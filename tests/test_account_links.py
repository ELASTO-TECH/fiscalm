"""Accounts, payments, statements and invoices stay linked: a payment file names the account it debits and only a
debit from that account pays it; the file waits for that account's statement; a collection records where it landed.
Synthetic data only."""

import dataclasses
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from firms import set_firm
from payment_cases import D0, _op, download_batch

from fiscalm import companies, recon
from fiscalm.bank import intake as statement_intake, statements
from fiscalm.ledger import Ledger
from fiscalm.payments import btgo, engine as pay
from fiscalm.payments.orders import PaymentFileError

pytestmark = pytest.mark.usefixtures("live")  # payment files are made only in Live

ING = "RO00INGB0000000000000009"


def nord():
    return companies.get("NORD")


def debit(item_id, ledger, iban, day=date(2026, 10, 27), ref="R1"):
    p = next(r for r in ledger.payments(company="NORD") if r["item_id"] == item_id)
    return statements.Tx(
        date=day,
        title="Transfer",
        description=p["details"] or "",
        direction="debit",
        amount=Decimal(p["amount"]),
        currency="RON",
        iban=iban,
        ref=ref,
        counterparty=p["payee_name"],
        counterparty_iban=p["payee_iban"],
    )


def statement(*txs, period=(date(2026, 10, 1), date(2026, 10, 31))):
    ibans = sorted({t.iban for t in txs})
    accounts = [statements.Account(i, "RON", [t for t in txs if t.iban == i]) for i in ibans]
    return statements.Statement("NORD", period[0], period[1], accounts)


@pytest.fixture
def two_ron(monkeypatch):
    """NORD pays from BT and also holds a RON account at ING."""
    set_firm("NORD", dataclasses.replace(nord(), accounts=nord().accounts + (companies.Account(ING, "RON", "ing"),)))
    return nord()


def test_only_a_debit_from_the_account_the_file_names_pays_it(ledger, home, two_ron):
    item = _op(ledger, 1)
    download_batch(ledger, [item])
    assert ledger.payments(company="NORD")[0]["source_iban"] == two_ron.ron_iban
    for iban in (ING, ""):  # same transfer, from another account or from no known one
        assert recon.auto_mark_paid(ledger, statement(debit(item, ledger, iban))) == []
    assert ledger.payments(company="NORD")[0]["status"] == "exported"
    assert recon.auto_mark_paid(ledger, statement(debit(item, ledger, two_ron.ron_iban, ref="R2"))) == [item]


def test_a_payment_exported_before_accounts_were_recorded_is_paid_from_any_company_account(ledger, home, two_ron):
    item = _op(ledger, 1)
    download_batch(ledger, [item])
    ledger.db.execute("UPDATE payments SET source_iban=NULL")
    ledger.db.commit()
    assert recon.auto_mark_paid(ledger, statement(debit(item, ledger, ING))) == [item]


def test_an_account_replaced_after_the_file_still_pays_what_the_file_named(ledger, home, monkeypatch):
    item = _op(ledger, 1)
    download_batch(ledger, [item])
    old = nord().ron_iban
    moved = tuple(
        dataclasses.replace(a, pays=False, retired="2026-10-28") if a.iban == old else a for a in nord().accounts
    )
    set_firm("NORD", dataclasses.replace(nord(), accounts=moved + (companies.Account(ING, "RON", "ing", pays=True),)))
    assert recon.auto_mark_paid(ledger, statement(debit(item, ledger, ING))) == []
    assert recon.auto_mark_paid(ledger, statement(debit(item, ledger, old, ref="R2"))) == [item]


def test_the_payment_file_waits_for_the_paying_accounts_statement(ledger, home, two_ron):
    item = _op(ledger, 1)
    ident = ledger.create_payment_file("NORD", "Statement hold")
    assert ledger.attach_payment_file(ident, "NORD", item)
    ledger.add_document("ing", "NORD", "2026-09", "statement", "/x/ing.xml", accounts=[ING])
    with pytest.raises(PaymentFileError):
        pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert not pay.named_path(ledger.payment_file(ident)).exists()
    ledger.add_document("bt", "NORD", "2026-09", "statement", "/x/bt.xml", accounts=[two_ron.ron_iban])
    assert pay.download_file(ledger, ident, "NORD", D0, lambda: None)["download_id"]


def test_a_statement_stored_before_accounts_were_recorded_covers_every_account(ledger, home):
    ledger.add_document("old", "NORD", "2026-09", "statement", "/x/old.pdf")
    assert statement_intake.covers(ledger, "NORD", "2026-09", nord().ron_iban)
    assert statement_intake.covers(ledger, "NORD", "2026-09", " " + nord().ron_iban.lower())
    assert not statement_intake.covers(ledger, "NORD", "2026-08", nord().ron_iban)
    assert not statement_intake.covers(ledger, "STUDIO9", "2026-09", nord().ron_iban)


def test_the_settling_transaction_rechecks_the_account(ledger, home, two_ron):
    """A matcher that decided before another connection's export bound the row still can't pay it from another
    account."""
    item = _op(ledger, 1)
    other = debit(item, ledger, ING)
    ledger.record_statement(statement(other))  # the debit is on record, from ING
    exporter = Ledger()
    try:
        download_batch(exporter, [item])  # meanwhile the file binds the row to BT
    finally:
        exporter.close()
    assert not ledger.mark_paid(item, "statement", statements.tx_key(other))
    assert statements.tx_key(other) not in ledger.used_txs()  # nothing reserved
    mine = debit(item, ledger, two_ron.ron_iban, ref="R2")
    ledger.record_statement(statement(mine))
    assert ledger.mark_paid(item, "statement", statements.tx_key(mine))


BT_NEW = "RO00BTRLRONCRT0000000003"


def switch_paying(monkeypatch, to, bank):
    moved = tuple(dataclasses.replace(a, pays=False, retired="2026-10-27") if a.pays else a for a in nord().accounts)
    set_firm("NORD", dataclasses.replace(nord(), accounts=moved + (companies.Account(to, "RON", bank, pays=True),)))


def test_after_a_paying_account_change_each_row_still_debits_the_account_it_was_exported_from(
    ledger, home, monkeypatch
):
    first = _op(ledger, 1)
    one, _ = download_batch(ledger, [first])
    old = nord().ron_iban
    switch_paying(monkeypatch, BT_NEW, "bt")
    second = _op(ledger, 2, "200.00")
    two, snapshot = download_batch(ledger, [second])
    assert (ledger.payment(first)["source_iban"], ledger.payment(second)["source_iban"]) == (old, BT_NEW)
    for ident, account in ((one, old), (two, BT_NEW)):
        path = pay.named_path(ledger.payment_file(ident))
        assert path.read_text().splitlines()[1].split(",")[1] == account
        assert btgo.validate_file(path, "NORD") == []
    assert recon.auto_mark_paid(ledger, statement(debit(first, ledger, old))) == [first]
    assert pay.import_file(ledger, two, "NORD", snapshot["download_id"], D0)
    assert BT_NEW.encode() in Path(ledger.payment_file(two)["path"]).read_bytes()


def test_a_paying_account_at_a_bank_without_a_payment_format_gets_no_file(ledger, home, monkeypatch):
    first = _op(ledger, 1)
    one, _ = download_batch(ledger, [first])
    switch_paying(monkeypatch, ING, "ing")
    second = _op(ledger, 2, "200.00")
    with pytest.raises(PaymentFileError):
        download_batch(ledger, [second])
    assert ledger.payment(second)["status"] == "pending"
    assert pay.download_file(ledger, one, "NORD", D0, lambda: None)["download_id"]


def test_one_companys_stuck_move_never_stops_another_companys_file(ledger, home, monkeypatch):
    first = _op(ledger, 1)
    one, snapshot = download_batch(ledger, [first])
    monkeypatch.setattr(pay, "_finish_named_import", lambda *a: (_ for _ in ()).throw(PaymentFileError("stuck")))
    with pytest.raises(PaymentFileError):
        pay.import_file(ledger, one, "NORD", snapshot["download_id"], D0)
    second = _op(ledger, 2, company="STUDIO9")
    _, other = download_batch(ledger, [second], company="STUDIO9")
    assert other["download_id"] and ledger.get_meta(f"payment_import:{one}")


def test_reading_a_statement_again_fills_the_account_of_a_row_recorded_before(ledger, home):
    item = _op(ledger, 1)
    t = debit(item, ledger, nord().ron_iban)
    key = statements.tx_key(t)
    ledger.db.execute(
        "INSERT INTO statement_txs(tx_key,company,direction,iban) VALUES(?,?,?,NULL)", (key, "NORD", "debit")
    )
    ledger.db.execute("INSERT INTO statement_txs(tx_key,company,direction,iban) VALUES('other','NORD','debit','RO00X')")
    ledger.db.commit()
    ledger.record_statement(statement(t))
    assert ledger.db.execute("SELECT iban FROM statement_txs WHERE tx_key=?", (key,)).fetchone()[0] == nord().ron_iban
    t2 = dataclasses.replace(t, ref="other-ref")
    ledger.db.execute("UPDATE statement_txs SET tx_key=? WHERE tx_key='other'", (statements.tx_key(t2),))
    ledger.db.commit()
    ledger.record_statement(statement(t2))  # a recorded account is never overwritten
    assert (
        ledger.db.execute("SELECT iban FROM statement_txs WHERE tx_key=?", (statements.tx_key(t2),)).fetchone()[0]
        == "RO00X"
    )
    download_batch(ledger, [item])
    assert recon.auto_mark_paid(ledger, statement(t)) == [item]  # the filled account now proves the bound row


def test_without_a_paying_account_the_file_says_so_instead_of_waiting_for_a_statement(ledger, home, monkeypatch):
    item = _op(ledger, 1)
    set_firm("NORD", dataclasses.replace(nord(), accounts=()))
    with pytest.raises(PaymentFileError):
        download_batch(ledger, [item])
    assert ledger.payment(item)["status"] == "pending"
