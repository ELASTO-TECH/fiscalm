"""The app's list filters and page selection over complete company entities."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

from fiscalm.api import search, state as api, transaction_rows
from fiscalm.api.responses import bounded, fail, page

REPORT_SCREENS = (
    "report_received",
    "report_issued",
    "report_tasks",
    "report_bank",
    "report_advances",
    "report_docs",
    "report_not_mailed",
)
SCREENS = ("vanzari", "cheltuieli", "bonuri", "parteneri", "tranzactii", *REPORT_SCREENS)


def _purchases(part: dict, receipts: bool) -> Iterator[tuple]:
    invoices = {r["id"]: r for r in part["invoices"]}
    documents = {r["id"]: r for r in part["documents"]}
    for expense in part["expenses"]:
        invoice, doc = invoices.get(expense["invoice"]), documents.get(expense["document"])
        data = expense | {"invoice": invoice, "document": doc}
        if invoice and (doc or not receipts):
            yield (
                invoice["issue_date"] or (doc or {}).get("date") or "",
                f"{invoice['supplier']}{' ' + doc['vendor'] if doc else ''} {invoice['number']}",
                not receipts and api.received_owed(invoice["status"], invoice["payment_state"]),
                not receipts and invoice["overdue"],
                data,
            )
        elif doc and (doc["document_type"] == "invoice") != receipts:
            yield (
                doc["date"] or doc["month"],
                f"{doc['vendor']} {doc['number'] or ''}",
                receipts,
                False,
                data,
            )


def _history(part: dict, direction: str) -> Iterator[tuple]:
    """Invoices from before fiscalm (state._history), beside the tracked ones: never open, never late."""
    for r in part["history"]:
        if r["direction"] == direction:
            yield r["issue_date"], f"{r['party_name']} {r['number']} {r['party_cui'] or ''}", False, False, r


def _report_purchases(part: dict, report: dict) -> Iterator[tuple[str, dict]]:
    """Full states already contain complete, conflict-checked expense references; scope each side independently."""
    invoices = {r["id"]: r for r in part["invoices"]}
    documents = {r["id"]: r for r in part["documents"]}
    received, docs = set(report["received"]), set(report["documents"])
    for expense in part["expenses"]:
        invoice = invoices.get(expense["invoice"]) if expense["invoice"] in received else None
        document = documents.get(expense["document"]) if expense["document"] in docs else None
        if invoice or document:
            yield "purchase", expense | {"invoice": invoice, "document": document}


def report_tasks(part: dict, report: dict) -> Iterator[tuple[str, dict]]:
    """What is still open in a month: debits without a document, a statement that does not add up, mails to check and
    debits that fit several invoices. Their number is the month's "de rezolvat"."""
    bank = report["bank"]
    if bank:
        yield from (("card", r) for r in bank["card"] if not r["doc"])
        if bank["balanced"] is False or bank["error"]:
            yield "bank", bank | {"card": [], "advances": []}
    keys = set(report["to_check"])
    yield from (("mail", r) for r in part["to_check"] if r["key"] in keys)
    yield from (
        ("ambiguous", r | {"candidate_count": len(r["candidates"])})
        for r in part["ambiguous"]
        if r["month"] == report["month"]
    )


def _report_items(part: dict, screen: str, month: str) -> Iterator[tuple]:
    report = next((r for r in part["reports"] if r["month"] == month), None)
    if report is None:
        fail(404, "report_not_found", "Choose a month from this company's reports.")
    bank, issued = report["bank"], set(report["issued"])
    groups = {
        "report_received": lambda: _report_purchases(part, report),
        "report_issued": lambda: (("issued", r) for r in part["issued"] if r["id"] in issued),
        "report_tasks": lambda: report_tasks(part, report),
        "report_bank": lambda: (("card", r) for r in (bank["card"] if bank else []) if r["doc"]),  # open ones: tasks
        "report_advances": lambda: (("advance", r) for r in (bank["advances"] if bank else [])),
        "report_docs": lambda: (("document", r) for r in report["docs"]),
        "report_not_mailed": lambda: (("not_mailed", r) for r in report["not_mailed"]),
    }
    for i, (kind, data) in enumerate(groups[screen]()):
        ident = (
            data["id"]
            if kind in ("purchase", "issued")
            else data["key"]
            if kind == "mail"
            else data["tx"]
            if kind == "ambiguous"
            else str(i)
        )
        yield (
            "",
            "",
            False,
            False,
            {"id": f"{part['co']}:{screen}:{month}:{kind}:{ident}", "company": part["co"], "kind": kind, "data": data},
        )


def _items(part: dict, screen: str, month: str = "") -> Iterator[tuple]:
    if screen == "tranzactii":
        yield from transaction_rows.items(part)
    elif screen in ("cheltuieli", "bonuri"):
        yield from _purchases(part, screen == "bonuri")
        if screen == "cheltuieli":
            yield from _history(part, "received")
    elif screen in REPORT_SCREENS:
        yield from _report_items(part, screen, month)
    elif screen == "vanzari":
        for r in part["issued"]:
            yield r["issue_date"] or "", f"{r['client']} {r['number']}", api.issued_owed(r), r["days_overdue"] > 0, r
        yield from _history(part, "issued")
    else:
        for r in part["partners"]:
            owes = any(Decimal(r[k]["total"]) > 0 or r[k]["other"] for k in ("payable", "receivable"))
            yield (
                r["last"],
                f"{r['name'] if r['name'] is not None else 'null'} {r['cui'] or ''}",
                owes,
                r["late"] > 0,
                r,
            )


def _cursor(value: str) -> tuple[str, int] | None:
    if not value:
        return None
    try:
        day, ordinal = value.rsplit(":", 1)
        if (
            not re.fullmatch(r"(?:[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?)?", day)
            or not ordinal.isascii()
            or not ordinal.isdigit()
        ):
            raise ValueError
        position = int(ordinal)
        if position > 1_000_000_000:
            raise ValueError
        return day, -position
    except ValueError:
        fail(400, "invalid_cursor", "Use the cursor from the same list and filters.")


def _transaction_range(screen: str, query: dict) -> tuple[str, str]:
    start, end = query.get("from", ""), query.get("to", "")
    if (start or end) and screen != "tranzactii":
        fail(400, "invalid_filter", "Only transactions accept a date range.")
    for value in (start, end):
        if value:
            try:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                    raise ValueError
                date.fromisoformat(value)
            except ValueError:
                fail(400, "invalid_date", "Use dates in YYYY-MM-DD format.")
    return start, end


def _choice_selected(row: dict, selection: str) -> bool:
    """Use the existing Nou/Contabil eligibility before counting or paging choices."""
    if selection == "storno":
        return row.get("can_storno", False)  # an invoice from history is never reversed in fiscalm
    if selection == "accountant":
        return bool(re.fullmatch(r"[0-9]{2,10}", row["cui"] or ""))
    return True


class Collector:
    """Complete match/month counts, one page; core lists are newest first, payments keep section order."""

    def __init__(self, query: dict) -> None:
        page([], "id", query)  # Validate the limit before projecting any entities.
        self.screen = query.get("screen", "")
        self.f, self.month = query.get("f", ""), query.get("month", "")
        if self.screen not in SCREENS:
            fail(400, "invalid_screen", "Choose a documented list.")

        if self.screen in REPORT_SCREENS and any(query.get(k) for k in ("f", "q")):
            fail(400, "invalid_filter", "Report sections accept only their month scope.")
        if self.screen in REPORT_SCREENS and not self.month:
            fail(400, "invalid_month", "Choose a report month in YYYY-MM format.")
        allowed = {
            "tranzactii": ("", "all", "in", "out"),
            "vanzari": ("", "open", "late", "storno"),
            "parteneri": ("", "open", "late", "accountant"),
        }.get(self.screen, ("", "open", "late"))
        if self.f not in allowed or self.month and self.screen == "parteneri":
            fail(400, "invalid_filter", "Choose a supported list filter.")
        self.start, self.end = _transaction_range(self.screen, query)
        if self.month and not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", self.month):
            fail(400, "invalid_month", "Use a month in YYYY-MM format.")
        self.report_month = self.month if self.screen in REPORT_SCREENS else ""
        if self.report_month:
            self.month = ""
        self.q = search.fold(re.sub(r"^" + search.SPACE + r"+|" + search.SPACE + r"+$", "", query.get("q", "")))
        self.after, self.limit = _cursor(query.get("cursor", "")), int(query.get("limit", "50"))
        self.position = self.total = self.remaining = 0
        self.months: set[str] = set()
        self.rows: list[tuple[tuple[str, int], dict]] = []

    def add(self, part: dict) -> None:
        selected = []
        for day, text, opened, late, row in _items(part, self.screen, self.report_month):
            # De explicat lists a debit waiting for its invoice last: an empty day sorts after every date
            waits = self.screen == "tranzactii" and not self.f and row["transaction"]["awaiting"]
            key = ("" if waits else day, -self.position)
            self.position += 1
            if day:
                self.months.add(day[:7])
            if not _choice_selected(row, self.f):
                continue
            if self.screen == "tranzactii" and not transaction_rows.selected(row, self.f, self.start, self.end):
                continue
            if (
                self.f == "open"
                and not opened
                or self.f == "late"
                and not late
                or self.month
                and not day.startswith(self.month)
                or self.q
                and self.q not in search.fold(text)
            ):
                continue
            self.total += 1
            if self.after is not None and key >= self.after:
                continue
            self.remaining += 1
            selected.append((key, row))
        selected = sorted([*self.rows, *selected], key=lambda r: r[0], reverse=True)[: self.limit]
        self.rows = [(key, bounded(row)) for key, row in selected]

    def result(self) -> dict:
        cursor = f"{self.rows[-1][0][0]}:{-self.rows[-1][0][1]}" if self.remaining > self.limit else None
        return {
            "data": [row for _, row in self.rows],
            "total": self.total,
            "next_cursor": cursor,
            "months": sorted(self.months, reverse=True),
        }
