"""Frozen pre-optimization readers from base 7ba5c2c, used only as equivalence oracles."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

# Frozen readers run with the original module globals (FunctionType below).
if TYPE_CHECKING:
    from fiscalm.api.state import (
        Decimal,
        Ledger,
        _partners,
        assign_partners,
        covered,
        date,
        efactura,
        iso_dt,
        money,
        received_owed,
        tax_id,
    )
    from fiscalm.efactura.inbox import OPEN


def state__invoices(
    L: Ledger, co: str, st: dict, today: date, live: bool, facts: efactura.PaymentFacts | None = None
) -> list[dict]:
    """Every received e-Factura invoice of the company (credit notes, duplicates and unreadable ones aside).
    status: open | linked (in the payment file) | marked (paid by the user, awaiting a statement debit or a mail, can be
    undone) | paid (confirmed) | skipped."""
    where = {p["item_id"]: k for k in ("held", "in_file", "lost") for p in st[k]}
    marked = {r["message_id"] for r in efactura.awaiting(L, co)}
    unpaid = {r["message_id"]: r for r in efactura.unpaid(L, co)}
    out = []
    for r in L.efactura_rows(company=co):
        if r["parse_error"] or r["status"] in ("credit", "duplicate"):
            continue
        r = unpaid.get(r["message_id"]) or dict(r)
        status = "marked" if r["message_id"] in marked else r["status"]
        if status == "linked" and r["message_id"] not in unpaid:  # its BT Go row is settled: paid, or dropped
            p = L.payment(r["item_id"]) if r["item_id"] else None
            status, r["paid_at"] = (
                ("skipped" if p and p["status"] == "skipped" else "paid"),
                p["paid_at"] if p else None,
            )
        ps = where.get(r["item_id"], "imported") if status == "linked" else None
        owed = received_owed(status, ps)
        part = covered(L, co, "efactura").get(r["message_id"], Decimal(0))
        out.append(
            {
                "id": r["message_id"],
                "company": co,
                "supplier": r["supplier_name"],
                "supplier_cui": tax_id(r.get("supplier_tax_id") or r["supplier_cui"]) or None,
                "partner": None,
                "number": r["number"],  # set by link_partners
                "issue_date": r["issue_date"],
                "due_date": r["due_date"],
                "currency": r["currency"] or "RON",
                "total": money(r["total"]),
                "credits": [],
                "credit_question": [],
                "credit_note": False,
                "status": status,
                "payment_state": ps,
                "item_id": r["item_id"],
                "payment_file": None,  # Scale fixtures contain no named-file membership.
                "amount_changed": False,
                "covered": money(part),
                "left": money(Decimal(r["total"]) - part if status in ("open", "marked", "linked") else 0),
                "paid_at": iso_dt(r["paid_at"]) if status in ("marked", "paid", "skipped") else None,
                "paid_method": r["paid_method"] if status in ("marked", "paid") else None,
                "paid_detail": (r["paid_detail"] or None) if status in ("marked", "paid") else None,
                "overdue": owed and bool(r["due_date"]) and r["due_date"] < today.isoformat(),
                "previous_iban_last4": [],  # The scale fixture has no proved supplier accounts.
                "accounts": [],  # nor invoices listing several IBANs.
                "can_approve": status == "open" and not part and efactura.payment_refusal(L, r) is None,
                "can_mark_paid": status == "open",
                "can_withdraw": ps in ("held", "in_file"),
                "can_reopen": status == "marked",
                "proved": status == "paid" and ("efactura " + r["message_id"]) in L.paid_proofs(co),
                "bon": (L.paid_proofs(co).get("efactura " + r["message_id"]) or "")[4:] or None
                if status == "paid" and (L.paid_proofs(co).get("efactura " + r["message_id"]) or "").startswith("bon:")
                else None,
            }
        )
    return sorted(out, key=lambda x: (x["issue_date"] or "", x["number"] or ""), reverse=True)


def state_link_partners(
    co: str,
    invoices: list[dict],
    rows: list[dict],
    L: Ledger | None = None,
    history: Sequence[dict] = (),
    credits: Sequence[dict] = (),
) -> list[dict]:
    """Use current identity rules while comparing the pre-optimization data readers."""
    assign_partners(co, invoices, rows, L, history)
    by = {r.get("id"): r["partner"] for r in rows}
    for c in credits:
        c["partner"] = by.get(c.pop("reduces", None) or "") or c["partner"]
    return _partners(co, invoices, rows, history, credits)


def inbox_unpaid(L: Ledger, company: str | None = None) -> list[dict]:
    """Open invoices plus approved ones whose payment is not confirmed yet, oldest due date first."""
    rows = []
    for r in L.efactura_rows(status=(OPEN, "linked"), company=company):
        r = dict(r)
        if r["parse_error"]:
            continue  # listed by unreadable()
        if r["status"] == "linked":
            p = L.payment(r["item_id"]) if r["item_id"] else None
            if not p or p["status"] in ("paid", "skipped"):
                continue
            r["payment_status"] = p["status"]
        rows.append(r)
    return sorted(rows, key=lambda r: (r["due_date"] or "9999", r["issue_date"] or ""))


def original_readers():
    """Temporarily install the original readers with their original module globals."""
    from contextlib import ExitStack
    from types import FunctionType
    from unittest.mock import patch

    from fiscalm.api import state as api
    from fiscalm.efactura import inbox as efactura

    stack = ExitStack()
    for module, names in (
        (api, ["link_partners", "_invoices"]),
        (efactura, ["unpaid"]),
    ):
        for name in names:
            ref = globals()[module.__name__.split(".")[-1] + "_" + name]
            original = FunctionType(ref.__code__, module.__dict__, name, ref.__defaults__)
            original.__kwdefaults__ = ref.__kwdefaults__
            stack.enter_context(patch.object(module, name, original))
    stack.enter_context(patch.object(api, "_StateReads", lambda L, registry: L))
    return stack
