from datetime import date

import pytest
from test_invoicing import draft, setup_invoice as setup_invoice
from test_issued import OTHER_IBAN, _credit, _st, _stored, sec as sec
from ubl_fake import ubl

from fiscalm import companies
from fiscalm.api import state as api
from fiscalm.bank import coverage, statements
from fiscalm.invoicing import engine as i
from fiscalm.mail import reminders as r

TODAY = date(2026, 12, 7)


def cover(L, last=TODAY, first=date(2026, 10, 1)):
    """A stored statement covering every NORD current account from `first` to `last` (bank.coverage)."""
    L.db.execute("DELETE FROM documents WHERE kind='statement'")
    L.db.commit()
    L.add_document("coverage", "NORD", "2026-10", "statement", "NORD/2026-10/statement.pdf")
    days = [first.isoformat(), last.isoformat(), True]
    coverage.record(
        L, "coverage", {companies.normalize_iban(a.iban): days for a in companies.get("NORD", L=L).current_accounts}
    )


@pytest.fixture
def reminder(setup_invoice, home, mail_account):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    (home / "state" / "LIVE").touch()
    cover(L)
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


# A person sends the text, so statements behind (monthly ones, mid-month) or missing warn on the card instead of
# hiding the reminder; current ones (up to STATEMENT_MAX_AGE_DAYS behind) say nothing.
@pytest.mark.parametrize(
    "last, warning",
    [
        (TODAY, None),
        (date(2026, 11, 30), None),
        (date(2026, 10, 31), r.STALE.format(date="31.10.2026")),
        (None, r.NO_STATEMENTS),
    ],
)
def test_statements_behind_or_missing_warn_on_the_text(reminder, last, warning):
    L, iid = reminder
    if last:
        cover(L, last)
    else:
        L.db.execute("DELETE FROM documents WHERE kind='statement'")
        L.db.commit()
    tasks = r.tasks(L, "NORD", TODAY, api._issued(L, "NORD", TODAY, True))
    assert [(t["id"], t["warning"]) for t in tasks] == [(i.import_id("local", iid), warning)]


@pytest.mark.parametrize("known", [False, True])
def test_reminder_bank_reads_do_not_scale_with_invoices(reminder, monkeypatch, known):
    L, iid = reminder
    invoice = i.invoice(L, iid, company="NORD")
    st = _st(_credit("121.00", TODAY, counterparty=invoice["data"]["client"]["name"]))
    L.record_statement(st)
    if known:
        L.db.execute("UPDATE issued SET collected_from_iban=? WHERE company='NORD'", (st.txs[0].counterparty_iban,))
        L.db.commit()
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
    accounts = len(companies.get("NORD", L=L).current_accounts)  # bank.coverage reads each account once
    assert counts[0] == counts[1] <= 3 + accounts


def _client_credit(L, iid, day=date(2026, 11, 10), **kw):
    client = i.invoice(L, iid, company="NORD")["data"]["client"]["name"]
    return _credit("50.00", day, **{"counterparty": client} | kw)


def _reserve(L, tx, what):
    L.db.execute("INSERT INTO settled_tx VALUES(?,?,?)", (statements.tx_key(tx), what, "x"))
    if what.startswith("twin "):  # brought by the other source, the bank feed
        L.db.execute("UPDATE statement_txs SET source='ob' WHERE tx_key=?", (statements.tx_key(tx),))
    L.db.commit()


# A repeat client's credit already settled to another invoice says nothing about this one; a conflict, a twin
# whose copy explains nothing or one without its details may still be this payment.
@pytest.mark.parametrize(
    "case, reminded",
    [
        ("unexplained", False),
        ("explained", True),
        ("conflict", False),
        ("named_on_a_later_read", False),  # first recorded without its payer, then read again naming the client
        ("no_details", False),
    ],
)
def test_only_an_unexplained_client_credit_refuses_text(reminder, monkeypatch, case, reminded):
    L, iid = reminder
    credit = _client_credit(L, iid)
    if case == "named_on_a_later_read":
        blank = _client_credit(L, iid, counterparty="", iban="", desc=credit.description)
        assert statements.tx_key(blank) == statements.tx_key(credit)
        L.record_statement(_st(blank))
    L.record_statement(_st(credit))
    if case in ("explained", "conflict"):
        _reserve(L, credit, "issued other" if case == "explained" else "bank reference conflict")
    if case == "no_details":
        L.db.execute("UPDATE statement_txs SET tx=NULL")
        L.db.commit()
    rows = api._issued(L, "NORD", TODAY, True)
    assert [t["id"] for t in r.tasks(L, "NORD", TODAY, rows)] == ([i.import_id("local", iid)] if reminded else [])


# The statement names the client (a twin); the other copies of the same payment name nobody. The twin is excused
# only when every line it could be a copy of is explained, one explained payment for one twin, a duplicate never
# counting as a second payment.
@pytest.mark.parametrize(
    "case, reminded",
    [
        ("twin", False),
        ("twin_of_explained", True),
        ("twin_and_explained_line_of_one_source", False),  # two lines of one source are two payments
        ("two_payers_one_explained", False),  # another client's twin wants the same explained payment
        ("two_twins_one_explained", False),
        ("explained_and_unexplained_copy", False),
        ("two_twins_original_and_its_duplicate", False),
        ("duplicate", False),
    ],
)
def test_every_copy_of_an_unexplained_payment_counts(reminder, case, reminded):
    L, iid = reminder
    named = _client_credit(L, iid)
    second = _client_credit(L, iid, day=date(2026, 11, 12), desc="Incasare 2")
    blank = _client_credit(L, iid, day=date(2026, 11, 11), counterparty="", iban="", desc="Incasare")
    settled = _client_credit(L, iid, day=date(2026, 11, 9), counterparty="", iban="", desc="Incasare 0")
    other = _client_credit(
        L, iid, day=date(2026, 11, 8), counterparty="OTHER CLIENT SRL", iban=OTHER_IBAN, desc="Incasare 3"
    )
    extra = {
        "two_payers_one_explained": [other],
        "two_twins_one_explained": [second],
        "explained_and_unexplained_copy": [settled],
        "two_twins_original_and_its_duplicate": [second, settled],
    }.get(case, [])
    L.record_statement(_st(named, blank, *extra))
    _reserve(L, named, "duplicate:" + statements.tx_key(blank) if case == "duplicate" else "twin feed")
    for twin in (second, other):
        if twin in extra:
            _reserve(L, twin, "twin feed")
    if case in (
        "twin_of_explained",
        "two_twins_one_explained",
        "twin_and_explained_line_of_one_source",
        "two_payers_one_explained",
    ):
        _reserve(L, blank, "issued other")
    if case == "twin_and_explained_line_of_one_source":
        L.db.execute("UPDATE statement_txs SET source='ob'")
        L.db.commit()
    if settled in extra:  # a settled line the twin could be a copy of; `blank` is unexplained, or its duplicate
        _reserve(L, settled, "issued other")
        if case == "two_twins_original_and_its_duplicate":
            _reserve(L, blank, "duplicate:" + statements.tx_key(settled))
    rows = api._issued(L, "NORD", TODAY, True)
    assert bool(r.tasks(L, "NORD", TODAY, rows)) is reminded


@pytest.mark.parametrize(
    "invoice, change, reminded",
    [
        (dict(), None, True),
        (dict(), "storno", False),  # a credit note against it: no longer the full amount owed
        (dict(), "linked", False),  # the firm's e-Factura directory links to another firm's
        (dict(supplier_cui="RO12345678"), None, False),  # another supplier's invoice under this firm's SPV
        (dict(supplier_cui=""), None, False),  # nothing says the supplier is this firm (stored unflagged)
        (dict(supplier_cui="DE11111111"), None, False),  # this firm's digits, another country's VAT id
        (dict(client="", client_cui=""), None, False),  # no buyer to tell a payment from
        (dict(client="ABC SRL", client_cui=""), None, False),  # a name too short for the payer matcher
        (dict(client="ABC SRL", client_cui="RO12345"), None, False),  # and a CUI too short for it
        (dict(), "foreign", False),  # this firm's CUI digits from a supplier in another country
    ],
)
def test_spv_synced_invoice_gets_reminder_text(reminder, home, sec, monkeypatch, invoice, change, reminded):
    L, _ = reminder
    L.db.execute("DELETE FROM issued WHERE company='NORD'")  # only the invoice synced from SPV is left
    L.db.commit()
    blobs = {"9001": ubl(**dict(number="EXT 7", issue="2026-11-01", due="2026-11-08") | invoice)}
    if change == "foreign":  # the supplier's address in Germany, its id the firm's bare digits
        blobs["9001"] = (
            blobs["9001"]
            .replace(
                b"<cbc:CompanyID>RO11111111</cbc:CompanyID>",
                b"<cbc:CompanyID>11111111</cbc:CompanyID>",
            )
            .replace(
                b"<cac:AccountingSupplierParty><cac:Party>",
                b"<cac:AccountingSupplierParty><cac:Party><cac:PostalAddress><cac:Country><cbc:IdentificationCode>DE"
                b"</cbc:IdentificationCode></cac:Country></cac:PostalAddress>",
            )
        )
    if change == "storno":
        blobs["9002"] = ubl(root="CreditNote", number="EXT 9", total="190.00", ref="EXT 7", issue="2026-11-05")
    blobs["9003"] = ubl(number="EXT 8", issue="0001-01-08", due="")  # an impossible window refuses only itself
    _stored(L, monkeypatch, **blobs)
    if change == "linked":
        (home / "efactura" / "NORD").rename(home / "efactura" / "OTHER")
        (home / "efactura" / "NORD").symlink_to(home / "efactura" / "OTHER")
    today = date(2026, 11, 18)
    tasks = r.tasks(L, "NORD", today, api._issued(L, "NORD", today, True))
    assert [t["id"] for t in tasks] == (["9001"] if reminded else [])
