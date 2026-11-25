"""Paged task references and bounded card inputs over complete company projections."""

from __future__ import annotations

from collections.abc import Iterator

from fiscalm.api.responses import bounded


def references(s: dict) -> Iterator[tuple[str, bool, dict]]:
    """Preserve Home.taskCards' source order, keys and urgency; state owns the money decisions. Urgent (red) is only money
    overdue and what fails until a person acts (an exported payment for a credited invoice, an unknown payment file, an
    expired, unreadable or refused connection, an unreadable e-Factura invoice); questions and retrying reads are not."""
    co = s["co"]
    for r in s.get("settlements", []):
        if r["status"] == "pending":
            yield co + "set" + r["id"], False, {"settlements": [r]}
    cod = s.get("cod", {})
    by_id = {r["id"]: r for r in cod.get("rows", [])}
    for t in cod.get("tasks", []):
        row = by_id.get(t["id"])
        patch = {"cod": cod | {"tasks": [t], "rows": [row] if row else []}}
        yield co + t["kind"] + t["id"], t["kind"] != "cod_returned", patch
    for file in s.get("files", []):
        if (
            file["question"]
            or s["live"]
            and file["status"] == "open"
            and file["payment_count"] > file["confirmed_count"] + file["skipped_count"]
        ):
            yield co + "pf" + str(file["id"]), file["question"] == "orphan", {"files": [file]}
    yield from _invoice_references(s)
    yield from _followups(s)
    if s.get("statement_hold"):  # automatic payments wait for last month's statement: upload it
        yield co + "stm", False, {"statement_hold": s["statement_hold"]}
    yield from _other_references(s)


def credit_holds(s: dict) -> list[dict]:
    """Linked invoices whose payment is no longer their net (a credit since, or one that stopped linking), imported
    in the bank or not: the difference stays to check until a person resolves it."""
    return [r for r in s["invoices"] if r.get("amount_changed")]


def _invoice_references(s: dict) -> Iterator[tuple[str, bool, dict]]:
    co = s["co"]
    invoices = {r["id"]: r for r in s["invoices"]}
    partners = {r["id"]: r for r in s["partners"]}
    for r in s["invoices"]:
        if r["status"] == "open" and r["due_date"] and r["due_date"] < s["today"]:
            yield co + "late" + r["id"], True, {"invoices": [r]}
    for r in s["ambiguous"]:
        yield co + "amb" + r["tx"], False, {"ambiguous": [r | {"candidate_count": len(r["candidates"])}]}
    for r in s.get("mail_proofs", []):
        yield co + "mp" + r["id"], False, {"mail_proofs": [r]}
    for r in s["invoices"]:
        if r.get("credit_question"):  # a credit note naming no invoice fits several: a person says which it cancels
            fits = [invoices[c["id"]] for c in r["credit_question"] if c["id"] in invoices]
            yield co + "cq" + r["id"], False, {"invoices": [r, *fits]}
    for r in credit_holds(s):
        yield co + "credit" + r["id"], True, {"invoices": [r]}
    for r in s["refused"]:
        if r["id"] in invoices:
            invoice = invoices[r["id"]]
            partner = partners.get(invoice["partner"])
            yield (
                co + "ref" + r["id"],
                False,
                {
                    "refused": [r],
                    "invoices": [invoice],
                    "partners": [partner] if partner else [],
                },
            )


def _followups(s: dict) -> Iterator[tuple[str, bool, dict]]:
    co = s["co"]
    issued = {r["id"]: r for r in s["issued"]}
    for r in s["issued"]:
        if r.get("local_id"):
            issued.setdefault(r["local_id"], r)
    partners = {r["id"]: r for r in s["partners"]}
    for r in s["reminder_tasks"]:
        invoice = issued.get(r["id"])
        yield (
            co + "rem" + r["id"],
            True,
            {
                "reminder_tasks": [r],
                "issued": [invoice] if invoice else [],
            },
        )
    for r in s["partner_tasks"]:
        partner = partners.get(r["id"])
        yield co + r["kind"] + r["id"], False, {"partner_tasks": [r], "partners": [partner] if partner else []}


def _other_references(s: dict) -> Iterator[tuple[str, bool, dict]]:
    co = s["co"]
    for r in s["reports"]:
        if r["status"] in ("ready", "prepared"):
            report = r | {"doc_count": sum(x["kind"] != "statement" for x in r["docs"])}
            yield "pk" + r["month"], False, {"reports": [report]}
    for r in s["to_check"]:
        yield "m" + r["key"], False, {"to_check": [r]}
    for r in s["recurring_refused"]:
        yield co + "rec" + r["id"], True, {"recurring_refused": [r]}
    for r in s.get("emag_tasks", []):
        yield "emag" + str(r["id"]), False, {"emag_tasks": [r]}
    for r in s.get("order_tasks", []):
        if r["kind"] == "order_vat_review" and r["draft_id"]:
            yield co + "ordvat" + r["order_id"], False, {"order_tasks": [r]}
    for r in s.get("bank_connections", []):
        if r["connection"] and r["connection"]["state"] != "ok":
            yield "bank" + co + r["bank"], r["connection"]["state"] == "expired", {"bank_connections": [r]}
    anaf = s["system"]["anaf"]
    if (
        anaf["status"] in ("ok", "error")
        and anaf["authorized"]
        and anaf["days_left"] is not None
        and anaf["days_left"] <= 30
    ):
        yield "anaf-exp", False, {"system": s["system"]}
    if anaf["status"] == "error":
        yield "anaf-err", anaf["reconnect"], {"system": s["system"]}
    if anaf["status"] == "expired":
        yield "anaf-off", True, {"system": s["system"]}
    if s["system"]["mail"]["stale"]:
        yield "mail", s["system"]["mail"]["reconnect"], {"system": s["system"]}
    for r in s["unreadable"]:
        yield co + "u" + r["id"], True, {"unreadable": [r]}


class Collector:
    """One read's exact deduplicated counts, one page and three highlights."""

    def __init__(self, offset: int = 0, limit: int = 0, group: str = "") -> None:
        self.offset, self.limit, self.group = offset, limit, group
        self.seen: set[str] = set()
        self.urgent = 0
        self.data: list[dict] = []
        self.first: list[dict] = []
        self.first_urgent: list[dict] = []

    def add(self, state: dict) -> None:
        rows = references(state)
        for key, urgent, patch in rows:
            if key in self.seen:
                continue
            position = len(self.seen)
            self.seen.add(key)
            self.urgent += int(urgent)
            selected = self.offset <= position < self.offset + self.limit
            if not (selected or len(self.first) < 3 or urgent and len(self.first_urgent) < 3):
                continue
            entry = {
                "key": key,
                "company": state["co"],
                "urgent": urgent,
                "patch": bounded({"live": state["live"], "today": state["today"], **patch}),
            }
            if selected:
                self.data.append(entry)
            if len(self.first) < 3:
                self.first.append(entry)
            if urgent and len(self.first_urgent) < 3:
                self.first_urgent.append(entry)

    def result(self) -> dict:
        end = self.offset + self.limit
        return {
            "total": len(self.seen),
            "urgent": self.urgent,
            "group": self.group,
            "data": self.data,
            "highlights": self.first_urgent or self.first,
            "next_cursor": str(end) if self.limit and end < len(self.seen) else None,
        }
