"""The faster date lookup preserves every candidate and the original tie order."""

from __future__ import annotations

import random
import re
from datetime import date, timedelta
from decimal import Decimal

from fiscalm import recon
from fiscalm.bank.statements import Tx


def reference(txs, receipts):
    matches, used = {}, set()
    for confidence in ("exact", "vendor"):
        for i, t in enumerate(txs):
            if i in matches:
                continue
            for r in receipts:
                if r["sha"] in used:
                    continue
                near = abs((t.date - date.fromisoformat(r["doc_date"])).days) <= recon.MATCH_DAYS
                word = re.split(r"[^a-z0-9]+", (r["vendor"] or "").lower())[0]
                fits = r["currency"] == (t.orig_currency or t.currency) and Decimal(r["amount"]) == (
                    t.orig_amount if t.orig_amount is not None else t.amount
                )
                if near and (fits if confidence == "exact" else len(word) >= 4 and word in t.counterparty.lower()):
                    matches[i] = confidence, dict(r)
                    used.add(r["sha"])
                    break
    return matches


def test_receipt_lookup_preserves_both_passes():
    rng = random.Random(702)
    for _ in range(20):
        receipts = [
            dict(
                sha=str(i),
                doc_date=(date(2026, 10, 1) + timedelta(days=rng.randrange(30))).isoformat(),
                currency=rng.choice(["RON", "EUR"]),
                amount=str(rng.randrange(1, 5)),
                vendor=rng.choice(["Magazin", "Bazar", "Ab", None]),
            )
            for i in range(200)
        ]
        txs = [
            Tx(
                date(2026, 10, 1) + timedelta(days=rng.randrange(30)),
                "",
                "",
                "debit",
                Decimal(rng.randrange(1, 5)),
                "RON",
                "",
                counterparty=rng.choice(["Magazin Bazar", "Magazin", "Ab"]),
                orig_amount=rng.choice([None, Decimal("2")]),
                orig_currency=rng.choice(["", "EUR"]),
            )
            for _ in range(60)
        ]
        assert recon.match_receipts(txs, receipts) == reference(txs, receipts)


def test_receipt_lookup_date_extremes():
    for day in (date.min, date.max):
        tx = Tx(day, "", "", "debit", Decimal("1"), "RON", "")
        receipt = dict(sha="r", doc_date=day.isoformat(), currency="RON", amount="1", vendor="")
        assert recon.match_receipts([tx], [receipt]) == reference([tx], [receipt])


def test_no_transactions_does_not_parse_receipt_dates():
    assert recon.match_receipts([], [dict(sha="bad", doc_date="2026-13-45")]) == {}


def test_receipt_window_includes_both_edges_and_excludes_next_day():
    for offset in (-13, -12, 12, 13):
        day = date(2026, 10, 15)
        tx = Tx(day, "", "", "debit", Decimal("1"), "RON", "")
        receipt = dict(
            sha="r", doc_date=(day + timedelta(days=offset)).isoformat(), currency="RON", amount="1", vendor=""
        )
        result = recon.match_receipts([tx], [receipt])
        assert result == reference([tx], [receipt])
        assert bool(result) == (abs(offset) <= recon.MATCH_DAYS)
