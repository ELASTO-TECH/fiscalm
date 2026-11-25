"""Read-only, evidence-checked reminder text for a human to copy and send."""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from decimal import Decimal
from functools import cached_property
from pathlib import Path

from fiscalm import companies
from fiscalm.bank import statements
from fiscalm.identity import tax_token
from fiscalm.invoicing import engine as invoicing, issued
from fiscalm.ledger import Ledger, finance_home, is_live

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
        self._lines: tuple[date, list] | None = None  # (since, account_lines from since): one read per request

    def policy(self) -> tuple[int, int]:
        """The firm's policy, read once per request."""
        if self._policy is None:
            self._policy = policy(self.L, self.co)
        return self._policy

    @cached_property
    def client_accounts(self) -> dict[str, set[str]]:
        accounts: dict[str, set[str]] = {}
        for row in self.L.issued_rows(company=self.co):
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
        """The firm's unexplained credits from `since` to `today`, once its statements cover every current account.
        The credits come from the ledger's account lines (statements, CSV and the bank feed alike); a credit already
        in `settled_tx` collected or explains something else, so it says nothing about this invoice. A twin line
        (the other source's copy of a payment) is left out by `account_lines` when its copy is there."""
        self.coverage(since, today, max_age)
        if self._lines is None or since < self._lines[0]:
            start = min(since, self.since or since)
            self._lines = (start, self.L.account_lines(self.co, start.isoformat()))
        return [
            statements.Tx.from_dict(json.loads(r["tx"]))
            for r in self._lines[1]
            if r["direction"] == "credit"
            and r["tx"]
            and since.isoformat() <= r["day"] <= today.isoformat()
            and (r["what"] is None or r["what"].startswith("twin "))
        ]

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
    after, max_age = view.policy() if view else policy(L, co)
    due_date = row.get("due_date")
    if iid := row.get("local_id"):  # authored in fiscalm: its own record must still agree with the row
        inv = invoices.get(iid) if invoices is not None else invoicing.invoice(L, iid, company=co)
        if inv is None or inv["status"] != "issued" or inv["data"]["totals"]["total"] != row["total"]:
            raise Refused(CLOSED)
        due_date = due_date or inv["data"].get("due_date")
    if not due_date or today < date.fromisoformat(due_date) + timedelta(days=after):
        raise Refused(CLOSED)
    row = row | {"due_date": due_date}
    since = date.fromisoformat(row["issue_date"]) - timedelta(days=issued.COLLECT_DAYS_BEFORE)
    view = view or StatementView(L, co)
    for tx in view.credits(since, today, max_age):
        if issued.credit_from_client(
            L, co, row["client"] or "", row["client_cui"] or "", tx, accounts=view.client_accounts
        ):
            raise Refused(PARTIAL)
    return row


def tasks(L: Ledger, co: str, today: date, rows: list[dict], *, invoices: dict[str, dict] | None = None) -> list[dict]:
    issue_days = [date.fromisoformat(r["issue_date"]) for r in rows if r.get("issue_date")]
    since = min(issue_days) - timedelta(days=issued.COLLECT_DAYS_BEFORE) if issue_days else None
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
