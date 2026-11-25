"""Settlement report parsing: CSV and XLSX refuse anything but plain, bounded, exact tables; uploads learn a format."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from test_settlements import collect, setup, statement, store_bank

from fiscalm import companies
from fiscalm.api import actions
from fiscalm.settlements import engine as settlements, upload as bank_upload
from fiscalm.settlements.reports import SOURCES, STRIPE, ReportError, money, parse, table

UPLOAD = dict(report_date="2026-10-10", today=date(2026, 10, 12))
NETOPIA = dict(
    source="netopia",
    name="Synthetic Processor SRL",
    currency="RON",
    mapping=dict(reference="Ref", gross="Gross", fee="Fee"),
)


NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def workbook(rows="", *more, sheet=None, strings=None):
    """An XLSX holding `rows` (or the raw `sheet` XML) on its first worksheet and `more` on the next ones, with
    optional shared strings."""
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        if strings is not None:
            z.writestr("xl/sharedStrings.xml", f"<sst {NS}>{strings}</sst>")
        z.writestr("xl/worksheets/sheet1.xml", sheet or f"<worksheet {NS}><sheetData>{rows}</sheetData></worksheet>")
        for n, extra in enumerate(more, 2):
            z.writestr(f"xl/worksheets/sheet{n}.xml", f"<worksheet {NS}><sheetData>{extra}</sheetData></worksheet>")
    return b.getvalue()


def cells(*rows):
    cell = '<c r="{}{}" t="inlineStr"><is><t>{}</t></is></c>'
    return "".join(
        "<row>" + "".join(cell.format(chr(65 + i), n, v) for i, v in enumerate(r)) + "</row>"
        for n, r in enumerate(rows, 1)
    )


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
    assert parse(data.replace(b"charge", b"refund"), "csv", "stripe")[0]["adjustment"] == "1"  # kept, flagged
    fee_only = data.replace(b"100.00,2.00,98.00", b"0.00,2.00,-2.00").replace(b"charge", b"fee")
    assert parse(fee_only, "csv", "stripe")[0]["fee"] == "2.00"  # a fee adjustment, flagged, not a refused file
    fee_back = data.replace(b"100.00,2.00,98.00", b"-100.00,2.00,-98.00").replace(b"charge", b"refund")
    assert parse(fee_back, "csv", "stripe")[0]["fee"] == "-2.00"  # the refund returned its fee: net says so
    for bad in (data.replace(b"98.00", b"98.01"), data.replace(b"100.00", b"NaN")):
        with pytest.raises(ReportError):
            parse(bad, "csv", "stripe")


def test_xlsx_sparse_and_formula():
    header = '<row><c r="A1" t="inlineStr"><is><t>Ref</t></is></c></row>'
    assert table(workbook(header + '<row><c r="C2"><v>12</v></c></row>'), "xlsx") == [["Ref"], ["", "", "12"]]
    with pytest.raises(ReportError):
        table(workbook(header + '<row><c r="A2"><f>1+1</f><v>2</v></c></row>'), "xlsx")


def test_xlsx_sheets_with_one_header_read_as_one_table():
    """Monthly workbooks split lines over sheets with one header: one table. Any other sheet (a summary, a title row,
    another layout) refuses: no sheet is left unread."""
    header = ["Ref", "Gross", "Fee"]
    data = workbook(cells(header, ["TEST-1", "100.00", "2.00"]), cells(header, ["TEST-2", "100.00", "2.00"]))
    assert table(data, "xlsx") == [header, ["TEST-1", "100.00", "2.00"], ["TEST-2", "100.00", "2.00"]]
    for other in (["Total", "Lines"], ["REF", "GROSS", "FEE"], ["Gross", "Ref", "Fee"]):
        with pytest.raises(ReportError):
            table(workbook(cells(header, ["TEST-1", "1", "0"], ["TEST-2", "1", "0"]), cells(other, ["1", "2"])), "xlsx")
    rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'
    for target, extra in (("worksheets/refunds.xml", cells(["Gross"], ["-5.00"])), ("worksheets/missing.xml", "")):
        named = io.BytesIO()  # a worksheet the workbook names but doesn't number (or doesn't hold)
        with zipfile.ZipFile(named, "w") as z:
            sheet = f"<worksheet {NS}><sheetData>{{}}</sheetData></worksheet>"
            z.writestr("xl/worksheets/sheet1.xml", sheet.format(cells(header, ["T", "1", "0"])))
            z.writestr(
                "xl/_rels/workbook.xml.rels",
                rels.format(f'<Relationship Id="rId9" Type="x/worksheet" Target="{target}"/>'),
            )
            if extra:
                z.writestr("xl/" + target, sheet.format(extra))
        with pytest.raises(ReportError):
            table(named.getvalue(), "xlsx")

    def declared(*targets):  # a workbook declaring Sales and Refunds, with relationships for `targets` only
        out, rid = io.BytesIO(), 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        with zipfile.ZipFile(out, "w") as z:
            sheet = f"<worksheet {NS}><sheetData>{{}}</sheetData></worksheet>"
            for n in (1, 2):
                z.writestr(f"xl/worksheets/sheet{n}.xml", sheet.format(cells(header, [f"T{n}", "1", "0"])))
            sheets = '<sheet name="Sales" r:id="rId1"/><sheet name="Refunds" r:id="rId2"/>'
            z.writestr("xl/workbook.xml", f"<workbook {NS} {rid}><sheets>{sheets}</sheets></workbook>")
            links = "".join(f'<Relationship Id="rId{n}" Type="x/worksheet" Target="{t}"/>' for n, t in targets)
            z.writestr("xl/_rels/workbook.xml.rels", rels.format(links))
        return out.getvalue()

    both = declared((1, "worksheets/sheet1.xml"), (2, "worksheets/sheet2.xml"))
    assert [r[0] for r in table(both, "xlsx")] == ["Ref", "T1", "T2"]
    with pytest.raises(ReportError):  # Refunds declared, its relationship gone
        table(declared((1, "worksheets/sheet1.xml")), "xlsx")
    for old, new in ((b"sheetData", b"data"), (b"xl/workbook.xml", b"xl/workbook.xmz")):
        broken = io.BytesIO()  # Refunds unreadable (no sheetData); the workbook manifest missing
        with zipfile.ZipFile(io.BytesIO(both)) as src, zipfile.ZipFile(broken, "w") as z:
            for info in src.infolist():
                body = src.read(info)
                name = (
                    info.filename.replace("xl/workbook.xml", new.decode())
                    if old == b"xl/workbook.xml"
                    else info.filename
                )
                z.writestr(name, body.replace(old, new) if name.endswith("sheet2.xml") else body)
            z.writestr(
                "_rels/.rels", rels.format('<Relationship Id="rId1" Type="x/officeDocument" Target="xl/workbook.xml"/>')
            )
        with pytest.raises(ReportError):
            table(broken.getvalue(), "xlsx")

    def variant(**parts):  # `both` with parts replaced or added (None drops one)
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(both)) as src, zipfile.ZipFile(out, "w") as z:
            files = {n: src.read(n) for n in src.namelist()} | parts
            for name, body in files.items():
                if body is not None:
                    z.writestr(name, body)
        return out.getvalue()

    refunds = f"<worksheet {NS}><sheetData>{cells(['Gross'], ['-5.00'])}</sheetData></worksheet>".encode()
    twice = rels.format(
        '<Relationship Id="rId1" Type="x/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId1" Type="x/worksheet" Target="worksheets/sheet2.xml"/>'
    )
    strict = both_book = zipfile.ZipFile(io.BytesIO(both)).read("xl/workbook.xml")
    strict = both_book.replace(NS.split('"')[1].encode(), b"http://purl.oclc.org/ooxml/spreadsheetml/main")
    for parts in (
        {"xl/_rels/workbook.xml.rels": twice.encode()},  # one id twice: a sheet could be dropped
        {"xl/workbook.xml": strict},  # another dialect: its sheet list can't be read
        {"xl/worksheets/sub/refunds.xml": refunds},  # nested under xl/worksheets/
        {"xl/worksheets/SHEET3.XML": refunds},  # upper case
        {  # Refunds outside xl/worksheets/, said to be a chart sheet
            "xl/refunds.xml": refunds,
            "xl/worksheets/sheet2.xml": None,
            "xl/_rels/workbook.xml.rels": rels.format(
                '<Relationship Id="rId1" Type="x/worksheet" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Type="x/chartsheet" Target="refunds.xml"/>'
            ).encode(),
        },
        {"xl/workbook.xml": both_book.replace(b' r:id="rId2"', b"")},  # a sheet without its id
        {"_rels/.rels": rels.format("").encode()},  # a package naming no workbook
    ):
        with pytest.raises(ReportError):
            table(variant(**parts), "xlsx")


@pytest.mark.parametrize("data", [b"a,a\n1,2", b"a,b\n1", b"a,b\n1,2,3"])
def test_bad_tables(data):
    with pytest.raises(ReportError):
        parse(data, "csv", "fan", {"reference": "a", "gross": "b", "fee": "b"})


@pytest.mark.parametrize("source", ["stripe", "fan"])
def test_xlsx_source_happy(source):
    """XLSX reads the same for the Stripe preset and for a mapped source (every mapped source parses alike)."""
    headers = ["invoice_number", "gross", "fee", "net", "currency", "automatic_payout_id", "reporting_category"]
    values = ["TEST-1", "100.00", "2.00", "98.00", "ron", "po_synthetic", "charge"]
    rows = cells(headers, values)
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


@pytest.mark.parametrize(
    "gross,fee,net,expected",
    [
        ("1.250,50", "-12,50", "1.238,00", ("1250.50", "12.50", "")),
        ("1,250.50", "12.50", "", ("1250.50", "12.50", "")),
        ("1 250,50 lei", "-", "1.250,50", ("1250.50", "0.00", "RON")),
        ("€ 100", "2,00 EUR", "98", ("100.00", "2.00", "EUR")),
        ("100", "2", "-", None),  # a mapped net must say the amount
        ("$ 100", "0", "", None),  # USD, CAD or AUD
        ("1,000,000,000,000.00", "0", "", None),  # past the stored amount's 12 digits
        ("1 " * 300 + "x", "0", "", None),  # a long odd cell refuses at once
        ("0.250,50", "0", "", None),  # a grouped amount never starts with 0
        ("100lei", "100,00", "-0,00", ("100.00", "100.00", "RON")),  # a net of -0 is stored as 0.00
        ("1.250", "0", "", None),  # one group, no decimals: 1250 or 1.25
        ("100,005", "0", "", None),
        ("1e3", "0", "", None),
        ("100 RON", "2 EUR", "", None),
        ("-100,00", "0", "", ("-100.00", "0.00", "")),
        ("-100,00", "2,00", "", None),  # a refund's fee: kept or returned? Only a mapped net says.
        (
            "0",
            "2,00",
            "-2,00",
            None,
        ),  # zero gross, a fee: an adjustment only with a non-charge category (test_stripe)  # a refund line: kept and flagged (test_settlements)
    ],
)
def test_real_amounts_parse_exactly_or_refuse(gross, fee, net, expected):
    """Romanian and English separators, currency words, signed fees and placeholders; ambiguity refuses."""
    data = f"Ref;Telefon;Gross;Fee;Net\n=TEST-1;+40700000000;{gross};{fee};{net}\n".encode()
    mapping = {"reference": "Ref", "gross": "Gross", "fee": "Fee"} | ({"net": "Net"} if net else {})
    if expected is None:
        with pytest.raises(ReportError):
            parse(data, "csv", "fan", mapping, ";")
        return
    line = parse(data, "csv", "fan", mapping, ";")[0]
    assert (line["reference"], line["gross"], line["fee"], line.get("currency", "")) == ("=TEST-1", *expected)
    assert money(line["gross"], signed=True) == Decimal(expected[0])  # what is stored reads back exactly
    if net:  # a mapped net is stored as gross minus fee, never `-0.00`
        assert line["net"] == f"{Decimal(expected[0]) - Decimal(expected[1]):.2f}"


def test_multi_payout_file_becomes_one_report_per_payout(ledger):
    """Two payout ids in one borderou: two reports, each dated by its own day, stored together; the file again is a
    duplicate."""
    for n in (1, 2):
        ledger.add_issued(dict(message_id=f"m{n}", company="NORD", number=f"TEST-{n}", status="open", raw_path=""))
    data = (
        b"Borderou;Factura;Ramburs;Comision;Data\nB-1;TEST-1;100,00;2,00;09.10.2026\nB-2;TEST-2;50,00;1,00;11.10.2026\n"
    )
    mapping = dict(batch="Borderou", reference="Factura", gross="Ramburs", fee="Comision", date="Data")
    described = NETOPIA | dict(source="fan", name="Synthetic Courier SRL", mapping=mapping)
    result = bank_upload.accept_report(ledger, "NORD", data, "text/csv", description=described, **UPLOAD)
    rows = settlements.state(ledger, "NORD")
    assert sorted(result["ids"]) == sorted(r["id"] for r in rows)
    assert sorted((r["report_key"], r["date"], r["net"]) for r in rows) == [
        ("B-1", "2026-10-09", "98.00"),
        ("B-2", "2026-10-11", "49.00"),
    ]
    partner = ledger.db.execute("SELECT partner FROM partner_settlement_formats").fetchone()[0]
    again = bank_upload.accept_report(ledger, "NORD", data, "text/csv", partner=partner, **UPLOAD)
    assert again["status"] == "duplicate" and len(settlements.state(ledger, "NORD")) == 2
    mixed = data.replace(b"B-2;TEST-2;50,00;1,00;11", b"B-1;TEST-2;50,00;1,00;11")
    old = data.replace(b"11.10.2026", b"11.10.2024")
    clash = data.replace(b"B-1;", b"B-9;", 1).replace(b"B-2;", b"B-1;")  # a new payout, then one already stored
    for bad in (mixed, old, clash):  # two days in one payout; a day past the upload rule; a stored payout id
        with pytest.raises(bank_upload.Refused):
            bank_upload.accept_report(ledger, "NORD", bad, "text/csv", partner=partner, **UPLOAD)
    assert len(settlements.state(ledger, "NORD")) == 2  # nothing of a refused file is stored
    once = described | {"mapping": {k: v for k, v in mapping.items() if k not in ("batch", "date")}}
    remapped = bank_upload.accept_report(ledger, "NORD", data, "text/csv", description=once, **UPLOAD)
    assert remapped["status"] == "duplicate" and len(settlements.state(ledger, "NORD")) == 2  # same bytes, one file
    b2 = next(r["id"] for r in settlements.state(ledger, "NORD") if r["report_key"] == "B-2")
    assert ledger.withdraw_report(b2, "NORD")  # withdrawn to correct it: it comes back as a corrected file
    again = bank_upload.accept_report(ledger, "NORD", data, "text/csv", partner=partner, **UPLOAD)
    assert again["status"] == "duplicate" and len(settlements.state(ledger, "NORD")) == 1
    corrected = b"Borderou;Factura;Ramburs;Comision;Data\nB-2;TEST-2;50,00;1,00;11.10.2026\n"
    assert (
        bank_upload.accept_report(ledger, "NORD", corrected, "text/csv", partner=partner, **UPLOAD)["status"] == "added"
    )


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
    "bad", [b"Ref,Gross,Fee\nTEST-1,NaN,2.00\n", b'Ref,Gross,Fee\nTEST-1,"1,250",2.00\n', b"\xff,\xff\n1,2\n"]
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


def test_one_report_writes_its_fees_one_way():
    """A fee written `-5` beside one written `5` could be a rebate or a deduction: the report refuses."""
    data = b"Ref;Gross;Fee\nTEST-1;100,00;5,00\nTEST-2;100,00;-5,00\n"
    mapping = {"reference": "Ref", "gross": "Gross", "fee": "Fee"}
    with pytest.raises(ReportError):
        parse(data, "csv", "fan", mapping, ";")
    assert [x["fee"] for x in parse(data.replace(b";5,00", b";-5,00"), "csv", "fan", mapping, ";")] == ["5.00", "5.00"]


def test_first_fan_borderou_settles_on_its_own(ledger):
    """Issue #49: a first FAN borderou with a phone column, `-` cells and Romanian amounts, matching open invoices
    and the bank credit to the cent, settles without a person's confirmation."""
    for n, total in ((1, "1250.50"), (2, "300.00")):
        ledger.add_issued(
            dict(
                message_id=f"fan-{n}",
                company="NORD",
                number=f"FAN-{n}",
                issue_date="2026-10-01",
                currency="RON",
                total=total,
                doc_type="invoice",
                status="open",
                raw_path="",
            )
        )
    store_bank(ledger, statement(amount=Decimal("1538.00")))  # 1550.50 collected minus 12.50 fee
    data = (
        b"AWB;Telefon destinatar;Numar factura;Ramburs;Comision ramburs;Observatii\n"
        b"1000000001;+40700000000;FAN-1;1.250,50;-12,50;-\n"
        b"1000000002;+40700000001;FAN-2;300,00;-;-\n"
    )
    mapping = dict(reference="Numar factura", gross="Ramburs", fee="Comision ramburs")
    described = dict(source="fan", name="Synthetic Courier SRL", currency="RON", mapping=mapping)
    result = bank_upload.accept_report(ledger, "NORD", data, "text/csv", description=described, **UPLOAD)
    assert result["message"] == bank_upload.COLLECTED
    assert {r["message_id"]: r["status"] for r in ledger.issued_rows()} == {"fan-1": "collected", "fan-2": "collected"}
    assert ledger.db.execute("SELECT count(*) FROM settlement_confirmations").fetchone()[0] == 0
