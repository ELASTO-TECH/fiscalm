import hashlib
import hmac
import json

import pytest
from test_commerce import (
    setup_invoice as setup_invoice,  # noqa: F401
    shop as shop,
)

from fiscalm import integrations
from fiscalm.api.v1 import Error
from fiscalm.commerce import orders as c, webhooks as w
from fiscalm.invoicing import engine as i


@pytest.fixture
def live_shop(shop, home):
    L, body = shop
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    integrations.update(
        L,
        body["channel"],
        dict(webhook_url="https://shop.example.invalid/hook", webhook_secret="synthetic-signing-secret"),
    )
    return L, body


def test_issue_journals_signed_event_and_retry(live_shop):
    L, body = live_shop
    order = c.intake(L, "NORD", body, "2026-10-31")
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    assert L.db.execute("SELECT count(*) FROM commerce_outbox").fetchone()[0] == 1
    calls = []

    def send(url, raw, headers):
        assert not L.db.in_transaction
        assert url == "https://shop.example.invalid/hook"
        assert (
            headers["X-Fiscalm-Signature"]
            == "sha256="
            + hmac.new(
                b"synthetic-signing-secret", headers["X-Fiscalm-Timestamp"].encode() + b"." + raw, hashlib.sha256
            ).hexdigest()
        )
        calls.append(json.loads(raw))
        return 503 if len(calls) == 1 else 204

    assert w.deliver(L, 100, send) == dict(sent=0, failed=1)
    assert w.deliver(L, 101, send) == dict(sent=0, failed=0)
    assert w.deliver(L, 160, send) == dict(sent=1, failed=0)
    assert calls[0] == calls[1] and calls[0]["type"] == "invoice.issued"
    assert calls[0]["invoice_id"] == order["draft_id"]
    assert "buyer" not in str(calls)
    assert w.deliver(L, 500, send) == dict(sent=0, failed=0)


def test_crash_after_claim_retries_same_event(live_shop):
    L, body = live_shop
    w.refused(L, "NORD", body, "invalid_order")
    event = L.db.execute("SELECT body FROM commerce_outbox").fetchone()[0]

    def crash(*args):
        raise KeyboardInterrupt("simulated process death")

    with pytest.raises(KeyboardInterrupt):
        w.deliver(L, 100, crash)
    calls = []
    assert w.deliver(L, 101, lambda *args: calls.append(args) or 204)["sent"] == 0
    assert w.deliver(L, 160, lambda *args: calls.append(args) or 204)["sent"] == 1
    assert calls[0][1].decode() == event


def test_redirects_are_never_followed(live_shop, monkeypatch):
    L, body = live_shop
    w.refused(L, "NORD", body, "invalid_order")
    requests = []

    def opened(req, **kwargs):
        requests.append(req)
        w.NoRedirect().redirect_request(req, None, 302, "Moved", {}, "https://other.example.invalid")

    monkeypatch.setattr(w._OPENER, "open", opened)
    assert w.deliver(L, 100)["failed"] == 1
    assert len(requests) == 1
    assert L.db.execute("SELECT sent_at FROM commerce_outbox").fetchone()[0] is None


def test_shadow_and_disabled_channels_send_nothing(live_shop, home):
    L, body = live_shop
    w.refused(L, "NORD", body, "invalid_order")
    (home / "state" / "LIVE").unlink()
    assert w.deliver(L, 100, lambda *args: pytest.fail("shadow effect"))["sent"] == 0
    (home / "state" / "LIVE").touch()
    integrations.set_enabled(L, body["channel"], False)
    assert w.deliver(L, 100, lambda *args: pytest.fail("disabled channel"))["sent"] == 0


def test_refusal_journal_and_write_only_destination(live_shop):
    L, body = live_shop
    with pytest.raises(Error):
        c.dispatch(L, "NORD", "orders_create", {}, {}, body | dict(prices_include_vat="bad"), "2026-10-31")
    event = json.loads(L.db.execute("SELECT body FROM commerce_outbox").fetchone()[0])
    assert event["type"] == "order.refused" and event["reason"] == "invalid_order"
    public = str(integrations.listing(L, "shop"))
    assert "https://shop.example.invalid" not in public and "synthetic-signing-secret" not in public
    assert not L.db.execute("SELECT 1 FROM invoices").fetchone()


def test_recovery_spv_and_confirmed_collection_events(live_shop):
    from fiscalm.efactura import client as anaf

    L, body = live_shop
    order = c.intake(L, "NORD", body, "2026-10-31")
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    L.db.execute("DELETE FROM commerce_outbox")  # emulate legacy issuance before journal hook
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,'rejected','synthetic')",
            (order["draft_id"], anaf.env()),
        )
        w.capture(L)
    with L.tx() as db:
        db.execute("UPDATE invoice_submissions SET status='accepted'")
        w.capture(L)
    L.db.execute(
        "INSERT INTO issued(message_id,company,raw_path,status,created_at) VALUES('webhook-credit','NORD','synthetic','open','2026-10-31')"
    )
    L.db.execute("INSERT INTO invoice_sent_links VALUES(?,?,?)", ("webhook-credit", order["draft_id"], anaf.env()))
    assert L.mark_issued_collected("webhook-credit", "synthetic credit", "synthetic-tx")
    events = [json.loads(r[0])["type"] for r in L.db.execute("SELECT body FROM commerce_outbox")]
    assert sorted(events) == ["invoice.collected", "invoice.issued", "invoice.spv_accepted", "invoice.spv_rejected"]
    w.capture(L)
    assert L.db.execute("SELECT count(*) FROM commerce_outbox").fetchone()[0] == 4


def test_config_requires_url_and_secret_together(shop):
    L, body = shop
    with pytest.raises(integrations.IntegrationError):
        integrations.update(L, body["channel"], {"webhook_url": "https://shop.example.invalid/hook"})
    assert not integrations.enabled(L, "shop")[0].has("webhook_url")


def test_two_workers_claim_one_delivery(live_shop):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from fiscalm.ledger import Ledger

    L, body = live_shop
    w.refused(L, "NORD", body, "invalid_order")
    entered, release = Event(), Event()
    calls = []

    def transport(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return 204

    def worker():
        other = Ledger()
        try:
            return w.deliver(other, 100, transport)
        finally:
            other.close()

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker)
        assert entered.wait(5)
        try:
            assert w.deliver(L, 100, lambda *args: pytest.fail("duplicate claim"))["sent"] == 0
        finally:
            release.set()
        assert future.result()["sent"] == 1
    assert len(calls) == 1


def test_unconfigured_backlog_cannot_starve_configured_shop(live_shop):
    L, body = live_shop
    other = integrations.add(L, "shop", "No webhook", dict(platform="custom", company="NORD", series="TEST"))
    for n in range(51):
        w.refused(L, "NORD", body | dict(channel=other["id"], external_id=f"ignored-{n}"), "invalid_order")
    w.refused(L, "NORD", body, "invalid_order")
    assert w.deliver(L, 100, lambda *args: 204) == dict(sent=1, failed=0)


def test_bad_config_isolated_and_backed_off(live_shop, monkeypatch):
    L, body = live_shop
    other = integrations.add(
        L,
        "shop",
        "Broken destination",
        dict(
            platform="custom",
            company="NORD",
            series="TEST",
            webhook_url="https://broken.example.invalid",
            webhook_secret="synthetic",
        ),
    )
    w.refused(L, "NORD", body | dict(channel=other["id"]), "invalid_order")
    w.refused(L, "NORD", body, "invalid_order")
    original = integrations.Instance.secret

    def secret(instance, key):
        if instance.id == other["id"]:
            raise OSError("missing synthetic credential")
        return original(instance, key)

    monkeypatch.setattr(integrations.Instance, "secret", secret)
    assert w.deliver(L, 100, lambda *args: 204) == dict(sent=1, failed=1)
    assert L.db.execute("SELECT next_at FROM commerce_outbox WHERE channel=?", (other["id"],)).fetchone()[0] == 160


def test_transient_rate_and_changed_existing_order_emit_no_refusal(live_shop, monkeypatch):
    from fiscalm import bnr

    L, body = live_shop

    def unavailable(*args, **kwargs):
        raise bnr.RateError("synthetic unavailable")

    monkeypatch.setattr(bnr, "rate", unavailable)
    with pytest.raises(Error) as err:
        c.dispatch(L, "NORD", "orders_create", {}, {}, body | dict(currency="EUR"), "2026-10-31")
    assert err.value.code == "rate_unavailable"
    assert not L.db.execute("SELECT 1 FROM commerce_outbox").fetchone()
    order = c.intake(L, "NORD", body, "2026-10-31")
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    with pytest.raises(Error):
        c.dispatch(
            L, "NORD", "orders_create", {}, {}, body | dict(payment=dict(method="card", paid=True)), "2026-10-31"
        )
    assert all(json.loads(r[0])["type"] != "order.refused" for r in L.db.execute("SELECT body FROM commerce_outbox"))


def test_refusal_journal_failure_preserves_input_error(live_shop, monkeypatch):
    import sqlite3

    L, body = live_shop

    def broken(*args):
        raise sqlite3.OperationalError("synthetic busy")

    monkeypatch.setattr(w, "refused", broken)
    with pytest.raises(Error) as err:
        c.dispatch(L, "NORD", "orders_create", {}, {}, body | dict(prices_include_vat="bad"), "2026-10-31")
    assert err.value.status == 400 and err.value.code == "invalid_order"


def test_upload_rejection_is_journalled_before_retry(live_shop):
    import anaf_fake

    from fiscalm.invoicing import spv as invoice_spv

    L, body = live_shop
    anaf_fake.setup(L)
    order = c.intake(L, "NORD", body, "2026-10-31")
    i.issue(L, order["draft_id"], "2026-10-31", company="NORD", version=1)
    state = invoice_spv.upload(L, order["draft_id"], lambda *args: b'<header ExecutionStatus="1"/>', company="NORD")
    assert state["status"] == "rejected"
    assert "invoice.spv_rejected" in [
        json.loads(r[0])["type"] for r in L.db.execute("SELECT body FROM commerce_outbox")
    ]
