"""Read-only, evidence-checked reminder text for a human to copy and send."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from datetime import date, timedelta
from decimal import Decimal
from functools import cached_property

from fiscalm import companies, history_invoices, storage
from fiscalm.bank import coverage, statements
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
STATEMENTS = "Un credit din extras nu poate fi citit. Verifică extrasul înainte să pregătești textul de reamintire."
# A person sends the text, so statements behind by more than STATEMENT_MAX_AGE_DAYS only warn on the card.
STALE = "Extrasele de cont acoperă încasările doar până pe {date}. Verifică în bancă dacă clientul a plătit înainte să trimiți textul."
NO_STATEMENTS = "Extrasele de cont nu acoperă întreaga perioadă de la emiterea facturii. Verifică în bancă dacă clientul a plătit înainte să trimiți textul."
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
# Statements this recent count as current: an older one could miss the payment that makes the text wrong.
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
    """Request-local snapshot of what the ledger holds of the firm's bank: its credit lines and the days each current
    account is covered (bank.coverage: statements, CSVs and the feed), each read once."""

    def __init__(self, L: Ledger, co: str, since: date | None = None):
        """`since`: the earliest day any caller will ask about, so the ledger is read once."""
        self.L, self.co, self.since = L, co, since
        self._policy: tuple[int, int] | None = None
        self._lines: tuple[date, list, set] | None = None  # (since, credit lines, explained keys)
        self._gaps: tuple[date, date, list] | None = None  # (since, today, every account's uncovered days)

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

    def credits(self, since: date, today: date) -> list[statements.Tx]:
        """Every representation, from `since` to `today`, of a credit on record that may still be this firm's client
        paying: statements, CSVs and the bank feed alike, each line in every reading kept of it, since copies of one
        payment may name its payer differently.
        A payment is left out only when proved to be something else (`_explained`). A credit on record without its
        details refuses: it could be the client's."""
        self.load(since)
        explained = self._lines[2]
        out = []
        window = since.isoformat(), today.isoformat()
        for r in self._lines[1]:
            if r["tx_key"] in explained:
                continue
            others = [statements.Tx.from_dict(json.loads(x)) for x in json.loads(r["readings"] or "[]")]
            days = [r["day"], *(t.date.isoformat() for t in others)]  # a later read may correct the booking date
            if not any(window[0] <= d <= window[1] for d in days):
                continue
            if not r["tx"]:
                raise Refused(STATEMENTS)
            out += [statements.Tx.from_dict(json.loads(r["tx"])), *others]  # every reading may name the payer
        return out

    def load(self, since: date) -> None:
        """Read the firm's credit lines from `since` (or the request's earlier `self.since`) once."""
        if self._lines is None or since < self._lines[0]:
            start = min(since, self.since or since)
            lo = start - timedelta(days=2 * BOUNDARY_DAYS)  # an explained copy just before, and the twins it could be
            lines = self.L.credit_lines(self.co, lo.isoformat())
            self._lines = (start, lines, _explained(lines))

    def covered_until(self, since: date, today: date) -> date | None:
        """The last day before which every current account is covered without a gap from `since`; None when they are
        covered through `today`. A firm without a current account has nothing covered."""
        if self._gaps is None or since < self._gaps[0] or today != self._gaps[1]:
            start = min(since, self.since or since)
            accounts = companies.get(self.co, L=self.L).current_accounts
            holes = [g for a in accounts for g in coverage.gaps(self.L, self.co, a.iban, start, today)]
            self._gaps = (start, today, holes if accounts else [(start, today)])
        starts = [max(first, since) for first, last in self._gaps[2] if last >= since]
        return min(starts) - timedelta(days=1) if starts else None


def _explained(rows: list) -> set[str]:
    """The keys of credit lines proved to be something other than an unexplained payment: an original line reserved
    in `settled_tx` as collecting or explaining an item; a duplicate line whose original is; a twin (the other
    source's copy of a payment) when every original line it could be a copy of is explained and could be the copy
    of no other twin. Two twins wanting one explained payment may be two payers: neither is excused, since only one
    of them is that payment. A conflict is never explained."""
    what = {r["tx_key"]: r["what"] or "" for r in rows}
    copy_of = lambda w: w.startswith(("twin ", "duplicate:"))  # noqa: E731
    originals = [r for r in rows if not copy_of(what[r["tx_key"]])]
    done = {r["tx_key"] for r in originals if what[r["tx_key"]] and not what[r["tx_key"]].startswith(UNEXPLAINED)}
    excused = {k for k, w in what.items() if w.startswith("duplicate:") and w.removeprefix("duplicate:") in done}
    twins = [r for r in rows if what[r["tx_key"]].startswith("twin ")]
    by_amount: dict[str, list] = {}  # a copy has the same amount: compare only those
    for e in originals:
        by_amount.setdefault(e["amount"], []).append(e)
    copies = {t["tx_key"]: [e["tx_key"] for e in by_amount.get(t["amount"], ()) if _copy(t, e)] for t in twins}
    wanted: dict[str, int] = {}
    for keys in copies.values():
        for k in keys:
            wanted[k] = wanted.get(k, 0) + 1
    excused |= {t for t, keys in copies.items() if keys and all(k in done and wanted[k] == 1 for k in keys)}
    return done | excused


def _copy(twin, line) -> bool:
    """`line` could be the other source's copy of `twin`: brought by another source (two lines of one source are two
    payments), the same known amount and currency, the same account (or one unknown), days apart at most
    BOUNDARY_DAYS by any of their readings' dates (Ledger.account_lines pairs by the stored day alone). Neither may
    have a reading with other money, so a date from one reading never combines with an amount from another."""
    apart = min(abs((a - b).days) for a in _days(twin) for b in _days(line))
    same_account = not twin["iban"] or not line["iban"] or twin["iban"] == line["iban"]
    same_money = twin["amount"] is not None and twin["amount"] == line["amount"] and _currency(twin) == _currency(line)
    return (
        _one_money(twin)  # a line read with other money on another read is no one's copy: in doubt, never paired
        and _one_money(line)
        and (twin["source"] or "file") != (line["source"] or "file")
        and same_money
        and same_account
        and apart <= BOUNDARY_DAYS
    )


def _one_money(line) -> bool:
    """Every reading of the line carries the same amount, currency and account as the stored one."""
    stored = json.loads(line["tx"]) if line["tx"] else {}
    money = lambda d: (d.get("amount"), d.get("currency"), d.get("iban"))  # noqa: E731
    return all(money(json.loads(x)) == money(stored) for x in json.loads(line["readings"] or "[]"))


def _days(line) -> set[date]:
    """Every booking day a line was read with: a bank may correct the date on a later read."""
    others = (json.loads(x)["date"] for x in json.loads(line["readings"] or "[]"))
    return {date.fromisoformat(line["day"]), *map(date.fromisoformat, others)}


def _currency(line) -> str | None:
    return json.loads(line["tx"]).get("currency") if line["tx"] else None


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
    # A digits-only id is a Romanian CUI: from a supplier in another country it is someone else's number.
    foreign = inv.supplier_tax_id.isdigit() and (inv.supplier_address or {}).get("country", "") not in ("", "RO")
    return (
        sha == stored["xml_sha"] and not foreign and history_invoices.direction(companies.get(co, L=L), inv) == "issued"
    )


def _lookback(issue_date: str, due_date: str) -> date:
    """The first day a credit paying the invoice may sit on: COLLECT_DAYS_BEFORE before its issue date, or before
    its due date when an SPV invoice's comes first."""
    return min(_shift(d, -issued.COLLECT_DAYS_BEFORE) for d in (issue_date, due_date))


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
    name, cui = row["client"] or "", row["client_cui"] or ""
    if not (firm_id(cui) and len(cui) >= 6) and not efactura.same_firm(name, name):
        raise Refused(CLOSED)  # no buyer a payment could be told from by the payer matcher's rules: no text
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
    since = _lookback(row["issue_date"], due_date)
    for tx in view.credits(since, today):
        if issued.credit_from_client(L, co, name, cui, tx, accounts=view.client_accounts):
            raise Refused(PARTIAL)
    until = view.covered_until(since, today)
    stale = until is not None and (until < since or until < today - timedelta(days=max_age))
    warning = (NO_STATEMENTS if until < since else STALE.format(date=ro(until))) if stale else None
    return row | {"warning": warning}


def tasks(L: Ledger, co: str, today: date, rows: list[dict], *, invoices: dict[str, dict] | None = None) -> list[dict]:
    starts = []
    for row in rows:
        if row["status"] == "open":  # only these may be reminded: the window need not reach further back
            with suppress(Refused):
                starts.append(_lookback(row["issue_date"], row.get("due_date") or row["issue_date"]))
    since = min(starts, default=None)
    out, view = [], StatementView(L, co, since)
    if since:  # one read per firm with an open invoice, however many of them reach the credit check
        view.load(since)
        view.covered_until(since, today)
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
                warning=row["warning"],
                body=BODY.format(**fields),
            )
        )
    return out
