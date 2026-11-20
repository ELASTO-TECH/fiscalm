"""Any number of companies from the config: what the config may hold, what it refuses, and the engine and API
answering for every configured company, not a fixed two. Synthetic companies only."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
import synthetic_world as scenario
from firms import set_firm, set_firms

from fiscalm import companies, docs
from fiscalm.api import actions, state as api
from fiscalm.bank import statements
from fiscalm.companies import ConfigError, companies_from
from fiscalm.efactura import inbox as efactura
from fiscalm.payments import engine as pay

RON = {"RON": "RO00TEST0000000000000009"}


def co(key, **extra):
    return {
        "key": key,
        "legal_name": f"{key} SRL",
        "cui": "1234567" + str(len(key)),
        "accounts": [{"iban": RON["RON"], "currency": "RON", "pays": True}],
    } | extra


def test_any_number_of_companies_in_config_order_with_palettes_in_turn():
    got = companies_from({"companies": [co("ALFA"), co("BETA-2"), co("GAMMA_3", palette="slate", name="Gamma")]})
    assert list(got) == ["ALFA", "BETA-2", "GAMMA_3"]
    assert [c.palette for c in got.values()] == ["amber", "green", "slate"]
    assert got["ALFA"].name == "ALFA" and got["GAMMA_3"].name == "Gamma"
    # Only key, legal_name, cui and ibans.RON are required; the rest has empty defaults.
    a = got["ALFA"]
    assert a.tax_ids() == {a.cui}
    assert a.aliases == ()  # the legal name alone names the firm
    assert a.named_in("Factura ALFA S.R.L.") and not a.named_in("ALFASRLX")


def test_palettes_wrap_after_the_last():
    got = companies_from({"companies": [co(f"C{i}") for i in range(8)]})
    assert [c.palette for c in got.values()][6:] == ["amber", "green"]


@pytest.mark.parametrize(
    "bad, why",
    [
        ({"companies": [co("ALL")]}, "not ALL"),  # ALL is the app's view of every company
        ({"companies": [co("../X")]}, "upper case"),  # a key must be safe in a URL and a path
        ({"companies": [co("A" * 21)]}, "upper case"),
        ({"companies": [co("ALFA"), co("ALFA")]}, "twice"),
        ({"companies": [co("ALFA", palette="pink")]}, "palette"),
        ({"companies": [co("ALFA", legal_name=" ")]}, "legal_name"),
        ({"companies": [co("ALFA", cui="")]}, "cui"),
        ({"companies": [co("ALFA", cui="RO")]}, "cui"),  # only a prefix: would be "" and match every document
        ({"companies": [co("ALFA", cui="12345678901")]}, "cui"),
    ],
)
def test_config_refuses(bad, why):
    with pytest.raises(ConfigError, match=why):
        companies_from(bad)


def test_cui_keeps_digits_only():
    assert companies_from({"companies": [co("ALFA", cui="RO 1234 5678")]})["ALFA"].cui == "12345678"


@pytest.fixture
def third(monkeypatch):
    """A third company next to the two of the example config."""
    gama = replace(
        companies.all()["STUDIO9"],
        key="GAMA",
        legal_name="GAMA SERVICII SRL",
        cui="66666666",
        accounts=(companies.Account("RO00TEST0000000000000003", "RON", "", pays=True),),
        name="Gama",
        palette="blue",
    )
    set_firms(companies.all() | {"GAMA": gama})
    return gama


def test_state_names_every_company_with_its_palette(ledger, home, monkeypatch, third):
    scenario.build(ledger, home, monkeypatch.setattr)
    s = api.state(ledger, "GAMA")
    assert [c["key"] for c in s["companies"]] == ["NORD", "STUDIO9", "GAMA"]
    assert s["companies"][2] | {"late": 0} == {
        "key": "GAMA",
        "name": "Gama",
        "legal_name": "GAMA SERVICII SRL",
        "palette": "blue",
        "late": 0,
        "logo": None,
    }
    assert s["companies"][0]["palette"] == "amber" and s["companies"][1]["palette"] == "green"
    every = api.state(ledger, "ALL")
    assert [p["co"] for p in every["parts"]] == ["NORD", "STUDIO9", "GAMA"]


def test_a_mail_can_go_to_any_configured_company_and_no_other(ledger, home, monkeypatch, third):
    scenario.build(ledger, home, monkeypatch.setattr)
    monkeypatch.setattr(actions, "_tick", lambda: None)
    monkeypatch.setattr(actions, "log", lambda line: None)
    assert not actions.act("mail-answer", "NORD", {"key": scenario.MAIL_KEY, "who": "NOPE"})["ok"]
    assert ledger.db.execute("SELECT 1 FROM documents WHERE sha=?", (scenario.MAIL_KEY,)).fetchone() is None
    assert api.state(ledger, "GAMA")["to_check"][0]["companies"] == ["NORD", "STUDIO9", "GAMA"]
    r = actions.act("mail-answer", "NORD", {"key": scenario.MAIL_KEY, "who": "GAMA"})
    assert r["ok"] and "firmei Gama" in r["message"]  # the name the app shows, not the key
    assert ledger.db.execute("SELECT company FROM documents WHERE sha=?", (scenario.MAIL_KEY,)).fetchone()[0] == "GAMA"


@pytest.mark.parametrize(
    "form",
    [
        "SRL",
        "S.R.L.",
        "SA",
        "S.A.",
        "PFA",
        "P.F.A.",
        "II",
        "I.I.",
        "IF",
        "I.F.",
        "SNC",
        "S.N.C.",
        "SCS",
        "S.C.S.",
        "RA",
        "R.A.",
    ],
)
@pytest.mark.parametrize("comma", ["", ","])
def test_legal_name_without_romanian_suffix(form, comma):
    firm = companies_from({"companies": [co("ALFA", legal_name=f"Invented Soft{comma} {form}", aliases=[])]})["ALFA"]
    assert firm.named_in("Bill to: Invented Soft")
    assert firm.named_in(f"Bill to: Invented Soft {form}")
    assert not firm.named_in("Bill to: Invented Software")
    assert not firm.named_in("Bill to: Preinvented Soft")
    assert not firm.named_in("Bill to: Invented")
    assert not firm.named_in("Bill to: Invented Soft Studio SA")
    assert not firm.named_in("Bill to: Other Invented Soft")
    other = "SRL" if form.replace(".", "") == "SA" else "SA"
    assert not firm.named_in(f"Bill to: Invented Soft {other}")


@pytest.mark.parametrize("name,other", [("Nord", "Nord Logistic SA"), ("AB", "AB InBev")])
def test_short_legal_core_is_a_whole_party_field(name, other):
    firm = companies_from({"companies": [co("ALFA", legal_name=f"{name} SRL", aliases=[])]})["ALFA"]
    assert firm.named_in(f"Bill to: {name}")
    assert not firm.named_in(f"Bill to: {other}")
    assert not firm.named_in(f"Atelier {name} Studio SA")
    assert not firm.named_in(f"Str. {name} 3")
    assert not firm.named_in(f"Bill to: {name} RO Services SA")
    assert not firm.named_in(f"Bill to: {name} VAT Studio SA")


def test_name_column_uses_original_unicode_text():
    firm = companies_from({"companies": [co("ALFA", legal_name="Invented Soft SRL", aliases=[])]})["ALFA"]
    text = "Straße ﬁﬁﬁ: Invented Soft"
    assert firm.name_at(text) == text.index("Invented Soft")
    assert firm.named_in(f"Bill to: Invented Soft, RO{firm.cui}")


def test_mixed_case_legal_name_is_recognised_on_a_document(monkeypatch):
    """The default name pattern must match the upper-cased text documents are searched in."""
    acme = companies_from({"companies": [co("ACME", legal_name="Acme Services SRL", cui="7654321")]})
    set_firms(acme)
    assert docs.inspect_text("Factura catre Acme Services SRL, total 10 lei").mentions == ["ACME"]


def test_an_empty_config_matches_no_transaction_and_no_invoice(ledger, monkeypatch):
    """No accountant, no owner: nothing may be classified as theirs by an empty string."""
    card = statements.Tx(
        date=None,
        title="Plata la POS non-BT",
        description="NIMBUS HOSTING INC",
        direction="debit",
        amount=Decimal("37.00"),
        currency="RON",
        iban="RO00X",
        counterparty="NIMBUS HOSTING INC",
        counterparty_iban="",
    )
    assert statements.classify_tx(card) == statements.NEEDS_INVOICE
    wire = statements.Tx(
        date=None,
        title="Transfer",
        description="PLATA FACTURA",
        direction="debit",
        amount=Decimal("20.00"),
        currency="RON",
        iban="RO00X",
        counterparty="FOO GMBH",
        counterparty_iban="DE00TEST",
    )
    assert statements.classify_tx(wire) == statements.NEEDS_INVOICE
    # an invoice without a supplier CUI is still open: not taken for the (missing) accountant's
    inv = efactura.Invoice(
        doc_type="invoice",
        number="X1",
        issue_date="2026-10-01",
        due_date="2026-10-15",
        currency="RON",
        total="100.00",
        supplier_name="Furnizor SRL",
        supplier_cui="",
        buyer_cui="11111111",
    )
    assert efactura._initial_status(ledger, "NORD", inv, "sha-x")[0] == efactura.OPEN


@pytest.mark.parametrize("other", ["ALFA2", "ALFA-2026-10"])
def test_a_named_payment_file_belongs_to_its_stored_company(ledger, home, other):
    set_firms(companies_from({"companies": [co("ALFA"), co(other, cui="1234569", accounts=[])]}))
    for company in ("ALFA", other):
        ident = ledger.create_payment_file(company, "Same editable name")
        file = ledger.payment_file(ident)
        assert file["company"] == company
        assert pay.named_path(file).parent == home / company / "payment-files"


def test_a_vat_payers_code_is_found_by_its_cui():
    """A firm's VAT code is RO and its CUI: a document carrying either is the firm's. Any other RO code is someone
    else's, and there is no per-install list of old codes."""
    ev = docs.inspect_text("Factura 12 Cumparator: RO 11111111")
    assert (ev.mentions, ev.tax_ids, ev.other_ro_tax_ids) == (["NORD"], ["11111111"], [])
    ev = docs.inspect_text("Factura 12 Cumparator: RO22222222")
    assert (ev.mentions, ev.other_ro_tax_ids) == ([], ["RO22222222"])
    assert docs.issues_for("NORD", docs.inspect_text("Factura RO11111111"), date(2026, 10, 1), set()) == []


@pytest.mark.parametrize(
    "text,ours",
    [
        ("Factura, cumparator CUI 11111111", True),
        ("Factura, cumparator RO11111111", True),
        ("Factura, cumparator RO 11111111", True),
        ("Factura, cumparator Atelier Nord SRL", True),
        ("Factura, cumparator Atelier Nord SRL, RO22222222", False),  # another code: refused, never guessed as ours
        ("Factura, cumparator RO22222222", False),
    ],
)
def test_ownership_by_cui_or_vat_code(text, ours):
    ev = docs.inspect_text(text, "f.pdf")
    if ours:
        docs.check_ownership("NORD", ev)
    else:
        with pytest.raises(docs.DocRefused):
            docs.check_ownership("NORD", ev)


def test_a_short_cui_is_found_by_its_digits(monkeypatch):
    set_firm("NORD", replace(companies.get("NORD"), cui="12"))
    assert "NORD" in docs.inspect_text("Factura RO12").mentions


def test_a_document_naming_another_firm_is_judged_only_by_the_ownership_rules():
    """No list of other firms: a name on the document is just text, and an RO tax id that is not ours still refuses."""
    ours = docs.inspect_text("Factura catre ATELIER NORD SRL, CUI 11111111, livrare pentru Other Group SRL")
    assert ours.mentions == ["NORD"] and not hasattr(ours, "foreign_entities")
    docs.check_ownership("NORD", ours)
    theirs = docs.inspect_text("Factura catre ATELIER NORD SRL, CUI 11111111, furnizor RO44444444")
    with pytest.raises(docs.DocRefused, match="RO44444444"):
        docs.check_ownership("NORD", theirs)


def test_ownership_refusals_without_the_foreign_list(ledger, home, monkeypatch):
    """What still refuses: our company not on it (unless a supporting reason), another RO tax id (unless the user
    confirms), and a refused document is neither recorded nor copied."""
    nobody = docs.inspect_text("Factura catre Other Group SRL, total 10 lei")
    with pytest.raises(docs.DocRefused, match="not on document"):
        docs.check_ownership("NORD", nobody)
    docs.check_ownership("NORD", nobody, supporting="bilet de avion")
    other_ro = docs.inspect_text("Factura catre ATELIER NORD SRL, CUI 11111111, furnizor RO44444444")
    docs.check_ownership("NORD", other_ro, allow_ro_supplier=True)
    studio = docs.inspect_text("Factura catre STUDIO 9 SRL, CUI 33333333")
    with pytest.raises(docs.DocRefused, match="not on document"):
        docs.check_ownership("NORD", studio)  # another configured firm is not ours either
    pdf = home / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(docs, "pdf_text", lambda path: "Factura catre Other Group SRL, total 10 lei")
    with pytest.raises(docs.DocRefused):
        docs.add_receipt(ledger, "NORD", pdf, doc_date="2026-10-01", home=home)
    assert not ledger.db.execute("SELECT 1 FROM documents").fetchone() and not (home / "NORD").exists()


def test_import_does_not_read_or_create_registry(tmp_path):
    import os
    import subprocess
    import sys

    home = tmp_path / "unopened"
    code = "from fiscalm import companies; assert not hasattr(companies, 'COMPANIES')"
    subprocess.run([sys.executable, "-c", code], env=os.environ | {"FISCALM_HOME": str(home)}, check=True)
    assert not home.exists()
