"""BT Go bulk-import CSV: the payment file format `btgo-csv` (payfiles.FORMATS), for BT's RON accounts. pay.py keeps
one rolling file per company; this module only turns rows into the file and checks a file.

Format (NOT yet validated by a real BT Go import):
  header OrderNumber,SourceAccountNumber,TargetAccountNumber,BeneficiaryName,
         BeneficiaryBankBIC,BeneficiaryFiscalCode,Amount,PaymentRef1,PaymentRef2,
         ValueDate,Urgent
  comma separator, CRLF, no quoting, no commas/semicolons/diacritics in text,
  details <= 105 chars, Amount '1234.50', ValueDate dd/mm/yyyy, Urgent F.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal
from pathlib import Path

from fiscalm import companies
from fiscalm.bank import catalogue as banks
from fiscalm.ledger import is_live
from fiscalm.payments.orders import PaymentFileError, PaymentItem
from fiscalm.validate import iban_valid

HEADER = [
    "OrderNumber",
    "SourceAccountNumber",
    "TargetAccountNumber",
    "BeneficiaryName",
    "BeneficiaryBankBIC",
    "BeneficiaryFiscalCode",
    "Amount",
    "PaymentRef1",
    "PaymentRef2",
    "ValueDate",
    "Urgent",
]
MAX_DETAILS = 105
_ALLOWED = re.compile(r"[^A-Za-z0-9 ./()+\-]")


CsvError = PaymentFileError  # the name older callers and tests use


def clean_text(s: str, max_len: int | None = None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace(",", " ").replace(";", " ")
    s = _ALLOWED.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if max_len is not None:
        s = s[:max_len].rstrip()
    return s


def build_rows(items: list[PaymentItem], value_date: date) -> list[list[str]]:
    if not items:
        raise CsvError("no items")
    keys = {i.company for i in items}
    if len(keys) != 1:
        raise CsvError(f"one file per company; got {sorted(keys)}")
    company = companies.get(keys.pop())
    rows = []
    for n, it in enumerate(items, start=1):
        # A row debits the account it was exported from, even after the paying account changed, so a rebuilt,
        # downloaded or archived file never moves a payment to another account. That account must be the company's
        # and at BT: this is a BT Go file (pay.py picks the format from the paying account's bank; this re-checks).
        source = it.source_iban or company.ron_iban
        account = company.account(source) if source else None
        if not account:
            raise CsvError("Configure the company RON IBAN before building payment files.")
        if account.bank not in ("bt", ""):  # "": outside the catalogue, only rows exported before formats (pay._format)
            raise CsvError("A BT Go file debits only BT accounts; this row's account is at another bank.")
        if account.currency != "RON":
            raise CsvError("A BT Go file debits only RON accounts.")
        is_trez = it.payee_iban[4:8] == "TREZ"
        rows.append(
            [
                str(n),
                account.iban,
                it.payee_iban,
                clean_text(it.payee_name, 70),
                it.payee_bic,
                company.cui if is_trez else "",
                f"{Decimal(it.amount):.2f}",
                clean_text(it.details, MAX_DETAILS),
                "",
                value_date.strftime("%d/%m/%Y"),
                "F",
            ]
        )
    return rows


def render(rows: list[list[str]]) -> str:
    out = [",".join(HEADER)] + [",".join(r) for r in rows]
    return "\r\n".join(out) + "\r\n"


def validate_text(data: str, company_key: str) -> list[str]:
    errs: list[str] = []
    company = companies.get(company_key)
    try:
        data.encode("ascii")
    except UnicodeEncodeError:
        errs.append("non-ASCII characters present")
    if not data.endswith("\r\n") or re.search(r"(?<!\r)\n", data):
        errs.append("line endings must be CRLF")
    lines = data.split("\r\n")[:-1]
    if not lines or lines[0] != ",".join(HEADER):
        errs.append("bad header")
        return errs
    seen = set()
    for ln_no, ln in enumerate(lines[1:], start=2):
        f = ln.split(",")
        if len(f) != len(HEADER):
            errs.append(f"line {ln_no}: {len(f)} fields, expected {len(HEADER)}")
            continue
        row = dict(zip(HEADER, f, strict=True))
        if '"' in ln or ";" in ln:
            errs.append(f"line {ln_no}: quotes/semicolons not allowed")
        if row["OrderNumber"] in seen or not row["OrderNumber"].isdigit():
            errs.append(f"line {ln_no}: bad/duplicate OrderNumber")
        seen.add(row["OrderNumber"])
        source = company.account(row["SourceAccountNumber"]) if row["SourceAccountNumber"] else None
        # a RON account of the company, current or retired since the row was exported from it (btgo.build_rows)
        if not source or source.bank not in ("bt", "") or source.currency != "RON":
            errs.append(f"line {ln_no}: source account is not a {company.key} account")
        tgt = row["TargetAccountNumber"]
        if not iban_valid(tgt):
            errs.append(f"line {ln_no}: target IBAN {tgt[-4:]} fails mod-97")
        is_trez = tgt[4:8] == "TREZ"
        if is_trez:
            if row["BeneficiaryBankBIC"] != banks.TREASURY_BIC:
                errs.append(f"line {ln_no}: treasury payment needs BIC {banks.TREASURY_BIC}")
            if row["BeneficiaryFiscalCode"] != company.cui:
                errs.append(f"line {ln_no}: treasury payment needs fiscal code {company.cui}")
        elif row["BeneficiaryFiscalCode"]:
            errs.append(f"line {ln_no}: fiscal code only allowed for treasury payments")
        if not re.fullmatch(r"\d+\.\d{2}", row["Amount"]) or Decimal(row["Amount"]) <= 0:
            errs.append(f"line {ln_no}: bad amount {row['Amount']!r}")
        for k in ("PaymentRef1", "PaymentRef2", "BeneficiaryName"):
            if len(row[k]) > MAX_DETAILS or _ALLOWED.search(row[k]):
                errs.append(f"line {ln_no}: {k} too long or has special chars")
        if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", row["ValueDate"]):
            errs.append(f"line {ln_no}: bad ValueDate")
        if row["Urgent"] != "F":
            errs.append(f"line {ln_no}: Urgent must be F")
    return errs


def value_date_of(data: str) -> date | None:
    """The ValueDate of a file we wrote (every row has the same one)."""
    lines = data.split("\r\n")
    if len(lines) < 2 or not lines[1]:
        return None
    d, m, y = lines[1].split(",")[HEADER.index("ValueDate")].split("/")
    return date(int(y), int(m), int(d))


def validate_file(path: Path, company_key: str) -> list[str]:
    return validate_text(Path(path).read_bytes().decode("ascii", errors="replace"), company_key)


def build(items: list[PaymentItem], value_date: date) -> bytes:
    """The file's bytes for `items` (one company), checked before they are returned."""
    if not is_live():
        raise CsvError("Payment files require a live installation")
    data = render(build_rows(items, value_date))
    errors = validate_text(data, items[0].company)
    if errors:
        raise CsvError("; ".join(errors))
    return data.encode("ascii")


def check(data: bytes, company_key: str) -> list[str]:
    return validate_text(data.decode("ascii", errors="replace"), company_key)


def value_date(data: bytes) -> date | None:
    return value_date_of(data.decode("ascii", "replace"))
