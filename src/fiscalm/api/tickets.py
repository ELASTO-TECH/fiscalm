"""Single-use, five-minute pre-authentication state, separate from sessions."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from fiscalm.api import sessions
from fiscalm.ledger import Ledger


def issue(
    L: Ledger,
    user_id: str | None,
    purpose: str,
    at: datetime,
    *,
    session_hash: str | None = None,
    challenge: bytes | None = None,
) -> str:
    ident = secrets.token_hex(32)
    values = dict(
        id_hash=sessions.digest(ident),
        user_id=user_id,
        purpose=purpose,
        session_hash=session_hash,
        challenge=challenge,
        expires_at=sessions.stamp(at + timedelta(minutes=5)),
    )
    if not L.add_auth_ticket(values, sessions.stamp(at)):
        raise PermissionError("user_disabled")
    return ident


def attempt(L: Ledger, ident: str, purpose: str, at: datetime, *, session_hash: str | None = None) -> dict | None:
    if not sessions.ID.fullmatch(ident):
        return None
    return L.attempt_auth_ticket(sessions.digest(ident), purpose, session_hash, sessions.stamp(at))


def consume(L: Ledger, ticket: dict, at: datetime) -> bool:
    return L.consume_auth_ticket(ticket, sessions.stamp(at))
