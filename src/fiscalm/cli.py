"""fiscalm command line. JSON on stdout for machine use; short text for humans."""

from __future__ import annotations

import argparse
import email.policy
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

from fiscalm import (
    PRODUCT,
    __version__,
    archive,
    backup,
    companies,
    docs,
    history,
    history_pdf,
    integrations as ig,
    links,
    pack,
    receipt_items,
    recon,
    scheduler,
    setup,
    telemetry,
    updates,
)
from fiscalm.api import actions, state as api, tokens, upload_assignment, uploads as upload_links
from fiscalm.api.mcp import stdio
from fiscalm.bank import connections as bank_connect, enablebanking as eb, feed as bank_feed, statements
from fiscalm.bank.intake import recover
from fiscalm.commerce.shopify import tick
from fiscalm.company_config import CompanyError, bank, change
from fiscalm.config_errors import ConfigError
from fiscalm.efactura import client as anaf, inbox as efactura
from fiscalm.invoicing import issued
from fiscalm.invoicing.cli import register
from fiscalm.ledger import USER, Ledger, is_live, user_note
from fiscalm.mail import invoices as mail_invoices
from fiscalm.migrations import SchemaError
from fiscalm.payments import engine as pay
from fiscalm.payments.orders import PaymentFileError
from fiscalm.products import cli as product_cli
from fiscalm.settlements import engine as settlements
from fiscalm.settlements.reports import LIMIT, ReportError


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _recent_months(n: int = 2) -> list[str]:
    d = date.today().replace(day=1)
    months = []
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
        months.append(d.strftime("%Y-%m"))
    return [date.today().strftime("%Y-%m"), *months][: n + 1]


# ------------------------------------------------------------------ commands


def cmd_update_check(a, L: Ledger) -> int:
    telemetry.ping(L, date.today())
    updates.check(L, date.today())
    return 0


def cmd_sync(a, L: Ledger) -> int:
    """Deterministic pipeline: fetch the accountant's and the bank's attachments, ingest, auto-reconcile, build payment files."""
    recover(L)
    mail = mail_invoices.run(L, date.today())  # invoices, statements and the accountant's OPs by mail
    L.set_meta("last_check_at", datetime.now().isoformat(timespec="seconds"))  # Setări: when mail was last read
    months = set()
    # Shadow (before cut-over): the Mac still archives, so only look, never upload.
    archived = [r for r in archive.archive_all(L, dry_run=not is_live()) if r["status"] != "already there"]
    try:  # the connected banks, once a day; read only, so before going live too (decided 2026-11-03)
        read_banks = bank_feed.run_all(L, date.today())
    except Exception as e:  # one failing provider never stops the statements and payment files
        read_banks = [{"status": "error", "error": type(e).__name__}]
    files = pay.prepare_files(L, date.today())
    refreshed = pay.refresh_to_pay(L)
    for co in companies.all(L):
        for mo in _recent_months(1):
            months.add((co, mo))
    reports = {}
    for co, mo in sorted(months):
        path, summary = recon.write_report(L, co, mo)
        reports[f"{co} {mo}"] = {"report": str(path), **summary}
    actions = []
    for f in files:
        actions.append(
            f"PAY: file {f['id']}, {f['payment_count']} payment(s), {f['total']} RON; download it in the app"
        )
    for co in companies.all(L):
        for mo in _recent_months(2):
            if pack.ready(L, co, mo) and (L.pack(co, mo) or {"status": None})["status"] not in ("prepared", "sent"):
                actions.append(
                    f"PACK: {co} {mo} ready for the accountant (fiscalm pack build --company {co} --month {mo})"
                )
    upload_assignment.process_pending(L)
    receipt_items.extract_pending(L)
    _out(
        {
            "mode": "live" if is_live() else "shadow",
            "mail": mail,
            "drive_archive": archived,
            "payment_files": files,
            "to_pay_refreshed": refreshed,
            "reports": reports,
            "banks_read": read_banks,
            "actions_needed": sorted(set(actions)),
        }
    )
    L.set_meta("last_run_at", datetime.now().isoformat(timespec="seconds"))  # Setări: the last automatic check
    return 0


def cmd_ingest(a, L: Ledger) -> int:
    res = [recon.ingest_file(L, Path(f), operator=True) for f in a.files]
    pay.refresh_to_pay(L)  # a statement may have just settled a whole file
    _out(res)
    return 0 if all(r["status"] not in ("refused", "error") for r in res) else 2


def cmd_doc_note(a, L: Ledger) -> int:
    row = (
        L.document(a.file)
        or L.db.execute("SELECT * FROM documents WHERE path=?", (str(Path(a.file).expanduser().resolve()),)).fetchone()
    )
    if not row:
        print(f"unknown document: {a.file}")
        return 1
    L.set_document_note(row["sha"], a.text)
    print(f"note set: {row['vendor']} {row['doc_date']}")
    return 0


def cmd_doc_inspect(a, L) -> int:
    ev = docs.inspect(a.file)
    _out(ev.to_dict())
    return 0


def cmd_doc_add(a, L: Ledger) -> int:
    try:
        res = docs.add_receipt(
            L,
            a.company,
            a.file,
            month=a.month,
            vendor=a.vendor or "",
            doc_date=a.date,
            amount=a.amount or "",
            currency=(a.currency or "").upper(),
            source=a.source or "",
            allow_ro_supplier=a.allow_ro_supplier,
            supporting=a.supporting or "",
        )
    except docs.DocRefused as e:
        _out({"status": "refused", "reason": str(e)})
        return 2
    _out(res)
    return 0


def cmd_pay_build(a, L: Ledger) -> int:
    file = L.payment_file(a.file_id)
    if not file:
        return 1
    today = date.today()
    value_date = datetime.strptime(a.value_date, "%d/%m/%Y").date() if a.value_date else today
    try:
        got = pay.download_file(L, a.file_id, file["company"], today, lambda: None, value_date=value_date)
    except PaymentFileError as exc:
        print(f"refused: {exc}")
        return 1
    _out({k: v for k, v in got.items() if k != "data"} | {"file": str(pay.named_path(L.payment_file(a.file_id)))})
    return 0 if "download_id" in got else 1


def cmd_pay_recover(a, L: Ledger) -> int:
    _out(pay.refresh_to_pay(L))
    return 0


def cmd_pay_imported(a, L: Ledger) -> int:
    file = L.payment_file(a.file_id)
    if not file:
        return 1
    try:
        ok = pay.import_file(L, a.file_id, file["company"], a.download_id, date.today())
    except PaymentFileError as exc:
        print(f"refused: {exc}")
        return 1
    return 0 if ok else 1


def cmd_pay_restore(a, L: Ledger) -> int:
    file = L.payment_file(a.file_id)
    if not file:
        return 1
    try:
        ok = pay.resolve_missing_file(L, a.file_id, file["company"], a.version, date.today(), False)
    except PaymentFileError as exc:
        print(f"refused: {exc}")
        return 1
    return 0 if ok else 1


def cmd_pay_paid(a, L: Ledger) -> int:
    ids = list(a.item_ids)
    if a.file:
        ids += [p["item_id"] for p in L.payments(status="exported") if p["csv_path"] == str(Path(a.file).resolve())]
    n = sum(L.mark_paid(i, a.note or "marked paid by the user") for i in ids)
    print(f"marked {n} payment(s) paid")
    _out(pay.refresh_to_pay(L))
    return 0


def cmd_pay_skip(a, L: Ledger) -> int:
    ids = list(a.item_ids)
    if a.file:
        ids += [
            p["item_id"]
            for p in L.payments(status=("pending", "exported"))
            if p["csv_path"] == str(Path(a.file).resolve())
        ]
    n = sum(L.mark_skipped(i, a.reason) for i in ids)
    print(f"skipped {n} payment(s): {a.reason}")
    if n < len(ids):
        print(f"{len(ids) - n} not open (unknown id, already paid or skipped)")
    for d in pay.refresh_to_pay(L):
        print(f"{d['action']}: {d['file']}")
    return 0 if n == len(ids) and ids else 1


def cmd_pay_ambiguous(a, L: Ledger) -> int:
    _out([x | {"company": co} for co in companies.all(L) for x in api.ambiguous(L, co, is_live())])
    return 0


def cmd_pay_resolve(a, L: Ledger) -> int:
    rec = next(iter(L.ambiguous(key=a.tx)), None)
    try:
        kind, cid = efactura.resolve_debit(L, rec["company"] if rec else "", a.tx, a.target)
    except efactura.ResolveError as e:
        _out({"status": "error", "error": str(e)})
        return 1
    _out({"status": "paid", "kind": kind, "id": cid, "tx": a.tx})
    return 0


def cmd_pay_validate(a, L) -> int:
    fmt = pay.paying_format(a.company)
    errs = (
        fmt.check(Path(a.file).read_bytes(), a.company)
        if fmt
        else ["no payment file format for this company's paying account"]
    )
    _out({"file": a.file, "errors": errs})
    return 0 if not errs else 2


def cmd_efactura_sync(a, L: Ledger) -> int:
    res = efactura.sync(L)
    if a.json:
        _out(res)
    else:
        print(
            f"e-Factura ({res['env']}): {len(res['new'])} new, {len(res['paid'])} marked paid"
            + (f"; errors: {'; '.join(res['errors'])}" if res["errors"] else "")
        )
        for warning in res.get("warnings", []):
            print("Warning: " + warning)
    return 0 if not res["errors"] else 2


def cmd_efactura_auth_status(a, L: Ledger) -> int:
    _out(anaf.token_state(L) | {"env": anaf.env(), "connection": anaf.connection(anaf.instance(L))})
    return 0


def cmd_efactura_refresh(a, L: Ledger) -> int:
    try:
        anaf.refresh(L)
    except (anaf.AnafError, anaf.secrets.SecretError) as e:
        _out({"status": "error", "error": str(e)})
        return 2
    _out(anaf.token_state(L) | {"status": "refreshed"})
    return 0


def cmd_efactura_list(a, L: Ledger) -> int:
    rows = efactura.unpaid(L) if not a.all else [dict(r) for r in L.efactura_rows()]
    _out(
        [
            {
                k: r.get(k)
                for k in (
                    "message_id",
                    "company",
                    "supplier_name",
                    "number",
                    "issue_date",
                    "due_date",
                    "total",
                    "currency",
                    "status",
                    "paid_note",
                    "parse_error",
                )
            }
            for r in rows
        ]
    )
    return 0


def cmd_efactura_approve(a, L: Ledger) -> int:
    company = _efactura_company(L, a.message_ids)
    if company is None:
        print("Select invoices from one company.", file=sys.stderr)
        return 2
    try:
        ident = pay.add_invoices(
            L,
            company,
            a.message_ids,
            file_id=a.file_id,
            name=(a.new_file or pay.default_name(L, company, date.today())) if a.file_id is None else None,
            approve_invoice=efactura.approve,
        )
    except (efactura.ApproveError, PaymentFileError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _out({"file_id": ident})
    return 0


def _efactura_company(L: Ledger, ids: list[str]) -> str | None:
    rows = [L.efactura(mid) for mid in ids]
    owners = {r["company"] for r in rows if r}
    return next(iter(owners)) if all(rows) and len(owners) == 1 else None


def cmd_efactura_evidence(a, L: Ledger) -> int:
    _out(efactura.evidence(L))
    return 0


def cmd_efactura_autopay(a, L: Ledger) -> int:
    try:
        sup = (
            efactura.set_autopay(L, a.message_id, on=not a.off)
            if a.message_id
            else {k: v for co in companies.all(L) for k, v in efactura.autopay_suppliers(L, co).items()}
        )
    except efactura.ApproveError as e:
        print(str(e))
        return 2
    _out(
        {
            "autopay_suppliers": sorted(sup.values()),
            "now": efactura.autopay_new(L) if a.message_id and not a.off else [],
        }
    )
    return 0


def cmd_efactura_judge(a, L: Ledger) -> int:
    try:
        res = efactura.judge(L)
    except Exception as e:  # the AI provider failing: the next tick tries again
        _out({"status": "error", "error": str(e)})
        return 2
    _out(res)
    return 0


def cmd_efactura_judged(a, L: Ledger) -> int:
    efactura.judged(L, a.message_id, a.mail_keys)
    print(f"{len(a.mail_keys)} mail(s) read for {a.message_id}")
    return 0


def cmd_efactura_paid(a, L: Ledger) -> int:
    note = a.note or "marked paid by the user"
    if a.mail:  # proof from mail: one mail confirms one invoice, reserved like judge's
        if len(a.message_ids) != 1 or len(a.mail) != 1:
            print("--mail confirms exactly one invoice with exactly one mail")
            return 2
        efactura.judged(L, a.message_ids[0], a.mail)
        ok = efactura.confirm_by_mail(L, a.message_ids[0], a.mail[0], note)
        print(
            "marked 1 e-Factura invoice(s) paid"
            if ok
            else "not confirmed: the mail already proves another invoice, or the invoice is not open"
        )
        return 0 if ok else 1
    if not user_note(note):  # the user's word, not proof: it waits for the statement or a mail
        note = f"{USER} {note}"
    n = sum(L.mark_efactura_paid(m, note) for m in a.message_ids)
    print(f"marked {n} e-Factura invoice(s) paid")
    return 0 if n == len(a.message_ids) else 1


def cmd_efactura_skip(a, L: Ledger) -> int:
    n = sum(L.mark_efactura_skipped(m, a.reason) for m in a.message_ids)
    print(f"skipped {n} e-Factura invoice(s): {a.reason}")
    return 0 if n == len(a.message_ids) else 1


def cmd_efactura_reparse(a, L: Ledger) -> int:
    _out(efactura.reparse(L) + issued.reparse(L))
    return 0


def cmd_efactura_sync_issued(a, L: Ledger) -> int:
    res = issued.sync(L)
    if a.json:
        _out(res)
    else:
        print(
            f"e-Factura issued ({res['env']}): {len(res['new'])} new, {len(res['collected'])} collected"
            + (f"; errors: {'; '.join(res['errors'])}" if res["errors"] else "")
        )
        for warning in res.get("warnings", []):
            print("Warning: " + warning)
    return 0 if not res["errors"] else 2


def cmd_efactura_issued(a, L: Ledger) -> int:
    out = []
    for co in [a.company] if a.company else sorted(companies.all(L)):
        v = issued.issued(L, co)
        if not a.all:
            v["rows"] = [r for r in v["rows"] if r["status"] == "neincasata"]
        out.append(v)
    _out(out)
    return 0


def cmd_efactura_collected(a, L: Ledger) -> int:
    if a.method == "other" and not a.note.strip():
        print("error: --note must say how it was collected", file=sys.stderr)
        return 2
    n = sum(issued.mark_collected(L, m, a.method, a.note if a.method == "other" else "") for m in a.message_ids)
    print(f"marked {n} issued invoice(s) collected")
    return 0 if n == len(a.message_ids) else 1


def cmd_efactura_uncollect(a, L: Ledger) -> int:
    n = sum(issued.unmark_collected(L, m) for m in a.message_ids)
    print(f"{n} issued invoice(s) open again")
    return 0 if n == len(a.message_ids) else 1


def cmd_history_import(a, L: Ledger) -> int:
    path = Path(a.file)
    if not path.is_file():
        print("Nothing imported: the file does not exist or is not a regular file.", file=sys.stderr)
        return 1
    try:
        if path.stat().st_size > history.MAX_FILE:
            raise history.Refused("too_big")
        res = history.import_file(L, a.company, path.read_bytes(), a.start, date.today())
    except history.Refused as e:
        print(f"Nothing imported: {e}", file=sys.stderr)
        return 1
    if a.json:
        _out(res.as_dict())
    else:
        print(
            f"{a.company} from {res.start}: {res.issued} issued and {res.received} received tracked, "
            f"{res.history_issued} issued and {res.history_received} received as history, "
            f"{res.duplicates} already there, {res.unreadable_count} unreadable"
        )
        for line in res.unreadable:
            print("  unreadable: " + line)
    return 0


def cmd_history_archive(a, L: Ledger) -> int:
    try:
        if a.history_cmd == "track":
            history.track(L, a.company, a.sha)
        else:
            history.archive_open(L, a.company, a.direction, a.ids)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Invoice tracking updated; no payment was recorded.")
    return 0


def cmd_history_reopen(a, L: Ledger) -> int:
    try:
        history_pdf.reopen(L, a.company, a.sha)
    except history_pdf.ReviewError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    actions.log(f"history-reopen {a.company} {a.sha[:12]}")
    print("The PDF is back in review (Setări › Firme); its history row was removed. No payment changed.")
    return 0


def cmd_history_list(a, L: Ledger) -> int:
    rows = history.rows(L, a.company)
    if a.json:
        _out({"start": history.start_date(L, a.company), "rows": rows})
        return 0
    print(
        f"{a.company}: start {history.start_date(L, a.company) or 'not set'}, "
        f"{sum(r['direction'] == 'issued' for r in rows)} issued and "
        f"{sum(r['direction'] == 'received' for r in rows)} received in history"
    )
    return 0


def cmd_status(a: argparse.Namespace, L: Ledger) -> int:
    open_pay = [dict(p) for p in L.payments(status=("pending", "exported"))]
    packs = _status_packs(L)
    to_import, missing = _status_imports(L, open_pay)
    res = _status_data(L, open_pay, packs, to_import, missing)
    if a.json:
        _out(res)
        return 0
    _print_payment_status(L, res, open_pay, packs)
    _print_efactura_status(L, res)
    return 0


def _status_packs(L: Ledger) -> list[dict]:
    packs = []
    for co in companies.all(L):
        for mo in _recent_months(3):
            pk = L.pack(co, mo)
            if pack.ready(L, co, mo) or (pk and pk["status"] == "prepared"):
                packs.append(
                    {
                        "company": co,
                        "month": mo,
                        "status": pk["status"] if pk else "ready",
                        "unsent_docs": len(pack.pending_docs(L, co, mo)),
                    }
                )
    return packs


def _status_imports(L: Ledger, open_pay: list[dict]) -> tuple[dict, list[str]]:
    files = {f["id"]: f for co in companies.all(L) for f in L.payment_files(company=co)}
    to_import, missing = {}, []
    for p in open_pay:
        file = files.get(p["file_id"])
        if p["status"] != "exported" or not file or file["imported_at"]:
            continue
        if not file["path"] or not Path(file["path"]).exists():
            missing.append(file["path"] or file["name"])
            continue
        group = to_import.setdefault(
            file["id"],
            {
                "id": file["id"],
                "name": file["name"],
                "company": p["company"],
                "file": file["path"],
                "items": [],
                "total": Decimal(0),
            },
        )
        group["items"].append(p["doc_ref"])
        group["total"] += Decimal(p["amount"])
    return to_import, missing


def _print_payment_status(L: Ledger, res: dict, open_pay: list[dict], packs: list[dict]) -> None:
    if not open_pay and not res["efactura_unpaid"]:
        print("Nothing to pay.")
    for f in res["to_import"]:
        print(f"IMPORT {f['company']}: {f['file']} - {len(f['items'])} payment(s), {f['total']} RON")
    for f in res["missing_files"]:
        print(f"MISSING FILE (items exported, file gone; check by hand): {f}")
    for f in res["orphan_files"]:
        print(f"UNKNOWN PAYMENT FILE (not in the ledger; do NOT import before checking): {f}")
    files = {f["id"]: f for co in companies.all(L) for f in L.payment_files(company=co)}
    for p in res["to_pay"]:
        if any(p["file_id"] == f["id"] for f in res["to_import"]):
            continue
        file = files.get(p["file_id"])
        state = (
            "imported, waiting for the statement"
            if file and file["imported_at"]
            else f"in file {file['id']}: {file['name']}"
            if file
            else "pending, choose a payment file"
        )
        print(f"PAY {p['company']}: {p['kind']} {p['doc_ref']} {p['amount']} RON to {p['payee_name']} - {state}")
    for p in L.payments(status="skipped"):
        if p["month"] in _recent_months(3):
            print(
                f"SKIPPED {p['company']}: {p['kind']} {p['doc_ref']} {p['amount']} RON ({p['details']}) - {p['paid_note']}"
            )
    for p in packs:
        print(f"PACK {p['company']} {p['month']}: {p['status']}, {p['unsent_docs']} doc(s) for the accountant")


def _print_efactura_status(L: Ledger, res: dict) -> None:
    today = date.today().isoformat()
    for r in res["efactura_unpaid"]:
        due = r["due_date"] or "no due date"
        late = " OVERDUE" if r["due_date"] and r["due_date"] < today else ""
        state = " (in the payment file, waiting for the statement)" if r["status"] == "linked" else ""
        print(
            f"E-FACTURA {r['company']}: {r['supplier_name']} {r['number']} {r['total']} {r['currency']}, "
            f"due {due}{late}{state} [{r['message_id']}]"
        )
    auth = res["efactura_auth"]
    if not auth["authorized"]:
        print(f"E-FACTURA: not connected to ANAF ({auth['error']})")
    elif auth["refresh_days_left"] <= 30:
        print(
            f"E-FACTURA: ANAF authorization expires {auth['refresh_expires_at'][:10]}: "
            "reconnect in Setări, Integrări, Conectează ANAF"
        )
    for r in efactura.unreadable(L):
        print(f"E-FACTURA {r['company']}: message {r['message_id']} unreadable ({r['parse_error']}); check it in SPV")
    for warning in res["spv_warnings"]:
        print(f"SPV warning: {warning}")
    if res["issued_last_error"]:
        print(f"SPV sent last error: {res['issued_last_error']}")
    if res["efactura_last_error"]:
        print(f"E-FACTURA last error: {res['efactura_last_error']}")
    print(f"last run: {res['last_run_at'] or 'never'}")


def _status_unpaid(L: Ledger) -> list[dict]:
    return [
        {
            k: r.get(k)
            for k in (
                "message_id",
                "company",
                "supplier_name",
                "number",
                "issue_date",
                "due_date",
                "total",
                "currency",
                "status",
            )
        }
        for r in efactura.unpaid(L)
    ]


def _status_data(L: Ledger, open_pay: list[dict], packs: list[dict], to_import: dict, missing: list[str]) -> dict:
    return {
        "to_pay": [
            {k: p[k] for k in ("company", "kind", "doc_ref", "amount", "payee_name", "status", "csv_path", "file_id")}
            for p in open_pay
        ],
        "to_import": [f | {"total": f"{f['total']:.2f}"} for f in to_import.values()],
        "missing_files": sorted(
            set(missing)
            | {
                p["csv_path"]
                for p in open_pay
                if p["status"] == "exported" and p["csv_path"] and not Path(p["csv_path"]).exists()
            }
        ),
        "orphan_files": pay.orphans(L),
        "packs": packs,
        "last_run_at": L.get_meta("last_run_at"),
        "efactura_unpaid": _status_unpaid(L),
        "efactura_auth": anaf.token_state(L),
        "efactura_last_sync": L.get_meta("efactura_last_sync"),
        "efactura_last_error": L.get_meta("efactura_last_error"),
        "issued_last_error": L.get_meta("issued_last_error"),
        "spv_warnings": [L.get_meta(k) for k in ("efactura_warnings", "issued_warnings") if L.get_meta(k)],
    }


def cmd_report(a, L: Ledger) -> int:
    cos = [a.company] if a.company else list(companies.all(L))
    out = {}
    for co in cos:
        path, summary = recon.write_report(L, co, a.month)
        out[co] = {"report": str(path), **summary}
        if a.print:
            print(path.read_text())
    if not a.print:
        _out(out)
    return 0


def cmd_pack_build(a, L: Ledger) -> int:
    _out(pack.build(L, a.company, a.month, summary=lambda: api.pack_summary(L, a.company, a.month)))
    return 0


def cmd_pack_sent(a, L: Ledger) -> int:
    ok = pack.mark_sent(L, a.company, a.month, a.version)
    _out({"ok": ok})
    return 0 if ok else 1


def cmd_statement(a, L) -> int:
    path = Path(a.file)
    co = a.company
    with path.open("rb") as stream:
        is_pdf = stream.read(5) == b"%PDF-"
    if "allmerge" in path.name and is_pdf:
        with tempfile.TemporaryDirectory() as td:
            co, _, out = statements.decrypt_to(path, Path(td))
            st = statements.parse(out, co)
    else:
        st = statements.parse(path, co or "")
    for t in st.txs:
        statements.classify_tx(t)
    res = {
        "company": st.company,
        "month": st.month,
        "accounts": [
            {
                "iban": x.iban,
                "currency": x.currency,
                "transactions": len(x.txs),
                "total_debit": str(x.total_debit),
                "total_credit": str(x.total_credit),
                "balanced": x.balanced,
            }
            for x in st.accounts
        ],
    }
    if a.transactions:
        res["transactions"] = [t.to_dict() for t in st.txs]
    _out(res)
    return 0


def cmd_archive(a, L: Ledger) -> int:
    if not is_live() and not a.dry_run:
        _out({"status": "error", "error": "shadow mode: the Mac still archives until the cut-over; use --dry-run"})
        return 2
    _out(archive.archive_all(L, dry_run=a.dry_run))
    return 0


def cmd_serve(a, L: Ledger) -> int:
    from fiscalm.api import web

    L.close()
    web.serve(a.host, a.port)
    return 0


def cmd_mcp(a, L: Ledger) -> int:
    return stdio(L)


def cmd_upload_link(a, L: Ledger) -> int:
    if a.upload_cmd == "create":
        _out(upload_links.create(L, "ALL" if a.all_firms else a.company, a.label, a.expires_in_days, a.max_files))
    elif a.upload_cmd == "list":
        _out(upload_links.list_links(L, "ALL" if a.all_firms else a.company))
    else:
        _out({"revoked": upload_links.revoke(L, a.id, "ALL" if a.all_firms else a.company)})
    return 0


def cmd_integration_status(a, L: Ledger) -> int:
    """Report integration prerequisites without contacting services or revealing secrets."""
    _out(
        {
            "executables": {name: bool(shutil.which(name)) for name in ("rclone", "claude")},
            "anaf": anaf.connection(anaf.instance(L)),
            "live": is_live(),
        }
    )
    return 0


def cmd_token(a, L: Ledger) -> int:
    try:
        if a.token_cmd == "create":
            owner = L.db.execute(
                "SELECT id FROM users WHERE username=? COLLATE NOCASE AND disabled_at IS NULL", (a.user,)
            ).fetchone()
            if not owner:
                raise ValueError("Unknown or disabled token owner.")
            _out(tokens.create(L, a.name, a.scope, user_id=owner["id"]))
        elif a.token_cmd == "list":
            _out(tokens.list_tokens(L))
        else:
            _out({"revoked": tokens.revoke(L, a.id)})
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


# ------------------------------------------------------------------ integrations


def _pairs(items: list[str] | None) -> dict:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit(f"--set wants key=value, got {it!r}")
        out[k.strip()] = v
    return out


def _stdin_secrets(keys: list[str] | None) -> dict:
    """Secrets come one per line on stdin, in the order of --secret: never on the command line (shell history)."""
    if not keys:
        return {}
    lines = sys.stdin.read().splitlines()
    if len(lines) < len(keys):
        raise SystemExit(f"expected {len(keys)} secret line(s) on stdin, got {len(lines)}")
    return dict(zip(keys, lines, strict=False))


def cmd_integration(a, L: Ledger) -> int:
    try:
        ig.check_listed(L, getattr(a, "kind", None) if a.int_cmd == "add" else "", getattr(a, "id", "") or "")
        if a.int_cmd == "kinds":
            out = ig.kinds()
        elif a.int_cmd == "list":
            out = ig.listing(L, a.kind)
        elif a.int_cmd == "add":
            ig.check_sources(a.kind, _pairs(a.set), a.secret or [])
            out = ig.add(L, a.kind, a.name or "", _pairs(a.set) | _stdin_secrets(a.secret))
        elif a.int_cmd == "update":
            row = next((i for i in ig.listing(L) if i["id"] == a.id), None)
            if row:
                ig.check_sources(row["kind"], _pairs(a.set), a.secret or [])
            out = ig.update(L, a.id, _pairs(a.set) | _stdin_secrets(a.secret), a.name)
        elif a.int_cmd == "clear":
            out = ig.clear(L, a.id, a.key)
        elif a.int_cmd in ("enable", "disable"):
            out = ig.set_enabled(L, a.id, a.int_cmd == "enable")
        elif a.int_cmd == "default":
            out = ig.make_default(L, a.id)
        else:
            ig.remove(L, a.id)
            out = {"removed": a.id}
    except ig.IntegrationError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_company_add(a, L: Ledger) -> int:
    try:
        _out(change(L=L, today=date.today(), cui=a.cui, name=a.name, palette=a.palette))
    except (CompanyError, ConfigError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_company_bank(a, L: Ledger) -> int:
    try:
        _out(bank(L=L, key=a.company, iban=a.iban, replace=a.replace, today=date.today()))
    except (CompanyError, ConfigError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_bank_setup(a, L: Ledger) -> int:
    try:
        cert = eb.generate_key()
    except eb.BankError as e:
        print(f"refused: {e.message}", file=sys.stderr)
        return 2
    print("1. Create an Enable Banking account: https://enablebanking.com/sign-in")
    print('2. API applications > Add a new application: Production, "Generate outside the browser and import')
    print('   public certificate", name fiscalm, your return address (https://<your fiscalm>/bank/return), and this')
    print("   certificate (no need to run their openssl commands):\n")
    print(cert)
    print(
        "3. Paste the application's ID in fiscalm, Setări > Integrări > Enable Banking (or `fiscalm integration add`)."
    )
    print("4. Link the company's accounts once in Enable Banking's panel, then run `fiscalm bank check`.")
    return 0


def cmd_bank_check(a, L: Ledger) -> int:
    results = bank_connect.checks(L)
    for r in results:
        print(("ok    " if r["ok"] else "TODO  ") + r["message"])
    return 0 if results and all(r["ok"] for r in results) and results[-1]["key"] == "return" else 1


def cmd_bank_read(a, L: Ledger) -> int:
    _out(bank_feed.run_all(L, date.today()))
    return 0


def cmd_bank_connections(a, L: Ledger) -> int:
    _out(bank_connect.connections(L))
    return 0


# ------------------------------------------------------------------ parser

BACKUP_COMMANDS = {
    "backup": "write a backup archive of the ledger and documents (secrets only if asked, encrypted)",
    "restore": "restore a backup archive into an empty home, in shadow mode",
}


class Parser(argparse.ArgumentParser):
    """Refuses abbreviated options (`--ho` for `--home`), at every level: subparsers are made with their parent's class.
    On a tool that keeps books, a prefix that happens to match is an accident, not a convenience."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, allow_abbrev=False, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    p = Parser(
        prog="fiscalm", description=f"{PRODUCT}'s engine: invoices, bank statements, payments and the accountant's pack"
    )
    p.add_argument(
        "--home", action=setup.Once, help="override $FISCALM_HOME (default ${XDG_DATA_HOME:-~/.local/share}/fiscalm)"
    )
    p.add_argument("--version", action="version", version=f"{PRODUCT} {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    setup.register(sub)
    scheduler.register(sub)
    for name, hlp in BACKUP_COMMANDS.items():  # listed for --help; main() hands them to backup.py's own parser
        sub.add_parser(name, help=hlp, add_help=False)
    sub.add_parser("mcp", help="serve MCP over stdio using FISCALM_MCP_TOKEN").set_defaults(fn=cmd_mcp)
    u = sub.add_parser("upload-link", help="links an outside person uses to upload a firm's documents")
    u = u.add_subparsers(dest="upload_cmd", required=True)
    for name, hlp in (
        ("create", "make a link and print it once"),
        ("list", "the firm's links, without their secrets"),
        ("revoke", "stop a link from accepting uploads"),
    ):
        s = u.add_parser(name, help=hlp)
        scope = s.add_mutually_exclusive_group(required=True)
        scope.add_argument("--company")
        scope.add_argument("--all-firms", action="store_true")
        s.set_defaults(fn=cmd_upload_link)
        if name == "create":
            s.add_argument("--label", required=True)
            s.add_argument("--expires-in-days", type=int, default=30)
            s.add_argument("--max-files", type=int)
        elif name == "revoke":
            s.add_argument("id")
    t = sub.add_parser("token", help="manage API access").add_subparsers(dest="token_cmd", required=True)
    s = t.add_parser("create", help="make an API token for an account and print it once")
    s.add_argument("--user", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--scope", choices=("read", "write"), required=True)
    s.set_defaults(fn=cmd_token)
    t.add_parser("list", help="the API tokens, without their secrets").set_defaults(fn=cmd_token)
    s = t.add_parser("revoke", help="stop a token from working")
    s.add_argument("id")
    s.set_defaults(fn=cmd_token)

    c = sub.add_parser("company", help="the firms fiscalm keeps books for")
    c = c.add_subparsers(dest="company_cmd", required=True)
    # main() runs it before this parser, with --home anywhere; listed here for --help.
    c.add_parser("check", help="whether a company is configured: reads the ledger, never writes; exit 3 if none")
    s = c.add_parser("add", help="add a firm by its CUI")
    s.add_argument("--cui", required=True)
    s.add_argument("--name")
    s.add_argument("--palette", choices=companies.PALETTES)
    s.set_defaults(fn=cmd_company_add)
    s = c.add_parser("bank", help="set the firm's paying RON account (--replace retires the old one)")
    s.add_argument("--company", required=True)
    s.add_argument("--iban", required=True)
    s.add_argument("--replace", action="store_true")
    s.set_defaults(fn=cmd_company_bank)

    b = sub.add_parser("bank", help="open banking: the install's Enable Banking application and its connections")
    b = b.add_subparsers(dest="bank_cmd", required=True)
    b.add_parser("setup", help="make the application's key and print what to paste in Enable Banking").set_defaults(
        fn=cmd_bank_setup
    )
    b.add_parser("check", help="check the Enable Banking application, as the setup guide does").set_defaults(
        fn=cmd_bank_check
    )
    b.add_parser("connections", help="list the companies' bank connections").set_defaults(fn=cmd_bank_connections)
    b.add_parser("read", help="read the connected banks (once a day) and match what they booked").set_defaults(
        fn=cmd_bank_read
    )

    product_cli.register(sub)
    register_settlement(sub)
    sub.add_parser(
        "sync",
        help="read the mail accounts (invoices, statements, the accountant's OPs), build payment files, write reports",
    ).set_defaults(fn=cmd_sync)
    s = sub.add_parser("ingest", help="ingest statements (BT PDF, CAMT.053 XML or MT940) and payment orders (OP PDFs)")
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_ingest)

    d = sub.add_parser("doc", help="receipts for the pack").add_subparsers(dest="doc_cmd", required=True)
    s = d.add_parser(
        "note", help="explain a receipt for the accountant (Romanian, one sentence; shown in the pack mail)"
    )
    s.add_argument("file", help="the receipt's path in the ledger (or its sha)")
    s.add_argument("text")
    s.set_defaults(fn=cmd_doc_note)
    s = d.add_parser("inspect", help="what fiscalm reads from a receipt PDF, without saving anything")
    s.add_argument("file")
    s.set_defaults(fn=cmd_doc_inspect)
    s = d.add_parser("add", help="add a receipt to a firm's month for the accountant's pack")
    s.add_argument("file")
    s.add_argument("--company", required=True)
    s.add_argument("--month")
    s.add_argument("--vendor")
    s.add_argument("--date", help="YYYY-MM-DD invoice date")
    s.add_argument("--amount", help="invoice total, e.g. 13.80 (for matching only)")
    s.add_argument("--currency")
    s.add_argument("--source", help="mail key or mirrored mail folder")
    s.add_argument("--allow-ro-supplier", action="store_true", help="only if the user confirmed it is not e-Factura")
    s.add_argument(
        "--supporting",
        help="why a document without the company on it backs a company-card payment "
        "(travel ticket/booking, account in the user's name)",
    )
    s.set_defaults(fn=cmd_doc_add)

    pp = sub.add_parser("pay", help="payment files").add_subparsers(dest="pay_cmd", required=True)
    s = pp.add_parser("build", help="download one named payment file and record its version")
    s.add_argument("file_id", type=int)
    s.add_argument("--value-date", help="dd/mm/yyyy (default today)")
    s.set_defaults(fn=cmd_pay_build)
    s = pp.add_parser("recover", help="finish journalled payment-file work")
    s.set_defaults(fn=cmd_pay_recover)
    s = pp.add_parser("imported", help="confirm the exact download imported in the bank")
    s.add_argument("file_id", type=int)
    s.add_argument("download_id", type=int)
    s.set_defaults(fn=cmd_pay_imported)
    s = pp.add_parser("restore", help="explicitly restore one missing named payment file")
    s.add_argument("file_id", type=int)
    s.add_argument("version")
    s.set_defaults(fn=cmd_pay_restore)
    s = pp.add_parser("mark-paid", help="the user says these payments were made (item ids or an exported file)")
    s.add_argument("item_ids", nargs="*")
    s.add_argument("--file", help="mark every item exported in this CSV")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_pay_paid)
    s = pp.add_parser(
        "skip", help="the user won't pay these through fiscalm (e.g. TVA rescheduled): drop them from the to-pay files"
    )
    s.add_argument("item_ids", nargs="*")
    s.add_argument("--file", help="skip every open item in this CSV")
    s.add_argument("--reason", required=True, help="why, in the user's words (shown in reports)")
    s.set_defaults(fn=cmd_pay_skip)
    s = pp.add_parser("ambiguous", help="statement debits that fit several invoices and settled none, with candidates")
    s.set_defaults(fn=cmd_pay_ambiguous)
    s = pp.add_parser("resolve", help="the user says which candidate an ambiguous debit paid (live only)")
    s.add_argument("tx", help="the debit's key (pay ambiguous)")
    s.add_argument("target", help="e-Factura message id or payment item id, one of its candidates")
    s.set_defaults(fn=cmd_pay_resolve)
    s = pp.add_parser("validate", help="check a payment file against the firm's bank format")
    s.add_argument("file")
    s.add_argument("--company", required=True)
    s.set_defaults(fn=cmd_pay_validate)

    hi = sub.add_parser(
        "history", help="a firm's invoices from before fiscalm, exported from another program or SPV"
    ).add_subparsers(dest="history_cmd", required=True)
    s = hi.add_parser(
        "import",
        help="import an e-Factura or SAGA XML, a PDF, a ZIP of these or of SPV messages, or a SmartBill unpaid XLSX",
    )
    s.add_argument("company")
    s.add_argument("file")
    s.add_argument(
        "--from",
        dest="start",
        required=True,
        metavar="YYYY-MM-DD",
        help="the day fiscalm keeps payments from: invoices before it are history, never matched",
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_history_import)
    s = hi.add_parser("track", help="owner claims an XML history invoice is still unpaid")
    s.add_argument("company")
    s.add_argument("sha")
    s.set_defaults(fn=cmd_history_archive)
    s = hi.add_parser(
        "archive-open", help="owner confirms untouched open invoices with identical history copies belong to history"
    )
    s.add_argument("company")
    s.add_argument("direction", choices=("issued", "received"))
    s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_history_archive)
    s = hi.add_parser("reopen", help="owner sends an accepted history PDF back to review, to correct or exclude it")
    s.add_argument("company")
    s.add_argument("sha", help="the row's xml_sha from `history list --json`")
    s.set_defaults(fn=cmd_history_reopen)
    s = hi.add_parser("list", help="the firm's start date and its history invoices")
    s.add_argument("company")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_history_list)

    ef = sub.add_parser(
        "efactura", help="supplier invoices received through ANAF e-Factura (read-only)"
    ).add_subparsers(dest="ef_cmd", required=True)
    s = ef.add_parser("sync", help="fetch new received invoices for every firm, match payments in statements")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_efactura_sync)
    ef.add_parser("auth-status", help="token expiry dates (never the tokens)").set_defaults(fn=cmd_efactura_auth_status)
    ef.add_parser("refresh", help="refresh the access token now").set_defaults(fn=cmd_efactura_refresh)
    s = ef.add_parser("list", help="unpaid received invoices (--all: every row)")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_efactura_list)
    s = ef.add_parser("approve", help="the user wants to pay these by the payment file: payment rows from the XML only")
    s.add_argument("message_ids", nargs="+")
    target = s.add_mutually_exclusive_group(required=True)
    target.add_argument("--file-id", type=int)
    target.add_argument("--new-file", nargs="?", const="")
    s.set_defaults(fn=cmd_efactura_approve)
    s = ef.add_parser("evidence", help="unpaid invoices with payment mails to read")
    s.set_defaults(fn=cmd_efactura_evidence)
    s = ef.add_parser(
        "autopay",
        help="this invoice's supplier is paid monthly by the payment file: its invoices go "
        "into it by themselves (no id: list them)",
    )
    s.add_argument("message_id", nargs="?")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_efactura_autopay)
    ef.add_parser(
        "judge", help="one call to the AI provider (Integrări) reads the payment mails of unpaid invoices"
    ).set_defaults(fn=cmd_efactura_judge)
    s = ef.add_parser("judged", help="these mails were read for this invoice and don't show its payment")
    s.add_argument("message_id")
    s.add_argument("mail_keys", nargs="+")
    s.set_defaults(fn=cmd_efactura_judged)
    s = ef.add_parser("mark-paid", help="paid another way (the user's word, or a payment mail the thread read)")
    s.add_argument("message_ids", nargs="+")
    s.add_argument("--note")
    s.add_argument("--mail", nargs="*", help="mail key(s) that prove it")
    s.set_defaults(fn=cmd_efactura_paid)
    s = ef.add_parser("skip", help="the user won't pay these (disputed, not ours)")
    s.add_argument("message_ids", nargs="+")
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_efactura_skip)
    ef.add_parser("reparse", help="parse stored ZIPs again (after a parser fix), no ANAF call").set_defaults(
        fn=cmd_efactura_reparse
    )
    s = ef.add_parser("sync-issued", help="fetch new issued (sent) invoices for every firm, match collections")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_efactura_sync_issued)
    s = ef.add_parser("issued", help="invoices the companies issued: uncollected ones, totals (--all: every row)")
    s.add_argument("--all", action="store_true")
    s.add_argument("--company")
    s.set_defaults(fn=cmd_efactura_issued)
    s = ef.add_parser("collected", help="the user: collected with no statement credit yet (awaits one)")
    s.add_argument("message_ids", nargs="+")
    s.add_argument("--method", required=True, choices=issued.COLLECTED_METHODS, help="bank is a payment order (OP)")
    s.add_argument("--note", default="", help="for --method other: how, in the user's words")
    s.set_defaults(fn=cmd_efactura_collected)
    s = ef.add_parser("uncollect", help="undo `collected` (never a collection a statement proved)")
    s.add_argument("message_ids", nargs="+")
    s.set_defaults(fn=cmd_efactura_uncollect)

    s = sub.add_parser("status", help="what do I need to pay / send?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("report", help="write report.md per company-month")
    s.add_argument("--month", required=True)
    s.add_argument("--company")
    s.add_argument("--print", action="store_true")
    s.set_defaults(fn=cmd_report)

    pk = sub.add_parser("pack", help="accountant pack").add_subparsers(dest="pack_cmd", required=True)
    s = pk.add_parser("build", help="freeze a firm's month of documents into the accountant's ZIP")
    s.add_argument("--company", required=True)
    s.add_argument("--month", required=True)
    s.set_defaults(fn=cmd_pack_build)

    s = pk.add_parser("mark-sent", help="confirm the exact downloaded accountant ZIP was sent")
    s.add_argument("--company", required=True)
    s.add_argument("--month", required=True)
    s.add_argument("--version", required=True)
    s.set_defaults(fn=cmd_pack_sent)

    s = sub.add_parser("statement", help="parse a statement (BT PDF, CAMT.053 XML or MT940) without saving it")
    s.add_argument("file")
    s.add_argument("--company")
    s.add_argument("--transactions", action="store_true")
    s.set_defaults(fn=cmd_statement)
    s = sub.add_parser("serve", help=f"serve {PRODUCT} (loopback by default)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("archive", help="copy documents into the Google Drive accountancy folders")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_archive)
    register(sub)
    sub.add_parser("update-check", help="check public releases without updating").set_defaults(fn=cmd_update_check)

    sy = sub.add_parser("shopify-sync", help="reconcile Shopify orders and invoice links")

    def shopify_sync(a, L: Ledger) -> int:
        print(json.dumps(tick(L, date.today().isoformat())))
        return 0

    sy.set_defaults(fn=shopify_sync)
    tr = sub.add_parser("tx-rules", help="explain free statement lines by saved rules and the bank's own fee lines")

    def tx_rules(a, L: Ledger) -> int:
        # the rows as Tranzacții shows them: a rule explains only a row left with nothing else proposed
        rows = {co: api.state(L, co)["transactions"]["rows"] for co in companies.all(L)}
        print(json.dumps({co: len(links.apply_rules(L, co, rows[co])) for co in rows}))
        return 0

    tr.set_defaults(fn=tx_rules)
    ig = sub.add_parser(
        "integration",
        help="outside connections (ANAF, AI, mail, storage, open banking, shop): any number of each; secrets are write-only",
    )
    ig.set_defaults(fn=cmd_integration_status)
    ig = ig.add_subparsers(dest="int_cmd")
    ig.add_parser("kinds", help="what can be added and each kind's fields").set_defaults(fn=cmd_integration)
    s = ig.add_parser("list", help="configured integrations (secrets only as set / not set)")
    s.add_argument("--kind")
    s.set_defaults(fn=cmd_integration)
    for name, hlp in (
        ("add", "add one: fiscalm integration add ai --set provider=anthropic --secret api_key < key.txt"),
        ("update", "change fields or replace secrets of one"),
    ):
        s = ig.add_parser(name, help=hlp)
        s.add_argument("kind" if name == "add" else "id")
        s.add_argument("--name")
        s.add_argument("--set", action="append", metavar="KEY=VALUE", help="a plain field (repeatable)")
        s.add_argument("--secret", action="append", metavar="KEY", help="a secret field, its value one line on stdin")
        s.set_defaults(fn=cmd_integration)
    s = ig.add_parser("clear", help="remove one optional secret")
    s.add_argument("id")
    s.add_argument("key")
    s.set_defaults(fn=cmd_integration)
    for name, hlp in (
        ("enable", "use this integration again"),
        ("disable", "stop using this integration, keeping its settings"),
        ("default", "make it the one used for its kind"),
        ("remove", "delete it and its secrets"),
    ):
        s = ig.add_parser(name, help=hlp)
        s.add_argument("id")
        s.set_defaults(fn=cmd_integration)
    return p


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    home, command = setup.split_home(args)
    if home:
        setup.apply_home(home)
    if command[:1] and command[0] in BACKUP_COMMANDS:
        return backup.main(args)
    try:
        if command == ["company", "check"]:  # the tick's gate: 3 without a company; --home may stand anywhere
            return companies.check_configured()
        parser = build_parser()
        if not command:
            parser.print_help()
            return 0
        # A mistyped command stops here: opening the ledger can upgrade its schema.
        a = parser.parse_args(args)
        if hasattr(a, "run"):  # commands that must work before a ledger exists open it themselves, or never
            return a.run(a)
        L = Ledger()
    except SchemaError as error:  # a newer schema or an unfinished restore: the message says what to do
        print(error, file=sys.stderr)
        return 1
    try:
        issued.backfill_own_rows(L)  # every run, serve and the tick included, after an upgrade: before anything reads
        configured = companies.all(L)
        if command[0] == "status" and not configured:
            print("No companies configured. " + companies.NEEDS_COMPANY)
            return 0
        if not configured and command[0] not in {"serve", "mcp", "token", "company", "integration", "update-check"}:
            print(companies.NEEDS_COMPANY, file=sys.stderr)
            return 1
        company = getattr(a, "company", None)
        if company is not None and company not in configured:
            parser.error("unknown company; expected one of " + ", ".join(configured))
        return a.fn(a, L)
    finally:
        L.close()


def cmd_settlement(a, L: Ledger) -> int:
    try:
        if a.operation == "list":
            result = settlements.state(L, a.company)
        elif a.operation == "withdraw":
            if not L.withdraw_report(a.id, a.company):
                raise ReportError("Report is not pending")
            result = {"status": "withdrawn"}
        elif a.operation == "match":
            result = issued.match_recent_statements(L)
        else:
            with Path(a.spec).open("rb") as f:
                spec_data = f.read(16385)
            if len(spec_data) > 16384:
                raise ReportError("Specification too large")
            spec = json.loads(spec_data)
            if not isinstance(spec, dict):
                raise ReportError("Specification must be an object")
            with Path(a.file).open("rb") as f:
                data = f.read(2 * LIMIT + 1)
            if len(data) > 2 * LIMIT:
                raise ReportError("Input too large")
            if a.mail:
                msg = BytesParser(policy=email.policy.default).parsebytes(data)
                senders = getaddresses(msg.get_all("From", []))
                if (
                    len(senders) != 1
                    or not spec.get("mail_sender")
                    or senders[0][1].lower() != spec["mail_sender"].lower()
                ):
                    raise ReportError("Mail sender differs; check the operator specification")
                parts = [p for p in msg.walk() if not p.is_multipart() and p.get_filename() == spec.get("attachment")]
                if len(parts) != 1 or not spec.get("attachment"):
                    raise ReportError("Exactly one named attachment required")
                data = parts[0].get_payload(decode=True)
            result = {"id": settlements.import_report(L, a.company, data, spec)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ReportError as e:
        print(str(e))
        return 1
    except (ValueError, TypeError, AttributeError, OSError, sqlite3.Error):
        # Never echo report content, bank details, input paths or MIME headers.
        print("Report refused; check identity, format, mapping and duplicate imports.")
        return 1


def register_settlement(sub) -> None:
    p = sub.add_parser("settlement", help="import reports and reconcile verified payouts")
    commands = p.add_subparsers(dest="operation", required=True)
    for op, hlp in (
        ("import", "import a payer's settlement report (a file, or one attachment of a saved mail)"),
        ("list", "the firm's reports and their state"),
        ("match", "settle reports whose bank credit has arrived"),
        ("withdraw", "withdraw a pending report"),
    ):
        c = commands.add_parser(op, help=hlp)
        if op != "match":
            c.add_argument("--company", required=True)
        if op == "withdraw":
            c.add_argument("id")
        if op == "import":
            c.add_argument("file")
            c.add_argument("--spec", required=True)
            c.add_argument("--mail", action="store_true", help="read a local RFC822 mail, never follow links")
        c.set_defaults(fn=cmd_settlement)


if __name__ == "__main__":
    sys.exit(main())
