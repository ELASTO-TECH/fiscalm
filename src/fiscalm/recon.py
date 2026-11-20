"""Ingest documents into the ledger, reconcile statements vs receipts/payments, write reports."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path

from fiscalm import companies
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.ledger import Ledger, finance_home
from fiscalm.payments.orders import parse_payment_pdf
from fiscalm.pdftext import sha256

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
    """Route one PDF: encrypted BT statement, or treasury OP. Anything else is reported back."""
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
            prefix = stream.read(4096).lstrip(b"\xef\xbb\xbf \t\r\n")
    except OSError:
        return {
            "file": str(path),
            "status": "refused",
            "errors": ["Cannot read source file; check the path and permissions"],
        }
    if allowed_companies is not None:
        return ingest_op(ledger, path, allowed_companies)
    from fiscalm.bank.mt940 import looks_like

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
    if re.search(r"allmerge.*\.pdf$", path.name, re.I):
        return ingest_statement(ledger, path, home, password_lookup)
    return ingest_op(ledger, path)


def ingest_op(ledger: Ledger, path: Path, allowed_companies: list[str] | None = None) -> dict:
    """A mailed OP may name only a firm configured for its sender, before any ledger write."""
    res = parse_payment_pdf(path)
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


def ingest_statement(
    ledger: Ledger,
    path: Path,
    home: Path,
    password_lookup=None,
    *,
    only: str | None = None,
    check=None,
    source: str | None = None,
    tmp_dir: Path | None = None,
) -> dict:
    """`only`: the company the statement is for (an upload); no other company's password is tried. `check(st)`: a
    last refusal on the parsed statement before anything is stored (raises statements.StatementError)."""
    from fiscalm.bank.intake import import_lock

    with import_lock():
        return _ingest_statement(
            ledger, path, home, password_lookup, only=only, check=check, source=source, tmp_dir=tmp_dir
        )


def _ingest_statement(
    ledger: Ledger,
    path: Path,
    home: Path,
    password_lookup=None,
    *,
    only: str | None = None,
    check=None,
    source: str | None = None,
    tmp_dir: Path | None = None,
) -> dict:
    kw = {"password_lookup": password_lookup} if password_lookup else {}
    if only is not None:
        kw["only"] = only
    try:
        company, month, out = statements.decrypt_to(path, tmp_dir or home / "state" / "tmp", **kw)
    except statements.SecretsUnavailable as e:
        return {"file": str(path), "status": "error", "errors": [str(e)]}
    except statements.StatementError as e:
        return {"file": str(path), "status": "not_ours", "errors": [str(e)]}
    from fiscalm.bank.intake import StoredStatementUnavailable, refuse_overlap, statement_ibans

    try:
        st = statements.parse(out, company)
        if check:
            check(st)
    except statements.StatementError as exc:
        out.unlink()
        return {"file": str(path), "status": "refused", "errors": [str(exc)]}
    except BaseException:  # a parser failing on a hostile or damaged file leaves no decrypted copy behind
        out.unlink(missing_ok=True)
        raise
    digest = sha256(path)
    try:
        refuse_overlap(ledger, st, digest)
    except StoredStatementUnavailable as exc:
        out.unlink()
        return {"file": str(path), "status": "error", "errors": [str(exc)]}
    except statements.StatementError as exc:
        out.unlink()
        return {"file": str(path), "status": "refused", "errors": [str(exc)]}
    existing = ledger.document(digest)
    if existing and not (existing["source"] or "").startswith("upload:"):
        dest = Path(existing["path"])
        out.unlink()
    else:
        dest = month_dir(company, month, home) / "extrase" / out.name
        if dest.exists() and sha256(dest) != sha256(out):
            dest = dest.with_stem(dest.stem + "-" + digest[:12])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(home / company, dest.parent):  # a symlink on the way would publish outside the data root
            out.unlink()
            return {
                "file": str(path),
                "status": "error",
                "errors": ["Unsafe statement storage directory; remove symlinks and retry"],
            }
        out.replace(dest)
    unbalanced = [a.iban for a in st.accounts if not a.balanced]
    with ledger.tx():
        if check:
            check(st)
        added = ledger.add_document(
            digest,
            company,
            month,
            "statement",
            str(dest),
            issues=[f"parser totals mismatch on {i[-4:]}" for i in unbalanced],
            source=source or f"mail:{path.name}",
            accounts=statement_ibans(st),
        )
        if added:
            ledger.set_meta("statement_stored_sha:" + digest, sha256(dest))
    paid = auto_mark_paid(ledger, st)
    from fiscalm.efactura import inbox as efactura

    ef_paid = efactura.match_statement(ledger, st)
    return {
        "file": str(path),
        "status": "added" if added else "duplicate",
        "company": company,
        "month": month,
        "unprotected": str(dest),
        "transactions": len(st.txs),
        "balanced": not unbalanced,
        "payments_marked_paid": paid,
        "efactura_marked_paid": ef_paid,
    }


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
