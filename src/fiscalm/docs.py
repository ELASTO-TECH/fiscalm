"""Receipts/invoices for the accountant pack: inspect evidence, enforce ownership, flag issues.

The LLM (skill) decides *which* mail attachments are receipts; this module
refuses anything that does not pass the ownership rule and computes issues
(VAT id, paying card) deterministically.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fiscalm import companies
from fiscalm.ledger import Ledger, finance_home
from fiscalm.pdftext import pdf_text, sha256


@dataclass
class Evidence:
    path: str
    mentions: list[str] = field(default_factory=list)  # company keys named/tax-id'd on the doc
    tax_ids: list[str] = field(default_factory=list)  # our tax ids found
    other_ro_tax_ids: list[str] = field(default_factory=list)  # RO ids that are not ours
    card_last4: str = ""
    dates: list[str] = field(default_factory=list)
    is_proforma: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


_MONTHS = {
    m: i
    for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)
}


def _dates(text: str) -> list[str]:
    out: set[str] = set()
    for y, m, d in re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        out.add(f"{y}-{m}-{d}")
    for d, m, y in re.findall(r"\b(\d{2})[./](\d{2})[./](20\d{2})\b", text):
        if int(m) <= 12:
            out.add(f"{y}-{m}-{d}")
    for mon, d, y in re.findall(r"\b([A-Z][a-z]{2,8})\.? (\d{1,2}), (20\d{2})\b", text):
        if mon[:3].lower() in _MONTHS:
            out.add(f"{y}-{_MONTHS[mon[:3].lower()]:02d}-{int(d):02d}")
    return sorted(out)


def _card(text: str) -> str:
    pats = [
        r"\*{2,}\s*\*{0,4}\s*(\d{4})\b",
        r"ending (?:in )?(\d{4})\b",
        r"\bVI\s+(\d{4})\b",
        r"\b[Xx•·]{2,}\s*(\d{4})\b",
        r"\b\d\*{3}\s+\*{4}\s+\*{4}\s+(\d{4})\b",
    ]
    for p in pats:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ""


def inspect_text(text: str, path: str = "") -> Evidence:
    ev = Evidence(path=path)
    up = text.upper()
    compact = re.sub(r"\s+", "", up)
    for co in companies.all().values():
        hit = co.named_in(text)
        for tid in sorted(co.tax_ids()):
            digits = re.sub(r"\D", "", tid)
            if digits in compact:
                ev.tax_ids.append(tid)
                hit = True
        if hit:
            ev.mentions.append(co.key)
    ours = {re.sub(r"\D", "", t) for co in companies.all().values() for t in co.tax_ids()}
    for m in re.finditer(r"(?<![A-Z])RO\s?([1-9]\d{5,9})\b", up):
        if m.group(1) not in ours:
            ev.other_ro_tax_ids.append("RO" + m.group(1))
    ev.other_ro_tax_ids = sorted(set(ev.other_ro_tax_ids))
    ev.card_last4 = _card(text)
    ev.dates = _dates(text)
    ev.is_proforma = bool(re.search(r"\bPRO\s?-?FORMA\b", up))
    return ev


def inspect(path: str | Path) -> Evidence:
    return inspect_text(pdf_text(path), str(path))


def card_owners(db) -> dict[tuple[str, str], str]:
    return {(r[0], r[1]): r[2] for r in db.execute("SELECT company,card,owner FROM card_owners")}


def issues_for(company: str, ev: Evidence, doc_date: date | None, owners: dict[tuple[str, str], str]) -> list[str]:
    """Deterministic issues worth telling the accountant/the user about. `owners`: explicit card ownership."""
    co = companies.get(company)
    issues: list[str] = []
    if ev.card_last4:
        if owners.get((company, ev.card_last4)) == "personal":
            issues.append(f"paid with the user's personal card ...{ev.card_last4}, not a {co.key} card")
        elif owner := owners.get((company, ev.card_last4)):
            if owner != co.key:
                issues.append(f"{co.key} invoice paid with {owner} card ...{ev.card_last4}")
        else:
            issues.append(f"unknown card owner ...{ev.card_last4}; awaiting confirmation")
    if len(ev.mentions) > 1:
        issues.append(f"document names both {' and '.join(ev.mentions)}; filed under {co.key}")
    return issues


class DocRefused(ValueError):
    pass


def check_ownership(company: str, ev: Evidence, allow_ro_supplier: bool = False, supporting: str = "") -> None:
    """Ownership rule: the company's name or tax id must be on the document.

    `supporting` names why a document without the company on it still backs a
    company-card payment (travel tickets, dev accounts in the user's name)."""
    co = companies.get(company)
    if co.key not in ev.mentions and not supporting:
        raise DocRefused(f"{Path(ev.path).name}: {co.legal_name} name/tax id not on document (ownership rule)")
    if ev.is_proforma:
        raise DocRefused(f"{Path(ev.path).name}: proforma, not a fiscal invoice")
    if ev.other_ro_tax_ids and not allow_ro_supplier:
        raise DocRefused(
            f"{Path(ev.path).name}: Romanian tax id(s) {', '.join(ev.other_ro_tax_ids)} found, likely an "
            "e-Factura supplier (already with the accountant). Use --allow-ro-supplier only if the user confirms."
        )


def add_receipt(
    ledger: Ledger,
    company: str,
    src: str | Path,
    *,
    month: str | None = None,
    vendor: str = "",
    doc_date: str | None = None,
    amount: str = "",
    currency: str = "",
    source: str = "",
    allow_ro_supplier: bool = False,
    supporting: str = "",
    home: Path | None = None,
) -> dict:
    src = Path(src)
    co = companies.get(company)
    ev = inspect(src)
    check_ownership(co.key, ev, allow_ro_supplier, supporting)
    digest = sha256(src)
    existing = ledger.document(digest)
    if existing and not (existing["source"] or "").startswith("upload:"):
        return {
            "status": "duplicate",
            "sha": digest,
            "company": existing["company"],
            "month": existing["month"],
            "sent_at": existing["sent_at"],
        }
    if amount and currency and doc_date:
        dup = ledger.db.execute(
            "SELECT path, sent_at FROM documents WHERE company=? AND amount=? AND currency=? AND doc_date=? "
            "AND kind='receipt' AND sha!=?",  # an upload of these same bytes is taken over below, not a duplicate
            (co.key, amount, currency, doc_date, digest),
        ).fetchone()
        if dup:
            return {"status": "duplicate", "sha": digest, "same_as": dup["path"], "sent_at": dup["sent_at"]}
    ddate = datetime.strptime(doc_date, "%Y-%m-%d").date() if doc_date else None
    if month is None:
        if ddate is None:
            raise DocRefused("need --month or --date to place the document in a month")
        month = ddate.strftime("%Y-%m")
    issues = issues_for(co.key, ev, ddate, card_owners(ledger.db))
    if supporting:
        issues = [*issues, f"supporting document: {supporting}"]
    dest_dir = (home or finance_home()) / co.key / month / "facturi"
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_vendor = re.sub(r"[^A-Za-z0-9.-]+", "-", vendor).strip("-") or "doc"
    dest = dest_dir / f"{doc_date or month}_{safe_vendor}_{src.name}"
    if existing and (existing["source"] or "").startswith("upload:"):
        dest = Path(existing["path"])
    if not dest.exists():
        shutil.copy2(src, dest)
    if not ledger.add_document(
        digest,
        co.key,
        month,
        "receipt",
        str(dest),
        vendor=vendor,
        doc_date=doc_date,
        amount=amount or None,
        currency=currency or None,
        card_last4=ev.card_last4 or None,
        issues=issues,
        source=source or "manual",
    ):  # an upload a person already linked to its card payment keeps that answer (Ledger.add_document)
        kept = ledger.document(digest)
        return {
            "status": "duplicate",
            "sha": digest,
            "company": kept["company"],
            "month": kept["month"],
            "sent_at": kept["sent_at"],
        }
    return {"status": "added", "sha": digest, "company": co.key, "month": month, "path": str(dest), "issues": issues}


CURRENCIES = ("EUR", "USD", "RON", "GBP", "CHF", "HUF", "PLN", "CZK", "SEK", "NOK", "DKK", "BGN", "CAD", "AUD")


def correct(L: Ledger, co: str, sha: str, amount: str, currency: str, doc_date: str, number: str, by: str) -> None:
    """Human metadata, used by ordinary readers next run; never a settlement operation."""
    if not re.fullmatch(r"-?\d{1,12}\.\d{2}", amount) or currency not in CURRENCIES:
        raise ValueError("Introdu suma cu două zecimale și o monedă acceptată.")
    if doc_date and (
        not re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc_date) or date.fromisoformat(doc_date).isoformat() != doc_date
    ):
        raise ValueError("Verifică data documentului.")
    if len(number) > 100 or any(ord(c) < 32 for c in number):
        raise ValueError("Verifică numărul documentului.")
    L.correct_document(co, sha, format(Decimal(amount), ".2f"), currency, doc_date, number, by)
