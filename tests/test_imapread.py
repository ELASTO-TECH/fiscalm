"""Read-only IMAP reading (fiscalm.mail.imap) against a fake IMAP server on localhost; synthetic mails only."""

import json
from datetime import date, timedelta
from email.message import EmailMessage

import pytest
from imap_fake import Fake, mail, plain_connect

from fiscalm import integrations
from fiscalm.mail import imap as imapread, reader

TODAY = date(2026, 11, 3)
PDF = b"%PDF-1.4\n% synthetic invoice\n"
UBL = b'<?xml version="1.0"?><Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'
ALLOWED = {"CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


def account(L, fake, *, folder="INBOX", password="fake-password", name="Synthetic mail"):
    row = integrations.add(
        L,
        "mail",
        name,
        {
            "host": "127.0.0.1",
            "port": str(fake.port),
            "username": "reader",
            "password": password,
            "from_address": "books@firm.example",
            "read_folder": folder,
        },
    )
    return integrations.instance(L, row["id"])


def run(L, acc, handle=None, **kw):
    got = []

    def keep(m):
        got.append(m)
        return "ok"

    out = imapread.read(L, acc, handle or keep, today=TODAY, connect=plain_connect, **kw)
    return out, got


def test_reads_new_messages_once_and_changes_nothing(ledger, fake):
    fake.add(
        mail(
            attachments=(
                ("../../etc/passwd.pdf", "application/pdf", PDF),
                ("factura.xml", "application/xml", UBL),
                ("logo.png", "image/png", b"\x89PNG\r\n\x1a\n"),
                ("notes.zip", "application/zip", b"PK"),
            )
        ),
        TODAY - timedelta(days=1),
    )
    fake.add(mail(message_id="<two@cloud.example>", subject="Newsletter"), TODAY)
    acc = account(ledger, fake)
    out, got = run(ledger, acc)
    assert out == {"read": 2, "left": 0, "errors": 0}
    first = got[0]
    assert [a.kind for a in first.attachments] == ["pdf", "xml"]  # by content; PNG and ZIP aren't documents
    assert first.attachments[0].name == "../../etc/passwd.pdf"  # kept as data only, never a path
    assert first.message_id == "inv-1@cloud.example" and first.address == "billing@cloud.example"
    assert first.subject == "Your invoice" and first.date == "2026-11-02"
    assert got[1].attachments == ()
    # Nothing changed on the server: read-only open, PEEK fetches, no other command.
    assert fake.verbs() <= ALLOWED and "EXAMINE" in fake.verbs()
    assert all(not m.flags for m in fake.folders["INBOX"].messages)
    assert all("PEEK" in c for c in fake.commands if c.upper().startswith("UID FETCH") and "BODY" in c)
    # The second run finds both remembered and fetches nothing.
    fetches = sum(c.upper().startswith("UID FETCH") for c in fake.commands)
    assert run(ledger, acc)[0] == {"read": 0, "left": 0, "errors": 0}
    assert sum(c.upper().startswith("UID FETCH") for c in fake.commands) == fetches
    rows = ledger.db.execute("SELECT message_id, result, sender FROM mail_seen ORDER BY uid").fetchall()
    assert [tuple(r) for r in rows] == [
        ("inv-1@cloud.example", "ok", "Example Cloud <billing@cloud.example>"),
        ("two@cloud.example", "ok", None),
    ]  # no document: no sender kept


def test_only_the_window_is_searched(ledger, fake):
    fake.add(mail(message_id="<old@x.example>"), TODAY - timedelta(days=imapread.WINDOW_DAYS + 1))
    fake.add(mail(message_id="<new@x.example>"), TODAY - timedelta(days=imapread.WINDOW_DAYS))
    _, got = run(ledger, account(ledger, fake))
    assert [m.message_id for m in got] == ["new@x.example"]
    assert "UID SEARCH SINCE 04-Oct-2026" in fake.commands


def test_count_limit_leaves_the_rest_for_the_next_run(ledger, fake, monkeypatch):
    monkeypatch.setattr(imapread, "MAX_MESSAGES", 2)
    for i in range(3):
        fake.add(mail(message_id=f"<m{i}@x.example>"), TODAY)
    acc = account(ledger, fake)
    out, got = run(ledger, acc)
    assert out["read"] == 2 and out["left"] == 1 and [m.message_id for m in got] == ["m0@x.example", "m1@x.example"]
    out, got = run(ledger, acc)
    assert out == {"read": 1, "left": 0, "errors": 0} and got[0].message_id == "m2@x.example"


def test_the_budget_holds_against_a_server_that_trickles(ledger, fake, monkeypatch):
    import time

    for i in range(3):
        fake.add(mail(message_id=f"<t{i}@x.example>"), TODAY)
    acc = account(ledger, fake)
    monkeypatch.setattr(imapread, "BUDGET", 1.5)
    fake.delay = 0.4  # every FETCH reply arrives one slow byte at a time: about 0.8 s each
    started = time.monotonic()
    out, got = run(ledger, acc)
    assert time.monotonic() - started < 4 and out["left"] >= 2 and out["read"] <= 1
    fake.delay = 0
    monkeypatch.setattr(imapread, "BUDGET", 120.0)
    out, got = run(ledger, acc)  # the next run goes on from there
    assert (
        len(got) + out["read"] >= 1
        and ledger.db.execute("SELECT COUNT(*) FROM mail_seen WHERE result='ok'").fetchone()[0] == 3
    )


def test_large_messages_are_never_downloaded(ledger, fake, monkeypatch):
    monkeypatch.setattr(imapread, "MESSAGE_MAX", 2000)
    fake.add(mail(message_id="<big@x.example>", attachments=(("a.pdf", "application/pdf", PDF + b"0" * 5000),)), TODAY)
    # A server that claims a small size still can't send more than MESSAGE_MAX + 1 bytes (partial fetch).
    fake.add(
        mail(message_id="<liar@x.example>", attachments=(("b.pdf", "application/pdf", PDF + b"1" * 5000),)),
        TODAY,
        size=100,
    )
    out, got = run(ledger, account(ledger, fake))
    assert got == [] and out["read"] == 0
    results = [tuple(r) for r in ledger.db.execute("SELECT uid, result FROM mail_seen ORDER BY uid")]
    assert results == [(1, "too-large"), (2, "too-large")]
    body_fetches = [c for c in fake.commands if "BODY.PEEK[]" in c]
    assert body_fetches == ["UID FETCH 2 (BODY.PEEK[]<0.2001>)"]  # the first was refused by its size alone


def test_a_server_ignoring_the_range_is_cut_off_before_the_literal_is_read(ledger, fake, monkeypatch):
    monkeypatch.setattr(imapread, "MESSAGE_MAX", 2000)
    fake.ignore_partial = True
    fake.add(
        mail(message_id="<huge@x.example>", attachments=(("a.pdf", "application/pdf", PDF + b"0" * 50000),)),
        TODAY,
        size=100,
    )
    fake.add(mail(message_id="<next@x.example>"), TODAY)
    acc = account(ledger, fake)
    out, got = run(ledger, acc)
    assert got == [] and out["left"] == 1
    assert ledger.db.execute("SELECT result FROM mail_seen WHERE uid=1").fetchone()[0] == "too-large"
    fake.ignore_partial = False
    out, got = run(ledger, acc)  # the next run skips it and reads the rest
    assert [m.message_id for m in got] == ["next@x.example"]


def test_a_fetch_that_fails_is_read_again(ledger, fake):
    fake.add(mail(), TODAY)
    fake.fetch_no = {1}
    acc = account(ledger, fake)
    assert run(ledger, acc)[0] == {"read": 0, "left": 0, "errors": 1}
    out, got = run(ledger, acc)
    assert out["read"] == 1 and got[0].message_id == "inv-1@cloud.example"


def test_failing_messages_never_hold_back_new_ones(ledger, fake, monkeypatch):
    monkeypatch.setattr(imapread, "MAX_MESSAGES", 2)
    for i in range(2):
        fake.add(mail(message_id=f"<bad{i}@x.example>", subject="poison"), TODAY)
    acc = account(ledger, fake)

    def handle(m):
        if m.subject == "poison":
            raise RuntimeError("always fails")
        return "ok"

    assert run(ledger, acc, handle)[0]["errors"] == 2
    fake.add(mail(message_id="<good@x.example>"), TODAY)
    out, got = run(ledger, acc, handle)
    assert out["read"] == 1  # the new one first, before the retries
    for _ in range(imapread.ATTEMPTS):
        run(ledger, acc, handle)
    rows = dict(ledger.db.execute("SELECT uid, attempts FROM mail_seen WHERE result='error'").fetchall())
    assert rows == {1: imapread.ATTEMPTS, 2: imapread.ATTEMPTS}  # then left as errors, no longer fetched
    fetches = len(fake.commands)
    run(ledger, acc, handle)
    assert not any(c.startswith("UID FETCH") for c in fake.commands[fetches:])


def test_an_unsolicited_fetch_never_stands_for_the_message_asked(ledger, fake):
    fake.add(mail(message_id="<one@x.example>", subject="first"), TODAY)
    fake.add(mail(message_id="<two@x.example>", subject="second"), TODAY)
    fake.unsolicited = True
    _, got = run(ledger, account(ledger, fake))
    assert [(m.message_id, m.subject) for m in got] == [("one@x.example", "first"), ("two@x.example", "second")]


def test_a_new_uidvalidity_reads_again_but_each_message_once(ledger, fake):
    fake.add(mail(), TODAY)
    acc = account(ledger, fake)
    assert run(ledger, acc)[0]["read"] == 1
    fake.folders["INBOX"].validity = 8  # the server renumbered the folder
    out, got = run(ledger, acc)
    assert (
        got == []
        and ledger.db.execute("SELECT result FROM mail_seen WHERE uidvalidity=8").fetchone()[0] == "same-message"
    )


def test_a_malformed_message_never_stops_the_run(ledger, fake, monkeypatch):
    fake.add(mail(message_id="<bad@x.example>"), TODAY)
    fake.add(mail(message_id="<good@x.example>"), TODAY)
    real = imapread.parse

    def parse(account, raw, today):
        if b"bad@x.example" in raw:
            raise RecursionError("deeply nested MIME")
        return real(account, raw, today)

    monkeypatch.setattr(imapread, "parse", parse)
    out, got = run(ledger, account(ledger, fake))
    assert out == {"read": 1, "left": 0, "errors": 1} and got[0].message_id == "good@x.example"


def test_attachment_limits(ledger, fake, monkeypatch):
    monkeypatch.setattr(imapread, "ATTACHMENT_MAX", 100)
    monkeypatch.setattr(imapread, "ATTACHMENTS_MAX", 2)
    files = (("big.pdf", "application/pdf", PDF + b"x" * 200),) + tuple(
        (f"{i}.pdf", "application/pdf", PDF + bytes([65 + i])) for i in range(3)
    )
    fake.add(mail(attachments=files), TODAY)
    _, got = run(ledger, account(ledger, fake))
    assert [a.data[-1:] for a in got[0].attachments] == [b"A", b"B"]


def test_same_message_in_two_folders_is_read_once(ledger, fake):
    raw = mail(message_id="<dup@x.example>")
    fake.add(raw, TODAY)
    fake.add(raw, TODAY, folder="Facturi")  # a copy in another folder
    acc = account(ledger, fake)
    out, got = run(ledger, acc)
    assert len(got) == 1 and out["read"] == 1
    integrations.update(ledger, acc.id, {"read_folder": "Facturi"})
    out, got = run(ledger, integrations.instance(ledger, acc.id))
    assert got == [] and sorted(r[0] for r in ledger.db.execute("SELECT result FROM mail_seen")) == [
        "ok",
        "same-message",
    ]
    other = account(ledger, fake, folder="Facturi", name="Other account")
    assert run(ledger, other)[0]["read"] == 1  # another account keeps its own memory


def test_message_without_id_is_known_by_its_digest(ledger, fake):
    fake.add(mail(message_id=None), TODAY)
    _, got = run(ledger, account(ledger, fake))
    assert got[0].message_id.startswith("sha256:") and len(got[0].message_id) == 71


def test_a_failing_handler_reads_the_message_again(ledger, fake):
    fake.add(mail(), TODAY)
    acc = account(ledger, fake)

    def boom(m):
        raise RuntimeError("crash while filing")

    assert run(ledger, acc, boom)[0] == {"read": 0, "left": 0, "errors": 1}
    assert [tuple(r) for r in ledger.db.execute("SELECT result, attempts FROM mail_seen")] == [("error", 1)]
    out, got = run(ledger, acc)
    assert out["read"] == 1 and got[0].message_id == "inv-1@cloud.example"


def test_forwarded_message_attachment_is_found(ledger, fake):
    inner = EmailMessage()
    inner["From"] = "Airline <tickets@air.example>"
    inner["Subject"] = "Receipt"
    inner.set_content("x")
    inner.add_attachment(PDF, maintype="application", subtype="pdf", filename="r.pdf")
    outer = EmailMessage()
    outer["From"] = "Someone <a@firm.example>"
    outer["Message-ID"] = "<fwd@firm.example>"
    outer["Subject"] = "Fwd: Receipt"
    outer.set_content("fyi")
    outer.add_attachment(inner)
    fake.add(outer.as_bytes().replace(b"\n", b"\r\n"), TODAY)
    _, got = run(ledger, account(ledger, fake))
    assert [a.kind for a in got[0].attachments] == ["pdf"]


def test_wrong_password_and_missing_folder_refuse_without_secrets(ledger, fake):
    bad = account(ledger, fake, password="wrong-password", name="Bad")
    with pytest.raises(imapread.ReadError) as e:
        run(ledger, bad)
    assert "wrong-password" not in str(e.value) and "AUTHENTICATIONFAILED" not in str(e.value)
    assert e.value.code == "wrong_password"
    with pytest.raises(imapread.ReadError) as e:
        run(ledger, account(ledger, fake, folder="Nu există", name="No folder"))
    assert e.value.code == "folder_missing"


@pytest.mark.parametrize("command", ["SEARCH", "FETCH"])
@pytest.mark.parametrize("error, code", [(OSError("reset"), "host_unreachable"), (TimeoutError(), "timeout")])
def test_a_link_lost_after_the_folder_opened_is_not_a_missing_folder(ledger, fake, command, error, code):
    fake.add(mail(), TODAY)

    def dropping(*a):
        m = plain_connect(*a)
        uid = m.uid
        m.uid = lambda *args: (_ for _ in ()).throw(error) if args[0] == command else uid(*args)
        return m

    with pytest.raises(imapread.ReadError) as e:
        imapread.read(ledger, account(ledger, fake), lambda m: "ok", today=TODAY, connect=dropping)
    assert e.value.code == code


def test_read_all_keeps_going_and_records_each_account(ledger, fake):
    fake.add(mail(), TODAY)
    bad = account(ledger, fake, password="wrong-password", name="Bad")
    good = account(ledger, fake, name="Good")
    off = account(ledger, fake, name="Off")
    integrations.set_enabled(ledger, off.id, False)  # Oprește is the one switch
    res = reader.read_all(ledger, lambda m: "ok", today=TODAY, connect=plain_connect)
    assert [(r["account"], r["ok"]) for r in res] == [(bad.id, False), (good.id, True)]
    saved = json.loads(ledger.get_meta(f"mail_read:{bad.id}"))
    assert saved["ok"] is False and saved["code"] == "wrong_password" and "wrong-password" not in json.dumps(saved)


def test_folder_names_are_quoted_and_checked():
    assert imapread._folder('Facturi "noi"') == '"Facturi \\"noi\\""'
    assert imapread._folder("Încasări & plăți") == '"&AM4-ncas&AQM-ri &- pl&AQMCGw-i"'
    for bad in ("", "INBOX\x01", "a\x7fb"):
        with pytest.raises(imapread.ReadError):
            imapread._folder(bad)


def test_an_empty_folder_reads_nothing(ledger, fake):
    assert run(ledger, account(ledger, fake))[0] == {"read": 0, "left": 0, "errors": 0}


def test_reading_is_on_by_default(ledger, fake):
    row = integrations.add(
        ledger,
        "mail",
        "Plain",
        {"host": "127.0.0.1", "username": "reader", "password": "x", "from_address": "books@firm.example"},
    )
    assert row["config"]["read_folder"] == "INBOX" and "read_invoices" not in row["config"]
    assert [a.config["host"] for a in imapread.accounts(ledger)] == ["127.0.0.1"]
    with pytest.raises(integrations.IntegrationError):  # no second reading switch
        integrations.add(
            ledger, "mail", "Bad", {"host": "h.example", "username": "u", "password": "x", "read_invoices": "yes"}
        )


def test_a_uid_after_the_literal_still_names_the_message(ledger, fake):
    fake.add(mail(), TODAY)
    fake.uid_last = True
    out, got = run(ledger, account(ledger, fake))
    assert out["read"] == 1 and got[0].message_id == "inv-1@cloud.example"


def test_a_trickling_greeting_is_bounded_too(ledger, fake, monkeypatch):
    import time

    monkeypatch.setattr(imapread, "BUDGET", 1.0)
    fake.greeting_delay = 0.3  # the whole greeting would take about 7 s
    started = time.monotonic()
    with pytest.raises(imapread.ReadError):
        run(ledger, account(ledger, fake))
    assert time.monotonic() - started < 3


def test_an_oversized_literal_is_refused_unread(ledger, fake, monkeypatch):
    monkeypatch.setattr(imapread, "MESSAGE_MAX", 2000)
    fake.ignore_partial = True
    fake.add(
        mail(message_id="<huge@x.example>", attachments=(("a.pdf", "application/pdf", PDF + b"0" * 500000),)),
        TODAY,
        size=100,
    )
    received = []
    real = imapread._Reader._more

    def more(self):
        real(self)
        received.append(len(self.buf))

    monkeypatch.setattr(imapread._Reader, "_more", more)
    run(ledger, account(ledger, fake))
    assert max(received) < 200000  # the 500 kB literal was never read in


def test_texts_and_sent_are_read_only_searches(ledger, fake):
    fake.add(mail(sender="Lumen <shop@lumen.example>", subject="Plata primita", body="Lumen 486,00 lei"), TODAY)
    fake.add(mail(message_id="<x@other.example>", body="unrelated"), TODAY)
    fake.add(mail(message_id="<old@lumen.example>", body="Lumen"), TODAY - timedelta(days=40))
    account(ledger, fake)
    got = imapread.texts(ledger, TODAY - timedelta(days=7), TODAY, ["lumen", "netopia"], connect=plain_connect)
    assert [(m["key"], m["subject"], m["text"]) for m in got] == [
        ("imap:inv-1@cloud.example", "Plata primita", "Lumen 486,00 lei")
    ]
    account(ledger, fake, name="Second")  # the same mail seen through a second account is still one proof
    assert len(imapread.texts(ledger, TODAY - timedelta(days=7), TODAY, ["lumen"], connect=plain_connect)) == 1
    assert fake.verbs() <= ALLOWED


@pytest.mark.parametrize(
    "sender, results, domain",
    [
        ("Pay <no@pay.example>", ["mx.own.example; dmarc=pass (p=reject) header.from=pay.example"], "pay.example"),
        ("Pay <no@pay.example>", ["mx.own.example; dmarc=pass header.from=other.example"], ""),
        ("Pay <no@pay.example>", ["mx.own.example; dmarc=fail header.from=pay.example"], ""),
        # a verdict under another service's name is the sender's to write
        ("Pay <no@pay.example>", ["mx.forged.example; dmarc=pass header.from=pay.example"], ""),
        ("Pay <no@pay.example>", ["mx.own.example; dmarc=none", "x; dmarc=pass header.from=pay.example"], ""),
        ("Pay <no@pay.example>", [], ""),
        ("a@pay.example, b@pay.example", ["mx.own.example; dmarc=pass header.from=pay.example"], ""),
    ],
)
def test_only_the_mailbox_providers_dmarc_pass_authenticates_a_sender(sender, results, domain):
    msg = EmailMessage()
    msg["From"] = sender
    for r in results:
        msg["Authentication-Results"] = r
    assert imapread.authenticated_domain(msg, "mx.own.example") == domain
    assert imapread.authenticated_domain(msg, "") == ""  # a provider fiscalm doesn't know: never authenticated
