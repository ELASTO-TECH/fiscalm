"""Output equivalence and stable query budgets, with no wall-time gate."""

from __future__ import annotations

import json

import pytest
import state_reference
from state_large import NOW, build, measure

from fiscalm.api import lists, state as api
from fiscalm.efactura import inbox as efactura
from fiscalm.invoicing import engine as invoicing, spv


@pytest.mark.parametrize("co", ["NORD", "ALL"])
@pytest.mark.parametrize("scale", [4, 8])
def test_state_equivalence_and_query_budget(ledger, home, co, scale, monkeypatch):
    """The same state as the original readers, in a bounded number of queries whatever the number of lines; live,
    every open invoice's can_approve runs check_payment, whose ANAF statuses and proved accounts are read once per
    company (measured: 104 and 158 queries at scale 8 and 100 alike)."""
    # Other tests may leave synthetic credentials in the session-wide fake directory.
    for name in ("SECRETS", "WEB_CONFIG", "WEBHOOK_CONFIG"):
        monkeypatch.setenv("FISCALM_" + name, str(home / name.lower()))
    build(ledger, home, scale=scale)
    actual, metrics = measure(ledger, co)
    with state_reference.original_readers():
        expected, before = measure(ledger, co)
    assert actual == expected
    assert json.dumps(actual, sort_keys=False) == json.dumps(expected, sort_keys=False)
    parts = actual.get("parts", [actual])
    for part in parts:
        company = part["co"]
        docs = {d["id"]: d for d in part["documents"]}
        assert len(docs) == 10 * scale + 4
        assert all(d["files"] for d in docs.values())
        assert api.doc_file(ledger, company, "takeover-" + company) is not None
        assert any(
            d["vendor"] == "takeover-" + company + ".jpg"
            for report in part["reports"]
            if report["month"] == "2026-10"
            for d in report["docs"]
        )

    assert all(report["bank"]["error"] is None for part in parts for report in part["reports"] if report["bank"])
    # Reuse whole-request invoice, payment, document, pack and metadata reads.
    # ALL: 145 since the enabled shops are read once per request for their Emitere automată rules.
    assert metrics["queries"] <= (102 if co == "NORD" else 145)
    assert metrics["queries"] < before["queries"]
    assert metrics["subprocesses"] == before["subprocesses"] == 0
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    live_state, live = measure(ledger, co)
    assert live["queries"] <= metrics["queries"] + 3 * len(parts)
    rows = {
        i["id"]: i for part in live_state.get("parts", [live_state]) for i in part["invoices"] if i["status"] == "open"
    }
    assert len(rows) > 7 * scale and not any(i["can_approve"] for i in rows.values())  # no valid IBAN in the build
    assert {efactura.payment_refusal(ledger, ledger.efactura(mid)).code for mid in rows} == {"no_valid_iban"}
    # The cache is per request: mutations must be visible immediately on the next read.
    ledger.db.execute("UPDATE efactura SET total='999.99' WHERE message_id='100000'")
    ledger.db.commit()
    assert api.state(ledger, co, NOW) != live_state


@pytest.mark.parametrize("read", [api.state, lists.summary])
def test_state_query_count_is_independent_of_invoice_count(ledger, home, read):
    build(ledger, home, scale=1, received=10)
    actual, small = measure(ledger, "ALL", read=read)
    with state_reference.original_readers():
        expected, _ = measure(ledger, "ALL", read=read)
    assert json.dumps(actual) == json.dumps(expected)
    for co, prefix in (("NORD", "1"), ("STUDIO9", "2")):
        for i in range(10, 100):
            ledger.db.execute(
                "INSERT INTO efactura(message_id,company,status,total,raw_path,created_at) VALUES(?,?,'open','12.34','missing.zip',?)",
                (prefix + str(i), co, NOW.date().isoformat()),
            )
    ledger.db.commit()
    _, large = measure(ledger, "ALL", read=read)
    assert small["queries"] == large["queries"]


def test_state_batches_authored_invoice_evidence(ledger, home):
    build(ledger, home, scale=4)
    _, small = measure(ledger, "ALL")
    for co, prefix in (("NORD", 500000), ("STUDIO9", 600000)):
        for i in range(1, 60):
            iid = f"authored-{co}-{i}"
            ledger.db.execute(
                "INSERT INTO invoices(id,company,series,number,status,data) "
                "SELECT ?,company,series,?,'issued',data FROM invoices WHERE id=?",
                (iid, i + 1, "local-" + co),
            )
            for env in ("test", "prod"):
                ledger.db.execute("INSERT INTO invoice_sent_links VALUES(?,?,?)", (str(prefix + i), iid, env))
            ledger.db.execute(
                "INSERT INTO invoice_lines VALUES(?,1,?)",
                (iid, json.dumps(dict(description="Produs inventat", unit="H87", quantity="1", net="321.00"))),
            )
    ledger.db.commit()
    # Real submission beats a sent-link fallback; duplicate reversal lines count separately.
    ledger.db.execute(
        "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) "
        "VALUES('authored-NORD-1','test','rejected','synthetic')"
    )
    ledger.db.executemany("INSERT INTO invoice_reversals VALUES('authored-NORD-1',?,'authored-NORD-2')", [(1,), (2,)])
    ledger.db.execute(
        "UPDATE invoice_sent_links SET invoice_id='authored-NORD-3' WHERE message_id='500004' AND environment='test'"
    )
    ledger.db.commit()
    actual, large = measure(ledger, "ALL")
    assert small["queries"] == large["queries"]
    for co in ("NORD", "STUDIO9"):
        rows = {r["local_id"]: r for part in actual["parts"] if part["co"] == co for r in part["issued"]}
        if co == "NORD":
            assert rows["authored-NORD-1"]["reversed_by"] == ["authored-NORD-2"]
            assert rows["authored-NORD-2"]["storno_of"] == "authored-NORD-1"
        records = invoicing.issued_records(ledger, co)
        assert records == [invoicing.invoice(ledger, r["id"], company=co) for r in records]
        expected = {r["id"]: sub for r in records if (sub := spv.submission(ledger, r["id"])) is not None}
        assert spv.submissions(ledger, co) == expected
