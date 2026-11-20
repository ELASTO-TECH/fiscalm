"""Company-owned, immutable invoice logos. No filename or media-type is trusted."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import struct
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from threading import RLock
from typing import Literal, TypeVar

from fiscalm import companies
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.ledger import Ledger, finance_home

MAX_BYTES = 512 * 1024
INVALID = "Se acceptă o imagine PNG sau JPEG validă, fără animație."
DIMENSIONS = "Imaginea trebuie să aibă între 32 și 1200 de pixeli pe fiecare latură."
SIZE = "Imaginea poate avea cel mult 512 KB."
PATH = "Sigla nu poate fi citită în siguranță. Administratorul trebuie să verifice dosarul firmei pe server."
UNAVAILABLE = "Sigla nu poate fi citită: trebuie încărcată din nou sau eliminată."
ORIENTATION = "Imaginea este rotită prin metadate. Trebuie salvată fără rotire și încărcată din nou."
READY_VERSION = 2
MISSING = "Firma nu are siglă."


def validate(data: bytes) -> tuple[str, int, int]:
    """Accept bounded PNG/JPEG structures, refusing unsupported encodings and corrupt framing."""
    if len(data) > MAX_BYTES:
        raise InvoiceError(SIZE)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height, color, compressed = _png_chunks(data)
        kind = "png"
    elif data.startswith(b"\xff\xd8"):
        width, height = _jpeg(data)
        kind = "jpeg"
    else:
        raise InvoiceError(INVALID)
    if not 32 <= width <= 1200 or not 32 <= height <= 1200:
        raise InvoiceError(DIMENSIONS)
    if kind == "png":
        _png_pixels(width, height, color, compressed)
    return kind, width, height


def _png_chunks(data: bytes) -> tuple[int, int, int, bytearray]:
    pos, seen, ended = 8, [], False
    compressed = bytearray()
    while pos + 12 <= len(data):
        (n,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4 : pos + 8]
        end = pos + 12 + n
        if end > len(data) or not re.fullmatch(b"[A-Za-z]{4}", tag) or tag[2] & 32:
            raise InvoiceError(INVALID)
        raw = data[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack_from(">I", data, pos + 8 + n)
        if zlib.crc32(tag + raw) != crc or (not seen and tag != b"IHDR"):
            raise InvoiceError(INVALID)
        if tag == b"IHDR":
            if seen:
                raise InvoiceError(INVALID)
            width, height, color = _png_header(raw)
        elif tag == b"IDAT":
            if b"IDAT" in seen and seen[-1] != b"IDAT":
                raise InvoiceError(INVALID)
            compressed.extend(raw)
        elif tag == b"IEND":
            if n or b"IDAT" not in seen or end != len(data):
                raise InvoiceError(INVALID)
            ended = True
        elif tag in (b"tRNS", b"acTL", b"fcTL", b"fdAT") or not tag[0] & 32:
            # Palette is not used by the supported color types.
            raise InvoiceError(INVALID)
        seen.append(tag)
        pos = end
    if not ended:
        raise InvoiceError(INVALID)
    return width, height, color, compressed


def _png_header(raw: bytes) -> tuple[int, int, int]:
    if len(raw) != 13:
        raise InvoiceError(INVALID)
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", raw)
    if depth != 8 or color not in (0, 2, 4, 6) or compression or filtering or interlace:
        raise InvoiceError(INVALID)
    return width, height, color


def _png_pixels(width: int, height: int, color: int, compressed: bytearray) -> None:
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    stride = 1 + width * channels
    expected = stride * height
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected + 1)
        if (
            len(pixels) != expected
            or not decoder.eof
            or decoder.unused_data
            or any(pixels[p] > 4 for p in range(0, expected, stride))
        ):
            raise InvoiceError(INVALID)
    except zlib.error:
        raise InvoiceError(INVALID) from None


def _jpeg(data: bytes) -> tuple[int, int]:
    width = height = 0
    pos, frame, scan, ended = 2, False, False, False
    tables: set[int] = set()
    ids: set[int] = set()
    while pos < len(data):
        marker, pos = _jpeg_marker(data, pos)
        if marker is None:
            break
        if marker == 0xD9:
            ended = pos == len(data) and frame and scan and tables == {0xDB, 0xC4}
            break
        raw, n = _jpeg_segment(data, pos, marker)
        if marker in (0xDB, 0xC4):
            if not raw:
                raise InvoiceError(INVALID)
            tables.add(marker)
        if marker == 0xE1 and raw.startswith(b"Exif\0\0"):
            _orientation(raw[6:])
        if marker in (0xC0, 0xC1, 0xC2):
            if frame:
                raise InvoiceError(INVALID)
            width, height, ids = _jpeg_frame(raw)
            frame = True
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4,):
            raise InvoiceError(INVALID)
        pos += n
        if marker == 0xDA:
            pos = _jpeg_scan(data, pos, raw, frame, ids)
            scan = True
    if not ended or ids == {ord("R"), ord("G"), ord("B")}:
        raise InvoiceError(INVALID)
    return width, height


def _jpeg_segment(data: bytes, pos: int, marker: int) -> tuple[bytes, int]:
    if marker in (0, 0xD8, 0xDE, 0xDF) or 0xD0 <= marker <= 0xD7 or pos + 2 > len(data):
        raise InvoiceError(INVALID)
    n = int.from_bytes(data[pos : pos + 2], "big")
    if n < 2 or pos + n > len(data):
        raise InvoiceError(INVALID)
    raw = data[pos + 2 : pos + n]
    return raw, n


def _jpeg_marker(data: bytes, pos: int) -> tuple[int | None, int]:
    if data[pos] != 255:
        raise InvoiceError(INVALID)
    while pos < len(data) and data[pos] == 255:
        pos += 1
    if pos >= len(data):
        return None, pos
    marker = data[pos]
    pos += 1
    return marker, pos


def _jpeg_frame(raw: bytes) -> tuple[int, int, set[int]]:
    if len(raw) < 6:
        raise InvoiceError(INVALID)
    depth, height, width, components = struct.unpack(">BHHB", raw[:6])
    if depth != 8 or components not in (1, 3) or len(raw) != 6 + 3 * components:
        raise InvoiceError(INVALID)
    ids = set(raw[6::3])
    if len(ids) != components:
        raise InvoiceError(INVALID)
    return width, height, ids


def _jpeg_scan(data: bytes, pos: int, raw: bytes, frame: bool, ids: set[int]) -> int:
    if not frame or not raw or raw[0] not in (1, 3) or len(raw) != 4 + 2 * raw[0]:
        raise InvoiceError(INVALID)
    scan_ids = raw[1:-3:2]
    if len(set(scan_ids)) != len(scan_ids) or not set(scan_ids) <= ids:
        raise InvoiceError(INVALID)
    while pos < len(data):
        if data[pos] == 255:
            if pos + 1 >= len(data):
                break
            if data[pos + 1] == 0 or 0xD0 <= data[pos + 1] <= 0xD7:
                pos += 2
                continue
            break
        pos += 1
    return pos


@contextmanager
def directory(co: str, *, create: bool = False) -> Iterator[int]:
    """Pin the company's own root and branding directory without following symlinks."""
    if co not in companies.all() or not companies.KEY_RE.fullmatch(co):
        raise InvoiceError(MISSING)
    fd = os.open(finance_home().resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for segment in (co, "branding"):
            if create:
                with suppress(FileExistsError):
                    os.mkdir(segment, 0o700, dir_fd=fd)
            child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        expected = finance_home().resolve() / co / "branding"
        if expected.resolve() != expected or not os.path.samestat(
            os.fstat(fd), os.stat(expected, follow_symlinks=False)
        ):
            raise InvoiceError(PATH)
        yield fd
    finally:
        os.close(fd)


_POINTER = "invoice_logo:"  # meta key prefix: the company's current logo sha, empty once removed


def current_sha(L: Ledger, co: str) -> str | None:
    if co not in companies.all(L):
        raise InvoiceError(MISSING)
    return L.get_meta(_POINTER + co) or None


def current_shas(L: Ledger) -> dict[str, str]:
    """The current logo sha of every company that has one (the company switch; state's request reads cache meta)."""
    return {co: sha for co in companies.all(L) if (sha := L.get_meta(_POINTER + co))}


@dataclass(frozen=True)
class Logo:
    """PDF-ready immutable logo, prepared at upload, cached by cleaned-original sha.

    kind is png/jpeg; width/height are 32..1200; components is 1 (gray) or 3
    (RGB); bits is always 8. JPEG data is cleaned DCT bytes for /DCTDecode,
    alpha=None. PNG data and alpha use PNG row filters with /FlateDecode and Predictor 15
    when predictor is true; otherwise they are unfiltered legacy samples.
    sha is SHA-256 of the cleaned original, not necessarily the uploaded bytes.
    current_logo selects the current pointer; logo_for requires an explicit sha.
    A legacy invoice with no sha must skip logo_for and render without a logo.
    """

    kind: Literal["png", "jpeg"]
    width: int
    height: int
    components: Literal[1, 3]
    bits: Literal[8]
    data: bytes
    alpha: bytes | None
    sha: str
    predictor: bool = False


def _orientation(tiff: bytes) -> None:
    try:
        endian = {b"II": "<", b"MM": ">"}[tiff[:2]]
        if struct.unpack_from(endian + "H", tiff, 2)[0] != 42:
            raise ValueError
        offset = struct.unpack_from(endian + "I", tiff, 4)[0]
        count = struct.unpack_from(endian + "H", tiff, offset)[0]
        for n in range(count):
            tag, kind, size, value = struct.unpack_from(endian + "HHI4s", tiff, offset + 2 + 12 * n)
            if tag == 0x112 and (kind != 3 or size != 1 or struct.unpack(endian + "H", value[:2])[0] != 1):
                raise InvoiceError(ORIENTATION)
    except InvoiceError:
        raise
    except (KeyError, ValueError, struct.error):
        raise InvoiceError(INVALID) from None


def _clean(data: bytes, kind: str) -> bytes:
    if kind == "png":
        result, pos = bytearray(data[:8]), 8
        while pos < len(data):
            n = int.from_bytes(data[pos : pos + 4], "big")
            if data[pos + 4 : pos + 8] in (b"IHDR", b"IDAT", b"IEND"):
                result.extend(data[pos : pos + 12 + n])
            pos += 12 + n
        return bytes(result)
    result, pos = bytearray(data[:2]), 2
    while pos < len(data):
        start = pos
        while data[pos] == 255:
            pos += 1
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            result.extend(data[start:pos])
            break
        n = int.from_bytes(data[pos : pos + 2], "big")
        raw = data[pos + 2 : pos + n]
        pos += n
        if marker != 0xFE and (not 0xE0 <= marker <= 0xEF or (marker == 0xEE and raw.startswith(b"Adobe"))):
            result.extend(data[start:pos])
        if marker == 0xDA:
            start = pos
            while pos < len(data):
                if data[pos] == 255:
                    if data[pos + 1] == 0 or 0xD0 <= data[pos + 1] <= 0xD7:
                        pos += 2
                        continue
                    break
                pos += 1
            result.extend(data[start:pos])
    return bytes(result)


def _jpeg_components(data: bytes) -> Literal[1, 3]:
    pos = 2
    while True:
        while data[pos] == 255:
            pos += 1
        marker = data[pos]
        pos -= 1
        n = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return 1 if data[pos + 9] == 1 else 3
        pos += 2 + n


def _components(data: bytes, kind: str) -> Literal[1, 3]:
    return (1 if data[25] in (0, 4) else 3) if kind == "png" else _jpeg_components(data)


def _filtered(pixels: bytes, width: int, channels: int) -> bytes:
    """Choose the smallest signed-residual row among none, sub, up and paeth."""
    stride = width * channels
    previous = bytes(stride)
    output = bytearray()
    cache: dict[tuple[bytes, bytes], bytes] = {}
    for start in range(0, len(pixels), stride):
        row = pixels[start : start + stride]
        key = (previous, row)
        encoded = cache.get(key)
        if encoded is None:
            sub, up, paeth = bytearray(), bytearray(), bytearray()
            for x, value in enumerate(row):
                a = row[x - channels] if x >= channels else 0
                b = previous[x]
                c = previous[x - channels] if x >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                prediction = a if pa <= pb and pa <= pc else b if pb <= pc else c
                sub.append((value - a) & 255)
                up.append((value - b) & 255)
                paeth.append((value - prediction) & 255)
            candidates = [(0, row), (1, sub), (2, up), (4, paeth)]
            filt, residual = min(candidates, key=lambda pair: sum(min(v, 256 - v) for v in pair[1]))
            encoded = bytes([filt]) + bytes(residual)
            if len(cache) >= 8:
                cache.clear()
            cache[key] = encoded
        output.extend(encoded)
        previous = row
    return zlib.compress(output)


def _prepare(data: bytes, kind: str, width: int, height: int, sha: str) -> Logo:
    if kind == "jpeg":
        return Logo("jpeg", width, height, _components(data, kind), 8, data, None, sha)
    color = data[25]
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    compressed, pos = bytearray(), 8
    while pos < len(data):
        n = int.from_bytes(data[pos : pos + 4], "big")
        if data[pos + 4 : pos + 8] == b"IDAT":
            compressed.extend(data[pos + 8 : pos + 8 + n])
        pos += 12 + n
    if color in (0, 2):
        return Logo("png", width, height, _components(data, kind), 8, bytes(compressed), None, sha, predictor=True)
    # qpdf's native PNG predictor decoder avoids a Python loop over every sample.
    # validate() has already bounded the inflated size and checked every filter byte.
    import pikepdf

    with pikepdf.Pdf.new() as pdf:
        stream = pdf.make_stream(
            bytes(compressed),
            Filter=pikepdf.Name.FlateDecode,
            DecodeParms=pikepdf.Dictionary(Predictor=15, Colors=channels, Columns=width, BitsPerComponent=8),
        )
        pixels = stream.read_bytes()
    alpha = None
    if color in (4, 6):
        alpha = _filtered(pixels[channels - 1 :: channels], width, 1)
        colour = bytearray(width * height * (channels - 1))
        for channel in range(channels - 1):
            colour[channel :: channels - 1] = pixels[channel::channels]
    else:
        colour = pixels
    return Logo(
        "png",
        width,
        height,
        _components(data, kind),
        8,
        _filtered(bytes(colour), width, channels - 1),
        alpha,
        sha,
        predictor=True,
    )


def _write(fd: int, name: str, data: bytes) -> None:
    temp = ".logo-" + secrets.token_hex(16)
    try:
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=fd)


def _derived(logo: Logo) -> bytes:
    encoded = json.dumps(
        dict(
            v=READY_VERSION,
            sha=logo.sha,
            kind=logo.kind,
            predictor=logo.predictor,
            width=logo.width,
            height=logo.height,
            components=logo.components,
            bits=logo.bits,
            data=base64.b64encode(logo.data).decode(),
            alpha=base64.b64encode(logo.alpha).decode() if logo.alpha else None,
        )
    ).encode()
    return hashlib.sha256(encoded).hexdigest().encode() + b"\n" + encoded


def set_logo(L: Ledger, co: str, data: bytes) -> None:
    kind, width, height = validate(data)
    data = _clean(data, kind)
    sha = hashlib.sha256(data).hexdigest()
    logo = _prepare(data, kind, width, height, sha)
    try:
        with directory(co, create=True) as fd:
            # Never remove another concurrent uploader's active temporary file.
            for name in os.listdir(fd):
                try:
                    if (
                        name.startswith(".logo-")
                        and os.stat(name, dir_fd=fd, follow_symlinks=False).st_mtime < time.time() - 3600
                    ):
                        os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
            _write(fd, f"logo-{sha}.pdf-ready", _derived(logo))
            _write(fd, f"logo-{sha}.{'jpg' if kind == 'jpeg' else 'png'}", data)
        L.set_meta(_POINTER + co, sha)
    except OSError:
        raise InvoiceError(PATH) from None


def remove_logo(L: Ledger, co: str) -> None:
    current_sha(L, co)
    L.set_meta(_POINTER + co, "")


def _read(fd: int, name: str, limit: int, signature: list[int] | None = None) -> bytes:
    image = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(image, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise InvoiceError(INVALID)
        if signature is not None:
            signature.extend((info.st_size, info.st_mtime_ns))
        return stream.read(limit + 1)


_T = TypeVar("_T")
_validation_cache: OrderedDict[tuple, tuple[str, int, int]] = OrderedDict()
_prepared_cache: OrderedDict[tuple, Logo] = OrderedDict()
_cache_lock = RLock()


def _cached(cache: OrderedDict[tuple, _T], key: tuple, load: Callable[[], _T]) -> _T:
    with _cache_lock:
        if key not in cache:
            cache[key] = load()
            if len(cache) > 64:
                cache.popitem(last=False)
        cache.move_to_end(key)
        return cache[key]


def _loaded(data: bytes, derived: bytes, sha: str, kind: str, width: int, height: int) -> Logo:
    # Verify the checksum and bound the streams before handing them to the PDF writer.
    digest, encoded = derived.split(b"\n", 1)
    if hashlib.sha256(encoded).hexdigest().encode() != digest:
        raise ValueError("Invalid prepared checksum")
    payload = json.loads(encoded)
    components = _components(data, kind)
    expected_header = dict(
        v=READY_VERSION,
        sha=sha,
        kind=kind,
        width=width,
        height=height,
        components=components,
        bits=8,
        predictor=kind == "png",
    )
    if not isinstance(payload, dict) or any(
        type(payload.get(key)) is not type(value) or payload.get(key) != value for key, value in expected_header.items()
    ):
        raise ValueError("Prepared identity mismatch")
    colour = base64.b64decode(payload["data"], validate=True)
    alpha = base64.b64decode(payload["alpha"], validate=True) if payload["alpha"] else None
    if kind == "png":
        for compressed, expected in ((colour, height * (1 + width * components)), (alpha, height * (1 + width))):
            if compressed is not None:
                decoder = zlib.decompressobj()
                samples = decoder.decompress(compressed, expected + 1)
                if (
                    len(samples) != expected
                    or not decoder.eof
                    or decoder.unused_data
                    or any(samples[n] > 4 for n in range(0, expected, expected // height))
                ):
                    raise ValueError("Invalid prepared sample count")
        if (data[25] in (4, 6)) != (alpha is not None):
            raise ValueError("Invalid alpha")
    elif colour != data or alpha is not None:
        raise ValueError("Invalid DCT data")
    return Logo(kind, width, height, components, 8, colour, alpha, sha, predictor=kind == "png")


def original(L: Ledger, co: str, sha: str) -> bytes:
    """Return validated cleaned browser bytes, never PDF sample streams."""
    if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
        raise InvoiceError(MISSING)
    with directory(co) as fd:
        for kind in ("png", "jpg"):
            signature: list[int] = []
            try:
                data = _read(fd, f"logo-{sha}.{kind}", MAX_BYTES, signature)
            except FileNotFoundError:
                continue
            if hashlib.sha256(data).hexdigest() != sha:
                raise InvoiceError(INVALID)
            actual, _, _ = _cached(_validation_cache, (sha, *signature), lambda data=data: validate(data))
            if actual != ("jpeg" if kind == "jpg" else kind):
                raise InvoiceError(INVALID)
            return data
    raise InvoiceError(MISSING)


def current_logo(L: Ledger, co: str) -> Logo | None:
    sha = current_sha(L, co)
    return logo_for(L, co, sha) if sha else None


def logo_for(L: Ledger, co: str, sha: str) -> Logo | None:
    """Read only this company's explicit immutable sha; missing/corrupt storage raises.

    See Logo for the PDF stream contract. Never pass a legacy invoice's null sha.
    Validations are cached by sha, size and mtime_ns after checking the sha.
    Persisted PDF streams are reused across processes and recomputed if missing/corrupt. Reads always pin safe paths.
    """
    if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
        raise InvoiceError(MISSING)
    try:
        with directory(co) as fd:
            for kind in ("png", "jpg"):
                signature: list[int] = []
                try:
                    data = _read(fd, f"logo-{sha}.{kind}", MAX_BYTES, signature)
                except FileNotFoundError:
                    continue
                if hashlib.sha256(data).hexdigest() != sha:
                    raise InvoiceError(INVALID)
                actual, width, height = _cached(_validation_cache, (sha, *signature), lambda data=data: validate(data))
                if actual != ("jpeg" if kind == "jpg" else kind):
                    raise InvoiceError(INVALID)
                try:
                    ready_signature: list[int] = []
                    derived = _read(fd, f"logo-{sha}.pdf-ready", 20 * 1024 * 1024, ready_signature)
                    return _cached(
                        _prepared_cache,
                        (sha, *signature, *ready_signature),
                        lambda actual=actual, data=data, derived=derived, height=height, width=width: _loaded(
                            data, derived, sha, actual, width, height
                        ),
                    )
                except (OSError, ValueError, KeyError, TypeError, zlib.error, InvoiceError):
                    logo = _prepare(data, actual, width, height, sha)
                    try:
                        _write(fd, f"logo-{sha}.pdf-ready", _derived(logo))
                    except OSError as error:
                        _log_once(co, error, "Invoice logo cache not saved")
                    return logo
    except OSError:
        raise InvoiceError(PATH) from None
    raise InvoiceError(MISSING)


_logged: set[tuple[str, str, str]] = set()


def _log_once(co: str, error: Exception, message: str) -> None:
    key = (str(finance_home()), co, message + type(error).__name__)
    if key not in _logged:
        _logged.add(key)
        logging.getLogger(__name__).warning("%s: %s", message, type(error).__name__)


def metadata(L: Ledger, co: str) -> dict | None:
    """Never let unavailable logo storage break the workspace; log only error type once."""
    try:
        logo = current_logo(L, co)
        if logo is None:
            return None
        return dict(
            sha=logo.sha, type="image/png" if logo.kind == "png" else "image/jpeg", width=logo.width, height=logo.height
        )
    except Exception as error:
        # Workspace projection must survive decoder/storage failures; log no input data.
        _log_once(co, error, "Invoice logo unavailable")
        return None


def issue_sha(L: Ledger, co: str, *, storno: bool = False) -> str | None:
    try:
        logo = current_logo(L, co)
        return logo.sha if logo else None
    except Exception:
        # Any decoder failure must refuse issuance with an actionable, secret-free message.
        raise InvoiceError(
            ("Factura de stornare" if storno else "Factura") + " nu a fost emisă. " + UNAVAILABLE
        ) from None
