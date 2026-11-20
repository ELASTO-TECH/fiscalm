import json
from contextlib import closing
from datetime import datetime
from pathlib import Path

import anaf_fake
import pytest
from test_invoice_spv import (
    archive,  # noqa: F401
    ready as ready,
)
from test_invoicing import DATA, PARTY, draft, setup_invoice as setup_invoice  # noqa: F401

from fiscalm import backup, cli, migrations
from fiscalm.invoicing import engine as i, spv, ubl
from fiscalm.ledger import Ledger
from fiscalm.pdftext import pdf_text


def test_cli_end_to_end(home, monkeypatch, capsys):
    monkeypatch.setenv("FISCALM_SECRETS", str(home / "secrets"))
    with closing(Ledger()) as L:
        anaf_fake.setup(L)
    monkeypatch.setenv("FISCALM_ANAF_ENV", "test")

    def run(*argv):
        assert (
            cli.main(
                [
                    "invoice",
                    *argv,
                    *(
                        ["--company", "NORD"]
                        if argv[0] in {"show", "issue", "xml", "pdf", "upload", "storno", "delete"}
                        else []
                    ),
                ]
            )
            == 0
        )
        output = capsys.readouterr().out
        assert PARTY["iban"] not in output
        return json.loads(output)

    def input_file(name, value):
        path = home / (name + ".json")
        path.write_text(json.dumps(value))
        return str(path)

    run("seller", input_file("seller", dict(company="NORD", profile=PARTY)))
    cid = run(
        "client",
        input_file(
            "client",
            dict(company="NORD", profile=PARTY | dict(cui="99999992", vat_id="RO99999992", name="Client Fictiv SRL")),
        ),
    )["id"]
    run("series", "create", "--company", "NORD", "--code", "TEST", "--start", "42")
    run("series", "create", "--company", "NORD", "--code", "ALT")
    assert run("series", "default", "--company", "NORD", "--code", "TEST") == {"status": "saved"}
    assert run("series", "remove", "--company", "NORD", "--code", "ALT") == {"status": "removed"}
    assert [(x["code"], x["default"]) for x in run("series", "list", "--company", "NORD")] == [("TEST", True)]
    run("auto-upload", "--company", "NORD", "off")
    iid = run("draft", input_file("draft", dict(company="NORD", series="TEST", client_id=cid, data=DATA)))["id"]
    assert run("show", iid)["number"] is None
    assert run("issue", iid, "--version", "1", "--today", "2026-10-31")["number"] == 42
    xml, pdf = home / "invoice.xml", home / "invoice.pdf"
    run("xml", iid, "--output", str(xml))
    assert ubl.validate(xml.read_bytes()) == []
    run("pdf", iid, "--output", str(pdf))
    assert "121,00" in pdf_text(pdf) and "Client Fictiv SRL" in pdf_text(pdf)
    (home / "state" / "LIVE").touch()
    calls = []

    def transport(method, url, body):
        calls.append(url)
        assert url.startswith("https://api.anaf.ro/test/FCTEL/rest/")
        if "/upload?" in url:
            assert body == xml.read_bytes()
            return b'<header index_incarcare="10001"/>'
        if "/stareMesaj?" in url:
            return b'<header stare="ok" id_descarcare="10002"/>'
        return archive(xml.read_bytes())

    monkeypatch.setattr(i, "upload", lambda L, iid, **kwargs: spv.upload(L, iid, transport, **kwargs))
    monkeypatch.setattr(i, "poll", lambda L: spv.poll(L, transport))
    assert run("upload", iid)["status"] == "uploaded"
    assert run("poll")[0]["status"] == "accepted"
    assert cli.main(["invoice", "upload", iid, "--company", "NORD"]) == 2
    assert "Există deja o încercare de trimitere" in capsys.readouterr().out
    assert len(calls) == 3
    with closing(Ledger()) as L:
        assert Path(spv.submission(L, iid)["raw_path"]).read_bytes() == archive(xml.read_bytes())
    # Storno issues a distinct document; the original can no longer be deleted.
    assert run("storno", iid, "--today", "2026-11-01", "--reason", "Return test")["number"] == 43
    assert cli.main(["invoice", "delete", iid, "--company", "NORD"]) == 2
    capsys.readouterr()


def test_issue_never_uploads_and_the_tick_does_on_the_day(setup_invoice, home, monkeypatch, capsys):
    L, _ = setup_invoice
    anaf_fake.setup(L)  # its default: 4 days after issue
    called = []

    def after(L, iid, *args, **kwargs):
        called.append(iid)
        return {"status": "uploaded"}

    monkeypatch.setattr(spv, "upload", after)
    monkeypatch.setattr(i, "poll", lambda L: [])
    i.issue(L, draft(setup_invoice), DATA["issue_date"], company="NORD", version=1)  # issued in shadow mode
    (home / "state" / "LIVE").touch()
    iid = draft(setup_invoice)
    capsys.readouterr()
    assert cli.main(["invoice", "issue", iid, "--version", "1", "--company", "NORD", "--today", "2026-10-31"]) == 0
    assert json.loads(capsys.readouterr().out)["upload_on"] == "2026-11-04" and called == []
    assert cli.main(["invoice", "tick", "--today", "2026-11-03"]) == 0
    assert called == []
    assert cli.main(["invoice", "tick", "--today", "2026-11-04"]) == 0
    assert called == [iid]  # the shadow-issued invoice is never uploaded later
    (home / "state" / "LIVE").unlink()
    capsys.readouterr()
    assert cli.main(["invoice", "tick"]) == 0
    assert called == [iid]
    sandbox = json.loads(capsys.readouterr().out)  # Sandbox still uploads to ANAF TEST, and says so
    assert (sandbox["status"], sandbox["environment"]) == ("shadow", "test")


def test_cli_refusal_no_secrets(setup_invoice, home, capsys):
    bad = home / "bad.json"
    bad.write_text("not-json secret-do-not-print")
    assert cli.main(["invoice", "draft", str(bad)]) == 2
    assert "secret" not in capsys.readouterr().out
    assert cli.main(["invoice", "series", "create", "--company", "NORD", "--code", "TEST"]) == 2
    capsys.readouterr()
    bad.write_text("{}")
    assert cli.main(["invoice", "seller", str(bad)]) == 2
    assert "company" in json.loads(capsys.readouterr().out)["reason"]
    (home / "state" / "LIVE").touch()
    assert cli.main(["invoice", "tick"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_recovery_commands(ready, monkeypatch, capsys):
    L, iid = ready
    calls = []

    def resolve(L, key, **kw):
        calls.append((key, kw))
        return None

    monkeypatch.setattr(spv, "resolve", resolve)
    assert cli.main(["invoice", "resolve", iid, "--company", "NORD", "--not-uploaded", "--checked-in-spv"]) == 0
    assert calls == [(iid, dict(company="NORD", upload_id=None, checked_in_spv=True))]

    def recover(L, key, **kw):
        assert kw == dict(company="NORD", start=datetime(2026, 10, 30), end=datetime(2026, 11, 1, 23, 59, 59, 999999))
        return dict(status="uploaded", upload_id="123")

    monkeypatch.setattr(spv, "recover_sent", recover)
    assert cli.main(["invoice", "recover", iid, "--company", "NORD", "--from", "2026-10-30", "--to", "2026-11-01"]) == 0


def test_cli_replace_draft_requires_seen_version(setup_invoice, home, capsys):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    path = home / "replacement.json"
    path.write_text(json.dumps(dict(company="NORD", series="TEST", client_id=cid, data=DATA | dict(notes="CLI edit"))))
    args = ["invoice", "draft", str(path), "--id", iid]
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == i.VERSION_REQUIRED
    assert cli.main(args + ["--version", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 2
    assert cli.main(args + ["--version", "1"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == i.STALE_DRAFT
    inv = i.invoice(L, iid, company="NORD")
    assert inv["version"] == 2 and inv["data"]["notes"] == "CLI edit"


@pytest.mark.parametrize(
    "command",
    [["invoicing", "check-issued"], ["statment"], ["invoice", "show", "1", "--company", "NORD", "--no-such-flag"]],
)
def test_unknown_command_leaves_an_old_ledger_untouched(tmp_path, monkeypatch, capsys, command):
    path = tmp_path / "selected/state/ledger.db"
    old_version = migrations.SCHEMA_VERSION - 1
    with monkeypatch.context() as patch:  # a real previous schema, as test_schema_versions builds them
        patch.setattr(migrations, "SCHEMA_VERSION", old_version)
        Ledger(path).close()
    before = path.read_bytes()
    try:
        code = cli.main(["--home", str(tmp_path / "selected"), *command])
    except SystemExit as exc:
        code = exc.code
    assert migrations.read_version(path) == old_version and path.read_bytes() == before
    assert backup.directory() == tmp_path / "selected/backups" and not backup.directory().exists()
    assert code == 2 and "usage:" in capsys.readouterr().err
