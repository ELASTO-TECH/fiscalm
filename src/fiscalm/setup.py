"""First-run commands, deliberately independent of company config imports."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fiscalm import backup, companies
from fiscalm.api import public_url, tokens, user_cli
from fiscalm.config_errors import ConfigError
from fiscalm.ledger import Ledger, finance_home, is_live
from fiscalm.migrations import SchemaError
from fiscalm.secrets import secrets_dir
from fiscalm.validate import iban_valid


class Once(argparse.Action):
    """An option given twice is refused: two homes on one command line is never something to guess about."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} given more than once")
        setattr(namespace, self.dest, values)


def split_home(args: list[str]) -> tuple[str | None, list[str]]:
    """`--home` wherever it stands, at most once and never abbreviated, and the arguments around it."""
    pre = argparse.ArgumentParser(prog="fiscalm", add_help=False, allow_abbrev=False)
    pre.add_argument("--home", action=Once)
    home, rest = pre.parse_known_args(args)
    return home.home, rest


def apply_home(home: str) -> None:
    """Keep explicit path overrides; isolate unspecified config and credentials."""
    root = Path(home).expanduser().resolve()
    os.environ["FISCALM_HOME"] = str(root)
    os.environ.setdefault("FISCALM_SECRETS", str(root / "secrets"))


def _company_argument(a: argparse.Namespace) -> companies.Company | None:
    fields = {k: getattr(a, k) for k in ("company_key", "cui", "legal_name", "iban")}
    company = None
    if any(v is not None for v in fields.values()):
        for key in fields:
            if not fields[key]:
                if not sys.stdin.isatty():
                    raise ValueError(f"Provide --{key.replace('_', '-')} or run interactively.")
                try:
                    fields[key] = input(key.replace("_", " ") + ": ").strip()
                except EOFError:
                    raise ValueError("Input ended. Provide all company flags or run interactively.") from None
        if not iban_valid(fields["iban"]):
            raise ValueError("Provide a valid IBAN (mod-97 checksum).")
        company = companies.company_from(
            dict(
                key=fields["company_key"],
                cui=fields["cui"],
                legal_name=fields["legal_name"],
                accounts=[dict(iban=fields["iban"], currency="RON", pays=True)],
            ),
            0,
        )
    return company


def _private_ledger() -> Ledger:
    old_umask = os.umask(0o077)
    try:
        ledger = Ledger()
    finally:
        os.umask(old_umask)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(ledger.path) + suffix)
        if path.exists():
            path.chmod(0o600)
    return ledger


def initialize(a: argparse.Namespace) -> None:
    backup.refuse_unfinished_restore(finance_home())
    origin, company = public_url.origin(https_only=True), _company_argument(a)
    home = finance_home()
    for directory in ("", "state", "config", "logs", "efactura", "inbox", "De plătit"):
        p = home / directory
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    secrets_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    secrets_dir().chmod(0o700)
    with closing(_private_ledger()) as L, L.tx():
        if company is not None:
            if companies.all(L):
                raise ValueError("Există deja firme configurate. Firmele se adaugă sau se modifică din fiscalm.")
            L.save_company(company)
        admin, link = user_cli.bootstrap(L, datetime.now(UTC))
        credential = None
        if not L.db.execute("SELECT 1 FROM api_tokens LIMIT 1").fetchone():
            if admin is None:
                raise ValueError("Mai întâi se creează un administrator cu fiscalm user create --role admin.")
            credential = tokens.create(L, "Acces API inițial", "write", user_id=admin)
        if L.get_meta("selfhost_initialized") != "yes":
            L.set_meta("selfhost_initialized", "yes")
    if link:
        user_cli.print_link(link, origin)
    if credential:
        print("Cheie API (se salvează acum): " + credential["token"])
    print(
        "Modul Live este deja activ."
        if is_live()
        else "Sandbox: lucrul local și trimiterea la ANAF Test sunt permise. Modul Live se alege în Setări › Mod."
    )


def _live(a: argparse.Namespace) -> None:
    backup.refuse_unfinished_restore(finance_home())
    if a.mode == "on":
        companies.require_company()
    if a.mode in ("on", "off"):
        with closing(Ledger()) as L:
            L.change_mode(a.mode == "on", is_live(), "cli", datetime.now(UTC).isoformat(), uuid4().hex)
    print("live" if is_live() else "shadow")


def register(sub: argparse._SubParsersAction) -> None:
    """`init`, `live`, `user` and `auth`: they run without a configured company and open the ledger themselves."""
    init = sub.add_parser("init", help="create a private Sandbox installation")
    init.set_defaults(step=initialize)
    for flag in ("company-key", "cui", "legal-name", "iban"):
        init.add_argument("--" + flag)
    live = sub.add_parser("live", help="control outward effects")
    live.add_argument("mode", choices=("on", "off", "status"))
    live.set_defaults(step=_live)
    for parser in (init, live, *user_cli.add_parser(sub)):
        parser.set_defaults(run=run)


def run(a: argparse.Namespace) -> int:
    try:
        a.step(a)
        return 0
    except (ValueError, ConfigError, SchemaError) as e:
        print(str(e), file=sys.stderr)
        return 1
    except OSError:
        print(
            "Configurarea nu a reușit. Se verifică setările și căile sau se rulează fiscalm init --help.",
            file=sys.stderr,
        )
        return 1
