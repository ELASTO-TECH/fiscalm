"""Integration health on the status page, the syncs and the reminders; all paths and credentials are synthetic."""

import json
from datetime import datetime, timedelta

import anaf_fake
import pytest
import synthetic_world as scenario

from fiscalm import integrations, secrets
from fiscalm.api import state as api, tasks
from fiscalm.efactura import client as anaf, inbox as efactura
from fiscalm.invoicing import issued
from fiscalm.ledger import Ledger

NOW = datetime(2026, 10, 27, 9, 30)
CONSUMERS = {"efactura": efactura, "issued": issued}
MAIL = {"host": "imap.example.test", "username": "u", "password": "x", "from_address": "a@example.test"}


@pytest.fixture
def isolated(ledger, home, monkeypatch):
    monkeypatch.setenv("FISCALM_SECRETS", str(home / "secrets"))
    return ledger


def _change(iid, action):
    """Another connection removes, disables or re-points the integration, as a second process would."""
    other = Ledger()
    try:
        if action == "remove":
            integrations.remove(other, iid)
        elif action == "disable":
            integrations.set_enabled(other, iid, False)
        else:
            integrations.update(other, iid, {"client_id": "another-app"})
    finally:
        other.close()


@pytest.mark.parametrize("live", [False, True])
def test_fresh_install_has_no_integration_warnings(isolated, home, live, monkeypatch):
    if live:
        (home / "state").mkdir(exist_ok=True)
        (home / "state" / "LIVE").touch()
    # A previous failed sync, or stray mirror output, cannot configure an integration.
    isolated.set_meta("efactura_last_error:NORD", "not authorized")
    isolated.set_meta("efactura_warnings:NORD", "old warning")
    monkeypatch.setattr(anaf, "token_state", lambda *a: pytest.fail("must not read absent credentials"))
    reading = integrations.add(isolated, "mail", "Reading", MAIL)
    integrations.set_enabled(isolated, reading["id"], False)  # a disabled one counts for nothing
    s = api.state(isolated, "NORD", NOW)
    assert s["live"] is live
    a, m = s["system"]["anaf"], s["system"]["mail"]
    assert a["status"] == "not_set_up" and not a["authorized"]
    assert a["error"] is None and a["warnings"] == []
    assert m == {"state": "not_set_up", "last_ok": None, "stale": False, "reconnect": False}


@pytest.mark.parametrize("healthy,sync_error", [(False, False), (True, False), (True, True)])
def test_configured_health(isolated, home, healthy, sync_error):
    anaf_fake.connect(isolated, now=NOW - timedelta(days=305 if healthy else 366))
    acc = integrations.add(isolated, "mail", "Reading", MAIL)
    run = {"ok": True} if healthy else {"ok": False, "code": "reconnect_required"}
    isolated.set_meta(f"mail_read:{acc['id']}", json.dumps(run))
    isolated.set_meta(f"mail_read_ok:{acc['id']}", NOW.isoformat())
    if sync_error:
        isolated.set_meta("issued_last_error:NORD", "synthetic sync failure")
    s = api.state(isolated, "NORD", NOW)["system"]
    assert s["anaf"]["status"] == ("error" if sync_error else "ok" if healthy else "expired")
    assert s["anaf"]["error"] == ("synthetic sync failure" if sync_error else None)
    assert s["mail"]["state"] == ("ok" if healthy else "error")
    assert s["mail"]["stale"] is s["mail"]["reconnect"] is (not healthy)
    assert s["anaf"]["reconnect"] is False  # a failed read with good credentials retries on its own


def test_not_connected_yet_is_setup_not_an_urgent_expiry(isolated):
    anaf_fake.setup(isolated)
    isolated.set_meta("efactura_last_error:NORD", "synthetic failure")
    s = api.state(isolated, "NORD", NOW)
    a = s["system"]["anaf"]
    assert a["status"] == "not_connected" and a["error"] == "synthetic failure" and not a["authorized"]
    assert not [key for key, _, _ in tasks.references(s) if key.startswith("anaf")]


def test_reconnect_needs_a_connection_and_one_gone_meanwhile_is_not_set_up(isolated, monkeypatch):
    iid = anaf_fake.connect(isolated, now=NOW)
    for key in ("efactura_revoked", "efactura_last_error", "efactura_warnings"):
        isolated.set_meta(key + ":NORD", "synthetic")
    assert api.state(isolated, "NORD", NOW)["system"]["anaf"]["reconnect"] is True
    integrations.update(isolated, iid, {"client_id": "another-app"})  # drops the tokens, leaves the old flags
    a = api.state(isolated, "NORD", NOW)["system"]["anaf"]
    assert a["status"] == "not_connected" and a["reconnect"] is False
    monkeypatch.setattr(
        anaf, "token_state", lambda *_, **kwargs: {"status": "not_set_up", "authorized": False}
    )  # disabled
    a = api.state(isolated, "NORD", NOW)["system"]["anaf"]
    assert (a["status"], a["error"], a["warnings"], a["reconnect"]) == ("not_set_up", None, [], False)


@pytest.mark.parametrize("module_name", CONSUMERS)
def test_all_anaf_consumers_use_shared_configuration_rule(isolated, monkeypatch, module_name):
    anaf_fake.setup(isolated)  # saved but not connected: counts everywhere, not just on the API page
    assert CONSUMERS[module_name].sync(isolated, NOW)["errors"]
    monkeypatch.setattr(anaf, "configured", lambda L: False)
    assert CONSUMERS[module_name].sync(isolated, NOW).get("not_authorized")


@pytest.mark.parametrize(
    "broken",
    [
        "{}",
        "{corrupt",
        json.dumps(
            {
                "access_token": "a",
                "refresh_token": "r",
                "access_expires_at": "2026-12-01",
                "refresh_expires_at": "bad-date",
            }
        ),
        "symlink",
        "unreadable",
    ],
)
def test_broken_tokens_are_an_error_not_a_crash(isolated, home, broken):
    iid = anaf_fake.connect(isolated, now=NOW)
    folder = secrets.secrets_dir()
    (path,) = folder.glob(f"int-{iid}-oauth-*")
    if broken == "symlink":
        (home / "target").write_text("{}")
        path.unlink()
        path.symlink_to(home / "target")
    elif broken == "unreadable":
        folder.chmod(0)
    else:
        path.write_text(broken)
        assert anaf.token_state(isolated, NOW)["status"] == "error"
        assert anaf.connection(anaf.instance(isolated), NOW)["state"] == "error"
    try:
        a = api.state(isolated, "NORD", NOW)["system"]["anaf"]
    finally:
        folder.chmod(0o700)
    assert a["status"] == "error" and a["error"] and not a["authorized"] and a["reconnect"]


def test_scenario_can_generate_absent_integration_case(isolated, home, monkeypatch):
    scenario.build(isolated, home, monkeypatch.setattr, NOW, integrations_configured=False)
    s = api.state(isolated, "NORD", NOW)["system"]
    assert s["anaf"]["status"] == "not_set_up"
    assert s["mail"]["state"] == "not_set_up" and s["mail"]["stale"] is False


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("action", ["remove", "disable"])
def test_deliberate_disconnect_is_quiet(isolated, monkeypatch, connected, action):
    iid = anaf_fake.connect(isolated, now=NOW) if connected else anaf_fake.setup(isolated)
    isolated.set_meta("efactura_last_error:NORD", "old failure")
    if action == "remove":
        integrations.remove(isolated, iid)
        assert not list(secrets.secrets_dir().glob(f"int-{iid}-*"))  # the tokens go with the integration
    else:
        integrations.set_enabled(isolated, iid, False)
    monkeypatch.setattr(anaf, "access_token", lambda *a, **k: pytest.fail("disconnected sync must not request tokens"))
    monkeypatch.setattr(anaf, "token_state", lambda *a: pytest.fail("disconnected consumers must not read tokens"))
    a = api.state(isolated, "NORD", NOW)["system"]["anaf"]
    assert a["status"] == "not_set_up" and a["error"] is None and not a["warnings"]
    for module in CONSUMERS.values():
        result = module.sync(isolated, NOW)
        assert result.get("not_authorized") and not result["errors"]
    if action == "disable":
        integrations.set_enabled(isolated, iid, True)
        assert anaf.configured(isolated) and (anaf_fake.tokens(isolated) is not None) is connected


def test_a_new_client_id_drops_the_tokens_and_a_new_secret_keeps_them(isolated):
    iid = anaf_fake.connect(isolated, now=NOW)
    integrations.update(isolated, iid, {"client_secret": "rotated-synthetic"})
    assert anaf_fake.tokens(isolated) is not None
    integrations.update(isolated, iid, {"client_id": "another-app"})
    assert anaf_fake.tokens(isolated) is None
    assert not list(secrets.secrets_dir().glob(f"int-{iid}-oauth-*"))


@pytest.mark.parametrize("module_name,action", [("efactura", "remove"), ("issued", "disable")])
def test_disconnect_during_token_acquisition(isolated, monkeypatch, module_name, action):
    iid = anaf_fake.setup(isolated)
    monkeypatch.setattr(anaf, "access_token", lambda *a, **k: _change(iid, action) or "synthetic")
    monkeypatch.setattr(anaf, "list_received", lambda *a: [])
    monkeypatch.setattr(anaf, "list_sent", lambda *a: [])
    CONSUMERS[module_name].sync(isolated, NOW)
    # The token was read before the change; the list calls are read-only and save nothing about the integration.
    assert not anaf.configured(isolated) and anaf_fake.tokens(isolated) is None


@pytest.mark.parametrize("action", ["remove", "disable", "new-client"])
def test_disconnect_during_refresh_does_not_restore_tokens(isolated, monkeypatch, action):
    iid = anaf_fake.connect(isolated, access="synthetic", refresh="fake", now=NOW)
    before = {p.name: p.read_text() for p in secrets.secrets_dir().glob(f"int-{iid}-oauth-*")}
    replacement = {"access_token": "replacement", "refresh_token": "replacement"}
    monkeypatch.setattr(anaf, "_post", lambda url, body, headers: _change(iid, action) or replacement)
    with pytest.raises(anaf.AuthMissing, match="removed, disabled or changed"):
        anaf.refresh(isolated, NOW)
    after = {p.name: p.read_text() for p in secrets.secrets_dir().glob(f"int-{iid}-oauth-*")}
    assert after == (before if action == "disable" else {})
    assert not any("replacement" in v for v in after.values())


def test_two_refreshes_racing_keep_one_pair(isolated, monkeypatch):
    """Two processes refresh the same tokens at once: the one that saves first wins, the other uses its tokens."""
    iid = anaf_fake.connect(isolated, access="old", refresh="r0", now=NOW - timedelta(days=80))
    answers = iter(
        [{"access_token": "second", "refresh_token": "r2"}, {"access_token": "first", "refresh_token": "r1"}]
    )

    def response(url, body, headers):
        a = next(answers)
        if a["access_token"] == "second":  # while the first caller waits for ANAF, another one refreshes and saves
            other = Ledger()
            try:
                assert anaf.access_token(NOW, L=other) == "first"
            finally:
                other.close()
        return a

    monkeypatch.setattr(anaf, "_post", response)
    assert anaf.access_token(NOW, L=isolated) == "first"
    assert anaf_fake.tokens(isolated)["refresh_token"] == "r1"
    assert len(list(secrets.secrets_dir().glob(f"int-{iid}-oauth-*"))) == 1


def test_each_inbox_says_how_its_last_read_went_without_the_error_text(isolated):
    read = integrations.add(isolated, "mail", "Read", MAIL)["id"]
    failed = integrations.add(isolated, "mail", "Failed", MAIL)["id"]
    integrations.add(isolated, "mail", "New", MAIL)
    isolated.set_meta(f"mail_read:{read}", json.dumps({"ok": True, "at": "2026-10-27T09:00:00"}))
    isolated.set_meta(f"mail_read_ok:{read}", "2026-10-27T09:00:00")
    isolated.set_meta(
        f"mail_read:{failed}", json.dumps({"ok": False, "error": "server said secret", "at": "2026-10-27T09:05:00"})
    )
    assert [i["reading"] for i in integrations.listing(isolated, "mail")] == [
        {"ok": True, "at": "2026-10-27T09:00:00", "last_ok": "2026-10-27T09:00:00"},
        {"ok": False, "at": "2026-10-27T09:05:00", "last_ok": None},
        {"ok": None, "at": None, "last_ok": None},
    ]
