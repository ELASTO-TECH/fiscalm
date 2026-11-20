"""The install's public address (FISCALM_PUBLIC_URL): the origin people's browsers use to reach this fiscalm.

Set once in Compose's `.env` (`FISCALM_PUBLIC_URL`). Upload
links are built on it, and ANAF sends the browser back to it after the certificate login (`anaf.redirect_uri`).
"""

from __future__ import annotations

import ipaddress
import os
import urllib.parse

ENV = "FISCALM_PUBLIC_URL"


class PublicURLError(ValueError):
    """FISCALM_PUBLIC_URL is set but is not an origin fiscalm may hand out. The message never repeats the value."""


def origin(*, https_only: bool = False) -> str | None:
    """The public origin without a trailing slash, or None when it isn't set.

    An origin only: a scheme, a host and an optional port; no user, password, path, query or fragment (one trailing
    slash is accepted). Plain http only for this machine (localhost, 127.0.0.1, ::1), and never when `https_only`."""
    value = os.environ.get(ENV)
    if value is None or value == "":
        return None
    try:
        if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError
        u = urllib.parse.urlsplit(value)
        if (
            u.scheme not in ("http", "https")
            or not u.hostname
            or not u.netloc
            or u.username is not None
            or u.password is not None
            or u.path not in ("", "/")
            or u.query
            or u.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError
        _ = u.port  # raises on a bad port
        if u.scheme == "http":
            if https_only:
                raise ValueError
            if u.hostname != "localhost" and not ipaddress.ip_address(u.hostname).is_loopback:
                raise ValueError
    except ValueError:
        raise PublicURLError(f"{ENV} must be an HTTPS origin (HTTP only on loopback)") from None
    return value.removesuffix("/")
