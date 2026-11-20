"""What a firm invoiced and was invoiced over time (turnover), per partner and per month and year, history included.

Every invoice counts at its total on its issue date, in its currency; every credit note counts negative on its own
date, never netted into the invoice it reduces. Tracked invoices and invoices from before fiscalm (`history.rows`,
through `api.state._history`) follow that same rule, so an invoice e-Factura sync later brings, replacing its history
row, leaves every figure as it was. An issued invoice SPV rejected is no invoice. The part from history is kept apart
(`history`) so a screen marks it. Turnover is never a balance: nothing here is owed, matched or paid (non-negotiables
2 and 3); De plătit and De încasat stay on the tracked invoices alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from decimal import Decimal

from fiscalm.dashboard import Sums

Entry = tuple[str | None, str, str, str, Decimal, bool]  # partner, side, issue date, currency, amount, from history
SIDES = ("sales", "expenses")


def _signed(total: str, credit_note: bool) -> Decimal:
    return -abs(Decimal(total)) if credit_note else Decimal(total)


def entries(
    invoices: list[dict], rows: list[dict], history: Iterable[dict], credits: Iterable[dict] = ()
) -> Iterator[Entry]:
    """Every invoice and credit note once: received invoices with the credit notes grouped under them, issued rows
    (a storno of fiscalm's is a row), the issued credit notes that are no row (`credits`, shaped as history rows),
    then the history rows still counted."""
    for r in invoices:
        cur = r["currency"] or "RON"
        yield r["partner"], "expenses", r["issue_date"] or "", cur, _signed(r["total"], r["credit_note"]), False
        for c in r["credits"]:
            yield r["partner"], "expenses", c["date"] or r["issue_date"] or "", cur, Decimal(c["amount"]), False
    for r in rows:
        if not r.get("rejected"):
            amount = _signed(r["total"], bool(r.get("storno_of")))
            yield r["partner"], "sales", r["issue_date"] or "", r["currency"] or "RON", amount, False
    for old, found in ((False, credits), (True, history)):
        for r in found:
            side = "sales" if r["direction"] == "issued" else "expenses"
            yield r["partner"], side, r["issue_date"], r["currency"], _signed(r["total"], r["credit_note"]), old


class Turnover:
    """Sales and expenses, per currency; `history` holds the part that came from history rows."""

    def __init__(self) -> None:
        self.all = {side: Sums() for side in SIDES}
        self.history = {side: Sums() for side in SIDES}

    def add(self, side: str, currency: str, amount: Decimal, from_history: bool) -> None:
        self.all[side].add(currency, amount)
        if from_history:
            self.history[side].add(currency, amount)

    def out(self) -> dict:
        return {
            **{side: self.all[side].out() for side in SIDES},
            "history": {side: self.history[side].out() for side in SIDES},
        }


def by_partner(found: Iterable[Entry]) -> dict[str, dict]:
    """Each partner's turnover, history included."""
    out: dict[str, Turnover] = {}
    for pid, side, _, currency, amount, old in found:
        if pid:
            out.setdefault(pid, Turnover()).add(side, currency, amount, old)
    return {pid: t.out() for pid, t in out.items()}


def by_year(found: Iterable[Entry]) -> list[dict]:
    """Per year, newest first, the year's turnover and each month that has an invoice (newest first)."""
    years: dict[str, Turnover] = {}
    months: dict[str, Turnover] = {}
    for _, side, day, currency, amount, old in found:
        if len(day) < 7:  # an invoice without an issue date has no month to count in
            continue
        years.setdefault(day[:4], Turnover()).add(side, currency, amount, old)
        months.setdefault(day[:7], Turnover()).add(side, currency, amount, old)
    return [
        {
            "year": year,
            "turnover": years[year].out(),
            "months": [
                {"month": m, "turnover": months[m].out()} for m in sorted(months, reverse=True) if m[:4] == year
            ],
        }
        for year in sorted(years, reverse=True)
    ]


def empty() -> dict:
    """The turnover of a partner with no invoice (a saved client, a receipt's vendor)."""
    return Turnover().out()
