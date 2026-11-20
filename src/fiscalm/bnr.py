"""BNR rate communicated strictly before the VAT chargeability date.

Fiscal Code art. 290(2), Norms pt. 35(1):
https://static.anaf.ro/static/10/Anaf/legislatie/Cod_fiscal_norme_2023.htm
Current feeds: https://curs.bnr.ro/.
Selections are final only when a publication on/after the requested day is present.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from fiscalm.efactura.inbox import UblError, safe_xml
from fiscalm.invoicing.engine import _day, decimal
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.ledger import Ledger

Fetch = Callable[[str], bytes]
NS = {"b": "http://www.bnr.ro/xsd"}
DAILY = "https://curs.bnr.ro/nbrfxrates.xml"


class RateError(InvoiceError):
    """No trustworthy BNR publication is available for this currency and day."""


class UnsupportedCurrency(RateError):
    """A complete BNR feed does not publish this currency."""


def fetch_xml(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.read(10 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and "/years/" in url:
            return b"<DataSet><Body/></DataSet>"
        raise RateError("Cursul BNR nu este disponibil. Încearcă din nou mai târziu.") from None
    except OSError:
        raise RateError("Cursul BNR nu este disponibil. Încearcă din nou mai târziu.") from None


def parse(data: bytes, currency: str) -> list[tuple[str, Decimal]]:
    """Read namespace-qualified BNR rates, including currencies quoted per multiplier units."""
    try:
        root = safe_xml(data)
        if root.tag.rsplit("}", 1)[-1] != "DataSet":
            raise RateError("Răspunsul BNR nu conține cursuri de schimb.")
        out = []
        for cube in root.findall("{*}Body/{*}Cube"):
            day = _day(cube.get("date", ""))
            for rate in cube.findall("{*}Rate"):
                if rate.get("currency") == currency:
                    value, multiplier = decimal(rate.text or ""), decimal(rate.get("multiplier", "1"))
                    if value <= 0 or multiplier <= 0:
                        raise RateError("Cursul sau multiplicatorul din răspunsul BNR nu este pozitiv.")
                    out.append((day, value / multiplier))
        return out
    except UblError:
        raise RateError("Răspunsul BNR nu poate fi citit. Încearcă din nou mai târziu.") from None
    except InvoiceError as exc:
        raise RateError(str(exc)) from None


def rate(
    L: Ledger, currency: str, issue_date: str, fetch: Fetch | None = None, *, today: str | None = None
) -> tuple[str, str]:
    """Return (RON per unit, publication date); never substitute a publication after the date.

    Cache a completed selection, not a partial year: a past partial feed cannot hide a later
    publication for another requested date. No network is needed for a previously selected day.
    """
    fetch = fetch or fetch_xml
    _day(issue_date)
    if issue_date > _day(today or date.today().isoformat()):
        raise RateError("Cursul BNR nu poate fi căutat pentru o dată viitoare.")
    if not re.fullmatch("[A-Z]{3}", currency):
        raise RateError("Moneda trebuie scrisă prin codul de trei litere.")
    if currency == "RON":
        return "1", issue_date
    cached = L.db.execute(
        "SELECT rate,published FROM invoice_rates WHERE currency=? AND invoice_date=?", (currency, issue_date)
    ).fetchone()
    if cached and cached["published"] < issue_date:
        return cached["rate"], cached["published"]
    year = int(issue_date[:4])
    candidates = parse(fetch(DAILY), currency)
    finalized = any(day >= issue_date for day, _ in candidates)
    if not any(day < issue_date for day, _ in candidates) or not any(day >= issue_date for day, _ in candidates):
        yearly = fetch(f"https://curs.bnr.ro/files/xml/years/nbrfxrates{year}.xml")
        candidates += parse(yearly, currency)
        if not candidates and safe_xml(yearly).findall("{*}Body/{*}Cube/{*}Rate"):
            raise UnsupportedCurrency(
                "BNR nu publică un curs pentru această monedă. Pentru o regulă nouă se alege altă monedă."
            )
    eligible = [(day, value) for day, value in candidates if day < issue_date]
    if not eligible and issue_date[5:7] == "01" and int(issue_date[8:]) <= 10 and year > 1:
        eligible = [
            (day, value)
            for day, value in parse(fetch(f"https://curs.bnr.ro/files/xml/years/nbrfxrates{year - 1}.xml"), currency)
            if day < issue_date
        ]
    if not eligible:
        raise RateError("Nu există un curs BNR anterior acestei date. Alege altă monedă sau dată.")
    published, value = max(eligible, key=lambda item: item[0])
    finalized = finalized or any(day >= issue_date for day, _ in candidates)
    if finalized:
        with L.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO invoice_rates VALUES(?,?,?,?)", (currency, issue_date, str(value), published)
            )
    return str(value), published


def recheck_rate(L: Ledger, currency: str, tax_date: str, selected: tuple[str, str]) -> tuple[str, str]:
    """Refuse a changed cached selection without fetching while a writer holds the lock."""
    cached = L.db.execute(
        "SELECT rate,published FROM invoice_rates WHERE currency=? AND invoice_date=?", (currency, tax_date)
    ).fetchone()
    if cached and (decimal(cached["rate"]) != decimal(selected[0]) or cached["published"] != selected[1]):
        raise RateError("Cursul BNR a fost actualizat. Încearcă din nou.")
    return selected


def check_invoice_rate(
    L: Ledger, data: dict, today: str, *, selected: tuple[str, str] | None = None
) -> tuple[str, str] | None:
    """Refuse issuance unless the selected publication and value can be confirmed."""
    if data["currency"] == "RON":
        return None
    from fiscalm.invoicing.engine import chargeability_date

    try:
        value, published = (
            rate(L, data["currency"], chargeability_date(data), today=today)
            if selected is None
            else recheck_rate(L, data["currency"], chargeability_date(data), selected)
        )
    except RateError:
        raise InvoiceError(
            "Cursul BNR nu poate fi confirmat. Factura nu a fost emisă. Încearcă din nou mai târziu."
        ) from None
    if published != data["exchange_date"] or decimal(value) != decimal(data["exchange_rate"]):
        raise InvoiceError(
            "Cursul nu corespunde ultimei publicații BNR anterioare exigibilității. Ciorna trebuie salvată din nou."
        )
    return value, published
