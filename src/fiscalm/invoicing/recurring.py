"""Monthly rules issue an invoice on their day, through the guarded invoice engine; one that it refuses stays a draft
with the reason. Issuing is ledger-local: SPV uploads follow their own rule and the shadow policy."""

from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date, timedelta
from uuid import uuid4

from fiscalm import bnr
from fiscalm.invoicing import engine as i
from fiscalm.ledger import Ledger


def create(
    L: Ledger, company: str, draft_id: str, day: int, due_days: int, today: str, first_period: str | None = None
) -> str:
    """Copy a company's saved-client draft; later edits cannot change the template."""
    i._company(L, company)
    i._day(today)
    if type(day) is not int or not 1 <= day <= 31 or type(due_days) is not int or not 0 <= due_days <= 365:
        raise i.InvoiceError("Ziua trebuie să fie între 1 și 31, iar termenul între 0 și 365 de zile.")
    current = date.fromisoformat(today)
    default_period = first_period is None
    if first_period is None:
        first_period = (
            today[:7]
            if current.day < min(day, calendar.monthrange(current.year, current.month)[1])
            else (current.replace(day=1) + timedelta(days=32)).strftime("%Y-%m")
        )
    if not isinstance(first_period, str):
        raise i.InvoiceError("Prima perioadă trebuie scrisă în formatul AAAA-LL și nu poate fi în trecut.")
    try:
        i._day(first_period + "-01")
        if first_period < today[:7]:
            raise ValueError
    except (ValueError, i.InvoiceError):
        raise i.InvoiceError("Prima perioadă trebuie scrisă în formatul AAAA-LL și nu poate fi în trecut.") from None
    with L.tx() as db:
        inv = i.invoice(L, draft_id, company=company)
        if default_period and inv["data"]["issue_date"][:7] == today[:7]:
            first_period = (current.replace(day=1) + timedelta(days=32)).strftime("%Y-%m")
        client_id = L.get_meta("draft_client:" + draft_id)
        if inv["status"] != "draft" or not client_id:
            raise i.InvoiceError("Regula are nevoie de o ciornă cu un client salvat.")
        # Fixed delivery/advance dates cannot safely repeat into another month.
        if inv["data"].get("delivery_date") or inv["data"].get("advance_invoice"):
            raise i.InvoiceError("Regula necesită o ciornă fără avans sau dată de livrare.")
        template = {k: inv["data"][k] for k in ("currency", "notes", "supply_country") if k in inv["data"]}
        template["lines"] = [{k: v for k, v in line.items() if k != "net"} for line in inv["lines"]]
        if _dependencies(L, company, client_id, inv["series"]):
            raise i.InvoiceError("Regula are nevoie de o ciornă cu un client și o serie existente.")
        rid = uuid4().hex
        db.execute(
            "INSERT INTO invoice_recurrences(id,company,client_id,series,template,day,due_days,start_period) VALUES(?,?,?,?,?,?,?,?)",
            (rid, company, client_id, inv["series"], json.dumps(template), day, due_days, first_period),
        )
        return rid


def _missing(has_client: bool, has_series: bool) -> str | None:
    """Why a rule cannot make drafts any more: its saved client or its series is gone."""
    return None if has_client and has_series else "missing_series" if has_client else "missing_client"


def _dependencies(L: Ledger, company: str, client_id: str, series: str) -> str | None:
    has_client = L.db.execute("SELECT 1 FROM invoice_clients WHERE company=? AND id=?", (company, client_id)).fetchone()
    return _missing(
        bool(has_client),
        bool(has_client)
        and bool(L.db.execute("SELECT 1 FROM invoice_series WHERE company=? AND code=?", (company, series)).fetchone()),
    )


def capabilities(paused: bool, missing: str | None) -> dict[str, bool]:
    """What `change` accepts for a rule: stop a running one, start a stopped one whose client and series still exist,
    delete any. Stopping a stopped rule would hide why fiscalm stopped it, so it is refused, as is starting a running one.
    The app offers every user with access to the company the same; API v1 also needs a write token."""
    return dict(can_pause=not paused, can_resume=paused and missing is None, can_delete=True)


_REFUSED = {
    "pause": "Regula este deja oprită.",
    "resume": "Regula este deja pornită.",
    "missing_client": "Clientul regulii lipsește. Pentru o regulă nouă, alege un client existent.",
    "missing_series": "Seria regulii nu mai există. După ce administratorul instalării reface aceeași serie, pornește regula.",
}


def change(L: Ledger, company: str, rid: str, op: str) -> None:
    """Recheck ownership and `capabilities` under the write lock; resume refuses missing dependencies."""
    i._company(L, company)
    with L.tx() as db:
        row = db.execute("SELECT * FROM invoice_recurrences WHERE id=? AND company=?", (rid, company)).fetchone()
        if row is None or op not in {"pause", "resume", "delete"}:
            raise i.InvoiceError("Regula nu a fost găsită.")
        missing = (
            _dependencies(L, company, row["client_id"], row["series"]) if row["paused"] and op == "resume" else None
        )
        if not capabilities(bool(row["paused"]), missing)["can_" + op]:
            raise i.InvoiceError(_REFUSED[missing or op])
        if op == "delete":
            db.execute("DELETE FROM invoice_recurrences WHERE id=? AND company=?", (rid, company))
            db.execute("DELETE FROM meta WHERE key IN (?,?)", ("recurrence_skip:" + rid, "recurrence_issue:" + rid))
        else:
            # The guard re-checks the state read above, so a racing change cannot flip it twice.
            db.execute(
                "UPDATE invoice_recurrences SET paused=?,reason=? WHERE id=? AND company=? AND paused=?",
                (int(op == "pause"), "manual" if op == "pause" else None, rid, company, row["paused"]),
            )


def list_rules(L: Ledger, company: str, today: str) -> list[dict]:
    """Read company-scoped rows without rereading the caller's registry snapshot, in a fixed number of queries. Each also
    says, for display, its saved client's name (`client`, null once the client is gone), the template's total in its
    currency as the engine computes it (`total`, null when it no longer validates or the seller is missing), the day `run`
    will next prepare a draft (`next_date`, see `next_date`) and what `change` accepts (`capabilities`)."""
    rows = L.db.execute("SELECT * FROM invoice_recurrences WHERE company=? ORDER BY id", (company,)).fetchall()
    if not rows:
        return []
    seller = L.db.execute("SELECT data FROM invoice_sellers WHERE company=?", (company,)).fetchone()
    vat_payer = bool(json.loads(seller["data"]).get("vat_payer")) if seller else None
    clients = {
        c["id"]: json.loads(c["data"]).get("name")
        for c in L.db.execute(
            "SELECT id,data FROM invoice_clients WHERE company=? AND id IN (SELECT client_id FROM invoice_recurrences WHERE company=?)",
            (company, company),
        )
    }
    series = {s["code"] for s in L.db.execute("SELECT code FROM invoice_series WHERE company=?", (company,))}
    # Each rule's last skipped months, and the invoice it last issued on its own, as issued (number, client).
    notes = {
        m["key"]: m
        for m in L.db.execute(
            "SELECT m.key,m.value,v.id,v.series,v.number,json_extract(v.data,'$.client.name') AS client FROM meta m"
            " LEFT JOIN invoices v ON m.key LIKE 'recurrence_issue:%' AND v.id=json_extract(m.value,'$.invoice')"
            " AND v.company=? AND v.status!='draft'"
            " WHERE m.key IN (SELECT p||id FROM invoice_recurrences, (SELECT 'recurrence_skip:' AS p"
            " UNION ALL SELECT 'recurrence_issue:') WHERE company=?)",
            (company, company),
        )
    }
    out = []
    for r in rows:
        template = json.loads(r["template"])
        missing = _missing(r["client_id"] in clients, r["series"] in series)
        try:
            total = i.calculate(template["lines"], vat_payer)[1]["total"] if vat_payer is not None else None
        except (i.InvoiceError, KeyError, TypeError, ValueError):
            total = None
        out.append(
            dict(r)
            | {
                "paused": bool(r["paused"]),
                "template": template,
                "backlog_policy": "current_only",
                "last_skipped": json.loads(notes["recurrence_skip:" + r["id"]]["value"])
                if "recurrence_skip:" + r["id"] in notes
                else None,
                "client": clients.get(r["client_id"]),
                "total": total,
                "next_date": None if missing else next_date(r, today),
                "last_invoice": _last_invoice(notes.get("recurrence_issue:" + r["id"])),
            }
            | capabilities(bool(r["paused"]), missing)
        )
    return out


def _last_invoice(note: sqlite3.Row | None) -> dict | None:
    """The invoice the rule last issued on its own: number, client as issued, and when."""
    if not note or not note["id"]:
        return None
    return dict(
        id=note["id"],
        number=f"{note['series']}-{note['number']}",
        client=note["client"] or "",
        at=json.loads(note["value"])["at"],
    )


def refused(L: Ledger, company: str) -> list[dict]:
    """The drafts a rule made that fiscalm did not issue and that are still drafts: each is a task with the engine's
    reason, until someone issues or deletes the draft. Deleting the rule keeps them."""
    return [
        dict(id=r["id"], client=r["client"] or "") | json.loads(r["value"])
        for r in L.db.execute(
            "SELECT v.id,json_extract(v.data,'$.client.name') AS client,m.value FROM invoices v"
            " JOIN meta m ON m.key='recurrence_refused:'||v.id WHERE v.company=? AND v.status='draft' ORDER BY m.value",
            (company,),
        )
    ]


def _issue(L: Ledger, rule: sqlite3.Row, iid: str, today: str, rate: tuple[str, str] | None) -> bool:
    """Issue the rule's new draft with the manual issue's every check and the rate already verified (no fetch under
    the lock); a refusal keeps the draft and records why, on the draft."""
    at = today + "T00:00:00"
    try:
        with L.tx():
            version = i.invoice(L, iid, company=rule["company"])["version"]
            i.issue(L, iid, today, company=rule["company"], version=version, selected=rate)
    except (i.InvoiceError, bnr.RateError) as exc:
        L.set_meta("recurrence_refused:" + iid, json.dumps(dict(recurrence=rule["id"], why=str(exc), at=at)))
        return False
    L.set_meta("recurrence_issue:" + rule["id"], json.dumps(dict(invoice=iid, at=at)))
    return True


def _on(period: str, day: int) -> str:
    """The rule's day in a month, clamped to the month's last day."""
    return f"{period}-{min(day, calendar.monthrange(int(period[:4]), int(period[5:7]))[1]):02d}"


def due(rule: sqlite3.Row | dict, today: str) -> tuple[str | None, str | None]:
    """The one schedule: whether `run` on `today` prepares this rule's draft. `(period, None)` when it does, else
    `(None, why)`: `paused`, `not_started` (before its first period), `made` (this month's draft is already made) or
    `before_day` (the rule's day, clamped to the month, has not come). Missed months are never due again; a missing
    client or series is `run`'s to check under its lock."""
    period = today[:7]
    if rule["paused"]:
        return None, "paused"
    if period < rule["start_period"]:
        return None, "not_started"
    if rule["last_period"] and period <= rule["last_period"]:
        return None, "made"
    if today < _on(period, rule["day"]):
        return None, "before_day"
    return period, None


def next_date(rule: sqlite3.Row | dict, today: str) -> str | None:
    """The first day from `today` on which `due` lets `run` prepare a draft; None while paused. A rule whose day has passed
    in a month not yet made is due today, on the next run."""
    day = today
    for _ in range(4):  # today, then at most: the first period, its day, the next month's day
        period, why = due(rule, day)
        if period:
            return day
        if why == "paused":
            return None
        made = max(day[:7], rule["last_period"] or day[:7])  # a period already made may lie months ahead
        month = (
            day[:7]
            if why == "before_day"
            else rule["start_period"]
            if why == "not_started"
            else (date.fromisoformat(made + "-01") + timedelta(days=32)).strftime("%Y-%m")
        )
        day = max(_on(month, rule["day"]), today)
    return None


def run(L: Ledger, today: str, *, events: list[dict] | None = None, refusals: list[str] | None = None) -> list[str]:
    """Atomically reserve one current period, save its draft and issue it, or roll all back; a refused issue keeps the
    draft and the period. Returns the made invoices' ids, issued or not; `refusals` gets the ones left drafts.

    Any other error rolls back that rule's transaction and stops this run; the next tick tries again.
    No upload/mail path is called. Draft deletion never releases a period.
    Missed months are counted, never replayed. Dates come from the caller.
    """
    current = date.fromisoformat(i._day(today))
    period = today[:7]
    created = []
    for snapshot in list(L.db.execute("SELECT * FROM invoice_recurrences ORDER BY id")):
        selected = None
        rate_error = None
        template = json.loads(snapshot["template"])
        if (
            template["currency"] != "RON"
            and due(snapshot, today)[0]
            and not L.db.execute(
                "SELECT 1 FROM invoice_recurrence_periods WHERE recurrence=? AND period=?", (snapshot["id"], period)
            ).fetchone()
        ):
            try:
                selected = bnr.rate(L, template["currency"], today, today=today)
            except bnr.RateError as exc:
                rate_error = exc
        with L.tx() as db:
            r = db.execute("SELECT * FROM invoice_recurrences WHERE id=? AND paused=0", (snapshot["id"],)).fetchone()
            if r is None or dict(r) != dict(snapshot):
                continue
            reason = _dependencies(L, r["company"], r["client_id"], r["series"])
            if reason:
                db.execute(
                    "UPDATE invoice_recurrences SET paused=1,reason=? WHERE id=? AND paused=0", (reason, r["id"])
                )
                continue
            if not due(r, today)[0]:
                continue
            if db.execute(
                "SELECT 1 FROM invoice_recurrence_periods WHERE recurrence=? AND period=?", (r["id"], period)
            ).fetchone():
                continue
            data = json.loads(r["template"]) | dict(
                issue_date=today, due_date=(current + timedelta(days=r["due_days"])).isoformat()
            )
            try:
                if data["currency"] != "RON":
                    if rate_error is not None:
                        raise rate_error
                    if selected is None:
                        raise bnr.RateError("Curs BNR indisponibil.")
                    data["exchange_rate"], data["exchange_date"] = bnr.recheck_rate(
                        L, data["currency"], i.chargeability_date(data), selected
                    )
                # Nested savepoint also rolls back a partially saved draft on refusal.
                with L.tx():
                    iid = i.save_draft(L, r["company"], r["series"], r["client_id"], data, recurrence_id=r["id"])
                    db.execute("INSERT INTO invoice_recurrence_periods VALUES(?,?,?)", (r["id"], period, iid))
            except bnr.UnsupportedCurrency:
                db.execute(
                    "UPDATE invoice_recurrences SET paused=1,reason='unsupported_currency' WHERE id=? AND paused=0",
                    (r["id"],),
                )
                continue
            except bnr.RateError:
                db.execute(
                    "UPDATE invoice_recurrences SET reason='rate_unavailable' WHERE id=? AND paused=0", (r["id"],)
                )
                continue
            except i.InvoiceError:
                db.execute(
                    "UPDATE invoice_recurrences SET paused=1,reason='invalid_template' WHERE id=? AND paused=0",
                    (r["id"],),
                )
                continue
            previous = date.fromisoformat((r["last_period"] or r["start_period"]) + "-01")
            skipped = max(
                0,
                (current.year - previous.year) * 12
                + current.month
                - previous.month
                - int(r["last_period"] is not None),
            )
            if skipped:
                first = (previous.replace(day=1) + timedelta(days=32)).replace(day=1) if r["last_period"] else previous
                last = current.replace(day=1) - timedelta(days=1)
                event = dict(
                    recurrence=r["id"],
                    company=r["company"],
                    from_period=first.strftime("%Y-%m"),
                    through_period=last.strftime("%Y-%m"),
                    count=skipped,
                    at=today + "T00:00:00",
                )
                L.set_meta("recurrence_skip:" + r["id"], json.dumps(event))
                if events is not None:
                    events.append(event)
            db.execute(
                "UPDATE invoice_recurrences SET last_period=?,skipped_periods=skipped_periods+?,reason=NULL WHERE id=? AND paused=0",
                (period, skipped, r["id"]),
            )
            rate = (data["exchange_rate"], data["exchange_date"]) if data["currency"] != "RON" else None
            if not _issue(L, r, iid, today, rate) and refusals is not None:
                refusals.append(iid)
            created.append(iid)
    return created


def report(L: Ledger, today: str) -> dict:
    """The tick reports which periods it deliberately omitted, only on the run that did so."""
    events: list[dict] = []
    drafts: list[str] = []
    made = run(L, today, events=events, refusals=drafts)
    return {"issued": [x for x in made if x not in drafts], "drafts": drafts, "skipped": events}
