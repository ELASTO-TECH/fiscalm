"""Session storage and HTTP-cookie refusals with synthetic users and a fixed UTC clock."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from fiscalm import cli
from fiscalm.api import maintenance, sessions, setup_links, tickets, user_cli, users as account_users
from fiscalm.ledger import Ledger


@pytest.fixture
def users(ledger):
    for name in ("first", "second"):
        ledger.db.execute(
            """INSERT INTO users(id,username,display_name,role,webauthn_handle,created_at)
            VALUES (?,?,?,'member',?,'2026-11-04')""",
            (name, name, name, name.encode()),
        )
    ledger.db.commit()
    return datetime(2026, 11, 4, tzinfo=UTC)


def test_cookie_exact_and_plain_http_refusal():
    ident = "a" * 64
    cookie = sessions.set_cookie(ident)
    assert cookie == f"__Host-session={ident}; Path=/; Secure; HttpOnly; SameSite=Strict"
    assert sessions.clear_cookie() == "__Host-session=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict"
    assert sessions.from_cookie(cookie, secure=True) == ident
    assert sessions.from_cookie("junk=a b; " + cookie, secure=True) == ident
    for header, secure in [
        (cookie, False),
        (cookie + "; __Host-session=" + ident, True),
        ("session=" + ident, True),
        ("__Host-session=bad", True),
        ('__Host-session="' + ident + '"', True),
        ("__Host-session; __Host-session=" + ident, True),
    ]:
        assert sessions.from_cookie(header, secure=secure) == ""


@pytest.mark.parametrize(
    "agent, name",
    [
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
            "Chrome pe Linux",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36 Edg/140.0",
            "Edge pe Windows",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Safari/604.1",
            "Safari pe iOS",
        ),
        ("Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36", "Chrome pe Android"),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:130.0) Gecko/20100101 Firefox/130.0", "Firefox pe macOS"),
        (
            "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36 EdgA/140.0",
            "Edge pe Android",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 EdgiOS/140.0 Safari/604.1",
            "Edge pe iOS",
        ),
        ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chro", "Linux"),
        ("curl/8.0", "Dispozitiv necunoscut"),
        ("", "Dispozitiv necunoscut"),
    ],
)
def test_sessions_are_named_by_browser_and_system(ledger, users, agent, name):
    sessions.issue(ledger, "first", users, agent=agent)
    assert [s["device"] for s in account_users.active_sessions(ledger, "first", users)] == [name]
    assert agent not in str(account_users.active_sessions(ledger, "first", users)) or not agent


def test_session_hash_rotation_expiry_and_stepup(ledger, users):
    at = users
    first = sessions.issue(ledger, "first", at, agent="x" * 300)
    second = sessions.issue(ledger, "first", at)
    assert first != second
    assert sessions.identify(ledger, "b" * 64, at) is None
    row = sessions.identify(ledger, first, at)
    assert row["id_hash"] == sessions.digest(first) and first not in str(row)
    assert len(row["user_agent"]) == 200
    assert sessions.recent(row, at + timedelta(minutes=9))
    assert not sessions.recent(row, at + timedelta(minutes=10))
    assert not sessions.recent(row, at - timedelta(seconds=1))
    assert sessions.identify(ledger, first, at + timedelta(days=14)) is None
    # Daily usage extends idle expiry, but never absolute expiry.
    for day in range(1, 30):
        assert sessions.identify(ledger, second, at + timedelta(days=day))
    assert sessions.identify(ledger, second, at + timedelta(days=30)) is None


def test_revocation_disable_demote_and_cross_user(ledger, users):
    ident = sessions.issue(ledger, "first", users)
    row = sessions.identify(ledger, ident, users)
    assert not ledger.revoke_session(row["id_hash"], "second", sessions.stamp(users))
    assert sessions.identify(ledger, ident, users)
    ledger.db.execute("UPDATE users SET role='readonly' WHERE id='first'")
    ledger.db.commit()
    assert sessions.identify(ledger, ident, users)["role"] == "readonly"
    assert ledger.revoke_session(row["id_hash"], "first", sessions.stamp(users))
    assert sessions.identify(ledger, ident, users) is None
    ident = sessions.issue(ledger, "first", users, recovery=True)
    assert not sessions.recent(sessions.identify(ledger, ident, users), users)
    ledger.db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='first'")
    ledger.db.commit()
    assert sessions.identify(ledger, ident, users) is None
    with pytest.raises(PermissionError):
        sessions.issue(ledger, "first", users)


def test_idle_touch_once_per_minute_and_busy_read(ledger, users):
    ident = sessions.issue(ledger, "first", users)
    assert sessions.identify(ledger, ident, users + timedelta(seconds=59))["last_seen_at"] == sessions.stamp(users)
    updated = users + timedelta(minutes=1)
    assert sessions.identify(ledger, ident, updated)["last_seen_at"] == sessions.stamp(updated)
    other = Ledger(ledger.path)
    try:
        with other.tx():
            row = sessions.identify(ledger, ident, users + timedelta(minutes=2))
            assert row and row["last_seen_at"] == sessions.stamp(updated)
    finally:
        other.close()
    assert ledger.db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_session_refuses_invalid_id_and_naive_clock(ledger, users):
    assert sessions.identify(ledger, "invalid", users) is None
    with pytest.raises(ValueError):
        sessions.set_cookie("invalid")
    with pytest.raises(ValueError):
        sessions.issue(ledger, "first", users.replace(tzinfo=None))


def test_session_touch_propagates_storage_failures(ledger, users):
    ident = sessions.issue(ledger, "first", users)
    readonly = Ledger(ledger.path, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError) as error:
            sessions.identify(readonly, ident, users + timedelta(minutes=1))
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    finally:
        readonly.close()


def test_retention_preserves_active_access_and_security_audit(ledger, users, monkeypatch, capsys):
    at, old = users, users - timedelta(days=90)
    live = sessions.issue(ledger, "first", at)
    sessions.issue(ledger, "first", at - timedelta(days=31))
    idle = sessions.issue(ledger, "first", at - timedelta(days=15))
    revoked = sessions.issue(ledger, "first", at)
    ledger.revoke_session(sessions.digest(revoked), "first", sessions.stamp(at))
    current_ticket = tickets.issue(ledger, "first", "signin", at)
    used_ticket = tickets.issue(ledger, "first", "signin", at)
    tickets.issue(ledger, "first", "signin", at - timedelta(minutes=6))
    ledger.db.execute(
        "UPDATE auth_tickets SET used_at=? WHERE id_hash=?", (sessions.stamp(at), sessions.digest(used_ticket))
    )
    live_link = setup_links.issue(ledger, "first", at, actor_id=None).partition("#")[2]
    setup_links.issue(ledger, "second", at - timedelta(days=4), actor_id=None)
    for when in (old, old + timedelta(seconds=1)):
        for kind in ("failed_signin", "signin"):
            ledger.db.execute(
                "INSERT INTO auth_events(at,kind,actor,ip) VALUES(?,?,?,?)",
                (sessions.stamp(when), kind, "anonymous", "192.0.2.1"),
            )
    ledger.db.commit()
    with pytest.raises(ValueError), ledger.tx():
        maintenance.sweep(ledger, at)

    class Clock:
        @staticmethod
        def now(_):
            return at

    monkeypatch.setattr(user_cli, "datetime", Clock)
    assert cli.main(["auth", "sweep"]) == 0
    assert "complete" in capsys.readouterr().out
    assert sessions.identify(ledger, live, at) and sessions.identify(ledger, idle, at) is None
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    assert tickets.attempt(ledger, current_ticket, "signin", at)
    assert ledger.db.execute("SELECT count(*) FROM auth_tickets").fetchone()[0] == 1
    assert setup_links.inspect(ledger, live_link, at)
    assert ledger.db.execute("SELECT count(*) FROM setup_links").fetchone()[0] == 1
    assert [
        tuple(r) for r in ledger.db.execute("SELECT kind,ip FROM auth_events WHERE actor='anonymous' ORDER BY at,kind")
    ] == [("signin", None), ("failed_signin", "192.0.2.1"), ("signin", "192.0.2.1")]


def test_retention_without_restored_secrets_preserves_active_session(ledger, users, tmp_path, monkeypatch):
    root = tmp_path / "missing-secrets"
    monkeypatch.setenv("FISCALM_SECRETS", str(root))
    current = sessions.issue(ledger, "first", users)
    sessions.issue(ledger, "first", users - timedelta(days=31))
    maintenance.sweep(ledger, users)
    assert not root.exists()
    assert sessions.identify(ledger, current, users)
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
