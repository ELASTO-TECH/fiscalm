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


class MissingAccounts(StatementError):
    """A statement that leaves out accounts the company held at its bank in that month; `accounts` names each
    one by currency and last four characters, e.g. "EUR 0001"."""

    def __init__(self, accounts: list[str]):
        super().__init__(
            "Statement missing configured accounts: "
            + ", ".join(accounts)
            + "; download a full company statement from the bank, or set the month an account opened"
            + " (fiscalm company opened)"
        )
        self.accounts = accounts


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
            m = re.search(r"din (\d{2}/\d{2}/\d{4}) - (\d{2}/\d{2}/\d{4})", text)
            if not m:
                raise StatementError("cannot determine statement month")
            month = parse_dmy(m.group(1)).strftime("%Y-%m")
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

    def close():
        nonlocal cur, cur_lines
        if cur is not None:
            cur.description = " ".join(s.strip() for s in cur_lines if s.strip())
            _enrich(cur)
            apply_wording(cur, banks.BY_ID["bt"])  # this reader is BT's: its PDF names lines in BT's words
            acct.txs.append(cur)
        cur, cur_lines = None, []

    period_start, period_end, one_period = _pdf_period(text)

    lines = text.splitlines()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if _opens_account(ln):
            close()
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            acct = _pdf_account(nxt)
            accounts.append(acct)
            in_table = False
            continue
        columns = _pdf_columns(ln)
        if columns is not None:
            debit_end, credit_end = columns
            in_table = acct is not None
            continue
        if s.startswith("BANCA TRANSILVANIA S.A."):
            in_table = False  # page footer; current tx may continue on next page
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
                _pdf_balance(acct, row, title, opening, final)
                if title.startswith("SOLD FINAL CONT"):
                    in_table = False
                continue
            close()
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
            cur_lines = [title]
            continue
        if s.startswith(_SKIP_TITLES):
            close()
            if s.startswith("SOLD FINAL CONT"):
                in_table = False
            continue
        if cur is not None:
            cur_lines.append(s)
    close()
    _pdf_trust(accounts, opening, final, one_period, period_end)
    return Statement(
        company=company, period_start=period_start, period_end=period_end, accounts=accounts, one_period=one_period
    )


def _pdf_columns(line: str) -> tuple[int, int] | None:
    stripped = line.strip()
    if stripped.startswith("Data") and "Descriere" in stripped and "Debit" in stripped and "Credit" in stripped:
        return line.index("Debit") + len("Debit"), line.index("Credit") + len("Credit")
    return None


def _pdf_period(text: str) -> tuple[date | None, date | None, bool]:
    pattern = r"din (\d{2}/\d{2}/\d{4}) - (\d{2}/\d{2}/\d{4})"
    period_start = period_end = None
    m = re.search(pattern, text)
    if m:
        period_start, period_end = parse_dmy(m.group(1)), parse_dmy(m.group(2))
    # A closing balance is dated by the statement's period: only when the whole file names one period.
    one_period = len(set(re.findall(pattern, text))) == 1

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
    acct: Account, row: re.Match[str], title: str, opening: dict[int, Decimal], final: dict[int, Decimal]
) -> None:
    if title.startswith("SOLD ANTERIOR") and acct.txs == [] and id(acct) not in opening:
        opening[id(acct)] = parse_amount(row.group("a1"))
    if title.startswith("SOLD FINAL CONT"):
        final[id(acct)] = parse_amount(row.group("a1"))
    if title.startswith("RULAJ TOTAL CONT") and row.group("a2"):
        acct.total_debit = parse_amount(row.group("a1"))
        acct.total_credit = parse_amount(row.group("a2"))


def _pdf_trust(
    accounts: list[Account],
    opening: dict[int, Decimal],
    final: dict[int, Decimal],
    one_period: bool,
    period_end: date | None,
) -> None:
    for a in accounts:  # the closing balance counts only when the opening one and the lines add up to it, to the cent
        start, end = opening.get(id(a)), final.get(id(a))
        a.trustworthy = bool(
            one_period
            and start is not None
            and end is not None
            and a.balanced
            and start + a.total_credit - a.total_debit == end
        )
        if a.trustworthy:
            a.closing, a.closing_day = end, period_end


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


# ISO 20022 sub-family codes (CAMT's BkTxCd, the open banking feed's bank_transaction_code) a parser reads as a fee
# or as interest. One list for every format that carries them.
ISO_FEES = ("CHRG", "COMM", "COMI", "COMT", "FEES")
ISO_INTEREST = ("INTR",)


def iso_category(*codes: str) -> str:
    """What ISO 20022 transaction codes say a line is: BANK_FEE, INTEREST, or "" when they don't say."""
    up = {c.strip().upper() for c in codes if c}
    return BANK_FEE if up & set(ISO_FEES) else INTEREST if up & set(ISO_INTEREST) else ""


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


def classify_tx(tx: Tx) -> str:
    """The line's category for the rules. Fees and interest come from its format (each parser sets them from its
    codes or its bank's wording); the rest is bank-neutral."""
    if tx.category in (BANK_FEE, INTEREST):
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
