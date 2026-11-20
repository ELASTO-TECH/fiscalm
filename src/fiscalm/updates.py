"""Daily, anonymous release discovery. Never download or execute an update."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from fiscalm import __version__
from fiscalm.ledger import Ledger

LATEST = "https://api.github.com/repos/ELASTO-TECH/fiscalm/releases/latest"
LIMIT = 65536
# Run in the source checkout: rebuilds the image and restarts the services on the same volumes.
UPGRADE = "docker compose up -d --build"
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _object(pairs: list[tuple]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return result


def _constant(value: str) -> None:
    raise ValueError("Invalid JSON constant")


def _release(data: bytes | str) -> dict[str, str]:
    obj = json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(obj, dict):
        raise ValueError("Invalid release")
    tag, url, published = (obj.get(k) for k in ("tag_name", "html_url", "published_at"))
    if not all(isinstance(v, str) for v in (tag, url, published)):
        raise ValueError("Invalid release fields")
    if not re.fullmatch("v?" + SEMVER, tag):
        raise ValueError("Only stable SemVer releases are shown")
    if url != "https://github.com/ELASTO-TECH/fiscalm/releases/tag/" + tag:
        raise ValueError("Invalid release page")
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", published):
        raise ValueError("Invalid publication date")
    datetime.fromisoformat(published.replace("Z", "+00:00"))
    return dict(tag_name=tag, html_url=url, published_at=published)


def _settings(L: Ledger) -> dict:
    try:
        settings = json.loads(L.get_meta("updates:state") or "null")
        if isinstance(settings, dict) and type(settings.get("enabled")) is bool:
            return {"enabled": settings["enabled"], "release": settings.get("release")}
    except (ValueError, RecursionError):
        pass
    return {"enabled": True, "release": None}


def enabled(L: Ledger) -> bool:
    return _settings(L)["enabled"]


def set_enabled(L: Ledger, on: bool) -> None:
    with L.tx():
        settings = _settings(L)
        settings["enabled"] = on
        L.set_meta("updates:state", json.dumps(settings))


def check(L: Ledger, today: date) -> None:
    """Claim the day before I/O, including failures, in live and shadow mode."""
    day = today.isoformat()
    with L.tx():
        if not enabled(L) or L.get_meta("updates:checked") == day:
            return
        L.set_meta("updates:checked", day)
    try:
        request = Request(LATEST, headers={"Accept": "application/vnd.github+json", "User-Agent": "fiscalm"})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=4) as response:
            data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            return
        release = _release(data)
    except (OSError, URLError, HTTPException, ValueError, RecursionError):
        return
    with L.tx():
        if enabled(L):
            settings = _settings(L)
            settings["release"] = release
            L.set_meta("updates:state", json.dumps(settings))


def state(L: Ledger) -> dict:
    settings = _settings(L)
    on = settings["enabled"]
    release = None
    try:
        candidate = _release(json.dumps(settings["release"]))
        current = re.fullmatch("v?" + SEMVER, __version__)
        latest = re.fullmatch("v?" + SEMVER, candidate["tag_name"])
        if on and current and latest and tuple(map(int, latest.groups()[:3])) > tuple(map(int, current.groups()[:3])):
            release = candidate
    except (ValueError, RecursionError):
        pass
    return {"enabled": on, "release": release, "command": "git pull && " + UPGRADE}
