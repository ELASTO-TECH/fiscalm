"""MT940 (SWIFT customer statement) as a fiscalm Statement, with the rules CAMT has: the accounts are the company's,
balances chain and add up, the statements of an account cover one full calendar month, only booked lines, and anything
that would need judgement (reversals, an account named twice, a currency change) is refused instead of guessed.

A file may hold several statements (one per day or one per account); each account's are joined in order and must
chain: every opening balance is the previous closing one."""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fiscalm import companies
from fiscalm.bank.statements import BANK_FEE, INTEREST, Account, PartialMonth, Statement, StatementError, Tx

MAX_MT940 = 10 * 1024 * 1024
_TAG = re.compile(r"^:(\d{2}[A-Z]?):", re.M)
_BAL = re.compile(r"^([CD])(\d{6})([A-Z]{3})(\d{1,15},\d{0,2})$")
_LINE = re.compile(r"^(\d{6})(\d{4})?(R?[CD])([A-Z])?(\d{1,15},\d{0,2})([A-Z]\w{3})(.*)$", re.S)


# SWIFT transaction type codes (the last three letters of :61:'s type, e.g. NCHG): charges, commission and brokerage
# fees are the bank's fee, INT is interest.
_FEE_TYPES = ("CHG", "COM", "BRF")
_INTEREST_TYPES = ("INT",)


def _category(kind: str) -> str:
    """What a :61: line's transaction type says it is: a fee, interest, or "" when it says neither."""
    return BANK_FEE if kind[1:] in _FEE_TYPES else INTEREST if kind[1:] in _INTEREST_TYPES else ""


def looks_like(data: bytes) -> bool:
    return bool(re.search(rb"(^|\n):20:", data[:4096])) and b":25:" in data[:8192]


def _amount(raw: str) -> Decimal:
    whole, _, frac = raw.partition(",")
    try:
        value = Decimal(f"{int(whole)}.{(frac or '0'):0<2}")
    except (InvalidOperation, ValueError):
        raise StatementError("MT940 amount invalid") from None
    return value


def _day(yymmdd: str) -> date:
    try:
        return date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        raise StatementError("MT940 date invalid") from None


def _fields(block: str) -> list[tuple[str, str]]:
    marks = list(_TAG.finditer(block))
    out = []
    for i, m in enumerate(marks):
        v = block[m.end() : (marks[i + 1].start() if i + 1 < len(marks) else len(block))]
        out.append((m[1], re.sub(r"(?:\r?\n-[^\n]*)+\s*$", "", v).strip()))  # a block's end marker "-" isn't data
    return out


def _details(text: str) -> tuple[str, str, str, str]:
    """(description, counterparty name, counterparty IBAN, booking text ?00) from a :86: field, structured
    (?00..?33) or free. The booking text ('POS refund') stays out of the description, which keys the line."""
    flat = text.replace("\r", "").replace("\n", "")
    if re.match(r"^\d{3}\?", flat) or flat.startswith("?"):
        parts = dict((m[1], m[2]) for m in re.finditer(r"\?(\d{2})([^?]*)", flat))
        desc = " ".join(parts[k] for k in sorted(parts) if "20" <= k <= "29" or "60" <= k <= "63").strip()
        name = (parts.get("32", "") + parts.get("33", "")).strip()
        iban = re.sub(r"\s+", "", parts.get("31", "")).upper()
        iban = iban if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", iban) else ""
        return desc, name, iban, parts.get("00", "").strip()
    return re.sub(r"\s+", " ", text).strip(), "", "", ""


def parse_mt940(data: bytes, company: str = "") -> Statement:
    if len(data) > MAX_MT940:
        raise StatementError("MT940 file too large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1250", errors="strict")
    blocks = [b for b in re.split(r"(?m)^(?=:20:)", text) if b.lstrip().startswith(":20:")]
    if not blocks:
        raise StatementError("MT940 contains no statement")
    owners = companies.all()
    per_account: dict[str, list[dict]] = {}
    for block in blocks:
        company, iban, part = _statement_block(block, company, owners)
        per_account.setdefault(iban, []).append(part)
    accounts, starts, ends = [], [], []
    for iban, parts in per_account.items():
        acct, start, end = _monthly_account(iban, parts)
        accounts.append(acct)
        starts.append(start)
        ends.append(end)
    if len(set(starts)) != 1:
        raise StatementError("MT940 accounts cover different months")
    return Statement(company, starts[0], ends[0], accounts)


def _statement_block(block: str, company: str, owners: dict[str, companies.Company]) -> tuple[str, str, dict]:
    f = _fields(block)
    company, iban = _account_fields(f, company, owners)
    open_tag = next(t for t, _ in f if t in ("60F", "60M"))
    close_tag = next(t for t, _ in f if t in ("62F", "62M"))
    opening = _BAL.match(next(v for t, v in f if t == open_tag).replace("\n", ""))
    closing = _BAL.match(next(v for t, v in f if t == close_tag).replace("\n", ""))
    if not opening or not closing or opening[3] != closing[3]:
        raise StatementError("MT940 balances missing or in different currencies")
    currency = opening[3]
    txs = _transactions(f, iban, currency)
    sign = lambda b: _amount(b[4]) * (1 if b[1] == "C" else -1)  # noqa: E731
    moved = sum((t.amount if t.direction == "credit" else -t.amount for t in txs), Decimal(0))
    if sign(opening) + moved != sign(closing):  # each statement on its own: errors never cancel across days
        raise StatementError("MT940 lines do not add up to its balances")
    return (
        company,
        iban,
        dict(
            currency=currency,
            open=sign(opening),
            close=sign(closing),
            final_open=open_tag == "60F",
            final_close=close_tag == "62F",
            open_day=_day(opening[2]),
            close_day=_day(closing[2]),
            txs=txs,
        ),
    )


def _transactions(f: list[tuple[str, str]], iban: str, currency: str) -> list[Tx]:
    txs: list[Tx] = []
    for i, (t, v) in enumerate(f):
        if t != "61":
            continue
        m = _LINE.match(v.split("\n")[0])
        if not m:
            raise StatementError("MT940 statement line not understood")
        if m[3].startswith("R"):
            raise StatementError("MT940 reversal lines require manual review")
        value_day, booking = _transaction_days(m)
        amount = _amount(m[5])
        if amount == 0:
            raise StatementError("MT940 zero amount refused")
        refs = (m[7] or "").split("\n")[0].strip()
        info = f[i + 1][1] if i + 1 < len(f) and f[i + 1][0] == "86" else ""
        desc, name, other, booked = _details(info)
        tx = Tx(
            date=booking,
            title=(booked or desc)[
                :80
            ],  # the :61: type (NTRF, NCHG) is a SWIFT booking code, never a name; it sets `category`
            description=desc,
            direction="credit" if m[3] == "C" else "debit",
            amount=amount,
            currency=currency,
            iban=iban,
            ref=refs,
            counterparty=name,
            counterparty_iban=other,
            value_date=value_day.isoformat(),
            category=_category(m[6]),
        )
        txs.append(tx)
    return txs


def _transaction_days(m: re.Match[str]) -> tuple[date, date]:
    value_day = _day(m[1])
    booking = value_day
    if m[2]:
        booking = date(value_day.year, int(m[2][:2]), int(m[2][2:]))
        if booking - value_day > timedelta(days=180):  # entry date in the previous year
            booking = booking.replace(year=booking.year - 1)
        elif value_day - booking > timedelta(days=180):
            booking = booking.replace(year=booking.year + 1)
    return value_day, booking


def _monthly_account(iban: str, parts: list[dict]) -> tuple[Account, date, date]:
    parts.sort(key=lambda p: (p["close_day"], p["open_day"]))
    if len({p["currency"] for p in parts}) != 1:
        raise StatementError("MT940 account changes currency")
    if not parts[0]["final_open"] or not parts[-1]["final_close"]:
        raise StatementError("MT940 needs the month's final opening (60F) and closing (62F) balances")
    for prev, nxt in zip(parts, parts[1:], strict=False):
        # every day covered: the next statement starts the day the previous one closed, or the day after
        if prev["close"] != nxt["open"] or nxt["open_day"] not in (
            prev["close_day"],
            prev["close_day"] + timedelta(days=1),
        ):
            raise StatementError("MT940 statements of an account do not chain")
    end = parts[-1]["close_day"]
    start = end.replace(day=1)
    if end.day != calendar.monthrange(end.year, end.month)[1] or parts[0]["open_day"] not in (
        start,
        start - timedelta(days=1),
    ):
        raise PartialMonth("MT940", start)
    txs = [t for p in parts for t in p["txs"]]
    if any(not start <= t.date <= end for t in txs):
        raise StatementError("MT940 booking date outside the month")
    debit = sum((t.amount for t in txs if t.direction == "debit"), Decimal(0))
    acct = Account(
        iban,
        parts[0]["currency"],
        txs,
        total_debit=debit,
        total_credit=parts[-1]["close"] - parts[0]["open"] + debit,
    )
    if not acct.balanced:  # as CAMT: lines that don't add up to the balances settle nothing, so none is taken
        raise StatementError("MT940 lines do not add up to its balances")
    acct.closing, acct.closing_day = parts[-1]["close"], end
    return acct, start, end


def _account_fields(f: list[tuple[str, str]], company: str, owners: dict[str, companies.Company]) -> tuple[str, str]:
    tags = [t for t, _ in f]
    for one in ("25", "60F", "62F"):
        if (
            tags.count(one) + (tags.count("60M") if one == "60F" else 0) + (tags.count("62M") if one == "62F" else 0)
            != 1
        ):
            raise StatementError(f"MT940 statement needs one :{one}: field")
    acct_raw = next(v for t, v in f if t == "25")
    iban = re.sub(r"\s+", "", acct_raw.split("/")[-1]).upper()
    keys = [k for k, co in owners.items() if iban and iban in co.own_ibans]
    if len(keys) != 1 or (company and company != keys[0]):
        raise StatementError("MT940 account does not belong to the selected company")
    company = keys[0]
    return company, iban
