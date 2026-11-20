"""Portable, checked snapshots. Restore never opens a writable production ledger."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import getpass
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from fiscalm import migrations
from fiscalm.migrations import SCHEMA_VERSION, SchemaError, check_version, read_version
from fiscalm.paths import finance_home
from fiscalm.secrets import secrets_dir

DOCUMENT_ROOTS = ("efactura", "history", "inbox", "mail", "De plătit")
RESTORING = ".fiscalm-restore"
AT_FDCWD, RENAME_NOREPLACE = -100, 1  # <fcntl.h>, <linux/fs.h>


class BackupError(ValueError):
    pass


def directory() -> Path:
    # Resolved: archives are read without following links, so a symlink on the way to them must not be in the path.
    return Path(os.environ.get("FISCALM_BACKUPS", str(finance_home() / "backups"))).expanduser().resolve()


def _read(path: Path) -> bytes:
    # Do not follow even an in-root symlink. Open first, then verify the descriptor.
    path = path.absolute()
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise BackupError("Backup refuses non-regular files.")
        return f.read()


def _files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise BackupError("Backup refuses symbolic links.")
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise BackupError("Backup refuses symbolic links.")
        if p.is_file():
            out.append(p)
        elif not p.is_dir():
            raise BackupError("Backup refuses non-regular files.")
    return out


def _crypt(data: bytes, password: str, *, decrypt: bool = False) -> bytes:
    if not password:
        raise BackupError("A non-empty passphrase is required.")
    salt, nonce = (data[:16], data[16:28]) if decrypt else (os.urandom(16), os.urandom(12))
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
    try:
        return (
            AESGCM(key).decrypt(nonce, data[28:], b"fiscalm-backup-1")
            if decrypt
            else (salt + nonce + AESGCM(key).encrypt(nonce, data, b"fiscalm-backup-1"))
        )
    except (InvalidTag, ValueError):
        raise BackupError("Wrong passphrase or damaged encrypted secrets.") from None


def _check(db: sqlite3.Connection) -> None:
    if (
        db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
        or db.execute("PRAGMA foreign_key_check").fetchone()
    ):
        raise BackupError("Ledger integrity check failed.")


def _roots(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='firms'").fetchone():
            raise BackupError(
                "This schema predates the company registry; a complete automatic backup is unavailable. "
                "Use the previous release to make and verify a complete installation backup first."
            )
        keys = [r[0] for r in db.execute("SELECT key FROM firms ORDER BY rowid")]
    if any(not isinstance(k, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,19}", k) or k == "ALL" for k in keys):
        raise BackupError("Invalid company key in ledger.")
    return (*DOCUMENT_ROOTS, *keys)


def _inventory(home: Path, roots: tuple[str, ...]) -> dict[Path, tuple[int, ...]]:
    files = {}
    for name in roots:
        for p in _files(home / name):
            st = p.stat()
            files[p] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return files


def _check_journals(db: sqlite3.Connection) -> None:
    if db.execute(
        "SELECT 1 FROM meta WHERE value != '' AND "
        "(key GLOB 'btgo_import:*' OR key GLOB 'btgo_merge:*' OR key GLOB 'statement_import:*' OR key GLOB 'payment_import:*' OR key GLOB 'payment_delete:*' OR key GLOB 'payment_prepare:*' OR key GLOB 'payment_shadow:*') LIMIT 1"
    ).fetchone():
        raise BackupError("Unfinished file move; finish recovery on the source before backing up or restoring.")


def _secret_payload(password: str) -> bytes:
    secrets = secrets_dir()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as z:
        for p in _files(secrets):
            z.writestr("secrets/" + p.relative_to(secrets).as_posix(), _read(p))
    return _crypt(payload.getvalue(), password)


def _write_archive(
    stage: Path,
    home: Path,
    roots: tuple[str, ...],
    files: dict[Path, tuple[int, ...]],
    password: str | None,
    source_path: Path,
) -> None:
    hashes = {}
    secrets = secrets_dir().resolve()
    with zipfile.ZipFile(stage / "archive", "w", compression=zipfile.ZIP_DEFLATED) as z:

        def add(name: str, data: bytes) -> None:
            hashes[name] = hashlib.sha256(data).hexdigest()
            z.writestr(name, data)

        add("state/ledger.db", _read(stage / "ledger.db"))
        for p in files:
            if p.resolve().is_relative_to(secrets):
                raise BackupError("Secrets overlap the document directory.")
            add(p.relative_to(home).as_posix(), _read(p))
        if password is not None:
            add("secrets.enc", _secret_payload(password))
        if files != _inventory(home, roots) or roots != _roots(source_path):
            raise BackupError("Files changed during backup; retry when the current import finishes.")
        z.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": 1,
                    "schema": read_version(stage / "ledger.db"),
                    "created_at": datetime.now(UTC).isoformat(),
                    "source_homes": list(
                        dict.fromkeys(
                            (
                                str(home),
                                str(finance_home()),
                                str(finance_home().absolute()),
                            )
                        )
                    ),
                    "sha256": hashes,
                },
                ensure_ascii=False,
            ),
        )


def create(output: Path, password: str | None = None, *, source_path: Path | None = None) -> Path:
    """Online SQLite snapshot plus unchanged document trees; never hold a writer lock while compressing."""
    home = finance_home().resolve()
    refuse_unfinished_restore(home)
    source_path = (source_path or home / "state/ledger.db").resolve()
    roots = _roots(source_path)
    output = output.expanduser().absolute()
    # The directory is resolved, so a symlink on the way to it neither breaks the backup nor hides a document
    # directory from the check below; the archive's own name is never followed.
    output = output.parent.resolve() / output.name
    if any(output.is_relative_to(home / name) for name in roots):
        raise BackupError("Backup destination must be outside document directories.")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".fiscalm-backup-", dir=output.parent) as tmp:
        stage = Path(tmp)
        before = _inventory(home, roots)
        with (
            closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(stage / "ledger.db")) as snapshot,
        ):
            source.backup(snapshot, pages=128)
            _check(snapshot)
            _check_journals(snapshot)
        if roots != _roots(stage / "ledger.db"):
            raise BackupError("Firms changed during backup; retry.")
        _write_archive(stage, home, roots, before, password, source_path)
        os.chmod(stage / "archive", 0o600)
        with (stage / "archive").open("rb") as f:
            os.fsync(f.fileno())
        os.link(stage / "archive", output)
    return output


def _migration_backup(path: Path, version: int) -> None:
    dest = directory()
    name = f"pre-migration-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-v{version}-to-v{migrations.SCHEMA_VERSION}.zip"
    create(dest / name, source_path=path)
    _sync_dir(dest)
    for old in sorted(dest.glob("pre-migration-*.zip"), reverse=True)[5:]:
        if old.is_file() and not old.is_symlink():
            old.unlink()


@contextmanager
def before_migration(db: sqlite3.Connection, path: Path) -> Iterator[None]:
    """Back up once before any upgrade; serialize upgrading starters across processes."""
    version = check_version(db)
    if version == migrations.SCHEMA_VERSION:
        yield
        return
    lock = None
    try:
        lock = path.with_suffix(".migration.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        version = check_version(db)
        if version not in (0, migrations.SCHEMA_VERSION):
            _migration_backup(path, version)
    except Exception as error:
        if lock is not None:
            lock.close()
        detail = (
            str(error)
            if isinstance(error, BackupError)
            else "Check permissions, free space and other upgrading processes."
        )
        raise SchemaError(
            "Pre-migration backup failed; startup refused without migrating. "
            + detail
            + " Finish pending import recovery with the previous release, then retry. "
            "To roll back, stop services and use the matching release with "
            "fiscalm --home /empty/finance restore /path/to/backup.zip. "
            "See docs/self-hosting.md, Backup and restore."
        ) from None
    try:
        yield
    finally:
        lock.close()


def _checked(
    z: zipfile.ZipFile,
    allowed: Callable[[str], bool],
    expected: dict[str, str] | None = None,
) -> list[zipfile.ZipInfo]:
    """Every member name is checked before anything is written."""
    names = z.namelist()
    if len(set(names)) != len(names):
        raise BackupError("Duplicate archive entry.")
    if expected is not None and set(names) != set(expected) | {"manifest.json"}:
        raise BackupError("Archive does not match its manifest.")
    for info in z.infolist():
        name = info.filename
        parts = PurePosixPath(name).parts
        if (
            not parts
            or name != PurePosixPath(name).as_posix()
            or name.startswith("/")
            or ".." in parts
            or "\\" in name
            or info.is_dir()
            or stat.S_ISLNK(info.external_attr >> 16)
        ):
            raise BackupError("Unsafe archive entry.")
        if not allowed(name):
            raise BackupError("Archive contains an unexpected file.")
    return z.infolist()


def _private_dir(path: Path) -> None:
    """0700 whatever the umask."""
    if not path.is_dir():
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)


def _write(z: zipfile.ZipFile, infos: list[zipfile.ZipInfo], root: Path, expected: dict[str, str] | None) -> None:
    for info in infos:
        name = info.filename
        for parent in reversed(PurePosixPath(name).parents[:-1]):
            _private_dir(root / parent)
        p = root / name
        h = hashlib.sha256()
        with z.open(info) as src, p.open("xb") as dst:
            os.chmod(p, 0o600)
            while block := src.read(1024 * 1024):
                h.update(block)
                dst.write(block)
        if expected is not None and name != "manifest.json" and h.hexdigest() != expected[name]:
            raise BackupError("Archive checksum mismatch.")


def _restore_ledger(path: Path, target: Path, manifest: dict) -> None:
    version = read_version(path)
    if version != manifest["schema"] or not 1 <= version <= SCHEMA_VERSION:
        raise BackupError("Ledger schema is newer than this code or differs from the manifest.")
    with closing(sqlite3.connect(path)) as db:
        _check(db)
        _check_journals(db)

        def relocate(value: str | None) -> str | None:
            for old in sorted(manifest["source_homes"], key=len, reverse=True):
                prefix = old.rstrip("/") + "/"
                if value and value.startswith(prefix):
                    relative = PurePosixPath(value[len(prefix) :])
                    if ".." in relative.parts or relative.is_absolute():
                        raise BackupError("Ledger path escapes its storage root.")
                    return str(target / relative)
            if value:
                raise BackupError("Ledger path is outside the archived home.")
            return value

        db.create_function("relocate", 1, relocate)
        # Only path columns are relocated. JSON move journals must be empty.
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            qt = '"' + table.replace('"', '""') + '"'
            columns = db.execute(f"PRAGMA table_info({qt})").fetchall()
            for col in columns:
                if col[1] not in (
                    "path",
                    "raw_path",
                    "zip_path",
                    "source_path",
                    "csv_path",
                ):
                    continue
                qc = '"' + col[1] + '"'
                db.execute(f"UPDATE {qt} SET {qc}=relocate({qc})")
        db.commit()
        _check(db)


def _unpack(archive: Path, stage: Path) -> dict:
    with zipfile.ZipFile(archive) as z:
        if len(set(z.namelist())) != len(z.namelist()):
            raise BackupError("Duplicate archive entry.")
        manifest = json.loads(z.read("manifest.json"))
        if manifest["format"] != 1:
            raise BackupError("Unsupported backup format.")
        registry = stage / ".registry.db"
        data = z.read("state/ledger.db")
        if hashlib.sha256(data).hexdigest() != manifest["sha256"]["state/ledger.db"]:
            raise BackupError("Archive checksum mismatch.")
        registry.write_bytes(data)
        roots = _roots(registry)
        registry.unlink()

        def allowed(name: str) -> bool:
            return name in (
                "manifest.json",
                "state/ledger.db",
                "secrets.enc",
            ) or (name.split("/")[0] in roots and "/" in name)

        _write(z, _checked(z, allowed, manifest["sha256"]), stage, manifest["sha256"])
    return manifest


def refuse_unfinished_restore(home: Path) -> None:
    """The one check run before anything opens, initialises, serves or backs up a home: a restore that has not yet
    removed its stage (see restore), in the home or in the configured secrets directory, left an incomplete
    installation. Two lstat calls; a missing or unreadable directory holds no stage that could be seen."""
    for root in (Path(home), secrets_dir()):
        if os.path.lexists(root / RESTORING):
            raise SchemaError(
                f"A restore into {root} did not finish. Stop every fiscalm service, empty the data directory and the "
                "secrets directory, then run the restore again (docs/self-hosting.md, Backup and restore)."
            )


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _move(source: Path, destination: Path) -> None:
    """rename(2) that never replaces an existing destination: Linux renameat2(RENAME_NOREPLACE)."""
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise BackupError("Restore needs Linux renameat2 to publish without replacing files.")
    if renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE):
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise BackupError(f"{destination} appeared during the restore and was not replaced.")
        if error in (errno.EINVAL, errno.ENOSYS):
            raise BackupError("This filesystem cannot rename without replacing; restore refused.")
        raise OSError(error, os.strerror(error))


@dataclass(frozen=True)
class _Claim:
    directory: Path  # the destination, empty when claimed
    stage: Path  # <directory>/.fiscalm-restore, the marker
    entries: Path  # the staged directory whose entries are published into `directory`
    made: tuple[Path, ...]  # directories the restore created on the way, outermost first


def _claim(directory: Path, what: str, claims: list[_Claim], entries: str = "") -> Path:
    """Create the restore's stage exclusively inside an empty (or missing) directory, on its filesystem, and make it and
    every directory created on the way durable before anything is published. A stage is owned (and ever removed)
    only once this restore's own mkdir created it. Empty means no entries, except lost+found at a filesystem root."""
    directory = directory.expanduser().absolute()
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise BackupError(f"Restore requires an empty {what}.")
    directory = directory.resolve()
    refuse_unfinished_restore(directory)
    made = tuple(d for d in (*reversed(directory.parents), directory) if not d.exists())
    try:
        for d in made:
            d.mkdir(mode=0o700 if d == directory else 0o777)
        if any(e.name != "lost+found" or not os.path.ismount(directory) for e in os.scandir(directory)):
            raise BackupError(f"Restore requires an empty {what}.")
        os.mkdir(directory / RESTORING, 0o700)
    except FileExistsError:
        _remove_made(made)
        raise BackupError(f"Restore requires an empty {what}.") from None
    except BaseException:
        _remove_made(made)
        raise
    stage = directory / RESTORING
    claims.append(_Claim(directory, stage, stage / entries, made))
    os.chmod(stage, 0o700)
    _private_dir(stage / ".probe")  # a filesystem without no-replace renames refuses now
    _move(stage / ".probe", stage / ".probed")
    os.rmdir(stage / ".probed")
    for d in (stage, directory, *(d.parent for d in reversed(made))):
        _sync_dir(d)
    return stage


def _remove_made(made: tuple[Path, ...]) -> None:
    for d in reversed(made):
        with suppress(OSError):  # rmdir removes only an empty directory: never anyone else's content
            d.rmdir()


def _secrets(stage: Path, home: Path, password: str | None, claims: list[_Claim]) -> None:
    """Decrypt archived secrets into FISCALM_SECRETS: inside the home stage when it is <home>/secrets, otherwise into a
    stage of its own in that (empty) directory."""
    enc = stage / "secrets.enc"
    if not enc.exists():
        return
    if password is None:
        raise BackupError("This backup requires its passphrase.")
    payload = _crypt(enc.read_bytes(), password, decrypt=True)
    enc.unlink()
    destination = secrets_dir().expanduser().absolute()
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        infos = _checked(z, lambda n: n.startswith("secrets/"))
        if destination.resolve() == home / "secrets":
            root = stage
        elif destination.resolve().is_relative_to(home):
            raise BackupError("FISCALM_SECRETS must be <FISCALM_HOME>/secrets or outside FISCALM_HOME.")
        else:
            root = _claim(destination, "secrets directory", claims, "secrets")
        _write(z, infos, root, None)
    _private_dir(root / "secrets")  # an archive of an empty secrets directory has no members


def _publish(claims: list[_Claim]) -> None:
    """Secrets first, the home last and its state directory (the ledger) last of all; then everything is flushed and
    only then are the stages removed, the home's marker last."""
    for c in reversed(claims):
        os.chmod(c.directory, 0o700)
        for entry in sorted(os.scandir(c.entries), key=lambda e: e.name == "state"):
            _move(Path(entry.path), c.directory / entry.name)
        _sync_dir(c.directory)
    os.sync()
    for c in reversed(claims):
        if c.entries != c.stage:
            os.rmdir(c.entries)
        os.rmdir(c.stage)
        _sync_dir(c.directory)


def _discard(claims: list[_Claim]) -> None:
    """Before publishing: undo every claim, the external secrets stage first and the home's marker last. A stage that
    cannot be removed stops the cleanup, so the home keeps its marker and nothing starts beside the leftover."""
    for c in reversed(claims):
        try:
            shutil.rmtree(c.stage)
        except OSError:
            raise BackupError(
                f"The restore failed and could not remove {c.stage}. Remove it, and {claims[0].stage} if it is still "
                "there, before using these directories."
            ) from None
        _remove_made(c.made)


def restore(archive: Path, target: Path, password: str | None = None) -> None:
    """Validate in a private stage inside an empty home; publish without replacing anything, ledger last; always
    shadow. An interrupted publish leaves the stage, which every entry point refuses (refuse_unfinished_restore)."""
    claims: list[_Claim] = []
    try:
        stage = _claim(target, "FISCALM_HOME", claims)
        target = claims[0].directory
        manifest = _unpack(archive, stage)
        _restore_ledger(stage / "state/ledger.db", target, manifest)
        (stage / "manifest.json").unlink()
        _secrets(stage, target, password, claims)
    except BaseException:
        _discard(claims)
        raise
    _publish(claims)


def scheduled(keep: int = 7) -> Path:
    if keep < 1:
        raise BackupError("Retention must keep at least one backup.")
    refuse_unfinished_restore(finance_home())
    dest = directory()
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (dest / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = create(dest / f"fiscalm-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.zip")
        for old in sorted(dest.glob("fiscalm-*.zip"), reverse=True)[keep:]:
            if not old.is_symlink() and old.is_file():
                old.unlink()
    return path


def latest() -> Path | None:
    refuse_unfinished_restore(finance_home())
    return next(
        (p for p in sorted(directory().glob("fiscalm-*.zip"), reverse=True) if p.is_file() and not p.is_symlink()),
        None,
    )


def main(args: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Back up or restore fiscalm, without loading company configuration.", allow_abbrev=False
    )
    p.add_argument("--home")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", allow_abbrev=False)
    b.add_argument("archive", nargs="?", type=Path)
    b.add_argument("--include-secrets", action="store_true")
    b.add_argument("--scheduled", action="store_true")
    b.add_argument("--keep", type=int, default=7)
    r = sub.add_parser("restore", allow_abbrev=False)
    r.add_argument("archive", type=Path)
    r.add_argument("--with-secrets", action="store_true")
    a = p.parse_args(args)
    try:
        if a.command == "backup" and a.scheduled and (a.include_secrets or a.archive):
            raise BackupError("Scheduled backups exclude secrets and choose their own archive name.")
        password = (
            getpass.getpass("Backup passphrase: ")
            if getattr(a, "include_secrets", False) or getattr(a, "with_secrets", False)
            else None
        )
        if a.command == "restore":
            restore(a.archive, finance_home(), password)
            print(f"Restored in Sandbox into {finance_home()}; secrets, if the archive had them, in {secrets_dir()}.")
        elif a.scheduled:
            print(scheduled(a.keep))
        elif a.archive:
            print(create(a.archive, password))
        else:
            raise BackupError("Supply an archive path or --scheduled.")
        return 0
    except (BackupError, SchemaError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (
        OSError,
        ValueError,
        KeyError,
        sqlite3.Error,
        zipfile.BadZipFile,
        RuntimeError,
    ):
        # Do not expose secrets or attacker-controlled archive text in errors.
        print(
            "Backup/restore refused. Check paths, empty destination, passphrase, archive integrity and schema version.",
            file=sys.stderr,
        )
        return 1
