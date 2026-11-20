"""Conservative firm identity shared by saved clients and partner enrichment."""

from __future__ import annotations

import re
import unicodedata


def firm_id(token: str | None) -> bool:
    """Anonymous buyers and personal identifiers never identify a firm."""
    return bool(token and not re.fullmatch(r"0+|[1-8][0-9]{12}", token))


def partner_identity(country: str, cui: str | None, vat_id: str | None) -> str:
    """Only a Romanian CUI or a country-prefixed VAT id proves the same firm, never a name or local foreign id."""
    if country == "RO":
        token = re.sub(r"\s+", "", cui or vat_id or "").upper().removeprefix("RO")
        return token if re.fullmatch(r"[0-9]+", token) and firm_id(token) else ""
    token = re.sub(r"[\s.-]+", "", vat_id or "").upper()
    return (
        token.removeprefix("RO")
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]+", token) and firm_id(token.removeprefix("RO"))
        else ""
    )


def tax_token(v: str | None) -> str:
    """A tax id as one token: a Romanian CUI as its digits (with or without RO); a foreign VAT id keeps its country
    prefix and letters (DE123456789, FRAB123456789). "" when there is none."""
    t = re.sub(r"[^A-Z0-9]", "", (v or "").upper())
    if re.match(r"^[A-Z]{2}", t) and not t.startswith("RO"):
        return t
    return re.sub(r"\D", "", t)


def firm_name(name: str | None) -> str:
    """A firm's name as one token, conservatively: case, accents, punctuation and the old 'S.C.' prefix don't count,
    the legal form does ('S.C. Alfa Consult S.R.L.' = 'ALFA CONSULT SRL', but 'ALFA SRL' != 'ALFA SA'). Letters of any
    alphabet count ('Łukasz' is not 'ukasz', '北京 SRL' is not '上海 SRL')."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(c)).upper()
    words = "".join(c if c.isalnum() else " " for c in plain).split()
    out, run = [], []
    for w in words + [""]:  # dotted forms written with spaces ('S R L') become one word ('SRL')
        if len(w) == 1:
            run.append(w)
            continue
        if run:
            out.append("".join(run))
            run = []
        if w:
            out.append(w)
    if out and out[0] == "SC" and len(out) > 1:
        out = out[1:]
    return "-".join(out).lower()


def partner_id(co: str, cui: str | None, name: str | None, client_id: str | None = None) -> str | None:
    """Who an invoice is with, as one id per company: by firm tax id, saved client id, else name."""
    t = tax_token(cui)
    if firm_id(t):
        return f"{co}.{t}"
    if client_id:
        return f"{co}.client-{client_id}"
    n = firm_name(name)
    return f"{co}.n-{n}" if n else None
