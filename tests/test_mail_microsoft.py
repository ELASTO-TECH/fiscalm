"""Synthetic Microsoft authorization and Graph transport; no network."""

import json
from datetime import date

import pytest
from test_api import _api, server as server  # noqa: F401

from fiscalm import integrations as ig
from fiscalm.mail import graph, imap, microsoft as ms, reader


@pytest.fixture
def microsoft(ledger, sec, monkeypatch):
    monkeypatch.setenv("FISCALM_MICROSOFT_CLIENT_ID", "invented-public-client")
    clock = [1000]
    monkeypatch.setattr(ms.time, "time", lambda: clock[0])
    calls = []
    replies = [dict(device_code="private-device", user_code="CODE", expires_in=900, interval=5)]

    def request(url, **kwargs):
        calls.append((url, kwargs))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply).encode()

    monkeypatch.setattr(ms, "request", request)
    return clock, calls, replies


def test_device_connection_refresh_and_private_files(ledger, microsoft, sec):
    clock, calls, replies = microsoft
    start = ms.start(ledger, "reader@outlook.com", "", "user-a")
    iid = start["id"]
    assert "private-device" not in json.dumps(start)
    assert ms.poll(ledger, iid, "user-a") == {"code": "authorization_pending", "interval": 5}
    with pytest.raises(ms.MicrosoftError, match="connection_changed"):
        ms.poll(ledger, iid, "user-b")
    clock[0] += 5
    replies.extend(
        [
            dict(
                access_token="private-access",
                refresh_token="private-refresh",
                expires_in=3600,
                scope="Mail.Read User.Read",
            ),
            dict(mail="reader@outlook.com"),
        ]
    )
    assert ms.poll(ledger, iid, "user-a") == {"code": "connected", "id": iid}
    assert calls[0] == (ms.AUTH + "devicecode", {"form": {"client_id": "invented-public-client", "scope": ms.SCOPES}})
    account = ig.instance(ledger, iid)
    assert ms.access_token(ledger, account) == "private-access"
    assert "private-" not in json.dumps(ig.listing(ledger))
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in sec.iterdir())
    clock[0] += 3600
    replies.append(
        dict(access_token="refreshed", refresh_token="rotated", expires_in=3600, scope="Mail.Read User.Read")
    )
    assert ms.access_token(ledger, account) == "refreshed"
    assert ms.load(ig.instance(ledger, iid))["refresh_token"] == "rotated"
    with pytest.raises(ms.MicrosoftError, match="connection_changed"):
        ms.save(ledger, account, {})


@pytest.mark.parametrize(
    "error", ["authorization_declined", "expired_token", "admin_consent_required", "tenant_policy_blocked", "slow_down"]
)
def test_poll_errors(ledger, microsoft, error):
    clock, calls, replies = microsoft
    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    clock[0] += 5
    replies.append(ms.MicrosoftError(error))
    if error == "slow_down":
        assert ms.poll(ledger, iid, "user-a") == {"code": "slow_down", "interval": 10}
    else:
        with pytest.raises(ms.MicrosoftError, match=error):
            ms.poll(ledger, iid, "user-a")


def test_wrong_account_and_scope_refused(ledger, microsoft):
    clock, calls, replies = microsoft
    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    clock[0] += 5
    replies.extend(
        [
            dict(access_token="a", refresh_token="r", expires_in=3600, scope="Mail.Read User.Read"),
            dict(mail="other@outlook.com"),
        ]
    )
    with pytest.raises(ms.MicrosoftError, match="account_mismatch"):
        ms.poll(ledger, iid, "user-a")
    assert not ms.load(ig.instance(ledger, iid)).get("refresh_token")
    with pytest.raises(ms.MicrosoftError, match="invalid_scopes"):
        ms.tokens(dict(access_token="a", refresh_token="r", expires_in=1, scope="Mail.ReadWrite User.Read"), {})


@pytest.mark.parametrize(
    "field,value", [("from_address", "other@outlook.com"), ("method", "imap"), ("client_id", "different-client")]
)
def test_identity_edits_drop_tokens(ledger, microsoft, field, value):
    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    account = ig.instance(ledger, iid)
    values = {field: value}
    if field == "method":
        values.update(host="imap.example.invalid", username="reader", password="invented")
    ig.update(ledger, iid, values)
    assert ig.OAUTH not in ig.instance(ledger, iid).versions
    with pytest.raises(ms.MicrosoftError, match="connection_changed"):
        ms.save(ledger, account, {})


def test_repeated_start_reuses_account(ledger, microsoft):
    clock, calls, replies = microsoft
    first = ms.start(ledger, "reader@outlook.com", "", "user-a")
    replies.append(dict(device_code="new-device", user_code="NEW", expires_in=900, interval=5))
    assert ms.start(ledger, "READER@outlook.com", "", "user-a")["id"] == first["id"]
    assert len(ig.listing(ledger, "mail")) == 1


@pytest.mark.parametrize(
    "status,error,numbers,expected",
    [
        (400, "invalid_grant", [], "reconnect_required"),
        (400, "invalid_grant", [65001], "admin_consent_required"),
        (400, "invalid_grant", [53003], "tenant_policy_blocked"),
        (400, "slow_down", [], "slow_down"),
        (403, {}, [], "reconnect_required"),
        (302, {}, [], "connection_failed"),
    ],
)
def test_http_error_codes(monkeypatch, status, error, numbers, expected):
    import io
    from unittest.mock import Mock
    from urllib.error import HTTPError

    opener = Mock()
    opener.open.side_effect = HTTPError(
        ms.AUTH + "token",
        status,
        "never echo",
        {},
        io.BytesIO(json.dumps({"error": error, "error_codes": numbers}).encode()),
    )
    monkeypatch.setattr(ms, "build_opener", lambda *a: opener)
    with pytest.raises(ms.MicrosoftError, match=expected):
        ms.request(ms.AUTH + "token", form={"client_id": "fake"})


def test_graph_mime_seen_cursor_and_reconnect(ledger, microsoft, monkeypatch):

    clock, calls, replies = microsoft
    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    account = ig.instance(ledger, iid)
    ms.save(ledger, account, dict(access_token="a", refresh_token="r", expires_at=5000))
    mime = b"From: supplier@example.invalid\r\nMessage-ID: <synthetic@example.invalid>\r\nSubject: Invoice\r\n\r\ntext"
    requests = []

    def request(url, **kwargs):
        requests.append(url)
        if url.endswith("/$value"):
            return mime
        if url == graph.INBOX + "?$select=id":
            return b'{"id":"folder-synthetic"}'
        return json.dumps(
            {
                "value": [{"id": "opaque/graph-id", "receivedDateTime": "2026-11-17T10:00:00Z"}],
                "@odata.deltaLink": ms.GRAPH + "me/mailFolders/folder-synthetic/messages/delta?$deltatoken=next",
            }
        ).encode()

    monkeypatch.setattr(ms, "request", request)
    handled = []
    assert reader.read_all(ledger, lambda m: handled.append(m) or "filed", today=date(2026, 11, 17))[0]["read"] == 1
    assert handled[0].subject == "Invoice"
    assert requests[2] == graph.INBOX + "/messages/opaque%2Fgraph-id/$value"
    assert reader.read_all(ledger, lambda m: pytest.fail("already handled"), today=date(2026, 11, 17))[0]["read"] == 0
    assert len(requests) == 4

    def revoked(*args, **kwargs):
        raise ms.MicrosoftError("reconnect_required")

    monkeypatch.setattr(ms, "request", revoked)
    assert not reader.read_all(ledger, lambda m: "filed", today=date(2026, 11, 17))[0]["ok"]
    assert ig.listing(ledger)[0]["reading"]["code"] == "reconnect_required"


def test_graph_bounded_pages_refuses_external_link(ledger, microsoft, monkeypatch):
    monkeypatch.setattr(graph, "PAGES", 4)

    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    account = ig.instance(ledger, iid)
    ms.save(ledger, account, dict(access_token="a", refresh_token="r", expires_at=5000))
    account = ig.instance(ledger, iid)
    urls = []

    def request(url, **kwargs):
        urls.append(url)
        if url == graph.INBOX + "?$select=id":
            return b'{"id":"folder-synthetic"}'
        return json.dumps(
            {
                "value": [],
                "@odata.nextLink": ms.GRAPH + "me/mailFolders/folder-synthetic/messages/delta?$skiptoken=next",
            }
        ).encode()

    monkeypatch.setattr(ms, "request", request)
    assert graph.read(ledger, account, lambda m: "filed", today=date(2026, 11, 17))["left"] == 1
    assert len(urls) == graph.PAGES + 1
    monkeypatch.setattr(
        ms,
        "request",
        lambda *a, **kw: json.dumps({"value": [], "@odata.nextLink": "https://evil.invalid/steal"}).encode(),
    )
    with pytest.raises(ms.MicrosoftError, match="invalid_page"):
        graph.read(ledger, account, lambda m: "filed", today=date(2026, 11, 17))


@pytest.mark.parametrize("error", ["response_too_large", "message_unavailable"])
def test_graph_bad_message_does_not_hold_later_mail(ledger, microsoft, monkeypatch, error):
    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    account = ig.instance(ledger, iid)
    ms.save(ledger, account, dict(access_token="a", refresh_token="r", expires_at=5000))
    handled = []

    def request(url, **kwargs):
        if url == graph.INBOX + "?$select=id":
            return b'{"id":"folder-synthetic"}'
        if "/bad/$value" in url:
            raise ms.MicrosoftError(error)
        if url.endswith("/$value"):
            return b"From: supplier@example.invalid\r\nMessage-ID: <later@example.invalid>\r\n\r\ninvoice"
        return json.dumps(
            {
                "value": [{"id": i, "receivedDateTime": "2026-11-17T10:00:00Z"} for i in ("bad", "later")],
                "@odata.deltaLink": ms.GRAPH + "me/mailFolders/folder-synthetic/messages/delta?$deltatoken=next",
            }
        ).encode()

    monkeypatch.setattr(ms, "request", request)
    for _ in range(imap.ATTEMPTS + 1):
        result = graph.read(
            ledger,
            ig.instance(ledger, iid),
            lambda m: handled.append(m.message_id) or "filed",
            today=date(2026, 11, 17),
        )
    assert handled == ["later@example.invalid"]
    assert result["left"] == 0


def test_graph_delta_resumes_snapshot_then_changes(ledger, microsoft, monkeypatch):
    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    ms.save(ledger, ig.instance(ledger, iid), dict(access_token="a", refresh_token="r", expires_at=5000))
    monkeypatch.setattr(graph, "PAGES", 1)
    pages = [
        dict(id="folder-synthetic"),
        {
            "value": [],
            "@odata.nextLink": ms.GRAPH + "me/mailfolders('folder-synthetic')/messages/delta?$skiptoken=snapshot",
        },
        {
            "value": [{"id": "removed", "@removed": {"reason": "deleted"}}],
            "@odata.deltaLink": ms.GRAPH + "me/mailFolders/folder-synthetic/messages/delta?$deltatoken=changes",
        },
    ]
    urls = []

    def request(url, **kwargs):
        urls.append(url)
        return json.dumps(pages.pop(0)).encode()

    monkeypatch.setattr(ms, "request", request)
    for _ in range(2):
        graph.read(ledger, ig.instance(ledger, iid), lambda m: pytest.fail("deleted"), today=date(2026, 11, 17))
    assert "$skiptoken=snapshot" in urls[-1]
    assert "$deltatoken=changes" in ledger.get_meta(f"mail_graph:{iid}:reader@outlook.com")


def test_graph_outage_does_not_consume_message_retries(ledger, microsoft, monkeypatch):
    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    account = ig.instance(ledger, iid)

    def outage(*args, **kwargs):
        raise ms.MicrosoftError("connection_failed")

    monkeypatch.setattr(ms, "request", outage)
    for _ in range(imap.ATTEMPTS + 1):
        with pytest.raises(ms.MicrosoftError, match="connection_failed"):
            graph._message(ledger, account, "temporarily-unavailable", "a", lambda m: "filed", date(2026, 11, 17))
    assert ledger.db.execute("SELECT count(*) FROM mail_seen WHERE account=?", (iid,)).fetchone()[0] == 0
    monkeypatch.setattr(ms, "request", lambda *a, **kw: b"Message-ID: <recovered@example.invalid>\r\n\r\nbody")
    assert (
        graph._message(ledger, account, "temporarily-unavailable", "a", lambda m: "filed", date(2026, 11, 17))["read"]
        == 1
    )


def test_graph_scans_old_snapshot_pages_until_delta(ledger, microsoft, monkeypatch):
    iid = ms.start(ledger, "reader@outlook.com", "", "user-a")["id"]
    ms.save(ledger, ig.instance(ledger, iid), dict(access_token="a", refresh_token="r", expires_at=5000))
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        if url == graph.INBOX + "?$select=id":
            return b'{"id":"folder-synthetic"}'
        link = "@odata.deltaLink" if len(calls) == 7 else "@odata.nextLink"
        return json.dumps(
            {
                "value": [{"id": "old", "receivedDateTime": "2020-01-01T00:00:00Z"}],
                link: ms.GRAPH + "me/mailFolders/folder-synthetic/messages/delta?$skiptoken=next",
            }
        ).encode()

    monkeypatch.setattr(ms, "request", request)
    result = graph.read(ledger, ig.instance(ledger, iid), lambda m: pytest.fail("old mail"), today=date(2026, 11, 17))
    assert result == {"read": 0, "left": 0, "errors": 0}
    assert len(calls) == 7


def test_selected_scope_exists_before_microsoft_authorization(ledger, microsoft):
    clock, calls, replies = microsoft
    started = ms.start(ledger, "reader@outlook.com", "", "user-a", firm_scope=["NORD"])
    iid = started["id"]
    assert ig.instance(ledger, iid).firm_scope == ["NORD"]
    clock[0] += 5
    replies.extend(
        [
            dict(access_token="synthetic", refresh_token="synthetic", expires_in=3600, scope="Mail.Read User.Read"),
            dict(mail="reader@outlook.com"),
        ]
    )
    assert ms.poll(ledger, iid, "user-a")["code"] == "connected"
    assert ig.instance(ledger, iid).firm_scope == ["NORD"]
    assert ig.coverage(ledger)[1]["mail"] == []
    with pytest.raises(ms.MicrosoftError, match="invalid_settings"):
        ms.start(ledger, "reader@outlook.com", iid, "user-a", firm_scope="all")
    assert len(calls) == 3  # device, token, profile; refused reconnect sends nothing


@pytest.mark.parametrize("scope", [None, "NORD", ["missing"], [1]])
def test_invalid_scope_is_refused_before_device_request(ledger, microsoft, scope):
    with pytest.raises(ig.IntegrationError):
        ms.start(ledger, "reader@outlook.com", "", "user-a", firm_scope=scope)
    assert microsoft[1] == [] and ig.listing(ledger) == []


def test_http_device_start_stores_scope_before_polling(ledger, microsoft, server):
    status, result = _api(server, "mail-microsoft-start", {"address": "reader@outlook.com", "firm_scope": ["NORD"]})
    assert status == 200 and result["ok"]
    iid = result["done"]["id"]
    assert ig.instance(ledger, iid).firm_scope == ["NORD"]
    status, refused = _api(
        server, "mail-microsoft-start", {"address": "reader@outlook.com", "id": iid, "firm_scope": "all"}
    )
    assert status == 200 and not refused["ok"]
    assert ig.instance(ledger, iid).firm_scope == ["NORD"]
