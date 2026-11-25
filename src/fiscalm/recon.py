"""Ingest documents into the ledger, reconcile statements vs receipts/payments, write reports."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path

from fiscalm import companies
from fiscalm.bank import statements
from fiscalm.bank.intake import (
    StoredStatementUnavailable,
    combine,
    hold_csv_days,
    import_lock,
    refuse_incomplete,
    refuse_overlap,
    refuse_unchained,
    statement_ibans,
)
from fiscalm.efactura import inbox as efactura
from fiscalm.ledger import Ledger, finance_home
from fiscalm.payments.orders import parse_payment_pdf
from fiscalm.pdftext import pdf_text, sha256
from fiscalm.storage import read_file

MATCH_DAYS = 12


def month_dir(company: str, month: str, home: Path | None = None) -> Path:
    return (home or finance_home()) / company / month


# --------------------------------------------------------------------- ingest


def ingest_file(
    ledger: Ledger,
    path: Path,
    home: Path | None = None,
    password_lookup=None,
    *,
    operator: bool = False,
    allowed_companies: list[str] | None = None,
) -> dict:
    """Route one file: a BT statement PDF, a CAMT or MT940 (operator only), or a treasury OP. Anything else is reported
    back."""
    path = Path(path)
    home = home or finance_home()
    if path.is_symlink():
        return {
            "file": str(path),
            "status": "refused",
            "errors": ["Statement source must be a regular file, not a symlink"],
        }
    try:
        with path.open("rb") as stream:
            raw = stream.read(4096)
    except OSError:
        return {
            "file": str(path),
            "status": "refused",
            "errors": ["Cannot read source file; check the path and permissions"],
        }
    if allowed_companies is not None:
        return ingest_op(ledger, path, allowed_companies)
    from fiscalm.bank.mt940 import looks_like

    prefix = raw.lstrip(b"\xef\xbb\xbf \t\r\n")

    if prefix.startswith(b"<") or path.suffix.lower() == ".xml" or looks_like(prefix):
        if not operator:
            return {"file": str(path), "status": "refused", "errors": ["CAMT and MT940 require operator CLI import"]}
        from fiscalm.bank.intake import StoredStatementUnavailable, ingest

        try:
            return ingest(ledger, path)
        except StoredStatementUnavailable as exc:
            return {"file": str(path), "status": "error", "errors": [str(exc)]}
        except statements.StatementError as exc:
            return {"file": str(path), "status": "refused", "errors": [str(exc)]}
    if not raw.startswith(b"%PDF-"):
        csv = path.suffix.lower() == ".csv"
        return {"file": str(path), "status": "refused", "errors": [_NOT_ACCOUNT_CSV if csv else _UNSUPPORTED]}
    if _bt_pdf(path):
        return ingest_statement(ledger, path, home, password_lookup, plain=operator)
    return ingest_op(ledger, path)


_NOT_ACCOUNT_CSV = "A CSV statement names no account: upload it on its account in the app"
_UNSUPPORTED = "Unsupported file: use a BT PDF, CAMT.053 XML, MT940 or a payment order PDF"


def ingest_files(ledger: Ledger, paths: list[Path], home: Path | None = None, password_lookup=None) -> list[dict]:
    """The operator's files (`fiscalm ingest`), each routed by `ingest_file`, except BT statement PDFs: those go in
    together (`ingest_statements`), so one month's files, one per account or one per day, are one statement."""
    home = home or finance_home()
    pdfs = [Path(p) for p in paths if _bt_pdf(Path(p))]
    rest = [ingest_file(ledger, Path(p), home, password_lookup, operator=True) for p in paths if Path(p) not in pdfs]
    return rest + (ingest_statements(ledger, pdfs, home, password_lookup, plain=True) if pdfs else [])


def _bt_pdf(path: Path) -> bool:
    """A BT statement: its monthly "allmerge" PDF, any PDF that needs a password (older per-account or daily ones), or
    a plain PDF whose text is a BT account statement, whatever its name (downloaded from BT24 or a shared folder)."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return False
    except OSError:  # ingest_file reports it
        return False
    if re.search(r"allmerge.*\.pdf$", path.name, re.I) or statements.encrypted(path):
        return True
    try:
        return statements.is_bt_statement(pdf_text(path))
    except RuntimeError:  # unreadable: the payment order reader refuses it on its own
        return False


def ingest_op(ledger: Ledger, path: Path, allowed_companies: list[str] | None = None) -> dict:
    """A mailed OP may name only a firm configured for its sender, before any ledger write."""
    try:
        res = parse_payment_pdf(path)
    except RuntimeError:  # a damaged or oversized PDF: this file is refused, the others of the call still run
        return {"file": str(path), "status": "refused", "errors": ["Cannot read the PDF; check the file"]}
    if not res.ok:
        return {"file": str(path), "status": "refused", "errors": res.errors}
    item = res.item
    if allowed_companies is not None and item.company not in allowed_companies:
        return {
            "file": str(path),
            "status": "refused",
            "code": "outside_scope",
            "errors": ["OP payer is not a firm configured for this sender"],
        }
    # Paid only when a statement shows it; the issue date proves nothing.
    added = ledger.add_payment(item, status="pending", note="")
    return {
        "file": str(path),
        "status": "added" if added else "duplicate",
        "item_id": item.item_id,
        "company": item.company,
        "amount": f"{item.amount:.2f}",
        "payee": item.payee_name,
    }


def _inside(root: Path, p: Path) -> bool:
    """`p` is `root` or under it, with no symlink from `root` down to `p`, and `root` itself is not a symlink."""
    if root.is_symlink():
        return False
    q = p
    while q != root:
        if q.is_symlink() or q == q.parent:
            return False
        q = q.parent
    return p.resolve().is_relative_to(root.resolve())


def ingest_statement(ledger: Ledger, path: Path, home: Path, password_lookup=None, **kw) -> dict:
    """One BT PDF: `ingest_statements` with a set of one."""
    return ingest_statements(ledger, [path], home, password_lookup, **kw)[0]


@dataclass
class _Opened:
    path: Path
    company: str
    month: str
    out: Path  # its decrypted copy
    st: statements.Statement
    digest: str


def ingest_statements(
    ledger: Ledger,
    paths: list[Path],
    home: Path,
    password_lookup=None,
    *,
    only: str | None = None,
    check=None,
    source: str | None = None,
    tmp_dir: Path | None = None,
    one_set: bool = False,
    plain: bool = False,
) -> list[dict]:
    """BT PDFs, grouped by company and month: each group is one statement (one file per account, or daily ones,
    `intake.combine`), checked whole and stored whole or not at all. One result per group, and one per file that
    isn't a statement of ours. `only`: the company the files are for (an upload); no other company's password is
    tried. `check(st)`: a last refusal on a group's statement before anything is stored (raises
    statements.StatementError). `one_set`: the files must make one group, or nothing is stored. `plain`: a PDF without
    a password is read too (a person brought it: the CLI or an upload, never mail)."""
    tmp = tmp_dir or home / "state" / "tmp"  # an upload's own staging is already confined (bank.upload._confined)
    tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not tmp_dir and not _inside(home, tmp):  # a symlink on the way would put decrypted copies outside the data root
        error = "Unsafe statement storage directory; remove symlinks and retry"
        return [{"file": str(path), "status": "error", "errors": [error]} for path in paths]
    work = tmp / f"set-{secrets.token_hex(8)}"
    kw = ({"password_lookup": password_lookup} if password_lookup else {}) | {"plain": plain}
    if only is not None:
        kw["only"] = only
    with import_lock():
        try:  # a parser failing on a hostile or damaged file leaves no decrypted copy behind
            failed, opened = [], []
            for i, path in enumerate(paths):
                o = _open_statement(Path(path), work / str(i), kw)
                if isinstance(o, dict):
                    failed.append(o)
                else:
                    opened.extend(o)
            for o in list(opened):  # a stored copy read again is the document it was stored for: a duplicate
                copy = _stored_copy(ledger, o, home)
                if isinstance(copy, dict):
                    opened.remove(o)
                    failed.append(copy)
                elif copy:
                    o.digest = copy
            groups: dict[tuple[str, str], list[_Opened]] = {}
            for o in opened:
                groups.setdefault((o.company, o.month), []).append(o)
            if one_set and (failed or len(groups) > 1):
                several = len(paths) == 1 and not failed  # one file, split by month: each month is uploaded alone
                error = (
                    statements.SEVERAL_MONTHS if several else "Statement set: files of different months or companies"
                )
                return failed[:1] or [_refused(opened, error)]
            # month by month: a month that starts after the 1st is checked against the one before it
            others = _checked(groups, check)  # the months imported together prove each other's edges
            return failed + [_store_set(ledger, groups[k], home, check, source, others) for k in sorted(groups)]
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _open_statement(path: Path, out_dir: Path, kw: dict) -> list[_Opened] | dict:
    """The file's statement, or one per month for a statement of several months (split by month, each part its own
    document); a dict says why the file is not read. The file is read once, into a private snapshot: what is parsed is
    what is identified."""
    snapshot = out_dir / "source" / path.name
    try:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, snapshot, follow_symlinks=False)
    except OSError:
        return {
            "file": str(path),
            "status": "error",
            "errors": ["Cannot read source file; check the path and permissions"],
        }
    try:
        company, month, out = statements.decrypt_to(snapshot, out_dir, **kw)
    except statements.SecretsUnavailable as e:
        return {"file": str(path), "status": "error", "errors": [str(e)]}
    except statements.StatementError as e:
        return {"file": str(path), "status": "not_ours", "errors": [str(e)]}
    except (ValueError, ArithmeticError):  # a printed date or amount that isn't one
        return {"file": str(path), "status": "refused", "errors": ["Cannot read the statement"]}
    try:
        st = statements.parse(out, company)
        if not st.period_start or not st.period_end:
            raise statements.StatementError("Cannot verify statement period")
        if statements.several_months(st):
            parts = statements.split_months(out, company, st, out_dir / "months")
            if month not in {m for m, *_ in parts}:  # a name's month, or the first printed: one of its months
                raise statements.StatementError("Statement file name names another month than its period")
            return [_Opened(path, company, m, piece, s, sha256(piece)) for m, piece, s in parts]
        if st.month != month:  # the month comes from what the bank printed, never from a file name
            raise statements.StatementError("Statement file name names another month than its period")
    except (ValueError, ArithmeticError):
        return {"file": str(path), "status": "refused", "errors": ["Cannot read the statement"]}
    except statements.StatementError as e:
        return {"file": str(path), "status": "refused", "errors": [str(e)]}
    return [_Opened(path, company, month, out, st, sha256(snapshot))]


def _checked(groups: dict[tuple[str, str], list[_Opened]], check) -> list[statements.Statement]:
    """Each month of the call as one statement, only those that pass their own checks: a refused file proves no
    neighbour's edge."""
    out = []
    for parts in groups.values():
        try:
            st = combine([o.st for o in parts])
            _set_checks(st, parts, check)
        except statements.StatementError:
            continue
        out.append(st)
    return out


def _stored_copy(ledger: Ledger, o: _Opened, home: Path) -> str | dict | None:
    """The statement document `o`'s bytes are the stored copy of, when they name no document of their own and that
    document's stored file, read inside its firm's storage, still has exactly these bytes; a dict when it doesn't."""
    sha = None if ledger.document(o.digest) else ledger.statement_of_copy(o.digest)
    if sha is None:
        return None
    doc = ledger.document(sha)
    data = read_file(doc["path"], [home.resolve() / doc["company"]], 32 * 1024 * 1024)
    if not data or hashlib.sha256(data[1]).hexdigest() != o.digest:
        return _refused([o], "Cannot read a stored statement; restore the stored document and retry", "error")
    return sha


def _refused(parts: list[_Opened], error: str, status: str = "refused", **extra) -> dict:
    return {"file": ", ".join(str(o.path) for o in parts), "status": status, "errors": [error], **extra}


def _store_set(ledger: Ledger, parts: list[_Opened], home: Path, check, source: str | None, others=()) -> dict:
    try:
        st = combine([o.st for o in parts])
        _set_checks(st, parts, check)
        refuse_unchained(ledger, st, others)  # under the import lock
        for o in parts:
            refuse_overlap(ledger, o.st, o.digest, siblings={p.digest for p in parts})
    except StoredStatementUnavailable as exc:
        return _refused(parts, str(exc), "error")
    except statements.StatementError as exc:
        return _refusal(parts, exc)
    folder = month_dir(parts[0].company, parts[0].month, home) / "extrase"
    folder.mkdir(parents=True, exist_ok=True)
    if not _inside(home / parts[0].company, folder):  # a symlink on the way would publish outside the data root
        return _refused(parts, "Unsafe statement storage directory; remove symlinks and retry", "error")
    stored = _publish(ledger, parts, folder, st, check, source)
    if isinstance(stored, dict):
        return stored
    paid = auto_mark_paid(ledger, st)
    ef_paid = efactura.match_statement(ledger, st)
    return {
        "file": ", ".join(str(o.path) for o in parts),
        "status": "added" if any(new for _, new in stored) else "duplicate",
        "company": parts[0].company,
        "month": parts[0].month,
        "unprotected": ", ".join(str(dest) for dest, _ in stored),
        "transactions": len(st.txs),
        "balanced": all(a.balanced for a in st.accounts),
        "payments_marked_paid": paid,
        "efactura_marked_paid": ef_paid,
    }


def _set_checks(st: statements.Statement, parts: list[_Opened], check) -> None:
    """What the firm's statement must be, checked again under the write lock that stores it."""
    own = companies.get(st.company)
    if any(not own.account(a.iban) for a in st.accounts):  # another account's lines never reach the matchers
        raise statements.StatementError("PDF account does not belong to the selected company")
    if check:
        check(st)
    refuse_incomplete(st)  # every caller: a statement, in one file or several, holds every account at its bank


def _refusal(parts: list[_Opened], exc: statements.StatementError) -> dict:
    """A refused set, with the engine's own facts for the person: the missing accounts, or the month left partial."""
    if isinstance(exc, statements.MissingAccounts):
        return _refused(parts, str(exc), missing=exc.accounts)
    if isinstance(exc, statements.PartialMonth):
        return _refused(parts, str(exc), partial=exc.month.isoformat())
    return _refused(parts, str(exc))


class _NotStored(Exception):
    """A file of the set is not kept as this firm's statement of that month (another document has its bytes, or a
    stored copy is gone): the set is not stored."""

    def __init__(self, error: str, status: str):
        super().__init__(error)
        self.error, self.status = error, status


def _publish(ledger: Ledger, parts: list[_Opened], folder: Path, st, check, source) -> list[tuple[Path, bool]] | dict:
    """Every file of the set stored as its own statement document in one ledger transaction, with the days a CSV
    already brought held (the same payment never settles twice), or none: on any failure the copies this call moved
    into the month's folder are removed again. (A crash between the moves and the commit leaves only unregistered
    copies, which a retry reuses by name and checksum.)"""
    placed: list[Path] = []
    try:
        dests = [_place(ledger, o, folder, placed) for o in parts]
        with ledger.tx():
            _set_checks(st, parts, check)
            added = [_add_part(ledger, o, dest, source) for o, dest in zip(parts, dests, strict=True)]
            for o in parts:
                _kept(ledger, o)
            hold_csv_days(ledger, st)
    except BaseException as exc:
        _unplace(placed)
        if isinstance(exc, _NotStored):
            return _refused(parts, exc.error, exc.status)
        if isinstance(exc, StoredStatementUnavailable):
            return _refused(parts, str(exc), "error")
        if isinstance(exc, statements.StatementError):
            return _refusal(parts, exc)
        raise
    return list(zip(dests, added, strict=True))


def _add_part(ledger: Ledger, o: _Opened, dest: Path, source: str | None) -> bool:
    added = ledger.add_document(
        o.digest,
        o.company,
        o.month,
        "statement",
        str(dest),
        issues=[f"parser totals mismatch on {a.iban[-4:]}" for a in o.st.accounts if not a.balanced],
        source=source or f"mail:{o.path.name}",
        accounts=statement_ibans(o.st),
    )
    if added:
        ledger.set_meta("statement_stored_sha:" + o.digest, sha256(dest))
    return added


def _kept(ledger: Ledger, o: _Opened) -> None:
    """The file is on record as the firm's statement of its month, and its stored copy is there."""
    doc = ledger.document(o.digest)
    if not doc or doc["kind"] != "statement" or doc["company"] != o.company or doc["month"] != o.month:
        raise _NotStored("Statement set: a file is already kept as another document", "refused")
    if not Path(doc["path"]).is_file():
        raise _NotStored("Cannot read a stored statement; restore the stored document and retry", "error")


def _unplace(placed: list[Path]) -> None:
    for dest in placed:
        dest.unlink(missing_ok=True)


def _place(ledger: Ledger, o: _Opened, folder: Path, placed: list[Path]) -> Path:
    """Where a set's file is kept: the stored copy of a statement imported before, else its decrypted copy, moved
    into the month's folder under a name no other file of that folder has."""
    existing = ledger.document(o.digest)
    if existing and not (existing["source"] or "").startswith("upload:"):
        o.out.unlink()
        return Path(existing["path"])
    dest = folder / o.out.name
    if dest.exists() and sha256(dest) != sha256(o.out):
        dest = dest.with_stem(dest.stem + "-" + o.digest[:12])
    fresh = not dest.exists()  # a copy already there (the same bytes) is never removed on failure
    o.out.replace(dest)
    if fresh:
        placed.append(dest)
    return dest


def classify(ledger: Ledger, company: str, txs: list[statements.Tx]) -> None:
    """Each line's category (statements.classify_tx); a card payment to a merchant remembered for a supplier
    (efactura.statement_partner: "Factura vine prin e-Factura") needs no receipt: its invoice arrives through SPV."""
    known = efactura.statement_partner(ledger, company)
    owners = ledger.card_owners()
    for t in txs:
        if (
            statements.classify_tx(t) == statements.NEEDS_INVOICE
            and (not t.card_last4 or owners.get((company, t.card_last4)) == company)
            and known(t)
        ):
            t.category = statements.DOMESTIC


def auto_mark_paid(ledger: Ledger, st: statements.Statement) -> list[str]:
    """A statement debit to the same IBAN with the same amount on/after issue date proves payment.

    When the payment details name an invoice number, only that invoice is marked: two open
    accountant's invoices of 350 lei must not both be settled by one transfer that names one of them.
    A debit pays one item, ever (ledger `settled_tx`, shared with the e-Factura matcher), and a
    debit that fits several candidates, payment rows or e-Factura invoices (efactura.rivals), pays none.
    """
    ledger.record_statement(st)  # the evidence behind every debit it settles (Ledger.paid_proofs)
    done = []
    open_items = ledger.payments(status=("pending", "exported"), company=st.company)
    used = ledger.used_txs()
    rivals = efactura.rivals(ledger, st.company)
    advance = efactura.remembered_advance(ledger, st.company)  # the owner's money: never a payment row's

    def settle(p, t):
        if ledger.mark_paid(p["item_id"], f"statement {t.date} REF {t.ref}", statements.tx_key(t)):
            done.append(p["item_id"])
        used.add(statements.tx_key(t))

    # Unambiguous debits first (one naming its invoice): once they paid theirs, a plain twin may fit a single one.
    for t in sorted(st.settlement_txs, key=lambda t: len(rivals(t)) != 1):
        if statements.pays_nothing(t) or statements.tx_key(t) in used or advance(t):
            continue
        left = rivals(t) - {("payment", i) for i in done}
        if len(left) == 1 and (key := left.pop())[0] == "payment":
            settle(next(p for p in open_items if p["item_id"] == key[1]), t)
    return done


# ---------------------------------------------------------------- reconcile


def _receipts(ledger: Ledger, company: str):
    """Receipts a matcher may still give to a debit: dated and priced, and not an upload the user named for one."""
    out = [
        d for d in ledger.documents(company=company, kind="receipt") if d["amount"] and d["currency"] and d["doc_date"]
    ]
    named = ledger.linked_receipts(company, out)
    return [d for d in out if d["sha"] not in named]


def match_receipts(txs: list[statements.Tx], receipts) -> dict[int, tuple[str, dict]]:
    """Map tx index -> (confidence, receipt row). Exact amount+currency within MATCH_DAYS first,
    then vendor-name match within MATCH_DAYS (FX-converted charges, e.g. EUR fare billed in RON)."""
    if not txs:
        return {}
    # Keep receipt order within each date window: it is the tie breaker in both passes.
    by_day: dict[int, list[tuple[int, dict]]] = {}
    for n, r in enumerate(receipts):
        by_day.setdefault(date.fromisoformat(r["doc_date"]).toordinal(), []).append((n, r))
    words: dict[str, str] = {}
    amounts: dict[str, Decimal] = {}
    matches: dict[int, tuple[str, dict]] = {}
    used: set[str] = set()

    @cache
    def nearby(day: date):
        ordinal = day.toordinal()
        return sorted(
            (
                pair
                for n in range(max(1, ordinal - MATCH_DAYS), min(date.max.toordinal(), ordinal + MATCH_DAYS) + 1)
                for pair in by_day.get(n, ())
            ),
            key=lambda pair: pair[0],
        )

    for i, t in enumerate(txs):
        amt = t.orig_amount if t.orig_amount is not None else t.amount
        cur = t.orig_currency or t.currency
        for _, r in nearby(t.date):
            if r["sha"] in used or r["currency"] != cur:
                continue
            if r["sha"] not in amounts:
                amounts[r["sha"]] = Decimal(r["amount"])
            if amounts[r["sha"]] == amt:
                matches[i] = ("exact", dict(r))
                used.add(r["sha"])
                break
    for i, t in enumerate(txs):
        if i in matches:
            continue
        cp = t.counterparty.lower()
        for _, r in nearby(t.date):
            if r["sha"] in used:
                continue
            if r["sha"] not in words:
                words[r["sha"]] = re.split(r"[^a-z0-9]+", (r["vendor"] or "").lower())[0]
            word = words[r["sha"]]
            if len(word) >= 4 and word in cp:
                matches[i] = ("vendor", dict(r))
                used.add(r["sha"])
                break
    return matches


def _name(t: statements.Tx) -> str:
    """A line's name for a person: its counterparty, else the bank's own words, with every IBAN and card number reduced
    to its last four (as Tranzacții shows them): the free text can hold a full account number."""
    return statements.masked(" ".join((t.counterparty or t.title or t.description).split()))


def _fmt(amount, cur="RON") -> str:
    return f"{Decimal(amount):,.2f} {cur}"


def build_report(ledger: Ledger, company: str, month: str, home: Path | None = None) -> tuple[str, dict]:
    home = home or finance_home()
    co = companies.get(company)
    lines = [f"# {co.legal_name} - {month}", "", f"_Generated {datetime.now():%Y-%m-%d %H:%M} by fiscalm_", ""]
    summary: dict = {"company": company, "month": month}

    # Payments
    pays = [p for p in ledger.payments(company=company) if p["month"] == month]
    lines.append("## Payments (OPs and the accountant's invoices)")
    if not pays:
        lines.append("- none recorded for this month")
    for p in pays:
        where = f" -> `{p['csv_path']}`" if p["csv_path"] else ""
        lines.append(
            f"- **{p['status'].upper()}** {p['kind']} {p['doc_ref']}: {_fmt(p['amount'])} to "
            f"{p['payee_name']} ({p['payee_iban']}){where}"
            + (f" - {p['paid_note']}" if p["status"] in ("paid", "skipped") and p["paid_note"] else "")
        )
    summary["payments_open"] = sum(1 for p in pays if p["status"] not in ("paid", "skipped"))
    lines.append("")

    # Statement
    st_docs = ledger.documents(company=company, month=month, kind="statement")
    lines.append("## Bank statement")
    if not st_docs:
        lines.append("- No statement for this month yet")
        summary["statement"] = False
        text = "\n".join(lines) + "\n"
        return text, summary
    summary["statement"] = True
    st = month_statement(st_docs, company)
    for a in st.accounts:
        ok = "totals match bank" if a.balanced else "**TOTALS DO NOT MATCH BANK - check manually**"
        lines.append(f"- {a.currency} {a.iban}: {len(a.txs)} transactions, {ok}")
    classify(ledger, company, st.txs)
    lines.append("")

    need = [t for t in st.txs if t.category == statements.NEEDS_INVOICE]
    matches = match_receipts(need, _receipts(ledger, company))
    missing = [t for i, t in enumerate(need) if i not in matches]
    summary.update(needs_invoice=len(need), matched=len(matches), missing=len(missing), transactions=len(st.txs))

    lines.append(
        f"## Foreign card/transfer outflows: {len(need)} need an invoice, {len(matches)} matched, "
        f"{len(missing)} WITHOUT invoice"
    )
    if matches:
        lines.append("")
        lines.append("### Matched")
        for i, t in enumerate(need):
            if i in matches:
                conf, r = matches[i]
                tag = "" if conf == "exact" else " (matched by vendor/date; amounts differ by FX)"
                lines.append(f"- {t.date} {_name(t)}: {_fmt(t.amount)} -> {Path(r['path']).name}{tag}")
    if missing:
        lines.append("")
        lines.append("### WITHOUT invoice (flagged; do not guess, find the receipt or ask the user)")
        for t in missing:
            orig = f" ({t.orig_amount} {t.orig_currency})" if t.orig_currency and t.orig_currency != "RON" else ""
            lines.append(f"- {t.date} {_name(t)}: {_fmt(t.amount)}{orig}, card ...{t.card_last4}")
    lines.append("")

    dom = [t for t in st.txs if t.category == statements.DOMESTIC]
    lines.append(f"## Romanian suppliers ({len(dom)}): invoices arrive via e-Factura/SPV, not checked here")
    for t in dom:
        lines.append(f"- {t.date} {_name(t)}: {_fmt(t.amount)}")
    lines.append("")
    adv = [t for t in st.txs if t.category == statements.OWNER_ADVANCE]
    if adv:
        lines.append("## Transfers to the user (avans trezorerie): need a settlement (decont), not an invoice")
        for t in adv:
            lines.append(f"- {t.date} {_fmt(t.amount)}")
        lines.append("")

    # Issues
    issues = []
    for d in ledger.documents(company=company, month=month):
        for iss in json.loads(d["issues"] or "[]"):
            issues.append(f"- {Path(d['path']).name}: {iss}")
    lines.append("## Invoices with issues")
    lines.extend(issues or ["- none"])
    summary["issues"] = len(issues)
    lines.append("")

    # Pack
    pk = ledger.pack(company, month)
    from fiscalm.pack import pending_docs

    unsent = pending_docs(ledger, company, month)
    lines.append("## Accountant pack")
    if pk and pk["status"] == "sent":
        lines.append(f"- sent ({pk['note'] or pk['updated_at']})")
    else:
        lines.append(
            f"- {len(unsent)} document(s) not yet sent to the accountant: `fiscalm pack build --company {company} --month {month}`"
        )
    summary["unsent_docs"] = len(unsent)
    return "\n".join(lines) + "\n", summary


def write_report(ledger: Ledger, company: str, month: str, home: Path | None = None) -> tuple[Path, dict]:
    text, summary = build_report(ledger, company, month, home)
    out = month_dir(company, month, home) / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return out, summary


def month_statement(docs, company: str, parse=None) -> statements.Statement:
    """Every statement stored for a company-month (one per bank) read as one: all their accounts and transactions,
    the period they span together. A month is never judged from one bank's statement alone."""
    parsed = [(parse or statements.parse)(Path(d["path"]), company) for d in docs]
    if len(parsed) == 1:
        return parsed[0]
    starts = [p.period_start for p in parsed if p.period_start]
    ends = [p.period_end for p in parsed if p.period_end]
    return statements.Statement(
        company,
        min(starts) if starts else None,
        max(ends) if ends else None,
        [a for p in parsed for a in p.accounts],
        partial=all(getattr(p, "partial", False) for p in parsed),
    )


def month_view(ledger: Ledger, company: str, month: str, rows: list[dict], parse=None) -> dict:
    """Statement checks and the transaction projection, without deciding explanations a second time."""
    st_docs = ledger.documents(company=company, month=month, kind="statement")
    view = {
        "company": company,
        "month": month,
        "statement": any(not str(d["path"]).endswith(".csv") for d in st_docs),
        "balanced": None,
        "matched": [],
        "missing": [],
        "domestic": 0,
        "advances": [],
        "file_missing": bool(st_docs) and not all(Path(d["path"]).exists() for d in st_docs),
    }
    if not st_docs or view["file_missing"]:
        return view  # a missing statement file: nothing can be said for the month
    st = month_statement(st_docs, company, parse)
    # a CSV has no balances: unknown, not a discrepancy
    checked = [a for a in st.accounts if a.total_debit is not None or a.total_credit is not None]
    view["balanced"] = all(a.balanced for a in checked) if checked else None
    for t in sorted(rows, key=lambda t: t["date"]):
        if not t["date"].startswith(month):
            continue
        row = {
            "date": t["date"],
            "who": t["counterparty"] or t["details"] or "",
            "kind": "card" if t["card"] or re.search(r"\bPOS\b", (t["details"] or "").upper()) else "transfer",
            "amount": t["amount"],
            "currency": t["currency"],
            "direction": t["direction"],
            "orig": f"{t['orig']['amount']} {t['orig']['currency']}" if t["orig"] else "",
            "awaiting": bool(t["awaiting"]),
        }
        if t["explanation"]:
            view["matched"].append(row | {"doc": t["explanation_text"]})
        else:
            view["missing"].append(row)
    return view
