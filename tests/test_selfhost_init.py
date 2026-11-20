"""fiscalm init and live: first run from empty, refusals that keep an existing config, and nothing written outside
the chosen home. Synthetic company data only."""

import argparse
import json
import os
import stat
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

import fiscalm
from fiscalm import cli, migrations, paths
from fiscalm.api import sessions, setup_links, tokens, users
from fiscalm.ledger import Ledger

COMPANY = [
    "--company-key",
    "EXAMPLE",
    "--cui",
    "12345678",
    "--legal-name",
    "Example SRL",
    "--iban",
    "RO35TEST0000000000000000",
]
INIT = ["init", *COMPANY]


def run_setup(tmp_path, *args):
    env = dict(
        os.environ,
        FISCALM_HOME=str(tmp_path / "data"),
        FISCALM_SECRETS=str(tmp_path / "secrets"),
    )
    return subprocess.run(
        [sys.executable, "-c", "import fiscalm; fiscalm.main()", *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


@pytest.fixture
def cli_in_process(monkeypatch, capsys):
    """The CLI entry point run in this process, for tests that only check a command's result."""

    def run(tmp_path, *args):
        monkeypatch.setenv("FISCALM_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "secrets"))
        monkeypatch.setattr(sys, "argv", ["fiscalm", *args])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit:
            fiscalm.main()
        out, err = capsys.readouterr()
        code = exit.value.code
        return subprocess.CompletedProcess(args, code if isinstance(code, int) else 1, out, err)

    return run


def test_init_from_empty_and_repeat(tmp_path):
    first = run_setup(tmp_path, *INIT)
    assert first.returncode == 0, first.stderr
    assert "Link de configurare (se salvează acum, valabil 72 de ore): " in first.stdout
    assert "/c/" not in first.stdout
    assert "Cheie API (se salvează acum): fiscalm_" in first.stdout
    assert "Sandbox:" in first.stdout
    assert not (tmp_path / "data/state/LIVE").exists()
    for directory in ("data", "data/state", "data/config", "data/logs", "secrets"):
        assert stat.S_IMODE((tmp_path / directory).stat().st_mode) == 0o700
    for file in ("data/state/ledger.db",):
        assert stat.S_IMODE((tmp_path / file).stat().st_mode) == 0o600
    with Ledger(tmp_path / "data/state/ledger.db").db as db:
        admin = db.execute("SELECT id,username,role,password_hash,totp_version FROM users").fetchone()
        assert tuple(admin)[1:] == ("admin", "admin", None, None)
        assert db.execute("SELECT user_id FROM api_tokens").fetchone()[0] == admin[0]
    status = run_setup(tmp_path, "integration")  # diagnostics read the new install, no network
    assert status.returncode == 0 and json.loads(status.stdout)["live"] is False
    again = run_setup(tmp_path, "init")
    assert again.returncode == 0 and "se salvează acum" not in again.stdout
    assert run_setup(tmp_path, "live", "on").returncode == 0
    assert run_setup(tmp_path, "live", "status").stdout.strip() == "live"
    assert run_setup(tmp_path, "init").stdout.strip() == "Modul Live este deja activ."
    assert run_setup(tmp_path, "live", "off").returncode == 0
    assert run_setup(tmp_path, "live", "status").stdout.strip() == "shadow"
    changed = [v if v != "Example SRL" else "Changed SRL" for v in INIT]
    assert run_setup(tmp_path, *changed).returncode == 1
    with Ledger(tmp_path / "data/state/ledger.db").db as db:
        assert db.execute("SELECT legal_name FROM firms").fetchone()[0] == "Example SRL"


def test_refusals_write_nothing(tmp_path):
    result = run_setup(tmp_path, "live", "on")
    assert result.returncode == 1
    assert result.stderr == "Add a company in fiscalm first.\n"
    assert not (tmp_path / "data/state/LIVE").exists()
    bad_key = [v if v != "EXAMPLE" else "../BAD" for v in INIT]
    assert run_setup(tmp_path, *bad_key).returncode == 1
    bad_iban = run_setup(tmp_path, *INIT[:-1], "RO00TEST0000")
    assert bad_iban.stderr == "Provide a valid IBAN (mod-97 checksum).\n"


def test_existing_permissions_kept_and_secrets_closed(tmp_path):
    for name in ("data", "secrets"):
        (tmp_path / name).mkdir()
        (tmp_path / name).chmod(0o755)  # establish permissions independently of the caller's umask
    assert run_setup(tmp_path, *INIT).returncode == 0
    assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "data/state/ledger.db").stat().st_mode) == 0o600


def test_existing_ledger_keeps_its_token_and_is_made_private(tmp_path, monkeypatch, capsys):
    root = tmp_path / "data"
    monkeypatch.setenv("FISCALM_HOME", str(root))
    monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "secrets"))
    ledger = Ledger()
    try:
        owner = users.create(ledger, "owner", "Owner", "member", datetime.now(UTC), actor_id=None)
        tokens.create(ledger, "Existing", "read", user_id=owner)  # a token, but no init marker
        ledger.db.execute("PRAGMA journal_mode=WAL")
        ledger.set_meta("test_sidecars", "yes")
        for suffix in ("", "-wal", "-shm"):
            Path(str(ledger.path) + suffix).chmod(0o644)
        assert cli.main(INIT) == 0
        assert "Cheie API" not in capsys.readouterr().out and len(tokens.list_tokens(ledger)) == 1
        for suffix in ("", "-wal", "-shm"):
            assert Path(str(ledger.path) + suffix).stat().st_mode & 0o777 == 0o600
    finally:
        ledger.close()


def test_integration_diagnostics_and_management_share_one_parser():
    parser = cli.build_parser()
    assert parser.parse_args(["integration"]).fn is cli.cmd_integration_status
    assert parser.parse_args(["integration", "list"]).fn is cli.cmd_integration


def commands(parser, path=()):
    """Every subcommand at every level once (an alias shares its command's parser): (words, parser, help line)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {choice.dest: choice.help for choice in action._choices_actions}
            walked = set()
            for name, sub in action.choices.items():
                if id(sub) not in walked:
                    walked.add(id(sub))
                    yield (*path, name), sub, helps.get(name)
                    yield from commands(sub, (*path, name))


def test_every_command_has_a_help_line_and_refuses_abbreviations():
    parser = cli.build_parser()
    assert [" ".join(words) for words, _, line in commands(parser) if not line] == []
    assert [" ".join(words) for words, sub, _ in commands(parser) if sub.allow_abbrev] == []
    assert not parser.allow_abbrev


@pytest.mark.parametrize(
    "args",
    [
        ["--ho", "{home}", "init"],
        ["--ho", "{home}", "live", "off"],
        ["--ho", "{home}", "company", "check"],
        ["--home", "{home}", "invoice", "show", "1", "--comp", "NORD"],
        ["--home", "{home}", "backup", "--include", "{home}.zip"],
        ["--ho={home}", "init"],
        ["--home", "{home}", "invoice", "show", "1", "--comp=NORD"],
        ["--home", "{home}", "restore", "{home}.zip", "--with"],
    ],
)
def test_abbreviated_options_are_refused_before_anything_is_written(tmp_path, cli_in_process, args):
    result = cli_in_process(tmp_path, *[arg.format(home=tmp_path / "chosen") for arg in args])
    assert result.returncode == 2 and "usage:" in result.stderr
    assert not (tmp_path / "chosen").exists() and not (tmp_path / "data").exists()


def older_ledger(home, monkeypatch):
    with monkeypatch.context() as patch:  # a real previous schema, as test_schema_versions builds them
        patch.setattr(migrations, "SCHEMA_VERSION", migrations.SCHEMA_VERSION - 1)
        Ledger(home / "state/ledger.db").close()


def tree(root):
    return {p: p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


@pytest.mark.parametrize("state", ["empty", "older schema"])
@pytest.mark.parametrize(
    "homes",
    [("--home", "{a}", "--home", "{a}"), ("--home={a}", "--home={a}"), ("--home", "{a}", "--home={b}")],
)
@pytest.mark.parametrize("command", [["init"], ["live", "off"], ["company", "check"], ["status"]])
def test_a_repeated_home_is_refused_before_anything_is_opened(
    tmp_path, monkeypatch, cli_in_process, state, homes, command
):
    a, b = tmp_path / "a", tmp_path / "b"
    if state == "older schema":
        older_ledger(a, monkeypatch)
        older_ledger(b, monkeypatch)
    before = tree(tmp_path)  # no ledger made or migrated, no pre-migration backup
    result = cli_in_process(tmp_path, *[arg.format(a=a, b=b) for arg in homes], *command)
    assert result.returncode == 2 and "--home" in result.stderr
    assert tree(tmp_path) == before


@pytest.mark.parametrize("state,code", [("empty", 3), ("older schema", 1), ("configured", 0)])
def test_company_check_takes_home_anywhere_and_never_writes(tmp_path, monkeypatch, state, code):
    """The tick's gate, as before the parser listed it: --home in any place, 3 with no company."""
    home = tmp_path / "chosen"
    if state == "configured":
        assert run_setup(tmp_path, "--home", str(home), *INIT).returncode == 0
    elif state == "older schema":
        older_ledger(home, monkeypatch)
    before = tree(home)
    for args in (["company", "check", "--home", str(home)], ["company", f"--home={home}", "check"]):
        assert run_setup(tmp_path, *args).returncode == code
    assert tree(home) == before
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("command", ["init", "live", "user", "auth", "backup", "restore", "tick", "scheduler"])
def test_guide_commands_listed_and_explained_without_a_ledger(tmp_path, cli_in_process, command):
    listing = cli_in_process(tmp_path, "--home", str(tmp_path / "empty"), "--help")
    assert listing.returncode == 0 and f"    {command} " in listing.stdout
    usage = cli_in_process(tmp_path, "--home", str(tmp_path / "empty"), command, "--help")
    assert usage.returncode == 0 and f"usage: fiscalm {command} " in usage.stdout
    assert not (tmp_path / "empty").exists() and not (tmp_path / "data").exists()


def test_home_defaults_before_cli_imports(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / "user"))
    env.pop("FISCALM_SECRETS", None)
    root = tmp_path / "isolated"
    result = subprocess.run(
        [sys.executable, "-c", "import fiscalm; fiscalm.main()", "--home", str(root), *INIT],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert (root / "state/ledger.db").exists()
    assert (root / "secrets").stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / "user/.config").exists()
    # Stop at the real serve/sync handlers, after normal CLI imports/config loading.
    for command in ("serve", "sync"):
        code = f"""import sys
from fiscalm import main
def trace(frame, event, arg):
    if event == 'call' and frame.f_code.co_name == 'cmd_{command}':
        from fiscalm.companies import all as firm_registry
        assert next(iter(firm_registry().values())).key == 'EXAMPLE'
        raise SystemExit(0)
sys.settrace(trace)
main()
"""
        result = subprocess.run(
            [sys.executable, "-c", code, "--home", str(root), command],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr


def test_server_user_commands_and_restore_revocation(tmp_path, cli_in_process):
    assert cli_in_process(tmp_path, "init").returncode == 0
    assert cli_in_process(tmp_path, "auth", "sweep").returncode == 0
    created = cli_in_process(tmp_path, "user", "create", "reader", "--role", "readonly")
    assert created.returncode == 0
    assert "Link de configurare" in created.stdout
    assert cli_in_process(tmp_path, "user", "create", "READER").returncode == 1
    listing = cli_in_process(tmp_path, "user", "list")
    rows = json.loads(listing.stdout)
    assert {r["username"]: r["role"] for r in rows} == {"admin": "admin", "reader": "readonly"}
    assert "password_hash" not in listing.stdout and "totp_version" not in listing.stdout
    path = tmp_path / "data/state/ledger.db"
    admin = next(r["id"] for r in rows if r["username"] == "admin")
    reader = next(r["id"] for r in rows if r["username"] == "reader")
    assert cli_in_process(tmp_path, "user", "disable", "reader").returncode == 0
    assert cli_in_process(tmp_path, "user", "enable", "reader").returncode == 0
    assert cli_in_process(tmp_path, "user", "role", "reader", "member").returncode == 0
    ledger = Ledger(path)
    try:
        ledger.db.execute(
            "UPDATE users SET password_hash='synthetic-hash',totp_version=? WHERE id=?", ("f" * 32, admin)
        )
        ledger.db.commit()
        assert cli_in_process(tmp_path, "user", "disable", "admin").returncode == 1
        assert cli_in_process(tmp_path, "user", "role", "admin", "member").returncode == 1
        assert cli_in_process(tmp_path, "user", "setup-link", "admin").returncode == 1
        reset = cli_in_process(tmp_path, "user", "reset", "admin")
        assert reset.returncode == 0 and "Link de configurare" in reset.stdout
        renewed = cli_in_process(tmp_path, "user", "setup-link", "admin")
        old = urllib.parse.urlsplit(reset.stdout.splitlines()[0].partition(": ")[2]).fragment
        fresh = urllib.parse.urlsplit(renewed.stdout.splitlines()[0].partition(": ")[2]).fragment
        assert setup_links.inspect(ledger, old, datetime.now(UTC)) is None
        assert setup_links.inspect(ledger, fresh, datetime.now(UTC))
        assert cli_in_process(tmp_path, "user", "unlock", "admin").returncode == 0
        assert cli_in_process(tmp_path, "user", "setup-link", "missing").returncode == 1
        cookie = sessions.issue(ledger, reader, datetime.now(UTC))
        assert cli_in_process(tmp_path, "auth", "forget-sessions").returncode == 0
        assert sessions.identify(ledger, cookie, datetime.now(UTC)) is None
        assert setup_links.inspect(ledger, fresh, datetime.now(UTC)) is None
        assert ledger.db.execute("SELECT revoked_at FROM api_tokens").fetchone()[0] is None
    finally:
        ledger.close()
    assert cli_in_process(tmp_path / "empty", "user", "list").returncode == 1


def test_two_initializers_publish_one_administrator_and_setup_link(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_setup(tmp_path, "init"), range(2)))
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results if r.returncode]
    assert sum("Link de configurare" in result.stdout for result in results) == 1
    assert sum("Cheie API" in result.stdout for result in results) == 1
    ledger = Ledger(tmp_path / "data/state/ledger.db")
    try:
        assert ledger.db.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0] == 1
        assert ledger.db.execute("SELECT count(*) FROM setup_links").fetchone()[0] == 1
        line = next(
            line for result in results for line in result.stdout.splitlines() if line.startswith("Link de configurare")
        )
        fragment = urllib.parse.urlsplit(line.partition(": ")[2]).fragment
        assert setup_links.inspect(ledger, fragment, datetime.now(UTC))["username"] == "admin"
    finally:
        ledger.close()


def test_failed_initial_api_write_leaves_no_account_or_setup_link(ledger, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic API credential failure")

    monkeypatch.setattr(tokens, "create", fail)
    with pytest.raises(RuntimeError):
        cli.main(["init"])
    assert ledger.db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM setup_links").fetchone()[0] == 0
    assert capsys.readouterr().out == ""


def test_repeat_init_preserves_accounts_and_prints_no_new_credentials(ledger, home, monkeypatch, capsys):
    monkeypatch.setenv("FISCALM_PUBLIC_URL", "https://fiscalm.example.invalid")
    monkeypatch.setenv("FISCALM_SECRETS", str(home / "secrets"))
    assert cli.main(["init"]) == 0
    assert "Link de configurare" in capsys.readouterr().out
    tables = ("users", "setup_links", "api_tokens", "auth_events")
    before = {table: ledger.db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    assert cli.main(["init"]) == 0
    assert "se salvează acum" not in capsys.readouterr().out
    assert {table: ledger.db.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before


@pytest.mark.parametrize("xdg", ["unset", "absolute", "relative"])
def test_default_directories_follow_xdg_and_never_name_live_data(tmp_path, monkeypatch, xdg):
    for name in ("FISCALM_HOME", "FISCALM_SECRETS", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    if xdg != "unset":  # a relative XDG value is invalid by the specification: it must not follow the working directory
        root = tmp_path if xdg == "absolute" else Path()
        monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    data, config = (tmp_path / ".local/share", tmp_path / ".config")
    if xdg == "absolute":
        data, config = tmp_path / "data", tmp_path / "config"
    assert (paths.finance_home(), paths.secrets_dir()) == (data / "fiscalm", config / "fiscalm/secrets")
