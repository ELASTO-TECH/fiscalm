"""POST api/history-upload: the history import from fiscalm (fiscalm.history). Synthetic data only."""

import json
import socket

import pytest
from pdf_fake import text_pdf
from test_api import (
    _req,  # noqa: F401
    server as server,
)
from test_history_import import export, unpaid_xlsx, zipped
from test_issued import ubl

from fiscalm import company_config, history, history_pdf
from fiscalm.api import web


def _post(port, data, ctype="application/zip", query="co=NORD&start=2026-10-01", **headers):
    code, _, raw = _req(port, "POST", f"/web/api/history-upload?{query}", data, {"Content-Type": ctype, **headers})
    return code, json.loads(raw)


def test_upload_imports_answers_in_romanian_and_works_before_going_live(server, ledger):
    code, r = _post(server, export())
    assert code == 200 and r["ok"] and r["state"]
    assert r["state"]["bounded"] and r["state"]["co"] == "NORD"
    assert r["state"]["invoices"] == r["state"]["issued"] == r["state"]["documents"] == []
    assert r["message"] == ("Facturile au fost importate: 2 facturi în istoric, o factură urmărită.")
    assert (r["done"]["history_issued"], r["done"]["history_received"], r["done"]["issued"]) == (1, 1, 1)
    assert "raw_path" not in json.dumps(r) and "unreadable" not in r["done"]  # no server path, no member names
    code, r = _post(server, export())
    assert r["message"] == "Nicio factură nouă: 3 facturi erau deja în fiscalm."
    for key, day in (("k1", "2026-09-14"), ("k2", "2026-08-03")):
        ledger.db.execute(
            "INSERT INTO statement_txs(tx_key,company,direction,day) VALUES(?,'NORD','debit',?)", (key, day)
        )
    ledger.db.commit()
    firms = {c["key"]: c["history"] for c in company_config.listing(ledger)["companies"]}  # the firms listing shows it
    assert (firms["NORD"], firms["STUDIO9"]) == (
        {"start": "2026-10-01", "first_statement": "2026-08-03", "issued": 1, "received": 1},
        {"start": None, "first_statement": None, "issued": 0, "received": 0},
    )


@pytest.mark.parametrize(
    "query,ctype,headers,status",
    [
        ("co=NOPE&start=2026-10-01", "application/zip", {}, 400),
        ("co=NORD&co=STUDIO9&start=2026-10-01", "application/zip", {}, 400),  # a key twice
        ("co=NORD&start=2026-10-01&start=2026-09-01", "application/zip", {}, 400),
        ("co=NORD&start=2026-10-01&path=", "application/zip", {}, 400),  # an unknown key, even blank
        ("co=NORD&start=2026-10-01&path=/etc/passwd", "application/zip", {}, 400),  # a request never names a path
        ("co=NORD&start=2026-10-01", "text/plain", {}, 415),  # what a cross-site form can send
        ("co=NORD&start=2026-10-01", "multipart/form-data", {}, 415),
        ("co=NORD&start=2026-10-01", "application/zip", {"Origin": "https://evil.invalid"}, 403),
    ],
)
def test_upload_refusals_write_nothing(server, ledger, query, ctype, headers, status):
    code, r = _post(server, export(), ctype, query, **headers)
    assert code == status and not r["ok"]
    assert history.start_date(ledger, "NORD") is None


def test_refusals_say_why_in_romanian(server, ledger, monkeypatch):
    code, r = _post(server, zipped(("x.xml", ubl(supplier_cui="RO33333333", number="X 9"))))
    assert code == 422 and r["message"] == (
        "Unele facturi nu sunt ale acestei firme, de exemplu X 9. Nu s-a importat "
        "nimic. Importă separat exportul fiecărei firme, la firma respectivă."
    )
    code, r = _post(server, export(), query="co=NORD&start=")
    assert code == 422 and r["message"] == "Data de început lipsește sau nu este o dată."
    _post(server, export())
    code, r = _post(server, export(), query="co=NORD&start=2026-09-01")
    assert r["message"] == "Data de început a firmei este deja 01.10.2026. Importă cu aceeași dată."
    monkeypatch.setattr(history, "MAX_FILE", 100)
    code, r = _post(server, export())
    assert code == 413 and r["message"] == history.MESSAGES_RO["too_big"]


def test_upload_is_for_administrators_only(server, ledger, monkeypatch):
    monkeypatch.setattr(web.Handler, "_require", lambda self, need: need in ("read", "member"))
    code, r = _post(server, export())
    assert code == 403 and r["message"] == "Doar administratorii pot importa facturi."
    assert history.start_date(ledger, "NORD") is None


@pytest.mark.parametrize(
    "n,old,tracked,dup,bad",
    [
        (1, "o factură în istoric", "o factură urmărită", "o factură era", "un fișier nu s-a"),
        (2, "2 facturi în istoric", "2 facturi urmărite", "2 facturi erau", "2 fișiere nu s-au"),
        (20, "20 de facturi în istoric", "20 de facturi urmărite", "20 de facturi erau", "20 de fișiere nu s-au"),
        (101, "101 facturi în istoric", "101 facturi urmărite", "101 facturi erau", "101 fișiere nu s-au"),
        (120, "120 de facturi în istoric", "120 de facturi urmărite", "120 de facturi erau", "120 de fișiere nu s-au"),
    ],
)
def test_counts_go_before_nouns_only(n, old, tracked, dup, bad):
    res = history.Result("NORD", "2026-10-01", history_issued=n, issued=n, duplicates=n, unreadable_count=n)
    assert history.done_ro(res) == (
        f"Facturile au fost importate: {old}, {tracked}, {dup} deja în fiscalm, {bad} putut citi."
    )


def test_an_oversized_member_is_skipped_and_the_rest_imported(server, ledger, monkeypatch):
    monkeypatch.setattr(history, "MAX_MEMBER", 50_000)
    code, r = _post(
        server,
        zipped(("ok.xml", ubl(number="NRD 0001", issue="2026-08-03")), ("big.xml", b"<a>" + b" " * 60_000 + b"</a>")),
    )
    assert code == 200 and r["done"]["history_issued"] == 1 and r["done"]["unreadable_count"] == 1
    assert r["message"] == "Facturile au fost importate: o factură în istoric, un fișier nu s-a putut citi."


def _raw(port, head: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", int(port)), timeout=10) as s:
        s.sendall(head)
        s.shutdown(socket.SHUT_WR)
        return s.recv(4096)


def test_framing_is_refused_before_the_body_is_read(server, ledger):
    path = b"POST /web/api/history-upload?co=NORD&start=2026-10-01 HTTP/1.1\r\nHost: books.example\r\nOrigin: https://books.example\r\nX-Forwarded-Proto: https\r\nCookie: __Host-session=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\r\nContent-Type: application/zip\r\n"
    assert b" 413 " in _raw(server, path + b"Content-Length: 999999999999\r\n\r\n")  # headers only: no body sent
    assert b" 400 " in _raw(server, path + b"Content-Length: 10\r\nContent-Length: 10\r\n\r\n0123456789")
    assert b" 400 " in _raw(server, path + b"Content-Length: 10\r\nTransfer-Encoding: chunked\r\n\r\n")
    assert b" 400 " in _raw(server, path + b"Content-Length: 100\r\n\r\nshort")  # truncated
    form = path.replace(b"application/zip", b"application/x-www-form-urlencoded")
    assert b" 415 " in _raw(server, form + b"Content-Length: 3\r\n\r\na=b")
    assert history.start_date(ledger, "NORD") is None


def test_review_table_reads_pdf_and_guarded_answer(server, ledger):
    port = server
    raw = text_pdf([])
    code, result = _post(port, zipped(("scan.pdf", raw)))
    assert code == 200 and result["done"]["review"] == 1
    code, _, response = _req(port, "GET", "/web/api/history-review?co=NORD")
    row = json.loads(response)["rows"][0]
    code, _, response = _req(port, "GET", f"/web/history-document?co=NORD&sha={row['sha']}")
    assert code == 200 and response == raw
    body = dict(
        co="NORD",
        sha=row["sha"],
        target="STUDIO9",
        direction="issued",
        number="TEST-9",
        issue_date="2023-10-01",
        total="119.00",
        currency="RON",
        party_name="Firma Exemplu SRL",
        party_tax_id="77777777",
    )
    code, _, response = _req(
        port, "POST", "/web/api/history-review", json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    assert code == 200 and json.loads(response)["ok"]
    assert history.rows(ledger, "STUDIO9")[0]["number"] == "TEST-9"
    assert not history_pdf.pending(ledger, "NORD")
    assert history_pdf.document(ledger, "STUDIO9", row["sha"]) == raw


def test_xlsx_uses_same_admin_history_upload_and_detects_bytes(server, ledger):
    assert _post(server, ubl(number="TEST-42", issue="2023-11-03", total="119.00"), "application/xml")[0] == 200
    code, result = _post(server, unpaid_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert code == 200 and result["done"]["records"] == 1
    assert not ledger.db.execute("SELECT 1 FROM settled_tx").fetchone()
