"""A firm's invoices from before fiscalm, brought in as UBL, SAGA XML and PDF documents (docs/history-import.md).

  fiscalm history import CO FILE --from YYYY-MM-DD    one XML, a ZIP of XMLs, or a ZIP of SPV message ZIPs
  fiscalm history list CO                              what was imported, per direction

FILE is untrusted data (AGENTS.md rule 5). Its size, the number of entries and every inflated byte are capped
before anything is parsed; a member name never becomes a path (a name with `..`, an absolute or drive path, a link
or an encrypted member refuses the whole archive); archives nest one level at most (a ZIP of ANAF message ZIPs);
XML with a DTD is refused by `efactura.safe_xml`. What is stored is named by its own sha256.

Direction comes from the firm's tax ids: the firm as supplier means issued, as buyer means received. An invoice
where it is neither (another firm's) or both refuses the whole import, and nothing is written.

The start date is the day from which fiscalm keeps the firm's payments. The first import sets it; every later import
must give the same one. An XML invoice dated from that day on is stored as if e-Factura sync had brought it (`issued` or
`efactura`, open, matched against statements). One dated before it is history (`history` table): listed beside the
tracked invoices (api.state._history) and kept for the series' numbers, never matched, never paid, never reminded.
History says nothing about payment: the XML has no paid state, and another program's "paid" is a claim, not proof
(rules 2 and 3), so it is neither paid nor owed in fiscalm. Sync uses the same start date; only an explicit unpaid claim opens history.

Idempotent: an invoice already in the ledger (the same XML, or the same number, date and kind from the same
supplier, synced or imported) is skipped. The whole import is one transaction.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from fiscalm import companies, history_invoices, history_pdf, history_saga, history_unpaid
from fiscalm.efactura import inbox as efactura
from fiscalm.efactura.credits import credit_number
from fiscalm.efactura.inbox import Invoice, UblError, already_received, parse_ubl, xml_from_zip
from fiscalm.history_invoices import direction
from fiscalm.history_xml import project
from fiscalm.invoicing import issued
from fiscalm.invoicing.engine import NOT_OURS, already_issued, import_id, kind_total
from fiscalm.ledger import Ledger, finance_home
from fiscalm.payments.engine import pay_lock
from fiscalm.settlements.reports import ReportError
from fiscalm.storage import read_file

MAX_FILE = 100 * 1024 * 1024  # the file given: a few years of a small firm's invoices, zipped
MAX_ENTRIES = 20_000  # entries of the outer ZIP, counted before anything is inflated
MAX_INFLATED = 400 * 1024 * 1024  # every byte actually inflated, all members together
MAX_MEMBER = 12 * 1024 * 1024  # one XML (efactura.MAX_XML is 10 MB) or one message ZIP
UNREADABLE_SHOWN = 50
IMPORTED = "history-import"  # upload_id of a tracked row this import stored: where it came from

MESSAGES = {
    "unpaid_refused": "{reason}",
    "bad_unpaid": "Unsupported unpaid report; use SmartBill XLSX Facturi neincasate.",
    "too_big": "The file is too large (100 MB, or 400 MB once unzipped, at most): import it one year at a time.",
    "unsupported_file": "The file is not one fiscalm reads: an e-Factura or SAGA XML, a PDF, a ZIP of these or of "
    "SPV messages, or a SmartBill unpaid XLSX.",
    "bad_zip": "The ZIP cannot be read: export it again from the other program or from SPV.",
    "too_many": f"The ZIP has more than {MAX_ENTRIES} entries: import it one year at a time.",
    "unsafe_zip": "The ZIP has an encrypted member, a link, a name used twice or a name that leaves its folder; "
    "nothing was imported.",
    "no_invoices": "No e-Factura invoice was found in the file.",
    "other_firm": "{n} invoice(s), e.g. {number}, are not this firm's: import each firm's export into that firm.",
    "bad_start": "The start date must be a date (YYYY-MM-DD).",
    "future_start": "The start date cannot be after today.",
    "start_differs": "This firm's start date is already {start}; give the same date.",
    "storage": "The firm's history folder is not where it should be (a link?); nothing was imported.",
}


# The same refusals as fiscalm says them (impersonal, docs/openapi.json "History upload").
MESSAGES_RO = {
    "unpaid_refused": "{reason}",
    "bad_unpaid": "Lista nu poate fi citită. Se acceptă XLSX SmartBill Facturi neîncasate, cu Rest de încasat.",
    "too_big": "Fișierul este prea mare: cel mult 100 MB, sau 400 MB dezarhivat. Importă câte un an.",
    "unsupported_file": "Fișierul nu este o factură e-Factura (XML) sau o arhivă ZIP cu facturi e-Factura.",
    "bad_zip": "Arhiva ZIP nu poate fi citită. Exportă din nou din programul anterior sau din SPV.",
    "too_many": "Arhiva are prea multe fișiere. Importă câte un an.",
    "unsafe_zip": "Arhiva are un fișier criptat, o legătură sau un nume nepermis. Nu s-a importat nimic.",
    "no_invoices": "Fișierul nu conține nicio factură e-Factura.",
    "other_firm": "Unele facturi nu sunt ale acestei firme, de exemplu {number}. Nu s-a importat nimic. "
    "Importă separat exportul fiecărei firme, la firma respectivă.",
    "bad_start": "Data de început lipsește sau nu este o dată.",
    "future_start": "Data de început nu poate fi după ziua de azi.",
    "start_differs": "Data de început a firmei este deja {start_ro}. Importă cu aceeași dată.",
    "storage": "Dosarul cu istoricul firmei nu este unde trebuie. Nu s-a importat nimic.",
}


def _n(n: int, one: str, many: str) -> str:
    """A count before a noun, the app's one plural rule (format.ts nOf): `one` as given, 2-19 "3 facturi", 20 and up
    "20 de facturi" (101-119 past a hundred take no "de"). Only ever before a noun, never an adjective or a verb."""
    return one if n == 1 else f"{n} {'de ' if n % 100 == 0 or n % 100 >= 20 else ''}{many}"


def message_ro(e: Refused) -> str:
    facts = dict(e.facts)
    if "start" in facts:
        y, m, d = str(facts["start"]).split("-")
        facts["start_ro"] = f"{d}.{m}.{y}"
    return MESSAGES_RO[e.code].format(**facts)


def done_ro(res: Result) -> str:
    """What an import did, in one sentence for the toast."""
    old, tracked = res.history_issued + res.history_received, res.issued + res.received
    parts = [
        *(
            [
                _n(
                    res.records,
                    "o înregistrare din programul anterior păstrată",
                    "înregistrări din programul anterior păstrate",
                )
            ]
            if res.records
            else []
        ),
        *([_n(res.review, "un document de verificat", "documente de verificat")] if res.review else []),
        *([f"{_n(old, 'o factură', 'facturi')} în istoric"] if old else []),
        *([_n(tracked, "o factură urmărită", "facturi urmărite")] if tracked else []),
        *([f"{_n(res.duplicates, 'o factură era', 'facturi erau')} deja în fiscalm"] if res.duplicates else []),
        *(
            [f"{_n(res.unreadable_count, 'un fișier nu s-a', 'fișiere nu s-au')} putut citi"]
            if res.unreadable_count
            else []
        ),
    ]
    prefix = (
        "Importul s-a încheiat: "
        if res.records
        else "Facturile au fost importate: "
        if old or tracked
        else "Nicio factură nouă: "
    )
    return prefix + ", ".join(parts) + "." if parts else "Nicio înregistrare nouă."


def summary(L: Ledger, company: str) -> dict:
    """The firm's history as Setări shows it: its start date, the oldest statement day and how many invoices count."""
    counted = rows(L, company)
    first = L.db.execute("SELECT MIN(day) FROM statement_txs WHERE company=?", (company,)).fetchone()[0]
    return {
        "start": start_date(L, company),
        "first_statement": first,  # the oldest statement day: the app proposes the start date from it
        "issued": sum(r["direction"] == "issued" for r in counted),
        "received": sum(r["direction"] == "received" for r in counted),
    }


class Refused(ValueError):
    """The whole file is refused and nothing is written; `code` names why (MESSAGES), for the person importing it."""

    def __init__(self, code: str, **facts: object):
        super().__init__(MESSAGES[code].format(**facts))
        self.code, self.facts = code, facts


@dataclass
class Found:
    invoice: Invoice | None
    sha: str  # sha256 of the invoice XML
    raw: bytes  # a ZIP holding the XML: the bytes given (a message ZIP) or one fiscalm wrapped
    name: str  # the member's name, shown only (never a path)
    records: list[dict] = field(default_factory=list)
    document: bytes | None = None


@dataclass
class Result:
    company: str
    start: str
    issued: int = 0  # tracked: stored as if e-Factura sync had brought them
    received: int = 0
    history_issued: int = 0
    history_received: int = 0
    duplicates: int = 0
    records: int = 0
    review: int = 0
    unreadable_count: int = 0
    unreadable: list[str] = field(default_factory=list)  # "name: why", the first UNREADABLE_SHOWN

    def as_dict(self) -> dict:
        return dict(self.__dict__)


# ------------------------------------------------------------------ read the file


def _shown(name: str) -> str:
    return re.sub(r"[^\w .,()+-]", "_", name)[-80:]


def _safe_name(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    parts = re.split(r"[/\\]", name)
    return not (
        not name
        or "\x00" in name
        or name.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", name)
        or ".." in parts
        or info.flag_bits & 0x1
        or stat.S_ISLNK(info.external_attr >> 16)
    )


def _check_names(infos: list[zipfile.ZipInfo]) -> None:
    names = [i.filename for i in infos]
    if len(set(names)) != len(names) or not all(_safe_name(i) for i in infos):
        raise UblError("unsafe ZIP")


def _wrap(xml: bytes, sha: str) -> bytes:
    """An XML as a ZIP shaped like ANAF's, so every row's raw file reads the same way (`invoice_from_zip`)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{sha}.xml", xml)
    return buf.getvalue()


def _pdf(data: bytes, name: str) -> Found:
    if len(data) > MAX_MEMBER:
        raise Refused("too_big")
    return Found(None, hashlib.sha256(data).hexdigest(), data, _shown(name))


def _bare(data: bytes) -> tuple[list[Found], list[str]] | None:
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"%PDF-"):
        return [_pdf(data, "document.pdf")], []
    return _one_xml(data, "fisier.xml") if head.startswith(b"<") else None


def _unpaid(data: bytes) -> Found:
    try:
        records = history_unpaid.parse(data)
    except (ReportError, UblError, KeyError, ValueError):
        raise Refused("bad_unpaid") from None
    return Found(None, hashlib.sha256(data).hexdigest(), data, "lista.xlsx", records)


def read(data: bytes) -> tuple[list[Found], list[str]]:
    """Every invoice in `data` and the members that could not be read ("name: why"). Raises Refused for a file
    that is refused as a whole."""
    if len(data) > MAX_FILE:
        raise Refused("too_big")
    if (bare := _bare(data)) is not None:
        return bare
    if not data.startswith(b"PK\x03\x04"):
        raise Refused("unsupported_file")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError):
        raise Refused("bad_zip") from None
    if len(infos) > MAX_ENTRIES:
        raise Refused("too_many")
    try:
        _check_names(infos)
    except UblError:
        raise Refused("unsafe_zip") from None
    if any(i.filename == "xl/workbook.xml" for i in infos):
        return [_unpaid(data)], []
    found, unreadable, inflated = [], [], 0
    companions = {}

    def count(n: int) -> None:
        nonlocal inflated
        inflated += n
        if inflated > MAX_INFLATED:
            raise Refused("too_big")

    for info in infos:
        if info.is_dir():
            continue
        try:
            with z.open(info) as f:
                member = f.read(MAX_MEMBER + 1)  # the header's size can lie: cap what is actually inflated
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, zlib.error):
            unreadable.append(f"{_shown(info.filename)}: unreadable")
            continue
        count(len(member))
        if len(member) > MAX_MEMBER:
            unreadable.append(f"{_shown(info.filename)}: too large")
            continue
        companions[info.filename] = member
        if member.startswith(b"%PDF-"):
            found.append(_pdf(member, info.filename))
            continue
        if member.startswith(b"PK\x03\x04"):  # one ANAF message: the invoice and its signature, nothing nested further
            try:
                xml = xml_from_zip(member, check=_check_names, inflated=count)  # what it inflates counts too
            except UblError as e:
                if str(e) == "unsafe ZIP":
                    raise Refused("unsafe_zip") from None
                unreadable.append(f"{_shown(info.filename)}: {e}")
                continue
            try:
                inv = parse_ubl(xml)
            except UblError as e:
                unreadable.append(f"{_shown(info.filename)}: {e}")
                continue
            found.append(Found(inv, hashlib.sha256(xml).hexdigest(), member, _shown(info.filename)))
        elif member[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
            more, bad = _one_xml(member, info.filename)
            found += more
            unreadable += bad
    return _companions(found, companions), unreadable


def _companions(found: list[Found], members: dict[str, bytes]) -> list[Found]:
    """Keep an archive's explicitly paired PDF with its authoritative XML."""
    invoices = {f.sha: f for f in found if f.invoice is not None}
    attached = set()
    for name, raw in members.items():
        if not raw.startswith(b"%PDF-"):
            continue
        xml_name = re.sub(r"_1_([^/]+)\.pdf$", r"_2_\1.xml", name)
        xml = members.get(xml_name) if xml_name != name else None
        inv = invoices.get(hashlib.sha256(xml).hexdigest()) if xml else None
        if inv is not None and inv.document is None:
            inv.document = raw
            attached.add(hashlib.sha256(raw).hexdigest())
    return [f for f in found if f.invoice is not None or f.sha not in attached]


def _saga(xml: bytes, name: str) -> list[Found] | None:
    parsed = history_saga.parse(xml)
    if parsed is None:
        return None
    normalized, records = parsed
    original_sha = hashlib.sha256(xml).hexdigest()
    found = []
    for invoice_xml in normalized:
        sha = hashlib.sha256(invoice_xml).hexdigest()
        raw = io.BytesIO(_wrap(invoice_xml, sha))
        with zipfile.ZipFile(raw, "a") as z:
            z.writestr("source.sha256", original_sha)
        found.append(Found(parse_ubl(invoice_xml), sha, raw.getvalue(), _shown(name)))
    if normalized:
        records = [{"kind": "source", "format": "saga"}]
    if records:
        sha = original_sha
        found.append(Found(None, sha, _wrap(xml, sha), _shown(name), records))
    return found


def _one_xml(xml: bytes, name: str) -> tuple[list[Found], list[str]]:
    try:
        if (found := _saga(xml, name)) is not None:
            return found, []
        inv = parse_ubl(xml)
    except UblError as e:
        if "root Signature" in str(e):
            return [], []
        return [], [f"{_shown(name)}: {e}"]
    sha = hashlib.sha256(xml).hexdigest()
    return [Found(inv, sha, _wrap(xml, sha), _shown(name))], []


# ------------------------------------------------------------------ decide


def _known(db, company: str, way: str, inv: Invoice, sha: str) -> bool:
    """Already in the ledger: the same XML, or the same invoice (number, date, kind, and for a received one the
    supplier's full tax id), synced, imported earlier, or earlier in this file."""
    tracked = already_issued(db, company, inv, sha) if way == "issued" else already_received(db, company, inv, sha)
    return tracked or (
        inv.doc_type == "invoice"
        and kind_total(inv)[0] != "credit_note"
        and history_invoices.known(db, company, way, inv, sha)
    )


# ------------------------------------------------------------------ write


def start_date(L: Ledger, company: str) -> str | None:
    return L.get_meta(f"history_start:{company}")


def archive_open(L: Ledger, company: str, way: str, mids: list[str]) -> None:
    """Explicit owner choice for old open rows; never infer settlement or rewrite proof."""
    table = {"issued": "issued", "received": "efactura"}[way]
    with pay_lock(), L.tx():
        for mid in mids:
            row = L.db.execute(f"SELECT * FROM {table} WHERE company=? AND message_id=?", (company, mid)).fetchone()
            if not row or not history_invoices.before_start(L, company, row["issue_date"]):
                raise ValueError("Invoice is missing or is not before the start date; nothing changed.")
            if not L.archive_history_twin(company, way, mid):
                raise ValueError(
                    "Invoice has no identical history copy, is not untouched open, or has payment evidence; nothing changed."
                )
            L.set_meta(f"history_archived:{company}:{way}:{row['xml_sha']}", mid)


def _history_xml(L: Ledger, company: str, row: dict) -> bytes:
    root = finance_home().resolve()
    stored = read_file(row["raw_path"], [root / "history" / company, root / "efactura" / company], MAX_MEMBER)
    if stored is None:
        raise Refused("storage")
    inv, sha = efactura.invoice_from_zip(stored[1])
    if sha != row["xml_sha"] or direction(companies.get(company, L=L), inv) != row["direction"]:
        raise Refused("storage")
    return stored[1]


def _track_credits(L: Ledger, company: str, way: str, target: dict) -> None:
    """Re-read original XMLs: history's projection omits credit validation fields."""
    reference = issued._ref if way == "issued" else credit_number
    for row in rows(L, company, way):
        if (
            row["doc_type"] != "credit_note"
            or reference(row["billing_ref"]) != reference(target["number"])
            or row["party_tax_id"] != target["party_tax_id"]
            or row["currency"] != target["currency"]
        ):
            continue
        raw = _history_xml(L, company, row)
        msg = {"id": import_id(way, row["xml_sha"]), "id_solicitare": IMPORTED}
        (issued.store if way == "issued" else efactura.store_locked)(L, company, msg, raw, track_history=True)


def track(L: Ledger, company: str, sha: str) -> None:
    """Owner claims an old invoice is unpaid, never that it has been paid."""
    with pay_lock(), L.tx():
        row = next((r for r in rows(L, company) if r["xml_sha"] == sha), None)
        if not row or row["pdf"] or row["doc_type"] != "invoice":
            raise ValueError("Choose an XML history invoice.")
        way = row["direction"]
        raw = _history_xml(L, company, row)
        _track_credits(L, company, way, row)
        mid = L.get_meta(f"history_archived:{company}:{way}:{sha}")
        if mid:
            L.reopen_history_twin(company, way, mid, sha)
            return
        msg = {"id": import_id(way, sha), "id_solicitare": IMPORTED}
        store = issued.store if way == "issued" else efactura.store_locked
        store(L, company, msg, raw, track_history=True)


def _history_path(company: str, issue_date: str, sha: str) -> Path:
    """$FISCALM_HOME/history/<CO>/<YYYY-MM>/<sha256>.zip, resolved inside the firm's own folder (a link out of it,
    even into another firm's, refuses)."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha) or not re.fullmatch(r"\d{4}-\d{2}", issue_date[:7]):
        raise Refused("storage")
    root = finance_home().resolve() / "history" / company
    path = root / issue_date[:7] / f"{sha}.zip"
    if not path.resolve().is_relative_to(root):  # a link anywhere under the data folder resolves elsewhere
        raise Refused("storage")
    return path


def _write(path: Path, raw: bytes) -> None:
    """Through a new temporary file (O_EXCL: never an existing name or link), then renamed into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent or path.is_symlink():  # created just now: check it again
        raise Refused("storage")
    if path.exists():
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".import-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _promotion_source(L: Ledger, company: str, row: dict) -> dict:
    """Read/check existing evidence before the write lock; never recover a missing original."""
    if L.db.execute("SELECT 1 FROM history_documents WHERE company=? AND sha=?", (company, row["xml_sha"])).fetchone():
        original = history_pdf.document(L, company, row["xml_sha"])
        if original is None:
            raise Refused("storage")
        co = companies.get(company, L=L)
        inv = Invoice(
            doc_type="invoice",
            number=row["number"],
            issue_date=row["issue_date"],
            due_date=row["due_date"] or "",
            currency=row["currency"],
            total=row["total"],
            supplier_name=co.legal_name,
            supplier_cui=co.cui,
            buyer_name=row["party_name"],
            buyer_cui=row["party_cui"] or "",
            buyer_tax_id=row["party_tax_id"] or "",
        )
        xml = project(inv)
        sha = hashlib.sha256(xml).hexdigest()
        raw = _wrap(xml, sha)
    else:
        root = finance_home().resolve() / "history" / company
        stored = read_file(row["raw_path"], [root, finance_home().resolve() / "efactura" / company], MAX_MEMBER)
        if stored is None:
            raise Refused("storage")
        original = raw = stored[1]
        xml = xml_from_zip(raw, check=_check_names)
        sha = hashlib.sha256(xml).hexdigest()
        if sha != row["xml_sha"]:
            raise Refused("storage")
    inv = parse_ubl(xml)
    if (
        direction(companies.get(company, L=L), inv) != "issued"
        or inv.doc_type != "invoice"
        or inv.number != row["number"]
        or inv.issue_date != row["issue_date"]
        or inv.total != row["total"]
        or inv.currency != row["currency"]
    ):
        raise Refused("storage")
    return dict(raw=raw, xml_sha=sha, invoice=inv, issue_date=inv.issue_date, read_size=len(original) + len(xml))


def _store_promotion(L: Ledger, company: str, source: dict) -> bool:
    inv = source["invoice"]
    _track_credits(
        L, company, "issued", {"number": inv.number, "party_tax_id": inv.buyer_tax_id, "currency": inv.currency}
    )
    archived = L.get_meta(f"history_archived:{company}:issued:{source['xml_sha']}")
    if archived:
        L.reopen_history_twin(company, "issued", archived, source["xml_sha"])
        return True
    if already_issued(L.db, company, source["invoice"], source["xml_sha"]):
        return False
    msg = {
        "id": import_id("issued", source["xml_sha"]),
        "id_solicitare": IMPORTED,
        "data_creare": source["issue_date"].replace("-", ""),
    }
    return issued.store(L, company, msg, source["raw"], track_history=True)["status"] == "open"


def _prepare_reports(L: Ledger, company: str, start: str, found: list[Found]) -> tuple[list[Found], dict]:
    reports = [f for f in found if f.records and f.records[0]["kind"] == history_unpaid.KIND]
    try:
        sources = {
            f.sha: history_unpaid.prepare(
                L, company, f.sha, f.records, lambda r: _promotion_source(L, company, r), MAX_INFLATED
            )
            for f in reports
        }
    except (history_unpaid.UnpaidError, UblError) as exc:
        raise Refused("unpaid_refused", reason=str(exc)) from None
    for f in reports:
        _write(_history_path(company, start, f.sha), f.raw)
    return reports, sources


def import_file(L: Ledger, company: str, data: bytes, start: str, today: date) -> Result:
    """Import every invoice in `data` for `company`, or nothing (Refused). Files are written before the rows
    that name them; a crash between leaves only files a re-import reuses."""
    co = companies.get(company, L=L)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start or ""):
            raise ValueError
        start_day = date.fromisoformat(start)
    except (TypeError, ValueError):
        raise Refused("bad_start") from None
    if start_day > today:
        raise Refused("future_start")
    found, unreadable = read(data)
    if sum(f.invoice is not None for f in found) > MAX_ENTRIES:
        raise Refused("too_many")
    plan = [(f, direction(co, f.invoice)) for f in found if f.invoice is not None]
    others = [f.invoice.number for f, way in plan if way is None]
    if others:  # another firm's invoice refuses all, before any write
        raise Refused("other_firm", n=len(others), number=_shown(others[0]) or "?")
    if not found:
        raise Refused("no_invoices")
    res = Result(company, start, unreadable_count=len(unreadable), unreadable=unreadable[:UNREADABLE_SHOWN])
    pdfs = [history_pdf.prepare(L, company, f.raw, f.name) for f in found if f.invoice is None and not f.records]
    reports, sources = _prepare_reports(L, company, start, found)
    with pay_lock(), L.tx() as db:
        set_start = start_date(L, company)
        if set_start and set_start != start:
            raise Refused("start_differs", start=set_start)
        if not set_start:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (f"history_start:{company}", start))
        for f, way in plan:
            inv = f.invoice
            history_pdf.supersede(L, company, way, inv)
            if f.document is not None:
                existing = db.execute(
                    "SELECT xml_sha FROM history WHERE company=? AND direction=? AND number=? AND issue_date=? "
                    "AND doc_type=? AND (?='issued' OR party_tax_id=?)",
                    (company, way, inv.number, inv.issue_date, kind_total(inv)[0], way, inv.supplier_tax_id),
                ).fetchone()
                history_pdf.attach(L, company, existing[0] if existing else f.sha, f.document)
            if _known(db, company, way, inv, f.sha):
                res.duplicates += 1
                continue
            if not history_invoices.untracked(L, company, inv):
                msg = {
                    "id": import_id(way, f.sha),
                    "id_solicitare": IMPORTED,
                    "data_creare": inv.issue_date.replace("-", ""),
                }
                (issued.store if way == "issued" else efactura.store_locked)(L, company, msg, f.raw)
                setattr(res, way, getattr(res, way) + 1)
                continue
            path = _history_path(company, inv.issue_date, f.sha)
            _write(path, f.raw)
            history_invoices.store(L, company, way, inv, f.sha, path)
            setattr(res, f"history_{way}", getattr(res, f"history_{way}") + 1)
        for f in found:
            if f.records and f.records[0]["kind"] != history_unpaid.KIND:
                _write(_history_path(company, start, f.sha), f.raw)
                res.records += history_saga.store_records(L, company, f.sha, f.records)
        for prepared in pdfs:
            status, way = history_pdf.ingest(L, company, prepared)
            if status == "imported":
                setattr(res, f"history_{way}", getattr(res, f"history_{way}") + 1)
            elif status == "review":
                res.review += 1
            else:
                res.duplicates += 1
        for f in reports:
            try:
                count, promoted = history_unpaid.apply(
                    L, company, f.sha, f.records, sources[f.sha], lambda s: _store_promotion(L, company, s)
                )
            except history_unpaid.UnpaidError as exc:
                raise Refused("unpaid_refused", reason=str(exc)) from None
            res.records += count
            res.issued += promoted
    return res


# ------------------------------------------------------------------ read back


def rows(L: Ledger, company: str, way: str | None = None) -> list[dict]:
    """The firm's history invoices that still count: one that e-Factura sync also brought is tracked there instead."""
    q = (
        "SELECT h.*, EXISTS(SELECT 1 FROM history_documents d WHERE d.company=h.company AND d.sha=h.xml_sha) AS is_pdf "
        " ,d.sha AS linked_pdf FROM history h LEFT JOIN ("
        "SELECT json_extract(data,'$.xml_sha') AS xml_sha,MIN(sha) AS sha FROM history_documents "
        "WHERE company=? AND json_extract(data,'$.xml_sha') IS NOT NULL GROUP BY xml_sha"
        ") d ON d.xml_sha=h.xml_sha WHERE h.company=? AND (? IS NULL OR h.direction=?) AND NOT EXISTS ("
        " SELECT 1 FROM issued i WHERE h.direction='issued' AND i.company=h.company AND i.status!='duplicate'"
        "  AND (i.xml_sha=h.xml_sha OR (i.number=h.number AND i.issue_date=h.issue_date AND i.doc_type=h.doc_type)))"
        " AND NOT EXISTS ("
        " SELECT 1 FROM efactura e WHERE h.direction='received' AND e.company=h.company AND e.status!='duplicate'"
        "  AND (e.xml_sha=h.xml_sha OR ((e.supplier_tax_id=h.party_tax_id OR (e.supplier_tax_id IS NULL"
        "   AND e.supplier_cui=h.party_cui)) AND e.number=h.number"
        "   AND e.issue_date=h.issue_date AND e.doc_type=h.doc_type)))"
        " ORDER BY h.issue_date, h.number"
    )
    out = []
    for record in L.db.execute(q, (company, company, way, way)):
        row = dict(record)
        row["document"] = row.pop("linked_pdf") or (row["xml_sha"] if row["is_pdf"] else None)
        row["pdf"] = bool(row.pop("is_pdf"))  # its document: the PDF it was read from (history_pdf.document)
        if not row["pdf"] or not history_pdf.tracked(L, company, row):
            out.append(row)
    return out


def rivals(L: Ledger, company: str, way: str) -> list[dict]:
    """History invoices as the statement matchers see them: shaped like `issued` / `efactura` rows, id
    "history:<sha>". They are never settled. They stop a payment that names one, or that fits one as well as a
    tracked invoice, from settling the tracked one: an old invoice paid late must not close a new one."""
    out = []
    for r in rows(L, company, way):
        if r["doc_type"] != "invoice":
            continue
        base = {
            "message_id": f"history:{r['xml_sha']}",
            "number": r["number"],
            "issue_date": r["issue_date"],
            "currency": r["currency"],
            "total": r["total"],
            "doc_type": "invoice",
            "status": "history",
            "parse_error": None,
        }
        out.append(
            base
            | (
                {"client_name": r["party_name"], "client_cui": r["party_cui"]}
                if way == "issued"
                else {"supplier_name": r["party_name"], "ibans": r["ibans"] or "[]"}
            )
        )
    return out


# ------------------------------------------------------------------ series


SERIES = re.compile(r"[A-Z][A-Z0-9-]{0,19}")  # invoicing.create_series accepts these codes


def split_number(number: str | None) -> tuple[str, int] | None:
    """('NRD', 42) from 'NRD 0042', 'NRD-0042', 'nrd0042', and fiscalm's own '<code>-<number>' ('AB--42' is code
    'AB-'): the number is the whole run of digits at the end, the code what is before it less one dash or the
    separators. None when the code is none fiscalm could continue (no letter first, spaces inside, too long) or the
    number is 0 or past what a series holds (10**12): never a guess at another split."""
    m = re.fullmatch(r"(.*?)(\d+)", (number or "").strip().upper())
    if not m:
        return None
    prefix, digits = m.group(1).rstrip(), m.group(2).lstrip("0")
    if not digits or len(digits) > 12:  # checked before int(): a few thousand digits is an error there, not a number
        return None
    code = prefix[:-1].rstrip() if prefix.endswith("-") else prefix.rstrip(" /_.#")
    return (code, int(digits)) if SERIES.fullmatch(code) else None


def series_numbers(L: Ledger, company: str) -> dict[str, dict]:
    """Per series code, the highest number the firm's issued invoices outside fiscalm used, and how many rows name it
    (an invoice both in history and synced counts twice):
    the history rows and every issued e-Factura row (synced or imported) the firm is the supplier of. fiscalm's own invoices come
    back from SPV too, always below their series' next number, so they never move it."""
    out: dict[str, dict] = {}
    # For a maximum a number seen twice (history and its synced copy) is harmless: no twin check, one cheap read
    # each, as this runs under the issuing write lock. A sent invoice whose supplier is not the firm never counts.
    numbers = [
        r[0] for r in L.db.execute("SELECT number FROM history WHERE company=? AND direction='issued'", (company,))
    ]
    numbers += [
        r[0]
        for r in L.db.execute(
            "SELECT number FROM issued WHERE company=? AND status!='duplicate' AND parse_error IS NULL "
            "AND COALESCE(issues, '') NOT LIKE ?",
            (company, f"%{NOT_OURS}%"),
        )
    ]
    for number in numbers:
        if parts := split_number(number):
            code, n = parts
            seen = out.setdefault(code, {"max": 0, "count": 0})
            seen["max"], seen["count"] = max(seen["max"], n), seen["count"] + 1
    return out
