"""Small UBL 2.1 Invoice generator and local checks for its supported CIUS-RO subset.

Sources: OMF 4092/2022 (CustomizationID 1.0.1, BR-RO rules):
https://static.anaf.ro/static/10/Anaf/legislatie/OMF_4092_2022.pdf
https://static.anaf.ro/static/10/Anaf/Informatii_R/Informatii_modificare_CIUS_RO.pdf
Official validator package (not vendored):
https://mfinante.gov.ro/static/10/eFactura/ro16931-ubl-1.0.9.zip
EN16931 rules: https://github.com/ConnectingEurope/eInvoicing-EN16931

Local validation is deliberately a subset, not an ANAF acceptance guarantee. Full official
Schematron execution is available via `uv run python tools/validate_cius_ro.py`; see
docs/efactura-anaf.md for pinned artifacts and provenance limits.
No allowances, charges or prepayments are generated. O cannot mix with
other categories. K requires an explicit delivery date. Explicit private clients use the anonymous B2C identity.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal, DecimalException
from xml.etree import ElementTree as ET

from fiscalm.efactura.inbox import NS, UblError, safe_xml
from fiscalm.invoicing.engine import (
    COUNTIES,
    LINE_DESCRIPTION_ERROR,
    LINE_DESCRIPTION_MAX,
    InvoiceError,
    _day,
    decimal,
    invoice,
    invoice_number,
    money,
    ron_totals,
)
from fiscalm.ledger import Ledger

CUSTOMIZATION = "urn:cen.eu:en16931:2017#compliant#urn:efactura.mfinante.ro:CIUS-RO:1.0.1"
ROOT = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)
ET.register_namespace("", ROOT)


def _add(parent: ET.Element, name: str, value: str | None = None, **attrs: str) -> ET.Element:
    prefix, local = name.split(":")
    child = ET.SubElement(parent, "{" + NS[prefix] + "}" + local, attrs)
    child.text = value
    return child


def _party(root: ET.Element, role: str, profile: dict, seller: bool, no_vat: bool) -> None:
    party = _add(_add(root, "cac:" + role), "cac:Party")
    address = _add(party, "cac:PostalAddress")
    for key, tag in [
        ("street", "StreetName"),
        ("city", "CityName"),
        ("postal_code", "PostalZone"),
        ("county", "CountrySubentity"),
    ]:
        if profile.get(key):
            _add(address, "cbc:" + tag, profile[key])
    _add(_add(address, "cac:Country"), "cbc:IdentificationCode", profile["country"])
    if profile.get("vat_id") and not no_vat:
        tax = _add(party, "cac:PartyTaxScheme")
        _add(tax, "cbc:CompanyID", profile["vat_id"])
        _add(_add(tax, "cac:TaxScheme"), "cbc:ID", "VAT")
    if seller:
        tax = _add(party, "cac:PartyTaxScheme")
        _add(tax, "cbc:CompanyID", profile["cui"])
        _add(_add(tax, "cac:TaxScheme"), "cbc:ID", "FC")
    legal = _add(party, "cac:PartyLegalEntity")
    _add(legal, "cbc:RegistrationName", profile["name"])
    identity = (
        profile.get("cui") or profile.get("vat_id") or profile.get("tax_id") or ("" if seller else "0000000000000")
    )
    if identity:
        _add(legal, "cbc:CompanyID", identity)
    if seller and profile.get("reg_com"):
        _add(legal, "cbc:CompanyLegalForm", profile["reg_com"])
    if profile.get("email") or profile.get("phone"):
        contact = _add(party, "cac:Contact")
        for key, tag in [("phone", "Telephone"), ("email", "ElectronicMail")]:
            if profile.get(key):
                _add(contact, "cbc:" + tag, profile[key])


def _category(parent: ET.Element, tag: str, category: str, rate: str, reason: str = "") -> None:
    node = _add(parent, "cac:" + tag)
    _add(node, "cbc:ID", category)
    if category != "O":
        _add(node, "cbc:Percent", rate)
    if reason and category not in {"S", "Z"}:
        _add(
            node,
            "cbc:TaxExemptionReasonCode" if re.fullmatch(r"VATEX-[A-Z0-9-]+", reason) else "cbc:TaxExemptionReason",
            reason,
        )
    _add(_add(node, "cac:TaxScheme"), "cbc:ID", "VAT")


def generate(L: Ledger, invoice_id: str, *, company: str) -> bytes:
    """Generate from frozen issued snapshots, refusing unsupported or locally invalid content."""
    inv = invoice(L, invoice_id, company=company)
    return build(inv)


def build(inv: dict) -> bytes:
    """Build and validate a candidate snapshot before reserving its number."""
    if inv["status"] == "draft":
        raise InvoiceError("Factura trebuie emisă înainte de exportul XML.")
    data, lines = inv["data"], inv["lines"]
    categories = {line["vat_category"] for line in lines}
    if "O" in categories and len(categories) != 1:
        raise InvoiceError("Categoria TVA O nu poate fi combinată cu alte categorii.")
    root = ET.Element("{" + ROOT + "}Invoice")
    _add(root, "cbc:CustomizationID", CUSTOMIZATION)
    _add(root, "cbc:ID", invoice_number(inv))
    for key, tag in [("issue_date", "IssueDate"), ("due_date", "DueDate")]:
        _add(root, "cbc:" + tag, data[key])
    _add(root, "cbc:InvoiceTypeCode", "380")
    for note in [*data.get("mentions", []), *([data["notes"]] if data.get("notes") else [])]:
        _add(root, "cbc:Note", note)
    if data.get("advance_payment_date") and not data.get("billing_ref"):
        _add(root, "cbc:TaxPointDate", _day(data["chargeability_date"]))
    currency = data["currency"]
    _add(root, "cbc:DocumentCurrencyCode", currency)
    if currency != "RON":
        _add(root, "cbc:TaxCurrencyCode", "RON")
    if data.get("billing_ref"):
        ref = _add(_add(root, "cac:BillingReference"), "cac:InvoiceDocumentReference")
        _add(ref, "cbc:ID", data["billing_ref"])
        _add(ref, "cbc:IssueDate", data["original_issue_date"])
    _party(root, "AccountingSupplierParty", data["seller"], True, categories == {"O"})
    _party(root, "AccountingCustomerParty", data["client"], False, categories == {"O"})
    if data.get("delivery_date") or "K" in categories:
        delivery = _add(root, "cac:Delivery")
        _add(delivery, "cbc:ActualDeliveryDate", _day(data.get("delivery_date", "")))
    if "K" in categories:
        if not data["client"].get("county", "").strip():
            raise InvoiceError("Pentru categoria K, lipsește regiunea clientului (BR-RO-211).")
        addr = _add(_add(delivery, "cac:DeliveryLocation"), "cac:Address")
        for key, tag in [
            ("street", "StreetName"),
            ("city", "CityName"),
            ("postal_code", "PostalZone"),
            ("county", "CountrySubentity"),
        ]:
            if data["client"].get(key):
                _add(addr, "cbc:" + tag, data["client"][key])
        _add(_add(addr, "cac:Country"), "cbc:IdentificationCode", data["client"]["country"])
    payment = _add(root, "cac:PaymentMeans")
    _add(payment, "cbc:PaymentMeansCode", "30")
    account = _add(payment, "cac:PayeeFinancialAccount")
    _add(account, "cbc:ID", data["seller"]["iban"])
    _add(account, "cbc:Name", data["seller"].get("account_name") or data["seller"]["name"])
    totals = data["totals"]
    tax = _add(root, "cac:TaxTotal")
    _add(tax, "cbc:TaxAmount", totals["vat"], currencyID=currency)
    for group in totals["taxes"]:
        sub = _add(tax, "cac:TaxSubtotal")
        _add(sub, "cbc:TaxableAmount", group["net"], currencyID=currency)
        _add(sub, "cbc:TaxAmount", group["vat"], currencyID=currency)
        reasons = sorted(
            {line.get("exemption_reason", "") for line in lines if line["vat_category"] == group["category"]}
        )
        _category(sub, "TaxCategory", group["category"], group["rate"], "; ".join(reasons))
    if currency != "RON":
        _add(_add(root, "cac:TaxTotal"), "cbc:TaxAmount", ron_totals(data)["vat"], currencyID="RON")
    monetary = _add(root, "cac:LegalMonetaryTotal")
    for tag, value in [
        ("LineExtensionAmount", totals["net"]),
        ("TaxExclusiveAmount", totals["net"]),
        ("TaxInclusiveAmount", totals["total"]),
        ("PayableAmount", totals["total"]),
    ]:
        _add(monetary, "cbc:" + tag, value, currencyID=currency)
    for n, row in enumerate(lines, 1):
        line = _add(root, "cac:InvoiceLine")
        _add(line, "cbc:ID", str(n))
        _add(line, "cbc:InvoicedQuantity", row["quantity"], unitCode=row["unit"])
        _add(line, "cbc:LineExtensionAmount", row["net"], currencyID=currency)
        item = _add(line, "cac:Item")
        _add(item, "cbc:Name", row["description"])
        _category(item, "ClassifiedTaxCategory", row["vat_category"], row["vat_rate"])
        _add(_add(line, "cac:Price"), "cbc:PriceAmount", row["unit_price"], currencyID=currency)
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    errors = validate(raw)
    if errors:
        raise InvoiceError("; ".join(errors))
    return raw


def validate(xml_bytes: bytes) -> list[str]:
    """Check generated-subset totals, mandatory fields, VAT category and Romanian address rules.

    Returns rule identifiers with explanations. Unsupported allowance/charge/prepayment
    structures refuse explicitly; this is not a general XSD/Schematron replacement.
    """
    try:
        root = safe_xml(xml_bytes)
    except UblError:
        return ["XML: Documentul XML nu poate fi citit: formatul trebuie verificat, iar orice DTD eliminat."]
    errors: list[str] = []

    def check(ok: bool, code: str, message: str) -> None:
        if not ok:
            errors.append(code + ": " + message)

    currency, taxes = _validate_header(root, check)
    for role in ["AccountingSupplierParty", "AccountingCustomerParty"]:
        _validate_party(root, role, check)
    _validate_delivery(root, check)
    _validate_limits(root, check)
    _validate_tax_structure(root, taxes, check)
    try:
        nets = _line_nets(root, check)
        subs = _validate_totals(root, taxes, nets, check)
        _validate_tax_groups(root, subs, nets, check)
        _validate_currencies(root, taxes, currency, check)
    except (InvoiceError, AttributeError, TypeError, DecimalException):
        check(False, "MANDATORY", "Unele sume sau date ale liniilor lipsesc ori nu sunt valide.")
    return errors


def _text(node: ET.Element, path: str) -> str:
    return (node.findtext(path, namespaces=NS) or "").strip()


def _amount(node: ET.Element, path: str, default: str | None = None) -> Decimal:
    value = _text(node, path)
    return decimal(value or default or "")


def _validate_header(root: ET.Element, check: Callable[[bool, str, str], None]) -> tuple[str, list[ET.Element]]:
    check(root.tag == "{" + ROOT + "}Invoice", "XML", "Documentul nu este o factură UBL.")
    check(_text(root, "cbc:CustomizationID") == CUSTOMIZATION, "BR-RO-001", "Formatul trebuie să fie CIUS-RO 1.0.1.")
    check(bool(re.search("[0-9]", _text(root, "cbc:ID"))), "BR-RO-010", "Numărul facturii trebuie să conțină o cifră.")
    check(
        _text(root, "cbc:InvoiceTypeCode") in {"380", "384", "389", "751"},
        "BR-RO-020",
        "Tipul facturii nu este acceptat.",
    )
    for path, label in [
        ("cbc:ID", "numărul facturii"),
        ("cbc:IssueDate", "data emiterii"),
        ("cbc:DueDate", "scadența"),
        ("cbc:DocumentCurrencyCode", "moneda facturii"),
        ("cac:InvoiceLine", "liniile facturii"),
    ]:
        verb = "Lipsesc " if path == "cac:InvoiceLine" else "Lipsește "
        check(root.find(path, NS) is not None, "MANDATORY", verb + label + ". Factura trebuie completată.")
    for path in ["cbc:IssueDate", "cbc:DueDate"]:
        try:
            _day(_text(root, path))
        except InvoiceError:
            check(False, "DATE", "Datele se scriu în formatul AAAA-LL-ZZ.")
    currency = _text(root, "cbc:DocumentCurrencyCode")
    check(bool(re.fullmatch("[A-Z]{3}", currency)), "CURRENCY", "Codul monedei lipsește sau nu este valid.")
    taxes = root.findall("cac:TaxTotal", NS)
    check(
        len(taxes) == (1 if currency == "RON" else 2),
        "BR-RO-030",
        "Totalurile TVA în moneda facturii și în lei trebuie verificate.",
    )
    if currency != "RON":
        check(
            _text(root, "cbc:TaxCurrencyCode") == "RON", "BR-RO-030", "Pentru facturi în valută, lipsește TVA în lei."
        )
    return currency, taxes


def _validate_party(root: ET.Element, role: str, check: Callable[[bool, str, str], None]) -> None:
    who, whose = ("Furnizorul", "furnizorului") if role == "AccountingSupplierParty" else ("Clientul", "clientului")
    party = root.find("cac:" + role + "/cac:Party", NS)
    if party is None:
        check(False, "MANDATORY", f"Lipsește {who.lower()}.")
        return
    name = _text(party, "cac:PartyLegalEntity/cbc:RegistrationName")
    check(bool(name), "MANDATORY", f"Lipsește numele {whose}.")
    check(len(name) <= 200, "BR-RO-L200", "Numele poate avea cel mult 200 de caractere.")
    if role == "AccountingSupplierParty":
        check(
            bool(_text(party, "cac:PartyTaxScheme/cbc:CompanyID")),
            "BR-RO-065",
            "Lipsește identificarea fiscală a furnizorului.",
        )
    if role == "AccountingCustomerParty":
        check(
            bool(
                _text(party, "cac:PartyLegalEntity/cbc:CompanyID")
                or any(
                    _text(t, "cac:TaxScheme/cbc:ID") == "VAT" and _text(t, "cbc:CompanyID")
                    for t in party.findall("cac:PartyTaxScheme", NS)
                )
            ),
            "BR-RO-065/120",
            "Lipsește identificarea fiscală a clientului.",
        )
    _validate_party_address(party, role, whose, check)


def _validate_party_address(party: ET.Element, role: str, whose: str, check: Callable[[bool, str, str], None]) -> None:
    address = party.find("cac:PostalAddress", NS)
    if address is None:
        check(False, "MANDATORY", f"Lipsește adresa {whose}.")
        return
    street, city = _text(address, "cbc:StreetName"), _text(address, "cbc:CityName")
    check(bool(street), "BR-RO-080", "Lipsește strada.")
    check(bool(city), "BR-RO-090", "Lipsește localitatea.")
    for tag, label, limit in [
        ("StreetName", "strada", 150),
        ("CityName", "localitatea", 50),
        ("PostalZone", "codul poștal", 20),
    ]:
        owner = "furnizorului" if role == "AccountingSupplierParty" else "clientului"
        check(
            len(_text(address, "cbc:" + tag)) <= limit,
            f"BR-RO-L{limit:03}",
            f"{label.capitalize()} {owner} poate avea cel mult {limit} de caractere.",
        )
    country, county = _text(address, "cac:Country/cbc:IdentificationCode"), _text(address, "cbc:CountrySubentity")
    check(bool(re.fullmatch("[A-Z]{2}", country)), "MANDATORY", "Codul țării lipsește sau nu are două litere.")
    if country == "RO":
        check(county in COUNTIES, "BR-RO-110", "Codul județului lipsește sau nu are formatul RO-XX.")
        if county == "RO-B":
            check(
                bool(re.fullmatch("SECTOR[1-6]", city)),
                "BR-RO-100",
                "Pentru București, localitatea trebuie să fie SECTOR1 până la SECTOR6.",
            )


def _validate_delivery(root: ET.Element, check: Callable[[bool, str, str], None]) -> None:
    for addr in root.findall(".//cac:DeliveryLocation/cac:Address", NS):
        check(
            bool(_text(addr, "cbc:CountrySubentity")),
            "BR-RO-211",
            "Lipsește regiunea livrării, inclusiv pentru livrări în străinătate.",
        )
        check(
            bool(_text(addr, "cbc:StreetName")) and bool(_text(addr, "cbc:CityName")),
            "BR-RO-ADDRESS",
            "Lipsesc strada și localitatea livrării.",
        )
        if _text(addr, "cac:Country/cbc:IdentificationCode") == "RO":
            check(_text(addr, "cbc:CountrySubentity") in COUNTIES, "BR-RO-110", "Lipsește județul livrării.")
            if _text(addr, "cbc:CountrySubentity") == "RO-B":
                check(
                    bool(re.fullmatch("SECTOR[1-6]", _text(addr, "cbc:CityName"))),
                    "BR-RO-100",
                    "Lipsește sectorul livrării: SECTOR1 până la SECTOR6.",
                )


def _validate_limits(root: ET.Element, check: Callable[[bool, str, str], None]) -> None:
    limits = {
        "StreetName": ("strada", 150),
        "CityName": ("localitatea", 50),
        "PostalZone": ("codul poștal", 20),
        "ID": ("numărul sau identificatorul", 200),
        "Note": ("nota", 300),
        "Description": ("descrierea", 200),
        "CompanyLegalForm": ("forma juridică", 1000),
        "Telephone": ("telefonul", 100),
        "ElectronicMail": ("adresa de email", 100),
        "TaxExemptionReason": ("motivul scutirii", 100),
        "PaymentID": ("referința plății", 140),
        "PaymentTerms": ("condițiile de plată", 300),
        "AdditionalStreetName": ("completarea adresei", 100),
    }
    for elem in root.iter():
        local = elem.tag.rsplit("}", 1)[-1]
        if local in limits:
            check(
                len(elem.text or "") <= limits[local][1],
                "BR-RO-LENGTH",
                f"{limits[local][0].capitalize()} poate avea cel mult {limits[local][1]} de caractere.",
            )
    for elem in root.findall(".//cac:Item/cbc:Name", NS):
        check(len(elem.text or "") <= LINE_DESCRIPTION_MAX, "BR-RO-L1024", LINE_DESCRIPTION_ERROR)
    for elem in root.findall(".//cac:PayeeFinancialAccount/cbc:Name", NS):
        check(len(elem.text or "") <= 200, "BR-RO-LENGTH", "Denumirea depășește limita permisă pentru câmp.")
    check(len(root.findall("cbc:Note", NS)) <= 20, "BR-RO-COUNT", "Factura poate avea cel mult 20 de note.")
    check(
        len(root.findall("cac:AdditionalDocumentReference", NS)) <= 50,
        "BR-RO-COUNT",
        "Factura poate avea cel mult 50 de documente justificative.",
    )
    check(
        len(root.findall("cac:BillingReference", NS)) <= 500,
        "BR-RO-COUNT",
        "Factura poate avea cel mult 500 de facturi anterioare.",
    )
    for item in root.findall(".//cac:Item", NS):
        check(
            len(item.findall("cac:AdditionalItemProperty", NS)) <= 50,
            "BR-RO-COUNT",
            "Fiecare articol poate avea cel mult 50 de proprietăți.",
        )


def _validate_tax_structure(root: ET.Element, taxes: list[ET.Element], check: Callable[[bool, str, str], None]) -> None:
    for tax in taxes:
        if tax.find("cac:TaxSubtotal", NS) is None:
            check(
                tax.find("cbc:TaxAmount", NS) is not None and tax.find("cbc:TaxAmount", NS).get("currencyID") == "RON",
                "BR-RO-030",
                "Totalul TVA contabil trebuie exprimat în lei.",
            )
    check(
        sum(t.find("cac:TaxSubtotal", NS) is not None for t in taxes) == 1,
        "BR-CO-15",
        "Factura poate avea un singur total TVA, cu subtotaluri.",
    )
    if (
        root.findall(".//cac:AllowanceCharge", NS)
        or root.find("cac:LegalMonetaryTotal/cbc:PrepaidAmount", NS) is not None
    ):
        check(False, "UNSUPPORTED", "Reducerile, taxele suplimentare și sumele achitate în avans nu sunt acceptate.")


def _line_nets(root: ET.Element, check: Callable[[bool, str, str], None]) -> dict[tuple[str, Decimal], Decimal]:
    lines = root.findall("cac:InvoiceLine", NS)
    nets: dict[tuple[str, Decimal], Decimal] = {}
    for line in lines:
        category = line.find("cac:Item/cac:ClassifiedTaxCategory", NS)
        if category is None:
            raise InvoiceError("Lipsește TVA-ul pe o linie.")
        cat, rate = _text(category, "cbc:ID"), _text(category, "cbc:Percent")
        key = (cat, decimal(rate or "0"))
        check(_amount(line, "cac:Price/cbc:PriceAmount") >= 0, "BR-27", "Prețul nu poate fi negativ.")
        net = _amount(line, "cbc:LineExtensionAmount")
        nets[key] = nets.get(key, Decimal(0)) + net
        check(
            money(_amount(line, "cbc:InvoicedQuantity") * _amount(line, "cac:Price/cbc:PriceAmount")) == money(net),
            "LINE-NET",
            "Valoarea netă diferă de cantitatea înmulțită cu prețul.",
        )
        check(
            bool(_text(line, "cac:Item/cbc:Name")) and bool(line.find("cbc:InvoicedQuantity", NS).get("unitCode")),
            "MANDATORY",
            "Fiecare linie are nevoie de denumire și unitate de măsură.",
        )
    return nets


def _validate_totals(
    root: ET.Element,
    taxes: list[ET.Element],
    nets: dict[tuple[str, Decimal], Decimal],
    check: Callable[[bool, str, str], None],
) -> list[ET.Element]:
    monetary = root.find("cac:LegalMonetaryTotal", NS)
    if monetary is None or not taxes:
        raise InvoiceError("Lipsesc totalurile facturii și ale TVA.")
    net = _amount(monetary, "cbc:LineExtensionAmount")
    exclusive = _amount(monetary, "cbc:TaxExclusiveAmount")
    inclusive = _amount(monetary, "cbc:TaxInclusiveAmount")
    vat = _amount(taxes[0], "cbc:TaxAmount")
    check(net == sum(nets.values()), "BR-CO-10", "Totalul net diferă de suma liniilor.")
    check(
        _amount(monetary, "cbc:AllowanceTotalAmount", "0") == 0,
        "BR-CO-11",
        "Reducerile la nivel de factură nu sunt acceptate.",
    )
    check(
        _amount(monetary, "cbc:ChargeTotalAmount", "0") == 0,
        "BR-CO-12",
        "Taxele suplimentare la nivel de factură nu sunt acceptate.",
    )
    check(exclusive == net, "BR-CO-13", "Totalul fără TVA diferă de valoarea netă.")
    check(inclusive == exclusive + vat, "BR-CO-15", "Totalul cu TVA diferă de valoarea netă plus TVA.")
    check(_amount(monetary, "cbc:PayableAmount") == inclusive, "BR-CO-16", "Suma de plată diferă de totalul cu TVA.")
    subs = taxes[0].findall("cac:TaxSubtotal", NS)
    check(
        vat == sum((_amount(sub, "cbc:TaxAmount") for sub in subs), Decimal(0)),
        "BR-CO-14",
        "TVA diferă de suma pe categorii.",
    )
    return subs


def _validate_currencies(
    root: ET.Element, taxes: list[ET.Element], currency: str, check: Callable[[bool, str, str], None]
) -> None:
    for elem in root.findall(".//*[@currencyID]"):
        expected = "RON" if len(taxes) > 1 and elem is taxes[1].find("cbc:TaxAmount", NS) else currency
        check(elem.get("currencyID") == expected, "CURRENCY", "Moneda unei sume diferă de moneda așteptată.")
        if not elem.tag.endswith("PriceAmount"):
            check(
                decimal(elem.text or "").as_tuple().exponent >= -2,
                "BR-RO-Z2",
                "Sumele pot avea cel mult două zecimale.",
            )


def _validate_tax_groups(
    root: ET.Element,
    subs: list[ET.Element],
    nets: dict[tuple[str, Decimal], Decimal],
    check: Callable[[bool, str, str], None],
) -> None:
    seen = set()
    for sub in subs:
        cat = _text(sub, "cac:TaxCategory/cbc:ID")
        raw_rate = _text(sub, "cac:TaxCategory/cbc:Percent")
        rate = decimal(raw_rate or "0")
        key = (cat, rate)
        check(key not in seen and key in nets, "VAT-GROUP", "Categoria TVA lipsește din linii sau este repetată.")
        seen.add(key)
        check(
            _amount(sub, "cbc:TaxableAmount") == nets.get(key),
            "BR-" + cat + "-08",
            "Baza impozabilă a categoriei diferă de suma liniilor.",
        )
        check(
            cat in {"S", "Z", "E", "AE", "K", "G", "O"} and (rate > 0 if cat == "S" else rate == 0),
            "BR-" + cat,
            "Categoria sau cota TVA nu este validă pe o linie.",
        )
        # CEN BR-CO-17 uses strict +/- 1 currency unit on absolute rounded values.
        check(
            abs(
                abs(_amount(sub, "cbc:TaxAmount")) - Decimal(money(abs(_amount(sub, "cbc:TaxableAmount")) * rate / 100))
            )
            < Decimal("1"),
            "BR-CO-17",
            "TVA pe categorie diferă de baza impozabilă înmulțită cu cota.",
        )
        reason = _text(sub, "cac:TaxCategory/cbc:TaxExemptionReason") or _text(
            sub, "cac:TaxCategory/cbc:TaxExemptionReasonCode"
        )
        check(
            bool(reason) if cat in {"E", "AE", "K", "G", "O"} else not reason,
            "BR-" + cat + "-REASON",
            "Motivul scutirii nu corespunde categoriei TVA.",
        )
        _validate_tax_identity(root, cat, raw_rate, nets, check)
    check(seen == set(nets), "VAT-GROUP", "Lipsește un subtotal TVA.")


def _validate_tax_identity(
    root: ET.Element,
    cat: str,
    raw_rate: str,
    nets: dict[tuple[str, Decimal], Decimal],
    check: Callable[[bool, str, str], None],
) -> None:
    supplier_vat = _text(root, "cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID")
    if cat in {"S", "Z", "E", "AE", "K", "G"}:
        check(bool(supplier_vat), "BR-" + cat + "-02", "Lipsește identificarea fiscală a furnizorului.")
    if cat == "K":
        supplier = root.findall("cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme", NS)
        check(
            any(_text(t, "cac:TaxScheme/cbc:ID") == "VAT" and _text(t, "cbc:CompanyID") for t in supplier),
            "BR-K-02",
            "Lipsește codul TVA al furnizorului.",
        )
    if cat in {"AE", "K"}:
        buyer_schemes = root.findall("cac:AccountingCustomerParty/cac:Party/cac:PartyTaxScheme", NS)
        check(
            any(_text(t, "cac:TaxScheme/cbc:ID") == "VAT" and _text(t, "cbc:CompanyID") for t in buyer_schemes),
            "BR-" + cat + "-03",
            "Lipsește codul TVA al clientului.",
        )
    if cat == "K":
        check(bool(_text(root, "cac:Delivery/cbc:ActualDeliveryDate")), "BR-K-DELIVERY", "Lipsește data livrării.")
        check(
            bool(_text(root, "cac:Delivery/cac:DeliveryLocation/cac:Address/cac:Country/cbc:IdentificationCode")),
            "BR-K-COUNTRY",
            "Lipsește țara livrării.",
        )
    if cat == "O":
        vat_schemes = [n.text for n in root.findall(".//cac:PartyTaxScheme/cac:TaxScheme/cbc:ID", NS)]
        check(
            len(nets) == 1 and not raw_rate and "VAT" not in vat_schemes,
            "BR-O",
            "Categoria O se folosește separat, fără coduri sau cote TVA.",
        )
