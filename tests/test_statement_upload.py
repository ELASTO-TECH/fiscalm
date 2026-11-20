"""Uploading a statement in fiscalm: one company, optionally one bank, the content decides the type, and it goes
through the same import as mail. Synthetic data only."""

import json
from datetime import date
from urllib.parse import urlencode

import pytest
from conftest import go_live
from test_api import (
    _req,  # noqa: F401
    server as server,
)
from test_camt import complete
from test_mt940 import EUR, block

from fiscalm import pack, recon
from fiscalm.api import web
from fiscalm.bank import statements, upload as su
from fiscalm.ledger import finance_home


def tmp_files():
    d = finance_home() / "state" / "tmp"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_a_complete_camt_is_imported_once(ledger):
    r = su.accept(ledger, "NORD", "", complete(), "application/xml")
    assert (r["status"], r["message"], r["company"]) == ("added", su.ADDED, "NORD")
    assert "unprotected" not in r and "file" not in r  # no server path leaves fiscalm
    (doc,) = ledger.documents(kind="statement")
    assert doc["company"] == "NORD" and doc["accounts"]
    assert su.accept(ledger, "NORD", "", complete(), "text/xml; charset=utf-8")["message"] == su.DUPLICATE
    assert tmp_files() == []  # the upload's private copy is gone


def test_a_bank_card_only_takes_that_banks_statement(ledger):
    with pytest.raises(su.Refused) as e:
        su.accept(ledger, "NORD", "ing", complete(), "application/xml")  # BT's accounts, uploaded on ING's card
    assert e.value.message == su.WRONG_BANK and not ledger.documents(kind="statement")
    assert su.accept(ledger, "NORD", "bt", complete(), "application/xml")["status"] == "added"


@pytest.mark.parametrize(
    "data,ctype,status,message",
    [
        (b"%PDF-1.7 ...", "application/xml", 415, su.NOT_A_STATEMENT),  # the content decides, and must agree
        (b"<Document/>", "application/pdf", 415, su.NOT_A_STATEMENT),
        (b"PK\x03\x04zip", "application/pdf", 415, su.NOT_A_STATEMENT),
        (b"<x/>", "text/html", 415, su.NOT_A_STATEMENT),
        (b"<" + b"x" * su.MAX_XML, "application/xml", 413, su.TOO_BIG),
        (b"%PDF-1.7", "application/pdf", 422, su.pdf_support()),  # on ING's card
    ],
)
def test_what_isnt_a_statement_is_refused_before_anything_is_written(ledger, data, ctype, status, message):
    with pytest.raises(su.Refused) as e:
        su.accept(ledger, "NORD", "ing" if message == su.pdf_support() else "", data, ctype)
    assert (e.value.status, e.value.message) == (status, message)
    assert not ledger.documents(kind="statement") and tmp_files() == []


@pytest.mark.parametrize(
    "co,bank,status",
    [
        ("NOPE", "", 400),
        ("NORD", "nope", 400),
        ("../NORD", "", 400),
        ("STUDIO9", "", 422),  # NORD's accounts
    ],
)
def test_an_unknown_company_or_bank_or_another_companys_statement_is_refused(ledger, co, bank, status):
    with pytest.raises(su.Refused) as e:
        su.accept(ledger, co, bank, complete(), "application/xml")
    assert e.value.status == status and not ledger.documents(kind="statement") and tmp_files() == []


def mt940(ron=None, **kw):
    """NORD's two BT accounts as MT940, the RON one changed by `kw` (or replaced by `ron`)."""
    return (ron or block(**kw)) + b"\r\n" + block(iban=EUR, ccy="EUR", closing="C261031EUR1000,00", lines=[], ref="E")


NOV_1_10 = dict(closing="C261110RON1000,00", lines=[])
CAMT_1_15 = complete().replace(b"2026-10-31", b"2026-10-15").replace(b"2026-10-25", b"2026-10-05")
CAMT_PRCD_1_15 = CAMT_1_15.replace(b"<Cd>OPBD</Cd>", b"<Cd>PRCD</Cd>").replace(b"2026-10-01", b"2026-09-30")


@pytest.mark.parametrize(
    "co,data,ctype,message",
    [
        (
            "NORD",
            mt940(opening="C261031RON1000,00", **NOV_1_10),
            "text/plain",
            su.PARTIAL.format(last=30, month="noiembrie"),
        ),
        ("NORD", CAMT_1_15, "application/xml", su.PARTIAL.format(last=31, month="octombrie")),
        ("NORD", CAMT_PRCD_1_15, "application/xml", su.PARTIAL.format(last=31, month="octombrie")),
        ("STUDIO9", mt940(), "text/plain", su.OTHER_ACCOUNT),
        ("STUDIO9", complete(), "application/xml", su.OTHER_ACCOUNT),
        ("NORD", mt940(closing="C261031RON700,00"), "text/plain", dict(su.KNOWN)["add up"]),
        (
            "NORD",
            mt940(lines=[":61:2610251025RD250,00NTRFNONREF", ":86:x"]),
            "text/plain",
            dict(su.KNOWN)["require manual review"],
        ),
        (
            "NORD",
            block() + b"\r\n" + block(iban=EUR, opening="C260831RON1,00", closing="C260930RON1,00", lines=[]),
            "text/plain",
            dict(su.KNOWN)["different months"],
        ),
        ("NORD", mt940(ron=block().replace(b":60F:", b":60M:")), "text/plain", su.RESTATE),
        (
            "NORD",
            complete().replace(b"053.001.02", b"052.001.02"),
            "application/xml",
            dict(su.KNOWN)["Expected CAMT.053"],
        ),
    ],
)
def test_a_refused_statement_says_why_and_what_to_do(ledger, co, data, ctype, message):
    with pytest.raises(su.Refused) as e:
        su.accept(ledger, co, "", data, ctype)
    assert e.value.message == message and not ledger.documents(kind="statement") and tmp_files() == []


def test_parser_text_never_reaches_the_message():
    columns = "CSV columns not found: already imported"  # a column name is the file's text
    assert su._refusal(statements.StatementError(columns)) == su.KNOWN[0][1]
    for text in ("Invalid CAMT XML: not well-formed (invalid token): line 1, column 7", f"{EUR}.sta: unknown"):
        assert su._refusal(statements.StatementError(text)) == su.NOT_READ


def _post(server, data, ctype="application/xml", query="co=NORD", **headers):
    return _req(server, "POST", f"/web/api/statement-upload?{query}", data, {"Content-Type": ctype, **headers})


def test_http_upload_answers_with_the_state_and_works_before_going_live(server, ledger):
    code, _, raw = _post(server, complete())
    r = json.loads(raw)
    assert code == 200 and r["ok"] and r["message"] == su.ADDED and r["done"]["status"] == "added" and r["state"]
    assert r["state"]["bounded"] and r["state"]["co"] == "NORD"
    assert r["state"]["invoices"] == r["state"]["issued"] == r["state"]["documents"] == []


@pytest.mark.parametrize(
    "query,data,ctype,headers,status",
    [
        ("co=NOPE", complete(), "application/xml", {}, 400),
        ("co=NORD&path=/etc/passwd", complete(), "application/xml", {}, 400),  # a request never names a path
        ("co=NORD", complete(), "application/json", {}, 415),
        ("co=NORD", complete(), "application/xml", {"Origin": "https://evil.invalid"}, 403),
    ],
)
def test_http_upload_refusals(server, ledger, query, data, ctype, headers, status):
    code, _, raw = _post(server, data, ctype, query, **headers)
    assert code == status and not json.loads(raw)["ok"]
    assert not ledger.documents(kind="statement")


def test_http_upload_is_for_administrators_only(server, ledger, monkeypatch):
    monkeypatch.setattr(web.Handler, "_require", lambda self, need: need in ("read", "member"))
    code, _, raw = _post(server, complete())
    assert code == 403 and json.loads(raw)["message"] == "Doar administratorii pot încărca extrase."
    assert not ledger.documents(kind="statement")


# ---- a BT PDF upload: what the PDF says is checked against the company before anything is stored


def fake_pdf_import(monkeypatch, accounts, parse_error=None, decrypt_error=None):
    """decrypt_to and parse stubbed: the upload's PDF 'opens' (as an unencrypted one would) and reads as `accounts`."""

    def decrypt(path, out_dir, password_lookup=None, only=None):
        if decrypt_error:
            raise decrypt_error
        out = out_dir / "Extras_BT_NORD_2026-10.pdf"
        out.write_bytes(b"%PDF-decrypted")
        return only, "2026-10", out

    def parse(path, company):
        if parse_error:
            raise parse_error
        accts = [statements.Account(i, c, []) for i, c in accounts]
        return statements.Statement(company, date(2026, 10, 1), date(2026, 10, 31), accts)

    monkeypatch.setattr(statements, "decrypt_to", decrypt)
    monkeypatch.setattr(statements, "parse", parse)


def leftovers():
    d = finance_home() / "state" / "tmp"
    return [p for p in d.rglob("*")] if d.exists() else []


NORD_RON, NORD_EUR, STUDIO9_RON = "RO24BTRLRONCRT0000000001", "RO71BTRLEURCRT0000000001", "RO94BTRLRONCRT0000000002"


@pytest.mark.parametrize(
    "accounts,message,errors",
    [
        ([(STUDIO9_RON, "RON")], su.OTHER_ACCOUNT, {}),  # NORD's CUI in the text, STUDIO9's account in the statement
        ([(NORD_RON, "RON"), (STUDIO9_RON, "RON")], su.OTHER_ACCOUNT, {}),
        ([(NORD_RON, "RON")], su.INCOMPLETE, {}),  # BT's EUR account missing
        ([], su.NOT_READ, {}),
        ([], su.NOT_READ, {"parse_error": ValueError("day 99 out of range")}),  # the parser chokes
        (
            [],
            su.NO_PASSWORD,
            {
                "decrypt_error": statements.SecretsUnavailable(  # said without any path
                    "x.pdf: secret file /home/someone/.config/fiscalm/secrets/bt-NORD is missing"
                )
            },
        ),
    ],
)
def test_a_pdfs_accounts_must_be_the_companys_and_complete(ledger, monkeypatch, accounts, message, errors):
    fake_pdf_import(monkeypatch, accounts, **errors)
    with pytest.raises(su.Refused) as e:
        su.accept(ledger, "NORD", "", b"%PDF-1.7 synthetic", "application/pdf")
    assert e.value.message == message
    assert not ledger.documents(kind="statement") and leftovers() == []


def test_a_pdf_accepted_in_the_app_goes_in_its_months_pack(ledger, monkeypatch):
    fake_pdf_import(monkeypatch, [(NORD_RON, "RON"), (NORD_EUR, "EUR")])
    assert su.accept(ledger, "NORD", "", b"%PDF-1.7 synthetic", "application/pdf")["status"] == "added"
    (doc,) = ledger.documents(kind="statement")
    assert doc["source"] == "statement-import"  # checked by the firm's password and accounts: not a held upload
    assert pack.ready(ledger, "NORD", "2026-10") and [
        d["sha"] for d in pack.pending_docs(ledger, "NORD", "2026-10")
    ] == [doc["sha"]]


def test_a_symlinked_company_directory_is_refused(ledger, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (finance_home() / "NORD").symlink_to(outside)
    with pytest.raises(su.Refused) as e:
        su.accept(ledger, "NORD", "", complete(), "application/xml")
    assert e.value.status == 409 and list(outside.iterdir()) == []


def test_a_symlink_under_the_company_never_receives_a_decrypted_statement(ledger, monkeypatch, tmp_path):
    fake_pdf_import(monkeypatch, [(NORD_RON, "RON"), (NORD_EUR, "EUR")])
    outside = tmp_path / "outside"
    outside.mkdir()
    month = recon.month_dir("NORD", "2026-10", finance_home())
    month.mkdir(parents=True)
    (month / "extrase").symlink_to(outside)
    with pytest.raises(su.Refused):
        su.accept(ledger, "NORD", "", b"%PDF-1.7 synthetic", "application/pdf")
    assert list(outside.iterdir()) == [] and not ledger.documents(kind="statement") and leftovers() == []


REPORT, REPORT_QUERY = b"Ref,Gross,Fee\nTEST-1,100.00,2.00\n", "co=NORD&report=1&date=2026-11-03"


def test_report_upload_asks_then_stores_on_partner(server, ledger, home):
    assert _post(server, REPORT, "text/csv", REPORT_QUERY)[0] == 409  # no report write in shadow mode
    go_live(home)
    code, _, raw = _post(server, REPORT, "text/csv", REPORT_QUERY)
    assert code == 409 and json.loads(raw)["report"]["columns"] == ["Ref", "Gross", "Fee"]
    fmt = dict(
        source="netopia", name="Synthetic Processor SRL", mapping=dict(reference="Ref", gross="Gross", fee="Fee")
    )
    code, _, raw = _post(server, REPORT, "text/csv", REPORT_QUERY + "&" + urlencode({"format": json.dumps(fmt)}))
    assert code == 200 and json.loads(raw)["ok"]
    assert (
        ledger.db.execute("SELECT partner FROM partner_settlement_formats").fetchone()[0]
        == "NORD.n-synthetic-processor-srl"
    )


@pytest.mark.parametrize(
    "case,status",
    [
        ("size", 413),
        ("encoding", 422),
        ("format", 400),
        ("nested", 400),
        ("statement-query", 400),
        ("large-format", 400),
    ],
)
def test_report_upload_refuses_untrusted_or_wrong_context(server, ledger, home, case, status):
    go_live(home)
    data, query, headers = REPORT, REPORT_QUERY, {}
    if case == "size":
        headers["Content-Length"] = str(4 * 1024 * 1024 + 1)
    elif case == "encoding":
        data = b"\xff,\xff\n1,2\n"
    elif case == "large-format":
        query += "&" + urlencode({"format": "x" * 4097})
    elif case in ("format", "nested"):
        query += "&" + urlencode({"format": "{" if case == "format" else "[" * 1500 + "]" * 1500})
    else:
        query = "co=NORD&format=ignored"
    assert _post(server, data, "text/csv", query, **headers)[0] == status
    assert ledger.db.execute("SELECT count(*) FROM settlement_reports").fetchone()[0] == 0
