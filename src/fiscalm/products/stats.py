"""Batched issued sales grouped by immutable invoice-line product links."""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext

from fiscalm import invoice_lines
from fiscalm.efactura import inbox
from fiscalm.invoicing.labels import UNITS
from fiscalm.ledger import Ledger
from fiscalm.products.engine import ProductError


def periods(month: str, count: int) -> list[str]:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) or type(count) is not int or count not in (1, 12):
        raise ProductError("Verifică luna și perioada.")
    year, number = map(int, month.split("-"))
    end = year * 12 + number - 1
    if not 1 <= year <= 9999 or end - count + 1 < 12:
        raise ProductError("Verifică luna.")
    return [f"{n // 12:04d}-{n % 12 + 1:02d}" for n in range(end - count + 1, end + 1)]


def _add(groups: dict, row: dict) -> None:
    key = (row["product_id"], row["currency"], row["unit"])
    group = groups.setdefault(
        key,
        dict(
            product_id=key[0],
            name=row["name"],
            currency=key[1],
            unit=key[2],
            revenue=Decimal(0),
            quantity=Decimal(0),
            invoices=set(),
        ),
    )
    with localcontext() as context:
        context.prec = 60
        group["revenue"] += Decimal(row["net"])
        group["quantity"] += Decimal(row["quantity"])
    group["invoices"].add(row["invoice_id"])


def _rows(groups: dict) -> list[dict]:
    return [
        {k: v for k, v in row.items() if k not in ("invoices", "revenue", "quantity")}
        | dict(
            revenue=format(row["revenue"].quantize(Decimal(".01"), rounding=ROUND_HALF_UP), "f"),
            quantity=format(row["quantity"], "f"),
            invoice_count=row["invoice_count"] if "invoice_count" in row else len(row["invoices"]),
        )
        for row in sorted(
            groups.values(), key=lambda r: (r["revenue"].copy_negate(), r["name"], r["currency"], r["unit"])
        )
    ]


def _sources(L: Ledger, co: str, start: str, end: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    with L.tx(immediate=False):
        authored = L.db.execute(
            """SELECT i.id, i.series, i.number, i.data AS invoice_data, l.data AS line_data, p.name
            FROM invoices i JOIN invoice_lines l ON l.invoice_id=i.id
            LEFT JOIN products p ON p.company=i.company AND p.id=json_extract(l.data,'$.product_id')
            WHERE i.company=? AND i.status!='draft'
            AND NOT EXISTS (SELECT 1 FROM invoice_submissions s WHERE s.invoice_id=i.id
                AND s.environment='prod' AND s.status='rejected')
            AND json_extract(i.data,'$.issue_date') BETWEEN ? AND ?""",
            (co, start, end),
        ).fetchall()
        external = L.db.execute(
            """SELECT s.* FROM issued s WHERE s.company=? AND s.status!='duplicate'
            AND (s.issue_date BETWEEN ? AND ? OR (s.parse_error IS NOT NULL
            AND substr(s.listed_at,1,8) BETWEEN replace(?,'-','') AND replace(?,'-','')))
            AND NOT EXISTS (
            SELECT 1 FROM invoice_sent_links l JOIN invoices i ON i.id=l.invoice_id
            WHERE l.message_id=s.message_id AND i.company=s.company)""",
            (co, start, end, start, end),
        ).fetchall()
    return authored, external


def _signed(value: str, direction: int) -> str:
    amount = Decimal(value)
    return format(amount.copy_negate() if direction < 0 else amount, "f")


def _document(raw: sqlite3.Row, co: str) -> tuple[dict | None, int]:
    tree = inbox.stored_tree(co, raw["raw_path"])
    document = invoice_lines.from_xml(tree) if tree is not None else None
    if not document or not document["lines"] or document["truncated"] or document["currency"] != raw["currency"]:
        return None, 1
    if any(
        n.get("currencyID", raw["currency"]) != raw["currency"]
        for n in tree.findall(".//cbc:LineExtensionAmount", inbox.NS)
    ):
        raise ValueError("line currency differs")
    credit = tree.tag.endswith("}CreditNote") or tree.findtext("cbc:InvoiceTypeCode", namespaces=inbox.NS) == "381"
    with localcontext() as context:
        context.prec = 60
        if any(Decimal(line["net"]) != Decimal(line["net"]).quantize(Decimal(".01")) for line in document["lines"]):
            return None, 1
        net = sum((Decimal(line["net"]) for line in document["lines"]), Decimal(0))
    return document, -1 if credit and net >= 0 else 1


def _external(
    rows: list[sqlite3.Row], co: str, authored: set[tuple[str, str, str]]
) -> Iterator[tuple[str, dict | None]]:
    codes = {u["label"]: u["code"] for u in UNITS}
    for raw in rows:
        if (raw["number"], raw["issue_date"], raw["doc_type"]) in authored:
            continue
        month = raw["issue_date"][:7] if raw["issue_date"] else raw["listed_at"][:4] + "-" + raw["listed_at"][4:6]
        if raw["parse_error"]:
            yield month, None
            continue
        try:
            document, direction = _document(raw, co)
            if document is None:
                yield month, None
                continue
            for line in document["lines"]:
                yield (
                    month,
                    line
                    | dict(
                        product_id=None,
                        name="Fără produs",
                        currency=raw["currency"],
                        invoice_id="spv:" + raw["message_id"],
                        unit=codes.get(line["unit"], line["unit"]),
                        net=_signed(line["net"], direction),
                        quantity=_signed(line["quantity"], direction),
                    ),
                )
        except (inbox.UblError, DecimalException, TypeError, ValueError):
            yield month, None


def sales(L: Ledger, co: str, month: str, count: int, *, today: date | None = None) -> dict:
    months = periods(month, count)
    year, number = map(int, month.split("-"))
    start, end = months[0] + "-01", date(year, number, calendar.monthrange(year, number)[1]).isoformat()
    end = min(end, today.isoformat()) if today else end
    monthly, totals = {m: {} for m in months}, {}
    unavailable = {m: 0 for m in months}
    rows, external = _sources(L, co, start, end)
    authored = set()
    for raw in rows:
        invoice, line = json.loads(raw["invoice_data"]), json.loads(raw["line_data"])
        kind = "credit_note" if Decimal(invoice["totals"]["total"]) < 0 else "invoice"
        authored.add((f"{raw['series']}-{raw['number']}", invoice["issue_date"], kind))
        row = line | dict(
            invoice_id="local:" + raw["id"],
            product_id=line.get("product_id") or None,
            name=raw["name"] or ("Fără produs" if not line.get("product_id") else line["description"]),
            currency=invoice["currency"],
        )
        _add(monthly[invoice["issue_date"][:7]], row)
        _add(totals, row)
    for period, line in _external(external, co, authored):
        if line is None:
            unavailable[period] += 1
        else:
            _add(monthly[period], line)
            _add(totals, line)
    return dict(
        company=co,
        complete=not any(unavailable.values()),
        unavailable_count=sum(unavailable.values()),
        **{"from": start, "to": end},
        months=[
            dict(month=m, products=_rows(monthly[m]), complete=not unavailable[m], unavailable_count=unavailable[m])
            for m in months
        ],
        totals=_rows(totals),
    )


def period(sales: dict, start: str, end: str) -> dict:
    """Dashboard ranges start on month boundaries within these twelve months.

    Disjoint issue months make document counts additive within each product/currency/unit.
    """
    months = [m for m in sales["months"] if start[:7] <= m["month"] <= end[:7]]
    groups = {}
    for month in months:
        for row in month["products"]:
            key = row["product_id"], row["currency"], row["unit"]
            group = groups.setdefault(key, row | dict(revenue=Decimal(0), quantity=Decimal(0), invoice_count=0))
            with localcontext() as context:
                context.prec = 60
                group["revenue"] += Decimal(row["revenue"])
                group["quantity"] += Decimal(row["quantity"])
            group["invoice_count"] += row["invoice_count"]
    return dict(
        company=sales["company"],
        complete=all(m["complete"] for m in months),
        unavailable_count=sum(m["unavailable_count"] for m in months),
        totals=_rows(groups),
    )
