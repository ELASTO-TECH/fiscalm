"""Journal eMAG work before effects; replay intake safely and retry PDF attachment URLs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from fiscalm import integrations
from fiscalm.api.responses import Error
from fiscalm.commerce import emag, orders as commerce
from fiscalm.invoicing import engine as invoicing
from fiscalm.ledger import Ledger, is_live


def note(L: Ledger, shop: integrations.Instance, external: str, reason: str | None, context: str | None = None) -> str:
    with L.tx() as db:
        db.execute(
            "INSERT INTO emag_orders VALUES(?,?,?,?,?) ON CONFLICT(channel,company,external_id) DO UPDATE SET reason=excluded.reason",
            (shop.id, shop.config["company"], external, uuid4().hex, reason),
        )
        task_id = db.execute(
            "SELECT id FROM emag_orders WHERE channel=? AND company=? AND external_id=?",
            (shop.id, shop.config["company"], external),
        ).fetchone()[0]
        if context is not None:
            db.execute("INSERT OR REPLACE INTO emag_context VALUES(?,?)", (task_id, context))
        return task_id


def ingest(L: Ledger, shop: integrations.Instance, row: dict, today: str, api=None) -> bool:
    external = emag.identifier(row["id"])
    co = shop.config["company"]
    old = L.db.execute(
        "SELECT id FROM commerce_orders WHERE company=? AND channel=? AND external_id=?", (co, shop.id, external)
    ).fetchone()
    if row.get("type") != 3 or type(row.get("status")) is not int or row["status"] not in range(6):
        raise emag.Refused("order_type_or_status")
    if row["status"] == 0 and not old:
        note(L, shop, external, None)
        return False
    if row["status"] in (0, 5) or row.get("is_storno"):
        if old and row["status"] == 0:
            commerce.command(L, co, old[0], "orders_cancel", {}, today)
            note(L, shop, external, None)
        else:
            note(L, shop, external, "return_review" if old else "cancelled_or_returned")
        return False
    body = emag.translate(shop, row)

    def authorize() -> None:
        if commerce.channel(L, co, shop.id) != shop:
            raise emag.Refused("channel_changed")

    result = commerce.intake(L, co, body, today, authorize)
    pending = L.db.execute(
        "SELECT attempts FROM emag_attachments WHERE channel=? AND company=? AND external_id=? AND sent_at IS NULL",
        (shop.id, co, external),
    ).fetchone()
    note(
        L,
        shop,
        external,
        "attachment_retry"
        if pending and pending[0]
        # A draft waiting for its VAT treatment has its own task; only a refused issue is eMAG's.
        else "auto_issue_refused"
        if result["message"] not in ("", commerce.VAT_REVIEW["message"])
        else None,
    )
    try:
        # eMAG courier reservations are identifiers only; never fetch their response URLs.
        for reservation in [] if old and commerce.get(L, co, old[0])["awb"] else row.get("awbs") or []:
            barcode = int(emag.identifier(reservation["emag_id"]))
            awbs = (api or emag.Client(shop))("awb/read", {"emag_id": barcode})
            if len(awbs) != 1 or awbs[0].get("order_id") != row["id"]:
                raise emag.Refused("awb_order")
            detail = awbs[0]
            if len(detail["awb"]) != 1 or len(detail["courier"]) != 1:
                raise emag.Refused("ambiguous_awb")
            commerce.command(
                L,
                co,
                result["id"],
                "orders_awb",
                dict(number=detail["awb"][0]["awb_number"], courier=detail["courier"][0]["courier_name"]),
                today,
            )
    except Exception as exc:
        note(L, shop, external, "awb_retry")
        return (
            not isinstance(exc, (emag.Refused, Error, ValueError, KeyError, TypeError)) or str(exc) == "api_unavailable"
        )
    return False


def attachments(L: Ledger, shop: integrations.Instance, stamp: int, api) -> None:
    if not is_live():
        return
    co = shop.config["company"]
    with L.tx() as db:
        for row in db.execute(
            "SELECT o.*,i.data FROM commerce_orders o JOIN invoices i ON i.id=o.draft_id AND i.company=o.company WHERE o.channel=? AND o.company=? AND i.status='issued'",
            (shop.id, co),
        ).fetchall():
            if json.loads(row["data"]).get("issued_live"):
                db.execute(
                    "INSERT OR IGNORE INTO emag_attachments(invoice_id,channel,company,external_id,token) VALUES(?,?,?,?,?)",
                    (row["draft_id"], shop.id, co, row["external_id"], secrets.token_hex(32)),
                )
    for row in L.db.execute(
        "SELECT * FROM emag_attachments WHERE channel=? AND company=? AND sent_at IS NULL AND next_at<=? LIMIT 50",
        (shop.id, co, stamp),
    ).fetchall():
        with L.tx() as db:
            claimed = db.execute(
                "UPDATE emag_attachments SET attempts=attempts+1,next_at=? WHERE invoice_id=? AND sent_at IS NULL AND next_at<=?",
                (stamp + 3600, row["invoice_id"], stamp),
            ).rowcount
        if not claimed:
            continue
        try:
            current = commerce.channel(L, co, shop.id)
            if current != shop or current.config["platform"] != "emag":
                raise emag.Refused("channel_changed")
            base = shop.config["emag_pdf_base"].rstrip("/")
            if not base.startswith("https://"):
                raise emag.Refused("pdf_base")
            url = base + "/emag-pdf/" + row["token"] + ".pdf"
            if not is_live():
                return
            api(
                "order/attachments/save",
                {"data": [dict(order_id=int(row["external_id"]), order_type=3, name="Factura.pdf", type=1, url=url)]},
            )
            with L.tx() as db:
                db.execute("UPDATE emag_attachments SET sent_at=? WHERE invoice_id=?", (stamp, row["invoice_id"]))
            with L.tx() as db:
                db.execute(
                    "UPDATE emag_orders SET reason=NULL WHERE channel=? AND company=? AND external_id=? AND reason='attachment_retry'",
                    (shop.id, co, row["external_id"]),
                )
        except Exception:
            with L.tx() as db:
                db.execute(
                    "UPDATE emag_orders SET reason='attachment_retry' WHERE channel=? AND company=? AND external_id=? AND reason IS NULL",
                    (shop.id, co, row["external_id"]),
                )


def pdf(L: Ledger, token: str) -> bytes | None:
    """An expiring capability reads exactly one company-bound immutable invoice."""
    if not is_live() or not re.fullmatch("[a-f0-9]{64}", token):
        return None
    row = L.db.execute("SELECT * FROM emag_attachments WHERE token=?", (token,)).fetchone()
    if not row or (row["sent_at"] is not None and row["sent_at"] + 86400 < int(time.time())):
        return None
    shop = next(
        (
            s
            for s in integrations.enabled(L, "shop")
            if s.id == row["channel"] and s.config["platform"] == "emag" and s.config["company"] == row["company"]
        ),
        None,
    )
    if not shop:
        return None
    from fiscalm.invoicing.pdf import render

    return render(L, row["invoice_id"], company=row["company"])


def tick(L: Ledger, today: str | None = None, stamp: int | None = None, factory=emag.Client) -> dict:
    today, stamp = today or date.today().isoformat(), int(time.time()) if stamp is None else stamp
    done = failed = 0
    for shop in integrations.enabled(L, "shop"):
        if shop.config["platform"] != "emag":
            continue
        lock = (L.path.parent / ("emag-" + shop.id + ".lock")).open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            continue
        started = time.monotonic()
        try:
            api = factory(shop)
            attachments(L, shop, stamp, api)
            since = shop.config["emag_since"]
            scope = hashlib.sha256((shop.id + ":" + shop.config["company"] + ":" + since).encode()).hexdigest()
            cursor_key = "emag-cursor:" + scope
            cursor = L.get_meta(cursor_key, since)
            overlap = (datetime.strptime(cursor, "%Y-%m-%d %H:%M:%S") - timedelta(minutes=5)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            watermark = datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M:%S")
            retry_pending = False
            # UTC is conservative if upstream uses Bucharest time: the overlap gets wider.
            for row in emag.pages(
                api, "order/read", {"type": 3, "createdAfter": since, "modifiedAfter": max(since, overlap)}
            ):
                if time.monotonic() - started > 480:
                    raise emag.Refused("tick_budget")
                try:
                    if not isinstance(row, dict):
                        raise emag.Refused("invalid_payload")
                    if not isinstance(row.get("date"), str) or row["date"] < since:
                        continue
                    retry_pending |= ingest(L, shop, row, today, api)
                    done += 1
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, emag.Refused) else "order_review"
                    try:
                        external = emag.identifier(row.get("id")) if isinstance(row, dict) else "invalid-payload"
                    except emag.Refused:
                        external = "invalid-payload"
                    note(
                        L, shop, external, reason, hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                    )
                    if isinstance(exc, emag.Refused):
                        retry_pending |= str(exc) in ("api_unavailable", "channel_changed", "tick_budget")
                    elif isinstance(exc, Error):
                        retry_pending |= exc.code == "rate_unavailable"
                    else:
                        retry_pending |= not isinstance(
                            exc, (invoicing.InvoiceError, ValueError, KeyError, TypeError, AttributeError)
                        )
                    failed += 1
            if not retry_pending:
                with L.tx():
                    L.set_meta(cursor_key, max(cursor, watermark))
            returns: dict[str, set[str]] = {}
            for row in emag.pages(api, "rma/read", {"type": 3, "date_start": since}):
                try:
                    external = emag.identifier(row["order_id"])
                    order = L.db.execute(
                        "SELECT draft_id FROM commerce_orders WHERE company=? AND channel=? AND external_id=?",
                        (shop.config["company"], shop.id, external),
                    ).fetchone()
                    if order:
                        active = returns.setdefault(external, set())
                        if row.get("request_status") in (2, 3, 6, 7):
                            active.add(emag.identifier(row["emag_id"]))
                except Exception:
                    note(L, shop, "invalid-rma", "order_review")
            for external, active in returns.items():
                if active:
                    note(L, shop, external, "return_review", "rma:" + ",".join(sorted(active)))
            attachments(L, shop, stamp, api)
            note(L, shop, "sync", None)
        except Exception:
            # No upstream messages, URLs, credentials or buyer data in tick output.
            note(L, shop, "sync", "sync_retry")
            failed += 1
        finally:
            lock.close()
    return dict(orders=done, failed=failed)


def tasks(L: Ledger, co: str) -> list[dict]:
    return [
        dict(row)
        for row in L.db.execute(
            """
        SELECT e.id,e.external_id,e.reason,o.id AS order_id,o.draft_id
        FROM emag_orders e LEFT JOIN commerce_orders o
          ON o.channel=e.channel AND o.company=e.company AND o.external_id=e.external_id
        LEFT JOIN invoices original ON original.id=o.draft_id AND original.company=o.company
        WHERE e.company=? AND e.external_id!='sync' AND e.reason IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM emag_decisions d WHERE d.id=e.id
            AND d.reason=e.reason || ':' || COALESCE((SELECT value FROM emag_context c WHERE c.id=e.id),''))
          AND NOT (e.reason='return_review' AND (
            EXISTS (SELECT 1 FROM invoice_refund_drafts r WHERE r.company=e.company AND r.original_id=o.draft_id)
            OR EXISTS (SELECT 1 FROM invoices r WHERE r.company=e.company
              AND json_extract(r.data,'$.billing_ref')=original.series || '-' || original.number)))
        """,
            (co,),
        )
    ]


def resolve(L: Ledger, co: str, task_id: str) -> None:
    with L.tx() as db:
        row = db.execute(
            "SELECT reason FROM emag_orders WHERE id=? AND company=? AND external_id!='sync' AND reason IS NOT NULL",
            (task_id, co),
        ).fetchone()
        if not row:
            raise ValueError("task")
        context = db.execute("SELECT value FROM emag_context WHERE id=?", (task_id,)).fetchone()
        db.execute(
            "INSERT OR REPLACE INTO emag_decisions VALUES(?,?)",
            (task_id, row["reason"] + ":" + (context[0] if context else "")),
        )


def refund(L: Ledger, co: str, task_id: str, today: str) -> dict:
    with L.tx() as db:
        row = db.execute(
            "SELECT o.id FROM emag_orders e JOIN commerce_orders o ON o.channel=e.channel AND o.company=e.company AND o.external_id=e.external_id WHERE e.id=? AND e.company=? AND e.reason='return_review'",
            (task_id, co),
        ).fetchone()
        if not row:
            raise ValueError("task")
        return commerce.command(L, co, row["id"], "orders_refund", {}, today)
