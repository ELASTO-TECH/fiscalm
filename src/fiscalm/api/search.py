"""Bounded search selections; formatting and navigation labels remain in the app."""

from __future__ import annotations

import re
import unicodedata

# Match JavaScript's whitespace and combining-mark ranges, including pasted nonbreaking spaces/BOMs.
SPACE = r"[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
AMOUNT = re.compile(r"^([-\u2212])?([0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+)(?:([.,])([0-9]{0,2}))?$")
FIELDS = (
    ("partners", ("id", "company", "name", "cui", "vat_id", "roles", "last")),
    ("issued", ("id", "company", "client", "number", "total", "owed", "currency", "issue_date")),
    ("invoices", ("id", "company", "supplier", "number", "total", "currency", "issue_date")),
    ("documents", ("id", "company", "vendor", "amount", "currency", "date", "month")),
)


def fold(value: str) -> str:
    return re.sub(r"[\u0300-\u036f]", "", unicodedata.normalize("NFD", value)).lower()


def words(query: str) -> list[str]:
    text = re.sub(r"(?<![a-z0-9_])ro" + SPACE + r"*(?=[0-9])", "", fold(query))
    return [word for word in re.split(SPACE + "+", text) if word]


def tax_id(value: str | None) -> str:
    return re.sub(r"^ro(?=[0-9])", "", re.sub(SPACE + "+", "", value or ""), flags=re.I)


def matches(word: str, text: str, amounts: list[str]) -> bool:
    if word in text:
        return True
    amount = AMOUNT.fullmatch(word)
    if not amount:
        return False
    negative, whole, exact, decimals = amount.groups()
    whole = whole.replace(".", "")
    for value in amounts:
        if negative and not value.startswith("-"):
            continue
        units, _, bani = value.removeprefix("-").partition(".")
        if (units == whole and bani.startswith(decimals or "")) if exact else units.startswith(whole):
            return True
    return False


def _terms(kind: str, row: dict) -> tuple[str, list[str]]:
    if kind == "partners":
        return f"{row['name']} {tax_id(row['cui'])} {tax_id(row.get('vat_id'))}", []
    if kind == "issued":
        return f"{row['client']} {row['number'] or ''} {tax_id(row['client_cui'])}", [row["total"], row["owed"]]
    if kind == "invoices":
        return f"{row['supplier']} {row['number'] or ''} {tax_id(row['supplier_cui'])}", [row["total"]]
    return f"{row['vendor']} {row['note']}", [row["amount"]] if row["amount"] is not None else []


def _date(kind: str, row: dict) -> str:
    if kind == "partners":
        return row["last"]
    if kind == "documents":
        return row["date"] if row["date"] is not None else row["month"]
    return row["issue_date"] or ""


class Collector:
    """At most six newest rows per category across every firm, with stable date ties."""

    def __init__(self, query: str) -> None:
        self.words = words(query)
        self.ready = len("".join(self.words).encode("utf-16-le", errors="surrogatepass")) >= 4
        self.rows: dict[str, list[dict]] = {kind: [] for kind, _ in FIELDS}

    def add(self, part: dict) -> None:
        if not self.ready:
            return
        for kind, fields in FIELDS:
            found = []
            for row in part[kind]:
                text, amounts = _terms(kind, row)
                text = fold(text)
                if all(matches(word, text, amounts) for word in self.words):
                    found.append(row)
            chosen = sorted([*self.rows[kind], *found], key=lambda r: _date(kind, r), reverse=True)[:6]
            self.rows[kind] = [{field: row[field] for field in fields} for row in chosen]

    def result(self) -> dict:
        return self.rows
