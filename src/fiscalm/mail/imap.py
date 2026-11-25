"""Read-only IMAP: the new messages of a mail account's reading folder (docs/mail-invoices.md).

Nothing on the server changes. The folder is opened with EXAMINE and every message is fetched with BODY.PEEK, so no
flag is set (not even \\Seen); the only commands sent are LOGIN, EXAMINE, UID SEARCH, UID FETCH and LOGOUT. There is
no path here that stores, copies, moves, deletes, appends or sends.

Each run is bounded: every run looks at the messages of the last WINDOW_DAYS days (one not read by then is never
read), at most MAX_MESSAGES of them, none larger than MESSAGE_MAX bytes (checked from RFC822.SIZE before anything is
downloaded, and again on every literal the server sends, so a server that lies can't make a read longer), at most
ATTACHMENTS_MAX attachments of up to ATTACHMENT_MAX bytes, and an absolute BUDGET of seconds per account that every
socket read checks (a server trickling bytes can't hold a run). What is left waits for the next run.

A message is remembered (`mail_seen`) by account, folder, UIDVALIDITY and UID, and by Message-ID, only after the
caller's handler returned: a crash in between reads it again, so handlers must be idempotent. A message that could
not be fetched, parsed or handled is retried on the next runs, after the new ones (so a few bad messages never hold
back the rest), up to ATTEMPTS times; then it stays recorded as an error. A FETCH reply counts only for the UID and
the item asked for.

Mail content is untrusted. A message becomes plain data: its sender, subject and date as text, and the bytes of the
attachments that are a PDF or XML by their content. Attachment names are never kept, so they never become paths; the
body is never read, so no link in it is ever seen.
"""

from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import hashlib
import html
import imaplib
import re
import sqlite3
import ssl
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta

from fiscalm import integrations, secrets
from fiscalm.ledger import Ledger, now

WINDOW_DAYS = 30
MAX_MESSAGES = 200
MESSAGE_MAX = 25 * 1024 * 1024
ATTACHMENTS_MAX = 10
ATTACHMENT_MAX = 15 * 1024 * 1024
TIMEOUT = 60
BUDGET = 120.0
TEXT_MAX = 300  # sender and subject, as stored and shown
ATTEMPTS = 3

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_XML_TYPES = ("application/xml", "text/xml")


class ReadError(RuntimeError):
    """The account could not be read; the message says what to check, never a password or a server's reply.
    `code`: the coarse kind Setări shows, one of mail.setup's test codes."""

    def __init__(self, message: str, code: str = "protocol_error"):
        super().__init__(message)
        self.code = code


def _failure(exc: Exception, stage: str) -> str:
    """A failed connection or login as a test code; the server's reply is never kept."""
    if isinstance(exc, TimeoutError | _OutOfTime):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, OSError | imaplib.IMAP4.abort):
        return "host_unreachable"
    return "wrong_password" if isinstance(exc, imaplib.IMAP4.error) and stage == "login" else "protocol_error"


@dataclass(frozen=True)
class Attachment:
    kind: str  # pdf | xml, from the bytes
    data: bytes
    name: str = ""  # the attachment's file name as data (a bank's or accountant's naming rule), never a path

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class Message:
    account: str
    message_id: str
    sender: str  # the From header as text
    address: str  # the sender's address, lowercased ("" when it has none)
    subject: str
    date: str  # YYYY-MM-DD from the Date header, else the day it was read
    attachments: tuple[Attachment, ...]
    # CSV or XLSX tables (kind "csv"/"xlsx"): only a settlement report from a sender a person named reads them.
    tables: tuple[Attachment, ...] = ()


def accounts(L: Ledger) -> list[integrations.Instance]:
    """The enabled mail accounts: each one is read; Oprește in Integrări is the one switch that stops it."""
    return integrations.enabled(L, "mail")


def seen(L: Ledger, account: str, message_id: str) -> bool:
    return (
        L.db.execute(
            "SELECT 1 FROM mail_seen WHERE account=? AND message_id=? AND result!='error' LIMIT 1",
            (account, message_id),
        ).fetchone()
        is not None
    )


class _OutOfTime(Exception):
    """The account's budget is spent; the run stops where it is."""


class _TooLarge(Exception):
    """The server sent a literal larger than the one allowed for this read."""


LINE_MAX = 1_000_000  # imaplib's own limit for one response line
LOGOUT_GRACE = 5.0


class _Reader:
    """The connection's reads, replacing imaplib's (whatever its version buffers through): every socket read checks
    the run's absolute deadline, and a literal larger than `limit[0]` is refused before a byte of it is read."""

    def __init__(self, m: imaplib.IMAP4, deadline: float):
        self.sock, self.deadline, self.limit = m.sock, deadline, [MESSAGE_MAX + 1]
        self.buf = bytearray(b"".join(getattr(m, "_readbuf", None) or []))  # what a newer imaplib already holds

    def _more(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise _OutOfTime()
        self.sock.settimeout(min(TIMEOUT, left))
        try:
            chunk = self.sock.recv(65536)
        except TimeoutError:
            if self.deadline - time.monotonic() <= 0:
                raise _OutOfTime() from None
            raise  # TIMEOUT seconds without a byte: the connection is gone
        if not chunk:
            raise imaplib.IMAP4.abort("connection closed")
        self.buf += chunk

    def readline(self) -> bytes:
        while b"\n" not in self.buf:
            if len(self.buf) > LINE_MAX:
                raise imaplib.IMAP4.error("response line too long")
            self._more()
        n = self.buf.index(b"\n") + 1
        line = bytes(self.buf[:n])
        del self.buf[:n]
        return line

    def read(self, size: int) -> bytes:
        if size > self.limit[0]:
            raise _TooLarge()
        while len(self.buf) < size:
            self._more()
        data = bytes(self.buf[:size])
        del self.buf[:size]
        return data


def _guard(m: imaplib.IMAP4, deadline: float) -> list[int]:
    """Bound the connection (see _Reader); returns the literal limit the caller sets before each FETCH."""
    r = _Reader(m, deadline)
    m.readline, m.read = r.readline, r.read  # imaplib reads every line and literal through these
    m._fiscalm_reader = r
    return r.limit


def _plain(v: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or "")).strip()[:TEXT_MAX]


def _utf7(name: str) -> str:
    """IMAP's modified UTF-7 (RFC 3501 5.1.3) for a folder name."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            b64 = base64.b64encode("".join(run).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b64}-")
            run.clear()

    for c in name:
        if 0x20 <= ord(c) <= 0x7E:
            flush()
            out.append("&-" if c == "&" else c)
        else:
            run.append(c)
    flush()
    return "".join(out)


def _folder(name: str) -> str:
    if not name or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        raise ReadError("Dosarul citit are un nume nevalid. Corectează-l în Setări › Integrări.", "invalid_settings")
    v = _utf7(name)
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def guarded(cls: type[imaplib.IMAP4], deadline: float) -> type[imaplib.IMAP4]:
    """`cls` with the bounded reader installed as soon as its socket opens, so the greeting and CAPABILITY that the
    constructor reads are bounded too."""

    class Guarded(cls):  # type: ignore[valid-type, misc]
        def open(self, *args, **kwargs):
            super().open(*args, **kwargs)
            _guard(self, deadline)

    return Guarded


def _connect(host: str, port: int, timeout: float, deadline: float) -> imaplib.IMAP4:
    return guarded(imaplib.IMAP4_SSL, deadline)(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)


def _open(
    account: integrations.Instance, connect: Callable[[str, int, float, float], imaplib.IMAP4], deadline: float
) -> tuple[imaplib.IMAP4, list[int]]:
    try:
        password = account.secret("password")
    except (OSError, secrets.SecretError):
        raise ReadError(
            "Parola contului de e-mail lipsește. Se completează în Setări, Integrări.", "invalid_settings"
        ) from None
    m, stage = None, "connect"
    try:
        m = connect(account.config["host"], int(account.config.get("port") or 993), min(TIMEOUT, BUDGET), deadline)
        reader = getattr(m, "_fiscalm_reader", None)
        if reader is None:  # a connection that isn't bounded is never used
            raise ValueError("unguarded connection")
        limit, stage = reader.limit, "login"
        m.login(account.config["username"], password)
    except (OSError, imaplib.IMAP4.error, ValueError, _OutOfTime, _TooLarge) as exc:
        if m is not None:
            _logout(m)
        raise ReadError(
            "Conectarea la contul de e-mail a eșuat. Se verifică serverul, utilizatorul și parola.",
            _failure(exc, stage),
        ) from None
    return m, limit


def _logout(m: imaplib.IMAP4) -> None:
    r = getattr(m, "_fiscalm_reader", None)
    if r is not None:
        r.deadline = time.monotonic() + LOGOUT_GRACE  # a few seconds to say goodbye, even past the budget
    try:
        m.logout()
    except Exception:  # the connection may already be gone; nothing was changed either way
        pass
    finally:
        with suppress(Exception):
            m.shutdown()  # imaplib shuts down only after a successful LOGOUT
        for name in ("readline", "read", "_fiscalm_reader"):  # no reference cycle left behind
            m.__dict__.pop(name, None)


def _record_for(data, uid: int, item: bytes) -> tuple[bytes, bytes | None] | None:
    """The one FETCH record of this UID that carries `item` (its text and literal), or None when there is none or more
    than one: an unsolicited or reordered reply never stands for the message asked about."""
    records: list[
        list
    ] = []  # [text, literals]: one per FETCH response, its pieces joined back (a UID may follow the literal)
    for x in data or ():
        meta, literal = (
            (x[0] or b"", x[1]) if isinstance(x, tuple) else (x, None) if isinstance(x, bytes) else (b"", None)
        )
        if re.match(rb"\d+ \(", meta) or not records:
            records.append([meta, []])
        else:
            records[-1][0] += b" " + meta
        if literal is not None:
            records[-1][1].append(literal)
    found = []
    for text, literals in records:
        got = re.search(rb"\bUID (\d+)", text, re.I)  # IMAP atoms ignore case
        if got and int(got.group(1)) == uid and item.upper() in text.upper() and len(literals) <= 1:
            found.append((text, literals[0] if literals else None))
    return found[0] if len(found) == 1 else None


def _size(m: imaplib.IMAP4, uid: int) -> int | None:
    """The message's RFC822.SIZE, or None when the server didn't say it for this UID."""
    typ, data = m.uid("FETCH", str(uid), "(RFC822.SIZE)")
    rec = _record_for(data, uid, b"RFC822.SIZE") if typ == "OK" else None
    size = re.search(rb"RFC822\.SIZE (\d+)", rec[0], re.I) if rec else None
    return int(size.group(1)) if size else None


def _message_id(v) -> str | None:
    s = str(v or "").strip()
    found = re.search(r"<([\x21-\x7e]{1,250})>", s)
    return found.group(1) if found else None


def _body(m: imaplib.IMAP4, uid: int, limit: list[int]) -> bytes | None:
    """The message's first MESSAGE_MAX + 1 bytes, or None when the server didn't send them for this UID. A partial
    fetch, and the literal itself is refused past that size (_TooLarge) before it is read."""
    limit[0] = MESSAGE_MAX + 1
    typ, data = m.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{MESSAGE_MAX + 1}>)")
    rec = _record_for(data, uid, b"BODY[]") if typ == "OK" else None
    return rec[1] if rec and rec[1] is not None else None


def _date(msg, today: date) -> str:
    try:
        return email.utils.parsedate_to_datetime(str(msg.get("Date"))).date().isoformat()
    except (TypeError, ValueError, IndexError, OverflowError):
        return today.isoformat()


def _kind(part, data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "pdf"
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    if head == b"<" and (part.get_content_type() in _XML_TYPES or (part.get_filename() or "").lower().endswith(".xml")):
        return "xml"
    return None


def _table(part, data: bytes) -> str | None:
    """A CSV or XLSX by its name and bytes: an XLSX is a ZIP, a CSV is UTF-8 text."""
    name = (part.get_filename() or "").lower()
    if name.endswith(".xlsx") and data.startswith(b"PK\x03\x04"):
        return "xlsx"
    try:
        return "csv" if name.endswith(".csv") and data.decode("utf-8-sig") else None
    except UnicodeError:
        return None


def parse(account: str, raw: bytes, today: date) -> Message:
    """A message as plain data. Its attachments: every non-body part whose bytes are a PDF or an XML document, in
    order, up to ATTACHMENTS_MAX, each at most ATTACHMENT_MAX bytes (larger ones are left out)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    found: list[Attachment] = []
    tables: list[Attachment] = []
    for part in msg.walk():
        if len(found) >= ATTACHMENTS_MAX and len(tables) >= ATTACHMENTS_MAX:
            break
        named_csv = (part.get_filename() or "").lower().endswith(".csv")
        if part.is_multipart() or (part.get_content_type() in ("text/plain", "text/html") and not named_csv):
            continue
        try:
            data = part.get_payload(decode=True)
        except Exception:  # a malformed part is skipped, never guessed
            continue
        if not isinstance(data, bytes) or not data or len(data) > ATTACHMENT_MAX:
            continue
        kind = _kind(part, data)
        if kind and len(found) < ATTACHMENTS_MAX:  # invoices and tables each keep their own allowance
            found.append(Attachment(kind, data, _plain(part.get_filename())[:120]))
        elif (table := _table(part, data)) and len(tables) < ATTACHMENTS_MAX:
            tables.append(Attachment(table, data, _plain(part.get_filename())[:120]))
    try:
        sender = _plain(msg.get("From"))
        subject = _plain(msg.get("Subject"))
    except Exception:  # an undecodable header reads as empty
        sender, subject = "", ""
    address = email.utils.parseaddr(sender)[1].lower()
    mid = _message_id(msg.get("Message-ID")) or "sha256:" + hashlib.sha256(raw).hexdigest()
    address = address if "@" in address else ""
    return Message(account, mid, sender, address, subject, _date(msg, today), tuple(found), tuple(tables))


def _record(
    L: Ledger, account: str, folder: str, validity: int, uid: int, mid: str, result: str, msg: Message | None = None
) -> None:
    """Remember one message. Its sender and subject are kept only when it carried a document: the rest of the
    folder (a whole inbox, maybe) leaves nothing but its ids behind."""
    if msg is not None and not msg.attachments:
        msg = None
    with L.tx() as db:
        db.execute(
            """INSERT INTO mail_seen(account,folder,uidvalidity,uid,message_id,sender,subject,received,result,at)
                      VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account,folder,uidvalidity,uid) DO UPDATE SET
                      message_id=excluded.message_id, sender=excluded.sender, subject=excluded.subject,
                      received=excluded.received, result=excluded.result, at=excluded.at""",
            (
                account,
                folder,
                validity,
                uid,
                mid,
                msg.sender if msg else None,
                msg.subject if msg else None,
                msg.date if msg else None,
                result,
                now(),
            ),
        )


def _failed(L: Ledger, account: str, folder: str, validity: int, uid: int) -> None:
    """One more failed attempt at this message: retried on the next runs, up to ATTEMPTS times."""
    with L.tx() as db:
        db.execute(
            """INSERT INTO mail_seen(account,folder,uidvalidity,uid,message_id,result,at,attempts)
                      VALUES(?,?,?,?,?,'error',?,1) ON CONFLICT(account,folder,uidvalidity,uid) DO UPDATE SET
                      attempts=attempts+1, at=excluded.at WHERE result='error'""",
            (account, folder, validity, uid, f"uid:{validity}:{uid}", now()),
        )


def read(
    L: Ledger,
    account: integrations.Instance,
    handle: Callable[[Message], str],
    *,
    today: date,
    connect: Callable[[str, int, float, float], imaplib.IMAP4] | None = None,
) -> dict:
    """Hand every new message of the account's reading folder to `handle` and remember it with the result `handle`
    returns. A message that fails (fetch, parse or handler) is retried on later runs; the run goes on.
    Returns {"read", "left", "errors"}; raises ReadError when the account or its folder can't be opened."""
    deadline = time.monotonic() + BUDGET
    folder = account.config.get("read_folder") or "INBOX"
    quoted = _folder(folder)
    m, limit = _open(account, connect or _connect, deadline)
    out = {"read": 0, "left": 0, "errors": 0}
    try:
        code = "folder_missing"  # only the server's refusal of the folder; a failure after it is another kind
        try:
            typ, _ = m.select(quoted, readonly=True)
            validity = m.response("UIDVALIDITY")[1]
            validity = int(validity[0]) if typ == "OK" and validity and validity[0] else None
            uids = None
            if typ == "OK":
                code = "protocol_error"
                since = today - timedelta(days=WINDOW_DAYS)
                typ, data = m.uid("SEARCH", "SINCE", f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}")
                uids = None if typ != "OK" else sorted({int(x) for x in data[0].split()}) if data and data[0] else []
        except (OSError, imaplib.IMAP4.error, ValueError, _OutOfTime, _TooLarge) as exc:
            validity, uids, code = None, None, _failure(exc, "folder")
        if validity is None or uids is None:
            raise ReadError("Dosarul citit nu poate fi deschis. Se verifică numele lui în Setări, Integrări.", code)
        rows = {
            r["uid"]: (r["result"], r["attempts"])
            for r in L.db.execute(
                "SELECT uid, result, attempts FROM mail_seen WHERE account=? AND folder=? AND uidvalidity=?",
                (account.id, folder, validity),
            )
        }
        retry = [u for u in uids if u in rows and rows[u][0] == "error" and rows[u][1] < ATTEMPTS]
        queue = [u for u in uids if u not in rows] + retry  # new ones first: failures never hold them back
        out["left"] = max(0, len(queue) - MAX_MESSAGES)
        queue = queue[:MAX_MESSAGES]
        for i, uid in enumerate(queue):
            fail = lambda uid=uid: _failed(L, account.id, folder, validity, uid)  # noqa: E731
            try:
                size = _size(m, uid)
                if size is not None and size > MESSAGE_MAX:
                    _record(L, account.id, folder, validity, uid, f"uid:{validity}:{uid}", "too-large")
                    continue
                raw = _body(m, uid, limit) if size is not None else None
            except _OutOfTime:
                out["left"] += len(queue) - i
                break
            except _TooLarge:  # the server ignored the partial range; the connection can't be trusted any more
                _record(L, account.id, folder, validity, uid, f"uid:{validity}:{uid}", "too-large")
                out["left"] += len(queue) - i - 1
                break
            except (OSError, imaplib.IMAP4.error) as exc:
                fail()
                raise ReadError(
                    "Legătura cu contul de e-mail s-a întrerupt. Citirea se reia la următoarea verificare.",
                    _failure(exc, "fetch"),
                ) from None
            if raw is None:
                fail()
                out["errors"] += 1
                continue
            if len(raw) > MESSAGE_MAX:
                _record(L, account.id, folder, validity, uid, f"uid:{validity}:{uid}", "too-large")
                continue
            try:
                msg = parse(account.id, raw, today)
            except Exception:  # a malformed message is retried, then left as an error; it never stops the run
                fail()
                out["errors"] += 1
                continue
            if seen(L, account.id, msg.message_id):
                _record(L, account.id, folder, validity, uid, msg.message_id, "same-message", msg)
                continue
            try:
                result = handle(msg)
            except Exception:  # retried on the next runs
                fail()
                out["errors"] += 1
                continue
            _record(L, account.id, folder, validity, uid, msg.message_id, result, msg)
            out["read"] += 1
    finally:
        _logout(m)
    return out


# ------------------------------------------------------------------ searching incoming payment proofs

TEXT_BYTES = 200_000  # of one message read for its text
SEARCH_MAX = 20  # messages read per search


def _quote(v: str) -> str:
    return '"' + re.sub(r"[\x00-\x1f\x7f]", " ", v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _day(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _search(m: imaplib.IMAP4, *criteria: str) -> list[int]:
    typ, data = m.uid("SEARCH", *criteria)
    return sorted({int(x) for x in data[0].split()}) if typ == "OK" and data and data[0] else []


def _text(msg) -> str:
    """The message's text: its plain parts, else its HTML with the tags removed (never rendered)."""
    plain, rich = [], []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html") and not part.get_filename():
            try:
                (plain if part.get_content_type() == "text/plain" else rich).append(part.get_content())
            except Exception:  # a part that can't be decoded reads as empty
                continue
    raw = " ".join(plain) or html.unescape(
        re.sub(r"<(style|script)\b.*?</\1>|<[^>]+>", " ", " ".join(rich), flags=re.S | re.I)
    )
    return re.sub(r"\s+", " ", raw).strip()


def texts(L: Ledger, start: date, end: date, words: list[str], *, connect=None) -> list[dict]:
    """Messages of the reading folders dated start..end whose text holds one of `words`, newest SEARCH_MAX per account,
    as {"key", "sender", "subject", "date", "text"} (key: imap:<Message-ID>, the same in every account, so a mail
    reserved as one invoice's proof can't prove another through a copy). Read-only, bounded like read();
    an account that can't be read is skipped. Untrusted content: callers look only for fixed phrases and amounts."""
    words = [w for w in words if w.isascii() and w.strip()]
    if not words:
        return []
    criteria = ["SINCE", _day(start), "BEFORE", _day(end + timedelta(days=1))]
    ors = [("TEXT", _quote(w)) for w in words]
    for _i in range(len(ors) - 1):
        criteria.append("OR")
    for c in ors:
        criteria.extend(c)
    out = []
    for account in accounts(L):
        if account.config.get("method") == "microsoft":
            continue
        try:
            m, limit = _open(account, connect or _connect, time.monotonic() + BUDGET)
        except ReadError:
            continue
        try:
            typ, _ = m.select(_folder(account.config.get("read_folder") or "INBOX"), readonly=True)
            for uid in (_search(m, *criteria) if typ == "OK" else [])[-SEARCH_MAX:]:
                limit[0] = TEXT_BYTES
                typ, data = m.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{TEXT_BYTES}>)")
                rec = _record_for(data, uid, b"BODY[]") if typ == "OK" else None
                if not rec or not rec[1]:
                    continue
                msg = email.message_from_bytes(rec[1], policy=email.policy.default)
                mid = _message_id(msg.get("Message-ID")) or "sha256:" + hashlib.sha256(rec[1]).hexdigest()
                if any(m["key"] == f"imap:{mid}" for m in out):
                    continue  # the same mail in two accounts is one mail: one proof, ever
                out.append(
                    {
                        "key": f"imap:{mid}",
                        "sender": _plain(msg.get("From")),
                        "subject": _plain(msg.get("Subject")),
                        "date": date.fromisoformat(_date(msg, end)),
                        "text": _text(msg),
                    }
                )
        except (OSError, imaplib.IMAP4.error, ValueError, _OutOfTime, _TooLarge, ReadError):
            continue
        finally:
            _logout(m)
    return out


def own_addresses(ledger: Ledger | None = None) -> tuple[str, ...]:
    """Every saved account's addresses remain ours, even after changing or disabling delivery."""
    if ledger is None:
        try:
            reader = Ledger(readonly=True)
        except sqlite3.OperationalError:
            return ()
        try:
            return own_addresses(reader)
        finally:
            reader.close()
    addresses = []
    for row in ledger.db.execute("SELECT id FROM integrations WHERE kind='mail'"):
        account = integrations.instance(ledger, row["id"])
        cfg = account.config
        addresses.extend([cfg["from_address"], *cfg["own_addresses"].split(",")])
    return tuple(dict.fromkeys(a.strip().lower() for a in addresses if a.strip()))


def is_own_sender(sender: str, ledger: Ledger | None = None) -> bool:
    return bool(set(own_addresses(ledger)) & {a.lower() for _, a in email.utils.getaddresses([sender])})
