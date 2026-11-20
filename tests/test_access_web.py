"""App access configuration and credential-gated public HTML; synthetic secrets only."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from contextlib import closing
from dataclasses import replace

import pytest
from test_api import _api, _get_json, _req, server as server  # noqa: F401
from test_api_v1 import request

from fiscalm.api import actions, tokens, uploads as upload_links, web
from fiscalm.api.schema import openapi
from fiscalm.ledger import Ledger, finance_home
from fiscalm.mail import setup as mail_setup


def test_tokens_install_wide_in_shadow(ledger, server, caplog):
    code, answer = _api(server, "token-create", {"name": "Reader", "scope": "read"})
    assert code == 200 and answer["ok"] and answer["state"]["co"] == "ALL"
    made = answer["done"]
    assert set(made) == {"id", "token", "scope"}
    assert tokens.verify(ledger, made["token"])
    with ledger.tx() as db:
        db.execute("UPDATE api_tokens SET created_at='2000-01-01' WHERE id=?", (made["id"],))
    other = _api(server, "token-create", {"co": "NORD", "name": "Writer", "scope": "write"})[1]["done"]
    for co in ("", "?co=NORD", "?co=STUDIO9", "?co=ALL"):
        code, listed = _get_json(server, "/web/api/tokens" + co)
        assert code == 200 and [r["id"] for r in listed] == [other["id"], made["id"]]
        assert set(listed[0]) == {
            "id",
            "name",
            "scope",
            "created_by",
            "created_at",
            "last_used_at",
            "revoked_at",
            "user_id",
            "owner",
        }
        assert listed[0]["created_by"] == listed[1]["created_by"] == "u_admin"
        assert listed[1]["last_used_at"]
        assert made["token"] not in json.dumps(listed)
    code, revoked = _api(server, "token-revoke", {"co": "STUDIO9", "id": made["id"]})
    assert code == 200 and revoked["done"] == {"revoked": True}
    assert _api(server, "token-revoke", {"id": made["id"]})[1]["done"] == {"revoked": False}
    assert tokens.verify(ledger, made["token"]) is None
    listed = _get_json(server, "/web/api/tokens")[1]
    assert listed[1]["revoked_at"]
    logs = "\n".join(p.read_text() for p in (finance_home() / "logs").glob("*.log")) + caplog.text
    assert made["id"] in logs and other["id"] in logs
    for secret in (made["token"], other["token"]):
        assert secret not in logs + json.dumps(revoked) + json.dumps(listed)
    assert "Reader" not in logs and "Writer" not in logs


@pytest.mark.parametrize(
    "action,body",
    [
        ("token-create", {"name": 12, "scope": "read"}),  # passes the HTTP check, refused by tokens.create
        ("token-create", {"name": "", "scope": "read"}),
        ("token-create", {"name": "x" * 101, "scope": "write"}),
        ("token-create", {"name": "x", "scope": "admin"}),
        ("token-create", {"name": [], "scope": "read"}),  # refused by the HTTP body check
        ("token-create", {"name": "x", "scope": "read", "extra": "no"}),
        ("token-create", {"co": "NOPE", "name": "x", "scope": "read"}),
        ("token-revoke", {"id": 12}),
        ("token-revoke", {"id": "../bad"}),
    ],
)
def test_token_refusals(ledger, server, action, body):
    code, answer = _api(server, action, body)
    assert code in (200, 400) and not answer["ok"]
    assert tokens.list_tokens(ledger) == []


def test_token_http_guards(ledger, server):
    body = {"name": "Reader", "scope": "read"}
    assert _api(server, "token-create", body, Origin="https://evil.example")[0] == 403
    assert _api(server, "token-create", body, ctype="text/plain")[0] == 415
    assert _api(server, "token-create", {**body, "name": "x" * 4100})[0] == 413
    assert _api(server, "token-create", body, user="unknown")[0] == 401
    assert _get_json(server, "/c/wrong/api/tokens")[0] == 404
    assert _get_json(server, "/web/api/tokens?co=NOPE")[0] == 400
    assert tokens.list_tokens(ledger) == []


def test_upload_html(ledger, tmp_path, monkeypatch, caplog):
    out = tmp_path / "out"
    out.mkdir()
    html = b'<!doctype html><script>console.log("upload")</script>'
    (out / "u.html").write_bytes(html)
    (out / "index.html").write_bytes(b'<script>console.log("owner")</script>')
    monkeypatch.setattr(web, "APP_OUT", out)
    monkeypatch.setenv("FISCALM_PUBLIC_URL", "https://books.example")
    monkeypatch.setenv("FISCALM_TRUST_PROXY", "1")
    delays = []
    monkeypatch.setattr(web.time, "sleep", delays.append)
    srv = web.Server(("127.0.0.1", 0), web.Handler)
    owner_hashes = web.script_hashes((out / "index.html").read_bytes())
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    port = srv.server_address[1]
    made = upload_links.create(ledger, "NORD", "Phone")
    path = made["path"]
    try:
        for suffix in ("", "/"):
            code, headers, raw = _req(port, "GET", path + suffix)
            assert code == 200 and raw == html
            assert headers["cache-control"] == "no-store" and headers["referrer-policy"] == "no-referrer"
            csp = headers["content-security-policy"]
            digest = base64.b64encode(hashlib.sha256(b'console.log("upload")').digest()).decode()
            assert "'sha256-" + digest + "'" in csp
            assert owner_hashes not in csp
            for rule in (
                "connect-src 'self'",
                "img-src 'self' blob: data:",
                "form-action 'none'",
                "frame-ancestors 'none'",
                "base-uri 'none'",
            ):
                assert rule in csp
        for bad in (
            path[:-1] + ("0" if path[-1] != "0" else "1"),
            "/u.html",
            "/web/u/",
            "/web/u.html",
            "/web/u/index.html",
            path + "/u.html",
        ):
            assert _req(port, "GET", bad)[0] in (401, 404)
        assert delays == [0.5]
        assert _req(port, "GET", path + "/api/info")[0] == 200
        assert _req(port, "HEAD", path)[0] == 404
        assert _req(port, "GET", path + "/manifest.webmanifest")[0] == 404
        changed = b"<script>rebuilt()</script>"
        (out / "u.html").write_bytes(changed)
        code, headers, raw = _req(port, "GET", path)
        assert code == 200 and raw == changed
        assert web.script_hashes(raw) in headers["content-security-policy"]
        assert web.script_hashes(html) not in headers["content-security-policy"]
        (out / "u.html").unlink()
        assert _req(port, "GET", path)[2].startswith(b"<!doctype html><html lang=ro>")
        upload_links.revoke(ledger, made["id"], "NORD")
        assert _req(port, "GET", path)[0] == 404
        assert delays == [0.5, 0.5]
        logs = "".join(p.read_text() for p in (finance_home() / "logs").rglob("*") if p.is_file())
        assert path not in caplog.text + logs
    finally:
        srv.shutdown()
        srv.server_close()


def test_bearer_cannot_administer_tokens(token_owner, ledger, server):
    bearer = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    for action, body in (
        ("token-create", {"name": "Hidden", "scope": "write"}),
        ("token-revoke", {"id": tokens.list_tokens(ledger)[0]["id"]}),
    ):
        assert request(server, bearer, "/companies/NORD/actions/" + action, "POST", body)[0] == 400
    assert len(tokens.list_tokens(ledger)) == 1 and tokens.verify(ledger, bearer)


@pytest.mark.parametrize("role", ["admin", "member", "readonly"])
def test_browser_tokens_are_owned_filtered_and_limited_by_role(ledger, server, role):
    owner = "u_" + role
    foreign = tokens.create(ledger, "Foreign", "read", user_id="u_member" if role != "member" else "u_admin")
    code, answer = _api(server, "token-create", {"name": "Owned", "scope": "read"}, user=role)
    assert code == 200 and answer["ok"]
    ident = answer["done"]["id"]
    assert ledger.db.execute("SELECT user_id,created_by FROM api_tokens WHERE id=?", (ident,)).fetchone()[:] == (
        owner,
        owner,
    )
    listed = _get_json(server, "/web/api/tokens?user_id=u_admin", user=role)[1]
    assert {t["id"] for t in listed} == ({ident, foreign["id"]} if role == "admin" else {ident})
    assert all(t["owner"]["id"] == owner for t in listed if t["id"] == ident)
    assert _api(server, "token-revoke", {"id": foreign["id"]}, user=role)[0] == (200 if role == "admin" else 403)
    assert _api(server, "token-revoke", {"id": ident}, user=role)[1]["done"]["revoked"]
    assert not _api(server, "token-revoke", {"id": ident}, user=role)[1]["done"]["revoked"]
    created = _api(server, "token-create", {"name": "Writer", "scope": "write"}, user=role)[1]
    assert created["ok"] == (role != "readonly")
    assert not _api(server, "token-create", {"name": "Forged", "scope": "read", "user_id": "u_admin"}, user=role)[1][
        "ok"
    ]
    if role != "admin":
        assert _api(server, "upload-link-create", {"co": "NORD", "label": "Phone"}, user=role)[0] == 403
    else:
        made = _api(server, "upload-link-create", {"co": "NORD", "label": "Phone"}, user=role)[1]["done"]
        assert _get_json(server, "/web/api/upload-links?co=NORD", user=role)[1][0]["id"] == made["id"]
        assert _api(server, "upload-link-revoke", {"co": "NORD", "id": made["id"]}, user=role)[1]["done"]["revoked"]


def test_token_all_context_and_log_failure(ledger, server, monkeypatch):
    def fail_log(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(web.actions, "log", fail_log)
    code, answer = _api(server, "token-create", {"co": "ALL", "name": "  " + "x" * 100 + "  ", "scope": "read"})
    assert code == 200 and answer["ok"] and answer["state"]["co"] == "ALL"
    assert tokens.verify(ledger, answer["done"]["token"])
    assert tokens.list_tokens(ledger)[0]["name"] == "x" * 100
    code, revoked = _api(server, "token-revoke", {"co": "ALL", "id": answer["done"]["id"]})
    assert code == 200 and revoked["done"] == {"revoked": True} and revoked["state"]["co"] == "ALL"


def test_malformed_upload_credentials_never_open_ledger(server, monkeypatch):
    def forbidden(*args):
        pytest.fail("malformed credential must not open the ledger or sleep")

    monkeypatch.setattr(web, "Ledger", forbidden)
    monkeypatch.setattr(web.time, "sleep", forbidden)
    for path in ("/u/", "/u/bad", "/u/manifest.webmanifest"):
        assert _req(server, "GET", path)[0] == 404
        assert _req(server, "HEAD", path)[0] == 404


@pytest.mark.parametrize(
    "origin", [None, "https://receipts.example.invalid/", "http://localhost:8090/", "http://[::1]:8090"]
)
def test_upload_public_origin(ledger, server, origin, caplog, monkeypatch):
    if origin is None:
        monkeypatch.delenv("FISCALM_PUBLIC_URL", raising=False)
    else:
        monkeypatch.setenv("FISCALM_PUBLIC_URL", origin)
    answer = _api(server, "upload-link-create", {"co": "NORD", "label": "Phone"})[1]
    assert answer["ok"]
    done = answer["done"]
    if origin:
        assert done["url"] == origin.removesuffix("/") + done["path"]
    else:
        assert "url" not in done
    logs = caplog.text + "".join(p.read_text() for p in (finance_home() / "logs").rglob("*") if p.is_file())
    assert done["path"] not in logs
    assert done.get("url", done["path"]) not in logs


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.invalid//",
        "https://example.invalid?x",
        "https://example.invalid#x",
        "https://user@example.invalid",
        "http://example.invalid",
        "http://192.0.2.1",
        "ftp://example.invalid",
        "https://example.invalid:bad",
        " https://example.invalid",
        "https://example.invalid\n",
    ],
)
def test_bad_upload_public_origin_refuses_before_create(ledger, server, origin, monkeypatch):
    lines = []
    monkeypatch.setattr(actions, "log", lines.append)
    monkeypatch.setenv("FISCALM_PUBLIC_URL", origin)
    answer = _api(server, "upload-link-create", {"co": "NORD", "label": "Phone"})[1]
    assert not answer["ok"]
    assert answer["message"] == (
        "Linkul Bondrop nu se poate crea: adresa publică a instalării nu este bună. "
        "Cere-i administratorului serverului să o corecteze."
    )
    assert lines == ["public address invalid (FISCALM_PUBLIC_URL)"]
    assert not upload_links.list_links(ledger, "NORD")


def test_openapi_excludes_token_administration():
    document = openapi()
    params = next(v["post"]["parameters"] for k, v in document["paths"].items() if k.endswith("/actions/{action}"))
    enum = next(p["schema"]["enum"] for p in params if p["name"] == "action")
    assert "token-create" not in enum and "token-revoke" not in enum


@pytest.mark.parametrize("case", ["demote", "disable", "revoke", "stale"])
def test_token_management_rechecks_session_and_admin_status_under_lock(ledger, server, monkeypatch, case):
    made = tokens.create(ledger, "Member token", "read", user_id="u_member")
    original = actions._token_create

    def changed(L, co, args, ctx):
        with closing(Ledger(ledger.path)) as other, other.tx() as db:
            if case == "demote":
                db.execute("UPDATE users SET role='member' WHERE id='u_admin'")
            elif case == "disable":
                db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='u_admin'")
            elif case == "revoke":
                db.execute("UPDATE sessions SET revoked_at='2026-11-04' WHERE user_id='u_admin'")
            else:
                db.execute("UPDATE sessions SET strong_auth_at='2026-11-01T00:00:00+00:00' WHERE user_id='u_admin'")
        return original(L, co, args, ctx)

    action = "token-create" if case == "stale" else "token-revoke"
    monkeypatch.setitem(actions.ACTIONS, action, replace(actions.ACTIONS[action], handler=changed))
    status, answer = _api(
        server, action, {"name": "Refused", "scope": "read"} if case == "stale" else {"id": made["id"]}
    )
    assert status == (401 if case == "stale" else 403)
    assert bool(answer.get("reauth")) == (case == "stale")
    assert tokens.verify(ledger, made["token"])
    assert len(tokens.list_tokens(ledger)) == 1
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='api_token_revoked'").fetchone()[0] == 0


@pytest.mark.parametrize("role", ["admin", "readonly"])
def test_token_request_loses_access_while_the_body_is_read(ledger, server, monkeypatch, role):
    original = web.Handler._json_body

    def revoked(handler, limit):
        result = original(handler, limit)
        with closing(Ledger(ledger.path)) as other, other.tx() as db:
            db.execute("UPDATE sessions SET revoked_at='2026-11-04' WHERE user_id=?", ("u_" + role,))
        return result

    monkeypatch.setattr(web.Handler, "_json_body", revoked)
    assert _api(server, "token-create", {"name": "Refused", "scope": "read"}, user=role)[0] == 403
    assert not tokens.list_tokens(ledger)
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='api_token_created'").fetchone()[0] == 0


def test_mail_setup_http_contract(ledger, server, monkeypatch, caplog):
    code, result = _api(server, "mail-detect", {"address": "reader@gmail.com"})
    assert code == 200 and result["ok"] and result["done"]["host"] == "imap.gmail.com"
    body = {"from_address": "reader@gmail.com", "password": "synthetic-mail-password"}
    monkeypatch.setattr(mail_setup, "test_connection", lambda cfg, pwd: {"code": "wrong_password"})
    code, result = _api(server, "mail-test", body)
    assert code == 200 and not result["ok"] and result["done"] == {"code": "wrong_password"}
    assert _api(server, "mail-test", body, user="member")[0] == 404
    assert _api(server, "mail-test", body, Origin="https://evil.example")[0] == 403
    logs = "\n".join(p.read_text() for p in (finance_home() / "logs").glob("*.log")) + caplog.text
    assert body["password"] not in logs + json.dumps(result)


@pytest.mark.parametrize("all_firms", [False, True])
def test_browser_link_scope(ledger, server, all_firms):
    code, result = _api(server, "upload-link-create", {"co": "NORD", "label": "Phone", "all_firms": all_firms})
    assert code == 200 and result["ok"]
    assert upload_links.verify(ledger, result["done"]["path"][3:])["company"] == ("ALL" if all_firms else "NORD")
