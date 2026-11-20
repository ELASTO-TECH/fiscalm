"""Password hashing only; a successful check is never sufficient to create a session."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import threading
import unicodedata


class PasswordBusy(RuntimeError):
    """Map to HTTP 503 with Retry-After; never queue unlimited memory-heavy hashes."""


def normalize(password: str) -> str:
    value = unicodedata.normalize("NFKC", password)
    if not 12 <= len(value) <= 128:
        raise ValueError("Parola trebuie să aibă între 12 și 128 de caractere.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Parola conține caractere nevalide.") from None
    return value


class Passwords:
    """One instance per server, shared by its request threads (at most 256 MiB in use)."""

    def __init__(self) -> None:
        self.slots = threading.BoundedSemaphore(2)

    def _derive(self, password: str, salt: bytes, cost: int) -> bytes:
        if not self.slots.acquire(timeout=5):
            raise PasswordBusy("Autentificarea este temporar indisponibilă. Încearcă din nou.")
        try:
            return hashlib.scrypt(password.encode(), salt=salt, n=2**cost, r=8, p=1, dklen=32, maxmem=256 * 1024**2)
        finally:
            self.slots.release()

    def hash(self, password: str) -> str:
        password = normalize(password)
        salt = secrets.token_bytes(16)
        key = self._derive(password, salt, 17)
        return "scrypt$17$8$1$" + "$".join(base64.b64encode(v).decode() for v in (salt, key))

    def verify(self, password: str, encoded: str | None) -> tuple[bool, bool]:
        """Return (matches, needs_upgrade); unknown users still run the current scrypt cost.

        Only our bounded parameter family is accepted. Malformed/absent hashes use a
        fixed dummy salt and digest; no ledger value can request unbounded work.
        """
        valid = True
        try:
            password = normalize(password)
        except ValueError:
            password, valid = "invalid-password", False
        parsed = _parse(encoded)
        salt, expected, cost = parsed or (b"\x00" * 16, b"\x00" * 32, 17)
        actual = self._derive(password, salt, cost)
        matches = hmac.compare_digest(actual, expected) and parsed is not None and valid
        return matches, matches and cost < 17


def _parse(encoded: str | None) -> tuple[bytes, bytes, int] | None:
    try:
        algorithm, cost, r, p, salt, key = (encoded or "").split("$")
        if algorithm != "scrypt" or cost not in ("14", "15", "16", "17") or (r, p) != ("8", "1"):
            return None
        raw_salt, raw_key = (base64.b64decode(v, validate=True) for v in (salt, key))
        if len(raw_salt) == 16 and len(raw_key) == 32:
            return raw_salt, raw_key, int(cost)
    except (ValueError, binascii.Error):
        pass
    return None
