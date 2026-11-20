"""Company-bound receipt upload credentials; only creation returns the path secret."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from fiscalm import companies
from fiscalm.ledger import Ledger

TOKEN = re.compile(r"([0-9a-f]{16})\.([0-9a-f]{64})", re.ASCII)
PUBLIC = "id, company, label, created_at, expires_at, revoked_at, uploads, last_used_at, max_files"


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create(L: Ledger, company: str, label: str, expires_in_days: int = 30, max_files: int | None = None) -> dict:
    if max_files is None:
        max_files = 100
    if (
        (company != "ALL" and company not in companies.all(L))
        or not isinstance(label, str)
        or not label.strip()
        or len(label) > 100
        or type(expires_in_days) is not int
        or not 1 <= expires_in_days <= 365
        or (max_files is not None and (type(max_files) is not int or not 1 <= max_files <= 2**63 - 1))
    ):
        raise ValueError("Use a company, a label of 1-100 characters, 1-365 days and a positive file limit.")
    ident, secret = secrets.token_hex(8), secrets.token_hex(32)
    token = f"{ident}.{secret}"
    at = timestamp()
    expires = (datetime.fromisoformat(at) + timedelta(days=expires_in_days)).isoformat(timespec="seconds")
    with L.tx() as db:
        db.execute(
            """INSERT INTO upload_links
                   (id,company,label,secret_hash,created_at,expires_at,max_files) VALUES(?,?,?,?,?,?,?)""",
            (ident, company, label.strip(), hashlib.sha256(token.encode()).hexdigest(), at, expires, max_files),
        )
    return {"id": ident, "path": f"/u/{token}"}


def list_links(L: Ledger, company: str) -> list[dict]:
    return [
        dict(r)
        for r in L.db.execute(
            f"SELECT {PUBLIC} FROM upload_links WHERE company IN (?,'ALL') ORDER BY created_at,id", (company,)
        )
    ]


def revoke(L: Ledger, ident: str, company: str) -> bool:
    with L.tx() as db:
        return (
            db.execute(
                """UPDATE upload_links SET revoked_at=?
                          WHERE id=? AND company=? AND revoked_at IS NULL""",
                (timestamp(), ident, company),
            ).rowcount
            == 1
        )


def verify(L: Ledger, token: str, company: str | None = None) -> dict | None:
    match = TOKEN.fullmatch(token)
    row = L.db.execute("SELECT * FROM upload_links WHERE id=?", (match[1] if match else "",)).fetchone()
    equal = hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row["secret_hash"] if row else "0" * 64)
    if (
        not equal
        or not row
        or (row["company"] != "ALL" and row["company"] not in companies.all(L))
        or (company is not None and row["company"] != company)
        or row["revoked_at"]
        or row["expires_at"] <= timestamp()
        or (row["max_files"] is not None and row["uploads"] >= row["max_files"])
    ):
        return None
    return {k: row[k] for k in PUBLIC.split(", ")}


def action(L: Ledger, company: str, name: str, args: dict) -> dict:
    if name == "upload-link-create":
        allowed = {"co", "label", "expires_in_days", "max_files", "all_firms"}
        if type(args.get("all_firms", False)) is not bool:
            raise ValueError("all_firms must be boolean")
        company = "ALL" if args.get("all_firms") else company
        if set(args) - allowed:
            raise ValueError("Unexpected upload link field.")
        return create(L, company, args.get("label", ""), args.get("expires_in_days", 30), args.get("max_files"))
    return {"revoked": revoke(L, args.get("id", ""), company) or revoke(L, args.get("id", ""), "ALL")}
