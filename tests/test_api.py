"""fiscalm: the API, its actions and the server (docs/openapi.json). Synthetic data only (tests/synthetic_world.py)."""

import hashlib
import http.client
import io
import json
import os
import re
import socket
import subprocess
import threading
import urllib.parse
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import anaf_fake
import pytest
import synthetic_world as scenario
from conftest import go_live
from test_invoicing import DATA, LINE, PARTY

from fiscalm import bnr, cli, companies, invoice_lines, pdftext
from fiscalm.api import actions, details, lists, sessions, state as api, tokens, web
from fiscalm.bank import statements
from fiscalm.commerce import webhooks
from fiscalm.efactura import client as anaf, inbox as efactura, public as anaf_public
from fiscalm.invoicing import (
    engine as invoicing,
    issued,
    pdf as invoice_pdf,
    spv as invoice_spv,
    ubl,
    workspace as invoicing_api,
)
from fiscalm.ledger import Ledger
from fiscalm.payments import btgo, engine as pay
from fiscalm.payments.orders import PaymentFileError

TOP = {
    "files",
    "payment_file_defaults",
    "firm_cards",
    "setup",
    "accountant",
    "orders",
    "order_tasks",
    "sales_origins",
    "cod_receivable",
    "recurrences",
    "recurring_refused",
    "rules",
    "can_manage_companies",
    "co",
    "live",
    "today",
    "companies",
    "payable",
    "receivable",
    "invoices",
    "documents",
    "issued",
    "history",
    "statement_hold",
    "reports",
    "turnover",
    "to_check",
    "news",
    "system",
    "refused",
    "unreadable",
    "ambiguous",
    "mail_proofs",
    "partners",
    "inbox",
    "upload_tasks",
    "expenses",
    "reminder_tasks",
    "partner_tasks",
    "settlements",
    "settlement_tasks",
    "settlement_transactions",
    "settlement_fees",
    "cod",
    "emag_tasks",
    "bank_connections",
    "dashboard",
    "dashboard_pages",
    "task_summary",
    "transactions",
}
INVOICE = {
    "payment_file",
    "amount_changed",
    "previous_iban_last4",
    "accounts",
    "paid_method",
    "paid_detail",
    "credits",
    "credit_question",
    "credit_note",
    "id",
    "company",
    "supplier",
    "supplier_cui",
    "partner",
    "number",
    "issue_date",
    "due_date",
    "currency",
    "total",
    "status",
    "payment_state",
    "item_id",
    "covered",
    "left",
    "paid_at",
    "overdue",
    "can_approve",
    "can_mark_paid",
    "can_withdraw",
    "can_reopen",
    "proved",
    "bon",
}
ISSUED = {
    "order_id",
    "order_sheet_id",
    "sales_origin",
    "order_number",
    "awb",
    "cod_at_courier",
    "id",
    "company",
    "number",
    "client",
    "client_cui",
    "partner",
    "issue_date",
    "due_date",
    "currency",
    "total",
    "credited",
    "covered",
    "owed",
    "left",
    "status",
    "days_overdue",
    "collected_at",
    "collected_on",
    "how",
    "collected_method",
    "note",
    "can_mark",
    "can_unmark",
    "origin",
    "spv",
    "can_storno",
    "can_upload",
    "can_resolve",
    "pdf",
    "storno_of",
    "reversed_by",
    "local_id",
    "rejected",
}
DOCUMENT = {
    "document_type",
    "can_correct",
    "number",
    "id",
    "company",
    "vendor",
    "date",
    "month",
    "amount",
    "currency",
    "note",
    "sent",
    "held",
    "card",
    "files",
    "invoice",
    "mail",
    "partner",
}
PAYMENT = {"item_id", "kind", "payee", "ref", "details", "amount", "due", "ef_id", "paid_at", "waits_statement"}
REPORT = {
    "month",
    "status",
    "sent_on",
    "prepared_at",
    "stale",
    "version",
    "mark",
    "docs",
    "received",
    "documents",
    "issued",
    "to_check",
    "bank",
    "not_mailed",
}


@pytest.fixture
def world(ledger, home, monkeypatch):
    class ScenarioDate(date):
        @classmethod
        def today(cls):
            return scenario.NOW.date()

    monkeypatch.setattr(pay, "date", ScenarioDate)
    return scenario.build(ledger, home, monkeypatch.setattr, scenario.NOW)


class Endpoint(int):
    def __new__(cls, port, origin, service):
        endpoint = super().__new__(cls, port)
        endpoint.origin = origin
        endpoint.service = service
        return endpoint


@pytest.fixture
def server(home, tmp_path, monkeypatch):
    monkeypatch.setenv("FISCALM_PUBLIC_URL", os.environ.get("FISCALM_PUBLIC_URL") or "https://books.example")
    monkeypatch.setenv("FISCALM_TRUST_PROXY", "1")
    connection = Ledger()
    try:
        at = datetime.now(UTC)
        for role, digit in (("admin", "a"), ("member", "b"), ("readonly", "c")):
            connection.db.execute(
                "INSERT INTO users(id,username,display_name,role,webauthn_handle,created_at,password_hash,totp_version) VALUES(?,?,?,?,?,?,?,?)",
                ("u_" + role, role, role, role, role.encode(), sessions.stamp(at), "synthetic-hash", "f" * 32),
            )
            connection.db.commit()
            issued = sessions.issue(connection, "u_" + role, at)
            connection.db.execute(
                "UPDATE sessions SET id_hash=? WHERE id_hash=?", (sessions.digest(digit * 64), sessions.digest(issued))
            )
            connection.db.commit()
    finally:
        connection.close()
    monkeypatch.setattr(web, "APP_OUT", tmp_path / "app" / "out")
    srv = web.Server(("127.0.0.1", 0), web.Handler)
    # A short poll: shutdown() waits up to one poll interval, 0.5 s by default, on every test that serves.
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield Endpoint(srv.server_address[1], srv.boundary.origin, srv)
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def quiet(monkeypatch):
    """Records the ticks the actions ask the scheduler for."""
    calls = []
    monkeypatch.setattr(actions, "_tick", lambda: calls.append("tick"))
    return calls


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=20)
    origin = getattr(port, "origin", "https://books.example")
    values = {
        "Host": urllib.parse.urlsplit(origin).netloc,
        "Origin": origin,
        "X-Forwarded-Proto": "https",
        "Cookie": "__Host-session=" + "a" * 64,
        **(headers or {}),
    }
    c.request(method, path, body=body, headers={k: v for k, v in values.items() if v is not None})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, data


def _api(port, action, body, ctype="application/json", user="admin", **headers):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    code, hdr, data = _req(
        port,
        "POST",
        f"/web/api/{action}",
        raw,
        {
            "Content-Type": ctype,
            "Cookie": "__Host-session=" + {"admin": "a", "member": "b", "readonly": "c"}.get(user, "d") * 64,
            **headers,
        },
    )
    return code, (json.loads(data) if hdr.get("content-type", "").startswith("application/json") else data)


def _get_json(port, path, user="admin", **headers):
    code, hdr, data = _req(
        port,
        "GET",
        path,
        headers={
            "Cookie": "__Host-session=" + {"admin": "a", "member": "b", "readonly": "c"}.get(user, "d") * 64,
            **headers,
        },
    )
    return code, (json.loads(data) if hdr.get("content-type", "").startswith("application/json") else data)


def _entity(port, kind: str, identity: str, co: str = "NORD") -> dict:
    query = urllib.parse.urlencode({"co": co, "kind": kind, "id": identity})
    code, result = _get_json(port, "/web/api/detail?" + query)
    assert code == 200
    return details.find(result["state"], kind, identity)


def _amounts(obj, path=""):
    """Every amount in the state: a string with exactly two decimals."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("total", "amount", "confirming", "credited", "owed") and not (
                k == "total" and "next_cursor" in obj
            ):
                if k == "confirming" and isinstance(v, dict):
                    assert set(v) == {"total", "other", "count"}, (path + k, v)
                else:
                    assert isinstance(v, str) and re.fullmatch(r"-?\d+\.\d\d", v), (path + k, v)
            _amounts(v, path + k + ".")
    elif isinstance(obj, list):
        for x in obj:
            _amounts(x, path)


def _shape(obj):
    """Key structure of a JSON value (dict keys, first element of lists)."""
    if isinstance(obj, dict):
        return {k: _shape(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shape(obj[0])] if obj and isinstance(obj[0], (dict, list)) else []
    return None


def _file_with_op(ledger, home, n=1, amount="100.00", days_ago=0):
    """Live, and one OP built into NORD's BT Go file: its payment item."""
    go_live(home)
    item = scenario._op(ledger, n, "NORD", amount, date.today() - timedelta(days=days_ago))
    ident = ledger.create_payment_file("NORD", "Test file")
    assert ledger.attach_payment_file(ident, "NORD", item)
    pay.download_file(ledger, ident, "NORD", date.today(), lambda: None)
    return item


def _doc(s, vendor):
    return next(d for d in s["documents"] if d["vendor"] == vendor)


def _inv(port, op, co="NORD", **body):
    """One invoicing POST: a JSON answer that carries the request's company whenever it carries a state."""
    code, out = _api(port, "invoicing/" + op, dict(co=co, **body))
    assert code == 200 and (out["state"] is None or out["state"]["co"] == out["invoicing"]["co"] == co)
    return out


def _draft(ledger, series, data=DATA):
    """NORD's seller, one client, a series and a draft: (client id, draft id)."""
    invoicing.save_seller(ledger, "NORD", PARTY)
    cid = invoicing.save_client(ledger, "NORD", PARTY)
    invoicing.create_series(ledger, "NORD", series)
    return cid, invoicing.save_draft(ledger, "NORD", series, cid, data)


def _spv_answer(xml):
    """SPV's ZIP holding the signed invoice."""
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("invoice.xml", xml)
    return raw.getvalue()


# ------------------------------------------------------------------ state


def test_state_shape_for_both_companies(ledger, world):
    for co in ("NORD", "STUDIO9"):
        s = api.state(ledger, co)
        assert set(s) == TOP and s["co"] == co and s["live"] is True
        assert [c["key"] for c in s["companies"]] == ["NORD", "STUDIO9"]
        assert all(set(i) == INVOICE and i["company"] == co for i in s["invoices"])
        assert all(set(i) == ISSUED and i["company"] == co for i in s["issued"])
        assert all(set(r) == REPORT for r in s["reports"])
        assert all("payments" not in f for f in s["files"])
        assert all(set(p) == PAYMENT for rows in api.prepared_payments(ledger, co).values() for p in rows)
        _amounts(s)
        assert re.fullmatch(r"\d{4}-\d\d-\d\d", s["today"])
        assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", n["at"]) and "date" in n for n in s["news"])
        assert s["system"]["anaf"]["days_left"] == 20 and s["system"]["mail"]["stale"] is False
        log = s["system"]["log"]  # the scenario's lines name /data/fiscalm/inbox: paths leave as their file name
        assert log and all(line.endswith("0 new mail(s) in inbox") for line in log)

    s = api.state(ledger, "NORD")
    inv = {i["id"]: i for i in s["invoices"]}
    assert "110" not in inv and [u["id"] for u in s["unreadable"]] == ["110"]
    assert {i["payment_state"] for i in inv.values() if i["status"] == "linked"} == {
        "held",
        "in_file",
        "lost",
        "imported",
    }
    assert (
        inv["101"]["can_approve"] and inv["101"]["overdue"] and inv["106"]["overdue"]
    )  # open, or still in a BT Go file
    assert inv["103"]["currency"] == "EUR" and not inv["103"]["can_approve"] and inv["103"]["can_mark_paid"]
    assert inv["105"]["can_withdraw"] and not inv["104"]["can_withdraw"] and not inv["104"]["overdue"]
    assert inv["108"]["status"] == "marked" and inv["108"]["can_reopen"] and inv["108"]["paid_at"]
    assert inv["111"]["status"] == "paid" and inv["111"]["paid_at"] and not inv["111"]["can_reopen"]
    assert s["refused"] == []
    assert inv["109"]["can_approve"] and inv["109"]["previous_iban_last4"] == ["9999"]
    assert len(s["files"]) == 4
    assert {f["status"] for f in s["files"]} == {"open", "imported", "confirmed"}
    assert any(f["changed_since_download"] for f in s["files"])
    assert s["to_check"][0]["key"] == scenario.MAIL_KEY
    assert s["to_check"][0]["companies"] == ["NORD", "STUDIO9"] and s["to_check"][0]["file"] == "pdf"
    assert {n["kind"] for n in s["news"]} >= {"invoice", "document"}
    assert [d["vendor"] for d in s["documents"]][:3] == ["Benzinaria Nord", "Brico Nord", "Example Cloud"] and all(
        set(d) == DOCUMENT for d in s["documents"]
    )
    assert s["documents"][2]["mail"]["number"] == "CL-2026-1042" and s["documents"][2]["amount"] == "49.01"
    assert {d["currency"] for d in s["documents"]} == {"RON", "EUR"}
    lab = api.state(ledger, "STUDIO9")
    assert lab["statement_hold"]["count"] == 1  # a different file is missing; this invoice still needs its statement
    assert {p["ef_id"]: p["waits_statement"] for p in api.prepared_payments(ledger, "STUDIO9")["held"]} == {
        None: False,
        "202": True,
    }


def test_an_unread_receipt_has_an_unknown_amount_not_zero(ledger, world):
    ledger.add_document("a" * 64, "NORD", "2026-10", "receipt", "unread.jpg", source="manual")
    ledger.set_document_note("a" * 64, "nota unread")
    doc = next(d for d in api.state(ledger, "NORD")["documents"] if d["id"] == "a" * 64)
    assert (doc["amount"], doc["vendor"], doc["date"]) == (None, "", None)
    # search reads it by its words and never matches an amount it doesn't have
    assert [d["id"] for d in lists.search(ledger, "NORD", {"q": "nota unread"})["documents"]] == ["a" * 64]
    assert all(d["id"] != "a" * 64 for d in lists.search(ledger, "NORD", {"q": "0,00"})["documents"])


def test_invoices_the_user_marked_paid_wait_under_confirming(ledger, world):
    # Three invoices marked paid without a bank debit or mail proof.
    # a marked EUR invoice stays under "confirming" in its own currency
    d = date.fromisoformat(api.state(ledger, "NORD")["today"]) - timedelta(days=12)
    marked = {
        "701": ("TEST 001", "1200.00", "RON"),
        "702": ("TEST 002", "500.00", "RON"),
        "703": ("TEST 003", "800.00", "RON"),
        "711": ("SE 0711", "20.00", "EUR"),
    }
    for mid, (number, total, currency) in marked.items():
        scenario._ef(
            ledger, mid, "NORD", "Furnizor Exemplu SRL", "RO7001", number, d, d + timedelta(days=15), total, currency
        )
    before = api.state(ledger, "NORD")["payable"]
    for mid in marked:
        r = actions.act("mark-paid", "NORD", {"id": mid, "method": "bank"})
        assert r["ok"] and r["message"] == "Factura a fost marcată ca plătită."
    s = api.state(ledger, "NORD")
    assert Decimal(before["total"]) - Decimal(s["payable"]["total"]) == Decimal("2500.00")  # not owed any more
    assert Decimal(s["payable"]["confirming"]) - Decimal(before["confirming"]) == Decimal("2500.00")
    assert s["payable"]["confirming_other"] == [{"currency": "EUR", "total": "20.00"}]
    inv = {r["id"]: r for r in s["invoices"]}
    for mid in marked:
        assert (inv[mid]["status"], inv[mid]["can_reopen"], inv[mid]["can_mark_paid"]) == ("marked", True, False)
    assert not {p["ef_id"] for f in s["files"] for p in api.payment_file_detail(ledger, f["id"])["payments"]} & set(
        marked
    )
    both = api.state(ledger, "ALL")
    assert Decimal(both["payable"]["confirming"]) >= Decimal("2500.00")
    assert both["payable"]["confirming_other"] == [{"currency": "EUR", "total": "20.00"}]
    # a statement debit confirms one: it leaves "confirming" and reads as paid
    assert ledger.mark_efactura_paid("702", "statement 2026-10-28 iban REF X", "tx-702")
    s = api.state(ledger, "NORD")
    assert Decimal(s["payable"]["confirming"]) - Decimal(before["confirming"]) == Decimal("2000.00")
    assert {r["id"]: r["status"] for r in s["invoices"]}["702"] == "paid"


def test_state_in_shadow_offers_no_payment_downloads(ledger):
    s = api.state(ledger, "NORD")
    assert s["live"] is False and s["files"] == [] and s["reports"]


def test_paid_is_proved_only_by_a_statement_a_mail_or_a_bon(ledger, world):
    """`proved` (the app's "="): a BT Go payment marked paid by hand (`fiscalm pay mark-paid`) is paid in the user's word only;
    one a statement debit settled is proved, and so is the invoice it paid. A received invoice is proved by a statement
    debit, a payment mail or its till receipt, never by the user's tap or an old note without proof."""
    by_hand, by_statement = ledger.efactura("104")["item_id"], ledger.efactura("105")["item_id"]
    assert cli.main(["pay", "mark-paid", by_hand]) == 0
    debit = statements.Tx(
        date=date(2026, 10, 26),
        title="Plata OP",
        description="Plata 105",
        direction="debit",
        amount=Decimal("64.90"),
        currency="RON",
        iban="RO24BTRLRONCRT0000000001",
        ref="X",
    )
    ledger.record_statement(statements.Statement("NORD", None, None, [statements.Account("x", "RON", [debit])]))
    assert ledger.mark_paid(by_statement, "statement 2026-10-26 REF X", statements.tx_key(debit))
    scenario._ef(ledger, "721", "NORD", "Curs Online SRL", "RO7021", "CO-0721", date(2026, 10, 1), None, "45.00")
    assert (
        cli.main(["efactura", "mark-paid", "721", "--mail", "nm:721.plata@curs.example", "--note", "mail: plată"]) == 0
    )
    scenario._ef(
        ledger,
        "722",
        "NORD",
        "Arhiva Veche SRL",
        "RO7022",
        "AV-0722",
        date(2026, 10, 1),
        None,
        "12.00",
        status="paid",
        paid_at="2026-10-02T10:00:00",
        paid_note="paid, no proof recorded",
    )
    s = api.state(ledger, "NORD")
    paid = {
        p["ef_id"]: p["proved"]
        for f in s["files"]
        for p in api.payment_file_detail(ledger, f["id"])["payments"]
        if p["state"] == "paid"
    }
    assert paid["104"] is False  # from the imported file; by_statement was in the current file, now paid
    inv = {r["id"]: (r["status"], r["proved"]) for r in s["invoices"]}
    assert inv["104"] == ("paid", False)  # its BT Go row was marked paid by hand
    assert inv["105"] == ("paid", True)
    assert inv["111"] == ("paid", True)  # a statement debit
    assert (
        inv["112"] == ("paid", True) and s["invoices"][[r["id"] for r in s["invoices"]].index("112")]["bon"]
    )  # its bon
    assert inv["721"] == ("paid", True)  # a payment mail
    assert inv["722"] == ("paid", False)  # a note, no proof
    assert inv["108"] == ("marked", False)  # the user's tap
    assert all(not proved for status, proved in inv.values() if status != "paid")


def test_payable_and_receivable(ledger, world):
    s = api.state(ledger, "NORD")
    # open RON invoices 101, 102, 109 + every row not imported yet (held 107, in the file 105 + OP 2 + supplier 3, lost 106 + OP 4)
    # confirming: imported rows (1845.00) and 108, marked paid by the user (89.00), until a statement or a mail confirms them
    # late_total: 101 (386.40), the lost rows 106 (64.90) and OP 4 (410.00), and OP 2 in the file (3200.00), all past due
    assert s["payable"] == {
        "total": "7642.30",
        "other": [{"currency": "EUR", "total": "20.00"}],
        "confirming": "684.00",
        "confirming_other": [],
        "late_total": "3651.30",
        "late_other": [],
        "due_soon": 2,
        "late": 3,
    }
    # 5001 overdue, 5002 due in 5 days, 5003 in EUR; 5004 collected, 5005 marked, 5006 credited by a credit note
    # late: 5001 and the scenario's two fiscalm invoices waiting on client reminders
    assert s["receivable"] == {
        "total": "6050.00",
        "other": [{"currency": "EUR", "total": "900.00"}],
        "late_total": "5405.00",
        "late_other": [],
        "due_soon": 1,
        "late": 3,
    }
    rows = {r["id"]: r for r in s["issued"]}
    assert rows["5001"]["status"] == "open" and rows["5001"]["days_overdue"] == 10 and rows["5001"]["can_mark"]
    assert (
        rows["5004"]["status"] == "collected" and rows["5004"]["how"] == "statement" and not rows["5004"]["can_unmark"]
    )
    assert (
        rows["5005"]["status"] == "marked"
        and rows["5005"]["note"] == "compensare cu factura lor"
        and rows["5005"]["can_unmark"]
    )
    assert (
        rows["5006"]["status"] == "credited" and rows["5006"]["credited"] == "300.00" and rows["5006"]["owed"] == "0.00"
    )
    assert "5007" not in rows  # the credit note itself is not an invoice to collect


def test_state_all_is_both_companies_read_only(ledger, world):
    a = api.state(ledger, api.ALL)
    e, lab = api.state(ledger, "NORD"), api.state(ledger, "STUDIO9")
    assert (
        set(a)
        == {
            "can_manage_companies",
            "co",
            "live",
            "today",
            "companies",
            "payable",
            "receivable",
            "parts",
            "sales_origins",
            "cod_receivable",
            "dashboard",
            "dashboard_pages",
            "task_summary",
        }
        and a["co"] == "ALL"
    )
    assert [p["co"] for p in a["parts"]] == ["NORD", "STUDIO9"] and a["parts"][0] == e
    assert a["payable"]["total"] == str(Decimal(e["payable"]["total"]) + Decimal(lab["payable"]["total"]))
    assert a["payable"]["late"] == e["payable"]["late"] + lab["payable"]["late"]
    assert a["receivable"]["total"] == "10250.00" and a["receivable"]["other"] == [
        {"currency": "EUR", "total": "900.00"}
    ]


def test_reports_per_month(ledger, world):
    m0, m1, m2 = world["months"]
    reps = {r["month"]: r for r in api.state(ledger, "NORD")["reports"]}
    assert list(reps) == [m0, m1, m2]  # older months hold nothing: not listed
    now, last, sent = reps[m0], reps[m1], reps[m2]
    assert now["status"] == "preparing" and now["bank"] is None and now["to_check"] == [scenario.MAIL_KEY]
    assert (
        set(now["issued"])
        == {"5002", "5003", "local-storno"}
        | {
            invoicing.import_id("local", "local-" + k)
            for k in ("accepted", "uploaded", "rejected", "reversed", "reminded", "overdue")
        }
        and "101" in now["received"]
    )
    # September's documents are September's pack
    assert sorted(d["vendor"] for d in now["docs"]) == ["Benzinaria Nord", "Brico Nord", "Example Cloud", "Figma"]
    assert last["status"] == "ready" and last["version"] is None and set(last["received"]) == {"104", "111"}
    assert {d["vendor"] for d in last["docs"]} == {None, "Hetzner", "Google Workspace"}
    card = last["bank"]["card"]
    assert len(card) == 7 and [(t["who"], t["kind"]) for t in card if not t["doc"]] == [
        ("NIMBUS HOSTING INC", "card"),
        ("Exemplu Imobiliare SRL", "transfer"),
    ]
    assert last["bank"]["advances"] == [{"date": f"{m1}-10", "amount": "500.00"}] and last["bank"]["balanced"] is True
    assert (
        sent["status"] == "sent"
        and sent["sent_on"] == f"{m1}-05"
        and [d["vendor"] for d in sent["docs"]] == ["GitHub", None]
    )
    lab = {r["month"]: r for r in api.state(ledger, "STUDIO9")["reports"]}
    assert lab[m1]["status"] == "preparing" and lab[m1]["bank"]["statement"] is False
    assert lab[m2]["bank"]["balanced"] is False  # totals that don't match the bank: something to solve


def test_card_payments_follow_receipts_of_any_month(ledger, home, monkeypatch):
    """A September 30 card charge, its receipt dated October 1: September's report counts it as soon as it arrives,
    and the statement PDF is parsed once."""
    pdf = home / "extL_Sep_2026_allmerge.pdf"
    pdf.write_bytes(b"%PDF")
    ledger.add_document("st09", "NORD", "2026-09", "statement", str(pdf))
    charge = statements.Tx(
        date=date(2026, 9, 30),
        title="Plata la POS non-BT",
        description="NIMBUS HOSTING INC;US",
        direction="debit",
        amount=Decimal("173.45"),
        currency="RON",
        iban="RO00BTRL",
        ref="V1",
        orig_amount=Decimal("37.00"),
        orig_currency="USD",
        country="US",
        counterparty="NIMBUS HOSTING INC",
    )
    ledger.record_statement(statements.Statement("NORD", None, None, [statements.Account("RO00BTRL", "RON", [charge])]))
    parsed = []
    monkeypatch.setattr(
        statements,
        "parse",
        lambda path, co: (
            parsed.append(path)
            or statements.Statement(co, None, None, [statements.Account("RO00BTRL", "RON", [charge])])
        ),
    )
    monkeypatch.setattr(api, "_STATEMENTS", {}, raising=False)
    assert [t["doc"] for t in api._bank(ledger, "NORD", "2026-09")["card"]] == [None]  # missing its invoice
    ledger.add_document(
        "r10",
        "NORD",
        "2026-10",
        "receipt",
        str(home / "Nimbus.pdf"),
        vendor="Nimbus",
        doc_date="2026-10-01",
        amount="37.00",
        currency="USD",
    )
    assert [t["doc"] for t in api._bank(ledger, "NORD", "2026-09")["card"]] == ["Nimbus.pdf"]
    assert len(parsed) == 1


def test_overdue_count_follows_what_the_user_still_has_to_pay(ledger, home):
    """The company dot, the card's Restante and the invoice's marker: overdue until imported in BT Go."""
    _item = _file_with_op(ledger, home, days_ago=40)  # a tax due on the 25th, long past
    s = api.state(ledger, "NORD")
    assert s["payable"]["late"] == s["companies"][0]["late"] == 1
    ident = ledger.payment(_item)["file_id"]
    assert pay.import_file(ledger, ident, "NORD", pay.file_downloads(ledger, ident)[0]["id"], date.today())
    s = api.state(ledger, "NORD")
    assert s["payable"]["late"] == s["companies"][0]["late"] == 0 and s["payable"]["confirming"] == "100.00"


def test_fixtures_are_generated_and_stable(ledger, world, home, tmp_path):
    """The app's fixtures come from this scenario (never committed): the live state's shape, the same bytes every run."""
    scenario.write_fixtures(tmp_path / "a")
    scenario.write_fixtures(tmp_path / "b")
    for name in ("state-NORD.json", "state-STUDIO9.json", "state-ALL.json", "invoicing-NORD.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    scenario.chart_fixtures(ledger, home, scenario.NOW.date())
    for co in ("NORD", "STUDIO9", "ALL"):
        fixture = json.loads((tmp_path / "a" / f"state-{co}.json").read_text())
        assert (
            _shape(fixture) == _shape(api.state(ledger, co, can_manage_companies=True))
            and fixture["today"] == scenario.NOW.date().isoformat()
        )
    assert json.loads((tmp_path / "a" / "invoicing-NORD.json").read_text()) == invoicing_api.workspace(ledger, "NORD")


# ------------------------------------------------------------------ HTTP: reads


def test_http_get_state_keeps_untrusted_text_as_data(ledger, world, server):
    """A supplier, client or mail can hold markup: the API returns it as JSON text (the app renders text, never HTML)."""
    evil = "<img src=x onerror=alert(1)><script>x</script>"
    ledger.db.execute("UPDATE efactura SET supplier_name=? WHERE message_id='101'", (evil,))
    ledger.db.execute("UPDATE issued SET client_name=? WHERE message_id='5001'", (evil,))
    ledger.db.execute("UPDATE mail_questions SET subject=?, sender=?", (evil, evil))
    ledger.db.commit()
    code, hdr, data = _req(server, "GET", "/web/api/state?co=NORD")
    assert (
        code == 200
        and hdr["content-type"] == "application/json; charset=utf-8"
        and hdr["x-content-type-options"] == "nosniff"
    )
    s = json.loads(data)
    assert s["co"] == "NORD" and s["bounded"] and s["invoices"] == s["issued"] == s["to_check"] == []
    assert _entity(server, "invoice", "101")["supplier"] == evil
    assert _entity(server, "issued", "5001")["client"] == evil
    mail = _entity(server, "mail", scenario.MAIL_KEY)
    assert mail["subject"] == evil and mail["sender"] == evil
    code, s = _get_json(server, "/web/api/state?co=ALL")
    assert code == 200 and [p["co"] for p in s["parts"]] == ["NORD", "STUDIO9"]
    for path, refused in (
        ("/web/api/state?co=NOPE", 400),
        ("/web/api/state", 400),  # never defaults to a company
        ("/web/api/other?co=NORD", 400),
        ("/c/wrong/api/state?co=NORD", 404),
        (f"/web/api/mail?co=NORD&key={scenario.MAIL_KEY}", 400),  # gone: the state carries it
        ("/web/api/invoicing?co=missing", 400),
        ("/web/api/invoicing?co=ALL", 400),
        ("/c/wrong/api/invoicing?co=NORD", 404),
    ):
        assert _get_json(server, path)[0] == refused, path


def test_mailfile_serves_only_documents_waiting_for_an_answer(ledger, world, server, monkeypatch, home):
    key = scenario.MAIL_KEY
    code, hdr, data = _req(server, "GET", f"/web/mailfile?co=NORD&key={key}")
    assert code == 200 and data.startswith(b"%PDF") and hdr["content-type"] == "application/pdf"
    assert hdr["content-disposition"] == "inline; filename=\"document.pdf\"; filename*=UTF-8''document.pdf"
    monkeypatch.setattr(pdftext, "first_page_png", lambda data: b"\x89PNG fake")
    code, hdr, data = _req(server, "GET", f"/web/mailfile?co=NORD&key={key}&png=1")
    assert code == 200 and hdr["content-type"] == "image/png" and data.startswith(b"\x89PNG")
    for bad in ("nm%3Aother", "..%2F..%2Fetc%2Fpasswd", "0" * 64, key.upper()):
        assert _req(server, "GET", f"/web/mailfile?co=NORD&key={bad}")[0] == 404, bad
    assert _req(server, "GET", f"/web/mailfile?co=ALL&key={key}")[0] == 404
    assert _req(server, "GET", f"/c/wrong/mailfile?co=NORD&key={key}")[0] == 404
    # a firm that is not an answer to the question sees nothing of it
    ledger.db.execute(
        "UPDATE mail_questions SET data=json_set(data, '$.reason', 'other-firm', '$.companies', json('[\"STUDIO9\"]'))"
    )
    ledger.db.commit()
    assert _req(server, "GET", f"/web/mailfile?co=NORD&key={key}")[0] == 404
    # a file swapped for a link, or other bytes, is never served
    f = home / "mail" / "pending" / f"{key}.pdf"
    f.write_bytes(b"%PDF-1.4 other bytes")
    assert _req(server, "GET", f"/web/mailfile?co=STUDIO9&key={key}")[0] == 404


def test_docfile_serves_only_receipt_files_inside_their_folders(ledger, world, server, home, monkeypatch):
    s = api.state(ledger, "NORD")
    row, het = _doc(s, "Benzinaria Nord"), _doc(s, "Hetzner")["id"]
    bon = row["id"]
    assert (row["amount"], row["currency"]) == ("300.08", "RON") and bon in s["reports"][0]["documents"]
    assert row["files"] == [{"id": bon, "type": "image"}]  # one row per file, no paths leave the server
    assert _doc(s, "Hetzner")["files"] == [{"id": het, "type": "pdf"}] and _doc(s, "Hetzner")["card"] == "0001"
    assert _doc(s, "Figma")["files"] == [] and _doc(s, "Figma")["card"] is None  # its file is not on this machine
    code, hdr, data = _req(server, "GET", f"/web/docfile?co=NORD&sha={bon}")
    assert code == 200 and hdr["content-type"] == "image/jpeg" and data == b"fake jpeg, Benzinaria Nord"
    assert hdr["content-disposition"].startswith('inline; filename="') and hdr["x-content-type-options"] == "nosniff"
    code, hdr, data = _req(server, "GET", f"/web/docfile?co=NORD&sha={het}")
    assert code == 200 and hdr["content-type"] == "application/pdf" and data.startswith(b"%PDF")
    monkeypatch.setattr(pdftext, "first_page_png", lambda data: b"\x89PNG fake")
    code, hdr, data = _req(server, "GET", f"/web/docfile?co=NORD&sha={het}&png=1")
    assert code == 200 and hdr["content-type"] == "image/png"
    for bad in (
        f"co=STUDIO9&sha={bon}",
        f"co=ALL&sha={bon}",
        "co=NORD&sha=nope",
        "co=NORD",
    ):  # another company's receipt
        assert _req(server, "GET", f"/web/docfile?{bad}")[0] == 404, bad
    assert _req(server, "GET", f"/c/wrong/docfile?co=NORD&sha={bon}")[0] == 404
    stmt = ledger.documents(company="NORD", kind="statement")[0]["sha"]
    (home / "NORD" / "extras.pdf").write_bytes(b"%PDF statement")
    ledger.db.execute("UPDATE documents SET path=? WHERE sha=?", (str(home / "NORD" / "extras.pdf"), stmt))
    ledger.db.commit()
    assert _req(server, "GET", f"/web/docfile?co=NORD&sha={stmt}")[0] == 404  # receipts only
    # a ledger path outside the receipt folders, climbing out of them, through a link, or not a PDF or image: never served
    outside = home.parent / "outside.pdf"
    outside.write_bytes(b"%PDF secret")
    (home / "NORD" / "link.pdf").symlink_to(outside)
    (home / "NORD" / "notes.txt").write_text("x")
    (home / "state" / "copy.pdf").write_bytes(b"%PDF state")
    for path in (
        outside,
        home / "NORD" / ".." / ".." / outside.name,
        home / "NORD" / "link.pdf",
        home / "NORD" / "notes.txt",
        home / "state" / "copy.pdf",
        home / "STUDIO9" / "x.pdf",
    ):
        ledger.db.execute("UPDATE documents SET path=? WHERE sha=?", (str(path), het))
        ledger.db.commit()
        assert _req(server, "GET", f"/web/docfile?co=NORD&sha={het}")[0] == 404, path
        assert _doc(api.state(ledger, "NORD"), "Hetzner")["files"] == [], path
    f = home / "NORD" / "2026-10-23_NORD_Benzinăria-Nord_bon-9.jpg"  # a name with diacritics: ASCII and UTF-8 forms
    f.write_bytes(b"fake jpeg")
    ledger.add_document(
        "ro1", "NORD", "2026-10", "receipt", str(f), vendor="Benzinăria Nord", issues=[], source="manual"
    )
    code, hdr, data = _req(server, "GET", "/web/docfile?co=NORD&sha=ro1")
    assert code == 200 and data == b"fake jpeg"
    assert hdr["content-disposition"].startswith('inline; filename="2026-10-23_NORD_Benzinaria-Nord_')
    assert "filename*=UTF-8''2026-10-23_NORD_Benzin%C4%83ria-Nord" in hdr["content-disposition"]


def test_docfile_reads_only_what_it_checked(ledger, world, server, home, monkeypatch):
    """Whatever changes between the check and the read serves nothing: the file swapped for a link to outside, grown
    past the limit, the company's folder swapped for a link elsewhere, or a FIFO (refused at once, no waiting)."""
    receipt = _doc(api.state(ledger, "NORD"), "Benzinaria Nord")
    bon, sha = receipt["id"], receipt["files"][0]["id"]
    outside = home.parent / "elsewhere"
    (outside / "NORD").mkdir(parents=True)
    (outside / "NORD" / "r.jpg").write_bytes(b"secret")
    (home / "NORD" / "swapped.jpg").symlink_to(outside / "NORD" / "r.jpg")
    (home / "NORD" / "big.jpg").write_bytes(b"too big")
    os.mkfifo(home / "NORD" / "pipe.jpg")
    real_doc_file = api.doc_file

    def swap_root(L, co, sha):  # the check passes, then the company's folder becomes a link to elsewhere
        f = home / "NORD" / "r.jpg"
        f.write_bytes(b"ok")
        checked = real_doc_file(L, co, sha) and f
        (home / "NORD").rename(home / "NORD-old")
        (home / "NORD").symlink_to(outside / "NORD")
        return checked

    for name, limit in (("swapped.jpg", api.DOC_MAX), ("big.jpg", 3), ("pipe.jpg", api.DOC_MAX)):
        monkeypatch.setattr(api, "doc_file", lambda L, co, sha, name=name: home / "NORD" / name)
        monkeypatch.setattr(api, "DOC_MAX", limit)
        assert api.read_doc(ledger, "NORD", bon) is None, name
        if name != "pipe.jpg":  # and over HTTP: the server serves only what read_doc read
            assert _req(server, "GET", f"/web/docfile?co=NORD&sha={sha}")[0] == 404, name
    monkeypatch.setattr(api, "doc_file", swap_root)
    assert api.read_doc(ledger, "NORD", bon) is None


# ------------------------------------------------------------------ HTTP: actions


def test_http_invoice_actions(ledger, world, server):
    code, r = _api(server, "mark-paid", {"co": "NORD", "id": "102", "method": "bank"})
    assert r["ok"] and r["next"] is None and ledger.efactura("102")["status"] == "paid"
    code, r = _api(server, "mark-paid", {"co": "NORD", "id": "102", "method": "bank"})
    assert not r["ok"] and r["message"] == "Factura nu a putut fi marcată. Reîncarcă pagina."
    code, r = _api(server, "reopen", {"co": "NORD", "id": "102"})
    assert r["ok"] and r["next"] is None and ledger.efactura("102")["status"] == "open"
    code, r = _api(server, "reopen", {"co": "NORD", "id": "102"})
    assert not r["ok"]
    code, r = _api(server, "reopen", {"co": "NORD", "id": "111"})  # paid by a statement debit: never undone here
    assert not r["ok"] and ledger.efactura("111")["status"] == "paid"
    code, r = _api(
        server, "withdraw", {"co": "NORD", "id": "104", "method": "bank"}
    )  # already imported in the bank's app
    assert (
        not r["ok"]
        and r["message"] == "Plata este deja importată în BT Go. Dacă nu se mai plătește, șterge-o din BT Go."
    )
    assert ledger.efactura("104")["status"] == "linked"


@pytest.mark.parametrize(
    "body, stored, refusal",
    [
        ({"method": "bank", "detail": "ignored"}, ("bank", None), ""),
        ({"method": "card", "detail": "4821"}, ("card", "4821"), ""),
        ({"method": "cash"}, ("cash", None), ""),
        ({"method": "own", "detail": "  Ana   Pop "}, ("own", "Ana Pop"), ""),
        ({"method": "other", "detail": "Compensare cu factura F-12"}, ("other", "Compensare cu factura F-12"), ""),
        ({}, None, "Alege cum ai plătit."),
        ({"method": "transfer"}, None, "Alege cum ai plătit."),
        ({"method": "card"}, None, "Alege cardul firmei cu care ai plătit."),  # the firm has a card on record
        ({"method": "card", "detail": "1111"}, None, "Alege cardul firmei cu care ai plătit."),  # a personal card
        ({"method": "own", "detail": " "}, None, "Scrie cine a plătit sau cum s-a plătit."),
        ({"method": "other"}, None, "Scrie cine a plătit sau cum s-a plătit."),
        ({"method": "other", "detail": "x" * 201}, None, "Textul poate avea cel mult 200 de caractere."),
    ],
)
def test_mark_paid_records_how_and_stays_awaiting_proof(ledger, world, server, body, stored, refusal):
    with ledger.tx() as db:
        db.execute("INSERT INTO card_owners VALUES('NORD','4821','NORD',NULL,'2026-11-01')")
        db.execute("INSERT INTO card_owners VALUES('NORD','1111','personal',NULL,'2026-11-01')")
    assert _entity(server, "invoice", "102")["paid_method"] is None
    code, r = _api(server, "mark-paid", {"co": "NORD", "id": "102", **body})
    if refusal:
        assert (r["ok"], r["message"]) == (False, refusal) and ledger.efactura("102")["status"] == "open"
        return
    assert r["ok"] and r["state"]["firm_cards"] == [
        "0001",
        "4821",
    ]  # the world's card and this one, not the personal one
    inv = _entity(server, "invoice", "102")  # the method is a fact: the marking still awaits a statement or a mail
    assert (inv["status"], inv["proved"], inv["paid_method"], inv["paid_detail"]) == ("marked", False, *stored)
    assert ledger.mark_efactura_paid("102", "statement 2026-11-03 REF X", "tx-102")  # the proof keeps how
    assert (ledger.efactura("102")["paid_method"], _entity(server, "invoice", "102")["status"]) == (stored[0], "paid")


def test_reopen_forgets_how_it_was_paid(ledger, world, server):
    assert _api(server, "mark-paid", {"co": "NORD", "id": "102", "method": "cash"})[1]["ok"]
    assert _api(server, "reopen", {"co": "NORD", "id": "102"})[1]["ok"]
    row = ledger.efactura("102")
    assert (row["status"], row["paid_method"], row["paid_detail"]) == ("open", None, None)


def test_http_resolve_debit(ledger, world, server):
    [a] = api.state(ledger, "STUDIO9")["ambiguous"]  # two 350.00 invoices of one supplier, a debit that names neither
    assert a["can_resolve"] and a["amount"] == "350.00" and a["counterparty"] == "CONTABIL EXPERT SRL"
    assert [(c["id"], c["number"], c["amount"]) for c in a["candidates"]] == [
        ("203", "CX-1009", "350.00"),
        ("204", "CX-1010", "350.00"),
    ]
    assert api.state(ledger, "NORD")["ambiguous"] == []
    for body in (
        {"co": "NORD", "tx": a["tx"], "target": "204"},
        {"co": "STUDIO9", "tx": a["tx"], "target": "201"},
        {"co": "STUDIO9", "tx": "x" * 20, "target": "204"},
    ):
        code, r = _api(server, "resolve-debit", body)
        assert code == 200 and not r["ok"] and r["message"] == "Plata nu mai așteaptă un răspuns."
    code, r = _api(server, "resolve-debit", {"co": "STUDIO9", "tx": a["tx"], "target": "204"})
    assert r["ok"] and r["message"] == "Factura CX-1010 este confirmată ca plătită, cu plata de pe extras ca dovadă."
    assert r["state"]["ambiguous"] == [] and ledger.efactura("204")["paid_tx"] == a["tx"]
    assert ledger.efactura("203")["status"] == "open"
    code, r = _api(server, "resolve-debit", {"co": "STUDIO9", "tx": a["tx"], "target": "203"})
    assert not r["ok"] and ledger.efactura("203")["status"] == "open"


def test_http_mail_proof_review(ledger, world, server):
    """A payment mail judge left for a person: one tap makes it one invoice's proof, or takes it off the tasks."""
    from fiscalm.api import tasks

    mail = {"key": "imap:<pay@processor.example>", "from": "Plati <no@processor.example>", "date": "2026-10-21"}
    iban = "RO49AAAA1B31007593840000"  # the sender's text shown masked, whenever it was stored
    efactura.set_review(ledger, mail | {"subject": f"Plata confirmata {iban}"}, ["203", "204"], "confirmare plata")
    efactura.set_review(ledger, mail | {"key": "imap:<news@x.example>", "subject": "Oferta"}, ["203"], "plata?")
    s = api.state(ledger, "STUDIO9")
    [_, m] = s["mail_proofs"]  # by key
    rid = efactura.review_id(mail["key"])
    assert (m["id"], [c["id"] for c in m["candidates"]]) == (rid, ["203", "204"])
    assert iban not in json.dumps(s["mail_proofs"]) and mail["key"] not in json.dumps(s) and m["candidate_count"] == 2
    assert "STUDIO9mp" + rid in [key for key, _, _ in tasks.references(s)]
    assert api.state(ledger, "NORD")["mail_proofs"] == []
    for body in (
        {"co": "NORD", "id": rid, "target": "204"},
        {"co": "STUDIO9", "id": rid, "target": "201"},
    ):
        code, r = _api(server, "mail-proof", body)
        assert code == 200 and not r["ok"] and r["message"] == "E-mailul nu mai așteaptă un răspuns."
    code, r = _api(server, "mail-proof", {"co": "STUDIO9", "id": rid})  # no answer is no dismissal
    assert not r["ok"] and len(api.state(ledger, "STUDIO9")["mail_proofs"]) == 2
    code, r = _api(server, "mail-proof", {"co": "STUDIO9", "id": rid, "target": "204"})
    assert r["ok"] and r["message"] == "Factura CX-1010 este confirmată ca plătită, cu e-mailul ca dovadă."
    assert ledger.paid_proofs("STUDIO9")["efactura 204"] == "ef-proof:" + mail["key"]
    news = efactura.review_id("imap:<news@x.example>")
    assert [x["id"] for x in api.state(ledger, "STUDIO9")["mail_proofs"]] == [news]
    code, r = _api(server, "mail-proof", {"co": "STUDIO9", "id": rid, "target": "203"})
    assert not r["ok"] and ledger.efactura("203")["status"] == "open"  # one mail proves one invoice
    code, r = _api(server, "mail-proof", {"co": "STUDIO9", "id": news, "target": ""})
    assert r["ok"] and api.state(ledger, "STUDIO9")["mail_proofs"] == [] and ledger.efactura("203")["status"] == "open"
    efactura.set_review(ledger, mail | {"key": "imap:<news@x.example>", "subject": "Oferta"}, ["203"], "plata?")
    assert api.state(ledger, "STUDIO9")["mail_proofs"] == []  # offered again by a later read: still no
    code, r = _api(server, "mail-proof", {"co": "STUDIO9", "id": news, "target": "203"})
    assert not r["ok"] and ledger.efactura("203")["status"] == "open"
    other = mail | {"key": "imap:<twice@x.example>", "subject": "Plata 1"}
    efactura.set_review(ledger, other, ["203"], "plata")
    efactura.set_review(ledger, other | {"subject": "Plata 2", "from": "Alt <a@other.example>"}, ["203"], "plata")
    assert api.state(ledger, "STUDIO9")["mail_proofs"] == []  # two mails, one Message-ID: neither proves


def test_withdraw_then_reopen_puts_the_row_back_in_the_file(ledger, world, server):
    code, r = _api(server, "withdraw", {"co": "NORD", "id": "105", "method": "cash"})  # in the file: out, marked paid
    assert (
        r["ok"]
        and r["next"] is None
        and ledger.efactura("105")["status"] == "paid"
        and ledger.payment(ledger.efactura("105")["item_id"])["status"] == "skipped"
    )
    inv = _entity(server, "invoice", "105")
    assert (inv["status"], inv["can_reopen"], inv["paid_method"]) == ("marked", True, "cash")
    code, r = _api(server, "reopen", {"co": "NORD", "id": "105"})
    assert (
        r["ok"] and r["message"] == "Marcarea a fost anulată. Factura este din nou pregătită pentru fișierul de plăți."
    )
    assert r["next"] is None and ledger.efactura("105")["status"] == "linked"
    assert ledger.payment(ledger.efactura("105")["item_id"])["status"] in ("pending", "exported")


def test_withdraw_refuses_an_import_interrupted_by_a_crash(ledger, home, monkeypatch):
    go_live(home)
    today = date.today()
    scenario._ef(
        ledger,
        "301",
        "NORD",
        "Furnizor SRL",
        "RO1",
        "N301",
        today - timedelta(days=5),
        today + timedelta(days=20),
        "50.00",
    )
    efactura.approve(ledger, "301", origin="person")
    ident = ledger.create_payment_file("NORD", "Crash")
    assert ledger.attach_payment_file(ident, "NORD", ledger.efactura("301")["item_id"])
    f = pay.download_file(ledger, ident, "NORD", today, lambda: None)
    monkeypatch.setattr(
        pay, "_finish_named_import", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash after journal"))
    )
    with pytest.raises(RuntimeError):
        pay.import_file(ledger, ident, "NORD", f["download_id"], today)
    monkeypatch.undo()
    assert efactura.withdraw(ledger, "301", "user: already paid (page)") == "already imported in the bank's app"
    assert ledger.efactura("301")["status"] == "linked"
    inv = {i["id"]: i for i in api.state(ledger, "NORD")["invoices"]}["301"]
    assert inv["payment_state"] == "imported" and not inv["can_withdraw"] and not inv["can_reopen"]


def test_http_issued_mark_and_unmark(ledger, world, server):
    for body, message in (
        ({"method": "other", "detail": "   "}, "Scrie cum a fost încasată."),
        ({"method": "other", "detail": "x" * 201}, "Textul poate avea cel mult 200 de caractere."),
        ({"method": "own"}, "Alege cum a fost încasată."),
    ):
        code, r = _api(server, "issued-mark", {"co": "NORD", "id": "5001"} | body)
        assert (r["ok"], r["message"], ledger.issued("5001")["status"]) == (False, message, "open")
    code, r = _api(server, "issued-mark", {"co": "NORD", "id": "5001", "note": "numerar"})  # the old field
    assert (r["ok"], r["message"], ledger.issued("5001")["status"]) == (
        False,
        "Cererea nu a putut fi procesată.",
        "open",
    )
    code, r = _api(
        server, "issued-mark", {"co": "STUDIO9", "id": "5001", "method": "cash"}
    )  # another company's invoice
    assert not r["ok"] and ledger.issued("5001")["status"] == "open"
    code, r = _api(server, "issued-mark", {"co": "NORD", "id": "5006", "method": "cash"})  # credited: nothing owed
    assert not r["ok"] and r["message"] == "Factura nu mai este de încasat."
    code, r = _api(server, "issued-mark", {"co": "NORD", "id": "5001", "method": "card"})
    assert r["ok"] and r["message"] == "Factura a fost marcată ca încasată." and r["next"] is None
    row = _entity(server, "issued", "5001")
    assert (row["status"], row["collected_method"], row["note"], row["how"], row["can_unmark"]) == (
        "marked",
        "card",
        "",
        "manual",
        True,
    )
    assert r["state"]["receivable"]["late"] == 2  # the scenario's two invoices waiting on client reminders
    code, r = _api(server, "issued-mark", {"co": "NORD", "id": "5001", "method": "cash"})
    assert not r["ok"]  # already marked
    code, r = _api(server, "issued-unmark", {"co": "NORD", "id": "5004"})  # collected by a statement credit: stays
    assert (
        not r["ok"]
        and r["message"] == "Marcarea acestei facturi nu poate fi anulată."
        and ledger.issued("5004")["status"] == "collected"
    )
    code, r = _api(server, "issued-unmark", {"co": "STUDIO9", "id": "5001"})
    assert not r["ok"] and ledger.issued("5001")["status"] == "marked"
    code, r = _api(server, "issued-unmark", {"co": "NORD", "id": "5001"})
    assert (
        r["ok"]
        and r["message"] == "Marcarea a fost anulată. Factura este din nou de încasat."
        and ledger.issued("5001")["status"] == "open"
    )
    assert _api(server, "issued-mark", {"co": "NORD", "id": "../5001", "method": "cash"})[1]["ok"] is False


def test_http_compose_check_and_mail_answer(ledger, world, server, quiet, monkeypatch):
    code, r = _api(server, "check", {"co": "STUDIO9"})
    assert r["ok"] and quiet == ["tick"] and r["state"]["co"] == "STUDIO9"
    for body in (
        {"who": "nobody"},
        {"who": "STUDIO9", "key": "0" * 64},
        {"who": "STUDIO9", "total": "x"},
        {"who": "STUDIO9", "total": "5"},
    ):
        code, r = _api(server, "mail-answer", {"co": "NORD", "key": scenario.MAIL_KEY} | body)
        assert not r["ok"], body
    assert r["state"]["record_counts"]["to_check"] == 1 and len(quiet) == 1
    code, r = _api(server, "mail-answer", {"co": "NORD", "key": scenario.MAIL_KEY, "who": "STUDIO9", "remember": "1"})
    assert r["ok"] and r["state"]["record_counts"]["to_check"] == 0 and r["state"]["to_check"] == []
    assert (
        ledger.db.execute("SELECT company FROM documents WHERE sha=?", (scenario.MAIL_KEY,)).fetchone()[0] == "STUDIO9"
    )
    assert (
        ledger.db.execute("SELECT company FROM mail_senders WHERE address='receipts@air.example'").fetchone()[0]
        == "STUDIO9"
    )
    code, r = _api(server, "mail-answer", {"co": "NORD", "key": scenario.MAIL_KEY, "who": "STUDIO9"})  # double submit
    assert not r["ok"] and len(quiet) == 1  # an answer files at once: no tick is started


def test_http_bad_requests(ledger, world, server):
    before = ledger.efactura("102")["status"]
    for body in (
        {"co": "NOPE", "id": "102"},
        {"co": "ALL", "id": "102"},  # ALL is for reading only
        {"id": "102"},
        b"[1, 2]",
        b"{not json",
        {"co": "NORD", "id": True},
        {"co": "NORD", "id": {"x": 1}},
    ):
        assert _api(server, "approve", body)[0] == 400, body
    assert _api(server, "delete-everything", {"co": "NORD"})[0] == 400
    assert _api(server, "ef-paid", {"co": "NORD", "id": "102"})[0] == 400  # the old page's form names are gone
    # a cross-site form can't send application/json
    assert _api(server, "mark-paid", b"co=NORD&id=102", "application/x-www-form-urlencoded")[0] == 415
    assert _api(server, "mark-paid", json.dumps({"co": "NORD", "id": "102"}).encode(), "text/plain")[0] == 415
    assert _api(server, "mark-paid", {"co": "NORD", "id": "102"}, Origin="http://evil.example")[0] == 403
    assert _api(server, "mark-paid", {"co": "NORD", "id": "102", "pad": "x" * 5000})[0] == 413
    assert _api(server, "mark-paid", {"co": "NORD", "id": "102"}, user="unknown")[0] == 401
    for path in ("/web/ef-paid", "/web/imported", "/web/", "/web/check"):  # the API is the only write path
        assert (
            _req(server, "POST", path, b"co=NORD&id=102", {"Content-Type": "application/x-www-form-urlencoded"})[0]
            == 404
        )
    assert _api(server, "invoicing/unknown", {"co": "NORD"})[0] == 400
    assert _api(server, "invoicing/recurrence-unknown", b" " * 4097)[0] == 413
    assert _req(server, "GET", "/web/api/state?co=NORD#suffix")[0] == 400
    assert _api(server, "invoicing/auto-upload", {"co": "missing", "on": True})[0] == 400
    assert _api(server, "invoicing/auto-upload", {"co": "NORD", "on": True}, Origin="https://evil.invalid")[0] == 403
    assert _api(server, "invoicing/auto-upload", {"co": "NORD"}, ctype="text/plain")[0] == 415
    assert _api(server, "invoicing/draft-save", b" " * 65537)[0] == 413
    assert _api(server, "invoicing/auto-upload", b"[" * 2000)[0] == 400
    assert _api(server, "invoicing/draft-save", {"co": "NORD", "notes": "x" * 5000})[0] == 200  # past 4 KB: validated
    assert ledger.efactura("102")["status"] == before
    code, r = _api(server, "mark-paid", {"co": "NORD", "id": "102", "method": "bank"}, Origin="https://books.example")
    assert code == 200 and r["ok"]  # same origin, application/json; charset is fine too
    code, r = _api(server, "reopen", {"co": "NORD", "id": "102"}, "application/json; charset=utf-8")
    assert code == 200 and r["ok"]


def test_shadow_local_mark_keeps_debt_until_proof(ledger, world, home, server, quiet):
    (home / "state" / "LIVE").unlink()
    before = api.state(ledger, "NORD")
    assert any(i["can_approve"] for i in before["invoices"])  # adding to a named file is local in Sandbox
    assert any(i["can_mark_paid"] for i in before["invoices"])
    assert all(a["can_resolve"] for a in api.state(ledger, "STUDIO9")["ambiguous"])
    code, result = _api(server, "mark-paid", {"co": "NORD", "id": "102", "method": "bank"})
    assert code == 200 and result["ok"]
    row = _entity(server, "invoice", "102")
    assert row["status"] == "marked" and not row["proved"] and row["can_reopen"]
    assert actions.act("mark-paid", "STUDIO9", {"id": "102"})["ok"] is False
    assert _req(server, "GET", "/web/file/NORD?v=unavailable")[0] == 404


def test_failures_answer_json_never_a_dropped_connection(ledger, home, server, quiet, monkeypatch):
    go_live(home)
    assert _api(server, "check", {"co": [], "id": "101"})[0] == 400
    assert _api(server, "check", {"co": {"a": 1}})[0] == 400
    monkeypatch.setattr(api, "state", lambda L, co: (_ for _ in ()).throw(OSError("/data/fiscalm/secret/path")))
    code, body = _api(server, "check", {"co": "NORD"})
    assert code == 200 and body["state"] is None and body["message"]  # the action ran; its answer isn't lost
    code, hdr, data = _req(server, "GET", "/web/api/state?co=NORD")
    assert code == 500 and b"/data/fiscalm" not in data and json.loads(data) == {"ok": False, "message": actions.ERROR}
    monkeypatch.setitem(
        actions.ACTIONS,
        "mark-paid",
        replace(actions.ACTIONS["mark-paid"], handler=lambda *a: (_ for _ in ()).throw(KeyError("x"))),
    )
    assert _api(server, "mark-paid", {"co": "NORD", "id": "101", "method": "bank"}) == (
        500,
        {"ok": False, "message": actions.ERROR},
    )


# ------------------------------------------------------------------ serving the app


def test_manifest_has_no_credential(home, server):
    code, hdr, data = _req(server, "GET", "/manifest.webmanifest")
    m = json.loads(data)
    assert code == 200 and hdr["content-type"] == "application/manifest+json"
    assert m["start_url"] == m["scope"] == m["id"] == "/" and m["name"] == "fiscalm" and m["display"] == "standalone"
    assert [i["src"] for i in m["icons"]] == ["/fiscalm/icon-192.png", "/fiscalm/icon-512.png"]
    assert _req(server, "GET", "/c/wrong/manifest.webmanifest")[0] == 404


def test_the_app_is_the_page_and_static_files_stay_contained(home, server, tmp_path):
    code, hdr, data = _req(server, "GET", "/")
    assert code == 404 and "nu este instalată" in data.decode()  # no build yet
    assert _req(server, "GET", "/_next/static/chunks/a.js")[0] == 404
    out = tmp_path / "app" / "out"
    (out / "_next" / "static" / "chunks").mkdir(parents=True)
    (out / "fiscalm").mkdir()
    (out / "index.html").write_text("<!doctype html><title>fiscalm</title>")
    (out / "_next" / "static" / "chunks" / "a.js").write_text("console.log(1)")
    (out / "fiscalm" / "icon-192.png").write_bytes(b"\x89PNG")
    (out / "fiscalm" / "notes.py").write_text("x")
    (out / "fiscalm" / ".env").write_text("SECRET=1")
    (tmp_path / "app" / "secret.txt").write_text("outside")
    (out / "fiscalm" / "link.txt").symlink_to(tmp_path / "app" / "secret.txt")
    code, hdr, data = _req(server, "GET", "/?co=STUDIO9&tab=facturi")
    assert (
        code == 200
        and hdr["content-type"].startswith("text/html")
        and b"fiscalm" in data
        and hdr["cache-control"] == "no-store"
    )
    assert _req(server, "GET", "/c/wrong/")[0] == 404
    for gone in ("/c/tok", "/c/tok?co=STUDIO9", "/web/app", "/web/app/", "/web/app/?co=STUDIO9"):
        assert _req(server, "GET", gone)[0] == 404
    for gone in ("/web/static/mona-sans-vf.woff2", "/web/index.html", "/web/x", "/web/api"):
        assert _req(server, "GET", gone)[0] in (400, 404), gone
    code, hdr, _ = _req(server, "GET", "/_next/static/chunks/a.js")
    assert code == 200 and hdr["content-type"].startswith("text/javascript") and "immutable" in hdr["cache-control"]
    code, hdr, _ = _req(server, "GET", "/fiscalm/icon-192.png")
    assert code == 200 and hdr["content-type"] == "image/png" and "immutable" not in hdr["cache-control"]
    for bad in (
        "/_next/../app/secret.txt",
        "/_next/%2e%2e/secret.txt",
        "/fiscalm/..%2f..%2fsecret.txt",
        "/fiscalm/%2e%2e%2f%2e%2e%2fsecret.txt",
        "/fiscalm/.env",
        "/fiscalm/link.txt",
        "/fiscalm/notes.py",
        "/_next/static/chunks/",
        "/_next/static/chunks",
        "/fiscalm//icon-192.png",
        "/fiscalm/%00.png",
        "/fiscalm/..%5c..%5csecret.txt",
        "/index.html",
        "/app/secret.txt",
    ):
        assert _req(server, "GET", bad)[0] == 404, bad


def test_the_clock_drives_every_window(ledger, world):
    """state(now=) and api.clock: the same answer for the same moment, whatever the real time."""
    pinned = api.clock()
    assert api.state(ledger, "NORD", now=pinned) == api.state(ledger, "NORD")
    later = api.state(ledger, "NORD", now=pinned + timedelta(days=60))
    assert later["today"] > api.state(ledger, "NORD")["today"]
    assert isinstance(pinned, datetime)


def test_replaced_statement_is_parsed_again_even_with_the_same_size_and_mtime(tmp_path, monkeypatch):
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"AAAA")
    calls = []
    monkeypatch.setattr(api.statements, "parse", lambda p, co: calls.append(p.read_bytes()) or len(calls))
    api._STATEMENTS.clear()
    assert api._statement(pdf, "NORD") == 1 and api._statement(pdf, "NORD") == 1  # parsed once
    st = pdf.stat()
    pdf.write_bytes(b"BBBB")
    os.utime(pdf, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size, same mtime, other content
    assert api._statement(pdf, "NORD") == 2


def test_invoicing_http_workspace_and_local_commands(server, ledger, monkeypatch):
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    seller, client = invoicing_api.party_out(PARTY, True), invoicing_api.party_out(PARTY)
    assert _inv(server, "seller-save", seller=seller)["ok"]
    assert _inv(server, "series-create", code="APP", start=1)["ok"]
    assert not _inv(server, "series-create", code="APP", start=1)["ok"]
    saved_client = _inv(server, "client-save", client=client)
    cid = saved_client["id"]
    assert saved_client["state"]["bounded"] and len(saved_client["state"]["partners"]) == 1
    assert saved_client["state"]["partners"][0]["client_id"] == cid and not saved_client["state"]["issued"]
    monkeypatch.setattr(anaf_public, "lookup", lambda *a, **kw: PARTY)
    assert _inv(server, "client-lookup", cui="99999991")["client"] == client
    assert len(invoicing_api.workspace(ledger, "NORD")["clients"]) == 1

    def offline(*a, **kw):
        raise anaf_public.AnafLookupError("ANAF lookup is unavailable; retry later.")

    monkeypatch.setattr(anaf_public, "lookup", offline)
    assert not _inv(server, "client-lookup", cui="99999991")["ok"]
    body = dict(
        series="APP", client_id=cid, **(DATA | dict(issue_date="2026-11-01", lines=[LINE | dict(exemption="")]))
    )
    saved = _inv(server, "draft-save", **body)
    iid, draft = saved["id"], saved["invoicing"]["drafts"][0]
    assert saved["ok"] and draft["client_id"] == cid and draft["totals"]["total"] == "121.00"
    assert draft["updated_at"] and draft["exchange_rate"] is None
    assert _inv(server, "draft-save", id=iid, version=1, **(body | dict(notes="Modificat")))["ok"]
    monkeypatch.setattr(bnr, "rate", lambda *a, **kw: ("5.0000", "2026-10-30"))
    eur = _inv(server, "draft-save", id=iid, version=2, **(body | dict(currency="EUR")))
    assert eur["invoicing"]["drafts"][0]["totals"]["total_ron"] == "605.00"
    code, headers, pdf = _req(server, "GET", f"/web/invoicing/preview.pdf?co=NORD&draft={iid}")
    assert code == 200 and pdf.startswith(b"%PDF") and "sandbox" not in headers["content-security-policy"]
    text = subprocess.run(["pdftotext", "-", "-"], input=pdf, capture_output=True).stdout.decode()
    assert "Ciornă" in text and "Fără număr" in text and "APP-1" not in text
    assert _req(server, "GET", f"/web/invoicing/{iid}.pdf?co=NORD")[0] == 404
    assert _req(server, "GET", f"/web/invoicing/preview.pdf?co=STUDIO9&draft={iid}")[0] == 404
    for op in ("upload",):
        assert _inv(server, op, id=iid)["message"] == actions.SHADOW
    assert _inv(server, "auto-upload", on=False)["ok"]  # no Origin header: a client outside the browser is served
    assert _inv(server, "auto-upload", on=True)["invoicing"]["auto_upload"]
    assert _inv(server, "draft-delete", id=iid)["ok"]
    assert not _inv(server, "draft-delete", id=iid)["ok"]
    assert not _inv(server, "client-save", id=cid, client={})["ok"]
    assert not _inv(server, "draft-save", **(body | dict(client_id="missing")))["ok"]
    assert not _inv(server, "seller-save", seller={})["ok"]


def test_invoicing_issue_upload_resolve_storno_http(server, ledger, home, monkeypatch):
    monkeypatch.setattr("fiscalm.ledger.now", lambda: "2026-11-01T10:00:00")
    _, iid = _draft(ledger, "WEB", DATA | dict(issue_date="2026-11-01"))
    go_live(home)
    anaf_fake.setup(ledger)  # its default: the tick sends an invoice 4 days after issue
    with monkeypatch.context() as at_issue:  # issuing never uploads
        at_issue.setattr(invoice_spv, "upload", lambda *a, **kw: pytest.fail("uploaded at issue"))
        assert not _inv(server, "issue", co="STUDIO9", id=iid, version=1)["ok"]
        out = _inv(server, "issue", id=iid, version=1)
    [row] = out["state"]["issued"]
    assert out["ok"] and (row["spv"]["state"], row["spv"]["upload_on"]) == ("none", "2026-11-05")
    assert not _inv(server, "issue", id=iid, version=1)["ok"]
    for ext in ("pdf", "xml"):
        code, headers, _ = _req(server, "GET", f"/web/invoicing/{iid}.{ext}?co=NORD")
        assert code == 200 and ("sandbox" in headers["content-security-policy"]) is (ext == "xml")
        assert _req(server, "GET", f"/web/invoicing/{iid}.{ext}?co=STUDIO9")[0] == 404
    assert not _inv(server, "draft-delete", id=iid)["ok"]
    assert not _inv(server, "storno", id=iid, reason="Corecție")["ok"]  # not accepted in SPV
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,?,?)",
            (iid, anaf.env(), "uploading", "hash"),
        )
    with invoice_spv._uploading():
        busy = _inv(server, "resolve-upload", id=iid, upload_id=None, checked_in_spv=True)
    assert not busy["ok"] and busy["message"] == "Trimiterea este încă în curs. Verifică în SPV după ce se termină."
    with ledger.tx() as db:
        db.execute("UPDATE invoice_submissions SET status='unknown' WHERE invoice_id=?", (iid,))
    assert not _inv(server, "resolve-upload", id=iid, upload_id=None, checked_in_spv=False)["ok"]
    assert not _inv(server, "resolve-upload", co="STUDIO9", id=iid, upload_id=None, checked_in_spv=True)["ok"]
    assert _inv(server, "resolve-upload", id=iid, upload_id="987", checked_in_spv=True)["ok"]
    assert not _inv(server, "resolve-upload", id=iid, upload_id=None, checked_in_spv=True)["ok"]
    monkeypatch.setattr(anaf, "prepare_invoice_request", lambda *args: lambda *a: b'<header index_incarcare="987"/>')
    assert not _inv(server, "upload", id=iid)["ok"]
    # accepted, and SPV's answer names the configured seller CUI (the SPV identity link)
    cui = companies.get("NORD").cui
    frozen = invoicing.invoice(ledger, iid, company="NORD")["data"]
    frozen["seller"] |= dict(cui=cui, vat_id="RO" + cui)
    with ledger.tx() as db:
        db.execute("UPDATE invoices SET data=? WHERE id=?", (json.dumps(frozen), iid))
    xml = ubl.generate(ledger, iid, company="NORD")
    with ledger.tx() as db:
        db.execute(
            "UPDATE invoice_submissions SET status='accepted', xml_sha=? WHERE invoice_id=?",
            (hashlib.sha256(xml).hexdigest(), iid),
        )
    issued.store(ledger, "NORD", dict(id="987654", data_creare="2026-11-01", id_solicitare="987"), _spv_answer(xml))
    [row] = api.state(ledger, "NORD")["issued"]
    assert (row["id"], row["local_id"], row["origin"]) == (invoicing.import_id("local", iid), iid, "fiscalm")
    assert not _inv(server, "upload", co="STUDIO9", id=iid)["ok"]
    result = _inv(server, "storno", id=iid, reason="Corecție")
    storno_id = result["id"]
    assert len(result["state"]["issued"]) == 1 and result["state"]["issued"][0]["local_id"] == storno_id
    original = _entity(server, "issued", iid)
    assert result["ok"] and (original["credited"], original["owed"], original["reversed_by"]) == (
        "121.00",
        "0.00",
        [storno_id],
    )
    issued.store(
        ledger,
        "NORD",
        dict(id="987655", data_creare="2026-11-01", id_solicitare="988"),
        _spv_answer(ubl.generate(ledger, storno_id, company="NORD")),
    )
    rows = {r["local_id"]: r for r in api.state(ledger, "NORD")["issued"]}
    assert len(rows) == 2 and rows[iid]["credited"] == "121.00" and rows[storno_id]["storno_of"] == iid
    assert not _inv(server, "storno", id=iid, reason="Corecție")["ok"]


def test_invoicing_refusals_preserve_company_and_messages(server, ledger, monkeypatch):
    data = DATA | dict(issue_date="2026-10-27", due_date="2026-11-27")
    cid, iid = _draft(ledger, "ISOLATE", data)
    draft = dict(series="ISOLATE", client_id=cid, **data)
    for op, body in (
        ("client-save", dict(id=cid, client=invoicing_api.party_out(PARTY))),
        ("draft-save", dict(id=iid, **draft)),
        ("draft-delete", dict(id=iid)),
    ):
        assert not _inv(server, op, co="STUDIO9", **body)["ok"], op
    out = _inv(server, "series-create", code="ISOLATE", start=1)
    assert out["message"] == "Seria există deja și numărul ei nu poate fi resetat."

    def offline(*a, **kw):
        raise bnr.RateError("Cursul BNR nu este disponibil. Încearcă din nou mai târziu.")  # what bnr.fetch_xml raises

    monkeypatch.setattr(bnr, "rate", offline)
    out = _inv(server, "draft-save", **(draft | dict(currency="EUR")))
    assert not out["ok"] and out["message"] == "Cursul BNR nu este disponibil. Încearcă din nou mai târziu."
    assert invoicing.invoice(ledger, iid, company="NORD")["status"] == "draft"


def test_client_lookup_save_edit_preserves_profile(server, ledger, monkeypatch):
    # The client's own registry number and phone, so the PDF can't show the seller's (PARTY) instead.
    profile = PARTY | dict(
        efactura_registered=True,
        inactive=True,
        account_name="Cont exemplu",
        reg_com="J00/0002/2026",
        phone="0000000002",
    )
    monkeypatch.setattr(anaf_public, "lookup", lambda *a, **kw: profile)
    client = _inv(server, "client-lookup", cui=PARTY["cui"])["client"]
    assert client["inactive"] and not _inv(server, "client-save", client=client)["ok"]
    profile["inactive"] = False
    client = _inv(server, "client-lookup", cui=PARTY["cui"])["client"]
    cid = _inv(server, "client-save", client=client)["id"]
    edited = {k: client[k] for k in ("name", "cui", "country", "address")} | dict(name="Client modificat")
    assert _inv(server, "client-save", id=cid, client=edited)["ok"]
    assert invoicing_api.workspace(ledger, "NORD")["clients"][0] == client | dict(
        name=edited["name"], id=cid, last_used=None, last_invoice=None
    )
    invoicing.save_seller(ledger, "NORD", PARTY)
    invoicing.create_series(ledger, "NORD", "PDF")
    pdf = invoice_pdf.generate(
        ledger, invoicing.save_draft(ledger, "NORD", "PDF", cid, DATA), company="NORD", preview=True
    )
    text = subprocess.run(["pdftotext", "-", "-"], input=pdf, capture_output=True, check=True).stdout.decode()
    assert profile["reg_com"] in text and profile["phone"] in text


def test_committed_storno_survives_a_failing_refresh(server, ledger, home, monkeypatch):
    """Once reversed, a failing state refresh leaves the answer ok and logs no detail."""
    go_live(home)
    monkeypatch.setattr("fiscalm.ledger.now", lambda: "2026-11-01T10:00:00")
    _, iid = _draft(ledger, "COMMIT", DATA | dict(issue_date="2026-11-01"))
    invoicing.issue(ledger, iid, "2026-11-01", company="NORD", version=1)
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,?,?)",
            (iid, anaf.env(), "accepted", "hash"),
        )

    def fail(*a, **kw):
        raise RuntimeError("sensitive text must not be logged")

    monkeypatch.setattr(invoicing_api, "workspace", fail)
    monkeypatch.setattr(api, "state", fail)
    out = _inv(server, "storno", id=iid, reason="Corecție")
    assert out["ok"] and invoicing.invoice(ledger, out["id"], company="NORD")["status"] != "draft"
    assert out["state"] is out["invoicing"] is None
    logs = "".join(p.read_text() for p in (home / "logs").glob("*.log"))
    assert "RuntimeError" in logs and "sensitive text" not in logs


def test_expenses_state(ledger, world):
    first = api.state(
        ledger, "NORD", scenario.NOW
    )  # the same moment, the same state: test_the_clock_drives_every_window
    pair = next(e for e in first["expenses"] if e["invoice"] == "112")
    assert (
        pair["document"] is not None
        and pair["document"] == next(i for i in first["invoices"] if i["id"] == "112")["bon"]
    )
    for part in api.state(ledger, "ALL", scenario.NOW)["parts"]:
        assert all(e["company"] == part["co"] for e in part["expenses"])
        assert {e["invoice"] for e in part["expenses"] if e["invoice"]} == {i["id"] for i in part["invoices"]}
        assert {e["document"] for e in part["expenses"] if e["document"]} == {d["id"] for d in part["documents"]}
        assert len({e["id"] for e in part["expenses"]}) == len(part["expenses"])
        if part["co"] == "STUDIO9":
            assert any(e["invoice"] == "205" and e["document"] is None for e in part["expenses"])


def test_mail_answer_first_total_and_firm_scope(ledger, world, server, quiet):
    """The first of several totals is index "0" (never read as no total), and a firm the document isn't offered to
    can't answer it, not even with none."""
    data = json.loads(
        ledger.db.execute("SELECT data FROM mail_questions WHERE sha=?", (scenario.MAIL_KEY,)).fetchone()[0]
    )
    data |= {"reason": "two-firms", "companies": ["STUDIO9"], "totals": [["249.90", "EUR"], ["209.92", "EUR"]]}
    ledger.db.execute("UPDATE mail_questions SET data=? WHERE sha=?", (json.dumps(data), scenario.MAIL_KEY))
    ledger.db.commit()
    assert not _api(server, "mail-answer", {"co": "NORD", "key": scenario.MAIL_KEY, "who": "none"})[1]["ok"]
    code, r = _api(server, "mail-answer", {"co": "STUDIO9", "key": scenario.MAIL_KEY, "who": "STUDIO9", "total": "0"})
    assert r["ok"], r["message"]
    row = ledger.db.execute("SELECT company, amount FROM documents WHERE sha=?", (scenario.MAIL_KEY,)).fetchone()
    assert tuple(row) == ("STUDIO9", "249.90")


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize(
    "registry,name", [(registry, name) for registry in (actions.ACTIONS, invoicing_api.ACTIONS) for name in registry]
)
def test_registered_action_guards(ledger, home, monkeypatch, registry, name, live):
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    spec = registry[name]
    monkeypatch.setitem(registry, name, replace(spec, handler=lambda *args: actions._res(True, "")))
    monkeypatch.setattr(invoicing_api, "_completed", lambda *args: {"ok": True})
    (home / "state").mkdir(exist_ok=True)
    if live:
        (home / "state" / "LIVE").touch()
    body = {"data": b"fixture"} if name == "logo" else {"on": False} if name == "auto-upload" else {}

    def run(body, **kwargs):
        return (
            actions.act(name, "NORD", body, **kwargs)
            if registry is actions.ACTIONS
            else invoicing_api.act(ledger, name, "NORD", body, **kwargs)
        )

    assert not run(body | {"unexpected": "value"})["ok"]
    assert not run(body, require=lambda need: False)["ok"]
    result = run(body)
    assert result["ok"] == (live or spec.effect == "local")
    if not live and spec.effect != "local":
        assert result["message"] == actions.SHADOW


@pytest.mark.parametrize("action", sorted(set(actions.ACTIONS) - set(actions.LOCAL_ACTIONS)))
def test_outward_actions_are_refused_in_shadow_through_both_transports(
    token_owner, ledger, server, world, home, action
):
    """Through the HTTP routes, not the dispatcher: API v1 refuses (403, or 400 for an action it does not expose) and
    the app answers with the shadow refusal."""
    (home / "state" / "LIVE").unlink()
    writer = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    body = json.dumps({"co": "NORD", "who": "NORD"}).encode()
    v1 = {"Authorization": "Bearer " + writer, "Content-Type": "application/json"}
    code, _, _ = _req(server, "POST", "/api/v1/companies/NORD/actions/" + action, body, v1)
    assert code == (403 if action in actions.API_ACTIONS else 400)
    code, _, raw = _req(server, "POST", "/web/api/" + action, body, {"Content-Type": "application/json"})
    assert code == 200 and json.loads(raw)["message"] == actions.SHADOW


def test_app_bounded_reads_authenticate_and_keep_sheet_lookup(server, ledger, world):
    for endpoint in ("state-summary", "state-list?collection=issued&limit=1"):
        assert _req(server, "GET", "/c/invalid/api/" + endpoint)[0] == 404
    status, _, body = _req(server, "GET", "/web/api/state-summary?co=NORD")
    assert status == 200
    assert json.loads(body)["payable"] == api.state(ledger, "NORD")["payable"]
    status, _, body = _req(server, "GET", "/web/api/state-list?co=NORD&collection=invoices&limit=1")
    assert status == 200 and len(json.loads(body)["data"]) == 1
    assert _req(server, "GET", "/web/api/state-list?co=ALL&collection=invoices")[0] == 404


@pytest.mark.parametrize("co,action,body", [("NORD", "check", {}), ("ALL", "update-checks", {"on": "0"})])
def test_private_action_and_legacy_state_route_return_bounded_summaries(server, ledger, world, quiet, co, action, body):
    full = api.state(ledger, co)
    code, result = _api(server, action, {"co": co, **body})
    assert code == 200 and result["ok"]
    if co == "NORD":
        assert quiet
    summary = result["state"]
    assert summary == lists.summary(ledger, co, can_manage_companies=True)
    assert _get_json(server, "/web/api/state?co=" + co) == (200, summary)
    for key in ("payable", "receivable", "cod_receivable", "companies"):
        assert summary[key] == full[key]
    for old, small in zip(full.get("parts", [full]), summary.get("parts", [summary]), strict=True):
        assert small["bounded"] and small["invoices"] == small["issued"] == small["documents"] == []
        assert "payments" not in small and not small["transactions"]["rows"]
        assert small["record_counts"]["invoices"] == len(old["invoices"])


@pytest.mark.parametrize("empty", [False, True])
def test_bounded_read_failures_return_safe_json(server, world, monkeypatch, empty):
    logged = []
    complete = threading.Event()

    def record(line):
        logged.append(line)
        if line.startswith("web: GET api/state-list "):
            complete.set()

    monkeypatch.setattr(web, "log", record)
    if empty:
        monkeypatch.setattr(companies, "all", lambda *args: {})
    else:

        def broken(*args, **kwargs):
            raise RuntimeError("synthetic-sensitive")

        monkeypatch.setattr(web.lists, "collection", broken)
    status, _, body = _req(server, "GET", "/web/api/state-list?co=NORD&collection=invoices")
    assert status == (409 if empty else 500)
    assert json.loads(body) == {"ok": False, "message": companies.NEEDS_COMPANY_RO if empty else actions.ERROR}
    # The response can reach the client before _dispatch's finally writes its access line.
    assert complete.wait(2)
    access = f"web: GET api/state-list {409 if empty else 500} ms"
    assert [re.sub(r"\d+ms$", "ms", line) for line in logged] == (
        [access] if empty else ["web: api state-list failed: RuntimeError", access]
    )


@pytest.mark.parametrize(
    "effect,error",
    [
        (lambda L: anaf.prepare_invoice_request(L), anaf.AnafError),
        (lambda L: invoice_spv.upload(L, "missing", company="NORD"), (invoicing.InvoiceError, anaf.AnafError)),
        (lambda L: webhooks.send("https://example.invalid", b"{}", {}), ValueError),
        (lambda L: btgo.build([], date(2026, 11, 4)), PaymentFileError),
        (lambda L: pay.download_file(L, 1, "NORD", date.today(), lambda: None), PaymentFileError),
        (lambda L: pay.resolve_missing_file(L, 1, "NORD", "", date.today(), imported=False), PaymentFileError),
        (lambda L: pay.import_file(L, 1, "NORD", 1, date.today()), PaymentFileError),
        (lambda L: pay.resolve_missing_file(L, 1, "NORD", "", date.today(), imported=True), PaymentFileError),
    ],
)
def test_outward_effect_refuses_direct_call_in_shadow(ledger, mail_account, effect, error, monkeypatch):
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    before = ledger.db.total_changes
    with pytest.raises(error):
        effect(ledger)
    assert ledger.db.total_changes == before


@pytest.mark.parametrize("change", ["readonly", "disabled", "revoked"])
def test_download_rechecks_session_under_writer_lock(ledger, home, server, monkeypatch, change):
    _file_with_op(ledger, home)
    ident = ledger.payments(company="NORD")[0]["file_id"]
    repair = pay._recover_named

    def revoke_after_admission(connection, today):
        repair(connection, today)
        assert not connection.db.in_transaction  # File journals must commit before their moves.
        other = Ledger()
        try:
            sql = {
                "readonly": "UPDATE users SET role='readonly' WHERE id='u_member'",
                "disabled": "UPDATE users SET disabled_at='2026-11-01T00:00:00Z' WHERE id='u_member'",
                "revoked": "UPDATE sessions SET revoked_at='2026-11-01T00:00:00Z' WHERE user_id='u_member'",
            }[change]
            with other.tx():
                other.db.execute(sql)
        finally:
            other.close()

    authorize = web.auth.authorize_write

    def locked_authority(connection, *args, **kwargs):
        assert connection.db.in_transaction
        return authorize(connection, *args, **kwargs)

    monkeypatch.setattr(web.auth, "authorize_write", locked_authority)
    monkeypatch.setattr(pay, "_recover_named", revoke_after_admission)
    code, result = _api(server, "payment-files", {"action": "payment_file_download", "file_id": ident}, user="member")
    assert code == 403 and result["ok"] is False
    assert len(pay.file_downloads(ledger, ident)) == 1


def test_detail_carries_invoice_lines_within_the_company(server, world):
    def lines(kind, identity, co="NORD", user="readonly"):
        query = urllib.parse.urlencode({"co": co, "kind": kind, "id": identity})
        return _get_json(server, "/web/api/detail?" + query, user=user)

    received, spv, local = lines("invoice", "102")[1], lines("issued", "5001")[1], lines("issued", "local-accepted")[1]
    assert [(r["description"], r["quantity"], r["unit"], r["net"]) for r in received["lines"]["lines"]] == [
        ("Abonament internet fibră, octombrie", "1", "buc", "74.37"),
        ("Abonament telefonie mobilă", "2", "buc", "24.80"),
    ]
    assert [r["unit"] for r in spv["lines"]["lines"]] == ["oră", "zi"] and not spv["lines"]["unavailable"]
    assert local["lines"]["lines"] == [
        dict(position=1, product_id="catalog-design", description="Servicii de proiectare", quantity="1", unit="oră", unit_price="100.00",
             net="100.00", vat_rate="21", vat_category="S", vat="21.00", gross="121.00")
    ]  # fmt: skip
    assert local["lines"]["totals"] == {  # as issued
        "net": "100.00", "vat": "21.00", "total": "121.00", "payable": "121.00",
        "taxes": [{"category": "S", "rate": "21", "net": "100.00", "vat": "21.00"}],
    }  # fmt: skip
    assert received["lines"]["totals"]["payable"] == "120.00"  # the stored XML's own
    assert lines("invoice", "101")[1]["lines"] == invoice_lines.unavailable()  # no stored document
    assert lines("invoice", "102", co="STUDIO9")[0] == 404
    assert lines("invoice", "102", user="nobody")[0] == 401


@pytest.mark.parametrize("action", ["approve", "imported", "restore", "download"])
def test_replaced_payment_actions_are_removed(server, ledger, action):
    before = ledger.db.total_changes
    assert _api(server, action, {"co": "NORD", "id": "101", "v": "old"})[0] == 400
    assert ledger.db.total_changes == before


@pytest.mark.parametrize("user", ["admin", "member", "readonly"])
def test_bootstrap_binds_identity_permissions_and_dashboard_to_cookie(ledger, server, user):
    code, result = _get_json(server, "/web/api/bootstrap?co=NORD", user=user)
    assert code == 200
    identity = _get_json(server, "/web/api/me", user=user)[1]
    assert result["identity"]["me"] == identity["me"]
    assert result["identity"]["auth"] == identity["auth"]
    assert next(s["id"] for s in result["identity"]["sessions"] if s["current"]) == next(
        s["id"] for s in identity["sessions"] if s["current"]
    )
    assert result["state"]["co"] == "NORD"
    assert result["state"]["can_manage_companies"] == (user == "admin")
    assert result["state"]["dashboard_pages"]
    assert result["live"] == result["state"]["live"] is False
    assert _get_json(server, "/web/api/bootstrap", user="unknown")[0] == 401
    assert _get_json(server, "/web/api/bootstrap?co=GONE", user=user)[1]["state"]["co"] == "ALL"


def test_bootstrap_recovery_session_has_no_company_data(ledger, server):
    with ledger.tx() as db:
        db.execute("UPDATE sessions SET recovery_only=1 WHERE user_id='u_admin'")
    code, result = _get_json(server, "/web/api/bootstrap?co=NORD")
    assert code == 200 and result["identity"]["auth"]["recovery_only"]
    assert result["state"] is None


def test_bootstrap_keeps_one_snapshot_during_role_change(ledger, server, monkeypatch):
    ledger.db.execute("PRAGMA journal_mode=WAL")
    original = web.auth.me

    def demote_after_identity(L, principal, at, **options):
        identity = original(L, principal, at, **options)
        other = Ledger()
        try:
            with other.tx() as db:
                db.execute("UPDATE users SET role='readonly' WHERE id='u_admin'")
        finally:
            other.close()
        return identity

    monkeypatch.setattr(web.auth, "me", demote_after_identity)
    code, result = _get_json(server, "/web/api/bootstrap?co=NORD")
    assert code == 200 and result["identity"]["me"]["role"] == "admin"
    assert result["state"]["can_manage_companies"] is True
    assert _get_json(server, "/web/api/bootstrap?co=NORD")[1]["identity"]["me"]["role"] == "readonly"


def test_app_reads_leave_one_access_line_without_query_ids_or_user(server, home):
    # a read with filter values, one naming a file id, one without a session, a route the app doesn't know, and an action
    assert _get_json(server, "/web/api/browse?co=NORD&screen=vanzari&q=Client%20Secret")[0] == 200
    _get_json(server, "/web/api/payment-files/4242")
    assert _get_json(server, "/web/api/companies", user="nobody")[0] == 401
    _get_json(server, "/web/api/Secret.Path")
    _get_json(server, "/web/api/synthetic-secret-token")
    _api(server, "check", {"co": "NORD"})
    lines = [
        line.split(" ", 2)[2]
        for p in (home / "logs").glob("*.log")
        for line in p.read_text().splitlines()
        if line.split(" ", 3)[2:3] == ["web:"] and " GET " in line
    ]
    assert [re.sub(r"\d+ms$", "ms", line) for line in lines] == [
        "web: GET api/browse 200 ms",
        "web: GET api/payment-files/:id 404 ms",
        "web: GET api/companies 401 ms",
        "web: GET api/? 400 ms",
        "web: GET api/? 400 ms",
    ]


def test_a_burst_of_connections_waits_in_the_listen_queue(server):
    # a page load through a proxy opens a connection per request; while every handler is busy, none is dropped
    busy = web.Server(("127.0.0.1", 0), web.Handler)  # listening, accepting nothing
    sockets = []
    try:
        for _ in range(40):
            sockets.append(socket.create_connection(busy.server_address, timeout=0.5))
    finally:
        for s in sockets:
            s.close()
        busy.server_close()
    assert len(sockets) == 40 and busy.daemon_threads
