"""CSV statements: read with the columns picked once per account; uploaded on an account. Synthetic data only."""

import dataclasses
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from firms import set_firm
from test_api import (
    _req,  # noqa: F401
    server as server,
)
from test_mt940 import block

from fiscalm import companies, company_config, pack, recon
from fiscalm.api import actions
from fiscalm.bank import csv as cs, intake as statement_intake, statements, upload as su
from fiscalm.bank.mt940 import parse_mt940
from fiscalm.efactura import inbox as efactura

ING = "RO00INGB0000000000000009"
RON, EUR = "RO24BTRLRONCRT0000000001", "RO71BTRLEURCRT0000000001"

SIGNED = (
    b"Data;Detalii;Suma;Beneficiar;IBAN beneficiar;Referinta\n"
    b"02.10.2026;Plata factura ABC123;-1.250,50;Furnizor SRL;RO49AAAA1B31007593840000;R1\n"
    b"15.10.2026;Incasare client;300,00;Client SRL;;R2\n"
)
SPLIT = b"date,details,debit,credit\n2026-10-03,Chirie,100.00,\n2026-10-04,Avans,,50.25\n"
M_SIGNED = {
    "date": "Data",
    "date_format": "%d.%m.%Y",
    "decimal": ",",
    "amount": "Suma",
    "description": "Detalii",
    "counterparty": "Beneficiar",
    "counterparty_iban": "IBAN beneficiar",
    "reference": "Referinta",
}
M_SPLIT = {
    "date": "date",
    "date_format": "%Y-%m-%d",
    "decimal": ".",
    "debit": "debit",
    "credit": "credit",
    "description": "details",
}


def test_a_preview_shows_the_columns_and_first_rows():
    p = cs.preview(SIGNED)
    assert p["columns"] == ["Data", "Detalii", "Suma", "Beneficiar", "IBAN beneficiar", "Referinta"]
    assert p["rows"][0][:3] == ["02.10.2026", "Plata factura ABC123", "-1.250,50"]


@pytest.mark.parametrize(
    "data,dates,decimals",
    [
        (SIGNED, {"Data": "%d.%m.%Y"}, {"Suma": ","}),
        (SPLIT, {"date": "%Y-%m-%d"}, {"debit": ".", "credit": "."}),
        # 03/04 reads as March or April and 100 with either separator: the person picks
        (b"Data,Suma,Detalii\n03/04/2026,100,Chirie\n05/06/2026,-20,Avans\n", {}, {}),
        # thousands alone prove no separator: -1.234 could be a dot decimal the import must refuse
        (b"Data;Suma;Detalii\n2026-10-02;-1.234;Chirie\n2026-10-03;5;Avans\n", {"Data": "%Y-%m-%d"}, {}),
    ],
)
def test_a_preview_settles_a_date_format_or_decimal_separator_only_when_one_fits_every_row(data, dates, decimals):
    assert cs.preview(data)["detected"] == {"dates": dates, "decimals": decimals}


def test_a_signed_amount_column_with_decimal_commas():
    st = cs.parse_csv(SIGNED, "NORD", RON, M_SIGNED)
    a, b = st.txs
    assert (a.date, a.direction, a.amount, a.counterparty, a.counterparty_iban, a.ref, a.currency, a.iban) == (
        date(2026, 10, 2),
        "debit",
        Decimal("1250.50"),
        "Furnizor SRL",
        "RO49AAAA1B31007593840000",
        "R1",
        "RON",
        RON,
    )
    assert (b.direction, b.amount, b.counterparty_iban) == ("credit", Decimal("300.00"), "")
    # partial: it proves only the days it holds, never the whole month
    assert (st.period_start, st.period_end, st.month, st.partial) == (
        date(2026, 10, 2),
        date(2026, 10, 15),
        "2026-10",
        True,
    )
    st = cs.parse_csv(SIGNED.replace(b"15.10.2026", b"03.12.2026"), "NORD", RON, M_SIGNED)  # spanning months
    assert (st.period_end, st.month, st.partial) == (date(2026, 12, 3), "2026-10", True)  # stored under its first


def test_debit_and_credit_columns():
    st = cs.parse_csv(SPLIT, "NORD", EUR, M_SPLIT)
    assert [(t.direction, t.amount, t.currency) for t in st.txs] == [
        ("debit", Decimal("100.00"), "EUR"),
        ("credit", Decimal("50.25"), "EUR"),
    ]


@pytest.mark.parametrize(
    "data,mapping,why",
    [
        (SIGNED.replace(b"02.10.2026", b"2026-10-02"), M_SIGNED, "date not understood"),
        (SIGNED.replace(b"-1.250,50", b"abc"), M_SIGNED, "amount not understood"),
        (SIGNED.replace(b"300,00", b"0,00"), M_SIGNED, "zero"),
        (SPLIT.replace(b"100.00,", b"100.00,1.00"), M_SPLIT, "exactly one"),
        (SIGNED, M_SIGNED | {"amount": "Nope"}, "columns not found"),
        (SIGNED, {k: v for k, v in M_SIGNED.items() if k != "description"}, "details"),
        (SIGNED, M_SIGNED | {"debit": "Suma"}, "one amount column"),
        (SIGNED, M_SIGNED | {"decimal": ";"}, "decimal"),
        (SIGNED, M_SIGNED | {"evil": "x"}, "unknown fields"),
    ],
)
def test_what_would_read_money_wrongly_is_refused(data, mapping, why):
    with pytest.raises(statements.StatementError, match=why):
        cs.parse_csv(data, "NORD", RON, mapping)


def test_a_csv_is_only_read_for_a_current_account_of_the_company():
    with pytest.raises(statements.StatementError, match="not a current account"):
        cs.parse_csv(SIGNED, "NORD", "RO94BTRLRONCRT0000000002", M_SIGNED)  # STUDIO9's


def rid(iban):
    return cs.account_id("NORD", iban)


def columns(*ibans):
    """The columns picked for each of NORD's accounts `ibans`."""
    for iban in ibans:
        actions.act("statement-csv-format", "NORD", {"co": "NORD", "account": rid(iban)} | M_SIGNED)


def with_ing(monkeypatch):
    nord = companies.get("NORD")
    set_firm("NORD", dataclasses.replace(nord, accounts=nord.accounts + (companies.Account(ING, "RON", "ing"),)))


def test_the_first_csv_asks_for_columns_then_imports_and_reads_again(ledger):
    with pytest.raises(su.Refused) as e:
        su.accept(ledger, "NORD", "bt", SIGNED, "text/csv", rid(RON))
    assert e.value.status == 409 and e.value.message == su.NEEDS_COLUMNS
    assert e.value.extra["columns"][2] == "Suma" and e.value.extra["account"] == rid(RON)
    assert not ledger.documents(kind="statement")
    r = actions.act("statement-csv-format", "NORD", {"co": "NORD", "account": rid(RON)} | M_SIGNED)
    assert r["ok"] and r["message"] == "Coloanele au fost salvate."
    # BT also holds NORD's EUR account: a CSV of one account is partial, so it doesn't owe the other one
    assert su.accept(ledger, "NORD", "bt", SIGNED, "text/csv", rid(RON))["status"] == "added"


def test_a_csv_of_a_bank_with_one_account_is_imported_and_its_stored_copy_reads_again(ledger, monkeypatch):
    with_ing(monkeypatch)
    columns(ING)
    r = su.accept(ledger, "NORD", "ing", SIGNED, "text/csv", rid(ING))
    assert (r["status"], r["message"]) == ("added", su.ADDED)
    (doc,) = ledger.documents(kind="statement")
    assert doc["path"].endswith(".csv") and json.loads(doc["accounts"]) == [ING]
    assert cs.NO_TOTALS in doc["issues"]  # no balances: said, not hidden
    again = statements.parse(Path(doc["path"]), "NORD")
    assert [t.amount for t in again.txs] == [Decimal("1250.50"), Decimal("300.00")]
    assert su.accept(ledger, "NORD", "ing", SIGNED, "text/csv", rid(ING))["message"] == su.DUPLICATE


@pytest.mark.parametrize(
    "account,bank",
    [("", "ing"), ("nope", ""), (cs.account_id("STUDIO9", "RO94BTRLRONCRT0000000002"), ""), (rid(RON), "ing")],
)  # a BT account on ING's card
def test_a_csv_needs_one_of_the_companys_accounts(ledger, account, bank):
    with pytest.raises(su.Refused) as e:
        su.accept(ledger, "NORD", bank, SIGNED, "text/csv", account)
    assert e.value.message == su.PICK_ACCOUNT


def test_the_column_picker_validates_and_names_only_the_companys_account():
    bad = actions.act("statement-csv-format", "NORD", {"co": "NORD", "account": rid(RON), "date": "Data"})
    assert not bad["ok"]
    other = actions.act("statement-csv-format", "NORD", {"co": "NORD", "account": "nope"} | M_SIGNED)
    assert not other["ok"]
    assert "statement-csv-format" in actions.LOCAL_ACTIONS and "statement-csv-format" not in actions.API_ACTIONS


def test_http_first_csv_answers_with_the_columns(server, ledger):
    code, _, raw = _req(
        server,
        "POST",
        f"/web/api/statement-upload?co=NORD&bank=bt&account={rid(RON)}",
        SIGNED,
        {"Content-Type": "text/csv"},
    )
    r = json.loads(raw)
    assert code == 409 and not r["ok"] and r["csv"]["columns"][0] == "Data" and len(r["csv"]["rows"]) == 2


@pytest.mark.parametrize(
    "raw,decimal,value",
    [
        ("1.250,50", ",", "1250.50"),
        ("1250,5", ",", "1250.5"),
        ("-3", ",", "-3"),
        ("1,250.50", ".", "1250.50"),
        ("12.34", ".", "12.34"),
        # written another way: refused, never read as another number
        ("12,34", ".", None),
        ("1.2345,00", ",", None),
        ("0,004", ",", None),
        ("1,23.45", ".", None),
        ("12.345.6", ",", None),
    ],
)
def test_amounts_as_exports_write_them(raw, decimal, value):
    if value is None:
        with pytest.raises(statements.StatementError, match="amount"):
            cs._money(raw, decimal)
    else:
        assert cs._money(raw, decimal) == Decimal(value)


def test_identical_rows_are_refused_as_one_transaction_read_twice():
    data = SIGNED + b"02.10.2026;Plata factura ABC123;-1.250,50;Furnizor SRL;RO49AAAA1B31007593840000;R1\n"
    with pytest.raises(statements.StatementError, match="identical rows"):
        cs.parse_csv(data, "NORD", RON, M_SIGNED)


def test_the_same_bytes_on_another_account_are_another_document_never_a_reinterpretation(ledger, monkeypatch):
    with_ing(monkeypatch)
    columns(RON, ING)
    su.accept(ledger, "NORD", "bt", SIGNED, "text/csv", rid(RON))
    (first,) = ledger.documents(kind="statement")
    su.accept(ledger, "NORD", "ing", SIGNED, "text/csv", rid(ING))
    docs = {d["sha"]: d for d in ledger.documents(kind="statement")}
    assert len(docs) == 2 and json.loads(docs[first["sha"]]["accounts"]) == [RON]
    again = statements.parse(Path(docs[first["sha"]]["path"]), "NORD")
    assert {a.iban for a in again.accounts} == {RON}  # the first is still read as RON's


def test_csvs_of_one_account_add_up_day_by_day_and_never_cover_a_month(ledger):
    columns(RON)
    su.accept(ledger, "NORD", "", SIGNED, "text/csv", rid(RON))  # 2 to 15 October
    later = SIGNED.replace(b"02.10.2026", b"20.10.2026").replace(b"15.10.2026", b"28.10.2026")
    assert su.accept(ledger, "NORD", "", later, "text/csv", rid(RON))["status"] == "added"  # 20 to 28: no overlap
    overlapping = (
        SIGNED.replace(b"02.10.2026", b"10.10.2026").replace(b"15.10.2026", b"22.10.2026").replace(b"R1", b"R9")
    )
    with pytest.raises(su.Refused):
        su.accept(ledger, "NORD", "", overlapping, "text/csv", rid(RON))
    assert not statement_intake.covers(ledger, "NORD", "2026-10", RON)  # partial: never the month's statement


def test_a_crash_after_publishing_a_csv_is_finished_on_the_next_run(ledger, monkeypatch):
    columns(RON)
    real = efactura.match_statement
    monkeypatch.setattr(efactura, "match_statement", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        su.accept(ledger, "NORD", "", SIGNED, "text/csv", rid(RON))
    assert (
        ledger.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'statement_import:%' AND value!=''").fetchone()[0]
        == 1
    )
    monkeypatch.setattr(efactura, "match_statement", real)
    statement_intake.recover(ledger)
    assert (
        ledger.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'statement_import:%' AND value!=''").fetchone()[0]
        == 0
    )
    (doc,) = ledger.documents(kind="statement")
    assert doc["path"].endswith(".csv")


def test_amounts_are_kept_with_two_decimals_whatever_the_export_wrote():
    st = cs.parse_csv(SIGNED.replace(b"-1.250,50", b"-1250,5").replace(b"300,00", b"300"), "NORD", RON, M_SIGNED)
    assert [str(t.amount) for t in st.txs] == ["1250.50", "300.00"]
    same = cs.parse_csv(SIGNED, "NORD", RON, M_SIGNED)
    assert statements.tx_key(st.txs[0]) == statements.tx_key(same.txs[0])  # one payment, however it was written


def test_a_csv_overlapping_an_earlier_one_that_spans_months_is_refused(ledger):
    columns(RON)
    long = SIGNED.replace(b"02.10.2026", b"20.08.2026").replace(b"15.10.2026", b"25.10.2026")
    assert su.accept(ledger, "NORD", "", long, "text/csv", rid(RON))["status"] == "added"  # stored under August
    (doc,) = ledger.documents(kind="statement")
    assert doc["month"] == "2026-08"
    later = SIGNED.replace(b"02.10.2026", b"20.10.2026").replace(b"15.10.2026", b"28.10.2026").replace(b"R1", b"R7")
    with pytest.raises(su.Refused):
        su.accept(ledger, "NORD", "", later, "text/csv", rid(RON))


def _full_october(debits):
    """RON's October as an MT940: an opening of 5000.00 and these (day, amount, ref) debits."""
    lines = [
        line
        for day, amount, ref in debits
        for line in (f":61:{day}{day[2:]}D{amount.replace('.', ',')}NTRF{ref}", ":86:Plata")
    ]
    close = str(Decimal("5000.00") - sum(Decimal(a) for _, a, _ in debits)).replace(".", ",")
    return parse_mt940(block(opening="C260930RON5000,00", closing=f"C261031RON{close}", lines=lines, ref="S1"), "NORD")


@pytest.mark.parametrize("csv_period", [(b"02.10.2026", b"15.10.2026"), (b"20.09.2026", b"15.10.2026")])
def test_a_full_statement_after_csvs_is_taken_and_the_csvs_days_keep_counting_once(ledger, csv_period):
    """The month's statement supersedes CSVs of its account (one stored under September included): its lines on the
    CSVs' days are reserved as their twins, so a payment a CSV brought never settles twice; its other days count."""
    columns(RON)
    su.accept(
        ledger,
        "NORD",
        "",
        SIGNED.replace(b"02.10.2026", csv_period[0]).replace(b"15.10.2026", csv_period[1]),
        "text/csv",
        rid(RON),
    )
    full = _full_october([("261002", "1250.50", "B1"), ("261025", "40.00", "B2")])
    statement_intake.refuse_overlap(ledger, full, "full-sha")  # not refused: it supersedes the CSV
    statement_intake.hold_csv_days(ledger, full)
    used = ledger.used_txs()
    assert [statements.tx_key(t) in used for t in full.txs] == [True, False]


def test_a_csv_never_stands_for_the_months_statement(ledger):
    columns(RON)
    su.accept(ledger, "NORD", "", SIGNED, "text/csv", rid(RON))
    ledger.add_document("rc1", "NORD", "2026-10", "receipt", "/x/rc1.pdf")  # something for the pack
    assert not pack.ready(ledger, "NORD", "2026-10")
    view = recon.month_view(ledger, "NORD", "2026-10", [])
    assert (view["statement"], view["balanced"]) == (False, None)  # no balances: unknown, not a discrepancy
    out = company_config.accounts_out(ledger, companies.get("NORD"))
    assert all(a["last_statement"] is None for a in out)


def test_a_csv_reads_its_banks_words():
    data = "Data;Detalii;Suma\n05.10.2026;Comision administrare cont;-15,00\n06.10.2026;Plata la POS MAGAZIN;-30,00\n"
    m = {"date": "Data", "date_format": "%d.%m.%Y", "decimal": ",", "amount": "Suma", "description": "Detalii"}
    txs = cs.parse_csv(data.encode(), "NORD", RON, m).txs  # a BT account: BT's words
    assert [statements.classify_tx(t) for t in txs] == [statements.BANK_FEE, statements.NEEDS_INVOICE]
