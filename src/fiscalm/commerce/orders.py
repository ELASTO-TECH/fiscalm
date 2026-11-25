"""Company-scoped shop orders; intake and invoice drafts commit together."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import date
from uuid import uuid4

from fiscalm import bnr, integrations
from fiscalm.api.responses import Error, fail, page
from fiscalm.identity import firm_name
from fiscalm.invoicing import engine as inv
from fiscalm.ledger import Ledger, is_live

LIMIT = 256 * 1024


def fields(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        fail(400, "invalid_order", "Use the documented order fields.")
    return value


def text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        fail(400, "invalid_order", "Order text is missing, too long or contains controls.")
    return value


def awb(value: object) -> dict:
    row = fields(value, {"courier", "number"}, {"picked_up_at", "delivered_at", "returned_at"})
    result = {k: text(v, 100) for k, v in row.items()}
    try:
        for key in ("picked_up_at", "delivered_at", "returned_at"):
            if key in result and date.fromisoformat(result[key]).isoformat() != result[key]:
                raise ValueError()
        if any(k in result for k in ("delivered_at", "returned_at")) and not result.get("picked_up_at"):
            raise ValueError()
        if any(result[k] < result["picked_up_at"] for k in ("delivered_at", "returned_at") if k in result):
            raise ValueError()
    except ValueError:
        fail(400, "invalid_order", "AWB lifecycle requires ISO dates in pickup order.")
    return result


def validate(body: object) -> dict:
    row = fields(
        body,
        {"channel", "external_id", "placed_at", "currency", "prices_include_vat", "buyer", "lines", "payment"},
        {"awb"},
    )
    for key in ("channel", "external_id", "placed_at", "currency"):
        text(row[key])
    if type(row["prices_include_vat"]) is not bool or not re.fullmatch("[A-Z]{3}", row["currency"]):
        fail(400, "invalid_order", "Use a currency code and boolean prices_include_vat.")
    inv._day(row["placed_at"])
    buyer = fields(row["buyer"], {"type", "name", "address"}, {"cui", "email"})
    if not isinstance(buyer["type"], str) or buyer["type"] not in ("person", "company"):
        fail(400, "invalid_order", "Buyer type must be person or company.")
    for key in ("name", "cui", "email"):
        if key in buyer:
            text(buyer[key])
    address = fields(buyer["address"], {"street", "city", "county", "country"}, {"postal"})
    for v in address.values():
        text(v)
    if buyer["type"] == "company" and not buyer.get("cui"):
        fail(400, "invalid_order", "Company buyers require a fiscal identifier.")
    payment = fields(row["payment"], {"method", "paid"}, {"processor"})
    if (
        not isinstance(payment["method"], str)
        or payment["method"] not in ("card", "cod", "transfer", "other")
        or type(payment["paid"]) is not bool
    ):
        fail(400, "invalid_order", "Use the documented payment method and boolean paid.")
    if "processor" in payment:
        text(payment["processor"])
    if "awb" in row:
        awb(row["awb"])
    if not isinstance(row["lines"], list) or not 1 <= len(row["lines"]) <= 1000:
        fail(400, "invalid_order", "Supply between 1 and 1000 order lines.")
    for line in row["lines"]:
        fields(line, {"description", "quantity", "unit", "unit_price", "vat_rate"}, {"sku"})
        for v in line.values():
            text(v)
        for key in ("quantity", "unit_price", "vat_rate"):
            inv.decimal(line[key])
        if inv.decimal(line["vat_rate"]) not in (0, 11, 21):
            fail(400, "invalid_order", "Use VAT rates 0, 11 or 21.")
    return row


# Legal forms by the saved firm's country: in Romania 'Spa' is a word of the name, in Italy it is S.p.A.
RO_LEGAL_FORMS = frozenset({"srl", "srld", "sa", "pfa", "ii", "if", "snc", "scs", "sca"})
LEGAL_FORMS = frozenset(
    {"gmbh", "ag", "ug", "kg", "ohg", "ltd", "llc", "inc", "plc", "sas", "sarl", "bv", "nv", "spa", "srl", "sl", "sa"}
)


def _name_parts(name: str, country: str) -> tuple[list[str], str]:
    """The words of a firm name and its trailing legal form ('' when it has none)."""
    words = [w for w in firm_name(name).split("-") if w]
    forms = RO_LEGAL_FORMS if country == "RO" else LEGAL_FORMS
    return (words[:-1], words[-1]) if len(words) > 1 and words[-1] in forms else (words, "")


def same_firm(saved: dict, buyer: dict) -> bool:
    """A company buyer is the saved company under its tax id when the names agree: case, accents and punctuation
    don't count, a legal form counts only when both names write one. A person or a private client never matches."""
    if buyer.get("private") or saved.get("private"):
        return False
    country = saved.get("country", "")
    (words, form), (typed, typed_form) = _name_parts(saved["name"], country), _name_parts(buyer["name"], country)
    return bool(words) and words == typed and (form == typed_form or not form or not typed_form)


def channel(L: Ledger, co: str, iid: str) -> integrations.Instance:
    found = next((x for x in integrations.enabled(L, "shop") if x.id == iid and x.config["company"] == co), None)
    if not found:
        fail(409, "invalid_channel", "Choose an enabled shop channel for this company.")
    return found


def _awb_delivery(L: Ledger, oid: str, value: dict) -> None:
    if "picked_up_at" in value or "returned_at" in value:
        L.db.execute(
            "INSERT INTO commerce_delivery(order_id,picked_up_at,returned_at) VALUES(?,?,?) "
            "ON CONFLICT(order_id) DO UPDATE SET picked_up_at=excluded.picked_up_at,returned_at=excluded.returned_at",
            (oid, value.get("picked_up_at"), value.get("returned_at")),
        )


def get(L: Ledger, co: str, oid: str) -> dict:
    row = L.db.execute("SELECT * FROM commerce_orders WHERE id=? AND company=?", (oid, co)).fetchone()
    if not row:
        fail(404, "not_found", "The order does not exist in this company.")
    result = dict(row)
    result["body"] = json.loads(result["body"])
    result["awb"] = json.loads(result["awb"]) if result["awb"] else None
    exists = L.db.execute("SELECT 1 FROM invoices WHERE id=? AND company=?", (row["draft_id"], co)).fetchone()
    invoice = inv.invoice(L, row["draft_id"], company=co) if exists and row["status"] != "cancelled" else None
    status = (
        row["status"]
        if row["status"] in ("refused", "cancelled")
        else ("cancelled" if invoice is None else "draft" if invoice["status"] == "draft" else "issued")
    )
    result["reason"] = json.loads(row["reason"]) if row["reason"] else None
    result["invoice_summary"] = (
        {"currency": invoice["data"]["currency"], "totals": invoice["data"]["totals"]} if invoice else None
    )
    if not row["draft_id"]:
        result.update(draft_id=None, client_id=None, draft_version=None)
    result.update(status=status, invoice_id=row["draft_id"] if status == "issued" else None)
    from fiscalm.invoicing.spv import submission

    state = submission(L, row["draft_id"]) if result["invoice_id"] else None
    result["spv"] = {k: state[k] for k in ("status", "errors")} if state else None
    result["links"] = {
        "draft": row["draft_id"] if status == "draft" else None,
        "invoice": result["invoice_id"],
        "pdf": result["invoice_id"],
    }
    return result


def response(order: dict) -> dict:
    return {k: order[k] for k in ("id", "draft_id", "invoice_id", "status")} | {"ok": True, "message": ""}


def _intake(
    L: Ledger,
    co: str,
    body: object,
    today: str,
    authorize: Callable[[], None] = lambda: None,
    *,
    auto_issue: bool = True,
) -> dict:
    body = validate(body)
    if any(body.get("awb", {}).get(k, "") > today for k in ("picked_up_at", "delivered_at", "returned_at")):
        fail(400, "invalid_order", "AWB lifecycle cannot be in the future.")
    if body["placed_at"] > today:
        fail(400, "invalid_order", "Order date cannot be in the future.")
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    prior = L.db.execute(
        "SELECT id,body FROM commerce_orders WHERE company=? AND channel=? AND external_id=?",
        (co, body["channel"], body["external_id"]),
    ).fetchone()
    prior_status = get(L, co, prior["id"])["status"] if prior else None
    needs_rate = (
        not prior or prior_status == "refused" or (json.loads(prior["body"]) != body and prior_status == "draft")
    )
    rates = (
        bnr.rate(L, body["currency"], min(today, body["placed_at"]), today=today)
        if body["currency"] != "RON" and needs_rate
        else None
    )
    with L.tx() as db:
        authorize()
        shop = channel(L, co, body["channel"])
        old = db.execute(
            "SELECT id FROM commerce_orders WHERE company=? AND channel=? AND external_id=?",
            (co, body["channel"], body["external_id"]),
        ).fetchone()
        order = get(L, co, old["id"]) if old else None
        if order and order["status"] != "refused" and order["body"] == body:
            return response(order)
        if order and order["status"] not in ("draft", "refused"):
            fail(409, "order_changed", "Only an unissued order can be changed.")
        if order and order["status"] == "draft":
            inv._draft_version(L, order["draft_id"], co, order["draft_version"])
        buyer, address = body["buyer"], body["buyer"]["address"]
        profile = {k: v for k, v in buyer.items() if k in ("name", "cui", "email")}
        profile |= {k: v for k, v in address.items() if k != "postal"}
        profile |= dict(postal_code=address.get("postal", ""), private=buyer["type"] == "person", vat_payer=False)
        normalized = inv._party(profile)
        identity = inv._identity(normalized)
        matches = [
            r
            for r in db.execute("SELECT id,data FROM invoice_clients WHERE company=?", (co,))
            if identity and inv._identity(json.loads(r["data"])) == identity
        ]
        if matches:
            if len(matches) != 1:
                fail(409, "client_conflict", "Resolve duplicate saved client identities first.")
            if not same_firm(json.loads(matches[0]["data"]), normalized):
                fail(
                    409,
                    "client_conflict",
                    "Shop buyer names another firm than the saved client. Review the client first.",
                )
            # Address and email drift is accepted: the invoice carries the saved client, the order keeps what was typed.
            cid = matches[0]["id"]
        else:
            # Private clients belong to this order only; never edit another draft's client.
            cid = inv.save_client(L, co, profile)

        seller = inv._seller(L, co)
        lines = []
        for source in body["lines"]:
            line = {k: v for k, v in source.items() if k != "sku"}
            rate = inv.decimal(line["vat_rate"])
            if not seller["vat_payer"] and rate != 0:
                fail(400, "invalid_order", "A non-VAT seller requires zero VAT order lines.")
            if seller["vat_payer"] and rate == 0:
                fail(
                    400,
                    "invalid_order",
                    "A zero-VAT line requires an explicit tax treatment; review it before invoicing.",
                )
            line["vat_category"] = "S" if rate else "O"
            if not seller["vat_payer"]:
                line["exemption_reason"] = "Scutire conform art. 310"
            # UBL prices stay nonnegative; discounts are negative quantities.
            if inv.decimal(line["unit_price"]) < 0:
                line["quantity"] = format(-inv.decimal(line["quantity"]), "f")
                line["unit_price"] = format(-inv.decimal(line["unit_price"]), "f")
            if body["prices_include_vat"]:
                line["gross_unit_price"] = line["unit_price"]
            lines.append(line)
        data = dict(
            issue_date=today, due_date=today, delivery_date=body["placed_at"], currency=body["currency"], lines=lines
        )
        if rates:
            data["exchange_rate"], data["exchange_date"] = rates
        did = inv.save_draft(
            L,
            co,
            shop.config["series"],
            cid,
            data,
            order["draft_id"] if order else None,
            version=order["draft_version"] if order else None,
        )
        version = inv.invoice(L, did, company=co)["version"]
        if order and body.get("awb") and body["awb"] != order["awb"]:
            db.execute("DELETE FROM commerce_delivery WHERE order_id=?", (order["id"],))
        oid = order["id"] if order else uuid4().hex
        db.execute(
            "INSERT INTO commerce_orders(id,company,channel,external_id,placed_at,body,draft_id,client_id,draft_version,status,awb) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "body=excluded.body,client_id=excluded.client_id,draft_id=excluded.draft_id,status=excluded.status,reason=NULL,placed_at=excluded.placed_at,draft_version=excluded.draft_version,awb=COALESCE(excluded.awb,commerce_orders.awb)",
            (
                oid,
                co,
                body["channel"],
                body["external_id"],
                body["placed_at"],
                canonical,
                did,
                cid,
                version,
                "draft",
                json.dumps(body.get("awb")) if body.get("awb") else None,
            ),
        )
        if body.get("awb"):
            _awb_delivery(L, oid, body["awb"])
    message = ""
    if auto_issue and shop.config["auto_issue"] == "true" and is_live():
        try:
            inv.issue(L, did, today, company=co, version=version, authorize=authorize)
        except inv.InvoiceError:
            message = "Order saved as a draft; automatic issue was refused. Review the invoice."
    return response(get(L, co, oid)) | {"message": message}


def dispatch(
    L: Ledger,
    co: str,
    resource: str,
    params: dict,
    query: dict,
    body: object,
    today: str,
    authorize: Callable[[], None] = lambda: None,
) -> object:
    try:
        if resource == "orders_create":
            return intake(L, co, body, today, authorize)
        if resource == "orders_list":
            if "month" in query and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", query["month"]):
                fail(400, "invalid_month", "Use YYYY-MM.")
            rows = [get(L, co, r[0]) for r in L.db.execute("SELECT id FROM commerce_orders WHERE company=?", (co,))]
            rows = [
                r
                for r in rows
                if all(
                    not query.get(k) or (r["placed_at"][:7] if k == "month" else r[k]) == query[k]
                    for k in ("status", "channel", "month")
                )
            ]
            return page(rows, "id", query)
        if resource in ("orders_awb", "orders_cancel", "orders_refund", "orders_discard", "orders_delivery"):
            return command(L, co, params["id"], resource, body, today, authorize)
        return get(L, co, params["id"])
    except Error as exc:
        if resource == "orders_create" and exc.status in (400, 409):
            from fiscalm.commerce.webhooks import note_refusal as refused

            refused(L, co, body, exc.code)
        raise
    except inv.DraftConflict as exc:
        if resource == "orders_create":
            from fiscalm.commerce.webhooks import note_refusal as refused

            refused(L, co, body, exc.code)
        fail(409, exc.code, str(exc))
    except bnr.RateError as exc:
        error = Error(409, "rate_unavailable", "The BNR rate is unavailable. Retry later.")
        error.order_id = getattr(exc, "order_id", None)
        raise error from exc
    except inv.InvoiceError as exc:
        if resource == "orders_create":
            from fiscalm.commerce.webhooks import note_refusal as refused

            refused(L, co, body, "invalid_order")
        error = Error(400, "invalid_order", str(exc))
        error.order_id = getattr(exc, "order_id", None)
        raise error from exc


def command(
    L: Ledger, co: str, oid: str, operation: str, body: object, today: str, authorize: Callable[[], None] = lambda: None
) -> dict:
    fields(
        body,
        {"courier", "number"}
        if operation == "orders_awb"
        else {"picked_up_at", "returned_at"}
        if operation == "orders_delivery"
        else set(),
        {"lines"}
        if operation == "orders_refund"
        else {"picked_up_at", "delivered_at", "returned_at"}
        if operation == "orders_awb"
        else set(),
    )
    if (
        operation == "orders_refund"
        and "lines" in body
        and (not isinstance(body["lines"], list) or any(type(n) is not int for n in body["lines"]))
    ):
        fail(400, "invalid_order", "Refund lines must be integer positions.")
    with L.tx() as db:
        authorize()
        order = get(L, co, oid)
        if operation == "orders_discard":
            if order["status"] == "cancelled" and order["reason"]:
                return response(order)
            if order["status"] != "refused":
                fail(409, "order_changed", "Only a refused order can be abandoned.")
            db.execute(
                "UPDATE commerce_orders SET status='cancelled' WHERE id=? AND company=? AND status='refused'", (oid, co)
            )
        elif operation == "orders_delivery":
            if not order["awb"] or order["status"] in ("cancelled", "refused"):
                fail(409, "order_changed", "Delivery facts require an active order with an AWB.")
            for value in body.values():
                if value is not None and (not isinstance(value, str) or inv._day(value) > today):
                    fail(400, "invalid_order", "Delivery dates must be ISO dates no later than today.")
            if body["picked_up_at"] and body["picked_up_at"] < order["placed_at"]:
                fail(400, "invalid_order", "Pickup cannot precede the order date.")
            if body["returned_at"] and not body["picked_up_at"]:
                fail(400, "invalid_order", "A return requires a pickup date.")
            if body["returned_at"] and body["picked_up_at"] and body["returned_at"] < body["picked_up_at"]:
                fail(400, "invalid_order", "Return cannot precede pickup.")
            db.execute(
                "INSERT INTO commerce_delivery VALUES(?,?,?) ON CONFLICT(order_id) DO UPDATE SET picked_up_at=excluded.picked_up_at,returned_at=excluded.returned_at",
                (oid, body["picked_up_at"], body["returned_at"]),
            )
        elif operation == "orders_awb":
            if order["status"] in ("cancelled", "refused"):
                fail(409, "order_changed", "Only a draft or issued order can receive an AWB.")
            value = awb(body)
            if any(value[k] > today for k in ("picked_up_at", "delivered_at", "returned_at") if k in value):
                fail(400, "invalid_order", "AWB lifecycle cannot be in the future.")
            if value != order["awb"]:
                db.execute("DELETE FROM commerce_delivery WHERE order_id=?", (oid,))
            db.execute("UPDATE commerce_orders SET awb=? WHERE id=? AND company=?", (json.dumps(awb(body)), oid, co))
            _awb_delivery(L, oid, value)
        elif operation == "orders_cancel":
            if order["status"] == "cancelled":
                return response(order)
            if order["status"] != "draft":
                fail(409, "order_changed", "Only unissued orders can be cancelled.")
            inv.delete_draft(L, order["draft_id"], company=co, version=order["draft_version"])
            db.execute("UPDATE commerce_orders SET status='cancelled' WHERE id=? AND company=?", (oid, co))
        else:
            if order["status"] != "issued":
                fail(409, "order_changed", "Refunds require an issued original invoice.")
            result = inv.storno(
                L, order["invoice_id"], today, "Retur comandă", body.get("lines"), company=co, draft=True
            )
            return response(order) | {
                "draft_id": result["id"],
                "invoice_id": None,
                "status": "draft",
                "order_status": order["status"],
                "refund_draft_id": result["id"],
            }
        return response(get(L, co, oid))


def intake(
    L: Ledger,
    co: str,
    body: object,
    today: str,
    authorize: Callable[[], None] = lambda: None,
    *,
    auto_issue: bool = True,
) -> dict:
    try:
        return _intake(L, co, body, today, authorize, auto_issue=auto_issue)
    except bnr.RateError:
        raise
    except (Error, inv.InvoiceError) as exc:
        code = (
            exc.code
            if isinstance(exc, Error)
            else "rate_unavailable"
            if isinstance(exc, bnr.RateError)
            else getattr(exc, "code", "invalid_order")
        )
        if not isinstance(exc, Error) or exc.status in (400, 409):
            try:
                oid = keep_refusal(
                    L, co, body, code, str(exc) if not isinstance(exc, Error) else exc.message, today, authorize
                )
            except sqlite3.Error:
                oid = None
            if oid:
                exc.order_id = oid
        raise


def keep_refusal(
    L: Ledger, co: str, body: object, code: str, message: str, today: str, authorize: Callable[[], None]
) -> str | None:
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("channel"), str)
        or not isinstance(body.get("external_id"), str)
    ):
        return None
    try:
        text(body["external_id"])
    except Error:
        return None
    if len(json.dumps(body, ensure_ascii=False).encode()) > LIMIT:
        return None
    placed_at = today
    with contextlib.suppress(inv.InvoiceError, TypeError):
        placed_at = inv._day(body.get("placed_at"))
    with L.tx() as db:
        authorize()
        try:
            channel(L, co, body["channel"])
        except Error:
            return None
        old = db.execute(
            "SELECT id,status FROM commerce_orders WHERE company=? AND channel=? AND external_id=?",
            (co, body["channel"], body["external_id"]),
        ).fetchone()
        if old and old["status"] != "refused":
            return None
        oid = old["id"] if old else uuid4().hex
        # Failed intake rolled back its client/draft first; empty internal references are never projected.
        db.execute(
            "INSERT INTO commerce_orders(id,company,channel,external_id,placed_at,body,draft_id,client_id,draft_version,status,reason) VALUES(?,?,?,?,?,?,'','',0,'refused',?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,reason=excluded.reason,placed_at=excluded.placed_at",
            (
                oid,
                co,
                body["channel"],
                body["external_id"],
                placed_at,
                json.dumps(json.loads(json.dumps(body), parse_constant=lambda _: None), ensure_ascii=False),
                json.dumps(dict(code=code, message=message)),
            ),
        )
        return oid
