"""Parse tax payment orders (OP PDFs) into payment items.

Every item passes hard validations or is refused with reasons. Nothing here is
guessed: if a field cannot be read unambiguously, the item is refused.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from fiscalm import companies
from fiscalm.bank import catalogue as banks
from fiscalm.pdftext import parse_amount, parse_dmy, pdf_text, sha256
from fiscalm.validate import WordsError, iban_valid, ro_words_to_int


class PaymentFileError(ValueError):
    """A payment file that can't be built or isn't what fiscalm wrote: nothing is written, the rows stay where they are."""


@dataclass
class PaymentItem:
    company: str
    kind: str  # "op" (tax payment order) or "efactura" (supplier invoice)
    payee_name: str
    payee_iban: str
    payee_bic: str
    payee_fiscal_code: str  # only for treasury payments (payer CUI), else ""
    amount: Decimal
    details: str
    issue_date: date
    doc_ref: str  # e.g. "OP 1 22/09/2026" or "AA 4669"
    source_path: str
    source_sha: str
    source_iban: str = (
        ""  # the company account the row debits once exported (payments.source_iban); "" = the paying one
    )

    @property
    def item_id(self) -> str:
        return f"{self.company}|{self.kind}|{self.payee_iban}|{self.amount:.2f}|{self.issue_date.isoformat()}|{self.doc_ref}"

    @property
    def month(self) -> str:
        return self.issue_date.strftime("%Y-%m")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount"] = f"{self.amount:.2f}"
        d["issue_date"] = self.issue_date.isoformat()
        d["item_id"] = self.item_id
        return d


@dataclass
class ParseResult:
    source: str
    item: PaymentItem | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.item is not None and not self.errors


class Refused(Exception):
    pass


def _lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines()]


def _next_nonblank(lines: list[str], i: int) -> tuple[int, str]:
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    return (j, lines[j].strip()) if j < len(lines) else (j, "")


def _chunks(line: str) -> list[str]:
    """Split a layout line on runs of 2+ spaces."""
    return [c for c in re.split(r"\s{2,}", line.strip()) if c]


# --------------------------------------------------------------------------- OP


def is_op(text: str) -> bool:
    return "ORDIN DE PLATA" in text and "PLATITOR" in text


def parse_op_text(text: str, source: str = "", source_sha: str = "") -> ParseResult:
    res = ParseResult(source=source)
    errs = res.errors
    lines = _lines(text)

    op_no, figures = _op_amount(lines, errs)

    payer_name = ""
    m = re.search(r"^PLATITOR\s+(.+)$", text, re.M)
    if m:
        payer_name = m.group(1).strip()
    else:
        errs.append("PLATITOR name not found")

    cifs = []
    for i, ln in enumerate(lines):
        if ln.strip().startswith("Cod de identificare fiscala"):
            _, nxt = _next_nonblank(lines, i)
            mm = re.match(r"(\d{2,10})\b", nxt)
            cifs.append(mm.group(1) if mm else "")
    payer_cui = cifs[0] if cifs else ""
    benef_cui = cifs[1] if len(cifs) > 1 else ""

    m_payer = re.search(r"^\s*platitor\s+(RO[0-9A-Z]{22})\s+(\w+)", text, re.M)
    m_benef = re.search(r"^\s*beneficiar\s+(RO[0-9A-Z]{22})\s+(\w+)", text, re.M)
    payer_iban = m_payer.group(1) if m_payer else ""
    benef_iban = m_benef.group(1) if m_benef else ""
    benef_bic = m_benef.group(2) if m_benef else ""
    if not payer_iban:
        errs.append("payer IBAN not found")
    if not benef_iban:
        errs.append("beneficiary IBAN not found")

    benef_name = _op_following(lines, "BENEFICIAR", errs, "beneficiary name not found", prefix=True)
    details = _op_following(lines, "Reprezentand", errs, "'Reprezentand' text not found")

    m_date = re.search(r"Data emiterii\s+(\d{2}/\d{2}/\d{4})", text)
    issue = None
    if m_date:
        issue = parse_dmy(m_date.group(1))
    else:
        errs.append("Data emiterii not found")

    company = _op_company(payer_name, payer_cui, payer_iban, errs)
    for label, ib in (("payer", payer_iban), ("beneficiary", benef_iban)):
        if ib and not iban_valid(ib):
            errs.append(f"{label} IBAN {ib[-4:]} fails mod-97 checksum")
    fiscal = _op_treasury(benef_iban, benef_bic, benef_cui, payer_cui, errs)

    if errs:
        return res
    res.item = PaymentItem(
        company=company.key,
        kind="op",
        payee_name=benef_name,
        payee_iban=benef_iban,
        payee_bic=benef_bic,
        payee_fiscal_code=fiscal,
        amount=figures,
        details=details,
        issue_date=issue,
        doc_ref=f"OP {op_no} {issue:%d/%m/%Y}",
        source_path=source,
        source_sha=source_sha,
    )
    return res


def _op_amount(lines: list[str], errs: list[str]) -> tuple[str, Decimal | None]:
    figures = words = None
    op_no = ""
    for i, ln in enumerate(lines):
        if "ORDIN DE PLATA" in ln:
            ch = _chunks(ln)
            # ['ORDIN DE PLATA', '1', '1 681']
            if len(ch) >= 3 and re.fullmatch(r"\d+", ch[1]):
                op_no = ch[1]
                if re.fullmatch(r"\d{1,3}( \d{3})*([.,]\d{2})?", ch[2]):
                    figures = parse_amount(ch[2].replace(",", "."))
            _, nxt = _next_nonblank(lines, i)
            if re.fullmatch(r"[a-z]+", nxt):
                words = nxt
            break
    if figures is None:
        errs.append("amount in figures not found")
    if words is None:
        errs.append("amount in words not found")
    if figures is not None and words is not None:
        try:
            w = ro_words_to_int(words)
            if Decimal(w) != figures:
                errs.append(f"amount mismatch: figures {figures} != words {words!r} ({w})")
        except WordsError as e:
            errs.append(f"amount words unparseable: {e}")

    return op_no, figures


def _op_following(lines: list[str], marker: str, errs: list[str], missing: str, *, prefix: bool = False) -> str:
    value = ""
    for pos, line in enumerate(lines):
        if line.strip().startswith(marker) if prefix else marker in line:
            _, following = _next_nonblank(lines, pos)
            value = _chunks(following)[0] if following else ""
            break
    if not value:
        errs.append(missing)
    return value


def _op_company(payer_name: str, payer_cui: str, payer_iban: str, errs: list[str]) -> companies.Company | None:
    company = companies.company_by_cui(payer_cui)
    if company is None:
        errs.append(f"payer CUI {payer_cui!r} is not one of the configured companies")
    else:
        if not company.named_in(payer_name):
            errs.append(f"payer name {payer_name!r} does not match CUI {payer_cui} ({company.legal_name})")
        if payer_iban and payer_iban not in company.own_ibans:
            errs.append(f"payer IBAN {payer_iban[-4:]} is not a known {company.key} account")
    return company


def _op_treasury(benef_iban: str, benef_bic: str, benef_cui: str, payer_cui: str, errs: list[str]) -> str:
    fiscal = ""
    if benef_iban and benef_iban[4:8] == "TREZ":
        if benef_bic != banks.TREASURY_BIC:
            errs.append(f"treasury IBAN but BIC is {benef_bic!r}, expected {banks.TREASURY_BIC}")
        if benef_cui and benef_cui != payer_cui:
            errs.append(f"beneficiary fiscal code {benef_cui} != payer CUI {payer_cui}")
        fiscal = payer_cui
    elif benef_iban:
        errs.append(f"beneficiary IBAN {benef_iban[-4:]} is not a treasury (TREZ) account; OPs must pay the treasury")

    return fiscal


def parse_payment_pdf(path: str | Path) -> ParseResult:
    path = Path(path)
    text = pdf_text(path)
    digest = sha256(path)
    if is_op(text):
        return parse_op_text(text, str(path), digest)
    return ParseResult(source=str(path), errors=["not a tax payment order"])
