"""Pinned browser origin and HTTPS proof, independent of credentials and route dispatch."""

from __future__ import annotations

import ipaddress
import os
import re
import ssl
import urllib.parse
from dataclasses import dataclass
from http.client import HTTPMessage
from socket import socket

from fiscalm.api import public_url, sessions


def _one(headers: HTTPMessage, name: str) -> str | None:
    values = headers.get_all(name, [])
    return values[0] if len(values) == 1 else None


def _authority(value: str) -> tuple[str, int] | None:
    if not re.fullmatch(r"(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]{1,5})?", value):
        return None
    try:
        parsed = urllib.parse.urlsplit("https://" + value)
        port = parsed.port if parsed.port is not None else 443
        return (parsed.hostname.lower(), port) if parsed.hostname and 0 < port <= 65535 else None
    except ValueError:
        return None


def _origin(value: str) -> tuple[str, int] | None:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or value != "https://" + parsed.netloc:
        return None
    return _authority(parsed.netloc)


@dataclass(frozen=True)
class Boundary:
    origin: str
    trust_proxy: bool = False

    def __post_init__(self) -> None:
        if _origin(self.origin) is None:
            raise ValueError("Browser authentication requires a configured HTTPS origin")

    @classmethod
    def configured(cls) -> Boundary:
        return cls(
            public_url.origin(https_only=True) or "",
            os.environ.get("FISCALM_TRUST_PROXY", "").lower() in ("1", "true", "yes"),
        )

    def accepts_host(self, headers: HTTPMessage) -> bool:
        host = _one(headers, "Host")
        if host is None:
            return False
        if self.trust_proxy and headers.get_all("X-Forwarded-Host") is not None:
            host = _one(headers, "X-Forwarded-Host")
        return host is not None and _authority(host) == _origin(self.origin)

    def secure(self, headers: HTTPMessage, connection: socket) -> bool:
        return isinstance(connection, ssl.SSLSocket) or (
            self.trust_proxy and _one(headers, "X-Forwarded-Proto") == "https"
        )

    def mutation(
        self, headers: HTTPMessage, connection: socket, *, media_types: tuple[str, ...] = ("application/json",)
    ) -> bool:
        content_type = _one(headers, "Content-Type")
        return (
            self.accepts_host(headers)
            and self.secure(headers, connection)
            and self.accepts_origin(headers)
            and content_type is not None
            and content_type.split(";", 1)[0].strip().lower() in media_types
        )

    def accepts_origin(self, headers: HTTPMessage) -> bool:
        origin = _one(headers, "Origin")
        return (
            origin is not None
            and _origin(origin) == _origin(self.origin)
            and headers.get_all("Sec-Fetch-Site") in (None, ["same-origin"])
        )

    def cookie(self, headers: HTTPMessage, connection: socket) -> str:
        if not self.accepts_host(headers):
            return ""
        return sessions.from_cookie(_one(headers, "Cookie") or "", secure=self.secure(headers, connection))

    def client_ip(self, headers: HTTPMessage, peer: str) -> str:
        forwarded = _one(headers, "X-Forwarded-For") if self.trust_proxy else None
        try:
            return str(ipaddress.ip_address(forwarded)) if forwarded else peer
        except ValueError:
            return peer  # Ambiguous proxy chains share the peer's rate budget; never trust a chosen element.
