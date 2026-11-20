"""Provider discovery and bounded, read-only IMAP setup checks. No credentials are retained."""

from __future__ import annotations

import imaplib
import ipaddress
import re
import socket
import ssl
import time

import dns.exception
import dns.resolver

from fiscalm.mail import imap

TIMEOUT = 8
# label, host, app-password URL; Microsoft and Proton deliberately have no password path.
PROVIDERS = {
    "gmail": ("Gmail", "imap.gmail.com", "https://myaccount.google.com/apppasswords"),
    "yahoo": ("Yahoo", "imap.mail.yahoo.com", "https://login.yahoo.com/account/security"),
    "icloud": ("iCloud", "imap.mail.me.com", "https://account.apple.com/account/manage"),
    "outlook": ("Microsoft", "outlook.office365.com", ""),
    "zoho": ("Zoho", "imap.zoho.com", "https://accounts.zoho.com/home#security/device_logins"),
    "gmx": ("GMX", "imap.gmx.com", ""),
    "aol": ("AOL", "imap.aol.com", "https://login.aol.com/account/security"),
    "fastmail": ("Fastmail", "imap.fastmail.com", "https://app.fastmail.com/settings/security/devicekeys"),
    "proton": ("Proton Mail", "", ""),
}
DOMAINS = {
    "gmail": "gmail.com googlemail.com",
    "yahoo": "yahoo.com yahoo.ro ymail.com",
    "icloud": "icloud.com me.com mac.com",
    "outlook": "outlook.com hotmail.com live.com outlook.ro hotmail.co.uk live.co.uk",
    "zoho": "zoho.com zohomail.com",
    "gmx": "gmx.com gmx.net gmx.de",
    "aol": "aol.com",
    "fastmail": "fastmail.com fastmail.fm",
    "proton": "proton.me protonmail.com protonmail.ch pm.me",
}


def detect(address: str) -> dict:
    """Fixed provider metadata; MX chooses only entries in our table, never an arbitrary host."""
    if not re.fullmatch(r"[^\s@\x00-\x1f]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", address):
        return {"code": "invalid_settings"}
    domain = address.rsplit("@", 1)[1].lower()
    key = next((k for k, domains in DOMAINS.items() if domain in domains.split()), "")
    mx = []
    if not key:
        try:
            mx = [str(r.exchange).lower().rstrip(".") for r in dns.resolver.resolve(domain, "MX", lifetime=2)]
        except (dns.exception.DNSException, OSError):
            mx = []
        for suffix, provider in (
            ("google.com", "gmail"),
            ("googlemail.com", "gmail"),
            ("outlook.com", "outlook"),
            ("zoho.com", "zoho"),
            ("zoho.eu", "zoho"),
        ):
            if any(host == suffix or host.endswith("." + suffix) for host in mx):
                key = provider
                break
    label, host, url = PROVIDERS.get(key, ("IMAP", "imap." + domain, ""))
    if key == "zoho" and mx:
        host = "imappro.zoho.eu" if any(h.endswith(".zoho.eu") for h in mx) else "imappro.zoho.com"
    if key == "gmx" and domain in ("gmx.de", "gmx.net"):
        host = "imap.gmx.net"
    hint = "Folosește parola de aplicație." if url else "Activează accesul IMAP în contul de e-mail."
    if key == "outlook":
        hint = "Conectează contul Microsoft."
    if key == "proton":
        hint = "Proton necesită Bridge local; conectarea directă nu este disponibilă."
    return {
        "provider": label,
        "host": host,
        "port": 993,
        "username": address,
        "app_password_required": bool(url),
        "app_password_url": url,
        "hint": hint,
        "supported": key != "proton",
        "method": "microsoft" if key == "outlook" else "imap",
        "hosts": [host] if key else [host, "mail." + domain],
    }


def _connect(host: str, port: int, timeout: float, deadline: float) -> imaplib.IMAP4:
    """Resolve once, reject every non-public result, pin the socket, retain TLS hostname verification."""
    addresses = list(dns.resolver.resolve_name(host, lifetime=2).addresses())
    if not addresses or any(
        (not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast) for a in addresses
    ):
        raise ValueError("private address")

    class Pinned(imaplib.IMAP4_SSL):
        def _create_socket(self, timeout):
            for ip in addresses:
                family = socket.AF_INET6 if ":" in ip else socket.AF_INET
                raw = None
                try:
                    raw = socket.socket(family, socket.SOCK_STREAM)
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError()
                    raw.settimeout(min(left, 3))
                    raw.connect((ip, port))
                    raw.settimeout(max(0.001, deadline - time.monotonic()))
                    return self.ssl_context.wrap_socket(raw, server_hostname=host)
                except OSError:
                    if raw is not None:
                        raw.close()
                    if ip == addresses[-1]:
                        raise
                except BaseException:
                    if raw is not None:
                        raw.close()
                    raise

    return imap.guarded(Pinned, deadline)(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)


def test_connection(config: dict[str, str], password: str) -> dict:
    """LOGIN, EXAMINE, LOGOUT only (imaplib also sends its mandatory CAPABILITY). No raw errors escape."""
    address = config.get("from_address", "")
    provider = detect(address)
    if "code" in provider:
        return provider
    if not provider["supported"] or provider["method"] == "microsoft":
        return {"code": "unsupported_provider"}
    try:
        port = int(config.get("port") or 993)
        folder = imap._folder(config.get("read_folder") or "INBOX")
        if port != 993 or not password:
            raise ValueError()
    except (ValueError, imap.ReadError):
        return {"code": "invalid_settings"}
    hosts = [config["host"]] if config.get("host") else provider["hosts"]
    result = {"code": "host_unreachable"}
    for host in hosts:
        result = _test(host, port, config.get("username") or address, password, folder)
        if result["code"] not in ("host_unreachable", "tls_error", "timeout"):
            break
    return result


def _test(host: str, port: int, username: str, password: str, folder: str) -> dict:
    m = None
    stage = "connect"
    try:
        m = _connect(host, port, TIMEOUT, time.monotonic() + TIMEOUT)
        stage = "login"
        m.login(username, password)
        stage = "folder"
        status, _ = m.select(folder, readonly=True)
        return (
            {"code": "ok", "host": host, "port": port, "username": username}
            if status == "OK"
            else {"code": "folder_missing"}
        )
    except (TimeoutError, imap._OutOfTime, dns.exception.Timeout):
        return {"code": "timeout"}
    except ssl.SSLError:
        return {"code": "tls_error"}
    except (OSError, dns.exception.DNSException):
        return {"code": "host_unreachable"}
    except (ValueError, UnicodeError, imap.ReadError):
        return {"code": "invalid_settings"}
    except imaplib.IMAP4.abort:
        return {"code": "host_unreachable"}
    except (imaplib.IMAP4.error, imap._TooLarge) as exc:
        # Inspect only for a known marker. Never return or log the server reply.
        app = "application-specific password required" in str(exc).lower() or "app password" in str(exc).lower()
        return {
            "code": "app_password_required"
            if app
            else "wrong_password"
            if stage == "login"
            else "folder_missing"
            if stage == "folder"
            else "protocol_error"
        }
    finally:
        if m is not None:
            imap._logout(m)
