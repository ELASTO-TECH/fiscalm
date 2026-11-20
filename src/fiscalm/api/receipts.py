"""Bounded receipt intake: stream to private staging files, never parse document contents."""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from fiscalm import companies
from fiscalm.api import uploads as upload_links
from fiscalm.ledger import Ledger, finance_home

FILE_MAX = 15 * 1024 * 1024
REQUEST_MAX = 40 * 1024 * 1024
HOUR_BYTES = 100 * 1024 * 1024
DAY_BYTES = 500 * 1024 * 1024
MIN_FREE = 100 * 1024 * 1024
DEADLINE = 30
MAX_DEADLINE = 190
LINK_HOUR_BYTES = 50 * 1024 * 1024
LINK_DAY_BYTES = 250 * 1024 * 1024
ADMISSION = threading.BoundedSemaphore(3)
STORE_LOCK = threading.RLock()


class Refused(ValueError):
    def __init__(self, status: int):
        self.status = status


def body_deadline(length: int) -> float:
    return min(MAX_DEADLINE, DEADLINE + length / (256 * 1024))


class Received:
    """Count only bytes actually read, including a body refused before completion."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.bytes = 0

    def read1(self, size: int) -> bytes:
        data = (self.stream.read1 if hasattr(self.stream, "read1") else self.stream.read)(size)
        self.bytes += len(data)
        return data


def sniff(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if (
        len(data) >= 16
        and data[4:8] == b"ftyp"
        and int.from_bytes(data[:4], "big") in range(16, min(len(data), 4096) + 1)
    ):
        end = int.from_bytes(data[:4], "big")
        brands = [data[8:12], *[data[i : i + 4] for i in range(16, end, 4)]]
        if any(b in (b"heic", b"heix") for b in brands):
            return "heic"
    raise Refused(415)


def storage_root(company: str) -> Path:
    root = finance_home().resolve()
    return root / "inbox" / "uploads" if company == "ALL" else root / company / "documents" / "inbox"


@contextmanager
def directory(company: str, day: str) -> Iterator[int]:
    """Pin and validate every directory component without following symlinks."""
    root = finance_home().resolve()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        segments = ("inbox", "uploads", day) if company == "ALL" else (company, "documents", "inbox", day)
        for segment in segments:
            if segment in ("", ".", "..") or "/" in segment or "\\" in segment:
                raise Refused(409)
            with suppress(FileExistsError):
                os.mkdir(segment, mode=0o700, dir_fd=fd)
            child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        expected = storage_root(company) / day
        if not os.path.samestat(os.fstat(fd), os.stat(expected, follow_symlinks=False)):
            raise Refused(409)
        if os.fstatvfs(fd).f_bavail * os.fstatvfs(fd).f_frsize < MIN_FREE + REQUEST_MAX:
            raise Refused(507)
        yield fd
    except OSError:
        raise Refused(409) from None
    finally:
        os.close(fd)


@contextmanager
def multipart(
    content_type: str, stream: BinaryIO, length: int, company: str, connection=None, deadline: float | None = None
) -> Iterator[list[tuple[str, BinaryIO]]]:
    """Bound buffers independently of line count; stop before processing an eleventh part."""
    if length > REQUEST_MAX:
        raise Refused(413)
    match = re.fullmatch(
        r'multipart/form-data;\s*boundary=(?:"([A-Za-z0-9_-]{1,70})"|([A-Za-z0-9_-]{1,70}))', content_type, re.I
    )
    if not match:
        raise Refused(400)
    boundary = (match[1] or match[2]).encode()
    reader = _MultipartReader(stream, length, boundary, connection, deadline)
    files = []
    try:
        with directory(company, upload_links.timestamp()[:10]) as fd:
            if reader.until(b"\r\n", 74) != b"--" + boundary:
                raise Refused(400)
            while True:
                if len(files) == 10:
                    raise Refused(413)
                _multipart_headers(reader.until(b"\r\n\r\n", 4096))
                name = ".upload-" + secrets.token_hex(16)
                out = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                os.unlink(name, dir_fd=fd)  # open staging file disappears on every exit/crash
                fh = os.fdopen(out, "w+b")
                # Register before streaming so failures still close the file.
                files.append(("", fh))
                files[-1] = (reader.file(fh), fh)
                if not reader.next_part():
                    break
            yield files
    except TimeoutError:
        raise Refused(408) from None
    finally:
        for _, fh in files:
            fh.close()


def _multipart_headers(headers: bytes) -> None:
    lines = headers.split(b"\r\n")
    if not any(
        re.fullmatch(rb'Content-Disposition: form-data; name="files"; filename="[^"\r\n]*"', line, re.I)
        for line in lines
    ) or any(not re.match(rb"(Content-Disposition|Content-Type):", line, re.I) for line in lines):
        raise Refused(400)


class _MultipartReader:
    """One bounded request buffer and deadline, shared by framing and file streaming."""

    def __init__(self, stream: BinaryIO, length: int, boundary: bytes, connection, deadline: float | None) -> None:
        self.stream = stream
        self.connection = connection
        self.remaining = length
        self.end = deadline if deadline is not None else time.monotonic() + DEADLINE
        self.buf = bytearray()
        self.marker = b"\r\n--" + boundary

    def read(self) -> None:
        left = self.end - time.monotonic()
        if left <= 0:
            raise Refused(408)
        if self.connection is not None:
            self.connection.settimeout(left)
        # read1 returns available bytes, so a trickle cannot reset the total deadline.
        data = (self.stream.read1 if hasattr(self.stream, "read1") else self.stream.read)(min(65536, self.remaining))
        if time.monotonic() >= self.end:
            raise Refused(408)
        if not data:
            raise Refused(400)
        self.remaining -= len(data)
        self.buf.extend(data)

    def until(self, delimiter: bytes, limit: int) -> bytes:
        while True:
            pos = self.buf.find(delimiter)
            if pos >= 0:
                if pos > limit:
                    raise Refused(413)
                out = bytes(self.buf[:pos])
                del self.buf[: pos + len(delimiter)]
                return out
            if len(self.buf) > limit or not self.remaining:
                raise Refused(413 if len(self.buf) > limit else 400)
            self.read()

    def file(self, fh: BinaryIO) -> str:
        size = 0
        prefix = bytearray()
        ext = None

        def write(chunk: bytes) -> None:
            nonlocal size, ext
            size += len(chunk)
            if size > FILE_MAX:
                raise Refused(413)
            prefix.extend(chunk[: max(0, 4096 - len(prefix))])
            if ext is None and len(prefix) >= 4096:
                ext = sniff(prefix)
            fh.write(chunk)

        while True:
            pos = self.buf.find(self.marker)
            if pos >= 0:
                write(bytes(self.buf[:pos]))
                del self.buf[: pos + len(self.marker)]
                break
            keep = len(self.marker) - 1
            if len(self.buf) > keep:
                write(bytes(self.buf[:-keep]))
                del self.buf[:-keep]
            if not self.remaining:
                raise Refused(400)
            self.read()
        ext = ext or sniff(prefix)
        fh.seek(0)
        return ext

    def next_part(self) -> bool:
        while len(self.buf) < 2 and self.remaining:
            self.read()
        suffix = bytes(self.buf[:2])
        del self.buf[:2]
        if suffix == b"--":
            while self.remaining:
                self.read()
                if len(self.buf) > 2:
                    raise Refused(400)
            if self.buf not in (b"", b"\r\n"):
                raise Refused(400)
            return False
        if suffix != b"\r\n":
            raise Refused(400)
        return True


def begin_request(L: Ledger, token: str) -> dict:
    """Charge failed requests too; authentication and quota are rechecked at commit."""
    at = upload_links.timestamp()
    since = (datetime.fromisoformat(at) - timedelta(hours=1)).isoformat(timespec="seconds")
    with L.tx(immediate=True) as db:
        link = upload_links.verify(L, token)
        if not link:
            raise Refused(404)
        count = db.execute(
            "SELECT COUNT(*) FROM upload_attempts WHERE link_id=? AND at>? AND files=0 AND bytes=0", (link["id"], since)
        ).fetchone()[0]
        if count >= 60:
            raise Refused(429)
        db.execute(
            "INSERT INTO upload_attempts(link_id,company,at,files,bytes) VALUES(?,?,?,0,0)",
            (link["id"], link["company"], at),
        )
        return link


def quota(L: Ledger, link: dict, count: int, total: int) -> None:
    at = datetime.fromisoformat(upload_links.timestamp())
    hour, day = [(at - timedelta(hours=h)).isoformat(timespec="seconds") for h in (1, 24)]
    recent = L.db.execute(
        "SELECT COALESCE(SUM(files),0) FROM upload_attempts WHERE link_id=? AND at>?", (link["id"], hour)
    ).fetchone()[0]
    link_hour, link_day = [
        L.db.execute(
            "SELECT COALESCE(SUM(bytes),0) FROM upload_attempts WHERE link_id=? AND at>?", (link["id"], since)
        ).fetchone()[0]
        for since in (hour, day)
    ]
    if link_hour + total > LINK_HOUR_BYTES or link_day + total > LINK_DAY_BYTES:
        raise Refused(429)
    hourly, daily = [
        L.db.execute(
            "SELECT COALESCE(SUM(bytes),0) FROM upload_attempts WHERE company=? AND at>?", (link["company"], since)
        ).fetchone()[0]
        for since in (hour, day)
    ]
    if recent + count > 60 or hourly + total > HOUR_BYTES or daily + total > DAY_BYTES:
        raise Refused(429)
    if link["max_files"] is not None and link["uploads"] + count > link["max_files"]:
        raise Refused(409)


def reserve_bytes(L: Ledger, token: str | dict, size: int) -> int:
    """Charge a request's declared bytes before reading it: a link by its token, a member by `member(...)`."""
    with L.tx(immediate=True) as db:
        link = upload_links.verify(L, token) if isinstance(token, str) else token
        if not link:
            raise Refused(404)
        quota(L, link, 0, size)
        row = db.execute(
            "INSERT INTO upload_attempts(link_id,company,at,files,bytes) VALUES(?,?,?,0,?)",
            (link["id"], link["company"], upload_links.timestamp(), size),
        )
        return row.lastrowid


def member(company: str, user_id: str) -> dict:
    """A signed-in member's uploads to one company, budgeted like a link's: the same hourly files and bytes per sender,
    and the company's shared byte budget. Their attempts are kept under "user:<id>", never a link id."""
    return {"id": "user:" + user_id, "company": company, "max_files": None, "uploads": 0}


def settle_bytes(L: Ledger, reservation: int, received: int) -> None:
    with L.tx() as db:
        db.execute("UPDATE upload_attempts SET bytes=? WHERE rowid=?", (received, reservation))


class _DeadlineReader:
    def __init__(self, source: BinaryIO, deadline: float | None):
        self.source = source
        self.deadline = deadline

    def read(self, size: int) -> bytes:
        check_deadline(self.deadline)
        data = self.source.read(size)
        check_deadline(self.deadline)
        return data


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise Refused(408)


def accept(
    L: Ledger,
    token: str,
    files: list[tuple[str, bytes | BinaryIO]],
    *,
    reserved: bool = False,
    deadline: float | None = None,
) -> list[dict]:
    """A Bondrop link's files: the link's quotas are checked before the disk work and again in the transaction."""
    with STORE_LOCK:
        link = upload_links.verify(L, token)
        if not link:
            raise Refused(404)
        prepared = _prepare(files)
        count, total = len(prepared), 0 if reserved else sum(p[3] for p in prepared)
        # Quotas depend on submitted files and bytes, never on global deduplication.
        quota(L, link, count, total)

        def record(db, at: str, stored: list[tuple[str, bool]]) -> None:
            current = upload_links.verify(L, token)
            if not current:
                raise Refused(404)
            quota(L, current, count, total)
            for sha, created in stored:
                db.execute(
                    "INSERT INTO upload_results(sha,link_id,at,duplicate) VALUES(?,?,?,?)",
                    (sha, link["id"], at, not created),
                )
            db.execute(
                "INSERT INTO upload_attempts(link_id,company,at,files,bytes) VALUES(?,?,?,?,?)",
                (link["id"], link["company"], at, count, total),
            )
            db.execute("UPDATE upload_links SET uploads=uploads+?,last_used_at=? WHERE id=?", (count, at, link["id"]))

        _store(L, link["company"], (link["id"], None), prepared, deadline, record)
        return [{"sha": p[0]} for p in prepared]


def accept_member(
    L: Ledger, sender: dict, files: list[tuple[str, BinaryIO]], *, deadline: float | None = None
) -> list[dict]:
    """A member's files (`member(...)`), whose bytes reserve_bytes already charged, stored exactly as a link's are.
    The caller's write guard re-checks the session; the file budget is checked again in the transaction."""
    with STORE_LOCK:
        prepared = _prepare(files)
        quota(L, sender, len(prepared), 0)

        def record(db, at: str, stored: list[tuple[str, bool]]) -> None:
            quota(L, sender, len(prepared), 0)
            db.execute(
                "INSERT INTO upload_attempts(link_id,company,at,files,bytes) VALUES(?,?,?,?,0)",
                (sender["id"], sender["company"], at, len(prepared)),
            )

        stored = _store(L, sender["company"], (None, sender["id"][5:]), prepared, deadline, record)
        return [{"sha": sha, "duplicate": not created} for sha, created in stored]


def _prepare(files: list[tuple[str, bytes | BinaryIO]]) -> list[tuple[str, str, BinaryIO, int]]:
    if not 1 <= len(files) <= 10:
        raise Refused(413)
    prepared = []
    for ext, data in files:
        fh = io.BytesIO(data) if isinstance(data, bytes) else data
        fh.seek(0, 2)
        size = fh.tell()
        if size > FILE_MAX:
            raise Refused(413)
        fh.seek(0)
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
        fh.seek(0)
        prepared.append((digest, ext, fh, size))
    return prepared


def _store(
    L: Ledger,
    company: str,
    owner: tuple[str | None, str | None],
    prepared: list[tuple[str, str, BinaryIO, int]],
    deadline: float | None,
    record: Callable[[sqlite3.Connection, str, list[tuple[str, bool]]], None] | None,
) -> list[tuple[str, bool]]:
    """Publish complete files named by their sha in the company's own inbox, then one short transaction records them
    as unchecked receipts; a failed batch removes the files no row refers to. `owner` is (link id, user id), exactly
    one set. Returns (sha, newly recorded)."""
    source = "upload:" + (owner[0] or "user:" + str(owner[1]))
    at = upload_links.timestamp()
    folder = storage_root(company) / at[:10]
    try:
        with directory(company, at[:10]) as fd, ExitStack() as cleanup:
            # Always do identical disk work, even for bytes known in another company.
            for sha, ext, fh, _ in prepared:
                name = sha + "." + ext
                temp = ".upload-" + secrets.token_hex(16)
                out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                try:
                    with os.fdopen(out, "wb") as dest:
                        shutil.copyfileobj(_DeadlineReader(fh, deadline), dest, 65536)
                        dest.flush()
                        os.fsync(dest.fileno())
                    try:
                        os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)

                        def remove_unreferenced(name=name):
                            if not L.db.execute(
                                "SELECT 1 FROM documents WHERE path=?", (str(folder / name),)
                            ).fetchone():
                                os.unlink(name, dir_fd=fd)

                        cleanup.callback(remove_unreferenced)
                    except FileExistsError:
                        if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                            raise Refused(409) from None
                finally:
                    os.unlink(temp, dir_fd=fd)
            os.fsync(fd)
            check_deadline(deadline)
            stored = []
            with L.tx(immediate=True) as db:
                check_deadline(deadline)
                for sha, ext, _, _ in prepared:
                    result = db.execute(
                        """INSERT OR IGNORE INTO documents
                        (sha,company,month,kind,path,issues,source,created_at)
                        VALUES(?,?,?,'receipt',?,'["to-check"]',?,?)""",
                        (sha, company, at[:7], str(folder / (sha + "." + ext)), source, at),
                    )
                    if result.rowcount:
                        db.execute(
                            "INSERT INTO receipt_uploads(sha,link_id,user_id,company,uploaded_at,file_type) "
                            "VALUES(?,?,?,?,?,?)",
                            (sha, *owner, company, at, ext),
                        )
                    stored.append((sha, bool(result.rowcount)))
                if record:
                    record(db, at, stored)
        return stored
    except OSError:
        raise Refused(409) from None


def inbox(L: Ledger, company: str) -> list[dict]:
    return [
        dict(r)
        for r in L.db.execute(
            """SELECT d.sha AS id, d.sha, u.uploaded_at,
           COALESCE(l.label,'') AS link_label, u.file_type FROM receipt_uploads u
           JOIN documents d ON d.sha=u.sha LEFT JOIN upload_links l ON l.id=u.link_id
           WHERE d.company=? AND d.source LIKE 'upload:%'
           ORDER BY u.uploaded_at DESC, d.sha""",
            (company,),
        )
    ]


def sweep_temps(now: float | None = None) -> int:
    """Remove only old regular staging files through pinned inbox directories."""
    cutoff = (time.time() if now is None else now) - MAX_DEADLINE
    removed = 0
    with ExitStack() as stack:
        for company in (*companies.all(), "ALL"):
            try:
                fd = os.open(finance_home(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, fd)
                segments = ("inbox", "uploads") if company == "ALL" else (company, "documents", "inbox")
                for segment in segments:
                    fd = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    stack.callback(os.close, fd)
                for _, _, names, parent in os.fwalk(".", follow_symlinks=False, dir_fd=fd):
                    for name in names:
                        if not name.startswith(".upload-"):
                            continue
                        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                            os.unlink(name, dir_fd=parent)
                            removed += 1
            except OSError:
                continue
    return removed
