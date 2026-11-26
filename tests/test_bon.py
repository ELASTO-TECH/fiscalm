"""A bon fiscal linked to the shop's e-Factura for the same purchase (Ledger.link_bon): paid at the till once, never
twice, never also through the payment file. Synthetic data."""

import json
import threading
from datetime import date
from decimal import Decimal

from synthetic_world import _ef

from fiscalm.api import state as api
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.ledger import Ledger

NORD_CUI = "11111111"


def _bon(ledger, home, day="2026-10-20", shop="Magazin Nordic", total="120.00", nr="0042", co="NORD"):
    f = home / co / day[:7] / "bonuri" / f"bon-{nr}.jpg"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(f"{shop} {nr} {total}".encode())
    sha = f"bon-{co}-{nr}"
    ledger.add_document(
        sha,
        co,
        day[:7],
        "receipt",
        str(f),
        vendor=shop,
        doc_date=day,
        amount=total,
        currency="RON",
        issues=[],
        source="manual",
    )
    return sha


def _link(ledger, sha, mid, nr="0042"):
    return ledger.link_bon(sha, mid, f"plătit la casă, bon {nr}")


def _inv(ledger, mid, issue="2026-10-21", total="120.00", supplier="Magazin Nordic SRL", co="NORD", **kw):
    _ef(ledger, mid, co, supplier, "RO7001", f"MN {mid}", date.fromisoformat(issue), date(2026, 11, 20), total, **kw)


def test_one_pair_is_paid_at_the_till_and_never_reaches_bt_go(ledger, home):
    sha = _bon(ledger, home)
    _inv(ledger, "701")
    assert _link(ledger, sha, "701")
    r = ledger.efactura("701")
    assert (r["status"], r["paid_note"], r["paid_tx"]) == ("paid", "plătit la casă, bon 0042", f"bon:{sha}")
    assert ledger.document(sha)["efactura_id"] == "701" and not _link(ledger, sha, "701")
    assert efactura.unpaid(ledger) == [] and not efactura.awaiting(ledger)
    try:
        efactura.approve(ledger, "701", origin="person")
        raise AssertionError("approved a purchase paid at the till")
    except efactura.ApproveError:
        pass
    s = api.state(ledger, "NORD")
    assert next(i for i in s["invoices"] if i["id"] == "701")["bon"] == sha
    assert next(d for d in s["documents"] if d["id"] == sha)["invoice"] == "701"


def test_a_bon_proves_the_users_mark(ledger, home):
    sha = _bon(ledger, home)
    _inv(ledger, "701")
    ledger.mark_efactura_paid("701", "user: already paid (page)")
    assert _link(ledger, sha, "701")
    assert ledger.efactura("701")["paid_tx"] == f"bon:{sha}" and not efactura.awaiting(ledger)


def _card(amount, d, shop="POS MAGAZIN NORDIC"):
    t = statements.Tx(
        date=d,
        title="Plata la POS",
        description=f"Plata la POS {shop}",
        direction="debit",
        amount=Decimal(amount),
        currency="RON",
        iban="RO24BTRLRONCRT0000000001",
        counterparty=shop,
        counterparty_iban="",
        ref=f"C{amount}",
    )
    return statements.Statement("NORD", None, None, [statements.Account("x", "RON", [t])]), statements.tx_key(t)


def test_cash_or_card_the_purchase_counts_once(ledger, home):
    sha = _bon(ledger, home)
    _inv(ledger, "701")
    assert _link(ledger, sha, "701", "0042")
    _inv(ledger, "702", issue="2026-10-23")  # another purchase, same shop and total, still unpaid
    st, key = _card("120.00", date(2026, 10, 20))
    # The card debit fits the bon's purchase and the open invoice: it settles neither, the user picks.
    assert efactura.match_statement(ledger, st) == [] and ledger.efactura("702")["status"] == "open"
    assert [sorted(c[1] for c in json.loads(a["candidates"])) for a in ledger.ambiguous()] == [["701", "702"]]
    # Once the other invoice is paid otherwise, the debit fits only the bon's purchase: it is used up, never looked for again.
    ledger.mark_efactura_paid("702", "statement 2026-10-25 iban REF T1", "other-debit")
    assert efactura.match_statement(ledger, st) == [] and key in ledger.used_txs()
    assert efactura.bon_paid(ledger, "NORD") == [] and not ledger.settle_bon_card("701", "a-second-debit")
    # Cash: no debit ever comes and the invoice stays paid.
    sha = _bon(ledger, home, day="2026-10-26", total="33.00", nr="0099")
    _inv(ledger, "703", issue="2026-10-26", total="33.00")
    assert _link(ledger, sha, "703", "0099")
    assert efactura.match_statement(ledger, _card("12.00", date(2026, 10, 27))[0]) == []
    assert ledger.efactura("703")["status"] == "paid" and [
        r["message_id"] for r in efactura.bon_paid(ledger, "NORD")
    ] == ["703"]


def test_a_card_debit_picked_by_the_user_for_a_bon_purchase(ledger, home):
    from fiscalm.ledger import finance_home

    (finance_home() / "state").mkdir(parents=True, exist_ok=True)
    (finance_home() / "state" / "LIVE").touch()
    sha = _bon(ledger, home)
    _inv(ledger, "701")
    assert _link(ledger, sha, "701", "0042")
    _inv(ledger, "702", issue="2026-10-23")
    st, key = _card("120.00", date(2026, 10, 20))
    efactura.match_statement(ledger, st)
    assert efactura.resolve_debit(ledger, "NORD", key, "701") == ("efactura", "701")
    assert key in ledger.used_txs() and ledger.efactura("702")["status"] == "open"


def test_two_connections_never_double_link(ledger, home):
    sha = _bon(ledger, home)
    _inv(ledger, "701")
    _inv(ledger, "702", issue="2026-10-22")
    gate, won = threading.Barrier(2), []

    def caller(mid):
        L = Ledger()
        gate.wait()
        won.append(L.link_bon(sha, mid, "plătit la casă, bon 0042"))
        L.close()

    threads = [threading.Thread(target=caller, args=(m,)) for m in ("701", "702")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(won) == [False, True]
    assert sorted(ledger.efactura(m)["status"] for m in ("701", "702")) == ["open", "paid"]
    assert ledger.document(sha)["efactura_id"] in ("701", "702")
    # and two bons for one invoice: the second finds it paid, and its key stays free
    other = _bon(ledger, home, nr="0043")
    paid = next(m for m in ("701", "702") if ledger.efactura(m)["status"] == "paid")
    assert not ledger.link_bon(other, paid, "x") and f"bon:{other}" not in ledger.used_txs()
    assert ledger.document(other)["efactura_id"] is None


def test_approval_and_a_bon_link_never_both_win(ledger, home):
    sha = _bon(ledger, home)
    _inv(ledger, "701")
    item = efactura.payment_item(ledger, "701")  # approval read the invoice open...
    assert ledger.link_bon(sha, "701", "plătit la casă, bon 0042")  # ...then the bon settled it
    assert ledger.queue_efactura(item, "701", "person") is None and ledger.payments() == []
    try:
        efactura.approve(ledger, "701", origin="person")
        raise AssertionError("approved a purchase paid at the till")
    except efactura.ApproveError:
        pass
    # the other order: approved first, the bon then finds it in BT Go and links nothing
    other = _bon(ledger, home, total="75.00", nr="0043")
    _inv(ledger, "702", total="75.00")
    assert efactura.approve(ledger, "702", origin="person")["added"]
    assert not ledger.link_bon(other, "702", "x") and ledger.efactura("702")["status"] == "linked"
    assert f"bon:{other}" not in ledger.used_txs() and ledger.document(other)["efactura_id"] is None


def test_two_debits_racing_for_one_bon_purchase_use_up_one(ledger, home):
    sha = _bon(ledger, home)
    _inv(ledger, "701")
    assert _link(ledger, sha, "701", "0042")
    gate, won = threading.Barrier(2), []

    def caller(key):
        L = Ledger()
        gate.wait()
        won.append(L.settle_bon_card("701", key))
        L.close()

    threads = [threading.Thread(target=caller, args=(k,)) for k in ("debit-a", "debit-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(won) == [False, True] and len({"debit-a", "debit-b"} & ledger.used_txs()) == 1


def test_approval_and_a_bon_link_racing_on_two_connections(ledger, home):
    sha = _bon(ledger, home)
    _inv(ledger, "701")
    item = efactura.payment_item(ledger, "701")
    gate, won = threading.Barrier(2), {}

    def caller(what):
        L = Ledger()
        gate.wait()
        won[what] = (
            L.link_bon(sha, "701", "plătit la casă, bon 0042")
            if what == "bon"
            else L.queue_efactura(item, "701", "person")
        )
        L.close()

    threads = [threading.Thread(target=caller, args=(w,)) for w in ("bon", "approve")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (won["bon"] is True) != (won["approve"] is True)  # exactly one wins
    r = ledger.efactura("701")
    assert (r["status"], len(ledger.payments())) == (("paid", 0) if won["bon"] else ("linked", 1))
