"""OpenAPI 3.1 contract; generated docs/openapi.json is the published copy."""

from __future__ import annotations

import re

from fiscalm import PRODUCT, docs, expenses, home_charts
from fiscalm.api import (
    actions,
    browse as browsing,
    dashboard_rows,
    details,
    lists,
    login,
    partner_rows,
    search as searching,
    sessions,
    state as api,
    transaction_choices,
)
from fiscalm.api.v1 import ROUTES
from fiscalm.commerce.schema import AWB, ORDER_BODY, obj as commerce_obj, schemas as commerce_schemas
from fiscalm.invoicing import workspace as invoicing_api
from fiscalm.invoicing.schema import response_schemas
from fiscalm.links import KINDS, LABELS, REASONS
from fiscalm.products.schema import sales_schemas, schemas as product_schemas

STRING = {"type": "string"}
NULL_STRING = {"type": ["string", "null"]}
BOOL = {"type": "boolean"}
INTEGER = {"type": "integer"}
MONEY = {
    "description": "Exact decimal amount with two places; never binary floating point. Currency is a separate field.",
    "type": "string",
    "pattern": r"^-?[0-9]+\.[0-9]{2}$",
}
OBJECT = {"type": "object"}
STRINGS = {"type": "array", "items": STRING}


def named_file_summary() -> dict:
    return obj(
        {
            "id": INTEGER,
            **{k: STRING for k in ("co", "name", "created_at", "currency")},
            "status": {"enum": ["open", "imported", "confirmed"]},
            "payment_count": INTEGER,
            "total": MONEY,
            "changed_since_download": BOOL,
            "question": {"enum": ["missing", "orphan", None]},
            "import_in": NULL_STRING,
            "confirmed_count": INTEGER,
            "skipped_count": INTEGER,
            "downloaded_at": NULL_STRING,
            "imported_at": NULL_STRING,
        }
    )


def ref(name: str) -> dict:
    return {"$ref": "#/components/schemas/" + name}


def array(items: dict) -> dict:
    return {"type": "array", "items": items}


def obj(fields: dict) -> dict:
    return {"type": "object", "properties": fields, "required": list(fields)}


def schemas() -> dict:
    links = {"type": "object", "additionalProperties": {"oneOf": [STRING, {"type": "null"}, STRINGS]}}

    def entity(fields: dict) -> dict:
        return obj({**fields, "links": links})

    def strings(*names: str) -> dict:
        return {n: STRING for n in names}

    def nullable(*names: str) -> dict:
        return {n: NULL_STRING for n in names}

    def bools(*names: str) -> dict:
        return {n: BOOL for n in names}

    credit = obj({**strings("id", "date", "currency"), "amount": MONEY})
    company = strings("key", "name", "legal_name", "palette")
    received = {
        **strings("id", "company", "supplier", "number", "currency", "status"),
        **nullable("issue_date", "due_date", "payment_state", "paid_at", "bon", "paid_detail"),
        "paid_method": {"enum": ["bank", "card", "cash", "own", "other", None]},
        "total": MONEY,
        "credits": array(obj({"id": STRING, "number": STRING, "amount": MONEY, "date": NULL_STRING})),
        "credit_question": array(obj({"id": STRING, "number": STRING, "amount": MONEY, "date": NULL_STRING})),
        "credit_note": BOOL,
        "covered": MONEY,
        "left": MONEY,
        "previous_iban_last4": STRINGS,
        "accounts": array(obj({"index": INTEGER, "bank": STRING, "ending": STRING})),
        **bools("overdue", "can_approve", "can_mark_paid", "can_withdraw", "can_reopen", "proved"),
    }
    issued = {
        **strings("id", "company", "number", "client", "currency", "status", "note"),
        **nullable("issue_date", "due_date", "collected_at", "collected_on", "how"),
        "collected_method": {"enum": ["bank", "cash", "card", "other", None]},
        "days_overdue": INTEGER,
        **{n: MONEY for n in ("total", "credited", "covered", "owed", "left")},
        **bools("can_mark", "can_unmark", "can_resolve", "can_storno", "can_upload", "pdf", "rejected"),
        "order_id": NULL_STRING,
        "order_sheet_id": NULL_STRING,
        "sales_origin": STRING,
        "order_number": NULL_STRING,
        "awb": {"type": ["object", "null"]},
        "cod_at_courier": BOOL,
        "origin": {"enum": ["fiscalm", "spv"]},
        "local_id": NULL_STRING,
        "storno_of": NULL_STRING,
        "reversed_by": STRINGS,
        "spv": {
            **obj(
                {
                    "state": STRING,
                    "errors": STRINGS,
                    "at": NULL_STRING,
                    "environment": {
                        "enum": ["prod", "test"],
                        "description": "Effective ANAF environment; TEST has no fiscal value.",
                    },
                    "upload_on": {
                        **NULL_STRING,
                        "description": "Not yet sent: the first day (YYYY-MM-DD) the scheduled tick may send it to SPV "
                        "on its own, and it keeps trying after that day (upload_after_days of the firm's spv_auto_upload rule); null when only a person can send it (auto-upload off, Sandbox, "
                        "or not required).",
                    },
                }
            ),
            "required": ["state", "errors", "at"],
        },
    }
    history = obj(
        {
            **strings("id", "company", "number", "party_name", "issue_date", "currency"),
            "kind": {"const": "history"},
            "sources": array({"const": "history"}),
            "direction": {"enum": ["issued", "received"]},
            **nullable("party_cui", "document", "partner"),
            "total": MONEY,
            "credit_note": BOOL,
        }
    )
    history["description"] = (
        "An invoice from before fiscalm (history import) that e-Factura sync has not brought: listed in Vânzări "
        "(issued) or Cheltuieli (received) beside tracked ones, with no payment state. Never owed, matched, paid "
        "or reminded; no balance reads it, only turnover (Turnover). document: the digest its PDF opens by in "
        "history-document, null for XML. partner: partners[].id, by the same identity rules as tracked invoices."
    )
    turnover = obj(
        {
            "sales": ref("Sums"),
            "expenses": ref("Sums"),
            "history": obj({"sales": ref("Sums"), "expenses": ref("Sums")}),
        }
    )
    turnover["description"] = (
        "What was invoiced over time, never a balance: sales are issued invoices, expenses received ones, each at "
        "its total on its issue date in its currency, and every credit note negative on its own date, tracked or "
        "from history alike. history is the part of each sum that came from history invoices."
    )
    payment = {
        **strings("kind", "payee", "ref", "details"),
        "amount": MONEY,
        **nullable("due", "ef_id", "paid_at"),
        "waits_statement": {
            **BOOL,
            "description": "A held payment fiscalm queued on its own (or of unknown origin) that waits for last "
            "month's statement of the paying account; one a person added never waits for it.",
        },
    }
    document = {
        "document_type": {
            "enum": ["invoice", "receipt"],
            "description": "Fiscal type from the document title or matching UBL, independent of mail provenance or card/payment metadata. Receipts may expect a linked invoice. Earlier mailed files are classified once from their stored content; unreadable files remain receipts.",
        },
        **strings("id", "company", "vendor", "month", "currency", "note"),
        **nullable("date", "invoice", "card", "partner", "number"),
        "amount": {
            "anyOf": [MONEY, {"type": "null"}],
            "description": "Null while the document is not read yet: its amount is unknown, not zero.",
        },
        "can_correct": BOOL,
        "sent": BOOL,
        "held": {
            **BOOL,
            "description": "An uploaded file no card payment names yet: it stays out of the accountant's pack until a "
            "person uploads it from that payment's sheet (pack.held_upload).",
        },
        "files": array(obj(strings("id", "type"))),
        "mail": {"anyOf": [obj({**strings("sender", "subject"), **nullable("date", "number")}), {"type": "null"}]},
    }
    bank = obj(
        {
            "statement": BOOL,
            "error": {"enum": ["missing", "password", "format", "unreadable", None]},
            "balanced": {"type": ["boolean", "null"]},
            "card": array(
                obj(
                    {
                        **strings("date", "who", "orig", "currency"),
                        "direction": {"enum": ["debit", "credit"]},
                        "kind": {"enum": ["card", "transfer"]},
                        "amount": MONEY,
                        "doc": NULL_STRING,
                        "awaiting": BOOL,
                    }
                )
            ),
            "efactura_suppliers": INTEGER,
            "advances": array(obj({"date": STRING, "amount": MONEY})),
        }
    )
    stale = {
        **BOOL,
        "description": "The prepared, unsent pack no longer matches its documents or summary: "
        "pack-prepare makes a new version. Never true once sent.",
    }
    report = {
        "month": STRING,
        "status": STRING,
        "sent_on": NULL_STRING,
        "prepared_at": {**NULL_STRING, "description": "When the unsent pack version was prepared; null otherwise."},
        "version": NULL_STRING,
        "mark": {
            **STRING,
            "description": "The pack's send/undo revision: pack-sent and pack-unsent pass it back and are "
            "refused once it changed.",
        },
        "stale": stale,
        "docs": array(obj({**nullable("vendor", "date"), **strings("kind", "note")})),
        "not_mailed": STRINGS,
        **{k: STRINGS for k in ("received", "documents", "issued", "to_check")},
        "bank": {"anyOf": [bank, {"type": "null"}]},
    }
    anaf_status = obj(
        {
            "checked_at": STRING,
            "vat_since": NULL_STRING,
            "inactive_register_removed_at": NULL_STRING,
            "registration_status": STRING,
            **{
                n: {"type": ["boolean", "null"]}
                for n in ("vat_payer", "efactura_registered", "inactive", "vat_on_collection")
            },
        }
    )
    partner = obj(
        {
            **strings("id", "company", "name", "last"),
            **nullable("cui", "vat_id", "country", "client_id", "ro_vat_status"),
            "held": array(obj({"invoice": STRING, "amount": MONEY, "currency": STRING})),
            "report_formats": array(obj({"signature": STRING, "source": STRING, "mapping": OBJECT, "courier": STRING})),
            "address": {"type": ["object", "null"]},
            "iban_last4": STRINGS,
            "roles": STRINGS,
            **{n: INTEGER for n in ("received", "issued", "late")},
            "payable": OBJECT,
            "receivable": OBJECT,
            "turnover": ref("Turnover"),
            "anaf_status": {"anyOf": [anaf_status, {"type": "null"}]},
            "anaf_error": {"enum": [None, "unavailable", "not_found", "invalid_response"]},
            "anaf_eligible": BOOL,
            "autopay": {"type": ["boolean", "null"]},
            "can_set_autopay": BOOL,
            "not_mailed": {"type": ["boolean", "null"]},
            "can_set_not_mailed": BOOL,
        }
    )
    paid_to = array(obj(strings("kind", "payee", "ref", "details")))
    link = obj(
        {
            **strings("id", "tx", "note", "by", "at", "currency"),
            "kind": {"enum": [*KINDS, "receipt"]},
            "partner": NULL_STRING,
            "other": {**NULL_STRING, "description": "A transfer's other end; for kind receipt, the bon's document id."},
            "parts": array(obj({"id": STRING, "amount": MONEY, "closed": BOOL})),
            "payments": paid_to,
            "reason": {"enum": [None, *REASONS, *LABELS]},
            "difference": MONEY,
            "rule": {
                "enum": [None, "saved", "bank", "transfer"],
                "description": "Made by no person: a saved rule (saved), the bank's own fee or interest line (bank), or "
                "both ends of a transfer between the firm's accounts, one naming the other's account (transfer). "
                "Undoing or changing it leaves the transaction to people.",
            },
        }
    )

    def mine(fields: dict) -> dict:  # an explanation the user made carries it (fiscalm.links); the engine's don't
        return obj(fields) | {"properties": {**fields, "link": ref("TxLink")}}

    explanation = {
        "oneOf": [
            mine({"kind": {"const": "payment"}, "invoices": STRINGS, "payments": paid_to}),
            mine({"kind": {"const": "collection"}, "issued": STRINGS, "settlement": NULL_STRING}),
            mine({"kind": {"const": "receipt"}, "invoices": STRINGS, "document": NULL_STRING}),
            obj(
                {
                    "kind": {"const": "label"},
                    "label": {"enum": list(LABELS)},
                    "note": NULL_STRING,
                    "link": ref("TxLink"),
                }
            ),
            obj({"kind": {"const": "advance"}, "who": STRING, "link": ref("TxLink")}),
            obj({"kind": {"const": "transfer"}, "other": NULL_STRING, "link": ref("TxLink")}),
            {"type": "null"},
        ]
    }
    proposal = {
        "oneOf": [
            obj({"kind": {"const": "invoices"}, "invoices": STRINGS}),
            obj({"kind": {"const": "receipt"}, "document": STRING}),
            obj({"kind": {"const": "settlement"}, "settlement": STRING}),
            obj(
                {
                    "kind": {"const": "remembered"},
                    "memory": STRING,
                    "as": obj({"kind": {"enum": list(KINDS)}, **nullable("partner", "reason", "note")}),
                }
            ),
            {"type": "null"},
        ]
    }
    transaction = obj(
        {
            **strings("id", "company", "date"),
            "direction": {"enum": ["debit", "credit"]},
            "amount": MONEY,
            "currency": STRING,
            **nullable("account", "counterparty", "details", "card", "card_owner"),
            "source": {"enum": ["statement", "bank"]},
            "orig": {"anyOf": [obj({"amount": MONEY, "currency": STRING}), {"type": "null"}]},
            "explanation": explanation,
            "proposal": proposal,
            "bank_says": {
                "enum": ["bank_fee", "interest", None],
                "description": "What the bank's own codes or words say the line is (a fee or interest); a default, not an explanation.",
            },
            "awaiting": {
                "anyOf": [obj({**strings("note", "by", "at")}), {"type": "null"}],
                "description": "Aștept factura (tx-await): a debit still to explain whose invoice has not arrived.",
            },
            "partner": NULL_STRING,
            "can_link": BOOL,
            "remember": {"type": "object", "additionalProperties": STRING},
        }
    )
    sums = obj({"total": MONEY, "other": array(obj({"currency": STRING, "total": MONEY}))})
    top = array(obj({"partner": NULL_STRING, **strings("company", "name"), **sums["properties"], "count": INTEGER}))
    product_period = sales_schemas()["ProductSales"]["properties"]
    product_period = obj({k: product_period[k] for k in ("company", "complete", "unavailable_count", "totals")})
    period = obj(
        {
            **strings("from", "to"),
            **{k: ref("Sums") for k in ("in", "out", "left")},
            "suppliers": top,
            "clients": top,
            "cash_status": {"enum": ["complete", "partial", "none"]},
            "rankings": array(obj({"currency": STRING, "clients": top, "suppliers": top})),
            "product_sales": array(product_period),
        }
    )
    period["description"] = (
        "Clients/suppliers rank native RON; rankings contains a separate top five for each currency. "
        "Positive net totals only, credit notes on their issue dates; skipped purchases excluded. "
        "Count is invoice/credit-note documents, history included. "
        "ALL keeps company-scoped partner identities."
    )
    chart_money = obj({key: ref("Sums") for key in ("in", "out", "net")})
    chart_month = obj(
        {
            "month": STRING,
            "in_progress": BOOL,
            "cash_status": {"enum": ["complete", "partial", "none"]},
            "observed": chart_money,
            **{k: ref("Sums") for k in ("invoiced", "collected", "received", "paid")},
            **{key: {"anyOf": [ref("Sums"), {"type": "null"}]} for key in ("in", "out", "net")},
        }
    )
    aging = obj(
        {
            "buckets": array(
                obj({"bucket": {"enum": list(home_charts.AGING)}, **sums["properties"], "count": INTEGER})
            ),
            "confirming": obj({**sums["properties"], "count": INTEGER}),
        }
    )
    charts = obj(
        {
            "months": array(chart_month),
            "aging": obj({side: aging for side in ("receivables", "payables")}),
            "product_sales": array(ref("ProductSales")),
            "vat": array(
                obj(
                    {
                        "month": STRING,
                        "estimate": {"const": True},
                        "basis": {"const": "recorded_invoices"},
                        "in_progress": BOOL,
                        "partial": BOOL,
                        "missing_count": INTEGER,
                        **{k: ref("Sums") for k in ("collected", "deductible", "net")},
                    }
                )
            ),
            "balance_history": array(
                obj(
                    {
                        **strings("company", "iban_last4", "currency", "bank"),
                        "points": array(obj({"day": STRING, "balance": MONEY})),
                    }
                )
            ),
            "balance_total": obj({**sums["properties"], "partial": BOOL, "day": NULL_STRING}),
        }
    )
    charts["description"] = (
        "Twelve months, oldest first. Native RON in total; other currencies separately, no conversion. "
        "Cash totals are null unless every configured current account has full trustworthy statement coverage. "
        "Observed includes deduplicated bank-feed lines and partial statements. No balance interpolation. "
        "Invoice series net credit notes on their own issue dates, including imported history. Collections/payments "
        "use bank booking dates and proof amounts; non-bank payment proofs use the recorded proof date. "
        "Aging covers tracked invoice rows only: payment orders such as taxes remain in payable but are outside invoice aging. "
        "Aging excludes unproved marks from debt buckets and keeps them in confirming. "
        "VAT is an invoice-tax estimate, not a declaration or deduction-eligibility decision. Missing tax evidence "
        "sets partial and missing_count; history without tax evidence is missing. Skipped purchases are excluded. "
        "Never infer tax from gross totals or convert currencies. Product sales reuse ProductSales through today, "
        "one result per firm even in ALL; period summaries group only like product ids, currencies and units. "
        "Product stats cover authored and external SPV detail, excluding history-only imports. "
        "External line nets with fractional cents are unavailable, keeping period and full-range cent totals consistent."
    )
    dashboard = obj(
        {
            "periods": obj(
                {k: ref("DashboardPeriod") for k in ("month", "quarter", "half_year", "trailing_year", "year")}
            ),
            "months": array(obj({"month": STRING, "in": ref("Sums"), "out": ref("Sums")})),
            "balances": array(
                obj(
                    {
                        **strings("company", "iban_last4", "currency", "bank"),
                        "balance": {"anyOf": [MONEY, {"type": "null"}]},
                        "day": NULL_STRING,
                    }
                )
            ),
            "through": NULL_STRING,
            "upcoming": array(
                obj(
                    {
                        **strings("company", "date", "id", "who", "number", "currency"),
                        "side": {"enum": ["in", "out"]},
                        "kind": {"enum": ["invoice", "payment", "issued"]},
                        "amount": MONEY,
                    }
                )
            ),
            "to_explain": INTEGER,
            "charts": charts,
        }
    )
    reasons = obj({d: obj({k: array({"enum": list(REASONS)}) for k in ("less", "more")}) for d in ("credit", "debit")})
    remembered = array(
        obj({**strings("id", "by", "shown", "kind", "note", "who_by", "at"), **nullable("partner", "reason")})
    )
    state = {
        "co": STRING,
        "dashboard": ref("Dashboard"),
        "dashboard_pages": ref("DashboardPages"),
        "task_summary": ref("TaskPage"),
        "accountant": obj({"cui": STRING, "senders": STRINGS, "recipient": STRING}),
        "transactions": obj(
            {"rows": array(transaction), "to_explain": INTEGER, "reasons": reasons, "remembered": remembered}
        ),
        "setup": {
            "description": "Administrators only: what the Configurează card on Acasă lists as not yet set up for this "
            "firm, in order: bank, history, shops (`int`: the shop setup sheet in Setări › Integrări; null for bank and history). "
            "Installation services live only in Setări › Integrări; `hidden` when an administrator closed the "
            "card for this firm. Null when nothing is left or the session can't manage firms.",
            "anyOf": [
                obj(
                    {
                        "left": array(
                            obj(
                                {
                                    "key": {"enum": ["bank", "history", "shops"]},
                                    "int": NULL_STRING,
                                }
                            )
                        ),
                        "hidden": BOOL,
                    }
                ),
                {"type": "null"},
            ],
        },
        "live": BOOL,
        "today": STRING,
        "partners": array(partner),
        "recurrences": array(ref("Recurrence")),
        "recurring_refused": array(obj(strings("id", "client", "recurrence", "why", "at"))),
        "companies": array(obj({**company, "late": INTEGER, "logo": NULL_STRING})),
        "payable": OBJECT,
        "receivable": OBJECT,
        "invoices": array(
            obj(received | {"payment_file": {"anyOf": [obj({"id": INTEGER, "name": STRING}), {"type": "null"}]}})
        ),
        "firm_cards": STRINGS,
        "issued": array(obj(issued)),
        "history": array(history),
        "documents": array(obj(document)),
        "expenses": array(
            obj(
                {
                    **strings("id", "company"),
                    **nullable("invoice", "document"),
                    "sources": array({"enum": list(expenses.SOURCE_ORDER)}),
                }
            )
        ),
        "inbox": array(obj(strings("id", "sha", "uploaded_at", "link_label", "file_type"))),
        "settlements": array(
            obj(
                {
                    **strings("id", "kind", "company", "partner", "source", "report_key", "status", "date", "currency"),
                    "credit": {"anyOf": [credit, {"type": "null"}]},
                    "reference": NULL_STRING,
                    "candidate": {
                        "anyOf": [obj({**strings("id", "date", "amount", "currency", "payer")}), {"type": "null"}]
                    },
                    "invoice_count": INTEGER,
                    "can_confirm": BOOL,
                    "human_confirmed": BOOL,
                    "payer": obj(strings("id", "name")),
                    **{n: MONEY for n in ("gross", "fee", "net")},
                    **nullable("error", "tx_key"),
                    "failed_line": {"type": ["integer", "null"]},
                    "sheet": obj(strings("co", "set")),
                    "links": obj({"transaction": NULL_STRING, "issued": STRINGS}),
                    "lines": array(
                        obj(
                            {
                                "position": INTEGER,
                                **nullable("reference", "awb", "order", "invoice"),
                                "gross": MONEY,
                                "fee": MONEY,
                                "proved": BOOL,
                            }
                        )
                    ),
                }
            )
        ),
        "settlement_tasks": array(
            obj(
                {
                    **strings("kind", "id", "company", "reason"),
                    "line": {"type": ["integer", "null"]},
                    "credit": {"anyOf": [credit, {"type": "null"}]},
                    "invoice_count": INTEGER,
                    "net": MONEY,
                    "currency": STRING,
                    "sheet": obj(strings("co", "set")),
                }
            )
        ),
        "settlement_transactions": array(
            obj(
                {
                    **strings("id", "company", "settlement", "currency"),
                    "fee": MONEY,
                    "sheet": obj(strings("co", "tx")),
                    "links": obj({"settlement": STRING, "issued": STRINGS}),
                }
            )
        ),
        "cod": obj(
            {
                "count": INTEGER,
                "totals": array(obj({"currency": STRING, "total": MONEY})),
                "overdue_days": INTEGER,
                "rows": array(
                    obj(
                        {
                            **strings("id", "company", "invoice"),
                            "awb": AWB,
                            **bools("waiting", "returned", "collected", "overdue"),
                        }
                    )
                ),
                "tasks": array(
                    obj(
                        {
                            **strings("kind", "id", "company", "invoice"),
                            "sheet": obj(strings("co", "iss")),
                            "suggestion": NULL_STRING,
                        }
                    )
                ),
            }
        ),
        "settlement_fees": array(obj(strings("report_id", "amount", "currency", "kind"))),
        "orders": array(ref("OrderSheet")),
        "order_tasks": array(ref("OrderTask")),
        "sales_origins": array(obj({"id": STRING, "company": NULL_STRING, "label": STRING})),
        "cod_receivable": obj(
            {"total": MONEY, "other": array(obj({"currency": STRING, "total": MONEY})), "count": INTEGER}
        ),
        "emag_tasks": array(
            obj(
                {
                    **strings("id", "external_id", "reason"),
                    "order_id": {"type": ["string", "null"]},
                    "draft_id": {"type": ["string", "null"]},
                }
            )
        ),
        "reminder_tasks": array(obj(strings("kind", "id", "label", "subject", "body"))),
        "partner_tasks": array(
            obj(
                {
                    "kind": {"enum": ["inactive_supplier", "inactive_client"]},
                    **strings("id", "company", "label", "detail", "checked_at"),
                }
            )
        ),
        "upload_tasks": array(obj(strings("kind", "id"))),
        "statement_hold": {"anyOf": [obj({"month": STRING, "count": INTEGER}), {"type": "null"}]},
        "files": array(named_file_summary()),
        "payment_file_defaults": array(
            obj({"co": STRING, "name": STRING, "open_file_id": {"type": ["integer", "null"]}})
        ),
        "reports": array(obj({**report, "to_check": STRINGS})),
        "turnover": obj(
            {
                "years": array(
                    obj(
                        {
                            "year": STRING,
                            "turnover": ref("Turnover"),
                            "months": array(obj({"month": STRING, "turnover": ref("Turnover")})),
                        }
                    )
                )
            }
        ),
        **{k: array(OBJECT) for k in ("to_check", "news", "ambiguous", "bank_connections")},
        "unreadable": array(obj({"id": STRING, "listed": NULL_STRING})),
        "refused": array(
            obj(
                {
                    "id": STRING,
                    "iban_last4": STRING,
                    "supplier": STRING,
                    "why": {"enum": ["unclear", "supplier_inactive", "ready"]},
                    "refused_checked_at": NULL_STRING,
                    "still_inactive": {"type": ["boolean", "null"]},
                }
            )
        )
        | {
            "description": "Stored automation refusals with current eligibility. Reading state never prepares payments."
        },
        "system": {
            **OBJECT,
            "description": "Installation health, running version, telemetry (enabled, forced_off by FISCALM_TELEMETRY=0), and updates: enabled (daily anonymous GitHub Releases check), release (validated tag_name, html_url, published_at of a newer stable SemVer, or null), command (manual update command for this install kind).",
        },
        "rules": array(ref("Rule")),
    }
    action = obj(
        {
            "ok": BOOL,
            "message": STRING,
            "next": {"type": ["object", "null"]},
            "done": {"type": ["object", "null"]},
            "state": {"anyOf": [ref("State"), {"type": "null"}]},
        }
    )
    action["properties"]["linked"] = obj({"id": STRING})  # tx-link only
    out = {
        "Meta": obj({"product": STRING, "version": STRING}),
        "Rule": obj(
            {
                **strings("id", "company"),
                "kind": {
                    "enum": [
                        "spv_auto_upload",
                        "shop_auto_issue",
                        "accountant_pack",
                        "receipt_upload",
                        "supplier_autopay",
                        "client_reminders",
                    ]
                },
                **bools("enabled", "can_pause", "can_resume", "can_edit"),
                **nullable("label"),
                "waiting_manual": INTEGER,
                "waiting_queue": INTEGER,
                "reason": {
                    "enum": [
                        None,
                        "paused",
                        "shadow_mode",
                        "anaf_not_connected",
                        "supplier_inactive",
                        "invoice_refused",
                        "revoked",
                        "expired",
                        "exhausted",
                        "invalid_settings",
                    ]
                },
                "settings": {
                    "anyOf": [
                        obj(
                            {
                                "after_days": INTEGER,
                                "source": {"enum": ["firm", "default"]},
                            }
                        ),
                        # spv_auto_upload, only while it is on: days after issue (0 to 5) the tick sends an invoice
                        obj({"upload_after_days": INTEGER}),
                        {"type": "null"},
                    ]
                },
                "last": {"anyOf": [obj({"action": STRING, "date": NULL_STRING, "count": INTEGER}), {"type": "null"}]},
            }
        ),
        "Company": entity(company),
        "Received": entity(received),
        "Issued": entity(issued),
        "Document": entity(document),
        "Payment": entity({**payment, **strings("id", "company", "status", "month")}),
        "Report": entity({"company": STRING, **report}),
        "State": obj(state),
        "Sums": sums,
        "Turnover": turnover,
        "DashboardPeriod": period,
        "Dashboard": dashboard,
        "TxLink": link,
        "Action": action,
        "Error": obj({"error": obj({"code": STRING, "message": STRING})}),
        "File": {"type": "string", "format": "binary"},
        "OpenAPI": OBJECT,
        "UploadLinkList": array(
            obj(
                {
                    **strings("id", "company", "label", "created_at", "expires_at"),
                    **nullable("revoked_at", "last_used_at"),
                    "uploads": INTEGER,
                    "max_files": {"type": ["integer", "null"]},
                }
            )
        ),
        "MCP": {
            "type": "object",
            "description": "JSON-RPC 2.0, no batches, notifications return 202 without execution. Use tools/list for scope-filtered input schemas, tools/call for execution, ping for health. No resource or prompt capability is advertised. Legacy 2025-11-25 uses initialize; subsequent HTTP requests require MCP-Protocol-Version. Modern 2026-07-28 uses server/discover and params._meta io.modelcontextprotocol/protocolVersion plus clientCapabilities; HTTP requires matching MCP-Protocol-Version, Mcp-Method and Mcp-Name for tools/call. HTTP Accept includes application/json and text/event-stream. Tools return JSON text and structuredContent on success, isError on refusal. Invoice issue/storno tools accept idempotency_key with the same semantics as the HTTP header.",
        },
    }
    descriptions = {
        "Received": {
            "status": "open; linked to a payment file; marked by a user awaiting proof; paid with confirmation; or skipped.",
            "payment_state": "For a linked invoice: held, in_file, lost or imported; otherwise null.",
            "proved": "True only with payment evidence. A manual paid mark is not bank or mail proof.",
            "bon": "Linked receipt document ID, or null. Never infer a link from matching amounts alone.",
            "paid_method": "How the person who marked it said they paid (bank, card, cash, own, other), or null. Never proof.",
            "paid_detail": "card: the card's last four digits; own: who paid; other: the person's note; else null.",
            "covered": "Amount covered by explicit transaction links.",
            "credits": "Unambiguously linked received credit notes, scoped to this company, supplier and currency.",
            "credit_question": "For a credit note naming no invoice: the invoices it could cancel (same supplier and "
            "currency, exact opposite total, issued up to 14 days before), when several fit or SPV order or an archived invoice "
            "leaves one in doubt. None is cancelled until credit-pick.",
            "credit_note": "An unmatched received credit note; never payable.",
            "left": "Remaining payable amount after credit notes and explicit links; zero when closed.",
        },
        "Issued": {
            "origin": "fiscalm for an invoice issued here; spv for one read back from ANAF (issued elsewhere).",
            "local_id": "The fiscalm invoice this row collects for (its commands and files); null for one issued elsewhere.",
            "status": "A user's collected mark awaits proof; a statement-backed collection is distinct and cannot be unmarked.",
            "covered": "Amount covered by explicit transaction links.",
            "credited": "Total of issued credit notes against this invoice.",
            "owed": "Outstanding amount after credit notes and explicit coverage.",
            "left": "Remaining amount to collect; zero once closed.",
            "can_resolve": "Whether an uncertain upload can currently be resolved after checking SPV; never retry blindly.",
            "collected_method": "For a user's mark: how they said the client paid (bank: payment order, cash, card, other), "
            "else null. Never proof.",
            "note": "For a user's mark collected another way (other): the person's note; else empty.",
        },
        "Document": {
            "id": "Company-owned document SHA-256, not a filesystem path.",
            "files": "Opaque file IDs for guarded downloads; never storage paths.",
        },
        "State": {
            "live": "False without state/LIVE. Configuration may be allowed, but money actions and outward writes refuse.",
            "today": "Engine date in ISO YYYY-MM-DD; clients do not recalculate due dates or money rules.",
            "firm_cards": "Last four digits of the cards a person said belong to this company; mark-paid card choices.",
            "companies": "The company switch: names, palette, overdue count and logo, the SHA-256 of the current invoice "
            "logo (null without one) for invoicing/logo?co=&sha=.",
            "parts": "Per-company state under ALL. Money and identities never merge across firms.",
            "expenses": "Stored invoice/receipt links only. Ambiguous or contradictory links stay separate; no fuzzy matching here.",
            "statement_hold": "Automatic payments held until the bank's record of the paying account covers every day of "
            "last month (month, YYYY-MM): a statement, CSV exports or the open banking feed. One task with the count; "
            "uploading the statement releases them. Null when none wait.",
        },
        "Action": {
            "message": "Romanian toast; clients should use error codes and capability flags, not parse this text.",
            "state": "Refreshed state, or null if the action committed but state reload failed. Read again; do not repeat blindly.",
            "next": "Optional engine navigation target.",
            "done": "Optional identifiers/results returned by the completed action. Named-file actions return the file and download identifiers when relevant.",
            "linked": "tx-link only: the explanation's id (a transaction's explanation.link.id), not an invoice.",
        },
    }
    for name, fields in descriptions.items():
        for field, description in fields.items():
            if field in out[name]["properties"]:
                out[name]["properties"][field] = out[name]["properties"][field] | {"description": description}
    for name in ("Company", "Received", "Issued", "Document", "Payment"):
        out[name + "Page"] = obj({"data": array(ref(name)), "next_cursor": NULL_STRING})
    purchase = obj(
        {
            **strings("id", "company"),
            "sources": array({"enum": list(expenses.SOURCE_ORDER)}),
            **{
                name: {"anyOf": [state[source]["items"], {"type": "null"}]}
                for name, source in (("invoice", "invoices"), ("document", "documents"))
            },
        }
    )
    report_data = {
        "purchase": purchase,
        "issued": state["issued"]["items"],
        "card": bank["properties"]["card"]["items"],
        "bank": bank,
        "mail": state["to_check"]["items"],
        "ambiguous": state["ambiguous"]["items"],
        "advance": bank["properties"]["advances"]["items"],
        "document": report["docs"]["items"],
        "not_mailed": STRING,
    }
    report_row = {
        "oneOf": [
            obj({**strings("id", "company"), "kind": {"const": kind}, "data": data})
            for kind, data in report_data.items()
        ]
    }
    target = obj(strings("inv", "iss", "doc", "set", "tab"))
    target["required"] = []
    target["properties"]["tab"] = {"enum": ["plati", "sarcini"]}
    said = obj({"text": STRING, "to": target, "proposal": BOOL})
    said["required"] = ["text"]
    transaction_row = obj(
        {**strings("id", "company"), "transaction": transaction, "said": {"anyOf": [said, {"type": "null"}]}}
    )
    out["BrowsePage"] = obj(
        {
            "data": array(
                {
                    "anyOf": [
                        state["issued"]["items"],
                        history,
                        state["partners"]["items"],
                        purchase,
                        report_row,
                        transaction_row,
                    ]
                }
            ),
            "total": INTEGER,
            "next_cursor": NULL_STRING,
            "months": STRINGS,
        }
    )
    out["BrowsePage"]["properties"]["data"]["maxItems"] = 200
    out["BrowsePage"]["description"] = (
        "One current app list page. Sales, purchases and partners are newest first with stable source-order ties. "
        "Filters run on complete entities; "
        "total counts all matches regardless of cursor, months covers the complete category before filters. "
        "Sales and partners return existing state rows; purchases/receipts return id, company, invoice and document. "
        "Sales and purchases also list the firm's history invoices (kind history), never open or late. "
        "Sales alone accept f=storno (can_storno); partners alone accept f=accountant (a 2–10 digit CUI). "
        "Report sections require month (YYYY-MM), preserve section/source order and carry id/company/kind/data; "
        "they refuse f/q and include full ambiguous-candidate counts before nested bounding. "
        "A purchase scopes its invoice/receipt independently to the month's existing report references. "
        "Transactions return transaction plus said (text/optional target/proposal), derived from complete identities. "
        "They accept f=empty (unexplained), all, in or out; from/to are inclusive ISO dates and apply only to transactions. "
        "Their search includes bank text, raw/comma/grouped amounts and the same displayed explanation/proposal. "
        "Nested record lists are capped at 50; financial totals and currency aggregates stay complete. "
        "Cursor contains only date and ordinal, never payment keys; replace pages after writes/filter changes."
    )
    out["DashboardPage"] = obj(
        {
            "data": array(
                {
                    "anyOf": [
                        dashboard["properties"]["upcoming"]["items"],
                        obj({**strings("id", "company", "partner", "currency"), "name": NULL_STRING, "amount": MONEY}),
                    ]
                }
            ),
            "total": INTEGER,
            "next_cursor": NULL_STRING,
        }
    )
    out["DashboardPage"]["description"] = (
        "One page (default 50, maximum 200) of upcoming or held_money, with its complete total row count. "
        "Upcoming keeps full dashboard order: date, side, who, then original company/source order. "
        "Held amounts sum every exposure per partner/currency before paging, preserving company/partner/currency order. "
        "Cursor contains only company/source positions, never payment keys or names. Reads rebuild one firm at a time; "
        "no snapshot or cache. Replace pages after data/company changes. Only collection/limit/cursor are accepted."
    )
    out["DashboardPages"] = obj({collection: ref("DashboardPage") for collection in dashboard_rows.COLLECTIONS})
    out["DashboardPages"]["description"] = (
        "Initial 50-row panels built from the same complete state as totals. Continue using dashboard-list "
        "with the panel's cursor and the same company/ALL selection. No additional initial projection is required."
    )
    out["ReportPages"] = obj(
        {
            "data": array(obj({"screen": {"enum": list(browsing.REPORT_SCREENS)}, "page": ref("BrowsePage")})),
            "total": {"const": len(browsing.REPORT_SCREENS)},
            "next_cursor": {"type": "null"},
        }
    )
    out["ReportPages"]["description"] = (
        "Initial pages for one company's existing report month, built from one complete projection. "
        "Each section has the requested limit (default 50, maximum 200), its exact total and its own browse cursor. "
        "Continue a section through browse with the same screen/month/limit. No cross-request cache or snapshot."
    )
    out["SearchResults"] = obj(
        {
            name: {**array(obj({field: state[name]["items"]["properties"][field] for field in fields})), "maxItems": 6}
            for name, fields in searching.FIELDS
        }
    )
    out["SearchResults"]["description"] = (
        "Six newest matches per category across complete company entities, with original identities and exact amounts. "
        "Every query word must match text or an amount prefix; decimal input fixes the whole units, negative input "
        "matches negative amounts only. Matching ignores case, combining diacritics and a tax ID's RO prefix. "
        "Queries below two normalized characters return empty categories; the app lists sections locally."
    )
    out["TaskEntry"] = obj({"key": STRING, "company": STRING, "urgent": BOOL, "patch": OBJECT})
    out["TaskEntry"]["description"] = (
        "Inputs for one existing task card, not a complete entity or decision. Merge patch into the company's "
        "summary for rendering only. Nested record lists contain at most 50 entries; use entity reads for details. "
        "Full file counts and monetary totals are scalars, never derived from the page. Key and urgency preserve "
        "task ordering and deduplication across companies."
    )
    out["TaskPage"] = obj(
        {
            "total": INTEGER,
            "urgent": INTEGER,
            "group": {"const": ""},
            "data": array(ref("TaskEntry")),
            "highlights": array(ref("TaskEntry")),
            "next_cursor": NULL_STRING,
        }
    )
    out["TaskPage"]["properties"]["data"]["maxItems"] = 200
    out["TaskPage"]["properties"]["highlights"]["maxItems"] = 3
    out["TaskPage"]["description"] = (
        "Complete deduplicated task counts, one bounded page, and the first three urgent tasks (otherwise first three). "
        "The cursor is a nonnegative offset; reads are current, so refresh after writes. File questions are per-file Home tasks linking to their detail."
    )
    counts = {"type": "object", "additionalProperties": INTEGER}
    summary = {k: {"$ref": "#/components/schemas/State/properties/" + k} for k in state}
    summary.update(
        accountant=obj(
            {
                **state["accountant"]["properties"],
                "partner": {"anyOf": [obj({"id": STRING, "name": NULL_STRING, "cui": STRING}), {"type": "null"}]},
            }
        ),
        upcoming_count=INTEGER,
        bounded={"const": True},
        task_summary=ref("TaskPage"),
        record_counts=counts,
        invoice_counts=counts,
        reports=array(obj({**report, "to_check": STRINGS, "counts": counts})),
    )
    out["StateSummary"] = obj(summary)
    partner_data = {
        "invoices": {"anyOf": [ref("State/properties/invoices/items"), ref("State/properties/history/items")]},
        "issued": {"anyOf": [ref("State/properties/issued/items"), ref("State/properties/history/items")]},
        "transactions": obj(
            {
                **strings("id", "company"),
                "transaction": ref("State/properties/transactions/properties/rows/items"),
                "said": {"anyOf": [said, {"type": "null"}]},
            }
        ),
        "held": obj({"invoice": STRING, "number": NULL_STRING, "amount": MONEY, "currency": STRING}),
        "settlements": ref("State/properties/settlements/items"),
        "remembered": obj(
            {"id": STRING, "memory": ref("State/properties/transactions/properties/remembered/items"), "text": STRING}
        ),
        "report_formats": ref("State/properties/partners/items/properties/report_formats/items"),
    }
    partner_pages = obj(
        {
            name: obj({"data": {**array(data), "maxItems": 200}, "total": INTEGER, "next_cursor": NULL_STRING})
            for name, data in partner_data.items()
        }
    )
    partner_pages["required"] = []
    out["PartnerPages"] = obj({"state": ref("StateSummary"), "pages": partner_pages})
    out["PartnerPages"]["description"] = (
        "Partner sheet: one company shell with the complete partner identity, capabilities and monetary balances; "
        "its held/report-format histories are empty. Without collection, pages contains all seven collections "
        "from one complete projection. With collection, only that page is returned. Each page has its exact total, "
        "a nonnegative offset cursor and at most limit rows (default 50, maximum 200). Invoices and issued list "
        "the partner's history invoices too (kind history). Invoice order is newest "
        "issue date first, stable on ties; other collections retain source order. Transaction descriptions and "
        "remembered labels use complete identities before paging; held invoice numbers are explicit. Other nested "
        "record lists are capped at 50. Omit id only for the company's complete remembered collection. The app "
        "accepts ALL with id and resolves the partner's owning company directly; bearer ALL remains refused. "
        "Reads are current, not a cross-request snapshot; replace pages after writes."
    )
    out["TransactionInvoiceChoice"] = obj(
        {
            **strings("id", "number", "currency"),
            **nullable("date", "partner"),
            "left": MONEY,
            "total": MONEY,
            "selected": BOOL,
            "position": INTEGER,
        }
    )
    out["PaymentRow"] = obj(payment)
    choice_types = {
        "partners": ref("State/properties/partners/items"),
        "invoices": ref("TransactionInvoiceChoice"),
        "selected": ref("TransactionInvoiceChoice"),
        "proposed": ref("TransactionInvoiceChoice"),
        "payments": obj({"id": STRING, "payment": ref("PaymentRow")}),
        "receipts": ref("State/properties/documents/items"),
    }
    choice_pages = obj(
        {
            name: obj({"data": {**array(row), "maxItems": 200}, "total": INTEGER, "next_cursor": NULL_STRING})
            for name, row in choice_types.items()
        }
    )
    choice_pages["required"] = []
    out["TransactionChoices"] = obj({"pages": choice_pages})
    out["TransactionChoices"]["description"] = (
        "Current transaction-form choices, without linking or settling anything. Without collection, one full owner "
        "projection returns initial partners, invoices, payments, selected and receipts pages; with collection, just that page. "
        "Each has an exact matching total and offset cursor (default 50, maximum 200). Only partners of the transaction's "
        "direction are offered, including a credit holder's complete exposure. Invoices retain the existing open/marked, "
        "SPV-ready, non-rejected, positive-left and account/original-currency filters; amounts restore this link's own "
        "contribution before editing. Closed items selected by the current link remain available. selected pages all "
        "existing invoice choices independently of the primary row's nested-list cap; position preserves invoice order "
        "across pages/search. Invoices sort by date then case-folded number; partners by case-folded display name. "
        "Prepared payments retain held/in-file/lost/imported order, for debits with RON account or original currency only. "
        "Payment page IDs are keyed opaque identifiers; raw item_id is private and never appears in a cursor. "
        "receipts lists, for a debit only, the firm's uploaded files with no amount yet (held, unsent, without an "
        "invoice), in the documents order: the bon a person may name for it with tx-receipt. "
        "q is an accent-insensitive display-text substring (maximum 200 characters); it excludes private payment keys. "
        "partner applies only to invoices or the initial bundle; selected always uses the current link's partner. "
        "Only the initial bundle has proposed: at most the one open invoice of either partner whose number the bank details "
        "name, when the transaction equals its remaining amount in its currency (never for an explained transaction); "
        "its partner is the default partner before the one the bank text points to. A proposal is never applied: the user confirms it. "
        "Reads are current, not a cross-request snapshot; replace choices after writes."
    )
    out["CoveredRow"] = {
        "oneOf": [
            obj(
                {
                    "kind": {"const": "invoice"},
                    **strings("id", "number", "currency"),
                    "amount": MONEY,
                    "left": MONEY,
                    "closed": BOOL,
                }
            ),
            obj({"kind": {"const": "payment"}, "id": STRING, "payment": paid_to["items"]}),
        ]
    }
    out["StateDetail"] = obj(
        {
            "state": {
                **ref("StateSummary"),
                "description": "Company shell with only the requested document and its purchase, refusal and partner context populated.",
            },
            "related": obj(
                {
                    "data": {
                        **array(
                            {
                                "anyOf": [
                                    received["credits"]["items"],
                                    state["issued"]["items"],
                                    ref("State/properties/settlements/items/properties/lines/items"),
                                    ref("CoveredRow"),
                                ]
                            }
                        ),
                        "maxItems": 200,
                    },
                    "total": INTEGER,
                    "next_cursor": NULL_STRING,
                }
            ),
        }
    )
    out["StateDetail"]["properties"]["transaction"] = obj(
        {
            "said": {"anyOf": [said, {"type": "null"}]},
            "parts_count": INTEGER,
            "payments_count": INTEGER,
            "choices": ref("TransactionChoices/properties/pages"),
        }
    )
    decimal = {"type": "string", "pattern": r"^[+-]?[0-9]+(\.[0-9]+)?$", "description": "Decimal number as written."}
    out["StateDetail"]["properties"]["lines"] = obj(
        {
            "lines": {
                **array(
                    obj(
                        {
                            "position": INTEGER,
                            **strings("description", "unit", "vat_category"),
                            "quantity": decimal,
                            "unit_price": {"anyOf": [decimal, {"type": "null"}]},
                            "net": decimal,
                            "vat_rate": {"anyOf": [decimal, {"type": "null"}]},
                            "vat": {"anyOf": [decimal, {"type": "null"}]},
                            "gross": {"anyOf": [decimal, {"type": "null"}]},
                        }
                    )
                ),
                "maxItems": 200,
            },
            "currency": STRING,
            "truncated": BOOL,
            "unavailable": BOOL,
            "totals": {
                "anyOf": [
                    obj(
                        {
                            "net": decimal,
                            "vat": decimal,
                            "total": decimal,
                            "payable": decimal,
                            "taxes": array(
                                obj(
                                    {
                                        **strings("category"),
                                        "rate": {"anyOf": [decimal, {"type": "null"}]},
                                        "net": decimal,
                                        "vat": decimal,
                                    }
                                )
                            ),
                        }
                    ),
                    {"type": "null"},
                ]
            },
        }
    )
    out["StateDetail"]["properties"]["lines"]["properties"]["lines"]["items"]["properties"]["product_id"] = {
        "type": "string",
        "description": "The authored line's same-firm product link, when present. Line values remain invoice snapshots.",
    }
    out["StateDetail"]["description"] = (
        "One document, settlement, transaction or eMAG-task sheet, including an issued invoice's local-id alias. Money, identities and capabilities "
        "come from the complete company projection. related pages received credit notes, issued reversals or settlement lines "
        "in their existing order (default 50, maximum 200); its cursor continues through this same endpoint. "
        "The selected invoice contains the current related page. Receipt articles remain complete (at most 40). "
        "Other nested historical lists are capped at 50. Reads are current, not a cross-request snapshot. "
        "Order id uses the existing company:task_id sheet link; issued context identifies an already issued draft. "
        "The app also accepts ALL, reading only candidate owners of global IDs (or the first offered mail company), "
        "without retaining other firms' histories. Settlement totals/counts and confirmation capabilities precede paging."
        " For transaction sheets only, transaction contains the complete said descriptor and exact link part/payment counts; "
        "related pages covered invoices then prepared payments in source order. Invoice numbers, remaining amounts and "
        "currencies have the existing missing-reference fallbacks. The shell keeps the selected transaction, its transfer "
        "counterpart, selected partner identities and settlement fee context; no unbounded form-choice arrays are included. "
        "The primary transaction's nested lists are capped at 50; use related and its exact total for covered rows."
        " Invoice and issued sheets also carry lines: an invoice written here lists its stored lines, one read from "
        "SPV the lines of its stored XML (at most 200, truncated says more exist; descriptions at most 300 characters). "
        "unavailable means no stored document could be read, or one was refused or malformed; every line is checked, "
        "also those past the 200 returned, so it is never partial. "
        "Lines are display only: the totals above remain the invoice's amounts. A line's vat and gross are its net "
        "times vat_rate, rounded half up to the cent (null without a rate), so they can differ from the document's "
        "VAT by rounding. totals are the document's own: the totals an invoice written here was issued with "
        "(payable is its total), or the stored XML's LegalMonetaryTotal and its TaxTotal in the document currency, "
        "one tax per subtotal; null when they are missing or not decimal numbers, never summed from the lines. "
        "A null unit_price means a valid price "
        "per a positive base quantity other than one; a missing price or a malformed base quantity makes the "
        "document unavailable."
        " Editable transactions also bundle five initial transaction-choices pages from the same owner projection; "
        "immutable transactions carry an empty choices object. Continue/filter through transaction-choices."
    )
    out["StateSummary"]["description"] = (
        "App shell with complete monetary totals, rankings, task counts and three bounded task highlights. "
        "Historical entity, payment, transaction and task-source lists are empty. Report rows contain metadata "
        "and complete counts only; dashboard_pages retains bounded initial panels and their continuation cursors. The app endpoint also accepts ALL and returns "
        "per-company shells and deduplicated global tasks. Company/configuration identities and currency "
        "aggregates stay complete. Scalar counters describe the original full collections before trimming; "
        "record_counts.remembered counts the company's complete remembered transaction explanations."
        " accountant.partner preserves the selected partner's id/name/CUI, or null if unavailable, without its history. "
        "Private mutation summaries may populate at most one changed-client partner and one issued invoice for "
        "canonical navigation; their nested history lists are bounded to 50. GET summaries keep those lists empty."
    )
    out["StateListPage"] = obj({"data": array(OBJECT), "next_cursor": NULL_STRING, "total": INTEGER})
    out["StateListPage"]["description"] = (
        "Existing state rows, at most 200, ordered by id (upcoming by date:side:who:opaque_id; candidates by engine date/number order). "
        "total counts all rows matching filters regardless of the cursor. id retrieves one row independently "
        "of the cursor and filters, including an issued invoice's local_id; upcoming uses cursor_id. "
        "candidates requires tx from the same company and uses opaque cursor_id; id accepts public payment IDs. "
        "Partner identities are computed on complete company data before paging. Reads are current, "
        "not snapshot-isolated across requests; replace pages after writes. This bounds transport, "
        "not the work needed to project a single company."
    )
    out.update(commerce_schemas())
    out.update(response_schemas())
    out.update(product_schemas())
    out.update(sales_schemas())
    out["NamedPaymentFileSummary"] = named_file_summary()
    out["NamedPaymentFile"] = obj(
        named_file_summary()["properties"]
        | {
            "payments": array(
                obj(
                    {
                        "id": STRING,
                        **strings("payee", "kind", "ref", "details", "currency"),
                        "amount": MONEY,
                        "due": NULL_STRING,
                        "ef_id": NULL_STRING,
                        "state": {"enum": ["in_file", "imported", "paid", "skipped", "lost"]},
                        "proved": BOOL,
                        "paid_at": NULL_STRING,
                        "added_after_download": BOOL,
                    }
                )
            ),
            "downloads": array(obj({"id": INTEGER, "version": STRING, "at": STRING})),
            **{k: BOOL for k in ("can_add", "can_rename", "can_remove", "can_delete", "can_download", "can_import")},
            "questions": array(
                {
                    "oneOf": [
                        obj({"kind": {"const": "missing"}, "version": STRING, "count": INTEGER}),
                        obj({"kind": {"const": "orphan"}, "name": STRING}),
                    ]
                }
            ),
        }
    )
    return out


def state_list_params() -> list[dict]:
    return [
        {"name": "collection", "in": "query", "required": True, "schema": {"enum": list(lists.COLLECTIONS)}},
        *({"name": n, "in": "query", "schema": STRING} for n in ("id", "partner", "tx")),
    ]


def _download_contract(route, operation: dict, response: dict) -> None:
    if route.resource == "pack_file":
        operation["parameters"].append(
            {
                "name": "version",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            }
        )
        response["content"] = {"application/zip": {"schema": ref("File")}}
    if route.resource in ("invoicing_pdf", "invoicing_xml"):
        mime = "application/pdf" if route.resource.endswith("pdf") else "application/xml"
        response["content"] = {mime: {"schema": ref("File")}}
    if route.resource in ("pack_file", "invoicing_pdf", "invoicing_xml"):
        response["headers"]["Content-Disposition"] = {"schema": STRING}


def openapi() -> dict:
    paths = {}
    for route in ROUTES:
        params = [
            {
                "name": n,
                "in": "path",
                "required": True,
                "schema": {"type": "string", **({"enum": list(actions.API_ACTIONS)} if n == "action" else {})},
            }
            for n in re.findall(r"{(\w+)}", route.path)
        ]
        if route.schema.endswith("Page"):
            params += [
                {
                    "name": "limit",
                    "in": "query",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                {"name": "cursor", "in": "query", "schema": STRING},
            ]
            if route.resource not in ("companies", "state_tasks", "state_browse", "dashboard_list"):
                params += [{"name": n, "in": "query", "schema": STRING} for n in ("status", "month")]
        params += {
            "transaction_choices": [
                {"name": "id", "in": "query", "required": True, "schema": STRING},
                {
                    "name": "collection",
                    "in": "query",
                    "schema": {"type": "string", "enum": list(transaction_choices.COLLECTIONS)},
                },
                {"name": "partner", "in": "query", "schema": STRING},
                {"name": "q", "in": "query", "schema": {"type": "string", "maxLength": 200}},
                {"name": "cursor", "in": "query", "schema": STRING},
                {
                    "name": "limit",
                    "in": "query",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
            ],
            "partner_pages": [
                {"name": "id", "in": "query", "schema": STRING},
                {"name": "collection", "in": "query", "schema": {"enum": list(partner_rows.COLLECTIONS)}},
                {"name": "cursor", "in": "query", "schema": STRING},
                {
                    "name": "limit",
                    "in": "query",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
            ],
            "state_detail": [
                {"name": "kind", "in": "query", "required": True, "schema": {"enum": list(details.KINDS)}},
                {"name": "id", "in": "query", "required": True, "schema": STRING},
                {"name": "cursor", "in": "query", "schema": STRING},
                {
                    "name": "limit",
                    "in": "query",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
            ],
            "state_search": [{"name": "q", "in": "query", "schema": STRING}],
            "dashboard_list": [
                {
                    "name": "collection",
                    "in": "query",
                    "required": True,
                    "schema": {"enum": list(dashboard_rows.COLLECTIONS)},
                }
            ],
            "state_report_pages": [
                {"name": "month", "in": "query", "required": True, "schema": STRING},
                {
                    "name": "limit",
                    "in": "query",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
            ],
            "state_list": state_list_params(),
            "state_browse": [
                {"name": "screen", "in": "query", "required": True, "schema": {"enum": list(browsing.SCREENS)}},
                {
                    "name": "f",
                    "in": "query",
                    "schema": {"enum": ["", "open", "late", "all", "in", "out", "storno", "accountant"]},
                },
                *({"name": n, "in": "query", "schema": STRING} for n in ("q", "month")),
                *({"name": n, "in": "query", "schema": {"type": "string", "format": "date"}} for n in ("from", "to")),
            ],
        }.get(route.resource, [])
        params += [{"name": "group", "in": "query", "schema": {"const": ""}}] if route.resource == "state_tasks" else []
        response = {
            "description": "Success",
            "content": {
                ("application/octet-stream" if route.schema == "File" else "application/json"): {
                    "schema": ref(route.schema)
                }
            },
            "headers": {"Fiscalm-API-Version": {"schema": {"const": "1"}}},
        }
        if route.schema == "File":
            response["content"] = {mime: {"schema": ref("File")} for mime in sorted(set(api.MEDIA.values()))}
        operation = {
            "operationId": route.resource,
            "security": [] if route.scope == "public" else [{"bearerAuth": []}],
            "parameters": params,
            "responses": {
                "200": response,
                **{
                    str(code): {
                        "description": "Request refused",
                        "content": {"application/json": {"schema": ref("Error")}},
                    }
                    for code in (400, 401, 403, 404, 405, 409, 413, 415, 500)
                },
            },
        }
        if route.resource in ("action", "upload_link_create", "upload_link_revoke"):
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": {"type": ["string", "integer"]},
                            "description": "Flat string/integer fields, no booleans. co, if supplied, must match the URL. See x-action-fields for each action. Integration changes require app authentication and return app_required for bearer requests.",
                        }
                    }
                },
            }

        if route.resource == "action":
            operation["x-action-fields"] = {
                name: (spec.public_description or spec.fields)
                + f" Effect: {spec.effect}; "
                + ("allowed in shadow." if spec.effect == "local" else "requires LIVE.")
                for name, spec in actions.ACTIONS.items()
                if spec.public
            }
        if route.resource == "upload_link_create":
            operation["requestBody"]["content"]["application/json"]["schema"] = {
                "type": "object",
                "required": ["label"],
                "additionalProperties": False,
                "properties": {
                    "co": STRING,
                    "label": {"type": "string", "minLength": 1, "maxLength": 100},
                    "all_firms": {
                        "type": "boolean",
                        "default": False,
                        "description": "Accept documents for every configured firm; unclear buyers wait for assignment.",
                    },
                    "expires_in_days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1, "default": 100},
                },
            }

        if route.resource == "upload_link_revoke":
            operation["requestBody"]["content"]["application/json"]["schema"] = {
                "type": "object",
                "properties": {"co": STRING},
                "additionalProperties": False,
            }
        if route.resource.startswith("invoicing_") and route.scope == "write":
            op = route.resource[10:]
            if op == "auto-upload":
                operation["description"] = (
                    "App-admin configuration only. Bearer requests always return 403 admin_required."
                )
                operation["responses"].pop("200")
                operation["responses"]["403"]["description"] = (
                    "App admin required; bearer tokens cannot change automation rules."
                )
            body_schema = invoicing_api.ACTIONS[op].public_schema
            if op in ("draft-save", "issue"):
                operation["description"] = (
                    "Echo the draft version read from the workspace. Existing-draft saves require it; "
                    "issue always requires it. Stale versions return stale_draft (409)."
                )
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": body_schema | {"properties": body_schema["properties"] | {"co": STRING}}
                    }
                },
            }
            if op in ("issue", "storno"):
                params.append(
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$", "maxLength": 128},
                        "description": "Per token and company for 24 hours. Same operation and JSON body replays the exact committed result; changed operation/body returns 409. Retry a 500 with the same key/body. Refusals are not cached. Issue/storno and replay commit atomically; issuing never uploads to SPV (see spv.upload_on). Other routes reject this header.",
                    }
                )
        _download_contract(route, operation, response)
        if route.resource == "orders_create":
            operation["description"] = (
                "Atomic shop order intake; live-only lifecycle webhooks use HMAC-SHA256(timestamp + dot + raw JSON), X-Fiscalm-Signature, durable retries and stable event IDs. See x-shop-webhooks in this schema."
            )
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": ORDER_BODY}}}
        if route.resource in ("orders_awb", "orders_cancel", "orders_refund", "orders_discard", "orders_delivery"):
            body_schema = (
                AWB
                if route.resource == "orders_awb"
                else commerce_obj(
                    {
                        "lines": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                        }
                    },
                    [],
                )
                if route.resource == "orders_refund"
                else commerce_obj({})
            )
            if route.resource == "orders_delivery":
                body_schema = commerce_obj(
                    {k: {"type": ["string", "null"], "format": "date"} for k in ("picked_up_at", "returned_at")}
                )
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": body_schema}}}
        if route.resource == "orders_list":
            params.append({"name": "channel", "in": "query", "schema": STRING})
        if route.resource == "mcp":
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": OBJECT}}}
            operation["responses"]["202"] = {"description": "Notification accepted, no response body"}
        paths.setdefault("/api/v1" + route.path, {})[route.method.lower()] = operation
    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{PRODUCT} API",
            "version": "1.0.0",
            "description": "Seller-save, the series operations (create, continue, default, remove) and logo operations are app/CLI configuration, not exposed by API v1. See docs/api.md.",
        },
        "paths": paths,
        "x-shop-webhooks": {
            "events": [
                "invoice.issued",
                "invoice.spv_accepted",
                "invoice.spv_rejected",
                "invoice.collected",
                "order.refused",
            ],
            "description": "Live-only, at-least-once delivery to operator-configured webhook_url with webhook_secret. Never accept a destination from an order; redirects refuse. Verify the signature against the raw body and check freshness before deduplicating by event ID. Non-2xx/transport errors retry with exponential backoff capped at one hour; crashes can redeliver the same event. Only statement-confirmed collection emits invoice.collected, never shop payment metadata.",
            "headers": {
                "X-Fiscalm-Timestamp": "ASCII Unix seconds used in the signature; reject stale timestamps.",
                "X-Fiscalm-Event-Id": "Stable event ID, identical to payload id. Deduplicate after signature verification.",
                "X-Fiscalm-Signature": "sha256=<hex HMAC-SHA256(webhook_secret, ASCII timestamp + '.' + raw UTF-8 body)>",
            },
            "payload": {
                "id": "Stable event ID",
                "type": "One of events",
                "company": "Company key",
                "order_id": "Order ID; may be null on intake refusal",
                "external_id": "Shop order reference",
                "channel": "Shop channel ID",
                "invoice_id": "Invoice ID; may be null on intake refusal",
                "reason": "Optional safe refusal code, never raw SPV, buyer, bank or exception data",
            },
        },
        "x-browser-auth": {
            "origin": "Pinned FISCALM_PUBLIC_URL HTTPS origin; Host is checked even for public routes, except /healthz. Forwarded headers are ignored unless the backend is private to an explicitly trusted proxy.",
            "cookie": {
                "name": sessions.COOKIE,
                "attributes": sessions.ATTRIBUTES,
                "idle_days": 14,
                "absolute_days": 30,
                "step_up_minutes": 10,
            },
            "me": {
                "method": "GET",
                "path": "/web/api/me",
                "session": "any",
                "response": "me (id, username, display_name, role, can), auth (via, method, recovery_only), factors (password_totp, passkeys_available, passkeys). Passkey metadata: id (base64url), name, backup_eligible, backed_up, created_at, last_used_at; no public-key material or secrets. A missing/expired/insecure cookie returns 401 with methods.password=true and methods.passkey indicating DNS-origin support.",
            },
            "bootstrap": {
                "method": "GET",
                "path": "/web/api/bootstrap",
                "session": "any",
                "query": "co: selected firm key or ALL (default). Unknown firms select ALL.",
                "response": "identity: the /me shape; state: state-summary including Acasă first dashboard pages (null for recovery-only sessions); live: boolean. Identity, authorization and state share one database snapshot of the request cookie. Match identity user, role, auth and current session digest against the displayed identity before applying state; discard replies across session-change generations. No raw session id is returned.",
            },
            "post": {
                endpoint.path: {
                    "session": endpoint.session,
                    "body": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            field: STRING | ({"writeOnly": True} if field != "username" else {})
                            for field in endpoint.fields.split()
                        },
                        "required": endpoint.fields.split(),
                    },
                }
                for endpoint in login.ENDPOINTS
            },
            "transport": "POST requires a matching Origin, application/json, at most 4096 bytes, no ambiguous framing, and same-origin Sec-Fetch-Site when supplied. No CORS. One shared 20-per-minute IP budget across authentication flows, persistent account lockout and 5-try single-use factor tickets.",
            "responses": "Password returns only a ticket, never a session. TOTP or verified passkey sign-in sets the HttpOnly cookie. Setup begin returns ticket, secret and otpauth URI; setup finish returns 10 recovery codes and sets the cookie. link means only the setup URL fragment. Recovery-only sessions can read me, replace the factor permitted by their proof, or sign out. Finishing passkey recovery clears every session and the cookie; finishing TOTP recovery closes every other session and sets a normal cookie, since the password, a recovery code and the new TOTP were all proved. auth.method is password_totp, passkey, recovery (password plus recovery code), or recovery_code (code-only passkey recovery). signout takes {} and clears the cookie. No raw session id is in JSON.",
            "passkeys": {
                "origin": "Requires a DNS host in the pinned HTTPS origin; HTTPS IP installs refuse passkey operations with 400. Changing the RP host requires re-enrolment or CLI/admin reset.",
                "options": "Options endpoints return {ok,ticket,publicKey}. publicKey is WebAuthn JSON options with unpadded base64url binary values, UV required, and discoverable credentials. Registration requests residentKey required, attestation none and ES256/EdDSA/RS256. No account name is submitted for passkey sign-in; step-up is fixed to the current cookie's account.",
                "proof": "All binary proof fields are canonical unpadded base64url strings. transports is a JSON-array string of browser hints, never identity proof. Challenge/type/origin/RP/UP/UV, exact credential owner and userHandle, signature and counter are checked. A positive counter must increase, including refusal of a return to zero. One-use tickets are five minutes and five attempts. Failed proof commits its attempt; final publication errors roll back factor/counter, ticket and session together.",
                "registration": "setup/passkey uses a current one-time factorless-account setup link. me/passkey add/rename/remove requires a normal recent own session of any role under the write lock. No body user id is accepted. Last-factor removal refuses. Adding a key closes other own sessions; setup/sign-in/step-up rotate the presented cookie atomically.",
                "recovery": "recover/code {username,code} is the one entry, refused with an existing browser session. Every refusal is the same 401, so nothing tells whether the account exists or which factor it uses before one 80-bit code is spent. For an account with a complete password/TOTP method it returns only {ok,ticket}; recover/password {username,ticket,password} then proves the password (a forged or spent ticket counts as a wrong password) and issues recovery/recovery_only access confined to TOTP replacement. For a passkey-only account it issues recovery_code/recovery_only access at once; recover/passkey options/finish accept only that exact proof/session/user, replace all old keys and password/TOTP, rotate codes, close every session/ticket/link and clear the cookie, so fresh key sign-in is required. Recovery provenance cannot cross flows.",
            },
            "own_account": "GET /web/api/me includes active sessions (one-way id digest, current flag, method, created_at, last_seen_at, device (for example Chrome pe Linux, named by the engine from the user agent), ip) and recovery_codes_remaining. POST /auth/me/recovery-codes regenerates ten codes once, voiding the old set; /auth/me/session-close accepts only an own session id, clearing the cookie when current; /auth/me/sessions-close-others keeps the current session. Password replacement POST /auth/me/password-change {password} hashes outside the write lock, rechecks the proved credentials and closes other sessions and pending tickets; TOTP replacement POST /auth/me/totp/begin {} returns a pending secret/URI/ticket, and /auth/me/totp/finish {ticket,code} activates only that session’s verified replacement and regenerates codes. The old factor works until commit; no password-only factor/removal path exists. All own-account mutations require a normal session of any role plus step-up within ten minutes, rechecked under the write lock. No supplied user id is accepted. Recovery-only sessions are refused.",
            "sensitive_actions": {
                "status": 401,
                "body": {"ok": False, "reauth": True},
                "description": "A normal cookie session needs fresh authentication for user administration, session-close, token creation. Step-up takes the identity from the current cookie, never from a submitted username.",
            },
            "document_upload": {
                "method": "POST",
                "path": "/web/api/document-upload?co=<company key>[&tx=<transaction id>]",
                "body": "multipart/form-data, 1 to 10 parts named files: JPEG, PNG, HEIC or PDF by their first bytes, 15 MiB each, 40 MiB per request. Budgeted like a Bondrop link per member (60 files and 50 MiB an hour, 250 MiB a day), within the company's shared upload budget.",
                "role": "member",
                "response": "{ok, files: [{sha, duplicate}]} in request order. Each file is stored like a Bondrop upload, named by its sha256 inside the company's own inbox, and becomes a receipt to check. No file name is read. With tx (one file only), the file is the bon of that card debit of the company: it takes the debit's amount, day, month and card and explains it, through the same guarded link as tx-link (kind receipt); a debit already explained, a credit, or a bon another debit names or that has its own data is refused with 409 and a message, and the file stays stored. Allowed in Sandbox. Refusals: 400 framing or query, 401/403 session, role or Origin, 408 too slow, 409 storage, 413 too large or too many, 415 other types, 429 budget used, 503 busy, 507 disk full.",
            },
        },
        "x-app-token-management": {
            "list": "/web/api/tokens: normal accounts see only their own metadata; administrators see all, including revoked historical ownerless entries.",
            "create": "/web/api/token-create {name,scope}: fresh normal session; current user is owner. Administrator/member may choose read or write; readonly may choose read only. Returns done.id/token/scope once.",
            "revoke": "/web/api/token-revoke {id}: normal session, own token only; administrators may revoke any. Owner, enabled actor and live administrator status are checked inside the same write lock. No body owner/actor override is accepted.",
            "capabilities": "me.can.own_tokens permits the own-token surface; me.can.manage_tokens permits administrator visibility/revocation; me.can.write limits the offered scopes. Bearer credentials never manage credentials.",
        },
        "x-app-user-administration": {
            "role": "manage_users",
            "get": {
                "/web/api/users": [
                    "id",
                    "username",
                    "display_name",
                    "role",
                    "disabled_at",
                    "locked_until",
                    "password_totp",
                    "passkeys",
                    "last_signin",
                ],
                "/web/api/user-sessions?id=<user id>": [
                    "id",
                    "method",
                    "created_at",
                    "last_seen_at",
                    "device",
                    "ip",
                ],
                "/web/api/auth-events": "Latest 100 events, newest first: at, kind, user_id, actor, detail. No secrets.",
            },
            "post": {
                name: {"path": "/web/api/" + name, "fields": spec.fields.split()}
                for name, spec in actions.ACTIONS.items()
                if spec.role == "manage_users"
            },
            "description": "Normal administrator cookie required. Every mutation also requires step-up within 10 minutes, rechecked under the same write lock. Unknown fields and actor/owner overrides are refused. user-create and setup/reset return done.url only once: a relative /setup#id.secret link valid 72 hours. Session id is a stored digest, never the raw cookie. Disabled users lose browser access and tokens immediately. Cannot remove the last working administrator. No company access restrictions.",
        },
        "x-app-integrations": {
            "get": "/web/api/integrations",
            "firm_scope": {"oneOf": [{"const": "all"}, {"type": "array", "items": {"type": "string"}}]},
            "description": "ANAF/mail items carry firm_scope. All includes future firms; explicit keys must exist; [] serves none. Disabled instances do not cover firms.",
            "post": {
                "mail-microsoft-start": "address, optional id, optional firm_scope. New inbox scope defaults to all and is stored before authorization. Scope is refused for existing inbox reconnects, including reuse by address.",
                "integration-add": "kind, name, f_<field>, optional firm_scope (default all). Success: done {id: created instance id}",
                "integration-update": "id, optional name, f_<field>, optional firm_scope (omitted preserves scope)",
            },
            "coverage": {
                "type": "array",
                "description": "One row per firm in company order. anaf is last successful reader if still enabled and covering, else oldest. anaf_also contains other enabled covering connections.",
                "items": {
                    "type": "object",
                    "properties": {
                        "firm": {"type": "string"},
                        "anaf": {"type": ["string", "null"]},
                        "anaf_also": {"type": "array", "items": {"type": "string"}},
                        "mail": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "x-app-ai-cli": {
            "route": "/web/api/integration-cli",
            "effect": "local",
            "role": "connect_integrations",
            "description": "App administrator only. Origin required on POST; JSON up to 4096 bytes. Official CLI-owned login, status and logout. No credentials or raw output are exposed; configuration is allowed in shadow.",
            "get": {"provider": ["claude-cli", "codex-cli"]},
            "post": {
                "op": ["start", "code", "cancel", "logout"],
                "provider": ["claude-cli", "codex-cli"],
                "code": "Only for code: one authorization code, 1..2048 characters, sent to CLI stdin, never persisted or logged.",
            },
            "response": {
                "provider": "CLI provider, or empty before login",
                "state": ["idle", "waiting", "connected", "failed", "expired", "cancelled", "invalid_url"],
                "url": "Only HTTPS on the selected provider's allowlisted domains; no userinfo, fragment or token query parameters",
                "code": "Only the short public device code, otherwise empty",
                "needs_code": "Whether CLI asks for a pasted authorization code",
                "readiness": ["ready", "missing", "not_logged_in"],
                "install_command": "Native install or Docker rebuild command; absent on POST",
            },
        },
        "x-app-product-stats": {
            "method": "GET",
            "path": "/web/api/product-stats",
            "query": "co: one firm (ALL refused); month: YYYY-MM, default current server month; months: 1 or 12, default 1. The selected month ends the range.",
            "response": "ProductSales. Revenue excludes VAT and includes signed storno lines; quantity is decimal text; invoice_count counts distinct documents including storno. Groups are separate per product, currency and unit. Current catalog names include archived products. Lines without a product, including external SPV invoices, use product_id=null and name=Fără produs. Unavailable or truncated external UBL contributes no guessed amount and makes its month and the range incomplete, with unavailable_count. Unparsed SPV messages use their listing month because their issue date is unknown. Authored invoices and their SPV copies are counted once, including unlinked copies with the same firm, number, date and kind. Includes local Sandbox invoices; production-rejected invoices are excluded, matching turnover. Imports kept only in history are outside this report's coverage. All months use two batched queries; each external file is read once.",
        },
        "x-app-products": {
            "bodies": {name: spec.schema for name, spec in actions.ACTIONS.items() if name.startswith("product-")},
            "method": "GET",
            "path": "/web/api/products",
            "query": "co: one firm key (required); id: optional product id for detail. ALL refused. List: q searches name/code; f empty=active or archived; cursor is a nonnegative offset, limit 1..200 (default 50).",
            "response": "Products, or ProductDetail with id; list defaults to active entries, with total and next_cursor. Archived entries use f=archived. InvoicingWorkspace.products contains all active entries for the local picker. Unknown detail returns 404.",
            "writes": "POST /web/api/product-add|product-update|product-archive|product-delete with {co, ...fields}. Member role, local in Sandbox. Bodies in x-app-products.bodies; result.done.product contains the saved Product (delete: id only). Refresh catalog after writes.",
        },
        "x-app-document-currencies": list(docs.CURRENCIES),
        "x-app-action-effects": {name: spec.effect for name, spec in actions.ACTIONS.items()},
        "x-app-action-roles": {name: spec.role for name, spec in actions.ACTIONS.items()},
        "x-invoicing-action-effects": {name: spec.effect for name, spec in invoicing_api.ACTIONS.items()},
        "x-app-actions": {name: spec.description for name, spec in actions.ACTIONS.items() if spec.description},
        "components": {
            "securitySchemes": {
                "sessionCookie": {"type": "apiKey", "in": "cookie", "name": sessions.COOKIE},
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": f"User-owned, scoped install-wide {PRODUCT} token. Rights are scope intersected with the owner’s current role; a disabled owner returns 401, read-only writes return 403 insufficient_scope. All routes require the configured Host and port.",
                },
            },
            "schemas": schemas(),
        },
    }
