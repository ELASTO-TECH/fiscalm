"""Invoice CLI boundaries: explicit files, safe summaries, no printed bank details or tokens."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from fiscalm import bnr
from fiscalm.commerce import emag_sync, webhooks as commerce_webhooks
from fiscalm.efactura import client as anaf
from fiscalm.efactura.partners import refresh
from fiscalm.invoicing import branding, engine as i, pdf as invoice_pdf, recurring, spv, ubl
from fiscalm.ledger import Ledger, is_live, now


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register invoice commands without changing existing CLI behavior."""
    emag = subparsers.add_parser("emag", help="eMAG shops connected as integrations")
    emag_commands = emag.add_subparsers(dest="emag_cmd", required=True)
    emag_commands.add_parser("tick", help="read new eMAG orders and attach their invoices, once").set_defaults(
        fn=emag_tick
    )
    commands = subparsers.add_parser(
        "invoice", aliases=["invoicing"], help="Create, issue and export invoices"
    ).add_subparsers(dest="invoice_cmd", required=True)
    recur = commands.add_parser("recur", help="recurring invoices issued from a draft every month")
    recur = recur.add_subparsers(dest="recur_cmd", required=True)
    for op, hlp in (
        ("run", "issue the invoices that are due"),
        ("create", "make a recurring invoice from a draft"),
        ("list", "the firm's recurring invoices"),
        ("pause", "stop issuing from one"),
        ("resume", "issue from one again"),
        ("delete", "delete one; drafts already made stay"),
    ):
        parser = recur.add_parser(op, help=hlp)
        if op != "run":
            parser.add_argument("--company", required=True)
        if op in ("run", "create"):
            parser.add_argument("--today", required=True, type=date.fromisoformat)
        if op == "create":
            parser.add_argument("--draft", required=True)
            parser.add_argument("--first-period")
            parser.add_argument("--day", required=True, type=int)
            parser.add_argument("--due-days", required=True, type=int)
        if op in ("pause", "resume", "delete"):
            parser.add_argument("id")
        parser.set_defaults(fn=run)
    commands.add_parser("partner-status", help="refresh the next clients' ANAF taxpayer status").set_defaults(fn=run)
    logos = commands.add_parser("logo", help="the logo on a firm's invoices")
    logos = logos.add_subparsers(dest="logo_cmd", required=True)
    for op, hlp in (("set", "use this image as the firm's logo"), ("remove", "remove the firm's logo")):
        parser = logos.add_parser(op, help=hlp)
        parser.add_argument("--company", required=True)
        if op == "set":
            parser.add_argument("file", type=Path)
        parser.set_defaults(fn=run)
    for name, hlp in (
        ("seller", "save a firm's seller details from JSON"),
        ("client", "save a client from JSON"),
        ("draft", "save a draft invoice from JSON"),
    ):
        parser = commands.add_parser(name, help=hlp)
        parser.add_argument("file", type=Path, help="JSON input file")
        if name == "draft":
            parser.add_argument("--id", help="Replace an existing draft")
            parser.add_argument("--version", type=int, help="Required with --id; version from show")
        parser.set_defaults(fn=run)
    _register_series(commands)
    for name, hlp in (
        ("issue", "give a draft its number and issue it"),
        ("storno", "issue a storno of an issued invoice, whole or some lines"),
        ("upload", "upload an issued invoice to ANAF SPV"),
        ("show", "an invoice's number, status and version"),
        ("delete", "delete a draft"),
        ("xml", "write an invoice's e-Factura XML"),
        ("pdf", "write an invoice's PDF"),
    ):
        parser = commands.add_parser(name, help=hlp)
        parser.add_argument("id")
        parser.add_argument("--company", required=True)
        if name == "issue":
            parser.add_argument("--version", type=int, required=True)
        if name in ["issue", "storno"]:
            parser.add_argument("--today", default=None)
        if name == "storno":
            parser.add_argument("--reason", required=True)
            parser.add_argument("--lines", nargs="+", type=int)
        if name in ["xml", "pdf"]:
            parser.add_argument("--output", required=True, type=Path)
        parser.set_defaults(fn=run)
    for name, hlp in (
        ("resolve", "after checking SPV, say what happened to an upload with an unknown result"),
        ("recover", "find an upload with an unknown result among the firm's sent invoices in SPV"),
    ):
        parser = commands.add_parser(name, help=hlp)
        parser.add_argument("id")
        parser.add_argument("--company", required=True)
        if name == "resolve":
            choice = parser.add_mutually_exclusive_group(required=True)
            choice.add_argument("--upload-id")
            choice.add_argument("--not-uploaded", action="store_true")
            parser.add_argument("--checked-in-spv", action="store_true", required=True)
        else:
            parser.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
            parser.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
        parser.set_defaults(fn=run)
    for name, hlp in (
        ("poll", "fetch ANAF's answer for uploads still waiting"),
        ("tick", "upload issued invoices not yet uploaded, then poll"),
    ):
        parser = commands.add_parser(name, help=hlp)
        if name == "tick":
            parser.add_argument("--today", default=None)
        parser.set_defaults(fn=run)
    parser = commands.add_parser("rate", help="the BNR exchange rate for a currency and day")
    parser.add_argument("currency")
    parser.add_argument("--date", required=True)
    parser.set_defaults(fn=run)
    parser = commands.add_parser("lookup-client", help="save a client from ANAF's public register by CUI")
    parser.add_argument("--company", required=True)
    parser.add_argument("--cui", required=True)
    parser.add_argument("--date", required=True)
    parser.set_defaults(fn=run)
    parser = commands.add_parser(
        "auto-upload", help="let the tick upload a firm's invoices to SPV (when: the ANAF integration), or not"
    )
    parser.add_argument("--company", required=True)
    parser.add_argument("enabled", choices=["on", "off"])
    parser.set_defaults(fn=run)


def _register_series(commands: argparse._SubParsersAction) -> None:
    series = commands.add_parser("series", help="a firm's invoice number series")
    series = series.add_subparsers(dest="series_cmd", required=True)
    for op, hlp in (
        ("list", "the firm's series, the default first"),
        ("create", "add a series; its numbers start at --start"),
        ("default", "the series new invoices start in"),
        ("remove", "remove a series no invoice, draft or recurring invoice uses"),
    ):
        parser = series.add_parser(op, help=hlp)
        parser.add_argument("--company", required=True)
        if op != "list":
            parser.add_argument("--code", required=True)
        if op == "create":
            parser.add_argument("--start", type=int, default=1)
        parser.set_defaults(fn=run)


def _summary(inv: dict) -> dict:
    return {key: inv[key] for key in ["id", "company", "series", "number", "status", "version"]}


def tick(L: Ledger, today: str) -> dict:
    """Upload unattempted issued invoices that are due (spv.upload_on), then poll; shadow permits TEST only."""
    if not spv.anaf.outward_allowed():
        return {"status": "shadow", "submitted": 0, "polled": 0}
    ids = list(
        L.db.execute(
            "SELECT id,company FROM invoices WHERE status!='draft' AND id NOT IN (SELECT invoice_id FROM invoice_submissions WHERE environment=?)",
            (spv.anaf.env(),),
        )
    )
    submitted = [
        spv.auto_upload(L, row[0], company=row[1], today=today)
        for row in ids
        if i.invoice(L, row[0], company=row[1])["data"].get("issued_live", False)
    ]
    results = i.poll(L)
    commerce_webhooks.tick(L)
    return {
        "status": "live" if is_live() else "shadow",
        "environment": spv.anaf.env(),
        "submitted": sum(row["status"] == "uploaded" for row in submitted),
        "polled": len(results),
        "errors": sum(row["status"] == "error" for row in submitted + results),
    }


def run(args: argparse.Namespace, L: Ledger) -> int:
    """Execute invoice commands; input errors return 2 and never echo input documents or secrets."""
    try:
        command = args.invoice_cmd
        result: dict | list
        handlers = {
            "recur": _run_recur,
            "partner-status": _run_partner_status,
            "logo": _run_logo,
            "seller": _run_input,
            "client": _run_input,
            "draft": _run_input,
            "series": _run_series,
            "issue": _run_issue,
            "storno": _run_issue,
            "show": _run_show,
            "delete": _run_delete,
            "xml": _run_export,
            "pdf": _run_export,
            "upload": _run_upload,
            "resolve": _run_resolve,
            "recover": _run_recover,
            "poll": _run_poll,
            "tick": _run_tick,
            "auto-upload": _run_auto_upload,
            "lookup-client": _run_lookup_client,
            "rate": _run_rate,
        }
        result = handlers.get(command, _unknown_command)(args, L)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except anaf.AnafError:
        print(
            json.dumps(
                {"status": "refused", "reason": "Check the ANAF integration settings and retry the invoice command."}
            )
        )
        return 2
    except i.InvoiceError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    except KeyError as exc:
        print(json.dumps({"status": "refused", "reason": f"Missing required invoice key: {exc.args[0]}."}))
        return 2
    except (OSError, ValueError, TypeError, AttributeError):
        print(
            json.dumps({"status": "refused", "reason": "Check the invoice command input and output file permissions."})
        )
        return 2


def _run_recur(args: argparse.Namespace, L: Ledger) -> dict | list:
    if args.recur_cmd == "run":
        result = recurring.report(L, args.today.isoformat())
    elif args.recur_cmd == "create":
        result = {
            "id": recurring.create(
                L, args.company, args.draft, args.day, args.due_days, args.today.isoformat(), args.first_period
            )
        }
    elif args.recur_cmd == "list":
        result = recurring.list_rules(L, args.company, now()[:10])
    else:
        recurring.change(L, args.company, args.id, args.recur_cmd)
        result = {"status": args.recur_cmd}
    return result


def _run_partner_status(args: argparse.Namespace, L: Ledger) -> dict | list:
    return refresh(L, date.today())


def _run_logo(args: argparse.Namespace, L: Ledger) -> dict | list:
    if args.logo_cmd == "set":
        with args.file.open("rb") as stream:
            branding.set_logo(L, args.company, stream.read(branding.MAX_BYTES + 1))
    else:
        branding.remove_logo(L, args.company)
    return {"status": "saved" if args.logo_cmd == "set" else "removed"}


def _run_input(args: argparse.Namespace, L: Ledger) -> dict | list:
    command = args.invoice_cmd
    if args.file.stat().st_size > 1024 * 1024:
        raise i.InvoiceError("Invoice input exceeds 1 MB.")
    value = json.loads(args.file.read_text(), parse_float=str)
    if command == "seller":
        i.save_seller(L, value["company"], value["profile"])
        result = {"status": "saved"}
    elif command == "client":
        result = {"id": i.save_client(L, value["company"], value["profile"])}
    else:
        iid = i.save_draft(
            L,
            value["company"],
            value["series"],
            value["client_id"],
            value["data"],
            args.id,
            version=args.version,
        )
        result = _summary(i.invoice(L, iid, company=value["company"]))
    return result


def _run_series(args: argparse.Namespace, L: Ledger) -> dict | list:
    command = args.series_cmd
    if command == "list":
        return sorted(i.series_list(L, args.company), key=lambda x: not x["default"])
    if command == "create":
        i.create_series(L, args.company, args.code, args.start)
    elif command == "default":
        i.set_default_series(L, args.company, args.code)
    else:
        i.remove_series(L, args.company, args.code)
    return {"status": {"create": "created", "default": "saved", "remove": "removed"}[command]}


def _run_issue(args: argparse.Namespace, L: Ledger) -> dict | list:
    command = args.invoice_cmd
    today = args.today or date.today().isoformat()
    inv = (
        i.issue(L, args.id, today, company=args.company, version=args.version)
        if command == "issue"
        else i.storno(L, args.id, today, args.reason, args.lines, company=args.company)
    )
    return _summary(inv) | {"upload_on": spv.upload_on(L, inv)}


def _run_show(args: argparse.Namespace, L: Ledger) -> dict | list:
    return _summary(i.invoice(L, args.id, company=args.company))


def _run_delete(args: argparse.Namespace, L: Ledger) -> dict | list:
    i.delete_draft(L, args.id, company=args.company)
    return {"status": "deleted"}


def _run_export(args: argparse.Namespace, L: Ledger) -> dict | list:
    command = args.invoice_cmd
    raw = (
        ubl.generate(L, args.id, company=args.company)
        if command == "xml"
        else invoice_pdf.render(L, args.id, company=args.company)
    )
    with args.output.open("xb") as stream:
        stream.write(raw)
    return {"status": "written", "file": str(args.output)}


def _run_upload(args: argparse.Namespace, L: Ledger) -> dict | list:
    state = i.upload(L, args.id, company=args.company)
    return {"status": state["status"], "upload_id": state["upload_id"]}


def _run_resolve(args: argparse.Namespace, L: Ledger) -> dict | list:
    state = spv.resolve(L, args.id, company=args.company, upload_id=args.upload_id, checked_in_spv=args.checked_in_spv)
    return {"status": state["status"] if state else "not_uploaded"}


def _run_recover(args: argparse.Namespace, L: Ledger) -> dict | list:
    if args.end < args.start:
        raise i.InvoiceError("Recovery end date cannot precede start date.")
    state = spv.recover_sent(
        L,
        args.id,
        company=args.company,
        start=datetime.combine(args.start, datetime.min.time()),
        end=datetime.combine(args.end, datetime.max.time()),
    )
    return {"status": state["status"], "upload_id": state["upload_id"]}


def _run_poll(args: argparse.Namespace, L: Ledger) -> dict | list:
    return [{"invoice_id": r["invoice_id"], "status": r["status"]} for r in i.poll(L)]


def _run_tick(args: argparse.Namespace, L: Ledger) -> dict | list:
    return tick(L, args.today or date.today().isoformat())


def _run_auto_upload(args: argparse.Namespace, L: Ledger) -> dict | list:
    spv.set_auto_upload(L, args.company, args.enabled == "on")
    return {"status": "saved"}


def _run_lookup_client(args: argparse.Namespace, L: Ledger) -> dict | list:
    return {"id": i.fill_client(L, args.company, args.cui, args.date)}


def _run_rate(args: argparse.Namespace, L: Ledger) -> dict | list:
    rate, day = bnr.rate(L, args.currency, args.date)
    return {"rate": rate, "publication_date": day}


def _unknown_command(args: argparse.Namespace, L: Ledger) -> dict:
    raise i.InvoiceError("Unknown invoice command.")


def emag_tick(args: argparse.Namespace, L: Ledger) -> int:
    print(json.dumps(emag_sync.tick(L)))
    return 0
