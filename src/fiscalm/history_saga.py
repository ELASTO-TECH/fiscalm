"""SAGA exchange XML, detected by root; collections are claims, never payment proof.

Structure: https://manual.sagasoft.ro/sagac/topic-76-import-date.html
Normalized UBL is an internal projection, not an e-Factura submitted to ANAF.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from fiscalm.efactura.inbox import Invoice, UblError, safe_xml, tax_token
from fiscalm.history_xml import project
from fiscalm.identity import partner_identity
from fiscalm.ledger import Ledger


def text(node: ET.Element, key: str) -> str:
    return (node.findtext(key) or "").strip()


def day(value: str) -> str:
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise UblError("Invalid SAGA date")


def amount(value: str) -> Decimal:
    if not re.fullmatch(r"-?\d{1,12}(?:[.,]\d{1,2})?", value):
        raise UblError("Invalid SAGA amount")
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        raise UblError("Invalid SAGA amount") from None


def invoice(node: ET.Element) -> bytes:
    header = node.find("Antet")
    if header is None or text(header, "FacturaTip") not in ("", "T"):
        raise UblError("Not a SAGA invoice")
    lines = node.findall("Detalii/Continut/Linie")
    if not lines or not text(header, "FacturaNumar"):
        raise UblError("Missing SAGA lines or number")
    currency = text(header, "FacturaMoneda").upper() or "RON"
    reverse = text(header, "FacturaTip") == "T" or text(header, "FacturaTaxareInversa").casefold() == "da"
    if currency != "RON" or (reverse and any(amount(text(x, "TVA") or "0") for x in lines)):
        raise UblError("Unverified SAGA currency or reverse-charge amount")
    total = sum((amount(text(x, "Valoare")) + amount(text(x, "TVA") or "0") for x in lines), Decimal(0))
    if amount(text(header, "FacturaAccize") or "0"):
        raise UblError("SAGA excise totals need review")
    return project(
        Invoice(
            doc_type="invoice",
            number=text(header, "FacturaNumar"),
            issue_date=day(text(header, "FacturaData")),
            due_date=day(text(header, "FacturaScadenta")) if text(header, "FacturaScadenta") else "",
            currency=currency,
            total=f"{total:.2f}",
            supplier_name=text(header, "FurnizorNume"),
            supplier_cui=tax_token(text(header, "FurnizorCIF")),
            buyer_cui=tax_token(text(header, "ClientCIF")),
            supplier_tax_id=text(header, "FurnizorCIF"),
            buyer_name=text(header, "ClientNume"),
            buyer_tax_id=text(header, "ClientCIF"),
            ibans=[text(header, "FurnizorIBAN")] if text(header, "FurnizorIBAN") else [],
        )
    )


def parse(data: bytes) -> tuple[list[bytes], list[dict]] | None:
    root = safe_xml(data)
    if root.tag == "Facturi":
        nodes = root.findall("Factura")
        if len(nodes) > 20_000:
            raise UblError("Too many SAGA invoices")
        return [invoice(node) for node in nodes], []
    if root.tag not in ("Clienti", "Incasari"):
        return None
    records = [{"kind": root.tag, "fields": {x.tag: x.text or "" for x in node}} for node in root.findall("Linie")]
    if not records or len(records) > 20_000:
        raise UblError("Empty SAGA export")
    return [], records


def store_records(L: Ledger, company: str, sha: str, records: list[dict]) -> int:
    """Keep each source claim once. Imported clients never overwrite a reviewed profile.

    SAGA's optional address fields may be missing; issuing still requires a complete,
    validated client. No collection record changes an invoice or reserves a bank credit.
    """
    key = f"history_source:{company}:{sha}"
    with L.tx() as db:
        if db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
            return 0
        for position, record in enumerate(records):
            if record["kind"] != "Clienti":
                continue
            f = record["fields"]
            country = f.get("Tara", "").strip().upper()
            raw_id = re.sub(r"\s+", "", f.get("Cod_fiscal", "")).upper()
            tax = tax_token(raw_id)
            profile = {
                k: f.get(v, "").strip()
                for k, v in {
                    "name": "Denumire",
                    "reg_com": "Reg_com",
                    "street": "Adresa",
                    "city": "Localitate",
                    "county": "Judet",
                    "email": "Email",
                    "phone": "Tel",
                    "iban": "Cont_banca",
                    "bank": "Banca",
                }.items()
            }
            profile.update(
                country=country,
                cui=tax if country == "RO" else "",
                vat_id=raw_id if re.fullmatch(r"[A-Z]{2}[A-Z0-9]+", raw_id) else "",
                ro_vat_status="unknown",
            )
            identity = partner_identity(country, profile["cui"], profile["vat_id"])
            if not profile["name"]:
                continue
            key_part = identity or f"{sha}:{position}"
            cid = hashlib.sha256(f"{company}:{key_part}".encode()).hexdigest()[:32]
            db.execute(
                "INSERT OR IGNORE INTO invoice_clients(id,company,identity,data) VALUES(?,?,?,?)",
                (cid, company, identity or None, json.dumps(profile)),
            )
        db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (key, json.dumps(records)))
    return sum(r["kind"] != "source" for r in records)
