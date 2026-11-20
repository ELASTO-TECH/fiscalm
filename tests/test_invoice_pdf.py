"""The invoice PDF: layout, legal mentions, pagination, fonts and logo. All invoice facts and images are synthetic."""

import io
import re
import sys
import xml.etree.ElementTree as ET

import pikepdf
import pytest
from fontTools.ttLib import TTFont
from PIL import Image
from test_api import (
    _req,  # noqa: F401
    server as server,
)
from test_invoicing import DATA, LINE, PARTY, draft_with_confirmed_rate as draft, setup_invoice as setup_invoice  # noqa: F401

from fiscalm import companies, pdftext
from fiscalm.invoicing import branding, engine as i, pdf as invoice_pdf, ubl
from fiscalm.invoicing.canvas import FONT, Canvas, _font, validate_text
from fiscalm.invoicing.labels import vat_choices
from fiscalm.pdftext import pdf_text

CBC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}"
EUR = dict(currency="EUR", exchange_rate="4.9753", exchange_date="2026-10-30")


def issued(setup_invoice, data=None):
    L, _ = setup_invoice
    iid = draft(setup_invoice, data)
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    return iid


def render(setup_invoice, data=None, preview=False):
    """The text and bytes of a new invoice (a draft's preview when asked); rendering twice gives the same bytes."""
    L, _ = setup_invoice
    iid = draft(setup_invoice, data) if preview else issued(setup_invoice, data)
    raw = invoice_pdf.generate(L, iid, company="NORD", preview=preview)
    assert raw == invoice_pdf.generate(L, iid, company="NORD", preview=preview)
    return pdf_text(raw), raw


def cells(raw, with_weight=False):
    result = []
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            for font, size, x, y, value in re.findall(
                r"BT /F([123]) ([\d.]+) Tf [\d.]+ Tc 1 0 0 1 ([\d.]+) ([\d.]+) Tm <([0-9a-f]+)>",
                page.Contents.read_bytes().decode(),
            ):
                cell = (float(size), float(x), float(y), bytes.fromhex(value).decode("utf-16-be"))
                result.append(cell + ({"1": "Regular", "2": "Medium", "3": "SemiBold"}[font],) if with_weight else cell)
    return result


def runs(page):
    return [
        bytes.fromhex(v).decode("utf-16-be")
        for v in re.findall(r"<([0-9a-f]+)> Tj", page.Contents.read_bytes().decode())
    ]


def tax_row(table, label):
    baseline = next(y for _, x, y, v in table if x == 51 and v == label)
    return [v for _, _, y, v in table if y == baseline]


def set_logo(L, rgb):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), rgb).save(buf, format="PNG")
    branding.set_logo(L, "NORD", buf.getvalue())


@pytest.mark.parametrize(
    "value,kind,printed",
    [("2.1", "price", "2,10"), ("2.1000", "price", "2,10"), ("2.125", "price", "2,125"), ("-0.03", "amount", "-0,03")],
)
def test_romanian_number_formats(value, kind, printed):
    assert invoice_pdf.romanian(value, kind) == printed


def test_pdf_pagination_and_draft_refusal(setup_invoice):
    L, _ = setup_invoice
    iid = draft(setup_invoice, DATA | dict(lines=[LINE | dict(description="Descriere lungă " * 6)] * 50))
    with pytest.raises(i.InvoiceError):
        invoice_pdf.generate(L, iid, company="NORD")
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    raw = invoice_pdf.generate(L, iid, company="NORD")
    assert "6.050,00 lei" in pdf_text(raw)
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        count = len(pdf.pages)
        assert count > 2
        for n, page in enumerate(pdf.pages, 1):
            # Every page repeats the table header, the seller and its own page number, once; no draft watermark.
            values = runs(page)
            assert {"DESCRIERE", "CANTITATE", "Firma Fictivă SRL, CUI 99999984"} <= set(values)
            assert values.count(f"Pagina {n} din {count}") == 1 and "/ExtGState" not in page.Resources
            if "Total fără TVA" in values:
                assert "Total de plată" in values


def test_a_preview_is_unnumbered_and_prints_romanian_formats(setup_invoice):
    L, _ = setup_invoice
    data = DATA | dict(
        delivery_date=DATA["issue_date"],
        notes="Note · două",
        lines=[LINE | dict(quantity="2.500", unit_price="1234.50")],
    )
    text, raw = render(setup_invoice, data, preview=True)
    assert L.db.execute("SELECT number FROM invoices").fetchone()[0] is None
    for value in [
        "Ciornă",
        "Fără număr",
        "Note · două",
        "31.10.2026",
        "30.11.2026",
        "Brașov",
        "oră",
        "2,5",
        "1.234,50",
        "Total fără TVA",
        "21%",
        "Pagina 1 din 1",
        "Cod TVA",
        "Reg. Com.",
    ]:
        assert value in text
    for value in ["Factură", "Seria TEST nr.", "2026-10-31", "RO-BV", "HUR", "21% S", "Data livrării:"]:
        assert value not in text
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        assert list(pdf.pages[0].MediaBox) == [0, 0, 595, 842]
    table = cells(raw)
    for name, edge in [("CANTITATE", 309), ("PREȚ UNITAR", 383), ("VALOARE", 452)]:
        size, x, _, value = next(cell for cell in table if cell[3] == name)
        assert abs(x + Canvas().measure(value, size, "Medium", 0.405) - edge) < 0.001


@pytest.mark.parametrize(
    "category,country,reason,mention,absent",
    [
        ("AE", "DE", None, "Taxare inversă", "art. 331"),
        ("E", "RO", None, "Scutit de TVA conform art. 292", "VATEX-"),
        ("K", "DE", None, "Livrare intracomunitară scutită de TVA conform art. 294 alin. (2)", "VATEX-"),
        ("G", "DE", None, "Export de bunuri scutit de TVA conform art. 294 alin. (1)", "VATEX-"),
        ("O", "RO", None, "Regimul special de scutire pentru întreprinderile mici conform art. 310", "VATEX-"),
        # A reason saved on the line is its only legal basis: nothing is added for its category.
        ("O", "CA", "Servicii neimpozabile conform art. 278 alin. (2)", None, "art. 310"),
        ("G", "CA", "Servicii neimpozabile conform art. 278 alin. (2)", None, "art. 294"),
    ],
)
def test_legal_mentions(setup_invoice, category, country, reason, mention, absent):
    L, cid = setup_invoice
    payer = not (category == "O" and reason is None)
    if not payer:
        i.save_seller(L, "NORD", PARTY | dict(vat_payer=False, vat_id=""))
    if country != "RO":
        client = dict(vat_id="DE999999999") if country == "DE" else dict(county="AB", vat_id="", tax_id="CA999999999")
        i.save_client(L, "NORD", PARTY | client | dict(country=country), cid)
    choice = next(c for c in vat_choices({"vat_payer": payer})["choices"] if c["category"] == category)
    mention = mention or reason
    text, raw = render(
        setup_invoice,
        DATA
        | dict(
            delivery_date=DATA["issue_date"],
            lines=[
                LINE
                | dict(vat_category=category, vat_rate=choice["rate"], exemption_reason=reason or choice["exemption"])
            ],
        ),
    )
    assert mention in " ".join(v for _, x, _, v in cells(raw) if x >= 312.5) or mention in text
    assert absent not in text and "VATEX-" not in text
    if category in {"E", "O"} or reason:
        assert text.count(mention[-8:]) == 1  # once, not again beside the tax rate
    if country == "CA":
        assert "AB" in text and "Alba" not in text


def test_summary_alignment_and_bnr_value(setup_invoice):
    # Sixteen short rows leave no room for the VAT table on page one.
    text, raw = render(setup_invoice, DATA | EUR | dict(lines=[LINE] * 16))
    flat = " ".join(text.split())
    for expected in [
        "1.936,00 EUR",
        "Curs BNR din 30.10.2026 4,9753 lei/EUR",
        "Total în lei 9.632,18 lei",
        "TVA în lei 1.671,70 lei",
    ]:
        assert expected in flat
    assert "Diferență de rotunjire" not in text
    table = cells(raw)
    top = next(y for _, _, y, v in table if v == "TVA PE COTE")
    assert next(y for _, _, y, v in table if v == "Total fără TVA") == top
    size, x, y, val = next(cell for cell in table if cell[3] == "4,9753 lei/EUR")
    assert abs(x + Canvas().measure(val, size) - 550) < 0.001
    assert any(v == "Curs BNR din 30.10.2026" and yy == y and xx == 331 for _, xx, yy, v in table)


def test_multirate_storno_rounding_sign(setup_invoice):
    L, _ = setup_invoice
    # Each rate's 0,01 EUR of VAT is 0,05 lei, but the invoice's 0,02 EUR is 0,09 lei, as in the XML.
    iid = issued(
        setup_invoice,
        DATA
        | EUR
        | dict(
            exchange_rate="4.5", lines=[LINE | dict(unit_price="0.03"), LINE | dict(unit_price="0.05", vat_rate="11")]
        ),
    )
    rev = i.storno(L, iid, "2026-11-01", "Corecție fictivă", company="NORD")["id"]
    assert b'currencyID="RON">0.09</' in ubl.generate(L, iid, company="NORD")
    for doc, sign, delta in [(iid, "", "-0,01"), (rev, "-", "0,01")]:
        raw = invoice_pdf.generate(L, doc, company="NORD")
        text = pdf_text(raw)
        assert f"Diferență de rotunjire TVA în lei: {delta} lei" in text and "XML" not in text
        assert f"TVA în lei {sign}0,09 lei" in " ".join(text.split())
        assert "BAZĂ (EUR)" in text and "TVA (LEI)" in text
        for label, base in [("21%", "0,03"), ("11%", "0,05")]:
            assert tax_row(cells(raw), label)[-3:] == [sign + base, sign + "0,01", sign + "0,05"]


def test_ron_tax_rows_go_from_the_highest_rate(setup_invoice):
    text, raw = render(setup_invoice, DATA | dict(lines=[LINE | dict(vat_rate="11", unit="H87"), LINE]))
    table = cells(raw)
    assert tax_row(table, "21%")[-2:] == ["100,00", "21,00"] and tax_row(table, "11%")[-2:] == ["100,00", "11,00"]
    assert next(y for _, x, y, v in table if x == 51 and v == "21%") > next(
        y for _, x, y, v in table if x == 51 and v == "11%"
    )
    assert "buc" in text


@pytest.mark.parametrize("payer", [True, False])
def test_large_amounts_fit_and_no_line_vat(setup_invoice, payer):
    L, _ = setup_invoice
    line = LINE | dict(quantity="1000000", unit_price="9999999.99")
    if not payer:
        i.save_seller(L, "NORD", PARTY | dict(vat_payer=False, vat_id=""))
        line |= dict(vat_category="O", vat_rate="0", exemption_reason="Scutire conform art. 310")
    text, raw = render(
        setup_invoice,
        DATA | EUR | dict(exchange_rate="4.5", lines=[line, line | dict(quantity="1", unit_price="999999999.99")]),
    )
    table = cells(raw)
    for value in ["1.000.000", "999.999.999,99", "9.999.999.990.000,00"]:
        assert value in [v for _, _, _, v in table]
    # All numeric cells remain single strings; their right edge is a column boundary.
    line_y = next(y for _, _, y, value in table if value == "Servicii de încercare")
    for size, x, y, value, weight in cells(raw, with_weight=True):
        widths = _font(weight)[2]
        if y <= line_y and re.fullmatch(r"-?[0-9.,]+(?: lei| EUR)?", value) and len(value) > 6:
            right = x + sum(widths[ord(c)] for c in value) * size / 1000
            assert 45 <= x < right <= 550.001 and size >= 6, (value, size, x, right)
    assert "TVA 21%" not in text
    assert not any(v == "21,00" and y == line_y for _, _, y, v in table)
    assert text.count("IBAN:") == 1
    due = [cell for cell in table if cell[3] == "Total de plată"]
    assert len(due) == 1 and due[0][0] == 9.75
    if not payer:
        assert "Cota TVA" not in text and "Total TVA" not in text and "TVA lei" not in text
        assert text.count("art. 310") == 1


def test_storno_and_original_print_their_xml_number_and_delivery_date(setup_invoice):
    L, _ = setup_invoice
    iid = issued(setup_invoice, DATA | dict(delivery_date="2026-10-29"))
    rev = i.storno(L, iid, "2026-11-01", "Corecție fictivă", company="NORD")["id"]
    texts = []
    for doc in [iid, rev]:
        number = ET.fromstring(ubl.generate(L, doc, company="NORD")).find(CBC + "ID").text
        texts.append(pdf_text(invoice_pdf.generate(L, doc, company="NORD")))
        assert "Nr. " + number in texts[-1] and "29.10.2026" in texts[-1] and "Data livrării:" not in texts[-1]
    original, storno = texts
    assert "DATA LIVRĂRII" in original and "INIȚIALE" not in original
    assert "DATA LIVRĂRII FACTURII" in storno and "INIȚIALE" in storno
    assert "Stornare pentru factura TEST-42" in storno and "-21,00" in storno and "-121,00 lei" in storno


def test_distinct_exemptions_have_separate_bases(setup_invoice):
    reasons = ["Scutit conform art. 292 alin. (1)", "Scutit conform art. 294 alin. (1)"]
    _, raw = render(
        setup_invoice,
        DATA
        | dict(
            lines=[
                LINE | dict(vat_category="E", vat_rate="0", unit_price=str(n * 100), exemption_reason=reason)
                for n, reason in enumerate(reasons, 1)
            ]
        ),
    )
    table = cells(raw)
    labels = " ".join(v for _, x, _, v in table if x == 51)
    assert all(labels.count(reason) == 1 for reason in reasons)
    # Two separately identified exemption bases, rather than a single 300,00 base.
    for base in ["100,00", "200,00"]:
        assert any(x > 150 and value == base for _, x, _, value in table)
    assert sum(value.startswith("Scutit:") for _, _, _, value in table) == 2


def test_tax_heading_repeats_on_break_and_previews_are_watermarked(setup_invoice):
    # Multiple saved exemption grounds make the tax table independently paginate.
    lines = [
        LINE | dict(vat_category="E", vat_rate="0", exemption_reason=f"Scutire conform art. 292, cazul {n}")
        for n in range(30)
    ]
    text, raw = render(setup_invoice, DATA | dict(lines=lines), preview=True)
    assert "cazul 29" in text
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        tax_pages = 0
        for page in pdf.pages:
            values = runs(page)
            if "TVA PE COTE" in values:
                tax_pages += 1
                assert "COTA TVA" in values and "BAZĂ (LEI)" in values
            assert "/Draft gs" in page.Contents.read_bytes().decode()
            assert float(page.Resources.ExtGState.Draft.ca) == 0.08
        assert tax_pages >= 2


def test_large_eur_tax_cells_do_not_overlap(setup_invoice):
    _, raw = render(
        setup_invoice, DATA | EUR | dict(exchange_rate="1", lines=[LINE | dict(unit_price="99999999999999")])
    )
    table = cells(raw)
    baseline = next(y for _, x, y, v in table if x == 51 and v == "21%")
    row = sorted((x, size, v) for size, x, y, v in table if y == baseline)
    for (x, size, value), (next_x, _, _) in zip(row, row[1:], strict=False):
        assert size >= 6 and x + Canvas().measure(value, size) <= next_x
    assert next(y for _, _, y, v in table if v == "Total fără TVA") < baseline


def test_all_runs_within_columns(setup_invoice, monkeypatch):
    L, cid = setup_invoice
    reference = "1" * 50
    name = "Societatea " + reference
    i.save_seller(L, "NORD", PARTY | dict(name=name))
    i.save_client(L, "NORD", PARTY | dict(name=name), cid)
    note = "Facturile " + ",".join(map(str, range(101, 125))) + " sunt incluse."
    drawn = []
    original = Canvas.text
    wrap = Canvas.wrap
    wrapped = {}

    def record_wrap(self, value, width, size=8.25, weight="Regular"):
        rows = wrap(self, value, width, size, weight)
        for row in rows:
            wrapped[row, size, weight] = width
        return rows

    monkeypatch.setattr(Canvas, "wrap", record_wrap)

    def record(self, value, x, y, size=8.25, weight="Regular", fill="#334155", right=None, width=None, tracking=0):
        measured = self.measure(value, size, weight, tracking)
        actual_size = max(6, size * width / measured) if width and measured > width else size
        end = right if right is not None else x + self.measure(value, actual_size, weight, tracking)
        assert end <= 550.001, (value, end)
        if not tracking:
            # A wrapped row is measured at the size it is drawn, so it fits its column.
            column = width or wrapped.get((value, size, weight))
            if column is not None:
                assert self.measure(value, actual_size, weight) <= column + 0.001, (value, column)
        if value and (set(value) == {"1"} or value.startswith("Societatea")):
            # Description and party/header names have distinct left anchors.
            edge = (
                225
                if x == 73
                else 425
                if y == 27
                else 305
                if x == 45 and (size == 13.5 or y == 787)
                else 282.5
                if x == 45
                else 550
            )
            assert end <= edge + 0.001, (value, x, end, edge)
        drawn.append(value)
        return original(self, value, x, y, size, weight, fill, right, width, tracking)

    monkeypatch.setattr(Canvas, "text", record)
    iid = issued(setup_invoice, DATA | dict(notes=note, lines=[LINE | dict(description=reference)]))
    invoice_pdf.render(L, iid, company="NORD")
    assert "sunt incluse." in " ".join(drawn)
    c = Canvas()
    for value in [reference, note, name]:
        rows = c.wrap(value, 155)
        assert all(c.measure(row, 8.25) <= 155 for row in rows)
        assert "".join(rows).replace(" ", "") == value.replace(" ", "")


def test_compact_name_normal_size_and_numeric_space(setup_invoice):
    L, _ = setup_invoice
    i.save_seller(L, "NORD", PARTY | dict(name="Societatea de servicii și echipamente fictive " * 3))
    _, raw = render(setup_invoice, DATA | dict(lines=[LINE | dict(unit_price="123456789.12")] * 45))
    table = cells(raw)
    compact = [(size, v) for size, x, y, v in table if x == 45 and y == 787]
    assert any(size == 10 and v.endswith("…") for size, v in compact)
    assert all(size == 8.25 for size, _, _, v in table if v == "123.456.789,12")


def test_nonvat_102_rows_and_footer_once(setup_invoice):
    L, _ = setup_invoice
    i.save_seller(L, "NORD", PARTY | dict(vat_payer=False, vat_id=""))
    _, raw = render(
        setup_invoice,
        DATA | dict(lines=[LINE | dict(vat_category="O", vat_rate="0", exemption_reason="art. 310")] * 102),
    )
    table = cells(raw)
    assert all(str(n) in [v for _, x, _, v in table if x == 48] for n in range(1, 103))
    assert all(size >= 6 for size, _, _, _ in table)
    assert all(x + Canvas().measure(v, size) <= 64 for size, x, _, v in table if x == 48 and v == "102")
    assert any(x == 73 and v == LINE["description"] for _, x, _, v in table)


def test_only_printable_text_reaches_the_pdf(setup_invoice):
    # Invisible formatting controls are dropped before validating, measuring and printing; unprintable text is refused.
    controls = "­" + "".join(map(chr, [*range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x206A), 0xFEFF]))
    validate_text(controls)
    assert Canvas().measure("ABC" + controls, 8.25) == Canvas().measure("ABC", 8.25)
    text, _ = render(setup_invoice, DATA | dict(notes="Început" + controls + "final"))
    assert "Începutfinal" in text
    with pytest.raises(i.InvoiceError, match="nu pot fi afișate în PDF"):
        Canvas().text("中文", 0, 0)
    assert set(companies.PALETTES) == set(companies.PRINT_ACCENTS)


def test_embedded_fonts_are_named_subsets_of_the_shipped_inter(setup_invoice):
    """Every glyph the PDF uses, resolved through its own CID-to-GID map, is the shipped Inter glyph, unchanged."""
    note = "Mențiuni: ăâîșț ĂÂÎȘȚ"
    text, raw = render(setup_invoice, DATA | dict(notes=note))
    assert note in text
    used = set()
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        assert pdf.pdf_version >= "1.4"
        fonts = list(pdf.pages[0].Resources.Font.values())
        assert {str(f.BaseFont).split("-")[-1] for f in fonts} == {"Regular", "Medium", "SemiBold"}
        for font in fonts:
            assert re.fullmatch(r"/[A-Z]{6}\+Inter-(Regular|Medium|SemiBold)", str(font.BaseFont))
            weight = str(font.BaseFont).split("-")[-1]
            cid_font = font.DescendantFonts[0]
            embedded = TTFont(io.BytesIO(cid_font.FontDescriptor.FontFile2.read_bytes()))
            head = embedded["head"]
            assert list(cid_font.FontDescriptor.FontBBox) == [
                round(getattr(head, k) * 1000 / head.unitsPerEm) for k in ["xMin", "yMin", "xMax", "yMax"]
            ]
            original = TTFont(FONT / f"Inter-{weight}.ttf")
            assert not {"GSUB", "GPOS", "GDEF"} & set(embedded.keys())
            to_gid = cid_font.CIDToGIDMap.read_bytes()
            widths = list(cid_font.W)
            cids = [int(widths[k]) for k in range(0, len(widths), 2)]
            assert cids
            for cid in cids:
                used.add((weight, chr(cid)))
                ours = embedded.getGlyphOrder()[int.from_bytes(to_gid[cid * 2 : cid * 2 + 2], "big")]
                theirs = original.getBestCmap()[cid]
                a, b = embedded["glyf"][ours], original["glyf"][theirs]
                assert a.getCoordinates(embedded["glyf"]) == b.getCoordinates(original["glyf"]), (weight, chr(cid))
                assert embedded["hmtx"][ours] == original["hmtx"][theirs], (weight, chr(cid))
    assert {("Regular", c) for c in "ăâîșțĂÂÎȘȚ"} <= used


@pytest.mark.parametrize("weight,advances", [("Regular", [576, 942]), ("Medium", [546, 947]), ("SemiBold", [516, 953])])
def test_shipped_inter_has_tabular_digits_and_upstream_punctuation(weight, advances):
    font = TTFont(FONT / f"Inter-{weight}.ttf")
    # Unmodified extras/ttf fonts from the official Inter 4.1 archive, unitsPerEm=2048, except tabular digits.
    assert [font["hmtx"][font.getBestCmap()[ord(c)]][0] for c in " -"] == advances
    _, glyphs, widths = _font(weight)
    assert len({widths[ord(d)] for d in "0123456789"}) == 1
    assert all(glyphs[ord(c)] for c in "șțăîâȘȚĂÎÂ")


def gradient(mode, size):
    """Smooth bands under a plain margin: small as a PNG, larger in a PDF that drops the PNG's row filters."""
    radial, linear = Image.radial_gradient("L").resize(size), Image.linear_gradient("L").resize(size)
    turned = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90).resize(size)
    image = Image.merge(mode, [radial, linear, turned, radial][: len(mode)])
    image.paste(Image.new(mode, (size[0], 8), "white"))
    return image


@pytest.mark.parametrize("kind,mode", [("png", "RGBA"), ("png", "LA"), ("png", "L"), ("jpeg", "RGB"), ("jpeg", "L")])
def test_logo_and_size_budget(setup_invoice, kind, mode):
    L, _ = setup_invoice
    image = gradient(mode, (240, 160))
    upload = io.BytesIO()
    image.save(upload, format=kind.upper())
    branding.set_logo(L, "NORD", upload.getvalue())
    logo = branding.current_logo(L, "NORD")
    iid = issued(setup_invoice)
    raw = invoice_pdf.generate(L, iid, company="NORD", logo=logo)
    assert raw == invoice_pdf.generate(L, iid, company="NORD", logo=logo)
    assert len(raw) < 150_000
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        assert "/Logo Do" in pdf.pages[0].Contents.read_bytes().decode()
        im = pdf.pages[0].Resources.XObject.Logo
        assert (im.Subtype, im.Width, im.Height, im.BitsPerComponent) == ("/Image", 240, 160, 8)
        assert im.ColorSpace == ("/DeviceGray" if logo.components == 1 else "/DeviceRGB") and logo.components == len(
            mode.rstrip("A")
        )
        assert im.read_raw_bytes() == logo.data and ("/SMask" in im) == mode.endswith("A")
        if kind == "jpeg":
            assert im.Filter == "/DCTDecode"
            return
        # Row filters (predictor 15) keep the logo near its upload size; unfiltered, the RGBA one is ten times larger.
        assert logo.predictor and im.DecodeParms.Predictor == 15
        assert len(logo.data) + len(logo.alpha or b"") < 2 * len(upload.getvalue())
        assert im.read_bytes() == image.convert(mode.rstrip("A")).tobytes()
        if logo.alpha:
            assert im.SMask.read_raw_bytes() == logo.alpha
            assert im.SMask.read_bytes() == image.getchannel("A").tobytes()


def test_issued_documents_keep_their_logo_and_previews_show_the_current_one(setup_invoice, home, server):
    L, _ = setup_invoice
    # An invoice issued without a logo has no logo to read and prints none.
    with pikepdf.open(io.BytesIO(invoice_pdf.render(L, issued(setup_invoice), company="NORD"))) as pdf:
        assert "/XObject" not in pdf.pages[0].Resources
    a, b = (100, 20, 30), (20, 100, 30)
    set_logo(L, a)
    sha = branding.current_sha(L, "NORD")
    first = issued(setup_invoice, DATA | dict(lines=[LINE] * 45))
    rev = i.storno(L, first, "2026-11-01", "Corecție fictivă", company="NORD")["id"]
    preview = draft(setup_invoice)
    set_logo(L, b)
    for iid, is_preview, rgb in [(first, False, a), (rev, False, a), (preview, True, b)]:
        with pikepdf.open(io.BytesIO(invoice_pdf.render(L, iid, company="NORD", preview=is_preview))) as pdf:
            assert pdf.pages[0].Resources.XObject.Logo.read_bytes() == bytes(rgb) * 1024
            if iid == first:
                # Continuation pages carry the logo in their compact header.
                assert len(pdf.pages) == 3 and all("/Logo Do" in p.Contents.read_bytes().decode() for p in pdf.pages)
                assert " 776 cm /Logo Do" in pdf.pages[1].Contents.read_bytes().decode()
    (home / "NORD" / "branding" / f"logo-{sha}.png").unlink()
    with pytest.raises(i.InvoiceError, match="Sigla nu poate fi citită"):
        invoice_pdf.render(L, first, company="NORD")
    (home / "NORD" / "branding" / f"logo-{branding.current_sha(L, 'NORD')}.png").unlink()
    for url in [f"{first}.pdf?co=NORD", f"preview.pdf?co=NORD&draft={preview}"]:
        code, headers, raw = _req(server, "GET", "/web/invoicing/" + url)
        assert code == 409 and branding.UNAVAILABLE in raw.decode()
        assert headers["content-type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize(
    "script,timeout",
    [
        ("import os; os.write(1, b'x' * (9 * 1024 * 1024))", 5),
        ("import os; os.write(2, b'x' * (9 * 1024 * 1024))", 5),
        ("import time; time.sleep(2)", 0.1),
    ],
)
def test_pdf_extraction_refuses_unbounded_output_and_time(tmp_path, monkeypatch, script, timeout):
    tool = tmp_path / "parser"
    tool.write_text(f"#!{sys.executable}\n{script}\n")
    tool.chmod(0o700)
    monkeypatch.setattr(pdftext, "PDFTOTEXT", str(tool))
    monkeypatch.setattr(pdftext, "TEXT_TIMEOUT", timeout, raising=False)
    with pytest.raises(RuntimeError):
        pdftext.pdf_text(b"%PDF-synthetic")
