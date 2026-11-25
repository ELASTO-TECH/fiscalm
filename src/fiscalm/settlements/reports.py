"""Bounded settlement tables. No formulas, macros, links or inferred column meanings."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree as ET

SOURCES = ("fan", "sameday", "cargus", "dpd", "gls", "stripe", "netopia", "payu", "courier", "processor")
# Stripe's documented payout report columns: the only mapping a Stripe report is read with.
STRIPE = {
    "reference": "invoice_number",
    "gross": "gross",
    "fee": "fee",
    "net": "net",
    "currency": "currency",
    "batch": "automatic_payout_id",
    "category": "reporting_category",
}
LIMIT = 4 * 1024 * 1024


class ReportError(ValueError):
    pass


# Currency words a report writes next to an amount or in its currency column, by ISO code.
CURRENCY_WORDS = {"RON": "RON", "LEI": "RON", "EUR": "EUR", "€": "EUR", "USD": "USD"}  # never `$`: USD, CAD, AUD…
_NUMBER = re.compile(
    r"(?P<int>\d{1,12}|[1-9]\d{0,2}(?P<sep>[., '])\d{3}(?:(?P=sep)\d{3}){0,3})(?:(?P<dec>[.,])(?P<cents>\d{1,2}))?"
)
# A lone dot or comma before the decimal digits is a separator; space, apostrophe and non-breaking spaces group thousands.
PLACEHOLDERS = ("-", "–", "—")


def currency_code(text: str) -> str:
    """`lei`, `RON`, `ron` and `€` name a currency the same way; anything else is kept as written, upper case."""
    return CURRENCY_WORDS.get(text.strip().upper(), text.strip().upper())


def amount(value: str) -> tuple[Decimal, str]:
    """A signed money cell as Romanian and English exports write it, exactly: `1.234,56`, `123,45`, `1,234.56`,
    `-12.50`, `1 250,50 RON`, `lei 10`. Returns the amount and the currency the cell names ("" when none).
    `1.234` or `1,234` (one group, no decimals) could be either reading, so it refuses; so do exponents and more than
    two decimals: no rounding, no guessing."""
    text = value.replace("\u00a0", " ").replace("\u202f", " ").strip() if isinstance(value, str) else ""
    # One step at a time, each a plain string test, so a long or odd cell refuses in linear time.
    number, word = text if len(text) <= 40 else "", ""
    for w in CURRENCY_WORDS:
        if number.upper().startswith(w) and not number[len(w) : len(w) + 1].isalpha():
            number, word = number[len(w) :].strip(), w
            break
        if number.upper().endswith(w) and not number[-len(w) - 1 : -len(w)].isalpha():
            number, word = number[: -len(w)].strip(), w
            break
    sign = number[:1] if number[:1] in "+-" else ""
    n = _NUMBER.fullmatch(number[len(sign) :].lstrip(" ")) if number else None
    if (
        not n
        or (n["dec"] and n["sep"] == n["dec"])
        or (n["sep"] in (".", ",") and n["int"].count(n["sep"]) == 1 and not n["dec"])
    ):
        raise ReportError("Invalid amount")
    digits = re.sub(r"\D", "", n["int"]) + "." + (n["cents"] or "0")
    if len(digits.partition(".")[0]) > 12:  # the same bound as a stored amount (`money`)
        raise ReportError("Invalid amount")
    return Decimal(("-" if sign == "-" else "") + digits), currency_code(word) if word else ""


def money(value: str, *, signed: bool = False) -> Decimal:
    """A stored or structured amount (a parsed line, a Shopify payout): plain dot decimal, nothing to interpret.
    `signed`: a stored report line, where a refund or adjustment keeps its minus."""
    pattern = r"-?\d{1,12}(?:\.\d{1,2})?" if signed else r"\d{1,12}(?:\.\d{1,2})?"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ReportError("Invalid amount; use decimal strings with a dot")
    return Decimal(value)


def _xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper() or b"\x00" in data:
        raise ReportError("Unsafe XML")
    return ET.fromstring(data)


def table(data: bytes, kind: str, delimiter: str = ",") -> list[list[str]]:
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in "\r\n\x00":
        raise ReportError("One printable delimiter required")
    if not data or len(data) > LIMIT:
        raise ReportError("Report is empty or too large")
    try:
        if kind == "csv":
            rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=delimiter, strict=True))
        elif kind == "xlsx":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = z.infolist()
                if (
                    len(infos) > 100
                    or sum(i.file_size for i in infos) > 8 * LIMIT
                    or len({i.filename for i in infos}) != len(infos)
                ):
                    raise ReportError("Oversized or duplicate XLSX entries")
                if any("externalLink" in i.filename or "vbaProject" in i.filename for i in infos):
                    raise ReportError("External links and macros refuse")
                names = _worksheets(z)
                strings = []
                if "xl/sharedStrings.xml" in z.namelist():
                    strings = ["".join(x.itertext()) for x in _xml(z.read("xl/sharedStrings.xml")).findall("s:si", NS)]
                rows = _one_table([_sheet(z.read(name), strings) for name in names])
        else:
            raise ReportError("Use CSV or XLSX")
    except (
        UnicodeError,
        csv.Error,
        zipfile.BadZipFile,
        ET.ParseError,
        IndexError,
        ValueError,
        AttributeError,
        KeyError,
    ) as e:
        raise ReportError("Unreadable report; check format and mapping") from e
    if not 2 <= len(rows) <= 10001 or any(len(r) > 128 or any(len(v) > 1024 for v in r) for r in rows):
        raise ReportError("Invalid report dimensions")
    # A cell starting with = + - @ is data here (a phone number, a signed fee, a placeholder): only XLSX formula nodes
    # refuse above. Writers that hand cells to a spreadsheet escape them (pack.summary_csv).
    return rows


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _relationships(z: zipfile.ZipFile, name: str) -> list[tuple[str, str, str]]:
    """(id, type, target) of a relationships part, read strictly: its own root, and every id present and unique."""
    root = _xml(z.read(name))
    rels = [(r.get("Id") or "", r.get("Type", ""), r.get("Target", "")) for r in root.iter(_RELS)]
    if root.tag != _RELS + "s" or any(not i for i, _, _ in rels) or len({i for i, _, _ in rels}) != len(rels):
        raise ReportError("Unreadable worksheet")
    return rels


def _chart(z: zipfile.ZipFile, path: str) -> bool:
    return path in z.namelist() and _xml(z.read(path)).tag == f"{{{NS['s']}}}chartsheet"


def _worksheets(z: zipfile.ZipFile) -> list[str]:
    """Every worksheet of the workbook, none left unread. Any manifest present is read strictly: the package's one
    office document is xl/workbook.xml and is there; the workbook lists its sheets, each resolving through the
    workbook relationships to a part that is there (a chart sheet holds no lines). Every worksheet relationship and
    every part under xl/worksheets/ (any depth or case) is read too."""
    names = set(z.namelist())
    if "_rels/.rels" in names:
        office = [t.lstrip("/") for _, kind, t in _relationships(z, "_rels/.rels") if kind.endswith("/officeDocument")]
        if office != ["xl/workbook.xml"] or "xl/workbook.xml" not in names:
            raise ReportError("Unreadable worksheet")  # the workbook that lists the sheets is not there
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rid, kind, target in _relationships(z, "xl/_rels/workbook.xml.rels"):
            rels[rid] = (kind, target.lstrip("/") if target.startswith("/") else "xl/" + target)
    declared = []
    if "xl/workbook.xml" in names:
        book = _xml(z.read("xl/workbook.xml"))
        if book.tag != f"{{{NS['s']}}}workbook" or book.find("s:sheets", NS) is None:
            raise ReportError("Unreadable worksheet")  # another dialect (Strict OOXML) or no sheet list
        for sheet in book.findall("s:sheets/s:sheet", NS):
            kind, path = rels.get(sheet.get(_RID), ("", ""))
            if kind.endswith("/chartsheet") and _chart(z, path):
                continue  # a chart sheet holds no lines; one that isn't really a chart sheet refuses below
            declared.append(path if kind.endswith("/worksheet") else "")
    named = [path for kind, path in rels.values() if kind.endswith("/worksheet")]
    if any(not re.fullmatch(r"xl/[A-Za-z0-9_/]+\.xml", n) or ".." in n or n not in names for n in declared + named):
        raise ReportError("Unreadable worksheet")
    found = sorted(n for n in names if re.fullmatch(r"(?i)xl/worksheets/(?!_rels/).+\.xml", n))
    return list(dict.fromkeys([*declared, *named, *found, *_typed(z)]))


def _typed(z: zipfile.ZipFile) -> list[str]:
    """The worksheet parts [Content_Types].xml names; each must be there. A default type making every part of an
    extension a worksheet can't be told part by part and refuses."""
    if "[Content_Types].xml" not in z.namelist():
        return []
    types = "{http://schemas.openxmlformats.org/package/2006/content-types}"
    root = _xml(z.read("[Content_Types].xml"))
    if root.tag != types + "Types" or any(
        "worksheet" in d.get("ContentType", "").lower() for d in root.iter(types + "Default")
    ):
        raise ReportError("Unreadable worksheet")
    parts = [
        o.get("PartName", "").lstrip("/")
        for o in root.iter(types + "Override")
        if o.get("ContentType", "").lower().endswith("worksheet+xml")
    ]
    if any(not re.fullmatch(r"xl/[A-Za-z0-9_/]+\.xml", p) or p not in z.namelist() for p in parts):
        raise ReportError("Unreadable worksheet")
    return parts


def _one_table(sheets: list[list[list[str]]]) -> list[list[str]]:
    """A workbook's table: every non-empty sheet, all with the same header row, read as one with the header once.
    Any other sheet (a summary, a title row, another layout) refuses the file: no sheet is left out unread."""
    tables: dict[tuple[str, ...], list[list[str]]] = {}
    for rows in sheets:
        if rows:
            tables.setdefault(tuple(rows[0]), [list(rows[0])]).extend(rows[1:])
    if len(tables) != 1:  # a summary or a sheet laid out another way could hold lines: never guessed
        raise ReportError("Exactly one table required")
    return next(iter(tables.values()))


def _sheet(xml: bytes, strings: list[str]) -> list[list[str]]:
    rows, root = [], _xml(xml)
    if root.tag != f"{{{NS['s']}}}worksheet" or root.find("s:sheetData", NS) is None:
        raise ReportError("Unreadable worksheet")  # never read as an empty sheet
    for row in root.findall("s:sheetData/s:row", NS):
        values: list[str] = []
        for c in row.findall("s:c", NS):
            if c.find("s:f", NS) is not None or c.get("t") in ("e", "b"):
                raise ReportError("Formula or unsupported cell")
            ref = c.get("r", "")
            if not re.fullmatch(r"[A-Z]{1,3}\d+", ref):
                raise ReportError("Invalid cell reference")
            col = 0
            for ch in re.sub(r"\d", "", ref):
                col = col * 26 + ord(ch) - 64
            if col > 128 or col <= len(values):
                raise ReportError("Invalid column ordering")
            values.extend([""] * (col - len(values)))
            v = c.findtext("s:v", "", NS)
            if c.get("t") == "s":
                if not v.isdigit() or int(v) >= len(strings):
                    raise ReportError("Invalid shared string index")
                values[-1] = strings[int(v)]
            else:
                values[-1] = "".join(c.find("s:is", NS).itertext()) if c.get("t") == "inlineStr" else v
        rows.append(values)
    return rows


def _money_line(n: int, line: dict[str, str]) -> dict[str, str]:
    """Gross, fee and net as exact cents, one currency per line. A fee written as a deduction (`-2,00`) is the same fee;
    an empty fee is zero."""
    gross, currencies = amount(line["gross"]), set()
    fee = amount(line["fee"]) if line["fee"] else (Decimal(0), "")
    net = amount(line["net"]) if "net" in line else None  # a mapped net is evidence: a blank or `-` refuses
    for _, code in (gross, fee, net or (0, "")):
        currencies.add(code)
    if line.get("currency"):
        currencies.add(currency_code(line["currency"]))
    currencies.discard("")
    if len(currencies) > 1:
        raise ReportError("Mixed currencies")
    total, charge = gross[0], abs(fee[0])
    # A refund, dispute or adjustment (negative amount or a category other than a charge) is kept as written and
    # flagged: its batch never settles on its own, and the Sarcină names this line.
    adjustment = total < 0 or line.get("category", "charge") != "charge"
    if adjustment and charge:
        # A returned fee or a fee kept: only a mapped net says which; without one the line can't be counted.
        if net is None or abs(total - net[0]) != charge:
            raise ReportError(f"Line {n}: invalid gross/fee/net")
        charge = total - net[0]
    if (
        not (total or (adjustment and charge))
        or (not adjustment and (total <= 0 or charge > total))
        or (net is not None and net[0] != total - charge)
    ):
        raise ReportError(f"Line {n}: invalid gross/fee/net")
    if not adjustment and not any(line.get(k) for k in ("reference", "awb", "order")):
        raise ReportError(f"Line {n}: missing reference or control character")
    # An adjustment's fee keeps its direction (negative: a fee returned); a charge's fee is always kept, positive.
    out = line | {"gross": f"{total:.2f}", "fee": f"{charge:.2f}"} | ({"adjustment": "1"} if adjustment else {})
    if net is not None:
        out["net"] = f"{total - charge:.2f}"  # equal to the mapped net, written as stored money
    if currencies:
        out["currency"] = currencies.pop()
    return out


def parse(
    data: bytes, kind: str, source: str, mapping: dict[str, str] | None = None, delimiter: str = ","
) -> list[dict[str, str]]:
    """Parse every row or refuse the entire report. Mapping keys are canonical field names."""
    if source not in SOURCES:
        raise ReportError("Unknown settlement source")
    if source == "stripe":
        if mapping is not None and mapping != STRIPE:
            raise ReportError("Stripe requires the documented payout columns")
        mapping = dict(STRIPE)
    if not isinstance(mapping, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()
    ):
        raise ReportError("Column mapping must contain text fields")
    if (
        not mapping
        or not {"gross", "fee"} <= mapping.keys()
        or not {"reference", "order", "awb"} & mapping.keys()
        or set(mapping)
        - {
            "reference",
            "gross",
            "fee",
            "net",
            "currency",
            "batch",
            "date",
            "category",
            "awb",
            "order",
            "courier",
            "company",
            "company_cui",
        }
    ):
        raise ReportError("Explicit identifier/gross/fee column mapping required")
    rows = table(data, kind, delimiter)
    header = rows.pop(0)
    if len(set(header)) != len(header) or not set(mapping.values()) <= set(header):
        raise ReportError("Missing or duplicate headers")
    result, signs = [], set()
    for n, row in enumerate(rows, 2):
        if kind == "xlsx" and len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        if len(row) != len(header):
            raise ReportError(f"Line {n}: wrong column count")
        line = {key: row[header.index(col)].strip() for key, col in mapping.items()}
        line = {k: "" if v in PLACEHOLDERS else v for k, v in line.items()}
        if any(ord(c) < 32 for v in line.values() for c in v) or ("batch" in line and not line["batch"]):
            raise ReportError(f"Line {n}: missing reference or control character")
        if "date" in line:
            line["date"] = (day(line["date"]) or _refuse(f"Line {n}: invalid date")).isoformat()
        result.append(_money_line(n, line))
        fee = amount(line["fee"])[0] if line["fee"] else 0
        if fee and not result[-1].get("adjustment"):
            signs.add(fee < 0)
    if len(signs) > 1:  # `-5` beside `5`: a rebate or a deduction? One report writes its fees one way.
        raise ReportError("Fee signs differ: invalid gross/fee/net")
    return result


def _refuse(message: str):
    raise ReportError(message)


def day(value: str) -> date | None:
    """A payment day as exports write it: 2026-10-31, 31.10.2026 or 31/10/2026, with an optional time."""
    m = re.fullmatch(
        r"(?:(\d{4})-(\d{2})-(\d{2})|(\d{1,2})[./](\d{1,2})[./](\d{4}))(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?", value.strip()
    )
    try:
        return m and date(*map(int, m.group(1, 2, 3) if m[1] else m.group(6, 5, 4)))
    except ValueError:
        return None


def payouts(lines: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """A report's lines by payout id, in file order ("" when the file has no payout column). With a mapped date
    column, every line of one payout names the same day."""
    out: dict[str, list[dict[str, str]]] = {}
    for line in lines:
        out.setdefault(line.get("batch", ""), []).append(line)
    if any(len({x["date"] for x in group}) > 1 for group in out.values() if "date" in group[0]):
        raise ReportError("Payout dates differ")
    return out
