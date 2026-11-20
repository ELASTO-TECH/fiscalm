"""Locally authored invoices. Issuing freezes parties, lines and totals and reserves one number.

All money is decimal text. VAT is rounded HALF_UP per category/rate on rounded line nets.
Issuing also writes the invoice's own `issued` row (`LOCAL`) in the same transaction, in Sandbox and Live alike:
that row is what a client owes and what a collection settles. The SPV copy read back later is evidence stored as
its `duplicate`, never a second collectable record.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import uuid4

from fiscalm import companies, company_config, ledger
from fiscalm.efactura import client as anaf, public as anaf_public
from fiscalm.efactura.inbox import Invoice, parse_ubl, tax_token
from fiscalm.history_invoices import kind_total
from fiscalm.invoicing.canvas import validate_text
from fiscalm.invoicing.copy import FIELDS
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.invoicing.labels import COUNTY_NAMES, NON_EU_SERVICES_REASON
from fiscalm.ledger import Ledger

LOCAL = "fiscalm"  # upload_id of the `issued` row fiscalm wrote when it issued the invoice: no SPV message, no ZIP
NOT_OURS = "supplier CUI"  # the issue a sent invoice carries when its supplier is not the company

# BT-153 Item/Name: BR-RO-L1024 (see docs/efactura-anaf.md).
LINE_DESCRIPTION_MAX = 100
LINE_DESCRIPTION_ERROR = f"Descrierea și motivul scutirii pot avea cel mult {LINE_DESCRIPTION_MAX} de caractere."


def decimal(value: str | int | Decimal) -> Decimal:
    """Read finite decimals with a dot or comma, refusing grouping and binary floats."""
    try:
        if isinstance(value, (float, bool)) or (
            isinstance(value, str) and not re.fullmatch(r"[+-]?(?:[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+)", value)
        ):
            raise ValueError
        if isinstance(value, str):
            value = value.replace(",", ".")
        result = Decimal(value)
        if not result.is_finite() or abs(result) > Decimal("1e15") or result.as_tuple().exponent < -12:
            raise ValueError
        return Decimal(format(result, "f"))
    except (InvalidOperation, ValueError, TypeError):
        raise InvoiceError("Cantitățile și sumele trebuie scrise ca numere zecimale finite.") from None


def money(value: Decimal) -> str:
    if not value.is_finite() or abs(value) > Decimal("1e15"):
        raise InvoiceError("Suma facturii este prea mare.")
    return format(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP), "f")


def _day(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise InvoiceError("Data trebuie scrisă în formatul AAAA-LL-ZZ.") from None


def _company(L: Ledger, company: str) -> None:
    if company not in companies.all(L):
        raise InvoiceError("Factura are nevoie de o firmă configurată.")


EU_COUNTRIES = set(
    [
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IE",
        "IT",
        "LV",
        "LT",
        "LU",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
    ]
)

# ISO 3166-1 alpha-2; VAT prefixes EL and XI are not countries.
COUNTRIES = {"1A"} | set(
    [
        "AD",
        "AE",
        "AF",
        "AG",
        "AI",
        "AL",
        "AM",
        "AO",
        "AQ",
        "AR",
        "AS",
        "AT",
        "AU",
        "AW",
        "AX",
        "AZ",
        "BA",
        "BB",
        "BD",
        "BE",
        "BF",
        "BG",
        "BH",
        "BI",
        "BJ",
        "BL",
        "BM",
        "BN",
        "BO",
        "BQ",
        "BR",
        "BS",
        "BT",
        "BV",
        "BW",
        "BY",
        "BZ",
        "CA",
        "CC",
        "CD",
        "CF",
        "CG",
        "CH",
        "CI",
        "CK",
        "CL",
        "CM",
        "CN",
        "CO",
        "CR",
        "CU",
        "CV",
        "CW",
        "CX",
        "CY",
        "CZ",
        "DE",
        "DJ",
        "DK",
        "DM",
        "DO",
        "DZ",
        "EC",
        "EE",
        "EG",
        "EH",
        "ER",
        "ES",
        "ET",
        "FI",
        "FJ",
        "FK",
        "FM",
        "FO",
        "FR",
        "GA",
        "GB",
        "GD",
        "GE",
        "GF",
        "GG",
        "GH",
        "GI",
        "GL",
        "GM",
        "GN",
        "GP",
        "GQ",
        "GR",
        "GS",
        "GT",
        "GU",
        "GW",
        "GY",
        "HK",
        "HM",
        "HN",
        "HR",
        "HT",
        "HU",
        "ID",
        "IE",
        "IL",
        "IM",
        "IN",
        "IO",
        "IQ",
        "IR",
        "IS",
        "IT",
        "JE",
        "JM",
        "JO",
        "JP",
        "KE",
        "KG",
        "KH",
        "KI",
        "KM",
        "KN",
        "KP",
        "KR",
        "KW",
        "KY",
        "KZ",
        "LA",
        "LB",
        "LC",
        "LI",
        "LK",
        "LR",
        "LS",
        "LT",
        "LU",
        "LV",
        "LY",
        "MA",
        "MC",
        "MD",
        "ME",
        "MF",
        "MG",
        "MH",
        "MK",
        "ML",
        "MM",
        "MN",
        "MO",
        "MP",
        "MQ",
        "MR",
        "MS",
        "MT",
        "MU",
        "MV",
        "MW",
        "MX",
        "MY",
        "MZ",
        "NA",
        "NC",
        "NE",
        "NF",
        "NG",
        "NI",
        "NL",
        "NO",
        "NP",
        "NR",
        "NU",
        "NZ",
        "OM",
        "PA",
        "PE",
        "PF",
        "PG",
        "PH",
        "PK",
        "PL",
        "PM",
        "PN",
        "PR",
        "PS",
        "PT",
        "PW",
        "PY",
        "QA",
        "RE",
        "RO",
        "RS",
        "RU",
        "RW",
        "SA",
        "SB",
        "SC",
        "SD",
        "SE",
        "SG",
        "SH",
        "SI",
        "SJ",
        "SK",
        "SL",
        "SM",
        "SN",
        "SO",
        "SR",
        "SS",
        "ST",
        "SV",
        "SX",
        "SY",
        "SZ",
        "TC",
        "TD",
        "TF",
        "TG",
        "TH",
        "TJ",
        "TK",
        "TL",
        "TM",
        "TN",
        "TO",
        "TR",
        "TT",
        "TV",
        "TW",
        "TZ",
        "UA",
        "UG",
        "UM",
        "US",
        "UY",
        "UZ",
        "VA",
        "VC",
        "VE",
        "VG",
        "VI",
        "VN",
        "VU",
        "WF",
        "WS",
        "YE",
        "YT",
        "ZA",
        "ZM",
        "ZW",
    ]
)

COUNTIES = {"RO-" + code for code in COUNTY_NAMES}


def _fields(data: dict, strings: set[str], flags: set[str] = frozenset()) -> dict:
    if not isinstance(data, dict) or set(data) - strings - flags:
        raise InvoiceError("Factura conține câmpuri necunoscute.")
    for key, value in data.items():
        if key in flags:
            if type(value) is not bool:
                raise InvoiceError("Alegerile de pe factură trebuie să fie bifate sau nu.")
        elif not isinstance(value, str) or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
            raise InvoiceError("Textul facturii conține caractere nepermise.")
    validate_text(data)
    return dict(data)


def normalize_vat_id(value: str) -> str:
    """Canonical VAT identity for validation, matching and transmission scope."""
    return re.sub(r"\s+", "", value).upper()


def _party(data: dict, seller: bool = False) -> dict:
    data = _fields(
        data,
        set(
            [
                "name",
                "street",
                "city",
                "country",
                "county",
                "postal_code",
                "cui",
                "vat_id",
                "iban",
                "bank",
                "account_name",
                "reg_com",
                "email",
                "phone",
                "tax_id",
                "ro_vat_status",
            ]
        ),
        set(["vat_payer", "private", "efactura_registered", "inactive"]) | ({"vat_on_collection"} if seller else set()),
    )
    for field in ("name", "street", "city", "country"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise InvoiceError(f"Lipsește {FIELDS[field]}.")
    if data["country"] not in COUNTRIES:
        raise InvoiceError(
            "Țara se scrie cu codul ISO: GR pentru Grecia, GB pentru Regatul Unit; EL și XI sunt prefixe TVA."
        )
    if "vat_id" in data:
        data["vat_id"] = normalize_vat_id(data["vat_id"])
    if not seller:
        _client_vat_registration(data)
    _party_address(data)
    if not seller:
        _client_tax_identity(data)
    if (not seller and data["country"] != "RO" and data.get("vat_id", "").startswith("RO")) and (
        not re.fullmatch("RO[0-9]{2,10}", data["vat_id"]) or data.get("cui") != data["vat_id"][2:]
    ):
        raise InvoiceError(
            "Pentru un client străin înregistrat în România, lipsesc codul TVA RO și CUI-ul românesc corespunzător."
        )
    if seller:
        _seller_tax_identity(data)
    return data


def _client_vat_registration(data: dict) -> None:
    data.setdefault("ro_vat_status", "unknown")
    if data["ro_vat_status"] not in {"unknown", "registered", "not_registered"}:
        raise InvoiceError(
            "Lipsește înregistrarea în scopuri de TVA în România: necunoscută, înregistrat sau nestabilit și neînregistrat."
        )
    if (
        data["country"] != "RO"
        and data["ro_vat_status"] == "not_registered"
        and data.get("vat_id", "").startswith("RO")
    ):
        raise InvoiceError(
            "Codul TVA românesc contrazice starea de neînregistrat în România. Codul sau starea înregistrării trebuie corectate."
        )


def _party_address(data: dict) -> None:
    if data["country"] == "RO" and data.get("county") not in COUNTIES:
        raise InvoiceError("Județul trebuie scris în formatul RO-XX.")
    if data.get("county") == "RO-B":
        match = re.match(r"(?i)^sector\s*([1-6])(?:\b|$)", data["city"])
        if not match:
            raise InvoiceError("Pentru București, localitatea trebuie să fie SECTOR1 până la SECTOR6.")
        data["city"] = "SECTOR" + match[1]
    for key, limit in dict(
        tax_id=100, name=200, street=150, city=50, postal_code=20, reg_com=1000, email=100, phone=100, account_name=200
    ).items():
        if len(data.get(key, "")) > limit:
            raise InvoiceError(f"{FIELDS[key].capitalize()} poate avea cel mult {limit} de caractere.")


def _client_tax_identity(data: dict) -> None:
    # Fiscal Code art. 316(1), 319(20)(f); VAT Directive art. 215 (EL for Greece).
    # https://static.anaf.ro/static/10/Anaf/legislatie/Cod_fiscal_norme_2023.htm
    # https://eur-lex.europa.eu/eli/dir/2006/112/oj/eng
    data["tax_id"] = data.get("tax_id", "").strip()
    for key in ("cui", "vat_id"):
        data[key] = normalize_vat_id(data.get(key, ""))
    if not (
        data["cui"] or data["vat_id"] or (data["tax_id"] and data["country"] != "RO") or data.get("private") is True
    ):
        raise InvoiceError("Lipsește identificarea fiscală, sau clientul trebuie declarat persoană fizică.")
    if data["country"] == "RO":
        _client_romanian_tax_id(data)
    elif data["vat_id"]:
        _client_foreign_vat_id(data)


def _client_romanian_tax_id(data: dict) -> None:
    if data["cui"].startswith("RO") and not data["vat_id"]:
        data["vat_id"] = data["cui"]
    data["cui"] = data["cui"].removeprefix("RO")
    if data["vat_id"]:
        if not re.fullmatch(r"RO[0-9]{2,10}", data["vat_id"]):
            raise InvoiceError("Codul de TVA al clientului trebuie să fie RO urmat de CUI.")
        if not data["cui"]:
            data["cui"] = data["vat_id"][2:]
        if data["vat_id"] != "RO" + data["cui"]:
            raise InvoiceError("Codul de TVA al clientului trebuie să fie RO urmat de același CUI.")
    if data["cui"]:
        digits = data["cui"]
        # Invalid overlong CUIs are refused below, after computing the checksum.
        control = (
            sum(int(a) * int(b) for a, b in zip(digits[:-1].zfill(9), "753217532", strict=False)) * 10 % 11
            if digits.isdigit()
            else -1
        )
        if not re.fullmatch(r"[1-9][0-9]{1,9}", digits) or int(digits[-1]) != (0 if control == 10 else control):
            raise InvoiceError("CUI-ul clientului trebuie să conțină cifre și o cifră de control validă.")
    if data.get("vat_payer") and not data["vat_id"]:
        raise InvoiceError("Lipsește codul de TVA al clientului plătitor de TVA.")


def _client_foreign_vat_id(data: dict) -> None:
    prefix = data["vat_id"][:2]
    allowed = (EU_COUNTRIES - {"GR"}) | {"EL"} | ({"GB", "XI"} if data["country"] == "GB" else set())
    if prefix not in allowed:
        raise InvoiceError(
            "Codul de TVA al clientului trebuie să fie un cod UE sau GB/XI pentru Regatul Unit. Identificatorii fiscali din afara UE se introduc în tax_id."
        )
    # Cheap syntax checks, not VIES registration verification.
    # https://www.vero.fi/en/businesses-and-corporations/taxes-and-charges/vat/international-commerce/vat-number/vat-numbers-issued-by-different-eu-countries/
    pattern = {
        "DE": r"DE[0-9]{9}",
        "EL": r"EL[0-9]{9}",
        "FR": r"FR[A-Z0-9]{2}[0-9]{9}",
        "IT": r"IT[0-9]{11}",
        "XI": r"XI[0-9]{9}",
        "GB": r"GB(?:[0-9]{9}(?:[0-9]{3})?|GD[0-4][0-9]{2}|HA[5-9][0-9]{2})",
    }.get(prefix, prefix + r"[A-Z0-9]+")
    if not re.fullmatch(pattern, data["vat_id"]):
        raise InvoiceError("Codul de TVA al clientului nu corespunde țării sau formatului acesteia.")


def _seller_tax_identity(data: dict) -> None:
    data.setdefault("vat_on_collection", False)
    if data["vat_on_collection"] and not data.get("vat_payer"):
        data["vat_on_collection"] = False
    if not re.fullmatch("[0-9]{2,10}", data.get("cui", "")):
        raise InvoiceError("CUI-ul firmei trebuie să aibă între 2 și 10 cifre.")
    if type(data.get("vat_payer")) is not bool:
        raise InvoiceError("Lipsește alegerea: firma este sau nu plătitoare de TVA.")
    # A Romanian VAT payer's code is RO followed by its CUI (Fiscal Code art. 316(1)): derived, never asked.
    data["vat_id"] = "RO" + data["cui"] if data["vat_payer"] else ""
    if not data.get("iban") or not data.get("bank"):
        raise InvoiceError("Lipsesc IBAN-ul și banca firmei.")


NOT_AN_ACCOUNT = (
    "IBAN-ul de pe factură trebuie să fie un cont al firmei. Contul se adaugă în Tranzacții, apoi se alege aici."
)
ACCOUNT_GONE = "Contul de pe factură nu este un cont curent al firmei. Alege alt cont pe factură, apoi emite-o din nou."
BANK_UNKNOWN = "Banca acestui cont nu este cunoscută. Alege contul din datele firmei pe factură sau alt cont."


def seller_account(L: Ledger, company: str, profile: dict) -> dict:
    """The seller's IBAN is one of the company's current accounts, so what an invoice asks clients to pay into is
    an account fiscalm reads; the bank's name comes from the bank catalogue when it knows the bank."""
    from fiscalm import companies

    if not isinstance(profile, dict) or not isinstance(profile.get("iban"), str) or not profile["iban"].strip():
        return profile  # _party says what is missing
    account = companies.get(company, L=L).account(profile["iban"])
    if not account or account.retired:
        raise InvoiceError(NOT_AN_ACCOUNT)
    return profile | {"iban": account.iban, "bank": _bank_name(account.bank, profile)}


def _bank_name(bank_id: str, profile: dict) -> str:
    """The catalogue's name for the account's bank; the profile's own only for a bank the catalogue doesn't know."""
    from fiscalm.bank import catalogue as banks

    if bank_id in banks.BY_ID:
        return banks.BY_ID[bank_id].name
    return profile["bank"] if isinstance(profile.get("bank"), str) else ""


def issuing_account(L: Ledger, company: str, seller: dict, iban: str | None = None) -> dict:
    """The seller an invoice is saved, previewed and issued with: its chosen account (`iban`, a draft's choice), else
    the profile's, must be a current account of the company. A profile saved before accounts were checked, or an
    account retired since, issues nothing. The bank is named by the catalogue, or by the profile for its own account.
    Invoices already issued keep their snapshot, and a storno repeats its original's seller as it was."""
    from fiscalm import companies

    account = companies.get(company, L=L).account(iban or seller.get("iban", ""))
    if not account or account.retired:
        raise InvoiceError(ACCOUNT_GONE)
    own = account.iban == companies.normalize_iban(seller.get("iban", ""))
    bank = _bank_name(account.bank, seller if own else {})
    if not bank:
        raise InvoiceError(BANK_UNKNOWN)
    # A profile saved before these were derived prints them derived too.
    vat_id = "RO" + seller["cui"] if seller.get("vat_payer") else ""
    return seller | {"iban": account.iban, "bank": bank, "vat_id": vat_id}


def account_choices(L: Ledger, company: str, seller: dict) -> dict:
    """The accounts a new invoice can name (what `issuing_account` accepts) and, per currency, the one it starts with:
    the profile's when it is in that currency, else the first account in it, else the profile's."""
    from fiscalm import companies

    own = companies.normalize_iban(seller.get("iban", ""))
    named = [
        (a, _bank_name(a.bank, seller if a.iban == own else {})) for a in companies.get(company, L=L).current_accounts
    ]
    accounts = [(a, bank) for a, bank in named if bank]
    by_currency = {a.currency: a.iban for a, _ in reversed(accounts)} | {
        a.currency: a.iban for a, _ in accounts if a.iban == own
    }
    return dict(
        accounts=[dict(iban=a.iban, currency=a.currency, bank_name=bank) for a, bank in accounts],
        default={c: by_currency.get(c, own) for c in sorted(set(by_currency) | {"RON", "EUR"})},
    )


def draft_seller(L: Ledger, inv: dict) -> dict:
    """The seller a draft shows and issues with: a storno draft repeats its original's, as `storno` issues it; any
    other draft, the profile with the draft's chosen account (`issuing_account` refuses one no longer current)."""
    refund = L.db.execute(
        "SELECT original_id FROM invoice_refund_drafts WHERE draft_id=? AND company=?", (inv["id"], inv["company"])
    ).fetchone()
    if refund:
        return invoice(L, refund[0], company=inv["company"])["data"]["seller"]
    return issuing_account(L, inv["company"], _seller(L, inv["company"]), inv["data"].get("iban"))


def draft_account(L: Ledger, inv: dict) -> str:
    """The IBAN a draft issues with, to show on it; for an account no longer current, the stored choice ("" for the
    profile's), so the app asks for another instead of showing one the engine would not use."""
    try:
        return draft_seller(L, inv)["iban"]
    except InvoiceError:
        return inv["data"].get("iban", "")


def save_seller(L: Ledger, company: str, profile: dict) -> None:
    """Replace the seller profile; already issued invoices retain their snapshot."""
    _company(L, company)
    data = json.dumps(_party(seller_account(L, company, profile), True))
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoice_sellers VALUES(?,?) ON CONFLICT(company) DO UPDATE SET data=excluded.data",
            (company, data),
        )


def _identifier(value: str | None, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not value or len(value) > 128:
        raise InvoiceError("Identificatorul trebuie să fie un text nevid de cel mult 128 de caractere.")


def _active_client(L: Ledger, profile: dict) -> None:
    cui = tax_token(profile.get("cui") or profile.get("vat_id") or "")
    status, _ = anaf_public.cached(L, cui)
    if profile.get("inactive") or (status and status.get("inactive", False)):
        raise InvoiceError("Contribuabilul este inactiv. Verifică starea lui înainte să emiți factura.")


def _identity(data: dict) -> str:
    from fiscalm.identity import partner_identity

    return partner_identity(data.get("country", ""), data.get("cui"), data.get("vat_id"))


PARTNER_ROLES = ("supplier", "client")


def client_roles(db, client_ids: list[str]) -> dict[str, list[str]]:
    """What the firm saved each partner as, in one read: a client, unless it was saved as a supplier (Parteneri ›
    Partener nou)."""
    rows = db.execute(
        "SELECT key,value FROM meta WHERE key IN (SELECT 'client_roles:' || value FROM json_each(?))",
        (json.dumps(client_ids),),
    )
    saved = {r["key"].removeprefix("client_roles:"): json.loads(r["value"]) for r in rows}
    return {cid: saved.get(cid, ["client"]) for cid in client_ids}


def save_client(L: Ledger, company: str, profile: dict, client_id: str | None = None, role: str | None = None) -> str:
    """Save a client without merging colliding legacy identities. `role` adds what the firm saves it as: without it a new
    partner is a client and one saved by id keeps its roles. With a role and no id, a firm already saved by its identity
    keeps its details and only gains the role (Parteneri › Partener nou never overwrites what was typed there)."""
    _company(L, company)
    _identifier(client_id, optional=True)
    if role not in (None, *PARTNER_ROLES):
        raise InvoiceError("Partenerul se salvează ca furnizor sau client.")
    with L.tx() as db:
        db.execute("UPDATE invoice_sellers SET data=data WHERE company=?", (company,))
        old = (
            db.execute("SELECT company,data FROM invoice_clients WHERE id=?", (client_id,)).fetchone()
            if client_id
            else None
        )
        if old and old["company"] != company:
            raise InvoiceError("Clientul aparține altei firme.")
        data = _party((json.loads(old["data"]) if old else {}) | profile)
        _active_client(L, data)
        identity = _identity(data)
        saved = {
            r["id"]: json.loads(r["data"])
            for r in db.execute("SELECT id,data FROM invoice_clients WHERE company=?", (company,))
            if identity and _identity(json.loads(r["data"])) == identity
        }
        matches = list(saved)
        if len(matches) > 1 or (client_id and any(cid != client_id for cid in matches)):
            raise InvoiceError("Există clienți cu aceeași identitate fiscală. Corectează-i separat înainte să salvezi.")
        cid = client_id or (matches[0] if matches else uuid4().hex)
        if role and not client_id and matches:
            data = saved[cid]
        # A stale key may occupy the new key even when its stored profile differs.
        occupied = (
            db.execute(
                "SELECT id FROM invoice_clients WHERE company=? AND identity=? AND id!=?", (company, identity, cid)
            ).fetchone()
            if identity
            else None
        )
        if occupied:
            raise InvoiceError("Există clienți cu aceeași identitate fiscală. Corectează-i separat înainte să salvezi.")
        db.execute(
            "INSERT INTO invoice_clients VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "identity=excluded.identity,data=excluded.data",
            (cid, company, identity or None, json.dumps(data)),
        )
        held, role = (client_roles(db, [cid])[cid] if old or matches else []), role or (None if client_id else "client")
        if role and role not in held:
            roles = [r for r in PARTNER_ROLES if r in (*held, role)]
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("client_roles:" + cid, json.dumps(roles)))
    return cid


def fill_client(L: Ledger, company: str, cui: str, day: str, fetch: anaf_public.Fetch = anaf_public.fetch_json) -> str:
    """Look up and save a company-scoped client. Refuse inactive taxpayers."""
    profile = anaf_public.lookup(cui, _day(day), fetch, L=L)
    anaf_public.remember(L, profile, day)
    return save_client(L, company, profile)


def create_series(L: Ledger, company: str, code: str, start: int = 1) -> None:
    """Create a series once, optionally continuing an externally used series. Never reset it."""
    _company(L, company)
    if (
        not isinstance(code, str)
        or not re.fullmatch("[A-Z][A-Z0-9-]{0,19}", code)
        or type(start) is not int
        or not 1 <= start < 10**12
    ):
        raise InvoiceError(
            "Seria trebuie scrisă cu majuscule, iar primul număr trebuie să fie pozitiv și mai mic de 1000000000000."
        )
    with L.tx() as db:
        if db.execute("SELECT 1 FROM invoice_series WHERE company=? AND code=?", (company, code)).fetchone():
            raise InvoiceError("Seria există deja și numărul ei nu poate fi resetat.")
        used = _imported_max(L, company, code)
        if start <= used:
            raise SeriesRefusal(_IMPORTED.format(code=code, used=used, next=used + 1))
        db.execute("INSERT INTO invoice_series VALUES(?,?,?,?)", (company, code, start, start))


_SERIES_DEFAULT = "invoice_series_default:"
# Why a series cannot be removed, checked in this order: an invoice numbered in it is legal record; a draft or a
# recurring rule would lose the series it was written for.
_SERIES_IN_USE = (
    ("SELECT 1 FROM invoices WHERE company=? AND series=? AND status!='draft'", "Seria are facturi emise."),
    ("SELECT 1 FROM invoices WHERE company=? AND series=? AND status='draft'", "O ciornă folosește seria."),
    ("SELECT 1 FROM invoice_recurrences WHERE company=? AND series=?", "O factură recurentă folosește seria."),
)


def series_list(L: Ledger, company: str) -> list[dict]:
    """A firm's series by code: next number, issued count, whether new invoices start in it, and why it cannot be
    removed (None when it can)."""
    _company(L, company)
    default = default_series(L, company)
    rows = L.db.execute(
        "SELECT s.code,s.next_number,(SELECT count(*) FROM invoices v WHERE v.company=s.company AND v.series=s.code "
        "AND v.status!='draft') AS issued FROM invoice_series s WHERE s.company=? ORDER BY s.code",
        (company,),
    ).fetchall()
    return [
        dict(code=r["code"], next_number=r["next_number"], issued=r["issued"], default=r["code"] == default)
        | dict(remove_refusal=_series_in_use(L.db, company, r["code"]))
        for r in rows
    ]


def default_series(L: Ledger, company: str) -> str | None:
    """The series a new invoice starts in: the one chosen for the firm while it exists, else the first by code."""
    codes = [r[0] for r in L.db.execute("SELECT code FROM invoice_series WHERE company=? ORDER BY code", (company,))]
    chosen = L.get_meta(_SERIES_DEFAULT + company)
    return chosen if chosen in codes else (codes[0] if codes else None)


def set_default_series(L: Ledger, company: str, code: str) -> None:
    """Choose the series new invoices start in. Numbers are not touched."""
    _company(L, company)
    with L.tx() as db:
        _require_series(db, company, code)
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (_SERIES_DEFAULT + company, code))


def remove_series(L: Ledger, company: str, code: str) -> None:
    """Remove a series nothing uses: no issued invoice, no draft, no recurring rule. The delete re-checks all three, so
    a draft saved in the meantime keeps its series."""
    _company(L, company)
    with L.tx() as db:
        _require_series(db, company, code)
        if refusal := _series_in_use(db, company, code):
            raise SeriesRefusal(refusal)
        guards = " ".join(f"AND NOT EXISTS ({sql})" for sql, _ in _SERIES_IN_USE)
        if not db.execute(
            f"DELETE FROM invoice_series WHERE company=? AND code=? {guards}",
            (company, code, *[company, code] * len(_SERIES_IN_USE)),
        ).rowcount:
            raise SeriesRefusal("Seria este folosită și rămâne.")
        db.execute("DELETE FROM meta WHERE key=? AND value=?", (_SERIES_DEFAULT + company, code))


def _require_series(db: sqlite3.Connection, company: str, code: object) -> None:
    if (
        not isinstance(code, str)
        or not db.execute("SELECT 1 FROM invoice_series WHERE company=? AND code=?", (company, code)).fetchone()
    ):
        raise InvoiceError("Seria lipsește.")


def _series_in_use(db: sqlite3.Connection, company: str, code: str) -> str | None:
    return next((why for sql, why in _SERIES_IN_USE if db.execute(sql, (company, code)).fetchone()), None)


def first_series(used: dict[str, dict], name: str) -> dict:
    """What a firm's first series proposes: the series its imported invoices used most, after their last number; else
    the initials of its name without the legal form ('Atelier Nord SRL' -> 'AN'), a one-word name's first three
    letters, from 1. Only a proposal: the user can change it before `create_series`."""
    if used:
        code = max(sorted(used), key=lambda c: used[c]["count"])
        return dict(code=code, next_number=used[code]["max"] + 1)
    words = [w for w in companies.name_words(name) if w not in companies.LEGAL_FORMS]
    code = "".join(w[0] for w in words)[:4] if len(words) > 1 else "".join(words)[:3]
    return dict(code=code if re.fullmatch("[A-Z][A-Z0-9]*", code) else "F", next_number=1)


_IMPORTED = "Facturile importate din seria {code} ajung la numărul {used}. Seria continuă de la cel puțin {next}."


class SeriesRefusal(InvoiceError):
    """A refusal about a series' numbers, built only from a checked series code and integers: shown as it is."""


def continue_series(L: Ledger, company: str, code: str, next_number: int) -> None:
    """Move a series fiscalm has issued nothing in past the numbers the imported invoices used (fiscalm.history): the
    only change a series' number ever takes. Refused once the series has an invoice issued in fiscalm, for a number
    the imports reach and for a lower one; the write re-checks the state it read."""
    _company(L, company)
    if (
        not isinstance(code, str)
        or not re.fullmatch("[A-Z][A-Z0-9-]{0,19}", code)
        or type(next_number) is not int
        or not 1 <= next_number < 10**12
    ):
        raise SeriesRefusal("Seria sau numărul nu are forma așteptată.")
    with L.tx() as db:
        row = db.execute(
            "SELECT next_number FROM invoice_series WHERE company=? AND code=?", (company, code)
        ).fetchone()
        if not row:
            raise SeriesRefusal("Seria lipsește.")
        used = _imported_max(L, company, code)
        if next_number <= used:
            raise SeriesRefusal(_IMPORTED.format(code=code, used=used, next=used + 1))
        if next_number <= row[0]:
            raise SeriesRefusal(f"Seria {code} continuă deja de la {row[0]}.")
        if not db.execute(
            "UPDATE invoice_series SET next_number=? WHERE company=? AND code=? AND next_number=? "
            "AND NOT EXISTS (SELECT 1 FROM invoices WHERE company=? AND series=? AND status!='draft')",
            (next_number, company, code, row[0], company, code),
        ).rowcount:
            raise SeriesRefusal(
                f"Seria {code} are deja facturi emise în fiscalm, iar numerotarea ei nu se mai schimbă."
            )


def _imported_max(L: Ledger, company: str, code: str) -> int:
    """The highest number of this series the firm used outside fiscalm (fiscalm.history.series_numbers), 0 if none."""
    from fiscalm import history

    return history.series_numbers(L, company).get(code, {}).get("max", 0)


def calculate(lines: list[dict], vat_payer: bool) -> tuple[list[dict], dict]:
    """Validate lines and return normalized lines and category VAT totals, without floats."""
    if not isinstance(lines, list) or not lines or any(not isinstance(row, dict) for row in lines):
        raise InvoiceError("Factura are nevoie de cel puțin o linie.")
    out, groups, gross_groups, modes = [], {}, {}, {}
    for line in lines:
        if not {"description", "quantity", "unit", "unit_price", "vat_category", "vat_rate"} <= line.keys():
            raise InvoiceError("Fiecare linie are nevoie de descriere, cantitate, unitate, preț și TVA.")
        row = _fields(
            {k: v for k, v in line.items() if k != "net"},
            set(
                [
                    "description",
                    "quantity",
                    "unit",
                    "unit_price",
                    "vat_category",
                    "vat_rate",
                    "exemption_reason",
                    "gross_unit_price",
                    "product_id",
                ]
            ),
        )
        qty, price, rate = (decimal(row.get(k, "0")) for k in ("quantity", "unit_price", "vat_rate"))
        cat, reason = row.get("vat_category"), row.get("exemption_reason", "")
        if "gross_unit_price" in row:
            gross_price = decimal(row["gross_unit_price"])
            if gross_price < 0 or rate < 0 or rate > 100:
                raise InvoiceError("Prețul cu TVA și cota TVA trebuie să fie pozitive.")
            price = (gross_price / (1 + rate / 100)).quantize(Decimal("0.000000000001"))
            row["unit_price"] = format(price, "f")
        if qty == 0 or price < 0:
            raise InvoiceError("Cantitatea trebuie să fie diferită de zero și prețul nu poate fi negativ.")
        if not row.get("description", "").strip() or not re.fullmatch("[A-Z0-9]{2,3}", row.get("unit", "")):
            raise InvoiceError("Fiecare linie are nevoie de descriere și unitate de măsură.")
        if (
            cat not in {"S", "Z", "E", "AE", "K", "G", "O"}
            or (cat == "S" and rate not in (21, 11))
            or (cat != "S" and rate != 0)
        ):
            raise InvoiceError(
                "Pentru categoria S, cota introdusă este 21% sau 11%; pentru Z/E/AE/K/G/O, se introduce 0."
            )
        if cat in {"E", "AE", "K", "G", "O"} and not reason.strip():
            raise InvoiceError("Categoria TVA necesită un motiv de scutire sau un cod VATEX.")
        # Fiscal Code art. 310 (ANAF source above): special exemption for non-payers.
        if vat_payer and cat == "O" and re.search(r"\b310\b", reason):
            raise InvoiceError("Un plătitor de TVA nu poate folosi scutirea conform art. 310.")
        if not vat_payer and (cat != "O" or "310" not in reason):
            raise InvoiceError("Un neplătitor de TVA trebuie să folosească O și scutirea conform art. 310.")
        if len(row.get("description", "")) > LINE_DESCRIPTION_MAX or len(reason) > 100:
            raise InvoiceError(LINE_DESCRIPTION_ERROR)
        rate = rate.normalize()
        net = money(qty * price)
        row.update(quantity=format(qty, "f"), unit_price=format(price, "f"), vat_rate=format(rate, "f"), net=net)
        out.append(row)
        key = (cat, format(rate, "f"))
        gross_mode = "gross_unit_price" in row
        if key in modes and modes[key] != gross_mode:
            raise InvoiceError("Toate liniile dintr-o categorie TVA trebuie să aibă același mod de preț.")
        modes[key] = gross_mode
        if gross_mode:
            gross_groups[key] = gross_groups.get(key, Decimal(0)) + Decimal(
                money(qty * decimal(row["gross_unit_price"]))
            )
        groups[key] = groups.get(key, Decimal(0)) + Decimal(net)
    if any(c == "O" for c, _ in groups) and len(groups) != 1:
        raise InvoiceError("Categoria TVA O nu poate fi combinată cu alte categorii.")
    if any(n < 0 for n in groups.values()):
        raise InvoiceError("Reducerile nu pot depăși baza categoriei TVA.")
    taxes = [
        dict(
            category=c,
            rate=r,
            net=money(n),
            vat=money(gross_groups[(c, r)] - n) if (c, r) in gross_groups else money(n * Decimal(r) / 100),
        )
        for (c, r), n in sorted(groups.items())
    ]
    if any(abs(Decimal(g["vat"]) - Decimal(money(Decimal(g["net"]) * Decimal(g["rate"]) / 100))) >= 1 for g in taxes):
        raise InvoiceError("Rotunjirea prețurilor cu TVA depășește limita admisă. Verifică liniile.")
    net = sum((Decimal(x["net"]) for x in out), Decimal(0))
    vat = sum((Decimal(x["vat"]) for x in taxes), Decimal(0))
    return out, dict(net=money(net), vat=money(vat), total=money(net + vat), taxes=taxes)


def chargeability_date(data: dict) -> str:
    """Ordinary tax point, also used for FX by cash-accounting sellers.

    Fiscal Code art. 281, 282(1)-(2), 290(2), Norms pt. 35(1)-(2):
    https://static.anaf.ro/static/10/Anaf/legislatie/Cod_fiscal_norme_2023.htm
    An advance receipt date covers this invoice's whole base; mixed advances need
    separate invoices. Category K follows art. 283, not the domestic advance rule.
    """
    issued = _day(data["issue_date"])
    delivery = _day(data["delivery_date"]) if data.get("delivery_date") else issued
    advance = _day(data["advance_payment_date"]) if data.get("advance_payment_date") else issued
    if any(isinstance(row, dict) and row.get("vat_category") == "K" for row in data.get("lines", [])):
        if data.get("advance_payment_date"):
            raise InvoiceError("Livrarea intracomunitară nu folosește data încasării avansului.")
        day = date.fromisoformat(delivery)
        limit = date(day.year + (day.month == 12), day.month % 12 + 1, 15).isoformat()
        tax_day = min(issued, limit)
        if any(row.get("vat_category") != "K" for row in data["lines"]) and tax_day != min(issued, delivery):
            raise InvoiceError("Operațiunile cu date de exigibilitate diferite necesită facturi separate.")
        return tax_day
    if data.get("advance_payment_date") and data.get("advance_invoice") is not True:
        raise InvoiceError("Data încasării avansului necesită o factură de avans.")
    if data.get("advance_payment_date") and data.get("delivery_date") and advance >= delivery:
        raise InvoiceError("Data încasării avansului trebuie să fie înaintea datei livrării.")
    if advance > issued:
        raise InvoiceError("Data încasării avansului nu poate fi după data emiterii.")
    return min(issued, delivery, advance)


def ron_totals(data: dict) -> dict:
    """Convert frozen VAT rather than independently rounding the RON base.

    Fiscal Code art. 319(23), Norms pt. 97(6), art. 290 (ANAF source above).
    BR-53 requires BT-111; BR-CO-17 concerns invoice-currency subtotals:
    https://docs.peppol.eu/poacc/billing/3.0/rules/ubl-tc434/BR-53/
    """
    rate = decimal(data["exchange_rate"]) if data["currency"] != "RON" else Decimal(1)
    result = {key: money(Decimal(data["totals"][key]) * rate) for key in ("net", "vat")}
    result["total"] = money(Decimal(result["net"]) + Decimal(result["vat"]))
    return result


def _payload(data: dict, seller: dict) -> tuple[dict, list[dict]]:
    data = dict(data)
    if any(k not in data for k in ("issue_date", "due_date", "currency", "lines")):
        raise InvoiceError("Lipsesc data emiterii, scadența, moneda sau liniile facturii.")
    _day(data["issue_date"])
    if _day(data["due_date"]) < data["issue_date"]:
        raise InvoiceError("Scadența nu poate fi înaintea datei emiterii.")
    if not re.fullmatch("[A-Z]{3}", data.get("currency", "")):
        raise InvoiceError("Moneda trebuie scrisă prin codul de trei litere.")
    if "supply_country" in data and data["supply_country"] not in COUNTRIES:
        raise InvoiceError("Țara trebuie scrisă prin codul de două litere.")
    tax_date = chargeability_date(data)
    if data["currency"] != "RON":
        if decimal(data.get("exchange_rate", "0")) <= 0 or _day(data.get("exchange_date", "")) >= tax_date:
            raise InvoiceError("Cursul trebuie să fie pozitiv și publicat înainte de data exigibilității.")
        data["exchange_rate"] = str(decimal(data["exchange_rate"]))
    lines, totals = calculate(data.pop("lines"), seller["vat_payer"])
    if len(data.get("notes", "")) > 300:
        raise InvoiceError("Nota facturii poate avea cel mult 300 de caractere.")
    if data.get("delivery_date"):
        _day(data["delivery_date"])
    if any(row["vat_category"] == "K" for row in lines) and not data.get("delivery_date"):
        raise InvoiceError("Categoria K necesită data livrării.")
    data["chargeability_date"] = tax_date
    # Art. 319(20)(p), art. 282(3)-(8), consolidated ANAF source above.
    categories = {row["vat_category"] for row in lines}
    collection = seller.get("vat_on_collection", False) and "S" in categories
    if collection and categories != {"S"}:
        raise InvoiceError("TVA la încasare cu tratamente diferite necesită facturi separate.")
    data.setdefault("advance_invoice", False)
    data["mentions"] = ["TVA la încasare"] if collection else []
    data["warnings"] = []
    if data.get("advance_payment_date") and not data.get("billing_ref"):
        receipt = date.fromisoformat(data["advance_payment_date"])
        deadline = date(receipt.year + (receipt.month == 12), receipt.month % 12 + 1, 15).isoformat()
        if data["issue_date"] > deadline:
            data["warnings"] = ["Factura de avans este emisă după termenul de la art. 319 alin. (16)."]
    if data.get("supply_country", "RO") not in COUNTRIES:
        raise InvoiceError("Țara trebuie scrisă prin codul de două litere.")
    if data.get("supply_country", "RO") != "RO" and any(row["vat_category"] in {"K", "G"} for row in lines):
        raise InvoiceError(
            "Pentru categoriile K și G, locul livrării trebuie să fie România (Codul fiscal, art. 275 alin. (1) lit. a)."
        )
    data["totals"] = totals
    return data, lines


def _client_treatment(client: dict, lines: list[dict]) -> None:
    if (
        any(row["vat_category"] == "O" and row.get("exemption_reason") == NON_EU_SERVICES_REASON for row in lines)
    ) and (
        client.get("private")
        or client["country"] in EU_COUNTRIES
        or client.get("vat_id", "")[:2] in EU_COUNTRIES - {"GR"} | {"EL", "XI"}
    ):
        raise InvoiceError("Art. 278 alin. (2) necesită un client persoană impozabilă din afara UE.")
    if any(row["vat_category"] == "K" for row in lines) and client.get("vat_id", "")[:2] not in (
        EU_COUNTRIES - {"RO", "GR"}
    ) | {"EL", "XI"}:
        raise InvoiceError("Livrarea intracomunitară necesită un client din UE sau GB cu cod XI pentru bunuri.")


def _seller(L: Ledger, company: str) -> dict:
    row = L.db.execute("SELECT data FROM invoice_sellers WHERE company=?", (company,)).fetchone()
    if row:
        return json.loads(row["data"])
    return _party(seller_account(L, company, company_config.known_facts(L, company)), True)


class DraftConflict(InvoiceError):
    """A draft precondition failed; adapters preserve the machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


VERSION_REQUIRED = "Versiunea ciornei este obligatorie."
INVALID_VERSION = "Versiunea ciornei trebuie să fie un număr întreg pozitiv."
MISSING_DRAFT = "Poate fi modificată doar o ciornă existentă a acestei firme."


def _version(version: int | None) -> None:
    if version is None:
        raise DraftConflict("version_required", VERSION_REQUIRED)
    if type(version) is not int or version < 1:
        raise DraftConflict("invalid_version", INVALID_VERSION)


def _draft_version(L: Ledger, iid: str, company: str, version: int | None) -> None:
    row = L.db.execute(
        "SELECT version FROM invoices WHERE id=? AND company=? AND status='draft'", (iid, company)
    ).fetchone()
    if not row:
        raise InvoiceError(MISSING_DRAFT)
    _version(version)
    if row["version"] != version:
        raise DraftConflict("stale_draft", STALE_DRAFT)


STALE_DRAFT = "Ciorna a fost modificată. Trebuie reîncărcată înainte de a continua."


def save_draft(
    L: Ledger,
    company: str,
    series: str,
    client_id: str,
    data: dict,
    draft_id: str | None = None,
    *,
    version: int | None = None,
    recurrence_id: str | None = None,
) -> str:
    """Create or fully replace an unnumbered draft, refusing stale or cross-company edits."""
    _company(L, company)
    _identifier(series)
    _identifier(client_id)
    _identifier(draft_id, optional=True)
    if not isinstance(data, dict) or "lines" not in data:
        raise InvoiceError("Lipsesc liniile facturii.")
    if isinstance(data["lines"], list) and any(isinstance(row, dict) and "net" in row for row in data["lines"]):
        raise InvoiceError("Sumele nete sunt calculate automat.")
    _fields(
        {k: v for k, v in data.items() if k != "lines"},
        set(
            [
                "issue_date",
                "due_date",
                "currency",
                "notes",
                "exchange_rate",
                "exchange_date",
                "delivery_date",
                "supply_country",
                "advance_payment_date",
                "iban",
            ]
        ),
        {"advance_invoice"},
    )
    with L.tx() as db:
        if draft_id:
            if db.execute("SELECT 1 FROM invoice_refund_drafts WHERE draft_id=?", (draft_id,)).fetchone():
                raise InvoiceError("Ciorna de stornare nu poate fi modificată. Se șterge și se creează alta.")
            _draft_version(L, draft_id, company, version)
        # Acquire the write lock before reading state, including across connections.
        db.execute("UPDATE invoice_series SET next_number=next_number WHERE company=? AND code=?", (company, series))
        if not db.execute("SELECT 1 FROM invoice_series WHERE company=? AND code=?", (company, series)).fetchone():
            raise InvoiceError("Mai întâi trebuie creată seria facturii.")
        client = db.execute(
            "SELECT data FROM invoice_clients WHERE id=? AND company=?", (client_id, company)
        ).fetchone()
        if not client:
            raise InvoiceError("Clientul trebuie să fie al acestei firme.")
        payload, lines = _payload(data, _seller(L, company))
        if "iban" in data:  # the chosen account, stored as the company configures it
            payload["iban"] = issuing_account(L, company, _seller(L, company), data["iban"])["iban"]
        payload["client"] = _party(json.loads(client["data"]))
        validate_text([payload, lines, _seller(L, company)])
        _client_treatment(payload["client"], lines)
        if (
            any(row["vat_category"] in {"K", "G"} for row in lines)
            and payload["client"]["country"] == _seller(L, company)["country"]
        ):
            raise InvoiceError("Categoriile K și G necesită un client din altă țară.")
        # Art. 278(2) general B2B place-of-supply rule; exceptions require another treatment.
        if (
            any(row.get("exemption_reason") == NON_EU_SERVICES_REASON and row["vat_category"] == "O" for row in lines)
        ) and (payload["client"].get("private") or payload["client"]["country"] in EU_COUNTRIES):
            raise InvoiceError("Art. 278 alin. (2) necesită un client persoană impozabilă din afara UE.")
        iid = draft_id or uuid4().hex
        if draft_id:
            changed = L.replace_invoice_draft(iid, company, series, json.dumps(payload), version)
            if not changed:
                raise DraftConflict("stale_draft", STALE_DRAFT)
        else:
            db.execute(
                "INSERT INTO invoices(id,company,series,status,data) VALUES(?,?,?,'draft',?)",
                (iid, company, series, json.dumps(payload)),
            )
        if not L.reference_invoice_products(
            company,
            iid,
            {r["product_id"] for r in lines if "product_id" in r},
            recurrence_id=recurrence_id,
            client_id=client_id,
        ):
            raise InvoiceError("Produsul nu mai este disponibil sau aparține altei firme.")
        if draft_id:
            db.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (iid,))
        db.executemany(
            "INSERT INTO invoice_lines VALUES(?,?,?)", [(iid, n, json.dumps(row)) for n, row in enumerate(lines, 1)]
        )
        from fiscalm.ledger import now

        for key, value in [
            ("draft_client:" + iid, client_id),
            ("draft_updated:" + iid, now()),
            ("client_used:" + client_id, now()),
        ]:
            db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    return iid


def invoice(L: Ledger, invoice_id: str, *, company: str) -> dict:
    """Read an invoice and its immutable snapshots (or its current draft)."""
    _identifier(invoice_id)
    row = L.db.execute("SELECT * FROM invoices WHERE id=? AND company=?", (invoice_id, company)).fetchone()
    if not row:
        raise InvoiceError("Factura nu a fost găsită.")
    lines = L.db.execute(
        "SELECT data FROM invoice_lines WHERE invoice_id=? ORDER BY position", (invoice_id,)
    ).fetchall()
    return _invoice_record(row, lines)


def _invoice_record(row, lines) -> dict:
    _identifier(row["id"])
    return dict(row) | {"data": json.loads(row["data"]), "lines": [json.loads(r["data"]) for r in lines]}


def issued_records(L: Ledger, company: str) -> list[dict]:
    """The same authored invoice snapshots as invoice(), read together for the issued projection."""
    lines: dict = {}
    for row in L.db.execute(
        "SELECT l.* FROM invoice_lines l JOIN invoices i ON i.id=l.invoice_id "
        "WHERE i.company=? AND i.status!='draft' ORDER BY l.position",
        (company,),
    ):
        lines.setdefault(row["invoice_id"], []).append(row)
    return [
        _invoice_record(row, lines.get(row["id"], []))
        for row in L.db.execute("SELECT * FROM invoices WHERE company=? AND status!='draft'", (company,))
    ]


def delete_draft(L: Ledger, draft_id: str, *, company: str, version: int | None = None) -> None:
    """Delete a draft only; issued numbers and contents are never deleted."""
    _identifier(draft_id)
    with L.tx() as db:
        if version is not None:
            _draft_version(L, draft_id, company, version)
        if not L.delete_invoice_draft(draft_id, company, version):
            raise InvoiceError("Poate fi ștearsă doar o ciornă.")
        db.execute("DELETE FROM invoice_refund_drafts WHERE draft_id=?", (draft_id,))
        db.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (draft_id,))
        db.execute("DELETE FROM meta WHERE key IN (?,?)", ("draft_client:" + draft_id, "draft_updated:" + draft_id))


def issue(
    L: Ledger,
    draft_id: str,
    today: str,
    *,
    company: str,
    version: int | None = None,
    authorize: Callable[[], None] | None = None,
    selected: tuple[str, str] | None = None,
) -> dict:
    """Reserve the next number only for the caller's version; rollback consumes no number. `selected`: a BNR rate the
    caller already verified (rate, publication), rechecked under the lock without fetching.

    The WHERE version guard is defence in depth under BEGIN IMMEDIATE, which already
    prevents another connection from changing the validated draft before issuance.
    """
    _day(today)
    _identifier(draft_id)
    refund = L.db.execute(
        "SELECT original_id,positions FROM invoice_refund_drafts WHERE draft_id=? AND company=?", (draft_id, company)
    ).fetchone()
    if refund:
        saved = invoice(L, draft_id, company=company)
        return storno(
            L,
            refund["original_id"],
            today,
            saved["data"]["notes"],
            json.loads(refund["positions"]),
            company=company,
            draft_id=draft_id,
            version=version,
            authorize=authorize,
        )
    from fiscalm.invoicing.branding import UNAVAILABLE, current_sha, issue_sha

    logo_sha = issue_sha(L, company)
    from fiscalm.bnr import check_invoice_rate

    before = invoice(L, draft_id, company=company)
    if selected is None:
        selected = check_invoice_rate(L, before["data"] | {"lines": before["lines"]}, today)
    with L.tx() as db:
        if authorize:
            authorize()
        if current_sha(L, company) != logo_sha:
            raise InvoiceError("Factura nu a fost emisă. " + UNAVAILABLE)
        db.execute("UPDATE invoices SET status=status WHERE id=? AND company=? AND status='draft'", (draft_id, company))
        inv = invoice(L, draft_id, company=company)
        if inv != before:
            raise InvoiceError("Ciorna a fost modificată. Încearcă din nou să emiți factura.")
        if inv["status"] != "draft":
            raise InvoiceError("Poate fi emisă doar o ciornă.")
        _version(version)
        if version != inv["version"]:
            raise DraftConflict("stale_draft", STALE_DRAFT)
        client_id = L.get_meta("draft_client:" + draft_id)
        saved = db.execute("SELECT data FROM invoice_clients WHERE id=? AND company=?", (client_id, company)).fetchone()
        if not saved:
            raise InvoiceError("Clientul trebuie să fie al acestei firme.")
        if _party(json.loads(saved["data"])) != _party(inv["data"]["client"]):
            raise InvoiceError("Clientul a fost modificat. Salvează din nou ciorna înainte să emiți factura.")
        inv["data"]["client"] = _party(inv["data"]["client"])
        _active_client(L, inv["data"]["client"])
        _client_treatment(inv["data"]["client"], inv["lines"])
        seller = draft_seller(L, inv)
        data, normalized_lines = _payload(inv["data"] | {"lines": inv["lines"]}, seller)
        if data["issue_date"] != today:
            raise InvoiceError("Data emiterii trebuie să fie data de astăzi.")
        from fiscalm.ledger import is_live

        check_invoice_rate(L, data | {"lines": normalized_lines}, today, selected=selected)
        data["issued_live"] = is_live()
        data["seller"] = seller
        data["logo_sha"] = logo_sha
        validate_text([data, inv["lines"]])
        _series_date(L, company, inv["series"], today)
        row = db.execute(
            "SELECT next_number FROM invoice_series WHERE company=? AND code=?", (inv["company"], inv["series"])
        ).fetchone()
        if not row or row[0] >= 10**12:
            raise InvoiceError("Seria lipsește sau nu mai are numere disponibile.")
        number = row[0]
        from fiscalm.invoicing.ubl import build

        build(inv | dict(status="issued", number=number, data=data, lines=normalized_lines))
        if not L.issue_invoice_draft(draft_id, company, number, json.dumps(data), inv["version"]):
            raise InvoiceError("Ciorna a fost deja emisă.")
        if not db.execute(
            "UPDATE invoice_series SET next_number=next_number+1 WHERE company=? AND code=? AND next_number=?",
            (inv["company"], inv["series"], number),
        ).rowcount:
            raise InvoiceError("Seria a fost modificată. Încearcă din nou să emiți factura.")
        _record_local(L, company, draft_id, build(invoice(L, draft_id, company=company)))
        from fiscalm.invoicing.spv import record_auto_upload_policy

        record_auto_upload_policy(L, company, draft_id)
        from fiscalm.commerce.webhooks import capture

        capture(L, invoice_id=draft_id)
    return invoice(L, draft_id, company=company)


def storno(
    L: Ledger,
    invoice_id: str,
    today: str,
    reason: str,
    lines: list[int] | None = None,
    *,
    company: str,
    draft: bool = False,
    draft_id: str | None = None,
    version: int | None = None,
    authorize: Callable[[], None] | None = None,
) -> dict:
    """Issue negative-quantity type-380 storno for selected whole lines (1-based positions).

    ANAF explicitly permits negative 380 invoices as an alternative to positive 381 credit notes:
    https://static.anaf.ro/static/10/Anaf/Informatii_R/Ghid%20cod%20facturi_final%20v2.9.pdf
    Original tax treatment, exchange rate and parties are preserved. An original line can be
    reversed once, atomically with numbering. No storno-of-storno or edited original is allowed.
    """
    _day(today)
    if not isinstance(reason, str) or not reason.strip():
        raise InvoiceError("Lipsește motivul stornării.")
    _fields({"notes": reason}, {"notes"})
    if len(reason) > 300:
        raise InvoiceError("Nota facturii poate avea cel mult 300 de caractere.")
    _identifier(invoice_id)
    from fiscalm.invoicing.branding import UNAVAILABLE, current_sha, issue_sha

    logo_sha = issue_sha(L, company, storno=True)
    with L.tx() as db:
        if authorize:
            authorize()
        if draft_id:
            _draft_version(L, draft_id, company, version)
            claim = db.execute(
                "SELECT * FROM invoice_refund_drafts WHERE draft_id=? AND company=?", (draft_id, company)
            ).fetchone()
            if not claim or claim["original_id"] != invoice_id or json.loads(claim["positions"]) != lines:
                raise InvoiceError("Ciorna de stornare nu corespunde facturii inițiale.")
        if current_sha(L, company) != logo_sha:
            raise InvoiceError("Factura de stornare nu a fost emisă. " + UNAVAILABLE)
        original = _storno_original(L, invoice_id, company, today)
        positions, previous = _storno_positions(L, original, lines, draft, draft_id)
        chosen = [dict(original["lines"][n - 1]) for n in positions]
        groups, taxes = _storno_taxes(L, original, chosen, positions, previous, company)
        data = _storno_data(original, chosen, groups, taxes, today, reason)
        data["issued_live"] = ledger.is_live()
        data["logo_sha"] = logo_sha
        validate_text([data, chosen])
        if draft:
            return _save_storno_draft(L, original, positions, data, chosen, company)
        iid = _issue_storno(L, original, positions, previous, data, chosen, today, company, draft_id, version)
    return invoice(L, iid, company=company)


def _storno_original(L: Ledger, invoice_id: str, company: str, today: str) -> dict:
    L.db.execute(
        "UPDATE invoices SET status=status WHERE id=? AND company=? AND status='issued'", (invoice_id, company)
    )
    original = invoice(L, invoice_id, company=company)
    if original["status"] != "issued" or original["data"].get("billing_ref"):
        raise InvoiceError("Poți storna doar o factură inițială emisă.")
    from fiscalm.invoicing.spv import scope, submission

    state = submission(L, invoice_id)
    if (state and state["status"] != "accepted") or (not state and ledger.is_live() and scope(original) == "required"):
        raise InvoiceError(
            "Așteaptă ca ANAF să accepte factura înainte să o stornezi. Corectează facturile respinse și trimite-le din nou."
        )
    if today < original["data"]["issue_date"]:
        raise InvoiceError("Data stornării nu poate fi înaintea datei facturii inițiale.")
    return original


def _storno_positions(
    L: Ledger, original: dict, lines: list[int] | None, draft: bool, draft_id: str | None
) -> tuple[list[int], set[int]]:
    positions = lines if lines is not None else list(range(1, len(original["lines"]) + 1))
    if (
        not positions
        or any(type(n) is not int or not 1 <= n <= len(original["lines"]) for n in positions)
        or len(set(positions)) != len(positions)
    ):
        raise InvoiceError("Liniile alese trebuie să fie diferite și să existe pe factură.")
    previous = {
        r[0] for r in L.db.execute("SELECT position FROM invoice_reversals WHERE original_id=?", (original["id"],))
    }
    if previous.intersection(positions):
        raise InvoiceError("Unele dintre liniile alese au fost deja stornate.")
    if not draft:
        for pending in L.db.execute(
            "SELECT draft_id,positions FROM invoice_refund_drafts WHERE original_id=?", (original["id"],)
        ):
            if pending["draft_id"] != draft_id and set(json.loads(pending["positions"])).intersection(positions):
                raise InvoiceError("Există deja o ciornă de stornare pentru liniile alese.")
    _storno_discounts(original, positions, previous)
    return positions, previous


def _storno_discounts(original: dict, positions: list[int], previous: set[int]) -> None:
    # Shop-wide discounts have no trusted per-item allocation. Refuse to guess it.
    for n in positions:
        line = original["lines"][n - 1]
        category = {
            pos
            for pos, row in enumerate(original["lines"], 1)
            if (row["vat_category"], row["vat_rate"]) == (line["vat_category"], line["vat_rate"])
            and pos not in previous
        }
        if any(Decimal(original["lines"][pos - 1]["net"]) < 0 for pos in category) and not category <= set(positions):
            raise InvoiceError("Returul trebuie să cuprindă toate liniile și reducerile din aceeași categorie TVA.")


def _storno_taxes(
    L: Ledger, original: dict, chosen: list[dict], positions: list[int], previous: set[int], company: str
) -> tuple[dict[tuple[str, str], Decimal], list[dict]]:
    groups = {}
    for row in chosen:
        key = (row["vat_category"], row["vat_rate"])
        groups[key] = groups.get(key, Decimal(0)) + Decimal(row["net"])
    taxes = []
    prior_ids = {
        r[0]
        for r in L.db.execute("SELECT DISTINCT storno_id FROM invoice_reversals WHERE original_id=?", (original["id"],))
    }
    for (cat, rate), net in sorted(groups.items()):
        remaining = [
            n
            for n, row in enumerate(original["lines"], 1)
            if row["vat_category"] == cat and row["vat_rate"] == rate and n not in previous
        ]
        available_net = sum(
            (Decimal(row["net"]) for n, row in enumerate(original["lines"], 1) if n in remaining), Decimal(0)
        )
        if net <= 0 or available_net - net < 0:
            raise InvoiceError("Produsul se alege împreună cu reducerea sa; returul nu poate depăși suma rămasă.")
        vat = _storno_group_vat(L, original, chosen, cat, rate, net, remaining, positions, prior_ids, company)
        taxes.append(dict(category=cat, rate=rate, net=money(net), vat=money(vat)))
    return groups, taxes


def _storno_group_vat(
    L: Ledger,
    original: dict,
    chosen: list[dict],
    cat: str,
    rate: str,
    net: Decimal,
    remaining: list[int],
    positions: list[int],
    prior_ids: set[str],
    company: str,
) -> Decimal:
    gross_lines = [row for row in chosen if row["vat_category"] == cat and row["vat_rate"] == rate]
    vat = (
        sum(
            (Decimal(money(Decimal(row["quantity"]) * Decimal(row["gross_unit_price"]))) for row in gross_lines),
            Decimal(0),
        )
        - net
        if all("gross_unit_price" in row for row in gross_lines)
        else Decimal(money(net * Decimal(rate) / 100))
    )
    available = next(
        Decimal(g["vat"]) for g in original["data"]["totals"]["taxes"] if g["category"] == cat and g["rate"] == rate
    )
    for sid in prior_ids:
        available += sum(
            (
                Decimal(g["vat"])
                for g in invoice(L, sid, company=company)["data"]["totals"]["taxes"]
                if g["category"] == cat and g["rate"] == rate
            ),
            Decimal(0),
        )
    vat = available if set(remaining) <= set(positions) else min(vat, available)
    return vat


def _storno_data(original: dict, chosen: list[dict], groups: dict, taxes: list[dict], today: str, reason: str) -> dict:
    net = sum(groups.values(), Decimal(0))
    vat = sum((Decimal(g["vat"]) for g in taxes), Decimal(0))
    totals = dict(net=money(net), vat=money(vat), total=money(net + vat), taxes=taxes)
    for row in chosen:
        row["quantity"] = format(-Decimal(row["quantity"]), "f")
        row["net"] = money(-Decimal(row["net"]))
    for field in ["net", "vat", "total"]:
        totals[field] = money(-Decimal(totals[field]))
    for group in totals["taxes"]:
        for field in ["net", "vat"]:
            group[field] = money(-Decimal(group[field]))
    data = original["data"] | dict(
        issue_date=today,
        due_date=today,
        notes=reason.strip(),
        totals=totals,
        billing_ref=invoice_number(original),
        original_issue_date=original["data"]["issue_date"],
    )
    return data


def _save_storno_draft(
    L: Ledger, original: dict, positions: list[int], data: dict, chosen: list[dict], company: str
) -> dict:
    pending = L.db.execute(
        "SELECT draft_id,positions FROM invoice_refund_drafts WHERE original_id=? AND company=?",
        (original["id"], company),
    ).fetchall()
    for row in pending:
        if set(json.loads(row["positions"])) == set(positions):
            return invoice(L, row["draft_id"], company=company)
        if set(json.loads(row["positions"])).intersection(positions):
            raise InvoiceError("Există deja o ciornă de stornare pentru liniile alese.")
    iid = uuid4().hex
    L.db.execute(
        "INSERT INTO invoices(id,company,series,status,data) VALUES(?,?,?,'draft',?)",
        (iid, company, original["series"], json.dumps(data)),
    )
    L.db.executemany(
        "INSERT INTO invoice_lines VALUES(?,?,?)",
        [(iid, n, json.dumps(row)) for n, row in enumerate(chosen, 1)],
    )
    L.db.execute(
        "INSERT INTO invoice_refund_drafts VALUES(?,?,?,?)", (iid, company, original["id"], json.dumps(positions))
    )
    client_id = L.get_meta("draft_client:" + original["id"])
    if client_id:
        L.db.execute("INSERT INTO meta VALUES(?,?)", ("draft_client:" + iid, client_id))
    return invoice(L, iid, company=company)


def _issue_storno(
    L: Ledger,
    original: dict,
    positions: list[int],
    previous: set[int],
    data: dict,
    chosen: list[dict],
    today: str,
    company: str,
    draft_id: str | None,
    version: int | None,
) -> str:
    invoice_id = original["id"]
    _series_date(L, company, original["series"], today)
    series = L.db.execute(
        "SELECT next_number FROM invoice_series WHERE company=? AND code=?",
        (original["company"], original["series"]),
    ).fetchone()
    if not series or series[0] >= 10**12:
        raise InvoiceError("Seria lipsește sau nu mai are numere disponibile.")
    from fiscalm.invoicing.ubl import build

    build(dict(status="issued", series=original["series"], number=series[0], data=data, lines=chosen))
    iid = draft_id or uuid4().hex
    if draft_id:
        if not L.delete_invoice_draft(draft_id, company, version):
            raise DraftConflict("stale_draft", STALE_DRAFT)
        L.db.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (draft_id,))
        L.db.execute("DELETE FROM invoice_refund_drafts WHERE draft_id=?", (draft_id,))
    L.db.execute(
        "INSERT INTO invoices(id,company,series,number,status,data) VALUES(?,?,?,?,'issued',?)",
        (iid, original["company"], original["series"], series[0], json.dumps(data)),
    )
    L.db.executemany(
        "INSERT INTO invoice_lines VALUES(?,?,?)", [(iid, n, json.dumps(row)) for n, row in enumerate(chosen, 1)]
    )
    L.db.executemany("INSERT INTO invoice_reversals VALUES(?,?,?)", [(invoice_id, n, iid) for n in positions])
    if not L.db.execute(
        "UPDATE invoice_series SET next_number=next_number+1 WHERE company=? AND code=? AND next_number=?",
        (original["company"], original["series"], series[0]),
    ).rowcount:
        raise InvoiceError("Seria a fost modificată. Încearcă din nou să emiți factura.")
    if len(previous | set(positions)) == len(original["lines"]):
        L.db.execute(
            "UPDATE invoices SET status='storno' WHERE id=? AND company=? AND status='issued'",
            (invoice_id, company),
        )
    _record_local(L, company, iid, build(invoice(L, iid, company=company)))
    from fiscalm.invoicing.spv import record_auto_upload_policy

    record_auto_upload_policy(L, company, iid)
    from fiscalm.commerce.webhooks import capture

    capture(L, invoice_id=iid)
    return iid


def upload(L: Ledger, invoice_id: str, *, company: str, authorize: Callable[[], None] | None = None) -> dict:
    """Submit an issued invoice through the live-only, idempotent SPV workflow."""
    from fiscalm.invoicing.spv import upload as submit

    return submit(L, invoice_id, company=company, authorize=authorize)


def poll(L: Ledger) -> list[dict]:
    """Poll pending SPV submissions and store their signed responses in live mode only."""
    from fiscalm.invoicing.spv import poll as check

    return check(L)


def invoice_number(inv: dict) -> str:
    """Unambiguous public series-number identifier."""
    if inv["number"] is None:
        raise InvoiceError("Ciorna nu are număr de factură. Numărul apare la emitere.")
    return f"{inv['series']}-{inv['number']}"


def already_issued(db, company: str, inv: Invoice, sha: str) -> bool:
    """The same issued invoice is already stored: the same XML, or the firm's same number, date and kind."""
    kind, _ = kind_total(inv)
    return (
        db.execute(
            "SELECT 1 FROM issued WHERE xml_sha=? OR (company=? AND number=? AND issue_date=? AND doc_type=?)",
            (sha, company, inv.number, inv.issue_date, kind),
        ).fetchone()
        is not None
    )


def record_issued(L: Ledger, company: str, row: dict, inv: Invoice, sha: str) -> str:
    """Insert one `issued` row parsed from an invoice XML, under the caller's write lock; return its status.
    The same invoice already stored (`already_issued`) becomes a `duplicate`, never a second record."""
    issues = list(inv.issues)
    if inv.supplier_cui and inv.supplier_cui != companies.get(company, L=L).cui:
        issues.append(f"{NOT_OURS} {inv.supplier_cui} is not {company}")
    kind, total = kind_total(inv)
    parsed = row | {
        "doc_type": kind,
        "number": inv.number,
        "issue_date": inv.issue_date,
        "due_date": inv.due_date,
        "currency": inv.currency,
        "total": f"{total:.2f}",
        "client_name": inv.buyer_name,
        "party_address": json.dumps(inv.buyer_address),
        "client_cui": inv.buyer_cui,
        "client_tax_id": inv.buyer_tax_id,
        "billing_ref": inv.billing_ref,
        "xml_sha": sha,
        "issues": json.dumps(issues),
        "created_at": ledger.now(),
    }
    duplicate = already_issued(L.db, company, inv, sha)
    parsed["status"] = "duplicate" if duplicate else "credit" if kind == "credit_note" else "open"
    cols = ",".join(parsed)
    L.db.execute(f"INSERT OR IGNORE INTO issued({cols}) VALUES({','.join('?' * len(parsed))})", list(parsed.values()))
    return parsed["status"]


def import_id(way: str, key: str) -> str:
    """The id of an `issued`/`efactura` row no ANAF message brought (an imported invoice, or fiscalm's own issued
    one): 30 digits, numeric like ANAF's message ids (about 10 digits today), so it is never one ANAF lists, and
    the same for the same source (an import is idempotent)."""
    n = int(hashlib.sha256(f"{way}:{key}".encode()).hexdigest()[:24], 16)
    return "9" + str(n).zfill(29)


def record_own(L: Ledger, company: str, invoice_id: str, inv: Invoice, sha: str | None) -> bool:
    """Under the caller's write lock: the invoice's collectable `issued` row, as `issued.store` would parse its XML,
    linked to the invoice. Writes nothing and returns False when the same invoice is already stored."""
    if already_issued(L.db, company, inv, sha):
        return False
    mid = import_id("local", invoice_id)
    row = dict(message_id=mid, company=company, upload_id=LOCAL, listed_at=inv.issue_date.replace("-", ""), raw_path="")
    record_issued(L, company, row, inv, sha)
    L.db.execute("INSERT INTO invoice_sent_links VALUES(?,?,?)", (mid, invoice_id, anaf.inbox_env()))
    return True


def _record_local(L: Ledger, company: str, invoice_id: str, xml: bytes) -> None:
    """At issue: a number already stored as issued refuses the whole issue."""
    inv = parse_ubl(xml)
    if not record_own(L, company, invoice_id, inv, hashlib.sha256(xml).hexdigest()):
        raise InvoiceError(f"Factura {inv.number} din {inv.issue_date} există deja printre facturile emise.")


def link_sent(L: Ledger, company: str, message_id: str, seller_cui: str, number: str, day: str) -> None:
    """Link one unique immutable local identity to a parsed sent message, never by amount alone."""
    sent = L.db.execute("SELECT * FROM issued WHERE message_id=? AND company=?", (message_id, company)).fetchone()
    if not sent or sent["parse_error"]:
        return
    matches = []
    for row in L.db.execute("SELECT * FROM invoices WHERE company=? AND status!='draft'", (company,)):
        data = json.loads(row["data"])
        if (data["seller"]["cui"], invoice_number(dict(row)), data["issue_date"]) == (seller_cui, number, day):
            client = data["client"]
            tax_id = tax_token(
                (client.get("vat_id") if data["seller"]["vat_payer"] else "")
                or client.get("cui")
                or client.get("vat_id")
                or "0000000000000"
            )
            current = L.db.execute(
                "SELECT xml_sha FROM invoice_submissions WHERE invoice_id=? AND environment=?",
                (row["id"], anaf.inbox_env()),
            ).fetchone()
            if (
                decimal(sent["total"]) != abs(decimal(data["totals"]["total"]))
                or sent["currency"] != data["currency"]
                or sent["client_tax_id"] != tax_id
                or (current and current["xml_sha"] != sent["xml_sha"])
            ):
                continue
            matches.append(row["id"])
    if len(matches) == 1:
        with L.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO invoice_sent_links VALUES(?,?,?)", (message_id, matches[0], anaf.inbox_env())
            )
            db.execute(
                "UPDATE invoice_submissions SET status='accepted',errors='[]',download_id=? WHERE invoice_id=? AND environment=?",
                (message_id, matches[0], anaf.inbox_env()),
            )
            db.execute("DELETE FROM meta WHERE key=?", ("invoice_upload_error:" + matches[0],))


def sent_links(L: Ledger, company: str) -> dict[str, str]:
    """{collectable `issued` row: its invoice}, one per invoice: the row fiscalm wrote when issuing it (or, for an
    invoice issued before that, its SPV copy). An SPV copy stored as `duplicate` is evidence, never listed here."""
    return L.sent_invoice_links(company, anaf.inbox_env())


def _series_date(L: Ledger, company: str, series: str, today: str) -> None:
    """Called under the issuing write lock; numbering must follow issue dates and never reuse a number the imported
    invoices used (fiscalm.history)."""
    dates = [
        json.loads(r[0])["issue_date"]
        for r in L.db.execute(
            "SELECT data FROM invoices WHERE company=? AND series=? AND status!='draft'", (company, series)
        )
    ]
    if dates and today < max(dates):
        raise InvoiceError("Data emiterii nu poate fi înaintea ultimei facturi din serie.")
    row = L.db.execute(
        "SELECT next_number FROM invoice_series WHERE company=? AND code=?", (company, series)
    ).fetchone()
    used = _imported_max(L, company, series)
    if row and row[0] <= used:  # a number the old program already used: never twice
        raise SeriesRefusal(
            f"Numărul {row[0]} din seria {series} a fost deja folosit în facturile importate. "
            f"Seria se continuă de la {used + 1}, apoi factura se emite din nou."
        )
