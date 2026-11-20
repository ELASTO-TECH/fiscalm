"""fiscalm serve: listen address, origin checks behind a proxy, security headers and what a request needs to get in."""

import http.client
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_api import (
    _req,  # noqa: F401
    server as server,
)

from fiscalm import companies
from fiscalm.api import tokens, transport, web
from fiscalm.ledger import Ledger


def test_address(monkeypatch):
    monkeypatch.delenv("FISCALM_HOST", raising=False)
    monkeypatch.delenv("FISCALM_PORT", raising=False)
    assert web.listen_address() == ("127.0.0.1", 8790)
    monkeypatch.setenv("FISCALM_HOST", "0.0.0.0")
    monkeypatch.setenv("FISCALM_PORT", "9000")
    assert web.listen_address() == ("0.0.0.0", 9000)
    monkeypatch.setattr(web, "tailscale_ip", lambda: "100.64.0.1")
    assert web.listen_address("tailscale", 42) == ("100.64.0.1", 42)
    assert web.listen_address("127.0.0.2", 43) == ("127.0.0.2", 43)


def test_health_and_html(server, tmp_path, monkeypatch):
    assert _req(server, "GET", "/healthz")[2] == b'{"ok":true}'
    monkeypatch.setattr(web, "APP_OUT", tmp_path)
    previous = None
    for script in ("hello()", "changed()"):  # the script hashes follow a rebuilt export
        (tmp_path / "index.html").write_text("<html><script>" + script + "</script></html>")
        code, headers, body = _req(server, "GET", "/")
        csp = headers["content-security-policy"]
        assert code == 200 and "'sha256-" in web.script_hashes(body) and web.script_hashes(body) in csp
        assert previous is None or previous not in csp
        previous = web.script_hashes(body)
    assert headers["referrer-policy"] == "no-referrer" and headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in csp and "form-action 'none'" in csp
    head_code, head_headers, head_body = _req(server, "HEAD", "/healthz")
    assert head_code == 200
    assert head_headers["content-length"] == str(len(b'{"ok":true}'))
    assert head_body == b""


def test_proxy_post_real_server(token_owner, server, monkeypatch):
    monkeypatch.setenv("FISCALM_TRUST_PROXY", "1")
    monkeypatch.setattr(web, "is_live", lambda: True)
    monkeypatch.setattr(web.actions, "act", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(web.api, "state", lambda *a: {})
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://books.example",
        "X-Forwarded-Host": "books.example",
        "X-Forwarded-Proto": "https",
    }
    body = json.dumps({"co": next(iter(companies.all()))})
    assert _req(server, "POST", "/web/api/check", body, headers)[0] == 200
    headers["X-Forwarded-Proto"] = "http"
    assert _req(server, "POST", "/web/api/check", body, headers)[0] == 401
    # V1 authenticates before dispatch; a valid credential reaches the same origin guard.
    L = Ledger()
    credential = tokens.create(L, "Proxy test", "write", user_id=token_owner)
    L.close()
    headers["Authorization"] = "Bearer " + credential["token"]
    assert _req(server, "POST", "/api/v1/companies/NORD/actions/check", body, headers)[0] == 403
    headers["X-Forwarded-Proto"] = "https"
    assert _req(server, "POST", "/api/v1/companies/NORD/actions/check", body, headers)[0] == 200


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png"])
def test_inline_attachment_sandbox(server, monkeypatch, ctype):
    def attachment(self):
        self._send(200, b"file", ctype, {"Content-Disposition": web.inline("example.pdf")})

    monkeypatch.setattr(web.Handler, "do_GET", attachment)
    headers = _req(server, "GET", "/")[1]
    prefix = "" if ctype == "application/pdf" else "sandbox; "
    assert headers["content-security-policy"] == prefix + "default-src 'none'; form-action 'none'"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"


def test_a_tailscale_identity_header_grants_nothing(server):
    """The short link is gone: a Tailscale login header (or one a client forges) opens neither the app nor the API."""
    me = {"Tailscale-User-Login": "owner@example.com"}
    assert _req(server, "GET", "/api/state?co=NORD", headers=me)[0] == 404
    assert _req(server, "GET", "/", headers=me)[0] == 404
    assert _req(server, "GET", "/api/v1/companies", headers=me)[0] == 401
    assert _req(server, "GET", "/web/api/state?co=NORD", headers={**me, "Cookie": None})[0] == 401
    assert (
        _req(server, "GET", "/web/api/state?co=NORD", headers=me)[0] == 200
    )  # the session cookie authorizes; the identity header is ignored


def test_default_app_export_is_the_build_directory(monkeypatch):
    monkeypatch.delenv("FISCALM_APP_OUT", raising=False)
    result = subprocess.run(
        [sys.executable, "-c", "from fiscalm.api.web import APP_OUT; print(APP_OUT)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(result.stdout.strip()) == Path(__file__).resolve().parents[1] / "app" / "out"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/auth/password", {"username": "admin", "password": "synthetic-password"}),
        ("/web/api/check", {"co": "NORD"}),
        ("/web/api/payment-files", {"action": "payment_file_download", "file_id": 1}),
    ],
)
@pytest.mark.parametrize(
    "overrides",
    [
        {"Origin": None},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"X-Forwarded-Proto": None},
    ],
)
def test_every_browser_post_refuses_csrf_before_dispatch(server, monkeypatch, path, body, overrides):
    def forbidden(*args, **kwargs):
        raise AssertionError("Admission must happen before a factor check or financial action")

    monkeypatch.setattr(server.service.browser_auth, "post", forbidden)
    monkeypatch.setattr(web.actions, "act", forbidden)
    monkeypatch.setattr(web.actions.pay, "download_file", forbidden)
    status = _req(server, "POST", path, json.dumps(body), {"Content-Type": "application/json", **overrides})[0]
    assert status in (401, 403, 415)


def raw_get(server, path, headers):
    connection = http.client.HTTPConnection("127.0.0.1", int(server), timeout=2)
    try:
        connection.putrequest("GET", path, skip_host=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


@pytest.mark.parametrize("hosts", [[], ["evil.example"], ["books.example:444"], ["books.example"] * 2])
def test_host_is_pinned_even_with_a_valid_cookie_and_health_is_exempt(server, hosts):
    headers = [("Host", value) for value in hosts] + [
        ("Cookie", "__Host-session=" + "a" * 64),
        ("X-Forwarded-Proto", "https"),
    ]
    assert raw_get(server, "/web/api/me", headers) == 404
    assert raw_get(server, "/healthz", headers) == 200


def test_untrusted_forwarded_headers_and_duplicated_cookies_grant_no_session(server):
    cookie = "__Host-session=" + "a" * 64
    assert _req(server, "GET", "/web/api/me", headers={"Cookie": cookie, "X-Forwarded-Proto": None})[0] == 401
    assert (
        raw_get(
            server,
            "/web/api/me",
            [("Host", "books.example"), ("Cookie", cookie), ("Cookie", cookie), ("X-Forwarded-Proto", "https")],
        )
        == 401
    )
    server.service.boundary = transport.Boundary(server.origin)
    assert (
        _req(server, "GET", "/web/api/me", headers={"Host": "evil.example", "X-Forwarded-Host": "books.example"})[0]
        == 404
    )
    assert _req(server, "GET", "/web/api/me", headers={"Cookie": cookie, "X-Forwarded-Proto": "https"})[0] == 401


@pytest.mark.parametrize("path", ["/auth/password", "/web/api/check"])
@pytest.mark.parametrize(
    "framing,body,status",
    [
        ([], b"", 400),
        ([("Content-Length", "2"), ("Content-Length", "2")], b"{}", 400),
        ([("Content-Length", "2"), ("Transfer-Encoding", "chunked")], b"{}", 400),
        ([("Content-Length", "-1")], b"", 400),
        ([("Content-Length", "two")], b"{}", 400),
        ([("Content-Length", "4097")], b"", 413),
        ([("Content-Length", "3")], b"{}", 400),
        ([("Content-Length", "1")], b"{", 400),
    ],
)
def test_browser_json_refuses_ambiguous_oversized_or_incomplete_bodies(server, path, framing, body, status):
    connection = http.client.HTTPConnection("127.0.0.1", int(server), timeout=2)
    try:
        connection.putrequest("POST", path, skip_host=True)
        for name, value in [
            ("Host", "books.example"),
            ("Origin", server.origin),
            ("X-Forwarded-Proto", "https"),
            ("Cookie", "__Host-session=" + "a" * 64),
            ("Content-Type", "application/json"),
            *framing,
        ]:
            connection.putheader(name, value)
        connection.endheaders(body)
        # Only the incomplete body needs EOF. Invalid framing may already have closed the connection.
        if framing == [("Content-Length", "3")]:
            connection.sock.shutdown(1)
        response = connection.getresponse()
        assert response.status == status
        response.read()
    finally:
        connection.close()
