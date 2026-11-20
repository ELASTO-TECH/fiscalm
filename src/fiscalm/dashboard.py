"""Acasă's numbers (docs/openapi.json, "dashboard"): how the firm is doing, as sums the app only displays.

Money in and out is what moved in the firm's accounts: the statement and bank-feed lines on record (`statement_txs`),
each counted once (a line the other source of the same month brought is a twin, reserved in `settled_tx`, and left
out). Who the firm buys from and sells to comes from its invoices. What falls due comes from the same rules as
De plătit and De încasat (`api.received_owed`, `api.issued_owed`). Amounts are Decimal, summed per currency and never
mixed: lei in `total`, every other currency in `other`. "Today" is passed in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from decimal import Decimal

from fiscalm import companies
from fiscalm.ledger import Ledger

TOP = 5  # the largest suppliers and clients shown
AHEAD_DAYS = 30  # "Următoarele 30 de zile"
MONTHS = 6  # the chart: this month and the five before it
PERIODS = (
    "month",
    "quarter",
    "half_year",
    "trailing_year",
    "year",
)  # current month, trailing 3/6/12 months, year to date


def money(a: Decimal) -> str:
    return f"{a:.2f}"


def _first_of(today: date, back: int) -> date:
    """The first day of the month `back` months before today's."""
    y, m = divmod(today.year * 12 + today.month - 1 - back, 12)
    return date(y, m + 1, 1)


def period_start(period: str, today: date) -> date:
    return {
        "month": _first_of(today, 0),
        "quarter": _first_of(today, 2),
        "half_year": _first_of(today, 5),
        "trailing_year": _first_of(today, 11),
        "year": date(today.year, 1, 1),
    }[period]


class Sums:
    """Amounts per currency, lei apart: the one shape every number below takes ({"total": lei, "other": [...]})."""

    def __init__(self) -> None:
        self.by: dict[str, Decimal] = {}

    def add(self, currency: str | None, amount: Decimal) -> None:
        c = currency or "RON"
        self.by[c] = self.by.get(c, Decimal(0)) + amount

    def ron(self) -> Decimal:
        return self.by.get("RON", Decimal(0))

    def out(self) -> dict:
        return {
            "total": money(self.ron()),
            "other": [{"currency": c, "total": money(v)} for c, v in sorted(self.by.items()) if c != "RON"],
        }


def _minus(a: Sums, b: Sums) -> Sums:
    left = Sums()
    for c in set(a.by) | set(b.by):
        left.add(c, a.by.get(c, Decimal(0)) - b.by.get(c, Decimal(0)))
    return left


def movements(
    L: Ledger, co: str, company: companies.Company, since: date, today: date
) -> list[tuple[str, str, str, Decimal]]:
    """The firm's account lines from `since` to `today`: (day, direction, currency, amount), each payment once. They are
    the lines Tranzacții lists (Ledger.account_lines, read once per state for both), so a sum and its list agree. A line
    counts on one of the firm's configured accounts (its currency is the account's); one that names no account of the
    firm says nothing about its money. `since` is at most a year back, inside what Tranzacții lists."""
    from fiscalm.transactions import cutoff

    currency = {companies.normalize_iban(a.iban): a.currency for a in company.accounts}
    lo, hi = since.isoformat(), today.isoformat()
    return sorted(
        (r["day"], r["direction"], currency[r["iban"]], Decimal(r["amount"]))
        for r in L.account_lines(co, cutoff(today))
        if r["iban"] in currency and lo <= r["day"] <= hi
    )


def _flows(lines: Iterable[tuple[str, str, str, Decimal]], start: str) -> tuple[Sums, Sums]:
    got, spent = Sums(), Sums()
    for day, direction, cur, amount in lines:
        if day >= start:
            (got if direction == "credit" else spent).add(cur, amount)
    return got, spent


def rank(e: dict) -> tuple:
    """How partners are ordered, here and under Toate firmele: by the amount in lei, then by the number of invoices, then
    by name. Amounts in other currencies never decide the order: they can't be compared with lei, nor with each other."""
    return (-Decimal(e["total"]), -e["count"], e["name"], e["company"])


def _top(entries: Iterable[tuple[str | None, str, str, Decimal]], co: str, currency: str) -> list[dict]:
    """The partners with the largest amounts: (partner id, name, currency, amount) summed per partner, ordered by `rank`."""
    by: dict[str, dict] = {}
    for pid, name, cur, amount in entries:
        if cur != currency:
            continue
        if not pid and not name:  # a partner can't be named without either: it is in the lists, not here
            continue
        key = pid or "n:" + name
        e = by.setdefault(key, {"partner": pid, "company": co, "name": name, "sums": Sums(), "count": 0})
        e["sums"].add(cur, amount)
        e["count"] += 1
    out = [
        {"partner": e["partner"], "company": e["company"], "name": e["name"], **e["sums"].out(), "count": e["count"]}
        for e in by.values()
        if e["sums"].by.get(currency, Decimal(0)) > 0
    ]
    return sorted(out, key=lambda e: currency_rank(e, currency))[:TOP]


def currency_rank(entry: dict, currency: str) -> tuple:
    amount = (
        entry["total"]
        if currency == "RON"
        else next((o["total"] for o in entry["other"] if o["currency"] == currency), "0.00")
    )
    return -Decimal(amount), -entry["count"], entry["name"], entry["company"]


def rankings(entries: list, names: dict, co: str, start: str, end: str) -> list[dict]:
    found = [e for e in entries if start <= e[2] <= end and e[0] in names]
    currencies = sorted({e[3] for e in found} | {"RON"}, key=lambda c: (c != "RON", c))
    return [
        {
            "currency": currency,
            **{
                kind: _top(
                    ((pid, names[pid], cur, amount) for pid, way, _, cur, amount, _ in found if way == side),
                    co,
                    currency,
                )
                for kind, side in (("clients", "sales"), ("suppliers", "expenses"))
            },
        }
        for currency in currencies
    ]


def merge_rankings(periods: list[dict]) -> list[dict]:
    currencies = sorted({r["currency"] for p in periods for r in p["rankings"]}, key=lambda c: (c != "RON", c))
    return [
        {
            "currency": currency,
            **{
                side: sorted(
                    (e for p in periods for r in p["rankings"] if r["currency"] == currency for e in r[side]),
                    key=lambda e: currency_rank(e, currency),
                )[:TOP]
                for side in ("clients", "suppliers")
            },
        }
        for currency in currencies
    ]


def upcoming(
    co: str,
    invoices: list[dict],
    rows: list[dict],
    unpaid_payments: list[dict],
    today: date,
    received_owed: Callable[[str, str | None], bool],
    issued_owed: Callable[[dict], bool],
) -> list[dict]:
    """What falls due from today to AHEAD_DAYS days on, by due date: received invoices still to pay, the other payments
    still to make (taxes, the accountant), issued invoices still owed. What is already overdue is in De plătit and
    De încasat, not here."""
    lo, hi = today.isoformat(), (today + timedelta(days=AHEAD_DAYS)).isoformat()
    out = [
        {
            "company": co,
            "date": r["due_date"],
            "side": "out",
            "kind": "invoice",
            "id": r["id"],
            "who": r["supplier"],
            "number": r["number"],
            "amount": r["left"],
            "currency": r["currency"] or "RON",
        }
        for r in invoices
        if r["due_date"] and lo <= r["due_date"] <= hi and received_owed(r["status"], r["payment_state"])
    ]
    out += [
        {
            "company": co,
            "date": p["due"],
            "side": "out",
            "kind": "payment",
            "id": p["item_id"],
            "who": p["payee_name"],
            "number": (p["doc_ref"] or "").strip(),
            "amount": money(Decimal(p["amount"])),
            "currency": "RON",
        }
        for p in unpaid_payments
        if p["kind"] != "efactura" and p["due"] and lo <= p["due"] <= hi
    ]
    out += [
        {
            "company": co,
            "date": r["due_date"],
            "side": "in",
            "kind": "issued",
            "id": r["id"],
            "who": r["client"],
            "number": r["number"],
            "amount": r["owed"],
            "currency": r["currency"] or "RON",
        }
        for r in rows
        if r["due_date"] and lo <= r["due_date"] <= hi and issued_owed(r)
    ]
    return sorted(out, key=lambda x: (x["date"], x["side"], x["who"]))


def company_view(
    L: Ledger,
    co: str,
    company: companies.Company,
    today: date,
    *,
    invoices: list[dict],
    rows: list[dict],
    unpaid_payments: list[dict],
    balances_: list[dict],
    to_explain: int,
    received_owed: Callable,
    issued_owed: Callable,
    entries: list,
    partners: list[dict],
) -> dict:
    """One firm's dashboard. `to_explain` is Tranzacții's count of transactions de explicat (transactions.view)."""
    t = today.isoformat()
    chart = [_first_of(today, back) for back in range(MONTHS - 1, -1, -1)]
    since = _first_of(today, 11)
    lines = movements(L, co, company, since, today)
    periods = {}
    for p in PERIODS:
        start = period_start(p, today).isoformat()
        got, spent = _flows(lines, start)
        ranked = rankings(entries, {p["id"]: p["name"] for p in partners}, co, start, t)
        ron = next(r for r in ranked if r["currency"] == "RON")
        periods[p] = {
            "from": start,
            "to": t,
            "in": got.out(),
            "out": spent.out(),
            "left": _minus(got, spent).out(),
            "suppliers": ron["suppliers"],
            "clients": ron["clients"],
            "rankings": ranked,
        }
    months = []
    for first in chart:
        mo = first.strftime("%Y-%m")
        got, spent = _flows([x for x in lines if x[0][:7] == mo], first.isoformat())
        months.append({"month": mo, "in": got.out(), "out": spent.out()})
    return {
        "periods": periods,
        "months": months,
        "balances": balances_,
        # a statement covers its account up to its closing day, even when its last line is earlier
        "through": max([x[0] for x in lines] + [b["day"] for b in balances_ if b["day"]], default=None),
        "upcoming": upcoming(co, invoices, rows, unpaid_payments, today, received_owed, issued_owed),
        "to_explain": to_explain,
    }


def _add_money(items: Iterable[dict]) -> dict:
    s = Sums()
    for m in items:
        s.add("RON", Decimal(m["total"]))
        for o in m["other"]:
            s.add(o["currency"], Decimal(o["total"]))
    return s.out()


def all_view(parts: list[dict]) -> dict:
    """Toate firmele: every firm's sums added per currency (lei with lei, euro with euro), the largest partners and the
    due dates of every firm together (each row keeps its firm), and every firm's accounts. `through` is the earliest
    firm's, so a total never claims more than every firm's lines cover."""
    ds = [p["dashboard"] for p in parts]
    periods = {}
    for k in PERIODS:
        ps = [d["periods"][k] for d in ds]
        ranked = lambda side, ps=ps: sorted((e for p in ps for e in p[side]), key=rank)[:TOP]  # noqa: E731
        periods[k] = {
            "from": ps[0]["from"],
            "to": ps[0]["to"],
            **{side: _add_money(p[side] for p in ps) for side in ("in", "out", "left")},
            "suppliers": ranked("suppliers"),
            "clients": ranked("clients"),
            "rankings": merge_rankings(ps),
        }
    months = [
        {"month": m["month"], **{side: _add_money(d["months"][i][side] for d in ds) for side in ("in", "out")}}
        for i, m in enumerate(ds[0]["months"])
    ]
    through = [d["through"] for d in ds]
    return {
        "periods": periods,
        "months": months,
        "balances": [b for d in ds for b in d["balances"]],
        "through": None if None in through else min(through),
        "upcoming": sorted((u for d in ds for u in d["upcoming"]), key=lambda x: (x["date"], x["side"], x["who"])),
        "to_explain": sum(d["to_explain"] for d in ds),
    }
