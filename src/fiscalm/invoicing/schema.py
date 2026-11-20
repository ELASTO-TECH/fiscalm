"""Shared schemas for public invoicing HTTP and MCP inputs."""

from __future__ import annotations

import re

TEXT = {"type": "string"}
BOOL = {"type": "boolean"}
VERSION = {"type": "integer", "minimum": 1}


def object_schema(fields: dict, required: tuple | list = ()) -> dict:
    return dict(type="object", properties=fields, required=list(required), additionalProperties=False)


ADDRESS = object_schema({k: TEXT for k in ("street", "city", "county", "postal_code", "country")})
PARTY = object_schema(
    {
        k: TEXT
        for k in (
            "name",
            "cui",
            "vat_id",
            "tax_id",
            "ro_vat_status",
            "reg_com",
            "iban",
            "bank",
            "account_name",
            "email",
            "phone",
            "country",
        )
    }
    | {k: BOOL for k in ("private", "vat_payer", "efactura_registered", "inactive")}
    | {"address": ADDRESS},
    ["name", "address", "vat_payer"],
)
LINE = object_schema(
    {
        k: TEXT
        for k in (
            "description",
            "quantity",
            "unit",
            "unit_price",
            "vat_category",
            "vat_rate",
            "exemption",
            "gross_unit_price",
            "product_id",
        )
    },
    ["description", "quantity", "unit", "unit_price", "vat_category", "vat_rate"],
)
LINE["properties"]["product_id"] = {
    **TEXT,
    "description": "Optional product of this invoice's firm. Copy catalog defaults when choosing a product, then send the line's own values. Saved lines never follow later catalog edits. New links require an active product; an archived product already on any line of this draft or its existing recurring template may stay. Recurrence context comes only from the rule runner, never from request fields. Only copy price when currencies match; no conversion is inferred.",
}
RECURRENCE = object_schema(
    {k: TEXT for k in ("id", "company", "client_id", "series", "start_period", "backlog_policy")}
    | {
        "template": {"type": "object"},
        "day": {"type": "integer"},
        "due_days": {"type": "integer"},
        "paused": BOOL,
        "reason": {
            "type": ["string", "null"],
            "enum": [
                None,
                "manual",
                "missing_client",
                "missing_series",
                "invalid_template",
                "rate_unavailable",
                "unsupported_currency",
            ],
        },
        "last_period": {"type": ["string", "null"]},
        "client": {"type": ["string", "null"]},
        "total": {"type": ["string", "null"]},
        "next_date": {"type": ["string", "null"]},
        **{k: BOOL for k in ("can_pause", "can_resume", "can_delete")},
        "skipped_periods": {"type": "integer"},
        "last_invoice": {
            "description": "The invoice the rule last issued on its own: number (series-number), client as issued, when.",
            "anyOf": [
                object_schema(
                    {
                        "id": TEXT,
                        "at": TEXT,
                        "number": TEXT,
                        "client": TEXT,
                    }
                ),
                {"type": "null"},
            ],
        },
        "last_skipped": {
            "anyOf": [
                object_schema(
                    {k: TEXT for k in ("recurrence", "company", "from_period", "through_period", "at")}
                    | {"count": {"type": "integer"}}
                ),
                {"type": "null"},
            ]
        },
    }
)
BODIES = {
    "recurrence-create": object_schema(
        {
            "draft_id": TEXT,
            "first_period": {"type": "string", "pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
            "day": {"type": "integer", "minimum": 1, "maximum": 31},
            "due_days": {"type": "integer", "minimum": 0, "maximum": 365},
        },
        ["draft_id", "day", "due_days"],
    ),
    **{
        op: object_schema({"id": TEXT}, ["id"]) for op in ("recurrence-pause", "recurrence-resume", "recurrence-delete")
    },
    "client-lookup": object_schema({"cui": TEXT}, ["cui"]),
    "client-save": object_schema(
        {
            "id": TEXT,
            "client": PARTY,
            "role": {
                "type": "string",
                "enum": ["client", "supplier"],
                "description": "What the firm saves the partner as, added to its roles; a new one is a client unless given. "
                "Without id, a firm already saved keeps its details and only gains the role.",
            },
        },
        ["client"],
    ),
    "draft-save": object_schema(
        {k: TEXT for k in ("id", "series", "client_id", "issue_date", "due_date", "currency", "notes", "delivery_date")}
        | {
            "version": VERSION,
            "advance_invoice": BOOL,
            "advance_payment_date": {"type": ["string", "null"]},
            "supply_country": TEXT,
            "delivery_date": {"type": ["string", "null"]},
            "iban": {
                "type": "string",
                "description": "The firm's current account the invoice asks to be paid into; without it, the seller "
                "profile's. Checked again at issue.",
            },
            "lines": {"type": "array", "items": LINE},
        },
        ["series", "client_id", "issue_date", "due_date", "currency", "notes", "lines"],
    )
    | {"dependentRequired": {"id": ["version"]}},
    **{op: object_schema({"id": TEXT}, ["id"]) for op in ("upload",)},
    "draft-delete": object_schema({"id": TEXT, "version": VERSION}, ["id"]),
    "issue": object_schema({"id": TEXT, "version": VERSION}, ["id", "version"]),
    "storno": object_schema(
        {"id": TEXT, "reason": TEXT, "lines": {"type": "array", "items": {"type": "integer"}}}, ["id", "reason"]
    ),
    "resolve-upload": object_schema(
        {"id": TEXT, "checked_in_spv": BOOL, "upload_id": {"type": ["string", "null"]}},
        ["id", "checked_in_spv", "upload_id"],
    ),
    "auto-upload": object_schema({"on": BOOL}, ["on"]),
}


def response_schemas() -> dict:
    """Describe app projections without including seller configuration or logo capabilities."""
    nullable = {"type": ["string", "null"]}

    def strings(*names: str) -> dict:
        return {n: TEXT for n in names}

    money = {"type": "string", "pattern": r"^-?[0-9]+\.[0-9]{2}$"}

    def array(item: dict) -> dict:
        return {"type": "array", "items": item}

    profile = PARTY
    flat_party = object_schema(
        {k: v for k, v in PARTY["properties"].items() if k != "address"}
        | ADDRESS["properties"]
        | {"vat_on_collection": BOOL}
    )
    engine_line = object_schema(
        {k: v for k, v in LINE["properties"].items() if k != "exemption"} | {"net": money, "exemption_reason": TEXT}
    )
    seller = object_schema(
        strings("legal_name", "cui", "vat_id", "reg_com", "iban", "bank", "email", "phone")
        | {"vat_payer": BOOL, "vat_on_collection": BOOL, "address": ADDRESS}
    )
    totals = object_schema(
        {k: money for k in ("net", "vat", "total")}
        | {
            "by_vat": array(object_schema(strings("category", "rate") | {"base": money, "vat": money})),
            "total_ron": {"type": ["string", "null"]},
            "vat_ron": {"type": ["string", "null"]},
        }
    )
    legal = {
        "advance_invoice": BOOL,
        "advance_payment_date": nullable,
        "chargeability_date": nullable,
        "mentions": array(TEXT),
        "warnings": array(TEXT),
        "supply_country": TEXT,
    }
    draft = object_schema(
        strings("id", "series", "issue_date", "due_date", "currency", "notes")
        | legal
        | {
            "order_id": nullable,
            "order_sheet_id": nullable,
            "sales_origin": TEXT,
            "order_number": nullable,
            "awb": {"type": ["object", "null"]},
            "cod_at_courier": BOOL,
            "version": VERSION,
            "client_id": nullable,
            "client": profile,
            "lines": array(LINE),
            "delivery_date": nullable,
            "iban": {
                "type": "string",
                "description": "The IBAN the draft issues with: its chosen account, else the seller profile's; a storno "
                "draft's original's. A chosen account no longer current comes back as stored; \"\" for the profile's.",
            },
            "updated_at": nullable,
            "totals": totals,
            "exchange_rate": {"anyOf": [object_schema(strings("rate", "date")), {"type": "null"}]},
        }
    )
    return {
        "InvoicingDraft": draft,
        "InvoicingIssued": object_schema(
            strings("id", "company", "series", "status")
            | {
                "version": VERSION,
                "number": {"type": "integer"},
                "data": object_schema(
                    strings("issue_date", "due_date", "currency", "notes", "exchange_rate", "exchange_date")
                    | legal
                    | {
                        "seller": flat_party,
                        "client": flat_party,
                        "totals": object_schema(
                            {k: money for k in ("net", "vat", "total")}
                            | {
                                "taxes": array(
                                    object_schema(strings("category", "rate") | {"net": money, "vat": money})
                                )
                            }
                        ),
                        "issued_live": BOOL,
                        "logo_sha": nullable,
                        "delivery_date": nullable,
                        "billing_ref": TEXT,
                        "original_issue_date": TEXT,
                    },
                    ["issue_date", "due_date", "currency", "seller", "client", "totals"],
                ),
                "lines": array(engine_line),
            }
        ),
        "Recurrence": RECURRENCE,
        "Recurrences": array({"$ref": "#/components/schemas/Recurrence"}),
        "InvoicingAction": object_schema(
            {
                "recurrences": array({"$ref": "#/components/schemas/Recurrence"}),
                "ok": BOOL,
                "message": TEXT,
                "id": nullable,
                "version": VERSION,
                "code": TEXT,
                "client": profile,
            },
            ["ok", "message", "id"],
        ),
        "InvoicingWorkspace": object_schema(
            {
                "co": TEXT,
                "live": BOOL,
                "today": TEXT,
                "seller": {"anyOf": [seller, {"type": "null"}]},
                "seller_prefill": object_schema(seller["properties"] | {"vat_payer": {"type": ["boolean", "null"]}}),
                "auto_upload": BOOL,
                "upload_after_days": {
                    "type": ["integer", "null"],
                    "description": "Days after issue the scheduled tick sends a new invoice to SPV (the firm's ANAF "
                    "integration, 0-5); null when it sends none on its own (auto-upload off, Sandbox, no ANAF).",
                },
                "series": array(
                    object_schema(
                        {
                            "code": TEXT,
                            "next_number": {"type": "integer"},
                            "issued": {"type": "integer"},
                            "imported": {"type": ["integer", "null"]},
                        }
                    )
                ),
                "series_new": {
                    "anyOf": [object_schema({"code": TEXT, "next_number": {"type": "integer"}}), {"type": "null"}]
                },
                "due_days": {"type": "integer"},
                "clients": array(
                    profile
                    | {
                        "properties": profile["properties"]
                        | {
                            "id": TEXT,
                            "last_used": nullable,
                            "last_invoice": {
                                "anyOf": [
                                    object_schema(
                                        {"due_days": {"type": "integer"}, "currency": TEXT, "lines": array(LINE)}
                                    ),
                                    {"type": "null"},
                                ]
                            },
                        }
                    }
                ),
                "line_description_max": {"type": "integer"},
                "accounts": array(object_schema(strings("iban", "currency", "bank", "bank_name") | {"pays": BOOL})),
                "invoice_accounts": {
                    "anyOf": [
                        object_schema(
                            {
                                "accounts": array(object_schema(strings("iban", "currency", "bank_name"))),
                                "default": {"type": "object", "additionalProperties": TEXT},
                            }
                        ),
                        {"type": "null"},
                    ]
                },
                "recurrences": array({"$ref": "#/components/schemas/Recurrence"}),
                "drafts": array(draft),
                "products": array({"$ref": "#/components/schemas/Product"}),
                "units": array(object_schema(strings("code", "label"))),
                "counties": array(object_schema(strings("county", "label") | {"city": nullable})),
                "countries": array(TEXT),
                "vat": object_schema(
                    {
                        "rates": array(TEXT),
                        "default": TEXT,
                        "categories": array(object_schema(strings("code", "label"))),
                        "exemptions": array(object_schema(strings("code", "label"))),
                        "choices": array(
                            object_schema(
                                strings("key", "label", "category", "rate", "exemption") | {"needs_delivery_date": BOOL}
                            )
                        ),
                    }
                ),
                "required": object_schema(
                    {
                        k: array(TEXT)
                        for k in (
                            "seller",
                            "seller_if_ro",
                            "seller_if_vat_payer",
                            "client_identity_any_of",
                            "client_if_vat_payer",
                            "client_if_k",
                            "client_if_ro_vat_registered",
                            "draft_if_advance_payment_date",
                        )
                    }
                    | {"client": object_schema({"RO": array(TEXT), "other": array(TEXT)})}
                ),
            }
        ),
    }


def validate(schema: dict, value: object) -> None:
    expected = schema.get("type", "string")
    if isinstance(expected, list):
        if value is None and "null" in expected:
            return
        expected = next(t for t in expected if t != "null")
    valid = {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }
    if not valid.get(expected, False) or ("enum" in schema and value not in schema["enum"]):
        raise ValueError("An argument has the wrong type or value.")
    if expected == "string" and (
        ("pattern" in schema and not re.fullmatch(schema["pattern"], value))
        or ("maxLength" in schema and len(value) > schema["maxLength"])
    ):
        raise ValueError("An argument has an invalid format or length.")
    if expected == "integer" and "minimum" in schema and value < schema["minimum"]:
        raise ValueError("An argument is below the minimum.")
    if expected == "object":
        fields = schema["properties"]
        if set(value) - set(fields) or set(schema.get("required", [])) - set(value):
            raise ValueError("Supply only the documented fields.")
        for key, required in schema.get("dependentRequired", {}).items():
            if key in value and set(required) - set(value):
                raise ValueError("Supply the fields required by this argument.")
        for key, item in value.items():
            validate(fields[key], item)
    if expected == "array":
        for item in value:
            validate(schema["items"], item)
