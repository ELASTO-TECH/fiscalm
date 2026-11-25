"""Install-wide uploads wait outside every firm's inbox until ownership is proved."""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import suppress
from pathlib import Path

from fiscalm import ai_extract, companies, docs, receipt_header
from fiscalm.api import receipts
from fiscalm.ledger import Ledger, finance_home
from fiscalm.mail import invoices
from fiscalm.receipt_header import TRIES
from fiscalm.storage import read_file


def staged(L: Ledger, sha: str) -> bool:
    """An upload still waiting outside every firm; each firm is offered it until one is assigned."""
    return bool(
        L.db.execute(
            "SELECT 1 FROM documents WHERE sha=? AND company='ALL' AND kind='receipt' AND source LIKE 'upload:%'",
            (sha,),
        ).fetchone()
    )


def pending_file(L: Ledger, sha: str) -> tuple[str, bytes] | None:
    row = L.db.execute(
        "SELECT path FROM documents WHERE sha=? AND company='ALL' AND source LIKE 'upload:%'", (sha,)
    ).fetchone()
    root = receipts.storage_root("ALL")
    raw = read_file(row["path"], [root], receipts.FILE_MAX) if row else None
    if raw and Path(raw[0]).suffix in (".pdf", ".jpg", ".png", ".heic") and hashlib.sha256(raw[1]).hexdigest() == sha:
        return raw
    return None


def assign(L: Ledger, sha: str, company: str) -> bool:
    if company not in companies.all(L):
        return False
    with receipts.STORE_LOCK, L.tx():
        raw = pending_file(L, sha)
        if not raw or hashlib.sha256(raw[1]).hexdigest() != sha:
            return False
        row = L.db.execute("SELECT * FROM receipt_uploads WHERE sha=? AND company='ALL'", (sha,)).fetchone()
        if not row:
            return False
        day, ext = row["uploaded_at"][:10], row["file_type"]
        if ext not in ("pdf", "jpg", "png", "heic"):
            return False
        target = finance_home().resolve() / company / "documents" / "inbox" / day / (sha + "." + ext)
        # Copy before the guarded ledger switch. A crash leaves the original available for retry;
        # after commit it leaves only an unreferenced staging copy, never a missing receipt.
        with receipts.directory(company, day) as fd:
            name = sha + "." + ext
            temp = ".upload-" + secrets.token_hex(16)
            out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(out, "wb") as dest:
                    dest.write(raw[1])
                    dest.flush()
                    os.fsync(dest.fileno())
                try:
                    os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    existing = read_file(
                        target, [finance_home().resolve() / company / "documents" / "inbox"], receipts.FILE_MAX
                    )
                    if not existing or existing[1] != raw[1]:
                        return False
            finally:
                os.unlink(temp, dir_fd=fd)
            os.fsync(fd)
        assigned = L.assign_upload(sha, company, str(target))
    if assigned:
        with suppress(OSError, receipts.Refused), receipts.directory("ALL", day) as fd:
            os.unlink(sha + "." + ext, dir_fd=fd)
            os.fsync(fd)
        receipt_header.apply(L, sha)  # the reading now fills the firm's document, when it is that firm's
    return assigned


def printed_roles(L: Ledger, sha: str) -> dict[str, str]:
    """The firms a PDF's own text names, each with the role its labels give it (invoices.roles): buyer, seller or ""."""
    raw = pending_file(L, sha)
    text = invoices.pdf_text(raw[1]) or "" if raw and raw[0].endswith(".pdf") else ""
    return invoices.roles(text, list(docs.inspect_text(text).mentions))


def owner(L: Ledger, sha: str) -> str | None:
    """The one firm the upload's reliable reading names as buyer and its PDF text doesn't contradict
    (receipt_header.buyer); else the one firm a PDF names, labelled buyer. None leaves it to a person."""
    read = receipt_header.buyer(L, sha)
    if read:
        return read
    roles = printed_roles(L, sha)
    printed = invoices.buyer_firm(invoices.Doc("pdf", firms=list(roles), roles=roles), list(roles)) or None
    proposed = receipt_header.proposed_buyer(L, sha)
    return None if proposed and proposed != printed else printed  # a reading that disagrees leaves it to a person


def waiting(L: Ledger) -> list[str]:
    """New install-wide uploads to assign now: once read, or at once with no AI to read them. One a person was asked
    about comes back here once, when a reading arrives (Ledger.save_receipt_header)."""
    rows = L.db.execute(
        "SELECT d.sha, h.header, h.tries FROM documents d LEFT JOIN receipt_headers h USING(sha) "
        "WHERE d.company='ALL' AND d.source LIKE 'upload:%' AND d.issues='[\"to-check\"]' ORDER BY d.created_at"
    ).fetchall()
    reading = ai_extract.ready(L)
    return [r["sha"] for r in rows if not reading or r["header"] is not None or (r["tries"] or 0) >= TRIES][:3]


def process_pending(L: Ledger) -> None:
    """Assign install-wide uploads to the one firm their reading and their PDF text agree on (owner); otherwise a
    person picks the firm. With an AI integration, a new upload waits for its reading."""
    for sha in waiting(L):
        company = owner(L, sha)
        try:
            if company and assign(L, sha, company):
                continue
        except (receipts.Refused, OSError):
            continue  # Keep the staged original available; one disk refusal cannot stop the tick.
        with L.tx() as db:
            db.execute("UPDATE documents SET issues='[\"buyer-unclear\"]' WHERE sha=? AND company='ALL'", (sha,))


def questions(L: Ledger) -> list[dict]:
    return [
        dict(
            key=r["sha"],
            subject="Document încărcat",
            sender="",
            at=r["created_at"],
            month=r["month"],
            date=None,
            supplier="",
            number=None,
            reason="upload-firm",
            companies=list(companies.all(L)),
            totals=[],
            file=Path(r["path"]).suffix.lstrip("."),
        )
        for r in L.documents(company="ALL", kind="receipt")
        if (r["source"] or "").startswith("upload:") and r["issues"] == '["buyer-unclear"]'
    ]
