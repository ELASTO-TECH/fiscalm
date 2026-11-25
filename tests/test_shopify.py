"""Faithful GraphQL envelopes; transport never reaches Shopify."""

import base64
import copy
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from test_invoicing import setup_invoice as setup_invoice
from test_settlements import collect, statement, store_bank

from fiscalm import integrations
from fiscalm.commerce import shopify as s
from fiscalm.invoicing import engine as inv
from fiscalm.ledger import Ledger
from fiscalm.settlements import engine as settlements


def amount(n, currency="RON"):
    return {
        "shopMoney": {"amount": str(n), "currencyCode": currency},
        "presentmentMoney": {"amount": "999.00", "currencyCode": "EUR"},
    }


@pytest.fixture
def shop(setup_invoice):
    L, _ = setup_invoice
    cfg = dict(
        platform="shopify",
        company="NORD",
        series="TEST",
        shop_domain="synthetic-shop.myshopify.com",
        admin_token="synthetic-admin",
        shopify_secret="synthetic-secret",
        fiscalm_url="https://books.example.test",
    )
    iid = integrations.add(L, "shop", "Synthetic Shopify", cfg)["id"]
    with L.tx():
        L.db.execute("UPDATE integrations SET created_at=? WHERE id=?", ("2026-10-30T00:00:00Z", iid))
    row = dict(
        id="gid://shopify/Order/12",
        updatedAt="2026-10-31T12:00:00Z",
        createdAt="2026-10-30T10:00:00Z",
        cancelledAt=None,
        edited=False,
        taxesIncluded=True,
        currencyCode="RON",
        email="test@example.test",
        displayFinancialStatus="PAID",
        paymentGatewayNames=["shopify_payments"],
        billingAddress=dict(
            name="Client fictiv",
            company=None,
            address1="Strada Exemplu 1",
            address2=None,
            city="Brașov",
            provinceCode="BV",
            countryCodeV2="RO",
            zip="500000",
        ),
        purchasingEntity=None,
        fulfillments=[],
        refunds=[],
        originalTotalPriceSet=amount("242.00"),
        lineItems=[
            dict(
                id="gid://shopify/LineItem/1",
                name="Produs",
                sku="A",
                quantity=2,
                isGiftCard=False,
                originalUnitPriceSet=amount("121"),
                discountAllocations=[],
                taxLines=[dict(rate="0.21", priceSet=amount("42"))],
            )
        ],
        shippingLines=[],
    )
    return L, iid, row


class FakeShopify:
    def __init__(self, row):
        self.row = row
        self.calls = []
        self.mutations = []

    def __call__(self, shop, query, variables):
        self.calls.append((query, variables))
        if "shopifyPaymentsAccount" in query:
            return {"shopifyPaymentsAccount": {"payouts": self.page([], None)}}
        if query.startswith("mutation"):
            self.mutations.append(variables)
            return {"metafieldsSet": {"userErrors": []}}
        if "orders(first:" in query:
            return {
                "orders": self.page([{"id": self.row["id"], "updatedAt": self.row["updatedAt"]}], variables["cursor"])
            }
        for key in ("lineItems", "shippingLines"):
            if key + "(first:" in query:
                return {"order": {key: self.page(self.row[key], variables["cursor"])}}
        if "{updatedAt}" in query:
            return {"order": {"updatedAt": self.row["updatedAt"]}}
        return {"order": copy.deepcopy(self.row)}

    @staticmethod
    def page(rows, cursor):
        # One node per page forces nested and outer pagination paths.
        start = int(cursor or 0)
        return dict(
            nodes=copy.deepcopy(rows[start : start + 1]),
            pageInfo=dict(hasNextPage=start + 1 < len(rows), endCursor=str(start + 1)),
        )


def signed(row, topic="orders/create", oid=12):
    raw = json.dumps({"id": oid, "order_id": oid, "url": "https://evil.invalid"}).encode()
    sig = base64.b64encode(hmac.new(b"synthetic-secret", raw, hashlib.sha256).digest()).decode()
    return raw, {
        "X-Shopify-Hmac-Sha256": sig,
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": "synthetic-shop.myshopify.com",
    }


def test_webhook_all_topics_replay_and_pagination(shop, monkeypatch, home):
    L, iid, row = shop
    (home / "state" / "LIVE").touch()
    row["lineItems"] *= 2
    row["originalTotalPriceSet"] = amount("484")
    fake = FakeShopify(row)
    monkeypatch.setattr(s, "graphql", fake)
    for topic in s.TOPICS:
        raw, headers = signed(row, topic)
        assert s.webhook(L, iid, headers, raw, "2026-10-31")["queued"]
    assert not fake.calls  # HMAC acknowledgment never waits for Shopify.
    assert L.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'shopify:pending:%'").fetchone()[0] == 1
    assert s.tick(L, "2026-10-31")["imported"] == 1
    assert s.tick(L, "2026-10-31")["imported"] == 0
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1
    assert any(v.get("cursor") == "1" for q, v in fake.calls if "lineItems(first:" in q)


@pytest.mark.parametrize(
    "change",
    [
        {"X-Shopify-Hmac-Sha256": "invalid"},
        {"X-Shopify-Shop-Domain": "other.myshopify.com"},
        {"X-Shopify-Topic": "app/uninstalled"},
    ],
)
def test_invalid_webhook_never_fetches(shop, monkeypatch, change):
    L, iid, row = shop
    monkeypatch.setattr(s, "graphql", lambda *a: pytest.fail("unverified network"))
    raw, headers = signed(row)
    with pytest.raises(s.ShopifyError):
        s.webhook(L, iid, headers | change, raw, "2026-10-31")


def test_mapping_net_discount_shipping_gift_redemption_b2b(shop):
    L, iid, row = shop
    row["taxesIncluded"] = False
    row["lineItems"][0]["originalUnitPriceSet"] = amount("100")
    row["lineItems"][0]["taxLines"][0]["priceSet"] = amount("37.80")
    row["lineItems"][0]["discountAllocations"] = [dict(allocatedAmountSet=amount("20"))]
    row["shippingLines"] = [
        dict(
            id="ship",
            title="Transport",
            discountedPriceSet=amount("10"),
            taxLines=[dict(rate="0.21", priceSet=amount("2.10"))],
        )
    ]
    row["originalTotalPriceSet"] = amount("229.90")
    row["paymentGatewayNames"] = ["gift_card"]
    row["purchasingEntity"] = dict(
        company=dict(name="Firma Fictivă SRL"), location=dict(taxRegistrationId="RO99999984")
    )
    result = s.ingest(L, iid, row, "2026-10-31")
    invoice = inv.invoice(L, result["draft_id"], company="NORD")
    assert invoice["data"]["totals"]["total"] == "229.90"
    assert not invoice["data"]["client"]["private"]
    assert invoice["data"]["currency"] == "RON"
    assert s.mapping(s.channel(L, iid), row)["payment"]["method"] == "other"


@pytest.mark.parametrize("case", ["voucher", "tax", "total", "currency", "company", "edit", "awb"])
def test_mapping_refuses_ambiguous_money_atomically(shop, case):
    L, iid, row = shop
    if case == "voucher":
        row["lineItems"][0]["isGiftCard"] = True
    if case == "tax":
        row["lineItems"][0]["taxLines"] *= 2
    if case == "total":
        row["originalTotalPriceSet"] = amount("240")
    if case == "currency":
        row["lineItems"][0]["originalUnitPriceSet"] = amount("121", "EUR")
    if case == "company":
        row["billingAddress"]["company"] = "Firma fără cod"
    if case == "edit":
        row["edited"] = True
    if case == "awb":
        row["fulfillments"] = [
            dict(
                status="SUCCESS", trackingInfo=[dict(company="Curier", number="A"), dict(company="Curier", number="B")]
            )
        ]
    integrations.update(L, iid, {"auto_issue": "true"})
    with pytest.raises(s.ShopifyError):
        s.ingest(L, iid, row, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0


def test_racing_intake_shadow_and_wrong_company(shop):
    L, iid, row = shop
    integrations.update(L, iid, {"auto_issue": "true"})
    barrier = Barrier(2)

    def run(_):
        other = Ledger()
        try:
            barrier.wait()
            return s.ingest(other, iid, row, "2026-10-31")
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(run, range(2))
    assert a == b
    assert a["status"] == "draft"
    with pytest.raises(s.commerce.Error):
        s.commerce.get(L, "STUDIO9", a["id"])
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1


def test_refund_awb_cancel_and_old_revision(shop):
    L, iid, row = shop
    result = s.ingest(L, iid, row, "2026-10-31")
    inv.issue(L, result["draft_id"], "2026-10-31", company="NORD", version=1)
    row["fulfillments"] = [dict(status="SUCCESS", trackingInfo=[dict(company="Curier", number="AWB-fictiv")])]
    row["refunds"] = [dict(id="refund", totalRefundedSet=amount("242"))]
    s.ingest(L, iid, row, "2026-10-31")
    s.ingest(L, iid, row, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoices WHERE status='draft'").fetchone()[0] == 1
    assert s.commerce.get(L, "NORD", result["id"])["awb"]["number"] == "AWB-fictiv"
    row["refunds"][0]["id"] = "another-refund"
    row["refunds"][0]["totalRefundedSet"] = amount("121")
    with pytest.raises(s.ShopifyError):
        s.ingest(L, iid, row, "2026-10-31")
    row["updatedAt"] = "2026-10-30T00:00:00Z"
    assert s.ingest(L, iid, row, "2026-10-31")["id"] == result["id"]


def test_tick_recovers_missed_webhook_and_writeback_live_only(shop, monkeypatch, home):
    L, iid, row = shop
    fake = FakeShopify(row)
    monkeypatch.setattr(s, "graphql", fake)
    assert s.tick(L, "2026-10-31")["imported"] == 1
    order = L.db.execute("SELECT draft_id FROM commerce_orders").fetchone()[0]
    inv.issue(L, order, "2026-10-31", company="NORD", version=1)
    s.write_back(L, iid)
    assert not fake.mutations
    (home / "state" / "LIVE").touch()
    # A historical shadow issue must never be published.
    s.write_back(L, iid)
    assert not fake.mutations
    data = json.loads(L.db.execute("SELECT data FROM invoices WHERE id=?", (order,)).fetchone()[0])
    data["issued_live"] = True
    L.db.execute("UPDATE invoices SET data=? WHERE id=?", (json.dumps(data), order))
    s.write_back(L, iid)
    s.write_back(L, iid)
    assert len(fake.mutations) == 1
    value = json.loads(fake.mutations[0]["fields"][0]["value"])
    assert value["number"] == "TEST-42"
    assert value["url"].startswith("https://books.example.test/")


@pytest.mark.parametrize(
    "domain",
    ["evil.test", "x.myshopify.com.evil.test", "https://x.myshopify.com", "x.myshopify.com/path", "x..myshopify.com"],
)
def test_domain_and_secrecy(shop, domain):
    L, iid, _ = shop
    with pytest.raises(integrations.IntegrationError):
        integrations.update(L, iid, {"shop_domain": domain})
    public = json.dumps(integrations.listing(L))
    assert "synthetic-admin" not in public and "synthetic-secret" not in public


def test_redirect_and_api_error_safe(shop, monkeypatch):
    from urllib.error import HTTPError

    L, iid, _ = shop

    class Opener:
        def open(self, req, timeout):
            assert req.full_url == "https://synthetic-shop.myshopify.com/admin/api/2026-10/graphql.json"
            raise HTTPError(req.full_url, 302, "secret remote message", {}, None)

    monkeypatch.setattr(s.urllib.request, "build_opener", lambda handler: Opener())
    with pytest.raises(s.ShopifyError, match="request failed"):
        s.graphql(s.channel(L, iid), "query{shop{id}}", {})


def test_partial_whole_line_refund_and_quantity_refusal(shop):
    L, iid, row = shop
    second = copy.deepcopy(row["lineItems"][0])
    second["id"] = "gid://shopify/LineItem/2"
    row["lineItems"].append(second)
    row["originalTotalPriceSet"] = amount("484")
    result = s.ingest(L, iid, row, "2026-10-31")
    inv.issue(L, result["draft_id"], "2026-10-31", company="NORD", version=1)
    row["refunds"] = [
        dict(id="refund", totalRefundedSet=amount("242"), items=[dict(quantity=2, lineItem={"id": second["id"]})])
    ]
    s.ingest(L, iid, row, "2026-10-31")
    s.ingest(L, iid, row, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoices WHERE status='draft'").fetchone()[0] == 1
    row["refunds"][0]["id"] = "another-refund"
    row["refunds"][0]["items"][0]["quantity"] = 1
    with pytest.raises(s.ShopifyError):
        s.ingest(L, iid, row, "2026-10-31")


def test_cancel_and_torn_read_and_stuck_pagination(shop, monkeypatch):
    L, iid, row = shop
    row["cancelledAt"] = "2026-10-31T13:00:00Z"
    assert s.ingest(L, iid, row, "2026-10-31")["status"] == "cancelled"
    fake = FakeShopify(row)

    def torn(shop, query, variables):
        if "{updatedAt}" in query:
            return {"order": {"updatedAt": "2026-11-01T00:00:00Z"}}
        return fake(shop, query, variables)

    monkeypatch.setattr(s, "graphql", torn)
    with pytest.raises(s.ShopifyError):
        s.read_order(s.channel(L, iid), row["id"])
    with pytest.raises(s.ShopifyError):
        s.connection(lambda c: dict(nodes=[], pageInfo=dict(hasNextPage=True, endCursor="same")))


def test_http_hook_is_public_post_only(shop, monkeypatch):
    from http.client import HTTPConnection
    from threading import Thread

    from fiscalm.api.web import Handler, Server

    L, iid, row = shop
    monkeypatch.setattr(s, "graphql", FakeShopify(row))
    monkeypatch.setenv("FISCALM_PUBLIC_URL", "https://books.example")
    server = Server(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()

    def request(method, body=None, headers=None):
        conn = HTTPConnection(*server.server_address, timeout=5)
        try:
            conn.request(method, "/hooks/shopify/" + iid, body, {"Host": "books.example", **(headers or {})})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    try:
        raw, headers = signed(row)
        assert request("POST", raw, headers) == 200
        assert request("POST", raw, headers | {"X-Shopify-Hmac-Sha256": "invalid"}) == 409
        assert request("GET") != 200
        assert request("PUT", raw, headers) != 200
        assert request("POST", b"x" * (s.commerce.LIMIT + 1), headers) == 409
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_gross_mixed_tax_discount_shipping_and_zero_vat_review(shop, home):
    L, iid, row = shop
    row["lineItems"][0]["discountAllocations"] = [dict(allocatedAmountSet=amount("24.20"))]
    row["lineItems"][0]["taxLines"][0]["priceSet"] = amount("37.80")
    row["shippingLines"] = [
        dict(
            id="ship",
            title="Transport",
            discountedPriceSet=amount("11.10"),
            taxLines=[dict(rate="0.11", priceSet=amount("1.10"))],
        )
    ]
    row["originalTotalPriceSet"] = amount("228.90")
    result = s.ingest(L, iid, row, "2026-10-31")
    assert inv.invoice(L, result["draft_id"], company="NORD")["data"]["totals"]["total"] == "228.90"
    row["lineItems"][0]["taxLines"] = []
    (home / "state" / "LIVE").touch()
    integrations.update(L, iid, {"auto_issue": "true"})
    row["id"], row["createdAt"] = "gid://shopify/Order/14", "2026-10-31T10:00:00Z"
    result = s.ingest(L, iid, row, "2026-10-31")  # a new order placed today, in Live, with auto issue
    assert result["status"] == "draft" and s.commerce.get(L, "NORD", result["id"])["reason"]["code"] == "vat_review"


def test_tax_mismatch_refuses_before_live_auto_issue(shop, home):
    L, iid, row = shop
    (home / "state" / "LIVE").touch()
    integrations.update(L, iid, {"auto_issue": "true"})
    row["lineItems"][0]["taxLines"][0]["priceSet"] = amount("41")
    with pytest.raises(s.ShopifyError, match="tax differs"):
        s.ingest(L, iid, row, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0
    row["lineItems"][0]["taxLines"][0]["priceSet"] = amount("42")
    row["createdAt"] = "2026-10-31T10:00:00Z"
    assert s.ingest(L, iid, row, "2026-10-31")["status"] == "issued"


def test_enabling_auto_issue_preserves_backfilled_draft_and_shadow_hook_noop(shop, home):
    L, iid, row = shop
    raw, headers = signed(row)
    assert not s.webhook(L, iid, headers, raw, "2026-10-31")["queued"]
    assert L.db.execute("SELECT count(*) FROM commerce_orders").fetchone()[0] == 0
    assert not L.db.execute("SELECT 1 FROM meta WHERE key LIKE 'shopify:pending:%'").fetchone()
    s.ingest(L, iid, row, "2026-10-31")
    (home / "state" / "LIVE").touch()
    integrations.update(L, iid, {"auto_issue": "true"})
    assert s.ingest(L, iid, row, "2026-10-31")["status"] == "draft"
    row["id"] = "gid://shopify/Order/13"
    assert s.ingest(L, iid, row, "2026-10-31")["status"] == "draft"  # old unseen order too


@pytest.mark.parametrize("deleted", [True, False])
def test_handled_partial_refund_never_regenerates_or_blocks_later_awb(shop, deleted):
    L, iid, row = shop
    second = copy.deepcopy(row["lineItems"][0])
    second["id"] = "gid://shopify/LineItem/2"
    # A zero discount allocation must not shift refund positions.
    row["lineItems"][0]["discountAllocations"] = [dict(allocatedAmountSet=amount("0"))]
    row["lineItems"].append(second)
    row["originalTotalPriceSet"] = amount("484")
    order = s.ingest(L, iid, row, "2026-10-31")
    inv.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    row["refunds"] = [
        dict(id="refund", totalRefundedSet=amount("242"), items=[dict(quantity=2, lineItem={"id": second["id"]})])
    ]
    s.ingest(L, iid, row, "2026-10-31")
    did = L.get_meta(f"shopify:refund:{iid}:refund")
    if deleted:
        inv.delete_draft(L, did, company="NORD", version=1)
    else:
        inv.issue(L, did, "2026-10-31", company="NORD", version=1)
    row["fulfillments"] = [dict(status="SUCCESS", trackingInfo=[dict(company="Curier", number="later")])]
    s.ingest(L, iid, row, "2026-10-31")
    assert s.commerce.get(L, "NORD", order["id"])["awb"]["number"] == "later"
    assert L.db.execute("SELECT count(*) FROM invoices WHERE status='draft'").fetchone()[0] == 0


def test_newest_page_and_writeback_run_even_with_bad_sweep_cursor(shop, monkeypatch):
    L, iid, row = shop
    fake = FakeShopify(row)
    writes = []
    L.set_meta("shopify:sweep:" + iid, "bad-cursor")

    def remote(shop, query, variables):
        if variables.get("cursor") == "bad-cursor":
            raise s.ShopifyError("bad cursor")
        return fake(shop, query, variables)

    monkeypatch.setattr(s, "graphql", remote)
    monkeypatch.setattr(s, "write_back", lambda *args: writes.append(True))
    result = s.tick(L, "2026-10-31")
    assert writes and result == dict(imported=1, refused=1, payouts=0, payout_refused=0)
    assert any("reverse:true" in q for q, _ in fake.calls)


def test_hook_during_read_survives_compare_delete_and_then_drains(shop, monkeypatch, home):
    L, iid, row = shop
    (home / "state" / "LIVE").touch()
    fake = FakeShopify(row)
    monkeypatch.setattr(s, "graphql", fake)
    original = s.read_order
    once = []

    def read(shop, oid):
        result = original(shop, oid)
        if not once:
            once.append(True)
            raw, headers = signed(row, "refunds/create")
            other = Ledger()
            try:
                s.webhook(other, iid, headers, raw, "2026-10-31")
            finally:
                other.close()
        return result

    monkeypatch.setattr(s, "read_order", read)
    assert s.tick(L, "2026-10-31")["imported"] == 1
    assert L.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'shopify:pending:%'").fetchone()[0] == 1
    assert s.tick(L, "2026-10-31")["imported"] == 1
    assert not L.db.execute("SELECT 1 FROM meta WHERE key LIKE 'shopify:pending:%'").fetchone()
    calls = len(fake.calls)
    assert s.tick(L, "2026-10-31")["imported"] == 0
    assert len(fake.calls) == calls + 2  # order head and payout listing, no per-order fetch


def test_webhook_orders_precede_historical_sweep_with_tied_attempts(shop, monkeypatch):
    L, iid, row = shop
    shop = s.channel(L, iid)
    s.queue(L, shop, "gid://shopify/Order/1", row["updatedAt"])
    s.queue(L, shop, "gid://shopify/Order/999")
    fake = FakeShopify(row)
    monkeypatch.setattr(s, "graphql", fake)
    monkeypatch.setattr(s, "write_back", lambda *a: None)
    seen = []

    def read(shop, oid):
        seen.append(oid)
        raise s.ShopifyError("no data")

    monkeypatch.setattr(s, "read_order", read)
    s.tick(L, "2026-10-31")
    assert seen[0] == "gid://shopify/Order/999"


class FakePayouts(FakeShopify):
    def __init__(self, row):
        super().__init__(row)
        self.payouts = [
            dict(
                id=f"gid://shopify/ShopifyPaymentsPayout/{n}",
                issuedAt="2026-10-31T10:00:00Z",
                status="PAID",
                transactionType="DEPOSIT",
                net=dict(amount="240.00", currencyCode="RON"),
            )
            for n in (1, 2)
        ]
        self.balance = [
            dict(
                id=f"balance-{n}",
                type="CHARGE",
                test=False,
                associatedPayout={"id": p["id"]},
                associatedOrder={"id": row["id"]},
                amount=dict(amount="242.00", currencyCode="RON"),
                fee=dict(amount="2.00", currencyCode="RON"),
                net=p["net"].copy(),
            )
            for n, p in enumerate(self.payouts)
        ]
        self.fail = False

    def __call__(self, shop, query, variables):
        if "shopifyPaymentsAccount" not in query:
            return super().__call__(shop, query, variables)
        assert not query.startswith("mutation")
        self.calls.append((query, variables))
        if self.fail:
            self.fail = False
            raise s.ShopifyError("retry")
        if "balanceTransactions(" in query:
            rows = [
                r for r in self.balance if r["associatedPayout"]["id"].endswith("/" + variables["filter"].split(":")[1])
            ]
            return {"shopifyPaymentsAccount": {"balanceTransactions": self.page(rows, variables["cursor"])}}
        return {"shopifyPaymentsAccount": {"payouts": self.page(self.payouts, variables["cursor"])}}


def test_payout_pagination_retry_and_shadow(shop, monkeypatch):
    L, iid, row = shop
    fake = FakePayouts(row)
    # Two charge lines per payout exercise both connections, without collecting from paid flags.
    fake.balance += [r | {"id": r["id"] + "b"} for r in fake.balance]
    for p in fake.payouts:
        p["net"]["amount"] = "480.00"
    fake.fail = True
    monkeypatch.setattr(s, "graphql", fake)
    assert s.tick(L, "2026-10-31")["payout_refused"] == 1
    assert s.tick(L, "2026-10-31")["payouts"] == 2
    assert s.tick(L, "2026-10-31")["payouts"] == 0
    assert L.db.execute("SELECT count(*) FROM settlement_reports").fetchone()[0] == 2
    assert not L.used_txs()
    for key in ("payouts(", "balanceTransactions("):
        assert any(key in q and v["cursor"] == "1" for q, v in fake.calls)


@pytest.mark.parametrize("case", ["currency", "fee", "total", "duplicate", "refund", "test", "wrong-payout"])
def test_payout_refuses_whole_report(shop, monkeypatch, case):
    L, iid, row = shop
    fake = FakePayouts(row)
    fake.payouts = fake.payouts[:1]
    r = fake.balance[0]
    if case == "currency":
        r["fee"]["currencyCode"] = "EUR"
    if case == "fee":
        r["fee"]["amount"] = "2.01"
    if case == "total":
        fake.payouts[0]["net"]["amount"] = "240.01"
    if case == "duplicate":
        fake.balance.insert(0, copy.deepcopy(r))
    if case == "refund":
        r["type"] = "REFUND"
    if case == "test":
        r["test"] = True
    if case == "wrong-payout":
        r["associatedPayout"]["id"] += "9"
    monkeypatch.setattr(s, "graphql", fake)
    assert s.payouts(L, s.channel(L, iid))["refused"] == 1
    assert not L.db.execute("SELECT 1 FROM settlement_reports").fetchone()
    assert not L.used_txs()


def test_payout_atomic_import_and_bank_collection(shop, monkeypatch, home):
    L, iid, row = shop
    fake = FakePayouts(row)
    monkeypatch.setattr(s, "graphql", fake)
    order = s.ingest(L, iid, row, "2026-10-31")
    inv.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    own = inv.import_id("local", order["draft_id"])  # never read back from SPV: its own row settles
    barrier = Barrier(2)

    def run(_):
        other = Ledger()
        try:
            barrier.wait()
            return s.import_payout(other, s.channel(other, iid), fake.payouts[0])
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(run, range(2))) == [False, True]
    rid = L.db.execute("SELECT id FROM settlement_reports").fetchone()[0]
    st = statement(date=date(2026, 10, 31), amount=Decimal("240.00"), counterparty="Shopify Payments")
    with pytest.raises(settlements.SettlementError, match="wrong_company"):
        L.settle_report(rid, settlements.BankEvidence("STUDIO9", None, None, st.accounts))
    assert L.issued_rows()[0]["status"] == "open"
    # Shared bank guards retain first-three confirmation and atomic reservation.
    store_bank(L, st)
    assert settlements.match_statement(L, st) == []
    assert collect(L, rid, st) == [own]
    assert len(L.used_txs()) == 1
    assert L.db.execute("SELECT amount FROM settlement_fees").fetchone()[0] == "2.00"


def test_payout_retry_after_balance_page_failure_and_reassigned_company(shop, monkeypatch):
    L, iid, row = shop
    fake = FakePayouts(row)
    shop_snapshot = s.channel(L, iid)
    failed = []

    def remote(shop, query, variables):
        if "balanceTransactions(" in query and not failed:
            failed.append(True)
            raise s.ShopifyError("retry")
        return fake(shop, query, variables)

    monkeypatch.setattr(s, "graphql", remote)
    with pytest.raises(s.ShopifyError):
        s.import_payout(L, shop_snapshot, fake.payouts[0])
    assert not L.db.execute("SELECT 1 FROM settlement_reports").fetchone()
    integrations.update(L, iid, {"company": "STUDIO9"})
    with pytest.raises(s.ShopifyError, match="configuration changed"):
        s.import_payout(L, shop_snapshot, fake.payouts[0])
    assert not L.db.execute("SELECT 1 FROM settlement_reports").fetchone()
    integrations.update(L, iid, {"company": "NORD"})
    assert s.import_payout(L, s.channel(L, iid), fake.payouts[0])


def test_payout_history_withdrawal_and_duplicates_skip_remote_reads(shop, monkeypatch):
    L, iid, row = shop
    fake = FakePayouts(row)
    fake.payouts[1]["issuedAt"] = "2025-01-01T00:00:00Z"
    monkeypatch.setattr(s, "graphql", fake)
    assert s.payouts(L, s.channel(L, iid))["imported"] == 1
    rid = L.db.execute("SELECT id FROM settlement_reports").fetchone()[0]
    assert s.payouts(L, s.channel(L, iid))["imported"] == 0
    assert L.withdraw_report(rid, "NORD")
    assert s.payouts(L, s.channel(L, iid))["imported"] == 0
    assert len([q for q, _ in fake.calls if "balanceTransactions(" in q]) == 1
    assert all(v["filter"] == "issued_at:>=2026-10-30" for q, v in fake.calls if "payouts(" in q)
    assert L.db.execute("SELECT count(*) FROM settlement_reports").fetchone()[0] == 1


def test_shop_without_payments_has_no_payout_refusal(shop, monkeypatch):
    L, iid, _ = shop
    monkeypatch.setattr(s, "graphql", lambda *args: {"shopifyPaymentsAccount": None})
    assert s.payouts(L, s.channel(L, iid)) == dict(imported=0, refused=0)
