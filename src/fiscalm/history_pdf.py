"""PDF history and its separate review queue. Never payment evidence or tracked debt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date, datetime
from decimal import Decimal

from fiscalm import ai_extract, companies
from fiscalm.efactura.inbox import Invoice, tax_token
from fiscalm.ledger import Ledger, finance_home, now
from fiscalm.mail import invoices as reader
from fiscalm.payments.engine import pay_lock
from fiscalm.pdftext import first_page_png


class ReviewError(ValueError):
    pass


def fields(L: Ledger, raw: bytes, company: str) -> dict:
    doc = reader.read_pdf(raw, allow_ro_supplier=True, strict_fields=True)
    archive = _archive_fields(doc.text, companies.get(company, L=L))
    if archive and not doc.refused and not re.search(r"\bseria\b", doc.text, re.I):
        return archive
    if re.search(r"\bseria\b", doc.text, re.I):
        doc.number = ""  # separately printed series needs explicit review
    role = doc.roles.get(company, "") if doc.firms == [company] else ""
    total = doc.totals[0] if len(doc.totals) == 1 and not doc.refused else None
    name, cui = reader.pdf_party(doc.text, "buyer" if role == "seller" else "seller") if role else ("", "")
    rules = {
        "direction": {"seller": "issued", "buyer": "received"}.get(role, ""),
        "number": doc.number,
        "issue_date": doc.date,
        "total": total[0] if total else "",
        "currency": total[1] if total else "",
        "party_name": name,
        "party_tax_id": tax_token(cui),
        "reason": doc.refused,
    }
    negative = any(Decimal(amount) < 0 for amount, _ in reader.totals(doc.text))
    explicit = reader.PROFORMA.search(doc.text) or reader.CREDIT.search(doc.text) or negative  # whatever the title
    if (valid(rules) and rules["party_tax_id"]) or doc.refused in ("proforma", "credit-note") or explicit:
        return rules
    return _with_ai(L, raw, company, doc, rules)


def _ai_fields(x: ai_extract.Extraction, company: str, text: str) -> dict:
    """The history fields an AI reading gives, the firm's role decided by the engine's match of its CUI or name."""
    way = "received" if x.buyer == company else "issued" if x.seller == company else ""
    name, tax = {"received": (x.seller_name, x.seller_tax_id), "issued": (x.buyer_name, x.buyer_tax_id)}.get(
        way, ("", "")
    )
    tax = tax_token(tax)
    if text and not ai_extract.printed_id(tax, text):
        tax = ""  # a partner id the document doesn't print whole
    return {
        "direction": way,
        "number": x.number if plausible_number(x.number) else "",
        "issue_date": x.issue_date,
        "total": x.total if x.currency else "",
        "currency": x.currency if x.total else "",
        "party_name": name if way else "",
        "party_tax_id": tax,
    }


def _with_ai(L: Ledger, raw: bytes, company: str, doc: reader.Doc, rules: dict) -> dict:
    """The rules' fields completed by one checked AI reading (fiscalm.ai_extract), in Sandbox and Live: reading a
    local document has no outward effect. A confident reading of an invoice that agrees with every field the rules
    found files without review; anything else waits in review with the AI's fields filled in."""
    image = None
    if not doc.text.strip():
        png = first_page_png(raw) if raw.startswith(b"%PDF-") and ai_extract.ready(L) else None
        image = ("image/png", png) if png else None
    x = ai_extract.extract(L, [company], text=doc.text, image=image) if doc.text.strip() or image else None
    if x is None:
        return rules
    ai = _ai_fields(x, company, doc.text)
    same = {"number": lambda a, b: number_key(a) == number_key(b), "party_name": lambda a, b: True}  # a name's spelling
    agree = all(not rules[k] or not v or same.get(k, str.__eq__)(rules[k], v) for k, v in ai.items())
    # A number or a total the rules saw but couldn't pick (several, a separate series) stays the reviewer's choice.
    seen = reader.numbers(doc.text) or re.search(r"\bseria\b", doc.text, re.I) or ARCHIVE_HEADER.search(doc.text)
    number_open = rules["number"] or not seen
    if doc.totals:
        total_open = rules["total"] or (ai["total"], ai["currency"]) in doc.totals
    else:  # none the rules could read: printed positive with its currency, as for a mailed invoice
        total_open = not doc.text.strip() or reader.printed_total(doc.text, ai["total"] or "0", ai["currency"])
    party = rules["party_tax_id"] or ai["party_tax_id"]  # a partner id printed whole, as an import requires
    party = party and company not in reader.disputed(doc.text, [company])  # never its delivery address
    certain = all((x.confident, not x.rejected, x.doc_type == "invoice", agree, number_open, total_open, party))
    return {k: rules[k] or v for k, v in ai.items()} | {"reason": "" if certain else "unsure"}


def _archive_party(lines: list[str]) -> tuple[str, str] | None:
    names = []
    for line in lines:
        line = line.strip()
        tax = re.fullmatch(r"CUI:\s*((?:RO)?[0-9]+)", line, re.I)
        if tax:
            return (" ".join(names), tax_token(tax[1])) if names else None
        if not line or ":" in line:
            return None
        names.append(line)
    return None


def plausible_number(number: str) -> bool:
    """An invoice number has a digit and is neither a label nor a date (`Data emitere: 06.02.2026`)."""
    return bool(
        re.search(r"\d", number)
        and len(number) <= 100
        and ":" not in number
        and not re.search(r"\bdata\b|\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2}", number, re.I)
    )


def _archive_number(head: list[str]) -> str:
    """The number beside `Factura` or before `Data Scadenta`; else the one series line under a bare heading."""
    if not head:
        return ""
    for found in (
        re.search(r"^\s*Factura\s+(.+?)\s*$", head[0], re.I),
        re.match(r"^\s*(.+?)\s{2,}Data Scadenta:?", head[1] if len(head) > 1 else "", re.I),
    ):
        if found and plausible_number(found[1].strip()):
            return found[1].strip()
    if not re.match(r"\s*Factura\b", head[0], re.I):
        return ""
    series = {m[0].strip() for line in head[1:] if (m := re.match(r"^\s*[A-Z]{1,6}[\s-]*\d+(?=\s{2,}|\s*$)", line))}
    return series.pop() if len(series) == 1 else ""


def _archive_total(text: str) -> tuple[str, str] | None:
    """The one `TOTAL ... LEI|RON|EUR` line, as a two-decimal string and its currency."""
    totals = re.findall(r"^\s*TOTAL:?\s+([0-9][0-9 .,]*)\s+(LEI|RON|EUR)\s*$", text, re.M | re.I)
    if len(totals) != 1:
        return None
    value, currency = totals[0]
    value = value.strip().replace(" ", "")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
        return None
    return f"{Decimal(value):.2f}", "RON" if currency.upper() == "LEI" else currency.upper()


ARCHIVE_HEADER = re.compile(r"^\s*Furnizor\s{2,}Client\s*$", re.M)  # an FGO/SmartBill export's party columns


def _archive_fields(text: str, co: companies.Company) -> dict:
    """Read the export's two explicit party columns, never a filename's claims."""
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines) if ARCHIVE_HEADER.fullmatch(line)]
    if len(headers) != 1:
        return {}
    i = headers[0]
    if i + 2 >= len(lines):
        return {}
    split = lines[i].index("Client") - 2
    if any(line[split - 1 : split + 1].strip() for line in lines[i + 1 : i + 9]):
        return {}
    parties = [
        _archive_party([line[:split] for line in lines[i + 1 : i + 9]]),
        _archive_party([line[split:] for line in lines[i + 1 : i + 9]]),
    ]
    if not all(parties):
        return {}
    names, ids = zip(*parties, strict=True)
    ours = [tax_token(co.cui) == value for value in ids]
    if sum(ours) != 1:
        return {}
    number = _archive_number(lines[:i])
    day = re.search(r"Data emitere:?\s*(\d{2}\.\d{2}\.\d{4})", text)
    total = _archive_total(text)
    if not number or not day or not total:
        return {}
    try:
        issue = datetime.strptime(day[1], "%d.%m.%Y").date().isoformat()
    except ValueError:
        return {}
    party = 1 if ours[0] else 0
    return dict(
        direction="issued" if ours[0] else "received",
        number=number,
        issue_date=issue,
        total=total[0],
        currency=total[1],
        party_name=names[party],
        party_tax_id=ids[party],
        reason="",
    )


def valid(data: dict) -> bool:
    if not all(
        isinstance(data.get(k), str)
        for k in ("direction", "number", "issue_date", "total", "currency", "party_name", "party_tax_id")
    ):
        return False
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["issue_date"]):
            return False
        date.fromisoformat(data["issue_date"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        data.get("direction") in ("issued", "received")
        and plausible_number(data["number"])
        and re.fullmatch(r"\d{1,12}\.\d{2}", data.get("total", ""))
        and Decimal(data["total"]) > 0
        and re.fullmatch(r"[A-Z]{3}", data.get("currency", ""))
        and data.get("party_name")
        and not data.get("reason")
    )


def number_key(number: str | None) -> tuple[str, str]:
    """Preserve the series and every digit, including invoice numbers below 100."""
    compact = re.sub(r"[\s./_-]+", "", (number or "").upper())
    match = re.fullmatch(r"(.*?)([0-9]+)", compact)
    return (match[1], match[2].lstrip("0") or "0") if match else (compact, "")


def tracked(L: Ledger, company: str, data: dict) -> bool:
    table = "issued" if data["direction"] == "issued" else "efactura"
    party = "client_cui" if table == "issued" else "COALESCE(supplier_tax_id,supplier_cui)"
    for r in L.db.execute(
        f"SELECT number,issue_date,{party} AS party FROM {table} WHERE company=? AND status!='duplicate' AND doc_type='invoice'",
        (company,),
    ):
        if (
            number_key(r["number"]) == number_key(data["number"])
            and r["issue_date"] == data["issue_date"]
            and (table == "issued" or tax_token(r["party"]) == data["party_tax_id"])
        ):
            return True
    return False


def _known(L: Ledger, company: str, data: dict) -> bool:
    if tracked(L, company, data):
        return True
    return any(
        number_key(r["number"]) == number_key(data["number"])
        and r["issue_date"] == data["issue_date"]
        and (data["direction"] == "issued" or r["party_tax_id"] == data["party_tax_id"])
        for r in L.db.execute(
            "SELECT number,issue_date,party_tax_id FROM history WHERE company=? AND direction=?",
            (company, data["direction"]),
        )
    )


def file_history(L: Ledger, company: str, sha: str, data: dict, path: str) -> bool:
    if _known(L, company, data):
        return False
    L.db.execute(
        "INSERT INTO history(company,xml_sha,direction,doc_type,number,issue_date,currency,total,"
        "party_name,party_cui,party_tax_id,raw_path,imported_at) VALUES(?,?,?,'invoice',?,?,?,?,?,?,?,?,?)",
        (
            company,
            sha,
            data["direction"],
            data["number"],
            data["issue_date"],
            data["currency"],
            data["total"],
            data["party_name"],
            re.sub(r"\D", "", data["party_tax_id"]),
            data["party_tax_id"],
            path,
            now(),
        ),
    )
    return True


def prepare(L: Ledger, company: str, raw: bytes, name: str) -> dict:
    """Slow extraction, AI and file I/O happen before the import takes write locks."""
    sha = hashlib.sha256(raw).hexdigest()
    data = fields(L, raw, company)
    path = reader._store(("history", company, "pdf"), sha, "pdf", raw)
    return dict(sha=sha, name=name, data=data, path=str(path))


def ingest(L: Ledger, company: str, prepared: dict) -> tuple[str, str]:
    sha, name, data, path = (prepared[k] for k in ("sha", "name", "data", "path"))
    with L.tx():
        if L.db.execute("SELECT 1 FROM history_documents WHERE company=? AND sha=?", (company, sha)).fetchone():
            return "duplicate", ""
        certain = valid(data) and bool(data["party_tax_id"])
        if certain:
            matches = [
                r["xml_sha"]
                for r in L.db.execute(
                    "SELECT * FROM history WHERE company=? AND direction=? AND issue_date=? AND currency=? AND total=?",
                    (company, data["direction"], data["issue_date"], data["currency"], data["total"]),
                )
                if number_key(r["number"]) == number_key(data["number"]) and r["party_tax_id"] == data["party_tax_id"]
            ]
            if len(matches) == 1:
                data = data | {"xml_sha": matches[0]}

        added = file_history(L, company, sha, data, path) if certain else False
        status = "accepted" if certain else "pending"
        L.db.execute(
            "INSERT INTO history_documents(company,sha,name,data,status) VALUES(?,?,?,?,?)",
            (company, sha, name, json.dumps(data), status),
        )
    return ("imported" if added else "duplicate" if certain else "review"), data["direction"]


def attach(L: Ledger, company: str, xml_sha: str, raw: bytes) -> None:
    """A companion PDF is evidence to open, never another invoice or payment proof."""
    sha = hashlib.sha256(raw).hexdigest()
    reader._store(("history", company, "pdf"), sha, "pdf", raw)
    L.db.execute(
        "INSERT INTO history_documents(company,sha,name,data,status) VALUES(?,?,?,?, 'accepted') "
        "ON CONFLICT(company,sha) DO UPDATE SET data=excluded.data,status='accepted'",
        (company, sha, "document.pdf", json.dumps({"xml_sha": xml_sha})),
    )


def pending(L: Ledger, company: str) -> list[dict]:
    return [
        {"sha": r["sha"], "name": r["name"], **json.loads(r["data"])}
        for r in L.db.execute(
            "SELECT sha,name,data FROM history_documents WHERE company=? AND status='pending' ORDER BY rowid",
            (company,),
        )
    ]


def document(L: Ledger, company: str, sha: str) -> bytes | None:
    if company not in companies.all(L) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None
    if not L.db.execute("SELECT 1 FROM history_documents WHERE company=? AND sha=?", (company, sha)).fetchone():
        return None
    fds = []
    try:
        fds.append(os.open(finance_home().resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in ("history", company, "pdf"):
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        fd = os.open(sha + ".pdf", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            raw = f.read(12 * 1024 * 1024 + 1)
        return raw if len(raw) <= 12 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == sha else None
    except OSError:
        return None
    finally:
        for fd in fds:
            os.close(fd)


def answer(L: Ledger, company: str, sha: str, data: dict, *, target: str | None = None) -> None:
    """Guarded one-time review; manual fields claim history, never prove payment."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ReviewError("Documentul nu mai așteaptă verificarea.")
    target = target or company
    if target not in companies.all(L):
        raise ReviewError("Firma nu mai este disponibilă.")
    data = data | {
        "party_tax_id": tax_token(data.get("party_tax_id", "")),
        "total": data.get("total", "").replace(",", "."),
    }
    discard = data.get("direction") == "none"
    if not discard and (not valid(data) or len(data.get("number", "")) > 100 or len(data.get("party_name", "")) > 200):
        raise ReviewError("Sunt necesare rolul firmei, numărul, data, totalul, moneda și partenerul.")
    if not discard and document(L, company, sha) is None:
        raise ReviewError("Documentul nu mai poate fi citit. Arhiva trebuie importată din nou.")
    if not discard and target != company:
        reader._store(("history", target, "pdf"), sha, "pdf", document(L, company, sha))
    with L.tx() as db:
        if not db.execute(
            "UPDATE history_documents SET status=?,data=? WHERE company=? AND sha=? AND status='pending'",
            ("dismissed" if discard else "accepted", json.dumps(data), company, sha),
        ).rowcount:
            raise ReviewError("Documentul nu mai așteaptă verificarea.")
        if not discard:
            if target != company:
                db.execute(
                    "INSERT OR IGNORE INTO history_documents(company,sha,name,data,status) "
                    "SELECT ?,sha,name,?, 'accepted' FROM history_documents WHERE company=? AND sha=?",
                    (target, json.dumps(data), company, sha),
                )
            path = finance_home().resolve() / "history" / target / "pdf" / (sha + ".pdf")
            file_history(L, target, sha, data, str(path))


def correct(L: Ledger, company: str, sha: str, number: str | None) -> str:
    """Owner fix for an accepted PDF that filed this firm's history row: a new number, or None to remove it.

    One transaction replaces the row, so matching never sees a gap and no review answer can land later. A corrected
    invoice already known (XML, issued, e-Factura) files nothing, as on import. Refused for an XML invoice or
    companion, a PDF that filed no row here, an invalid number and a row the firm now tracks."""
    with pay_lock(), L.tx() as db:  # like an import: never between a matcher's read and its write
        row = db.execute(
            "SELECT h.*,d.data FROM history h "
            "JOIN history_documents d ON d.company=h.company AND d.sha=h.xml_sha "
            "WHERE h.company=? AND h.xml_sha=? AND d.status='accepted'",
            (company, sha),
        ).fetchone()
        if not row or "xml_sha" in json.loads(row["data"]):
            raise ReviewError("Documentul nu are o factură în istoric.")
        if tracked(L, company, dict(row)):
            raise ReviewError("Factura este urmărită. Corectarea nu se face din istoric.")
        keys = ("direction", "issue_date", "currency", "total", "party_name", "party_tax_id")
        data = {k: row[k] or "" for k in keys} | {"number": (number or "").strip(), "reason": ""}
        if number is not None and not (valid(data) and data["party_tax_id"]):  # certain, as on import
            raise ReviewError("Numărul nu este valid sau partenerul nu are CUI.")
        db.execute("DELETE FROM history WHERE company=? AND xml_sha=?", (company, sha))
        db.execute(
            "UPDATE history_documents SET status=?,data=? WHERE company=? AND sha=? AND status='accepted'",
            ("dismissed" if number is None else "accepted", json.dumps(data), company, sha),
        )
        if number is None:
            return "removed"
        return "corrected" if file_history(L, company, sha, data, row["raw_path"]) else "duplicate"


def supersede(L: Ledger, company: str, way: str, inv: Invoice) -> None:
    """XML replaces a PDF projection; the original PDF remains attached in history_documents."""
    for row in L.db.execute(
        "SELECT h.xml_sha,h.number,h.issue_date,h.party_tax_id FROM history h JOIN history_documents d "
        "ON h.company=d.company AND h.xml_sha=d.sha WHERE h.company=? AND h.direction=?",
        (company, way),
    ).fetchall():
        if (
            number_key(row["number"]) == number_key(inv.number)
            and row["issue_date"] == inv.issue_date
            and (way == "issued" or row["party_tax_id"] == inv.supplier_tax_id)
        ):
            L.db.execute("DELETE FROM history WHERE company=? AND xml_sha=?", (company, row["xml_sha"]))
