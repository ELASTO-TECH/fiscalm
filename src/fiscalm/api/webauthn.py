"""Bounded WebAuthn ceremony parsing; ticket, owner and counter writes live in the flow."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from fiscalm.api import cbor, cose
from fiscalm.api.transport import Boundary


class WebAuthnError(ValueError):
    """A malformed or unverified WebAuthn response."""


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(value: str, maximum: int) -> bytes:
    if not isinstance(value, str) or not 0 < len(value) <= (maximum * 4 + 2) // 3:
        raise WebAuthnError("Invalid base64url length")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value, re.ASCII):
        raise WebAuthnError("Invalid base64url alphabet")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise WebAuthnError("Invalid base64url") from None
    if len(raw) > maximum or b64(raw) != value:
        raise WebAuthnError("Noncanonical base64url")
    return raw


def _pairs(pairs: list[tuple]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise WebAuthnError("Duplicate client data key")
    return result


def _constant(value: str) -> None:
    raise WebAuthnError("Invalid client data constant")


def _client(raw: bytes, challenge: bytes, origin: str, kind: str) -> bytes:
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 4096:
            raise WebAuthnError("Invalid client data size")
        client = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        if (
            not isinstance(client, dict)
            or client.get("type") != kind
            or client.get("challenge") != b64(challenge)
            or client.get("origin") != origin
            or client.get("crossOrigin", False) is not False
            or "topOrigin" in client
        ):
            raise WebAuthnError("Client data binding refused")
    except (ValueError, TypeError, RecursionError):
        raise WebAuthnError("Invalid client data") from None
    return hashlib.sha256(raw).digest()


@dataclass(frozen=True)
class Authenticator:
    raw: bytes
    counter: int
    backup_eligible: bool
    backed_up: bool
    credential_id: bytes = b""
    public_key: cose.Key | None = None
    aaguid: bytes = b""


def _authenticator(raw: bytes, rp_id: str, registration: bool) -> Authenticator:
    if not isinstance(raw, bytes) or not 37 <= len(raw) <= cbor.MAX_BYTES:
        raise WebAuthnError("Invalid authenticator data size")
    flags, counter = raw[32], int.from_bytes(raw[33:37], "big")
    if raw[:32] != hashlib.sha256(rp_id.encode("ascii")).digest() or flags & 5 != 5:
        raise WebAuthnError("Relying party or user verification refused")
    if bool(flags & 64) != registration or (flags & 16 and not flags & 8):
        raise WebAuthnError("Invalid authenticator flags")
    offset, ident, key, aaguid = 37, b"", None, b""
    if registration:
        if len(raw) < 55:
            raise WebAuthnError("Missing attested credential")
        size = int.from_bytes(raw[53:55], "big")
        if not 1 <= size <= 1023 or len(raw) < 55 + size:
            raise WebAuthnError("Invalid credential id size")
        aaguid, ident, offset = raw[37:53], raw[55 : 55 + size], 55 + size
        _, used = cbor.decode_prefix(raw[offset:])
        key = cose.parse(raw[offset : offset + used])
        offset += used
    if flags & 128:
        extensions = cbor.decode(raw[offset:])
        if not isinstance(extensions, dict) or any(not isinstance(k, str) for k in extensions):
            raise WebAuthnError("Invalid signed extensions")
    elif offset != len(raw):
        raise WebAuthnError("Trailing authenticator data")
    return Authenticator(raw, counter, bool(flags & 8), bool(flags & 16), ident, key, aaguid)


@dataclass(frozen=True)
class Verifier:
    origin: str

    def __post_init__(self) -> None:
        Boundary(self.origin)  # Reuse the browser's pinned HTTPS-origin syntax.
        parsed = urlsplit(self.origin)
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            port = f":{parsed.port}" if parsed.port not in (None, 443) else ""
            object.__setattr__(self, "origin", f"https://{parsed.hostname}{port}")
            return
        raise ValueError("Passkeys require a DNS host, not an IP address")

    @property
    def rp_id(self) -> str:
        return urlsplit(self.origin).hostname or ""

    def registration(
        self, attestation: bytes, client_data: bytes, challenge: bytes, credential_id: bytes
    ) -> Authenticator:
        try:
            _client(client_data, challenge, self.origin, "webauthn.create")
            obj = cbor.decode(attestation)
            if not isinstance(obj, dict) or set(obj) != {"fmt", "attStmt", "authData"}:
                raise WebAuthnError("Invalid attestation object")
            if obj["fmt"] != "none" or obj["attStmt"] != {}:
                raise WebAuthnError("Unsupported attestation")
            result = _authenticator(obj["authData"], self.rp_id, True)
            if result.credential_id != credential_id:
                raise WebAuthnError("Credential id mismatch")
            return result
        except (ValueError, TypeError):
            raise WebAuthnError("Registration refused") from None

    def assertion(
        self, raw: bytes, client_data: bytes, challenge: bytes, signature: bytes, public_key: bytes
    ) -> Authenticator:
        try:
            client_hash = _client(client_data, challenge, self.origin, "webauthn.get")
            result = _authenticator(raw, self.rp_id, False)
            if not cose.parse(public_key).verify(signature, raw + client_hash):
                raise WebAuthnError("Invalid credential signature")
            return result
        except (ValueError, TypeError):
            raise WebAuthnError("Assertion refused") from None
