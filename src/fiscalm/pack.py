"""Monthly accountant pack: a frozen ZIP for a human to download and send."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fiscalm import companies
from fiscalm.bank import catalogue as banks, statements
from fiscalm.bank.intake import month_missing
from fiscalm.identity import partner_id
from fiscalm.ledger import Ledger, finance_home, now
from fiscalm.payments.engine import pay_lock
from fiscalm.recon import month_dir

LUNI = [
    "ianuarie",
    "februarie",
    "martie",
    "aprilie",
    "mai",
    "iunie",
    "iulie",
    "august",
    "septembrie",
    "octombrie",
    "noiembrie",
    "decembrie",
]


def partner_of(company: str, d) -> str | None:
    """A document's partner: by its supplier's name only (a receipt carries no tax id, so it never joins a partner
    that has one: when in doubt they stay apart)."""
    return partner_id(company, None, d["vendor"])


def not_mailed(ledger: Ledger, company: str) -> set[str]:
    """The partners whose documents this firm doesn't send the accountant (the tick on the partner's sheet)."""
    return set(json.loads(ledger.get_meta(f"pack_not_mailed:{company}", "[]")))


class PackWaiting(ValueError):
    """The firm's pack snapshot is created and not sent yet: changing what goes in it now would make the snapshot and the
    report disagree, so the tick waits until the snapshot is sent."""


def set_not_mailed(ledger: Ledger, company: str, partner: str, on: bool) -> str:
    """Tick or untick "Nu trimite contabilului" on a partner the firm has a document from; returns its name. Read and
    written in one transaction, so two ticks never lose one another; refused while a snapshot waits (PackWaiting), and
    under the lock composing holds, so a snapshot being created now is waited for and then refuses it too."""
    with pay_lock(), ledger.tx(immediate=True) as db:
        names = [
            d["vendor"] for d in ledger.documents(company=company, kind="receipt") if partner_of(company, d) == partner
        ]
        if not partner or not names:
            raise ValueError("no document from this partner")
        if db.execute("SELECT 1 FROM packs WHERE company=? AND status='prepared'", (company,)).fetchone():
            raise PackWaiting("a pack snapshot waits to be sent")
        held = not_mailed(ledger, company)
        held = held | {partner} if on else held - {partner}
        ledger.set_meta(f"pack_not_mailed:{company}", json.dumps(sorted(held)))
    return names[0]


def pending_docs(ledger: Ledger, company: str, month: str, *, left_out: bool = False):
    """Unsent docs for company up to `month` (late receipts from earlier months ride along). An upload waits until it is
    read or a person names the card payment it is the bon of (`held_upload`); a partner's documents ticked "Nu trimite
    contabilului" stay out (`left_out=True` lists those instead)."""
    held, docs = not_mailed(ledger, company), ledger.documents(company=company, up_to_month=month, unsent_only=True)
    named = ledger.linked_receipts(company, docs)
    # an earlier month's statements ride along only once every account of their banks is known to be there
    months = {d["month"] for d in docs if d["kind"] == "statement"}
    waiting = {m for m in months if month_missing(ledger, company, m) != []}
    return [
        d
        for d in docs
        if not held_upload(d, named)
        and (d["kind"] == "receipt" and partner_of(company, d) in held) == left_out
        and not (d["kind"] == "statement" and d["month"] in waiting)
    ]


def held_upload(d, named: set[str]) -> bool:
    """An uploaded file stays out of the accountant's pack until its date, amount and currency are known: read from
    it for its own firm (receipt_header) or taken from the card payment a person linked it to (links.receipt). The
    bytes alone, a photo nobody could read, prove nothing (maintainer decision, #44, 2026-11-25)."""
    unread = not (d["doc_date"] and d["amount"] and d["currency"])
    return (d["source"] or "").startswith("upload:") and d["sha"] not in named and unread


def ready(ledger: Ledger, company: str, month: str) -> bool:
    """A pack is ready once that month's statement is in, with every account of its banks, and something is unsent."""
    # a CSV is partial (its own days): it never stands for the month's statement
    has_statement = any(
        not str(d["path"]).endswith(".csv") for d in ledger.documents(company=company, month=month, kind="statement")
    )
    complete = has_statement and month_missing(ledger, company, month) == []
    return complete and bool(pending_docs(ledger, company, month))


_NOT_NAME = re.compile(r"[^A-Za-z0-9]+")
_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".xml", ".sta", ".mt940", ".csv", ".txt")
SUMMARY = "rezumat.csv"


def _word(text: str | None, limit: int = 40) -> str:
    """Outside text (a supplier's name, a bank) as part of a file name: ASCII letters and digits joined by "-". No dot,
    no separator, so no `..` and no path can come out of it."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return _NOT_NAME.sub("-", folded).strip("-")[:limit].strip("-")


def archive_name(company: companies.Company, month: str) -> str:
    """The ZIP as the accountant receives it: "<firma>-<luna>.zip"."""
    return f"{_word(company.name) or company.key}-{month}.zip"


def _amount(doc) -> str:
    try:
        value = Decimal(doc["amount"] or "")
    except InvalidOperation:
        return ""
    currency = doc["currency"] or ""
    return f"{abs(value):.2f}{currency if re.fullmatch(r'[A-Z]{3}', currency) else ''}"


def member_name(doc, company: companies.Company, taken: set[str]) -> str:
    """A document's name in the ZIP, built only from allowlisted parts: "2026-10_extras_BT_RON.sta",
    "2026-10-06_cheltuiala_Benzinaria-Exemplu_86.40RON.pdf"; a second one with the same name gets "_2"."""
    suffix = Path(doc["path"]).suffix.lower()
    if doc["kind"] == "statement":
        ibans = set(json.loads(doc["accounts"] or "[]"))
        bank = sorted({b.id.upper() for b in map(banks.for_iban, ibans) if b})
        currencies = sorted({a.currency for a in company.accounts if a.iban in ibans})
        parts = [doc["month"], "extras", *bank, *currencies]
    else:
        day = doc["doc_date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc["doc_date"] or "") else doc["month"]
        parts = [day, "cheltuiala", _word(doc["vendor"]) or "fara-nume", _amount(doc)]
    stem = "_".join(p for p in parts if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", p or "") and ".." not in p)
    extension = suffix if suffix in _EXTENSIONS else ".bin"
    name, n = stem + extension, 1
    while name.lower() in taken or name.lower() == SUMMARY:  # a ZIP opened on macOS or Windows ignores case
        n += 1
        name = f"{stem}_{n}{extension}"
    taken.add(name.lower())
    return name


def summary_csv(rows: list[list[str]]) -> bytes:
    """`rezumat.csv`: one line per transaction, separated by ";" for a Romanian spreadsheet. Every cell has its IBANs and
    card numbers reduced to the last four (a note can hold one); one a spreadsheet would run as a formula starts with an
    apostrophe, so the bank's and the user's text stay text."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    for row in rows:
        cells = [statements.masked(" ".join(c.split())) for c in row]
        writer.writerow(["'" + c if c[:1] in ("=", "+", "-", "@") else c for c in cells])
    return out.getvalue().encode("utf-8-sig")


def _fingerprint(docs: Sequence, rows: Sequence[list[str]]) -> str:
    """What a snapshot was made from: its documents and its summary, exactly as rezumat.csv would hold it."""
    made = hashlib.sha256(
        json.dumps(sorted((d["sha"], d["amount"], d["currency"], d["doc_date"]) for d in docs)).encode()
    )
    made.update(summary_csv(list(rows)))
    return made.hexdigest()


def stale(ledger: Ledger, company: str, month: str, rows: Sequence[list[str]]) -> bool:
    """Whether a prepared, unsent snapshot no longer matches what preparing now would freeze: a document arrived or
    left, or the summary (`rows`, `api.state.pack_summary`) changed. A sent snapshot is what was sent: never stale."""
    current = ledger.pack(company, month)
    if not current or current["status"] != "prepared":
        return False
    made = ledger.get_meta(f"pack_from:{company}:{month}")
    return made != _fingerprint(pending_docs(ledger, company, month), rows)


def build(
    ledger: Ledger,
    company: str,
    month: str,
    summary: Callable[[], Sequence[list[str]]] = lambda: (),
    home: Path | None = None,
) -> dict:
    """Freeze exactly these documents before the human downloads and sends the ZIP, with `summary()` (every transaction
    of the month and what explains it, `api.state.pack_summary`) as rezumat.csv. The summary reads the whole state,
    which takes `pay_lock` itself, so it is read first and the ZIP is frozen only if no other connection wrote to the
    ledger in between (SQLite's data_version); otherwise it is read again, and after three tries nothing is frozen.
    A sent snapshot, or a prepared one that is not `stale`, is returned as it is; a stale one gets a new version in a file
    of its own, so the old version's bytes never change (its download is refused as no longer current)."""
    firm = companies.get(company, L=ledger)
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("Invalid month")
    for _ in range(3):
        seen = ledger.db.execute("PRAGMA data_version").fetchone()[0]
        rows = list(summary())
        if (done := _freeze(ledger, firm, month, home, rows, seen)) is not None:
            return done
    raise ValueError("Dosarul s-a schimbat în timpul pregătirii.")


def _freeze(
    ledger: Ledger, firm: companies.Company, month: str, home: Path | None, rows: list, seen: int
) -> dict | None:
    company = firm.key
    with pay_lock(), ledger.tx():
        if ledger.db.execute("PRAGMA data_version").fetchone()[0] != seen:
            return None  # the summary may not describe what is frozen now
        current = ledger.pack(company, month)
        if current and current["status"] in ("prepared", "sent") and not stale(ledger, company, month, rows):
            return dict(current) | {"version": ledger.get_meta(f"pack_version:{company}:{month}")}
        if not ready(ledger, company, month):
            raise ValueError("Dosarul se pregătește după sosirea extrasului.")
        docs = pending_docs(ledger, company, month)
        folder = month_dir(company, month, home)
        folder.mkdir(parents=True, exist_ok=True)
        root = finance_home().resolve() / company
        if not folder.resolve().is_relative_to(root):
            raise ValueError("Invalid pack storage")
        with TemporaryDirectory(dir=folder) as staging:
            temporary = Path(staging) / "pack.zip"
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                taken: set[str] = set()
                archive.writestr(SUMMARY, summary_csv(rows))
                for doc in docs:
                    source = Path(doc["path"])
                    with source.open("rb") as stream:
                        if not Path(f"/proc/self/fd/{stream.fileno()}").resolve().is_relative_to(root):
                            raise ValueError("Document outside company storage")
                        archive.writestr(member_name(doc, firm, taken), stream.read())
            version = hashlib.sha256(temporary.read_bytes()).hexdigest()
            # each version its own file: a version's download never changes, even when the month is prepared again
            path = folder / f"pack-{company}-{month}-{version[:16]}.zip"
            temporary.replace(path)
        ledger.set_meta(f"pack_shas:{company}:{month}", json.dumps([d["sha"] for d in docs]))
        ledger.set_meta(f"pack_version:{company}:{month}", version)
        ledger.set_meta(f"pack_from:{company}:{month}", _fingerprint(docs, rows))
        ledger.set_pack(company, month, "prepared", str(path))
        return dict(ledger.pack(company, month)) | {"version": version}


def file(ledger: Ledger, company: str, month: str, version: str) -> bytes:
    """Serve only the exact prepared snapshot, from this company's opened storage."""
    current = ledger.pack(company, month)
    if not current or current["status"] not in ("prepared", "sent"):
        raise ValueError("Dosarul nu este pregătit.")
    if not version or ledger.get_meta(f"pack_version:{company}:{month}") != version:
        raise ValueError("Dosarul s-a schimbat.")
    with Path(current["zip_path"]).open("rb") as stream:
        if not Path(f"/proc/self/fd/{stream.fileno()}").resolve().is_relative_to(finance_home().resolve() / company):
            raise ValueError("Invalid pack storage")
        data = stream.read()
    if hashlib.sha256(data).hexdigest() != version:
        raise ValueError("Dosarul s-a schimbat.")
    return data


def mark(row) -> str:
    """The pack's send/undo revision as a page shows it: a digest of the row's note, which every send and undo makes
    unique. Read from the same row as the state it names, so the two never disagree."""
    return hashlib.sha256((row["note"] or "").encode()).hexdigest()[:16] if row else ""


def _shown(ledger: Ledger, row, version: str, shown: str | None) -> bool:
    """The request names this snapshot and, when given, the revision it was shown: a tab opened before a send or an
    undo is refused instead of repeating or undoing a later one."""
    if not version or ledger.get_meta(f"pack_version:{row['company']}:{row['month']}") != version:
        return False
    return shown is None or mark(row) == shown


def mark_sent(ledger: Ledger, company: str, month: str, version: str, shown: str | None = None) -> bool:
    """A human confirms this frozen snapshot; later documents remain unsent."""
    with pay_lock(), ledger.tx() as db:
        current = ledger.pack(company, month)
        if not current or current["status"] != "prepared" or not _shown(ledger, current, version, shown):
            return False
        shas = json.loads(ledger.get_meta(f"pack_shas:{company}:{month}", "[]"))
        if any(not (d := ledger.document(sha)) or d["company"] != company for sha in shas):
            return False
        stamp = now()
        ledger.mark_documents_sent(shas, stamp)
        db.execute(
            # updated_at stays the preparation time, which an undo shows again
            "UPDATE packs SET status='sent',note=? WHERE company=? AND month=? AND status='prepared'",
            (f"sent {stamp} #{uuid4().hex[:12]}", company, month),
        )
        return True


def unmark_sent(ledger: Ledger, company: str, month: str, version: str, shown: str) -> bool:
    """Undo `mark_sent` for this snapshot: it is prepared again and its documents are unsent, except those another sent pack
    of the firm also froze (that ZIP went to the accountant; only a pack's send marks documents sent). A send by the
    earlier writer (no "#" in its note) stamped the send time over the preparation time: that pack shows none."""
    with pay_lock(), ledger.tx() as db:
        current = ledger.pack(company, month)
        note = current["note"] or "" if current else ""
        if not current or current["status"] != "sent" or not note.startswith("sent "):
            return False
        if not _shown(ledger, current, version, shown):
            return False
        reopened = db.execute(
            "UPDATE packs SET status='prepared',note=?,updated_at=CASE WHEN ? THEN updated_at END"
            " WHERE company=? AND month=? AND status='sent' AND note=?",
            (f"reopened #{uuid4().hex[:12]}", " #" in note, company, month, note),
        )
        if reopened.rowcount != 1:
            return False
        covered = {
            sha
            for other in ledger.sent_months(company) - {month}
            for sha in json.loads(ledger.get_meta(f"pack_shas:{company}:{other}", "[]"))
        }
        for sha in set(json.loads(ledger.get_meta(f"pack_shas:{company}:{month}", "[]"))) - covered:
            db.execute(
                "UPDATE documents SET sent_at=NULL, sent_message_date=NULL WHERE sha=? AND company=?", (sha, company)
            )
        return True


def statement_bank(doc) -> str:
    """The bank a stored statement's accounts identify, or a neutral label when no bank is known."""
    found = {b.label for b in map(banks.for_iban, json.loads(doc["accounts"] or "[]")) if b}
    return ", ".join(sorted(found)) if found else "Altă bancă"
