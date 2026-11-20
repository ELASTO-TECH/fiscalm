"""Integrări: any number of outside connections, secrets write-only, refusals, the app's actions (allowed in shadow
mode: configuration, no outward effect) and no fallback to the first install's files. Invented keys only; no network."""

import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading

import pytest
from test_api import _api, _get_json, server as server  # noqa: F401  (server is a fixture)

from fiscalm import cli, integrations as ig, secrets
from fiscalm.efactura import client as anaf
from fiscalm.integrations import IntegrationError
from fiscalm.ledger import Ledger, finance_home

KEY = "sk-test-0000-not-a-real-key"
MAIL = {"host": "imap.example.com", "username": "u", "from_address": "sender@example.invalid", "password": "p"}


def _files(sec):
    return {p.name: p.read_text() for p in sec.iterdir()} if sec.exists() else {}


def test_add_keeps_secrets_out_of_every_listing(ledger, sec):
    sec.mkdir(mode=0o755)
    sec.chmod(0o755)  # too open: closed before a secret goes in
    got = ig.add(ledger, "ai", "Claude", {"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key": KEY})
    assert stat.S_IMODE(sec.stat().st_mode) == 0o700
    assert got["secrets"] == {"api_key": True} and got["default"] and got["config"]["provider"] == "anthropic"
    assert KEY not in json.dumps(ig.listing(ledger)) and KEY not in json.dumps(got)
    assert KEY not in json.dumps(dict(ledger.db.execute("SELECT * FROM integrations").fetchone()))  # nor the ledger
    [f] = sec.glob(f"int-{got['id']}-api_key-*")  # one versioned file
    assert f.read_text() == KEY and stat.S_IMODE(f.stat().st_mode) == 0o600
    inst = ig.default(ledger, "ai")
    assert inst.secret("api_key") == KEY and inst.config["model"] == "claude-sonnet-5-5"


def test_any_number_of_a_kind_with_one_default(ledger, sec):
    a = ig.add(ledger, "ai", "Claude", {"provider": "anthropic", "api_key": KEY})
    b = ig.add(ledger, "ai", "Local", {"provider": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen"})
    c = ig.add(ledger, "bank", "Bank", {"bank": "bt", "company": "NORD"})
    assert [i["default"] for i in ig.listing(ledger, "ai")] == [True, False] and c["default"]
    # the schema itself refuses two defaults of a kind
    with pytest.raises(sqlite3.IntegrityError), ledger.tx() as db:
        db.execute("UPDATE integrations SET is_default=1 WHERE kind='ai'")
    ig.make_default(ledger, b["id"])
    assert ig.default(ledger, "ai").id == b["id"]
    ig.set_enabled(ledger, b["id"], False)  # a disabled default is skipped
    assert ig.default(ledger, "ai").id == a["id"]
    ig.set_enabled(ledger, b["id"], True)
    ig.remove(ledger, b["id"])  # removing the default hands it to the oldest left
    assert [i["id"] for i in ig.listing(ledger, "ai")] == [a["id"]] and ig.listing(ledger, "ai")[0]["default"]


@pytest.mark.parametrize(
    "kind, values, why",
    [
        ("webhook", {}, "necunoscut"),
        ("ai", {"provider": "gpt-builtin"}, "Serviciu"),
        ("ai", {"provider": "anthropic"}, "Cheie API"),  # a hosted provider needs a key
        (
            "ai",
            {"provider": "openai", "base_url": "http://models.example.com/v1", "api_key": KEY},
            "http doar",
        ),  # off-site
        (
            "ai",
            {"provider": "openai", "base_url": "http://169.254.169.254/v1", "model": "m"},
            "http doar",
        ),  # cloud metadata
        ("ai", {"provider": "openai", "base_url": "https://u:p@api.example.com", "api_key": KEY}, "fără utilizator"),
        ("ai", {"provider": "openai", "base_url": "file:///etc/passwd", "api_key": KEY}, "http"),
        ("ai", {"provider": "anthropic", "api_key": KEY + "\nX"}, "nepermise"),
        ("ai", {"provider": "anthropic", "api_key": KEY, "extra": "x"}, "necunoscut"),
        ("mail", {**MAIL, "password": ""}, "Parolă"),
        ("mail", {**MAIL, "host": "imap.example.com; rm"}, "server"),
        ("mail", {**MAIL, "port": "99999"}, "număr"),
        ("bank", {"bank": "bt", "company": "NOPE"}, "Firma"),
    ],
)
def test_refusals_write_nothing_and_never_echo_a_secret(ledger, sec, kind, values, why):
    with pytest.raises(IntegrationError, match=why) as e:
        ig.add(ledger, kind, "x", values)
    assert ig.listing(ledger) == [] and _files(sec) == {} and KEY not in str(e.value)


@pytest.mark.parametrize("url", ["https://user:pw@host／example.com/x", "https://host:abc/x"])
def test_a_malformed_url_is_refused_without_repeating_it(ledger, sec, url):
    with pytest.raises(IntegrationError) as e:
        ig.add(ledger, "ai", "Local", {"provider": "openai", "model": "m", "base_url": url})
    assert "pw" not in str(e.value) and "host" not in str(e.value) and e.value.__cause__ is None
    assert e.value.__suppress_context__


@pytest.mark.parametrize("public", ["https://fiscalm.example.com", "", "http://fiscalm.example.com"])
def test_known_addresses_and_mail_reading_are_proposed(monkeypatch, public):
    monkeypatch.setenv("FISCALM_PUBLIC_URL", public)
    defaults = {(k["key"], f["key"]): f["default"] for k in ig.kinds() for f in k["fields"]}
    origin = public if public.startswith("https:") else ""  # only an https origin is proposed
    assert defaults["shop", "fiscalm_url"] == origin
    assert defaults["openbanking", "return_url"] == (origin and origin + "/bank/return")


def test_conditional_requirements_are_one_rule_for_engine_and_app(ledger, sec):
    """The clauses the settings screen reads are the ones the engine enforces; only supported kinds are offered."""
    assert "webhook" not in {k["key"] for k in ig.kinds()}
    ai = next(k for k in ig.kinds() if k["key"] == "ai")
    key = next(f for f in ai["fields"] if f["key"] == "api_key")
    assert key["required_when"] == [{"provider": ["anthropic"]}, {"provider": ["openai"], "base_url": [""]}]
    with pytest.raises(IntegrationError, match="obligatoriu pentru această alegere"):
        ig.add(ledger, "ai", "x", {"provider": "openai", "model": "m"})  # default address: a key is needed
    assert ig.add(ledger, "ai", "y", {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:1/v1"})
    assert ig.add(ledger, "ai", "z", {"provider": "claude-cli"})  # the CLI needs neither


def test_anaf_firm_scope_and_coverage(ledger, sec):
    a = ig.add(ledger, "anaf", "ANAF", {"client_id": "cid", "client_secret": "csec"})
    b = ig.add(ledger, "anaf", "ANAF 2", {"client_id": "cid2", "client_secret": "csec2"}, firm_scope=["NORD"])
    assert a["firm_scope"] == "all"
    assert ig.coverage(ledger)[0] == {"firm": "NORD", "anaf": a["id"], "anaf_also": [b["id"]], "mail": []}
    ledger.set_meta("anaf_read_connection:NORD", b["id"])
    assert ig.anaf_for_firm(ledger, "NORD").id == b["id"]
    ig.update(ledger, b["id"], {}, firm_scope=[])
    assert ig.anaf_for_firm(ledger, "NORD").id == a["id"]
    ig.update(ledger, a["id"], {}, firm_scope=["STUDIO9"])
    assert ig.anaf_for_firm(ledger, "NORD") is None
    with pytest.raises(IntegrationError):
        ig.update(ledger, a["id"], {}, firm_scope=["missing"])
    assert ig.instance(ledger, a["id"]).firm_scope == ["STUDIO9"]


def test_two_connections_adding_anaf_at_once_keep_separate_secrets(ledger, sec):
    results, start = [], threading.Barrier(2)

    def add(n):
        L = Ledger()  # one connection per thread, as two processes would have
        try:
            start.wait()
            results.append(ig.add(L, "anaf", f"ANAF {n}", {"client_id": f"c{n}", "client_secret": f"s{n}"})["id"])
        except IntegrationError as e:
            results.append(str(e))
        finally:
            L.close()

    ts = [threading.Thread(target=add, args=(n,)) for n in (1, 2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(r.startswith("anaf-") for r in results) == 2
    assert len(ig.listing(ledger, "anaf")) == 2 and len(_files(sec)) == 2


def test_update_keeps_a_secret_sent_empty_replaces_one_sent_and_a_refusal_keeps_it(ledger, sec):
    i = ig.add(ledger, "ai", "Claude", {"provider": "anthropic", "api_key": KEY})
    ig.update(ledger, i["id"], {"model": "claude-opus-5-5", "api_key": ""})
    assert ig.default(ledger, "ai").secret("api_key") == KEY
    for values, name, why in [
        ({"api_key": "sk-other"}, "", "Nume"),
        ({"api_key": "sk-other", "provider": "nope"}, None, "Serviciu"),
    ]:
        with pytest.raises(IntegrationError, match=why):
            ig.update(ledger, i["id"], values, name=name)
        assert ig.default(ledger, "ai").secret("api_key") == KEY
    ig.update(ledger, i["id"], {"api_key": "sk-new"}, name="Claude 2")
    got = ig.listing(ledger)[0]
    assert ig.default(ledger, "ai").secret("api_key") == "sk-new" and got["name"] == "Claude 2"
    assert got["config"]["model"] == "claude-opus-5-5"


def test_a_required_secret_cannot_be_cleared_an_optional_one_can(ledger, sec):
    w = ig.add(
        ledger,
        "ai",
        "Local",
        {"provider": "openai", "model": "m", "base_url": "http://localhost:1234", "api_key": "s3"},
    )
    assert ig.clear(ledger, w["id"], "api_key")["secrets"] == {"api_key": False}
    m = ig.add(ledger, "mail", "Mail", {**MAIL, "password": "  pa ss  "})
    with pytest.raises(IntegrationError, match="Parolă"):
        ig.clear(ledger, m["id"], "password")
    assert ig.default(ledger, "mail").secret("password") == "  pa ss  "  # a password keeps its spaces


@pytest.mark.parametrize("iid", ["../../etc/passwd", None, "ai-1234567g"])
def test_unknown_or_malformed_ids_are_refused(ledger, sec, iid):
    with pytest.raises(IntegrationError, match="nu există"):
        ig.update(ledger, iid, {})
    with pytest.raises(IntegrationError, match="nu există"):
        ig.remove(ledger, iid)


def test_remove_deletes_its_secrets_and_a_late_update_leaves_no_orphan(ledger, sec):
    i = ig.add(ledger, "mail", "Mail", MAIL)
    ig.remove(ledger, i["id"])
    assert _files(sec) == {} and ig.listing(ledger) == []
    with pytest.raises(IntegrationError, match="nu există"):
        ig.update(ledger, i["id"], {"password": "late"})
    assert _files(sec) == {}


def test_a_failed_update_never_undoes_a_concurrent_one(ledger, sec, monkeypatch):
    """The failed update's new files go; the row (and the other writer's change) are untouched: nothing to restore."""
    i = ig.add(ledger, "anaf", "ANAF", {"client_id": "old-client", "client_secret": "old-secret"})
    other = Ledger()
    try:
        ig.update(other, i["id"], {"client_id": "winner-client", "client_secret": "winner-secret"})
    finally:
        other.close()
    real = ig.json.dumps
    monkeypatch.setattr(ig.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        ig.update(ledger, i["id"], {"client_secret": "loser-secret"})
    monkeypatch.setattr(ig.json, "dumps", real)
    inst = ig.default(ledger, "anaf")
    assert (inst.config["client_id"], inst.secret("client_secret")) == ("winner-client", "winner-secret")
    assert len(list(sec.glob(f"int-{i['id']}-*"))) == 1  # the loser's file did not stay


def test_a_crash_between_the_file_and_the_commit_leaves_the_old_pair(ledger, sec, home):
    """A process killed right after writing the new secret: the row still names the old file, which is intact."""
    i = ig.add(ledger, "anaf", "ANAF", {"client_id": "old-client", "client_secret": "old-secret"})
    code = f"""
import os
from fiscalm import integrations as ig, secrets
from fiscalm.ledger import Ledger
real = secrets.write_secret
def write_then_die(name, value):
    real(name, value)
    os._exit(9)
secrets.write_secret = write_then_die
ig.update(Ledger(), {i["id"]!r}, {{"client_id": "new-client", "client_secret": "new-secret"}})
"""
    env = os.environ | {"FISCALM_HOME": str(home), "FISCALM_SECRETS": str(sec)}
    assert subprocess.run([sys.executable, "-c", code], env=env, timeout=60).returncode == 9
    inst = ig.default(ledger, "anaf")
    assert (inst.config["client_id"], inst.secret("client_secret")) == ("old-client", "old-secret")
    ig.update(ledger, i["id"], {"client_id": "next"})  # the next write sweeps the orphan
    assert len(list(sec.glob(f"int-{i['id']}-*"))) == 1


def test_secret_files_must_be_private(sec):
    with pytest.raises(secrets.SecretError, match="not found"):
        secrets.read_secret("synthetic-secret")
    sec.mkdir()
    f = sec / "synthetic-secret"
    f.write_text("pw\n")
    os.chmod(f, 0o644)
    with pytest.raises(secrets.SecretError, match="readable"):
        secrets.read_secret("synthetic-secret")
    os.chmod(f, 0o600)
    assert secrets.read_secret("synthetic-secret") == "pw"


def test_a_secret_is_read_only_through_a_safe_open(ledger, sec, tmp_path, monkeypatch):
    i = ig.add(ledger, "ai", "Claude", {"provider": "anthropic", "api_key": KEY})
    outside = tmp_path / "outside"
    outside.write_text("not yours")
    outside.chmod(0o600)
    [f] = sec.glob(f"int-{i['id']}-api_key-*")
    f.unlink()
    f.symlink_to(outside)
    with pytest.raises(secrets.SecretError, match="not a regular file"):
        ig.default(ledger, "ai").secret("api_key")
    monkeypatch.delattr(ig.secrets.os, "O_NOFOLLOW")  # without it a link could not be refused: no read at all
    with pytest.raises(secrets.SecretError, match="O_NOFOLLOW"):
        ig.default(ledger, "ai").secret("api_key")


def test_anaf_reads_only_the_integration(ledger, sec, monkeypatch):
    monkeypatch.delenv("FISCALM_ANAF_ENV", raising=False)
    for name in ("anaf-client-id", "anaf-client-secret", "anaf-tokens.json"):  # files from before integrations
        secrets.write_secret(name, '{"access_token": "synthetic", "refresh_token": "synthetic"}')
    assert not anaf.configured(ledger) and anaf.env() == "test"
    ig.add(ledger, "anaf", "ANAF", {"client_id": "new-id", "client_secret": "new-secret"})
    inst = anaf.instance(ledger)
    assert (
        inst.config["client_id"] == "new-id" and inst.secret("client_secret") == "new-secret" and anaf.env() == "test"
    )
    (ledger.path.parent / "LIVE").touch()
    assert anaf.env() == "prod"
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")  # the environment wins (dev, tests)
    assert anaf.env() == "prod"


def test_bank_password_only_from_the_integration_its_default_wins(ledger, sec):
    secrets.write_secret("bt-NORD", "old-pass")  # a password file of the first install: never read
    with pytest.raises(secrets.SecretError, match="Setări, Firme"):
        secrets.bt_statement_password("NORD")
    assert not secrets.has_statement_password("NORD")
    ig.add(ledger, "bank", "BT NORD", {"bank": "bt", "company": "NORD", "statement_password": "new-pass"})
    assert secrets.bt_statement_password("NORD") == "new-pass" and secrets.has_statement_password("NORD")
    b = ig.add(ledger, "bank", "BT nou", {"bank": "bt", "company": "NORD", "statement_password": "newer-pass"})
    assert secrets.bt_statement_password("NORD") == "new-pass"
    ig.make_default(ledger, b["id"])
    assert secrets.bt_statement_password("NORD") == "newer-pass"
    with pytest.raises(secrets.SecretError):
        secrets.bt_statement_password("STUDIO9")  # no integration: refused


def test_cli_reads_secrets_from_stdin_prints_none_and_keeps_fields_apart(ledger, sec, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("leaked-into-config\n"))
    assert cli.main(["integration", "add", "ai", "--set", "provider=claude-cli", "--secret", "model"]) == 2
    assert "--set" in capsys.readouterr().err and ig.listing(ledger) == []
    assert cli.main(["integration", "add", "ai", "--set", "provider=anthropic", "--set", f"api_key={KEY}"]) == 2
    assert "--secret" in capsys.readouterr().err and ig.listing(ledger) == []
    assert cli.main(["integration", "add", "ai", "--set", "provider=nope"]) == 2
    assert "refused: Serviciu" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert (
        cli.main(["integration", "add", "ai", "--name", "Claude", "--set", "provider=anthropic", "--secret", "api_key"])
        == 0
    )
    out = capsys.readouterr().out
    assert KEY not in out and json.loads(out)["secrets"] == {"api_key": True}
    assert cli.main(["integration", "list"]) == 0 and KEY not in capsys.readouterr().out


# ---- through the app's API


def _items(port):
    code, r = _get_json(port, "/web/api/integrations")
    assert code == 200
    return r


def _log() -> str:
    return "\n".join(p.read_text() for p in (finance_home() / "logs").glob("*.log"))


def test_app_adds_changes_switches_and_removes_in_shadow_mode(home, sec, server):
    r = _items(server)
    assert (
        r["items"] == []
        and r["live"] is False
        and {k["key"] for k in r["kinds"]} == {k for k, v in ig.KINDS.items() if not v.firm}
    )
    assert [f["key"] for f in next(k for k in r["kinds"] if k["key"] == "ai")["fields"] if f["secret"]] == ["api_key"]
    assert _get_json(server, "/c/wrong/api/integrations")[0] == 404  # behind the token like every API route
    code, r = _api(
        server,
        "integration-add",
        {"co": "NORD", "kind": "ai", "name": "Claude", "f_provider": "anthropic", "f_api_key": KEY},
    )
    assert code == 200 and r["ok"] and r["message"] == "Integrarea a fost adăugată." and KEY not in json.dumps(r)
    item = _items(server)["items"][0]
    assert item["secrets"] == {"api_key": True} and item["default"] and KEY not in json.dumps(_items(server))
    iid = item["id"]
    assert r["done"] == {"id": iid}
    code, r = _api(
        server, "integration-update", {"co": "NORD", "id": iid, "f_model": "claude-sonnet-5-5", "f_api_key": ""}
    )
    assert r["ok"] and _items(server)["items"][0]["config"]["model"] == "claude-sonnet-5-5"
    L = Ledger()
    try:
        assert ig.default(L, "ai").secret("api_key") == KEY  # sent empty: kept
    finally:
        L.close()
    _api(
        server,
        "integration-add",
        {
            "co": "NORD",
            "kind": "ai",
            "name": "Local",
            "f_provider": "openai",
            "f_base_url": "http://127.0.0.1:11434/v1",
            "f_model": "qwen3",
        },
    )
    other = next(i["id"] for i in _items(server)["items"] if i["name"] == "Local")
    assert _api(server, "integration-default", {"co": "NORD", "id": other})[1]["ok"]
    assert [i["name"] for i in _items(server)["items"] if i["default"]] == ["Local"]
    code, r = _api(server, "integration-enable", {"co": "NORD", "id": other, "on": "0"})
    assert (
        r["message"] == "Integrarea a fost oprită."
        and not next(i for i in _items(server)["items"] if i["id"] == other)["enabled"]
    )
    assert _api(server, "integration-remove", {"co": "NORD", "id": iid})[1]["ok"]
    assert [i["name"] for i in _items(server)["items"]] == ["Local"]
    assert KEY not in _log() and "integration-add" in _log()


def test_app_refusals_say_why_and_change_nothing(home, sec, server):
    code, r = _api(server, "integration-add", {"co": "NORD", "kind": "ai", "name": "x", "f_provider": "anthropic"})
    assert code == 200 and not r["ok"] and r["message"].startswith("Cheie API") and _items(server)["items"] == []
    code, r = _api(server, "integration-update", {"co": "NORD", "id": "../../etc", "f_model": "x"})
    assert not r["ok"] and r["message"] == "integrarea nu există"
    code, r = _api(
        server,
        "integration-add",
        {"co": "NORD", "kind": "ai", "name": "x", "f_provider": "anthropic", "f_api_key": KEY, "f_nope": "1"},
    )
    assert not r["ok"] and _items(server)["items"] == [] and KEY not in json.dumps(r)
    code, r = _api(server, "integration-add", {"co": "NOPE", "kind": "ai", "f_provider": "anthropic", "f_api_key": KEY})
    assert code == 400  # the body rules of every POST still hold
    code, r = _api(
        server,
        "integration-add",
        {"co": "NORD", "kind": "ai", "f_provider": "anthropic", "f_api_key": KEY},
        Origin="https://evil.example",
    )
    assert code == 403 and _items(server)["items"] == []


@pytest.mark.parametrize(
    "scope,valid", [("all", True), ([], True), (["NORD"], True), (None, False), ([1], False), ("NORD", False)]
)
def test_action_scope_shapes(scope, valid):
    from fiscalm.api.actions import ACTIONS

    assert ACTIONS["integration-add"].valid({"kind": "mail", "firm_scope": scope}) is valid
    assert not ACTIONS["integration-enable"].valid({"id": "mail-12345678", "firm_scope": scope})


def test_http_scope_updates_and_coverage(home, sec, server):
    code, result = _api(
        server,
        "integration-add",
        {
            "kind": "anaf",
            "name": "Synthetic",
            "f_client_id": "synthetic",
            "f_client_secret": "synthetic-value",
            "firm_scope": ["NORD"],
        },
    )
    assert code == 200 and result["ok"]
    iid = result["done"]["id"]
    view = _items(server)
    assert view["items"][0]["firm_scope"] == ["NORD"]
    assert view["coverage"][0]["anaf"] == iid and view["coverage"][1]["anaf"] is None
    code, result = _api(server, "integration-update", {"id": iid, "firm_scope": []})
    assert code == 200 and result["ok"]
    assert _items(server)["coverage"][0]["anaf"] is None
