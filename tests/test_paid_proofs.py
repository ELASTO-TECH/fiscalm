"""`proved` (the app's "="): evidence proves a received invoice or a BT Go payment paid, never the shape of a key.
Ledger.paid_proofs is the one rule; api.state and the public resources read it. Synthetic data only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import synthetic_world as scenario

from fiscalm import cli
from fiscalm.api import state as api
from fiscalm.bank import statements
from fiscalm.ledger import finance_home

DAY = date(2026, 10, 1)


def _debit(co: str, n: int, direction: str = "debit") -> statements.Tx:
    """A transaction of `co`'s statement, put on record as a sync would (Ledger.record_statement)."""
    return statements.Tx(
        date=DAY,
        title="Plata OP",
        description=f"Plata {n}",
        direction=direction,
        amount=Decimal(f"{n}.00"),
        currency="RON",
        iban=f"RO00BTRLRONCRT00000000{n:02d}",
        ref=f"OP{n}",
    )


def _record(L, co: str, *txs: statements.Tx) -> list[str]:
    L.record_statement(statements.Statement(co, None, None, [statements.Account("x", "RON", list(txs))]))
    return [statements.tx_key(t) for t in txs]


def _invoice(L, mid: str, co: str = "NORD", **kw) -> None:
    scenario._ef(L, mid, co, "Furnizor Test SRL", f"RO{mid}", f"FT-{mid}", DAY, None, "10.00", **kw)


def _payment(L, n: int, co: str = "NORD") -> str:
    """An imported historical payment with its own named file; no bank bytes are generated."""
    item = scenario._op(L, n, co, f"{n}.00", DAY)
    archive = finance_home() / co / "plati" / f"payments-{co}-{DAY}.csv"
    ident = L.create_payment_file(co, "Historical import")
    assert L.attach_payment_file(ident, co, item)
    with L.tx() as db:
        db.execute("UPDATE payments SET status='exported', csv_path=? WHERE item_id=?", (str(archive), item))
        db.execute("UPDATE payment_files SET imported_at=?,path=? WHERE id=?", (DAY.isoformat(), str(archive), ident))
    return item


def _receipt(L, sha: str, co: str) -> None:
    assert L.add_document(
        sha, co, "2026-10", "receipt", f"/nonexistent/{sha}.jpg", vendor="Furnizor Test", amount="10.00"
    )


def _proved(L, co: str = "NORD") -> tuple[dict, dict]:
    s = api.state(L, co)
    return (
        {r["id"]: r["proved"] for r in s["invoices"] if r["status"] == "paid"},
        {
            p["item_id"]: next(
                r["proved"]
                for r in api.payment_file_detail(L, p["file_id"])["payments"]
                if r["id"] == api.payment_id(p["item_id"])
            )
            for p in L.payments(company=co, status="paid")
        },
    )


def test_a_key_with_nothing_behind_it_proves_nothing(ledger):
    """An empty `bon:` or `ef-proof:`, or 20 hex characters that are no statement transaction, were
    taken as proof. The writes go through (the key is reserved), but nothing proves them paid."""
    other = _record(ledger, "STUDIO9", _debit("STUDIO9", 41))[0]
    credit = _record(ledger, "NORD", _debit("NORD", 42, "credit"))[0]
    _receipt(ledger, "f" * 64, "STUDIO9")
    keys = {
        "801": "bon:",
        "802": "ef-proof:",
        "803": "a" * 20,
        "804": "bon:" + "e" * 64,
        "805": "bon:" + "f" * 64,
        "806": "ef-proof:nm:never-read",
        "807": other,
        "808": credit,
    }
    for mid, key in keys.items():
        _invoice(ledger, mid)
        assert ledger.mark_efactura_paid(mid, "payment proof", tx=key)
    by_key = _payment(ledger, 21)
    assert ledger.mark_paid(by_key, "statement", "b" * 20)
    invoices, payments = _proved(ledger)
    assert invoices == dict.fromkeys(keys, False)
    assert payments == {by_key: False}
    assert ledger.paid_proofs("NORD") == {}


def test_evidence_proves_paid(ledger):
    """A recorded debit of the same company reserved for the item, a receipt of the company linked to the invoice, a
    mail read for the invoice: each proves it. A payment's debit proves the invoice it paid too."""
    rent, wages = _record(ledger, "NORD", _debit("NORD", 11), _debit("NORD", 12))
    _invoice(ledger, "811")
    assert ledger.mark_efactura_paid("811", "statement", tx=rent)
    _invoice(ledger, "812")
    _receipt(ledger, "c" * 64, "NORD")
    assert ledger.link_bon("c" * 64, "812", "plătit la casă, bon 0042")
    _invoice(ledger, "813")
    assert (
        cli.main(["efactura", "mark-paid", "813", "--mail", "nm:813.plata@magazin.example", "--note", "mail: plată"])
        == 0
    )
    paid = _payment(ledger, 22)
    _invoice(ledger, "814", status="linked")
    ledger.db.execute("UPDATE efactura SET item_id=? WHERE message_id='814'", (paid,))
    ledger.db.commit()
    assert ledger.mark_paid(paid, "statement", wages)
    invoices, payments = _proved(ledger)
    assert invoices == {"811": True, "812": True, "813": True, "814": True}
    assert payments == {paid: True}
    proofs = ledger.paid_proofs("NORD")
    assert proofs["efactura 811"] == rent and proofs["efactura 814"] == proofs["payment " + paid] == wages
    assert (
        proofs["efactura 812"] == "bon:" + "c" * 64
        and proofs["efactura 813"] == "ef-proof:nm:813.plata@magazin.example"
    )
    assert (
        api.state(ledger, "NORD")["invoices"][[r["id"] for r in api.state(ledger, "NORD")["invoices"]].index("812")][
            "bon"
        ]
        == "c" * 64
    )


def test_a_key_reserved_for_another_item_proves_nothing(ledger):
    """settled_tx says which item a debit paid: a key reserved for another one proves neither this invoice nor, through
    the old fallback, the payment of this invoice. Two invoices settled before settled_tx with one key prove none."""
    taken, twin = _record(ledger, "NORD", _debit("NORD", 31), _debit("NORD", 32))
    other = _payment(ledger, 23)
    assert ledger.mark_paid(other, "statement", taken)  # the debit paid this payment
    queued = _payment(ledger, 24)
    _invoice(ledger, "831", status="paid", paid_at="2026-10-02T10:00:00", paid_note="statement", paid_tx=taken)
    ledger.db.execute("UPDATE efactura SET item_id=? WHERE message_id='831'", (queued,))
    for mid in ("832", "833"):
        _invoice(ledger, mid, status="paid", paid_at="2026-10-02T10:00:00", paid_note="statement", paid_tx=twin)
    ledger.db.commit()
    assert ledger.mark_paid(queued, "marked paid by the user")
    invoices, payments = _proved(ledger)
    assert invoices == {"831": False, "832": False, "833": False}
    assert payments == {other: True, queued: False}


def test_the_old_fallback_stays_in_the_company(ledger):
    """A payment settled before settled_tx is proved by the debit on the invoice it paid, of the same company only."""
    nord, studio = _record(ledger, "NORD", _debit("NORD", 51))[0], _record(ledger, "STUDIO9", _debit("STUDIO9", 52))[0]
    own, foreign = _payment(ledger, 25), _payment(ledger, 26)
    _invoice(ledger, "851", status="paid", paid_at="2026-10-02T10:00:00", paid_note="statement", paid_tx=nord)
    _invoice(
        ledger, "852", "STUDIO9", status="paid", paid_at="2026-10-02T10:00:00", paid_note="statement", paid_tx=studio
    )
    for mid, item in (("851", own), ("852", foreign)):
        ledger.db.execute("UPDATE efactura SET item_id=? WHERE message_id=?", (item, mid))
    ledger.db.commit()
    for item in (own, foreign):
        assert ledger.mark_paid(item, "marked paid by the user")
    assert _proved(ledger)[1] == {own: True, foreign: False}
    assert _proved(ledger, "STUDIO9")[0] == {"852": True}  # the invoice itself stays proved in its own company


def test_a_payment_with_its_own_reservation_does_not_fall_back(ledger):
    """A payment whose own reserved key proves nothing stays unproved, even when its invoice carries a real debit."""
    debit = _record(ledger, "NORD", _debit("NORD", 61))[0]
    paid = _payment(ledger, 27)
    _invoice(ledger, "861", status="paid", paid_at="2026-10-02T10:00:00", paid_note="statement", paid_tx=debit)
    ledger.db.execute("UPDATE efactura SET item_id=? WHERE message_id='861'", (paid,))
    ledger.db.commit()
    assert ledger.mark_paid(paid, "statement", "no statement line")
    assert _proved(ledger) == ({"861": True}, {paid: False})


def test_public_resources_link_only_a_proved_transaction(ledger):
    """The public resources expose a transaction only when the same rule proves the item paid."""
    from fiscalm.api import state as api_resources

    real = _record(ledger, "NORD", _debit("NORD", 71))[0]
    for mid, key in (("871", real), ("872", "d" * 20)):
        _invoice(ledger, mid)
        assert ledger.mark_efactura_paid(mid, "statement", tx=key)
    proved, unproved = _payment(ledger, 28), _payment(ledger, 29)
    assert ledger.mark_paid(proved, "statement", _record(ledger, "NORD", _debit("NORD", 72))[0])
    assert ledger.mark_paid(unproved, "statement", "e" * 20)
    received = {
        r["id"]: (r["proved"], r["links"]["transaction"])
        for r in api_resources.resource(ledger, "received", "NORD", {}, {})["data"]
    }
    assert received == {"871": (True, real), "872": (False, None)}
    payments = {
        r["amount"]: r["links"]["transaction"]
        for r in api_resources.resource(ledger, "payments", "NORD", {}, {})["data"]
    }
    assert payments == {"28.00": ledger.paid_proofs("NORD")["payment " + proved], "29.00": None}


def test_old_statements_are_put_on_record_once(ledger, home, monkeypatch):
    """A debit that settled an item before statement_txs existed proves it again once the sync has read its statement:
    a recent one is matched again, an older one is read once, only for its record."""
    from fiscalm.efactura import inbox as efactura

    old = _debit("NORD", 81)
    path = home / "old.pdf"
    path.write_bytes(b"fake statement")
    assert ledger.add_document("1" * 64, "NORD", "2025-01", "statement", str(path))
    reads = []

    def parse(p, co):
        reads.append(co)
        return statements.Statement(co, None, None, [statements.Account("x", "RON", [old])])

    monkeypatch.setattr(statements, "parse", parse)
    _invoice(
        ledger,
        "881",
        status="paid",
        paid_at="2025-01-20T10:00:00",
        paid_note="statement",
        paid_tx=statements.tx_key(old),
    )
    assert _proved(ledger)[0] == {"881": False}
    efactura.match_recent_statements(ledger)
    efactura.match_recent_statements(ledger)
    assert reads == ["NORD"] and _proved(ledger)[0] == {"881": True}
