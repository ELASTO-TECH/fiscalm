"""Public partner registries: bounded tick refreshes, cache-only state reads."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal

from fiscalm.company_config import valid_cui
from fiscalm.efactura import public as anaf_public
from fiscalm.identity import partner_identity
from fiscalm.ledger import Ledger

BATCH_SIZE = 100  # ANAF v9: doc_WS_V9.txt; transport also limits requests to one/second.


def eligible_cui(country: str | None, cui: str | None) -> str:
    """Unknown-country e-Factura numeric ids may be Romanian; explicit foreign ids never are."""
    if not isinstance(cui, str) or country not in (None, "", "RO"):
        return ""
    token = partner_identity("RO", cui, None)
    return token if re.fullmatch(r"[1-9][0-9]{1,9}", token) and valid_cui(token) else ""


def cached(L: Ledger, cui: str) -> tuple[dict | None, str | None]:
    return anaf_public.cached(L, cui)


def inactive(status: dict | None) -> bool:
    """Only a trusted positive cached flag proves inactivity, even after a failed refresh."""
    return bool(status and status.get("inactive") is True)


def removed(status: dict | None) -> bool:
    """ANAF registration status proves removal; leaving the inactive register does not."""
    # v9 field (values are not enumerated):
    # https://static.anaf.ro/static/10/Anaf/Informatii_R/Servicii_web/doc_WS_V9.txt
    # RADIERE is the review-reported event form (not enumerated in v9); do not confuse inactive-register dataRadiere with it.
    return bool(
        status
        and re.match(
            r"^(?:RADIERE|RADIAT[ĂA]?)(?:\s+DIN DATA\s+\d{2}\.\d{2}\.\d{4})?$",
            status.get("registration_status", "").strip().upper(),
        )
    )


def supplier_cui(invoice: dict) -> str:
    """The CUI fiscalm checks at ANAF for an invoice's supplier, or '' when it is not an eligible Romanian one."""
    address = json_object(invoice["party_address"] or "{}")
    return (
        eligible_cui(address.get("country"), invoice["supplier_tax_id"] or invoice["supplier_cui"])
        if address is not None
        else ""
    )


def cached_statuses(L: Ledger, cuis: set[str]) -> dict[str, dict | None]:
    """`cached(L, cui)[0]` for every CUI, in one read: what a state needs for many invoices at once."""
    found = {
        r["cui"]: json.loads(r["status"]) if r["status"] else None
        for r in L.db.execute(
            "SELECT cui,status FROM partner_anaf WHERE cui IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(cuis)),),
        )
    }
    return {cui: found.get(cui) for cui in cuis}


def tasks(partners: list[dict]) -> list[dict]:
    """One warning per company, partner and exposed role, using cache and outstanding balances only."""
    out = []
    for p in partners:
        status = p["anaf_status"]
        for role, balance, label, detail in (
            (
                "supplier",
                "payable",
                "Furnizor inactiv la ANAF",
                "Există facturi de plătit. Verifică situația furnizorului cu contabilul.",
            ),
            (
                "client",
                "receivable",
                "Client inactiv sau radiat la ANAF",
                "Există facturi de încasat. Verifică situația clientului.",
            ),
        ):
            money = p[balance]
            outstanding = any(Decimal(v) > 0 for v in [money["total"], *[x["total"] for x in money["other"]]])
            if outstanding and (inactive(status) or (role == "client" and removed(status))):
                out.append(
                    dict(
                        kind="inactive_" + role,
                        id=p["id"],
                        company=p["company"],
                        label=label,
                        detail=detail,
                        checked_at=status["checked_at"],
                    )
                )
    return out


def json_object(raw: str) -> dict | None:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, RecursionError):
        return None


def party_cui(party: dict) -> str:
    return (
        eligible_cui("RO", party.get("cui") or party.get("vat_id"))
        if party.get("country") == "RO" and not party.get("private")
        else ""
    )


def _cuis(L: Ledger) -> list[str]:
    ids: set[str] = set()
    for table, column in (("efactura", "supplier_cui"), ("issued", "client_cui")):
        for row in L.db.execute(
            f"SELECT COALESCE(NULLIF({column.replace('_cui', '_tax_id')},''),{column}),party_address FROM {table}"
        ):
            address = json_object(row[1] or "{}")
            if address is not None:
                ids.add(eligible_cui(address.get("country"), row[0]))
    for row in L.db.execute(
        "SELECT data FROM invoice_clients UNION ALL SELECT data FROM invoices WHERE status!='draft'"
    ):
        record = json_object(row[0])
        if record is not None:
            party = record.get("client", record)
            if isinstance(party, dict):
                ids.add(party_cui(party))
    return sorted(ids - {""})


def refresh(L: Ledger, today: date, fetch: anaf_public.Fetch | None = None) -> dict:
    """Claim only the next batch. Interrupted attempts remain errors and retry after one day."""
    days = json.loads(L.get_meta("partner_anaf_refresh_days", "7"))
    if days is False or days == 0:
        return dict(attempted=0, errors=0)
    if type(days) is not int or days < 1:
        raise ValueError("partner_anaf_refresh_days must be a positive integer or false/0.")
    day = today.isoformat()
    cutoff, retry = (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()
    candidates = _cuis(L)
    attempted = errors = 0
    for offset in range(0, len(candidates), BATCH_SIZE):
        batch = []
        with L.tx() as db:
            for cui in candidates[offset : offset + BATCH_SIZE]:
                claimed = db.execute(
                    "INSERT INTO partner_anaf(cui,attempted_at,error) VALUES(?,?,'unavailable') "
                    "ON CONFLICT(cui) DO UPDATE SET attempted_at=excluded.attempted_at,error='unavailable' "
                    "WHERE (partner_anaf.error IS NULL AND partner_anaf.attempted_at<=?) "
                    "OR (partner_anaf.error IS NOT NULL AND partner_anaf.attempted_at<=?)",
                    (cui, day, cutoff, retry),
                )
                if claimed.rowcount:
                    batch.append(cui)
        if not batch:
            continue
        attempted += len(batch)
        statuses, error = {}, None
        try:
            raw = (fetch or anaf_public.fetch_json)(
                anaf_public.URL, json.dumps([{"cui": int(c), "data": day} for c in batch]).encode()
            )
            statuses = {
                cui: anaf_public.registry_status(row, day)
                for cui, row in anaf_public.parse_found(raw, batch, day).items()
            }
        except (OSError, anaf_public.AnafLookupError):
            error = "unavailable"
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            error = "invalid_response"
        with L.tx() as db:
            for cui in batch:
                status = statuses.get(cui)
                errors += status is None
                if status:
                    anaf_public.store_status(L, cui, status)
                else:
                    db.execute(
                        "UPDATE partner_anaf SET error=? WHERE cui=? AND attempted_at=?",
                        (error or "not_found", cui, day),
                    )
    return dict(attempted=attempted, errors=errors)
