"""Public ANAF taxpayer lookup, v9. No OAuth or credentials.

Official shape and limit (one request/second):
https://static.anaf.ro/static/10/Anaf/Informatii_R/Servicii_web/doc_WS_V9.txt
Registered-office fields are used, never guessed from a free-form address. Lookup data is
untrusted; callers review missing fields before saving. A seller's bank details stay manual.
"""

from __future__ import annotations

import fcntl
import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import date

from fiscalm.company_config import SILENT, UNKNOWN, AnafSilent, CompanyError, normalize_cui
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.ledger import Ledger, finance_home

URL = "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"
Fetch = Callable[[str, bytes], bytes]
_lock = threading.Lock()


class AnafLookupError(InvoiceError):
    """ANAF did not return one unambiguous taxpayer with valid registry flags."""


def fetch_json(url: str, body: bytes) -> bytes:
    state = finance_home() / "state"
    state.mkdir(parents=True, exist_ok=True)
    with _lock, (state / "anaf-public.lock").open("a+") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        gate.seek(0)
        previous = gate.read()
        try:
            previous_at = float(previous)
        except ValueError:
            previous_at = 0
        if previous_at:
            time.sleep(max(0, min(1, 1 - (time.time() - previous_at))))
        gate.seek(0)
        gate.truncate()
        gate.write(str(time.time()))
        gate.flush()
        request = urllib.request.Request(url, body, {"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read(1024 * 1024 + 1)
        except OSError:
            raise AnafLookupError("ANAF lookup is unavailable; retry later.") from None


def parse_found(raw: bytes, cuis: list[str], day: str) -> dict[str, dict]:
    """Validate one ANAF envelope and every registry flag before any caller trusts its rows."""
    if len(raw) > 1024 * 1024:
        raise ValueError
    data = json.loads(raw)
    if data.get("cod", 200) != 200 or not isinstance(data["found"], list):
        raise ValueError
    found = {}
    for row in data["found"]:
        general = row["date_generale"]
        cui = str(general["cui"])
        if cui not in cuis or cui in found or general["data"] != day:
            raise ValueError
        registry_status(row, day)
        found[cui] = row
    missing = data.get("notFound", [])
    if not isinstance(missing, list) or any(str(c) not in cuis or str(c) in found for c in missing):
        raise ValueError
    return found


def _day(value: object) -> str | None:
    if value in ("", None):
        return None
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError
    return value


def registry_status(row: dict, day: str) -> dict:
    """Normalize the registries once for interactive lookups and tick refreshes."""
    general, vat, inactive = row["date_generale"], row["inregistrare_scop_Tva"], row["stare_inactiv"]
    flags = dict(
        vat_payer=vat["scpTVA"],
        efactura_registered=general["statusRO_e_Factura"],
        inactive=inactive["statusInactivi"],
        vat_on_collection=row["inregistrare_RTVAI"]["statusTvaIncasare"],
    )
    if any(type(v) is not bool for v in flags.values()) or not isinstance(general["stare_inregistrare"], str):
        raise ValueError
    periods = vat.get("perioade_TVA", [])
    periods = [periods] if isinstance(periods, dict) else periods
    starts = []
    for period in periods:
        start, end = _day(period.get("data_inceput_ScpTVA")), _day(period.get("data_sfarsit_ScpTVA"))
        if start and start <= day and (not end or end >= day):
            starts.append(start)
    return dict(
        checked_at=day,
        **flags,
        vat_since=starts[0] if flags["vat_payer"] and len(starts) == 1 else None,
        inactive_register_removed_at=_day(inactive.get("dataRadiere")),
        registration_status=general["stare_inregistrare"][:200],
    )


def cached(L: Ledger, cui: str) -> tuple[dict | None, str | None]:
    row = L.db.execute("SELECT status,error FROM partner_anaf WHERE cui=?", (cui,)).fetchone()
    return (json.loads(row["status"]) if row["status"] else None, row["error"]) if row else (None, None)


def store_status(L: Ledger, cui: str, status: dict) -> None:
    with L.tx() as db:
        db.execute(
            "INSERT INTO partner_anaf(cui,attempted_at,status,error) VALUES(?,?,?,NULL) "
            "ON CONFLICT(cui) DO UPDATE SET status=excluded.status,error=NULL, "
            "attempted_at=MAX(partner_anaf.attempted_at,excluded.attempted_at) "
            "WHERE partner_anaf.status IS NULL OR json_extract(partner_anaf.status,'$.checked_at')<=?",
            (cui, status["checked_at"], json.dumps(status), status["checked_at"]),
        )


def lookup(cui: str, day: str, fetch: Fetch = fetch_json, *, L: Ledger | None = None) -> dict:
    """Return one validated profile; cache only through the caller's ledger when supplied."""
    if not isinstance(cui, str):
        raise AnafLookupError("CUI-ul trebuie scris ca text.")
    cui = cui.removeprefix("RO")
    if not re.fullmatch("[0-9]{2,10}", cui):
        raise AnafLookupError("Enter a Romanian CUI containing 2 to 10 digits.")
    try:
        date.fromisoformat(day)
        raw = fetch(URL, json.dumps([{"cui": int(cui), "data": day}]).encode())
        row = parse_found(raw, [cui], day)[cui]
        address = row["adresa_sediu_social"]
        status = registry_status(row, day)

        country = address.get("stara")
        if not isinstance(country, str) or country.strip().upper() not in {"", "RO", "ROMANIA", "ROMÂNIA"}:
            raise ValueError
        if L is not None:
            store_status(L, cui, status)
        return _profile(row, cui, status)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise AnafLookupError("ANAF returned no unambiguous complete taxpayer; verify the CUI and retry.") from None


def _profile(row: dict, cui: str, status: dict) -> dict:
    general, address = row["date_generale"], row.get("adresa_sediu_social", {})

    def text(value: object) -> str:
        return value if isinstance(value, str) else str(value) if type(value) is int else ""

    county = text(address.get("scod_JudetAuto")).upper()
    vat, registered, inactive = (status[k] for k in ("vat_payer", "efactura_registered", "inactive"))
    return dict(
        cui=cui,
        name=text(general["denumire"]),
        reg_com=text(general.get("nrRegCom")),
        vat_payer=vat,
        vat_id="RO" + cui if vat else "",
        efactura_registered=registered,
        inactive=inactive,
        country="RO" if "stara" in address else "",
        county="RO-" + county if county else "",
        street=" ".join(text(address.get(k)) for k in ("sdenumire_Strada", "snumar_Strada", "sdetalii_Adresa")).strip(),
        city=_city(text(address.get("sdenumire_Localitate")), county),
        postal_code=text(address.get("scod_Postal")),
        phone=text(general.get("telefon")),
        email="",
    )


def remember(L: Ledger, profile: dict, day: str) -> None:
    """Retain the last trusted lookup, including inactivity, independently of client edits."""
    old, _ = cached(L, profile["cui"])
    status = dict(
        checked_at=day,
        vat_since=None,
        inactive_register_removed_at=None,
        registration_status="",
        efactura_registered=None,
        vat_on_collection=None,
        vat_payer=None,
        inactive=None,
    )
    if old:
        status.update(old)
    status.update(
        checked_at=day, **{k: profile[k] for k in ("inactive", "vat_payer", "efactura_registered") if k in profile}
    )
    store_status(L, profile["cui"], status)


def _city(value: str, county: str) -> str:
    match = re.search(r"(?i)sector\s*([1-6])", value)
    return "SECTOR" + match[1] if county == "B" and match else value


def lookup_company(cui: str, day: str) -> dict:
    """Validate a setup profile without opening or writing a ledger."""
    cui = normalize_cui(cui)
    try:
        raw = fetch_json(URL, json.dumps([{"cui": int(cui), "data": day}]).encode())
        found = parse_found(raw, [cui], day)
        unknown = not found and [str(c) for c in json.loads(raw)["notFound"]] == [cui]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, AnafLookupError):
        raise AnafSilent(SILENT) from None
    if unknown:
        raise CompanyError(UNKNOWN.format(cui=cui))
    try:
        row = found[cui]
        general = row["date_generale"]
        status = registry_status(row, day)
        vat, inactive = status["vat_payer"], status["inactive"]
        if (
            not isinstance(general["denumire"], str)
            or len(general["denumire"]) > 200
            or not general["denumire"].strip()
            or not isinstance(general["adresa"], str)
            or not general["adresa"].strip()
        ):
            raise ValueError
        office = row.get("adresa_sediu_social", {})
        if "stara" in office and (
            not isinstance(office["stara"], str)
            or office["stara"].strip().upper() not in {"", "RO", "ROMANIA", "ROMÂNIA"}
        ):
            raise ValueError
        return dict(
            cui=cui,
            legal_name=general["denumire"],
            address=general["adresa"],
            vat_payer=vat,
            inactive=inactive,
            invoice_profile=_profile(row, cui, status) | dict(vat_on_collection=status["vat_on_collection"]),
        )
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise AnafSilent(SILENT) from None
