"""Paged transaction-form choices over complete identities and remaining amounts; no linking decision. The one
invoice the bank's details name by number at exactly its remaining amount is proposed first; the user confirms it."""

from __future__ import annotations

import re
from decimal import Decimal

from fiscalm import links
from fiscalm.api import search, state as api
from fiscalm.api.responses import bounded, fail, page

COLLECTIONS = ("partners", "invoices", "payments", "selected", "receipts")


def options(query: dict) -> tuple[str, int, int]:
    collection, cursor = query.get("collection", ""), query.get("cursor", "")
    if (
        set(query) - {"id", "collection", "partner", "q", "cursor", "limit"}
        or not query.get("id")
        or collection not in ("", *COLLECTIONS)
        or ("partner" in query and collection not in ("", "invoices"))
        or ("q" in query and collection not in ("partners", "invoices", "payments"))
        or len(query.get("q", "")) > 200
    ):
        fail(400, "invalid_query", "Choose a transaction and a documented form collection.")
    page([], "id", query)
    if cursor and (not collection or not re.fullmatch(r"[0-9]{1,8}", cursor)):
        fail(400, "invalid_cursor", "Use the cursor returned for the same form collection.")
    return collection, int(cursor or "0"), int(query.get("limit", "50"))


def project(full: dict, query: dict) -> dict:
    collection, offset, limit = options(query)
    tx = next((t for t in full["transactions"]["rows"] if t["id"] == query["id"]), None)
    if tx is None:
        fail(404, "not_found", "The transaction is not in this company's current list.")
    link = (tx.get("explanation") or {}).get("link") or {}
    needle = re.sub(f"^{search.SPACE}+|{search.SPACE}+$", "", search.fold(query.get("q", "")))
    offer = proposed(full, tx)
    pages = {} if collection else {"proposed": {"data": offer, "total": len(offer), "next_cursor": None}}
    # the proposed invoice's partner comes before the one the bank text points to, so the form shows what it preselects
    partner = query.get("partner", link.get("partner") or (offer or [{}])[0].get("partner") or tx.get("partner") or "")
    for name in (collection,) if collection else COLLECTIONS:
        rows = records(full, tx, name, partner, searched=bool(needle))
        rows = [r for r in rows if not needle or needle in search.fold(terms(r, name))]
        if offset > len(rows):
            fail(400, "invalid_cursor", "This cursor is no longer in the form collection.")
        selected = rows[offset : offset + limit]
        if name == "payments":
            selected = [r | {"id": api.payment_id(r["payment"]["item_id"])} for r in selected]
        pages[name] = {
            "data": [bounded(r) for r in selected],
            "total": len(rows),
            "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
        }
    return {"pages": pages}


def unread_upload(d: dict) -> bool:
    return d["held"] and d["amount"] is None and not d["sent"] and not d["invoice"]


def partner_name(row: dict) -> str:
    return row["name"] or f"CUI {row['cui'] if row['cui'] is not None else 'null'}"


def records(full: dict, tx: dict, collection: str, partner: str, searched: bool = False) -> list[dict]:
    credit = tx["direction"] == "credit"
    if collection == "partners":
        eligible = (
            p
            for p in full["partners"]
            if ("client" if credit else "supplier") in p["roles"] or (credit and p.get("held"))
        )
        return [
            p | {"held": [], "report_formats": []} for p in sorted(eligible, key=lambda p: partner_name(p).casefold())
        ]
    if collection == "payments":
        return [] if credit else prepared(full, tx, searched)
    if collection == "receipts":  # uploads with no data of their own yet: a debit's bon to name (tx-receipt)
        return [] if credit else [d for d in full["documents"] if unread_upload(d)]
    if collection == "selected":
        link = (tx.get("explanation") or {}).get("link") or {}
        return [r for r in invoices(full, tx, link.get("partner") or "") if r["selected"]]
    return invoices(full, tx, partner)


def prepared(full: dict, tx: dict, searched: bool) -> list[dict]:
    """The prepared payments a debit could be, all in lei: offered, those of exactly its amount in lei (a card's
    original amount when that was in lei, by its size: links.plan reads it so), the amount the payment matcher asks
    for too (recon.auto_mark_paid); searched, the whole file, for one paid with a fee or another difference."""
    orig = tx.get("orig") or {}
    lei = tx["amount"] if tx["currency"] == "RON" else orig.get("amount") if orig.get("currency") == "RON" else None
    if lei is None:
        return []
    return [
        {"payment": p}
        for name in ("held", "in_file", "lost", "imported")
        for p in full["prepared_payments"][name]
        if searched or Decimal(p["amount"]) == abs(Decimal(lei))
    ]


def terms(row: dict, collection: str) -> str:
    if collection == "partners":
        return f"{partner_name(row)} {row['cui'] or ''}"
    if collection == "payments":
        p = row["payment"]
        return " ".join(p.get(k) or "" for k in ("payee", "ref", "details", "amount"))
    return " ".join(row.get(k) or "" for k in ("number", "date", "left", "total"))


def _currencies(tx: dict) -> set[str]:
    return {c for c in (tx["currency"], (tx.get("orig") or {}).get("currency")) if c}


def _open(full: dict, tx: dict) -> list[dict]:
    """The firm's invoices a transaction of this direction may still close, in one of its currencies."""
    credit, currencies = tx["direction"] == "credit", _currencies(tx)
    return [
        r
        for r in (full["issued"] if credit else full["invoices"])
        if r["status"] in ("open", "marked")
        and Decimal(r["left"]) > 0
        and (r.get("currency") if credit else r.get("currency") or "RON") in currencies
        and (not credit or (not r.get("rejected") and r.get("local_id") != r["id"]))
    ]


def proposed(full: dict, tx: dict) -> list[dict]:
    """The open invoice the bank's details name by its number, when the transaction is exactly what is left of it in
    its currency: at most one, never applied. A number that names several open invoices, or another amount, proposes
    nothing. The details are only compared with the firm's own invoice numbers. Without one, the invoices the AI
    integration proposed (ai_explain), while every one is still open and what is left of them is the amount."""
    if tx.get("explanation"):
        return []
    return _named(full, tx) or _guessed(full, tx)


def _guessed(full: dict, tx: dict) -> list[dict]:
    """The AI proposal's invoices (ai_explain) while they still add up and are all still its partner's."""
    p = tx.get("proposal") or {}
    picks = exactly(full, tx, p.get("invoices", [])) if p.get("kind") == "ai" else []
    return picks if all(r["partner"] == p["as"]["partner"] for r in picks) else []


def exactly(full: dict, tx: dict, ids: list[str]) -> list[dict]:
    """The picks of invoices `ids` when every one is open for the transaction, all in one currency, and what is left of
    them is its amount in that currency to the cent; else none."""
    left = {r["id"]: r for r in _open(full, tx)}
    rows = [left[i] for i in dict.fromkeys(ids) if i in left]
    currencies = {r.get("currency") or "RON" for r in rows}
    if not rows or len(rows) != len(ids) or len(currencies) != 1:
        return []
    orig = tx.get("orig") or {}
    amount = {tx["currency"]: tx["amount"], orig.get("currency"): orig.get("amount")}.get(currencies.pop())
    if not amount or sum(Decimal(r["left"]) for r in rows) != abs(Decimal(amount)):
        return []
    return [pick(r, r["left"], False) | {"position": n} for n, r in enumerate(rows)]


def _named(full: dict, tx: dict) -> list[dict]:
    if not tx.get("details"):
        return []
    text = f" {links.plain(tx['details'])} "
    found = [r for r in _open(full, tx) if links.names_number(text, r["number"] or "")]
    if len(found) != 1:
        return []
    r, orig = found[0], tx.get("orig") or {}
    amount = {tx["currency"]: tx["amount"], orig.get("currency"): orig.get("amount")}.get(r.get("currency") or "RON")
    return [pick(r, r["left"], False) | {"position": 0}] if amount and Decimal(amount) == Decimal(r["left"]) else []


def invoices(full: dict, tx: dict, partner: str) -> list[dict]:
    credit = tx["direction"] == "credit"
    source = full["issued"] if credit else full["invoices"]
    link = (tx.get("explanation") or {}).get("link") or {}
    mine = {p["id"]: p["amount"] for p in link.get("parts", [])} if link.get("kind") == "documents" else {}
    held = {h["invoice"] for p in full["partners"] if p["id"] == partner for h in p.get("held", [])}
    rows = [r for r in _open(full, tx) if partner and (r["partner"] == partner or (credit and r["id"] in held))]
    rows.sort(key=lambda r: (r["issue_date"] or "", r["number"].casefold()))
    picks = [pick(r, f"{Decimal(r['left']) + Decimal(mine.get(r['id'], '0')):.2f}", r["id"] in mine) for r in rows]
    present, by_id = {r["id"] for r in rows}, {r["id"]: r for r in source}
    for identity, amount in mine.items():
        r = by_id.get(identity)
        if r and identity not in present and (r["partner"] == partner or (credit and link.get("partner") == partner)):
            picks.append(pick(r, amount, True))
    return [r | {"position": i} for i, r in enumerate(picks)]


def pick(row: dict, left: str, selected: bool) -> dict:
    return {
        "id": row["id"],
        "number": row["number"],
        "date": row["issue_date"],
        "left": left,
        "total": row["total"],
        "currency": row.get("currency") or "RON",
        "partner": row["partner"],
        "selected": selected,
    }
