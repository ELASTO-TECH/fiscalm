"""PDF history and its separate review queue. Never payment evidence or tracked debt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date, datetime
from decimal import Decimal

from fiscalm import companies
from fiscalm.efactura.inbox import Invoice, tax_token
from fiscalm.ledger import Ledger, finance_home, is_live, now
from fiscalm.mail import invoices as reader


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
    total = reader.pdf_total(L, doc, allow_ai=is_live()) if not doc.refused else None
    name, cui = reader.pdf_party(doc.text, "buyer" if role == "seller" else "seller") if role else ("", "")
    return {
        "direction": {"seller": "issued", "buyer": "received"}.get(role, ""),
        "number": doc.number,
        "issue_date": doc.date,
        "total": total[0] if total else "",
        "currency": total[1] if total else "",
        "party_name": name,
        "party_tax_id": tax_token(cui),
        "reason": doc.refused,
    }


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


def _archive_fields(text: str, co: companies.Company) -> dict:
    """Read the export's two explicit party columns, never a filename's claims."""
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*Furnizor\s{2,}Client\s*", line)]
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
    number = re.search(r"^\s*Factura\s+(.+?)\s*$", lines[0], re.I)
    if not number:
        number = re.match(r"^\s*(.+?)\s{2,}Data Scadenta:?", lines[1], re.I)
    day = re.search(r"Data emitere:?\s*(\d{2}\.\d{2}\.\d{4})", text)
    totals = re.findall(r"^\s*TOTAL:?\s+([0-9][0-9 .,]*)\s+(LEI|RON|EUR)\s*$", text, re.M | re.I)
    if not number or not re.search(r"\d", number[1]) or len(number[1]) > 100 or not day or len(totals) != 1:
        return {}
    value, currency = totals[0]
    value = value.strip().replace(" ", "")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
        return {}
    try:
        issue = datetime.strptime(day[1], "%d.%m.%Y").date().isoformat()
    except ValueError:
        return {}
    party = 1 if ours[0] else 0
    return dict(
        direction="issued" if ours[0] else "received",
        number=number[1].strip(),
        issue_date=issue,
        total=f"{Decimal(value):.2f}",
        currency="RON" if currency.upper() == "LEI" else currency.upper(),
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
        and data.get("number")
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
    party = "client_cui" if table == "issued" else "supplier_tax_id"
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
