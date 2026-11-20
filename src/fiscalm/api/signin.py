"""Password proof issues only a ticket; a consumed second factor issues a fresh session."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fiscalm import secrets as store
from fiscalm.api import limits, passwords, recovery_codes, sessions, tickets, totp, totp_store, users
from fiscalm.ledger import Ledger

FAILED = "Conectarea nu a reușit."
BUSY = "Autentificarea este temporar indisponibilă. Încearcă din nou."
# Said only to whoever holds a ticket, which the password (or the account's own setup link) already earned.
WRONG_CODE = "Codul nu este corect. Verifică ora telefonului și încearcă din nou."


@dataclass(frozen=True)
class Result:
    status: int
    body: dict[str, object]
    session_id: str | None = None  # Transport sets only the HttpOnly cookie, never JSON.
    retry_after: int = 0
    clear_session: bool = False


def _refusal(status: int = 401, retry: int = 0) -> Result:
    return Result(status, dict(ok=False, message=FAILED), retry_after=retry)


def _user(L: Ledger, username: str) -> dict | None:
    if not users.valid_username(username):
        return None
    row = L.db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    return dict(row) if row else None


def _principal(L: Ledger, principal: dict, at: datetime) -> dict | None:
    current = sessions.lookup(L, principal["id_hash"], at)
    return current if current and current["user_id"] == principal["user_id"] and not current["recovery_only"] else None


def _live_recovery(L: Ledger, ident: str, user_id: str, at: datetime) -> bool:
    """Read-only check before hashing; the attempt is still claimed and consumed under the write lock."""
    return bool(
        sessions.ID.fullmatch(ident)
        and L.db.execute(
            "SELECT 1 FROM auth_tickets WHERE id_hash=? AND purpose='recovery' AND user_id=? AND session_hash IS NULL"
            " AND used_at IS NULL AND attempts<5 AND expires_at>?",
            (sessions.digest(ident), user_id, sessions.stamp(at)),
        ).fetchone()
    )


def _blocked(L: Ledger, username: str, at: datetime) -> Result | None:
    state = limits.state(L, username, at)
    if state["locked"]:
        return _refusal()
    return _refusal(429, state["retry_after"]) if state["retry_after"] else None


def _failure(L: Ledger, username: str, at: datetime, reason: str, ip: str) -> Result:
    limits.failure(L, username, at, reason=reason, ip=ip)
    return _refusal()


class Signin:
    def __init__(self, clock: Callable[[], datetime], hasher: passwords.Passwords | None = None) -> None:
        self.clock = clock
        self.hasher = hasher if hasher is not None else passwords.Passwords()

    def password(
        self,
        L: Ledger,
        username: str,
        supplied: str,
        *,
        principal: dict | None = None,
        recovery: str = "",
        agent: str = "",
        ip: str = "",
    ) -> Result:
        """With a recovery ticket from recover(), the proved password opens the recovery session."""
        at = self.clock()
        current = _principal(L, principal, at) if principal is not None else None
        if principal is not None and current is None:
            return _refusal()
        username = current["username"] if current else users.canonical_username(username)
        try:
            reservation = limits.reserve(L, username, at)
        except PermissionError:
            return _refusal(429, 1)
        if not reservation:
            return self._unreserved(L, username, supplied)
        user = _user(L, username)
        encoded = user["password_hash"] if user and not user["disabled_at"] and user["totp_version"] else None
        if recovery and not (user and _live_recovery(L, recovery, user["id"], at)):
            encoded = None  # Without its own ticket, recovery takes the dummy path: no hash cost reveals the account.
        try:
            valid, upgrade = self.hasher.verify(supplied, encoded)
            # No upgrade during recovery: its extra hash would time a correct password before the ticket is checked.
            replacement = self.hasher.hash(supplied) if upgrade and not recovery else None
        except passwords.PasswordBusy:
            limits.release(L, username, reservation, self.clock())
            return Result(503, dict(ok=False, message=BUSY), retry_after=5)
        except BaseException:
            limits.release(L, username, reservation, self.clock())
            raise
        with L.tx():
            at = self.clock()
            if not limits.release(L, username, reservation, at):
                return _refusal()
            proof = dict(valid=valid, replacement=replacement, recovery=recovery)
            return self._password_ticket(L, username, user, proof, principal, at, agent, ip)

    def _unreserved(self, L: Ledger, username: str, supplied: str) -> Result:
        blocked = _blocked(L, username, self.clock())
        if blocked and blocked.status == 401:
            try:
                self.hasher.verify(supplied, None)  # Locked names take the same dummy KDF path.
            except passwords.PasswordBusy:
                return Result(503, dict(ok=False, message=BUSY), retry_after=5)
        return blocked or _refusal(429, 1)

    def _password_ticket(
        self,
        L: Ledger,
        username: str,
        previous: dict | None,
        proof: dict,
        principal: dict | None,
        at: datetime,
        agent: str,
        ip: str,
    ) -> Result:
        blocked = _blocked(L, username, at)
        if blocked:
            return blocked
        current = _user(L, username)
        if principal is not None and _principal(L, principal, at) is None:
            return _refusal()
        # A forged or spent recovery ticket fails like a wrong password, so it cannot test passwords.
        recovery = tickets.attempt(L, proof["recovery"], "recovery", at) if proof["recovery"] else None
        replacement = proof["replacement"]
        if (
            not proof["valid"]
            or not current
            or current["disabled_at"]
            or not previous
            or any(current[k] != previous[k] for k in ("id", "password_hash", "totp_version"))
            or (proof["recovery"] and (not recovery or recovery["user_id"] != current["id"]))
        ):
            reason = "unknown_user" if not current else "disabled_user" if current["disabled_at"] else "password"
            return _failure(L, username, at, reason, ip)
        if replacement:
            L.rehash_password(current, replacement)
        if recovery:
            if not tickets.consume(L, recovery, at):
                raise PermissionError("ticket_changed")
            return complete(L, current, at, recovery=True, agent=agent, ip=ip)
        ticket = tickets.issue(
            L,
            current["id"],
            "stepup" if principal else "signin",
            at,
            session_hash=principal["id_hash"] if principal else None,
            challenge=json.dumps(
                dict(password_hash=replacement or current["password_hash"], totp_version=current["totp_version"])
            ).encode(),
        )
        return Result(200, dict(ok=True, ticket=ticket))

    def factor(
        self,
        L: Ledger,
        ident: str,
        supplied: str,
        *,
        principal: dict | None = None,
        presented: dict | None = None,
        agent: str = "",
        ip: str = "",
    ) -> Result:
        with L.tx():
            at = self.clock()
            if principal is not None and _principal(L, principal, at) is None:
                return _refusal()
            ticket = tickets.attempt(
                L,
                ident,
                "stepup" if principal else "signin",
                at,
                session_hash=principal["id_hash"] if principal else None,
            )
            if not ticket or (principal and ticket["user_id"] != principal["user_id"]):
                return _refusal()
            row = L.db.execute("SELECT * FROM users WHERE id=?", (ticket["user_id"],)).fetchone()
            user = dict(row)
            blocked = _blocked(L, user["username"], at)
            if blocked:
                return blocked
            proof = json.loads(ticket["challenge"])
            unchanged = not user["disabled_at"] and all(user[k] == proof[k] for k in ("password_hash", "totp_version"))
            if not unchanged:
                return _failure(L, user["username"], at, "totp", ip)
            if not self._second_factor(L, user, supplied, at):
                limits.failure(L, user["username"], at, reason="totp", ip=ip)
                return Result(401, dict(ok=False, message=WRONG_CODE if ticket["attempts"] < 5 else FAILED))
            if not tickets.consume(L, ticket, at):
                raise PermissionError("ticket_changed")
            return complete(L, user, at, previous=principal or presented, agent=agent, ip=ip)

    def recover(
        self, L: Ledger, username: str, code: str, *, presented: dict | None = None, agent: str = "", ip: str = ""
    ) -> Result:
        """The one way back in. Every refusal is the same, so nothing tells whether the account
        exists or which factor it uses before its recovery code is spent."""
        with L.tx():
            at = self.clock()
            if presented is not None:
                return _refusal()
            username = users.canonical_username(username)
            state = limits.state(L, username, at, include_busy=False)
            if state["locked"]:
                return _refusal()
            if state["retry_after"]:
                return _refusal(429, state["retry_after"])
            user = _user(L, username)
            password = bool(user and user["password_hash"] and user["totp_version"])
            usable = user and not user["disabled_at"]
            factor = usable and (
                password or L.db.execute("SELECT 1 FROM passkeys WHERE user_id=?", (user["id"],)).fetchone()
            )
            if not factor or not recovery_codes.consume(L, user["id"], code, at):
                return _failure(L, username, at, "recovery", ip)
            if password:  # A code never replaces a password account's factor alone; its password comes next.
                return Result(200, dict(ok=True, ticket=tickets.issue(L, user["id"], "recovery", at)))
            return complete(L, user, at, recovery=True, method="passkey", agent=agent, ip=ip)

    @staticmethod
    def _second_factor(L: Ledger, user: dict, supplied: str, at: datetime) -> bool:
        try:
            step = totp.match(totp_store.seed(user["id"], user["totp_version"]), supplied, int(at.timestamp()))
        except store.SecretError:
            return False
        return step is not None and L.consume_totp(user["id"], user["totp_version"], step)


def complete(
    L: Ledger,
    user: dict,
    at: datetime,
    *,
    recovery: bool = False,
    method: str = "password_totp",
    previous: dict | None = None,
    agent: str = "",
    ip: str = "",
) -> Result:
    """Caller proves the factor and consumes its ticket in the same outer transaction."""
    with L.tx():
        stamp = sessions.stamp(at)
        if recovery:
            L.close_user_sessions(user["id"], stamp)
        if previous:
            L.revoke_session(previous["id_hash"], previous["user_id"], stamp)
        ident = sessions.issue(L, user["id"], at, recovery=recovery, method=method, agent=agent, ip=ip)
        limits.success(L, user["id"], user["username"])
        L.record_signin(user["id"], sessions.method_name(method, recovery=recovery), stamp, ip)
        return Result(200, dict(ok=True, recovery=recovery), session_id=ident)
