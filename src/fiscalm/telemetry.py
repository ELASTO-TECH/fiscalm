"""Daily opt-out install counts. The payload contains no company identifiers."""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from fiscalm import __version__, companies
from fiscalm.ledger import Ledger, is_live
from fiscalm.updates import LIMIT, NoRedirect

ENDPOINT = "https://fiscalm.ro/api/v1/ping"


def state(L: Ledger) -> dict[str, bool]:
    forced_off = os.environ.get("FISCALM_TELEMETRY") == "0"
    return {"enabled": not forced_off and L.get_meta("telemetry:enabled") != "0", "forced_off": forced_off}


def set_enabled(L: Ledger, on: bool) -> None:
    L.set_meta("telemetry:enabled", "1" if on else "0")


def ping(L: Ledger, today: date) -> None:
    """Claim attempts before I/O; concurrent ticks share one identity and daily slot."""
    try:
        with L.tx():
            if not is_live() or not state(L)["enabled"] or L.get_meta("telemetry:checked") == today.isoformat():
                return
            install = L.get_meta("telemetry:install") or str(uuid4())
            L.set_meta("telemetry:install", install)
            L.set_meta("telemetry:checked", today.isoformat())
            payload = {
                "install": install,
                "version": __version__,
                "firms": len(companies.all(L)),
                "schema": L.db.execute("PRAGMA user_version").fetchone()[0],
            }
        request = Request(
            ENDPOINT,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "fiscalm"},
            method="POST",
        )
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=4) as response:
            if not 200 <= response.status < 300 or len(response.read(LIMIT + 1)) > LIMIT:
                raise ValueError("Invalid telemetry response")
    except Exception:  # Statistics must never stop accounting work, including local storage failures.
        logging.getLogger(__name__).warning("Anonymous statistics unavailable")
