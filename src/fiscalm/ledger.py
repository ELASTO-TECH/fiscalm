"""Small SQLite state so nothing is paid or sent twice.

Location: $FISCALM_HOME/state/ledger.db (default ${XDG_DATA_HOME:-~/.local/share}/fiscalm/state/ledger.db).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from fiscalm import companies

from fiscalm import audit, backup
from fiscalm.efactura import credits
from fiscalm.migrations import check_version, migrate
from fiscalm.paths import finance_home

LIVE_MARKER = "LIVE"  # <FISCALM_HOME>/state/LIVE, the only marker
_PASSWORD_TOTP = "(length(users.password_hash)>0 AND length(users.totp_version)>0)"
_WORKING_SIGNIN = "(" + _PASSWORD_TOTP + " OR EXISTS(SELECT 1 FROM passkeys WHERE passkeys.user_id=users.id))"
_LIVE_TICKET_OWNER = """(
    (auth_tickets.purpose='passkey_signin' AND auth_tickets.user_id IS NULL
     AND auth_tickets.session_hash IS NULL AND typeof(auth_tickets.challenge)='blob'
     AND length(auth_tickets.challenge)=32)
    OR (auth_tickets.purpose!='passkey_signin' AND EXISTS
        (SELECT 1 FROM users WHERE id=auth_tickets.user_id AND disabled_at IS NULL)))"""


DOCUMENT_CORRECTABLE = """sent_at IS NULL AND COALESCE(source,'') NOT LIKE 'upload:%'
            AND NOT EXISTS (SELECT 1 FROM settled_tx WHERE tx_key='bon:'||documents.sha
                OR what='receipt '||documents.sha)
            AND NOT EXISTS (SELECT 1 FROM tx_link_items WHERE item='receipt '||documents.sha)
            AND NOT EXISTS (SELECT 1 FROM efactura WHERE paid_tx='bon:'||documents.sha
                OR message_id=documents.efactura_id AND (paid_tx IS NOT NULL OR status='paid'))"""


def is_live() -> bool:
    """Outward effects (Drive archive, notifications, SPV uploads) only once the install is live:
    <FISCALM_HOME>/state/LIVE exists. Before that it runs in shadow: same ledger logic, no effects."""
    return (finance_home() / "state" / LIVE_MARKER).exists()


# A note that starts with USER is the user's own word (a tap, a CLI command), never proof.
USER = "user:"
# An e-Factura row the user marked paid ("Marchează ca plătită", `efactura mark-paid` without a mail): paid in
# their word, but not confirmed until a statement debit or a payment mail proves it.
USER_MARKED = f"status='paid' AND paid_tx IS NULL AND paid_note LIKE '{USER}%'"
# Proved paid by a payment mail, as _PROOFS reads it: `ef-proof:<mail key>` reserved for this invoice, and that mail
# read for it (judged); its payment-file row, if any, not paid by a debit already. Its bank debit, when the statement
# brings it, takes the mail's place as the proof; the mail stays reserved for this invoice, so it never proves another.
MAIL_PROVED = (
    "status='paid' AND substr(paid_tx,1,9)='ef-proof:' AND length(paid_tx)>9 AND NOT EXISTS (SELECT 1 FROM payments "
    "p WHERE p.item_id=efactura.item_id AND p.status='paid') AND EXISTS (SELECT 1 FROM settled_tx s WHERE "
    "s.tx_key=efactura.paid_tx AND s.what='efactura ' || efactura.message_id) AND EXISTS (SELECT 1 FROM meta m "
    "WHERE m.key='ef-judged:' || efactura.message_id || ':' || substr(efactura.paid_tx, 10))"
)


# The display name of the account recorded as having done something (`by_name`), never its id; "" for none or the CLI.
ACTOR_NAME = "COALESCE((SELECT display_name FROM users WHERE id={}), '') AS by_name"


def user_note(note: str | None) -> bool:
    """The note is the user's own word (a mark, not proof)."""
    return (note or "").startswith(USER)


def user_words(note: str | None) -> str:
    """What the user wrote, without the marker."""
    n = note or ""
    return n[len(USER) :].strip() if n.startswith(USER) else n.strip()


# An invoice the user's links paid in part (fiscalm.links): no proof but another explanation may settle it, or its
# rest would disappear from what is owed. Every settling writer of the item's table checks it in its WHERE.
def unlinked(kind: str) -> str:
    return f"NOT EXISTS (SELECT 1 FROM tx_link_items i WHERE i.item='{kind} ' || message_id)"


def covered(L: Ledger, company: str, kind: str = "") -> dict[str, Decimal]:
    """What the user's explanations settle of each of `company`'s items ('issued <id>', 'efactura <id>',
    'payment <id>'); with `kind`, of that kind's items only, by their id. Read through L.link_rows, which a state read
    caches."""
    out: dict[str, Decimal] = {}
    for it in L.link_rows(company)[1]:
        k, _, ident = it["item"].partition(" ")
        if not kind or k == kind:
            name = ident if kind else it["item"]
            out[name] = out.get(name, Decimal(0)) + Decimal(it["amount"])
    return out


# An e-Factura row paid at the till: its proof is the bon fiscal of the same purchase (paid_tx 'bon:<sha>').
BON_PAID = "status='paid' AND paid_tx LIKE 'bon:%'"
# Who put a payment in the payment file (payments.origin); a row of unknown origin is treated as automatic.
AUTHORS = {"person": "the user", "auto": "the autopay rule"}


def statement_tx(value: str | None) -> str | None:
    """`value` when it has the shape of a statement transaction's key (statements.tx_key: 20 lowercase hex), else None.
    The shape alone proves nothing paid: Ledger.paid_proofs checks the transaction is on record."""
    return value if value and re.fullmatch(r"[0-9a-f]{20}", value) else None


# What proves a received invoice or a payment-file payment paid. Evidence, not the shape of a key:
# - a statement debit: on record in statement_txs for the same company, and reserved for exactly this item in
#   settled_tx (an invoice settled before settled_tx existed: its own paid_tx is the reservation, so no other
#   invoice may carry the same key);
# - a bon fiscal (`bon:<sha>`): a receipt document of the same company linked to this invoice, reserved for it;
# - a payment mail (`ef-proof:<mail key>`): reserved for this invoice, and that mail was read for it (judged).
# A payment without a reservation of its own (settled before settled_tx) is proved by the statement debit on the
# invoice it paid, of the same company; an invoice in the payment file, by its payment's debit.
_PROOFS = """
WITH own AS (
  SELECT e.message_id, e.item_id, e.paid_tx AS k, EXISTS (SELECT 1 FROM statement_txs t WHERE t.tx_key=e.paid_tx
         AND t.company=e.company AND t.direction='debit') AS debit, s.tx_key IS NOT NULL AS reserved
  FROM efactura e LEFT JOIN settled_tx s ON s.tx_key=e.paid_tx
  WHERE e.company=:co AND e.paid_tx IS NOT NULL AND CASE WHEN s.tx_key IS NULL
    THEN NOT EXISTS (SELECT 1 FROM efactura o WHERE o.paid_tx=e.paid_tx AND o.message_id!=e.message_id)
    ELSE s.what='efactura ' || e.message_id OR s.what='link' AND EXISTS (SELECT 1 FROM tx_link_items i
         WHERE i.tx_key=s.tx_key AND i.item='efactura ' || e.message_id AND i.closed=1) END
), proved AS (
  SELECT * FROM own WHERE debit
     OR reserved AND substr(k,1,4)='bon:' AND EXISTS (SELECT 1 FROM documents d WHERE d.sha=substr(k,5) AND d.sha!=''
        AND d.kind='receipt' AND d.company=:co AND d.efactura_id=own.message_id)
     OR reserved AND substr(k,1,9)='ef-proof:' AND length(k)>9
        AND EXISTS (SELECT 1 FROM meta m WHERE m.key='ef-judged:' || own.message_id || ':' || substr(k,10))
), pay AS (
  SELECT p.item_id, s.tx_key AS k FROM payments p JOIN settled_tx s ON s.what='payment ' || p.item_id
  JOIN statement_txs t ON t.tx_key=s.tx_key AND t.company=p.company AND t.direction='debit' WHERE p.company=:co
  UNION ALL
  SELECT p.item_id, s.tx_key FROM payments p JOIN tx_link_items i ON i.item='payment ' || p.item_id AND i.closed=1
  JOIN settled_tx s ON s.tx_key=i.tx_key AND s.what='link'
  JOIN statement_txs t ON t.tx_key=s.tx_key AND t.company=p.company AND t.direction='debit' WHERE p.company=:co
  UNION ALL
  SELECT p.item_id, o.k FROM payments p JOIN proved o ON o.item_id=p.item_id AND o.debit
  WHERE p.company=:co AND NOT EXISTS (SELECT 1 FROM settled_tx s WHERE s.what='payment ' || p.item_id)
)
SELECT 'payment ' || item_id, k, 0 FROM pay
UNION ALL SELECT 'efactura ' || message_id, k, 0 FROM proved
UNION ALL SELECT 'efactura ' || e.message_id, pay.k, 1 FROM efactura e JOIN pay ON pay.item_id=e.item_id WHERE e.company=:co
ORDER BY 3
"""


BOUNDARY_DAYS = 3  # a payment can be dated up to this many days apart by a statement and by the bank


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LinkRefused(ValueError):
    """An explanation that can't be written as asked; `code` says why (fiscalm.links words it for the user)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AuthorizationRefused(Exception):
    """A write guard refused authority; never treat it as a file/parser OSError."""


class _SetChanged(Exception):
    """An invoice of a set changed since it was read: the whole collection rolls back."""


class Ledger:
    def __init__(self, path: Path | None = None, *, readonly: bool = False, timeout: float = 5.0):
        self._write_guard: Callable[[Ledger], None] | None = None
        self._authorized_tx = False
        self.path = Path(path) if path else finance_home() / "state" / "ledger.db"
        backup.refuse_unfinished_restore(self.path.parent.parent)
        if readonly:
            self.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
            self.db.row_factory = sqlite3.Row
            try:
                check_version(self.db, require_current=True)
            except BaseException:
                self.db.close()
                raise
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=timeout)
        self.db.row_factory = sqlite3.Row
        try:
            with backup.before_migration(self.db, self.path):
                migrate(self.db)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def guard_writes(self, authorize: Callable[[Ledger], None] | None) -> Iterator[None]:
        """Bind authority to each outer write transaction, without locking during parsing or I/O."""
        if authorize is None:
            yield
            return
        if self.db.in_transaction or self._write_guard is not None:
            raise RuntimeError("Install the write guard before starting a transaction")
        self._write_guard = authorize
        try:
            yield
            if self.db.in_transaction:
                raise RuntimeError("A guarded writer left a transaction open")
        finally:
            if self.db.in_transaction:
                self.db.rollback()
            self._write_guard = None

    @contextmanager
    def tx(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Take the write lock with BEGIN IMMEDIATE; nest transactions as savepoints.

        immediate=False opts into deferred BEGIN for callers that do not need the
        write lock before their first write. It has no effect on nested savepoints.
        Only the outer transaction commits, even if its first write is nested.
        A guarded connection always takes the write lock and checks authority before
        yielding. Nested writes share that decision and cannot change its principal.

        A nested failure rolls back its own writes. If SQLite aborts the entire
        transaction (for example on disk full), preserve that original failure.
        """
        if self.db.in_transaction:
            if self._write_guard is not None and not self._authorized_tx:
                raise RuntimeError("Start guarded writes through Ledger.tx")
            # Nested engine writes must remain part of the caller's atomic operation.
            name = "nested_" + uuid4().hex
            self.db.execute(f"SAVEPOINT {name}")
            try:
                yield self.db
                self.db.execute(f"RELEASE {name}")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute(f"ROLLBACK TO {name}")
                    self.db.execute(f"RELEASE {name}")
                raise
        else:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE" if immediate or self._write_guard else "BEGIN")
                if self._write_guard is not None:
                    try:
                        self._write_guard(self)
                    except PermissionError as exc:
                        raise AuthorizationRefused(str(exc)) from None
                    self._authorized_tx = True
                try:
                    yield self.db
                finally:
                    self._authorized_tx = False

    @staticmethod
    def _require_user_admin(db: sqlite3.Connection, actor_id: str | None) -> None:
        """None is the server CLI, never an identity accepted from an HTTP body."""
        if (
            actor_id is not None
            and not db.execute(
                "SELECT 1 FROM users WHERE id=? AND role='admin' AND disabled_at IS NULL", (actor_id,)
            ).fetchone()
        ):
            raise PermissionError("admin_required")

    def add_user(self, values: dict[str, str | bytes], actor_id: str | None) -> None:
        with self.tx() as db:
            self._require_user_admin(db, actor_id)
            if db.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (values["username"],)).fetchone():
                raise ValueError("Numele de utilizator este deja folosit.")
            db.execute(
                """INSERT INTO users(id,username,display_name,role,webauthn_handle,created_at,created_by)
                VALUES(:id,:username,:display_name,:role,:webauthn_handle,:created_at,:created_by)""",
                values,
            )
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'user_created',?,?)",
                (values["created_at"], values["id"], values["created_by"]),
            )

    def change_user(
        self,
        user_id: str,
        actor_id: str | None,
        stamp: str,
        *,
        role: str | None = None,
        enabled: bool | None = None,
        display_name: str | None = None,
    ) -> None:
        if role not in (None, "admin", "member", "readonly"):
            raise ValueError("Rol necunoscut.")
        with self.tx() as db:
            self._require_user_admin(db, actor_id)
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                raise ValueError("unknown_user")
            new_role = user["role"] if role is None else role
            name = user["display_name"] if display_name is None else display_name
            disabled = user["disabled_at"]
            if enabled is True:
                disabled = None
            elif enabled is False and disabled is None:
                disabled = stamp
            if new_role == user["role"] and disabled == user["disabled_at"] and name == user["display_name"]:
                return
            self._keep_working_admin(db, user, new_role, disabled)
            changed = db.execute(
                """UPDATE users SET role=?,disabled_at=?,disabled_by=?,display_name=?
                WHERE id=? AND role=? AND disabled_at IS ? AND display_name=?""",
                (
                    new_role,
                    disabled,
                    user["disabled_by"]
                    if disabled == user["disabled_at"]
                    else (None if disabled is None else (actor_id or "cli")),
                    name,
                    user_id,
                    user["role"],
                    user["disabled_at"],
                    user["display_name"],
                ),
            ).rowcount
            if changed != 1:
                raise PermissionError("user_changed")
            if disabled is not None:
                self._revoke_user_access(db, user_id, stamp)
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor,detail) VALUES(?,'user_changed',?,?,?)",
                (
                    stamp,
                    user_id,
                    "cli" if actor_id is None else "user " + actor_id,
                    json.dumps({"role": new_role, "enabled": disabled is None, "display_name": name}),
                ),
            )

    @staticmethod
    def _keep_working_admin(db: sqlite3.Connection, user: sqlite3.Row, role: str, disabled: str | None) -> None:
        working = (
            user["role"] == "admin"
            and user["disabled_at"] is None
            and db.execute("SELECT 1 FROM users WHERE id=? AND " + _WORKING_SIGNIN, (user["id"],)).fetchone()
        )
        if (
            working
            and (role != "admin" or disabled is not None)
            and not db.execute(
                "SELECT 1 FROM users WHERE id!=? AND role='admin' AND disabled_at IS NULL AND "
                + _WORKING_SIGNIN
                + " LIMIT 1",
                (user["id"],),
            ).fetchone()
        ):
            raise ValueError("Instalarea trebuie să aibă cel puțin un administrator care se poate conecta.")

    @staticmethod
    def _revoke_user_access(db: sqlite3.Connection, user_id: str, stamp: str) -> None:
        db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (stamp, user_id))
        db.execute("UPDATE auth_tickets SET used_at=? WHERE user_id=? AND used_at IS NULL", (stamp, user_id))
        db.execute("UPDATE setup_links SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (stamp, user_id))

    def add_setup_link(self, values: dict[str, str], actor_id: str | None) -> None:
        """Replace links atomically; an admin reset also removes factors and access.

        Server CLI recovery may reset the last admin. HTTP callers always carry
        an enabled administrator id and must pass recent authentication first.
        """
        with self.tx() as db:
            self._require_user_admin(db, actor_id)
            user = db.execute("SELECT * FROM users WHERE id=? AND disabled_at IS NULL", (values["user_id"],)).fetchone()
            if not user:
                raise ValueError("unknown_user")
            reset = values["purpose"] == "reset"
            if reset and actor_id is not None:
                self._keep_working_admin(db, user, user["role"], values["created_at"])
            if not reset and (
                user["password_hash"]
                or user["totp_version"]
                or db.execute("SELECT 1 FROM passkeys WHERE user_id=?", (user["id"],)).fetchone()
            ):
                raise ValueError("account_already_configured")
            if reset:
                db.execute(
                    "UPDATE users SET password_hash=NULL,totp_version=NULL,totp_step=NULL,locked_until=NULL,failures=0,failures_since=NULL WHERE id=?",
                    (user["id"],),
                )
                db.execute("DELETE FROM recovery_codes WHERE user_id=?", (user["id"],))
                db.execute("DELETE FROM passkeys WHERE user_id=?", (user["id"],))
                self.clear_auth_failures(user["id"], values["auth_key"])
                self._revoke_user_access(db, user["id"], values["created_at"])
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'user_reset',?,?)",
                    (values["created_at"], user["id"], values["created_by"]),
                )
            db.execute(
                "UPDATE setup_links SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (values["created_at"], user["id"]),
            )
            db.execute(
                """INSERT INTO setup_links(id,secret_hash,user_id,purpose,created_by,created_at,expires_at)
                VALUES(:id,:secret_hash,:user_id,:purpose,:created_by,:created_at,:expires_at)""",
                values,
            )
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'setup_link_created',?,?)",
                (values["created_at"], user["id"], values["created_by"]),
            )

    def consume_setup_link(self, ident: str, secret_hash: str, stamp: str) -> bool:
        """The factor flow consumes the link in its own activation transaction."""
        with self.tx() as db:
            row = db.execute(
                """UPDATE setup_links SET used_at=? WHERE id=? AND secret_hash=?
                AND expires_at>? AND used_at IS NULL AND revoked_at IS NULL
                AND EXISTS(SELECT 1 FROM users WHERE id=setup_links.user_id AND disabled_at IS NULL)
                RETURNING user_id""",
                (stamp, ident, secret_hash, stamp),
            ).fetchone()
            if row:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'setup_link_used',?,?)",
                    (stamp, row["user_id"], "user " + row["user_id"]),
                )
            return row is not None

    def add_auth_ticket(self, values: dict[str, str | bytes | None], stamp: str) -> bool:
        """Publish pre-authentication state, bounding live tickets across the install."""
        with self.tx() as db:
            if values["purpose"] == "passkey_signin":
                if (
                    values["user_id"] is not None
                    or values["session_hash"] is not None
                    or not isinstance(values["challenge"], bytes)
                    or len(values["challenge"]) != 32
                ):
                    return False
            else:
                owner = db.execute("SELECT disabled_at FROM users WHERE id=?", (values["user_id"],)).fetchone()
                if not owner or owner["disabled_at"]:
                    return False
            db.execute("DELETE FROM auth_tickets WHERE used_at IS NOT NULL OR expires_at<=?", (stamp,))
            db.execute(
                """DELETE FROM auth_tickets WHERE id_hash IN
                (SELECT id_hash FROM auth_tickets ORDER BY expires_at DESC,id_hash DESC LIMIT -1 OFFSET 999)"""
            )
            db.execute(
                """INSERT INTO auth_tickets(id_hash,purpose,user_id,session_hash,challenge,expires_at)
                VALUES(:id_hash,:purpose,:user_id,:session_hash,:challenge,:expires_at)""",
                values,
            )
            return True

    def attempt_auth_ticket(self, id_hash: str, purpose: str, session_hash: str | None, stamp: str) -> dict | None:
        """Claim one of five attempts; the session binding cannot be supplied by another account."""
        with self.tx() as db:
            row = db.execute(
                """UPDATE auth_tickets SET attempts=attempts+1
                WHERE id_hash=? AND purpose=? AND session_hash IS ?
                AND used_at IS NULL AND expires_at>? AND attempts<5
                AND """
                + _LIVE_TICKET_OWNER
                + " RETURNING *",
                (id_hash, purpose, session_hash, stamp),
            ).fetchone()
            return dict(row) if row else None

    def consume_auth_ticket(self, ticket: dict, stamp: str) -> bool:
        """A verified attempt wins once; races, disable and later attempts invalidate it.

        The flow wraps attempt, factor verification and consumption in one outer
        transaction. Ticket data comes from attempt_auth_ticket, never a request.
        """
        with self.tx() as db:
            changed = db.execute(
                """UPDATE auth_tickets SET used_at=? WHERE id_hash=? AND purpose=? AND user_id IS ?
                AND session_hash IS ? AND attempts=? AND attempts BETWEEN 1 AND 5
                AND used_at IS NULL AND expires_at>?
                AND """
                + _LIVE_TICKET_OWNER,
                (
                    stamp,
                    ticket["id_hash"],
                    ticket["purpose"],
                    ticket["user_id"],
                    ticket["session_hash"],
                    ticket["attempts"],
                    stamp,
                ),
            ).rowcount
            return changed == 1

    def add_passkey(self, values: dict) -> bool:
        """Registration proof and its single-use ticket must share the caller's transaction."""
        with self.tx() as db:
            changed = db.execute(
                """INSERT INTO passkeys
                (credential_id,user_id,public_key,algorithm,sign_count,backup_eligible,
                backed_up,aaguid,name,transports,created_at)
                SELECT :credential_id,id,:public_key,:algorithm,:sign_count,:backup_eligible,
                :backed_up,:aaguid,:name,:transports,:created_at FROM users
                WHERE id=:user_id AND disabled_at IS NULL
                AND NOT EXISTS(SELECT 1 FROM passkeys WHERE credential_id=:credential_id)""",
                values,
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor,detail) VALUES(?,'factor_added',?,?,?)",
                    (values["created_at"], values["user_id"], "user " + values["user_id"], '{"method":"passkey"}'),
                )
            return changed == 1

    def use_passkey(self, expected: dict, counter: int, eligible: bool, backed_up: bool, stamp: str) -> bool:
        """Only after signature proof; consume the challenge in the same outer transaction.

        A counter refusal returns normally so its audit commits. Zero-to-zero
        authenticators rely on the flow's single-use challenge, not the counter.
        """
        if (
            type(counter) is not int
            or not 0 <= counter <= 4294967295
            or type(eligible) is not bool
            or type(backed_up) is not bool
            or backed_up > eligible
            or eligible != expected["backup_eligible"]
        ):
            return False
        identity = (
            expected["credential_id"],
            expected["user_id"],
            expected["public_key"],
            expected["algorithm"],
            expected["backup_eligible"],
        )
        predicate = """credential_id=? AND user_id=? AND public_key=? AND algorithm=?
            AND backup_eligible=?
            AND EXISTS(SELECT 1 FROM users WHERE id=passkeys.user_id AND disabled_at IS NULL)"""
        with self.tx() as db:
            current = db.execute("SELECT sign_count FROM passkeys WHERE " + predicate, identity).fetchone()
            if not current:
                return False
            if current[0] and counter <= current[0]:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'passkey_counter_refused',?,?)",
                    (stamp, expected["user_id"], "user " + expected["user_id"]),
                )
                return False
            changed = db.execute(
                "UPDATE passkeys SET sign_count=?,backed_up=?,last_used_at=? WHERE "
                + predicate
                + " AND sign_count=? AND (sign_count=0 OR sign_count<?)",
                (counter, int(backed_up), stamp, *identity, expected["sign_count"], counter),
            ).rowcount
            return changed == 1

    def rename_passkey(self, credential_id: bytes, user_id: str, name: str, stamp: str) -> bool:
        """The caller must prove recent owner authentication under the same write lock."""
        with self.tx() as db:
            changed = db.execute(
                """UPDATE passkeys SET name=? WHERE credential_id=? AND user_id=? AND name!=?
                AND EXISTS(SELECT 1 FROM users WHERE id=passkeys.user_id AND disabled_at IS NULL)""",
                (name, credential_id, user_id, name),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'passkey_renamed',?,?)",
                    (stamp, user_id, "user " + user_id),
                )
            return changed == 1

    def remove_passkey(self, credential_id: bytes, user_id: str, stamp: str) -> bool:
        """Owner-only removal cannot discard the last complete strong sign-in method."""
        with self.tx() as db:
            if not db.execute(
                """SELECT 1 FROM passkeys WHERE credential_id=? AND user_id=?
                AND EXISTS(SELECT 1 FROM users WHERE id=passkeys.user_id AND disabled_at IS NULL)""",
                (credential_id, user_id),
            ).fetchone():
                return False
            changed = db.execute(
                """DELETE FROM passkeys WHERE credential_id=? AND user_id=?
                AND EXISTS(SELECT 1 FROM users WHERE id=passkeys.user_id AND disabled_at IS NULL AND ("""
                + _PASSWORD_TOTP
                + """ OR EXISTS(SELECT 1 FROM passkeys remaining
                    WHERE remaining.user_id=users.id AND remaining.credential_id!=passkeys.credential_id)))""",
                (credential_id, user_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Ultima metodă de conectare nu poate fi eliminată.")
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor,detail) VALUES(?,'factor_removed',?,?,?)",
                (stamp, user_id, "user " + user_id, '{"method":"passkey"}'),
            )
            return True

    def add_session(self, values: dict[str, str | int]) -> bool:
        """Publish a fresh session only while its owner is still enabled.

        The authentication flow calls this in the same transaction that consumes
        its second factor; a password check alone must never call this method.
        """
        with self.tx() as db:
            changed = db.execute(
                """INSERT INTO sessions
                (id_hash,user_id,method,recovery_only,created_at,last_seen_at,idle_expires_at,
                 expires_at,strong_auth_at,user_agent,ip)
                SELECT :id_hash,id,:method,:recovery_only,:created_at,:created_at,:idle_expires_at,
                 :expires_at,:created_at,:user_agent,:ip FROM users
                WHERE id=:user_id AND disabled_at IS NULL""",
                values,
            ).rowcount
            return changed == 1

    def touch_session(self, id_hash: str, stamp: str, idle: str, cutoff: str) -> None:
        """Usage metadata is best effort; the caller always reads committed revocations."""
        timeout = self.db.execute("PRAGMA busy_timeout").fetchone()[0]
        try:
            self.db.execute("PRAGMA busy_timeout=0")
            with self.tx() as db:
                db.execute(
                    """UPDATE sessions SET last_seen_at=?,idle_expires_at=min(expires_at,?)
                    WHERE id_hash=? AND revoked_at IS NULL AND last_seen_at<=?
                    AND expires_at>? AND idle_expires_at>?
                    AND EXISTS (SELECT 1 FROM users WHERE id=sessions.user_id AND disabled_at IS NULL)""",
                    (stamp, idle, id_hash, cutoff, stamp, stamp),
                )
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
        finally:
            self.db.execute(f"PRAGMA busy_timeout={timeout}")

    def revoke_session(self, id_hash: str, user_id: str, stamp: str, *, audit: bool = False) -> bool:
        """Even an authenticated caller cannot close another user's session by id."""
        with self.tx() as db:
            changed = db.execute(
                "UPDATE sessions SET revoked_at=? WHERE id_hash=? AND user_id=? AND revoked_at IS NULL",
                (stamp, id_hash, user_id),
            ).rowcount
            if changed == 1 and audit:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'signout',?,?)",
                    (stamp, user_id, "user " + user_id),
                )
            return changed == 1

    def close_owned_session(self, id_hash: str, user_id: str, stamp: str) -> bool:
        with self.tx() as db:
            if not self.revoke_session(id_hash, user_id, stamp):
                return False
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'session_closed',?,?)",
                (stamp, user_id, "user " + user_id),
            )
            return True

    def close_other_sessions(self, user_id: str, current: str, stamp: str) -> int:
        with self.tx() as db:
            changed = db.execute(
                "UPDATE sessions SET revoked_at=? WHERE user_id=? AND id_hash!=? AND revoked_at IS NULL",
                (stamp, user_id, current),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor,detail) VALUES(?,'session_closed',?,?,?)",
                    (stamp, user_id, "user " + user_id, json.dumps({"scope": "others", "count": changed})),
                )
            return changed

    def close_admin_session(self, id_hash: str, actor_id: str, stamp: str) -> None:
        with self.tx() as db:
            self._require_user_admin(db, actor_id)
            row = db.execute(
                "UPDATE sessions SET revoked_at=? WHERE id_hash=? AND revoked_at IS NULL RETURNING user_id",
                (stamp, id_hash),
            ).fetchone()
            if not row:
                raise ValueError("Sesiunea nu mai este activă.")
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'session_closed',?,?)",
                (stamp, row["user_id"], "user " + actor_id),
            )

    def rehash_password(self, user: dict, replacement: str) -> None:
        with self.tx() as db:
            changed = db.execute(
                """UPDATE users SET password_hash=? WHERE id=? AND password_hash=?
                AND totp_version IS ? AND disabled_at IS NULL""",
                (replacement, user["id"], user["password_hash"], user["totp_version"]),
            ).rowcount
            if changed != 1:
                raise PermissionError("password_changed")

    def change_password(self, user: dict, replacement: str, current: str, stamp: str) -> None:
        with self.tx() as db:
            self.rehash_password(user, replacement)
            self.close_other_sessions(user["id"], current, stamp)
            db.execute("UPDATE auth_tickets SET used_at=? WHERE user_id=? AND used_at IS NULL", (stamp, user["id"]))
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'password_changed',?,?)",
                (stamp, user["id"], "user " + user["id"]),
            )

    def close_user_sessions(self, user_id: str, stamp: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (stamp, user_id))

    def record_signin(self, user_id: str, method: str, stamp: str, ip: str) -> None:
        if method not in ("password_totp", "passkey", "recovery", "recovery_code"):
            raise ValueError("Unknown sign-in method")
        with self.tx() as db:
            db.execute(
                """INSERT INTO auth_events(at,kind,user_id,actor,ip,detail)
                SELECT ?,'signin',id,?, ?,? FROM users WHERE id=? AND disabled_at IS NULL""",
                (stamp, "user " + user_id, ip[:64], json.dumps({"method": method}), user_id),
            )

    def forget_auth_sessions(self, stamp: str) -> None:
        """Server-only restore recovery: revoke cookies and temporary capabilities together."""
        with self.tx() as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL", (stamp,))
            db.execute("UPDATE auth_tickets SET used_at=? WHERE used_at IS NULL", (stamp,))
            db.execute("UPDATE setup_links SET revoked_at=? WHERE revoked_at IS NULL", (stamp,))
            db.execute("INSERT INTO auth_events(at,kind,actor) VALUES(?,'all_sessions_revoked','cli')", (stamp,))

    def purge_auth_state(self, stamp: str, cutoff: str) -> None:
        """Local retention only: preserve active access and all security audit events except old failures."""
        with self.tx() as db:
            db.execute(
                "DELETE FROM sessions WHERE expires_at<=? OR idle_expires_at<=? OR revoked_at IS NOT NULL",
                (stamp, stamp),
            )
            db.execute("DELETE FROM auth_tickets WHERE expires_at<=? OR used_at IS NOT NULL", (stamp,))
            db.execute(
                "DELETE FROM setup_links WHERE expires_at<=? OR used_at IS NOT NULL OR revoked_at IS NOT NULL", (stamp,)
            )
            db.execute("DELETE FROM auth_events WHERE kind='failed_signin' AND at<=?", (cutoff,))
            db.execute("UPDATE auth_events SET ip=NULL WHERE at<=? AND ip IS NOT NULL", (cutoff,))

    def finish_recovery(self, user_id: str, stamp: str, *, keep_passkey: bytes | None = None) -> None:
        with self.tx() as db:
            if keep_passkey is None:
                if not db.execute(
                    "SELECT 1 FROM users WHERE id=? AND disabled_at IS NULL AND " + _PASSWORD_TOTP, (user_id,)
                ).fetchone():
                    raise PermissionError("replacement_factor_required")
                db.execute("DELETE FROM passkeys WHERE user_id=?", (user_id,))
            else:
                changed = db.execute(
                    """UPDATE users SET password_hash=NULL,totp_version=NULL,totp_step=NULL WHERE id=? AND disabled_at IS NULL
                    AND EXISTS(SELECT 1 FROM passkeys WHERE user_id=users.id AND credential_id=?)""",
                    (user_id, keep_passkey),
                ).rowcount
                if changed != 1:
                    raise PermissionError("replacement_factor_required")
                db.execute("DELETE FROM passkeys WHERE user_id=? AND credential_id!=?", (user_id, keep_passkey))
            self._revoke_user_access(db, user_id, stamp)
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'recovery_completed',?,?)",
                (stamp, user_id, "user " + user_id),
            )

    def record_auth_failure(self, values: dict) -> None:
        with self.tx() as db:
            changed = db.execute(
                """INSERT INTO auth_limits(key_hash,user_id,failures,since,retry_at,locked_until)
                VALUES(:key_hash,:user_id,:failures,:since,:retry_at,:locked_until)
                ON CONFLICT(key_hash) DO UPDATE SET user_id=excluded.user_id,failures=excluded.failures,
                since=excluded.since,retry_at=excluded.retry_at,locked_until=excluded.locked_until
                WHERE auth_limits.failures=:expected AND auth_limits.since=:expected_since""",
                values,
            ).rowcount
            if changed != 1:
                raise PermissionError("authentication_limit_changed")
            db.execute(
                "UPDATE users SET failures=:failures,failures_since=:since,locked_until=:locked_until WHERE id=:user_id",
                values,
            )
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor,ip,detail) VALUES(:at,'failed_signin',:user_id,'anonymous',:ip,:detail)",
                values,
            )
            if values["locked_until"]:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor,ip) VALUES(:at,'account_locked',:user_id,'anonymous',:ip)",
                    values,
                )
            db.execute("INSERT INTO auth_failure_window(key_hash,at) VALUES(:key_hash,:window_at)", values)
            db.execute("DELETE FROM auth_failure_window WHERE at<=:cutoff", values)
            self._prune_auth_limits(db, values)

    @staticmethod
    def _prune_auth_limits(db: sqlite3.Connection, values: dict) -> None:
        removed = list(
            db.execute(
                """DELETE FROM auth_limits WHERE user_id IS NULL
                AND (busy_until IS NULL OR busy_until<=:at) AND (retry_at IS NULL OR retry_at<=:at)
                AND (locked_until IS NULL OR locked_until<=:at)
                AND NOT EXISTS(SELECT 1 FROM auth_failure_window w
                    WHERE w.key_hash=auth_limits.key_hash AND w.at>:cutoff)
                RETURNING key_hash""",
                values,
            )
        )
        removed += list(
            db.execute(
                "DELETE FROM auth_limits WHERE key_hash IN (SELECT key_hash FROM auth_limits WHERE user_id IS NULL ORDER BY since DESC,key_hash DESC LIMIT -1 OFFSET 10000) RETURNING key_hash"
            )
        )
        db.executemany("DELETE FROM auth_failure_window WHERE key_hash=?", [(r[0],) for r in removed])

    def reserve_auth_attempt(self, values: dict) -> None:
        with self.tx() as db:
            changed = db.execute(
                """INSERT INTO auth_limits(key_hash,user_id,failures,since,reservation,busy_until)
                VALUES(:key_hash,:user_id,0,:since,:reservation,:busy_until)
                ON CONFLICT(key_hash) DO UPDATE SET user_id=excluded.user_id,reservation=excluded.reservation,busy_until=excluded.busy_until
                WHERE (busy_until IS NULL OR busy_until<=:at) AND (retry_at IS NULL OR retry_at<=:at)
                AND (locked_until IS NULL OR locked_until<=:at)""",
                values,
            ).rowcount
            if changed != 1:
                raise PermissionError("authentication_reservation_changed")
            self._prune_auth_limits(db, values)

    def release_auth_attempt(self, key_hash: str, reservation: str, stamp: str) -> bool:
        with self.tx() as db:
            changed = db.execute(
                """UPDATE auth_limits SET reservation=NULL,busy_until=NULL
                WHERE key_hash=? AND reservation=? AND busy_until>?""",
                (key_hash, reservation, stamp),
            ).rowcount
            return changed == 1

    def clear_auth_failures(self, user_id: str, key_hash: str) -> None:
        with self.tx() as db:
            db.execute(
                "DELETE FROM auth_failure_window WHERE key_hash IN (SELECT key_hash FROM auth_limits WHERE user_id=? OR key_hash=?)",
                (user_id, key_hash),
            )
            db.execute("DELETE FROM auth_limits WHERE user_id=? OR key_hash=?", (user_id, key_hash))
            db.execute("UPDATE users SET failures=0,failures_since=NULL,locked_until=NULL WHERE id=?", (user_id,))

    def unlock_user(self, user_id: str, key_hash: str, stamp: str, actor_id: str | None) -> None:
        with self.tx() as db:
            self._require_user_admin(db, actor_id)
            self.clear_auth_failures(user_id, key_hash)
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'account_unlocked',?,?)",
                (stamp, user_id, "cli" if actor_id is None else "user " + actor_id),
            )

    def replace_recovery_codes(self, user_id: str, hashes: list[str], stamp: str) -> None:
        if len(hashes) != 10 or len(set(hashes)) != 10 or not all(re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes):
            raise ValueError("Ten distinct recovery code hashes are required")
        with self.tx() as db:
            if not db.execute(
                "SELECT 1 FROM users WHERE id=? AND disabled_at IS NULL AND " + _WORKING_SIGNIN,
                (user_id,),
            ).fetchone():
                raise PermissionError("factor_required")
            db.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))
            db.executemany(
                "INSERT INTO recovery_codes(user_id,code_hash,created_at) VALUES(?,?,?)",
                [(user_id, value, stamp) for value in hashes],
            )
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'recovery_codes_regenerated',?,?)",
                (stamp, user_id, "user " + user_id),
            )

    def consume_recovery_code(self, user_id: str, code_hash: str, stamp: str) -> bool:
        with self.tx() as db:
            changed = db.execute(
                """UPDATE recovery_codes SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL
                AND EXISTS(SELECT 1 FROM users WHERE id=recovery_codes.user_id AND disabled_at IS NULL)""",
                (stamp, user_id, code_hash),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'recovery_code_used',?,?)",
                    (stamp, user_id, "user " + user_id),
                )
            return changed == 1

    def activate_totp(self, ticket: dict, data: dict, step: int, stamp: str) -> bool:
        """Activate exactly the credentials read at publication, never after reset or another activation."""
        with self.tx() as db:
            changed = db.execute(
                """UPDATE users SET password_hash=?,totp_version=?,totp_step=? WHERE id=?
                AND disabled_at IS NULL AND totp_version IS ? AND password_hash IS ?""",
                (
                    data["password_hash"],
                    data["version"],
                    step,
                    ticket["user_id"],
                    data["expected_version"],
                    data["expected_password"],
                ),
            ).rowcount
            if changed != 1:
                return False
            db.execute(
                """UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL
                AND (id_hash!=? OR ? IS NULL)""",
                (stamp, ticket["user_id"], ticket["session_hash"], ticket["session_hash"]),
            )
            db.execute(
                "INSERT INTO auth_events(at,kind,user_id,actor,detail) VALUES(?,'factor_added',?,?,?)",
                (stamp, ticket["user_id"], "user " + ticket["user_id"], '{"method":"password_totp"}'),
            )
            if data["expected_version"] is not None:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor,detail) VALUES(?,'factor_removed',?,?,?)",
                    (stamp, ticket["user_id"], "user " + ticket["user_id"], '{"method":"totp"}'),
                )
            if data["expected_password"] is not None and data["password_hash"] != data["expected_password"]:
                db.execute(
                    "INSERT INTO auth_events(at,kind,user_id,actor) VALUES(?,'password_changed',?,?)",
                    (stamp, ticket["user_id"], "user " + ticket["user_id"]),
                )
            return True

    def consume_totp(self, user_id: str, version: str, step: int) -> bool:
        """A seed version and time step can authenticate an enabled user only once.

        Recheck under the write lock: another connection, reset or disable may have
        invalidated the factor since the caller read the seed and verified its code.
        """
        with self.tx() as db:
            changed = db.execute(
                """UPDATE users SET totp_step=? WHERE id=? AND totp_version=?
                AND disabled_at IS NULL AND (totp_step IS NULL OR totp_step < ?)""",
                (step, user_id, version, step),
            ).rowcount
            return changed == 1

    def save_company(
        self,
        company: companies.Company,
        *,
        expected: companies.Company | None = None,
        lookup_profile: dict | None = None,
    ) -> None:
        """Insert an identity or replace its facts only if the caller's version still holds.

        BEGIN IMMEDIATE covers the comparison and every child row. Identity fields
        cannot change, and database uniqueness arbitrates simultaneous CUI/IBAN adds.
        """
        company = company.validated()
        with self.tx() as db:
            row = db.execute("SELECT * FROM firms WHERE key=?", (company.key,)).fetchone()
            if (row is not None) != (expected is not None) or expected and not self._company_matches(expected):
                raise ValueError("Company changed; reload before saving.")
            if expected is None:
                db.execute(
                    "INSERT INTO firms(key,name,legal_name,cui,palette,lookup_profile) VALUES(?,?,?,?,?,?)",
                    (
                        company.key,
                        company.name,
                        company.legal_name,
                        company.cui,
                        company.palette,
                        json.dumps(lookup_profile) if lookup_profile is not None else None,
                    ),
                )
            else:
                if (company.key, company.cui, company.legal_name) != (expected.key, expected.cui, expected.legal_name):
                    raise ValueError("Company identity cannot change.")
                changed = db.execute(
                    "UPDATE firms SET name=?,palette=? WHERE key=? AND cui=? AND legal_name=?",
                    (company.name, company.palette, company.key, expected.cui, expected.legal_name),
                )
                if changed.rowcount != 1:
                    raise ValueError("Company changed; reload before saving.")
            for table in ("firm_accounts", "firm_aliases"):
                db.execute(f"DELETE FROM {table} WHERE company=?", (company.key,))
            db.executemany(
                "INSERT INTO firm_accounts(company,position,iban,currency,bank,pays,retired,opened) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [
                    (company.key, n, a.iban, a.currency, a.bank, a.pays, a.retired, a.opened)
                    for n, a in enumerate(company.accounts)
                ],
            )
            for table, values in (("firm_aliases", company.aliases),):
                db.executemany(
                    f"INSERT INTO {table} VALUES(?,?,?)", [(company.key, n, value) for n, value in enumerate(values)]
                )

    def _company_matches(self, expected: companies.Company) -> bool:
        row = self.db.execute(
            "SELECT key,legal_name,cui,name,palette FROM firms WHERE key=?", (expected.key,)
        ).fetchone()
        if tuple(row) != (expected.key, expected.legal_name, expected.cui, expected.name, expected.palette):
            return False
        accounts = [
            tuple(r)
            for r in self.db.execute(
                "SELECT iban,currency,bank,pays,retired,opened FROM firm_accounts WHERE company=? ORDER BY position",
                (expected.key,),
            )
        ]
        if accounts != [(a.iban, a.currency, a.bank, a.pays, a.retired, a.opened) for a in expected.accounts]:
            return False
        for table, field, values in (("firm_aliases", "alias", expected.aliases),):
            rows = self.db.execute(f"SELECT {field} FROM {table} WHERE company=? ORDER BY position", (expected.key,))
            if tuple(r[0] for r in rows) != values:
                return False
        return True

    def card_owners(self) -> dict[tuple[str, str], str]:
        return {(r[0], r[1]): r[2] for r in self.db.execute("SELECT company,card,owner FROM card_owners")}

    def firm_cards(self, company: str) -> list[str]:
        """The last four digits of the cards a person said belong to `company` (assign_card)."""
        rows = self.db.execute(
            "SELECT card FROM card_owners WHERE company=? AND owner=? ORDER BY card", (company, company)
        )
        return [r[0] for r in rows]

    def card_owner(self, company: str, card: str) -> str | None:
        row = self.db.execute("SELECT owner FROM card_owners WHERE company=? AND card=?", (company, card)).fetchone()
        return row[0] if row else None

    def assign_card(self, company: str, key: str, owner: str, by: str | None = None) -> bool:
        """Ask once, for an observed card only; serialize conflicting answers without overwriting one."""
        with self.tx() as db:
            row = db.execute("SELECT tx FROM statement_txs WHERE tx_key=? AND company=?", (key, company)).fetchone()
            card = json.loads(row[0]).get("card_last4") if row and row[0] else None
            if not card or not re.fullmatch(r"[0-9]{4}", card):
                return False
            if owner != "personal" and not db.execute("SELECT 1 FROM firms WHERE key=?", (owner,)).fetchone():
                return False
            return (
                db.execute(
                    "INSERT OR IGNORE INTO card_owners VALUES(?,?,?,?,?)", (company, card, owner, by, now())
                ).rowcount
                == 1
            )

    def await_invoice(self, company: str, key: str, note: str, by: str | None = None) -> bool:
        """Aștept factura: a debit of this company, not explained or settled, waits for its invoice, with the user's
        note if any. Reserves nothing: the invoice still settles it when it arrives."""
        with self.tx() as db:
            return (
                db.execute(
                    "INSERT OR REPLACE INTO tx_awaiting(tx_key,company,note,by,at) SELECT ?,?,?,?,? WHERE EXISTS "
                    "(SELECT 1 FROM statement_txs WHERE tx_key=? AND company=? AND direction='debit') "
                    "AND NOT EXISTS (SELECT 1 FROM settled_tx WHERE tx_key=?)",
                    (key, company, note, by, now(), key, company, key),
                ).rowcount
                == 1
            )

    def stop_awaiting(self, company: str, key: str) -> bool:
        """The debit no longer waits for its invoice (await_invoice)."""
        with self.tx() as db:
            return db.execute("DELETE FROM tx_awaiting WHERE tx_key=? AND company=?", (key, company)).rowcount == 1

    def awaited(self, company: str) -> dict[str, dict]:
        """The debits waiting for their invoice: {tx_key: {note, by (display name), at}}."""
        return {
            r["tx_key"]: {"note": r["note"], "by": r["by_name"], "at": r["at"][:19]}
            for r in self.db.execute(
                f"SELECT a.*, {ACTOR_NAME.format('a.by')} FROM tx_awaiting a WHERE company=?", (company,)
            )
        }

    def ai_proposals(self, company: str) -> dict[str, dict]:
        """What the AI integration proposed for lines still to explain (ai_explain): {tx_key: {kind, partner,
        invoices, reason, note}}; kind 'none' marks a line asked with nothing to propose."""
        return {
            r["tx_key"]: {k: r[k] for k in ("kind", "partner", "reason", "note")}
            | {"invoices": json.loads(r["invoices"])}
            for r in self.db.execute("SELECT * FROM tx_ai WHERE company=?", (company,))
        }

    def keep_ai_proposals(self, company: str, proposals: dict[str, dict]) -> None:
        """Keep the AI's checked proposals by line, replacing an older one for the same line."""
        with self.tx() as db:
            for key, p in proposals.items():
                db.execute(
                    "INSERT OR REPLACE INTO tx_ai(tx_key,company,kind,partner,invoices,reason,note,at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        key,
                        company,
                        p["kind"],
                        p.get("partner"),
                        json.dumps(p.get("invoices") or []),
                        p.get("reason"),
                        p.get("note"),
                        now(),
                    ),
                )

    def card_allows(self, company: str, card: str) -> bool:
        return not card or self.card_owner(company, card) == company

    def _card_proof(self, table: str, ident: str, key: str | None) -> bool:
        """Recheck card ownership under the settling writer's lock, including the item's firm."""
        row = self.db.execute("SELECT company,tx FROM statement_txs WHERE tx_key=?", (key,)).fetchone()
        if not row or not row["tx"]:
            return True
        card = json.loads(row["tx"]).get("card_last4") or ""
        if not card:
            return True
        item = self.db.execute(
            f"SELECT company FROM {table} WHERE {('item_id' if table == 'payments' else 'message_id')}=?", (ident,)
        ).fetchone()
        return bool(item and item[0] == row["company"] and self.card_allows(item[0], card))

    # ---- meta
    def change_mode(self, live: bool, expected: bool, actor: str, stamp: str, request_id: str) -> bool:
        """Serialize browser and CLI switches; duplicate/stale submissions do nothing."""
        with self.tx() as db:
            if is_live() != expected or live == expected or self.get_meta("mode-request:" + request_id):
                return False
            self.set_meta("mode-request:" + request_id, stamp)
            db.execute(
                "INSERT INTO auth_events(at,kind,actor,detail) VALUES(?,'mode_requested',?,?)",
                (stamp, actor, "live" if live else "sandbox"),
            )
            revision = self.get_meta("mode:revision", "0")
        # Record intent before changing storage so a crash cannot hide the requesting actor.
        with self.tx() as db:
            if is_live() != expected or self.get_meta("mode:revision", "0") != revision:
                return False
            self.set_meta("mode:revision", uuid4().hex)
            marker = finance_home() / "state" / LIVE_MARKER
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if live:
                marker.touch(mode=0o600)
            else:
                marker.unlink(missing_ok=True)
            db.execute(
                "INSERT INTO auth_events(at,kind,actor,detail) VALUES(?,'mode_changed',?,?)",
                (stamp, actor, "live" if live else "sandbox"),
            )
        return True

    def meta_values(self, prefix: str) -> list[str]:
        """The values of every meta key starting with `prefix`, by key."""
        rows = self.db.execute("SELECT value FROM meta WHERE substr(key, 1, ?)=? ORDER BY key", (len(prefix), prefix))
        return [r[0] for r in rows]

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    def credit_picks(self) -> dict[str, str]:
        """Credit note id -> the invoice a person said it cancels (or "-": none)."""
        rows = self.db.execute("SELECT key,value FROM meta WHERE key >= 'credit_pick:' AND key < 'credit_pick;'")
        return {key.removeprefix("credit_pick:"): value for key, value in rows}

    def pick_credit_original(self, note_id: str, target: str, asked: Callable[[], bool]) -> bool:
        """Which invoice a credit note without a reference cancels, as a person answered; the first answer stands.
        `asked` re-checks the question under the write lock, so two answers can't take one invoice."""
        with self.tx() as db:
            if not asked():
                return False
            sql = "INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)"
            return db.execute(sql, (f"credit_pick:{note_id}", target)).rowcount == 1

    def create_payment_file(self, company: str, name: str) -> int:
        name = name.strip()
        if not 1 <= len(name) <= 200:
            raise ValueError("Payment file name must contain 1 to 200 characters")
        with self.tx() as db:
            highest = db.execute("SELECT coalesce(max(id),0) FROM payment_files").fetchone()[0]
            ident = max(highest, int(self.get_meta("payment_file_sequence") or "0")) + 1
            db.execute(
                "INSERT INTO payment_files(id,company,name,created_at) VALUES(?,?,?,?)", (ident, company, name, now())
            )
            self.set_meta("payment_file_sequence", str(ident))
            return ident

    def payment_files(self, *, company: str):
        return list(
            self.db.execute("SELECT * FROM payment_files WHERE company=? ORDER BY created_at DESC,id DESC", (company,))
        )

    def payment_file(self, file_id: int):
        return self.db.execute("SELECT * FROM payment_files WHERE id=?", (file_id,)).fetchone()

    def rename_payment_file(self, file_id: int, company: str, name: str) -> bool:
        name = name.strip()
        if not 1 <= len(name) <= 200:
            raise ValueError("Payment file name must contain 1 to 200 characters")
        with self.tx() as db:
            return bool(
                db.execute(
                    "UPDATE payment_files SET name=? WHERE id=? AND company=? AND imported_at IS NULL",
                    (name, file_id, company),
                ).rowcount
            )

    def delete_payment_file(self, file_id: int, company: str) -> bool:
        with self.tx() as db:
            empty = db.execute(
                "SELECT * FROM payment_files WHERE id=? AND company=? AND imported_at IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM payments WHERE file_id=?)",
                (file_id, company, file_id),
            ).fetchone()
            if not empty:
                return False
            self.set_meta(
                "payment_file_sequence", str(max(file_id, int(self.get_meta("payment_file_sequence") or "0")))
            )
            history = [dict(r) for r in db.execute("SELECT * FROM payment_downloads WHERE file_id=?", (file_id,))]
            self.set_meta(f"payment_deleted:{file_id}", json.dumps({"file": dict(empty), "downloads": history}))
            if empty["path"]:
                self.set_meta(f"payment_delete:{file_id}", json.dumps(dict(empty)))
            db.execute("DELETE FROM payment_downloads WHERE file_id=?", (file_id,))
            return bool(
                db.execute(
                    "DELETE FROM payment_files WHERE id=? AND company=? AND imported_at IS NULL "
                    "AND NOT EXISTS(SELECT 1 FROM payments WHERE file_id=?)",
                    (file_id, company, file_id),
                ).rowcount
            )

    def attach_payment_file(self, file_id: int, company: str, item_id: str, *, origin: str | None = None) -> bool:
        """The single payment row owns membership; competing writers cannot claim it twice."""
        if origin not in (None, "person", "auto"):
            raise ValueError("Invalid payment origin")
        with self.tx() as db:
            changed = db.execute(
                "UPDATE payments SET file_id=?,origin=coalesce(?,origin) WHERE item_id=? AND company=? AND file_id IS NULL "
                "AND status='pending' AND EXISTS(SELECT 1 FROM payment_files "
                "WHERE id=? AND company=? AND imported_at IS NULL)",
                (file_id, origin, item_id, company, file_id, company),
            ).rowcount
            if changed:
                db.execute("UPDATE payment_files SET revision=revision+1 WHERE id=?", (file_id,))
            return bool(changed)

    def remove_file_payment(self, file_id: int, company: str, item_id: str) -> bool:
        with self.tx() as db:
            allowed = db.execute(
                "SELECT 1 FROM payments p JOIN payment_files f ON f.id=p.file_id "
                "WHERE p.item_id=? AND f.id=? AND f.company=? AND f.imported_at IS NULL "
                "AND p.status IN ('pending','exported','skipped') "
                "AND NOT EXISTS(SELECT 1 FROM tx_link_items WHERE item='payment ' || p.item_id)",
                (item_id, file_id, company),
            ).fetchone()
            if not allowed:
                return False
            skipped = db.execute("SELECT 1 FROM payments WHERE item_id=? AND status='skipped'", (item_id,)).fetchone()
            if skipped:
                db.execute(
                    "UPDATE payments SET file_id=NULL,csv_path=NULL WHERE item_id=? AND file_id=? AND status='skipped'",
                    (item_id, file_id),
                )
                db.execute("UPDATE payment_files SET revision=revision+1 WHERE id=?", (file_id,))
                return True
            invoice = db.execute(
                "SELECT message_id FROM efactura WHERE item_id=? AND company=? AND status='linked'", (item_id, company)
            ).fetchone()
            if invoice:
                self.set_meta(f"payment_file_removed:{invoice['message_id']}", str(file_id))
            db.execute(
                "UPDATE efactura SET status='open',item_id=NULL,paid_note=NULL "
                "WHERE item_id=? AND company=? AND status='linked'",
                (item_id, company),
            )
            db.execute(
                "DELETE FROM payments WHERE item_id=? AND file_id=? AND status IN ('pending','exported')",
                (item_id, file_id),
            )
            db.execute("UPDATE payment_files SET revision=revision+1 WHERE id=?", (file_id,))
            return True

    def record_file_download(
        self, file_id: int, company: str, revision: int, version: str, items: list[str], value_date: str
    ) -> int | None:
        with self.tx() as db:
            if not db.execute(
                "SELECT 1 FROM payment_files WHERE id=? AND company=? AND revision=? AND imported_at IS NULL",
                (file_id, company, revision),
            ).fetchone():
                return None
            # Every download is its own row, the same bytes again included: the history shows each one. The latest is
            # the newest created_at, then the highest id (a tie within one second); rows recorded before #51 reused
            # an older id with a refreshed time, which the same order still reads right.
            return db.execute(
                "INSERT INTO payment_downloads(file_id,version,revision,items,created_at,value_date) "
                "VALUES(?,?,?,?,?,?)",
                (file_id, version, revision, json.dumps(items), now(), value_date),
            ).lastrowid

    def prepare_file_download(
        self, file_id: int, company: str, revision: int, path: str, items: list[str], source_iban: str
    ) -> None:
        with self.tx() as db:
            if not db.execute(
                "UPDATE payment_files SET path=? WHERE id=? AND company=? AND revision=? AND imported_at IS NULL",
                (path, file_id, company, revision),
            ).rowcount:
                raise ValueError("Payment file changed")
            self.mark_exported(items, path, source_iban)

    def close_payment_file(self, file_id: int, company: str, revision: int, job: str) -> bool:
        with self.tx() as db:
            if not db.execute(
                "UPDATE payment_files SET imported_at=? WHERE id=? AND company=? AND revision=? AND imported_at IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM payments p WHERE p.file_id=payment_files.id "
                "AND p.status IN ('pending','exported') AND p.item_id NOT IN (SELECT value FROM json_each(?, '$.ids')))",
                (now(), file_id, company, revision, job),
            ).rowcount:
                return False
            self.set_meta(f"payment_import:{file_id}", job)
            return True

    def answer_file_not_imported(self, file_id: int, company: str, revision: int, download_id: int) -> bool:
        """The latest download was not imported: bump the revision so that download can no longer be imported."""
        with self.tx() as db:
            latest = db.execute(
                "SELECT id FROM payment_downloads WHERE file_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (file_id,)
            ).fetchone()
            if (latest["id"] if latest else 0) != download_id:
                return False
            return bool(
                db.execute(
                    "UPDATE payment_files SET revision=revision+1 "
                    "WHERE id=? AND company=? AND revision=? AND imported_at IS NULL",
                    (file_id, company, revision),
                ).rowcount
            )

    def clear_inactive_file_path(self, file_id: int, company: str, revision: int) -> bool:
        with self.tx() as db:
            return bool(
                db.execute(
                    "UPDATE payment_files SET path=NULL WHERE id=? AND company=? AND revision=? AND imported_at IS NULL "
                    "AND NOT EXISTS(SELECT 1 FROM payments WHERE file_id=? AND status IN ('pending','exported'))",
                    (file_id, company, revision, file_id),
                ).rowcount
            )

    def finish_payment_file(self, file_id: int, path: str, items: list[str]) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE payments SET csv_path=? WHERE file_id=? AND item_id IN (SELECT value FROM json_each(?))",
                (path, file_id, json.dumps(items)),
            )
            db.execute("UPDATE payment_files SET path=? WHERE id=? AND imported_at IS NOT NULL", (path, file_id))

    # ---- payments
    def payment(self, item_id: str):
        return self.db.execute("SELECT * FROM payments WHERE item_id=?", (item_id,)).fetchone()

    def add_payment(self, item, status: str = "pending", note: str = "") -> bool:
        """Insert a parsed PaymentItem. Returns False if already known (never re-added)."""
        with self.tx() as db:
            return self._insert_payment(db, item, status, note)

    def _insert_payment(self, db, item, status: str, note: str, origin: str | None = None) -> bool:
        d = item.to_dict()
        # Same OP re-sent as a different file: same item_id -> dedupe. Also dedupe on source sha.
        dup = db.execute(
            "SELECT 1 FROM payments WHERE item_id=? OR (source_sha=? AND source_sha!='')",
            (d["item_id"], d["source_sha"]),
        ).fetchone()
        if dup:
            return False
        db.execute(
            """INSERT INTO payments(item_id,company,kind,month,payee_name,payee_iban,payee_bic,
               fiscal_code,amount,details,issue_date,doc_ref,source_path,source_sha,status,
               paid_at,paid_note,created_at,origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d["item_id"],
                d["company"],
                d["kind"],
                item.month,
                d["payee_name"],
                d["payee_iban"],
                d["payee_bic"],
                d["payee_fiscal_code"],
                d["amount"],
                d["details"],
                d["issue_date"],
                d["doc_ref"],
                d["source_path"],
                d["source_sha"],
                status,
                now() if status == "paid" else None,
                note or None,
                now(),
                origin,
            ),
        )
        return True

    def payments(self, status: str | tuple[str, ...] | None = None, company: str | None = None):
        q, args = "SELECT * FROM payments WHERE 1=1", []
        if status:
            st = (status,) if isinstance(status, str) else status
            q += f" AND status IN ({','.join('?' * len(st))})"
            args += list(st)
        if company:
            q += " AND company=?"
            args.append(company)
        return self.db.execute(q + " ORDER BY company, issue_date, item_id", args).fetchall()

    def set_credit_reference(self, message_id: str, path: str, reference: str, day: str) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE efactura SET billing_ref=?,billing_date=? WHERE message_id=? AND raw_path=? AND billing_ref IS NULL AND status='credit'",
                (reference, day, message_id, path),
            )

    def withdraw_credited_payment(self, message_id: str, item_id: str) -> bool:
        """Caller holds pay_lock and checks credits under the same write transaction."""
        with self.tx() as db:
            row = db.execute(
                "SELECT * FROM efactura WHERE message_id=? AND item_id=? AND status='linked'", (message_id, item_id)
            ).fetchone()
            return bool(row and self._withdraw_pending_invoice_payment(db, row))

    def mark_exported(self, item_ids: list[str], csv_path: str, source_iban: str | None = None) -> None:
        """`source_iban`: the company account the payment file debits; only a debit from it can pay these items."""
        with self.tx() as db:
            for iid in item_ids:
                db.execute(
                    "UPDATE payments SET status='exported', csv_path=?, exported_at=?, source_iban=? "
                    "WHERE item_id=? AND status='pending'",
                    (csv_path, now(), source_iban or None, iid),
                )

    def mark_paid(self, item_id: str, note: str, tx: str | None = None) -> bool:
        """`tx`: the statement debit that proves it (statements.tx_key); it can never pay anything else. A row whose
        payment file named the account it debits is paid only by a debit on record from that account; this is checked
        here, in the settling transaction, so an export binding the row while a matcher was deciding still counts."""
        note = audit.note(note)
        with self.tx() as db:
            if not self._card_proof("payments", item_id, tx):
                return False
            row = db.execute("SELECT source_iban FROM payments WHERE item_id=?", (item_id,)).fetchone()
            if (
                tx
                and row
                and row["source_iban"]
                and not db.execute(
                    "SELECT 1 FROM statement_txs WHERE tx_key=? AND iban=?", (tx, row["source_iban"])
                ).fetchone()
            ):
                return False
            if self._net_changed(db, item_id) or not self._reserve(db, tx, f"payment {item_id}"):
                return False
            cur = db.execute(
                "UPDATE payments SET status='paid', paid_at=?, paid_note=? WHERE item_id=? AND status!='paid'",
                (now(), note, item_id),
            )
            self._release_unless(db, tx, cur.rowcount)
        return cur.rowcount > 0

    def _net_changed(self, db, item_id: str) -> bool:
        """An invoice payment for a net (below its invoice's total, and a credit note of its supplier names the
        invoice) that is no longer the total minus the invoice's linked credits: a debit settling it would hide the
        difference, so no writer settles it. Rows no credit note names keep settling as they always did."""
        row = db.execute(
            "SELECT p.amount,p.company,e.message_id,e.total,e.number,coalesce(nullif(e.supplier_tax_id,''),"
            "e.supplier_cui) AS supplier FROM payments p JOIN efactura e ON e.item_id=p.item_id WHERE p.item_id=?",
            (item_id,),
        ).fetchone()
        if not row or not row["total"] or Decimal(row["amount"]) >= Decimal(row["total"]):
            return False
        named = credits.credit_number(row["number"] or "")
        if not named or not any(
            credits.credit_number(c["billing_ref"] or "") == named
            for c in db.execute(
                "SELECT billing_ref FROM efactura WHERE company=? AND doc_type='credit_note' "
                "AND coalesce(nullif(supplier_tax_id,''),supplier_cui)=?",
                (row["company"], row["supplier"]),
            )
        ):
            return False
        credited = credits.credited(self, row["company"])
        net = Decimal(row["total"]) - credited.get(row["message_id"], Decimal(0))
        return Decimal(row["amount"]) != net

    def sent_invoice_links(self, company: str, environment: str) -> dict[str, str]:
        return dict(
            self.db.execute(
                "SELECT l.message_id,l.invoice_id FROM invoice_sent_links l "
                "JOIN invoices i ON i.id=l.invoice_id JOIN issued s ON s.message_id=l.message_id "
                "WHERE i.company=? AND s.company=? AND l.environment=? AND s.status!='duplicate'",
                (company, company, environment),
            )
        )

    def issued_client_links(self, company: str) -> dict[str, str]:
        return dict(
            self.db.execute(
                "SELECT i.id,m.value FROM invoices i JOIN meta m ON m.key='draft_client:'||i.id "
                "JOIN invoice_clients c ON c.id=m.value AND c.company=i.company WHERE i.company=?",
                (company,),
            )
        )

    def paid_proofs(self, company: str) -> dict[str, str]:
        """Every received invoice and payment-file payment of `company` that evidence proves paid (the rule in _PROOFS):
        'efactura <message_id>' | 'payment <item_id>' (the names `settled_tx` reserves them under) -> the proof's key,
        a statement debit, `bon:<sha>` or `ef-proof:<mail>`. An item missing here is paid in the user's word at most:
        a tap, `fiscalm pay mark-paid`, a note, or a key with nothing behind it."""
        out: dict[str, str] = {}
        for what, key, _ in self.db.execute(_PROOFS, {"co": company}):
            out.setdefault(what, key)
        return out

    def record_statement(self, st) -> None:
        """The transactions of `st` (statements.Statement, the trustworthy accounts the matchers settle from) are on
        record as `st.company`'s: the evidence a statement key is checked against (paid_proofs).

        The same payment can arrive from a statement and from the bank (open banking) under two keys, with different
        dates or words. So one source settles an account's month: the first to bring it (account_month_source; a
        statement stored before, of that account and month, counts as having brought it). The other source's lines
        of that month are kept as evidence but reserved, so they settle nothing. Near a month's edge a payment can be
        dated on either side: a line within BOUNDARY_DAYS of the edge is reserved too when the neighbouring month
        belongs to the other source and holds a line of the same amount and direction near the edge (or its lines
        are too old to say). In doubt, a payment stays to confirm by hand rather than settle twice."""
        from fiscalm.bank.statements import tx_key
        from fiscalm.companies import normalize_iban

        source = getattr(st, "source", "file") or "file"
        lines = [
            (t, tx_key(t), normalize_iban(t.iban) or None)
            for t in st.settlement_txs
            if t.direction in ("debit", "credit")
        ]
        statement_keys = {key for _, key, _ in lines}
        with self.tx() as db:
            owners: dict[tuple[str, str], str] = {}

            def owner(iban: str, month: str, claim: bool = True) -> str | None:
                """The source that settles `month` of `iban`; `claim`: this source takes it if nobody has. The bank
                takes a month only when it read it from its first day, in this read or since its first one (`covered`):
                a month it saw in part stays the statement's (its lines there are evidence only)."""
                start = getattr(st, "period_start", None)
                if (
                    claim
                    and source == "ob"
                    and start
                    and start > date.fromisoformat(month + "-01")
                    and not self._feed_read_from(db, st.company, iban, month + "-01")
                ):
                    claim = None  # look, but don't take: and if nobody has it, it isn't the bank's
                if (iban, month) not in owners:
                    row = db.execute(
                        "SELECT source FROM account_month_source WHERE company=? AND iban=? AND month=?",
                        (st.company, iban, month),
                    ).fetchone()
                    found = row[0] if row else None
                    if not found and self._file_has(db, st.company, iban, month):
                        found = "file"
                    if not found and claim is None:
                        owners[(iban, month)] = "file"
                        return "file"
                    if not found and not claim:
                        return None
                    if not found:
                        db.execute(
                            "INSERT OR IGNORE INTO account_month_source VALUES(?,?,?,?)",
                            (st.company, iban, month, source),
                        )
                        found = db.execute(
                            "SELECT source FROM account_month_source WHERE company=? AND iban=? AND month=?",
                            (st.company, iban, month),
                        ).fetchone()[0]
                    owners[(iban, month)] = found
                return owners[(iban, month)]

            for t, key, iban in lines:
                day, amount, shown = t.date.isoformat(), f"{t.amount:.2f}", json.dumps(t.to_dict())
                new = db.execute(
                    "INSERT OR IGNORE INTO statement_txs(tx_key,company,direction,iban,day,amount,source,tx) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (key, st.company, t.direction, iban, day, amount, source, shown),
                ).rowcount
                if not new:
                    # A stable bank key cannot carry different monetary proof on a later read.
                    conflict = db.execute(
                        "SELECT 1 FROM statement_txs WHERE tx_key=? AND company=? "
                        "AND ((amount IS NOT NULL AND amount!=?) OR direction!=? "
                        "OR (iban IS NOT NULL AND iban!=?) "
                        "OR (tx IS NOT NULL AND json_extract(tx,'$.currency')!=?))",
                        (key, st.company, amount, t.direction, iban, t.currency),
                    ).fetchone()
                    if conflict:
                        db.execute(
                            "INSERT OR IGNORE INTO settled_tx VALUES(?,?,?)", (key, "bank reference conflict", now())
                        )
                        self._reference_copy(db, st.company, t, key, iban, True, statement_keys)
                    else:
                        self._learn_payer(db, st.company, key, t)
                    # A row recorded before accounts were (iban NULL) learns its account from the same statement
                    # read again; a recorded account is never overwritten, and another company's row is never touched.
                    db.execute(
                        "UPDATE statement_txs SET iban=COALESCE(iban,?), day=COALESCE(day,?), "
                        "amount=COALESCE(amount,?), source=COALESCE(source,?) WHERE tx_key=? AND company=? "
                        "AND direction=?",
                        (iban, day, amount, source, key, st.company, t.direction),
                    )
                    continue
                if not iban:
                    continue
                month = day[:7]
                reserved = owner(iban, month) != source
                if not reserved:
                    first = t.date.replace(day=1)
                    nxt = (first + timedelta(days=32)).replace(day=1)
                    lo = (t.date - timedelta(days=BOUNDARY_DAYS)).isoformat()  # the twin's possible days
                    hi = (t.date + timedelta(days=BOUNDARY_DAYS)).isoformat()
                    for edge, other_month in (
                        (first, (first - timedelta(days=1)).strftime("%Y-%m")),
                        (nxt, nxt.strftime("%Y-%m")),
                    ):
                        if abs((t.date - edge).days) > BOUNDARY_DAYS + 1 or owner(iban, other_month, False) in (
                            source,
                            None,
                        ):
                            continue
                        # rows recorded before accounts or days were kept can't be told apart: they hold the line
                        near = db.execute(
                            "SELECT count(*) FROM statement_txs WHERE company=? AND direction=? "
                            "AND COALESCE(source,'file')!=? AND (iban IS NULL OR iban=?) AND "
                            "(day IS NULL OR (day BETWEEN ? AND ? AND substr(day,1,7)=? AND amount=?))",
                            (st.company, t.direction, source, iban, lo, hi, other_month, amount),
                        )
                        if near.fetchone()[0]:
                            reserved = True
                if self._reference_copy(db, st.company, t, key, iban, reserved, statement_keys):
                    continue
                if reserved:
                    db.execute("INSERT OR IGNORE INTO settled_tx VALUES(?,?,?)", (key, f"twin {source}", now()))

    @staticmethod
    def _reference_copy(db, company: str, tx, key: str, iban: str, held: bool, statement_keys: set[str]) -> bool:
        """Keep both proofs, but reserve the later copy under the same write lock as recording it.

        Only a bank's account-servicer reference identifies a copy. Date, words and customer references do not.
        Conflicting evidence stays visible and reserved, rather than silently changing a proved payment.
        """
        reference = tx.bank_reference
        if not reference or reference == "NOTPROVIDED":
            return False
        rows = db.execute(
            "SELECT t.tx_key, t.amount, t.direction, t.tx, s.what FROM statement_txs t "
            "LEFT JOIN settled_tx s ON s.tx_key=t.tx_key WHERE t.company=? AND t.iban=? AND t.tx_key!=? "
            "AND (json_extract(t.tx,'$.bank_reference')=? OR json_extract(t.tx,'$.ref')=?) ORDER BY t.rowid",
            (company, iban, key, reference, "eb:" + reference),
        ).fetchall()
        # Distinct lines within one statement can share a transfer reference, including its fee.
        rows = [r for r in rows if r["tx_key"] not in statement_keys]
        if not rows:
            return False
        same = all(
            r["amount"] == f"{tx.amount:.2f}"
            and r["direction"] == tx.direction
            and json.loads(r["tx"])["currency"] == tx.currency
            for r in rows
        )
        roots = [r for r in rows if not (r["what"] or "").startswith("duplicate:")]
        primary = roots[0] if roots else rows[0]
        if same and roots and all((r["what"] or "").startswith("twin ") for r in roots):
            target = primary["tx_key"] if held else key
            for r in roots[1 if held else 0 :]:
                db.execute(
                    "UPDATE settled_tx SET what=? WHERE tx_key=? AND what LIKE 'twin %'",
                    ("duplicate:" + target, r["tx_key"]),
                )
            if not held:
                return False
        what = "duplicate:" + primary["tx_key"] if same else "bank reference conflict"
        db.execute("INSERT OR IGNORE INTO settled_tx VALUES(?,?,?)", (key, what, now()))
        if not same:
            for r in rows:
                db.execute("INSERT OR IGNORE INTO settled_tx VALUES(?,?,?)", (r["tx_key"], what, now()))
        return True

    def account_lines(self, company: str, since: str) -> list:
        """The company's account lines from `since` (an ISO day) on, newest first, each payment once: the one list
        Tranzacții shows and Acasă sums. Rows: n (the record order), tx_key, direction, iban, day, amount, source, tx
        (statements.Tx as JSON, or null) and what (its settled_tx reservation, or null).

        A line reserved as a twin (`twin <source>` by record_statement, `twin csv` by statement_intake.hold_csv_days, so
        that it settles nothing) is left out only when the lines on record hold its other copy: the same direction and
        amount, the same account (or either one unknown, so a real payment never disappears), at most BOUNDARY_DAYS
        apart; the closest pairs are made first, and a copy hides one twin, never two. A twin without one (the bank
        feed of a month no statement brought yet, a day a partial CSV doesn't cover) is a payment of its own and stays."""
        lo = (date.fromisoformat(since) - timedelta(days=BOUNDARY_DAYS)).isoformat()  # a copy can sit just before
        rows = self.db.execute(
            "SELECT t.rowid AS n, t.tx_key, t.direction, t.iban, t.day, t.amount, t.source, t.tx, s.what FROM statement_txs t "
            "LEFT JOIN settled_tx s ON s.tx_key=t.tx_key WHERE t.company=? AND t.day>=? AND t.amount IS NOT NULL "
            "ORDER BY t.day DESC, t.rowid DESC",
            (company, lo),
        ).fetchall()
        rows = [r for r in rows if not (r["what"] or "").startswith("duplicate:")]
        twin = lambda r: (r["what"] or "").startswith("twin ")  # noqa: E731
        copies: dict[tuple, list] = {}  # by direction, amount and day: only those can be copies
        for r in rows:
            if not twin(r):
                copies.setdefault((r["direction"], r["amount"], r["day"]), []).append(r)
        pairs = []  # every twin with every copy it could be, the closest days first
        for t in (r for r in rows if twin(r)):
            day = date.fromisoformat(t["day"])
            for apart in range(BOUNDARY_DAYS + 1):
                for d in {day - timedelta(days=apart), day + timedelta(days=apart)}:
                    for c in copies.get((t["direction"], t["amount"], d.isoformat()), ()):
                        if not c["iban"] or not t["iban"] or c["iban"] == t["iban"]:
                            pairs.append((apart, t["n"], c["n"], t["tx_key"], c["tx_key"]))
        used: set[str] = set()
        hidden: set[str] = set()
        for _, _, _, tw, copy in sorted(pairs):
            if tw not in hidden and copy not in used:
                used.add(copy)
                hidden.add(tw)
        return [r for r in rows if r["tx_key"] not in hidden and r["day"] >= since]

    def credit_lines(self, company: str, since: str) -> list:
        """Every credit on record for the company from `since` (an ISO day) on, each with its settled_tx reservation
        (`what`, or null), twins and conflicts included: the raw evidence of who may have paid, which account_lines
        narrows to one line per payment. Rows: tx_key, iban, day, amount, source, tx (statements.Tx as JSON, or null),
        what."""
        return self.db.execute(
            "SELECT t.tx_key, t.iban, t.day, t.amount, t.source, t.tx, s.what FROM statement_txs t "
            "LEFT JOIN settled_tx s ON s.tx_key=t.tx_key WHERE t.company=? AND t.direction='credit' AND t.day>=? "
            "ORDER BY t.day, t.rowid",
            (company, since),
        ).fetchall()

    @staticmethod
    def _feed_read_from(db, company: str, iban: str, day: str) -> bool:
        """The bank's feed has read the account every day since `day` or before (bank_connections.covered), over
        all its reads: a month it read from its first day is the bank's, whatever day this read starts."""
        rows = db.execute("SELECT covered FROM bank_connections WHERE company=?", (company,))
        return any(json.loads(c).get(iban, "9999-12-31") <= day for (c,) in rows)

    @staticmethod
    def _learn_payer(db, company: str, key: str, t) -> None:
        """The same money read again naming its payer (a parser that learned the layout, the feed adding a name)
        fills the payer fields the stored copy lacks: who paid is what a client reminder checks. Only an empty field
        is filled, only when the two reads agree on every payer field both name, and nothing else in the stored copy
        (its description, reference, dates) changes."""
        row = db.execute("SELECT tx FROM statement_txs WHERE tx_key=? AND company=?", (key, company)).fetchone()
        if not row:
            return
        if row["tx"] is None:
            db.execute(
                "UPDATE statement_txs SET tx=? WHERE tx_key=? AND company=?", (json.dumps(t.to_dict()), key, company)
            )
            return
        stored, fields = (
            json.loads(row["tx"]),
            {"counterparty": t.counterparty, "counterparty_iban": t.counterparty_iban},
        )
        if any(stored.get(f) and v and stored[f] != v for f, v in fields.items()):
            return  # the reads disagree on who paid: keep the first, in doubt
        learned = {f: v for f, v in fields.items() if v and not stored.get(f)}
        if learned:
            db.execute(
                "UPDATE statement_txs SET tx=? WHERE tx_key=? AND company=?",
                (json.dumps(stored | learned), key, company),
            )

    @staticmethod
    def _file_has(db, company: str, iban: str, month: str) -> bool:
        """A statement document of the company's month names this account (or, stored before accounts were recorded,
        names none: it was the company's only bank's)."""
        for (accounts,) in db.execute(
            "SELECT accounts FROM documents WHERE company=? AND month=? AND kind='statement'", (company, month)
        ):
            if not accounts or iban in json.loads(accounts):
                return True
        return False

    @staticmethod
    def _reserve(db, tx: str | None, what: str) -> bool:
        """One debit (or credit) settles one thing, ever: reserve `tx` first, inside the caller's transaction, so
        a second caller (another connection too: the insert takes the write lock) finds it taken. No tx: nothing to do."""
        if not tx:
            return True
        if db.execute("SELECT 1 FROM efactura WHERE paid_tx=?", (tx,)).fetchone():  # rows settled before settled_tx
            return False
        return db.execute("INSERT OR IGNORE INTO settled_tx VALUES(?,?,?)", (tx, what, now())).rowcount == 1

    @staticmethod
    def _release_unless(db, tx: str | None, settled: int) -> None:
        if tx and not settled:  # nothing was settled: the key stays free
            db.execute("DELETE FROM settled_tx WHERE tx_key=?", (tx,))

    def used_txs(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT tx_key FROM settled_tx")} | {
            r[0] for r in self.db.execute("SELECT paid_tx FROM efactura WHERE paid_tx IS NOT NULL")
        }

    # ---- ambiguous debits (efactura.note_ambiguous): refreshed on every match, the user picks one (resolve_debit)
    def set_ambiguous(self, key: str, company: str, tx: dict, candidates: list) -> None:
        with self.tx() as db:  # never one already reserved
            db.execute(
                "INSERT OR REPLACE INTO ambiguous_tx SELECT ?,?,?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM settled_tx WHERE tx_key=?) AND NOT EXISTS (SELECT 1 FROM efactura WHERE paid_tx=?)",
                (key, company, json.dumps(tx), json.dumps(sorted(candidates)), now(), key, key),
            )

    def drop_ambiguous(self, key: str | None = None) -> None:
        """One debit, or (no key) every debit reserved since it was recorded."""
        with self.tx() as db:
            if key:
                db.execute("DELETE FROM ambiguous_tx WHERE tx_key=?", (key,))
            else:
                db.execute(
                    "DELETE FROM ambiguous_tx WHERE tx_key IN (SELECT tx_key FROM settled_tx) "
                    "OR tx_key IN (SELECT paid_tx FROM efactura WHERE paid_tx IS NOT NULL)"
                )

    def ambiguous(self, company: str | None = None, key: str | None = None):
        q, args = "SELECT * FROM ambiguous_tx WHERE 1=1", []
        for col, val in (("company", company), ("tx_key", key)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        return self.db.execute(q + " ORDER BY company, json_extract(tx, '$.date'), tx_key", args).fetchall()

    def mark_skipped(self, item_id: str, reason: str) -> bool:
        """Taking a payment out of an open file's bytes invalidates its downloads."""
        if not reason.strip():
            raise ValueError("a reason is required")
        with self.tx() as db:
            row = db.execute(
                "UPDATE payments SET status='skipped',paid_at=?,paid_note=? WHERE item_id=? "
                "AND status IN ('pending','exported') RETURNING file_id",
                (now(), audit.note(reason.strip()), item_id),
            ).fetchone()
            if row and row["file_id"]:
                db.execute(
                    "UPDATE payment_files SET revision=revision+1 WHERE id=? AND imported_at IS NULL", (row["file_id"],)
                )
            return row is not None

    def requeue_file_payment(self, item_id: str, check_file: Callable[[int], None]) -> bool:
        """Reopening a user's mark never adds a payment to a file already closed at the bank."""
        with self.tx() as db:
            payment = db.execute("SELECT file_id FROM payments WHERE item_id=?", (item_id,)).fetchone()
            if payment and payment["file_id"]:
                check_file(payment["file_id"])
            row = db.execute(
                "UPDATE payments SET status='pending',csv_path=NULL,exported_at=NULL,paid_at=NULL,paid_note=NULL "
                "WHERE item_id=? AND status='skipped' AND (file_id IS NULL OR EXISTS "
                "(SELECT 1 FROM payment_files WHERE id=payments.file_id AND imported_at IS NULL)) RETURNING file_id",
                (item_id,),
            ).fetchone()
            if row and row["file_id"]:
                db.execute(
                    "UPDATE payment_files SET revision=revision+1 WHERE id=? AND imported_at IS NULL", (row["file_id"],)
                )
            return row is not None

    # ---- e-Factura (received supplier invoices)
    def efactura(self, message_id: str):
        return self.db.execute("SELECT * FROM efactura WHERE message_id=?", (message_id,)).fetchone()

    def add_efactura(self, row: dict) -> bool:
        if self.efactura(row["message_id"]):
            return False
        row = row | {"created_at": now()}
        if row.get("status") == "paid":
            row.setdefault("paid_at", now())
        cols = ",".join(row)
        with self.tx() as db:
            db.execute(f"INSERT INTO efactura({cols}) VALUES({','.join('?' * len(row))})", list(row.values()))
        return True

    def efactura_rows(self, status: str | tuple[str, ...] | None = None, company: str | None = None):
        q, args = "SELECT * FROM efactura WHERE 1=1", []
        if status:
            st = (status,) if isinstance(status, str) else status
            q += f" AND status IN ({','.join('?' * len(st))})"
            args += list(st)
        if company:
            q += " AND company=?"
            args.append(company)
        return self.db.execute(q + " ORDER BY company, due_date, issue_date, message_id", args).fetchall()

    def mark_efactura_paid(
        self,
        message_id: str,
        note: str,
        tx: str | None = None,
        *,
        to_iban: str = "",
        how: tuple[str, str] | None = None,
    ) -> bool:
        """Open or skipped -> paid; a row the user marked paid (still unconfirmed) takes the proof (a debit or a mail),
        never a second the user mark; a row a mail proved (MAIL_PROVED) takes its statement debit. `how`: the person's
        (paid_method, paid_detail), checked by efactura.paid_method; a fact kept on the row, never proof. A proof
        closing a marked row keeps how the person said they paid."""
        note = audit.note(note)
        method, detail = how or (None, None)
        with self.tx() as db:
            if not self._card_proof("efactura", message_id, tx):
                return False
            if not self._reserve(db, tx, f"efactura {message_id}"):
                return False
            cur = db.execute(
                "UPDATE efactura SET status='paid', paid_at=?, paid_note=?, paid_tx=?, "
                "paid_method=COALESCE(?,paid_method), paid_detail=COALESCE(?,paid_detail) WHERE message_id=? "
                f"AND (status IN ('open','skipped') OR ({USER_MARKED} AND ? NOT LIKE '{USER}%') OR ({MAIL_PROVED} "
                "AND EXISTS (SELECT 1 FROM statement_txs t WHERE t.tx_key=? AND t.company=efactura.company "
                "AND t.direction='debit'))) "  # only this firm's recorded debit replaces a mail
                f"AND (? IS NULL OR {unlinked('efactura')})",  # a proof never closes one paid in part
                (now(), note, tx, method, detail, message_id, note, tx, tx),
            )
            if cur.rowcount and tx and not tx.startswith(("bon:", "ef-proof:")) and to_iban:
                db.execute(
                    "INSERT OR IGNORE INTO supplier_ibans SELECT company,supplier_cui,supplier_iban,? "
                    "FROM efactura WHERE message_id=? AND supplier_cui!='' AND supplier_iban=? AND ibans=?",
                    ("statement:" + tx, message_id, to_iban, json.dumps([to_iban])),
                )
            self._release_unless(db, tx, cur.rowcount)
        return cur.rowcount > 0

    def _withdraw_pending_invoice_payment(self, db, row) -> bool:
        """Withdraw only a never-exported pending row; journalled file moves stay intact."""
        removed = db.execute(
            "DELETE FROM payments WHERE item_id=? AND company=? AND status='pending' "
            "AND csv_path IS NULL AND exported_at IS NULL",
            (row["item_id"], row["company"]),
        ).rowcount
        if removed:
            db.execute(
                "UPDATE efactura SET status='open',item_id=NULL,paid_note=NULL WHERE message_id=? AND status='linked'",
                (row["message_id"],),
            )
        return bool(removed)

    def link_bon(self, sha: str, message_id: str, note: str) -> bool:
        """The bon `sha` proves this invoice paid at the till: both sides are written in one transaction, or neither.
        A bon pays one invoice, ever (its key `bon:<sha>` is reserved like a debit), and an invoice takes one proof,
        so two connections linking at once never double-link."""
        note = audit.note(note)
        with self.tx() as db:
            if not self._reserve(db, f"bon:{sha}", f"efactura {message_id}"):
                return False
            cur = db.execute(
                "UPDATE efactura SET status='paid', paid_at=?, paid_note=?, paid_tx=? WHERE message_id=? "
                f"AND (status='open' OR ({USER_MARKED})) AND {unlinked('efactura')} "
                "AND NOT EXISTS(SELECT 1 FROM documents WHERE efactura_id=?)",
                (now(), note, f"bon:{sha}", message_id, message_id),
            )
            if cur.rowcount:
                cur = db.execute(
                    "UPDATE documents SET efactura_id=? WHERE sha=? AND kind='receipt' AND efactura_id IS NULL",
                    (message_id, sha),
                )
            if not cur.rowcount:
                db.rollback()
                return False
        return True

    def settle_bon_card(self, message_id: str, tx: str) -> bool:
        """A card debit paid a purchase whose invoice a bon already settled: the debit is used up, once per purchase,
        so it is never looked for again (nor taken by another invoice of the same shop and amount)."""
        what = f"card of bon, efactura {message_id}"
        with self.tx() as db:
            if not self._card_proof("efactura", message_id, tx):
                return False
            if db.execute("SELECT 1 FROM efactura WHERE paid_tx=?", (tx,)).fetchone():
                return False
            return (
                db.execute(
                    "INSERT OR IGNORE INTO settled_tx SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM settled_tx "
                    f"WHERE what=?) AND EXISTS (SELECT 1 FROM efactura WHERE message_id=? AND {BON_PAID})",
                    (tx, what, now(), what, message_id),
                ).rowcount
                == 1
            )

    def mark_efactura_skipped(self, message_id: str, reason: str) -> bool:
        if not reason.strip():
            raise ValueError("a reason is required")
        with self.tx() as db:
            cur = db.execute(
                "UPDATE efactura SET status='skipped', paid_at=?, paid_note=? WHERE message_id=? AND status='open'",
                (now(), reason.strip(), message_id),
            )
        return cur.rowcount > 0

    def queue_efactura(self, item, message_id: str, origin: str) -> bool | None:
        """A person ('person') or the autopay rule ('auto') approved this invoice: link it and add its payment-file row,
        recording who, in one transaction. The link is guarded by the
        invoice still being open, so an invoice settled meanwhile (by a bon, a debit, a mark) never gets a payment row.
        Returns whether the row was new, or None when the invoice is no longer open (nothing written). An invoice the
        user's links paid in part never gets one: the row would pay its whole total again."""
        with self.tx(immediate=True) as db:
            from fiscalm.efactura.inbox import payment_item

            if origin == "auto" and self.get_meta(f"payment_file_removed:{message_id}"):
                return None
            row = db.execute("SELECT status,total FROM efactura WHERE message_id=?", (message_id,)).fetchone()
            if row and origin == "auto" and Decimal(item.amount) != Decimal(row["total"]):
                return None  # autopay never queues a credited invoice's net (efactura.approve), re-checked here
            if (
                not row
                or row["status"] != "open"
                or db.execute("SELECT 1 FROM tx_link_items WHERE item=?", ("efactura " + message_id,)).fetchone()
            ):
                return None
            # Re-check under the write lock (account trust, revocations, a credit note since): the row it would build
            # now must be the item, its account and net amount included; otherwise nothing is written.
            if payment_item(self, message_id, account=item.payee_iban).item_id != item.item_id:
                return None
            if not db.execute(
                "UPDATE efactura SET status='linked', item_id=?, paid_note=? WHERE message_id=? AND status='open'",
                (item.item_id, f"added to the payment file by {AUTHORS[origin]}", message_id),
            ).rowcount:
                return None
            if origin == "person":
                self.set_meta(f"payment_file_removed:{message_id}", "")
            return self._insert_payment(db, item, "pending", "", origin)

    def add_product(self, co: str, ident: str, row: dict) -> None:
        self.db.execute(
            """INSERT INTO products(company,id,kind,name,code,unit,unit_price,currency,vat_rate,vat_category,exemption,description)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                co,
                ident,
                *(
                    row[k]
                    for k in (
                        "kind",
                        "name",
                        "code",
                        "unit",
                        "unit_price",
                        "currency",
                        "vat_rate",
                        "vat_category",
                        "exemption",
                        "description",
                    )
                ),
            ),
        )

    def update_product(self, co: str, ident: str, version: int, row: dict, archived: bool) -> bool:
        return bool(
            self.db.execute(
                """UPDATE products SET kind=?,name=?,code=?,unit=?,unit_price=?,currency=?,vat_rate=?,vat_category=?,exemption=?,description=?,
            archived=?,version=version+1 WHERE company=? AND id=? AND version=?""",
                (
                    *(
                        row[k]
                        for k in (
                            "kind",
                            "name",
                            "code",
                            "unit",
                            "unit_price",
                            "currency",
                            "vat_rate",
                            "vat_category",
                            "exemption",
                            "description",
                        )
                    ),
                    int(archived),
                    co,
                    ident,
                    version,
                ),
            ).rowcount
        )

    def delete_product(self, co: str, ident: str, version: int) -> bool:
        return bool(
            self.db.execute(
                """DELETE FROM products WHERE company=? AND id=? AND version=? AND NOT EXISTS (
            SELECT 1 FROM product_references WHERE company=? AND product_id=?)""",
                (co, ident, version, co, ident),
            ).rowcount
        )

    def reference_invoice_products(
        self,
        co: str,
        iid: str,
        product_ids: set[str],
        *,
        recurrence_id: str | None = None,
        client_id: str | None = None,
    ) -> bool:
        """Retain archived links only from this draft or its trusted recurring template."""
        if not product_ids:
            return True
        ids = sorted(product_ids)
        eligible = f"""SELECT p.company,p.id,? AS invoice_id FROM products p
            JOIN invoices i ON i.id=? AND i.company=p.company AND i.status='draft'
            WHERE p.company=? AND p.id IN ({",".join("?" for _ in ids)}) AND (
                p.archived=0 OR EXISTS (SELECT 1 FROM invoice_lines l
                    WHERE l.invoice_id=i.id AND json_extract(l.data,'$.product_id')=p.id)
                OR EXISTS (SELECT 1 FROM invoice_recurrences r, json_each(r.template,'$.lines') l
                    WHERE r.id=? AND r.company=p.company AND r.client_id=? AND r.series=i.series AND r.paused=0
                    AND json_extract(l.value,'$.product_id')=p.id))"""
        args = (iid, iid, co, *ids, recurrence_id, client_id)
        with self.tx(immediate=True) as db:
            if {r["id"] for r in db.execute(eligible, args)} != product_ids:
                return False
            # Repeat the eligibility SELECT in the writer, under the same lock.
            db.execute(
                "INSERT OR IGNORE INTO product_references(company,product_id,invoice_id) " + eligible,
                args,
            )
        return True

    def replace_invoice_draft(self, iid: str, company: str, series: str, data: str, version: int) -> bool:
        """Replace only the draft version the caller saw, within the caller's transaction."""
        return bool(
            self.db.execute(
                "UPDATE invoices SET series=?,data=?,version=version+1 WHERE id=? AND company=? AND status='draft' AND version=?",
                (series, data, iid, company, version),
            ).rowcount
        )

    def delete_invoice_draft(self, iid: str, company: str, version: int | None) -> bool:
        """Delete only an unnumbered draft, optionally matching the caller's version."""
        return bool(
            self.db.execute(
                "DELETE FROM invoices WHERE id=? AND company=? AND status='draft' AND (? IS NULL OR version=?)",
                (iid, company, version, version),
            ).rowcount
        )

    def issue_invoice_draft(self, iid: str, company: str, number: int, data: str, version: int) -> bool:
        """Freeze only the validated draft version, within the caller's transaction."""
        return bool(
            self.db.execute(
                "UPDATE invoices SET status='issued',number=?,data=? WHERE id=? AND company=? AND status='draft' AND version=?",
                (number, data, iid, company, version),
            ).rowcount
        )

    # ---- issued invoices (outgoing, e-Factura sent messages)
    def reopen_history_twin(self, company: str, way: str, mid: str, sha: str) -> None:
        table = {"issued": "issued", "received": "efactura"}[way]
        with self.tx() as db:
            if (
                db.execute(
                    f"UPDATE {table} SET status='open' WHERE company=? AND message_id=? AND xml_sha=? AND status='duplicate'",
                    (company, mid, sha),
                ).rowcount
                != 1
            ):
                raise ValueError("Invoice changed; nothing reopened.")

    def archive_history_twin(self, company: str, way: str, mid: str) -> bool:
        """Owner-requested reclassification only; preserve every row and payment proof."""
        table = {"issued": "issued", "received": "efactura"}[way]
        proof = (
            "collected_tx IS NULL AND collected_at IS NULL AND collected_note IS NULL"
            if way == "issued"
            else "paid_tx IS NULL AND paid_at IS NULL AND COALESCE(paid_note,'')='' AND item_id IS NULL"
        )
        references = (
            "NOT EXISTS (SELECT 1 FROM settlement_lines WHERE invoice_id=?) AND NOT EXISTS (SELECT 1 FROM invoice_sent_links WHERE message_id=?)"
            if way == "issued"
            else "NOT EXISTS (SELECT 1 FROM documents WHERE efactura_id=?)"
        )
        with self.tx() as db:
            return (
                db.execute(
                    f"UPDATE {table} SET status='duplicate' WHERE company=? AND message_id=? AND status='open' "
                    f"AND {proof} AND {references} AND NOT EXISTS (SELECT 1 FROM tx_link_items WHERE item=?) "
                    f"AND EXISTS (SELECT 1 FROM history h WHERE h.company={table}.company AND h.direction=? "
                    f"AND h.xml_sha={table}.xml_sha)",
                    (company, mid, *((mid, mid) if way == "issued" else (mid,)), f"{table} {mid}", way),
                ).rowcount
                == 1
            )

    def issued(self, message_id: str):
        return self.db.execute("SELECT * FROM issued WHERE message_id=?", (message_id,)).fetchone()

    def add_issued(self, row: dict) -> bool:
        if self.issued(row["message_id"]):
            return False
        row = row | {"created_at": now()}
        cols = ",".join(row)
        with self.tx() as db:
            db.execute(f"INSERT INTO issued({cols}) VALUES({','.join('?' * len(row))})", list(row.values()))
        return True

    def issued_rows(self, status: str | tuple[str, ...] | None = None, company: str | None = None):
        # collected_on: the booking day of the statement credit that collected it (collected_at is when it was recorded)
        q, args = (
            "SELECT i.*, (SELECT day FROM statement_txs t WHERE t.tx_key=i.collected_tx AND t.company=i.company) "
            "AS collected_on FROM issued i WHERE 1=1",
            [],
        )
        if status:
            st = (status,) if isinstance(status, str) else status
            q += f" AND status IN ({','.join('?' * len(st))})"
            args += list(st)
        if company:
            q += " AND company=?"
            args.append(company)
        return self.db.execute(q + " ORDER BY company, issue_date, number, message_id", args).fetchall()

    def withdraw_report(self, report_id: str, company: str) -> bool:
        """Withdraw pending evidence only; preserve the bytes and never undo a collection."""
        with self.tx() as db:
            changed = bool(
                db.execute(
                    "UPDATE settlement_reports SET status='withdrawn',error=NULL "
                    "WHERE id=? AND company=? AND status='pending' AND tx_key IS NULL",
                    (report_id, company),
                ).rowcount
            )
            if changed:
                db.execute("DELETE FROM settlement_credits WHERE report_id=?", (report_id,))
            return changed

    def settle_report(
        self,
        report_id: str,
        statement,
        *,
        confirmed_by: str | None = None,
        expected_tx: str | None = None,
        _confirmation: object = None,
    ) -> list[str]:
        """Validate under the write lock, reserve one credit and collect the complete report or roll back."""
        from fiscalm.settlements import engine as settlements

        with self.tx() as db:
            report = db.execute("SELECT * FROM settlement_reports WHERE id=?", (report_id,)).fetchone()
            if not report or report["status"] != "pending":
                raise settlements.SettlementError("report_unavailable")
            from fiscalm.settlements.engine import BankEvidence, evidence_versions

            if confirmed_by is None and not isinstance(statement, BankEvidence):
                raise settlements.SettlementError("statement_unavailable")
            if isinstance(statement, BankEvidence) and statement.versions != tuple(
                evidence_versions(self, report["company"], set(statement.months))
            ):
                raise settlements.SettlementError("statement_changed")
            credit, invoices, fee, currency = settlements.verify(self, report, statement)
            key = settlements.statements.tx_key(credit)
            if not self.card_allows(report["company"], credit.card_last4):
                raise settlements.SettlementError("credit_unavailable")
            data = json.loads(report["data"])
            payer_id = data["payer_id"]
            legacy = getattr(statement, "legacy_proofs", {}).get(key)
            if legacy and self.get_meta("statement_stored_sha:" + legacy[0]) not in (None, legacy[1]):
                raise settlements.SettlementError("statement_changed")
            if confirmed_by is not None:
                from fiscalm.settlements.engine import _HUMAN_CONFIRMATION

                if _confirmation is not _HUMAN_CONFIRMATION or not confirmed_by.strip() or expected_tx != key:
                    raise settlements.SettlementError("confirmation_refused")
            elif legacy or data.get("mailed_by"):
                # A person confirms an unpinned legacy PDF copy, and a report that came by mail (a From header is no
                # proof of who sent it); every other verified batch settles on its own.
                preview = json.dumps(
                    {
                        "id": key,
                        "date": credit.date.isoformat(),
                        "amount": f"{credit.amount:.2f}",
                        "currency": credit.currency,
                    }
                )
                db.execute(
                    "INSERT INTO settlement_credits VALUES(?,?,?) ON CONFLICT(report_id) DO UPDATE SET "
                    "credit=excluded.credit,invoice_count=excluded.invoice_count WHERE credit!=excluded.credit OR invoice_count!=excluded.invoice_count",
                    (report_id, preview, len(invoices)),
                )
                # Mail first: its sheet offers withdrawal beside the confirmation; a legacy copy is pinned on confirm.
                reason = "mail_confirmation_required" if data.get("mailed_by") else "statement_confirmation_required"
                db.execute(
                    "UPDATE settlement_reports SET error=? WHERE id=? AND status='pending' AND error IS NOT ?",
                    (reason, report_id, reason),
                )
                return []
            if not self._reserve(db, key, f"settlement {report_id}"):
                raise settlements.SettlementError("credit_used")
            for position, mid in enumerate(invoices, 1):
                from fiscalm.companies import normalize_iban

                if not db.execute(
                    "UPDATE issued SET status='collected',collected_at=?,collected_note=?,collected_tx=?,"
                    "collected_into_iban=? "
                    f"WHERE message_id=? AND company=? AND status='open' AND parse_error IS NULL AND {unlinked('issued')}",
                    (
                        now(),
                        f"settlement {report_id}",
                        key,
                        normalize_iban(credit.iban) or None,
                        mid,
                        report["company"],
                    ),
                ).rowcount:
                    raise settlements.SettlementError("invoice_changed")
                db.execute("INSERT INTO settlement_lines VALUES(?,?,?)", (report_id, position, mid))
            db.execute(
                "INSERT INTO settlement_fees(report_id,company,amount,currency) VALUES(?,?,?,?)",
                (report_id, report["company"], f"{fee:.2f}", currency),
            )
            if not db.execute(
                "UPDATE settlement_reports SET status='collected',tx_key=?,error=NULL WHERE id=? AND status='pending'",
                (key, report_id),
            ).rowcount:
                raise settlements.SettlementError("report_changed")
            preview = json.dumps(
                {
                    "id": key,
                    "date": credit.date.isoformat(),
                    "amount": f"{credit.amount:.2f}",
                    "currency": credit.currency,
                }
            )
            db.execute(
                "INSERT INTO settlement_credits VALUES(?,?,?) ON CONFLICT(report_id) DO UPDATE SET "
                "credit=excluded.credit,invoice_count=excluded.invoice_count",
                (report_id, preview, len(invoices)),
            )
            if confirmed_by is not None:
                if legacy:
                    self.set_meta("statement_stored_sha:" + legacy[0], legacy[1])
                db.execute(
                    "INSERT INTO settlement_confirmations VALUES(?,?,?,?,?)",
                    (report_id, report["company"], payer_id, confirmed_by, now()),
                )
            from fiscalm.commerce.webhooks import capture

            for mid in invoices:
                capture(self, message_id=mid)
            return invoices

    def mark_issued_collected(
        self, message_id: str | tuple[str, ...], note: str, tx: str, from_iban: str = "", into_iban: str = ""
    ) -> bool:
        """A statement credit collected this invoice, or (a tuple) exactly this set of one firm's invoices: all of
        them or none. `tx` (statements.tx_key) can never settle anything else. `into_iban`: the company account the
        credit landed in."""
        mids = (message_id,) if isinstance(message_id, str) else tuple(message_id)
        note = audit.note(note)
        try:
            with self.tx() as db:
                firms = db.execute(
                    f"SELECT count(DISTINCT company) FROM issued WHERE message_id IN ({','.join('?' * len(mids))})",
                    mids,
                ).fetchone()[0]
                if not mids or len(set(mids)) != len(mids) or firms != 1:
                    return False
                if not all(self._card_proof("issued", m, tx) for m in mids):
                    return False
                states = "('open','marked')" if len(mids) == 1 else "('open')"  # a set confirms no one's mark
                if not self._reserve(db, tx, "issued " + " ".join(mids)):
                    return False
                for m in mids:
                    if not db.execute(
                        "UPDATE issued SET status='collected', collected_at=?, collected_note=?, collected_tx=?, "
                        "collected_from_iban=?, collected_into_iban=? WHERE message_id=? "
                        f"AND status IN {states} "
                        f"AND parse_error IS NULL AND {unlinked('issued')}",
                        (now(), note, tx, from_iban or None, into_iban or None, m),
                    ).rowcount:
                        raise _SetChanged  # rolls back the reservation and every invoice before it
                from fiscalm.commerce.webhooks import capture

                for m in mids:
                    capture(self, message_id=m)
        except _SetChanged:
            return False
        return True

    def mark_issued_manual(self, message_id: str, note: str, method: str) -> bool:
        """The user: collected by `method` (issued.COLLECTED_METHODS) with no statement credit to point to yet."""
        if not note.strip():
            raise ValueError("a note is required")
        note = audit.note(note)
        with self.tx() as db:
            cur = db.execute(
                "UPDATE issued SET status='marked', collected_at=?, collected_note=?, collected_method=? WHERE message_id=? "
                "AND status='open' AND parse_error IS NULL",
                (now(), note.strip(), method, message_id),
            )
        return cur.rowcount > 0

    def unmark_issued(self, message_id: str) -> bool:
        """Undo a manual mark only; a collection proved by a statement credit stays."""
        with self.tx() as db:
            cur = db.execute(
                "UPDATE issued SET status='open', collected_at=NULL, collected_note=NULL, collected_method=NULL "
                "WHERE message_id=? "
                "AND status='marked' AND collected_tx IS NULL",
                (message_id,),
            )
        return cur.rowcount > 0

    # ---- the user's explanations of statement transactions (fiscalm.links)
    def link_rows(self, company: str) -> tuple[list, list]:
        """The company's explanations (tx_links) and what they cover (tx_link_items: tx_key, item, amount, closed), oldest
        first, in one read."""
        links = self.db.execute(
            "SELECT l.*, (SELECT json_group_array(json_object('tx_key',i.tx_key,'item',i.item,'amount',i.amount,"
            "'closed',i.closed)) FROM (SELECT * FROM tx_link_items WHERE tx_key=l.tx_key ORDER BY rowid) i) AS covers, "
            f"{ACTOR_NAME.format('l.by')} FROM tx_links l WHERE l.company=? ORDER BY l.at, l.rowid",
            (company,),
        ).fetchall()
        return links, [it for ln in links for it in json.loads(ln["covers"])]

    def link_tx(
        self,
        company: str,
        tx_key: str,
        was: str,
        build: Callable[[], dict],
        need: Callable[[str], object],
        by: str | None,
        rule: str | None = None,
    ) -> str:
        """The user's explanation of one statement transaction of `company`, written in one transaction under the
        write lock, or nothing. `was` is the explanation the user saw ("" for none): another one since refuses. The
        transaction is reserved in settled_tx as 'link', so no matcher, settlement or other explanation takes it; a
        second explanation of it replaces the first (its items reopen first), never adds to it. `build` runs after
        that, under the lock, and returns {kind, partner, reason, difference, note, items: [(item, amount)]}, so what
        is left of each item is read in the same transaction that settles it. An item is closed once the parts
        every explanation settles reach `need(item)` (links.need). An item 'tx <key>' is the other end of a transfer
        between the firm's accounts: that transaction is reserved by this explanation too, or nothing is written.
        `rule` names an explanation no person made (links.apply_rules). Returns the new explanation's id."""
        with self.tx() as db:
            if not db.execute("SELECT 1 FROM statement_txs WHERE tx_key=? AND company=?", (tx_key, company)).fetchone():
                raise LinkRefused("unknown")
            cur = db.execute("SELECT * FROM tx_links WHERE tx_key=?", (tx_key,)).fetchone()
            if (cur["id"] if cur else "") != was:
                raise LinkRefused("changed")
            if cur:
                self._drop_link(db, cur, need)
            elif not self._reserve(db, tx_key, "link"):
                raise LinkRefused("used")
            plan = build()
            ident = uuid4().hex[:16]
            db.execute(
                "INSERT INTO tx_links(tx_key,id,company,kind,partner,reason,difference,note,by,at,rule) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tx_key,
                    ident,
                    company,
                    plan["kind"],
                    plan.get("partner"),
                    plan.get("reason"),
                    f"{plan['difference']:.2f}",
                    plan.get("note"),
                    by,
                    datetime.now().isoformat(timespec="microseconds"),
                    rule,
                ),
            )
            for item, amount in plan["items"]:
                db.execute(
                    "INSERT INTO tx_link_items(tx_key,item,amount) VALUES(?,?,?)", (tx_key, item, f"{amount:.2f}")
                )
            for item, _ in plan["items"]:
                if item.startswith("tx "):
                    if not self._reserve(db, item[3:], "link"):
                        raise LinkRefused("used")
                else:
                    self._settle_item(db, company, item, need(item))
            return ident

    def unlink_tx(self, company: str, tx_key: str, was: str, need: Callable[[str], object]) -> list[str]:
        """Undo the explanation `was` of a transaction: every item it closed is put back as it was (an invoice the user
        had marked paid is marked again, not opened), an item another explanation closed reopens when what is left
        no longer covers it, and the transaction is free again for the matchers. Returns the items it covered."""
        with self.tx() as db:
            cur = db.execute("SELECT * FROM tx_links WHERE tx_key=? AND company=?", (tx_key, company)).fetchone()
            if not cur or cur["id"] != was:
                raise LinkRefused("changed")
            items = self._drop_link(db, cur, need)
            if not db.execute("DELETE FROM settled_tx WHERE tx_key=? AND what='link'", (tx_key,)).rowcount:
                raise LinkRefused("changed")
            return items

    def remember(self, company: str, key_kind: str, key: str, link: dict, by: str | None) -> str:
        """Keep what an explanation said for this account, card or merchant, replacing what was kept for it before."""
        ident = uuid4().hex[:16]
        with self.tx() as db:
            db.execute(
                "INSERT INTO tx_memory(id,company,key_kind,key,kind,partner,reason,note,by,at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(company,key_kind,key) DO UPDATE SET id=excluded.id, kind=excluded.kind, "
                "partner=excluded.partner, reason=excluded.reason, note=excluded.note, by=excluded.by, at=excluded.at",
                (
                    ident,
                    company,
                    key_kind,
                    key,
                    link["kind"],
                    link["partner"],
                    link["reason"],
                    link["note"] or None,
                    by,
                    now(),
                ),
            )
        return ident

    def forget(self, company: str, ident: str) -> bool:
        with self.tx() as db:
            return db.execute("DELETE FROM tx_memory WHERE id=? AND company=?", (ident, company)).rowcount > 0

    def memories(self, company: str) -> list:
        return self.db.execute(
            f"SELECT m.*, {ACTOR_NAME.format('m.by')} FROM tx_memory m WHERE company=? ORDER BY at DESC, rowid DESC",
            (company,),
        ).fetchall()

    def rules_off(self, company: str) -> set[str]:
        """The transactions whose explanation a person removed or replaced: no rule explains them again."""
        return {r[0] for r in self.db.execute("SELECT tx_key FROM tx_rules_off WHERE company=?", (company,))}

    def _drop_link(self, db, link, need: Callable[[str], object]) -> list[str]:
        # only a person drops an explanation (a replacement or Anulează legătura): rules leave the line, and a
        # transfer's other end, to them
        items = db.execute("SELECT * FROM tx_link_items WHERE tx_key=?", (link["tx_key"],)).fetchall()
        for key in [link["tx_key"], *(it["item"][3:] for it in items if it["item"].startswith("tx "))]:
            db.execute("INSERT OR IGNORE INTO tx_rules_off VALUES(?,?)", (key, link["company"]))
        for it in items:
            if it["closed"]:
                self._reopen_item(db, link["company"], it)
        db.execute("DELETE FROM tx_link_items WHERE tx_key=?", (link["tx_key"],))
        db.execute("DELETE FROM tx_links WHERE tx_key=?", (link["tx_key"],))
        for it in items:
            if it["item"].startswith("tx "):  # a transfer's other end is free again too
                db.execute("DELETE FROM settled_tx WHERE tx_key=? AND what='link'", (it["item"][3:],))
            else:
                self._settle_item(db, link["company"], it["item"], need(it["item"]))
        return [it["item"] for it in items]

    def _settle_item(self, db, company: str, item: str, need) -> None:
        """An item is closed exactly while the parts its explanations settle reach `need`: by the newest of them
        when it gets there, reopened (by the one that closed it) when an undo leaves less."""
        rows = db.execute(
            "SELECT i.* FROM tx_link_items i JOIN tx_links l USING(tx_key) WHERE i.item=? ORDER BY l.at, l.rowid",
            (item,),
        ).fetchall()
        covered = sum((Decimal(r["amount"]) for r in rows), Decimal(0))
        closer = next((r for r in rows if r["closed"]), None)
        if rows and not closer and covered >= need:
            self._close_item(db, company, item, rows[-1]["tx_key"])
        elif closer and covered < need:
            self._reopen_item(db, company, closer)
            db.execute(
                "UPDATE tx_link_items SET closed=0, prior=NULL WHERE tx_key=? AND item=?", (closer["tx_key"], item)
            )

    _ITEM_COLUMNS = {
        "issued": (
            "issued",
            "message_id",
            ("status", "collected_at", "collected_note", "collected_tx", "collected_into_iban"),
        ),
        "efactura": ("efactura", "message_id", ("status", "paid_at", "paid_note", "paid_tx")),
        # a payment's file too: paying it can archive its rolling file, and an undo puts it back there
        "payment": ("payments", "item_id", ("status", "paid_at", "paid_note", "csv_path")),
        # a bon the user named for a card debit takes the debit's month, day, amount and card (links.receipt)
        "receipt": ("documents", "sha", ("month", "doc_date", "amount", "currency", "card_last4")),
    }

    def _close_item(self, db, company: str, item: str, tx_key: str) -> None:
        """Settle `item` with the statement transaction `tx_key`, guarded by its state still being one an explanation
        may close (re-checked here, in the write): an issued invoice open or marked, a received invoice open or
        marked paid by the user (the transaction is the proof it waited for), a payment row not paid yet and, when its
        payment file named the account it debits, debited from that account."""
        kind, _, ident = item.partition(" ")
        table, key, cols = self._ITEM_COLUMNS[kind]
        row = db.execute(
            f"SELECT {','.join(cols)} FROM {table} WHERE {key}=? AND company=?", (ident, company)
        ).fetchone()
        note = audit.note(f"statement {tx_key}, linked by the user")
        if kind == "issued":
            cur = db.execute(
                "UPDATE issued SET status='collected', collected_at=?, collected_note=?, collected_tx=?, "
                "collected_into_iban=(SELECT iban FROM statement_txs WHERE tx_key=?) WHERE message_id=? "
                "AND company=? AND status IN ('open','marked') AND parse_error IS NULL AND doc_type='invoice'",
                (now(), note, tx_key, tx_key, ident, company),
            )
        elif kind == "efactura":
            cur = db.execute(
                "UPDATE efactura SET status='paid', paid_at=?, paid_note=?, paid_tx=? WHERE message_id=? "
                f"AND company=? AND (status='open' OR ({USER_MARKED})) AND parse_error IS NULL",
                (now(), note, tx_key, ident, company),
            )
        elif kind == "receipt":
            t = json.loads(db.execute("SELECT tx FROM statement_txs WHERE tx_key=?", (tx_key,)).fetchone()["tx"])
            # only an upload nothing has dated, priced, sent or tied to an invoice yet: no other explanation is moved
            cur = db.execute(
                "UPDATE documents SET month=?, doc_date=?, amount=?, currency=?, card_last4=? WHERE sha=? AND company=? "
                "AND kind='receipt' AND source LIKE 'upload:%' AND amount IS NULL AND efactura_id IS NULL AND sent_at IS NULL",
                (
                    t["date"][:7],
                    t["date"],
                    t.get("orig_amount") or t["amount"],
                    t.get("orig_currency") or t["currency"],
                    t.get("card_last4") or None,
                    ident,
                    company,
                ),
            )
        else:
            if self._net_changed(db, ident):
                raise LinkRefused("item_changed")
            cur = db.execute(
                "UPDATE payments SET status='paid', paid_at=?, paid_note=? WHERE item_id=? AND company=? "
                "AND status IN ('pending','exported') AND (source_iban IS NULL OR source_iban="
                "(SELECT iban FROM statement_txs WHERE tx_key=?))",
                (now(), note, ident, company, tx_key),
            )
        if not row or not cur.rowcount:
            raise LinkRefused("item_changed")
        db.execute(
            "UPDATE tx_link_items SET closed=1, prior=? WHERE tx_key=? AND item=?",
            (json.dumps(dict(row)), tx_key, item),
        )

    def _reopen_item(self, db, company: str, it) -> None:
        """Put back the item `it` closed, as it was before, only while it is still closed by that transaction."""
        kind, _, ident = it["item"].partition(" ")
        table, key, cols = self._ITEM_COLUMNS[kind]
        prior = json.loads(it["prior"])
        # payments carry no transaction column: the note this close wrote is the guard
        settled, guard = {
            "issued": ("status='collected' AND collected_tx=?", it["tx_key"]),
            "efactura": ("status='paid' AND paid_tx=?", it["tx_key"]),
            "payment": ("status='paid' AND paid_note LIKE ?", f"statement {it['tx_key']}, linked by the user%"),
            # still this explanation's bon, and not sent: a bon the accountant has stays as sent
            "receipt": (
                "sent_at IS NULL AND 'receipt '||sha IN (SELECT item FROM tx_link_items WHERE tx_key=?)",
                it["tx_key"],
            ),
        }[kind]
        cur = db.execute(
            f"UPDATE {table} SET {','.join(c + '=?' for c in cols)} WHERE {key}=? AND company=? AND {settled}",
            (*(prior[c] for c in cols), ident, company, guard),
        )
        if not cur.rowcount:
            raise LinkRefused("item_changed")

    # ---- documents
    def correct_document(
        self, co: str, sha: str, amount: str, currency: str, doc_date: str, number: str, by: str
    ) -> None:
        """Serialize corrections against proof writers and pack confirmations; retain every previous answer."""
        with self.tx(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM documents WHERE sha=? AND company=? AND kind='receipt'", (sha, co)
            ).fetchone()
            if not row:
                raise ValueError("Documentul nu mai există. Reîncarcă pagina.")
            if (row["source"] or "").startswith("upload:"):
                raise ValueError("Asociază documentul cu plata din Tranzacții pentru a completa suma.")
            mail = db.execute("SELECT number FROM mail_documents WHERE sha=?", (sha,)).fetchone()
            key = "document-corrections:" + sha
            history = json.loads(self.get_meta(key) or "[]")
            previous = {k: row[k] for k in ("amount", "currency", "doc_date", "month")}
            previous["number"] = mail[0] if mail else history[-1]["value"]["number"] if history else ""
            if not db.execute(
                "UPDATE documents SET amount=?,currency=?,doc_date=COALESCE(NULLIF(?,''),doc_date),month=? "
                "WHERE sha=? AND company=? AND " + DOCUMENT_CORRECTABLE,
                (amount, currency, doc_date, doc_date[:7] if doc_date else row["month"], sha, co),
            ).rowcount:
                raise ValueError(
                    "Documentul confirmă o plată sau este într-un dosar trimis contabilului. Nu poate fi corectat."
                )
            value = {
                "amount": amount,
                "currency": currency,
                "doc_date": doc_date or row["doc_date"],
                "month": doc_date[:7] if doc_date else row["month"],
                "number": number or previous["number"],
            }
            history.append({"at": now(), "by": by, "previous": previous, "value": value})
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, json.dumps(history)))
            if number:
                db.execute("UPDATE mail_documents SET number=? WHERE sha=?", (number, sha))

    def set_document_note(self, sha: str, note: str) -> bool:
        note = audit.note(note)
        with self.tx() as db:
            return db.execute("UPDATE documents SET note=? WHERE sha=?", (note, sha)).rowcount > 0

    def assign_upload(self, sha: str, company: str, path: str) -> bool:
        """First assignment wins; the file has already been safely copied into the firm inbox."""
        with self.tx() as db:
            changed = db.execute(
                "UPDATE documents SET company=?,path=?,issues='[\"to-check\"]' WHERE sha=? AND company='ALL' AND source LIKE 'upload:%'",
                (company, path, sha),
            ).rowcount
            if changed:
                db.execute("UPDATE receipt_uploads SET company=? WHERE sha=? AND company='ALL'", (company, sha))
            return bool(changed)

    def document(self, sha: str):
        return self.db.execute("SELECT * FROM documents WHERE sha=?", (sha,)).fetchone()

    def statement_of_copy(self, digest: str) -> str | None:
        """The statement document whose stored copy (a PDF's decrypted copy) has these bytes, when exactly one has."""
        rows = self.db.execute(
            "SELECT d.sha FROM meta m JOIN documents d ON m.key='statement_stored_sha:'||d.sha "
            "WHERE m.value=? AND d.kind='statement'",
            (digest,),
        ).fetchall()
        return rows[0]["sha"] if len(rows) == 1 else None

    def add_document(self, sha: str, company: str, month: str, kind: str, path: str, **kw) -> bool:
        issues = json.dumps(kw.pop("issues", []) or [])
        with self.tx(immediate=True) as db:
            existing = self.document(sha)
            values = (
                company,
                month,
                kind,
                path,
                kw.get("vendor"),
                kw.get("doc_date"),
                kw.get("amount"),
                kw.get("currency"),
                kw.get("card_last4"),
                issues,
                kw.get("source"),
                now(),
            )
            accounts = json.dumps(sorted(kw["accounts"])) if kw.get("accounts") else None
            if existing:
                if not (existing["source"] or "").startswith("upload:") or (kw.get("source") or "").startswith(
                    "upload:"
                ):
                    return False
                # Trusted ingestion owns metadata and company; a receipt keeps its stored bytes, a statement points
                # at the copy its importer checked and stored in the firm's own storage (a PDF one decrypted). A bon a
                # person linked to its card payment (links.receipt) keeps that payment's data and source.
                if not db.execute(
                    """UPDATE documents SET company=?,month=?,kind=?,path=?,vendor=?,doc_date=?,
                    amount=?,currency=?,card_last4=?,issues=?,source=?,created_at=?,accounts=? WHERE sha=? AND source LIKE 'upload:%'
                    AND NOT EXISTS (SELECT 1 FROM tx_link_items WHERE item='receipt '||documents.sha)""",
                    values[:3] + (path if kind == "statement" else existing["path"],) + values[4:] + (accounts, sha),
                ).rowcount:
                    return False
                db.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    (
                        "upload-takeover:" + sha,
                        json.dumps({"at": now(), "company": company, "source": kw.get("source")}),
                    ),
                )
            else:
                db.execute(
                    """INSERT INTO documents
                    (company,month,kind,path,vendor,doc_date,amount,currency,card_last4,issues,source,created_at,accounts,sha)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values + (accounts, sha),
                )
        return True

    def linked_receipts(self, company: str, docs: list | None = None) -> set[str]:
        """The uploads the user named as a card debit's bon (links.receipt): each explains that debit and no other.
        Given the documents a caller is about to filter, it reads nothing when none of them is an upload."""
        if docs is not None and not any((d["source"] or "").startswith("upload:") for d in docs):
            return set()
        return {
            r[0]
            for r in self.db.execute(
                "SELECT substr(i.item, 9) FROM tx_link_items i JOIN tx_links l USING(tx_key) "
                "WHERE l.company=? AND i.item LIKE 'receipt %'",
                (company,),
            )
        }

    def documents(
        self,
        company: str | None = None,
        month: str | None = None,
        kind: str | None = None,
        unsent_only: bool = False,
        up_to_month: str | None = None,
    ):
        q, args = "SELECT * FROM documents WHERE 1=1", []
        for col, val in (("company", company), ("month", month), ("kind", kind)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        if up_to_month:
            q += " AND month<=?"
            args.append(up_to_month)
        if unsent_only:
            q += " AND sent_at IS NULL"
        return self.db.execute(q + " ORDER BY company, month, kind, doc_date", args).fetchall()

    def mark_documents_sent(self, shas: list[str], message_date: str) -> None:
        with self.tx() as db:
            for s in shas:
                db.execute(
                    "UPDATE documents SET sent_at=?, sent_message_date=? WHERE sha=? AND sent_at IS NULL",
                    (now(), message_date, s),
                )

    # ---- packs
    def pack(self, company: str, month: str):
        return self.db.execute("SELECT * FROM packs WHERE company=? AND month=?", (company, month)).fetchone()

    def set_pack(self, company: str, month: str, status: str, zip_path: str | None = None, note: str = "") -> None:
        note = audit.note(note)
        with self.tx() as db:
            db.execute(
                """INSERT INTO packs(company,month,status,zip_path,updated_at,note) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(company,month) DO UPDATE SET status=excluded.status,
                   zip_path=COALESCE(excluded.zip_path, packs.zip_path), updated_at=excluded.updated_at,
                   note=excluded.note""",
                (company, month, status, zip_path, now(), note),
            )

    def sent_months(self, company: str) -> set[str]:
        return {
            r["month"] for r in self.db.execute("SELECT month FROM packs WHERE company=? AND status='sent'", (company,))
        }
