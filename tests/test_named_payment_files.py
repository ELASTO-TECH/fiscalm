"""Named payment files use only synthetic money and disposable installs."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from payment_cases import D0, _op
from test_api import _req, server as server, world as world
from test_payfiles import ing_has_a_format as ing_has_a_format

from fiscalm import cli, companies, ledger as ledger_module
from fiscalm.api import actions, state
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.ledger import Ledger
from fiscalm.payments import engine as pay
from fiscalm.payments.orders import PaymentFileError


def test_named_file_download_changes_import_and_recovery(ledger, live, monkeypatch):
    first = ledger.create_payment_file("NORD", "First")
    second = ledger.create_payment_file("NORD", "Second")
    one = _op(ledger, 1)
    assert ledger.attach_payment_file(first, "NORD", one)
    downloaded = pay.download_file(ledger, first, "NORD", D0, lambda: None)
    two = _op(ledger, 2)
    assert ledger.attach_payment_file(first, "NORD", two)
    assert pay.named_files(ledger, "NORD")[1]["changed_since_download"]
    with pytest.raises(PaymentFileError):
        pay.import_file(ledger, first, "NORD", downloaded["download_id"], D0)
    downloaded = pay.download_file(ledger, first, "NORD", D0, lambda: None)
    assert pay.import_file(ledger, first, "NORD", downloaded["download_id"], D0)
    assert ledger.payment_file(first)["imported_at"]
    assert not ledger.attach_payment_file(first, "NORD", _op(ledger, 3))
    assert ledger.delete_payment_file(second, "NORD")
    detail = state.payment_file_detail(ledger, first)
    assert detail["status"] == "imported" and not detail["can_add"]
    assert len(detail["payments"]) == 2
    assert all(not p["proved"] for p in detail["payments"])


def test_missing_named_file_never_recreated_automatically(ledger, live):
    ident = ledger.create_payment_file("NORD", "Missing")
    assert ledger.attach_payment_file(ident, "NORD", _op(ledger, 4))
    pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    path = pay.named_path(ledger.payment_file(ident))
    path.unlink()
    assert pay.download_file(ledger, ident, "NORD", D0, lambda: None)["question"] == "missing"
    assert not path.exists()


def test_named_file_api_local_edits_in_sandbox_and_download_refusal(server):
    def post(body):
        code, _, raw = _req(
            server, "POST", "/web/api/payment-files", json.dumps(body), {"Content-Type": "application/json"}
        )
        assert code == 200
        return json.loads(raw)

    result = post({"action": "payment_file_create", "co": "NORD", "name": "Suppliers", "invoice_ids": []})
    assert result["ok"]
    ident = result["done"]["file_id"]
    assert post({"action": "payment_file_rename", "file_id": ident, "name": "New name"})["ok"]
    assert not post({"action": "payment_file_download", "file_id": ident})["ok"]
    assert actions.ACTIONS["payment_file_download"].effect == "payment_file"
    code, _, raw = _req(server, "GET", f"/web/api/payment-files/{ident}")
    assert code == 200 and json.loads(raw)["name"] == "New name"
    assert post({"action": "payment_file_delete", "file_id": ident})["ok"]


def test_import_journal_survives_failure_before_archive_move(ledger, live, monkeypatch):
    ident = ledger.create_payment_file("NORD", "Crash")
    assert ledger.attach_payment_file(ident, "NORD", _op(ledger, 7))
    downloaded = pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    finish = pay._finish_named_import
    monkeypatch.setattr(pay, "_finish_named_import", lambda *args: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        pay.import_file(ledger, ident, "NORD", downloaded["download_id"], D0)
    assert ledger.payment_file(ident)["imported_at"]
    assert ledger.get_meta(f"payment_import:{ident}")
    monkeypatch.setattr(pay, "_finish_named_import", finish)
    pay._recover_named(ledger, D0)
    assert not ledger.get_meta(f"payment_import:{ident}")
    assert pay.named_path(ledger.payment_file(ident)).exists()
    assert not state.payment_file_detail(ledger, ident)["can_add"]


def test_missing_file_answer_is_version_bound(ledger, live):
    ident = ledger.create_payment_file("NORD", "Missing")
    item = _op(ledger, 8)
    assert ledger.attach_payment_file(ident, "NORD", item)
    pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    path = pay.named_path(ledger.payment_file(ident))
    path.unlink()
    with pytest.raises(PaymentFileError):
        pay.resolve_missing_file(ledger, ident, "NORD", "stale", D0, False)
    assert not path.exists()
    assert pay.resolve_missing_file(ledger, ident, "NORD", pay.version([item]), D0, False)
    assert path.exists()


def test_named_selection_approval_is_atomic_and_refuses_foreign_invoice(ledger, world, home):
    (home / "state" / "LIVE").unlink()
    before = len(pay.named_files(ledger, "NORD"))
    with pytest.raises(PaymentFileError):
        pay.add_invoices(ledger, "NORD", ["101", "not-found"], name="Atomic", approve_invoice=efactura.approve)
    assert ledger.efactura("101")["status"] == "open"
    assert len(pay.named_files(ledger, "NORD")) == before
    ident = pay.add_invoices(ledger, "NORD", ["101"], name="Chosen", approve_invoice=efactura.approve)
    assert ledger.payment(ledger.efactura("101")["item_id"])["file_id"] == ident
    assert state.payment_file_detail(ledger, ident)["payments"][0]["ef_id"] == "101"


def test_download_rechecks_payment_after_committed_intent(ledger, live, monkeypatch):
    ident = ledger.create_payment_file("NORD", "Race")
    item = _op(ledger, 9)
    assert ledger.attach_payment_file(ident, "NORD", item)
    assert ledger.attach_payment_file(ident, "NORD", _op(ledger, 10))
    prepare = pay._prepare_download

    def settle_after_intent(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        with closing(Ledger(ledger.path)) as other:
            assert other.mark_paid(item, "user: already paid")
        return prepared

    monkeypatch.setattr(pay, "_prepare_download", settle_after_intent)
    with pytest.raises(PaymentFileError):
        pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert not pay.file_downloads(ledger, ident)
    assert not pay.named_path(ledger.payment_file(ident)).exists()
    monkeypatch.setattr(pay, "_prepare_download", prepare)
    assert "download_id" in pay.download_file(ledger, ident, "NORD", D0, lambda: None)


def test_two_connections_add_same_invoice_to_only_one_named_file(ledger, world):
    ids = [ledger.create_payment_file("NORD", name) for name in ("First", "Second")]
    barrier = Barrier(2)

    def add(file_id):
        with closing(Ledger(ledger.path)) as other:
            barrier.wait(timeout=5)
            try:
                pay.add_invoices(other, "NORD", ["101"], file_id=file_id, approve_invoice=efactura.approve)
            except efactura.ApproveError:
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(add, ids)) == [False, True]
    assert sum(len(pay.file_rows(ledger, ident)) for ident in ids) == 1


@pytest.mark.parametrize("after_download", [True, False])
def test_import_with_payment_proved_before_confirmation(ledger, live, after_download):
    ident = ledger.create_payment_file("NORD", "Proof")
    first, second = _op(ledger, 11), _op(ledger, 12)
    for item in (first, second):
        assert ledger.attach_payment_file(ident, "NORD", item)
    if after_download:
        snapshot = pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    else:
        pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    debit = statements.Tx(
        date=D0,
        title="Synthetic debit",
        description="CAS",
        direction="debit",
        amount=Decimal("100.00"),
        currency="RON",
        iban=ledger.payment(first)["source_iban"],
        ref="proof",
    )
    ledger.record_statement(statements.Statement("NORD", None, None, [statements.Account("x", "RON", [debit])]))
    assert ledger.mark_paid(first, "statement", statements.tx_key(debit))
    if not after_download:
        snapshot = pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert not pay.named_files(ledger, "NORD")[0]["changed_since_download"]
    assert pay.import_file(ledger, ident, "NORD", snapshot["download_id"], D0)
    assert (
        next(p for p in state.payment_file_detail(ledger, ident)["payments"] if p["id"] == state.payment_id(first))[
            "state"
        ]
        == "paid"
    )
    assert not ledger.get_meta(f"payment_import:{ident}")


def test_deleted_file_id_is_never_reused(ledger):
    first = ledger.create_payment_file("NORD", "First")
    assert pay.delete_file(ledger, first, "NORD")
    second = ledger.create_payment_file("NORD", "Second")
    assert second > first
    assert state.payment_file_detail(ledger, first) is None


def test_missing_import_refuses_members_added_after_last_download(ledger, live):
    ident = ledger.create_payment_file("NORD", "Missing")
    first, second = _op(ledger, 15), _op(ledger, 16)
    assert ledger.attach_payment_file(ident, "NORD", first)
    pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert ledger.attach_payment_file(ident, "NORD", second)
    pay.named_path(ledger.payment_file(ident)).unlink()
    with pytest.raises(PaymentFileError):
        pay.resolve_missing_file(ledger, ident, "NORD", pay.version([first, second]), D0, True)
    assert ledger.payment_file(ident)["imported_at"] is None


def test_download_keeps_named_file_payments_in_outstanding_totals(ledger, live):
    ident = ledger.create_payment_file("NORD", "Debt")
    assert ledger.attach_payment_file(ident, "NORD", _op(ledger, 17))
    before = state.state(ledger, "NORD")["payable"]["total"]
    pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert state.state(ledger, "NORD")["payable"]["total"] == before == "100.00"


def test_default_names_are_distinct_and_duplicate_invoice_refusal_is_romanian(ledger, world):
    name = pay.default_name(ledger, "NORD", D0)
    ident = pay.add_invoices(ledger, "NORD", ["101"], name=name, approve_invoice=efactura.approve)
    assert pay.default_name(ledger, "NORD", D0) == name + " (2)"
    result = actions.act("payment_file_create", "NORD", {"name": "Another", "invoice_ids": ["101"]})
    assert not result["ok"] and "fișier" in result["message"] and "not an open" not in result["message"]
    assert len(pay.file_rows(ledger, ident)) == 1


def test_reopened_payment_invalidates_the_download(ledger, live):
    ident = ledger.create_payment_file("NORD", "Reopen")
    first, second = _op(ledger, 21), _op(ledger, 22)
    for item in (first, second):
        assert ledger.attach_payment_file(ident, "NORD", item)
    assert ledger.mark_skipped(second, "user: paid another way")
    snapshot = pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert ledger.requeue_file_payment(second, lambda fid: pay.editable_file(ledger, fid, "NORD"))
    with pytest.raises(PaymentFileError):
        pay.import_file(ledger, ident, "NORD", snapshot["download_id"], D0)
    assert ledger.payment_file(ident)["imported_at"] is None
    assert ledger.payment(second)["status"] == "pending"
    # The Ledger guard independently refuses active rows outside a forged snapshot.
    file = ledger.payment_file(ident)
    assert not ledger.close_payment_file(ident, "NORD", file["revision"], json.dumps({"ids": [first]}))


def test_two_connections_import_only_once(ledger, live):
    ident = ledger.create_payment_file("NORD", "Import race")
    assert ledger.attach_payment_file(ident, "NORD", _op(ledger, 23))
    snapshot = pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    barrier = Barrier(2)

    def mark(_):
        with closing(Ledger(ledger.path)) as other:
            barrier.wait(timeout=5)
            try:
                return pay.import_file(other, ident, "NORD", snapshot["download_id"], D0)
            except PaymentFileError:
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(mark, range(2))) == [False, True]
    assert not ledger.get_meta(f"payment_import:{ident}")


def test_a_file_downloaded_twice_stays_editable_and_imports_its_latest_download(ledger, world, monkeypatch):
    ident = pay.add_invoices(ledger, "NORD", ["101"], name="Twice", approve_invoice=efactura.approve)
    monkeypatch.setattr(ledger_module, "now", lambda: "2099-01-01T10:00:00")  # every download in the same second
    first = pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    with closing(Ledger(ledger.path)) as other:
        latest = pay.download_file(other, ident, "NORD", D0, lambda: None)  # the same bytes again: a retry
    detail = state.payment_file_detail(ledger, ident)
    assert detail["question"] is None and detail["questions"] == [] and detail["can_add"] and detail["can_import"]
    assert [d["id"] for d in detail["downloads"]] == [latest["download_id"], first["download_id"]]
    assert state.payment_file_defaults(ledger, "NORD", D0, pay.named_files(ledger, "NORD"))[0]["open_file_id"] == ident
    pay.add_invoices(ledger, "NORD", ["102"], file_id=ident, approve_invoice=efactura.approve)
    assert state.payment_file_detail(ledger, ident)["changed_since_download"]
    with pytest.raises(PaymentFileError):  # the edit came after both downloads: import needs a new one
        pay.import_file(ledger, ident, "NORD", latest["download_id"], D0)
    a = pay.download_file(ledger, ident, "NORD", D0, lambda: None, value_date=D0)
    pay.download_file(ledger, ident, "NORD", D0, lambda: None, value_date=D0 + timedelta(days=1))
    again = pay.download_file(ledger, ident, "NORD", D0, lambda: None, value_date=D0)  # A, B, then A again
    assert state.payment_file_detail(ledger, ident)["downloads"][0]["id"] == again["download_id"] != a["download_id"]
    with pytest.raises(PaymentFileError):
        pay.import_file(ledger, ident, "NORD", latest["download_id"], D0)
    assert pay.import_file(ledger, ident, "NORD", again["download_id"], D0)
    assert Path(ledger.payment_file(ident)["path"]).read_bytes() == again["data"]


def test_a_download_recorded_before_51_that_reused_its_row_stays_the_latest(ledger, world):
    """Before #51 a download of the same bytes refreshed its first row's time: A (lower id, newer time) is latest."""
    ident = pay.add_invoices(ledger, "NORD", ["101"], name="Legacy", approve_invoice=efactura.approve)
    a = pay.download_file(ledger, ident, "NORD", D0, lambda: None, value_date=D0)
    b = pay.download_file(ledger, ident, "NORD", D0, lambda: None, value_date=D0 + timedelta(days=1))
    ledger.db.execute("UPDATE payment_downloads SET created_at='2099-01-01T10:00:00' WHERE id=?", (a["download_id"],))
    ledger.db.commit()
    assert state.payment_file_detail(ledger, ident)["downloads"][0]["id"] == a["download_id"]
    with pytest.raises(PaymentFileError):
        pay.import_file(ledger, ident, "NORD", b["download_id"], D0)
    assert pay.import_file(ledger, ident, "NORD", a["download_id"], D0)
    assert Path(ledger.payment_file(ident)["path"]).read_bytes() == a["data"]


def test_all_skipped_is_never_confirmed_and_can_be_emptied(ledger, live):
    ident = ledger.create_payment_file("NORD", "Skipped")
    item = _op(ledger, 24)
    assert ledger.attach_payment_file(ident, "NORD", item)
    pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert ledger.mark_skipped(item, "user: paid another way")
    summary = pay.named_files(ledger, "NORD")[0]
    assert summary["status"] == "open" and summary["confirmed_count"] == 0
    assert ledger.remove_file_payment(ident, "NORD", item)
    assert ledger.payment(item)["status"] == "skipped" and ledger.payment(item)["file_id"] is None
    assert pay.delete_file(ledger, ident, "NORD")


def test_invoice_undo_mark_requeues_into_a_file_downloaded_twice_and_flags_the_change(ledger, world):
    ident = pay.add_invoices(ledger, "NORD", ["101", "102"], name="Undo", approve_invoice=efactura.approve)
    assert efactura.withdraw(ledger, "102", "user: already paid", ("bank", "")) is None
    pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    pay.download_file(ledger, ident, "NORD", D0 + timedelta(days=1), lambda: None)
    assert efactura.reopen(ledger, "102") == "queued"
    assert ledger.payment(ledger.efactura("102")["item_id"])["status"] == "pending"
    assert state.payment_file_detail(ledger, ident)["changed_since_download"]


def test_missing_file_with_only_skipped_payments_can_be_resolved_without_bank_bytes(ledger, live):
    ident = ledger.create_payment_file("NORD", "Skipped missing")
    item = _op(ledger, 25)
    assert ledger.attach_payment_file(ident, "NORD", item)
    pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert ledger.mark_skipped(item, "user: paid another way")
    pay.named_path(ledger.payment_file(ident)).unlink()
    assert pay.clear_missing_inactive_file(ledger, ident, "NORD", pay.version([]))
    detail = state.payment_file_detail(ledger, ident)
    assert detail["status"] == "open" and detail["question"] is None and detail["can_remove"]
    assert ledger.remove_file_payment(ident, "NORD", item)
    assert pay.delete_file(ledger, ident, "NORD")


def test_named_file_keeps_its_bank_format_extension(ledger, live, ing_has_a_format):
    ident = ledger.create_payment_file("NORD", "Other bank")
    assert ledger.attach_payment_file(ident, "NORD", _op(ledger, 26))
    snapshot = pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    assert snapshot["name"].endswith(".xml") and snapshot["data"].startswith(b"FAKE\n")
    assert pay.import_file(ledger, ident, "NORD", snapshot["download_id"], D0)
    assert pay.named_path(ledger.payment_file(ident)).suffix == ".xml"


def test_manual_removal_is_not_undone_by_automatic_supplier_rule(ledger, world):
    efactura.set_autopay(ledger, "101", True)
    ident = pay.add_invoices(ledger, "NORD", ["101"], name="Manual", approve_invoice=efactura.approve)
    item = ledger.efactura("101")["item_id"]
    assert ledger.remove_file_payment(ident, "NORD", item)
    efactura.autopay_new(ledger)
    assert ledger.efactura("101")["status"] == "open"
    pay.add_invoices(ledger, "NORD", ["101"], file_id=ident, approve_invoice=efactura.approve)
    assert ledger.efactura("101")["status"] == "linked"


def test_automatic_rule_never_changes_a_download_awaiting_bank_confirmation(ledger, world):
    ident = pay.add_invoices(ledger, "NORD", ["102"], name="Downloaded", approve_invoice=efactura.approve)
    snapshot = pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    revision = ledger.payment_file(ident)["revision"]
    members = [r["item_id"] for r in pay.file_rows(ledger, ident)]
    efactura.set_autopay(ledger, "101", True)
    efactura.autopay_new(ledger)
    assert ledger.payment_file(ident)["revision"] == revision
    assert [r["item_id"] for r in pay.file_rows(ledger, ident)] == members
    assert ledger.payment(ledger.efactura("101")["item_id"])["file_id"] != ident
    assert pay.import_file(ledger, ident, "NORD", snapshot["download_id"], D0)


def test_preapproved_unfiled_invoice_can_be_put_in_a_named_file(ledger, world):
    item = efactura.approve(ledger, "101", origin="auto")["item_id"]
    assert ledger.payment(item)["file_id"] is None
    invoice = next(r for r in state.state(ledger, "NORD")["invoices"] if r["id"] == "101")
    assert invoice["can_approve"] and invoice["payment_file"] is None
    ident = pay.add_invoices(ledger, "NORD", ["101"], name="Previously approved", approve_invoice=efactura.approve)
    row = ledger.payment(item)
    assert row["file_id"] == ident and row["origin"] == "person" and row["status"] == "pending"
    invoice = next(r for r in state.state(ledger, "NORD")["invoices"] if r["id"] == "101")
    assert not invoice["can_approve"] and invoice["payment_file"]["id"] == ident
    with pytest.raises(efactura.ApproveError):
        pay.add_invoices(ledger, "NORD", ["101"], name="Duplicate", approve_invoice=efactura.approve)


def test_cli_approval_uses_an_explicit_named_file_locally(ledger, world, home, capsys):
    (home / "state" / "LIVE").unlink()
    assert cli.main(["efactura", "approve", "101", "102", "--new-file", "CLI chosen"]) == 0
    files = {ledger.payment(ledger.efactura(mid)["item_id"])["file_id"] for mid in ("101", "102")}
    assert len(files) == 1
    file = ledger.payment_file(files.pop())
    assert file["name"] == "CLI chosen" and file["path"] is None
    capsys.readouterr()
    assert cli.main(["pay", "build", str(file["id"])]) == 1
    capsys.readouterr()
    (home / "state" / "LIVE").touch()
    assert cli.main(["pay", "build", str(file["id"]), "--value-date", "26/10/2026"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["file_id"] == file["id"] and result["download_id"]
    assert pay.file_downloads(ledger, file["id"])[0]["value_date"] == D0.isoformat()
    assert result["file"] == str(pay.named_path(ledger.payment_file(file["id"])))


def test_cli_value_date_cannot_bypass_the_current_statement_hold(ledger, live, monkeypatch):
    class November(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 18)

    monkeypatch.setattr(cli, "date", November)
    item = _op(ledger, 91)
    ident = ledger.create_payment_file("NORD", "Current statement required")
    assert ledger.attach_payment_file(ident, "NORD", item)
    ledger.add_document(
        "september",
        "NORD",
        "2026-09",
        "statement",
        "/synthetic/september.xml",
        accounts=[companies.get("NORD").ron_iban],
    )
    assert cli.main(["pay", "build", str(ident), "--value-date", "26/10/2026"]) == 1
    assert not pay.named_path(ledger.payment_file(ident)).exists()
    assert ledger.payment(item)["status"] == "pending"


def test_shadow_move_journal_finishes_after_switching_back_to_live(ledger, live, home, monkeypatch):
    item = _op(ledger, 92)
    ident = ledger.create_payment_file("NORD", "Migrated file")
    assert ledger.attach_payment_file(ident, "NORD", item)
    pay.download_file(ledger, ident, "NORD", D0, lambda: None)
    source = pay.to_pay_dir() / "payments-NORD.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    pay.named_path(ledger.payment_file(ident)).replace(source)
    other = source.with_name("payments-STUDIO9.csv")
    other.write_bytes(b"unrelated legacy file")
    with ledger.tx() as db:
        db.execute("UPDATE payment_files SET path=? WHERE id=?", (str(source), ident))
        db.execute("UPDATE payments SET csv_path=? WHERE item_id=?", (str(source), item))
    original = Path.replace

    def interrupted(path, destination):
        if path == source:
            raise OSError("crash after journal")
        return original(path, destination)

    monkeypatch.setattr(Path, "replace", interrupted)
    (home / "state" / "LIVE").unlink()
    with pytest.raises(OSError):
        pay.refresh_to_pay(ledger, D0)
    assert ledger.get_meta("payment_shadow:payments-NORD.csv") and source.exists()
    monkeypatch.setattr(Path, "replace", original)
    (home / "state" / "LIVE").touch()
    pay.refresh_to_pay(ledger, D0)
    assert not source.exists() and not ledger.get_meta("payment_shadow:payments-NORD.csv")
    assert list((source.parent / ".shadow").glob("payments-NORD.csv.*.blocked"))
    assert other.read_bytes() == b"unrelated legacy file"


def test_preparing_one_firm_does_not_assign_another_firms_queue(ledger):
    first = _op(ledger, 93)
    other = _op(ledger, 94, company="STUDIO9")
    pay.prepare_files(ledger, D0, company="NORD")
    assert ledger.payment(first)["file_id"] is not None
    assert ledger.payment(other)["file_id"] is None
