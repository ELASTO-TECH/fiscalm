"""Bounded IP throttling and persistent, indistinguishable account-name throttling."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from sqlite3 import Row

from fiscalm.api import sessions, users
from fiscalm.ledger import Ledger


class IPAttempts:
    """One server-owned instance, shared by every authentication endpoint."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: OrderedDict[str, deque[int]] = OrderedDict()

    def attempt(self, ip: str, second: int) -> int:
        with self.lock:
            entries = self.entries.setdefault(ip, deque())
            self.entries.move_to_end(ip)
            while entries and entries[0] <= second - 60:
                entries.popleft()
            if len(entries) >= 20:
                return max(1, entries[0] + 60 - second)
            entries.append(second)
            if len(self.entries) > 10_000:
                self.entries.popitem(last=False)
            return 0


def key(username: str) -> str:
    return hashlib.sha256(users.canonical_username(username).encode("utf-8", "surrogatepass")).hexdigest()


def state(L: Ledger, username: str, at: datetime, *, include_busy: bool = True) -> dict:
    row = L.db.execute("SELECT * FROM auth_limits WHERE key_hash=?", (key(username),)).fetchone()
    current = sessions.stamp(at)
    locked = bool(row and row["locked_until"] and row["locked_until"] > current)
    fields = ("retry_at", "busy_until") if include_busy else ("retry_at",)
    deadlines = [row[k] for k in fields if row and row[k]]
    retry = max([0, *(math.ceil((datetime.fromisoformat(v) - at).total_seconds()) for v in deadlines)])
    return dict(locked=locked, retry_after=retry)


def failure(L: Ledger, username: str, at: datetime, *, reason: str, ip: str = "") -> None:
    if reason not in ("password", "totp", "passkey", "recovery", "unknown_user", "disabled_user"):
        raise ValueError("Unknown authentication failure reason")
    username, current = users.canonical_username(username), sessions.stamp(at)
    with L.tx() as db:
        row = db.execute("SELECT * FROM auth_limits WHERE key_hash=?", (key(username),)).fetchone()
        if row and row["locked_until"] and row["locked_until"] > current:
            return  # A flood cannot extend an existing 24-hour lock.
        history = db.execute(
            "SELECT count(*),min(at) FROM auth_failure_window WHERE key_hash=? AND at>?",
            (key(username), _time(at - timedelta(days=1))),
        ).fetchone()
        count = history[0] + 1
        owner = _owner(L, username)
        values = dict(
            key_hash=key(username),
            user_id=owner[0] if owner else None,
            failures=count,
            since=history[1] or _time(at),
            retry_at=_deadline(at, timedelta(seconds=min(2 ** (count - 5), 900))) if 5 <= count < 20 else None,
            locked_until=_deadline(at, timedelta(days=1)) if count >= 20 else None,
            expected=row["failures"] if row else None,
            expected_since=row["since"] if row else None,
            at=current,
            cutoff=_time(at - timedelta(days=1)),
            window_at=_time(at),
            detail=json.dumps({"reason": reason}),
            ip=ip[:64],
        )
        L.record_auth_failure(values)


def success(L: Ledger, user_id: str, username: str) -> None:
    """Only after the complete second-factor sign-in, never after its password step."""
    L.clear_auth_failures(user_id, key(username))


def unlock(L: Ledger, user_id: str, at: datetime, *, actor_id: str | None) -> None:
    with L.tx() as db:
        row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("unknown_user")
        L.unlock_user(user_id, key(row[0]), sessions.stamp(at), actor_id)


def _time(at: datetime) -> str:
    sessions.stamp(at)  # Reject naive timestamps before converting to UTC.
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def _deadline(at: datetime, duration: timedelta) -> str:
    target = at + duration
    if target.microsecond:
        target += timedelta(seconds=1)
    return sessions.stamp(target)


def _owner(L: Ledger, username: str) -> Row | None:
    if not users.valid_username(username):
        return None
    return L.db.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()


def reserve(L: Ledger, username: str, at: datetime) -> str | None:
    """One password computation per account name, before taking a KDF slot."""
    username = users.canonical_username(username)
    with L.tx():
        status = state(L, username, at)
        if status["locked"] or status["retry_after"]:
            return None
        owner, token = _owner(L, username), secrets.token_hex(32)
        values = dict(
            key_hash=key(username),
            user_id=owner[0] if owner else None,
            at=sessions.stamp(at),
            since=_time(at),
            reservation=sessions.digest(token),
            busy_until=_deadline(at, timedelta(seconds=10)),
            cutoff=_time(at - timedelta(days=1)),
        )
        L.reserve_auth_attempt(values)
        return token


def release(L: Ledger, username: str, token: str, at: datetime) -> bool:
    """Expired/superseded work may never publish a password ticket."""
    return bool(
        sessions.ID.fullmatch(token)
        and L.release_auth_attempt(key(username), sessions.digest(token), sessions.stamp(at))
    )
