"""Seed publication/activation/sweep, including crash and two-connection races."""

import base64
import multiprocessing as mp
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from test_auth_sessions import users as users  # noqa: F401

from fiscalm import secrets as store
from fiscalm.api import passwords, totp, totp_store
from fiscalm.ledger import Ledger


@pytest.fixture(autouse=True)
def private_seeds(tmp_path, monkeypatch):
    monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "secrets"))


@pytest.fixture
def hasher(monkeypatch):
    service = passwords.Passwords()
    monkeypatch.setattr(service, "hash", lambda _: "synthetic-password-hash")
    return service


def valid_code(pending, at):
    return totp.code(base64.b32decode(pending["secret"]), int(at.timestamp()) // 30)


def test_seed_setup_attempts_activation_and_storage(ledger, users, hasher):
    pending = totp_store.begin(ledger, "first", "synthetic-password", hasher, users)
    assert pending["uri"].startswith("otpauth://totp/fiscalm:first?secret=")
    path = next(store.secrets_dir().glob("auth-totp-*"))
    assert path.stat().st_mode & 0o777 == 0o600
    with ledger.tx(), pytest.raises(ValueError):
        totp_store.sweep(ledger, users)
    totp_store.sweep(ledger, users)
    assert path.exists()
    wrong = "000000" if valid_code(pending, users) != "000000" else "111111"
    assert not totp_store.finish(ledger, pending["ticket"], wrong, users)
    assert ledger.db.execute("SELECT attempts FROM auth_tickets").fetchone()[0] == 1
    assert not totp_store.finish(
        ledger, pending["ticket"], valid_code(pending, users), users, session_hash="another-session"
    )
    assert totp_store.finish(ledger, pending["ticket"], valid_code(pending, users), users)
    assert not totp_store.finish(ledger, pending["ticket"], valid_code(pending, users), users)
    user = ledger.db.execute("SELECT * FROM users WHERE id='first'").fetchone()
    assert totp_store.seed("first", user["totp_version"]) == base64.b32decode(pending["secret"])
    assert not ledger.consume_totp("first", user["totp_version"], int(users.timestamp()) // 30)
    assert pending["secret"] not in str([tuple(r) for r in ledger.db.execute("SELECT * FROM auth_events")])


def test_activation_race_and_rollback_keep_old_seed(ledger, users, hasher, monkeypatch):
    original = totp_store.begin(ledger, "first", "synthetic-password", hasher, users)
    assert totp_store.finish(ledger, original["ticket"], valid_code(original, users), users)
    old = next(store.secrets_dir().glob("auth-totp-*"))
    later = users + timedelta(seconds=30)
    monkeypatch.setattr(hasher, "hash", lambda _: "replacement-password-hash")
    pending = [totp_store.begin(ledger, "first", "synthetic-password", hasher, later) for _ in range(2)]
    with pytest.raises(ValueError), ledger.tx():
        assert totp_store.finish(ledger, pending[0]["ticket"], valid_code(pending[0], later), later)
        with pytest.raises(ValueError):
            totp_store.sweep(ledger, later)
        raise ValueError("synthetic outer activation failure")
    assert old.exists()
    barrier = Barrier(2)

    def activate(p):
        other = Ledger(ledger.path)
        try:
            barrier.wait(timeout=5)
            return totp_store.finish(other, p["ticket"], valid_code(p, later), later)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(activate, pending)) == [False, True]
    assert old.exists()  # No cleanup before the caller's activation transaction commits.
    assert ledger.db.execute("SELECT count(*) FROM auth_events WHERE kind='password_changed'").fetchone()[0] == 1
    totp_store.sweep(ledger, later + timedelta(minutes=6))
    assert not old.exists() and len(list(store.secrets_dir().glob("auth-totp-*"))) == 1


def test_seed_failures_disabled_expiry_and_path_refusals(ledger, users, hasher, monkeypatch):
    pending = totp_store.begin(ledger, "first", "synthetic-password", hasher, users)
    path = next(store.secrets_dir().glob("auth-totp-*"))
    path.write_text("not-base32")
    assert not totp_store.finish(ledger, pending["ticket"], "123456", users)
    path.write_text(base64.b32encode(b"short").decode())
    assert not totp_store.finish(ledger, pending["ticket"], "123456", users)
    path.unlink()
    path.symlink_to(ledger.path)
    assert not totp_store.finish(ledger, pending["ticket"], "123456", users)
    with pytest.raises(ValueError):
        totp_store.begin(ledger, "../escape", "synthetic-password", hasher, users)
    for owner in ("missing", "second"):
        ledger.db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='second'")
        ledger.db.commit()
        with pytest.raises(PermissionError):
            totp_store.begin(ledger, owner, "synthetic-password", hasher, users)
    assert not totp_store.finish(ledger, "bad", "123456", users)
    totp_store.sweep(ledger, users + timedelta(minutes=5))
    assert not path.exists() and not path.is_symlink()

    def fail(name, value):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(store, "write_secret", fail)
    with pytest.raises(OSError):
        totp_store.begin(ledger, "first", "synthetic-password", hasher, users)


def test_crash_after_publish_before_commit_preserves_old_factor(ledger, users, hasher):
    pending = totp_store.begin(ledger, "first", "synthetic-password", hasher, users)
    assert totp_store.finish(ledger, pending["ticket"], valid_code(pending, users), users)
    version = ledger.db.execute("SELECT totp_version FROM users WHERE id='first'").fetchone()[0]
    old_seed = totp_store.seed("first", version)

    def crash():
        other = Ledger(ledger.path)
        write = store.write_secret

        def write_then_crash(name, value):
            write(name, value)
            os._exit(17)

        store.write_secret = write_then_crash
        totp_store.begin(other, "first", "synthetic-password", hasher, users)

    process = mp.get_context("fork").Process(target=crash)
    process.start()
    process.join(10)
    assert process.exitcode == 17
    assert totp_store.seed("first", version) == old_seed
    assert len(list(store.secrets_dir().glob("auth-totp-*"))) == 2
    totp_store.sweep(ledger, users)
    assert len(list(store.secrets_dir().glob("auth-totp-*"))) == 1


@pytest.mark.parametrize("phase", ["publish", "activate"])
def test_sweep_cannot_interleave_publish_or_activation(ledger, users, hasher, monkeypatch, phase):
    original = totp_store.begin(ledger, "first", "synthetic-password", hasher, users)
    assert totp_store.finish(ledger, original["ticket"], valid_code(original, users), users)
    pending = totp_store.begin(ledger, "first", "synthetic-password", hasher, users) if phase == "activate" else None
    paused, resume = Event(), Event()

    def pause():
        paused.set()
        assert resume.wait(timeout=5)

    if phase == "publish":
        write = store.write_secret

        def blocked_write(name, value):
            write(name, value)
            pause()

        monkeypatch.setattr(store, "write_secret", blocked_write)
    else:
        activate = Ledger.activate_totp

        def blocked_activate(self, *args):
            result = activate(self, *args)
            pause()
            return result

        monkeypatch.setattr(Ledger, "activate_totp", blocked_activate)

    def operation():
        other = Ledger(ledger.path)
        try:
            if pending:
                return totp_store.finish(other, pending["ticket"], valid_code(pending, users), users)
            return totp_store.begin(other, "first", "synthetic-password", hasher, users)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operation)
        try:
            assert paused.wait(timeout=5)
            other = Ledger(ledger.path, timeout=0.01)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    totp_store.sweep(other, users)
            finally:
                other.close()
        finally:
            resume.set()
        assert future.result(timeout=5)
    totp_store.sweep(ledger, users)
    active = ledger.db.execute("SELECT totp_version FROM users WHERE id='first'").fetchone()[0]
    assert len(totp_store.seed("first", active)) == 20


@pytest.mark.parametrize("temporary", [False, True])
def test_sweep_preserves_foreign_users_pending_seeds_and_directories(ledger, users, hasher, tmp_path, temporary):
    active = totp_store.begin(ledger, "first", "synthetic-password", hasher, users)
    assert totp_store.finish(ledger, active["ticket"], valid_code(active, users), users)
    pending = totp_store.begin(ledger, "second", "synthetic-password", hasher, users)
    root = store.secrets_dir()
    orphan = f"auth-totp-first-{'a' * 32}"
    if temporary:
        orphan = f".{orphan}.12345678"
    store.write_secret(orphan, "synthetic-unused-seed")
    foreign = f"auth-totp-foreign-{'b' * 32}"
    store.write_secret(foreign, "belongs-to-another-ledger")
    store.write_secret("integration-secret", "unrelated")
    directory = root / f"auth-totp-first-{'c' * 32}"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    link = root / f"auth-totp-first-{'d' * 32}"
    link.symlink_to(outside)
    store.write_secret(".auth-totp-first-bad", "unrecognized-file")
    totp_store.sweep(ledger, users)
    assert not (root / orphan).exists() and not link.is_symlink()
    assert directory.is_dir() and outside.read_text() == "untouched"
    assert (root / foreign).read_text() == "belongs-to-another-ledger"
    assert (root / "integration-secret").read_text() == "unrelated"
    assert (root / ".auth-totp-first-bad").exists()
    totp_store.sweep(ledger, users + timedelta(minutes=6))
    assert len(list(root.glob("auth-totp-second-*"))) == 0
    assert not totp_store.finish(ledger, pending["ticket"], valid_code(pending, users), users + timedelta(minutes=6))
    assert totp_store.seed("first", ledger.db.execute("SELECT totp_version FROM users WHERE id='first'").fetchone()[0])


@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "owner"])
def test_sweep_refuses_unsafe_secrets_directory(ledger, users, tmp_path, monkeypatch, unsafe):
    root = store.secrets_dir()
    root.mkdir(mode=0o700)
    if unsafe == "permissions":
        root.chmod(0o755)
    elif unsafe == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        monkeypatch.setenv("FISCALM_SECRETS", str(alias))
    else:
        monkeypatch.setattr(totp_store.os, "getuid", lambda: -1)
    with pytest.raises(ValueError):
        totp_store.sweep(ledger, users)


def test_crash_during_private_write_leaves_temporary_seed_for_sweep(ledger, users, hasher):
    def crash():
        other = Ledger(ledger.path)
        os.replace = lambda *args: os._exit(17)
        totp_store.begin(other, "first", "synthetic-password", hasher, users)

    process = mp.get_context("fork").Process(target=crash)
    process.start()
    process.join(10)
    assert process.exitcode == 17
    assert len(list(store.secrets_dir().glob(".auth-totp-*"))) == 1
    assert ledger.db.execute("SELECT count(*) FROM auth_tickets").fetchone()[0] == 0
    totp_store.sweep(ledger, users)
    assert not list(store.secrets_dir().iterdir())
