"""Ten one-time 80-bit recovery codes, returned only when replacing the set."""

from __future__ import annotations

import base64
import re
import secrets
from datetime import datetime

from fiscalm.api import sessions
from fiscalm.ledger import Ledger

CODE = re.compile(r"[A-Z2-7]{16}", re.ASCII)


def generate(L: Ledger, user_id: str, at: datetime) -> list[str]:
    """Caller must prove setup completion or recent authentication in its outer transaction."""
    raw = set()
    while len(raw) < 10:
        raw.add(base64.b32encode(secrets.token_bytes(10)).decode())
    codes = sorted(raw)
    L.replace_recovery_codes(user_id, [sessions.digest(v) for v in codes], sessions.stamp(at))
    return ["-".join(value[i : i + 4] for i in range(0, 16, 4)) for value in codes]


def consume(L: Ledger, user_id: str, supplied: str, at: datetime) -> bool:
    """Not a sign-in by itself: the flow must also prove the password for password users."""
    code = supplied.replace("-", "").replace(" ", "").upper()
    if not CODE.fullmatch(code):
        return False
    return L.consume_recovery_code(user_id, sessions.digest(code), sessions.stamp(at))
