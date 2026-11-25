"""fiscalm API (docs/openapi.json): what the user's app shows, as JSON data.

Amounts are strings with two decimals ("386.40"), dates ISO, never HTML. Every rule about money (what is
owed, overdue, held, ready to send) is decided here, from the ledger; the app only displays it and posts
the user's answers (fiscalm/api/actions.py). `state(L, "ALL")` is the read-only aggregate of every company.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import cached_property, lru_cache
from pathlib import Path

import pikepdf

from fiscalm import (
    __version__,
    accountant,
    companies,
    dashboard,
    expenses,
    history,
    home_charts,
    integrations,
    pack,
    receipt_items,
    recon,
    rules,
    secrets as secret_store,
    telemetry,
    transactions,
    turnover,
    updates,
)
from fiscalm.api import dashboard_rows, tasks, upload_assignment
from fiscalm.api.receipts import inbox
from fiscalm.api.responses import fail, page
from fiscalm.bank import connections as bank_connect, statements
from fiscalm.commerce import emag_sync, state as commerce_state
from fiscalm.efactura import client as anaf, credits as received_credits, inbox as efactura, partners as partner_anaf
from fiscalm.identity import firm_id, firm_name, partner_id, partner_identity
from fiscalm.invoicing import branding, engine as invoicing, issued, recurring
from fiscalm.ledger import (
    DOCUMENT_CORRECTABLE,
    Ledger,
    covered,
    finance_home,
    is_live,
    statement_tx,
    user_note,
    user_words,
)
from fiscalm.mail import imap as imapread, invoices as mail_invoices, reminders
from fiscalm.payments import engine as pay
from fiscalm.payments.engine import pay_lock
from fiscalm.settlements import cod, engine as settlements
from fiscalm.storage import read_file

ALL = "ALL"
MEDIA = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
REPORT_MONTHS = 6  # the current month and five before it
SOON_DAYS = 7  # "Scadente în 7 zile"
DOC_MAX = 25 * 1024 * 1024  # a receipt's file served to the app (phone photos are a few MB)


def clock() -> datetime:
    """Now, for every window below (tests and the fixtures pin it)."""
    return datetime.now()


def money(a) -> str:
    return f"{Decimal(a or 0):.2f}"


def iso_dt(s) -> str | None:
    """'2026-10-26 14:30:11' / '...+03:00' -> '2026-10-26T14:30:11' (local time, no zone)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return str(s)[:19].replace(" ", "T")
    if dt.tzinfo:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def months_back(n: int, today: date) -> list[str]:
    """This month and the n before it, newest first: ['2026-10', '2026-09', ...]."""
    d = today.replace(day=1)
    out = [d.strftime("%Y-%m")]
    for _ in range(n):
        d = (d - timedelta(days=1)).replace(day=1)
        out.append(d.strftime("%Y-%m"))
    return out


def _sum(rows, key: str = "amount") -> Decimal:
    return sum((Decimal(r[key]) for r in rows), Decimal(0))


def _ron(r) -> bool:
    return (r["currency"] or "RON") == "RON"


def _days(iso, today: date) -> int | None:
    return (date.fromisoformat(str(iso)[:10]) - today).days if iso else None


def _soon(iso, today: date) -> bool:
    n = _days(iso, today)
    return n is not None and 0 <= n <= SOON_DAYS


# ------------------------------------------------------------------ ledger reads


def _payment_due(p, ef_due: dict) -> str | None:
    """When a payment row should be paid: the invoice's due date, the 25th for taxes."""
    if p["item_id"] in ef_due:
        return ef_due[p["item_id"]]
    if p["kind"] == "op" and p["issue_date"]:
        d = date.fromisoformat(p["issue_date"])
        d = d.replace(day=25) if d.day <= 25 else (d.replace(day=1) + timedelta(days=32)).replace(day=25)
        return d.isoformat()
    return None


def _payments_by_state(L: Ledger, co: str, company: companies.Company | None = None) -> dict:
    """The company's open payment rows: held (pending), in an open named file, gone with a missing file,
    imported (waiting for the statement). Each row gets its due date and, if any, its e-Factura id."""
    ef = {r["item_id"]: r for r in L.efactura_rows(company=co) if r["item_id"] is not None}
    ef_due = {k: v["due_date"] for k, v in ef.items()}
    files = {r["id"]: r for r in L.payment_files(company=co)}
    out: dict = {"held": [], "in_file": [], "lost": [], "imported": []}
    for p in L.payments(company=co, status=("pending", "exported")):
        p = dict(p) | {
            "due": _payment_due(p, ef_due),
            "ef_id": ef[p["item_id"]]["message_id"] if p["item_id"] in ef else None,
        }
        f = files.get(p["file_id"])
        if f:
            section = (
                "imported"
                if f["imported_at"]
                else "held"
                if p["status"] == "pending"
                else "in_file"
                if p["csv_path"] and Path(p["csv_path"]).exists()
                else "lost"
            )
            out[section].append(p)
            continue
        section = (
            "held"
            if p["status"] == "pending"
            else "in_file"
            if p["csv_path"] and Path(p["csv_path"]).exists()
            else "lost"
        )
        out[section].append(p)
    return out


def _unpaid(st: dict) -> list:
    """Payment rows the user still has to pay: held, in the file, in a missing file (imported rows are their paid orders)."""
    return st["held"] + st["in_file"] + st["lost"]


def _overdue(open_rows: list, st: dict, today: date) -> tuple[list, list]:
    """Overdue = past due and not yet paid by the user: open invoices, and payment rows still waiting to be imported."""
    t = today.isoformat()
    return [r for r in open_rows if r["due_date"] and r["due_date"] < t], [
        p for p in _unpaid(st) if p["due"] and p["due"] < t
    ]


def _late(open_rows: list, st: dict, today: date) -> int:
    invoices, payments = _overdue(open_rows, st, today)
    return len(invoices) + len(payments)


def _open_invoices(L: Ledger, co: str) -> list:
    return [r for r in efactura.unpaid(L, co) if r["status"] == "open"]


def _left(L: Ledger, co: str, rows) -> list[dict]:
    """Received invoice rows for the sums, `total` the part still unpaid (less what debits the user linked settle)."""
    have = covered(L, co, "efactura")
    return [
        dict(r) | {"total": money(max(Decimal(0), Decimal(r["total"]) - have.get(r["message_id"], Decimal(0))))}
        for r in rows
    ]


def _mail_reading(L: Ledger, now: datetime) -> dict:
    """How reading the mail accounts goes (imapread.read_all's outcomes): not_set_up with no account reading, error
    when an account's last read failed, else ok; last_ok is the oldest account's last good read."""
    accounts = imapread.accounts(L)
    if not accounts:
        return {"state": "not_set_up", "last_ok": None, "stale": False, "reconnect": False}
    runs = [json.loads(L.get_meta(f"mail_read:{x.id}") or "{}") for x in accounts]
    oks = [L.get_meta(f"mail_read_ok:{x.id}") for x in accounts]
    m = {
        "state": "error" if any(r.get("ok") is False for r in runs) else "ok" if all(oks) else "unknown",
        "last_ok": min(oks) if all(oks) else None,
    }
    reconnect = any(r.get("code") == "reconnect_required" for r in runs)  # the account needs signing in again
    return {"state": m["state"], "last_ok": iso_dt(m["last_ok"]), "stale": _stale(m, now), "reconnect": reconnect}


def _stale(m: dict, now: datetime) -> bool:
    """Reading mail is behind: an error state, or no successful read for 12 hours."""
    if (m or {}).get("state") not in (None, "ok", "syncing"):
        return True
    ok = (m or {}).get("last_ok")
    try:
        return not ok or now - datetime.fromisoformat(str(ok)[:19]) > timedelta(hours=12)
    except ValueError:
        return True


_STATEMENTS: dict = {}


def _statement(path: Path, co: str):
    """The statement PDF, parsed once per version of the file: it is the slow part of a state (about 0.9 s a company)."""
    data = path.read_bytes()
    key = (co, hashlib.sha256(data).hexdigest())  # replacements must invalidate the parse
    parsed = _STATEMENTS.pop(key) if key in _STATEMENTS else statements.parse(path, co)
    _STATEMENTS[key] = parsed
    if len(_STATEMENTS) > 512:  # 12 months across multiple firms and accounts; evict one, never flush the working set
        del _STATEMENTS[next(iter(_STATEMENTS))]
    return parsed


def _unread(e: Exception) -> str:
    """Why a stored statement can't be read, as a kind the app words; never the exception's own text."""
    if isinstance(e, pikepdf.PasswordError | statements.SecretsUnavailable):
        return "password"
    if isinstance(e, FileNotFoundError):
        return "missing"
    if isinstance(e, statements.StatementError | pikepdf.PdfError | ValueError):
        return "format"
    return "unreadable"


def _listed_day(listed: str | None) -> str | None:
    """The day ANAF listed a message (its data_creare, YYYYMMDDhhmm) as YYYY-MM-DD, or None."""
    try:
        return date(int(listed[:4]), int(listed[4:6]), int(listed[6:8])).isoformat() if listed else None
    except ValueError:
        return None


def _month_view(L: Ledger, co: str, mo: str, tx_rows: list[dict] | None = None) -> dict:
    """Read the month's statements and the same explanations used by Tranzacții and rezumat.csv."""
    if tx_rows is None:
        y, m = map(int, mo.split("-"))
        today = date(y + m // 12, m % 12 + 1, 1)
        reports, _ = settlements.snapshot(L, co)
        tx_rows = _transactions(L, companies.get(co, L=L), today, reports, [])["rows"]
    names = {d["sha"]: statements.masked(Path(d["path"]).name) for d in L.documents(company=co, kind="receipt")}
    rows = [t | {"explanation_text": _explained(t["explanation"], names, t["direction"])} for t in tx_rows]
    return recon.month_view(L, co, mo, rows, parse=_statement)


def log_tail(now: datetime, n: int = 25) -> list[str]:
    p = finance_home() / "logs" / f"finance-{now:%Y-%m}.log"
    try:
        return p.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


_PATH = re.compile(r"(?:/(?:data|home|tmp|Users)/[^\s'\",;:)]+)")


def _redact(line: str) -> str:
    """Log lines leave the server: absolute paths become their file name (no layout or user names)."""
    return _PATH.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1] or "…", line)


def ambiguous(L: Ledger, co: str, live: bool = False) -> list[dict]:
    """Statement debits that settled none and wait for a person (efactura.note_ambiguous), with their candidates."""
    used, fits_of, out = L.used_txs(), efactura.candidates(L, co), []
    for a in L.ambiguous(co):
        if a["tx_key"] in used:
            continue
        t, cands = statements.Tx.from_dict(json.loads(a["tx"])), []
        fits = fits_of(t)  # a candidate paid otherwise since is no longer a choice
        for kind, cid in json.loads(a["candidates"]):
            if (kind, cid) not in fits:
                continue
            r = L.efactura(cid) if kind == "efactura" else L.payment(cid)
            if r and kind == "efactura":
                cands.append(
                    {
                        "id": cid,
                        "kind": kind,
                        "number": r["number"],
                        "amount": money(r["total"]),
                        "currency": r["currency"] or "RON",
                        "date": r["issue_date"],
                    }
                )
            elif r:
                cands.append(
                    {
                        "id": cid,
                        "kind": kind,
                        "number": (r["doc_ref"] or "").strip(),
                        "amount": money(r["amount"]),
                        "currency": "RON",
                        "date": r["issue_date"],
                    }
                )
        if not cands:
            continue
        out.append(
            {
                "tx": a["tx_key"],
                "date": t.date.isoformat(),
                "amount": money(t.amount),
                "currency": t.currency,
                "counterparty": statements.masked(t.counterparty),
                "description": statements.masked(t.description),
                "month": t.date.isoformat()[:7],
                "candidates": sorted(cands, key=lambda c: (c["date"] or "", c["number"] or "")),
                "can_resolve": True,
            }
        )
    return out


# ------------------------------------------------------------------ state, one company


def _refused(L: Ledger, co: str) -> tuple[list[dict], efactura.PaymentFacts]:
    """Stored automation refusals for open invoices.
    Reasons reflect current proof; reading them never queues or retries a payment."""
    rows: dict[str, dict] = {}
    for r in L.db.execute("SELECT key FROM meta WHERE key LIKE 'efactura_autopay_refused:%'"):
        row = L.efactura(r["key"].split(":", 1)[1])
        if row and row["company"] == co and row["status"] == "open":
            rows.setdefault(row["message_id"], row)
    facts = efactura.PaymentFacts(L, L.efactura_rows(company=co))
    refused: list[dict] = []
    for row in rows.values():
        x = efactura.autopay_refusal(L, row, L.get_meta(f"efactura_autopay_refused:{row['message_id']}"), facts)
        refused.append(
            {
                **x,
                "iban_last4": (_payee(row, facts) or "")[-4:],
                "supplier": row["supplier_name"],
            }
        )
    return refused, facts


def _payee(r, facts: efactura.PaymentFacts) -> str:
    """The account fiscalm would pay for this invoice (efactura.payee_iban), else the XML's first."""
    try:
        return efactura.payee_iban(r, facts)
    except efactura.ApproveError:
        return r["supplier_iban"]


def _previous_ibans(r, facts: efactura.PaymentFacts) -> list[str]:
    """Last 4 digits of the supplier's proved accounts when the account fiscalm would pay is not one of them."""
    payee = _payee(r, facts)
    proved = facts.proved(r)
    return sorted(i[-4:] for i in proved if i != payee) if payee and payee not in proved else []


def _invoices(
    L: Ledger, co: str, st: dict, today: date, live: bool, facts: efactura.PaymentFacts | None = None
) -> list[dict]:
    """Every received e-Factura invoice of the company (credit notes, duplicates and unreadable ones aside).
    status: open | linked (in the payment file) | marked (paid by the user, awaiting a statement debit or a mail, can be
    undone) | paid | skipped. `proved`: paid, and evidence proves it (Ledger.paid_proofs: a statement debit, a payment
    mail or the till's bon fiscal); a payment-file payment marked paid by hand is paid but not proved. `bon`: the proving bon.
    `can_approve` is true exactly when `approve` would pass now
    (efactura.payment_refusal, the rule behind `refused[].why`); named-file preparation is local in Sandbox."""
    where = {p["item_id"]: k for k in ("held", "in_file", "lost") for p in st[k]}
    marked = {r["message_id"] for r in efactura.awaiting(L, co)}
    unpaid = {r["message_id"]: r for r in efactura.unpaid(L, co)}
    facts = facts or efactura.PaymentFacts(L, [r for r in unpaid.values() if r["status"] == "open"])
    payments = {p["item_id"]: p for p in L.payments()}
    membership = {}
    if any(p["file_id"] is not None for p in payments.values()):
        membership = {
            r["item_id"]: {"id": r["id"], "name": r["name"]}
            for r in L.db.execute(
                "SELECT p.item_id,f.id,f.name FROM payments p JOIN payment_files f ON f.id=p.file_id WHERE p.company=?",
                (co,),
            )
        }
    proofs = L.paid_proofs(co)
    have = covered(L, co, "efactura")
    credits = received_credits.credit_links(L, co)
    grouped = {c["message_id"] for group in credits.values() for c in group}
    out = []
    for r in L.efactura_rows(company=co):
        if r["parse_error"] or r["status"] == "duplicate" or r["message_id"] in grouped:
            continue
        r = unpaid.get(r["message_id"]) or dict(r)
        corrections = credits.get(r["message_id"], [])
        credit_total = sum((abs(Decimal(c["total"])) for c in corrections), Decimal(0))
        gross = Decimal(r.get("gross_total", r["total"]))
        net = max(Decimal(0), gross - credit_total)
        status = (
            "marked"
            if r["message_id"] in marked or (r["status"] == "paid" and not r["paid_tx"] and user_note(r["paid_note"]))
            else r["status"]
        )
        if status == "credit" or net == 0 and corrections and status in ("open", "marked"):
            status = "skipped"
        if status == "linked" and r["message_id"] not in unpaid:  # its payment-file row is settled: paid, or dropped
            p = payments.get(r["item_id"]) if r["item_id"] else None
            status, r["paid_at"] = (
                ("skipped" if p and p["status"] == "skipped" else "paid"),
                p["paid_at"] if p else None,
            )
        ps = where.get(r["item_id"], "imported") if status == "linked" else None
        owed = received_owed(status, ps)
        proof = proofs.get("efactura " + r["message_id"]) if status == "paid" else None
        part = have.get(r["message_id"], Decimal(0))  # what debits the user linked to it settle
        out.append(
            {
                "id": r["message_id"],
                "company": co,
                "supplier": r["supplier_name"],
                "supplier_cui": tax_id(r.get("supplier_tax_id") or r["supplier_cui"]) or None,
                "partner": None,
                "number": r["number"],  # set by link_partners
                "issue_date": r["issue_date"],
                "due_date": r["due_date"],
                "currency": r["currency"] or "RON",
                "total": money(gross),
                "credits": [
                    {
                        "id": c["message_id"],
                        "number": c["number"],
                        "amount": money(-abs(Decimal(c["total"]))),
                        "date": c["issue_date"],
                    }
                    for c in corrections
                ],
                "credit_note": r["doc_type"] == "credit_note",
                "status": status,
                "payment_state": ps,
                "item_id": r["item_id"],
                "payment_file": membership.get(r["item_id"]),
                "covered": money(part),
                "left": money(max(Decimal(0), net - part) if status in ("open", "marked", "linked") else 0),
                "paid_at": iso_dt(r["paid_at"]) if status in ("marked", "paid", "skipped") else None,
                # How the person said they paid (efactura.paid_method): a fact, never proof.
                "paid_method": r["paid_method"] if status in ("marked", "paid") else None,
                "paid_detail": (r["paid_detail"] or None) if status in ("marked", "paid") else None,
                "overdue": owed and bool(r["due_date"]) and r["due_date"] < today.isoformat(),
                "previous_iban_last4": _previous_ibans(r, facts),
                "can_approve": status in ("open", "linked")
                and not part
                and efactura.payment_refusal(L, r, facts, unfiled=True) is None,
                "can_mark_paid": status == "open",
                "can_withdraw": ps in ("held", "in_file"),
                "can_reopen": status == "marked",
                "proved": proof is not None,
                "bon": proof[4:] if proof and proof.startswith("bon:") else None,
            }
        )
    return sorted(out, key=lambda x: (x["issue_date"] or "", x["number"] or ""), reverse=True)


def doc_roots(co: str) -> list[Path]:
    """Where a document's file may live: fiscalm's own folders for the company."""
    home = finance_home()
    return [home / co, home / "inbox", *home.glob(f"_catchup-*/{co}"), *home.glob(f"_backfill-*/{co}")]


def _servable(path: str | None, roots: list[Path]) -> Path | None:
    try:
        f = Path(path).resolve() if path else None
        ok = f is not None and f.suffix.lower() in MEDIA and f.is_file() and f.stat().st_size <= DOC_MAX
    except (OSError, ValueError):
        return None
    return f if ok and any(f.is_relative_to(r) for r in roots) else None


def receipt_roots(L: Ledger, co: str) -> tuple[list[Path], dict[str, Path]]:
    """Resolved company roots and trusted original upload roots for all its receipts."""
    roots = [r.resolve() for r in doc_roots(co)]
    takeovers = L.db.execute(
        "SELECT u.sha,u.company,u.link_id FROM receipt_uploads u JOIN documents d ON d.sha=u.sha WHERE d.company=?",
        (co,),
    )
    if hasattr(L, "_expense_intake"):
        takeovers = list(takeovers)
        for row in takeovers:
            L._expense_intake.setdefault(row["sha"], set()).add("upload_link" if row["link_id"] else "upload")
    by_company = {company: (finance_home() / company / "documents" / "inbox").resolve() for company in companies.all(L)}
    return roots, {r["sha"]: by_company[r["company"]] for r in takeovers if r["company"] in by_company}


def _receipt_roots(roots: list[Path], originals: dict[str, Path], sha: str) -> list[Path]:
    return roots + ([originals[sha]] if sha in originals else [])


def doc_file(L: Ledger, co: str, sha: str) -> Path | None:
    """The file of one of the company's receipts, or None: only the path the ledger stored, resolved, a PDF or an
    image, strictly inside doc_roots (a path in the ledger never reaches anything else)."""
    d = L.db.execute(
        "SELECT path,source FROM documents WHERE sha=? AND company=? AND kind='receipt'", (sha, co)
    ).fetchone()
    f = _servable(d["path"], _receipt_roots(*receipt_roots(L, co), sha)) if d else None
    if f and (d["source"] or "").startswith("upload:"):
        own = finance_home().resolve() / co / "documents" / "inbox"
        if not f.is_relative_to(own):
            return None
    return f


def read_doc(L: Ledger, co: str, sha: str) -> tuple[str, bytes] | None:
    """doc_file's name and bytes, checked again on the open file. The roots are resolved once, before anything is
    opened; the file is opened without following a final link and without blocking (a FIFO can't stall the
    request); and what was opened (its real path under those roots, a regular file, its size) must pass before a
    byte is read. Swapping the file, a directory or a root between the check and the read serves nothing.
    Linux only: it reads the opened file's path from /proc (and refuses when it can't)."""
    roots = _receipt_roots(*receipt_roots(L, co), sha)
    f = doc_file(L, co, sha)
    return read_file(f, roots, DOC_MAX) if f else None


def _mail_document_type(co: str, sha: str, path: str, stored: str | None) -> str:
    if stored in ("invoice", "receipt"):
        return stored
    return mail_invoices.cached_document_type(co, sha, path)


def _document_metadata(L: Ledger, co: str) -> dict[str, dict]:
    rows = L.db.execute(
        "SELECT documents.sha AS document_sha,documents.path AS document_path,m.*,c.value AS corrections,t.value AS document_type,("
        + DOCUMENT_CORRECTABLE
        + ") AS can_correct FROM documents "
        "LEFT JOIN mail_documents m ON m.sha=documents.sha "
        "LEFT JOIN meta c ON c.key='document-corrections:'||documents.sha "
        "LEFT JOIN meta t ON t.key='document-type:'||documents.sha WHERE documents.company=? AND documents.kind='receipt'",
        (co,),
    )
    result = {}
    for m in rows:
        history = json.loads(m["corrections"] or "[]")
        result[m["document_sha"]] = {
            "number": m["number"] or (history[-1]["value"]["number"] if history else None) or None,
            "document_type": _mail_document_type(co, m["sha"], m["document_path"], m["document_type"])
            if m["sha"] is not None
            else "receipt",
            "can_correct": bool(m["can_correct"]),
            "mail": {
                "sender": m["sender"] or "",
                "subject": m["subject"] or "",
                "date": m["received"],
                "number": m["number"] or None,
            }
            if m["sha"] is not None
            else None,
        }
    return result


def _documents(L: Ledger, co: str) -> list[dict]:
    """The firm's receipts in the accountant's pack (invoices from mail, uploaded photos and files), one row per file."""
    roots, originals = receipt_roots(L, co)
    structured = {r[0] for r in L.db.execute("SELECT sha FROM receipt_items WHERE company=?", (co,))}
    metadata = _document_metadata(L, co)
    if hasattr(L, "_expense_intake"):
        for sha, fields in metadata.items():
            if fields["mail"]:
                L._expense_intake.setdefault(sha, set()).add("mail")
    out, docs = [], L.documents(company=co, kind="receipt")
    named = L.linked_receipts(co, docs)
    for d in docs:
        f = _servable(d["path"], _receipt_roots(roots, originals, d["sha"]))
        out.append(
            {
                "id": d["sha"],
                **metadata.get(
                    d["sha"], {"mail": None, "number": None, "can_correct": False, "document_type": "receipt"}
                ),
                "company": co,
                "vendor": d["vendor"] or "",  # unknown until read: the app names it by its day, never by its file
                "date": d["doc_date"],
                "month": d["month"],
                # An unread receipt has no amount yet: unknown, never zero.
                "amount": money(d["amount"]) if d["amount"] is not None else None,
                "currency": d["currency"] or "RON",
                "note": d["note"] or "",
                "sent": bool(d["sent_at"]),
                "held": pack.held_upload(d, named),
                "invoice": d["efactura_id"],
                "card": d["card_last4"] or None,
                "partner": pack.partner_of(co, d),
                "files": [{"id": d["sha"], "type": "pdf" if f.suffix.lower() == ".pdf" else "image"}] if f else [],
                "items": receipt_items.details(L, co, d["sha"]) if d["sha"] in structured else None,
            }
        )
    return sorted(out, key=lambda x: (x["date"] or x["month"], x["vendor"]), reverse=True)


ISSUED_STATUS = {"neincasata": "open", "incasata": "collected", "marcata": "marked", "stornata": "credited"}


def _issued(L: Ledger, co: str, today: date, live: bool, records: list[dict] | None = None) -> list[dict]:
    """Invoices the company issued (issued.issued), with what the client still owes."""
    out = []
    for r in issued.issued(L, co, today)["rows"]:
        note = r["collected_note"] or ""
        out.append(
            {
                "id": r["message_id"],
                "company": co,
                "number": r["number"],
                "client": r["client_name"],
                "client_cui": tax_id(r.get("client_tax_id") or r["client_cui"]) or None,
                "partner": None,  # set by link_partners
                "issue_date": r["issue_date"],
                "due_date": r["due_date"],
                "currency": r["currency"] or "RON",
                "total": money(r["total"]),
                "credited": r["credited"],
                "covered": r["covered"],
                "owed": r["owed"],
                "left": r["left"],
                "status": ISSUED_STATUS[r["status"]],
                "days_overdue": r["days_overdue"],
                "collected_at": iso_dt(r["collected_at"]),
                "collected_on": r["collected_on"],
                "how": r["how"],
                # How the person said the client paid (issued.COLLECTED_METHODS): a fact, never proof.
                "collected_method": r["collected_method"],
                "note": user_words(note) if r["how"] == "manual" and r["collected_method"] == "other" else "",
                "can_mark": r["can_mark"],
                "can_unmark": r["can_unmark"],
            }
        )
    return issued.merge_local_rows(L, co, out, live, records=records)


def _history(L: Ledger, co: str) -> list[dict]:
    """The firm's invoices from before fiscalm that still count (history.rows), as Vânzări (`issued`) and Cheltuieli
    (`received`) list them beside the tracked ones. They carry no payment state: never owed, matched, paid or reminded,
    and no total or count of what is owed reads them (non-negotiables 2 and 3). `document`: the digest its PDF opens by
    (history-document), including an XML invoice’s companion; null when there is no PDF."""
    return [
        {
            "id": "history:" + r["xml_sha"],
            "kind": "history",
            "sources": ["history"],
            "company": co,
            "direction": r["direction"],
            "number": r["number"],
            "party_name": r["party_name"] or "",
            "party_cui": tax_id(r["party_tax_id"] or r["party_cui"]) or None,
            "issue_date": r["issue_date"],
            "currency": r["currency"] or "RON",
            "total": money(r["total"]),
            "credit_note": r["doc_type"] == "credit_note",
            "document": r["document"],
            "partner": None,  # set by link_partners
        }
        for r in history.rows(L, co)
    ]


def received_owed(status: str, payment_state: str | None) -> bool:
    """A received invoice is still to pay: open, or sent to the bank file but not imported yet (held, in the file, in a
    lost file). The one rule for De achitat, overdue and a partner's balance."""
    return status == "open" or (status == "linked" and payment_state in ("held", "in_file", "lost"))


tax_id = efactura.tax_token  # one rule for a tax id, at ingestion and here


def link_partners(
    co: str,
    invoices: list[dict],
    rows: list[dict],
    L: Ledger | None = None,
    history: Sequence[dict] = (),
    credits: Sequence[dict] = (),
) -> list[dict]:
    """Set `partner` on every received, issued and history invoice (assign_partners), and on each issued credit note
    (`issued_credits`) the partner of the invoice it reduces when there is one; return the company's partners, newest
    activity first."""
    assign_partners(co, invoices, rows, L, history)
    by = {r.get("id"): r["partner"] for r in rows}
    for c in credits:
        c["partner"] = by.get(c.pop("reduces", None) or "") or c["partner"]
    return _partners(co, invoices, rows, history, credits)


def issued_credits(L: Ledger, co: str, own: dict | None = None) -> list[dict]:
    """Issued credit notes that are no row of their own (fiscalm's own stornos are, beside their invoice), shaped as
    history rows for `turnover`: counted on their own date. `reduces`: the invoice they reduce, whose partner
    link_partners gives them; else their client's, when it is a firm."""
    own = invoicing.sent_links(L, co) if own is None else own
    reduces = issued.credit_links(L, co)
    return [
        {
            "reduces": reduces.get(r["message_id"]),
            "party_name": r["client_name"] or "",
            "party_cui": cui if firm_id(cui := tax_id(r["client_tax_id"] or r["client_cui"])) else None,
            "partner": (partner_id(co, cui, r["client_name"]) if cui else None),
            "direction": "issued",
            "issue_date": r["issue_date"] or "",
            "currency": r["currency"] or "RON",
            "total": r["total"],
            "credit_note": True,
        }
        for r in L.issued_rows(company=co)
        if r["status"] == "credit" and not r["parse_error"] and r["message_id"] not in own
    ]


def assign_partners(
    co: str, invoices: list[dict], rows: list[dict], L: Ledger | None = None, history: Sequence[dict] = ()
) -> None:
    """Assign firm tax identities and private saved-client identities consistently for display and writes.
    Only suppliers without a tax id may inherit a unique firm identity by name. An issued row without
    firm identity uses its verified saved client, otherwise its own invoice id: names never merge people.
    A history invoice (state._history) follows the same rules as a tracked one of its direction."""

    def parties():  # (row, name, its tax id's key, issued, from history)
        for r in invoices:
            yield r, r["supplier"], "supplier_cui", False, False
        for r in rows:
            yield r, r["client"], "client_cui", True, False
        for r in history:
            yield r, r["party_name"], "party_cui", r["direction"] == "issued", True

    clients = _issued_clients(L, co) if L is not None else {}
    names = {name: firm_name(name) for name in {name for _, name, _, _, _ in parties()}}
    ids_by_name: dict[str, set[str]] = {}
    for r, name, key, _, old in parties():  # history never changes how a tracked invoice is assigned
        if not old and firm_id(tax_id(r[key])) and names[name]:
            ids_by_name.setdefault(names[name], set()).add(r[key])
    for r, name, key, sold, old in parties():
        cui = r[key]
        if not cui and not sold and len(ids_by_name.get(names[name], ())) == 1:
            cui = next(iter(ids_by_name[names[name]]))
        if (sold or old) and not firm_id(tax_id(cui)):
            cid = clients.get(r.get("id")) or clients.get(r.get("local_id"))
            # A name cannot distinguish two people. Without a saved id, isolate each invoice; another program's
            # export is never trusted to tell a person from a firm, in either direction.
            own = f"{co}.{'issued' if sold else 'received'}-{r['id']}"
            r["partner"] = partner_id(co, None, name, cid) if cid else own
            r[key] = None
        else:
            r["partner"] = partner_id(co, cui, name)


def _issued_clients(L: Ledger, co: str) -> dict[str, str]:
    """Resolve authored invoices and their SPV rows to a saved client in this firm only."""
    local = L.issued_client_links(co)
    return local | {mid: local[iid] for mid, iid in invoicing.sent_links(L, co).items() if iid in local}


def item_partners(L: Ledger, co: str) -> dict[str, str | None]:
    """Each received and issued invoice's partner ('efactura <id>' / 'issued <id>' -> partners[].id), by
    assign_partners: what the user's links name as De la cine and check their invoices against (fiscalm.links)."""
    received = [
        {
            "_item": "efactura " + r["message_id"],
            "supplier": r["supplier_name"],
            "supplier_cui": tax_id(r["supplier_tax_id"] or r["supplier_cui"]) or None,
        }
        for r in L.efactura_rows(company=co)
        if r["status"] not in ("credit", "duplicate")
    ]
    rows = [
        {
            "id": r["message_id"],
            "_item": "issued " + r["message_id"],
            "client": r["client_name"],
            "client_cui": tax_id(r["client_tax_id"] or r["client_cui"]) or None,
        }
        for r in L.issued_rows(company=co)
        if r["status"] not in ("credit", "duplicate")
    ]
    assign_partners(co, received, rows, L)
    return {r["_item"]: r["partner"] for r in (*received, *rows)}


def _partners(
    co: str, invoices: list[dict], rows: list[dict], history: Sequence[dict] = (), credits: Sequence[dict] = ()
) -> list[dict]:
    by: dict[str, dict] = {}

    def entry(pid: str, name: str, cui: str | None, when: str | None) -> dict:
        p = by.setdefault(
            pid,
            {
                "id": pid,
                "company": co,
                "name": "",
                "cui": None,
                "roles": [],
                "last": "",
                "received": 0,
                "issued": 0,
                "_pay": {},
                "_get": {},
                "late": 0,
                "_named": "",
            },
        )
        if name and (when or "") >= p["_named"]:  # the newest invoice that has a name names it
            p["name"], p["_named"] = name, when or ""
        p["last"] = max(p["last"], when or "")
        p["cui"] = p["cui"] or cui
        return p

    for r in invoices:
        if not r["partner"]:
            continue
        p = entry(r["partner"], r["supplier"], r["supplier_cui"], r["issue_date"])
        p["received"] += 1
        if received_owed(r["status"], r["payment_state"]):
            _add_to(p["_pay"], r["currency"], Decimal(r["left"]))
            p["late"] += int(r["overdue"])
    for r in rows:
        if not r["partner"]:
            continue
        p = entry(r["partner"], r["client"], r["client_cui"], r["issue_date"])
        p["issued"] += 1
        if issued_owed(r):
            _add_to(p["_get"], r["currency"], Decimal(r["owed"]))
            p["late"] += int(r["days_overdue"] > 0)
    named = {
        pid for pid, p in by.items() if p["name"]
    }  # fiscalm's own invoices name a partner before another program's
    # Never owed or late. A credit note no row lists (issued_credits) keeps its partner and role, but is no invoice.
    for listed, found in ((True, history), (False, credits)):
        for r in found:
            if r["partner"]:
                name = "" if r["partner"] in named else r["party_name"]
                p = entry(r["partner"], name, r["party_cui"], r["issue_date"])
                if listed:
                    p["issued" if r["direction"] == "issued" else "received"] += 1
                else:
                    p["_credited"] = True
    sums = turnover.by_partner(turnover.entries(invoices, rows, history, credits))
    out = []
    for p in by.values():
        p.pop("_named")
        credited = p.pop("_credited", False)
        p["turnover"] = sums.get(p["id"]) or turnover.empty()
        p["roles"] = [x for x, n in (("supplier", p["received"]), ("client", p["issued"] or credited)) if n]
        for k, key in (("_pay", "payable"), ("_get", "receivable")):
            t = p.pop(k)
            p[key] = {"total": money(t.pop("RON", 0)), "other": _other(t)}
        out.append(p)
    return sorted(out, key=lambda p: (p["last"] or "", p["name"] or ""), reverse=True)


def _document_partners(L: Ledger, co: str, partners: list[dict], documents: list[dict]) -> list[dict]:
    """The suppliers of the firm's receipts join its partners (by name, `pack.partner_of`), each with `not_mailed`:
    whether its documents stay out of the accountant's pack; null for a partner the firm has no document from."""
    held, by, docs = pack.not_mailed(L, co), {p["id"]: p for p in partners}, set()
    for d in documents:
        if d["partner"] and d["partner"] not in by:
            by[d["partner"]] = {
                "id": d["partner"],
                "company": co,
                "name": d["vendor"],
                "cui": None,
                "roles": ["supplier"],
                "last": d["date"] or "",
                "received": 0,
                "issued": 0,
                "late": 0,
                "iban_last4": [],
                "payable": {"total": "0.00", "other": []},
                "receivable": {"total": "0.00", "other": []},
            }
            partners.append(by[d["partner"]])
        docs.add(d["partner"])
    for p in partners:
        p["not_mailed"] = (p["id"] in held) if p["id"] in docs else None
    return partners


def _partner_details(L: Ledger, co: str, partners: list[dict], invoices: list[dict], rows: list[dict]) -> list[dict]:
    """Enrich from local records only. Ambiguous saved identities stay separate."""
    by = {p["id"]: p for p in partners}
    autopay = efactura.autopay_running(L, co)
    for p in partners:
        p.update(
            vat_id=p["cui"] if p["cui"] and not p["cui"].isdigit() else None,
            address=None,
            country=None,
            iban_last4=[],
            ro_vat_status=None,
            client_id=None,
        )
    for table, items in (("efactura", invoices), ("issued", rows)):
        stored = {r["message_id"]: dict(r) for r in getattr(L, table + "_rows")(company=co)}
        # Apply the latest available party address across both invoice directions below.
        for r in items:
            source, p = stored.get(r["id"]), by.get(r["partner"])
            if not source or p is None:
                continue
            address = json.loads(source["party_address"] or "{}")
            if any(address.values()) and (r["issue_date"] or "") >= p.get("_address_date", ""):
                p["address"], p["country"] = address, address.get("country") or None
                p["_address_date"] = r["issue_date"] or ""
            if table == "efactura":
                ibans = json.loads(source["ibans"] or "[]") or [source["supplier_iban"]]
                p["iban_last4"] = sorted(set(p["iban_last4"]) | {i[-4:] for i in ibans if i and len(i) >= 4})
    clients = [
        (r["id"], json.loads(r["data"]))
        for r in L.db.execute("SELECT id,data FROM invoice_clients WHERE company=? ORDER BY id", (co,))
    ]
    identities = [partner_identity(d.get("country", ""), d.get("cui"), d.get("vat_id")) for _, d in clients]
    saved_roles = invoicing.client_roles(L.db, [cid for cid, _ in clients])
    for (cid, d), identity in zip(clients, identities, strict=True):
        matches = (
            [p for p in partners if p["cui"] == identity]
            if identity and identities.count(identity) == 1
            else [p for p in partners if p["id"] == partner_id(co, None, None, cid)]
            if not identity
            else []
        )
        p = matches[0] if len(matches) == 1 else None
        if p is None:
            pid = f"{co}.{identity}" if identity and identities.count(identity) == 1 else f"{co}.client-{cid}"
            p = {
                "id": pid,
                "company": co,
                "name": d["name"],
                "cui": identity or None,
                "roles": [],
                "last": "",
                "received": 0,
                "issued": 0,
                "late": 0,
                "payable": {"total": "0.00", "other": []},
                "receivable": {"total": "0.00", "other": []},
                "iban_last4": [],
            }
            partners.append(p)
        p["roles"] += [r for r in saved_roles[cid] if r not in p["roles"]]
        p.update(
            client_id=cid,
            vat_id=d.get("vat_id") or None,
            country=d.get("country") or None,
            address={k: d.get(k, "") for k in ("street", "city", "county", "postal_code", "country")},
            ro_vat_status=d.get("ro_vat_status"),
            _anaf_cui=partner_anaf.party_cui(d),
        )
    local = {
        r["id"]: partner_anaf.json_object(r["client"])
        for r in L.db.execute(
            """SELECT id,CASE WHEN json_valid(data) THEN json_extract(data,'$.client') END AS client
           FROM invoices WHERE company=? AND status!='draft'""",
            (co,),
        )
    }
    for r in rows:
        p, record = by.get(r["partner"]), local.get(r.get("local_id"))
        if p is not None and record:
            p["_anaf_cui"] = partner_anaf.party_cui(record)
    statuses = {
        r["cui"]: (json.loads(r["status"]) if r["status"] else None, r["error"])
        for r in L.db.execute("SELECT cui,status,error FROM partner_anaf")
    }
    for p in partners:
        if not firm_id(p["cui"]):
            p["address"] = None
        cui = p.pop("_anaf_cui", partner_anaf.eligible_cui(p["country"], p["cui"]))
        p["anaf_status"], p["anaf_error"] = statuses.get(cui, (None, None))
        p["anaf_eligible"] = bool(cui)  # a Romanian firm the tick checks; never a private person or a foreign partner
        p.pop("_address_date", None)
        p.setdefault("held", [])
        p.setdefault("report_formats", [])
        p.setdefault("not_mailed", None)  # a saved client only: no document from it
        p.setdefault("turnover", turnover.empty())  # a saved client or a receipt's vendor: no invoice
        # Whether the supplier's invoices go into the payment file by themselves now (chosen and not paused in
        # Automatizări); null when the firm has no e-Factura invoice from it, so there is nothing to tick.
        p["autopay"] = (p["cui"] in autopay) if p["cui"] and "supplier" in p["roles"] else None
    return sorted(partners, key=lambda p: (p["last"], p["name"]), reverse=True)


def issued_owed(r: dict) -> bool:
    """An issued invoice the client still owes (the one rule for De încasat and a partner's balance)."""
    return r["status"] == "open" and not r.get("rejected", False)


def _add_to(totals: dict, currency: str | None, amount: Decimal) -> None:
    c = currency or "RON"
    totals[c] = totals.get(c, Decimal(0)) + amount


def _totals(rows, cur_key: str = "currency", amount_key: str = "total") -> tuple[Decimal, dict]:
    ron, other = Decimal(0), {}
    for r in rows:
        if (r[cur_key] or "RON") == "RON":
            ron += Decimal(r[amount_key])
        else:
            other[r[cur_key]] = other.get(r[cur_key], Decimal(0)) + Decimal(r[amount_key])
    return ron, other


def _other(other: dict) -> list[dict]:
    return [{"currency": c, "total": money(v)} for c, v in sorted(other.items())]


def _payable(open_rows: list, st: dict, marked: list, today: date) -> dict:
    """De achitat: open invoices and every payment row the user has not imported yet. confirming: paid by the user
    (imported rows, invoices they marked paid), not yet seen on a statement or in a mail. late_total, late_other: the
    overdue part, by the rule `late` counts (_overdue)."""
    ron, other = _totals(open_rows)
    marked_ron, marked_other = _totals(marked)
    late_invoices, late_payments = _overdue(open_rows, st, today)
    late_ron, late_other = _totals(late_invoices)
    late_ron += _sum(late_payments)
    return {
        "total": money(ron + _sum(_unpaid(st))),
        "other": _other(other),
        "late_total": money(late_ron),
        "late_other": _other(late_other),
        "confirming": money(_sum(st["imported"]) + marked_ron),
        "confirming_other": _other(marked_other),
        "due_soon": sum(1 for r in open_rows if _soon(r["due_date"], today))
        + sum(1 for p in _unpaid(st) if _soon(p["due"], today)),
        "late": _late(open_rows, st, today),
    }


def _receivable(rows: list[dict], today: date) -> dict:
    """De încasat: what clients still owe on issued invoices."""
    owed = [r for r in rows if issued_owed(r)]
    ron, other = _totals(owed, amount_key="owed")
    late_ron, late_other = _totals([r for r in owed if r["days_overdue"] > 0], amount_key="owed")
    return {
        "total": money(ron),
        "other": _other(other),
        "late_total": money(late_ron),
        "late_other": _other(late_other),
        "due_soon": sum(1 for r in owed if _soon(r["due_date"], today)),
        "late": sum(1 for r in owed if r["days_overdue"] > 0),
    }


def _statement_hold(
    L: Ledger, co: str, company: companies.Company, st: dict, today: date
) -> tuple[dict | None, set[str]]:
    """Automatic payments held for last month's statement (pay.statement_waits), as one Sarcină: the month and how
    many wait; and their ids. None while a hold the engine checks first applies (no paying account or format, the
    unavailable file format)."""
    if not company.ron_iban or not pay.paying_format(co, company):
        return None, set()
    month, waiting = pay.statement_waits(L, co, st["held"], today)
    hold = {"month": month, "count": len(waiting)} if waiting else None
    return hold, {p["item_id"] for p in waiting}


def _payment(p: dict, waits_statement: bool = False) -> dict:
    return {
        "item_id": p["item_id"],
        "kind": p["kind"],
        "payee": p["payee_name"],
        "ref": (p["doc_ref"] or "").strip(),
        "details": (p["details"] or "").strip(),
        "amount": money(p["amount"]),
        "due": p.get("due"),
        "ef_id": p.get("ef_id"),
        "paid_at": iso_dt(p["paid_at"]),
        "waits_statement": waits_statement,
    }


def _bank(L: Ledger, co: str, mo: str, tx_rows: list[dict] | None = None) -> dict:
    """Statement checks and all monthly transactions, explained by the same projection as Tranzacții."""
    unread = {"statement": True, "balanced": None, "card": [], "efactura_suppliers": 0, "advances": []}
    try:
        v = _month_view(L, co, mo, tx_rows)
    except Exception as e:  # a statement that can't be read must not take the app down
        return unread | {"error": _unread(e)}
    if v.get("file_missing"):
        return unread | {"error": "missing"}

    def row(t, doc):
        return {
            "date": t["date"],
            "who": t["who"],
            "kind": t["kind"],
            "amount": money(t["amount"]),
            "orig": t.get("orig") or "",
            "doc": doc,
            "awaiting": t.get("awaiting", False),
            "currency": t.get("currency", "RON"),
            "direction": t.get("direction", "debit"),
        }  # noqa: E731

    return {
        "statement": bool(v["statement"]),
        "error": None,
        "balanced": v["balanced"],
        "card": sorted(
            [row(t, t.get("doc")) for t in v["matched"]] + [row(t, None) for t in v["missing"]], key=lambda t: t["date"]
        ),
        "efactura_suppliers": v["domestic"],
        "advances": [{"date": x["date"], "amount": money(x["amount"])} for x in v["advances"]],
    }


def _pack(L: Ledger, co: str, mo: str, registry: dict | None = None) -> dict:
    """The accountant's pack for the month: its state, its exact frozen documents and its download version."""
    pk = L.pack(co, mo)
    status = (
        pk["status"]
        if pk and pk["status"] in ("prepared", "sent")
        else "ready"
        if pack.ready(L, co, mo)
        else "preparing"
    )
    if status in ("prepared", "sent"):
        shas = json.loads(L.get_meta(f"pack_shas:{co}:{mo}", "[]"))
        docs = [d for sha in shas if (d := L.document(sha)) and d["company"] == co]
    else:
        sent = L.sent_months(co)
        docs = [d for d in pack.pending_docs(L, co, mo) if d["month"] == mo or d["month"] in sent]
    note = (pk["note"] if pk else "") or ""
    return {
        "status": status,
        "sent_on": note[5:15] if status == "sent" and note.startswith("sent 20") else None,
        "prepared_at": pk["updated_at"] if status == "prepared" else None,
        "version": L.get_meta(f"pack_version:{co}:{mo}"),
        "mark": pack.mark(pk),
        "docs": [
            {
                "kind": "statement" if x["kind"] == "statement" else "invoice",
                "vendor": None if x["kind"] == "statement" else x["vendor"] or Path(x["path"]).name,
                "date": x["doc_date"],
                "note": x["note"] or "",
            }
            for x in docs
        ],
    }


# Fără document, as the accountant reads it (links.LABELS)
PACK_LABELS = {
    "bank_fee": "Comision bancar",
    "interest": "Dobândă",
    "personal": "Cheltuială personală",
    "other": "Altele",
}


def _explained(x: dict | None, names: dict[str, str], direction: str) -> str:
    """What explains a transaction (transactions.view's `explanation`), in words; a proposal explains nothing."""
    if not x:
        return "Lipsește documentul" if direction == "debit" else "Fără explicație"
    if x["kind"] == "label":
        return "; ".join(filter(None, ["Fără document: " + PACK_LABELS.get(x["label"], "Altele"), x.get("note")]))
    if x["kind"] == "advance":
        return "Avans de trezorerie" + (f": {x['who']}" if x.get("who") else "")
    if x["kind"] == "transfer":
        return "Transfer între conturile firmei"
    ids = x.get("invoices") or x.get("issued") or ([x["document"]] if x.get("document") else [])
    words = [names.get(i, "document") for i in ids]
    words += [" ".join(filter(None, [p["payee"], p["ref"]])) for p in x.get("payments") or []]
    return "; ".join(dict.fromkeys(w for w in words if w)) or "Explicată"


def pack_summary(L: Ledger, co: str, month: str, now: datetime | None = None) -> list[list[str]]:
    """The pack's rezumat.csv: every transaction of the month, as Tranzacții shows it, and what explains it. A month
    older than Tranzacții's window is read as on the first day after it, so its transactions are inside the window."""
    now = now or clock()
    if month < transactions.cutoff(now.date())[:7]:
        y, m = map(int, month.split("-"))
        now = datetime(y + m // 12, m % 12 + 1, 1, tzinfo=now.tzinfo)
    return summary_rows(state(L, co, now), month)


def summary_rows(s: dict, month: str) -> list[list[str]]:
    """rezumat.csv's rows for `month`, from one company's state."""
    names = {r["id"]: f"Factura {r['number'] or ''} de la {r['supplier'] or ''}" for r in s["invoices"]}
    names |= {r["id"]: f"Factura emisă {r['number'] or ''} către {r['client'] or ''}" for r in s["issued"]}
    names |= {d["id"]: f"Document {d['vendor'] or ''}" for d in s["documents"]}
    rows = sorted((t for t in s["transactions"]["rows"] if t["date"].startswith(month)), key=lambda t: t["date"])
    return [["Data", "Tip", "Suma", "Moneda", "Partener", "Detalii", "Ce o explică"]] + [
        [
            t["date"],
            "Încasare" if t["direction"] == "credit" else "Plată",
            t["amount"].replace(".", ","),
            t["currency"],
            t["counterparty"] or "",
            t["details"] or "",
            _explained(t["explanation"], names, t["direction"]),
        ]
        for t in rows
    ]


def monthly_report(
    L: Ledger,
    co: str,
    mo: str,
    today: date,
    invoices: list,
    documents: list,
    rows: list,
    to_check: list,
    registry: dict | None = None,
    tx_rows: list[dict] | None = None,
) -> dict:
    return {
        "month": mo,
        "received": [r["id"] for r in invoices if (r["issue_date"] or "").startswith(mo) and r["status"] != "skipped"],
        "documents": [d["id"] for d in documents if d["month"] == mo],
        "issued": [r["id"] for r in rows if (r["issue_date"] or "").startswith(mo)],
        "to_check": [m["key"] for m in to_check if m["month"] == mo],
        "bank": _bank(L, co, mo, tx_rows) if mo < today.isoformat()[:7] else None,
        "not_mailed": sorted({d["vendor"] for d in pack.pending_docs(L, co, mo, left_out=True) if d["month"] == mo}),
        **_pack(L, co, mo, registry),
    }


def _reports(
    L: Ledger,
    co: str,
    today: date,
    invoices: list,
    documents: list,
    rows: list,
    to_check: list,
    registry: dict | None = None,
    tx_rows: list[dict] | None = None,
) -> list[dict]:
    """One report per month, newest first: what came in, what was issued, the statement and its card payments,
    and the accountant's pack. Months before the last one appear only when they hold something."""
    out = []
    for i, mo in enumerate(months_back(REPORT_MONTHS - 1, today)):
        rep = monthly_report(L, co, mo, today, invoices, documents, rows, to_check, registry, tx_rows)
        has = (
            rep["received"]
            or rep["documents"]
            or rep["issued"]
            or rep["to_check"]
            or rep["docs"]
            or (rep["bank"] or {}).get("statement")
            or rep["status"] in ("prepared", "sent")
        )
        if i < 2 or has:
            out.append(rep)
    return out


def _news(L: Ledger, co: str, since: datetime, recurrences: list[dict]) -> list[dict]:
    """What arrived lately, newest first: e-Factura invoices, documents and statements, handled mails."""
    s = since.isoformat(timespec="seconds")
    out = [
        {
            "kind": "invoice",
            "title": r["supplier_name"],
            "at": iso_dt(r["created_at"] or r["issue_date"]),
            "date": r["issue_date"],
            "ref": r["message_id"],
            "result": r["status"],
        }
        for r in efactura.unpaid(L, co)
    ]
    out += [
        {
            "kind": "statement" if x["kind"] == "statement" else "document",
            "title": f"Extras {pack.statement_bank(x)}"
            if x["kind"] == "statement"
            else (x["vendor"] or Path(x["path"]).name),
            "at": iso_dt(x["created_at"]),
            "date": x["doc_date"],
            "ref": x["sha"] if x["kind"] == "receipt" else None,
            "result": "added",
        }
        for x in L.db.execute(
            "SELECT * FROM documents WHERE company=? AND created_at >= ? AND COALESCE(source,'') NOT LIKE 'upload:%' ORDER BY created_at DESC LIMIT 30",
            (co, s),
        )
    ]
    for rule in recurrences:
        made = rule["last_invoice"]
        if made and made["at"] >= s:
            out.append(
                dict(kind="rule", title="Factură lunară emisă", at=made["at"], date=made["at"][:10], ref=made["id"])
                | dict(result=f"{made['number']}, {made['client']}".rstrip(", "))
            )
        event = rule["last_skipped"]
        if event and event["at"] >= s:
            out.append(
                dict(
                    kind="rule",
                    title="Perioade omise",
                    at=event["at"],
                    date=event["at"][:10],
                    ref=rule["id"],
                    result=f"{event['from_period']} - {event['through_period']}",
                )
            )
    return sorted(out, key=lambda x: x["at"] or "", reverse=True)[:30]


def _to_check(L: Ledger, co: str) -> list[dict]:
    """Mailed documents in doubt that this firm is one of the answers to (fiscalm.mail.invoices), oldest first: what was
    read on it and the answers to pick. Untrusted text (sender, subject, supplier), shown as text."""
    out = upload_assignment.questions(L)
    for q in mail_invoices.questions(L):
        d = q["data"]
        if co not in d["companies"]:
            continue
        out.append(
            {
                "key": q["sha"],
                "subject": q["subject"] or "",
                "sender": q["sender"] or "",
                "at": iso_dt(q["created_at"]),
                "month": (d["date"] or q["received"] or "")[:7],
                "date": d["date"],
                "supplier": d["supplier"],
                "number": d["number"] or None,
                "reason": d["reason"],
                "companies": d["companies"],
                "totals": [{"amount": money(a), "currency": c} for a, c in d["totals"]],
                "file": "pdf" if d["kind"] == "pdf" else None,
            }
        )
    return out


SETUP_DISMISSED = "setup_card_dismissed"  # meta: the firms whose Configurează card an administrator closed
SETUP_ITEMS = ("bank", "history", "shops")


def setup_card(L: Ledger, co: str, connected_banks: list) -> dict | None:
    """Missing firm-owned setup. Shared services are configured once in Setări › Integrări.

    Banks and history belong to `co`; a shop's config names exactly one company.
    Dismissal is per firm and never hides installation settings.
    """
    done = {
        "bank": bool(connected_banks)
        or L.db.execute("SELECT 1 FROM statement_txs WHERE company=? LIMIT 1", (co,)).fetchone() is not None,
        "history": history.start_date(L, co) is not None,
        "shops": any(i.config.get("company") == co for i in integrations.enabled(L, "shop")),
    }
    left = [{"key": key, "int": "new:shop" if key == "shops" else None} for key in SETUP_ITEMS if not done[key]]
    hidden = co in json.loads(L.get_meta(SETUP_DISMISSED) or "[]")
    return {"left": left, "hidden": hidden} if left else None


def hide_setup_card(L: Ledger, co: str, hidden: bool) -> None:
    """Close or reopen the Configurează card for one firm; the items stay in Setări. Repeating it changes nothing."""
    with L.tx():
        closed = set(json.loads(L.get_meta(SETUP_DISMISSED) or "[]"))
        closed = closed | {co} if hidden else closed - {co}
        L.set_meta(SETUP_DISMISSED, json.dumps(sorted(closed)))


def _system(L: Ledger, now: datetime, co: str) -> dict:
    a = anaf.token_state(L, now, company=co) if anaf.instance(L, co) else {"status": "not_set_up"}
    ac = a["status"] != "not_set_up"  # also when it was disabled or removed between the two reads
    revoked = a["status"] == "connected" and (
        L.get_meta(f"efactura_revoked:{co}") or L.get_meta(f"issued_revoked:{co}")
    )
    error = (L.get_meta(f"efactura_last_error:{co}") or L.get_meta(f"issued_last_error:{co}") or None) if ac else None
    if not error and a.get("status") == "error":
        error = a.get("error")
    if not ac:
        status = "not_set_up"
    elif a["status"] != "connected":  # error, or not_connected: setup (the Configurează card), not an expiry
        status = a["status"]
    elif a["refresh_days_left"] < 0:
        status = "expired"
    else:
        status = "error" if error else "ok"
    return {
        "version": __version__,
        "updates": updates.state(L),
        "telemetry": telemetry.state(L),
        "configuration_error": None,
        "anaf": {
            "status": status,
            "authorized": bool(a.get("authorized")),
            "expires": str(a["refresh_expires_at"])[:10] if a.get("refresh_expires_at") else None,
            "days_left": a.get("refresh_days_left"),
            "last_sync": iso_dt(L.get_meta("efactura_last_sync")),
            "error": error,
            # needs a person: credentials that can't be read, or SPV access refused; other errors retry on their own
            "reconnect": a["status"] == "error" or bool(revoked),
            "warnings": [
                L.get_meta(f"{k}:{co}")
                for k in ("efactura_warnings", "issued_warnings")
                if ac and L.get_meta(f"{k}:{co}")
            ],
        },
        "mail": _mail_reading(L, now),
        "last_run": iso_dt(L.get_meta("last_run_at")),
        "last_check": iso_dt(L.get_meta("last_check_at")),
        "log": [_redact(x) for x in log_tail(now)],
    }


def _dashboard(
    L: Ledger,
    co: str,
    company: companies.Company,
    today: date,
    invoices: list,
    rows: list,
    st: dict,
    txs: dict,
    balances: list,
    turnover_entries: list,
    partners: list,
) -> dict:
    """Acasă's numbers (fiscalm.dashboard), once invoices and issued rows carry their partner; De explicat is Tranzacții's
    own count (`txs`, fiscalm.transactions.view)."""
    return dashboard.company_view(
        L,
        co,
        company,
        today,
        invoices=invoices,
        rows=rows,
        unpaid_payments=_unpaid(st),
        balances_=balances,
        to_explain=txs["to_explain"],
        received_owed=received_owed,
        issued_owed=issued_owed,
        entries=turnover_entries,
        partners=partners,
    )


def _company_entry(key: str, late: int, registry: dict, logos: dict[str, str]) -> dict:
    """What the app shows of a company in its switch: names, colour scheme, overdue count, and the sha of its invoice
    logo (null without one), so the app draws the logo from a cache-busting URL without asking each firm."""
    c = registry[key]
    return {
        "key": key,
        "name": c.name,
        "legal_name": c.legal_name,
        "palette": c.palette,
        "late": late,
        "logo": logos.get(key),
    }


def _company(L: Ledger, co: str, now: datetime, registry: dict, entries: list[dict]) -> dict:
    today, live = now.date(), is_live()
    st = _payments_by_state(L, co, registry[co])
    open_rows = _open_invoices(L, co)
    refused, facts = _refused(L, co)
    invoices = _invoices(L, co, st, today, live, facts)
    documents = _documents(L, co)
    uploads = inbox(L, co)
    records = invoicing.issued_records(L, co)
    rows = _issued(L, co, today, live, records)
    commerce_view = commerce_state.project(L, co, rows)
    reminder_tasks = reminders.tasks(L, co, today, rows, invoices={r["id"]: r for r in records})
    settlement_reports, settlement_fees = settlements.snapshot(L, co)
    cod_state = cod.state(L, co, rows, today)
    waiting = {r["invoice"] for r in cod_state["rows"] if r["waiting"]}
    for row in rows:
        row["cod_at_courier"] = row["id"] in waiting
    waiting_orders = {r["id"] for r in cod_state["rows"] if r["waiting"]}
    for order in commerce_view["orders"]:
        order["cod_at_courier"] = order["id"] in waiting_orders
    totals = {t["currency"]: t["total"] for t in cod_state["totals"]}
    commerce_view["cod_receivable"] = {
        "count": cod_state["count"],
        "total": totals.pop("RON", "0.00"),
        "other": [{"currency": c, "total": v} for c, v in totals.items()],
    }
    recurrences = recurring.list_rules(
        L, co, today.isoformat()
    )  # read once: the state lists them, the news reads their skips
    to_check = _to_check(L, co)
    statement_hold, _ = _statement_hold(L, co, registry[co], st, today)
    sent_links = invoicing.sent_links(L, co)
    old, credits = _history(L, co), issued_credits(L, co, sent_links)
    partners = _document_partners(L, co, link_partners(co, invoices, rows, L, old, credits), documents)
    settlements.partner_money(L, co, partners, rows, settlement_reports, today)
    partners = _partner_details(L, co, partners, invoices, rows)
    unclear = ambiguous(L, co, live)
    txs = _transactions(L, registry[co], today, settlement_reports, unclear)
    named_files = pay.named_files(L, co)
    turnover_entries = list(turnover.entries(invoices, rows, old, credits))
    skipped_purchases = {r["message_id"] for r in L.efactura_rows(company=co) if r["status"] == "skipped"}
    charts, balances = home_charts.company_view(
        L,
        co,
        registry[co],
        today,
        _statement,
        turnover_entries,
        invoices,
        rows,
        txs["rows"],
        settlement_reports,
        received_owed,
        issued_owed,
        records,
        old,
        sent_links,
    )
    out = {
        "co": co,
        "live": live,
        "today": today.isoformat(),
        **commerce_view,
        "recurrences": recurrences,
        "recurring_refused": recurring.refused(L, co),
        "companies": entries,
        "payable": _payable(_left(L, co, open_rows), st, _left(L, co, efactura.awaiting(L, co)), today),
        "receivable": _receivable(rows, today),
        "invoices": invoices,
        "firm_cards": L.firm_cards(co),
        "documents": documents,
        "expenses": expenses.derive(co, invoices, documents, expenses.evidence(L, co)),
        "inbox": uploads,
        "reminder_tasks": reminder_tasks,
        "settlements": settlement_reports,
        "settlement_tasks": settlements.tasks(settlement_reports),
        "settlement_transactions": settlements.transactions(settlement_reports),
        "cod": cod_state,
        "settlement_fees": settlement_fees,
        "emag_tasks": emag_sync.tasks(L, co),
        "upload_tasks": [{"kind": "to_check", "id": u["id"]} for u in uploads],
        "issued": rows,
        "history": old,
        "partners": partners,
        "accountant": accountant.settings(L, co),
        "partner_tasks": partner_anaf.tasks(partners),
        "dashboard": home_charts.period_statuses(
            {
                **_dashboard(
                    L,
                    co,
                    registry[co],
                    today,
                    invoices,
                    rows,
                    st,
                    txs,
                    balances,
                    list(
                        turnover.entries([r for r in invoices if r["id"] not in skipped_purchases], rows, old, credits)
                    ),
                    partners,
                ),
                "charts": charts,
            }
        ),
        "statement_hold": statement_hold,
        "files": named_files,
        "payment_file_defaults": payment_file_defaults(L, co, today, named_files),
        "reports": _reports(L, co, today, invoices, documents, rows, to_check, registry, txs["rows"]),
        "turnover": {"years": turnover.by_year(turnover_entries)},
        "to_check": [{k: v for k, v in m.items() if k != "month"} for m in to_check],
        "news": _news(L, co, now - timedelta(days=30), recurrences),
        "system": _system(L, now, co),
        # connected banks only: Home asks for a reconnect before one ends (bank_connect.status)
        "bank_connections": [b for b in bank_connect.status(L, registry[co], today) if b["connection"]],
        "rules": rules.list_rules(L, co, registry, getattr(L, "shops", None)),
        "refused": refused,
        "unreadable": [
            {"id": r["message_id"], "listed": _listed_day(r["listed_at"])}
            for r in efactura.unreadable(L)
            if r["company"] == co
        ],
        "ambiguous": unclear,
        "transactions": txs,
    }
    for r in out["reports"]:  # a prepared pack is checked against the summary preparing it now would write
        r["stale"] = r["status"] == "prepared" and pack.stale(L, co, r["month"], summary_rows(out, r["month"]))
    return out


def _transactions(L: Ledger, company: companies.Company, today: date, settlement_reports: list, unclear: list) -> dict:
    """Tranzacții (fiscalm.transactions): the company's statement transactions with what explains them."""
    return transactions.view(
        L,
        company,
        today,
        settlements=settlement_reports,
        ambiguous=unclear,
        live=is_live(),
        partners=item_partners(L, company.key),
    )


# ------------------------------------------------------------------ state, all companies


def _add(parts: list[dict], key: str) -> dict:
    """Sum payable or receivable over the companies: lei, other currencies (other, confirming_other), counts."""

    def by_currency(k: str) -> list[dict]:
        other: dict = {}
        for p in parts:
            for o in p[key][k]:
                other[o["currency"]] = other.get(o["currency"], Decimal(0)) + Decimal(o["total"])
        return _other(other)

    return {
        k: by_currency(k)
        if isinstance(v, list)
        else money(sum((Decimal(p[key][k]) for p in parts), Decimal(0)))
        if isinstance(v, str)
        else sum(p[key][k] for p in parts)
        for k, v in parts[0][key].items()
    }


def setup_state(can_manage_companies: bool) -> dict:
    return {"setup": "needs_company", "companies": [], "live": is_live(), "can_manage_companies": can_manage_companies}


class _StateReads:
    """Read-only snapshot shared by projections within one request. Writers must use the Ledger.

    Callers must not mutate these row lists; actions and later requests use fresh reads.
    """

    def __init__(self, ledger: Ledger, registry: dict) -> None:
        self._expense_intake: dict[str, set[str]] = {}
        self._firm_registry = registry
        self._ledger = ledger
        self._rows = lru_cache(maxsize=None)(self._read_rows)
        self._filtered = lru_cache(maxsize=None)(self._filtered)
        self.documents = lru_cache(maxsize=None)(self.documents)
        for name in (
            "payment_files",
            "paid_proofs",
            "account_lines",
            "link_rows",
            "memories",
            "card_owners",
            "sent_invoice_links",
            "issued_client_links",
        ):
            setattr(self, name, lru_cache(maxsize=None)(getattr(ledger, name)))

    def _read_rows(self, name: str) -> dict:
        grouped: dict = {}
        for row in getattr(self._ledger, name)():
            grouped.setdefault(row["company"], []).append(row)
        return grouped

    def _filtered(self, name: str, status=None, company=None) -> list:
        grouped = self._rows(name)
        rows = grouped.get(company, []) if company else [r for group in grouped.values() for r in group]
        statuses = (status,) if isinstance(status, str) else status
        return [r for r in rows if r["status"] in statuses] if statuses else rows

    def efactura_rows(self, status=None, company=None) -> list:
        return self._filtered("efactura_rows", status, company)

    def issued_rows(self, status=None, company=None) -> list:
        return self._filtered("issued_rows", status, company)

    def payments(self, status=None, company=None) -> list:
        return self._filtered("payments", status, company)

    def documents(self, company=None, month=None, kind=None, unsent_only=False, up_to_month=None) -> list:
        return [
            r
            for r in self._filtered("documents", company=company)
            if (not month or r["month"] == month)
            and (not kind or r["kind"] == kind)
            and (not unsent_only or r["sent_at"] is None)
            and (not up_to_month or r["month"] <= up_to_month)
        ]

    @cached_property
    def _meta(self) -> dict:
        return dict(self.db.execute("SELECT key,value FROM meta"))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self._meta.get(key, default)

    @cached_property
    def shops(self) -> list[integrations.Instance]:
        """The enabled shop integrations, read once for every firm's rules."""
        return integrations.enabled(self._ledger, "shop")

    @cached_property
    def _packs(self) -> dict:
        return {(r["company"], r["month"]): r for r in self.db.execute("SELECT * FROM packs")}

    def pack(self, company: str, month: str):
        return self._packs.get((company, month))

    def sent_months(self, company: str) -> set[str]:
        return {m for (c, m), r in self._packs.items() if c == company and r["status"] == "sent"}

    def __getattr__(self, name: str):
        return getattr(self._ledger, name)


def state(
    L: Ledger,
    co: str,
    now: datetime | None = None,
    *,
    can_manage_companies: bool = False,
    company_entries: list[dict] | None = None,
) -> dict:
    registry = companies.all(L)
    if not registry:
        return setup_state(can_manage_companies)
    reads = L if hasattr(L, "_firm_registry") else _StateReads(L, registry)
    result = _state(reads, co, now, registry, company_entries)
    for part in [result, *result.get("parts", [])]:
        part["can_manage_companies"] = can_manage_companies
        for rule in part.get("rules", []):
            rule.update(rules.capabilities(rule, admin=can_manage_companies))
        may = rules.may_change(admin=can_manage_companies)
        for p in part.get("partners", []):
            p["can_set_autopay"] = may and p.get("autopay") is not None
            p["can_set_not_mailed"] = may and p.get("not_mailed") is not None
    collector = tasks.Collector()
    for part in result.get("parts", [result]):
        part["setup"] = setup_card(L, part["co"], part["bank_connections"]) if can_manage_companies else None
        part["dashboard_pages"] = dashboard_rows.initial(part)
        own = tasks.Collector()
        own.add(part)
        part["task_summary"] = own.result()
        if "parts" in result:
            collector.add(part)
    if "parts" in result:
        result["dashboard_pages"] = dashboard_rows.merge(result["parts"])
        result["task_summary"] = collector.result()
    return result


def entity_rows(
    L: Ledger, co: str, now: datetime | None = None, *, with_sales: bool = False, can_manage_companies: bool = False
) -> dict:
    """Complete searchable entities and identities, without dashboard/report/transaction projections."""
    now = now or clock()
    today, live = now.date(), is_live()
    registry = companies.all(L)
    reads = _StateReads(L, registry)
    st = _payments_by_state(reads, co, registry[co])
    _, facts = _refused(reads, co)
    invoices = _invoices(reads, co, st, today, live, facts)
    documents = _documents(reads, co)
    rows = _issued(reads, co, today, live)
    if with_sales:
        commerce_state.project(reads, co, rows)
        waiting = {r["invoice"] for r in cod.state(reads, co, rows, today)["rows"] if r["waiting"]}
        for row in rows:
            row["cod_at_courier"] = row["id"] in waiting
    old = _history(reads, co)
    partners = link_partners(co, invoices, rows, reads, old, issued_credits(reads, co))
    partners = _document_partners(reads, co, partners, documents)
    reports, _ = settlements.snapshot(reads, co)
    settlements.partner_money(reads, co, partners, rows, reports, today)
    partners = _partner_details(reads, co, partners, invoices, rows)
    may = rules.may_change(admin=can_manage_companies)
    for partner in partners:
        partner["can_set_autopay"] = may and partner.get("autopay") is not None
        partner["can_set_not_mailed"] = may and partner.get("not_mailed") is not None
    return {
        "co": co,
        "expenses": expenses.derive(co, invoices, documents, expenses.evidence(reads, co)),
        "invoices": invoices,
        "issued": rows,
        "history": old,
        "documents": documents,
        "partners": partners,
    }


def company_entries(L: Ledger, registry: dict, today: date) -> list[dict]:
    """Compute the switch with the same request-scoped rows used by the company projections."""
    entries = []
    reads = L if hasattr(L, "_firm_registry") else _StateReads(L, registry)
    logos = branding.current_shas(reads)
    for co in registry:
        late = _late(_open_invoices(reads, co), _payments_by_state(reads, co), today)
        entries.append(_company_entry(co, late, registry, logos))
    return entries


def _state(L: Ledger, co: str, now: datetime | None, registry: dict, entries: list[dict] | None = None) -> dict:
    """GET api/state: one company, or ALL (every company's state in `parts`, with summed payable and receivable).
    ALL is for reading only; every action names a real company."""
    now = now or clock()
    entries = entries if entries is not None else company_entries(L, registry, now.date())
    if co != ALL:
        return _company(L, co, now, registry, entries)
    parts = [_company(L, c, now, registry, entries) for c in registry]
    return {
        "cod_receivable": _add(parts, "cod_receivable"),
        "sales_origins": [dict(id="manual", company=None, label="Manual")]
        + [origin for part in parts for origin in part["sales_origins"] if origin["id"] != "manual"],
        "co": ALL,
        "live": parts[0]["live"],
        "today": parts[0]["today"],
        "companies": parts[0]["companies"],
        "payable": _add(parts, "payable"),
        "receivable": _add(parts, "receivable"),
        "dashboard": home_charts.all_view(parts),
        "parts": parts,
    }


def payment_id(item_id: str | None) -> str | None:
    if not item_id:
        return None
    with pay_lock():
        if not secret_store.has_secret("payment-id-key"):
            secret_store.write_secret("payment-id-key", secrets.token_hex(32))
        key = secret_store.read_secret("payment-id-key")
    return hmac.new(key.encode(), item_id.encode(), hashlib.sha256).hexdigest()


def resource(L: Ledger, kind: str, co: str, params: dict, query: dict) -> object:
    if kind == "document_file":
        got = read_doc(L, co, params["sha"])
        if not got:
            fail(404, "file_not_found", "The file is unavailable. Refresh document metadata.")
        return got
    month = query.get("month", "")
    if month and not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        fail(400, "invalid_month", "Use a month in YYYY-MM format.")
    today = clock().date()
    st = _payments_by_state(L, co)
    received = _invoices(L, co, st, today, is_live())
    issued_rows = _issued(L, co, today, is_live())
    documents = _documents(L, co)
    if kind == "report":
        mo = params["month"]
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", mo):
            fail(400, "invalid_month", "Use a month in YYYY-MM format.")
        rep = {"company": co, **monthly_report(L, co, mo, today, received, documents, issued_rows, _to_check(L, co))}
        rep["stale"] = rep["status"] == "prepared" and pack.stale(L, co, mo, pack_summary(L, co, mo))
        rep["links"] = {k: rep[k] for k in ("received", "issued", "documents")}
        return rep
    if kind.startswith("received"):
        rows = received
        proofs = L.paid_proofs(co)  # an invoice/payment pair shares one statement proof; only a proved one is linked
        for r in rows:
            raw = L.efactura(r["id"])
            tx = statement_tx(proofs.get("efactura " + r["id"])) if r["proved"] else None
            r["links"] = {
                "partner": raw["supplier_cui"] or r["supplier"],
                "payment": payment_id(r["item_id"]),
                "document": r["bon"],
                "transaction": tx,
                "report": (r["issue_date"] or "")[:7],
            }
    elif kind.startswith("issued"):
        rows = issued_rows
        credits = issued.credit_links(L, co)
        for r in rows:
            raw = L.issued(r["id"])
            r["links"] = {
                "order": r.get("order_sheet_id"),
                "partner": (raw["client_cui"] if raw else r.get("client_cui")) or r["client"],
                "transaction": statement_tx(raw["collected_tx"] if raw else None),
                "report": (r["issue_date"] or "")[:7],
                "reversals": list(
                    dict.fromkeys(
                        [mid for mid, target in credits.items() if target == r["id"]] + r.get("reversed_by", [])
                    )
                ),
            }
    elif kind.startswith("document"):
        rows = documents
        for r in rows:
            card = L.db.execute(
                "SELECT tx_key FROM settled_tx WHERE what=?", ("card of bon, efactura " + (r["invoice"] or ""),)
            ).fetchone()
            r["links"] = {
                "invoice": r["invoice"],
                "report": r["month"],
                "transaction": statement_tx(card[0] if card else None),
            }
    else:
        # Unlike state, the resource includes all payment history, including skipped rows.
        rows = []
        visible = {p["item_id"]: (group, p) for group, ps in st.items() for p in ps}
        proofs = L.paid_proofs(co)
        for raw in L.payments(company=co):
            raw = dict(raw)
            group, p = visible.get(raw["item_id"], (raw["status"], raw))
            r = _payment(p)
            ef = next((x for x in received if x["item_id"] == r["item_id"]), None)
            r.update(id=payment_id(r["item_id"]), company=co, status=group, month=raw["month"])
            tx = statement_tx(
                proofs.get("payment " + raw["item_id"])
            )  # a withdrawn one: the debit that paid its invoice
            r["links"] = {"invoice": ef["id"] if ef else None, "transaction": tx, "report": raw["month"]}
            rows.append(r)
    for r in rows:
        r.pop("item_id", None)
    ident = params.get("id") or params.get("sha")
    if ident:
        row = next((r for r in rows if r["id"] == ident), None)
        if row is None:
            fail(404, "not_found", "The entity does not exist in this company. Refresh the list.")
        return row
    status = query.get("status", "")
    rows = [
        r
        for r in rows
        if (
            not status
            or r.get("status") == status
            or status == "late"
            and (r.get("overdue") or r.get("days_overdue", 0) > 0)
        )
        and (not month or (r.get("month") or r.get("issue_date") or "")[:7] == month)
    ]
    return page(rows, "id", query)


def payment_file_defaults(L: Ledger, co: str, today: date, files: list[dict]) -> list[dict]:
    return [
        {
            "co": co,
            "name": pay.default_name(L, co, today),
            "open_file_id": next((f["id"] for f in files if f["status"] == "open" and f["question"] is None), None),
        }
    ]


def payment_file_detail(L: Ledger, file_id: int) -> dict | None:
    file = L.payment_file(file_id)
    if not file:
        return None
    co = file["company"]
    summary = next(f for f in pay.named_files(L, co) if f["id"] == file_id)
    rows = pay.file_rows(L, file_id)
    proofs = L.paid_proofs(co)
    downloads = pay.file_downloads(L, file_id)
    downloaded = set(downloads[0]["items"]) if downloads else set()
    invoices = {r["item_id"]: r for r in L.efactura_rows(company=co) if r["item_id"]}
    open_ = summary["status"] == "open"
    path = pay.named_path(file)
    missing = bool(file["path"]) and not path.exists() and open_
    questions = _named_file_questions(summary, rows, missing, path)
    payments = _named_file_payments(L, co, rows, downloads, downloaded, invoices, proofs, file, missing)
    active = [r for r in rows if r["status"] in ("pending", "exported")]
    _, waiting = pay.statement_waits(L, co, active, clock().date())
    format_available = pay.paying_format(co, companies.get(co, L)) is not None
    return summary | {
        "payments": payments,
        "downloads": [{"id": d["id"], "version": d["version"], "at": d["created_at"]} for d in downloads],
        "can_add": open_ and summary["question"] is None,
        "can_rename": open_ and summary["question"] is None,
        "can_remove": open_ and summary["question"] is None,
        "can_delete": open_ and not rows,
        "can_download": open_ and not missing and bool(active) and not waiting and format_available and is_live(),
        "can_import": open_ and bool(downloads) and not missing and not summary["changed_since_download"] and is_live(),
        "questions": questions,
    }


def _named_file_questions(summary, rows, missing, path) -> list[dict]:
    questions = []
    if missing:
        questions.append(
            {
                "kind": "missing",
                "count": len(rows),
                "version": pay.version(r["item_id"] for r in rows if r["status"] in ("pending", "exported")),
            }
        )
    if summary["question"] == "orphan":
        questions.append({"kind": "orphan", "name": path.name})
    return questions


def _named_file_payments(L, co, rows, downloads, downloaded, invoices, proofs, file, missing) -> list[dict]:
    payments = []
    for r in rows:
        invoice = invoices.get(r["item_id"])
        proved = "payment " + r["item_id"] in proofs
        payments.append(
            {
                "id": payment_id(r["item_id"]),
                "payee": r["payee_name"],
                "kind": r["kind"],
                "ref": r["doc_ref"],
                "details": r["details"],
                "amount": f"{Decimal(r['amount']):.2f}",
                "currency": "RON",
                "due": invoice["due_date"] if invoice else None,
                "ef_id": invoice["message_id"] if invoice else None,
                "state": "skipped"
                if r["status"] == "skipped"
                else "paid"
                if r["status"] == "paid" or proved
                else "imported"
                if file["imported_at"]
                else "lost"
                if missing
                else "in_file",
                "proved": proved,
                "paid_at": r["paid_at"],
                "added_after_download": bool(downloads) and r["item_id"] not in downloaded,
            }
        )
    return payments


def prepared_payments(L: Ledger, co: str) -> dict:
    """Prepared rows for transaction explanations only, never a payment-file list."""
    st = _payments_by_state(L, co)
    _, waits = _statement_hold(L, co, companies.get(co, L), st, clock().date())
    return {k: [_payment(p, p["item_id"] in waits) for p in rows] for k, rows in st.items()}
