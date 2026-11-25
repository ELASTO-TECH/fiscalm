"""Read-only, evidence-checked reminder text for a human to copy and send."""

from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from datetime import date, timedelta
from decimal import Decimal
from functools import cached_property
from pathlib import Path

from fiscalm import companies, history_invoices, storage
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.identity import firm_id, tax_token
from fiscalm.invoicing import engine as invoicing, issued
from fiscalm.ledger import BOUNDARY_DAYS, Ledger, finance_home, is_live

LABEL = "Text pentru reamintire"
PARTIAL = "Factura are o încasare parțială sau o corecție. Verifică soldul înainte să pregătești textul de reamintire."
SUBJECT = "Reamintire de plată: factura {number} ({company})"
BODY = """Bună ziua,

Factura {number}, emisă de {company}, în valoare de {total} {currency}, a avut scadența pe {due_date}.
Până la această dată, plata nu apare în extrasul de cont. La cerere, factura poate fi retransmisă.
Dacă plata a fost efectuată, vă rugăm să ne transmiteți dovada plății.

Vă mulțumim."""

CLOSED = "Factura nu mai este eligibilă. Verifică încasarea și corecțiile."
STATEMENTS = "Extrasele lipsesc, nu sunt actualizate sau nu pot fi verificate. Sunt necesare extrase recente care pot fi verificate."
CONFIG = "Configurarea reamintirilor nu este validă."
SETTINGS_BAD = "Numărul de zile poate fi între 1 și 365."
SETTINGS_SAVED = "Setările reamintirilor au fost salvate."


def ro(value: date | Decimal) -> str:
    """Romanian dates and two-decimal amounts for reminder copy."""
    return (
        value.strftime("%d.%m.%Y")
        if isinstance(value, date)
        else format(value, ",.2f").translate(str.maketrans(",.", ".,"))
    )


class Refused(ValueError):
    pass


# A firm's reminder settings (Automatizări, Reamintiri de plată): the first after `after_days` past the due date, the
# Local text only; fiscalm does not schedule or send messages. Stored per firm in the ledger; DEFAULTS until set.
SETTINGS = ("after_days",)
DEFAULTS = {"after_days": 7}
LIMITS = {"after_days": 365}
# A reminder needs a statement this recent: an older one could miss the payment that makes it wrong.
STATEMENT_MAX_AGE_DAYS = 7
MAX_ZIP = 20 * 1024 * 1024  # a stored SPV message read back to prove ownership
DAYS = (date(1900, 1, 1), date(9000, 1, 1))  # the invoice days a reminder works with
# The settled_tx reservation that keeps a credit from settling though it paid nothing yet.
UNEXPLAINED = "bank reference conflict"


def _meta_key(co: str) -> str:
    return f"client_reminders:{co}"


def settings(L: Ledger, co: str) -> dict:
    """The firm's reminder settings, with `source` `firm` once set in Automatizări, else `default`. Refuses
    (CONFIG) a stored value out of its limits, so a broken setting prepares nothing."""
    raw = L.get_meta(_meta_key(co))
    if raw is None:
        return DEFAULTS | {"source": "default"}
    try:
        stored = json.loads(raw)
    except ValueError:
        raise Refused(CONFIG) from None
    if (
        not isinstance(stored, dict)
        or set(stored) != set(SETTINGS)
        or any(type(stored[k]) is not int or not 1 <= stored[k] <= LIMITS[k] for k in SETTINGS)
    ):
        raise Refused(CONFIG)
    return {k: stored[k] for k in SETTINGS} | {"source": "firm"}


def set_settings(L: Ledger, co: str, values: dict) -> dict:
    """Save the firm's reminder settings: every one a whole number from 1 to its limit, or nothing is saved. Changes
    only when local reminder text may be shown; no mail account is changed."""
    companies.get(co, L=L)
    if set(values) != set(SETTINGS):
        raise Refused(SETTINGS_BAD)
    out = {}
    for k in SETTINGS:
        v = values[k].strip() if isinstance(values[k], str) else ""
        if not re.fullmatch(r"[0-9]{1,3}", v) or not 1 <= int(v) <= LIMITS[k]:  # ASCII digits only
            raise Refused(SETTINGS_BAD)
        out[k] = int(v)
    L.set_meta(_meta_key(co), json.dumps(out, sort_keys=True))
    return out


def policy(L: Ledger, co: str) -> tuple[int, int]:
    """Minimum overdue days and maximum age of trustworthy statement evidence."""
    return settings(L, co)["after_days"], STATEMENT_MAX_AGE_DAYS


class StatementView:
    """Request-local, lazy statement snapshot; no subprocesses under the write lock."""

    def __init__(self, L: Ledger, co: str, since: date | None = None):
        """`since`: the earliest day any caller will ask credits from, so the account lines are read once."""
        self.L, self.co, self.since = L, co, since
        self.parsed: dict[str, statements.Statement] = {}
        self.versions: dict[str, tuple] = {}
        self._policy: tuple[int, int] | None = None
        self._lines: tuple[date, list, set] | None = None  # (since, credit lines, explained keys): read once

    def policy(self) -> tuple[int, int]:
        """The firm's policy, read once per request."""
        if self._policy is None:
            self._policy = policy(self.L, self.co)
        return self._policy

    @cached_property
    def issued(self) -> dict[str, dict]:
        """The firm's `issued` rows by message id, read once."""
        return {r["message_id"]: r for r in self.L.issued_rows(company=self.co)}

    @cached_property
    def client_accounts(self) -> dict[str, set[str]]:
        accounts: dict[str, set[str]] = {}
        for row in self.issued.values():
            if row["collected_from_iban"]:  # the CUI as the reminder rows carry it (api.state.tax_id)
                cui = tax_token(row["client_tax_id"] or row["client_cui"])
                accounts.setdefault(row["collected_from_iban"], set()).add(cui)
        return accounts

    def documents(self, since: date) -> list[dict]:
        # Statements may span months; use parsed periods for coverage.
        return [dict(d) for d in self.L.documents(company=self.co, kind="statement")]

    def version(self, doc: dict) -> tuple:
        path = Path(doc["path"])
        root = finance_home().resolve() / self.co
        if not path.resolve().is_relative_to(root):
            raise Refused(STATEMENTS)
        stat = path.stat()
        return tuple(sorted(doc.items())), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def credits(self, since: date, today: date, max_age: int) -> list[statements.Tx]:
        """Every representation, from `since` to `today`, of a credit that may still be this firm's client paying,
        once its statements cover every current account: the lines on record (statements, CSV and the bank feed
        alike) and the lines of the stored statements just parsed, since copies of one payment may name its payer
        differently. A payment is left out only when proved to be something else (`_explained`). A credit on record
        without its details refuses: it could be the client's."""
        self.coverage(since, today, max_age)
        if self._lines is None or since < self._lines[0]:
            start = min(since, self.since or since)
            lo = start - timedelta(days=BOUNDARY_DAYS)  # an explained copy can sit just before
            lines = self.L.credit_lines(self.co, lo.isoformat())
            self._lines = (start, lines, _explained(lines))
        explained = self._lines[2]
        out = []
        for r in self._lines[1]:
            if r["tx_key"] in explained or not since.isoformat() <= r["day"] <= today.isoformat():
                continue
            if not r["tx"]:
                raise Refused(STATEMENTS)
            out.append(statements.Tx.from_dict(json.loads(r["tx"])))
        for st in self.parsed.values():
            out += [
                t
                for t in st.settlement_txs
                if t.direction == "credit" and since <= t.date <= today and statements.tx_key(t) not in explained
            ]
        return out

    def coverage(self, since: date, today: date, max_age: int) -> None:
        """Refuse (STATEMENTS) unless stored statements cover every current account from `since` to `max_age` days
        before `today`."""
        docs = self.documents(since)
        # Every current account must be covered, not one per currency: a payment into any of them would be missed.
        coverage: dict[str, list[tuple[date, date]]] = {
            a.iban: [] for a in companies.get(self.co, L=self.L).current_accounts
        }
        try:
            for doc in docs:
                key = doc["sha"]
                version = self.version(doc)
                if key not in self.parsed:
                    with Path(doc["path"]).open("rb") as stream:
                        opened = Path(f"/proc/{os.getpid()}/fd/{stream.fileno()}")
                        if not opened.resolve().is_relative_to(finance_home().resolve() / self.co):
                            raise Refused(STATEMENTS)
                        # Share the content cache with the rest of api/state, after checking the opened file.
                        from fiscalm.api.state import _statement

                        self.parsed[key] = _statement(opened, self.co)
                    self.versions[key] = version
                if self.versions[key] != version:
                    raise Refused(STATEMENTS)
                st = self.parsed[key]
                if st.company != self.co or not st.accounts or any(not a.trustworthy for a in st.accounts):
                    raise Refused(STATEMENTS)
                if not st.period_start or not st.period_end or st.period_start > st.period_end:
                    raise Refused(STATEMENTS)
                for account in st.accounts:
                    iban = re.sub(r"\s+", "", account.iban).upper()
                    if iban in coverage:
                        coverage[iban].append((st.period_start, st.period_end))
        except (OSError, ValueError):
            raise Refused(STATEMENTS) from None
        if not coverage:
            raise Refused(STATEMENTS)
        until = max(since, today - timedelta(days=max_age))
        for periods in coverage.values():
            covered = since - timedelta(days=1)
            for start, end in sorted(periods):
                if start > covered + timedelta(days=1):
                    break
                covered = max(covered, end)
                if covered >= until:
                    break
            if covered < until:
                raise Refused(STATEMENTS)


def _explained(rows: list) -> set[str]:
    """The keys of credit lines proved to be something other than an unexplained payment: an original line reserved
    in `settled_tx` as collecting or explaining an item; a duplicate line whose original is; a twin (the other
    source's copy of a payment) whose every possible copy among the original lines is explained, paired one to one
    with them, closest days first: one explained payment excuses one twin, never two, and a duplicate is never
    a copy of its own. A conflict is never explained."""
    what = {r["tx_key"]: r["what"] or "" for r in rows}
    copy_of = lambda w: w.startswith(("twin ", "duplicate:"))  # noqa: E731
    originals = [r for r in rows if not copy_of(what[r["tx_key"]])]
    done = {r["tx_key"] for r in originals if what[r["tx_key"]] and not what[r["tx_key"]].startswith(UNEXPLAINED)}
    excused = {k for k, w in what.items() if w.startswith("duplicate:") and w.removeprefix("duplicate:") in done}
    pairs = []
    for t in (r for r in rows if what[r["tx_key"]].startswith("twin ")):
        copies = [e for e in originals if _copy(t, e)]
        if copies and all(e["tx_key"] in done for e in copies):
            day = date.fromisoformat(t["day"])
            pairs += [(abs((day - date.fromisoformat(e["day"])).days), t["tx_key"], e["tx_key"]) for e in copies]
    used: set[str] = set()
    for _, twin, copy in sorted(pairs):
        if twin not in excused and copy not in used:
            excused.add(twin)
            used.add(copy)
    return done | excused


def _copy(twin, line) -> bool:
    """`line` could be the other source's copy of `twin`: brought by another source (two lines of one source are two
    payments), same amount, same account (or one unknown), days apart at most BOUNDARY_DAYS (Ledger.account_lines
    pairs twins the same way)."""
    apart = abs((date.fromisoformat(twin["day"]) - date.fromisoformat(line["day"])).days)
    same_account = not twin["iban"] or not line["iban"] or twin["iban"] == line["iban"]
    other_source = twin["source"] != line["source"]
    return other_source and twin["amount"] == line["amount"] and same_account and apart <= BOUNDARY_DAYS


def _ours(L: Ledger, co: str, stored: dict) -> bool:
    """An invoice synced from SPV is the firm's only when its stored XML says so: the very document the row was
    read from (xml_sha) names the firm as supplier (history_invoices.direction: country-aware tax ids, so neither a
    missing supplier nor a foreign VAT id with the same digits passes). The path is rebuilt from the numeric message
    id and read only inside the firm's own e-Factura directory (storage.read_file: checked on the opened file)."""
    root = (finance_home() / "efactura").resolve() / co  # the firm's own name, never a link to another firm's
    try:
        path = issued.raw_path(co, stored["message_id"], stored["listed_at"] or "")
    except ValueError:
        return False
    if not (read := storage.read_file(path, [root], MAX_ZIP)):
        return False
    try:
        inv, sha = efactura.invoice_from_zip(read[1])
    except efactura.UNREADABLE:  # UblError is a ValueError
        return False
    return sha == stored["xml_sha"] and history_invoices.direction(companies.get(co, L=L), inv) == "issued"


def _shift(day: str, days: int) -> date:
    """`day` (ISO) moved by `days`; one outside DAYS refuses that invoice alone (CLOSED), so the windows built on
    it (a lookback, a twin's copy days before) never leave the calendar."""
    try:
        out = date.fromisoformat(day) + timedelta(days=days)
    except (TypeError, ValueError, OverflowError):
        raise Refused(CLOSED) from None
    if not DAYS[0] <= out <= DAYS[1]:
        raise Refused(CLOSED)
    return out


def eligible(
    L: Ledger,
    co: str,
    ident: str,
    today: date,
    *,
    rows: list[dict] | None = None,
    view: StatementView | None = None,
    row_index: dict | None = None,
    invoices: dict[str, dict] | None = None,
) -> dict:
    """The open, unpaid, overdue issued invoice `ident` (authored in fiscalm, or synced from SPV) a reminder may be
    prepared for, with its due date; refuses one a client credit not yet explained could have paid. Cheap ledger
    refusals precede the bank reads; a caller may reuse one statement snapshot."""
    from fiscalm.api import state as api

    rows = rows if rows is not None else api._issued(L, co, today, is_live())
    row = (
        row_index.get(ident)
        if row_index is not None
        else next((r for r in rows if ident in (r["id"], r.get("local_id"))), None)
    )
    if not row or row["status"] != "open" or row.get("rejected") or Decimal(row["owed"]) <= 0:
        raise Refused(CLOSED)
    if Decimal(row["owed"]) != Decimal(row["total"]) or Decimal(row["credited"]) or row.get("reversed_by"):
        raise Refused(PARTIAL)
    view = view or StatementView(L, co)
    name = row["client"] or ""
    if not firm_id(row["client_cui"]) and not efactura.same_firm(name, name):
        raise Refused(CLOSED)  # no buyer a payment could be told from (the payer matcher's own rule): no text
    after, max_age = view.policy()
    due_date = row.get("due_date")
    if iid := row.get("local_id"):  # authored in fiscalm: its own record must still agree with the row
        inv = invoices.get(iid) if invoices is not None else invoicing.invoice(L, iid, company=co)
        if inv is None or inv["status"] != "issued" or inv["data"]["totals"]["total"] != row["total"]:
            raise Refused(CLOSED)
        due_date = due_date or inv["data"].get("due_date")
    if not due_date or today < _shift(due_date, after):
        raise Refused(CLOSED)
    if not iid:  # synced from SPV: read its stored XML only once everything cheap passed
        stored = view.issued.get(row["id"])
        if not stored or stored["parse_error"] or not _ours(L, co, stored):
            raise Refused(CLOSED)  # not proved this firm's to claim
    row = row | {"due_date": due_date}
    since = _shift(row["issue_date"], -issued.COLLECT_DAYS_BEFORE)
    for tx in view.credits(since, today, max_age):
        if issued.credit_from_client(
            L, co, row["client"] or "", row["client_cui"] or "", tx, accounts=view.client_accounts
        ):
            raise Refused(PARTIAL)
    return row


def tasks(L: Ledger, co: str, today: date, rows: list[dict], *, invoices: dict[str, dict] | None = None) -> list[dict]:
    starts = []
    for row in rows:
        with suppress(Refused):
            starts.append(_shift(row["issue_date"], -issued.COLLECT_DAYS_BEFORE))
    since = min(starts, default=None)
    out, view = [], StatementView(L, co, since)
    row_index: dict = {}
    for row in rows:
        for ident in (row["id"], row.get("local_id")):
            row_index.setdefault(ident, row)
    for row in rows:
        try:
            row = eligible(L, co, row["id"], today, rows=rows, view=view, row_index=row_index, invoices=invoices)
        except ValueError:
            continue
        fields = dict(
            number=row["number"],
            company=companies.get(co, L=L).legal_name,
            total=ro(Decimal(row["total"])),
            currency=row["currency"],
            due_date=ro(date.fromisoformat(row["due_date"])),
        )
        out.append(
            dict(
                kind="client_reminder",
                id=row["id"],
                label=LABEL,
                subject=SUBJECT.format(**fields),
                body=BODY.format(**fields),
            )
        )
    return out
