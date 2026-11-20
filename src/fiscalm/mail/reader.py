"""One intake reader interface, dispatching each account to its read-only transport."""

from __future__ import annotations

import imaplib
import json
from collections.abc import Callable
from datetime import date

from fiscalm.ledger import Ledger, now
from fiscalm.mail import graph, imap, microsoft


def read_all(
    L: Ledger,
    handle: Callable[[imap.Message], str],
    *,
    today: date,
    connect: Callable[[str, int, float, float], imaplib.IMAP4] | None = None,
) -> list[dict]:
    """Read every account with reading on. One failing account never stops the others; each run's outcome is kept in
    `meta` (mail_read:<account>) for Setări."""
    results = []
    for account in imap.accounts(L):
        try:
            res = {"account": account.id, "ok": True} | (
                graph.read(L, account, handle, today=today)
                if account.config.get("method") == "microsoft"
                else imap.read(L, account, handle, today=today, connect=connect)
            )
        except microsoft.MicrosoftError as e:
            res = {
                "account": account.id,
                "ok": False,
                "code": str(e),
                "error": "Reconectează contul Microsoft."
                if str(e) == "reconnect_required"
                else "Citirea Microsoft se reia la următoarea verificare.",
            }
        except imap.ReadError as e:
            res = {"account": account.id, "ok": False, "code": e.code, "error": str(e)}
        except Exception:  # one account's surprise never stops the others
            res = {
                "account": account.id,
                "ok": False,
                "error": "Citirea contului de e-mail a eșuat. Citirea se reia la următoarea verificare.",
            }
        L.set_meta(f"mail_read:{account.id}", json.dumps(res | {"at": now()}, ensure_ascii=False))
        if res["ok"]:
            L.set_meta(f"mail_read_ok:{account.id}", now())
        results.append(res)
    return results
