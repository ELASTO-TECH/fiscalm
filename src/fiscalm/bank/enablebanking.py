"""Enable Banking: the open banking provider an install reads its own bank accounts through (read only, PSD2 account
information; never a payment). Each install has its own Enable Banking application; fiscalm holds its private key in the
secrets directory and signs every request with it. See docs/self-hosting.md.

Requests are signed JWTs (RS256, `kid` = the application's id). Signing uses the system's `openssl`, so no new Python
dependency is needed; the key never leaves the secrets directory and is never logged. Every network call goes through
`Client.http`, which tests replace: no test reaches the network.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import secrets as pysecrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from fiscalm import secrets

API = "https://api.enablebanking.com"
PROVIDER = "enablebanking"
KEY = "enablebanking-private.pem"  # in the secrets directory
CERTIFICATE = "enablebanking-certificate.pem"  # public: pasted into the Enable Banking application
TIMEOUT = 30


class BankError(RuntimeError):
    """A refusal or failure to show people: `message` is Romanian and holds no secret, token or path."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message, self.code = message, code


def generate_key() -> str:
    """Make the application's key pair in the secrets directory, once. Returns the public certificate (PEM) to paste in
    Enable Banking. Never replaces a key: the application registered with it would stop working. Two runs at once are
    serialised, and each publishes with a link that fails if the file exists, so one of them is refused."""
    d = secrets.secrets_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(d / ".enablebanking.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (d / KEY).exists() or (d / CERTIFICATE).exists():
            raise BankError(
                "Cheia aplicației există deja. Se folosește certificatul ei, sau cheia se șterge întâi, cu grijă."
            )
        tag = pysecrets.token_hex(8)
        key_tmp, cert_tmp = d / f".{KEY}.{tag}", d / f".{CERTIFICATE}.{tag}"
        old = os.umask(0o077)
        try:
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:4096",
                    "-nodes",
                    "-keyout",
                    str(key_tmp),
                    "-out",
                    str(cert_tmp),
                    "-days",
                    "3650",
                    "-subj",
                    "/CN=fiscalm",
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            os.chmod(key_tmp, 0o600)
            os.chmod(cert_tmp, 0o600)
            os.link(cert_tmp, d / CERTIFICATE)  # FileExistsError: published meanwhile, nothing replaced
            try:
                os.link(key_tmp, d / KEY)
            except BaseException:
                (d / CERTIFICATE).unlink(missing_ok=True)
                raise
        except FileExistsError:
            raise BankError("Cheia aplicației există deja. Se folosește certificatul ei.") from None
        except (OSError, subprocess.SubprocessError):
            raise BankError("Cheia nu a putut fi generată. Verifică dacă openssl este instalat.") from None
        finally:
            os.umask(old)
            key_tmp.unlink(missing_ok=True)
            cert_tmp.unlink(missing_ok=True)
    return certificate()


def certificate() -> str:
    return secrets.read_secret(CERTIFICATE) + "\n"


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def signed_jwt(application_id: str, now: int | None = None) -> str:
    """A one-hour token for the API, signed with the install's private key."""
    now = int(time.time()) if now is None else now
    head = _b64(json.dumps({"typ": "JWT", "alg": "RS256", "kid": application_id}).encode())
    body = _b64(
        json.dumps({"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": now + 3600}).encode()
    )
    key = secrets.secrets_dir() / KEY
    secrets.read_secret(KEY)  # the same checks as every secret: a regular 0600 file of this user, no symlink
    try:
        sig = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key)],
            input=head + b"." + body,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise BankError("Cererea către Enable Banking nu a putut fi semnată. Verifică cheia aplicației.") from None
    return (head + b"." + body + b"." + _b64(sig)).decode()


Http = Callable[[str, str, dict | None, dict], tuple[int, dict]]


MAX_RESPONSE = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer token elsewhere: never followed."""

    def redirect_request(self, *a, **kw):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _body(stream) -> dict:
    raw = stream.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("response too large")
    data = json.loads(raw or b"{}")
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _http(method: str, url: str, body: dict | None, headers: dict) -> tuple[int, dict]:
    if not url.startswith(API + "/"):
        raise BankError("Adresă Enable Banking neașteptată.")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:  # noqa: S310 - a fixed https API
            return r.status, _body(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, _body(e)
        except (ValueError, RecursionError):
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RecursionError):
        raise BankError("Enable Banking nu răspunde acum. Încearcă din nou mai târziu.", "network") from None


def trusted_page(url: str) -> bool:
    """The page a person is sent to, to confirm access: Enable Banking's own (it then hands over to the bank). https,
    its domain or a subdomain, no user name or password, no other port; anything else is refused, so a tampered answer
    never sends someone to another site."""
    # Browsers read a backslash as a slash and drop tabs and newlines, so a URL holding one can mean another host to
    # them than to this parser: refused, as is anything but plain printable ASCII.
    if not re.fullmatch(r"[!-~]+", url) or "\\" in url:
        return False
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return (
        u.scheme == "https"
        and not u.username
        and not u.password
        and port in (None, 443)
        and u.netloc.lower() == host
        and re.fullmatch(r"(?:[a-z0-9-]+\.)*enablebanking\.com", host) is not None
    )


# What a refusal from Enable Banking means for people (error codes from its API reference).
_REFUSALS = {
    "EXPIRED_SESSION": "Accesul la bancă a expirat. Conectează din nou banca.",
    "REVOKED_SESSION": "Accesul la bancă a fost retras din bancă. Conectează din nou banca.",
    "CLOSED_SESSION": "Accesul la bancă a fost închis. Conectează din nou banca.",
    "ASPSP_RATE_LIMIT_EXCEEDED": "Banca permite doar câteva citiri pe zi. fiscalm citește din nou datele peste câteva ore.",
    "REDIRECT_URI_NOT_ALLOWED": "Adresa de întoarcere nu este trecută în aplicația din Enable Banking.",
}


@dataclass
class Client:
    application_id: str
    http: Http = field(default=_http)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": "Bearer " + signed_jwt(self.application_id), "Content-Type": "application/json"}
        status, data = self.http(method, API + path, body, headers)
        if 200 <= status < 300:
            return data
        code = str(data.get("error") or data.get("code") or status)
        code = code if code in _REFUSALS else "refused"  # only codes fiscalm knows reach a log or a page
        raise BankError(
            _REFUSALS.get(code, "Enable Banking a refuzat cererea. Verifică aplicația, apoi încearcă din nou."),
            code,
        )

    def application(self) -> dict:
        return self._call("GET", "/application")

    def start_auth(
        self, bank_name: str, country: str, redirect_url: str, state: str, valid_until: str, psu_type: str = "business"
    ) -> str:
        """The bank's login page to send the person to. Account information only."""
        data = self._call(
            "POST",
            "/auth",
            {
                "access": {"valid_until": valid_until},
                "aspsp": {"name": bank_name, "country": country},
                "state": state,
                "redirect_url": redirect_url,
                "psu_type": psu_type,
            },
        )
        url = data.get("url")
        if not isinstance(url, str) or not trusted_page(url):
            raise BankError("Enable Banking nu a dat adresa băncii. Încearcă din nou.")
        return url

    def create_session(self, code: str) -> dict:
        """The session the bank's return code opens: its id, the accounts shared and until when."""
        return self._call("POST", "/sessions", {"code": code})

    def account_details(self, uid: str) -> dict:
        return self._call("GET", f"/accounts/{urllib.parse.quote(uid, safe='')}/details")

    def transactions(self, uid: str, date_from: str, date_to: str) -> list[dict]:
        """Every transaction of an account in [date_from, date_to], all pages (the continuation key needs the same
        dates on every page)."""
        out: list[dict] = []
        key = None
        for _ in range(200):  # a bound, never an endless loop on a misbehaving answer
            q = {"date_from": date_from, "date_to": date_to} | ({"continuation_key": key} if key else {})
            data = self._call(
                "GET", f"/accounts/{urllib.parse.quote(uid, safe='')}/transactions?" + urllib.parse.urlencode(q)
            )
            page = data.get("transactions")
            if not isinstance(page, list):  # an answer without its list proves nothing was booked: never read as none
                raise BankError("Banca a trimis un răspuns fără tranzacții. Încearcă din nou.", "malformed")
            out += page
            key = data.get("continuation_key")
            if not key:
                return out
        raise BankError("Banca a trimis prea multe pagini de tranzacții. Încearcă din nou.")
