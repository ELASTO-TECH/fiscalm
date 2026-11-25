"""Conservative links and net amounts for received supplier credit notes."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal

from fiscalm.ledger import Ledger

# A note without a reference cancels an invoice issued at most this long before it: e-Factura reaches the buyer within
# 5 working days of issue, and a full storno of a wrong invoice follows within days of it being seen.
UNREFERENCED_DAYS = 14
NO_INVOICE = "-"  # a person's answer that an unreferenced note cancels none of its candidates
UNCLEAR_REFERENCE = "unclear"  # billing_date of a note with a reference naming no single invoice: it links to none


def credit_number(value: str) -> str:
    value = re.sub(r"\b(?:nr|seria)\b[.]?", "", (value or "").upper(), flags=re.IGNORECASE)
    return "|".join((p.lstrip("0") or "0") if p.isdigit() else p for p in re.findall(r"[A-Z]+|[0-9]+", value))


# Issues parse_ubl notes that never decide who is paid or how much: a due date it could not read, or an extra account
# string that is not an IBAN (the invoice's valid IBANs are still checked on their own). Anything else blocks.
INFORMATIONAL_ISSUES = ("bad due date ", "payee account ")


def blocking_issues(row) -> list[str]:
    """The stored invoice's issues that block paying it or linking a credit note to it."""
    return [i for i in json.loads(row["issues"] or "[]") if not i.startswith(INFORMATIONAL_ISSUES)]


def _clean(r: dict) -> bool:
    return not r["parse_error"] and r["status"] != "duplicate" and not blocking_issues(r)


def _original_of(r: dict, credit: dict, *, flagged: bool = False) -> bool:
    """An invoice the credit note could correct: same supplier and currency, issued on or before it. `flagged` also
    admits an invoice with blocking issues: still a plausible original, so it counts against a unique match."""
    return (
        r["doc_type"] == "invoice"
        and Decimal(r["total"]) > 0
        and bool(r["supplier_cui"])
        and (r["supplier_tax_id"] or r["supplier_cui"]) == (credit["supplier_tax_id"] or credit["supplier_cui"])
        and r["currency"] == credit["currency"]
        and r["issue_date"] <= credit["issue_date"]
        and (flagged or not blocking_issues(r))  # the same rule as paying it
    )


def _reverses(r: dict, credit: dict) -> bool:
    """The exact opposite total, issued shortly before, not shown to have reached SPV after the note."""
    start = (date.fromisoformat(credit["issue_date"]) - timedelta(days=UNREFERENCED_DAYS)).isoformat()
    return (
        _original_of(r, credit, flagged=True)
        and Decimal(r["total"]) == abs(Decimal(credit["total"]))
        and r["issue_date"] >= start
        and not (_listed(r) and _listed(credit) and r["listed_at"] > credit["listed_at"])
    )


def _listed(r: dict) -> bool:
    """An SPV listing time (data_creare, YYYYMMDDHHMM); an imported file's issue day proves no order."""
    try:
        return datetime.strptime(r["listed_at"] or "", "%Y%m%d%H%M").strftime("%Y%m%d%H%M") == r["listed_at"]
    except ValueError:
        return False


def _before(r: dict, credit: dict) -> bool:
    """Proved in SPV before the note: both listing times valid, the invoice's earlier (the same minute proves none)."""
    return _listed(r) and _listed(credit) and r["listed_at"] < credit["listed_at"]


def _archived_rival(L: Ledger, company: str, credit: dict) -> bool:
    """An invoice kept only in history (before the firm's start) that the note could cancel just as well."""
    start = (date.fromisoformat(credit["issue_date"]) - timedelta(days=UNREFERENCED_DAYS)).isoformat()
    return any(
        (h["party_tax_id"] or h["party_cui"]) == (credit["supplier_tax_id"] or credit["supplier_cui"])
        and Decimal(h["total"]) == abs(Decimal(credit["total"]))
        for h in L.db.execute(
            "SELECT party_tax_id,party_cui,total FROM history WHERE company=? AND direction='received' "
            "AND doc_type='invoice' AND currency=? AND issue_date BETWEEN ? AND ?",
            (company, credit["currency"], start, credit["issue_date"]),
        )
    )


def _unreferenced(L: Ledger, company: str, credit: dict, fits: list[dict], pick: str | None) -> dict | None:
    """The invoice a note naming none cancels: the only fit, proved in SPV before the note and with no archived rival,
    or the one a person picked. None leaves it unapplied; a pick no longer among the fits never moves to another."""
    if pick is not None:
        return next((r for r in fits if r["message_id"] == pick), None)
    only = fits[0] if len(fits) == 1 else None
    if only and _original_of(only, credit) and _before(only, credit) and not _archived_rival(L, company, credit):
        return only
    return None


def links(L: Ledger, company: str) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Referenced notes link to their unique original; overcredits refuse the whole group. Then each note without a
    reference, oldest first, cancels the one invoice it fits that no other note took; several leave a question."""
    stored = [dict(r) for r in L.efactura_rows(company=company)]
    credits = [r for r in stored if r["doc_type"] == "credit_note" and _clean(r)]
    if not credits:
        return {}, {}
    rows = [r for r in stored if not r["parse_error"] and r["status"] != "duplicate"]
    linked: dict[str, list[dict]] = {}
    for credit in credits:
        reference = credit["billing_ref"]
        if not reference or not credit_number(reference):
            continue
        matches = [
            r
            for r in rows
            if _original_of(r, credit)
            and (not credit["billing_date"] or r["issue_date"] == credit["billing_date"])
            and credit_number(r["number"]) == credit_number(reference)
        ]
        if len(matches) == 1:
            linked.setdefault(matches[0]["message_id"], []).append(credit)
    taken = set(linked)
    totals = {r["message_id"]: Decimal(r["total"]) for r in rows if r["message_id"] in linked}
    linked = {
        key: group
        for key, group in linked.items()
        if 0 < sum((abs(Decimal(r["total"])) for r in group), Decimal(0)) <= totals[key]
    }
    questions: dict[str, list[dict]] = {}
    picks = L.credit_picks()
    reserved = {invoice: note for note, invoice in picks.items() if invoice != NO_INVOICE}  # answers hold their invoice
    unreferenced = (c for c in credits if c["billing_ref"] == "" and c["billing_date"] != UNCLEAR_REFERENCE)
    for credit in sorted(unreferenced, key=lambda c: (c["issue_date"], c["listed_at"] or "", c["message_id"])):
        note = credit["message_id"]
        fits = [
            r
            for r in rows
            if r["message_id"] not in taken and reserved.get(r["message_id"], note) == note and _reverses(r, credit)
        ]
        if chosen := _unreferenced(L, company, credit, fits, picks.get(note)):
            linked[chosen["message_id"]] = [credit]
            taken.add(chosen["message_id"])
        elif fits and note not in picks:
            questions[note] = fits
    return linked, questions


def credit_links(L: Ledger, company: str) -> dict[str, list[dict]]:
    """Invoice id -> the received credit notes that reduce it."""
    return links(L, company)[0]


def credit_questions(L: Ledger, company: str) -> dict[str, list[dict]]:
    """Credit note id -> the invoices it could cancel, when it names none and fits several: a person picks."""
    return links(L, company)[1]


def pick(L: Ledger, company: str, note: str, target: str) -> bool:
    """A person's answer to a credit_questions entry: one of its invoices, or NO_INVOICE. The first answer stands."""

    def asked() -> bool:  # read again inside the write lock: another answer may have taken the invoice
        fits = credit_questions(L, company).get(note)
        return fits is not None and target in {r["message_id"] for r in fits} | {NO_INVOICE}

    return L.pick_credit_original(note, target, asked)


def net_invoices(L: Ledger, rows: Iterable) -> list[dict]:
    totals: dict[str, dict[str, Decimal]] = {}
    out = []
    for row in rows:
        r = dict(row)
        if r["company"] not in totals:
            totals[r["company"]] = credited(L, r["company"])
        amount = totals[r["company"]].get(r["message_id"], Decimal(0))
        r["gross_total"] = r["total"]
        r["total"] = f"{max(Decimal(0), Decimal(r['total']) - amount):.2f}"
        if Decimal(r["total"]) > 0:
            out.append(r)
    return out


def credited(L: Ledger, company: str) -> dict[str, Decimal]:
    return {
        key: sum((abs(Decimal(r["total"])) for r in group), Decimal(0))
        for key, group in credit_links(L, company).items()
    }
