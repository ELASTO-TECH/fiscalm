import http.client
import importlib.util
import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import sys
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from test_api import DATA, _api, _draft, server as server, world as world  # noqa: F401

from fiscalm.api import actions, web
from fiscalm.invoicing import engine as invoicing, issued

ROOT = Path(__file__).parents[1]


class DevelopmentUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen = dict(self.headers)
        if self.path.startswith("/_next/"):
            self.send_response(101)
            self.end_headers()
            return
        data = json.dumps(dict(path=self.path, headers=dict(self.headers), port=self.server.server_port)).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_GET


def _development_certificate(root):
    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=localhost",
        "-keyout",
        str(root / "key.pem"),
        "-out",
        str(root / "cert.pem"),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _development_upstream(cleanup):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), DevelopmentUpstream)
    cleanup.callback(upstream.server_close)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    cleanup.callback(thread.join, 3)
    cleanup.callback(upstream.shutdown)
    return upstream


@pytest.fixture(params=[False, True], ids=["localhost-tls", "tailnet-http"])
def development_proxy(request, tmp_path):
    persistent = request.param
    root = tmp_path
    with ExitStack() as cleanup:
        _development_certificate(root)
        if not persistent:  # Only a directory bin/dev marked as its own is accepted.
            refused = subprocess.run(
                ["node", str(ROOT / "bin/dev-proxy.mjs")],
                env=dict(os.environ, FISCALM_DEV_ROOT=str(root)),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert refused.returncode != 0 and "Start this helper through bin/dev engine." in refused.stderr
            (root / ".fiscalm-dev").touch()
        app, engine = (_development_upstream(cleanup) for _ in range(2))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(
            os.environ,
            FISCALM_DEV_ROOT=str(root),
            FISCALM_DEV_PORT=str(port),
            FISCALM_DEV_APP_PORT=str(app.server_port),
            FISCALM_DEV_ENGINE_PORT=str(engine.server_port),
        )
        if persistent:
            env.update(FISCALM_DEV_PERSISTENT="1", FISCALM_PUBLIC_URL=f"https://localhost:{port}")
        process = subprocess.Popen(
            ["node", str(ROOT / "bin/dev-proxy.mjs")],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        cleanup.callback(process.communicate, timeout=3)
        cleanup.callback(process.terminate)
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == (
            "Development proxy ready." if persistent else "Development HTTPS ready."
        )
        yield port, app, engine, persistent


def test_development_https_proxy_preserves_the_auth_boundary(development_proxy):
    port, app, engine, persistent = development_proxy
    authority = f"localhost:{port}"

    def request(path, headers, method="GET"):
        connection = (
            http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            if persistent
            else http.client.HTTPSConnection("127.0.0.1", port, context=ssl._create_unverified_context(), timeout=3)
        )
        try:
            connection.request(method, path, headers={"Host": authority, **headers})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    spoofed = {
        "X-Forwarded-Host": "forged.invalid",
        "X-Forwarded-Proto": "http",
        "X-Forwarded-For": "203.0.113.1",
        "Forwarded": "host=forged.invalid",
        "Origin": "https://forged.invalid",
        "Cookie": "fixture=value",
    }
    paths = (
        "/auth/password",
        "/web/api/me",
        "/api/v1/companies",
        "/u/example",
        "/manifest.webmanifest",
        "/hooks/shopify/example",
        "/bank/return?state=fixture",
        "/emag-pdf/example",
        "/healthz",
        "/anaf/callback?state=fixture",
    )
    for path in paths:
        status, body = request(path, spoofed, "POST" if path.startswith("/hooks/") else "GET")
        data = json.loads(body)
        assert status == 200 and data["port"] == engine.server_port and data["path"] == path
        headers = {key.lower(): value for key, value in data["headers"].items()}
        assert (headers["x-forwarded-host"], headers["x-forwarded-proto"], headers["x-forwarded-for"]) == (
            authority,
            "https",
            "127.0.0.1",
        )
        assert headers["cookie"] == "fixture=value" and "forwarded" not in headers
        assert headers["origin"] == "https://forged.invalid"  # The engine must see, and refuse, the actual Origin.
    for path in ("/", "/setup", "/web/"):
        status, body = request(path, spoofed)
        data = json.loads(body)
        assert status == 200 and data["port"] == app.server_port and data["path"] == "/"
        assert not {"cookie", "forwarded", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-for"} & {
            key.lower() for key in data["headers"]
        }
    assert request("/auth/password", {"Host": "forged.invalid"})[0] == 404
    assert request("//forged.invalid/auth/password", {})[0] == 404
    assert request("https://forged.invalid/auth/password", {})[0] == 404
    assert request("/\\[", {})[0] == 404
    assert request("/web/api/me", {})[0] == 200  # A malformed target must not terminate the proxy.
    assert request("/_next/webpack-hmr", {"Connection": "Upgrade", "Upgrade": "websocket"})[0] == 101
    assert "cookie" not in {key.lower() for key in app.seen}


def test_development_proxy_refuses_plain_http_duplicate_hosts_and_api_upgrades(development_proxy):
    port, _, _, persistent = development_proxy

    def connect():
        if persistent:
            return http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        return http.client.HTTPSConnection("127.0.0.1", port, context=ssl._create_unverified_context(), timeout=3)

    if not persistent:
        with (
            closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as connection,
            pytest.raises((OSError, http.client.HTTPException)),
        ):
            connection.request("GET", "/web/api/me", headers={"Host": f"localhost:{port}", "Cookie": "fixture=value"})
            connection.getresponse()
    with closing(connect()) as connection:
        connection.putrequest("GET", "/web/api/me", skip_host=True)
        connection.putheader("Host", f"localhost:{port}")
        connection.putheader("Host", f"localhost:{port}")
        connection.endheaders()
        assert connection.getresponse().status == 404
    with closing(connect()) as connection:
        connection.request(
            "GET",
            "/auth/password",
            headers={"Host": f"localhost:{port}", "Connection": "Upgrade", "Upgrade": "websocket"},
        )
        with pytest.raises(http.client.RemoteDisconnected):
            connection.getresponse()


@pytest.mark.parametrize("local", ["clean", "dirty", "ahead"])
def test_persistent_development_updates_only_clean_fast_forwards(tmp_path, monkeypatch, local):
    spec = importlib.util.spec_from_file_location("dev_session", ROOT / "bin/dev-session.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    upstream, checkout = tmp_path / "upstream", tmp_path / "checkout"

    def git(at, *args):
        return subprocess.check_output(["git", "-C", str(at), *args], text=True, stderr=subprocess.DEVNULL).strip()

    upstream.mkdir()
    git(upstream, "init", "-b", "dev")
    for at in (upstream,):
        git(at, "config", "user.name", "Synthetic developer")
        git(at, "config", "user.email", "developer@example.invalid")
    git(upstream, "commit", "--allow-empty", "-m", "Initial")
    subprocess.run(["git", "clone", "-q", str(upstream), str(checkout)], check=True)
    git_home = tmp_path / "git-home"
    git_home.mkdir()
    (git_home / ".gitconfig").write_text("[user]\nname = Synthetic developer\n")
    monkeypatch.setenv("HOME", str(tmp_path / "ai-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ai-home/.config"))
    monkeypatch.setenv("FISCALM_DEV_GIT_HOME", str(git_home))
    monkeypatch.setattr(module, "ROOT", checkout)
    assert module.git("config", "--global", "--get", "user.name") == "Synthetic developer"
    assert os.environ["HOME"] == str(tmp_path / "ai-home")
    monkeypatch.setenv("FISCALM_HOME", str(tmp_path / "sandbox"))
    module.require_sandbox()
    if local == "dirty":
        (checkout / "unsaved").touch()
    elif local == "ahead":
        git(checkout, "config", "user.name", "Synthetic developer")
        git(checkout, "config", "user.email", "developer@example.invalid")
        git(checkout, "commit", "--allow-empty", "-m", "Local work")
    before = git(checkout, "rev-parse", "HEAD")
    git(upstream, "commit", "--allow-empty", "-m", "Reviewed fix")
    if local == "ahead":
        with pytest.raises(subprocess.CalledProcessError):
            module.update()
    else:
        assert module.update() == (local == "clean")
    assert git(checkout, "rev-parse", "HEAD") == (git(upstream, "rev-parse", "HEAD") if local == "clean" else before)
    if local == "clean":
        marker = tmp_path / "sandbox/state/LIVE"
        marker.parent.mkdir(parents=True)
        marker.touch()
        with pytest.raises(SystemExit, match="Sandbox"):
            module.require_sandbox()


RUFF = (["uv", "run", "ruff", "format", "--check"], ["uv", "run", "ruff", "check"])
PYTEST = ["uv", "run", "pytest", "-q", "--ff", "-n", "2"]


def _quick_calls(*paths):
    return [[*RUFF[0], *paths], [*RUFF[1], *paths], [*PYTEST, *paths]]


@pytest.mark.parametrize(
    "changes, fail, code, calls",
    [
        (["edit-test"], None, 0, _quick_calls("tests/test_a.py")),
        (["new:tests/test_has\nnewline.py"], None, 0, _quick_calls("tests/test_has\nnewline.py")),
        (["new:tests/test_ță.py"], None, 0, _quick_calls("tests/test_ță.py")),
        (["delete-test"], None, 0, []),
        (["edit-sql"], None, 0, []),
        (["delete-app"], None, 0, [["npm", "test", "--", "--maxWorkers=2"]]),
        (["edit-app"], None, 0, [["npm", "test", "--", "--changed", "base", "--maxWorkers=2"]]),
        (["edit-test", "edit-app"], "ruff", 1, _quick_calls("tests/test_a.py")[:1]),
        (["edit-test", "edit-app"], "pytest", 1, _quick_calls("tests/test_a.py")),
        (["edit-app"], "base", 1, []),
        (["edit-app"], "diff", 1, []),
        (["edit-app"], "ls-files", 1, []),
    ],
    ids=[
        "test",
        "newline",
        "non-ascii",
        "deleted-test",
        "sql",
        "deleted-app",
        "app",
        "ruff-fails",
        "pytest-fails",
        "missing-base",
        "git-diff-fails",
        "git-ls-files-fails",
    ],
)
def test_quick_checks_exactly_what_changed_and_stops_on_any_failure(tmp_path, changes, fail, code, calls):
    repo, shims, log = tmp_path / "repo", tmp_path / "shims", tmp_path / "calls"
    files = {
        "bin/test.sh": (ROOT / "bin/test.sh").read_text(),
        ".gitignore": "node_modules/\n",
        "src/fiscalm/baseline.sql": "CREATE TABLE t(x);\n",
        "tests/test_a.py": "def test_a():\n    pass\n",
        "app/src/x.ts": "export const x = 1;\n",
        "app/package-lock.json": "{}\n",
        "app/node_modules/.fiscalm-lock": "{}\n",
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    (repo / "bin/test.sh").chmod(0o755)
    git = ["git", "-C", str(repo), "-c", "user.name=Synthetic", "-c", "user.email=dev@example.invalid"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "Base"], ["tag", "base"]):
        subprocess.run([*git, *args], check=True)
    shims.mkdir()
    # Each call is logged as one JSON argument vector, so argument boundaries stay visible.
    record = f"#!{sys.executable}\nimport json, sys\nargs = [sys.argv[0].rsplit('/', 1)[1], *sys.argv[1:]]\n"
    record += f"open({str(log)!r}, 'a').write(json.dumps(args) + '\\n')\nsys.exit({fail!r} in args[:3])\n"
    for tool in ("uv", "npm"):
        (shims / tool).write_text(record)
    if fail in ("diff", "ls-files"):
        (shims / "git").write_text(f'#!/bin/sh\n[ "$1" = {fail} ] && exit 128\nexec "{shutil.which("git")}" "$@"\n')
    for shim in shims.iterdir():
        shim.chmod(0o755)
    edits = {
        "edit-test": lambda: (repo / "tests/test_a.py").write_text("def test_a():\n    assert True\n"),
        "delete-test": lambda: (repo / "tests/test_a.py").unlink(),
        "edit-sql": lambda: (repo / "src/fiscalm/baseline.sql").write_text("CREATE TABLE u(x);\n"),
        "delete-app": lambda: (repo / "app/src/x.ts").unlink(),
        "edit-app": lambda: (repo / "app/src/x.ts").write_text("export const x = 2;\n"),
    }
    for change in changes:
        edits[change]() if change in edits else (repo / change.removeprefix("new:")).write_text(
            "def test_b():\n    pass\n"
        )
    env = dict(os.environ, PATH=f"{shims}:{os.environ['PATH']}", FISCALM_TEST_BASE="nope" if fail == "base" else "base")
    result = subprocess.run(["bin/test.sh", "quick"], cwd=repo, env=env, capture_output=True, text=True, timeout=30)
    recorded = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert (result.returncode, recorded) == (code, calls), result.stdout + result.stderr


@pytest.mark.parametrize("helper", ["dev-engine", "dev-session"])
def test_development_helpers_refuse_live_before_writing(ledger, home, server, monkeypatch, helper):
    monkeypatch.syspath_prepend(str(ROOT / "bin"))
    spec = importlib.util.spec_from_file_location("dev_helper", ROOT / f"bin/{helper}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    production = actions.ACTIONS["mode-switch"]
    monkeypatch.setitem(actions.ACTIONS, "mode-switch", production)

    _, invoice = _draft(ledger, "DEV")
    invoicing.issue(ledger, invoice, DATA["issue_date"], company="NORD", version=1)
    own = invoicing.import_id("local", invoice)
    with ledger.tx() as db:
        db.execute("DELETE FROM invoice_sent_links WHERE invoice_id=?", (invoice,))
        db.execute("DELETE FROM issued WHERE message_id=?", (own,))
    ledger.set_meta(issued.OWN_ROWS_PENDING, "pending")

    def exercise(*args):
        assert ledger.get_meta(issued.OWN_ROWS_PENDING) is None
        assert ledger.issued(own) is not None
        body = {"mode": "live", "expected": "sandbox", "confirmed": "yes", "request_id": "a" * 32}
        before = [tuple(row) for row in ledger.db.execute("SELECT * FROM auth_events")]
        status, answer = _api(server, "mode-switch", body)
        assert status == 200 and not answer["ok"]
        assert answer["message"] == "Instalarea de dezvoltare rămâne în Sandbox."
        assert not (home / "state/LIVE").exists()
        assert not ledger.get_meta("mode-request:" + "a" * 32)
        assert [tuple(row) for row in ledger.db.execute("SELECT * FROM auth_events")] == before
        # Sandbox remains the ordinary no-op response, not a development refusal.
        body.update(mode="sandbox")
        answer = _api(server, "mode-switch", body)[1]
        assert {key: answer[key] for key in ("ok", "message", "next", "done")} == actions._mode_switch(
            ledger, "", body, actions.Context("mode-switch", body, datetime.now(UTC), created_by="u_admin")
        )
        assert not (home / "state/LIVE").exists()
        assert [tuple(row) for row in ledger.db.execute("SELECT * FROM auth_events")] == before
        monkeypatch.setitem(actions.ACTIONS, "mode-switch", production)
        body.update(mode="live")
        assert _api(server, "mode-switch", body)[1]["ok"]
        assert (home / "state/LIVE").exists()
        assert ledger.db.execute("SELECT 1 FROM auth_events WHERE kind='mode_changed'").fetchone()

    dev_sandbox = importlib.import_module("dev_sandbox")

    monkeypatch.setattr(web, "serve", exercise)
    if helper == "dev-engine":
        monkeypatch.setattr(module, "require_isolation", lambda: None)
        monkeypatch.setattr(module, "offline_anaf", lambda: None)
        monkeypatch.setattr(sys, "argv", ["dev-engine.py", "--serve"])
        module.main()
    else:
        calls = []
        monkeypatch.setattr(module, "start", lambda *args: calls.append(args))
        monkeypatch.setattr(module, "stop", lambda process: None)
        monkeypatch.setenv("FISCALM_DEV_APP_PORT", "3001")
        monkeypatch.setattr(module, "sources", lambda: (_ for _ in ()).throw(InterruptedError()))
        with pytest.raises(InterruptedError):
            module.run()
        assert calls[0] == (str(ROOT / ".venv/bin/python"), str(ROOT / "bin/dev_sandbox.py"))
        dev_sandbox.serve_engine()


def test_verify_engine_reaches_only_loopback():
    """bin/verify's engine refuses every name, TCP connection and datagram off the machine, and keeps loopback.
    In a child process: the audit hook cannot be removed."""
    probe = """
import importlib.util, socket, sys
spec = importlib.util.spec_from_file_location("dev_engine", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.offline_network()
refused = []
for attempt in (
    lambda: socket.getaddrinfo("imap.example.com", 993),
    lambda: socket.gethostbyname("example.com"),
    lambda: socket.socket().connect(("192.0.2.1", 993)),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("192.0.2.53", 53)),
):
    try:
        attempt()
    except OSError as e:
        refused.append("offline" in str(e))
with socket.socket() as listener, socket.socket() as client:
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    client.connect(socket.getaddrinfo("localhost", listener.getsockname()[1], socket.AF_INET)[0][4])
print(refused)
"""
    env = os.environ | {"PYTHONPATH": os.pathsep.join([str(ROOT / "bin"), str(ROOT / "tests")])}
    run = subprocess.run(
        [sys.executable, "-c", probe, str(ROOT / "bin/dev-engine.py")], capture_output=True, text=True, env=env
    )
    assert (run.returncode, run.stdout.strip()) == (0, "[True, True, True, True]"), run.stderr
