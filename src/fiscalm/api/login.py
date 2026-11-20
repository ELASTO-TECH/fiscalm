"""Browser account endpoints; transport admission happens before these flows run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fiscalm.api import (
    auth,
    enrolment,
    limits,
    passkeys,
    passwords,
    recovery_codes,
    sessions,
    setup_links,
    signin,
    webauthn,
)
from fiscalm.ledger import Ledger


@dataclass(frozen=True)
class Endpoint:
    path: str
    fields: str
    handler: Callable
    session: str = "normal"
    stepup: bool = False
    passkey: bool = False


class BrowserAuth:
    def __init__(self, clock: Callable[[], datetime], origin: str) -> None:
        self.clock = clock
        hasher = passwords.Passwords()
        self.signin = signin.Signin(clock, hasher)
        self.enrolment = enrolment.Enrolment(clock, hasher)
        try:
            self.passkeys = passkeys.Passkeys(clock, origin)
        except ValueError:
            self.passkeys = None  # WebAuthn RP IDs must be DNS names; HTTPS IP installs use password/TOTP.
        self.attempts = limits.IPAttempts()
        self.routes = {endpoint.path: endpoint for endpoint in ENDPOINTS}

    def post(self, L: Ledger, path: str, body: object, principal: dict | None, *, agent: str, ip: str) -> signin.Result:
        route = self.routes.get(path)
        if not route:
            return signin.Result(404, dict(ok=False))
        if route.session != "public" and (
            principal is None
            or (route.session == "normal" and principal["recovery_only"])
            or (route.session == "recovery" and not principal["recovery_only"])
        ):
            return self._refusal()
        if (
            not isinstance(body, dict)
            or set(body) != set(route.fields.split())
            or not all(isinstance(v, str) for v in body.values())
        ):
            return signin.Result(400, dict(ok=False))
        if route.passkey and self.passkeys is None:
            return signin.Result(
                400, dict(ok=False, message="Pentru cheile de acces, folosește o adresă HTTPS cu nume de domeniu.")
            )
        if path != "/auth/signout":
            retry = self.attempts.attempt(ip, int(self.clock().timestamp()))
            if retry:
                return signin.Result(429, dict(ok=False, message=signin.FAILED), retry_after=retry)
        options = {"stepup": True} if route.stepup else {}
        try:
            return route.handler(self, L, body, principal, agent, ip, **options)
        except PermissionError as exc:
            return (
                signin.Result(401, dict(ok=False, reauth=True, message=auth.REAUTH_MESSAGE))
                if str(exc) == "reauth_required"
                else self._refusal()
            )

    @staticmethod
    def _refusal() -> signin.Result:
        return signin.Result(401, dict(ok=False, message=signin.FAILED))

    def _password(
        self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str, *, stepup: bool = False
    ) -> signin.Result:
        if stepup and principal is None:
            return self._refusal()
        return self.signin.password(
            L, body.get("username", ""), body["password"], principal=principal if stepup else None, ip=ip
        )

    def _factor(
        self,
        L: Ledger,
        body: dict,
        principal: dict | None,
        agent: str,
        ip: str,
        *,
        stepup: bool = False,
    ) -> signin.Result:
        if stepup and principal is None:
            return self._refusal()
        return self.signin.factor(
            L,
            body["ticket"],
            body["code"],
            principal=principal if stepup else None,
            presented=principal if not stepup else None,
            agent=agent,
            ip=ip,
        )

    def _recover_code(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        return self.signin.recover(L, body["username"], body["code"], presented=principal, agent=agent, ip=ip)

    def _recover_password(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        if principal is not None or not body["ticket"]:
            return self._refusal()
        return self.signin.password(L, body["username"], body["password"], recovery=body["ticket"], agent=agent, ip=ip)

    def _inspect(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        link = setup_links.inspect(L, body["link"], self.clock())
        return (
            signin.Result(200, dict(ok=True, user={k: link[k] for k in ("username", "display_name")}))
            if link
            else self._refusal()
        )

    def _begin_setup(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        return self.enrolment.begin_setup(L, body["link"], body["password"])

    def _finish_setup(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        return self.enrolment.finish_setup(
            L, body["link"], body["ticket"], body["code"], presented=principal, agent=agent, ip=ip
        )

    def _begin_recovery(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        return self.enrolment.begin_recovery(L, principal) if principal else self._refusal()

    def _finish_recovery(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        if not principal:
            return self._refusal()
        return self.enrolment.finish_recovery(L, principal, body["ticket"], body["code"], agent=agent, ip=ip)

    def _password_change(self, L: Ledger, body: dict, principal: dict, agent: str, ip: str) -> signin.Result:
        return self.enrolment.change_password(L, principal, body["password"])

    def _totp_begin(self, L: Ledger, body: dict, principal: dict, agent: str, ip: str) -> signin.Result:
        return self.enrolment.begin_totp(L, principal)

    def _totp_finish(self, L: Ledger, body: dict, principal: dict, agent: str, ip: str) -> signin.Result:
        return self.enrolment.finish_totp(L, principal, body["ticket"], body["code"])

    def _owner(self, L: Ledger, principal: dict, at: datetime) -> str:
        auth.authorize_write(L, principal, at, "read", recent=True)
        return principal["user_id"]

    def _codes(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        with L.tx():
            at = self.clock()
            owner = self._owner(L, principal, at)
            codes = recovery_codes.generate(L, owner, at)
        return signin.Result(200, dict(ok=True, codes=codes))

    def _session_close(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        with L.tx():
            at = self.clock()
            owner = self._owner(L, principal, at)
            if not L.close_owned_session(body["id"], owner, sessions.stamp(at)):
                return signin.Result(404, dict(ok=False))
        return signin.Result(200, dict(ok=True), clear_session=body["id"] == principal["id_hash"])

    def _sessions_others(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        with L.tx():
            at = self.clock()
            owner = self._owner(L, principal, at)
            closed = L.close_other_sessions(owner, principal["id_hash"], sessions.stamp(at))
        return signin.Result(200, dict(ok=True, closed=closed))

    def _signout(self, L: Ledger, body: dict, principal: dict | None, agent: str, ip: str) -> signin.Result:
        if not principal:
            return signin.Result(401, dict(ok=False), clear_session=True)
        L.revoke_session(principal["id_hash"], principal["user_id"], sessions.stamp(self.clock()), audit=True)
        return signin.Result(200, dict(ok=True), clear_session=True)

    def _key_options(self, L, body, principal, agent, ip, *, stepup=False) -> signin.Result:
        return self.passkeys.begin_signin(L, principal=principal if stepup else None)

    def _key_signin(self, L, body, principal, agent, ip, *, stepup=False) -> signin.Result:
        return self.passkeys.finish_signin(
            L,
            body["ticket"],
            body,
            principal=principal if stepup else None,
            presented=principal if not stepup else None,
            agent=agent,
            ip=ip,
        )

    def _key_setup_begin(self, L, body, principal, agent, ip) -> signin.Result:
        return self.passkeys.begin_setup(L, body["link"], body["name"])

    def _key_setup_finish(self, L, body, principal, agent, ip) -> signin.Result:
        return self.passkeys.finish_setup(
            L, body["link"], body["ticket"], body, presented=principal, agent=agent, ip=ip
        )

    def _key_add_begin(self, L, body, principal, agent, ip) -> signin.Result:
        return self.passkeys.begin_add(L, principal, body["name"])

    def _key_add_finish(self, L, body, principal, agent, ip) -> signin.Result:
        return self.passkeys.finish_add(L, principal, body["ticket"], body)

    def _key_recovery_begin(self, L, body, principal, agent, ip) -> signin.Result:
        return self.passkeys.begin_recovery(L, principal, body["name"])

    def _key_recovery_finish(self, L, body, principal, agent, ip) -> signin.Result:
        return self.passkeys.finish_recovery(L, principal, body["ticket"], body)

    @staticmethod
    def _key_id(body: dict) -> bytes | None:
        try:
            return webauthn.unb64(body["credential_id"], 1023)
        except ValueError:
            return None

    def _key_rename(self, L, body, principal, agent, ip) -> signin.Result:
        with L.tx():
            at = self.clock()
            owner = self._owner(L, principal, at)
            ident = self._key_id(body)
            if ident is None:
                return signin.Result(400, dict(ok=False))
            try:
                name = passkeys.label(body["name"])
            except ValueError as exc:
                return signin.Result(400, dict(ok=False, message=str(exc)))
            if not L.db.execute(
                "SELECT 1 FROM passkeys WHERE credential_id=? AND user_id=?", (ident, owner)
            ).fetchone():
                return signin.Result(404, dict(ok=False))
            L.rename_passkey(ident, owner, name, sessions.stamp(at))
            return signin.Result(200, dict(ok=True))

    def _key_remove(self, L, body, principal, agent, ip) -> signin.Result:
        with L.tx():
            at = self.clock()
            owner = self._owner(L, principal, at)
            ident = self._key_id(body)
            if ident is None:
                return signin.Result(400, dict(ok=False))
            try:
                removed = L.remove_passkey(ident, owner, sessions.stamp(at))
            except ValueError as exc:
                return signin.Result(400, dict(ok=False, message=str(exc)))
            return signin.Result(200 if removed else 404, dict(ok=removed))


ENDPOINTS = (
    Endpoint("/auth/passkey/options", "", BrowserAuth._key_options, session="public", passkey=True),
    Endpoint(
        "/auth/passkey/finish",
        "ticket credential_id user_handle client_data authenticator_data signature",
        BrowserAuth._key_signin,
        session="public",
        passkey=True,
    ),
    Endpoint("/auth/step-up/passkey/options", "", BrowserAuth._key_options, stepup=True, passkey=True),
    Endpoint(
        "/auth/step-up/passkey/finish",
        "ticket credential_id user_handle client_data authenticator_data signature",
        BrowserAuth._key_signin,
        stepup=True,
        passkey=True,
    ),
    Endpoint("/auth/setup/passkey/options", "link name", BrowserAuth._key_setup_begin, session="public", passkey=True),
    Endpoint(
        "/auth/setup/passkey/finish",
        "link ticket credential_id client_data attestation transports",
        BrowserAuth._key_setup_finish,
        session="public",
        passkey=True,
    ),
    Endpoint("/auth/me/passkey/options", "name", BrowserAuth._key_add_begin, passkey=True),
    Endpoint(
        "/auth/me/passkey/finish",
        "ticket credential_id client_data attestation transports",
        BrowserAuth._key_add_finish,
        passkey=True,
    ),
    Endpoint("/auth/me/passkey/rename", "credential_id name", BrowserAuth._key_rename, passkey=True),
    Endpoint("/auth/me/passkey/remove", "credential_id", BrowserAuth._key_remove, passkey=True),
    Endpoint(
        "/auth/recover/passkey/options", "name", BrowserAuth._key_recovery_begin, session="recovery", passkey=True
    ),
    Endpoint(
        "/auth/recover/passkey/finish",
        "ticket credential_id client_data attestation transports",
        BrowserAuth._key_recovery_finish,
        session="recovery",
        passkey=True,
    ),
    Endpoint("/auth/password", "username password", BrowserAuth._password, session="public"),
    Endpoint("/auth/totp", "ticket code", BrowserAuth._factor, session="public"),
    Endpoint("/auth/step-up/password", "password", BrowserAuth._password, session="normal", stepup=True),
    Endpoint("/auth/step-up/totp", "ticket code", BrowserAuth._factor, session="normal", stepup=True),
    Endpoint("/auth/setup/inspect", "link", BrowserAuth._inspect, session="public"),
    Endpoint("/auth/setup/begin", "link password", BrowserAuth._begin_setup, session="public"),
    Endpoint("/auth/setup/finish", "link ticket code", BrowserAuth._finish_setup, session="public"),
    Endpoint("/auth/recover/code", "username code", BrowserAuth._recover_code, session="public"),
    Endpoint("/auth/recover/password", "username ticket password", BrowserAuth._recover_password, session="public"),
    Endpoint("/auth/recover/begin", "", BrowserAuth._begin_recovery, session="recovery"),
    Endpoint("/auth/recover/finish", "ticket code", BrowserAuth._finish_recovery, session="recovery"),
    Endpoint("/auth/me/password-change", "password", BrowserAuth._password_change),
    Endpoint("/auth/me/totp/begin", "", BrowserAuth._totp_begin),
    Endpoint("/auth/me/totp/finish", "ticket code", BrowserAuth._totp_finish),
    Endpoint("/auth/me/recovery-codes", "", BrowserAuth._codes),
    Endpoint("/auth/me/session-close", "id", BrowserAuth._session_close),
    Endpoint("/auth/me/sessions-close-others", "", BrowserAuth._sessions_others),
    Endpoint("/auth/signout", "", BrowserAuth._signout, session="any"),
)
