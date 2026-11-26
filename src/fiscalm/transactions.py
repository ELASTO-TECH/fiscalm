"""Tranzacții: every statement transaction of a company, newest first, with what explains it (docs/openapi.json,
"Tranzacții"; docs/information-architecture.md section 4).

Read only. It shows what the engine already decided and decides nothing itself: a debit is a payment when
Ledger.paid_proofs proves it paid an invoice or a payment row, a credit is a collection when an issued invoice or a
settlement report holds it, a card debit is a receipt when a bon settled its invoice or recon.match_receipts finds the
receipt by its exact amount. The user's own explanation (fiscalm.links) comes before all of them. A transaction without
one is "de explicat"; what the engine would propose for it (the invoices an ambiguous debit fits, a settlement waiting
for a person, a receipt found by the shop's name) is shown as a proposal and never counts as an explanation, and so is
the partner its text points to (links.suggest).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta

from fiscalm import links, recon
from fiscalm.bank import statements
from fiscalm.companies import Company
from fiscalm.efactura import inbox as efactura
from fiscalm.invoicing import issued
from fiscalm.ledger import Ledger

# The reasons a difference can be named by (links.FITS), for the sheet: money in or out, less or more than the documents.
REASONS = {d: {"less": list(links.FITS[(d, -1)]), "more": list(links.FITS[(d, 1)])} for d in ("credit", "debit")}
MONTHS = 13  # this month and the twelve before it
DETAILS_MAX = 140  # a statement's free text, shortened: it is data from the bank, shown as text


def cutoff(today: date) -> str:
    """The first day Tranzacții lists (and Acasă reads its sums from): the first of the month MONTHS - 1 months back."""
    back = today.year * 12 + today.month - MONTHS  # the first month shown, counted back from this one
    return f"{back // 12:04d}-{back % 12 + 1:02d}-01"


def _text(value: str | None) -> str | None:
    """The bank's text as shown: whitespace folded, every IBAN and card number reduced to its last four, shortened."""
    return statements.masked(" ".join((value or "").split()))[:DETAILS_MAX] or None


def view(
    L: Ledger,
    company: Company,
    today: date,
    *,
    settlements: list[dict],
    ambiguous: list[dict],
    live: bool = False,
    partners: dict[str, str | None] | None = None,
) -> dict:
    """`rows`: the company's statement transactions of the last MONTHS months, newest first; `to_explain`: how many
    have no explanation. The lines are Ledger.account_lines (the ones Acasă sums): a twin whose other copy is on record
    is left out, a twin without one is a real payment and stays, to explain. `settlements` and `ambiguous` are the state's own lists
    (settlements.snapshot, api.ambiguous), so a proposal is exactly what Sarcini shows. `can_link`: the user may
    explain the row on its sheet now (live; not a twin, not explained by the engine, not a Decontare's credit waiting
    for a person); `awaiting`: the user's note on a debit still to explain whose invoice has not arrived; `partner`: the user's explanation's partner, else the one the bank's text points to."""
    owners = L.card_owners()
    co = company.key
    found = [r for r in L.account_lines(co, cutoff(today)) if r["tx"] is not None]
    if not found:
        return {"rows": [], "to_explain": 0, "reasons": REASONS, "remembered": links.remembered(L.memories(co))}

    paid, payments, received = _debit_proofs(L, co)
    collected, settled, waiting = _credit_proofs(L, co, settlements)
    verified_waiting = set(waiting)
    choose = {a["tx"]: [c["id"] for c in a["candidates"]] for a in ambiguous}
    mine = links.explanations(L, co)
    mine |= {m["other"]: m for m in list(mine.values()) if m["other"]}  # a transfer explains its other end too

    rows, txs = _transaction_rows(found, owners, co)
    _settlement_proposals(settlements, txs, waiting)
    receipt_of = _receipt_matches(L, co, rows, txs, found, mine, owners)

    for i, row in enumerate(rows):
        key, what = row["id"], row.pop("_what")
        if what.startswith("twin "):  # a payment of its own whose reservation settles nothing: to explain
            continue
        if key in mine:
            row["explanation"], row["partner"], row["can_link"] = (
                _mine(mine[key], key, row["direction"]),
                mine[key]["partner"],
                True,
            )
            continue
        if row["direction"] == "debit":
            _explain_debit(row, i, what, received, paid, payments, choose, receipt_of)
        else:
            _explain_credit(row, settled, collected, waiting)
        row["can_link"] = not row["explanation"] and key not in verified_waiting
    awaited = L.awaited(co)
    for row in rows:
        row["awaiting"] = None if row["explanation"] else awaited.get(row["id"])
    loose, kept = _remembered_proposals(L, co, rows, txs, partners, company.own_ibans)
    return {"rows": rows, "to_explain": len(loose), "reasons": REASONS, "remembered": links.remembered(kept)}


def _debit_proofs(L: Ledger, co: str) -> tuple[dict, dict, dict]:
    # Debits: what paid_proofs proves each one paid (an invoice, a payment row, or both: an invoice sent to the bank file).
    paid: dict[str, dict] = defaultdict(lambda: {"invoices": [], "payments": []})
    for what, key in L.paid_proofs(co).items():
        kind, _, ident = what.partition(" ")
        paid[key]["invoices" if kind == "efactura" else "payments"].append(ident)
    payments = {p["item_id"]: p for p in L.payments(company=co)} if any(v["payments"] for v in paid.values()) else {}
    received = {r["message_id"]: r for r in L.efactura_rows(company=co)}
    return paid, payments, received


def _credit_proofs(L: Ledger, co: str, settlements: list[dict]) -> tuple[dict, dict, dict]:
    # Credits: the issued invoices a statement credit collected, and the settlement report it is the payout of.
    collected: dict[str, list[str]] = defaultdict(list)
    for r in L.issued_rows(company=co):
        if r["status"] == "collected" and r["collected_tx"]:
            collected[r["collected_tx"]].append(r["message_id"])
    settled = {r["tx_key"]: r for r in settlements if r["status"] == "collected" and r["tx_key"]}
    waiting = {r["credit"]["id"]: r["id"] for r in settlements if r["status"] == "pending" and r.get("credit")}
    return collected, settled, waiting


def _transaction_rows(found: list[dict], owners: dict, co: str) -> tuple[list[dict], dict[str, statements.Tx]]:
    rows, txs = [], {}
    for r in found:
        t = statements.Tx.from_dict(json.loads(r["tx"]))
        txs[r["tx_key"]] = t
        rows.append(
            {
                "id": r["tx_key"],
                "company": co,
                "date": r["day"],
                "direction": r["direction"],
                "amount": f"{t.amount:.2f}",
                "currency": t.currency,
                "account": (r["iban"] or "")[-4:] or None,
                "source": "bank" if r["source"] == "ob" else "statement",
                "counterparty": _text(t.counterparty),
                "details": _text(t.description or t.title),
                "card": t.card_last4 or None,
                "card_owner": owners.get((co, t.card_last4)),
                "orig": {"amount": f"{t.orig_amount:.2f}", "currency": t.orig_currency}
                if t.orig_amount is not None and t.orig_currency and t.orig_currency != t.currency
                else None,
                "explanation": None,
                "proposal": None,
                # what the bank's own codes or words say the line is (statements.pays_nothing): a fee or interest
                "bank_says": t.category if t.category in (statements.BANK_FEE, statements.INTEREST) else None,
                "awaiting": None,
                "partner": None,
                "can_link": False,
                "_what": r["what"] or "",
            }
        )

    return rows, txs


def _settlement_proposals(settlements: list[dict], txs: dict[str, statements.Tx], waiting: dict) -> None:
    # A report with a wrong total is still a proposal on its unique payer credit, never an explanation.
    proposed: dict[str, list[str]] = defaultdict(list)
    for report in settlements:
        if report["status"] != "pending" or report.get("credit"):
            continue
        candidates = [
            key
            for key, tx in txs.items()
            if tx.direction == "credit"
            and tx.currency == report["currency"]
            and 0 <= (tx.date - date.fromisoformat(report["date"])).days <= 14
            and issued.same_firm(report["payer"]["name"], tx.counterparty)
        ]
        if len(candidates) == 1:
            proposed[candidates[0]].append(report["id"])
    waiting.update({key: ids[0] for key, ids in proposed.items() if len(ids) == 1 and key not in waiting})


def _receipt_matches(
    L: Ledger, co: str, rows: list[dict], txs: dict[str, statements.Tx], found: list[dict], mine: dict, owners: dict
) -> dict[int, tuple[str, str]]:
    # Card debits a receipt proves: the same matcher Rapoarte uses (recon.match_receipts), month by month, on the
    # debits that need a receipt. An exact amount explains the debit; a match by the shop's name alone is a proposal.
    receipts = recon._receipts(L, co)
    recon.classify(L, co, [txs[r["id"]] for r in rows])
    by_month: dict[str, list[tuple[int, statements.Tx]]] = defaultdict(list)
    # in the order the lines were recorded, which is the statement's (account after account, each in its own order,
    # as Rapoarte passes Statement.txs): the matcher gives each receipt to the first debit that fits. A twin still
    # listed settles nothing and is matched with nothing.
    for i in sorted(range(len(rows)), key=lambda i: found[i]["n"]):
        row, t = rows[i], txs[rows[i]["id"]]
        if (
            row["_what"].startswith("twin ")
            or row["id"] in mine
            or (t.card_last4 and owners.get((co, t.card_last4)) != co)
        ):
            continue
        if row["direction"] == "debit" and t.category == statements.NEEDS_INVOICE:
            by_month[row["date"][:7]].append((i, t))
    receipt_of: dict[int, tuple[str, str]] = {}
    for month, need in by_month.items():
        first = date.fromisoformat(month + "-01")
        lo = (first - timedelta(days=recon.MATCH_DAYS)).isoformat()
        hi = ((first + timedelta(days=32)).replace(day=1) + timedelta(days=recon.MATCH_DAYS)).isoformat()
        near = [d for d in receipts if lo <= d["doc_date"] <= hi]  # outside the window none can match: same answer
        for n, (how, doc) in recon.match_receipts([t for _, t in need], near).items():
            receipt_of[need[n][0]] = (how, doc["sha"])

    return receipt_of


def _explain_debit(
    row: dict, i: int, what: str, received: dict, paid: dict, payments: dict, choose: dict, receipt_of: dict
) -> None:
    key = row["id"]
    proof = paid.get(key)
    mid = what.removeprefix("card of bon, efactura ") if what.startswith("card of bon, efactura ") else None
    bon = (received[mid]["paid_tx"] or "") if mid in received else ""
    if mid in received and received[mid]["status"] == "paid" and bon.startswith("bon:") and len(bon) > 4:
        # this company's invoice, still proved at the till by its bon (Ledger.settle_bon_card's condition)
        row["explanation"] = {"kind": "receipt", "invoices": [mid], "document": bon.removeprefix("bon:")}
    elif proof:
        row["explanation"] = {
            "kind": "payment",
            "invoices": sorted(set(proof["invoices"])),
            "payments": [links.named_payment(payments[p]) for p in proof["payments"] if p in payments],
        }
    elif i in receipt_of and receipt_of[i][0] == "exact":
        row["explanation"] = {"kind": "receipt", "invoices": [], "document": receipt_of[i][1]}
    elif key in choose:
        row["proposal"] = {"kind": "invoices", "invoices": choose[key]}
    elif i in receipt_of:
        row["proposal"] = {"kind": "receipt", "document": receipt_of[i][1]}


def _explain_credit(row: dict, settled: dict, collected: dict, waiting: dict) -> None:
    key = row["id"]
    if key in settled:
        row["explanation"] = {
            "kind": "collection",
            "issued": settled[key]["links"]["issued"],
            "settlement": settled[key]["id"],
        }
    elif collected.get(key):
        row["explanation"] = {"kind": "collection", "issued": sorted(collected[key]), "settlement": None}
    elif key in waiting:
        row["proposal"] = {"kind": "settlement", "settlement": waiting[key]}


def _mine(m: dict, key: str, direction: str) -> dict:
    """The user's explanation (links.explanations) as Tranzacții names it, by its kind; `link` carries the decision."""
    if m["kind"] == "label":
        return {"kind": "label", "label": m["reason"], "note": m["note"], "link": m}
    if m["kind"] == "advance":
        return {"kind": "advance", "who": m["note"], "link": m}
    if m["kind"] == "receipt":
        return {"kind": "receipt", "invoices": [], "document": m["other"], "link": m}
    if m["kind"] == "transfer":
        return {"kind": "transfer", "other": m["other"] if m["tx"] == key else m["tx"], "link": m}
    ids = [p["id"] for p in m["parts"]]
    return (
        {"kind": "payment", "invoices": ids, "payments": m["payments"], "link": m}
        if direction == "debit"
        else {"kind": "collection", "issued": ids, "settlement": None, "link": m}
    )


def _remembered_proposals(
    L: Ledger, co: str, rows: list[dict], txs: dict[str, statements.Tx], partners: dict | None, own: frozenset[str]
) -> tuple[list[dict], list[dict]]:
    loose = [r for r in rows if not r["explanation"]]
    kept = L.memories(co)
    for r in rows:
        r["remember"] = links.shown_keys(efactura.line_keys(txs[r["id"]], own)) if r["can_link"] else {}
    loose_txs = [txs[r["id"]] for r in loose]
    guessed = L.ai_proposals(co)
    for r, partner, kept_as in zip(
        loose, links.suggest(L, co, loose_txs, partners or {}), links.proposals(co, loose_txs, kept, L), strict=True
    ):
        r["proposal"] = r["proposal"] or kept_as or (_guessed(guessed.get(r["id"])) if r["can_link"] else None)
        said = r["proposal"]["as"] if r["proposal"] and r["proposal"]["kind"] in ("remembered", "ai") else {}
        r["partner"] = (said.get("partner") if said.get("kind") == "documents" else None) or partner
    return loose, kept


def _guessed(p: dict | None) -> dict | None:
    """The AI integration's checked proposal (ai_explain), shaped like a remembered one, with the invoices it names."""
    if not p or p["kind"] == "none":
        return None
    return {"kind": "ai", "as": {k: p[k] for k in ("kind", "partner", "reason", "note")}, "invoices": p["invoices"]}
