"""Anonymous update discovery and local administrator preference."""

from __future__ import annotations

import io
import json
from datetime import date
from http.client import IncompleteRead
from unittest.mock import Mock
from uuid import UUID

import pytest
from conftest import go_live

from fiscalm import __version__, cli, companies, telemetry, updates
from fiscalm.api import actions, auth
from fiscalm.ledger import is_live

TODAY = date(2026, 11, 4)
RELEASE = {
    "tag_name": "v0.2.0",
    "html_url": "https://github.com/ELASTO-TECH/fiscalm/releases/tag/v0.2.0",
    "published_at": "2026-11-04T10:00:00Z",
}


@pytest.mark.parametrize(
    "tag,available", [("v0.2.0", True), ("v0.1.0", False), ("v0.0.9", False), ("v0.2.0-rc.1", False)]
)
def test_daily_release_check_in_shadow(ledger, monkeypatch, tag, available):
    monkeypatch.setattr(updates, "__version__", "0.1.0")
    data = {**RELEASE, "tag_name": tag, "html_url": RELEASE["html_url"].replace("v0.2.0", tag), "body": "discarded"}
    opener = Mock()
    opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(updates, "build_opener", lambda *handlers: opener)
    assert not is_live()
    updates.check(ledger, TODAY)
    updates.check(ledger, TODAY)
    assert opener.open.call_count == 1
    request = opener.open.call_args.args[0]
    assert request.full_url == updates.LATEST and request.data is None
    assert dict(request.header_items()) == {"Accept": "application/vnd.github+json", "User-agent": "fiscalm"}
    assert opener.open.call_args.kwargs == {"timeout": 4}
    assert bool(updates.state(ledger)["release"]) is available
    if available:
        assert json.loads(ledger.get_meta("updates:state"))["release"] == RELEASE
    updates.check(ledger, date(2026, 11, 5))
    assert opener.open.call_count == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"[]",
        b'{"tag_name":NaN}',
        b'{"tag_name":1,"tag_name":2}',
        b" " * (updates.LIMIT + 1),
        json.dumps({**RELEASE, "html_url": "https://example.invalid/"}).encode(),
        json.dumps({**RELEASE, "published_at": "invalid"}).encode(),
        TimeoutError(),
        IncompleteRead(b"synthetic"),
    ],
)
def test_invalid_or_unavailable_response_is_quiet(ledger, monkeypatch, payload):
    opener = Mock()
    if isinstance(payload, Exception):
        opener.open.side_effect = payload
    else:
        opener.open.return_value = io.BytesIO(payload)
    monkeypatch.setattr(updates, "build_opener", lambda *handlers: opener)
    updates.check(ledger, TODAY)
    assert updates.state(ledger)["release"] is None
    assert updates.state(ledger)["release"] is None


def test_admin_switch_persists_and_stops_reads(ledger, monkeypatch):
    opener = Mock()
    monkeypatch.setattr(updates, "build_opener", opener)
    args = {"on": "0"}
    assert not actions.act("update-checks", "NORD", args, require=lambda role: auth.require({"role": "member"}, role))[
        "ok"
    ]
    assert updates.enabled(ledger)
    assert actions.act("update-checks", "NORD", args, require=lambda role: auth.require({"role": "admin"}, role))["ok"]
    updates.check(ledger, TODAY)
    assert not updates.state(ledger)["enabled"]
    opener.assert_not_called()
    assert not actions.act(
        "update-checks", "NORD", {"on": "bad"}, require=lambda role: auth.require({"role": "admin"}, role)
    )["ok"]


def test_upgrade_command(ledger):
    assert updates.state(ledger)["command"] == "git pull && docker compose up -d --build"


def test_tick_command_checks_even_without_a_company(ledger, monkeypatch):
    monkeypatch.setattr(cli.companies, "all", lambda *args: {})
    check = Mock()
    monkeypatch.setattr(updates, "check", check)
    assert cli.main(["update-check"]) == 0
    assert check.call_count == 1


@pytest.mark.parametrize("stored", ["{", "[]", '{"enabled":true}'])
def test_unusable_notice_cache_does_not_break_state(ledger, stored):
    ledger.set_meta("updates:state", stored)
    assert updates.state(ledger)["release"] is None
    assert updates.state(ledger)["enabled"] is True


@pytest.mark.parametrize("gate", ["sandbox", "switch", "env"])
def test_telemetry_refusals(ledger, home, monkeypatch, gate):

    if gate != "sandbox":
        go_live(home)
    if gate == "switch":
        telemetry.set_enabled(ledger, False)
    if gate == "env":
        monkeypatch.setenv("FISCALM_TELEMETRY", "0")
    opener = Mock()
    monkeypatch.setattr(telemetry, "build_opener", opener)
    telemetry.ping(ledger, TODAY)
    opener.assert_not_called()
    assert ledger.get_meta("telemetry:install") is None


def test_telemetry_payload_daily_identity_and_failure(ledger, live, monkeypatch, caplog):

    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status, response.read.return_value = 204, b""
    opener = Mock()
    opener.open.return_value = response

    def hardened_opener(proxy, redirect):
        assert proxy.proxies == {} and isinstance(redirect, updates.NoRedirect)
        return opener

    monkeypatch.setattr(telemetry, "build_opener", hardened_opener)
    telemetry.ping(ledger, TODAY)
    telemetry.ping(ledger, TODAY)
    assert opener.open.call_count == 1
    request = opener.open.call_args.args[0]
    payload = json.loads(request.data)
    assert UUID(payload["install"]).version == 4
    assert payload == {
        "install": ledger.get_meta("telemetry:install"),
        "version": __version__,
        "firms": len(companies.all(ledger)),
        "schema": ledger.db.execute("PRAGMA user_version").fetchone()[0],
    }
    assert request.full_url == telemetry.ENDPOINT and request.method == "POST"
    assert opener.open.call_args.kwargs == {"timeout": 4}
    opener.open.side_effect = TimeoutError("private server details")
    updates.set_enabled(ledger, False)
    assert cli.main(["update-check"]) == 0
    assert json.loads(opener.open.call_args.args[0].data)["install"] == payload["install"]
    assert "Anonymous statistics unavailable" in caplog.text
    assert "private server details" not in caplog.text


def test_telemetry_admin_preference(ledger):

    for role, ok in (("member", False), ("admin", True)):
        result = actions.act(
            "telemetry-settings",
            "NORD",
            {"on": "0"},
            require=lambda permission, role=role: auth.require({"role": role}, permission),
        )
        assert result["ok"] is ok
    assert not telemetry.state(ledger)["enabled"]
    assert not actions.act(
        "telemetry-settings",
        "NORD",
        {"on": "bad"},
        require=lambda permission: auth.require({"role": "admin"}, permission),
    )["ok"]


@pytest.mark.parametrize("status,body", [(302, b""), (500, b""), (200, b"x" * (updates.LIMIT + 1))])
def test_telemetry_bad_responses_skip_the_day(ledger, live, monkeypatch, status, body):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status, response.read.return_value = status, body
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(telemetry, "build_opener", lambda *handlers: opener)
    telemetry.ping(ledger, TODAY)
    telemetry.ping(ledger, TODAY)
    assert opener.open.call_count == 1
    if status == 200:
        response.read.assert_called_once_with(updates.LIMIT + 1)
