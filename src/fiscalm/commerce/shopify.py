"""Shopify custom-app intake. Destinations and credentials come only from config."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import time
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from decimal import Decimal

from fiscalm import integrations
from fiscalm.bank import statements
from fiscalm.commerce import orders as commerce
from fiscalm.commerce.webhooks import NoRedirect
from fiscalm.invoicing import engine as invoicing
from fiscalm.ledger import Ledger, is_live
from fiscalm.settlements import engine as settlements
from fiscalm.settlements.reports import money as report_money

VERSION = "2026-10"
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com")
TOPICS = {"orders/create", "orders/updated", "refunds/create", "fulfillments/create"}
MONEY = "{shopMoney{amount currencyCode}}"
PAGE = "pageInfo{hasNextPage endCursor}"
LINE = (
    "id name sku quantity isGiftCard originalUnitPriceSet"
    + MONEY
    + " discountAllocations{allocatedAmountSet"
    + MONEY
    + "} taxLines{rate priceSet"
    + MONEY
    + "}"
)
SHIPPING = "id title discountedPriceSet" + MONEY + " taxLines{rate priceSet" + MONEY + "}"
ORDER = (
    """id updatedAt createdAt cancelledAt edited taxesIncluded currencyCode email displayFinancialStatus
 paymentGatewayNames billingAddress{name company address1 address2 city provinceCode countryCodeV2 zip}
 purchasingEntity{... on PurchasingCompany{company{name} location{taxRegistrationId}}}
 fulfillments(first:250){status trackingInfo{company number}}
 refunds{id totalRefundedSet"""
    + MONEY
    + """}
 originalTotalPriceSet"""
    + MONEY
)


class ShopifyError(ValueError):
    """Safe diagnostic: never includes remote text, credentials or buyer details."""


def channel(L: Ledger, iid: str) -> integrations.Instance:
    found = next(
        (s for s in integrations.enabled(L, "shop") if s.id == iid and s.config["platform"] == "shopify"), None
    )
    if not found or not DOMAIN.fullmatch(found.config["shop_domain"]):
        raise ShopifyError("Shopify channel is disabled or invalid.")
    return found


def access_token(shop: integrations.Instance) -> str:
    """Custom-app credential boundary; a hosted OAuth token provider can replace it."""
    return shop.secret("admin_token")


def graphql(shop: integrations.Instance, query: str, variables: dict) -> dict:
    if not DOMAIN.fullmatch(shop.config["shop_domain"]):
        raise ShopifyError("Invalid Shopify domain.")
    url = f"https://{shop.config['shop_domain']}/admin/api/{VERSION}/graphql.json"
    req = urllib.request.Request(
        url,
        data=json.dumps(dict(query=query, variables=variables)).encode(),
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": access_token(shop)},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ShopifyError("Shopify response exceeds limit.")
            result = json.loads(raw, parse_float=Decimal)
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise ShopifyError("Shopify rejected the query; check app scopes and API version.")
        return result["data"]
    except ShopifyError:
        raise
    except Exception:
        raise ShopifyError("Shopify request failed; check credentials and retry.") from None


def connection(fetch: Callable[[str | None], dict]) -> list[dict]:
    rows, seen, cursor = [], set(), None
    for _ in range(100):
        part = fetch(cursor)
        rows.extend(part["nodes"])
        if not part["pageInfo"]["hasNextPage"]:
            return rows
        cursor = part["pageInfo"]["endCursor"]
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise ShopifyError("Shopify pagination did not finish; nothing was imported.")


def read_order(shop: integrations.Instance, oid: str) -> dict:
    if not re.fullmatch(r"gid://shopify/Order/[0-9]+", oid):
        raise ShopifyError("Invalid Shopify order ID.")
    row = graphql(shop, "query($id:ID!){order(id:$id){" + ORDER + "}}", {"id": oid})["order"]
    if not row or row["id"] != oid:
        raise ShopifyError("Shopify order is unavailable.")
    for key, fields in (("lineItems", LINE), ("shippingLines", SHIPPING)):

        def fetch(cursor: str | None, fields=fields, key=key) -> dict:
            return graphql(
                shop,
                "query($id:ID!,$cursor:String){order(id:$id){"
                + key
                + "(first:100,after:$cursor){nodes{"
                + fields
                + "}"
                + PAGE
                + "}}}",
                {"id": oid, "cursor": cursor},
            )["order"][key]

        row[key] = connection(fetch)
    for refund in row["refunds"]:
        refund["items"] = connection(
            lambda cursor, refund=refund: graphql(
                shop,
                "query($id:ID!,$cursor:String){node(id:$id){... on Refund{refundLineItems(first:100,after:$cursor){nodes{quantity lineItem{id}}"
                + PAGE
                + "}}}}",
                {"id": refund["id"], "cursor": cursor},
            )["node"]["refundLineItems"]
        )
    # Refuse a torn snapshot across pagination rather than invoice mixed revisions.
    latest = graphql(shop, "query($id:ID!){order(id:$id){updatedAt}}", {"id": oid})["order"]
    if latest["updatedAt"] != row["updatedAt"]:
        raise ShopifyError("Shopify order changed during read; retry.")
    return row


def money(value: dict, currency: str) -> Decimal:
    m = value["shopMoney"]
    amount = Decimal(str(m["amount"]))
    if m["currencyCode"] != currency or not amount.is_finite():
        raise ShopifyError("Inconsistent shop currency or amount.")
    return amount


def mapping(shop: integrations.Instance, row: dict) -> dict:
    if row["edited"]:
        raise ShopifyError("Edited Shopify orders require manual review.")
    currency = row["currencyCode"]
    address = row.get("billingAddress") or {}
    company = row.get("purchasingEntity") or {}
    vat = (company.get("location") or {}).get("taxRegistrationId")
    name = (company.get("company") or {}).get("name") or address.get("company")
    if name and not vat:
        raise ShopifyError("Company buyer needs a VAT identifier; review Shopify B2B details.")
    buyer = dict(
        type="company" if name else "person",
        name=name or address.get("name"),
        address=dict(
            street=" ".join(filter(None, [address.get("address1"), address.get("address2")])),
            city=address.get("city"),
            county=address.get("provinceCode"),
            country=address.get("countryCodeV2"),
        ),
    )
    if address.get("countryCodeV2") == "RO" and address.get("provinceCode"):
        buyer["address"]["county"] = "RO-" + address["provinceCode"].removeprefix("RO-")
    if vat:
        buyer["cui"] = vat
    if row.get("email"):
        buyer["email"] = row["email"]
    if address.get("zip"):
        buyer["address"]["postal"] = address["zip"]
    lines = []
    for source in row["lineItems"] + row["shippingLines"]:
        if source.get("isGiftCard"):
            raise ShopifyError("Gift card sales require explicit voucher tax treatment; review manually.")
        taxes = source["taxLines"]
        if len(taxes) > 1:
            raise ShopifyError("Multiple taxes on one Shopify line require review.")
        rate = Decimal(str(taxes[0]["rate"])) * 100 if taxes else Decimal(0)
        qty = source.get("quantity", 1)
        if type(qty) is not int or qty <= 0:
            raise ShopifyError("Invalid Shopify quantity.")
        price = money(source.get("originalUnitPriceSet", source.get("discountedPriceSet")), currency)
        lines.append(
            dict(
                description=source.get("name", source.get("title")),
                quantity=str(qty),
                unit="H87",
                unit_price=str(price),
                vat_rate=str(rate),
            )
        )
        discount = sum(
            (money(x["allocatedAmountSet"], currency) for x in source.get("discountAllocations", [])), Decimal(0)
        )
        if price < 0 or discount < 0 or discount > price * qty:
            raise ShopifyError("Invalid Shopify discount or price.")
        if discount:
            lines.append(
                dict(description="Discount", quantity="1", unit="H87", unit_price=str(-discount), vat_rate=str(rate))
            )
    gateways = row["paymentGatewayNames"]
    method = "cod" if any(g.lower() in ("cash on delivery (cod)", "cash on delivery") for g in gateways) else "other"
    if len(gateways) == 1 and any(g.lower() in ("shopify_payments", "shopify payments") for g in gateways):
        method = "card"
    return dict(
        channel=shop.id,
        external_id=row["id"].rsplit("/", 1)[1],
        placed_at=row["createdAt"][:10],
        currency=currency,
        prices_include_vat=row["taxesIncluded"],
        buyer=buyer,
        lines=lines,
        payment=dict(
            method=method, paid=row["displayFinancialStatus"] == "PAID", processor=", ".join(gateways) or "Shopify"
        ),
    )


def ingest(L: Ledger, iid: str, row: dict, today: str) -> dict:
    shop = channel(L, iid)
    co = shop.config["company"]
    body = mapping(shop, row)
    key = f"shopify:revision:{iid}:{body['external_id']}"
    with L.tx():
        if channel(L, iid).config != shop.config:
            raise ShopifyError("Shopify configuration changed; retry.")
        previous = L.get_meta(key)
        old = L.db.execute(
            "SELECT id FROM commerce_orders WHERE company=? AND channel=? AND external_id=?",
            (co, iid, body["external_id"]),
        ).fetchone()
        if previous and previous > row["updatedAt"]:
            return commerce.response(commerce.get(L, co, old[0]))
        order = commerce.get(L, co, old[0]) if old else None
        new_order = order is None
        _check_issued_order(order, body)
        if not order or order["status"] == "draft":
            order = _intake_checked(L, co, body, row, today)
        if row["cancelledAt"] and order["status"] == "draft":
            commerce.command(L, co, order["id"], "orders_cancel", {}, today)
        _tracking(L, co, row, order, today)
        if row["refunds"] and order["status"] == "issued":
            _refunds(L, co, iid, body, row, order, today)
        L.set_meta(key, row["updatedAt"])
        order = commerce.get(L, co, order["id"])
        if (
            new_order
            and body["placed_at"] == today
            and order["status"] == "draft"
            and not row["cancelledAt"]
            and not row["refunds"]
            and is_live()
            and shop.config["auto_issue"] == "true"
        ):
            # Keep the durable draft for manual review; never auto-retry issue.
            with suppress(invoicing.InvoiceError):
                invoicing.issue(L, order["draft_id"], today, company=co, version=order["draft_version"])
        return commerce.response(commerce.get(L, co, order["id"]))


def _check_issued_order(order: dict | None, body: dict) -> None:
    if (
        order
        and order["status"] == "issued"
        and any(order["body"][k] != body[k] for k in ("buyer", "lines", "currency", "prices_include_vat"))
    ):
        raise ShopifyError("Issued Shopify order changed; review before creating corrections.")


def _intake_checked(L: Ledger, co: str, body: dict, row: dict, today: str) -> dict:
    # Disable auto issue until the mapped total has passed the engine calculation.
    commerce.validate(body)
    result = commerce.intake(L, co, body, today, auto_issue=False)
    order = commerce.get(L, co, result["id"])
    invoice = invoicing.invoice(L, order["draft_id"], company=co)
    expected_tax = {}
    for line in row["lineItems"] + row["shippingLines"]:
        for tax in line["taxLines"]:
            rate = Decimal(str(tax["rate"])) * 100
            expected_tax[rate] = expected_tax.get(rate, Decimal(0)) + money(tax["priceSet"], body["currency"])
    actual_tax = {Decimal(t["rate"]): Decimal(t["vat"]) for t in invoice["data"]["totals"]["taxes"]}
    if any(
        actual_tax.get(rate, Decimal(0)) != expected_tax.get(rate, Decimal(0))
        for rate in actual_tax.keys() | expected_tax.keys()
    ):
        raise ShopifyError("Invoice tax differs from Shopify; review tax rounding and rates.")
    if Decimal(invoice["data"]["totals"]["total"]) != money(row["originalTotalPriceSet"], body["currency"]):
        raise ShopifyError("Invoice total differs from Shopify shop total; review taxes and discounts.")
    return order


def _tracking(L: Ledger, co: str, row: dict, order: dict, today: str) -> None:
    tracking = {
        (t["company"], t["number"])
        for f in row["fulfillments"]
        if f["status"] == "SUCCESS"
        for t in f["trackingInfo"]
        if t.get("number") and t.get("company")
    }
    if len(row["fulfillments"]) >= 250 or len(tracking) > 1:
        raise ShopifyError("Multiple tracking numbers require manual AWB review.")
    if tracking and order["status"] != "cancelled":
        courier, number = next(iter(tracking))
        commerce.command(L, co, order["id"], "orders_awb", dict(courier=courier, number=number), today)


def _refunds(L: Ledger, co: str, iid: str, body: dict, row: dict, order: dict, today: str) -> None:
    if len(row["refunds"]) >= 250:
        raise ShopifyError("Too many refunds; review manually.")
    for refund in row["refunds"]:
        refund_key = f"shopify:refund:{iid}:{refund['id']}"
        if L.get_meta(refund_key):
            continue
        total = money(refund["totalRefundedSet"], body["currency"])
        if total == 0:
            continue
        selection = _refund_selection(row, refund, body, total)
        result = commerce.command(
            L, co, order["id"], "orders_refund", {} if selection is None else {"lines": selection}, today
        )
        draft = invoicing.invoice(L, result["draft_id"], company=co)
        if -Decimal(draft["data"]["totals"]["total"]) != total:
            raise ShopifyError("Refund differs from the proposed storno; review manually.")
        L.set_meta(refund_key, result["draft_id"])


def _refund_selection(row: dict, refund: dict, body: dict, total: Decimal) -> list[int] | None:
    selection = None
    if total != money(row["originalTotalPriceSet"], body["currency"]):
        positions, pos = {}, 1
        for item in row["lineItems"]:
            positions[item["id"]] = (pos, item["quantity"])
            pos += 1 + bool(
                sum(
                    (money(a["allocatedAmountSet"], body["currency"]) for a in item["discountAllocations"]),
                    Decimal(0),
                )
            )
        selection = []
        for item in refund.get("items", []):
            position, quantity = positions.get(item["lineItem"]["id"], (None, None))
            if position is None or item["quantity"] != quantity:
                raise ShopifyError("Partial quantity refund requires manual storno review.")
            selection.append(position)
        if not selection:
            raise ShopifyError("Refund allocation requires manual storno review.")
    return selection


def webhook(L: Ledger, iid: str, headers: dict, raw: bytes, today: str) -> dict:
    shop = channel(L, iid)
    expected = base64.b64encode(hmac.new(shop.secret("shopify_secret").encode(), raw, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, headers.get("X-Shopify-Hmac-Sha256", "")):
        raise ShopifyError("Invalid Shopify signature.")
    topic = headers.get("X-Shopify-Topic")
    if topic not in TOPICS or headers.get("X-Shopify-Shop-Domain") != shop.config["shop_domain"]:
        raise ShopifyError("Invalid Shopify topic or shop.")
    payload = json.loads(raw)
    oid = payload.get("order_id") if topic in ("refunds/create", "fulfillments/create") else payload.get("id")
    if type(oid) is not int or oid <= 0:
        raise ShopifyError("Invalid Shopify order ID.")
    # Acknowledge within Shopify's deadline; only the tick fetches remote data.
    if is_live():
        queue(L, shop, f"gid://shopify/Order/{oid}")
    return {"ok": True, "queued": is_live()}


def queue(L: Ledger, shop: integrations.Instance, oid: str, updated_at: str | None = None) -> None:
    key = f"shopify:pending:{shop.id}:{oid.rsplit('/', 1)[1]}"
    with L.tx():
        if channel(L, shop.id).config != shop.config:
            raise ShopifyError("Shopify configuration changed; retry.")
        revision = L.get_meta(f"shopify:revision:{shop.id}:{oid.rsplit('/', 1)[1]}")
        if updated_at and revision and revision >= updated_at:
            return
        previous = json.loads(L.get_meta(key) or "{}")
        L.set_meta(
            key,
            json.dumps(
                {
                    "id": oid,
                    "attempted": previous.get("attempted", 0),
                    "company": shop.config["company"],
                    "seq": previous.get("seq", 0) + 1,
                    "queued_at": time.time(),
                    "webhook": updated_at is None or previous.get("webhook", False),
                }
            ),
        )


def tick(L: Ledger, today: str) -> dict:
    """Newest-page overlap plus a resumable historical sweep; retries stay durable."""
    counts = dict(imported=0, refused=0, payouts=0, payout_refused=0)
    for shop in integrations.enabled(L, "shop"):
        if shop.config["platform"] != "shopify":
            continue
        # Writeback has an independent budget and always runs before remote reads.
        try:
            write_back(L, shop.id)
        except Exception:
            counts["refused"] += 1
        cursor_key = "shopify:sweep:" + shop.id
        cursor = L.get_meta(cursor_key) or None
        try:
            # Always revisit the newest page, independently of historical progress.
            for after in dict.fromkeys((None, cursor)):
                part = graphql(
                    shop,
                    "query($cursor:String){orders(first:50,after:$cursor,sortKey:UPDATED_AT,reverse:true){nodes{id updatedAt}"
                    + PAGE
                    + "}}",
                    {"cursor": after},
                )["orders"]
                for row in part["nodes"]:
                    queue(L, shop, row["id"], row["updatedAt"])
                if after == cursor:
                    L.set_meta(cursor_key, part["pageInfo"]["endCursor"] if part["pageInfo"]["hasNextPage"] else "")
        except Exception:
            counts["refused"] += 1
            L.set_meta(cursor_key, "")  # Invalid cursors restart; pending IDs survive.
        pending = [
            (r["key"], json.loads(r["value"]))
            for r in L.db.execute("SELECT key,value FROM meta WHERE key LIKE ?", (f"shopify:pending:{shop.id}:%",))
        ]
        deadline = time.monotonic() + 120
        for key, item in sorted(
            pending,
            key=lambda p: (
                p[1]["attempted"] != 0,
                not p[1].get("webhook", False),
                p[1]["attempted"] or -p[1].get("queued_at", 0),
            ),
        ):
            if time.monotonic() >= deadline:
                break
            if item["company"] != shop.config["company"]:
                counts["refused"] += 1
                continue
            # The attempted time commits before network so failed orders cannot starve others.
            with L.tx():
                current = L.get_meta(key)
                if not current:
                    continue
                item = json.loads(current)  # Never overwrite a hook queued since the scan.
                item["attempted"] = time.time()
                L.set_meta(key, json.dumps(item))
            try:
                ingest(L, shop.id, read_order(shop, item["id"]), today)
                with L.tx() as db:
                    db.execute("DELETE FROM meta WHERE key=? AND value=?", (key, json.dumps(item)))
                counts["imported"] += 1
            except Exception:
                counts["refused"] += 1
        try:
            result = payouts(L, shop)
            counts["payouts"] += result["imported"]
            counts["payout_refused"] += result["refused"]
        except Exception:
            counts["payout_refused"] += 1
    return counts


def payout_money(value: dict, currency: str) -> Decimal:
    if value["currencyCode"] != currency:
        raise ShopifyError("Payout currency differs; review manually.")
    return report_money(value["amount"])


def payout_key(shop: integrations.Instance, payout: dict) -> str:
    pid = payout["id"]
    if not re.fullmatch(r"gid://shopify/ShopifyPaymentsPayout/[0-9]+", pid):
        raise ShopifyError("Invalid Shopify payout ID.")
    return "shopify_" + shop.id + "_" + pid.rsplit("/", 1)[1]


def payout_report(shop: integrations.Instance, payout: dict) -> tuple[bytes, dict]:
    """Read the complete transfer, refusing unsupported accounting rather than dropping lines."""
    pid = payout["id"]
    report_key = payout_key(shop, payout)
    rows = connection(
        lambda cursor: graphql(
            shop,
            "query($cursor:String,$filter:String!){shopifyPaymentsAccount{"
            "balanceTransactions(first:100,after:$cursor,query:$filter,hideTransfers:true){nodes{"
            "id type test associatedPayout{id} associatedOrder{id} "
            "amount{amount currencyCode} fee{amount currencyCode} net{amount currencyCode}}" + PAGE + "}}}",
            {"cursor": cursor, "filter": "payments_transfer_id:" + pid.rsplit("/", 1)[1]},
        )["shopifyPaymentsAccount"]["balanceTransactions"]
    )
    currency = payout["net"]["currencyCode"]
    net, seen = Decimal(0), set()
    data = io.StringIO()
    writer = csv.writer(data, lineterminator="\n")
    writer.writerow(["order", "gross", "fee", "net", "currency", "batch"])
    for row in rows:
        oid = (row.get("associatedOrder") or {}).get("id", "")
        if (
            row["id"] in seen
            or row["test"]
            or row["type"] != "CHARGE"
            or row["associatedPayout"]["id"] != pid
            or not re.fullmatch(r"gid://shopify/Order/[0-9]+", oid)
        ):
            raise ShopifyError("Unsupported or duplicate payout transaction; review manually.")
        seen.add(row["id"])
        gross, fee, amount = (payout_money(row[k], currency) for k in ("amount", "fee", "net"))
        if gross <= 0 or fee > gross or amount != gross - fee:
            raise ShopifyError("Payout gross, fees and net differ; review manually.")
        net += amount
        writer.writerow([oid.rsplit("/", 1)[1], str(gross), str(fee), str(amount), currency, report_key])
    if not rows or net != payout_money(payout["net"], currency):
        raise ShopifyError("Incomplete payout; review manually.")
    return data.getvalue().encode(), dict(
        source="processor",
        report_key=report_key,
        date=payout["issuedAt"][:10],
        currency=currency,
        payer_name="Shopify Payments",
        channel=shop.id,
        mapping={k: k for k in ("order", "gross", "fee", "net", "currency", "batch")},
    )


def payout_exists(L: Ledger, shop: integrations.Instance, key: str) -> bool:
    # Withdrawn evidence is durable too: a human dismissal must survive every retry.
    return (
        L.db.execute(
            "SELECT 1 FROM settlement_reports WHERE company=? AND report_key=? AND json_extract(data,'$.channel')=?",
            (shop.config["company"], key, shop.id),
        ).fetchone()
        is not None
    )


def import_payout(L: Ledger, shop: integrations.Instance, payout: dict) -> bool:
    key = payout_key(shop, payout)
    if payout_exists(L, shop, key):
        return False
    data, spec = payout_report(shop, payout)
    with L.tx():
        if channel(L, shop.id).config != shop.config:
            raise ShopifyError("Shopify configuration changed; retry.")
        # Re-check under the write lock after network reads from overlapping ticks.
        if payout_exists(L, shop, key):
            return False
        settlements.import_report(L, shop.config["company"], data, spec)
    return True


def payouts(L: Ledger, shop: integrations.Instance) -> dict:
    """Retry from the complete bounded payout listing; remote paid status is never collection proof."""
    counts = dict(imported=0, refused=0)
    since = L.db.execute("SELECT created_at FROM integrations WHERE id=?", (shop.id,)).fetchone()[0][:10]
    rows = connection(
        lambda cursor: (
            graphql(
                shop,
                "query($cursor:String,$filter:String!){shopifyPaymentsAccount{payouts(first:100,after:$cursor,query:$filter){"
                "nodes{id issuedAt status transactionType net{amount currencyCode}}" + PAGE + "}}}",
                {"cursor": cursor, "filter": "issued_at:>=" + since},
            )["shopifyPaymentsAccount"]
            or {"payouts": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
        )["payouts"]
    )
    for payout in rows:
        if payout["issuedAt"][:10] < since or payout["status"] != "PAID" or payout["transactionType"] != "DEPOSIT":
            continue
        try:
            counts["imported"] += import_payout(L, shop, payout)
        except Exception:
            counts["refused"] += 1  # No remote text, identifiers or credentials in diagnostics.
    # Revisit pending reports even on retries, including when bank evidence arrives later.
    settlements.match_statement(L, statements.Statement(shop.config["company"], None, None, []))
    return counts


def write_back(L: Ledger, iid: str) -> None:
    """Idempotent metafield set, retried after crashes, never sends in shadow."""
    if not is_live():
        return
    shop = channel(L, iid)
    base = shop.config.get("fiscalm_url", "")
    if not base:
        return
    for row in L.db.execute(
        "SELECT id FROM commerce_orders WHERE channel=? AND company=?", (iid, shop.config["company"])
    ):
        order = commerce.get(L, shop.config["company"], row[0])
        if not order["invoice_id"]:
            continue
        invoice = invoicing.invoice(L, order["invoice_id"], company=shop.config["company"])
        if not invoice["data"].get("issued_live"):
            continue
        value = json.dumps(
            dict(
                number=invoicing.invoice_number(invoice),
                url=base.rstrip("/") + "/?co=" + shop.config["company"] + "&iss=" + order["invoice_id"],
            )
        )
        key = "shopify:written:" + order["id"]
        if L.get_meta(key) == value:
            continue
        if not is_live():
            return
        result = graphql(
            channel(L, iid),
            "mutation($fields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$fields){userErrors{code}}}",
            {
                "fields": [
                    dict(
                        ownerId="gid://shopify/Order/" + order["external_id"],
                        namespace="fiscalm",
                        key="invoice",
                        type="json",
                        value=value,
                    )
                ]
            },
        )
        if result["metafieldsSet"]["userErrors"]:
            raise ShopifyError("Shopify refused invoice metafield; check write_orders scope.")
        L.set_meta(key, value)


def handle(handler) -> None:
    """Public HMAC route, bounded bytes and generic failures; no bearer credential."""
    handler.close_connection = True
    iid = handler.path.removeprefix("/hooks/shopify/")
    try:
        if (
            handler.headers.get("Transfer-Encoding")
            or len(handler.headers.get_all("Content-Length", [])) != 1
            or any(
                len(handler.headers.get_all(k, [])) != 1
                for k in ("X-Shopify-Hmac-Sha256", "X-Shopify-Topic", "X-Shopify-Shop-Domain")
            )
        ):
            raise ShopifyError("Invalid headers.")
        size = int(handler.headers["Content-Length"])
        if not 0 < size <= commerce.LIMIT:
            raise ShopifyError("Invalid size.")
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise ShopifyError("Incomplete body.")
        L = Ledger()
        try:
            webhook(L, iid, handler.headers, raw, date.today().isoformat())
        finally:
            L.close()
        handler._json(200, {"ok": True})
    except Exception:
        handler._json(409, {"ok": False, "error": "shopify_retry_or_review"})
