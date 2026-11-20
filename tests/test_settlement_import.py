"""Settlement report parsing: CSV and XLSX refuse anything but plain, bounded, exact tables; uploads learn a format."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date

import pytest
from test_settlements import collect, setup, statement

from fiscalm import companies
from fiscalm.api import actions
from fiscalm.settlements import engine as settlements, upload as bank_upload
from fiscalm.settlements.reports import SOURCES, STRIPE, ReportError, parse, table

UPLOAD = dict(report_date="2026-10-10", today=date(2026, 10, 12))
NETOPIA = dict(
    source="netopia",
    name="Synthetic Processor SRL",
    currency="RON",
    mapping=dict(reference="Ref", gross="Gross", fee="Fee"),
)


NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def workbook(rows="", *, sheet=None, strings=None):
    """A one-worksheet XLSX holding `rows` (or the raw `sheet` XML), with optional shared strings."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        if strings is not None:
            z.writestr("xl/sharedStrings.xml", f"<sst {NS}>{strings}</sst>")
        z.writestr("xl/worksheets/sheet1.xml", sheet or f"<worksheet {NS}><sheetData>{rows}</sheetData></worksheet>")
    return b.getvalue()


@pytest.mark.parametrize("source", [s for s in SOURCES if s != "stripe"])
def test_mapped_sources(source):
    assert (
        parse(
            b"Factura,Suma,Taxa\nTEST-1,100.00,2.00\n",
            "csv",
            source,
            {"reference": "Factura", "gross": "Suma", "fee": "Taxa"},
        )[0]["gross"]
        == "100.00"
    )


def test_stripe():
    data = b"invoice_number,gross,fee,net,currency,automatic_payout_id,reporting_category\nTEST-1,100.00,2.00,98.00,ron,po_test,charge\n"
    assert parse(data, "csv", "stripe")[0]["fee"] == "2.00"
    for bad in (data.replace(b"98.00", b"98.01"), data.replace(b"charge", b"refund"), data.replace(b"100.00", b"NaN")):
        with pytest.raises(ReportError):
            parse(bad, "csv", "stripe")


def test_xlsx_sparse_and_formula():
    header = '<row><c r="A1" t="inlineStr"><is><t>Ref</t></is></c></row>'
    assert table(workbook(header + '<row><c r="C2"><v>12</v></c></row>'), "xlsx") == [["Ref"], ["", "", "12"]]
    with pytest.raises(ReportError):
        table(workbook(header + '<row><c r="A2"><f>1+1</f><v>2</v></c></row>'), "xlsx")


@pytest.mark.parametrize("data", [b"a,a\n1,2", b"a,b\n1", b"a,b\n1,2,3"])
def test_bad_tables(data):
    with pytest.raises(ReportError):
        parse(data, "csv", "fan", {"reference": "a", "gross": "b", "fee": "b"})


@pytest.mark.parametrize("source", ["stripe", "fan"])
def test_xlsx_source_happy(source):
    """XLSX reads the same for the Stripe preset and for a mapped source (every mapped source parses alike)."""
    headers = ["invoice_number", "gross", "fee", "net", "currency", "automatic_payout_id", "reporting_category"]
    values = ["TEST-1", "100.00", "2.00", "98.00", "ron", "po_synthetic", "charge"]
    rows = "".join(
        "<row>"
        + "".join(f'<c r="{chr(65 + i)}{n}" t="inlineStr"><is><t>{v}</t></is></c>' for i, v in enumerate(row))
        + "</row>"
        for n, row in enumerate([headers, values], 1)
    )
    mapping = (
        None if source == "stripe" else {"reference": "invoice_number", "gross": "gross", "fee": "fee", "net": "net"}
    )
    assert parse(workbook(rows), "xlsx", source, mapping)[0]["reference"] == "TEST-1"


def test_xlsx_dtd_zip_duplicates_and_size():
    for contents in ("<!DOCTYPE x><worksheet/>", '<!ENTITY x "y"><worksheet/>'):
        with pytest.raises(ReportError):
            table(workbook(sheet=contents), "xlsx")
    with pytest.raises(ReportError):
        table(b"x" * (4 * 1024 * 1024 + 1), "csv")


@pytest.mark.parametrize("mapping", [[], {"reference": [], "gross": "a", "fee": "b"}])
def test_malformed_mapping(mapping):
    with pytest.raises(ReportError):
        parse(b"a,b\n1,2", "csv", "fan", mapping)


def test_bad_delimiter_and_negative_string_index():
    with pytest.raises(ReportError):
        table(b"a,b\n1,2", "csv", "abc")
    with pytest.raises(ReportError):
        table(
            workbook(
                '<row><c r="A1" t="s"><v>-1</v></c></row><row><c r="A2"><v>1</v></c></row>',
                strings="<si><t>Ref</t></si>",
            ),
            "xlsx",
        )


@pytest.mark.parametrize("value", ["=SUM(1)", " +cmd", "@ref", "-1"])
def test_csv_formula_cells_refuse(value):
    with pytest.raises(ReportError):
        table(f"Ref,Gross,Fee\n{value},100.00,2.00\n".encode(), "csv")


def test_uploaded_format_belongs_to_partner_and_is_reused(ledger):
    setup(ledger)
    data = b"Ref,Gross,Fee\nTEST-1,100.00,2.00\n"
    with pytest.raises(bank_upload.Refused) as ask:
        bank_upload.accept_report(ledger, "NORD", data, "text/csv", **UPLOAD)
    assert ask.value.status == 409 and ask.value.extra["known"] == []
    result = bank_upload.accept_report(ledger, "NORD", data, "text/csv", description=NETOPIA, **UPLOAD)
    partner = ledger.db.execute("SELECT partner FROM partner_settlement_formats").fetchone()[0]
    assert partner == "NORD.n-synthetic-processor-srl"
    assert next(r for r in settlements.state(ledger, "NORD") if r["id"] == result["id"])["partner"] == partner
    with pytest.raises(bank_upload.Refused) as ask:
        bank_upload.accept_report(ledger, "NORD", data, "text/csv", **UPLOAD)
    assert ask.value.extra["known"] == [dict(id=partner, name=NETOPIA["name"], source="netopia", company_cui_column="")]
    assert (
        bank_upload.accept_report(ledger, "NORD", data, "text/csv", partner=partner, **UPLOAD)["status"] == "duplicate"
    )
    with pytest.raises(bank_upload.Refused):
        bank_upload.accept_report(ledger, "STUDIO9", data, "text/csv", partner=partner, **UPLOAD)
    assert ask.value.extra["payer"] == partner
    signature = ledger.db.execute("SELECT signature FROM partner_settlement_formats").fetchone()[0]
    assert actions.act("settlement-format-delete", "NORD", dict(partner=partner, signature=signature))["ok"]
    with pytest.raises(bank_upload.Refused) as ask:
        bank_upload.accept_report(ledger, "NORD", data, "text/csv", **UPLOAD)
    assert ask.value.extra["known"] == [] and ask.value.extra["payer"] == ""


@pytest.mark.parametrize(
    "dates,day",
    [
        (("09.10.2026", "9/10/2026"), "2026-10-09"),
        (("2026-10-09", "2026-10-10"), ""),
        (("ieri", "ieri"), ""),
        (("2026-10-09 14:30", "2026-10-09T08:00:00"), "2026-10-09"),
        (("2026-10-0999", "2026-10-09"), ""),
    ],
)
@pytest.mark.parametrize("orders", [False, True])
def test_preview_guesses_columns_and_date_only_to_prefill(ledger, dates, day, orders):
    if orders:
        ledger.db.execute(
            "INSERT INTO commerce_orders VALUES('o1','NORD','shop','1','','{}','d','c',1,'new',NULL,NULL)"
        )
    rows = "".join(f"100{n},TEST-{n},100.00,2.00,{d}\n" for n, d in enumerate(dates))
    data = f"AWB,Factură,RAMBURS,Comision,Data plății\n{rows}".encode()
    with pytest.raises(bank_upload.Refused) as ask:
        bank_upload.accept_report(ledger, "NORD", data, "text/csv", **UPLOAD)
    guess = dict(reference="Factură", gross="RAMBURS", fee="Comision") | (dict(awb="AWB") if orders else {})
    assert {k: ask.value.extra[k] for k in ("guess", "date", "orders")} == dict(guess=guess, date=day, orders=orders)
    assert ledger.db.execute("SELECT count(*) FROM settlement_reports").fetchone()[0] == 0


@pytest.mark.parametrize("second,currency", [("ron", "RON"), ("eur", "")])
def test_preview_prefills_stripe_currency_and_saved_payers(ledger, second, currency):
    setup(ledger)
    bank_upload.accept_report(
        ledger, "NORD", b"Ref,Gross,Fee\nTEST-1,100.00,2.00\n", "text/csv", description=NETOPIA, **UPLOAD
    )
    ledger.add_issued(
        dict(
            message_id="eur-1",
            company="NORD",
            number="EUR-1",
            issue_date="2026-10-01",
            currency="eur",
            total="10.00",
            doc_type="invoice",
            status="open",
            raw_path="",
        )
    )
    rows = "".join(f"TEST-{n},100.00,2.00,98.00,{c},po_test,charge\n" for n, c in enumerate(("ron", second)))
    data = f"invoice_number,gross,fee,net,currency,automatic_payout_id,reporting_category\n{rows}".encode()
    with pytest.raises(bank_upload.Refused) as ask:
        bank_upload.accept_report(ledger, "NORD", data, "text/csv", **UPLOAD)
    payer = dict(source="netopia", name=NETOPIA["name"], cui="", currency="RON", courier="")
    assert {k: ask.value.extra[k] for k in ("source", "guess", "currency", "currencies", "payers")} == dict(
        source="stripe", guess=STRIPE, currency=currency, currencies=["RON", "EUR"], payers=[payer]
    )


@pytest.mark.parametrize(
    "bad", [b"Ref,Gross,Fee\nTEST-1,NaN,2.00\n", b"Ref,Gross,Fee\n=CMD,100.00,2.00\n", b"\xff,\xff\n1,2\n"]
)
def test_refused_upload_saves_no_partner_format(ledger, bad):
    with pytest.raises(bank_upload.Refused):
        bank_upload.accept_report(
            ledger, "NORD", bad, "text/csv", description={k: v for k, v in NETOPIA.items() if k != "currency"}, **UPLOAD
        )
    assert ledger.db.execute("SELECT count(*) FROM partner_settlement_formats").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
@pytest.mark.parametrize("value", ["same", "empty", "foreign", "unknown", "invalid", "unmapped"])
def test_uploaded_company_cui_refuses_whole_report(ledger, kind, value):
    setup(ledger)
    ledger.db.execute("DELETE FROM settlement_reports")
    for n in (1, 2):
        ledger.add_issued(
            dict(
                message_id=f"foreign-{n}",
                company="STUDIO9",
                number=f"TEST-{n}",
                issue_date="2026-10-01",
                currency="RON",
                total="100.00",
                doc_type="invoice",
                status="open",
                raw_path="",
            )
        )
    cui = companies.get("NORD", L=ledger).cui
    other = {
        "same": f" ro 0{cui} ",
        "empty": "",
        "foreign": companies.get("STUDIO9", L=ledger).cui,
        "unknown": "99999923",
        "invalid": "NORD",
        "unmapped": "99999923",
    }[value]
    rows = [["Ref", "Gross", "Fee", "CUI"], ["TEST-1", "100.00", "2.00", cui], ["TEST-2", "100.00", "2.00", other]]
    data = ("\n".join(",".join(row) for row in rows) + "\n").encode()
    if kind == "xlsx":
        data = workbook(
            "".join(
                "<row>"
                + "".join(
                    f'<c r="{chr(65 + i)}{n}" t="inlineStr"><is><t>{v}</t></is></c>' for i, v in enumerate(row) if v
                )
                + "</row>"
                for n, row in enumerate(rows, 1)
            )
        )
    description = NETOPIA | {
        "name": "Synthetic Courier SRL",
        "mapping": NETOPIA["mapping"] | ({} if value == "unmapped" else {"company_cui": "CUI"}),
    }
    if value in ("foreign", "unknown", "invalid"):
        with pytest.raises(bank_upload.Refused) as refused:
            bank_upload.accept_report(
                ledger,
                "NORD",
                data,
                bank_upload.XLSX if kind == "xlsx" else "text/csv",
                description=description,
                **UPLOAD,
            )
        assert refused.value.status == 422
        assert ledger.db.execute("SELECT count(*) FROM settlement_reports").fetchone()[0] == 0
        assert ledger.db.execute("SELECT count(*) FROM partner_settlement_formats").fetchone()[0] == 0
    else:
        result = bank_upload.accept_report(
            ledger, "NORD", data, bank_upload.XLSX if kind == "xlsx" else "text/csv", description=description, **UPLOAD
        )
        saved = ledger.db.execute("SELECT partner,data FROM partner_settlement_formats").fetchone()
        assert json.loads(saved["data"])["mapping"] == description["mapping"]
        assert collect(ledger, result["id"], statement()) == ["settlement-1", "settlement-2"]
        with pytest.raises(bank_upload.Refused) as ask:
            bank_upload.accept_report(
                ledger, "NORD", data, bank_upload.XLSX if kind == "xlsx" else "text/csv", **UPLOAD
            )
        assert ask.value.extra["known"][0]["company_cui_column"] == ("" if value == "unmapped" else "CUI")
    assert len(ledger.used_txs()) == (0 if value in ("foreign", "unknown", "invalid") else 1)
    assert all(r["status"] == "open" for r in ledger.db.execute("SELECT status FROM issued WHERE company='STUDIO9'"))
    if value in ("foreign", "unknown", "invalid"):
        assert all(r["status"] == "open" for r in ledger.db.execute("SELECT status FROM issued"))
