"""First-run HTTP contract, with conftest's isolated paths and a fake registry."""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date

import pytest
from firms import entries, set_firms
from test_api import _api, _get_json, _req, server as server  # noqa: F401
from test_selfhost_init import run_setup

from fiscalm import companies, company_config as cc, integrations, scheduler
from fiscalm.api import mcp, state as api, tokens, v1 as api_v1, web
from fiscalm.ledger import Ledger


def fiscalm_cli(*args, **env):
    return subprocess.run(
        [sys.executable, "-c", "import fiscalm; fiscalm.main()", *args],
        capture_output=True,
        text=True,
        env=dict(os.environ, **env),
        timeout=15,
    )


@pytest.fixture
def empty(home, monkeypatch):
    set_firms({})
    monkeypatch.setattr(
        cc,
        "lookup",
        lambda cui, today: dict(
            cui=cui, legal_name="Firma Exemplu SRL", inactive=False, vat_payer=True, address="Exemplu"
        ),
    )
    return home


@pytest.mark.parametrize("empty", [{}, []], indirect=True)
def test_setup_and_admin_flow(empty, server):
    web.APP_OUT.mkdir(parents=True)
    (web.APP_OUT / "index.html").write_text("<html>fiscalm</html>")
    assert _req(server, "GET", "/")[0] == 200
    expected = api.setup_state(True)
    assert _get_json(server, "/web/api/state") == (200, expected)
    assert _get_json(server, "/web/api/state?co=ALL") == (200, expected)
    assert _get_json(server, "/c/wrong/api/state")[0] == 404
    assert _api(server, "company-add", {"cui": "12345674"}, Origin="https://evil.example")[0] == 403
    assert _api(server, "company-add", {"cui": "12345674"}, ctype="text/plain")[0] == 415
    assert _api(server, "company-add", {"cui": "12345674", "name": "x" * 4096})[0] == 413
    # Every company action is admin-only, including preview and bank changes.
    assert not _get_json(server, "/web/api/state", user="readonly")[1]["can_manage_companies"]
    for action in ("company-lookup", "company-add", "company-update", "company-bank"):
        assert _api(server, action, {"cui": "12345674"}, user="readonly")[0] == 403
    assert _api(server, "company-lookup", {"cui": "12345674"})[1]["state"] == expected
    result = _api(server, "company-add", {"cui": "12345674"})[1]
    assert result["ok"] and "setup" not in result["state"]
    key = result["done"]["key"]
    assert companies.get(key).ron_iban == ""
    bank = {"key": key, "iban": "RO35TEST0000000000000000"}
    assert _api(server, "company-bank", bank)[1]["ok"]
    # Approved copy (2026-11-02): only the CLI replaces a paying account.
    assert _api(server, "company-bank", bank)[1]["message"] == (
        "Contul în lei este deja adăugat. Administratorul îl poate schimba din linia de comandă."
    )
    assert not (empty / "state/LIVE").exists()
    assert entries()["companies"][0]["key"] == key
    assert _get_json(server, "/web/api/state?co=ALL")[1]["companies"][0]["key"] == key


def test_refuse_company_work(empty, server, ledger, token_owner):
    credential = tokens.verify(ledger, tokens.create(ledger, "First", "write", user_id=token_owner)["token"])
    for route in ("invoicing", "upload-links"):
        assert _get_json(server, f"/web/api/{route}?co=EXAMPLE") == (
            409,
            {"ok": False, "message": companies.NEEDS_COMPANY_RO},
        )
    for action in ("check", "approve", "invoicing/draft-save"):
        assert _api(server, action, {"co": "EXAMPLE"}) == (409, {"ok": False, "message": companies.NEEDS_COMPANY_RO})
    for route in api_v1.ROUTES:
        if "{co}" not in route.path:
            continue
        with pytest.raises(api_v1.Error) as exc:
            api_v1.execute_as(ledger, credential, route, {"co": "EXAMPLE"}, {})
        assert (exc.value.status, exc.value.code, exc.value.message) == (409, "needs_company", companies.NEEDS_COMPANY)
    _, response = mcp.dispatch(
        ledger,
        {"scope": "read"},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_state", "arguments": {"co": "EXAMPLE"}},
        },
    )
    assert response["result"]["isError"]
    assert "needs_company" in response["result"]["content"][0]["text"]
    for command in (["sync"], ["report", "--company", "EXAMPLE", "--month", "2026-01"], ["invoice", "tick"]):
        result = fiscalm_cli(*command)
        assert result.returncode == 1 and result.stderr.strip() == companies.NEEDS_COMPANY


@pytest.mark.parametrize(
    "command,code,out,err",
    [
        (["status"], 0, "No companies configured. " + companies.NEEDS_COMPANY, ""),
        (["company", "check"], 3, "", ""),  # the tick's gate
        (["status", "--no-such-flag"], 2, "", "usage: fiscalm"),
        (["status", "extra"], 2, "", "usage: fiscalm"),
    ],
)
def test_empty_install_answers_valid_and_invalid_command_lines(empty, command, code, out, err):
    """A valid command line keeps its empty-install answer; an invalid one is a usage error before the ledger."""
    result = fiscalm_cli(*command)
    assert result.returncode == code
    assert result.stdout.strip() == out and result.stderr.startswith(err)


@pytest.mark.parametrize("second_cui", ["12345674", "12345682"])
def test_first_company_race(empty, monkeypatch, second_cui):
    barrier = threading.Barrier(2)

    def lookup(cui, today):
        barrier.wait(timeout=10)
        return dict(cui=cui, legal_name="Firma Exemplu SRL", inactive=False)

    monkeypatch.setattr(cc, "lookup", lookup)

    def add(cui):
        try:
            return cc.change(cui=cui, today=date(2026, 1, 1))
        except cc.CompanyError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(add, ["12345674", second_cui]))
    assert sum(r is not None for r in results) == 1
    assert len(entries()["companies"]) == 1


def test_init_preserves_existing_shadow_ledger(tmp_path):
    assert (
        run_setup(
            tmp_path,
            "init",
            "--company-key",
            "EXAMPLE",
            "--cui",
            "12345674",
            "--legal-name",
            "Example SRL",
            "--iban",
            "RO35TEST0000000000000000",
        ).returncode
        == 0
    )
    files = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*") if p.is_file()}
    assert any(p.suffix == ".db" for p in files)
    assert run_setup(tmp_path, "init").returncode == 0
    assert all(p.read_bytes() == before for p, before in files.items())
    assert not (tmp_path / "data/state/LIVE").exists()


def test_tick_refuses_before_ledger(empty, tmp_path):
    root = tmp_path / "tick-home"
    result = subprocess.run(
        [sys.executable, "-m", "fiscalm", "tick"],
        env=dict(os.environ, FISCALM_HOME=str(root)),
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "No companies configured; skipping tick." in next((root / "logs").glob("*.log")).read_text()
    assert not list(root.rglob("*.db"))


def test_install_management_while_empty(empty, server):
    assert _get_json(server, "/web/api/companies")[1]["companies"] == []
    assert _get_json(server, "/web/api/tokens")[0] == 200
    assert _get_json(server, "/web/api/integrations")[1]["items"] == []
    code, result = _api(server, "token-create", {"name": "Synthetic access", "scope": "read"})
    assert code == 200 and result["ok"] and result["state"] == api.setup_state(True)
    assert _api(server, "token-revoke", {"id": result["done"]["id"]})[1]["ok"]
    code, result = _api(
        server,
        "integration-add",
        {
            "kind": "ai",
            "name": "Synthetic AI",
            "f_provider": "openai",
            "f_model": "synthetic",
            "f_base_url": "http://localhost:1234",
            "f_api_key": "synthetic",
        },
    )
    assert code == 200 and result["ok"]
    iid = _get_json(server, "/web/api/integrations")[1]["items"][0]["id"]
    for action, fields in [
        ("integration-update", {"name": "Renamed AI"}),
        ("integration-enable", {"on": "1"}),
        ("integration-clear", {"key": "api_key"}),
        ("integration-default", {}),
        ("integration-remove", {}),
    ]:
        code, result = _api(server, action, {"id": iid, **fields})
        assert code == 200 and result["ok"], result
    assert _get_json(server, "/web/api/integrations")[1]["items"] == []
    assert _get_json(server, "/web/api/tokens", user="readonly") == (200, [])
    assert _api(server, "token-create", {"name": "Reader", "scope": "read"}, user="readonly")[1]["ok"]


def test_company_management_is_for_admins_only(empty, server, ledger):
    assert cc.change(cui="12345674", name="EXAMPLE", today=date(2026, 1, 1))["key"]
    assert web.require("u_admin", "manage_firms") and not web.require("missing", "manage_firms")
    for user, allowed in [("admin", True), ("readonly", False)]:
        for co in ("ALL", "EXAMPLE"):
            state = _get_json(server, f"/web/api/state?co={co}", user=user)[1]
            assert state["can_manage_companies"] is allowed
            assert all(p["can_manage_companies"] is allowed for p in state.get("parts", []))
    value = api_v1.public_state(api.state(ledger, "ALL"), ledger)
    assert "can_manage_companies" not in value
    assert all("can_manage_companies" not in p for p in value.get("parts", []))


@pytest.mark.parametrize("value", ["1m", "0", "-1", "soon"])
def test_tick_refuses_a_check_timeout_that_is_not_seconds(tmp_path, monkeypatch, value):
    monkeypatch.setenv("FISCALM_COMPANY_CHECK_TIMEOUT", value)
    with pytest.raises(SystemExit, match="FISCALM_COMPANY_CHECK_TIMEOUT"):
        scheduler.tick(tmp_path / "home", ["false"])
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("check_rc", [0, 1, "sleep"])
@pytest.mark.parametrize("has_ledger", [False, True])
def test_tick_check_failure_stops_pipeline(tmp_path, monkeypatch, check_rc, has_ledger):
    calls = tmp_path / "calls"
    stub = tmp_path / "fiscalm"
    stub.write_text(
        '#!/bin/bash\necho "$*" >> "$CALLS"\n'
        'if [ "$*" = "company check" ]; then\n'
        '  if [ "$CHECK_RC" = sleep ]; then sleep 30; else exit "$CHECK_RC"; fi\nfi\n'
        'if [ "$*" = "status" ]; then exit 1; fi\n'
        'echo "{}"\n'
    )
    stub.chmod(0o700)
    state = tmp_path / "home/state"
    state.mkdir(parents=True)
    if has_ledger:
        (state / "ledger.db").touch()
    cleanup = ["auth sweep"] if has_ledger else []
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("CHECK_RC", str(check_rc))
    monkeypatch.setenv("FISCALM_COMPANY_CHECK_TIMEOUT", "0.1")
    rc = scheduler.tick(tmp_path / "home", [str(stub)])
    expected_rc = 124 if check_rc == "sleep" else check_rc
    assert rc == expected_rc
    if expected_rc:
        assert calls.read_text().splitlines() == cleanup + ["company check"]
        log = next((tmp_path / "home/logs").glob("*.log")).read_text()
        assert f"company check failed (rc={expected_rc})" in log
        return
    assert calls.read_text().splitlines() == cleanup + [
        "company check",
        "update-check",
        "pay redate",
        "sync",
        "efactura sync",
        "efactura sync-issued",
        "tx-rules",
        "efactura judge",
        f"invoicing recur run --today {date.today().isoformat()}",
        "shopify-sync",
        "invoice tick",
        "emag tick",
        "invoice partner-status",
    ]


@pytest.mark.parametrize(
    "left", [{"mode": "done", "company": "done"}, dict.fromkeys(("mode", "company", "anaf"), "done")]
)
def test_an_install_that_left_the_old_wizard_opens_as_usual(empty, server, left):
    key = _api(server, "company-add", {"cui": "12345674"})[1]["done"]["key"]
    with closing(Ledger()) as L:  # the old wizard's progress, stopped midway or finished
        L.set_meta("onboarding", json.dumps({"completed": left, "_cursor": "bank", "revision": 3}))
    code, state = _get_json(server, f"/web/api/state?co={key}")
    assert code == 200 and state["co"] == key and "onboarding" not in state
    assert [i["key"] for i in state["setup"]["left"]] == ["bank", "history", "shops"]
    assert [c["key"] for c in state["companies"]] == [key] and not (empty / "state/LIVE").exists()


def test_setup_card_lists_what_is_left_per_firm_until_done_or_hidden(empty, server):
    first, second = (_api(server, "company-add", {"cui": cui})[1]["done"]["key"] for cui in ("12345674", "12345682"))

    def left(co, user="admin"):
        card = _get_json(server, f"/web/api/state?co={co}", user=user)[1]["setup"]
        return card and not card["hidden"] and [(i["key"], i["int"]) for i in card["left"]]

    everything = [
        ("bank", None),
        ("history", None),
        ("shops", "new:shop"),
    ]
    assert left(first) == left(second) == everything
    assert left(first, "member") is None
    assert "setup" not in _get_json(server, "/web/api/state?co=ALL")[1]
    with closing(Ledger()) as L:
        integrations.add(L, "shop", "Magazin exemplu", dict(platform="custom", company=first, series="TEST"))
    assert ("shops", "new:shop") not in left(first) and left(second) == everything
    with closing(Ledger()) as L:  # Installation AI never changes either firm's checklist
        cli = integrations.add(L, "ai", "Claude Code", dict(provider="claude-cli"))
        integrations.set_enabled(L, cli["id"], False)
    assert left(second) == everything
    assert _api(server, "setup-card-dismiss", {"co": first}, user="member")[0] == 403
    for _ in range(2):  # dismissing again changes nothing
        assert _api(server, "setup-card-dismiss", {"co": first})[1]["ok"]
    assert left(first) is False and left(second) == everything
    assert _api(server, "setup-card-show", {"co": first}, user="member")[0] == 403
    assert _api(server, "setup-card-show", {"co": first})[1]["ok"]
    assert left(first) and left(second)
