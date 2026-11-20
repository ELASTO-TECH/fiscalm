"""Small deterministic PDF canvas for the invoice's embedded fonts and raster logo."""

from __future__ import annotations

import hashlib
import io
import re
import struct
from functools import lru_cache
from pathlib import Path

import pikepdf
from fontTools import subset
from fontTools.ttLib import TTFont
from pikepdf import Array, Dictionary, Name, Stream

from fiscalm.invoicing.branding import Logo
from fiscalm.invoicing.errors import InvoiceError

INK = "#0F172A"
BODY = "#334155"
MUTED = "#64748B"
RULE = "#E2E8F0"
TINT = "#F8FAFC"


@lru_cache(maxsize=3)
def _unshaped(weight: str) -> bytes:
    """The embedded font without its OpenType layout tables, cached for the process (two first renders at once
    may both build it; the bytes are the same).

    The PDF positions every glyph itself and never shapes text, so GSUB, GPOS and GDEF carry nothing it uses;
    parsing them on each subset was most of an invoice's render time. Outlines, metrics and cmap are unchanged.
    """
    tt = TTFont(io.BytesIO(_font(weight)[0]), recalcTimestamp=False)
    for tag in ("GSUB", "GPOS", "GDEF"):
        if tag in tt:
            del tt[tag]
    buf = io.BytesIO()
    tt.save(buf)
    return buf.getvalue()


def color(hex_value: str) -> str:
    return " ".join(f"{int(hex_value[n : n + 2], 16) / 255:.5f}" for n in (1, 3, 5))


class Canvas:
    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.used: dict[str, set[int]] = {w: set() for w in ("Regular", "Medium", "SemiBold")}

    def measure(self, value: str, size: float, weight: str = "Regular", tracking: float = 0) -> float:
        value = clean_text(value)
        widths = _font(weight)[2]
        return sum(widths.get(ord(c), 1000) for c in value) * size / 1000 + max(0, len(value) - 1) * tracking

    def text(
        self,
        value: str,
        x: float,
        y: float,
        size: float = 8.25,
        weight: str = "Regular",
        fill: str = BODY,
        right: float | None = None,
        width: float | None = None,
        tracking: float = 0,
    ) -> None:
        value = " ".join(clean_text(str(value)).split())
        if any(not _font(weight)[1].get(ord(c)) for c in value):
            raise InvoiceError("Factura conține caractere care nu pot fi afișate în PDF.")
        measured = self.measure(value, size, weight, tracking)
        if width and measured > width:
            size = max(6, size * width / measured)
        if right is not None:
            x = right - self.measure(value, size, weight, tracking)
        self.used[weight].update(map(ord, value))
        font = {"Regular": "F1", "Medium": "F2", "SemiBold": "F3"}[weight]
        self.pages[-1].append(
            f"{color(fill)} rg BT /{font} {size} Tf {tracking} Tc 1 0 0 1 {x} {y} Tm <{value.encode('utf-16-be').hex()}> Tj ET"
        )

    def ellipsis(self, value: str, x: float, y: float, size: float, weight: str, fill: str, width: float) -> None:
        value = clean_text(value)
        if self.measure(value, size, weight) > width:
            while value and self.measure(value + "…", size, weight) > width:
                value = value[:-1]
            value += "…"
        self.text(value, x, y, size, weight, fill)

    def wrap(self, value: str, width: float, size: float = 8.25, weight: str = "Regular") -> list[str]:
        rows, current = [], ""
        for word in clean_text(str(value)).split():
            if self.measure((current + " " + word).strip(), size, weight) <= width:
                current = (current + " " + word).strip()
                continue
            if current:
                rows.append(current)
            current = ""
            if re.fullmatch(r"[+-]?[0-9][0-9.,/:%+-]*", word) and self.measure(word, size, weight) <= width:
                current = word
                continue
            for char in word:
                if current and self.measure(current + char, size, weight) > width:
                    rows.append(current)
                    current = ""
                current += char
        return rows + ([current] if current else [""])

    def rule(self, x: float, y: float, end: float, fill: str = RULE, width: float = 0.75) -> None:
        self.pages[-1].append(f"{color(fill)} RG {width} w {x} {y} m {end} {y} l S")

    def rect(self, x: float, y: float, w: float, h: float, fill: str) -> None:
        self.pages[-1].append(f"{color(fill)} rg {x} {y} {w} {h} re f")

    def save(self, logo: Logo | None = None) -> bytes:
        pdf = pikepdf.Pdf.new()
        fonts = Dictionary()
        for weight, used in self.used.items():
            if not used:
                continue
            widths = _font(weight)[2]
            tt = TTFont(io.BytesIO(_unshaped(weight)), recalcTimestamp=False)
            options = subset.Options()
            options.recalc_timestamp = False
            options.layout_features = []
            sub = subset.Subsetter(options=options)
            sub.populate(unicodes=sorted(used))
            sub.subset(tt)
            buf = io.BytesIO()
            tt.save(buf, reorderTables=True)
            glyphs = {c: tt.getGlyphID(g) for c, g in tt.getBestCmap().items()}
            tag = "".join(chr(65 + b % 26) for b in hashlib.sha256(buf.getvalue()).digest()[:6])
            name = Name("/" + tag + "+Inter-" + weight)
            head = tt["head"]
            bbox = [round(getattr(head, k) * 1000 / head.unitsPerEm) for k in ("xMin", "yMin", "xMax", "yMax")]
            ff = Stream(pdf, buf.getvalue())
            ff.Length1 = len(buf.getvalue())
            desc = pdf.make_indirect(
                Dictionary(
                    Type=Name.FontDescriptor,
                    FontName=name,
                    Flags=32,
                    FontBBox=Array(bbox),
                    ItalicAngle=0,
                    Ascent=969,
                    Descent=-241,
                    CapHeight=728,
                    StemV=80,
                    FontFile2=ff,
                )
            )
            mapping = bytearray((max(used) + 1) * 2)
            for c in used:
                struct.pack_into(">H", mapping, c * 2, glyphs[c])
            cid = pdf.make_indirect(
                Dictionary(
                    Type=Name.Font,
                    Subtype=Name.CIDFontType2,
                    BaseFont=name,
                    CIDSystemInfo=Dictionary(Registry="Adobe", Ordering="Identity", Supplement=0),
                    FontDescriptor=desc,
                    CIDToGIDMap=Stream(pdf, bytes(mapping)),
                    W=Array([v for c in sorted(used) for v in (c, Array([widths[c]]))]),
                )
            )
            cmap = [
                "/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
                "/CMapName /Unicode def /CMapType 2 def",
                "1 begincodespacerange <0000> <FFFF> endcodespacerange",
            ]
            codes = sorted(used)
            for n in range(0, len(codes), 100):
                chunk = codes[n : n + 100]
                cmap += [f"{len(chunk)} beginbfchar"] + [f"<{c:04X}> <{c:04X}>" for c in chunk] + ["endbfchar"]
            cmap += ["endcmap CMapName currentdict /CMap defineresource pop end end"]
            fonts[Name("/" + {"Regular": "F1", "Medium": "F2", "SemiBold": "F3"}[weight])] = pdf.make_indirect(
                Dictionary(
                    Type=Name.Font,
                    Subtype=Name.Type0,
                    BaseFont=name,
                    Encoding=Name("/Identity-H"),
                    DescendantFonts=Array([cid]),
                    ToUnicode=Stream(pdf, "\n".join(cmap).encode()),
                )
            )
        image = _image(pdf, logo) if logo else None
        for commands in self.pages:
            page = pdf.add_blank_page(page_size=(595, 842))
            page.Resources = Dictionary(Font=fonts)
            if any("/Draft gs" in command for command in commands):
                page.Resources.ExtGState = Dictionary(Draft=Dictionary(Type=Name.ExtGState, ca=0.08, CA=0.08))
            if image is not None:
                page.Resources.XObject = Dictionary(Logo=image)
            page.Contents = Stream(pdf, "\n".join(commands).encode())
        out = io.BytesIO()
        pdf.save(out, deterministic_id=True, min_version="1.4")
        return out.getvalue()


def _image(pdf: pikepdf.Pdf, logo: Logo) -> Stream:
    """Embed the upload-time prepared samples without decoding them again."""
    im = Stream(pdf, b"")
    im.Type, im.Subtype = Name.XObject, Name.Image
    im.Width, im.Height, im.BitsPerComponent = logo.width, logo.height, logo.bits
    im.ColorSpace = Name.DeviceGray if logo.components == 1 else Name.DeviceRGB
    im.write(logo.data, filter=Name.DCTDecode if logo.kind == "jpeg" else Name.FlateDecode)
    if logo.predictor:
        im.DecodeParms = Dictionary(Predictor=15, Colors=logo.components, BitsPerComponent=8, Columns=logo.width)
    if logo.alpha is not None:
        mask = Stream(pdf, b"")
        mask.Type, mask.Subtype, mask.ColorSpace = Name.XObject, Name.Image, Name.DeviceGray
        mask.Width, mask.Height, mask.BitsPerComponent = logo.width, logo.height, logo.bits
        mask.write(logo.alpha, filter=Name.FlateDecode)
        if logo.predictor:
            mask.DecodeParms = Dictionary(Predictor=15, Colors=1, BitsPerComponent=8, Columns=logo.width)
        im.SMask = mask
    return im


FONT = Path(__file__).with_name("fonts") / "inter"


@lru_cache(maxsize=3)
def _font(weight: str = "Regular") -> tuple[bytes, dict[int, int], dict[int, int]]:
    raw = (FONT / f"Inter-{weight}.ttf").read_bytes()
    tables = {}
    for n in range(struct.unpack_from(">H", raw, 4)[0]):
        tag, _, offset, length = struct.unpack_from(">4sIII", raw, 12 + n * 16)
        tables[tag] = raw[offset : offset + length]
    cmap, glyphs = tables[b"cmap"], {}
    for n in range(struct.unpack_from(">H", cmap, 2)[0]):
        platform, encoding, offset = struct.unpack_from(">HHI", cmap, 4 + n * 8)
        if platform != 3 or encoding != 1 or struct.unpack_from(">H", cmap, offset)[0] != 4:
            continue
        data = cmap[offset:]
        count = struct.unpack_from(">H", data, 6)[0] // 2
        for k in range(count):
            end = struct.unpack_from(">H", data, 14 + k * 2)[0]
            start = struct.unpack_from(">H", data, 16 + count * 2 + k * 2)[0]
            delta = struct.unpack_from(">h", data, 16 + count * 4 + k * 2)[0]
            loc = 16 + count * 6 + k * 2
            shift = struct.unpack_from(">H", data, loc)[0]
            for code in range(start, min(end, 65534) + 1):
                gid = struct.unpack_from(">H", data, loc + shift + 2 * (code - start))[0] if shift else code
                glyphs[code] = ((gid + delta) & 65535) if gid else 0
        break
    units = struct.unpack_from(">H", tables[b"head"], 18)[0]
    metrics = struct.unpack_from(">H", tables[b"hhea"], 34)[0]
    widths = {
        code: round(struct.unpack_from(">H", tables[b"hmtx"], min(gid, metrics - 1) * 4)[0] * 1000 / units)
        for code, gid in glyphs.items()
    }
    return raw, glyphs, widths


def clean_text(value: str) -> str:
    """Drop invisible formatting controls before measuring, validating or printing."""
    return "".join(
        c
        for c in value
        if ord(c) not in {0xAD, *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x206A), 0xFEFF}
    )


def validate_text(value: object) -> None:
    """Refuse text the invoice PDF cannot print before reserving a number."""
    if isinstance(value, str):
        glyphs = _font()[1]
        if any(not glyphs.get(ord(c)) for c in clean_text(value)):
            raise InvoiceError("Factura conține caractere care nu pot fi afișate în PDF.")
    elif isinstance(value, dict):
        for item in value.values():
            validate_text(item)
    elif isinstance(value, list):
        for item in value:
            validate_text(item)
