"""Acasă charts from statement and invoice evidence in the bootstrap's request-scoped reads."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from calendar import monthrange
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fiscalm import companies, dashboard
from fiscalm.efactura import inbox
from fiscalm.ledger import Ledger, finance_home
from fiscalm.products import stats as product_stats
from fiscalm.storage import read_file
from fiscalm.transactions import cutoff


def _parse_statement(path: Path, root: Path, co: str, parse: Callable):
    """Linux: parse the checked descriptor, never a substituted path."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or path.suffix == ".csv":
        raise ValueError("statement outside company storage or partial CSV")
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = Path(f"/proc/self/fd/{stream.fileno()}")
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or not opened.resolve().is_relative_to(root):
            raise ValueError("statement outside company storage")
        return parse(opened, co)


def statement_evidence(L: Ledger, co: str, company: companies.Company, today: date, parse: Callable) -> tuple:
    """Read each stored statement once; no balance interpolation or CSV completeness claims."""
    want = {companies.normalize_iban(a.iban): a for a in company.accounts}
    coverage = {iban: [] for iban in want}
    closings: dict = {iban: {} for iban in want}
    since = dashboard._first_of(today, 11)
    root = (finance_home() / co).resolve()
    for doc in L.documents(company=co, kind="statement"):
        if doc["month"] < since.strftime("%Y-%m") or doc["month"] > today.strftime("%Y-%m"):
            continue
        try:
            st = _parse_statement(Path(doc["path"]), root, co, parse)
        except Exception:
            continue  # unavailable evidence stays unknown
        for account in st.accounts:
            iban = companies.normalize_iban(account.iban)
            if iban not in want or account.currency != want[iban].currency or not account.trustworthy:
                continue
            if not st.partial and st.period_start and st.period_end:
                coverage[iban].append((st.period_start, st.period_end))
            if account.closing is not None and account.closing_day and since <= account.closing_day <= today:
                closings[iban].setdefault(account.closing_day, set()).add(account.closing)
    history = [
        {
            "company": co,
            "iban_last4": iban[-4:],
            "currency": a.currency,
            "bank": a.bank,
            "points": [
                {"day": day.isoformat(), "balance": dashboard.money(next(iter(values)))}
                for day, values in sorted(closings[iban].items())
                if len(values) == 1
            ],
        }
        for iban, a in want.items()
    ]
    balances, total = _current_balances(co, want, closings)
    return coverage, history, total, balances


def _current_balances(co: str, accounts: dict, closings: dict) -> tuple[list[dict], dict]:
    """The tile sums exactly the same proved balances that the account rows display."""
    balances = []
    for iban, account in accounts.items():
        if account.retired:
            continue
        day = max(closings[iban]) if closings[iban] else None
        values = closings[iban].get(day, set())
        balances.append(
            {
                "company": co,
                "iban_last4": iban[-4:],
                "currency": account.currency,
                "bank": account.bank,
                "balance": dashboard.money(next(iter(values))) if len(values) == 1 else None,
                "day": day.isoformat() if day and len(values) == 1 else None,
            }
        )
    total = dashboard.Sums()
    for balance in balances:
        if balance["balance"] is not None:
            total.add(balance["currency"], Decimal(balance["balance"]))
    days = [balance["day"] for balance in balances if balance["day"]]
    return balances, {
        **total.out(),
        "partial": len(days) < len(balances) or not balances,
        "day": min(days, default=None),
    }


def _covered(ranges: list, first: date, last: date) -> bool:
    through = first - timedelta(days=1)
    for lo, hi in sorted(ranges):
        if lo > through + timedelta(days=1):
            break
        through = max(through, hi)
    return through >= last


def cash_month(month: str, observed: dict, coverage: dict, today: date) -> dict:
    first = date.fromisoformat(month + "-01")
    last = first.replace(day=monthrange(first.year, first.month)[1])
    complete = bool(coverage) and last <= today and all(_covered(r, first, last) for r in coverage.values())
    present = any(lo <= last and hi >= first for ranges in coverage.values() for lo, hi in ranges)
    has_lines = any(Decimal(s["total"]) or any(Decimal(o["total"]) for o in s["other"]) for s in observed.values())
    status = "complete" if complete else "partial" if present or has_lines else "none"
    return {
        "month": month,
        "in_progress": first <= today <= last,
        "cash_status": status,
        "observed": observed,
        **{key: value if complete else None for key, value in observed.items()},
    }


def cash(L: Ledger, co: str, company: companies.Company, today: date, parse: Callable) -> tuple[dict, list[dict]]:
    coverage, history, total, balances = statement_evidence(L, co, company, today, parse)
    lines = dashboard.movements(L, co, company, dashboard._first_of(today, 11), today)
    months = []
    for back in range(11, -1, -1):
        first = dashboard._first_of(today, back)
        month = first.strftime("%Y-%m")
        got, spent = dashboard._flows([r for r in lines if r[0][:7] == month], first.isoformat())
        observed = {"in": got.out(), "out": spent.out(), "net": dashboard._minus(got, spent).out()}
        required = {
            companies.normalize_iban(a.iban) for a in company.accounts if not a.retired or first.isoformat() < a.retired
        }
        months.append(cash_month(month, observed, {i: r for i, r in coverage.items() if i in required}, today))
    return {"months": months, "balance_history": history, "balance_total": total}, balances


def merge(parts: list[dict]) -> dict:
    """ALL is known only when every firm's evidence is known."""
    months = []
    for i, first in enumerate(parts[0]["months"]):
        rows = [p["months"][i] for p in parts]
        complete = all(r["cash_status"] == "complete" for r in rows)
        observed = {k: dashboard._add_money(r["observed"][k] for r in rows) for k in ("in", "out", "net")}
        months.append(
            {
                "month": first["month"],
                "in_progress": first["in_progress"],
                "cash_status": "complete"
                if complete
                else "none"
                if all(r["cash_status"] == "none" for r in rows)
                else "partial",
                "observed": observed,
                **{k: v if complete else None for k, v in observed.items()},
            }
        )
    for i, month in enumerate(months):
        month.update(
            {
                k: dashboard._add_money(p["months"][i][k] for p in parts)
                for k in ("invoiced", "collected", "received", "paid")
            }
        )
    vat = []
    for i, first in enumerate(parts[0]["vat"]):
        rows = [p["vat"][i] for p in parts]
        vat.append(
            {
                **first,
                **{k: dashboard._add_money(r[k] for r in rows) for k in ("collected", "deductible", "net")},
                "partial": any(r["partial"] for r in rows),
                "missing_count": sum(r["missing_count"] for r in rows),
            }
        )
    balances = [p["balance_total"] for p in parts]
    days = [b["day"] for b in balances if b["day"]]
    return {
        "months": months,
        "aging": {side: _merge_aging([p["aging"][side] for p in parts]) for side in ("receivables", "payables")},
        "vat": vat,
        "product_sales": [s for p in parts for s in p["product_sales"]],
        "balance_history": [s for p in parts for s in p["balance_history"]],
        "balance_total": {
            **dashboard._add_money(balances),
            "partial": any(b["partial"] for b in balances),
            "day": min(days, default=None),
        },
    }


def period_statuses(dashboard_: dict) -> dict:
    """Period KPIs have the same evidence label as the months they cover."""
    for period in dashboard_["periods"].values():
        period["product_sales"] = [
            product_stats.period(s, period["from"], period["to"]) for s in dashboard_["charts"]["product_sales"]
        ]
        statuses = [m["cash_status"] for m in dashboard_["charts"]["months"] if m["month"] >= period["from"][:7]]
        period["cash_status"] = (
            "complete"
            if all(s == "complete" for s in statuses)
            else "none"
            if all(s == "none" for s in statuses)
            else "partial"
        )
    return dashboard_


def all_view(parts: list[dict]) -> dict:
    return period_statuses(
        {
            **dashboard.all_view(parts),
            "charts": merge([p["dashboard"]["charts"] for p in parts]),
        }
    )


AGING = ("not_due", "1_30", "31_60", "over_60", "unknown_due")


def aging(rows: list[dict], side: str, today: date, owed: Callable[[dict], bool]) -> dict:
    sums = {key: dashboard.Sums() for key in (*AGING, "confirming")}
    counts = dict.fromkeys(sums, 0)
    for row in rows:
        status = row["status"]
        confirming = status == "marked" or (
            side == "payables" and (status == "paid" and not row["proved"] or status == "linked" and not owed(row))
        )
        amount = _net(row) - Decimal(row.get("covered", "0.00")) if confirming else Decimal(row["left"])
        if amount <= 0 or row.get("rejected"):
            continue
        if not confirming and not owed(row):
            continue
        due = row["due_date"]
        late = (today - date.fromisoformat(due)).days if due else None
        key = (
            "unknown_due"
            if late is None
            else "not_due"
            if late <= 0
            else "1_30"
            if late <= 30
            else "31_60"
            if late <= 60
            else "over_60"
        )
        if confirming:
            key = "confirming"
        sums[key].add(row["currency"], amount)
        counts[key] += 1
    return {
        "buckets": [{"bucket": k, **sums[k].out(), "count": counts[k]} for k in AGING],
        "confirming": {**sums["confirming"].out(), "count": counts["confirming"]},
    }


def invoice_months(charts: dict, entries: list, today: date) -> None:
    """Net credit notes on their own issue dates, exactly as turnover does."""
    monthly = {m["month"]: {k: dashboard.Sums() for k in ("invoiced", "received")} for m in charts["months"]}
    for _, side, day, currency, amount, _ in entries:
        if day[:7] in monthly and day <= today.isoformat():
            monthly[day[:7]]["invoiced" if side == "sales" else "received"].add(currency, amount)
    for month in charts["months"]:
        month.update({k: s.out() for k, s in monthly[month["month"]].items()})


def _bank_amount(tx: dict, row: dict) -> Decimal | None:
    if tx["currency"] == row["currency"]:
        return Decimal(tx["amount"])
    orig = tx.get("orig") or {"currency": tx.get("orig_currency"), "amount": tx.get("orig_amount")}
    return Decimal(orig["amount"]) if orig["currency"] == row["currency"] and bool(orig["amount"]) else None


def _allocations(tx: dict, found: dict, reports: dict) -> list:
    e = tx["explanation"]
    parts = e.get("link", {}).get("parts", [])
    if parts:
        return [(p["id"], Decimal(p["amount"])) for p in parts]
    if e.get("settlement") in reports:
        return [(p["invoice"], Decimal(p["gross"])) for p in reports[e["settlement"]]["lines"]]
    ids = e.get("issued", []) if e["kind"] == "collection" else e.get("invoices", [])
    if ids and all(i in found for i in ids):  # one credit, one invoice or one exact set: the bank amount once
        amount = _bank_amount(tx, found[ids[0]])
        nets = [_net(found[i]) for i in ids]
        if amount is None:
            return []
        return (
            list(zip(ids, nets, strict=True))
            if sum(nets) == amount
            else [(ids[0], amount), *((i, Decimal(0)) for i in ids[1:])]
        )
    return []


def _proof_amount(row: dict, side: str, evidence: dict, proofs: dict, collections: dict, batches: dict) -> tuple:
    if side == "collected":
        if row.get("how") != "statement":
            return "", None
        if row["id"] in batches:
            return batches[row["id"]]
        key = collections.get(row["id"])
    else:
        if not row.get("proved"):
            return "", None
        key = proofs.get("efactura " + row["id"])
    if key in evidence:
        tx = evidence[key]
        return tx["day"], _bank_amount(tx, row)
    if side == "paid" and (key or "").startswith(("bon:", "ef-proof:")) and row.get("paid_at"):
        day = row["paid_at"][:10]
        amount = Decimal(row["total"]) + sum(
            (Decimal(c["amount"]) for c in row["credits"] if c["date"] and c["date"] <= day), Decimal(0)
        )
        return day, max(Decimal(0), amount)
    return "", None


def settlements(
    charts: dict,
    txs: list[dict],
    rows: list[dict],
    invoices: list[dict],
    evidence: dict,
    proofs: dict,
    collections: dict,
    reports: list[dict],
    today: date,
) -> None:
    """Bank dates and amounts survive later stornos; report gross differs from its net bank credit."""
    monthly = {m["month"]: {k: dashboard.Sums() for k in ("collected", "paid")} for m in charts["months"]}
    issued, received = {r["id"]: r for r in rows}, {r["id"]: r for r in invoices}
    reports = {r["id"]: r for r in reports if r["status"] == "collected"}
    batches = {p["invoice"]: (r["credit"]["date"], Decimal(p["gross"])) for r in reports.values() for p in r["lines"]}
    seen = {"collected": set(), "paid": set()}
    for tx in txs:
        e = tx["explanation"]
        if not e or e["kind"] not in ("collection", "payment", "receipt"):
            continue
        side, found = ("collected", issued) if e["kind"] == "collection" else ("paid", received)
        day = tx["date"]
        for ident, amount in _allocations(tx, found, reports):
            if ident in found and day[:7] in monthly and day <= today.isoformat():
                monthly[day[:7]][side].add(found[ident]["currency"], amount)
                seen[side].add(ident)
    for side, found in (("collected", rows), ("paid", invoices)):
        for row in found:
            if row["id"] in seen[side]:
                continue
            day, amount = _proof_amount(row, side, evidence, proofs, collections, batches)
            if amount is not None and day[:7] in monthly and day <= today.isoformat():
                monthly[day[:7]][side].add(row["currency"], amount)
    for month in charts["months"]:
        month.update({k: v.out() for k, v in monthly[month["month"]].items()})


def _net(row: dict) -> Decimal:
    amount = Decimal(row["total"]) - Decimal(row.get("credited", "0.00"))
    amount += sum((Decimal(c["amount"]) for c in row.get("credits", [])), Decimal(0))
    return max(Decimal(0), amount)


def company_view(
    L: Ledger,
    co: str,
    company: companies.Company,
    today: date,
    parse: Callable,
    entries: list,
    invoices: list[dict],
    rows: list[dict],
    txs: list[dict],
    reports: list[dict],
    received_owed: Callable[[str, str | None], bool],
    issued_owed: Callable[[dict], bool],
    records: list[dict],
    history: list[dict],
    sent_links: dict[str, str],
) -> tuple[dict, list[dict]]:
    charts, balances = cash(L, co, company, today, parse)
    charts["product_sales"] = [product_stats.sales(L, co, today.strftime("%Y-%m"), 12, today=today)]
    invoice_months(charts, entries, today)
    evidence = {
        r["tx_key"]: json.loads(r["tx"]) | {"day": r["day"]}
        for r in L.account_lines(co, cutoff(today))
        if r["tx"] is not None
    }
    collections = {r["message_id"]: r["collected_tx"] for r in L.issued_rows(company=co)}
    settlements(charts, txs, rows, invoices, evidence, L.paid_proofs(co), collections, reports, today)
    charts["aging"] = {
        "receivables": aging(rows, "receivables", today, issued_owed),
        "payables": aging(invoices, "payables", today, lambda r: received_owed(r["status"], r["payment_state"])),
    }
    charts["vat"] = vat_estimate(L, co, today, rows, records, history, sent_links)
    return charts, balances


def _merge_aging(found: list[dict]) -> dict:
    def total(rows):
        return {**dashboard._add_money(rows), "count": sum(r["count"] for r in rows)}

    return {
        "buckets": [{"bucket": key, **total([p["buckets"][i] for p in found])} for i, key in enumerate(AGING)],
        "confirming": total([p["confirming"] for p in found]),
    }


def _tax(row: dict, root) -> Decimal | None:
    stored = read_file(row["raw_path"], [root], inbox.MAX_XML)
    if not stored:
        return None
    try:
        data = inbox.xml_from_zip(stored[1])
        if row["xml_sha"] != hashlib.sha256(data).hexdigest():
            return None
        xml = inbox.safe_xml(data)
        amounts = [n.text for n in xml.findall("{*}TaxTotal/{*}TaxAmount") if n.get("currencyID") == row["currency"]]
        if len(amounts) != 1:
            return None
        amount = inbox._amount(amounts[0])
        return amount if amount is not None and amount.is_finite() else None
    except (inbox.UblError, InvalidOperation, TypeError):
        return None


def _sources(L: Ledger, co: str, rows: list[dict], records: list[dict], history: list[dict], sent_links: dict) -> list:
    local = {r["id"]: r for r in records}
    visible = {r["id"]: r for r in rows}
    used = set()
    found = []
    for side, raw in (("collected", L.issued_rows(company=co)), ("deductible", L.efactura_rows(company=co))):
        for record in raw:
            if record["parse_error"] or record["status"] in ("duplicate", "skipped"):
                continue
            shown = visible.get(record["message_id"], {}) if side == "collected" else {}
            local_id = sent_links.get(record["message_id"]) if side == "collected" else None
            if shown.get("rejected") or visible.get(local_id, {}).get("rejected"):
                continue
            authored = local.get(local_id)
            if authored:
                if local_id in used:
                    continue
                used.add(local_id)
            found.append((side, dict(record), authored))
    for row in rows:
        local_id = row.get("local_id")
        if local_id in local and local_id not in used and not row.get("rejected"):
            data = local[local_id]["data"]
            found.append(
                (
                    "collected",
                    {
                        "issue_date": data["issue_date"],
                        "currency": data["currency"],
                        "doc_type": "credit_note" if row.get("storno_of") else "invoice",
                    },
                    local[local_id],
                )
            )
            used.add(local_id)
    for row in history:
        found.append(
            (
                "collected" if row["direction"] == "issued" else "deductible",
                row | {"doc_type": "credit_note" if row["credit_note"] else "invoice"},
                None,
            )
        )
    return found


def vat_estimate(
    L: Ledger, co: str, today: date, rows: list[dict], records: list[dict], history: list[dict], sent_links: dict
) -> list[dict]:
    months = [dashboard._first_of(today, back).strftime("%Y-%m") for back in (1, 0)]
    values = {m: {"collected": dashboard.Sums(), "deductible": dashboard.Sums(), "missing_count": 0} for m in months}
    root = (finance_home() / "efactura" / co).resolve()
    for side, row, authored in _sources(L, co, rows, records, history, sent_links):
        month = (row["issue_date"] or "")[:7]
        if month not in values or (row["issue_date"] or "") > today.isoformat():
            continue
        if authored:
            value = authored["data"].get("totals", {}).get("vat")
            amount = inbox._amount(value) if isinstance(value, str) else None
        else:
            amount = _tax(row, root) if row.get("raw_path") else None
        if amount is None:
            values[month]["missing_count"] += 1
        else:
            values[month][side].add(row["currency"], -abs(amount) if row["doc_type"] == "credit_note" else amount)
    return [
        {
            "month": month,
            "estimate": True,
            "basis": "recorded_invoices",
            "in_progress": month == months[-1],
            "partial": bool(values[month]["missing_count"]),
            "missing_count": values[month]["missing_count"],
            "collected": values[month]["collected"].out(),
            "deductible": values[month]["deductible"].out(),
            "net": dashboard._minus(values[month]["collected"], values[month]["deductible"]).out(),
        }
        for month in months
    ]
