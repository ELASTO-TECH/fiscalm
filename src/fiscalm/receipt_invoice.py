"""A read receipt finds the e-Factura invoice of the same purchase, when exactly one fits.

A shop that issues an e-Factura for a till purchase sends it a few days after the receipt. A receipt filled from its
own reliable reading (receipt_header) fits an invoice of its own firm when the supplier's tax id is the same, whole
(identity.tax_token: a Romanian CUI by its digits, a foreign VAT id with its country), the total and currency are equal
to the cent and the invoice is dated from the receipt's day to WINDOW days after. Rivals are counted before anything
is chosen: every invoice of the firm whatever its state, every reading of the firm whatever its age. Only a receipt
that fits exactly one invoice, which no other receipt fits, is associated, and only while that invoice is open, clean
and unexplained and the receipt still holds its reading's data (AGENTS.md rule 2: when in doubt, do nothing).

A till receipt (doc_type receipt) that prints how it was paid proves the invoice paid at the till: Ledger.link_bon
reserves `bon:<sha>` like a debit, so the receipt pays that one invoice, ever; a card purchase's debit is then used up
by Ledger.settle_bon_card, a cash one never looks for a debit. Any other receipt is only associated
(Ledger.attach_receipt). Runs in the tick, and in e-Factura sync before the autopay rule, so a purchase already paid
at the till never reaches a payment file.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fiscalm import companies, history, receipt_header
from fiscalm.identity import tax_token
from fiscalm.ledger import Ledger

WINDOW = 7  # days an invoice may follow its till receipt
RECENT = 90  # days back a receipt still looks for its invoice


def _fits(found: dict, supplier: str, invoice, *, rival: bool = False) -> bool:
    """The invoice is this receipt's purchase: its verified supplier, the exact total and currency, in the window.
    As a `rival` it only may be: a row kept with its tax id's digits alone, or nothing left to pay (an invoice whose
    payable the payment at the till brought to zero)."""
    try:
        day, issued = date.fromisoformat(found["doc_date"]), date.fromisoformat(invoice["issue_date"] or "")
        payable, paid = Decimal(invoice["total"] or ""), Decimal(found["amount"])
    except (ValueError, InvalidOperation):
        return False
    if not (supplier and day <= issued <= day + timedelta(days=WINDOW)):
        return False
    same_currency = invoice["currency"] == found["currency"]
    if not rival:
        return bool(invoice["supplier_tax_id"]) and _supplier(invoice) == supplier and same_currency and payable == paid
    digits = re.sub(r"\D", "", supplier)
    maybe = _supplier(invoice) == supplier or not invoice["supplier_tax_id"] and _supplier(invoice) == digits
    return maybe and (payable == 0 or same_currency and payable == paid)


def _supplier(invoice) -> str:
    """The invoice's supplier as identity.tax_token reads it; a row kept before its full tax id has its digits only."""
    if invoice["supplier_tax_id"]:
        return tax_token(invoice["supplier_tax_id"])
    return re.sub(r"\D", "", invoice["supplier_cui"] or "")


def _open(invoice) -> bool:
    """An invoice a receipt may still take: open, parsed, without recorded issues (another buyer, a bad account), and
    neither associated with a document nor explained by a transaction."""
    return (
        invoice["status"] == "open"
        and invoice["parse_error"] is None
        and not json.loads(invoice["issues"] or "[]")
        and not invoice["taken"]
    )


def _readings(L: Ledger, company: str) -> list[tuple]:
    """(document, its reading's fields, supplier tax id, receipt pays) for every reliable reading of the firm."""
    out = []
    for row in L.db.execute(
        "SELECT d.*, h.header, u.uploaded_at, EXISTS (SELECT 1 FROM tx_link_items WHERE item='receipt '||d.sha) AS named "
        "FROM documents d JOIN receipt_headers h USING(sha) JOIN receipt_uploads u USING(sha) "
        "WHERE d.company=? AND d.kind='receipt' AND d.source LIKE 'upload:%' AND h.header IS NOT NULL",
        (company,),
    ):
        header = json.loads(row["header"])
        found = receipt_header.fields(header, company, row["uploaded_at"])
        if found:
            # proof of payment: a till receipt whose AI answer and printed text (when read) agree on card or cash
            said = header.get("payment_said", "")
            pays = (
                header["doc_type"] == "receipt"
                and header.get("payment") in ("card", "cash")
                and said == header["payment"]
            )
            out.append((row, found, tax_token(header["seller_tax_id"]), pays))
    return out


def pairs(L: Ledger, company: str, today: date) -> list[tuple[str, str, bool]]:
    """(receipt sha, invoice message id, the receipt proves payment) for each receipt and invoice that fit only each
    other, when both may still be changed."""
    invoices = L.db.execute(
        "SELECT e.*, EXISTS (SELECT 1 FROM documents d WHERE d.efactura_id=e.message_id) "
        "OR EXISTS (SELECT 1 FROM tx_link_items WHERE item='efactura '||e.message_id) AS taken "
        "FROM efactura e WHERE company=? AND doc_type='invoice' AND status!='duplicate'",
        (company,),
    ).fetchall()
    invoices += [  # history invoices are rivals too, never taken (history.rows: one sync also brought is not listed)
        {
            "supplier_tax_id": h["party_tax_id"],
            "supplier_cui": h["party_cui"],
            "status": "history",
            "message_id": "history:" + h["xml_sha"],
        }
        | {k: h[k] for k in ("issue_date", "total", "currency")}
        for h in history.rows(L, company, "received")
        if h["doc_type"] == "invoice"
    ]
    readings = _readings(L, company)
    fits = {
        row["sha"]: [i for i in invoices if _fits(found, supplier, i, rival=True)]
        for row, found, supplier, _ in readings
    }
    claims = Counter(i["message_id"] for found in fits.values() for i in found)
    since = (today - timedelta(days=RECENT)).isoformat()
    out = []
    for row, found, supplier, pays in readings:
        mine = fits[row["sha"]]
        filled = all(row[k] == v for k, v in found.items() if k != "vendor")  # still the reading's own data
        free = row["efactura_id"] is None and not row["named"] and found["doc_date"] >= since
        sure = len(mine) == 1 and _fits(found, supplier, mine[0])
        if sure and claims[mine[0]["message_id"]] == 1 and _open(mine[0]) and filled and free:
            out.append((row["sha"], mine[0]["message_id"], pays))
    return out


def associate(L: Ledger, today: date) -> int:
    """Associate each firm's receipts with the one invoice each fits; every pair is decided again under the write lock,
    so a receipt or invoice that changed since is left alone. Returns how many were associated."""
    done = 0
    for company in companies.all(L):
        for sha, mid, _ in pairs(L, company, today):
            with L.tx():
                again = {(s, m): pays for s, m, pays in pairs(L, company, today)}
                if (sha, mid) in again:
                    paid = again[(sha, mid)]
                    note = "plătit la casă, bon citit automat"
                    done += L.link_bon(sha, mid, note) if paid else L.attach_receipt(sha, company, mid)
    return done
