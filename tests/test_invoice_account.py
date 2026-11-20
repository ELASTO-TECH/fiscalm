"""The account an invoice asks clients to pay into is one of the company's accounts, chosen, not typed."""

import dataclasses
import json

import pytest
from firms import set_firm
from test_invoicing import DATA, PARTY, draft, setup_invoice as setup_invoice  # noqa: F401

from fiscalm import companies
from fiscalm.invoicing import engine as i, pdf as invoice_pdf, ubl, workspace as invoicing_api
from fiscalm.pdftext import pdf_text

NORD_RON, NORD_EUR, STUDIO9_RON = "RO24BTRLRONCRT0000000001", "RO71BTRLEURCRT0000000001", "RO94BTRLRONCRT0000000002"


def saved(ledger, co="NORD"):
    return json.loads(ledger.db.execute("SELECT data FROM invoice_sellers WHERE company=?", (co,)).fetchone()[0])


def store(ledger, profile):
    """Write a seller profile as the ledger holds it, bypassing the checks of save_seller."""
    ledger.db.execute("UPDATE invoice_sellers SET data=? WHERE company='NORD'", (json.dumps(profile),))
    ledger.db.commit()


def retire(monkeypatch, iban, day):
    nord = companies.get("NORD")
    accounts = tuple(dataclasses.replace(a, pays=False, retired=day) if a.iban == iban else a for a in nord.accounts)
    set_firm("NORD", dataclasses.replace(nord, accounts=accounts))


def series_next(L):
    return L.db.execute("SELECT next_number FROM invoice_series WHERE company='NORD' AND code='TEST'").fetchone()[0]


@pytest.mark.parametrize("iban,retired", [(STUDIO9_RON, False), ("RO00ZZZZ0000000000000001", False), (NORD_EUR, True)])
def test_an_iban_that_isnt_the_companys_current_account_is_refused(ledger, monkeypatch, iban, retired):
    if retired:
        retire(monkeypatch, iban, "2026-10-01")
    with pytest.raises(i.InvoiceError, match="trebuie să fie un cont al firmei"):
        i.save_seller(ledger, "NORD", PARTY | {"iban": iban})
    assert not ledger.db.execute("SELECT 1 FROM invoice_sellers").fetchone()


@pytest.mark.parametrize("given", ["", "Altă bancă"])
def test_a_catalogue_bank_is_named_by_the_catalogue(ledger, given):
    i.save_seller(ledger, "NORD", PARTY | {"iban": " ro71 btrl eurc rt00 0000 0001", "bank": given})
    assert (saved(ledger)["iban"], saved(ledger)["bank"]) == (NORD_EUR, "Banca Transilvania")


def test_the_workspace_offers_the_companys_current_accounts(ledger):
    accounts = invoicing_api.workspace(ledger, "NORD", "2026-11-03")["accounts"]
    assert accounts == [
        dict(iban=NORD_RON, currency="RON", bank="bt", pays=True, bank_name="Banca Transilvania"),
        dict(iban=NORD_EUR, currency="EUR", bank="bt", pays=False, bank_name="Banca Transilvania"),
    ]
    assert [a["iban"] for a in invoicing_api.workspace(ledger, "STUDIO9", "2026-11-03")["accounts"]] == [STUDIO9_RON]


def test_a_spaced_iban_of_a_current_account_issues_with_the_account_as_configured(setup_invoice):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    store(L, saved(L) | {"iban": " ro24 btrl ronc rt00 0000 0001 "})
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    assert i.invoice(L, iid, company="NORD")["data"]["seller"]["iban"] == NORD_RON


@pytest.mark.parametrize("gone", ["unknown", "retired"])
def test_the_account_refusals_reach_the_app_and_api_word_for_word(setup_invoice, home, monkeypatch, gone):
    L, _ = setup_invoice
    (home / "state" / "LIVE").touch()
    monkeypatch.setattr(invoicing_api.ledger, "now", lambda: "2026-10-31T12:00:00")
    r = invoicing_api.act(
        L, "seller-save", "NORD", {"seller": invoicing_api.party_out(PARTY | {"iban": STUDIO9_RON}, True)}
    )
    assert not r["ok"] and r["message"] == i.NOT_AN_ACCOUNT
    iid = draft(setup_invoice)
    # A profile saved before accounts were checked, or an account retired after the draft, issues nothing.
    if gone == "unknown":
        store(L, saved(L) | {"iban": "RO00FAKE0000000000000000"})
    else:
        retire(monkeypatch, NORD_RON, "2026-10-30")
    before = series_next(L)
    r = invoicing_api.act(L, "issue", "NORD", {"id": iid, "version": 1})
    assert not r["ok"] and r["message"] == i.ACCOUNT_GONE
    assert series_next(L) == before and i.invoice(L, iid, company="NORD")["status"] == "draft"


@pytest.mark.parametrize("profile, ron, eur", [(NORD_RON, NORD_RON, NORD_EUR), (NORD_EUR, NORD_RON, NORD_EUR)])
def test_a_new_invoice_starts_with_the_profiles_account_in_its_currency(setup_invoice, profile, ron, eur):
    L, _ = setup_invoice
    i.save_seller(L, "NORD", PARTY | {"iban": profile})
    choices = invoicing_api.workspace(L, "NORD", "2026-11-03")["invoice_accounts"]
    assert [a["iban"] for a in choices["accounts"]] == [NORD_RON, NORD_EUR]
    assert choices["default"] == {"EUR": eur, "RON": ron}


def test_the_chosen_account_is_on_the_pdf_and_the_xml_and_stays_once_issued(setup_invoice, monkeypatch):
    L, _ = setup_invoice  # the profile names NORD_RON
    iid = draft(setup_invoice, DATA | {"iban": " ro71 btrl eurc rt00 0000 0001"})
    assert i.invoice(L, iid, company="NORD")["data"]["iban"] == NORD_EUR
    spaced = " ".join(NORD_EUR[n : n + 4] for n in range(0, len(NORD_EUR), 4))
    assert spaced in pdf_text(invoice_pdf.generate(L, iid, company="NORD", preview=True))
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    xml, pdf = ubl.generate(L, iid, company="NORD"), invoice_pdf.render(L, iid, company="NORD")
    flat = xml.replace(b"\n", b"").replace(b" ", b"")
    assert f"<cac:PayeeFinancialAccount><cbc:ID>{NORD_EUR}</cbc:ID>".encode() in flat
    assert spaced in pdf_text(pdf) and NORD_RON not in pdf_text(pdf).replace(" ", "")
    retire(monkeypatch, NORD_EUR, "2026-11-01")  # an issued invoice never changes, whatever its account becomes
    assert ubl.generate(L, iid, company="NORD") == xml
    assert i.invoice(L, iid, company="NORD")["data"]["seller"]["iban"] == NORD_EUR


@pytest.mark.parametrize("when", ["save", "issue"])
def test_an_account_that_isnt_the_firms_current_one_saves_or_issues_nothing(setup_invoice, monkeypatch, when):
    L, _ = setup_invoice
    if when == "save":  # another firm's account
        with pytest.raises(i.InvoiceError, match=i.ACCOUNT_GONE):
            draft(setup_invoice, DATA | {"iban": STUDIO9_RON})
        assert not L.db.execute("SELECT 1 FROM invoices").fetchone()
        return
    iid = draft(setup_invoice, DATA | {"iban": NORD_EUR})
    retire(monkeypatch, NORD_EUR, "2026-10-30")
    before = series_next(L)
    with pytest.raises(i.InvoiceError, match=i.ACCOUNT_GONE):
        i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    assert series_next(L) == before and i.invoice(L, iid, company="NORD")["status"] == "draft"


def test_a_saved_draft_shows_the_account_it_issues_with(setup_invoice, monkeypatch):
    L, _ = setup_invoice
    i.save_seller(L, "NORD", PARTY | {"iban": NORD_EUR})
    plain, chosen = draft(setup_invoice), draft(setup_invoice, DATA | {"iban": NORD_RON})  # RON invoices

    def shown():
        return {d["id"]: d["iban"] for d in invoicing_api.workspace(L, "NORD", "2026-11-03")["drafts"]}

    assert shown() == {plain: NORD_EUR, chosen: NORD_RON}  # no choice: the profile's account, as issue uses it
    retire(monkeypatch, NORD_RON, "2026-11-01")
    assert shown()[chosen] == NORD_RON  # a retired choice stays visible, so the app asks for another
    assert [a["iban"] for a in invoicing_api.workspace(L, "NORD", "2026-11-03")["invoice_accounts"]["accounts"]] == [
        NORD_EUR
    ]
    assert i.issue(L, plain, "2026-10-31", company="NORD", version=1)["data"]["seller"]["iban"] == NORD_EUR


def test_a_storno_draft_previews_with_its_originals_account(setup_invoice, monkeypatch):
    L, _ = setup_invoice
    iid = draft(setup_invoice, DATA | {"iban": NORD_EUR})
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    refund = i.storno(L, iid, "2026-10-31", "Retur", [1], company="NORD", draft=True)["id"]
    retire(monkeypatch, NORD_EUR, "2026-11-01")
    spaced = " ".join(NORD_EUR[n : n + 4] for n in range(0, len(NORD_EUR), 4))
    assert spaced in pdf_text(invoice_pdf.generate(L, refund, company="NORD", preview=True))
