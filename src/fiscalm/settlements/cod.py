"""COD exposure derives only from explicit courier lifecycle evidence, never AWB allocation."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from fiscalm.ledger import Ledger


def state(L: Ledger, company: str, invoices: list[dict], today: date, overdue_days: int = 7) -> dict:
    if type(overdue_days) is not int or not 0 <= overdue_days <= 365:
        raise ValueError("COD overdue days must be between 0 and 365")
    by_local = {r.get("local_id") or r["id"]: r for r in invoices}
    rows, tasks = [], []
    totals: dict[str, Decimal] = {}
    for o in L.db.execute(
        "SELECT o.*,i.status AS invoice_status,d.order_id AS delivery_id,d.picked_up_at,d.returned_at FROM commerce_orders o "
        "LEFT JOIN commerce_delivery d ON d.order_id=o.id "
        "JOIN invoices i ON i.id=o.draft_id AND i.company=o.company "
        "WHERE o.company=? AND o.status!='cancelled' AND i.status='issued' ORDER BY o.id",
        (company,),
    ):
        body, awb = json.loads(o["body"]), json.loads(o["awb"]) if o["awb"] else {}
        if o["delivery_id"]:
            awb.update({k: o[k] for k in ("picked_up_at", "returned_at") if o[k]})
            for k in ("picked_up_at", "returned_at"):
                if not o[k]:
                    awb.pop(k, None)
        invoice = by_local.get(o["draft_id"])
        if not invoice or invoice.get("rejected") or body["payment"]["method"] != "cod" or not awb:
            continue
        fully_credited = Decimal(invoice["credited"]) >= Decimal(invoice["total"])
        collected = invoice["status"] == "collected" or fully_credited
        owed = max(Decimal(invoice["total"]) - Decimal(invoice["credited"]) - Decimal(invoice["covered"]), Decimal(0))
        returned = bool(awb.get("returned_at") and awb["returned_at"] <= today.isoformat())
        picked_up = bool(awb.get("picked_up_at") and awb["picked_up_at"] <= today.isoformat())
        waiting = picked_up and not returned and not collected
        late = (
            waiting
            and bool(awb.get("delivered_at"))
            and (today - date.fromisoformat(awb["delivered_at"])).days > overdue_days
        )
        rows.append(
            {
                "id": o["id"],
                "company": company,
                "invoice": invoice["id"],
                "awb": awb,
                "waiting": waiting,
                "returned": returned,
                "collected": collected,
                "overdue": late,
            }
        )
        if waiting:
            totals[invoice["currency"]] = totals.get(invoice["currency"], Decimal(0)) + owed
        if (returned and not fully_credited) or late:
            tasks.append(
                {
                    "kind": "cod_returned" if returned else "cod_overdue",
                    "id": o["id"],
                    "company": company,
                    "invoice": invoice["id"],
                    "sheet": {"co": company, "iss": invoice["id"]},
                    "suggestion": "review_refund_draft" if returned else None,
                }
            )
    return {
        "count": sum(r["waiting"] for r in rows),
        "totals": [{"currency": c, "total": f"{v:.2f}"} for c, v in sorted(totals.items())],
        "rows": rows,
        "tasks": tasks,
        "overdue_days": overdue_days,
    }
