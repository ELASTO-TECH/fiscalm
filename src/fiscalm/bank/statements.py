"""BT monthly 'allmerge' statements: decrypt, write unprotected copy, parse transactions.

The parser self-checks every account section: the sum of parsed debits/credits
must equal the bank's own totals, and opening plus movements must equal closing
within one statement period; otherwise the section cannot settle anything.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pikepdf

from fiscalm import companies, secrets
from fiscalm.bank import catalogue as banks
from fiscalm.pdftext import parse_amount, parse_dmy, pdf_text

MONTHS_EN = {
    m: i
    for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)
}


class StatementError(RuntimeError):
    pass


class PartialMonth(StatementError):
    """A statement that leaves out days of its calendar month; `month` is that month's first day."""

    def __init__(self, kind: str, month: date):
        super().__init__(f"{kind} must cover a full calendar month; partial statements are refused")
        self.month = month


# ------------------------------------------------------------------- decrypt


class SecretsUnavailable(RuntimeError):
    """A company password could not be read; retry later, decide nothing."""


def month_from_filename(name: str) -> str | None:
    m = re.search(r"extL_([A-Z][a-z]{2})_(\d{4})_allmerge", name)
    if not m or m.group(1) not in MONTHS_EN:
        return None
    return f"{m.group(2)}-{MONTHS_EN[m.group(1)]:02d}"


def is_bt_statement(text: str) -> bool:
    """The text of a BT account statement: BT's name and an `EXTRAS CONT` header, as every BT statement prints."""
    return "BANCA TRANSILVANIA" in text.upper() and re.search(r"EXTRAS\s+CONT", text) is not None


def open_encrypted(
    path: Path, password_lookup=secrets.bt_statement_password, only: str | None = None, plain: bool = False
):
    """Try each company's Keychain password. Returns (company_key, pikepdf.Pdf) or raises.

    A statement that opens with neither password (e.g. the user's personal PFA
    statement) is not ours. A PDF without a password, accepted only when `plain` (a person brought it: the operator
    CLI or an upload, never mail, where it would prove nothing about its sender), belongs to the one company with a BT
    account whose CUI its header names; no password is read for it.
    """
    if _plain(path):
        if not plain:
            raise StatementError(f"{Path(path).name}: an unprotected PDF is imported only from the CLI or the app")
        return _open_plain(Path(path), only)
    unavailable = []
    for key, company in companies.all().items():
        if only is not None and key != only:  # the statement was uploaded for this company: no other is tried
            continue
        # only a company with a BT account, current or retired (its last statements still arrive), gets BT's PDFs
        if not any(a.bank == "bt" for a in company.accounts):
            continue
        try:
            pw = password_lookup(key)
        except secrets.SecretError as e:
            unavailable.append(str(e))
            continue
        try:
            pdf = pikepdf.open(path, password=pw)
        except pikepdf.PasswordError:
            continue
        finally:
            pw = None  # noqa: F841 - drop reference early
        return key, pdf
    if unavailable:
        # Can't tell "not ours" from "Keychain locked/inaccessible": never conclude not-ours.
        raise SecretsUnavailable(f"{Path(path).name}: " + "; ".join(unavailable))
    raise StatementError(f"{Path(path).name}: no company password opens it (not a statement of a configured company)")


def _plain(path: Path) -> bool:
    """A PDF that opens without a password (a damaged or missing one is neither: the password loop refuses it)."""
    try:
        pikepdf.open(path).close()
    except (pikepdf.PasswordError, pikepdf.PdfError, OSError, ValueError):
        return False
    return True


def _open_plain(path: Path, only: str | None):
    try:
        text = pdf_text(path)
    except RuntimeError:
        raise StatementError(f"{path.name}: cannot read the PDF") from None
    named = [
        key
        for key, company in companies.all().items()
        if any(a.bank == "bt" for a in company.accounts) and names_cui(text, company.cui)
    ]  # every firm first: a header naming two is nobody's, whichever firm it was uploaded for
    if len(named) != 1 or (only is not None and named[0] != only):
        raise StatementError(f"{path.name}: its header names no single configured company with a BT account")
    return named[0], pikepdf.open(path)


def encrypted(path: Path) -> bool:
    """A PDF that opens only with a password, as BT sends its statements."""
    try:
        pikepdf.open(path).close()
    except pikepdf.PasswordError:
        return True
    except (pikepdf.PdfError, OSError, ValueError):
        return False
    return False


def names_cui(text: str, cui: str) -> bool:
    """The header, before the first account section, names this CUI: `CUI: 123` or, on older BT statements,
    `CUI:123`, never a longer number. A CUI in a transaction's text (a payer's, a reference) is not the holder's."""
    lines = text.splitlines()
    header = "\n".join(itertools.takewhile(lambda ln: not _opens_account(ln), lines))
    return re.search(rf"CUI:[ \t]*{re.escape(cui)}(?!\d)", header) is not None


def _opens_account(line: str) -> bool:
    """The line that starts an account section, for the parser and the holder check alike."""
    return line.strip().startswith("CONT ") and "Valuta" in line


def decrypt_to(
    path: Path, out_dir: Path, password_lookup=secrets.bt_statement_password, only: str | None = None, plain=False
) -> tuple[str, str, Path]:
    """Decrypt statement into out_dir as an UNPROTECTED PDF.

    Returns (company_key, month 'YYYY-MM', output path). Verifies the CUI in the
    text matches the company whose password opened it.
    """
    path = Path(path)
    key, pdf = open_encrypted(path, password_lookup, only, plain)
    month = month_from_filename(path.name)
    tmp = out_dir / f".tmp-{path.stem}.pdf"
    out_dir.mkdir(parents=True, exist_ok=True)
    with pdf:
        pdf.save(tmp)  # pikepdf drops encryption unless encryption= is given
    try:
        with pikepdf.open(tmp) as check:  # must open WITHOUT a password
            if check.is_encrypted:
                raise StatementError("output is still encrypted")
        text = pdf_text(tmp)
        company = companies.get(key)
        if not names_cui(text, company.cui):
            raise StatementError(f"decrypted with {key} password but CUI {company.cui} not in text")
        if month is None:
            start = _pdf_period(text)[0]  # a monthly period, or an older daily statement's day
            if not start:
                raise StatementError("cannot determine statement month")
            month = start.strftime("%Y-%m")
        final = out_dir / f"Extras_BT_{key}_{month}.pdf"
        tmp.replace(final)
        return key, month, final
    finally:
        if tmp.exists():
            tmp.unlink()


# --------------------------------------------------------------------- parse


# An account or a card number inside the bank's text ("...;<NAME>;<IBAN>;<BIC>;..." on a transfer): never sent whole,
# however it is grouped. A candidate is a run of letters and digits with single spaces or hyphens between them; what is
# masked is the longest start of it that is an IBAN (ISO 13616 check digits) or a card number (Luhn), so the words
# after it stay. An unseparated run in an IBAN's or a card's shape is masked even when its check digits fail.
_IBAN_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}[ -]?\d{2}(?:[ -]?[A-Za-z0-9]){11,30}")
_CARD_RUN = re.compile(r"(?<![0-9])\d(?:[ -]?\d){12,18}(?![0-9])")
_SEP = re.compile(r"[ -]")


def _iban_ok(chars: str) -> bool:
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", chars):
        return False
    moved = chars[4:] + chars[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _luhn_ok(digits: str) -> bool:
    total = 0
    for n, d in enumerate(reversed(digits)):
        x = int(d) * (2 if n % 2 else 1)
        total += x - 9 if x > 9 else x
    return total % 10 == 0


def _mask(text: str, run: re.Pattern, ok, shape: re.Pattern) -> str:
    def one(m: re.Match) -> str:
        raw = m.group(0)
        ends = [i + 1 for i, c in enumerate(raw) if not _SEP.fullmatch(c)]  # each position a start of it may end at
        for end in reversed(ends):
            chars = _SEP.sub("", raw[:end]).upper()
            if ok(chars):
                return "••••" + chars[-4:] + raw[end:]
        word = re.match(r"[A-Za-z0-9]+", raw).group(0)
        return "••••" + word[-4:] + raw[len(word) :] if shape.fullmatch(word.upper()) else raw

    return run.sub(one, text)


def masked(text: str) -> str:
    """`text` with every IBAN and card number in it reduced to its last four (the rule above): what may be shown or kept
    of the bank's free text."""
    text = _mask(text, _IBAN_RUN, _iban_ok, re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}"))
    return _mask(text, _CARD_RUN, _luhn_ok, re.compile(r"\d{13,19}"))


@dataclass
class Tx:
    date: date
    title: str
    description: str
    direction: str  # "debit" or "credit"
    amount: Decimal
    currency: str
    iban: str
    ref: str = ""
    orig_amount: Decimal | None = None
    orig_currency: str = ""
    card_last4: str = ""
    country: str = ""
    counterparty: str = ""
    counterparty_iban: str = ""
    value_date: str = ""  # CAMT value date; booking date remains `date`
    category: str = ""  # filled by classify_tx
    bank_reference: str = ""  # account servicer reference, never an end-to-end/customer reference

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["amount"] = f"{self.amount:.2f}"
        d["orig_amount"] = f"{self.orig_amount:.2f}" if self.orig_amount is not None else ""
        if not self.bank_reference:
            d.pop("bank_reference", None)
        if not self.value_date:
            d.pop("value_date", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Tx:
        return cls(
            **d
            | {
                "date": date.fromisoformat(d["date"]),
                "amount": Decimal(d["amount"]),
                "orig_amount": Decimal(d["orig_amount"]) if d["orig_amount"] else None,
            }
        )


def tx_key(t: Tx) -> str:
    """Stable identity of a statement line, so a debit that paid one item never pays another (nor a credit
    collects two issued invoices). The direction is part of it: a credit never shares a debit's key.
    A debit keeps the formula from before directions were keyed, so the debits already reserved in
    `settled_tx` / `efactura.paid_tx` (only debits were ever reserved then) stay valid without a migration."""
    if t.ref.startswith("eb:"):  # read from the bank: its own reference is the identity, whatever text it adds later
        return hashlib.sha256(f"ob|{t.iban}|{t.ref}|{t.direction}".encode()).hexdigest()[:20]
    base = f"{t.iban}|{t.date}|{t.amount}|{t.ref}|{t.description}"
    if t.direction != "debit":
        base += f"|{t.direction}"
    return hashlib.sha256(base.encode()).hexdigest()[:20]


@dataclass
class Account:
    iban: str
    currency: str
    txs: list[Tx] = field(default_factory=list)
    total_debit: Decimal | None = None
    total_credit: Decimal | None = None
    trustworthy: bool = True
    # The bank's own closing balance (negative when overdrawn) and its day, kept only when the statement proves it:
    # the opening balance plus the lines it holds equals it. None when the format has none (CSV, the bank feed).
    closing: Decimal | None = None
    closing_day: date | None = None
    # The period the bank printed above this account's section (a BT PDF names one per section); None elsewhere.
    period: tuple[date, date] | None = None
    # An older daily statement's number (`EXTRAS CONT Numarul: N din dd/mm/yyyy`): BT numbers them one by one.
    serial: int | None = None

    @property
    def balanced(self) -> bool:
        d = sum((t.amount for t in self.txs if t.direction == "debit"), Decimal(0))
        c = sum((t.amount for t in self.txs if t.direction == "credit"), Decimal(0))
        return self.total_debit == d and self.total_credit == c


@dataclass
class Statement:
    company: str
    period_start: date | None
    period_end: date | None
    accounts: list[Account]
    partial: bool = field(default=False, kw_only=True)  # covers only its own dates (a CSV), not a whole month
    one_period: bool = field(default=True, kw_only=True)  # False: a PDF whose sections name different periods
    source: str = field(
        default="file", kw_only=True
    )  # "file": a statement document; "ob": from the bank (open banking)

    @property
    def txs(self) -> list[Tx]:
        return [t for a in self.accounts for t in a.txs]

    @property
    def settlement_txs(self) -> list[Tx]:
        return [t for a in self.accounts if a.trustworthy for t in a.txs]

    @property
    def month(self) -> str:
        return self.period_start.strftime("%Y-%m") if self.period_start else ""


_SKIP_TITLES = (
    "RULAJ ZI",
    "SOLD FINAL ZI",
    "SOLD ANTERIOR",
    "RULAJ TOTAL CONT",
    "SOLD FINAL CONT",
    "TOTAL DISPONIBIL",
    "Fonduri proprii",
    "Credit neutilizat",
)
_AMT = r"-?\d{1,3}(?:,\d{3})*\.\d{2}"
_ROW = re.compile(
    rf"^(?P<pre>\s*(?P<date>\d{{2}}/\d{{2}}/\d{{4}})?\s+)(?P<title>\S.*?)\s{{2,}}(?P<a1>{_AMT})(?:\s{{2,}}(?P<a2>{_AMT}))?\s*$"
)


def _enrich(tx: Tx) -> None:
    d = tx.description
    m = re.search(r"REF:\s*(\S+)", d)
    tx.ref = m.group(1) if m else ""
    tx.bank_reference = tx.ref
    m = re.search(r"valoare (?:tranzactie|trz):\s*(-?[\d.]+)\s+([A-Z]{3})", d)
    if m:
        tx.orig_amount, tx.orig_currency = Decimal(m.group(1)), m.group(2)
    m = re.search(r"\b\d{4}(\d{4})\s+(?:valoare|RRN)", d)
    if m:
        tx.card_last4 = m.group(1)
    m = re.search(r"\b([A-Z]{2,3})\s+\d{8}\s+(?:valoare|RRN)", d)
    if m:
        tx.country = {"ROM": "RO"}.get(m.group(1), m.group(1)[:2])
    m = re.search(r"TID:?\s*\S+\s+(.+?)\s+(?:[A-Z]{2,3}\s+)?\d{8}\s+(?:valoare|RRN)", d)
    if m:
        tx.counterparty = m.group(1).strip()
    else:
        # Transfers: "...;<NAME>;<IBAN>;<BIC>;..." -> the field before the IBAN is the name.
        fields = [f.strip() for f in d.split(";")]
        for i, f in enumerate(fields):
            if i and re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{12,30}", f.replace(" ", "")):
                tx.counterparty = re.sub(r"\s+", " ", fields[i - 1])
                tx.counterparty_iban = f.replace(" ", "")
                break


def parse_text(text: str, company: str) -> Statement:
    accounts: list[Account] = []
    acct: Account | None = None
    cur: Tx | None = None
    cur_lines: list[str] = []
    cur_date: date | None = None
    in_table = False
    debit_end = credit_end = 0
    opening: dict[int, Decimal] = {}  # by id(account): SOLD ANTERIOR, the first one of its section
    final: dict[int, Decimal] = {}  # SOLD FINAL CONT
    ends: dict[int, tuple[int, str | None]] = {}  # by id(tx): its amount's end column, the side it sits exactly under
    days: dict[tuple[int, date], tuple[Decimal, Decimal]] = {}  # by (id(account), day): RULAJ ZI debits, credits

    def close():
        nonlocal cur, cur_lines
        if cur is not None:
            cur.description = " ".join(s.strip() for s in cur_lines if s.strip())
            _enrich(cur)
            apply_wording(cur, banks.BY_ID["bt"])  # this reader is BT's: its PDF names lines in BT's words
            acct.txs.append(cur)
        cur, cur_lines = None, []

    period_start, period_end, one_period = _pdf_period(text)
    section = serial = None  # a statement header's period (and a daily one's number) since the last account opened
    carried = False  # after a page footer, until a header, an account or a table: the account's closing rows may come

    lines = text.splitlines()
    for i, ln in enumerate(lines):
        s = ln.strip()
        header = None if in_table else _section_header(ln)
        if carried and (header or _opens_account(ln)):
            carried = False  # another statement or account: the closing rows that follow are not this account's
        if not in_table and header:
            section, serial = header
        if _opens_account(ln):
            close()
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            acct, cur_date = _pdf_account(nxt), None  # a section's lines are dated by its own rows only
            # its own header, else the file's one period; a section under none, in a file of several, has none
            acct.period = section or ((period_start, period_end) if one_period and period_start else None)
            acct.serial = serial if section else None
            section = serial = None
            accounts.append(acct)
            in_table = False
            continue
        columns = _pdf_columns(ln)
        if columns is not None:
            debit_end, credit_end = columns
            in_table, carried = acct is not None, False
            continue
        if s.startswith("BANCA TRANSILVANIA S.A."):
            carried = carried or (in_table and id(acct) not in final)
            in_table = False  # page footer; current tx may continue on next page
            continue
        if carried and (row := _closing_row(ln)):  # the totals or final balance alone on the next page
            close()
            _carried_balance(acct, row, opening, final)
            carried = row.group("title").strip() != "SOLD FINAL CONT"
            continue
        if not in_table or not s:
            continue
        row = _ROW.match(ln)
        if row and (row.group("date") or row.end("a1") > debit_end - 15):
            title = row.group("title").strip()
            if row.group("date"):
                cur_date = parse_dmy(row.group("date"))
            if title.startswith(_SKIP_TITLES):
                close()
                _pdf_balance(acct, row, title, opening, final, (days, cur_date))
                if title.startswith("SOLD FINAL CONT"):
                    in_table = False
                continue
            close()
            if cur_date is None:
                raise StatementError("PDF line without a booking date; download a complete statement")
            a1_end = row.end("a1")
            direction = "debit" if abs(a1_end - debit_end) <= abs(a1_end - credit_end) else "credit"
            cur = Tx(
                date=cur_date,
                title=title,
                description="",
                direction=direction,
                amount=parse_amount(row.group("a1")),
                currency=acct.currency,
                iban=acct.iban,
            )
            ends[id(cur)], cur_lines = (a1_end, _under(a1_end, debit_end, credit_end)), [title]
            continue
        if s.startswith(_SKIP_TITLES):
            close()
            if s.startswith("SOLD FINAL CONT"):
                in_table = False
            continue
        if cur is not None:
            cur_lines.append(s)
    close()
    _pdf_trust(_directions_by_totals(accounts, ends, days), opening, final)
    if periods := [a.period for a in accounts if a.period]:  # the sections' days, not a period printed elsewhere
        period_start, period_end = min(p[0] for p in periods), max(p[1] for p in periods)
    return Statement(
        company=company, period_start=period_start, period_end=period_end, accounts=accounts, one_period=one_period
    )


def _section_header(line: str) -> tuple[tuple[date, date], int | None] | None:
    """A statement header's period, and an older daily one's number."""
    if m := re.search(_HEADER, line):
        return (parse_dmy(m.group(1)), parse_dmy(m.group(2))), None
    if m := re.search(_DAILY, line):
        return (parse_dmy(m.group(2)),) * 2, int(m.group(1))
    return None


_CLOSING = re.compile(
    rf"^\s*(?P<title>RULAJ TOTAL CONT|SOLD FINAL CONT)\s{{2,}}(?P<a1>{_AMT})(?:\s{{2,}}(?P<a2>{_AMT}))?\s*$"
)


def _closing_row(line: str) -> re.Match[str] | None:
    """A row of an account's totals (two amounts) or final balance (one), nothing else on it: it may follow a page
    break."""
    row = _CLOSING.match(line)
    return row if row and bool(row.group("a2")) == (row.group("title") == "RULAJ TOTAL CONT") else None


def _carried_balance(acct: Account, row: re.Match[str], opening: dict, final: dict) -> None:
    """A closing row read after a page break. Totals the bank printed already are never replaced: two that disagree
    leave the account unproven and unbalanced for good (its totals and final balance are marked unknown, and nothing
    later sets them)."""
    if id(acct) in final and final[id(acct)] is None:
        return
    if row.group("title").strip() == "RULAJ TOTAL CONT" and acct.total_debit is not None:
        if (acct.total_debit, acct.total_credit) != (parse_amount(row.group("a1")), parse_amount(row.group("a2"))):
            final[id(acct)], acct.total_debit, acct.total_credit = None, None, None
        return
    _pdf_balance(acct, row, row.group("title").strip(), opening, final)


def _pdf_columns(line: str) -> tuple[int, int] | None:
    stripped = line.strip()
    if stripped.startswith("Data") and "Descriere" in stripped and "Debit" in stripped and "Credit" in stripped:
        return line.index("Debit") + len("Debit"), line.index("Credit") + len("Credit")
    return None


_PERIOD = r"din (\d{2}/\d{2}/\d{4}) - (\d{2}/\d{2}/\d{4})"
_HEADER = r"(?i)\bextras\b.*?" + _PERIOD  # a statement's own header ("EXTRAS CONT ... din"), not a line's text


# An older daily statement's header names its number and its one day.
_DAILY = r"EXTRAS CONT\s+Numarul:\s*(\d+)\s+din (\d{2}/\d{2}/\d{4})(?!\s*-\s*\d)"


def _pdf_period(text: str) -> tuple[date | None, date | None, bool]:
    period_start = period_end = None
    periods = set(re.findall(_PERIOD, text)) | {(d, d) for _, d in re.findall(_DAILY, text)}
    if m := re.search(_PERIOD, text):
        period_start, period_end = parse_dmy(m.group(1)), parse_dmy(m.group(2))
    elif m := re.search(_DAILY, text):
        period_start = period_end = parse_dmy(m.group(2))
    one_period = len(periods) == 1

    return period_start, period_end, one_period


def _pdf_account(line: str) -> Account:
    """The account line: its currency is the token right before `Cod IBAN`, alone on the line (2024 on) or after
    `Extras numarul N` (2021-2023). A BT IBAN spells its currency too (`BTRL<CCY>`): the two must agree."""
    m = re.search(r"(?:^|\s)([A-Z]{3})\s+Cod IBAN:\s*(RO[0-9A-Z]{22})", line)
    if not re.search(r"Cod IBAN:\s*RO[0-9A-Z]{22}", line):
        raise StatementError("PDF account IBAN missing; download a complete statement")
    if not m:
        raise StatementError("PDF account currency missing; download a complete statement")
    currency, iban = m.groups()
    if iban[4:8] == "BTRL" and iban[8:11].isalpha() and iban[8:11] != currency:
        raise StatementError("PDF account currency contradicts its IBAN; download a complete statement")
    return Account(iban=iban, currency=currency)


def _pdf_balance(
    acct: Account, row: re.Match[str], title: str, opening: dict[int, Decimal], final: dict[int, Decimal], daily=None
) -> None:
    if daily and title.startswith("RULAJ ZI") and row.group("a2") and daily[1]:
        key, totals = (id(acct), daily[1]), (parse_amount(row.group("a1")), parse_amount(row.group("a2")))
        daily[0][key] = totals if daily[0].get(key, totals) == totals else None  # None: two that disagree
    if title.startswith("SOLD ANTERIOR") and acct.txs == [] and id(acct) not in opening:
        opening[id(acct)] = parse_amount(row.group("a1"))
    if title.startswith("SOLD FINAL CONT") and final.get(id(acct), 0) is not None:  # None: contradicted totals
        final[id(acct)] = parse_amount(row.group("a1"))
    if title.startswith("RULAJ TOTAL CONT") and row.group("a2"):
        acct.total_debit = parse_amount(row.group("a1"))
        acct.total_credit = parse_amount(row.group("a2"))


def _under(end: int, debit_end: int, credit_end: int) -> str | None:
    """The column an amount sits exactly under (its end within 2 of the header's), else None."""
    return "debit" if abs(end - debit_end) <= 2 else "credit" if abs(end - credit_end) <= 2 else None


def _directions_by_totals(accounts: list[Account], ends: dict, days: dict) -> list[Account]:
    """Older BT PDFs print lines of different widths, so an amount's column measured against the header's can land
    nearer the wrong one. When the lines read so don't add up to the account's own totals (RULAJ TOTAL CONT: debits,
    credits), the amounts' columns are split where they leave the widest gap, the left ones debits: that reading is
    kept only if it is the one reading (that split, or all one side) giving both totals to the cent, it moves no line
    that sits exactly under a header column, and no other assignment of the lines gives the same totals (checked day
    by day against RULAJ ZI when the statement prints it): otherwise which line is which isn't proven, and the section
    stays as read, and unproven. A moved line is read for BT's words again (a fee is a debit)."""
    for acct in accounts:
        if not (acct.balanced or acct.total_debit is None or not acct.txs):
            _redirect(acct, ends, {d: v for (a, d), v in days.items() if a == id(acct)})
    return accounts


MAX_LINES = 20  # lines of one day (or section) whose assignment is checked to be the only one; more: unproven


def _only_assignment(amounts: list[Decimal], debit: Decimal) -> bool:
    """Exactly one choice of these lines sums to `debit` (the rest being the credits). Only positive amounts are
    counted so; any other leaves it unproven."""
    if len(amounts) > MAX_LINES or any(a <= 0 for a in amounts):
        return False
    ways: dict[Decimal, int] = {Decimal(0): 1}
    for a in amounts:
        for total, n in list(ways.items()):
            if total + a <= debit:
                ways[total + a] = ways.get(total + a, 0) + n
    return ways.get(debit, 0) == 1


def _proven(acct: Account, reading: dict[int, str], days: dict) -> bool:
    """No other assignment of the lines gives the bank's totals. The reading gives every day's RULAJ ZI it prints (two
    that disagree prove nothing); it is the only one per day when every line's day has one, else over the section."""
    if None in days.values():
        return False
    for d in days:  # a day the section prints totals for but holds no line of must total nothing
        lines = [t for t in acct.txs if t.date == d]
        debit = sum((t.amount for t in lines if reading[id(t)] == "debit"), Decimal(0))
        credit = sum((t.amount for t in lines if reading[id(t)] == "credit"), Decimal(0))
        if (debit, credit) != days[d]:
            return False
    if days and all(t.date in days for t in acct.txs):
        return all(
            _only_assignment([t.amount for t in acct.txs if t.date == d], days[d][0])
            for d in {t.date for t in acct.txs}
        )
    return _only_assignment([t.amount for t in acct.txs], acct.total_debit)


def _redirect(acct: Account, ends: dict, days: dict) -> None:
    cols = sorted({ends[id(t)][0] for t in acct.txs})
    gap = max(range(1, len(cols)), key=lambda i: cols[i] - cols[i - 1], default=None)
    readings = [{id(t): side for t in acct.txs} for side in ("debit", "credit")]
    if gap is not None:
        readings.append({id(t): "debit" if ends[id(t)][0] < cols[gap] else "credit" for t in acct.txs})

    def adds_up(reading: dict[int, str]) -> bool:
        total = {"debit": Decimal(0), "credit": Decimal(0)}
        for t in acct.txs:
            total[reading[id(t)]] += t.amount
        return (total["debit"], total["credit"]) == (acct.total_debit, acct.total_credit)

    fitting = [r for r in readings if adds_up(r)]
    if len(fitting) != 1:
        return
    reading = fitting[0]
    if any((under := ends[id(t)][1]) and under != reading[id(t)] for t in acct.txs):
        return  # it would move a line the header's own column places
    if not _proven(acct, reading, days):
        return  # another assignment of the lines gives the same totals: which line is which isn't proven
    for t in acct.txs:
        if t.direction != reading[id(t)]:
            t.direction, t.category = reading[id(t)], ""
            apply_wording(t, banks.BY_ID["bt"])


def _pdf_trust(accounts: list[Account], opening: dict[int, Decimal], final: dict[int, Decimal]) -> None:
    """The closing balance counts only when the opening one and the lines add up to it, to the cent; it is dated by
    the period printed above its section, so a section without one, or with a line outside it, proves nothing."""
    for a in accounts:
        start, end = opening.get(id(a)), final.get(id(a))
        a.trustworthy = bool(
            a.period
            and all(a.period[0] <= t.date <= a.period[1] for t in a.txs)
            and start is not None
            and end is not None
            and a.balanced
            and start + a.total_credit - a.total_debit == end
        )
        if a.trustworthy:
            a.closing, a.closing_day = end, a.period[1]


SEVERAL_MONTHS = (
    "Statement of several months that can't be split safely by month; download each month's statement from the bank"
)


def several_months(st: Statement) -> bool:
    """A PDF whose sections name periods of different months: BT's quarterly statement."""
    return len({d.strftime("%Y-%m") for a in st.accounts if a.period for d in a.period}) > 1


def split_months(path: Path, company: str, st: Statement, folder: Path) -> list[tuple[str, Path, Statement]]:
    """A statement of several months, split by its pages into one PDF per month, each read again as a statement of
    its own: (month, file, statement). Refused unless every section covers days of one month, no page holds two
    months, each part names the firm in its own header (a stored part is read again on its own) and the parts read
    back to exactly the file's sections, lines and balances."""
    if any(not a.period or a.period[0].strftime("%Y-%m") != a.period[1].strftime("%Y-%m") for a in st.accounts):
        raise StatementError(SEVERAL_MONTHS)
    folder.mkdir(parents=True, exist_ok=True)
    parts = []
    with pikepdf.open(path) as pdf:
        for month, pages in _pages_by_month(pdf_text(path), len(pdf.pages), st).items():
            out = folder / f"Extras_BT_{company}_{month}.pdf"
            with pikepdf.new() as piece:
                piece.pages.extend(pdf.pages[i] for i in pages)
                piece.save(out, deterministic_id=True)  # the same file splits into the same bytes: a duplicate
            text = pdf_text(out)
            if not names_cui(text, companies.get(company).cui):
                raise StatementError(SEVERAL_MONTHS)
            parts.append((month, out, parse_text(text, company)))
    if Counter(map(_section, st.accounts)) != Counter(_section(a) for *_, p in parts for a in p.accounts) or any(
        a.period[0].strftime("%Y-%m") != month for month, _, p in parts for a in p.accounts
    ):
        raise StatementError(SEVERAL_MONTHS)
    return parts


def _pages_by_month(text: str, count: int, st: Statement) -> dict[str, list[int]]:
    """Each page under the month of the periods it prints, a page printing none under the one before (the first
    month for a header page)."""
    pages = text.split("\f")
    if len(pages) < count:
        raise StatementError(SEVERAL_MONTHS)
    month = min(a.period[0] for a in st.accounts).strftime("%Y-%m")
    out: dict[str, list[int]] = {}
    for i, page in enumerate(pages[:count]):
        named = {parse_dmy(m.group(1)).strftime("%Y-%m") for m in re.finditer(_PERIOD, page)}
        if len(named) > 1:
            raise StatementError(SEVERAL_MONTHS)
        month = named.pop() if named else month
        out.setdefault(month, []).append(i)
    return out


def _section(a: Account) -> tuple:
    return (a.iban, a.currency, a.period, a.closing, a.total_debit, a.total_credit, a.trustworthy) + tuple(
        tx_key(t) for t in a.txs
    )


def parse_pdf(path: Path, company: str) -> Statement:
    return parse_text(pdf_text(path.read_bytes()), company)


def parse(path: Path, company: str) -> Statement:
    """Choose the statement format from its bytes, regardless of the filename. A stored CSV is the exception: it names
    no account, so it is read with the account and columns stored beside it when it was imported."""
    if Path(path).suffix == ".csv":
        from fiscalm.bank.csv import MAX_CSV, parse_csv

        side = json.loads(Path(str(path) + ".json").read_text(encoding="utf-8"))
        with Path(path).open("rb") as stream:
            return parse_csv(stream.read(MAX_CSV + 1), company, side["iban"], side["mapping"])
    with Path(path).open("rb") as stream:
        prefix = stream.read(4096)
    if prefix.startswith(b"%PDF-"):
        return parse_pdf(path, company)
    if prefix.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        from fiscalm.bank.camt import parse_camt

        return parse_camt(path, company)
    from fiscalm.bank.mt940 import MAX_MT940, looks_like, parse_mt940

    if looks_like(prefix):
        with Path(path).open("rb") as stream:
            return parse_mt940(stream.read(MAX_MT940 + 1), company)
    raise StatementError("Unsupported statement: use a BT PDF, CAMT.053 XML or MT940 export")


# ------------------------------------------------------------------ classify

# Categories for outflows. Only "needs_invoice" can end up flagged as missing.
BANK_FEE = "bank_fee"
INTEREST = "interest"  # interest the bank pays or charges: no invoice, never an invoice's payment
TAX = "tax"
OWNER_ADVANCE = "owner_advance"  # "Avans trezorerie" to the user
DOMESTIC = "domestic"  # Romanian supplier -> invoice arrives via e-Factura/SPV
NEEDS_INVOICE = "needs_invoice"  # foreign supplier -> must have a receipt in the pack
INCOME = "income"
CASH = "cash"  # a cash withdrawal, by the bank's code: money the firm still holds


# ISO 20022 sub-family codes (CAMT's BkTxCd, the open banking feed's bank_transaction_code) a parser reads as a fee
# or as interest. One list for every format that carries them.
ISO_FEES = ("CHRG", "COMM", "COMI", "COMT", "FEES")
ISO_INTEREST = ("INTR",)
ISO_CASH = ("CWDL",)  # a cash withdrawal


def iso_category(*codes: str) -> str:
    """What ISO 20022 transaction codes say a line is: BANK_FEE, INTEREST, CASH, or "" when they don't say."""
    up = {c.strip().upper() for c in codes if c}
    if up & set(ISO_FEES):
        return BANK_FEE
    return INTEREST if up & set(ISO_INTEREST) else CASH if up & set(ISO_CASH) else ""


def apply_wording(tx: Tx, bank) -> None:
    """What a bank's own words (`banks.Bank.wording`) say about one of its lines: its fee, its interest, or a card
    payment at its own terminal (then the merchant is in the bank's country). For the text formats: its PDF and CSV."""
    wording = bank.wording
    if tx.direction == "debit" and (
        tx.title.startswith(wording.fee_titles) or any(p in tx.description.upper() for p in wording.fee_phrases)
    ):
        tx.category = BANK_FEE
    elif tx.title.startswith(wording.interest_titles):
        tx.category = INTEREST
    elif tx.title.startswith(wording.own_terminal_titles) and not any(
        m in tx.title for m in wording.other_terminal_marks
    ):
        tx.country = bank.country


def pays_nothing(tx: Tx) -> bool:
    """The bank's own line (a fee, interest): it never pays an invoice or a payment-file row, whatever it fits."""
    return classify_tx(tx) in (BANK_FEE, INTEREST)


_CASH = re.compile(r"\bATM\b|RETRAGERE\s+(?:DE\s+)?NUMERAR|CASH\s+WITHDRAWAL")


def cash_withdrawal(tx: Tx) -> bool:
    """Cash taken out of the account (an ATM): money the firm still holds, never an invoice's payment."""
    return tx.category == CASH or bool(_CASH.search(f"{tx.title} {tx.description}".upper()))


def classify_tx(tx: Tx) -> str:
    """The line's category for the rules. Fees and interest come from its format (each parser sets them from its
    codes or its bank's wording); the rest is bank-neutral."""
    if tx.category in (BANK_FEE, INTEREST, CASH):
        return tx.category
    d = tx.description.upper()
    if tx.direction == "credit":
        tx.category = INCOME
    elif "TREZ" in tx.counterparty_iban or "BUGETUL" in d:
        tx.category = TAX
    elif "AVANS TREZORERIE" in d:  # the owner's other advances: the user's Avans, remembered for their account
        tx.category = OWNER_ADVANCE
    elif not tx.card_last4 and tx.counterparty_iban.startswith("RO"):
        tx.category = DOMESTIC
    else:
        tx.category = NEEDS_INVOICE
    return tx.category
