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


# Currency words a report writes next to an amount or in its currency column, by ISO code.
CURRENCY_WORDS = {"RON": "RON", "LEI": "RON", "EUR": "EUR", "€": "EUR", "USD": "USD", "$": "USD"}
_WORD = "|".join(re.escape(w) for w in CURRENCY_WORDS)
_AMOUNT = re.compile(rf"(?i)(?:(?P<pre>{_WORD})\s*)?(?P<sign>[+-]?)\s*(?P<number>[\d.,' ]+?)\s*(?P<post>{_WORD})?")
_NUMBER = re.compile(
    r"(?P<int>\d{1,12}|\d{1,3}(?P<sep>[., '])\d{3}(?:(?P=sep)\d{3}){0,3})(?:(?P<dec>[.,])(?P<cents>\d{1,2}))?"
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
    m = _AMOUNT.fullmatch(text)
    n = _NUMBER.fullmatch(m["number"]) if m and not (m["pre"] and m["post"]) else None
    if (
        not n
        or (n["dec"] and n["sep"] == n["dec"])
        or (n["sep"] in (".", ",") and n["int"].count(n["sep"]) == 1 and not n["dec"])
    ):
        raise ReportError("Invalid amount")
    digits = re.sub(r"\D", "", n["int"]) + "." + (n["cents"] or "0")
    word = m["pre"] or m["post"] or ""
    return Decimal(m["sign"] + digits), currency_code(word) if word else ""


def money(value: str) -> Decimal:
    return amount(value)[0]


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
    # A cell starting with = + - @ is data here (a phone number, a signed fee, a placeholder): only XLSX formula nodes
    # refuse above. Writers that hand cells to a spreadsheet escape them (pack.summary_csv).
    return rows


def _money_line(n: int, line: dict[str, str]) -> dict[str, str]:
    """Gross, fee and net as exact cents, one currency per line. A fee written as a deduction (`-2,00`) is the same fee;
    an empty fee is zero."""
    gross, currencies = amount(line["gross"]), set()
    fee = amount(line["fee"]) if line["fee"] else (Decimal(0), "")
    net = amount(line["net"]) if line.get("net") else None
    for _, code in (gross, fee, net or (0, "")):
        currencies.add(code)
    if line.get("currency"):
        currencies.add(currency_code(line["currency"]))
    currencies.discard("")
    if len(currencies) > 1:
        raise ReportError("Mixed currencies")
    total, charge = gross[0], abs(fee[0])
    if total <= 0 or charge > total or (net is not None and net[0] != total - charge):
        raise ReportError(f"Line {n}: invalid gross/fee/net")
    if "category" in line and line["category"] != "charge":
        raise ReportError(f"Line {n}: refunds, disputes and adjustments require review")
    out = line | {"gross": f"{total:.2f}", "fee": f"{charge:.2f}"}
    if net is not None:
        out["net"] = f"{net[0]:.2f}"
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
        line = {k: "" if v in PLACEHOLDERS else v for k, v in line.items()}
        if not any(line.get(k) for k in ("reference", "awb", "order")) or any(
            ord(c) < 32 for v in line.values() for c in v
        ):
            raise ReportError(f"Line {n}: missing reference or control character")
        result.append(_money_line(n, line))
    batches = {r.get("batch") for r in result}
    if len(batches) != 1:
        raise ReportError("Exactly one payout required")
    return result
