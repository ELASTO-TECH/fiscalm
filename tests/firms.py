"""Synthetic company builders used by tests and the app scenario."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict

from fiscalm import companies
from fiscalm.ledger import Ledger


def seed(L: Ledger) -> None:
    for key, name, cui, suffix, currencies in (
        ("NORD", "Atelier Nord", "11111111", "0001", ("RON", "EUR")),
        ("STUDIO9", "Studio 9", "33333333", "0002", ("RON",)),
    ):
        L.save_company(
            companies.Company(
                key,
                name + " SRL",
                cui,
                tuple(
                    companies.Account(iban("RO00BTRL" + c + "CRT000000" + suffix), c, "bt", c == "RON")
                    for c in currencies
                ),
                key,
                companies.PALETTES[0 if key == "NORD" else 1],
                (name,),
            )
        )


def entries() -> dict:
    result = []
    for c in companies.all().values():
        entry = asdict(c)
        entry["accounts"] = [a.to_config() for a in c.accounts]
        entry["aliases"] = list(c.aliases)
        result.append(entry)
    return {"companies": result}


def set_entries(cfg: dict) -> None:
    set_firms(companies.companies_from(cfg))


def set_firms(current: dict[str, companies.Company]) -> None:
    """Replace isolated test identities; production has no delete or identity rewrite."""
    with closing(Ledger()) as L, L.tx():
        for table in ("firm_accounts", "firm_aliases", "firms"):
            L.db.execute(f"DELETE FROM {table}")
        for c in current.values():
            L.save_company(c)


def set_firm(key: str, company: companies.Company) -> None:
    set_firms(companies.all() | {key: company})


def iban(value: str) -> str:
    digits = "".join(str(int(c, 36)) for c in value[4:] + value[:2] + "00")
    return value[:2] + f"{98 - int(digits) % 97:02d}" + value[4:]
