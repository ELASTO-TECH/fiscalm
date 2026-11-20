from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_commerce import (
    setup_invoice as setup_invoice,  # noqa: F401
    shop as shop,
)

from fiscalm.api.v1 import Error
from fiscalm.commerce import orders as c
from fiscalm.invoicing import engine as i
from fiscalm.ledger import Ledger


def test_awb_cancel_and_stale_draft(shop):
    L, body = shop
    order = c.intake(L, "NORD", body, "2026-10-31")
    c.command(L, "NORD", order["id"], "orders_awb", dict(courier="Synthetic courier", number="AWB-1"), "2026-10-31")
    assert c.get(L, "NORD", order["id"])["awb"]["number"] == "AWB-1"
    with pytest.raises(Error):
        c.command(L, "STUDIO9", order["id"], "orders_cancel", {}, "2026-10-31")
    assert c.command(L, "NORD", order["id"], "orders_cancel", {}, "2026-10-31")["status"] == "cancelled"
    assert c.command(L, "NORD", order["id"], "orders_cancel", {}, "2026-10-31")["status"] == "cancelled"
    with pytest.raises(Error):
        c.command(L, "NORD", order["id"], "orders_awb", dict(courier="X", number="Y"), "2026-10-31")


def issued(shop):
    L, body = shop
    order = c.intake(L, "NORD", body | dict(lines=body["lines"] * 2), "2026-10-31")
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    return L, order


def test_refund_is_unnumbered_storno_draft_then_issues_same_id(shop):
    L, order = issued(shop)
    with pytest.raises(Error):
        c.command(L, "NORD", order["id"], "orders_cancel", {}, "2026-10-31")
    result = c.command(L, "NORD", order["id"], "orders_refund", {"lines": [1]}, "2026-10-31")
    refund = i.invoice(L, result["draft_id"], company="NORD")
    assert refund["status"] == "draft" and refund["number"] is None
    assert refund["lines"][0]["quantity"] == "-2"
    assert refund["data"]["totals"]["total"] == "-242.00"
    assert refund["data"]["billing_ref"] == "TEST-42"
    assert L.db.execute("SELECT count(*) FROM invoice_reversals").fetchone()[0] == 0
    assert c.command(L, "NORD", order["id"], "orders_refund", {"lines": [1]}, "2026-10-31")["draft_id"] == refund["id"]
    with pytest.raises(i.InvoiceError):
        i.issue(L, refund["id"], "2026-10-31", company="NORD", version=2)
    done = i.issue(L, refund["id"], "2026-10-31", company="NORD", version=1)
    assert done["id"] == refund["id"] and done["number"] == 43 and done["status"] == "issued"
    assert L.db.execute("SELECT count(*) FROM invoice_reversals").fetchone()[0] == 1
    with pytest.raises(i.InvoiceError):
        c.command(L, "NORD", order["id"], "orders_refund", {"lines": [1]}, "2026-10-31")


def test_refund_delete_releases_pending_positions_and_edit_refuses(shop):
    L, order = issued(shop)
    result = c.command(L, "NORD", order["id"], "orders_refund", {}, "2026-10-31")
    refund = i.invoice(L, result["draft_id"], company="NORD")
    with pytest.raises(i.InvoiceError):
        i.save_draft(
            L, "NORD", "TEST", L.get_meta("draft_client:" + refund["id"]), dict(lines=[]), refund["id"], version=1
        )
    with pytest.raises(i.InvoiceError):
        c.command(L, "NORD", order["id"], "orders_refund", {"lines": [1]}, "2026-10-31")
    i.delete_draft(L, refund["id"], company="NORD", version=1)
    assert c.command(L, "NORD", order["id"], "orders_refund", {}, "2026-10-31")["draft_id"] != refund["id"]


@pytest.mark.parametrize("lines", [[], [0], [3], [1, 1], [True], "1"])
def test_refund_invalid_positions_refuse(shop, lines):
    L, order = issued(shop)
    with pytest.raises((Error, i.InvoiceError)):
        c.command(L, "NORD", order["id"], "orders_refund", {"lines": lines}, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoice_refund_drafts").fetchone()[0] == 0


def test_racing_refunds_share_one_draft(shop):
    L, order = issued(shop)
    barrier = Barrier(2)

    def submit(_):
        other = Ledger()
        try:
            barrier.wait()
            return c.command(other, "NORD", order["id"], "orders_refund", {}, "2026-10-31")
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        a, b = list(pool.map(submit, range(2)))
    assert a == b
    assert L.db.execute("SELECT count(*) FROM invoice_refund_drafts").fetchone()[0] == 1


def test_discount_line_and_gross_refund_preserve_paid_amount(shop):
    from decimal import Decimal

    L, body = shop
    body = body | dict(
        lines=[
            body["lines"][0] | dict(quantity="1", unit_price="33.33"),
            body["lines"][0] | dict(description="Reducere", quantity="1", unit_price="-1.99"),
        ]
    )
    order = c.intake(L, "NORD", body, "2026-10-31")
    invoice = i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    assert invoice["data"]["totals"]["total"] == "31.34"
    assert invoice["lines"][1]["quantity"] == "-1" and Decimal(invoice["lines"][1]["unit_price"]) > 0
    refund = c.command(L, "NORD", order["id"], "orders_refund", {}, "2026-10-31")
    done = i.issue(L, refund["draft_id"], "2026-10-31", company="NORD", version=1)
    assert done["data"]["totals"]["total"] == "-31.34"


def test_excess_discount_refuses(shop):
    L, body = shop
    with pytest.raises(i.InvoiceError):
        c.intake(L, "NORD", body | dict(lines=[body["lines"][0] | dict(unit_price="-121")]), "2026-10-31")


@pytest.mark.parametrize("selected", [[1], [2]])
def test_discount_refund_requires_balanced_selection(shop, selected):
    L, body = shop
    order = c.intake(
        L,
        "NORD",
        body
        | dict(
            lines=[
                body["lines"][0] | dict(quantity="1", unit_price="100"),
                body["lines"][0] | dict(quantity="1", unit_price="-90"),
            ]
        ),
        "2026-10-31",
    )
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    with pytest.raises(i.InvoiceError):
        c.command(L, "NORD", order["id"], "orders_refund", {"lines": selected}, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoice_refund_drafts").fetchone()[0] == 0


def test_partial_gross_refunds_each_match_paid_line(shop):
    L, body = shop
    order = c.intake(
        L, "NORD", body | dict(lines=[body["lines"][0] | dict(quantity="1", unit_price="1.99")] * 2), "2026-10-31"
    )
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    for position in (1, 2):
        refund = c.command(L, "NORD", order["id"], "orders_refund", {"lines": [position]}, "2026-10-31")
        done = i.issue(L, refund["draft_id"], "2026-10-31", company="NORD", version=1)
        assert done["data"]["totals"]["total"] == "-1.99"


def test_manual_storno_cannot_take_pending_refund_lines(shop):
    L, order = issued(shop)
    c.command(L, "NORD", order["id"], "orders_refund", {"lines": [1]}, "2026-10-31")
    with pytest.raises(i.InvoiceError):
        i.storno(L, order["invoice_id"] or order["draft_id"], "2026-10-31", "Retur", [1], company="NORD")


def test_order_wide_discount_cannot_be_reserved_by_separate_refunds(shop):
    L, body = shop
    order = c.intake(
        L,
        "NORD",
        body | dict(lines=[body["lines"][0] | dict(quantity="1", unit_price=price) for price in ("100", "100", "-50")]),
        "2026-10-31",
    )
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    for positions in ([1], [2], [1, 3]):
        with pytest.raises(i.InvoiceError):
            c.command(L, "NORD", order["id"], "orders_refund", {"lines": positions}, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoice_refund_drafts").fetchone()[0] == 0
    refund = c.command(L, "NORD", order["id"], "orders_refund", {}, "2026-10-31")
    assert (
        i.issue(L, refund["draft_id"], "2026-10-31", company="NORD", version=1)["data"]["totals"]["total"] == "-150.00"
    )


def test_many_gross_rounding_differences_refuse_before_draft(shop):
    L, body = shop
    with pytest.raises(i.InvoiceError):
        c.intake(
            L,
            "NORD",
            body | dict(lines=[body["lines"][0] | dict(quantity="1", unit_price="0.01")] * 1000),
            "2026-10-31",
        )
    assert not L.db.execute("SELECT 1 FROM invoices").fetchone()
