"""A tiny IMAP4rev1 server on localhost for tests: synthetic messages, no TLS, no network beyond 127.0.0.1.

It speaks just enough for imaplib (CAPABILITY, LOGIN, LIST, SELECT/EXAMINE, UID SEARCH, UID FETCH, LOGOUT), records
every command, and keeps real flag semantics: a FETCH of BODY[] without PEEK sets \\Seen, STORE changes flags, so a
test can prove the reader changed nothing.
"""

from __future__ import annotations

import re
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Msg:
    uid: int
    raw: bytes
    day: date
    flags: set = field(default_factory=set)
    size: int | None = None  # what RFC822.SIZE reports (None: the true size)


@dataclass
class Box:
    messages: list[Msg] = field(default_factory=list)
    validity: int = 7


class Fake:
    def __init__(self, user: str = "reader", password: str = "fake-password"):
        self.user, self.password = user, password
        self.folders: dict[str, Box] = {"INBOX": Box()}
        self.commands: list[str] = []
        self.fail_after: int | None = None  # drop the connection after this many FETCH commands
        self.fetch_no: set[int] = set()  # UIDs whose next FETCH answers NO (once)
        self.ignore_partial = False  # send the whole message whatever range was asked
        self.unsolicited = False  # put a FETCH of another message before the one asked for
        self.delay = 0.0  # seconds before each FETCH reply, one byte at a time
        self.sent: str | None = None  # the folder LIST marks \\Sent
        self.greeting_delay = 0.0  # the greeting, one byte at a time, this many seconds apart
        self.uid_last = False  # put UID after the literal in FETCH replies
        self.lower_atoms = False  # write FLAGS as "flags" (valid: IMAP atoms ignore case)
        self._fetches = 0
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                fake._serve(self.rfile, self.wfile)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add(self, raw: bytes, day: date, folder: str = "INBOX", size: int | None = None) -> Msg:
        box = self.folders.setdefault(folder, Box())
        m = Msg(max([x.uid for x in box.messages], default=0) + 1, raw, day, size=size)
        box.messages.append(m)
        return m

    def verbs(self) -> set[str]:
        out = set()
        for c in self.commands:
            parts = c.split()
            out.add(" ".join(parts[:2]).upper() if parts[0].upper() == "UID" else parts[0].upper())
        return out

    # ---------------------------------------------------------------- protocol
    def _serve(self, rfile, wfile):
        def send(b: bytes):
            wfile.write(b)
            wfile.flush()

        if not self._greet(send):
            return
        box: Box | None = None
        while True:
            line = rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            tag, _, rest = text.partition(" ")
            self.commands.append(rest)
            cmd = rest.split(" ", 1)[0].upper()
            arg = rest.split(" ", 1)[1] if " " in rest else ""
            if cmd == "CAPABILITY":
                send(b"* CAPABILITY IMAP4rev1\r\n" + f"{tag} OK done\r\n".encode())
            elif cmd == "LOGIN":
                self._login(send, tag, arg)
            elif cmd == "LIST":
                for name in self.folders:
                    flags = "\\Sent" if name == self.sent else "\\HasNoChildren"
                    send(f'* LIST ({flags}) "/" "{name}"\r\n'.encode())
                send(f"{tag} OK done\r\n".encode())
            elif cmd in ("SELECT", "EXAMINE"):
                name = arg.strip()
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\") if name.startswith('"') else name
                box = self.folders.get(name)
                if box is None:
                    send(f"{tag} NO no such folder\r\n".encode())
                    continue
                ro = "[READ-ONLY]" if cmd == "EXAMINE" else "[READ-WRITE]"
                send(
                    f"* FLAGS (\\Seen \\Answered)\r\n* {len(box.messages)} EXISTS\r\n* 0 RECENT\r\n"
                    f"* OK [UIDVALIDITY {box.validity}] ok\r\n{tag} OK {ro} done\r\n".encode()
                )
            elif cmd == "UID" and box is not None:
                if not self._uid(send, tag, arg, box):
                    return
            elif cmd == "LOGOUT":
                send(f"* BYE bye\r\n{tag} OK done\r\n".encode())
                return
            else:
                send(f"{tag} BAD unsupported\r\n".encode())

    def _login(self, send, tag: str, arg: str) -> None:
        words = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', arg)
        vals = [(a or b).replace('\\"', '"').replace("\\\\", "\\") for a, b in words]
        ok = vals == [self.user, self.password]
        send(f"{tag} {'OK logged in' if ok else 'NO [AUTHENTICATIONFAILED] bad'}\r\n".encode())

    def _greet(self, send) -> bool:
        if self.greeting_delay:
            for ch in b"* OK fake IMAP ready\r\n":
                time.sleep(self.greeting_delay)
                try:
                    send(bytes([ch]))
                except OSError:
                    return False
        else:
            send(b"* OK fake IMAP ready\r\n")
        return True

    def _uid(self, send, tag: str, arg: str, box: Box) -> bool:
        sub, _, spec = arg.partition(" ")
        if sub.upper() == "SEARCH":
            uids = " ".join(str(x.uid) for x in box.messages if _matches(x, _tokens(spec)))
            send(f"* SEARCH {uids}\r\n{tag} OK done\r\n".encode())
        elif sub.upper() == "FETCH":
            return self._fetch(send, tag, spec, box)
        elif sub.upper() == "STORE":
            send(f"{tag} OK done\r\n".encode())
            for x in box.messages:
                x.flags.add("touched")
        else:
            send(f"{tag} BAD unsupported\r\n".encode())
        return True

    def _fetch(self, send, tag: str, spec: str, box: Box) -> bool:
        self._fetches += 1
        if self.fail_after is not None and self._fetches > self.fail_after:
            return False  # the server goes away mid-run
        uid_s, _, what = spec.partition(" ")
        if uid_s.isdigit() and int(uid_s) in self.fetch_no:
            self.fetch_no.discard(int(uid_s))
            send(f"{tag} NO try later\r\n".encode())
            return True
        if self.delay:
            for ch in b"* ":
                time.sleep(self.delay)
                send(bytes([ch]))
            send(b"OK still here\r\n")
        if self.unsolicited:
            other = next((x for x in box.messages if str(x.uid) != uid_s), None)
            if other:
                lit = other.raw
                send(b"* 99 FETCH (UID %d BODY[]<0> {%d}\r\n" % (other.uid, len(lit)) + lit + b")\r\n")
        for seq, x in enumerate(box.messages, 1):
            if str(x.uid) != uid_s:
                continue
            self._fetch_message(send, seq, x, what)
        send(f"{tag} OK done\r\n".encode())
        return True

    def _fetch_message(self, send, seq: int, x: Msg, what: str) -> None:
        items = [f"UID {x.uid}".encode()]
        literal = b""
        if "RFC822.SIZE" in what:
            items.append(f"RFC822.SIZE {x.size if x.size is not None else len(x.raw)}".encode())
        if "FLAGS" in what.split("BODY")[0]:
            items.append((("flags (" if self.lower_atoms else "FLAGS (") + " ".join(sorted(x.flags)) + ")").encode())
        if "HEADER.FIELDS" in what:
            fields = re.search(r"HEADER\.FIELDS \(([^)]*)\)", what).group(1)
            want = [f.lower().encode() + b":" for f in fields.split()]
            head = x.raw.split(b"\r\n\r\n", 1)[0]
            literal = b"\r\n".join(h for h in head.split(b"\r\n") if h.lower().startswith(tuple(want))) + b"\r\n\r\n"
            items.append(b"BODY[HEADER.FIELDS (%s)] {%d}" % (fields.encode(), len(literal)))
        elif "BODY" in what:
            part = re.search(r"<0\.(\d+)>", what)
            literal = x.raw[: int(part.group(1))] if part and not self.ignore_partial else x.raw
            items.append((b"BODY[]<0> {%d}" if part else b"BODY[] {%d}") % len(literal))
            if "PEEK" not in what:
                x.flags.add("\\Seen")
        if self.uid_last and literal:
            items = items[1:]
            send(b"* %d FETCH (" % seq + b" ".join(items) + b"\r\n" + literal + b" UID %d)\r\n" % x.uid)
        else:
            send(b"* %d FETCH (" % seq + b" ".join(items) + (b"\r\n" + literal if literal else b"") + b")\r\n")


def _tokens(spec: str) -> list[str]:
    return [a or b for a, b in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', spec)]


def _matches(x: Msg, t: list[str]) -> bool:
    """IMAP SEARCH keys used by fiscalm: SINCE, BEFORE, SUBJECT, TO, FROM, TEXT and OR (prefix), all ANDed."""

    def one(i: int) -> tuple[bool, int]:
        key = t[i].upper()
        if key == "OR":
            a, j = one(i + 1)
            b, k = one(j)
            return a or b, k
        val = t[i + 1]
        if key in ("SINCE", "BEFORE"):
            d = datetime.strptime(val, "%d-%b-%Y").date()
            return (x.day >= d if key == "SINCE" else x.day < d), i + 2
        import email
        import email.policy

        msg = email.message_from_bytes(x.raw, policy=email.policy.default)
        hay = {
            "SUBJECT": str(msg["Subject"]),
            "TO": str(msg["To"]),
            "FROM": str(msg["From"]),
            "TEXT": x.raw.decode("utf-8", "replace"),
        }[key]
        return val.lower() in hay.lower(), i + 2

    i, ok = 0, True
    while i < len(t):
        r, i = one(i)
        ok = ok and r
    return ok


def plain_connect(host: str, port: int, timeout: float = 5, deadline: float | None = None):
    """imaplib without TLS for the fake server, bounded like the real connection (imapread.guarded)."""
    import imaplib
    import time

    from fiscalm.mail import imap as imapread

    return imapread.guarded(imaplib.IMAP4, deadline or time.monotonic() + imapread.BUDGET)(
        host, port, timeout=min(timeout, 5)
    )


def mail(
    *,
    sender: str = "Example Cloud <billing@cloud.example>",
    subject: str = "Your invoice",
    message_id: str | None = "<inv-1@cloud.example>",
    day: str = "Mon, 02 Nov 2026 10:00:00 +0000",
    attachments: tuple[tuple[str, str, bytes], ...] = (),
    body: str = "See attached.",
) -> bytes:
    """A synthetic RFC 822 message with (filename, content type, bytes) attachments."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "books@firm.example"
    msg["Subject"] = subject
    msg["Date"] = day
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    for name, ctype, data in attachments:
        main, sub = ctype.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


__all__ = ["Fake", "mail", "plain_connect", "socket"]
