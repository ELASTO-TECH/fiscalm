from datetime import date

import pytest
from test_invoicing import draft, setup_invoice as setup_invoice
from test_issued import _credit, _st

from fiscalm import companies
from fiscalm.api import state as api
from fiscalm.bank import statements
from fiscalm.invoicing import engine as i
from fiscalm.mail import reminders as r

TODAY = date(2026, 12, 7)


def cover_accounts(st):
    from fiscalm import companies
    from fiscalm.bank import statements

    st.period_start = date(2026, 10, 1)
    txs = st.txs
    st.accounts = [
        statements.Account(iban, currency, txs if currency == "RON" else [])
        for currency, iban in companies.get("NORD").ibans.items()
    ]


@pytest.fixture
def reminder(setup_invoice, home, monkeypatch, mail_account):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    (home / "state" / "LIVE").touch()
    from test_issued import _st

    from fiscalm.bank import statements

    st = _st()
    cover_accounts(st)
    st.period_end = TODAY
    statement = home / "NORD" / "2026-10" / "statement.pdf"
    statement.parent.mkdir(parents=True, exist_ok=True)
    statement.write_bytes(b"%PDF-synthetic-" + iid.encode())
    L.add_document("coverage", "NORD", "2026-10", "statement", str(statement))
    monkeypatch.setattr(statements, "parse", lambda *args: st)
    return L, iid


@pytest.mark.parametrize("live", [False, True])
def test_reminder_is_copyable_text_without_mail_or_ledger_writes(reminder, home, live):
    L, iid = reminder
    if not live:
        (home / "state" / "LIVE").unlink()
    before = L.db.total_changes
    tasks = r.tasks(L, "NORD", TODAY, api._issued(L, "NORD", TODAY, live))
    assert len(tasks) == 1 and tasks[0]["id"] == i.import_id("local", iid)
    assert "TEST-42" in tasks[0]["subject"] and "121,00 RON" in tasks[0]["body"]
    assert L.db.total_changes == before


@pytest.mark.parametrize("change", ["paid", "partial", "other_company", "fresh"])
def test_reminder_keeps_money_and_company_refusals(reminder, change):
    L, iid = reminder
    co = "STUDIO9" if change == "other_company" else "NORD"
    today = date(2026, 11, 1) if change == "fresh" else TODAY
    rows = api._issued(L, co, today, True)
    if change == "partial":
        rows = [row | {"owed": "120.00"} for row in rows]
    if change == "paid":
        rows = [row | {"status": "collected"} for row in rows]
    with pytest.raises(r.Refused):
        r.eligible(L, co, iid, today, rows=rows)


def test_missing_or_stale_statement_refuses_text(reminder):
    L, iid = reminder
    L.db.execute("DELETE FROM documents WHERE kind='statement'")
    with pytest.raises(r.Refused, match=r.STATEMENTS):
        r.eligible(L, "NORD", iid, TODAY)


@pytest.mark.parametrize("known", [False, True])
def test_reminder_bank_reads_do_not_scale_with_invoices(reminder, monkeypatch, known):
    L, iid = reminder
    invoice = i.invoice(L, iid, company="NORD")
    st = _st(_credit("121.00", TODAY, counterparty=invoice["data"]["client"]["name"]))
    cover_accounts(st)
    st.period_end = TODAY
    if known:
        L.db.execute("UPDATE issued SET collected_from_iban=? WHERE company='NORD'", (st.txs[0].counterparty_iban,))
        L.db.commit()
    monkeypatch.setattr(statements, "parse", lambda *args: st)
    rows = api._issued(L, "NORD", TODAY, True)
    counts = []
    for n in (10, 100):
        reads = api._StateReads(L, companies.all(L))
        queries = []
        L.db.set_trace_callback(queries.append)
        try:
            # A possible credit must refuse every reminder, regardless of batch size.
            assert r.tasks(reads, "NORD", TODAY, rows * n, invoices={iid: invoice}) == []
        finally:
            L.db.set_trace_callback(None)
        counts.append(len(queries))
    assert counts[0] == counts[1] <= 3
