"""Acasă's numbers (fiscalm.dashboard): synthetic data only, the scenario's world and invented statements."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import synthetic_world as scenario
from test_api import world as world  # noqa: F401
from test_camt import parse as parse_camt, xml as camt_xml
from test_mt940 import block

from fiscalm import companies, dashboard, home_charts, transactions
from fiscalm.api import lists, state as api
from fiscalm.bank import statements
from fiscalm.bank.mt940 import parse_mt940

RON, EUR = "RO24BTRLRONCRT0000000001", "RO71BTRLEURCRT0000000001"
TODAY = scenario.NOW.date()


def _money(total: str, *other: tuple[str, str]) -> dict:
    return {"total": total, "other": [{"currency": c, "total": t} for c, t in other]}


def _tx(day: date, amount: str, ref: str) -> statements.Tx:
    return statements.Tx(
        date=day,
        title="Incasare",
        description=ref,
        direction="credit",
        amount=Decimal(amount),
        currency="RON",
        iban=RON,
        ref=ref,
    )


def _record(ledger, start: date, end: date, *txs: statements.Tx, **kw) -> None:
    ledger.record_statement(statements.Statement("NORD", start, end, [statements.Account(RON, "RON", list(txs))], **kw))


def _moves(ledger, since: date = date(2026, 10, 1)) -> list:
    return dashboard.movements(ledger, "NORD", companies.get("NORD"), since, TODAY)


def test_the_firm_in_numbers(world, ledger):
    s = api.state(ledger, "NORD")
    d = s["dashboard"]
    month, quarter, year = (d["periods"][k] for k in ("month", "quarter", "year"))
    assert (month["from"], quarter["from"], year["from"], month["to"]) == (
        "2026-10-01",
        "2026-08-01",
        "2026-01-01",
        "2026-10-27",
    )
    # the history (4200 in, 2950 out) and NORD's own October lines: a 500 credit, card payments of 74.70 and 173.45, a 15 fee
    assert (month["in"], month["out"], month["left"]) == (_money("4700.00"), _money("4463.15"), _money("236.85"))
    # September: the history, the scenario's rent and wages (2400, 940), Hetzner's card payment (44.80) and the 3000
    # collected from Client Delta: every line once
    assert d["months"][-2] == {"month": "2026-09", "in": _money("13350.00"), "out": _money("9884.80")}
    # lei and euro are never added: the EUR account's credit stays apart, in its own currency
    assert year["in"] == _money("58400.00", ("EUR", "600.00"))
    assert year["left"] == _money("8502.05", ("EUR", "600.00"))
    assert [m["month"] for m in d["months"]] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert d["through"] == "2026-10-27"
    # De explicat is Tranzacții's own count
    assert (
        d["to_explain"]
        == s["transactions"]["to_explain"]
        == sum(1 for r in s["transactions"]["rows"] if not r["explanation"])
    )


def test_acasa_sums_exactly_the_lines_tranzactii_lists(world, ledger):
    """One list (Ledger.account_lines): what Acasă sums for this year is what Tranzacții lists for it, line by line."""
    s = api.state(ledger, "NORD")
    year = s["dashboard"]["periods"]["year"]
    listed = [r for r in s["transactions"]["rows"] if r["date"] >= year["from"]]
    for side, direction in (("in", "credit"), ("out", "debit")):
        by: dict = {}
        for r in listed:
            if r["direction"] == direction:
                by[r["currency"]] = by.get(r["currency"], Decimal(0)) + Decimal(r["amount"])
        assert Decimal(year[side]["total"]) == by.get("RON", Decimal(0))
        assert {o["currency"]: Decimal(o["total"]) for o in year[side]["other"]} == {
            c: v for c, v in by.items() if c != "RON"
        }


def test_largest_partners_net_of_credit_notes_and_rejections(world, ledger):
    scenario._ef(
        ledger,
        "7800",
        "NORD",
        "Chirie Spatiu SRL",
        "RO1011",
        "CS-ST",
        date(2026, 9, 20),
        None,
        "-400.00",
        status="credit",
    )
    ledger.db.execute("UPDATE efactura SET doc_type='credit_note' WHERE message_id='7800'")
    ledger.db.commit()  # Ungrouped credit notes project as skipped, but still reduce purchases.
    d = api.state(ledger, "NORD")["dashboard"]
    q = d["periods"]["quarter"]
    assert [(s["name"], s["total"]) for s in q["suppliers"][:2]] == [
        ("Mobilier Vechi SRL", "3120.00"),
        ("Chirie Spatiu SRL", "2000.00"),
    ]
    assert len(q["suppliers"]) == dashboard.TOP
    clients = {c["name"]: c for c in d["periods"]["month"]["clients"]}
    # Net of the storno; the count includes both invoice and credit-note documents, but no rejected invoice.
    assert clients["Client Exemplu SRL"]["total"] == "847.00" and clients["Client Exemplu SRL"]["count"] == 6
    # Euro has its own ranking and is never compared with lei.
    euro = next(r for r in d["periods"]["month"]["rankings"] if r["currency"] == "EUR")
    assert euro["clients"][0] == {
        "partner": "NORD.issued-5003",
        "company": "NORD",
        "name": "Client Gamma GmbH",
        "total": "0.00",
        "other": [{"currency": "EUR", "total": "900.00"}],
        "count": 1,
    }
    assert "Client Gamma GmbH" not in clients
    # Client Zeta's invoice is fully credited: no sale
    assert all(c["name"] != "Client Zeta SRL" for c in d["periods"]["quarter"]["clients"])


def test_upcoming_is_what_falls_due_in_thirty_days(world, ledger):
    s = api.state(ledger, "NORD")
    up = s["dashboard"]["upcoming"]
    assert [u["date"] for u in up] == sorted(u["date"] for u in up)
    assert all("2026-10-27" <= u["date"] <= "2026-11-26" for u in up)
    ids = {(u["kind"], u["id"]) for u in up}
    assert ("invoice", "102") in ids and ("issued", "5002") in ids
    assert ("invoice", "101") not in ids  # overdue: in De plătit's late part, not here
    assert ("invoice", "108") not in ids  # marked paid by the user: nothing left to pay
    assert ("invoice", "103") in ids and next(u for u in up if u["id"] == "103")["currency"] == "EUR"
    assert all(u["company"] == "NORD" for u in up)


def test_late_parts_follow_the_late_counts(world, ledger):
    s = api.state(ledger, "NORD")
    today = s["today"]
    late_invoices = [r for r in s["invoices"] if r["status"] == "open" and r["due_date"] and r["due_date"] < today]
    late_payments = [
        p
        for k in ("held", "in_file", "lost")
        for p in api.prepared_payments(ledger, "NORD")[k]
        if p["due"] and p["due"] < today
    ]
    assert s["payable"]["late"] == len(late_invoices) + len(late_payments)
    assert Decimal(s["payable"]["late_total"]) == sum(Decimal(x["total"]) for x in late_invoices) + sum(
        Decimal(p["amount"]) for p in late_payments
    )
    overdue = s["dashboard"]["charts"]["aging"]["payables"]["buckets"][1:4]
    assert sum(b["count"] for b in overdue) == 2
    assert sum(Decimal(b["total"]) for b in overdue) == Decimal("451.30")
    assert [(p["kind"], p["details"], p["amount"], p["due"]) for p in late_payments if p["ef_id"] is None] == [
        ("op", "TVA", "3200.00", "2026-09-25")
    ]
    late_issued = [r for r in s["issued"] if api.issued_owed(r) and r["days_overdue"] > 0]
    assert s["receivable"]["late"] == len(late_issued)
    assert Decimal(s["receivable"]["late_total"]) == sum(Decimal(r["owed"]) for r in late_issued) == Decimal("5405.00")


def test_all_firms_add_up_per_currency(world, ledger):
    a = api.state(ledger, "ALL")
    parts = {p["co"]: p["dashboard"] for p in a["parts"]}
    for k in dashboard.PERIODS:
        for side in ("in", "out", "left"):
            got = a["dashboard"]["periods"][k][side]
            assert Decimal(got["total"]) == sum(Decimal(p["periods"][k][side]["total"]) for p in parts.values())
    assert a["dashboard"]["periods"]["year"]["in"]["other"] == [{"currency": "EUR", "total": "600.00"}]
    assert a["payable"]["late_total"] == f"{sum(Decimal(p['payable']['late_total']) for p in a['parts']):.2f}"
    assert {b["company"] for b in a["dashboard"]["balances"]} == {"NORD", "STUDIO9"}
    up = a["dashboard"]["upcoming"]
    assert {u["company"] for u in up} == {"NORD", "STUDIO9"} and [u["date"] for u in up] == sorted(
        u["date"] for u in up
    )
    assert a["dashboard"]["through"] == min(p["through"] for p in parts.values())


def test_balances_shown_only_when_a_statement_proves_them(world, ledger):
    d = api.state(ledger, "ALL")["dashboard"]
    by = {(b["company"], b["currency"]): b for b in d["balances"]}
    assert by[("NORD", "RON")]["balance"] == "18420.55" and by[("NORD", "RON")]["day"] == "2026-09-30"
    assert by[("STUDIO9", "RON")]["balance"] is None and by[("STUDIO9", "RON")]["day"] is None


def test_transactions_reach_the_statements_closing_day(ledger):
    _record(ledger, date(2026, 9, 1), date(2026, 9, 30), _tx(date(2026, 9, 25), "10.00", "a"))
    empty = dict(
        invoices=[],
        rows=[],
        unpaid_payments=[],
        to_explain=0,
        received_owed=None,
        issued_owed=None,
        entries=[],
        partners=[],
    )
    for day, through in (("2026-09-30", "2026-09-30"), (None, "2026-09-25")):
        d = dashboard.company_view(ledger, "NORD", companies.get("NORD"), TODAY, balances_=[{"day": day}], **empty)
        assert d["through"] == through


def test_lines_that_cant_be_placed_are_left_out(ledger):
    with ledger.tx() as db:
        db.executemany(
            "INSERT INTO statement_txs(tx_key,company,direction,iban,day,amount,source) VALUES(?,?,?,?,?,?,?)",
            [
                ("a", "NORD", "credit", None, "2026-10-05", "10.00", "file"),  # no account
                ("b", "NORD", "credit", RON, None, "10.00", "file"),  # no day
                (
                    "c",
                    "NORD",
                    "credit",
                    "RO94BTRLRONCRT0000000002",
                    "2026-10-05",
                    "10.00",
                    "file",
                ),  # another firm's account
                ("d", "STUDIO9", "credit", RON, "2026-10-05", "10.00", "file"),  # another firm's line
                ("e", "NORD", "debit", RON, "2026-10-28", "10.00", "file"),  # after today
                ("f", "NORD", "debit", RON, "2026-10-06", "7.25", "file"),
            ],
        )
    assert _moves(ledger) == [("2026-10-06", "debit", "RON", Decimal("7.25"))]


BT_HEAD = "Data        Descriere                                   Debit           Credit"


def bt_text(opening: str = "1,000.00", closing: str = "1,150.00") -> str:
    rows = [
        "CONT CURENT NORD                     Valuta",
        f"RON  Cod IBAN: {RON}",
        BT_HEAD,
        "01/10/2026  SOLD ANTERIOR                                           " + opening,
        "05/10/2026  Incasare OP                                                   200.00",
        "            Client Exemplu SRL",
        "12/10/2026  Plata OP                                   50.00",
        "            Furnizor Exemplu SRL",
        "            RULAJ TOTAL CONT                           50.00          200.00",
        "            SOLD FINAL CONT                                         " + closing,
    ]
    return "Extras de cont din 01/10/2026 - 31/10/2026\n" + "\n".join(rows) + "\n"


def test_closing_balance_of_each_format(tmp_path):
    (a,) = parse_camt(tmp_path, camt_xml()).accounts
    assert (a.closing, a.closing_day) == (Decimal("750.00"), date(2026, 10, 31))
    (a,) = parse_mt940(block(), "NORD").accounts
    assert (a.closing, a.closing_day) == (Decimal("750.00"), date(2026, 10, 31))
    (a,) = statements.parse_text(bt_text(), "NORD").accounts
    assert a.trustworthy and len(a.txs) == 2
    assert (a.closing, a.closing_day) == (Decimal("1150.00"), date(2026, 10, 31))


@pytest.mark.parametrize(
    "text",
    [
        bt_text(closing="1,100.00"),
        bt_text(opening="900.00"),
        bt_text().replace("200.00\n            Client", "210.00\n            Client"),
    ],
)
def test_a_bt_balance_that_doesnt_add_up_cannot_settle(text, ledger):
    """Opening plus the lines must equal the closing balance, and the lines the bank's totals: else no balance."""
    st = statements.parse_text(text, "NORD")
    assert not st.settlement_txs
    ledger.record_statement(st)
    assert not ledger.db.execute("SELECT 1 FROM statement_txs").fetchone()


def test_balances_read_the_newest_statement_that_proves_one(ledger, home):
    home = home / "NORD"
    home.mkdir(exist_ok=True)
    co = companies.get("NORD")
    sep, aug = home / "sep.xml", home / "aug.pdf"
    sep.write_bytes(b"sep")
    aug.write_bytes(b"aug")
    ledger.add_document("s" * 40, "NORD", "2026-09", "statement", str(sep))
    ledger.add_document("a" * 40, "NORD", "2026-08", "statement", str(aug))
    ledger.add_document("m" * 40, "NORD", "2026-07", "statement", str(home / "missing.pdf"))

    def parse(path, company):
        if (
            path.resolve().name == "sep.xml"
        ):  # September proves the RON account; its EUR account has no balance (a CSV, say)
            return statements.Statement(
                company,
                None,
                None,
                [
                    statements.Account(RON, "RON", closing=Decimal("-12.50"), closing_day=date(2026, 9, 30)),
                    statements.Account(EUR, "EUR"),
                ],
            )
        if path.resolve().name == "aug.pdf":
            return statements.Statement(
                company,
                None,
                None,
                [
                    statements.Account(RON, "RON", closing=Decimal("99.00"), closing_day=date(2026, 8, 31)),
                    statements.Account(EUR, "EUR", closing=Decimal("40.00"), closing_day=date(2026, 8, 31)),
                ],
            )
        raise AssertionError(path)

    got = home_charts.cash(ledger, "NORD", co, TODAY, parse)[1]
    assert [(b["currency"], b["balance"], b["day"]) for b in got] == [
        ("RON", "-12.50", "2026-09-30"),
        ("EUR", "40.00", "2026-08-31"),
    ]

    def broken(path, company):
        raise statements.StatementError("unreadable")

    assert [b["balance"] for b in home_charts.cash(ledger, "NORD", co, TODAY, broken)[1]] == [None, None]


def test_periods_start_where_they_say():
    d = date(2026, 2, 14)
    assert dashboard.period_start("month", d) == date(2026, 2, 1)
    assert dashboard.period_start("quarter", d) == date(2025, 12, 1)
    assert dashboard.period_start("year", d) == date(2026, 1, 1)


def test_a_bank_feed_read_from_mid_month_counts_until_a_statement_brings_the_month(ledger):
    """record_statement reserves a feed's lines of a month it didn't read from the 1st (the statement will settle that
    month). Until a statement brings it, those lines are the only record of the money: they count. Once it does, the
    statement's lines count and the feed's twin doesn't: the payment once."""
    _record(ledger, date(2026, 10, 10), date(2026, 10, 27), _tx(date(2026, 10, 15), "500.00", "eb:15"), source="ob")
    assert [r[0] for r in ledger.db.execute("SELECT what FROM settled_tx")] == ["twin ob"]
    assert _moves(ledger) == [("2026-10-15", "credit", "RON", Decimal("500.00"))]
    _record(ledger, date(2026, 10, 1), date(2026, 10, 31), _tx(date(2026, 10, 15), "500.00", "F15"))
    assert ledger.db.execute("SELECT count(*) FROM statement_txs WHERE company='NORD'").fetchone()[0] == 2
    assert _moves(ledger) == [("2026-10-15", "credit", "RON", Decimal("500.00"))]


def test_a_bt_pdf_with_two_periods_dates_each_balance_by_the_period_above_it():
    september = bt_text().replace("01/10/2026 - 31/10/2026", "01/09/2026 - 30/09/2026").replace("/10/2026", "/09/2026")
    text = bt_text() + september.replace(RON, EUR).replace("RON  Cod", "EUR  Cod")
    accounts = statements.parse_text(text, "NORD").accounts
    assert len(accounts) == 2 and all(a.balanced for a in accounts)
    closing = Decimal("1150.00")
    assert [(a.closing, a.closing_day) for a in accounts] == [
        (closing, date(2026, 10, 31)),
        (closing, date(2026, 9, 30)),
    ]
    # a section with no period above it, in a file naming two, proves none
    text = bt_text().split("\n", 1)[1] + bt_text() + september
    assert statements.parse_text(text, "NORD").accounts[0].closing is None
    # a line outside its section's period proves none either
    assert statements.parse_text(bt_text() + september.replace("05/09", "05/10"), "NORD").accounts[1].closing is None


def test_a_bt_pdfs_month_is_its_sections_and_a_line_needs_its_day():
    """Review: a period printed on a cover page names no month; a line before any date is no statement line."""
    assert statements.parse_text("Extras de cont din 01/09/2026 - 30/09/2026\n" + bt_text(), "NORD").month == "2026-10"
    undated = bt_text().replace("01/10/2026  SOLD", "            SOLD").replace("05/10/2026  Inc", "            Inc")
    for text in (undated, bt_text() + undated.split("\n", 1)[1]):  # alone, or after a section that had dates
        with pytest.raises(statements.StatementError, match="booking date"):
            statements.parse_text(text, "NORD")


@pytest.mark.parametrize(
    "after_footer,trusted",
    [
        ("            SOLD FINAL CONT                    1,150.00", True),
        (
            "Extras de cont din 01/11/2026 - 30/11/2026\n            SOLD FINAL CONT     1,150.00",
            False,
        ),  # next statement's
        ("            RULAJ TOTAL CONT     60.00     200.00\n            SOLD FINAL CONT     1,150.00", False),
        ("            SOLD FINAL CONT estimat            1,150.00", False),
        ("", False),  # really missing
    ],
)
def test_after_a_page_footer_only_the_accounts_own_closing_rows_count(after_footer, trusted):
    """Review: across a page break the final balance is read only for the account still open, by its exact row; a
    second set of totals that disagrees, or another statement's header between, proves nothing."""
    head, _ = bt_text().rsplit("            SOLD FINAL CONT", 1)
    text = head + "BANCA TRANSILVANIA S.A. - footer      4/5\n\f      BANCA TRANSILVANIA\n\n" + after_footer + "\n"
    (account,) = statements.parse_text(text, "NORD").accounts
    assert account.trustworthy is trusted


def test_the_newest_balance_of_a_month_wins_and_two_that_disagree_prove_none(ledger, home):
    home = home / "NORD"
    home.mkdir(exist_ok=True)
    co = companies.get("NORD")
    for name in ("a.xml", "b.xml", "c.xml"):
        (home / name).write_bytes(name.encode())
    ledger.add_document("a" * 40, "NORD", "2026-09", "statement", str(home / "a.xml"), doc_date="2026-09-10")
    ledger.add_document("b" * 40, "NORD", "2026-09", "statement", str(home / "b.xml"), doc_date="2026-09-30")

    def acct(iban, cur, amount, day):
        return statements.Account(iban, cur, closing=Decimal(amount), closing_day=date(2026, 9, day))  # noqa: E731

    parsed = {
        "a.xml": [acct(RON, "RON", "10.00", 10), acct(EUR, "EUR", "5.00", 30)],
        "b.xml": [acct(RON, "RON", "30.00", 30), acct(EUR, "EUR", "6.00", 30)],
    }
    got = home_charts.cash(
        ledger,
        "NORD",
        co,
        TODAY,
        lambda path, company: statements.Statement(company, None, None, parsed[path.resolve().name]),
    )[1]
    assert [(b["currency"], b["balance"], b["day"]) for b in got] == [
        ("RON", "30.00", "2026-09-30"),
        ("EUR", None, None),
    ]


def test_partners_rank_the_same_for_one_firm_and_for_all_without_comparing_currencies():
    def e(name, total, count, co="NORD", other=()):
        return {
            "partner": None,
            "company": co,
            "name": name,
            "total": total,  # noqa: E731
            "other": list(other),
            "count": count,
        }

    rows = [
        e("Euro Mare", "0.00", 1, other=[{"currency": "EUR", "total": "9000.00"}]),
        e("Euro Des", "0.00", 3, "STUDIO9"),
        e("Lei", "10.00", 1),
    ]
    assert [x["name"] for x in sorted(rows, key=dashboard.rank)] == ["Lei", "Euro Des", "Euro Mare"]
    got = dashboard._top(
        [
            (None, "B", "EUR", Decimal("5")),
            (None, "A", "EUR", Decimal("900")),
            (None, "C", "USD", Decimal("9000")),
            (None, "D", "EUR", Decimal("-50")),
        ],
        "NORD",
        "EUR",
    )
    assert [x["name"] for x in got] == ["A", "B"]  # a tie in lei and in count goes by name, never by 900 USD > 5 EUR


def test_a_payment_dated_across_the_month_edge_counts_once_when_the_feed_starts_after_it(ledger):
    """September 30 on the statement, October 2 in a bank feed read from October 2: one payment."""
    _record(ledger, date(2026, 9, 1), date(2026, 9, 30), _tx(date(2026, 9, 30), "70.00", "F30"))
    _record(ledger, date(2026, 10, 2), date(2026, 10, 27), _tx(date(2026, 10, 2), "70.00", "eb:2"), source="ob")
    assert _moves(ledger, date(2026, 9, 1)) == [("2026-09-30", "credit", "RON", Decimal("70.00"))]
    assert _moves(ledger) == []  # counted on its statement's day


def test_a_partial_csv_hides_only_the_bank_lines_it_brought(ledger):
    """A CSV of October 2 to 15 takes the month; a bank-feed credit of the 20th, which no CSV line matches, still counts,
    and the feed's copy of a CSV line doesn't."""
    _record(ledger, date(2026, 10, 2), date(2026, 10, 15), _tx(date(2026, 10, 5), "300.00", "C5"), partial=True)
    _record(
        ledger,
        date(2026, 10, 1),
        date(2026, 10, 27),
        _tx(date(2026, 10, 5), "300.00", "eb:5"),
        _tx(date(2026, 10, 20), "70.00", "eb:20"),
        source="ob",
    )
    assert _moves(ledger) == [
        ("2026-10-05", "credit", "RON", Decimal("300.00")),
        ("2026-10-20", "credit", "RON", Decimal("70.00")),
    ]


def test_a_line_without_an_account_pairs_with_any_in_both_screens(ledger):
    """A line on record without its account (no IBAN) can be the copy of a twin on any account, or a twin of any: the
    one rule (Ledger.account_lines) decides for Tranzacții and Acasă alike, so a real payment never disappears from the
    list. Acasă sums only lines on one of the firm's accounts: one without an account is listed, not summed."""
    co = companies.get("NORD")
    tx = lambda key, day, amount, ref: json.dumps(_tx(day, amount, ref).to_dict())  # noqa: E731
    with ledger.tx() as db:
        db.executemany(
            "INSERT INTO statement_txs(tx_key,company,direction,iban,day,amount,source,tx) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    "plain",
                    "NORD",
                    "credit",
                    RON,
                    "2026-10-06",
                    "80.00",
                    "file",
                    tx("plain", date(2026, 10, 6), "80.00", "P"),
                ),
                (
                    "twin-none",
                    "NORD",
                    "credit",
                    None,
                    "2026-10-07",
                    "80.00",
                    "ob",
                    tx("twin-none", date(2026, 10, 7), "80.00", "eb:P"),
                ),
                (
                    "copy-none",
                    "NORD",
                    "credit",
                    None,
                    "2026-10-12",
                    "30.00",
                    "file",
                    tx("copy-none", date(2026, 10, 12), "30.00", "N"),
                ),
                (
                    "twin-acct",
                    "NORD",
                    "credit",
                    RON,
                    "2026-10-13",
                    "30.00",
                    "ob",
                    tx("twin-acct", date(2026, 10, 13), "30.00", "eb:N"),
                ),
            ],
        )
        db.executemany(
            "INSERT INTO settled_tx VALUES(?,?,?)", [("twin-none", "twin ob", "x"), ("twin-acct", "twin ob", "x")]
        )
    listed = [r["id"] for r in transactions.view(ledger, co, TODAY, settlements=[], ambiguous=[])["rows"]]
    assert listed == ["copy-none", "plain"]  # each twin hidden by its copy, whichever side lacks the account
    assert _moves(ledger) == [("2026-10-06", "credit", "RON", Decimal("80.00"))]


def test_chart_cash_evidence_and_all(world, ledger, home):
    tmp_path = home / "NORD"
    tmp_path.mkdir(exist_ok=True)
    co = companies.get("NORD")
    parsed = {}
    first, last = date(2026, 9, 1), date(2026, 9, 30)
    for i, a in enumerate(co.current_accounts):
        path = tmp_path / f"statement-{i}"
        path.write_text("invented")
        account = statements.Account(a.iban, a.currency, [], closing=Decimal("12.34"), closing_day=last)
        parsed[path] = statements.Statement("NORD", first, last, [account])
        ledger.add_document(company="NORD", month="2026-09", kind="statement", sha=f"chart-{i}", path=str(path))
    parse = lambda path, co: parsed[path.resolve()]  # noqa: E731
    charts, _ = home_charts.company_view(
        ledger, "NORD", co, TODAY, parse, [], [], [], [], [], api.received_owed, api.issued_owed, [], [], {}
    )
    september, october = charts["months"][-2:]
    assert len(charts["months"]) == 12
    assert september["cash_status"] == "complete"
    assert september["net"] == september["observed"]["net"]
    assert october["cash_status"] == "partial" and october["net"] is None and october["in_progress"]
    assert charts["balance_total"] == {**_money("12.34", ("EUR", "12.34")), "partial": False, "day": "2026-09-30"}
    assert all(s["points"] == [{"day": "2026-09-30", "balance": "12.34"}] for s in charts["balance_history"])
    # Missing evidence in one firm makes ALL partial, keeping observed money separate.
    unknown, _ = home_charts.company_view(
        ledger,
        "STUDIO9",
        companies.get("STUDIO9"),
        TODAY,
        parse,
        [],
        [],
        [],
        [],
        [],
        api.received_owed,
        api.issued_owed,
        [],
        [],
        {},
    )
    merged = home_charts.merge([charts, unknown])
    assert merged["months"][-2]["net"] is None
    assert merged["balance_total"]["partial"]


@pytest.mark.parametrize(
    "partial,trustworthy,end,status",
    [
        (False, True, 30, "complete"),
        (True, True, 30, "none"),
        (False, False, 30, "none"),
        (False, True, 29, "partial"),
    ],
)
def test_chart_refuses_unproven_month(ledger, home, partial, trustworthy, end, status):
    tmp_path = home / "NORD"
    tmp_path.mkdir(exist_ok=True)
    co = replace(companies.get("NORD"), accounts=(companies.get("NORD").current_accounts[0],))
    path = tmp_path / "statement"
    path.write_text("invented")
    ledger.add_document(company="NORD", month="2026-09", kind="statement", sha="chart", path=str(path))
    st = statements.Statement(
        "NORD",
        date(2026, 9, 1),
        date(2026, 9, end),
        [statements.Account(RON, "RON", [], trustworthy=trustworthy)],
        partial=partial,
    )
    got = home_charts.cash(ledger, "NORD", co, TODAY, lambda *args: st)[0]["months"][-2]
    assert got["cash_status"] == status
    assert (got["in"] is not None) == (status == "complete")


def test_chart_period_labels_use_every_month(world, ledger):
    s = api.state(ledger, "ALL")
    assert lists.summary(ledger, "ALL")["dashboard"]["charts"] == s["dashboard"]["charts"]
    for part in [s, *s["parts"]]:
        d = part["dashboard"]
        assert d["periods"]["half_year"]["from"] == "2026-05-01"
        assert d["periods"]["trailing_year"]["from"] == "2025-11-01"
        assert d["periods"]["month"]["cash_status"] == "partial"


def test_chart_statement_cannot_read_another_firms_file(ledger, home):
    other = home / "STUDIO9" / "statement"
    other.parent.mkdir(exist_ok=True)
    other.write_text("invented")
    ledger.add_document(company="NORD", month="2026-09", kind="statement", sha="escape", path=str(other))
    calls = []
    got, _ = home_charts.cash(ledger, "NORD", companies.get("NORD"), TODAY, lambda *args: calls.append(args))
    assert not calls
    assert got["months"][-2]["cash_status"] == "none"
    assert got["balance_total"]["partial"]


def test_chart_parsing_is_bounded_and_cache_survives_many_accounts(ledger, home, monkeypatch):
    root = home / "NORD"
    root.mkdir(exist_ok=True)
    calls = []
    for i in range(80):
        path = root / f"statement-{i}"
        path.write_text(f"invented-{i}")
        ledger.add_document(
            company="NORD",
            month="2026-09" if i < 70 else "2024-01",
            kind="statement",
            sha=f"bounded-{i}",
            path=str(path),
        )
    st = statements.Statement("NORD", date(2026, 9, 1), date(2026, 9, 30), [])
    monkeypatch.setattr(api, "_STATEMENTS", {})

    def parse(path, co):
        calls.append(path.read_text())
        return st

    monkeypatch.setattr(statements, "parse", parse)
    for _ in range(2):
        home_charts.cash(ledger, "NORD", companies.get("NORD"), TODAY, api._statement)
    assert len(calls) == 70 and len(api._STATEMENTS) == 70


def test_chart_pdf_parser_uses_checked_bytes(tmp_path, monkeypatch):
    path = tmp_path / "statement.pdf"
    path.write_bytes(b"%PDF-invented")
    monkeypatch.setattr(api, "_STATEMENTS", {})
    monkeypatch.setattr(statements, "pdf_text", lambda data: data.decode())
    monkeypatch.setattr(statements, "parse_text", lambda text, co: (text, co))
    assert api._statement(path, "NORD") == ("%PDF-invented", "NORD")


def test_generated_chart_evidence_matches_account_balances(world, ledger, home):
    scenario.chart_fixtures(ledger, home, TODAY)
    for co in ("NORD", "STUDIO9", "ALL"):
        state = api.state(ledger, co)
        for part in state.get("parts", [state]):
            assert all(r["status"] == "sent" for r in part["reports"] if r["month"] < "2026-09")
        d = state["dashboard"]
        charts = d["charts"]
        for side in ("receivables", "payables"):
            assert all(bucket["count"] > 0 for bucket in charts["aging"][side]["buckets"])
        assert len(charts["vat"]) == 2
        assert [m["cash_status"] for m in charts["months"]].count("complete") == 8
        assert charts["months"][-1]["in_progress"]
        assert charts["months"][-5]["cash_status"] == "partial"
        assert not charts["balance_total"]["partial"]
        assert charts["balance_total"]["day"] == "2026-09-30"
        ron = sum((Decimal(b["balance"]) for b in d["balances"] if b["currency"] == "RON"), Decimal(0))
        assert charts["balance_total"]["total"] == f"{ron:.2f}"
        for account in d["balances"]:
            series = next(
                s
                for s in charts["balance_history"]
                if all(s[k] == account[k] for k in ("company", "iban_last4", "currency", "bank"))
            )
            assert series["points"][-1] == {"day": account["day"], "balance": account["balance"]}


def test_chart_aging_keeps_manual_marks_apart():
    base = dict(
        total="100.00", left="100.00", covered="0.00", currency="RON", status="open", proved=False, payment_state=None
    )
    days = ("2026-10-30", "2026-10-26", "2026-09-26", "2026-08-26", None)
    rows = [base | {"due_date": day} for day in days]
    rows += [base | {"due_date": days[1], "status": "marked", "covered": "10.00"}]
    for side in ("receivables", "payables"):
        owed = (
            api.issued_owed if side == "receivables" else lambda r: api.received_owed(r["status"], r["payment_state"])
        )
        got = home_charts.aging(rows, side, TODAY, owed)
        assert [(b["bucket"], b["count"], b["total"]) for b in got["buckets"]] == [
            (k, 1, "100.00") for k in home_charts.AGING
        ]
        assert got["confirming"] == {**_money("90.00"), "count": 1}
    pending = base | {"due_date": days[1], "status": "linked", "payment_state": "imported"}
    got = home_charts.aging([pending], "payables", TODAY, lambda r: api.received_owed(r["status"], r["payment_state"]))
    assert all(b["count"] == 0 for b in got["buckets"])
    assert got["confirming"] == {**_money("100.00"), "count": 1}


def test_chart_turnover_credit_date_and_proved_partial_collections():
    charts = {"months": [{"month": "2026-09"}, {"month": "2026-10"}]}
    entries = [
        (None, "sales", "2026-09-01", "RON", Decimal("100"), False),
        (None, "sales", "2026-10-01", "RON", Decimal("-20"), False),
        (None, "expenses", "2026-10-01", "EUR", Decimal("50"), False),
    ]
    home_charts.invoice_months(charts, entries, TODAY)
    base = dict(total="100.00", credited="0.00", currency="RON", collected_on="2026-10-02", how="statement")
    txs = [
        {"date": day, "explanation": {"kind": "collection", "link": {"parts": [{"id": "i", "amount": amount}]}}}
        for day, amount in (("2026-09-15", "20.00"), ("2026-10-02", "80.00"))
    ]
    invoices = [dict(id="bill", total="50.00", currency="EUR", credits=[], proved=False, paid_at="2026-10-02")]
    home_charts.settlements(
        charts, txs, [base | {"id": "i"}, base | {"id": "manual", "how": "manual"}], invoices, {}, {}, {}, [], TODAY
    )
    sep, oct_ = charts["months"]
    assert sep["invoiced"] == _money("100.00")
    assert oct_["invoiced"] == _money("-20.00")
    assert sep["collected"] == _money("20.00") and oct_["collected"] == _money("80.00")
    assert oct_["received"] == _money("0.00", ("EUR", "50.00"))
    assert oct_["paid"] == _money("0.00")


@pytest.mark.parametrize("batch", [False, True])
def test_chart_collection_uses_proof_amount_before_later_storno(batch):
    charts = {"months": [{"month": "2026-10"}]}
    row = dict(id="i", currency="RON", total="100.00", credited="20.00", how="statement", collected_on="2026-10-02")
    report = dict(
        id="r", status="collected", credit={"date": "2026-10-02"}, lines=[{"invoice": "i", "gross": "100.00"}]
    )
    tx = dict(
        date="2026-10-02",
        currency="RON",
        amount="95.00" if batch else "100.00",
        explanation={"kind": "collection", "issued": ["i"], "settlement": "r" if batch else None},
    )
    home_charts.settlements(charts, [tx], [row], [], {}, {}, {}, [report] if batch else [], TODAY)
    assert charts["months"][0]["collected"] == _money("100.00")


@pytest.mark.parametrize("proof,paid", [("old-debit", "0.00"), ("bon:receipt", "50.00"), ("ef-proof:mail", "50.00")])
def test_chart_missing_bank_evidence_never_uses_match_date(proof, paid):
    charts = {"months": [{"month": "2026-10"}]}
    invoice = dict(id="bill", total="50.00", currency="RON", credits=[], proved=True, paid_at="2026-10-05T12:00:00")
    home_charts.settlements(charts, [], [], [invoice], {}, {"efactura bill": proof}, {}, [], TODAY)
    assert charts["months"][0]["paid"] == _money(paid)


def test_chart_vat_uses_declared_taxes_and_labels_missing_evidence(ledger, home):
    for mid, currency, total, day in (
        ("701", "RON", "105.00", 10),
        ("702", "EUR", "205.00", 10),
        ("703", "RON", "50.00", 10),
        ("704", "RON", "-21.00", 9),
        ("709", "RON", "105.00", 10),
    ):
        issue = date(2026, day, 12)
        scenario._ef(ledger, mid, "NORD", "Furnizor TVA Exemplu", "900001", mid, issue, None, total, currency)
    ledger.db.execute("UPDATE efactura SET doc_type='credit_note',status='credit' WHERE message_id='704'")
    ledger.db.execute("UPDATE efactura SET status='skipped' WHERE message_id='709'")
    ledger.db.commit()
    for mid, tax in (("701", "5.00"), ("702", "5.00"), ("704", "1.00"), ("709", "5.00")):
        scenario.chart_tax_document(ledger, "efactura", ledger.efactura(mid), home, Decimal(tax))
    scenario._issued(
        ledger, "705", "NORD", "V-705", "Client TVA Exemplu", "900002", date(2026, 10, 12), None, "201.00", "EUR"
    )
    scenario.chart_tax_document(ledger, "issued", ledger.issued("705"), home, Decimal("1.00"))
    local = {"id": "local", "data": {"issue_date": "2026-10-12", "currency": "RON", "totals": {"vat": "9.00"}}}
    for mid in ("706", "707"):
        scenario._issued(
            ledger, mid, "NORD", "LOCAL-1", "Client TVA Exemplu", "900002", date(2026, 10, 12), None, "109.00"
        )
    copies = [{"id": mid, "local_id": "local"} for mid in ("706", "707", "local")]
    previous, current = home_charts.vat_estimate(
        ledger, "NORD", TODAY, copies, [local], [], {mid: "local" for mid in ("706", "707")}
    )
    assert previous["month"] == "2026-09" and not previous["in_progress"]
    assert previous["deductible"] == _money("-1.00") and previous["net"] == _money("1.00")
    assert current["estimate"] and current["basis"] == "recorded_invoices" and current["in_progress"]
    assert current["collected"] == _money("9.00", ("EUR", "1.00"))
    assert current["deductible"] == _money("5.00", ("EUR", "5.00"))
    assert current["net"] == _money("4.00", ("EUR", "-4.00"))
    assert current["partial"] and current["missing_count"] == 1


@pytest.mark.parametrize("bad", ["other_firm", "changed", "DTD", "no_tax"])
def test_chart_vat_refuses_untrusted_or_unidentified_tax(ledger, home, bad):
    scenario._ef(ledger, "701", "NORD", "Furnizor TVA Exemplu", "900001", "V-701", date(2026, 10, 12), None, "105.00")
    scenario.chart_tax_document(ledger, "efactura", ledger.efactura("701"), home, Decimal("5.00"))
    row = ledger.efactura("701")
    path = Path(row["raw_path"])
    if bad == "other_firm":
        other = home / "efactura" / "STUDIO9" / "other.zip"
        other.parent.mkdir(parents=True, exist_ok=True)
        path.rename(other)
        ledger.db.execute("UPDATE efactura SET raw_path=? WHERE message_id='701'", (str(other),))
    else:
        xml = api.efactura.xml_from_zip(path.read_bytes())
        xml = (
            b'<!DOCTYPE Invoice [<!ENTITY x "999">]>' + xml
            if bad == "DTD"
            else xml.replace(b">5.00</cbc:TaxAmount>", b">7.00</cbc:TaxAmount>")
            if bad == "changed"
            else xml.replace(b"TaxAmount", b"UnknownAmount")
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("invoice.xml", xml)
        if bad != "changed":
            ledger.db.execute(
                "UPDATE efactura SET xml_sha=? WHERE message_id='701'", (hashlib.sha256(xml).hexdigest(),)
            )
    ledger.db.commit()
    current = home_charts.vat_estimate(ledger, "NORD", TODAY, [], [], [], {})[-1]
    assert current["deductible"] == _money("0.00")
    assert current["partial"] and current["missing_count"] == 1


def test_chart_vat_local_storno_is_not_a_missing_external_document(ledger):
    scenario._issued(
        ledger,
        "708",
        "NORD",
        "ST-1",
        "Client TVA Exemplu",
        "900002",
        date(2026, 10, 12),
        None,
        "-109.00",
        doc_type="credit_note",
    )
    local = {"id": "local-storno", "data": {"issue_date": "2026-10-12", "currency": "RON", "totals": {"vat": "-9.00"}}}
    shown = [{"id": "local-storno", "local_id": "local-storno", "storno_of": "original"}]
    current = home_charts.vat_estimate(ledger, "NORD", TODAY, shown, [local], [], {"708": "local-storno"})[-1]
    assert current["collected"] == current["net"] == _money("-9.00")
    assert not current["partial"] and current["missing_count"] == 0


def test_chart_product_sales_keep_firm_identity_and_generated_links(world, ledger):
    state = api.state(ledger, "ALL", scenario.NOW)
    sales = state["dashboard"]["charts"]["product_sales"]
    assert [s["company"] for s in sales] == ["NORD", "STUDIO9"]
    linked = next(r for r in sales[0]["totals"] if r["product_id"] == "catalog-design")
    assert (linked["revenue"], linked["quantity"], linked["invoice_count"]) == ("200.00", "2", 4)
    period = state["dashboard"]["periods"]["month"]["product_sales"]
    assert [r["company"] for r in period] == ["NORD", "STUDIO9"]
    assert linked in period[0]["totals"] and len(sales[0]["months"]) == 12
