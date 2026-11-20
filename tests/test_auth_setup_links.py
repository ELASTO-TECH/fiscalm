"""Setup and reset links, never URL-path credentials, with synthetic users only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from test_auth_users import accounts as accounts  # noqa: F401

from fiscalm.api import sessions, setup_links, tickets, users
from fiscalm.ledger import Ledger


@pytest.fixture
def pending(ledger, accounts):
    ids, at = accounts
    target = users.create(ledger, "pending", "Pending", "member", at, actor_id=ids["first"])
    return ids, at, target


def test_setup_link_expiry_supersession_and_single_use(ledger, pending):
    ids, at, target = pending
    path = setup_links.issue(ledger, target, at, actor_id=ids["first"])
    assert path.startswith("/setup#") and "?" not in path
    fragment = path.partition("#")[2]
    info = setup_links.inspect(ledger, fragment, at)
    assert info["user_id"] == target and info["purpose"] == "enroll"
    assert "secret_hash" not in info
    for bad in (path, "bad", "0" * 32 + "." + "0" * 32, fragment[:33] + "0" * 32, fragment[:-1]):
        assert setup_links.inspect(ledger, bad, at) is None
        assert not setup_links.consume(ledger, bad, at)
    expired = at + timedelta(hours=72)
    assert setup_links.inspect(ledger, fragment, expired) is None
    assert not setup_links.consume(ledger, fragment, expired)
    newer = setup_links.issue(ledger, target, at, actor_id=None).partition("#")[2]
    assert setup_links.inspect(ledger, fragment, at) is None
    assert not setup_links.consume(ledger, fragment, at)
    assert setup_links.consume(ledger, newer, at)
    assert not setup_links.consume(ledger, newer, at)
    assert setup_links.inspect(ledger, newer, at) is None
    events = [dict(r) for r in ledger.db.execute("SELECT kind,actor,detail FROM auth_events")]
    assert [r["kind"] for r in events][-3:] == ["setup_link_created", "setup_link_created", "setup_link_used"]
    assert not any(value in str(events) for value in (fragment, newer, fragment.partition(".")[2]))


def test_setup_link_race_and_outer_rollback(ledger, pending):
    _, at, target = pending
    fragment = setup_links.issue(ledger, target, at, actor_id=None).partition("#")[2]
    with pytest.raises(ValueError), ledger.tx():
        assert setup_links.consume(ledger, fragment, at)
        raise ValueError("synthetic activation failure")
    barrier = Barrier(2)

    def consume(_):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return setup_links.consume(other, fragment, at)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume, range(2))) == [False, True]


def test_reset_revokes_access_and_root_recovers_last_admin(ledger, accounts):
    ids, at = accounts
    first, second = ids["first"], ids["second"]
    cookie = sessions.issue(ledger, second, at)
    ticket = tickets.issue(ledger, second, "totp", at)
    ledger.db.execute("INSERT INTO recovery_codes VALUES(?,'synthetic-hash',?,NULL)", (second, sessions.stamp(at)))
    ledger.db.commit()
    path = setup_links.issue(ledger, second, at, actor_id=first, reset=True)
    assert setup_links.inspect(ledger, path.partition("#")[2], at)["purpose"] == "reset"
    assert sessions.identify(ledger, cookie, at) is None
    assert tickets.attempt(ledger, ticket, "totp", at) is None
    assert not ledger.db.execute("SELECT 1 FROM recovery_codes WHERE user_id=?", (second,)).fetchone()
    user = ledger.db.execute("SELECT * FROM users WHERE id=?", (second,)).fetchone()
    assert user["password_hash"] is None and user["totp_version"] is None
    before = ledger.db.total_changes
    with pytest.raises(ValueError):
        setup_links.issue(ledger, first, at, actor_id=first, reset=True)
    assert ledger.db.total_changes == before
    setup_links.issue(ledger, first, at, actor_id=None, reset=True)
    assert ledger.db.execute("SELECT password_hash FROM users WHERE id=?", (first,)).fetchone()[0] is None
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='user_reset'").fetchone()[0] == 2


def test_setup_refuses_wrong_role_disabled_and_configured_users(ledger, pending):
    ids, at, target = pending
    with pytest.raises(PermissionError):
        setup_links.issue(ledger, target, at, actor_id=ids["member"])
    with pytest.raises(ValueError):
        setup_links.issue(ledger, ids["first"], at, actor_id=None)
    with pytest.raises(ValueError):
        setup_links.issue(ledger, "missing", at, actor_id=None)
    fragment = setup_links.issue(ledger, target, at, actor_id=None).partition("#")[2]
    ledger.change_user(target, None, sessions.stamp(at), enabled=False)
    assert setup_links.inspect(ledger, fragment, at) is None
    assert not setup_links.consume(ledger, fragment, at)
    with pytest.raises(ValueError):
        setup_links.issue(ledger, target, at, actor_id=None)
