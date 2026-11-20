"""fiscalm's company-scoped invoice workspace and commands; accounting stays in the engine."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from fiscalm import bnr, companies, company_config, history, ledger, rules
from fiscalm.api import actions
from fiscalm.api.responses import fail
from fiscalm.api.tokens import authorize
from fiscalm.bank import catalogue as banks
from fiscalm.commerce.state import draft_fields
from fiscalm.efactura import client as anaf, public as anaf_public
from fiscalm.invoicing import (
    branding,
    copy as invoice_copy,
    engine,
    pdf as invoice_pdf,
    recurring,
    spv as invoice_spv,
    ubl,
)
from fiscalm.invoicing.labels import COUNTY_NAMES, UNITS, vat_choices
from fiscalm.invoicing.schema import BODIES, object_schema, validate
from fiscalm.ledger import Ledger, now
from fiscalm.products import engine as products

ADDRESS = ("street", "city", "county", "postal_code", "country")


REQUIRED = dict(
    seller=["legal_name", "cui", "vat_payer", "address.street", "address.city", "address.country", "iban", "bank"],
    seller_if_ro=["address.county"],
    seller_if_vat_payer=[],
    client=dict(
        RO=["name", "address.street", "address.city", "country", "address.county"],
        other=["name", "address.street", "address.city", "country"],
    ),
    client_if_vat_payer=["vat_id"],
    draft_if_advance_payment_date=["advance_invoice:true"],
    client_identity_any_of=["cui", "vat_id", "tax_id", "private:true"],
    client_if_k=["address.county"],
    client_if_ro_vat_registered=["cui", "vat_id"],
)


def party_out(p: dict, seller: bool = False) -> dict:
    keys = (
        ("cui", "vat_id", "reg_com", "iban", "bank", "email", "phone")
        if seller
        else ("cui", "vat_id", "tax_id", "reg_com", "iban", "bank", "account_name", "email", "phone")
    )
    return {k: p.get(k, "") for k in keys} | dict(
        **(
            {
                "legal_name": p.get("name", ""),
                "vat_payer": p.get("vat_payer", False),
                "vat_on_collection": p.get("vat_on_collection", False),
            }
            if seller
            else {
                "ro_vat_status": p.get("ro_vat_status", "unknown"),
                "name": p.get("name", ""),
                "private": p.get("private", False),
                **{k: p.get(k, False) for k in ("vat_payer", "efactura_registered", "inactive")},
                "country": p.get("country", ""),
            }
        ),
        address={k: p.get(k, "") for k in ADDRESS},
    )


def party_in(p: dict, seller: bool = False) -> dict:
    p = dict(p)
    address = p.pop("address")
    if not isinstance(address, dict) or set(address) - set(ADDRESS):
        raise ValueError
    if seller:
        p["name"] = p.pop("legal_name")
    if p.get("country") and address.get("country") and p["country"] != address["country"]:
        raise engine.InvoiceError("Țara clientului diferă de țara din adresă.")
    country = p.get("country") or address.get("country", "")
    return p | address | dict(country=country)


# Legea 72/2013: without an agreed term, payment is due 30 days after the invoice is received.
DEFAULT_DUE_DAYS = 30


def last_invoices(L: Ledger, co: str, clients: set[str]) -> tuple[int, dict[str, dict]]:
    """What a new invoice starts from: the term of the firm's latest issued invoice, and for each of these clients the
    term, currency and lines of its own latest one. Corrections (storno) don't count. Only proposals: the form shows
    them and the user changes or sends them."""
    rows = L.db.execute(
        "SELECT i.id, json_extract(i.data,'$.issue_date') AS issued, json_extract(i.data,'$.due_date'), m.value, "
        "json_extract(i.data,'$.currency') FROM invoices i LEFT JOIN meta m ON m.key='draft_client:'||i.id "
        "WHERE i.company=? AND i.status='issued' AND COALESCE(json_extract(i.data,'$.billing_ref'),'')='' "
        "ORDER BY issued, i.rowid",
        (co,),
    ).fetchall()

    def term(row: tuple) -> int:
        return (date.fromisoformat(row[2]) - date.fromisoformat(row[1])).days

    latest = {r[3]: r for r in rows if r[3] in clients}
    return (term(rows[-1]) if rows else DEFAULT_DUE_DAYS), {
        client: dict(
            due_days=term(r),
            currency=r[4],
            lines=[_line_out(line) for line in engine.invoice(L, r[0], company=co)["lines"]],
        )
        for client, r in latest.items()
    }


def _line_out(line: dict) -> dict:
    return {k: v for k, v in line.items() if k not in {"net", "exemption_reason"}} | dict(
        exemption=line.get("exemption_reason", "")
    )


def accounts_out(co: str) -> list[dict]:
    """The company's current accounts: the only IBANs a new invoice may carry (engine.seller_account at save,
    engine.issuing_account at issue; issued invoices and their stornos keep the seller they were issued with)."""

    return [
        dict(
            iban=a.iban,
            currency=a.currency,
            bank=a.bank,
            pays=a.pays,
            bank_name=banks.BY_ID[a.bank].name if a.bank in banks.BY_ID else "",
        )
        for a in companies.get(co).current_accounts
    ]


def _plain(name: str) -> str:
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode()


# The county choices of a Romanian address, by name: Bucharest is chosen by sector, which the engine takes as the city.
COUNTIES = [
    dict(county="RO-" + code, city="SECTOR" + str(n), label=f"{name}, Sectorul {n}")
    if code == "B"
    else dict(county="RO-" + code, city=None, label=name)
    for code, name in sorted(COUNTY_NAMES.items(), key=lambda item: _plain(item[1]))
    for n in (range(1, 7) if code == "B" else [0])
]


def workspace(L: Ledger, co: str, today: str | None = None) -> dict:
    engine._company(L, co)
    today = today or ledger.now()[:10]
    try:
        seller = engine._seller(L, co)
    except engine.InvoiceError:
        seller = None
    rows = L.db.execute("SELECT * FROM invoice_clients WHERE company=?", (co,)).fetchall()
    due_days, last = last_invoices(L, co, {row["id"] for row in rows})
    clients = []
    for row in rows:
        clients.append(
            party_out(json.loads(row["data"]))
            | dict(id=row["id"], last_used=L.get_meta("client_used:" + row["id"]), last_invoice=last.get(row["id"]))
        )
    clients.sort(key=lambda c: (c["last_used"] or "", c["id"]), reverse=True)
    drafts = []
    for row in L.db.execute("SELECT id FROM invoices WHERE company=? AND status='draft' ORDER BY id", (co,)):
        inv = engine.invoice(L, row["id"], company=co)
        drafts.append(draft_out(L, inv))

    used = history.series_numbers(L, co)  # the numbers the firm's invoices used outside fiscalm, per series
    series = engine.series_list(L, co)
    logo = branding.metadata(L, co) if seller else None
    prefill = company_config.known_facts(L, co)
    return dict(
        co=co,
        live=ledger.is_live(),
        today=today,
        seller=(
            party_out(seller, True)
            | dict(logo=logo, logo_unavailable=bool(branding.current_sha(L, co)) and logo is None)
        )
        if seller
        else None,
        seller_prefill=party_out(prefill, True),
        auto_upload=invoice_spv.auto_upload_enabled(L, co),
        upload_after_days=invoice_spv.upload_after_days(L, co),
        series=[x | dict(imported=used[x["code"]]["max"] if x["code"] in used else None) for x in series],
        series_new=None if series else engine.first_series(used, companies.get(co, L).legal_name),
        accounts=accounts_out(co),
        invoice_accounts=engine.account_choices(L, co, seller) if seller else None,
        due_days=due_days,
        clients=clients,
        products=[p for p in products.catalog(L, co)["products"] if not p["archived"]],
        recurrences=recurring.list_rules(L, co, today),
        drafts=drafts,
        line_description_max=engine.LINE_DESCRIPTION_MAX,
        units=UNITS,
        counties=COUNTIES,
        countries=sorted(engine.COUNTRIES),
        # Before the seller is complete (a firm with no account yet), the VAT the registry knows chooses the lines' VAT.
        vat=vat_choices(seller or (prefill if isinstance(prefill.get("vat_payer"), bool) else None)),
        required=REQUIRED,
    )


def draft_out(L: Ledger, inv: dict) -> dict:
    """Project one already company-checked draft for app and API callers."""
    d = inv["data"]
    totals = d["totals"]

    return dict(
        **draft_fields(L, inv["company"], inv["id"]),
        id=inv["id"],
        version=inv["version"],
        series=inv["series"],
        client_id=L.get_meta("draft_client:" + inv["id"]),
        client=party_out(d["client"]),
        issue_date=d["issue_date"],
        due_date=d["due_date"],
        currency=d["currency"],
        iban=engine.draft_account(L, inv),
        exchange_rate=None if d["currency"] == "RON" else dict(rate=d["exchange_rate"], date=d["exchange_date"]),
        lines=[_line_out(line) for line in inv["lines"]],
        advance_invoice=d.get("advance_invoice", False),
        delivery_date=d.get("delivery_date") or None,
        advance_payment_date=d.get("advance_payment_date") or None,
        warnings=d.get("warnings", []),
        supply_country=d.get("supply_country", "RO"),
        chargeability_date=d.get("chargeability_date"),
        mentions=d.get("mentions", []),
        notes=d.get("notes", ""),
        totals={k: totals[k] for k in ("net", "vat", "total")}
        | dict(
            by_vat=[dict(category=g["category"], rate=g["rate"], base=g["net"], vat=g["vat"]) for g in totals["taxes"]],
            total_ron=None if d["currency"] == "RON" else engine.ron_totals(d)["total"],
            vat_ron=None if d["currency"] == "RON" else engine.ron_totals(d)["vat"],
        ),
        updated_at=L.get_meta("draft_updated:" + inv["id"]),
    )


@dataclass
class CommandContext:
    op: str
    today: str
    result: dict
    authorize_upload: Callable[[], None] | None


def _recurrence(L: Ledger, co: str, body: dict, ctx: CommandContext) -> None:
    validate(BODIES[ctx.op], {k: v for k, v in body.items() if k != "co"})
    if len(json.dumps(body, ensure_ascii=False).encode()) > 4096:
        raise ValueError
    if ctx.op == "recurrence-create":
        ctx.result["id"] = recurring.create(
            L, co, body["draft_id"], body["day"], body["due_days"], ctx.today, body.get("first_period")
        )
    else:
        recurring.change(L, co, body["id"], ctx.op.removeprefix("recurrence-"))
    ctx.result["recurrences"] = recurring.list_rules(L, co, ctx.today)


def _client_lookup(L: Ledger, co: str, body: dict, ctx: CommandContext) -> None:
    profile = anaf_public.lookup(body["cui"], ctx.today, L=L)
    anaf_public.remember(L, profile, ctx.today)
    ctx.result["client"] = party_out(profile)


def _draft_save(L: Ledger, co: str, body: dict, ctx: CommandContext) -> None:
    data = {k: body[k] for k in ("issue_date", "due_date", "currency", "notes")}
    for field in ("delivery_date", "advance_payment_date"):
        if body.get(field):
            data[field] = body[field]
    if "supply_country" in body:
        data["supply_country"] = body["supply_country"]
    if "iban" in body:
        data["iban"] = body["iban"]
    data["advance_invoice"] = body.get("advance_invoice", False)
    data["lines"] = [dict(line, exemption_reason=line.get("exemption", "")) for line in body["lines"]]
    for line in data["lines"]:
        line.pop("exemption", None)
    if data["currency"] != "RON":
        data["exchange_rate"], data["exchange_date"] = bnr.rate(
            L, data["currency"], engine.chargeability_date(data), today=ctx.today
        )
    with L.tx():
        ctx.result["id"] = engine.save_draft(
            L, co, body["series"], body["client_id"], data, ctx.result["id"], version=body.get("version")
        )
        ctx.result["version"] = engine.invoice(L, ctx.result["id"], company=co)["version"]


def act(
    L: Ledger,
    op: str,
    co: str,
    body: dict,
    *,
    authorize_upload: Callable[[], None] | None = None,
    require: Callable[[str], bool] = lambda need: True,
) -> dict:
    """Dispatch known commands; refusals keep the workspace available and never expose tracebacks."""
    iid = body.get("id")
    result = dict(ok=False, message=actions.BAD, id=iid if isinstance(iid, str) else None)
    spec = ACTIONS.get(op)
    if spec is None:
        return result
    if not allowed_in_mode(op):
        return result | dict(message=actions.SHADOW)
    if not require(spec.role):
        return result | dict(message=actions.ROLE_REFUSALS.get(spec.role, actions.BAD))
    ctx = CommandContext(op, ledger.now()[:10], result, authorize_upload)
    try:
        _validate_body(op, body, iid, spec)
        spec.handler(L, co, body, ctx)
        return _completed(L, co, op, body, spec, result)
    except anaf.AnafError:
        return result | dict(message="Conexiunea SPV nu este disponibilă. Verifică autorizarea, apoi încearcă din nou.")
    except engine.DraftConflict as exc:
        return result | dict(message=str(exc), code=exc.code)
    except engine.InvoiceError as exc:
        shown = op in {"logo", "logo-remove"} or op.startswith("recurrence-") or isinstance(exc, engine.SeriesRefusal)
        return result | dict(message=str(exc) if shown else invoice_copy.refusal(str(exc)))
    except sqlite3.IntegrityError:
        return result | dict(message="Datele intră în conflict cu o înregistrare existentă.")
    except (KeyError, TypeError, ValueError, AttributeError):
        return result


def _validate_body(op: str, body: dict, iid: str | None, spec: actions.Action) -> None:
    if op in {"draft-save", "issue", "draft-delete"} and "version" in body:
        engine._version(body["version"])
    for key in ("series", "client_id", "code", "cui"):
        if key in body:
            engine._identifier(body[key])
    if "upload_id" in body and body["upload_id"] is not None:
        engine._identifier(body["upload_id"])
    if "id" in body and (not isinstance(iid, str) or not iid or len(iid) > 128):
        raise ValueError
    if not spec.valid(body):
        raise ValueError


def _completed(
    L: Ledger,
    co: str,
    op: str,
    body: dict,
    spec: actions.Action,
    result: dict,
) -> dict:
    iid = result["id"]
    message = spec.message
    if op == "auto-upload" and not body["on"]:
        message = "Trimiterea automată în SPV a fost oprită."
    if op == "client-save" and body.get("role") == "supplier":
        message = "Furnizorul a fost salvat."
    if op == "issue":
        message += "".join(" " + warning for warning in engine.invoice(L, iid, company=co)["data"].get("warnings", []))
    return result | dict(ok=True, message=message, id=iid)


def allowed_in_mode(op: str) -> bool:
    """Use registered policy for dispatch and stored-result replay."""
    spec = ACTIONS.get(op)
    return bool(spec and spec.allowed_in_mode())


def file(L: Ledger, co: str, iid: str, kind: str, *, preview: bool = False) -> bytes:
    """Read the same company-guarded invoice bytes for app and integration clients."""

    inv = engine.invoice(L, iid, company=co)
    if kind not in {"pdf", "xml"} or (inv["status"] == "draft") != preview:
        raise engine.InvoiceError("Factura nu a fost găsită.")
    return (
        invoice_pdf.render(L, iid, company=co, preview=preview) if kind == "pdf" else ubl.generate(L, iid, company=co)
    )


def write_app(
    L: Ledger, op: str, co: str, body: dict, *, authorize: Callable[[], None], require: Callable[[str], bool]
) -> dict:
    """Authorize under the write lock; SPV uploads claim and authorize in their own transactions."""
    if op in {"upload", "recover-upload"}:  # ANAF is called outside the lock; each authorizes inside its own write
        return act(L, op, co, body, authorize_upload=authorize, require=require)
    with L.tx():
        authorize()
        return act(L, op, co, body, require=require)


# Identifier fields are inspected by the shared guard even on unrelated commands.
IDENTIFIERS = {k: {} for k in ("id", "series", "client_id", "code", "cui", "upload_id", "version")}
APP_BODIES = {
    "seller-save": object_schema({"seller": {"type": "object"}}),
    "series-create": object_schema({"code": {"type": "string"}, "start": {"type": "integer"}}),
    "series-continue": object_schema({"code": {"type": "string"}, "next_number": {"type": "integer"}}),
    "series-default": object_schema({"code": {"type": "string"}}, ["code"]),
    "series-remove": object_schema({"code": {"type": "string"}}, ["code"]),
    "logo": object_schema({"data": {"type": "bytes"}}),
    "logo-remove": object_schema({}),
    "recover-upload": object_schema({"id": {"type": "string"}}, ["id"]),
}


def _body(op: str) -> dict:
    schema = (BODIES | APP_BODIES)[op]
    properties = IDENTIFIERS | schema["properties"]
    if op == "issue":  # shared issuance forms also send the correction reason; only storno uses it
        properties["reason"] = BODIES["storno"]["properties"]["reason"]
    return schema | {"properties": properties}


# One row per entry keeps the handler and policy together.
# fmt: off
ACTIONS = {
    op: actions.Action(
        handler,
        "",
        effect=effect,
        role="manage_rules" if op == "auto-upload" else "member",
        public=op in BODIES,
        schema=_body(op),
        public_schema=BODIES.get(op),
        message=message,
        limit=branding.MAX_BYTES if op == "logo" else 4096 if op.startswith("recurrence-") else 65536,
    )
    # Keep each handler beside its success message and shadow policy.
    for op, (handler, message, effect) in {
        'recurrence-create': (_recurrence, 'Regula a fost creată.', "local"),
        'recurrence-pause': (_recurrence, 'Regula a fost oprită.', "local"),
        'recurrence-resume': (_recurrence, 'Regula a fost pornită.', "local"),
        'recurrence-delete': (_recurrence, 'Regula a fost ștearsă.', "local"),
        'logo': (lambda L, co, b, c: branding.set_logo(L, co, b['data']), 'Sigla a fost încărcată.', "local"),
        'logo-remove': (lambda L, co, b, c: branding.remove_logo(L, co), 'Sigla a fost eliminată.', "local"),
        'seller-save': (lambda L, co, b, c: engine.save_seller(L, co, party_in(b['seller'], True)), 'Datele firmei au fost salvate.', "local"),
        'series-create': (lambda L, co, b, c: engine.create_series(L, co, b['code'], b['start']), 'Seria a fost creată.', "local"),
        'series-continue': (lambda L, co, b, c: engine.continue_series(L, co, b['code'], b['next_number']), 'Seria a fost continuată.', "local"),
        'series-default': (lambda L, co, b, c: engine.set_default_series(L, co, b['code']), 'Seria implicită a fost schimbată.', "local"),
        'series-remove': (lambda L, co, b, c: engine.remove_series(L, co, b['code']), 'Seria a fost ștearsă.', "local"),
        'client-lookup': (_client_lookup, 'Datele clientului au fost găsite.', "local"),
        'client-save': (lambda L, co, b, c: c.result.update(id=engine.save_client(L, co, party_in(b['client']), c.result['id'], b.get('role'))), 'Clientul a fost salvat.', "local"),
        'draft-save': (_draft_save, 'Ciorna a fost salvată.', "local"),
        'upload': (lambda L, co, b, c: engine.upload(L, b['id'], company=co, authorize=c.authorize_upload), 'Starea trimiterii în SPV a fost actualizată.', "anaf_upload"),
        'draft-delete': (lambda L, co, b, c: engine.delete_draft(L, b['id'], company=co, version=b.get('version')), 'Ciorna a fost ștearsă.', "local"),
        'issue': (lambda L, co, b, c: engine.issue(L, b['id'], c.today, company=co, version=b.get('version')), 'Factura a fost emisă.', "local"),
        'storno': (lambda L, co, b, c: c.result.update(id=engine.storno(L, b['id'], c.today, b['reason'], b.get('lines'), company=co)['id']), 'Factura de stornare a fost emisă.', "local"),
        'resolve-upload': (lambda L, co, b, c: invoice_spv.resolve(L, b['id'], company=co, upload_id=b['upload_id'], checked_in_spv=b['checked_in_spv']), 'Trimiterea în SPV a fost lămurită.', "local"),
        'recover-upload': (lambda L, co, b, c: invoice_spv.recover_unknown(L, b['id'], company=co, authorize=c.authorize_upload), 'Factura a fost găsită în SPV.', "anaf_upload"),
        'auto-upload': (lambda L, co, b, c: invoice_spv.set_auto_upload(L, co, b['on']), 'Trimiterea automată în SPV a fost pornită.', "local"),
    }.items()
}
# fmt: on


def read(L: Ledger, co: str, resource: str, params: dict) -> object:
    if resource == "invoicing_recurrences":
        return recurring.list_rules(L, co, now()[:10])
    if resource == "invoicing_workspace":
        value = workspace(L, co)
        # Logo capabilities and private configuration stay in the authenticated app.
        if value["seller"]:
            value["seller"].pop("logo", None)
            value["seller"].pop("logo_unavailable", None)
        return value
    iid = params["id"]
    try:
        inv = engine.invoice(L, iid, company=co)
        if resource == "invoicing_draft":
            if inv["status"] == "draft":
                return draft_out(L, inv)
        elif resource == "invoicing_issued" and inv["status"] != "draft":
            return inv
        elif resource in ("invoicing_pdf", "invoicing_xml"):
            kind = resource.rsplit("_", 1)[1]
            return iid + "." + kind, file(L, co, iid, kind)
    except (engine.InvoiceError, ValueError, OSError):
        pass
    fail(404, "not_found", "The invoice does not exist in this company or state.")


def write(L: Ledger, credential: dict, co: str, op: str, body: object, key: str | None) -> dict:
    if not isinstance(body, dict) or body.get("co", co) != co:
        fail(400, "invalid_action", "Use the URL company and documented invoicing fields.")
    body = {k: v for k, v in body.items() if k != "co"}
    try:
        validate(ACTIONS[op].public_schema, body)
    except ValueError:
        fail(400, "invalid_action", "Use the documented invoicing fields and types.")
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 4096:
        fail(413, "body_too_large", "The body exceeds 4 KB. Send a smaller request.")
    if not allowed_in_mode(op):
        fail(403, "shadow_mode", "Activate this installation before writing outside fiscalm.")
    if key is not None:
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            fail(400, "invalid_idempotency_key", "Use a 1-128 character ASCII idempotency key.")
        if not credential.get("id"):
            fail(400, "missing_credential_id", "A persisted token ID is required for idempotent requests.")
    if op == "upload":
        # The SPV claim must commit before sending, so it owns its transaction.
        return perform(L, co, op, body, authorize_upload=lambda: authorize(L, credential))
    stamp = int(time.time())
    with L.tx() as db:
        # Hold authorization through engine writes; their tx blocks nest as savepoints.
        authorize(L, credential)
        if op == "auto-upload" and not rules.may_change(bearer=True):
            fail(403, "admin_required", rules.ADMIN_REFUSAL)
        if key is None:
            result = perform(L, co, op, body)
        else:
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            fingerprint = hashlib.sha256(
                json.dumps([op, body], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            db.execute("DELETE FROM api_invoice_requests WHERE created_at<=?", (stamp - 86400,))
            row = db.execute(
                "SELECT * FROM api_invoice_requests WHERE token_id=? AND company=? AND key_hash=?",
                (credential["id"], co, key_hash),
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    fail(409, "idempotency_conflict", "This key already names a different request. Use a new key.")
                return json.loads(row["response"])
            result = perform(L, co, op, body)
            db.execute(
                "INSERT INTO api_invoice_requests VALUES(?,?,?,?,?,?)",
                (credential["id"], co, key_hash, fingerprint, stamp, json.dumps(result)),
            )
    return result


def perform(L: Ledger, co: str, op: str, body: dict, *, authorize_upload: Callable[[], None] | None = None) -> dict:
    result = act(L, op, co, body, authorize_upload=authorize_upload)
    if not result["ok"]:
        fail(409, result.get("code", "action_refused"), result["message"])
    return result
