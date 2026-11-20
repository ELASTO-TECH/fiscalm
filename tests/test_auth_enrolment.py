"""Setup links and recovery sessions authorize only their own atomic enrolment flow."""

import base64
import sqlite3
from contextlib import closing
from datetime import timedelta
from functools import partial

import pytest
from test_api import _get_json, server as server  # noqa: F401
from test_auth_sessions import users as users  # noqa: F401
from test_auth_signin import auth as auth, auth_request, browser as browser  # noqa: F401
from test_auth_totp_store import hasher as hasher, private_seeds as private_seeds  # noqa: F401

from fiscalm.api import (
    enrolment,
    passwords,
    recovery_codes,
    sessions,
    setup_links,
    signin,
    tickets,
    totp,
    totp_store,
    users as account_users,
)
from fiscalm.ledger import Ledger


def code(pending, at):
    return totp.code(base64.b32decode(pending.body["secret"]), int(at.timestamp()) // 30)


def test_setup_link_binds_own_ticket_and_commits_wrong_code(ledger, users, hasher):
    engine = enrolment.Enrolment(lambda: users, hasher)
    fragment = setup_links.issue(ledger, "first", users, actor_id=None).partition("#")[2]
    foreign = setup_links.issue(ledger, "second", users, actor_id=None).partition("#")[2]
    assert engine.begin_setup(ledger, "invalid", "synthetic-password").status == 401
    pending = engine.begin_setup(ledger, fragment, "synthetic-password")
    assert pending.status == 200 and pending.session_id is None
    ticket, valid = pending.body["ticket"], code(pending, users)
    assert engine.finish_setup(ledger, foreign, ticket, valid).body["message"] == signin.FAILED
    assert engine.finish_setup(ledger, fragment, "bad", valid).body["message"] == signin.FAILED
    wrong = "000000" if valid != "000000" else "111111"
    refused = engine.finish_setup(ledger, fragment, ticket, wrong)
    assert refused.status == 401 and refused.body["message"] == signin.WRONG_CODE
    assert ledger.db.execute("SELECT attempts FROM auth_tickets").fetchone()[0] == 1
    assert setup_links.inspect(ledger, fragment, users)
    result = engine.finish_setup(ledger, fragment, ticket, valid)
    assert result.status == 200 and len(result.body["codes"]) == 10
    assert sessions.identify(ledger, result.session_id, users)["user_id"] == "first"
    assert engine.finish_setup(ledger, fragment, ticket, valid).status == 401
    assert engine.begin_setup(ledger, fragment, "synthetic-password").status == 401


def test_setup_code_after_its_last_attempt_asks_to_start_again(ledger, users, hasher):
    engine = enrolment.Enrolment(lambda: users, hasher)
    fragment = setup_links.issue(ledger, "first", users, actor_id=None).partition("#")[2]
    pending = engine.begin_setup(ledger, fragment, "synthetic-password")
    wrong = "000000" if code(pending, users) != "000000" else "111111"
    messages = [engine.finish_setup(ledger, fragment, pending.body["ticket"], wrong).body["message"] for _ in range(6)]
    assert messages == [signin.WRONG_CODE] * 4 + [enrolment.EXPIRED] * 2
    assert setup_links.inspect(ledger, fragment, users)


def test_setup_supersession_expiry_and_failed_final_write_roll_back(ledger, users, hasher, monkeypatch):
    at = [users]
    engine = enrolment.Enrolment(lambda: at[0], hasher)
    old = setup_links.issue(ledger, "first", users, actor_id=None).partition("#")[2]
    pending = engine.begin_setup(ledger, old, "synthetic-password")
    fresh = setup_links.issue(ledger, "first", users, actor_id=None).partition("#")[2]
    assert engine.finish_setup(ledger, fresh, pending.body["ticket"], code(pending, users)).status == 401
    assert engine.finish_setup(ledger, old, pending.body["ticket"], code(pending, users)).status == 401
    pending = engine.begin_setup(ledger, fresh, "synthetic-password")

    def fail(*args):
        raise RuntimeError("synthetic final-write failure")

    monkeypatch.setattr(recovery_codes, "generate", fail)
    with pytest.raises(RuntimeError):
        engine.finish_setup(ledger, fresh, pending.body["ticket"], code(pending, users))
    assert setup_links.inspect(ledger, fresh, users)
    assert ledger.db.execute("SELECT totp_version FROM users WHERE id='first'").fetchone()[0] is None
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    at[0] += timedelta(hours=72)
    assert engine.finish_setup(ledger, fresh, pending.body["ticket"], "123456").status == 401


@pytest.mark.parametrize("busy", [False, True])
def test_setup_password_hash_failures_leave_link_unused(ledger, users, hasher, monkeypatch, busy):
    engine = enrolment.Enrolment(lambda: users, hasher)
    fragment = setup_links.issue(ledger, "first", users, actor_id=None).partition("#")[2]

    def fail(_):
        raise passwords.PasswordBusy() if busy else ValueError("synthetic invalid password")

    monkeypatch.setattr(hasher, "hash", fail)
    result = engine.begin_setup(ledger, fragment, "synthetic-password")
    assert result.status == (503 if busy else 400)
    assert busy or result.body["message"] == "synthetic invalid password"  # The rule reaches the screen.
    assert setup_links.inspect(ledger, fragment, users)
    assert not ledger.db.execute("SELECT 1 FROM auth_tickets").fetchone()


def test_recovery_replaces_totp_preserves_password_and_closes_every_session(ledger, auth, hasher, monkeypatch):
    service, at = auth
    engine = enrolment.Enrolment(lambda: at[0], hasher)
    codes = recovery_codes.generate(ledger, "first", at[0])
    ticket = service.recover(ledger, "first", codes[0]).body["ticket"]
    signed = service.password(ledger, "first", "synthetic-password", recovery=ticket)
    principal = sessions.identify(ledger, signed.session_id, at[0])
    before = dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone())
    pending = engine.begin_recovery(ledger, principal)
    assert pending.status == 200
    for invalid in ("bad", "x\ud800"):
        assert engine.finish_recovery(ledger, principal, invalid, "123456").status == 401
    wrong = "000000" if code(pending, at[0]) != "000000" else "111111"
    assert engine.finish_recovery(ledger, principal, pending.body["ticket"], wrong).status == 401
    assert (
        ledger.db.execute(
            "SELECT attempts FROM auth_tickets WHERE purpose='totp_enroll' AND used_at IS NULL"
        ).fetchone()[0]
        == 1
    )
    foreign = sessions.identify(ledger, sessions.issue(ledger, "second", at[0], recovery=True), at[0])
    assert engine.finish_recovery(ledger, foreign, pending.body["ticket"], code(pending, at[0])).status == 401
    normal = sessions.identify(ledger, sessions.issue(ledger, "first", at[0]), at[0])
    assert engine.begin_recovery(ledger, normal).status == 401
    with monkeypatch.context() as patch:

        def fail(*args):
            raise RuntimeError("synthetic recovery final-write failure")

        patch.setattr(recovery_codes, "generate", fail)
        with pytest.raises(RuntimeError):
            engine.finish_recovery(ledger, principal, pending.body["ticket"], code(pending, at[0]))
    assert ledger.db.execute("SELECT totp_version FROM users WHERE id='first'").fetchone()[0] == before["totp_version"]
    assert sessions.lookup(ledger, principal["id_hash"], at[0])
    result = engine.finish_recovery(ledger, principal, pending.body["ticket"], code(pending, at[0]))
    assert result.status == 200 and not result.clear_session
    assert len(result.body["codes"]) == 10 and set(result.body["codes"]).isdisjoint(codes)
    assert sessions.identify(ledger, signed.session_id, at[0]) is None
    assert sessions.lookup(ledger, normal["id_hash"], at[0]) is None
    fresh = sessions.identify(ledger, result.session_id, at[0])
    assert fresh["method"] == "password_totp" and not fresh["recovery_only"] and sessions.recent(fresh, at[0])
    assert (
        ledger.db.execute("SELECT count(*) FROM sessions WHERE user_id='first' AND revoked_at IS NULL").fetchone()[0]
        == 1
    )
    after = ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone()
    assert after["password_hash"] == before["password_hash"] and after["totp_version"] != before["totp_version"]
    assert engine.begin_recovery(ledger, principal).status == 401
    assert engine.finish_recovery(ledger, principal, pending.body["ticket"], "123456").status == 401
    at[0] += timedelta(seconds=30)
    assert service.password(ledger, "first", "wrong-password").status == 401
    ticket = service.password(ledger, "first", "synthetic-password").body["ticket"]
    assert service.factor(ledger, ticket, code(pending, at[0])).status == 200


def test_http_setup_link_wrong_code_cross_user_expiry_and_single_use(ledger, browser):
    server, service, at = browser
    previous = "__Host-session=" + sessions.issue(ledger, "first", at[0])
    owner = account_users.create(ledger, "new-account", "Cont nou", "member", at[0], actor_id=None)
    link = setup_links.issue(ledger, owner, at[0], actor_id=None).partition("#")[2]
    foreign = setup_links.issue(ledger, "second", at[0], actor_id=None, reset=True).partition("#")[2]
    assert auth_request(server, "/auth/setup/inspect", {"link": link})[2]["user"]["username"] == "new-account"
    status, headers, pending = auth_request(
        server, "/auth/setup/begin", {"link": link, "password": "synthetic-password"}
    )
    assert status == 200 and "set-cookie" not in headers
    valid = totp.code(base64.b32decode(pending["secret"]), int(at[0].timestamp()) // 30)
    body = {"link": link, "ticket": pending["ticket"], "code": valid}
    assert auth_request(server, "/auth/setup/finish", body | {"role": "admin"})[0] == 400
    assert auth_request(server, "/auth/setup/finish", body | {"link": foreign})[0] == 401
    wrong = "000000" if valid != "000000" else "111111"
    assert auth_request(server, "/auth/setup/finish", body | {"code": wrong}, Cookie=previous)[0] == 401
    assert _get_json(server, "/web/api/me", Cookie=previous)[0] == 200
    assert (
        ledger.db.execute(
            "SELECT attempts FROM auth_tickets WHERE id_hash=?", (sessions.digest(pending["ticket"]),)
        ).fetchone()[0]
        == 1
    )
    assert setup_links.inspect(ledger, link, at[0])
    status, headers, result = auth_request(server, "/auth/setup/finish", body, Cookie=previous)
    cookie = headers["set-cookie"].split(";", 1)[0]
    assert status == 200 and len(result["codes"]) == 10
    assert cookie != previous and _get_json(server, "/web/api/me", Cookie=previous)[0] == 401
    assert _get_json(server, "/web/api/me", Cookie=cookie)[1]["me"]["role"] == "member"
    assert auth_request(server, "/auth/setup/finish", body)[0] == 401
    assert auth_request(server, "/auth/setup/inspect", {"link": link})[0] == 401
    expired = setup_links.issue(ledger, "second", at[0] - timedelta(hours=73), actor_id=None).partition("#")[2]
    assert auth_request(server, "/auth/setup/begin", {"link": expired, "password": "synthetic-password"})[0] == 401


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
def test_password_change_hashes_outside_lock_and_preserves_only_current_session(ledger, browser, monkeypatch, role):
    server, service, at = browser
    ledger.db.execute("UPDATE users SET role=? WHERE id='first'", (role,))
    ledger.db.commit()
    current = sessions.issue(ledger, "first", at[0])
    cookie = "__Host-session=" + current
    extra = sessions.issue(ledger, "first", at[0])
    ticket = service.password(ledger, "first", "synthetic-password").body["ticket"]
    previous = dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone())

    def hash_password(password):
        assert password == "replacement-password"
        with closing(sqlite3.connect(ledger.path, timeout=0.01)) as other, other:
            other.execute("BEGIN IMMEDIATE")  # No engine connection may hold the write lock during hashing.
        return "replacement-synthetic-hash"

    monkeypatch.setattr(service.hasher, "hash", hash_password)
    status, headers, result = auth_request(
        server, "/auth/me/password-change", {"password": "replacement-password"}, Cookie=cookie
    )
    assert status == 200 and result["ok"] and "set-cookie" not in headers
    changed = ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone()
    assert (
        changed["password_hash"] == "replacement-synthetic-hash" and changed["totp_version"] == previous["totp_version"]
    )
    assert sessions.identify(ledger, current, at[0]) and not sessions.identify(ledger, extra, at[0])
    assert tickets.attempt(ledger, ticket, "signin", at[0]) is None
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='password_changed'").fetchone()[0] == 1
    assert "replacement-password" not in str(list(map(tuple, ledger.db.execute("SELECT * FROM auth_events"))))


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
def test_totp_replacement_binds_session_keeps_old_factor_until_verified_and_rotates_codes(ledger, browser, role):
    server, service, at = browser
    ledger.db.execute("UPDATE users SET role=? WHERE id='first'", (role,))
    ledger.db.commit()
    current = sessions.issue(ledger, "first", at[0])
    cookie = "__Host-session=" + current
    extra = sessions.issue(ledger, "first", at[0])
    foreign = sessions.issue(ledger, "second", at[0])
    old = dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone())
    old_codes = recovery_codes.generate(ledger, "first", at[0])
    status, _, pending = auth_request(server, "/auth/me/totp/begin", {}, Cookie=cookie)
    assert status == 200 and pending["uri"].startswith("otpauth://")
    valid = totp.code(base64.b32decode(pending["secret"]), int(at[0].timestamp()) // 30)
    body = {"ticket": pending["ticket"], "code": valid}
    assert auth_request(server, "/auth/me/totp/finish", body | {"ticket": "bad"}, Cookie=cookie)[0] == 401
    for other in (extra, foreign):
        assert auth_request(server, "/auth/me/totp/finish", body, Cookie="__Host-session=" + other)[0] == 401
    wrong = "000000" if valid != "000000" else "111111"
    assert auth_request(server, "/auth/me/totp/finish", body | {"code": wrong}, Cookie=cookie)[0] == 401
    assert ledger.db.execute("SELECT totp_version FROM users WHERE id='first'").fetchone()[0] == old["totp_version"]
    assert (
        ledger.db.execute(
            "SELECT attempts FROM auth_tickets WHERE id_hash=?", (sessions.digest(pending["ticket"]),)
        ).fetchone()[0]
        == 1
    )
    from_old = service.password(ledger, "first", "synthetic-password").body["ticket"]
    previous_code = totp.code(totp_store.seed("first", old["totp_version"]), int(at[0].timestamp()) // 30)
    assert service.factor(ledger, from_old, previous_code).status == 200
    status, headers, result = auth_request(server, "/auth/me/totp/finish", body, Cookie=cookie)
    assert status == 200 and len(result["codes"]) == 10 and "set-cookie" not in headers
    changed = ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone()
    assert changed["password_hash"] == old["password_hash"] and changed["totp_version"] != old["totp_version"]
    assert sessions.identify(ledger, current, at[0]) and sessions.identify(ledger, foreign, at[0])
    assert not sessions.identify(ledger, extra, at[0])
    assert not ledger.consume_totp("first", old["totp_version"], int(at[0].timestamp()) // 30 + 1)
    assert all(not recovery_codes.consume(ledger, "first", value, at[0]) for value in old_codes)
    assert auth_request(server, "/auth/me/totp/finish", body, Cookie=cookie)[0] == 401
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='factor_removed'").fetchone()[0] == 1
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='password_changed'").fetchone()[0] == 0


@pytest.mark.parametrize("case", ["changed", "revoked", "stale", "busy", "invalid"])
def test_password_change_refuses_late_authority_credentials_and_hash_failures(ledger, browser, monkeypatch, case):
    server, service, at = browser
    raw = sessions.issue(ledger, "first", at[0])
    user = dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone())

    def hash_password(password):
        if case == "busy":
            raise passwords.PasswordBusy()
        if case == "invalid":
            raise ValueError("synthetic invalid password")
        if case == "stale":
            at[0] += timedelta(minutes=11)
        else:
            with closing(Ledger(ledger.path)) as other:
                if case == "changed":
                    other.change_password(user, "first-change-hash", sessions.digest(raw), sessions.stamp(at[0]))
                else:
                    other.revoke_session(sessions.digest(raw), "first", sessions.stamp(at[0]))
        return "refused-hash"

    monkeypatch.setattr(service.hasher, "hash", hash_password)
    status, headers, answer = auth_request(
        server, "/auth/me/password-change", {"password": "replacement-password"}, Cookie="__Host-session=" + raw
    )
    assert status == (503 if case == "busy" else 400 if case == "invalid" else 401)
    assert bool(answer.get("reauth")) == (case == "stale")
    assert ledger.db.execute("SELECT password_hash FROM users WHERE id='first'").fetchone()[0] == (
        "first-change-hash" if case == "changed" else user["password_hash"]
    )
    if case == "busy":
        assert headers["retry-after"] == "5"


@pytest.mark.parametrize("field", ["password_hash", "totp_version"])
def test_totp_replacement_refuses_a_missing_signin_pair(ledger, browser, field):
    server, service, at = browser
    cookie = "__Host-session=" + sessions.issue(ledger, "first", at[0])
    ledger.db.execute("UPDATE users SET " + field + "=NULL WHERE id='first'")
    ledger.db.commit()
    assert auth_request(server, "/auth/me/totp/begin", {}, Cookie=cookie)[0] == 401


def test_totp_replacement_expiry_and_final_failure_preserve_the_old_factor(ledger, browser, monkeypatch):
    server, service, at = browser
    cookie = "__Host-session=" + sessions.issue(ledger, "first", at[0])
    extra = sessions.issue(ledger, "first", at[0])
    old = dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone())
    pending = auth_request(server, "/auth/me/totp/begin", {}, Cookie=cookie)[2]
    valid = totp.code(base64.b32decode(pending["secret"]), int(at[0].timestamp()) // 30)
    body = {"ticket": pending["ticket"], "code": valid}
    original = recovery_codes.generate

    def fail(*args):
        original(*args)
        raise PermissionError("synthetic final-write failure")

    with monkeypatch.context() as patch:
        patch.setattr(recovery_codes, "generate", fail)
        assert auth_request(server, "/auth/me/totp/finish", body, Cookie=cookie)[0] == 401
    assert dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone()) == old
    assert sessions.identify(ledger, extra, at[0])
    assert (
        ledger.db.execute(
            "SELECT used_at FROM auth_tickets WHERE id_hash=?", (sessions.digest(pending["ticket"]),)
        ).fetchone()[0]
        is None
    )
    at[0] += timedelta(minutes=5)
    assert auth_request(server, "/auth/me/totp/finish", body, Cookie=cookie)[0] == 401
    assert dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone()) == old


@pytest.mark.parametrize("flow", ["signin", "setup"])
def test_presented_session_replacement_rolls_back_with_failed_final_write(ledger, browser, monkeypatch, flow):
    server, service, at = browser
    old = sessions.issue(ledger, "second", at[0])
    presented = sessions.identify(ledger, old, at[0])
    if flow == "signin":
        ticket = service.password(ledger, "first", "synthetic-password").body["ticket"]
        user = ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone()
        valid = totp.code(totp_store.seed("first", user["totp_version"]), int(at[0].timestamp()) // 30)
        finish = partial(service.factor, ledger, ticket, valid, presented=presented)
    else:
        owner = account_users.create(ledger, "new-account", "Cont nou", "member", at[0], actor_id=None)
        link = setup_links.issue(ledger, owner, at[0], actor_id=None).partition("#")[2]
        engine = server.service.browser_auth.enrolment
        pending = engine.begin_setup(ledger, link, "synthetic-password")
        finish = partial(
            engine.finish_setup, ledger, link, pending.body["ticket"], code(pending, at[0]), presented=presented
        )
    before = ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
    original = ledger.record_signin

    def fail(*args):
        raise RuntimeError("synthetic final-write failure")

    monkeypatch.setattr(ledger, "record_signin", fail)
    with pytest.raises(RuntimeError):
        finish()
    assert sessions.identify(ledger, old, at[0])
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == before
    monkeypatch.setattr(ledger, "record_signin", original)
    result = finish()
    assert result.status == 200 and sessions.identify(ledger, result.session_id, at[0])
    assert sessions.identify(ledger, old, at[0]) is None
