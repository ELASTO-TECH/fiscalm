"""Shop orders in settlements: order and AWB identifiers, courier lifecycle, COD exposure, money held by partners."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from test_commerce import (
    setup_invoice as setup_invoice,  # noqa: F401
    shop as shop,
)
from test_links import _bill
from test_settlement_import import UPLOAD, workbook
from test_settlements import collect, statement, store_bank

from fiscalm import companies, links
from fiscalm.api import state as api
from fiscalm.api.v1 import Error
from fiscalm.bank import statements
from fiscalm.commerce import orders as commerce
from fiscalm.efactura import client as anaf
from fiscalm.invoicing import engine as invoicing
from fiscalm.settlements import engine as settlements, upload
from fiscalm.settlements.engine import tasks

AWB_REPORT = b"ID,Gross,Fee\nAWB-1,242.00,2.00\n"


def order(shop, *, mid="spv-order", **awb):
    """An order invoiced in fiscalm and never read back from SPV: its own `issued` row, under a readable id."""
    L, body = shop
    o = commerce.intake(L, "NORD", body | {"awb": {"courier": "fan", "number": "AWB-1"} | awb}, "2026-10-31")
    invoicing.issue(L, o["draft_id"], "2026-10-31", company="NORD", version=1)
    with L.tx() as db:
        for table in ("issued", "invoice_sent_links"):
            db.execute(
                f"UPDATE {table} SET message_id=? WHERE message_id=?",
                (mid, invoicing.import_id("local", o["draft_id"])),
            )
    return L, o


def report(L, data=AWB_REPORT, mapping=None, **spec):
    return settlements.import_report(
        L,
        "NORD",
        data,
        dict(
            source="fan",
            report_key="batch1",
            date="2026-11-01",
            currency="RON",
            payer_name="Synthetic Courier SRL",
            mapping=mapping or {"awb": "ID", "gross": "Gross", "fee": "Fee"},
        )
        | spec,
    )


def payout(**kw):
    return statement(**dict(date=date(2026, 11, 2), amount=Decimal("240.00")) | kw)


def state_on(L, day=11):
    return api.state(L, "NORD", datetime(2026, 11, day, tzinfo=UTC))


@pytest.mark.parametrize(
    "columns,spec",
    [
        (("awb",), {}),
        (("order",), {}),
        (("order", "reference"), {}),
        (("awb",), {"source": "courier", "courier": "fan"}),
    ],
    ids=["awb", "order", "order+invoice", "generic-courier"],
)
def test_order_and_awb_collect_with_journal_and_tx_link(shop, columns, spec):
    L, o = order(shop, picked_up_at="2026-10-31")
    values = {"awb": "AWB-1", "order": commerce.get(L, "NORD", o["id"])["external_id"], "reference": "TEST-42"}
    data = f"{','.join(columns)},Gross,Fee\n{','.join(values[c] for c in columns)},242.00,2.00\n".encode()
    rid = report(L, data, {c: c for c in columns} | {"gross": "Gross", "fee": "Fee"}, **spec)
    pending = settlements.state(L, "NORD")[0]
    assert pending["lines"][0]["invoice"] == "spv-order"
    assert tasks([pending])[0]["sheet"] == {"co": "NORD", "set": rid}
    assert collect(L, rid, payout()) == ["spv-order"]
    s = state_on(L)
    assert s["cod"]["count"] == 0
    assert s["settlement_tasks"] == []
    assert s["settlement_transactions"][0]["links"] == {"settlement": rid, "issued": ["spv-order"]}
    assert s["settlements"][0]["lines"][0]["invoice"] == "spv-order"
    assert s["settlements"][0]["payer"]["id"]
    assert s["settlements"][0]["net"] == "240.00"
    assert not api.state(L, "STUDIO9")["settlements"]


@pytest.mark.parametrize(
    "failure,change,reason",
    [
        ("company", ("NORD", "STUDIO9"), "invoice_missing_or_ambiguous"),
        ("disagree", ("TEST-42", "UNKNOWN-1"), "invoice_missing_or_ambiguous"),
        ("ambiguous", ("", ""), "invoice_missing_or_ambiguous"),
        ("courier", (",fan,", ",other,"), "awb_unknown"),
        ("awb", ("AWB-1,", "AWB-9,"), "awb_unknown"),
        ("order", ("AWB-1,,", ",ORDER-9,"), "order_unknown"),
    ],
)
def test_identifier_conflicts_settle_nothing(shop, failure, change, reason):
    L, o = order(shop)
    if failure == "ambiguous":
        with L.tx() as db:
            row = list(db.execute("SELECT * FROM commerce_orders").fetchone())
            row[0] = "another-order"
            row[3] = "another-external-id"
            db.execute("INSERT INTO commerce_orders VALUES(" + ",".join("?" * len(row)) + ")", row)
    data = "AWB,Order,Invoice,Company,Courier,Gross,Fee\nAWB-1,,TEST-42,NORD,fan,242.00,2.00\n".replace(*change)
    columns = {"awb": "AWB", "order": "Order", "reference": "Invoice", "company": "Company", "courier": "Courier"}
    report(L, data.encode(), columns | {"gross": "Gross", "fee": "Fee"})
    st, _ = store_bank(L, payout())
    assert settlements.match_statement(L, st) == []
    s = settlements.state(L, "NORD")[0]
    assert s["error"] == f"line_1_{reason}"
    assert s["failed_line"] == 1 and tasks([s])[0]["line"] == 1
    assert not L.used_txs() and L.issued("spv-order")["status"] == "open"
    assert not L.db.execute("SELECT * FROM settlement_lines").fetchall()


@pytest.mark.parametrize(
    "awb,count,kind",
    [
        ({}, 0, None),
        ({"picked_up_at": "2026-10-31"}, 1, None),
        ({"picked_up_at": "2026-10-31", "delivered_at": "2026-10-31"}, 1, "cod_overdue"),
        ({"picked_up_at": "2026-10-31", "returned_at": "2026-10-31"}, 0, "cod_returned"),
    ],
)
def test_cod_requires_actual_pickup_and_tasks_open_invoice(shop, awb, count, kind):
    L, o = order(shop, **awb)
    before = L.db.execute("SELECT count(*) FROM invoices").fetchone()[0]
    s = state_on(L)["cod"]
    assert s["count"] == count
    assert [t["kind"] for t in s["tasks"]] == ([kind] if kind else [])
    if kind:
        assert s["tasks"][0]["sheet"] == {"co": "NORD", "iss": "spv-order"}
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == before


def test_draft_pickup_is_not_counted(shop):
    L, body = shop
    commerce.intake(
        L, "NORD", body | {"awb": {"courier": "fan", "number": "AWB-1", "picked_up_at": "2026-10-31"}}, "2026-10-31"
    )
    assert state_on(L)["cod"]["count"] == 0


@pytest.mark.parametrize(
    "dates",
    [
        {"delivered_at": "2026-10-31"},
        {"picked_up_at": "not-a-day"},
        {"picked_up_at": "2026-10-31", "delivered_at": "2026-10-30"},
        {"picked_up_at": "2099-01-01"},
    ],
)
def test_bad_lifecycle_refuses(shop, dates):
    L, body = shop
    with pytest.raises(Error):
        commerce.intake(L, "NORD", body | {"awb": {"courier": "fan", "number": "AWB-1"} | dates}, "2026-10-31")


def test_user_mark_never_removes_cod_exposure(shop):
    L, _ = order(shop, picked_up_at="2026-10-31")
    L.mark_issued_manual("spv-order", "manual mark", "other")
    s = state_on(L)["cod"]
    assert s["count"] == 1 and s["totals"] == [{"currency": "RON", "total": "242.00"}]


def test_cod_overdue_window_boundary(shop):
    L, _ = order(shop, picked_up_at="2026-10-31", delivered_at="2026-10-31")
    assert state_on(L, 7)["cod"]["tasks"] == []
    assert state_on(L, 8)["cod"]["tasks"][0]["kind"] == "cod_overdue"


def test_pending_awb_query_count_does_not_grow_per_line(shop):
    L, _ = order(shop)
    counts = []
    for size in (1, 1000):
        rid = report(L, b"ID,Gross,Fee\n" + b"AWB-1,242.00,2.00\n" * size)
        queries = []
        L.db.set_trace_callback(queries.append)
        state_on(L)
        L.db.set_trace_callback(None)
        counts.append(len(queries))
        L.withdraw_report(rid, "NORD")
    assert counts[0] == counts[1]


def test_return_task_closes_after_full_storno(shop):
    L, o = order(shop, picked_up_at="2026-10-31", returned_at="2026-10-31")
    invoicing.storno(L, o["draft_id"], "2026-11-01", "Synthetic return", company="NORD")
    assert state_on(L)["cod"]["tasks"] == []


def test_collected_return_still_needs_refund_decision(shop):
    L, _ = order(shop, picked_up_at="2026-10-31", returned_at="2026-10-31")
    L.mark_issued_collected("spv-order", "statement proof", "synthetic-credit")
    s = state_on(L)["cod"]
    assert s["count"] == 0 and s["tasks"][0]["kind"] == "cod_returned"


def test_local_storno_refuses_collection_before_spv_sync(shop):
    L, o = order(shop)
    rid = report(L)
    invoicing.storno(L, o["draft_id"], "2026-11-01", "Synthetic return", company="NORD")
    with pytest.raises(settlements.SettlementError, match="^line_1_invoice_not_open_or_amount_differs$"):
        collect(L, rid, payout())
    assert not L.used_txs()


def test_lifecycle_replacement_command_refuses_future(shop):
    L, o = order(shop)
    with pytest.raises(Error):
        commerce.command(
            L,
            "NORD",
            o["id"],
            "orders_awb",
            {"courier": "fan", "number": "AWB-1", "picked_up_at": "2099-01-01"},
            "2026-11-01",
        )
    commerce.command(
        L,
        "NORD",
        o["id"],
        "orders_awb",
        {"courier": "fan", "number": "AWB-1", "picked_up_at": "2026-10-31"},
        "2026-11-01",
    )
    assert state_on(L)["cod"]["count"] == 1


@pytest.mark.parametrize("already_collected", [False, True])
def test_unidentified_private_credit_note_keeps_return_task_even_after_collection(shop, already_collected):
    L, _ = order(shop, picked_up_at="2026-10-31", returned_at="2026-10-31")
    if already_collected:
        L.mark_issued_collected("spv-order", "statement proof", "synthetic-credit")
    L.add_issued(
        dict(
            message_id="spv-credit",
            company="NORD",
            doc_type="credit_note",
            number="CREDIT-1",
            billing_ref="TEST-42",
            issue_date="2026-11-01",
            currency="RON",
            total="242.00",
            client_name=L.issued("spv-order")["client_name"],
            status="credit",
            raw_path="",
        )
    )
    s = state_on(L)
    assert next(i for i in s["issued"] if i["id"] == "spv-order")["credited"] == "0.00"
    assert s["cod"]["count"] == 0 and s["cod"]["tasks"][0]["kind"] == "cod_returned"


def test_rejected_invoice_has_no_cod_exposure(shop):
    L, o = order(shop, picked_up_at="2026-10-31", returned_at="2026-10-31")
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,errors,xml_sha) VALUES(?,?,?,?,?)",
            (o["draft_id"], anaf.inbox_env(), "rejected", "[]", "synthetic"),
        )
    s = state_on(L)["cod"]
    assert s["count"] == 0 and s["tasks"] == []


@pytest.mark.parametrize(
    "payment,awb,expected",
    [
        ({"method": "card", "paid": True, "processor": "Synthetic Processor SRL"}, {}, "processor"),
        ({"method": "card", "paid": False, "processor": "Synthetic Processor SRL"}, {}, None),
        ({"method": "cod", "paid": False}, {"picked_up_at": "2026-10-31"}, None),
        ({"method": "cod", "paid": False}, {"picked_up_at": "2026-10-31", "delivered_at": "2026-10-31"}, "courier"),
        (
            {"method": "cod", "paid": False},
            {"picked_up_at": "2026-10-31", "delivered_at": "2026-10-31", "returned_at": "2026-10-31"},
            None,
        ),
    ],
)
def test_partner_holds_money_only_after_customer_paid(shop, payment, awb, expected):
    L, body = shop
    body["payment"] = payment
    order(shop, **awb)
    s = state_on(L, 3)
    holders = [p for p in s["partners"] if p["held"]]
    assert len(holders) == bool(expected)
    if expected:
        assert expected in holders[0]["roles"]
        assert holders[0]["held"] == [{"invoice": "spv-order", "amount": "242.00", "currency": "RON"}]
        L.db.execute("UPDATE issued SET status='collected' WHERE message_id='spv-order'")
        assert not any(p["held"] for p in api.state(L, "NORD")["partners"])


@pytest.mark.parametrize(
    "payment,awb,holder",
    [
        ({"method": "card", "paid": True, "processor": "Synthetic Processor SRL"}, {}, "NORD.99999911"),
        ({"method": "cod", "paid": False}, {"picked_up_at": "2026-10-31", "delivered_at": "2026-10-31"}, "NORD.n-fan"),
    ],
)
def test_payout_without_report_uses_same_guarded_links(shop, payment, awb, holder):
    L, body = shop
    body["payment"] = payment
    if payment["method"] == "card":
        _bill(L, "9001", supplier="Synthetic Processor SRL", cui="99999911")
    order(shop, mid="7001", **awb)
    body["external_id"] = "order-2"
    body["buyer"]["name"] = "Synthetic Second Buyer SRL"
    order(shop, mid="7002", **awb)
    st = payout(counterparty="Synthetic Payout SRL", amount=Decimal("480.00"))
    L.record_statement(st)
    key = statements.tx_key(st.txs[0])
    args = dict(invoices=["7001", "7002"], partners=api.item_partners(L, "NORD"), reason="courier_fee", by="admin")
    with pytest.raises(links.LinkRefused):
        links.link(L, "NORD", key, **args, holder="NORD.n-other-processor")
    result = links.link(L, "NORD", key, **args, holder=holder)
    assert result["partner"] == holder and result["difference"] == "-4.00"
    assert all(L.issued(mid)["status"] == "collected" for mid in ("7001", "7002"))
    assert links.explanations(L, "NORD")[key]["parts"] == [
        {"id": mid, "amount": "242.00", "closed": True} for mid in ("7001", "7002")
    ]


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_mixed_company_upload_with_shared_order_and_invoice_refuses(shop, kind):
    L, o = order(shop)
    L.db.execute(
        "INSERT INTO commerce_orders SELECT 'foreign-order','STUDIO9',channel,external_id,placed_at,body,"
        "draft_id,client_id,draft_version,status,awb,reason FROM commerce_orders WHERE id=?",
        (o["id"],),
    )
    original = L.db.execute("SELECT * FROM issued WHERE message_id='spv-order'").fetchone()
    L.add_issued(dict(original) | {"message_id": "foreign-invoice", "company": "STUDIO9"})
    rows = [
        ["Ref", "Order", "Gross", "Fee", "CUI"],
        [
            original["number"],
            commerce.get(L, "NORD", o["id"])["external_id"],
            "242.00",
            "2.00",
            companies.get("NORD", L=L).cui,
        ],
        [
            original["number"],
            commerce.get(L, "NORD", o["id"])["external_id"],
            "242.00",
            "2.00",
            companies.get("STUDIO9", L=L).cui,
        ],
    ]
    data = ("\n".join(",".join(row) for row in rows) + "\n").encode()
    if kind == "xlsx":
        data = workbook(
            "".join(
                "<row>"
                + "".join(f'<c r="{chr(65 + i)}{n}" t="inlineStr"><is><t>{v}</t></is></c>' for i, v in enumerate(row))
                + "</row>"
                for n, row in enumerate(rows, 1)
            )
        )
    store_bank(L, payout(amount=Decimal("480.00")))
    with pytest.raises(upload.Refused):
        upload.accept_report(
            L,
            "NORD",
            data,
            upload.XLSX if kind == "xlsx" else "text/csv",
            description=dict(
                source="fan",
                name="Synthetic Courier SRL",
                mapping=dict(reference="Ref", order="Order", gross="Gross", fee="Fee", company_cui="CUI"),
            ),
            **UPLOAD,
        )
    assert not L.used_txs()
    assert L.db.execute("SELECT count(*) FROM settlement_reports").fetchone()[0] == 0
    assert all(r[0] == "open" for r in L.db.execute("SELECT status FROM issued"))
