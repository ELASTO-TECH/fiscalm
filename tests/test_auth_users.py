"""Account roles, guarded changes and live browser authorization; synthetic users only."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_api import _api, _get_json, server as server, world as world  # noqa: F401

from fiscalm import companies, integrations as ig, secrets
from fiscalm.api import actions, auth, recovery_codes, sessions, setup_links, tickets, users as account_users, web
from fiscalm.invoicing import workspace as invoicing_api
from fiscalm.ledger import AuthorizationRefused, Ledger, is_live


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
def test_every_registered_action_checks_the_role(ledger, server, monkeypatch, role):
    calls = []
    monkeypatch.setattr(actions, "act", lambda action, *a, **kw: calls.append(action) or {"ok": True})
    monkeypatch.setattr(
        invoicing_api, "act", lambda L, op, *a, **kw: calls.append(op) or {"ok": True, "id": "synthetic", "message": ""}
    )
    registry = dict(actions.ACTIONS) | {"invoicing/" + op: spec for op, spec in invoicing_api.ACTIONS.items()}
    for name, spec in registry.items():
        calls.clear()
        code, _ = _api(
            server,
            name + "?co=NORD" if name == "invoicing/logo" else name,
            b"synthetic image" if name == "invoicing/logo" else {"co": "NORD"},
            ctype="image/png" if name == "invoicing/logo" else "application/json",
            user=role,
        )
        allowed = auth.require({"role": role}, spec.role)
        assert (bool(calls), code == 200) == (allowed, allowed), (name, role, code)


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
def test_shared_role_policy_and_bearer_refusals(role):
    user = {"role": role}
    assert auth.require(user, "read")
    assert auth.require(user, "member") == (role != "readonly")
    assert auth.require(user, "own_tokens") and not auth.require(user, "own_tokens", bearer=True)
    for need in (
        "manage_users",
        "manage_firms",
        "manage_tokens",
        "manage_rules",
        "manage_integrations",
        "connect_integrations",
    ):
        assert auth.require(user, need) == (role == "admin")
        assert not auth.require(user, need, bearer=True)
    assert auth.require(user, "manage_integrations", has_admin=False) == (role != "readonly")
    assert not auth.require(user, "unknown")
    assert not auth.require(dict(user, recovery_only=True), "read")
    assert not auth.require(None, "read")


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
@pytest.mark.parametrize(
    "route,need",
    [
        ("tokens", "own_tokens"),
        ("integrations", "manage_integrations"),
        ("state?co=NORD", "read"),
        ("companies", "read"),
        ("me", "read"),
    ],
)
def test_browser_read_roles(ledger, server, role, route, need):
    code, answer = _get_json(server, "/web/api/" + route, user=role)
    assert (code == 200) == auth.require({"role": role}, need)
    if route.startswith("state"):
        assert answer["can_manage_companies"] == (role == "admin")


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
def test_firm_bank_sources_name_inboxes_only_to_who_may_read_integrations(ledger, server, mail_account, role):
    _, answer = _get_json(server, "/web/api/companies", user=role)
    mail = [s for c in answer["companies"] for b in c["banks"] for s in b["sources"] if s["source"] == "mail"]
    assert mail and any(s["available"] for s in mail)
    named = [s.get("mailboxes") for s in mail if s["available"]]
    assert all(m == [mail_account["id"]] for m in named) if role == "admin" else all(m is None for m in named)
    with ledger.tx() as db:  # Acasă's reconnect task carries a connected bank's status: never its inboxes
        db.execute(
            "INSERT INTO bank_connections(id,company,bank,provider,accounts,valid_until,created_at) "
            "VALUES('bc-0000000000000001','NORD','bt','enablebanking','[]','2026-01-01','2025-10-01')"
        )
    _, state = _get_json(server, "/web/api/state?co=NORD", user=role)
    assert state["bank_connections"] and mail_account["id"] not in json.dumps(state)


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
def test_every_registered_get_checks_the_role(ledger, server, monkeypatch, role):
    examples = {
        "api/payment-files/[0-9]+": "api/payment-files/1",
        "invoicing/.*": "invoicing/example.pdf",
        "(?:api/)?backup": "api/backup",
        "api/.*": "api/unknown",
    }
    for key, route in web.ROUTES.items():
        method, pattern = key
        if method != "GET" or not route.auth:
            continue
        calls = []

        def admitted(handler, identity, query, calls=calls, pattern=pattern):
            calls.append(pattern)
            handler._json(200, {})

        with monkeypatch.context() as patch:
            patch.setitem(web.ROUTES, key, replace(route, handler=admitted))
            status, _ = _get_json(server, "/web/" + examples.get(pattern, pattern) + "?co=NORD", user=role)
        allowed = not route.role or auth.require({"role": role}, route.role)
        assert (status == 200, bool(calls)) == (allowed, allowed), (pattern, role, status)


def test_member_integrations_exception_ends_when_an_admin_exists(ledger, server):
    ledger.db.execute("UPDATE users SET role='member' WHERE role='admin'")
    ledger.db.commit()
    assert _get_json(server, "/web/api/integrations", user="member")[0] == 200
    assert _get_json(server, "/web/api/integrations", user="readonly")[0] == 404
    ledger.db.execute("UPDATE users SET role='admin' WHERE id='u_admin'")
    ledger.db.commit()
    assert _get_json(server, "/web/api/integrations", user="member")[0] == 404


@pytest.fixture
def accounts(ledger):
    at = datetime(2026, 11, 4, tzinfo=UTC)
    ids = {
        name: account_users.create(ledger, name, name, role, at, actor_id=None)
        for name, role in (("first", "admin"), ("second", "admin"), ("member", "member"))
    }
    ledger.db.execute("UPDATE users SET password_hash='synthetic-hash',totp_version='synthetic-version'")
    ledger.db.commit()
    return ids, at


def test_account_roles_disable_closes_access_and_audits(ledger, accounts):
    ids, at = accounts
    first, second = ids["first"], ids["second"]
    token = account_users.create(ledger, "ＲＥＡＤＥＲ", " Citire ", "readonly", at, actor_id=first)
    assert dict(ledger.db.execute("SELECT username,display_name,role FROM users WHERE id=?", (token,)).fetchone()) == {
        "username": "reader",
        "display_name": "Citire",
        "role": "readonly",
    }
    for actor in (ids["member"], "missing"):
        with pytest.raises(PermissionError):
            account_users.create(ledger, "extra", "Extra", "admin", at, actor_id=actor)
        with pytest.raises(PermissionError):
            ledger.change_user(token, actor, at.isoformat(), role="admin")
    ledger.db.execute(
        "INSERT INTO setup_links(id,secret_hash,user_id,purpose,created_by,created_at,expires_at) "
        "VALUES('synthetic-link','synthetic-digest',?,'enroll','cli',?,'2026-11-07')",
        (second, at.isoformat()),
    )
    ledger.db.commit()
    cookie = sessions.issue(ledger, second, at)
    ticket = tickets.issue(ledger, second, "totp", at)
    ledger.change_user(second, first, at.isoformat(), enabled=False)
    original = dict(ledger.db.execute("SELECT * FROM users WHERE id=?", (second,)).fetchone())
    ledger.change_user(second, None, (at + timedelta(days=1)).isoformat(), enabled=False)
    assert dict(ledger.db.execute("SELECT * FROM users WHERE id=?", (second,)).fetchone()) == original
    assert sessions.identify(ledger, cookie, at) is None
    assert tickets.attempt(ledger, ticket, "totp", at) is None
    assert ledger.db.execute("SELECT revoked_at FROM setup_links WHERE id='synthetic-link'").fetchone()[0]
    with pytest.raises(PermissionError):
        ledger.change_user(first, second, at.isoformat(), role="member")
    with pytest.raises(ValueError):
        ledger.change_user(first, None, at.isoformat(), role="member")
    with pytest.raises(ValueError):
        ledger.change_user(first, None, at.isoformat(), enabled=False)
    ledger.change_user(second, first, at.isoformat(), enabled=True)
    ledger.change_user(second, first, at.isoformat(), role="member")
    ledger.change_user(second, first, at.isoformat(), role="member")  # Idempotent, one audit event.
    assert [r[0] for r in ledger.db.execute("SELECT kind FROM auth_events")] == ["user_created"] * 4 + [
        "user_changed"
    ] * 3
    assert "synthetic-hash" not in str(list(ledger.db.execute("SELECT detail FROM auth_events")))


@pytest.mark.parametrize(
    "name,display,role",
    [
        ("bad/name", "Valid", "admin"),
        ("valid", "", "member"),
        ("valid", "line\nbreak", "readonly"),
        ("valid", "Valid", "root"),
    ],
)
def test_account_validation(ledger, accounts, name, display, role):
    with pytest.raises(ValueError):
        account_users.create(ledger, name, display, role, accounts[1], actor_id=None)


def test_last_working_admin_race(ledger, accounts):
    ids, at = accounts
    barrier = threading.Barrier(2)

    def demote(ident):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            other.change_user(ident, None, at.isoformat(), role="member")
            return True
        except ValueError:
            return False
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(demote, (ids["first"], ids["second"]))) == [False, True]
    assert ledger.db.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0] == 1
    with pytest.raises(ValueError):
        ledger.change_user("missing", None, at.isoformat(), enabled=False)


def test_duplicate_account_is_refused_before_any_audit_write(ledger, accounts):
    ids, at = accounts
    before = ledger.db.execute("SELECT count(*) FROM auth_events").fetchone()[0]
    with pytest.raises(ValueError):
        account_users.create(ledger, "ＦＩＲＳＴ", "Duplicate", "admin", at, actor_id=ids["first"])
    assert ledger.db.execute("SELECT count(*) FROM users").fetchone()[0] == 3
    assert ledger.db.execute("SELECT count(*) FROM auth_events").fetchone()[0] == before


def test_restore_revocation_keeps_factors_and_recovery_codes(ledger, accounts):
    ids, at = accounts
    normal = sessions.issue(ledger, ids["first"], at)
    recovery = sessions.issue(ledger, ids["second"], at, recovery=True)
    ticket = tickets.issue(ledger, ids["first"], "signin", at)
    link = setup_links.issue(ledger, ids["member"], at, actor_id=None, reset=True).partition("#")[2]
    codes = recovery_codes.generate(ledger, ids["first"], at)
    factor = ledger.db.execute("SELECT totp_version FROM users WHERE id=?", (ids["first"],)).fetchone()[0]
    ledger.forget_auth_sessions(sessions.stamp(at))
    assert sessions.identify(ledger, normal, at) is None and sessions.identify(ledger, recovery, at) is None
    assert tickets.attempt(ledger, ticket, "signin", at) is None
    assert setup_links.inspect(ledger, link, at) is None
    assert ledger.db.execute("SELECT totp_version FROM users WHERE id=?", (ids["first"],)).fetchone()[0] == factor
    assert recovery_codes.consume(ledger, ids["first"], codes[0], at)
    assert ledger.db.execute("SELECT actor FROM auth_events WHERE kind='all_sessions_revoked'").fetchone()[0] == "cli"


def test_engine_admission_refuses_unknown_actions_and_missing_firm(monkeypatch):
    assert not actions.act("unknown", "NORD", {})["ok"]
    monkeypatch.setattr(companies, "all", lambda: {})
    result = actions.act("mark-paid", "NORD", {})
    assert result == {"ok": False, "message": companies.NEEDS_COMPANY_RO, "next": None, "done": None}


def test_admin_account_actions_and_safe_reads(ledger, server):
    status, made = _api(
        server, "user-create", {"username": "new-member", "display_name": "Membru nou", "role": "member"}
    )
    ident, link = made["done"]["id"], made["done"]["url"]
    assert status == 200 and made["ok"] and link.startswith("/setup#")
    listed = _get_json(server, "/web/api/users")[1]
    row = next(u for u in listed if u["id"] == ident)
    assert row["role"] == "member" and not row["password_totp"] and row["last_signin"] is None
    assert set(row) == {
        "id",
        "username",
        "display_name",
        "role",
        "disabled_at",
        "locked_until",
        "password_totp",
        "passkeys",
        "last_signin",
    }
    assert _api(server, "user-update", {"id": ident, "display_name": "Citire", "role": "readonly"})[1]["ok"]
    changed = next(u for u in _get_json(server, "/web/api/users")[1] if u["id"] == ident)
    assert (changed["display_name"], changed["role"]) == ("Citire", "readonly")
    assert not _api(server, "user-update", {"id": ident, "display_name": "Citire", "role": "root"})[1]["ok"]
    replacement = _api(server, "user-setup-link", {"id": ident})[1]["done"]["url"]
    assert setup_links.inspect(ledger, link.partition("#")[2], server.service.clock()) is None
    assert replacement != link
    assert _api(server, "user-disable", {"id": ident})[1]["ok"]
    assert _api(server, "user-enable", {"id": ident})[1]["ok"]
    assert setup_links.inspect(ledger, replacement.partition("#")[2], server.service.clock()) is None
    assert _api(server, "user-unlock", {"id": ident})[1]["ok"]
    pending = _get_json(server, "/web/api/user-sessions?id=u_member")[1]
    assert len(pending) == 1 and "id_hash" not in pending[0]
    assert _api(server, "session-close", {"id": pending[0]["id"]})[1]["ok"]
    assert not _api(server, "session-close", {"id": pending[0]["id"]})[1]["ok"]
    assert _get_json(server, "/web/api/me", user="member")[0] == 401
    events = _get_json(server, "/web/api/auth-events")[1]
    assert events[0]["kind"] == "session_closed" and events[0]["actor"] == "user u_admin"
    assert link not in str(events) and replacement not in str(events)


@pytest.mark.parametrize("action", ["user-disable", "user-update", "user-reset"])
def test_http_refuses_removing_the_last_working_admin(ledger, server, action):
    before = dict(ledger.db.execute("SELECT * FROM users WHERE id='u_admin'").fetchone())
    body = {"id": "u_admin"} | ({"role": "member", "display_name": "Membru"} if action == "user-update" else {})
    assert not _api(server, action, body)[1]["ok"]
    assert dict(ledger.db.execute("SELECT * FROM users WHERE id='u_admin'").fetchone()) == before


def test_admin_reset_closes_access_and_issues_only_a_setup_link(ledger, server):
    before = _get_json(server, "/web/api/user-sessions?id=u_member")[1]
    assert before
    status, answer = _api(server, "user-reset", {"id": "u_member"})
    assert status == 200 and answer["ok"]
    link = setup_links.inspect(ledger, answer["done"]["url"].partition("#")[2], server.service.clock())
    assert link["user_id"] == "u_member" and link["purpose"] == "reset"
    assert _get_json(server, "/web/api/me", user="member")[0] == 401
    assert _get_json(server, "/web/api/user-sessions?id=u_member")[1] == []


@pytest.mark.parametrize(
    "body",
    [
        {"username": "invalid/name", "display_name": "Nume", "role": "member"},
        {"username": "valid", "display_name": "Nume", "role": "root"},
        {"username": "valid", "display_name": "line\nbreak", "role": "member"},
        {"username": "admin", "display_name": "Duplicat", "role": "admin"},
    ],
)
def test_user_create_refusals_write_no_account_link_or_event(ledger, server, body):
    before = [
        ledger.db.execute("SELECT count(*) FROM " + table).fetchone()[0]
        for table in ("users", "setup_links", "auth_events")
    ]
    assert not _api(server, "user-create", body)[1]["ok"]
    assert [
        ledger.db.execute("SELECT count(*) FROM " + table).fetchone()[0]
        for table in ("users", "setup_links", "auth_events")
    ] == before


def test_user_create_rolls_back_when_link_creation_fails(ledger, server, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("Configurarea nu a putut fi creată.")

    monkeypatch.setattr(setup_links, "issue", fail)
    assert not _api(server, "user-create", {"username": "pending", "display_name": "În așteptare", "role": "member"})[
        1
    ]["ok"]
    assert not ledger.db.execute("SELECT 1 FROM users WHERE username='pending'").fetchone()
    assert ledger.db.execute("SELECT count(*) FROM auth_events").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["disable", "demote", "revoke", "stale"])
def test_admin_authority_and_stepup_are_rechecked_on_the_writer(ledger, server, monkeypatch, change):
    original = actions._account_action

    def changed(L, co, args, ctx):
        with closing(Ledger(ledger.path)) as other, other.tx() as db:
            if change == "disable":
                db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='u_admin'")
            elif change == "demote":
                db.execute("UPDATE users SET role='member' WHERE id='u_admin'")
            elif change == "revoke":
                db.execute("UPDATE sessions SET revoked_at='2026-11-04' WHERE user_id='u_admin'")
            else:
                db.execute("UPDATE sessions SET strong_auth_at='2026-11-01T00:00:00+00:00' WHERE user_id='u_admin'")
        return original(L, co, args, ctx)

    monkeypatch.setitem(actions.ACTIONS, "user-create", replace(actions.ACTIONS["user-create"], handler=changed))
    status, answer = _api(server, "user-create", {"username": "refused", "display_name": "Refuzat", "role": "admin"})
    assert status == (401 if change == "stale" else 403)
    assert bool(answer.get("reauth")) == (change == "stale")
    assert not ledger.db.execute("SELECT 1 FROM users WHERE username='refused'").fetchone()


def test_account_administration_requires_fresh_session_and_refuses_actor_overrides(ledger, server):
    body = {"username": "new", "display_name": "Nou", "role": "member"}
    assert not _api(server, "user-create", body | {"actor_id": "cli"})[1]["ok"]
    assert _api(server, "user-create", body, Origin="https://elsewhere.example")[0] == 403
    ledger.db.execute("UPDATE sessions SET strong_auth_at='2026-11-01T00:00:00+00:00' WHERE user_id='u_admin'")
    ledger.db.commit()
    status, answer = _api(server, "user-create", body)
    assert status == 401 and answer["reauth"]
    assert not ledger.db.execute("SELECT 1 FROM users WHERE username='new'").fetchone()


@pytest.mark.parametrize(
    "action,body",
    [
        ("user-create", {"username": "refused", "display_name": "Refuzat", "role": "admin"}),
        ("token-create", {"name": "Refused", "scope": "read"}),
    ],
)
def test_account_action_without_a_session_actor_is_refused(ledger, action, body):
    with pytest.raises(PermissionError):
        actions.act(action, "ALL", body)
    assert ledger.db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM auth_events").fetchone()[0] == 0


def test_refused_account_update_writes_no_success_audit(ledger, server):
    before = dict(ledger.db.execute("SELECT * FROM users WHERE id='u_member'").fetchone())
    ledger.db.execute(
        "CREATE TRIGGER refuse_account_update BEFORE UPDATE OF role,display_name ON users "
        "WHEN OLD.id='u_member' BEGIN SELECT RAISE(IGNORE); END"
    )
    ledger.db.commit()
    assert _api(server, "user-update", {"id": "u_member", "display_name": "Schimbat", "role": "readonly"})[0] == 403
    assert dict(ledger.db.execute("SELECT * FROM users WHERE id='u_member'").fetchone()) == before
    assert ledger.db.execute("SELECT count(*) FROM auth_events").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["none", "demote", "disable", "revoke"])
def test_generic_browser_writer_rechecks_after_admission_without_holding_lock_during_handler(
    ledger, server, world, monkeypatch, change
):
    row = dict(ledger.db.execute("SELECT * FROM efactura WHERE company='NORD' AND status='open' LIMIT 1").fetchone())
    original = actions.invoice_action

    def delayed(current, co, body, ctx):
        with closing(Ledger(ledger.path)) as other, other.tx() as db:
            if change == "demote":
                db.execute("UPDATE users SET role='readonly' WHERE id='u_admin'")
            elif change == "disable":
                db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='u_admin'")
            elif change == "revoke":
                db.execute("UPDATE sessions SET revoked_at='2026-11-04' WHERE user_id='u_admin'")
        return original(current, co, body, ctx)

    monkeypatch.setitem(actions.ACTIONS, "mark-paid", replace(actions.ACTIONS["mark-paid"], handler=delayed))
    status, answer = _api(server, "mark-paid", {"co": "NORD", "id": row["message_id"], "method": "bank"})
    assert status == (200 if change == "none" else 403)
    assert answer["ok"] == (change == "none")
    after = dict(ledger.efactura(row["message_id"]))
    assert (after != row) == (change == "none")


@pytest.mark.parametrize(
    "route,target",
    [
        ("statement-upload?co=NORD", "statement"),
        ("statement-upload?co=NORD&report=1", "report"),
        ("history-upload?co=NORD&start=2026-11-01", "history"),
    ],
)
def test_upload_authority_is_checked_by_the_actual_writer_after_body_admission(
    ledger, server, monkeypatch, route, target
):
    def parsed(current, *args, **kwargs):
        with closing(Ledger(ledger.path)) as other, other.tx() as db:
            db.execute("UPDATE users SET role='member' WHERE id='u_admin'")
        current.set_meta("unauthorized_upload", target)
        raise AssertionError("A refused upload wrote ledger state")

    module, name = {
        "statement": (web.su, "accept"),
        "report": (web.bank_upload, "accept_report"),
        "history": (web.history, "import_file"),
    }[target]
    monkeypatch.setattr(module, name, parsed)
    assert _api(server, route, b"<synthetic/>", ctype="application/xml")[0] == 403
    assert ledger.get_meta("unauthorized_upload") is None


def test_write_guard_forces_lock_preserves_nested_atomicity_and_clears_after_refusal(ledger, server):
    at = server.service.clock()
    principal = sessions.identify(ledger, "b" * 64, at)
    checked = []
    with closing(Ledger(ledger.path, timeout=0)) as other:

        def authorize(current):
            with pytest.raises(sqlite3.OperationalError, match="locked"), other.tx():
                pass
            checked.append(current)
            auth.authorize_write(current, principal, at)

        with ledger.guard_writes(authorize):
            with ledger.tx(immediate=False) as db:
                db.execute("UPDATE users SET role='readonly' WHERE id='u_member'")
                ledger.set_meta("authorized_nested", "yes")
            assert checked == [ledger]
            with pytest.raises(AuthorizationRefused), ledger.guard_writes(None):
                ledger.set_meta("must_not_exist", "no")
        assert not ledger.db.in_transaction and ledger.get_meta("must_not_exist") is None
        ledger.set_meta("trusted_after_guard", "yes")
        assert ledger.get_meta("authorized_nested") == ledger.get_meta("trusted_after_guard") == "yes"


@pytest.mark.parametrize("nested", [False, True])
def test_guarded_writes_refuse_unmanaged_transactions_and_rollback_them(ledger, nested):
    with pytest.raises(RuntimeError), ledger.guard_writes(lambda current: None):
        ledger.db.execute("INSERT INTO meta(key,value) VALUES('unguarded','no')")
        if nested:
            ledger.set_meta("nested_after_unguarded", "no")
    assert not ledger.db.in_transaction
    assert ledger.get_meta("unguarded") is ledger.get_meta("nested_after_unguarded") is None


@pytest.mark.parametrize("action", ["integration-add", "integration-update"])
@pytest.mark.parametrize("change", ["readonly", "disabled", "revoked"])
def test_integration_transactions_recheck_authority_before_rows_or_secret_files(
    ledger, server, monkeypatch, action, change
):
    values = {
        "host": "imap.example.invalid",
        "username": "example",
        "from_address": "invoices@example.invalid",
        "password": "synthetic-old-password",
    }
    body = (
        {"kind": "mail", "name": "Poștă de test"}
        if action == "integration-add"
        else {"id": ig.add(ledger, "mail", "Poștă de test", values)["id"]}
    )
    body |= {"f_" + key: value for key, value in (values | {"password": "synthetic-new-password"}).items()}
    before = [dict(row) for row in ledger.db.execute("SELECT * FROM integrations")]
    files = set(secrets.secrets_dir().glob("int-*"))
    operation = action.removeprefix("integration-")
    original = getattr(ig, operation)

    def changed(current, *args, **kwargs):
        with closing(Ledger(ledger.path)) as other, other.tx() as db:
            if change == "readonly":
                db.execute("UPDATE users SET role='readonly' WHERE id='u_admin'")
            elif change == "disabled":
                db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='u_admin'")
            else:
                db.execute("UPDATE sessions SET revoked_at='2026-11-04' WHERE user_id='u_admin'")
        return original(current, *args, **kwargs)

    monkeypatch.setattr(ig, operation, changed)
    status, answer = _api(server, action, body)
    assert status == 403, answer
    assert [dict(row) for row in ledger.db.execute("SELECT * FROM integrations")] == before
    assert set(secrets.secrets_dir().glob("int-*")) == files


@pytest.mark.parametrize("refusal", ["member", "readonly", "stale", "origin", "missing-origin"])
def test_mode_switch_refuses_without_admin_fresh_auth_and_origin(ledger, server, refusal):
    if refusal == "stale":
        ledger.db.execute("UPDATE sessions SET strong_auth_at='2026-11-01T00:00:00+00:00'")
        ledger.db.commit()
    headers = (
        {"Origin": None if refusal == "missing-origin" else "https://elsewhere.example"} if "origin" in refusal else {}
    )
    code, result = _api(
        server,
        "mode-switch",
        {"mode": "live", "expected": "sandbox", "confirmed": "yes", "request_id": "a" * 32},
        user=refusal if refusal in ("member", "readonly") else "admin",
        **headers,
    )
    assert code == (401 if refusal == "stale" else 403)
    assert not is_live()
    assert not ledger.db.execute("SELECT 1 FROM auth_events WHERE kind='mode_changed'").fetchone()


def test_mode_switch_double_submit_and_return_to_shadow_are_audited(ledger, server):
    body = {"mode": "live", "expected": "sandbox", "confirmed": "yes", "request_id": "a" * 32}
    assert _api(server, "mode-switch", body)[1]["ok"]
    assert is_live()
    assert not _api(server, "mode-switch", body)[1]["ok"]
    assert _api(
        server, "mode-switch", {"mode": "sandbox", "expected": "live", "confirmed": "yes", "request_id": "b" * 32}
    )[1]["ok"]
    assert not is_live()
    assert not _api(server, "mode-switch", body)[1]["ok"]
    assert not is_live()
    assert [tuple(r) for r in ledger.db.execute("SELECT actor,detail FROM auth_events WHERE kind='mode_changed'")] == [
        ("u_admin", "live"),
        ("u_admin", "sandbox"),
    ]


def test_competing_mode_requests_are_consumed_once(ledger):
    def switch():
        with closing(Ledger(ledger.path)) as connection:
            return connection.change_mode(True, False, "cli", "2026-11-05T09:00:00+00:00", "c" * 32)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: switch(), range(2))) == [False, True]
    assert [
        tuple(row) for row in ledger.db.execute("SELECT actor,detail FROM auth_events WHERE kind='mode_changed'")
    ] == [("cli", "live")]


def test_pending_enable_refuses_intervening_enable_and_shutdown(ledger, monkeypatch):
    pending, resume = threading.Event(), threading.Event()

    def delayed_switch():
        with closing(Ledger(ledger.path)) as delayed:
            original = delayed.tx
            paused = False

            @contextmanager
            def pause_after_intent(*args, **kwargs):
                nonlocal paused
                with original(*args, **kwargs) as db:
                    yield db
                if not paused and not delayed.db.in_transaction:
                    paused = True
                    pending.set()
                    assert resume.wait(5)

            monkeypatch.setattr(delayed, "tx", pause_after_intent)
            return delayed.change_mode(True, False, "cli", "2026-11-05", "d" * 32)

    with ThreadPoolExecutor(max_workers=1) as pool, closing(Ledger(ledger.path)) as other:
        first = pool.submit(delayed_switch)
        try:
            assert pending.wait(5)
            assert other.change_mode(True, False, "cli", "2026-11-05", "e" * 32)
            assert other.change_mode(False, True, "cli", "2026-11-05", "f" * 32)
        finally:
            resume.set()
        assert not first.result()
    assert not is_live()
