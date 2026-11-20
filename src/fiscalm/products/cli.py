"""CLI catalog commands use the same decisions as app actions."""

from __future__ import annotations

import argparse
import json

from fiscalm.ledger import Ledger
from fiscalm.products import engine
from fiscalm.products.schema import FIELDS


def command(args: argparse.Namespace, L: Ledger) -> int:
    try:
        if args.product_op == "list":
            result = engine.catalog(L, args.company)
        else:
            data = {k: getattr(args, k) for k in (*FIELDS, "id", "version") if getattr(args, k, None) is not None}
            if args.product_op == "archive":
                data["archived"] = not args.restore
            result = engine.write(L, args.company, "product-" + args.product_op, data)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except engine.ProductError as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False))
        return 1


def register(sub: argparse._SubParsersAction) -> None:
    commands = sub.add_parser("product", help="firm product and service catalog").add_subparsers(
        dest="product_op", required=True
    )
    for op, help_text in {
        "add": "add a product or service to the firm's catalog",
        "list": "list the firm's catalog, including archived entries",
        "update": "edit a product's defaults at its current version",
        "archive": "archive a product, or restore it with --restore",
        "delete": "delete a product only if no invoice has ever referenced it",
    }.items():
        parser = commands.add_parser(op, help=help_text)
        parser.add_argument("--company", required=True)
        parser.set_defaults(fn=command)
        if op in ("update", "archive", "delete"):
            parser.add_argument("id")
            parser.add_argument("--version", type=int, required=True)
        if op == "archive":
            parser.add_argument("--restore", action="store_true")
        if op in ("add", "update"):
            for field in FIELDS:
                parser.add_argument(
                    "--" + field.replace("_", "-"),
                    required=op == "add" and field not in ("code", "description", "exemption"),
                )
