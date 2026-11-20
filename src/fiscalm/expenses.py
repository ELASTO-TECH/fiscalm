"""Purchases derived from existing invoice/receipt evidence, without changing the ledger."""

from __future__ import annotations

from urllib.parse import quote

from fiscalm.history import IMPORTED
from fiscalm.ledger import Ledger

SOURCE_ORDER = ("efactura", "mail", "upload_link", "upload", "history")


def evidence(L: Ledger, co: str) -> dict[tuple[str, str], set[str]]:
    """Read intake records, never archive destinations or file names."""
    out = {}
    for r in L.efactura_rows(company=co):
        out[("invoice", r["message_id"])] = {"history" if r["upload_id"] == IMPORTED else "efactura"}
    for d in L.documents(company=co, kind="receipt"):
        source = d["source"] or ""
        codes = {"mail"} if source.startswith("mail:") else set()
        if source == "manual":
            codes.add("upload")
        out[("document", d["sha"])] = codes
        if source.startswith("upload:"):
            codes.add("upload" if source.startswith("upload:user:") else "upload_link")
    intake = getattr(L, "_expense_intake", None)
    if intake is not None:
        for (kind, key), codes in out.items():
            if kind == "document":
                codes.update(intake.get(key, set()))
        return out
    for table, code in (("mail_documents", "mail"), ("receipt_uploads", None)):
        for r in L.db.execute(
            f"SELECT m.* FROM {table} m JOIN documents d ON d.sha=m.sha WHERE d.company=? AND d.kind='receipt'", (co,)
        ):
            if ("document", r["sha"]) in out:
                out[("document", r["sha"])].add(code or ("upload_link" if r["link_id"] else "upload"))
    return out


def derive(co: str, invoices: list[dict], documents: list[dict], provenance: dict | None = None) -> list[dict]:
    """One expense per invoice or receipt, merging only unambiguous stored links in this firm.

    Either side may carry the evidence. Conflicting claims at either end refuse every affected
    merge, including claims to missing entities. Similar amounts, dates and names prove nothing.
    """
    provenance = provenance or {}
    invs = {r["id"]: r for r in invoices if r["company"] == co}
    docs = {d["id"]: d for d in documents if d["company"] == co}
    claims = {(r["id"], r["bon"]) for r in invs.values() if r.get("bon")}
    claims.update((d["invoice"], d["id"]) for d in docs.values() if d.get("invoice"))
    by_invoice: dict[str, set[str]] = {}
    by_document: dict[str, set[str]] = {}
    for inv, doc in claims:
        by_invoice.setdefault(inv, set()).add(doc)
        by_document.setdefault(doc, set()).add(inv)
    linked = {
        inv: doc
        for inv, doc in claims
        if inv in invs and doc in docs and len(by_invoice[inv]) == len(by_document[doc]) == 1
    }
    used = set(linked.values())
    rows = [("invoice", inv, inv, linked.get(inv)) for inv in invs]
    rows += [("document", doc, None, doc) for doc in docs if doc not in used]
    return [
        {
            "id": f"{quote(co, safe='')}:{kind}:{quote(key, safe='')}",
            "company": co,
            "invoice": inv,
            "document": doc,
            "sources": [
                code
                for code in SOURCE_ORDER
                if code in (provenance.get(("invoice", inv), set()) | provenance.get(("document", doc), set()))
            ],
        }
        for kind, key, inv, doc in rows
    ]
