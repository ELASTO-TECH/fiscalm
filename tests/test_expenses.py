"""Only persisted, unambiguous evidence joins a purchase's documents."""

import pytest

from fiscalm.expenses import derive, evidence
from fiscalm.ledger import Ledger


def invoice(id="i1", bon=None, company="NORD"):
    return {"id": id, "company": company, "bon": bon}


def document(id="d1", inv=None, company="NORD"):
    return {"id": id, "company": company, "invoice": inv}


def test_either_stored_link_is_evidence():
    for invs, docs in (
        ([invoice(bon="d1")], [document()]),
        ([invoice()], [document(inv="i1")]),
        ([invoice(bon="d1")], [document(inv="i1")]),
    ):
        assert derive("NORD", invs, docs) == [
            {"id": "NORD:invoice:i1", "company": "NORD", "invoice": "i1", "document": "d1", "sources": []}
        ]


def test_standalone_entities_and_no_similarity_matching():
    inv, doc = invoice(), document()
    inv.update(total="10.00", issue_date="2026-10-01", supplier="Magazin Inventat")
    doc.update(amount="10.00", date="2026-10-01", vendor="Magazin Inventat")
    rows = derive("NORD", [inv], [doc])
    assert [(r["invoice"], r["document"]) for r in rows] == [("i1", None), (None, "d1")]
    assert derive("NORD", [inv], []) == rows[:1]
    assert derive("NORD", [], [doc]) == rows[1:]


def test_foreign_invoice_never_joins():
    rows = derive("NORD", [invoice(company="STUDIO9", bon="d1")], [document(inv="i1")])
    assert [(r["invoice"], r["document"]) for r in rows] == [(None, "d1")]
    assert derive("STUDIO9", [invoice(company="STUDIO9", bon="d1")], [document(inv="i1")])[0]["document"] is None


def test_conflicting_claims_refuse_all_merges():
    cases = [
        ([invoice(bon="d1")], [document(inv="i1"), document("d2", "i1")]),
        ([invoice(bon="d1"), invoice("i2", "d1")], [document(inv="i1")]),
        ([invoice(bon="d1")], [document(inv="missing")]),
        ([invoice(bon="missing")], [document(inv="i1")]),
    ]
    for invs, docs in cases:
        rows = derive("NORD", invs, docs)
        assert len(rows) == len(invs) + len(docs)
        assert all(r["invoice"] is None or r["document"] is None for r in rows)


def test_ids_are_firm_qualified_and_ignore_order():
    invs, docs = [invoice(), invoice("i2", "d2")], [document(), document("d2")]
    first = derive("NORD", invs, docs)
    assert {r["id"] for r in first} == {r["id"] for r in derive("NORD", invs[::-1], docs[::-1])}
    other = derive("STUDIO9", [invoice(company="STUDIO9")], [document(company="STUDIO9")])
    assert not {r["id"] for r in first} & {r["id"] for r in other}


def test_conflict_does_not_prevent_an_unrelated_pair():
    rows = derive(
        "NORD",
        [invoice(bon="d1"), invoice("i2", "d3")],
        [document(inv="i1"), document("d2", "i1"), document("d3", "i2")],
    )
    assert {(r["invoice"], r["document"]) for r in rows} == {("i1", None), (None, "d1"), (None, "d2"), ("i2", "d3")}


def test_sources_union_has_one_stable_order():
    provenance = {
        ("invoice", "i1"): {"efactura", "history"},
        ("document", "d1"): {"upload", "mail", "upload_link", "efactura"},
    }
    assert derive("NORD", [invoice(bon="d1")], [document()], provenance)[0]["sources"] == [
        "efactura",
        "mail",
        "upload_link",
        "upload",
        "history",
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("mail:inbox", ["mail"]),
        ("upload:link", ["upload_link"]),
        ("upload:user:owner", ["upload"]),
        ("manual", ["upload"]),
        ("", []),
    ],
)
def test_document_intake_sources(ledger, source, expected):
    ledger.add_document("d1", "NORD", "2026-10", "receipt", "/fake/receipt.pdf", source=source)
    assert derive("NORD", [], [document()], evidence(ledger, "NORD"))[0]["sources"] == expected
    assert evidence(ledger, "STUDIO9") == {}


def test_filed_upload_keeps_intake_record(ledger):
    ledger.add_document("d1", "NORD", "2026-10", "receipt", "/fake/receipt.pdf", source="mail:inbox")
    ledger.db.execute(
        "INSERT INTO receipt_uploads(sha,link_id,company,uploaded_at,file_type) VALUES(?,?,?,?,?)",
        ("d1", "link", "NORD", "2026-10-01", "pdf"),
    )
    assert derive("NORD", [], [document()], evidence(ledger, "NORD"))[0]["sources"] == ["mail", "upload_link"]


@pytest.mark.parametrize(("upload_id", "expected"), [("spv-message", ["efactura"]), ("history-import", ["history"])])
def test_invoice_intake_sources(ledger, upload_id, expected):
    ledger.add_efactura(
        {
            "message_id": "i1",
            "company": "NORD",
            "upload_id": upload_id,
            "raw_path": "/fake/invoice.zip",
            "status": "open",
        }
    )
    ledger.add_document("d1", "NORD", "2026-10", "receipt", "/fake/receipt.pdf", source="upload:link")
    rows = derive("NORD", [invoice(bon="d1")], [document()], evidence(ledger, "NORD"))
    assert rows[0]["sources"] == (
        expected + ["upload_link"] if expected == ["efactura"] else ["upload_link", "history"]
    )


def test_statement_takeover_of_upload_is_not_expense_evidence(ledger):
    ledger.add_document("d1", "NORD", "2026-10", "receipt", "/fake/statement.pdf", source="upload:link")
    ledger.db.execute(
        "INSERT INTO receipt_uploads(sha,link_id,company,uploaded_at,file_type) VALUES(?,?,?,?,?)",
        ("d1", "link", "NORD", "2026-10-01", "pdf"),
    )
    ledger.db.commit()
    assert ledger.add_document("d1", "NORD", "2026-10", "statement", "/fake/statement.pdf", source="manual")
    assert evidence(ledger, "NORD") == {}


def test_concurrent_upload_waits_for_next_projection(ledger, monkeypatch):
    read = ledger.documents
    writer = Ledger(ledger.path)

    def upload_after_read(**kwargs):
        rows = read(**kwargs)
        writer.add_document("new", "NORD", "2026-10", "receipt", "/fake/receipt.pdf", source="upload:link")
        with writer.tx() as db:
            db.execute(
                "INSERT INTO receipt_uploads(sha,link_id,company,uploaded_at,file_type) VALUES(?,?,?,?,?)",
                ("new", "link", "NORD", "2026-10-01", "pdf"),
            )
        return rows

    monkeypatch.setattr(ledger, "documents", upload_after_read)
    try:
        assert evidence(ledger, "NORD") == {}
        monkeypatch.setattr(ledger, "documents", read)
        assert evidence(ledger, "NORD") == {("document", "new"): {"upload_link"}}
    finally:
        writer.close()
