"""Durable, at-least-once shop notifications. Only configured destinations are used."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from uuid import uuid4

from fiscalm import integrations
from fiscalm.ledger import Ledger, is_live


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(NoRedirect)


def send(url: str, body: bytes, headers: dict[str, str]) -> int:
    if not is_live():
        raise ValueError("Webhooks require a live installation")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with _OPENER.open(request, timeout=10) as response:
        return response.status


def enqueue(
    L: Ledger, order: dict, event: str, key: str, invoice_id: str | None = None, reason: str | None = None
) -> None:
    """Persist one stable event; order data never supplies a destination or secret."""
    if not is_live():
        return
    iid = uuid4().hex
    body = dict(
        id=iid,
        type=event,
        company=order["company"],
        order_id=order["id"],
        external_id=order["external_id"],
        channel=order["channel"],
        invoice_id=invoice_id,
    )
    if reason:
        body["reason"] = reason
    with L.tx() as db:
        db.execute(
            "INSERT OR IGNORE INTO commerce_outbox(id,event_key,channel,company,body) VALUES(?,?,?,?,?)",
            (iid, key, order["channel"], order["company"], json.dumps(body, separators=(",", ":"), ensure_ascii=False)),
        )


def capture(L: Ledger, invoice_id: str | None = None, message_id: str | None = None) -> None:
    """Recover events from durable invoice state, including issue/collection before a crash."""
    if not is_live():
        return
    from fiscalm.efactura import client as anaf
    from fiscalm.invoicing import engine as inv, spv as invoice_spv

    if message_id:
        for link in L.db.execute("SELECT invoice_id FROM invoice_sent_links WHERE message_id=?", (message_id,)):
            capture(L, invoice_id=link[0])
        return
    query, args = "SELECT * FROM commerce_orders", ()
    if invoice_id:
        invoice = L.db.execute("SELECT company,data FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not invoice:
            return
        reference = json.loads(invoice["data"]).get("billing_ref", "")
        query = "SELECT o.* FROM commerce_orders o JOIN invoices i ON i.id=o.draft_id AND i.company=o.company WHERE o.company=? AND (o.draft_id=? OR i.series || '-' || i.number=?)"
        args = (invoice["company"], invoice_id, reference)
    with L.tx():
        for row in L.db.execute(query, args):
            order = dict(row)
            original = L.db.execute(
                "SELECT * FROM invoices WHERE id=? AND company=? AND status!='draft'", (row["draft_id"], row["company"])
            ).fetchone()
            if not original:
                continue
            number = inv.invoice_number(dict(original))
            invoices = L.db.execute(
                "SELECT id,data FROM invoices WHERE company=? AND status!='draft' AND (id=? OR json_extract(data,'$.billing_ref')=?)",
                (row["company"], row["draft_id"], number),
            ).fetchall()
            for invoice in invoices:
                if not json.loads(invoice["data"]).get("issued_live"):
                    continue
                iid = invoice["id"]
                base = row["id"] + ":" + iid
                enqueue(L, order, "invoice.issued", base + ":issued", iid)
                state = invoice_spv.submission(L, iid)
                if state and state["status"] in ("accepted", "rejected"):
                    event = "invoice.spv_" + state["status"]
                    enqueue(L, order, event, base + ":" + event + ":" + anaf.env(), iid)
                collected = L.db.execute(
                    "SELECT 1 FROM issued s JOIN invoice_sent_links l ON l.message_id=s.message_id WHERE l.invoice_id=? AND l.environment=? AND s.company=? AND s.status='collected'",
                    (iid, anaf.env(), row["company"]),
                ).fetchone()
                if collected:
                    enqueue(L, order, "invoice.collected", base + ":collected", iid)


def refused(L: Ledger, co: str, body: object, code: str) -> None:
    if not is_live() or not isinstance(body, dict):
        return
    channel, external = body.get("channel"), body.get("external_id")
    if not isinstance(channel, str) or not isinstance(external, str) or not external.strip() or len(external) > 200:
        return
    if not any(x.id == channel and x.config["company"] == co for x in integrations.enabled(L, "shop")):
        return
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    row = L.db.execute(
        "SELECT id,status FROM commerce_orders WHERE company=? AND channel=? AND external_id=?", (co, channel, external)
    ).fetchone()
    if row and row["status"] != "refused":
        return
    order = dict(id=row["id"] if row else None, company=co, channel=channel, external_id=external)
    enqueue(L, order, "order.refused", f"{co}:{channel}:{digest}:{code}", reason=code)


def deliver(L: Ledger, stamp: int, transport: Callable[[str, bytes, dict[str, str]], int] = send) -> dict:
    """Commit a timed claim before sending. Crashed or failed attempts retry with backoff."""
    if L.db.in_transaction:
        raise ValueError("Webhook delivery requires its own committed transaction.")
    if not is_live():
        return dict(sent=0, failed=0)
    capture(L)
    sent = failed = 0
    started = time.monotonic()
    rows = L.db.execute(
        "SELECT o.id FROM commerce_outbox o JOIN integrations i ON i.id=o.channel AND i.kind='shop' AND i.enabled=1 AND json_extract(i.config,'$.company')=o.company WHERE o.sent_at IS NULL AND o.next_at<=? AND json_extract(i.config,'$._v.webhook_url') IS NOT NULL AND json_extract(i.config,'$._v.webhook_secret') IS NOT NULL ORDER BY o.next_at,o.id LIMIT 50",
        (stamp,),
    ).fetchall()
    for row in rows:
        if time.monotonic() - started >= 30:
            break
        current = stamp + int(time.monotonic() - started)
        lease = uuid4().hex
        with L.tx() as db:
            if not is_live():
                break
            event = db.execute(
                "SELECT * FROM commerce_outbox WHERE id=? AND sent_at IS NULL AND next_at<=?", (row["id"], current)
            ).fetchone()
            if not event:
                continue
            shop = next(
                (
                    x
                    for x in integrations.enabled(L, "shop")
                    if x.id == event["channel"] and x.config["company"] == event["company"]
                ),
                None,
            )
            if not shop:
                continue
            attempts = event["attempts"] + 1
            retry_at = current + max(60, min(3600, 2 ** min(attempts, 12)))
            db.execute(
                "UPDATE commerce_outbox SET attempts=?,next_at=?,lease=? WHERE id=?",
                (attempts, retry_at, lease, row["id"]),
            )
            try:
                url, secret = shop.secret("webhook_url"), shop.secret("webhook_secret")
                integrations._check_url(url, integrations.Field("webhook_url", "Webhook", kind="url"))
            except Exception:
                failed += 1
                continue
        body = event["body"].encode("utf-8")
        signature = hmac.new(secret.encode(), str(current).encode() + b"." + body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Fiscalm-Timestamp": str(current),
            "X-Fiscalm-Signature": "sha256=" + signature,
            "X-Fiscalm-Event-Id": event["id"],
        }
        try:
            if not is_live():
                break
            status = transport(url, body, headers)
            success = 200 <= status < 300
        except Exception:
            # Never log a destination, response, exception text or credential.
            success = False
        with L.tx() as db:
            if success:
                db.execute(
                    "UPDATE commerce_outbox SET sent_at=? WHERE id=? AND lease=? AND sent_at IS NULL",
                    (current, row["id"], lease),
                )
                sent += 1
            else:
                failed += 1
    return dict(sent=sent, failed=failed)


def tick(L: Ledger) -> dict:
    return deliver(L, int(time.time()))


def note_refusal(L: Ledger, co: str, body: object, code: str) -> None:
    with contextlib.suppress(sqlite3.Error, ValueError, OSError):
        refused(L, co, body, code)
