"""Microsoft public-client device authorization. Secrets never cross the action boundary."""

from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fiscalm import integrations as ig, secrets
from fiscalm.ledger import Ledger, finance_home

AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0/"
GRAPH = "https://graph.microsoft.com/v1.0/"
SCOPES = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read offline_access"


class MicrosoftError(RuntimeError):
    """Only a fixed public code, never a remote response."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url: str, *, form: dict | None = None, token: str = "", limit: int = 1_000_000) -> bytes:
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = urlencode(form).encode() if form is not None else None
    try:
        with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers), timeout=20) as response:
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise MicrosoftError("response_too_large")
            return raw
    except HTTPError as exc:
        if exc.code in (400, 404) and url.startswith(GRAPH) and url.endswith("/$value"):
            raise MicrosoftError("message_unavailable") from None
        if exc.code == 410 and url.startswith(GRAPH):
            raise MicrosoftError("delta_reset") from None
        if exc.code in (401, 403):
            raise MicrosoftError("reconnect_required") from None
        try:
            body = json.loads(exc.read(100_000))
            code = body.get("error")
            code = code if isinstance(code, str) else ""
            numbers = body.get("error_codes", [])
        except (ValueError, OSError, AttributeError):
            code, numbers = "", []
        numbers = numbers if isinstance(numbers, list) else []
        public = (
            code
            if code in {"authorization_pending", "slow_down", "authorization_declined", "expired_token"}
            else "connection_failed"
        )
        if 65001 in numbers or 90094 in numbers:
            public = "admin_consent_required"
        elif 53003 in numbers or 530036 in numbers:
            public = "tenant_policy_blocked"
        elif code == "invalid_grant":
            public = "reconnect_required"
        raise MicrosoftError(public) from None
    except (OSError, ValueError):
        raise MicrosoftError("connection_failed") from None


def client_id() -> str:
    value = os.environ.get("FISCALM_MICROSOFT_CLIENT_ID")
    if value is None:
        try:
            value = json.loads((finance_home() / "config" / "microsoft.json").read_text()).get("client_id", "")
        except FileNotFoundError:
            value = ""
    if not value:
        raise MicrosoftError("not_configured")
    return value


def load(account: ig.Instance) -> dict:
    try:
        return json.loads(account.secret(ig.OAUTH))
    except (OSError, ValueError, secrets.SecretError):
        raise MicrosoftError("reconnect_required") from None


def save(L: Ledger, account: ig.Instance, data: dict) -> None:
    if not ig.put_oauth(
        L,
        account.id,
        json.dumps(data),
        client_id=account.config["client_id"],
        expect=account.versions.get(ig.OAUTH),
        guard=lambda db: (
            json.loads(db.execute("SELECT config FROM integrations WHERE id=?", (account.id,)).fetchone()[0]).get(
                "from_address"
            )
            == account.config["from_address"]
        ),
    ):
        raise MicrosoftError("connection_changed")


def tokens(response: dict, previous: dict) -> dict:
    if not response.get("access_token") or not (response.get("refresh_token") or previous.get("refresh_token")):
        raise MicrosoftError("reconnect_required")
    scopes = {s.rsplit("/", 1)[-1] for s in response.get("scope", "").split()}
    if not {"Mail.Read", "User.Read"} <= scopes or scopes - {
        "Mail.Read",
        "User.Read",
        "offline_access",
        "openid",
        "profile",
        "email",
    }:
        raise MicrosoftError("invalid_scopes")
    return {k: response.get(k) or previous.get(k) for k in ("access_token", "refresh_token")} | {
        "expires_at": time.time() + int(response["expires_in"])
    }


_UNSET = object()


def start(L: Ledger, address: str, iid: str, owner: str, *, firm_scope: object = _UNSET) -> dict:
    cid = client_id()
    if not address or "@" not in address:
        raise MicrosoftError("invalid_settings")
    if not iid:
        existing = next(
            (
                a
                for a in ig.enabled(L, "mail")
                if a.config.get("method") == "microsoft" and a.config["from_address"].lower() == address.lower()
            ),
            None,
        )
        iid = existing.id if existing else ""
    account = ig.instance(L, iid) if iid else None
    if iid and (
        not account or account.kind != "mail" or not account.enabled or account.config.get("method") != "microsoft"
    ):
        raise MicrosoftError("invalid_settings")
    if account and account.config["from_address"].lower() != address.lower():
        raise MicrosoftError("invalid_settings")
    if account and firm_scope is not _UNSET:
        raise MicrosoftError("invalid_settings")
    scope = ig.validate_firm_scope(L, "all" if firm_scope is _UNSET else firm_scope)
    response = json.loads(request(AUTH + "devicecode", form={"client_id": cid, "scope": SCOPES}))
    if not account:
        item = ig.add(
            L, "mail", address, {"from_address": address, "method": "microsoft", "client_id": cid}, firm_scope=scope
        )
        account = ig.instance(L, item["id"])
    elif account.config["client_id"] != cid:
        ig.update(L, iid, {"client_id": cid})
        account = ig.instance(L, iid)
    interval = max(5, int(response.get("interval", 5)))
    expires = min(1800, int(response["expires_in"]))
    save(
        L,
        account,
        {
            "device_code": response["device_code"],
            "owner": owner,
            "expires_at": time.time() + expires,
            "next_poll": time.time() + interval,
            "interval": interval,
        },
    )
    return {
        "code": "authorization_pending",
        "id": account.id,
        "user_code": response["user_code"],
        "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": expires,
        "interval": interval,
    }


def poll(L: Ledger, iid: str, owner: str) -> dict:
    account = ig.instance(L, iid)
    if not account or account.kind != "mail" or not account.enabled or account.config.get("method") != "microsoft":
        raise MicrosoftError("invalid_settings")
    data = load(account)
    if data.get("owner") != owner or not data.get("device_code"):
        raise MicrosoftError("connection_changed")
    if data["expires_at"] <= time.time():
        raise MicrosoftError("expired_token")
    if data["next_poll"] > time.time():
        return {"code": "authorization_pending", "interval": data["interval"]}
    # Claim this polling slot before I/O. Another connection cannot redeem the same slot.
    claimed = data | {"next_poll": time.time() + data["interval"]}
    save(L, account, claimed)
    account = ig.instance(L, iid)
    if load(account) != claimed:
        raise MicrosoftError("connection_changed")
    try:
        response = json.loads(
            request(
                AUTH + "token",
                form={
                    "client_id": account.config["client_id"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": data["device_code"],
                },
            )
        )
    except MicrosoftError as exc:
        if str(exc) == "slow_down":
            data["interval"] += 5
            save(L, account, data | {"next_poll": time.time() + data["interval"]})
        if str(exc) in ("slow_down", "authorization_pending"):
            return {"code": str(exc), "interval": data["interval"]}
        raise
    result = tokens(response, {})
    profile = json.loads(request(GRAPH + "me?$select=mail,userPrincipalName", token=result["access_token"]))
    if account.config["from_address"].lower() not in {
        str(profile.get(k) or "").lower() for k in ("mail", "userPrincipalName")
    }:
        raise MicrosoftError("account_mismatch")
    save(L, account, result)
    return {"code": "connected", "id": iid}


def access_token(L: Ledger, account: ig.Instance) -> str:
    data = load(account)
    if not data.get("refresh_token") or account.config["client_id"] != client_id():
        raise MicrosoftError("reconnect_required")
    if data["expires_at"] <= time.time() + 60:
        response = json.loads(
            request(
                AUTH + "token",
                form={
                    "client_id": account.config["client_id"],
                    "grant_type": "refresh_token",
                    "refresh_token": data["refresh_token"],
                    "scope": SCOPES,
                },
            )
        )
        data = tokens(response, data)
        save(L, account, data)
    return data["access_token"]
