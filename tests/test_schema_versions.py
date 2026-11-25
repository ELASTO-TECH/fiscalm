"""Product schema upgrades, built entirely from synthetic ledgers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import multiprocessing as mp
import os
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from threading import Barrier

import pytest
from firms import seed
from test_invoicing import DATA, LINE, PARTY

from fiscalm import __version__, backup, bnr, cli, migrations as m, pack
from fiscalm.api import state as api
from fiscalm.invoicing import engine as invoicing, issued, ubl
from fiscalm.ledger import Ledger


@pytest.fixture(autouse=True)
def empty_registry(home):
    from firms import set_firms

    set_firms({})


def test_fresh_schema_matches_dev_before_migrations(ledger):
    rows = list(
        map(
            list,
            ledger.db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name NOT IN ('passkeys','bank_account_offers','auth_failure_window','auth_limits','setup_links','recovery_codes','auth_events','bank_auth_owners','sessions','auth_tickets','history_documents','archive','partner_settlement_formats','firms','firm_accounts','firm_cards','card_owners','firm_aliases','receipt_uploads') ORDER BY type,name"
            ),
        )
    )
    # Core product schema, including removal of legacy mappings and per-firm account permissions; upgrades are checked separately.
    assert hashlib.sha256(json.dumps(rows).encode()).hexdigest() == (
        "bb15c5174ccfd3d421582cb6e2c0f577a12b62a912bd1f209dcde6beefc4bb46"
    )
    assert not ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='user_firms'").fetchone()
    assert m.read_version(ledger.path) == m.SCHEMA_VERSION
    assert ledger.get_meta(f"schema_migration:{m.SCHEMA_VERSION}")
    with ledger.tx():  # An up-to-date opener must not compete for the write lock.
        Ledger(ledger.path, timeout=0.01).close()


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("version", range(1, m.SCHEMA_VERSION))
def test_upgrade_preserves_previous_schemas_data(tmp_path, monkeypatch, version, archived):
    path = tmp_path / "ledger.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", version)
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO integrations VALUES('anaf-previous','anaf','ANAF',?,1,1,'2026-01-01','2026-01-01')",
            (json.dumps({"client_id": "synthetic-client"} | ({"env": "test"} if version < 21 else {})),),
        )
        db.execute(
            "INSERT INTO integrations VALUES('shop-previous','shop','Shop',?,1,1,'2026-01-01','2026-01-01')",
            (
                json.dumps(
                    {"platform": "shopify", "fiscalm_url" if version >= 22 else "contahub_url": "https://books.example"}
                ),
            ),
        )
        db.execute(
            "INSERT INTO users(id,username,display_name,role,webauthn_handle,created_at) "
            "VALUES('synthetic-user','example','Example','readonly',X'01','2026-11-04')"
        )
        db.execute(
            "INSERT INTO api_tokens(id,name,scope,secret_hash,created_at,created_by) VALUES('owned','Script','read','digest','2026-11-04','synthetic-user')"
        )
        db.execute(
            "INSERT INTO api_tokens(id,name,scope,secret_hash,created_at) VALUES('ownerless','Script','write','digest','2026-11-04')"
        )
        if version >= 18:
            db.execute("UPDATE api_tokens SET user_id='synthetic-user' WHERE id='owned'")
            db.execute("UPDATE api_tokens SET revoked_at='2026-11-04' WHERE id='ownerless'")
        if version >= 12:
            db.execute(
                "INSERT INTO sessions VALUES('session','synthetic-user','password_totp',0,'2026-11-04','2026-11-04','2026-12-01','2026-12-01','2026-11-04','Synthetic browser','192.0.2.1',NULL)"
            )
            db.execute(
                "INSERT INTO auth_tickets(id_hash,purpose,user_id,attempts,expires_at) VALUES('ticket','signin','synthetic-user',1,'2026-12-01')"
            )
        if version >= 14:
            db.execute(
                "INSERT INTO setup_links(id,secret_hash,user_id,purpose,created_by,created_at,expires_at) VALUES('link','digest','synthetic-user','enroll','cli','2026-11-04','2026-12-01')"
            )
            db.execute("INSERT INTO recovery_codes VALUES('synthetic-user','digest','2026-11-04',NULL)")
        if version >= 5:
            db.execute(
                "INSERT INTO firms(key,name,legal_name,cui,palette) VALUES('EXAMPLE','Example','Example SRL','12345674','amber')"
            )
            if version < 7:
                db.execute("INSERT INTO firm_cards VALUES('EXAMPLE',0,'9876')")
        if archived:
            db.execute(
                "CREATE TABLE IF NOT EXISTS archive (sha TEXT PRIMARY KEY, dest TEXT NOT NULL, "
                "status TEXT NOT NULL, archived_at TEXT NOT NULL)"
            )
            db.execute("INSERT INTO archive VALUES('synthetic','drive:example','copied','2026-01-01')")
        db.execute(
            "INSERT INTO upload_links(id,company,label,secret_hash,created_at,expires_at) "
            "VALUES('phone','EXAMPLE','Phone','digest','2026-01-01','2026-02-01')"
        )
        db.execute(
            "INSERT INTO documents(sha,company,month,kind,path,created_at) "
            "VALUES('synthetic-receipt','EXAMPLE','2026-01','receipt','/synthetic.jpg','2026-01-01')"
        )
        db.execute(
            "INSERT INTO receipt_uploads VALUES('synthetic-receipt','phone','2026-01-01','jpg')"
            if version < 27
            else "INSERT INTO receipt_uploads VALUES('synthetic-receipt','phone',NULL,'EXAMPLE','2026-01-01','jpg')"
        )
        db.execute(
            "INSERT INTO payments(item_id,company,kind,month,amount,status,created_at) "
            "VALUES('synthetic','EXAMPLE','invoice','2026-01','12.34','pending','2026-01-01')"
        )
        db.execute(  # marked paid before the marking said how: still the person's word, with no method
            "INSERT INTO efactura(message_id,company,raw_path,status,paid_at,paid_note,created_at) "
            "VALUES('9001','EXAMPLE','/synthetic.zip','paid','2026-01-02','user: already paid (page)','2026-01-01')"
        )
        if version >= 2:  # a person's explanation from before rules explained lines: still the person's
            db.execute(
                "INSERT INTO tx_links(tx_key,id,company,kind,difference,at) "
                "VALUES('synthetic-line','person','EXAMPLE','label','0.00','2026-01-01')"
            )
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO packs(company,month,status) VALUES('EXAMPLE','2026-01',?)",
            ("composed" if version < 10 else "built",),
        )
        if version < 10:
            db.execute("INSERT INTO client_reminders VALUES('obsolete',1,'EXAMPLE','2026-01-01','pending',NULL)")
    if version < 5:
        with pytest.raises(m.SchemaError, match="predates the company registry"):
            Ledger(path)
        assert m.read_version(path) == version
        assert not list(backup.directory().glob("pre-migration-*.zip"))
        return
    ledger = Ledger(path)
    assert not ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='supplier_iban_confirmations'").fetchone()
    scoped = version <= m.MIGRATIONS.index(m.integration_firm_scope)  # a row written later gets no scope from it
    assert json.loads(ledger.db.execute("SELECT config FROM integrations WHERE id='anaf-previous'").fetchone()[0]) == {
        "client_id": "synthetic-client",
    } | ({"firm_scope": "all"} if scoped else {})
    assert json.loads(ledger.db.execute("SELECT config FROM integrations WHERE id='shop-previous'").fetchone()[0]) == {
        "platform": "shopify",
        "fiscalm_url": "https://books.example",
    }
    assert ledger.pack("EXAMPLE", "2026-01")["status"] == "built"
    assert not ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='client_reminders'").fetchone()
    assert m.read_version(path) == m.SCHEMA_VERSION
    assert ledger.payment("synthetic")["amount"] == "12.34"
    assert ledger.payment("synthetic")["origin"] is None  # unknown: it waits for the statement like an automatic one
    marked = ledger.efactura("9001")
    assert (marked["paid_note"], marked["paid_method"], marked["paid_detail"]) == (
        "user: already paid (page)",
        None,
        None,
    )
    assert tuple(map(tuple, ledger.db.execute("SELECT * FROM receipt_uploads"))) == (
        ("synthetic-receipt", "phone", None, "EXAMPLE", "2026-01-01", "jpg"),
    )
    assert ledger.card_owners() == {}
    assert ledger.db.execute("SELECT role FROM users WHERE id='synthetic-user'").fetchone()[0] == "readonly"
    assert not {"all_firms", "legacy_name"} & {r[1] for r in ledger.db.execute("PRAGMA table_info(users)")}
    assert not ledger.db.execute("PRAGMA foreign_key_check").fetchall()
    assert ledger.db.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == (1 if version >= 12 else 0)
    assert ledger.db.execute("SELECT count(*) FROM auth_tickets").fetchone()[0] == (1 if version >= 12 else 0)
    assert ledger.db.execute("SELECT count(*) FROM setup_links").fetchone()[0] == (1 if version >= 14 else 0)
    assert ledger.db.execute("SELECT count(*) FROM recovery_codes").fetchone()[0] == (1 if version >= 14 else 0)
    owner, revoked = ledger.db.execute("SELECT user_id,revoked_at FROM api_tokens WHERE id='owned'").fetchone()
    assert owner == "synthetic-user" and bool(revoked) == (version < 23)  # issued under the earlier prefix
    kept = ledger.db.execute("SELECT revoked_at FROM api_tokens WHERE id='ownerless'").fetchone()[0]
    assert kept == "2026-11-04" if version >= 18 else kept  # An earlier revocation keeps its own time.
    for table in (
        "passkeys",
        "bank_account_offers",
        "auth_failure_window",
        "auth_limits",
        "auth_events",
    ):
        assert ledger.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert list(map(tuple, ledger.db.execute("SELECT * FROM archive"))) == (
        [("synthetic", "drive:example", "copied", "2026-01-01")] if archived else []
    )
    assert list(map(tuple, ledger.db.execute("SELECT id, rule FROM tx_links"))) == [("person", None)]
    assert ledger.db.execute("SELECT count(*) FROM tx_awaiting").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM mail_documents").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM history_documents").fetchone()[0] == 0
    assert (
        ledger.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'schema_migration:%'").fetchone()[0]
        == m.SCHEMA_VERSION
    )
    ledger.close()


def _start(path, barrier):
    barrier.wait()
    Ledger(path).close()


def test_concurrent_start(tmp_path):
    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(2)
    path = tmp_path / "ledger.db"
    processes = [ctx.Process(target=_start, args=(path, barrier)) for _ in range(2)]
    for p in processes:
        p.start()
    for p in processes:
        p.join(15)
        assert p.exitcode == 0
    assert m.read_version(path) == m.SCHEMA_VERSION


def _crash(db):
    db.execute("CREATE TABLE unfinished(id TEXT)")
    db.execute("UPDATE meta SET value='lost' WHERE key='synthetic'")
    os._exit(17)


def test_crash_rolls_back_schema_data_and_version(ledger, monkeypatch):
    ledger.set_meta("synthetic", "kept")
    monkeypatch.setattr(m, "MIGRATIONS", (*m.MIGRATIONS, _crash))
    monkeypatch.setattr(m, "SCHEMA_VERSION", m.SCHEMA_VERSION + 1)
    p = mp.get_context("fork").Process(target=lambda: Ledger(ledger.path))
    p.start()
    p.join(15)
    assert p.exitcode == 17
    assert list(backup.directory().glob("pre-migration-*.zip"))
    assert m.read_version(ledger.path) == m.SCHEMA_VERSION - 1
    assert not ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name='unfinished'").fetchone()
    assert ledger.get_meta("synthetic") == "kept"
    assert not ledger.get_meta(f"schema_migration:{m.SCHEMA_VERSION}")


@pytest.mark.parametrize(
    "version,readonly,reason",
    [
        (m.SCHEMA_VERSION + 1, False, "newer.*Upgrade fiscalm"),
        (m.SCHEMA_VERSION + 1, True, "newer.*Upgrade fiscalm"),
        (1, True, "needs an upgrade"),
        (0, False, "Unversioned ledger"),
    ],
)
def test_incompatible_ledger_refused_without_writes(ledger, version, readonly, reason):
    ledger.db.execute(f"PRAGMA user_version={version}")
    before = ledger.path.read_bytes()
    with pytest.raises(m.SchemaError, match=reason):
        Ledger(ledger.path, readonly=readonly)
    assert m.read_version(ledger.path) == version
    assert before == ledger.path.read_bytes()


def test_version_without_config(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "from fiscalm import main; main()", "--version"],
        env={**os.environ, "FISCALM_HOME": str(tmp_path / "empty")},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"fiscalm {__version__}"


def test_firm_cui_race_and_stale_update(ledger):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier

    from fiscalm import companies

    company = companies.Company("ALFA", "Alfa Exemplu SRL", "12345674", (), "Alfa")
    barrier = Barrier(2)

    def add(key):
        L = Ledger(ledger.path)
        try:
            barrier.wait()
            L.save_company(replace(company, key=key))
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            L.close()

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(add, ("ALFA", "BETA"))) == [False, True]
    (saved,) = companies.all(ledger).values()
    newer = replace(saved, name="Nou")
    ledger.save_company(newer, expected=saved)
    with pytest.raises(ValueError, match="changed"):
        ledger.save_company(replace(saved, name="Stale"), expected=saved)
    with pytest.raises(ValueError):
        ledger.save_company(replace(newer, key="OTHER"), expected=newer)
    assert companies.all(ledger)[saved.key] == newer


@pytest.mark.parametrize("field,value", [("key", "../X"), ("key", "ALL"), ("palette", "unknown")])
def test_firm_writer_refuses_invalid_facts(ledger, field, value):
    from dataclasses import replace

    from fiscalm import companies

    company = companies.Company("ALFA", "Alfa Exemplu SRL", "12345674", ())
    with pytest.raises(Exception, match="company"):
        ledger.save_company(replace(company, **{field: value}))
    assert not companies.all(ledger)


@pytest.mark.parametrize("cui", ["RO12345674", "012345674"])
def test_firm_writer_normalizes_identifiers(ledger, cui):
    from dataclasses import replace

    from fiscalm import companies

    account = companies.Account("RO00BTRL0000000000000001", "RON", "bt", True)
    company = companies.Company("ALFA", "Alfa Exemplu SRL", "12345674", (account,))
    ledger.save_company(company)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.save_company(replace(company, key="BETA", cui=cui, accounts=()))
    with pytest.raises(sqlite3.IntegrityError):
        ledger.save_company(
            replace(company, key="BETA", cui="23456785", accounts=(replace(account, iban=account.iban.lower()),))
        )
    assert companies.all(ledger)["ALFA"] == replace(company, name="ALFA")


def test_upgrade_backup_precedes_migration_and_retains_five(ledger, monkeypatch, home, tmp_path_factory):
    alias = tmp_path_factory.mktemp("alias") / "home"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("FISCALM_HOME", str(alias))  # a data path that crosses a symlink still upgrades
    original = m.SCHEMA_VERSION
    monkeypatch.setattr(m, "SCHEMA_VERSION", original + 1)

    def upgrade(db):
        archives = list((home / "backups").glob("pre-migration-*.zip"))
        assert archives
        with zipfile.ZipFile(max(archives)) as z:
            assert json.loads(z.read("manifest.json"))["schema"] == original
        db.execute("INSERT INTO meta VALUES('backed-up','yes')")

    monkeypatch.setattr(m, "MIGRATIONS", (*m.MIGRATIONS, upgrade))
    for _ in range(7):
        ledger.db.execute(f"PRAGMA user_version={original}")
        ledger.db.execute("DELETE FROM meta WHERE key IN ('backed-up', ?)", (f"schema_migration:{original + 1}",))
        ledger.db.commit()
        Ledger().close()
    assert len(list((home / "backups").glob("pre-migration-*.zip"))) == 5
    Ledger().close()
    assert len(list((home / "backups").glob("pre-migration-*.zip"))) == 5


def test_backup_failure_blocks_upgrade(ledger, monkeypatch, home):
    monkeypatch.setattr(m, "SCHEMA_VERSION", m.SCHEMA_VERSION + 1)

    def fail(*args, **kwargs):
        raise OSError("synthetic")

    monkeypatch.setattr(backup, "create", fail)
    with pytest.raises(m.SchemaError, match="startup refused without migrating"):
        Ledger()
    assert m.read_version(ledger.path) == m.SCHEMA_VERSION - 1


def test_current_and_fresh_ledgers_need_no_backup(home):
    Ledger().close()
    assert not (home / "backups").exists()


def test_rate_limit_window_upgrade_keeps_existing_counts(tmp_path, monkeypatch):
    path = tmp_path / "limits.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", m.MIGRATIONS.index(m.authentication_attempts))
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO auth_limits VALUES('synthetic',NULL,5,'2026-11-04T00:00:00+00:00',NULL,NULL)")
    upgraded = Ledger(path)
    try:
        assert (
            list(map(tuple, upgraded.db.execute("SELECT * FROM auth_failure_window")))
            == [("synthetic", "2026-11-04T00:00:00.000000+00:00")] * 5
        )
        assert upgraded.db.execute("SELECT reservation,busy_until FROM auth_limits").fetchone()[:] == (None, None)
    finally:
        upgraded.close()


def test_fresh_migrations_hold_startup_lock_between_schema_versions(tmp_path, monkeypatch):
    ctx = mp.get_context("fork")
    entered, resume = ctx.Event(), ctx.Event()
    path = tmp_path / "starters.db"
    original = m.MIGRATIONS[1]

    def pause_after_first_version(db):
        entered.set()
        assert resume.wait(timeout=5)
        original(db)

    monkeypatch.setattr(m, "MIGRATIONS", (m.MIGRATIONS[0], pause_after_first_version, *m.MIGRATIONS[2:]))
    first = ctx.Process(target=_start, args=(path, ctx.Barrier(1)))
    second = None
    first.start()
    try:
        assert entered.wait(timeout=5)
        with path.with_suffix(".migration.lock").open("a") as probe, pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        second = ctx.Process(target=_start, args=(path, ctx.Barrier(1)))
        second.start()
    finally:
        resume.set()
        for process in (first, second):
            if process is not None:
                process.join(15)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
    assert first.exitcode == second.exitcode == 0
    assert m.read_version(path) == m.SCHEMA_VERSION


@pytest.mark.parametrize("failure", ["after_rebuild", "invalid_fk"])
def test_account_rebuild_failure_rolls_back_schema_and_preserves_children(tmp_path, monkeypatch, failure):
    path = tmp_path / "ledger.db"
    previous = m.MIGRATIONS.index(m.account_schema)
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", previous)
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO users(id,username,display_name,role,webauthn_handle,created_at,legacy_name) VALUES('owner','owner','Owner','member',X'01','2026-11-04','discarded-name')"
        )
        db.execute(
            "INSERT INTO api_tokens(id,name,scope,secret_hash,created_at,user_id) VALUES('key','Script','read','digest','2026-11-04',?)",
            ("missing" if failure == "invalid_fk" else "owner",),
        )
    original = m.account_schema

    def failed(db):
        original(db)
        raise RuntimeError("synthetic post-rebuild failure")

    if failure == "after_rebuild":
        monkeypatch.setattr(m, "MIGRATIONS", (*m.MIGRATIONS[:previous], failed, *m.MIGRATIONS[previous + 1 :]))
    error, message = (
        (RuntimeError, "synthetic post-rebuild failure")
        if failure == "after_rebuild"
        else (m.SchemaError, "foreign-key validation")
    )
    with closing(sqlite3.connect(path)) as db, pytest.raises(error, match=message):
        m.migrate(db)  # Exercise the runner itself; normal startup also validates its pre-migration backup.
    assert m.read_version(path) == previous
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT legacy_name FROM users").fetchone()[0] == "discarded-name"
        assert db.execute("SELECT count(*) FROM api_tokens").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='users_next'").fetchone()


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_account_schema_rebuild_preserves_fk_setting_and_enforces_valid_relations(foreign_keys):
    with sqlite3.connect(":memory:") as db:
        m.baseline(db)
        m.account_management(db)
        m.auth_sessions(db)
        m.api_token_owners(db)
        db.execute(
            "INSERT INTO users(id,username,display_name,role,webauthn_handle,created_at,legacy_name) VALUES('owner','owner','Owner','admin',X'01','2026-11-04','discarded')"
        )
        db.execute(
            "INSERT INTO api_tokens(id,name,scope,secret_hash,created_at,user_id) VALUES('key','Script','read','digest','2026-11-04','owner')"
        )
        db.commit()
        db.execute(f"PRAGMA foreign_keys={foreign_keys}")
        with db:
            db.execute("BEGIN IMMEDIATE")
            m.account_schema(db)
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == foreign_keys
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT user_id FROM api_tokens").fetchone()[0] == "owner"
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE api_tokens SET user_id='missing'")


@pytest.mark.parametrize("case", ["open", "partial", "collected", "unbuildable"])
def test_first_run_after_upgrade_gives_earlier_invoices_their_own_row(ledger, monkeypatch, case):
    """Before schema 24 an invoice issued in fiscalm had no `issued` row of its own. The first run after the upgrade
    writes it: listed once, owed right, a collection made through its linked SPV copy kept once."""
    seed(ledger)
    monkeypatch.setattr(bnr, "fetch_xml", lambda _: (_ for _ in ()).throw(bnr.RateError("offline")))
    invoicing.save_seller(ledger, "NORD", PARTY)
    cid = invoicing.save_client(
        ledger, "NORD", PARTY | dict(name="Client Fictiv SRL", cui="99999992", vat_id="RO99999992")
    )
    invoicing.create_series(ledger, "NORD", "TEST", 42)
    iid = invoicing.save_draft(ledger, "NORD", "TEST", cid, DATA | dict(lines=[LINE, LINE | dict(unit_price="200")]))
    invoicing.issue(ledger, iid, DATA["issue_date"], company="NORD", version=1)
    if case == "partial":
        invoicing.storno(ledger, iid, "2026-11-01", "Retur", [2], company="NORD")
    own = invoicing.import_id("local", iid)
    with ledger.tx() as db:  # the data schema 23 left: no own rows; "collected" read its SPV copy back, linked
        if case == "collected":
            for table in ("issued", "invoice_sent_links"):
                db.execute(f"UPDATE {table} SET message_id='7001' WHERE message_id=?", (own,))
            db.execute("UPDATE issued SET upload_id='5001' WHERE message_id='7001'")
        else:
            db.execute("DELETE FROM issued")
            db.execute("DELETE FROM invoice_sent_links")
        m.own_issued_rows(db)  # what the upgrade to schema 24 leaves for the first run
    if case == "collected":
        assert ledger.mark_issued_collected("7001", "statement", "tx-1")
    ledger.close()
    if case == "unbuildable":  # a rule added since: the stored snapshot no longer generates XML
        monkeypatch.setattr(ubl, "build", lambda inv: (_ for _ in ()).throw(invoicing.InvoiceError("BR-synthetic")))
    with closing(Ledger()) as L:
        assert L.get_meta(m.OWN_ROWS_PENDING) == "1"
    assert cli.main(["status"]) == 0
    with closing(Ledger()) as L:
        rows = [r for r in api._issued(L, "NORD", date(2026, 11, 2), False) if r["local_id"] == iid]
        owed = {"open": "363.00", "partial": "121.00", "collected": "0.00", "unbuildable": "363.00"}[case]
        assert [(r["owed"], r["status"]) for r in rows] == [(owed, "collected" if case == "collected" else "open")]
        assert (L.issued(own) is None) == (case == "collected")
        assert L.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == (case == "collected")
        assert L.get_meta(m.OWN_ROWS_PENDING) is None and issued.backfill_own_rows(L) == 0


def test_named_payment_files_upgrade_from_32(tmp_path, monkeypatch):
    path = tmp_path / "previous.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", 32)
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO firms(key,name,legal_name,cui,palette) VALUES('EXAMPLE','Example','Example SRL','12345674','amber')"
        )
        for item, status, csv_path in (
            ("first", "exported", "/synthetic/De plătit/payments-EXAMPLE.csv"),
            ("second", "pending", None),
            ("old", "paid", "/synthetic/EXAMPLE/plati/payments-EXAMPLE-2026-10-01.csv"),
        ):
            db.execute(
                "INSERT INTO payments(item_id,company,kind,month,amount,status,csv_path,created_at) "
                "VALUES(?,'EXAMPLE','op','2026-10','12.34',?,?,'2026-10-01T10:00:00Z')",
                (item, status, csv_path),
            )
        history = [
            {"version": "snapshot", "items": ["first", "old", "second"], "at": "2026-10-02", "value_date": "2026-10-02"}
        ]
        db.execute("INSERT INTO meta VALUES('btgo_download:EXAMPLE',?)", (json.dumps(history),))
    with closing(Ledger(path)) as ledger:
        assert m.read_version(path) == m.SCHEMA_VERSION
        files = list(ledger.db.execute("SELECT * FROM payment_files"))
        assert len(files) == 2
        assert {f["name"] for f in files} == {"Example plăți 1 oct 2026"}
        assert sum(f["imported_at"] is not None for f in files) == 1
        assert ledger.payment("second")["file_id"] is None
        assert ledger.payment("first")["file_id"] != ledger.payment("old")["file_id"]
        assert ledger.payment("first")["file_id"]
        assert ledger.payment("old")["file_id"]
        assert ledger.payment_file(ledger.payment("first")["file_id"])["imported_at"] is None
        assert ledger.payment_file(ledger.payment("old")["file_id"])["imported_at"] is not None
        downloads = list(ledger.db.execute("SELECT * FROM payment_downloads"))
        assert len(downloads) == 2
        assert all(d["revision"] == -1 for d in downloads)
        assert all(json.loads(d["items"]) == ["first", "old", "second"] for d in downloads)
        assert ledger.payment("first")["csv_path"] == "/synthetic/De plătit/payments-EXAMPLE.csv"
        assert ledger.payment("old")["status"] == "paid"


def test_issued_marks_get_a_method_on_upgrade_from_33(tmp_path, monkeypatch):
    path = tmp_path / "previous.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", 33)
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        for mid, status, note, tx in (
            ("cash", "marked", "user: Numerar [user u_admin]", None),
            ("offset", "marked", "user: Compensare", None),
            ("proved", "collected", "statement x", "tx-1"),
            ("open", "open", None, None),
        ):
            db.execute(
                "INSERT INTO issued(message_id,company,raw_path,status,collected_note,collected_tx,created_at) "
                "VALUES(?,'EXAMPLE','synthetic',?,?,?,'2026-10-31')",
                (mid, status, note, tx),
            )
    with closing(Ledger(path)) as ledger:
        assert m.read_version(path) == m.SCHEMA_VERSION
        methods = dict(ledger.db.execute("SELECT message_id, collected_method FROM issued"))
        assert methods == {"cash": "cash", "offset": "other", "proved": None, "open": None}


@pytest.mark.parametrize("kind", ["import", "merge"])
def test_named_files_upgrade_preserves_pending_move_identity(tmp_path, monkeypatch, kind):
    path = tmp_path / "previous.db"
    source = "/synthetic/De plătit/payments-EXAMPLE.csv"
    target = (
        "/synthetic/EXAMPLE/plati/payments-EXAMPLE-2026-11-18.csv"
        if kind == "import"
        else "/synthetic/De plătit/payments-EXAMPLE-new.csv"
    )
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", 32)
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO payments(item_id,company,kind,month,amount,status,csv_path,created_at) "
            "VALUES('queued','EXAMPLE','op','2026-11','12.34','exported',?,'2026-11-18')",
            (source,),
        )
        job = (
            {"src": source, "dest": target, "ids": ["queued"], "vd": "2026-11-18"}
            if kind == "import"
            else {"src": source, "dst": target}
        )
        db.execute("INSERT INTO meta VALUES(?,?)", (f"btgo_{kind}:EXAMPLE", json.dumps(job)))
    # Exercise the callback directly: startup backup separately refuses unfinished legacy journals.
    with sqlite3.connect(path) as db:
        m.migrate(db)
    with closing(Ledger(path)) as ledger:
        payment = ledger.payment("queued")
        file = ledger.payment_file(payment["file_id"])
        assert file["path"] == target
        assert bool(file["imported_at"]) == (kind == "import")
        assert payment["csv_path"] == source  # migration moves no bytes or journal steps
        assert ledger.get_meta(f"btgo_{kind}:EXAMPLE") == json.dumps(job)


def test_two_connections_cannot_claim_payment_for_two_files(ledger):
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO payments(item_id,company,kind,month,amount,status,created_at) "
            "VALUES('invoice-payment','EXAMPLE','efactura','2026-11','12.34','pending','2026-11-18')"
        )
    files = [ledger.create_payment_file("EXAMPLE", name) for name in ("First", "Second")]
    barrier = Barrier(2)

    def claim(file_id):
        with closing(Ledger(ledger.path)) as connection:
            barrier.wait(timeout=5)
            return connection.attach_payment_file(
                file_id, "EXAMPLE", "invoice-payment", origin="person" if file_id == files[0] else "auto"
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, files)) == [False, True]
    row = ledger.payment("invoice-payment")
    assert row["file_id"] in files
    assert row["origin"] == ("person" if row["file_id"] == files[0] else "auto")
    assert not ledger.delete_payment_file(ledger.payment("invoice-payment")["file_id"], "EXAMPLE")


def test_previous_schema_integrations_gain_all_firms(tmp_path, monkeypatch):
    path = tmp_path / "scope.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", m.MIGRATIONS.index(m.integration_firm_scope))
        old = Ledger(path)
        with old.tx() as db:
            for kind in ("anaf", "mail"):
                db.execute(
                    "INSERT INTO integrations VALUES(?,?,?, ?,1,1,'2026-01-01','2026-01-01')",
                    (kind + "-12345678", kind, kind, "{}"),
                )
        old.close()
    with closing(Ledger(path)) as upgraded:
        assert [json.loads(r[0])["firm_scope"] for r in upgraded.db.execute("SELECT config FROM integrations")] == [
            "all",
            "all",
        ]
        assert not upgraded.db.execute("SELECT 1 FROM sqlite_master WHERE name='integrations_one_anaf'").fetchone()


def test_the_spv_day_moves_from_the_anaf_connection_to_each_firm(tmp_path, monkeypatch):
    path = tmp_path / "delay.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", m.MIGRATIONS.index(m.spv_upload_delay_per_firm))
        old = Ledger(path)
        with old.tx() as db:
            for key, cui in (("NORD", "11"), ("SUD", "22"), ("EST", "33")):
                db.execute("INSERT INTO firms VALUES(?,?,?,?, 'blue', NULL)", (key, key, key + " SRL", cui))
            for iid, scope, days in (("anaf-aaaaaaaa", "all", "1"), ("anaf-bbbbbbbb", ["SUD"], "5")):
                config = json.dumps({"client_id": "synthetic", "firm_scope": scope, "upload_after_days": days})
                db.execute(
                    "INSERT INTO integrations VALUES(?, 'anaf', ?, ?, 1, 0, '2026-01-01', '2026-01-01')",
                    (iid, iid, config),
                )
            db.execute("INSERT INTO meta VALUES('anaf_read_connection:SUD', 'anaf-bbbbbbbb')")
            db.execute("INSERT INTO invoice_settings VALUES('EST', 0)")
        old.close()
    with closing(Ledger(path)) as upgraded:
        rows = upgraded.db.execute(
            "SELECT company, auto_upload, upload_after_days FROM invoice_settings ORDER BY company"
        )
        assert [tuple(r) for r in rows] == [("EST", 0, 1), ("NORD", 1, 1), ("SUD", 1, 5)]  # a paused firm stays paused
        configs = [json.loads(r[0]) for r in upgraded.db.execute("SELECT config FROM integrations")]
        assert all("upload_after_days" not in c for c in configs)


def test_app_statements_join_packs_on_upgrade_but_never_after_a_sent_pack(tmp_path, monkeypatch):
    path = tmp_path / "previous.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", m.MIGRATIONS.index(m.app_statements_imported))
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO packs(company,month,status) VALUES('EXAMPLE','2026-09','sent'),('EXAMPLE','2026-10','built')"
        )
        for sha, month, kind, source in (
            ("aug", "2026-08", "statement", "upload:app"),  # before a sent pack's month: it would have gone with it
            ("sep", "2026-09", "statement", "upload:app"),
            ("oct", "2026-10", "statement", "upload:app"),  # its month's pack is not sent: it goes in it now
            ("bon", "2026-10", "receipt", "upload:phone"),  # an upload link's file still waits for its card payment
        ):
            db.execute(
                "INSERT INTO documents(sha,company,month,kind,path,source,created_at) "
                "VALUES(?,'EXAMPLE',?,?,'/synthetic.pdf',?,'2026-10-01')",
                (sha, month, kind, source),
            )
    with closing(Ledger(path)) as ledger:
        assert [d["sha"] for d in pack.pending_docs(ledger, "EXAMPLE", "2026-11")] == ["oct"]
        assert sorted(json.loads(ledger.get_meta("app-statements-counted-sent"))) == ["aug", "sep"]
        assert dict(ledger.db.execute("SELECT sha, source FROM documents")) == {
            "aug": "statement-import",
            "sep": "statement-import",
            "oct": "statement-import",
            "bon": "upload:phone",
        }


def test_dropping_receipt_lines_keeps_a_confirmed_receipt_invoice_association(tmp_path, monkeypatch):
    path = tmp_path / "previous.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", m.MIGRATIONS.index(m.drop_receipt_items))
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO documents(sha,company,month,kind,path,source,created_at,efactura_id) "
            "VALUES('bon','EXAMPLE','2026-10','receipt','/synthetic.jpg','upload:phone','2026-10-01','8001')"
        )
        db.execute(
            "INSERT INTO efactura(message_id,company,raw_path,status,created_at) "
            "VALUES('8001','EXAMPLE','/synthetic.zip','open','2026-10-01')"
        )
        db.execute("INSERT INTO receipt_items VALUES('bon','EXAMPLE','{}','{}','user',1,1,'2026-10-02')")
        db.execute("INSERT INTO receipt_invoice_links VALUES('bon','8001','EXAMPLE','2026-10-02')")
    with closing(Ledger(path)) as ledger:
        assert ledger.document("bon")["efactura_id"] == "8001"
        assert not ledger.db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'receipt_i%'").fetchone()


@pytest.mark.parametrize(
    "ai, kept",
    [
        # "no AI" the enabled default: the other service stays off
        ([("ai-aaaaaaaa", "anthropic", 1, 0), ("ai-bbbbbbbb", "none", 1, 1)], [("ai-aaaaaaaa", 0)]),
        # the default is stopped, so the oldest enabled one, "no AI", was in use: a newer service stays off
        (
            [("ai-aaaaaaaa", "anthropic", 0, 1), ("ai-bbbbbbbb", "none", 1, 0), ("ai-cccccccc", "codex-cli", 1, 0)],
            [("ai-aaaaaaaa", 0), ("ai-cccccccc", 0)],
        ),
        # another service was in use: nothing changes for it
        ([("ai-aaaaaaaa", "anthropic", 1, 1), ("ai-bbbbbbbb", "none", 1, 0)], [("ai-aaaaaaaa", 1)]),
    ],
)
def test_reading_off_and_no_ai_become_a_stopped_integration(tmp_path, monkeypatch, ai, kept):
    path = tmp_path / "switch.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", m.MIGRATIONS.index(m.one_off_switch))
        old = Ledger(path)
        with old.tx() as db:
            rows = [
                ("mail-aaaaaaaa", "mail", {"read_invoices": "false", "firm_scope": "all"}, 1, 1),
                ("mail-bbbbbbbb", "mail", {"read_invoices": "true", "firm_scope": "all"}, 1, 0),
            ] + [(iid, "ai", {"provider": provider}, on, default) for iid, provider, on, default in ai]
            for n, (iid, kind, config, on, default) in enumerate(rows):
                db.execute(
                    "INSERT INTO integrations VALUES(?, ?, ?, ?, ?, ?, ?, '2026-01-01')",
                    (iid, kind, iid, json.dumps(config), on, default, f"2026-01-0{n + 1}"),
                )
        old.close()
    with closing(Ledger(path)) as upgraded:
        rows = upgraded.db.execute("SELECT id, enabled, config FROM integrations ORDER BY id").fetchall()
        assert [(r[0], r[1]) for r in rows if r[0].startswith("ai-")] == kept  # the "no AI" row is gone
        assert [(r[0], r[1], json.loads(r[2])) for r in rows if r[0].startswith("mail-")] == [
            ("mail-aaaaaaaa", 0, {"firm_scope": "all"}),
            ("mail-bbbbbbbb", 1, {"firm_scope": "all"}),
        ]


def test_credit_notes_stored_without_a_reference_are_read_again_on_upgrade(tmp_path, monkeypatch):
    """Several references were stored as none: the next sync reads every such note again (fill_credit_references)."""
    path = tmp_path / "previous.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", m.MIGRATIONS.index(m.credit_references_reread))
        Ledger(path).close()
    with sqlite3.connect(path) as db:
        for mid, status, ref in (("none", "credit", ""), ("named", "credit", "SUP 1"), ("invoice", "open", "")):
            db.execute(
                "INSERT INTO efactura(message_id,company,raw_path,status,billing_ref,created_at) "
                "VALUES(?,'EXAMPLE','synthetic',?,?,'2026-10-31')",
                (mid, status, ref),
            )
    with closing(Ledger(path)) as ledger:
        refs = dict(ledger.db.execute("SELECT message_id, billing_ref FROM efactura"))
        assert refs == {"none": None, "named": "SUP 1", "invoice": ""}


def test_stored_statements_keep_covering_their_month_on_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "previous.db"
    with monkeypatch.context() as patch:
        patch.setattr(m, "SCHEMA_VERSION", m.MIGRATIONS.index(m.bank_coverage))
        Ledger(path).close()
    a, b = "RO00XXXX0000000000000001", "RO00XXXX0000000000000002"
    with sqlite3.connect(path) as db:
        for sha, file, accounts in (
            ("camt", "/s.xml", [a, b]),  # its import refused lines that don't give the balances
            ("pdf", "/s.pdf", [a]),  # nothing proves its lines were matched: it counts once imported again
            ("csv", "/s.csv", [a]),  # a CSV proves only its own days: they are not on record
            ("old", "/s.xml", None),  # names no account: covers none
        ):
            db.execute(
                "INSERT INTO documents(sha,company,month,kind,path,accounts,created_at) "
                "VALUES(?,'EXAMPLE','2026-02','statement',?,?,'2026-03-01')",
                (sha, file, accounts and json.dumps(accounts)),
            )
    with closing(Ledger(path)) as ledger:
        got = {r[0]: r[1] and json.loads(r[1]) for r in ledger.db.execute("SELECT sha, coverage FROM documents")}
    assert got == {
        "camt": {a: ["2026-02-01", "2026-02-28", True], b: ["2026-02-01", "2026-02-28", True]},
        "pdf": None,
        "csv": None,
        "old": None,
    }
