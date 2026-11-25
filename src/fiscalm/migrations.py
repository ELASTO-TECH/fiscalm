"""Append-only product schema history. No first-install ledger conversions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from importlib.resources import files
from pathlib import Path


class SchemaError(RuntimeError):
    """Opening this ledger with this code is unsafe."""


def read_version(path: Path) -> int:
    """Read PRAGMA user_version without creating, changing or migrating a ledger."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return db.execute("PRAGMA user_version").fetchone()[0]
    finally:
        db.close()


def execute_sql(db: sqlite3.Connection, script: str) -> None:
    """Run DDL without executescript's implicit commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise SchemaError("Incomplete baseline SQL")


def baseline(db: sqlite3.Connection) -> None:
    execute_sql(db, files("fiscalm").joinpath("baseline.sql").read_text(encoding="utf-8"))


def transaction_links(db: sqlite3.Connection) -> None:
    execute_sql(
        db,
        """
CREATE TABLE tx_link_items (  -- what an explanation covers: 'issued <id>' | 'efactura <id>' | 'payment <id>'
  tx_key TEXT NOT NULL REFERENCES tx_links(tx_key), item TEXT NOT NULL,
  amount TEXT NOT NULL,                 -- the part of the item it settles
  closed INTEGER NOT NULL DEFAULT 0,    -- this explanation closed the item: `prior` is the item as it was, put back on undo
  prior TEXT,
  PRIMARY KEY (tx_key, item)
);
CREATE TABLE tx_links (  -- the user's explanation of one statement transaction (finrec.links): its
  tx_key TEXT PRIMARY KEY,              -- settled_tx reservation is 'link'; a second explanation replaces the first
  id TEXT NOT NULL UNIQUE,              -- this explanation (a new one gets a new id: a stale sheet can't replace it)
  company TEXT NOT NULL, kind TEXT NOT NULL,  -- documents; later a label, a transfer, an advance, a Decontare
  partner TEXT,                         -- partners[].id of the documents it covers, or null
  reason TEXT,                          -- what the difference is (links.REASONS), null when the amounts agree
  difference TEXT NOT NULL,             -- the transaction minus what its documents had left, signed, two decimals
  note TEXT, by TEXT, at TEXT NOT NULL  -- the user's words (reason 'other'), who decided it and when
);
CREATE TABLE tx_memory (  -- what the user asked to be remembered from an explanation (finrec.links): a
  id TEXT PRIMARY KEY, company TEXT NOT NULL,  -- counterparty's account, a card or a merchant's name, proposed again
  key_kind TEXT NOT NULL CHECK (key_kind IN ('iban','card','name')), key TEXT NOT NULL,  -- on its next transactions
  kind TEXT NOT NULL, partner TEXT, reason TEXT, note TEXT, by TEXT, at TEXT NOT NULL,
  UNIQUE (company, key_kind, key)
);
CREATE INDEX tx_link_item ON tx_link_items(item);
""",
    )


def mail_documents_and_indexes(db: sqlite3.Connection) -> None:
    execute_sql(
        db,
        """
CREATE TABLE mail_documents (  -- a document filed from a mail (finrec.mail_invoices): which mail it came in
  sha TEXT PRIMARY KEY REFERENCES documents(sha), account TEXT NOT NULL, message_id TEXT NOT NULL,
  address TEXT, sender TEXT, subject TEXT, received TEXT, number TEXT
);
CREATE TABLE mail_questions (  -- a mailed document in doubt, waiting in Sarcini for one answer
  sha TEXT PRIMARY KEY, account TEXT NOT NULL, message_id TEXT NOT NULL, address TEXT, sender TEXT, subject TEXT,
  received TEXT, data TEXT NOT NULL,    -- JSON: why it is asked, the firms and totals offered, what was read
  path TEXT NOT NULL, created_at TEXT NOT NULL, answered_at TEXT, answer TEXT
);
CREATE TABLE mail_senders (  -- what the user said once about an exact sender address
  address TEXT PRIMARY KEY, company TEXT,  -- NULL: its documents are not invoices
  by TEXT, at TEXT NOT NULL
);
CREATE INDEX documents_company_month_kind ON documents(company,month,kind,doc_date);
CREATE INDEX efactura_company_status ON efactura(company,status);
CREATE INDEX history_company_direction ON history(company,direction);
CREATE INDEX issued_company_status ON issued(company,status);
CREATE INDEX payments_company_status ON payments(company,status);
CREATE INDEX statement_company_day ON statement_txs(company,day);
DROP TABLE mails;
""",
    )


def partner_settlement_formats(db: sqlite3.Connection) -> None:
    execute_sql(
        db,
        """
CREATE TABLE partner_settlement_formats (
  company TEXT NOT NULL, partner TEXT NOT NULL, signature TEXT NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY(company,partner,signature)
);
""",
    )


def firms(db: sqlite3.Connection) -> None:
    execute_sql(
        db,
        """
CREATE TABLE firms (
  key TEXT PRIMARY KEY, name TEXT NOT NULL, legal_name TEXT NOT NULL,
  cui TEXT NOT NULL UNIQUE, palette TEXT NOT NULL
);
CREATE TABLE firm_accounts (
  company TEXT NOT NULL REFERENCES firms(key), position INTEGER NOT NULL,
  iban TEXT NOT NULL UNIQUE, currency TEXT NOT NULL, bank TEXT NOT NULL,
  pays INTEGER NOT NULL CHECK(pays IN (0,1)), retired TEXT NOT NULL,
  CHECK(NOT (pays=1 AND retired!='')), PRIMARY KEY(company,position)
);
CREATE UNIQUE INDEX firm_paying_currency ON firm_accounts(company,currency) WHERE pays=1;
CREATE TABLE firm_cards (
  company TEXT NOT NULL REFERENCES firms(key), position INTEGER NOT NULL, card TEXT NOT NULL,
  PRIMARY KEY(company,position)
);
CREATE TABLE firm_aliases (
  company TEXT NOT NULL REFERENCES firms(key), position INTEGER NOT NULL, alias TEXT NOT NULL,
  PRIMARY KEY(company,position)
);
""",
    )


def archive(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS archive ("
        "sha TEXT PRIMARY KEY, dest TEXT NOT NULL, status TEXT NOT NULL, archived_at TEXT NOT NULL)"
    )


def card_owners(db: sqlite3.Connection) -> None:
    db.execute("DROP TABLE firm_cards")
    db.execute(
        "CREATE TABLE card_owners (company TEXT NOT NULL REFERENCES firms(key), "
        "card TEXT NOT NULL CHECK(length(card)=4 AND card NOT GLOB '*[^0-9]*'), "
        "owner TEXT NOT NULL, by TEXT, at TEXT NOT NULL, PRIMARY KEY(company,card))"
    )


def received_credit_references(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE efactura ADD COLUMN billing_ref TEXT")
    db.execute("ALTER TABLE efactura ADD COLUMN billing_date TEXT NOT NULL DEFAULT ''")
    db.execute(
        "UPDATE efactura SET doc_type='credit_note',status=CASE WHEN status='duplicate' THEN status ELSE 'credit' END WHERE CAST(total AS NUMERIC)<0"
    )


def receipt_items(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE receipt_items (sha TEXT PRIMARY KEY REFERENCES documents(sha), "
        "company TEXT NOT NULL, payload TEXT NOT NULL, original TEXT NOT NULL, "
        "provenance TEXT NOT NULL, verified INTEGER NOT NULL CHECK(verified IN (0,1)), "
        "revision INTEGER NOT NULL, at TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE receipt_invoice_links (sha TEXT PRIMARY KEY REFERENCES documents(sha), "
        "invoice TEXT NOT NULL UNIQUE REFERENCES efactura(message_id), "
        "company TEXT NOT NULL, at TEXT NOT NULL)"
    )


def readonly_mail(db: sqlite3.Connection) -> None:
    db.execute("DROP TABLE client_reminders")
    db.execute(
        "DELETE FROM meta WHERE key GLOB 'invoice_mail:*' OR key GLOB 'reminder_sequence:*' OR key GLOB 'pack_draft_ids:*' OR key GLOB 'pack_subject:*' OR key GLOB 'pack_recipient:*'"
    )
    db.execute("ALTER TABLE packs RENAME TO old_packs")
    db.execute("""CREATE TABLE packs (
        company TEXT NOT NULL, month TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('built','prepared','sent')),
        zip_path TEXT, updated_at TEXT, note TEXT, PRIMARY KEY(company,month))""")
    db.execute("""INSERT INTO packs SELECT company,month,
        CASE WHEN status='composed' THEN 'built' ELSE status END,zip_path,updated_at,note FROM old_packs""")
    db.execute("DROP TABLE old_packs")
    db.execute(
        "UPDATE integrations SET config=json_remove(config,'$.drafts','$.from_name','$._v.drafts','$._v.from_name') WHERE kind='mail'"
    )
    db.execute(
        "UPDATE meta SET value=json_remove(value,'$.interval_days','$.max_count') WHERE key GLOB 'client_reminders:*' AND json_valid(value)"
    )


def history_documents(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE history_documents (company TEXT NOT NULL, sha TEXT NOT NULL, name TEXT NOT NULL, "
        "data TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','accepted','dismissed')), "
        "PRIMARY KEY(company,sha))"
    )


def auth_sessions(db: sqlite3.Connection) -> None:
    execute_sql(
        db,
        """
CREATE TABLE sessions (
  id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
  method TEXT NOT NULL, recovery_only INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  idle_expires_at TEXT NOT NULL, expires_at TEXT NOT NULL, strong_auth_at TEXT NOT NULL,
  user_agent TEXT NOT NULL, ip TEXT, revoked_at TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE TABLE auth_tickets (
  id_hash TEXT PRIMARY KEY, purpose TEXT NOT NULL, user_id TEXT REFERENCES users(id),
  session_hash TEXT, challenge BLOB, attempts INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT NOT NULL, used_at TEXT
);
CREATE INDEX auth_tickets_expiry ON auth_tickets(expires_at);
""",
    )


def bank_auth_owners(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE bank_auth_owners (state TEXT PRIMARY KEY REFERENCES bank_auth_states(state) ON DELETE CASCADE, "
        "created_by TEXT NOT NULL, browser_hash TEXT NOT NULL)"
    )


def account_management(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE users DROP COLUMN all_firms")
    execute_sql(
        db,
        """
CREATE TABLE setup_links (
  id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
  purpose TEXT NOT NULL CHECK(purpose IN ('enroll','reset')), created_by TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT, revoked_at TEXT
);
CREATE INDEX setup_links_user ON setup_links(user_id);
CREATE TABLE recovery_codes (
  user_id TEXT NOT NULL REFERENCES users(id), code_hash TEXT NOT NULL,
  created_at TEXT NOT NULL, used_at TEXT, PRIMARY KEY(user_id,code_hash)
);
CREATE TABLE auth_events (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, user_id TEXT,
  actor TEXT NOT NULL, ip TEXT, detail TEXT
);
CREATE INDEX auth_events_at ON auth_events(at);
""",
    )


def authentication_limits(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE auth_limits (key_hash TEXT PRIMARY KEY,user_id TEXT,failures INTEGER NOT NULL,"
        "since TEXT NOT NULL,retry_at TEXT,locked_until TEXT)"
    )
    db.execute("CREATE INDEX auth_limits_owner ON auth_limits(user_id,since)")


def authentication_attempts(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE auth_limits ADD COLUMN reservation TEXT")
    db.execute("ALTER TABLE auth_limits ADD COLUMN busy_until TEXT")
    db.execute("CREATE TABLE auth_failure_window(key_hash TEXT NOT NULL,at TEXT NOT NULL)")
    db.execute("CREATE INDEX auth_failure_account ON auth_failure_window(key_hash,at)")
    db.execute("CREATE INDEX auth_failure_expiry ON auth_failure_window(at)")
    db.execute(
        """WITH RECURSIVE counts(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM counts WHERE n<20)
        INSERT INTO auth_failure_window(key_hash,at)
        SELECT key_hash,strftime('%Y-%m-%dT%H:%M:%f',since)||'000+00:00'
        FROM auth_limits JOIN counts ON counts.n<=min(failures,20)"""
    )


def bank_account_offers(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE bank_auth_owners DROP COLUMN browser_hash")
    db.execute(
        "CREATE TABLE bank_account_offers (id TEXT PRIMARY KEY, company TEXT NOT NULL, bank TEXT NOT NULL, "
        "created_by TEXT NOT NULL, accounts TEXT NOT NULL, valid_until TEXT NOT NULL, created_at REAL NOT NULL, "
        "used INTEGER NOT NULL DEFAULT 0)"
    )


def api_token_owners(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE api_tokens ADD COLUMN user_id TEXT REFERENCES users(id)")
    db.execute("UPDATE api_tokens SET user_id=created_by WHERE created_by IN (SELECT id FROM users)")
    db.execute(
        "UPDATE api_tokens SET revoked_at=coalesce(revoked_at,strftime('%Y-%m-%dT%H:%M:%SZ','now')) WHERE user_id IS NULL"
    )
    db.execute("CREATE INDEX api_tokens_owner ON api_tokens(user_id)")


def account_schema(db: sqlite3.Connection) -> None:
    # Preserve the connection’s FK setting; defer its NO ACTION account references during the rebuild.
    db.execute("PRAGMA defer_foreign_keys=ON")
    db.execute("""CREATE TABLE users_next (
      id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','member','readonly')),
      password_hash TEXT, totp_version TEXT, totp_step INTEGER,
      webauthn_handle BLOB NOT NULL UNIQUE,
      created_at TEXT NOT NULL, created_by TEXT, disabled_at TEXT, disabled_by TEXT,
      locked_until TEXT, failures INTEGER NOT NULL DEFAULT 0, failures_since TEXT)""")
    columns = "id,username,display_name,role,password_hash,totp_version,totp_step,webauthn_handle,created_at,created_by,disabled_at,disabled_by,locked_until,failures,failures_since"
    db.execute(f"INSERT INTO users_next({columns}) SELECT {columns} FROM users")
    db.execute("DROP TABLE users")
    db.execute("ALTER TABLE users_next RENAME TO users")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise SchemaError("Account schema rebuild failed foreign-key validation.")
    # SQLite remembers the DROP as a deferred failure. Clear it only after validating every FK.
    db.execute("PRAGMA defer_foreign_keys=OFF")


def passkey_credentials(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE passkeys (
      credential_id BLOB NOT NULL PRIMARY KEY CHECK(typeof(credential_id)='blob' AND length(credential_id) BETWEEN 1 AND 1023),
      user_id TEXT NOT NULL REFERENCES users(id),
      public_key BLOB NOT NULL CHECK(typeof(public_key)='blob' AND length(public_key) BETWEEN 1 AND 16384),
      algorithm INTEGER NOT NULL CHECK(algorithm IN (-7,-8,-257)),
      sign_count INTEGER NOT NULL CHECK(typeof(sign_count)='integer' AND sign_count BETWEEN 0 AND 4294967295),
      backup_eligible INTEGER NOT NULL CHECK(backup_eligible IN (0,1)),
      backed_up INTEGER NOT NULL CHECK(backed_up IN (0,1) AND backed_up<=backup_eligible),
      aaguid BLOB NOT NULL CHECK(typeof(aaguid)='blob' AND length(aaguid)=16),
      name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80), transports TEXT NOT NULL,
      created_at TEXT NOT NULL, last_used_at TEXT)""")
    db.execute("CREATE INDEX passkeys_user ON passkeys(user_id)")


def anaf_mode_environment(db: sqlite3.Connection) -> None:
    db.execute("UPDATE integrations SET config=json_remove(config,'$.env') WHERE kind='anaf'")


def shop_address_field(db: sqlite3.Connection) -> None:
    # The Shopify field that holds this install's address is named after the product, like the field it replaces.
    db.execute(
        "UPDATE integrations SET config=json_remove(json_set(config,'$.fiscalm_url',json_extract(config,'$.contahub_url')),"
        "'$.contahub_url') WHERE kind='shop' AND json_type(config,'$.contahub_url') IS NOT NULL"
    )


def api_token_prefix(db: sqlite3.Connection) -> None:
    # Tokens now start with the product's name. One issued before can no longer sign in, so the list must say so.
    db.execute(
        "UPDATE api_tokens SET revoked_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime') WHERE revoked_at IS NULL"
    )


OWN_ROWS_PENDING = "own_issued_rows_pending"  # meta: invoices issued before fiscalm wrote their own `issued` row


def own_issued_rows(db: sqlite3.Connection) -> None:
    # A marker only; `issued.backfill_own_rows` writes the rows on the next run, because they come from engine code
    # this module cannot import (the ledger imports it). A ledger without an issued invoice gets no marker.
    db.execute(
        "INSERT INTO meta(key,value) SELECT ?,'1' WHERE EXISTS (SELECT 1 FROM invoices WHERE status!='draft')",
        (OWN_ROWS_PENDING,),
    )


def anaf_attempt_refusal(db: sqlite3.Connection) -> None:
    # What a finished attempt to connect ANAF was refused with, so the integration can show it after the return.
    db.execute("ALTER TABLE anaf_auth_states ADD COLUMN refusal TEXT")


def firm_lookup_profile(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE firms ADD COLUMN lookup_profile TEXT")


def member_uploads(db: sqlite3.Connection) -> None:
    # A receipt now comes from a Bondrop link or from a signed-in member; the inbox it was stored in is kept per row.
    db.execute("""CREATE TABLE receipt_uploads_next (
      sha TEXT PRIMARY KEY REFERENCES documents(sha), link_id TEXT REFERENCES upload_links(id),
      user_id TEXT REFERENCES users(id), company TEXT NOT NULL, uploaded_at TEXT NOT NULL, file_type TEXT NOT NULL,
      CHECK ((link_id IS NULL) != (user_id IS NULL)))""")
    db.execute(
        "INSERT INTO receipt_uploads_next(sha,link_id,company,uploaded_at,file_type) "
        "SELECT u.sha,u.link_id,COALESCE(l.company,d.company,''),u.uploaded_at,u.file_type FROM receipt_uploads u "
        "LEFT JOIN upload_links l ON l.id=u.link_id LEFT JOIN documents d ON d.sha=u.sha"
    )
    db.execute("DROP TABLE receipt_uploads")
    db.execute("ALTER TABLE receipt_uploads_next RENAME TO receipt_uploads")
    db.execute("CREATE INDEX receipt_upload_rate ON receipt_uploads(link_id, uploaded_at)")


def awaited_invoices(db: sqlite3.Connection) -> None:
    # A debit whose invoice has not arrived yet ("Aștept factura"): still to explain, with the user's note. It reserves
    # nothing, so the invoice settles it when it comes.
    db.execute("""CREATE TABLE tx_awaiting (
      tx_key TEXT PRIMARY KEY REFERENCES statement_txs(tx_key), company TEXT NOT NULL, note TEXT NOT NULL,
      by TEXT, at TEXT NOT NULL)""")


def drop_supplier_iban_confirmations(db: sqlite3.Connection) -> None:
    """Supplier accounts no longer need human confirmation (maintainer, 2026-11-17)."""
    db.execute("DROP TABLE supplier_iban_confirmations")
    db.execute("DELETE FROM meta WHERE key LIKE 'supplier_account_review:%'")


def rule_explanations(db: sqlite3.Connection) -> None:
    # An explanation no person made (links.apply_rules): 'saved' by a saved rule, 'bank' by the bank's own fee or
    # interest line. A transaction whose explanation a person removed or replaced is theirs: no rule explains it again.
    db.execute("ALTER TABLE tx_links ADD COLUMN rule TEXT")
    db.execute("CREATE TABLE tx_rules_off (tx_key TEXT PRIMARY KEY, company TEXT NOT NULL)")


def payment_origin(db: sqlite3.Connection) -> None:
    # Who queued a payment: 'person' (an invoice someone added) or 'auto' (Automatizări › Plăți). NULL is unknown (a
    # mailed OP, a row from before this): like 'auto', it waits for last month's statement (maintainer, 2026-11-18).
    db.execute("ALTER TABLE payments ADD COLUMN origin TEXT CHECK (origin IN ('person','auto'))")


def received_paid_method(db: sqlite3.Connection) -> None:
    # How a person says they paid a received invoice they marked (efactura.paid_method): a fact, never proof.
    # paid_detail: the card's last four digits, who paid from their own money, or the note for another way.
    db.execute(
        "ALTER TABLE efactura ADD COLUMN paid_method TEXT "
        "CHECK(paid_method IS NULL OR paid_method IN ('bank','card','cash','own','other'))"
    )
    db.execute("ALTER TABLE efactura ADD COLUMN paid_detail TEXT")


def issued_collected_method(db: sqlite3.Connection) -> None:
    # How a person says an issued invoice they marked was collected (issued.collected_method): a fact, never proof.
    # A mark from before this had only a note: "Numerar" was cash, any other note (Compensare included) is "other".
    db.execute(
        "ALTER TABLE issued ADD COLUMN collected_method TEXT "
        "CHECK(collected_method IS NULL OR collected_method IN ('bank','cash','card','other'))"
    )
    db.execute(
        "UPDATE issued SET collected_method=CASE WHEN collected_note LIKE 'user: Numerar%' THEN 'cash' ELSE 'other' END "
        "WHERE status='marked' AND collected_tx IS NULL"
    )


def payment_file_name(short_name: str, day: date) -> str:
    months = ("ian", "feb", "mar", "apr", "mai", "iun", "iul", "aug", "sept", "oct", "nov", "dec")
    return f"{short_name} plăți {day.day} {months[day.month - 1]} {day.year}"


def named_payment_files(db: sqlite3.Connection) -> None:
    execute_sql(
        db,
        """
CREATE TABLE payment_files (
 id INTEGER PRIMARY KEY, company TEXT NOT NULL, name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
 created_at TEXT NOT NULL, imported_at TEXT, path TEXT, revision INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE payments ADD COLUMN file_id INTEGER REFERENCES payment_files(id);
CREATE INDEX payments_file ON payments(file_id);
CREATE TABLE payment_downloads (
 id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES payment_files(id),
 version TEXT NOT NULL, revision INTEGER NOT NULL, items TEXT NOT NULL,
 created_at TEXT NOT NULL, value_date TEXT
);
""",
    )
    for (company, path), rows in _payment_file_groups(db).items():
        created = min(r["created"] for r in rows)
        firm = db.execute("SELECT name FROM firms WHERE key=?", (company,)).fetchone()
        name = payment_file_name((firm[0] if firm else company) or company, date.fromisoformat(created[:10]))
        imported = created if Path(path).parent.name == "plati" else None
        ident = db.execute(
            "INSERT INTO payment_files(company,name,created_at,imported_at,path) VALUES(?,?,?,?,?)",
            (company, name, created, imported, path),
        ).lastrowid
        for row in rows:
            db.execute("UPDATE payments SET file_id=? WHERE item_id=? AND company=?", (ident, row["id"], company))
    _migrate_payment_downloads(db)


def _payment_file_groups(db: sqlite3.Connection) -> dict:
    """Journal intent owns identity even before a crash-interrupted move updates csv_path."""
    imports, merges = {}, {}
    for key, raw in db.execute("SELECT key,value FROM meta WHERE key LIKE 'btgo_import:%' OR key LIKE 'btgo_merge:%'"):
        if not raw:
            continue
        kind, company = key.split(":", 1)
        job = json.loads(raw)
        if kind == "btgo_import":
            imports.update({(company, item): job["dest"] for item in job["ids"]})
        else:
            merges[company, job["src"]] = job["dst"]
    groups = {}
    for item, company, path, created in db.execute(
        "SELECT item_id,company,csv_path,coalesce(exported_at,created_at) FROM payments "
        "WHERE csv_path IS NOT NULL AND csv_path!='' ORDER BY created_at,company,item_id"
    ):
        target = imports.get((company, item), merges.get((company, path), path))
        groups.setdefault((company, target), []).append({"id": item, "created": created})
    return groups


def _migrate_payment_downloads(db: sqlite3.Connection) -> None:
    for key, raw in db.execute("SELECT key,value FROM meta WHERE key LIKE 'btgo_download:%'").fetchall():
        if not raw:
            continue
        company = key.split(":", 1)[1]
        records = json.loads(raw)
        for record in records if isinstance(records, list) else [records]:
            items = record["items"]
            files = {
                r[0]
                for item in items
                for r in db.execute(
                    "SELECT file_id FROM payments WHERE item_id=? AND company=? AND file_id IS NOT NULL",
                    (item, company),
                )
            }
            for ident in sorted(files):
                db.execute(
                    "INSERT INTO payment_downloads(file_id,version,revision,items,created_at,value_date) "
                    "VALUES(?,?,-1,?,?,?)",
                    (ident, record["version"], json.dumps(items), record["at"], record.get("value_date")),
                )


def integration_firm_scope(db: sqlite3.Connection) -> None:
    db.execute("DROP INDEX integrations_one_anaf")
    db.execute("UPDATE integrations SET config=json_set(config,'$.firm_scope','all') WHERE kind IN ('anaf','mail')")


def spv_upload_delay_per_firm(db: sqlite3.Connection) -> None:
    """The SPV upload delay belongs to the firm's rule, next to its switch, not to an ANAF connection that may serve
    several firms. Each firm keeps the delay of the connection that read its invoices (as `anaf_for_firm` picks it)."""
    db.execute(
        "ALTER TABLE invoice_settings ADD COLUMN upload_after_days INTEGER NOT NULL DEFAULT 4 "
        "CHECK(upload_after_days BETWEEN 0 AND 5)"
    )
    anaf = db.execute(
        "SELECT id, config FROM integrations WHERE kind='anaf' AND enabled=1 ORDER BY created_at, rowid"
    ).fetchall()
    for (firm,) in db.execute("SELECT key FROM firms").fetchall():
        chosen = db.execute("SELECT value FROM meta WHERE key=?", (f"anaf_read_connection:{firm}",)).fetchone()
        covering = [(i, json.loads(c)) for i, c in anaf if _covers(json.loads(c), firm)]
        picked = next((c for i, c in covering if chosen and i == chosen[0]), covering[0][1] if covering else None)
        days = (picked or {}).get("upload_after_days")
        if isinstance(days, str) and days in {str(n) for n in range(6)}:
            db.execute(
                "INSERT INTO invoice_settings(company, auto_upload, upload_after_days) VALUES(?,1,?) "
                "ON CONFLICT(company) DO UPDATE SET upload_after_days=excluded.upload_after_days",
                (firm, int(days)),
            )
    db.execute("UPDATE integrations SET config=json_remove(config,'$.upload_after_days') WHERE kind='anaf'")


def one_off_switch(db: sqlite3.Connection) -> None:
    """An integration is stopped by its own Oprește only: a mail account with reading off becomes a stopped account,
    and the "no AI" provider goes, stopping every other AI service when it was the one in use."""
    for iid, cfg in db.execute("SELECT id, config FROM integrations WHERE kind='mail'").fetchall():
        config = json.loads(cfg)
        if config.pop("read_invoices", "true") == "false":
            db.execute("UPDATE integrations SET enabled=0 WHERE id=?", (iid,))
        db.execute("UPDATE integrations SET config=? WHERE id=?", (json.dumps(config), iid))
    # The provider in use, chosen as integrations.default() chooses it: the enabled default, else the oldest enabled.
    used = db.execute(
        "SELECT json_extract(config,'$.provider') FROM integrations WHERE kind='ai' AND enabled=1 "
        "ORDER BY is_default DESC, created_at LIMIT 1"
    ).fetchone()
    if used and used[0] == "none":
        db.execute("UPDATE integrations SET enabled=0 WHERE kind='ai'")
    db.execute("DELETE FROM integrations WHERE kind='ai' AND json_extract(config,'$.provider')='none'")


def _covers(config: dict, firm: str) -> bool:
    scope = config.get("firm_scope", "all")
    return scope == "all" or firm in scope


def product_catalog(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE products (
        company TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
        name TEXT NOT NULL, code TEXT NOT NULL, unit TEXT NOT NULL,
        unit_price TEXT NOT NULL, currency TEXT NOT NULL, vat_rate TEXT NOT NULL,
        vat_category TEXT NOT NULL, exemption TEXT NOT NULL, description TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(company,id))""")
    db.execute("""CREATE TABLE product_references (
        company TEXT NOT NULL, product_id TEXT NOT NULL, invoice_id TEXT NOT NULL,
        PRIMARY KEY(company,product_id,invoice_id),
        FOREIGN KEY(company,product_id) REFERENCES products(company,id))""")


def app_statements_imported(db: sqlite3.Connection) -> None:
    # A statement accepted in the app was stored as an "upload:" (an unchecked file), so every pack held it back.
    # It is a checked import, like the XML one. A pack already sent for its month or a later one went without it:
    # it counts as sent, so it never reaches the accountant later as an old document; the meta row lists those.
    late = [
        r[0]
        for r in db.execute(
            "SELECT sha FROM documents d WHERE source='upload:app' AND kind='statement' AND sent_at IS NULL "
            "AND month <= (SELECT MAX(month) FROM packs p WHERE p.company=d.company AND p.status='sent')"
        )
    ]
    for sha in late:
        db.execute("UPDATE documents SET sent_at=strftime('%Y-%m-%dT%H:%M:%S','now','localtime') WHERE sha=?", (sha,))
    if late:
        db.execute("INSERT INTO meta(key,value) VALUES('app-statements-counted-sent',?)", (json.dumps(late),))
    db.execute("UPDATE documents SET source='statement-import' WHERE source='upload:app' AND kind='statement'")


def account_opened(db: sqlite3.Connection) -> None:
    # The first month an account existed (YYYY-MM), set by a person; '' when unknown, so the account is owed in every
    # month's statement as before (bank.intake.missing_accounts).
    db.execute("ALTER TABLE firm_accounts ADD COLUMN opened TEXT NOT NULL DEFAULT ''")


def drop_receipt_items(db: sqlite3.Connection) -> None:
    # Line-by-line receipt transcription is gone (#44). A receipt already associated with an invoice keeps that
    # association on documents.efactura_id; the tables only held the transcribed lines and a copy of that link.
    db.execute("DROP TABLE receipt_invoice_links")
    db.execute("DROP TABLE receipt_items")


def credit_references_reread(db: sqlite3.Connection) -> None:
    # A received credit note with several references was stored without one, like a note that names no invoice.
    # The next e-Factura sync reads both again from their archives (efactura.fill_credit_references).
    db.execute("UPDATE efactura SET billing_ref=NULL,billing_date='' WHERE status='credit' AND billing_ref=''")


def bank_coverage(db: sqlite3.Connection) -> None:
    # What each stored statement covers, per account (bank.coverage.recorded), and from which day the open banking
    # feed has read each account. A CAMT or MT940 stored before this was a whole month of the accounts it names,
    # proven by its balances, and its lines were matched (the backup before migrating refuses a ledger with an
    # unfinished, journalled import). A PDF, a CSV, or one that names no account, covers nothing yet: nothing
    # proves its lines were matched (a PDF set had no journal), so it counts once imported again.
    # A connection's feed covers its accounts from its next read on.
    db.execute("ALTER TABLE documents ADD COLUMN coverage TEXT")
    db.execute("ALTER TABLE bank_connections ADD COLUMN covered TEXT NOT NULL DEFAULT '{}'")
    db.execute(
        "UPDATE documents SET coverage=(SELECT json_group_object(a.value, json(json_array(documents.month||'-01', "
        "date(documents.month||'-01','+1 month','-1 day'), json('true')))) "
        "FROM json_each(documents.accounts) a) "
        "WHERE kind='statement' AND accounts IS NOT NULL AND (path LIKE '%.xml' OR path LIKE '%.sta')"
    )


def ai_transaction_proposals(db: sqlite3.Connection) -> None:
    # What the AI integration proposed for a statement line still to explain (ai_explain): kind label | advance |
    # documents | none ('none': asked, nothing to propose, so it is not asked again). invoices: a JSON list of ids.
    db.execute(
        "CREATE TABLE tx_ai (tx_key TEXT PRIMARY KEY, company TEXT NOT NULL, kind TEXT NOT NULL, partner TEXT, "
        "invoices TEXT NOT NULL DEFAULT '[]', reason TEXT, note TEXT, at TEXT NOT NULL)"
    )


MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    baseline,
    transaction_links,
    mail_documents_and_indexes,
    partner_settlement_formats,
    firms,
    archive,
    card_owners,
    received_credit_references,
    receipt_items,
    readonly_mail,
    history_documents,
    auth_sessions,
    bank_auth_owners,
    account_management,
    authentication_limits,
    authentication_attempts,
    bank_account_offers,
    api_token_owners,
    account_schema,
    passkey_credentials,
    anaf_mode_environment,
    shop_address_field,
    api_token_prefix,
    own_issued_rows,
    anaf_attempt_refusal,
    firm_lookup_profile,
    member_uploads,
    awaited_invoices,
    drop_supplier_iban_confirmations,
    rule_explanations,
    payment_origin,
    received_paid_method,
    named_payment_files,
    issued_collected_method,
    integration_firm_scope,
    spv_upload_delay_per_firm,
    product_catalog,
    one_off_switch,
    app_statements_imported,
    account_opened,
    drop_receipt_items,
    credit_references_reread,
    bank_coverage,
    ai_transaction_proposals,
)
SCHEMA_VERSION = len(MIGRATIONS)


def check_version(db: sqlite3.Connection, *, require_current: bool = False) -> int:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"Ledger schema {version} is newer than supported schema {SCHEMA_VERSION}. "
            "Upgrade fiscalm before opening this ledger; do not downgrade the database."
        )
    if version < 0:
        raise SchemaError("Invalid ledger schema version. Restore a verified backup.")
    if require_current and version != SCHEMA_VERSION:
        raise SchemaError(
            f"Ledger schema {version} needs an upgrade to schema {SCHEMA_VERSION}. "
            "Start the current fiscalm release to migrate before reading this ledger."
        )
    return version


def migrate(db: sqlite3.Connection) -> None:
    """Serialize starters; commit each schema change, version and receipt together.

    Migration callbacks use execute(), never executescript(), commit() or external
    effects. SQLite rolls back DDL and data even if the process dies before commit.
    """
    if check_version(db) == SCHEMA_VERSION:
        return
    while True:
        try:
            db.execute("BEGIN IMMEDIATE")
            version = check_version(db)
            if version == SCHEMA_VERSION:
                db.rollback()
                return
            if (
                version == 0
                and db.execute("SELECT 1 FROM sqlite_master WHERE substr(name,1,7)!='sqlite_' LIMIT 1").fetchone()
            ):
                raise SchemaError(
                    "Unversioned ledger is not a product schema. "
                    "Use a fresh product installation; first-install ledgers are not migrated."
                )
            MIGRATIONS[version](db)
            db.execute(
                "INSERT INTO meta(key,value) VALUES(?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (f"schema_migration:{version + 1}",),
            )
            db.execute(f"PRAGMA user_version={version + 1}")
            db.commit()
        except BaseException:
            db.rollback()
            raise
