"""Versioned TOTP seeds: publish and activate under the ledger lock, sweep committed state."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import stat
from datetime import datetime
from urllib.parse import quote

from fiscalm import secrets as store
from fiscalm.api import passwords, sessions, tickets, totp
from fiscalm.ledger import Ledger

USER_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}", re.ASCII)
VERSION = re.compile(r"[0-9a-f]{32}", re.ASCII)
SEED_FILE = re.compile(rf"auth-totp-({USER_ID.pattern})-({VERSION.pattern})", re.ASCII)


def _name(user_id: str, version: str) -> str:
    if not USER_ID.fullmatch(user_id) or not VERSION.fullmatch(version):
        raise ValueError("Invalid TOTP reference")
    return f"auth-totp-{user_id}-{version}"


def seed(user_id: str, version: str) -> bytes:
    try:
        value = base64.b32decode(store.read_secret(_name(user_id, version)))
    except (ValueError, binascii.Error) as exc:
        raise store.SecretError("Invalid TOTP seed") from exc
    if len(value) != 20:
        raise store.SecretError("Invalid TOTP seed")
    return value


def begin(
    L: Ledger,
    user_id: str,
    password: str,
    hasher: passwords.Passwords,
    at: datetime,
    *,
    session_hash: str | None = None,
) -> dict:
    """The caller authorizes setup or recent authentication before publishing a seed."""
    return publish(L, user_id, hasher.hash(password), at, session_hash=session_hash)


def publish(
    L: Ledger,
    user_id: str,
    password_hash: str,
    at: datetime,
    *,
    session_hash: str | None = None,
    setup_id: str | None = None,
) -> dict:
    """Publish an already hashed password, allowing recovery to keep the proved password."""
    version = secrets.token_hex(16)
    name = _name(user_id, version)
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    with L.tx() as db:
        user = db.execute("SELECT * FROM users WHERE id=? AND disabled_at IS NULL", (user_id,)).fetchone()
        if not user:
            raise PermissionError("user_unavailable")
        if version == user["totp_version"] or (store.secrets_dir() / name).exists():
            raise ValueError("TOTP version collision")
        data = dict(
            version=version,
            expected_version=user["totp_version"],
            expected_password=user["password_hash"],
            password_hash=password_hash,
            setup_id=setup_id,
        )
        try:
            ticket = tickets.issue(
                L, user_id, "totp_enroll", at, session_hash=session_hash, challenge=json.dumps(data).encode()
            )
            store.write_secret(name, secret)
        except BaseException:
            (store.secrets_dir() / name).unlink(missing_ok=True)
            raise
    return dict(
        ticket=ticket,
        secret=secret,
        uri=f"otpauth://totp/fiscalm:{quote(user['username'], safe='')}?secret={secret}&issuer=fiscalm&algorithm=SHA1&digits=6&period=30",
    )


def finish(L: Ledger, ident: str, supplied: str, at: datetime, *, session_hash: str | None = None) -> bool:
    """Wrong codes return normally, committing the failed attempt. Never raise for a wrong code.

    A surrounding setup/recovery flow must also bind and consume its capability in
    this transaction. Sweep is deliberately separate: outer rollback must retain
    the previous seed, even after this function has returned successfully.
    """
    with L.tx():
        ticket = tickets.attempt(L, ident, "totp_enroll", at, session_hash=session_hash)
        if not ticket:
            return False
        data = json.loads(ticket["challenge"])
        try:
            step = totp.match(seed(ticket["user_id"], data["version"]), supplied, int(at.timestamp()))
        except store.SecretError:
            return False
        if step is None or not L.activate_totp(ticket, data, step, sessions.stamp(at)):
            return False
        if not tickets.consume(L, ticket, at):
            raise PermissionError("ticket_changed")
    return True


def _file_owner(name: str) -> str | None:
    if name.startswith("."):
        name, _, suffix = name[1:].rpartition(".")
        if not re.fullmatch(r"[a-z0-9_]{8}", suffix, re.ASCII):
            return None
    match = SEED_FILE.fullmatch(name)
    return match[1] if match else None


def sweep(L: Ledger, at: datetime) -> None:
    """A sweep cannot run inside activation's uncommitted outer transaction."""
    if L.db.in_transaction:
        raise ValueError("TOTP cleanup requires committed state")
    with L.tx() as db:
        root = store.secrets_dir()
        if not root.exists():
            return
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Authentication secrets require a private directory of this user")
        owners = {r[0] for r in db.execute("SELECT id FROM users")}
        keep = {
            _name(r[0], r[1]) for r in db.execute("SELECT id,totp_version FROM users WHERE totp_version IS NOT NULL")
        }
        for row in db.execute(
            "SELECT user_id,challenge FROM auth_tickets WHERE purpose='totp_enroll' AND used_at IS NULL AND expires_at>?",
            (sessions.stamp(at),),
        ):
            data = json.loads(row["challenge"])
            keep.add(_name(row["user_id"], data["version"]))
        for path in root.iterdir():
            if _file_owner(path.name) in owners and path.name not in keep and (path.is_symlink() or not path.is_dir()):
                path.unlink(missing_ok=True)
