"""fiscalm actions (POST api/<action>, docs/openapi.json): the user's answers, through the engine's own guarded code.

Each action takes the company it acts on (never ALL) and string arguments, and returns
{ok, message, next, done}: `message` is the Romanian sentence for the app's toast. Shadow mode refuses
outward effects; local work stays available.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from fiscalm import (
    accountant,
    audit,
    companies,
    company_config as cc,
    docs,
    history_pdf,
    integrations as ig,
    ledger,
    links,
    pack,
    receipt_items,
    rules,
    secrets,
    telemetry,
    updates,
)
from fiscalm.api import limits, public_url, sessions, setup_links, state as api, tokens, uploads as upload_links, users
from fiscalm.api.responses import Error as APIError
from fiscalm.bank import connections as bank_connect, csv as cs, enablebanking
from fiscalm.bank.statements import StatementError
from fiscalm.commerce import emag_sync, orders as commerce
from fiscalm.efactura import client as anaf, inbox as efactura
from fiscalm.invoicing import issued
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.ledger import USER, AuthorizationRefused, Ledger, finance_home
from fiscalm.mail import invoices as mail_invoices, microsoft, reminders, setup as mail_setup
from fiscalm.payments import engine as pay
from fiscalm.payments.orders import PaymentFileError
from fiscalm.products import engine as products
from fiscalm.products.schema import BODIES as PRODUCT_BODIES
from fiscalm.settlements import upload as settlement_upload
from fiscalm.settlements.engine import perform, recheck_pending

FIELD = "f_"  # an integration field in a POST body: f_<key>
SHADOW = "Disponibil doar în modul Live."
BAD = "Cererea nu a putut fi procesată."
STALE_DOWNLOAD = "Fișierul s-a schimbat între timp. Descarcă-l din nou."
ERROR = "Operațiunea nu a reușit. Încearcă din nou peste un minut."
AUTOPAY_ON = "De acum, facturile de la {name} se pun automat în fișierul de plăți."
AUTOPAY_OFF = "Facturile de la {name} nu se mai pun automat în fișierul de plăți."
AUTOPAY_UNKNOWN = "Firma nu are facturi de la acest furnizor."
NOT_MAILED_ON = "Documentele de la {name} nu se mai pun în dosarul pentru contabil."
NOT_MAILED_OFF = "Documentele de la {name} se pun din nou în dosarul pentru contabil."
NOT_MAILED_UNKNOWN = "Firma nu are documente de la acest partener."
NOT_MAILED_PREPARED = "Dosarul pentru contabil este pregătit. Poți schimba regula după ce confirmi trimiterea."


def log(line: str) -> None:
    p = finance_home() / "logs" / f"finance-{date.today():%Y-%m}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        actor = " [" + audit.actor.get() + "]" if audit.actor.get() else ""
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}{actor}\n")


def n_of(n: int, one: str, few: str) -> str:
    """Romanian plural: 1 plată, 2 plăți, 20 de plăți."""
    if n == 1:
        return f"1 {one}"
    return f"{n} de {few}" if n >= 20 else f"{n} {few}"


def _res(ok: bool, message: str, tab: str | None = None, at: str | None = None, done: dict | None = None) -> dict:
    return {"ok": ok, "message": message, "next": {"tab": tab, "at": at} if tab else None, "done": done}


def _tick() -> None:
    """Ask for a tick now: `fiscalm scheduler` polls for this file. A link, FIFO, socket, device or directory at that
    name is refused (OSError): the request never blocks and never touches a file outside the installation."""
    state = finance_home().resolve() / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Non-blocking, so a FIFO at that name cannot hang the request; only a regular file counts as one.
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(state / "tick.request", flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("state/tick.request is not a regular file")
    finally:
        os.close(fd)


def _linked(L: Ledger, done: dict) -> str:
    """The toast after the sheet's button: what the transaction was linked to or marked as, and the invoice a part
    still owed keeps open."""
    if done["kind"] != "documents":
        return {
            "label": "Tranzacția a fost marcată fără document.",
            "advance": "Tranzacția a fost marcată ca avans.",
            "transfer": "Tranzacția a fost marcată ca transfer între conturi."
            + (" Cealaltă parte a transferului a fost găsită și marcată la fel." if done["other"] else ""),
        }[done["kind"]]
    numbers = [(r["number"] if (r := L.issued(p["id"]) or L.efactura(p["id"])) else p["id"]) for p in done["parts"]]
    if not numbers:
        return "Tranzacția a fost asociată cu plata pregătită."
    msg = (
        f"Tranzacția a fost asociată cu factura {numbers[0]}."
        if len(numbers) == 1
        else f"Tranzacția a fost asociată cu {n_of(len(numbers), 'factură', 'facturi')}."
    )
    rest = [(n, p) for n, p in zip(numbers, done["parts"], strict=True) if not p["closed"]]
    if not rest:
        return msg
    owed = "de încasat" if L.issued(rest[0][1]["id"]) else "de plătit"
    return msg + f" Restul facturii {rest[0][0]} rămâne {owed}."


def _receipt_items(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    try:
        revision = int(args.get("revision", ""))
        with L.tx():
            ctx.authorize()
            if ctx.action == "receipt-lines-save":
                payload = json.loads(args.get("payload", ""))
                ok = L.save_receipt_items(co, args.get("id", ""), payload, revision)
            else:
                ok = receipt_items.confirm(L, co, args.get("id", ""), args.get("invoice", ""), revision)
        return _res(
            ok,
            (
                "Articolele au fost salvate."
                if ctx.action == "receipt-lines-save"
                else "Asocierea a fost confirmată. Plata se verifică separat."
            )
            if ok
            else "Documentul s-a schimbat între timp.",
        )
    except (ValueError, TypeError, RecursionError):
        return _res(False, BAD)


def _order_discard(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    try:
        done = commerce.command(L, co, ctx.get("id"), "orders_discard", {}, date.today().isoformat(), ctx.authorize)
        with suppress(OSError):
            ctx.log(
                f"web: {audit.actor.get() or json.dumps(ctx.created_by or 'app')} order-discard company={co} id={done['id']}"
            )
        return _res(True, "", done=done)
    except commerce.Error:
        return _res(False, BAD)


def _accountant_save(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if set(args) != {"co", "cui", "senders", "recipient"}:
        return _res(False, BAD)
    try:
        done = accountant.save(L, co, args["cui"], args["senders"], args["recipient"])
    except (ValueError, KeyError) as e:
        return _res(False, str(e) if isinstance(e, ValueError) else BAD)
    ctx.log(
        f"web: {audit.actor.get() or json.dumps(ctx.created_by or 'app')} accountant-save company={co} senders={len(done['senders'])}"
    )
    return _res(True, "Datele contabilului au fost salvate.", done=done)


def _statement_csv_format(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    keys = set(cs.FIELDS) | {"date_format", "decimal"}
    acct = cs.account_by_id(co, ctx.get("account"))
    if not acct:
        return _res(False, "Lipsește contul pentru care sunt coloanele.")
    try:
        cs.save_mapping(L, co, acct.iban, {k: v for k, v in args.items() if k in keys})
    except StatementError:
        return _res(
            False,
            "Configurarea CSV nu este validă. Verifică în CSV coloanele de dată, detalii și sumă (sau debit și credit). Verifică și formatul datei și separatorul zecimal.",
        )
    return _res(True, "Coloanele au fost salvate.")


def _bank_setup(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if set(args) - {"co"} != {"operation"} or ctx.get("operation") not in ("prepare", "check"):
        return _res(False, BAD)
    try:
        done = bank_connect.prepare(L) if ctx.get("operation") == "prepare" else {"checks": bank_connect.checks(L)}
    except (enablebanking.BankError, public_url.PublicURLError):
        return _res(
            False, "Configurarea nu a putut fi verificată. Verifică cheia, certificatul și adresele instalării."
        )
    return _res(
        True,
        "Configurarea a fost pregătită." if ctx.get("operation") == "prepare" else "Configurarea a fost verificată.",
        done=done,
    )


def _bank_connect(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if set(args) != {"co", "bank"} or not ctx.created_by:
        return _res(False, BAD)
    try:
        url = bank_connect.start(L, co, ctx.get("bank"), date.today(), origin=ctx.origin, created_by=ctx.created_by)
    except enablebanking.BankError as e:
        return _res(False, e.message)
    ctx.log(f"bank-connect {co} {ctx.get('bank')}")
    return _res(True, "Confirmă în aplicația băncii.", done={"url": url})


def _bank_disconnect(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if set(args) != {"co", "bank"} or not ctx.created_by:
        return _res(False, BAD)
    bank_connect.disconnect(L, co, ctx.get("bank"), ctx.authorize)
    ctx.log(f"bank-disconnect {co} {ctx.get('bank')}")
    return _res(True, "Citirea directă a fost oprită. Istoricul și extrasele se păstrează.")


def _bank_accounts(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if not ctx.created_by:
        return _res(False, BAD)
    operation = args.get("operation")
    try:
        if operation == "list" and set(args) == {"co", "operation"}:
            return _res(True, "", done={"offers": bank_connect.pending(L, co, ctx.created_by)})
        if operation == "confirm" and set(args) == {"co", "operation", "offer", "accounts"}:
            done = bank_connect.confirm(
                L, co, ctx.get("offer"), args["accounts"], ctx.created_by, ctx.authorize, today=date.today()
            )
            ctx.log(f"bank-accounts {co} {done['bank']}: confirmed {len(done['read'])} accounts")
            return _res(True, "Conturile au fost conectate.", done=done)
    except (enablebanking.BankError, cc.CompanyError) as exc:
        return _res(False, str(exc))
    except secrets.SecretError:
        return _res(False, "Accesul pregătit nu mai este disponibil. Pornește din nou conectarea.")
    return _res(False, BAD)


def _integration_connect(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if set(args) - {"co"} != {"id"} or not ctx.created_by:  # the app sends the company it shows; ignored here
        return _res(False, BAD)
    try:
        url = anaf.start(L, ctx.get("id"), ctx.created_by)
    except anaf.ConnectError as e:
        ctx.log(f"anaf-connect: refused {e.code}")
        return _res(False, e.message)
    ctx.log("anaf-connect: started")
    return _res(True, "Urmează autentificarea cu certificatul digital la ANAF.", done={"url": url})


def _token_create(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if not ctx.created_by:
        raise PermissionError("session_required")
    with L.tx():
        ctx.authorize()
        done = _token_change(L, args, ctx)
    if done is None:
        message = (
            "Cheia are nevoie de un nume de cel mult 100 de caractere și de un nivel de acces permis contului."
            if ctx.action == "token-create"
            else "Identificatorul cheii API nu este valid."
        )
        return _res(False, message)
    with suppress(OSError):
        ctx.log(f"web: {ctx.action} {done.get('id', args.get('id', ''))}")
    return _res(True, "", done=done)


def _token_change(L: Ledger, args: dict, ctx: Context) -> dict | None:
    if ctx.action == "token-create":
        try:
            return tokens.create(
                L, args.get("name"), args.get("scope"), created_by=ctx.created_by, user_id=ctx.created_by
            )
        except (ValueError, TypeError):
            return None
    ident = args.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{16}", ident):
        return None
    return {"revoked": tokens.revoke(L, ident, actor_id=ctx.created_by)}


def _upload_link_create(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    try:
        try:
            origin = public_url.origin() if ctx.action == "upload-link-create" else None
        except public_url.PublicURLError:
            ctx.log("public address invalid (FISCALM_PUBLIC_URL)")
            return _res(
                False,
                "Linkul Bondrop nu se poate crea: adresa publică a instalării nu este bună. "
                "Cere-i administratorului serverului să o corecteze.",
            )
        done = upload_links.action(L, co, ctx.action, args)
        if origin:
            done["url"] = origin + done["path"]
        ident = done.get("id") if ctx.action == "upload-link-create" else args.get("id", "")
        if isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{16}", ident):
            # Preserve the committed change and the one-time upload path.
            with suppress(OSError):
                ctx.log(f"web: {ctx.action} {ident}")
        return _res(True, "Linkul a fost revocat." if ctx.action == "upload-link-revoke" else "", done=done)
    except (ValueError, TypeError):
        return _res(False, BAD)


def _rule_pause(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    try:
        if args.get("co", co) != co:
            return _res(False, BAD)
        on = ctx.action == "rule-resume"
        rules.set_enabled(L, co, ctx.get("id"), on)
        return _res(True, rules.CHANGED[on], done={"id": ctx.get("id"), "enabled": on})
    except efactura.ApproveError:
        return _res(False, BAD)
    except ValueError:
        return _res(False, BAD)


def _rule_reminders(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    values = {k: v for k, v in args.items() if k != "co"}
    if args.get("co", co) != co or not all(isinstance(v, str) for v in values.values()):
        return _res(False, BAD)
    done = reminders.set_settings(L, co, values)
    ctx.log(f"web: rule-reminders company={co} " + " ".join(f"{k}={v}" for k, v in done.items()))
    return _res(True, reminders.SETTINGS_SAVED, done=done)


def _rule_spv_delay(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if args.get("co", co) != co:
        return _res(False, BAD)
    days = rules.set_spv_delay(L, co, ctx.get("days"))
    ctx.log(f"web: rule-spv-delay company={co} days={days}")
    return _res(True, rules.SPV_DELAY_SAVED, done={"upload_after_days": days})


def _rule_partner_not_mailed(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if args.get("co", co) != co or ctx.get("on") not in ("0", "1"):
        return _res(False, BAD)
    try:
        name = pack.set_not_mailed(L, co, ctx.get("partner"), ctx.get("on") == "1")
    except pack.PackWaiting:
        return _res(False, NOT_MAILED_PREPARED)
    except ValueError:
        return _res(False, NOT_MAILED_UNKNOWN)
    ctx.log(f"web: rule-partner-not-mailed company={co} on={ctx.get('on')}")
    return _res(
        True,
        (NOT_MAILED_ON if ctx.get("on") == "1" else NOT_MAILED_OFF).format(name=name),
        done={"partner": ctx.get("partner"), "on": ctx.get("on") == "1"},
    )


def _rule_supplier_autopay(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    try:
        if args.get("co", co) != co or ctx.get("on") not in ("0", "1"):
            return _res(False, BAD)
        done = efactura.set_supplier_autopay(L, co, ctx.get("cui"), ctx.get("on") == "1")
    except efactura.ApproveError:
        return _res(False, AUTOPAY_UNKNOWN)
    ctx.log(f"web: rule-supplier-autopay company={co} on={ctx.get('on')}")
    if ctx.get("on") != "1":
        return _res(True, AUTOPAY_OFF.format(name=done["name"]), done={"cui": ctx.get("cui"), "on": False})
    n = done["waiting"]
    more = (
        " O factură deschisă intră la următoarea verificare."
        if n == 1
        else f" {n_of(n, 'factură deschisă', 'facturi deschise')} intră la următoarea verificare."
        if n
        else ""
    )
    return _res(True, AUTOPAY_ON.format(name=done["name"]) + more, done={"cui": ctx.get("cui"), "on": True})


def _emag_resolve(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    try:
        if args.get("co", co) != co:
            return _res(False, BAD)
        if ctx.action == "emag-resolve":
            emag_sync.resolve(L, co, ctx.get("id"))
            return _res(True, "Marcată ca rezolvată.")
        emag_sync.refund(L, co, ctx.get("id"), date.today().isoformat())
        return _res(True, "Ciorna de stornare a fost pregătită.")
    except (AuthorizationRefused, APIError):
        raise
    except Exception:
        return _res(False, "Comanda nu poate fi rezolvată. Verifică factura și starea ei în SPV.")


def _settlement_confirm(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    return perform(L, co, args, "user " + ctx.created_by if ctx.created_by else "", ctx.authorize)


def _settlement_withdraw(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    with L.tx():
        ctx.authorize()
        ok = L.withdraw_report(str(args.get("id", "")), co)
        if ok:
            recheck_pending(L, co)
    return _res(ok, "Raportul a fost șters." if ok else "Raportul nu poate fi șters: verifică dacă este deja încasat.")


def _settlement_format_delete(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    with L.tx():
        ctx.authorize()
        ok = settlement_upload.delete_format(L, co, str(args.get("partner", "")), str(args.get("signature", "")))
    return _res(ok, "Formatul a fost șters." if ok else "Formatul nu mai există.")


def check(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    # Same tick as the schedule (safe in shadow too: the tick itself respects the mode).
    try:
        _tick()
    except OSError:
        ctx.log("web: manual check refused: the tick request could not be written")
        return _res(False, "Verificarea nu a putut porni.")
    ctx.log("web: manual check requested")
    return _res(True, "Verificarea a pornit. Rezultatul apare peste un minut, la reîncărcarea paginii.")


def invoice_action(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """approve / mark-paid / reopen / withdraw on one received e-Factura invoice of `co`. Only approve moves the person
    (to its success sheet); the others answer where they were asked, with a toast."""
    mid = ctx.get("id")
    out = _res(False, "Factura nu a fost găsită.")
    row = L.efactura(mid) if mid.isdigit() else None
    if not row or row["company"] != co:
        pass
    elif ctx.action in ("mark-paid", "withdraw"):
        out = _mark_paid(L, co, mid, ctx)
    elif ctx.action == "reopen":
        res = efactura.reopen(L, mid)
        out = _res(
            res is not None,
            "Marcarea a fost anulată. Factura este din nou de plătit."
            if res == "open"
            else "Marcarea a fost anulată. Factura este din nou pregătită pentru fișierul de plăți."
            if res == "queued"
            else "Marcarea acestei facturi nu poate fi anulată.",
        )
    ctx.log(f"web: {ctx.action} {mid[:20]}: {out['message'][:60] or 'ok'}")
    return out


PAID_METHOD_RO = {
    "method": "Alege cum ai plătit.",
    "card": "Alege cardul firmei cu care ai plătit.",
    "detail": "Scrie cine a plătit sau cum s-a plătit.",
    "long": f"Textul poate avea cel mult {efactura.PAID_DETAIL_MAX} de caractere.",
}


def _mark_paid(L: Ledger, co: str, mid: str, ctx: Context) -> dict:
    """mark-paid (an open invoice) or withdraw (one in the payment file), with how the person paid: the marking stays
    awaiting a statement or a mail whatever the method."""
    try:
        how = efactura.paid_method(L, co, ctx.get("method"), ctx.get("detail"))
    except efactura.PaidMethodError as e:
        return _res(False, PAID_METHOD_RO[e.code])
    note = f"{USER} already paid (page)"
    if ctx.action == "mark-paid":
        ok = L.mark_efactura_paid(mid, note, how=how)
        return _res(
            ok, "Factura a fost marcată ca plătită." if ok else "Factura nu a putut fi marcată. Reîncarcă pagina."
        )
    why = efactura.withdraw(L, mid, note, how)
    if why == pay.QUESTION_REFUSAL:
        return _res(False, why)
    return _res(
        not why,
        "Factura a fost scoasă din fișierul de plăți și marcată ca plătită."
        if not why
        else f"Plata este deja importată în {pay.import_in(L, co)}. Dacă nu se mai plătește, "
        f"șterge-o din {pay.import_in(L, co)}."
        if "imported" in why
        else "Factura nu mai este la plată.",
    )


def issued_mark(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """An issued invoice collected without a statement credit yet, with how the client paid (issued.COLLECTED_METHODS):
    the marking stays awaiting a statement whatever the method."""
    mid = ctx.get("id")
    # the issued view's own rule: an invoice of this company that a client still owes something on
    owed = any(r["message_id"] == mid and r["can_mark"] for r in issued.issued(L, co)["rows"])
    try:
        ok = owed and issued.mark_collected(L, mid, ctx.get("method"), ctx.get("detail"))
    except efactura.PaidMethodError as e:
        return _res(False, COLLECTED_METHOD_RO[e.code])
    ctx.log(f"web: issued-mark {mid[:20]}: {'ok' if ok else 'nothing'}")
    return _res(
        ok,
        "Factura a fost marcată ca încasată." if ok else "Factura nu mai este de încasat.",
    )


COLLECTED_METHOD_RO = {
    "method": "Alege cum a fost încasată.",
    "detail": "Scrie cum a fost încasată.",
    "long": f"Textul poate avea cel mult {efactura.PAID_DETAIL_MAX} de caractere.",
}


def issued_unmark(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """Undo the user's mark (never a collection a statement proved)."""
    mid = ctx.get("id")
    row = L.issued(mid) if efactura.valid_id(mid) else None
    ok = bool(row and row["company"] == co) and issued.unmark_collected(L, mid)
    ctx.log(f"web: issued-unmark {mid[:20]}: {'ok' if ok else 'nothing'}")
    return _res(
        ok,
        "Marcarea a fost anulată. Factura este din nou de încasat."
        if ok
        else "Marcarea acestei facturi nu poate fi anulată.",
    )


def resolve_debit(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """A statement debit that fit several invoices: the user picks the one it paid (efactura.resolve_debit)."""
    number = next(
        (
            c["number"]
            for a in api.ambiguous(L, co)
            if a["tx"] == ctx.get("tx")
            for c in a["candidates"]
            if c["id"] == ctx.get("target")
        ),
        "",
    )
    try:
        efactura.resolve_debit(L, co, ctx.get("tx"), ctx.get("target"))
        out = _res(True, f"Factura {number} este confirmată ca plătită, cu plata de pe extras ca dovadă.")
    except efactura.ResolveError:
        out = _res(False, "Plata nu mai așteaptă un răspuns.")
    ctx.log(f"web: resolve-debit {ctx.get('tx')[:20]}: {'ok' if out['ok'] else 'nothing'}")
    return out


def card_owner(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    ok = L.assign_card(co, ctx.get("tx"), ctx.get("owner"), ctx.created_by)
    return _res(
        ok,
        "Titularul cardului a fost salvat." if ok else "Titularul cardului nu a fost salvat.",
    )


def tx_link(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """A transaction's sheet: the user explains it with documents (links.link). Local bookkeeping, but it settles
    invoices, with the same proof guards in both modes."""
    try:
        done = links.link(
            L,
            co,
            ctx.get("tx"),
            kind=ctx.get("kind") or "documents",
            invoices=[i for i in ctx.get("invoices").split(",") if i],
            partners=api.item_partners(L, co),
            payment=ctx.get("payment"),
            reason=ctx.get("reason"),
            note=ctx.get("note"),
            who=ctx.get("who"),
            holder=ctx.get("holder"),
            was=ctx.get("was"),
            remember=ctx.get("remember"),
            by=ctx.created_by,
        )
        out = _res(True, _linked(L, done)) | {"linked": {"id": done["id"]}}
    except links.LinkRefused as e:
        out = _res(False, links.REFUSED.get(e.code, BAD))
    ctx.log(f"web: tx-link {ctx.get('tx')[:20]}: {'ok' if out['ok'] else 'refused'}")
    return out


def memory_forget(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """Forget what was remembered for an account, a card or a merchant: its next transactions propose nothing from it.
    Available in both modes."""
    ok = L.forget(co, ctx.get("id"))
    return _res(ok, "Asocierea a fost ștearsă." if ok else "Asocierea nu mai există.")


def tx_await(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """Aștept factura: on=1, a debit stays to explain, with the user's optional note (links.await_invoice); on=0 stops."""
    on = ctx.get("on")
    if on not in ("0", "1"):
        return _res(False, BAD)
    try:
        ok = (
            links.await_invoice(L, co, ctx.get("tx"), ctx.get("note"), ctx.created_by)
            if on == "1"
            else L.stop_awaiting(co, ctx.get("tx"))
        )
    except links.LinkRefused as e:
        return _res(False, links.REFUSED.get(e.code, BAD))
    done = "Tranzacția așteaptă factura." if on == "1" else "Tranzacția nu mai așteaptă factura."
    return _res(ok, done if ok else "Tranzacția nu mai poate aștepta factura.")


def tx_unlink(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """Undo the explanation the user made (links.unlink): what it closed reopens as it was."""
    try:
        links.unlink(L, co, ctx.get("tx"), ctx.get("was"))
        out = _res(True, "Asocierea a fost anulată.")
    except links.LinkRefused as e:
        out = _res(False, links.REFUSED.get(e.code, BAD))
    ctx.log(f"web: tx-unlink {ctx.get('tx')[:20]}: {'ok' if out['ok'] else 'refused'}")
    return out


def document_correct(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    try:
        docs.correct(
            L,
            co,
            ctx.get("id"),
            ctx.get("amount"),
            ctx.get("currency"),
            ctx.get("date"),
            ctx.get("number"),
            audit.actor.get() or ctx.created_by or "app",
        )
    except ValueError as e:
        return _res(False, str(e))
    return _res(True, "Datele documentului au fost salvate.")


def upload_assign(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    ok = api.upload_assignment.assign(L, ctx.get("key"), ctx.get("who"))
    return _res(ok, "Documentul a fost atribuit firmei." if ok else "Documentul nu mai așteaptă un răspuns.")


def mail_answer(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """A mailed document in doubt (mail_invoices.answer): `who` a firm offered for it or "none" (not an invoice),
    `total` the index of the total picked when several were offered, `remember` "1" to tie the sender to the answer.
    A local write, available in both modes."""

    key, who, total = ctx.get("key"), ctx.get("who"), ctx.get("total")
    if total and not total.isdigit():
        return _res(False, BAD)
    try:
        if not any(q["key"] == key for q in api._to_check(L, co)):  # only a question this firm is an answer to
            raise mail_invoices.AnswerError("Documentul nu mai așteaptă un răspuns.")
        done = mail_invoices.answer(
            L,
            key,
            who,
            total=int(total) if total else None,
            remember=ctx.get("remember") == "1",
            by=audit.actor.get() or "app",
        )
    except mail_invoices.AnswerError as e:
        out = _res(False, str(e))
    else:
        same = " Regula se aplică documentelor viitoare de la acest expeditor." if ctx.get("remember") == "1" else ""
        if done["status"] == "not-invoice":
            out = _res(True, "Documentul a fost marcat: nu este factură. Nu intră în dosar." + same)
        else:
            name = companies.all(L)[who].name
            out = _res(
                True,
                (
                    f"Factura a fost atribuită firmei {name}."
                    if done["status"] == "filed"
                    else f"Factura era deja la firma {name}."
                )
                + same,
            )
    ctx.log(f"web: mail-answer {who}: {out['message'][:40]}")
    return out


def pack_action(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    month = ctx.get("month")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        return _res(False, BAD)
    try:
        if ctx.action == "pack-prepare":
            pack.build(L, co, month, summary=lambda: api.pack_summary(L, co, month))
            return _res(True, "Dosarul a fost pregătit.")
        undo = ctx.action == "pack-unsent"
        version, mark = ctx.get("version"), args.get("mark")
        # the app always names the mark it shows (a tab from before an undo is refused); v1 and the CLI may omit it
        if mark is None and (undo or not ctx.company_only):
            return _res(False, "Dosarul s-a schimbat între timp.")
        ok = pack.unmark_sent(L, co, month, version, mark) if undo else pack.mark_sent(L, co, month, version, mark)
        done = "Dosarul nu mai este marcat ca trimis." if undo else "Dosarul a fost marcat ca trimis."
        return _res(ok, done if ok else "Dosarul s-a schimbat între timp.")
    except (ValueError, OSError):
        return _res(False, "Dosarul nu a putut fi pregătit sau confirmat. Verifică documentele.")


def _mail_microsoft(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    try:
        if not ctx.created_by:
            raise microsoft.MicrosoftError("invalid_settings")
        if ctx.action == "mail-microsoft-start":
            done = microsoft.start(
                L,
                ctx.get("address"),
                ctx.get("id"),
                ctx.created_by,
                **({"firm_scope": args["firm_scope"]} if "firm_scope" in args else {}),
            )
        else:
            done = microsoft.poll(L, ctx.get("id"), ctx.created_by)
        return _res(True, "", done=done)
    except (
        microsoft.MicrosoftError,
        ig.IntegrationError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        code = (
            str(exc)
            if isinstance(exc, microsoft.MicrosoftError)
            else "invalid_settings"
            if isinstance(exc, ig.IntegrationError)
            else "connection_failed"
        )
        if code == "not_configured":  # the owner sees only that it is missing; the administrator reads how here
            ctx.log("web: Microsoft mail has no app: set FISCALM_MICROSOFT_CLIENT_ID or config/microsoft.json")
        return _res(False, "Conectarea Microsoft nu a reușit.", done={"code": code})


def _mail_setup(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if ctx.action == "mail-detect":
        result = mail_setup.detect(str(args.get("address") or ""))
    else:
        account = ig.instance(L, str(args.get("id") or ""))
        if args.get("id"):
            if account is None or account.kind != "mail" or set(args) - {"id", "co"}:
                return _res(False, BAD, done={"code": "invalid_settings"})
            try:
                password = account.secret("password")
            except (OSError, secrets.SecretError):
                return _res(False, BAD, done={"code": "invalid_settings"})
            config = account.config
        else:
            config = {k: str(v) for k, v in args.items() if k not in ("password", "co")}
            password = str(args.get("password") or "")
        result = mail_setup.test_connection(config, password)
    ok = result.get("code", "ok") == "ok"
    return _res(ok, "Verificare încheiată." if ok else "Verifică setările contului.", done=result)


def integration(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    """Integrări: add, change, switch on or off, make default, remove. Field values come as f_<key>; a secret is
    written to its file and never echoed. The log names the action and the instance, never a value."""

    item = ig.instance(L, str(args.get("id") or ""))
    if (args.get("kind") == "ai" or (item and item.kind == "ai")) and not ctx.require("connect_integrations"):
        raise PermissionError("ai_admin_required")
    values = {k[len(FIELD) :]: str(v) for k, v in args.items() if k.startswith(FIELD)}
    iid, name = str(args.get("id") or ""), args.get("name")
    try:
        ig.check_listed(L, str(args.get("kind") or "") if ctx.action == "integration-add" else "", iid)
        if ctx.action == "integration-add":
            got = ig.add(
                L, str(args.get("kind") or ""), str(name or ""), values, firm_scope=args.get("firm_scope", "all")
            )
            iid = got["id"]
        elif ctx.action == "integration-update":
            ig.update(
                L,
                iid,
                values,
                str(name) if name is not None else None,
                **({"firm_scope": args["firm_scope"]} if "firm_scope" in args else {}),
            )
        elif ctx.action == "integration-clear":
            ig.clear(L, iid, str(args.get("key") or ""))
        elif ctx.action == "integration-enable":
            on = str(args.get("on") or "") == "1"
            ig.set_enabled(L, iid, on)
            out = _res(True, "Integrarea a fost pornită." if on else "Integrarea a fost oprită.")
            ctx.log(f"web: {ctx.action} {iid} {'on' if on else 'off'}")
            return out
        elif ctx.action == "integration-default":
            ig.make_default(L, iid)
        else:
            ig.remove(L, iid)
    except ig.IntegrationError as e:
        ctx.log(f"web: {ctx.action} refused")
        return _res(False, str(e))
    ctx.log(f"web: {ctx.action} {iid}")
    return _res(
        True, INTEGRATION_DONE[ctx.action], **({"done": {"id": iid}} if ctx.action == "integration-add" else {})
    )


def company_action(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    args = {k: v for k, v in args.items() if k != "co"}
    try:
        if ctx.action in ("company-lookup", "company-add") and "cui" not in args:
            raise cc.CompanyError(cc.BAD)
        if ctx.action in ("company-update", "company-bank", "company-account") and "key" not in args:
            raise cc.CompanyError(cc.BAD)
        if ctx.action == "company-statement-password":
            return _statement_password(L, args, ctx)
        if ctx.action in ("company-alias-add", "company-alias-remove"):
            if set(args) != {"key", "alias"}:
                raise cc.CompanyError(cc.BAD)
            result = cc.set_alias(L=L, key=args["key"], alias=args["alias"], add=ctx.action == "company-alias-add")
            return _res(
                True,
                "Numele a fost adăugat." if ctx.action == "company-alias-add" else "Numele a fost șters.",
                done=result,
            )
        if ctx.action == "company-bank":
            if set(args) != {"key", "iban"}:
                raise cc.CompanyError(cc.BAD)
            result = cc.bank(L=L, **args)
        elif ctx.action == "company-account":
            if set(args) != {"key", "iban", "currency"}:
                raise cc.CompanyError(cc.BAD)
            result = cc.add_account(L=L, **args)
        else:
            result = (
                cc.lookup(args["cui"], date.today())
                if ctx.action == "company-lookup"
                else cc.change(L=L, **args, today=date.today())
            )
        return _res(
            True,
            {
                "company-lookup": "Firma a fost verificată la ANAF.",
                "company-add": "Firma a fost adăugată.",
                "company-update": "Firma a fost actualizată.",
                "company-bank": "Contul firmei a fost salvat.",
                "company-account": "Contul a fost adăugat.",
            }[ctx.action],
            done=result,
        )
    except cc.AnafSilent as e:
        return _res(False, str(e), done={"retry": True})
    except cc.CompanyError as e:
        return _res(False, str(e))
    except cc.ConfigError:
        return _res(
            False,
            "Configurarea nu a putut fi citită. Verifică fișierele de configurare, apoi încearcă din nou.",
        )


def _statement_password(L: Ledger, args: dict, ctx: Context) -> dict:
    """A firm's statement password at one of its banks (Setări › Firme › Bănci). Write-only: the log names the firm and
    the bank, never the value."""
    if set(args) != {"key", "bank", "password"} or args["key"] not in companies.all(L):
        raise cc.CompanyError(cc.BAD)
    try:
        ig.set_statement_password(L, args["key"], args["bank"], args["password"])
    except ig.IntegrationError as e:
        ctx.log(f"web: {ctx.action} refused")
        return _res(False, str(e))
    ctx.log(f"web: {ctx.action} {args['key']} {args['bank']}")
    return _res(True, "Parola extraselor a fost salvată.")


INTEGRATION_DONE = {
    "integration-add": "Integrarea a fost adăugată.",
    "integration-update": "Modificările au fost salvate.",
    "integration-clear": "Valoarea a fost ștearsă.",
    "integration-default": "Integrarea a fost stabilită ca implicită.",
    "integration-remove": "Integrarea a fost ștearsă.",
}


def history_review(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    target = args.get("target", co)
    if ctx.company_only and target != co:
        return _res(False, BAD)
    fields = {
        k: args.get(k, "")
        for k in ("direction", "number", "issue_date", "total", "currency", "party_name", "party_tax_id")
    }
    history_pdf.answer(L, co, args.get("sha", ""), fields, target=target)
    return _res(
        True, "Documentul a fost exclus din import." if fields["direction"] == "none" else "Factura a fost confirmată."
    )


def _telemetry_settings(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if args.get("on") not in ("0", "1"):
        return _res(False, BAD)
    telemetry.set_enabled(L, args["on"] == "1")
    return _res(True, "Setarea a fost salvată.")


def _update_checks(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    on = args.get("on")
    if on not in ("0", "1"):
        return _res(False, BAD)
    updates.set_enabled(L, on == "1")
    return _res(
        True,
        "De acum, fiscalm verifică zilnic versiunile noi."
        if on == "1"
        else "fiscalm nu mai verifică automat versiunile noi.",
    )


def _setup_card_dismiss(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    api.hide_setup_card(L, co, True)
    return _res(True, "Cardul Configurează a fost ascuns. Îl afișezi din nou din Setări › Firme.")


def _setup_card_show(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    api.hide_setup_card(L, co, False)
    return _res(True, "Cardul Configurează apare din nou pe Acasă.")


def _mode_switch(L: Ledger, co: str, args: dict, ctx: Context, *, allow_live: bool = True) -> dict:
    if args.get("mode") not in ("live", "sandbox") or args.get("expected") not in ("live", "sandbox"):
        return _res(False, BAD)
    if args.get("confirmed") != "yes" or not ctx.created_by or not re.fullmatch(r"[0-9a-f]{32}", ctx.get("request_id")):
        return _res(False, BAD)
    if args["mode"] == "live" and not allow_live:
        return _res(False, "Instalarea de dezvoltare rămâne în Sandbox.")
    changed = L.change_mode(
        args["mode"] == "live",
        args["expected"] == "live",
        ctx.created_by,
        sessions.stamp(ctx.at),
        ctx.get("request_id"),
    )
    done = "Live este activ." if args["mode"] == "live" else "Sandbox este activ."
    return _res(changed, done if changed else "Modul a fost deja schimbat.")


def _account_action(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if not ctx.created_by:
        raise PermissionError("session_required")
    try:
        with L.tx():
            ctx.authorize()
            done = _administer(L, ctx.action, args, ctx.created_by, ctx.at)
    except ValueError as exc:
        message = {
            "unknown_user": "Utilizatorul nu mai este disponibil.",
            "account_already_configured": "Contul este deja configurat. Autentificarea se poate reseta.",
        }.get(str(exc), str(exc))
        return _res(False, message)
    return _res(True, ACTIONS[ctx.action].message, done=done)


def _administer(L: Ledger, action: str, args: dict, actor_id: str, at: datetime) -> dict:
    """The app holds its session/step-up write lock around this account mutation."""
    if action == "user-create":
        ident = users.create(
            L, args.get("username", ""), args.get("display_name", ""), args.get("role", ""), at, actor_id=actor_id
        )
        return {"id": ident, "url": setup_links.issue(L, ident, at, actor_id=actor_id)}
    ident = args.get("id", "")
    operations = {
        "user-update": lambda: L.change_user(
            ident,
            actor_id,
            sessions.stamp(at),
            role=args.get("role"),
            display_name=users.display(args.get("display_name", "")),
        ),
        "user-disable": lambda: L.change_user(ident, actor_id, sessions.stamp(at), enabled=False),
        "user-enable": lambda: L.change_user(ident, actor_id, sessions.stamp(at), enabled=True),
        "user-unlock": lambda: limits.unlock(L, ident, at, actor_id=actor_id),
        "user-setup-link": lambda: setup_links.issue(L, ident, at, actor_id=actor_id),
        "user-reset": lambda: setup_links.issue(L, ident, at, actor_id=actor_id, reset=True),
        "session-close": lambda: L.close_admin_session(ident, actor_id, sessions.stamp(at)),
    }
    result = operations[action]()
    return {"url": result} if action in ("user-setup-link", "user-reset") else {"id": ident}


@dataclass(frozen=True)
class Context:
    action: str
    args: dict
    at: datetime
    company_only: bool = False
    created_by: str | None = None
    authorize: Callable[[], None] = lambda: None

    log: Callable[[str], None] = lambda line: None

    origin: str | None = None
    require: Callable[[str], bool] = lambda need: True

    def get(self, key: str) -> str:
        return str(self.args.get(key) or "")


def _payment_file_refusal(exc: Exception) -> str:
    if isinstance(exc, efactura.ApproveError):
        return (
            "Factura e deja într-un fișier de plăți sau nu mai este deschisă."
            if exc.code == "not_open"
            else efactura.approve_error_ro(exc)
        )
    if isinstance(exc, pay.FileRefusal):
        return str(exc)
    if str(exc) == "Payment file changed":
        return STALE_DOWNLOAD
    if isinstance(exc, PaymentFileError):
        return "Fișierul nu poate fi pregătit. Verifică datele plăților și contul firmei."
    return "Verifică numele fișierului și plățile alese. Numele poate avea între 1 și 200 de caractere."


def _product(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if not set(PRODUCT_BODIES[ctx.action]["required"]) <= args.keys():
        raise products.ProductError("Verifică datele produsului.")
    result = products.write(L, co, ctx.action, {k: v for k, v in args.items() if k != "co"})
    return _res(True, "Produsul a fost salvat.", done={"product": result})


def _file_create(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    ident = pay.add_invoices(L, co, _file_invoice_ids(args), name=ctx.get("name"), approve_invoice=efactura.approve)
    return _res(True, "Fișierul a fost creat.", done={"file_id": ident})


def _file_invoice_ids(args: dict) -> list[str]:
    ids = args.get("invoice_ids")
    if not isinstance(ids, list) or any(type(v) not in (str, int) or not str(v).isdigit() for v in ids):
        raise ValueError(BAD)
    return [str(v) for v in ids]


def _file_add(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    ids = _file_invoice_ids(args)
    if not ids:
        raise ValueError(BAD)
    ident = pay.add_invoices(L, co, ids, file_id=int(args["file_id"]), approve_invoice=efactura.approve)
    return _res(True, "Plățile au fost adăugate.", done={"file_id": ident})


def _file_rename(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    with pay.pay_lock(), L.tx():
        pay.editable_file(L, int(args["file_id"]), co)
        ok = L.rename_payment_file(int(args["file_id"]), co, ctx.get("name"))
    return _res(ok, "Numele a fost schimbat.")


def _file_remove(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    ident = int(args["file_id"])
    item = next(
        (r["item_id"] for r in pay.file_rows(L, ident) if api.payment_id(r["item_id"]) == args.get("payment_id")), ""
    )
    with pay.pay_lock(), L.tx():
        file = pay.editable_file(L, ident, co)
        if file["path"] and not pay.named_path(file).exists():
            raise pay.FileRefusal("Fișierul lipsește. Confirmă dacă l-ai importat înainte să-l schimbi.")
        ok = L.remove_file_payment(ident, co, item)
    return _res(ok, "Plata a fost scoasă din fișier." if ok else "Plata nu poate fi scoasă.")


def _file_delete(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    ok = pay.delete_file(L, int(args["file_id"]), co)
    return _res(ok, "Fișierul a fost șters." if ok else "Fișierul nu este gol.")


def _file_download(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    got = pay.download_file(L, int(args["file_id"]), co, ctx.at.date(), ctx.authorize)
    if "question" in got:
        return _res(False, "Ai importat fișierul care lipsește?", done=got)
    data = base64.b64encode(got.pop("data")).decode("ascii")
    return _res(True, "Fișierul a fost descărcat.", done=got | {"content_base64": data})


def _file_restore(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    if pay.clear_missing_inactive_file(L, int(args["file_id"]), co, ctx.get("version")):
        return _res(True, "Fișierul nu mai are plăți de efectuat.", done={"file_id": int(args["file_id"])})
    got = pay.download_file(
        L, int(args["file_id"]), co, ctx.at.date(), ctx.authorize, restore_version=ctx.get("version")
    )
    data = base64.b64encode(got.pop("data")).decode("ascii")
    return _res(True, "Fișierul a fost refăcut.", done=got | {"content_base64": data})


def _file_missing_import(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    ok = pay.resolve_missing_file(L, int(args["file_id"]), co, ctx.get("version"), ctx.at.date(), True)
    return _res(ok, "Ai confirmat importul în bancă." if ok else STALE_DOWNLOAD)


def _file_import(L: Ledger, co: str, args: dict, ctx: Context) -> dict:
    ok = pay.import_file(L, int(args["file_id"]), co, int(args["download_id"]), ctx.at.date())
    return _res(ok, "Ai confirmat importul în bancă.")


@dataclass(frozen=True)
class Action:
    handler: Callable
    fields: str
    effect: Literal["local", "external", "payment_file", "anaf_upload"] = "local"

    def allowed_in_mode(self) -> bool:
        if self.effect == "anaf_upload":
            return anaf.outward_allowed()
        return self.effect == "local" or ledger.is_live()

    role: str = "member"
    firm: bool = True
    public: bool = True
    strings: bool = False
    errors: tuple[type[Exception], ...] = ()
    message: str = ""
    schema: dict | None = None
    public_schema: dict | None = None
    limit: int = 4096
    actor: bool = False
    description: str = ""
    public_description: str = ""

    def valid(self, args: dict) -> bool:
        if self.schema is not None:
            return self._valid_body(args)
        if self.handler in (integration, _mail_microsoft) and "firm_scope" in args:
            if "firm_scope" not in self.fields.split():
                return False
            scope = args["firm_scope"]
            if scope != "all" and (not isinstance(scope, list) or any(not isinstance(k, str) for k in scope)):
                return False
            return len(json.dumps(args).encode()) <= 4096 and self.valid(
                {k: v for k, v in args.items() if k != "firm_scope"}
            )
        if self.handler == _upload_link_create and "all_firms" in self.fields.split() and "all_firms" in args:
            return type(args["all_firms"]) is bool and self.valid({k: v for k, v in args.items() if k != "all_firms"})
        allowed = set(self.fields.split()) | {"co"}
        if self.handler == integration:
            allowed |= {FIELD + f.key for kind in ig.KINDS.values() for f in kind.fields if not f.rule}
        types = (str,) if self.strings else (str, int)
        return (
            not set(args) - allowed
            and all(isinstance(v, types) and not isinstance(v, bool) for v in args.values())
            and len(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode()) <= 4096
        )

    def _valid_body(self, args: dict) -> bool:
        properties = self.schema["properties"] | {"co": {"type": "string"}}
        if set(args) - properties.keys():
            return False
        for key, value in args.items():
            expected = properties[key].get("type")
            if expected is None:
                continue
            kinds = expected if isinstance(expected, list) else [expected]
            if type(value) not in [
                {
                    "string": str,
                    "integer": int,
                    "boolean": bool,
                    "object": dict,
                    "array": list,
                    "null": type(None),
                    "bytes": bytes,
                }[k]
                for k in kinds
            ]:
                return False
        size = (
            len(args.get("data", b""))
            if "data" in properties
            else len(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode())
        )
        return size <= self.limit


# One row per entry keeps the handler and policy together.
# fmt: off
ACTIONS = {
    **{name: Action(_product, " ".join(body["properties"]), public=False,
                   schema=body, errors=(products.ProductError,),
                   description="Company catalog defaults; local in Sandbox. Version guards edits; delete refuses any invoice reference.")
       for name, body in PRODUCT_BODIES.items()},
    'mode-switch': Action(_mode_switch, 'mode expected confirmed request_id', role='manage_firms', firm=False, public=False, strings=True, actor=True, description='mode and expected: live/sandbox; confirmed=yes. Fresh administrator session required. Audited.'),
    'user-create': Action(_account_action, 'username display_name role', role='manage_users', firm=False, public=False, strings=True, actor=True, errors=(ValueError,), message='Utilizatorul a fost adăugat.', description='Account administration; fresh administrator session required. username display_name role.'),
    'user-update': Action(_account_action, 'id display_name role', role='manage_users', firm=False, public=False, strings=True, actor=True, errors=(ValueError,), message='Utilizatorul a fost actualizat.', description='Account administration; fresh administrator session required. id display_name role.'),
    'user-disable': Action(_account_action, 'id', role='manage_users', firm=False, public=False, strings=True, actor=True, errors=(ValueError,), message='Utilizatorul a fost dezactivat.', description='Account administration; fresh administrator session required. id.'),
    'user-enable': Action(_account_action, 'id', role='manage_users', firm=False, public=False, strings=True, actor=True, errors=(ValueError,), message='Utilizatorul a fost reactivat.', description='Account administration; fresh administrator session required. id.'),
    'user-unlock': Action(_account_action, 'id', role='manage_users', firm=False, public=False, strings=True, actor=True, errors=(ValueError,), message='Utilizatorul a fost deblocat.', description='Account administration; fresh administrator session required. id.'),
    'user-setup-link': Action(_account_action, 'id', role='manage_users', firm=False, public=False, strings=True, actor=True, errors=(ValueError,), message='Linkul de configurare a fost creat.', description='Account administration; fresh administrator session required. id.'),
    'user-reset': Action(_account_action, 'id', role='manage_users', firm=False, public=False, strings=True, actor=True, errors=(ValueError,), message='Autentificarea a fost resetată.', description='Account administration; fresh administrator session required. id.'),
    'session-close': Action(_account_action, 'id', role='manage_users', firm=False, public=False, strings=True, actor=True, errors=(ValueError,), message='Sesiunea a fost închisă.', description='Account administration; fresh administrator session required. id.'),
    'history-review': Action(history_review, 'sha target direction number issue_date total currency party_name party_tax_id', role='manage_firms', public=False, strings=True, errors=(history_pdf.ReviewError,), description='Review one pending history PDF by digest, correcting invoice fields or excluding it. Local only; never confirms payment.'),
    'receipt-lines-save': Action(_receipt_items, 'id revision payload', public=False, description='id: receipt SHA; revision: displayed version; payload: JSON string with lines (name, unit, quantity, net after discounts excluding VAT, vat percentage), total, currency, payment (cash/card/empty). Empty lines skip. 4 KB.'),
    'receipt-invoice-confirm': Action(_receipt_items, 'id revision invoice', public=False, description='id: receipt SHA; revision; invoice: displayed unique proposal ID. Rechecks under lock, never confirms payment. Local operation.'),
    'setup-card-dismiss': Action(_setup_card_dismiss, '', role='manage_firms', public=False, strings=True, description='Close the selected firm\'s Configurează card on Acasă; the items it listed stay in Setări, and `setup-card-show` reopens it. Administrator only; repeating it changes nothing.'),
    'setup-card-show': Action(_setup_card_show, '', role='manage_firms', public=False, strings=True, description='Show the selected firm\'s closed Configurează card on Acasă again, while something is left to set up. Administrator only; repeating it changes nothing.'),
    'telemetry-settings': Action(_telemetry_settings, 'on', effect='local', role='manage_firms', firm=False, public=False, strings=True, description='on ("1" or "0"). Administrator only. Daily anonymous Live install statistics, enabled by default; FISCALM_TELEMETRY=0 overrides this local preference.'),
    'update-checks': Action(_update_checks, 'on', role='manage_firms', firm=False, public=False, strings=True, description='on ("1" or "0"). App administrator only. Enable or disable the anonymous daily GitHub Releases check, also allowed in shadow mode.'),
    'accountant-save': Action(_accountant_save, 'cui senders recipient', role='manage_firms', public=False, strings=True, description='cui (existing partner), senders (newline-separated addresses, at most ten), recipient, co. App admin configuration.'),
    'settlement-withdraw': Action(_settlement_withdraw, 'id', strings=True, description='Withdraw pending settlement evidence; never release a reserved credit.'),
    'settlement-format-delete': Action(_settlement_format_delete, 'partner signature', strings=True, public=False, description="partner, signature: one of the partner's report_formats. Forgets the saved columns; uploaded reports are unchanged."),
    'settlement-confirm': Action(_settlement_confirm, 'id tx confirmed', actor=True, public_description='id: 32-hex report ID; tx: 20-hex exact displayed credit ID; confirmed: yes. Local operation; v1 requires a write key owned by an active person (else 403 person_owned_key_required). Rechecks all evidence under lock.'),
    'order-discard': Action(_order_discard, 'id', public=False, actor=True, description='co, id: company-scoped order sheet ID. App-only local configuration.'),
    'emag-refund': Action(_emag_resolve, 'id', public_description='id: company-scoped order sheet ID; prepares a refund draft, never sends money.'),
    'emag-resolve': Action(_emag_resolve, 'id', public_description='id: company-scoped order sheet ID; acknowledges the current review.'),
    'payment_file_restore': Action(_file_restore, 'file_id version', effect='payment_file', public=False, errors=(PaymentFileError, ValueError), schema={'type':'object','properties':{'file_id':{'type':'integer'},'version':{'type':'string'}},'required':['file_id','version']}),
    'payment_file_missing_import': Action(_file_missing_import, 'file_id version', effect='payment_file', public=False, errors=(PaymentFileError, ValueError), schema={'type':'object','properties':{'file_id':{'type':'integer'},'version':{'type':'string'}},'required':['file_id','version']}),
    'payment_file_create': Action(_file_create, 'name invoice_ids', effect='local', public=False, errors=(PaymentFileError, efactura.ApproveError, ValueError), schema={'type': 'object', 'properties': {'name': {'type': 'string'}, 'invoice_ids': {'type': 'array', 'items': {'type': ['string', 'integer']}}}, 'required': ['name', 'invoice_ids']}),
    'payment_file_add': Action(_file_add, 'file_id invoice_ids', effect='local', public=False, errors=(PaymentFileError, efactura.ApproveError, ValueError), schema={'type': 'object', 'properties': {'file_id': {'type': 'integer'}, 'invoice_ids': {'type': 'array', 'items': {'type': ['string', 'integer']}}}, 'required': ['file_id', 'invoice_ids']}),
    'payment_file_rename': Action(_file_rename, 'file_id name', effect='local', public=False, errors=(PaymentFileError, efactura.ApproveError, ValueError), schema={'type': 'object', 'properties': {'file_id': {'type': 'integer'}, 'name': {'type': 'string'}}, 'required': ['file_id', 'name']}),
    'payment_file_remove': Action(_file_remove, 'file_id payment_id', effect='local', public=False, errors=(PaymentFileError, efactura.ApproveError, ValueError), schema={'type': 'object', 'properties': {'file_id': {'type': 'integer'}, 'payment_id': {'type': 'string'}}, 'required': ['file_id', 'payment_id']}),
    'payment_file_delete': Action(_file_delete, 'file_id', effect='local', public=False, errors=(PaymentFileError, efactura.ApproveError, ValueError), schema={'type': 'object', 'properties': {'file_id': {'type': 'integer'}}, 'required': ['file_id']}),
    'payment_file_download': Action(_file_download, 'file_id', effect='payment_file', public=False, errors=(PaymentFileError, efactura.ApproveError, ValueError), schema={'type': 'object', 'properties': {'file_id': {'type': 'integer'}}, 'required': ['file_id']}),
    'payment_file_import': Action(_file_import, 'file_id download_id', effect='payment_file', public=False, errors=(PaymentFileError, efactura.ApproveError, ValueError), schema={'type': 'object', 'properties': {'file_id': {'type': 'integer'}, 'download_id': {'type': 'integer'}}, 'required': ['file_id', 'download_id']}),
    'mark-paid': Action(invoice_action, 'id method detail', strings=True, public_description='id; method: how the person paid, one of bank (the firm\'s account), card (a firm card), cash, own (their own money) or other; detail: card: one of firm_cards (required once the firm has one), own: who paid, other: a short note, both required, at most 200 characters; empty for bank and cash. Records a fact only: the invoice stays marked, awaiting a statement or a mail, whatever the method.'),
    'reopen': Action(invoice_action, 'id', public_description='id'),
    'withdraw': Action(invoice_action, 'id method detail', strings=True, public_description='id, method, detail: as mark-paid. Takes the invoice out of the payment file and marks it paid.'),
    'pack-prepare': Action(pack_action, 'month', public_description='month: YYYY-MM. Freeze the company ZIP for download; an unsent stale version is replaced by a new one, a sent one never. Local in both modes.'),
    'pack-sent': Action(pack_action, 'month version mark', public_description='month, version: exact prepared ZIP SHA-256; optional mark: the report\'s mark as shown, refused once the pack was sent or unsent since. Human confirms sending only that snapshot.'),
    'pack-unsent': Action(pack_action, 'month version mark', public=False, description='month, version: the sent ZIP SHA-256; mark: the report\'s mark as shown. Undoes pack-sent: the snapshot is prepared again and the documents only it covers are unsent. Local operation.'),
    'check': Action(check, '', public_description='No fields. Starts the tick; permitted in shadow mode, which the tick respects.'),
    'document-correct': Action(document_correct, 'id amount currency date number', public=False, strings=True, actor=True, description='Correct receipt/document metadata only. amount: exact two-decimal string; currency: engine currency code; date and number optional. Refuses settled proof and sent accountant packs; records actor, time and previous values. Never settles money.'),
    'upload-assign': Action(upload_assign, 'key who', public=False, strings=True, description='Assign an unassigned upload to a configured firm. First answer wins.'),
    'mail-answer': Action(mail_answer, 'key who total remember', public_description='In v1 who must equal the URL company or none (else wrong_company). key (a to_check[].key this co is offered for), who (one of its companies, or none: not an invoice), optional total (the index into its totals as a digit string, needed when it has several), optional remember (1: the exact sender address goes the same way next time)'),
    'rule-pause': Action(_rule_pause, 'id', role='manage_rules', public=False, description='id (company-qualified rule id)'),
    'rule-resume': Action(_rule_pause, 'id', role='manage_rules', public=False, description='id (company-qualified rule id)'),
    'rule-supplier-autopay': Action(_rule_supplier_autopay, 'cui on', role='manage_rules', public=False, description="cui (a partner's, from the state), on (1/0)"),
    'rule-partner-not-mailed': Action(_rule_partner_not_mailed, 'partner on', role='manage_rules', public=False, description="partner (a partner's id, from the state), on (1/0)"),
    'rule-spv-delay': Action(_rule_spv_delay, 'days', role='manage_rules', public=False, strings=True, errors=(InvoiceError,), description='days (digit string, 0..5): days after issue the tick sends an invoice to SPV; refused while spv_auto_upload is off'),
    'rule-reminders': Action(_rule_reminders, 'after_days', role='manage_rules', public=False, strings=True, errors=(reminders.Refused,), description='after_days (digit string, 1..365)'),
    'issued-mark': Action(issued_mark, 'id method detail', strings=True, public_description='id, method (bank: payment order, cash, card, other), detail: for other, how it was collected (1 to 200 characters); else empty. Never proof: the invoice awaits a statement credit.'),
    'issued-unmark': Action(issued_unmark, 'id', public_description='id'),
    'resolve-debit': Action(resolve_debit, 'tx target', public_description='tx, target (one of its candidates[].id)'),
    'card-owner': Action(card_owner, 'tx owner', public=False, actor=True, description='tx (an observed card transaction), owner (a companies[].key or personal); first answer wins, never confirms payment'),
    'tx-link': Action(tx_link, 'tx kind invoices payment reason note who holder was remember', public=False, actor=True, description='kind (documents, the default; label: Fără document, reason one of bank_fee, interest, personal, other and an optional note for the accountant, required for other, at most 200 characters; transfer: Transfer între conturi; advance: Avans, who the administrator or associate, 1 to 100 characters), tx (a transactions.rows[].id; for an explanation already made, its link.tx), invoices (comma-separated ids: issued[].id for a credit, invoices[].id for a debit) or payment (one payments.*[].item_id, debits only), reason ("" or one of rest, courier_fee, bank_fee, fx, other), note (other only, 1 to 200 characters), was (the row\'s explanation.link.id, "" for none), remember ("", or one of the row\'s remember keys: iban, card for a label only, name; never for a transfer)'),
    'tx-await': Action(tx_await, 'tx on note', public=False, actor=True, description='tx (a debit in transactions.rows[].id, not explained or settled), on ("1" waits, "0" stops waiting), note (on=1 only, optional, what is expected, at most 200 characters). Aștept factura: the debit stays to explain, listed last, and reserves nothing. Local operation.'),
    'tx-unlink': Action(tx_unlink, 'tx was', public=False, actor=True, description="tx, was (the explanation's id)"),
    'memory-forget': Action(memory_forget, 'id', public=False, description='id (a transactions.remembered[].id)'),
    'token-create': Action(_token_create, 'name scope', role='own_tokens', firm=False, public=False, actor=True, description='name, scope (read/write). Fresh normal session; owner is the current user; read-only accounts can create read tokens only. Returns secret once.'),
    'token-revoke': Action(_token_create, 'id', role='own_tokens', firm=False, public=False, actor=True, description='id. Normal session; revokes own credential, administrator may revoke any.'),
    'upload-link-create': Action(_upload_link_create, 'label expires_in_days max_files all_firms', role='manage_tokens', public_description='label; optional all_firms (boolean), expires_in_days (1..365), max_files (positive integer). Write scope required; explicit upload-link routes are also available.'),
    'upload-link-revoke': Action(_upload_link_create, 'id', role='manage_tokens', public_description='id: upload link ID. Write scope required; explicit upload-link routes are also available.'),
    'mail-microsoft-start': Action(_mail_microsoft, 'address id firm_scope', role='manage_integrations', firm=False, public=False, strings=True, actor=True, description='Start read-only Microsoft device flow. address required, id optional for reconnect. Optional firm_scope (all or firm-key array) is stored before a new inbox can connect; default all. A supplied scope is refused when reconnecting an existing inbox, including address-based reuse. done: code=authorization_pending, id, user_code, verification_uri, expires_in seconds, interval seconds. Persist id for poll. No device code or tokens returned. Errors: not_configured, invalid_settings, admin_consent_required, tenant_policy_blocked, connection_failed.'),
    'mail-microsoft-poll': Action(_mail_microsoft, 'id', role='manage_integrations', firm=False, public=False, strings=True, actor=True, description='Poll Microsoft device flow by integration id from start, same user. done.code: authorization_pending or slow_down (wait done.interval seconds), connected (done.id), expired_token, authorization_declined, admin_consent_required, tenant_policy_blocked, account_mismatch, connection_changed, reconnect_required, invalid_scopes, connection_failed. Stop on terminal errors. integration.config.method=microsoft; reading.code=reconnect_required means Reconectează.'),
    'mail-detect': Action(_mail_setup, 'address', role='manage_integrations', firm=False, public=False, strings=True, description='address: email. done: method (microsoft offers device button, imap offers password), provider, host, port (993), username, app_password_required, app_password_url, hint, supported, hosts (ordered fallback candidates). Invalid address: done.code=invalid_settings. MX discovery is bounded; no credentials.'),
    'mail-test': Action(_mail_setup, 'id from_address password host port username read_folder', role='manage_integrations', firm=False, public=False, description='Either id alone (saved mail account) or from_address, password and optional host, port (993 only), username, read_folder (INBOX). Never saves. done.code: ok, wrong_password, app_password_required, host_unreachable, tls_error, folder_missing, timeout, invalid_settings, unsupported_provider, protocol_error. Success also returns host, port, username for integration-add f_<field>. Verified TLS, LOGIN, EXAMINE, LOGOUT only; public IPs only. No password or raw server reply returned.'),
    'integration-add': Action(integration, 'kind name firm_scope', role='manage_integrations', firm=False, description='kind, name, f_<field> per field; optional firm_scope all or firm-key array (ANAF/mail); success done contains only id of the created instance', public_description='App authentication required; bearer requests return 403 app_required.'),
    'integration-update': Action(integration, 'id name firm_scope', role='manage_integrations', firm=False, description='id, optional name, f_<field>', public_description='App authentication required; bearer requests return 403 app_required.'),
    'integration-clear': Action(integration, 'id key', role='manage_integrations', firm=False, description='id, key', public_description='App authentication required; bearer requests return 403 app_required.'),
    'integration-enable': Action(integration, 'id on', role='manage_integrations', firm=False, description='id, on ("1" or "0")', public_description='App authentication required; bearer requests return 403 app_required.'),
    'integration-default': Action(integration, 'id', role='manage_integrations', firm=False, description='id', public_description='App authentication required; bearer requests return 403 app_required.'),
    'integration-remove': Action(integration, 'id', role='manage_integrations', firm=False, description='id', public_description='App authentication required; bearer requests return 403 app_required.'),
    'company-lookup': Action(company_action, 'cui', role='manage_firms', firm=False, public=False, strings=True, description='{ "cui": "12345674" }. Refused with done.retry=true when ANAF gave no usable answer (ask again later), without it when ANAF does not know the CUI.'),
    'company-add': Action(company_action, 'cui name palette', role='manage_firms', firm=False, public=False, strings=True, description='{ "cui": "12345674", "name": "Exemplu", "palette": "blue" } (name, palette optional). Looks the CUI up again; refused with done.retry=true when ANAF gave no usable answer.'),
    'company-update': Action(company_action, 'key name palette', role='manage_firms', firm=False, public=False, strings=True, description='{ "key": "EXEMPLU", "name": "Alt nume", "palette": "rose" } (at least one display field)'),
    'company-bank': Action(company_action, 'key iban', role='manage_firms', firm=False, public=False, strings=True, description='{ "key": "EXEMPLU", "iban": "<RON IBAN>" } (both required)'),
    'company-account': Action(company_action, 'key iban currency', role='manage_firms', firm=False, public=False, strings=True, description='{ "key": "EXEMPLU", "iban": "<IBAN>", "currency": "EUR" } (all required)'),
    'company-statement-password': Action(company_action, 'key bank password', role='manage_firms', firm=False, public=False, strings=True, description='{ "key": "EXEMPLU", "bank": "bt", "password": "<parola>" } (all required; bank: a catalogue bank with password-protected PDF statements). Write-only: never returned or logged.'),
    'company-alias-add': Action(company_action, 'key alias', role='manage_firms', firm=False, public=False, strings=True, description='{ "key": "EXEMPLU", "alias": "Exemplu Grup" } (both required)'),
    'company-alias-remove': Action(company_action, 'key alias', role='manage_firms', firm=False, public=False, strings=True, description='{ "key": "EXEMPLU", "alias": "Exemplu Grup" } (both required)'),
    'statement-csv-format': Action(_statement_csv_format, 'account date_format decimal date amount debit credit description counterparty counterparty_iban reference', public=False, strings=True, description='account, co; column names from fiscalm.bank.csv.FIELDS plus date_format and decimal, all strings. App admin configuration.'),
    'bank-setup': Action(_bank_setup, 'operation', role='connect_integrations', firm=False, public=False, description='operation: prepare (generate key once, return public certificate and configured callback URLs; no network) or check (verify Enable Banking application). No private key or session is returned. Read-only banking allowed in shadow mode.'),
    'bank-connect': Action(_bank_connect, 'bank', public=False, role='connect_integrations', actor=True, description='bank (catalogue ID), co; no other fields. App admin; starts read-only account consent.'),
    'bank-disconnect': Action(_bank_disconnect, 'bank', public=False, role='connect_integrations', actor=True, description='bank (catalogue ID), co; no other fields. Stops future local bank reads and cancels pending consent choices. Keeps all history and statement imports; no provider call or remote revocation.'),
    'bank-accounts': Action(_bank_accounts, '', public=False, role='connect_integrations', actor=True, schema={"type": "object", "properties": {"operation": {"type": "string"}, "offer": {"type": "string"}, "accounts": {"type": "array", "items": {"type": "string"}}}}, description='operation=list returns only the creator’s unexpired masked account offers. operation=confirm, offer (opaque ID), accounts (opaque IDs) registers and connects exactly the selected accounts atomically. No IBAN, URL, session or provider UID is accepted.'),
    'integration-connect': Action(_integration_connect, 'id', role='connect_integrations', firm=False, public=False, actor=True, description='id (the ANAF integration)'),
}
# fmt: on
API_ACTIONS = tuple(name for name, spec in ACTIONS.items() if spec.public)
LOCAL_ACTIONS = tuple(name for name, spec in ACTIONS.items() if spec.effect == "local")


def act(
    action: str,
    co: str,
    args: dict,
    *,
    company_only: bool = False,
    created_by: str | None = None,
    authorize: Callable[[Ledger], None] | None = None,
    require: Callable[[str], bool] = lambda need: True,
    origin: str | None = None,
    at: datetime | None = None,
) -> dict:
    """Validate common policy once and own the action's ledger lifetime."""
    spec = ACTIONS.get(action)
    if spec is None:
        return _res(False, BAD)
    if not companies.all() and spec.firm:
        return _res(False, companies.NEEDS_COMPANY_RO)
    if spec.role and not require(spec.role):
        return _res(False, ROLE_REFUSALS.get(spec.role, BAD))
    if not spec.allowed_in_mode():
        return _res(False, SHADOW)
    if not isinstance(args, dict) or not spec.valid(args):
        return _res(False, cc.BAD if action.startswith("company-") else BAD)
    ctx = Context(
        action,
        args,
        at=at or datetime.now(UTC),
        company_only=company_only,
        created_by=created_by,
        authorize=(lambda: authorize(L)) if authorize is not None else (lambda: None),
        log=log,
        origin=origin,
        require=require,
    )
    L = Ledger()
    try:
        with L.guard_writes(authorize):
            if authorize is not None:
                with L.tx():
                    pass  # Admit filesystem-only actions too; later transactions recheck independently.
            return spec.handler(L, co, args, ctx)
    except spec.errors as exc:
        if action.startswith("payment_file_"):
            return _res(False, _payment_file_refusal(exc))
        return _res(False, str(exc))
    finally:
        L.close()


ROLE_REFUSALS = {
    "manage_users": "Doar administratorii pot gestiona utilizatorii.",
    "manage_tokens": "Doar administratorii pot gestiona linkurile de încărcare.",
    "own_tokens": "Contul nu poate gestiona cheile API.",
    "manage_firms": "Doar administratorii pot gestiona firmele.",
    "manage_rules": rules.ADMIN_REFUSAL,
    "connect_integrations": anaf.NOT_ADMIN,
    "manage_integrations": BAD,
}
