import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import pytest
from test_api import (
    _api,  # noqa: F401
    server as server,
)
from test_api_v1 import request
from test_invoicing import DATA, LINE, PARTY, draft, setup_invoice as setup_invoice  # noqa: F401

from fiscalm import bnr
from fiscalm.api import mcp_tools, schema as api_schema, state as api, tokens
from fiscalm.invoicing import cli as invoice_cli, engine as i, recurring as r
from fiscalm.ledger import Ledger

EUR = DATA | dict(
    issue_date="2027-02-01", due_date="2027-02-15", currency="EUR", exchange_rate="5.25", exchange_date="2027-01-29"
)


def rule(setup_invoice, day=31, today="2026-10-01"):
    L, _ = setup_invoice
    return r.create(L, "NORD", draft(setup_invoice), day, 15, today, first_period=today[:7] if day == 31 else None)


def eur_rule(setup_invoice, data=EUR):
    """A monthly rule on the 1st from a EUR draft; returns the draft."""
    source = draft(setup_invoice, data)
    r.create(setup_invoice[0], "NORD", source, 1, 15, "2026-10-15")
    return source


def periods(L):
    return L.db.execute("SELECT count(*) FROM invoice_recurrence_periods").fetchone()[0]


def next_number(L):
    return L.db.execute("SELECT next_number FROM invoice_series").fetchone()[0]


def test_current_only_clamp_template_snapshot_issued_on_its_day(setup_invoice):
    L, cid = setup_invoice
    source = draft(setup_invoice)
    rid = r.create(L, "NORD", source, 31, 15, "2026-10-01", first_period="2026-10")
    # Later edits and the deletion of the source draft never change the template.
    i.save_draft(
        L, "NORD", "TEST", cid, DATA | dict(notes="Changed", lines=[LINE | dict(unit_price="999")]), source, version=1
    )
    i.delete_draft(L, source, company="NORD", version=2)
    assert r.run(L, "2027-02-27") == []
    ids = r.run(L, "2027-02-28")
    assert len(ids) == 1
    inv = i.invoice(L, ids[0], company="NORD")
    assert (inv["status"], inv["number"]) == ("issued", 42)
    assert r.list_rules(L, "NORD", "2027-02-28")[0]["last_invoice"] == dict(
        id=ids[0], number="TEST-42", client="Client Fictiv SRL", at="2027-02-28T00:00:00"
    )
    assert inv["data"]["issue_date"] == "2027-02-28"
    assert inv["data"]["due_date"] == "2027-03-15"
    assert (inv["data"]["currency"], inv["data"]["notes"]) == ("RON", "")
    assert (inv["lines"][0]["description"], inv["lines"][0]["unit_price"]) == (LINE["description"], "100")
    assert r.list_rules(L, "NORD", "2026-10-27")[0]["skipped_periods"] == 4
    assert r.list_rules(L, "NORD", "2026-10-27")[0]["backlog_policy"] == "current_only"
    assert r.run(L, "2027-02-28") == []
    assert len(r.run(L, "2027-03-31")) == 1
    assert next_number(L) == 44
    assert L.db.execute("SELECT count(*) FROM invoice_submissions").fetchone()[0] == 0
    assert L.db.execute("SELECT recurrence FROM invoice_recurrence_periods LIMIT 1").fetchone()[0] == rid


def test_two_connections_one_period_and_crash(setup_invoice, monkeypatch):
    L, _ = setup_invoice
    rule(setup_invoice)
    original = i.save_draft

    def crash(*a, **kw):
        original(*a, **kw)
        raise RuntimeError("crash after draft insert")

    monkeypatch.setattr(i, "save_draft", crash)
    with pytest.raises(RuntimeError):
        r.run(L, "2026-10-31")
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1
    assert periods(L) == 0
    monkeypatch.setattr(i, "save_draft", original)
    # a crash after the reservation, before the caller's commit
    with pytest.raises(RuntimeError), L.tx():
        assert len(r.run(L, "2026-10-31")) == 1
        raise RuntimeError("crash before commit")
    assert periods(L) == 0
    barrier = Barrier(2)

    def run(_):
        other = Ledger(L.path)
        try:
            barrier.wait(timeout=5)
            return r.run(other, "2026-10-31")
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        assert sorted(map(len, pool.map(run, range(2)))) == [0, 1]
    assert periods(L) == 1
    assert [tuple(x) for x in L.db.execute("SELECT status, number FROM invoices WHERE status!='draft'")] == [
        ("issued", 42)
    ]
    assert next_number(L) == 43  # one number, taken once


def test_a_refused_issue_keeps_the_draft_and_its_task_until_the_draft_goes(setup_invoice):
    L, _ = setup_invoice
    rid = rule(setup_invoice)
    i.issue(L, draft(setup_invoice, DATA | dict(issue_date="2026-11-02")), "2026-11-02", company="NORD", version=1)
    report = r.report(L, "2026-10-31")  # a clock behind the series' last invoice: the engine refuses, as by hand
    [kept] = report["drafts"]
    why = "Data emiterii nu poate fi înaintea ultimei facturi din serie."
    task = dict(id=kept, client="Client Fictiv SRL", recurrence=rid, why=why, at="2026-10-31T00:00:00")
    assert (report["issued"], r.refused(L, "NORD"), r.list_rules(L, "NORD", "2026-10-31")[0]["last_invoice"]) == (
        [],
        [task],
        None,
    )
    assert (periods(L), next_number(L), r.run(L, "2026-10-31")) == (1, 43, [])
    tasks = api.state(L, "NORD", datetime(2026, 10, 31, 12))["task_summary"]
    assert any(t["key"] == "NORDrec" + kept for t in tasks["highlights"])
    assert len(r.report(L, "2026-11-30")["issued"]) == 1  # next month issues; October's task stays
    assert r.list_rules(L, "NORD", "2026-11-30")[0]["last_invoice"]["number"] == "TEST-43"
    r.change(L, "NORD", rid, "delete")
    assert r.refused(L, "NORD") == [task]  # its draft is still there
    i.delete_draft(L, kept, company="NORD", version=1)
    assert r.refused(L, "NORD") == []


def test_a_refusal_after_numbering_rolls_the_issue_back(setup_invoice, monkeypatch):
    L, _ = setup_invoice
    rule(setup_invoice)

    def refuse(*_):
        raise i.InvoiceError("refused late")

    monkeypatch.setattr(i, "_record_local", refuse)  # after the draft became an invoice and the series moved
    [kept] = r.report(L, "2026-10-31")["drafts"]
    assert (i.invoice(L, kept, company="NORD")["status"], next_number(L), periods(L)) == ("draft", 42, 1)


@pytest.mark.parametrize("table,reason", [("invoice_clients", "missing_client"), ("invoice_series", "missing_series")])
def test_missing_dependencies_pause_and_scope(setup_invoice, table, reason):
    L, _ = setup_invoice
    rid = rule(setup_invoice)
    listed = lambda: r.list_rules(L, "NORD", "2026-10-27")[0]  # noqa: E731
    caps = lambda: {k: v for k, v in listed().items() if k.startswith("can_")}  # noqa: E731
    with pytest.raises(i.InvoiceError):
        r.change(L, "LABO", rid, "delete")
    assert caps() == dict(can_pause=True, can_resume=False, can_delete=True)
    with pytest.raises(i.InvoiceError, match="Regula este deja pornită."):
        r.change(L, "NORD", rid, "resume")
    with L.tx() as db:
        db.execute("DELETE FROM " + table)
    assert listed()["next_date"] is None  # no next draft shown; run stops the rule instead
    assert r.run(L, "2026-10-31") == []
    assert (listed()["paused"], listed()["reason"]) == (True, reason)
    # No Pornește; pausing again would hide why fiscalm stopped it.
    assert caps() == dict(can_pause=False, can_resume=False, can_delete=True)
    with pytest.raises(i.InvoiceError) as refused:
        r.change(L, "NORD", rid, "resume")
    assert (
        str(refused.value)
        == {
            "missing_client": "Clientul regulii lipsește. Pentru o regulă nouă, alege un client existent.",
            "missing_series": "Seria regulii nu mai există. După ce administratorul instalării reface aceeași serie, pornește regula.",
        }[reason]
    )
    with pytest.raises(i.InvoiceError, match="Regula este deja oprită."):
        r.change(L, "NORD", rid, "pause")
    r.change(L, "NORD", rid, "delete")
    assert r.list_rules(L, "NORD", "2026-10-27") == []


@pytest.mark.parametrize("day,due", [(0, 1), (32, 1), (True, 1), (1, -1), (1, 366)])
def test_invalid_schedule(setup_invoice, day, due):
    L, _ = setup_invoice
    with pytest.raises(i.InvoiceError):
        r.create(L, "NORD", draft(setup_invoice), day, due, "2026-10-01")


def test_invalid_template_pauses(setup_invoice):
    L, _ = setup_invoice
    rule(setup_invoice)
    with L.tx() as db:
        db.execute("DELETE FROM invoice_sellers")
    assert r.run(L, "2026-10-31") == []
    assert r.list_rules(L, "NORD", "2026-10-27")[0]["reason"] == "invalid_template"
    assert periods(L) == 0


def test_cli_creates_runs_and_issues_a_generated_draft_by_version(setup_invoice, tmp_path, capsys):
    L, cid = setup_invoice
    parser = argparse.ArgumentParser()
    invoice_cli.register(parser.add_subparsers())

    def cli(*argv):
        args = parser.parse_args(["invoicing", *argv])
        code = args.fn(args, L)
        out = capsys.readouterr().out
        return code, json.loads(out) if code == 0 else None

    assert cli(
        "recur",
        "create",
        "--company",
        "NORD",
        "--draft",
        draft(setup_invoice),
        "--day",
        "31",
        "--due-days",
        "15",
        "--today",
        "2026-10-01",
        "--first-period",
        "2026-10",
    )[1]["id"]
    code, made = cli("recur", "run", "--today", "2026-10-31")
    assert code == 0 and len(made["issued"]) == 1 and made["drafts"] == []
    assert cli("recur", "run", "--today", "2026-10-31")[1] == {"issued": [], "drafts": [], "skipped": []}
    iid = draft(setup_invoice)
    source = tmp_path / "draft.json"
    source.write_text(
        json.dumps(
            dict(
                company="NORD", series="TEST", client_id=cid, data=DATA | dict(due_date="2026-11-15", notes="Reviewed")
            )
        )
    )
    assert cli("draft", str(source), "--id", iid, "--version", "1")[1]["version"] == 2
    issue = ("issue", iid, "--company", "NORD", "--today", "2026-10-31", "--version")
    assert cli(*issue, "1")[0] == 2  # a stale version never issues
    assert i.invoice(L, iid, company="NORD")["status"] == "draft" and next_number(L) == 43
    code, result = cli(*issue, "2")
    assert code == 0 and (result["number"], result["version"]) == (43, 2)
    assert r.run(L, "2026-10-31") == []
    with pytest.raises(i.InvoiceError):
        r.run(L, "20261031")


@pytest.mark.parametrize(
    "today,day,first_period,expected,drafts",
    [
        ("2026-10-15", 31, None, "2026-11", 0),  # the template's issue month is this month: the next one
        ("2027-02-28", 31, None, "2027-03", 0),
        ("2027-02-27", 31, None, "2027-02", 0),  # clamped day passed or not
        ("2026-12-31", 31, None, "2027-01", 0),
        ("2026-10-15", 1, "2026-10", "2026-10", 1),
        ("2026-10-15", 1, "2027-01", "2027-01", 0),  # explicit
    ],
)
def test_first_period_starts_after_elapsed_schedule(setup_invoice, today, day, first_period, expected, drafts):
    L, _ = setup_invoice
    r.create(L, "NORD", draft(setup_invoice), day, 15, today, first_period)
    assert r.list_rules(L, "NORD", "2026-10-27")[0]["start_period"] == expected
    assert len(r.run(L, today)) == drafts


@pytest.mark.parametrize("first_period", ["2026-09", "2026-1", 202610])
def test_explicit_first_period_refuses_past_or_invalid(setup_invoice, first_period):
    L, _ = setup_invoice
    with pytest.raises(i.InvoiceError, match="Prima perioadă"):
        r.create(L, "NORD", draft(setup_invoice), 1, 15, "2026-10-15", first_period)
    assert r.list_rules(L, "NORD", "2026-10-27") == []


def current_feed(_):
    return b'<DataSet><Body><Cube date="2027-01-29"><Rate currency="EUR">5.25</Rate></Cube><Cube date="2027-02-01"><Rate currency="EUR">5.30</Rate></Cube></Body></DataSet>'


def test_fx_rate_failure_leaves_period_open_and_retries_current_rate(setup_invoice, monkeypatch):
    L, _ = setup_invoice
    eur_rule(setup_invoice, DATA | dict(currency="EUR", exchange_rate="4", exchange_date="2026-09-30"))
    assert r.run(L, "2027-02-01") == []  # fixture fakes an unavailable feed
    assert periods(L) == 0
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1
    failed = r.list_rules(L, "NORD", "2026-10-27")[0]
    assert failed["reason"] == "rate_unavailable" and not failed["paused"]
    monkeypatch.setattr(bnr, "fetch_xml", current_feed)
    iid = r.run(L, "2027-02-01")[0]
    inv = i.invoice(L, iid, company="NORD")
    assert (inv["data"]["exchange_rate"], inv["data"]["exchange_date"]) == ("5.25", "2027-01-29")
    assert r.list_rules(L, "NORD", "2026-10-27")[0]["reason"] is None
    assert r.run(L, "2027-02-01") == []
    assert inv["number"] == 42


@pytest.mark.parametrize("selection,value,published", [("fetch", "4", "2027-01-29"), ("cache", "5.25", "2026-09-30")])
def test_issue_refuses_known_stale_fx_without_consuming_number(setup_invoice, monkeypatch, selection, value, published):
    L, cid = setup_invoice
    data = EUR | dict(exchange_rate=value, exchange_date=published)
    iid = draft(setup_invoice, data)
    if selection == "cache":
        with L.tx() as db:
            db.execute("INSERT INTO invoice_rates VALUES(?,?,?,?)", ("EUR", "2027-02-01", "5.25", "2027-01-29"))
    else:
        monkeypatch.setattr(bnr, "fetch_xml", current_feed)
    with pytest.raises(i.InvoiceError, match="ultimei publicații BNR"):
        i.issue(L, iid, "2027-02-01", company="NORD", version=1)
    assert i.invoice(L, iid, company="NORD")["status"] == "draft" and next_number(L) == 42
    i.save_draft(L, "NORD", "TEST", cid, EUR, iid, version=1)
    assert i.issue(L, iid, "2027-02-01", company="NORD", version=2)["number"] == 42


def test_gap_after_first_draft_reports_exact_skipped_periods(setup_invoice):
    L, _ = setup_invoice
    rid = rule(setup_invoice)
    assert len(r.run(L, "2026-10-31")) == 1
    report = r.report(L, "2027-02-28")
    assert len(report["issued"]) == 1
    event = report["skipped"][0]
    assert (event["from_period"], event["through_period"], event["count"]) == ("2026-11", "2027-01", 3)
    assert event["recurrence"] == rid
    assert r.report(L, "2027-02-28") == {"issued": [], "drafts": [], "skipped": []}
    state = api.state(L, "NORD", datetime(2027, 2, 28, 12))
    assert state["recurrences"][0]["skipped_periods"] == 3
    assert state["recurrences"][0]["last_skipped"] == event
    issued, news = [n for n in state["news"] if n["kind"] == "rule"]  # what went out, then the months omitted
    assert (issued["title"], issued["result"]) == ("Factură lunară emisă", "TEST-43, Client Fictiv SRL")
    assert news["ref"] == rid and news["result"] == "2026-11 - 2027-01"
    assert api.state(L, "STUDIO9", datetime(2027, 2, 28, 12))["news"] == []


def partial_feed(_):
    """Before the day's publication: the selection isn't cached, so issuing would fetch again."""
    return b'<DataSet><Body><Cube date="2027-01-29"><Rate currency="EUR">5.25</Rate></Cube></Body></DataSet>'


@pytest.mark.parametrize("operation,feed", [("run", current_feed), ("run", partial_feed), ("issue", current_feed)])
def test_fx_fetch_allows_another_writer(setup_invoice, monkeypatch, operation, feed):
    L, _ = setup_invoice
    source = eur_rule(setup_invoice)

    def fetch(url):
        assert not L.db.in_transaction
        other = Ledger(L.path)
        try:
            with other.tx():
                other.set_meta("other_writer", "ok")
        finally:
            other.close()
        return feed(url)

    monkeypatch.setattr(bnr, "fetch_xml", fetch)
    if operation == "run":
        assert len(r.report(L, "2027-02-01")["issued"]) == 1
    else:
        assert i.issue(L, source, "2027-02-01", company="NORD", version=1)["number"] == 42
    assert L.get_meta("other_writer") == "ok"


def test_issue_refuses_unconfirmed_rate(setup_invoice):
    L, _ = setup_invoice
    source = draft(setup_invoice, DATA | dict(currency="EUR", exchange_rate="4.5", exchange_date="2026-10-30"))
    with pytest.raises(i.InvoiceError, match="Cursul BNR nu poate fi confirmat"):
        i.issue(L, source, DATA["issue_date"], company="NORD", version=1)
    assert i.invoice(L, source, company="NORD")["status"] == "draft" and next_number(L) == 42


@pytest.mark.parametrize("operation", ["run", "issue"])
def test_cached_publication_rechecked_under_lock(setup_invoice, monkeypatch, operation):
    L, _ = setup_invoice
    source = eur_rule(setup_invoice)
    original = bnr.rate

    def changed(*args, **kwargs):
        selected = original(*args, **kwargs)
        with L.tx() as db:
            db.execute("UPDATE invoice_rates SET rate='5.26' WHERE currency='EUR'")
        return selected

    monkeypatch.setattr(bnr, "fetch_xml", current_feed)
    monkeypatch.setattr(bnr, "rate", changed)
    if operation == "run":
        assert r.run(L, "2027-02-01") == []
    else:
        with pytest.raises(i.InvoiceError, match="nu poate fi confirmat"):
            i.issue(L, source, "2027-02-01", company="NORD", version=1)
    assert periods(L) == 0 and next_number(L) == 42


def test_unsupported_currency_pauses_without_retry(setup_invoice, monkeypatch):
    L, _ = setup_invoice
    eur_rule(setup_invoice, DATA | dict(currency="XYZ", exchange_rate="4", exchange_date="2026-09-30"))
    monkeypatch.setattr(bnr, "fetch_xml", current_feed)
    assert r.run(L, "2027-02-01") == []
    failed = r.list_rules(L, "NORD", "2026-10-27")[0]
    assert failed["paused"] and failed["reason"] == "unsupported_currency"

    def unexpected(_):
        pytest.fail("Paused rule fetched BNR again")

    monkeypatch.setattr(bnr, "fetch_xml", unexpected)
    assert r.run(L, "2027-03-01") == [] and periods(L) == 0


def test_issue_confirms_intracommunity_tax_date(setup_invoice, monkeypatch):
    L, cid = setup_invoice
    i.save_client(L, "NORD", PARTY | dict(country="DE", cui="DE999999999", vat_id="DE999999999"), cid)
    data = EUR | dict(
        delivery_date="2027-01-15", lines=[LINE | dict(vat_category="K", vat_rate="0", exemption_reason="VATEX-EU-IC")]
    )
    source = draft(setup_invoice, data)
    monkeypatch.setattr(bnr, "fetch_xml", current_feed)
    issued = i.issue(L, source, "2027-02-01", company="NORD", version=1)
    assert issued["data"]["chargeability_date"] == "2027-02-01"


def test_listing_names_client_total_and_the_day_run_prepares_the_next_draft(setup_invoice):
    L, cid = setup_invoice
    rid = r.create(L, "NORD", draft(setup_invoice), 31, 15, "2026-10-01", first_period="2026-10")
    row = r.list_rules(L, "NORD", "2026-10-05")[0]
    assert (row["client"], row["total"], row["next_date"]) == ("Client Fictiv SRL", "121.00", "2026-10-31")
    assert r.list_rules(L, "NORD", "2027-02-03")[0]["next_date"] == "2027-02-28"  # clamped to the month
    assert r.run(L, "2026-10-30") == []  # the day before next_date: nothing
    assert len(r.run(L, "2026-10-31")) == 1  # next_date: the draft
    assert r.list_rules(L, "NORD", "2026-10-31")[0]["next_date"] == "2026-11-30"
    # A month missed while paused: resumed after its day, the next run (today) prepares it.
    r.change(L, "NORD", rid, "pause")
    assert r.list_rules(L, "NORD", "2026-12-31")[0]["next_date"] is None
    r.change(L, "NORD", rid, "resume")
    assert r.list_rules(L, "NORD", "2027-01-10")[0]["next_date"] == "2027-01-31"
    assert r.run(L, "2027-01-30") == [] and len(r.run(L, "2027-01-31")) == 1
    L.db.execute("UPDATE invoice_recurrences SET day=5 WHERE id=?", (rid,))
    L.db.commit()
    assert r.list_rules(L, "NORD", "2027-02-20")[0]["next_date"] == "2027-02-20"
    # The client gone, the template no longer valid: unknown, never guessed.
    with L.tx() as db:
        db.execute("DELETE FROM invoice_clients WHERE id=?", (cid,))
        db.execute(
            "UPDATE invoice_recurrences SET template=json_set(template, '$.lines[0].vat_rate', '7') WHERE id=?", (rid,)
        )
    row = r.list_rules(L, "NORD", "2027-02-20")[0]
    assert (row["client"], row["total"]) == (None, None)


def schedule(day=31, start="2026-10", last=None, paused=0):
    return dict(day=day, start_period=start, last_period=last, paused=paused)


@pytest.mark.parametrize(
    "rule,today,expected",
    [
        (schedule(day=31), "2027-02-27", (None, "before_day")),
        (schedule(day=31), "2027-02-28", ("2027-02", None)),  # clamped to February's last day
        (schedule(day=31), "2028-02-29", ("2028-02", None)),  # and to a leap year's
        (schedule(day=1, start="2026-12"), "2026-11-30", (None, "not_started")),  # first_period
        (schedule(day=1, start="2026-12"), "2026-12-01", ("2026-12", None)),
        (schedule(day=5, last="2026-10"), "2026-10-31", (None, "made")),
        (schedule(day=5, last="2026-11"), "2026-10-31", (None, "made")),  # never backwards into an earlier month
        (schedule(day=5, last="2026-10"), "2027-03-05", ("2027-03", None)),  # skipped months: only the current one
        (schedule(day=5, last="2026-10", paused=1), "2027-03-05", (None, "paused")),
    ],
)
def test_due_is_the_one_schedule(rule, today, expected):
    assert r.due(rule, today) == expected


@pytest.mark.parametrize(
    "rule",
    [
        schedule(day=31),
        schedule(day=29, start="2027-02"),
        schedule(day=1, start="2027-01"),
        schedule(day=15, last="2026-10"),
        schedule(day=31, last="2026-12"),
        schedule(day=5, paused=1),
        schedule(day=31, last="2027-02"),
    ],
)  # a period made months ahead: past it
def test_next_date_is_the_first_day_due_says_run_makes_a_draft(rule):
    """Display and run cannot drift: next_date is, day by day, the first day `due` accepts."""
    days = [date(2026, 10, 1) + timedelta(days=n) for n in range(250)]
    for n, today in enumerate(days):
        first = next((d.isoformat() for d in days[n:] if r.due(rule, d.isoformat())[0]), None)
        if first is None and not rule["paused"]:
            continue  # beyond the scanned range
        assert r.next_date(rule, today.isoformat()) == first, today


def test_run_makes_drafts_exactly_on_the_days_next_date_names(setup_invoice):
    L, _ = setup_invoice
    r.create(L, "NORD", draft(setup_invoice), 31, 15, "2026-10-01", first_period="2026-12")
    made = []
    for n in range(160):
        today = (date(2026, 10, 1) + timedelta(days=n)).isoformat()
        expected = r.list_rules(L, "NORD", today)[0]["next_date"]
        if r.run(L, today):
            made.append(today)
        assert (expected == today) == (made[-1:] == [today]), today
    assert made == ["2026-12-31", "2027-01-31", "2027-02-28"]


def test_listing_reads_a_fixed_number_of_queries(setup_invoice):
    L, cid = setup_invoice

    def queries() -> int:
        seen: list[str] = []
        L.db.set_trace_callback(seen.append)
        try:
            r.list_rules(L, "NORD", "2026-10-27")
        finally:
            L.db.set_trace_callback(None)
        return len(seen)

    rid = rule(setup_invoice)
    L.set_meta("recurrence_skip:" + rid, '{"count": 1}')
    one = queries()
    for _ in range(5):
        rule(setup_invoice)
    assert queries() == one == 5
    skipped = {x["id"]: x["last_skipped"] for x in r.list_rules(L, "NORD", "2026-10-27")}
    assert skipped.pop(rid) == {"count": 1} and set(skipped.values()) == {None}


def test_recurrences_through_v1_the_app_and_mcp(token_owner, setup_invoice, server):
    L, _ = setup_invoice
    writer = tokens.create(L, "Synthetic writer", "write", user_id=token_owner)["token"]
    reader = tokens.create(L, "Synthetic reader", "read", user_id=token_owner)["token"]
    path = "/companies/NORD/invoicing"
    body = dict(draft_id=draft(setup_invoice), day=1, due_days=15, first_period="2099-01")
    assert request(server, reader, path + "/recurrence-create", "POST", body)[0] == 403
    assert request(server, writer, "/companies/STUDIO9/invoicing/recurrence-create", "POST", body)[0] == 409
    code, created = request(server, writer, path + "/recurrence-create", "POST", body)
    assert code == 200 and created["ok"]
    rid = created["id"]
    rules = request(server, reader, path + "/recurrences")[1]
    assert [x["id"] for x in rules] == [rid] and rules[0]["start_period"] == "2099-01"
    assert api.state(L, "NORD")["recurrences"] == rules
    assert mcp_tools.call(L, "read", "get_invoicing_recurrences", {"co": "NORD"})[0]["id"] == rid
    assert request(server, reader, "/companies/STUDIO9/invoicing/recurrences")[1] == []
    assert mcp_tools.call(L, "read", "get_invoicing_recurrences", {"co": "STUDIO9"}) == []
    code, paused = request(server, writer, path + "/recurrence-pause", "POST", {"id": rid})
    assert code == 200 and paused["message"] == "Regula a fost oprită."  # the toast repeats the button's verb
    assert r.list_rules(L, "NORD", "2026-10-27")[0]["reason"] == "manual"
    again = _api(server, "invoicing/recurrence-pause", dict(co="NORD", id=rid))[1]  # a double submit changes nothing
    assert not again["ok"] and again["message"] == "Regula este deja oprită."
    assert {k: v for k, v in api.state(L, "NORD")["recurrences"][0].items() if k.startswith("can_")} == dict(
        can_pause=False, can_resume=True, can_delete=True
    )
    assert _api(server, "invoicing/recurrence-resume", dict(co="NORD", id=rid), Origin="https://evil.invalid")[0] == 403
    assert _api(server, "invoicing/recurrence-create", b" " * 4097)[0] == 413
    assert not _api(server, "invoicing/recurrence-create", dict(co="NORD", **body, unexpected=1))[1]["ok"]
    assert (
        _api(server, "invoicing/recurrence-resume", dict(co="NORD", id=rid))[1]["message"] == "Regula a fost pornită."
    )
    assert not r.list_rules(L, "NORD", "2026-10-27")[0]["paused"]
    assert request(server, writer, path + "/recurrence-list", "POST", {})[0] == 404
    with L.tx() as db:
        db.execute("DELETE FROM invoice_clients")
    r.run(L, "2099-01-01")
    result = _api(server, "invoicing/recurrence-resume", dict(co="NORD", id=rid))[1]
    assert not result["ok"] and "Clientul regulii lipsește" in result["message"]
    code, result = request(server, writer, path + "/recurrence-resume", "POST", {"id": rid})
    assert code == 409 and "Clientul regulii lipsește" in result["error"]["message"]
    assert request(server, writer, path + "/recurrence-delete", "POST", {"id": rid})[0] == 200
    assert r.list_rules(L, "NORD", "2026-10-27") == []


def test_openapi_and_mcp_contract():
    doc = api_schema.openapi()
    base = "/api/v1/companies/{co}/invoicing/"
    assert doc["paths"][base + "recurrences"]["get"]["responses"]["200"]
    assert base + "recurrence-list" not in doc["paths"]
    assert "recurrences" in doc["components"]["schemas"]["State"]["properties"]
    specs = {t["name"]: t for t in mcp_tools.definitions("write")}
    assert "invoicing_recurrence_list" not in specs
    assert not specs["invoicing_recurrence_create"]["annotations"]["idempotentHint"]
    assert specs["invoicing_recurrence_delete"]["annotations"]["destructiveHint"]
    tool = next(t for t in mcp_tools.definitions("read") if t["name"] == "get_invoicing_recurrences")
    assert tool["annotations"]["readOnlyHint"] and not tool["annotations"]["destructiveHint"]
    assert set(tool["inputSchema"]["properties"]) == {"co"}
