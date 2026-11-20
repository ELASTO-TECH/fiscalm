"""The lines an invoice sheet shows, read-only: an invoice written in fiscalm shows its stored lines; one read from
SPV, sent or received, shows the lines of its stored UBL XML. Display only: no total, match or payment uses them.

The document totals are shown as written: an invoice written here has the totals it was issued with; a UBL document
its LegalMonetaryTotal and the TaxTotal in its own currency. Totals that are missing or not decimal numbers are left
out (null) rather than summed from the lines. Each line's VAT and total with VAT are its net amount times its rate,
rounded to the cent, so they can differ from the document's VAT totals by rounding.

Every source has the same shape. A file that is missing, outside the company's e-Factura directory, refused (a DTD)
or malformed (an amount that is not a decimal number) makes the whole list unavailable, never a partial guess."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal, localcontext

from fiscalm.efactura import inbox as ubl
from fiscalm.invoicing import engine as invoicing
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.invoicing.labels import UNITS
from fiscalm.ledger import Ledger

MAX_LINES = 200
TEXT_MAX = 300
CODE_MAX = 10
DECIMAL = re.compile(r"[+-]?[0-9]{1,18}(\.[0-9]{1,18})?")
UNIT_LABELS = {u["code"]: u["label"] for u in UNITS}


def unavailable() -> dict:
    return {"lines": [], "currency": "", "truncated": False, "unavailable": True, "totals": None}


def sheet(L: Ledger, co: str, kind: str, row: dict) -> dict:
    """Lines of the invoice the sheet shows (kind `invoice` received, `issued` sent), read within company `co`."""
    try:
        if kind == "issued" and row.get("local_id"):
            inv = invoicing.invoice(L, row["local_id"], company=co)
            return project(inv["lines"], inv["data"]["currency"], _issued_totals(inv["data"].get("totals")))
        table = "efactura" if kind == "invoice" else "issued"
        stored = L.db.execute(f"SELECT raw_path FROM {table} WHERE message_id=? AND company=?", (row["id"], co))
        found = stored.fetchone()
        tree = ubl.stored_tree(co, found["raw_path"]) if found else None
        return from_xml(tree) if tree is not None else unavailable()
    except (ubl.UblError, InvoiceError):
        return unavailable()


def from_xml(tree: ET.Element) -> dict:
    """Lines of a UBL Invoice or CreditNote, as written; UblError for anything malformed."""
    namespace, _, name = tree.tag[1:].partition("}")
    if ubl.ROOTS.get(namespace) != name:
        raise ubl.UblError("not a UBL invoice or credit note")
    quantity = "cbc:InvoicedQuantity" if name == "Invoice" else "cbc:CreditedQuantity"
    # Every line is checked, also past the ones returned: the tree is already built, so a refusal stays whole.
    lines = [_line(node, quantity) for node in tree.iterfind(f"cac:{name}Line", ubl.NS)]
    currency = _text(tree, "cbc:DocumentCurrencyCode")
    return project(lines, currency, _xml_totals(tree, currency))


def _xml_totals(tree: ET.Element, currency: str) -> dict | None:
    """LegalMonetaryTotal and the one TaxTotal with subtotals in the document currency (the VAT repeated in the
    accounting currency, BT-111, has none); None unless every amount is a decimal number in the document currency."""
    own = [
        t
        for t in tree.iterfind("cac:TaxTotal", ubl.NS)
        if t.find("cac:TaxSubtotal", ubl.NS) is not None
        and _currency(t.find("cbc:TaxAmount", ubl.NS)) in ("", currency)
    ]
    if len(own) != 1:  # none, or several that XML order would choose between
        return None
    money = tree.find("cac:LegalMonetaryTotal", ubl.NS)
    amounts = [*own[0].iter(), *(money.iter() if money is not None else [])]
    if any(_currency(node) not in ("", currency) for node in amounts):
        return None
    taxes = [
        {
            "category": _text(sub, "cac:TaxCategory/cbc:ID")[:CODE_MAX],
            "rate": _text(sub, "cac:TaxCategory/cbc:Percent") or None,
            "net": _text(sub, "cbc:TaxableAmount"),
            "vat": _text(sub, "cbc:TaxAmount"),
        }
        for sub in own[0].iterfind("cac:TaxSubtotal", ubl.NS)
    ]
    path = "cac:LegalMonetaryTotal/cbc:"
    totals = {
        "net": _text(tree, path + "TaxExclusiveAmount"),
        "vat": _text(own[0], "cbc:TaxAmount"),
        "total": _text(tree, path + "TaxInclusiveAmount"),
        "payable": _text(tree, path + "PayableAmount"),
        "taxes": taxes,
    }
    return totals if _usable(totals) else None


def _issued_totals(totals: dict | None) -> dict | None:
    """The totals an invoice written here was issued with; it is paid in full, so payable is its total."""
    if not isinstance(totals, dict):
        return None
    taxes = [{k: g.get(k) for k in ("category", "rate", "net", "vat")} for g in totals.get("taxes", [])]
    out = {**{k: totals.get(k) for k in ("net", "vat", "total")}, "payable": totals.get("total"), "taxes": taxes}
    return out if _usable(out) else None


def _usable(totals: dict) -> bool:
    taxes = totals["taxes"]
    amounts = [totals[k] for k in ("net", "vat", "total", "payable")] + [g[k] for g in taxes for k in ("net", "vat")]
    return all(_number(a) for a in amounts + [g["rate"] for g in taxes if g["rate"]])


def _currency(node: ET.Element | None) -> str:
    return "" if node is None else node.get("currencyID", "").strip()


def _line(node: ET.Element, quantity: str) -> dict:
    qty = node.find(quantity, ubl.NS)
    price, base = _text(node, "cac:Price/cbc:PriceAmount"), node.find("cac:Price/cbc:BaseQuantity", ubl.NS)
    base_text = "1" if base is None else (base.text or "").strip()
    # A price is money: a missing one, or a base quantity that is not a positive decimal, refuses the document.
    if not _number(price) or not _number(base_text) or Decimal(base_text) <= 0:
        raise ubl.UblError("unit price")
    tax = "cac:Item/cac:ClassifiedTaxCategory/"
    return {
        "description": _text(node, "cac:Item/cbc:Name") or _text(node, "cac:Item/cbc:Description"),
        "quantity": _text(node, quantity),
        "unit": qty.get("unitCode", "") if qty is not None else "",
        # A price per base quantity other than one is shown as no unit price rather than a misleading one.
        "unit_price": price if Decimal(base_text) == 1 else None,
        "net": _text(node, "cbc:LineExtensionAmount"),
        "vat_rate": _text(node, tax + "cbc:Percent") or None,
        "vat_category": _text(node, tax + "cbc:ID"),
    }


def project(rows: list[dict], currency: str, totals: dict | None) -> dict:
    """The sheet's shape for lines from any source: every row is validated, the first MAX_LINES are returned with the
    document `totals` (None when it has none usable). UblError when an amount, code or description of any row is not
    usable."""
    if not re.fullmatch("[A-Z]{3}", currency or ""):
        raise ubl.UblError("currency")
    lines = []
    for position, row in enumerate(rows, 1):
        amounts = [row[k] for k in ("quantity", "net")] + [row[k] for k in ("unit_price", "vat_rate") if row[k]]
        if not row["description"].strip() or not all(_number(a) for a in amounts):
            raise ubl.UblError(f"line {position}")
        if position > MAX_LINES:
            continue
        unit = row["unit"][:CODE_MAX]
        lines.append(
            {
                "position": position,
                **({"product_id": row["product_id"]} if "product_id" in row else {}),
                "description": row["description"].strip()[:TEXT_MAX],
                "quantity": row["quantity"],
                "unit": UNIT_LABELS.get(unit, unit),
                "unit_price": row["unit_price"] or None,
                "net": row["net"],
                "vat_rate": row["vat_rate"] or None,
                "vat_category": row["vat_category"][:CODE_MAX],
                **_with_vat(row["net"], row["vat_rate"] or None),
            }
        )
    return {
        "lines": lines,
        "currency": currency,
        "truncated": len(rows) > MAX_LINES,
        "unavailable": False,
        "totals": totals,
    }


def _with_vat(net: str, rate: str | None) -> dict:
    """A line's VAT and total with VAT, net times rate rounded half up to the cent; null without a rate."""
    if rate is None:
        return {"vat": None, "gross": None}
    with localcontext() as ctx:
        ctx.prec = 60  # every decimal _number accepts, multiplied by a rate, stays exact
        vat = (Decimal(net) * Decimal(rate) / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        return {"vat": format(vat, "f"), "gross": format(Decimal(net) + vat, "f")}


def _number(value: str) -> bool:
    return isinstance(value, str) and DECIMAL.fullmatch(value) is not None


def _text(node: ET.Element, path: str) -> str:
    return (node.findtext(path, "", ubl.NS) or "").strip()
