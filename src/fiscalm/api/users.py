"""Account names and guarded administrator-managed roles. No per-company permissions."""

from __future__ import annotations

import re
import secrets
import unicodedata
from datetime import datetime

from fiscalm.api.sessions import device, stamp
from fiscalm.api.webauthn import b64
from fiscalm.ledger import Ledger


def canonical_username(value: str) -> str:
    return unicodedata.normalize("NFKC", value).lower()


def valid_username(value: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value, re.ASCII) is not None


def create(L: Ledger, username: str, display_name: str, role: str, at: datetime, *, actor_id: str | None) -> str:
    username = canonical_username(username)
    display_name = display(display_name)
    if not valid_username(username):
        raise ValueError("Numele de utilizator trebuie să aibă între 1 și 64 de litere, cifre sau caractere ._-.")
    if role not in ("admin", "member", "readonly"):
        raise ValueError("Rol necunoscut.")
    ident = "u_" + secrets.token_hex(16)
    L.add_user(
        dict(
            id=ident,
            username=username,
            display_name=display_name,
            role=role,
            webauthn_handle=secrets.token_bytes(16),
            created_at=stamp(at),
            created_by="cli" if actor_id is None else "user " + actor_id,
        ),
        actor_id,
    )
    return ident


def display(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(value) <= 100 or not value.isprintable():
        raise ValueError("Numele afișat trebuie să aibă între 1 și 100 de caractere.")
    return value


def listing(L: Ledger) -> list[dict]:
    return [
        dict(r) | {"password_totp": bool(r["password_totp"])}
        for r in L.db.execute(
            "SELECT id,username,display_name,role,disabled_at,locked_until,"
            "(password_hash IS NOT NULL AND totp_version IS NOT NULL) AS password_totp,"
            "(SELECT count(*) FROM passkeys WHERE user_id=u.id) AS passkeys,"
            "(SELECT max(at) FROM auth_events WHERE user_id=u.id AND kind='signin') AS last_signin "
            "FROM users u ORDER BY username"
        )
    ]


def active_sessions(L: Ledger, user_id: str, at: datetime) -> list[dict]:
    return [
        dict(
            id=r["id"],
            method=r["method"],
            created_at=r["created_at"],
            last_seen_at=r["last_seen_at"],
            device=device(r["user_agent"]),
            ip=r["ip"],
        )
        for r in L.db.execute(
            "SELECT id_hash AS id,method,created_at,last_seen_at,user_agent,ip FROM sessions "
            "WHERE user_id=? AND revoked_at IS NULL AND expires_at>? AND idle_expires_at>? ORDER BY last_seen_at DESC",
            (user_id, stamp(at), stamp(at)),
        )
    ]


def passkey_listing(L: Ledger, user_id: str) -> list[dict]:
    return [
        dict(
            id=b64(r["credential_id"]),
            name=r["name"],
            backup_eligible=bool(r["backup_eligible"]),
            backed_up=bool(r["backed_up"]),
            created_at=r["created_at"],
            last_used_at=r["last_used_at"],
        )
        for r in L.db.execute(
            "SELECT credential_id,name,backup_eligible,backed_up,created_at,last_used_at FROM passkeys WHERE user_id=? ORDER BY created_at,credential_id",
            (user_id,),
        )
    ]
