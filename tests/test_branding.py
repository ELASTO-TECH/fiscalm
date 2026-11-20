"""Synthetic logos only; all image bytes are constructed here with the stdlib."""

import hashlib
import http.client
import json
import os
import struct
import time
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import pikepdf
import pytest
from test_api import _api, _req, server as server  # noqa: F401
from test_invoicing import (
    draft,  # noqa: F401
    setup_invoice as setup_invoice,
)

from fiscalm import cli
from fiscalm.api import state as api
from fiscalm.invoicing import branding as b, engine as i, workspace as invoicing_api
from fiscalm.ledger import Ledger


def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def png(width=32, height=32, depth=8, color=2, interlace=0):
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color, 1)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace))
        + chunk(b"IDAT", zlib.compress((b"\0" * (1 + width * channels)) * height))
        + chunk(b"IEND", b"")
    )


def with_idat(raw, color=2):
    """png() with its pixel data replaced by `raw` (row filter bytes included), zlib-compressed."""
    return png(color=color)[:33] + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def seg(marker, raw):
    return bytes([255, marker]) + struct.pack(">H", len(raw) + 2) + raw


DHT = seg(0xC4, b"\0" + bytes([1]) + bytes(15) + b"\0")


def jpeg(marker=0xC0, width=32, height=32, components=3, depth=8, ids=None):
    ids = ids or bytes(range(1, components + 1))
    frame = struct.pack(">BHHB", depth, height, width, components) + b"".join(bytes([n, 0x11, 0]) for n in ids)
    scan = bytes([components]) + b"".join(bytes([n, 0]) for n in ids) + b"\0\x3f\0"
    # A structural JPEG scan: validation deliberately does not decode DCT samples.
    return (
        b"\xff\xd8"
        + seg(0xDB, b"\0" + bytes([1]) * 64)
        + DHT
        + seg(marker, frame)
        + seg(0xDA, scan)
        + b"\x01\xff\x00\x02\xff\xd9"
    )


def exif(orientation):
    return seg(
        0xE1, b"Exif\0\0II" + struct.pack("<HIH", 42, 8, 1) + struct.pack("<HHIHHI", 0x112, 3, 1, orientation, 0, 0)
    )


def in_jpeg(*segments):
    return jpeg()[:2] + b"".join(segments) + jpeg()[2:]


def in_png(*chunks):
    return png()[:33] + b"".join(chunks) + png()[33:]


def logo_file(home, sha, ext="png"):
    return home / "NORD" / "branding" / f"logo-{sha}.{ext}"


def ready_path(home, sha):
    return logo_file(home, sha, "pdf-ready")


def decoded(logo, alpha=False):
    with pikepdf.Pdf.new() as pdf:
        stream = pdf.make_stream(
            logo.alpha if alpha else logo.data,
            Filter=pikepdf.Name.FlateDecode,
            DecodeParms=pikepdf.Dictionary(
                Predictor=15, Colors=1 if alpha else logo.components, Columns=logo.width, BitsPerComponent=8
            ),
        )
        return stream.read_bytes()


@pytest.fixture
def prepares(monkeypatch):
    """Counts PDF preparations; tests clear it after their own uploads."""
    calls, prepare = [], b._prepare
    monkeypatch.setattr(b, "_prepare", lambda *args: calls.append(1) or prepare(*args))
    return calls


def issue_or_storno(setup_invoice, storno):
    """A draft to issue, or an issued invoice to reverse: its id and the call that does it."""
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    if not storno:
        return iid, lambda: i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    iid = i.issue(L, iid, "2026-10-31", company="NORD", version=1)["id"]
    return iid, lambda: i.storno(L, iid, "2026-11-01", "Corecție", company="NORD")


@pytest.mark.parametrize("data,components", [(jpeg(), 3), (jpeg(0xC1, components=1), 1), (jpeg(0xC2), 3)])
def test_accept_jpeg(ledger, home, data, components):
    b.set_logo(ledger, "NORD", data)
    logo = b.current_logo(ledger, "NORD")
    assert b.original(ledger, "NORD", logo.sha) == logo.data == data and logo.alpha is None
    assert (logo.width, logo.height, logo.components) == (32, 32, components)
    assert logo_file(home, hashlib.sha256(data).hexdigest(), "jpg").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "data,message",
    [
        (png()[:30], b.INVALID),
        (png()[:8] + chunk(b"IHDR", b""), b.INVALID),
        (png()[:-1], b.INVALID),
        (png()[:-1] + b"X", b.INVALID),
        (png() + b"extra", b.INVALID),
        (png(interlace=1), b.INVALID),
        (png(depth=16), b.INVALID),
        (png(color=3), b.INVALID),
        (in_png(chunk(b"acTL", struct.pack(">II", 1, 0))), b.INVALID),
        (in_png(chunk(b"tRNS", bytes(6))), b.INVALID),
        (with_idat(bytes(10_000_000)), b.INVALID),  # inflates past the declared size
        (with_idat((b"\x05" + bytes(96)) * 32), b.INVALID),  # unknown row filter
        (b"<svg/>", b.INVALID),
        (b"x" * (b.MAX_BYTES + 1), b.SIZE),
        (png(width=31), b.DIMENSIONS),
        (png(height=1201), b.DIMENSIONS),
        (jpeg(height=1201), b.DIMENSIONS),
        (jpeg()[:-1], b.INVALID),
        (b"\xff\xd8\xff\xc0\x00\x10", b.INVALID),
        (jpeg(0xC2, depth=12), b.INVALID),
        (jpeg(components=4), b.INVALID),
        (in_jpeg(seg(0xC0, struct.pack(">BHHB", 8, 32, 32, 1) + b"\x01\x11\x00")), b.INVALID),
        (in_jpeg(seg(0xDE, b"\0")), b.INVALID),
        (jpeg().replace(DHT, b""), b.INVALID),
        (jpeg().replace(b"\xff\xdb\0C" + b"\0" + bytes([1]) * 64, b""), b.INVALID),  # no quantization table
        (
            jpeg().replace(b"\xff\xda\0\x0c\x03\x01", b"\xff\xda\0\x0c\x03\x04"),
            b.INVALID,
        ),  # scan names no frame component
        (jpeg(ids=b"RGB"), b.INVALID),  # RGB without an Adobe marker
        (in_jpeg(exif(6)), b.ORIENTATION),
    ],
)
def test_refuse(ledger, data, message):
    with pytest.raises(i.InvoiceError) as error:
        b.set_logo(ledger, "NORD", data)
    assert str(error.value) == message
    assert b.current_sha(ledger, "NORD") is None


@pytest.mark.parametrize(
    "data,clean",
    [
        (in_png(chunk(b"tEXt", b"private GPS"), chunk(b"iCCP", b"profile\0\0" + zlib.compress(b"private"))), png()),
        (
            in_jpeg(
                exif(1),
                seg(0xED, b"private GPS"),
                seg(0xFE, b"private comment"),
                seg(0xE0, b"JFIF\0" + bytes(9) + b"private thumbnail"),
                seg(0xE2, b"ICC_PROFILE\0private profile"),
            ),
            jpeg(),
        ),
    ],
)
def test_metadata_is_stripped_and_the_sha_names_the_clean_image(ledger, data, clean):
    b.set_logo(ledger, "NORD", data)
    sha = b.current_sha(ledger, "NORD")
    assert b.original(ledger, "NORD", sha) == clean and sha == hashlib.sha256(clean).hexdigest()


@pytest.mark.parametrize("filt,color", [(f, 6) for f in range(5)] + [(4, c) for c in (0, 2, 4)])
def test_png_unfilter_exact_samples(ledger, filt, color):
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    stride = 32 * channels
    pixels = bytes((n * 17 + n // stride) % 256 for n in range(32 * stride))
    encoded = bytearray()
    for y in range(32):
        encoded.append(filt)
        for x in range(stride):
            a = pixels[y * stride + x - channels] if x >= channels else 0
            up = pixels[(y - 1) * stride + x] if y else 0
            c = pixels[(y - 1) * stride + x - channels] if y and x >= channels else 0
            p = a + up - c
            pa, pb, pc = abs(p - a), abs(p - up), abs(p - c)
            predictor = [0, a, up, (a + up) // 2, a if pa <= pb and pa <= pc else up if pb <= pc else c][filt]
            encoded.append((pixels[y * stride + x] - predictor) & 255)
    b.set_logo(ledger, "NORD", with_idat(bytes(encoded), color))
    logo = b.current_logo(ledger, "NORD")
    if color in (4, 6):
        assert decoded(logo, alpha=True) == pixels[channels - 1 :: channels]
        expected = bytes(v for n, v in enumerate(pixels) if n % channels != channels - 1)
    else:
        expected = pixels
    assert decoded(logo) == expected


def test_company_versions_and_snapshots(setup_invoice, home):
    L, _ = setup_invoice
    b.set_logo(L, "NORD", png())
    old = b.current_sha(L, "NORD")
    issued = i.issue(L, draft(setup_invoice), "2026-10-31", company="NORD", version=1)
    assert issued["data"]["logo_sha"] == old
    b.set_logo(L, "NORD", png(width=33))
    new = b.current_sha(L, "NORD")
    assert new != old
    assert [c["logo"] for c in api.state(L, "NORD")["companies"]] == [new, None]  # the switch draws the current logo
    assert b.original(L, "NORD", old) == png()
    assert i.invoice(L, issued["id"], company="NORD")["data"]["logo_sha"] == old
    reversed_inv = i.storno(L, issued["id"], "2026-11-01", "Corecție", company="NORD")
    assert reversed_inv["data"]["logo_sha"] == new
    with pytest.raises(i.InvoiceError):
        b.logo_for(L, "STUDIO9", old)
    b.remove_logo(L, "NORD")
    assert b.current_logo(L, "NORD") is None
    assert api.state(L, "NORD")["companies"][0]["logo"] is None
    assert b.original(L, "NORD", old) == png()
    assert b.original(L, "NORD", new) == png(width=33)
    assert invoicing_api.workspace(L, "NORD")["seller"]["logo"] is None


@pytest.mark.parametrize("component", ["company", "branding", "file"])
def test_symlink_refused(ledger, home, tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    co = home / "NORD"
    if component == "company":
        co.symlink_to(outside, target_is_directory=True)
    else:
        co.mkdir()
        if component == "branding":
            (co / "branding").symlink_to(outside, target_is_directory=True)
        else:
            b.set_logo(ledger, "NORD", png())
            image = next((co / "branding").glob("logo-*.png"))
            image.unlink()
            (outside / "image").write_bytes(png())
            image.symlink_to(outside / "image")
    with pytest.raises(i.InvoiceError) as error:
        if component == "file":
            b.current_logo(ledger, "NORD")
        else:
            b.set_logo(ledger, "NORD", png())
    # Approved copy (2026-11-02).
    assert (
        str(error.value)
        == "Sigla nu poate fi citită în siguranță. Administratorul trebuie să verifice dosarul firmei pe server."
    )
    assert list(outside.iterdir()) == ([] if component != "file" else [outside / "image"])
    with pytest.raises(i.InvoiceError) as error:
        b.set_logo(ledger, "../outside", png())
    assert str(error.value) == "Firma nu are siglă."


def test_http_shadow_and_headers(server, setup_invoice, monkeypatch):
    url = "/web/api/invoicing/logo?co=NORD"
    code, _, raw = _req(server, "POST", url, png(), {"Content-Type": "image/jpeg"})
    result = json.loads(raw)
    assert code == 200 and result["ok"] and result["id"] is None and result["state"]
    logo = result["invoicing"]["seller"]["logo"]
    assert logo == dict(sha=hashlib.sha256(png()).hexdigest(), type="image/png", width=32, height=32)
    loads, load = [], b.logo_for
    monkeypatch.setattr(b, "logo_for", lambda *args: loads.append(1) or load(*args))
    url = "/web/invoicing/logo?co=NORD"
    code, headers, raw = _req(server, "GET", url)
    assert code == 200 and raw == png() and loads == [1]
    assert headers["content-type"] == "image/png"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert headers["cache-control"] == "no-store"
    code, headers, _ = _req(server, "GET", url + "&sha=" + logo["sha"])
    assert code == 200 and headers["cache-control"] == "private, max-age=60"
    assert _req(server, "GET", url + "&sha=../bad")[0] == 404
    assert _req(server, "GET", url + "&sha=")[0] == 404
    assert _req(server, "GET", url.replace("NORD", "STUDIO9") + "&sha=" + logo["sha"])[0] == 404
    assert _api(server, "invoicing/logo-remove", {"co": "NORD"}, Origin="https://evil.invalid")[0] == 403
    assert _api(server, "invoicing/logo-remove", {"co": "NORD"})[1]["ok"]
    assert _req(server, "GET", url)[0] == 404
    assert _req(server, "GET", url + "&sha=" + logo["sha"])[0] == 200
    assert _req(server, "GET", url.replace("/tok/", "/bad/"))[0] == 404


@pytest.mark.parametrize(
    "length,ctype,origin,status,message",
    [
        (str(b.MAX_BYTES + 1), "image/png", None, 413, "Imaginea poate avea cel mult 512 KB."),  # approved copy
        (None, "image/png", None, 400, "Cererea nu a putut fi procesată."),
        ("-1", "image/png", None, 400, "Cererea nu a putut fi procesată."),
        ("100", "text/html", None, 415, b.INVALID),
        ("100", "image/png", "https://evil.invalid", 403, "Cererea nu a putut fi procesată."),
    ],
)
def test_http_refuses_before_reading_body(server, length, ctype, origin, status, message):
    conn = http.client.HTTPConnection("127.0.0.1", int(server), timeout=2)
    conn.putrequest("POST", "/web/api/invoicing/logo?co=NORD", skip_host=True)
    conn.putheader("Host", "books.example")
    conn.putheader("X-Forwarded-Proto", "https")
    conn.putheader("Cookie", "__Host-session=" + "a" * 64)
    if length is not None:
        conn.putheader("Content-Length", length)
    conn.putheader("Content-Type", ctype)
    conn.putheader("Origin", origin or server.origin)
    conn.endheaders()  # Intentionally send no body; a read would hang.
    response = conn.getresponse()
    assert response.status == status
    assert json.loads(response.read())["message"] == message
    conn.close()


def test_upload_and_refusal_copy(server):
    """No seller saved yet: the upload still answers with the workspace."""

    def upload(data, ctype="image/png"):
        code, _, raw = _req(server, "POST", "/web/api/invoicing/logo?co=NORD", data, {"Content-Type": ctype})
        assert code == 200
        return json.loads(raw)

    assert upload(png())["message"] == "Sigla a fost încărcată."
    rotated = upload(in_jpeg(exif(6)), "image/jpeg")
    assert rotated["message"] == "Imaginea este rotită prin metadate. Trebuie salvată fără rotire și încărcată din nou."
    refused = upload(png(interlace=1))
    assert not refused["ok"] and refused["message"] == b.INVALID


def test_cli(ledger, home, capsys):
    image = home / "input.bin"
    image.write_bytes(png())
    assert cli.main(["invoicing", "logo", "set", "--company", "NORD", str(image)]) == 0
    assert b.original(ledger, "NORD", b.current_sha(ledger, "NORD")) == png()
    assert cli.main(["invoicing", "logo", "remove", "--company", "NORD"]) == 0
    assert b.current_logo(ledger, "NORD") is None
    image.write_bytes(b"<svg/>")
    assert cli.main(["invoicing", "logo", "set", "--company", "NORD", str(image)]) == 2
    assert b.INVALID in capsys.readouterr().out


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink"])
def test_unavailable_does_not_break_workspace_or_remove(setup_invoice, home, caplog, damage):
    L, _ = setup_invoice
    b.set_logo(L, "NORD", png())
    path = logo_file(home, b.current_sha(L, "NORD"))
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"corrupt")
    else:
        path.unlink()
        path.symlink_to(home / "outside")
    b._logged.clear()
    for _ in range(2):
        seller = invoicing_api.workspace(L, "NORD")["seller"]
        assert seller["logo"] is None and seller["logo_unavailable"]
    assert len([r for r in caplog.records if "Invoice logo unavailable" in r.message]) == 1
    iid = draft(setup_invoice)
    with pytest.raises(i.InvoiceError, match=b.UNAVAILABLE):
        i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    assert i.invoice(L, iid, company="NORD")["status"] == "draft"
    b.remove_logo(L, "NORD")
    assert not invoicing_api.workspace(L, "NORD")["seller"]["logo_unavailable"]
    assert i.issue(L, iid, "2026-10-31", company="NORD", version=1)["data"]["logo_sha"] is None


@pytest.mark.parametrize("storno", [False, True])
@pytest.mark.parametrize("damage", ["missing", "pointer changed"])
def test_issue_and_storno_refuse_an_unreadable_or_changed_logo(setup_invoice, home, monkeypatch, storno, damage):
    L, _ = setup_invoice
    iid, act = issue_or_storno(setup_invoice, storno)
    b.set_logo(L, "NORD", png())
    if damage == "missing":
        logo_file(home, b.current_sha(L, "NORD")).unlink()
    else:
        issue_sha = b.issue_sha

        def changed(*args, **kwargs):
            sha = issue_sha(*args, **kwargs)
            b.remove_logo(L, "NORD")
            return sha

        monkeypatch.setattr(b, "issue_sha", changed)
    with pytest.raises(i.InvoiceError) as error:
        act()
    # Approved copy (2026-11-02).
    assert (
        str(error.value)
        == ("Factura de stornare" if storno else "Factura")
        + " nu a fost emisă. Sigla nu poate fi citită: trebuie încărcată din nou sau eliminată."
    )
    assert i.invoice(L, iid, company="NORD")["status"] == ("issued" if storno else "draft")


@pytest.mark.parametrize("storno", [False, True])
def test_prepare_outside_transaction(setup_invoice, home, monkeypatch, storno):
    L, _ = setup_invoice
    _, act = issue_or_storno(setup_invoice, storno)
    b.set_logo(L, "NORD", png())
    ready_path(home, b.current_sha(L, "NORD")).unlink()
    prepare, calls = b._prepare, []
    monkeypatch.setattr(b, "_prepare", lambda *args: calls.append(L.db.in_transaction) or prepare(*args))
    act()
    assert calls == [False]


def test_concurrent_uploads_and_stale_cleanup(ledger, home, monkeypatch):
    b.set_logo(ledger, "NORD", png())
    directory = home / "NORD" / "branding"
    stale, recent, gone = directory / ".logo-stale", directory / ".logo-active", directory / ".logo-gone"
    for path in (stale, recent, gone):
        path.write_bytes(b"temp")
    os.utime(stale, (1, 1))
    # Another uploader removes `.logo-gone` between the listing and the stat; the cleanup must skip it.
    stat = os.stat

    def vanishing(path, *args, **kwargs):
        if path == gone.name:
            raise FileNotFoundError(path)
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(b.os, "stat", vanishing)

    def upload(width):
        L = Ledger()
        try:
            b.set_logo(L, "NORD", png(width=width))
        finally:
            L.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(upload, [33, 34, 33, 35]))
    assert not stale.exists() and recent.exists()
    assert b.current_logo(ledger, "NORD").width in (33, 34, 35)
    for width in (33, 34, 35):
        sha = hashlib.sha256(png(width=width)).hexdigest()
        assert b.logo_for(ledger, "NORD", sha).width == width


def test_prepared_once_and_hash_checked_on_every_read(ledger, home, monkeypatch, prepares):
    data = (
        png(1200, 1200, color=6)[:33]
        + chunk(b"IDAT", zlib.compress((b"\x01" + bytes(4800)) * 1200))
        + chunk(b"IEND", b"")
    )
    start = time.perf_counter()
    b.set_logo(ledger, "NORD", data)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.8, f"Preparation took {elapsed:.3f}s"
    sha = b.current_sha(ledger, "NORD")
    logo = b.logo_for(ledger, "NORD", sha)
    monkeypatch.setattr(b, "validate", lambda *_: pytest.fail("Repeated full validation"))
    assert b.logo_for(ledger, "NORD", sha) == logo and len(prepares) == 1
    assert len(decoded(logo)) == 1200 * 1200 * 3 and len(decoded(logo, alpha=True)) == 1200 * 1200
    # Same size and modification time, other bytes: the cached validation must not vouch for them.
    path = logo_file(home, sha)
    info = path.stat()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(i.InvoiceError):
        b.current_logo(ledger, "NORD")


def test_caches_stay_bounded():
    cache = OrderedDict()
    for n in range(65):
        assert b._cached(cache, n, lambda n=n: n) == n
    assert list(cache) == list(range(1, 65))


def ready_bytes(payload):
    encoded = json.dumps(payload).encode()
    return hashlib.sha256(encoded).hexdigest().encode() + b"\n" + encoded


@pytest.mark.parametrize(
    "damage",
    ["missing", "corrupt", "checksum", "not a dict", "v", "sha", "kind", "width", "height", "components", "bits"],
)
def test_a_bad_ready_file_is_rebuilt(ledger, home, prepares, damage):
    """Gray with alpha: the ready file must say one colour component, not the PNG's two channels."""
    b.set_logo(ledger, "NORD", with_idat((b"\0" + bytes([53]) * 64) * 32, color=4))
    logo = b.current_logo(ledger, "NORD")
    path = ready_path(home, logo.sha)
    good = path.read_bytes()
    payload = json.loads(good.split(b"\n", 1)[1])
    prepares.clear()
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"corrupt")
    elif damage == "checksum":
        path.write_bytes(b"0" * 64 + good[64:])
    elif damage == "not a dict":
        path.write_bytes(ready_bytes([]))
    else:
        path.write_bytes(ready_bytes(payload | {damage: "wrong" if damage in ("sha", "kind") else 0}))
    assert b.logo_for(ledger, "NORD", logo.sha) == logo
    assert prepares == [1] and path.read_bytes() == good
    assert decoded(logo) == decoded(logo, alpha=True) == bytes([53]) * 1024


def test_readonly_ready_is_optional(setup_invoice, home, caplog):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    b.set_logo(L, "NORD", png(color=4))
    sha = b.current_sha(L, "NORD")
    path = ready_path(home, sha)
    path.unlink()
    b._logged.clear()
    path.parent.chmod(0o500)
    try:
        for _ in range(2):
            assert b.current_logo(L, "NORD").components == 1
        assert i.issue(L, iid, "2026-10-31", company="NORD", version=1)["data"]["logo_sha"] == sha
        assert not path.exists()
        assert len([r for r in caplog.records if "cache not saved" in r.message]) == 1
    finally:
        path.parent.chmod(0o700)


def test_original_still_validates_without_preparing(ledger, home, monkeypatch):
    b.set_logo(ledger, "NORD", png())
    monkeypatch.setattr(b, "_prepare", lambda *_: pytest.fail("Original must not prepare PDF data"))
    ready_path(home, b.current_sha(ledger, "NORD")).unlink()
    assert b.original(ledger, "NORD", b.current_sha(ledger, "NORD")) == png()
    invalid = b"not an image"
    sha = hashlib.sha256(invalid).hexdigest()
    logo_file(home, sha).write_bytes(invalid)
    with pytest.raises(i.InvoiceError):
        b.original(ledger, "NORD", sha)


def test_logo_before_seller_configuration(server, ledger):
    url = "/web/api/invoicing/logo?co=NORD"
    code, _, raw = _req(server, "POST", url, png(), {"Content-Type": "image/png"})
    result = json.loads(raw)
    assert code == 200 and result["ok"] and result["invoicing"]["seller"] is None
    assert _req(server, "GET", "/web/invoicing/logo?co=NORD")[2] == png()
    assert _req(server, "GET", "/web/invoicing/logo?co=STUDIO9")[0] == 404
    refused = invoicing_api.act(ledger, "logo", "STUDIO9", {"data": png()}, require=lambda _: False)
    assert not refused["ok"] and b.current_logo(ledger, "STUDIO9") is None
