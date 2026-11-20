"""Product catalog contract shared by app actions and OpenAPI."""

from __future__ import annotations

from fiscalm.invoicing.labels import VAT
from fiscalm.invoicing.schema import object_schema

TEXT = {"type": "string"}
FIELDS = {
    "kind": {"type": "string", "enum": ["produs", "serviciu"]},
    **{k: TEXT for k in ("name", "code", "unit", "currency", "description", "exemption")},
    "unit_price": {"type": "string", "pattern": r"^[0-9]+\.[0-9]{2}$"},
    "vat_rate": {"type": "string", "enum": VAT["rates"]},
    "vat_category": {"type": "string", "enum": [c["code"] for c in VAT["categories"]]},
}
BODIES = {
    "product-add": object_schema(
        FIELDS, ["kind", "name", "unit", "unit_price", "currency", "vat_category", "vat_rate"]
    ),
    "product-update": object_schema({"id": TEXT, "version": {"type": "integer"}, **FIELDS}, ["id", "version"]),
    "product-archive": object_schema(
        {"id": TEXT, "version": {"type": "integer"}, "archived": {"type": "boolean"}},
        ["id", "version", "archived"],
    ),
    "product-delete": object_schema({"id": TEXT, "version": {"type": "integer"}}, ["id", "version"]),
}


def schemas() -> dict:
    product = object_schema(
        {
            "id": TEXT,
            "company": TEXT,
            **FIELDS,
            "archived": {"type": "boolean"},
            "version": {"type": "integer"},
            "can_delete": {"type": "boolean"},
        },
        ["id", "company", *FIELDS, "archived", "version", "can_delete"],
    )
    product["description"] = (
        "One firm's catalog entry. unit is the invoice unit code (H87=buc, HUR=oră, MON=lună, KGM=kg). "
        "Price is net of VAT. Editing defaults never changes saved invoice lines. "
        "Version is required for updates, archive/restore and delete; stale writes refuse. "
        "Only never-referenced entries can be deleted; referenced entries may be archived."
    )
    return {
        "Product": product,
        "Products": object_schema(
            {
                "company": TEXT,
                "products": {"type": "array", "items": product},
                "total": {"type": "integer"},
                "next_cursor": {"type": ["string", "null"]},
            },
            ["company", "products", "total", "next_cursor"],
        ),
        "ProductDetail": object_schema({"product": product}, ["product"]),
    }


def sales_schemas() -> dict:
    def response(fields: dict) -> dict:
        return object_schema(fields, list(fields))

    decimal = {"type": "string", "pattern": r"^-?[0-9]+(\.[0-9]+)?$"}
    row = response(
        {
            "product_id": {"type": ["string", "null"]},
            **{k: TEXT for k in ("name", "currency", "unit")},
            "revenue": {"type": "string", "pattern": r"^-?[0-9]+\.[0-9]{2}$"},
            "quantity": decimal,
            "invoice_count": {"type": "integer"},
        }
    )
    products = {"type": "array", "items": row}
    coverage = {"complete": {"type": "boolean"}, "unavailable_count": {"type": "integer"}}
    return {
        "ProductSales": response(
            {
                "company": TEXT,
                "from": TEXT,
                "to": TEXT,
                **coverage,
                "months": {"type": "array", "items": response({"month": TEXT, "products": products, **coverage})},
                "totals": products,
            }
        )
    }
