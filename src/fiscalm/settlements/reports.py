"""Bounded settlement tables. No formulas, macros, links or inferred column meanings."""

from __future__ import annotations

import csv
import io
import re
import zipfile
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


def money(value: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
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
                sheets = [i.filename for i in infos if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", i.filename)]
                if len(sheets) != 1:
                    raise ReportError("Exactly one worksheet required")
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                strings = []
                if "xl/sharedStrings.xml" in z.namelist():
                    strings = ["".join(x.itertext()) for x in _xml(z.read("xl/sharedStrings.xml")).findall("s:si", ns)]
                rows = []
                for row in _xml(z.read(sheets[0])).findall("s:sheetData/s:row", ns):
                    values: list[str] = []
                    for c in row.findall("s:c", ns):
                        if c.find("s:f", ns) is not None or c.get("t") in ("e", "b"):
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
                        v = c.findtext("s:v", "", ns)
                        if c.get("t") == "s":
                            if not v.isdigit() or int(v) >= len(strings):
                                raise ReportError("Invalid shared string index")
                            values[-1] = strings[int(v)]
                        else:
                            values[-1] = "".join(c.find("s:is", ns).itertext()) if c.get("t") == "inlineStr" else v
                    rows.append(values)
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
    if any(v.lstrip().startswith(("=", "+", "-", "@")) for row in rows for v in row):
        raise ReportError("Formula cells refuse")
    return rows


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
    result = []
    for n, row in enumerate(rows, 2):
        if kind == "xlsx" and len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        if len(row) != len(header):
            raise ReportError(f"Line {n}: wrong column count")
        line = {key: row[header.index(col)].strip() for key, col in mapping.items()}
        if not any(line.get(k) for k in ("reference", "awb", "order")) or any(
            ord(c) < 32 for v in line.values() for c in v
        ):
            raise ReportError(f"Line {n}: missing reference or control character")
        gross, fee = money(line["gross"]), money(line["fee"])
        if gross <= 0 or fee > gross or ("net" in line and money(line["net"]) != gross - fee):
            raise ReportError(f"Line {n}: invalid gross/fee/net")
        if "category" in line and line["category"] != "charge":
            raise ReportError(f"Line {n}: refunds, disputes and adjustments require review")
        result.append(line | {"gross": f"{gross:.2f}", "fee": f"{fee:.2f}"})
    batches = {r.get("batch") for r in result}
    if len(batches) != 1:
        raise ReportError("Exactly one payout required")
    return result
