"""Hashed session identifiers and cookies; no sign-in or factor verification here."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

from fiscalm.ledger import Ledger

COOKIE = "__Host-session"
ATTRIBUTES = "Path=/; Secure; HttpOnly; SameSite=Strict"
ID = re.compile(r"[0-9a-f]{64}", re.ASCII)


# First match wins: Edge and Opera also announce Chrome, Chrome also announces Safari.
# A desktop-mode iPad announces itself as a Mac; the agent alone cannot tell them apart.
_BROWSERS = (
    ("Edg/", "Edge"),
    ("EdgA/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"),
    ("OPT/", "Opera"),
    ("OPiOS/", "Opera"),
    ("Firefox/", "Firefox"),
    ("FxiOS/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def device(agent: str) -> str:
    """A person-readable name such as "Chrome pe Linux"; the raw user agent never leaves the engine."""
    browser = next((name for mark, name in _BROWSERS if mark in agent), "")
    system = next((name for mark, name in _SYSTEMS if mark in agent), "")
    if browser and system:
        return f"{browser} pe {system}"
    return browser or system or "Dispozitiv necunoscut"


def stamp(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("Authentication timestamps must include a timezone")
    return at.astimezone(UTC).isoformat(timespec="seconds")


def digest(ident: str) -> str:
    return hashlib.sha256(ident.encode()).hexdigest()


def method_name(method: str, *, recovery: bool = False) -> str:
    """Keep code-only recovery distinct from recovery that already proved a password."""
    if method not in ("password_totp", "passkey"):
        raise ValueError("Unknown strong sign-in method")
    return ("recovery_code" if method == "passkey" else "recovery") if recovery else method


def issue(
    L: Ledger,
    user_id: str,
    at: datetime,
    *,
    recovery: bool = False,
    method: str = "password_totp",
    agent: str = "",
    ip: str = "",
) -> str:
    """Called inside the factor-consumption transaction, never after a password alone."""
    recorded_method = method_name(method, recovery=recovery)
    ident = secrets.token_hex(32)
    values = dict(
        id_hash=digest(ident),
        user_id=user_id,
        method=recorded_method,
        recovery_only=int(recovery),
        created_at=stamp(at),
        idle_expires_at=stamp(at + timedelta(days=14)),
        expires_at=stamp(at + timedelta(days=30)),
        user_agent=agent[:200],
        ip=ip[:64],
    )
    if not L.add_session(values):
        raise PermissionError("user_disabled")
    return ident


def identify(L: Ledger, ident: str, at: datetime) -> dict | None:
    if not ID.fullmatch(ident):
        return None
    key, current = digest(ident), stamp(at)
    row = _read(L, key, current)
    if row and row["last_seen_at"] <= stamp(at - timedelta(minutes=1)):
        L.touch_session(key, current, stamp(at + timedelta(days=14)), stamp(at - timedelta(minutes=1)))
        row = _read(L, key, current)
    return row


def lookup(L: Ledger, id_hash: str, at: datetime) -> dict | None:
    """Read committed authorization by stored id hash under the caller's write lock."""
    return _read(L, id_hash, stamp(at)) if ID.fullmatch(id_hash) else None


def _read(L: Ledger, key: str, current: str) -> dict | None:
    row = L.db.execute(
        """SELECT s.*,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.id_hash=? AND s.revoked_at IS NULL AND u.disabled_at IS NULL
        AND s.expires_at>? AND s.idle_expires_at>?""",
        (key, current, current),
    ).fetchone()
    return dict(row) if row else None


def recent(session: dict, at: datetime) -> bool:
    return not session["recovery_only"] and stamp(at - timedelta(minutes=10)) < session["strong_auth_at"] <= stamp(at)


def set_cookie(ident: str) -> str:
    if not ID.fullmatch(ident):
        raise ValueError("Invalid session identifier")
    return f"{COOKIE}={ident}; {ATTRIBUTES}"


def clear_cookie() -> str:
    return f"{COOKIE}=; Max-Age=0; {ATTRIBUTES}"


def from_cookie(header: str, *, secure: bool) -> str:
    """Transport must establish HTTPS itself; an HTTP cookie is refused even on loopback."""
    values = []
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            values.append(value)
    return values[0] if secure and len(values) == 1 and ID.fullmatch(values[0]) else ""
