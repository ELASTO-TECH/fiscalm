"""Bounded app reads. Summaries always use complete company projections."""

from __future__ import annotations

import re
from datetime import datetime

from fiscalm import companies, home_charts, invoice_lines
from fiscalm.api import (
    browse as browsing,
    dashboard_rows,
    details,
    partner_rows,
    search as searching,
    state as api,
    tasks,
    transaction_choices as choices,
)
from fiscalm.api.responses import bounded, fail, page
from fiscalm.ledger import Ledger

# These are the existing state entities, including their original identities and links.
COLLECTIONS = (
    "invoices",
    "issued",
    "documents",
    "partners",
    "expenses",
    "transactions",
    "upcoming",
    "candidates",
)


def partner_pages(
    L: Ledger, co: str, query: dict, now: datetime | None = None, *, can_manage_companies: bool = False
) -> dict:
    identity, _, _, _ = partner_rows.options(query)
    registry = companies.all(L)
    # Partner IDs already start with the owning company key; company keys cannot contain a dot.
    owner = identity.partition(".")[0] if co == api.ALL else co
    if owner not in registry:
        fail(404, "company_not_found", "Choose the partner's configured company.")
    now = now or api.clock()
    entries = api.company_entries(L, registry, now.date())
    full = api.state(L, owner, now, can_manage_companies=can_manage_companies, company_entries=entries)
    return partner_rows.project(full, metadata(full), query)


def detail(L: Ledger, co: str, query: dict, now: datetime | None = None, *, can_manage_companies: bool = False) -> dict:
    kind, identity, offset, limit = details.options(query)
    full, row = _detail_source(L, co, kind, identity, now, can_manage_companies)
    out = details.context(full, metadata(full), kind, row, offset, limit)
    if kind in ("invoice", "issued"):
        out["lines"] = invoice_lines.sheet(L, full["co"], kind, row)
    return out


def transaction_choices(
    L: Ledger, co: str, query: dict, now: datetime | None = None, *, can_manage_companies: bool = False
) -> dict:
    choices.options(query)
    full, _ = _detail_source(L, co, "transaction", query["id"], now, can_manage_companies)
    return choices.project(full, query)


def _detail_source(
    L: Ledger, co: str, kind: str, identity: str, now: datetime | None, can_manage_companies: bool
) -> tuple[dict, dict]:
    registry = companies.all(L)
    if co not in (*registry, api.ALL):
        fail(404, "company_not_found", "Choose a configured company or ALL.")
    keys = details.owners(L, kind, identity, list(registry)) if co == api.ALL else [co]
    if not keys:
        fail(404, "not_found", "The document does not exist in a configured company.")
    now = now or api.clock()
    entries = api.company_entries(L, registry, now.date())
    # Owner lookup preserves unqualified links and the first offered company without projecting unrelated firms.
    for key in keys:
        full = api.state(L, key, now, can_manage_companies=can_manage_companies, company_entries=entries)
        row = details.find(full, kind, identity)
        if row is not None:
            if kind == "transaction":
                full["prepared_payments"] = api.prepared_payments(L, full["co"])
            return full, row
        del full
    fail(404, "not_found", "The document does not exist in this company.")


def summary(
    L: Ledger,
    co: str,
    now: datetime | None = None,
    *,
    can_manage_companies: bool = False,
    client_id: str = "",
    issued_id: str = "",
) -> dict:
    """Share request-local rows; release each firm's full JSON after extracting its summary."""
    registry = companies.all(L)
    if not registry:
        return api.setup_state(can_manage_companies)
    if co not in (*registry, api.ALL):
        fail(404, "company_not_found", "List companies first.")
    if co == api.ALL and (client_id or issued_id):
        fail(400, "invalid_query", "Navigation context requires one company.")
    now = now or api.clock()
    L = api._StateReads(L, registry)
    entries = api.company_entries(L, registry, now.date())
    collector = tasks.Collector()
    parts = []
    for key in registry if co == api.ALL else (co,):
        full = api.state(L, key, now, can_manage_companies=can_manage_companies, company_entries=entries)
        collector.add(full)
        part = metadata(full, client_id=client_id, issued_id=issued_id)
        parts.append(part)
        del full
    if co != api.ALL:
        return parts[0] | {"can_manage_companies": can_manage_companies}
    return {
        "co": co,
        "live": parts[0]["live"],
        "today": parts[0]["today"],
        "companies": parts[0]["companies"],
        **{k: api._add(parts, k) for k in ("payable", "receivable", "cod_receivable")},
        "dashboard": home_charts.all_view(parts),
        "dashboard_pages": dashboard_rows.merge(parts),
        "parts": parts,
        "can_manage_companies": can_manage_companies,
        "upcoming_count": sum(p["upcoming_count"] for p in parts),
        "bounded": True,
        "task_summary": collector.result(),
        "sales_origins": [dict(id="manual", company=None, label="Manual")]
        + [origin for part in parts for origin in part["sales_origins"] if origin["id"] != "manual"],
    }


def collection(
    L: Ledger, co: str, query: dict, now: datetime | None = None, *, can_manage_companies: bool = False
) -> dict:
    """Page existing projections after rules run, never before calculating money or identities.

    A direct id lookup is independent of the page and filters, so saved sheet links remain reachable.
    Cursors use the existing entity id ordering; clients replace their page after a write.
    """
    name = query.get("collection", "")
    if name not in COLLECTIONS:
        fail(400, "invalid_collection", "Choose a documented state collection.")
    if co not in companies.all(L):
        fail(404, "company_not_found", "Choose one configured company.")
    page([], "id", query)  # Refuse invalid limits before projecting company history.
    if not query.get("id"):
        _filters(name, query)
    full = api.state(L, co, now, can_manage_companies=can_manage_companies, company_entries=[])
    rows, key = _collection_rows(full, name, query)
    # Upcoming can name the same item on either side; keep the original row untouched.
    if name == "upcoming":
        rows = [r | {"cursor_id": f"{r['date']}:{r['side']}:{r['who']}:{api.payment_id(r['id'])}"} for r in rows]
        key = "cursor_id"
    if query.get("id"):
        found = next((r for r in rows if query["id"] in _identifiers(r, key, name)), None)
        if found is None:
            fail(404, "not_found", "The entity does not exist in this company.")
        return {"data": [found], "next_cursor": None, "total": 1}
    partner, month, status = (query.get(k, "") for k in ("partner", "month", "status"))
    rows = [
        r
        for r in rows
        if (not partner or r.get("partner") == partner)
        and (not month or (r.get("month") or r.get("issue_date") or r.get("date") or "")[:7] == month)
        and (not status or r.get("status") == status)
    ]
    return page(rows, key, query) | {"total": len(rows)}


def _collection_rows(full: dict, name: str, query: dict) -> tuple[list[dict], str]:
    if name == "candidates":
        row = next((r for r in full["ambiguous"] if r["tx"] == query.get("tx")), None)
        if row is None:
            fail(404, "not_found", "Choose an ambiguous transaction in this company.")
        # Preserve the engine's date/number ordering, including ties, without putting payment keys in URLs.
        return [
            r | {"cursor_id": f"{i:09d}:{api.payment_id(r['id'])}"} for i, r in enumerate(row["candidates"])
        ], "cursor_id"
    if name == "transactions":
        return full["transactions"]["rows"], "id"
    if name == "upcoming":
        return full["dashboard"]["upcoming"], "id"
    return full[name], "id"


def _identifiers(row: dict, key: str, name: str) -> tuple:
    identifiers = (row[key], row.get("local_id"))
    if name == "candidates":
        identifiers += (row["id"],)
        if row["kind"] == "payment":
            identifiers += (api.payment_id(row["id"]),)
    return identifiers


def _filters(name: str, query: dict) -> None:
    supported = {
        "partner": {"invoices", "issued", "documents", "transactions"},
        "month": {"invoices", "issued", "documents", "transactions", "upcoming"},
        "status": {"invoices", "issued"},
        "tx": {"candidates"},
    }
    for key, collections in supported.items():
        if query.get(key) and name not in collections:
            fail(400, "invalid_filter", "This collection does not support that filter.")
    if query.get("month") and not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", query["month"]):
        fail(400, "invalid_month", "Use a month in YYYY-MM format.")


def metadata(full: dict, *, client_id: str = "", issued_id: str = "") -> dict:
    """Complete aggregates and bounded highlights, with optional single-entity mutation navigation context."""
    part = {k: [] if isinstance(v, list) else v for k, v in full.items() if k != "prepared_payments"}
    part.update(
        {
            k: full[k]
            for k in (
                "companies",
                "sales_origins",
                "news",
                "bank_connections",
                "rules",
                "recurrences",
                "firm_cards",
                "files",
                "payment_file_defaults",
            )
        }
    )
    part["record_counts"] = {k: len(v) for k, v in full.items() if isinstance(v, list)}
    part["record_counts"]["remembered"] = len(full["transactions"]["remembered"])
    part["invoice_counts"] = {
        status: sum(r["status"] == status for r in full["invoices"]) for status in ("open", "marked")
    }
    part["transactions"] = full["transactions"] | {"rows": [], "remembered": []}
    part["dashboard"] = full["dashboard"] | {"upcoming": []}
    part["upcoming_count"] = len(full["dashboard"]["upcoming"])
    part["cod"] = full["cod"] | {"rows": [], "tasks": []}
    part["reports"] = [_report_metadata(r, full) for r in full["reports"]]
    # Configuration needs the selected identity even when its partner is beyond the first list page.
    cui = full["accountant"]["cui"]
    accountant = next((p for p in full["partners"] if p["cui"] == cui), None) if cui else None
    part["accountant"] = full["accountant"] | {
        "partner": {k: accountant[k] for k in ("id", "name", "cui")} if accountant else None
    }
    # A mutation can move a client's canonical partner ID or issue a local invoice alias. Keep only that target.
    partner = next((p for p in full["partners"] if p["client_id"] == client_id), None) if client_id else None
    part["partners"] = [bounded(partner | {"held": [], "report_formats": []})] if partner else []
    issued = details.find(full, "issued", issued_id) if issued_id else None
    part["issued"] = [bounded(issued)] if issued else []
    return part | {"bounded": True}


def _report_metadata(report: dict, full: dict) -> dict:
    received, documents = set(report["received"]), set(report["documents"])
    bank = report["bank"]
    result = {k: [] if isinstance(v, list) else v for k, v in report.items()}
    result["bank"] = bank | {"card": [], "advances": []} if bank else None
    result["counts"] = {k: len(v) for k, v in report.items() if isinstance(v, list)} | {
        "purchases": sum(e["invoice"] in received or e["document"] in documents for e in full["expenses"]),
        "card": len(bank["card"]) if bank else 0,
        "matched": sum(bool(r["doc"]) for r in bank["card"]) if bank else 0,
        "advances": len(bank["advances"]) if bank else 0,
        "ambiguous": sum(r["month"] == report["month"] for r in full["ambiguous"]),
        "statements": sum(r["kind"] == "statement" for r in report["docs"]),
        "open": sum(1 for _ in browsing.report_tasks(full, report)),
    }
    return result


def task_page(
    L: Ledger, co: str, query: dict, now: datetime | None = None, *, can_manage_companies: bool = False
) -> dict:
    page([], "id", query)
    try:
        offset, limit = int(query.get("cursor") or "0"), int(query.get("limit", "50"))
    except ValueError:
        fail(400, "invalid_cursor", "Use a nonnegative integer task cursor.")
    if not 0 <= offset <= 10_000_000 or query.get("group", "") != "":
        fail(400, "invalid_cursor", "Use a valid task cursor and group.")
    registry = companies.all(L)
    if co not in (*registry, api.ALL):
        fail(404, "company_not_found", "Choose a configured company or ALL.")
    collector = tasks.Collector(offset, limit, query.get("group", ""))
    now = now or api.clock()
    for key in registry if co == api.ALL else (co,):
        full = api.state(L, key, now, can_manage_companies=can_manage_companies, company_entries=[])
        collector.add(full)
        del full
    return collector.result()


def search(L: Ledger, co: str, query: dict, now: datetime | None = None) -> dict:
    registry = companies.all(L)
    if co not in (*registry, api.ALL):
        fail(404, "company_not_found", "Choose a configured company or ALL.")
    collector = searching.Collector(query.get("q", ""))
    now = now or api.clock()
    if collector.ready:
        for key in registry if co == api.ALL else (co,):
            collector.add(api.entity_rows(L, key, now))
    return collector.result()


def browse(L: Ledger, co: str, query: dict, now: datetime | None = None, *, can_manage_companies: bool = False) -> dict:
    collector = browsing.Collector(query)
    registry = companies.all(L)
    if co not in (*registry, api.ALL):
        fail(404, "company_not_found", "Choose a configured company or ALL.")
    now = now or api.clock()
    for key in registry if co == api.ALL else (co,):
        if collector.screen in (*browsing.REPORT_SCREENS, "tranzactii"):
            part = api.state(L, key, now, can_manage_companies=can_manage_companies, company_entries=[])
        else:
            part = api.entity_rows(
                L, key, now, with_sales=collector.screen == "vanzari", can_manage_companies=can_manage_companies
            )
        collector.add(part)
        del part
    return collector.result()


def report_pages(
    L: Ledger, co: str, query: dict, now: datetime | None = None, *, can_manage_companies: bool = False
) -> dict:
    """Build all seven bounded initial section pages from one company projection, released on return."""
    if set(query) - {"month", "limit"}:
        fail(400, "invalid_query", "Use only month and the per-section limit for initial report pages.")
    page([], "id", query)
    collectors = [browsing.Collector(query | {"screen": screen}) for screen in browsing.REPORT_SCREENS]
    if co not in companies.all(L):
        fail(404, "company_not_found", "Choose one configured company.")
    full = api.state(L, co, now, can_manage_companies=can_manage_companies, company_entries=[])
    result = []
    for collector in collectors:
        collector.add(full)
        result.append({"screen": collector.screen, "page": collector.result()})
    return {"data": result, "total": len(result), "next_cursor": None}


def dashboard_page(
    L: Ledger, co: str, query: dict, now: datetime | None = None, *, can_manage_companies: bool = False
) -> dict:
    registry = companies.all(L)
    if co not in (*registry, api.ALL):
        fail(404, "company_not_found", "Choose a configured company or ALL.")
    now = now or api.clock()
    return dashboard_rows.project(
        list(registry) if co == api.ALL else [co],
        lambda key: api.state(L, key, now, can_manage_companies=can_manage_companies, company_entries=[]),
        query,
    )
