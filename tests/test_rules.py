"""Automatizări: the rules a firm runs on its own (supplier autopay, SPV upload, receipt links, the accountant's pack),
who may pause or resume them, and the refusals that keep a paused or doubtful rule from paying. Synthetic data only."""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, current_thread

import pytest
from test_api import (
    _api,  # noqa: F401
    server as server,
    world as world,
)
from test_api_v1 import request
from test_efactura import OTHER_IBAN, _stored, ubl

from fiscalm import companies, pack, rules
from fiscalm.api import (
    actions,
    mcp_tools,
    schema as api_schema,
    state as api,
    tokens,
    uploads as upload_links,
    v1 as api_v1,
    web,
)
from fiscalm.efactura import inbox as efactura
from fiscalm.invoicing import spv as invoice_spv
from fiscalm.ledger import Ledger
from fiscalm.payments import engine as pay


def test_rules_state_and_contract(ledger, world):
    upload_links.create(ledger, "NORD", "Receipt link")
    ledger.set_pack("NORD", "2026-09", "prepared")
    state = api.state(ledger, "NORD")
    assert {r["kind"] for r in state["rules"]} >= {"spv_auto_upload", "accountant_pack", "receipt_upload"}
    assert all(r["company"] == "NORD" for r in state["rules"])
    link = next(r for r in state["rules"] if r["kind"] == "receipt_upload")
    assert link["enabled"] and link["last"] is None and not link["can_pause"]
    assert "secret" not in str(link)
    pack = next(r for r in state["rules"] if r["kind"] == "accountant_pack")
    assert pack["last"]["action"] == "download" and pack["last"]["count"] >= 1
    assert api_v1.public_state(state, ledger)["rules"] == state["rules"]
    assert api_schema.schemas()["State"]["properties"]["rules"]["items"] == {"$ref": "#/components/schemas/Rule"}
    assert all("rules" in part for part in api.state(ledger, "ALL")["parts"])


def test_supplier_pause_resume_scope_and_refusal(ledger, home, monkeypatch, server):
    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(issue="2026-09-15", supplier="SYNTHETIC SERVICES SRL"),
            "3002": ubl(supplier="SYNTHETIC SERVICES SRL"),
        },
    )
    efactura.set_autopay(ledger, "3001")
    ident = "NORD:supplier_autopay:99999999"
    rules.set_enabled(ledger, "NORD", ident, False)
    rules.set_enabled(ledger, "NORD", ident, False)
    assert efactura.autopay_new(ledger) == []
    assert not next(r for r in rules.list_rules(ledger, "NORD") if r["id"] == ident)["enabled"]
    assert not efactura.autopay_paused(ledger, "STUDIO9", "99999999")
    with pytest.raises(ValueError):
        rules.set_enabled(ledger, "STUDIO9", ident, True)
    rules.set_enabled(ledger, "NORD", ident, True)
    rules.set_enabled(ledger, "NORD", ident, True)
    rules.set_enabled(ledger, "NORD", ident, False)
    _stored(
        ledger, monkeypatch, **{"3003": ubl(iban=OTHER_IBAN, issue="2026-10-25", supplier="SYNTHETIC SERVICES SRL")}
    )


def test_actions_guards_and_double_submit(token_owner, ledger, server, world, home, monkeypatch):
    ident = "NORD:spv_auto_upload"
    body = {"co": "NORD", "id": ident}
    for _ in range(2):  # a repeat succeeds and says the same
        assert _api(server, "rule-pause", body)[1] | {"state": None} == {
            "ok": True,
            "message": "Regula a fost oprită.",
            "done": {"id": ident, "enabled": False},
            "state": None,
            "next": None,
        }
    assert not next(r for r in rules.list_rules(ledger, "NORD") if r["id"] == ident)["enabled"]
    assert not _api(server, "rule-resume", body | {"co": "STUDIO9"})[1]["ok"]
    assert _api(server, "rule-resume", body, Origin="https://evil.example")[0] == 403
    assert _api(server, "rule-resume", body | {"pad": "x" * 4096})[0] == 413
    assert _api(server, "rule-resume", body, ctype="text/plain")[0] == 415
    for _ in range(2):
        assert _api(server, "rule-resume", body)[1]["message"] == "Regula a fost pornită."
    (home / "state" / "LIVE").unlink()
    assert actions.act("rule-pause", "NORD", body)["ok"]
    assert _api(server, "rule-pause", body)[1]["ok"]
    assert not next(r for r in rules.list_rules(ledger, "NORD") if r["id"] == ident)["enabled"]
    ledger.db.execute("UPDATE users SET role='member' WHERE id='u_admin'")
    ledger.db.commit()
    assert _api(server, "rule-pause", body)[0] == 403
    secret = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    assert request(server, secret, "/companies/NORD/actions/rule-pause", "POST", {"id": ident})[0] == 400


def test_the_spv_day_is_the_running_rules_own_setting(ledger, server, world):
    spv_rule = lambda: next(r for r in rules.list_rules(ledger, "NORD") if r["kind"] == "spv_auto_upload")  # noqa: E731
    assert (spv_rule()["settings"], rules.capabilities(spv_rule(), admin=True)["can_edit"]) == (
        {"upload_after_days": 4},
        True,
    )
    assert _api(server, "rule-spv-delay", {"co": "NORD", "days": "2"})[1] | {"state": None} == {
        "ok": True,
        "message": rules.SPV_DELAY_SAVED,
        "done": {"upload_after_days": 2},
        "state": None,
        "next": None,
    }
    assert spv_rule()["settings"] == {"upload_after_days": 2}
    assert invoice_spv.upload_delay(ledger, "STUDIO9") == 4  # each firm its own
    invoice_spv.set_auto_upload(ledger, "NORD", False)
    assert (spv_rule()["settings"], rules.capabilities(spv_rule(), admin=True)["can_edit"]) == (None, False)
    refused = _api(server, "rule-spv-delay", {"co": "NORD", "days": "1"})[1]
    assert not refused["ok"] and invoice_spv.upload_delay(ledger, "NORD") == 2
    invoice_spv.set_auto_upload(ledger, "NORD", True)
    assert spv_rule()["settings"] == {"upload_after_days": 2}  # back on, the day it had


def test_reasons_by_kind_revoked_link_and_company_spv_setting(ledger, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL")})
    efactura.set_autopay(ledger, "3001")
    made = upload_links.create(ledger, "NORD", "Receipt link")
    by_kind = {r["kind"]: r for r in rules.list_rules(ledger, "NORD")}
    assert [
        by_kind[k]["reason"] for k in ("supplier_autopay", "receipt_upload", "spv_auto_upload", "accountant_pack")
    ] == [None, None, "shadow_mode", None]  # only what has an outward effect waits for live
    upload_links.revoke(ledger, made["id"], "NORD")
    invoice_spv.set_auto_upload(ledger, "NORD", False)
    rows = rules.list_rules(ledger, "NORD")
    assert next(r for r in rows if r["kind"] == "receipt_upload")["reason"] == "revoked"
    assert not next(r for r in rows if r["kind"] == "spv_auto_upload")["enabled"]
    assert next(r for r in rules.list_rules(ledger, "STUDIO9") if r["kind"] == "spv_auto_upload")["enabled"]
    # with no ANAF connection the rule waits for one, in either mode
    monkeypatch.setattr(rules.anaf, "token_state", lambda L, **kwargs: {"authorized": False, "status": "not_set_up"})
    assert (
        next(r for r in rules.list_rules(ledger, "NORD") if r["kind"] == "spv_auto_upload")["reason"]
        == "anaf_not_connected"
    )


def test_shared_admin_policy_and_capabilities(token_owner, ledger, server, world, home):
    body = {"co": "NORD", "id": "NORD:spv_auto_upload"}
    for live in (True, False):
        if not live:
            (home / "state" / "LIVE").unlink()
        assert _api(server, "rule-pause", body)[1]["ok"]
        admin = api.state(ledger, "NORD", can_manage_companies=True)
        assert next(r for r in admin["rules"] if r["id"] == body["id"])["can_resume"]
        assert not any(r["can_resume"] or r["can_pause"] for r in api.state(ledger, "NORD")["rules"])
        assert not any(r["can_resume"] or r["can_pause"] for r in api_v1.public_state(admin, ledger)["rules"])
        assert _api(server, "invoicing/auto-upload", {"co": "NORD", "on": True})[1]["ok"]
        assert _api(server, "rule-resume", body)[1]["ok"]
    ledger.db.execute("UPDATE users SET role='member' WHERE id='u_admin'")
    ledger.db.commit()
    for action, args in [("rule-pause", body), ("invoicing/auto-upload", {"co": "NORD", "on": False})]:
        code, value = _api(server, action, args)
        assert code == 403 and value["message"] == rules.ADMIN_REFUSAL
    secret = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    assert request(server, secret, "/companies/NORD/invoicing/auto-upload", "POST", {"on": False})[0] == 403


@pytest.mark.parametrize("currency,reason,code", [("EUR", "invoice_refused", "not_ron")])
def test_a_refusal_other_than_a_changed_account_does_not_block_resume(ledger, monkeypatch, currency, reason, code):
    """Each invoice is still checked, so the supplier can be resumed; an account no statement or administrator vouched
    for yet reads as unproved, not as a changed IBAN. Nothing is paid."""
    _stored(ledger, monkeypatch, **{"3001": ubl(currency=currency, supplier="SYNTHETIC SERVICES SRL")})
    efactura.set_autopay(ledger, "3001")
    ident = "NORD:supplier_autopay:99999999"
    rules.set_enabled(ledger, "NORD", ident, False)
    rule = next(r for r in api.state(ledger, "NORD", can_manage_companies=True)["rules"] if r["id"] == ident)
    assert rule["reason"] == reason and rule["can_resume"]
    rules.set_enabled(ledger, "NORD", ident, True)
    assert [r["status"] for r in efactura.autopay_new(ledger)] == ["refused"]
    assert efactura.stored_refusal(ledger.get_meta("efactura_autopay_refused:3001")) == (code, None)
    assert ledger.efactura("3001")["status"] == "open"


def test_pause_serializes_with_autopay_scan(ledger, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL")})
    efactura.set_autopay(ledger, "3001")
    pause_writing, auto_attempted, scan_entered, release_pause = (Event() for _ in range(4))
    original_lock, original_scan = pay.pay_lock, efactura._autopay_new

    @contextmanager
    def observed_lock():
        if current_thread().name.startswith("auto"):
            auto_attempted.set()
        with original_lock():
            yield

    def observed_scan(L):
        scan_entered.set()
        return original_scan(L)

    def pause():
        L = Ledger()
        original_set = L.set_meta

        def hold_write(key, value):
            pause_writing.set()
            assert release_pause.wait(5)
            original_set(key, value)

        L.set_meta = hold_write
        try:
            efactura.set_autopay_enabled(L, "NORD", "99999999", False)
        finally:
            L.close()

    def autopay():
        L = Ledger()
        try:
            return efactura.autopay_new(L)
        finally:
            L.close()

    monkeypatch.setattr(pay, "pay_lock", observed_lock)
    monkeypatch.setattr(efactura, "_autopay_new", observed_scan)
    with (
        ThreadPoolExecutor(1, thread_name_prefix="pause") as pauses,
        ThreadPoolExecutor(1, thread_name_prefix="auto") as autos,
    ):
        p = pauses.submit(pause)
        try:
            assert pause_writing.wait(5)
            a = autos.submit(autopay)
            assert auto_attempted.wait(5)
            assert not scan_entered.wait(0.2), "autopay must wait for the pause lock before reading configuration"
        finally:
            release_pause.set()
        p.result(timeout=5)
        assert a.result(timeout=5) == []
    assert ledger.efactura("3001")["status"] == "open"


def test_rule_reason_enum_matches_typescript():
    text = (Path(__file__).parents[1] / "app/src/lib/types.ts").read_text()
    union = re.search(r"  reason: (.+);", text).group(1)
    expected = set(re.findall(r'"([a-z_]+)"', union)) | {None}
    assert set(api_schema.schemas()["Rule"]["properties"]["reason"]["enum"]) == expected
    assert "rules: Rule[]" in text and "rules?:" not in text


def test_bearer_projection_calls_shared_policy(ledger, monkeypatch):
    seen = []
    original = rules.may_change

    def policy(*, admin=False, bearer=False):
        seen.append((admin, bearer))
        return original(admin=admin, bearer=bearer)

    monkeypatch.setattr(rules, "may_change", policy)
    state = {"rules": [{"kind": "supplier_autopay", "enabled": True, "reason": None}]}
    result = api_v1.public_state(state, ledger)
    assert seen == [(False, True)]
    assert not result["rules"][0]["can_pause"]


def test_autopay_waiting_queue_uses_stored_validation(ledger, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL")})
    efactura.set_autopay(ledger, "3001")
    ident = "NORD:supplier_autopay:99999999"
    rules.set_enabled(ledger, "NORD", ident, False)
    _stored(
        ledger,
        monkeypatch,
        **{
            "3002": ubl(number="SYN-2", supplier="SYNTHETIC SERVICES SRL"),
            "3003": ubl(number="SYN-3", currency="EUR", supplier="SYNTHETIC SERVICES SRL"),
        },
    )

    def supplier():
        return next(r for r in rules.list_rules(ledger, "NORD") if r["id"] == ident)

    with monkeypatch.context() as cheap:
        cheap.setattr(efactura, "payment_item", lambda *_: pytest.fail("state built a payment row"))
        assert supplier()["waiting_queue"] == 2
        assert all(r["waiting_queue"] == 0 for r in rules.list_rules(ledger, "STUDIO9"))
    assert efactura.autopay_new(ledger) == []
    rules.set_enabled(ledger, "NORD", ident, True)
    assert supplier()["waiting_queue"] == 0
    results = efactura._autopay_new(ledger)
    assert sum(bool(r.get("item_id")) for r in results) == 2
    assert ledger.efactura("3003")["status"] == "open"


def test_legacy_bearer_configuration_is_documented_as_forbidden():
    operation = api_schema.openapi()["paths"]["/api/v1/companies/{co}/invoicing/auto-upload"]["post"]
    assert "200" not in operation["responses"] and "403" in operation["responses"]
    assert "admin_required" in operation["description"]
    tool = next(t for t in mcp_tools.definitions("write") if t["name"] == "invoicing_auto_upload")
    assert "403 admin_required" in tool["description"]


@pytest.mark.parametrize(
    "codes,reason",
    [
        (("invoice_issues", "not_ron"), "invoice_refused"),
        (("not_ron", "supplier_inactive"), "supplier_inactive"),
    ],
)
def test_autopay_rule_reason_is_the_most_serious_refusal_code(ledger, monkeypatch, codes, reason):
    """The rule's reason comes from check_payment's codes, never its messages; the most serious refused invoice names it."""
    _stored(
        ledger,
        monkeypatch,
        **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL"), "3002": ubl(number="B2", supplier="SYNTHETIC SERVICES SRL")},
    )
    efactura.set_autopay(ledger, "3001")
    refusals = dict(zip(("3001", "3002"), codes, strict=True))

    def check(L, r):
        raise efactura.ApproveError(
            refusals[r["message_id"]], "supplier IBAN differs from proved history"
        )  # the text never counts

    monkeypatch.setattr(efactura, "check_payment", check)
    rule = next(r for r in rules.list_rules(ledger, "NORD") if r["id"] == "NORD:supplier_autopay:99999999")
    assert rule["reason"] == reason


# ---- the tick on a supplier: per firm, from the app, picked from the firm's own invoices


def test_supplier_autopay_is_each_firms_own_choice(ledger, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL"), "3002": ubl(number="s9", supplier="SYNTHETIC SERVICES SRL")},
    )
    ledger.db.execute("UPDATE efactura SET company='STUDIO9' WHERE message_id='3002'")
    ledger.db.commit()
    done = efactura.set_supplier_autopay(ledger, "NORD", "99999999", True)
    assert done["name"] == "SYNTHETIC SERVICES SRL" and done["waiting"] == 1
    assert efactura.autopay_suppliers(ledger, "NORD") == {"99999999": "SYNTHETIC SERVICES SRL"}
    assert efactura.autopay_suppliers(ledger, "STUDIO9") == {}  # the other firm's invoices stay manual
    assert [r["company"] for r in rules.list_rules(ledger, "NORD") if r["kind"] == "supplier_autopay"] == ["NORD"]
    assert not [r for r in rules.list_rules(ledger, "STUDIO9") if r["kind"] == "supplier_autopay"]
    nord = next(p for p in api.state(ledger, "NORD")["partners"] if p["cui"] == "99999999")
    s9 = next(p for p in api.state(ledger, "STUDIO9")["partners"] if p["cui"] == "99999999")
    assert (nord["autopay"], s9["autopay"]) == (True, False)
    efactura.set_supplier_autopay(ledger, "NORD", "99999999", False)
    assert efactura.autopay_suppliers(ledger, "NORD") == {} and efactura.autopay_paused(ledger, "NORD", "99999999")


def test_only_a_supplier_the_firm_has_invoices_from_can_be_ticked(ledger, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL")})
    for co, cui in (("STUDIO9", "99999999"), ("NORD", "12345678"), ("NORD", "")):
        with pytest.raises(efactura.ApproveError):
            efactura.set_supplier_autopay(ledger, co, cui, True)
    assert efactura.autopay_suppliers(ledger, "NORD") == {} and efactura.autopay_suppliers(ledger, "STUDIO9") == {}
    client = next(p for p in api.state(ledger, "NORD")["partners"] if p["cui"] == "99999999")
    assert client["autopay"] is False and not client["can_set_autopay"]  # not an admin


def test_a_paused_supplier_is_not_ticked_and_ticking_it_resumes_it(ledger, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL")})
    efactura.set_supplier_autopay(ledger, "NORD", "99999999", True)
    rules.set_enabled(ledger, "NORD", "NORD:supplier_autopay:99999999", False)
    assert efactura.autopay_paused(ledger, "NORD", "99999999")
    p = next(p for p in api.state(ledger, "NORD")["partners"] if p["cui"] == "99999999")
    assert p["autopay"] is False  # stopped in Automatizări: the sheet doesn't say it runs
    efactura.set_supplier_autopay(ledger, "NORD", "99999999", True)
    assert not efactura.autopay_paused(ledger, "NORD", "99999999")
    assert next(p for p in api.state(ledger, "NORD")["partners"] if p["cui"] == "99999999")["autopay"] is True


def test_the_old_install_wide_list_becomes_each_firms_own(ledger, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL")})
    ledger.set_meta("efactura_autopay", json.dumps({"99999999": "SYNTHETIC SERVICES SRL", "55555555": "NOBODY SRL"}))
    assert efactura.autopay_suppliers(ledger, "NORD") == {"99999999": "SYNTHETIC SERVICES SRL"}
    assert efactura.autopay_suppliers(ledger, "STUDIO9") == {}  # no invoice from it there: starts off, not inherited
    assert ledger.get_meta("efactura_autopay") is None
    assert efactura.autopay_suppliers(ledger, "NORD") == {"99999999": "SYNTHETIC SERVICES SRL"}  # once only


def test_the_supplier_autopay_action(ledger, server, home, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL")})
    assert "rule-supplier-autopay" not in actions.API_ACTIONS and "rule-supplier-autopay" in actions.LOCAL_ACTIONS
    code, r = _api(server, "rule-supplier-autopay", {"co": "NORD", "cui": "99999999", "on": "1"})
    assert (
        r["ok"]
        and r["message"]
        == actions.AUTOPAY_ON.format(name="SYNTHETIC SERVICES SRL")
        + " O factură deschisă intră la următoarea verificare."
    )
    assert efactura.autopay_suppliers(ledger, "NORD") == {
        "99999999": "SYNTHETIC SERVICES SRL"
    }  # works before going live
    code, r = _api(server, "rule-supplier-autopay", {"co": "NORD", "cui": "99999999", "on": "0"})
    assert r["ok"] and r["message"] == actions.AUTOPAY_OFF.format(name="SYNTHETIC SERVICES SRL")
    for body in (
        {"co": "NORD", "cui": "99999999", "on": "yes"},
        {"co": "NORD", "cui": "99999999", "on": "1", "x": "1"},
        {"co": "STUDIO9", "cui": "99999999", "on": "1"},
    ):
        assert not _api(server, "rule-supplier-autopay", body)[1]["ok"]
    assert efactura.autopay_suppliers(ledger, "NORD") == {} and efactura.autopay_suppliers(ledger, "STUDIO9") == {}
    monkeypatch.setattr(web.Handler, "_require", lambda self, need: need != "manage_rules")
    code, r = _api(server, "rule-supplier-autopay", {"co": "NORD", "cui": "99999999", "on": "1"})
    assert not r["ok"] and efactura.autopay_suppliers(ledger, "NORD") == {}


def test_a_partner_ticked_not_mailed_stays_out_of_the_pack(ledger, server, monkeypatch):
    ledger.add_document(
        "sha-ai",
        "NORD",
        "2026-10",
        "receipt",
        "/nonexistent/ai.pdf",
        vendor="Synthetic AI Inc",
        doc_date="2026-10-05",
        amount="20.00",
        currency="USD",
        source="mail:synthetic",
    )
    ledger.add_document("sha-st", "NORD", "2026-10", "statement", "/nonexistent/st.pdf", vendor="Synthetic AI Inc")
    partner = "NORD.n-synthetic-ai-inc"
    ledger.set_pack("NORD", "2026-09", "prepared")  # a prepared snapshot waits: its selection cannot change
    assert (
        _api(server, "rule-partner-not-mailed", {"co": "NORD", "partner": partner, "on": "1"})[1]["message"]
        == actions.NOT_MAILED_PREPARED
    )
    ledger.set_pack("NORD", "2026-09", "sent")
    got = []
    with pay.pay_lock():  # a snapshot being prepared holds the lock until it is recorded
        tick = threading.Thread(
            target=lambda: got.append(
                _api(server, "rule-partner-not-mailed", {"co": "NORD", "partner": partner, "on": "1"})[1]
            )
        )
        tick.start()
        tick.join(0.3)
        assert tick.is_alive()  # the tick waits for it
        ledger.set_pack("NORD", "2026-10", "prepared")
    tick.join()
    assert got[0]["message"] == actions.NOT_MAILED_PREPARED
    ledger.set_pack("NORD", "2026-10", "built")
    r = _api(server, "rule-partner-not-mailed", {"co": "NORD", "partner": partner, "on": "1"})[1]
    assert r["ok"] and r["message"] == actions.NOT_MAILED_ON.format(name="Synthetic AI Inc")  # before going live too
    s = api.state(ledger, "NORD")
    assert [d["sha"] for d in pack.pending_docs(ledger, "NORD", "2026-10")] == ["sha-st"]  # never a statement
    assert next(p for p in s["partners"] if p["id"] == partner)["not_mailed"]
    assert next(m for m in s["reports"] if m["month"] == "2026-10")["not_mailed"] == ["Synthetic AI Inc"]
    for body in (
        {"co": "NORD", "partner": "NORD.n-nobody", "on": "1"},
        {"co": "STUDIO9", "partner": partner, "on": "0"},
        {"co": "NORD", "partner": partner, "on": "yes"},
    ):
        assert not _api(server, "rule-partner-not-mailed", body)[1]["ok"]
    monkeypatch.setattr(web.Handler, "_require", lambda self, need: need != "manage_rules")
    assert not _api(server, "rule-partner-not-mailed", {"co": "NORD", "partner": partner, "on": "0"})[1]["ok"]
    assert pack.not_mailed(ledger, "NORD") == {partner}


def test_the_migration_through_a_state_read_never_replays_a_finished_one(ledger, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{"3001": ubl(supplier="SYNTHETIC SERVICES SRL"), "3002": ubl(number="s9", supplier="SYNTHETIC SERVICES SRL")},
    )
    ledger.db.execute("UPDATE efactura SET company='STUDIO9' WHERE message_id='3002'")
    ledger.db.commit()
    ledger.set_meta("efactura_autopay", json.dumps({"99999999": "SYNTHETIC SERVICES SRL"}))
    reads = api._StateReads(ledger, companies.all(ledger))
    assert reads.get_meta("efactura_autopay")  # the state read has cached the legacy list
    other = Ledger(ledger.path)
    try:  # another connection migrates it, then the user turns the supplier off in NORD
        assert efactura.autopay_suppliers(other, "STUDIO9") == {"99999999": "SYNTHETIC SERVICES SRL"}
        efactura.set_supplier_autopay(other, "NORD", "99999999", False)
    finally:
        other.close()
    assert efactura.autopay_suppliers(reads, "NORD") == {}  # not turned back on from the cached list
    assert efactura.autopay_suppliers(reads, "STUDIO9") == {"99999999": "SYNTHETIC SERVICES SRL"}
    assert ledger.get_meta("efactura_autopay") is None
