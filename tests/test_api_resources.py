from datetime import date
from decimal import Decimal
from urllib.parse import quote

import pytest
from test_api import (
    server as server,  # noqa: F401
    world as world,
)
from test_api_v1 import request
from test_efactura import _stored, ubl

from fiscalm.api import state as api, tokens
from fiscalm.api.state import payment_id, resource
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.invoicing import issued


def test_lists_details_filters_links(token_owner, ledger, server, world):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    for kind, ident in (
        ("invoices/received", "101"),
        ("invoices/issued", "5001"),
        ("payments", None),
        ("documents", None),
    ):
        path = "/companies/NORD/" + kind
        code, result = request(server, secret, path + "?limit=1")
        assert code == 200 and len(result["data"]) == 1
        row = result["data"][0]
        assert "links" in row
        ident = ident or row["id"]
        assert request(server, secret, path + "/" + quote(ident, safe=""))[0] == 200
        assert request(server, secret, path + "/nonexistent")[0] == 404
        if result["next_cursor"]:
            code, second = request(server, secret, path + "?limit=1&cursor=" + quote(result["next_cursor"], safe=""))
            assert code == 200 and second["data"][0]["id"] > row["id"]
    code, result = request(server, secret, "/companies/NORD/invoices/received?status=open")
    assert code == 200 and all(r["status"] == "open" for r in result["data"])
    assert request(server, secret, "/companies/STUDIO9/invoices/received/101")[0] == 404
    assert request(server, secret, "/companies/NORD/documents?month=bad")[0] == 400
    mo = world["months"][1]
    code, report = request(server, secret, "/companies/NORD/reports/" + mo)
    assert code == 200 and report["links"]["received"]
    assert request(server, secret, "/companies/NORD/reports/2026-13")[0] == 400


def test_document_bytes_use_open_file_guard(token_owner, ledger, home, server, world):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    doc = next(d for d in api.state(ledger, "NORD")["documents"] if d["vendor"] == "Hetzner")
    path = "/companies/NORD/documents/" + doc["id"] + "/file"
    assert request(server, secret, path)[1].startswith(b"%PDF")
    assert request(server, secret, path.replace("NORD", "STUDIO9"))[0] == 404
    target = home / "private.txt"
    target.write_bytes(b"private")
    ledger.db.execute("UPDATE documents SET path=? WHERE sha=?", (str(target), doc["id"]))
    ledger.db.commit()
    assert request(server, secret, path)[0] == 404


def test_public_ids_reports_and_credit_rules(ledger, world, monkeypatch):
    co = "NORD"
    payments = resource(ledger, "payments", co, {}, {})
    assert payments["data"]
    for p in payments["data"]:
        assert len(p["id"]) == 64 and "item_id" not in p
    for r in resource(ledger, "received", co, {}, {})["data"]:
        assert "item_id" not in r
        if r["links"]["payment"]:
            p = resource(ledger, "payment_one", co, {"id": r["links"]["payment"]}, {})
            assert r["links"]["transaction"] == p["links"]["transaction"]
    monkeypatch.setattr(issued, "credit_links", lambda *a: {"5001": "5001", "ambiguous": None})
    row = resource(ledger, "issued_one", co, {"id": "5001"}, {})
    assert row["links"]["reversals"] == ["5001"]
    for rep in api.state(ledger, co)["reports"]:
        public = resource(ledger, "report", co, {"month": rep["month"]}, {})
        assert all(public[k] == v for k, v in rep.items())


def test_bon_proof_is_not_a_bank_transaction(ledger, world):
    invoice = next(r for r in resource(ledger, "received", "NORD", {}, {})["data"] if r["bon"])
    assert invoice["links"]["document"] == invoice["bon"]
    assert invoice["links"]["transaction"] is None
    assert ledger.settle_bon_card(invoice["id"], "b" * 20)
    invoice = resource(ledger, "received_one", "NORD", {"id": invoice["id"]}, {})
    assert invoice["links"]["transaction"] is None
    doc = resource(ledger, "document_one", "NORD", {"sha": invoice["bon"]}, {})
    assert doc["links"]["transaction"] == "b" * 20


@pytest.mark.parametrize(
    "error, kind",
    [
        (OSError("Cannot read /private/statement.pdf"), "unreadable"),
        (FileNotFoundError("/private/statement.pdf"), "missing"),
        (statements.SecretsUnavailable("statement.pdf: locked"), "password"),
        (statements.StatementError("Unsupported statement: /private/statement.pdf"), "format"),
    ],
)
def test_report_bank_errors_are_a_kind_never_the_text(ledger, monkeypatch, error, kind):
    def broken(*a):
        raise error

    monkeypatch.setattr(api, "_month_view", broken)
    assert api._bank(ledger, "NORD", "2026-10")["error"] == kind


def test_a_statement_whose_file_is_gone_reads_as_missing(ledger, tmp_path):
    ledger.add_document("sha-gone".ljust(40, "0"), "NORD", "2026-10", "statement", str(tmp_path / "gone.pdf"))
    assert api._bank(ledger, "NORD", "2026-10")["error"] == "missing"


@pytest.mark.parametrize("listed, day", [("202610141230", "2026-10-14"), ("", None), ("202613", None)])
def test_an_unreadable_efactura_shows_the_day_anaf_listed_it(ledger, world, listed, day):
    ledger.db.execute("UPDATE efactura SET listed_at=? WHERE parse_error IS NOT NULL AND company='NORD'", (listed,))
    assert [r["listed"] for r in api.state(ledger, "NORD")["unreadable"]] == [day]


def test_pair_proofs_exclude_mail_and_include_withdrawn_statement(ledger, world):
    raw = ledger.db.execute("SELECT * FROM efactura WHERE item_id IS NOT NULL LIMIT 1").fetchone()
    assert raw
    debit = statements.Tx(
        date=date(2026, 10, 20),
        title="Plata OP",
        description="Plata",
        direction="debit",
        amount=Decimal(raw["total"]),
        currency="RON",
        iban="RO24BTRLRONCRT0000000001",
        ref="W",
    )
    ledger.record_statement(statements.Statement(raw["company"], None, None, [statements.Account("x", "RON", [debit])]))
    key = statements.tx_key(debit)
    # only a transaction of the company's statement proves the pair; a bare 20-hex string is no transaction
    for proof, expected in (("ef-proof:private-message-id", None), ("bon:receipt", None), ("a" * 20, None), (key, key)):
        ledger.db.execute("DELETE FROM settled_tx WHERE what=?", ("payment " + raw["item_id"],))
        ledger.db.execute("UPDATE payments SET status='skipped' WHERE item_id=?", (raw["item_id"],))
        ledger.db.execute("UPDATE efactura SET paid_tx=?,status='paid' WHERE message_id=?", (proof, raw["message_id"]))
        ledger.db.commit()
        invoice = resource(ledger, "received_one", raw["company"], {"id": raw["message_id"]}, {})
        payment = resource(ledger, "payment_one", raw["company"], {"id": payment_id(raw["item_id"])}, {})
        assert invoice["links"]["transaction"] == payment["links"]["transaction"] == expected


def test_payment_ids_require_install_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "first"))
    first = payment_id("synthetic-payment")
    assert first == payment_id("synthetic-payment")
    assert (tmp_path / "first/payment-id-key").stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "second"))
    assert first != payment_id("synthetic-payment")


def test_received_can_approve_tracks_current_refusal(ledger, home, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    (home / "state" / "LIVE").touch()
    ledger.set_meta("efactura_autopay_refused:3001", "iban_unproved")

    def received():
        return resource(ledger, "received_one", "NORD", {"id": "3001"}, {})

    assert efactura.current_refusal(ledger, ledger.efactura("3001")) == "ready"
    assert received()["can_approve"] is True
