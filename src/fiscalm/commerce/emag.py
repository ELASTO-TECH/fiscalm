"""eMAG Marketplace reads and conservative order translation. No response supplies a URL."""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from collections.abc import Callable, Iterator
from decimal import Decimal

from fiscalm import integrations
from fiscalm.commerce import orders as commerce
from fiscalm.commerce.webhooks import _OPENER
from fiscalm.invoicing.labels import COUNTY_NAMES

BASES = {country: f"https://marketplace-api.emag.{country.lower()}/api-3/" for country in ("RO", "BG", "HU")}
ENDPOINTS = {"order/read", "rma/read", "awb/read", "order/attachments/save"}


class Refused(ValueError):
    """A safe reason code, never an upstream message or credential."""


class Client:
    def __init__(self, shop: integrations.Instance):
        self.shop = shop

    def __call__(self, endpoint: str, body: dict) -> list:
        if endpoint not in ENDPOINTS:
            raise Refused("endpoint")
        import urllib.request

        auth = base64.b64encode(
            (self.shop.secret("emag_username") + ":" + self.shop.secret("emag_password")).encode()
        ).decode()
        request = urllib.request.Request(
            BASES[self.shop.config["marketplace_country"]] + endpoint,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Basic " + auth},
        )
        try:
            with _OPENER.open(request, timeout=10) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError()
            data = json.loads(raw, parse_float=str)
            if data.get("isError") is not False or not isinstance(data.get("results"), list):
                raise ValueError()
            return data["results"]
        except Exception:
            raise Refused("api_unavailable") from None


def pages(api: Callable, endpoint: str, filters: dict) -> Iterator[dict]:
    for number in range(1, 1001):
        rows = api(endpoint, filters | {"currentPage": number, "itemsPerPage": 100})
        if not isinstance(rows, list) or len(rows) > 100:
            raise Refused("invalid_page")
        yield from rows
        if len(rows) < 100:
            return
    raise Refused("pagination_limit")


def identifier(value: object) -> str:
    if type(value) is not int or not 1 <= value <= 4294967295:
        raise Refused("invalid_id")
    return str(value)


def number(value: object) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int)):
        raise Refused("invalid_decimal")
    result = Decimal(value)
    if not result.is_finite():
        raise Refused("invalid_decimal")
    return result


def translate(shop: integrations.Instance, row: dict) -> dict:
    """Use order-time VAT only. Current catalogue VAT cannot prove historic tax treatment."""
    if row.get("is_complete") != 1 or row.get("is_storno"):
        raise Refused("incomplete_or_storno")
    buyer = row["customer"]
    if buyer["legal_entity"] not in (0, 1):
        raise Refused("buyer_type")
    address = _buyer_address(buyer)
    client = dict(
        type="company" if buyer["legal_entity"] else "person",
        name=buyer.get("company") if buyer["legal_entity"] else buyer["billing_name"],
        address=address,
    )
    if buyer["legal_entity"]:
        client["cui"] = buyer["code"]
    if address["country"] != shop.config["marketplace_country"]:
        raise Refused("cross_border_tax")
    lines, currencies = _order_lines(shop, row)
    # Shipping lacks a documented tax basis/rate. Refuse rather than infer it from merchandise.
    if number(row.get("shipping_tax", "0")) or row.get("shipping_tax_voucher_split"):
        raise Refused("shipping_tax")
    _voucher_allocation(row)
    if len(currencies) != 1:
        raise Refused("currency")
    method = {1: "cod", 2: "transfer", 3: "card"}[row["payment_mode_id"]]
    return commerce.validate(
        dict(
            channel=shop.id,
            external_id=identifier(row["id"]),
            placed_at=row["date"][:10],
            currency=currencies.pop(),
            prices_include_vat=False,
            buyer=client,
            lines=lines,
            payment=dict(method=method, paid=False, processor="emag"),
        )
    )


def _folded(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value).casefold() if c.isascii() and c.isalnum())


def _buyer_address(buyer: dict) -> dict:
    address = {
        key: buyer["billing_" + source]
        for key, source in [("street", "street"), ("city", "city"), ("county", "suburb"), ("country", "country")]
    }
    if address["country"] == "RO":
        matched = {_folded(name): "RO-" + code for code, name in COUNTY_NAMES.items()}
        if address["county"] not in commerce.inv.COUNTIES:
            address["county"] = matched.get(_folded(address["county"]), "")
        if not address["county"]:
            raise Refused("county")
        if address["county"] == "RO-B":
            sector = re.fullmatch(r"(?:bucuresti)?sector([1-6])", _folded(address["city"]))
            if not sector:
                raise Refused("sector")
            address["city"] = "SECTOR" + sector[1]
    return address


def _order_lines(shop: integrations.Instance, row: dict) -> tuple[list[dict], set[str]]:
    lines = []
    currencies = set()
    for product in row["products"]:
        if product["status"] == 0:
            continue
        if product["status"] != 1 or product.get("recycle_warranties"):
            raise Refused("unsupported_product")
        if "vat" not in product:
            raise Refused("missing_vat")
        rate = number(product["vat"]) * 100
        quantity, price = number(product["quantity"]), number(product["sale_price"])
        if quantity <= 0 or price < 0:
            raise Refused("invalid_product")
        currencies.add(
            product.get("currency", {"RO": "RON", "BG": "EUR", "HU": "HUF"}[shop.config["marketplace_country"]])
        )
        lines.append(
            dict(
                description=product["name"],
                quantity=str(quantity),
                unit="H87",
                unit_price=str(price),
                vat_rate=str(rate),
            )
        )
        lines.extend(_voucher_lines(product, rate))
    return lines, currencies


def _voucher_lines(product: dict, rate: Decimal) -> list[dict]:
    lines = []
    for split in product.get("product_voucher_split") or []:
        if split.get("offered_by") != "seller":
            raise Refused("voucher_funding")
        value, vat = number(split["value"]), number(split["vat_value"])
        split_rate = number(split["vat"]) * 100
        if split_rate != rate:
            raise Refused("voucher_tax")
        if value >= 0 or vat > 0 or abs((value * split_rate / 100).quantize(Decimal(".01")) - vat) > Decimal(".01"):
            raise Refused("voucher_tax")
        lines.append(
            dict(
                description=split["voucher_name"],
                quantity="1",
                unit="H87",
                unit_price=str(value),
                vat_rate=str(split_rate),
            )
        )
    return lines


def _voucher_allocation(row: dict) -> None:
    totals = {}
    for product in row["products"]:
        for split in product.get("product_voucher_split") or []:
            key = identifier(split["voucher_id"])
            net, tax = totals.get(key, (Decimal(0), Decimal(0)))
            totals[key] = net + number(split["value"]), tax + number(split["vat_value"])
    expected = {}
    for voucher in row.get("vouchers") or []:
        if voucher.get("status") == 0:
            continue
        key = identifier(voucher["voucher_id"])
        if key in expected:
            raise Refused("voucher_allocation")
        expected[key] = number(voucher["sale_price"]), number(voucher["sale_price_vat"])
    if totals != expected:
        raise Refused("voucher_allocation")
