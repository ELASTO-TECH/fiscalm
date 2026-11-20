"""Server-only user administration; no passwords or existing credentials in output."""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from datetime import UTC, datetime

from fiscalm import backup
from fiscalm.api import limits, maintenance, public_url, sessions, setup_links, users
from fiscalm.ledger import Ledger, finance_home


def bootstrap(L: Ledger, at: datetime) -> tuple[str | None, str | None]:
    with L.tx() as db:
        if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            ident = users.create(L, "admin", "Administrator", "admin", at, actor_id=None)
            return ident, setup_links.issue(L, ident, at, actor_id=None)
        row = db.execute(
            "SELECT id FROM users WHERE role='admin' AND disabled_at IS NULL ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        return (row[0] if row else None), None


def print_link(path: str, origin: str | None) -> None:
    print(f"Link de configurare (se salvează acum, valabil 72 de ore): {origin or ''}{path}")


def _user(L: Ledger, name: str) -> dict:
    name = users.canonical_username(name)
    row = (
        L.db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone() if users.valid_username(name) else None
    )
    if not row:
        raise ValueError("Unknown user.")
    return dict(row)


def _list(L: Ledger, a: argparse.Namespace) -> None:
    print(
        json.dumps(
            [
                dict(r)
                for r in L.db.execute(
                    "SELECT id,username,display_name,role,disabled_at,locked_until FROM users ORDER BY username"
                )
            ],
            ensure_ascii=False,
        )
    )


def _create(L: Ledger, a: argparse.Namespace) -> None:
    origin, at = public_url.origin(https_only=True), datetime.now(UTC)
    with L.tx():
        ident = users.create(L, a.name, a.display_name or a.name, a.role, at, actor_id=None)
        path = setup_links.issue(L, ident, at, actor_id=None)
    print_link(path, origin)


def _link(L: Ledger, a: argparse.Namespace) -> None:
    origin = public_url.origin(https_only=True)
    path = setup_links.issue(L, _user(L, a.name)["id"], datetime.now(UTC), actor_id=None, reset=a.reset)
    print_link(path, origin)


def _change(L: Ledger, a: argparse.Namespace) -> None:
    L.change_user(_user(L, a.name)["id"], None, sessions.stamp(datetime.now(UTC)), role=a.role, enabled=a.enabled)
    print("User updated.")


def _unlock(L: Ledger, a: argparse.Namespace) -> None:
    limits.unlock(L, _user(L, a.name)["id"], datetime.now(UTC), actor_id=None)
    print("User unlocked.")


def _forget(L: Ledger, a: argparse.Namespace) -> None:
    L.forget_auth_sessions(sessions.stamp(datetime.now(UTC)))
    print("Sessions, tickets and setup links revoked.")


def _sweep(L: Ledger, a: argparse.Namespace) -> None:
    maintenance.sweep(L, datetime.now(UTC))
    print("Authentication cleanup complete.")


def execute(a: argparse.Namespace) -> None:
    backup.refuse_unfinished_restore(finance_home())
    if not (finance_home() / "state/ledger.db").is_file():
        raise ValueError("Run fiscalm init first.")
    with closing(Ledger()) as L:
        a.handler(L, a)


def add_parser(sub: argparse._SubParsersAction) -> tuple[argparse.ArgumentParser, ...]:
    """The `user` and `auth` commands; each opens the ledger only once `fiscalm init` has made it."""
    account = sub.add_parser("user", help="manage accounts on the server")
    account.set_defaults(step=execute)
    actions = account.add_subparsers(dest="operation", required=True)
    actions.add_parser("list", help="every account: name, role, disabled or locked").set_defaults(handler=_list)
    create = actions.add_parser("create", help="create an account and print its one-time setup link")
    create.add_argument("name")
    create.add_argument("--display-name")
    create.add_argument("--role", choices=("admin", "member", "readonly"), default="member")
    create.set_defaults(handler=_create)
    for command, handler, hlp in (
        ("setup-link", _link, "print a new one-time setup link for an account"),
        ("reset", _link, "print a one-time link to replace the account's sign-in"),
        ("unlock", _unlock, "lift the lock left by failed sign-ins"),
        ("role", _change, "change an account's role"),
        ("enable", _change, "let a disabled account sign in again"),
        ("disable", _change, "stop an account from signing in"),
    ):
        parser = actions.add_parser(command, help=hlp)
        parser.add_argument("name")
        if command == "role":
            parser.add_argument("role", choices=("admin", "member", "readonly"))
        else:
            parser.set_defaults(role=None)
        parser.set_defaults(
            handler=handler,
            reset=command == "reset",
            enabled=True if command == "enable" else False if command == "disable" else None,
        )
    auth = sub.add_parser("auth", help="browser access recovery and local retention")
    auth.set_defaults(step=execute)
    auth_actions = auth.add_subparsers(required=True)
    auth_actions.add_parser(
        "forget-sessions", help="revoke every browser session, sign-in ticket and setup link"
    ).set_defaults(handler=_forget)
    auth_actions.add_parser(
        "sweep", help="delete ended sessions, tickets and setup links, and old failed sign-ins"
    ).set_defaults(handler=_sweep)
    return account, auth
