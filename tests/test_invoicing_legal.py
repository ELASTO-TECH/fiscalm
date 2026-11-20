"""Legal invariants use invented identities and fake BNR publications only."""

import json
import sqlite3
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from test_invoicing import DATA, LINE, PARTY, UPLOAD_DAY, draft, next_number, setup_invoice as setup_invoice

from fiscalm import bnr, integrations, ledger
from fiscalm.invoicing import engine as i, pdf as invoice_pdf, spv as invoice_spv, ubl, workspace as api
from fiscalm.invoicing.labels import NON_EU_SERVICES_REASON
from fiscalm.pdftext import pdf_text

K_LINE = LINE | dict(vat_category="K", vat_rate="0", exemption_reason="VATEX-EU-IC")


def issue(setup_invoice, data=None):
    return i.issue(setup_invoice[0], draft(setup_invoice, data), DATA["issue_date"], company="NORD", version=1)


def pdf(L, iid, path):
    path.write_bytes(invoice_pdf.generate(L, iid, company="NORD"))
    return " ".join(pdf_text(path).split())


def set_snapshot(L, iid, data):
    L.db.execute("UPDATE invoices SET data=? WHERE id=?", (json.dumps(data), iid))


@pytest.mark.parametrize(
    "profile,refusal",
    [
        (dict(cui="99999992", vat_id="RO99999984"), "același CUI"),
        (dict(country="GR", vat_id="GR999999999"), "cod UE"),
        (dict(country="FR", vat_id="FR123"), "nu corespunde țării"),
        (dict(cui="123ABC", vat_id=""), "CUI"),
        (dict(cui="99999991", vat_id=""), "CUI"),
        (dict(cui="00", vat_id=""), "CUI"),
        (dict(cui="99999992", vat_id=""), "plătitor"),
        (dict(cui="  ", vat_id="  "), "identificarea fiscală"),
        (dict(country="EL"), "GR.*GB"),
        (dict(country="XI"), "GR.*GB"),
    ],
)
def test_client_identity_refusals(setup_invoice, profile, refusal):
    L, cid = setup_invoice
    with pytest.raises(i.InvoiceError, match=refusal):
        i.save_client(L, "NORD", PARTY | profile)
    before = api.workspace(L, "NORD")["clients"]  # updating the existing client is refused the same way
    assert not api.act(L, "client-save", "NORD", dict(id=cid, client=api.party_out(PARTY | profile)))["ok"]
    assert api.workspace(L, "NORD")["clients"] == before


@pytest.mark.parametrize(
    "profile,expected",
    [
        (dict(cui=" ro 99999992 ", vat_id=" ro 99999992 "), "RO99999992"),
        (dict(cui="", vat_id="RO99999992"), "RO99999992"),
        (dict(cui="ro99999992", vat_id=""), "RO99999992"),
        (dict(country="DE", cui="99999992", vat_id="RO99999992"), "RO99999992"),
        (dict(country="GR", vat_id="el 999999999"), "EL999999999"),
        (dict(country="DE", vat_id="FRAB999999999"), "FRAB999999999"),
        (dict(country="GB", cui="", vat_id="GBGD001"), "GBGD001"),
        (dict(country="GB", cui="", vat_id="GBHA599"), "GBHA599"),
        (dict(country="1A", cui="", vat_id="", tax_id="LOCAL"), ""),
    ],
)
def test_client_normalizes_before_identity_lookup(setup_invoice, profile, expected):
    L, cid = setup_invoice
    saved = i.save_client(L, "NORD", PARTY | profile)
    if expected.startswith("RO"):
        assert saved == cid  # one Romanian VAT identity is one client, whatever the address country
    client = next(c for c in api.workspace(L, "NORD")["clients"] if c["id"] == saved)
    assert client["vat_id"] == expected


@pytest.mark.parametrize(
    "changes,tax_day",
    [
        (dict(delivery_date="2026-10-28"), "2026-10-28"),
        (dict(delivery_date="2026-11-03"), "2026-10-31"),
        (dict(delivery_date="2026-11-03", advance_payment_date="2026-10-26", advance_invoice=True), "2026-10-26"),
    ],
)
def test_save_selects_chargeability_rate_and_read_does_not_recompute(setup_invoice, monkeypatch, changes, tax_day):
    L, cid = setup_invoice
    called = []

    def rate(_L, currency, day, **kwargs):
        called.append((currency, day))
        return "4.5", "2026-10-23"

    monkeypatch.setattr(bnr, "rate", rate)
    i.save_seller(L, "NORD", PARTY | dict(vat_on_collection=True))
    body = DATA | dict(series="TEST", client_id=cid, currency="EUR") | changes
    result = api.act(L, "draft-save", "NORD", body)
    assert result["ok"], result
    d = api.workspace(L, "NORD")["drafts"][0]
    assert d["chargeability_date"] == tax_day
    assert called == [("EUR", tax_day)]
    assert d["exchange_rate"] == dict(rate="4.5", date="2026-10-23")
    assert d["totals"]["vat_ron"] == "94.50"
    # A draft saved without a tax point: reads keep its saved values; the next save recomputes.
    old = i.invoice(L, result["id"], company="NORD")["data"]
    old.pop("chargeability_date")
    set_snapshot(L, result["id"], old)
    assert api.workspace(L, "NORD")["drafts"][0]["chargeability_date"] is None
    assert len(called) == 1
    assert api.act(L, "draft-save", "NORD", body | dict(id=result["id"], version=1))["ok"]
    assert api.workspace(L, "NORD")["drafts"][0]["chargeability_date"] == tax_day
    assert len(called) == 2


def test_rate_cannot_postdate_tax_point(setup_invoice):
    with pytest.raises(i.InvoiceError, match="exigibilității"):
        draft(
            setup_invoice,
            DATA | dict(currency="EUR", exchange_rate="5", exchange_date="2026-10-28", delivery_date="2026-10-28"),
        )
    with pytest.raises(i.InvoiceError, match="avansului"):
        draft(setup_invoice, DATA | dict(advance_payment_date="2026-11-01"))
    with pytest.raises(i.InvoiceError, match="înaintea datei livrării"):  # the same day is not before
        draft(
            setup_invoice,
            DATA | dict(advance_invoice=True, advance_payment_date="2026-10-28", delivery_date="2026-10-28"),
        )


def test_collection_flag_and_mentions_are_frozen(setup_invoice):
    L, _ = setup_invoice
    assert api.workspace(L, "NORD")["seller"]["vat_on_collection"] is False
    for flag in ("true", 1, None):
        with pytest.raises(i.InvoiceError):
            i.save_seller(L, "NORD", PARTY | dict(vat_on_collection=flag))
    i.save_seller(L, "NORD", PARTY | dict(vat_payer=False, vat_on_collection=True))
    assert api.workspace(L, "NORD")["seller"]["vat_on_collection"] is False
    i.save_seller(L, "NORD", PARTY | dict(vat_on_collection=True))
    iid = draft(setup_invoice, DATA | dict(notes="O notă separată."))
    assert api.workspace(L, "NORD")["drafts"][0]["mentions"] == ["TVA la încasare"]
    inv = i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    i.save_seller(L, "NORD", PARTY)
    assert i.invoice(L, iid, company="NORD")["data"]["mentions"] == ["TVA la încasare"]
    root = ET.fromstring(ubl.build(inv))
    assert [e.text for e in root.findall("cbc:Note", ubl.NS)] == ["TVA la încasare", "O notă separată."]
    credit = i.storno(L, iid, DATA["issue_date"], "Corecție", company="NORD")
    assert credit["data"]["mentions"] == ["TVA la încasare"]


def test_vat_choices_and_service_refusals(setup_invoice):
    L, cid = setup_invoice
    choices = {c["key"]: c for c in api.workspace(L, "NORD")["vat"]["choices"]}
    assert "O310" not in choices
    assert choices["G"]["label"] == "Export de bunuri"
    assert choices["O278"]["category"] == "O" and "278" in choices["O278"]["exemption"]
    with pytest.raises(i.InvoiceError, match="310"):
        i.calculate([LINE | dict(vat_category="O", vat_rate="0", exemption_reason="art. 310")], True)
    # Art. 278(2) needs a taxable person outside the EU: not Romanian, EU, private, or EU-registered.
    for country, private, vat_id in [
        ("RO", False, "RO99999984"),
        ("DE", False, "DE999999999"),
        ("GB", True, "GB999999999"),
        ("US", False, "DE999999999"),
    ]:
        i.save_client(L, "NORD", PARTY | dict(country=country, private=private, vat_id=vat_id), cid)
        with pytest.raises(i.InvoiceError, match="278"):
            draft(
                setup_invoice,
                DATA
                | dict(lines=[LINE | dict(vat_category="O", vat_rate="0", exemption_reason=NON_EU_SERVICES_REASON)]),
            )


@pytest.mark.parametrize(
    "price,ron",
    [("0.03", dict(net="0.14", vat="0.05", total="0.19")), ("100", dict(net="450.00", vat="94.50", total="544.50"))],
)
def test_ron_vat_matches_xml_and_existing_pdf_conversion(setup_invoice, tmp_path, price, ron):
    L, _ = setup_invoice
    iid = draft(
        setup_invoice,
        DATA
        | dict(currency="EUR", exchange_rate="4.5", exchange_date="2026-10-30", lines=[LINE | dict(unit_price=price)]),
    )
    with L.tx() as db:
        db.execute("INSERT INTO invoice_rates VALUES(?,?,?,?)", ("EUR", DATA["issue_date"], "4.5", "2026-10-30"))
    inv = i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    # RON VAT converts the frozen VAT; the RON total adds the rounded components.
    assert i.ron_totals(inv["data"]) == ron
    assert ron["vat"] == i.money(Decimal(inv["data"]["totals"]["vat"]) * Decimal("4.5"))
    root = ET.fromstring(ubl.build(inv))
    assert root.findall("cac:TaxTotal/cbc:TaxAmount", ubl.NS)[1].text == ron["vat"]
    assert ron["vat"].replace(".", ",") + " lei" in pdf(L, iid, tmp_path / "invoice.pdf")
    credit = i.storno(L, iid, DATA["issue_date"], "Corecție", company="NORD")
    assert Decimal(i.ron_totals(credit["data"])["vat"]) == -Decimal(ron["vat"])


def test_intracommunity_tax_point_is_not_domestic_delivery_date():
    data = DATA | dict(delivery_date="2026-09-02", lines=[K_LINE])
    assert i.chargeability_date(data) == "2026-10-15"
    assert i.chargeability_date(data | dict(issue_date="2026-09-04")) == "2026-09-04"
    with pytest.raises(i.InvoiceError, match="avansului"):
        i.chargeability_date(data | dict(advance_payment_date="2026-09-01"))
    with pytest.raises(i.InvoiceError, match="facturi separate"):  # mixed tax points
        i.chargeability_date(data | dict(lines=[LINE, K_LINE]))


def test_api_caches_bnr_selection_for_delivery_day(setup_invoice, monkeypatch):
    L, cid = setup_invoice
    real_rate = bnr.rate

    def selected(ledger, currency, day, **kwargs):
        feed = b'<DataSet><Body><Cube date="2026-10-27"><Rate currency="EUR">4.5</Rate></Cube><Cube date="2026-10-28"><Rate currency="EUR">5</Rate></Cube></Body></DataSet>'
        return real_rate(ledger, currency, day, fetch=lambda _: feed, **kwargs)

    monkeypatch.setattr(bnr, "rate", selected)
    result = api.act(
        L, "draft-save", "NORD", DATA | dict(series="TEST", client_id=cid, currency="EUR", delivery_date="2026-10-28")
    )
    assert result["ok"], result
    rate = L.db.execute("SELECT * FROM invoice_rates").fetchone()
    assert (rate["invoice_date"], rate["published"], rate["rate"]) == ("2026-10-28", "2026-10-27", "4.5")
    assert api.workspace(L, "NORD")["drafts"][0]["exchange_rate"] == dict(rate="4.5", date="2026-10-27")


@pytest.mark.parametrize("category", ["S", "K"])
def test_collection_mention_by_treatment_in_xml_and_pdf(setup_invoice, tmp_path, category):
    L, cid = setup_invoice
    i.save_seller(L, "NORD", PARTY | dict(vat_on_collection=True))
    if category == "K":
        i.save_client(L, "NORD", PARTY | dict(country="DE", vat_id="DE999999999", tax_id="SYNTHETIC"), cid)
    choice = next(c for c in api.workspace(L, "NORD")["vat"]["choices"] if c["category"] == category)
    inv = issue(
        setup_invoice,
        DATA
        | dict(
            delivery_date="2026-10-28",
            lines=[LINE | dict(vat_category=category, vat_rate=choice["rate"], exemption_reason=choice["exemption"])],
        ),
    )
    assert inv["data"]["mentions"] == (["TVA la încasare"] if category == "S" else [])
    assert ("TVA la încasare" in ubl.build(inv).decode()) == (category == "S")
    assert pdf(L, inv["id"], tmp_path / "synthetic.pdf").count("TVA la încasare") == (1 if category == "S" else 0)


def test_collection_mixed_treatments_refused(setup_invoice):
    i.save_seller(setup_invoice[0], "NORD", PARTY | dict(vat_on_collection=True))
    with pytest.raises(i.InvoiceError, match="facturi separate"):
        draft(
            setup_invoice,
            DATA | dict(lines=[LINE, LINE | dict(vat_category="E", vat_rate="0", exemption_reason="art. 292")]),
        )


def test_advance_is_explicit_and_printed(setup_invoice, tmp_path):
    data = DATA | dict(advance_payment_date="2026-10-25")
    with pytest.raises(i.InvoiceError, match="factură de avans"):
        draft(setup_invoice, data)
    L, _ = setup_invoice
    inv = issue(setup_invoice, data | dict(advance_invoice=True))
    root = ET.fromstring(ubl.build(inv))
    assert root.findtext("cbc:TaxPointDate", namespaces=ubl.NS) == "2026-10-25"
    assert root.find("cac:InvoicePeriod/cbc:DescriptionCode", ubl.NS) is None
    # BT-7 reads the frozen tax point rather than re-deriving it from the receipt.
    changed = inv | dict(data=inv["data"] | dict(advance_payment_date="2026-11-02"))
    assert ET.fromstring(ubl.build(changed)).findtext("cbc:TaxPointDate", namespaces=ubl.NS) == "2026-10-25"
    text = pdf(L, inv["id"], tmp_path / "advance.pdf")
    assert "DATA ÎNCASĂRII AVANSULUI" in text and "25.10.2026" in text
    credit = i.storno(L, inv["id"], DATA["issue_date"], "Corecție", company="NORD")
    assert "(FACTURA INIȚIALĂ)" in pdf(L, credit["id"], tmp_path / "advance.pdf")


def test_advance_deadline_warning(setup_invoice):
    L, _ = setup_invoice
    iid = draft(setup_invoice, DATA | dict(advance_invoice=True, advance_payment_date="2026-09-01"))
    assert api.workspace(L, "NORD")["drafts"][0]["warnings"] == [
        "Factura de avans este emisă după termenul de la art. 319 alin. (16)."
    ]
    assert i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)["data"]["warnings"]


@pytest.mark.parametrize(
    "client,accepted",
    [
        (dict(country="GB", vat_id="GB999999999"), False),
        (dict(country="GB", vat_id="XI999999999"), True),
        (dict(country="DE"), False),
    ],
)  # a Romanian VAT id at a foreign address
def test_k_needs_an_eu_vat_registration(setup_invoice, client, accepted):
    L, cid = setup_invoice
    i.save_client(L, "NORD", PARTY | client, cid)
    data = DATA | dict(delivery_date="2026-10-24", lines=[K_LINE])
    if accepted:
        assert draft(setup_invoice, data)
    else:
        with pytest.raises(i.InvoiceError, match="UE"):
            draft(setup_invoice, data)


def test_issue_rechecks_and_freezes_the_client_snapshot(setup_invoice):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    data = i.invoice(L, iid, company="NORD")["data"]
    set_snapshot(L, iid, data | dict(client=data["client"] | dict(vat_id="RO99999984")))
    with pytest.raises(i.InvoiceError, match="același CUI"):
        i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert next_number(L) == 42
    set_snapshot(L, iid, data | dict(client=data["client"] | dict(vat_id="ro99999992")))
    issued = i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert issued["data"]["client"]["vat_id"] == "RO99999992"
    assert "RO99999992" in ubl.build(issued).decode()


def test_non_eu_tax_identity_separate_from_vat(setup_invoice):
    L, cid = setup_invoice
    i.save_client(L, "NORD", PARTY | dict(country="CH", cui="", vat_id="", tax_id="LOCAL-123"), cid)
    client = next(c for c in api.workspace(L, "NORD")["clients"] if c["id"] == cid)
    assert client["tax_id"] == "LOCAL-123" and client["vat_id"] == ""
    with pytest.raises(i.InvoiceError, match="tax_id"):
        i.save_client(L, "NORD", PARTY | dict(country="CH", vat_id="CH123"), cid)


def test_local_tax_ids_never_merge_clients(setup_invoice):
    L, _ = setup_invoice
    us = i.save_client(L, "NORD", PARTY | dict(country="US", cui="", vat_id="", tax_id="  Local 123  "))
    ch = i.save_client(L, "NORD", PARTY | dict(country="CH", cui="", vat_id="", tax_id="Local 123"))
    again = i.save_client(L, "NORD", PARTY | dict(country="US", cui="", vat_id="", tax_id="Local 123"))
    assert len({us, ch, again}) == 3
    clients = {c["id"]: c for c in api.workspace(L, "NORD")["clients"]}
    assert clients[us]["tax_id"] == "Local 123"


def test_o_only_uses_fiscal_not_vat_scheme_for_registered_seller(setup_invoice):
    L, cid = setup_invoice
    i.save_client(L, "NORD", PARTY | dict(country="US", cui="", vat_id="", tax_id="LOCAL-123"), cid)
    inv = issue(
        setup_invoice,
        DATA | dict(lines=[LINE | dict(vat_category="O", vat_rate="0", exemption_reason=NON_EU_SERVICES_REASON)]),
    )
    root = ET.fromstring(ubl.build(inv))
    schemes = root.findall("cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme", ubl.NS)
    assert [
        (t.findtext("cac:TaxScheme/cbc:ID", namespaces=ubl.NS), t.findtext("cbc:CompanyID", namespaces=ubl.NS))
        for t in schemes
    ] == [("FC", PARTY["cui"])]
    assert (
        root.findtext("cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:CompanyID", namespaces=ubl.NS)
        == "LOCAL-123"
    )


def test_pdf_mentions_deduplicate_saved_and_derived(setup_invoice, tmp_path, monkeypatch):
    L, _ = setup_invoice
    inv = issue(
        setup_invoice,
        DATA | dict(lines=[LINE | dict(vat_category="E", vat_rate="0", exemption_reason="Scutit conform art. 292")]),
    )
    original = invoice_pdf.invoice

    def snapshot(*args, **kwargs):
        inv = original(*args, **kwargs)
        inv["data"]["mentions"] = ["Scutit conform art. 292", "Mențiune suplimentară"] * 2
        return inv

    monkeypatch.setattr(invoice_pdf, "invoice", snapshot)
    text = pdf(L, inv["id"], tmp_path / "mentions.pdf")
    assert text.count("Scutit conform art. 292") == text.count("Mențiune suplimentară") == 1


@pytest.mark.parametrize("editing", [False, True])
def test_legacy_identity_collision_is_refused_without_merging(setup_invoice, editing):
    L, cid = setup_invoice
    raw = L.db.execute("SELECT data FROM invoice_clients WHERE id=?", (cid,)).fetchone()[0]
    L.db.execute("INSERT INTO invoice_clients VALUES(?,?,?,?)", ("legacy", "NORD", "RO99999992", raw))
    before = list(map(tuple, L.db.execute("SELECT * FROM invoice_clients ORDER BY id")))
    result = api.act(
        L, "client-save", "NORD", dict(client=api.party_out(json.loads(raw))) | (dict(id=cid) if editing else {})
    )
    assert not result["ok"] and "aceeași identitate fiscală" in result["message"]
    assert list(map(tuple, L.db.execute("SELECT * FROM invoice_clients ORDER BY id"))) == before


def test_api_integrity_error_is_a_refusal(setup_invoice, monkeypatch):
    def conflict(*args):
        raise sqlite3.IntegrityError("private database detail")

    monkeypatch.setattr(i, "save_client", conflict)
    result = api.act(setup_invoice[0], "client-save", "NORD", dict(client=api.party_out(PARTY)))
    assert not result["ok"] and "conflict" in result["message"]
    assert "private" not in result["message"]


def test_tax_only_business_scope_and_manual_b2b_transport(setup_invoice, monkeypatch):
    L, cid = setup_invoice
    integrations.add(L, "anaf", "Synthetic ANAF", {"client_id": "fake", "client_secret": "fake"})
    i.save_client(
        L,
        "NORD",
        PARTY | dict(country="US", cui="", vat_id="", tax_id="LOCAL 123", ro_vat_status="not_registered"),
        cid,
    )
    iid = issue(setup_invoice)["id"]
    monkeypatch.setattr(ledger, "is_live", lambda: True)
    calls = []

    def transport(method, url, body):
        calls.append((method, url))
        return b'<header index_incarcare="12345"/>'

    assert invoice_spv.auto_upload(L, iid, transport, company="NORD", today=UPLOAD_DAY)["status"] == "not_required"
    with pytest.raises(i.InvoiceError, match="nu se transmite"):
        invoice_spv.upload(L, iid, transport, company="NORD")
    assert calls == []
    # Only an explicitly registered foreign business may manually opt in (#83).
    data = i.invoice(L, iid, company="NORD")["data"]
    set_snapshot(L, iid, data | dict(client=data["client"] | dict(efactura_registered=True)))
    assert invoice_spv.auto_upload(L, iid, transport, company="NORD", today=UPLOAD_DAY)["status"] == "not_required"
    assert invoice_spv.upload(L, iid, transport, company="NORD")["status"] == "uploaded"
    assert "/upload?" in calls[0][1] and "extern=DA" in calls[0][1]


def test_issue_refuses_changed_saved_client_until_draft_resaved(setup_invoice):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    i.save_client(L, "NORD", dict(name="Client corectat fictiv"), cid)
    with pytest.raises(i.InvoiceError, match="Salvează din nou ciorna"):
        i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert i.invoice(L, iid, company="NORD")["number"] is None
    i.save_draft(L, "NORD", "TEST", cid, DATA, iid, version=1)
    assert (
        i.issue(L, iid, DATA["issue_date"], company="NORD", version=2)["data"]["client"]["name"]
        == "Client corectat fictiv"
    )


@pytest.mark.parametrize("payer", [True, False])
def test_every_offered_vat_choice_can_be_issued(setup_invoice, payer):
    L, cid = setup_invoice
    i.save_seller(L, "NORD", PARTY | dict(vat_payer=payer, vat_id=PARTY["vat_id"] if payer else ""))
    workspace = api.workspace(L, "NORD", DATA["issue_date"])
    choices = workspace["vat"]["choices"]
    assert workspace["vat"]["default"] in {c["key"] for c in choices}
    if payer:
        assert {c["category"] for c in choices} == {"S", "Z", "E", "AE", "K", "G", "O"}
    else:
        assert len(choices) == 1 and choices[0]["category"] == "O" and "310" in choices[0]["exemption"]
    for choice in choices:
        country = ("GB", "GB999999999") if choice["key"] == "O278" else ("DE", "DE999999999")
        i.save_client(L, "NORD", PARTY | dict(country=country[0], vat_id=country[1]), cid)
        data = DATA | dict(
            lines=[
                LINE
                | dict(vat_category=choice["category"], vat_rate=choice["rate"], exemption_reason=choice["exemption"])
            ]
        )
        if choice["needs_delivery_date"]:
            with pytest.raises(i.InvoiceError, match="data livrării"):
                i.save_draft(L, "NORD", "TEST", cid, data)
            data["delivery_date"] = DATA["issue_date"]
        i.issue(L, i.save_draft(L, "NORD", "TEST", cid, data), DATA["issue_date"], company="NORD", version=1)
