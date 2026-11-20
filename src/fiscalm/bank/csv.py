"""A bank's CSV export as a fiscalm Statement, read with the columns the person picked once for that account.

Every bank lays its CSV out differently, so fiscalm doesn't guess: the first CSV of an account is shown as a preview,
the person says which column is the date, the amount (or debit and credit), the details, and optionally the other
party, its IBAN and the reference; that mapping is kept for the account (ledger `csv_formats`) and used for every
later CSV of it. A CSV names no account, so it is uploaded on one: every row is that account's. It carries no
balances, so its totals can't be checked: the stored document says so (an issue), as it does for a statement whose
totals don't add up. It may span any days, across months: it proves only the days it holds and is never a month's
statement (stored under its first month; statement_intake checks every CSV of the company, whatever its month).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime
from decimal import Decimal

from fiscalm import companies
from fiscalm.bank import catalogue as banks
from fiscalm.bank.statements import Account, Statement, StatementError, Tx
from fiscalm.ledger import Ledger, now

MAX_CSV = 5 * 1024 * 1024
MAX_ROWS = 20000
DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y")
FIELDS = ("date", "amount", "debit", "credit", "description", "counterparty", "counterparty_iban", "reference")
NO_TOTALS = "CSV statement: no balances to check its totals against"


def account_id(company: str, iban: str) -> str:
    """An opaque name for one of a company's accounts, so the app can point at it without its IBAN."""
    return hashlib.sha256(f"{company}|{iban}".encode()).hexdigest()[:12]


def account_by_id(company: str, ident: str):
    """The company's current account `ident` names, or None."""
    return next((a for a in companies.get(company).current_accounts if account_id(company, a.iban) == ident), None)


def _text(data: bytes) -> str:
    if len(data) > MAX_CSV:
        raise StatementError("CSV file too large")
    for enc in ("utf-8-sig", "cp1250"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise StatementError("CSV encoding not understood")


def _rows(data: bytes) -> list[list[str]]:
    text = _text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if len(rows) < 2 or len(rows) > MAX_ROWS:
        raise StatementError("CSV has no rows or too many")
    return rows


def preview(data: bytes) -> dict:
    """The header and first rows, for the person to pick columns from, and what the whole file settles by itself."""
    rows = _rows(data)
    columns = [c.strip()[:80] for c in rows[0]]
    return {"columns": columns, "rows": [[c.strip()[:80] for c in r] for r in rows[1:6]], "detected": _detect(rows)}


def _is_date(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def _detect(rows: list[list[str]]) -> dict:
    """Per column, the one date format or the one decimal separator that reads every value in it. A separator counts
    only with a decimal part as proof (1.250,50), never thousands alone (-1.234). A column two fit (every day up to the
    12th, only whole amounts) settles nothing: the person picks, and the import checks every row."""
    dates, decimals = {}, {}
    for i, name in enumerate(c.strip()[:80] for c in rows[0]):
        values = [r[i].strip() for r in rows[1:] if i < len(r) and r[i].strip()]
        formats = [f for f in DATE_FORMATS if values and all(_is_date(v, f) for v in values)]
        separators = [
            d
            for d, grouped in _GROUPED.items()
            if values
            and all(re.fullmatch(grouped, _plain(v)) for v in values)
            and any(re.search(re.escape(d) + r"\d{1,2}$", _plain(v)) for v in values)
        ]
        if len(formats) == 1:
            dates[name] = formats[0]
        elif len(separators) == 1:
            decimals[name] = separators[0]
    return {"dates": dates, "decimals": decimals}


def check_mapping(m: dict, columns: list[str] | None = None) -> dict:
    """A mapping's shape (and, with `columns`, that its columns exist). Refuses what would read money wrongly."""
    if not isinstance(m, dict) or set(m) - set(FIELDS) - {"date_format", "decimal"}:
        raise StatementError("CSV mapping has unknown fields")
    if not all(isinstance(v, str) for v in m.values()):
        raise StatementError("CSV mapping values must be text")
    if not m.get("date") or not m.get("description"):
        raise StatementError("CSV mapping needs the date and the details columns")
    if bool(m.get("amount")) == bool(m.get("debit") or m.get("credit")) or (
        not m.get("amount") and not (m.get("debit") and m.get("credit"))
    ):
        raise StatementError("CSV mapping needs one amount column, or a debit and a credit column")
    if m.get("date_format") not in DATE_FORMATS or m.get("decimal") not in (",", "."):
        raise StatementError("CSV mapping needs a date format and a decimal separator")
    if columns is not None:
        missing = [v for k, v in m.items() if k in FIELDS and v and v not in columns]
        if missing:
            raise StatementError("CSV columns not found: " + ", ".join(missing[:3]))
    return {k: v for k, v in m.items() if v}


_GROUPED = {
    ",": r"[-+]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?",
    ".": r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?",
}


def _plain(raw: str) -> str:
    return re.sub(r"[\s\u00a0]", "", raw or "").replace("−", "-")


def _money(raw: str, decimal: str) -> Decimal | None:
    """An amount as the export writes it: thousands grouped only by threes, at most two decimals. Anything else (a
    comma where the mapping expects a point, a third decimal) is refused, never read as another number."""
    s = _plain(raw)
    if not s:
        return None
    if not re.fullmatch(_GROUPED[decimal], s):
        raise StatementError("CSV amount not understood")
    s = s.replace(".", "").replace(",", ".") if decimal == "," else s.replace(",", "")
    return Decimal(s)


def parse_csv(data: bytes, company: str, iban: str, mapping: dict) -> Statement:
    co = companies.get(company)
    account = co.account(iban)
    if not account or account.retired:
        raise StatementError("CSV account is not a current account of the company")
    rows = _rows(data)
    header = [c.strip() for c in rows[0]]
    m = check_mapping(mapping, header)
    col = {k: header.index(v) for k, v in m.items() if k in FIELDS}

    def cell(r: list[str], k: str) -> str:
        i = col.get(k)
        return r[i].strip() if i is not None and i < len(r) else ""

    txs: list[Tx] = []
    for r in rows[1:]:
        try:
            day = datetime.strptime(cell(r, "date"), m["date_format"]).date()
        except ValueError:
            raise StatementError("CSV date not understood") from None
        if "amount" in col:
            value = _money(cell(r, "amount"), m["decimal"])
            if value is None:
                raise StatementError("CSV row without an amount")
            direction, value = ("credit", value) if value > 0 else ("debit", -value)
        else:
            debit, credit = _money(cell(r, "debit"), m["decimal"]), _money(cell(r, "credit"), m["decimal"])
            if bool(debit) == bool(credit):
                raise StatementError("CSV row needs exactly one of debit and credit")
            direction, value = ("debit", abs(debit)) if debit is not None and debit else ("credit", abs(credit or 0))
        if value == 0:
            raise StatementError("CSV zero amount refused")
        other = re.sub(r"\s+", "", cell(r, "counterparty_iban")).upper()
        txs.append(
            Tx(
                date=day,
                title=cell(r, "description")[:80],
                description=cell(r, "description"),
                direction=direction,
                amount=value.quantize(Decimal("0.01")),
                currency=account.currency,
                iban=account.iban,
                ref=cell(r, "reference"),
                counterparty=cell(r, "counterparty"),
                counterparty_iban=other if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", other) else "",
            )
        )
    from fiscalm.bank.statements import apply_wording, tx_key

    bank = banks.for_iban(account.iban)
    for t in txs:  # a CSV export carries no codes: its bank's own words say which lines are fees or interest
        if bank:
            apply_wording(t, bank)
    if len({tx_key(t) for t in txs}) != len(txs):  # two rows read as one transaction: map the reference column
        raise StatementError("CSV has identical rows; add the bank's reference column so each is one transaction")
    acct = Account(account.iban, account.currency, txs)  # no balances: totals can't be checked (NO_TOTALS)
    # Partial: it proves only the days it holds, never a whole month (no balances to show nothing is missing).
    return Statement(company, min(t.date for t in txs), max(t.date for t in txs), [acct], partial=True)


def saved_mapping(L: Ledger, company: str, iban: str) -> dict | None:
    row = L.db.execute("SELECT mapping FROM csv_formats WHERE company=? AND iban=?", (company, iban)).fetchone()
    return json.loads(row["mapping"]) if row else None


def save_mapping(L: Ledger, company: str, iban: str, mapping: dict) -> dict:
    m = check_mapping(mapping)
    with L.tx() as db:
        db.execute(
            "INSERT INTO csv_formats(company,iban,mapping,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(company, iban) DO UPDATE SET mapping=excluded.mapping, updated_at=excluded.updated_at",
            (company, iban, json.dumps(m), now()),
        )
    return m
