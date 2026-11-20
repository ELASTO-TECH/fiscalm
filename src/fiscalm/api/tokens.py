"""User-owned, install-wide API credentials. Only creation returns the bearer secret."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta

from fiscalm.api.responses import fail
from fiscalm.ledger import Ledger, now

TOKEN = re.compile(r"fiscalm_([0-9a-f]{16})_([0-9a-f]{64})", re.ASCII)
PUBLIC = "id, name, scope, created_by, created_at, last_used_at, revoked_at, user_id"


def create(L: Ledger, name: str, scope: str, *, user_id: str, created_by: str | None = None) -> dict:
    """Create a credential; the caller must show its secret once and never log it."""
    if isinstance(name, str):
        name = name.strip()
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or scope not in ("read", "write"):
        raise ValueError("Use a name of 1-100 characters and scope read or write.")
    ident, secret = secrets.token_hex(8), secrets.token_hex(32)
    bearer = f"fiscalm_{ident}_{secret}"
    with L.tx() as db:
        owner = db.execute("SELECT role FROM users WHERE id=? AND disabled_at IS NULL", (user_id,)).fetchone()
        if not owner or (scope == "write" and owner["role"] == "readonly"):
            raise ValueError("An enabled owner with sufficient rights is required.")
        at = now()
        db.execute(
            "INSERT INTO api_tokens(id,name,scope,secret_hash,created_at,created_by,user_id) VALUES(?,?,?,?,?,?,?)",
            (ident, name.strip(), scope, hashlib.sha256(bearer.encode()).hexdigest(), at, created_by, user_id),
        )
        db.execute(
            "INSERT INTO auth_events(at,kind,user_id,actor,detail) VALUES(?,'api_token_created',?,?,?)",
            (at, user_id, "user " + created_by if created_by else "cli", '{"id":"' + ident + '"}'),
        )
    return {"id": ident, "token": bearer, "scope": scope}


def list_tokens(L: Ledger, *, user_id: str | None = None) -> list[dict]:
    """Return metadata only, including revoked credentials for audit."""
    return [
        _metadata(r)
        for r in L.db.execute(
            "SELECT t.*,u.display_name AS owner_name FROM api_tokens t LEFT JOIN users u ON u.id=t.user_id "
            "WHERE (? IS NULL OR t.user_id=?) ORDER BY t.created_at DESC,t.id DESC",
            (user_id, user_id),
        )
    ]


def _metadata(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in PUBLIC.split(", ")} | {
        "owner": {"id": row["user_id"], "display_name": row["owner_name"]} if row["user_id"] else None
    }


def _current(L: Ledger, ident: str) -> dict | None:
    row = L.db.execute(
        "SELECT t.*,u.role AS owner_role,u.display_name AS owner_name FROM api_tokens t "
        "JOIN users u ON u.id=t.user_id WHERE t.id=? AND t.revoked_at IS NULL AND u.disabled_at IS NULL",
        (ident,),
    ).fetchone()
    return (_metadata(row) | {"scope": "read" if row["owner_role"] == "readonly" else row["scope"]}) if row else None


def authorize(L: Ledger, credential: dict, *, admin: bool = False) -> None:
    """Recheck token and owner together under the financial writer's lock."""
    current = _current(L, credential.get("id", ""))
    if not current or current["user_id"] != credential.get("user_id"):
        fail(401, "invalid_token", "The token is no longer authorized. Use an active token.")
    if (
        admin
        and not L.db.execute(
            "SELECT 1 FROM users WHERE id=? AND role='admin' AND disabled_at IS NULL", (current["user_id"],)
        ).fetchone()
    ):
        fail(403, "admin_required", "Only administrators can manage install-wide upload links.")
    if current["scope"] != "write":
        fail(403, "insufficient_scope", "The token or its owner is read-only.")


def revoke(L: Ledger, ident: str, *, actor_id: str | None = None) -> bool:
    with L.tx() as db:
        at = now()
        administrator = actor_id is None
        if actor_id is not None:
            actor = db.execute("SELECT role FROM users WHERE id=? AND disabled_at IS NULL", (actor_id,)).fetchone()
            target = db.execute("SELECT user_id FROM api_tokens WHERE id=?", (ident,)).fetchone()
            if not actor or (actor["role"] != "admin" and (not target or target["user_id"] != actor_id)):
                raise PermissionError("token_not_owned")
            administrator = actor["role"] == "admin"
        row = db.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL AND (user_id=? OR ?) RETURNING user_id",
            (at, ident, actor_id, administrator),
        ).fetchone()
        if row:
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor,detail) VALUES(?,'api_token_revoked',?,?,?)",
                (at, row["user_id"], "user " + actor_id if actor_id else "cli", '{"id":"' + ident + '"}'),
            )
        return row is not None


def verify(L: Ledger, bearer: str, scope: str = "read") -> dict | None:
    """Constant-time hash comparison, including unknown ids; write implies read."""
    match = TOKEN.fullmatch(bearer)
    ident = match[1] if match else ""
    row = L.db.execute("SELECT * FROM api_tokens WHERE id=?", (ident,)).fetchone()
    digest = hashlib.sha256(bearer.encode()).hexdigest()
    equal = hmac.compare_digest(digest, row["secret_hash"] if row else "0" * 64)
    if not equal or not row or row["revoked_at"] or scope not in ("read", "write"):
        return None
    stamp = now()
    if row["last_used_at"] and datetime.fromisoformat(stamp) - datetime.fromisoformat(row["last_used_at"]) < timedelta(
        minutes=1
    ):
        current = _current(L, ident)
        return current if current and (scope == "read" or current["scope"] == "write") else None
    timeout = L.db.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        L.db.execute("PRAGMA busy_timeout=0")
        with L.tx() as db:
            if (
                db.execute(
                    "UPDATE api_tokens SET last_used_at=? WHERE id=? AND revoked_at IS NULL", (stamp, ident)
                ).rowcount
                != 1
            ):
                return None
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        # Usage metadata is best effort; authentication still checks committed revocations.
        if not L.db.execute("SELECT 1 FROM api_tokens WHERE id=? AND revoked_at IS NULL", (ident,)).fetchone():
            return None
    finally:
        L.db.execute(f"PRAGMA busy_timeout={timeout}")
    current = _current(L, ident)
    return current if current and (scope == "read" or current["scope"] == "write") else None
