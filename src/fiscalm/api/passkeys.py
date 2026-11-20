"""Atomic passkey ceremonies, bound to account setup links or a recently proved session."""

from __future__ import annotations

import json
import secrets
import unicodedata
from collections.abc import Callable
from datetime import datetime

from fiscalm import PRODUCT
from fiscalm.api import auth, enrolment, limits, recovery_codes, sessions, setup_links, signin, tickets, webauthn
from fiscalm.ledger import Ledger


def _refusal() -> signin.Result:
    return signin.Result(401, dict(ok=False, message=signin.FAILED))


def label(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(value) <= 80 or not value.isprintable():
        raise ValueError("Numele cheii trebuie să aibă între 1 și 80 de caractere.")
    return value


def _transports(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) > 1024:
        raise ValueError("Invalid transport hints")
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) > 8 or not all(isinstance(v, str) for v in values):
        raise ValueError("Invalid transport hints")
    # Transport names are optional browser hints, never identity or signature proof.
    known = {"usb", "nfc", "ble", "internal", "hybrid", "smart-card"}
    return json.dumps(sorted(set(values) & known))


class Passkeys:
    def __init__(self, clock: Callable[[], datetime], origin: str) -> None:
        self.clock, self.verifier = clock, webauthn.Verifier(origin)

    def _options(
        self,
        L: Ledger,
        owner: str,
        name: str,
        at: datetime,
        purpose: str,
        *,
        session_hash: str | None = None,
        setup_id: str | None = None,
    ) -> signin.Result:
        try:
            name = label(name)
        except ValueError as exc:
            return signin.Result(400, dict(ok=False, message=str(exc)))
        user = L.db.execute("SELECT * FROM users WHERE id=?", (owner,)).fetchone()
        nonce = secrets.token_bytes(32)
        proof = json.dumps(dict(nonce=webauthn.b64(nonce), name=name, setup_id=setup_id)).encode()
        token = tickets.issue(L, owner, purpose, at, session_hash=session_hash, challenge=proof)
        options = dict(
            challenge=webauthn.b64(nonce),
            rp=dict(id=self.verifier.rp_id, name=PRODUCT),
            user=dict(
                id=webauthn.b64(user["webauthn_handle"]), name=user["username"], displayName=user["display_name"]
            ),
            pubKeyCredParams=[dict(type="public-key", alg=alg) for alg in (-7, -8, -257)],
            timeout=300_000,
            attestation="none",
            authenticatorSelection=dict(residentKey="required", userVerification="required"),
            excludeCredentials=[
                dict(type="public-key", id=webauthn.b64(r[0]))
                for r in L.db.execute("SELECT credential_id FROM passkeys WHERE user_id=?", (owner,))
            ],
        )
        return signin.Result(200, dict(ok=True, ticket=token, publicKey=options))

    def _register(
        self,
        L: Ledger,
        owner: str,
        token: str,
        purpose: str,
        at: datetime,
        body: dict[str, str],
        *,
        session_hash: str | None = None,
        setup_id: str | None = None,
    ) -> bool:
        ticket = tickets.attempt(L, token, purpose, at, session_hash=session_hash)
        if not ticket or ticket["user_id"] != owner:
            return False
        try:
            proof = json.loads(ticket["challenge"])
            if proof["setup_id"] != setup_id:
                return False
            credential = self.verifier.registration(
                webauthn.unb64(body["attestation"], 16384),
                webauthn.unb64(body["client_data"], 4096),
                webauthn.unb64(proof["nonce"], 32),
                webauthn.unb64(body["credential_id"], 1023),
            )
            values = dict(
                credential_id=credential.credential_id,
                user_id=owner,
                public_key=credential.public_key.encoded,
                algorithm=credential.public_key.algorithm,
                sign_count=credential.counter,
                backup_eligible=int(credential.backup_eligible),
                backed_up=int(credential.backed_up),
                aaguid=credential.aaguid,
                name=label(proof["name"]),
                transports=_transports(body["transports"]),
                created_at=sessions.stamp(at),
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            return False
        if not L.add_passkey(values):
            return False
        if not tickets.consume(L, ticket, at):
            raise PermissionError("ticket_changed")
        return True

    def begin_setup(self, L: Ledger, fragment: str, name: str) -> signin.Result:
        with L.tx():
            at = self.clock()
            link = enrolment.setup_account(L, fragment, at)
            return (
                self._options(L, link["user_id"], name, at, "passkey_setup", setup_id=link["id"])
                if link
                else _refusal()
            )

    def finish_setup(
        self,
        L: Ledger,
        fragment: str,
        token: str,
        body: dict[str, str],
        *,
        presented: dict | None = None,
        agent: str = "",
        ip: str = "",
    ) -> signin.Result:
        with L.tx():
            at = self.clock()
            link = enrolment.setup_account(L, fragment, at)
            if not link or not self._register(
                L, link["user_id"], token, "passkey_setup", at, body, setup_id=link["id"]
            ):
                return _refusal()  # Commit wrong-proof attempts; leave the setup link unused.
            if not setup_links.consume(L, fragment, at):
                raise PermissionError("setup_link_changed")
            codes = recovery_codes.generate(L, link["user_id"], at)
            user = dict(L.db.execute("SELECT * FROM users WHERE id=?", (link["user_id"],)).fetchone())
            result = signin.complete(L, user, at, method="passkey", previous=presented, agent=agent, ip=ip)
            return signin.Result(200, dict(ok=True, codes=codes), session_id=result.session_id)

    def begin_add(self, L: Ledger, principal: dict, name: str) -> signin.Result:
        with L.tx():
            at = self.clock()
            auth.authorize_write(L, principal, at, "read", recent=True)
            return self._options(L, principal["user_id"], name, at, "passkey_add", session_hash=principal["id_hash"])

    def finish_add(self, L: Ledger, principal: dict, token: str, body: dict[str, str]) -> signin.Result:
        with L.tx():
            at = self.clock()
            auth.authorize_write(L, principal, at, "read", recent=True)
            if not self._register(
                L, principal["user_id"], token, "passkey_add", at, body, session_hash=principal["id_hash"]
            ):
                return _refusal()
            L.close_other_sessions(principal["user_id"], principal["id_hash"], sessions.stamp(at))
            return signin.Result(200, dict(ok=True))

    def begin_signin(self, L: Ledger, *, principal: dict | None = None) -> signin.Result:
        with L.tx():
            at = self.clock()
            if principal is not None:
                auth.authorize_write(L, principal, at, "read")
                if not L.db.execute("SELECT 1 FROM passkeys WHERE user_id=?", (principal["user_id"],)).fetchone():
                    return _refusal()
            nonce = secrets.token_bytes(32)
            token = tickets.issue(
                L,
                principal["user_id"] if principal else None,
                "passkey_stepup" if principal else "passkey_signin",
                at,
                session_hash=principal["id_hash"] if principal else None,
                challenge=nonce,
            )
            # Discovery always returns a userHandle; the step-up ticket fixes the permitted account.
            options = dict(
                challenge=webauthn.b64(nonce), rpId=self.verifier.rp_id, userVerification="required", timeout=300_000
            )
            return signin.Result(200, dict(ok=True, ticket=token, publicKey=options))

    @staticmethod
    def _signin_ticket(L: Ledger, token: str, principal: dict | None, at: datetime) -> dict | None:
        if principal is not None:
            auth.authorize_write(L, principal, at, "read")
        ticket = tickets.attempt(
            L,
            token,
            "passkey_stepup" if principal else "passkey_signin",
            at,
            session_hash=principal["id_hash"] if principal else None,
        )
        return ticket if ticket and (principal is None or ticket["user_id"] == principal["user_id"]) else None

    @staticmethod
    def _failed(L: Ledger, username: str, at: datetime, ip: str) -> signin.Result:
        if username:
            limits.failure(L, username, at, reason="passkey", ip=ip)
        return _refusal()

    def _assertion(self, ticket: dict, credential: dict, body: dict[str, str]) -> webauthn.Authenticator | None:
        try:
            if webauthn.unb64(body["user_handle"], 64) != credential["webauthn_handle"]:
                return None
            return self.verifier.assertion(
                webauthn.unb64(body["authenticator_data"], 16384),
                webauthn.unb64(body["client_data"], 4096),
                ticket["challenge"],
                webauthn.unb64(body["signature"], 1024),
                credential["public_key"],
            )
        except (ValueError, TypeError, KeyError):
            return None

    def finish_signin(
        self,
        L: Ledger,
        token: str,
        body: dict[str, str],
        *,
        principal: dict | None = None,
        presented: dict | None = None,
        agent: str = "",
        ip: str = "",
    ) -> signin.Result:
        with L.tx():
            at = self.clock()
            ticket = self._signin_ticket(L, token, principal, at)
            if not ticket:
                return _refusal()
            username = principal["username"] if principal else ""
            try:
                ident = webauthn.unb64(body["credential_id"], 1023)
            except (ValueError, TypeError, KeyError):
                return self._failed(L, username, at, ip)
            row = L.db.execute(
                """SELECT p.*,u.username,u.webauthn_handle,u.disabled_at FROM passkeys p
                JOIN users u ON u.id=p.user_id WHERE p.credential_id=?""",
                (ident,),
            ).fetchone()
            if not row:
                return self._failed(L, username, at, ip)
            credential = dict(row)
            username = username or credential["username"]
            state = limits.state(L, username, at, include_busy=False)
            if state["locked"]:
                return _refusal()
            if state["retry_after"]:
                return signin.Result(429, dict(ok=False, message=signin.FAILED), retry_after=state["retry_after"])
            if credential["disabled_at"] or (principal and credential["user_id"] != principal["user_id"]):
                return self._failed(L, username, at, ip)
            proof = self._assertion(ticket, credential, body)
            if proof is None or not L.use_passkey(
                credential, proof.counter, proof.backup_eligible, proof.backed_up, sessions.stamp(at)
            ):
                return self._failed(L, username, at, ip)
            if not tickets.consume(L, ticket, at):
                raise PermissionError("ticket_changed")
            user = dict(id=credential["user_id"], username=credential["username"])
            return signin.complete(L, user, at, method="passkey", previous=principal or presented, agent=agent, ip=ip)

    def begin_recovery(self, L: Ledger, principal: dict, name: str) -> signin.Result:
        with L.tx():
            at = self.clock()
            current = enrolment.recovery_session(L, principal, at, method="recovery_code")
            if not current:
                return _refusal()
            return self._options(L, current["user_id"], name, at, "passkey_recovery", session_hash=current["id_hash"])

    def finish_recovery(self, L: Ledger, principal: dict, token: str, body: dict[str, str]) -> signin.Result:
        with L.tx():
            at = self.clock()
            current = enrolment.recovery_session(L, principal, at, method="recovery_code")
            if not current or not self._register(
                L, current["user_id"], token, "passkey_recovery", at, body, session_hash=current["id_hash"]
            ):
                return _refusal()
            keep = webauthn.unb64(body["credential_id"], 1023)  # Already verified by _register.
            L.finish_recovery(current["user_id"], sessions.stamp(at), keep_passkey=keep)
            codes = recovery_codes.generate(L, current["user_id"], at)
            return signin.Result(200, dict(ok=True, codes=codes), clear_session=True)
