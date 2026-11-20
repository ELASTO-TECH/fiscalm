import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from test_api import server as server  # noqa: F401
from test_invoicing import setup_invoice as setup_invoice  # noqa: F401

from fiscalm import integrations
from fiscalm.commerce import emag, emag_sync as sync, orders as commerce
from fiscalm.invoicing import engine as inv
from fiscalm.ledger import Ledger

TODAY = "2026-10-31"


@pytest.fixture
def channel(setup_invoice):
    L, _ = setup_invoice
    saved = integrations.add(
        L,
        "shop",
        "eMAG test",
        dict(
            platform="emag",
            company="NORD",
            series="TEST",
            emag_since="2026-10-01 00:00:00",
            emag_username="synthetic-user",
            emag_password="synthetic-password",
            emag_pdf_base="https://invoices.example.invalid",
        ),
    )
    shop = next(s for s in integrations.enabled(L, "shop") if s.id == saved["id"])
    row = dict(
        id=123,
        type=3,
        status=1,
        is_complete=1,
        date=TODAY + " 10:00:00",
        payment_mode_id=1,
        payment_status=0,
        shipping_tax="0",
        customer=dict(
            legal_entity=0,
            billing_name="Client fictiv",
            billing_country="RO",
            billing_suburb="RO-BV",
            billing_city="Brașov",
            billing_street="Strada Exemplu 1",
        ),
        products=[
            dict(
                id=10,
                product_id=20,
                status=1,
                quantity=2,
                sale_price="100",
                vat="0.21",
                name="Produs fictiv",
                currency="RON",
            )
        ],
    )
    return L, shop, row


class Fake:
    def __init__(self, rows=(), rmas=()):
        self.rows, self.rmas = list(rows), list(rmas)
        self.calls = []
        self.fail = False

    def __call__(self, endpoint, body):
        self.calls.append((endpoint, body))
        if endpoint == "order/attachments/save":
            assert set(body) == {"data"}
            assert body["data"][0]["order_type"] == 3
            if self.fail:
                raise emag.Refused("api_unavailable")
            return []
        if endpoint == "awb/read":
            return [
                dict(order_id=123, awb=[dict(awb_number="SYNTHETIC-AWB")], courier=[dict(courier_name="Curier fictiv")])
            ]
        assert "data" not in body and body["type"] == 3
        source = self.rows if endpoint == "order/read" else self.rmas
        start = (body["currentPage"] - 1) * body["itemsPerPage"]
        return deepcopy(source[start : start + body["itemsPerPage"]])


def run(L, api, stamp=100):
    return sync.tick(L, TODAY, stamp, lambda _: api)


def test_pagination_duplicates_changed_and_refused_after_issue(channel):
    L, shop, row = channel
    api = Fake([row] * 101)
    assert run(L, api)["orders"] == 101
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1
    assert [b["currentPage"] for e, b in api.calls if e == "order/read"] == [1, 2]
    row["products"][0]["quantity"] = 3
    run(L, Fake([row]))
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    assert inv.invoice(L, order["draft_id"], company="NORD")["data"]["totals"]["total"] == "363.00"
    inv.issue(L, order["draft_id"], TODAY, company="NORD", version=2)
    row["products"][0]["quantity"] = 4
    assert run(L, Fake([row]))["failed"] == 1
    assert sync.tasks(L, "NORD")[0]["reason"] == "order_review"
    assert inv.invoice(L, order["draft_id"], company="NORD")["data"]["totals"]["total"] == "363.00"


@pytest.mark.parametrize("status", range(6))
def test_every_order_status(channel, status):
    L, shop, row = channel
    run(L, Fake([row]))
    row["status"] = status
    run(L, Fake([row]))
    order = L.db.execute("SELECT id FROM commerce_orders").fetchone()
    assert commerce.get(L, "NORD", order[0])["status"] == ("cancelled" if status == 0 else "draft")
    assert bool(sync.tasks(L, "NORD")) == (status == 5)


@pytest.mark.parametrize("mode,method", [(1, "cod"), (2, "transfer"), (3, "card")])
def test_payment_metadata_never_collects(channel, mode, method):
    L, shop, row = channel
    row.update(payment_mode_id=mode, payment_status=1)
    run(L, Fake([row]))
    body = json.loads(L.db.execute("SELECT body FROM commerce_orders").fetchone()[0])
    assert body["payment"] == dict(method=method, paid=False, processor="emag")
    assert L.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 0


def test_awb_and_rma_suggest_only(channel):
    L, shop, row = channel
    row["awbs"] = [dict(emag_id=42)]
    run(L, Fake([row], [dict(emag_id=7, order_id=123, request_status=6)]))
    order = commerce.get(L, "NORD", L.db.execute("SELECT id FROM commerce_orders").fetchone()[0])
    assert order["awb"]["number"] == "SYNTHETIC-AWB"
    assert sync.tasks(L, "NORD")[0]["reason"] == "return_review"
    assert L.db.execute("SELECT count(*) FROM invoice_refund_drafts").fetchone()[0] == 0


def test_live_attachment_failure_retry_and_pdf_guard(channel, home, monkeypatch):
    L, shop, row = channel
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    run(L, Fake([row]))
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    inv.issue(L, order["draft_id"], TODAY, company="NORD", version=1)
    api = Fake([row])
    api.fail = True
    run(L, api)
    assert sync.tasks(L, "NORD")[0]["reason"] == "attachment_retry"
    run(L, api, 101)
    assert len([e for e, _ in api.calls if e == "order/attachments/save"]) == 1
    api.fail = False
    run(L, api, 3700)
    uploads = [b for e, b in api.calls if e == "order/attachments/save"]
    assert len(uploads) == 2 and uploads[0] == uploads[1]
    assert sync.tasks(L, "NORD") == []
    token = L.db.execute("SELECT token FROM emag_attachments").fetchone()[0]
    monkeypatch.setattr("fiscalm.commerce.emag_sync.time.time", lambda: 3700)
    monkeypatch.setattr(
        "fiscalm.invoicing.pdf.render",
        lambda ledger, iid, company: b"pdf" if company == "NORD" and iid == order["draft_id"] else None,
    )
    assert sync.pdf(L, token) == b"pdf"
    assert sync.pdf(L, "../secret") is None
    integrations.update(L, shop.id, dict(company="STUDIO9"))
    assert sync.pdf(L, token) is None


def test_shadow_never_attaches_and_secrets_write_only(channel):
    L, shop, row = channel
    integrations.update(L, shop.id, dict(auto_issue="true"))
    api = Fake([row])
    run(L, api)
    assert all(e != "order/attachments/save" for e, _ in api.calls)
    assert L.db.execute("SELECT status FROM invoices").fetchone()[0] == "draft"
    public = str(integrations.listing(L))
    assert "synthetic-password" not in public and "synthetic-user" not in public
    assert sync.tasks(L, "STUDIO9") == []


def test_two_ticks_racing(channel):
    L, shop, row = channel
    barrier = Barrier(2)

    def work(_):
        other = Ledger()
        try:
            barrier.wait()
            return run(other, Fake([row]))
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(work, range(2)))
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["shipping", "vat", "voucher", "storno", "unknown", "fbe"])
def test_ambiguous_tax_and_status_refuse(channel, change):
    L, shop, row = channel
    if change == "shipping":
        row["shipping_tax"] = "10"
    if change == "vat":
        row["products"][0].pop("vat")
    if change == "voucher":
        row["vouchers"] = [dict(voucher_id=1)]
    if change == "storno":
        row["is_storno"] = True
    if change == "unknown":
        row["status"] = 42
    if change == "fbe":
        row["type"] = 2
    run(L, Fake([row]))
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0
    assert sync.tasks(L, "NORD")


def test_redirect_and_error_body_not_exposed(channel, monkeypatch):
    L, shop, row = channel

    def redirect(req, **kwargs):
        from fiscalm.commerce.webhooks import NoRedirect

        NoRedirect().redirect_request(req, None, 302, "secret", {}, "https://attacker.invalid")

    monkeypatch.setattr(emag._OPENER, "open", redirect)
    with pytest.raises(emag.Refused, match="api_unavailable"):
        emag.Client(shop)("order/read", {})


def test_cutoff_bad_rows_county_and_payment_changes(channel):
    L, shop, row = channel
    old = deepcopy(row)
    old.update(id=99, date="2026-09-01 00:00:00")
    row["customer"]["billing_suburb"] = "Brașov"
    api = Fake([old, {"id": "bad"}, None, row])
    run(L, api)
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1
    assert L.db.execute("SELECT count(*) FROM commerce_orders WHERE external_id='99'").fetchone()[0] == 0
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    inv.issue(L, order["draft_id"], TODAY, company="NORD", version=1)
    row["payment_status"] = 1
    run(L, Fake([row]))
    assert not any(t["external_id"] == "123" for t in sync.tasks(L, "NORD"))
    assert any(e == "rma/read" for e, _ in api.calls)
    request = next(b for e, b in api.calls if e == "order/read")
    assert request["createdAfter"] == shop.config["emag_since"]
    assert "modifiedAfter" in request


def test_resolve_company_shadow_and_rma_finalized(channel, home):
    L, shop, row = channel
    run(L, Fake([row], [dict(emag_id=7, order_id=123, request_status=6)]))
    task = sync.tasks(L, "NORD")[0]
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    with pytest.raises(ValueError):
        sync.resolve(L, "STUDIO9", task["id"])
    sync.resolve(L, "NORD", task["id"])
    run(L, Fake([row], [dict(emag_id=7, order_id=123, request_status=6)]))
    assert sync.tasks(L, "NORD") == []
    # Finalization in eMAG is not proof that a credit note exists.
    L.db.execute("DELETE FROM emag_decisions")
    L.db.commit()
    run(L, Fake([row], [dict(emag_id=7, order_id=123, request_status=7)]))
    assert sync.tasks(L, "NORD")[0]["reason"] == "return_review"


def test_attachments_run_even_if_order_read_fails(channel, home):
    L, shop, row = channel
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    run(L, Fake([row]))
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    inv.issue(L, order["draft_id"], TODAY, company="NORD", version=1)
    api = Fake()

    def broken(endpoint, body):
        if endpoint == "order/read":
            raise emag.Refused("api_unavailable")
        return api(endpoint, body)

    run(L, broken)
    assert any(e == "order/attachments/save" for e, _ in api.calls)
    assert sync.tasks(L, "NORD") == []
    assert integrations.listing(L)[0]["sync_error"]


def test_county_sector_exact_and_voucher_reconciliation(channel):
    L, shop, row = channel
    row["customer"].update(billing_suburb="București", billing_city="Sector 3")
    assert emag.translate(shop, row)["buyer"]["address"]["city"] == "SECTOR3"
    row["customer"]["billing_city"] = "Bucuresti"
    with pytest.raises(emag.Refused, match="sector"):
        emag.translate(shop, row)
    row["customer"].update(billing_suburb="Brașov", billing_city="Brașov")
    split = dict(voucher_id=1, value="-10", vat_value="-2.10", vat="0.21", offered_by="seller", voucher_name="Reducere")
    row["products"][0]["product_voucher_split"] = [split]
    row["vouchers"] = [dict(voucher_id=1, status=1, sale_price="-10", sale_price_vat="-2.10")]
    assert len(emag.translate(shop, row)["lines"]) == 2
    row["vouchers"].append(dict(voucher_id=2, status=1, sale_price="-10", sale_price_vat="-2.10"))
    with pytest.raises(emag.Refused, match="voucher_allocation"):
        emag.translate(shop, row)


def test_refund_action_prepares_one_draft_and_clears_task(channel, home):
    L, shop, row = channel
    run(L, Fake([row]))
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    inv.issue(L, order["draft_id"], TODAY, company="NORD", version=1)
    row["status"] = 5
    run(L, Fake([row]))
    task = sync.tasks(L, "NORD")[0]
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    with pytest.raises(ValueError):
        sync.refund(L, "STUDIO9", task["id"], TODAY)
    from fiscalm.efactura import client as anaf

    L.db.execute(
        "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,'accepted','synthetic')",
        (order["draft_id"], anaf.env()),
    )
    L.db.commit()
    draft = sync.refund(L, "NORD", task["id"], TODAY)
    assert inv.invoice(L, draft["draft_id"], company="NORD")["status"] == "draft"
    assert sync.tasks(L, "NORD") == []
    run(L, Fake([row]))
    assert sync.tasks(L, "NORD") == []


def test_crashed_attachment_claim_replays_stable_url(channel, home):
    L, shop, row = channel
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    run(L, Fake([row]))
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    inv.issue(L, order["draft_id"], TODAY, company="NORD", version=1)
    calls = []

    def crash(endpoint, body):
        if endpoint == "order/attachments/save":
            calls.append(body)
            raise KeyboardInterrupt()
        return Fake([row])(endpoint, body)

    with pytest.raises(KeyboardInterrupt):
        run(L, crash)
    api = Fake([row])
    run(L, api, 101)
    assert not any(e == "order/attachments/save" for e, _ in api.calls)
    run(L, api, 3700)
    assert [b for e, b in api.calls if e == "order/attachments/save"] == calls


def test_default_cutoff_is_setup_time(setup_invoice):
    L, _ = setup_invoice
    saved = integrations.add(
        L,
        "shop",
        "New eMAG",
        dict(
            platform="emag",
            company="NORD",
            series="TEST",
            emag_username="synthetic-user",
            emag_password="synthetic-password",
            emag_pdf_base="https://invoices.example.invalid",
        ),
    )
    from datetime import date

    assert saved["config"]["emag_since"][:10] == date.today().isoformat()


def test_incremental_overlap_and_four_decimal_prices(channel):
    L, shop, row = channel
    row["products"][0].update(sale_price="12.1234", quantity=2)
    api = Fake([row])
    run(L, api, 1793443200)  # 2026-10-31 10:40 UTC
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    assert inv.invoice(L, order["draft_id"], company="NORD")["data"]["totals"]["total"] == "29.34"
    api = Fake([row])
    run(L, api, 1793443500)
    request = next(b for e, b in api.calls if e == "order/read")
    assert request["modifiedAfter"] > shop.config["emag_since"]


def test_pdf_http_capability_expiry_disable_and_shadow(channel, home, server, monkeypatch):
    from test_api import _req

    L, shop, row = channel
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    run(L, Fake([row]))
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    inv.issue(L, order["draft_id"], TODAY, company="NORD", version=1)
    run(L, Fake([row]), 100)
    token = L.db.execute("SELECT token FROM emag_attachments").fetchone()[0]
    monkeypatch.setattr("fiscalm.invoicing.pdf.render", lambda *a, **k: b"%PDF-synthetic")
    monkeypatch.setattr("fiscalm.commerce.emag_sync.time.time", lambda: 100)
    code, headers, body = _req(server, "GET", "/emag-pdf/" + token + ".pdf")
    assert code == 200 and headers["content-type"] == "application/pdf" and body == b"%PDF-synthetic"
    assert headers["cache-control"] == "no-store"
    monkeypatch.setattr("fiscalm.commerce.emag_sync.time.time", lambda: 100 + 86401)
    assert _req(server, "GET", "/emag-pdf/" + token + ".pdf")[0] == 404
    monkeypatch.setattr("fiscalm.commerce.emag_sync.time.time", lambda: 100)
    (home / "state" / "LIVE").unlink()
    assert _req(server, "GET", "/emag-pdf/" + token + ".pdf")[0] == 404


@pytest.mark.parametrize("status", [1, 2, 3, 4, 5, 6, 7])
def test_rma_statuses(channel, status):
    L, shop, row = channel
    run(L, Fake([row], [dict(emag_id=7, order_id=123, request_status=status)]))
    assert bool(sync.tasks(L, "NORD")) == (status in (2, 3, 6, 7))


def test_new_rma_after_acknowledgement_reopens_task(channel, home):
    L, shop, row = channel
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    run(L, Fake([row], [dict(emag_id=7, order_id=123, request_status=6)]))
    task = sync.tasks(L, "NORD")[0]
    sync.resolve(L, "NORD", task["id"])
    run(L, Fake([row], [dict(emag_id=8, order_id=123, request_status=6)]))
    assert sync.tasks(L, "NORD")[0]["id"] == task["id"]


def test_two_live_ticks_attach_once(channel, home):
    L, shop, row = channel
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    run(L, Fake([row]))
    order = L.db.execute("SELECT * FROM commerce_orders").fetchone()
    inv.issue(L, order["draft_id"], TODAY, company="NORD", version=1)
    barrier = Barrier(2)
    api = Fake([row])

    def work(_):
        other = Ledger()
        try:
            barrier.wait()
            run(other, api)
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(work, range(2)))
    assert len([e for e, _ in api.calls if e == "order/attachments/save"]) == 1


@pytest.mark.parametrize("change", [{"status": 5}, {"status": 4, "is_storno": True}])
def test_finalized_rma_does_not_hide_order_return(channel, change):
    L, shop, row = channel
    run(L, Fake([row]))
    row.update(change)
    run(L, Fake([row], [dict(emag_id=7, order_id=123, request_status=7)]))
    assert sync.tasks(L, "NORD")[0]["reason"] == "return_review"


def test_recorded_tax_refusal_advances_cursor_but_unknown_error_does_not(channel, monkeypatch):
    L, shop, row = channel
    run(L, Fake([row, dict(row, id=124, shipping_tax="15")]), 1793443200)
    cursor = L.db.execute("SELECT key,value FROM meta WHERE key LIKE 'emag-cursor:%'").fetchone()
    assert cursor and cursor["value"] > shop.config["emag_since"]

    def unknown(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(sync, "ingest", unknown)
    run(L, Fake([row]), 1793443800)
    assert L.get_meta(cursor["key"]) == cursor["value"]


def test_cancelled_unimported_order_never_prompts_invoicing(channel):
    L, shop, row = channel
    run(L, Fake([dict(row, shipping_tax="15")]))
    assert sync.tasks(L, "NORD")
    run(L, Fake([dict(row, status=0)]))
    assert sync.tasks(L, "NORD") == []
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0


def test_awb_failure_preserves_issued_invoice_and_has_own_task(channel, home):
    L, shop, row = channel
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    integrations.update(L, shop.id, dict(auto_issue="true"))
    row["awbs"] = [dict(emag_id=42)]
    api = Fake([row])

    def broken_awb(endpoint, body):
        if endpoint == "awb/read":
            raise emag.Refused("api_unavailable")
        return api(endpoint, body)

    run(L, broken_awb)
    assert L.db.execute("SELECT status FROM invoices").fetchone()[0] == "issued"
    assert sync.tasks(L, "NORD")[0]["reason"] == "awb_retry"
    run(L, Fake([row]), 3700)
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1
    assert sync.tasks(L, "NORD") == []


def test_return_task_projection_has_bounded_reads(channel):
    L, shop, row = channel
    for external in range(123, 143):
        current = dict(row, id=external)
        sync.ingest(L, shop, current, TODAY)
        sync.note(L, shop, str(external), "return_review")
    queries = []
    L.db.set_trace_callback(lambda sql: queries.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
    try:
        tasks = sync.tasks(L, "NORD")
    finally:
        L.db.set_trace_callback(None)
    assert len(tasks) == 20 and len(queries) <= 2
