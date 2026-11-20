import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from test_api import _req, quiet, server as server, world as world  # noqa: F401
from test_efactura import anaf_zip, ubl

from fiscalm import ai, companies, docs, integrations, links, pack, receipt_items, recon, transactions
from fiscalm.api import actions, receipts as intake, state as api, uploads as upload_links
from fiscalm.bank import statements
from fiscalm.efactura import inbox as ef
from fiscalm.ledger import Ledger, LinkRefused
from fiscalm.receipt_lines import signatures

JPEG = b"\xff\xd8\xffexample"
PNG = b"\x89PNG\r\n\x1a\nexample"


def multipart(files):
    body = b""
    for name, data in files:
        body += (
            b'--test-boundary\r\nContent-Disposition: form-data; name="files"; filename="'
            + name.encode()
            + b'"\r\nContent-Type: text/plain\r\n\r\n'
            + data
            + b"\r\n"
        )
    return body + b"--test-boundary--\r\n"


def post(server, path, files, **headers):
    return _req(
        server,
        "POST",
        path if "?" in path else path + "/api/files",
        multipart(files),
        {
            "Content-Type": "multipart/form-data; boundary=test-boundary",
            "Origin": getattr(server, "origin", "https://books.example"),
            **headers,
        },
    )


def test_http_intake_and_isolation(ledger, server, home, world):
    (home / "state" / "LIVE").unlink()
    made = upload_links.create(ledger, "NORD", "Phone")
    path = made["path"]
    assert _req(server, "GET", path)[0] == 200
    code, _, raw = _req(server, "GET", path + "/api/info")
    assert code == 200 and json.loads(raw) == {"company": "NORD", "label": "Phone"}
    code, _, raw = post(server, path, [("../../escape.pdf", JPEG), ("photo.jpg", PNG)])
    assert code == 200 and json.loads(raw)["ok"]
    sha = hashlib.sha256(JPEG).hexdigest()
    row = ledger.document(sha)
    assert row["kind"] == "receipt" and json.loads(row["issues"]) == ["to-check"]
    assert str(home / "NORD" / "documents" / "inbox") in row["path"]
    assert row["path"].endswith(sha + ".jpg")
    assert api.read_doc(ledger, "NORD", sha)[1] == JPEG
    assert api.read_doc(ledger, "STUDIO9", sha) is None
    state = api.state(ledger, "NORD")
    assert len(state["inbox"]) == 2 and state["inbox"][0]["link_label"] == "Phone"
    assert {"kind": "to_check", "id": sha} in state["upload_tasks"]
    assert not api.state(ledger, "STUDIO9")["inbox"]
    assert all(d["sha"] != sha for d in pack.pending_docs(ledger, "NORD", "9999-12"))
    assert post(server, path, [("duplicate.jpg", JPEG)])[0] == 200
    assert upload_links.list_links(ledger, "NORD")[0]["uploads"] == 3
    for rest in ("/api/state", "/api/upload-links", "/docfile?co=NORD&sha=" + sha, "/api/files/" + sha):
        assert _req(server, "GET", path + rest)[0] == 404
    upload_links.revoke(ledger, made["id"], "NORD")
    assert post(server, path, [("x.jpg", JPEG)])[0] == 404


def test_http_refusals(ledger, server, monkeypatch):
    path = upload_links.create(ledger, "NORD", "Phone")["path"]
    for bad in ("/u/bad", path[:-1] + "x"):
        assert _req(server, "GET", bad)[0] == 404
        assert post(server, bad, [("x", JPEG)])[0] == 404
    for origin in ("", "null", "https://evil.example"):
        assert post(server, path, [("x", JPEG)], Origin=origin)[0] == 403
    assert post(server, path, [("x", JPEG)], **{"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert post(server, path, [("valid.jpg", b"not an image")])[0] == 415
    assert post(server, path, [("x", JPEG)] * 11)[0] == 413
    assert (
        _req(
            server,
            "POST",
            path + "/api/files",
            b"",
            {
                "Host": "books.example",
                "X-Forwarded-Proto": "https",
                "Origin": getattr(server, "origin", "https://books.example"),
                "Content-Length": str(intake.REQUEST_MAX + 1),
            },
        )[0]
        == 413
    )
    monkeypatch.setattr(intake, "FILE_MAX", 4)
    assert post(server, path, [("x", JPEG)])[0] == 413
    assert not upload_links.list_links(ledger, "NORD")[0]["uploads"]


def test_quota_and_race(ledger):
    made = upload_links.create(ledger, "NORD", "Limited", max_files=1)
    token = made["path"][3:]

    def attempt(data):
        L = Ledger()
        try:
            try:
                intake.accept(L, token, [("jpg", data)])
                return 200
            except intake.Refused as e:
                return e.status
        finally:
            L.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (JPEG, PNG)))
    assert sorted(results) == [200, 404]
    assert not upload_links.verify(ledger, token)
    assert upload_links.list_links(ledger, "NORD")[0]["uploads"] == 1


def test_batch_atomic_quota_rate_and_symlink(ledger, home):
    made = upload_links.create(ledger, "NORD", "Phone", max_files=1)
    with pytest.raises(intake.Refused) as e:
        intake.accept(ledger, made["path"][3:], [("jpg", JPEG), ("png", PNG)])
    assert e.value.status == 409 and not ledger.document(hashlib.sha256(JPEG).hexdigest())
    (home / "NORD").symlink_to(home / "STUDIO9", target_is_directory=True)
    (home / "STUDIO9").mkdir()
    with pytest.raises(intake.Refused):
        intake.accept(ledger, made["path"][3:], [("jpg", JPEG)])
    assert not list((home / "STUDIO9").iterdir())
    (home / "NORD").unlink()
    token = upload_links.create(ledger, "NORD", "Rate")["path"][3:]
    for n in range(60):
        intake.accept(ledger, token, [("jpg", JPEG + str(n).encode())])
    with pytest.raises(intake.Refused) as e:
        intake.accept(ledger, token, [("png", PNG)])
    assert e.value.status == 429


@pytest.mark.parametrize(
    "data,ext",
    [
        (JPEG, "jpg"),
        (PNG, "png"),
        (b"%PDF-1.7\nPK\x03\x04" + b"zip-bomb-ish bytes", "pdf"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic", "heic"),
    ],
)
def test_sniff_only(ledger, data, ext):
    assert intake.sniff(data) == ext
    token = upload_links.create(ledger, "NORD", "Bytes")["path"][3:]
    body = multipart([("lie.exe", data)])
    with intake.multipart("multipart/form-data; boundary=test-boundary", io.BytesIO(body), len(body), "NORD") as files:
        assert files[0][0] == ext and files[0][1].read() == data
        intake.accept(ledger, token, files)


def test_malformed_multipart(ledger):
    for body in (b"", b"--test-boundary\r\n", multipart([("x", JPEG)])[:-20]):
        with (
            pytest.raises(intake.Refused),
            intake.multipart("multipart/form-data; boundary=test-boundary", io.BytesIO(body), len(body), "NORD"),
        ):
            pytest.fail("accepted malformed multipart")


def test_pending_not_archived_or_packaged(ledger, monkeypatch):
    from fiscalm import archive

    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    intake.accept(ledger, token, [("jpg", JPEG)])

    def forbidden(*args):
        pytest.fail("unreviewed receipt reached archive")

    monkeypatch.setattr(archive, "target", forbidden)
    assert archive.archive_all(ledger) == []
    assert pack.pending_docs(ledger, "NORD", "9999-12") == []


def test_final_and_nested_symlinks_refused(ledger, home):
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    day = upload_links.timestamp()[:10]
    root = home / "NORD" / "documents" / "inbox"
    root.mkdir(parents=True)
    outside = home / "outside"
    outside.mkdir()
    (root / day).symlink_to(outside, target_is_directory=True)
    with pytest.raises(intake.Refused):
        intake.accept(ledger, token, [("jpg", JPEG)])
    (root / day).unlink()
    (root / day).mkdir()
    sha = hashlib.sha256(JPEG).hexdigest()
    (root / day / (sha + ".jpg")).symlink_to(outside / "target")
    with pytest.raises(intake.Refused):
        intake.accept(ledger, token, [("jpg", JPEG)])
    assert not list(outside.iterdir())


def test_global_duplicate_does_not_disclose_or_reassign(ledger, home):
    sha = hashlib.sha256(JPEG).hexdigest()
    ledger.add_document(sha, "STUDIO9", "2026-10", "receipt", str(home / "other.jpg"))
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    assert intake.accept(ledger, token, [("jpg", JPEG)]) == [{"sha": sha}]
    assert ledger.document(sha)["company"] == "STUDIO9"
    assert not intake.inbox(ledger, "NORD")
    assert upload_links.list_links(ledger, "NORD")[0]["uploads"] == 1


def test_duplicate_race(ledger):
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]

    def attempt(_):
        L = Ledger()
        try:
            return intake.accept(L, token, [("jpg", JPEG)])
        finally:
            L.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(list(pool.map(attempt, range(2)))) == 2
    assert upload_links.list_links(ledger, "NORD")[0]["uploads"] == 2
    assert len(intake.inbox(ledger, "NORD")) == 1


def test_expired_and_exhausted_http_use_auth_delay(ledger, server, monkeypatch):
    from fiscalm.api import web

    delays = []
    monkeypatch.setattr(web.time, "sleep", delays.append)
    path = upload_links.create(ledger, "NORD", "Expired")["path"]
    with ledger.tx() as db:
        db.execute("UPDATE upload_links SET expires_at='2000-01-01'")
    assert _req(server, "GET", path + "/api/info")[0] == 404
    path = upload_links.create(ledger, "NORD", "Exhausted", max_files=1)["path"]
    assert post(server, path, [("x", JPEG)])[0] == 200
    assert _req(server, "GET", path)[0] == 404
    assert post(server, path, [("x", PNG)])[0] == 404
    assert delays == [0.5, 0.5, 0.5]


def test_app_management_in_shadow(ledger, server):
    body = json.dumps({"co": "NORD", "label": "Phone", "expires_in_days": 7, "max_files": 3}).encode()
    code, _, raw = _req(server, "POST", "/web/api/upload-link-create", body, {"Content-Type": "application/json"})
    made = json.loads(raw)
    assert code == 200 and made["ok"]
    assert upload_links.verify(ledger, made["done"]["path"][3:])
    body = json.dumps({"co": "NORD", "id": made["done"]["id"]}).encode()
    code, _, raw = _req(server, "POST", "/web/api/upload-link-revoke", body, {"Content-Type": "application/json"})
    assert code == 200 and json.loads(raw)["done"]["revoked"]


def member_post(server, query, files, user="member", **headers):
    cookie = "__Host-session=" + {"member": "b", "readonly": "c"}.get(user, "d") * 64
    code, _, raw = post(server, "/web/api/document-upload?" + query, files, Cookie=cookie, **headers)
    return code, json.loads(raw) if raw.startswith(b"{") else None


def test_member_upload_is_stored_like_a_link_upload_in_shadow(ledger, server, home):
    assert not (home / "state" / "LIVE").exists()  # Sandbox
    code, body = member_post(server, "co=NORD", [("../../escape.pdf", JPEG), ("photo.jpg", PNG)])
    jpeg, png = hashlib.sha256(JPEG).hexdigest(), hashlib.sha256(PNG).hexdigest()
    assert code == 200 and body == {
        "ok": True,
        "files": [{"sha": jpeg, "duplicate": False}, {"sha": png, "duplicate": False}],
    }
    row = ledger.document(jpeg)
    assert (row["kind"], row["company"], row["source"]) == ("receipt", "NORD", "upload:user:u_member")
    assert row["path"] == str(
        home.resolve() / "NORD" / "documents" / "inbox" / row["created_at"][:10] / (jpeg + ".jpg")
    )
    assert api.read_doc(ledger, "NORD", jpeg)[1] == JPEG and api.read_doc(ledger, "STUDIO9", jpeg) is None
    state = api.state(ledger, "NORD")
    assert {u["sha"] for u in state["inbox"]} == {jpeg, png} and {"kind": "to_check", "id": jpeg} in state[
        "upload_tasks"
    ]
    assert all(d["sha"] != jpeg for d in pack.pending_docs(ledger, "NORD", "9999-12"))
    assert member_post(server, "co=NORD", [("again.jpg", JPEG)])[1]["files"] == [{"sha": jpeg, "duplicate": True}]
    attempts = ledger.db.execute("SELECT link_id,company,files FROM upload_attempts WHERE files>0").fetchall()
    assert [tuple(a) for a in attempts] == [("user:u_member", "NORD", 2), ("user:u_member", "NORD", 1)]
    assert not ledger.db.execute("SELECT 1 FROM upload_results").fetchone()


@pytest.mark.parametrize("limit", ["LINK_HOUR_BYTES", "HOUR_BYTES"])
def test_member_upload_shares_the_upload_budgets(ledger, server, monkeypatch, limit):
    assert member_post(server, "co=NORD", [("a.jpg", JPEG)])[0] == 200
    charged = ledger.db.execute("SELECT SUM(bytes) FROM upload_attempts WHERE link_id='user:u_member'").fetchone()[0]
    monkeypatch.setattr(intake, limit, charged + 50)  # the first request's bytes stay charged
    assert member_post(server, "co=NORD", [("b.png", PNG)])[0] == 429
    assert not ledger.document(hashlib.sha256(PNG).hexdigest())


@pytest.mark.parametrize(
    "query, files, user, headers, status",
    [
        ("co=NORD", [("x.jpg", JPEG)], "readonly", {}, 403),
        ("co=NORD", [("x.jpg", JPEG)], "nobody", {}, 401),
        ("co=NORD", [("x.jpg", JPEG)], "member", {"Origin": "https://evil.example"}, 403),
        ("co=NORD", [("x.jpg", JPEG)], "member", {"Sec-Fetch-Site": "cross-site"}, 403),
        ("co=NORD", [("x.jpg", JPEG)], "member", {"Content-Type": "application/json"}, 415),
        ("co=ELSEWHERE", [("x.jpg", JPEG)], "member", {}, 400),
        ("co=NORD&path=../STUDIO9", [("x.jpg", JPEG)], "member", {}, 400),
        (
            "co=NORD",
            [("x.xml", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')],
            "member",
            {},
            415,
        ),
        ("co=NORD", [("x.jpg", JPEG)] * 11, "member", {}, 413),
    ],
)
def test_member_upload_refusals_store_nothing(ledger, server, home, query, files, user, headers, status):
    assert member_post(server, query, files, user, **headers)[0] == status
    assert not ledger.db.execute("SELECT 1 FROM documents").fetchone()
    assert not list(home.glob("*/documents/inbox/*/*"))


def test_member_upload_refuses_a_symlinked_inbox(ledger, server, home):
    root = home / "NORD" / "documents" / "inbox"
    root.mkdir(parents=True)
    outside = home / "outside"
    outside.mkdir()
    (root / upload_links.timestamp()[:10]).symlink_to(outside, target_is_directory=True)
    assert member_post(server, "co=NORD", [("x.jpg", JPEG)])[0] == 409
    assert not list(outside.iterdir()) and not ledger.db.execute("SELECT 1 FROM documents").fetchone()


def test_framing_and_real_file_limit(ledger, server):
    path = upload_links.create(ledger, "NORD", "Phone")["path"]
    assert post(server, path, [("x", JPEG + b"x" * intake.FILE_MAX)])[0] == 413
    assert post(server, path, [], **{"Transfer-Encoding": "chunked"})[0] == 400
    assert post(server, path, [], **{"Content-Length": "-1"})[0] == 400
    assert post(server, path, [], **{"Content-Type": "text/plain"})[0] == 415
    assert post(server, path, [("x", b"PK\x03\x04%PDF-1.7")])[0] == 415
    assert not upload_links.list_links(ledger, "NORD")[0]["uploads"]


@pytest.mark.parametrize(
    "payload",
    [
        b"x\n" * (15 * 1024 * 1024),
        multipart([("x", JPEG)])[: -len(b"--test-boundary--\r\n")] * 100000 + b"--test-boundary--\r\n",
    ],
)
def test_amplification_bounded(ledger, payload):
    import time
    import tracemalloc

    body = payload if payload.startswith(b"--") else multipart([("x", payload)])
    stream = io.BytesIO(body)
    tracemalloc.start()
    start = time.monotonic()
    try:
        with (
            pytest.raises(intake.Refused) as error,
            intake.multipart("multipart/form-data; boundary=test-boundary", stream, len(body), "NORD"),
        ):
            pytest.fail("accepted amplification input")
        assert error.value.status in (413, 415)
        assert time.monotonic() - start < 3
        assert tracemalloc.get_traced_memory()[1] < 2 * 1024 * 1024
        assert stream.tell() < 1024 * 1024
    finally:
        tracemalloc.stop()


def test_slow_trickle_total_deadline(ledger, monkeypatch):
    import time

    monkeypatch.setattr(intake, "DEADLINE", 0.06)

    class Trickle:
        def read1(self, n):
            time.sleep(0.015)
            return b"-"

    start = time.monotonic()
    with (
        pytest.raises(intake.Refused) as error,
        intake.multipart("multipart/form-data; boundary=test-boundary", Trickle(), 1000, "NORD"),
    ):
        pytest.fail("accepted trickle")
    assert error.value.status == 408
    assert time.monotonic() - start < 0.3


def test_busy_and_refused_requests_count(ledger, server, monkeypatch):
    import threading

    monkeypatch.setattr(intake, "ADMISSION", threading.BoundedSemaphore(1))
    path = upload_links.create(ledger, "NORD", "Phone")["path"]
    with intake.ADMISSION:
        code, headers, _ = post(server, path, [("x", JPEG)])
        assert code == 503 and headers["retry-after"] == "5"
    for _ in range(59):
        assert post(server, path, [("x", b"bad")])[0] == 415
    assert post(server, path, [("x", JPEG)])[0] == 429


def test_proxy_origin_host(ledger, server):
    path = upload_links.create(ledger, "NORD", "Phone")["path"]
    assert (
        post(
            server,
            path,
            [("x", JPEG)],
            Host="receipts.example",
            Origin="https://receipts.example",
            **{"X-Forwarded-Host": "evil.example"},
        )[0]
        == 404
    )
    assert post(server, path, [("x", JPEG)], Origin="https://receipts.example")[0] == 403
    assert post(server, path, [("x", JPEG)], **{"X-Forwarded-Host": "books.example"})[0] == 200


def test_revoke_during_scanning_and_no_orphans(ledger, server, home, monkeypatch):
    original = intake.accept
    path = upload_links.create(ledger, "NORD", "Phone")["path"]

    def revoked(L, token, files, **kw):
        other = Ledger()
        try:
            upload_links.revoke(other, token.split(".")[0], "NORD")
        finally:
            other.close()
        return original(L, token, files, **kw)

    monkeypatch.setattr(intake, "accept", revoked)
    assert post(server, path, [("x", JPEG)])[0] == 404
    assert not list((home / "NORD").rglob("*.jpg"))
    assert not list((home / "NORD").rglob(".upload-*"))


def test_disk_writes_outside_transaction_and_failed_batch_cleanup(ledger, home, monkeypatch):
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    original = intake.shutil.copyfileobj
    calls = []

    def copy(*args):
        assert not ledger.db.in_transaction
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return original(*args)

    monkeypatch.setattr(intake.shutil, "copyfileobj", copy)
    with pytest.raises(intake.Refused):
        intake.accept(ledger, token, [("jpg", JPEG), ("png", PNG)])
    assert not ledger.documents()
    assert not [p for p in (home / "NORD").rglob("*") if p.is_file()]


@pytest.mark.parametrize("limit", ["HOUR_BYTES", "DAY_BYTES"])
def test_company_bytes_and_free_space(ledger, home, monkeypatch, limit):
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    other = upload_links.create(ledger, "NORD", "Other")["path"][3:]
    monkeypatch.setattr(intake, limit, len(JPEG))
    intake.accept(ledger, token, [("jpg", JPEG)])
    with pytest.raises(intake.Refused) as error:
        intake.accept(ledger, other, [("jpg", JPEG)])
    assert error.value.status == 429
    monkeypatch.setattr(intake, "MIN_FREE", 2**63)
    with pytest.raises(intake.Refused) as error, intake.directory("NORD", "2026-01-01"):
        pass
    assert error.value.status == 507


def test_duplicate_quota_and_record(ledger):
    token = upload_links.create(ledger, "NORD", "Phone", max_files=2)["path"][3:]
    intake.accept(ledger, token, [("jpg", JPEG)])
    with pytest.raises(intake.Refused):
        intake.accept(ledger, token, [("jpg", JPEG), ("jpg", JPEG)])
    intake.accept(ledger, token, [("jpg", JPEG)])
    assert [r[0] for r in ledger.db.execute("SELECT duplicate FROM upload_results")] == [0, 1]
    assert not upload_links.verify(ledger, token)


def test_heif_excludes_avif():
    for brand in (b"avif", b"mif1", b"msf1", b"hevc"):
        with pytest.raises(intake.Refused):
            intake.sniff(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 4 + b"mif1avif")


@pytest.mark.parametrize("co", ["NORD", "STUDIO9"])
@pytest.mark.parametrize("source", ["mail:synthetic", "manual"])
def test_trusted_ingest_takes_over(ledger, co, source):
    sha = hashlib.sha256(JPEG).hexdigest()
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    intake.accept(ledger, token, [("jpg", JPEG)])
    oldpath = ledger.document(sha)["path"]
    assert ledger.add_document(
        sha, co, "2026-01", "receipt", oldpath, source=source, vendor="Invented vendor", amount="12.34", issues=[]
    )
    row = ledger.document(sha)
    assert row["company"] == co and row["source"] == source and row["amount"] == "12.34"
    assert row["path"] == oldpath and __import__("pathlib").Path(oldpath).read_bytes() == JPEG
    assert ledger.get_meta("upload-takeover:" + sha)
    assert not intake.inbox(ledger, "NORD")


def test_unreviewed_signal_survives_issue_edit(ledger, monkeypatch):
    from fiscalm import archive

    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    intake.accept(ledger, token, [("jpg", JPEG)])
    with ledger.tx() as db:
        db.execute("UPDATE documents SET issues='[]'")
    assert not pack.pending_docs(ledger, "NORD", "9999-12")
    monkeypatch.setattr(archive, "target", lambda *args: pytest.fail("archived unreviewed"))
    assert archive.archive_all(ledger) == []


@pytest.mark.parametrize("co", ["NORD", "STUDIO9"])
@pytest.mark.parametrize("source", ["manual", "mail:synthetic"])
def test_docs_entrypoint_takes_over(ledger, home, monkeypatch, co, source):
    from fiscalm import docs

    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    intake.accept(ledger, token, [("jpg", JPEG)])
    src = home / "trusted.jpg"
    src.write_bytes(JPEG)
    monkeypatch.setattr(docs, "inspect", lambda path: docs.Evidence(str(path), mentions=[co]))
    result = docs.add_receipt(ledger, co, src, month="2026-01", source=source, vendor="Example")
    assert result["status"] == "added"
    assert ledger.document(hashlib.sha256(JPEG).hexdigest())["source"] == source


def test_trusted_cross_company_file_remains_readable(ledger):
    sha = hashlib.sha256(JPEG).hexdigest()
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    intake.accept(ledger, token, [("jpg", JPEG)])
    ledger.add_document(sha, "STUDIO9", "2026-01", "receipt", "unused", source="manual")
    assert api.read_doc(ledger, "STUDIO9", sha)[1] == JPEG
    assert api.read_doc(ledger, "NORD", sha) is None


def test_revoked_after_file_write_rolls_back(ledger, home, monkeypatch):
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    original = intake.shutil.copyfileobj

    def copy(*args):
        original(*args)
        other = Ledger()
        try:
            upload_links.revoke(other, token.split(".")[0], "NORD")
        finally:
            other.close()

    monkeypatch.setattr(intake.shutil, "copyfileobj", copy)
    with pytest.raises(intake.Refused) as error:
        intake.accept(ledger, token, [("jpg", JPEG)])
    assert error.value.status == 404
    assert not ledger.documents()
    assert not [p for p in (home / "NORD").rglob("*") if p.is_file()]


def test_body_deadline_includes_storage(ledger, home, monkeypatch):
    import time

    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    original = intake.shutil.copyfileobj

    def copy(*args):
        original(*args)
        time.sleep(0.03)

    monkeypatch.setattr(intake.shutil, "copyfileobj", copy)
    with pytest.raises(intake.Refused) as error:
        intake.accept(ledger, token, [("jpg", JPEG)], deadline=time.monotonic() + 0.02)
    assert error.value.status == 408
    assert not ledger.documents()
    assert not [p for p in (home / "NORD").rglob("*") if p.is_file()]


def test_duplicate_has_same_disk_work_and_preflight(ledger, monkeypatch):
    token = upload_links.create(ledger, "NORD", "Phone", max_files=3)["path"][3:]
    calls = []
    original = intake.shutil.copyfileobj

    def copy(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(intake.shutil, "copyfileobj", copy)
    for _ in range(2):
        intake.accept(ledger, token, [("jpg", JPEG)])
    assert len(calls) == 2
    with pytest.raises(intake.Refused):
        intake.accept(ledger, token, [("jpg", JPEG), ("jpg", JPEG)])
    assert len(calls) == 2


def test_short_bodies_do_not_exhaust_company(ledger, server):
    import http.client
    import socket

    path = upload_links.create(ledger, "NORD", "Leaked")["path"]
    other = upload_links.create(ledger, "NORD", "Other")["path"]
    for mib in (40, 40, 20):
        conn = http.client.HTTPConnection("127.0.0.1", int(server), timeout=3)
        conn.request(
            "POST",
            path + "/api/files",
            b"x",
            {
                "Host": "books.example",
                "X-Forwarded-Proto": "https",
                "Origin": getattr(server, "origin", "https://books.example"),
                "Content-Length": str(mib * 1024 * 1024),
                "Content-Type": "multipart/form-data; boundary=test-boundary",
            },
        )
        conn.sock.shutdown(socket.SHUT_WR)
        response = conn.getresponse()
        assert response.status == 400
        response.read()
        conn.close()
    assert ledger.db.execute("SELECT SUM(bytes) FROM upload_attempts").fetchone()[0] == 3
    assert post(server, other, [("x", JPEG)])[0] == 200


def test_link_byte_limit_precedes_company(ledger, monkeypatch):
    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    monkeypatch.setattr(intake, "LINK_HOUR_BYTES", 10)
    reservation = intake.reserve_bytes(ledger, token, 10)
    with pytest.raises(intake.Refused) as error:
        intake.reserve_bytes(ledger, token, 1)
    assert error.value.status == 429
    intake.settle_bytes(ledger, reservation, 1)
    assert intake.reserve_bytes(ledger, token, 9)


def test_admission_released_before_response(ledger, server, monkeypatch):
    import threading

    from fiscalm.api.web import Handler

    # A single slot makes the ordering assertion independent of configured capacity.
    monkeypatch.setattr(intake, "ADMISSION", threading.BoundedSemaphore(1))
    path = upload_links.create(ledger, "NORD", "Phone")["path"]
    original = Handler._json
    checked = []

    def respond(self, status, body):
        if self.path.startswith(path) and not checked:
            checked.append(True)
            assert intake.ADMISSION.acquire(blocking=False), "response started while slot still held"
            intake.ADMISSION.release()
            assert post(server, path, [("next", JPEG)])[0] == 200
        return original(self, status, body)

    monkeypatch.setattr(Handler, "_json", respond)
    assert post(server, path, [("bad", b"bad")])[0] == 415
    assert checked


def test_deadline_scales_and_is_capped():
    assert intake.body_deadline(0) == 30
    assert intake.body_deadline(256 * 1024) == 31
    assert intake.body_deadline(intake.REQUEST_MAX) == intake.MAX_DEADLINE
    assert intake.body_deadline(2**40) == intake.MAX_DEADLINE


def test_temp_sweep_stays_inside_inbox(ledger, home):
    import os

    with intake.directory("NORD", "2026-01-01"):
        pass
    root = home / "NORD" / "documents" / "inbox"
    day = root / "2026-01-01"
    outside = home / ".upload-outside"
    outside.write_bytes(b"keep")
    for name in (".upload-old", ".upload-new", "complete.jpg"):
        (day / name).write_bytes(b"keep")
    (day / ".upload-symlink").symlink_to(outside)
    (root / "escape").symlink_to(home, target_is_directory=True)
    for file in (day / ".upload-old", day / "complete.jpg", outside):
        os.utime(file, (1, 1))
    assert intake.sweep_temps() == 1
    assert not (day / ".upload-old").exists()
    assert (day / ".upload-new").exists() and (day / ".upload-symlink").is_symlink()
    assert (day / "complete.jpg").exists() and outside.exists()


def test_uploads_excluded_from_news_and_unsent(ledger, home, monkeypatch):
    from datetime import datetime, timedelta

    from fiscalm import recon

    token = upload_links.create(ledger, "NORD", "Phone")["path"][3:]
    intake.accept(ledger, token, [("jpg", JPEG)])
    assert not api._news(ledger, "NORD", datetime.now() - timedelta(days=1), [])
    from types import SimpleNamespace

    month = upload_links.timestamp()[:7]
    ledger.add_document("a" * 64, "NORD", month, "statement", str(home / "statement.pdf"))
    monkeypatch.setattr(recon.statements, "parse", lambda *args: SimpleNamespace(accounts=[], txs=[]))
    _, summary = recon.build_report(ledger, "NORD", month)
    assert summary["unsent_docs"] == len(pack.pending_docs(ledger, "NORD", month)) == 1


def test_shared_socket_timeout_only_limits_reads(ledger, server, monkeypatch):
    import socket
    import time

    from fiscalm.api.web import Handler

    monkeypatch.setattr(Handler, "timeout", 0.04)
    with socket.create_connection(("127.0.0.1", int(server)), timeout=2) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost:")
        assert sock.recv(1) == b""
    original = Handler._json

    def slow(self, *args):
        time.sleep(0.08)  # model a long ANAF/AI call after the body has been read
        return original(self, *args)

    monkeypatch.setattr(Handler, "_json", slow)
    path = upload_links.create(ledger, "NORD", "Phone")["path"]
    assert _req(server, "GET", path + "/api/info")[0] == 200


def test_pdf_preview_child_has_address_space_limit(monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    from fiscalm import pdftext

    run = subprocess.run
    monkeypatch.setattr(pdftext.shutil, "which", lambda tool: "/fake/pdftoppm")

    def preview(args, **kwargs):
        result = run(
            [sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"], **kwargs
        )
        assert int(result.stdout) == 512 * 1024 * 1024
        Path(args[-1] + ".png").write_bytes(PNG)
        return result

    monkeypatch.setattr(pdftext.subprocess, "run", preview)
    assert pdftext.first_page_png(b"%PDF-test") == PNG


def test_one_occupied_slot_allows_another_link(ledger, server):
    path = upload_links.create(ledger, "NORD", "Other")["path"]
    with intake.ADMISSION:
        assert post(server, path, [("x", JPEG)])[0] == 200


def test_selfhost_upload_listener_and_csp(ledger, tmp_path, monkeypatch):
    import http.client
    import threading

    from fiscalm.api import web

    (tmp_path / "index.html").write_text("<script>bootstrap()</script>")
    monkeypatch.setattr(web, "APP_OUT", tmp_path)
    monkeypatch.setenv("FISCALM_PUBLIC_URL", "https://books.example")
    monkeypatch.setenv("FISCALM_TRUST_PROXY", "1")
    monkeypatch.setenv("FISCALM_HOST", "127.0.0.1")
    monkeypatch.setenv("FISCALM_PORT", "0")
    swept = []
    monkeypatch.setattr(intake, "sweep_temps", lambda: swept.append(True))
    path = upload_links.create(ledger, "NORD", "Phone")["path"]

    original_server = web.Server

    def listener(address, handler):
        srv = original_server(address, handler)
        run = srv.serve_forever

        def exercise():
            thread = threading.Thread(target=run, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                port = srv.server_address[1]
                assert swept == [True]
                assert _req(port, "GET", "/healthz")[0] == 200
                assert post(port, path, [("photo.jpg", JPEG)])[0] == 200
                # A rebuilt export gets hashes for the newly served bytes.
                (tmp_path / "index.html").write_text("<script>changed()</script>")
                for route, expected in (
                    ("/", web.script_hashes((tmp_path / "index.html").read_bytes())),
                    (path, "default-src 'none'; frame-ancestors 'none'"),
                ):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    conn.request("GET", route, headers={"Host": "books.example"})
                    response = conn.getresponse()
                    assert response.status == 200
                    policies = response.headers.get_all("Content-Security-Policy")
                    assert len(policies) == 1
                    assert expected in policies[0]
                    if route == path:
                        assert "script-src" not in policies[0]
                    assert response.headers["Referrer-Policy"] == "no-referrer"
                    assert response.headers["X-Frame-Options"] == "DENY"
                    response.read()
                    conn.close()
            finally:
                srv.shutdown()
                thread.join()
                srv.server_close()

        srv.serve_forever = exercise
        return srv

    monkeypatch.setattr(web, "Server", listener)
    web.serve()


def test_upload_trusted_proxy_origin(ledger, server, monkeypatch):
    path = upload_links.create(ledger, "NORD", "Phone")["path"]
    headers = {
        "Origin": "https://books.example",
        "X-Forwarded-Host": "books.example",
        "X-Forwarded-Proto": "https",
    }
    assert post(server, path, [("photo.jpg", JPEG)], **(headers | {"X-Forwarded-Proto": None}))[0] == 403
    assert post(server, path, [("photo.jpg", JPEG)], **headers)[0] == 200
    for overrides in (
        {"X-Forwarded-Proto": "http"},
        {"Origin": "https://other.example"},
        {"Origin": "https://receipts.example/path"},
        {"Origin": "https://receipts.example?q=1"},
        {"Origin": "https://receipts.example#fragment"},
        {"Origin": ""},
        {"Origin": "null"},
        {"Origin": "http://["},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        assert post(server, path, [("photo.jpg", JPEG)], **(headers | overrides))[0] == 403


def item_payload(**changes):
    return {
        "lines": [{"name": "Cablu USB 2", "unit": "H87", "quantity": "2", "net": "100.00", "vat": "21"}],
        "total": "121.00",
        "currency": "RON",
        "payment": "cash",
        **changes,
    }


def structured_purchase(ledger, home):
    made = upload_links.create(ledger, "NORD", "Phone")
    sha = intake.accept(ledger, made["path"].split("/u/")[1], [("jpg", JPEG)])[0]["sha"]
    node = '<cac:InvoiceLine><cbc:InvoicedQuantity unitCode="H87">2</cbc:InvoicedQuantity><cbc:LineExtensionAmount>100.00</cbc:LineExtensionAmount><cac:Item><cbc:Name>Cablu USB 2</cbc:Name><cac:ClassifiedTaxCategory><cbc:Percent>21</cbc:Percent></cac:ClassifiedTaxCategory></cac:Item></cac:InvoiceLine>'
    raw = anaf_zip(ubl(total="121.00").replace(b"</Invoice>", node.encode() + b"</Invoice>"))

    ef.store(ledger, "NORD", {"id": "8001", "data_creare": "202610211200"}, raw)
    return sha


@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, True),
        ({"name": "Hârtie"}, False),
        ({"name": "Cablu USB 3"}, False),
        ({"quantity": "1"}, False),
        ({"unit": "SET"}, False),
        ({"vat": ""}, False),
        ({"net": "110.00"}, False),
    ],
)
def test_receipt_line_rules(change, expected):
    left = item_payload()
    right = deepcopy(left)
    right["lines"][0].update(change)
    assert (signatures(left) == signatures(right)) is expected
    left["lines"].append({"name": "Adaptor", "unit": "H87", "quantity": "1", "net": "10.00", "vat": "21"})
    left["total"] = "133.10"
    assert signatures(left) == signatures({**left, "lines": list(reversed(left["lines"]))})
    assert signatures({**left, "total": "120.00"}) is None


def test_receipt_confirm_is_atomic_and_never_payment(ledger, home):
    sha = structured_purchase(ledger, home)
    assert not ledger.save_receipt_items("STUDIO9", sha, item_payload(), 0)
    assert ledger.save_receipt_items("NORD", sha, item_payload(), 0)
    assert receipt_items.matching(ledger, "NORD", sha) == ["8001"]
    assert not receipt_items.confirm(ledger, "STUDIO9", sha, "8001", 1)
    assert not receipt_items.confirm(ledger, "NORD", sha, "8001", 0)

    def attempt():
        other = Ledger(ledger.path)
        try:
            return receipt_items.confirm(other, "NORD", sha, "8001", 1)
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    assert ledger.document(sha)["efactura_id"] == "8001"
    assert ledger.efactura("8001")["status"] == "open"
    assert not ledger.efactura("8001")["paid_tx"]
    assert not ledger.save_receipt_items("NORD", sha, item_payload(), 1)
    assert ledger.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 0


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("live_mode", [False, True])
def test_uploaded_receipt_ai_is_untrusted_and_optional(ledger, home, monkeypatch, fail, live_mode):
    sha = structured_purchase(ledger, home)
    if live_mode:
        (home / "state" / "LIVE").touch()
    monkeypatch.setattr(ai, "ask_json", lambda *a, **kw: pytest.fail("unconfigured AI called"))
    receipt_items.extract_pending(ledger)
    assert not ledger.db.execute("SELECT 1 FROM receipt_items").fetchone()
    integrations.add(
        ledger, "ai", "Local", {"provider": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "test"}
    )

    def ask(L, system, prompt, schema, **kw):
        assert "untrusted" in system and schema == receipt_items.SCHEMA
        assert kw["image"] == ("image/jpeg", JPEG)
        if fail:
            raise ai.AIError("no decision")
        return item_payload()

    monkeypatch.setattr(ai, "ask_json", ask)
    receipt_items.extract_pending(ledger)
    detail = receipt_items.details(ledger, "NORD", sha)
    assert detail["lines"] == ([] if fail else item_payload()["lines"])
    assert not detail["verified"] and detail["proposal"] is None
    assert ledger.save_receipt_items("NORD", sha, item_payload(), 1)
    assert receipt_items.details(ledger, "NORD", sha)["proposal"] == "8001"


def test_cash_receipt_warns_only_for_verified_card_debit(ledger, home):
    sha = structured_purchase(ledger, home)
    assert ledger.save_receipt_items("NORD", sha, item_payload(), 0)
    assert receipt_items.confirm(ledger, "NORD", sha, "8001", 1)
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO statement_txs(tx_key,company,direction,tx) VALUES('card-proof','NORD','debit',?)",
            (json.dumps({"card_last4": "9876"}),),
        )
    assert ledger.assign_card("NORD", "card-proof", "NORD")
    assert ledger.mark_efactura_paid("8001", "statement", "card-proof")
    assert receipt_items.details(ledger, "NORD", sha)["payment_mismatch"]
    with ledger.tx() as db:
        db.execute("UPDATE statement_txs SET company='STUDIO9' WHERE tx_key='card-proof'")
    assert not receipt_items.details(ledger, "NORD", sha)["payment_mismatch"]


def test_moved_receipt_never_reports_a_save_it_did_not_make(ledger, home):
    sha = structured_purchase(ledger, home)
    assert ledger.save_receipt_items("NORD", sha, item_payload(), 0)
    with ledger.tx() as db:
        db.execute("UPDATE documents SET company='STUDIO9' WHERE sha=?", (sha,))
    assert not ledger.save_receipt_items("STUDIO9", sha, item_payload(), 0)
    assert receipt_items.details(ledger, "STUDIO9", sha)["lines"] == []


def test_stale_edits_and_a_second_receipt_cannot_replace_verified_evidence(ledger, home):
    sha = structured_purchase(ledger, home)
    assert not receipt_items.confirm(ledger, "NORD", sha, "8001", 0)
    assert ledger.save_receipt_items("NORD", sha, item_payload(), 0)
    assert not ledger.save_receipt_items("NORD", sha, item_payload(lines=[]), 0, verified=False)
    assert not receipt_items.confirm(ledger, "NORD", sha, "missing", 1)
    assert receipt_items.confirm(ledger, "NORD", sha, "8001", 1)
    other = "f" * 64
    ledger.add_document(other, "NORD", "2026-10", "receipt", str(home / "NORD" / "other.jpg"))
    assert ledger.save_receipt_items("NORD", other, item_payload(), 0)
    assert not receipt_items.confirm(ledger, "NORD", other, "8001", 1)
    assert ledger.document(other)["efactura_id"] is None
    assert not ledger.link_bon(other, "8001", "another receipt")
    assert ledger.efactura("8001")["status"] == "open"


def test_receipt_writer_rechecks_a_previously_eligible_proposal(ledger, home):
    sha = structured_purchase(ledger, home)
    assert ledger.save_receipt_items("NORD", sha, item_payload(), 0)
    with ledger.tx() as db:
        db.execute("UPDATE efactura SET company='STUDIO9' WHERE message_id='8001'")
    assert not ledger.confirm_receipt_invoice("NORD", sha, "8001", 1, lambda: True)
    assert ledger.document(sha)["efactura_id"] is None
    with ledger.tx() as db:
        db.execute("UPDATE efactura SET company='NORD' WHERE message_id='8001'")
    assert receipt_items.confirm(ledger, "NORD", sha, "8001", 1)
    assert not ledger.confirm_receipt_invoice("NORD", sha, "8001", 1, lambda: True)


def test_receipt_actions_keep_payment_separate(ledger, home):
    sha = structured_purchase(ledger, home)
    (home / "state" / "LIVE").touch()
    body = {"id": sha, "revision": 0, "payload": json.dumps(item_payload())}
    assert not actions.act("receipt-lines-save", "STUDIO9", body)["ok"]
    assert actions.act("receipt-lines-save", "NORD", body)["ok"]
    assert api._documents(ledger, "NORD")[0]["items"]["proposal"] == "8001"
    assert actions.act("receipt-invoice-confirm", "NORD", {"id": sha, "revision": 1, "invoice": "8001"})["ok"]
    assert ledger.efactura("8001")["status"] == "open"


def card_debit(ledger, co="NORD", n=1, direction="debit"):
    t = statements.Tx(
        date(2026, 10, 6), "POS", f"Plata {n}", direction, Decimal("86.40"), "RON", "", ref=f"P{n}", card_last4="1234"
    )
    ledger.record_statement(statements.Statement(co, None, None, [statements.Account("x", "RON", [t])]))
    key = statements.tx_key(t)
    assert ledger.card_owner(co, "1234") or ledger.assign_card(co, key, co)
    return key


def explanation(ledger, key):
    view = transactions.view(ledger, companies.get("NORD"), date(2026, 10, 27), settlements=[], ambiguous=[], live=True)
    return next(r for r in view["rows"] if r["id"] == key)["explanation"]


def test_a_bon_uploaded_from_a_card_payment_explains_it_once(ledger, server, home, monkeypatch):
    key = card_debit(ledger)
    code, body = member_post(server, "co=NORD&tx=" + key, [("photo.jpg", JPEG)])
    sha = hashlib.sha256(JPEG).hexdigest()
    assert code == 200 and body == {"ok": True, "files": [{"sha": sha, "duplicate": False}]}
    doc = ledger.document(sha)
    assert (doc["month"], doc["doc_date"], doc["amount"], doc["currency"], doc["card_last4"]) == (
        "2026-10",
        "2026-10-06",
        "86.40",
        "RON",
        "1234",
    )
    said = explanation(ledger, key)
    assert said["kind"] == "receipt" and said["document"] == sha
    assert [d["held"] for d in api.state(ledger, "NORD")["documents"]] == [False]
    assert [d["sha"] for d in pack.pending_docs(ledger, "NORD", "2026-10")] == [sha]
    # trusted ingestion of the same bytes never takes over a linked bon: no second match, no undo over its data
    (home / "trusted.jpg").write_bytes(JPEG)
    monkeypatch.setattr(docs, "inspect", lambda path: docs.Evidence(str(path), mentions=["STUDIO9"]))
    took = docs.add_receipt(ledger, "STUDIO9", home / "trusted.jpg", month="2026-11", source="manual", amount="90.00")
    assert (took["status"], took["company"], took["month"]) == ("duplicate", "NORD", "2026-10")
    kept = ledger.document(sha)
    assert (kept["amount"], kept["company"], kept["source"]) == ("86.40", "NORD", "upload:user:u_member")
    assert recon._receipts(ledger, "NORD") == [] and not ledger.get_meta("upload-takeover:" + sha)
    # one debit, one bon: a second one is refused before anything is stored
    assert member_post(server, "co=NORD&tx=" + key, [("other.png", PNG)])[0] == 409
    assert not ledger.document(hashlib.sha256(PNG).hexdigest())
    # an undo gives the bon back as it came: no data of its own, out of the pack again
    links.unlink(ledger, "NORD", key, said["link"]["id"])
    assert ledger.document(sha)["amount"] is None and pack.pending_docs(ledger, "NORD", "2026-10") == []


@pytest.mark.parametrize("case", ["other firm", "credit", "two files", "bon of another debit"])
def test_a_bon_for_a_debit_it_cannot_explain_is_refused(ledger, server, case):
    first = card_debit(ledger)
    assert member_post(server, "co=NORD&tx=" + first, [("photo.jpg", JPEG)])[0] == 200
    key, files = {
        "other firm": (lambda: card_debit(ledger, "STUDIO9", 2), [("x.png", PNG)]),
        "credit": (lambda: card_debit(ledger, n=3, direction="credit"), [("x.png", PNG)]),
        "two files": (lambda: card_debit(ledger, n=4), [("x.png", PNG), ("y.png", PNG + b"2")]),
        "bon of another debit": (lambda: card_debit(ledger, n=5), [("again.jpg", JPEG)]),
    }[case]
    code, _ = member_post(server, "co=NORD&tx=" + key(), files)
    assert code == (400 if case == "two files" else 409)
    assert not ledger.document(hashlib.sha256(PNG).hexdigest())
    assert ledger.linked_receipts("NORD") == {hashlib.sha256(JPEG).hexdigest()}  # the first bon is not moved
    assert explanation(ledger, first)["document"] == hashlib.sha256(JPEG).hexdigest()


def test_two_connections_naming_one_bon_for_two_debits_have_one_winner(ledger):
    keys = [card_debit(ledger, n=n) for n in (1, 2)]
    (stored,) = intake.accept_member(ledger, intake.member("NORD", "u_member"), [("jpg", JPEG)])

    def attempt(key):
        L = Ledger()
        try:
            return links.receipt(L, "NORD", key, stored["sha"])["tx"]
        except LinkRefused:
            return None
        finally:
            L.close()

    with ThreadPoolExecutor(2) as pool:
        won = [k for k in pool.map(attempt, keys) if k]
    assert len(won) == 1 and ledger.linked_receipts("NORD") == {stored["sha"]}
