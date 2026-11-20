"""Payment file formats: a payment is prepared in the format of the bank of the account it debits, a bank without one
gets no file (held), and a format added from outside works through per-file download and import. Synthetic data."""

import dataclasses
from datetime import date

import pytest
from firms import set_firm
from payment_cases import D0, _op, download_batch

from fiscalm import companies
from fiscalm.api import state as api
from fiscalm.bank import catalogue as banks
from fiscalm.payments import engine as pay, files as payfiles
from fiscalm.payments.orders import PaymentFileError

pytestmark = pytest.mark.usefixtures("live")  # payment files are made only in Live

ING = "RO00INGB0000000000000009"
OUTSIDE = "RO00ZZZZ0000000000000009"  # a bank code that isn't in the catalogue


def _paying(monkeypatch, iban, bank, currency="RON"):
    nord = companies.get("NORD")
    accounts = tuple(dataclasses.replace(a, pays=False) for a in nord.accounts) + (
        companies.Account(iban, currency, bank, pays=True),
    )
    set_firm("NORD", dataclasses.replace(nord, accounts=accounts))


def test_every_format_a_bank_names_is_registered_and_bt_uses_bt_go():
    assert all(b.payment_file in payfiles.FORMATS for b in banks.CATALOGUE if b.payment_file)
    fmt = payfiles.for_account(companies.Account("RO00BTRL0000000000000001", "RON", "bt"))
    assert fmt and fmt.id == "btgo-csv" and fmt.app == "BT Go" and fmt.extension == ".csv"


@pytest.mark.parametrize(
    "account,why",
    [
        (None, "no paying account"),
        (companies.Account("RO00BTRL0000000000000001", "EUR", "bt"), "can't debit a EUR account"),
        (companies.Account(ING, "RON", "ing"), "(ING Bank)"),
        (companies.Account(OUTSIDE, "RON", ""), "(not in the catalogue)"),
    ],
)
def test_an_account_without_a_format_gets_none_and_a_reason(account, why):
    assert payfiles.for_account(account) is None
    assert why in payfiles.why_not(account)


# ------------------------------------------------------------------ a format added from outside


def _fake_build(items, value_date):
    if any(i.company != items[0].company for i in items):
        raise PaymentFileError("one company per file")
    lines = [f"{value_date.isoformat()}|{i.source_iban}|{i.payee_iban}|{i.amount:.2f}" for i in items]
    return ("FAKE\n" + "\n".join(lines) + "\n").encode()


def _fake_value_date(data):
    line = data.decode().split("\n")[1]
    return date.fromisoformat(line.split("|")[0])


FAKE = payfiles.PaymentFormat(
    "fake-xml",
    "Banca Test Online",
    ".xml",
    ("RON",),
    _fake_build,
    lambda data, co: [] if data.startswith(b"FAKE\n") else ["not a fake file"],
    _fake_value_date,
)


@pytest.fixture
def ing_has_a_format(monkeypatch):
    monkeypatch.setitem(payfiles.FORMATS, FAKE.id, FAKE)
    monkeypatch.setattr(payfiles, "EXTENSIONS", (".csv", ".xml"))
    monkeypatch.setitem(banks.BY_ID, "ing", dataclasses.replace(banks.BY_ID["ing"], payment_file=FAKE.id))
    _paying(monkeypatch, ING, "ing")


def _to_xml_bank(monkeypatch):
    monkeypatch.setitem(payfiles.FORMATS, FAKE.id, FAKE)
    monkeypatch.setattr(payfiles, "EXTENSIONS", (".csv", ".xml"))
    monkeypatch.setitem(banks.BY_ID, "ing", dataclasses.replace(banks.BY_ID["ing"], payment_file=FAKE.id))
    _paying(monkeypatch, ING, "ing")


@pytest.mark.parametrize("account", [(OUTSIDE, ""), (ING, "ing")])
def test_unsupported_account_never_writes_a_bank_file(ledger, monkeypatch, account):
    _paying(monkeypatch, *account)
    item = _op(ledger, 1)
    with pytest.raises(PaymentFileError):
        download_batch(ledger, [item])
    row = ledger.payment(item)
    assert row["status"] == "pending" and row["source_iban"] is None
    assert not pay.named_path(ledger.payment_file(row["file_id"])).exists()


def test_registered_format_download_import_and_orphan_guard(ledger, home, ing_has_a_format):
    item = _op(ledger, 1)
    ident, snapshot = download_batch(ledger, [item])
    path = pay.named_path(ledger.payment_file(ident))
    assert path.suffix == ".xml" and path.read_bytes().startswith(b"FAKE\n")
    assert f"|{ING}|" in path.read_text()
    assert pay.file_downloads(ledger, ident)[0]["value_date"] == D0.isoformat()
    assert api.state(ledger, "NORD")["files"][0]["import_in"] == "Banca Test Online"
    before = path.read_bytes()
    pay.refresh_to_pay(ledger, D0)
    assert path.read_bytes() == before
    assert pay.import_file(ledger, ident, "NORD", snapshot["download_id"], D0)
    archive = pay.named_path(ledger.payment_file(ident))
    assert archive.suffix == ".xml" and archive.read_bytes() == before and not path.exists()
    pay.to_pay_dir().mkdir(parents=True, exist_ok=True)
    (pay.to_pay_dir() / "payments-NORD-stray.xml").write_text("x")
    assert pay.bank_change_blocked(ledger, "NORD")


def test_existing_file_keeps_its_bank_after_a_paying_account_change(ledger, monkeypatch):
    first = _op(ledger, 1)
    one, snapshot = download_batch(ledger, [first])
    original = pay.named_path(ledger.payment_file(one))
    before = original.read_bytes()
    _to_xml_bank(monkeypatch)
    second = _op(ledger, 2, "200.00")
    two, other = download_batch(ledger, [second])
    summaries = {f["id"]: f for f in api.state(ledger, "NORD")["files"]}
    assert summaries[one]["import_in"] == "BT Go"
    assert summaries[two]["import_in"] == "Banca Test Online"
    assert api.payment_file_detail(ledger, one)["import_in"] == "BT Go"
    assert original.read_bytes() == before
    assert pay.import_file(ledger, one, "NORD", snapshot["download_id"], D0)
    assert pay.import_file(ledger, two, "NORD", other["download_id"], D0)
    assert pay.named_path(ledger.payment_file(one)).suffix == ".csv"
    assert pay.named_path(ledger.payment_file(two)).suffix == ".xml"


def test_restoring_a_missing_file_keeps_its_original_format(ledger, monkeypatch):
    item = _op(ledger, 1)
    ident, _ = download_batch(ledger, [item])
    path = pay.named_path(ledger.payment_file(ident))
    path.unlink()
    _to_xml_bank(monkeypatch)
    expected = pay.version([item])
    with pytest.raises(PaymentFileError):
        pay.resolve_missing_file(ledger, ident, "NORD", "stale", D0, imported=False)
    assert not path.exists()
    assert pay.resolve_missing_file(ledger, ident, "NORD", expected, D0, imported=False)
    assert path.suffix == ".csv" and path.read_bytes().startswith(b"OrderNumber")
    assert ING.encode() not in path.read_bytes()


@pytest.mark.parametrize("source", ["none", "connection", "earlier_statement"])
def test_statement_hold_requires_a_statement_source(ledger, source):
    item = _op(ledger, 1)
    if source == "connection":
        with ledger.tx() as db:
            db.execute(
                "INSERT INTO bank_connections(id,company,bank,provider,accounts,valid_until,created_at) VALUES ('bc-test','NORD','bt','enablebanking','[]','2026-12-31','2026-10-01')"
            )
    if source == "earlier_statement":
        ledger.add_document("prior", "NORD", "2026-08", "statement", "prior.xml")
    if source == "none":
        download_batch(ledger, [item])
        assert ledger.payment(item)["status"] == "exported"
    else:
        with pytest.raises(PaymentFileError):
            download_batch(ledger, [item])
        assert ledger.payment(item)["status"] == "pending"
