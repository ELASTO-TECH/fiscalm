"""Company catalog defaults, independent of frozen invoice records."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from fiscalm import companies
from fiscalm.invoicing.labels import VAT, vat_choices
from fiscalm.ledger import Ledger
from fiscalm.products.schema import FIELDS


class ProductError(ValueError):
    pass


def normalize(data: dict) -> dict:
    if set(data) != set(FIELDS) or any(not isinstance(v, str) for v in data.values()):
        raise ProductError("Verifică datele produsului.")
    row = {k: v.strip() for k, v in data.items()}
    if row["kind"] not in ("produs", "serviciu") or not 1 <= len(row["name"]) <= 200:
        raise ProductError("Verifică denumirea și tipul produsului.")
    if len(row["code"]) > 100 or len(row["description"]) > 2000:
        raise ProductError("Codul sau descrierea este prea lungă.")
    if not re.fullmatch(r"[A-Z0-9]{2,3}", row["unit"]) or not re.fullmatch(r"[A-Z]{3}", row["currency"]):
        raise ProductError("Verifică unitatea de măsură și moneda.")
    try:
        if not re.fullmatch(r"[0-9]+\.[0-9]{2}", row["unit_price"]):
            raise ProductError("Prețul trebuie scris cu două zecimale.")
        price = Decimal(row["unit_price"])
        valid = (
            price.is_finite() and 0 <= price <= Decimal("999999999999.99") and price == price.quantize(Decimal(".01"))
        )
    except InvalidOperation:
        valid = False
    if not valid or row["vat_rate"] not in VAT["rates"]:
        raise ProductError("Verifică prețul și cota TVA.")
    choices = vat_choices({"vat_payer": True})["choices"] + vat_choices(None)["choices"]
    if not any(c["category"] == row["vat_category"] and c["rate"] == row["vat_rate"] for c in choices):
        raise ProductError("Verifică categoria și cota TVA.")
    if len(row["exemption"]) > 100 or (row["vat_category"] not in ("S", "Z") and not row["exemption"]):
        raise ProductError("Categoria TVA necesită un motiv de scutire.")
    return row | {"unit_price": format(price, ".2f")}


def get(L: Ledger, co: str, ident: str) -> dict:
    row = L.db.execute("SELECT * FROM products WHERE company=? AND id=?", (co, ident)).fetchone()
    if row is None:
        raise ProductError("Produsul nu mai este disponibil.")
    return dict(row) | {"archived": bool(row["archived"]), "can_delete": not used(L, co, ident)}


def used(L: Ledger, co: str, ident: str) -> bool:
    return (
        L.db.execute(
            "SELECT 1 FROM product_references WHERE company=? AND product_id=? LIMIT 1", (co, ident)
        ).fetchone()
        is not None
    )


def catalog(L: Ledger, co: str) -> dict:
    rows = L.db.execute(
        """SELECT p.*, NOT EXISTS (
        SELECT 1 FROM product_references r WHERE r.company=p.company AND r.product_id=p.id
        ) AS can_delete FROM products p WHERE company=? ORDER BY archived,name,id""",
        (co,),
    ).fetchall()
    return {
        "company": co,
        "products": [dict(r) | {"archived": bool(r["archived"]), "can_delete": bool(r["can_delete"])} for r in rows],
    }


def write(L: Ledger, co: str, op: str, args: dict) -> dict:
    if co not in companies.all():
        raise ProductError("Alege o firmă.")
    with L.tx(immediate=True):
        if op == "product-add":
            row = normalize({"code": "", "description": "", "exemption": ""} | args)
            ident = uuid4().hex
            L.add_product(co, ident, row)
            return get(L, co, ident)
        ident, version = args.get("id"), args.get("version")
        if not isinstance(ident, str) or type(version) is not int or not 1 <= version < 2**63 - 1:
            raise ProductError("Verifică produsul și versiunea.")
        current = get(L, co, ident)
        if op == "product-delete":
            ok = L.delete_product(co, ident, version)
        else:
            fields = {k: v for k, v in args.items() if k in FIELDS}
            row = normalize({k: current[k] for k in FIELDS} | fields)
            archived = args.get("archived", current["archived"])
            if type(archived) is not bool:
                raise ProductError("Verifică starea produsului.")
            ok = L.update_product(co, ident, version, row, archived)
        if not ok:
            raise ProductError("Produsul s-a modificat sau este folosit pe o factură. Reîncarcă lista.")
        return {"id": ident} if op == "product-delete" else get(L, co, ident)


def list_page(L: Ledger, co: str, query: dict) -> dict:
    try:
        limit, offset = int(query.get("limit") or "50"), int(query.get("cursor") or "0")
    except ValueError:
        raise ProductError("Verifică pagina.") from None
    if not 1 <= limit <= 200 or not 0 <= offset < 2**63 or query.get("f", "") not in ("", "archived"):
        raise ProductError("Verifică pagina și filtrul.")
    needle = str(query.get("q", "")).strip().casefold()
    if len(needle) > 200:
        raise ProductError("Căutarea este prea lungă.")
    rows = [
        r
        for r in catalog(L, co)["products"]
        if r["archived"] == (query.get("f") == "archived")
        and (not needle or needle in (r["name"] + " " + r["code"]).casefold())
    ]
    return dict(
        company=co,
        products=rows[offset : offset + limit],
        total=len(rows),
        next_cursor=str(offset + limit) if offset + limit < len(rows) else None,
    )
