"""Saved account identity is read-only and prevents our own mail becoming supplier evidence."""

import imaplib
import ssl
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fiscalm import integrations
from fiscalm.api.actions import ACTIONS, Context, _mail_setup
from fiscalm.mail import imap, setup


def test_saved_addresses_remain_ours_after_disabling_account(ledger, mail_account):
    assert imap.is_own_sender("Example <ALIAS@example.invalid>", ledger)
    assert imap.is_own_sender("sender@example.invalid", ledger)
    assert not imap.is_own_sender("sender@example.invalid.attacker.test", ledger)
    integrations.set_enabled(ledger, mail_account["id"], False)
    assert imap.is_own_sender("alias@example.invalid", ledger)


def test_setup_provider_table_and_mx(monkeypatch):

    for provider, domains in setup.DOMAINS.items():
        for domain in domains.split():
            result = setup.detect("reader@" + domain)
            assert result["provider"] == setup.PROVIDERS[provider][0]
            assert result["supported"] == (provider != "proton")
    for mx, label in (
        ("aspmx.l.google.com.", "Gmail"),
        ("x.mail.protection.outlook.com.", "Microsoft"),
        ("mx.zoho.eu.", "Zoho"),
        ("mx.attacker.test.", "IMAP"),
    ):
        monkeypatch.setattr(setup.dns.resolver, "resolve", lambda *a, mx=mx, **kw: [SimpleNamespace(exchange=mx)])
        assert setup.detect("reader@firm.example")["provider"] == label
    assert setup.detect("bad address") == {"code": "invalid_settings"}


def test_setup_readonly_and_sanitized_errors(monkeypatch):

    cfg = {"from_address": "reader@gmail.com"}
    client = Mock()
    client.select.return_value = ("OK", [])
    monkeypatch.setattr(setup, "_connect", lambda *a: client)
    assert setup.test_connection(cfg, "secret")["code"] == "ok"
    client.login.assert_called_once_with("reader@gmail.com", "secret")
    client.select.assert_called_once_with('"INBOX"', readonly=True)
    client.logout.assert_called_once()
    for error, code in (
        (imaplib.IMAP4.error("secret"), "wrong_password"),
        (imaplib.IMAP4.error("Application-specific password required: secret"), "app_password_required"),
        (TimeoutError("secret"), "timeout"),
        (ssl.SSLError("secret"), "tls_error"),
        (OSError("secret"), "host_unreachable"),
    ):
        client.login.side_effect = error
        assert setup.test_connection(cfg, "secret") == {"code": code}
    client.login.side_effect = None
    client.select.return_value = ("NO", [b"secret"])
    assert setup.test_connection(cfg, "secret") == {"code": "folder_missing"}
    assert setup.test_connection(cfg | {"port": "143"}, "secret") == {"code": "invalid_settings"}
    assert setup.test_connection({"from_address": "reader@outlook.com"}, "secret") == {"code": "unsupported_provider"}


def test_setup_refuses_private_dns_before_socket(monkeypatch):

    sock = Mock(side_effect=AssertionError("must not open socket"))
    monkeypatch.setattr(setup.socket, "socket", sock)
    for address in ("127.0.0.1", "10.0.0.1", "::1", "169.254.169.254", "::ffff:127.0.0.1"):
        monkeypatch.setattr(
            setup.dns.resolver,
            "resolve_name",
            lambda *a, address=address, **kw: SimpleNamespace(addresses=lambda: [address]),
        )
        with pytest.raises(ValueError):
            setup._connect("imap.example", 993, 8, 100)
    sock.assert_not_called()


def test_setup_actions_saved_and_unsaved(ledger, mail_account, monkeypatch):

    calls = []
    monkeypatch.setattr(setup, "test_connection", lambda cfg, pwd: calls.append((cfg, pwd)) or {"code": "ok"})
    args = {"id": mail_account["id"]}
    assert _mail_setup(ledger, "", args, Context("mail-test", args, datetime(2026, 11, 17, tzinfo=UTC)))["ok"]
    assert calls[0][0]["from_address"] == "sender@example.invalid"
    assert calls[0][1]
    assert not _mail_setup(
        ledger, "", args | {"host": "other"}, Context("mail-test", args, datetime(2026, 11, 17, tzinfo=UTC))
    )["ok"]
    for action in ("mail-detect", "mail-test"):
        assert ACTIONS[action].role == "manage_integrations"
        assert not ACTIONS[action].public
        assert ACTIONS[action].allowed_in_mode()


@pytest.mark.parametrize("creation_fails", [False, True])
def test_setup_pins_public_ip_and_verifies_hostname(monkeypatch, creation_fails):
    raw, context, unreachable = Mock(), Mock(), Mock()
    unreachable.connect.side_effect = OSError("no IPv6 route")
    sockets = Mock(side_effect=[OSError("no IPv6 support") if creation_fails else unreachable, raw])
    monkeypatch.setattr(
        setup.dns.resolver,
        "resolve_name",
        lambda *a, **kw: SimpleNamespace(addresses=lambda: ["2606:4700:4700::1111", "8.8.8.8"]),
    )
    monkeypatch.setattr(setup.socket, "socket", sockets)
    monkeypatch.setattr(setup.ssl, "create_default_context", lambda: context)

    def construct(cls, deadline):
        def create(host, port, **kwargs):
            instance = object.__new__(cls)
            instance.ssl_context = kwargs["ssl_context"]
            return instance._create_socket(kwargs["timeout"])

        return create

    monkeypatch.setattr(setup.imap, "guarded", construct)
    setup._connect("imap.example", 993, 8, setup.time.monotonic() + 8)
    if not creation_fails:
        unreachable.close.assert_called_once()
    raw.connect.assert_called_once_with(("8.8.8.8", 993))
    context.wrap_socket.assert_called_once_with(raw, server_hostname="imap.example")
