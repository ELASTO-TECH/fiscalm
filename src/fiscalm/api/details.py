"""Sparse document-sheet context, selected after complete identities and money are calculated."""

from __future__ import annotations

import json
import re

from fiscalm.api import transaction_rows, upload_assignment
from fiscalm.api.responses import bounded, fail, page
from fiscalm.ledger import Ledger
from fiscalm.mail import invoices as mail_invoices

KINDS = {
    "invoice": "invoices",
    "issued": "issued",
    "document": "documents",
    "mail": "to_check",
    "settlement": "settlements",
    "order": "emag_tasks",
    "transaction": "transactions",
}


def owners(L: Ledger, kind: str, identity: str, keys: list[str]) -> list[str]:
    """Resolve existing global IDs first; only read owners, preserving company order for aliases/mail choices."""
    if kind == "order":
        return [key for key in keys if key == identity.partition(":")[0]]
    if kind == "mail" and upload_assignment.staged(L, identity):
        return keys
    if kind == "mail":
        row = L.db.execute(
            "SELECT data FROM mail_questions WHERE sha=? AND answered_at IS NULL", (identity,)
        ).fetchone()
        offered = mail_invoices.offered(json.loads(row[0]), keys) if row else []
        return [key for key in keys if key in offered]
    tables = {
        "invoice": (("efactura", "message_id"),),
        "issued": (("issued", "message_id"), ("invoices", "id")),
        "document": (("documents", "sha"),),
        "settlement": (("settlement_reports", "id"),),
        "transaction": (("statement_txs", "tx_key"),),
    }
    found = set()
    for table, column in tables[kind]:
        row = L.db.execute(f"SELECT company FROM {table} WHERE {column}=?", (identity,)).fetchone()
        if row:
            found.add(row[0])
    return [key for key in keys if key in found]


def options(query: dict) -> tuple[str, str, int, int]:
    kind, identity = query.get("kind", ""), query.get("id", "")
    if set(query) - {"kind", "id", "cursor", "limit"} or kind not in KINDS or not identity:
        fail(400, "invalid_query", "Choose a documented document kind and its existing id.")
    page([], "id", query)
    cursor = query.get("cursor", "")
    if cursor and (
        kind not in ("invoice", "issued", "settlement", "transaction") or not re.fullmatch(r"[0-9]{1,8}", cursor)
    ):
        fail(400, "invalid_cursor", "Use the related-document cursor returned for this invoice.")
    return kind, identity, int(cursor or "0"), int(query.get("limit", "50"))


def find(full: dict, kind: str, identity: str) -> dict | None:
    if kind == "transaction":
        return next((r for r in full["transactions"]["rows"] if r["id"] == identity), None)
    if kind == "order":
        return next((r for r in full["emag_tasks"] if identity == f"{full['co']}:{r['id']}"), None)
    key = "key" if kind == "mail" else "id"
    return next(
        (r for r in full[KINDS[kind]] if identity == r[key] or (kind == "issued" and identity == r.get("local_id"))),
        None,
    )


def context(full: dict, shell: dict, kind: str, row: dict, offset: int, limit: int) -> dict:
    """Keep the purchase pairing, supplier refusal and partner capabilities intact."""
    if kind == "transaction":
        return transaction_rows.detail(full, shell, row, offset, limit)
    selected = {key: [] for key in ("invoices", "issued", "documents", "expenses", "partners", "refused", "to_check")}
    selected[KINDS[kind]] = [row]
    if kind in ("invoice", "document"):
        selected.update(purchase_context(full, kind, row))
    invoice_ids = {r["id"] for r in selected["invoices"]}
    selected["refused"] = [r for r in full["refused"] if r["id"] in invoice_ids]
    partner_ids = {r.get("partner") for key in ("invoices", "issued", "documents") for r in selected[key]}
    selected["partners"] = [p | {"held": []} for p in full["partners"] if p["id"] in partner_ids]
    related = relations(full, kind, row, offset, limit)
    result = shell | bounded(selected)
    result.update(related_context(full, kind, row, related["data"]))
    return {"state": result, "related": related}


def related_context(full: dict, kind: str, row: dict, data: list[dict]) -> dict:
    """Keep full primary scalars while replacing only the paged relation and its lookup context."""
    if kind == "invoice":
        return {"invoices": [bounded(row) | {"credits": data}]}
    if kind == "issued":
        originals = [r for r in full["issued"] if row.get("storno_of") and r.get("local_id") == row["storno_of"]]
        # The related page itself is bounded; preserve every requested row, even at the maximum limit.
        rows = [bounded(r) for r in (row, *originals, *data)]
        rows[0]["reversed_by"] = [r["local_id"] for r in data]
        return {"issued": rows}
    if kind == "settlement":
        return {"settlements": [bounded(row) | {"lines": data}]}
    if kind == "order":
        issued = next((r for r in full["issued"] if row.get("draft_id") and r.get("local_id") == row["draft_id"]), None)
        return {"issued": [bounded(issued)] if issued else []}
    return {}


def purchase_context(full: dict, kind: str, row: dict) -> dict:
    field = "invoice" if kind == "invoice" else "document"
    expenses = [e for e in full["expenses"] if e[field] == row["id"]]
    selected = {"expenses": expenses}
    for source, reference in (("invoices", "invoice"), ("documents", "document")):
        ids = {e[reference] for e in expenses}
        if source == KINDS[kind]:
            ids.add(row["id"])
        selected[source] = [r for r in full[source] if r["id"] in ids]
    return selected


def relations(full: dict, kind: str, row: dict, offset: int, limit: int) -> dict:
    rows = row.get("credits", []) if kind == "invoice" else []
    if kind == "settlement":
        rows = row["lines"]
    if kind == "issued":
        by_local = {r.get("local_id"): r for r in full["issued"] if r.get("local_id")}
        rows = [by_local[key] for key in row.get("reversed_by", []) if key in by_local]
    if offset > len(rows):
        fail(400, "invalid_cursor", "The related-row cursor is no longer in this document.")
    return {
        "data": [bounded(r) for r in rows[offset : offset + limit]],
        "total": len(rows),
        "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
    }
