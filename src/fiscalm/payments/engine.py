"""Named payment files and their journalled bank-file lifecycle. Never moves money."""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from fiscalm import companies
from fiscalm.bank import intake as statement_intake
from fiscalm.efactura.credits import credited
from fiscalm.ledger import Ledger, finance_home, is_live
from fiscalm.migrations import payment_file_name
from fiscalm.payments import files as payfiles
from fiscalm.payments.orders import PaymentFileError, PaymentItem


def _item(row) -> PaymentItem:
    """A ledger payment row as a file row, debiting the account it was exported from (the format re-checks it)."""
    columns = row.keys()  # sqlite3.Row has no dict.get().
    return PaymentItem(
        company=row["company"],
        kind=row["kind"],
        payee_name=row["payee_name"],
        payee_iban=row["payee_iban"],
        payee_bic=row["payee_bic"],
        payee_fiscal_code=row["fiscal_code"] or "",
        amount=Decimal(row["amount"]),
        details=row["details"],
        issue_date=date.fromisoformat(row["issue_date"]),
        doc_ref=row["doc_ref"],
        source_path=row["source_path"],
        source_sha=row["source_sha"],
        source_iban=(row["source_iban"] if "source_iban" in columns else None) or "",
    )


def to_pay_dir() -> Path:
    """Legacy storage inspected for migrated and orphan files; new files use company-owned paths."""
    return finance_home() / TO_PAY


def _previous_month(d: date) -> str:
    return f"{d.year - 1}-12" if d.month == 1 else f"{d.year}-{d.month - 1:02d}"


def missing_statement(ledger: Ledger, company: str, today: date) -> str:
    """Require last month's paying-account statement only for firms that receive statements."""
    month = _previous_month(today)
    connected = ledger.db.execute("SELECT 1 FROM bank_connections WHERE company=? LIMIT 1", (company,)).fetchone()
    earlier = any(d["month"] < today.strftime("%Y-%m") for d in ledger.documents(company=company, kind="statement"))
    if not connected and not earlier:
        return ""
    account = companies.get(company).paying_account
    return month if account and not statement_intake.covers(ledger, company, month, account.iban) else ""


@contextmanager
def pay_lock():
    """One payment mutation at a time across processes (watcher, headless Claude, the user's shell).
    Without it, two runs could read the same pending items and both export them."""
    path = finance_home() / "state" / "pay.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def paying_format(company: str, co: companies.Company | None = None) -> payfiles.PaymentFormat | None:
    """The format of the company's payment files: its paying account's bank's, or None (no file, payments held).
    `co`: the company as a snapshot read it (api.state), else the current configuration."""
    co = co or companies.get(company)
    return payfiles.for_account(co.paying_account if co.ron_iban else None)


def _format(items: list[PaymentItem], co: companies.Company | None = None) -> payfiles.PaymentFormat:
    """The format of a file holding `items`: the one of the account they debit (the format re-checks every row)."""
    co = co or companies.get(items[0].company)
    account = co.account(items[0].source_iban or co.ron_iban or "")
    fmt = payfiles.for_account(account)
    if not fmt and account and not account.bank:
        # exported before formats existed, from an account outside the catalogue (its source maybe not even recorded):
        # it was written as BT Go then, so its file is finished (redated, archived, restored) the same way. New rows
        # from such an account never get here: download_file refuses them first.
        fmt = payfiles.FORMATS["btgo-csv"]
    if not fmt:
        raise PaymentFileError(payfiles.why_not(account))
    return fmt


def _payment_files(folder: Path, pattern: str = "*") -> list[Path]:
    return sorted(p for ext in payfiles.EXTENSIONS for p in folder.glob(pattern + ext))


def _publish(items: list[PaymentItem], path: Path, value_date: date) -> None:
    if not is_live():
        raise PaymentFileError("Payment files require a live installation")
    fmt = _format(items)
    if path.suffix != fmt.extension:  # a file is never written in another format than its name says
        raise PaymentFileError(f"{path.name} holds {fmt.app} rows: import it before paying from another bank")
    data = fmt.build(items, value_date)  # checked by the format before it is written
    tmp_dir = path.parent / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    written = tmp_dir / path.name
    written.write_bytes(data)
    written.replace(path)  # atomic: a crash never leaves half a payment file
    tmp_dir.rmdir()


def version(item_ids) -> str:
    """Names one exact set of rows (what a download held)."""
    return hashlib.sha256(",".join(sorted(item_ids)).encode()).hexdigest()[:12]


def stale_payments(ledger: Ledger, company: str) -> set[str]:
    """Pending invoice payments whose amount is not their invoice's total minus its linked credits now (a credit that
    stopped linking, or arrived since): no debit settles them, so a shortfall is never hidden as paid."""
    credits = credited(ledger, company)
    pending = {p["item_id"]: Decimal(p["amount"]) for p in ledger.payments(company=company, status="pending")}
    return {
        r["item_id"]
        for r in ledger.efactura_rows(status="linked", company=company)
        if r["item_id"] in pending
        and pending[r["item_id"]] != Decimal(r["total"]) - credits.get(r["message_id"], Decimal(0))
    }


def _without_credits(ledger: Ledger, rows: list) -> list:
    """Under pay_lock: no unimported invoice payment keeps an amount that is not its invoice's total minus its linked
    credits now (efactura.payment_item): one built before a credit, or for a credit that no longer links, is withdrawn."""
    with ledger.tx():
        credits = {k: v for co in {r["company"] for r in rows} for k, v in credited(ledger, co).items()}
        amounts = {r["item_id"]: Decimal(r["amount"]) for r in rows}
        by_item = {
            r["item_id"]: r["message_id"]
            for r in ledger.efactura_rows(status="linked")
            if r["item_id"] in amounts
            and amounts[r["item_id"]] != Decimal(r["total"]) - credits.get(r["message_id"], Decimal(0))
        }
        for row in rows:
            if row["item_id"] in by_item and not ledger.withdraw_credited_payment(
                by_item[row["item_id"]], row["item_id"]
            ):
                raise PaymentFileError(
                    "invoice has a credit note; verify the exported payment in the bank before proceeding"
                )
        return [r for r in rows if r["item_id"] not in by_item]


def _quarantine(ledger: Ledger, *, pending_only: bool = False) -> list[dict]:
    """Journal removal of existing bank files from circulation, without generating bytes."""
    root = to_pay_dir()
    if not root.resolve().is_relative_to(finance_home().resolve()):
        raise PaymentFileError("Invalid shadow payment storage")
    prefix = "payment_shadow:"
    jobs = {
        r["key"][len(prefix) :]: r["value"]
        for r in ledger.db.execute("SELECT key,value FROM meta WHERE key LIKE 'payment_shadow:%'")
    }
    jobs = {name: value for name, value in jobs.items() if value}
    names = set(jobs) | (set() if pending_only else {p.name for p in _payment_files(root)})
    done = []
    for name in sorted(names):
        if Path(name).name != name or not name or name in (".", ".."):
            raise PaymentFileError("Invalid shadow payment journal")
        source = root / name
        if source.is_symlink():
            raise PaymentFileError("Payment file is a symlink")
        if source.exists():
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if name in jobs and jobs[name] != digest:
                raise PaymentFileError("Payment file changed during shadow quarantine")
            ledger.set_meta(prefix + name, digest)
            destination = root / ".shadow" / (name + "." + digest + ".blocked")
            destination.parent.mkdir(exist_ok=True)
            if not destination.parent.resolve().is_relative_to(root.resolve()):
                raise PaymentFileError("Invalid shadow payment storage")
            source.replace(destination)
            done.append({"file": str(source), "action": "quarantined in shadow"})
        ledger.set_meta(prefix + name, "")
    return done


def statement_waits(ledger: Ledger, company: str, rows: list, today: date) -> tuple[str, list]:
    """The pending rows that wait for last month's statement of the paying account, and that month ("" when none).
    Only payments fiscalm queued on its own, or of unknown origin (a mailed OP, a row from before origins were
    recorded): a person who picks an invoice to pay knows whether they already paid it; a rule doesn't, and the
    statement shows what was already paid by hand (maintainer, 2026-11-18)."""
    waiting = [r for r in rows if r["origin"] != "person"]
    month = missing_statement(ledger, company, today) if waiting else ""
    return (month, waiting) if month else ("", [])


def orphans(ledger: Ledger) -> list[str]:
    """Payment files in the to-pay folder that no ledger row points at: never rewritten or reminded,
    so they must be reported, since importing one blindly could pay twice."""
    known = {r["csv_path"] for r in ledger.payments() if r["csv_path"]}
    return sorted(str(p) for p in _payment_files(to_pay_dir()) if str(p) not in known)


TO_PAY = "De plătit"


class FileRefusal(PaymentFileError):
    """A refusal safe to show verbatim to the person, in Romanian."""


QUESTION_REFUSAL = "Răspunde întâi la întrebarea despre acest fișier."


def default_name(ledger: Ledger, company: str, today: date) -> str:
    co = companies.get(company, ledger)
    base = payment_file_name(co.name or co.key, today)
    used = {r["name"] for r in ledger.payment_files(company=company)}
    candidate, suffix = base, 2
    while candidate in used:
        candidate = f"{base} ({suffix})"
        suffix += 1
    return candidate


def file_rows(ledger: Ledger, file_id: int) -> list:
    return list(ledger.db.execute("SELECT * FROM payments WHERE file_id=? ORDER BY created_at,item_id", (file_id,)))


def file_downloads(ledger: Ledger, file_id: int) -> list[dict]:
    return [
        dict(r) | {"items": json.loads(r["items"])}
        for r in ledger.db.execute(
            "SELECT * FROM payment_downloads WHERE file_id=? ORDER BY created_at DESC,id DESC", (file_id,)
        )
    ]


def named_files(ledger: Ledger, company: str) -> list[dict]:
    proofs = ledger.paid_proofs(company)
    files = ledger.payment_files(company=company)
    if not files:
        return []
    members, snapshots = {}, {}
    for row in ledger.payments(company=company):
        members.setdefault(row["file_id"], []).append(row)
    for row in ledger.db.execute(
        "SELECT d.* FROM payment_downloads d JOIN payment_files f ON f.id=d.file_id "
        "WHERE f.company=? ORDER BY d.created_at DESC,d.id DESC",
        (company,),
    ):
        snapshots.setdefault(row["file_id"], []).append(dict(row) | {"items": json.loads(row["items"])})
    return [_named_summary(ledger, f, members.get(f["id"], []), snapshots.get(f["id"], []), proofs) for f in files]


def _named_summary(ledger: Ledger, file, rows: list, downloads_: list[dict], proofs: dict) -> dict:
    """A file downloaded several times stays editable: the latest download is the one to import, and an edit since
    then only shows `changed_since_download` (import waits for a new download). Questions are about the file itself."""
    confirmable = [r for r in rows if r["status"] != "skipped"]
    confirmed = bool(confirmable) and all("payment " + r["item_id"] in proofs for r in confirmable)
    changed = bool(downloads_) and downloads_[0]["revision"] != file["revision"]
    path = named_path(file)
    question = (
        "missing"
        if file["path"] and not path.exists() and not file["imported_at"] and not confirmed
        else "orphan"
        if path.exists() and not rows
        else None
    )
    return {
        "id": file["id"],
        "co": file["company"],
        "name": file["name"],
        "created_at": file["created_at"],
        "question": question,
        "import_in": import_in(ledger, file["company"], rows=rows),
        "status": "confirmed" if confirmed else "imported" if file["imported_at"] else "open",
        "confirmed_count": sum("payment " + r["item_id"] in proofs for r in confirmable),
        "skipped_count": len(rows) - len(confirmable),
        "downloaded_at": downloads_[0]["created_at"] if downloads_ else None,
        "imported_at": file["imported_at"],
        "payment_count": len(rows),
        "total": f"{sum((Decimal(r['amount']) for r in rows), Decimal(0)):.2f}",
        "currency": "RON",
        "changed_since_download": changed if not file["imported_at"] and not confirmed else False,
    }


def open_file(ledger: Ledger, file_id: int, company: str):
    file = ledger.payment_file(file_id)
    if not file or file["company"] != company or file["imported_at"]:
        raise FileRefusal("Fișierul nu mai este deschis.")
    rows = file_rows(ledger, file_id)
    proofs = ledger.paid_proofs(company)
    confirmable = [r for r in rows if r["status"] != "skipped"]
    if confirmable and all("payment " + r["item_id"] in proofs for r in confirmable):
        raise FileRefusal("Plățile din fișier sunt deja confirmate.")
    return file


def editable_file(ledger: Ledger, file_id: int, company: str):
    file = open_file(ledger, file_id, company)
    summary = next(f for f in named_files(ledger, company) if f["id"] == file_id)
    if summary["question"]:
        raise FileRefusal(QUESTION_REFUSAL)
    return file


def add_invoices(
    ledger: Ledger,
    company: str,
    invoice_ids: list[str],
    *,
    file_id: int | None = None,
    name: str | None = None,
    approve_invoice: Callable,
    accounts: dict[str, str] | None = None,
    amounts: dict[str, str] | None = None,
) -> int:
    """`accounts`: the IBAN a person picked for an invoice listing several (efactura.approve's `account`). `amounts`:
    the amount the person saw for an invoice; its payment must be that amount, or nothing is added."""
    with pay_lock(), ledger.tx():
        if file_id is None:
            file_id = ledger.create_payment_file(company, name or "")
        file = editable_file(ledger, file_id, company)
        if file["path"] and not named_path(file).exists():
            raise FileRefusal("Fișierul lipsește. Confirmă dacă l-ai importat înainte să-l schimbi.")
        if len(set(invoice_ids)) != len(invoice_ids):
            raise FileRefusal("Factura este selectată de două ori.")
        for mid in invoice_ids:
            row = ledger.efactura(mid)
            if not row or row["company"] != company:
                raise FileRefusal("Factura nu aparține firmei.")
            account = (accounts or {}).get(mid)
            item = approve_invoice(ledger, mid, origin="person", unfiled=True, account=account)["item_id"]
            if mid in (amounts or {}) and Decimal(ledger.payment(item)["amount"]) != Decimal(amounts[mid]):
                raise FileRefusal("Suma de plată s-a schimbat. Verifică factura din nou.")
            if not ledger.attach_payment_file(file_id, company, item, origin="person"):
                raise FileRefusal("Factura este deja într-un fișier sau s-a schimbat.")
        return file_id


def named_path(file) -> Path:
    fmt = paying_format(file["company"])
    extension = fmt.extension if fmt else ".csv"
    path = (
        Path(file["path"])
        if file["path"]
        else finance_home() / file["company"] / "payment-files" / f"{file['id']}{extension}"
    )
    root = finance_home().resolve()
    company_root = root / file["company"]
    legacy = path.parent == to_pay_dir() and path.name.startswith(f"payments-{file['company']}")
    if (
        path.is_symlink()
        or not path.resolve().is_relative_to(root)
        or (not legacy and not path.resolve().is_relative_to(company_root))
    ):
        raise PaymentFileError("Invalid payment file storage")
    return path


def _prepare_download(
    ledger: Ledger,
    file_id: int,
    company: str,
    today: date,
    authorize: Callable,
    restore_version: str | None = None,
    *,
    value_date: date | None = None,
) -> dict:
    with ledger.tx():
        authorize()
        file = open_file(ledger, file_id, company)
        rows = [r for r in file_rows(ledger, file_id) if r["status"] in ("pending", "exported")]
        path = named_path(file)
        missing_version = version(r["item_id"] for r in rows)
        if restore_version is not None and (not file["path"] or path.exists() or missing_version != restore_version):
            raise FileRefusal("Plățile s-au schimbat. Verifică fișierul din nou.")
        if restore_version is not None:
            downloads_ = file_downloads(ledger, file_id)
            latest_id = downloads_[0]["id"] if downloads_ else 0
            if not ledger.answer_file_not_imported(file_id, company, file["revision"], latest_id):
                raise FileRefusal("Fișierul s-a schimbat. Verifică-l din nou.")
            file = ledger.payment_file(file_id)
        if restore_version is None and file["path"] and not path.exists():
            return {"question": "missing", "file_id": file_id, "version": missing_version}
        month, waiting = statement_waits(ledger, company, rows, today)
        if waiting:
            raise FileRefusal(f"Plățile automate așteaptă extrasul pentru {month}.")
        if any(r["status"] == "pending" for r in rows) and not paying_format(company):
            raise FileRefusal("Fișierele pentru banca acestui cont nu sunt acceptate.")
        rows = _without_credits(ledger, rows)
        if not rows:
            raise FileRefusal("Fișierul nu are plăți de descărcat.")
        source = companies.get(company).ron_iban or ""
        items = [replace(_item(r), source_iban=r["source_iban"] or source) for r in rows]
        data = _format(items).build(items, value_date or today)
        ledger.set_meta(
            f"payment_prepare:{file_id}",
            json.dumps({"path": str(path), "revision": file["revision"], "version": hashlib.sha256(data).hexdigest()}),
        )
        return {
            "file": file,
            "path": path,
            "items": items,
            "data": data,
            "rows": rows,
            "value_date": value_date or today,
        }


def download_file(
    ledger: Ledger,
    file_id: int,
    company: str,
    today: date,
    authorize: Callable,
    *,
    restore_version: str | None = None,
    value_date: date | None = None,
) -> dict:
    if not is_live():
        raise PaymentFileError("Payment files require a live installation")
    with pay_lock():
        _recover_named(ledger, today)
        prepared = _prepare_download(ledger, file_id, company, today, authorize, restore_version, value_date=value_date)
        if "question" in prepared:
            return prepared
        with ledger.tx():
            authorize()
            file = open_file(ledger, file_id, company)
            rows = [r for r in file_rows(ledger, file_id) if r["status"] in ("pending", "exported")]
            rows = _without_credits(ledger, rows)
            month, waiting = statement_waits(ledger, company, rows, today)
            if waiting:
                raise FileRefusal(f"Plățile automate așteaptă extrasul pentru {month}.")
            if file["revision"] != prepared["file"]["revision"] or {r["item_id"] for r in rows} != {
                r["item_id"] for r in prepared["rows"]
            }:
                raise FileRefusal("Plățile s-au schimbat. Descarcă fișierul din nou.")
            ledger.prepare_file_download(
                file_id,
                company,
                file["revision"],
                str(prepared["path"]),
                [r["item_id"] for r in rows],
                companies.get(company).ron_iban,
            )
            _publish(prepared["items"], named_path(file), prepared["value_date"])
            ident = ledger.record_file_download(
                file_id,
                company,
                prepared["file"]["revision"],
                hashlib.sha256(prepared["data"]).hexdigest(),
                [r["item_id"] for r in prepared["rows"]],
                prepared["value_date"].isoformat(),
            )
            if not ident:
                raise FileRefusal("Fișierul s-a schimbat. Descarcă-l din nou.")
            ledger.set_meta(f"payment_prepare:{file_id}", "")
        return {
            "file_id": file_id,
            "download_id": ident,
            "data": prepared["data"],
            "name": prepared["path"].name,
            "content_type": "text/csv" if prepared["path"].suffix == ".csv" else "application/octet-stream",
        }


def import_file(ledger: Ledger, file_id: int, company: str, download_id: int, today: date) -> bool:
    if not is_live():
        raise PaymentFileError("Payment files require a live installation")
    with pay_lock():
        _recover_named(ledger, today)
        with ledger.tx():
            file = open_file(ledger, file_id, company)
            downloads_ = file_downloads(ledger, file_id)
            pick = next((d for d in downloads_ if d["id"] == download_id), None)
            rows = file_rows(ledger, file_id)
            if (
                not pick
                or pick["version"] != downloads_[0]["version"]
                or pick["revision"] != file["revision"]
                or not set(pick["items"]) <= {r["item_id"] for r in rows}
                or not {r["item_id"] for r in rows if r["status"] in ("pending", "exported")} <= set(pick["items"])
            ):
                raise FileRefusal(
                    "Versiunea nu mai este actuală. Verifică în bancă ce plăți ai importat înainte să continui."
                )
            source = named_path(file)
            # The file on disk is what the bank got last: a row that is not those bytes (two downloads recorded in one
            # second before #51 can order wrong) is never imported; a new download settles it.
            if source.exists() and hashlib.sha256(source.read_bytes()).hexdigest() != pick["version"]:
                raise FileRefusal("Descarcă fișierul din nou înainte să-l imporți.")
            dest = finance_home() / company / "plati" / f"payment-file-{file_id}{named_path(file).suffix}"
            job = {
                "src": str(source),
                "dest": str(dest),
                "ids": pick["items"],
                "vd": pick["value_date"],
                "version": pick["version"],
            }
            if not ledger.close_payment_file(file_id, company, file["revision"], json.dumps(job)):
                return False
        _finish_named_import(ledger, file_id, job)
        return True


def _finish_named_import(ledger: Ledger, file_id: int, job: dict) -> None:
    file = ledger.payment_file(file_id)
    if not file or not file["imported_at"]:
        raise PaymentFileError("Invalid payment import journal")
    src, dest = Path(job["src"]), Path(job["dest"])
    expected = finance_home() / file["company"] / "plati" / f"payment-file-{file_id}{src.suffix}"
    if (
        dest != expected
        or dest.is_symlink()
        or not dest.resolve().is_relative_to(finance_home().resolve() / file["company"])
    ):
        raise PaymentFileError("Invalid payment archive")
    named_path(dict(file) | {"path": str(src)})
    rows = [r for r in file_rows(ledger, file_id) if r["item_id"] in set(job["ids"])]
    if {r["item_id"] for r in rows} != set(job["ids"]):
        raise PaymentFileError("Payment import membership changed")
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.exists() and hashlib.sha256(src.read_bytes()).hexdigest() == job["version"]:
            src.replace(dest)
        else:
            _publish([_item(r) for r in rows], dest, date.fromisoformat(job["vd"]))
    if hashlib.sha256(dest.read_bytes()).hexdigest() != job["version"]:
        raise PaymentFileError("Payment archive differs from the downloaded version")
    ledger.finish_payment_file(file_id, str(dest), job["ids"])
    if src.exists():
        src.unlink()
    ledger.set_meta(f"payment_import:{file_id}", "")


def _recover_named(ledger: Ledger, today: date) -> list[dict]:
    for key, raw in ledger.db.execute("SELECT key,value FROM meta WHERE key LIKE 'payment_prepare:%'").fetchall():
        if raw:
            _finish_download_attempt(ledger, int(key.split(":")[1]), json.loads(raw))
    for key, raw in ledger.db.execute("SELECT key,value FROM meta WHERE key LIKE 'payment_delete:%'").fetchall():
        if raw:
            _finish_file_delete(ledger, int(key.split(":")[1]))
    done = _quarantine(ledger, pending_only=is_live())
    if not is_live():
        return done
    for key, raw in ledger.db.execute("SELECT key,value FROM meta WHERE key LIKE 'payment_import:%'").fetchall():
        if raw:
            job = json.loads(raw)
            try:
                _finish_named_import(ledger, int(key.split(":")[1]), job)
            except (PaymentFileError, OSError):
                done.append({"file": job["src"], "held": "payment import recovery needs attention"})
            else:
                done.append({"file": job["src"], "action": "import finished"})
    return done


def resolve_missing_file(ledger: Ledger, file_id: int, company: str, expect: str, today: date, imported: bool) -> bool:
    if not imported:
        return "download_id" in download_file(ledger, file_id, company, today, lambda: None, restore_version=expect)
    if not is_live():
        raise PaymentFileError("Payment files require a live installation")
    with pay_lock():
        with ledger.tx():
            file = open_file(ledger, file_id, company)
            rows = file_rows(ledger, file_id)
            active = [r for r in rows if r["status"] in ("pending", "exported")]
            if not file["path"] or named_path(file).exists() or version(r["item_id"] for r in active) != expect:
                return False
            downloads_ = file_downloads(ledger, file_id)
            latest = downloads_[0] if downloads_ else None
            if (
                not latest
                or latest["revision"] != file["revision"]
                or not set(latest["items"]) <= {r["item_id"] for r in rows}
                or not {r["item_id"] for r in active} <= set(latest["items"])
                or not latest["value_date"]
            ):
                raise FileRefusal(
                    "Versiunea nu mai este actuală. Verifică în bancă ce plăți ai importat înainte să continui."
                )
            job = {
                "src": str(named_path(file)),
                "dest": str(finance_home() / company / "plati" / f"payment-file-{file_id}{named_path(file).suffix}"),
                "ids": latest["items"],
                "vd": latest["value_date"],
                "version": latest["version"],
            }
            if not ledger.close_payment_file(file_id, company, file["revision"], json.dumps(job)):
                return False
        _finish_named_import(ledger, file_id, job)
        return True


def assign_automatic_files(ledger: Ledger, today: date, company: str | None = None) -> None:
    """A supplier rule uses the newest open file; only its statement guard controls publication."""
    with pay_lock(), ledger.tx():
        for row in ledger.payments(company=company, status="pending"):
            if row["file_id"] is not None or row["origin"] == "person":
                continue
            co = row["company"]
            ident = next(
                (
                    f["id"]
                    for f in named_files(ledger, co)
                    if f["status"] == "open" and f["question"] is None and f["downloaded_at"] is None
                ),
                None,
            )
            if ident is not None and (file := ledger.payment_file(ident))["path"] and not named_path(file).exists():
                continue
            if ident is None:
                ident = ledger.create_payment_file(co, default_name(ledger, co, today))
            if not ledger.attach_payment_file(ident, co, row["item_id"]):
                raise PaymentFileError("Payment changed while assigning its file")


def delete_file(ledger: Ledger, file_id: int, company: str) -> bool:
    with pay_lock():
        with ledger.tx():
            open_file(ledger, file_id, company)
            if not ledger.delete_payment_file(file_id, company):
                return False
        _finish_file_delete(ledger, file_id)
        return True


def _finish_file_delete(ledger: Ledger, file_id: int) -> None:
    raw = ledger.get_meta(f"payment_delete:{file_id}")
    if raw:
        file = json.loads(raw)
        path = named_path(file)
        if path.exists():
            path.unlink()
        ledger.set_meta(f"payment_delete:{file_id}", "")


def _finish_download_attempt(ledger: Ledger, file_id: int, job: dict) -> None:
    file = ledger.payment_file(file_id)
    if not file:
        ledger.set_meta(f"payment_prepare:{file_id}", "")
        return
    source = named_path(dict(file) | {"path": job["path"]})
    recorded = file_downloads(ledger, file_id)
    delivered = any(d["version"] == job["version"] and d["revision"] == job["revision"] for d in recorded)
    if source.exists() and not delivered and hashlib.sha256(source.read_bytes()).hexdigest() == job["version"]:
        dest = (
            finance_home() / file["company"] / "payment-files" / ".unpublished" / f"{file_id}-{job['version']}.blocked"
        )
        if not dest.resolve().is_relative_to(finance_home().resolve() / file["company"]):
            raise PaymentFileError("Invalid unpublished payment storage")
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.replace(dest)
    ledger.set_meta(f"payment_prepare:{file_id}", "")


def clear_missing_inactive_file(ledger: Ledger, file_id: int, company: str, expect: str) -> bool:
    with pay_lock(), ledger.tx():
        file = open_file(ledger, file_id, company)
        rows = file_rows(ledger, file_id)
        if any(r["status"] in ("pending", "exported") for r in rows):
            return False
        if not file["path"] or named_path(file).exists() or expect != version([]):
            return False
        snapshots = file_downloads(ledger, file_id)
        latest = snapshots[0]["id"] if snapshots else 0
        if not ledger.answer_file_not_imported(file_id, company, file["revision"], latest):
            return False
        if not ledger.clear_inactive_file_path(file_id, company, file["revision"] + 1):
            raise FileRefusal("Fișierul s-a schimbat. Verifică-l din nou.")
        return True


def prepare_files(ledger: Ledger, today: date, company: str | None = None) -> list[dict]:
    """Queue approved rows into named files locally; only explicit per-file download publishes bytes."""
    assign_automatic_files(ledger, today, company=company)
    return [
        f
        for co in ([company] if company else companies.all(ledger))
        for f in named_files(ledger, co)
        if f["status"] == "open"
    ]


def refresh_to_pay(ledger: Ledger, today: date | None = None) -> list[dict]:
    with pay_lock():
        return _recover_named(ledger, today or date.today())


def import_in(ledger: Ledger, company: str, co: companies.Company | None = None, *, rows: list | None = None) -> str:
    co = co or companies.get(company, ledger)
    exported = next((r for r in rows or [] if r["source_iban"]), None)
    fmt = payfiles.for_account(co.account(exported["source_iban"])) if exported else paying_format(company, co)
    return fmt.app if fmt else "aplicația băncii"


def bank_change_reason(ledger: Ledger, company: str) -> str | None:
    unresolved = "Există un fișier de plăți neimportat. Confirmă importul înainte să schimbi contul."
    summaries = {f["id"]: f for f in named_files(ledger, company)}
    for file in ledger.payment_files(company=company):
        if any(
            ledger.get_meta(f"{kind}:{file['id']}") for kind in ("payment_import", "payment_prepare", "payment_delete")
        ):
            return unresolved
        if summaries[file["id"]]["status"] == "open" and file["path"]:
            return unresolved
    if any(
        Path(path).parent == finance_home() / company / "payment-files"
        or Path(path).name.startswith(f"payments-{company}.")
        or Path(path).name.startswith(f"payments-{company}-")
        for path in orphans(ledger)
    ):
        return "Există un fișier de plăți fără plăți asociate. Verifică-l înainte să schimbi contul."
    return None


def bank_change_blocked(ledger: Ledger, company: str) -> bool:
    return bank_change_reason(ledger, company) is not None
