"""Anonymous update discovery and local administrator preference."""

from __future__ import annotations

import io
import json
from datetime import date
from http.client import IncompleteRead
from unittest.mock import Mock

import pytest

from fiscalm import cli, updates
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
