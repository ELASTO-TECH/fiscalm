"""Read-only Inbox delta/MIME transport feeding the existing parser and intake callback."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import date, timedelta
from urllib.parse import quote, unquote, urlencode, urlsplit

from fiscalm import integrations as ig
from fiscalm.ledger import Ledger
from fiscalm.mail import imap, microsoft as ms

INBOX = ms.GRAPH + "me/mailFolders/inbox"
PAGES = 2000  # Metadata pages are cheap; the wall budget and MIME count also bound every run.


def _page_url(url: str, folder: str) -> str:
    parsed = urlsplit(url)
    path = re.fullmatch(r"/v1.0/me/mail[Ff]olders(?:/([^/]+)|\('([^']+)'\))/messages/delta", unquote(parsed.path))
    if (
        parsed.scheme != "https"
        or parsed.netloc != "graph.microsoft.com"
        or parsed.fragment
        or not path
        or (path[1] or path[2]) != folder
    ):
        raise ms.MicrosoftError("invalid_page")
    return url


def _message(
    L: Ledger, account: ig.Instance, uid: str, token: str, handle: Callable[[imap.Message], str], today: date
) -> dict:
    row = L.db.execute(
        "SELECT result,attempts FROM mail_seen WHERE account=? AND folder=? AND uidvalidity=0 AND uid=?",
        (account.id, "INBOX", uid),
    ).fetchone()
    if row and (row["result"] != "error" or row["attempts"] >= imap.ATTEMPTS):
        return {"read": 0, "errors": 0}
    try:
        raw = ms.request(INBOX + "/messages/" + quote(uid, safe="") + "/$value", token=token, limit=imap.MESSAGE_MAX)
        msg = imap.parse(account.id, raw, today)
        result = "same-message" if imap.seen(L, account.id, msg.message_id) else handle(msg)
        imap._record(L, account.id, "INBOX", 0, uid, msg.message_id, result, msg)
        return {"read": int(result != "same-message"), "errors": 0}
    except ms.MicrosoftError as exc:
        if str(exc) in ("reconnect_required", "connection_changed", "connection_failed"):
            raise
        if str(exc) == "response_too_large":
            imap._record(L, account.id, "INBOX", 0, uid, f"graph:{uid}", "too-large")
            return {"read": 0, "errors": 0}
    except Exception:
        pass  # MIME/handler failures use the same retry limit as transport failures.
    imap._failed(L, account.id, "INBOX", 0, uid)
    return {"read": 0, "errors": 1}


def _cursor(L: Ledger, account: ig.Instance, token: str, today: date) -> tuple[str, dict]:
    key = f"mail_graph:{account.id}:{account.config['from_address'].lower()}"
    state = json.loads(L.get_meta(key) or "{}")
    if not state:
        folder = json.loads(ms.request(INBOX + "?$select=id", token=token))["id"]
        url = ms.GRAPH + "me/mailFolders/" + quote(folder, safe="") + "/messages/delta?"
        # No server filter: Graph caps filtered delta rounds at 5,000 messages.
        state = {
            "cutoff": (today - timedelta(days=imap.WINDOW_DAYS)).isoformat(),
            "folder": folder,
            "url": url
            + urlencode({"$select": "id,receivedDateTime", "$orderby": "receivedDateTime desc", "$top": "50"}),
        }
        L.set_meta(key, json.dumps(state))
    return key, state


def read(L: Ledger, account: ig.Instance, handle: Callable[[imap.Message], str], *, today: date) -> dict:
    token = ms.access_token(L, account)
    deadline = time.monotonic() + imap.BUDGET
    key, state = _cursor(L, account, token, today)
    out = {"read": 0, "left": 0, "errors": 0}
    cutoff = state["cutoff"]
    processed = 0
    for _ in range(PAGES):
        if time.monotonic() >= deadline:
            break
        try:
            page = json.loads(ms.request(_page_url(state["url"], state["folder"]), token=token))
        except ms.MicrosoftError as exc:
            if str(exc) == "delta_reset":
                L.set_meta(key, "{}")
                raise ms.MicrosoftError("connection_failed") from None
            raise
        if len(page["value"]) > 50:
            raise ms.MicrosoftError("response_too_large")
        for item in page["value"]:
            if time.monotonic() >= deadline or processed >= imap.MAX_MESSAGES:
                return out | {"left": 1}  # Replay page; remembered messages are skipped.
            if "@removed" in item or item.get("receivedDateTime", "")[:10] < cutoff:
                continue
            processed += 1
            result = _message(L, account, item["id"], token, handle, today)
            for field in ("read", "errors"):
                out[field] += result[field]
        if out["errors"]:
            return out | {"left": 1}  # Retry failures before acknowledging the delta page.
        url = page.get("@odata.nextLink") or page.get("@odata.deltaLink")
        if not url:
            raise ms.MicrosoftError("invalid_page")
        state["url"] = _page_url(url, state["folder"])
        L.set_meta(key, json.dumps(state))
        if "@odata.deltaLink" in page:
            return out
    return out | {"left": 1}
