"""Content-addressed CAMT storage; journal before publishing, retain the source for retries."""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
from collections.abc import Collection, Iterator
from contextlib import contextmanager, suppress
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fiscalm import companies
from fiscalm.api.receipts import directory
from fiscalm.bank import banking_days, coverage, statements
from fiscalm.bank.camt import MAX_XML, parse_camt
from fiscalm.ledger import Ledger, finance_home
from fiscalm.pdftext import pdf_text
from fiscalm.storage import read_file


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


def refuse_overlap(L: Ledger, st: statements.Statement, digest: str, siblings: Collection[str] = ()) -> None:
    """One document per account and month: a statement of another bank (other accounts) for the same month is
    fine, the same account twice is not. `siblings`: the other files of the set `st` arrives in (`combine`), checked
    with it as one statement; a stored one of them is this set imported before, not another statement. The months the period touches and their neighbours are read, and every CSV
    of the company (one may span months; it is stored under its first). A statement stored
    before accounts were recorded is read to learn its accounts; if it can't be read, nothing is imported."""
    if not st.period_start or not st.period_end:
        raise statements.StatementError("Cannot verify statement period")
    refuse_unsound_accounts(st, L)
    lo, hi = span(st)
    mine = set(statement_ibans(st))
    if not mine:
        raise statements.StatementError("Statement names no account")
    months = _months(lo.replace(day=1) - timedelta(days=1), hi.replace(day=28) + timedelta(days=4))
    docs = [
        d
        for m in sorted(months, key=lambda m: m != st.month)
        for d in L.documents(company=st.company, month=m, kind="statement")
    ]
    # a CSV may span months and is stored under its first: every one of the company's is checked
    docs += [d for d in _csv_docs(L, st.company) if d["month"] not in months]
    for doc in docs:
        month = doc["month"]
        if doc["sha"] == digest or doc["sha"] in siblings:
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
        theirs_lo, theirs_hi = span(previous)
        if lo <= theirs_hi and theirs_lo <= hi:
            raise statements.StatementError("Overlapping statement already imported")


def refuse_unsound_accounts(st: statements.Statement, L: Ledger | None = None) -> None:
    """One statement holds an account's days once, in the currency the firm keeps it in: two sections of one account
    for the same days, or an account in another currency, settle nothing."""
    co = companies.get(st.company, L=L)
    seen: dict[tuple[str, str], list[tuple[date, date]]] = {}
    for a in st.accounts:
        own = co.account(a.iban)
        if own and own.currency != a.currency:
            raise statements.StatementError("Statement account currency differs from the firm's account")
        lo, hi = a.period or (st.period_start, st.period_end)
        run = seen.setdefault((companies.normalize_iban(a.iban), a.currency), [])
        if any(lo <= theirs_hi and theirs_lo <= hi for theirs_lo, theirs_hi in run):
            raise statements.StatementError("Statement: an account appears twice for the same days")
        run.append((lo, hi))


def span(st: statements.Statement) -> tuple[date, date]:
    """Every day a statement covers: from its accounts' printed periods (one may start later than another), else
    its own."""
    periods = [a.period for a in st.accounts if a.period]
    if not periods:
        return st.period_start, st.period_end
    return min(p[0] for p in periods + [(st.period_start,) * 2]), max(p[1] for p in periods + [(st.period_end,) * 2])


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
    the answer is kept on the document, so it is read once; a file that can't be read, or that reads as another
    firm's, or has a section that isn't the firm's account in its currency for days of the row's month (a PDF reads
    under the firm it is opened for, so its accounts are what prove it is that firm's), proves no account."""
    if doc["accounts"]:
        return json.loads(doc["accounts"])
    try:
        st = statements.parse(Path(doc["path"]), doc["company"])
    except (OSError, RuntimeError, ValueError):
        return []
    found = statement_ibans(st)
    if st.company != doc["company"] or not all(_section_of(L, st, a, doc["month"]) for a in st.accounts):
        return []
    if found:
        with L.tx() as db:
            db.execute(
                "UPDATE documents SET accounts=? WHERE sha=? AND accounts IS NULL", (json.dumps(found), doc["sha"])
            )
    return found


def _section_of(L: Ledger, st: statements.Statement, a: statements.Account, month: str) -> bool:
    """A statement section is the firm's account, in the currency the firm keeps it in, for days of `month` only."""
    own = companies.get(st.company, L=L).account(a.iban)
    lo, hi = a.period or (st.period_start, st.period_end)
    return bool(
        own
        and own.currency == a.currency
        and lo
        and hi
        and lo <= hi
        and lo.strftime("%Y-%m") == hi.strftime("%Y-%m") == month
    )


def hold_csv_days(L: Ledger, st: statements.Statement) -> None:
    """A full statement arriving after CSVs of the same account: its lines on the days a CSV already brought are kept
    as evidence and reserved (`twin csv`), so the CSV's lines, which may already have settled something, stay the
    ones that count for those days, and the same payment never settles twice. Its other days settle as usual."""
    ranges: dict[str, list[tuple]] = {}
    for doc in _csv_docs(L, st.company):  # any month: a CSV may span several
        csv = _read_stored(doc, st.company)
        lo, hi = span(st)
        if csv.period_end < lo or csv.period_start > hi:
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


def _read_stored(doc, company: str) -> statements.Statement:
    try:
        return statements.parse(Path(doc["path"]), company)
    except (OSError, RuntimeError) as exc:
        raise StoredStatementUnavailable(
            "Cannot read a stored statement; restore the stored document and retry"
        ) from exc


def missing_accounts(st: statements.Statement, L: Ledger | None = None) -> list[str]:
    """The company's accounts at the statement's banks that the statement leaves out: a statement covers every
    account the company held at its bank in its period, so none of them is silently skipped. An account opened after
    the period's last month (set by a person) or retired before its first day is not owed; without a period, or with
    sections naming different periods, or a period that ends before it starts, every account is. Accounts at other banks come
    from their own statements. An account's bank is the one configured for it, else the catalogue's for its IBAN code,
    else the IBAN code itself: two accounts with the same code outside the catalogue are one bank, so a statement
    with one of them must carry both; a different code is another bank, with its own statements."""
    from fiscalm.bank import catalogue as banks

    co = companies.get(st.company, L=L)

    def bank_of(iban: str) -> str:
        own = co.account(iban)
        if own and own.bank:
            return own.bank
        b = banks.for_iban(iban)
        # outside the catalogue, the IBAN's own country and bank code name the bank
        return b.id if b else "code:" + iban[:2] + iban[4:8]

    present = {(companies.normalize_iban(a.iban), a.currency) for a in st.accounts}
    held = {bank_of(i) for i, _ in present}
    first, last = st.period_start, st.period_end or st.period_start
    if not st.one_period or (first and last and last < first):  # periods that disagree or run backwards: all owed
        first = last = None
    expected = {(a.iban, a.currency) for a in co.accounts if bank_of(a.iban) in held and a.held_between(first, last)}
    return sorted(f"{ccy} {iban[-4:]}" for iban, ccy in expected - present)


def combine(parts: list[statements.Statement]) -> statements.Statement:
    """BT PDFs of one company and month, read as the one statement they make together: one file with every account,
    one file per account (older exports), or daily statements. Together they cover the whole calendar month, and every
    account's days exactly once, without a gap. Each file keeps its own accounts and its own trust (a file's lines settle only when
    its balances add up), as when a stored file is read again on its own; two consecutive files of an account whose
    balances are proven must chain (each opens where the one before closed), or a statement between them is missing.
    An account's days may start after the 1st only where none is left out (`_late_start`). Older daily statements
    come only for days with movements: numbered ones one after the other may leave days between them, and the month
    may start or end with such days, proven by the neighbouring months' statements (`refuse_unchained`, the caller's,
    completeness is a warning, `month_missing`)."""
    if any(not p.period_start or not p.period_end or p.period_end < p.period_start for p in parts):
        raise statements.StatementError("Cannot verify statement period")
    first, last = min(p.period_start for p in parts), max(p.period_end for p in parts)
    if len(parts) == 1 and parts[0].one_period and first.day == 1 and (last + timedelta(days=1)).day == 1:
        return parts[0]  # the whole month in one file, one period: as printed
    if any(p.partial for p in parts):
        raise statements.StatementError("Statement set: every file must name one period")
    runs: dict[tuple[str, str], list[tuple[date, date, statements.Account]]] = {}
    for p in parts:
        for a in p.accounts:
            lo, hi = _span(p, a)
            runs.setdefault((companies.normalize_iban(a.iban), a.currency), []).append((lo, hi, a))
    spans = [s for run in runs.values() for s in run] or [(p.period_start, p.period_end, None) for p in parts]
    first, last = min(lo for lo, _, _ in spans), max(hi for _, hi, _ in spans)
    last = _month_end(last)
    if first.strftime("%Y-%m") != last.strftime("%Y-%m"):
        raise statements.StatementError("Statement set: files of different months")
    co = companies.get(parts[0].company)
    for run in runs.values():
        _refuse_gaps(sorted(run, key=lambda s: s[0]), last, co)
    # the whole month: its accounts are those held from the 1st (each section keeps its own printed days)
    return statements.Statement(parts[0].company, last.replace(day=1), last, [a for p in parts for a in p.accounts])


def _span(p: statements.Statement, a: statements.Account) -> tuple[date, date]:
    """The days an account's section covers: the period printed above it, else its file's single period."""
    if a.period:
        lo, hi = a.period
    elif p.one_period:
        lo, hi = p.period_start, p.period_end
    else:
        raise statements.StatementError("Statement set: every file must name one period")
    if hi < lo:
        raise statements.StatementError("Cannot verify statement period")
    return lo, hi


def _month_end(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _consecutive(before: statements.Account, after: statements.Account) -> bool:
    """Two proven daily statements of one year with no other between them: the next number. Across a new year the
    numbering may restart, so no number proves that the year before ended with the last statement it holds."""
    if before.serial is None or after.serial is None or not (before.trustworthy and after.trustworthy):
        return False
    return after.serial == before.serial + 1 and before.period[1].year == after.period[0].year


def opening(a: statements.Account) -> Decimal:
    """A proven account's opening balance: its closing one less what its lines moved."""
    return a.closing - a.total_credit + a.total_debit


def _late_start(start: date, a: statements.Account, co) -> bool:
    """An account's month may start after the 1st only when the days before could hold no movement, and its opening
    balance is proven: no banking day before `start` (weekends, legal holidays), or the account opened on that exact
    day (set by a person, `company opened` YYYY-MM-DD; only non-banking days between) and opened at zero."""
    month = start.replace(day=1)
    if not a.trustworthy:
        return False
    if banking_days.none_between(month, start) or a.serial is not None:  # numbered: the month before proves it
        return True
    own = co.account(a.iban)
    day = own.opened if own and len(own.opened) == 10 else ""
    if not day or not month.isoformat() <= day <= start.isoformat():
        return False
    return banking_days.none_between(date.fromisoformat(day), start) and opening(a) == 0


def _refuse_gaps(run: list[tuple[date, date, statements.Account]], last: date, co) -> None:
    """One account's sections, by first day: each day from the month's start to `last` once, proven balances
    chaining."""
    day, before = last.replace(day=1), None
    if run[0][0] > day and _late_start(run[0][0], run[0][2], co):
        day = run[0][0]
    for lo, hi, a in run:
        if lo < day:
            raise statements.StatementError("Statement set: an account appears twice for the same days")
        numbered = before is not None and (before.serial is not None or a.serial is not None)
        if (lo > day or numbered) and not (before and _consecutive(before, a)):  # numbered: one after the other
            raise statements.PartialMonth("Statement set", last.replace(day=1))
        # trustworthy: opening + credits - debits == closing, to the cent, so the opening is known
        proven = before is not None and before.trustworthy and a.trustworthy
        if proven and opening(a) != before.closing:
            raise statements.StatementError("Statement set: balances do not chain; a statement is missing")
        day, before = hi + timedelta(days=1), a
    end = last + timedelta(days=1)  # a numbered last statement: the month after proves it
    if day != end and before.serial is None:
        raise statements.PartialMonth("Statement set", last.replace(day=1))


def refuse_unchained(L: Ledger, st: statements.Statement, others: Collection[statements.Statement] = ()) -> None:
    """A month that starts after the 1st must open where the previous month's stored statement of that account
    closed, when one is on record: otherwise days between them are missing. Checked from both sides, so the order the
    months arrive in changes nothing: a stored next month that starts after the 1st must open where this one closes.
    Numbered daily statements need their neighbours, stored or among `others` (the statements imported with it): the
    month's first one is the first of its year or follows the previous month's last, and its last one is followed by
    the next month's first, by number and balance. A stored statement that can't be read, or a balance that isn't
    proven, can't show that: nothing is imported."""
    co = companies.get(st.company)
    for iban, sections in _by_account(st).items():
        (lo, first), (_, last) = sections[0], sections[-1]
        month_end = _month_end(lo)
        for (_, a), (nxt, _) in zip(sections, sections[1:], strict=False):  # days between daily statements
            _refuse_booked(L, st.company, iban, (a.period[1] if a.period else st.period_end) + timedelta(days=1), nxt)
        if lo.day != 1:
            _refuse_booked(L, st.company, iban, lo.replace(day=1), lo)
            before = _neighbours(L, st, lo.replace(day=1) - timedelta(days=1), iban, others)
            if before:
                _refuse_break(before[-1][1], first, numbered=first.serial is not None)
            elif first.serial not in (None, 1) and not _late_start_unnumbered(lo, first, co):
                raise statements.StatementError(DAILY_EDGE)
        end = last.period[1] if last.period else st.period_end
        after = _neighbours(L, st, month_end + timedelta(days=1), iban, others)
        if end < month_end:  # only a numbered daily statement gets here (combine)
            _refuse_booked(L, st.company, iban, end + timedelta(days=1), month_end + timedelta(days=1))
            if not after:
                raise statements.StatementError(DAILY_EDGE)
            _refuse_break(last, after[0][1], numbered=True)
        elif after and after[0][0].day != 1:
            _refuse_break(last, after[0][1], numbered=after[0][1].serial is not None)


DAILY_EDGE = (
    "Daily statements: the month's first or last statement can't be shown to be its first or last; import the month"
    " together with the previous and the next month's statements"
)


def _late_start_unnumbered(start: date, a: statements.Account, co) -> bool:
    return _late_start(start, dataclasses.replace(a, serial=None), co)


def _neighbours(L: Ledger, st, day: date, iban: str, others) -> list[tuple[date, statements.Account]]:
    """The account's sections in `day`'s month: the stored ones when that month is on record, else those of a month
    imported together with `st` (`others`: the firm's months that passed their own checks)."""
    if stored := _stored_sections(L, st.company, day, iban):
        return stored
    month = day.strftime("%Y-%m")
    found = [
        s
        for o in others
        if o is not st and o.company == st.company and o.month == month
        for s in _by_account(o).get(iban, [])
    ]
    return sorted(found, key=lambda s: s[0])


def _by_account(st: statements.Statement) -> dict[str, list[tuple[date, statements.Account]]]:
    """Each account's sections by the first day they cover."""
    out: dict[str, list[tuple[date, statements.Account]]] = {}
    for a in st.accounts:
        lo = a.period[0] if a.period else st.period_start
        out.setdefault(companies.normalize_iban(a.iban), []).append((lo, a))
    return {k: sorted(v, key=lambda s: s[0]) for k, v in out.items()}


def _stored_sections(L: Ledger, company: str, day: date, iban: str) -> list[tuple[date, statements.Account]]:
    """The account's sections in the stored statements of `day`'s month (one file, or one per day), by first day. A
    document on record for the account that shows none of its sections can't show where it closed: nothing is
    imported (one stored before accounts were recorded shows which it covers when read)."""
    found = []
    for doc in L.documents(company=company, month=day.strftime("%Y-%m"), kind="statement"):
        named = json.loads(doc["accounts"]) if doc["accounts"] else None
        if str(doc["path"]).endswith(".csv") or (named is not None and iban not in named):
            continue
        sections = _by_account(_read_evidence(L, doc, company)).get(iban, [])
        if named is not None and not sections:
            raise StoredStatementUnavailable(
                "A stored statement of a neighbouring month does not show this account; restore it and retry"
            )
        found += sections
    return sorted(found, key=lambda s: s[0])


def _read_evidence(L: Ledger, doc, company: str) -> statements.Statement:
    """A stored statement read as evidence: inside the company's own storage, on the opened file, its bytes the ones
    stored (the decrypted copy's digest for a PDF), the firm's CUI in a PDF's header."""
    root = finance_home().resolve() / company
    data = read_file(doc["path"], [root], 32 * 1024 * 1024)
    pdf = bool(data) and data[1].startswith(b"%PDF-")
    expected = L.get_meta("statement_stored_sha:" + doc["sha"]) if pdf else doc["sha"]
    if not data or not expected or identity(data[1]) != expected:
        raise StoredStatementUnavailable("Cannot read a stored statement; restore the stored document and retry")
    if not pdf:
        return parse_structured(data[1], company)[0]
    try:
        text = pdf_text(data[1])
    except RuntimeError as exc:
        raise StoredStatementUnavailable(
            "Cannot read a stored statement; restore the stored document and retry"
        ) from exc
    if not statements.names_cui(text, companies.get(company, L=L).cui):
        raise StoredStatementUnavailable("Cannot read a stored statement; restore the stored document and retry")
    return statements.parse_text(text, company)


def _refuse_booked(L: Ledger, company: str, iban: str, lo: date, hi: date) -> None:
    """No line already on record for the account (a CSV, the bank feed) is booked on the days a statement skips."""
    if L.db.execute(
        "SELECT 1 FROM statement_txs WHERE company=? AND iban=? AND day>=? AND day<?",
        (company, iban, lo.isoformat(), hi.isoformat()),
    ).fetchone():
        raise statements.PartialMonth("Statement", lo.replace(day=1))


def _refuse_break(before: statements.Account, after: statements.Account, numbered: bool = False) -> None:
    """`after`, starting after its month's 1st, opens where `before` closed: on the eve of `after`'s month, or, for
    numbered daily statements, as the next one."""
    month = (after.period[0] if after.period else after.closing_day).replace(day=1)
    if numbered and not _consecutive(before, after):
        raise statements.StatementError(DAILY_EDGE)
    if not numbered and before.closing_day != month - timedelta(days=1):
        raise statements.StatementError("Statement balances do not chain with the next or previous month")
    if before.closing is None or not after.trustworthy:
        raise statements.StatementError(
            "Statement starts after the 1st and a neighbouring month's balance is not proven"
        )
    if opening(after) != before.closing:
        raise statements.StatementError("Statement balances do not chain with the next or previous month")


def month_missing(L: Ledger, company: str, month: str) -> list[str] | None:
    """The accounts the month's stored statements leave out at their banks (missing_accounts of them together); a
    statement of one account is imported on its own, and this says which ones are still to come. None when a stored
    statement names no account (stored before accounts were recorded) or one that isn't the firm's, or the firm is
    unknown: nobody can say the month is complete. Reads only the ledger, never a stored file."""
    if company not in companies.all(L):
        return None
    co = companies.get(company, L=L)
    docs = [d for d in L.documents(company=company, month=month, kind="statement") if not d["path"].endswith(".csv")]
    learned = [json.loads(d["accounts"] or "[]") for d in docs]
    owned = [co.account(i) for ibans in learned for i in ibans]
    if not all(learned) or not all(owned):
        return None
    first = date.fromisoformat(month + "-01")
    accounts = [statements.Account(a.iban, a.currency) for a in owned]
    st = statements.Statement(company, first, _month_end(first), accounts)
    return missing_accounts(st, L) if accounts else []


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
    refuse_overlap(L, st, digest)
    if not st.partial:  # a CAMT or MT940 month next to a BT month that starts after the 1st
        refuse_unchained(L, st)
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
    coverage.record(L, digest, coverage.recorded(st))  # last: its days count only once its lines are matched
    L.set_meta(job, "")
    return {
        "file": str(path),
        "status": "added" if added else "duplicate",
        "company": st.company,
        "month": st.month,
        "unprotected": str(dest),
        "transactions": len(st.txs),
        "balanced": not issues,
        "missing": [] if st.partial else month_missing(L, st.company, st.month) or [],
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
