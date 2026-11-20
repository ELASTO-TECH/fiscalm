import contextlib
import io
import json
import os
import shutil
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest
from test_api import server  # noqa: F401

from fiscalm import backup, companies, scheduler
from fiscalm.ledger import Ledger
from fiscalm.migrations import SchemaError


@pytest.fixture
def books(home, monkeypatch):
    monkeypatch.setenv("FISCALM_SECRETS", str(home / "secrets"))
    ledger = Ledger()
    ledger.set_meta("example", "value")
    ledger.close()
    doc = home / "NORD/2026-01/document.xml"
    doc.parent.mkdir(parents=True)
    doc.write_text("synthetic document")
    (home / "state/LIVE").touch()
    (home / "secrets").mkdir()
    (home / "secrets/key").write_text("synthetic-secret")
    return home


def test_online_backup_while_writing_and_shadow_restore(books, tmp_path, monkeypatch):
    alias = tmp_path / "home-link"
    alias.symlink_to(books, target_is_directory=True)
    monkeypatch.setenv("FISCALM_HOME", str(alias))
    mail = books / "mail/pending/question.xml"
    mail.parent.mkdir(parents=True)
    mail.write_text("pending document")
    with sqlite3.connect(books / "state/ledger.db") as db:
        db.execute(
            "INSERT INTO documents(sha,company,month,kind,path,created_at) VALUES('sha','NORD','2026-01','receipt',?,'2026-01-01')",
            (str(alias / "NORD/2026-01/document.xml"),),
        )
    start, wrote = threading.Event(), threading.Event()
    errors = []

    def writer():
        try:
            start.wait(10)
            ledger = Ledger()  # the real default five-second busy timeout
            ledger.set_meta("during_backup", "written")
            ledger.close()
        except sqlite3.Error as error:
            errors.append(error)
        finally:
            wrote.set()

    read = backup._read

    def concurrent_read(path):
        if path == books / "NORD/2026-01/document.xml":
            start.set()
            assert wrote.wait(7), "writer blocked during document compression"
        return read(path)

    monkeypatch.setattr(backup, "_read", concurrent_read)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        archive = backup.create(tmp_path / "copy.zip")
    finally:
        start.set()
        thread.join(10)
    assert not errors
    target = tmp_path / "restored"
    backup.restore(archive, target)
    assert not (target / "state/LIVE").exists()
    assert (target / "NORD/2026-01/document.xml").read_text() == "synthetic document"
    assert (target / "mail/pending/question.xml").read_text() == "pending document"
    assert not (target / "secrets").exists()
    with sqlite3.connect(target / "state/ledger.db") as db:
        assert db.execute("SELECT path FROM documents").fetchone()[0] == str(target / "NORD/2026-01/document.xml")
        assert db.execute("SELECT value FROM meta WHERE key='during_backup'").fetchone() is None
    ledger = Ledger(target / "state/ledger.db")
    assert companies.all(ledger) == companies.all()
    ledger.close()


def empty_volumes(tmp_path, monkeypatch):
    """Docker: /data and /secrets are existing empty mount points, here 0755 so the restore must tighten them."""
    target, secrets = tmp_path / "data", tmp_path / "secrets-volume"
    monkeypatch.setenv("FISCALM_SECRETS", str(secrets))
    for d in (target, secrets):
        d.mkdir(mode=0o755)
    return target, secrets


def listing(*roots):
    return sorted(str(p) for r in roots for p in r.rglob("*"))


@pytest.mark.parametrize("volumes", [False, True])
def test_encrypted_secrets_and_wrong_passphrase_leave_target_empty(books, tmp_path, monkeypatch, volumes):
    archive = backup.create(tmp_path / "encrypted.zip", "passphrase")
    with zipfile.ZipFile(archive) as z:
        assert all(b"synthetic-secret" not in z.read(n) for n in z.namelist())
    target, secrets = empty_volumes(tmp_path, monkeypatch) if volumes else (tmp_path / "restore",) * 2
    secrets = secrets if volumes else target / "secrets"
    monkeypatch.setenv("FISCALM_SECRETS", str(secrets))
    for password in (None, "wrong") + (("passphrase",) if volumes else ()):
        if password == "passphrase":
            (secrets / "other").touch()  # a non-empty secrets directory refuses the whole restore
        with pytest.raises(backup.BackupError):
            backup.restore(archive, target, password)
        assert target.exists() == volumes and listing(target) == []
        assert listing(secrets) == ([str(secrets / "other")] if password == "passphrase" else [])
    (secrets / "other").unlink(missing_ok=True)
    old = os.umask(0o022)
    try:
        backup.restore(archive, target, "passphrase")
    finally:
        os.umask(old)
    assert (secrets / "key").read_text() == "synthetic-secret"
    assert sorted(p.name for p in target.iterdir()) == (["NORD", "state"] if volumes else ["NORD", "secrets", "state"])
    for path in (target, secrets, *target.rglob("*"), *secrets.rglob("*")):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600), path


@pytest.mark.parametrize("stop", ["between entries", "after state", "secrets stage left"])
def test_interrupted_restore_refuses_every_entry_point(books, tmp_path, monkeypatch, stop):
    archive = backup.create(tmp_path / "copy.zip")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups/fiscalm-20200101T000000000000Z.zip").write_bytes(b"older good archive")
    target, secrets = tmp_path / "data", tmp_path / "secrets-volume"
    monkeypatch.setenv("FISCALM_SECRETS", str(secrets))
    with monkeypatch.context() as m:
        if stop == "after state":  # the final flush fails: everything is published, the stage is still there
            m.setattr(backup.os, "sync", lambda: (_ for _ in ()).throw(OSError("killed")))
        with pytest.raises(OSError) if stop == "after state" else contextlib.nullcontext():
            backup.restore(archive, target)
    if stop == "between entries":  # what a kill between two renames leaves
        (target / ".fiscalm-restore").mkdir()
        (target / "state").rename(target / ".fiscalm-restore/state")
    elif stop == "secrets stage left":  # the home looks complete; the secrets directory still has a stage
        (secrets / ".fiscalm-restore").mkdir(parents=True)
    before = listing(target, secrets, tmp_path / "backups")
    monkeypatch.setenv("FISCALM_HOME", str(target))
    monkeypatch.setenv("FISCALM_BACKUPS", str(tmp_path / "backups"))
    for refused in (
        Ledger,
        lambda: Ledger(target / "state/ledger.db", readonly=True),
        lambda: backup.create(tmp_path / "manual.zip"),
        lambda: backup.scheduled(1),
        backup.latest,
        lambda: backup.restore(archive, target),
        scheduler.tick,
        lambda: scheduler.ticks(60, ["true"]),
        lambda: scheduler.backups(60, 1),
    ):
        with pytest.raises(SchemaError, match="did not finish"):
            refused()
    assert listing(target, secrets, tmp_path / "backups") == before and not (tmp_path / "manual.zip").exists()
    shutil.rmtree(target), shutil.rmtree(secrets, ignore_errors=True)
    backup.restore(archive, target)
    Ledger().close()


@pytest.mark.parametrize("exist", [True, False])
def test_publish_order_is_durable(books, tmp_path, monkeypatch, exist):
    archive = backup.create(tmp_path / "encrypted.zip", "passphrase")
    target, secrets = (tmp_path / "data", tmp_path / "vol") if exist else (tmp_path / "new/data", tmp_path / "s/new")
    monkeypatch.setenv("FISCALM_SECRETS", str(secrets))
    for d in (target, secrets) if exist else ():
        d.mkdir()
    events, fsync, rmdir = [], os.fsync, os.rmdir

    def seen():  # what is published: everything in the destinations but the stage
        return sorted(
            p.name for r in (target, secrets) if r.is_dir() for p in r.iterdir() if p.name != ".fiscalm-restore"
        )

    def record(kind, path):
        events.append((kind, path, seen(), (target / ".fiscalm-restore").exists()))

    with monkeypatch.context() as m:
        m.setattr(backup.os, "fsync", lambda fd: (record("fsync", Path(os.readlink(f"/proc/self/fd/{fd}"))), fsync(fd)))
        m.setattr(backup.os, "sync", lambda: record("sync", None))
        m.setattr(backup.os, "rmdir", lambda d: (record("rmdir", Path(d)), rmdir(d)))
        backup.restore(archive, target, "passphrase")
    synced_first = {path for kind, path, published, _ in events if kind == "fsync" and not published}
    made = set() if exist else {tmp_path, target.parent, secrets.parent}
    assert {target, target / ".fiscalm-restore", secrets, secrets / ".fiscalm-restore"} | made <= synced_first
    flush = next(i for i, e in enumerate(events) if e[0] == "sync")
    assert events[flush][2] == ["NORD", "key", "state"] and events[flush][3]
    assert ("fsync", target, ["NORD", "key", "state"], True) in events[:flush]
    assert ("rmdir", target / ".fiscalm-restore", ["NORD", "key", "state"], True) in events[flush:]
    assert events[-1] == ("fsync", target, ["NORD", "key", "state"], False)


def test_publish_never_replaces_a_competitor(books, tmp_path, monkeypatch):
    archive = backup.create(tmp_path / "encrypted.zip", "passphrase")
    target, secrets = empty_volumes(tmp_path, monkeypatch)
    publish_all = backup._publish

    def competing(claims):
        (secrets / "key").write_text("competitor")  # after the emptiness check, before the first rename
        publish_all(claims)

    monkeypatch.setattr(backup, "_publish", competing)
    with pytest.raises(backup.BackupError, match="not replaced"):
        backup.restore(archive, target, "passphrase")
    assert (secrets / "key").read_text() == "competitor"
    (tmp_path / "file").touch(), (tmp_path / "empty").mkdir()
    with pytest.raises(backup.BackupError, match="not replaced"):
        backup._move(tmp_path / "file", tmp_path / "empty")  # a plain rename would replace an empty directory
    assert (tmp_path / "file").exists()


@pytest.mark.parametrize("window", ["after the guard", "after the emptiness check"])
def test_foreign_stage_is_never_adopted_or_removed(books, tmp_path, monkeypatch, window):
    archive = backup.create(tmp_path / "copy.zip")
    target = tmp_path / "data"
    target.mkdir()
    foreign = target / ".fiscalm-restore"

    def appear():
        if not foreign.exists():
            foreign.mkdir()
            (foreign / "file").write_text("another restore")

    if window == "after the guard":
        guard = backup.refuse_unfinished_restore
        monkeypatch.setattr(backup, "refuse_unfinished_restore", lambda home: (guard(home), appear()))
    else:
        scandir = os.scandir
        monkeypatch.setattr(
            backup.os,
            "scandir",
            lambda d=".": (list(scandir(d)), appear())[0] if Path(d) == target and not foreign.exists() else scandir(d),
        )
    with pytest.raises(backup.BackupError, match="empty"):
        backup.restore(archive, target)
    assert (
        listing(target) == [str(foreign), str(foreign / "file")] and (foreign / "file").read_text() == "another restore"
    )


def test_failed_cleanup_keeps_the_marker_and_a_secrets_stage_refuses(books, tmp_path, monkeypatch):
    archive = backup.create(tmp_path / "encrypted.zip", "passphrase")
    target, secrets = empty_volumes(tmp_path, monkeypatch)
    write = backup._write

    def failing(z, infos, root, expected):
        write(z, infos, root, expected)
        if root.parent == secrets:
            os.chmod(root / "secrets", 0o500)  # the cleanup cannot empty it
            raise OSError("injected")

    monkeypatch.setattr(backup, "_write", failing)
    with pytest.raises(backup.BackupError, match="could not remove"):
        backup.restore(archive, target, "passphrase")
    assert (target / ".fiscalm-restore").is_dir()
    monkeypatch.setenv("FISCALM_HOME", str(target))
    os.chmod(secrets / ".fiscalm-restore/secrets", 0o700)
    shutil.rmtree(target / ".fiscalm-restore")  # the home now looks empty and reusable
    with pytest.raises(SchemaError, match=str(secrets)):
        Ledger()
    shutil.rmtree(secrets / ".fiscalm-restore")
    secrets.chmod(0)  # unreadable: nothing to see, no refusal and no other error
    try:
        Ledger().close()
    finally:
        secrets.chmod(0o700)


def rewrite(archive, mutate):
    with zipfile.ZipFile(archive) as z:
        entries = {n: z.read(n) for n in z.namelist()}
    mutate(entries)
    with zipfile.ZipFile(archive, "w") as z:
        for n, data in entries.items():
            z.writestr(n, data)


@pytest.mark.parametrize("volumes", [False, True])
def test_encrypted_payload_refuses_configuration_outside_secrets(books, tmp_path, monkeypatch, volumes):
    archive = backup.create(tmp_path / "encrypted.zip", "passphrase")
    target, secrets = empty_volumes(tmp_path, monkeypatch) if volumes else (tmp_path / "restore", None)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as z:
        z.writestr("secrets/key", "synthetic-secret")
        z.writestr("config/web.json", "unexpected configuration")

    def mutate(entries):
        entries["secrets.enc"] = backup._crypt(payload.getvalue(), "passphrase")
        manifest = json.loads(entries["manifest.json"])
        manifest["sha256"]["secrets.enc"] = backup.hashlib.sha256(entries["secrets.enc"]).hexdigest()
        entries["manifest.json"] = json.dumps(manifest).encode()

    rewrite(archive, mutate)
    with pytest.raises(backup.BackupError):
        backup.restore(archive, target, "passphrase")
    assert target.exists() == volumes and listing(target) == []
    assert not volumes or listing(secrets) == []  # both destinations exactly as they were


@pytest.mark.parametrize(
    "kind",
    [
        "checksum",
        "traversal",
        "state/LIVE",
        "newer",
        "integrity",
        "duplicate",
        "service.env",
        "config/unknown.json",
        "secrets/plain",
        "ledger_path_escape",
        "ledger_path_external",
    ],
)
def test_corrupt_or_unsafe_archive_refused(books, tmp_path, kind):
    archive = backup.create(tmp_path / "copy.zip")

    def mutate(entries):
        manifest = json.loads(entries["manifest.json"])
        if kind in ("checksum", "integrity"):
            entries["state/ledger.db"] = b"not a database"
            if kind == "integrity":
                manifest["sha256"]["state/ledger.db"] = backup.hashlib.sha256(entries["state/ledger.db"]).hexdigest()
        elif kind in (
            "traversal",
            "state/LIVE",
            "service.env",
            "config/unknown.json",
            "secrets/plain",
        ):
            name = "../outside" if kind == "traversal" else kind
            entries[name] = b"x"
            manifest["sha256"][name] = backup.hashlib.sha256(b"x").hexdigest()
        elif kind.startswith("ledger_path_"):
            with sqlite3.connect(":memory:") as db:
                db.deserialize(entries["state/ledger.db"])
                path = str(books / "../outside") if kind.endswith("escape") else "/outside/source.xml"
                db.execute(
                    "INSERT INTO documents(sha,company,month,kind,path,created_at) VALUES('escape','NORD','2026-01','receipt',?,'2026-01-01')",
                    (path,),
                )
                db.commit()
                entries["state/ledger.db"] = db.serialize()
            manifest["sha256"]["state/ledger.db"] = backup.hashlib.sha256(entries["state/ledger.db"]).hexdigest()
        elif kind == "newer":
            with sqlite3.connect(":memory:") as db:
                db.deserialize(entries["state/ledger.db"])
                db.execute("PRAGMA user_version=99999")
                entries["state/ledger.db"] = db.serialize()
            manifest["sha256"]["state/ledger.db"] = backup.hashlib.sha256(entries["state/ledger.db"]).hexdigest()
            manifest["schema"] = 99999
        entries["manifest.json"] = json.dumps(manifest).encode()

    rewrite(archive, mutate)
    if kind == "duplicate":
        with zipfile.ZipFile(archive, "a") as z:
            z.writestr("state/ledger.db", b"{}")
    with pytest.raises((ValueError, sqlite3.Error)):
        backup.restore(archive, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()
    assert not (tmp_path / "outside").exists()


def test_destination_and_symlink_refusals(books, tmp_path):
    archive = backup.create(tmp_path / "copy.zip")
    with pytest.raises(FileExistsError):
        backup.create(archive)
    target = tmp_path / "restore"
    target.mkdir()
    (target / "keep").touch()
    with pytest.raises(backup.BackupError, match="empty"):
        backup.restore(archive, target)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    assert backup.create(alias / "through-link.zip") == tmp_path / "through-link.zip"
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(books, target_is_directory=True)
    with pytest.raises(backup.BackupError, match="outside document"):
        backup.create(home_alias / "NORD/inside.zip")
    (books / "NORD/link").symlink_to(books / "state/ledger.db")
    with pytest.raises(backup.BackupError, match="symbolic"):
        backup.create(tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


def test_changing_document_refused(books, tmp_path, monkeypatch):
    read = backup._read
    path = books / "NORD/2026-01/document.xml"

    def changing(p):
        data = read(p)
        if p == path:
            p.write_text("changed")
        return data

    monkeypatch.setattr(backup, "_read", changing)
    with pytest.raises(backup.BackupError, match="changed"):
        backup.create(tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


def test_retention_only_after_success(books, tmp_path, monkeypatch):
    dest = tmp_path / "backups"
    monkeypatch.setenv("FISCALM_BACKUPS", str(dest))
    first = backup.scheduled(keep=1)
    second = backup.scheduled(keep=1)
    assert not first.exists() and second.exists()
    assert backup.latest() == second
    with pytest.raises(backup.BackupError):
        backup.scheduled(keep=0)
    assert second.exists()


@pytest.mark.parametrize("given", ["environment", "--home", "--home twice"])
def test_cli_restore_without_config(books, tmp_path, given):
    import subprocess

    archive = backup.create(tmp_path / "copy.zip")
    target = tmp_path / "fresh"
    args = {"environment": [], "--home": ["--home", str(target)], "--home twice": ["--home", str(target)] * 2}[given]
    env = {**os.environ, "FISCALM_HOME": str(target if given == "environment" else tmp_path / "unused")}
    result = subprocess.run(["fiscalm", *args, "restore", str(archive)], env=env, capture_output=True, check=False)
    assert (result.returncode == 0) == (given != "--home twice"), result.stderr
    assert (target / "state/ledger.db").exists() == (given != "--home twice") and not (target / "state/LIVE").exists()
    assert not (tmp_path / "unused").exists()


COMMANDS = (
    ["serve"],
    ["init"],
    ["live", "status"],
    ["live", "on"],
    ["user", "list"],
    ["auth", "sweep"],
    ["tick"],
    ["scheduler"],
    ["scheduler", "--backup"],
    ["backup", "manual.zip"],
    ["company", "check"],
    ["status"],
    ["restore", "copy.zip"],
)


@pytest.mark.parametrize("stage", ["home, no ledger yet", "secrets directory only"])
def test_every_command_refuses_an_unfinished_restore(books, tmp_path, monkeypatch, capsys, stage):
    from fiscalm import cli
    from fiscalm.api import web

    archive = backup.create(tmp_path / "copy.zip")
    target, secrets = tmp_path / "data", tmp_path / "secrets-volume"
    monkeypatch.setenv("FISCALM_SECRETS", str(secrets))
    backup.restore(archive, target)
    if stage.startswith("home"):
        (target / ".fiscalm-restore").mkdir()
        (target / "state").rename(target / ".fiscalm-restore/state")
    else:
        (secrets / ".fiscalm-restore").mkdir(parents=True)
    monkeypatch.setenv("FISCALM_HOME", str(target))
    monkeypatch.chdir(tmp_path)

    def reached(*_):
        raise AssertionError("an unguarded command got past its start")

    for seam in ((web, "serve"), (scheduler, "_requested"), (backup, "scheduled"), (scheduler.signal, "signal")):
        monkeypatch.setattr(*seam, reached if seam[1] != "signal" else lambda *_: None)
    before, umask = listing(target, secrets), os.umask(0o022)
    try:
        for command in COMMANDS:
            assert cli.main(command) != 0, command
            assert "did not finish" in capsys.readouterr().err, command
    finally:
        os.umask(umask)
    assert listing(target, secrets) == before and not (tmp_path / "manual.zip").exists()


def test_admin_only_backup_download(server, tmp_path, monkeypatch):  # noqa: F811
    from test_api import _req

    dest = tmp_path / "backups"
    dest.mkdir()
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv("FISCALM_BACKUPS", str(tmp_path / "alias/backups"))
    assert _req(server, "GET", "/web/api/backup")[2] == b'{"last_backup": null}'
    path = dest / "fiscalm-20261103.zip"
    path.write_bytes(b"archive")
    assert _req(server, "GET", "/web/backup")[2] == b"archive"
    assert json.loads(_req(server, "GET", "/web/api/backup")[2])["last_backup"]
    for route in ("backup", "api/backup"):
        assert _req(server, "GET", "/web/" + route, headers={"Cookie": "__Host-session=" + "b" * 64})[0] == 403
    assert _req(server, "GET", "/c/invalid/backup")[0] == 404


@pytest.mark.parametrize(
    "journal",
    [
        "btgo_import:NORD",
        "payment_import:1",
        "payment_delete:1",
        "payment_prepare:1",
        "payment_shadow:payments-NORD.csv",
    ],
)
def test_pending_move_cannot_restore_into_production(books, tmp_path, journal):
    archive = backup.create(tmp_path / "copy.zip")
    with sqlite3.connect(books / "state/ledger.db") as db:
        db.execute(
            "INSERT INTO meta VALUES(?, ?)",
            (journal, json.dumps({"src": str(books / "NORD/2026-01/document.xml")})),
        )
    with pytest.raises(backup.BackupError, match="Unfinished"):
        backup.create(tmp_path / "pending.zip")

    def add_journal(entries):
        entries["state/ledger.db"] = (books / "state/ledger.db").read_bytes()
        manifest = json.loads(entries["manifest.json"])
        manifest["sha256"]["state/ledger.db"] = backup.hashlib.sha256(entries["state/ledger.db"]).hexdigest()
        entries["manifest.json"] = json.dumps(manifest).encode()

    rewrite(archive, add_journal)
    with pytest.raises(backup.BackupError, match="Unfinished"):
        backup.restore(archive, tmp_path / "restore")
    assert (books / "NORD/2026-01/document.xml").read_text() == "synthetic document"


def test_firm_added_during_backup_refuses_publication(books, tmp_path, monkeypatch):
    original = backup._write_archive

    def changed(*args):
        # A real writer adds the identity after the SQLite snapshot was taken.
        L = Ledger()
        try:
            L.save_company(companies.Company("NEW", "Firma Noua SRL", "12345674", ()))
        finally:
            L.close()
        return original(*args)

    monkeypatch.setattr(backup, "_write_archive", changed)
    output = tmp_path / "changed.zip"
    with pytest.raises(backup.BackupError, match="changed"):
        backup.create(output)
    assert not output.exists()
