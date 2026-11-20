"""Persistent account-name limits and bounded server-local IP attempt windows."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from test_auth_users import accounts as accounts  # noqa: F401

from fiscalm.api import limits, sessions, setup_links, users
from fiscalm.ledger import Ledger


def test_ip_sliding_window_and_bounded_storage():
    attempts = limits.IPAttempts()
    assert [attempts.attempt("synthetic-ip", i) for i in range(20)] == [0] * 20
    assert attempts.attempt("synthetic-ip", 20) == 40
    assert attempts.attempt("synthetic-ip", 59) == 1
    assert attempts.attempt("synthetic-ip", 60) == 0
    assert attempts.attempt("another-ip", 20) == 0
    for i in range(10001):
        attempts.attempt(str(i), 100)
    assert len(attempts.entries) == 10000


def test_known_unknown_delay_lock_restart_and_expiry(ledger, accounts):
    ids, at = accounts
    for count in range(1, 21):
        for username in ("second", "unknown"):
            limits.failure(ledger, username, at, reason="password")
        known = limits.state(ledger, "second", at)
        assert known == limits.state(ledger, "unknown", at)
        assert known == dict(locked=count >= 20, retry_after=min(2 ** (count - 5), 900) if 5 <= count < 20 else 0)
        if known["retry_after"]:
            assert limits.state(ledger, "second", at + timedelta(microseconds=1))["retry_after"] == known["retry_after"]
        if count < 20:
            at += timedelta(seconds=max(1, known["retry_after"]))
    limits.failure(ledger, "second", at + timedelta(hours=1), reason="recovery")
    assert ledger.db.execute("SELECT failures FROM users WHERE id=?", (ids["second"],)).fetchone()[0] == 20
    other = Ledger(ledger.path)
    try:
        assert limits.state(other, "ＳＥＣＯＮＤ", at)["locked"]
        assert limits.state(other, "unknown", at)["locked"]
    finally:
        other.close()
    expired = at + timedelta(days=1)
    assert not limits.state(ledger, "second", expired)["locked"]
    limits.failure(ledger, "second", expired, reason="totp")
    assert ledger.db.execute("SELECT failures FROM users WHERE id=?", (ids["second"],)).fetchone()[0] == 1
    limits.success(ledger, ids["second"], "second")
    assert limits.state(ledger, "second", expired) == dict(locked=False, retry_after=0)
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='account_locked'").fetchone()[0] == 2


def test_unlock_reset_and_precreation_limits(ledger, accounts):
    ids, at = accounts
    limits.failure(ledger, "second", at, reason="recovery")
    with pytest.raises(PermissionError):
        limits.unlock(ledger, ids["second"], at, actor_id=ids["member"])
    limits.unlock(ledger, ids["second"], at, actor_id=ids["first"])
    assert not ledger.db.execute("SELECT 1 FROM auth_limits").fetchone()
    limits.failure(ledger, "second", at, reason="totp")
    setup_links.issue(ledger, ids["second"], at, actor_id=ids["first"], reset=True)
    assert not ledger.db.execute("SELECT 1 FROM auth_limits").fetchone()
    limits.failure(ledger, "pending", at, reason="unknown_user")
    ident = users.create(ledger, "pending", "Pending", "member", at, actor_id=None)
    limits.unlock(ledger, ident, at, actor_id=None)
    assert not ledger.db.execute("SELECT 1 FROM auth_limits").fetchone()
    with pytest.raises(ValueError):
        limits.unlock(ledger, "missing", at, actor_id=None)
    with pytest.raises(ValueError):
        limits.failure(ledger, "second", at, reason="uncontrolled-detail")


def test_failure_race_and_bounded_unknown_names(ledger, accounts):
    _, at = accounts
    barrier = Barrier(2)

    def fail(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            limits.failure(other, "second", at, reason="password")
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(fail, range(2)))
    assert ledger.db.execute("SELECT failures FROM auth_limits WHERE user_id IS NOT NULL").fetchone()[0] == 2
    row = dict(ledger.db.execute("SELECT * FROM auth_limits WHERE user_id IS NOT NULL").fetchone())
    with pytest.raises(PermissionError):
        ledger.record_auth_failure({**row, "expected": -1, "expected_since": row["since"]})
    assert dict(ledger.db.execute("SELECT * FROM auth_limits WHERE user_id IS NOT NULL").fetchone()) == row
    ledger.db.executemany(
        "INSERT INTO auth_limits(key_hash,failures,since) VALUES(?,1,?)",
        [(str(i), at.isoformat()) for i in range(10001)],
    )
    ledger.db.executemany(
        "INSERT INTO auth_failure_window(key_hash,at) VALUES(?,?)",
        [(str(i), at.isoformat(timespec="microseconds")) for i in range(10001)],
    )
    ledger.db.commit()
    limits.failure(ledger, "unknown", at, reason="unknown_user")
    assert ledger.db.execute("SELECT count(*) FROM auth_limits WHERE user_id IS NULL").fetchone()[0] == 10000
    assert ledger.db.execute("SELECT failures FROM auth_limits WHERE user_id IS NOT NULL").fetchone()[0] == 2


def test_reservation_race_expiry_and_nonce_binding(ledger, accounts):
    _, at = accounts
    barrier = Barrier(2)

    def reserve(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return limits.reserve(other, "ＳＥＣＯＮＤ", at)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(reserve, range(2)))
    assert sum(t is not None for t in tokens) == 1
    token = next(t for t in tokens if t is not None)
    assert limits.reserve(ledger, "second", at) is None
    with pytest.raises(PermissionError):
        ledger.reserve_auth_attempt(
            dict(
                key_hash=limits.key("second"),
                user_id=accounts[0]["second"],
                since=sessions.stamp(at),
                at=sessions.stamp(at),
                reservation="stale",
                busy_until=sessions.stamp(at + timedelta(seconds=10)),
                cutoff=sessions.stamp(at - timedelta(days=1)),
            )
        )
    assert not limits.release(ledger, "second", "bad", at)
    assert not limits.release(ledger, "second", "0" * 64, at)
    later = at + timedelta(seconds=10)
    replacement = limits.reserve(ledger, "second", later)
    assert replacement and replacement != token
    assert not limits.release(ledger, "second", token, later)
    assert not limits.release(ledger, "first", replacement, later)
    assert limits.release(ledger, "second", replacement, later)
    assert not limits.release(ledger, "second", replacement, later)
    assert limits.reserve(ledger, "second", later)


def test_subsecond_deadlines_rolling_day_and_surrogate_name(ledger, accounts):
    _, at = accounts
    for hour in range(19):
        limits.failure(ledger, "second", at + timedelta(hours=hour), reason="password")
    boundary = at + timedelta(days=1, seconds=1)
    limits.failure(ledger, "second", boundary, reason="password")
    assert ledger.db.execute("SELECT failures FROM users WHERE username='second'").fetchone()[0] == 19
    limits.failure(ledger, "second", boundary + timedelta(seconds=1), reason="password")
    assert limits.state(ledger, "second", boundary + timedelta(seconds=1))["locked"]
    assert limits.reserve(ledger, "second", boundary + timedelta(seconds=1)) is None
    fractional = at + timedelta(microseconds=900000)
    for _ in range(5):
        limits.failure(ledger, "unknown", fractional, reason="unknown_user")
    assert limits.state(ledger, "unknown", fractional + timedelta(seconds=0.2))["retry_after"] >= 1
    assert limits.reserve(ledger, "unknown", fractional + timedelta(seconds=0.2)) is None
    limits.failure(ledger, "x\ud800", at, reason="unknown_user")
    assert limits.reserve(ledger, "x\ud800", at)


def test_unknown_cleanup_keeps_a_new_reservation_after_old_failure(ledger, accounts):
    _, at = accounts
    limits.failure(ledger, "unknown", at, reason="unknown_user")
    later = at + timedelta(hours=25)
    token = limits.reserve(ledger, "unknown", later)
    assert token
    assert limits.reserve(ledger, "unknown", later) is None
    assert limits.release(ledger, "unknown", token, later)


def test_unknown_cleanup_keeps_recent_failures_and_retry_deadline(ledger, accounts):
    _, at = accounts
    for _ in range(4):
        limits.failure(ledger, "unknown", at, reason="unknown_user")
    recent = at + timedelta(days=1) - timedelta(microseconds=100000)
    limits.failure(ledger, "unknown", recent, reason="unknown_user")
    later = at + timedelta(days=1, microseconds=100000)
    limits.failure(ledger, "another", later, reason="unknown_user")
    assert limits.state(ledger, "unknown", later)["retry_after"] >= 1
    assert (
        ledger.db.execute(
            "SELECT count(*) FROM auth_failure_window WHERE key_hash=?", (limits.key("unknown"),)
        ).fetchone()[0]
        == 1
    )
    limits.failure(ledger, "unknown", later + timedelta(seconds=2), reason="unknown_user")
    assert (
        ledger.db.execute("SELECT failures FROM auth_limits WHERE key_hash=?", (limits.key("unknown"),)).fetchone()[0]
        == 2
    )
