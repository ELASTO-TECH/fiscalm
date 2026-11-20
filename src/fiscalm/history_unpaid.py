"""SmartBill's unpaid report: claims can open history, never confirm collection."""

from __future__ import annotations

import io
import json
import re
import unicodedata
import zipfile
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from fiscalm.efactura.inbox import safe_xml, tax_token
from fiscalm.history_saga import day
from fiscalm.ledger import Ledger
from fiscalm.settlements.reports import table

KIND = "smartbill-unpaid"


class UnpaidError(ValueError):
    pass


def label(value: str) -> str:
    return " ".join(
        "".join(c for c in unicodedata.normalize("NFD", value.casefold()) if not unicodedata.combining(c)).split()
    )


def amount(value: str) -> str:
    value = value.strip().replace("\u00a0", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", value):
        value = value.replace(".", "")
    value = value.replace(",", ".")
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
        raise UnpaidError("Lista are o sumă care nu poate fi citită. Nu s-a importat nimic.")
    return f"{Decimal(value):.2f}"


def issue_day(value: str) -> str:
    if re.fullmatch(r"\d{1,5}(?:\.0+)?", value):
        return (date(1899, 12, 30) + timedelta(days=int(Decimal(value)))).isoformat()
    try:
        return day(value)
    except ValueError:
        raise UnpaidError("Lista are o dată care nu poate fi citită. Nu s-a importat nimic.") from None


def parse(data: bytes) -> list[dict]:
    """Pinned public column meanings, with the existing bounded, formula-free XLSX reader."""
    rows = table(data, "xlsx")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        props = safe_xml(z.read("xl/workbook.xml")).find(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbookPr"
        )
        if props is not None and props.get("date1904", "0").lower() not in ("0", "false"):
            raise UnpaidError("Lista folosește un calendar Excel neacceptat. Nu s-a importat nimic.")
    required = {"factura", "data emiterii", "rest de incasat"}
    for _index, row in enumerate(rows[:30]):
        header = [label(v) for v in row]
        if required <= set(header):
            break
    else:
        raise UnpaidError("Se acceptă lista SmartBill Facturi neîncasate, cu Rest de încasat.")
    nonempty = [h for h in header if h]
    if len(set(nonempty)) != len(nonempty):
        raise UnpaidError("Lista are coloane repetate. Nu s-a importat nimic.")
    out = []
    for row in rows[_index + 1 :]:
        cells = dict(zip(header, row + [""] * max(0, len(header) - len(row)), strict=False))
        number = cells["factura"].strip()
        if not number or label(number) == "total":
            continue  # client/group headings and report totals identify no invoice
        out.append(
            dict(
                kind=KIND,
                number=number,
                issue_date=issue_day(cells["data emiterii"]),
                remaining=amount(cells["rest de incasat"]),
            )
        )
    if not out:
        raise UnpaidError("Lista nu conține facturi cu număr și dată.")
    return out


def _targets(L: Ledger, company: str, claims: list[dict]) -> list[dict]:
    indexed: dict[tuple, list[dict]] = {}
    historic = L.db.execute(
        "SELECT * FROM history WHERE company=? AND direction='issued' AND doc_type='invoice'", (company,)
    )
    tracked = L.db.execute(
        "SELECT * FROM issued WHERE company=? AND doc_type='invoice' AND status!='duplicate' AND parse_error IS NULL",
        (company,),
    )
    for is_tracked, rows in ((False, historic), (True, tracked)):
        for row in rows:
            r = dict(row) | {"tracked": is_tracked}
            if is_tracked:
                r["party_tax_id"] = tax_token(r["client_tax_id"] or r["client_cui"])
            key = (r["number"].strip().casefold(), r["issue_date"])
            indexed.setdefault(key, []).append(r)
    return [_target(indexed.get((c["number"].strip().casefold(), c["issue_date"]), []), c) for c in claims]


def _target(candidates: list[dict], claim: dict) -> dict:
    identities = {(r["number"], r["party_tax_id"], r["total"], r["currency"]) for r in candidates}
    if len(identities) != 1:
        raise UnpaidError(
            "O factură din listă lipsește sau nu se identifică sigur. Facturile se importă înaintea listei. Nu s-a importat nimic."
        )
    target = next((r for r in candidates if r["tracked"]), candidates[0])
    if Decimal(claim["remaining"]) > Decimal(target["total"]):
        raise UnpaidError("Restul de încasat depășește totalul facturii. Nu s-a importat nimic.")
    return target


def prepare(
    L: Ledger, company: str, sha: str, claims: list[dict], load: Callable[[dict], dict], limit: int
) -> dict[str, dict]:
    """Read source evidence outside the ledger write lock, with one aggregate byte cap."""
    if L.get_meta(f"history_unpaid:{company}:{sha}"):
        return {}
    targets = _targets(L, company, claims)
    sources, size = {}, 0
    for claim, target in zip(claims, targets, strict=True):
        if target["tracked"] or Decimal(claim["remaining"]) == 0 or target["xml_sha"] in sources:
            continue
        source = load(target)
        size += source["read_size"]
        if size > limit:
            raise UnpaidError("Lista are prea multe documente sursă. Se importă pe perioade.")
        sources[target["xml_sha"]] = source | {"target": target}
    return sources


def apply(
    L: Ledger, company: str, sha: str, claims: list[dict], sources: dict[str, dict], store: Callable[[dict], bool]
) -> tuple[int, int]:
    """Re-check targets inside the caller's transaction before opening any debt."""
    key = f"history_unpaid:{company}:{sha}"
    if L.get_meta(key):
        return 0, 0
    targets = _targets(L, company, claims)
    keys = [(r["number"], r["issue_date"]) for r in targets]
    if len(set(keys)) != len(keys):
        raise UnpaidError("Aceeași factură apare de mai multe ori în listă. Nu s-a importat nimic.")
    if any(
        not t["tracked"] and Decimal(c["remaining"]) > 0 and t["xml_sha"] not in sources
        for c, t in zip(claims, targets, strict=True)
    ):
        raise UnpaidError("Facturile s-au schimbat. Lista trebuie importată din nou.")
    for target in targets:
        if not target["tracked"] and target["xml_sha"] in sources:
            old = sources[target["xml_sha"]]["target"]
            if any(
                target[k] != old[k] for k in ("number", "issue_date", "total", "currency", "party_name", "party_tax_id")
            ):
                raise UnpaidError("Facturile s-au schimbat. Lista trebuie importată din nou.")
    promoted = 0
    for claim, target in zip(claims, targets, strict=True):
        if Decimal(claim["remaining"]) > 0 and not target["tracked"]:
            promoted += store(sources[target["xml_sha"]])
    L.db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (key, json.dumps(claims)))
    return len(claims), promoted
