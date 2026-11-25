"""The user explains a statement transaction (fiscalm.links): De la cine, Ce acoperă and the named difference. One
transaction explains one set of documents; a second explanation replaces the first; an undo reopens exactly what it
closed; a part still owed keeps the invoice open for that part everywhere. Synthetic data only."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import synthetic_world as scenario
from payment_cases import download_batch
from test_settlements import collect, statement

from fiscalm import companies, links, recon, transactions
from fiscalm.api import actions, lists, state as api, transaction_choices, users
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.invoicing import issued
from fiscalm.ledger import Ledger, LinkRefused
from fiscalm.payments import engine as pay
from fiscalm.settlements import engine as settlements

TODAY = date(2026, 10, 27)
NORD_RON = "RO24BTRLRONCRT0000000001"
CLIENT = "Client Exemplu SRL"


def _tx(n: int, direction: str = "credit", amount: str = "100.00", **kw) -> statements.Tx:
    return statements.Tx(
        date=kw.pop("day", TODAY - timedelta(days=3)),
        title="Incasare OP",
        description=kw.pop("description", f"Plata {n}"),
        direction=direction,
        amount=Decimal(amount),
        currency=kw.pop("currency", "RON"),
        iban=kw.pop("iban", NORD_RON),
        ref=f"OP{n}",
        **kw,
    )


def _record(L, co: str, *txs: statements.Tx) -> list[str]:
    L.record_statement(statements.Statement(co, None, None, [statements.Account("x", "RON", list(txs))]))
    return [statements.tx_key(t) for t in txs]


def _sale(
    L, mid: str, total: str = "100.00", *, client: str = CLIENT, cui: str = "5001", days: int = 20, co="NORD", **kw
):
    scenario._issued(
        L, mid, co, f"NRD {mid}", client, cui, TODAY - timedelta(days=days), TODAY + timedelta(days=10), total, **kw
    )


def _bill(L, mid: str, total: str = "100.00", *, supplier: str = "Furnizor Exemplu SRL", cui: str = "1001", **kw):
    scenario._ef(
        L, mid, "NORD", supplier, cui, f"FE-{mid}", TODAY - timedelta(days=20), TODAY + timedelta(days=5), total, **kw
    )


def _link(L, key: str, **kw) -> dict:
    return links.link(L, "NORD", key, partners=api.item_partners(L, "NORD"), **kw)


def _view(L, **kw) -> dict:
    return transactions.view(L, companies.get("NORD"), TODAY, settlements=[], ambiguous=[], **kw)


def _rows(L, live: bool = True) -> dict[str, dict]:
    return {r["id"]: r for r in _view(L, live=live, partners=api.item_partners(L, "NORD"))["rows"]}


def _owed(L, mid: str) -> dict:
    return next(r for r in issued.issued(L, "NORD", TODAY)["rows"] if r["message_id"] == mid)


def test_a_credit_explains_one_issued_invoice_and_settles_it(ledger, live):
    (key,) = _record(ledger, "NORD", _tx(1, counterparty=CLIENT))
    _sale(ledger, "7001")
    assert _rows(ledger)[key]["explanation"] is None and _rows(ledger)[key]["can_link"] is True
    admin = users.create(ledger, "ana", "Ana Exemplu", "admin", datetime(2026, 10, 27, tzinfo=UTC), actor_id=None)
    done = _link(ledger, key, invoices=["7001"], by=admin, remember="name")
    assert ledger.issued("7001")["status"] == "collected" and ledger.issued("7001")["collected_tx"] == key
    assert ledger.db.execute("SELECT what FROM settled_tx WHERE tx_key=?", (key,)).fetchone()[0] == "link"
    row = _rows(ledger)[key]
    assert row["explanation"] == {"kind": "collection", "issued": ["7001"], "settlement": None, "link": done}
    assert done["parts"] == [{"id": "7001", "amount": "100.00", "closed": True}]
    assert (done["partner"], done["reason"], done["difference"], done["by"]) == (
        "NORD.5001",
        None,
        "0.00",
        "Ana Exemplu",
    )
    assert _view(ledger)["remembered"][0]["who_by"] == "Ana Exemplu" and admin not in json.dumps(_view(ledger))
    assert row["partner"] == "NORD.5001" and row["can_link"] is True  # the user may change it
    # collected on the credit's booking day; collected_at stays the moment it was recorded
    owed = _owed(ledger, "7001")
    assert (owed["how"], owed["collected_on"]) == ("statement", (TODAY - timedelta(days=3)).isoformat())
    assert owed["collected_at"][:10] != owed["collected_on"]


@pytest.mark.parametrize(
    "details, amount, proposed",
    [
        ("Plata factura NRD-7031", "100.00", ["7031"]),
        ("PLATA FACTURA NRD7031 CLIENT", "100.00", ["7031"]),
        ("Plata factura NRD-7031", "99.00", []),  # another amount
        ("Plata factura NRD-70311", "100.00", []),  # another number
        ("Plata facturi NRD-7031 si NRD-7032", "100.00", []),  # two numbers: ambiguous
    ],
)
def test_an_invoice_the_details_name_at_its_exact_amount_is_proposed_never_applied(
    ledger, live, details, amount, proposed
):
    (key,) = _record(ledger, "NORD", _tx(31, amount=amount, description=details, counterparty="Altcineva"))
    _sale(ledger, "7031")
    _sale(ledger, "7032", client="Alt Client SRL", cui="5002")
    full = api.state(ledger, "NORD")
    tx = next(t for t in full["transactions"]["rows"] if t["id"] == key)
    pages = transaction_choices.project(full, {"id": key})["pages"]
    assert [r["id"] for r in pages["proposed"]["data"]] == proposed and tx["explanation"] is None
    assert [r["id"] for r in pages["invoices"]["data"]] == proposed  # its client is the form's partner
    assert ledger.issued("7031")["status"] == "open"
    if proposed:
        _link(ledger, key, invoices=proposed)
        full = api.state(ledger, "NORD")
        assert transaction_choices.project(full, {"id": key})["pages"]["proposed"]["data"] == []


def test_a_part_still_owed_keeps_the_newest_invoice_open_for_that_part_everywhere(ledger, live):
    first, rest = _record(ledger, "NORD", _tx(2, amount="150.00"), _tx(3, amount="50.00"))
    _sale(ledger, "7002", days=30)
    _sale(ledger, "7003", days=10)
    with pytest.raises(LinkRefused) as e:
        _link(ledger, first, invoices=["7002", "7003"])
    assert e.value.code == "reason"  # 150 of 200: the difference is named or nothing is written
    assert ledger.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 0
    done = _link(ledger, first, invoices=["7003", "7002"], reason="rest")
    assert done["parts"] == [
        {"id": "7002", "amount": "100.00", "closed": True},
        {"id": "7003", "amount": "50.00", "closed": False},
    ]
    assert done["difference"] == "-50.00"
    assert ledger.issued("7002")["status"] == "collected" and ledger.issued("7003")["status"] == "open"
    row = _owed(ledger, "7003")
    assert (row["owed"], row["left"], row["covered"], row["status"]) == ("50.00", "50.00", "50.00", "neincasata")
    s = api.state(ledger, "NORD", datetime.combine(TODAY, datetime.min.time()))
    assert s["receivable"]["total"] == "50.00"
    assert next(p for p in s["partners"] if p["id"] == "NORD.5001")["receivable"]["total"] == "50.00"
    assert [u["amount"] for u in s["dashboard"]["upcoming"] if u.get("number") == "NRD 7003"] == ["50.00"]
    # the matcher never settles a partly paid invoice, even with a credit of exactly its rest
    st = statements.Statement(
        "NORD", None, None, [statements.Account("x", "RON", [_tx(4, amount="50.00", counterparty=CLIENT)])]
    )
    assert issued.match_statement(ledger, st) == [] and ledger.issued("7003")["status"] == "open"
    # the rest, linked by the user, closes it
    _link(ledger, rest, invoices=["7003"])
    assert ledger.issued("7003")["status"] == "collected" and _owed(ledger, "7003")["left"] == "0.00"


def test_a_named_fee_closes_every_invoice_picked_and_keeps_the_difference(ledger, live):
    (key,) = _record(ledger, "NORD", _tx(5, amount="95.00"))
    _sale(ledger, "7004")
    done = _link(ledger, key, invoices=["7004"], reason="courier_fee", note="ignored")
    assert (done["reason"], done["difference"], done["note"]) == ("courier_fee", "-5.00", "")
    assert done["parts"] == [{"id": "7004", "amount": "100.00", "closed": True}]
    assert ledger.issued("7004")["status"] == "collected"


@pytest.mark.parametrize(
    "direction,amount,reason",
    [
        ("debit", "95.00", "courier_fee"),
        ("credit", "105.00", "rest"),
        ("debit", "105.00", "rest"),
        ("credit", "105.00", "courier_fee"),
        ("credit", "95.00", "other"),
    ],
)  # Altceva needs its words
def test_a_reason_that_does_not_fit_the_difference_is_refused(ledger, live, direction, amount, reason):
    (key,) = _record(ledger, "NORD", _tx(6, direction, amount))
    _sale(ledger, "7005")
    _bill(ledger, "205")
    with pytest.raises(LinkRefused) as e:
        _link(ledger, key, invoices=["7005" if direction == "credit" else "205"], reason=reason)
    assert e.value.code == "reason"


def test_a_debit_settles_a_received_invoice_the_user_marked_paid_and_proves_it(ledger, live):
    """The mark waited for proof (rule 2); the user's link to a real statement debit is that proof."""
    (key,) = _record(ledger, "NORD", _tx(7, "debit", "100.00"))
    _bill(ledger, "206")
    assert ledger.mark_efactura_paid("206", "user: already paid (page)")
    assert ledger.paid_proofs("NORD").get("efactura 206") is None
    _link(ledger, key, invoices=["206"])
    r = ledger.efactura("206")
    assert (r["status"], r["paid_tx"]) == ("paid", key) and ledger.paid_proofs("NORD")["efactura 206"] == key
    assert efactura.awaiting(ledger, "NORD") == []
    links.unlink(ledger, "NORD", key, _rows(ledger)[key]["explanation"]["link"]["id"])
    assert [x["message_id"] for x in efactura.awaiting(ledger, "NORD")] == ["206"]  # marked again, as it was


def test_a_second_explanation_replaces_the_first_and_a_stale_one_is_refused(ledger, live):
    (key,) = _record(ledger, "NORD", _tx(8))
    _sale(ledger, "7006")
    _sale(ledger, "7007")
    _sale(ledger, "7009", "60.00")
    first = _link(ledger, key, invoices=["7006"])
    with pytest.raises(LinkRefused) as e:
        _link(ledger, key, invoices=["7007"])  # the sheet saw no explanation: another one was made since
    assert e.value.code == "changed"
    with pytest.raises(LinkRefused):  # a replacement that fails (100 for 60, unnamed) leaves the first whole
        _link(ledger, key, invoices=["7009"], was=first["id"])
    assert (
        ledger.issued("7006")["status"] == "collected" and links.explanations(ledger, "NORD")[key]["id"] == first["id"]
    )
    second = _link(ledger, key, invoices=["7007"], was=first["id"])
    assert ledger.issued("7006")["status"] == "open" and ledger.issued("7007")["status"] == "collected"
    assert ledger.db.execute("SELECT count(*) FROM tx_link_items").fetchone()[0] == 1
    with pytest.raises(LinkRefused):
        links.unlink(ledger, "NORD", key, first["id"])
    links.unlink(ledger, "NORD", key, second["id"])
    assert ledger.issued("7007")["status"] == "open"
    assert ledger.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 0  # free for the matchers again


def test_undo_reopens_a_marked_invoice_as_marked_and_one_another_link_closed(ledger, live):
    a, b = _record(ledger, "NORD", _tx(10, amount="60.00"), _tx(11, amount="40.00"))
    _sale(ledger, "7010")
    assert issued.mark_collected(ledger, "7010", "cash")
    first = _link(ledger, a, invoices=["7010"], reason="rest")
    assert ledger.issued("7010")["status"] == "marked"  # 60 of 100: still the user's mark
    _link(ledger, b, invoices=["7010"])
    assert ledger.issued("7010")["status"] == "collected" and ledger.issued("7010")["collected_tx"] == b
    links.unlink(ledger, "NORD", a, first["id"])  # what is left (40) no longer covers it: it reopens as it was
    r = ledger.issued("7010")
    assert (r["status"], r["collected_tx"]) == ("marked", None) and r["collected_method"] == "cash"
    assert links.explanations(ledger, "NORD")[b]["parts"] == [{"id": "7010", "amount": "40.00", "closed": False}]


def test_refusals_leave_nothing_written(ledger, live):
    credit, debit, eur, settled, other = _record(
        ledger, "NORD", _tx(14), _tx(15, "debit"), _tx(16, currency="EUR"), _tx(17), _tx(18)
    )
    (stranger,) = _record(ledger, "STUDIO9", _tx(19, iban="RO94BTRLRONCRT0000000002"))
    _sale(ledger, "7012", "50.00")
    _sale(ledger, "7013", "50.00", client="Alt Client SRL", cui="5002")
    _sale(ledger, "7014")
    _sale(ledger, "7015")
    assert ledger.mark_issued_collected("7015", "statement", settled)
    cases = [
        ("partner", credit, ["7012", "7013"]),
        ("currency", eur, ["7014"]),
        ("item_changed", debit, ["7014"]),
        ("item_changed", other, ["7015"]),  # already settled by another transaction
        ("unknown", stranger, ["7014"]),
        ("used", settled, ["7014"]),
        ("nothing", other, []),
    ]
    for code, key, ids in cases:
        with pytest.raises(LinkRefused) as e:
            _link(ledger, key, invoices=ids)
        assert e.value.code == code
    with pytest.raises(LinkRefused) as e:
        _link(ledger, other, invoices=["7012"], reason="other", note="x" * (links.NOTE_MAX + 1))
    assert e.value.code == "note"
    assert ledger.db.execute("SELECT count(*) FROM tx_links").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM settled_tx WHERE what='link'").fetchone()[0] == 0


def test_a_prepared_payment_is_paid_whole_and_proved(ledger, live):
    short, key = _record(ledger, "NORD", _tx(30, "debit", "20.00"), _tx(20, "debit", "22.00"))
    item = scenario._op(ledger, 22, "NORD", "22.00", TODAY - timedelta(days=10))
    with pytest.raises(LinkRefused) as e:
        _link(ledger, short, invoices=[], payment=item, reason="rest")
    assert e.value.code == "payment_rest"
    done = _link(ledger, key, invoices=[], payment=item)
    assert ledger.payment(item)["status"] == "paid" and ledger.paid_proofs("NORD")["payment " + item] == key
    assert done["parts"] == [] and done["payments"][0]["payee"] == "BUGETUL GENERAL CONSOLIDAT"
    assert item not in str(_rows(ledger)[key])  # never the payee's account
    links.unlink(ledger, "NORD", key, done["id"])
    assert ledger.payment(item)["status"] == "pending"


def test_a_partly_paid_received_invoice_never_goes_into_the_payment_file_or_settles_alone(ledger, live):
    (a,) = _record(ledger, "NORD", _tx(21, "debit", "60.00"))
    _bill(ledger, "207")
    _link(ledger, a, invoices=["207"], reason="rest")
    with pytest.raises(efactura.ApproveError):
        efactura.approve(ledger, "207", origin="person")  # the row would pay the whole total again
    inv = next(
        i
        for i in api.state(ledger, "NORD", datetime.combine(TODAY, datetime.min.time()))["invoices"]
        if i["id"] == "207"
    )
    assert (inv["status"], inv["covered"], inv["left"], inv["can_approve"]) == ("open", "60.00", "40.00", False)
    r = ledger.efactura("207")
    rest = _tx(22, "debit", "40.00", counterparty_iban=r["supplier_iban"])
    assert efactura.rivals(ledger, "NORD")(rest) == {("partial", "207")}
    efactura.match_statement(ledger, statements.Statement("NORD", None, None, [statements.Account("x", "RON", [rest])]))
    assert ledger.efactura("207")["status"] == "open"


def test_the_partner_the_statement_text_points_to_is_suggested(ledger, live):
    by_cui, by_name, nobody = _record(
        ledger,
        "NORD",
        _tx(23, description="Plata factura CUI 50017788"),
        _tx(24, counterparty="CLIENT EXEMPLU S.R.L."),
        _tx(25, counterparty="Altcineva"),
    )
    _sale(ledger, "7016", cui="50017788", client="Firma Cu Cod SRL")
    _sale(ledger, "7017")
    rows = _rows(ledger)
    assert (rows[by_cui]["partner"], rows[by_name]["partner"], rows[nobody]["partner"]) == (
        "NORD.50017788",
        "NORD.5001",
        None,
    )
    assert rows[by_cui]["explanation"] is None  # a default for De la cine, never an explanation


@pytest.mark.parametrize("one", ["transaction", "invoice"])
def test_two_connections_linking_one_invoice_have_one_winner(ledger, live, monkeypatch, one):
    a, b = _record(ledger, "NORD", _tx(27), _tx(28))
    _sale(ledger, "7019")
    _sale(ledger, "7020")
    _race(monkeypatch, [(a, "7019"), (a, "7020")] if one == "transaction" else [(a, "7020"), (b, "7020")])
    assert sorted(ledger.issued(m)["status"] for m in ("7019", "7020")) == ["collected", "open"]
    assert ledger.db.execute("SELECT count(*) FROM tx_link_items").fetchone()[0] == 1
    assert ledger.db.execute("SELECT count(*) FROM settled_tx WHERE what='link'").fetchone()[0] == 1


def _race(monkeypatch, calls):
    """Both start together; each one's plan reads slowly, so the second runs while the first holds what it read."""
    slow = links.covered

    def covered(*args, **kw):
        time.sleep(0.2)
        return slow(*args, **kw)

    monkeypatch.setattr(links, "covered", covered)
    gate, won = threading.Barrier(len(calls)), []

    def caller(key, mid):
        L = Ledger(timeout=10)
        gate.wait()
        try:
            _link(L, key, invoices=[mid])
            won.append(True)
        except LinkRefused:
            won.append(False)
        finally:
            L.close()

    threads = [threading.Thread(target=caller, args=c) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(won) == [False, True]


def test_the_actions_say_what_happened_and_shadow_mode_refuses(ledger, home, live):
    (key,) = _record(ledger, "NORD", _tx(29, amount="150.00"))
    _sale(ledger, "7021", days=30)
    _sale(ledger, "7022", days=10)
    out = actions.act(
        "tx-link",
        "NORD",
        {"co": "NORD", "tx": key, "invoices": "7021,7022", "reason": "rest", "was": ""},
        created_by="admin",
    )
    assert (
        out["ok"]
        and out["message"] == "Tranzacția a fost asociată cu 2 facturi. Restul facturii NRD 7022 rămâne de încasat."
        and out["done"] is None  # `done` is approve's success sheet; the link's id has its own key
    )
    assert ledger.db.execute('SELECT "by" FROM tx_links WHERE tx_key=?', (key,)).fetchone()[0] == "admin"
    refused = actions.act("tx-link", "NORD", {"co": "NORD", "tx": key, "invoices": "7021", "was": ""})
    assert refused == {
        "ok": False,
        "message": "Explicația tranzacției s-a schimbat între timp.",
        "next": None,
        "done": None,
    }
    out = actions.act("tx-unlink", "NORD", {"co": "NORD", "tx": key, "was": out["linked"]["id"]})
    assert out["ok"] and out["message"] == "Asocierea a fost anulată."
    (home / "state" / "LIVE").unlink()
    assert actions.act("tx-link", "NORD", {"co": "NORD", "tx": key, "invoices": "7021,7022", "reason": "rest"})["ok"]
    assert ledger.db.execute("SELECT count(*) FROM tx_links").fetchone()[0] == 1
    assert _rows(ledger, live=False)[key]["can_link"] is True


def test_a_partly_paid_invoice_is_never_closed_whole_by_another_writer(ledger, live):
    """Every settling writer re-checks the user's links in its WHERE: a Decontare, a statement credit or debit, a mail
    proof, a bon. Only the user may still say the rest was paid (a mark, not a proof)."""
    part, other = _record(ledger, "NORD", _tx(40, amount="60.00"), _tx(41, "debit", "60.00"))
    for n in ("8001", "8002"):
        ledger.add_issued(
            dict(
                message_id=n,
                company="NORD",
                number=f"TEST-{n}",
                issue_date="2026-10-01",
                currency="RON",
                total="100.00",
                doc_type="invoice",
                status="open",
                raw_path="",
                client_name=CLIENT,
                client_cui="5001",
            )
        )
    _link(ledger, part, invoices=["8001"], reason="rest")
    rid = settlements.import_report(
        ledger,
        "NORD",
        b"Ref,Gross,Fee\nTEST-8001,100.00,2.00\nTEST-8002,100.00,2.00\n",
        dict(
            source="fan",
            report_key="r-links",
            date="2026-10-10",
            currency="RON",
            payer_name="Synthetic Courier SRL",
            mapping={"reference": "Ref", "gross": "Gross", "fee": "Fee"},
        ),
    )
    with pytest.raises(settlements.SettlementError):
        collect(ledger, rid, statement())
    assert ledger.issued("8001")["status"] == ledger.issued("8002")["status"] == "open"
    assert not ledger.mark_issued_collected("8001", "statement", _record(ledger, "NORD", _tx(42, amount="40.00"))[0])
    _bill(ledger, "208")
    _link(ledger, other, invoices=["208"], reason="rest")
    assert not ledger.mark_efactura_paid("208", "mail", "ef-proof:synthetic")
    assert not ledger.mark_efactura_paid("208", "statement", _record(ledger, "NORD", _tx(43, "debit", "40.00"))[0])
    assert not ledger.link_bon("sha-bon-208", "208", "bon")
    assert ledger.efactura("208")["status"] == "open"
    assert (
        ledger.db.execute("SELECT count(*) FROM settled_tx WHERE what!='link'").fetchone()[0] == 0
    )  # nothing reserved
    assert ledger.mark_efactura_paid("208", "user: already paid (page)")  # the user's word for the rest stays possible


@pytest.mark.parametrize("how", ["replace", "undo"])
def test_a_replacement_that_frees_a_payment_puts_it_back_in_the_payment_file(ledger, live, how):
    """Undoing a statement link restores membership without rebuilding bank bytes."""
    (key,) = _record(ledger, "NORD", _tx(47, "debit", "100.00"))
    item = scenario._op(ledger, 47, "NORD", "100.00", TODAY - timedelta(days=10))
    ident, _ = download_batch(ledger, [item], today=TODAY)
    path = str(pay.named_path(ledger.payment_file(ident)))
    _bill(ledger, "209")
    first = _link(ledger, key, invoices=[], payment=item)
    assert (
        ledger.payment(item)["status"] == "paid" and ledger.payment(item)["csv_path"] == path
    )  # bank bytes remain the downloaded snapshot
    if how == "undo":
        links.unlink(ledger, "NORD", key, first["id"])
    else:
        _link(ledger, key, invoices=["209"], was=first["id"])
    assert (ledger.payment(item)["status"], ledger.payment(item)["csv_path"]) == ("exported", path)
    summary = api.state(ledger, "NORD", datetime.combine(TODAY, datetime.min.time()))["files"][0]
    assert summary["id"] == ident and summary["status"] == "open" and summary["question"] is None


def test_reopening_a_mark_on_an_invoice_paid_in_part_never_brings_back_its_whole_payment(ledger, live):
    (key,) = _record(ledger, "NORD", _tx(49, "debit", "60.00"))
    _bill(ledger, "211")
    efactura.approve(ledger, "211", origin="person")
    item = ledger.efactura("211")["item_id"]
    assert efactura.withdraw(ledger, "211", "user: already paid (page)") is None
    _link(ledger, key, invoices=["211"], reason="rest")
    assert efactura.reopen(ledger, "211") == "open"
    assert ledger.efactura("211")["status"] == "open" and ledger.payment(item)["status"] == "skipped"


def test_a_card_paying_an_invoice_in_its_own_currency_is_counted_in_that_currency(ledger, live):
    (key,) = _record(
        ledger, "NORD", _tx(48, "debit", "173.45", orig_amount=Decimal("20.00"), orig_currency="EUR", card_last4="0001")
    )
    assert ledger.assign_card("NORD", key, "NORD")
    _bill(ledger, "210", "19.50", currency="EUR")
    done = _link(ledger, key, invoices=["210"], reason="bank_fee")
    assert (done["currency"], done["difference"], done["parts"][0]["amount"]) == ("EUR", "0.50", "19.50")
    assert ledger.efactura("210")["status"] == "paid"


@pytest.mark.parametrize(
    "kind,args,refused",
    [
        ("label", {"reason": "bank_fee"}, {"reason": "other"}),
        ("advance", {"who": "Administrator Exemplu"}, {"who": " "}),
    ],
)
def test_a_transaction_no_document_explains_is_labelled_for_the_accountant(ledger, live, kind, args, refused):
    (key,) = _record(ledger, "NORD", _tx(50, "debit", "15.00"))
    with pytest.raises(LinkRefused):
        _link(ledger, key, kind=kind, **refused)
    done = _link(ledger, key, kind=kind, **args)
    row = _rows(ledger)[key]
    assert row["explanation"]["kind"] == kind and _view(ledger)["to_explain"] == 0
    links.unlink(ledger, "NORD", key, done["id"])
    assert _rows(ledger)[key]["explanation"] is None


def test_a_transfer_between_the_firms_accounts_explains_both_ends_when_one_is_seen(ledger, live):
    eur = "RO71BTRLEURCRT0000000001"
    out, into, stray, client = _record(
        ledger,
        "NORD",
        _tx(51, "debit", "300.00", counterparty_iban=eur),
        _tx(52, "credit", "300.00", iban=eur),
        _tx(53, "credit", "300.00", day=TODAY - timedelta(days=20)),
        _tx(54, "credit", "300.00", iban=eur, counterparty_iban="RO49AAAA1B31007593840000"),
    )
    ledger.db.execute(
        "INSERT INTO settlement_reports(id,company,source,report_key,digest,raw,data) VALUES"
        "('r1','NORD','csv','k','d',x'00','{}')"
    )
    ledger.db.execute("INSERT INTO settlement_credits VALUES('r1',?,1)", (json.dumps({"id": into}),))
    assert (
        links.other_end(ledger, "NORD", out, _tx(51, "debit", "300.00", counterparty_iban=eur)) is None
    )  # a Decontare's
    ledger.db.execute("DELETE FROM settlement_credits")
    done = _link(ledger, out, kind="transfer")
    assert done["other"] == into  # not the credit that names an outside payer
    rows = _rows(ledger)
    assert rows[into]["explanation"] == {"kind": "transfer", "other": out, "link": done}
    assert rows[stray]["explanation"] is None
    with pytest.raises(LinkRefused) as e:
        _link(ledger, into, kind="label", reason="bank_fee")  # explained from the other end: changed from there
    assert e.value.code == "used"
    links.unlink(ledger, "NORD", out, done["id"])
    assert ledger.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 0


def test_a_remembered_explanation_is_proposed_for_the_next_transaction_and_can_be_forgotten(ledger, live):
    """Ține minte: kept for the account, card or merchant, proposed (never applied) on its next transactions."""
    iban = "RO49AAAA1B31007593840000"
    first, card, later, card_later = _record(
        ledger,
        "NORD",
        _tx(60, "debit", counterparty_iban=iban),
        _tx(61, "debit", card_last4="0003", counterparty="SHOP"),
        _tx(62, "debit", counterparty_iban=iban),
        _tx(63, "debit", card_last4="0003", counterparty="ALT SHOP"),
    )
    with pytest.raises(LinkRefused) as e:
        _link(ledger, first, kind="advance", who="Administrator Exemplu", remember="card")  # no card on it
    assert e.value.code == "remember"
    _link(ledger, first, kind="advance", who="Administrator Exemplu", remember="iban")
    assert ledger.assign_card("NORD", card, "personal")
    _link(ledger, card, kind="label", reason="personal", remember="card")
    rows = _rows(ledger)
    assert rows[later]["explanation"] is None and rows[later]["proposal"]["as"] == {
        "kind": "advance",
        "partner": None,
        "reason": None,
        "note": "Administrator Exemplu",
    }
    assert (
        rows[card_later]["proposal"]["as"]["reason"] == "personal"
        and rows[card_later]["remember"]["card"] == "••••0003"
    )
    view = _view(ledger)
    assert iban not in json.dumps(view) and [m["by"] for m in view["remembered"]] == ["card", "iban"]
    assert ledger.forget("NORD", view["remembered"][0]["id"]) and _rows(ledger)[card_later]["proposal"] is None


def test_a_remembered_merchant_never_shows_an_account_and_a_remembered_advance_never_pays_an_invoice(ledger, live):
    full = "RO49AAAA1B31007593840000"
    card, out = _record(
        ledger,
        "NORD",
        _tx(64, "debit", counterparty=f"SHOP {full}", card_last4="0004"),
        _tx(65, "debit", counterparty_iban=full, counterparty="Furnizor Exemplu SRL"),
    )
    assert ledger.assign_card("NORD", card, "NORD")
    _link(ledger, card, kind="label", reason="other", note="abonament", remember="name")
    _link(ledger, out, kind="advance", who="Administrator Exemplu", remember="iban")
    view = _view(ledger, live=True)
    assert full not in json.dumps(view)
    _bill(ledger, "212")  # the same name and amount, another account: the owner's transfer settles nothing
    later = _tx(66, "debit", counterparty_iban=full, counterparty="Furnizor Exemplu SRL")
    item = scenario._op(ledger, 67, "NORD", "100.00", TODAY - timedelta(days=10))  # nor a prepared payment to it
    ledger.db.execute("UPDATE payments SET payee_iban=? WHERE item_id=?", (full, item))
    st = statements.Statement("NORD", None, None, [statements.Account("x", "RON", [later])])
    recon.auto_mark_paid(ledger, st)
    efactura.match_statement(ledger, st)
    assert ledger.efactura("212")["status"] == "open" and ledger.payment(item)["status"] == "pending"


def test_a_debit_waits_for_its_invoice_still_to_explain_and_the_invoice_still_settles_it(ledger, live):
    """Aștept factura: an optional note, nothing reserved, listed last in De explicat; a credit or a settled debit
    can't wait; on=0 stops waiting."""
    rent, credit, fee = _record(
        ledger,
        "NORD",
        _tx(70, "debit", "1200.00", description="Chirie octombrie", day=TODAY - timedelta(days=1)),  # the newest
        _tx(71),
        _tx(72, "debit", "5.00", category=statements.BANK_FEE),
    )
    wait = {"co": "NORD", "tx": rent, "on": "1", "note": ""}
    assert not actions.act("tx-await", "NORD", wait | {"on": ""}, created_by="u")["ok"]
    assert actions.act("tx-await", "NORD", wait, created_by="u")["ok"] and _rows(ledger)[rent]["awaiting"]["note"] == ""
    pages, cursor = [], ""  # one row a page: the cursor walks past the waiting debit too
    while cursor is not None:
        page = lists.browse(ledger, "NORD", {"screen": "tranzactii", "f": "", "limit": "1", "cursor": cursor})
        pages, cursor = pages + [r["id"] for r in page["data"]], page["next_cursor"]
    assert pages[-1] == rent and sorted(pages) == sorted([rent, credit, fee])
    assert actions.act("tx-await", "NORD", wait | {"note": "Factura chiriei"}, created_by="u")["ok"]
    assert not links.await_invoice(ledger, "NORD", credit, "Factura")
    with pytest.raises(LinkRefused):
        links.await_invoice(ledger, "NORD", rent, "x" * 201)
    rows, view = _rows(ledger), _view(ledger)
    assert rows[rent]["awaiting"]["note"] == "Factura chiriei" and rows[rent]["explanation"] is None
    assert view["to_explain"] == 3 and ledger.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 0
    assert (rows[fee]["bank_says"], rows[rent]["bank_says"]) == ("bank_fee", None)  # the default the sheet starts on
    _bill(ledger, "270", "1200.00")
    _link(ledger, rent, invoices=["270"])
    assert _rows(ledger)[rent]["awaiting"] is None and not links.await_invoice(ledger, "NORD", rent, "Alta")
    assert actions.act("tx-await", "NORD", wait | {"on": "0"})["ok"]  # stops: the note goes
    assert not ledger.awaited("NORD") and not actions.act("tx-await", "NORD", wait | {"on": "0"})["ok"]


def _apply(L, rows: dict | None = None) -> list[str]:
    return sorted(links.apply_rules(L, "NORD", list((rows or _rows(L)).values())))


def _saved_on(L, day: date) -> None:
    L.db.execute("UPDATE tx_memory SET at=?", (f"{day}T09:00:00",))
    L.db.commit()


def test_saved_rules_and_the_banks_own_fee_lines_explain_their_lines_and_an_undo_sticks(ledger, live):
    """Fără document and Avans rules explain every line of theirs still to explain, from before the day they were
    saved too, and the bank's own fee line explains itself (the rule kept on the explanation); a line waiting for its
    invoice or one a person undid is left alone."""
    iban = "RO49AAAA1B31007593840000"
    first, card, fee, later, card_later, waiting, older = _record(
        ledger,
        "NORD",
        _tx(80, "debit", counterparty_iban=iban),
        _tx(81, "debit", card_last4="0003", counterparty="SHOP"),
        _tx(82, "debit", "5.00", category=statements.BANK_FEE, description="Comision administrare cont"),
        _tx(83, "debit", "40.00", counterparty_iban=iban),
        _tx(84, "debit", "12.00", card_last4="0003", counterparty="ALT SHOP"),
        _tx(85, "debit", "70.00", counterparty_iban=iban),
        _tx(86, "debit", "90.00", counterparty_iban=iban, day=TODAY - timedelta(days=20)),
    )
    _link(ledger, first, kind="advance", who="Administrator Exemplu", remember="iban")
    assert ledger.assign_card("NORD", card, "personal")
    _link(ledger, card, kind="label", reason="personal", remember="card")
    assert links.await_invoice(ledger, "NORD", waiting, "")
    _saved_on(ledger, TODAY - timedelta(days=10))
    assert _apply(ledger) == sorted([fee, later, card_later, older])
    rows = _rows(ledger)
    assert (rows[later]["explanation"]["who"], rows[later]["explanation"]["link"]["rule"]) == (
        "Administrator Exemplu",
        "saved",
    )
    assert (rows[card_later]["explanation"]["label"], rows[fee]["explanation"]["label"]) == ("personal", "bank_fee")
    assert rows[fee]["explanation"]["link"]["rule"] == "bank" and rows[first]["explanation"]["link"]["rule"] is None
    assert rows[waiting]["explanation"] is None and rows[older]["explanation"]["link"]["rule"] == "saved"
    links.unlink(ledger, "NORD", fee, rows[fee]["explanation"]["link"]["id"])
    assert _apply(ledger) == [] and _rows(ledger)[fee]["explanation"] is None


def test_a_rule_rechecks_under_the_lock_what_a_person_decided_since_the_rows_were_read(ledger, live):
    """Rows read, then a person waits for an invoice, forgets or replaces the rule, or undoes a transfer: the stale
    rows explain nothing (both ends of a transfer are left to people)."""
    iban, own = "RO49AAAA1B31007593840000", "RO71BTRLEURCRT0000000001"
    first, waits, forgot, out, back = _record(
        ledger,
        "NORD",
        _tx(90, "debit", counterparty_iban=iban),
        _tx(91, "debit", "41.00", counterparty_iban=iban),
        _tx(92, "debit", "42.00", counterparty_iban=iban),
        _tx(93, "debit", "43.00", counterparty_iban=own),
        _tx(94, "credit", "43.00", iban=own),
    )
    _link(ledger, first, kind="advance", who="Administrator Exemplu", remember="iban")
    stale = _rows(ledger)
    assert links.await_invoice(ledger, "NORD", waits, "")
    assert _apply(ledger, {k: stale[k] for k in (waits,)}) == []
    ledger.forget("NORD", _view(ledger)["remembered"][0]["id"])
    assert _apply(ledger, {k: stale[k] for k in (forgot,)}) == []
    done = _link(ledger, out, kind="transfer")
    assert done["other"] == back
    links.unlink(ledger, "NORD", out, done["id"])
    assert ledger.rules_off("NORD") >= {out, back}


def test_a_rule_leaves_a_card_debit_its_receipt_explains(ledger, live):
    """A merchant rule never takes a debit an exact receipt explains, even one that arrives after the rows were
    read (re-checked under the lock): the receipt stays where it is."""
    card, later = _record(
        ledger,
        "NORD",
        _tx(95, "debit", card_last4="0005", counterparty="SHOP EXEMPLU"),
        _tx(96, "debit", "25.00", card_last4="0005", counterparty="SHOP EXEMPLU"),
    )
    assert ledger.assign_card("NORD", card, "NORD")
    _link(ledger, card, kind="label", reason="other", note="abonament", remember="name")
    stale = _rows(ledger)
    assert stale[later]["proposal"]["kind"] == "remembered"
    day = (TODAY - timedelta(days=3)).isoformat()
    ledger.add_document(
        "r" * 64,
        "NORD",
        day[:7],
        "receipt",
        "bon.jpg",
        vendor="SHOP EXEMPLU",
        doc_date=day,
        amount="25.00",
        currency="RON",
    )
    assert _apply(ledger, {later: stale[later]}) == [] and _apply(ledger) == []
    assert _rows(ledger)[later]["explanation"]["kind"] == "receipt"


def test_a_rule_never_links_or_settles_an_invoice_on_its_own(ledger, live):
    """A remembered partner's invoices stay a proposal: the next debit to that supplier settles nothing by a rule."""
    first, later = _record(
        ledger,
        "NORD",
        _tx(97, "debit", counterparty="Furnizor Exemplu SRL"),
        _tx(98, "debit", counterparty="Furnizor Exemplu SRL"),
    )
    _bill(ledger, "311")
    _bill(ledger, "312")
    _link(ledger, first, invoices=["311"], remember="name")
    assert _apply(ledger) == []
    assert ledger.efactura("312")["status"] == "open"
    assert not ledger.db.execute("SELECT 1 FROM settled_tx WHERE tx_key=?", (later,)).fetchone()
    assert _rows(ledger)[later]["proposal"]["as"]["kind"] == "documents"


def test_a_transfer_between_the_firms_accounts_explains_itself_when_one_end_names_the_other(ledger, live):
    """A debit naming another of the firm's accounts and the credit there, both free: a Transfer între conturi with
    both ends, made by no person. Two ends that share only the amount, naming no account, are left to people, and so
    is a transfer a person undid."""
    eur = "RO71BTRLEURCRT0000000001"
    out, into, bare_out, bare_in = _record(
        ledger,
        "NORD",
        _tx(60, "debit", "5000.00", counterparty_iban=eur),
        _tx(61, "credit", "5000.00", iban=eur),
        _tx(62, "debit", "700.00"),
        _tx(63, "credit", "700.00", iban=eur),
    )
    assert _apply(ledger) == [out]
    rows = _rows(ledger)
    assert (rows[into]["explanation"]["other"], rows[out]["explanation"]["link"]["rule"]) == (out, "transfer")
    assert rows[bare_out]["explanation"] is None and rows[bare_in]["explanation"] is None
    links.unlink(ledger, "NORD", out, rows[out]["explanation"]["link"]["id"])
    assert _apply(ledger) == [] and _rows(ledger)[into]["explanation"] is None


EUR_ACCOUNT = "RO71BTRLEURCRT0000000001"


@pytest.mark.parametrize(
    "case",
    [
        "two debits, one credit",
        "a report claims the credit",
        "another firm's account",
        "an unreadable line",
        "a rule",
        "an invoice the credit names",
        "two invoices the credit names",
        "an invoice issued after the read",
        "its number without zeros",
    ],
)
def test_a_transfer_rule_leaves_a_pair_it_cant_be_sure_of(ledger, live, monkeypatch, case):
    """Settled by nothing: a second debit that fits the credit too, a pending report's payer claiming the credit or a
    rule saved for it after the rows were read, a debit on another firm's account, a same-amount line fiscalm can't
    read, a credit whose details name an open invoice at any amount, even one issued after the rows were read."""
    debit = {"iban": "RO94BTRLRONCRT0000000002"} if case == "another firm's account" else {}
    out, into = _record(
        ledger,
        "NORD",
        _tx(67, "debit", "800.00", counterparty_iban=EUR_ACCOUNT, **debit),
        _tx(
            68,
            "credit",
            "800.00",
            iban=EUR_ACCOUNT,
            counterparty="Administrator Exemplu",
            description="Plata NRD 7031 si NRD 7032 NRD-42",
        ),
    )
    if case in ("an invoice the credit names", "two invoices the credit names"):
        _sale(ledger, "7031", "800.00" if case == "an invoice the credit names" else "300.00")
    if case == "its number without zeros":
        scenario._issued(ledger, "7033", "NORD", "NRD 0042", CLIENT, "5001", TODAY, TODAY, "300.00")
    if case == "two invoices the credit names":
        _sale(ledger, "7032", "500.00")
    if case == "two debits, one credit":
        _record(
            ledger, "NORD", _tx(66, "debit", "800.00", counterparty_iban=EUR_ACCOUNT, day=TODAY - timedelta(days=4))
        )
    if case == "an unreadable line":
        ledger.db.execute(
            "INSERT INTO statement_txs(tx_key,company,direction,tx,day,amount,iban) VALUES('x1','NORD','credit',NULL,?,"
            "'800.00',?)",
            ((TODAY - timedelta(days=3)).isoformat(), EUR_ACCOUNT),
        )
        ledger.db.commit()
    rows = _rows(ledger)
    if case == "a rule":
        name = efactura.line_keys(_tx(68, "credit", "800.00", counterparty="Administrator Exemplu"), frozenset())[
            "name"
        ]
        ledger.remember("NORD", "name", name, {"kind": "advance", "partner": None, "reason": None, "note": "A"}, None)
    if case == "an invoice issued after the read":
        _sale(ledger, "7031", "800.00")
    monkeypatch.setattr(settlements, "claimed_credit", lambda L, co, t: case == "a report claims the credit")
    assert (
        _apply(ledger, rows) == []
        and not ledger.db.execute("SELECT 1 FROM settled_tx WHERE tx_key=?", (into,)).fetchone()
    )


def test_a_transfer_rule_skips_bank_interest_and_an_unassigned_card(ledger, live):
    """The bank's interest is never a transfer's other end; a card debit nobody assigned is skipped, not a stop."""
    card, interest_out, interest, out, into = _record(
        ledger,
        "NORD",
        _tx(69, "debit", "30.00", card_last4="0009", counterparty="SHOP EXEMPLU"),
        _tx(70, "debit", "12.00", counterparty_iban=EUR_ACCOUNT),
        _tx(71, "credit", "12.00", iban=EUR_ACCOUNT, category=statements.INTEREST, day=TODAY - timedelta(days=5)),
        _tx(72, "debit", "410.00", counterparty_iban=EUR_ACCOUNT),
        _tx(73, "credit", "410.00", iban=EUR_ACCOUNT),
    )
    assert _apply(ledger) == sorted([interest, out])  # the interest as Fără document, by the bank's own words
    assert _rows(ledger)[interest_out]["explanation"] is None and _rows(ledger)[card]["explanation"] is None


def test_two_connections_explaining_the_same_transfer_explain_it_once(ledger, live, monkeypatch):
    """Both read the same rows and race to explain the transfer: one explains it, the other finds both ends taken."""
    out, _ = _record(
        ledger,
        "NORD",
        _tx(64, "debit", "900.00", counterparty_iban=EUR_ACCOUNT),
        _tx(65, "credit", "900.00", iban=EUR_ACCOUNT),
    )
    stale, slow, gate, done = list(_rows(ledger).values()), links.other_end, threading.Barrier(2), []

    def other_end(*args, **kw):
        time.sleep(0.2)
        return slow(*args, **kw)

    def run() -> None:
        L = Ledger(timeout=10)
        gate.wait()
        try:
            done.append(links.apply_rules(L, "NORD", stale))
        except Exception as e:  # noqa: BLE001 - a worker's failure is the test's
            done.append(e)
        finally:
            L.close()

    monkeypatch.setattr(links, "other_end", other_end)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(done) == [[], [out]] and ledger.db.execute("SELECT count(*) FROM tx_links").fetchone()[0] == 1


def test_a_stale_rule_yields_to_the_rule_that_wins_now(ledger, live):
    """Rows read under a merchant's Avans rule, then a card rule saved: the card's rule wins, the stale one writes
    nothing."""
    first, later = _record(
        ledger,
        "NORD",
        _tx(74, "debit", "15.00", card_last4="0007", counterparty="SHOP EXEMPLU", day=TODAY - timedelta(days=6)),
        _tx(75, "debit", "16.00", card_last4="0007", counterparty="SHOP EXEMPLU"),
    )
    assert ledger.assign_card("NORD", first, "personal")
    _link(ledger, first, kind="advance", who="Administrator Exemplu", remember="name")
    stale = _rows(ledger)
    assert stale[later]["proposal"]["as"]["kind"] == "advance"
    ledger.remember(
        "NORD", "card", "0007", {"kind": "label", "partner": None, "reason": "personal", "note": None}, None
    )
    assert _apply(ledger, {later: stale[later]}) == [] and _apply(ledger) == [later]
    assert _rows(ledger)[later]["explanation"]["label"] == "personal"
