"""Public tests: synthetic data only, fake company config. Real-fixture tests: tests_private (not in git)."""

import os
import shutil
import tempfile
from contextlib import closing
from pathlib import Path

# Nor any real data, secret or endpoint: every other path fiscalm reads points at an empty temp dir unless a test
# sets its own. Set before fiscalm is imported (some modules read them at import).
_FAKE = Path(tempfile.mkdtemp(prefix="fiscalm-tests-"))
os.environ["FISCALM_HOME"] = str(_FAKE / "home")
os.environ["FISCALM_SECRETS"] = str(_FAKE / "secrets")

import pytest  # noqa: E402


def _offline_updates(*args):
    raise OSError("Synthetic offline release check")


@pytest.fixture(scope="session")
def _seeded_state(tmp_path_factory):
    """One migrated, seeded state directory per worker; `home` copies it so each test still gets its own.

    It is made once per worker, before any test's environment or clock patches, so every copy carries that worker's
    migration timestamps (`schema_migration:*` meta rows) and whatever else creation writes. No test asserts on those;
    a test that needs a fresh initialisation builds its own ledger, as tests/test_schema_versions.py does."""
    from firms import seed

    from fiscalm.ledger import Ledger

    state = tmp_path_factory.mktemp("template") / "state"
    with closing(Ledger(state / "ledger.db")) as L:
        seed(L)
    leftovers = sorted(p.name for p in state.iterdir() if p.name.endswith(("-wal", "-shm", "-journal")))
    assert not leftovers, f"The template ledger left SQLite sidecars: {leftovers}"
    return state


@pytest.fixture(autouse=True)
def home(tmp_path, _seeded_state):
    from fiscalm import updates

    shutil.copytree(_seeded_state, tmp_path / "state")
    with pytest.MonkeyPatch.context() as environment:
        environment.setattr(updates, "build_opener", _offline_updates)
        environment.setenv("FISCALM_HOME", str(tmp_path))
        environment.delenv("FISCALM_HEADLESS", raising=False)
        yield tmp_path


def go_live(home: Path) -> None:
    """Switch a test's install to Live: the state/LIVE marker every outward effect checks."""
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "LIVE").touch()


@pytest.fixture
def live(home):
    """The test runs in Live from its start."""
    go_live(home)


@pytest.fixture
def sec(tmp_path, monkeypatch):
    """An empty secrets directory of the test's own (not created until something writes a secret)."""
    d = tmp_path / "secrets"
    monkeypatch.setenv("FISCALM_SECRETS", str(d))
    return d


@pytest.fixture
def ledger(home):
    from fiscalm.ledger import Ledger

    L = Ledger()
    yield L
    L.close()


@pytest.fixture
def token_owner(ledger):
    """Opt in to a synthetic owner; first-start tests still begin without accounts."""
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO users(id,username,display_name,role,webauthn_handle,created_at) "
            "VALUES('u_token','token-owner','Token owner','member',X'03','2026-11-04')"
        )
    return "u_token"


@pytest.fixture
def mail_account(ledger):
    from fiscalm import integrations

    return integrations.add(
        ledger,
        "mail",
        "Example mail",
        {
            "host": "imap.example.invalid",
            "port": "1993",
            "username": "login",
            "password": "synthetic-password",
            "from_address": "sender@example.invalid",
            "own_addresses": "alias@example.invalid",
        },
    )


def pytest_make_parametrize_id(config, val, argname):
    """Long bytes or text parameters get a short stable id (length and hash), not the payload itself.

    A 30 MB payload used as a test id was copied between xdist workers in every report.
    """
    if isinstance(val, (bytes, str)) and len(val) > 60:
        import hashlib

        raw = val if isinstance(val, bytes) else val.encode()
        return f"{argname}-{len(raw)}B-{hashlib.sha256(raw).hexdigest()[:8]}"
    return None
