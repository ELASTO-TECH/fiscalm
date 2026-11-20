"""What a statement's content is and what its lines are. Synthetic data only."""

from datetime import date
from decimal import Decimal

import pytest
from test_dashboard import BT_HEAD
from test_efactura import _supplier_twins

from fiscalm import recon
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura


@pytest.mark.parametrize("prefix", [b"", b"\n", b"\xef\xbb\xbf"])
def test_the_content_decides_and_a_prefixed_pdf_header_is_refused(tmp_path, monkeypatch, prefix):
    path = tmp_path / "bank.xml"  # the name says XML: the bytes decide
    path.write_bytes(prefix + b"%PDF-1.7")
    monkeypatch.setattr(statements, "parse_pdf", lambda path, co: "pdf")
    if not prefix:
        assert statements.parse(path, "NORD") == "pdf"
        return
    with pytest.raises(statements.StatementError, match="Unsupported statement"):
        statements.parse(path, "NORD")


@pytest.mark.parametrize(
    "line,currency",
    [
        ("RON Cod IBAN: ", "IBAN missing"),
        ("EUR  Cod IBAN: RO00BTRLEURCRT0000000001", "EUR"),  # 2024 on: the currency starts the line
        ("Extras numarul 6        EUR       Cod IBAN: RO00BTRLEURCRT0000000001", "EUR"),  # 2021-2023
        ("Extras numarul 6        RON       Cod IBAN: RO00BTRLEURCRT0000000001", "contradicts its IBAN"),
        ("Extras numarul 6                  Cod IBAN: RO00BTRLEURCRT0000000001", "currency missing"),
    ],
)
def test_a_bt_pdf_account_line_gives_its_iban_and_currency_or_is_refused(line, currency):
    text = f"CONT 123 Valuta\n{line}\n"
    if len(currency) == 3:
        (acct,) = statements.parse_text(text, "NORD").accounts
        assert (acct.iban, acct.currency) == ("RO00BTRLEURCRT0000000001", currency)
        return
    with pytest.raises(statements.StatementError, match=currency):
        statements.parse_text(text, "NORD")


@pytest.mark.parametrize("category", [statements.BANK_FEE, statements.INTEREST])
@pytest.mark.parametrize("twins", [False, True])
def test_fee_cannot_settle_or_be_ambiguous(ledger, twins, category):
    ids, tx, st = _supplier_twins(ledger)
    tx.title = "Comision"
    tx.category = category
    if not twins:
        ledger.mark_skipped(ids[1], "synthetic")
    assert recon.auto_mark_paid(ledger, st) == []
    efactura.note_ambiguous(ledger, st)
    assert not ledger.ambiguous()
    assert ledger.payment(ids[0])["status"] == "pending"


def test_a_treasury_advance_to_anyone_is_an_advance(monkeypatch):
    """Review (copy): "AVANS TREZORERIE" to an employee lands with the owner's advances, so the report's line for them
    names no recipient ("Transfer din contul firmei, de decontat")."""

    def tx(counterparty, desc):
        return statements.Tx(
            date=date(2026, 10, 5),
            title="Transfer",
            description=desc,
            direction="debit",
            amount=Decimal("300.00"),
            currency="RON",
            iban="RO00TEST0000000000000001",
            counterparty=counterparty,
            counterparty_iban="RO00TEST0000000000000009",
        )

    assert statements.classify_tx(tx("ION ANGAJAT", "AVANS TREZORERIE deplasare")) == statements.OWNER_ADVANCE
    assert statements.classify_tx(tx("ANA POP", "transfer")) == statements.DOMESTIC  # a name is never a setting


def test_bt_pdf_reads_bts_words_and_the_rules_none():
    rows = [
        "CONT CURENT NORD                     Valuta",
        "RON  Cod IBAN: RO24BTRLRONCRT0000000001",
        BT_HEAD,
        "05/10/2026  Comision administrare cont             15.00",
        "            REF: F1",
        "07/10/2026  Plata la POS                           30.00",
        "            TID: 1 MAGAZIN NORD ROM 12345678 RRN",
        "08/10/2026  Plata la POS non-BT                    40.00",
        "            TID: 2 SHOP INC USA 12345678 RRN",
    ]
    st = statements.parse_text("\n".join(rows) + "\n", "NORD")
    assert [statements.classify_tx(t) for t in st.txs] == [
        statements.BANK_FEE,
        statements.NEEDS_INVOICE,
        statements.NEEDS_INVOICE,
    ]
    for t in st.txs:  # the same lines from a format that said nothing: BT's words alone decide nothing
        t.category, t.country = "", ""
    assert {statements.classify_tx(t) for t in st.txs} == {statements.NEEDS_INVOICE}
