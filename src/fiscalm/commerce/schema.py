"""Order schemas for the published API."""

from __future__ import annotations


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return dict(
        type="object",
        properties=properties,
        required=list(properties) if required is None else required,
        additionalProperties=False,
    )


S = {"type": "string"}
D = dict(type="string", pattern=r"^-?[0-9]+([.,][0-9]{1,12})?$")
AWB = obj(
    dict(
        courier=S,
        number=S,
        **{k: dict(type="string", format="date") for k in ("picked_up_at", "delivered_at", "returned_at")},
    ),
    ["courier", "number"],
)
ORDER_BODY = obj(
    dict(
        channel=S,
        external_id=S,
        placed_at=dict(type="string", format="date"),
        currency=S,
        prices_include_vat={"type": "boolean"},
        buyer=obj(
            dict(
                type={"enum": ["person", "company"]},
                name=S,
                cui=S,
                email=S,
                address=obj(
                    dict(street=S, city=S, county=S, country=S, postal=S), ["street", "city", "county", "country"]
                ),
            ),
            ["type", "name", "address"],
        ),
        lines=dict(
            type="array",
            minItems=1,
            maxItems=1000,
            items=obj(
                dict(sku=S, description=S, quantity=D, unit=S, unit_price=D, vat_rate=D),
                ["description", "quantity", "unit", "unit_price", "vat_rate"],
            ),
        ),
        payment=obj(
            dict(method={"enum": ["card", "cod", "transfer", "other"]}, processor=S, paid={"type": "boolean"}),
            ["method", "paid"],
        ),
        awb=AWB,
    ),
    ["channel", "external_id", "placed_at", "currency", "prices_include_vat", "buyer", "lines", "payment"],
)


def schemas() -> dict:
    nullable = {"type": ["string", "null"]}
    action = obj(
        dict(
            ok={"type": "boolean"},
            message=S,
            id=S,
            draft_id=nullable,
            invoice_id=nullable,
            status=S,
            order_status=S,
            refund_draft_id=S,
        ),
        ["ok", "message", "id", "draft_id", "invoice_id", "status"],
    )
    order = obj(
        dict(
            id=S,
            company=S,
            channel=S,
            external_id=S,
            placed_at=S,
            body={"type": "object"},
            draft_id=nullable,
            client_id=nullable,
            draft_version={"type": ["integer", "null"]},
            reason={"type": ["object", "null"]},
            invoice_summary={"type": ["object", "null"]},
            status=S,
            awb={"anyOf": [AWB, {"type": "null"}]},
            invoice_id=nullable,
            spv={"type": ["object", "null"]},
            links=obj(dict(draft=nullable, invoice=nullable, pdf=nullable)),
        )
    )
    sheet = obj(
        order["properties"]
        | dict(
            sheet_id=S,
            channel_name=nullable,
            company_name=S,
            delivery=obj(dict(picked_up_at=nullable, returned_at=nullable)),
            cod_at_courier={"type": "boolean"},
            can_discard={"type": "boolean"},
            can_cancel={"type": "boolean"},
        )
    )
    task = obj(
        dict(
            kind={"const": "order_refused"},
            id=S,
            company=S,
            order_id=S,
            sheet={"const": "ord"},
            reason=obj(dict(code=S, message=S)),
            label=S,
            action={"const": "order-discard"},
        )
    )
    return dict(
        OrderSheet=sheet,
        OrderTask=task,
        Order=order,
        OrderAction=action,
        OrderPage=obj(dict(data={"type": "array", "items": order}, next_cursor=nullable)),
    )
