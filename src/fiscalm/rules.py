"""Read existing automations and change only their guarded configuration."""

from __future__ import annotations

from fiscalm import companies, integrations
from fiscalm.api import auth, uploads as upload_links
from fiscalm.efactura import client as anaf, inbox as efactura
from fiscalm.invoicing import spv as invoice_spv
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.ledger import Ledger, is_live
from fiscalm.mail import reminders

ADMIN_REFUSAL = "Doar administratorii pot modifica automatizările."
# The toast after Oprește / Pornește repeats the button's verb (AGENTS.md).
CHANGED = {False: "Regula a fost oprită.", True: "Regula a fost pornită."}
# A supplier-autopay rule names its most serious current invoice refusal.
AUTOPAY_REASONS = ("invoice_refused", "supplier_inactive")
# The rules a person can stop and start here.
MUTABLE = ("spv_auto_upload", "shop_auto_issue", "supplier_autopay")


def may_change(*, admin: bool = False, bearer: bool = False) -> bool:
    """Rule configuration is app-admin only, including the legacy invoicing route, in either mode."""
    return auth.require({"role": "admin" if admin else "member"}, "manage_rules", bearer=bearer)


def capabilities(rule: dict, *, admin: bool = False, bearer: bool = False) -> dict:
    may = may_change(admin=admin, bearer=bearer)
    mutable = rule["kind"] in MUTABLE and may
    return dict(
        can_pause=mutable and rule["enabled"],
        can_resume=mutable and not rule["enabled"],
        # broken reminder settings too: saving them repairs them; the SPV delay only while the rule runs (it has no
        # effect otherwise, so the projection carries no settings then)
        can_edit=may and (rule["kind"] == "client_reminders" or rule["kind"] == "spv_auto_upload" and rule["enabled"]),
    )


def list_rules(
    L: Ledger, co: str, registry: dict | None = None, shops: list[integrations.Instance] | None = None
) -> list[dict]:
    """Company-qualified rules; unknown execution history stays null, never inferred. `shops`: the enabled shop
    integrations, when the caller already read them for every firm (api.state, once per request)."""
    if co not in (registry if registry is not None else companies.all(L)):
        raise ValueError("Unknown company")
    result = []

    def add(
        kind: str,
        key: str = "",
        *,
        enabled: bool = True,
        reason: str | None = None,
        label: str | None = None,
        last: dict | None = None,
        waiting_queue: int = 0,
        settings: dict | None = None,
    ) -> None:
        result.append(
            dict(
                id=f"{co}:{kind}" + (f":{key}" if key else ""),
                kind=kind,
                company=co,
                enabled=enabled,
                label=label,
                reason=reason
                or (
                    "paused"
                    if not enabled
                    else "shadow_mode"
                    if kind in ("spv_auto_upload", "shop_auto_issue") and not is_live()
                    else None
                ),
                last=last,
                waiting_manual=invoice_spv.waiting_manual(L, co) if kind == "spv_auto_upload" else 0,
                waiting_queue=waiting_queue,
                settings=settings,
                can_pause=False,
                can_resume=False,
                can_edit=False,
            )
        )

    # Without an ANAF connection nothing can be sent in either mode: that is what the rule waits for first.
    connected = anaf.instance(L, co) is not None and anaf.token_state(L, company=co).get("authorized")
    sends, days = invoice_spv.auto_upload_setting(L, co)
    add(
        "spv_auto_upload",
        enabled=sends,
        reason=None if connected else "anaf_not_connected",
        settings={"upload_after_days": days} if sends else None,
    )
    # A shop's orders become issued invoices on their own only in Live (commerce.orders); in Sandbox they stay drafts.
    for shop in integrations.enabled(L, "shop") if shops is None else shops:
        if shop.config.get("company") == co:
            add("shop_auto_issue", shop.id, enabled=shop.config.get("auto_issue") == "true", label=shop.name)
    packs = L.db.execute(
        "SELECT * FROM packs WHERE company=? AND status IN ('prepared','sent') ORDER BY month DESC", (co,)
    ).fetchall()
    add(
        "accountant_pack",
        last=dict(
            action="download",
            date=(packs[0]["updated_at"] or "")[:10] or None if packs[0]["status"] == "prepared" else None,
            count=len(packs),
        )
        if packs
        else None,
    )
    now = upload_links.timestamp()
    for link in upload_links.list_links(L, co):
        reason = (
            "revoked"
            if link["revoked_at"]
            else "expired"
            if link["expires_at"] <= now
            else "exhausted"
            if link["max_files"] is not None and link["uploads"] >= link["max_files"]
            else None
        )
        add(
            "receipt_upload",
            link["id"],
            enabled=reason is None,
            reason=reason,
            label=link["label"],
            last=dict(action="upload", date=link["last_used_at"][:10], count=link["uploads"])
            if link["last_used_at"]
            else None,
        )
    suppliers = efactura.autopay_suppliers(L, co)
    rows = L.efactura_rows(company=co)
    for cui in sorted({r["supplier_cui"] for r in rows if r["supplier_cui"] in suppliers}):
        reason = None
        waiting = 0
        paused = efactura.autopay_paused(L, co, cui)
        for r in rows:
            if r["supplier_cui"] == cui and r["status"] == efactura.OPEN:
                try:
                    efactura.check_payment(L, r)
                    waiting += int(paused)
                except efactura.ApproveError as exc:
                    why = exc.code if exc.code in efactura.SHOWN_REFUSALS else "invoice_refused"
                    reason = max(reason, why, key=lambda x: AUTOPAY_REASONS.index(x) if x else -1)
        add("supplier_autopay", cui, label=suppliers[cui], reason=reason, enabled=not paused, waiting_queue=waiting)
    try:
        settings = reminders.settings(L, co)
    except reminders.Refused:
        settings = None  # a broken setting prepares nothing; the rule says so instead of guessing
    add("client_reminders", settings=settings, reason=None if settings else "invalid_settings")
    return result


def set_enabled(L: Ledger, co: str, ident: str, enabled: bool) -> None:
    """Set a desired state idempotently; never run the rule or confirm an IBAN."""
    rule = next((r for r in list_rules(L, co) if r["id"] == ident), None)
    if not rule or rule["kind"] not in MUTABLE:
        raise ValueError("Rule cannot be changed")
    if rule["kind"] == "spv_auto_upload":
        invoice_spv.set_auto_upload(L, co, enabled)
    elif rule["kind"] == "shop_auto_issue":
        integrations.update(L, ident.rsplit(":", 1)[1], {"auto_issue": "true" if enabled else "false"})
    else:
        efactura.set_autopay_enabled(L, co, ident.rsplit(":", 1)[1], enabled)


SPV_DELAY_SAVED = "Ziua trimiterii în SPV a fost salvată."


def set_spv_delay(L: Ledger, co: str, days: str) -> int:
    """The firm's SPV upload delay, a digit string 0 to 5; refused while the rule is off (InvoiceError)."""
    if not isinstance(days, str) or not days.isdigit():
        raise InvoiceError("Alege între 0 și 5 zile după emitere.")
    invoice_spv.set_upload_delay(L, co, int(days))
    return int(days)
