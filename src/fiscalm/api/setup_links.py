"""One-time account setup capabilities; secrets travel in a fragment, then a JSON body."""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import datetime, timedelta

from fiscalm.api import limits, sessions
from fiscalm.ledger import Ledger

FRAGMENT = re.compile(r"([0-9a-f]{32})\.([0-9a-f]{32})", re.ASCII)


def issue(L: Ledger, user_id: str, at: datetime, *, actor_id: str | None, reset: bool = False) -> str:
    ident, secret = secrets.token_hex(16), secrets.token_hex(16)
    row = L.db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    L.add_setup_link(
        dict(
            id=ident,
            auth_key=limits.key(row[0]) if row else "",
            secret_hash=sessions.digest(secret),
            user_id=user_id,
            purpose="reset" if reset else "enroll",
            created_at=sessions.stamp(at),
            expires_at=sessions.stamp(at + timedelta(hours=72)),
            created_by="cli" if actor_id is None else "user " + actor_id,
        ),
        actor_id,
    )
    return f"/setup#{ident}.{secret}"


def inspect(L: Ledger, fragment: str, at: datetime) -> dict | None:
    parsed = FRAGMENT.fullmatch(fragment)
    if not parsed:
        return None
    row = L.db.execute(
        """SELECT l.*,u.username,u.display_name,u.disabled_at FROM setup_links l
        JOIN users u ON u.id=l.user_id WHERE l.id=?""",
        (parsed[1],),
    ).fetchone()
    equal = hmac.compare_digest(sessions.digest(parsed[2]), row["secret_hash"] if row else "0" * 64)
    if (
        not equal
        or not row
        or row["used_at"]
        or row["revoked_at"]
        or row["disabled_at"]
        or row["expires_at"] <= sessions.stamp(at)
    ):
        return None
    return {key: row[key] for key in ("id", "user_id", "username", "display_name", "purpose")}


def consume(L: Ledger, fragment: str, at: datetime) -> bool:
    parsed = FRAGMENT.fullmatch(fragment)
    return bool(parsed and L.consume_setup_link(parsed[1], sessions.digest(parsed[2]), sessions.stamp(at)))
