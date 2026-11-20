"""Bounded partner-sheet histories and remembered explanations, with complete source identities."""

from __future__ import annotations

import re

from fiscalm.api import transaction_rows
from fiscalm.api.responses import bounded, fail, page

COLLECTIONS = ("invoices", "issued", "transactions", "held", "settlements", "remembered", "report_formats")


def options(query: dict) -> tuple[str, str, int, int]:
    identity, collection = query.get("id", ""), query.get("collection", "")
    if set(query) - {"id", "collection", "cursor", "limit"} or collection not in ("", *COLLECTIONS):
        fail(400, "invalid_query", "Choose a documented partner collection.")
    if not identity and collection != "remembered":
        fail(400, "invalid_query", "Name the partner, or choose the company's remembered explanations.")
    page([], "id", query)
    cursor = query.get("cursor", "")
    if cursor and (not collection or not re.fullmatch(r"[0-9]{1,8}", cursor)):
        fail(400, "invalid_cursor", "Use a cursor from the same partner collection.")
    return identity, collection, int(cursor or "0"), int(query.get("limit", "50"))


def project(full: dict, shell: dict, query: dict) -> dict:
    identity, collection, offset, limit = options(query)
    partner = next((p for p in full["partners"] if p["id"] == identity), None)
    if identity and partner is None:
        fail(404, "not_found", "The partner does not exist in this company.")
    pages = {}
    for name in (collection,) if collection else COLLECTIONS:
        rows = records(full, partner, name)
        if offset > len(rows):
            fail(400, "invalid_cursor", "This cursor is no longer in the partner collection.")
        pages[name] = {
            "data": [bounded(r) for r in rows[offset : offset + limit]],
            "total": len(rows),
            "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
        }
    # Preserve the complete identity, balances and capabilities; histories have their own exact page totals.
    state = shell | {"partners": [partner | {"held": [], "report_formats": []}] if partner else []}
    return {"state": state, "pages": pages}


def records(full: dict, partner: dict | None, collection: str) -> list[dict]:
    identity = partner["id"] if partner else None
    handlers = {
        "invoices": lambda: invoices(full, "invoices", "received", identity),
        "issued": lambda: invoices(full, "issued", "issued", identity),
        "transactions": lambda: linked_transactions(full, identity),
        "held": lambda: held(full, partner),
        "settlements": lambda: [r for r in full.get("settlements", []) if r["partner"] == identity],
        "remembered": lambda: remembered(full, identity),
        "report_formats": lambda: partner.get("report_formats", []),
    }
    return handlers[collection]()


def invoices(full: dict, collection: str, direction: str, identity: str | None) -> list[dict]:
    """The partner's tracked invoices of one direction and its history ones (kind history), newest first."""
    rows = [r for r in full[collection] if r["partner"] == identity]
    rows += [r for r in full["history"] if r["direction"] == direction and r["partner"] == identity]
    return sorted(rows, key=lambda r: r["issue_date"] or "", reverse=True)


def linked_transactions(full: dict, identity: str) -> list[dict]:
    transactions = full["transactions"] | {
        "rows": [
            r
            for r in full["transactions"]["rows"]
            if ((r.get("explanation") or {}).get("link") or {}).get("partner") == identity
        ]
    }
    # Descriptions use every referenced entity, even when that invoice is on a different page.
    return [entry[-1] for entry in transaction_rows.items(full | {"transactions": transactions})]


def held(full: dict, partner: dict) -> list[dict]:
    numbers = {r["id"]: r["number"] for r in full["issued"]}
    return [row | {"number": numbers.get(row["invoice"])} for row in partner.get("held", [])]


def remembered(full: dict, identity: str | None) -> list[dict]:
    partners = {p["id"]: p for p in full["partners"]}
    return [
        {"id": row["id"], "memory": row, "text": transaction_rows.remembered_text(row, partners.get(row["partner"]))}
        for row in full["transactions"]["remembered"]
        if identity is None or row["partner"] == identity
    ]
