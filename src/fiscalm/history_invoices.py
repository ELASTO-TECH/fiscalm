"""Shared start-date policy and storage for invoices without payment tracking."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from fiscalm import companies
from fiscalm.identity import tax_token
from fiscalm.ledger import Ledger, now

if TYPE_CHECKING:
    from fiscalm.efactura.inbox import Invoice


def direction(co: companies.Company, inv: Invoice) -> str | None:
    """'issued' when the firm is the supplier, 'received' when it is the buyer; None when it is neither (another
    firm's invoice) or both (nothing says which side to keep)."""
    ours = {tax_token(t) for t in co.tax_ids()} - {""}
    supplier, buyer = inv.supplier_tax_id in ours, inv.buyer_tax_id in ours
    return None if supplier == buyer else "issued" if supplier else "received"


def before_start(L: Ledger, company: str, issue_date: str) -> bool:
    start = L.get_meta(f"history_start:{company}")
    return bool(start and issue_date and issue_date < start)


def known(db, company: str, way: str, inv: Invoice, sha: str) -> bool:
    kind, _ = kind_total(inv) if way == "issued" else (inv.doc_type, Decimal(inv.total))
    party = None if way == "issued" else inv.supplier_tax_id
    return (
        db.execute(
            "SELECT 1 FROM history WHERE company=? AND (xml_sha=? OR (direction=? AND number=? AND issue_date=? "
            "AND doc_type=? AND (? IS NULL OR party_tax_id=?)))",
            (company, sha, way, inv.number, inv.issue_date, kind, party, party),
        ).fetchone()
        is not None
    )


def untracked(L: Ledger, company: str, inv: Invoice) -> bool:
    """Old invoices have no debt state; credit notes must still correct tracked debts."""
    return before_start(L, company, inv.issue_date) and inv.doc_type == "invoice" and Decimal(inv.total) >= 0


def kind_total(inv: Invoice) -> tuple[str, Decimal]:
    """Normalize an issued negative invoice as a credit note, with a positive correction amount."""
    total = Decimal(inv.total)
    kind = "credit_note" if inv.doc_type == "invoice" and total < 0 else inv.doc_type
    return kind, abs(total)


def store(L: Ledger, company: str, way: str, inv: Invoice, sha: str, path: Path) -> None:
    """Caller owns the ledger write transaction; share the import's identity check."""
    if known(L.db, company, way, inv, sha):
        return
    db = L.db
    kind, total = kind_total(inv) if way == "issued" else (inv.doc_type, Decimal(inv.total))
    name, cui, tax = (
        (inv.buyer_name, inv.buyer_cui, inv.buyer_tax_id)
        if way == "issued"
        else (inv.supplier_name, inv.supplier_cui, inv.supplier_tax_id)
    )
    db.execute(
        "INSERT INTO history(company,xml_sha,direction,doc_type,number,issue_date,due_date,currency,"
        "total,party_name,party_cui,party_tax_id,ibans,billing_ref,raw_path,imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            company,
            sha,
            way,
            kind,
            inv.number,
            inv.issue_date,
            inv.due_date or None,
            inv.currency,
            f"{total:.2f}",
            name,
            cui,
            tax,
            json.dumps(inv.ibans) if way == "received" else None,
            inv.billing_ref or None,
            str(path),
            now(),
        ),
    )
