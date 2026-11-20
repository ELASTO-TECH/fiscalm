"""Minimal internal UBL projections of imported data; never an ANAF submission."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from fiscalm.efactura.inbox import NS, Invoice


def _add(parent: ET.Element, tag: str, value: str = "") -> ET.Element:
    prefix, name = tag.split(":")
    child = ET.SubElement(parent, f"{{{NS[prefix]}}}{name}")
    child.text = value or None
    return child


def project(inv: Invoice) -> bytes:
    root = ET.Element("{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice")
    _add(root, "cbc:ID", inv.number)
    _add(root, "cbc:IssueDate", inv.issue_date)
    if inv.due_date:
        _add(root, "cbc:DueDate", inv.due_date)
    _add(root, "cbc:DocumentCurrencyCode", inv.currency)
    for role, name, tax in (
        ("AccountingSupplierParty", inv.supplier_name, inv.supplier_tax_id or inv.supplier_cui),
        ("AccountingCustomerParty", inv.buyer_name, inv.buyer_tax_id or inv.buyer_cui),
    ):
        legal = _add(_add(_add(root, f"cac:{role}"), "cac:Party"), "cac:PartyLegalEntity")
        _add(legal, "cbc:RegistrationName", name)
        _add(legal, "cbc:CompanyID", tax)
    _add(_add(root, "cac:LegalMonetaryTotal"), "cbc:PayableAmount", inv.total)
    for iban in inv.ibans:
        _add(_add(_add(root, "cac:PaymentMeans"), "cac:PayeeFinancialAccount"), "cbc:ID", iban)
    return ET.tostring(root, encoding="utf-8")
