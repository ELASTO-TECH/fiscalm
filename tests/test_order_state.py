import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_commerce import (
    setup_invoice as setup_invoice,  # noqa: F401
    shop as shop,
)

from fiscalm.api import actions, state as api, tokens
from fiscalm.api.v1 import Error
from fiscalm.commerce import orders as c, state as commerce_state
from fiscalm.invoicing import engine as i
from fiscalm.ledger import AuthorizationRefused, Ledger


def refuse(L, body):
    with pytest.raises(Error) as err:
        c.intake(L, "NORD", body | dict(lines=[]), "2026-10-31")
    return err.value.order_id


def test_refusal_corrected_resend_keeps_identity_without_draft(shop):
    L, body = shop
    oid = refuse(L, body)
    order = c.get(L, "NORD", oid)
    assert order["status"] == "refused" and order["draft_id"] is None
    assert order["reason"]["code"] == "invalid_order"
    assert not L.db.execute("SELECT 1 FROM invoices").fetchone()
    assert refuse(L, body) == oid
    part = commerce_state.project(L, "NORD", [])
    assert part["order_tasks"][0]["id"] == "NORD:" + oid
    assert part["orders"][0]["can_discard"]
    result = c.intake(L, "NORD", body, "2026-10-31")
    assert result["id"] == oid and result["status"] == "draft"
    assert commerce_state.project(L, "NORD", [])["order_tasks"] == []
    assert c.get(L, "NORD", oid)["reason"] is None


def test_discard_is_scoped_and_cannot_cancel_accepted_order(shop):
    L, body = shop
    oid = refuse(L, body)
    with pytest.raises(Error):
        c.command(L, "STUDIO9", oid, "orders_discard", {}, "2026-10-31")
    discarded = c.command(L, "NORD", oid, "orders_discard", {}, "2026-10-31")
    assert discarded["status"] == "cancelled" and discarded["draft_id"] is None
    assert c.get(L, "NORD", oid)["draft_version"] is None
    assert c.command(L, "NORD", oid, "orders_discard", {}, "2026-10-31")["status"] == "cancelled"
    assert commerce_state.project(L, "NORD", [])["order_tasks"] == []
    with pytest.raises(Error):
        c.intake(L, "NORD", body, "2026-10-31")
    order = c.intake(L, "NORD", body | dict(external_id="second"), "2026-10-31")
    with pytest.raises(Error):
        c.command(L, "NORD", order["id"], "orders_discard", {}, "2026-10-31")
    assert c.get(L, "NORD", order["id"])["status"] == "draft"


def test_refused_and_corrected_race_cannot_downgrade_a_draft(shop):
    L, body = shop
    oid = refuse(L, body)
    barrier = Barrier(2)

    def submit(bad):
        other = Ledger()
        try:
            barrier.wait()
            if bad:
                try:
                    c.intake(other, "NORD", body | dict(lines=[]), "2026-10-31")
                except Error as exc:
                    return getattr(exc, "order_id", None)
            return c.intake(other, "NORD", body, "2026-10-31")["id"]
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        # A failed resend after correction must not replace the accepted draft.
        futures = [pool.submit(submit, x) for x in (False, True)]
        for f in futures:
            f.result()
    assert c.get(L, "NORD", oid)["status"] == "draft"
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1


def test_cod_requires_pickup_not_returned_and_outstanding(shop):
    L, body = shop
    order = c.intake(
        L, "NORD", body | dict(awb=dict(courier="Courier fictiv", number="../../not-a-link")), "2026-10-31"
    )
    iid = order["draft_id"]
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    rows = [dict(local_id=iid, owed="242.00", currency="RON", rejected=False)]
    assert commerce_state.project(L, "NORD", rows)["cod_receivable"]["count"] == 0
    c.command(
        L, "NORD", order["id"], "orders_delivery", dict(picked_up_at="2026-10-31", returned_at=None), "2026-10-31"
    )
    view = commerce_state.project(L, "NORD", rows)
    assert view["cod_receivable"] == dict(count=1, total="242.00", other=[])
    assert rows[0]["order_sheet_id"] == "NORD:" + order["id"]
    assert "../../" not in rows[0]["order_sheet_id"]
    rows[0]["owed"] = "0.00"
    assert commerce_state.project(L, "NORD", rows)["cod_receivable"]["count"] == 0
    rows[0]["owed"] = "242.00"
    c.command(
        L,
        "NORD",
        order["id"],
        "orders_delivery",
        dict(picked_up_at="2026-10-31", returned_at="2026-10-31"),
        "2026-10-31",
    )
    assert commerce_state.project(L, "NORD", rows)["cod_receivable"]["count"] == 0
    c.command(L, "NORD", order["id"], "orders_awb", dict(courier="Other", number="replacement"), "2026-10-31")
    assert commerce_state.project(L, "NORD", rows)["orders"][0]["delivery"]["picked_up_at"] is None


def test_origins_label_company_and_full_state_exposes_sheet(shop):
    from datetime import datetime

    L, body = shop
    oid = refuse(L, body)
    state = api.state(L, "ALL", datetime(2026, 10, 31))
    nord = next(p for p in state["parts"] if p["co"] == "NORD")
    assert nord["orders"][0]["sheet_id"] == "NORD:" + oid
    assert nord["order_tasks"][0]["sheet"] == "ord"
    origin = next(x for x in state["sales_origins"] if x["id"] == body["channel"])
    assert origin["company"] == "NORD" and "(NORD)" in origin["label"]


from test_api import server as server  # noqa: E402,F401
from test_api_v1 import request  # noqa: E402


def test_http_refusal_is_addressable_and_discard_needs_write_scope(token_owner, shop, server):
    L, body = shop
    writer = tokens.create(L, "Synthetic writer", "write", user_id=token_owner)["token"]
    reader = tokens.create(L, "Synthetic reader", "read", user_id=token_owner)["token"]
    code, result = request(server, writer, "/companies/NORD/orders", "POST", body | dict(lines=[]))
    assert code == 400 and result["order_id"]
    oid = result["order_id"]
    assert request(server, reader, "/companies/NORD/orders/" + oid)[1]["status"] == "refused"
    path = "/companies/NORD/orders/" + oid + "/discard"
    assert request(server, reader, path, "POST", {})[0] == 403
    assert request(server, writer, path, "POST", {}, Origin="https://other.example.invalid")[0] == 403
    assert request(server, writer, path, "POST", {})[1]["status"] == "cancelled"


def test_app_discard_action_shadow_and_untrusted_fields(shop, home):
    L, body = shop
    oid = refuse(L, body)
    assert not actions.act("order-discard", "STUDIO9", {"id": oid})["ok"]
    assert not actions.act("order-discard", "NORD", {"id": oid, "url": "https://example.invalid"})["ok"]
    assert actions.act("order-discard", "NORD", {"id": oid}, created_by="synthetic-user")["ok"]
    assert "synthetic-user" in "".join(p.read_text() for p in (home / "logs").glob("*.log"))
    assert c.get(L, "NORD", oid)["status"] == "cancelled"


def test_refused_nonfinite_input_cannot_poison_state_json(shop):
    import json

    L, body = shop
    with pytest.raises(Error):
        c.intake(L, "NORD", body | dict(lines=[body["lines"][0] | dict(quantity=float("nan"))]), "2026-10-31")
    state = commerce_state.project(L, "NORD", [])
    json.dumps(state, allow_nan=False)
    assert state["orders"][0]["status"] == "refused"


def test_identical_currency_retry_after_refusal_reloads_rate(shop, monkeypatch):
    from fiscalm import bnr

    L, body = shop
    body = body | dict(currency="EUR")
    oid = c.keep_refusal(L, "NORD", body, "rate_unavailable", "synthetic previous refusal", "2026-10-31", lambda: None)
    calls = []
    monkeypatch.setattr(bnr, "rate", lambda *args, **kwargs: calls.append(True) or ("5", "2026-10-30"))
    result = c.intake(L, "NORD", body, "2026-10-31")
    assert calls and result["id"] == oid and result["status"] == "draft"
    assert i.invoice(L, result["draft_id"], company="NORD")["data"]["exchange_rate"] == "5"


def test_temporary_rate_failure_does_not_create_refused_task(shop, monkeypatch):
    from fiscalm import bnr

    L, body = shop

    def unavailable(*args, **kwargs):
        raise bnr.RateError("synthetic unavailable")

    monkeypatch.setattr(bnr, "rate", unavailable)
    with pytest.raises(Error) as err:
        c.dispatch(L, "NORD", "orders_create", {}, {}, body | dict(currency="EUR"), "2026-10-31")
    assert err.value.code == "rate_unavailable"
    assert commerce_state.project(L, "NORD", [])["order_tasks"] == []
    assert not L.db.execute("SELECT 1 FROM commerce_orders").fetchone()


def test_refusal_storage_failure_preserves_validation_error(shop, monkeypatch):
    import sqlite3

    L, body = shop

    def busy(*args):
        raise sqlite3.OperationalError("synthetic busy")

    monkeypatch.setattr(c, "keep_refusal", busy)
    with pytest.raises(Error) as err:
        c.intake(L, "NORD", body | dict(lines=[]), "2026-10-31")
    assert err.value.code == "invalid_order"


def test_draft_and_refund_keep_opaque_origin_relations(shop):
    from fiscalm.invoicing import workspace as invoicing_api

    L, body = shop
    order = c.intake(L, "NORD", body, "2026-10-31")
    draft = invoicing_api.draft_out(L, i.invoice(L, order["draft_id"], company="NORD"))
    assert draft["order_sheet_id"] == "NORD:" + order["id"]
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    refund = c.command(L, "NORD", order["id"], "orders_refund", {}, "2026-10-31")
    draft = invoicing_api.draft_out(L, i.invoice(L, refund["draft_id"], company="NORD"))
    assert draft["sales_origin"] == body["channel"] and draft["order_id"] == order["id"]
    i.issue(L, refund["draft_id"], "2026-10-31", company="NORD", version=1)
    rows = [dict(local_id=refund["draft_id"], owed="0.00", currency="RON", rejected=False)]
    commerce_state.project(L, "NORD", rows)
    assert rows[0]["sales_origin"] == body["channel"] and not rows[0]["cod_at_courier"]
    assert c.get(L, "NORD", order["id"])["status"] == "issued"


def test_real_state_aggregates_cod_across_companies_and_currencies(shop, monkeypatch):
    from datetime import datetime

    from test_invoicing import PARTY

    from fiscalm import bnr, integrations

    L, body = shop
    i.save_seller(L, "STUDIO9", PARTY | {"iban": "RO94BTRLRONCRT0000000002"})
    i.create_series(L, "STUDIO9", "TEST")
    second = integrations.add(
        L, "shop", "Synthetic second shop", dict(platform="custom", company="STUDIO9", series="TEST")
    )
    monkeypatch.setattr(bnr, "rate", lambda *args, **kwargs: ("5", "2026-10-30"))
    for co, channel, currency in [("NORD", body["channel"], "RON"), ("STUDIO9", second["id"], "EUR")]:
        order = c.intake(
            L,
            co,
            body | dict(channel=channel, currency=currency, awb=dict(courier="Synthetic", number="awb")),
            "2026-10-31",
        )
        i.issue(L, order["draft_id"], "2026-10-31", company=co, version=1)
        c.command(
            L, co, order["id"], "orders_delivery", dict(picked_up_at="2026-10-31", returned_at=None), "2026-10-31"
        )
    state = api.state(L, "ALL", datetime(2026, 10, 31))
    assert state["cod_receivable"] == dict(total="242.00", other=[dict(currency="EUR", total="242.00")], count=2)
    assert all(part["issued"][0]["cod_at_courier"] for part in state["parts"])


@pytest.mark.parametrize(
    "delivery",
    [
        dict(picked_up_at="2026-11-01", returned_at=None),
        dict(picked_up_at="2026-10-30", returned_at=None),
        dict(picked_up_at=None, returned_at="2026-10-31"),
        dict(picked_up_at="2026-10-31", returned_at="2026-10-30"),
    ],
)
def test_delivery_refusals_leave_no_facts(shop, delivery):
    L, body = shop
    order = c.intake(L, "NORD", body | dict(awb=dict(courier="Synthetic", number="awb")), "2026-10-31")
    with pytest.raises(Error):
        c.command(L, "NORD", order["id"], "orders_delivery", delivery, "2026-10-31")
    assert not L.db.execute("SELECT 1 FROM commerce_delivery").fetchone()


def test_connector_can_explicitly_suppress_auto_issue(shop, home, monkeypatch):
    from fiscalm import integrations

    L, body = shop
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    integrations.update(L, body["channel"], {"auto_issue": "true"})
    monkeypatch.setattr(i, "issue", lambda *args, **kwargs: pytest.fail("connector suppressed issuance"))
    assert c.intake(L, "NORD", body, "2026-10-31", auto_issue=False)["status"] == "draft"


def test_app_order_discard_checks_authority_while_holding_write_lock(shop):
    L, body = shop
    oid = refuse(L, body)
    L.db.execute("PRAGMA busy_timeout=10")
    checks = []

    def deny(guarded_ledger):
        assert guarded_ledger.db.in_transaction
        with pytest.raises(sqlite3.OperationalError), L.tx():
            pytest.fail("The guarded writer must hold the lock on its own connection")
        checks.append(True)
        raise PermissionError("account_disabled")

    with pytest.raises(AuthorizationRefused):
        actions.act("order-discard", "NORD", {"id": oid}, authorize=deny)
    assert checks == [True] and c.get(L, "NORD", oid)["status"] == "refused"
