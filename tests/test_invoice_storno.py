import io
import zipfile
from decimal import Decimal

import pytest
from test_invoicing import DATA, LINE, PARTY, PDF_TEXT, draft, next_number, race, setup_invoice as setup_invoice

from fiscalm import companies
from fiscalm.api import state as api
from fiscalm.efactura.inbox import parse_ubl
from fiscalm.invoicing import engine as i, issued, pdf as invoice_pdf, ubl
from fiscalm.pdftext import pdf_text


def issue(setup_invoice, data=None):
    return i.issue(setup_invoice[0], draft(setup_invoice, data), DATA["issue_date"], company="NORD", version=1)["id"]


def test_partial_then_full(setup_invoice, tmp_path):
    L, _ = setup_invoice
    iid = issue(setup_invoice, DATA | dict(lines=[LINE, LINE | dict(unit_price="200")]))
    before = i.invoice(L, iid, company="NORD")
    first = i.storno(L, iid, "2026-11-01", "Return test", [1], company="NORD")
    assert first["data"]["totals"]["total"] == "-121.00"
    assert first["number"] == 43
    assert i.invoice(L, iid, company="NORD")["status"] == "issued"
    parsed = parse_ubl(ubl.generate(L, first["id"], company="NORD"))
    assert (parsed.total, parsed.billing_ref) == ("-121.00", "TEST-42")
    path = tmp_path / "storno.pdf"
    path.write_bytes(invoice_pdf.generate(L, first["id"], company="NORD"))
    assert "Stornare pentru factura TEST-42 din 31.10.2026" in pdf_text(path)
    with pytest.raises(i.InvoiceError, match="deja stornate"):
        i.storno(L, iid, "2026-11-01", "Again", [1], company="NORD")
    second = i.storno(L, iid, "2026-11-01", "Return rest", [2], company="NORD")
    assert second["number"] == 44
    original = i.invoice(L, iid, company="NORD")
    assert original["status"] == "storno"
    assert original["data"] == before["data"] and original["lines"] == before["lines"]
    with pytest.raises(i.InvoiceError):
        i.storno(L, first["id"], "2026-11-02", "No", company="NORD")


@pytest.mark.parametrize("positions", [[], [0], [2], [1, 1], [True]])
def test_bad_positions(setup_invoice, positions):
    L, _ = setup_invoice
    iid = issue(setup_invoice)
    with pytest.raises(i.InvoiceError):
        i.storno(L, iid, "2026-11-01", "Reason", positions, company="NORD")
    assert i.storno(L, iid, "2026-11-01", "Reason", company="NORD")["number"] == 43


def test_storno_refusals(setup_invoice):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    with pytest.raises(i.InvoiceError):
        i.storno(L, iid, "2026-11-01", "Reason", company="NORD")
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    with pytest.raises(i.InvoiceError):
        i.storno(L, iid, "2026-11-01", "", company="NORD")
    with pytest.raises(i.InvoiceError):
        i.storno(L, iid, "2026-10-30", "Reason", company="NORD")


def test_reversal_race(setup_invoice):
    L, _ = setup_invoice
    iid = issue(setup_invoice)
    won, refused = race(L.path, lambda other, _: i.storno(other, iid, "2026-11-01", "Reason", company="NORD")["number"])
    assert won == [43] and len(refused) == 1
    assert next_number(L) == 44


def test_partial_rounding_sums_to_original(setup_invoice):
    """Ten lines whose own VAT rounds up: the reversals never return more VAT than the original charged."""
    L, _ = setup_invoice
    iid = issue(setup_invoice, DATA | dict(lines=[LINE | dict(unit_price=".03")] * 10))
    vat = Decimal(i.invoice(L, iid, company="NORD")["data"]["totals"]["vat"])
    reversals = [i.storno(L, iid, "2026-11-01", "Return", [n], company="NORD") for n in range(1, 11)]
    for reversal in reversals:
        assert ubl.validate(ubl.generate(L, reversal["id"], company="NORD")) == []
    assert sum(Decimal(r["data"]["totals"]["vat"]) for r in reversals) == -vat
    assert all(Decimal(r["data"]["totals"]["vat"]) <= 0 for r in reversals)
    with pytest.raises(i.InvoiceError):
        i.storno(L, reversals[0]["id"], "2026-11-01", "Wrong company", company="STUDIO9")


def test_storno_series_order_and_printability(setup_invoice):
    L, _ = setup_invoice
    iid = issue(setup_invoice)
    later = draft(setup_invoice, DATA | dict(issue_date="2026-11-02"))
    i.issue(L, later, "2026-11-02", company="NORD", version=1)
    with pytest.raises(i.InvoiceError, match="ultimei facturi"):
        i.storno(L, iid, "2026-11-01", "Return", company="NORD")
    with pytest.raises(i.InvoiceError) as refused:
        i.storno(L, iid, "2026-11-02", "中文", company="NORD")
    assert str(refused.value) == PDF_TEXT
    assert next_number(L) == 44


def test_storno_small_quantity_plain_decimal(setup_invoice):
    L, _ = setup_invoice
    iid = issue(setup_invoice, DATA | dict(lines=[LINE | dict(quantity="0.0000001", unit_price="10000000")]))
    assert i.storno(L, iid, "2026-11-01", "Return", company="NORD")["lines"][0]["quantity"] == "-0.0000001"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_partial_storno_sync_and_duplicate_keep_local_credit(setup_invoice, monkeypatch, ambiguous):
    L, cid = setup_invoice
    cui = companies.get("NORD").cui
    i.save_seller(L, "NORD", PARTY | dict(cui=cui, vat_id="RO" + cui))
    iid = issue(setup_invoice, DATA | dict(lines=[LINE, LINE | dict(unit_price="200")]))
    rid = i.storno(L, iid, "2026-11-01", "Retur", [1], company="NORD")["id"]

    def row():
        return next(r for r in api.state(L, "NORD")["issued"] if r["local_id"] == iid)

    assert (row()["credited"], row()["owed"]) == ("121.00", "242.00")

    def sync(local_id, mid):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as z:
            z.writestr("invoice.xml", ubl.generate(L, local_id, company="NORD"))
        issued.store(L, "NORD", dict(id=mid, data_creare="2026-11-01"), raw.getvalue())

    sync(iid, "123")
    if ambiguous:
        links = issued.credit_links
        monkeypatch.setattr(issued, "credit_links", lambda *a: links(*a) | {"124": None})
    sync(rid, "124")
    sync(rid, "125")
    assert (row()["credited"], row()["owed"]) == ("121.00", "242.00")
    assert len(api.state(L, "NORD")["issued"]) == 2
