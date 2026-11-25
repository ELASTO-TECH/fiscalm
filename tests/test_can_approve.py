"""can_approve is check_payment's verdict: state and API v1 offer approval exactly when `approve` would pass."""

import json
from datetime import date
from decimal import Decimal

import pytest
from conftest import go_live
from firms import iban as synthetic_iban
from test_efactura import OTHER_IBAN, SUPPLIER_IBAN, _debit_st, _stored, _tx, ubl
from test_partner_anaf import CUIS

from fiscalm import companies, recon
from fiscalm.api import actions, state as api, state as api_resources
from fiscalm.bank import catalogue as banks
from fiscalm.efactura import inbox as efactura, public as anaf_public
from fiscalm.payments import btgo

INACTIVE_CUI = CUIS[0]


def _set(ledger, assignments, *args, mid="3001"):
    ledger.db.execute(f"UPDATE efactura SET {assignments} WHERE message_id=?", (*args, mid))
    ledger.db.commit()


def _inactive(ledger, monkeypatch, mid="3001"):
    _set(ledger, "supplier_cui=?,supplier_tax_id=?", INACTIVE_CUI, INACTIVE_CUI, mid=mid)
    anaf_public.store_status(ledger, INACTIVE_CUI, {"checked_at": "2026-10-21", "inactive": True})


def _credited(ledger, monkeypatch, total="250.00", due="2026-11-20"):
    xml = ubl(root="CreditNote", number="COR 2", total=total, due=due).replace(
        b"</CreditNote>",
        b"<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>RMBU 2222602</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference></CreditNote>",
    )
    _stored(ledger, monkeypatch, **{"3002": xml})


# One setup per refusal check_payment can raise for a stored invoice (unknown_* are raised before it reads a row).
REFUSING = {
    "not_open": lambda L, mp: _set(L, "status='paid'"),
    "not_ron": lambda L, mp: _set(L, "currency='EUR'"),
    "supplier_inactive": _inactive,
    "credited": _credited,
    "invoice_issues": lambda L, mp: _set(L, "issues=?", '["synthetic issue"]'),
    "several_ibans": lambda L, mp: _set(L, "ibans=?", json.dumps([SUPPLIER_IBAN, OTHER_IBAN])),
    "no_valid_iban": lambda L, mp: _set(L, "ibans=?", json.dumps(["RO00NOTANIBAN"])),
    "own_iban": lambda L, mp: mp.setattr(
        companies, "company_by_iban", lambda iban: "NORD" if iban == SUPPLIER_IBAN else None
    ),
}


def test_every_check_payment_refusal_is_covered():
    assert set(REFUSING) == efactura.REFUSALS - {"unknown_invoice", "unknown_supplier"}


def _can_approve(ledger, mid="3001"):
    state = {i["id"]: i for i in api.state(ledger, "NORD")["invoices"]}[mid]["can_approve"]
    v1 = api_resources.resource(ledger, "received_one", "NORD", {"id": mid}, {})["can_approve"]
    return state, v1


@pytest.mark.parametrize("code", sorted(REFUSING))
def test_each_refusal_disables_approval_everywhere(ledger, home, monkeypatch, code):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    go_live(home)
    REFUSING[code](ledger, monkeypatch)
    assert efactura.payment_refusal(ledger, ledger.efactura("3001")).code == code
    assert _can_approve(ledger) == ((True, True) if code == "several_ibans" else (False, False))  # a person picks
    unrecorded = api.state(ledger, "NORD")["refused"]
    assert unrecorded == []
    ledger.set_meta("efactura_autopay_refused:3001", "stored before; the reason shown is the current one")
    refused = api.state(ledger, "NORD")["refused"]
    assert [x["why"] for x in refused] == ([] if code == "not_open" else [efactura.shown_refusal(code)])
    result = actions.act("approve", "NORD", {"id": "3001"})
    assert result["ok"] is False and not ledger.db.execute("SELECT 1 FROM payments").fetchone()


def test_ready_invoice_can_be_added_to_named_file_in_sandbox(ledger, home, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    assert api.state(ledger, "NORD")["refused"] == []
    assert efactura.payment_refusal(ledger, ledger.efactura("3001")) is None
    assert _can_approve(ledger) == (True, True)  # adding is local; only generating/downloading needs Live
    go_live(home)
    assert _can_approve(ledger) == (True, True)
    assert actions.act("payment_file_create", "NORD", {"name": "Suppliers", "invoice_ids": ["3001"]})["ok"] is True
    assert _can_approve(ledger) == (False, False)  # it is in the payment file now


def test_one_batched_state_answers_like_each_invoice_alone(ledger, home, monkeypatch):
    """State reads every open invoice's facts at once; each answer matches check_payment run on that invoice alone."""
    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(),
            "3002": ubl(number="X2", supplier="FURNIZOR DOI SRL", supplier_cui="RO5", iban=OTHER_IBAN),
            "3003": ubl(number="X3", supplier="FURNIZOR TREI SRL", supplier_cui="RO7"),
            "3004": ubl(number="X4", supplier="FURNIZOR PATRU SRL", supplier_cui="RO8", currency="EUR"),
        },
    )
    go_live(home)
    _inactive(ledger, monkeypatch, mid="3003")
    shown = {i["id"]: i["can_approve"] for i in api.state(ledger, "NORD")["invoices"]}
    alone = {mid: efactura.payment_refusal(ledger, ledger.efactura(mid)) for mid in shown}
    assert shown == {mid: exc is None for mid, exc in alone.items()}
    assert {mid: exc and exc.code for mid, exc in alone.items()} == {
        "3001": None,
        "3002": None,
        "3003": "supplier_inactive",
        "3004": "not_ron",
    }


@pytest.mark.parametrize("proof", ["none", "marked", "statement", "same", "other-firm", "settled-payment"])
def test_previous_account_notice_requires_statement_proof(ledger, home, monkeypatch, proof):
    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="NEXT", iban=OTHER_IBAN)})
    if proof == "marked":
        ledger.mark_efactura_paid("3001", "user: paid")
    elif proof in {"statement", "same", "other-firm"}:
        ledger.mark_efactura_paid("3001", "statement", "synthetic-debit", to_iban=SUPPLIER_IBAN)
        if proof == "same":
            _set(ledger, "supplier_iban=?,ibans=?", SUPPLIER_IBAN, json.dumps([SUPPLIER_IBAN]), mid="3002")
        elif proof == "other-firm":
            _set(ledger, "company='STUDIO9'")
            ledger.db.execute("UPDATE supplier_ibans SET company='STUDIO9'")
            ledger.db.commit()
    elif proof == "settled-payment":
        item = efactura.approve(ledger, "3001", origin="person")["item_id"]
        ledger.mark_paid(item, "statement", "synthetic-debit")
    go_live(home)
    invoice = next(i for i in api.state(ledger, "NORD")["invoices"] if i["id"] == "3002")
    assert invoice["previous_iban_last4"] == ([SUPPLIER_IBAN[-4:]] if proof in {"statement", "settled-payment"} else [])
    assert invoice["can_approve"]
    assert efactura.payment_item(ledger, "3002").payee_iban == (OTHER_IBAN if proof != "same" else SUPPLIER_IBAN)


TREZ_IBAN = synthetic_iban("RO00TREZ0000999900003333")


@pytest.mark.parametrize(
    ("ibans", "proved", "pays"),
    [
        ([OTHER_IBAN, SUPPLIER_IBAN], SUPPLIER_IBAN, SUPPLIER_IBAN),  # the one proved for this supplier wins
        ([TREZ_IBAN, SUPPLIER_IBAN], None, SUPPLIER_IBAN),  # the only commercial account beside Treasury
        ([SUPPLIER_IBAN, TREZ_IBAN], TREZ_IBAN, TREZ_IBAN),  # a proved Treasury account wins, with Treasury's BIC
        ([SUPPLIER_IBAN, TREZ_IBAN], "both", "several_ibans"),  # two proved: never the TREZ rule
        ([TREZ_IBAN, OTHER_IBAN, SUPPLIER_IBAN], None, "several_ibans"),
        ([OTHER_IBAN, SUPPLIER_IBAN], None, "several_ibans"),
        (["DE00NOTROMANIAN", SUPPLIER_IBAN], None, SUPPLIER_IBAN),  # only valid Romanian IBANs count
    ],
)
def test_an_invoice_with_several_ibans_pays_the_one_account_it_can_prove(
    ledger, home, monkeypatch, ibans, proved, pays
):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    _set(ledger, "ibans=?,supplier_iban=?", json.dumps(ibans), ibans[0])
    for iban in ibans if proved == "both" else [proved] if proved else []:
        _prove(ledger, iban)
    refusal = efactura.payment_refusal(ledger, ledger.efactura("3001"))
    assert (refusal.code if refusal else efactura.payment_item(ledger, "3001").payee_iban) == pays
    if not refusal:  # the XML's BIC is not tied to one of several accounts; Treasury has its own
        item = efactura.payment_item(ledger, "3001")
        assert item.payee_bic == (banks.TREASURY_BIC if pays == TREZ_IBAN else "")
        assert btgo.validate_text(btgo.render(btgo.build_rows([item], date(2026, 10, 27))), "NORD") == []


def _prove(ledger, iban):
    ledger.db.execute(
        "INSERT OR IGNORE INTO supplier_ibans SELECT company,supplier_cui,?,'statement:x' FROM efactura", (iban,)
    )
    ledger.db.commit()


def test_proof_arriving_after_the_account_was_chosen_keeps_paying_that_account_of_the_invoice(
    ledger, home, monkeypatch
):
    """The TREZ rule picked the commercial account; a later proof of the Treasury one changes only the default for new
    payments: the account queued (or built before the writer lock) is the supplier's own, signed in this invoice."""
    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="X2")})
    for mid in ("3001", "3002"):
        _set(ledger, "ibans=?,supplier_iban=?", json.dumps([TREZ_IBAN, SUPPLIER_IBAN]), TREZ_IBAN, mid=mid)
    queued = efactura.approve(ledger, "3001", origin="person")["item_id"]  # unfiled, paying the commercial account
    item = efactura.payment_item(ledger, "3002")  # read before the writer lock
    ledger.set_meta("efactura_autopay_refused:3002", "stored before")
    assert api.state(ledger, "NORD")["refused"][0]["iban_last4"] == SUPPLIER_IBAN[-4:]  # the account it would pay
    _prove(ledger, TREZ_IBAN)
    invoice = {i["id"]: i for i in api.state(ledger, "NORD")["invoices"]}["3001"]
    assert invoice["previous_iban_last4"] == [TREZ_IBAN[-4:]]  # its queued payment pays the unproved account
    assert ledger.queue_efactura(item, "3002", "person") is True
    assert ledger.payment(item.item_id)["payee_iban"] == SUPPLIER_IBAN
    assert efactura.approve(ledger, "3001", origin="person", unfiled=True)["item_id"] == queued  # same row, offered
    assert _can_approve(ledger) == (True, True)
    with pytest.raises(efactura.ApproveError):  # an account the invoice does not list is never paid
        efactura.payment_item(ledger, "3001", account=OTHER_IBAN)


@pytest.mark.parametrize(("issue", "blocks"), [("bad due date '2026-13-01'", False), ("buyer CUI 5 is not NORD", True)])
def test_only_blocking_invoice_issues_refuse_payment(ledger, home, monkeypatch, issue, blocks):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    _set(ledger, "issues=?", json.dumps([issue]))
    refusal = efactura.payment_refusal(ledger, ledger.efactura("3001"))
    assert (refusal.code if refusal else None) == ("invoice_issues" if blocks else None)


@pytest.mark.parametrize("due", ["2026-11-20", "2026-13-01"])  # either may carry an unreadable due date
@pytest.mark.parametrize("note_due", ["2026-11-20", "2026-13-01"])
def test_a_partly_credited_invoice_pays_its_net_remaining(ledger, home, monkeypatch, due, note_due):
    _stored(ledger, monkeypatch, **{"3001": ubl(due=due)})
    _credited(ledger, monkeypatch, "50.00", note_due)
    assert efactura.payment_refusal(ledger, ledger.efactura("3001")) is None
    assert efactura.payment_item(ledger, "3001").amount == Decimal("200.00")


def test_a_person_picks_one_of_several_unproved_accounts_and_fiscalm_remembers_it_once_paid(ledger, home, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="X2", total="300.00")})  # no rival debit
    for mid in ("3001", "3002"):
        _set(ledger, "ibans=?,supplier_iban=?", json.dumps([OTHER_IBAN, SUPPLIER_IBAN]), OTHER_IBAN, mid=mid)
    invoice = {i["id"]: i for i in api.state(ledger, "NORD")["invoices"]}["3001"]
    assert invoice["can_approve"] and invoice["accounts"] == [
        {"index": 0, "bank": OTHER_IBAN[4:8], "ending": OTHER_IBAN[-4:]},
        {"index": 1, "bank": SUPPLIER_IBAN[4:8], "ending": SUPPLIER_IBAN[-4:]},
    ]
    for picks in ({}, {"3001": 2}, {"3001": True}, {"3002": 1}):
        body = {"name": "Picked", "invoice_ids": ["3001"], "accounts": picks}
        assert actions.act("payment_file_create", "NORD", body)["ok"] is False
    body = {"name": "Picked", "invoice_ids": ["3001"], "accounts": {"3001": 1}}
    assert actions.act("payment_file_create", "NORD", body)["ok"] is True
    item = ledger.efactura("3001")["item_id"]
    assert ledger.payment(item)["payee_iban"] == SUPPLIER_IBAN
    assert efactura.payment_refusal(ledger, ledger.efactura("3002")).code == "several_ibans"
    _stored(ledger, monkeypatch, **{"3003": ubl(number="X3", total="320.00")})  # picked before proof arrives
    _set(ledger, "ibans=?,supplier_iban=?", json.dumps([OTHER_IBAN, SUPPLIER_IBAN]), OTHER_IBAN, mid="3003")
    debit = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL EXEMPLU SRL", SUPPLIER_IBAN, desc="plata factura RMBU")
    assert recon.auto_mark_paid(ledger, _debit_st(debit)) == [item]  # the bank statement proves the picked account
    assert efactura.payment_item(ledger, "3002").payee_iban == SUPPLIER_IBAN
    stale = {"name": "Stale", "invoice_ids": ["3003"], "accounts": {"3003": 0}}  # the other account, no longer offered
    assert actions.act("payment_file_create", "NORD", stale)["ok"] is False


def test_the_picker_never_offers_an_own_account_and_tells_alike_endings_apart(ledger, home, monkeypatch):
    twin = synthetic_iban("RO00INGB0000999900021111")  # same bank and last 4 as SUPPLIER_IBAN (…01111 vs …21111)
    own = synthetic_iban("RO00BTRL0000999900007777")
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    _set(ledger, "ibans=?,supplier_iban=?", json.dumps([own, SUPPLIER_IBAN, twin]), own)
    monkeypatch.setattr(companies, "company_by_iban", lambda iban: "NORD" if iban == own else None)
    accounts = {i["id"]: i for i in api.state(ledger, "NORD")["invoices"]}["3001"]["accounts"]
    assert [(a["index"], a["ending"]) for a in accounts] == [(1, SUPPLIER_IBAN[-5:]), (2, twin[-5:])]
    assert SUPPLIER_IBAN[-4:] == twin[-4:] and SUPPLIER_IBAN[-5:] != twin[-5:]
    far = synthetic_iban("RO00INGB1000999900001111")  # differs from SUPPLIER_IBAN only past 6 characters from the end
    _set(ledger, "ibans=?,supplier_iban=?", json.dumps([SUPPLIER_IBAN, far]), SUPPLIER_IBAN)
    invoice = {i["id"]: i for i in api.state(ledger, "NORD")["invoices"]}["3001"]
    assert invoice["accounts"] == [] and not invoice["can_approve"]  # never more of the IBAN: paid by hand
