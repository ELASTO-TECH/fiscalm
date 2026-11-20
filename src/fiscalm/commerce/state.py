"""Order sheet data and sales relations. Shop text never chooses a link or action."""

from __future__ import annotations

from decimal import Decimal

from fiscalm import companies, integrations
from fiscalm.commerce import orders as commerce
from fiscalm.ledger import Ledger


def sales_fields(order: dict | None) -> dict:
    return dict(
        order_id=order["id"] if order else None,
        order_sheet_id=f"{order['company']}:{order['id']}" if order else None,
        sales_origin=order["channel"] if order else "manual",
        order_number=order["external_id"] if order else None,
        awb=order["awb"] if order else None,
        cod_at_courier=order.get("cod_at_courier", False) if order else False,
    )


def draft_fields(L: Ledger, co: str, invoice_id: str) -> dict:
    row = L.db.execute("SELECT id FROM commerce_orders WHERE company=? AND draft_id=?", (co, invoice_id)).fetchone()
    if not row:
        row = L.db.execute(
            "SELECT o.id FROM commerce_orders o JOIN invoice_refund_drafts r ON r.original_id=o.draft_id AND r.company=o.company WHERE o.company=? AND r.draft_id=?",
            (co, invoice_id),
        ).fetchone()
    return sales_fields(commerce.get(L, co, row[0]) if row else None)


def project(L: Ledger, co: str, issued: list[dict]) -> dict:
    snapshot = getattr(L, "_commerce_snapshot", None)
    if snapshot is None:
        snapshot = (
            list(L.db.execute("SELECT id,company FROM commerce_orders ORDER BY id")),
            integrations.listing(L, "shop"),
        )
        # Only a single API read owns this wrapper; never cache on a writable Ledger.
        if hasattr(L, "_ledger"):
            L._commerce_snapshot = snapshot
    order_ids, shop_rows = snapshot
    shops = {r["id"]: r for r in shop_rows if r["config"]["company"] == co}
    orders = []
    by_invoice = {}
    for row in order_ids:
        if row["company"] != co:
            continue
        order = commerce.get(L, co, row[0])
        shop = shops.get(order["channel"])
        order["sheet_id"] = f"{co}:{order['id']}"
        order["channel_name"] = shop["name"] if shop else None
        order["company_name"] = companies.all(L)[co].name
        order["delivery"] = dict(picked_up_at=None, returned_at=None)
        delivery = L.db.execute(
            "SELECT picked_up_at,returned_at FROM commerce_delivery WHERE order_id=?", (order["id"],)
        ).fetchone()
        if delivery:
            order["delivery"] = dict(delivery)
        order["cod_at_courier"] = False
        order["can_discard"] = order["status"] == "refused"
        order["can_cancel"] = order["status"] == "draft"
        orders.append(order)
        if order["invoice_id"]:
            by_invoice[order["invoice_id"]] = order
    totals: dict[str, Decimal] = {}
    count = 0
    for row in issued:
        order = by_invoice.get(row.get("local_id"))
        if by_invoice and not order and row.get("local_id"):
            original = L.db.execute(
                "SELECT original_id FROM invoice_reversals WHERE storno_id=? LIMIT 1", (row["local_id"],)
            ).fetchone()
            order = by_invoice.get(original[0]) if original else None
        original_row = order and row.get("local_id") == order["invoice_id"]
        if order and original_row:
            body = order["body"]
            delivery = order["delivery"]
            eligible = (
                order["status"] == "issued"
                and body["payment"]["method"] == "cod"
                and bool(order["awb"])
                and bool(delivery["picked_up_at"])
                and not delivery["returned_at"]
                and Decimal(row["owed"]) > 0
                and not row.get("rejected")
            )
            order["cod_at_courier"] = eligible
            if eligible:
                totals[row["currency"]] = totals.get(row["currency"], Decimal(0)) + Decimal(row["owed"])
                count += 1
        row.update(sales_fields(order))
        if not original_row:
            row["cod_at_courier"] = False
    tasks = [
        dict(
            kind="order_refused",
            id=o["sheet_id"],
            company=co,
            order_id=o["id"],
            sheet="ord",
            reason=o["reason"],
            label="Comandă refuzată",
            action="order-discard",
        )
        for o in orders
        if o["status"] == "refused"
    ]
    origins = [dict(id="manual", company=None, label="Manual")]
    origins += [
        dict(id=iid, company=co, label=f"{shop['name']} ({companies.all(L)[co].name})") for iid, shop in shops.items()
    ]
    return dict(
        orders=orders,
        order_tasks=tasks,
        sales_origins=origins,
        cod_receivable=dict(
            total=format(totals.pop("RON", Decimal(0)), ".2f"),
            other=[dict(currency=k, total=format(v, ".2f")) for k, v in sorted(totals.items())],
            count=count,
        ),
    )
