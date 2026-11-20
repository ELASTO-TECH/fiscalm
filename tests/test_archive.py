from unittest.mock import call, patch

import pytest
from conftest import go_live

from fiscalm import archive, integrations as ig


class Drive:
    calls = []
    files = []

    def __init__(self, cfg):
        self.calls.append(cfg["remote"])

    def mkdir(self, parent, name):
        self.calls.append(name)
        return "folder"

    def list(self, folder):
        return self.files

    def upload(self, src, folder, name):
        self.calls.append((name, src.read_bytes()))


@pytest.fixture
def configured(ledger, home):
    Drive.calls, Drive.files = [], []
    item = ig.add(ledger, "storage", "Archive", dict(company="NORD", remote="drive:", root="root", layout="quarter"))
    root = home / "NORD"
    root.mkdir()
    source = root / "receipt.pdf"
    source.write_bytes(b"document")
    ledger.add_document("sha", "NORD", "2026-11", "receipt", str(source))
    return item, source


def test_shadow_and_dry_run_make_no_remote_calls(ledger, home, configured):
    ledger.add_document("second", "NORD", "2026-11", "receipt", str(configured[1]))
    with patch.object(archive.companies, "all", wraps=archive.companies.all) as registry:
        assert [r["status"] for r in archive.archive_all(ledger, drive_factory=Drive)] == ["would copy"] * 2
        assert registry.call_args_list == [call(ledger)]
    go_live(home)
    assert archive.archive_all(ledger, dry_run=True, drive_factory=Drive)[0]["status"] == "would copy"
    assert Drive.calls == []
    assert ledger.db.execute("SELECT count(*) FROM archive").fetchone()[0] == 0


def test_company_layout_and_retry(ledger, home, configured):
    go_live(home)
    assert archive.archive_all(ledger, drive_factory=Drive)[0]["status"] == "copied"
    assert Drive.calls == ["drive:", "2026", "Q4", "2026-11", "Cheltuieli", ("receipt.pdf", b"document")]
    assert archive.archive_all(ledger, drive_factory=Drive) == []


@pytest.mark.parametrize(
    "refusal", ["disabled", "other-company", "ambiguous", "symlink", "outside", "company-symlink", "month", "name"]
)
def test_refusals_never_upload(ledger, home, configured, refusal, tmp_path):
    item, source = configured
    if refusal == "disabled":
        ig.set_enabled(ledger, item["id"], False)
    elif refusal == "other-company":
        ig.update(ledger, item["id"], {"company": "STUDIO9"})
    elif refusal == "ambiguous":
        ig.add(ledger, "storage", "Other", dict(company="NORD", remote="other:", root="other", layout="month"))
    elif refusal in ("symlink", "outside", "company-symlink"):
        outside = home / "SUD"
        outside.mkdir()
        f = outside / "secret.pdf"
        f.write_bytes(b"private")
        if refusal == "symlink":
            source.unlink()
            source.symlink_to(f)
        elif refusal == "company-symlink":
            source.unlink()
            source.parent.rmdir()
            source.parent.symlink_to(outside, target_is_directory=True)
        else:
            ledger.db.execute("UPDATE documents SET path=?", (str(f),))
    elif refusal == "month":
        ledger.db.execute("UPDATE documents SET month='2026-99'")
    else:
        ledger.db.execute("UPDATE documents SET path=?", (str(source.parent / "..bad:name.pdf"),))
    go_live(home)
    assert archive.archive_all(ledger, drive_factory=Drive)[0]["status"] == "error"
    assert Drive.calls == []


def test_collision_is_retried_not_recorded(ledger, home, configured):
    go_live(home)
    Drive.files = [{"Name": "receipt.pdf", "Hashes": {"md5": "different"}}]
    assert archive.archive_all(ledger, drive_factory=Drive)[0]["status"] == "error"
    assert ledger.db.execute("SELECT count(*) FROM archive").fetchone()[0] == 0
    Drive.files = []
    assert archive.archive_all(ledger, drive_factory=Drive)[0]["status"] == "copied"


@pytest.mark.parametrize(
    "field,value",
    [("remote", "../else:"), ("remote", ":local:"), ("root", "../escape"), ("layout", "{q}"), ("company", "NOPE")],
)
def test_storage_fields_refuse_paths_and_free_formats(ledger, field, value):
    cfg = dict(company="NORD", remote="drive:", root="folder", layout="month")
    cfg[field] = value
    with pytest.raises(ig.IntegrationError):
        ig.add(ledger, "storage", "Archive", cfg)


def test_month_layout_and_statement_filename(ledger, home, configured):
    item, source = configured
    ig.update(ledger, item["id"], {"layout": "month"})
    ledger.db.execute("UPDATE documents SET kind='statement'")
    go_live(home)
    assert archive.archive_all(ledger, drive_factory=Drive)[0]["dest"] == "NORD/2026-11/Extrase/receipt.pdf"
    assert "Q4" not in Drive.calls


def test_drive_refuses_duplicate_folders_before_upload(monkeypatch):
    monkeypatch.setattr(archive.Drive, "_run", lambda *args, **kw: "drive: drive")
    drive = archive.Drive({"remote": "drive:"})
    monkeypatch.setattr(
        drive,
        "list",
        lambda _: [{"Name": "2026-11", "IsDir": True, "ID": "a"}, {"Name": "2026-11", "IsDir": True, "ID": "b"}],
    )
    with pytest.raises(archive.DriveError):
        drive.mkdir("root", "2026-11")


def test_path_swap_does_not_change_upload(ledger, home, configured):
    _, source = configured

    class Swap(Drive):
        def mkdir(self, parent, name):
            source.write_bytes(b"changed after read")
            return super().mkdir(parent, name)

    go_live(home)
    archive.archive_all(ledger, drive_factory=Swap)
    assert Drive.calls[-1] == ("receipt.pdf", b"document")


@pytest.mark.parametrize("name", ["Factura (1).pdf", "factura nr.12, 2026.pdf", "a+b.pdf"])
def test_attachment_punctuation_gets_stable_safe_name(ledger, home, configured, name):
    _, source = configured
    renamed = source.with_name(name)
    source.rename(renamed)
    ledger.db.execute("UPDATE documents SET path=?", (str(renamed),))
    go_live(home)
    result = archive.archive_all(ledger, drive_factory=Drive)[0]
    assert result["status"] == "copied"
    uploaded, data = Drive.calls[-1]
    assert uploaded.endswith(".pdf") and data == b"document"
    assert archive.target(ledger.documents()[0])[2] == uploaded


def test_refusal_reason_survives(ledger, home, configured):
    ig.set_enabled(ledger, configured[0]["id"], False)
    assert "exactly one enabled archive" in archive.archive_all(ledger)[0]["note"]


@pytest.mark.parametrize("reply", ["broken json", "{}", "[{}]", '[{"Name":"folder","IsDir":true}]'])
def test_bad_listing_is_an_actionable_refusal(monkeypatch, reply):
    monkeypatch.setattr(
        archive.Drive, "_run", lambda self, cmd, *args, **kw: "drive: drive" if cmd == "listremotes" else reply
    )
    drive = archive.Drive({"remote": "drive:"})
    with pytest.raises(archive.DriveError, match="invalid Drive listing"):
        drive.list("root")


@pytest.mark.parametrize("remote", ["drive: local", "drive: sftp", "other: drive"])
def test_only_google_drive_remotes(monkeypatch, remote):
    monkeypatch.setattr(archive.Drive, "_run", lambda *args, **kw: remote)
    with pytest.raises(archive.DriveError, match="Google Drive"):
        archive.Drive({"remote": "drive:"})


def test_identical_existing_file_recorded(ledger, home, configured):
    import hashlib

    Drive.files = [{"Name": "receipt.pdf", "Hashes": {"md5": hashlib.md5(b"document").hexdigest()}}]
    go_live(home)
    assert archive.archive_all(ledger, drive_factory=Drive)[0]["status"] == "already there"
    assert archive.archive_all(ledger, drive_factory=Drive) == []


def test_upload_failure_retries(ledger, home, configured):
    class Failing(Drive):
        def upload(self, *args):
            raise archive.DriveError("upload failed; check access")

    go_live(home)
    assert archive.archive_all(ledger, drive_factory=Failing)[0]["note"] == "upload failed; check access"
    assert archive.archive_all(ledger, drive_factory=Drive)[0]["status"] == "copied"


def test_oversized_file_refused(ledger, home, configured):
    with configured[1].open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    go_live(home)
    assert "50 MiB" in archive.archive_all(ledger, drive_factory=Drive)[0]["note"]
    assert Drive.calls == []


@pytest.mark.parametrize("month,quarter", [("2026-03", "Q1"), ("2026-04", "Q2")])
def test_invoice_date_and_quarter_boundary(ledger, home, configured, month, quarter):
    ledger.db.execute("UPDATE documents SET doc_date=?", (month + "-01",))
    go_live(home)
    assert (
        archive.archive_all(ledger, drive_factory=Drive)[0]["dest"]
        == f"NORD/2026/{quarter}/{month}/Cheltuieli/receipt.pdf"
    )


def test_disabled_archive_does_not_make_choice_ambiguous(ledger, home, configured):
    extra = ig.add(ledger, "storage", "Disabled", dict(company="NORD", remote="other:", root="root", layout="month"))
    ig.set_enabled(ledger, extra["id"], False)
    go_live(home)
    assert archive.archive_all(ledger, drive_factory=Drive)[0]["status"] == "copied"
