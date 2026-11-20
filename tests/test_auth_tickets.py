"""Pre-authentication tickets are finite, bound to their purpose/session, and single use."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from test_auth_sessions import users as users  # noqa: F401

from fiscalm.api import sessions, tickets
from fiscalm.ledger import Ledger


def test_ticket_boundaries_attempt_limit_and_restart(ledger, users):
    token = tickets.issue(ledger, "first", "totp", users, session_hash="first-session")
    stored = dict(ledger.db.execute("SELECT * FROM auth_tickets").fetchone())
    assert stored["id_hash"] == sessions.digest(token) and token not in str(stored)
    assert not tickets.consume(ledger, stored, users)  # No attempt has been claimed.
    for ident, purpose, binding in [
        ("bad", "totp", "first-session"),
        (token, "setup", "first-session"),
        (token, "totp", "second-session"),
        (token, "totp", None),
    ]:
        assert tickets.attempt(ledger, ident, purpose, users, session_hash=binding) is None
    for expected in range(1, 5):
        assert tickets.attempt(ledger, token, "totp", users, session_hash="first-session")["attempts"] == expected
    other = Ledger(ledger.path)
    try:
        fifth = tickets.attempt(other, token, "totp", users, session_hash="first-session")
        assert fifth["attempts"] == 5
        assert tickets.attempt(other, token, "totp", users, session_hash="first-session") is None
        assert not tickets.consume(other, {**fifth, "user_id": "second"}, users)
        assert tickets.consume(other, fifth, users)
        assert not tickets.consume(other, fifth, users)
    finally:
        other.close()
    assert tickets.attempt(ledger, token, "totp", users, session_hash="first-session") is None


def test_ticket_expiry_disabled_owner_and_stale_attempt(ledger, users):
    token = tickets.issue(ledger, "first", "totp", users)
    first = tickets.attempt(ledger, token, "totp", users)
    second = tickets.attempt(ledger, token, "totp", users)
    assert not tickets.consume(ledger, first, users)
    expired = users + timedelta(minutes=5)
    assert tickets.attempt(ledger, token, "totp", expired) is None
    assert not tickets.consume(ledger, second, expired)
    ledger.db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='first'")
    ledger.db.commit()
    assert tickets.attempt(ledger, token, "totp", users) is None
    assert not tickets.consume(ledger, second, users)
    for owner in ("first", "missing"):
        with pytest.raises(PermissionError):
            tickets.issue(ledger, owner, "totp", users)


@pytest.mark.parametrize("anonymous", [False, True])
def test_two_connections_consume_once_and_outer_rollback(ledger, users, anonymous):
    owner, purpose = (None, "passkey_signin") if anonymous else ("first", "totp")
    token = tickets.issue(ledger, owner, purpose, users, challenge=b"c" * 32)
    claimed = tickets.attempt(ledger, token, purpose, users)
    barrier = Barrier(2)

    def consume(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return tickets.consume(other, claimed, users)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]
    token = tickets.issue(ledger, owner, purpose, users, challenge=b"c" * 32)
    with pytest.raises(ValueError), ledger.tx():
        claimed = tickets.attempt(ledger, token, purpose, users)
        assert tickets.consume(ledger, claimed, users)
        raise ValueError("synthetic factor failure")
    assert tickets.attempt(ledger, token, purpose, users)["attempts"] == 1


def test_only_unbound_passkey_signin_with_32_byte_challenge_may_have_no_owner(ledger, users):
    for owner, purpose, binding, challenge in (
        (None, "signin", None, b"c" * 32),
        (None, "stepup", None, b"c" * 32),
        (None, "passkey_signin", "session", b"c" * 32),
        ("first", "passkey_signin", None, b"c" * 32),
        (None, "passkey_signin", None, None),
        (None, "passkey_signin", None, "c" * 32),
        (None, "passkey_signin", None, b"c" * 31),
        (None, "passkey_signin", None, b"c" * 33),
    ):
        with pytest.raises(PermissionError):
            tickets.issue(ledger, owner, purpose, users, session_hash=binding, challenge=challenge)
    assert ledger.db.execute("SELECT count(*) FROM auth_tickets").fetchone()[0] == 0
    token = tickets.issue(ledger, None, "passkey_signin", users, challenge=b"c" * 32)
    assert tickets.attempt(ledger, token, "signin", users) is None
    assert tickets.attempt(ledger, token, "passkey_signin", users, session_hash="session") is None
    first = tickets.attempt(ledger, token, "passkey_signin", users)
    for count in range(2, 6):
        current = tickets.attempt(ledger, token, "passkey_signin", users)
        assert current["attempts"] == count and current["user_id"] is None
    assert tickets.attempt(ledger, token, "passkey_signin", users) is None
    assert not tickets.consume(ledger, first, users)
    assert not tickets.consume(ledger, current | {"user_id": "first"}, users)
    assert tickets.consume(ledger, current, users) and not tickets.consume(ledger, current, users)
    token = tickets.issue(ledger, None, "passkey_signin", users, challenge=b"c" * 32)
    claimed = tickets.attempt(ledger, token, "passkey_signin", users)
    assert not tickets.consume(ledger, claimed, users + timedelta(minutes=5))
    assert tickets.attempt(ledger, token, "passkey_signin", users + timedelta(minutes=5)) is None


@pytest.mark.parametrize(
    "column,value",
    [
        ("purpose", "signin"),
        ("user_id", "first"),
        ("session_hash", "session"),
        ("challenge", b"short"),
        ("challenge", "c" * 32),
    ],
)
def test_anonymous_ticket_guards_are_rechecked_on_attempt_and_consume(ledger, users, column, value):
    token = tickets.issue(ledger, None, "passkey_signin", users, challenge=b"c" * 32)
    claimed = tickets.attempt(ledger, token, "passkey_signin", users)
    with ledger.tx() as db:
        db.execute(f"UPDATE auth_tickets SET {column}=?", (value,))
    changed = claimed | {column: value}
    assert tickets.attempt(ledger, token, changed["purpose"], users, session_hash=changed["session_hash"]) is None
    assert not tickets.consume(ledger, changed, users)


def test_ticket_capacity_evicts_oldest_and_sweeps_used_expired(ledger, users):
    ledger.db.executemany(
        """INSERT INTO auth_tickets(id_hash,purpose,user_id,expires_at)
        VALUES (?,'totp','first',?)""",
        [(str(i), sessions.stamp(users + timedelta(seconds=i + 1))) for i in range(1000)],
    )
    ledger.db.commit()
    fresh = tickets.issue(ledger, "first", "totp", users + timedelta(seconds=10))
    assert ledger.db.execute("SELECT count(*) FROM auth_tickets").fetchone()[0] == 991
    # Fill to capacity and then evict the oldest live ticket, not the fresh one.
    for _ in range(10):
        tickets.issue(ledger, "first", "totp", users + timedelta(seconds=10))
    assert ledger.db.execute("SELECT count(*) FROM auth_tickets").fetchone()[0] == 1000
    assert not ledger.db.execute("SELECT 1 FROM auth_tickets WHERE id_hash='10'").fetchone()
    claimed = tickets.attempt(ledger, fresh, "totp", users + timedelta(seconds=10))
    assert tickets.consume(ledger, claimed, users + timedelta(seconds=10))
    tickets.issue(ledger, "first", "totp", users + timedelta(seconds=10))
    assert not ledger.db.execute("SELECT 1 FROM auth_tickets WHERE id_hash=?", (sessions.digest(fresh),)).fetchone()
