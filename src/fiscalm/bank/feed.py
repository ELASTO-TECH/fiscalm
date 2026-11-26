"""The daily read of a company's connected bank (open banking): booked transactions only, through the same matchers
as a statement, never a second time the same payment (twins with statements are reserved: Ledger.record_statement).

Read only. A connection is read at most once a day (banks allow about four reads a day without the person present);
a consent that ended, was revoked or hit the bank's limit is recorded on the connection and said in fiscalm, and
nothing is matched from it. See docs/self-hosting.md.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fiscalm import companies
from fiscalm.bank import catalogue as banks, connections as bank_connect, coverage, enablebanking as eb
from fiscalm.bank.statements import Account, Statement, Tx, apply_wording, iso_category
from fiscalm.ledger import Ledger

OVERLAP_DAYS = coverage.LATE_BOOKING_DAYS  # a booking can land a few days late: read them again (same keys)
FIRST_DAYS = 89  # what most banks give the first time (BT gives 120)
NOT_BOOKED = ("PDNG", "SCHD", "HOLD", "INFO", "CNCL", "RJCT")  # what the bank says isn't booked; anything else may be


def _iban(party: dict | None) -> str:
    raw = ((party or {}).get("iban") or "") if isinstance(party, dict) else ""
    s = re.sub(r"\s+", "", raw).upper()
    return s if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s) else ""


def to_tx(raw: dict, iban: str, currency: str) -> Tx | None:
    """One booked transaction as fiscalm's, or None when it isn't one the engine should read (pending, other currency,
    malformed). Its reference is the bank's own (`entry_reference`), so reading it again gives the same key."""
    if not isinstance(raw, dict) or raw.get("status") != "BOOK":
        return None
    amt = raw.get("transaction_amount") or {}
    if not isinstance(amt, dict):
        return None
    try:
        exact = Decimal(str(amt.get("amount")))
        amount = exact.quantize(Decimal("0.01"))
        if not exact.is_finite() or amount != exact:
            return None
        day = date.fromisoformat(str(raw.get("booking_date")))
    except (InvalidOperation, ValueError):
        return None
    ref = str(raw.get("entry_reference") or "")
    if amt.get("currency") != currency or amount <= 0 or not ref or len(ref) > 200:
        return None
    direction = {"DBIT": "debit", "CRDT": "credit"}.get(raw.get("credit_debit_indicator"))
    if not direction:
        return None
    other = "creditor" if direction == "debit" else "debtor"
    details = raw.get("remittance_information") or []
    text = " ".join(str(x) for x in details if isinstance(x, str))[:500] if isinstance(details, list) else ""
    party = raw.get(other) if isinstance(raw.get(other), dict) else {}
    tx = Tx(
        date=day,
        title=text[:80],
        description=text,
        direction=direction,
        amount=amount,
        currency=currency,
        iban=iban,
        ref="eb:" + ref,
        bank_reference=ref,
        counterparty=str(party.get("name") or "")[:140],
        counterparty_iban=_iban(raw.get(other + "_account")),
        value_date=str(raw.get("value_date") or "")[:10],
        category=iso_category(*_codes(raw)),
        country=_country(party),
    )
    bank = banks.for_iban(iban)
    if bank and not tx.category:  # no code from the bank: its own words in the text, as in its PDF and CSV
        apply_wording(tx, bank)
    return tx


def _codes(raw: dict) -> list[str]:
    """The ISO 20022 family and sub-family the bank gave the transaction (`bank_transaction_code`), if any."""
    code = raw.get("bank_transaction_code")
    return [str(code.get(k) or "") for k in ("code", "sub_code")] if isinstance(code, dict) else []


def _country(party: dict) -> str:
    """The other party's country from its postal address (a card merchant's, when the bank gives it), else ""."""
    address = party.get("postal_address")
    c = str(address.get("country") or "").upper() if isinstance(address, dict) else ""
    return c if re.fullmatch(r"[A-Z]{2}", c) else ""


def read(L: Ledger, connection: dict, today: date, api: eb.Client | None = None) -> tuple[Statement, dict[str, date]]:
    """What the bank booked on the connection's accounts since the last read (with a few days' overlap), and for each
    account with a booked line fiscalm could not read (`to_tx` refused it), the latest such line's day: the feed
    proves nothing up to it."""
    row = L.db.execute("SELECT * FROM bank_connections WHERE id=?", (connection["id"],)).fetchone()
    if not row:
        raise eb.BankError("Citirea directă a fost oprită.", "disconnected")
    co = companies.get(row["company"], L=L)
    last = date.fromisoformat(row["last_read"][:10]) if row["last_read"] else None
    start = (last - timedelta(days=OVERLAP_DAYS)) if last else today - timedelta(days=FIRST_DAYS)
    api = api or bank_connect.client(L)
    accounts, unread = [], {}
    for a in json.loads(row["accounts"]):
        own = co.account(a["iban"])
        if not own or own.retired:  # an account retired since it was connected is no longer read
            continue
        raws = api.transactions(a["uid"], start.isoformat(), today.isoformat())
        read_ = [(r, to_tx(r, own.iban, own.currency)) for r in raws]
        accounts.append(Account(own.iban, own.currency, [t for _, t in read_ if t]))
        lost = [_booked_on(r, today) for r, t in read_ if not t and _booked_on(r, today)]
        if lost:
            unread[companies.normalize_iban(own.iban)] = max(lost)
    return Statement(row["company"], start, today, accounts, source="ob"), unread


def _booked_on(raw: object, today: date) -> date | None:
    """The day of a line the bank may have booked (any status but one in NOT_BOOKED, an empty or unknown one too):
    its booking date, or today when it has none."""
    if not isinstance(raw, dict):
        return today
    if raw.get("status") in NOT_BOOKED:
        return None
    try:
        return min(date.fromisoformat(str(raw.get("booking_date"))), today)
    except ValueError:
        return today


def run_all(L: Ledger, today: date, api: eb.Client | None = None) -> list[dict]:
    """Read every connection still valid and not read today, and match what it brought. One connection's refusal
    (expired, revoked, the bank's daily limit) is kept on it and the others go on."""
    from fiscalm import recon
    from fiscalm.efactura import inbox as efactura
    from fiscalm.invoicing import issued

    out = []
    for c in bank_connect.connections(L):
        if c["valid_until"] and c["valid_until"] < today.isoformat():
            _note(L, c["id"], "EXPIRED_SESSION")
            out.append({"connection": c["id"], "status": "expired"})
            continue
        with L.tx() as db:  # claim today's attempt first: two syncs at once never call the bank twice
            if not db.execute(
                "UPDATE bank_connections SET attempted_on=? WHERE id=? AND (attempted_on IS NULL OR attempted_on<?)",
                (today.isoformat(), c["id"], today.isoformat()),
            ).rowcount:
                continue
        try:
            st, unread = read(L, c, today, api)  # from the last read that worked, so a failed day loses nothing
        except eb.BankError as e:
            _note(L, c["id"], e.code or "error")
            out.append({"connection": c["id"], "status": "error", "code": e.code})
            continue
        paid = recon.auto_mark_paid(L, st)  # records the lines first (twins reserved), then matches
        ef = efactura.match_statement(L, st)
        col = issued.match_statement(L, st)
        _read(L, c["id"], today, st, unread)
        out.append(
            {
                "connection": c["id"],
                "status": "read",
                "transactions": len(st.txs),
                "payments_marked_paid": len(paid),
                "efactura_marked_paid": len(ef),
                "issued_collected": len(col),
            }
        )
    return out


def _read(L: Ledger, cid: str, today: date, st: Statement, unread: dict[str, date]) -> None:
    """Record a read that worked, after its lines were matched. It keeps, for each account read, the first day the
    feed holds of it (`covered`, bank.coverage): an account read before keeps its day, a new one starts at this read's
    first day, one no longer read (retired, left out on reconnecting) drops out, and one with a booked line fiscalm
    could not read starts again the day after it."""
    with L.tx() as db:
        row = db.execute("SELECT covered FROM bank_connections WHERE id=?", (cid,)).fetchone()
        was = json.loads(row["covered"]) if row else {}
        ibans = {companies.normalize_iban(a.iban) for a in st.accounts}
        covered = {i: was.get(i, st.period_start.isoformat()) for i in sorted(ibans)}
        for i, day in unread.items():
            covered[i] = max(covered[i], (day + timedelta(days=1)).isoformat())
        db.execute(
            "UPDATE bank_connections SET last_read=?, last_error=NULL, covered=? WHERE id=?",
            (today.isoformat(), json.dumps(covered), cid),
        )


def _note(L: Ledger, cid: str, error: str) -> None:
    with L.tx() as db:
        db.execute("UPDATE bank_connections SET last_error=? WHERE id=?", (error, cid))
