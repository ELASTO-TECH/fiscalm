from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from test_invoicing import (
    DATA,
    LINE,
    PARTY,
    draft_with_confirmed_rate as draft,
    next_number,
    setup_invoice as setup_invoice,
)

from fiscalm.efactura.inbox import NS, parse_ubl
from fiscalm.invoicing import engine as i, ubl
from fiscalm.invoicing.workspace import workspace

EUR = DATA | dict(currency="EUR", exchange_rate="5", exchange_date="2026-10-30")
BUYER_ADDRESS = "cac:AccountingCustomerParty/cac:Party/cac:PostalAddress/cbc:"


def issued_xml(setup_invoice, data=None):
    L, _ = setup_invoice
    iid = draft(setup_invoice, data)
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    return ubl.generate(L, iid, company="NORD")


def codes(root):
    return {e.split(":")[0] for e in ubl.validate(ET.tostring(root))}


def test_roundtrip(setup_invoice):
    raw = issued_xml(setup_invoice)
    assert ubl.validate(raw) == []
    inv = parse_ubl(raw)
    assert (inv.number, inv.total, inv.supplier_name, inv.buyer_name) == (
        "TEST-42",
        "121.00",
        PARTY["name"],
        "Client Fictiv SRL",
    )


def test_foreign_currency(setup_invoice):
    raw = issued_xml(setup_invoice, EUR)
    root = ET.fromstring(raw)
    assert root.findall("cac:TaxTotal", NS)[1].find("cbc:TaxAmount", NS).text == "105.00"
    assert ubl.validate(raw) == []
    root.remove(root.find("cbc:TaxCurrencyCode", NS))
    assert "BR-RO-030" in codes(root)


@pytest.mark.parametrize(
    "path,value,code",
    [
        ("cbc:CustomizationID", "bad", "BR-RO-001"),
        ("cbc:ID", "ABC", "BR-RO-010"),
        ("cbc:InvoiceTypeCode", "381", "BR-RO-020"),
        ("cbc:IssueDate", "bad", "DATE"),
        ("cbc:DocumentCurrencyCode", "RO", "CURRENCY"),
        ("cac:LegalMonetaryTotal/cbc:LineExtensionAmount", "99", "BR-CO-10"),
        ("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", "99", "BR-CO-13"),
        ("cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "99", "BR-CO-15"),
        ("cac:LegalMonetaryTotal/cbc:PayableAmount", "99", "BR-CO-16"),
        ("cac:TaxTotal/cbc:TaxAmount", "99", "BR-CO-14"),
        ("cac:TaxTotal/cac:TaxSubtotal/cbc:TaxableAmount", "99", "BR-S-08"),
        ("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:Percent", "-1", "BR-S"),
        ("cac:InvoiceLine/cac:Item/cbc:Name", "", "MANDATORY"),
        ("cac:InvoiceLine/cbc:InvoicedQuantity", "2", "LINE-NET"),
        (BUYER_ADDRESS + "StreetName", "", "BR-RO-080"),
        (BUYER_ADDRESS + "CityName", "", "BR-RO-090"),
        (BUYER_ADDRESS + "CountrySubentity", "RO-XX", "BR-RO-110"),
        (BUYER_ADDRESS + "PostalZone", "x" * 21, "BR-RO-L020"),
    ],
)
def test_broken_rule(setup_invoice, path, value, code):
    root = ET.fromstring(issued_xml(setup_invoice))
    root.find(path, NS).text = value
    assert code in codes(root)


def test_bucharest_draft_private_and_malformed(setup_invoice):
    L, cid = setup_invoice
    with pytest.raises(i.InvoiceError):
        ubl.generate(L, draft(setup_invoice), company="NORD")
    with pytest.raises(i.InvoiceError):
        i.save_seller(L, "NORD", PARTY | dict(city="București", county="RO-B"))


def test_validation_explains_and_refuses_dtds():
    errors = ubl.validate(b"<Invoice/>")
    assert errors[0] == "XML: Documentul nu este o factură UBL."
    assert "BR-RO-001: Formatul trebuie să fie CIUS-RO 1.0.1." in errors
    assert (
        ubl.validate(b"<!DOCTYPE Invoice><Invoice/>")
        == ubl.validate(b"<!DOCTYPE x><x/>")
        == ["XML: Documentul XML nu poate fi citit: formatul trebuie verificat, iar orice DTD eliminat."]
    )


def test_missing_and_unsupported(setup_invoice):
    root = ET.fromstring(issued_xml(setup_invoice))
    monetary = root.find("cac:LegalMonetaryTotal", NS)
    ET.SubElement(monetary, "{" + NS["cbc"] + "}AllowanceTotalAmount").text = "1"
    ET.SubElement(monetary, "{" + NS["cbc"] + "}ChargeTotalAmount").text = "1"
    ET.SubElement(root, "{" + NS["cac"] + "}AllowanceCharge")
    assert {"BR-CO-11", "BR-CO-12", "UNSUPPORTED"} <= codes(root)
    # Refusals name the form's fields, never the XML's.
    root.remove(root.find("cac:InvoiceLine", NS))
    root.remove(root.find("cbc:IssueDate", NS))
    root.find(BUYER_ADDRESS + "StreetName", NS).text = "x" * 151
    errors = ubl.validate(ET.tostring(root))
    assert "MANDATORY: Lipsesc liniile facturii. Factura trebuie completată." in errors
    assert "MANDATORY: Lipsește data emiterii. Factura trebuie completată." in errors
    assert "BR-RO-L150: Strada clientului poate avea cel mult 150 de caractere." in errors
    assert all("cbc:" not in error and "StreetName" not in error for error in errors)


@pytest.mark.parametrize("category", ["Z", "E", "O"])
def test_bad_zero_category_rules(setup_invoice, category):
    if category == "O":
        i.save_seller(setup_invoice[0], "NORD", PARTY | dict(vat_payer=False, vat_id=""))
    root = ET.fromstring(
        issued_xml(
            setup_invoice,
            DATA | dict(lines=[LINE | dict(vat_category=category, vat_rate="0", exemption_reason="art. 310")]),
        )
    )
    sub = root.find("cac:TaxTotal/cac:TaxSubtotal", NS)
    sub.find("cbc:TaxableAmount", NS).text = "99"
    cat = sub.find("cac:TaxCategory", NS)
    if category != "O":
        cat.find("cbc:Percent", NS).text = "21"
    if category != "Z":
        cat.remove(cat.find("cbc:TaxExemptionReason", NS))
    else:
        ET.SubElement(cat, "{" + NS["cbc"] + "}TaxExemptionReason").text = "bad"
    assert {f"BR-{category}-08", f"BR-{category}-REASON"} <= codes(root)


def test_vat_identity_groups_lengths_and_currency(setup_invoice):
    root = ET.fromstring(issued_xml(setup_invoice))
    party = root.find("cac:AccountingSupplierParty/cac:Party", NS)
    for node in party.findall("cac:PartyTaxScheme", NS):
        party.remove(node)
    party.find("cac:PartyLegalEntity/cbc:RegistrationName", NS).text = "x" * 201
    party.find("cac:PartyLegalEntity/cbc:CompanyID", NS).text = ""
    root.find("cac:LegalMonetaryTotal/cbc:PayableAmount", NS).set("currencyID", "EUR")
    assert {"BR-S-02", "BR-RO-L200", "BR-RO-065", "CURRENCY"} <= codes(root)
    root.find("cac:TaxTotal", NS).remove(root.find("cac:TaxTotal/cac:TaxSubtotal", NS))
    assert "VAT-GROUP" in codes(root)


def test_export_failure_does_not_take_number(setup_invoice, monkeypatch):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    monkeypatch.setattr(ubl, "validate", lambda _: ["refused by validator"])
    with pytest.raises(i.InvoiceError, match="refused by validator"):
        i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert next_number(L) == 42
    assert i.invoice(L, iid, company="NORD")["status"] == "draft"


def test_numeric_rates_price_and_account(setup_invoice):
    root = ET.fromstring(issued_xml(setup_invoice, DATA | dict(lines=[LINE, LINE | dict(vat_rate="21.00")])))
    root.find("cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", NS).text = "21.00"
    assert codes(root) == set()
    assert root.find("cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:Name", NS).text == PARTY["name"]
    root.find("cac:InvoiceLine/cac:Price/cbc:PriceAmount", NS).text = "-100"
    assert "BR-27" in codes(root)


def test_missing_tax_amount_returns_error(setup_invoice):
    root = ET.fromstring(issued_xml(setup_invoice))
    tax = root.find("cac:TaxTotal", NS)
    tax.remove(tax.find("cbc:TaxAmount", NS))
    assert ubl.validate(ET.tostring(root))


def test_monetary_scale_and_buyer_identity_fields(setup_invoice):
    root = ET.fromstring(issued_xml(setup_invoice))
    root.find("cac:LegalMonetaryTotal/cbc:PayableAmount", NS).text = "121.000"
    buyer = root.find("cac:AccountingCustomerParty/cac:Party", NS)
    buyer.find("cac:PartyLegalEntity/cbc:CompanyID", NS).text = ""
    buyer.find("cac:PartyTaxScheme/cac:TaxScheme/cbc:ID", NS).text = "FC"
    assert {"BR-RO-Z2", "BR-RO-065/120"} <= codes(root)


def test_account_name_has_own_limit(setup_invoice):
    L, _ = setup_invoice
    i.save_seller(L, "NORD", PARTY | dict(account_name="A" * 200))
    assert ubl.validate(issued_xml(setup_invoice)) == []


@pytest.mark.parametrize("delta,allowed", [("0.99", True), ("1.00", False)])
def test_cen_category_vat_tolerance(setup_invoice, delta, allowed):
    root = ET.fromstring(issued_xml(setup_invoice))
    root.find("cac:TaxTotal/cac:TaxSubtotal/cbc:TaxAmount", NS).text = str(Decimal("21") + Decimal(delta))
    assert ("BR-CO-17" not in codes(root)) == allowed


def test_br_ro_211_foreign_delivery_requires_subdivision(setup_invoice):
    L, cid = setup_invoice
    k = DATA | dict(
        delivery_date="2026-10-29", lines=[LINE | dict(vat_category="K", vat_rate="0", exemption_reason="VATEX-EU-IC")]
    )
    i.save_client(L, "NORD", PARTY | dict(country="DE", county="", vat_id="DE999999999"), cid)
    with pytest.raises(i.InvoiceError, match="BR-RO-211"):
        issued_xml(setup_invoice, k)
    assert next_number(L) == 42
    i.save_client(L, "NORD", PARTY | dict(country="DE", county="DE-BE", vat_id="DE999999999"), cid)
    root = ET.fromstring(issued_xml(setup_invoice, k))
    addr = root.find("cac:Delivery/cac:DeliveryLocation/cac:Address", NS)
    addr.remove(addr.find("cbc:CountrySubentity", NS))
    assert "BR-RO-211" in codes(root)


def test_delivery_date_preserved_for_standard_invoice(setup_invoice):
    delivery = ET.fromstring(issued_xml(setup_invoice, DATA | dict(delivery_date="2026-10-29"))).find(
        "cac:Delivery", NS
    )
    assert delivery.findtext("cbc:ActualDeliveryDate", namespaces=NS) == "2026-10-29"
    assert len(delivery) == 1  # no delivery location for a domestic S invoice


def test_line_name_boundary_shared_by_draft_xml_and_workspace(setup_invoice):
    limit = i.LINE_DESCRIPTION_MAX
    assert workspace(setup_invoice[0], "NORD")["line_description_max"] == limit
    raw = issued_xml(setup_invoice, DATA | dict(lines=[LINE | dict(description="x" * limit)]))
    assert ubl.validate(raw) == []
    root = ET.fromstring(raw)
    item = root.find("cac:InvoiceLine/cac:Item", NS)
    assert item.find("cbc:Description", NS) is None
    assert item.find("cbc:Name", NS).text == "x" * limit
    item.find("cbc:Name", NS).text += "x"
    assert ubl.validate(ET.tostring(root)) == ["BR-RO-L1024: " + i.LINE_DESCRIPTION_ERROR]
    with pytest.raises(i.InvoiceError) as error:
        draft(setup_invoice, DATA | dict(lines=[LINE | dict(description="x" * (limit + 1))]))
    assert str(error.value) == i.LINE_DESCRIPTION_ERROR


def test_distinct_item_description_boundary(setup_invoice):
    root = ET.fromstring(issued_xml(setup_invoice))
    description = ET.SubElement(root.find("cac:InvoiceLine/cac:Item", NS), "{" + NS["cbc"] + "}Description")
    description.text = "x" * 200
    assert ubl.validate(ET.tostring(root)) == []
    description.text += "x"
    assert ubl.validate(ET.tostring(root)) == ["BR-RO-LENGTH: Descrierea poate avea cel mult 200 de caractere."]


@pytest.mark.parametrize(
    "profile,legal,vat",
    [
        (dict(country="DE", cui="DE999999999", vat_id="DE999999999"), "DE999999999", "DE999999999"),
        (dict(country="US", cui="US-TEST-999", vat_id=""), "US-TEST-999", None),
        (dict(country="DE", cui="", vat_id="", private=True), "0000000000000", None),
    ],
)
def test_foreign_buyer_identity(setup_invoice, profile, legal, vat):
    """Foreign identities survive export even when ANAF cannot route them domestically."""
    i.save_client(setup_invoice[0], "NORD", PARTY | profile | dict(county=""), setup_invoice[1])
    raw = issued_xml(setup_invoice)
    party = ET.fromstring(raw).find("cac:AccountingCustomerParty/cac:Party", NS)
    assert party.findtext("cac:PartyLegalEntity/cbc:CompanyID", namespaces=NS) == legal
    assert party.findtext("cac:PartyTaxScheme/cbc:CompanyID", namespaces=NS) == vat
    assert party.findtext("cac:PartyTaxScheme/cac:TaxScheme/cbc:ID", namespaces=NS) == ("VAT" if vat else None)
    assert party.findtext("cac:PostalAddress/cac:Country/cbc:IdentificationCode", namespaces=NS) == profile["country"]
    assert party.find("cac:PostalAddress/cbc:CountrySubentity", NS) is None
    assert party.find("cac:PartyIdentification", NS) is None
    assert party.find("cac:PartyLegalEntity/cbc:CompanyID", NS).attrib == {}
    assert ubl.validate(raw) == []
