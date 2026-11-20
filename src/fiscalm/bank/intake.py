"""Content-addressed CAMT storage; journal before publishing, retain the source for retries."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import timedelta
from pathlib import Path

from fiscalm import companies
from fiscalm.api.receipts import directory
from fiscalm.bank import statements
from fiscalm.bank.camt import MAX_XML, parse_camt
from fiscalm.ledger import Ledger, finance_home


def identity(data: bytes, csv_account: tuple[str, dict] | None = None) -> str:
    """A stored statement's identity. A CSV is its bytes read on one account with given columns: the same bytes read
    another way are another document, never a duplicate that would reinterpret the first."""
    h = hashlib.sha256(data)
    if csv_account:
        h.update(b"\x00csv\x00" + json.dumps([csv_account[0], csv_account[1]], sort_keys=True).encode())
    return h.hexdigest()


def parse_structured(data: bytes, company: str = "") -> tuple[statements.Statement, str]:
    """A bank's structured export, CAMT.053 (XML) or MT940, with the extension its stored copy gets."""
    from fiscalm.bank.mt940 import looks_like, parse_mt940

    if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        return parse_camt(data, company), ".xml"
    if looks_like(data):
        return parse_mt940(data, company), ".sta"
    raise statements.StatementError("Unsupported statement: use CAMT.053 XML or MT940")


@contextmanager
def import_lock() -> Iterator[None]:
    """Serialize statement publication across processes, retaining committed crash journals."""
    state = finance_home() / "state"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "statement-import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class StoredStatementUnavailable(statements.StatementError):
    """Retry after the operator restores an unreadable adjacent statement."""


def statement_ibans(st: statements.Statement) -> list[str]:
    """The company accounts a parsed statement covers."""
    return sorted({companies.normalize_iban(a.iban) for a in st.accounts if a.iban})


def refuse_overlap(L: Ledger, st: statements.Statement, digest: str) -> None:
    """One document per account and month: a statement of another bank (other accounts) for the same month is
    fine, the same account twice is not. The months the period touches and their neighbours are read, and every CSV
    of the company (one may span months; it is stored under its first). A statement stored
    before accounts were recorded is read to learn its accounts; if it can't be read, nothing is imported."""
    if not st.period_start or not st.period_end:
        raise statements.StatementError("Cannot verify statement period")
    mine = set(statement_ibans(st))
    if not mine:
        raise statements.StatementError("Statement names no account")
    months = _months(
        st.period_start.replace(day=1) - timedelta(days=1), st.period_end.replace(day=28) + timedelta(days=4)
    )
    docs = [
        d
        for m in sorted(months, key=lambda m: m != st.month)
        for d in L.documents(company=st.company, month=m, kind="statement")
    ]
    # a CSV may span months and is stored under its first: every one of the company's is checked
    docs += [d for d in _csv_docs(L, st.company) if d["month"] not in months]
    for doc in docs:
        month = doc["month"]
        if doc["sha"] == digest:
            continue
        previous = None
        if doc["accounts"]:
            theirs = set(json.loads(doc["accounts"]))
        else:
            previous = _read_stored(doc, st.company)
            theirs = set(statement_ibans(previous))
            if not theirs:  # its accounts can't be learned, so it can't be shown not to overlap
                raise StoredStatementUnavailable(
                    "Cannot tell which accounts a stored statement covers; restore the stored document and retry"
                )
        if not mine & theirs:
            continue
        if not st.partial and str(doc["path"]).endswith(".csv"):
            continue  # a full statement supersedes partial CSVs: their days' lines are held (csv_days)
        if month == st.month and not (st.partial or str(doc["path"]).endswith(".csv")):
            raise statements.StatementError("Statement already imported for this account and month")
        previous = previous or _read_stored(doc, st.company)
        if not previous.period_start or not previous.period_end:
            raise StoredStatementUnavailable(
                "Cannot verify adjacent statement period; restore the stored document and retry"
            )
        if st.period_start <= previous.period_end and previous.period_start <= st.period_end:
            raise statements.StatementError("Overlapping statement already imported")


def _months(lo, hi) -> set[str]:
    """Every calendar month from `lo` to `hi` (dates), as YYYY-MM."""
    out, d = set(), lo.replace(day=1)
    while d <= hi:
        out.add(d.strftime("%Y-%m"))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _csv_docs(L: Ledger, company: str) -> list:
    return [d for d in L.documents(company=company, kind="statement") if str(d["path"]).endswith(".csv")]


def learn_accounts(L: Ledger, doc) -> list[str]:
    """The accounts a stored statement covers. One stored before accounts were recorded is read from its file and
    the answer is kept on the document, so it is read once; a file that can't be read proves no account."""
    if doc["accounts"]:
        return json.loads(doc["accounts"])
    try:
        found = statement_ibans(statements.parse(Path(doc["path"]), doc["company"]))
    except (OSError, RuntimeError, ValueError):
        return []
    if found:
        with L.tx() as db:
            db.execute(
                "UPDATE documents SET accounts=? WHERE sha=? AND accounts IS NULL", (json.dumps(found), doc["sha"])
            )
    return found


def hold_csv_days(L: Ledger, st: statements.Statement) -> None:
    """A full statement arriving after CSVs of the same account: its lines on the days a CSV already brought are kept
    as evidence and reserved (`twin csv`), so the CSV's lines, which may already have settled something, stay the
    ones that count for those days, and the same payment never settles twice. Its other days settle as usual."""
    ranges: dict[str, list[tuple]] = {}
    for doc in _csv_docs(L, st.company):  # any month: a CSV may span several
        csv = _read_stored(doc, st.company)
        if csv.period_end < st.period_start or csv.period_start > st.period_end:
            continue
        for a in csv.accounts:
            ranges.setdefault(companies.normalize_iban(a.iban), []).append((csv.period_start, csv.period_end))
    if not ranges:
        return
    L.record_statement(st)
    with L.tx() as db:
        for t in st.settlement_txs:
            if any(lo <= t.date <= hi for lo, hi in ranges.get(companies.normalize_iban(t.iban), [])):
                db.execute(
                    "INSERT OR IGNORE INTO settled_tx VALUES(?,?,?)",
                    (statements.tx_key(t), "twin csv", __import__("fiscalm.ledger", fromlist=["now"]).now()),
                )


def covers(L: Ledger, company: str, month: str, iban: str) -> bool:
    """A statement of `month` is on record for the company's account `iban`. One stored before accounts were recorded
    covers every account (they were the company's only bank's); one that names its accounts must name this one."""
    iban = companies.normalize_iban(iban)
    return any(
        (not d["accounts"] or iban in json.loads(d["accounts"])) and not str(d["path"]).endswith(".csv")
        for d in L.documents(company=company, month=month, kind="statement")
    )  # a CSV is partial: never


def _read_stored(doc, company: str) -> statements.Statement:
    try:
        return statements.parse(Path(doc["path"]), company)
    except (OSError, RuntimeError) as exc:
        raise StoredStatementUnavailable(
            "Cannot read a stored statement; restore the stored document and retry"
        ) from exc


def missing_accounts(st: statements.Statement) -> list[str]:
    """The company's accounts at the statement's banks that the statement leaves out: a statement covers every
    account the company held at its bank in its period, so none of them is silently skipped. An account opened after
    the period's last month (set by a person) or retired before its first day is not owed; without a period, or with
    sections naming different periods, every account is. Accounts at other banks come
    from their own statements. An account's bank is the one configured for it, else the catalogue's for its IBAN code,
    else the IBAN code itself: two accounts with the same code outside the catalogue are one bank, so a statement
    with one of them must carry both; a different code is another bank, with its own statements."""
    from fiscalm.bank import catalogue as banks

    co = companies.get(st.company)

    def bank_of(iban: str) -> str:
        own = co.account(iban)
        if own and own.bank:
            return own.bank
        b = banks.for_iban(iban)
        # outside the catalogue, the IBAN's own country and bank code name the bank
        return b.id if b else "code:" + iban[:2] + iban[4:8]

    present = {(companies.normalize_iban(a.iban), a.currency) for a in st.accounts}
    held = {bank_of(i) for i, _ in present}
    first, last = (st.period_start, st.period_end or st.period_start) if st.one_period else (None, None)
    expected = {(a.iban, a.currency) for a in co.accounts if bank_of(a.iban) in held and a.held_between(first, last)}
    return sorted(f"{ccy} {iban[-4:]}" for iban, ccy in expected - present)


def refuse_incomplete(st: statements.Statement) -> None:
    """Refuse a whole-month statement that leaves out an account its bank holds for the company in that month."""
    missing = [] if st.partial else missing_accounts(st)  # a partial import (CSV) speaks for its one account only
    if missing:
        raise statements.MissingAccounts(missing)


def ingest(L: Ledger, path: Path, company: str = "", check=None, csv_account: tuple[str, dict] | None = None) -> dict:
    from fiscalm.api.receipts import Refused

    try:
        with import_lock():
            return _ingest(L, path, company, check, csv_account)
    except Refused as exc:
        message = (
            "Not enough storage space; free space and retry"
            if exc.status == 507
            else "Unsafe statement storage directory; remove symlinks and retry"
        )
        raise statements.StatementError(message) from exc
    except OSError as exc:
        raise statements.StatementError(
            "Cannot read or store statement safely; use a regular file and check storage"
        ) from exc


def _ingest(L: Ledger, path: Path, company: str = "", check=None, csv_account: tuple[str, dict] | None = None) -> dict:
    """Validate before writing; retrying a crash publishes the same immutable document."""
    source = os.open(path, os.O_RDONLY | (0 if str(path).startswith("/proc/self/fd/") else os.O_NOFOLLOW))
    try:
        with os.fdopen(os.dup(source), "rb") as stream:
            data = stream.read(MAX_XML + 1)
        if csv_account:  # a CSV names no account: it was uploaded on one, read with that account's columns
            from fiscalm.bank.csv import parse_csv

            st, ext = parse_csv(data, company, *csv_account), ".csv"
        else:
            st, ext = parse_structured(data, company)
    finally:
        os.close(source)
    digest = identity(data, csv_account)
    if check:
        check(st)
    refuse_incomplete(st)
    refuse_overlap(L, st, digest)
    day = st.period_start.isoformat()
    name = digest + ext
    dest = finance_home().resolve() / st.company / "documents" / "inbox" / day / name
    from fiscalm.bank.csv import NO_TOTALS

    issues = (
        [NO_TOTALS]
        if ext == ".csv"
        else ["parser totals mismatch on " + a.iban[-4:] for a in st.accounts if not a.balanced]
    )
    job = "statement_import:" + digest
    with directory(st.company, day) as fd:
        temp = ".statement-" + secrets.token_hex(16)
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        L.set_meta(
            job,
            json.dumps(
                {
                    "company": st.company,
                    "day": day,
                    "sha": digest,
                    "temp": temp,
                    "ext": ext,
                    **({"csv": list(csv_account)} if csv_account else {}),
                }
            ),
        )
        if csv_account:  # what the stored CSV needs to be read again: its account and columns, beside it, first.
            # Its identity includes them, so a sidecar of this name always says the same: never rewritten.
            side = ".statement-" + secrets.token_hex(16)
            sfd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(sfd, "w") as stream:
                json.dump({"iban": csv_account[0], "mapping": csv_account[1]}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                os.link(side, name + ".json", src_dir_fd=fd, dst_dir_fd=fd)
            os.unlink(side, dir_fd=fd)
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
        added = L.add_document(
            digest,
            st.company,
            st.month,
            "statement",
            str(dest),
            issues=issues,
            source="statement-import",
            accounts=statement_ibans(st),
        )
    if not st.partial:
        hold_csv_days(L, st)
    from fiscalm import recon
    from fiscalm.efactura import inbox as efactura

    paid = recon.auto_mark_paid(L, st)
    ef_paid = efactura.match_statement(L, st)
    L.set_meta(job, "")
    return {
        "file": str(path),
        "status": "added" if added else "duplicate",
        "company": st.company,
        "month": st.month,
        "unprotected": str(dest),
        "transactions": len(st.txs),
        "balanced": not issues,
        "payments_marked_paid": paid,
        "efactura_marked_paid": ef_paid,
    }


def recover(L: Ledger) -> None:
    """Finish journalled publications on the next sync; never regenerate missing bytes."""
    jobs = L.db.execute("SELECT key,value FROM meta WHERE key LIKE 'statement_import:%' AND value != ''").fetchall()
    for row in jobs:
        try:
            _recover_one(L, row)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error):
            logging.getLogger(__name__).warning("Statement recovery failed; journal retained for operator review")


def _recover_one(L: Ledger, row) -> None:
    job = json.loads(row["value"])
    digest, day, company, temp = (job[k] for k in ("sha", "day", "company", "temp"))
    if (
        not re.fullmatch(r"[a-f0-9]{64}", digest)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
        or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,19}", company)
        or not re.fullmatch(r"\.statement-[a-f0-9]{32}", temp)
    ):
        raise statements.StatementError("Invalid statement recovery journal")
    ext = job.get("ext", ".xml")
    csv_account = tuple(job["csv"]) if job.get("csv") else None
    if ext not in (".xml", ".sta", ".csv") or (ext == ".csv") != bool(csv_account):
        raise statements.StatementError("Invalid statement recovery journal")
    with directory(company, day) as fd:
        for name in (digest + ext, temp):
            try:
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                continue
            try:
                with os.fdopen(os.dup(source), "rb") as stream:
                    data = stream.read(MAX_XML + 1)
                if identity(data, csv_account) != digest:
                    raise statements.StatementError("Statement recovery bytes changed; review the stored file")
                ingest(L, Path(f"/proc/self/fd/{source}"), company, None, csv_account)
            finally:
                os.close(source)
            if name == temp:
                os.unlink(temp, dir_fd=fd)
            break
        else:
            raise statements.StatementError("Statement recovery source missing; restore the original file")
