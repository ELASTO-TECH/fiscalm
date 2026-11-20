"""Atomic password/TOTP setup, replacement and recovery, bound to the current account."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from fiscalm.api import auth, passwords, recovery_codes, sessions, setup_links, signin, totp_store
from fiscalm.ledger import Ledger


def setup_account(L: Ledger, fragment: str, at: datetime) -> dict | None:
    """A current setup capability for an account that still has no configured factor."""
    link = setup_links.inspect(L, fragment, at)
    if not link:
        return None
    row = L.db.execute("SELECT password_hash,totp_version FROM users WHERE id=?", (link["user_id"],)).fetchone()
    configured = (
        any(row[k] is not None for k in ("password_hash", "totp_version"))
        or L.db.execute("SELECT 1 FROM passkeys WHERE user_id=?", (link["user_id"],)).fetchone()
    )
    return None if configured else link


def _setup_ticket(L: Ledger, ident: str, link: dict) -> bool:
    if not sessions.ID.fullmatch(ident):
        return False
    row = L.db.execute(
        "SELECT user_id,challenge FROM auth_tickets WHERE id_hash=? AND purpose='totp_enroll' AND session_hash IS NULL",
        (sessions.digest(ident),),
    ).fetchone()
    return bool(
        row and row["user_id"] == link["user_id"] and json.loads(row["challenge"]).get("setup_id") == link["id"]
    )


def recovery_session(L: Ledger, principal: dict, at: datetime, *, method: str = "recovery") -> dict | None:
    current = sessions.lookup(L, principal["id_hash"], at)
    return (
        current
        if current
        and current["user_id"] == principal["user_id"]
        and current["recovery_only"]
        and current["method"] == method
        else None
    )


def _factor_ticket(L: Ledger, principal: dict, ident: str) -> bool:
    return bool(
        sessions.ID.fullmatch(ident)
        and L.db.execute(
            "SELECT 1 FROM auth_tickets WHERE id_hash=? AND user_id=? AND session_hash=? AND purpose='totp_enroll'",
            (sessions.digest(ident), principal["user_id"], principal["id_hash"]),
        ).fetchone()
    )


EXPIRED = "Configurarea a expirat. Reîncepe configurarea."


def _refusal(message: str = signin.FAILED) -> signin.Result:
    return signin.Result(401, dict(ok=False, message=message))


def _wrong_code(L: Ledger, ident: str, at: datetime) -> signin.Result:
    """Asked only after the caller proved its own link or session: the attempt was already counted."""
    row = L.db.execute(
        "SELECT attempts,expires_at FROM auth_tickets WHERE id_hash=? AND used_at IS NULL", (sessions.digest(ident),)
    ).fetchone()
    live = row and row["attempts"] < 5 and row["expires_at"] > sessions.stamp(at)
    return _refusal(signin.WRONG_CODE if live else EXPIRED)


class Enrolment:
    def __init__(self, clock: Callable[[], datetime], hasher: passwords.Passwords) -> None:
        self.clock, self.hasher = clock, hasher  # Shared with Signin: one KDF semaphore per server.

    def _hash(self, password: str) -> str | signin.Result:
        try:
            return self.hasher.hash(password)  # Never hash while holding the ledger lock.
        except passwords.PasswordBusy:
            return signin.Result(503, dict(ok=False, message=signin.BUSY), retry_after=5)
        except ValueError as exc:  # passwords.normalize states its own rule, never the password.
            return signin.Result(400, dict(ok=False, message=str(exc)))

    def change_password(self, L: Ledger, principal: dict, password: str) -> signin.Result:
        with L.tx():
            auth.authorize_write(L, principal, self.clock(), "read", recent=True)
            user = dict(L.db.execute("SELECT * FROM users WHERE id=?", (principal["user_id"],)).fetchone())
        hashed = self._hash(password)
        if isinstance(hashed, signin.Result):
            return hashed
        with L.tx():
            at = self.clock()
            auth.authorize_write(L, principal, at, "read", recent=True)
            L.change_password(user, hashed, principal["id_hash"], sessions.stamp(at))
        return signin.Result(200, dict(ok=True))

    def begin_totp(self, L: Ledger, principal: dict) -> signin.Result:
        with L.tx():
            at = self.clock()
            auth.authorize_write(L, principal, at, "read", recent=True)
            user = L.db.execute(
                "SELECT password_hash,totp_version FROM users WHERE id=?", (principal["user_id"],)
            ).fetchone()
            if not user["password_hash"] or not user["totp_version"]:
                return _refusal()
            data = totp_store.publish(
                L, principal["user_id"], user["password_hash"], at, session_hash=principal["id_hash"]
            )
        return signin.Result(200, dict(ok=True, **data))

    def finish_totp(self, L: Ledger, principal: dict, ticket: str, code: str) -> signin.Result:
        with L.tx():
            at = self.clock()
            auth.authorize_write(L, principal, at, "read", recent=True)
            if not _factor_ticket(L, principal, ticket):
                return _refusal()
            if not totp_store.finish(L, ticket, code, at, session_hash=principal["id_hash"]):
                return _wrong_code(L, ticket, at)  # Wrong-code attempts commit; the old factor stays active.
            codes = recovery_codes.generate(L, principal["user_id"], at)
        return signin.Result(200, dict(ok=True, codes=codes))

    def begin_setup(self, L: Ledger, fragment: str, password: str) -> signin.Result:
        if not setup_account(L, fragment, self.clock()):
            return _refusal()
        hashed = self._hash(password)
        if isinstance(hashed, signin.Result):
            return hashed
        with L.tx():
            at = self.clock()
            link = setup_account(L, fragment, at)
            if not link:
                return _refusal()
            data = totp_store.publish(L, link["user_id"], hashed, at, setup_id=link["id"])
            return signin.Result(200, dict(ok=True, **data))

    def finish_setup(
        self,
        L: Ledger,
        fragment: str,
        ticket: str,
        code: str,
        *,
        presented: dict | None = None,
        agent: str = "",
        ip: str = "",
    ) -> signin.Result:
        with L.tx():
            at = self.clock()
            link = setup_account(L, fragment, at)
            if not link or not _setup_ticket(L, ticket, link):
                return _refusal()
            if not totp_store.finish(L, ticket, code, at):
                return _wrong_code(L, ticket, at)  # Commit the wrong-code attempt; leave the link unused.
            if not setup_links.consume(L, fragment, at):
                raise PermissionError("setup_link_changed")
            codes = recovery_codes.generate(L, link["user_id"], at)
            if presented:
                L.revoke_session(presented["id_hash"], presented["user_id"], sessions.stamp(at))
            ident = sessions.issue(L, link["user_id"], at, agent=agent, ip=ip)
            L.record_signin(link["user_id"], "password_totp", sessions.stamp(at), ip)
            return signin.Result(200, dict(ok=True, codes=codes), session_id=ident)

    def begin_recovery(self, L: Ledger, principal: dict) -> signin.Result:
        with L.tx():
            at = self.clock()
            current = recovery_session(L, principal, at)
            if not current:
                return _refusal()
            user = L.db.execute("SELECT password_hash FROM users WHERE id=?", (current["user_id"],)).fetchone()
            data = totp_store.publish(L, current["user_id"], user[0], at, session_hash=current["id_hash"])
            return signin.Result(200, dict(ok=True, **data))

    def finish_recovery(
        self, L: Ledger, principal: dict, ticket: str, code: str, *, agent: str = "", ip: str = ""
    ) -> signin.Result:
        with L.tx():
            at = self.clock()
            current = recovery_session(L, principal, at)
            if not current or not _factor_ticket(L, current, ticket):
                return _refusal()
            if not totp_store.finish(L, ticket, code, at, session_hash=current["id_hash"]):
                return _wrong_code(L, ticket, at)  # Failed attempts stay counted even inside this outer transaction.
            codes = recovery_codes.generate(L, current["user_id"], at)
            L.finish_recovery(current["user_id"], sessions.stamp(at))
            # This recovery proved the password, a recovery code and the new TOTP: all a sign-in asks.
            user = dict(id=current["user_id"], username=current["username"])
            return replace(signin.complete(L, user, at, agent=agent, ip=ip), body=dict(ok=True, codes=codes))
