"""A firm's bank coverage: the days fiscalm holds the bank's own record of an account, from stored statements
(PDF, CAMT, MT940), CSV exports and the open banking feed. One answer for every rule that waits for the bank
(automatic payments, reminders, reports): `gaps` lists the days still missing.

A day counts only once its lines went through the matchers, so a payment already made by hand is marked paid before
anything that waits for the bank goes on. A range is `trusted` only when the bank's balances prove its lines complete
(opening + credits - debits = closing, to the cent): a balanced statement. A CSV export or the feed carries no
balances, so it covers its days without that proof; a rule that must not act on an incomplete record asks for
`trusted_only`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from fiscalm import companies
from fiscalm.bank import statements
from fiscalm.ledger import BOUNDARY_DAYS, Ledger

LATE_BOOKING_DAYS = 5  # a booking can reach the bank's feed this many days after the date it carries


@dataclass(frozen=True)
class Range:
    first: date
    last: date
    source: str  # "statement", "csv" or "feed"
    trusted: bool


def recorded(st: statements.Statement) -> dict[str, list]:
    """What an imported statement covers, per account: [first day, last day, trusted]. A whole statement the import
    checked covers its period (a month, even one whose first days had no banking day); a partial one (a CSV) its
    own days. An account with a section the matchers never read (`trustworthy` false: its balances don't prove its
    lines) covers nothing; one is trusted only when it is whole and every section proves its balances."""
    out: dict[str, list] = {}
    withheld = {companies.normalize_iban(a.iban) for a in st.accounts if not a.trustworthy}
    for a in st.accounts:
        first, last, iban = st.period_start, st.period_end, companies.normalize_iban(a.iban)
        if not (iban and first and last and first <= last) or iban in withheld:
            continue
        trusted = not st.partial and a.balanced
        was = out.get(iban, [first.isoformat(), last.isoformat(), True])
        out[iban] = [min(first.isoformat(), was[0]), max(last.isoformat(), was[1]), trusted and was[2]]
    return out


def record(L: Ledger, sha: str, days: dict[str, list]) -> None:
    """Keep what a stored statement covers (`recorded`), once its lines are matched. The same bytes read again (a
    parser fix, a statement stored before coverage was kept) are matched again first, so their reading replaces it,
    an empty one too."""
    with L.tx() as db:
        db.execute("UPDATE documents SET coverage=? WHERE sha=?", (json.dumps(days), sha))


@dataclass(frozen=True)
class _Record:
    """What the ledger holds of one account, read once per answer: its lines with why each is kept from the matchers
    (settled_tx), the source that settles each month (account_month_source), and the lines a copy of the same bank
    reference stands for, whatever day the copy carries."""

    lines: list
    owners: dict[str, str]
    copied: set[str]
    feeds: list[tuple[str, str]]  # each connection's `covered` (JSON) and last read
    documents: list[tuple[str, str]]  # each stored statement's `coverage` (JSON) and path

    @classmethod
    def read(cls, L: Ledger, company: str, iban: str) -> _Record:
        rows = L.db.execute(  # one read: the account's lines and months' sources, the firm's copies, feeds, statements
            "SELECT 'line' AS kind, t.tx_key, t.direction, t.day, t.amount, t.source, s.what FROM statement_txs t "
            "LEFT JOIN settled_tx s ON s.tx_key=t.tx_key WHERE t.company=:co AND t.iban=:iban "
            "UNION ALL SELECT 'owner', month, NULL, NULL, NULL, source, NULL FROM account_month_source "
            "WHERE company=:co AND iban=:iban "
            "UNION ALL SELECT 'copy', substr(s.what, 11), NULL, NULL, NULL, NULL, NULL FROM settled_tx s "
            "JOIN statement_txs t USING(tx_key) WHERE t.company=:co AND s.what LIKE 'duplicate:%' "
            "UNION ALL SELECT 'feed', covered, NULL, last_read, NULL, NULL, NULL FROM bank_connections "
            "WHERE company=:co UNION ALL SELECT 'doc', coverage, NULL, NULL, NULL, path, NULL FROM documents "
            "WHERE company=:co AND kind='statement' AND coverage IS NOT NULL ORDER BY 1, 4, 2",
            {"co": company, "iban": iban},
        ).fetchall()
        return cls(
            [r for r in rows if r["kind"] == "line"],
            {r["tx_key"]: r["source"] for r in rows if r["kind"] == "owner"},
            {r["tx_key"] for r in rows if r["kind"] == "copy"},
            [(r["tx_key"], r["day"]) for r in rows if r["kind"] == "feed"],
            [(r["tx_key"], r["source"]) for r in rows if r["kind"] == "doc"],
        )


def ranges(L: Ledger, company: str, iban: str, record: _Record | None = None) -> list[Range]:
    """Every range of days the firm holds the bank's record of the account `iban`, in no particular order. A source
    covers only the months it settles: a statement or CSV where the feed doesn't (Ledger.record_statement), the feed
    where it does, or holds no line yet. In the other source's month its lines are evidence only, kept from the
    matchers, so they can't show a payment already made."""
    iban = companies.normalize_iban(iban)
    record = record or _Record.read(L, company, iban)
    out = []
    for days, path in record.documents:
        got = json.loads(days).get(iban)
        if got:
            source = "csv" if str(path).endswith(".csv") else "statement"
            out += _settled(record, Range(date.fromisoformat(got[0]), date.fromisoformat(got[1]), source, bool(got[2])))
    for covered_, last_read in record.feeds:
        since = json.loads(covered_).get(iban)
        if since and last_read:
            last = date.fromisoformat(last_read[:10]) - timedelta(LATE_BOOKING_DAYS)
            out += _settled(record, Range(date.fromisoformat(since), last, "feed", False))
    return out


def _settled(record: _Record, r: Range) -> list[Range]:
    """The parts of `r`, month by month, in the months its source settles: the source recorded for the month, else a
    statement, or the feed while no line of the month is on record from any source (then nothing is kept).

    The feed (`covered`, kept by feed._read after matching) runs from the first day it read of the account to
    LATE_BOOKING_DAYS before its last read: each read starts that far before the previous one, so the days between
    hold no gap, and the last ones can still get late bookings."""
    feed, months_with_lines = r.source == "feed", {line["day"][:7] for line in record.lines}
    out, day = [], r.first
    while day <= r.last:
        end, month = min(r.last, _month_end(day)), day.strftime("%Y-%m")
        owner = record.owners.get(month)
        if (owner == "ob") == feed if owner else not feed or month not in months_with_lines:
            out.append(Range(day, end, r.source, r.trusted))
        day = end + timedelta(1)
    return out


def _month_end(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def gaps(
    L: Ledger, company: str, iban: str, first: date, last: date, *, trusted_only: bool = False
) -> list[tuple[date, date]]:
    """The days from `first` to `last` (inclusive) without the bank's record of the account, as ranges in order;
    empty when every day is covered. A day holding a line kept from the matchers as another source's twin, without
    that twin on record (`_unproven`), is not covered: the line may be a payment nothing has matched."""
    iban = companies.normalize_iban(iban)
    record = _Record.read(L, company, iban)
    out, day = [], first
    holes = _unproven(record, first, last)
    held = sorted(
        part
        for r in ranges(L, company, iban, record)
        if r.trusted or not trusted_only
        for part in _without(r.first, r.last, holes)
    )
    for lo, hi in held:
        if hi < day:
            continue
        if lo > last:
            break
        if lo > day:
            out.append((day, lo - timedelta(1)))
        day = max(day, hi + timedelta(1))
        if day > last:
            return out
    return out + [(day, last)] if day <= last else out


def _without(lo: date, hi: date, holes: list[date]) -> list[tuple[date, date]]:
    """The days from `lo` to `hi` without `holes` (sorted), as ranges."""
    out = []
    for h in holes:
        if lo <= h <= hi:
            if lo < h:
                out.append((lo, h - timedelta(1)))
            lo = h + timedelta(1)
    return out + [(lo, hi)] if lo <= hi else out


def _unproven(record: _Record, first: date, last: date) -> list[date]:
    """The days of the account's lines kept from the matchers with nothing on record to stand for them: a line
    reserved as a twin (`twin ...` in settled_tx: Ledger.record_statement, intake.hold_csv_days) without its twin, or
    one whose bank reference carries conflicting money (`bank reference conflict`). A twin is a line of the same
    direction and amount from the other source (the feed's for a statement line, a statement's for the feed's), at
    most BOUNDARY_DAYS apart, or from a CSV the same day (every statement line of a CSV day is the reserved one); it is
    not itself kept from the matchers, nor already the line a copy of the same bank reference (`duplicate:`) stands
    for, and stands for one reserved line. Pairing is a maximum matching over the account's whole history (a chain of
    lines competing for the same twins can run any length), the lines outside first..last served first, so a line
    inside never takes the twin of one outside it and hides its own gap."""
    lines, copied = record.lines, record.copied
    inside = lambda r: first.isoformat() <= r["day"] <= last.isoformat()  # noqa: E731
    reserved = sorted((r for r in lines if (r["what"] or "").startswith("twin ")), key=inside)
    free: dict[tuple, list] = {}  # by direction and amount: a twin has both
    for r in lines:
        if not _kept(r["what"] or "") and r["tx_key"] not in copied:
            free.setdefault((r["direction"], r["amount"]), []).append(r)

    def twins(r) -> list[str]:
        day = date.fromisoformat(r["day"])
        return [
            c["tx_key"]
            for c in free.get((r["direction"], r["amount"]), [])
            if (
                c["day"] == r["day"]
                if r["what"] == "twin csv"
                else c["source"] != r["source"] and abs((date.fromisoformat(c["day"]) - day).days) <= BOUNDARY_DAYS
            )
        ]

    holes = [reserved[i] for i in _unpaired([twins(r) for r in reserved])]
    holes += [r for r in lines if r["what"] == "bank reference conflict"]
    return sorted({date.fromisoformat(r["day"]) for r in holes if inside(r)})


def _kept(what: str) -> bool:
    """A settled_tx reason that keeps a line from the matchers (Ledger.record_statement, intake.hold_csv_days)."""
    return what.startswith(("twin ", "duplicate:")) or what == "bank reference conflict"


def _unpaired(adjacent: list[list[str]]) -> list[int]:
    """The reserved lines (by index) left without a twin by a maximum matching of reserved lines to the free lines
    each may be the twin of (`adjacent`): Kuhn's augmenting paths, searched breadth first without recursion."""
    twin_of: dict[str, int] = {}  # a free line -> the reserved line it is the twin of
    paired: dict[int, str] = {}
    left = []
    for start in range(len(adjacent)):
        came: dict[str, int] = {}  # a free line -> the reserved line the search reached it from
        queue, end = [start], None
        while queue and end is None:
            u = queue.pop(0)
            for c in adjacent[u]:
                if c in came:
                    continue
                came[c] = u
                if c not in twin_of:
                    end = c
                    break
                queue.append(twin_of[c])
        if end is None:
            left.append(start)
            continue
        while True:  # shift every pair along the path by one: each reserved line keeps a twin, `start` gains one
            u = came[end]
            before = paired.get(u)
            twin_of[end], paired[u] = u, end
            if u == start:
                break
            end = before
    return left


def covered(L: Ledger, company: str, iban: str, first: date, last: date, *, trusted_only: bool = False) -> bool:
    return not gaps(L, company, iban, first, last, trusted_only=trusted_only)
