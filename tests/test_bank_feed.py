"""The daily read of a connected bank, and the same payment from a statement and from the bank counted once.
No network: Enable Banking is a fake. Synthetic data only."""

import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from decimal import Decimal

import pytest
from firms import set_firm
from payment_cases import _op, download_batch

from fiscalm import companies, recon
from fiscalm.bank import coverage, enablebanking as eb, feed as bank_feed, statements
from fiscalm.ledger import Ledger, now
from fiscalm.payments import engine as pay

RON = "RO24BTRLRONCRT0000000001"


def raw(ref="E1", amount="100.00", day="2026-10-27", flow="DBIT", status="BOOK", ccy="RON", iban=None, name="Payee"):
    other = "creditor" if flow == "DBIT" else "debtor"
    return {
        "entry_reference": ref,
        "transaction_amount": {"amount": amount, "currency": ccy},
        "credit_debit_indicator": flow,
        "status": status,
        "booking_date": day,
        "value_date": day,
        other: {"name": name},
        other + "_account": {"iban": iban or ""},
        "remittance_information": ["Plata", "OP 1"],
    }


def test_only_booked_lines_in_the_accounts_currency_are_read():
    t = bank_feed.to_tx(raw(iban="ro49 aaaa 1b31 0075 9384 0000"), RON, "RON")
    assert (t.direction, t.amount, t.ref, t.counterparty, t.counterparty_iban, t.description, t.date) == (
        "debit",
        Decimal("100.00"),
        "eb:E1",
        "Payee",
        "RO49AAAA1B31007593840000",
        "Plata OP 1",
        date(2026, 10, 27),
    )
    assert bank_feed.to_tx(raw(flow="CRDT"), RON, "RON").direction == "credit"
    for bad in (
        raw(status="PDNG"),
        raw(ccy="EUR"),
        raw(amount="-5"),
        raw(amount="x"),
        *[raw(amount=a) for a in ("NaN", "sNaN", "Infinity", "1E100", "0.001")],
        raw() | {"transaction_amount": ["malformed"]},
        raw(ref=""),
        raw(flow="?"),
        raw(day="2026-13-01"),
        "nope",
        {},
    ):
        assert bank_feed.to_tx(bad, RON, "RON") is None
    later = raw() | {"remittance_information": ["Plata", "OP 1", "detalii adaugate de banca"]}
    # the bank's reference is the identity, whatever text it adds later
    assert statements.tx_key(bank_feed.to_tx(raw(), RON, "RON")) == statements.tx_key(
        bank_feed.to_tx(later, RON, "RON")
    )


class Api:
    def __init__(self, txs=None, error=None):
        self.calls, self.txs, self.error = [], txs or [], error

    def transactions(self, uid, start, end):
        self.calls.append((uid, start, end))
        if self.error:
            raise self.error
        return self.txs


def connection(ledger, accounts=((RON, "u1"),), last_read=None, valid_until="2027-02-01"):
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO bank_connections(id,company,bank,provider,accounts,valid_until,created_at,last_read) "
            "VALUES('bc-0000000000000001','NORD','bt','enablebanking',?,?,?,?)",
            (json.dumps([{"iban": i, "uid": u} for i, u in accounts]), valid_until, now(), last_read),
        )
    return {"id": "bc-0000000000000001"}


def test_the_first_read_goes_back_89_days_later_ones_overlap_5(ledger):
    api = Api()
    bank_feed.read(ledger, connection(ledger), date(2026, 11, 3), api)
    assert api.calls == [("u1", "2026-08-06", "2026-11-03")]
    ledger.db.execute("UPDATE bank_connections SET last_read='2026-11-02'")
    ledger.db.commit()
    bank_feed.read(ledger, {"id": "bc-0000000000000001"}, date(2026, 11, 3), api)
    assert api.calls[-1] == ("u1", "2026-10-28", "2026-11-03")


def test_an_account_retired_since_it_was_connected_is_not_read(ledger, monkeypatch):
    nord = companies.get("NORD")
    set_firm(
        "NORD",
        dataclasses.replace(
            nord,
            accounts=tuple(
                dataclasses.replace(a, pays=False, retired="2026-10-01") if a.iban == RON else a for a in nord.accounts
            ),
        ),
    )
    api = Api()
    st, _ = bank_feed.read(ledger, connection(ledger), date(2026, 11, 3), api)
    assert api.calls == [] and st.accounts == []


def test_a_read_matches_a_payment_and_is_done_once_a_day(ledger, home, live):
    download_batch(ledger, [_op(ledger, 1)])
    (p,) = ledger.payments(company="NORD")
    api = Api([raw(amount=p["amount"], iban=p["payee_iban"], name=p["payee_name"])])
    connection(ledger)
    (out,) = bank_feed.run_all(ledger, date(2026, 11, 3), api)
    assert out["status"] == "read" and out["payments_marked_paid"] == 1
    assert ledger.payments(company="NORD")[0]["status"] == "paid"
    assert bank_feed.run_all(ledger, date(2026, 11, 3), api) == []  # read today already
    assert len(api.calls) == 1


def test_the_feed_covers_what_it_read_and_releases_payments_once_past_the_month(ledger):
    """Each account from the first day read of it to a few days before the last read (late bookings); an account
    added on reconnecting from its own first read."""
    connection(ledger)
    bank_feed.run_all(ledger, date(2026, 11, 3), Api())  # read 6 August to 3 November: October may still change
    assert coverage.gaps(ledger, "NORD", RON, date(2026, 8, 1), date(2026, 10, 31)) == [
        (date(2026, 8, 1), date(2026, 8, 5)),
        (date(2026, 10, 30), date(2026, 10, 31)),
    ]
    assert pay.missing_statement(ledger, "NORD", date(2026, 11, 3)) == "2026-10"
    bank_feed.run_all(ledger, date(2026, 11, 5), Api(error=eb.BankError("x", "ASPSP_RATE_LIMIT_EXCEEDED")))
    assert pay.missing_statement(ledger, "NORD", date(2026, 11, 5)) == "2026-10"  # a refused read covers nothing
    eur = companies.get("NORD").ibans["EUR"]
    with ledger.tx() as db:  # reconnected with the EUR account too
        db.execute(
            "UPDATE bank_connections SET accounts=?",
            (json.dumps([{"iban": RON, "uid": "u1"}, {"iban": eur, "uid": "u2"}]),),
        )
    bank_feed.run_all(ledger, date(2026, 11, 6), Api())
    assert pay.missing_statement(ledger, "NORD", date(2026, 11, 6)) == ""
    assert coverage.gaps(ledger, "NORD", eur, date(2026, 10, 1), date(2026, 11, 1)) == [
        (date(2026, 10, 1), date(2026, 10, 28))  # read from 29 October (the last read's 3rd, less 5 days)
    ]
    # a booked line fiscalm can't read (no bank reference): the feed proves nothing up to its day
    # (nor one with an empty status: only a status the bank gives for unbooked lines is left out)
    unread = [raw(ref="", day="2026-10-20"), raw(status="", day="2026-10-25"), raw(status="PDNG", day="2026-10-28")]
    bank_feed.run_all(ledger, date(2026, 11, 7), Api(unread))
    assert coverage.gaps(ledger, "NORD", RON, date(2026, 10, 1), date(2026, 10, 31)) == [
        (date(2026, 10, 1), date(2026, 10, 25))
    ]


@pytest.mark.parametrize("read_since", ["2026-08-06", None])
def test_a_month_the_feed_read_from_its_first_day_is_its_own_whatever_day_its_first_line_comes(
    ledger, home, live, read_since
):
    """Quiet until the 20th: that read starts on the 14th, yet the feed has read October from the 1st, so its debit
    settles the payment. A feed that can't show it read the 1st leaves the month to a statement, and doesn't cover it."""
    download_batch(ledger, [_op(ledger, 1)])
    (p,) = ledger.payments(company="NORD")
    connection(ledger, last_read="2026-10-19")
    with ledger.tx() as db:
        db.execute("UPDATE bank_connections SET covered=?", (json.dumps({RON: read_since} if read_since else {}),))
    paid = Api([raw(amount=p["amount"], iban=p["payee_iban"], name=p["payee_name"], day="2026-10-20")])
    bank_feed.run_all(ledger, date(2026, 10, 20), paid)
    assert (ledger.payments(company="NORD")[0]["status"] == "paid") == bool(read_since)
    bank_feed.run_all(ledger, date(2026, 11, 6), Api())
    assert pay.missing_statement(ledger, "NORD", date(2026, 11, 6)) == ("" if read_since else "2026-10")


@pytest.mark.parametrize("feed_first", [True, False])
def test_a_statement_covers_only_months_the_feed_does_not_settle(ledger, feed_first):
    """In the feed's month a CSV's lines are kept from the matchers, so its days can't show a payment already made."""
    if feed_first:
        record(ledger, [line("2026-10-02", ref="eb:1")], "ob")  # the feed took October first
    ledger.add_document("c" * 64, "NORD", "2026-10", "statement", "/x/october.csv", accounts=[RON])
    coverage.record(ledger, "c" * 64, {RON: ["2026-10-01", "2026-10-31", False]})
    october = coverage.gaps(ledger, "NORD", RON, date(2026, 10, 1), date(2026, 10, 31))
    assert october == ([(date(2026, 10, 1), date(2026, 10, 31))] if feed_first else [])


@pytest.mark.parametrize(
    "why, kept_from, twin_from, twin_day",
    [
        ("twin csv", "file", "file", "2026-10-10"),  # a full statement's line on a CSV day: the CSV's, that day
        ("twin ob", "ob", "file", "2026-10-13"),  # the feed's line in a statement's month: the statement's
    ],
)
def test_a_line_kept_from_the_matchers_as_a_twin_with_no_twin_on_record_leaves_its_day_uncovered(
    ledger, why, kept_from, twin_from, twin_day
):
    """It may be a payment nothing matched: its day waits until its twin, from the other source, is on record; a line
    of its own source a day later is another payment, not its twin."""
    record(ledger, [line("2026-10-02", ref="a")], "file")
    kept = line("2026-10-10", "70.00", "kept")
    record(ledger, [kept], kept_from)
    with ledger.tx() as db:
        db.execute("INSERT OR REPLACE INTO settled_tx VALUES(?,?,?)", (statements.tx_key(kept), why, now()))
    ledger.add_document("s" * 64, "NORD", "2026-10", "statement", "/x/october.xml", accounts=[RON])
    coverage.record(ledger, "s" * 64, {RON: ["2026-10-01", "2026-10-31", True]})
    october = date(2026, 10, 1), date(2026, 10, 31)
    other = line("2026-10-11", "70.00", "another payment")
    record(ledger, [other], kept_from)
    with ledger.tx() as db:  # matched as its own payment: free, yet of the same source, so no twin of the kept line
        db.execute("DELETE FROM settled_tx WHERE tx_key=?", (statements.tx_key(other),))
    assert coverage.gaps(ledger, "NORD", RON, *october) == [(date(2026, 10, 10), date(2026, 10, 10))]
    record(ledger, [line(twin_day, "70.00", "its twin")], twin_from)
    assert coverage.gaps(ledger, "NORD", RON, *october) == []


def test_twins_pair_so_that_no_pairing_choice_leaves_one_out():
    """A reserved line that took a twin another one needs moves to its other twin; thousands of identical lines
    pair without running out of stack."""
    assert coverage._unpaired([["a", "b"], ["a"]]) == []  # the first takes "a", then gives it up for "b"
    assert coverage._unpaired([["a"], ["a"]]) == [1]
    many = [[f"free{j}" for j in range(1500)] for _ in range(1500)]
    assert coverage._unpaired(many) == []


def test_twins_pair_on_the_ledger(ledger):
    """Feed lines on the 2nd and 6th, the statement's on the 5th and 8th: 5-2 and 8-6 pair both (5-6 would leave 8)."""
    record(ledger, [line(d, "70.00", "eb:" + d) for d in ("2026-10-02", "2026-10-06")], "ob")
    kept = [line(d, "70.00", "st:" + d) for d in ("2026-10-05", "2026-10-08")]
    record(ledger, kept, "file")
    with ledger.tx() as db:
        for t in kept:
            db.execute("INSERT OR REPLACE INTO settled_tx VALUES(?,?,?)", (statements.tx_key(t), "twin file", now()))
    assert unproven(ledger, date(2026, 10, 1), date(2026, 10, 31)) == []


def unproven(ledger, first, last):
    return coverage._unproven(coverage._Record.read(ledger, "NORD", RON), first, last)


def kept(ledger, t, why):
    with ledger.tx() as db:
        db.execute("INSERT OR REPLACE INTO settled_tx VALUES(?,?,?)", (statements.tx_key(t), why, now()))


def test_a_chain_of_lines_competing_for_twins_is_seen_whole(ledger):
    """The statement's Oct 31, Nov 3 and Nov 9 and the feed's Nov 3 and 6: November's two take both twins, so
    October's last day waits, however far the chain runs past the month."""
    feed = [line(d, "70.00", "eb:" + d) for d in ("2026-11-03", "2026-11-06")]
    record(ledger, feed, "ob")
    statement = [line(d, "70.00", "st:" + d) for d in ("2026-10-31", "2026-11-03", "2026-11-09")]
    record(ledger, statement, "file")
    for t in statement:
        kept(ledger, t, "twin file")
    assert unproven(ledger, date(2026, 10, 1), date(2026, 10, 31)) == [date(2026, 10, 31)]


@pytest.mark.parametrize("case", ["copy", "conflict", "just after"])
def test_a_line_no_twin_stands_for_leaves_its_day_uncovered(ledger, case):
    """The feed's line that a copy of the same bank reference already stands for is no twin of another; a line whose
    reference carries conflicting money waits for a person; a line just past the month keeps its own twin."""
    october = date(2026, 10, 1), date(2026, 10, 31)
    feed = line("2026-10-10", "70.00", "eb:1")
    record(ledger, [feed], "ob")
    if case == "copy":
        copy, missed = line("2026-11-08", "70.00", "st:1"), line("2026-10-11", "70.00", "st:2")  # copy weeks later
        record(ledger, [copy, missed], "file")
        kept(ledger, copy, "duplicate:" + statements.tx_key(feed))
        kept(ledger, missed, "twin file")
        assert unproven(ledger, *october) == [date(2026, 10, 11)]
    elif case == "conflict":
        kept(ledger, feed, "bank reference conflict")
        assert unproven(ledger, *october) == [date(2026, 10, 10)]
    else:
        orphan, after = (line(d, "70.00", "eb:" + d) for d in ("2026-10-31", "2026-11-02"))
        record(ledger, [orphan, after], "ob")
        record(ledger, [line("2026-11-02", "70.00", "st:nov")], "file")
        kept(ledger, orphan, "twin ob")
        kept(ledger, after, "twin ob")
        assert unproven(ledger, *october) == [date(2026, 10, 31)]


def line(day, amount="50.00", ref="R", words="x", flow="debit"):
    return statements.Tx(
        date=date.fromisoformat(day),
        title=words,
        description=words,
        direction=flow,
        amount=Decimal(amount),
        currency="RON",
        iban=RON,
        ref=ref,
    )


def record(ledger, txs, source):
    ledger.record_statement(
        statements.Statement("NORD", None, None, [statements.Account(RON, "RON", txs)], source=source)
    )


def usable(ledger, txs):
    used = ledger.used_txs()
    return [statements.tx_key(t) not in used for t in txs]


@pytest.mark.parametrize("first", ["ob", "file"])
def test_one_source_settles_an_accounts_month_whatever_the_order_dates_or_words(ledger, first):
    second = "file" if first == "ob" else "ob"
    a = [
        line("2026-10-27", ref="eb:A1" if first == "ob" else "F1"),
        line("2026-10-27", ref="eb:A2" if first == "ob" else "F2"),
    ]
    b = [
        line("2026-10-28", ref="eb:B1" if second == "ob" else "G1", words="other"),  # a day later, other words
        line("2026-10-12", ref="eb:B2" if second == "ob" else "G2", amount="99.00"),
    ]
    record(ledger, a, first)
    record(ledger, b, second)
    assert usable(ledger, a) == [True, True]  # the month's owner: two equal payments stay two
    assert usable(ledger, b) == [False, False]  # the other source: evidence only, settles nothing
    record(ledger, a + b, first)  # reading again changes nothing
    assert usable(ledger, a) == [True, True] and usable(ledger, b) == [False, False]


def test_interleaved_arrivals_keep_every_payment_of_the_owner(ledger):
    ob1, ob2 = line("2026-10-27", ref="eb:1"), line("2026-10-27", ref="eb:2")
    f1, f2 = line("2026-10-27", ref="F1"), line("2026-10-27", ref="F2")
    for txs, src in (([ob1], "ob"), ([f1], "file"), ([ob2], "ob"), ([f2], "file")):
        record(ledger, txs, src)
    assert usable(ledger, [ob1, ob2, f1, f2]) == [True, True, False, False]


def test_a_statement_stored_before_owns_its_month_against_the_bank(ledger):
    ledger.add_document("old", "NORD", "2026-10", "statement", "/x/old.pdf")  # before accounts or day were recorded
    bank = [line("2026-10-27", ref="eb:1")]
    record(ledger, bank, "ob")
    assert usable(ledger, bank) == [False]


def test_richer_statement_evidence_of_a_bank_owned_month_waits_for_a_person(ledger, home):
    """Known trade-off: the bank's line lacked the payee's IBAN, the statement's has it, but the month is the bank's:
    the payment stays to confirm by hand rather than risk settling twice."""
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    download_batch(ledger, [_op(ledger, 1)])
    (p,) = ledger.payments(company="NORD")
    connection(ledger)
    bank_feed.run_all(ledger, date(2026, 11, 3), Api([raw(amount=p["amount"])]))  # no IBAN on the bank's line
    rich = statements.Tx(
        date=date(2026, 10, 27),
        title="OP",
        description="OP",
        direction="debit",
        amount=Decimal(p["amount"]),
        currency="RON",
        iban=RON,
        ref="F1",
        counterparty_iban=p["payee_iban"],
    )
    assert (
        recon.auto_mark_paid(ledger, statements.Statement("NORD", None, None, [statements.Account(RON, "RON", [rich])]))
        == []
    )
    assert ledger.payments(company="NORD")[0]["status"] == "exported"


def test_a_refusal_or_an_ended_consent_is_kept_on_the_connection(ledger):
    connection(ledger)
    (out,) = bank_feed.run_all(ledger, date(2026, 11, 3), Api(error=eb.BankError("x", "ASPSP_RATE_LIMIT_EXCEEDED")))
    assert out == {"connection": "bc-0000000000000001", "status": "error", "code": "ASPSP_RATE_LIMIT_EXCEEDED"}
    row = ledger.db.execute("SELECT last_error, last_read, attempted_on FROM bank_connections").fetchone()
    assert tuple(row) == ("ASPSP_RATE_LIMIT_EXCEEDED", None, "2026-11-03")  # tried today, never read
    api = Api()
    assert bank_feed.run_all(ledger, date(2026, 11, 3), api) == [] and api.calls == []  # not tried twice a day
    ledger.db.execute("UPDATE bank_connections SET valid_until='2026-11-01'")
    ledger.db.commit()
    api = Api()
    (out,) = bank_feed.run_all(ledger, date(2026, 11, 3), api)
    assert out["status"] == "expired" and api.calls == []


def test_a_failed_day_loses_nothing_the_next_read_starts_from_the_last_that_worked(ledger):
    connection(ledger, last_read="2026-10-20")
    bank_feed.run_all(ledger, date(2026, 11, 3), Api(error=eb.BankError("x", "ASPSP_RATE_LIMIT_EXCEEDED")))
    api = Api()
    (out,) = bank_feed.run_all(ledger, date(2026, 11, 4), api)
    assert out["status"] == "read" and api.calls == [("u1", "2026-10-15", "2026-11-04")]  # from 10-20, not 11-03
    assert ledger.db.execute("SELECT last_read FROM bank_connections").fetchone()[0] == "2026-11-04"


def test_the_bank_owns_only_months_it_read_from_their_first_day(ledger):
    """The first read starts mid-August: August stays the statement's, so its early payments are not lost."""
    bank_aug, bank_sep = line("2026-08-10", ref="eb:1"), line("2026-09-10", ref="eb:2")
    ledger.record_statement(
        statements.Statement(
            "NORD",
            date(2026, 8, 6),
            date(2026, 11, 3),
            [statements.Account(RON, "RON", [bank_aug, bank_sep])],
            source="ob",
        )
    )
    file_aug = [line("2026-08-01", amount="80.00", ref="F1"), line("2026-08-10", ref="F2")]
    record(ledger, file_aug, "file")
    assert usable(ledger, [bank_aug, bank_sep]) == [False, True]  # August: evidence only; September: the bank's
    assert usable(ledger, file_aug) == [True, True]


@pytest.mark.parametrize("account_known", [True, False])
def test_the_edge_guard_holds_only_twins_within_three_days(ledger, account_known):
    """A payment dated across a month edge by the two sources (October the statement's, November the bank's), also
    against a line recorded before accounts were kept (which account is unknown)."""
    record(ledger, [line("2026-10-28", amount="70.00", ref="F1"), line("2026-10-30", amount="71.00", ref="F2")], "file")
    if not account_known:
        ledger.db.execute("UPDATE statement_txs SET iban=NULL")
        ledger.db.commit()
    nov = [
        line("2026-11-02", amount="70.00", ref="eb:1"),  # 5 days from its look-alike: another payment
        line("2026-11-02", amount="71.00", ref="eb:2"),  # 3 days: maybe the same one, held
        line("2026-11-01", amount="72.00", ref="eb:3"),
    ]  # no look-alike
    record(ledger, nov, "ob")
    assert usable(ledger, nov) == [True, False, True]


@pytest.mark.parametrize(
    "extra,kind",
    [
        ({"bank_transaction_code": {"code": "ACCB", "sub_code": "CHRG"}}, statements.BANK_FEE),
        ({"bank_transaction_code": {"code": "ACCB", "sub_code": "INTR"}}, statements.INTEREST),
        (
            {"remittance_information": ["Comision plata OP"]},
            statements.BANK_FEE,
        ),  # no code: BT's words, this is a BT account
        ({"creditor": {"name": "Magazin Nord", "postal_address": {"country": "RO"}}}, statements.NEEDS_INVOICE),
        ({"creditor": {"name": "Shop Inc"}, "bank_transaction_code": "CHRG"}, statements.NEEDS_INVOICE),
    ],
)
def test_what_the_bank_says_a_line_is(extra, kind):
    assert statements.classify_tx(bank_feed.to_tx(raw() | extra, RON, "RON")) == kind


@pytest.mark.parametrize("first", ["ob", "file"])
def test_bank_reference_counts_copies_once_even_when_booking_date_and_words_change(ledger, first):
    ob = bank_feed.to_tx(raw(ref="bank-1"), RON, "RON")
    file = dataclasses.replace(ob, ref="bank-1|customer-ref", date=date(2026, 11, 5), description="Other words")
    txs = {"ob": ob, "file": file}
    record(ledger, [txs[first]], first)
    record(ledger, [txs["file" if first == "ob" else "ob"]], "file" if first == "ob" else "ob")
    assert sum(usable(ledger, [ob, file])) == 1
    assert len(ledger.account_lines("NORD", "2026-10-01")) == 1
    assert ledger.db.execute("SELECT count(*) FROM statement_txs").fetchone()[0] == 2  # both proofs remain


def test_two_connections_racing_the_same_bank_reference_reserve_only_one_payment(ledger):
    ob = bank_feed.to_tx(raw(ref="bank-race"), RON, "RON")
    file = dataclasses.replace(ob, ref="bank-race|customer-ref", description="Statement")

    def reserve(pair):
        tx, source = pair
        with closing(Ledger()) as db:
            record(db, [tx], source)
            with db.tx() as conn:
                return db._reserve(conn, statements.tx_key(tx), "payment test")

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(reserve, [(ob, "ob"), (file, "file")])) == [False, True]


@pytest.mark.parametrize("change", [{"amount": Decimal("101.00")}, {"currency": "EUR"}, {"direction": "credit"}])
def test_conflicting_bank_reference_stays_visible_and_settles_nothing(ledger, change):
    ob = bank_feed.to_tx(raw(ref="bank-conflict"), RON, "RON")
    file = dataclasses.replace(ob, ref="bank-conflict|customer-ref", **change)
    record(ledger, [ob], "ob")
    record(ledger, [file], "file")
    assert usable(ledger, [ob, file]) == [False, False]
    assert len(ledger.account_lines("NORD", "2026-10-01")) == 2


@pytest.mark.parametrize("first", ["ob", "file"])
def test_reference_copy_does_not_block_the_month_owner_after_a_held_copy(ledger, first):
    record(ledger, [line("2026-10-01", ref="owner")], first)
    ob = bank_feed.to_tx(raw(ref="held-copy"), RON, "RON")
    file = dataclasses.replace(ob, ref="held-copy|customer-ref", date=date(2026, 10, 28))
    copies = {"ob": ob, "file": file}
    other = "file" if first == "ob" else "ob"
    record(ledger, [copies[other]], other)
    record(ledger, [copies[first]], first)
    assert usable(ledger, [copies[other], copies[first]]) == [False, True]
    assert len(ledger.account_lines("NORD", "2026-10-20")) == 1


@pytest.mark.parametrize("first", ["ob", "file"])
def test_two_held_copies_count_once_and_a_later_eligible_proof_can_settle(ledger, first):
    record(ledger, [line("2026-10-01", ref="owner-oct", amount="1.00")], "ob")
    record(ledger, [line("2026-11-15", ref="owner-nov", amount="2.00")], "file")
    ob = bank_feed.to_tx(raw(ref="held-boundary", day="2026-11-02"), RON, "RON")
    file = dataclasses.replace(ob, ref="held-boundary|customer", date=date(2026, 10, 31))
    txs = {"ob": ob, "file": file}
    other = "file" if first == "ob" else "ob"
    record(ledger, [txs[first]], first)
    record(ledger, [txs[other]], other)
    assert usable(ledger, [ob, file]) == [False, False]

    def copies():
        return [
            r
            for r in ledger.account_lines("NORD", "2026-10-20")
            if json.loads(r["tx"]).get("bank_reference") == "held-boundary"
        ]

    assert len(copies()) == 1
    eligible = dataclasses.replace(file, date=date(2026, 11, 16))
    record(ledger, [eligible], "file")
    assert usable(ledger, [ob, file, eligible]) == [False, False, True]
    assert len(copies()) == 1


@pytest.mark.parametrize("change", [{"amount": Decimal("101.00")}, {"currency": "EUR"}, {"direction": "credit"}])
@pytest.mark.parametrize("already_settled", [False, True])
def test_rereading_a_bank_reference_with_changed_money_never_creates_another_payment(ledger, change, already_settled):
    original = bank_feed.to_tx(raw(ref="changed-proof"), RON, "RON")
    record(ledger, [original], "ob")
    if already_settled:
        with ledger.tx() as db:
            assert ledger._reserve(db, statements.tx_key(original), "synthetic payment")
    changed = dataclasses.replace(original, **change)
    record(ledger, [changed], "ob")
    assert usable(ledger, [original, changed]) == [False, False]
    old = ledger.db.execute("SELECT tx FROM statement_txs WHERE tx_key=?", (statements.tx_key(original),)).fetchone()
    assert json.loads(old[0]) == original.to_dict()
    if already_settled:
        assert (
            ledger.db.execute("SELECT what FROM settled_tx WHERE tx_key=?", (statements.tx_key(original),)).fetchone()[
                0
            ]
            == "synthetic payment"
        )


@pytest.mark.parametrize("change", [{"amount": Decimal("101.00")}, {"currency": "EUR"}])
def test_a_mutated_duplicate_also_holds_the_unsettled_canonical_statement_proof(ledger, change):
    ob = bank_feed.to_tx(raw(ref="mutated-duplicate"), RON, "RON")
    file = dataclasses.replace(ob, ref="mutated-duplicate|customer", description="Statement proof")
    record(ledger, [file], "file")
    record(ledger, [ob], "ob")
    assert usable(ledger, [file, ob]) == [True, False]
    record(ledger, [dataclasses.replace(ob, **change)], "ob")
    assert usable(ledger, [file, ob]) == [False, False]


@pytest.mark.parametrize("second_amount", ["100.00", "2.50"])
def test_distinct_lines_of_one_statement_keep_their_money_even_with_a_shared_reference(ledger, second_amount):
    payment = dataclasses.replace(bank_feed.to_tx(raw(ref="shared-ref"), RON, "RON"), ref="file-payment")
    other = dataclasses.replace(payment, amount=Decimal(second_amount), description="Separate statement line")
    for _ in range(2):
        record(ledger, [payment, other], "file")
        assert usable(ledger, [payment, other]) == [True, True]
        assert len(ledger.account_lines("NORD", "2026-10-01")) == 2
