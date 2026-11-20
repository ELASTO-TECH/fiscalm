"""Complete password+second-factor flows against real SQLite and versioned TOTP seeds."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
from test_api import _api, _get_json, _req, server as server  # noqa: F401
from test_auth_sessions import users as users  # noqa: F401
from test_auth_totp_store import hasher as hasher, private_seeds as private_seeds  # noqa: F401

from fiscalm.api import (
    enrolment,
    limits,
    passwords,
    recovery_codes,
    sessions,
    setup_links,
    signin,
    tickets,
    totp,
    totp_store,
)
from fiscalm.ledger import Ledger


@pytest.fixture
def auth(ledger, users, hasher, monkeypatch):
    for owner in ("first", "second"):
        pending = totp_store.begin(ledger, owner, "synthetic-password", hasher, users)
        code = totp.code(base64.b32decode(pending["secret"]), int(users.timestamp()) // 30)
        assert totp_store.finish(ledger, pending["ticket"], code, users)
    at = [users + timedelta(seconds=30)]
    monkeypatch.setattr(
        hasher, "verify", lambda supplied, encoded: (supplied == "synthetic-password" and encoded is not None, False)
    )
    return signin.Signin(lambda: at[0], hasher), at


def otp(ledger, at, owner="first"):
    version = ledger.db.execute("SELECT totp_version FROM users WHERE id=?", (owner,)).fetchone()[0]
    return totp.code(totp_store.seed(owner, version), int(at.timestamp()) // 30)


def password_ticket(ledger, service, *, principal=None):
    result = service.password(ledger, "first", "synthetic-password", principal=principal)
    assert result.status == 200 and result.session_id is None
    return result.body["ticket"]


@pytest.mark.parametrize("previous_owner", ["first", "second"])
def test_passkey_completion_preserves_shared_rotation_limits_and_audit(ledger, users, previous_owner):
    previous = sessions.issue(ledger, previous_owner, users)
    untouched = sessions.issue(ledger, previous_owner, users)
    principal = sessions.identify(ledger, previous, users)
    user = dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone())
    limits.failure(ledger, "first", users, reason="passkey")
    result = signin.complete(ledger, user, users, method="passkey", previous=principal)
    current = sessions.identify(ledger, result.session_id, users)
    assert current["method"] == "passkey" and sessions.recent(current, users) and not current["recovery_only"]
    assert sessions.identify(ledger, previous, users) is None and sessions.identify(ledger, untouched, users)
    assert result.session_id not in str(result.body)
    assert not ledger.db.execute("SELECT 1 FROM auth_limits").fetchone()
    event = ledger.db.execute("SELECT actor,detail FROM auth_events WHERE kind='signin'").fetchone()
    assert event["actor"] == "user first" and json.loads(event["detail"]) == {"method": "passkey"}


@pytest.mark.parametrize("failure", ["disabled", "method", "audit"])
def test_passkey_completion_refusal_does_not_revoke_the_presented_cookie(ledger, users, monkeypatch, failure):
    previous = sessions.issue(ledger, "second", users)
    principal = sessions.identify(ledger, previous, users)
    user = dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone())
    if failure == "disabled":
        with ledger.tx() as db:
            db.execute("UPDATE users SET disabled_at=? WHERE id='first'", (sessions.stamp(users),))
    if failure == "audit":

        def fault(*args):
            raise ValueError("synthetic sign-in audit failure")

        monkeypatch.setattr(ledger, "record_signin", fault)
    with pytest.raises((PermissionError, ValueError)):
        signin.complete(
            ledger, user, users, method="password" if failure == "method" else "passkey", previous=principal
        )
    assert sessions.identify(ledger, previous, users)
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    assert not ledger.db.execute("SELECT 1 FROM auth_events WHERE kind='signin'").fetchone()


def test_password_never_alone_wrong_totp_replay_and_new_session(ledger, auth):
    service, at = auth
    ticket = password_ticket(ledger, service)
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    correct = otp(ledger, at[0])
    wrong = "000000" if correct != "000000" else "111111"
    assert service.factor(ledger, ticket, wrong).status == 401
    assert ledger.db.execute("SELECT failures FROM users WHERE id='first'").fetchone()[0] == 1
    result = service.factor(ledger, ticket, correct)
    assert result.status == 200 and result.session_id not in str(result.body)
    assert sessions.identify(ledger, result.session_id, at[0])
    assert sessions.identify(ledger, "a" * 64, at[0]) is None
    assert service.factor(ledger, ticket, correct).status == 401
    assert service.factor(ledger, password_ticket(ledger, service), correct).status == 401
    at[0] += timedelta(seconds=30)
    fresh = service.factor(ledger, password_ticket(ledger, service), otp(ledger, at[0]))
    assert fresh.status == 200 and fresh.session_id != result.session_id
    events = str([tuple(r) for r in ledger.db.execute("SELECT detail FROM auth_events")])
    assert "synthetic-password" not in events and result.session_id not in events


@pytest.mark.parametrize("case", ["wrong", "unknown", "disabled", "locked", "surrogate"])
def test_same_generic_refusal_and_kdf_path(ledger, auth, monkeypatch, case):
    service, at = auth
    name, supplied = "first", "wrong-password"
    if case == "unknown":
        name = "missing"
    elif case == "surrogate":
        name = "x\ud800"
    elif case == "disabled":
        ledger.db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='first'")
        ledger.db.commit()
    elif case == "locked":
        for _ in range(20):
            limits.failure(ledger, name, at[0], reason="password")
    calls = []
    original = service.hasher.verify
    monkeypatch.setattr(service.hasher, "verify", lambda *args: calls.append(args[1]) or original(*args))
    result = service.password(ledger, name, supplied)
    assert result.status == 401 and result.body == dict(ok=False, message=signin.FAILED)
    assert result.session_id is None and len(calls) == 1
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_wrong_factor_budget_persists_and_password_does_not_clear_it(ledger, auth):
    service, at = auth
    ticket = password_ticket(ledger, service)
    wrong = "000000" if otp(ledger, at[0]) != "000000" else "111111"
    refused = [service.factor(ledger, ticket, wrong) for _ in range(5)]
    # The ticket proves the password, so only its holder learns the code was wrong; the spent ticket says nothing.
    assert [(r.status, r.body["message"]) for r in refused] == [(401, signin.WRONG_CODE)] * 4 + [(401, signin.FAILED)]
    assert service.factor(ledger, ticket, otp(ledger, at[0])).body == dict(ok=False, message=signin.FAILED)
    assert service.password(ledger, "first", "synthetic-password").status == 429
    at[0] += timedelta(seconds=2)
    password_ticket(ledger, service)
    assert ledger.db.execute("SELECT failures FROM users WHERE id='first'").fetchone()[0] == 5


def test_same_totp_two_connections_create_one_session(ledger, auth):
    service, at = auth
    pending = [password_ticket(ledger, service) for _ in range(2)]
    code, barrier = otp(ledger, at[0]), Barrier(2)

    def authenticate(ticket):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return service.factor(other, ticket, code).status
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(authenticate, pending)) == [200, 401]
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def recovered(ledger, service, code, password="synthetic-password"):
    """The one recovery entry: the code first, then the password of a password account."""
    ticket = service.recover(ledger, "first", code)
    assert ticket.status == 200 and ticket.session_id is None and set(ticket.body) == {"ok", "ticket"}
    return service.password(ledger, "first", password, recovery=ticket.body["ticket"])


def test_recovery_needs_code_then_password_closes_sessions_and_cannot_step_up(ledger, auth):
    service, at = auth
    old = service.factor(ledger, password_ticket(ledger, service), otp(ledger, at[0]))
    codes = recovery_codes.generate(ledger, "first", at[0])
    assert recovered(ledger, service, codes[0], "wrong-password").status == 401
    assert sessions.identify(ledger, old.session_id, at[0])
    result = recovered(ledger, service, codes[1])
    assert result.status == 200 and result.body["recovery"] is True
    assert sessions.identify(ledger, old.session_id, at[0]) is None
    principal = sessions.identify(ledger, result.session_id, at[0])
    assert principal["recovery_only"] and principal["method"] == "recovery" and not sessions.recent(principal, at[0])
    assert service.password(ledger, "first", "synthetic-password", principal=principal).status == 401
    assert service.recover(ledger, "first", codes[1]).status == 401


@pytest.mark.parametrize("ticket", ["", "a" * 64, "signin", "other_user", "spent"])
def test_recovery_password_without_its_own_ticket_counts_as_a_wrong_password(ledger, auth, ticket):
    service, at = auth
    code = recovery_codes.generate(ledger, "first", at[0])[0]
    if ticket == "signin":
        ticket = password_ticket(ledger, service)
    elif ticket == "other_user":
        ticket = tickets.issue(ledger, "second", "recovery", at[0])
    elif ticket == "spent":
        ticket = service.recover(ledger, "first", code).body["ticket"]
        assert service.password(ledger, "first", "synthetic-password", recovery=ticket).status == 200
    before = ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
    result = service.password(ledger, "first", "synthetic-password", recovery=ticket or "x")
    assert result.status == 401 and result.body == dict(ok=False, message=signin.FAILED)
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == before
    assert ledger.db.execute("SELECT count(*) FROM auth_failure_window").fetchone()[0] == 1


def test_recovery_password_without_its_ticket_takes_the_dummy_hash_and_never_rehashes(
    ledger, auth, hasher, monkeypatch
):
    """Hash cost must not tell a forged ticket whether the account exists or how old its hash is."""
    service, at = auth
    checked, hashed = [], []
    monkeypatch.setattr(
        hasher, "verify", lambda s, e: checked.append(e) or (s == "synthetic-password" and e is not None, True)
    )
    monkeypatch.setattr(hasher, "hash", lambda supplied: hashed.append(supplied) or "synthetic-upgraded-hash")
    code = recovery_codes.generate(ledger, "first", at[0])[0]
    foreign = tickets.issue(ledger, "second", "recovery", at[0])
    for ticket in ("a" * 64, foreign, "not-a-ticket"):
        assert service.password(ledger, "first", "synthetic-password", recovery=ticket).status == 401
    assert checked == [None, None, None]
    assert recovered(ledger, service, code).status == 200 and checked[-1] is not None and hashed == []


@pytest.mark.parametrize("case", ["wrong", "unknown", "disabled", "unconfigured", "presented"])
def test_recovery_code_refusals_say_nothing_about_the_account(ledger, auth, case):
    service, at = auth
    code = recovery_codes.generate(ledger, "first", at[0])[0]
    name, presented = "missing" if case == "unknown" else "first", None
    if case == "disabled":
        ledger.change_user("first", None, sessions.stamp(at[0]), enabled=False)
    if case == "unconfigured":
        with ledger.tx() as db:
            db.execute("UPDATE users SET password_hash=NULL,totp_version=NULL WHERE id='first'")
    if case == "presented":
        presented = sessions.identify(ledger, sessions.issue(ledger, "second", at[0]), at[0])
    result = service.recover(ledger, name, "invalid" if case == "wrong" else code, presented=presented)
    assert (result.status, result.body, result.session_id) == (401, dict(ok=False, message=signin.FAILED), None)
    assert not ledger.db.execute("SELECT 1 FROM auth_tickets WHERE purpose='recovery'").fetchone()


def test_stepup_binds_owner_and_rotates_only_the_current_session(ledger, auth):
    service, at = auth
    initial = service.factor(ledger, password_ticket(ledger, service), otp(ledger, at[0]))
    principal = sessions.identify(ledger, initial.session_id, at[0])
    another = sessions.issue(ledger, "first", at[0])
    foreign = sessions.identify(ledger, sessions.issue(ledger, "second", at[0]), at[0])
    assert (
        service.password(ledger, "second", "synthetic-password", principal={**principal, "user_id": "second"}).status
        == 401
    )
    assert (
        service.password(ledger, "first", "synthetic-password", principal={**principal, "id_hash": "bad"}).status == 401
    )
    at[0] += timedelta(seconds=30)
    ticket = service.password(ledger, "second", "synthetic-password", principal=principal).body["ticket"]
    assert service.factor(ledger, ticket, otp(ledger, at[0]), principal=foreign).status == 401
    result = service.factor(ledger, ticket, otp(ledger, at[0]), principal=principal)
    assert result.status == 200 and result.session_id != initial.session_id
    assert sessions.identify(ledger, initial.session_id, at[0]) is None
    assert sessions.identify(ledger, another, at[0])
    assert sessions.recent(sessions.identify(ledger, result.session_id, at[0]), at[0])


@pytest.mark.parametrize("case", ["busy", "expired", "reset", "error"])
def test_kdf_outcome_never_leaves_a_usable_lease_or_stale_password_proof(ledger, auth, monkeypatch, case):
    service, at = auth

    def verify(*args):
        if case == "busy":
            raise passwords.PasswordBusy()
        if case == "error":
            raise RuntimeError("synthetic KDF failure")
        if case == "expired":
            at[0] += timedelta(seconds=11)
        else:
            setup_links.issue(ledger, "first", at[0], actor_id=None, reset=True)
        return True, False

    monkeypatch.setattr(service.hasher, "verify", verify)
    if case == "error":
        with pytest.raises(RuntimeError):
            service.password(ledger, "first", "synthetic-password")
    else:
        result = service.password(ledger, "first", "synthetic-password")
        assert result.status == (503 if case == "busy" else 401)
    assert limits.state(ledger, "first", at[0])["retry_after"] == 0
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_ticket_expiry_password_upgrade_and_atomic_guard_failure(ledger, auth, monkeypatch):
    service, at = auth
    expired = password_ticket(ledger, service)
    at[0] += timedelta(minutes=5)
    assert service.factor(ledger, expired, otp(ledger, at[0])).status == 401
    previous = dict(ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone())
    monkeypatch.setattr(service.hasher, "verify", lambda *args: (True, True))
    monkeypatch.setattr(service.hasher, "hash", lambda _: "upgraded-synthetic-hash")
    ticket = password_ticket(ledger, service)
    assert (
        ledger.db.execute("SELECT password_hash FROM users WHERE id='first'").fetchone()[0] == "upgraded-synthetic-hash"
    )
    with pytest.raises(PermissionError):
        ledger.rehash_password(previous, "stale-hash")
    with pytest.raises(ValueError):
        ledger.record_signin("first", "password", sessions.stamp(at[0]), "")
    original_step = ledger.db.execute("SELECT totp_step FROM users WHERE id='first'").fetchone()[0]
    monkeypatch.setattr(tickets, "consume", lambda *args: False)
    with pytest.raises(PermissionError):
        service.factor(ledger, ticket, otp(ledger, at[0]))
    assert ledger.db.execute("SELECT totp_step FROM users WHERE id='first'").fetchone()[0] == original_step
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


@pytest.fixture
def browser(server, auth):
    service, at = auth
    server.service.clock = service.clock
    engine = server.service.browser_auth
    engine.clock, engine.signin = service.clock, service
    engine.enrolment = enrolment.Enrolment(service.clock, service.hasher)
    assert engine.signin.hasher is engine.enrolment.hasher
    return server, service, at


def auth_request(server, path, body, **headers):
    code, response_headers, raw = _req(
        server, "POST", path, json.dumps(body).encode(), {"Content-Type": "application/json", "Cookie": None, **headers}
    )
    return code, response_headers, json.loads(raw)


def http_ticket(server, **headers):
    status, response, body = auth_request(
        server, "/auth/password", {"username": "first", "password": "synthetic-password"}, **headers
    )
    assert status == 200 and "set-cookie" not in response
    return body["ticket"]


@pytest.mark.parametrize("previous_owner", ["first", "second"])
def test_http_password_totp_refusals_fixation_and_signout(ledger, browser, previous_owner):
    server, service, at = browser
    previous = "__Host-session=" + sessions.issue(ledger, previous_owner, at[0])
    untouched = sessions.issue(ledger, previous_owner, at[0])
    before = ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0]
    ticket = http_ticket(server)
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == before
    valid = otp(ledger, at[0])
    wrong = "000000" if valid != "000000" else "111111"
    assert auth_request(server, "/auth/totp", {"ticket": ticket, "code": wrong}, Cookie=previous)[0] == 401
    assert _get_json(server, "/web/api/me", Cookie=previous)[0] == 200
    status, headers, body = auth_request(server, "/auth/totp", {"ticket": ticket, "code": valid}, Cookie=previous)
    cookie = headers["set-cookie"].split(";", 1)[0]
    assert status == 200 and "Path=/; Secure; HttpOnly; SameSite=Strict" in headers["set-cookie"]
    assert cookie.partition("=")[2] not in str(body) and cookie != previous
    assert _get_json(server, "/web/api/me?user_id=second", Cookie=cookie)[1]["me"]["id"] == "first"
    assert _get_json(server, "/web/api/me", Cookie=previous)[0] == 401
    assert sessions.identify(ledger, untouched, at[0])
    assert auth_request(server, "/auth/totp", {"ticket": ticket, "code": valid})[0] == 401
    assert auth_request(server, "/auth/totp", {"ticket": http_ticket(server), "code": valid})[0] == 401
    assert auth_request(server, "/auth/signout", {}, Cookie=cookie)[1]["set-cookie"] == sessions.clear_cookie()
    assert _get_json(server, "/web/api/me", Cookie=cookie)[0] == 401
    assert ledger.db.execute("SELECT actor FROM auth_events WHERE kind='signout'").fetchone()[0] == "user first"


@pytest.mark.parametrize("case", ["wrong", "unknown", "locked", "disabled"])
def test_http_account_refusals_are_indistinguishable(ledger, browser, case):
    server, service, at = browser
    name = "missing" if case == "unknown" else "first"
    if case == "locked":
        for _ in range(20):
            limits.failure(ledger, name, at[0], reason="password")
    elif case == "disabled":
        ledger.db.execute("UPDATE users SET disabled_at=? WHERE id='first'", (sessions.stamp(at[0]),))
        ledger.db.commit()
    status, headers, body = auth_request(server, "/auth/password", {"username": name, "password": "wrong-password"})
    assert status == 401 and body == dict(ok=False, message=signin.FAILED) and "set-cookie" not in headers


def test_http_stepup_binds_session_and_rotates_only_its_owner(ledger, browser):
    server, service, at = browser
    ledger.db.execute("UPDATE users SET role='admin' WHERE id='first'")
    ledger.db.commit()
    status, headers, _ = auth_request(server, "/auth/totp", {"ticket": http_ticket(server), "code": otp(ledger, at[0])})
    cookie = headers["set-cookie"].split(";", 1)[0]
    assert status == 200
    at[0] += timedelta(minutes=11)
    assert _api(server, "token-create", {"name": "test", "scope": "read"}, Cookie=cookie)[1]["reauth"]
    assert (
        auth_request(
            server, "/auth/step-up/password", {"username": "second", "password": "synthetic-password"}, Cookie=cookie
        )[0]
        == 400
    )
    ticket = auth_request(server, "/auth/step-up/password", {"password": "synthetic-password"}, Cookie=cookie)[2][
        "ticket"
    ]
    code = otp(ledger, at[0])
    assert (
        auth_request(
            server, "/auth/step-up/totp", {"ticket": ticket, "code": code}, Cookie="__Host-session=" + "b" * 64
        )[0]
        == 401
    )
    status, headers, _ = auth_request(server, "/auth/step-up/totp", {"ticket": ticket, "code": code}, Cookie=cookie)
    fresh = headers["set-cookie"].split(";", 1)[0]
    assert status == 200 and fresh != cookie
    assert _get_json(server, "/web/api/me", Cookie=cookie)[0] == 401
    assert _get_json(server, "/web/api/me", Cookie=fresh)[1]["me"]["id"] == "first"
    assert _api(server, "token-create", {"name": "test", "scope": "read"}, Cookie=fresh)[0] == 200


def test_http_recovery_is_restricted_and_finishes_signed_in(ledger, browser):
    server, service, at = browser
    old = "__Host-session=" + sessions.issue(ledger, "first", at[0])
    codes = recovery_codes.generate(ledger, "first", at[0])
    body = {"username": "first", "code": codes[0]}
    assert auth_request(server, "/auth/recover/code", body, Cookie=old)[0] == 401
    status, headers, answer = auth_request(server, "/auth/recover/code", body)
    assert status == 200 and "set-cookie" not in headers and set(answer) == {"ok", "ticket"}
    body = {"username": "first", "ticket": answer["ticket"], "password": "synthetic-password"}
    status, headers, answer = auth_request(server, "/auth/recover/password", body)
    cookie = headers["set-cookie"].split(";", 1)[0]
    assert status == 200 and answer["recovery"]
    assert _get_json(server, "/web/api/me", Cookie=cookie)[1]["auth"]["recovery_only"]
    assert _get_json(server, "/web/api/state?co=NORD", Cookie=cookie)[0] == 403
    assert auth_request(server, "/auth/step-up/password", {"password": "synthetic-password"}, Cookie=cookie)[0] == 401
    assert _get_json(server, "/web/api/me", Cookie=old)[0] == 401
    pending = auth_request(server, "/auth/recover/begin", {}, Cookie=cookie)[2]
    valid = totp.code(base64.b32decode(pending["secret"]), int(at[0].timestamp()) // 30)
    status, headers, answer = auth_request(
        server, "/auth/recover/finish", {"ticket": pending["ticket"], "code": valid}, Cookie=cookie
    )
    fresh = headers["set-cookie"].split(";", 1)[0]
    assert status == 200 and len(answer["codes"]) == 10 and fresh.partition("=")[2] not in str(answer)
    assert _get_json(server, "/web/api/me", Cookie=cookie)[0] == 401
    identity = _get_json(server, "/web/api/me", Cookie=fresh)[1]
    assert identity["auth"]["method"] == "password_totp" and not identity["auth"]["recovery_only"]


def test_http_ip_budget_is_shared_and_busy_hashing_is_a_503(ledger, browser, monkeypatch):
    server, service, at = browser
    for _ in range(20):
        assert auth_request(server, "/auth/setup/inspect", {"link": "invalid"})[0] == 401
    status, headers, _ = auth_request(server, "/auth/password", {"username": "first", "password": "synthetic-password"})
    assert status == 429 and int(headers["retry-after"]) > 0
    at[0] += timedelta(seconds=61)

    def busy(*args):
        raise passwords.PasswordBusy()

    monkeypatch.setattr(service.hasher, "verify", busy)
    status, headers, body = auth_request(
        server, "/auth/password", {"username": "first", "password": "synthetic-password"}
    )
    assert status == 503 and headers["retry-after"] == "5" and "ticket" not in body


def test_http_auth_refuses_unknown_endpoints_and_extra_fields(browser):
    server, service, at = browser
    assert auth_request(server, "/auth/unknown", {})[0] == 404
    assert (
        auth_request(
            server, "/auth/password", {"username": "first", "password": "synthetic-password", "role": "admin"}
        )[0]
        == 400
    )


@pytest.mark.parametrize("role,letter", [("admin", "a"), ("member", "b"), ("readonly", "c")])
def test_every_role_can_manage_only_its_own_sessions_and_codes(ledger, server, role, letter):
    owner, cookie = "u_" + role, "__Host-session=" + letter * 64
    other = "u_member" if role != "member" else "u_admin"
    at = server.service.clock()
    before_codes = recovery_codes.generate(ledger, owner, at)
    extra = sessions.issue(ledger, owner, at)
    me = _get_json(server, "/web/api/me?user_id=" + other, Cookie=cookie)[1]
    current = next(s["id"] for s in me["sessions"] if s["current"])
    assert me["me"]["id"] == owner and me["recovery_codes_remaining"] == 10
    assert len(me["sessions"]) == 2 and extra not in str(me)
    status, _, fresh = auth_request(server, "/auth/me/recovery-codes", {}, Cookie=cookie)
    assert status == 200 and len(set(fresh["codes"])) == 10
    assert all(not recovery_codes.consume(ledger, owner, code, at) for code in before_codes)
    foreign = ledger.db.execute(
        "SELECT id_hash FROM sessions WHERE user_id=? AND revoked_at IS NULL", (other,)
    ).fetchone()[0]
    assert auth_request(server, "/auth/me/session-close", {"id": foreign}, Cookie=cookie)[0] == 404
    assert sessions.lookup(ledger, foreign, at)
    own_extra = sessions.digest(extra)
    assert auth_request(server, "/auth/me/session-close", {"id": own_extra}, Cookie=cookie)[0] == 200
    assert auth_request(server, "/auth/me/session-close", {"id": own_extra}, Cookie=cookie)[0] == 404
    sessions.issue(ledger, owner, at)
    assert auth_request(server, "/auth/me/sessions-close-others", {}, Cookie=cookie)[2] == {"ok": True, "closed": 1}
    assert auth_request(server, "/auth/me/sessions-close-others", {}, Cookie=cookie)[2]["closed"] == 0
    assert sessions.lookup(ledger, current, at) and sessions.lookup(ledger, foreign, at)
    assert (
        auth_request(server, "/auth/me/session-close", {"id": current}, Cookie=cookie)[1]["set-cookie"]
        == sessions.clear_cookie()
    )
    assert _get_json(server, "/web/api/me", Cookie=cookie)[0] == 401
    events = str(list(map(tuple, ledger.db.execute("SELECT * FROM auth_events"))))
    assert all(code not in events and code.replace("-", "") not in events for code in fresh["codes"])
    assert extra not in events


@pytest.mark.parametrize("case", ["recovery", "stale", "revoked", "disabled"])
def test_own_account_refuses_recovery_stale_or_lost_sessions(ledger, server, case):
    at = server.service.clock()
    cookie = "__Host-session=" + sessions.issue(ledger, "u_member", at, recovery=case == "recovery")
    if case != "recovery":
        with ledger.tx() as db:
            if case == "stale":
                db.execute(
                    "UPDATE sessions SET strong_auth_at=? WHERE user_id='u_member'",
                    (sessions.stamp(at - timedelta(minutes=11)),),
                )
            elif case == "revoked":
                db.execute("UPDATE sessions SET revoked_at=? WHERE user_id='u_member'", (sessions.stamp(at),))
            else:
                db.execute("UPDATE users SET disabled_at=? WHERE id='u_member'", (sessions.stamp(at),))
    before = ledger.db.execute("SELECT count(*) FROM auth_events").fetchone()[0]
    for path, body in [
        ("recovery-codes", {}),
        ("session-close", {"id": "f" * 64}),
        ("sessions-close-others", {}),
        ("password-change", {"password": "replacement-password"}),
        ("totp/begin", {}),
        ("totp/finish", {"ticket": "f" * 64, "code": "123456"}),
    ]:
        status, _, answer = auth_request(server, "/auth/me/" + path, body, Cookie=cookie)
        assert status == 401 and bool(answer.get("reauth")) == (case == "stale")
    assert ledger.db.execute("SELECT count(*) FROM auth_events").fetchone()[0] == before


def test_own_code_regeneration_rechecks_clock_and_rolls_back_final_failure(ledger, server, monkeypatch):
    engine, at = server.service.browser_auth, server.service.clock()
    cookie = "__Host-session=" + "b" * 64
    path = "/auth/me/recovery-codes"
    old = recovery_codes.generate(ledger, "u_member", at)
    before = list(map(tuple, ledger.db.execute("SELECT * FROM recovery_codes")))
    assert auth_request(server, path, {"user_id": "u_admin"}, Cookie=cookie)[0] == 400
    assert auth_request(server, path, {}, Cookie=cookie, Origin="https://elsewhere.example")[0] == 403
    route = engine.routes[path]

    def late(service, *args, **kwargs):
        service.clock = lambda: at + timedelta(minutes=11)
        return route.handler(service, *args, **kwargs)

    monkeypatch.setitem(engine.routes, path, replace(route, handler=late))
    assert auth_request(server, path, {}, Cookie=cookie)[2]["reauth"]
    assert list(map(tuple, ledger.db.execute("SELECT * FROM recovery_codes"))) == before
    engine.clock = lambda: at
    monkeypatch.setitem(engine.routes, path, route)
    original = Ledger.replace_recovery_codes

    def fail(L, *args):
        original(L, *args)
        raise PermissionError("synthetic final-write refusal")

    monkeypatch.setattr(Ledger, "replace_recovery_codes", fail)
    assert auth_request(server, path, {}, Cookie=cookie)[0] == 401
    assert list(map(tuple, ledger.db.execute("SELECT * FROM recovery_codes"))) == before
    assert recovery_codes.consume(ledger, "u_member", old[0], at)
