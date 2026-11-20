"""Request-local API actor attribution; credentials themselves never enter audit text."""

from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar

actor: ContextVar[str] = ContextVar("api_actor", default=str(os.environ.pop("FISCALM_ACTION_ACTOR", "")))


def identity(credential: dict) -> str:
    name = str(credential.get("name", ""))
    name = re.sub(r"fiscalm_[0-9a-f]{16}_[0-9a-f]{64}|[A-Z]{2}\s*[0-9]{2}(?:\s*[A-Z0-9]){11,30}", "[redacted]", name)
    return "token " + json.dumps({"id": credential.get("id", ""), "name": name}, ensure_ascii=True)


def note(text: str) -> str:
    suffix = " [" + actor.get() + "]" if actor.get() else ""
    return text if not suffix or text.endswith(suffix) else text + suffix
