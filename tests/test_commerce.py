from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_invoicing import PARTY, setup_invoice as setup_invoice

from fiscalm import integrations, rules
from fiscalm.api import actions, tokens
from fiscalm.api.v1 import Error
from fiscalm.commerce import orders as c
from fiscalm.invoicing import engine as i
from fiscalm.ledger import Ledger


@pytest.fixture
def shop(setup_invoice):
    L, _ = setup_invoice
    row = integrations.add(L, "shop", "Synthetic shop", dict(platform="custom", company="NORD", series="TEST"))
    body = dict(
        channel=row["id"],
        external_id="order-1",
        placed_at="2026-10-31",
        currency="RON",
        prices_include_vat=True,
        buyer=dict(
            type="person",
            name="Client fictiv",
            address=dict(street="Strada Exemplu 1", city="Brașov", county="RO-BV", country="RO"),
        ),
        lines=[dict(description="Produs", quantity="2", unit="H87", unit_price="121", vat_rate="21")],
        payment=dict(method="cod", paid=False),
    )
    return L, body


def test_intake_duplicate_update_and_stale_guard(shop):
    L, body = shop
    first = c.intake(L, "NORD", body, "2026-10-31")
    assert first == c.intake(L, "NORD", body, "2026-10-31")
    invoice = i.invoice(L, first["draft_id"], company="NORD")
    assert invoice["data"]["totals"]["total"] == "242.00"
    assert invoice["data"]["client"]["private"]
    changed = body | dict(payment=dict(method="card", paid=True))
    assert c.intake(L, "NORD", changed, "2026-10-31")["id"] == first["id"]
    assert i.invoice(L, first["draft_id"], company="NORD")["version"] == 2
    L.db.execute("UPDATE invoices SET version=3 WHERE id=?", (first["draft_id"],))
    with pytest.raises(i.DraftConflict):
        c.intake(L, "NORD", body, "2026-10-31")


def test_issued_change_and_wrong_company(shop):
    L, body = shop
    result = c.intake(L, "NORD", body, "2026-10-31")
    i.issue(L, result["draft_id"], "2026-10-31", company="NORD", version=1)
    assert c.intake(L, "NORD", body, "2026-10-31")["status"] == "issued"
    with pytest.raises(Error) as err:
        c.intake(L, "NORD", body | dict(external_id="order-1", currency="EUR"), "2026-10-31")
    assert err.value.code == "order_changed"
    with pytest.raises(Error):
        c.get(L, "STUDIO9", result["id"])
    with pytest.raises(Error):
        c.intake(L, "STUDIO9", body, "2026-10-31")


def test_crash_rolls_back_order_and_draft(shop, monkeypatch):
    L, body = shop
    original = i.save_draft

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(i, "save_draft", crash)
    with pytest.raises(RuntimeError):
        c.intake(L, "NORD", body, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM commerce_orders").fetchone()[0] == 0
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0
    monkeypatch.setattr(i, "save_draft", original)
    c.intake(L, "NORD", body, "2026-10-31")


def test_two_connections_exactly_one_draft(shop):
    L, body = shop
    barrier = Barrier(2)

    def submit(_):
        other = Ledger()
        try:
            barrier.wait()
            return c.intake(other, "NORD", body, "2026-10-31")
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        a, b = list(pool.map(submit, range(2)))
    assert a == b
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1


def test_shadow_auto_issue_is_draft(shop):
    L, body = shop
    integrations.update(L, body["channel"], {"auto_issue": "true"})
    assert c.intake(L, "NORD", body, "2026-10-31")["status"] == "draft"


def test_auto_issue_is_the_shops_rule_in_automatizari_not_an_integration_field(shop, home):
    L, body = shop
    rule = lambda: next(r for r in rules.list_rules(L, "NORD") if r["kind"] == "shop_auto_issue")  # noqa: E731
    assert (rule()["id"], rule()["enabled"], rule()["label"]) == (
        f"NORD:shop_auto_issue:{body['channel']}",
        False,
        "Synthetic shop",
    )
    assert not any(f["key"] == "auto_issue" for k in integrations.kinds() for f in k["fields"])
    assert not actions.ACTIONS["integration-update"].valid({"id": body["channel"], "f_auto_issue": "true"})
    rules.set_enabled(L, "NORD", rule()["id"], True)
    assert integrations.instance(L, body["channel"]).config["auto_issue"] == "true"
    assert rule()["reason"] == "shadow_mode" and c.intake(L, "NORD", body, "2026-10-31")["status"] == "draft"
    (home / "state" / "LIVE").touch()
    assert rule()["reason"] is None
    assert not [r for r in rules.list_rules(L, "STUDIO9") if r["kind"] == "shop_auto_issue"]  # the shop's own firm


@pytest.mark.parametrize(
    "change",
    [
        {"prices_include_vat": "true"},
        {"payment": {"method": [], "paid": True}},
        {"placed_at": "not-a-date"},
        {"lines": []},
        {"url": "https://example.invalid"},
    ],
)
def test_refuse_invalid_order(shop, change):
    L, body = shop
    with pytest.raises((Error, i.InvoiceError)):
        c.intake(L, "NORD", body | change, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0


def test_deleted_draft_cancels_order(shop):
    L, body = shop
    result = c.intake(L, "NORD", body, "2026-10-31")
    i.delete_draft(L, result["draft_id"], company="NORD", version=1)
    assert c.get(L, "NORD", result["id"])["status"] == "cancelled"
    assert c.intake(L, "NORD", body, "2026-10-31")["status"] == "cancelled"
    assert len(c.dispatch(L, "NORD", "orders_list", {}, {}, None, "2026-10-31")["data"]) == 1


def test_shop_never_overwrites_saved_client(shop):
    L, body = shop
    cid = i.save_client(L, "NORD", PARTY)
    before = L.db.execute("SELECT data FROM invoice_clients WHERE id=?", (cid,)).fetchone()[0]
    body = body | dict(buyer=body["buyer"] | dict(type="company", cui=PARTY["cui"]))
    with pytest.raises(Error) as err:
        c.intake(L, "NORD", body, "2026-10-31")
    assert err.value.code == "client_conflict"
    assert L.db.execute("SELECT data FROM invoice_clients WHERE id=?", (cid,)).fetchone()[0] == before


@pytest.mark.parametrize(
    "saved,typed,country,same",
    [
        ("Firma Fictivă SRL", "S.C. FIRMA FICTIVA S.R.L.", "RO", True),
        ("Firma Fictivă SRL", "Firma Fictivă", "RO", True),
        ("Spa SRL", "spa s.r.l.", "RO", True),
        ("Atelier Spa SRL", "Atelier Spa", "RO", True),
        ("Atelier SRL", "Atelier Spa", "RO", False),
        ("Atelier SRL", "Atelier SA", "RO", False),
        ("Atelier Spa Design SRL", "Atelier Design SRL", "RO", False),
        ("Atelier Spa SRL", "Atelier SRL", "RO", False),
        ("Atelier Fittizio S.p.A.", "Atelier Fittizio", "IT", True),
        ("...", "!!!", "RO", False),
    ],
)
def test_same_firm_names(saved, typed, country, same):
    assert c.same_firm(dict(name=saved, country=country), dict(name=typed)) is same
    assert not c.same_firm(dict(name=saved, country=country), dict(name=saved, private=True))


@pytest.mark.parametrize("name", ["S.C. FIRMA FICTIVA S.R.L.", "firma fictivă"])
def test_returning_firm_accepts_address_and_email_drift(shop, name):
    L, body = shop
    cid = i.save_client(L, "NORD", PARTY | dict(street="Strada Exemplu nr. 10"))
    before = L.db.execute("SELECT data FROM invoice_clients WHERE id=?", (cid,)).fetchone()[0]
    buyer = dict(type="company", name=name, cui=PARTY["cui"], email="comenzi@example.invalid")
    buyer["address"] = body["buyer"]["address"] | dict(street="Str. Exemplu 10")
    result = c.intake(L, "NORD", body | dict(buyer=buyer), "2026-10-31")
    assert c.get(L, "NORD", result["id"])["client_id"] == cid
    client = i.invoice(L, result["draft_id"], company="NORD")["data"]["client"]
    assert (client["street"], client["email"]) == ("Strada Exemplu nr. 10", PARTY["email"])
    assert L.db.execute("SELECT data FROM invoice_clients WHERE id=?", (cid,)).fetchone()[0] == before
    assert i.issue(L, result["draft_id"], "2026-10-31", company="NORD", version=1)["status"] == "issued"


@pytest.mark.parametrize("price,qty", [("1.99", "1"), ("0.99", "3"), ("33.33", "1")])
def test_gross_rounding_preserves_amount(shop, price, qty):
    L, body = shop
    from decimal import Decimal

    result = c.intake(
        L, "NORD", body | dict(lines=[body["lines"][0] | dict(unit_price=price, quantity=qty)]), "2026-10-31"
    )
    invoice = i.invoice(L, result["draft_id"], company="NORD")
    assert Decimal(invoice["data"]["totals"]["total"]) == Decimal(price) * Decimal(qty)
    issued = i.issue(L, result["draft_id"], "2026-10-31", company="NORD", version=1)
    assert issued["data"]["totals"] == invoice["data"]["totals"]


def test_live_auto_issue_and_refusal_preserves_draft(shop, home, monkeypatch):
    L, body = shop
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    integrations.update(L, body["channel"], {"auto_issue": "true"})
    assert c.intake(L, "NORD", body, "2026-10-31")["status"] == "issued"

    def refuse(*args, **kwargs):
        raise i.InvoiceError("simulated refusal")

    monkeypatch.setattr(i, "issue", refuse)
    second = c.intake(L, "NORD", body | dict(external_id="second"), "2026-10-31")
    assert second["status"] == "draft" and second["message"]


def test_rate_fetch_precedes_write_lock(shop, monkeypatch):
    from fiscalm import bnr

    L, body = shop

    def rate(*args, **kwargs):
        assert not L.db.in_transaction
        return "5", "2026-10-30"

    monkeypatch.setattr(bnr, "rate", rate)
    c.intake(L, "NORD", body | dict(currency="EUR"), "2026-10-31")


from test_api import server as server  # noqa: E402,F401
from test_api_v1 import request  # noqa: E402


def test_http_limits_auth_and_resources(token_owner, shop, server, monkeypatch):
    import json
    from datetime import date

    L, body = shop
    body = body | dict(placed_at=date.today().isoformat())
    writer = tokens.create(L, "Synthetic writer", "write", user_id=token_owner)
    reader = tokens.create(L, "Synthetic reader", "read", user_id=token_owner)
    path = "/companies/NORD/orders"
    assert request(server, reader["token"], path, "POST", body)[0] == 403
    raw = json.dumps(body).encode()
    code, result = request(server, writer["token"], path, "POST", raw + b" " * (c.LIMIT - len(raw)))
    assert code == 200
    assert request(server, writer["token"], path, "POST", raw + b" " * (c.LIMIT + 1 - len(raw)))[0] == 413
    assert request(server, writer["token"], "/companies/NORD/invoicing/client-save", "POST", b" " * 4097)[0] == 413
    assert request(server, reader["token"], path + "/" + result["id"])[0] == 200
    assert request(server, reader["token"], "/companies/STUDIO9/orders/" + result["id"])[0] == 404
    rows = request(
        server,
        reader["token"],
        path + "?month=" + body["placed_at"][:7] + "&channel=" + body["channel"] + "&status=draft",
    )[1]["data"]
    assert len(rows) == 1
    assert request(server, reader["token"], path + "?month=2020-01")[1]["data"] == []
    i.issue(L, result["draft_id"], date.today().isoformat(), company="NORD", version=1)
    assert (
        request(server, writer["token"], path, "POST", body | dict(payment=dict(method="card", paid=True)))[1]["error"][
            "code"
        ]
        == "order_changed"
    )
    tokens.revoke(L, writer["id"])
    assert request(server, writer["token"], path, "POST", body)[0] == 401


def test_disabled_and_distinct_channels(shop):
    L, body = shop
    first = c.intake(L, "NORD", body, "2026-10-31")
    second = integrations.add(L, "shop", "Other shop", dict(platform="custom", company="NORD", series="TEST"))
    assert c.intake(L, "NORD", body | dict(channel=second["id"]), "2026-10-31")["id"] != first["id"]
    integrations.set_enabled(L, second["id"], False)
    with pytest.raises(Error):
        c.intake(L, "NORD", body | dict(channel=second["id"]), "2026-10-31")


def test_vat_payer_zero_rate_refuses_without_guessing(shop):
    L, body = shop
    with pytest.raises(Error) as err:
        c.intake(L, "NORD", body | dict(lines=[body["lines"][0] | dict(vat_rate="0")]), "2026-10-31")
    assert err.value.code == "invalid_order"
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0
    assert L.db.execute("SELECT count(*) FROM commerce_orders WHERE status='refused'").fetchone()[0] == 1
