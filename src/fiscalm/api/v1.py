"""Bearer-only public API. The route table is also the OpenAPI source."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, unquote, urlsplit

from fiscalm import PRODUCT, __version__, audit, companies, pack, rules
from fiscalm.api import actions, auth, lists, state as api, tokens, uploads as upload_links
from fiscalm.api.responses import Error, fail, page
from fiscalm.api.state import payment_id, resource
from fiscalm.commerce import orders as commerce
from fiscalm.invoicing import workspace as invoicing_api
from fiscalm.ledger import Ledger
from fiscalm.settlements.engine import perform


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    resource: str
    schema: str
    scope: str = "read"


ROUTES = (
    Route("POST", "/mcp", "mcp", "MCP"),
    Route("GET", "/meta", "meta", "Meta", "public"),
    Route("GET", "/openapi.json", "openapi", "OpenAPI", "public"),
    Route("GET", "/companies", "companies", "CompanyPage"),
    Route("GET", "/companies/{co}/upload-links", "upload_links", "UploadLinkList"),
    Route("POST", "/companies/{co}/upload-links", "upload_link_create", "Action", "write"),
    Route("POST", "/companies/{co}/upload-links/{id}/revoke", "upload_link_revoke", "Action", "write"),
    Route("GET", "/companies/{co}/state", "state", "State"),
    Route("GET", "/companies/{co}/state-summary", "state_summary", "StateSummary"),
    Route("GET", "/companies/{co}/detail", "state_detail", "StateDetail"),
    Route("GET", "/companies/{co}/transaction-choices", "transaction_choices", "TransactionChoices"),
    Route("GET", "/companies/{co}/partner-pages", "partner_pages", "PartnerPages"),
    Route("GET", "/companies/{co}/state-list", "state_list", "StateListPage"),
    Route("GET", "/companies/{co}/browse", "state_browse", "BrowsePage"),
    Route("GET", "/companies/{co}/report-pages", "state_report_pages", "ReportPages"),
    Route("GET", "/companies/{co}/dashboard-list", "dashboard_list", "DashboardPage"),
    Route("GET", "/companies/{co}/search", "state_search", "SearchResults"),
    Route("GET", "/companies/{co}/tasks", "state_tasks", "TaskPage"),
    Route("GET", "/companies/{co}/invoices/received", "received", "ReceivedPage"),
    Route("GET", "/companies/{co}/invoices/received/{id}", "received_one", "Received"),
    Route("GET", "/companies/{co}/invoices/issued", "issued", "IssuedPage"),
    Route("GET", "/companies/{co}/invoices/issued/{id}", "issued_one", "Issued"),
    Route("GET", "/companies/{co}/payments", "payments", "PaymentPage"),
    Route("GET", "/companies/{co}/payments/{id}", "payment_one", "Payment"),
    Route("GET", "/companies/{co}/documents", "documents", "DocumentPage"),
    Route("GET", "/companies/{co}/documents/{sha}", "document_one", "Document"),
    Route("GET", "/companies/{co}/documents/{sha}/file", "document_file", "File"),
    Route("GET", "/companies/{co}/reports/{month}/pack", "pack_file", "File"),
    Route("GET", "/companies/{co}/reports/{month}", "report", "Report"),
    Route("POST", "/companies/{co}/actions/{action}", "action", "Action", "write"),
)

# Explicit operation paths keep configuration-only app commands outside the bearer surface.
ROUTES += (
    Route("GET", "/companies/{co}/invoicing/recurrences", "invoicing_recurrences", "Recurrences"),
    Route("GET", "/companies/{co}/invoicing", "invoicing_workspace", "InvoicingWorkspace"),
    Route("GET", "/companies/{co}/invoicing/drafts/{id}", "invoicing_draft", "InvoicingDraft"),
    Route("GET", "/companies/{co}/invoicing/issued/{id}", "invoicing_issued", "InvoicingIssued"),
    Route("GET", "/companies/{co}/invoicing/issued/{id}/pdf", "invoicing_pdf", "File"),
    Route("GET", "/companies/{co}/invoicing/issued/{id}/xml", "invoicing_xml", "File"),
) + tuple(
    Route("POST", "/companies/{co}/invoicing/" + op, "invoicing_" + op, "InvoicingAction", "write")
    for op, spec in invoicing_api.ACTIONS.items()
    if spec.public
)


ROUTES += (
    Route("POST", "/companies/{co}/orders", "orders_create", "OrderAction", "write"),
    Route("GET", "/companies/{co}/orders", "orders_list", "OrderPage"),
    Route("GET", "/companies/{co}/orders/{id}", "orders_one", "Order"),
    *(
        Route("POST", "/companies/{co}/orders/{id}/" + op, "orders_" + op, "OrderAction", "write")
        for op in ("awb", "cancel", "refund", "discard", "delivery")
    ),
)


def run_action(
    L: Ledger, co: str, action: str, body: object, *, authorize: Callable[[Ledger], None] | None = None
) -> dict:
    if action.startswith("integration-") and not auth.require(None, "manage_integrations", bearer=True):
        fail(403, "app_required", "Manage integrations through the authenticated app.")
    if (
        action not in actions.API_ACTIONS
        or not isinstance(body, dict)
        or not all(
            (k == "all_firms" and action == "upload-link-create" and type(v) is bool)
            or (isinstance(v, (str, int)) and not isinstance(v, bool))
            for k, v in body.items()
        )
        or body.get("co", co) != co
    ):
        fail(400, "invalid_action", "The action body is invalid. Check the action contract.")
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 4096:
        fail(413, "body_too_large", "The body exceeds 4 KB. Send a smaller request.")
    if action == "mail-answer" and body.get("who") not in (co, "none"):
        fail(400, "wrong_company", "The destination differs from the URL company. Use its URL.")
    if not actions.ACTIONS[action].allowed_in_mode():
        fail(403, "shadow_mode", "Activate this install before writing outside fiscalm.")
    if action == "upload-link-create":
        try:
            with L.tx():
                if authorize is not None:
                    authorize(L)
                return {"ok": True, "message": "", "next": None, "done": upload_links.action(L, co, action, body)}
        except (ValueError, TypeError):
            fail(400, "invalid_action", "Use the documented upload link fields.")
    # Only target takes a payment candidate id; invoice ids remain unchanged.
    if action == "resolve-debit":
        identifiers = {payment_id(r["item_id"]): r["item_id"] for r in L.payments(company=co)}
        body = dict(body)
        body["target"] = identifiers.get(body.get("target"), body.get("target", ""))
    result = actions.act(
        action, co, {k: str(v) for k, v in body.items()} | {"co": co}, company_only=True, authorize=authorize
    )
    if not result["ok"]:
        fail(409, "action_refused", "The action was refused. Refresh state and check its preconditions.")
    try:
        result["state"] = public_state(api.state(L, co), L)
    except Exception:
        result["state"] = None
    return result


def public_state(value: object, L: Ledger | None = None) -> object:
    """Remove internal payment keys and replace references with public identifiers."""
    identifiers = {r["item_id"]: payment_id(r["item_id"]) for r in L.payments()} if L else {}

    def project(item: object) -> object:
        if isinstance(item, dict):
            if item.get("kind") in rules.MUTABLE:
                item = item | rules.capabilities(item, bearer=True)
            return {k: project(v) for k, v in item.items() if k not in ("item_id", "can_manage_companies")}
        if isinstance(item, list):
            return [project(v) for v in item]
        if isinstance(item, str):
            for internal, public in identifiers.items():
                item = item.replace(internal, public)
        return item

    return project(value)


def execute_as(
    L: Ledger,
    credential: dict,
    route: Route,
    params: dict,
    query: dict,
    body: object = None,
    *,
    idempotency_key: str | None = None,
) -> object:
    def guard(current: Ledger) -> None:
        shared = isinstance(body, dict) and body.get("all_firms") is True
        if route.resource == "upload_link_revoke" or (
            route.resource == "action" and params.get("action") == "upload-link-revoke"
        ):
            ident = params.get("id") or (body or {}).get("id")
            shared = bool(
                current.db.execute("SELECT 1 FROM upload_links WHERE id=? AND company='ALL'", (ident,)).fetchone()
            )
        tokens.authorize(current, credential, admin=shared)

    with L.guard_writes(guard if route.scope == "write" else None):
        return _execute_as(L, credential, route, params, query, body, idempotency_key=idempotency_key)


def _execute_as(
    L: Ledger,
    credential: dict,
    route: Route,
    params: dict,
    query: dict,
    body: object = None,
    *,
    idempotency_key: str | None = None,
) -> object:

    if "co" in params and not companies.all(L):
        fail(409, "needs_company", companies.NEEDS_COMPANY)
    if idempotency_key is not None and route.resource not in ("invoicing_issue", "invoicing_storno"):
        fail(400, "unsupported_idempotency_key", "Idempotency-Key is supported only for invoice issue and storno.")
    who = audit.identity(credential) if credential else ""
    reset = audit.actor.set(who)
    try:
        if route.resource == "action":
            action = params["action"] if params["action"] in actions.API_ACTIONS else "invalid"
            co = params["co"] if params["co"] in companies.all(L) else "invalid"
            actions.log(f"api: {who} action={action} company={co}")
        if route.scope == "write" and credential.get("scope") != "write":
            fail(403, "insufficient_scope", "This token is read-only. Use a write token.")
        if route.resource.startswith("invoicing_") and route.scope == "write":
            co = params["co"]
            if co not in companies.all(L):
                fail(404, "company_not_found", "List companies first.")
            actions.log(f"api: {who} action={route.resource} company={co}")
            return invoicing_api.write(L, credential, co, route.resource[10:], body, idempotency_key)
        if route.resource.startswith("orders_") and route.scope == "write":
            co = params["co"]
            if co not in companies.all(L):
                fail(404, "company_not_found", "List companies first.")
            if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > commerce.LIMIT:
                fail(413, "body_too_large", "The order body exceeds 256 KB.")
            actions.log(f"api: {who} action={route.resource} company={co}")
            return commerce.dispatch(
                L,
                co,
                route.resource,
                params,
                query,
                body,
                date.today().isoformat(),
                lambda: tokens.authorize(L, credential),
            )
        if route.resource == "action" and params.get("action") == "settlement-confirm":
            if params["co"] not in companies.all(L):
                fail(404, "company_not_found", "List companies first.")
            if not isinstance(body, dict):
                fail(
                    400,
                    "confirmation_refused",
                    "Confirmation requires a valid request.",
                )
            owner = credential["user_id"]
            tokens.authorize(L, credential)
            result = perform(L, params["co"], body, f"user {owner} via {who}", lambda: tokens.authorize(L, credential))
            if not result["ok"]:
                fail(409, "action_refused", "Refresh the settlement and verify its credit and invoices.")
            try:
                result["state"] = public_state(api.state(L, params["co"]), L)
            except Exception:
                result["state"] = None  # Preserve the committed confirmation if the following read fails.
            return result
        return execute(
            L,
            route,
            params,
            query,
            body,
            authorize=(lambda current: tokens.authorize(current, credential)) if route.scope == "write" else None,
        )
    finally:
        audit.actor.reset(reset)


def execute(
    L: Ledger,
    route: Route,
    params: dict,
    query: dict,
    body: object = None,
    *,
    authorize: Callable[[Ledger], None] | None = None,
) -> object:

    if route.resource == "openapi":
        from fiscalm.api.schema import openapi

        return openapi()
    co = params.get("co")
    if co is not None and not companies.all(L):
        fail(409, "needs_company", companies.NEEDS_COMPANY)
    if co is not None and co not in companies.all(L):
        fail(404, "company_not_found", "The company does not exist. List companies first.")
    if route.resource.startswith("orders_"):
        if (
            route.scope == "write"
            and len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > commerce.LIMIT
        ):
            fail(413, "body_too_large", "The order body exceeds 256 KB.")
        return commerce.dispatch(L, co, route.resource, params, query, body, date.today().isoformat())
    if route.resource.startswith("invoicing_"):
        return invoicing_api.read(L, co, route.resource, params)
    if route.resource == "companies":
        rows = [
            {"key": c.key, "name": c.name, "legal_name": c.legal_name, "palette": c.palette, "links": {}}
            for c in companies.all(L).values()
        ]
        return page(rows, "key", query)
    if route.resource == "upload_links":
        return upload_links.list_links(L, co)
    if route.resource in ("upload_link_create", "upload_link_revoke"):
        if not isinstance(body, dict) or (route.resource == "upload_link_revoke" and set(body) - {"co"}):
            fail(400, "invalid_action", "Use the documented upload link fields.")
        return run_action(
            L,
            co,
            route.resource.replace("_", "-"),
            body | ({"id": params["id"]} if "id" in params else {}),
            authorize=authorize,
        )
    if route.resource == "pack_file":
        if set(query) != {"version"}:
            fail(400, "invalid_query", "Name the exact prepared ZIP version.")
        try:
            data = pack.file(L, co, params["month"], query["version"])
            return pack.archive_name(companies.get(co, L=L), params["month"]), data
        except (ValueError, OSError):
            fail(404, "not_found", "The prepared company ZIP or its version is unavailable.")
    state_reads = {
        "meta": lambda: {"product": PRODUCT, "version": __version__},
        "state": lambda: api.state(L, co),
        "state_summary": lambda: lists.summary(L, co),
        "state_detail": lambda: lists.detail(L, co, query),
        "transaction_choices": lambda: lists.transaction_choices(L, co, query),
        "partner_pages": lambda: lists.partner_pages(L, co, query),
        "state_list": lambda: lists.collection(L, co, query),
        "state_tasks": lambda: lists.task_page(L, co, query),
        "state_search": lambda: lists.search(L, co, query),
        "state_browse": lambda: lists.browse(L, co, query),
        "state_report_pages": lambda: lists.report_pages(L, co, query),
        "dashboard_list": lambda: lists.dashboard_page(L, co, query),
    }
    if route.resource in state_reads:
        return public_state(state_reads[route.resource](), None if route.resource == "meta" else L)
    if route.resource == "action":
        return run_action(L, co, params["action"], body, authorize=authorize)
    return resource(L, route.resource, co, params, query)


def match(path: str, method: str | None = None) -> tuple[Route, dict] | None:
    candidates = sorted(ROUTES, key=lambda r: r.method != method)
    for route in candidates:
        names = re.findall(r"{(\w+)}", route.path)
        pattern = re.sub(r"{\w+}", r"([^/]+)", route.path)
        found = re.fullmatch("/api/v1" + pattern, path)
        if found:
            return route, dict(zip(names, (unquote(x) for x in found.groups()), strict=True))
    return None


def handle(h) -> None:
    """Serve a v1 request without consulting path tokens, cookies or Tailscale identity."""

    L = None
    try:
        parsed = urlsplit(h.path)
        found = match(parsed.path, h.command)
        L = Ledger()
        credential = _credential(h, L, found)
        route, params = found
        _admit_route(h, route, credential)
        query = _query(parsed.query)
        body = None
        if h.command == "POST":
            n = _body_length(h, route)
            try:
                body = json.loads(h.rfile.read(n))
            except (ValueError, UnicodeDecodeError):
                if route.resource == "mcp":
                    from fiscalm.api.mcp import error

                    return _send_json(h, 400, error(None, -32700, "Invalid JSON."))
                fail(400, "invalid_json", "The JSON body is invalid. Correct it and retry.")
        key = _idempotency_key(h, route)
        if route.resource == "mcp":
            return _serve_mcp(h, L, credential, body)
        out = execute_as(L, credential, route, params, query, body, idempotency_key=key)
        if isinstance(out, tuple):
            _send_file(h, route, out)
        else:
            _send_json(h, 200, out)
    except Error as e:
        _send_api_error(h, e, found if e.status == 405 else None)
    except Exception:
        _send_internal_error(h)
    finally:
        if L:
            L.close()


def _send_json(h, status: int, obj: object, extra: dict | None = None) -> None:
    h._send(
        status,
        json.dumps(obj, ensure_ascii=False).encode(),
        "application/json; charset=utf-8",
        {"Fiscalm-API-Version": "1", **(extra or {})},
    )


def _credential(h, L: Ledger, found: tuple[Route, dict] | None) -> dict | None:
    auth = h.headers.get_all("Authorization", [])
    bearer = auth[0][7:] if len(auth) == 1 and auth[0][:7].lower() == "bearer " else ""
    credential = tokens.verify(L, bearer)
    if not credential and not (found and found[0].scope == "public"):
        time.sleep(0.5)
        fail(401, "unauthorized", "The bearer token is missing or invalid. Create or replace it.")
    if not found:
        fail(404, "not_found", "The resource does not exist. Check the API documentation.")
    return credential


def _admit_route(h, route: Route, credential: dict | None) -> None:
    origin = h.headers.get("Origin")
    if route.resource == "mcp":
        _check_origin(h, origin)
    if h.command != route.method:
        fail(405, "method_not_allowed", "This method is not supported. Check the API documentation.")
    if route.scope == "write" and credential["scope"] != "write":
        fail(403, "insufficient_scope", "This token is read-only. Use a write token.")


def _query(raw_query: str) -> dict[str, str]:
    raw_q = parse_qs(raw_query, keep_blank_values=True)
    if any(len(v) != 1 for v in raw_q.values()):
        fail(400, "invalid_query", "Query parameters must occur once. Remove duplicates.")
    query = {k: v[0] for k, v in raw_q.items()}
    return query


def _body_length(h, route: Route) -> int:
    if h.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
        fail(415, "unsupported_media_type", "Send an application/json body.")
    origin = h.headers.get("Origin")
    _check_origin(h, origin)
    lengths = h.headers.get_all("Content-Length", [])
    if h.headers.get("Transfer-Encoding") or len(lengths) != 1:
        fail(400, "invalid_length", "Send one Content-Length header without transfer encoding.")
    try:
        n = int(lengths[0])
    except ValueError:
        fail(400, "invalid_length", "Content-Length is invalid. Send the body length in bytes.")
    if n < 0:
        fail(400, "invalid_length", "Content-Length cannot be negative.")
    if n > (256 * 1024 if route.resource.startswith("orders_") else 4096):
        fail(413, "body_too_large", "The body exceeds the route limit. Send a smaller request.")
    return n


def _idempotency_key(h, route: Route) -> str | None:
    keys = h.headers.get_all("Idempotency-Key", [])
    if len(keys) > 1:
        fail(400, "invalid_idempotency_key", "Send one Idempotency-Key header.")
    if keys and route.resource == "mcp":
        fail(
            400,
            "unsupported_idempotency_key",
            "Idempotency-Key is supported only for invoice issue and storno; MCP uses the idempotency_key argument.",
        )
    return keys[0] if keys else None


def _serve_mcp(h, L: Ledger, credential: dict, body: object) -> None:
    from fiscalm.api.mcp import dispatch

    status, result = dispatch(L, credential, body, h.headers)
    if result is None:
        return h._send(202, b"", "application/json", {"Fiscalm-API-Version": "1"})
    return _send_json(h, status, result)


def _send_file(h, route: Route, out: tuple[str, bytes]) -> None:
    name, data = out
    from fiscalm.api.web import inline

    h._send(
        200,
        data,
        (
            "application/zip"
            if route.resource == "pack_file"
            else "application/xml"
            if name.endswith(".xml")
            else api.MEDIA[__import__("pathlib").Path(name).suffix.lower()]
        ),
        {"Fiscalm-API-Version": "1", "Content-Disposition": inline(name)},
    )


def _check_origin(h, origin: str | None) -> None:
    if origin and not (
        h.server.boundary.secure(h.headers, h.connection) and h.server.boundary.accepts_origin(h.headers)
    ):
        fail(403, "invalid_origin", "The origin is not allowed. Use the same server origin.")


def _send_api_error(h, e: Error, found: tuple[Route, dict] | None) -> None:
    _send_json(
        h,
        e.status,
        {
            "error": {"code": e.code, "message": e.message},
            **({"order_id": e.order_id} if getattr(e, "order_id", None) else {}),
        },
        {"WWW-Authenticate": "Bearer"} if e.status == 401 else {"Allow": found[0].method} if e.status == 405 else None,
    )


def _send_internal_error(h) -> None:
    _send_json(
        h,
        500,
        {
            "error": {
                "code": "internal_error",
                "message": "The request failed. Retry later or contact the administrator.",
            }
        },
    )
