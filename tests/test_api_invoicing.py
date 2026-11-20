import base64
import html
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from test_api import (
    _req,  # noqa: F401
    server as server,
)
from test_api_v1 import request
from test_invoice_lookups import response
from test_invoicing import DATA, LINE, PARTY, draft, setup_invoice as setup_invoice  # noqa: F401

from fiscalm import (
    integrations,
    ledger as store,
)
from fiscalm.api import (
    actions,
    auth,
    mcp,
    mcp_tools,
    schema as api_schema,
    state as api_resources,
    state as state_api,
    tokens,
    v1 as api_v1,
    web,
)
from fiscalm.efactura import client as anaf, public as anaf_public
from fiscalm.invoicing import branding, engine as i, spv, workspace as api_invoicing, workspace as invoicing_api
from fiscalm.ledger import Ledger
from fiscalm.mail import invoices as mail_invoices

ROOT = "/companies/NORD/invoicing"


@pytest.fixture
def context(token_owner, setup_invoice, home, monkeypatch):
    L, cid = setup_invoice
    (home / "state" / "LIVE").touch()
    monkeypatch.setattr(store, "now", lambda: DATA["issue_date"] + "T12:00:00")
    secret = tokens.create(L, "Synthetic writer", "write", user_id=token_owner)["token"]
    return L, cid, secret, tokens.verify(L, secret)


def v1(L, cred, op, body, key=None):
    """Run one public invoicing write for NORD the way HTTP and MCP do, without a server."""
    route = next(r for r in api_v1.ROUTES if r.resource == "invoicing_" + op)
    return api_v1.execute_as(L, cred, route, {"co": "NORD"}, {}, body, idempotency_key=key)


def post(server, token, op, body, **headers):
    return request(server, token, ROOT + "/" + op, "POST", body, **headers)


def issued(L, cid, status=None, series="TEST"):
    """An issued NORD invoice; with `status`, also its SPV submission in that state."""
    iid = i.save_draft(L, "NORD", series, cid, DATA)
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    if status:
        with L.tx() as db:
            db.execute(
                "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,?,?)",
                (iid, anaf.env(), status, "synthetic"),
            )
    return iid


def on_two_connections(L, call):
    """Run call(ledger) on two connections released at the same moment."""
    gate = Barrier(2)

    def run(_):
        other = Ledger(L.path)
        try:
            gate.wait(timeout=5)
            return call(other)
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        return list(pool.map(run, range(2)))


def revoke_when_locking(L, cred, monkeypatch):
    """Revoke the token after authentication, just before the next write transaction starts."""
    original = L.tx

    @contextmanager
    def revoke_then_lock(*, immediate=True):
        if not immediate:
            with original(immediate=False) as db:
                yield db
            return
        monkeypatch.setattr(L, "tx", original)
        tokens.revoke(L, cred["id"])
        with original() as db:
            yield db

    monkeypatch.setattr(L, "tx", revoke_then_lock)


def test_http_workspace_draft_scopes_and_company(token_owner, context, server, monkeypatch):
    L, cid, secret, _ = context
    reader = tokens.create(L, "Reader", "read", user_id=token_owner)["token"]
    for op in (
        "client-save",
        "client-lookup",
        "draft-save",
        "draft-delete",
        "issue",
        "storno",
        "upload",
        "resolve-upload",
        "auto-upload",
    ):
        assert post(server, reader, op, {})[0] == 403
    code, result = post(server, secret, "draft-save", DATA | dict(series="TEST", client_id=cid))
    assert code == 200 and result["ok"]
    iid = result["id"]
    code, ws = request(server, reader, ROOT)
    assert code == 200 and ws["drafts"][0]["id"] == iid
    assert "logo" not in ws["seller"] and "spv" not in ws["seller"]
    monkeypatch.setattr(
        invoicing_api, "workspace", lambda *a, **kw: pytest.fail("a single-draft read built the workspace")
    )
    assert request(server, reader, ROOT + "/drafts/" + iid) == (200, ws["drafts"][0])
    assert request(server, reader, ROOT + "/drafts/missing")[0] == 404
    assert request(server, reader, "/companies/STUDIO9/invoicing/drafts/" + iid)[0] == 404
    assert request(server, secret, "/companies/STUDIO9/invoicing/issue", "POST", {"id": iid, "version": 1})[0] == 409
    assert post(server, secret, "draft-delete", {"id": iid, "co": "STUDIO9"})[0] == 400
    for op in ("seller-save", "series-create", "logo"):
        assert post(server, secret, op, {})[0] == 404
    code, refused = post(server, secret, "auto-upload", {"on": True})
    assert code == 403 and refused["error"]["code"] == "admin_required"
    for op, body in [
        ("upload", {"id": "absent"}),
        ("resolve-upload", {"id": "absent", "checked_in_spv": True, "upload_id": None}),
    ]:
        assert post(server, secret, op, body)[0] == 409


def test_shadow(context, server, home, monkeypatch):
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    secret = context[2]
    (home / "state" / "LIVE").unlink()
    for op, body in [
        ("issue", {"id": "x", "version": 1}),
        ("storno", {"id": "x", "reason": "Retur"}),
        ("upload", {"id": "x"}),
        ("resolve-upload", {"id": "x", "upload_id": None, "checked_in_spv": True}),
    ]:
        code, result = post(server, secret, op, body)
        assert (code, result["error"]["code"]) == ((403, "shadow_mode") if op == "upload" else (409, "action_refused"))


def test_issue_idempotency_and_downloads(context, server):
    L, cid, secret, _ = context
    iid = draft((L, cid))
    headers = {"Idempotency-Key": "example-issue-1"}
    first = post(server, secret, "issue", {"id": iid, "version": 1}, **headers)
    assert first[0] == 200
    assert post(server, secret, "issue", {"id": iid, "version": 1}, **headers) == first
    assert post(server, secret, "issue", {"id": "different", "version": 1}, **headers)[0] == 409
    assert L.db.execute("SELECT count(*) FROM invoices WHERE status='issued'").fetchone()[0] == 1
    status, invoice = request(server, secret, ROOT + "/issued/" + iid)
    assert status == 200 and invoice["id"] == iid
    assert request(server, secret, ROOT + "/drafts/" + iid)[0] == 404
    for kind, mime in [("pdf", "application/pdf"), ("xml", "application/xml")]:
        code, hdr, raw = _req(
            server,
            "GET",
            "/api/v1" + ROOT + "/issued/" + iid + "/" + kind,
            headers={"Authorization": "Bearer " + secret},
        )
        assert code == 200 and hdr["content-type"] == mime
        assert "inline" in hdr["content-disposition"] and hdr["fiscalm-api-version"] == "1"
        assert raw == invoicing_api.file(L, "NORD", iid, kind)
        assert request(server, secret, "/companies/STUDIO9/invoicing/issued/" + iid + "/" + kind)[0] == 404
        data = mcp_tools.call(L, "read", "get_invoicing_" + kind, {"co": "NORD", "id": iid})
        assert base64.b64decode(data["data"]) == raw


def test_concurrent_issue_and_crash_rollback(context, monkeypatch):
    L, cid, _, cred = context
    iid = draft((L, cid))
    results = on_two_connections(L, lambda other: v1(other, cred, "issue", {"id": iid, "version": 1}, "race"))
    assert results[0] == results[1] and results[0]["ok"]
    assert L.db.execute("SELECT next_number FROM invoice_series").fetchone()[0] == 43
    iid2 = draft((L, cid))
    original = invoicing_api.act

    def crash(*a, **kw):
        original(*a, **kw)
        raise RuntimeError("simulated crash before receipt")

    monkeypatch.setattr(invoicing_api, "act", crash)
    with pytest.raises(RuntimeError):
        v1(L, cred, "issue", {"id": iid2, "version": 1}, "crash")
    assert i.invoice(L, iid2, company="NORD")["status"] == "draft"
    assert L.db.execute("SELECT next_number FROM invoice_series").fetchone()[0] == 43


def test_mcp_storno(context):
    L, cid, _, cred = context
    iid = issued(L, cid, "accepted")
    message = dict(
        jsonrpc="2.0",
        id=1,
        method="tools/call",
        params=dict(
            name="invoicing_storno",
            arguments=dict(co="NORD", body=dict(id=iid, reason="Retur fictiv"), idempotency_key="return-1"),
        ),
    )
    code, result = mcp.dispatch(L, cred, message)
    assert code == 200 and not result["result"]["isError"]
    sid = result["result"]["structuredContent"]["id"]
    assert i.invoice(L, sid, company="NORD")["data"]["billing_ref"] == "TEST-42"
    assert mcp.dispatch(L, cred, message) == (code, result)
    specs = {t["name"]: t for t in mcp_tools.definitions("write")}
    for op in ("issue", "storno", "upload", "resolve_upload", "draft_delete", "draft_save", "client_save"):
        assert specs["invoicing_" + op]["annotations"]["destructiveHint"]
    assert not any(t["name"].startswith("invoicing_") for t in mcp_tools.definitions("read"))


def test_concurrent_storno_same_key(context):
    L, cid, _, cred = context
    iid = issued(L, cid, "accepted")
    results = on_two_connections(
        L, lambda other: v1(other, cred, "storno", {"id": iid, "reason": "Retur"}, "race-storno")
    )
    assert results[0] == results[1] and results[0]["ok"]
    assert L.db.execute("SELECT count(*) FROM invoice_reversals").fetchone()[0] == 1
    assert L.db.execute("SELECT next_number FROM invoice_series").fetchone()[0] == 44


def test_idempotency_atomic_storno_receipt(context):
    L, cid, _, cred = context
    iid = issued(L, cid, "accepted")
    # SQLite authorizer fails the receipt insert after the nested storno has succeeded.
    L.db.set_authorizer(
        lambda action, name, *rest: (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_INSERT and name == "api_invoice_requests"
            else sqlite3.SQLITE_OK
        )
    )
    with pytest.raises(sqlite3.DatabaseError):
        v1(L, cred, "storno", {"id": iid, "reason": "Retur"}, "return")
    L.db.set_authorizer(None)
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 1
    assert L.db.execute("SELECT count(*) FROM invoice_reversals").fetchone()[0] == 0
    assert L.db.execute("SELECT next_number FROM invoice_series").fetchone()[0] == 43


def test_idempotency_expiry_token_isolation_and_body_logs(token_owner, context, server, monkeypatch):
    L, cid, secret, _ = context
    logs = []
    monkeypatch.setattr(actions, "log", logs.append)
    iid = draft((L, cid))
    key = {"Idempotency-Key": "private-key-marker"}
    assert post(server, secret, "issue", {"id": iid, "version": 1}, **key)[0] == 200
    another = tokens.create(L, "Other writer", "write", user_id=token_owner)["token"]
    assert post(server, another, "issue", {"id": iid, "version": 1}, **key)[0] == 409
    with L.tx() as db:
        db.execute("UPDATE api_invoice_requests SET created_at=created_at-86400")
    assert post(server, secret, "issue", {"id": iid, "version": 1}, **key)[0] == 409
    assert post(server, secret, "issue", {"id": iid, "version": 1}, **{"Idempotency-Key": "bad key"})[0] == 400
    assert post(server, secret, "issue", {"id": iid, "note": "private-body-marker", "version": 1})[0] == 400
    text = "\n".join(logs)
    assert all(value not in text for value in (secret, iid, "private-key-marker", "private-body-marker"))
    assert "invoicing_issue" in text
    assert post(server, secret, "issue", {"id": "x" * 4200, "version": 1})[0] == 413


def test_receipt_cross_transport_company_and_shadow(context, server, home):
    L, cid, secret, cred = context
    iid = draft((L, cid))
    args = dict(co="NORD", body={"id": iid, "version": 1}, idempotency_key="shared")
    first = mcp_tools.call(L, "write", "invoicing_issue", args, cred)
    assert mcp_tools.call(L, "write", "invoicing_issue", args, cred) == first
    headers = {"Idempotency-Key": "shared"}
    assert post(server, secret, "issue", {"id": iid, "version": 1}, **headers) == (200, first)
    # The same key in another company names an independent issuance.
    i.save_seller(L, "STUDIO9", PARTY | {"iban": "RO94BTRLRONCRT0000000002"})  # its own account
    i.create_series(L, "STUDIO9", "TEST", 42)
    other = i.save_draft(L, "STUDIO9", "TEST", i.save_client(L, "STUDIO9", PARTY), DATA)
    second = mcp_tools.call(
        L, "write", "invoicing_issue", args | dict(co="STUDIO9", body={"id": other, "version": 1}), cred
    )
    assert second["ok"] and second["id"] != first["id"]
    assert L.db.execute("SELECT count(*) FROM api_invoice_requests").fetchone()[0] == 2
    (home / "state" / "LIVE").unlink()
    assert post(server, secret, "issue", {"id": iid, "version": 1}, **headers) == (200, first)
    assert mcp_tools.call(L, "write", "invoicing_issue", args, cred) == first


def test_utf8_body_limit(context, server):
    _, cid, secret, _ = context
    body = DATA | dict(series="TEST", client_id=cid, notes="ă" * 1500)
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) < 4096
    assert post(server, secret, "draft-save", raw)[0] == 409  # the engine's note limit, not 413


@pytest.mark.parametrize("key,stored", [(None, False), ("revoked", False), ("revoked", True)])
def test_revoked_after_auth_before_transaction(context, monkeypatch, key, stored):
    L, cid, _, cred = context
    iid = draft((L, cid))
    if stored:
        v1(L, cred, "issue", {"id": iid, "version": 1}, key)
    revoke_when_locking(L, cred, monkeypatch)
    with pytest.raises(api_v1.Error) as exc:
        v1(L, cred, "issue", {"id": iid, "version": 1}, key)
    assert exc.value.status == 401 and exc.value.code == "invalid_token"
    assert i.invoice(L, iid, company="NORD")["status"] == ("issued" if stored else "draft")
    assert len(L.db.execute("SELECT * FROM api_invoice_requests").fetchall()) == int(stored)


@pytest.mark.parametrize(
    "path,body",
    [
        ("/companies/NORD/invoicing/auto-upload", {"on": False}),
        ("/mcp", dict(jsonrpc="2.0", id=1, method="tools/list")),
    ],
)
def test_unsupported_idempotency_header(context, server, path, body):
    code, result = request(server, context[2], path, "POST", body, **{"Idempotency-Key": "unsupported"})
    assert (code, result["error"]["code"]) == (400, "unsupported_idempotency_key")


def test_key_schema_and_missing_identity(context):
    specs = {t["name"]: t for t in mcp_tools.definitions("write")}
    header = next(
        p["schema"]
        for p in api_schema.openapi()["paths"]["/api/v1/companies/{co}/invoicing/issue"]["post"]["parameters"]
        if p["name"] == "Idempotency-Key"
    )
    for op in ("issue", "storno"):
        schema = specs["invoicing_" + op]["inputSchema"]["properties"]["idempotency_key"]
        assert schema == header
        assert schema["maxLength"] == 128
        for bad in ("", "bad key", "a" * 129, "ă", "key\n"):
            with pytest.raises(ValueError):
                mcp_tools.validate(schema, bad)
    L, cid, _, _ = context
    with pytest.raises(api_v1.Error) as exc:
        v1(L, {"scope": "write"}, "issue", {"id": draft((L, cid)), "version": 1}, "valid")
    assert (exc.value.status, exc.value.code) == (401, "invalid_token")


def test_nested_savepoints_and_transaction_abort(context):
    L = context[0]
    with L.tx() as db:
        db.execute("INSERT INTO meta VALUES('outer', 'yes')")
        with pytest.raises(ValueError), L.tx():
            L.set_meta("nested", "no")
            raise ValueError("nested refusal")
        assert L.get_meta("outer") == "yes" and L.get_meta("nested") is None
        with L.tx():
            L.set_meta("success", "yes")
        assert db.in_transaction
    with pytest.raises(RuntimeError), L.tx():
        L.set_meta("success", "rolled back")
        raise RuntimeError("outer refusal")
    assert L.get_meta("success") == "yes"
    # RAISE(ROLLBACK) simulates SQLite aborting the entire transaction on IO failure.
    L.db.execute(
        "CREATE TEMP TRIGGER abort_meta BEFORE INSERT ON meta WHEN NEW.key='abort' "
        "BEGIN SELECT RAISE(ROLLBACK, 'simulated disk full'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="simulated disk full"), L.tx():
        L.set_meta("abort", "no")
    assert not L.db.in_transaction


def test_shared_shadow_policy(context, monkeypatch):
    L, _, _, cred = context
    seen = []

    def refuse(op):
        seen.append(op)
        return False

    monkeypatch.setattr(invoicing_api, "allowed_in_mode", refuse)
    assert not invoicing_api.act(L, "auto-upload", "NORD", {"on": False})["ok"]
    with pytest.raises(api_v1.Error) as exc:
        v1(L, cred, "auto-upload", {"on": False})
    assert exc.value.code == "shadow_mode" and seen == ["auto-upload", "auto-upload"]


@pytest.mark.parametrize("op,body", [("draft-delete", {"id": "missing"}), ("client-lookup", {"cui": "12345678"})])
def test_unkeyed_writes_recheck_token_before_dispatch(context, monkeypatch, op, body):
    L, _, _, cred = context
    tokens.revoke(L, cred["id"])
    monkeypatch.setattr(
        api_invoicing, "perform", lambda *a, **kw: pytest.fail("a revoked token reached the dispatcher")
    )
    with pytest.raises(api_v1.Error) as exc:
        v1(L, cred, op, body)
    assert exc.value.status == 401 and exc.value.code == "invalid_token"


def test_unkeyed_upload_rechecks_token_and_commits_claim(token_owner, context, monkeypatch):
    L, cid, _, cred = context
    iid = issued(L, cid)
    integrations.add(L, "anaf", "Synthetic ANAF", {"client_id": "fake", "client_secret": "fake"})
    revoke_when_locking(L, cred, monkeypatch)
    with pytest.raises(api_v1.Error) as exc:
        v1(L, cred, "upload", {"id": iid})
    assert exc.value.code == "invalid_token"
    assert not L.db.execute("SELECT 1 FROM invoice_submissions").fetchone()
    cred = tokens.verify(L, tokens.create(L, "Replacement", "write", user_id=token_owner)["token"])

    def crash(method, url, body):
        other = Ledger(L.path)
        try:
            assert (
                other.db.execute("SELECT status FROM invoice_submissions WHERE invoice_id=?", (iid,)).fetchone()[0]
                == "uploading"
            )
        finally:
            other.close()
        raise KeyboardInterrupt("simulated process crash after durable claim")

    monkeypatch.setattr(anaf, "prepare_invoice_request", lambda *args, **kwargs: crash)
    with pytest.raises(KeyboardInterrupt):
        v1(L, cred, "upload", {"id": iid})
    assert (
        L.db.execute("SELECT status FROM invoice_submissions WHERE invoice_id=?", (iid,)).fetchone()[0] == "uploading"
    )


def test_legal_fields_survive_strict_http_schema(context, server):
    L, _, secret, _ = context
    client = invoicing_api.party_out(PARTY | dict(country="US", cui="", vat_id="", tax_id="LOCAL 123"))
    code, saved = post(server, secret, "client-save", {"client": client | dict(ro_vat_status="not_registered")})
    assert code == 200 and saved["ok"]
    code, result = post(
        server,
        secret,
        "draft-save",
        DATA
        | dict(
            series="TEST",
            client_id=saved["id"],
            advance_invoice=True,
            advance_payment_date="2026-09-01",
            supply_country="DE",
        ),
    )
    assert code == 200 and result["ok"]
    code, preview = request(server, secret, ROOT + "/drafts/" + result["id"])
    assert code == 200 and preview["client"]["tax_id"] == "LOCAL 123"
    assert preview["client"]["ro_vat_status"] == "not_registered" and preview["supply_country"] == "DE"
    assert preview["chargeability_date"] == "2026-09-01" and preview["warnings"]
    assert preview["advance_invoice"] is True
    assert spv.scope(i.invoice(L, result["id"], company="NORD")) == "not_required"


def test_strict_versions_and_specific_conflicts(context, server):
    L, cid, secret, cred = context
    iid = draft((L, cid))
    assert post(server, secret, "issue", {"id": iid})[0] == 400
    with pytest.raises(ValueError):
        mcp_tools.call(L, "write", "invoicing_issue", dict(co="NORD", body={"id": iid}), cred)
    body = DATA | dict(id=iid, series="TEST", client_id=cid)
    with pytest.raises(ValueError):
        mcp_tools.call(L, "write", "invoicing_draft_save", dict(co="NORD", body=body), cred)
    for op in ("issue", "draft-save", "draft-delete"):
        payload = body if op == "draft-save" else {"id": iid}
        assert post(server, secret, op, payload | dict(version=0))[0] == 400
    result = mcp_tools.call(L, "write", "invoicing_draft_save", dict(co="NORD", body=body | dict(version=1)), cred)
    assert result["version"] == 2
    for op in ("issue", "draft-save", "draft-delete"):
        payload = body if op == "draft-save" else {"id": iid}
        code, result = post(server, secret, op, payload | dict(version=1))
        assert code == 409 and result["error"]["code"] == "stale_draft"
    assert i.invoice(L, iid, company="NORD")["version"] == 2
    assert "version" in api_schema.openapi()["components"]["schemas"]["InvoicingDraft"]["properties"]


def test_client_lookup_caches_inside_caller_transaction(context, server, monkeypatch):
    L, _, secret, _ = context
    day = DATA["issue_date"]
    real_urlopen = anaf_public.urllib.request.urlopen

    def open_url(req, timeout):
        if req.full_url != anaf_public.URL:
            return real_urlopen(req, timeout=timeout)
        assert json.loads(req.data) == [dict(cui=99999984, data=day)]
        data = response()
        data["found"][0]["date_generale"]["data"] = day
        data["found"][0]["inregistrare_scop_Tva"]["perioade_TVA"] = [dict(data_inceput_ScpTVA="2026-01-01")]
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(anaf_public.urllib.request, "urlopen", open_url)
    code, result = post(server, secret, "client-lookup", dict(cui="99999984"))
    assert code == 200 and result["ok"] and result["client"]["cui"] == "99999984"
    status, error = anaf_public.cached(L, "99999984")
    assert error is None and status["checked_at"] == day
    assert status["vat_since"] == "2026-01-01"


# ---- documents served to the browser: refusals are plain HTML that leaks nothing

MISSING = "Factura nu a fost găsită. Încearcă din nou din fiscalm."


@pytest.mark.parametrize(
    "route,failure,status,message",
    [
        ("missing.pdf", "missing", 404, MISSING),
        ("missing.xml", "logo", 409, branding.UNAVAILABLE),
        ("preview.pdf?draft=missing", "path", 404, MISSING),
    ],
)
def test_document_refusals_are_safe_html(server, monkeypatch, route, failure, status, message):
    def refuse(*args, **kwargs):
        if failure == "logo":
            raise i.InvoiceError(branding.UNAVAILABLE)
        if failure == "path":
            raise OSError("/private/storage/secret.pdf")
        raise i.InvoiceError("Factura nu a fost găsită.")

    monkeypatch.setattr(invoicing_api, "file", refuse)
    code, headers, raw = _req(server, "GET", "/web/invoicing/" + route + ("&" if "?" in route else "?") + "co=NORD")
    page = raw.decode()
    assert code == status and headers["content-type"] == "text/html; charset=utf-8"
    assert html.escape(message) in page and page.startswith("<!doctype html>") and '{"error"' not in page
    assert "/private/" not in page and "<script" not in page
    assert headers["x-frame-options"] == "DENY" and headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer" and headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


@pytest.mark.parametrize("route", ["absent.pdf", "absent.xml", "preview.pdf?draft=absent"])
def test_a_missing_invoice_is_a_plain_404(server, route):
    code, headers, raw = _req(server, "GET", "/web/invoicing/" + route + ("&" if "?" in route else "?") + "co=NORD")
    assert code == 404 and headers["content-type"] == "text/html; charset=utf-8" and MISSING in raw.decode()


def test_document_refusal_escapes_its_message():
    handler = object.__new__(web.Handler)
    sent = []
    handler._send = lambda *args: sent.append(args)
    handler._invoice_error(409, 'Verifică <sigla> & "factura".')
    assert b"&lt;sigla&gt; &amp; &quot;factura&quot;" in sent[0][1] and b"<sigla>" not in sent[0][1]


@pytest.mark.parametrize("route", ["absent.xml?co=BAD", "x.txt?co=NORD", "a<b.pdf?co=NORD"])
def test_invalid_document_routes_never_reach_the_reader(server, monkeypatch, route):
    monkeypatch.setattr(invoicing_api, "file", lambda *a, **k: pytest.fail("reached the invoice reader"))
    code, headers, raw = _req(server, "GET", "/web/invoicing/" + route)
    assert code == 404 and headers["content-type"] == "text/html; charset=utf-8"
    assert MISSING in raw.decode() and "a<b" not in raw.decode()
    assert _req(server, "GET", "/web/invoicing/logo?co=BAD")[::2] == (404, b"not found")


def test_document_head_is_refused(server, monkeypatch):
    monkeypatch.setattr(invoicing_api, "file", lambda *a, **k: pytest.fail("HEAD reached the invoice reader"))
    code, headers, raw = _req(server, "HEAD", "/web/invoicing/absent.pdf?co=NORD")
    assert code == 405 and raw == b"" and headers["content-length"] == str(len(b"method not allowed"))


def test_invoice_and_preview_headers(setup_invoice, server):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    code, preview, _ = _req(server, "GET", f"/web/invoicing/preview.pdf?co=NORD&draft={iid}")
    assert code == 200 and preview["x-content-type-options"] == "nosniff"
    assert "sandbox" not in preview["content-security-policy"]
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    for ext in ("pdf", "xml"):
        code, headers, _ = _req(server, "GET", f"/web/invoicing/{iid}.{ext}?co=NORD")
        assert code == 200 and headers["x-content-type-options"] == "nosniff"
        assert headers["content-disposition"].startswith("inline; filename=")
        assert ("sandbox" in headers["content-security-policy"]) is (ext == "xml")


@pytest.mark.parametrize("kind", ["mailfile", "docfile"])
def test_attachments_keep_the_pdf_policy(ledger, server, monkeypatch, kind):
    key = "a" * 64
    monkeypatch.setattr(
        mail_invoices, "pending_file", lambda L, sha: ("pdf", b"%PDF-synthetic") if sha == key else None
    )
    monkeypatch.setattr(web.api, "_to_check", lambda L, co: [{"key": key}])
    monkeypatch.setattr(web.api, "read_doc", lambda *_: ("test.pdf", b"%PDF-synthetic"))
    code, headers, _ = _req(server, "GET", f"/web/{kind}?key={key}&co=NORD&sha=synthetic")
    assert code == 200 and headers["content-security-policy"] == "default-src 'none'; form-action 'none'"
    assert headers["content-disposition"] == web.inline("document.pdf" if kind == "mailfile" else "test.pdf")
    assert headers["referrer-policy"] == "no-referrer" and headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == ("private, max-age=3600" if kind == "docfile" else "no-store")


# ---- the app's actions: refusals, shadow mode, what the workspace promises


@pytest.mark.parametrize(
    "op,key,bad",
    [
        ("client-lookup", "cui", 3),
        ("draft-save", "series", []),
        ("upload", "id", {}),
        ("resolve-upload", "upload_id", True),
    ],
)
def test_every_action_refuses_a_non_text_identifier(context, op, key, bad):
    L, cid, _, _ = context
    body = dict(id=draft((L, cid)), series="TEST", client_id="unused", code="NEXT", cui="99999984", on=True) | {
        key: bad
    }
    result = invoicing_api.act(L, op, "NORD", body)
    assert result["ok"] is False
    assert result["message"] == (
        actions.BAD if key == "id" else "Identificatorul trebuie să fie un text nevid de cel mult 128 de caractere."
    )


@pytest.mark.parametrize("op,body", [("auto-upload", {"on": []}), ("series-create", {"code": "NEXT", "start": []})])
def test_configuration_values_of_the_wrong_type_are_refused(context, op, body):
    assert invoicing_api.act(context[0], op, "NORD", body)["ok"] is False


@pytest.mark.parametrize("change", [{"name": ""}, {"county": "bad"}])
def test_client_refusals_reach_the_app_as_the_engine_says_them(setup_invoice, change):
    L, _ = setup_invoice
    message = invoicing_api.act(L, "client-save", "NORD", dict(client=invoicing_api.party_out(PARTY | change)))[
        "message"
    ]
    with pytest.raises(i.InvoiceError) as exc:
        i.save_client(L, "NORD", PARTY | change)
    assert message == str(exc.value)


@pytest.mark.parametrize("where", ["top", "address"])
def test_client_country_roundtrips_and_a_conflict_is_refused(setup_invoice, where):
    L, _ = setup_invoice
    client = invoicing_api.party_out(PARTY)
    del (client["address"] if where == "top" else client)["country"]
    result = invoicing_api.act(L, "client-save", "NORD", dict(client=client))
    saved = next(c for c in invoicing_api.workspace(L, "NORD")["clients"] if c["id"] == result["id"])
    assert saved["country"] == saved["address"]["country"] == "RO"
    conflict = invoicing_api.party_out(PARTY) | dict(country="DE", vat_id="DE999999999")
    result = invoicing_api.act(L, "client-save", "NORD", dict(client=conflict))
    assert not result["ok"] and result["message"] == "Țara clientului diferă de țara din adresă."


@pytest.mark.parametrize("category", ["S", "K"])
def test_workspace_keeps_delivery_and_precise_lines(setup_invoice, category):
    L, cid = setup_invoice
    i.save_client(L, "NORD", PARTY | dict(country="DE", vat_id="DE999999999"), cid)
    line = LINE | dict(
        unit_price="0.125",
        quantity="3.125",
        vat_category=category,
        vat_rate="0" if category == "K" else "21",
        exemption="VATEX-EU-IC" if category == "K" else "",
    )
    body = DATA | dict(series="TEST", client_id=cid, delivery_date=DATA["issue_date"], lines=[line])
    assert invoicing_api.act(L, "draft-save", "NORD", body)["ok"]
    workspace = invoicing_api.workspace(L, "NORD")
    before = workspace["drafts"][0]
    assert (before["lines"][0]["unit_price"], before["lines"][0]["quantity"]) == ("0.125", "3.125")
    assert before["delivery_date"] == DATA["issue_date"]
    assert {"legal_name", "cui", "vat_payer", "iban", "bank"} <= set(workspace["required"]["seller"])
    assert "address.county" in workspace["required"]["client"]["RO"]
    assert "address.county" not in workspace["required"]["client"]["other"]
    saved = {
        k: before[k]
        for k in (
            "id",
            "version",
            "series",
            "client_id",
            "issue_date",
            "due_date",
            "delivery_date",
            "currency",
            "notes",
            "lines",
        )
    }
    assert invoicing_api.act(L, "draft-save", "NORD", saved)["ok"]
    assert invoicing_api.workspace(L, "NORD")["drafts"][0]["totals"] == before["totals"]
    assert L.get_meta("draft_client:" + before["id"])
    i.delete_draft(L, before["id"], company="NORD")
    assert L.get_meta("draft_client:" + before["id"]) is None and L.get_meta("draft_updated:" + before["id"]) is None


def test_a_new_invoice_starts_from_the_latest_terms_and_lines(setup_invoice):
    L, cid = setup_invoice
    other = i.save_client(L, "NORD", PARTY | dict(name="Alt Client SRL", cui="22222229", vat_id="RO22222229"))
    workspace = invoicing_api.workspace(L, "NORD")
    assert workspace["due_days"] == invoicing_api.DEFAULT_DUE_DAYS == 30
    assert {c["last_invoice"] for c in workspace["clients"]} == {None}
    hours = LINE | dict(quantity="8", unit_price="150")
    for client, due, lines in ((cid, "2026-11-14", [hours, LINE]), (other, "2026-11-07", [LINE]), (cid, "", [LINE])):
        iid = i.save_draft(L, "NORD", "TEST", client, DATA | dict(due_date=due or DATA["due_date"], lines=lines))
        if due:  # the last one stays a draft: only issued invoices count
            issued = i.issue(L, iid, "2026-10-31", company="NORD", version=1)["id"]
    workspace = invoicing_api.workspace(L, "NORD")
    assert workspace["due_days"] == 7
    last = {c["id"]: c["last_invoice"] for c in workspace["clients"]}
    assert last[other] == dict(due_days=7, currency="RON", lines=[LINE | dict(exemption="")])
    # Neither the cancelled invoice nor its credit note counts.
    i.storno(L, issued, "2026-11-05", "Anulare", company="NORD")
    workspace = invoicing_api.workspace(L, "NORD")
    last = {c["id"]: c["last_invoice"] for c in workspace["clients"]}
    assert (workspace["due_days"], last[other]) == (14, None)
    assert last[cid]["due_days"] == 14
    assert [(x["quantity"], x["unit_price"]) for x in last[cid]["lines"]] == [("8", "150"), ("1", "100")]


@pytest.mark.parametrize("live", [False, True])
def test_rejected_invoice_owes_nothing_and_numbers_stay_natural(setup_invoice, home, live):
    L, cid = setup_invoice
    i.create_series(L, "NORD", "DEMO", 9)
    if live:
        (home / "state/LIVE").touch()
    rejected = issued(L, cid, "rejected", "DEMO")
    with L.tx() as db:
        db.execute("UPDATE invoice_submissions SET environment='prod' WHERE invoice_id=?", (rejected,))
    issued(L, cid, series="DEMO")
    state = state_api.state(L, "NORD")
    assert [r["number"] for r in state["issued"]] == ["DEMO-9", "DEMO-10"]
    assert state["receivable"]["total"] == "121.00"
    row = next(r for r in state["issued"] if r["local_id"] == rejected)
    assert row["rejected"] is True and row["owed"] == "0.00" and not row["can_mark"]


def test_v1_and_mcp_read_local_invoices(setup_invoice):
    L, cid = setup_invoice
    iid = issued(L, cid)
    mid = i.import_id("local", iid)
    row = api_resources.resource(L, "issued_one", "NORD", {"id": mid}, {})
    assert row["local_id"] == iid and row["links"]["transaction"] is None and row["can_resolve"] is False
    assert mcp_tools.call(L, "read", "get_invoice", {"co": "NORD", "side": "issued", "id": mid}) == row
    assert "can_resolve" in api_schema.schemas()["Issued"]["required"]


def test_app_invoice_write_rechecks_revocation_on_another_connection(context):
    L, cid, secret, credential = context
    iid = draft((L, cid))
    other = Ledger(L.path)
    try:
        assert tokens.revoke(other, credential["id"])
    finally:
        other.close()
    checked = []

    def authorize():
        checked.append(L.db.in_transaction)
        invoicing_api.authorize(L, credential)

    with pytest.raises(api_v1.Error):
        invoicing_api.write_app(
            L, "issue", "NORD", {"id": iid, "version": 1}, authorize=authorize, require=lambda _: True
        )
    assert checked == [True]
    assert i.invoice(L, iid, company="NORD")["status"] == "draft"


@pytest.mark.parametrize("change", ["disable", "demote"])
def test_app_authority_changed_after_dispatch_refuses_the_write(context, server, monkeypatch, change):
    L, _, _, _ = context
    original = invoicing_api.write_app

    stamp = server.service.clock().isoformat()
    L.change_user("u_member", None, stamp, role="admin")
    checks = []
    original_guard = auth.authorize_write

    def checked(ledger, *args):
        checks.append(ledger.db.in_transaction)
        return original_guard(ledger, *args)

    def change_then_write(*args, **kwargs):
        other = Ledger(L.path)
        try:
            other.change_user(
                "u_admin", "u_member", stamp, **({"enabled": False} if change == "disable" else {"role": "readonly"})
            )
        finally:
            other.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(auth, "authorize_write", checked)
    monkeypatch.setattr(invoicing_api, "write_app", change_then_write)
    code, _, _ = _req(
        server,
        "POST",
        "/web/api/invoicing/auto-upload",
        json.dumps({"co": "NORD", "on": False}),
        {"Content-Type": "application/json"},
    )
    assert code == 403 and checks == [True]
    assert L.db.execute("SELECT count(*) FROM invoice_settings WHERE company='NORD'").fetchone()[0] == 0
