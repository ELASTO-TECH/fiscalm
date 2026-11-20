"""ANAF OAuth2 and the e-Factura API (list/download plus guarded invoice upload/status).

Connecting (Setări, Integrări, the ANAF integration, "Conectează ANAF"): an administrator starts it in fiscalm, the
browser goes to ANAF's certificate login, and ANAF sends it back to this install's own address
(`<FISCALM_PUBLIC_URL>/anaf/callback`), where fiscalm exchanges the code and saves the tokens with the integration's
secrets. What fiscalm relies on from ANAF, with sources, is in docs/efactura-anaf.md ("Conectarea din fiscalm"):

  - authorization code grant at logincert.anaf.ro with the user's qualified certificate, `token_content_type=jwt`
    on both requests, client authentication as HTTP Basic; the code must be exchanged within 60 seconds;
  - access token 90 days, refresh token 365 days; a refresh returns a new pair, both are saved;
  - no OAuth `state`: ANAF's procedure leaves it empty, and sending one broke the certificate login on the first real
    run (2026-10-26). The attempt is bound to the administrator's browser by fiscalm instead: a single-use start link
    on the public address sets an HttpOnly cookie, and the callback is refused without it (see `start`).
  - GET {base}/listaMesajePaginatieFactura?startTime&endTime&cif&pagina&filtru=P|T (window <= 60 days, ms;
    P = received "FACTURA PRIMITA", T = sent "FACTURA TRIMISA": the same E-Factura scope and token),
    GET {base}/descarcare?id=  -> ZIP with the invoice XML and the Ministry's signature XML.
    Errors come back as HTTP 200 with {"eroare": ...}.
  - limiteApeluriAPI.txt: 1000 calls/minute overall, 100 000 paginated list calls/day/CUI,
    10 downloads/day per message. fiscalm makes a handful per tick and downloads each message once.

Tokens are never printed, logged or put in the app's state.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets as pysecrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timedelta

from fiscalm import PRODUCT, companies, integrations, ledger, secrets
from fiscalm.api import public_url
from fiscalm.ledger import Ledger

AUTHORIZE_URL = "https://logincert.anaf.ro/anaf-oauth2/v1/authorize"
TOKEN_URL = "https://logincert.anaf.ro/anaf-oauth2/v1/token"
API = {"prod": "https://api.anaf.ro/prod/FCTEL/rest", "test": "https://api.anaf.ro/test/FCTEL/rest"}
ACCESS_DAYS, REFRESH_DAYS = 90, 365
REFRESH_BEFORE = timedelta(days=14)  # renew the access token this long before it expires
EXPIRING_DAYS = 30  # the sheet and the reminders ask for a reconnect this long before the authorization ends
CALLBACK_PATH = "/anaf/callback"
START_PATH = "/anaf/start/"
COOKIE = "fiscalm_anaf"
STATE_TTL = 10 * 60  # from "Conectează ANAF" to ANAF's return: the certificate login and its PIN
UA = "fiscalm/1.0"

_SECRET = re.compile(r"^[A-Za-z0-9_-]{40,64}$")  # start links and browser cookies: token_urlsafe(32)
_CODE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,4096}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{1,8192}$")  # RFC 6750 b64token: safe in an Authorization header


class AnafError(RuntimeError):
    pass


class CompanyUnauthorized(AnafError):
    """The token cannot read this company in SPV."""


class AuthMissing(AnafError):
    """No usable tokens: an administrator connects ANAF in Setări, Integrări, with the qualified certificate."""


class ConnectError(AnafError):
    """Connecting refused: `message` is what the person reads (Romanian), `code` what the log says. `integration`:
    the integration whose sheet shows this refusal (`finish` kept it on the attempt), or "" when no attempt is known."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message, self.code, self.integration = message, code, ""


NO_PUBLIC_URL = "Adresa publică a instalării nu este setată. Administratorul serverului o setează."
BAD_PUBLIC_URL = (
    "Adresa publică a instalării nu este bună: trebuie să fie https, fără cale. "
    "Administratorul serverului o corectează."
)
NOT_SET_UP = "Integrarea ANAF este oprită sau nu există. Se configurează în Setări, la Integrări."
NO_CLIENT = "Lipsesc Client ID sau Client secret din integrarea ANAF."
NOT_ADMIN = "Conectarea ANAF o poate porni doar un administrator."
STATE_BAD = (
    f"Legătura cu ANAF a expirat sau a fost deja folosită. Conectarea se pornește din nou din {PRODUCT}, "
    "în Setări, la Integrări."
)
DENIED = (
    "ANAF a refuzat autorizarea accesului. Nu s-a schimbat nimic. Dacă fereastra de alegere a certificatului nu mai "
    "apare, închide complet browserul, deschide-l din nou și conectează ANAF."
)
OAUTH_ERRORS = {
    "access_denied": DENIED,
    "invalid_client": "ANAF nu recunoaște aplicația. Verifică Client ID în detaliile aplicației din portalul ANAF.",
    "unauthorized_client": "ANAF nu permite autorizarea acestei aplicații. Verifică serviciul E-Factura în portalul ANAF.",
    "invalid_request": "ANAF a refuzat cererea de conectare. Verifică adresa Callback URL în aplicația din portalul ANAF.",
    "invalid_scope": "ANAF a refuzat permisiunile cerute de aplicație. Administratorul instalării verifică integrarea.",
    "unsupported_response_type": "ANAF nu acceptă cererea de autorizare. Administratorul instalării verifică integrarea.",
    "server_error": "ANAF a întâmpinat o eroare. Conectarea se poate porni din nou mai târziu.",
    "temporarily_unavailable": "Autorizarea ANAF este temporar indisponibilă. Încearcă din nou mai târziu.",
}
OAUTH_REFUSED = "ANAF a refuzat conectarea, fără o cauză recunoscută. Conexiunea nu a fost salvată."
CHANGED = f"Integrarea ANAF s-a schimbat în timpul conectării. Conectarea se pornește din nou din {PRODUCT}."
NO_COMPANY = "Conectarea ANAF cere cel puțin o firmă adăugată în Setări, la Firme."
NO_RIGHTS = (
    "Certificatul folosit nu are drept în SPV pentru nicio firmă a instalării. Conexiunea nu a fost salvată; "
    "se reconectează cu certificatul unei persoane cu drept în SPV pentru firme."
)
VERIFY = (
    "Accesul la e-Factura nu a putut fi verificat la ANAF. Conexiunea nu a fost salvată; conectarea se poate "
    "porni din nou."
)
EXCHANGE = (
    f"ANAF nu a confirmat autorizarea. Conectarea se poate porni din nou din {PRODUCT}. Codul de la ANAF "
    "expiră într-un minut, așa că pașii se fac fără pauză."
)


@contextmanager
def _ledger(L: Ledger | None) -> Generator[Ledger]:
    if L is not None:
        yield L
        return
    L = Ledger()
    try:
        yield L
    finally:
        L.close()


def instance(L: Ledger, company: str | None = None) -> integrations.Instance | None:
    """The ANAF integration in use: enabled, configured in Setări, Integrări. None when there is none."""
    return integrations.anaf_for_firm(L, company) if company is not None else integrations.default(L, "anaf")


def configured(L: Ledger) -> bool:
    """An enabled ANAF integration exists (connected or not); never reads credentials."""
    return instance(L) is not None


def require_configured(L: Ledger | None = None, *, company: str | None = None) -> integrations.Instance:
    """Refuse outward ANAF work before selecting an environment or reading tokens."""
    with _ledger(L) as L:
        inst = instance(L, company)
    if inst is None:
        raise AnafError("ANAF integration is disabled or not configured; enable or configure it before SPV calls.")
    return inst


def outward_allowed(environment: str | None = None) -> bool:
    """One upload/status policy: shadow permits only the effective ANAF TEST environment."""
    return ledger.is_live() or (environment or env()) == "test"


def env() -> str:
    """Upload/status environment follows LIVE; the override is only for development/tests."""
    return _environment("prod" if ledger.is_live() else "test")


def inbox_env() -> str:
    """Inbound fiscal evidence stays in production, including on a shadow install."""
    return _environment("prod")


def _environment(default: str) -> str:
    selected = os.environ.get("FISCALM_ANAF_ENV") or default
    if selected not in API:
        raise AnafError(f"FISCALM_ANAF_ENV must be one of {sorted(API)}")
    return selected


def redirect_uri() -> str:
    """The address ANAF sends the browser back to, exactly as it is registered in the ANAF OAuth app (Callback URL):
    the install's public address over https, then /anaf/callback. ANAF accepts only https callbacks."""
    try:
        base = public_url.origin(https_only=True)
    except public_url.PublicURLError:
        raise ConnectError(BAD_PUBLIC_URL, f"{public_url.ENV} invalid") from None
    if not base:
        raise ConnectError(NO_PUBLIC_URL, f"{public_url.ENV} missing")
    return base + CALLBACK_PATH


# ------------------------------------------------------------------ tokens


def jwt_exp(token: str) -> datetime | None:
    """Expiry from a JWT's payload (no signature check: only used to plan refreshes)."""
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return datetime.fromtimestamp(int(payload["exp"]))
    except (IndexError, ValueError, KeyError, TypeError, OverflowError, OSError):
        return None


def _post(url: str, body: bytes, headers: dict) -> dict:
    """POST a form to ANAF's token endpoint, answer as JSON (the one place tests replace with a fake ANAF)."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _token_request(inst: integrations.Instance, form: dict) -> dict:
    """One call to the token endpoint with the integration's client (HTTP Basic). Errors name the HTTP status or
    ANAF's OAuth error code, never a token, the code or the answer's body."""
    cid = inst.config.get("client_id") or ""
    try:
        csec = inst.secret("client_secret")
    except secrets.SecretError:
        raise AnafError("ANAF client secret is missing; save it in Setări, Integrări.") from None
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    try:
        data = _post(
            TOKEN_URL,
            urllib.parse.urlencode(form).encode(),
            {
                "Authorization": f"Basic {basic}",
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    except urllib.error.HTTPError as e:
        raise AnafError(f"ANAF token endpoint answered HTTP {e.code}") from None
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise AnafError(f"ANAF token request failed: {type(e).__name__}") from None
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
        err = data.get("error") if isinstance(data, dict) else None
        err = err if isinstance(err, str) and re.fullmatch(r"[a-z_]{1,40}", err) else "no error field"
        raise AnafError(f"ANAF token response without access_token ({err})")
    if not _TOKEN.fullmatch(data["access_token"]) or (
        "refresh_token" in data
        and not (isinstance(data["refresh_token"], str) and _TOKEN.fullmatch(data["refresh_token"]))
    ):
        raise AnafError("ANAF token response with a malformed token")  # never saved, never sent in a header
    return data


def _tokens(inst: integrations.Instance) -> dict | None:
    """The saved tokens, or None when the integration was never connected. Raises SecretError when unreadable."""
    if integrations.OAUTH not in inst.versions:
        return None
    try:
        t = json.loads(inst.secret(integrations.OAUTH))
        if not isinstance(t, dict) or not all(
            isinstance(t.get(k), str) and t[k]
            for k in ("access_token", "refresh_token", "access_expires_at", "refresh_expires_at")
        ):
            raise ValueError
        if not _TOKEN.fullmatch(t["access_token"]) or not _TOKEN.fullmatch(t["refresh_token"]):
            raise ValueError
        datetime.fromisoformat(t["access_expires_at"]), datetime.fromisoformat(t["refresh_expires_at"])
    except ValueError:
        raise secrets.SecretError("ANAF tokens are malformed; connect ANAF again in Setări, Integrări.") from None
    return t


def _merged(data: dict, previous: dict | None, now: datetime) -> dict:
    """A token answer as saved. The refresh token's expiry is the JWT's when readable; otherwise the earliest plausible
    one (365 days from the first authorization), so the reminder is never late."""
    prev = previous or {}
    access, refresh_ = data["access_token"], data.get("refresh_token") or prev.get("refresh_token", "")
    if not refresh_:
        raise AnafError("ANAF token response without refresh_token")
    a_exp = jwt_exp(access) or now + timedelta(days=ACCESS_DAYS)
    r_exp = jwt_exp(refresh_) or (
        datetime.fromisoformat(prev["refresh_expires_at"])
        if prev.get("refresh_expires_at")
        else now + timedelta(days=REFRESH_DAYS)
    )
    return {
        "access_token": access,
        "refresh_token": refresh_,
        "access_expires_at": a_exp.isoformat(timespec="seconds"),
        "refresh_expires_at": r_exp.isoformat(timespec="seconds"),
        "authorized_at": prev.get("authorized_at") or now.isoformat(timespec="seconds"),
        "refreshed_at": now.isoformat(timespec="seconds"),
    }


def token_state(L: Ledger | None = None, now: datetime | None = None, *, company: str | None = None) -> dict:
    """What the status page and nudges need: dates only, no token values.
    `status`: connected (with `authorized` true, even past expiry: see `refresh_days_left`), not_connected
    (configured, never connected), error (unreadable tokens), not_set_up (no enabled integration)."""
    now = now or datetime.now()
    with _ledger(L) as L:
        inst = instance(L, company)
        if inst is None:
            return {"authorized": False, "status": "not_set_up", "error": "ANAF is not configured (Setări, Integrări)."}
        try:
            inst, t = _fresh_tokens(L, inst)
        except (secrets.SecretError, ValueError, TypeError):
            t = False
    try:
        if t is False:
            raise secrets.SecretError("unreadable")
        if t is None:
            return {
                "authorized": False,
                "status": "not_connected",
                "error": "ANAF is not connected yet: Setări, Integrări, Conectează ANAF.",
            }
        r_exp = datetime.fromisoformat(t["refresh_expires_at"])
        return {
            "authorized": True,
            "status": "connected",
            "access_expires_at": t["access_expires_at"],
            "refresh_expires_at": t["refresh_expires_at"],
            "refresh_days_left": (r_exp - now).days,
            "authorized_at": t.get("authorized_at"),
        }
    except (secrets.SecretError, ValueError, TypeError):
        return {
            "authorized": False,
            "status": "error",
            "error": "ANAF credentials could not be read; connect ANAF again in Setări, Integrări.",
        }


def redirect() -> dict:
    """The Callback URL to register in the ANAF OAuth app (`redirect_uri`), or None with `redirect_error` saying what
    the server's administrator sets first. Shown before the integration exists: ANAF asks for it to make the app."""
    try:
        return {"redirect_uri": redirect_uri(), "redirect_error": None}
    except ConnectError as e:
        return {"redirect_uri": None, "redirect_error": e.message}


def connection(inst: integrations.Instance | None, now: datetime | None = None, L: Ledger | None = None) -> dict:
    """The ANAF integration's connection as its sheet shows it. `state`: not_connected, connected, expiring (within
    EXPIRING_DAYS), expired, error (tokens unreadable). `expires`: the day the authorization ends. `redirect_uri`: the
    Callback URL to register in the ANAF OAuth app, or None with `redirect_error` saying what to set. `refusal`: why
    the newest attempt to connect did not finish, while it is recent; read only with a ledger. No token."""
    now = now or datetime.now()
    out = {"state": "not_connected", "expires": None, "connected_at": None, "refusal": None} | redirect()
    if inst is None:
        return out
    if L is not None:
        newest = L.db.execute(
            "SELECT refusal FROM anaf_auth_states WHERE integration_id=? AND created_at>=? "
            "ORDER BY created_at DESC, link_hash DESC LIMIT 1",
            (inst.id, now.timestamp() - STATE_TTL),
        ).fetchone()
        out["refusal"] = newest["refusal"] if newest else None
    try:
        if L is None:
            t = _tokens(inst)
        else:
            try:
                t = _tokens(inst)
            except secrets.SecretError:  # replaced by a refresh since the row was read: read the row once more
                again = integrations.instance(L, inst.id)
                if again is None or again.versions.get(integrations.OAUTH) == inst.versions.get(integrations.OAUTH):
                    raise
                t = _tokens(again)
        if t is None:
            return out
        r_exp = datetime.fromisoformat(t["refresh_expires_at"])
    except (secrets.SecretError, ValueError, TypeError):
        return out | {"state": "error"}
    state = "expired" if r_exp <= now else "expiring" if r_exp - now <= timedelta(days=EXPIRING_DAYS) else "connected"
    return out | {
        "state": state,
        "expires": r_exp.date().isoformat(),
        "connected_at": str(t.get("authorized_at") or "")[:10] or None,
    }


def _fresh_tokens(L: Ledger, inst: integrations.Instance) -> tuple[integrations.Instance, dict | None]:
    """The tokens of `inst`; when its file was replaced between reading the row and the file (a refresh elsewhere),
    the row is read once more instead of calling the connection unreadable."""
    try:
        return inst, _tokens(inst)
    except secrets.SecretError:
        again = integrations.instance(L, inst.id)
        if (
            again is None
            or again.id != inst.id
            or again.versions.get(integrations.OAUTH) == inst.versions.get(integrations.OAUTH)
        ):
            raise
        return again, _tokens(again)


def _connected(L: Ledger, company: str | None = None) -> tuple[integrations.Instance, dict]:
    inst, t = _fresh_tokens(L, require_configured(L, company=company))
    if t is None:
        raise AuthMissing("ANAF is not connected yet: Setări, Integrări, Conectează ANAF.")
    return inst, t


def _refresh(L: Ledger, inst: integrations.Instance, t: dict, now: datetime) -> dict:
    """Refresh and save, unless another process saved newer tokens first (theirs are used) or the integration was
    removed, disabled or pointed at another app meanwhile (nothing is saved)."""
    data = _token_request(
        inst, {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "token_content_type": "jwt"}
    )
    new = _merged(data, t, now)
    if integrations.put_oauth(
        L,
        inst.id,
        json.dumps(new),
        client_id=inst.config.get("client_id") or "",
        expect=inst.versions.get(integrations.OAUTH),
    ):
        return new
    current = integrations.instance(L, inst.id)
    if (
        current is None
        or not current.enabled
        or current.id != inst.id
        or current.config.get("client_id") != inst.config.get("client_id")
    ):
        raise AuthMissing("ANAF setup was removed, disabled or changed; connect ANAF again in Setări, Integrări.")
    _, theirs = _fresh_tokens(L, current)
    if theirs is None:
        raise AuthMissing("ANAF setup was removed, disabled or changed; connect ANAF again in Setări, Integrări.")
    return theirs


def refresh(L: Ledger | None = None, now: datetime | None = None) -> dict:
    """Refresh the access token now (the CLI's `efactura refresh`)."""
    now = now or datetime.now()
    with _ledger(L) as L:
        inst, t = _connected(L)
        return _refresh(L, inst, t, now)


def access_token(
    now: datetime | None = None, *, L: Ledger | None = None, company: str | None = None, expected_id: str | None = None
) -> str:
    """A valid access token, refreshed automatically well before it expires."""
    now = now or datetime.now()
    with _ledger(L) as L:
        if company is None and len(integrations.enabled(L, "anaf")) > 1:
            raise AuthMissing("Choose a firm for ANAF access.")
        inst, t = _connected(L, company)
        if expected_id is not None and inst.id != expected_id:
            raise AuthMissing("ANAF firm coverage changed; read again on the next check.")
        if datetime.fromisoformat(t["refresh_expires_at"]) <= now:
            raise AuthMissing("ANAF authorization expired: connect ANAF again in Setări, Integrări.")
        a_exp = datetime.fromisoformat(t["access_expires_at"])
        if a_exp - now < REFRESH_BEFORE:
            # Saving a refresh takes its own write transaction (integrations.put_oauth). Inside a caller's transaction
            # (an invoice upload's claim) the token still valid for up to REFRESH_BEFORE is used; the tick refreshes it.
            if not L.db.in_transaction:
                t = _refresh(L, inst, t, now)
            elif a_exp <= now:
                raise AuthMissing("ANAF access token expired; the next check refreshes it.")
        return t["access_token"]


# ------------------------------------------------------------------ connecting from fiscalm


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def start(L: Ledger, iid: str, user: str) -> str:
    """Start connecting the ANAF integration `iid` for administrator `user`; returns the start link to open.

    The link is on the install's public address and works once, within STATE_TTL. Opening it (`open_start`) sets a
    cookie in that browser and sends it to ANAF; only that browser can then finish (`finish`). A new start cancels
    earlier unfinished attempts for this instance. Only hashes are stored."""
    inst = integrations.instance(L, iid)
    if inst is None or inst.kind != "anaf" or not inst.enabled:
        raise ConnectError(NOT_SET_UP, "not_set_up")
    if not inst.config.get("client_id") or not inst.has("client_secret"):
        raise ConnectError(NO_CLIENT, "no_client")
    if not companies.all(L):
        raise ConnectError(NO_COMPANY, "no_company")
    redirect = redirect_uri()
    link = pysecrets.token_urlsafe(32)
    with L.tx() as db:
        db.execute(
            "DELETE FROM anaf_auth_states WHERE (integration_id=? AND stage!='used') OR created_at < ?",
            (iid, time.time() - STATE_TTL),
        )
        db.execute(
            "INSERT INTO anaf_auth_states(link_hash, integration_id, client_id, redirect_uri, created_by, "
            "created_at) VALUES(?,?,?,?,?,?)",
            (_hash(link), inst.id, inst.config["client_id"], redirect, user, time.time()),
        )
    return redirect.removesuffix(CALLBACK_PATH) + START_PATH + link


def authorize_url(client_id: str, redirect: str) -> str:
    """ANAF's certificate login for this app and callback. No `state` (see the module docstring)."""
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "token_content_type": "jwt"}
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(q)}"


def open_start(L: Ledger, link: str) -> tuple[str, str]:
    """The start link was opened: returns (ANAF's login page, the browser secret for the cookie). Once per link."""
    if not isinstance(link, str) or not _SECRET.fullmatch(link):
        raise ConnectError(STATE_BAD, "link")
    browser = pysecrets.token_urlsafe(32)
    # A public route: an unknown link is refused on a read, without taking the write lock.
    row = L.db.execute(
        "SELECT client_id, redirect_uri, created_at FROM anaf_auth_states WHERE link_hash=? AND stage='issued'",
        (_hash(link),),
    ).fetchone()
    if not row or time.time() - row["created_at"] > STATE_TTL:
        raise ConnectError(STATE_BAD, "link")
    with L.tx() as db:
        if not db.execute(
            "UPDATE anaf_auth_states SET stage='opened', browser_hash=? WHERE link_hash=? AND stage='issued'",
            (_hash(browser), _hash(link)),
        ).rowcount:
            raise ConnectError(STATE_BAD, "link")
    return authorize_url(row["client_id"], row["redirect_uri"]), browser


def _take(L: Ledger, browser: str):
    """The attempt this browser opened, once: a second use, an old one or an unknown cookie gets None."""
    if not isinstance(browser, str) or not _SECRET.fullmatch(browser):
        return None
    row = L.db.execute(
        "SELECT * FROM anaf_auth_states WHERE browser_hash=? AND stage='opened'", (_hash(browser),)
    ).fetchone()
    if not row or time.time() - row["created_at"] > STATE_TTL:
        return None
    with L.tx() as db:
        if not db.execute(
            "UPDATE anaf_auth_states SET stage='used' WHERE link_hash=? AND stage='opened'", (row["link_hash"],)
        ).rowcount:
            return None
    return row


def finish(
    L: Ledger, query: dict, browser: str, may_connect: Callable[[str], bool], now: datetime | None = None
) -> dict:
    """ANAF's return to /anaf/callback. `query` is untrusted: only `code` and `error` are read, and nothing in it names
    a URL or an action. `browser` is the cookie `open_start` set. Refuses (ConnectError) unless the attempt is this
    browser's, unused and fresh; its administrator still `may_connect`; the integration and the callback address are
    the ones it started with; and ANAF exchanges the code. Then the tokens replace the integration's old ones.
    A refusal of this browser's attempt is kept on it, for the integration's sheet (`connection`).
    Returns {"expires": "YYYY-MM-DD"}."""
    row = _take(L, browser)
    if row is None:
        raise ConnectError(STATE_BAD, "state")
    try:
        return _complete(L, row, query, may_connect, now or datetime.now())
    except ConnectError as e:
        with L.tx() as db:
            db.execute("UPDATE anaf_auth_states SET refusal=? WHERE link_hash=?", (e.message, row["link_hash"]))
        e.integration = row["integration_id"]
        raise


def _complete(L: Ledger, row, query: dict, may_connect: Callable[[str], bool], now: datetime) -> dict:
    """What `finish` checks, exchanges and saves for `row`, the attempt this browser opened."""
    if error := query.get("error"):
        # Only known protocol codes reach the UI/log; descriptions and arbitrary error values may contain secrets.
        reason = error if isinstance(error, str) and error in OAUTH_ERRORS else "unknown"
        raise ConnectError(OAUTH_ERRORS.get(reason, OAUTH_REFUSED), "oauth_" + reason)
    code = query.get("code", "")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ConnectError(STATE_BAD, "code")
    if not may_connect(row["created_by"]):
        raise ConnectError(NOT_ADMIN, "not_admin")
    inst = integrations.instance(L, row["integration_id"])
    if inst is None or not inst.enabled or inst.config.get("client_id") != row["client_id"]:
        raise ConnectError(CHANGED, "changed")
    if redirect_uri() != row["redirect_uri"]:
        raise ConnectError(CHANGED, "redirect")
    try:
        data = _token_request(
            inst,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": row["redirect_uri"],
                "token_content_type": "jwt",
            },
        )
        tok = _merged(data, None, now)  # a new authorization: a new 365-day refresh token
    except AnafError:
        raise ConnectError(EXCHANGE, "exchange") from None
    _verify(tok["access_token"], now, L, inst)

    def newest(db) -> bool:  # a later "Conectează ANAF" wins over this one, even if this one finishes last
        return (
            db.execute("SELECT 1 FROM anaf_auth_states WHERE link_hash=?", (row["link_hash"],)).fetchone() is not None
            and db.execute(
                "SELECT 1 FROM anaf_auth_states WHERE integration_id=? AND (created_at > ? OR (created_at = ? AND link_hash > ?))",
                (inst.id, row["created_at"], row["created_at"], row["link_hash"]),
            ).fetchone()
            is None
        )

    if not integrations.put_oauth(L, inst.id, json.dumps(tok), client_id=row["client_id"], replace=True, guard=newest):
        raise ConnectError(CHANGED, "changed")
    return {"expires": tok["refresh_expires_at"][:10]}


def _verify(token: str, now: datetime, L: Ledger, inst: integrations.Instance) -> None:
    """Before tokens replace the integration's: they must read at least one of the install's firms in SPV (a read-only
    listing of the last day). ANAF's return carries no `state`, so this is what keeps a certificate without rights
    for these firms (someone else's code, opened in the administrator's browser) from replacing the connection."""
    denied = 0
    for co in companies.all(L).values():
        if not inst.covers(co.key):
            continue
        try:
            list_received(co.cui, now - timedelta(days=1), now - timedelta(minutes=1), token)
            return
        except CompanyUnauthorized:
            denied += 1
        except AnafError:
            raise ConnectError(VERIFY, "verify") from None
    raise ConnectError(NO_RIGHTS if denied else NO_COMPANY, "no_rights" if denied else "no_company")


# ------------------------------------------------------------------ API (read-only)


def _get(path: str, params: dict, token: str, *, environment: str | None = None) -> tuple[int, bytes, str]:
    require_configured()
    if environment is not None and not outward_allowed(environment):
        raise AnafError("Invoice SPV calls are disabled in shadow mode.")
    url = f"{API[environment or inbox_env()]}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read(50 * 1024 * 1024), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise CompanyUnauthorized("ANAF refused access for this company (HTTP 403)") from None
        if e.code == 401:
            raise AuthMissing(f"ANAF refused the token (HTTP {e.code})") from None
        if e.code == 429:
            raise AnafError("ANAF rate limit (HTTP 429); next tick retries") from None
        raise AnafError(f"ANAF answered HTTP {e.code} on {path}") from None
    except (OSError, ValueError, http.client.HTTPException) as e:  # never the message: it may hold the header
        raise AnafError(f"ANAF {path} failed: {type(e).__name__}") from None


def list_received(cif: str, start: datetime, end: datetime, token: str) -> list[dict]:
    """Every 'FACTURA PRIMITA' message for this CIF in [start, end] (at most 60 days back), all pages."""
    return _list(cif, start, end, token, "P", "FACTURA PRIMITA")


def list_sent(cif: str, start: datetime, end: datetime, token: str, *, environment: str | None = None) -> list[dict]:
    """Every 'FACTURA TRIMISA' message (invoices the company issued through SPV), same window and paging."""
    return _list(cif, start, end, token, "T", "FACTURA TRIMISA", environment)


def _list(
    cif: str, start: datetime, end: datetime, token: str, filtru: str, tip: str, environment: str | None = None
) -> list[dict]:
    out, page = [], 1
    # The span from the naive difference, not start.timestamp(): across a DST change the local clock
    # would stretch it by an hour, past ANAF's 60-day limit.
    end_ms = int(end.timestamp() * 1000)
    start_ms = end_ms - int((end - start).total_seconds() * 1000)
    while True:
        params = {"startTime": start_ms, "endTime": end_ms, "cif": cif, "pagina": page, "filtru": filtru}
        _, body, _ = _get(
            "listaMesajePaginatieFactura", params, token, **({"environment": environment} if environment else {})
        )
        try:
            data = json.loads(body)
        except ValueError:
            raise AnafError("ANAF list answer is not JSON") from None
        if data.get("eroare"):
            if str(data["eroare"]).startswith("Nu exista mesaje"):
                return out
            denial = str(data["eroare"]).casefold().replace("ț", "t").replace("ă", "a")
            if denial.startswith("nu aveti drept de acces"):
                raise CompanyUnauthorized("ANAF refused access for this company")
            raise AnafError(f"ANAF list: {str(data['eroare'])[:160]}")
        out += [m for m in data.get("mesaje") or [] if m.get("tip") == tip]
        if page >= int(data.get("numar_total_pagini") or 1):
            return out
        page += 1


def download(message_id: str, token: str, *, environment: str | None = None) -> bytes:
    """The ZIP for one message. ANAF reports errors as JSON with HTTP 200, so the bytes are checked."""
    if not str(message_id).isdigit():
        raise AnafError("message id must be numeric")
    _, body, _ = _get("descarcare", {"id": message_id}, token, **({"environment": environment} if environment else {}))
    if not body.startswith(b"PK\x03\x04"):
        try:
            err = json.loads(body).get("eroare", "")
        except ValueError:
            err = "not a ZIP"
        raise AnafError(f"ANAF download {message_id}: {str(err)[:160]}")
    return body


# ------------------------------------------------------------------ issuing (live or ANAF TEST)


def prepare_invoice_request(L: Ledger | None = None, *, environment: str | None = None, company: str | None = None):
    """Acquire credentials and bind the host before the durable upload claim."""
    require_configured(L, company=company)
    environment = environment or env()
    if not outward_allowed(environment):
        raise AnafError("Invoice SPV calls are disabled in shadow mode.")
    base = API[environment]
    if base not in {"https://api.anaf.ro/prod/FCTEL/rest", "https://api.anaf.ro/test/FCTEL/rest"}:
        raise AnafError("Invalid ANAF host.")
    token = access_token(L=L, company=company)
    return lambda method, url, body: invoice_request(method, url, body, token=token, environment=environment)


def invoice_request(
    method: str, url: str, body: bytes | None, *, token: str | None = None, environment: str | None = None
) -> bytes:
    """OAuth transport for invoice submission. Never expose tokens or response bodies in errors.

    https://mfinante.gov.ro/static/10/eFactura/prezentare%20apeluri%20API%20E-factura.pdf
    Hosts follow env(), including TEST. Shadow checks precede even OAuth token refresh.
    """
    require_configured()
    environment = environment or env()
    if not outward_allowed(environment):
        raise AnafError("Invoice SPV calls are disabled in shadow mode.")
    # Never accept a URL supplied by invoice data or an ANAF response.
    if not url.startswith(API[environment] + "/"):
        raise AnafError("Invoice request host does not match the configured ANAF environment.")
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + (token if token is not None else access_token()),
            "User-Agent": UA,
            "Content-Type": "application/xml",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read(50 * 1024 * 1024 + 1)
            if len(raw) > 50 * 1024 * 1024:
                raise AnafError("ANAF invoice response exceeded the size limit.")
            return raw
    except (OSError, ValueError, http.client.HTTPException):
        raise AnafError("ANAF invoice request failed; its outcome may be unknown.") from None


def record_firm_read_status(L: Ledger, prefix: str, configured, out: dict, denied: list[str]) -> None:
    """Keep failures and coverage warnings on the firm they describe; successful reads clear old errors."""
    for key in configured:
        for field, values in (("last_error", out["errors"]), ("warnings", out["warnings"])):
            L.set_meta(f"{prefix}_{field}:{key}", "\n".join(v for v in values if v.startswith(key + ":")))
        lost = key in denied and (L.get_meta(f"{prefix}_listed:{key}") or len(denied) == len(configured))
        L.set_meta(f"{prefix}_revoked:{key}", "1" if lost else "")
