"""Public registry cache uses synthetic CUIs and a fake ANAF transport only."""

import json
from datetime import date

import pytest
from payment_cases import download_batch
from synthetic_world import _ef

from fiscalm.api import state as api
from fiscalm.company_config import valid_cui
from fiscalm.efactura import partners as registry
from fiscalm.payments import engine as pay

TODAY = date(2026, 11, 2)
CUIS = [str(n) for n in range(9000000, 9003000) if valid_cui(str(n))][:101]


def client(L, cui=CUIS[0], **extra):
    data = dict(name="Firma Inventata SRL", country="RO", cui=cui, **extra)
    L.db.execute("INSERT INTO invoice_clients VALUES(?,?,?,?)", (cui, "NORD", cui, json.dumps(data)))
    L.db.commit()


def row(cui, day):
    return dict(
        date_generale=dict(cui=cui, data=day, statusRO_e_Factura=True, stare_inregistrare="INREGISTRAT"),
        inregistrare_scop_Tva=dict(
            scpTVA=True, perioade_TVA=[dict(data_inceput_ScpTVA="2020-01-01", data_sfarsit_ScpTVA="")]
        ),
        stare_inactiv=dict(statusInactivi=True, dataRadiere=""),
        inregistrare_RTVAI=dict(statusTvaIncasare=False),
    )


class Anaf:
    def __init__(self):
        self.calls = []

    def __call__(self, url, body):
        batch = json.loads(body)
        self.calls.append(batch)
        assert len(batch) <= 100
        return json.dumps(dict(cod=200, found=[row(str(c["cui"]), c["data"]) for c in batch], notFound=[])).encode()


def test_cache_hit_stale_refresh_and_shadow_mode(ledger):
    from fiscalm.ledger import is_live

    assert not is_live()
    client(ledger)
    fetch = Anaf()
    assert registry.refresh(ledger, TODAY, fetch) == dict(attempted=1, errors=0)
    assert registry.refresh(ledger, date(2026, 11, 8), fetch)["attempted"] == 0
    assert registry.refresh(ledger, date(2026, 11, 9), fetch)["attempted"] == 1
    status, error = registry.cached(ledger, CUIS[0])
    assert status == dict(
        checked_at="2026-11-09",
        vat_payer=True,
        vat_since="2020-01-01",
        efactura_registered=True,
        inactive=True,
        inactive_register_removed_at=None,
        registration_status="INREGISTRAT",
        vat_on_collection=False,
    )
    assert error is None and len(fetch.calls) == 2


@pytest.mark.parametrize("failure", ["network", "malformed", "absent", "bad_flag", "wrong_date", "duplicate"])
def test_failed_refresh_retains_data_and_throttles_retries(ledger, failure):
    client(ledger)
    registry.refresh(ledger, TODAY, Anaf())
    old, _ = registry.cached(ledger, CUIS[0])

    def fail(url, body):
        if failure == "network":
            raise OSError("untrusted error text")
        r = row(CUIS[0], "2026-11-09")
        if failure == "bad_flag":
            r["stare_inactiv"]["statusInactivi"] = "false"
        if failure == "wrong_date":
            r["date_generale"]["data"] = "2026-11-08"
        return (
            b"{"
            if failure == "malformed"
            else json.dumps(
                dict(cod=200, found=[] if failure == "absent" else [r, r] if failure == "duplicate" else [r])
            ).encode()
        )

    assert registry.refresh(ledger, date(2026, 11, 9), fail)["errors"] == 1
    assert registry.cached(ledger, CUIS[0]) == (
        old,
        "unavailable" if failure == "network" else "not_found" if failure == "absent" else "invalid_response",
    )
    assert registry.refresh(ledger, date(2026, 11, 10), fail)["attempted"] == 1


def test_invalid_foreign_and_private_clients_skipped(ledger):
    client(ledger, "12345678")  # invalid checksum
    client(ledger, CUIS[0], private=True)
    client(ledger, "DE" + CUIS[1])
    client(ledger, "00000000")
    client(ledger, "1990101000001")
    assert registry.refresh(ledger, TODAY, Anaf())["attempted"] == 0
    assert registry.eligible_cui("DE", CUIS[2]) == ""
    assert registry.eligible_cui("RO", " ro " + CUIS[2]) == CUIS[2]


def test_batches_and_deduplication_across_invoice_directions(ledger):
    for cui in CUIS:
        client(ledger, cui)
    _ef(ledger, "991", "NORD", "Firma Inventata SRL", CUIS[0], "X-1", TODAY, TODAY, "10.00")
    fetch = Anaf()
    assert registry.refresh(ledger, TODAY, fetch) == dict(attempted=101, errors=0)
    assert [len(c) for c in fetch.calls] == [100, 1]


def test_state_reads_cache_only_and_schema_matches(ledger, monkeypatch):
    from fiscalm.api import schema as api_schema
    from fiscalm.efactura import public as anaf_public

    client(ledger)

    def fail(*args):
        pytest.fail("state must never use network")

    monkeypatch.setattr(anaf_public, "fetch_json", fail)
    never = api.state(ledger, "NORD")["partners"][0]
    assert never["anaf_status"] is None and never["anaf_error"] is None and never["anaf_eligible"] is True
    registry.refresh(ledger, TODAY, Anaf())
    partner = api.state(ledger, "NORD")["partners"][0]
    assert partner["anaf_status"]["inactive"] and partner["anaf_error"] is None
    schema = api_schema.schemas()["State"]["properties"]["partners"]["items"]
    assert set(schema["required"]) == set(partner)


def test_configurable_interval_and_failure_before_first_check(ledger, monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"partner_anaf_refresh_days": 2}')
    ledger.set_meta(
        "partner_anaf_refresh_days", json.dumps(json.loads(cfg.read_text()).get("partner_anaf_refresh_days", 7))
    )
    client(ledger)

    def fail(*args):
        raise OSError

    registry.refresh(ledger, TODAY, fail)
    assert registry.cached(ledger, CUIS[0]) == (None, "unavailable")
    assert registry.refresh(ledger, date(2026, 11, 3), Anaf())["attempted"] == 1
    assert registry.refresh(ledger, date(2026, 11, 4), Anaf())["attempted"] == 0


def test_local_invoice_clients_without_saved_client_are_refreshed(ledger):
    data = dict(client=dict(name="Firma Inventata SRL", country="RO", cui=CUIS[0]))
    ledger.db.execute(
        "INSERT INTO invoices(id,company,series,number,status,data) VALUES(?,?,?,?,?,?)",
        ("local", "NORD", "X", 1, "issued", json.dumps(data)),
    )
    ledger.db.commit()
    assert registry.refresh(ledger, TODAY, Anaf())["attempted"] == 1


def test_claim_is_shared_between_connections(ledger):
    from fiscalm.ledger import Ledger

    client(ledger)
    other = Ledger()
    try:

        def fetch(url, body):
            assert registry.refresh(other, TODAY, Anaf())["attempted"] == 0
            return Anaf()(url, body)

        assert registry.refresh(ledger, TODAY, fetch)["attempted"] == 1
    finally:
        other.close()


@pytest.mark.parametrize("country,private", [("RO", True), ("DE", False)])
def test_state_never_attaches_romanian_cache_to_private_or_foreign_partner(ledger, country, private):
    client(ledger)
    registry.refresh(ledger, TODAY, Anaf())
    data = dict(name="Firma Inventata SRL", country=country, cui=CUIS[0], private=private)
    ledger.db.execute("UPDATE invoice_clients SET data=?", (json.dumps(data),))
    ledger.db.commit()
    p = api.state(ledger, "NORD")["partners"][0]
    assert p["anaf_status"] is None and p["anaf_error"] is None and p["anaf_eligible"] is False


@pytest.mark.parametrize("days", [-1, True, "7"])
def test_bad_refresh_config_refuses_network(ledger, monkeypatch, tmp_path, days):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(dict(partner_anaf_refresh_days=days)))
    ledger.set_meta(
        "partner_anaf_refresh_days", json.dumps(json.loads(cfg.read_text()).get("partner_anaf_refresh_days", 7))
    )
    client(ledger)
    fetch = Anaf()
    with pytest.raises(ValueError, match="positive integer"):
        registry.refresh(ledger, TODAY, fetch)
    assert fetch.calls == []


def test_explicit_foreign_tax_id_without_address_is_never_looked_up(ledger):
    _ef(ledger, "991", "NORD", "Firma Inventata SRL", CUIS[0], "X-1", TODAY, TODAY, "10.00")
    ledger.db.execute("UPDATE efactura SET supplier_tax_id=?,party_address='{}'", ("DE" + CUIS[0],))
    ledger.db.commit()
    assert registry.refresh(ledger, TODAY, Anaf())["attempted"] == 0


def test_interrupted_batch_is_marked_and_retried_without_claiming_later_batches(ledger):
    all_cuis = [str(n) for n in range(9000000, 9010000) if valid_cui(str(n))][:201]
    for cui in all_cuis:
        client(ledger, cui)
    fetch = Anaf()

    def interrupted(url, body):
        if len(fetch.calls) == 1:
            raise SystemExit
        return fetch(url, body)

    with pytest.raises(SystemExit):
        registry.refresh(ledger, TODAY, interrupted)
    assert ledger.db.execute("SELECT count(*) FROM partner_anaf").fetchone()[0] == 200
    assert registry.cached(ledger, all_cuis[100]) == (None, "unavailable")
    assert registry.cached(ledger, all_cuis[200]) == (None, None)
    assert registry.refresh(ledger, date(2026, 11, 3), Anaf()) == dict(attempted=101, errors=0)
    assert registry.cached(ledger, all_cuis[100])[0]["checked_at"] == "2026-11-03"


def test_interactive_lookup_and_tick_share_status_and_inactive_guard(ledger, monkeypatch):
    from test_invoice_lookups import response

    from fiscalm.efactura import public as anaf_public
    from fiscalm.invoicing import engine as invoicing

    cui = "99999984"
    client(ledger, cui)
    data = response()
    raw_row = data["found"][0]
    raw_row["date_generale"]["data"] = TODAY.isoformat()
    raw_row["stare_inactiv"]["statusInactivi"] = True
    anaf_public.lookup(cui, TODAY.isoformat(), lambda *_: json.dumps(data).encode(), L=ledger)
    assert api.state(ledger, "NORD")["partners"][0]["anaf_status"]["inactive"]
    assert registry.refresh(ledger, TODAY, Anaf())["attempted"] == 0
    with pytest.raises(invoicing.InvoiceError, match="inactiv"):
        invoicing._active_client(ledger, dict(cui=cui, inactive=False))

    def active(url, body):
        r = row(cui, "2026-11-09")
        r["stare_inactiv"]["statusInactivi"] = False
        return json.dumps(dict(cod=200, found=[r])).encode()

    registry.refresh(ledger, date(2026, 11, 9), active)
    invoicing._active_client(ledger, dict(cui=cui, inactive=False))
    assert ledger.get_meta("invoice_taxpayer:" + cui) is None


@pytest.mark.parametrize("days", [False, 0])
def test_refresh_can_be_disabled(ledger, tmp_path, monkeypatch, days):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(dict(partner_anaf_refresh_days=days)))
    ledger.set_meta(
        "partner_anaf_refresh_days", json.dumps(json.loads(cfg.read_text()).get("partner_anaf_refresh_days", 7))
    )
    client(ledger)
    assert registry.refresh(ledger, TODAY, lambda *_: pytest.fail("network"))["attempted"] == 0


def test_corrupt_party_json_does_not_stop_valid_partners(ledger):
    client(ledger)
    with ledger.tx() as db:
        db.execute("INSERT INTO invoice_clients VALUES(?,?,?,?)", ("broken", "NORD", "broken", "{"))
    assert registry.refresh(ledger, TODAY, Anaf())["attempted"] == 1


def test_state_reads_statuses_with_one_query(ledger):
    for cui in CUIS:
        client(ledger, cui)
    queries = []
    ledger.db.set_trace_callback(queries.append)
    try:
        api.state(ledger, "NORD")
    finally:
        ledger.db.set_trace_callback(None)
    assert sum("FROM partner_anaf" in q for q in queries) == 1


@pytest.mark.parametrize("entry", ["client", "company", "batch"])
def test_all_lookups_reject_the_same_malformed_registry_flags(ledger, monkeypatch, entry):
    from test_invoice_lookups import response

    from fiscalm.company_config import CompanyError
    from fiscalm.efactura import public as anaf_public

    data = response()
    data["found"][0]["inregistrare_RTVAI"]["statusTvaIncasare"] = "false"
    payload = json.dumps(data).encode()
    monkeypatch.setattr(anaf_public, "fetch_json", lambda *_: payload)
    if entry == "client":
        with pytest.raises(anaf_public.AnafLookupError):
            anaf_public.lookup("99999984", "2026-10-31", lambda *_: payload)
    elif entry == "company":
        with pytest.raises(CompanyError):
            anaf_public.lookup_company("99999984", "2026-10-31")
    else:
        client(ledger, "99999984")
        registry.refresh(ledger, date(2026, 10, 31), lambda *_: payload)
        assert registry.cached(ledger, "99999984") == (None, "invalid_response")


def test_company_lookup_needs_no_ledger(ledger, monkeypatch):
    from test_invoice_lookups import response

    from fiscalm.efactura import public as anaf_public

    data = response()
    data["found"][0]["date_generale"]["adresa"] = "Strada Inventata 1"
    monkeypatch.setattr(anaf_public, "fetch_json", lambda *_: json.dumps(data).encode())
    monkeypatch.setattr(anaf_public, "Ledger", lambda: pytest.fail("lookup must not open a ledger"))
    with ledger.tx():
        assert anaf_public.lookup_company("99999984", "2026-10-31")["vat_payer"]
        assert anaf_public.lookup("99999984", "2026-10-31", lambda *_: json.dumps(data).encode())["vat_payer"]
    assert registry.cached(ledger, "99999984") == (None, None)


def test_rate_limit_timestamp_is_shared_between_processes(home):
    import io
    import multiprocessing

    from fiscalm.efactura import public as anaf_public

    ctx = multiprocessing.get_context("fork")
    delays = ctx.Queue()

    def request():
        anaf_public.time.time = lambda: 1000.0
        anaf_public.time.sleep = delays.put
        anaf_public.urllib.request.urlopen = lambda *_a, **_kw: io.BytesIO(b"{}")
        anaf_public.fetch_json(anaf_public.URL, b"[]")

    for _ in range(2):
        process = ctx.Process(target=request)
        process.start()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("rate-limit process did not finish")
        assert process.exitcode == 0
    assert delays.get(timeout=5) == 1.0
    delays.close()


def test_corrupt_rate_limit_timestamp_is_rewritten(home, monkeypatch):
    import io

    from fiscalm.efactura import public as anaf_public

    gate = home / "state/anaf-public.lock"
    gate.parent.mkdir(exist_ok=True)
    gate.write_text("interrupted timestamp")
    monkeypatch.setattr(anaf_public.time, "time", lambda: 1000.0)
    monkeypatch.setattr(anaf_public.time, "sleep", lambda _: pytest.fail("no previous call"))
    monkeypatch.setattr(anaf_public.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"{}"))
    assert anaf_public.fetch_json(anaf_public.URL, b"[]") == b"{}"
    assert float(gate.read_text()) == 1000.0


def test_inactive_partner_tasks_are_cache_only_and_company_qualified(ledger, monkeypatch):
    from synthetic_world import _issued

    from fiscalm.api import schema as api_schema
    from fiscalm.efactura import public as anaf_public

    monkeypatch.setattr(anaf_public, "fetch_json", lambda *_: pytest.fail("state must not look up ANAF"))
    for co, mid in [("NORD", "991"), ("STUDIO9", "992")]:
        _ef(ledger, mid, co, "Firma Inventata SRL", CUIS[0], "X-1", TODAY, TODAY, "10.00")
        _issued(ledger, mid, co, "Y-1", "Firma Inventata SRL", CUIS[0], TODAY, TODAY, "20.00", currency="EUR")
    ledger.db.commit()
    assert api.state(ledger, "NORD")["partner_tasks"] == []  # never checked
    registry.refresh(ledger, TODAY, Anaf())
    state = api.state(ledger, "ALL")
    for part in state["parts"]:
        tasks = part["partner_tasks"]
        assert [t["kind"] for t in tasks] == ["inactive_supplier", "inactive_client"]
        assert all(t["id"] == f"{part['co']}.{CUIS[0]}" and t["company"] == part["co"] for t in tasks)
        assert set(tasks[0]) == set(api_schema.schemas()["State"]["properties"]["partner_tasks"]["items"]["required"])
        assert tasks[0]["checked_at"] == TODAY.isoformat()
        assert tasks[0]["label"] == "Furnizor inactiv la ANAF"
        assert tasks[0]["detail"] == "Există facturi de plătit. Verifică situația furnizorului cu contabilul."
        assert tasks[1]["label"] == "Client inactiv sau radiat la ANAF"
        assert tasks[1]["detail"] == "Există facturi de încasat. Verifică situația clientului."
    ledger.mark_efactura_paid("991", "statement", None)
    ledger.db.execute("UPDATE issued SET status='collected' WHERE company='NORD'")
    ledger.db.commit()
    assert api.state(ledger, "NORD")["partner_tasks"] == []
    assert len(api.state(ledger, "STUDIO9")["partner_tasks"]) == 2


@pytest.mark.parametrize(
    "status,expected",
    [
        ({"inactive": False, "registration_status": "INREGISTRAT"}, []),
        ({"inactive": False, "registration_status": "RADIAT"}, ["inactive_client"]),
        ({"inactive": False, "registration_status": "RADIERE din data 01.10.2026"}, ["inactive_client"]),
        ({"inactive": False, "registration_status": "INREGISTRAT din data 01.10.2020"}, []),
        ({"inactive": False, "registration_status": "RADIERE ANULATA"}, []),
        ({"inactive": False, "registration_status": "NERADIAT"}, []),
        ({"inactive": False, "inactive_register_removed_at": "2026-10-01"}, []),
        ({"inactive": True}, ["inactive_supplier", "inactive_client"]),
    ],
)
def test_partner_tasks_use_last_trusted_status(ledger, status, expected):
    from synthetic_world import _issued

    from fiscalm.efactura import public as anaf_public

    _ef(ledger, "991", "NORD", "Firma Inventata SRL", CUIS[0], "X", TODAY, TODAY, "10.00")
    _issued(ledger, "991", "NORD", "Y", "Firma Inventata SRL", CUIS[0], TODAY, TODAY, "20.00")
    anaf_public.store_status(ledger, CUIS[0], dict(checked_at=TODAY.isoformat(), **status))
    ledger.db.execute("UPDATE partner_anaf SET error='unavailable'")
    ledger.db.commit()
    assert [t["kind"] for t in api.state(ledger, "NORD")["partner_tasks"]] == expected


@pytest.mark.parametrize("country,private", [("RO", True), ("DE", False)])
def test_inactive_client_with_unpaid_invoice_excludes_private_and_foreign(ledger, country, private):
    from synthetic_world import _issued

    client(ledger)
    registry.refresh(ledger, TODAY, Anaf())
    data = dict(name="Firma Inventata SRL", country=country, cui=CUIS[0], private=private)
    ledger.db.execute("UPDATE invoice_clients SET data=?", (json.dumps(data),))
    _issued(ledger, "991", "NORD", "X", "Firma Inventata SRL", CUIS[0], TODAY, TODAY, "20.00")
    ledger.db.execute("UPDATE issued SET party_address=?", (json.dumps({"country": country}),))
    ledger.db.commit()
    assert api.state(ledger, "NORD")["partner_tasks"] == []


def test_inactive_supplier_autopay_refuses_from_cache(ledger, monkeypatch):
    from fiscalm.efactura import inbox as efactura, public as anaf_public

    _ef(ledger, "991", "NORD", "Firma Inventata SRL", CUIS[0], "X", TODAY, TODAY, "10.00")
    ledger.db.commit()
    efactura.payment_item(ledger, "991")  # never checked remains eligible
    registry.refresh(ledger, TODAY, Anaf())
    monkeypatch.setattr(anaf_public, "fetch_json", lambda *_: pytest.fail("no lookup"))
    monkeypatch.setattr(pay, "prepare_files", lambda *args, **kwargs: pytest.fail("no payment file"))
    efactura.set_autopay(ledger, "991")
    reason = "supplier inactive at ANAF; checked_at=2026-11-02"
    with pytest.raises(efactura.ApproveError, match="supplier inactive at ANAF"):
        efactura.payment_item(ledger, "991")
    assert efactura.autopay_new(ledger) == [{"message_id": "991", "status": "refused", "reason": reason}]
    from fiscalm.api import actions
    from fiscalm.ledger import finance_home

    (finance_home() / "state").mkdir(exist_ok=True)
    (finance_home() / "state" / "LIVE").touch()
    result = actions.act("payment_file_create", "NORD", {"name": "Inactive", "invoice_ids": ["991"]})
    assert result["ok"] is False
    assert result["message"] == (
        "Furnizorul figurează inactiv la ANAF (verificat pe 2026-11-02). "
        "Verifică situația cu contabilul înainte de plată."
    )
    state = api.state(ledger, "NORD")
    assert state["invoices"][0]["can_approve"] is False
    assert state["refused"] == [
        {
            "id": "991",
            "why": "supplier_inactive",
            "iban_last4": "0991",
            "supplier": "Firma Inventata SRL",
            "refused_checked_at": "2026-11-02",
            "still_inactive": True,
        }
    ]
    assert ledger.efactura("991")["status"] == "open"
    assert not ledger.db.execute("SELECT 1 FROM payments").fetchone()
    ledger.db.execute("UPDATE efactura SET supplier_tax_id=?,party_address=?", (CUIS[0], json.dumps({"country": "DE"})))
    ledger.db.commit()
    efactura.payment_item(ledger, "991")  # a Romanian cache hit cannot block a foreign supplier


@pytest.mark.parametrize("stage", ["in_file", "awaiting"])
def test_inactive_supplier_task_for_unconfirmed_payment(ledger, stage, home):
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    from fiscalm.efactura import inbox as efactura

    _ef(ledger, "991", "NORD", "Firma Inventata SRL", CUIS[0], "X", TODAY, TODAY, "10.00")
    ledger.db.commit()
    if stage == "in_file":
        efactura.approve(ledger, "991", origin="person")
        download_batch(ledger, [ledger.efactura("991")["item_id"]], today=TODAY)
        assert api.state(ledger, "NORD")["invoices"][0]["payment_state"] == "in_file"
    else:
        assert ledger.mark_efactura_paid("991", "user: already paid (page)")
        assert api.state(ledger, "NORD")["invoices"][0]["status"] == "marked"
    registry.refresh(ledger, TODAY, Anaf())
    assert [t["kind"] for t in api.state(ledger, "NORD")["partner_tasks"]] == (
        ["inactive_supplier"] if stage == "in_file" else []
    )


def test_fully_storno_issued_invoice_has_no_inactive_task(ledger):
    from synthetic_world import _issued

    _issued(ledger, "991", "NORD", "X", "Firma Inventata SRL", CUIS[0], TODAY, TODAY, "20.00")
    registry.refresh(ledger, TODAY, Anaf())
    assert len(api.state(ledger, "NORD")["partner_tasks"]) == 1
    _issued(
        ledger,
        "992",
        "NORD",
        "X-S",
        "Firma Inventata SRL",
        CUIS[0],
        TODAY,
        None,
        "20.00",
        doc_type="credit_note",
        ref="X",
    )
    assert api.state(ledger, "NORD")["partner_tasks"] == []


def test_inactive_refusal_keeps_its_date_after_supplier_turns_active(ledger, monkeypatch):
    """The refusal is shown with the check it was recorded with; a later active status flips still_inactive only."""
    from fiscalm.efactura import inbox as efactura

    _ef(ledger, "991", "NORD", "Firma Inventata SRL", CUIS[0], "X", TODAY, TODAY, "10.00")
    ledger.db.commit()
    registry.refresh(ledger, TODAY, Anaf())
    monkeypatch.setattr(pay, "prepare_files", lambda *args, **kwargs: pytest.fail("no payment file"))
    efactura.set_autopay(ledger, "991")
    assert efactura.autopay_new(ledger)[0]["status"] == "refused"
    assert json.loads(ledger.get_meta("efactura_autopay_refused:991")) == {
        "code": "supplier_inactive",
        "checked_at": "2026-11-02",
        "message": "supplier inactive at ANAF; checked_at=2026-11-02",
    }

    def active(url, body):
        r = row(CUIS[0], "2026-11-09")
        r["stare_inactiv"]["statusInactivi"] = False
        return json.dumps(dict(cod=200, found=[r], notFound=[])).encode()

    assert registry.refresh(ledger, date(2026, 11, 9), active) == dict(attempted=1, errors=0)
    state = api.state(ledger, "NORD")
    assert [p["anaf_status"]["checked_at"] for p in state["partners"] if p["anaf_status"]] == ["2026-11-09"]
    # Current eligibility wins: the supplier is active and its account confirmed, so the refusal no longer holds.
    assert [_anaf_fields(x) for x in state["refused"]] == [
        {"id": "991", "why": "ready", "refused_checked_at": "2026-11-02", "still_inactive": False}
    ]
    assert state["partner_tasks"] == []

    ledger.db.execute("DELETE FROM partner_anaf")
    ledger.db.commit()
    assert _anaf_fields(api.state(ledger, "NORD")["refused"][0]) == {
        "id": "991",
        "why": "ready",
        "refused_checked_at": "2026-11-02",
        "still_inactive": None,
    }


def _anaf_fields(x: dict) -> dict:
    return {k: x[k] for k in ("id", "why", "refused_checked_at", "still_inactive")}


def test_stored_refusal_never_outranks_current_eligibility(ledger):
    """The stored record says why autopay refused once; state shows fiscalm's reason now (efactura.current_refusal). The
    record only dates an inactive supplier; a date stored with any other code is ignored."""
    from fiscalm.efactura import inbox as efactura

    _ef(ledger, "991", "NORD", "Firma Inventata SRL", CUIS[0], "X", TODAY, TODAY, "10.00")
    key = "efactura_autopay_refused:991"
    ledger.set_meta(key, json.dumps(dict(code="iban_changed", checked_at="2026-10-20", message="x")))
    assert _anaf_fields(api.state(ledger, "NORD")["refused"][0]) == {
        "id": "991",
        "why": "ready",
        "refused_checked_at": None,
        "still_inactive": None,
    }
    ledger.set_meta(
        key, efactura.refusal_record(efactura.ApproveError("supplier_inactive", "x", checked_at="2026-10-20"))
    )
    assert _anaf_fields(api.state(ledger, "NORD")["refused"][0]) == {  # never checked since: status unknown
        "id": "991",
        "why": "ready",
        "refused_checked_at": "2026-10-20",
        "still_inactive": None,
    }
    registry.refresh(ledger, TODAY, Anaf())  # inactive now
    assert _anaf_fields(api.state(ledger, "NORD")["refused"][0]) == {
        "id": "991",
        "why": "supplier_inactive",
        "refused_checked_at": "2026-10-20",
        "still_inactive": True,
    }
    ledger.set_meta(key, json.dumps(dict(code="iban_changed", checked_at="2026-10-20", message="x")))
    assert _anaf_fields(api.state(ledger, "NORD")["refused"][0]) == {  # only today's check finds it inactive
        "id": "991",
        "why": "supplier_inactive",
        "refused_checked_at": "2026-11-02",
        "still_inactive": True,
    }
    for stale in ("not_ron", "invoice_issues"):
        ledger.set_meta(key, efactura.refusal_record(efactura.ApproveError(stale, "x")))
        assert api.state(ledger, "NORD")["refused"][0]["why"] == "supplier_inactive"
    ledger.db.execute("UPDATE efactura SET currency='EUR'")
    ledger.db.commit()
    assert _anaf_fields(
        api.state(ledger, "NORD")["refused"][0]
    ) == {  # not_ron is checked first, and named only as "unclear"
        "id": "991",
        "why": "unclear",
        "refused_checked_at": None,
        "still_inactive": None,
    }


@pytest.mark.parametrize(
    "stored,expected",
    [
        # Exactly fiscalm's own texts, from before refusals carried a code.
        ("supplier inactive at ANAF; checked_at=2026-11-02", ("supplier_inactive", "2026-11-02")),
        ("supplier IBAN differs from its earlier invoices; verify by phone first", ("unclear", None)),
        # Anything else is unclear, with no date, whatever it contains.
        ("invoice has issues: bad due date 'supplier inactive at ANAF; checked_at=2040-01-01'", ("unclear", None)),
        ("supplier inactive at ANAF; checked_at=2040-01-01; and more", ("unclear", None)),
        ("supplier inactive at ANAF; checked_at=2026-13-45", ("unclear", None)),
        ("supplier IBAN differs: checked_at=2026-11-02", ("unclear", None)),
        (
            "invoice has issues: bad due date 'supplier IBAN differs from its earlier invoices; verify by phone first'",
            ("unclear", None),
        ),
        ('{"code": "pay_now", "checked_at": "2040-01-01"}', ("unclear", None)),
        ('{"code": "iban_changed", "checked_at": "2040-01-01"}', ("unclear", None)),
        ('{"code": "supplier_inactive", "checked_at": "soon"}', ("supplier_inactive", None)),
        ('["supplier_inactive"]', ("unclear", None)),
        ("", ("unclear", None)),
        (None, ("unclear", None)),
    ],
)
def test_a_stored_refusal_is_read_by_its_code_or_fiscalms_exact_old_text(stored, expected):
    from fiscalm.efactura import inbox as efactura

    assert efactura.stored_refusal(stored) == expected


def test_inactive_supplier_reads_as_inactive_in_the_rule(ledger, monkeypatch):
    """The rules list takes the reason from fiscalm's code: an inactive supplier is never "date neclare" or
    a plain refused invoice, and it doesn't block resuming the supplier (each invoice is still checked)."""
    from fiscalm import rules
    from fiscalm.efactura import inbox as efactura

    _ef(ledger, "991", "NORD", "Firma Inventata SRL", CUIS[0], "X", TODAY, TODAY, "10.00")
    ledger.db.commit()
    registry.refresh(ledger, TODAY, Anaf())
    monkeypatch.setattr(pay, "prepare_files", lambda *args, **kwargs: pytest.fail("no payment file"))
    efactura.set_autopay(ledger, "991")
    assert efactura.autopay_new(ledger)[0]["status"] == "refused"
    ident = f"NORD:supplier_autopay:{CUIS[0]}"
    assert next(r for r in rules.list_rules(ledger, "NORD") if r["id"] == ident)["reason"] == "supplier_inactive"
    rules.set_enabled(ledger, "NORD", ident, False)
    rules.set_enabled(ledger, "NORD", ident, True)

    def active(url, body):
        r = row(CUIS[0], "2026-11-09")
        r["stare_inactiv"]["statusInactivi"] = False
        return json.dumps(dict(cod=200, found=[r], notFound=[])).encode()

    registry.refresh(ledger, date(2026, 11, 9), active)  # the refusal no longer holds
    assert next(r for r in rules.list_rules(ledger, "NORD") if r["id"] == ident)["reason"] is None
