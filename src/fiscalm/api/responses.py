"""Shared public API refusals and paginated responses, independent of route execution."""

from __future__ import annotations


class Error(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def fail(status: int, code: str, message: str) -> None:
    raise Error(status, code, message)


def page(rows: list[dict], key: str, query: dict) -> dict:
    try:
        limit = int(query.get("limit", "50"))
    except ValueError:
        fail(400, "invalid_limit", "Limit must be an integer from 1 to 200.")
    if not 1 <= limit <= 200:
        fail(400, "invalid_limit", "Limit must be an integer from 1 to 200.")
    cursor = query.get("cursor", "")
    rows = sorted(rows, key=lambda r: r[key])
    rows = [r for r in rows if r[key] > cursor]
    selected = rows[:limit]
    return {"data": selected, "next_cursor": selected[-1][key] if len(rows) > limit else None}


def bounded(value: object, key: str = "") -> object:
    """Paged records use scalars for totals/counts; identities and currency aggregates stay complete."""
    if isinstance(value, dict):
        return {name: bounded(item, name) for name, item in value.items()}
    if isinstance(value, list):
        aggregate = all(isinstance(item, dict) and set(item) <= {"currency", "total"} for item in value)
        rows = value if key in ("companies", "both") or aggregate else value[:50]
        return [bounded(item) for item in rows]
    return value
