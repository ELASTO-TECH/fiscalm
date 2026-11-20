"""Conservative receipt line validation and comparison, independent of storage."""

from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal, InvalidOperation

FIELDS = ("name", "unit", "quantity", "net", "vat")
LINE_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in FIELDS}, "required": list(FIELDS)}
SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {"type": "array", "items": LINE_SCHEMA},
        **{k: {"type": "string"} for k in ("total", "currency", "payment")},
    },
    "required": ["lines", "total", "currency", "payment"],
}


def number(value: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,12}(?:\.\d{1,4})?", value):
        raise ValueError("Invalid decimal")
    return Decimal(value)


def validate(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        raise ValueError("Invalid receipt")
    if any(not isinstance(payload.get(k), str) for k in ("total", "currency", "payment")):
        raise ValueError("Invalid receipt")
    if any(
        not isinstance(line, dict) or any(not isinstance(v, str) for v in line.values()) for line in payload["lines"]
    ):
        raise ValueError("Invalid line")
    if set(payload) != set(SCHEMA["required"]) or len(payload["lines"]) > 40:
        raise ValueError("Invalid receipt")
    if payload["payment"] not in ("", "cash", "card") or not re.fullmatch("[A-Z]{3}|", payload["currency"]):
        raise ValueError("Invalid receipt")
    for line in payload["lines"]:
        if set(line) != set(FIELDS) or any(len(v) > 160 for v in line.values()):
            raise ValueError("Invalid line")
        for key in ("quantity", "net", "vat"):
            if line[key]:
                number(line[key])
    if payload["total"]:
        number(payload["total"])
    return payload


def signatures(payload: dict) -> Counter | None:
    """Exact labels preserve model numbers (USB 2 never becomes USB 3); missing values refuse."""
    try:
        validate(payload)
        if not payload["lines"] or not payload["currency"] or not payload["total"]:
            return None
        result = Counter()
        gross = Decimal(0)
        for line in payload["lines"]:
            if not all(line.values()):
                return None
            qty, net, vat = (number(line[k]) for k in ("quantity", "net", "vat"))
            if qty <= 0 or vat > 100:
                return None
            key = tuple(" ".join(line[k].casefold().split()) for k in ("name", "unit")) + (qty, net, vat)
            result[key] += 1
            gross += (net * (1 + vat / 100)).quantize(Decimal(".01"))
        if any(n > 1 for n in result.values()) or gross != number(payload["total"]):
            return None
        return result
    except (ValueError, InvalidOperation):
        return None
