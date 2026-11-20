"""Browser transport refuses spoofed authorities, insecure cookies and CSRF before sign-in."""

import socket
import ssl
from http.client import HTTPMessage

import pytest

from fiscalm.api.transport import Boundary


def headers(**overrides):
    message = HTTPMessage()
    values = {
        "Host": "fiscalm.example",
        "Origin": "https://fiscalm.example",
        "Content-Type": "application/json",
        "X-Forwarded-Proto": "https",
        **overrides,
    }
    for name, value in values.items():
        for item in value if isinstance(value, list) else [value] if value is not None else []:
            message[name] = item
    return message


@pytest.mark.parametrize(
    "host",
    [
        None,
        ["fiscalm.example"] * 2,
        "other.example",
        "fiscalm.example:444",
        "fiscalm.example:0",
        "fiscalm.example:99999",
        "fiscalm.example:",
        "fiscalm.example/path",
        "x@fiscalm.example",
        "fiscalm.example?x",
        "[bad",
        "[:::]",
        "fiscalm.example,other.example",
    ],
)
def test_host_refusals(host):
    assert not Boundary("https://fiscalm.example").accepts_host(headers(Host=host))


def test_authority_is_pinned_including_proxy_and_port():
    direct, proxy = Boundary("https://fiscalm.example"), Boundary("https://fiscalm.example", True)
    assert direct.accepts_host(headers(Host="FISCALM.EXAMPLE:443", **{"X-Forwarded-Host": "evil.example"}))
    assert not direct.accepts_host(headers(Host="internal", **{"X-Forwarded-Host": "fiscalm.example"}))
    assert proxy.accepts_host(headers(Host="internal", **{"X-Forwarded-Host": "fiscalm.example"}))
    assert not proxy.accepts_host(headers(**{"X-Forwarded-Host": ["fiscalm.example"] * 2}))
    assert not proxy.accepts_host(headers(Host=None, **{"X-Forwarded-Host": "fiscalm.example"}))
    assert not proxy.accepts_host(headers(**{"X-Forwarded-Host": "evil.example"}))
    ipv6 = Boundary("https://[::1]:8790")
    assert ipv6.accepts_host(headers(Host="[::1]:8790"))
    assert not ipv6.accepts_host(headers(Host="[::1]"))


@pytest.mark.parametrize(
    "override",
    [
        {"Origin": None},
        {"Origin": ["https://fiscalm.example"] * 2},
        {"Origin": "https://other.example"},
        {"Origin": "null"},
        {"Origin": "https://[:::]"},
        {"Origin": "https://fiscalm.example/"},
        {"Origin": "https://fiscalm.example#x"},
        {"Origin": "http://fiscalm.example"},
        {"Origin": "https://fiscalm.example:444"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": ["same-origin"] * 2},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Content-Type": ["application/json"] * 2},
        {"Content-Type": None},
        {"X-Forwarded-Proto": "http"},
        {"X-Forwarded-Proto": ["https"] * 2},
        {"X-Forwarded-Proto": "https,http"},
    ],
)
def test_signin_and_cookie_mutations_require_origin_https_and_json(override):
    with socket.socket() as connection:
        boundary = Boundary("https://fiscalm.example", True)
        assert boundary.mutation(headers(), connection)
        assert not boundary.mutation(headers(**override), connection)


def test_cookie_only_over_proven_https_with_one_header():
    cookie = "__Host-session=" + "a" * 64
    values = headers(Cookie=cookie, **{"Tailscale-User-Login": "admin", "Authorization": "Bearer ignored"})
    with socket.socket() as connection:
        assert Boundary("https://fiscalm.example").cookie(values, connection) == ""
        proxy = Boundary("https://fiscalm.example", True)
        assert proxy.cookie(values, connection) == "a" * 64
        assert proxy.cookie(headers(Cookie=[cookie] * 2), connection) == ""
        assert proxy.cookie(headers(Cookie=cookie, Host="evil.example"), connection) == ""
        assert proxy.cookie(headers(Cookie=cookie, **{"X-Forwarded-Proto": None}), connection) == ""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with context.wrap_socket(socket.socket(), server_side=True, do_handshake_on_connect=False) as tls:
        assert Boundary("https://fiscalm.example").cookie(values, tls) == "a" * 64
        assert Boundary("https://fiscalm.example").mutation(
            headers(**{"Sec-Fetch-Site": "same-origin", "Content-Type": "Application/JSON; charset=utf-8"}), tls
        )


@pytest.mark.parametrize("forwarded", [None, "bad", "192.0.2.1, 192.0.2.2", ["192.0.2.1"] * 2])
def test_ambiguous_client_ip_shares_proxy_rate_budget(forwarded):
    assert (
        Boundary("https://fiscalm.example", True).client_ip(headers(**{"X-Forwarded-For": forwarded}), "192.0.2.9")
        == "192.0.2.9"
    )


def test_proxy_trust_and_configuration_are_explicit(monkeypatch):
    message = headers(**{"X-Forwarded-For": "2001:db8::1"})
    assert Boundary("https://fiscalm.example").client_ip(message, "192.0.2.9") == "192.0.2.9"
    monkeypatch.setenv("FISCALM_PUBLIC_URL", "https://fiscalm.example/")
    monkeypatch.setenv("FISCALM_TRUST_PROXY", "true")
    boundary = Boundary.configured()
    assert boundary.client_ip(message, "192.0.2.9") == "2001:db8::1"
    monkeypatch.delenv("FISCALM_PUBLIC_URL")
    with pytest.raises(ValueError):
        Boundary.configured()
    with pytest.raises(ValueError):
        Boundary("http://localhost:8790")
