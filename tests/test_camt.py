"""Invented CAMT statements and their import; no bank files or network."""

import json
import re
import subprocess
import sys
import threading
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal

import pytest
import tick_benchmark
from firms import iban as synthetic_iban, set_firms
from scale_benchmark import build, seed_mail_questions
from test_api import (
    quiet,  # noqa: F401
    server as server,
)
from test_efactura import SUPPLIER_IBAN, ubl
from test_receipt_upload import post

from fiscalm import archive, cli, companies, recon
from fiscalm.api import receipts as receipt_upload, uploads as upload_links
from fiscalm.bank import camt, intake as statement_intake, statements
from fiscalm.efactura import inbox as efactura
from fiscalm.ledger import Ledger
from fiscalm.mail import imap as imapread, invoices as mail_invoices


@pytest.mark.parametrize("module", ["fiscalm.bank.camt", "fiscalm.bank.upload"])
def test_statement_parser_imports_without_prior_initialization(module):
    subprocess.run([sys.executable, "-I", "-c", f"import {module}"], check=True, capture_output=True)


def detail(amount="250.00", ref="bank-1", remit="Invoice ABC123"):
    return f"""<TxDtls><Refs><AcctSvcrRef>{ref}</AcctSvcrRef><EndToEndId>end-{ref}</EndToEndId></Refs>
    <AmtDtls><TxAmt><Amt Ccy="RON">{amount}</Amt></TxAmt></AmtDtls>
    <RltdPties><Cdtr><Nm>Example Supplier SRL</Nm></Cdtr><CdtrAcct><Id><IBAN>{SUPPLIER_IBAN}</IBAN></Id></CdtrAcct></RltdPties>
    <RmtInf><Ustrd>{remit}</Ustrd><Strd><CdtrRefInf><Ref>ABC123</Ref></CdtrRefInf></Strd></RmtInf></TxDtls>"""


def entry(details=None, amount="250.00", flow="DBIT", status="BOOK"):
    return f"""<Ntry><Amt Ccy="RON">{amount}</Amt><CdtDbtInd>{flow}</CdtDbtInd><Sts>{status}</Sts>
    <BookgDt><Dt>2026-10-25</Dt></BookgDt><ValDt><Dt>2026-10-26</Dt></ValDt>
    <AcctSvcrRef>entry-1</AcctSvcrRef><NtryDtls>{detail() if details is None else details}</NtryDtls></Ntry>"""


def xml(version=2, entries=None, closing="750.00", iban=None, currency="RON"):
    iban = iban or synthetic_iban(f"RO00BTRL{currency}CRT0000000001")
    balances = "".join(
        f"""<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp><Amt Ccy="RON">{amt}</Amt>
    <CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-{day}</Dt></Dt></Bal>"""
        for code, amt, day in [("OPBD", "1000.00", "01"), ("CLBD", closing, "31")]
    )
    return f"""<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.0{version}"><BkToCstmrStmt><Stmt>
    <Acct><Id><IBAN>{iban}</IBAN></Id><Ccy>{currency}</Ccy></Acct>{balances}{entry() if entries is None else entries}
    </Stmt></BkToCstmrStmt></Document>""".replace('Ccy="RON"', f'Ccy="{currency}"').encode()


def section(data):
    return data[data.index(b"<Stmt>") : data.index(b"</Stmt>") + 7]


def complete():
    """NORD's whole BT month: the RON account with one debit and the EUR account without lines."""
    return xml().replace(
        b"</BkToCstmrStmt>", section(xml(currency="EUR", entries="", closing="1000")) + b"</BkToCstmrStmt>"
    )


def parse(tmp_path, data):
    p = tmp_path / "not-a-statement.pdf"
    p.write_bytes(data)
    return statements.parse(p, "NORD")


def write(tmp_path, data, name="bank.xml"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def add_invoice(ledger, mid="1", number="ABC123"):
    inv = efactura.parse_ubl(ubl(number=number, supplier="Example Supplier SRL"))
    d = asdict(inv)
    ledger.add_efactura(
        dict(
            message_id=mid,
            company="NORD",
            raw_path="fake",
            status="open",
            supplier_iban=inv.supplier_iban,
            **{
                k: d[k]
                for k in (
                    "doc_type",
                    "number",
                    "issue_date",
                    "due_date",
                    "currency",
                    "total",
                    "supplier_name",
                    "supplier_cui",
                )
            },
        )
    )


def fake_pdf(monkeypatch, tmp_path):
    """An encrypted BT PDF that reads as the complete CAMT month; its decrypted copy has other bytes on every opening."""
    p = write(tmp_path, b"%PDF-1.7 synthetic encrypted", "allmerge.pdf")
    st = camt.parse_camt(complete())
    monkeypatch.setattr(statements, "parse_pdf", lambda path, company: st)
    opened = []

    def decrypt(path, out_dir, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "decrypted.pdf"
        out.write_bytes(b"%%PDF-1.7 synthetic save %d" % len(opened))
        opened.append(path)
        return "NORD", "2026-10", out

    monkeypatch.setattr(statements, "decrypt_to", decrypt)
    return p


# ---- reading a CAMT.053 file


@pytest.mark.parametrize("version", range(2, 9))
def test_versions_and_fields(tmp_path, version):
    st = parse(tmp_path, xml(version))
    (t,) = st.txs
    assert st.accounts[0].balanced
    assert st.month == "2026-10"
    assert (t.date, t.value_date, t.amount, t.direction, t.currency) == (
        date(2026, 10, 25),
        "2026-10-26",
        Decimal("250.00"),
        "debit",
        "RON",
    )
    assert t.counterparty == "Example Supplier SRL" and t.counterparty_iban == SUPPLIER_IBAN
    assert t.description == "Invoice ABC123 ABC123" and t.ref == "bank-1|end-bank-1"
    assert statements.tx_key(t) == statements.tx_key(parse(tmp_path, xml(version)).txs[0])
    assert statements.Tx.from_dict(t.to_dict()) == t


def test_batch(tmp_path):
    st = parse(tmp_path, xml(entries=entry(detail("100", "a") + detail("150", "b"))))
    assert [t.amount for t in st.txs] == [Decimal(100), Decimal(150)]
    assert st.accounts[0].balanced and len({statements.tx_key(t) for t in st.txs}) == 2


@pytest.mark.parametrize(
    "entries,why",
    [
        (entry(detail("100", "a") + detail("151", "b")), "does not add up"),
        (entry(detail("249")), "does not add up"),
        (entry().replace("<NtryDtls>", "<NtryDtls><Btch><NbOfTxs>2</NbOfTxs></Btch>"), "incomplete"),
        (entry(details='<Btch><TtlAmt Ccy="RON">250</TtlAmt></Btch>'), "incomplete"),  # a total alone settles nothing
        (entry(detail("100.005", "a") + detail("149.995", "b")), "fractional precision"),
    ],
)
def test_a_batch_that_isnt_complete_to_the_cent_is_refused(tmp_path, entries, why):
    with pytest.raises(statements.StatementError, match=why):
        parse(tmp_path, xml(entries=entries))


def test_credit_foreign_currency_pending(tmp_path):
    st = parse(tmp_path, xml(currency="EUR", entries=entry(flow="CRDT") + entry(status="PDNG"), closing="1250"))
    assert st.accounts[0].balanced and len(st.txs) == 1
    assert st.txs[0].direction == "credit" and st.txs[0].currency == "EUR"


def test_credit_counterparty_and_status_choice(tmp_path):
    data = xml(8, entries=entry(flow="CRDT", status="<Cd>BOOK</Cd>"), closing="1250")
    data = data.replace(b"CdtrAcct", b"DbtrAcct").replace(b"<Cdtr>", b"<Dbtr>").replace(b"</Cdtr>", b"</Dbtr>")
    st = parse(tmp_path, data)
    assert st.accounts[0].balanced
    assert st.txs[0].counterparty_iban == SUPPLIER_IBAN


@pytest.mark.parametrize(
    "data",
    [
        xml(iban="RO94BTRLRONCRT0000000002"),
        b'<!DOCTYPE x [<!ENTITY bomb "boom">]>' + xml(),
        xml(9),
        b"<Invoice/>",
        b"random",
    ],
)
def test_refused(tmp_path, data):
    with pytest.raises(statements.StatementError):
        parse(tmp_path, data)


def test_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(camt, "MAX_XML", 20)
    with pytest.raises(statements.StatementError, match="too large"):
        parse(tmp_path, xml())


@pytest.mark.parametrize(
    "old,new",
    [
        (b"OPBD", b"OTHER"),
        (b"CLBD", b"OTHER"),
        (b"<CdtDbtInd>DBIT</CdtDbtInd>", b"<CdtDbtInd>DBIT</CdtDbtInd><CdtDbtInd>CRDT</CdtDbtInd>"),
        (b"<Sts>BOOK</Sts>", b"<Sts>BOOK</Sts><Sts>BOOK</Sts>"),
        (b"<BookgDt>", b"<BookgDt><Dt>2026-10-25</Dt></BookgDt><BookgDt>"),
        (b"<Ntry>", b"<Ntry><RvslInd>true</RvslInd>"),
        (b"<TxDtls>", b"<TxDtls><RtrInf><Rsn><Cd>AC01</Cd></Rsn></RtrInf>"),
        (b"<Ntry>", b"<Ntry><Chrgs/>"),
        (b"250.00", b"0.00"),
        (b"250.00", b"250.001"),
        (b"bank-1", b"NOTPROVIDED"),
        (b"<Dt>2026-10-01</Dt>", b"<Dt>2026-10-02</Dt>"),
    ],
)
def test_refusal_shapes(tmp_path, old, new):
    data = xml().replace(old, new)
    if old == b"bank-1":
        data = data.replace(b"end-NOTPROVIDED", b"NOTPROVIDED").replace(b"entry-1", b"NOTPROVIDED")
    with pytest.raises(statements.StatementError):
        parse(tmp_path, data)


@pytest.mark.parametrize("field", ["Sts", "BookgDt", "CdtDbtInd"])
def test_required_fields_before_book_filter(tmp_path, field):
    pending = re.sub("<" + field + ">.*?</" + field + ">", "", entry(status="PDNG"))
    with pytest.raises(statements.StatementError, match="entry field"):
        parse(tmp_path, xml(entries=pending, closing="1000"))


@pytest.mark.parametrize("closing", ["751", "0"])
def test_mismatch_never_settles(tmp_path, ledger, closing):
    add_invoice(ledger)
    with pytest.raises(statements.StatementError, match="balances do not add up"):
        parse(tmp_path, xml(closing=closing))
    assert not ledger.used_txs()


def test_untrustworthy_credit_and_two_accounts(tmp_path, ledger):
    ledger.add_issued(
        dict(
            message_id="credit-1",
            company="NORD",
            raw_path="fake",
            status="open",
            doc_type="invoice",
            number="ABC123",
            issue_date="2026-10-10",
            currency="RON",
            total="250.00",
            client_name="Example Supplier SRL",
            client_cui="66666666",
        )
    )
    data = xml(entries=entry(flow="CRDT"), closing="1251").replace(b"Cdtr", b"Dbtr")
    with pytest.raises(statements.StatementError, match="balances do not add up"):
        parse(tmp_path, data)
    combined = data.replace(
        b"</BkToCstmrStmt>", section(xml(currency="EUR", entries="", closing="1000")) + b"</BkToCstmrStmt>"
    )
    with pytest.raises(statements.StatementError, match="balances do not add up"):
        parse(tmp_path, combined)
    assert ledger.issued("credit-1")["status"] == "open"


PERIOD = b"<FrToDt><FrDt>2026-10-01</FrDt><ToDt>2026-10-31</ToDt></FrToDt>"
PERIOD_TM = b"<FrToDt><FrDtTm>2026-10-01T00:00:00</FrDtTm><ToDtTm>2026-10-31T23:59:59</ToDtTm></FrToDt>"


@pytest.mark.parametrize(
    "old,new,period",
    [
        (b"<BookgDt><Dt>2026-10-25</Dt></BookgDt>", b"<BookgDt><Dt>2026-11-03</Dt></BookgDt>", PERIOD),
        (b"<BookgDt><Dt>2026-10-25</Dt></BookgDt>", b"<BookgDt><DtTm>2026-10-31T22:30:00Z</DtTm></BookgDt>", PERIOD),
        (b"<Dt>2026-10-01</Dt>", b"<Dt>2026-09-30</Dt>", PERIOD),
        (b"<Dt>2026-10-01</Dt>", b"<Dt>2026-09-30</Dt>", PERIOD_TM),  # the reported period wins over the opening's day
        (b"<Dt>2026-10-31</Dt>", b"<Dt>2026-11-01</Dt>", PERIOD),
        (b"2026-10-", b"2026-09-", PERIOD),
    ],
)
def test_dates_within_declared_period(tmp_path, old, new, period):
    data = xml().replace(old, new).replace(b"2026-09-31", b"2026-09-30").replace(b"<Stmt>", b"<Stmt>" + period)
    with pytest.raises(statements.StatementError, match="outside"):
        parse(tmp_path, data)


def test_period_and_bucharest(tmp_path):
    data = xml().replace(
        b"<Stmt>", b"<Stmt><FrToDt><FrDtTm>2026-09-30T21:00:00Z</FrDtTm><ToDtTm>2026-10-31T21:59:59Z</ToDtTm></FrToDt>"
    )
    assert parse(tmp_path, data).month == "2026-10"
    data = xml().replace(
        b"<BookgDt><Dt>2026-10-25</Dt></BookgDt>", b"<BookgDt><DtTm>2026-10-24T22:00:00Z</DtTm></BookgDt>"
    )
    assert parse(tmp_path, data).txs[0].date == date(2026, 10, 25)


def test_prcd_and_duplicate_opening(tmp_path):
    """A bank's previous closing balance (PRCD) opens the month, dated its first day or the last day before it."""
    prcd = xml().replace(b"<Cd>OPBD</Cd>", b"<Cd>PRCD</Cd>")
    assert parse(tmp_path, prcd).accounts[0].balanced
    day_before = prcd.replace(b"<Dt>2026-10-01</Dt>", b"<Dt>2026-09-30</Dt>", 1).replace(b"<Stmt>", b"<Stmt>" + PERIOD)
    assert parse(tmp_path, day_before).month == "2026-10"
    with pytest.raises(statements.StatementError, match="outside"):  # two days early is outside
        parse(tmp_path, day_before.replace(b"<Dt>2026-09-30</Dt>", b"<Dt>2026-09-29</Dt>", 1))
    data = xml()
    bal = data[data.index(b"<Bal>") : data.index(b"</Bal>") + 6]
    with pytest.raises(statements.StatementError):  # PRCD and OPBD both: two openings
        parse(tmp_path, data.replace(b"</Acct>", b"</Acct>" + bal.replace(b"OPBD", b"PRCD")))


@pytest.mark.parametrize("explicit_currency", [True, False])
def test_duplicate_account_refused(tmp_path, explicit_currency):
    data = xml().replace(b"</BkToCstmrStmt>", section(xml().replace(b"bank-1", b"bank-2")) + b"</BkToCstmrStmt>")
    if not explicit_currency:
        data = data.replace(b"<Ccy>RON</Ccy>", b"")
    with pytest.raises(statements.StatementError, match="Duplicate CAMT account"):
        parse(tmp_path, data)


def test_accounts_months_differ(tmp_path):
    other = xml(currency="EUR").replace(b"2026-10-", b"2026-12-")
    with pytest.raises(statements.StatementError, match="same calendar month"):
        parse(tmp_path, xml().replace(b"</BkToCstmrStmt>", section(other) + b"</BkToCstmrStmt>"))


def test_foreign_amount(tmp_path):
    data = xml().replace(
        b'<TxAmt><Amt Ccy="RON">250.00</Amt></TxAmt>',
        b'<TxAmt><Amt Ccy="EUR">50.00</Amt></TxAmt><CntrValAmt><Amt Ccy="RON">250.00</Amt></CntrValAmt><InstdAmt><Amt Ccy="EUR">50.00</Amt></InstdAmt>',
    )
    t = parse(tmp_path, data).txs[0]
    assert (t.amount, t.orig_amount, t.orig_currency) == (Decimal("250"), Decimal("50"), "EUR")


@pytest.mark.parametrize(
    "code,kind",
    [
        *((f"<Prtry><Cd>{c}</Cd></Prtry>", statements.BANK_FEE) for c in ("CHRG", "COMM", "FEES", "COMI", "COMT")),
        *(
            (f"<Domn><Cd>PMNT</Cd><Fmly><Cd>ICDT</Cd><SubFmlyCd>{c}</SubFmlyCd></Fmly></Domn>", statements.BANK_FEE)
            for c in ("CHRG", "COMM")
        ),
        ("<Domn><Cd>PMNT</Cd><Fmly><Cd>ICDT</Cd><SubFmlyCd>INTR</SubFmlyCd></Fmly></Domn>", statements.INTEREST),
        # a fee is the subfamily alone: another family, or no domain code at all, is still a fee
        ("<Domn><Fmly><Cd>ACCB</Cd><SubFmlyCd>CHRG</SubFmlyCd></Fmly></Domn>", statements.BANK_FEE),
        ("<Domn><Fmly><Cd>ICDT</Cd><SubFmlyCd>CHRG</SubFmlyCd></Fmly></Domn>", statements.BANK_FEE),
    ],
)
def test_coded_fee_never_settles(tmp_path, ledger, code, kind):
    add_invoice(ledger)
    st = parse(tmp_path, xml().replace(b"<Ntry>", f"<Ntry><BkTxCd>{code}</BkTxCd>".encode()))
    assert (st.txs[0].category, statements.classify_tx(st.txs[0])) == (kind, kind)
    assert efactura.match_statement(ledger, st) == [] and not ledger.used_txs()


@pytest.mark.parametrize("encoding", ["iso-8859-1", "windows-1250"])
def test_declared_encoding(tmp_path, encoding):
    data = xml().decode().replace("Example Supplier", "Exemplu Café")
    assert parse(tmp_path, ('<?xml version="1.0" encoding="' + encoding + '"?>' + data).encode(encoding)).txs
    with pytest.raises(statements.StatementError, match="UTF-8"):
        parse(tmp_path, data.encode(encoding))


def test_normalized_iban_duplicate_keys(tmp_path):
    iban = companies.get("NORD").ron_iban
    assert parse(tmp_path, xml(iban=" ".join(iban.lower()))).accounts[0].iban == iban
    with pytest.raises(statements.StatementError, match="identities"):
        parse(tmp_path, xml(entries=entry() + entry(), closing="500"))


def test_retired_account(tmp_path, monkeypatch):
    owners = companies.all()
    co = owners["NORD"]
    retired = co.ron_iban
    owners["NORD"] = replace(co, accounts=(companies.Account(retired, "RON", "bt", retired="2026-09-30"),))
    set_firms(owners)
    assert parse(tmp_path, xml(iban=retired)).accounts[0].iban == retired


@pytest.mark.parametrize("country,kind", [("RO", statements.NEEDS_INVOICE), ("US", statements.NEEDS_INVOICE)])
def test_the_merchants_country_comes_from_its_address(tmp_path, country, kind):
    data = xml().replace(SUPPLIER_IBAN.encode(), b"DE00AAAA0000000000000001")
    t = parse(tmp_path, data.replace(b"</Cdtr>", f"<PstlAdr><Ctry>{country}</Ctry></PstlAdr></Cdtr>".encode(), 1)).txs[
        0
    ]
    assert (t.country, statements.classify_tx(t)) == (country, kind)


# ---- matching: one transaction settles one item, never when in doubt


def test_reconcile_and_idempotence(tmp_path, ledger):
    add_invoice(ledger)
    st = parse(tmp_path, xml())
    assert efactura.match_statement(ledger, st) == ["1"]
    assert efactura.match_statement(ledger, parse(tmp_path, xml())) == []
    assert len(ledger.used_txs()) == 1


def test_ambiguous_none_settles(tmp_path, ledger):
    add_invoice(ledger, "1", "ONE123")
    add_invoice(ledger, "2", "TWO456")
    st = parse(
        tmp_path,
        xml(
            entries=entry(
                detail(remit="Payment").replace("<Strd><CdtrRefInf><Ref>ABC123</Ref></CdtrRefInf></Strd>", "")
            )
        ),
    )
    assert efactura.match_statement(ledger, st) == [] and not ledger.used_txs()


def test_camt_reservation_race(tmp_path, ledger):
    add_invoice(ledger, "1", "ONE123")
    add_invoice(ledger, "2", "TWO456")
    key = statements.tx_key(parse(tmp_path, xml()).txs[0])
    gate, won = threading.Barrier(2), []

    def reserve(mid):
        connection = Ledger()
        try:
            gate.wait()
            won.append(connection.mark_efactura_paid(mid, "CAMT proof", key))
        finally:
            connection.close()

    threads = [threading.Thread(target=reserve, args=(mid,)) for mid in ("1", "2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(won) == [False, True]
    assert len(ledger.used_txs()) == 1


# ---- importing: who may import a CAMT, once, journalled


def test_intake_duplicate_and_archive(tmp_path, ledger):
    p = write(tmp_path, complete())
    first = recon.ingest_file(ledger, p, operator=True)
    assert first["status"] == "added" and first["balanced"]
    assert recon.ingest_file(ledger, p, operator=True)["status"] == "duplicate"
    docs = ledger.documents(company="NORD", kind="statement")
    assert len(docs) == 1 and archive.target(docs[0])[2].endswith(".xml")
    assert p.exists()  # source retained, never lost during a publication


def test_partial_month_not_stored(tmp_path, ledger):
    result = recon.ingest_file(ledger, write(tmp_path, xml().replace(b"2026-10-01", b"2026-10-02")), operator=True)
    assert result["status"] == "refused" and "full calendar month" in result["errors"][0]
    assert not ledger.documents(kind="statement")


def test_forged_camt_receipt_link_settles_nothing(ledger, server):
    add_invoice(ledger)
    token = upload_links.create(ledger, "NORD", "Test")["path"]
    assert post(server, token, [("forged.xml", xml())])[0] == 415
    assert not ledger.documents(kind="statement") and not ledger.used_txs()
    assert ledger.efactura_rows(company="NORD", status="open")


def test_forged_camt_mail_settles_nothing(tmp_path, ledger):
    add_invoice(ledger)
    sender = companies.get("NORD").statement_senders[0]
    msg = imapread.Message(
        "a",
        "synthetic@bank.example",
        sender,
        sender.lower(),
        "s",
        "2026-10-01",
        (imapread.Attachment("xml", complete(), "allmerge.xml"),),
    )
    assert mail_invoices.handle(ledger, msg) == "statement:none"  # a statement by mail is only ever its PDF
    # A disguised attachment also cannot reach the operator-only importer from sync.
    assert recon.ingest_file(ledger, write(tmp_path, complete(), "allmerge.pdf"))["status"] == "refused"
    assert not ledger.documents(kind="statement") and not ledger.used_txs()


@pytest.mark.parametrize("order", ["pdf-camt", "camt-pdf", "camt-camt"])
def test_overlapping_document_refused(tmp_path, ledger, monkeypatch, order):
    p = write(tmp_path, complete(), "one.xml")
    pdf = fake_pdf(monkeypatch, tmp_path)
    if order == "pdf-camt":
        assert recon.ingest_file(ledger, pdf)["status"] == "added"
        result = recon.ingest_file(ledger, p, operator=True)
    else:
        assert statement_intake.ingest(ledger, p)["status"] == "added"
        if order == "camt-camt":
            p.write_bytes(complete().replace(b"bank-1", b"different-export"))
            result = recon.ingest_file(ledger, p, operator=True)
        else:
            result = recon.ingest_file(ledger, pdf)
    assert result["status"] == "refused" and "already imported" in result["errors"][0]
    assert len(ledger.documents(kind="statement")) == 1
    assert not ledger.used_txs()


def test_source_pdf_duplicate_reruns_matching(ledger, tmp_path, monkeypatch):
    p = fake_pdf(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(recon, "auto_mark_paid", lambda *a: calls.append(1) or [])
    assert recon.ingest_file(ledger, p)["status"] == "added"
    assert recon.ingest_file(ledger, p)["status"] == "duplicate"
    assert len(calls) == 2 and len(ledger.documents(kind="statement")) == 1


def test_decrypted_name_collision_preserves_bytes(ledger, tmp_path, monkeypatch, home):
    p = fake_pdf(monkeypatch, tmp_path)
    dest = home / "NORD/2026-10/extrase/decrypted.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"%PDF-1.7 earlier orphan")
    r = recon.ingest_file(ledger, p)
    assert r["status"] == "added" and r["unprotected"] != str(dest)
    assert dest.read_bytes() == b"%PDF-1.7 earlier orphan"


@pytest.mark.parametrize("failure", ["symlink-storage", "disk", "symlink-source"])
def test_intake_clear_refusal(ledger, tmp_path, monkeypatch, home, failure):
    p = write(tmp_path, complete())
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    if failure == "symlink-storage":
        (home / "NORD").symlink_to(elsewhere, target_is_directory=True)
    elif failure == "disk":

        @contextmanager
        def full(*a):
            raise receipt_upload.Refused(507)
            yield

        monkeypatch.setattr(statement_intake, "directory", full)
    else:
        alias = tmp_path / "alias.xml"
        alias.symlink_to(p)
        p = alias
    r = recon.ingest_file(ledger, p, operator=True)
    assert r["status"] == "refused" and r["errors"][0]
    assert not list(elsewhere.iterdir()) and not ledger.documents(kind="statement")


def crash_ingest(ledger, p, monkeypatch, target, name):
    """Import `p` with `target.name` failing once, as a process dying at that step."""
    with monkeypatch.context() as m:
        m.setattr(target, name, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError):
            statement_intake.ingest(ledger, p)


@pytest.mark.parametrize("step", ["publication", "ledger"])
def test_journal_recovers_before_publication(tmp_path, ledger, monkeypatch, caplog, step):
    """A crash before the stored copy is published or before the ledger records it is finished once, on the next
    run, even beside a journal that can't be read (kept, and said, for the operator)."""
    ledger.set_meta("statement_import:bad", "{bad json")
    target, name = (statement_intake.os, "replace") if step == "publication" else (ledger, "add_document")
    crash_ingest(ledger, write(tmp_path, complete()), monkeypatch, target, name)
    statement_intake.recover(ledger)
    assert "Statement recovery failed" in caplog.text and ledger.get_meta("statement_import:bad")
    assert len(ledger.documents(kind="statement")) == 1
    statement_intake.recover(ledger)
    assert len(ledger.documents(kind="statement")) == 1


@pytest.mark.parametrize("corruption", ["bytes", "missing", "symlink"])
def test_failed_recovery_source_is_reported(tmp_path, ledger, monkeypatch, caplog, corruption):
    p = write(tmp_path, complete())
    crash_ingest(ledger, p, monkeypatch, ledger, "add_document")
    row = ledger.db.execute("SELECT key,value FROM meta WHERE key LIKE 'statement_import:%'").fetchone()
    stored = tmp_path / "NORD/documents/inbox/2026-10-01" / (json.loads(row["value"])["sha"] + ".xml")
    if corruption == "bytes":
        stored.write_bytes(b"changed")
    else:
        stored.unlink()
        if corruption == "symlink":
            stored.symlink_to(p)
    statement_intake.recover(ledger)
    assert "Statement recovery failed" in caplog.text
    assert ledger.get_meta(row["key"]) and not ledger.documents(kind="statement")


# ---- the command line


def test_cmd_statement_infers_company(tmp_path, ledger, capsys):
    p = write(tmp_path, complete(), "statement.xml")
    assert cli.cmd_statement(Namespace(file=str(p), company=None, transactions=False), ledger) == 0
    assert json.loads(capsys.readouterr().out)["company"] == "NORD"


def test_failed_recovery_does_not_block_sync(ledger, monkeypatch):
    ledger.set_meta("statement_import:broken", "{broken")
    calls = []

    def stage(name, result):
        def run(*args, **kwargs):
            calls.append(name)
            return result

        return run

    monkeypatch.setattr(mail_invoices, "run", stage("mail", {"accounts": []}))
    monkeypatch.setattr(cli.archive, "archive_all", stage("archive", []))
    monkeypatch.setattr(cli.pay, "prepare_files", stage("payment files", []))
    monkeypatch.setattr(cli.pay, "refresh_to_pay", stage("refresh", []))
    monkeypatch.setattr(cli.recon, "write_report", stage("report", ("fake", {})))
    monkeypatch.setattr(cli.pack, "ready", stage("pack ready", False))
    assert cli.cmd_sync(Namespace(), ledger) == 0
    assert {"mail", "payment files", "pack ready", "report"} <= set(calls)
    # Setări shows the last automatic check and when mail was read: the tick's sync records both (no agent run does),
    # in shadow mode too (this ledger is not live)
    assert ledger.get_meta("last_run_at") and ledger.get_meta("last_check_at")


def test_a_sync_that_fails_moves_only_what_it_did(ledger, monkeypatch):
    """Mail unreadable: neither time moves. Mail read, a later step failing: only the mail time moves."""
    ledger.set_meta("last_run_at", "old-run")
    ledger.set_meta("last_check_at", "old-check")
    monkeypatch.setattr(mail_invoices, "run", lambda L, today: (_ for _ in ()).throw(OSError("mail down")))
    with pytest.raises(OSError):
        cli.cmd_sync(Namespace(), ledger)
    assert (ledger.get_meta("last_run_at"), ledger.get_meta("last_check_at")) == ("old-run", "old-check")
    monkeypatch.setattr(mail_invoices, "run", lambda L, today: {"accounts": []})
    monkeypatch.setattr(cli.pay, "prepare_files", lambda *a, **k: (_ for _ in ()).throw(OSError("payment files down")))
    with pytest.raises(OSError):
        cli.cmd_sync(Namespace(), ledger)
    assert ledger.get_meta("last_run_at") == "old-run" and ledger.get_meta("last_check_at") != "old-check"


@pytest.mark.parametrize("batch", [False, True])
def test_only_individual_account_servicer_references_identify_bank_copies(tmp_path, batch):
    details = detail(ref="a") + (detail(ref="b") if batch else "")
    details = re.sub(r"<AcctSvcrRef>.*?</AcctSvcrRef>", "", details)
    st = parse(
        tmp_path,
        xml(entries=entry(details, amount="500.00" if batch else "250.00"), closing="500.00" if batch else "750.00"),
    )
    assert [t.bank_reference for t in st.txs] == (["", ""] if batch else ["entry-1"])


@pytest.mark.parametrize("legacy", [False, True])
def test_offline_scale_tick_reads_synthetic_inputs_without_money_changes(ledger, legacy):
    build(ledger, 100)
    seed_mail_questions(ledger, 3)
    measured = tick_benchmark.run(ledger, datetime(2026, 10, 27, 9, 30), legacy_registry=legacy, mail_count=1)
    assert measured["mode"] == "shadow"
    assert measured["mail_messages"] == 1 and measured["receipt_extractions"] == 3
    assert measured["statement_rows"] > 0 and measured["reports"] == 4
    assert measured["external_connections"] == 0 and measured["pdf_subprocesses"] == 4
