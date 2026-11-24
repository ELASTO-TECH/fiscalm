"""fiscalm HTTP server for the app and API.

The listener defaults to loopback. Browser requests authenticate with secure session cookies.
The public origin is pinned; forwarded headers require explicit proxy trust.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import os
import re
import subprocess
import time
import unicodedata
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fiscalm import (
    PRODUCT,
    ai,
    ai_login,
    audit,
    backup,
    companies,
    history,
    history_pdf,
    integrations as ig,
    links,
    pack,
    pdftext,
    updates,
)
from fiscalm.api import (
    actions,
    auth,
    lists,
    login,
    receipts as receipt_upload,
    sessions,
    signin,
    state as api,
    tokens as api_tokens,
    transport,
    uploads as upload_links,
    users,
    v1 as api_v1,
)
from fiscalm.api.actions import log
from fiscalm.bank import connections as bank_connect, enablebanking, upload as su
from fiscalm.commerce import emag_sync, shopify
from fiscalm.company_config import listing
from fiscalm.config_errors import ConfigError
from fiscalm.efactura import client as anaf
from fiscalm.invoicing import branding, engine as invoicing, workspace as invoicing_api
from fiscalm.ledger import AuthorizationRefused, Ledger, is_live
from fiscalm.mail import invoices as mail_invoices
from fiscalm.pack import LUNI
from fiscalm.products import engine as products, stats as product_stats
from fiscalm.settlements import upload as bank_upload

# The fiscalm app (Next.js static export): code, fonts and icons only, no data.
APP_OUT = Path(os.environ.get("FISCALM_APP_OUT", Path(__file__).resolve().parents[3] / "app" / "out"))
APP_PUBLIC = ("_next/", "fiscalm/")
APP_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}
THEME = ("#111516", "#111516")  # the app's dark background, also its status bar (app/src/app/globals.css --bg)


def require(user_id: str | None, need: str) -> bool:
    """Account checks for single-use integration callbacks, whose owner was recorded at start."""
    if not user_id:
        return False
    with contextlib.closing(Ledger(readonly=True)) as L:
        row = L.db.execute("SELECT role FROM users WHERE id=? AND disabled_at IS NULL", (user_id,)).fetchone()
        return auth.require(dict(row) if row else None, need, has_admin=auth.has_admin(L))


def inline(name: str) -> str:
    """Content-Disposition for a file shown in the page: an ASCII fallback name (HTTP headers are Latin-1) and the
    real one, diacritics included, in RFC 5987 form."""

    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in ascii_)[:80] or "fisier"
    return f"inline; filename=\"{safe}\"; filename*=UTF-8''{urllib.parse.quote(name[:120], safe='')}"


def manifest() -> dict:
    """The installed app opens the public shell; no credential is put in its URL."""
    start = "/"
    return {
        "name": PRODUCT,
        "short_name": PRODUCT,
        "id": start,
        "start_url": start,
        "scope": start,
        "display": "standalone",
        "lang": "ro",
        "background_color": THEME[0],
        "theme_color": THEME[1],
        "icons": [
            {"src": "/fiscalm/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/fiscalm/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }


class Server(ThreadingHTTPServer):
    """Own and close the optional CLI login child with this listener."""

    # A proxy (Tailscale serve) opens one connection per request: a page load bursts ~20. socketserver's default
    # backlog of 5 makes the kernel drop the rest, and the browser waits 1, 3, 7 s before connecting again.
    request_queue_size = 128

    def __init__(self, *args, **kwargs) -> None:
        self.clock = kwargs.pop("clock", lambda: datetime.now(UTC))
        self.boundary = transport.Boundary.configured()
        self.browser_auth = login.BrowserAuth(self.clock, self.boundary.origin)
        self.ai_login = ai_login.Login()
        super().__init__(*args, **kwargs)

    def server_close(self) -> None:
        self.ai_login.cancel()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "fiscalm"
    timeout = 30  # socket reads, not a handler execution deadline

    def log_message(self, fmt, *args):  # no paths (they hold the token) in logs
        pass

    def log_request(self, code="-", size="-") -> None:
        self._status = code  # for the access line (_dispatch); send_response calls this once per answer

    def _auth(self) -> tuple[dict, str] | None:
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/web/"):
            return None
        with contextlib.closing(Ledger()) as L:
            current = sessions.identify(
                L, self.server.boundary.cookie(self.headers, self.connection), self.server.clock()
            )
        if not current:
            return None
        self.principal, self.app_user = current, current["user_id"]
        return current, path[5:]

    def _require(self, need: str) -> bool:
        with contextlib.closing(Ledger(readonly=True)) as L:
            current = sessions.lookup(L, self.principal["id_hash"], self.server.clock())
            return auth.require(current, need, has_admin=auth.has_admin(L))

    def _auth_post(self) -> None:
        self.close_connection = True
        if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
            return self._bad(415)
        if not self.server.boundary.mutation(self.headers, self.connection):
            return self._bad(403)
        body, status = self._json_body(4096)
        if status:
            return self._bad(status)
        try:
            with contextlib.closing(Ledger()) as L:
                principal = sessions.identify(
                    L, self.server.boundary.cookie(self.headers, self.connection), self.server.clock()
                )
                result = self.server.browser_auth.post(
                    L,
                    urllib.parse.urlsplit(self.path).path,
                    body,
                    principal,
                    agent=self.headers.get("User-Agent", ""),
                    ip=self.server.boundary.client_ip(self.headers, self.client_address[0]),
                )
        except PermissionError:
            return self._json(401, dict(ok=False, message=signin.FAILED))
        except Exception as exc:
            with contextlib.suppress(OSError):
                log(f"auth: failed {type(exc).__name__}")
            return self._json(503, dict(ok=False, message=signin.BUSY), {"Retry-After": "5"})
        extra = {}
        if result.session_id:
            extra["Set-Cookie"] = sessions.set_cookie(result.session_id)
        elif result.clear_session:
            extra["Set-Cookie"] = sessions.clear_cookie()
        if result.retry_after:
            extra["Retry-After"] = str(result.retry_after)
        self._json(result.status, result.body, extra)

    def _me(self) -> None:
        with contextlib.closing(Ledger(readonly=True)) as L:
            self._json(
                200,
                auth.me(
                    L,
                    self.principal,
                    self.server.clock(),
                    passkeys_available=self.server.browser_auth.passkeys is not None,
                ),
            )

    def _bootstrap(self, arg) -> None:
        # One SQLite snapshot: authorization, identity and projections cannot observe
        # different roles or sessions. Never identify a second cookie mid-request.
        with contextlib.closing(Ledger(readonly=True)) as L:
            L.db.execute("BEGIN")
            current = sessions.lookup(L, self.principal["id_hash"], self.server.clock())
            if not current:
                return self._bad(401)
            identity = auth.me(
                L,
                current,
                self.server.clock(),
                passkeys_available=self.server.browser_auth.passkeys is not None,
            )
            state = None
            if not current["recovery_only"]:
                co = arg("co") or api.ALL
                if co not in (*companies.all(L), api.ALL):
                    co = api.ALL
                state = lists.summary(L, co, can_manage_companies=identity["me"]["can"]["manage_firms"])
            self._json(
                200, dict(identity=identity, state=state, live=state["live"] if state is not None else is_live())
            )

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", (extra or {}).pop("Cache-Control", "no-store"))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            if "Content-Security-Policy" not in (extra or {}):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self' "
                    + script_hashes(body)
                    + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                )
            self.send_header("X-Frame-Options", "DENY")
        if (extra or {}).get("Content-Disposition", "").startswith("inline;"):
            prefix = "" if ctype.split(";", 1)[0] == "application/pdf" else "sandbox; "
            self.send_header("Content-Security-Policy", prefix + "default-src 'none'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _404(self):
        self._send(404, b"not found")

    def _json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _bank_return(self) -> None:
        """The bank sends the browser back here after the person confirmed (or cancelled). Public: what proves it is
        the single-use state fiscalm issued (bank_connect.finish). The page says what happened and nothing else: no
        token, no account beyond its last four digits."""

        query = {
            k: v[0]
            for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()
            if k in ("state", "code", "error") and v
        }
        with contextlib.closing(Ledger()) as L:
            try:
                r = bank_connect.finish(L, query, may_connect=lambda name: require(name, "connect_integrations"))
                title = "Conturile se pot alege în fiscalm"
                text = "Conturile se aleg în fiscalm, la firma pentru care a început conectarea, în cel mult 15 minute. Niciun cont nu se citește până atunci."
                log(f"bank-return {r['company']} {r['bank']}: prepared {r['count']} accounts")
                code = 200
            except enablebanking.BankError as e:
                title, text, code = "Banca nu a fost conectată", e.message, 400
                log(f"bank-return: refused {e.code or 'error'}")
            except Exception as exc:
                title, text, code = (
                    "Banca nu a fost conectată",
                    "A apărut o eroare. Conectează din nou banca.",
                    500,
                )
                log(f"bank-return: failed {type(exc).__name__}")
        self._outcome(code, title, text)

    def _outcome(self, code: int, title: str, text: str, extra: dict | None = None) -> None:
        """The page a browser lands on after an outside service sent it back: what happened, nothing else."""
        body = (
            f'<!doctype html><html lang="ro"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><title>{html.escape(title)}</title>'
            f'<body style="background:Canvas;color:CanvasText;font:17px/1.5 system-ui;max-width:32rem;margin:3rem auto;'
            f'padding:0 1rem"><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>'
            f'<p><a href="/?tab=setari&amp;sec=integrari">Revino în {PRODUCT}</a></p></body></html>'
        )
        self._send(code, body.encode(), "text/html; charset=utf-8", extra)

    def _anaf_start(self) -> None:
        """A start link from "Conectează ANAF" (anaf.start), opened in the browser holding the certificate: once, it
        sets the cookie that binds ANAF's return to this browser and sends it to ANAF's login."""

        link = urllib.parse.urlsplit(self.path).path[len(anaf.START_PATH) :]
        with contextlib.closing(Ledger()) as L:
            try:
                url, browser = anaf.open_start(L, link)
            except anaf.ConnectError as e:
                log(f"anaf-start: refused {e.code}")
                return self._outcome(400, "ANAF nu a fost conectat", e.message)
            except Exception as exc:
                log(f"anaf-start: failed {type(exc).__name__}")
                return self._outcome(500, "ANAF nu a fost conectat", anaf.STATE_BAD)
        log("anaf-start: sent to ANAF")
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header(
            "Set-Cookie",
            f"{anaf.COOKIE}={browser}; Path={anaf.CALLBACK_PATH}; Max-Age={anaf.STATE_TTL}; "
            "HttpOnly; Secure; SameSite=Lax",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _anaf_callback(self) -> None:
        """ANAF sends the browser back here with a one-time code (anaf.finish decides). Public: what proves the
        attempt is the cookie the start link set, once. Only `code` and `error` are read; the page shows no token.
        A connection, or a refusal of this browser's attempt, goes back to the app, which shows it."""

        query = {
            k: v[0]
            for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()
            if k in ("code", "error") and v
        }
        browser = ""
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == anaf.COOKIE:
                browser = value
        clear = {"Set-Cookie": f"{anaf.COOKIE}=; Path={anaf.CALLBACK_PATH}; Max-Age=0; HttpOnly; Secure; SameSite=Lax"}
        back = "/?tab=setari&sec=integrari"
        with contextlib.closing(Ledger()) as L:
            try:
                r = anaf.finish(L, query, browser, lambda name: require(name, "connect_integrations"))
                day = r["expires"]
                title, code = "ANAF este conectat", 303
                text = (
                    f"Facturile din e-Factura se citesc de acum. Conexiunea expiră pe {int(day[8:10])} "
                    f"{LUNI[int(day[5:7]) - 1]} {day[:4]}."
                )
                log("anaf-callback: connected")
            except anaf.ConnectError as e:
                title, text, code = "ANAF nu a fost conectat", e.message, 400
                log(f"anaf-callback: refused {e.code}")
                if e.integration:  # the integration's sheet shows why
                    code, back = 303, f"{back}&int={urllib.parse.quote(e.integration, safe='')}"
            except Exception as exc:
                title, text, code = "ANAF nu a fost conectat", anaf.EXCHANGE, 500
                log(f"anaf-callback: failed {type(exc).__name__}")
        if code == 303:
            clear["Location"] = back
        self._outcome(code, title, text, clear)

    def _invoice_error(self, code: int, message: str) -> None:
        title = "Factura nu poate fi deschisă"
        body = (
            f'<!doctype html><html lang="ro"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="color-scheme" content="light dark">'
            f'<title>{title}</title><body style="background:Canvas;color:CanvasText"><h1>{title}</h1>'
            f"<p>{html.escape(message)}</p></body></html>"
        )
        self._send(code, body.encode(), "text/html; charset=utf-8")

    def _app_file(self, rel: str) -> None:
        """A file of the app's static export, strictly under APP_OUT: no traversal, no dotfiles, known types only."""
        rel = urllib.parse.unquote(rel)
        parts = rel.split("/")
        root = APP_OUT.resolve()
        ctype = APP_TYPES.get(Path(rel).suffix.lower())
        if (
            not ctype
            or "\\" in rel
            or "\x00" in rel
            or any(p in ("", ".", "..") or p.startswith(".") for p in parts)
            or not root.is_dir()
        ):
            return self._404()
        f = (root / rel).resolve()
        if not f.is_relative_to(root) or not f.is_file():
            return self._404()
        cache = "public, max-age=31536000, immutable" if rel.startswith("_next/static/") else "public, max-age=3600"
        self._send(200, f.read_bytes(), ctype, {"Cache-Control": cache})

    def _upload(self) -> None:
        """Upload links authorize only this three-route surface, never the owner's API."""

        parts = self.path.split("?", 1)[0].split("/")
        token = parts[2] if len(parts) > 2 else ""
        rest = "/".join(parts[3:])
        if not upload_links.TOKEN.fullmatch(token):
            return self._404()
        L = Ledger()
        admitted = False
        reservation = None
        received = receipt_upload.Received(self.rfile)
        try:
            link = upload_links.verify(L, token)
            if not link:
                time.sleep(0.5)
                return self._404()
            if self.command == "GET":
                return self._upload_get(rest, link)
            if self.command != "POST" or rest != "api/files":
                return self._404()
            admitted = receipt_upload.ADMISSION.acquire(blocking=False)
            if not admitted:
                with contextlib.suppress(receipt_upload.Refused):
                    receipt_upload.begin_request(L, token)
                return self._send(503, b'{"ok": false}', "application/json", {"Retry-After": "5"})
            receipt_upload.begin_request(L, token)
            n, ctype = self._upload_headers()
            deadline = time.monotonic() + receipt_upload.body_deadline(n)
            reservation = receipt_upload.reserve_bytes(L, token, n)
            with receipt_upload.multipart(ctype, received, n, link["company"], self.connection, deadline) as files:
                result = receipt_upload.accept(L, token, files, reserved=True, deadline=deadline)
            status, response = 200, {"ok": True, "files": result}
        except receipt_upload.Refused as e:
            status, response = e.status, {"ok": False}
        except (OSError, ValueError, UnicodeError):
            status, response = 400, {"ok": False}
        finally:
            try:
                if reservation is not None:
                    receipt_upload.settle_bytes(L, reservation, received.bytes)
            finally:
                if admitted:
                    receipt_upload.ADMISSION.release()
                self.close_connection = True
                L.close()
        if status == 404:
            time.sleep(0.5)
            return self._404()
        self._json(status, response)

    def _upload_get(self, rest: str, link: dict) -> None:
        if rest == "":
            page = APP_OUT / "u.html"
            if page.is_file():
                body = page.read_bytes()
                csp = (
                    "default-src 'self'; script-src 'self' "
                    + script_hashes(body)
                    + "; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
                    "font-src 'self'; connect-src 'self'; object-src 'none'; "
                    "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
                )
                return self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": csp})
            return self._send(
                200,
                (
                    "<!doctype html><html lang=ro><meta charset=utf-8>"
                    f"<title>{PRODUCT}</title><body>{PRODUCT}</body></html>"
                ).encode(),
                "text/html; charset=utf-8",
                {"Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"},
            )
        if rest == "api/info":
            return self._json(
                200,
                {
                    "company": "Toate firmele" if link["company"] == "ALL" else companies.all()[link["company"]].name,
                    "label": link["label"],
                },
            )
        return self._404()

    def _upload_headers(self) -> tuple[int, str]:
        if not (
            self.server.boundary.secure(self.headers, self.connection)
            and self.server.boundary.accepts_origin(self.headers)
        ):
            raise receipt_upload.Refused(403)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            raise receipt_upload.Refused(400)
        try:
            n = int(lengths[0])
        except ValueError:
            raise receipt_upload.Refused(400) from None
        if n <= 0:
            raise receipt_upload.Refused(400)
        if n > receipt_upload.REQUEST_MAX:
            raise receipt_upload.Refused(413)
        ctype = self.headers.get("Content-Type", "")
        if not ctype.lower().startswith("multipart/form-data;"):
            raise receipt_upload.Refused(415)
        return n, ctype

    def _document_upload(self, arg) -> None:
        """POST api/document-upload?co=KEY: receipt photos and invoice PDFs from a signed-in member, multipart like a
        Bondrop link's, stored by receipt_upload.accept_member the same way. Local work, so allowed in Sandbox. The
        query names only the company and maybe `tx`, a card debit of it: then the one file is that debit's bon and
        explains it (links.receipt), or a 409 says why not. No file name is read, kept or logged."""

        self.close_connection = True
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        co, tx = arg("co"), arg("tx")
        if co not in companies.all() or set(query) - {"tx"} != {"co"} or any(len(v) != 1 for v in query.values()):
            return self._json(400, {"ok": False})
        L, admitted, files = Ledger(), receipt_upload.ADMISSION.acquire(blocking=False), []
        received, reservation = receipt_upload.Received(self.rfile), None
        sender = receipt_upload.member(co, self.app_user)
        try:
            if not admitted:
                return self._json(503, {"ok": False}, {"Retry-After": "5"})
            n, ctype = self._upload_headers()
            deadline = time.monotonic() + receipt_upload.body_deadline(n)
            with L.guard_writes(lambda current: auth.authorize_write(current, self.principal, self.server.clock())):
                reservation = receipt_upload.reserve_bytes(L, sender, n)
                if tx:
                    links.receipt_allowed(L, co, tx)  # before any byte is stored; links.receipt checks again
                with receipt_upload.multipart(ctype, received, n, co, self.connection, deadline) as parts:
                    if tx and len(parts) != 1:
                        raise receipt_upload.Refused(400)
                    files = receipt_upload.accept_member(L, sender, parts, deadline=deadline)
                if tx:
                    links.receipt(L, co, tx, files[0]["sha"], self.app_user)
            status, body = 200, {"ok": True, "files": files}
        except links.LinkRefused as e:
            status, body = 409, {"ok": False, "message": links.REFUSED.get(e.code, links.REFUSED["receipt"])}
        except AuthorizationRefused:
            status, body = 403, {"ok": False}
        except receipt_upload.Refused as e:
            status, body = e.status, {"ok": False}
        except (OSError, ValueError, UnicodeError):
            status, body = 400, {"ok": False}
        finally:
            try:
                if reservation is not None:  # what was actually read stays charged, even for a refused body
                    receipt_upload.settle_bytes(L, reservation, received.bytes)
            finally:
                if admitted:
                    receipt_upload.ADMISSION.release()
                L.close()
        log(f"document-upload {co}: {len(files)} stored" if files else f"document-upload {co}: refused {status}")
        self._json(status, body)

    def _statement_upload(self) -> None:
        """POST api/statement-upload?co=KEY[&bank=ID]: one statement's bytes, typed application/pdf or application/xml,
        or several BT PDFs of one month as multipart/form-data `files`, imported together (su.accept_pdfs).
        Admin only (it settles money once matched); local only, so allowed before the install goes live."""

        self.close_connection = True

        def refuse(code, message):
            return self._json(code, {"ok": False, "message": message, "state": None})

        if not self._require("manage_firms"):
            return refuse(403, "Doar administratorii pot încărca extrase.")
        lengths = self.headers.get_all("Content-Length", [])
        if (
            len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
            or self.headers.get("Transfer-Encoding")
        ):
            return refuse(400, actions.BAD)
        digits = lengths[0].lstrip("0") or "0"
        ctype = self.headers.get("Content-Type") or ""
        several = ctype.lower().startswith("multipart/form-data;")  # BT PDFs of one statement, sent together
        if len(digits) > 9 or int(digits) > (receipt_upload.REQUEST_MAX if several else su.MAX_PDF):
            return refuse(413, su.TOO_BIG)
        n = int(digits)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        co, bank, account = query.get("co", [""])[0], query.get("bank", [""])[0], query.get("account", [""])[0]
        if (
            co not in companies.all()
            or n == 0
            or any(len(v) != 1 for v in query.values())
            or set(query) - {"co", "bank", "account", "report", "date", "partner", "format"}
            or query.get("report", ["1"]) != ["1"]
            or ("report" not in query and bool(set(query) & {"date", "partner", "format"}))
        ):
            return refuse(400, actions.BAD)
        if query.get("report") == ["1"] and n > bank_upload.LIMIT:
            return refuse(413, bank_upload.TOO_BIG)
        if several and set(query) - {"co", "bank"}:
            return refuse(400, actions.BAD)
        try:
            data = self._statement_files(ctype, n, co) if several else self.rfile.read(n)
        except receipt_upload.Refused as e:
            return refuse(e.status, su.TOO_BIG if e.status == 413 else su.NOT_A_STATEMENT)
        if not several and len(data) != n:
            return refuse(400, actions.BAD)
        with (
            contextlib.closing(Ledger()) as L,
            L.guard_writes(
                lambda current: auth.authorize_write(current, self.principal, self.server.clock(), "manage_firms")
            ),
        ):
            try:
                if query.get("report") == ["1"]:
                    raw_format = query.get("format", [""])[0]
                    if len(raw_format) > 4096:
                        return refuse(400, actions.BAD)
                    try:
                        description = json.loads(raw_format) if raw_format else None
                    except (ValueError, RecursionError):
                        return refuse(400, actions.BAD)
                    res = bank_upload.accept_report(
                        L,
                        co,
                        data,
                        self.headers.get("Content-Type") or "",
                        report_date=query.get("date", [""])[0],
                        description=description,
                        partner=query.get("partner", [""])[0],
                    )
                elif several:
                    res = su.accept_pdfs(L, co, bank, data)
                else:
                    res = su.accept(L, co, bank, data, ctype, account)
            except AuthorizationRefused:
                return refuse(403, actions.ROLE_REFUSALS["manage_firms"])
            except bank_upload.Refused as e:
                return self._json(
                    e.status, {"ok": False, "message": e.message, "state": None, "report": e.extra or None}
                )
            except su.Refused as e:
                log(f"statement-upload {co}: refused {e.status}")
                if e.extra:  # a CSV's first upload: its columns and first rows, for picking which is which
                    return self._json(e.status, {"ok": False, "message": e.message, "state": None, "csv": e.extra})
                return refuse(e.status, e.message)
            except Exception as exc:
                log(f"statement-upload {co}: failed {type(exc).__name__}")
                return refuse(500, actions.ERROR)
            log(f"statement-upload {co}: {res['status']} {res.get('month', '')}")
            body = {
                "ok": True,
                "message": res["message"],
                "done": {k: res[k] for k in ("status", "month", "transactions") if k in res},
            }
            try:
                body["state"] = lists.summary(L, co, can_manage_companies=self._require("manage_firms"))
            except Exception as exc:
                log(f"statement-upload: state after failed: {type(exc).__name__}")
                body["state"] = None
            return self._json(200, body)

    def _statement_files(self, ctype: str, n: int, co: str) -> list[bytes]:
        """A multipart body's files, read whole: several PDFs of one statement, at most su.MAX_FILES."""
        deadline = time.monotonic() + receipt_upload.body_deadline(n)
        with receipt_upload.multipart(ctype, self.rfile, n, co, self.connection, deadline, su.MAX_FILES) as parts:
            return [fh.read() for _, fh in parts]

    def _history_upload(self) -> None:
        """POST api/history-upload?co=KEY&start=YYYY-MM-DD: one e-Factura XML or a ZIP of them, from another program or
        SPV (fiscalm.history). Admin only; local only (nothing leaves the install), so allowed before the install goes live.
        The query names the company and the start date, nothing else; no request names a file or a path."""

        self.close_connection = True

        def refuse(code, message):
            return self._json(code, {"ok": False, "message": message, "state": None})

        if not self._require("manage_firms"):
            return refuse(403, "Doar administratorii pot importa facturi.")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in (
            "application/zip",
            "application/x-zip-compressed",
            "application/xml",
            "text/xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ):
            return refuse(415, history.MESSAGES_RO["unsupported_file"])
        lengths = self.headers.get_all("Content-Length", [])
        if (
            len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
            or self.headers.get("Transfer-Encoding")
        ):
            return refuse(400, actions.BAD)
        digits = lengths[0].lstrip("0") or "0"
        if len(digits) > 10 or int(digits) > history.MAX_FILE:
            return refuse(413, history.MESSAGES_RO["too_big"])
        n = int(digits)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        co, start = query.get("co", [""])[0], query.get("start", [""])[0]
        if (
            co not in companies.all()
            or n == 0
            or set(query) - {"co", "start"}
            or any(len(v) != 1 for v in query.values())
        ):
            return refuse(400, actions.BAD)
        data = self.rfile.read(n)
        if len(data) != n:
            return refuse(400, actions.BAD)
        with (
            contextlib.closing(Ledger()) as L,
            L.guard_writes(
                lambda current: auth.authorize_write(current, self.principal, self.server.clock(), "manage_firms")
            ),
        ):
            try:
                res = history.import_file(L, co, data, start, date.today())
            except AuthorizationRefused:
                return refuse(403, actions.ROLE_REFUSALS["manage_firms"])
            except history.Refused as e:
                log(f"history-upload {co}: refused {e.code}")
                return refuse(422, history.message_ro(e))
            except Exception as exc:
                log(f"history-upload {co}: failed {type(exc).__name__}")
                return refuse(500, actions.ERROR)
            log(
                f"history-upload {co}: {res.history_issued + res.history_received} history, "
                f"{res.issued + res.received} tracked, {res.duplicates} known, {res.unreadable_count} unreadable"
            )
            body = {
                "ok": True,
                "message": history.done_ro(res),
                "done": {k: v for k, v in res.as_dict().items() if k not in ("company", "unreadable")},
            }
            try:
                body["state"] = lists.summary(L, co, can_manage_companies=self._require("manage_firms"))
            except Exception as exc:
                log(f"history-upload: state after failed: {type(exc).__name__}")
                body["state"] = None
            return self._json(200, body)

    def _invoice_file(self, name: str, arg) -> None:

        co = arg("co")
        missing = f"Factura nu a fost găsită. Încearcă din nou din {PRODUCT}."
        if co not in companies.all():
            return self._404() if name == "logo" else self._invoice_error(404, missing)
        if name == "logo":
            with contextlib.closing(Ledger()) as L:
                try:
                    explicit = "sha" in urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query, keep_blank_values=True
                    )
                    logo = branding.logo_for(L, co, arg("sha")) if explicit else branding.current_logo(L, co)
                    if logo is None:
                        return self._404()
                    kind, data = logo.kind, branding.original(L, co, logo.sha)
                except (invoicing.InvoiceError, OSError):
                    return self._404()
            return self._send(
                200,
                data,
                "image/png" if kind == "png" else "image/jpeg",
                {
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                    "Cache-Control": "private, max-age=60" if explicit else "no-store",
                },
            )
        preview = name == "preview.pdf"
        if not preview and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}\.(pdf|xml)", name):
            return self._invoice_error(404, missing)
        iid = arg("draft") if preview else name.rsplit(".", 1)[0]
        with contextlib.closing(Ledger()) as L:
            try:
                data = invoicing_api.file(L, co, iid, name.rsplit(".", 1)[1], preview=preview)
            except invoicing.InvoiceError as error:
                if str(error) == branding.UNAVAILABLE:
                    return self._invoice_error(409, branding.UNAVAILABLE)
                return self._invoice_error(404, missing)
            except (ValueError, OSError):
                return self._invoice_error(404, missing)
        self._send(
            200,
            data,
            "application/pdf" if name.endswith(".pdf") else "application/xml",
            {"Content-Disposition": inline(name)},
        )

    def _packfile(self, q) -> None:
        co, month, version = q("co"), q("month"), q("v")
        with contextlib.closing(Ledger()) as ledger:
            try:
                data = pack.file(ledger, co, month, version)
            except (ValueError, OSError):
                return self._send(404, b"", "text/plain")
            name = pack.archive_name(companies.get(co, L=ledger), month)
        self._send(200, data, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})

    def _products(self, identity, arg) -> None:
        co = arg("co")
        if co not in companies.all():
            return self._bad()
        with contextlib.closing(Ledger()) as L:
            try:
                out = (
                    {"product": products.get(L, co, arg("id"))}
                    if arg("id")
                    else products.list_page(L, co, {k: arg(k) for k in ("q", "f", "cursor", "limit") if arg(k)})
                )
            except products.ProductError:
                return self._404() if arg("id") else self._bad()
        self._json(200, out)

    def _product_stats(self, identity, arg) -> None:
        co = arg("co")
        if co not in companies.all():
            return self._bad()
        try:
            count = int(arg("months") or "1")
            with contextlib.closing(Ledger()) as L:
                out = product_stats.sales(L, co, arg("month") or self.server.clock().strftime("%Y-%m"), count)
        except (products.ProductError, ValueError):
            return self._bad()
        self._json(200, out)

    def _named_file_get(self, identity, arg) -> None:
        file_id = int(identity[1].rsplit("/", 1)[1])
        if not 0 < file_id < 2**63:
            return self._404()
        with contextlib.closing(Ledger()) as L:
            out = api.payment_file_detail(L, file_id)
        if out is None:
            return self._404()
        self._json(200, out)

    def _named_file_post(self, identity, arg) -> None:
        body, status = self._json_body(4096)
        if status or not isinstance(body, dict):
            return self._bad(status or 400)
        action = body.get("action")
        spec = actions.ACTIONS.get(action) if isinstance(action, str) else None
        if spec is None or not action.startswith("payment_file_"):
            return self._bad()
        args = {k: v for k, v in body.items() if k != "action"}
        if not spec.valid(args) or not set(spec.schema["required"]) <= args.keys():
            return self._bad()
        if "file_id" in args and not 0 < args["file_id"] < 2**63:
            return self._404()
        with contextlib.closing(Ledger()) as L:
            f = L.payment_file(args["file_id"]) if "file_id" in args else None
            co = f["company"] if f else args.get("co")
        if co not in companies.all() or "file_id" in args and not f:
            return self._404()
        try:
            out = actions.act(
                action,
                co,
                args,
                authorize=lambda L: auth.authorize_write(L, self.principal, self.server.clock(), spec.role),
                require=self._action_right,
                at=self.server.clock(),
                origin=self.headers.get("Origin") or "",
            )
        except (PermissionError, AuthorizationRefused):
            return self._bad(403)
        self._json(200, out)

    def _history_review(self, arg, *, document: bool = False) -> None:
        co = arg("co")
        if co not in companies.all():
            return self._404()
        with contextlib.closing(Ledger()) as L:
            if not document:
                return self._json(200, {"rows": history_pdf.pending(L, co)})
            raw = history_pdf.document(L, co, arg("sha"))
        if not raw:
            return self._404()
        return self._send(200, raw, "application/pdf", {"Content-Disposition": inline("document.pdf")})

    def _mailfile(self, arg) -> None:
        """The PDF of a mailed document still waiting for an answer, found by its digest; checked again on the bytes
        (mail_invoices.pending_file). Nothing in the request names a path; a mail's attachment name is never used."""
        co, key = arg("co"), arg("key")
        if co not in companies.all() or not re.fullmatch(r"[0-9a-f]{64}", key):
            return self._404()

        with contextlib.closing(Ledger()) as L:
            got = mail_invoices.pending_file(L, key)
            offered = got and any(q["key"] == key for q in api._to_check(L, co))
        if not got or not offered or got[0] != "pdf":
            return self._404()
        data = got[1]
        if arg("png"):
            png = pdftext.first_page_png(data)  # the preview is a picture: nothing in the PDF runs in the page
            return self._send(200, png, "image/png") if png else self._404()
        self._send(200, data, "application/pdf", {"Content-Disposition": inline("document.pdf")})

    def _docfile(self, arg) -> None:
        """A receipt's file: the row is found by sha and company, and only the path the ledger stored is served
        (api.doc_file keeps it inside the receipt folders). Nothing in the request names a path."""
        co = arg("co")
        if co not in companies.all() or not arg("sha"):
            return self._404()
        with contextlib.closing(Ledger()) as L:
            got = api.read_doc(L, co, arg("sha")) or api.upload_assignment.pending_file(L, arg("sha"))
        if not got:
            return self._404()
        name, data = got
        cache = {"Cache-Control": "private, max-age=3600"}  # a sha names one file's bytes
        if Path(name).suffix.lower() == ".pdf" and arg("png"):
            png = pdftext.first_page_png(data)  # the preview is a picture: nothing in the PDF runs in the page
            return self._send(200, png, "image/png", cache) if png else self._404()
        self._send(200, data, api.MEDIA[Path(name).suffix.lower()], {**cache, "Content-Disposition": inline(name)})

    def _dispatch(self) -> None:
        """An app read leaves one access line: its route, status and duration, never the query, an id or the user."""
        route = access_route(self.command, self.path)
        if route is None:
            return self._answer()
        self._status, started, failure = "-", time.monotonic(), ""
        try:
            self._answer()
        except BaseException as e:
            failure = f" {type(e).__name__}"
            raise
        finally:
            with contextlib.suppress(OSError):
                log(
                    f"web: {self.command} {route} {self._status} {round((time.monotonic() - started) * 1000)}ms{failure}"
                )

    def _answer(self) -> None:
        if self.path != "/healthz" and not self.server.boundary.accepts_host(self.headers):
            return self._404()
        route = _find_route(self.command, self.path, False)
        identity = None
        if route is None:
            if self.command not in ("GET", "POST"):
                return self._send(405, b"method not allowed")
            identity = self._auth()
            if not identity:
                return (
                    self._json(
                        401,
                        dict(
                            ok=False, methods=dict(password=True, passkey=self.server.browser_auth.passkeys is not None)
                        ),
                    )
                    if self.path.startswith("/web/")
                    else self._404()
                )
            route = _find_route(self.command, identity[1], True)
        if route is None:
            return self._404()
        if route.auth and identity[0]["recovery_only"] and identity[1] not in ("api/me", "api/bootstrap"):
            return self._bad(403)
        if route.auth and route.role and not self._require(route.role):
            return (
                self._404()
                if route.denial == 404
                else self._json(403, dict(ok=False, message=actions.ROLE_REFUSALS.get(route.role, actions.BAD)))
            )
        if route.auth and self.command == "POST" and not self._post_guard(identity[1][4:]):
            return
        if route.firm and not companies.all():
            return self._json(409, {"ok": False, "message": companies.NEEDS_COMPANY_RO})
        q = urllib.parse.parse_qs(self.path.partition("?")[2])
        reset = audit.actor.set("user " + self.app_user) if route.auth else None
        try:
            route.handler(self, identity, lambda key: q.get(key, [""])[0])
        finally:
            if reset is not None:
                audit.actor.reset(reset)

    do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _dispatch

    def _index(self, _, arg) -> None:
        index = APP_OUT / "index.html"
        if not index.is_file():
            return self._send(404, f"Aplicația {PRODUCT} nu este instalată încă.".encode())
        self._send(200, index.read_bytes(), "text/html; charset=utf-8")

    def _emag_pdf(self) -> None:
        with contextlib.closing(Ledger()) as L:
            body = emag_sync.pdf(L, self.path.removeprefix("/emag-pdf/").removesuffix(".pdf"))
        return self._send(200, body, "application/pdf") if body else self._404()

    def _backup(self, rest: str) -> None:
        path = backup.latest()
        if rest == "api/backup":
            return self._json(
                200, {"last_backup": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat() if path else None}
            )
        if path is None:
            return self._404()
        try:
            body = backup._read(path)
        except OSError:
            return self._404()
        self._send(200, body, "application/zip", {"Content-Disposition": 'attachment; filename="backup.zip"'})

    def _accounts(self, arg, *, kind: str = "users") -> None:
        with contextlib.closing(Ledger(readonly=True)) as L:
            reads = {
                "users": lambda: users.listing(L),
                "sessions": lambda: users.active_sessions(L, arg("id"), self.server.clock()),
                "events": lambda: [
                    dict(r)
                    for r in L.db.execute(
                        "SELECT at,kind,user_id,actor,detail FROM auth_events ORDER BY id DESC LIMIT 100"
                    )
                ],
            }
            self._json(200, reads[kind]())

    def _companies(self) -> None:
        with contextlib.closing(Ledger()) as L:
            try:
                return self._json(200, listing(L, inboxes=self._require("manage_integrations")))
            except ConfigError:
                return self._json(500, {"ok": False, "message": actions.ERROR})

    def _tokens(self, arg) -> None:
        if arg("co") and arg("co") not in (*companies.all(), api.ALL):
            return self._bad()
        with contextlib.closing(Ledger()) as L:
            owner = None if self._require("manage_tokens") else self.app_user
            return self._json(200, api_tokens.list_tokens(L, user_id=owner))

    def _ai_cli(self, arg) -> None:
        manager = self.server.ai_login
        if self.command == "GET":
            result = manager.snapshot()
            provider = arg("provider")
            if provider not in ai_login.DOMAINS:
                return self._bad()
            if result["state"] != "waiting":
                result["readiness"] = ai.cli_status(provider)
            result["install_command"] = updates.UPGRADE
            return self._json(200, result)
        body, status = self._json_body(4096)
        if status:
            return self._bad(status)
        if (
            not isinstance(body, dict)
            or set(body) - {"op", "provider", "code"}
            or not all(isinstance(v, str) for v in body.values())
        ):
            return self._bad()
        ops = {
            "start": lambda: manager.start(body.get("provider", "")),
            "code": lambda: manager.submit(body.get("code", "")),
            "cancel": manager.cancel,
            "logout": lambda: manager.logout(body.get("provider", "")),
        }
        if body.get("op") not in ops:
            return self._bad()
        try:
            ops[body["op"]]()
        except ValueError:
            return self._json(
                409,
                {
                    "ok": False,
                    "message": "Autentificarea nu a putut fi modificată. Se verifică starea și se reîncearcă.",
                },
            )
        return self._json(200, {"ok": True, **manager.snapshot()})

    def _integrations(self) -> None:
        with contextlib.closing(Ledger()) as L:
            items = ig.listing(L)
            statuses = {}
            for item in items:
                provider = item["config"].get("provider", "")
                if item["kind"] == "ai" and provider.endswith("-cli"):
                    if provider not in statuses:
                        statuses[provider] = ai.cli_status(provider)
                    item["cli_status"] = statuses[provider]
            return self._json(
                200,
                {
                    "kinds": ig.kinds(),
                    "items": items,
                    "coverage": ig.coverage(L),
                    "live": is_live(),
                    "can_manage_ai": self._require("connect_integrations"),
                },
            )

    def _api_read(self, what: str, build: Callable, arg) -> None:
        if not companies.all():
            if what == "state":
                return self._json(200, api.setup_state(self._require("manage_firms")))
            return self._json(409, {"ok": False, "message": companies.NEEDS_COMPANY_RO})
        co = arg("co")
        if co not in (*companies.all(), *((api.ALL,) if what == "state" else ())):
            return self._bad()
        try:
            with contextlib.closing(Ledger()) as L:
                out = build(
                    L, co, **({"can_manage_companies": self._require("manage_firms")} if what == "state" else {})
                )
        except Exception as e:
            log(f"web: api {what} failed: {type(e).__name__}")
            return self._json(500, {"ok": False, "message": actions.ERROR})
        self._json(200, out)

    def _state_list(self, arg, *, kind: str = "collection") -> None:
        if not companies.all() and kind != "summary":
            return self._json(409, {"ok": False, "message": companies.NEEDS_COMPANY_RO})
        query = {
            k: arg(k)
            for k in (
                "collection",
                "kind",
                "id",
                "partner",
                "month",
                "status",
                "cursor",
                "limit",
                "group",
                "tx",
                "q",
                "screen",
                "f",
                "from",
                "to",
            )
            if arg(k)
        }
        try:
            with contextlib.closing(Ledger()) as L:
                options = {"can_manage_companies": self._require("manage_firms")}
                out = {
                    "summary": lambda: lists.summary(L, arg("co"), **options),
                    "detail": lambda: lists.detail(L, arg("co"), query, **options),
                    "transaction-choices": lambda: lists.transaction_choices(L, arg("co"), query, **options),
                    "partner-pages": lambda: lists.partner_pages(L, arg("co"), query, **options),
                    "collection": lambda: lists.collection(L, arg("co"), query, **options),
                    "tasks": lambda: lists.task_page(L, arg("co"), query, **options),
                    "search": lambda: lists.search(L, arg("co"), query),
                    "browse": lambda: lists.browse(L, arg("co"), query, **options),
                    "report-pages": lambda: lists.report_pages(L, arg("co"), query, **options),
                    "dashboard-list": lambda: lists.dashboard_page(L, arg("co"), query, **options),
                }[kind]()
        except api_v1.Error as e:
            return self._json(e.status, {"ok": False, "code": e.code, "message": e.message})
        except Exception as e:
            log(f"web: api state-list failed: {type(e).__name__}")
            return self._json(500, {"ok": False, "message": actions.ERROR})
        self._json(200, out)

    def _bad(self, code: int = 400) -> None:
        self._json(code, {"ok": False, "message": actions.BAD})

    def _permission(self, action: str, phase: str) -> bool:
        spec = _action_spec(action)
        if spec is None and action.startswith(("rule-", "company-")):
            spec = next(s for name, s in actions.ACTIONS.items() if name.partition("-")[0] == action.partition("-")[0])
        if spec is None or spec.role == "member":
            return True
        when, status = PERMISSIONS.get(spec.role, ("origin", 403))
        if when != phase or self._require(spec.role):
            return True
        if status == 404:
            self._404()
        else:
            self._json(status, {"ok": False, "message": actions.ROLE_REFUSALS[spec.role]})
        return False

    def _post_guard(self, action: str) -> bool:
        """Check Origin before body handling; cross-site forms cannot send application/json."""
        if (
            action.startswith("integration-")
            and action != "integration-connect"
            and not self._require("manage_integrations")
        ):
            self._404()
            return False
        if not self._permission(action, "early"):
            return False
        spec = _action_spec(action)
        if not self._require("own_tokens" if spec and spec.role == "own_tokens" else "member"):
            self._json(403, dict(ok=False, message="Contul are acces doar pentru citire."))
            return False
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        # Raw upload handlers own their MIME allowlists and precise refusal messages.
        types = (
            ("multipart/form-data",)
            if action == "document-upload"
            else (content_type,)
            if action in ("statement-upload", "history-upload", "invoicing/logo")
            else ("application/json",)
        )
        if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() not in types:
            self.close_connection = True
            self._bad(415)
            return False
        if not self.server.boundary.mutation(self.headers, self.connection, media_types=types):
            self.close_connection = True
            self._bad(403)
            return False
        if auth.needs_recent(action) and not sessions.recent(self.principal, self.server.clock()):
            self._json(401, dict(ok=False, reauth=True, message=auth.REAUTH_MESSAGE))
            return False
        if not self._permission(action, "origin"):
            return False
        spec = _action_spec(action)
        needs_firm = spec.firm if spec else not action.startswith(("company-", "integration-", "token-"))
        if not companies.all() and needs_firm:
            self._json(409, {"ok": False, "message": companies.NEEDS_COMPANY_RO})
            return False
        return True

    def _json_body(self, limit: int) -> tuple[object, int]:
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return None, 415
        lengths = self.headers.get_all("Content-Length", [])
        if (
            len(lengths) != 1
            or self.headers.get("Transfer-Encoding")
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
        ):
            return None, 400
        digits = lengths[0].lstrip("0") or "0"
        if len(digits) > 9 or int(digits) > limit:
            return None, 413
        n = int(digits)
        data = self.rfile.read(n)
        if len(data) != n:
            return None, 400
        try:
            return json.loads(data.decode("utf-8")), 0
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None, 400

    def _logo_body(self) -> dict | None:
        self.close_connection = True
        lengths = self.headers.get_all("Content-Length", [])
        if (
            len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
            or self.headers.get("Transfer-Encoding")
        ):
            return self._logo_bad(400, actions.BAD)
        digits = lengths[0].lstrip("0") or "0"
        if len(digits) > 6 or int(digits) > branding.MAX_BYTES:
            return self._logo_bad(413, branding.SIZE)
        n = int(digits)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() not in ("image/png", "image/jpeg"):
            return self._logo_bad(415, branding.INVALID)
        co = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("co", [""])[0]
        if co not in companies.all() or n == 0:
            return self._logo_bad(400, actions.BAD)
        data = self.rfile.read(n)
        if len(data) != n:
            return self._logo_bad(400, branding.INVALID)
        return dict(co=co, data=data)

    def _logo_bad(self, code: int, message: str) -> None:
        self._json(code, dict(ok=False, message=message, id=None, invoicing=None, state=None))

    def _invoice_post(self, identity, arg) -> None:
        op = identity[1].removeprefix("api/invoicing/")
        spec = invoicing_api.ACTIONS.get(op)
        if op == "logo":
            body = self._logo_body()
            if body is None:
                return
        else:
            body, status = self._json_body(spec.limit if spec else 4096 if op.startswith("recurrence-") else 65536)
            if status:
                return self._bad(status)
        if (
            not isinstance(body, dict)
            or spec is None
            or not isinstance(body.get("co"), str)
            or body["co"] not in companies.all()
        ):
            return self._bad()
        with contextlib.closing(Ledger()) as L:
            try:
                res = invoicing_api.write_app(
                    L,
                    op,
                    body["co"],
                    body,
                    require=self._action_right,
                    authorize=lambda: auth.authorize_write(L, self.principal, self.server.clock(), spec.role),
                )
            except PermissionError:
                return self._bad(403)
            except Exception as exc:
                log(f"invoicing: {op} failed: {type(exc).__name__}")
                return self._json(500, {"ok": False, "message": actions.ERROR})
            target = {"client-save": "client_id", "issue": "issued_id", "storno": "issued_id"}.get(op)
            navigation = {target: res["id"]} if target and res.get("id") and res["ok"] else {}
            for key, build in (("invoicing", invoicing_api.workspace), ("state", lists.summary)):
                try:
                    res[key] = build(
                        L,
                        body["co"],
                        **(
                            {"can_manage_companies": self._require("manage_firms"), **navigation}
                            if key == "state"
                            else {}
                        ),
                    )
                except Exception as exc:
                    log(f"invoicing: {key} after {op} failed: {type(exc).__name__}")
                    res[key] = None
        self._json(200, res)

    def _action_right(self, need: str) -> bool:
        return self._require(need)

    def _engine_post(self, identity, arg) -> None:
        action = identity[1][4:]
        body, status = self._json_body(4096)
        if status:
            return self._bad(status)
        if not self._permission(action, "body"):
            return
        spec = actions.ACTIONS.get(action)
        co = _engine_company(action, body, spec)
        if co is None:
            return self._bad()
        try:
            res = actions.act(
                action,
                co,
                body,
                created_by=self.app_user if spec.actor else None,
                authorize=lambda L: auth.authorize_write(
                    L, self.principal, self.server.clock(), spec.role, recent=auth.needs_recent(action)
                ),
                at=self.server.clock(),
                require=self._action_right,
                origin=self.headers.get("Origin") or "",
            )
        except (PermissionError, AuthorizationRefused) as exc:
            if str(exc) == "reauth_required":
                return self._json(401, dict(ok=False, reauth=True, message=auth.REAUTH_MESSAGE))
            return self._bad(403)
        except Exception as e:
            log(f"web: api {action} failed: {type(e).__name__}")
            return self._json(500, {"ok": False, "message": actions.ERROR})
        # The action may have committed; a failed refresh must preserve its response.
        try:
            with contextlib.closing(Ledger()) as L:
                res["state"] = lists.summary(L, co, can_manage_companies=self._require("manage_firms")) if co else None
        except Exception as e:
            log(f"web: api state after {action} failed: {type(e).__name__}")
            res["state"] = None
        self._json(200, res)


def access_route(method: str, path: str) -> str | None:
    """The app read `path` names, as one of ROUTES' own read routes (`api/payment-files/:id`); `api/?` for any other
    name, so nothing a request chose reaches the log. None when `path` is no app read."""
    if method != "GET" or not path.startswith("/web/api/"):
        return None
    name = path.split("?", 1)[0][5:]
    if name in READ_ROUTES:
        return name
    return "api/payment-files/:id" if re.fullmatch(r"api/payment-files/[0-9]+", name) else "api/?"


@dataclass(frozen=True)
class Route:
    handler: Callable
    auth: bool = True
    firm: bool = False
    raw: bool = False
    role: str = "read"
    denial: int = 403


def _find_route(method: str, path: str, authenticated: bool) -> Route | None:
    return next(
        (
            route
            for (verb, pattern), route in ROUTES.items()
            if route.auth == authenticated
            and verb in (method, "*")
            and re.fullmatch(pattern, path if route.raw else path.split("?", 1)[0])
        ),
        None,
    )


def _action_spec(name: str) -> actions.Action | None:
    return invoicing_api.ACTIONS.get(name[10:]) if name.startswith("invoicing/") else actions.ACTIONS.get(name)


def _engine_company(action: str, body: object, spec: actions.Action | None) -> str | None:
    company = action.startswith("company-")
    install = spec is not None and not spec.firm and not company
    if (
        not isinstance(body, dict)
        or spec is None
        or (company and not all(isinstance(v, str) for v in body.values()))
        or not (
            spec.valid(body)
            if action.startswith("product-")
            or action in ("integration-add", "integration-update", "mail-microsoft-start", "upload-link-create")
            else all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in body.values())
        )
        or (
            not company
            and (not install or "co" in body)
            and (
                not isinstance(body.get("co"), str)
                or body["co"] not in (*companies.all(), *((api.ALL,) if install else ()))
            )
        )
    ):
        return None
    return api.ALL if company else body.get("co", api.ALL if install else "")


PERMISSIONS = {
    "manage_integrations": ("early", 404),
    "connect_integrations": ("early", 403),
    "own_tokens": ("body", 403),
}

# One row per entry keeps the handler and policy together.
# fmt: off
ROUTES = {
    ('GET', '/emag-pdf/.*'): Route(lambda h, a, q: h._emag_pdf(), auth=False),
    ('*', '/u/.*'): Route(lambda h, a, q: h._upload(), auth=False),
    ('GET', '/healthz'): Route(lambda h, a, q: h._send(200, b'{"ok":true}', 'application/json'), raw=True, auth=False),
    ('HEAD', '/healthz'): Route(lambda h, a, q: h._send(200, b'{"ok":true}', 'application/json'), raw=True, auth=False),
    ('GET', '/bank/return'): Route(lambda h, a, q: h._bank_return(), auth=False),
    ('GET', '/anaf/callback'): Route(lambda h, a, q: h._anaf_callback(), auth=False),
    ('GET', '/anaf/start/.*'): Route(lambda h, a, q: h._anaf_start(), auth=False),
    ('POST', '/hooks/shopify/.*'): Route(lambda h, a, q: shopify.handle(h), auth=False),
    ('*', '/api/v1/.*'): Route(lambda h, a, q: api_v1.handle(h), auth=False),
    ('GET', '/(?:' + '|'.join(re.escape(p) for p in APP_PUBLIC) + ').*'): Route(lambda h, a, q: h._app_file(h.path.split('?', 1)[0][1:]), auth=False),
    ('GET', '/(?:setup)?'): Route(Handler._index, auth=False),
    ('POST', '/auth/.*'): Route(lambda h, a, q: h._auth_post(), auth=False),
    ('GET', 'api/bootstrap'): Route(lambda h, a, q: h._bootstrap(q), auth=True, role=''),
    ('GET', 'api/me'): Route(lambda h, a, q: h._me(), auth=True, role=''),
    ('GET', '/manifest\\.webmanifest'): Route(lambda h, a, q: h._send(200, json.dumps(manifest(), ensure_ascii=False).encode(), 'application/manifest+json'), auth=False),
    ('GET', 'invoicing/.*'): Route(lambda h, a, q: h._invoice_file(a[1][10:], q), auth=True, firm=True),
    ('GET', '(?:api/)?backup'): Route(lambda h, a, q: h._backup(a[1]), auth=True, role='manage_firms'),
    ('GET', 'api/companies'): Route(lambda h, a, q: h._companies(), auth=True),
    ('GET', 'api/users'): Route(lambda h, a, q: h._accounts(q), role='manage_users'),
    ('GET', 'api/user-sessions'): Route(lambda h, a, q: h._accounts(q, kind='sessions'), role='manage_users'),
    ('GET', 'api/auth-events'): Route(lambda h, a, q: h._accounts(q, kind='events'), role='manage_users'),
    ('GET', 'api/tokens'): Route(lambda h, a, q: h._tokens(q), auth=True, role='own_tokens'),
    ('GET', 'api/integration-cli'): Route(lambda h, a, q: h._ai_cli(q), auth=True, role='connect_integrations', denial=404),
    ('POST', 'api/integration-cli'): Route(lambda h, a, q: h._ai_cli(q), auth=True, role='connect_integrations', denial=404),
    ('GET', 'api/integrations'): Route(lambda h, a, q: h._integrations(), auth=True, role='manage_integrations', denial=404),
    ('GET', 'api/state-summary'): Route(lambda h, a, q: h._state_list(q, kind="summary"), auth=True),
    ('GET', 'api/detail'): Route(lambda h, a, q: h._state_list(q, kind="detail"), auth=True),
    ('GET', 'api/transaction-choices'): Route(lambda h, a, q: h._state_list(q, kind="transaction-choices"), auth=True),
    ('GET', 'api/partner-pages'): Route(lambda h, a, q: h._state_list(q, kind="partner-pages"), auth=True),
    ('GET', 'api/browse'): Route(lambda h, a, q: h._state_list(q, kind='browse'), auth=True),
    ('GET', 'api/report-pages'): Route(lambda h, a, q: h._state_list(q, kind='report-pages'), auth=True),
    ('GET', 'api/dashboard-list'): Route(lambda h, a, q: h._state_list(q, kind='dashboard-list'), auth=True),
    ('GET', 'api/search'): Route(lambda h, a, q: h._state_list(q, kind='search'), auth=True),
    ('GET', 'api/tasks'): Route(lambda h, a, q: h._state_list(q, kind='tasks'), auth=True),
    ('GET', 'api/state-list'): Route(lambda h, a, q: h._state_list(q), auth=True),
    ('GET', 'api/state'): Route(lambda h, a, q: h._api_read('state', lists.summary, q), auth=True),
    ('GET', 'api/product-stats'): Route(Handler._product_stats, auth=True),
    ('GET', 'api/products'): Route(Handler._products, auth=True),
    ('GET', 'api/invoicing'): Route(lambda h, a, q: h._api_read('invoicing', invoicing_api.workspace, q), auth=True),
    ('GET', 'api/upload-links'): Route(lambda h, a, q: h._api_read('upload-links', upload_links.list_links, q), auth=True),
    ('GET', 'api/history-review'): Route(lambda h, a, q: h._history_review(q), auth=True, role='manage_firms', denial=404, firm=True),
    ('GET', 'api/payment-files/[0-9]+'): Route(Handler._named_file_get, auth=True),
    ('POST', 'api/payment-files'): Route(Handler._named_file_post, auth=True),
    ('GET', 'api/.*'): Route(lambda h, a, q: h._bad(), auth=True),
    ('GET', 'packfile'): Route(lambda h, a, q: h._packfile(q), auth=True, firm=True),

    ('GET', 'history-document'): Route(lambda h, a, q: h._history_review(q, document=True), auth=True, role='manage_firms', denial=404, firm=True),
    ('GET', 'mailfile'): Route(lambda h, a, q: h._mailfile(q), auth=True, firm=True),
    ('GET', 'docfile'): Route(lambda h, a, q: h._docfile(q), auth=True, firm=True),
    ('POST', 'api/document-upload'): Route(lambda h, a, q: h._document_upload(q), auth=True, firm=True),
    ('POST', 'api/statement-upload'): Route(lambda h, a, q: h._statement_upload(), auth=True),
    ('POST', 'api/history-upload'): Route(lambda h, a, q: h._history_upload(), auth=True),
    ('POST', 'api/invoicing/.*'): Route(Handler._invoice_post, auth=True),
    ('POST', 'api/.*'): Route(Handler._engine_post, auth=True),
}
# fmt: on
# The app reads by fixed name; the access log names only these.
READ_ROUTES = frozenset(p for v, p in ROUTES if v == "GET" and re.fullmatch(r"api/[a-z-]+", p)) | {"api/backup"}


def tailscale_ip() -> str:
    out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()
    if not out:
        raise SystemExit("no Tailscale IP: refusing to listen anywhere else")
    return out[0]


def listen_address(host: str | None = None, port: int | None = None) -> tuple[str, int]:
    host = host or os.environ.get("FISCALM_HOST", "127.0.0.1")
    return (
        tailscale_ip() if host == "tailscale" else host,
        port if port is not None else int(os.environ.get("FISCALM_PORT", "8790")),
    )


def script_hashes(body: bytes) -> str:
    """Hash the exact HTML bytes being served, including after an in-place rebuild."""
    return " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m).digest()).decode() + "'"
        for m in re.findall(rb"<script\b[^>]*>(.*?)</script>", body, re.DOTALL)
    )


def serve(host: str | None = None, port: int | None = None) -> None:
    Ledger().close()  # Refuse incompatible schemas before binding the socket.
    receipt_upload.sweep_temps()
    address = listen_address(host, port)
    server = Server(address, Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
