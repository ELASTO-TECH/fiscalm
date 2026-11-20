"""RFC 6238, six digits, SHA-1; callers must atomically consume the accepted step."""

from __future__ import annotations

import hashlib
import hmac
import re
import struct


def code(seed: bytes, step: int) -> str:
    digest = hmac.new(seed, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def match(seed: bytes, supplied: str, timestamp: int) -> int | None:
    """Check all windows, without returning early on a matching secret-derived value."""
    if not re.fullmatch(r"[0-9]{6}", supplied):
        return None
    accepted = None
    for step in range(max(0, timestamp // 30 - 1), timestamp // 30 + 2):
        if hmac.compare_digest(code(seed, step), supplied):
            accepted = step
    return accepted
