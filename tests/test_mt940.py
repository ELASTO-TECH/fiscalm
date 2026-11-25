"""MT940 statements: the same rules as CAMT. Synthetic data only."""

import json
from datetime import date
from decimal import Decimal

import pytest

from fiscalm import recon
from fiscalm.api import state as api
from fiscalm.bank import statements, upload as su
from fiscalm.bank.mt940 import looks_like, parse_mt940

RON, EUR = "RO24BTRLRONCRT0000000001", "RO71BTRLEURCRT0000000001"
SUPPLIER = "RO49AAAA1B31007593840000"


def block(iban=RON, ccy="RON", opening="C260930RON1000,00", closing="C261031RON750,00", lines=None, ref="STMT1"):
    lines = (
        lines
        if lines is not None
        else [
            ":61:2610251025D250,00NTRFNONREF//bank-1",
            f":86:?20Factura ABC123?31{SUPPLIER}?32Example Supplier?33 SRL",
        ]
    )
    return "\r\n".join(
        [
            f":20:{ref}",
            f":25:{iban}",
            ":28C:1/1",
            f":60F:{opening.replace('RON', ccy)}",
            *lines,
            f":62F:{closing.replace('RON', ccy)}",
            "-",
        ]
    ).encode()


def test_a_month_reads_like_camt():
    assert looks_like(block()) and not looks_like(b"<Document/>") and not looks_like(b"%PDF-")
    st = parse_mt940(block(), "NORD")
    (a,) = st.accounts
    assert (st.company, st.period_start, st.period_end, a.iban, a.currency, a.balanced) == (
        "NORD",
        date(2026, 10, 1),
        date(2026, 10, 31),
        RON,
        "RON",
        True,
    )
    (t,) = a.txs
    assert (t.date, t.direction, t.amount, t.ref, t.counterparty, t.counterparty_iban, t.description) == (
        date(2026, 10, 25),
        "debit",
        Decimal("250.00"),
        "NONREF//bank-1",
        "Example Supplier SRL",
        SUPPLIER,
        "Factura ABC123",
    )
    assert statements.tx_key(t) == statements.tx_key(parse_mt940(block(), "NORD").txs[0])  # stable identity


def test_daily_statements_in_one_file_chain_into_the_month():
    first = block(closing="C261015RON900,00", lines=[":61:2610101010D100,00NTRFNONREF", ":86:Taxe"])
    second = block(opening="C261015RON900,00", closing="C261031RON650,00", ref="STMT2")
    st = parse_mt940(first + b"\r\n" + second, "NORD")
    assert len(st.txs) == 2 and st.accounts[0].balanced


@pytest.mark.parametrize(
    "data,why",
    [
        (block(closing="C261030RON750,00"), "full calendar month"),
        (block(opening="C261005RON1000,00"), "full calendar month"),
        (block(iban="RO94BTRLRONCRT0000000002"), "does not belong"),  # STUDIO9's account
        (block(lines=[":61:2610251025RD250,00NTRFNONREF", ":86:x"]), "reversal"),
        (block(lines=[":61:2610251025D0,00NTRFNONREF", ":86:x"]), "zero"),
        (block(lines=[":61:2611051105D250,00NTRFNONREF", ":86:x"]), "outside the month"),
        (block() + b"\r\n" + block(opening="C261031RON1,00", closing="C261031RON1,00", ref="X", lines=[]), "chain"),
        (b":20:X\r\n:25:" + RON.encode() + b"\r\n-", "needs one"),
    ],
)
def test_what_needs_judgement_is_refused(data, why):
    with pytest.raises(statements.StatementError, match=why):
        parse_mt940(data, "NORD")


@pytest.mark.parametrize(
    "data",
    [
        block(closing="C261031RON700,00"),
        # each daily statement must add up on its own: a day 50 short and the next 50 over is two errors, not none
        block(closing="C261015RON900,00", lines=[":61:2610101010D150,00NTRFNONREF", ":86:x"])
        + b"\r\n"
        + block(
            opening="C261015RON900,00",
            closing="C261031RON650,00",
            ref="S2",
            lines=[":61:2610251025D200,00NTRFNONREF", ":86:x"],
        ),
    ],
)
def test_totals_that_dont_add_up_are_refused(data):
    with pytest.raises(statements.StatementError, match="do not add up"):
        parse_mt940(data, "NORD")


def test_intermediate_balances_only_or_a_gap_between_days_are_refused():
    inter = block().replace(b":60F:", b":60M:").replace(b":62F:", b":62M:")
    with pytest.raises(statements.StatementError, match="final"):
        parse_mt940(inter, "NORD")
    first = block(closing="C261010RON1000,00", lines=[])
    second = block(opening="C261025RON1000,00", ref="S2")  # 11 to 24 missing
    with pytest.raises(statements.StatementError, match="chain"):
        parse_mt940(first + b"\r\n" + second, "NORD")
    middle = block(closing="C261015RON1000,00", lines=[]).replace(b":62F:", b":62M:")
    end = block(opening="C261015RON1000,00", ref="S3").replace(b":60F:", b":60M:")
    assert parse_mt940(middle + b"\r\n" + end, "NORD").accounts[0].balanced  # intermediate in between is fine


def test_another_companys_statement_is_refused_when_one_is_named():
    with pytest.raises(statements.StatementError, match="does not belong"):
        parse_mt940(block(), "STUDIO9")


def complete_mt940():
    return block() + b"\r\n" + block(iban=EUR, ccy="EUR", closing="C261031EUR1000,00", lines=[], ref="E")


def test_the_months_open_debits_are_named_by_their_words_never_the_booking_code(ledger):
    lines = [
        ":61:2610031003D100,00NTRFNONREF//bank-1",
        ":86:?20Chirie octombrie?32Exemplu Imobiliare SRL",  # a name, no IBAN: a transfer that needs its invoice
        ":61:2610061006D40,00NTRFNONREF//bank-2",
        f":86:?20POS PLATA CARD BENZINARIA EXEMPLU?21{SUPPLIER}",  # no ?32: its own words name it, IBAN masked
        ":61:2610071007D15,00NCHGNONREF//bank-3",
        ":86:?20Comision administrare cont",  # the bank's fee: needs no document
    ]
    data = block(closing="C261031RON845,00", lines=lines) + b"\r\n" + block(iban=EUR, ccy="EUR", lines=[], ref="E")
    su.accept(ledger, "NORD", "bt", data.replace(b"C261031EUR750,00", b"C261031EUR1000,00"), "text/plain")
    view = api._month_view(ledger, "NORD", "2026-10")
    assert [(r["who"], r["kind"]) for r in view["missing"]] == [
        ("Exemplu Imobiliare SRL", "transfer"),
        ("POS PLATA CARD BENZINARIA EXEMPLU " + statements.masked(SUPPLIER), "card"),
        ("Comision administrare cont", "transfer"),
    ]


def test_an_mt940_is_imported_by_the_operator_and_through_an_upload(ledger, tmp_path):
    p = tmp_path / "extras.sta"
    p.write_bytes(complete_mt940())
    assert recon.ingest_file(ledger, p)["status"] == "refused"  # mail and links: never (unsigned, like CAMT)
    with pytest.raises(su.Refused) as e:  # the upload must hold every account of its bank
        su.accept(ledger, "NORD", "", block(), "text/plain")
    assert e.value.message == su.incomplete(["EUR 0001"])
    r = su.accept(ledger, "NORD", "bt", complete_mt940(), "text/plain")
    assert (r["status"], r["message"]) == ("added", su.ADDED)
    (doc,) = ledger.documents(kind="statement")
    assert doc["path"].endswith(".sta") and sorted(json.loads(doc["accounts"])) == sorted([RON, EUR])
    assert su.accept(ledger, "NORD", "", complete_mt940(), "application/octet-stream")["message"] == su.DUPLICATE


@pytest.mark.parametrize(
    "kind,category",
    [("NCHG", statements.BANK_FEE), ("NCOM", statements.BANK_FEE), ("NINT", statements.INTEREST), ("NTRF", "")],
)
def test_the_transaction_type_tells_fees_and_interest(kind, category):
    lines = [f":61:2610251025D250,00{kind}NONREF//bank-1", ":86:?20Linie sintetica"]
    assert parse_mt940(block(lines=lines), "NORD").txs[0].category == category


def test_the_booking_text_is_the_title_and_stays_out_of_the_key():
    lines = [":61:2610251025C250,00NTRFNONREF//bank-1", ":86:?00POS refund?20Merchant refund"]
    tx = parse_mt940(block(lines=lines, closing="C261031RON1250,00"), "NORD").txs[0]
    assert (tx.title, tx.description) == ("POS refund", "Merchant refund")
