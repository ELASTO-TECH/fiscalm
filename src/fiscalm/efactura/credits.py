"""Conservative links and net amounts for received supplier credit notes."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal

from fiscalm.ledger import Ledger


def credit_number(value: str) -> str:
    value = re.sub(r"\b(?:nr|seria)\b[.]?", "", (value or "").upper(), flags=re.IGNORECASE)
    return "|".join((p.lstrip("0") or "0") if p.isdigit() else p for p in re.findall(r"[A-Z]+|[0-9]+", value))


# Issues parse_ubl notes that never decide who is paid or how much: a due date it could not read, or an extra account
# string that is not an IBAN (the invoice's valid IBANs are still checked on their own). Anything else blocks.
INFORMATIONAL_ISSUES = ("bad due date ", "payee account ")


def blocking_issues(row) -> list[str]:
    """The stored invoice's issues that block paying it or linking a credit note to it."""
    return [i for i in json.loads(row["issues"] or "[]") if not i.startswith(INFORMATIONAL_ISSUES)]


def credit_links(L: Ledger, company: str) -> dict[str, list[dict]]:
    """Unique original in this firm, same supplier and currency; overcredits refuse the whole group."""
    stored = L.efactura_rows(company=company)
    credits = [
        dict(r)
        for r in stored
        if r["doc_type"] == "credit_note"
        and not r["parse_error"]
        and r["status"] != "duplicate"
        and not json.loads(r["issues"] or "[]")
    ]
    if not credits:
        return {}
    rows = [dict(r) for r in stored if not r["parse_error"] and r["status"] != "duplicate"]
    linked: dict[str, list[dict]] = {}
    for credit in credits:
        reference = credit["billing_ref"]
        if not reference or not credit_number(reference):
            continue
        matches = [
            r
            for r in rows
            if r["doc_type"] == "invoice"
            and Decimal(r["total"]) > 0
            and r["supplier_cui"]
            and (r["supplier_tax_id"] or r["supplier_cui"]) == (credit["supplier_tax_id"] or credit["supplier_cui"])
            and r["currency"] == credit["currency"]
            and r["issue_date"] <= credit["issue_date"]
            and (not credit["billing_date"] or r["issue_date"] == credit["billing_date"])
            and not blocking_issues(r)  # the same rule as paying it: an original payable in full is also credited
            and credit_number(r["number"]) == credit_number(reference)
        ]
        if len(matches) == 1:
            linked.setdefault(matches[0]["message_id"], []).append(credit)
    totals = {r["message_id"]: Decimal(r["total"]) for r in rows if r["message_id"] in linked}
    return {
        key: group
        for key, group in linked.items()
        if 0 < sum((abs(Decimal(r["total"])) for r in group), Decimal(0)) <= totals[key]
    }


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
