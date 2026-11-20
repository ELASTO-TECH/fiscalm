"""Tranzacții (fiscalm.transactions): every statement transaction with what the engine already knows explains it, and
De explicat for the rest. Read only: it shows settlements, it makes none. Synthetic data only."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
import synthetic_world as scenario

from fiscalm import companies, docs, links, recon, transactions
from fiscalm.api import state as api
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.ledger import Ledger, LinkRefused

TODAY = date(2026, 10, 27)
NORD_RON, NORD_EUR = "RO24BTRLRONCRT0000000001", "RO71BTRLEURCRT0000000001"
STUDIO_RON = "RO94BTRLRONCRT0000000002"


def _tx(
    n: int,
    direction: str = "debit",
    *,
    day: date = TODAY - timedelta(days=5),
    iban: str = NORD_RON,
    amount: str | None = None,
    **kw,
) -> statements.Tx:
    return statements.Tx(
        date=day,
        title=kw.pop("title", "Plata OP"),
        description=kw.pop("description", f"Plata {n}"),
        direction=direction,
        amount=Decimal(amount or f"{n}.00"),
        currency="RON",
        iban=iban,
        ref=kw.pop("ref", f"OP{n}"),
        **kw,
    )


def _record(L, co: str, *txs: statements.Tx, **kw) -> list[str]:
    L.record_statement(
        statements.Statement(co, kw.pop("start", None), None, [statements.Account("x", "RON", list(txs))], **kw)
    )
    return [statements.tx_key(t) for t in txs]


def _view(L, co: str = "NORD", *, settlements=(), ambiguous=(), live=False) -> dict:
    return transactions.view(
        L, companies.get(co), TODAY, settlements=list(settlements), ambiguous=list(ambiguous), live=live
    )


def _rows(L, co: str = "NORD", **kw) -> dict[str, dict]:
    return {r["id"]: r for r in _view(L, co, **kw)["rows"]}


def _invoice(L, mid: str, co: str = "NORD", total: str = "10.00") -> None:
    scenario._ef(L, mid, co, "Furnizor Test SRL", f"RO{mid}", f"FT-{mid}", TODAY - timedelta(days=20), None, total)


def _card(n: int, **kw) -> statements.Tx:
    return _tx(
        n,
        title="Plata la POS non-BT",
        description=f"Plata la POS SHOP{n}",
        counterparty=kw.pop("who", f"SHOP{n} INC"),
        country="US",
        card_last4="0001",
        **kw,
    )


def test_rows_newest_first_only_this_company_s_in_the_window_with_details_and_no_full_iban(ledger):
    other, first_day = "RO49AAAA1B31007593840000", date(2025, 10, 1)  # MONTHS=13: October 2025 to October 2026
    old, new, edge, _ = _record(
        ledger,
        "NORD",
        _tx(12, day=TODAY - timedelta(days=40), counterparty="Furnizor   Test\nSRL", counterparty_iban=other),
        _tx(13, "credit", day=TODAY - timedelta(days=2), description="x" * 300),
        _tx(16, day=first_day),
        _tx(17, day=first_day - timedelta(days=1)),
    )
    _record(ledger, "STUDIO9", _tx(15, iban=STUDIO_RON))
    view = _view(ledger)
    assert [r["id"] for r in view["rows"]] == [new, old, edge] and len(_rows(ledger, "STUDIO9")) == 1
    first, second, _ = view["rows"]
    assert first | {"details": None} == {
        "id": new,
        "company": "NORD",
        "date": (TODAY - timedelta(days=2)).isoformat(),
        "direction": "credit",
        "amount": "13.00",
        "currency": "RON",
        "account": "0001",
        "source": "statement",
        "counterparty": None,
        "details": None,
        "card": None,
        "card_owner": None,
        "orig": None,
        "explanation": None,
        "proposal": None,
        "bank_says": None,
        "awaiting": None,
        "partner": None,
        "can_link": True,
        "remember": {},
    }
    assert first["details"] == "x" * transactions.DETAILS_MAX  # the bank's free text, shortened
    assert second["counterparty"] == "Furnizor Test SRL"  # whitespace folded: shown as text
    assert view["to_explain"] == 3
    shown = json.dumps(view)
    assert other not in shown and NORD_RON not in shown  # accounts only by their last four digits


def test_a_debit_is_explained_by_what_proves_it_paid(ledger):
    """The proof is Ledger.paid_proofs: an invoice it settled, a payment row, or both (an invoice sent to the bank file)."""
    by_invoice, by_payment, by_file, used_elsewhere = _record(ledger, "NORD", _tx(21), _tx(22), _tx(23), _tx(24))
    _invoice(ledger, "901")
    assert ledger.mark_efactura_paid("901", "statement", tx=by_invoice)
    item = scenario._op(ledger, 22, "NORD", "22.00", TODAY - timedelta(days=10), details="Impozit")
    assert ledger.mark_paid(item, "statement", by_payment)
    scenario._ef(
        ledger, "902", "NORD", "Furnizor Fisier SRL", "RO902", "FF-902", TODAY - timedelta(days=20), None, "23.00"
    )
    efactura.approve(ledger, "902", origin="person")
    assert ledger.mark_paid(ledger.efactura("902")["item_id"], "statement", by_file)
    rows = _rows(ledger)
    assert rows[by_invoice]["explanation"] == {"kind": "payment", "invoices": ["901"], "payments": []}
    assert rows[by_payment]["explanation"] == {
        "kind": "payment",
        "invoices": [],
        "payments": [
            {
                "kind": "op",
                "payee": "BUGETUL GENERAL CONSOLIDAT",
                "ref": ledger.payment(item)["doc_ref"].strip(),
                "details": "Impozit",
            }
        ],
    }
    assert rows[by_file]["explanation"]["invoices"] == ["902"] and len(rows[by_file]["explanation"]["payments"]) == 1
    assert rows[used_elsewhere]["explanation"] is None
    # a key reserved by another company's item proves nothing here
    _invoice(ledger, "903", co="STUDIO9")
    assert ledger.mark_efactura_paid("903", "statement", tx=used_elsewhere)
    assert _rows(ledger)[used_elsewhere]["explanation"] is None


def test_a_credit_is_explained_by_the_invoice_it_collected_or_its_settlement(ledger):
    single, batch, waiting, plain = _record(ledger, "NORD", *(_tx(n, "credit") for n in (31, 32, 33, 34)))
    scenario._issued(
        ledger, "7001", "NORD", "NRD 0100", "Client Test SRL", "RO7001", TODAY - timedelta(days=9), None, "31.00"
    )
    assert ledger.mark_issued_collected("7001", "statement", single)
    reports = [
        {"id": "NORD:s1", "status": "collected", "tx_key": batch, "links": {"issued": ["7002", "7003"]}},
        {"id": "NORD:s2", "status": "pending", "tx_key": None, "credit": {"id": waiting}},
        {
            "id": "NORD:s3",
            "status": "pending",
            "tx_key": None,
            "credit": None,
            "currency": "EUR",
            "date": TODAY.isoformat(),
            "payer": {"name": "Unknown processor"},
        },
    ]
    rows = _rows(ledger, settlements=reports)
    assert rows[single]["explanation"] == {"kind": "collection", "issued": ["7001"], "settlement": None}
    assert rows[batch]["explanation"] == {"kind": "collection", "issued": ["7002", "7003"], "settlement": "NORD:s1"}
    # a report waiting for a person proposes, it doesn't explain
    assert rows[waiting]["explanation"] is None and rows[waiting]["proposal"] == {
        "kind": "settlement",
        "settlement": "NORD:s2",
    }
    assert rows[plain]["explanation"] is None and rows[plain]["proposal"] is None
    assert _view(ledger, settlements=reports)["to_explain"] == 2


def test_a_card_debit_is_explained_by_its_receipt(ledger):
    """A bon that settled the shop's invoice uses up the card debit (Ledger.settle_bon_card); otherwise the receipt
    matcher of Rapoarte (recon.match_receipts): the exact amount explains, the shop's name alone only proposes."""
    till, exact, by_name, none = _record(
        ledger,
        "NORD",
        _card(51),
        _card(52, orig_amount=Decimal("9.00"), orig_currency="EUR"),
        _card(53, who="FIGMA INC"),
        _card(54),
    )
    assert ledger.assign_card("NORD", till, "NORD")
    day = (TODAY - timedelta(days=7)).isoformat()
    for sha, vendor, amount, currency in (
        ("a" * 64, "Shop51", "51.00", "RON"),
        ("b" * 64, "Hetzner", "9.00", "EUR"),
        ("c" * 64, "Figma", "15.00", "EUR"),
    ):
        assert ledger.add_document(
            sha,
            "NORD",
            day[:7],
            "receipt",
            f"/nonexistent/{sha}.jpg",
            vendor=vendor,
            doc_date=day,
            amount=amount,
            currency=currency,
        )
    _invoice(ledger, "904", total="51.00")
    assert ledger.link_bon("a" * 64, "904", "bon")
    assert ledger.settle_bon_card("904", till)
    rows = _rows(ledger)
    assert rows[till]["explanation"] == {"kind": "receipt", "invoices": ["904"], "document": "a" * 64}
    assert rows[exact]["explanation"] == {"kind": "receipt", "invoices": [], "document": "b" * 64}
    assert rows[exact]["orig"] == {"amount": "9.00", "currency": "EUR"} and rows[exact]["card"] == "0001"
    assert rows[by_name]["explanation"] is None and rows[by_name]["proposal"] == {
        "kind": "receipt",
        "document": "c" * 64,
    }
    assert rows[none]["explanation"] is None and rows[none]["proposal"] is None


def test_reads_stay_bounded_as_lines_grow(ledger):
    def queries(n: int) -> int:
        _record(ledger, "NORD", *(_card(100 + i + n * 1000) for i in range(n)))
        count = 0

        def trace(_):
            nonlocal count
            count += 1

        ledger.db.set_trace_callback(trace)
        try:
            _view(ledger)
        finally:
            ledger.db.set_trace_callback(None)
        return count

    assert queries(3) == queries(200)


def test_state_carries_the_list(ledger, home, monkeypatch):
    """api.state wires it with the state's own ambiguous list: STUDIO9's debit that fits two invoices."""
    scenario.build(ledger, home, monkeypatch.setattr)
    s = api.state(ledger, "STUDIO9")
    [row] = [r for r in s["transactions"]["rows"] if r["proposal"]]
    assert row["proposal"] == {"kind": "invoices", "invoices": [c["id"] for c in s["ambiguous"][0]["candidates"]]}
    nord = api.state(ledger, "NORD")["transactions"]
    # NORD's own five lines to explain, and Acasă's six months of history (scenario.HISTORY), none of it explained
    history = [r for r in nord["rows"] if "ISTORIC" in (r["counterparty"] or "")]
    assert history and not any(r["explanation"] for r in history)
    assert nord["to_explain"] == sum(1 for r in nord["rows"] if not r["explanation"]) == 4 + len(history)
    assert {r["explanation"]["kind"] for r in nord["rows"] if r["explanation"]} == {"payment", "collection", "receipt"}


def test_a_twin_without_its_other_copy_is_a_real_payment_and_stays(ledger):
    """A bank read that saw only part of a month reserves its lines (the statement will settle that month), with no
    copy on record yet. Hidden, a real payment would vanish; it stays De explicat, matched with nothing (not even a
    receipt of its exact amount), until its copy comes."""
    from_bank = _record(
        ledger, "NORD", _card(81, day=date(2026, 10, 22), ref="eb:81"), start=date(2026, 10, 15), source="ob"
    )[0]
    assert ledger.db.execute("SELECT what FROM settled_tx WHERE tx_key=?", (from_bank,)).fetchone()[0] == "twin ob"
    assert ledger.add_document(
        "9" * 64,
        "NORD",
        "2026-10",
        "receipt",
        "/nonexistent/9.jpg",
        vendor="Shop81",
        doc_date="2026-10-22",
        amount="81.00",
        currency="RON",
    )
    row = _rows(ledger)[from_bank]
    assert row["explanation"] is None and row["proposal"] is None and _view(ledger)["to_explain"] == 1
    from_file = _record(ledger, "NORD", _card(81, day=date(2026, 10, 22)), start=date(2026, 10, 1))[0]
    assert list(_rows(ledger)) == [from_file]  # the statement brought its copy: listed once


def test_a_copy_hides_one_twin_never_two(ledger):
    """Two payments of the same amount two days apart, read by the bank, and the statement that brought only one so far:
    one bank line is that one's copy, the other is a payment of its own."""
    month = date(2026, 10, 1)
    from_file = _record(ledger, "NORD", _tx(82, day=date(2026, 10, 10)), start=month)[0]
    twins = _record(
        ledger,
        "NORD",
        _tx(82, day=date(2026, 10, 10), ref="eb:a"),
        _tx(82, day=date(2026, 10, 12), ref="eb:b"),
        start=month,
        source="ob",
    )
    rows = _rows(ledger)
    assert from_file in rows and twins[0] not in rows and twins[1] in rows
    other = _record(ledger, "NORD", _tx(82, "credit", day=date(2026, 10, 10), ref="eb:c"), start=month, source="ob")[0]
    assert other in _rows(ledger)  # a credit is never a debit's copy


def test_the_bank_text_never_carries_a_full_account_or_card_number(ledger):
    """A BT transfer's details hold the other side's IBAN ("...;<NAME>;<IBAN>;<BIC>;...")."""
    iban = "RO49AAAA1B31007593840000"
    key = _record(
        ledger,
        "NORD",
        _tx(
            83,
            description=f"Plata;FURNIZOR SRL;{iban};AAAARO22;Factura 1 card 4242424242424242",
            counterparty="FURNIZOR SRL RO49 AAAA 1B31 0075 9384 0000",
        ),
    )[0]
    row = _rows(ledger)[key]
    assert row["details"] == "Plata;FURNIZOR SRL;••••0000;AAAARO22;Factura 1 card ••••4242"
    assert row["counterparty"] == "FURNIZOR SRL ••••0000"
    shown = json.dumps(_view(ledger))
    assert iban not in shown and "4242424242424242" not in shown and "1B31" not in shown
    # however it is grouped, and only the number: the words after it stay
    for raw, want in (
        ("card 4242 4242 4242 4242 ok", "card ••••4242 ok"),
        ("card 4242-4242-4242-4242", "card ••••4242"),
        ("RO 49 AAAA 1B31 0075 9384 0000 factura 123", "••••0000 factura 123"),
        (f"{iban.lower()} chirie", "••••0000 chirie"),
        (f"{iban} FACTURA 123", "••••0000 FACTURA 123"),
        ("RO24BTRLRONCRT0000000001 x", "••••0001 x"),  # its shape, even with check digits that fail
        ("OP 5 06/09/2026, factura 2026, 1250 lei", "OP 5 06/09/2026, factura 2026, 1250 lei"),
    ):
        assert transactions._text(raw) == want, raw


@pytest.mark.parametrize("second_iban,days,receipt_day", [(NORD_RON, (10, 20), 15), (NORD_EUR, (12, 10), 11)])
def test_a_receipt_goes_to_the_debit_rapoarte_gives_it(ledger, second_iban, days, receipt_day):
    """Two debits of the same amount and one receipt between them: recon.match_receipts gives it to the first in the
    statement's own order (Statement.txs, account after account), never by date; the list, newest first, follows it."""
    first = _card(95, day=date(2026, 10, days[0]), amount="30.00")
    second = _card(96, day=date(2026, 10, days[1]), amount="30.00", iban=second_iban)
    accounts: dict[str, list] = {}
    for t in (first, second):
        accounts.setdefault(t.iban, []).append(t)
    st = statements.Statement("NORD", None, None, [statements.Account(i, "RON", ts) for i, ts in accounts.items()])
    ledger.record_statement(st)
    assert ledger.assign_card("NORD", statements.tx_key(first), "NORD")
    assert ledger.add_document(
        "f" * 64,
        "NORD",
        "2026-10",
        "receipt",
        "/nonexistent/f.jpg",
        vendor="Magazin",
        doc_date=f"2026-10-{receipt_day}",
        amount="30.00",
        currency="RON",
    )
    assert list(recon.match_receipts(st.txs, recon._receipts(ledger, "NORD"))) == [0]  # Rapoarte: the first line
    rows = _rows(ledger)
    assert rows[statements.tx_key(first)]["explanation"] == {"kind": "receipt", "invoices": [], "document": "f" * 64}
    assert rows[statements.tx_key(second)]["explanation"] is None


def test_a_bon_reservation_explains_only_this_company_s_invoice_still_paid_by_its_bon(ledger):
    mine, stale = _record(ledger, "NORD", _card(93), _card(94))
    _invoice(ledger, "905", co="STUDIO9")
    _invoice(ledger, "906")
    for key, mid in ((mine, "905"), (stale, "906")):  # another company's invoice; one with no bon behind it
        ledger.db.execute("INSERT INTO settled_tx VALUES(?,?,?)", (key, f"card of bon, efactura {mid}", "2026-10-20"))
    ledger.db.commit()
    rows = _rows(ledger)
    assert rows[mine]["explanation"] is None and rows[stale]["explanation"] is None


def test_many_equal_twins_pair_in_bounded_time(ledger):
    month = date(2026, 10, 1)
    _record(
        ledger,
        "NORD",
        *(_tx(15, day=month + timedelta(days=i % 28), ref=f"F{i}", description=f"Comision {i}") for i in range(400)),
        start=month,
    )
    _record(
        ledger,
        "NORD",
        *(_tx(15, day=month + timedelta(days=i % 28), ref=f"eb:{i}") for i in range(400)),
        start=month,
        source="ob",
    )
    started = time.perf_counter()
    view = _view(ledger)
    assert time.perf_counter() - started < 2
    assert len(view["rows"]) == 400  # each bank line found its copy


def test_wrong_report_total_only_proposes_its_unique_credit(ledger):
    (key,) = _record(ledger, "NORD", _tx(99, "credit", counterparty="Synthetic Processor SRL"))
    report = dict(
        id="report",
        status="pending",
        tx_key=None,
        credit=None,
        currency="RON",
        date=(TODAY - timedelta(days=6)).isoformat(),
        payer={"name": "Synthetic Processor SRL"},
        net="1.00",
    )
    row = _rows(ledger, settlements=[report], live=True)[key]
    assert row["explanation"] is None and row["proposal"] == {"kind": "settlement", "settlement": "report"}
    assert row["can_link"] is True
    assert ledger.used_txs() == set()
    _record(ledger, "NORD", _tx(100, "credit", counterparty="Synthetic Processor SRL"))
    assert all(r["proposal"] is None for r in _rows(ledger, settlements=[report]).values())


@pytest.mark.parametrize("owner", [None, "personal", "STUDIO9", "NORD"])
def test_card_ownership_gates_payment_proof_and_document_links(ledger, owner):
    t = _tx(10, card_last4="9876", country="RO", counterparty="Furnizor Test SRL")
    (key,) = _record(ledger, "NORD", t)
    _invoice(ledger, "991")
    item = scenario._op(ledger, 10, "NORD", "10.00", TODAY - timedelta(days=20))
    assert statements.classify_tx(t) == statements.NEEDS_INVOICE
    if owner:
        assert ledger.assign_card("NORD", key, owner)
    assert _rows(ledger)[key]["card_owner"] == owner
    if owner != "NORD":
        assert not efactura.rivals(ledger, "NORD")(t)
        assert not ledger.mark_paid(item, "statement proof", key)
        assert not ledger.settle_bon_card("991", key)
        assert not ledger.mark_efactura_paid("991", "statement proof", key)
        with pytest.raises(LinkRefused):
            links.link(ledger, "NORD", key, invoices=["991"], partners={"efactura 991": "supplier"})
        assert ledger.efactura("991")["status"] == "open" and key not in ledger.used_txs()
    else:
        assert ledger.mark_efactura_paid("991", "statement proof", key)
        assert not ledger.mark_paid(item, "statement proof", key)
        assert not ledger.settle_bon_card("991", key)
        assert not ledger.mark_efactura_paid("991", "statement proof", key)


def test_card_owner_first_answer_wins_across_connections(ledger):
    (key,) = _record(ledger, "NORD", _tx(10, card_last4="9876"))
    barrier = Barrier(2)

    def answer(owner):
        with closing(Ledger(ledger.path)) as other:
            barrier.wait()
            return other.assign_card("NORD", key, owner)

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(answer, ("NORD", "personal"))) == [False, True]
    assert ledger.card_owner("NORD", "9876") in ("NORD", "personal")
    assert not ledger.assign_card("STUDIO9", key, "STUDIO9")
    assert not ledger.assign_card("NORD", key, "invalid")


def test_card_supplier_requires_remembered_association_not_country(ledger):
    t = _tx(10, card_last4="9876", country="RO", counterparty="Furnizor Test SRL")
    (key,) = _record(ledger, "NORD", t)
    assert ledger.assign_card("NORD", key, "NORD")
    recon.classify(ledger, "NORD", [t])
    assert t.category == statements.NEEDS_INVOICE
    _invoice(ledger, "991")
    links.link(ledger, "NORD", key, invoices=["991"], partners={"efactura 991": "supplier"}, remember="name")
    later = _tx(11, card_last4="9876", country="RO", counterparty="Furnizor Test SRL")
    recon.classify(ledger, "NORD", [later])
    assert later.category == statements.DOMESTIC
    assert ledger.forget("NORD", ledger.memories("NORD")[0]["id"])
    recon.classify(ledger, "NORD", [later])
    assert later.category == statements.NEEDS_INVOICE


def test_identical_card_suffixes_in_different_firms_need_separate_answers(ledger):
    (nord,) = _record(ledger, "NORD", _tx(10, card_last4="9876"))
    (studio,) = _record(ledger, "STUDIO9", _tx(11, iban=STUDIO_RON, card_last4="9876"))
    assert ledger.assign_card("NORD", nord, "personal")
    assert ledger.card_owner("STUDIO9", "9876") is None
    assert _rows(ledger, "STUDIO9")[studio]["card_owner"] is None
    assert ledger.assign_card("STUDIO9", studio, "STUDIO9")
    assert not ledger.card_allows("NORD", "9876") and ledger.card_allows("STUDIO9", "9876")


@pytest.mark.parametrize("owner", [None, "personal", "STUDIO9", "NORD"])
def test_card_refund_cannot_collect_without_firm_ownership(ledger, owner):
    (key,) = _record(ledger, "NORD", _tx(10, "credit", card_last4="9876"))
    scenario._issued(ledger, "992", "NORD", "TEST-992", "Client Test SRL", "12345674", TODAY, None, "10.00")
    if owner:
        assert ledger.assign_card("NORD", key, owner)
    assert ledger.mark_issued_collected("992", "statement proof", key) == (owner == "NORD")


@pytest.mark.parametrize(
    "owner,warning", [(None, "unknown"), ("personal", "personal"), ("STUDIO9", "STUDIO9"), ("NORD", "")]
)
def test_receipt_ownership_uses_only_the_filing_firms_answer(ledger, owner, warning):
    (key,) = _record(ledger, "NORD", _tx(10, card_last4="9876"))
    if owner:
        assert ledger.assign_card("NORD", key, owner)
    notes = docs.issues_for("NORD", docs.Evidence("", card_last4="9876"), None, ledger.card_owners())
    assert bool(notes) == bool(warning)
    if warning:
        assert warning in notes[0]
