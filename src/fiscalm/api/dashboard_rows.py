"""Bounded dashboard panels with full money aggregates and the existing cross-company order."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from decimal import Decimal
from heapq import nsmallest
from itertools import islice

from fiscalm.api.responses import fail, page
from fiscalm.dashboard import money

COLLECTIONS = ("upcoming", "held_money")


def _rows(part: dict, collection: str) -> Iterator[dict]:
    if collection == "upcoming":
        yield from part["dashboard"]["upcoming"]
        return
    for partner in part["partners"]:
        amounts: dict[str, Decimal] = {}
        for held in partner.get("held", []):
            currency = held["currency"]
            amounts[currency] = amounts.get(currency, Decimal("0")) + Decimal(held["amount"])
        for currency, amount in amounts.items():
            yield {
                "id": f"{partner['id']}:{currency}",
                "company": part["co"],
                "partner": partner["id"],
                "name": partner["name"],
                "currency": currency,
                "amount": money(amount),
            }


def _key(row: dict, firm: int, ordinal: int, collection: str) -> tuple:
    # Source position is the same stable tie-breaker as concatenating full states in company order.
    return (row["date"], row["side"], row["who"], firm, ordinal) if collection == "upcoming" else (firm, ordinal)


def _options(query: dict, firm_count: int) -> tuple[str, int, tuple[int, int] | None]:
    if set(query) - {"collection", "limit", "cursor"}:
        fail(400, "invalid_filter", "Dashboard panels accept only collection, limit and cursor.")
    collection = query.get("collection", "")
    if collection not in COLLECTIONS:
        fail(400, "invalid_collection", "Choose a documented dashboard panel.")
    page([], "id", query)
    raw = query.get("cursor", "")
    cursor = None
    if raw:
        if not re.fullmatch(r"[0-9]{1,8}:[0-9]{1,8}", raw):
            fail(400, "invalid_cursor", "Use the cursor from the same dashboard panel and company.")
        firm, ordinal = (int(p) for p in raw.split(":"))
        if firm >= firm_count or ordinal > 10_000_000:
            fail(400, "invalid_cursor", "Use the cursor from the same dashboard panel and company.")
        cursor = firm, ordinal
    return collection, int(query.get("limit", "50")), cursor


def project(keys: list[str], read: Callable[[str], dict], query: dict) -> dict:
    """Read the cursor's company first to recover its ordering key, then retain at most one selected page."""
    collection, limit, cursor = _options(query, len(keys))
    order = ([cursor[0]] if cursor else []) + [i for i in range(len(keys)) if not cursor or i != cursor[0]]
    selected: list[tuple] = []
    total = remaining = 0
    after = None

    def candidates(part: dict, firm: int, previous: list, after: tuple | None) -> Iterator[tuple]:
        nonlocal total, remaining
        yield from previous
        for ordinal, row in enumerate(_rows(part, collection)):
            total += 1
            key = _key(row, firm, ordinal, collection)
            if after is not None and key <= after:
                continue
            remaining += 1
            yield key, firm, ordinal, row

    for firm in order:
        part = read(keys[firm])
        if cursor and firm == cursor[0]:
            row = next(islice(_rows(part, collection), cursor[1], cursor[1] + 1), None)
            if row is None:
                fail(400, "invalid_cursor", "The cursor's row is no longer in this dashboard panel.")
            after = _key(row, firm, cursor[1], collection)

        selected = nsmallest(limit, candidates(part, firm, selected, after), key=lambda entry: entry[0])
        del part
    return _response(selected, total, remaining > limit)


def _response(selected: list[tuple], total: int, more: bool) -> dict:
    return {
        "data": [entry[3] for entry in selected],
        "total": total,
        "next_cursor": f"{selected[-1][1]}:{selected[-1][2]}" if more else None,
    }


def initial(part: dict) -> dict:
    """The initial 50-row pages share the state already built for totals; no second ledger projection."""
    return {collection: project([part["co"]], lambda _: part, {"collection": collection}) for collection in COLLECTIONS}


def merge(parts: list[dict]) -> dict:
    """A global first page needs at most the first 50 rows from each firm's ordered first page."""
    result = {}
    for collection in COLLECTIONS:
        pages = [part["dashboard_pages"][collection] for part in parts]
        candidates = (
            (_key(row, firm, ordinal, collection), firm, ordinal, row)
            for firm, page in enumerate(pages)
            for ordinal, row in enumerate(page["data"])
        )
        selected = nsmallest(50, candidates, key=lambda entry: entry[0])
        total = sum(page["total"] for page in pages)
        result[collection] = _response(selected, total, total > len(selected))
    return result
