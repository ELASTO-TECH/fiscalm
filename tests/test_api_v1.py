import json
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import quote

import pytest
from test_api import (
    _req,  # noqa: F401
    quiet as quiet,
    server as server,
    world as world,
)
from test_efactura import SUPPLIER_IBAN, _stored, ubl
from test_settlements import setup as setup_settlement

from fiscalm import audit, integrations
from fiscalm.api import (
    actions,
    browse as browsing,
    details,
    lists,
    partner_rows,
    state as api,
    state as api_resources,
    tokens,
    transaction_choices,
    transaction_rows,
    uploads as upload_links,
    v1 as api_v1,
)
from fiscalm.ledger import Ledger
from fiscalm.payments.orders import PaymentItem


def request(server, secret, path="/companies", method="GET", body=None, **headers):
    hdr = {"Authorization": "Bearer " + secret, **headers}
    if method == "POST":
        hdr.setdefault("Content-Type", "application/json")
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
        hdr.setdefault("Content-Type", "application/json")
    code, hdr, raw = _req(server, method, "/api/v1" + path, body, hdr)
    assert hdr["fiscalm-api-version"] == "1"
    return code, json.loads(raw) if raw and hdr["content-type"].startswith("application/json") else raw


@pytest.mark.parametrize("credit", [False, True])
def test_transaction_choices_keep_open_closed_currency_and_holder_candidates(ledger, world, credit):
    full = api.state(ledger, "NORD")
    full["prepared_payments"] = api.prepared_payments(ledger, "NORD")
    tx = full["transactions"]["rows"][0]
    partner, other = full["partners"][:2]
    partner.update(roles=["supplier", "client", "courier"], held=[{"invoice": "7"}])
    source = "issued" if credit else "invoices"
    template = full[source][0]
    full[source] = [
        template
        | dict(
            id=str(i),
            number=f"SYN-{i}",
            issue_date="2026-10-01",
            partner=partner["id"],
            status="open",
            left="10.00",
            currency="RON",
            rejected=False,
            local_id=None,
        )
        for i in range(1, 11)
    ]
    for index, changes in {
        1: dict(status="marked", currency="EUR"),
        2: dict(rejected=True),
        3: dict(local_id="4"),
        4: dict(status="collected" if credit else "paid", left="0.00"),
        5: dict(currency="GBP"),
        6: dict(partner=other["id"]),
        7: dict(partner=other["id"]),
        8: dict(left="0.00"),
        9: dict(currency=None),
    }.items():
        full[source][index].update(changes)
    link = dict(
        kind="documents", partner=partner["id"], parts=[dict(id="1", amount="1.00"), dict(id="5", amount="3.00")]
    )
    tx.update(
        direction="credit" if credit else "debit",
        currency="RON",
        amount="173.45",
        orig=dict(currency="EUR", amount="10.00"),
        explanation={"link": link},
    )
    payment = next(p for name in ("held", "in_file", "lost", "imported") for p in full["prepared_payments"][name])
    full["prepared_payments"] = {name: [] for name in full["prepared_payments"]} | {
        "held": [payment | dict(payee="Same amount", amount="173.45"), payment | dict(payee="Other", amount="173.46")]
    }
    pages = transaction_choices.project(full, {"id": tx["id"]})["pages"]
    assert [r["id"] for r in pages["invoices"]["data"]] == (
        ["1", "2", "7", "5"] if credit else ["1", "10", "2", "3", "4", "5"]
    )
    assert [(r["id"], r["left"]) for r in pages["selected"]["data"]] == [("1", "11.00"), ("5", "3.00")]
    if credit:
        assert next(r for r in pages["invoices"]["data"] if r["id"] == "7")["partner"] == other["id"]
        assert not pages["payments"]["data"]
    else:  # offered: only a prepared payment of the debit's own lei amount; searched: the whole file
        assert [r["payment"]["payee"] for r in pages["payments"]["data"]] == ["Same amount"]
        searched = transaction_choices.project(full, {"id": tx["id"], "collection": "payments", "q": "a"})
        assert searched["pages"]["payments"]["total"] == 2
        tx.update(currency="EUR", amount="20.00", orig=dict(currency="RON", amount="-173.45"))  # a signed original
        pages = transaction_choices.project(full, {"id": tx["id"], "collection": "payments"})["pages"]
        assert [r["payment"]["payee"] for r in pages["payments"]["data"]] == ["Same amount"]


def test_transaction_choice_pages_search_selected_money_scope_and_public_payment_ids(
    token_owner, ledger, server, world, monkeypatch
):
    full = api.state(ledger, "NORD")
    full["prepared_payments"] = api.prepared_payments(ledger, "NORD")
    tx, partner, invoice = full["transactions"]["rows"][0], full["partners"][0], full["invoices"][0]
    full["partners"] = [
        partner | dict(id=f"NORD.choice-{i}", name=f"Firmă aleasă {i:03}", roles=["supplier"], held=[])
        for i in range(151)
    ]
    owner = full["partners"][0]["id"]
    full["invoices"] = [
        invoice | dict(id=str(90000 + i), number=f"SYN-{i}", partner=owner, status="paid", left="0.00")
        for i in range(151)
    ]
    payment = next(p for name in ("held", "in_file", "lost", "imported") for p in full["prepared_payments"][name])
    full["prepared_payments"] = {name: [] for name in full["prepared_payments"]}
    full["prepared_payments"]["held"] = [
        payment | dict(item_id=f"private-payment-{i}", payee=f"Beneficiar {i}", amount=tx["amount"]) for i in range(151)
    ]
    parts = [dict(id=r["id"], amount="90071992547409.93", closed=True) for r in full["invoices"]]
    tx.update(
        direction="debit",
        currency="RON",
        can_link=True,
        explanation={
            "kind": "payment",
            "invoices": [r["id"] for r in parts],
            "payments": [],
            "link": dict(kind="documents", partner=owner, parts=parts),
        },
    )
    calls = []

    def read(*args, **kwargs):
        calls.append(args[1])
        return full

    monkeypatch.setattr(api, "state", read)
    monkeypatch.setattr(api, "prepared_payments", lambda *args: full["prepared_payments"])
    initial = lists.detail(ledger, "ALL", {"kind": "transaction", "id": tx["id"]})["transaction"]["choices"]
    assert calls == ["NORD"] and initial.pop("proposed")["total"] == initial.pop("receipts")["total"] == 0
    assert all(p["total"] == 151 and len(p["data"]) == 50 for p in initial.values())
    secret = tokens.create(ledger, "Choice reader", "read", user_id=token_owner)["token"]
    path = f"transaction-choices?id={tx['id']}&collection=payments&cursor=150"
    request(server, secret, "/companies/NORD/" + path)
    _req(server, "GET", "/web/api/" + path + "&co=ALL")
    before = ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes
    for name in set(transaction_choices.COLLECTIONS) - {"receipts"}:  # the world has no unread upload
        seen, cursor = [], ""
        while True:
            page = lists.transaction_choices(ledger, "NORD", dict(id=tx["id"], collection=name, cursor=cursor))[
                "pages"
            ][name]
            seen.extend(page["data"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == 151 and len({r["id"] for r in seen}) == 151
        if name in ("invoices", "selected"):
            assert all(r["left"] == "90071992547409.93" and r["selected"] for r in seen)
        elif name == "payments":
            assert all("|" not in r["id"] and not r["id"].startswith("private-payment") for r in seen)
    query = dict(id=tx["id"], collection="partners", q="firma aleasa 150")
    assert lists.transaction_choices(ledger, "NORD", query)["pages"]["partners"]["data"][0]["id"] == "NORD.choice-150"
    for name, needle, expected_count in [
        ("invoices", "SYN-150", 1),
        ("payments", "Beneficiar 150", 1),
        ("payments", "private-payment-150", 0),
    ]:
        found = lists.transaction_choices(ledger, "NORD", dict(id=tx["id"], collection=name, q=needle))["pages"][name]
        assert found["total"] == expected_count
    expected = lists.transaction_choices(ledger, "NORD", dict(id=tx["id"], collection="payments", cursor="150"))
    assert request(server, secret, "/companies/NORD/" + path) == (200, api_v1.public_state(expected, ledger))
    assert request(server, secret, "/companies/ALL/" + path)[0] == 404
    assert (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes) == before


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"collection": "absent"},
        {"collection": "payments", "partner": "x"},
        {"collection": "selected", "q": ""},
        {"limit": "201"},
        {"cursor": "50"},
        {"collection": "invoices", "cursor": "bad"},
        {"collection": "partners", "q": "x" * 201},
    ],
)
def test_transaction_choices_refuse_before_projection(ledger, world, monkeypatch, query):
    monkeypatch.setattr(api, "state", lambda *a, **kw: pytest.fail("invalid choice read projected history"))
    with pytest.raises(api_v1.Error) as error:
        lists.transaction_choices(ledger, "NORD", query if not query else {"id": "existing", **query})
    assert error.value.status == 400


@pytest.mark.parametrize(
    "kind,co",
    [
        ("invoice", "STUDIO9"),
        ("issued", "STUDIO9"),
        ("document", "STUDIO9"),
        ("mail", "NORD"),
        ("settlement", "NORD"),
        ("order", "STUDIO9"),
        ("transaction", "STUDIO9"),
    ],
)
def test_detail_reads_only_candidate_owner_and_preserves_public_sheet_context(
    token_owner, ledger, server, world, monkeypatch, kind, co
):
    if kind == "settlement":
        setup_settlement(ledger)
    if kind == "order":
        ledger.db.execute(
            "INSERT INTO emag_orders VALUES('synthetic',?,'ORDER-1','synthetic-task','attachment_retry')", (co,)
        )
        ledger.db.commit()
    full = api.state(ledger, co)
    row = full["transactions"]["rows"][0] if kind == "transaction" else full[details.KINDS[kind]][0]
    identity = row["key"] if kind == "mail" else f"{co}:{row['id']}" if kind == "order" else row["id"]
    query = {"kind": kind, "id": identity}
    expected = lists.detail(ledger, co, query)
    calls, read = [], api.state

    def counted(*args, **kwargs):
        calls.append(args[1])
        return read(*args, **kwargs)

    monkeypatch.setattr(api, "state", counted)
    secret = tokens.create(ledger, "Detail owner reader", "read", user_id=token_owner)["token"]
    tokens.verify(ledger, secret)
    path = "detail?kind=" + kind + "&id=" + quote(identity)
    assert _req(server, "GET", "/web/api/" + path + "&co=ALL")[0] == 200
    before, changes = ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes
    assert lists.detail(ledger, "ALL", query) == expected
    assert request(server, secret, "/companies/" + co + "/" + path) == (200, api_v1.public_state(expected, ledger))
    assert calls == [co, co, co]
    assert (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes) == (before, changes)
    assert request(server, secret, "/companies/ALL/" + path)[0] == 404


def test_settlement_lines_keep_full_money_controls_and_all_later_links(ledger, world, monkeypatch):
    identity = setup_settlement(ledger)
    full = api.state(ledger, "NORD")
    row = next(r for r in full["settlements"] if r["id"] == identity)
    lines = [row["lines"][0] | {"position": i + 1, "invoice": f"issued-{i}"} for i in range(251)]
    row["lines"] = lines
    row["invoice_count"] = 251
    row.update(gross="25100.00", fee="502.00", net="24598.00", can_confirm=True, failed_line=None)
    row["credit"] = {"id": "c" * 20, "date": "2026-10-12", "amount": row["net"], "currency": "RON"}
    monkeypatch.setattr(api, "state", lambda *a, **kw: full)
    seen, cursor = [], ""
    while True:
        result = lists.detail(ledger, "NORD", {"kind": "settlement", "id": identity, "limit": "200", "cursor": cursor})
        selected = result["state"]["settlements"][0]
        assert {
            k: selected[k] for k in ("net", "gross", "fee", "invoice_count", "can_confirm", "credit", "failed_line")
        } == {k: row[k] for k in ("net", "gross", "fee", "invoice_count", "can_confirm", "credit", "failed_line")}
        assert selected["lines"] == result["related"]["data"] and result["related"]["total"] == 251
        seen.extend(selected["lines"])
        cursor = result["related"]["next_cursor"]
        if cursor is None:
            break
    assert seen == lines
    issued = next(r for r in full["issued"] if r.get("local_id"))
    full["emag_tasks"] = [
        {
            "id": "task",
            "draft_id": issued["local_id"],
            "external_id": "ORDER-1",
            "order_id": None,
            "reason": "attachment_retry",
        }
    ]
    context = lists.detail(ledger, "NORD", {"kind": "order", "id": "NORD:task"})["state"]
    assert context["issued"] == [issued]


@pytest.mark.parametrize(
    "direction,source,kind", [("credit", "issued", "collection"), ("debit", "invoices", "payment")]
)
def test_transaction_detail_pages_complete_covered_facts_and_public_projection(
    token_owner, ledger, server, world, monkeypatch, direction, source, kind
):
    full = api.state(ledger, "NORD")
    tx = full["transactions"]["rows"][0]
    full[source] = [full[source][0] | {"id": str(90000 + i), "number": f"SYN-{i}", "left": "0.01"} for i in range(250)]
    full[source][249]["currency"] = None
    parts = [{"id": str(90000 + i), "amount": "90071992547409.93", "closed": False} for i in range(251)]
    payments = [{"kind": "op", "payee": "Sintetic", "ref": str(i), "details": "Plată pregătită"} for i in range(2)]
    link = dict(
        id="synthetic-link",
        tx=tx["id"],
        kind="documents",
        other=None,
        partner=full["partners"][0]["id"],
        parts=parts,
        payments=payments,
        reason="rest",
        difference="-0.01",
        currency="RON",
        note="",
        by="admin",
        at="2026-10-27",
    )
    tx.update(
        direction=direction,
        explanation={
            "kind": kind,
            "issued" if direction == "credit" else "invoices": [p["id"] for p in parts],
            "payments": payments,
            "link": link,
        },
    )
    expected_said = next(transaction_rows.items(full | {"transactions": {"rows": [tx]}}))[-1]["said"]
    monkeypatch.setattr(api, "state", lambda *a, **kw: full)
    secret = tokens.create(ledger, "Transaction detail", "read", user_id=token_owner)["token"]
    tokens.verify(ledger, secret)
    before = ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes
    seen, cursor = [], ""
    while True:
        query = {"kind": "transaction", "id": tx["id"], "limit": "200", "cursor": cursor}
        result = lists.detail(ledger, "NORD", query)
        assert {k: result["transaction"][k] for k in ("said", "parts_count", "payments_count")} == {
            "said": expected_said,
            "parts_count": 251,
            "payments_count": 2,
        }
        selected = result["state"]["transactions"]["rows"][0]
        assert selected["explanation"]["link"]["parts"] == parts[:50]
        assert result["state"]["payable"] == full["payable"] and not result["state"][source]
        assert result["related"]["total"] == 253
        path = f"/companies/NORD/detail?kind=transaction&id={tx['id']}&limit=200&cursor={cursor}"
        assert request(server, secret, path) == (200, api_v1.public_state(result, ledger))
        seen.extend(result["related"]["data"])
        cursor = result["related"]["next_cursor"]
        if cursor is None:
            break
    assert [r["id"] for r in seen[:251]] == [p["id"] for p in parts]
    assert seen[249]["number"] == "SYN-249" and seen[249]["left"] == "0.01"
    assert seen[249]["currency"] == tx["currency"]
    assert seen[250] == {
        "kind": "invoice",
        **parts[250],
        "number": parts[250]["id"],
        "left": "0.00",
        "currency": tx["currency"],
    }
    assert [r["payment"] for r in seen[251:]] == payments
    assert (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes) == before
    with pytest.raises(api_v1.Error) as error:
        lists.detail(ledger, "NORD", query | {"cursor": "254"})
    assert (error.value.status, error.value.code) == (400, "invalid_cursor")


@pytest.mark.parametrize("kind", ["transfer", "collection"])
def test_transaction_detail_keeps_transfer_counterpart_and_settlement_fee(ledger, world, kind):
    report_id = setup_settlement(ledger)
    full = api.state(ledger, "NORD")
    tx, other = full["transactions"]["rows"][:2]
    tx["explanation"] = (
        {"kind": "transfer", "other": other["id"]}
        if kind == "transfer"
        else {"kind": "collection", "issued": [full["issued"][0]["id"]], "settlement": report_id}
    )
    result = details.context(full, lists.metadata(full), "transaction", tx, 0, 50)
    assert result["related"] == {"data": [], "total": 0, "next_cursor": None}
    assert (
        result["transaction"]["said"]
        == next(transaction_rows.items(full | {"transactions": {"rows": [tx]}}))[-1]["said"]
    )
    if kind == "transfer":
        assert [t["id"] for t in result["state"]["transactions"]["rows"]] == [tx["id"], other["id"]]
    else:
        original = next(r for r in full["settlements"] if r["id"] == report_id)
        assert result["state"]["settlements"][0]["fee"] == original["fee"]


def test_mail_owner_preserves_registry_order_and_closed_or_unknown_ids_refuse_before_history(
    ledger, world, monkeypatch
):
    q = ledger.db.execute("SELECT sha,data FROM mail_questions WHERE answered_at IS NULL LIMIT 1").fetchone()
    data = json.loads(q["data"]) | {"reason": "many-firms", "companies": ["STUDIO9", "NORD"]}
    ledger.db.execute("UPDATE mail_questions SET data=? WHERE sha=?", (json.dumps(data), q["sha"]))
    assert details.owners(ledger, "mail", q["sha"], ["NORD", "STUDIO9"]) == ["NORD", "STUDIO9"]
    monkeypatch.setattr(api, "state", lambda *a, **kw: pytest.fail("unknown entity projected history"))
    ledger.db.execute("UPDATE mail_questions SET answered_at='2026-10-27' WHERE sha=?", (q["sha"],))
    with pytest.raises(api_v1.Error) as error:
        lists.detail(ledger, "ALL", {"kind": "mail", "id": q["sha"]})
    assert error.value.status == 404
    for kind in details.KINDS:
        with pytest.raises(api_v1.Error) as error:
            lists.detail(ledger, "ALL", {"kind": kind, "id": "absent"})
        assert error.value.status == 404


@pytest.mark.parametrize("co", ["NORD", "STUDIO9"])
def test_partner_pages_keep_identity_money_and_read_only_company_scope(
    token_owner, ledger, server, world, monkeypatch, co
):
    full = api.state(ledger, co)
    partner = full["partners"][0]
    calls, read = [], api.state

    def counted(*args, **kwargs):
        calls.append(args[1])
        return read(*args, **kwargs)

    monkeypatch.setattr(api, "state", counted)
    secret = tokens.create(ledger, "Partner reader", "read", user_id=token_owner)["token"]
    tokens.verify(ledger, secret)
    path = "partner-pages?id=" + quote(partner["id"])
    assert _req(server, "GET", "/web/api/" + path + "&co=" + co)[0] == 200
    before, changes = ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes
    expected = lists.partner_pages(ledger, "ALL", {"id": partner["id"]})
    assert set(expected["pages"]) == set(partner_rows.COLLECTIONS)
    assert expected["state"]["partners"] == [partner | {"held": [], "report_formats": []}]
    assert expected["state"]["payable"] == full["payable"] and not expected["state"]["invoices"]
    assert expected["state"]["companies"] == full["companies"]
    for name in ("invoices", "issued"):
        rows = sorted(
            (r for r in full[name] if r["partner"] == partner["id"]), key=lambda r: r["issue_date"] or "", reverse=True
        )
        assert expected["pages"][name] == {"data": rows, "total": len(rows), "next_cursor": None}
    assert request(server, secret, "/companies/" + co + "/" + path) == (200, api_v1.public_state(expected, ledger))
    assert calls == [co, co, co]  # ALL resolves directly to the owner, never projects other firms.
    assert (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes) == (before, changes)
    assert request(server, secret, "/companies/ALL/" + path)[0] == 404
    assert request(server, secret, "/companies/" + ("NORD" if co == "STUDIO9" else "STUDIO9") + "/" + path)[0] == 404


def test_every_partner_history_is_paged_with_complete_reference_text(ledger, world):
    full = api.state(ledger, "NORD")
    p = full["partners"][0]
    for source in ("invoices", "issued"):
        full[source] = [
            full[source][0]
            | {"id": f"{source}-{i}", "number": f"Ref-{i}", "partner": p["id"], "issue_date": "2026-10-27"}
            for i in range(151)
        ]
    p["held"] = [{"invoice": r["id"], "amount": "0.10", "currency": "RON"} for r in full["issued"]]
    p["report_formats"] = [
        {"signature": str(i), "source": "csv", "mapping": {"reference": "Ref"}, "courier": ""} for i in range(151)
    ]
    full["settlements"] = [
        {"id": str(i), "partner": p["id"], "report_key": str(i), "net": "0.10", "currency": "RON"} for i in range(151)
    ]
    tx = full["transactions"]["rows"][0] | {
        "explanation": {
            "kind": "collection",
            "issued": ["issued-150"],
            "settlement": None,
            "link": {"partner": p["id"], "parts": [{"closed": False}]},
        }
    }
    full["transactions"]["rows"] = [tx | {"id": str(i)} for i in range(151)]
    full["transactions"]["remembered"] = [
        {
            "id": str(i),
            "partner": p["id"],
            "kind": "documents",
            "reason": None,
            "note": "",
            "by": "name",
            "shown": "Synthetic",
            "who_by": "admin",
            "at": "2026-10-27",
        }
        for i in range(151)
    ]
    shell = lists.metadata(full)
    expected = {name: partner_rows.records(full, p, name) for name in partner_rows.COLLECTIONS}
    assert expected["transactions"][0]["said"]["text"] == "Încasarea parțială a facturii Ref-150"
    assert expected["held"][-1]["number"] == "Ref-150"
    assert expected["remembered"][0]["text"] == "De la " + p["name"]
    for name, rows in expected.items():
        seen, cursor = [], ""
        while True:
            result = partner_rows.project(full, shell, {"id": p["id"], "collection": name, "cursor": cursor})
            assert set(result["pages"]) == {name}
            page = result["pages"][name]
            assert page["total"] == 151 and len(page["data"]) <= 50
            seen.extend(page["data"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == rows
    whole = partner_rows.project(full, shell, {"collection": "remembered", "limit": "200"})
    assert whole["pages"]["remembered"]["data"] == expected["remembered"]
    assert whole["state"]["partners"] == []
    assert whole["state"]["record_counts"]["remembered"] == 151


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"collection": "held"},
        {"id": "NORD.1", "collection": "absent"},
        {"id": "NORD.1", "cursor": "1"},
        {"id": "NORD.1", "collection": "held", "cursor": "-1"},
        {"id": "NORD.1", "limit": "201"},
        {"id": "NORD.1", "q": "x"},
    ],
)
def test_partner_history_refuses_before_projection(ledger, monkeypatch, query):
    monkeypatch.setattr(api, "state", lambda *a, **kw: pytest.fail("invalid request projected history"))
    with pytest.raises(api_v1.Error) as error:
        lists.partner_pages(ledger, "NORD", query)
    assert error.value.status == 400


@pytest.mark.parametrize("kind", ["invoice", "issued", "document", "mail"])
def test_document_details_keep_sheet_context_and_scope(token_owner, ledger, server, world, kind):
    full = api.state(ledger, "NORD")
    row = full[details.KINDS[kind]][0]
    identity = row["key" if kind == "mail" else "id"]
    query = {"kind": kind, "id": identity}
    expected = lists.detail(ledger, "NORD", query)
    small = expected["state"]
    assert row == details.find(small, kind, identity)
    for key in ("payable", "receivable", "cod_receivable", "companies", "invoice_counts"):
        assert small[key] == lists.metadata(full)[key]
    assert not small["transactions"]["rows"] and "payments" not in small
    for key in ("expenses", "invoices", "documents", "partners", "refused"):
        for selected in small[key]:
            original = next(r for r in full[key] if r["id"] == selected["id"])
            assert selected == (original | {"held": []} if key == "partners" else original)
    secret = tokens.create(ledger, "Detail reader", "read", user_id=token_owner)["token"]
    tokens.verify(ledger, secret)
    path = "detail?kind=" + kind + "&id=" + quote(identity)
    assert _req(server, "GET", "/web/api/" + path + "&co=NORD")[0] == 200
    before, changes = ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes
    assert request(server, secret, "/companies/NORD/" + path) == (200, api_v1.public_state(expected, ledger))
    assert lists.detail(ledger, "ALL", query) == expected
    assert (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes) == (before, changes)
    assert request(server, secret, "/companies/ALL/" + path)[0] == 404
    if kind != "mail":
        assert request(server, secret, "/companies/STUDIO9/" + path)[0] == 404


@pytest.mark.parametrize("kind", ["invoice", "issued"])
def test_document_relations_and_local_alias_stay_reachable(ledger, world, monkeypatch, kind):
    full = api.state(ledger, "NORD")
    row = full[details.KINDS[kind]][0]
    if kind == "invoice":
        rows = [{"id": str(i), "number": "Credit " + str(i), "amount": "0.01"} for i in range(151)]
        row["credits"] = rows
    else:
        row["local_id"] = "local-original"
        rows = [row | {"id": "reversal-" + str(i), "local_id": "local-" + str(i)} for i in range(151)]
        row["reversed_by"] = [r["local_id"] for r in rows]
        full["issued"].extend(rows)
    monkeypatch.setattr(api, "state", lambda *a, **kw: full)
    query = {"kind": kind, "id": row.get("local_id", row["id"]), "limit": "50"}
    seen, cursor = [], ""
    while True:
        result = lists.detail(ledger, "NORD", query | {"cursor": cursor})
        related = result["related"]
        assert related["total"] == 151 and len(related["data"]) <= 50
        seen.extend(related["data"])
        selected = details.find(result["state"], kind, query["id"])
        assert (selected["credits"] if kind == "invoice" else selected["reversed_by"]) == (
            related["data"] if kind == "invoice" else [r["local_id"] for r in related["data"]]
        )
        cursor = related["next_cursor"]
        if cursor is None:
            break
    assert seen == rows


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"kind": "absent", "id": "1"},
        {"kind": "invoice", "id": "1", "limit": "201"},
        {"kind": "document", "id": "1", "cursor": "1"},
        {"kind": "invoice", "id": "1", "cursor": "-1"},
        {"kind": "invoice", "id": "1", "q": "x"},
    ],
)
def test_document_detail_refuses_before_projection(ledger, world, monkeypatch, query):
    monkeypatch.setattr(api, "state", lambda *a, **kw: pytest.fail("invalid request projected history"))
    with pytest.raises(api_v1.Error) as error:
        lists.detail(ledger, "NORD", query)
    assert error.value.status == 400


def test_bearer_only_and_routes(token_owner, ledger, server, world):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    assert request(server, "")[0] == 401
    assert request(server, "tok")[0] == 401
    code, headers, _ = _req(server, "GET", "/api/v1/companies")
    assert code == 401 and headers["www-authenticate"] == "Bearer"
    assert request(server, secret, Authorization="bEaReR " + secret)[1]["data"][0]["key"] == "NORD"
    assert request(server, secret, "/companies/NORD/state")[1] == api_v1.public_state(api.state(ledger, "NORD"), ledger)
    assert request(server, secret, "/companies/ALL/state")[0] == 404
    assert request(server, secret, "/absent")[0] == 404
    code, headers, _ = _req(server, "DELETE", "/api/v1/companies", None, {"Authorization": "Bearer " + secret})
    assert code == 405 and headers["allow"] == "GET"
    assert request(server, secret, "/companies/NORD/actions/check", "POST", {})[0] == 403


# Local actions still enforce their normal preconditions in shadow.
SHADOW = {
    "settlement-confirm": 409,
    "check": 200,
    "upload-link-revoke": 200,
    "upload-link-create": 400,
    "order-discard": 409,
}


def test_actions_validation_shadow_and_company(token_owner, ledger, server, home, world, quiet):
    secret = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    path = "/companies/NORD/actions/mark-paid"
    for body in ([], {"co": "STUDIO9", "id": "102"}, {"id": True}, {"id": {}}, b"{bad"):
        assert request(server, secret, path, "POST", body)[0] == 400
    assert request(server, secret, path, "POST", b"{}", **{"Content-Type": "text/plain"})[0] == 415
    assert request(server, secret, path, "POST", {}, Origin="https://evil.example")[0] == 403
    assert request(server, secret, path, "POST", {"id": "102", "pad": "x" * 4100})[0] == 413
    assert request(server, secret, path, "POST", {"id": "201"})[0] == 409
    assert ledger.efactura("201")["status"] == "open"
    assert request(server, secret, path, "POST", {"id": "102", "method": "bank"})[0] == 200
    (home / "state" / "LIVE").unlink()
    for action in actions.API_ACTIONS:
        body = {"who": "none"} if action == "mail-answer" else {}
        code, _ = request(server, secret, "/companies/NORD/actions/" + action, "POST", body)
        assert code == (
            400
            if action.startswith("company-")
            else 403
            if action.startswith("integration-")
            else SHADOW.get(action, 409)
            if actions.ACTIONS[action].effect == "local"
            else 403
        ), action


def test_pagination_validation(token_owner, ledger, server):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    code, first = request(server, secret, "/companies?limit=1")
    assert code == 200 and first["next_cursor"] == "NORD"
    assert request(server, secret, "/companies?limit=1&cursor=NORD")[1]["data"][0]["key"] == "STUDIO9"
    for query in ("limit=0", "limit=201", "limit=x", "limit=1&limit=2"):
        assert request(server, secret, "/companies?" + query)[0] == 400


def test_internal_errors_do_not_leak(token_owner, ledger, server, monkeypatch):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]

    def broken(*args):
        raise RuntimeError("secret /private/path")

    monkeypatch.setattr(api, "state", broken)
    code, result = request(server, secret, "/companies/NORD/state")
    assert code == 500 and "secret" not in str(result)


def test_action_actor_is_logged_and_recorded(token_owner, ledger, server, world, quiet, home):
    made = tokens.create(ledger, "Accounting script", "write", user_id=token_owner)
    assert (
        request(server, made["token"], "/companies/NORD/actions/mark-paid", "POST", {"id": "102", "method": "bank"})[0]
        == 200
    )
    note = ledger.efactura("102")["paid_note"]
    logs = "".join(p.read_text() for p in (home / "logs").glob("*.log"))
    assert made["id"] in note and "Accounting script" in note
    assert made["id"] in logs and "Accounting script" in logs
    assert made["token"] not in logs + note
    for action, how in (("withdraw", {"method": "bank"}), ("reopen", {})):
        assert (
            request(server, made["token"], "/companies/NORD/actions/" + action, "POST", {"id": "105", **how})[0] == 200
        )
        assert made["id"] in ledger.efactura("105")["paid_note"]


def test_audit_identity_redacts_a_spaced_iban():
    name = "RO00 TEST 0000 0000 0000 0000"
    assert audit.identity({"name": name}) == 'token {"id": "", "name": "[redacted]"}'


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_action_utf8_byte_limit(token_owner, ledger, server, world, quiet, monkeypatch, transport):
    seen = []

    def act(action, co, body, **kwargs):
        seen.append(body)
        return {"ok": True, "message": ""}

    monkeypatch.setattr(actions, "act", act)
    secret = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    # Exactly 4 KB with compact, unescaped UTF-8; escaping exceeds the budget.
    body = {"id": "ă" * 2043 + "x"}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(raw) == 4096
    if transport == "http":
        assert request(server, secret, "/companies/NORD/actions/mark-paid", "POST", raw)[0] == 200
        assert request(server, secret, "/companies/NORD/actions/mark-paid", "POST", raw + b" ")[0] == 413
    else:
        assert api_v1.run_action(ledger, "NORD", "mark-paid", body)["ok"]
        with pytest.raises(api_v1.Error) as exc:
            api_v1.run_action(ledger, "NORD", "mark-paid", {"id": body["id"] + "ă"})
        assert exc.value.code == "body_too_large"
    assert len(seen) == 1 and seen[0]["id"] == body["id"]


@pytest.mark.parametrize(
    "action,code", [("confirm-supplier-iban", 400), ("revoke-supplier-iban", 400), ("integration-add", 403)]
)
def test_supplier_trust_and_integrations_are_never_delegated_to_bearers(
    token_owner, ledger, server, home, monkeypatch, action, code
):
    """Live, with a valid body: a write token still can't confirm a supplier's account or point the AI elsewhere."""
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    (home / "state" / "LIVE").touch()
    secret = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    body = (
        {"id": "3001", "iban": SUPPLIER_IBAN, "confirmed": "yes"}
        if code == 400
        else dict(
            kind="ai", name="Local", f_provider="openai", f_base_url="http://127.0.0.1:11434/v1", f_model="synthetic"
        )
    )
    assert request(server, secret, "/companies/NORD/actions/" + action, "POST", body)[0] == code
    assert integrations.enabled(ledger, "ai") == []


def test_public_payment_candidate_resolves_only_in_its_company(token_owner, ledger, world, server):
    record = ledger.ambiguous("STUDIO9")[0]
    tx = json.loads(record["tx"])
    item = PaymentItem(
        "STUDIO9",
        "efactura",
        "Synthetic supplier",
        tx["counterparty_iban"],
        "",
        "",
        Decimal(tx["amount"]),
        "Synthetic payment",
        date.fromisoformat(tx["date"]),
        "http",
        "",
        "http",
    )
    ledger.add_payment(item)
    candidates = [*json.loads(record["candidates"]), ["payment", item.item_id]]
    ledger.db.execute("UPDATE ambiguous_tx SET candidates=? WHERE tx_key=?", (json.dumps(candidates), record["tx_key"]))
    ledger.db.commit()
    public = api_v1.public_state(api.state(ledger, "STUDIO9"), ledger)
    candidate = next(
        c for a in public["ambiguous"] for c in a["candidates"] if c["id"] == api_resources.payment_id(item.item_id)
    )
    body = {"tx": record["tx_key"], "target": candidate["id"]}
    secret = tokens.create(ledger, "Writer", "write", user_id=token_owner)["token"]
    # A public payment id from another company cannot resolve this debit.
    assert request(server, secret, "/companies/NORD/actions/resolve-debit", "POST", body)[0] == 409
    assert request(server, secret, "/companies/STUDIO9/actions/resolve-debit", "POST", body)[0] == 200
    assert ledger.payment(item.item_id)["status"] == "paid"


def test_bounded_state_pages_keep_public_payment_ids_and_chronological_cursors(
    token_owner, ledger, server, world, monkeypatch
):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    monkeypatch.setattr(api, "clock", lambda: datetime(2026, 10, 25))
    ledger.db.execute(
        "INSERT INTO payments(item_id,company,kind,month,issue_date,amount,status,created_at) "
        "VALUES(?,'NORD','op','2026-10','2026-10-01','12.00','pending','2026-10-01')",
        ("NORD|op|RO00TEST0000000000000001|12.00|2026-10-01|SYN",),
    )
    ledger.db.commit()
    summary = request(server, secret, "/companies/NORD/state-summary")[1]
    assert summary["payable"] == api.state(ledger, "NORD")["payable"]
    for endpoint in ("state-summary", "state-list?collection=upcoming"):
        assert request(server, secret, "/companies/ALL/" + endpoint)[0] == 404
    path = "/companies/NORD/state-list?collection=upcoming&limit=1"
    rows, cursor = [], ""
    while True:
        code, result = request(server, secret, path + "&cursor=" + quote(cursor, safe=""))
        assert code == 200
        rows.extend(result["data"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert any(r["kind"] == "payment" for r in rows)
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    for r in rows:
        assert "|" not in r["id"] and "|" not in r["cursor_id"]
        assert request(server, secret, path + "&id=" + quote(r["cursor_id"], safe=""))[1]["data"] == [r]


def test_task_pages_are_read_only_company_scoped_and_public(token_owner, ledger, server, world, home):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    (home / "state" / "LIVE").unlink()
    before = ledger.db.total_changes
    code, result = request(server, secret, "/companies/NORD/tasks?limit=2")
    assert code == 200 and 0 < len(result["data"]) <= 2
    assert all(r["company"] == "NORD" for r in result["data"])
    assert len(result["highlights"]) <= 3
    text = json.dumps(result)
    for payment in ledger.payments():
        assert payment["item_id"] not in text
    assert '"item_id"' not in text
    assert request(server, secret, "/companies/ALL/tasks")[0] == 404
    assert request(server, secret, "/companies/NORD/tasks?limit=0")[0] == 400
    assert _req(server, "GET", "/web/api/tasks?co=ALL&limit=1")[0] == 200
    assert api.state(ledger, "ALL")["task_summary"] == lists.summary(ledger, "ALL")["task_summary"]
    assert _req(server, "GET", "/c/wrong/api/tasks?co=NORD")[0] == 404
    assert ledger.db.total_changes == before


def test_payment_candidate_cursors_and_lookup_use_public_ids(token_owner, ledger, server, world):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    record = ledger.ambiguous("STUDIO9")[0]
    tx = json.loads(record["tx"])
    stored = json.loads(record["candidates"])
    for i in range(3):
        item = PaymentItem(
            "STUDIO9",
            "op",
            "Synthetic supplier",
            tx["counterparty_iban"],
            "",
            "",
            Decimal(tx["amount"]),
            f"Candidate {i}",
            date.fromisoformat(tx["date"]),
            f"ref-{i}",
            "",
            "http",
        )
        ledger.add_payment(item)
        stored.append(["payment", item.item_id])
    ledger.db.execute("UPDATE ambiguous_tx SET candidates=? WHERE tx_key=?", (json.dumps(stored), record["tx_key"]))
    ledger.db.commit()
    original = api.state(ledger, "STUDIO9")["ambiguous"][0]["candidates"]
    expected = api_v1.public_state(original, ledger)
    query = "/state-list?collection=candidates&tx=" + quote(record["tx_key"], safe="") + "&limit=1"
    assert request(server, secret, "/companies/NORD" + query)[0] == 404
    rows, cursor = [], ""
    while True:
        code, result = request(server, secret, "/companies/STUDIO9" + query + "&cursor=" + quote(cursor, safe=""))
        assert code == 200 and result["total"] == len(expected)
        rows.extend(result["data"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert "|" not in cursor
    assert [{k: v for k, v in r.items() if k != "cursor_id"} for r in rows] == expected
    for r in rows:
        assert request(server, secret, "/companies/STUDIO9" + query + "&id=" + quote(r["id"], safe=""))[1]["data"] == [
            r
        ]
    private_path = "/web/api/state-list?co=STUDIO9&collection=candidates&limit=1&tx=" + quote(record["tx_key"], safe="")
    body = json.loads(_req(server, "GET", private_path)[2])
    assert "|" not in body["next_cursor"]


def test_search_is_bounded_company_scoped_and_read_only_in_shadow(token_owner, ledger, server, world, home):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    (home / "state" / "LIVE").unlink()
    code, result = request(server, secret, "/companies/NORD/search?q=alfa")
    assert code == 200 and result["issued"] and result["partners"]
    assert all(r["company"] == "NORD" for rows in result.values() for r in rows)
    assert all(len(rows) <= 6 for rows in result.values())
    assert request(server, secret, "/companies/ALL/search?q=omega")[0] == 404
    assert request(server, secret, "/companies/NORD/search?q=a")[1] == {k: [] for k in result}
    code, _, raw = _req(server, "GET", "/web/api/search?co=ALL&q=omega")
    assert code == 200 and json.loads(raw)["issued"][0]["company"] == "STUDIO9"
    assert _req(server, "GET", "/c/wrong/api/search?co=NORD&q=alfa")[0] == 404


def test_browse_pages_keep_public_projection_and_company_scope_in_shadow(token_owner, ledger, server, world, home):
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    (home / "state" / "LIVE").unlink()
    tokens.verify(ledger, secret)  # Prime authentication metadata, including the app's legacy user import.
    assert _req(server, "GET", "/web/api/search?co=NORD&q=a")[0] == 200
    before = ledger.db.execute("PRAGMA data_version").fetchone()[0]
    for screen in ("vanzari", "cheltuieli", "bonuri", "parteneri"):
        path = "/companies/NORD/browse?screen=" + screen + "&limit=1"
        rows, cursor, total = [], "", None
        while True:
            code, result = request(server, secret, path + "&cursor=" + quote(cursor, safe=""))
            assert code == 200 and len(result["data"]) <= 1
            assert all(r["company"] == "NORD" for r in result["data"])
            assert '"item_id"' not in json.dumps(result)
            total = result["total"] if total is None else total
            assert total == result["total"]
            rows.extend(result["data"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
        assert len(rows) == total
    assert request(server, secret, "/companies/ALL/browse?screen=vanzari")[0] == 404
    assert _req(server, "GET", "/web/api/browse?co=ALL&screen=vanzari&limit=1")[0] == 200
    assert _req(server, "GET", "/c/wrong/api/browse?co=NORD&screen=vanzari")[0] == 404
    assert ledger.db.execute("PRAGMA data_version").fetchone()[0] == before


def _report_expected(full, report):
    bank = report["bank"]
    return {
        "issued": [("issued", r) for r in full["issued"] if r["id"] in report["issued"]],
        "bank": [("card", r) for r in bank["card"] if r["doc"]] if bank else [],  # the open ones are tasks, listed once
        "advances": [("advance", r) for r in bank["advances"]] if bank else [],
        "docs": [("document", r) for r in report["docs"]],
        "not_mailed": [("not_mailed", r) for r in report["not_mailed"]],
        "tasks": (
            ([("card", r) for r in bank["card"] if not r["doc"]] if bank else [])
            + (
                [("bank", bank | {"card": [], "advances": []})]
                if bank and (bank["balanced"] is False or bank["error"])
                else []
            )
            + [("mail", r) for r in full["to_check"] if r["key"] in report["to_check"]]
            + [
                ("ambiguous", r | {"candidate_count": len(r["candidates"])})
                for r in full["ambiguous"]
                if r["month"] == report["month"]
            ]
        ),
    }


@pytest.mark.parametrize("section", ["received", "issued", "tasks", "bank", "advances", "docs", "not_mailed"])
def test_report_pages_keep_monthly_sources_order_identity_and_public_fields(
    token_owner, ledger, server, world, section
):
    if section == "received":
        pair = next(e for e in api.state(ledger, "NORD")["expenses"] if e["invoice"] and e["document"])
        ledger.db.execute("UPDATE documents SET month='2026-09',doc_date='2026-09-30' WHERE sha=?", (pair["document"],))
        ledger.db.commit()
    co = "STUDIO9" if section == "tasks" else "NORD"
    full = api.state(ledger, co)
    summary = lists.summary(ledger, co)
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    tokens.verify(ledger, secret)
    assert _req(server, "GET", "/web/api/search?co=NORD&q=a")[0] == 200
    before = ledger.db.execute("PRAGMA data_version").fetchone()[0]
    for report in full["reports"][:2]:  # both the current month without a bank and a completed month
        expected = _report_expected(full, report)
        query = {"screen": "report_" + section, "month": report["month"], "limit": "2"}
        rows, cursor = [], ""
        while True:
            result = lists.browse(ledger, co, query | {"cursor": cursor})
            assert result["months"] == [] and len(result["data"]) <= 2
            rows.extend(result["data"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
            assert "|" not in cursor
        assert len(rows) == result["total"] and all(r["company"] == co for r in rows)
        if section == "received":
            ids = [
                e["id"]
                for e in full["expenses"]
                if e["invoice"] in report["received"] or e["document"] in report["documents"]
            ]
            assert [r["data"]["id"] for r in rows] == ids
            counts = next(r for r in summary["reports"] if r["month"] == report["month"])["counts"]
            assert len(rows) == counts["purchases"]
            for row in rows:
                for field, source, allowed in (
                    ("invoice", "invoices", "received"),
                    ("document", "documents", "documents"),
                ):
                    item = row["data"][field]
                    if item:
                        assert item["id"] in report[allowed] and item == next(
                            r for r in full[source] if r["id"] == item["id"]
                        )
        else:
            assert [(r["kind"], r["data"]) for r in rows] == expected[section]
        if section == "tasks":  # the month's "de rezolvat" counts exactly these rows
            counts = next(r for r in summary["reports"] if r["month"] == report["month"])["counts"]
            assert counts["open"] == len(rows)
        path = "/companies/" + co + "/browse?screen=report_" + section + "&month=" + report["month"] + "&limit=200"
        code, public = request(server, secret, path)
        assert code == 200 and public == api_v1.public_state(lists.browse(ledger, co, query | {"limit": "200"}), ledger)
        assert '"item_id"' not in json.dumps(public)
    assert ledger.db.execute("PRAGMA data_version").fetchone()[0] == before
    assert request(server, secret, "/companies/" + co + "/browse?screen=report_received&month=2020-01")[0] == 404


def test_report_tasks_keep_all_candidate_choices_reachable_after_bounding(ledger, world):
    full = api.state(ledger, "STUDIO9")
    ambiguity = full["ambiguous"][0]
    ambiguity["candidates"] = [ambiguity["candidates"][0] | {"id": f"synthetic-{i}"} for i in range(151)]
    collector = browsing.Collector({"screen": "report_tasks", "month": ambiguity["month"], "limit": "200"})
    collector.add(full)
    row = next(r for r in collector.result()["data"] if r["kind"] == "ambiguous")
    assert row["id"].endswith(":" + ambiguity["tx"])
    assert row["data"]["candidate_count"] == 151 and len(row["data"]["candidates"]) == 50


@pytest.mark.parametrize("co", ["NORD", "STUDIO9"])
def test_initial_report_pages_share_one_projection_and_keep_browse_cursors(
    token_owner, ledger, server, world, monkeypatch, co
):
    month = "2026-10"
    query = {"month": month, "limit": "2"}
    expected = {
        "data": [
            {"screen": screen, "page": lists.browse(ledger, co, query | {"screen": screen})}
            for screen in browsing.REPORT_SCREENS
        ],
        "total": 7,
        "next_cursor": None,
    }
    read, calls = api.state, []

    def counted(*args, **kwargs):
        calls.append(args[1])
        return read(*args, **kwargs)

    monkeypatch.setattr(api, "state", counted)
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    tokens.verify(ledger, secret)
    assert _req(server, "GET", "/web/api/search?co=NORD&q=a")[0] == 200
    before, changes = ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes
    assert lists.report_pages(ledger, co, query) == expected
    code, _, private = _req(server, "GET", f"/web/api/report-pages?co={co}&month={month}&limit=2")
    assert code == 200 and json.loads(private) == expected
    code, public = request(server, secret, f"/companies/{co}/report-pages?month={month}&limit=2")
    assert code == 200 and public == api_v1.public_state(expected, ledger)
    assert calls == [co, co, co]
    assert (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes) == (before, changes)
    for entry in expected["data"]:
        result = entry["page"]
        if result["next_cursor"]:
            following = lists.browse(ledger, co, query | {"screen": entry["screen"], "cursor": result["next_cursor"]})
            assert not ({r["id"] for r in result["data"]} & {r["id"] for r in following["data"]})
    assert request(server, secret, f"/companies/ALL/report-pages?month={month}")[0] == 404


@pytest.mark.parametrize(
    "query,code",
    [
        ({}, "invalid_month"),
        ({"month": "2026-99"}, "invalid_month"),
        ({"month": "2026-10", "limit": "201"}, "invalid_limit"),
        ({"month": "2026-10", "cursor": ":2"}, "invalid_query"),
    ],
)
def test_initial_report_pages_refuse_before_projection(ledger, monkeypatch, query, code):
    monkeypatch.setattr(api, "state", lambda *a, **kw: pytest.fail("Invalid report request projected state"))
    with pytest.raises(api_v1.Error) as error:
        lists.report_pages(ledger, "NORD", query)
    assert (error.value.status, error.value.code) == (400, code)


@pytest.mark.parametrize(
    "mode,extra",
    [
        ("", {}),
        ("all", {}),
        ("in", {}),
        ("out", {"month": "2026-09"}),
        ("all", {"from": "2026-10-01", "to": "2026-10-20"}),
    ],
)
def test_transaction_browser_keeps_order_full_money_filters_and_public_ids(
    token_owner, ledger, server, world, mode, extra
):
    full = api.state(ledger, "NORD")
    source = full["transactions"]["rows"]
    expected = [
        t
        for t in source
        if (
            not mode
            and not t["explanation"]
            or mode == "all"
            or mode == "in"
            and t["direction"] == "credit"
            or mode == "out"
            and t["direction"] == "debit"
        )
        and (not extra.get("month") or t["date"].startswith(extra["month"]))
        and (not extra.get("from") or t["date"] >= extra["from"])
        and (not extra.get("to") or t["date"] <= extra["to"])
    ]
    query = {"screen": "tranzactii", "f": mode, "limit": "2"} | extra
    rows, cursor = [], ""
    while True:
        result = lists.browse(ledger, "NORD", query | {"cursor": cursor})
        rows.extend(result["data"])
        assert result["total"] == len(expected) and result["months"] == sorted(
            {t["date"][:7] for t in source}, reverse=True
        )
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert "|" not in cursor
    assert [r["id"] for r in rows] == [t["id"] for t in expected]
    assert [
        (r["transaction"]["amount"], r["transaction"]["orig"], r["transaction"]["explanation"] is None) for r in rows
    ] == [(t["amount"], t["orig"], t["explanation"] is None) for t in expected]
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    tokens.verify(ledger, secret)
    assert _req(server, "GET", "/web/api/search?co=NORD&q=a")[0] == 200
    before, changes = ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes
    suffix = "&".join(f"{k}={v}" for k, v in query.items())
    code, _, private = _req(server, "GET", "/web/api/browse?co=NORD&" + suffix)
    assert code == 200 and json.loads(private) == lists.browse(ledger, "NORD", query)
    code, public = request(server, secret, "/companies/NORD/browse?" + suffix)
    assert code == 200 and public == api_v1.public_state(lists.browse(ledger, "NORD", query), ledger)
    assert '"item_id"' not in json.dumps(public)
    assert (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes) == (before, changes)


@pytest.mark.parametrize(
    "explanation,proposal,text,target",
    [
        ({"kind": "payment", "invoices": ["r"], "payments": []}, None, "Plata facturii R42", {"inv": "r"}),
        (
            {"kind": "payment", "invoices": ["r"], "payments": [], "link": {"parts": [{"closed": False}]}},
            None,
            "Plata parțială a facturii R42",
            {"inv": "r"},
        ),
        (
            {"kind": "payment", "invoices": [], "payments": [{"details": "Servicii", "ref": "X"}]},
            None,
            "Plată din fișierul de plăți, servicii",
            {"tab": "plati"},
        ),
        ({"kind": "collection", "issued": ["i"], "settlement": None}, None, "Încasarea facturii E42", {"iss": "i"}),
        (
            {"kind": "collection", "issued": ["i"] * 20, "settlement": "s"},
            None,
            "Decontare Procesator Sintetic, 20 de facturi",
            {"set": "s"},
        ),
        ({"kind": "receipt", "document": "d", "invoices": []}, None, "Bon de la Magazin Sintetic", {"doc": "d"}),
        ({"kind": "label", "label": "interest"}, None, "Fără document, dobândă", None),
        ({"kind": "advance", "who": "Asociat Sintetic"}, None, "Avans, Asociat Sintetic", None),
        ({"kind": "transfer"}, None, "Transfer între conturi", None),
        (None, {"kind": "invoices", "invoices": ["r"] * 101}, "Se potrivește cu 101 facturi", {"tab": "sarcini"}),
        (None, {"kind": "settlement", "settlement": "s"}, "Decontare de verificat", {"set": "s"}),
        (
            None,
            {"kind": "remembered", "as": {"kind": "documents", "partner": "p"}},
            "Regulă salvată: de la cui 9000",
            None,
        ),
        (None, {"kind": "receipt", "document": "absent"}, "Poate fi un bon", None),
    ],
)
def test_transaction_browser_descriptions_use_complete_identities_before_bounding(explanation, proposal, text, target):
    tx = {
        "id": "tx",
        "direction": "debit",
        "date": "2026-10-20",
        "amount": "-1234.50",
        "counterparty": "",
        "details": "",
        "orig": None,
        "explanation": explanation,
        "proposal": proposal,
    }
    part = {
        "co": "NORD",
        "transactions": {"rows": [tx]},
        "invoices": [{"id": "r", "number": "R42"}],
        "issued": [{"id": "i", "number": "E42"}],
        "documents": [{"id": "d", "vendor": "Magazin Sintetic"}],
        "settlements": [{"id": "s", "payer": {"name": "Procesator Sintetic"}}],
        "partners": [{"id": "p", "name": None, "cui": "9000"}],
    }
    _, terms, _, _, row = next(transaction_rows.items(part))
    assert row["said"] == {
        "text": text,
        **({"to": target} if target else {}),
        **({"proposal": True} if proposal else {}),
    }
    assert all(t in terms for t in (text, "-1234.50", "-1234,50", "-1.234,50"))
    collector = browsing.Collector({"screen": "tranzactii", "f": "all"})
    collector.add(part)
    assert collector.result()["data"][0]["said"] == row["said"]


@pytest.mark.parametrize("collection", ["upcoming", "held_money"])
def test_dashboard_panel_http_scoping_public_projection_and_shadow_readonly(
    token_owner, ledger, server, world, home, collection
):
    (home / "state" / "LIVE").unlink(missing_ok=True)
    secret = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    tokens.verify(ledger, secret)
    assert _req(server, "GET", "/web/api/search?co=NORD&q=a")[0] == 200
    query = {"collection": collection, "limit": "2"}
    before, changes = ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes
    for co in ("NORD", "ALL"):
        expected = lists.dashboard_page(ledger, co, query)
        assert api.state(ledger, co)["dashboard_pages"][collection] == lists.dashboard_page(
            ledger, co, {"collection": collection}
        )
        assert lists.summary(ledger, co)["dashboard_pages"][collection] == lists.dashboard_page(
            ledger, co, {"collection": collection}
        )
        code, _, body = _req(server, "GET", f"/web/api/dashboard-list?co={co}&collection={collection}&limit=2")
        assert code == 200 and json.loads(body) == expected
    code, public = request(server, secret, f"/companies/NORD/dashboard-list?collection={collection}&limit=2")
    assert code == 200 and public == api_v1.public_state(lists.dashboard_page(ledger, "NORD", query), ledger)
    assert '"item_id"' not in json.dumps(public)
    assert (ledger.db.execute("PRAGMA data_version").fetchone()[0], ledger.db.total_changes) == (before, changes)
    assert request(server, secret, f"/companies/ALL/dashboard-list?collection={collection}")[0] == 404
    assert _req(server, "GET", f"/c/wrong/api/dashboard-list?co=NORD&collection={collection}")[0] == 404


def test_owner_role_limits_http_and_mcp_tokens_immediately(token_owner, ledger, server, world, quiet, monkeypatch):
    made = tokens.create(ledger, "Writer", "write", user_id=token_owner)
    monkeypatch.setattr(actions, "act", lambda *a, **kw: {"ok": True})
    path = "/companies/NORD/actions/mark-paid"
    assert request(server, made["token"], path, "POST", {"id": "102", "method": "bank"})[0] == 200
    ledger.db.execute("UPDATE users SET role='readonly' WHERE id=?", (token_owner,))
    ledger.db.commit()
    assert request(server, made["token"])[0] == 200
    assert request(server, made["token"], path, "POST", {"id": "102", "user_id": "u_admin"})[0] == 403
    route = next(r for r in api_v1.ROUTES if r.resource == "action")
    with pytest.raises(api_v1.Error) as refused:
        api_v1.execute_as(
            ledger,
            tokens.verify(ledger, made["token"]),
            route,
            {"co": "NORD", "action": "mark-paid"},
            {},
            {"id": "102"},
        )
    assert refused.value.status == 403
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    tools = request(server, made["token"], "/mcp", "POST", message, **headers)[1]["result"]["tools"]
    assert not any(t["name"].startswith("action_") for t in tools)
    ledger.db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id=?", (token_owner,))
    ledger.db.commit()
    assert request(server, made["token"])[0] == 401
    assert request(server, made["token"], "/mcp", "POST", message, **headers)[0] == 401
    assert tokens.list_tokens(ledger)[0]["revoked_at"] is None
    ledger.db.execute("UPDATE users SET disabled_at=NULL WHERE id=?", (token_owner,))
    ledger.db.commit()
    assert request(server, made["token"])[0] == 200
    assert request(server, made["token"], path, "POST", {"id": "102"})[0] == 403


@pytest.mark.parametrize("surface", ["http", "mcp"])
@pytest.mark.parametrize("change,code", [("readonly", 403), ("disabled", 401), ("revoked", 401), ("owner", 401)])
def test_generic_bearer_writer_rechecks_real_owner_and_authority_after_admission(
    token_owner, ledger, server, world, monkeypatch, surface, change, code
):
    made = tokens.create(ledger, "Writer", "write", user_id=token_owner)
    before = dict(ledger.db.execute("SELECT * FROM efactura WHERE company='NORD' AND status='open' LIMIT 1").fetchone())
    original = actions.invoice_action

    def changed(current, co, body, ctx):
        with closing(Ledger(ledger.path)) as other, other.tx() as db:
            if change == "readonly":
                db.execute("UPDATE users SET role='readonly' WHERE id=?", (token_owner,))
            elif change == "disabled":
                db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id=?", (token_owner,))
            elif change == "revoked":
                tokens.revoke(other, made["id"])
            else:
                db.execute("UPDATE api_tokens SET user_id='u_admin' WHERE id=?", (made["id"],))
        return original(current, co, body, ctx)

    monkeypatch.setitem(actions.ACTIONS, "mark-paid", replace(actions.ACTIONS["mark-paid"], handler=changed))
    if surface == "http":
        assert (
            request(
                server,
                made["token"],
                "/companies/NORD/actions/mark-paid",
                "POST",
                {"id": before["message_id"], "method": "bank"},
            )[0]
            == code
        )
    else:
        status, reply = request(
            server,
            made["token"],
            "/mcp",
            "POST",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "action_mark_paid",
                    "arguments": {"co": "NORD", "body": {"id": before["message_id"], "method": "bank"}},
                },
            },
            Accept="application/json, text/event-stream",
            **{"MCP-Protocol-Version": "2025-11-25"},
        )
        assert status == 200 and reply["result"]["isError"]
        error = json.loads(reply["result"]["content"][0]["text"])["error"]
        assert error["code"] == ("insufficient_scope" if code == 403 else "invalid_token")
    assert dict(ledger.efactura(before["message_id"])) == before


@pytest.mark.parametrize("change,expected", [("readonly", 403), ("revoked", 401)])
def test_explicit_upload_link_revocation_rechecks_the_actual_writer(
    token_owner, ledger, server, monkeypatch, change, expected
):
    made = tokens.create(ledger, "Writer", "write", user_id=token_owner)
    link = upload_links.create(ledger, "NORD", "Telefon de test")
    spec = actions.ACTIONS["upload-link-revoke"]

    def changed(current, co, body, ctx):
        with closing(Ledger(ledger.path)) as other, other.tx() as db:
            if change == "readonly":
                db.execute("UPDATE users SET role='readonly' WHERE id=?", (token_owner,))
            else:
                tokens.revoke(other, made["id"])
        return spec.handler(current, co, body, ctx)

    monkeypatch.setitem(actions.ACTIONS, "upload-link-revoke", replace(spec, handler=changed))
    assert (
        request(server, made["token"], f"/companies/NORD/upload-links/{link['id']}/revoke", "POST", {})[0] == expected
    )
    assert upload_links.list_links(ledger, "NORD")[0]["revoked_at"] is None


@pytest.mark.parametrize(
    "path,body",
    [
        ("/companies/UNKNOWN/actions/mark-paid", {"id": "101"}),
        ("/companies/UNKNOWN/upload-links/0123456789abcdef/revoke", {}),
    ],
)
def test_write_alias_and_generic_action_share_unknown_company_validation(token_owner, ledger, server, path, body):
    made = tokens.create(ledger, "Writer", "write", user_id=token_owner)
    status, answer = request(server, made["token"], path, "POST", body)
    assert status == 404 and answer["error"]["code"] == "company_not_found"
