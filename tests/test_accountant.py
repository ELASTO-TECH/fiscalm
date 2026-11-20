"""Accountant roles never grant supplier payment or account trust."""

import email.utils
from contextlib import nullcontext
from datetime import date
from decimal import Decimal

import pytest
from synthetic_world import build

from fiscalm import accountant, companies, recon
from fiscalm.api import actions, state as api
from fiscalm.mail import imap as imapread, invoices as mail_invoices
from fiscalm.payments.orders import ParseResult, PaymentItem


def partner(L, co="NORD", cui="55555555"):
    L.add_efactura(
        dict(
            message_id=co,
            company=co,
            supplier_name="Servicii Contabile Exemplu SRL",
            supplier_cui=cui,
            number="SC 1",
            issue_date="2026-10-01",
            total="100.00",
            currency="RON",
            status="open",
            raw_path="synthetic.xml",
        )
    )


def test_per_firm_setting_and_clear(ledger):
    partner(ledger)
    assert accountant.settings(ledger, "NORD") == dict(cui="", senders=[], recipient="")
    body = dict(
        co="NORD",
        cui="55555555",
        senders="Office@accountant.example\noffice@accountant.example",
        recipient="pack@accountant.example",
    )
    result = actions.act("accountant-save", "NORD", body)
    assert result["ok"]
    assert api.state(ledger, "NORD")["accountant"] == dict(
        cui="55555555", senders=["office@accountant.example"], recipient=body["recipient"]
    )
    assert accountant.settings(ledger, "STUDIO9")["cui"] == ""
    assert ledger.efactura("NORD")["status"] == "open"
    assert actions.act("accountant-save", "NORD", dict(co="NORD", cui="", senders="", recipient=""))["ok"]
    assert accountant.settings(ledger, "NORD")["cui"] == ""
    assert "accountant-save" in actions.LOCAL_ACTIONS
    assert "accountant-save" not in actions.API_ACTIONS


# 11111111 is STUDIO9's partner: a firm chooses only among its own.
@pytest.mark.parametrize(
    "changes",
    [
        dict(cui="11111111"),
        dict(cui=""),
        dict(recipient="a@x.example\nBcc:b@y.example"),
        dict(senders="a@x.example or from:b@y.example"),
        dict(iban="anything"),
        dict(senders=1),
    ],
)
def test_invalid_write_preserves_setting(ledger, changes):
    partner(ledger)
    partner(ledger, "STUDIO9", "11111111")
    args = dict(co="NORD", cui="55555555", senders="op@accountant.example", recipient="pack@accountant.example")
    assert actions.act("accountant-save", "NORD", args)["ok"]
    before = accountant.settings(ledger, "NORD")
    assert not actions.act("accountant-save", "NORD", args | changes)["ok"]
    assert accountant.settings(ledger, "NORD") == before


def test_accountant_mailbox_matching_is_exact(ledger):
    partner(ledger)
    accountant.save(ledger, "NORD", "55555555", "op@accountant.example", "")
    found = [
        s
        for s in (
            "Other <evil-op@accountant.example>",
            "Display op@accountant.example <evil@example.com>",
            "Office <op@accountant.example>",
        )
        if mail_invoices.known(
            ledger, imapread.Message("a", s, s, email.utils.parseaddr(s)[1].lower(), "s", "2026-10-01", ())
        )
    ]
    assert found == ["Office <op@accountant.example>"]


def test_ro_prefix_and_sender_limit(ledger):
    partner(ledger)
    assert accountant.save(ledger, "NORD", "RO55555555", "", "")["cui"] == "55555555"
    with pytest.raises(ValueError, match="10"):
        accountant.save(ledger, "NORD", "55555555", "\n".join(f"op{i}@accountant.example" for i in range(11)), "")


def test_corrupt_setting_fails_closed(ledger):
    for value in ("bad", '{"cui":1}', '{"cui":"55555555","senders":["bad"],"recipient":""}'):
        ledger.set_meta("accountant:NORD", value)
        assert accountant.settings(ledger, "NORD") == dict(cui="", senders=[], recipient="")


def test_mail_op_refuses_other_firm_before_insert(ledger, home, monkeypatch):
    path = home / "op.pdf"
    path.write_bytes(b"%PDF synthetic")
    item = PaymentItem(
        "STUDIO9",
        "op",
        "Treasury",
        "",
        "",
        "33333333",
        Decimal("100.00"),
        "tax",
        date(2026, 10, 1),
        "OP 1",
        str(path),
        "sha",
    )
    monkeypatch.setattr(recon, "parse_payment_pdf", lambda p: ParseResult(str(p), item))
    assert recon.ingest_file(ledger, path, allowed_companies=["NORD"])["status"] == "refused"
    assert ledger.payment(item.item_id) is None
    assert recon.ingest_file(ledger, path, allowed_companies=["STUDIO9"])["status"] == "added"


@pytest.mark.parametrize("tax_id,allowed", [("RO55555555", True), ("DE55555555", False)])
def test_accountant_tax_identity_keeps_country(ledger, tax_id, allowed):
    partner(ledger)
    ledger.db.execute("UPDATE efactura SET supplier_tax_id=? WHERE company='NORD'", (tax_id,))
    ledger.db.commit()
    with pytest.raises(ValueError, match="partenerii") if not allowed else nullcontext():
        assert accountant.save(ledger, "NORD", "55555555", "", "")["cui"] == "55555555"


def test_every_selectable_scenario_partner_can_be_saved(ledger, home, monkeypatch):
    build(ledger, home, monkeypatch.setattr)
    checked = 0
    for co in companies.all():
        for p in api.state(ledger, co)["partners"]:
            if p["cui"] and p["cui"].isdigit() and 2 <= len(p["cui"]) <= 10:
                assert accountant.save(ledger, co, p["cui"], "", "")["cui"] == p["cui"]
                checked += 1
    assert checked >= 24
