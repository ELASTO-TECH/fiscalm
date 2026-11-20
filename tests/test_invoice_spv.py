import contextlib
import hashlib
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from http.client import RemoteDisconnected
from pathlib import Path
from threading import Barrier

import anaf_fake
import pytest
from test_api import (
    _api,  # noqa: F401
    server as server,
)
from test_invoicing import DATA, UPLOAD_DAY, draft, setup_invoice as setup_invoice

from fiscalm import cli, companies, integrations, rules
from fiscalm.api import state as state_api
from fiscalm.efactura import client as anaf, inbox
from fiscalm.invoicing import cli as invoice_cli, engine as i, issued, spv, ubl, workspace as invoicing_api
from fiscalm.ledger import Ledger


def archive(xml):
    """SPV's answer ZIP, the same bytes on every call (a fixed time in the entries, not the clock's)."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for name, data in (("invoice.xml", xml), ("semnatura_test.xml", "<signature/>")):
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return stream.getvalue()


@pytest.fixture(autouse=True)
def anaf_setup(home, monkeypatch):
    monkeypatch.setenv("FISCALM_SECRETS", str(home / "secrets"))
    L = Ledger()
    anaf_fake.setup(L)
    L.close()


@pytest.fixture
def ready(setup_invoice, home, monkeypatch):
    monkeypatch.setenv("FISCALM_ANAF_ENV", "test")
    (home / "state" / "LIVE").touch()
    L, _ = setup_invoice
    anaf_fake.setup(L)
    iid = draft(setup_invoice)
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    return L, iid


def fake_for(L, iid, calls, state="ok"):
    def transport(method, url, body):
        calls.append((method, url, body))
        assert url.startswith("https://api.anaf.ro/test/FCTEL/rest/")
        if "/upload?" in url:
            assert "standard=UBL" in url and "cif=99999984" in url
            assert body == ubl.generate(L, iid, company="NORD")
            return b'<header ExecutionStatus="0" index_incarcare="10001"/>'
        if "/stareMesaj?" in url:
            assert "id_incarcare=10001" in url
            return f'<header stare="{state}" id_descarcare="10002"/>'.encode()
        assert "/descarcare?id=10002" in url
        return archive(
            ubl.generate(L, iid, company="NORD")
            if state == "ok"
            else b'<errors><error errorMessage="BR-S-08 bank RO24BTRLRONCRT0000000001"/></errors>'
        )

    return transport


def test_upload_poll_store_and_idempotence(ready):
    L, iid = ready
    calls = []
    transport = fake_for(L, iid, calls)
    L.set_meta("invoice_upload_error:" + iid, "old")
    assert spv.auto_upload(L, iid, transport, company="NORD", today=UPLOAD_DAY)["status"] == "uploaded"
    assert L.get_meta("invoice_upload_error:" + iid) is None  # a later success clears the earlier failure
    assert spv.auto_upload(L, iid, transport, company="NORD", today=UPLOAD_DAY)["status"] == "uploaded"
    assert len(calls) == 1
    result = spv.poll(L, transport)[0]
    assert result["status"] == "accepted"
    path = Path(result["raw_path"])
    assert path.is_relative_to(L.path.parent.parent / "efactura" / "NORD")
    assert path.read_bytes().startswith(b"PK")
    assert spv.poll(L, transport) == []
    with pytest.raises(i.InvoiceError, match="Există deja o încercare de trimitere"):
        spv.upload(L, iid, transport, company="NORD")
    assert len(calls) == 3


def with_transport(monkeypatch, transport):
    """Run the tick's auto_upload against a fake ANAF and without polling."""
    real = spv.auto_upload
    monkeypatch.setattr(spv, "auto_upload", lambda L, iid, **kw: real(L, iid, transport, **kw))
    monkeypatch.setattr(i, "poll", lambda *_: [])


def test_tick_uploads_on_the_fourth_day_after_issue_never_at_issue(ready, monkeypatch):
    L, iid = ready  # issued in Live with auto-upload on, and nothing sent
    calls = []
    with_transport(monkeypatch, fake_for(L, iid, calls))
    assert spv.submission(L, iid) is None and spv.upload_on(L, i.invoice(L, iid, company="NORD")) == UPLOAD_DAY
    assert invoice_cli.tick(L, "2026-11-03")["submitted"] == 0 and calls == []
    assert spv.auto_upload(L, iid, company="NORD", today="2026-11-03") == {
        "invoice_id": iid,
        "status": "scheduled",
        "upload_on": UPLOAD_DAY,
    }
    assert invoice_cli.tick(L, UPLOAD_DAY)["submitted"] == 1 and len(calls) == 1


def test_a_pause_holds_an_invoice_issued_while_on_and_a_resume_releases_it(ready, monkeypatch):
    L, iid = ready
    # Issued while on: recorded apart from the manual key, which older releases read as "only a person sends it".
    assert (L.get_meta("invoice_auto_upload:" + iid), L.get_meta("invoice_manual_upload:" + iid)) == ("1", None)
    calls = []
    with_transport(monkeypatch, fake_for(L, iid, calls))
    spv.set_auto_upload(L, "NORD", False)
    assert invoice_cli.tick(L, UPLOAD_DAY)["submitted"] == 0 and calls == []
    spv.set_auto_upload(L, "NORD", True)
    assert spv.waiting_manual(L, "NORD") == 0
    assert invoice_cli.tick(L, UPLOAD_DAY)["submitted"] == 1 and len(calls) == 1


def test_a_pause_saved_between_the_decision_and_the_claim_sends_nothing(ready, monkeypatch):
    L, iid = ready
    real = spv.upload

    def pause_then_upload(*args, **kwargs):
        other = Ledger(L.path)
        try:
            spv.set_auto_upload(other, "NORD", False)
        finally:
            other.close()
        return real(*args, **kwargs)

    monkeypatch.setattr(spv, "upload", pause_then_upload)
    result = spv.auto_upload(L, iid, lambda *_: pytest.fail("sent after the pause"), company="NORD", today=UPLOAD_DAY)
    assert result["status"] == "disabled" and spv.submission(L, iid) is None
    assert L.get_meta("invoice_upload_error:" + iid) is None  # a deferral, not a failure


@pytest.mark.parametrize("days, day", [("0", "2026-10-31"), ("5", "2026-11-05")])
def test_the_firms_rule_sets_the_upload_day(ready, days, day):
    L, iid = ready
    with pytest.raises(i.InvoiceError):  # 6 calendar days can pass ANAF's 5 working days
        rules.set_spv_delay(L, "NORD", "6")
    rules.set_spv_delay(L, "NORD", days)
    assert spv.upload_on(L, i.invoice(L, iid, company="NORD")) == day
    spv.set_auto_upload(L, "NORD", False)
    with pytest.raises(i.InvoiceError):  # off, the day has no effect, so it can't be chosen
        rules.set_spv_delay(L, "NORD", "2")
    assert spv.upload_delay(L, "NORD") == int(days) and spv.upload_after_days(L, "NORD") is None


@pytest.mark.parametrize("state", ["nok", "XML cu erori nepreluat de sistem"])  # the second with an error ZIP to keep
def test_nok_number_retained_and_safe_retry(ready, state):
    L, iid = ready
    calls = []
    transport = fake_for(L, iid, calls, state)
    spv.upload(L, iid, transport, company="NORD")
    result = spv.poll(L, transport)[0]
    assert result["status"] == "rejected" and result["errors"] == ["BR-S-08"]  # rule codes only, never the bank details
    assert Path(result["raw_path"]).read_bytes().startswith(b"PK")
    assert i.invoice(L, iid, company="NORD")["number"] == 42
    assert spv.upload(L, iid, transport, company="NORD")["status"] == "uploaded"
    assert len(calls) == 4


def test_shadow_and_setting(ready, home, monkeypatch):
    L, iid = ready
    spv.set_auto_upload(L, "NORD", False)

    def fail(*_):
        return pytest.fail("unexpected network")

    assert spv.auto_upload(L, iid, fail, company="NORD", today=UPLOAD_DAY)["status"] == "disabled"
    (home / "state" / "LIVE").unlink()
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    with pytest.raises(i.InvoiceError):
        spv.upload(L, iid, fail, company="NORD")
    with pytest.raises(i.InvoiceError):
        spv.poll(L, fail)
    assert spv.submission(L, iid) is None


def test_timeout_unknown_never_resubmits(ready):
    L, iid = ready

    def timeout(*_):
        raise OSError("secret response that must never be stored")

    with pytest.raises(i.InvoiceError, match="Nu se știe"):
        spv.upload(L, iid, timeout, company="NORD")
    assert spv.submission(L, iid)["status"] == "unknown"
    assert "secret" not in str(spv.submission(L, iid))
    with pytest.raises(i.InvoiceError, match="Există deja o încercare de trimitere"):
        spv.upload(L, iid, lambda *_: pytest.fail("duplicate"), company="NORD")
    assert i.invoice(L, iid, company="NORD")["status"] == "issued"


def test_crash_and_environment_refusal(ready, monkeypatch):
    L, iid = ready
    calls = []
    spv.upload(L, iid, fake_for(L, iid, calls), company="NORD")
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    assert spv.poll(L, lambda *_: pytest.fail("wrong host"))[0]["status"] == "environment_mismatch"
    assert spv.upload(L, iid, lambda *_: b'<header index_incarcare="20001"/>', company="NORD")["status"] == "uploaded"


def test_safe_company_directory(ready, home):
    L, iid = ready
    root = home / "efactura"
    root.mkdir()
    (root / "STUDIO9").mkdir()
    (root / "NORD").symlink_to(root / "STUDIO9", target_is_directory=True)
    calls = []
    transport = fake_for(L, iid, calls)
    spv.upload(L, iid, transport, company="NORD")
    assert spv.poll(L, transport)[0]["status"] == "error"
    assert not list((root / "STUDIO9").iterdir())


def test_two_connections_upload_once(ready):
    L, iid = ready
    barrier = Barrier(2)
    calls = []

    def run(_):
        other = Ledger(L.path)
        try:
            barrier.wait(timeout=5)

            def transport(*args):
                calls.append(args)
                return b'<header index_incarcare="10001"/>'

            try:
                return spv.upload(other, iid, transport, company="NORD")
            except i.InvoiceError:
                return None
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(run, range(2)))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "answer,expected",
    [
        (b"<header/>", "unknown"),
        (b'<header index_incarcare="../x"/>', "unknown"),
        (b'<header ExecutionStatus="1" index_incarcare="10001"/>', "rejected"),
    ],
)  # a rejection wins over an index
def test_upload_response_refusals(ready, answer, expected):
    L, iid = ready
    result = spv.upload(L, iid, lambda *_: answer, company="NORD")
    assert (result["status"], result["errors"]) == (expected, ["ANAF nu a acceptat trimiterea. Răspunsul este în SPV."])
    assert (
        spv.auto_upload(L, iid, lambda *_: pytest.fail("automatic rejection retry"), company="NORD", today=UPLOAD_DAY)[
            "status"
        ]
        == expected
    )


def test_poll_processing_and_bad_download_preserve_state(ready):
    L, iid = ready
    spv.upload(L, iid, lambda *_: b'<header index_incarcare="10001"/>', company="NORD")
    assert spv.poll(L, lambda *_: b'<header stare="in prelucrare"/>')[0]["status"] == "processing"

    def bad(method, url, body):
        return b'<header stare="ok" id_descarcare="10002"/>' if "/stareMesaj?" in url else archive(b"<wrong/>")

    assert spv.poll(L, bad)[0]["status"] == "error"
    assert spv.submission(L, iid)["status"] == "uploaded"


def test_store_atomic_retry_and_symlink_refusal(ready, home):
    L, iid = ready
    raw = archive(ubl.generate(L, iid, company="NORD"))
    path = spv._store(L, "NORD", "2026-10-31", "10002", raw)
    assert spv._store(L, "NORD", "2026-10-31", "10002", raw) == path
    with pytest.raises(i.InvoiceError):
        spv._store(L, "NORD", "2026-10-31", "10002", b"other")
    target = home / "outside.zip"
    target.write_bytes(b"unchanged")
    (path.parent / "10003.zip").symlink_to(target)
    with pytest.raises(OSError):
        spv._store(L, "NORD", "2026-10-31", "10003", raw)
    assert target.read_bytes() == b"unchanged"
    assert not list(path.parent.glob("*.part"))


@pytest.mark.parametrize(
    "state, expected, errors",
    [
        ("XML cu erori nepreluat de sistem", "rejected", ["ANAF a respins XML-ul înainte de prelucrare."]),
        ("unexpected", "error", ["Starea sau răspunsul SPV nu a putut fi preluat, verificarea se va relua."]),
    ],
)
def test_poll_exact_states(ready, state, expected, errors):
    L, iid = ready
    spv.upload(L, iid, lambda *_: b'<header index_incarcare="10001"/>', company="NORD")
    result = spv.poll(L, lambda *_: f'<header stare="{state}"/>'.encode())[0]
    assert (result["status"], result["errors"]) == (expected, errors)


def test_preflight_failure_leaves_no_claim(ready, monkeypatch):
    L, iid = ready

    def fail(**kwargs):
        raise anaf.AnafError("credentials unavailable")

    monkeypatch.setattr(anaf, "access_token", fail)
    with pytest.raises(anaf.AnafError):
        spv.upload(L, iid, company="NORD")
    assert spv.submission(L, iid) is None


def test_rejected_retry_replaces_checksum(ready, monkeypatch):
    L, iid = ready
    spv.upload(L, iid, lambda *_: b'<header ExecutionStatus="1"/>', company="NORD")
    raw = ubl.generate(L, iid, company="NORD") + b"\n"
    monkeypatch.setattr(ubl, "generate", lambda *_args, **_kwargs: raw)
    spv.upload(L, iid, lambda *_: b'<header index_incarcare="10001"/>', company="NORD")
    assert spv.submission(L, iid)["xml_sha"] == hashlib.sha256(raw).hexdigest()


def test_explicit_recovery_and_company_refusal(ready):
    L, iid = ready
    spv.upload(L, iid, lambda *_: b"<header/>", company="NORD")
    with pytest.raises(i.InvoiceError):
        spv.upload(L, iid, lambda *_: pytest.fail("wrong company"), company="STUDIO9")
    with pytest.raises(i.InvoiceError):
        spv.resolve(L, iid, company="STUDIO9", upload_id="10001", checked_in_spv=True)
    with pytest.raises(i.InvoiceError):
        spv.resolve(L, iid, company="NORD", upload_id=None, checked_in_spv=False)
    L.set_meta("invoice_upload_error:" + iid, "old")
    assert spv.resolve(L, iid, company="NORD", upload_id=None, checked_in_spv=True) is None
    assert L.get_meta("invoice_upload_error:" + iid) is None
    spv.upload(L, iid, lambda *_: b"<header/>", company="NORD")
    L.set_meta("invoice_upload_error:" + iid, "old")
    assert spv.resolve(L, iid, company="NORD", upload_id="10001", checked_in_spv=True)["status"] == "uploaded"
    assert L.get_meta("invoice_upload_error:" + iid) is None
    with pytest.raises(i.InvoiceError):
        spv.resolve(L, iid, company="NORD", upload_id=None, checked_in_spv=True)


@pytest.mark.parametrize("status", ["rejected", "unknown", "uploaded", "uploading", "accepted"])
def test_storno_requires_accepted_spv(ready, status):
    L, iid = ready
    L.db.execute(
        "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,?,?)",
        (iid, "test", status, "fake"),
    )
    L.db.commit()
    if status == "accepted":
        assert i.storno(L, iid, "2026-11-01", "Return", company="NORD")["status"] == "issued"
    else:
        with pytest.raises(i.InvoiceError, match="ANAF să accepte factura"):
            i.storno(L, iid, "2026-11-01", "Return", company="NORD")


def test_sent_recovery_looks_in_the_week_of_the_unknown_attempt(ready, monkeypatch):
    L, iid = ready
    monkeypatch.setattr(anaf, "access_token", lambda **kwargs: pytest.fail("network"))
    with pytest.raises(i.InvoiceError, match="necunoscut"):  # nothing sent yet: refused before any network call
        spv.recover_unknown(L, iid, company="NORD")
    spv.upload(L, iid, lambda *_: b"<header/>", company="NORD")
    monkeypatch.setattr(anaf, "access_token", lambda **kwargs: "fake")
    monkeypatch.setattr(anaf, "download", lambda *_, **kw: archive(ubl.generate(L, iid, company="NORD")))
    windows = []

    def listed(_cui, start, end, _token):
        windows.append((start, end))
        return [dict(id="10002", id_solicitare="10001")]

    assert spv.recover_unknown(L, iid, company="NORD", list_sent=listed)["upload_id"] == "10001"
    claimed = datetime.fromtimestamp(spv.submission(L, iid)["claimed_at"])
    assert windows[0][0] < claimed <= windows[0][1]


@pytest.mark.parametrize("another_attempt", ["", "before listing", "while listing"])
def test_the_app_asks_spv_outside_the_ledger_lock_and_settles_only_the_attempt_it_matched(
    ready, monkeypatch, another_attempt
):
    L, iid = ready
    spv.upload(L, iid, lambda *_: b"<header/>", company="NORD")
    monkeypatch.setattr(anaf, "download", lambda *_, **kw: archive(ubl.generate(L, iid, company="NORD")))

    def retry():  # another person settled the attempt and sent the invoice again
        L.db.execute("UPDATE invoice_submissions SET claimed_at=claimed_at+1 WHERE invoice_id=?", (iid,))
        L.db.commit()

    def configured(_L):  # runs between the read that chose the window and the read that guards the write
        if another_attempt == "before listing":
            retry()

    def listed(*_, **__):
        assert not L.db.in_transaction  # other writers and a token refresh are not blocked while ANAF answers
        assert another_attempt != "before listing"  # the window was chosen for the old attempt: never searched
        if another_attempt:
            retry()
        return [dict(id="10002", id_solicitare="10001")]

    monkeypatch.setattr(anaf, "require_configured", configured)
    monkeypatch.setattr(anaf, "access_token", lambda **kwargs: "fake")

    monkeypatch.setattr(anaf, "list_sent", listed)
    authorized = []
    r = invoicing_api.write_app(
        L,
        "recover-upload",
        "NORD",
        {"id": iid},
        authorize=lambda: authorized.append(L.db.in_transaction),
        require=lambda _: True,
    )
    assert (r["ok"], spv.submission(L, iid)["status"]) == (
        (False, "unknown") if another_attempt else (True, "uploaded")
    )
    assert authorized == ([] if another_attempt == "before listing" else [True])  # inside the write that re-checks


def test_transport_http_exception(ready, monkeypatch):
    monkeypatch.setattr(anaf, "access_token", lambda **kwargs: "fake")

    def fail(*args, **kwargs):
        raise RemoteDisconnected("sensitive response")

    monkeypatch.setattr(anaf.urllib.request, "urlopen", fail)
    with pytest.raises(anaf.AnafError, match="outcome may be unknown"):
        anaf.invoice_request("POST", anaf.API[anaf.env()] + "/upload", b"<x/>")


def test_an_invoice_issued_in_shadow_mode_is_never_uploaded_on_its_own(setup_invoice, home, monkeypatch):
    monkeypatch.setenv("FISCALM_ANAF_ENV", "test")
    L, _ = setup_invoice
    spv.set_auto_upload(L, "NORD", False)
    paused = draft(setup_invoice)
    i.issue(L, paused, "2026-10-31", company="NORD", version=1)
    spv.set_auto_upload(L, "NORD", True)
    iid = draft(setup_invoice)
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    assert not any(i.invoice(L, x, company="NORD")["data"]["issued_live"] for x in (paused, iid))
    assert L.get_meta("invoice_manual_upload:" + paused) == "1"
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    assert invoice_cli.tick(L, UPLOAD_DAY) == {"status": "shadow", "submitted": 0, "polled": 0}
    monkeypatch.setenv("FISCALM_ANAF_ENV", "test")
    (home / "state" / "LIVE").touch()
    for x in (paused, iid):
        assert (
            spv.auto_upload(
                L, x, lambda *_: pytest.fail("shadow invoice auto-uploaded"), company="NORD", today=UPLOAD_DAY
            )["status"]
            == "disabled"
        )
    assert spv.waiting_manual(L, "NORD") == 0  # paused in shadow mode: nothing waits for a manual upload
    assert spv.upload(L, iid, fake_for(L, iid, []), company="NORD")["status"] == "uploaded"


def waiting_manual(L, company):
    return next(r for r in rules.list_rules(L, company) if r["kind"] == "spv_auto_upload")["waiting_manual"]


@pytest.mark.parametrize("recorded", [True, False])  # False: issued before issuance recorded the pause
def test_paused_issuance_never_enters_resumed_backlog(setup_invoice, home, monkeypatch, recorded):
    L, _ = setup_invoice
    monkeypatch.setenv("FISCALM_ANAF_ENV", "test")
    (home / "state" / "LIVE").touch()
    spv.set_auto_upload(L, "NORD", False)
    iid = draft(setup_invoice)
    with monkeypatch.context() as old:
        if not recorded:
            old.setattr(spv, "record_auto_upload_policy", lambda *_: None)
        # No auto_upload call before resuming: even a crash after numbering must be safe.
        i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    assert L.get_meta("invoice_manual_upload:" + iid) == ("1" if recorded else None)
    spv.set_auto_upload(L, "NORD", True)
    calls = []
    transport = fake_for(L, iid, calls)
    with_transport(monkeypatch, transport)
    assert invoice_cli.tick(L, UPLOAD_DAY)["submitted"] == 0
    assert calls == [] and spv.submission(L, iid) is None
    assert (waiting_manual(L, "NORD"), waiting_manual(L, "STUDIO9")) == (1, 0)
    assert spv.upload(L, iid, transport, company="NORD")["status"] == "uploaded"
    assert len(calls) == 1 and spv.waiting_manual(L, "NORD") == 0


def test_storno_issued_while_paused_needs_manual_upload(ready):
    L, iid = ready
    transport = fake_for(L, iid, [])
    spv.upload(L, iid, transport, company="NORD")
    assert spv.poll(L, transport)[0]["status"] == "accepted"
    spv.set_auto_upload(L, "NORD", False)
    reversal = i.storno(L, iid, "2026-10-31", "Corecție", company="NORD")
    assert L.get_meta("invoice_manual_upload:" + reversal["id"]) == "1"
    spv.set_auto_upload(L, "NORD", True)
    assert (
        spv.auto_upload(
            L,
            reversal["id"],
            lambda *_: pytest.fail("unexpected automatic storno upload"),
            company="NORD",
            today=UPLOAD_DAY,
        )["status"]
        == "disabled"
    )


@pytest.mark.parametrize("path", ["prepare", "transport", "upload", "poll", "recover", "get", "refresh"])
def test_disabled_anaf_refuses_outward_calls(ready, monkeypatch, path):
    L, iid = ready
    row = integrations.default(L, "anaf")
    integrations.set_enabled(L, row.id, False)
    monkeypatch.delenv("FISCALM_ANAF_ENV")
    monkeypatch.setattr(anaf, "access_token", lambda *a: pytest.fail("token read"))
    monkeypatch.setattr(anaf, "env", lambda: pytest.fail("disabled environment must not fall back to prod"))
    monkeypatch.setattr(anaf.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("network"))
    calls = {
        "prepare": anaf.prepare_invoice_request,
        "transport": lambda: anaf.invoice_request("POST", anaf.API["prod"] + "/upload", b"x", token="fake"),
        "upload": lambda: spv.upload(L, iid, lambda *a: pytest.fail("upload"), company="NORD"),
        "poll": lambda: spv.poll(L, lambda *a: pytest.fail("poll")),
        "recover": lambda: spv.recover_sent(L, iid, company="NORD", start=None, end=None),
        "get": lambda: anaf._get("descarcare", {"id": "1"}, "fake"),
        "refresh": lambda: anaf.refresh(L),
    }
    with pytest.raises(anaf.AnafError, match="disabled or not configured"):
        calls[path]()


def test_prepare_under_the_upload_write_lock_uses_the_valid_token_and_refreshes_after(ready, monkeypatch):
    L, _ = ready
    anaf_fake.connect(L, access="old", refresh="fake", now=datetime.now() - timedelta(days=80))
    fake = anaf_fake.TokenEndpoint({"access_token": "fresh"})
    monkeypatch.setattr(anaf, "_post", fake)
    with L.tx():  # refreshing would need a write transaction of its own: the still valid token is used
        assert anaf.access_token(L=L) == "old"
        anaf.prepare_invoice_request(L)
    assert not fake.forms and anaf_fake.tokens(L)["access_token"] == "old"
    assert anaf.access_token(L=L) == "fresh" and fake.forms[0]["refresh_token"] == "fake"
    assert anaf_fake.tokens(L)["refresh_token"] == "fake"  # an answer without a new refresh token keeps the old one
    anaf_fake.connect(L, access="old", refresh="fake", now=datetime.now() - timedelta(days=91))
    with L.tx(), pytest.raises(anaf.AuthMissing, match="expired"):
        anaf.access_token(L=L)


# ---- an invoice found already sent in SPV links only when its identity matches


def sent_row(L, iid, **changes):
    inv = i.invoice(L, iid, company="NORD")
    row = (
        dict(
            message_id="98765",
            company="NORD",
            number=i.invoice_number(inv),
            issue_date=DATA["issue_date"],
            currency="RON",
            total="121.00",
            client_cui="99999992",
            client_tax_id="99999984",
            raw_path="/synthetic/not-opened",
            xml_sha=hashlib.sha256(ubl.generate(L, iid, company="NORD")).hexdigest(),
            status="open",
            created_at="2026-10-31",
        )
        | changes
    )
    with L.tx() as db:
        db.execute(f"INSERT INTO issued({','.join(row)}) VALUES({','.join('?' for _ in row)})", list(row.values()))


def test_a_linked_invoice_is_never_uploaded_again(ready, server, monkeypatch, capsys):
    L, iid = ready
    sent_row(L, iid)
    with L.tx() as db:
        db.execute("INSERT INTO invoice_sent_links VALUES(?,?,?)", ("98765", iid, anaf.env()))
    monkeypatch.setattr(anaf, "prepare_invoice_request", lambda *args: pytest.fail("transport prepared"))
    with pytest.raises(i.InvoiceError, match="Există deja o încercare de trimitere"):
        spv.upload(L, iid, lambda *_: pytest.fail("transport called"), company="NORD")
    code, result = _api(server, "invoicing/upload", dict(co="NORD", id=iid))
    assert code == 200 and result["ok"] is False and "Există deja o încercare de trimitere" in result["message"]
    assert cli.main(["invoice", "upload", iid, "--company", "NORD"]) == 2
    assert "Există deja o încercare de trimitere" in capsys.readouterr().out
    assert L.db.execute("SELECT count(*) FROM invoice_submissions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "submitted,change",
    [
        (True, dict(client_tax_id="99999976")),
        (True, dict(currency="EUR")),
        (True, dict(xml_sha="other")),
        (False, dict(total="122.00")),
    ],
)
def test_a_sent_invoice_links_only_with_the_same_identity(ready, submitted, change):
    L, iid = ready
    if submitted:
        with L.tx() as db:
            db.execute(
                "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,?,?)",
                (iid, anaf.env(), "unknown", hashlib.sha256(ubl.generate(L, iid, company="NORD")).hexdigest()),
            )
    sent_row(L, iid, **change)
    i.link_sent(L, "NORD", "98765", "99999984", "TEST-42", DATA["issue_date"])
    assert "98765" not in i.sent_links(L, "NORD")
    assert (spv.submission(L, iid) or {}).get("status") == ("unknown" if submitted else None)


@pytest.mark.parametrize(
    "status,running,live,expected",
    [
        ("uploading", True, True, False),
        ("uploading", False, True, True),
        ("unknown", True, True, True),
        ("unknown", False, False, True),
        ("accepted", False, True, False),
        ("rejected", False, True, False),
    ],
)
def test_the_app_offers_resolve_exactly_when_the_engine_allows_it(setup_invoice, home, status, running, live, expected):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    if live:
        (home / "state").mkdir(exist_ok=True)
        (home / "state" / "LIVE").touch()
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,?,?)",
            (iid, anaf.env(), status, "hash"),
        )
    with spv._uploading() if running else contextlib.nullcontext():
        row = next(r for r in state_api.state(L, "NORD")["issued"] if r["local_id"] == iid)
        result = invoicing_api.act(L, "resolve-upload", "NORD", dict(id=iid, upload_id=None, checked_in_spv=True))
    assert row["can_resolve"] is expected and result["ok"] is expected


def test_a_slow_upload_cannot_be_resolved_as_not_sent_before_it_ends(ready, monkeypatch):
    """ANAF's timeout is per socket operation, so a running upload can outlast any fixed wait."""
    L, iid = ready
    upload, later = fake_for(L, iid, []), spv.time.time() + 3600

    def slow(method, url, body):
        monkeypatch.setattr(spv.time, "time", lambda: later)
        with pytest.raises(i.InvoiceError, match="în curs"):
            spv.resolve(L, iid, company="NORD", upload_id=None, checked_in_spv=True)
        return upload(method, url, body)

    assert spv.upload(L, iid, slow, company="NORD")["upload_id"] == "10001"
    assert not spv.upload_running()


@pytest.mark.parametrize("state", ["ok", "nok"])
def test_shadow_test_acceptance_is_isolated(ready, home, monkeypatch, state):
    L, iid = ready
    (home / "state" / "LIVE").unlink()
    calls = []
    assert invoicing_api.ACTIONS["upload"].allowed_in_mode()
    assert spv.upload(L, iid, fake_for(L, iid, calls), company="NORD")["status"] == "uploaded"
    assert spv.poll(L, fake_for(L, iid, calls, state))[0]["status"] == ("accepted" if state == "ok" else "rejected")
    row = next(r for r in state_api.state(L, "NORD")["issued"] if r.get("local_id") == iid)
    assert row["spv"]["environment"] == "test"
    assert row["owed"] == "121.00" and row["status"] == "open" and not row["rejected"]
    assert not L.db.execute("SELECT 1 FROM settled_tx").fetchone()
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    assert spv.submission(L, iid) is None
    assert not invoicing_api.ACTIONS["upload"].allowed_in_mode()
    assert len(calls) == 3


@pytest.mark.parametrize("path", ["prepare", "transport"])
def test_shadow_prod_transport_never_refreshes_or_calls(ready, home, monkeypatch, path):
    (home / "state" / "LIVE").unlink()
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    monkeypatch.setattr(anaf, "access_token", lambda *a, **kw: pytest.fail("token refresh"))
    monkeypatch.setattr(anaf.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("network"))
    with pytest.raises(anaf.AnafError, match="shadow"):
        if path == "prepare":
            anaf.prepare_invoice_request(ready[0])
        else:
            anaf.invoice_request("POST", anaf.API["prod"] + "/upload", b"<x/>")


def test_test_inboxes_never_import(ready, monkeypatch):
    monkeypatch.setattr(anaf, "access_token", lambda *a, **kw: pytest.fail("TEST inbox"))
    assert issued.sync(ready[0])["skipped_test"]
    assert inbox.sync(ready[0])["skipped_test"]


def test_response_ids_are_isolated_by_environment(ready, monkeypatch):
    L, _ = ready
    test_path = spv._store(L, "NORD", "2026-10-31", "10002", b"test response")
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    prod_path = spv._store(L, "NORD", "2026-10-31", "10002", b"production response")
    assert test_path != prod_path
    assert test_path.read_bytes() == b"test response"
    assert prod_path.read_bytes() == b"production response"


@pytest.mark.parametrize("state", ["early-rejection", "ok", "nok"])
def test_poll_keeps_test_evidence_in_test_when_mode_changes(ready, home, monkeypatch, state):
    L, iid = ready
    xml = ubl.generate(L, iid, company="NORD")
    spv.upload(L, iid, fake_for(L, iid, []), company="NORD")
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,upload_id,xml_sha) VALUES(?,'prod','uploaded','20001',?)",
            (iid, hashlib.sha256(xml).hexdigest()),
        )
    (home / "state/LIVE").unlink()
    monkeypatch.setattr(anaf, "env", lambda: "prod" if (home / "state/LIVE").exists() else "test")
    calls = []

    def transport(method, url, body):
        calls.append(url)
        assert url.startswith(anaf.API["test"])
        if "stareMesaj" in url:
            with closing(Ledger(L.path)) as other:
                assert other.change_mode(True, False, "u_admin", "2026-11-05", "d" * 32)
            return (
                b'<header stare="XML cu erori nepreluat de sistem"/>'
                if state == "early-rejection"
                else f'<header stare="{state}" id_descarcare="10002"/>'.encode()
            )
        return archive(xml if state == "ok" else b'<error cod="BR-S-08"/>')

    result = spv.poll(L, transport)
    rows = {
        r["environment"]: dict(r) for r in L.db.execute("SELECT * FROM invoice_submissions WHERE invoice_id=?", (iid,))
    }
    assert rows["prod"]["status"] == "uploaded" and rows["prod"]["upload_id"] == "20001"
    assert rows["test"]["status"] == ("accepted" if state == "ok" else "rejected")
    assert any(r.get("environment") == "test" for r in result)
    if state != "early-rejection":
        path = Path(rows["test"]["raw_path"])
        assert "/emise/test/" in str(path) and path.read_bytes().startswith(b"PK")


def test_poll_rechecks_production_permission_before_download(ready, home, monkeypatch):
    L, iid = ready
    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,upload_id,xml_sha) VALUES(?,'prod','uploaded','20001','hash')",
            (iid,),
        )
    calls = []

    def transport(method, url, body):
        calls.append(url)
        (home / "state/LIVE").unlink()
        return b'<header stare="ok" id_descarcare="10002"/>'

    assert spv.poll(L, transport)[0]["status"] == "error"
    assert len(calls) == 1
    assert spv.submission(L, iid)["status"] == "uploaded"


def test_late_rejection_never_rejects_a_newer_submission(ready):
    L, iid = ready
    spv.upload(L, iid, fake_for(L, iid, []), company="NORD")

    def late(method, url, body):
        with closing(Ledger(L.path)) as other:
            assert (
                spv.poll(other, lambda *_: b'<header stare="XML cu erori nepreluat de sistem"/>')[0]["status"]
                == "rejected"
            )
            assert (
                spv.upload(other, iid, lambda *_: b'<header index_incarcare="20001"/>', company="NORD")["status"]
                == "uploaded"
            )
        return b'<header stare="XML cu erori nepreluat de sistem"/>'

    assert spv.poll(L, late)[0]["status"] == "uploaded"
    assert spv.submission(L, iid)["upload_id"] == "20001"
    with pytest.raises(i.InvoiceError):
        spv.upload(L, iid, lambda *_: pytest.fail("duplicate submission"), company="NORD")


@pytest.mark.parametrize("live", [False, True])
def test_mode_selects_uploads_but_inbound_reads_stay_in_production(ready, home, monkeypatch, live):
    monkeypatch.delenv("FISCALM_ANAF_ENV")
    if not live:
        (home / "state/LIVE").unlink()
    assert anaf.env() == ("prod" if live else "test")
    assert anaf.inbox_env() == "prod" and anaf.outward_allowed()
    calls = []

    class Answer(io.BytesIO):
        status = 200
        headers = {}

    def get(request, **kwargs):
        calls.append(request.full_url)
        return Answer(b'{"mesaje":[]}' if "listaMesaje" in request.full_url else b"PK\x03\x04synthetic")

    monkeypatch.setattr(anaf.urllib.request, "urlopen", get)
    day = datetime(2026, 11, 5)
    anaf.list_received("99999984", day, day, "synthetic")
    anaf.download("123", "synthetic")
    monkeypatch.setattr(anaf, "access_token", lambda *args, **kwargs: "synthetic")
    send = anaf.prepare_invoice_request(ready[0])
    send("GET", anaf.API[anaf.env()] + "/stareMesaj?id_incarcare=123", None)
    assert all(url.startswith(anaf.API["prod"]) for url in calls[:2])
    assert all(url.startswith(anaf.API["prod" if live else "test"]) for url in calls[2:])


@pytest.mark.parametrize("operation", ["upload", "poll", "recover", "uncovered"])
def test_spv_uses_the_firms_connection_not_the_default(ready, sec, monkeypatch, operation):
    import json

    L, iid = ready
    first = anaf_fake.connect(L, access="wrong-default")
    integrations.update(L, first, {}, firm_scope=["STUDIO9"])
    second = integrations.add(
        L, "anaf", "NORD certificate", {"client_id": "nord-client", "client_secret": "synthetic"}, firm_scope=["NORD"]
    )["id"]
    tokens = anaf._merged({"access_token": "nord-token", "refresh_token": "synthetic"}, None, datetime.now())
    assert integrations.put_oauth(L, second, json.dumps(tokens), client_id="nord-client", replace=True)
    seen = []

    def urlopen(request, **kwargs):
        seen.append(request.get_header("Authorization"))
        body = (
            b'<header stare="in prelucrare"/>'
            if "stareMesaj" in request.full_url
            else b'<header ExecutionStatus="0" index_incarcare="10001"/>'
        )
        return contextlib.closing(io.BytesIO(body))

    monkeypatch.setattr(anaf.urllib.request, "urlopen", urlopen)
    if operation == "uncovered":
        integrations.update(L, second, {}, firm_scope=[])
        with pytest.raises(anaf.AnafError):
            spv.upload(L, iid, company="NORD")
        assert spv.submission(L, iid) is None and not seen
    elif operation == "recover":
        spv.upload(L, iid, lambda *_: b"<header/>", company="NORD")

        def listed(cui, start, end, token):
            seen.append("Bearer " + token)
            return []

        with pytest.raises(i.InvoiceError):
            spv.recover_unknown(L, iid, company="NORD", list_sent=listed)
        assert seen == ["Bearer nord-token"]
    else:
        spv.upload(L, iid, company="NORD")
        if operation == "poll":
            assert spv.poll(L)[0]["status"] == "processing"
        assert seen == ["Bearer nord-token"] * (2 if operation == "poll" else 1)


def test_poll_continues_after_one_firms_authentication_failure(ready, monkeypatch):
    from test_invoicing import PARTY

    L, first = ready
    i.save_seller(L, "STUDIO9", PARTY | {"iban": companies.get("STUDIO9", L).ron_iban})
    client = i.save_client(L, "STUDIO9", PARTY | dict(name="Synthetic buyer", cui="99999992", vat_id="RO99999992"))
    i.create_series(L, "STUDIO9", "OTHER", 1)
    second = i.save_draft(L, "STUDIO9", "OTHER", client, DATA)
    for iid in (first, second):
        with L.tx() as db:
            db.execute(
                "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha,upload_id) VALUES(?,'test','uploaded','synthetic',?)",
                (iid, iid),
            )
    calls = []

    def prepare(ledger, *, environment, company):
        calls.append(company)
        if company == "NORD":
            raise anaf.AuthMissing("synthetic missing connection")
        return lambda *a: b'<header stare="in prelucrare"/>'

    monkeypatch.setattr(anaf, "prepare_invoice_request", prepare)
    assert [r["status"] for r in spv.poll(L)] == ["error", "processing"]
    assert calls == ["NORD", "STUDIO9"]
