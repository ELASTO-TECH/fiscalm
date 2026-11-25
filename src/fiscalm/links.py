"""The user explains a statement transaction fiscalm can't (docs/information-architecture.md section 4, "The user
explains what fiscalm can't"; docs/openapi.json, `tx-link` and `tx-unlink`).

On a transaction's sheet the user names who it is from or to (De la cine) and what it covers (Ce acoperă): one or more
of that partner's open invoices (issued ones for a credit, received ones for a debit) or one prepared payment. The
amounts don't have to agree. The difference is named (REASONS): a part still owed ("rest") keeps the newest invoice open
for that part, anything else (a courier's or the bank's fee, an exchange difference, something the user writes) closes
every invoice picked.

The rules (AGENTS.md rules 2 and 3): one transaction explains one set of documents, and a second explanation replaces
the first atomically (Ledger.link_tx); an invoice is closed exactly while what its explanations settle reaches what it
asked for, so one already covered can't be picked again; every write re-checks, under the write lock, the state the
plan was made from. A user's link to a transaction on record is the proof a payment waited for: an invoice the user had
marked paid is settled by it. A partly covered invoice is never settled by a matcher: it stays a rival for its
remaining amount, so a payment that may be its rest settles nothing else on its own.

The link model is the explanation of one transaction (tx_links) and the parts of items it settles (tx_link_items), with
a kind: `documents` here; a Decontare, a label, a transfer or an advance are more kinds of the same row.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

from fiscalm import companies, recon
from fiscalm.bank import statements
from fiscalm.commerce.webhooks import capture
from fiscalm.efactura import inbox as efactura
from fiscalm.efactura.inbox import firm_core
from fiscalm.identity import firm_id, tax_token
from fiscalm.invoicing import issued
from fiscalm.ledger import BOUNDARY_DAYS, Ledger, LinkRefused, covered, user_note
from fiscalm.payments import engine as pay
from fiscalm.settlements import engine as settlements

REASONS = ("rest", "courier_fee", "bank_fee", "fx", "other")
# The reasons that can name a difference: by the transaction's direction and whether it is less (-1) or more (+1)
# than what its documents had left. A courier keeps its fee out of money it collected; a bank takes its fee from money
# in, or adds it to money out.
FITS = {
    ("credit", -1): ("courier_fee", "bank_fee", "rest", "fx", "other"),
    ("credit", 1): ("fx", "other"),
    ("debit", -1): ("rest", "fx", "other"),
    ("debit", 1): ("bank_fee", "fx", "other"),
}
NOTE_MAX = 200
# Fără document: a transaction no document explains, named for the accountant
LABELS = ("bank_fee", "interest", "personal", "other")
WHO_MAX = 100  # an advance's administrator or associate, by name
KINDS = ("documents", "label", "transfer", "advance")  # and "receipt", only from an upload (receipt)
# What a saved rule explains on its own (apply_rules): kinds that link and settle nothing. A rule naming a partner's
# documents stays a proposal: picking the invoice is a person's (AGENTS.md rules 2 and 3).
AUTOMATIC = ("label", "advance")
# what an explanation can be remembered by: the counterparty's account, the card (a label only), a merchant's name
ID = re.compile(r"\d{1,30}")  # an e-Factura message id (efactura.valid_id)

REFUSED = {  # LinkRefused.code -> what the user reads
    "card_owner": "Tranzacția poate fi explicată după alegerea titularului cardului.",
    "card_firm": "Cardul nu aparține acestei firme. Plata nu se confirmă.",
    "unknown": "Tranzacția nu mai este în listă.",
    "changed": "Explicația tranzacției s-a schimbat între timp.",
    "used": "Tranzacția are deja o explicație.",
    "settlement": "Tranzacția așteaptă confirmarea unei decontări.",
    "item_changed": "O factură aleasă s-a schimbat între timp.",
    "nothing": "Lipsesc facturile sau plata pe care le acoperă tranzacția.",
    "partner": "Facturile alese trebuie să fie de la același partener.",
    "currency": "Tranzacția și facturile trebuie să fie în aceeași monedă.",
    "reason": "Alege motivul diferenței sau descrie-l.",
    "note": f"Explicația diferenței are nevoie de cel mult {NOTE_MAX} de caractere.",
    "short": "Suma nu ajunge pentru toate facturile alese.",
    "payment_rest": "O plată pregătită se acoperă întreagă. Diferența se numește altfel.",
    "label": "Alege tipul tranzacției și completează explicația cerută.",
    "label_note": f"Nota are nevoie de cel mult {NOTE_MAX} de caractere.",
    "who": f"Lipsește numele administratorului sau al asociatului, de cel mult {WHO_MAX} de caractere.",
    "remember": "Tranzacția nu are un cont, un card sau un comerciant pentru care să se poată salva o regulă.",
    "receipt": "Bonul explică deja o plată sau are deja datele lui. Nu a fost asociat cu tranzacția.",
}


def need(L: Ledger, company: str) -> Callable[[str], Decimal]:
    """What an item asks for before any explanation: an issued invoice its total less its credit notes, a received
    invoice its total, a payment row its amount. Read when asked, so inside the caller's write transaction."""
    credited: dict[str, Decimal] | None = None

    def of(item: str) -> Decimal:
        nonlocal credited
        kind, _, ident = item.partition(" ")
        if kind == "receipt":  # a bon asks for nothing: the debit it explains is its amount
            return Decimal(0)
        if kind == "issued":
            if credited is None:
                credited = issued._credited(L, company)
            r = L.issued(ident)
            return Decimal(r["total"]) - credited.get(ident, Decimal(0)) if r else Decimal(0)
        if kind == "efactura":
            r = L.efactura(ident)
            return (
                max(Decimal(0), Decimal(r["total"]) - efactura.credited(L, company).get(ident, Decimal(0)))
                if r
                else Decimal(0)
            )
        p = L.payment(ident)
        return Decimal(p["amount"]) if p else Decimal(0)

    return of


def _open(L: Ledger, company: str, item: str, t: statements.Tx, iban: str | None) -> tuple[str, str] | None:
    """(currency, issue date) of an item an explanation may still close, else None: an issued invoice open or marked
    by the user, a received invoice open or marked paid by the user, a payment row not paid yet (debited from the
    account its payment file named, when it named one)."""
    kind, _, ident = item.partition(" ")
    if kind == "issued" and t.direction == "credit":
        r = L.issued(ident)
        if (
            r
            and r["company"] == company
            and r["status"] in ("open", "marked")
            and not r["parse_error"]
            and r["doc_type"] == "invoice"
        ):
            return r["currency"] or "RON", r["issue_date"] or ""
    elif kind == "efactura" and t.direction == "debit":
        r = L.efactura(ident)
        marked = r and r["status"] == "paid" and not r["paid_tx"] and user_note(r["paid_note"])
        if (
            r
            and r["company"] == company
            and (r["status"] == "open" or marked)
            and not r["parse_error"]
            and (r["doc_type"] or "invoice") == "invoice"
        ):
            return r["currency"] or "RON", r["issue_date"] or ""
    elif kind == "payment" and t.direction == "debit":
        p = L.payment(ident)
        if (
            p
            and p["company"] == company
            and p["status"] in ("pending", "exported")
            and (not p["source_iban"] or p["source_iban"] == iban)
        ):
            return "RON", p["issue_date"] or ""
    return None


def _transaction(L: Ledger, company: str, key: str) -> tuple[statements.Tx, str | None]:
    """The transaction on record, and its account; refused when it isn't the company's, or a Decontare waiting for a
    person names it (step 3 explains it)."""
    row = L.db.execute("SELECT tx, iban FROM statement_txs WHERE tx_key=? AND company=?", (key, company)).fetchone()
    if not row or not row["tx"]:
        raise LinkRefused("unknown")
    t = statements.Tx.from_dict(json.loads(row["tx"]))
    if t.card_last4 and L.card_owner(company, t.card_last4) is None:
        raise LinkRefused("card_owner")
    if t.direction == "credit" and _awaits_settlement(L, company, key):
        raise LinkRefused("settlement")
    return t, row["iban"]


def _awaits_settlement(L: Ledger, company: str, key: str) -> bool:
    """A Decontare waiting for a person names this credit: step 3 explains it, nothing else may take it."""
    return (
        L.db.execute(
            "SELECT 1 FROM settlement_credits c JOIN settlement_reports r ON r.id=c.report_id WHERE "
            "r.company=? AND r.status='pending' AND json_extract(c.credit,'$.id')=?",
            (company, key),
        ).fetchone()
        is not None
    )


def _left(L: Ledger, company: str, t: statements.Tx, iban: str | None, items: list[str]) -> dict[str, tuple]:
    """(currency, issue date, what is left) of every item, each still open with something left to settle."""
    have, asks, facts = covered(L, company), need(L, company), {}
    for item in items:
        fact = _open(L, company, item, t, iban)
        left = asks(item) - have.get(item, Decimal(0)) if fact else Decimal(0)
        if fact is None or left <= 0:
            raise LinkRefused("item_changed")
        facts[item] = (*fact, left)
    return facts


def _named(t: statements.Tx, diff: Decimal, reason: str, note: str) -> tuple[str | None, str]:
    """The reason and the user's words for a difference: none when there is none, else one that fits it (FITS);
    `other` needs words."""
    if diff == 0:
        return None, ""
    if reason not in FITS[(t.direction, 1 if diff > 0 else -1)]:
        raise LinkRefused("reason")
    words = " ".join(note.split()) if reason == "other" else ""
    if reason == "other" and (not words or len(words) > NOTE_MAX):
        raise LinkRefused("note" if words else "reason")
    return reason, words


def _parts(ordered: list[str], facts: dict, money: Decimal, rest: bool) -> list[tuple[str, Decimal]]:
    """What each item settles, the oldest first: all that is left of each, or with a part still owed, the money there
    is, so the newest keeps the rest (every item picked gets some)."""
    if not rest:
        return [(i, facts[i][2]) for i in ordered]
    parts = []
    for i in ordered:
        part = min(money, facts[i][2])
        if part <= 0:
            raise LinkRefused("short")
        parts.append((i, part))
        money -= part
    return parts


def plan(
    L: Ledger,
    company: str,
    key: str,
    *,
    invoices: list[str],
    payment: str,
    reason: str,
    note: str,
    partners: dict[str, str | None],
    holder: str = "",
) -> Callable[[], dict]:
    """What explaining transaction `key` with these documents writes, decided when called (Ledger.link_tx calls it
    under the write lock): every item still open, of one partner (`partners`, api.item_partners) and one currency,
    the difference named by a reason that fits it, the parts each item settles. Refuses (LinkRefused) instead of
    guessing."""

    def build() -> dict:
        t, iban = _transaction(L, company, key)
        if not L.card_allows(company, t.card_last4):
            raise LinkRefused("card_firm")
        prefix = "issued " if t.direction == "credit" else "efactura "
        items = [prefix + i for i in invoices] if not payment else ["payment " + payment]
        if not items or (payment and invoices) or len(set(items)) != len(items):
            raise LinkRefused("nothing")
        facts = _left(L, company, t, iban, items)
        currencies = {c for c, _, _ in facts.values()}
        cur = currencies.pop() if len(currencies) == 1 else None
        money = (
            t.amount
            if t.currency == cur
            else abs(t.orig_amount)
            if cur and t.orig_currency == cur and t.orig_amount
            else None
        )
        if money is None:
            raise LinkRefused("currency")
        ids = {partners.get(i) for i in items} if not payment else {None}
        holding = (
            settlements.payout_partner(L, company, t, invoices, holder, partners)
            if holder and t.direction == "credit" and invoices
            else None
        )
        if holder and not holding:
            raise LinkRefused("partner")
        if holding:
            ids = {holding}
        if not payment and (len(ids) != 1 or None in ids):
            raise LinkRefused("partner")
        diff = money - sum(f[2] for f in facts.values())
        reason_, words = _named(t, diff, reason, note)
        if reason_ == "rest" and payment:
            raise LinkRefused("payment_rest")
        ordered = sorted(items, key=lambda i: (facts[i][1], i))
        return {
            "kind": "documents",
            "partner": ids.pop(),
            "reason": reason_,
            "difference": diff,
            "note": words or None,
            "items": _parts(ordered, facts, money, reason_ == "rest"),
        }

    return build


def _labelled(L: Ledger, company: str, key: str, label: str, note: str) -> Callable[[], dict]:
    """Fără document: a label from LABELS and the user's words for the accountant (needed for `other`)."""

    def build() -> dict:
        _transaction(L, company, key)
        words = " ".join(note.split())
        if label not in LABELS or (label == "other" and not words):
            raise LinkRefused("label")
        if len(words) > NOTE_MAX:
            raise LinkRefused("label_note")
        return {"kind": "label", "reason": label, "difference": Decimal(0), "note": words or None, "items": []}

    return build


def _advance(L: Ledger, company: str, key: str, who: str) -> Callable[[], dict]:
    """Avans: the owner's money in or out, with the administrator or associate it came from or went to."""

    def build() -> dict:
        _transaction(L, company, key)
        name = " ".join(who.split())
        if not name or len(name) > WHO_MAX:
            raise LinkRefused("who")
        return {"kind": "advance", "difference": Decimal(0), "note": name, "items": []}

    return build


def other_end(L: Ledger, company: str, key: str, t: statements.Tx, *, but: str = "") -> str | None:
    """The other end of a transfer between the firm's accounts, when exactly one is on record and free: the opposite
    direction, the same amount and currency, on another of its accounts (the one the transfer names, when it names one
    of the firm's), naming this account or none, not a credit a Decontare waits on, at most BOUNDARY_DAYS apart. A
    transfer to an account that isn't the firm's has none. `but`: a transaction whose reservation doesn't count (the
    one being explained, asking from the other end). Read under the write lock (Ledger.link_tx runs the build)."""
    own = companies.get(company, L=L).own_ibans
    to = companies.normalize_iban(t.counterparty_iban)
    if to and to not in own:
        return None
    lo, hi = (t.date - timedelta(days=BOUNDARY_DAYS)).isoformat(), (t.date + timedelta(days=BOUNDARY_DAYS)).isoformat()
    rows = L.db.execute(
        "SELECT tx_key, tx FROM statement_txs WHERE company=? AND direction=? AND amount=? AND tx_key!=? AND day BETWEEN ? "
        "AND ? AND iban IS NOT NULL AND iban!=? AND (?='' OR iban=?) "
        "AND tx_key NOT IN (SELECT tx_key FROM settled_tx WHERE tx_key!=?)",
        (
            company,
            "credit" if t.direction == "debit" else "debit",
            f"{t.amount:.2f}",
            key,
            lo,
            hi,
            companies.normalize_iban(t.iban),
            to or "",
            to or "",
            but,
        ),
    ).fetchall()
    mine = companies.normalize_iban(t.iban)
    if any(not r["tx"] for r in rows):  # a line fiscalm can't read may be the other end: none is sure
        return None

    def fits(r) -> bool:  # the same currency; a counterparty it names is this account; no Decontare waits on it
        other = json.loads(r["tx"])
        named = companies.normalize_iban(other.get("counterparty_iban") or "")
        return (
            other["currency"] == t.currency
            and named in ("", mine)
            and not (other["direction"] == "credit" and _awaits_settlement(L, company, r["tx_key"]))
        )

    found = [r["tx_key"] for r in rows if fits(r)]
    return found[0] if len(found) == 1 else None


def _transfer(L: Ledger, company: str, key: str) -> Callable[[], dict]:
    """Transfer între conturi, with its other end when it is seen (other_end): both are explained together."""

    def build() -> dict:
        t, _ = _transaction(L, company, key)
        other = other_end(L, company, key, t)
        return {"kind": "transfer", "difference": Decimal(0), "items": [("tx " + other, t.amount)] if other else []}

    return build


def link(
    L: Ledger,
    company: str,
    key: str,
    *,
    kind: str = "documents",
    invoices: list[str] = (),
    partners: dict[str, str | None] | None = None,
    payment: str = "",
    reason: str = "",
    note: str = "",
    who: str = "",
    was: str = "",
    remember: str = "",
    by: str | None = None,
    holder: str = "",
) -> dict:
    """Explain transaction `key` (one builder per kind, Ledger.link_tx), replacing the explanation `was` the user saw;
    then drop it from the ambiguous debits, tell a shop of an order it collected (commerce_webhooks.capture), bring
    the payment file up to date when a payment row was paid or freed, and keep it for the transaction's account, card or
    merchant when the user asked (`remember`: memory_keys), so its next transactions propose it."""
    if kind not in KINDS or not all(ID.fullmatch(i) for i in invoices) or reason not in ("", *REASONS, *LABELS):
        raise LinkRefused("nothing")
    build = {
        "documents": lambda: plan(
            L,
            company,
            key,
            invoices=list(invoices),
            payment=payment,
            reason=reason,
            note=note,
            partners=partners or {},
            holder=holder,
        ),
        "label": lambda: _labelled(L, company, key, reason, note),
        "advance": lambda: _advance(L, company, key, who),
        "transfer": lambda: _transfer(L, company, key),
    }[kind]()
    keep = _memory_key(L, company, key, kind, remember) if remember else None
    paid_before = L.db.execute(
        "SELECT 1 FROM tx_link_items WHERE tx_key=? AND item LIKE 'payment %'", (key,)
    ).fetchone()
    L.link_tx(company, key, was, build, need(L, company), by)
    L.drop_ambiguous(key)
    done = explanations(L, company)[key]
    for p in done["parts"]:
        if p["closed"] and L.issued(p["id"]):
            capture(L, message_id=p["id"])
    if payment or paid_before:  # a payment row paid leaves the payment file; one a replacement freed goes back to it
        pay.refresh_to_pay(L)
    if keep:
        L.remember(company, remember, keep, done, by)
    return done


def receipt_allowed(L: Ledger, company: str, key: str) -> None:
    """Refuse a bon for `key` before its file is stored: not this company's debit, or already explained."""
    t, _ = _transaction(L, company, key)
    if t.direction != "debit":
        raise LinkRefused("receipt")
    if L.db.execute("SELECT 1 FROM settled_tx WHERE tx_key=?", (key,)).fetchone():
        raise LinkRefused("used")


def receipt(L: Ledger, company: str, key: str, sha: str, by: str | None = None) -> dict:
    """The user's answer "this bon is this card payment", given by uploading it from the debit's sheet: the bon takes the
    debit's amount, day and card (Ledger._close_item), so nothing is read from the photo or guessed. One debit, one bon:
    a debit already explained, a credit, or a bon another debit explains or that already has its own data is refused,
    under the write lock."""

    def build() -> dict:
        t, _ = _transaction(L, company, key)
        item = "receipt " + sha
        if t.direction != "debit" or L.db.execute("SELECT 1 FROM tx_link_items WHERE item=?", (item,)).fetchone():
            raise LinkRefused("receipt")
        return {"kind": "receipt", "difference": Decimal(0), "items": [(item, t.orig_amount or t.amount)]}

    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise LinkRefused("receipt")
    try:
        L.link_tx(company, key, "", build, need(L, company), by)
    except LinkRefused as e:
        raise LinkRefused({"item_changed": "receipt", "changed": "used"}.get(e.code, e.code)) from None
    L.drop_ambiguous(key)
    return explanations(L, company)[key]


def _memory_key(L: Ledger, company: str, key: str, kind: str, how: str) -> str:
    """The key the user asked to remember the explanation by (efactura.line_keys); a card only remembers a label, a
    transfer nothing."""
    t, _ = _transaction(L, company, key)
    value = efactura.line_keys(t, companies.get(company, L=L).own_ibans).get(how)
    if not value or kind == "transfer" or (how == "card" and kind != "label"):
        raise LinkRefused("remember")
    return value


def shown_keys(keys: dict[str, str]) -> dict[str, str]:
    """A memory key as the app shows it: an account or a card by its last four digits, a merchant by its name."""
    return {k: ("••••" + v[-4:] if k in ("iban", "card") else v) for k, v in keys.items()}


def remembered(kept: list) -> list[dict]:
    """What the user asked to be remembered (Ledger.memories), newest first: {id, by: iban | card | name, shown, kind,
    partner, reason, note (an advance's: who), who_by (the user's display name), at}."""
    return [
        {
            "id": m["id"],
            "by": m["key_kind"],
            "shown": shown_keys({m["key_kind"]: m["key"]})[m["key_kind"]],
            "kind": m["kind"],
            "partner": m["partner"],
            "reason": m["reason"],
            "note": m["note"] or "",
            "who_by": m["by_name"],
            "at": m["at"][:19],
        }
        for m in kept
    ]


def proposals(company: str, txs: list[statements.Tx], kept: list, L: Ledger | None = None) -> list[dict | None]:
    """What the user asked to remember for each transaction's card, account or merchant (that order), as a proposal:
    {kind: remembered, memory, as: {kind, partner, reason, note}}. One of the AUTOMATIC kinds is applied on its own
    (apply_rules); the rest the user confirms on the sheet."""
    by_key = {(m["key_kind"], m["key"]): m for m in kept}
    own, out = companies.get(company, L=L).own_ibans, []
    for t in txs:
        keys = efactura.line_keys(t, own)
        m = next((by_key[(k, keys[k])] for k in ("card", "iban", "name") if (k, keys.get(k)) in by_key), None)
        out.append(
            {"kind": "remembered", "memory": m["id"], "as": {k: m[k] for k in ("kind", "partner", "reason", "note")}}
            if m and (m["key_kind"] != "card" or t.direction == "debit")
            else None
        )
    return out


def apply_rules(L: Ledger, company: str, rows: list[dict]) -> list[str]:
    """Explain on their own, after the matchers ran, the Tranzacții rows (transactions.view) left De explicat with
    nothing else proposed for them: by a saved Fără document or Avans rule, on any line of its card, account or
    merchant still to explain, older ones too; else a line the bank's own codes or words call its fee or interest, as
    Fără document; else a debit whose other end on another of the firm's accounts is free too (_own_transfer), as a
    Transfer între conturi with both ends. A row with any other proposal (an exact or named receipt, a settlement,
    invoices it fits) or waiting for its invoice is left to people. Written by Ledger.link_tx under the write lock,
    which checks again what a person may have decided since the rows were read and leaves a line naming an open
    invoice to a person (_untouched). Returns the keys explained."""
    free = {r["id"] for r in rows if not (r["explanation"] or r["proposal"] or r["awaiting"]) and r["can_link"]}
    done = []
    for r in rows:
        if r["explanation"] or not r["can_link"] or r["awaiting"]:
            continue
        kept = r["proposal"]["as"] if r["proposal"] and r["proposal"]["kind"] == "remembered" else None
        memory = r["proposal"]["memory"] if kept else None
        if kept and kept["kind"] in AUTOMATIC:
            words, rule = kept["note"] or "", "saved"
            if kept["kind"] == "label":
                build = _labelled(L, company, r["id"], kept["reason"] or "", words)
            else:
                build = _advance(L, company, r["id"], words)
        elif not r["proposal"] and r["bank_says"]:
            memory, rule, build = None, "bank", _labelled(L, company, r["id"], r["bank_says"], "")
        elif not r["proposal"] and r["direction"] == "debit" and _free_other_end(L, company, r["id"], free):
            memory, rule, build = None, "transfer", _own_transfer(L, company, r["id"], free)
        else:
            continue
        try:
            L.link_tx(
                company, r["id"], "", _untouched(L, company, r["id"], memory, build), need(L, company), None, rule
            )
        except LinkRefused:  # a person decided meanwhile, or refused as a person's would be: left to explain
            continue
        done.append(r["id"])
    return done


def _free_other_end(L: Ledger, company: str, key: str, free: set[str]) -> str | None:
    """The other end (other_end) of debit `key` when a rule may explain both as a Transfer între conturi: each is the
    other's only candidate, it is one of the rows left free, nobody decided otherwise for it since (_decided), both
    are on the firm's own accounts, neither is the bank's own fee or interest or has a saved rule now (proposals), the
    credit is no pending report's (settlements.claimed_credit), neither names an open invoice (names_open_invoice),
    and one of the two names the other's account. Two lines that only share an amount and a few days, naming no
    account, may be a payment and an unrelated collection: left to people. A line refused as a person's would be has
    none."""
    try:
        t, _ = _transaction(L, company, key)
        other = other_end(L, company, key, t)
        if other not in free or _decided(L, other):
            return None
        o, _ = _transaction(L, company, other)
        if names_open_invoice(L, company, o):
            return None
    except LinkRefused:
        return None
    if t.card_last4 or o.card_last4:  # a card pays a merchant; it never moves money between the firm's accounts
        return None
    bank = {statements.BANK_FEE, statements.INTEREST} & {t.category, o.category}
    named = companies.normalize_iban(t.counterparty_iban) or companies.normalize_iban(o.counterparty_iban)
    own = companies.get(company, L=L).own_ibans
    if bank or not named or not {companies.normalize_iban(t.iban), companies.normalize_iban(o.iban)} <= own:
        return None
    if settlements.claimed_credit(L, company, o) or any(proposals(company, [t, o], L.memories(company), L)):
        return None  # a pending report's credit, or a rule saved since the rows were read, is not a transfer's
    return other if other_end(L, company, other, o, but=key) == key else None


def _own_transfer(L: Ledger, company: str, key: str, free: set[str]) -> Callable[[], dict]:
    """A Transfer între conturi no person made, both ends together, read again under the write lock."""

    def build() -> dict:
        t, _ = _transaction(L, company, key)
        other = _free_other_end(L, company, key, free)
        if not other:
            raise LinkRefused("changed")
        return {"kind": "transfer", "difference": Decimal(0), "items": [("tx " + other, t.amount)]}

    return build


def plain(text: str) -> str:
    """Letters and digits only, lower case, without diacritics: how bank text and invoice numbers are compared."""
    return " ".join(re.findall(r"[0-9a-z]+", companies.fold(text).lower()))


def names_number(text: str, number: str) -> bool:
    """`text` (plain, padded with spaces) names invoice `number`: three characters or more with a digit, whole, as
    written or without its spaces."""
    p = plain(number)
    return len(p) >= 3 and any(c.isdigit() for c in p) and (f" {p} " in text or f" {p.replace(' ', '')} " in text)


def names_open_invoice(L: Ledger, company: str, t: statements.Tx) -> bool:
    """The line's bank details name one of the firm's invoices it may still pay (a debit) or collect (a credit), at any
    amount, by any reading a matcher or the sheet uses (names_number; the full identifier with or without its zeros,
    issued._ident_re; its digits, as the collections matcher reads them): what it covers is a person's to say, never a
    rule's. Read under the write lock."""
    words = t.description or t.title or ""
    text, norm = f" {plain(words)} ", efactura._norm(words)

    def names(number: str) -> bool:
        # a supplier's number is outside data: one too short names dates, one too long is not a number
        sure = 3 <= len(number) <= 40 and (re.search(r"[A-Za-z]", number) or efactura.number_of(number))
        ident, digits = (issued._ident_re(number) if sure else None), efactura.number_of(number)
        return (
            names_number(text, number)
            or bool(ident and ident.search(norm))
            or bool(digits and re.search(rf"(?<!\d){digits}(?!\d)", words))
        )

    sql = (
        "SELECT number FROM issued WHERE company=? AND status IN ('open','marked')"
        if t.direction == "credit"
        else "SELECT number FROM efactura WHERE company=? "
        "AND (status IN ('open','linked') OR status='paid' AND paid_tx IS NULL)"
    )
    return any(names(r["number"] or "") for r in L.db.execute(sql, (company,)))


def _decided(L: Ledger, key: str) -> bool:
    """A person decided for the line: took it back from an explanation, waits for its invoice, or must choose for it."""
    return bool(
        L.db.execute(
            "SELECT 1 FROM tx_rules_off WHERE tx_key=?1 UNION ALL SELECT 1 FROM tx_awaiting WHERE tx_key=?1 "
            "UNION ALL SELECT 1 FROM ambiguous_tx WHERE tx_key=?1",
            (key,),
        ).fetchone()
    )


def _untouched(L: Ledger, company: str, key: str, memory: str | None, build: Callable[[], dict]) -> Callable[[], dict]:
    """A rule's explanation, under the write lock, only while nobody decided otherwise since the rows were read: the
    line not decided by a person (_decided), not a card debit a receipt could explain (recon.match_receipts, by
    amount or by name), not a line naming an open invoice (names_open_invoice), not a credit a pending settlement
    report claims, and the saved rule still the one that wins
    for the line (proposals: a replaced rule has a new id, a card's rule comes before an account's or a merchant's)."""

    def build_once() -> dict:
        t, _ = _transaction(L, company, key)
        now = proposals(company, [t], L.memories(company), L)[0] if memory else None
        gone = memory and (now or {}).get("memory") != memory
        receipt = t.direction == "debit" and t.card_last4 and recon.match_receipts([t], recon._receipts(L, company))
        if (
            _decided(L, key)
            or names_open_invoice(L, company, t)
            or gone
            or receipt
            or (t.direction == "credit" and settlements.claimed_credit(L, company, t))
        ):
            raise LinkRefused("changed")
        return build()

    return build_once


def await_invoice(L: Ledger, company: str, key: str, note: str, by: str | None = None) -> bool:
    """Aștept factura (Ledger.await_invoice): the debit stays to explain, with what the user expects if they said."""
    note = " ".join(note.split())
    if len(note) > NOTE_MAX:
        raise LinkRefused("label_note")
    return L.await_invoice(company, key, note, by)


def unlink(L: Ledger, company: str, key: str, was: str) -> list[str]:
    """Undo the explanation `was` (Ledger.unlink_tx); a payment row it paid goes back to the payment file."""
    items = L.unlink_tx(company, key, was, need(L, company))
    if any(i.startswith("payment ") for i in items):
        pay.refresh_to_pay(L)
    return items


def named_payment(p) -> dict:
    """A payment row as Tranzacții names it: who was paid and for what (never the payee's account)."""
    return {
        "kind": p["kind"],
        "payee": p["payee_name"] or "",
        "ref": (p["doc_ref"] or "").strip(),
        "details": (p["details"] or "").strip(),
    }


def explanations(L: Ledger, company: str) -> dict[str, dict]:
    """The user's explanations by the transaction they are recorded on: {id, tx, kind, other (a transfer's other end),
    partner, parts: [{id, amount, closed}] (the invoices, each with the part it settles), payments (the payment rows,
    named_payment), reason (a difference's, or a label's), difference, currency (what the parts and the difference are
    counted in: the documents', a card's original currency when they are in it), note (an advance's: who), by (the user's display name), at}."""
    links, items = L.link_rows(company)
    by_tx: dict[str, list] = defaultdict(list)
    for it in items:
        by_tx[it["tx_key"]].append(it)
    out = {}
    for ln in links:
        parts, payments, currency, other = [], [], "RON", None
        for it in by_tx[ln["tx_key"]]:
            kind, _, ident = it["item"].partition(" ")
            if kind in ("tx", "receipt"):  # a transfer's other end, or the bon (links.receipt)
                other = ident
                continue
            if kind == "payment":
                payments += [named_payment(p)] if (p := L.payment(ident)) else []
                continue
            r = L.issued(ident) if kind == "issued" else L.efactura(ident)
            currency = (r["currency"] if r else None) or "RON"
            parts.append({"id": ident, "amount": f"{Decimal(it['amount']):.2f}", "closed": bool(it["closed"])})
        out[ln["tx_key"]] = {
            "id": ln["id"],
            "tx": ln["tx_key"],
            "kind": ln["kind"],
            "other": other,
            "partner": ln["partner"],
            "parts": parts,
            "payments": payments,
            "reason": ln["reason"],
            "difference": ln["difference"],
            "currency": currency,
            "note": ln["note"] or "",
            "rule": ln["rule"],
            "by": ln["by_name"],
            "at": ln["at"][:19],
        }
    return out


def _core(name: str | None) -> str:
    return " ".join(firm_core(name))


def _pointers(L: Ledger, company: str, partners: dict[str, str | None]) -> dict[str, dict[str, set]]:
    """What points to a partner on a statement line: for money in, the account an earlier collection came from
    (`iban`), the client's CUI (`cui`) and exact name (`name`); for money out, an account its invoices name (`out`)
    and the supplier's exact name (`payee`)."""
    maps: dict[str, dict[str, set]] = {k: defaultdict(set) for k in ("iban", "cui", "name", "out", "payee")}
    for r in L.issued_rows(company=company):
        if pid := partners.get("issued " + r["message_id"]):
            for k, v in (
                ("iban", r["collected_from_iban"]),
                ("cui", r["client_cui"]),
                ("name", _core(r["client_name"])),
            ):
                if v and len(v) >= 6 and (k == "name" or firm_id(tax_token(r["client_cui"]))):
                    maps[k][v].add(pid)
    for r in L.efactura_rows(company=company):
        if pid := partners.get("efactura " + r["message_id"]):
            for iban in {*json.loads(r["ibans"] or "[]"), r["supplier_iban"] or ""} - {""}:
                maps["out"][iban].add(pid)
            if len(name := _core(r["supplier_name"])) >= 6:
                maps["payee"][name].add(pid)
    return maps


def suggest(L: Ledger, company: str, txs: list[statements.Tx], partners: dict[str, str | None]) -> list[str | None]:
    """The partner each transaction's bank text points to, when exactly one does (the sheet's default for De la cine,
    never an explanation). A credit, as issued.credit_from_client reads it: the account an earlier collection of the
    client came from, else its CUI in the text, else its exact name. A debit: an account the supplier's invoices name,
    else its exact name. A transfer between the firm's own accounts points to nobody."""
    own, maps, out = companies.get(company, L=L).own_ibans, _pointers(L, company, partners), []
    for t in txs:
        found: set = set()
        if t.counterparty_iban in own:
            pass
        elif t.direction == "credit":
            cuis = re.findall(r"(?<!\d)\d{6,}(?!\d)", f"{t.counterparty} {t.description}")
            found = (
                maps["iban"].get(t.counterparty_iban, set())
                or set().union(*(maps["cui"].get(c, set()) for c in cuis))
                or maps["name"].get(_core(t.counterparty), set())
            )
        else:
            found = maps["out"].get(t.counterparty_iban, set()) or maps["payee"].get(_core(t.counterparty), set())
        out.append(next(iter(found)) if len(found) == 1 else None)
    return out
