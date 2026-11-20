"""Synthetic UBL shared by issued-invoice tests and app fixtures; no test dependencies."""

from __future__ import annotations


def ubl(
    number: str = "NRD 0042",
    total: str = "1190.00",
    issue: str = "2026-10-10",
    due: str = "2026-10-25",
    client: str = "BETA SOFTWARE SRL",
    client_cui: str = "RO66666666",
    supplier_cui: str = "RO11111111",
    root: str = "Invoice",
    currency: str = "RON",
    ref: str = "",
) -> bytes:
    ns = f"urn:oasis:names:specification:ubl:schema:xsd:{root}-2"
    billing = (
        (
            f"<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>{ref}</cbc:ID>"
            f"</cac:InvoiceDocumentReference></cac:BillingReference>"
        )
        if ref
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="{ns}" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>{number}</cbc:ID><cbc:IssueDate>{issue}</cbc:IssueDate>{f"<cbc:DueDate>{due}</cbc:DueDate>" if due else ""}
 <cbc:DocumentCurrencyCode>{currency}</cbc:DocumentCurrencyCode>{billing}
 <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme><cbc:CompanyID>{supplier_cui}</cbc:CompanyID>
  </cac:PartyTaxScheme><cac:PartyLegalEntity><cbc:RegistrationName>Atelier Nord SRL</cbc:RegistrationName>
  </cac:PartyLegalEntity></cac:Party></cac:AccountingSupplierParty>
 <cac:AccountingCustomerParty><cac:Party><cac:PartyTaxScheme><cbc:CompanyID>{client_cui}</cbc:CompanyID>
  </cac:PartyTaxScheme><cac:PartyLegalEntity><cbc:RegistrationName>{client}</cbc:RegistrationName>
  </cac:PartyLegalEntity></cac:Party></cac:AccountingCustomerParty>
 <cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="{currency}">{total}</cbc:PayableAmount></cac:LegalMonetaryTotal>
</{root}>""".encode()
