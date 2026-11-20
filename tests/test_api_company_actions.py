"""Accountant downloads and explicit sending confirmations, with synthetic documents."""

import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from threading import Barrier

import pytest
import synthetic_world as scenario
from test_api import _req, server as server, world as world
from test_api_v1 import request
from test_transactions import _record, _tx

from fiscalm import audit, companies, links, pack
from fiscalm.api import actions, state as api, tokens
from fiscalm.bank import statements
from fiscalm.ledger import Ledger, now


@pytest.fixture
def documents(ledger, home):
    folder = home / "NORD" / "2026-10"
    folder.mkdir(parents=True)
    for name, kind in [("statement", "statement"), ("receipt", "receipt")]:
        path = folder / (name + ".pdf")
        path.write_bytes(("synthetic " + name).encode())
        ledger.add_document(name, "NORD", "2026-10", kind, str(path))
    return ledger


@pytest.mark.parametrize("live", [False, True])
def test_pack_download_and_confirmation_freeze_only_its_documents(token_owner, documents, server, home, live):
    L = documents
    marker = home / "state" / "LIVE"
    marker.touch() if live else marker.unlink(missing_ok=True)
    assert actions.act("pack-prepare", "NORD", {"month": "2026-10"})["ok"]
    version = L.get_meta("pack_version:NORD:2026-10")
    code, headers, data = _req(server, "GET", f"/web/packfile?co=NORD&month=2026-10&v={version}")
    assert code == 200 and headers["content-type"] == "application/zip"
    assert headers["content-disposition"] == 'attachment; filename="NORD-2026-10.zip"'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["rezumat.csv", "2026-10_cheltuiala_fara-nume.pdf", "2026-10_extras.pdf"]
        assert archive.read("rezumat.csv").decode("utf-8-sig").startswith("Data;Tip;Suma;Moneda;Partener;Detalii;")
    assert hashlib.sha256(data).hexdigest() == version
    reader = tokens.create(L, "Reader", "read", user_id=token_owner)["token"]
    assert request(server, reader, "/companies/NORD/reports/2026-10/pack?version=" + version) == (200, data)
    assert request(server, reader, "/companies/STUDIO9/reports/2026-10/pack?version=" + version)[0] == 404
    assert _req(server, "GET", f"/web/packfile?co=STUDIO9&month=2026-10&v={version}")[0] == 404
    L.add_document("late", "NORD", "2026-10", "receipt", str(home / "NORD" / "late.pdf"))
    assert not pack.mark_sent(L, "NORD", "2026-10", "stale")
    assert not pack.mark_sent(L, "STUDIO9", "2026-10", version)
    with L.tx() as db:  # prepared earlier: sending and its undo keep that time
        db.execute("UPDATE packs SET updated_at='2026-10-31T09:00:00'")
    assert actions.act("pack-sent", "NORD", {"month": "2026-10", "version": version}, company_only=True)["ok"]  # v1
    assert L.document("receipt")["sent_at"] and not L.document("late")["sent_at"]
    assert not pack.mark_sent(L, "NORD", "2026-10", version)


def _month_pack(L, home, month: str) -> str:
    path = home / "NORD" / month / "statement.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic statement " + month.encode())
    L.add_document("statement" + month, "NORD", month, "statement", str(path))
    pack.build(L, "NORD", month)
    return L.get_meta(f"pack_version:NORD:{month}")


def test_undo_reopens_only_what_that_send_alone_covered(documents, home):
    L = documents
    october, november = (
        pack.build(L, "NORD", "2026-10")["version"],
        _month_pack(L, home, "2026-11"),
    )  # both froze the receipt
    shown = api.state(L, "NORD")["reports"]
    mark = next(r for r in shown if r["month"] == "2026-10")["mark"]
    assert not actions.act("pack-sent", "NORD", {"month": "2026-10", "version": october})[
        "ok"
    ]  # the app names its mark
    with L.tx() as db:  # prepared and sent in the same second: still a known preparation time
        db.execute("UPDATE packs SET updated_at=? WHERE month='2026-10'", (now(),))
    prepared_at = L.pack("NORD", "2026-10")["updated_at"]
    assert actions.act("pack-sent", "NORD", {"month": "2026-10", "version": october, "mark": mark})["ok"]
    assert pack.mark_sent(L, "NORD", "2026-11", november)
    # a tab shown before the send can neither send again nor undo; the current mark undoes it
    assert not actions.act("pack-unsent", "NORD", {"month": "2026-10", "version": october, "mark": mark})["ok"]
    assert not actions.act("pack-unsent", "NORD", {"month": "2026-10", "version": october})["ok"]
    sent = pack.mark(L.pack("NORD", "2026-10"))
    assert actions.act("pack-unsent", "NORD", {"month": "2026-10", "version": october, "mark": sent})["ok"]
    assert not actions.act("pack-sent", "NORD", {"month": "2026-10", "version": october, "mark": mark})["ok"]
    reopened = L.pack("NORD", "2026-10")
    assert (reopened["status"], reopened["updated_at"]) == ("prepared", prepared_at)
    # November's ZIP, sent, holds the receipt too: it stays sent
    assert L.document("receipt")["sent_at"] and L.document("statement")["sent_at"]
    assert actions.act(
        "pack-unsent", "NORD", {"month": "2026-11", "version": november, "mark": pack.mark(L.pack("NORD", "2026-11"))}
    )["ok"]
    assert not L.document("receipt")["sent_at"] and not L.document("statement2026-11")["sent_at"]


def test_undo_of_a_pack_sent_before_prep_times_were_kept_shows_no_prep_time(documents):
    L = documents
    version = pack.build(L, "NORD", "2026-10")["version"]
    assert pack.mark_sent(L, "NORD", "2026-10", version)
    with L.tx() as db:  # the earlier writer stamped the send time over the preparation time
        db.execute("UPDATE packs SET note=substr(note, 1, 24), updated_at=substr(note, 6, 19)")  # nor a unique note
    assert pack.unmark_sent(L, "NORD", "2026-10", version, pack.mark(L.pack("NORD", "2026-10")))
    assert next(r for r in api.state(L, "NORD")["reports"] if r["month"] == "2026-10")["prepared_at"] is None


@pytest.mark.parametrize(
    "vendor,date,suffix,name",
    [
        (
            "Benzinăria Exemplu S.R.L.",
            "2026-10-06",
            ".PDF",
            "2026-10-06_cheltuiala_Benzinaria-Exemplu-S-R-L_86.40RON.pdf",
        ),
        ("../../etc/passwd", "../x", ".pdf", "2026-10_cheltuiala_etc-passwd_86.40RON.pdf"),
        ("a/b\\c\x00", "2026-10-06", ".sh", "2026-10-06_cheltuiala_a-b-c_86.40RON.bin"),
        ("", None, ".jpg", "2026-10_cheltuiala_fara-nume_86.40RON.jpg"),
    ],
)
def test_member_names_are_built_from_allowlisted_parts_only(vendor, date, suffix, name):
    doc = {"kind": "receipt", "month": "2026-10", "path": "/x/f" + suffix, "vendor": vendor, "doc_date": date}
    taken: set[str] = set()
    firm = companies.get("NORD")
    assert pack.member_name(doc | {"amount": "-86.4", "currency": "RON"}, firm, taken) == name
    stem, extension = name.rsplit(".", 1)
    loud = doc | {"vendor": vendor.upper(), "amount": "86.40", "currency": "RON"}  # the same name to macOS or Windows
    assert pack.member_name(loud, firm, taken).lower() == f"{stem}_2.{extension}".lower()


def test_two_people_cannot_confirm_the_same_snapshot_twice(documents):
    L = documents
    pack.build(L, "NORD", "2026-10")
    version = L.get_meta("pack_version:NORD:2026-10")
    barrier = Barrier(2)

    def confirm():
        other = Ledger(L.path)
        try:
            barrier.wait(timeout=5)
            return pack.mark_sent(other, "NORD", "2026-10", version)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: confirm(), range(2))) == [False, True]


def test_changed_zip_and_symlink_escape_are_refused(documents, home):
    outside = home / "outside.zip"
    outside.write_bytes(b"outside")
    (home / "NORD" / "2026-10" / "pack-NORD-2026-10.tmp").symlink_to(outside)
    pack.build(documents, "NORD", "2026-10")
    assert outside.read_bytes() == b"outside"
    row = documents.pack("NORD", "2026-10")
    version = documents.get_meta("pack_version:NORD:2026-10")
    path = Path(row["zip_path"])
    path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        pack.file(documents, "NORD", "2026-10", version)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        pack.file(documents, "NORD", "2026-10", version)


def test_the_summary_lists_every_transaction_of_the_month_and_what_explains_it(ledger, world):
    rows = api.pack_summary(ledger, "NORD", "2026-09")
    assert rows[0] == ["Data", "Tip", "Suma", "Moneda", "Partener", "Detalii", "Ce o explică"]
    assert len(rows) == 1 + sum(
        t["date"].startswith("2026-09") for t in api.state(ledger, "NORD")["transactions"]["rows"]
    )
    explained = {r[0]: r[6] for r in rows[1:]}
    assert explained["2026-09-09"] == "Factura CS-0920 de la Chirie Spatiu SRL"
    assert explained["2026-09-15"] == "Factura emisă NRD 0040 către Client Delta SRL"
    assert (explained["2026-09-05"], explained["2026-09-04"]) == ("Lipsește documentul", "Fără explicație")
    # a month past Tranzacții's window is summarised all the same
    assert api.pack_summary(ledger, "NORD", "2026-09", now=datetime(2028, 1, 1)) == rows
    # the bank's and the user's text never runs as a spreadsheet formula, and never shows a whole account number
    data = pack.summary_csv([["=HYPERLINK(1)", "-1", "+a", "@b", "\n=x", "IBAN RO49AAAA1B31007593840000"]])
    assert data.decode("utf-8-sig") == "'=HYPERLINK(1);'-1;'+a;'@b;'=x;IBAN ••••0000\r\n"


def test_the_summary_and_the_documents_are_frozen_from_one_ledger_state(documents):
    calls = []

    def summary():  # another process writes between the first read and the freeze: read again
        calls.append(1)
        if len(calls) == 1:
            other = Ledger(documents.path)
            other.set_meta("unrelated", "x")
            other.close()
        return [["Data"], [str(len(calls))]]

    pack.build(documents, "NORD", "2026-10", summary)
    version = documents.get_meta("pack_version:NORD:2026-10")
    with zipfile.ZipFile(io.BytesIO(pack.file(documents, "NORD", "2026-10", version))) as archive:
        assert archive.read("rezumat.csv").decode("utf-8-sig") == "Data\r\n2\r\n"


def test_reports_pack_warning_and_transactions_share_explanations(ledger, home, monkeypatch):
    txs = [_tx(41), _tx(42), _tx(43), _tx(44, "credit")]
    st = statements.Statement("NORD", None, None, [statements.Account("x", "RON", txs)])
    transfer, fee, awaited, credit = _record(ledger, "NORD", *txs)
    path = home / "NORD" / "statement.pdf"
    path.parent.mkdir()
    path.write_bytes(b"synthetic statement")
    ledger.add_document("statement", "NORD", "2026-10", "statement", str(path))
    monkeypatch.setattr(api, "_statement", lambda *a: st)
    links.link(ledger, "NORD", transfer, kind="transfer")
    links.link(ledger, "NORD", fee, kind="label", reason="bank_fee")
    assert links.await_invoice(ledger, "NORD", awaited, "Factura urmează")
    scenario._issued(ledger, "7001", "NORD", "TEST 1", "Client Test", "RO7001", date(2026, 10, 1), None, "44.00")
    assert ledger.mark_issued_collected("7001", "statement", credit)
    s = api.state(ledger, "NORD", datetime(2026, 11, 11))
    report = next(r for r in s["reports"] if r["month"] == "2026-10")
    bank = report["bank"]
    missing = [t for t in bank["card"] if not t["doc"]]
    # Home's pack warning and the report list use these same counts.
    assert len(bank["card"]) == 4
    assert sum(bool(t["doc"]) for t in bank["card"]) == 3
    assert len(missing) == s["transactions"]["to_explain"] == 1
    assert missing[0]["awaiting"]
    assert ledger.awaited("NORD")[awaited]
    assert awaited not in ledger.used_txs()
    summary = api.pack_summary(ledger, "NORD", "2026-10", datetime(2026, 11, 11))
    assert sum(r[-1] == "Lipsește documentul" for r in summary[1:]) == 1
    pack.build(ledger, "NORD", "2026-10", lambda: summary)
    version = ledger.get_meta("pack_version:NORD:2026-10")
    frozen = pack.file(ledger, "NORD", "2026-10", version)
    assert pack.mark_sent(ledger, "NORD", "2026-10", version)
    links.link(ledger, "NORD", awaited, kind="label", reason="other", note="Explicație")
    assert pack.file(ledger, "NORD", "2026-10", version) == frozen


def test_report_keeps_currency_and_direction_of_unexplained_credits(ledger, home, monkeypatch):
    euro, refund = _tx(45, "credit"), _tx(46, "credit", card_last4="0001")
    euro.currency = "EUR"
    _record(ledger, "NORD", euro, refund)
    path = home / "statement.pdf"
    path.write_bytes(b"synthetic statement")
    ledger.add_document("s", "NORD", "2026-10", "statement", str(path))
    st = statements.Statement("NORD", None, None, [])
    monkeypatch.setattr(api, "_statement", lambda *a: st)
    rows = api._bank(ledger, "NORD", "2026-10")["card"]
    assert [(r["currency"], r["direction"], r["kind"], r["doc"]) for r in rows] == [
        ("RON", "credit", "card", None),
        ("EUR", "credit", "transfer", None),
    ]


def test_a_changed_month_is_prepared_again_as_a_new_version_and_a_sent_one_never(ledger, home, monkeypatch):
    fee = _record(ledger, "NORD", _tx(47))[0]
    path = home / "NORD" / "statement.pdf"
    path.parent.mkdir()
    path.write_bytes(b"synthetic statement")
    ledger.add_document("statement", "NORD", "2026-10", "statement", str(path))
    monkeypatch.setattr(api, "_statement", lambda *a: statements.Statement("NORD", None, None, []))
    when = datetime(2026, 11, 11)

    def prepare() -> str:
        pack.build(ledger, "NORD", "2026-10", lambda: api.pack_summary(ledger, "NORD", "2026-10", when))
        return ledger.get_meta("pack_version:NORD:2026-10")

    def report() -> dict:
        return next(r for r in api.state(ledger, "NORD", when)["reports"] if r["month"] == "2026-10")

    first = prepare()
    assert (report()["status"], report()["stale"], bool(report()["prepared_at"])) == ("prepared", False, True)
    assert prepare() == first  # nothing changed: the same version, no new file
    frozen = pack.file(ledger, "NORD", "2026-10", first)
    links.link(ledger, "NORD", fee, kind="label", reason="bank_fee")
    assert report()["stale"]
    second = prepare()
    assert second != first and not report()["stale"]
    with zipfile.ZipFile(io.BytesIO(pack.file(ledger, "NORD", "2026-10", second))) as archive:
        assert "Comision bancar" in archive.read("rezumat.csv").decode("utf-8-sig")
    kept = [hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.glob("2026-10/pack-*.zip")]
    assert sorted(kept) == sorted([first, second]) and hashlib.sha256(frozen).hexdigest() == first
    with pytest.raises(ValueError):  # the old version's link no longer serves anything, never other bytes
        pack.file(ledger, "NORD", "2026-10", first)
    assert not pack.mark_sent(ledger, "NORD", "2026-10", first)
    assert pack.mark_sent(ledger, "NORD", "2026-10", second)
    ledger.add_document("late", "NORD", "2026-10", "receipt", str(path))  # the sent month changes: it stays as sent
    assert (report()["status"], report()["stale"]) == ("sent", False)
    assert prepare() == second


def test_document_amount_correction_is_local_and_audited(documents):
    token = audit.actor.set("user synthetic-admin")
    body = {"id": "receipt", "amount": "125.40", "currency": "PLN", "date": "2026-10-15", "number": "TEST-42"}
    try:
        assert actions.act("document-correct", "NORD", body)["ok"]
        body["amount"] = "140.00"
        body["date"] = "2026-11-01"
        assert actions.act("document-correct", "NORD", body)["ok"]
    finally:
        audit.actor.reset(token)
    row = documents.document("receipt")
    assert (row["amount"], row["currency"], row["doc_date"]) == ("140.00", "PLN", "2026-11-01")
    assert row["month"] == "2026-11"
    history = json.loads(documents.get_meta("document-corrections:receipt"))
    assert history[0]["previous"]["amount"] is None
    assert history[1]["previous"]["amount"] == "125.40"
    assert history[1]["by"] == "user synthetic-admin" and history[1]["at"]
    assert not documents.db.execute("SELECT 1 FROM settled_tx").fetchone()


@pytest.mark.parametrize("guard", ["sent", "proof", "upload", "other-company"])
def test_document_correction_refuses_frozen_evidence(documents, guard):
    if guard == "sent":
        documents.mark_documents_sent(["receipt"], "2026-10-28")
    elif guard == "proof":
        with documents.tx() as db:
            db.execute("INSERT INTO settled_tx VALUES('bon:receipt','efactura synthetic',?)", (now(),))
    elif guard == "upload":
        with documents.tx() as db:
            db.execute("UPDATE documents SET source='upload:synthetic' WHERE sha='receipt'")
    body = {"id": "receipt", "amount": "100.00", "currency": "EUR"}
    assert not actions.act("document-correct", "STUDIO9" if guard == "other-company" else "NORD", body)["ok"]
    assert documents.document("receipt")["amount"] is None
    assert documents.get_meta("document-corrections:receipt") is None


@pytest.mark.parametrize("amount,currency", [("NaN", "EUR"), ("1.001", "EUR"), ("1e2", "EUR"), ("10.00", "XYZ")])
def test_document_correction_validates_money(documents, amount, currency):
    assert not actions.act("document-correct", "NORD", {"id": "receipt", "amount": amount, "currency": currency})["ok"]


def test_concurrent_document_corrections_keep_both_answers(documents):
    barrier = Barrier(2)

    def correct(amount):
        other = Ledger(documents.path)
        try:
            barrier.wait(timeout=5)
            other.correct_document("NORD", "receipt", amount, "PLN", "", "", "synthetic-user")
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(correct, ["10.00", "20.00"]))
    history = json.loads(documents.get_meta("document-corrections:receipt"))
    assert len(history) == 2
    assert history[1]["previous"] == history[0]["value"]
    assert documents.document("receipt")["amount"] == history[1]["value"]["amount"]


def test_document_correction_invalidates_prepared_pack(documents):
    first = pack.build(documents, "NORD", "2026-10")
    version = documents.get_meta("pack_version:NORD:2026-10")
    assert first and not pack.stale(documents, "NORD", "2026-10", [])
    documents.correct_document("NORD", "receipt", "999.00", "EUR", "2026-10-03", "", "synthetic-user")
    assert pack.stale(documents, "NORD", "2026-10", [])
    pack.build(documents, "NORD", "2026-10")
    assert documents.get_meta("pack_version:NORD:2026-10") != version
    assert not pack.stale(documents, "NORD", "2026-10", [])


def test_document_arriving_during_metadata_read_waits_for_edit_permission(documents, monkeypatch):
    read = api._document_metadata

    def arrived(L, co):
        metadata = read(L, co)
        writer = Ledger(documents.path)
        try:
            writer.add_document("late", co, "2026-10", "receipt", "/fake/late.pdf")
        finally:
            writer.close()
        return metadata

    monkeypatch.setattr(api, "_document_metadata", arrived)
    row = next(d for d in api._documents(documents, "NORD") if d["id"] == "late")
    assert row["amount"] is None and row["can_correct"] is False
    monkeypatch.setattr(api, "_document_metadata", read)
    assert next(d for d in api._documents(documents, "NORD") if d["id"] == "late")["can_correct"]
