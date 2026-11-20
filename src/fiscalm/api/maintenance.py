"""Local authentication retention; file cleanup sees committed factor and ticket references."""

from __future__ import annotations

from datetime import datetime, timedelta

from fiscalm.api import sessions, totp_store
from fiscalm.ledger import Ledger


def sweep(L: Ledger, at: datetime) -> None:
    totp_store.sweep(L, at)  # Refuses an outer transaction; never delete a seed before activation commits.
    L.purge_auth_state(sessions.stamp(at), sessions.stamp(at - timedelta(days=90)))
