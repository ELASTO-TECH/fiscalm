"""Transaction display/search projection, using complete identities before rows are paged.

No settlement decision: these are the existing app's words and targets for the engine's explanations/proposals.
Keeping the descriptor with the row lets API search and the paged UI use the same complete references.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal
from types import MappingProxyType

from fiscalm.api import transaction_choices
from fiscalm.api.responses import bounded, fail
from fiscalm.pack import LUNI

LABELS = MappingProxyType(
    {"bank_fee": "Comision bancar", "interest": "Dobândă", "personal": "Cheltuială personală", "other": "Altceva"}
)
KINDS = MappingProxyType({"documents": "Facturi sau o plată pregătită", "transfer": "Transfer între conturi"})


def _count(n: int) -> str:
    if n == 1:
        return "o factură"
    return f"{n}{' de' if n >= 20 and (n % 100 == 0 or n % 100 >= 20) else ''} facturi"


def _one(refs: dict, name: str, ids: list) -> dict | None:
    return refs[name].get(ids[0]) if len(ids) == 1 else None


def _explained(e: dict, refs: dict) -> dict | None:
    partial = any(not p["closed"] for p in (e.get("link") or {}).get("parts", []))
    if e["kind"] == "payment":
        row = _one(refs, "invoices", e["invoices"])
        if row:
            return {
                "text": f"Plata parțială a facturii {row['number']}" if partial else f"Plata facturii {row['number']}",
                "to": {"inv": row["id"]},
            }
        if len(e["invoices"]) > 1:
            return {"text": f"Plata a {_count(len(e['invoices']))}"}
        payment = next(iter(e["payments"]), {})
        suffix = payment.get("details") or payment.get("ref") or ""
        if len(suffix) > 1 and suffix[0].isupper() and suffix[1].islower():
            suffix = suffix[0].lower() + suffix[1:]
        return {"text": "Plată din fișierul de plăți" + (", " + suffix if suffix else ""), "to": {"tab": "plati"}}
    if e["kind"] == "collection":
        settlement = refs["settlements"].get(e.get("settlement"))
        if settlement:
            return {
                "text": f"Decontare {settlement['payer']['name']}, {_count(len(e['issued']))}",
                "to": {"set": settlement["id"]},
            }
        row = _one(refs, "issued", e["issued"])
        if row:
            return {
                "text": f"Încasarea parțială a facturii {row['number']}"
                if partial
                else f"Încasarea facturii {row['number']}",
                "to": {"iss": row["id"]},
            }
        return {"text": f"Încasarea a {_count(len(e['issued']))}"}
    if e["kind"] == "receipt":
        doc, row = refs["documents"].get(e["document"]), _one(refs, "invoices", e["invoices"])
        if doc:  # a bon not read yet is named by its day, never by its file
            day = doc.get("date") or ""
            name = (
                f"Bon de la {doc['vendor']}"
                if doc["vendor"]
                else f"Bon din {int(day[8:])} {LUNI[int(day[5:7]) - 1]}"
                if day
                else "Bon"
            )
            return {"text": name, "to": {"doc": doc["id"]}}
        return {"text": f"Bon pentru factura {row['number']}", "to": {"inv": row["id"]}} if row else {"text": "Bon"}
    return _simple(e)


def _simple(e: dict) -> dict:
    return {
        "text": {
            "label": lambda: f"Fără document, {LABELS[e['label']].lower()}",
            "advance": lambda: f"Avans, {e['who']}",
            "transfer": lambda: "Transfer între conturi",
        }[e["kind"]]()
    }


def _proposed(p: dict, refs: dict) -> dict | None:
    if p["kind"] == "invoices":
        return {"text": f"Se potrivește cu {_count(len(p['invoices']))}", "to": {"tab": "sarcini"}, "proposal": True}
    if p["kind"] == "settlement":
        return {"text": "Decontare de verificat", "to": {"set": p["settlement"]}, "proposal": True}
    if p["kind"] == "remembered":
        choice = p["as"]
        partner = refs["partners"].get(choice.get("partner"))
        text = remembered_text(choice, partner)
        return {"text": "Regulă salvată: " + text.lower(), "proposal": True}
    if p["kind"] == "receipt":
        doc = refs["documents"].get(p["document"])
        return {
            "text": f"Poate fi bonul de la {doc['vendor']}" if doc else "Poate fi un bon",
            **({"to": {"doc": doc["id"]}} if doc else {}),
            "proposal": True,
        }
    return None


def remembered_text(choice: dict, partner: dict | None) -> str:
    """The existing remembered-choice label, shared with its bounded management list."""
    if choice["kind"] == "label":
        return f"Fără document, {LABELS[choice['reason']].lower()}"
    if choice["kind"] == "advance":
        return f"Avans, {choice.get('note') or ''}"
    if partner:
        return f"De la {transaction_choices.partner_name(partner)}"
    return KINDS[choice["kind"]]


def _typed(amount: str) -> list[str]:
    whole, _, bani = amount.lstrip("-").partition(".")
    whole = whole.lstrip("0") or "0"
    grouped = re.sub(r"\B(?=([0-9]{3})+(?![0-9]))", ".", whole)
    formatted = ("-" if Decimal(amount) < 0 else "") + grouped + "," + bani
    return [amount, amount.replace(".", ",", 1), formatted]


def items(part: dict) -> Iterator[tuple]:
    refs = {
        key: {r["id"]: r for r in part.get(key, [])}
        for key in ("invoices", "issued", "documents", "partners", "settlements")
    }
    for tx in part["transactions"]["rows"]:
        said = (
            _explained(tx["explanation"], refs)
            if tx["explanation"]
            else _proposed(tx["proposal"], refs)
            if tx["proposal"]
            else None
        )
        terms = [
            tx["counterparty"],
            tx["details"],
            *_typed(tx["amount"]),
            *(_typed(tx["orig"]["amount"]) if tx["orig"] else []),
            said["text"] if said else None,
        ]
        yield (
            tx["date"],
            " ".join(str(t) for t in terms if t),
            False,
            False,
            {"id": tx["id"], "company": part["co"], "transaction": tx, "said": said},
        )


def selected(row: dict, mode: str, start: str, end: str) -> bool:
    tx = row["transaction"]
    return (
        {"": not tx["explanation"], "all": True, "in": tx["direction"] == "credit", "out": tx["direction"] == "debit"}[
            mode
        ]
        and (not start or tx["date"] >= start)
        and (not end or tx["date"] <= end)
    )


def detail(full: dict, shell: dict, tx: dict, offset: int, limit: int) -> dict:
    """Keep complete display facts before cutting the user's covered rows to one page."""
    explanation = tx.get("explanation") or {}
    link = explanation.get("link") or {}
    descriptor = next(items(full | {"transactions": {"rows": [tx]}}))[-1]["said"]
    covered = covered_rows(full, tx, link)
    if offset > len(covered):
        fail(400, "invalid_cursor", "This cursor is no longer in the transaction's covered rows.")
    other = explanation.get("other") if explanation.get("kind") == "transfer" else None
    partners = {link.get("partner"), tx.get("partner")}
    state = shell | {
        "transactions": shell["transactions"]
        | {"rows": [bounded(tx), *(bounded(t) for t in full["transactions"]["rows"] if t["id"] == other)]},
        "partners": [p | {"held": [], "report_formats": []} for p in full["partners"] if p["id"] in partners],
        "settlements": [bounded(r) for r in full["settlements"] if r["id"] == explanation.get("settlement")],
    }
    return {
        "state": state,
        "transaction": {
            "said": descriptor,
            "parts_count": len(link.get("parts", [])),
            "payments_count": len(link.get("payments", [])),
            "choices": transaction_choices.project(full, {"id": tx["id"]})["pages"] if tx["can_link"] else {},
        },
        "related": {
            "data": covered[offset : offset + limit],
            "total": len(covered),
            "next_cursor": str(offset + limit) if offset + limit < len(covered) else None,
        },
    }


def covered_rows(full: dict, tx: dict, link: dict) -> list[dict]:
    """The old Covered view's order and missing-reference fallbacks, with exact decimal strings."""
    source = "issued" if tx["direction"] == "credit" else "invoices"
    invoices = {r["id"]: r for r in full[source]}
    rows = []
    for part in link.get("parts", []):
        invoice = invoices.get(part["id"], {})
        currency = invoice.get("currency", tx["currency"])
        rows.append(
            {
                "kind": "invoice",
                **part,
                "number": invoice.get("number", part["id"]),
                "left": invoice.get("left", "0.00"),
                "currency": tx["currency"] if currency is None else currency,
            }
        )
    return rows + [{"kind": "payment", "id": str(i), "payment": row} for i, row in enumerate(link.get("payments", []))]
