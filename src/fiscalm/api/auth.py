"""Account roles and live session authorization, shared by browser dispatch and guarded writes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from fiscalm.api import sessions, users
from fiscalm.ledger import Ledger

REAUTH_MESSAGE = "Confirmă din nou identitatea ca să continui."


def capabilities(user: Mapping | None, *, bearer: bool = False, has_admin: bool = True) -> dict[str, bool]:
    app = user is not None and not bearer and not user.get("recovery_only", False)
    admin = app and user["role"] == "admin"
    write = app and user["role"] in ("admin", "member")
    return dict(
        read=app,
        member=write,
        write=write,
        manage_users=admin,
        manage_firms=admin,
        manage_tokens=admin,
        own_tokens=app,
        manage_rules=admin,
        manage_integrations=admin or (write and not has_admin),
        connect_integrations=admin,
    )


def require(user: Mapping | None, need: str, *, bearer: bool = False, has_admin: bool = True) -> bool:
    return capabilities(user, bearer=bearer, has_admin=has_admin).get(need, False)


def has_admin(L: Ledger) -> bool:
    return L.db.execute("SELECT 1 FROM users WHERE role='admin' AND disabled_at IS NULL LIMIT 1").fetchone() is not None


def authorize_write(L: Ledger, principal: dict, at: datetime, need: str = "member", *, recent: bool = False) -> None:
    """Called while the engine holds its write lock, before its guarded mutation."""
    current = sessions.lookup(L, principal["id_hash"], at)
    if not current or current["user_id"] != principal["user_id"] or not require(current, need, has_admin=has_admin(L)):
        raise PermissionError("session_not_authorized")
    if recent and not sessions.recent(current, at):
        raise PermissionError("reauth_required")


def needs_recent(action: str) -> bool:
    return action.startswith("user-") or action in (
        "mode-switch",
        "session-close",
        "token-create",
    )


def me(L: Ledger, principal: dict, at: datetime, *, passkeys_available: bool = False) -> dict:
    row = L.db.execute(
        "SELECT id,username,display_name,role,password_hash,totp_version FROM users WHERE id=?", (principal["user_id"],)
    ).fetchone()
    return dict(
        me={k: row[k] for k in ("id", "username", "display_name", "role")}
        | dict(can=capabilities(principal, has_admin=has_admin(L))),
        auth=dict(via="cookie", method=principal["method"], recovery_only=bool(principal["recovery_only"])),
        factors=dict(
            password_totp=bool(row["password_hash"] and row["totp_version"]),
            passkeys=users.passkey_listing(L, principal["user_id"]),
            passkeys_available=passkeys_available,
        ),
        recovery_codes_remaining=L.db.execute(
            "SELECT count(*) FROM recovery_codes WHERE user_id=? AND used_at IS NULL", (principal["user_id"],)
        ).fetchone()[0],
        sessions=[
            entry | {"current": entry["id"] == principal["id_hash"]}
            for entry in users.active_sessions(L, principal["user_id"], at)
        ],
    )
