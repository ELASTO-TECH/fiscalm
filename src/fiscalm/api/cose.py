"""WebAuthn public COSE keys; cryptography owns all curve and signature operations."""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from fiscalm.api import cbor

PublicKey = ec.EllipticCurvePublicKey | ed25519.Ed25519PublicKey | rsa.RSAPublicKey


class CoseError(ValueError):
    """An unsupported or malformed credential public key."""


@dataclass(frozen=True)
class Key:
    algorithm: int
    encoded: bytes
    _public: PublicKey = field(repr=False)

    def verify(self, signature: bytes, data: bytes) -> bool:
        if not isinstance(signature, bytes) or not isinstance(data, bytes):
            return False
        if len(signature) > 1024 or len(data) > cbor.MAX_BYTES + 32:
            return False
        try:
            if isinstance(self._public, ec.EllipticCurvePublicKey):
                self._public.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            elif isinstance(self._public, ed25519.Ed25519PublicKey):
                self._public.verify(signature, data)
            else:
                self._public.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError, UnsupportedAlgorithm):
            return False
        return True


def _constant(values: dict, label: int, expected: int) -> None:
    if type(values[label]) is not int or values[label] != expected:
        raise CoseError("Invalid COSE parameter")


def _binary(values: dict, label: int, minimum: int, maximum: int) -> bytes:
    value = values[label]
    if not isinstance(value, bytes) or not minimum <= len(value) <= maximum:
        raise CoseError("Invalid COSE key size")
    return value


def _es256(values: dict) -> PublicKey:
    _constant(values, 1, 2)
    _constant(values, -1, 1)
    x, y = (int.from_bytes(_binary(values, label, 32, 32), "big") for label in (-2, -3))
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def _eddsa(values: dict) -> PublicKey:
    _constant(values, 1, 1)
    _constant(values, -1, 6)
    raw = _binary(values, -2, 32, 32)
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    # OpenSSL accepts small-order keys, including the trivially forgeable identity.
    # The two order-8 y coordinates come from libsodium 1.0.18
    # ge25519_has_small_order; attribution and ISC licence are in cose.LICENSE.
    small_order = (
        0,
        1,
        (1 << 255) - 20,
        2707385501144840649318225287225658788936804267575313519463743609750303402022,
        55188659117513257062467267217118295137698188065244968500265048394206261417927,
    )
    if y >= (1 << 255) - 19 or y in small_order:
        raise CoseError("Invalid Ed25519 public key")
    return ed25519.Ed25519PublicKey.from_public_bytes(raw)


def _rs256(values: dict) -> PublicKey:
    _constant(values, 1, 3)
    n, e = _binary(values, -1, 256, 1024), _binary(values, -2, 1, 8)
    if n[0] == 0 or e[0] == 0:
        raise CoseError("Nonminimal RSA integer")
    modulus, exponent = int.from_bytes(n, "big"), int.from_bytes(e, "big")
    if not 2048 <= modulus.bit_length() <= 8192:
        raise CoseError("Invalid RSA strength")
    return rsa.RSAPublicNumbers(exponent, modulus).public_key()


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    for width, additional in ((1, 24), (2, 25), (4, 26), (8, 27)):
        if value < 1 << (8 * width):
            return bytes([(major << 5) | additional]) + value.to_bytes(width, "big")
    raise CoseError("COSE integer too large")


def _scalar(value: object) -> bytes:
    if type(value) is int:
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, bytes):
        return _head(2, len(value)) + value
    raise CoseError("Unsupported COSE value")


def _canonical(values: dict) -> bytes:
    """Only the flat public-key map, not a general-purpose CBOR encoder."""
    pairs = sorted(((_scalar(k), _scalar(v)) for k, v in values.items()), key=lambda pair: (len(pair[0]), pair[0]))
    return _head(5, len(pairs)) + b"".join(k + v for k, v in pairs)


def parse(data: bytes) -> Key:
    """Accept precisely the required public parameters and CTAP2 canonical encoding."""
    try:
        values = cbor.decode(data)
        if not isinstance(values, dict) or type(values.get(3)) is not int:
            raise CoseError("Missing COSE algorithm")
        algorithm = values[3]
        spec = {
            -7: ({1, 3, -1, -2, -3}, _es256),
            -8: ({1, 3, -1, -2}, _eddsa),
            -257: ({1, 3, -1, -2}, _rs256),
        }.get(algorithm)
        if spec is None or set(values) != spec[0] or data != _canonical(values):
            raise CoseError("Unsupported COSE public key")
        return Key(algorithm, data, spec[1](values))
    except (ValueError, TypeError, OverflowError, UnsupportedAlgorithm):
        raise CoseError("Invalid credential public key") from None
