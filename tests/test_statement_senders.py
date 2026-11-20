"""Mail sources follow current accounts, without widening automatic statement import."""

import email.utils
from dataclasses import replace

import pytest
from firms import set_firm

from fiscalm import accountant, companies, company_config, recon
from fiscalm.bank import catalogue as banks, upload as statement_upload
from fiscalm.mail import imap as imapread, invoices as mail_invoices


def test_current_banks_supply_all_senders_once_and_retired_banks_supply_none(monkeypatch, ledger):
    bt = banks.BY_ID["bt"]
    ing = replace(banks.BY_ID["ing"], statement_senders=("statements@ing.example", "archive@ing.example"))
    monkeypatch.setitem(banks.BY_ID, "ing", ing)
    nord = companies.get("NORD")
    added = companies.Account("RO00INGB0000000000000009", "EUR", "ing")
    co = replace(nord, accounts=nord.accounts + (added,))
    set_firm("NORD", co)
    assert set(co.statement_senders) == set(bt.statement_senders + ing.statement_senders)
    rows = company_config.accounts_out(ledger, co)
    assert rows[0]["statement_senders"] == list(bt.statement_senders)
    assert rows[-1]["statement_senders"] == list(ing.statement_senders)
    co = replace(co, accounts=nord.accounts + (replace(added, retired="2026-10-01"),))
    assert co.statement_senders == bt.statement_senders
    assert company_config.accounts_out(ledger, co)[-1]["statement_senders"] == []
    assert replace(co, accounts=(replace(added, bank=""),)).statement_senders == ()


def test_mail_matches_exact_catalogue_addresses_and_keeps_bt_attachment_rule(monkeypatch, ledger):
    ing = replace(banks.BY_ID["ing"], statement_senders=("statements@ing.example",))
    monkeypatch.setitem(banks.BY_ID, "ing", ing)
    monkeypatch.setattr(banks, "CATALOGUE", tuple(ing if b.id == "ing" else b for b in banks.CATALOGUE))
    nord = companies.get("NORD")
    set_firm(
        "NORD", replace(nord, accounts=nord.accounts + (companies.Account("RO00INGB0000000000000009", "EUR", "ing"),))
    )

    def kind(sender, names):
        msg = imapread.Message(
            "a",
            sender,
            sender,
            email.utils.parseaddr(sender)[1].lower(),
            "s",
            "2026-10-01",
            tuple(imapread.Attachment("pdf", b"%PDF-" + n.encode(), n) for n in names),
        )
        got = mail_invoices.known(ledger, msg)
        return got and (got[0], [a.name for a in got[1]])

    assert kind("Bank <STATEMENTS@ing.example>", ["monthly.pdf"]) == ("statement", ["monthly.pdf"])
    assert kind("EmailExtrase@btrl.ro", ["ALLMERGE.pdf", "notice.pdf"]) == ("statement", ["ALLMERGE.pdf"])
    for forged in ("statements@ing.example.evil", '"statements@ing.example" <attacker@example.com>'):
        assert kind(forged, ["forged.pdf"]) is None


@pytest.mark.parametrize(
    "readers,message",
    [
        (
            ("bt", "ing"),
            "fiscalm citește extrase PDF doar de la BT și ING Bank. "
            "Pentru alte bănci se încarcă extrasul CAMT (XML) sau MT940.",
        ),
        ((), "fiscalm nu citește extrase PDF. Pentru alte bănci se încarcă extrasul CAMT (XML) sau MT940."),
    ],
)
def test_pdf_support_names_catalogue_readers(monkeypatch, readers, message):
    ing = replace(banks.BY_ID["ing"], statement_formats=("camt053", "example-pdf"))
    monkeypatch.setattr(banks, "CATALOGUE", tuple(ing if r == "ing" else banks.BY_ID[r] for r in readers))
    assert statement_upload.pdf_support() == message


def test_missing_statement_report_makes_no_arrival_promise(ledger):
    text, summary = recon.build_report(ledger, "NORD", "2026-10")
    assert text.split("## Bank statement\n")[1] == "- No statement for this month yet\n"
    assert summary["statement"] is False


def test_statement_classification_precedes_accountant_sender(monkeypatch, ledger):
    sender = companies.get("NORD").statement_senders[0]
    monkeypatch.setattr(accountant, "settings", lambda L, co: {"senders": [sender.lower()]})
    msg = imapread.Message("a", "k", sender, sender.lower(), "s", "2026-10-01", ())
    assert mail_invoices.known(ledger, msg)[0] == "statement"
