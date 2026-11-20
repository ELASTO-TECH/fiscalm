"""Crash-conservative invoice submission: reserve before upload, never retry unknown outcomes.

API contract: https://mfinante.gov.ro/static/10/eFactura/prezentare%20apeluri%20API%20E-factura.pdf
Upload returns index_incarcare; stareMesaj returns stare and id_descarcare. No remote
response selects a host or filename. Signed responses are stored with openat/O_NOFOLLOW
inside the company's own directory. Rejected invoices keep their used number forever.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import time
import urllib.parse
import zipfile
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

from fiscalm import ledger
from fiscalm.efactura import client as anaf
from fiscalm.efactura.inbox import UblError, invoice_from_zip, safe_xml, valid_id
from fiscalm.invoicing import ubl
from fiscalm.invoicing.engine import LOCAL, _company, _day, invoice, invoice_number, normalize_vat_id
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.ledger import Ledger

Transport = Callable[[str, str, bytes | None], bytes]


def _live(environment: str | None = None) -> None:
    if not anaf.outward_allowed(environment):
        raise InvoiceError("Trimiterea în SPV nu este disponibilă până la activarea instalării.")


def _request(
    transport: Transport, method: str, endpoint: str, params: dict, body: bytes | None = None, *, environment: str
) -> bytes:
    _live(environment)
    return transport(method, anaf.API[environment] + "/" + endpoint + "?" + urllib.parse.urlencode(params), body)


def _upload_lock() -> Path:
    return ledger.finance_home() / "state" / "spv-upload.lock"


@contextlib.contextmanager
def _uploading() -> Iterator[None]:
    """Held by every upload from before its claim until its outcome is written.

    The OS drops the lock when the process ends, so an `uploading` row while nobody holds it
    belongs to an upload that can no longer write its outcome. ANAF's timeout applies to each
    socket operation, not to the whole request, so elapsed time cannot prove that.
    """
    path = _upload_lock()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        yield


def upload_running() -> bool:
    """Whether any upload in any process may still write an outcome; creates nothing."""
    try:
        fd = os.open(_upload_lock(), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _response(raw: bytes) -> dict:
    try:
        root = safe_xml(raw)
        if root.tag.rsplit("}", 1)[-1] != "header":
            raise InvoiceError("Răspunsul ANAF pentru factură are un format neașteptat.")
        return dict(root.attrib)
    except UblError:
        raise InvoiceError("Răspunsul ANAF pentru factură nu poate fi citit.") from None


def submission(L: Ledger, invoice_id: str, *, environment: str | None = None) -> dict | None:
    """Return submission status without credentials, or None before the first attempt."""
    environment = environment or anaf.env()
    row = L.db.execute(
        "SELECT * FROM invoice_submissions WHERE invoice_id=? AND environment=?", (invoice_id, environment)
    ).fetchone()
    if row:
        return _submission_record(invoice_id, environment, row, None)
    linked = L.db.execute(
        "SELECT s.* FROM invoice_sent_links l JOIN issued s ON s.message_id=l.message_id "
        "WHERE l.invoice_id=? AND l.environment=? AND s.upload_id IS NOT ?",
        (invoice_id, environment, LOCAL),
    ).fetchone()
    return _submission_record(invoice_id, environment, None, linked)


def submissions(L: Ledger, company: str) -> dict[str, dict]:
    """Batch the same submission evidence and fallback used by submission(), for one firm's projection."""
    environment = anaf.env()
    rows = {
        r["invoice_id"]: r
        for r in L.db.execute(
            "SELECT s.* FROM invoice_submissions s JOIN invoices i ON i.id=s.invoice_id "
            "WHERE i.company=? AND s.environment=?",
            (company, environment),
        )
    }
    linked = {}
    for r in L.db.execute(
        "SELECT l.invoice_id,s.* FROM invoice_sent_links l JOIN issued s ON s.message_id=l.message_id "
        "JOIN invoices i ON i.id=l.invoice_id "
        "WHERE i.company=? AND l.environment=? AND s.upload_id IS NOT ? ORDER BY l.rowid",
        (company, environment, LOCAL),
    ):
        linked.setdefault(r["invoice_id"], r)
    return {
        iid: _submission_record(iid, environment, rows.get(iid), linked.get(iid)) for iid in rows.keys() | linked.keys()
    }


def _submission_record(invoice_id: str, environment: str, row, linked) -> dict | None:
    if row:
        return dict(row) | {"errors": json.loads(row["errors"])}

    if linked:
        return dict(
            invoice_id=invoice_id,
            status="accepted",
            errors=[],
            upload_id=linked["upload_id"],
            download_id=linked["message_id"],
            raw_path=linked["raw_path"],
            xml_sha=linked["xml_sha"],
            environment=environment,
        )
    return None


def scope(inv: dict) -> str:
    """Conservative reporting: only an affirmative foreign-client declaration can skip.

    Unknown legacy clients remain mandatory, including home VAT ids with an RO CUI.
    RO-established and RO-registered buyers stay required even for foreign supplies
    (conservative for services under art. 278(2)). No signature/return value change.
    """
    buyer = inv["data"]["client"]
    vat = normalize_vat_id(buyer.get("vat_id", ""))
    if buyer["country"] == "RO" or vat.startswith("RO") or buyer.get("ro_vat_status", "unknown") != "not_registered":
        return "required"
    if inv["data"].get("supply_country", "RO") != "RO":
        return "not_required"
    if buyer.get("private"):
        return "required"
    return "optional" if buyer.get("efactura_registered") else "not_required"


UPLOAD_DAYS = range(6)  # 0 to 5 calendar days after issue: always inside ANAF's 5 working days
UPLOAD_DAYS_DEFAULT = 4


def auto_upload_setting(L: Ledger, company: str) -> tuple[bool, int]:
    """The firm's auto-upload rule in one read: whether it is on (absent settings mean on, for the tick and API) and
    its chosen day (`upload_delay`)."""
    row = L.db.execute(
        "SELECT auto_upload, upload_after_days FROM invoice_settings WHERE company=?", (company,)
    ).fetchone()
    return (bool(row[0]), row[1]) if row else (True, UPLOAD_DAYS_DEFAULT)


def auto_upload_enabled(L: Ledger, company: str) -> bool:
    return auto_upload_setting(L, company)[0]


def record_auto_upload_policy(L: Ledger, company: str, iid: str) -> None:
    """At issuance, in the same transaction as the invoice number: invoice_manual_upload when auto-upload is paused
    (only a person sends it, even after a resume), else invoice_auto_upload (the tick sends it on its upload_on day).
    Two keys, so the manual one keeps the meaning older releases read."""
    if scope(invoice(L, iid, company=company)) == "required":
        key = "invoice_auto_upload:" if auto_upload_enabled(L, company) else "invoice_manual_upload:"
        L.set_meta(key + iid, "1")


def waiting_manual(L: Ledger, company: str) -> int:
    return sum(
        invoice(L, iid, company=company)["data"].get("issued_live", False)
        and (submission(L, iid) or {}).get("status") in (None, "rejected")
        for iid in manual_uploads(L, company)
    )


def set_auto_upload(L: Ledger, company: str, enabled: bool) -> None:
    """Persist the per-company setting; absent settings mean enabled."""
    _company(L, company)
    if type(enabled) is not bool:
        raise InvoiceError("Trimiterea automată trebuie activată sau dezactivată.")
    with L.tx() as db:
        if enabled and not auto_upload_enabled(L, company):
            # Invoices issued before issuance recorded its decision stay manual; recorded ones keep theirs.
            for row in db.execute(
                "SELECT id FROM invoices i WHERE company=? AND status!='draft' "
                "AND NOT EXISTS (SELECT 1 FROM meta WHERE key IN ('invoice_manual_upload:'||i.id,"
                "'invoice_auto_upload:'||i.id))",
                (company,),
            ).fetchall():
                record_auto_upload_policy(L, company, row["id"])
        db.execute(
            "INSERT INTO invoice_settings(company, auto_upload) VALUES(?,?) "
            "ON CONFLICT(company) DO UPDATE SET auto_upload=excluded.auto_upload",
            (company, int(enabled)),
        )


def upload_delay(L: Ledger, company: str) -> int:
    """The firm's chosen delay, set with its auto-upload rule (Automatizări), whether or not the rule runs now."""
    return auto_upload_setting(L, company)[1]


def set_upload_delay(L: Ledger, company: str, days: int) -> None:
    """Change the delay of a running rule only: with auto-upload off the delay has no effect, so it is not offered."""
    _company(L, company)
    if type(days) is not int or days not in UPLOAD_DAYS:
        raise InvoiceError("Alege între 0 și 5 zile după emitere.")
    with L.tx() as db:
        if not auto_upload_enabled(L, company):
            raise InvoiceError("Pornește trimiterea automată în SPV ca să alegi ziua.")
        db.execute(
            "INSERT INTO invoice_settings(company, auto_upload, upload_after_days) VALUES(?,1,?) "
            "ON CONFLICT(company) DO UPDATE SET upload_after_days=excluded.upload_after_days",
            (company, days),
        )


def upload(
    L: Ledger,
    invoice_id: str,
    transport: Transport = anaf.invoice_request,
    *,
    company: str,
    authorize: Callable[[], None] | None = None,
    environment: str | None = None,
) -> dict:
    """Upload once; refuse existing state except a known rejection.

    Crashes/timeouts after claiming leave uploading/unknown, requiring operator investigation
    in SPV. Retrying that state never sends again; there is no ANAF idempotency key.
    An optional authorization check runs under the claim transaction's write lock.
    """
    anaf.require_configured(L)
    environment = environment or anaf.env()
    _live(environment)
    xml = ubl.generate(L, invoice_id, company=company)
    inv = invoice(L, invoice_id, company=company)
    if scope(inv) == "not_required":
        raise InvoiceError("Factura nu se transmite în SPV pentru această operațiune.")
    with _uploading():
        with L.tx() as db:
            anaf.require_configured(L)
            if authorize is not None:
                authorize()
            # Take the write lock before checking both submissions and imported acceptance.
            db.execute("UPDATE invoices SET status=status WHERE id=?", (invoice_id,))
            previous = submission(L, invoice_id, environment=environment)
            if previous and previous["status"] != "rejected":
                raise InvoiceError(
                    "Există deja o încercare de trimitere în SPV. Verifică starea ei înainte să trimiți factura din nou."
                )
            if transport is anaf.invoice_request:
                transport = anaf.prepare_invoice_request(L, environment=environment, company=company)
            if previous:
                db.execute(
                    "UPDATE invoice_submissions SET status='uploading',upload_id=NULL,download_id=NULL,errors='[]',raw_path=NULL,xml_sha=?,claimed_at=? WHERE invoice_id=? AND environment=? AND status='rejected'",
                    (hashlib.sha256(xml).hexdigest(), time.time(), invoice_id, environment),
                )
            else:
                db.execute(
                    "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha,claimed_at) VALUES(?,?,?,?,?)",
                    (invoice_id, environment, "uploading", hashlib.sha256(xml).hexdigest(), time.time()),
                )
        try:
            buyer = inv["data"]["client"]
            endpoint = "upload" if buyer.get("cui") or buyer.get("vat_id") or buyer.get("tax_id") else "uploadb2c"
            params = {"standard": "UBL", "cif": inv["data"]["seller"]["cui"]}
            if buyer["country"] != "RO":
                params["extern"] = "DA"
            response = _response(_request(transport, "POST", endpoint, params, xml, environment=environment))
            uid = response.get("index_incarcare", "")
            if response.get("ExecutionStatus") == "1" or not valid_id(uid):
                # Explicit rejection is known safe to retry; malformed/ambiguous success is not.
                status = "rejected" if response.get("ExecutionStatus") == "1" else "unknown"
                with L.tx() as db:
                    db.execute(
                        "UPDATE invoice_submissions SET status=?,errors=? WHERE invoice_id=? AND environment=? AND status='uploading'",
                        (
                            status,
                            json.dumps(["ANAF nu a acceptat trimiterea. Răspunsul este în SPV."]),
                            invoice_id,
                            environment,
                        ),
                    )
                    from fiscalm.commerce.webhooks import capture

                    capture(L, invoice_id=invoice_id)
            else:
                with L.tx() as db:
                    db.execute(
                        "UPDATE invoice_submissions SET status='uploaded',upload_id=? WHERE invoice_id=? AND environment=? AND status='uploading'",
                        (uid, invoice_id, environment),
                    )
        except Exception:
            with L.tx() as db:
                db.execute(
                    "UPDATE invoice_submissions SET status='unknown',errors=? WHERE invoice_id=? AND environment=? AND status='uploading'",
                    (
                        json.dumps(
                            ["Rezultatul trimiterii este necunoscut. Verifică în SPV înainte să trimiți din nou."]
                        ),
                        invoice_id,
                        environment,
                    ),
                )
            raise InvoiceError("Factura este salvată. Nu se știe dacă a ajuns în SPV. Verifică în SPV.") from None
    if submission(L, invoice_id, environment=environment)["status"] in {"uploaded", "accepted"}:
        with L.tx() as db:
            db.execute("DELETE FROM meta WHERE key=?", ("invoice_upload_error:" + invoice_id,))
    return submission(L, invoice_id, environment=environment)


class _NotDue(Exception):
    """The automatic upload's decision changed before its claim; nothing was sent."""


def upload_after_days(L: Ledger, company: str) -> int | None:
    """How many days after issue the tick sends the firm's invoices to SPV, or None when it sends none on its own.

    Set with the firm's rule in Automatizări (`upload_delay`); the days between leave time to review an invoice and
    send or fix it by hand. None in Sandbox, with the rule off, or without an ANAF connection for the firm.
    """
    if not ledger.is_live() or not auto_upload_enabled(L, company) or anaf.instance(L, company) is None:
        return None
    return upload_delay(L, company)


def manual_uploads(L: Ledger, company: str) -> set[str]:
    """The firm's invoices issued while auto-upload was paused: only a person sends them."""
    return {
        r[0]
        for r in L.db.execute(
            "SELECT i.id FROM invoices i JOIN meta m ON m.key='invoice_manual_upload:'||i.id AND m.value='1' "
            "WHERE i.company=?",
            (company,),
        )
    }


def upload_on(L: Ledger, inv: dict) -> str | None:
    """The day the tick sends this issued invoice to SPV on its own, or None when only a person can send it."""
    manual = L.get_meta("invoice_manual_upload:" + inv["id"]) == "1"
    return upload_day(inv, upload_after_days(L, inv["company"]), manual)


def upload_day(inv: dict, days: int | None, manual: bool) -> str | None:
    """upload_on with the firm's upload_after_days and the invoice's pause record already read (lists batch them)."""
    if (
        days is None
        or manual
        or inv["status"] == "draft"
        or scope(inv) != "required"
        or not inv["data"].get("issued_live", False)
    ):
        return None
    return (date.fromisoformat(inv["data"]["issue_date"]) + timedelta(days=days)).isoformat()


def auto_upload(
    L: Ledger, invoice_id: str, transport: Transport = anaf.invoice_request, *, company: str, today: str
) -> dict:
    """The tick's decision for one issued invoice: upload it from its upload_on day; failures never undo it."""
    environment = anaf.env()
    inv = invoice(L, invoice_id, company=company)
    if inv["status"] == "draft":
        raise InvoiceError("Factura se emite înainte de trimiterea în SPV.")
    existing = submission(L, invoice_id, environment=environment)
    if existing:
        if existing["status"] in {"uploaded", "accepted"}:
            with L.tx() as db:
                db.execute("DELETE FROM meta WHERE key=?", ("invoice_upload_error:" + invoice_id,))
        return existing
    if scope(inv) != "required":
        return {"invoice_id": invoice_id, "status": "not_required", "errors": []}
    day = upload_on(L, inv)
    if day is None or not anaf.outward_allowed():
        return {"invoice_id": invoice_id, "status": "disabled"}
    if _day(today) < day:
        return {"invoice_id": invoice_id, "status": "scheduled", "upload_on": day}

    def still_due() -> None:  # under the claim's write lock: a pause or a longer delay saved meanwhile wins
        due = upload_on(L, inv)
        if due is None or today < due or submission(L, invoice_id, environment=environment):
            raise _NotDue  # an attempt made meanwhile (a person's, even a rejected one) is never retried here

    try:
        return upload(L, invoice_id, transport, company=company, authorize=still_due, environment=environment)
    except _NotDue:
        return {"invoice_id": invoice_id, "status": "disabled"}
    except Exception as exc:
        from fiscalm.api.actions import log

        log(f"invoicing: auto upload failed: {type(exc).__name__}")
        L.set_meta("invoice_upload_error:" + invoice_id, ledger.now())
        return {
            "invoice_id": invoice_id,
            "status": "error",
            "errors": ["Factura este salvată. Verifică trimiterea în SPV."],
        }


def _store(L: Ledger, company: str, day: str, mid: str, raw: bytes, *, environment: str | None = None) -> Path:
    environment = environment or anaf.env()
    _company(L, company)
    _day(day)
    if not re.fullmatch("[A-Za-z0-9_-]+", company) or not valid_id(mid):
        raise InvoiceError("Firma sau identificatorul mesajului ANAF nu este valid.")
    root = ledger.finance_home() / "efactura"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in [company, "emise", environment, day[:7]]:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        filename = mid + ".zip"
        temporary = ".invoice-" + uuid4().hex + ".part"
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                existing = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                with os.fdopen(existing, "rb") as stream:
                    if stream.read(50 * 1024 * 1024 + 1) != raw:
                        raise InvoiceError("Există deja un alt răspuns semnat pentru acest mesaj.") from None
            os.fsync(fd)
        finally:
            os.unlink(temporary, dir_fd=fd)
    finally:
        os.close(fd)
    return root / company / "emise" / environment / day[:7] / (mid + ".zip")


def _errors(raw: bytes) -> list[str]:
    """Extract rule codes only; error bodies can contain full bank details or other sensitive data."""
    codes = set()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 10:
                raise ValueError
            for item in archive.infolist():
                if item.filename.endswith(".xml") and item.file_size <= 10 * 1024 * 1024:
                    with archive.open(item) as stream:
                        root = safe_xml(stream.read(10 * 1024 * 1024 + 1))
                    codes.update(re.findall(r"\bBR-[A-Z0-9-]+", str(root.attrib) + _error_text(root)))
    except (ValueError, OSError, UblError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        pass
    return sorted(codes) or ["ANAF a respins factura. Detaliile sunt în răspunsul semnat."]


def _error_text(root: ET.Element) -> str:
    return " ".join(" ".join(node.attrib.values()) + (node.text or "") for node in root.iter())


def poll(L: Ledger, transport: Transport = anaf.invoice_request) -> list[dict]:
    """Check uploaded invoices, store signed ok/nok ZIPs, and retain numbers on rejection."""
    anaf.require_configured(L)
    environment = anaf.env()
    _live(environment)
    default_transport = transport is anaf.invoice_request
    out = []
    for row in L.db.execute("SELECT * FROM invoice_submissions WHERE status='uploaded'").fetchall():
        iid = row["invoice_id"]
        if row["environment"] != environment:
            out.append({"invoice_id": iid, "status": "environment_mismatch"})
            continue
        try:
            if default_transport:
                company = L.db.execute("SELECT company FROM invoices WHERE id=?", (iid,)).fetchone()[0]
                transport = anaf.prepare_invoice_request(L, environment=environment, company=company)
            response = _response(
                _request(transport, "GET", "stareMesaj", {"id_incarcare": row["upload_id"]}, environment=environment)
            )
            state = response.get("stare")
            if state == "in prelucrare":
                out.append({"invoice_id": iid, "status": "processing"})
                continue
            if state not in {"ok", "nok", "XML cu erori nepreluat de sistem"}:
                raise InvoiceError("Răspunsul ANAF nu conține o stare recunoscută.")
            if state == "XML cu erori nepreluat de sistem" and not response.get("id_descarcare"):
                with L.tx() as db:
                    db.execute(
                        "UPDATE invoice_submissions SET status='rejected',errors=? WHERE invoice_id=? AND environment=? AND status='uploaded' AND upload_id=?",
                        (
                            json.dumps(["ANAF a respins XML-ul înainte de prelucrare."]),
                            iid,
                            environment,
                            row["upload_id"],
                        ),
                    )
                    from fiscalm.commerce.webhooks import capture

                    capture(L, invoice_id=iid)
                out.append(submission(L, iid, environment=environment))
                continue
            mid = response.get("id_descarcare", "")
            if not valid_id(mid):
                raise InvoiceError("Identificatorul răspunsului semnat nu este valid.")
            raw = _request(transport, "GET", "descarcare", {"id": mid}, environment=environment)
            if not raw.startswith(b"PK\x03\x04"):
                raise InvoiceError("Răspunsul semnat nu este o arhivă ZIP.")
            inv = invoice(L, iid, company=L.db.execute("SELECT company FROM invoices WHERE id=?", (iid,)).fetchone()[0])
            if state == "ok":
                parsed, sha = invoice_from_zip(raw)
                if sha != row["xml_sha"]:
                    raise InvoiceError("XML-ul semnat diferă de cel trimis.")
                if (
                    parsed.number != invoice_number(inv)
                    or parsed.supplier_cui != inv["data"]["seller"]["cui"]
                    or parsed.total != inv["data"]["totals"]["total"]
                    or parsed.currency != inv["data"]["currency"]
                ):
                    raise InvoiceError("Factura semnată nu corespunde facturii trimise.")
            path = _store(L, inv["company"], inv["data"]["issue_date"], mid, raw, environment=environment)
            errors = _errors(raw) if state != "ok" else []
            with L.tx() as db:
                db.execute(
                    "UPDATE invoice_submissions SET status=?,download_id=?,raw_path=?,errors=? WHERE invoice_id=? AND environment=? AND status='uploaded' AND upload_id=?",
                    (
                        "accepted" if state == "ok" else "rejected",
                        mid,
                        str(path),
                        json.dumps(errors),
                        iid,
                        environment,
                        row["upload_id"],
                    ),
                )
                from fiscalm.commerce.webhooks import capture

                capture(L, invoice_id=iid)
            if state == "ok":
                with L.tx() as db:
                    db.execute("DELETE FROM meta WHERE key=?", ("invoice_upload_error:" + iid,))
            out.append(submission(L, iid, environment=environment))
        except (InvoiceError, anaf.AnafError, anaf.secrets.SecretError, OSError, UblError):
            out.append(
                {
                    "invoice_id": iid,
                    "status": "error",
                    "errors": ["Starea sau răspunsul SPV nu a putut fi preluat, verificarea se va relua."],
                }
            )
    return out


def can_resolve(current: dict | None, *, running: bool) -> bool:
    """Resolve an unknown outcome, or a claim whose upload can no longer write one (see `_uploading`)."""
    return bool(current and (current["status"] == "unknown" or current["status"] == "uploading" and not running))


def resolve(
    L: Ledger,
    invoice_id: str,
    *,
    company: str,
    upload_id: str | None,
    checked_in_spv: bool,
    environment: str | None = None,
    authorize: Callable[[], None] | None = None,
    claim: tuple | None = None,
) -> dict | None:
    """Explicit operator resolution after checking SPV, guarded against stale poll/upload writes. A lookup made outside
    the lock passes the (xml_sha, claimed_at) it matched: another attempt since then settles nothing."""
    environment = environment or anaf.env()
    invoice(L, invoice_id, company=company)
    if checked_in_spv is not True or (upload_id is not None and not valid_id(upload_id)):
        raise InvoiceError("Verifică în SPV. Completează indexul valid sau confirmă că factura nu a fost trimisă.")
    with L.tx() as db:
        db.execute(
            "UPDATE invoice_submissions SET status=status WHERE invoice_id=? AND environment=?",
            (invoice_id, environment),
        )
        if authorize is not None:
            authorize()
        current = submission(L, invoice_id, environment=environment)
        if claim and current and (current["xml_sha"], current["claimed_at"]) != claim:
            raise InvoiceError("Nu este clar care este factura trimisă. Verifică în SPV.")
        if not can_resolve(current, running=upload_running()):
            if current and current["status"] == "uploading":
                raise InvoiceError("Trimiterea este încă în curs. Verifică în SPV după ce se termină.")
            raise InvoiceError("Poți lămuri doar o trimitere cu rezultat necunoscut.")
        if upload_id is None:
            changed = db.execute(
                "DELETE FROM invoice_submissions WHERE invoice_id=? AND environment=? AND status IN ('unknown','uploading')",
                (invoice_id, environment),
            ).rowcount
        else:
            changed = db.execute(
                "UPDATE invoice_submissions SET status='uploaded',upload_id=?,errors='[]' WHERE invoice_id=? AND environment=? AND status IN ('unknown','uploading')",
                (upload_id, invoice_id, environment),
            ).rowcount
        if not changed:
            raise InvoiceError("Poți lămuri doar o trimitere cu rezultat necunoscut.")
        db.execute("DELETE FROM meta WHERE key=?", ("invoice_upload_error:" + invoice_id,))
    return submission(L, invoice_id, environment=environment)


def recover_unknown(
    L: Ledger, invoice_id: str, *, company: str, authorize: Callable[[], None] | None = None, list_sent=None
) -> dict | None:
    """recover_sent over the week from the unknown attempt, so the person never copies an index from SPV."""
    invoice(L, invoice_id, company=company)
    current = submission(L, invoice_id)
    if not current or not current.get("claimed_at"):
        raise InvoiceError("Poți recupera doar o trimitere cu rezultat necunoscut.")
    start = datetime.fromtimestamp(current["claimed_at"]) - timedelta(hours=1)
    end = min(start + timedelta(days=7), datetime.now())
    return recover_sent(
        L,
        invoice_id,
        company=company,
        start=start,
        end=end,
        list_sent=list_sent or anaf.list_sent,
        authorize=authorize,
        claim=(current["xml_sha"], current["claimed_at"]),
    )


def recover_sent(
    L: Ledger, invoice_id: str, *, company: str, start, end, list_sent=anaf.list_sent, authorize=None, claim=None
) -> dict | None:
    """Match downloaded sent invoices, never free-form ANAF details or just an amount. ANAF is asked outside any ledger
    transaction; the resolution then re-checks, under the lock, that it is still the attempt that was matched. `claim`:
    the (xml_sha, claimed_at) the caller chose the window for; another attempt since then is refused."""
    anaf.require_configured(L)
    environment = anaf.env()
    _live(environment)
    inv = invoice(L, invoice_id, company=company)
    current = submission(L, invoice_id, environment=environment)
    if not current or current["status"] not in {"unknown", "uploading"}:
        raise InvoiceError("Poți recupera doar o trimitere cu rezultat necunoscut.")
    if claim and (current["xml_sha"], current["claimed_at"]) != claim:
        raise InvoiceError("Nu este clar care este factura trimisă. Verifică în SPV.")
    token = anaf.access_token(L=L, company=company)
    matches = []
    for message in list_sent(
        inv["data"]["seller"]["cui"],
        start,
        end,
        token,
        **({"environment": environment} if list_sent is anaf.list_sent else {}),
    ):
        mid = str(message.get("id", ""))
        uid = str(message.get("id_solicitare", ""))
        if not valid_id(mid) or not valid_id(uid):
            continue
        try:
            _live(environment)
            parsed, sha = invoice_from_zip(anaf.download(mid, token, environment=environment))
        except (UblError, anaf.AnafError):
            continue
        if (
            parsed.supplier_cui == inv["data"]["seller"]["cui"]
            and parsed.number == invoice_number(inv)
            and parsed.issue_date == inv["data"]["issue_date"]
            and sha == current["xml_sha"]
        ):
            matches.append(uid)
    if len(set(matches)) != 1:
        raise InvoiceError("Nu este clar care este factura trimisă. Verifică în SPV.")
    claim = (current["xml_sha"], current["claimed_at"])  # the same snapshot guards the write
    return resolve(
        L,
        invoice_id,
        company=company,
        upload_id=matches[0],
        checked_in_spv=True,
        environment=environment,
        authorize=authorize,
        claim=claim,
    )
