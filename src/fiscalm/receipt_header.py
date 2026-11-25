"""An uploaded receipt's header, read by the install's AI (ai_extract) and checked before anything uses it.

An upload (Bondrop, Cheltuieli, an install-wide link) arrives as bytes only. The tick reads each one with the AI and
keeps the answer in `receipt_headers`, so a file is read once: a request that brought no answer (an outage, a refusal)
is tried again on later ticks, at most TRIES times. A reading fills the document's date, amount, currency, shop and
card only when it is confident, complete and plausible, is a receipt or an invoice, and names as buyer the upload's own
firm or no buyer at all (a till receipt rarely names one). Anything else leaves the document as it was: unread, out of
the accountant's pack (pack.held_upload), unmatched. A HEIC photo is never read: no provider takes it, and fiscalm has
no decoder; a person names its card payment instead (links.receipt).

The reading is a proposal (AGENTS.md rule 5). It never settles anything: a filled receipt only lets the existing
matchers find its card debit (recon.match_receipts) and lets the pack carry it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path

from fiscalm import ai_extract, companies
from fiscalm.api import receipts
from fiscalm.identity import tax_token
from fiscalm.ledger import Ledger
from fiscalm.mail import invoices
from fiscalm.pdftext import first_page_png
from fiscalm.storage import read_file

BATCH = 3  # readings per tick: each is one AI request
TRIES = 3  # requests per upload that brought no answer before it stays unread
IMAGES = {".jpg": "image/jpeg", ".png": "image/png"}
KINDS = ("receipt", "invoice")  # a pro forma, a credit note or anything else never stands for an expense
OLDEST = timedelta(days=366)  # a receipt dated further back than this before its upload is a misreading


def stored_file(L: Ledger, sha: str) -> tuple[str, bytes] | None:
    """The upload's bytes, read inside its own firm's (or the install-wide) storage root and checked against its id."""
    row = L.db.execute("SELECT company, path FROM documents WHERE sha=? AND source LIKE 'upload:%'", (sha,)).fetchone()
    raw = read_file(row["path"], [receipts.storage_root(row["company"])], receipts.FILE_MAX) if row else None
    return raw if raw and hashlib.sha256(raw[1]).hexdigest() == sha else None


def content(name: str, data: bytes) -> tuple[str, tuple[str, bytes] | None]:
    """What the AI is shown: a PDF's text, else its first page as a picture (a scan); a JPEG or PNG photo as is."""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        text = invoices.pdf_text(data) or ""
        page = None if text.strip() else first_page_png(data, dpi=150)
        return text, ("image/png", page) if page else None
    return "", (IMAGES[suffix], data) if suffix in IMAGES else None


def read(L: Ledger, name: str, data: bytes) -> dict | None:
    """One checked reading: {} when there is nothing to read, None when the request brought no answer."""
    text, image = content(name, data)
    if not text.strip() and image is None:
        return {}
    firms = list(companies.all(L))
    x = ai_extract.extract(L, firms, text=text, image=image)
    if not x:
        return None
    return dataclasses.asdict(x) | {"printed": printed(L, data, text) if text.strip() else {}}


def _labelled_buyer(L: Ledger, text: str) -> str:
    """The firm key of the buyer block the text labels (invoices.pdf_party), "?" for a buyer that is none of the firms,
    "" for no labelled buyer."""
    firms = companies.all(L)
    name, tax_id = invoices.pdf_party(text, "buyer")
    if name or tax_id:
        found = [k for k, co in firms.items() if (tax_token(tax_id) == co.cui if tax_id else co.named_in(name))]
        return found[0] if len(found) == 1 else "?"
    for line in text.splitlines():  # a buyer label followed by a name, without a tax id
        label = invoices.BUYER.search(line)
        rest = line[label.end() :] if label else ""
        if re.search(r"[^\W\d_]{3}", rest):
            found = [k for k, co in firms.items() if co.named_in(rest) or co.tax_id_match(rest)]
            return found[0] if len(found) == 1 else "?"
    return ""


def printed(L: Ledger, data: bytes, text: str) -> dict:
    """What a PDF's own text says, by the mail rules (invoices.read_pdf): the firms it names with their roles, the
    firms it disputes, why it is never an expense (a pro forma, a credit note), its totals, and its labelled buyer."""
    rules = invoices.read_pdf(data, allow_ro_supplier=True)
    named = rules.firms
    never = invoices.PROFORMA.search(text) or invoices.CREDIT.search(text)  # whatever the title says
    return {
        "roles": rules.roles,
        "disputed": invoices.disputed(text, named) if named else [],
        "refused": "not-expense" if never or rules.refused in ("proforma", "credit-note") else "",
        "totals": rules.totals,
        "buyer": _labelled_buyer(L, text),
        "card": rules.card_last4,
        "date": rules.date,
    }


def reliable(header: dict) -> bool:
    return (
        bool(header.get("confident"))
        and not header.get("rejected")
        and header.get("doc_type") in KINDS
        and not disagrees(header)
    )


def contradicted(header: dict, company: str) -> bool:
    """The document's own text says `company` is not its buyer: it labels the firm the seller, labels another firm
    the buyer, or names the firm only on a delivery address or with labels that disagree (invoices.disputed)."""
    printed = header.get("printed") or {}
    roles = printed.get("roles") or {}
    other_buyer = printed.get("buyer", "") not in ("", company)
    return (
        company in (printed.get("disputed") or [])
        or roles.get(company) == "seller"
        or any(k != company and r == "buyer" for k, r in roles.items())
        or other_buyer
    )


def disagrees(header: dict) -> bool:
    """The PDF's text refuses what the reading proposes: a pro forma or a credit note, or totals that don't include the
    reading's total in its currency (a subtotal, another currency)."""
    printed = header.get("printed") or {}
    totals = [tuple(t) for t in printed.get("totals") or []]
    card, day = printed.get("card"), printed.get("date")
    return (
        bool(printed.get("refused"))
        or bool(totals)
        and (header["total"], header["currency"]) not in totals
        or bool(card)
        and header["card_last4"] not in ("", card)  # the AI read another card than the one printed
        or bool(day)
        and header["issue_date"] != day  # a due date or another date than the printed issue date
    )


def fields(header: dict, company: str, uploaded: str) -> dict | None:
    """The document fields a reading may fill for `company`, or None when it must not fill any."""
    if not reliable(header):
        return None
    if header["buyer"] != company and (header["buyer"] or header["buyer_tax_id"] or header["buyer_name"]):
        return None  # billed to another firm, or to a buyer that is none of the firms
    if header["seller"] == company or contradicted(header, company):
        return None  # the firm's own sale, or a document its text says another firm bought
    day, amount, currency = header["issue_date"], header["total"], header["currency"]
    upload_day = date.fromisoformat(uploaded[:10])
    if not (day and amount and currency) or not upload_day - OLDEST <= date.fromisoformat(day) <= upload_day:
        return None
    return {
        "vendor": " ".join(header["seller_name"].split())[:80] or None,
        "doc_date": day,
        "amount": amount,
        "currency": currency,
        "card_last4": header["card_last4"] or (header.get("printed") or {}).get("card") or None,
    }


def apply(L: Ledger, sha: str) -> bool:
    """Fill the document from its stored reading, for the firm that holds it now. Install-wide uploads wait."""
    row = L.db.execute(
        "SELECT d.company, h.header, u.uploaded_at FROM documents d JOIN receipt_headers h USING(sha) "
        "JOIN receipt_uploads u USING(sha) WHERE d.sha=? AND h.header IS NOT NULL",
        (sha,),
    ).fetchone()
    if not row or row["company"] == "ALL":
        return False
    found = fields(json.loads(row["header"]), row["company"], row["uploaded_at"])
    return bool(found) and L.fill_upload(sha, row["company"], found)


def proposed_buyer(L: Ledger, sha: str) -> str:
    """The firm any stored reading names as buyer, reliable or not; "" for none."""
    row = L.db.execute("SELECT header FROM receipt_headers WHERE sha=? AND header IS NOT NULL", (sha,)).fetchone()
    return json.loads(row["header"]).get("buyer", "") if row else ""


def buyer(L: Ledger, sha: str) -> str | None:
    """The firm a reliable stored reading names as buyer, by the engine's own CUI or name match; None otherwise."""
    row = L.db.execute("SELECT header FROM receipt_headers WHERE sha=? AND header IS NOT NULL", (sha,)).fetchone()
    header = json.loads(row["header"]) if row else {}
    found = header["buyer"] if reliable(header) else ""
    return found if found and not contradicted(header, found) else None


def extract_pending(L: Ledger) -> None:
    """Bounded scheduled work. First every stored reading a stopped tick left unapplied (no request); then, with an AI
    integration, the oldest unread uploads, BATCH at a time."""
    for row in L.db.execute(
        "SELECT sha FROM documents d JOIN receipt_headers h USING(sha) WHERE d.company!='ALL' AND d.amount IS NULL "
        "AND d.source LIKE 'upload:%' AND json_extract(h.header, '$.confident')"
    ).fetchall():
        apply(L, row["sha"])
    if not ai_extract.ready(L):
        return
    rows = L.db.execute(
        "SELECT d.sha, COALESCE(h.tries, 0) AS tries FROM documents d JOIN receipt_uploads u USING(sha) "
        "LEFT JOIN receipt_headers h USING(sha) WHERE (h.sha IS NULL OR h.header IS NULL AND h.tries<?) "
        "AND d.kind='receipt' AND d.source LIKE 'upload:%' AND d.amount IS NULL ORDER BY u.uploaded_at LIMIT ?",
        (TRIES, BATCH),
    ).fetchall()
    for row in rows:
        raw = stored_file(L, row["sha"])
        header = read(L, *raw) if raw else {}
        answer = None if header is None else json.dumps(header, ensure_ascii=False)
        if L.save_receipt_header(row["sha"], answer, row["tries"]) and answer:
            apply(L, row["sha"])
