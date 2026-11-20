"""Bounded CBOR subset for WebAuthn: definite values, without tags or floats."""

from __future__ import annotations

MAX_BYTES = 16_384
MAX_DEPTH = 8
MAX_COLLECTION = 128
MAX_ITEMS = 512


class CBORError(ValueError):
    """Malformed, unsupported or resource-exhausting CBOR input."""


class _Decoder:
    def __init__(self, data: bytes) -> None:
        self.data, self.offset, self.items = data, 0, 0

    def _take(self, count: int) -> bytes:
        end = self.offset + count
        if end > len(self.data):
            raise CBORError("Truncated CBOR")
        result = self.data[self.offset : end]
        self.offset = end
        return result

    def _argument(self, additional: int) -> int:
        if additional < 24:
            return additional
        width = {24: 1, 25: 2, 26: 4, 27: 8}.get(additional)
        if width is None:
            raise CBORError("Unsupported CBOR length")
        return int.from_bytes(self._take(width), "big")

    def item(self, depth: int = 0) -> object:
        self.items += 1
        if depth > MAX_DEPTH or self.items > MAX_ITEMS:
            raise CBORError("CBOR structure too large")
        head = self._take(1)[0]
        major, additional = head >> 5, head & 31
        if major == 7:
            if additional not in (20, 21, 22):
                raise CBORError("Unsupported CBOR simple value")
            return {20: False, 21: True, 22: None}[additional]
        size = self._argument(additional)
        if major in (0, 1):
            return size if major == 0 else -1 - size
        if major in (2, 3):
            value = self._take(size)
            try:
                return value if major == 2 else value.decode("utf-8")
            except UnicodeDecodeError:
                raise CBORError("Invalid CBOR text") from None
        if major not in (4, 5) or size > MAX_COLLECTION:
            raise CBORError("Unsupported CBOR collection")
        if major == 4:
            return [self.item(depth + 1) for _ in range(size)]
        result = {}
        for _ in range(size):
            key = self.item(depth + 1)
            if type(key) not in (int, bytes, str) or key in result:
                raise CBORError("Invalid or duplicate CBOR map key")
            result[key] = self.item(depth + 1)
        return result


def decode_prefix(data: bytes) -> tuple[object, int]:
    """Read one value and its byte length, for a COSE key within authenticatorData."""
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise CBORError("CBOR input too large or invalid")
    decoder = _Decoder(data)
    return decoder.item(), decoder.offset


def decode(data: bytes) -> object:
    """Decode exactly one value; trailing bytes are never silently ignored."""
    value, used = decode_prefix(data)
    if used != len(data):
        raise CBORError("Trailing CBOR data")
    return value
