import json
from contextlib import closing

import pytest
from test_api import _req, quiet, server as server, world as world  # noqa: F401
from test_api_v1 import request

from fiscalm import cli
from fiscalm.api import actions, mcp_tools, tokens, uploads as upload_links
from fiscalm.ledger import Ledger


def test_lifecycle(ledger, capsys, monkeypatch):
    made = upload_links.create(ledger, "NORD", "Phone", max_files=2)
    token = made["path"][3:]
    assert upload_links.verify(ledger, token, "NORD")
    assert not upload_links.verify(ledger, token, "STUDIO9")
    for bad in ("bad", token[:-1] + "x", "f" * 16 + token[16:]):
        assert not upload_links.verify(ledger, bad)
    assert token not in str(upload_links.list_links(ledger, "NORD"))
    assert token not in str(list(map(tuple, ledger.db.execute("SELECT * FROM upload_links"))))
    assert cli.main(["upload-link", "list", "--company", "NORD"]) == 0
    assert token not in capsys.readouterr().out
    assert not upload_links.revoke(ledger, made["id"], "STUDIO9")
    assert upload_links.revoke(ledger, made["id"], "NORD")
    assert not upload_links.verify(ledger, token)
    made = upload_links.create(ledger, "NORD", "Expiry")
    monkeypatch.setattr(upload_links, "timestamp", lambda: "9999")
    assert not upload_links.verify(ledger, made["path"][3:])


@pytest.mark.parametrize(
    "kw",
    [
        {"company": "UNKNOWN"},
        {"label": ""},
        {"expires_in_days": 0},
        {"expires_in_days": 366},
        {"max_files": 0},
        {"max_files": True},
        {"max_files": 2**63},
    ],
)
def test_validation(ledger, kw):
    with pytest.raises(ValueError):
        upload_links.create(ledger, **({"company": "NORD", "label": "Phone"} | kw))


def test_http_management(token_owner, ledger, server, home, world):
    (home / "state" / "LIVE").unlink()
    writer = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    reader = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    path = "/companies/NORD/upload-links"
    assert request(server, reader, path, "POST", {"label": "Phone"})[0] == 403
    code, result = request(server, writer, path, "POST", {"label": "Phone"})
    assert code == 200 and result["ok"]
    made = result["done"]
    assert request(server, reader, path)[1][0]["id"] == made["id"]
    code, _, raw = _req(server, "GET", "/web/api/upload-links?co=NORD")
    assert code == 200 and made["path"].encode() not in raw
    assert request(server, writer, path + "/" + made["id"] + "/revoke", "POST", {})[1]["done"]["revoked"]
    assert not upload_links.verify(ledger, made["path"][3:])


def test_mcp_does_not_expose_upload_credentials(ledger):
    assert not any("upload_link" in d["name"] for d in mcp_tools.definitions("write"))
    with pytest.raises(ValueError):
        mcp_tools.call(ledger, "write", "action_upload_link_create", {"co": "NORD", "body": {}})


@pytest.mark.parametrize("value", [True, 1.5, "2"])  # refused by the v1 body check, the same, by upload_links
def test_strict_integer_http(token_owner, ledger, server, world, value):
    writer = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    assert (
        request(server, writer, "/companies/NORD/upload-links", "POST", {"label": "Phone", "max_files": value})[0]
        == 400
    )


def test_cli_default_quota(ledger, capsys):
    assert cli.main(["upload-link", "create", "--company", "NORD", "--label", "Phone"]) == 0
    made = json.loads(capsys.readouterr().out)
    assert upload_links.verify(ledger, made["path"][3:])["max_files"] == 100


def test_app_management_logs_only_id(ledger, home, monkeypatch):
    lines = []
    monkeypatch.setattr(actions, "log", lines.append)
    result = actions.act("upload-link-create", "NORD", {"label": "Private label"})
    made = result["done"]
    actions.act("upload-link-revoke", "NORD", {"id": made["id"]})
    assert lines == [f"web: upload-link-create {made['id']}", f"web: upload-link-revoke {made['id']}"]


def test_app_management_survives_log_failure(ledger, monkeypatch):
    def fail_log(line):
        raise OSError("log unavailable")

    monkeypatch.setattr(actions, "log", fail_log)
    result = actions.act("upload-link-create", "NORD", {"label": "Phone"})
    assert result["ok"]
    made = result["done"]
    assert upload_links.verify(ledger, made["path"][3:], "NORD")
    assert result["message"] == ""  # the new link is shown once, no toast
    result = actions.act("upload-link-revoke", "NORD", {"id": made["id"]})
    assert result["ok"] and result["done"]["revoked"] and result["message"] == "Linkul a fost revocat."
    assert not upload_links.verify(ledger, made["path"][3:])


def test_all_firms_cli(ledger, capsys):
    assert cli.main(["upload-link", "create", "--all-firms", "--label", "Phone"]) == 0
    made = json.loads(capsys.readouterr().out)
    assert upload_links.verify(ledger, made["path"][3:])["company"] == "ALL"
    assert upload_links.list_links(ledger, "NORD")[0]["id"] == made["id"]


@pytest.mark.parametrize("route", ["explicit", "action"])
def test_shared_link_api_requires_admin(token_owner, ledger, server, world, route):
    writer = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    path = "/companies/NORD/upload-links"
    assert request(server, writer, path, "POST", {"label": "Phone", "all_firms": True})[0] == 403
    with ledger.tx() as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (token_owner,))
    code, result = request(server, writer, path, "POST", {"label": "Phone", "all_firms": True})
    assert code == 200 and result["ok"]
    with ledger.tx() as db:
        db.execute("UPDATE users SET role='member' WHERE id=?", (token_owner,))
    ident = result["done"]["id"]
    revoke_path = (
        path + "/" + ident + "/revoke" if route == "explicit" else "/companies/NORD/actions/upload-link-revoke"
    )
    body = {} if route == "explicit" else {"id": ident}
    assert request(server, writer, revoke_path, "POST", body)[0] == 403
    assert ledger.db.execute("SELECT revoked_at FROM upload_links WHERE id=?", (ident,)).fetchone()[0] is None
    with ledger.tx() as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (token_owner,))
    assert request(server, writer, revoke_path, "POST", body)[1]["done"]["revoked"]


@pytest.mark.parametrize("route", ["explicit", "action"])
def test_shared_revocation_rechecks_admin_at_writer(token_owner, ledger, server, world, monkeypatch, route):
    with ledger.tx() as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (token_owner,))
    writer = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    made = upload_links.create(ledger, "ALL", "Phone")
    original = actions.act

    def demote_before_writer(*args, **kwargs):
        with closing(Ledger()) as current, current.tx() as db:
            db.execute("UPDATE users SET role='member' WHERE id=?", (token_owner,))
        return original(*args, **kwargs)

    monkeypatch.setattr(actions, "act", demote_before_writer)
    path = (
        "/companies/NORD/upload-links/" + made["id"] + "/revoke"
        if route == "explicit"
        else "/companies/NORD/actions/upload-link-revoke"
    )
    body = {} if route == "explicit" else {"id": made["id"]}
    assert request(server, writer, path, "POST", body)[0] == 403
    assert ledger.db.execute("SELECT revoked_at FROM upload_links WHERE id=?", (made["id"],)).fetchone()[0] is None
