"""Company reads must seek, rather than scan the other firms' books."""

from __future__ import annotations

import pytest
from firms import set_firms
from scale_benchmark import NOW, build

from fiscalm import expenses
from fiscalm.api import browse as browsing, dashboard_rows, lists, state as api
from fiscalm.api.responses import Error, bounded


@pytest.mark.parametrize(
    "sql,index",
    [
        ("SELECT * FROM payments WHERE company=? AND status='paid'", "payments_company_status"),
        (
            "SELECT * FROM documents WHERE company=? AND month='2026-10' AND kind='receipt'",
            "documents_company_month_kind",
        ),
        ("SELECT * FROM efactura WHERE company=? AND status='open'", "efactura_company_status"),
        ("SELECT * FROM issued WHERE company=?", "issued_company_status"),
        ("SELECT * FROM statement_txs WHERE company=? AND day>='2026-10-01'", "statement_company_day"),
        ("SELECT * FROM history WHERE company=? AND direction='issued'", "history_company_direction"),
    ],
)
def test_company_read_uses_index(ledger, sql, index):
    plan = " ".join(r["detail"] for r in ledger.db.execute("EXPLAIN QUERY PLAN " + sql, ("NORD",)))
    assert "SEARCH" in plan and index in plan


@pytest.mark.parametrize("divisor", [200, 100])
def test_fifteen_firm_query_budget(ledger, monkeypatch, divisor):
    from dataclasses import replace

    from scale_benchmark import NOW, build

    from fiscalm import companies
    from fiscalm.api import state as api

    company = next(iter(companies.all().values()))
    set_firms({f"SYN{i}": replace(company, key=f"SYN{i}", cui=str(12000000 + i), accounts=()) for i in range(15)})
    build(ledger, divisor)
    for co, budget in [
        ("SYN0", 249),
        ("ALL", 1505),
    ]:  # One extra bounded read per firm for structured receipt presence.
        queries = []
        ledger.db.set_trace_callback(queries.append)
        try:
            state = api.state(ledger, co, NOW)
        finally:
            ledger.db.set_trace_callback(None)
        assert len(queries) <= budget  # doubling entities must not introduce per-row reads
        assert sum(len(p["issued"]) for p in state.get("parts", [state])) == (15000 // divisor) * (
            15 if co == "ALL" else 1
        )

    from fiscalm.api import lists

    queries = []
    ledger.db.set_trace_callback(queries.append)
    try:
        lists.collection(ledger, "SYN0", {"collection": "invoices"}, NOW)
    finally:
        ledger.db.set_trace_callback(None)
    assert len(queries) <= 183  # One receipt-presence read; no other firms' switch counts.


def test_bounded_reads_keep_complete_totals_and_every_identity(ledger):
    from scale_benchmark import NOW, build

    from fiscalm.api import lists, state as api

    build(ledger, 100)
    full = api.state(ledger, "ALL", NOW)
    compact = lists.summary(ledger, "ALL", NOW)
    for key in ("payable", "receivable", "cod_receivable"):
        assert compact[key] == full[key]
    assert compact["task_summary"] == full["task_summary"]
    for original, small in zip(full["parts"], compact["parts"], strict=True):
        assert small["dashboard"] == original["dashboard"] | {"upcoming": []}
        assert small["upcoming_count"] == len(original["dashboard"]["upcoming"])
        assert small["bounded"]
        for key in (
            "payable",
            "receivable",
            "cod_receivable",
            "companies",
            "rules",
            "recurrences",
            "bank_connections",
        ):
            assert small[key] == original[key]
        assert {k: v for k, v in small["accountant"].items() if k != "partner"} == original["accountant"]
        for key in ("invoices", "issued", "partners", "documents", "expenses", "ambiguous", "orders"):
            assert small[key] == [] and small["record_counts"][key] == len(original[key])
        assert small["transactions"] == original["transactions"] | {"rows": [], "remembered": []}
        assert "payments" not in small and "payment_counts" not in small
        assert small["files"] == original["files"]
        task_page = lists.task_page(ledger, original["co"], {}, NOW)
        assert small["task_summary"] == original["task_summary"] == task_page | {"data": [], "next_cursor": None}
        for old, report in zip(original["reports"], small["reports"], strict=True):
            assert report["counts"]["docs"] == len(old["docs"])
            assert report["counts"]["issued"] == len(old["issued"])
            assert report["counts"]["purchases"] == sum(
                e["invoice"] in old["received"] or e["document"] in old["documents"] for e in original["expenses"]
            )
            assert all(not report[k] for k in ("received", "issued", "documents", "to_check", "docs"))
        for name in lists.COLLECTIONS:
            if name == "candidates":  # Nested card options have their own scope tests.
                continue
            expected = (
                original["transactions"]["rows"]
                if name == "transactions"
                else original["dashboard"]["upcoming"]
                if name == "upcoming"
                else original[name]
            )
            rows, cursor = [], ""
            while True:
                result = lists.collection(
                    ledger, original["co"], {"collection": name, "limit": "101", "cursor": cursor}, NOW
                )
                assert result["total"] == len(expected)
                assert len(result["data"]) <= 101
                rows.extend(result["data"])
                cursor = result["next_cursor"]
                if cursor is None:
                    break
            if name == "upcoming":
                assert [{k: v for k, v in r.items() if k != "cursor_id"} for r in rows] == sorted(
                    expected, key=lambda r: next(x["cursor_id"] for x in rows if x["id"] == r["id"])
                )
            else:
                assert rows == sorted(expected, key=lambda r: r["id"])
            if rows:
                found = lists.collection(
                    ledger,
                    original["co"],
                    {
                        "collection": name,
                        "id": rows[-1].get("cursor_id", rows[-1]["id"]),
                        "cursor": "zzz",
                        "status": "absent",
                    },
                    NOW,
                )
                assert found["data"] == [rows[-1]]


@pytest.mark.parametrize(
    "query",
    [{"collection": "absent"}, {"collection": "issued", "limit": "201"}, {"collection": "issued", "id": "absent"}],
)
def test_state_collection_refuses_invalid_reads(ledger, query):
    from fiscalm.api import lists
    from fiscalm.api.responses import Error

    with pytest.raises(Error) as error:
        lists.collection(ledger, "NORD", query)
    assert error.value.status == (404 if query.get("id") else 400)


@pytest.mark.parametrize(
    "query,code",
    [
        ({"collection": "invoices", "month": "2026-99"}, "invalid_month"),
        ({"collection": "partners", "month": "2026-10"}, "invalid_filter"),
        ({"collection": "documents", "status": "paid"}, "invalid_filter"),
    ],
)
def test_collection_filter_refusals(ledger, query, code):
    from fiscalm.api import lists
    from fiscalm.api.responses import Error

    with pytest.raises(Error) as error:
        lists.collection(ledger, "NORD", query)
    assert (error.value.status, error.value.code) == (400, code)


def test_tasks_page_keeps_every_key_count_and_bounded_card_inputs(ledger):
    from scale_benchmark import NOW, build

    from fiscalm.api import lists, state as api, tasks

    build(ledger, 100)
    ledger.db.execute("UPDATE efactura SET status='open', due_date='2026-10-01'")
    ledger.db.commit()
    original = api.state(ledger, "ALL", NOW)
    expected = dict((key, urgent) for part in original["parts"] for key, urgent, _ in tasks.references(part))
    rows, cursor = [], ""
    while True:
        result = lists.task_page(ledger, "ALL", {"limit": "50", "cursor": cursor}, NOW)
        assert result["total"] == len(expected)
        assert result["urgent"] == sum(expected.values())
        assert len(result["data"]) <= 50
        assert [r["key"] for r in result["highlights"]] == [k for k, u in expected.items() if u][:3]
        rows.extend(result["data"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert [r["key"] for r in rows] == list(expected)
    assert all(len(r["patch"].get("invoices", [])) <= 1 for r in rows)
    assert sum("late" in r["key"] for r in rows) == 300
    part = original["parts"][0]
    part["invoices"][0].update(
        status="linked", payment_state="in_file", credits=[{"id": "credit"}], amount_changed=True
    )
    part["live"] = True
    part["files"] = [
        {"id": 123, "status": "open", "question": None, "payment_count": 250, "confirmed_count": 0, "skipped_count": 0}
    ]
    collector = tasks.Collector(limit=200)
    collector.add(part)
    assert any(r["key"] == part["co"] + "credit" + part["invoices"][0]["id"] for r in collector.data)
    file_card = next(r for r in collector.data if r["key"] == part["co"] + "pf123")
    assert file_card["patch"]["files"] == part["files"]
    assert "payments" not in file_card["patch"]  # a file task carries no invoice/payment rows


@pytest.mark.parametrize(
    "query,code",
    [
        ({"limit": "201"}, "invalid_limit"),
        ({"cursor": "-1"}, "invalid_cursor"),
        ({"cursor": "x"}, "invalid_cursor"),
        ({"group": "absent"}, "invalid_cursor"),
    ],
)
def test_task_page_refuses_invalid_reads(ledger, query, code):
    from fiscalm.api import lists
    from fiscalm.api.responses import Error

    with pytest.raises(Error) as error:
        lists.task_page(ledger, "NORD", query)
    assert (error.value.status, error.value.code) == (400, code)


def test_search_uses_complete_entity_identities_and_amounts(ledger):
    from scale_benchmark import NOW, build

    from fiscalm.api import lists, search as searching, state as api

    build(ledger, 100)
    full = api.state(ledger, "ALL", NOW)
    for query in ("SYN149", "123,45", "RO 90000000", "partener", "−123,45"):
        expected = searching.Collector(query)
        for part in full["parts"]:
            expected.add(part)
        actual = lists.search(ledger, "ALL", {"q": query}, NOW)
        assert actual == expected.result()
        assert all(len(rows) <= 6 for rows in actual.values())
    result = lists.search(ledger, "NORD", {"q": "SYN149"}, NOW)
    assert result["invoices"][0]["id"] == "efactura-0-149"
    assert result["issued"][0]["id"] == "issued-0-149"


@pytest.mark.parametrize(
    "screen,source",
    [("vanzari", "issued"), ("parteneri", "partners"), ("cheltuieli", "invoices"), ("bonuri", "documents")],
)
def test_browse_pages_keep_every_row_identity_and_exact_balances(ledger, screen, source):
    build(ledger, 100)
    full = api.state(ledger, "ALL", NOW, can_manage_companies=True)
    expected = [r for part in full["parts"] for r in part[source]]
    date_key = "last" if source == "partners" else "date" if source == "documents" else "issue_date"
    expected.sort(key=lambda r: r[date_key] or "", reverse=True)
    rows, cursor = [], ""
    while True:
        result = lists.browse(
            ledger, "ALL", {"screen": screen, "limit": "71", "cursor": cursor}, NOW, can_manage_companies=True
        )
        assert result["total"] == len(expected) and len(result["data"]) <= 71
        assert result["months"] == sorted({r[date_key][:7] for r in expected if r[date_key]}, reverse=True)
        rows.extend(result["data"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert "|" not in cursor
    if screen == "cheltuieli":
        rows = [r["invoice"] for r in rows]
    elif screen == "bonuri":
        rows = [r["document"] for r in rows]
    assert rows == [bounded(r) for r in expected]
    if screen == "vanzari":
        query = {
            "screen": screen,
            "f": "open",
            "q": "partener sintetic 100 srl syn100",
            "month": "2026-07",
            "limit": "1",
        }
        result = lists.browse(ledger, "ALL", query, NOW)
        expected = [r for r in expected if r["number"] == "SYN100"]
        assert result["total"] == len(expected) == 2
        assert result["data"][0] == expected[0] and result["next_cursor"]


def test_summary_navigation_keeps_exact_late_identities_without_their_history(ledger):
    build(ledger, 100)
    full = api.state(ledger, "NORD", NOW)
    partner, issued = full["partners"][-1], full["issued"][-1]
    partner["client_id"], issued["local_id"] = "saved-client", "new-invoice"
    partner["held"] = [{"invoice": str(i)} for i in range(151)]
    issued["reversed_by"] = [str(i) for i in range(151)]
    full["accountant"] = {"cui": partner["cui"], "senders": [], "recipient": ""}
    small = lists.metadata(full, client_id="saved-client", issued_id="new-invoice")
    assert small["accountant"] == full["accountant"] | {"partner": {k: partner[k] for k in ("id", "name", "cui")}}
    assert small["partners"] == [bounded(partner | {"held": [], "report_formats": []})]
    assert small["issued"] == [bounded(issued)] and len(small["issued"][0]["reversed_by"]) == 50
    for key in ("payable", "receivable", "cod_receivable", "companies", "task_summary"):
        assert small[key] == full[key]
    assert small["record_counts"]["partners"] == len(full["partners"]) > 50
    assert "partner" not in full["accountant"] and len(partner["held"]) == len(issued["reversed_by"]) == 151
    plain = lists.metadata(full)
    assert plain["partners"] == plain["issued"] == []
    assert lists.metadata(full, client_id="absent", issued_id="absent") == plain
    full["accountant"]["cui"] = "absent"
    assert lists.metadata(full)["accountant"]["partner"] is None
    with pytest.raises(Error, match="Navigation context requires one company"):
        lists.summary(ledger, "ALL", client_id="saved-client")


def test_browse_purchase_categories_filters_and_links(ledger):
    build(ledger, 100)
    part = api.state(ledger, "NORD", NOW)
    inv, doc, mail = part["invoices"][0], part["documents"][0], part["documents"][1]
    inv["bon"], doc["invoice"] = doc["id"], inv["id"]
    mail.update(mail={"number": "MAIL-99"}, number="MAIL-99", document_type="invoice")
    part["expenses"] = expenses.derive("NORD", part["invoices"], part["documents"], expenses.evidence(ledger, "NORD"))
    for screen, query, expected in [
        ("cheltuieli", inv["supplier"] + " " + doc["vendor"] + " " + inv["number"], [(inv, doc)]),
        ("bonuri", inv["supplier"] + " " + doc["vendor"], [(inv, doc)]),
        ("cheltuieli", "MAIL-99", [(None, mail)]),
        ("bonuri", "MAIL-99", []),
    ]:
        collector = browsing.Collector({"screen": screen, "q": "\ufeff" + query + "\u00a0"})
        collector.add(part)
        result = collector.result()
        assert [(r["invoice"], r["document"]) for r in result["data"]] == expected
        assert result["total"] == len(expected)
    collector = browsing.Collector({"screen": "bonuri", "f": "open"})
    collector.add(part)
    assert collector.result()["total"] == len(part["documents"]) - 2  # paired receipt and mailed invoice excluded
    plain = part["invoices"][2]
    collector = browsing.Collector({"screen": "cheltuieli", "q": plain["supplier"] + " " + plain["number"]})
    collector.add(part)
    assert [r["invoice"]["id"] for r in collector.result()["data"]] == [plain["id"]]


@pytest.mark.parametrize(
    "screen,selection,source", [("vanzari", "storno", "issued"), ("parteneri", "accountant", "partners")]
)
def test_browse_choice_filters_keep_complete_eligibility_and_late_identities(ledger, screen, selection, source):
    build(ledger, 100)
    part = api.state(ledger, "NORD", NOW)
    base = part[source][0]
    part[source] = [
        base
        | {
            "id": f"choice-{i}",
            "can_storno": i % 3 != 0,
            "cui": ("", "123", "123A", "１２", "12345678901", "456789")[i % 6],
        }
        for i in range(151)
    ]
    expected = [r for i, r in enumerate(part[source]) if (i % 3 != 0 if selection == "storno" else i % 6 in (1, 5))]
    rows, cursor = [], ""
    while True:
        collector = browsing.Collector({"screen": screen, "f": selection, "limit": "17", "cursor": cursor})
        collector.add(part)
        result = collector.result()
        assert result["total"] == len(expected) and len(result["data"]) <= 17
        rows.extend(result["data"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert rows == [bounded(r) for r in expected]


@pytest.mark.parametrize(
    "query,code",
    [
        ({"screen": "absent"}, "invalid_screen"),
        ({"limit": "201"}, "invalid_limit"),
        ({"cursor": "secret|0"}, "invalid_cursor"),
        ({"cursor": "2026-10-01:-1"}, "invalid_cursor"),
        ({"month": "2026-99"}, "invalid_month"),
        ({"f": "absent"}, "invalid_filter"),
        ({"screen": "parteneri", "month": "2026-10"}, "invalid_filter"),
        ({"screen": "parteneri", "f": "storno"}, "invalid_filter"),
        ({"screen": "vanzari", "f": "accountant"}, "invalid_filter"),
        ({"screen": "bonuri", "f": "accountant"}, "invalid_filter"),
        ({"screen": "payment_file", "month": "2026-10"}, "invalid_screen"),
        ({"screen": "payment_files", "q": "payment"}, "invalid_screen"),
        ({"screen": "payment_file"}, "invalid_screen"),
        ({"screen": "tranzactii", "f": "open"}, "invalid_filter"),
        ({"screen": "tranzactii", "from": "2026-02-30"}, "invalid_date"),
        ({"screen": "bonuri", "from": "2026-10-01"}, "invalid_filter"),
        ({"screen": "report_received"}, "invalid_month"),
        ({"screen": "report_bank", "month": "2026-99"}, "invalid_month"),
        ({"screen": "report_tasks", "month": "2026-10", "f": "open"}, "invalid_filter"),
        ({"screen": "report_docs", "month": "2026-10", "q": "supplier"}, "invalid_filter"),
    ],
)
def test_browse_refuses_invalid_reads_before_projection(ledger, monkeypatch, query, code):
    monkeypatch.setattr(api, "entity_rows", lambda *a, **kw: pytest.fail("Invalid request projected rows"))
    monkeypatch.setattr(api, "state", lambda *a, **kw: pytest.fail("Invalid request projected state"))
    with pytest.raises(Error) as error:
        lists.browse(ledger, "NORD", {"screen": "vanzari"} | query)
    assert (error.value.status, error.value.code) == (400, code)


@pytest.mark.parametrize("collection", ["upcoming", "held_money"])
def test_dashboard_pages_preserve_full_order_and_held_currency_sums(collection):
    parts = {co: {"co": co, "partners": [], "dashboard": {"upcoming": []}} for co in ("A", "B", "C")}
    for co, who in (("A", "Zulu"), ("B", "Alfa"), ("C", "Mijloc")):
        common = {"company": co, "kind": "invoice", "amount": "1.00", "currency": "RON", "number": "SYN"}
        parts[co]["dashboard"]["upcoming"] = [
            {**common, "id": co + "0", "date": "2026-11-01", "side": "out", "who": who},
            {**common, "id": co + "1", "date": "2026-11-02", "side": "in", "who": "Același"},
        ]
    parts["A"]["partners"] = [
        {
            "id": "A.p",
            "name": "Curier",
            "held": [{"currency": "RON", "amount": "0.10"}] * 151 + [{"currency": "EUR", "amount": "1.20"}],
        }
    ]
    parts["B"]["partners"] = [{"id": "B.p", "name": "Procesator", "held": [{"currency": "EUR", "amount": "3.00"}]}]
    expected_ids = (
        ["B0", "C0", "A0", "A1", "B1", "C1"] if collection == "upcoming" else ["A.p:RON", "A.p:EUR", "B.p:EUR"]
    )
    seen, cursor, calls = [], "", []

    def read(co):
        calls.append(co)
        return parts[co]

    while True:
        calls.clear()
        result = dashboard_rows.project(list(parts), read, {"collection": collection, "limit": "1", "cursor": cursor})
        assert len(calls) == 3 and set(calls) == set(parts)  # no second projection to recover a cursor
        if cursor:
            assert calls[0] == list(parts)[int(cursor.split(":")[0])]
        assert result["total"] == len(expected_ids) and len(result["data"]) == 1
        seen.extend(result["data"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert all(p.isascii() and p.isdecimal() for p in cursor.split(":"))
    assert [r["id"] for r in seen] == expected_ids
    if collection == "held_money":
        assert [(r["company"], r["partner"], r["currency"], r["amount"]) for r in seen] == [
            ("A", "A.p", "RON", "15.10"),
            ("A", "A.p", "EUR", "1.20"),
            ("B", "B.p", "EUR", "3.00"),
        ]


@pytest.mark.parametrize(
    "query,code",
    [
        ({"collection": "unknown"}, "invalid_collection"),
        ({"collection": "upcoming", "limit": "201"}, "invalid_limit"),
        ({"collection": "upcoming", "cursor": "99:0"}, "invalid_cursor"),
        ({"collection": "held_money", "cursor": "secret|key"}, "invalid_cursor"),
        ({"collection": "held_money", "q": "x"}, "invalid_filter"),
    ],
)
def test_dashboard_panels_refuse_before_projection(query, code):
    with pytest.raises(Error) as error:
        dashboard_rows.project(["A"], lambda co: pytest.fail("Invalid panel projected state"), query)
    assert (error.value.status, error.value.code) == (400, code)


@pytest.mark.parametrize("collection", ["upcoming", "held_money"])
def test_initial_dashboard_panels_merge_complete_prefixes_and_continue(collection):
    parts = {}
    for firm, co in enumerate(("A", "B")):
        parts[co] = {
            "co": co,
            "dashboard": {
                "upcoming": [
                    {
                        "id": f"{co}{i}",
                        "company": co,
                        "date": "2026-11-01",
                        "side": "in",
                        "who": f"{2 * i + 1 - firm:03d}",
                    }
                    for i in range(70)
                ]
            },
            "partners": [
                {"id": f"{co}.p{i}", "name": f"Partener {i}", "held": [{"currency": "RON", "amount": "0.01"}]}
                for i in range(70)
            ],
        }
        parts[co]["dashboard_pages"] = dashboard_rows.initial(parts[co])
        assert all(len(page["data"]) == 50 and page["total"] == 70 for page in parts[co]["dashboard_pages"].values())
    first = dashboard_rows.merge(list(parts.values()))[collection]
    assert first == dashboard_rows.project(list(parts), parts.__getitem__, {"collection": collection})
    assert first["total"] == 140 and len(first["data"]) == 50 and first["next_cursor"] is not None
    following = dashboard_rows.project(
        list(parts), parts.__getitem__, {"collection": collection, "cursor": first["next_cursor"]}
    )
    assert len(following["data"]) == 50 and not (
        {r["id"] for r in first["data"]} & {r["id"] for r in following["data"]}
    )
