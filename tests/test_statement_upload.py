"""Uploading a statement in fiscalm: one company, optionally one bank, the content decides the type, and it goes
through the same import as mail. Synthetic data only."""

import json
import zipfile
from datetime import date
from urllib.parse import urlencode

import pytest
from conftest import go_live
from pdf_fake import text_pdf
from test_api import (
    _req,  # noqa: F401
    server as server,
)
from test_camt import complete
from test_mt940 import EUR, block
from test_statement_accounts import bt_files

from fiscalm import companies, integrations, pack, recon
from fiscalm.api import receipts as intake, uploads as upload_links, web
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

    def decrypt(path, out_dir, password_lookup=None, only=None, plain=False):
        if decrypt_error:
            raise decrypt_error
        out_dir.mkdir(parents=True, exist_ok=True)
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


@pytest.mark.parametrize(
    "header,ours",
    [
        ("CUI: {}", True),
        ("CUI:{}", True),
        ("CUI:{}9", False),  # a longer number that starts with the CUI is another firm
        ("CUI:99999999\nCONT 123 Valuta\nTransfer;Atelier Nord;CUI:{}", False),  # ours only in a transaction
        ("CUI:{}\nCUI:33333333", False),  # two firms named: nobody's, even uploaded for one of them
    ],
)
def test_a_pdf_names_the_firm_with_or_without_a_space_but_never_by_a_prefix(tmp_path, header, ours):
    """Older BT statements print `CUI:<n>`. Only the header, before the first account, names the holder."""
    pdf = tmp_path / "statement.pdf"
    lines = [*header.format(companies.get("NORD").cui).split("\n"), "din 01/10/2026 - 31/10/2026"]
    pdf.write_bytes(text_pdf(lines))
    if ours:
        assert statements.decrypt_to(pdf, tmp_path / "out", None, "NORD", plain=True)[:2] == ("NORD", "2026-10")
    else:
        with pytest.raises(statements.StatementError):
            statements.decrypt_to(pdf, tmp_path / "out", None, "NORD", plain=True)


def test_the_holder_check_ends_where_the_parser_starts_an_account():
    """Extracted text may indent the account line with any whitespace; the parser and the check agree on it."""
    text = "CUI:99999999\n\u00a0CONT 123 Valuta\nTransfer;Atelier Nord;CUI:11111111\n"
    assert not statements.names_cui(text, "11111111")


def leftovers():
    d = finance_home() / "state" / "tmp"
    return [p for p in d.rglob("*")] if d.exists() else []


NORD_RON, NORD_EUR, STUDIO9_RON = "RO24BTRLRONCRT0000000001", "RO71BTRLEURCRT0000000001", "RO94BTRLRONCRT0000000002"


@pytest.mark.parametrize(
    "accounts,message,errors",
    [
        ([(STUDIO9_RON, "RON")], su.OTHER_ACCOUNT, {}),  # NORD's CUI in the text, STUDIO9's account in the statement
        ([(NORD_RON, "RON"), (STUDIO9_RON, "RON")], su.OTHER_ACCOUNT, {}),
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


@pytest.mark.parametrize("link", [None, "NORD", "ALL"])  # the same bytes sent first through an upload link
def test_a_pdf_accepted_in_the_app_goes_in_its_months_pack(ledger, monkeypatch, link):
    fake_pdf_import(monkeypatch, [(NORD_RON, "RON"), (NORD_EUR, "EUR")])
    if link:
        intake.accept(ledger, upload_links.create(ledger, link, "Phone")["path"][3:], [("pdf", b"%PDF-1.7 synthetic")])
    assert su.accept(ledger, "NORD", "", b"%PDF-1.7 synthetic", "application/pdf")["status"] == "added"
    (doc,) = ledger.documents(kind="statement")
    assert doc["source"] == "statement-import"  # checked by the firm's password and accounts: not a held upload
    assert [d["sha"] for d in pack.pending_docs(ledger, "NORD", "2026-10")] == [doc["sha"]]
    with zipfile.ZipFile(pack.build(ledger, "NORD", "2026-10")["zip_path"]) as made:  # the checked, decrypted copy
        assert made.read("2026-10_extras_BT_EUR_RON.pdf") == b"%PDF-decrypted"


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


def test_a_file_naming_accounts_in_an_error_never_reads_as_missing_accounts():
    """Review: a CSV column name comes from the file; only the engine's typed list names a missing account."""
    assert su._say(["CSV columns not found: missing configured accounts: EUR 9999; x"]) == su.KNOWN[0][1]


# ---- several BT PDFs of one month, uploaded together


def multipart(files: list[bytes]) -> tuple[bytes, str]:
    parts = [
        b'--b0undary\r\nContent-Disposition: form-data; name="files"; filename="extras.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n" + data + b"\r\n"
        for data in files
    ]
    return b"".join(parts) + b"--b0undary--\r\n", "multipart/form-data; boundary=b0undary"


def test_one_months_pdfs_uploaded_together_are_one_statement(ledger, sec, tmp_path):
    integrations.set_statement_password(ledger, "NORD", "bt", "synthetic")
    ron, eur = (p.read_bytes() for p in bt_files(tmp_path))
    r = su.accept_pdfs(ledger, "NORD", "", [ron])
    assert (r["status"], r["message"]) == ("added", su.ADDED + su.still_missing(["EUR 0001"]))
    r = su.accept_pdfs(ledger, "NORD", "bt", [eur])
    assert (r["status"], r["message"], r["month"]) == ("added", su.ADDED, "2026-10") and "unprotected" not in r
    assert len(ledger.documents(kind="statement")) == 2 and leftovers() == []
    assert su.accept_pdfs(ledger, "NORD", "", [eur, ron])["message"] == su.DUPLICATE


@pytest.mark.parametrize(
    "case,status,message",
    [
        ("whole", 200, su.ADDED),
        ("days missing", 422, su.PARTIAL.format(last=31, month="octombrie")),
        ("not a pdf", 415, su.NOT_A_STATEMENT),
        ("damaged pdf", 422, su.NOT_READ),
        ("on an account", 400, None),  # a CSV's query: never for several PDFs
    ],
)
def test_http_upload_of_several_pdfs(server, ledger, sec, tmp_path, case, status, message):
    integrations.set_statement_password(ledger, "NORD", "bt", "synthetic")
    files = [p.read_bytes() for p in bt_files(tmp_path, ((1, 15), (16, 31)))]
    if case == "days missing":
        files.pop()
    if case == "not a pdf":
        files[0] = complete()
    if case == "damaged pdf":
        files[0] = b"%PDF-1.7 " + b"x" * 5000
    body, ctype = multipart(files)
    code, _, raw = _post(server, body, ctype, "co=NORD&account=x" if case == "on an account" else "co=NORD")
    r = json.loads(raw)
    assert code == status and (message is None or r["message"] == message)
    assert len(ledger.documents(kind="statement")) == (4 if status == 200 else 0)
