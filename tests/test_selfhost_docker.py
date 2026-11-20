import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fiscalm import scheduler
from fiscalm.api import actions

FISCALM = [sys.executable, "-m", "fiscalm"]


def stop(seconds):  # the scheduler's sleep: the first poll ends the test
    raise SystemExit(77)


def fake_tick(tmp_path, body):
    tick = tmp_path / "tick"
    tick.write_text("#!/bin/bash\n" + body + "\n")
    tick.chmod(0o700)
    return [str(tick)]


def poll(home, command):
    with pytest.raises(SystemExit) as stopped:
        scheduler.ticks(5, command, home, sleep=stop)
    assert stopped.value.code == 77


def test_check_now_asks_the_scheduler(home):
    actions._tick()
    assert (home / "state/tick.request").is_file()
    actions._tick()
    outside = home.parent / "outside"
    (home / "state/tick.request").unlink()
    (home / "state/tick.request").symlink_to(outside)
    with pytest.raises(OSError):
        actions._tick()  # a link at the request's name is refused, never followed
    assert not outside.exists()


@pytest.mark.parametrize("kind", ["fifo", "fifo with a reader", "directory"])
def test_check_now_refuses_what_is_not_a_regular_file_without_blocking(home, kind):
    request = home / "state/tick.request"
    request.parent.mkdir(exist_ok=True)
    request.mkdir() if kind == "directory" else os.mkfifo(request)
    reader = os.open(request, os.O_RDONLY | os.O_NONBLOCK) if kind == "fifo with a reader" else None
    outcome = []

    def ask():
        try:
            actions._tick()
            outcome.append("written")
        except OSError:
            outcome.append("refused")

    asking = threading.Thread(target=ask, daemon=True)
    asking.start()
    asking.join(5)  # a blocking open would hang here: fail instead of hanging the suite
    try:
        assert outcome == ["refused"]
    finally:
        if asking.is_alive() and kind == "fifo":
            os.close(os.open(request, os.O_RDONLY | os.O_NONBLOCK))  # release a blocked writer
        if reader is not None:
            os.close(reader)


@pytest.mark.parametrize("kind", ["dangling link", "link to a file", "directory"])
def test_scheduler_removes_a_request_that_is_not_a_regular_file(home, tmp_path, kind):
    request, target = home / "state/tick.request", tmp_path / "target"
    if kind == "directory":
        request.mkdir()
        (request / "inside").touch()
    else:
        if kind == "link to a file":
            target.touch()
        request.symlink_to(target)
    poll(home, fake_tick(tmp_path, 'echo tick > "$FISCALM_HOME/ran"'))
    assert not os.path.lexists(request) and not (home / "ran").exists()
    assert target.exists() == (kind == "link to a file")  # never followed


def test_a_second_sigterm_does_not_cut_the_cleanup_short(tmp_path):
    ready, done = tmp_path / "ready", tmp_path / "done"
    # The step needs half a second to stop; the tick gets SIGTERM twice meanwhile.
    step = fake_tick(
        tmp_path,
        f"trap 'sleep 0.5; echo done > {done}; exit 0' TERM\necho ready > {ready}\nwhile true; do sleep 0.1; done",
    )
    code = (
        "import signal, sys\nfrom fiscalm import scheduler\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\n"
        f"with open({str(tmp_path / 'log')!r}, 'ab', buffering=0) as log:\n"
        f"    scheduler._run(scheduler.Run({step!r}, log, log.fileno()), (), 60)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        p.terminate()
        time.sleep(0.2)
        p.terminate()
        assert p.wait(timeout=15) == 143
        assert done.exists()  # the tick waited for its step to finish stopping
    finally:
        if p.poll() is None:
            p.kill()


def test_a_timed_out_step_takes_its_whole_group(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STOP_GRACE", 0.3)
    pid = tmp_path / "pid"
    # The step exits on SIGTERM at once; the process it started ignores SIGTERM.
    step = fake_tick(tmp_path, f"(trap '' TERM; exec sleep 30) & echo $! > {pid}\nexec sleep 30")
    with open(tmp_path / "log", "ab", buffering=0) as log, open(tmp_path / "lock", "a") as lock:
        assert scheduler._run(scheduler.Run(step, log, lock.fileno()), (), 0.5) == (scheduler.TIMED_OUT, "")
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_a_killed_tick_keeps_the_lock_while_its_step_runs(tmp_path):
    home, pid, calls = tmp_path / "home", tmp_path / "pid", tmp_path / "calls"
    slow = fake_tick(tmp_path, f'[ "$*" = "company check" ] || exit 0\necho $$ > {pid}\nexec sleep 30')
    other = tmp_path / "other"
    other.write_text(f'#!/bin/bash\necho "$*" >> {calls}\n')
    other.chmod(0o700)
    code = f"import sys\nfrom pathlib import Path\nfrom fiscalm import scheduler\nsys.exit(scheduler.tick(Path({str(home)!r}), {slow!r}))"
    tick = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "FISCALM_COMPANY_CHECK_TIMEOUT": "60"})
    deadline = time.monotonic() + 10
    while not pid.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    step = int(pid.read_text())
    try:
        tick.kill()
        tick.wait(timeout=5)
        assert scheduler.tick(home, [str(other)]) == 0 and not calls.exists()
        assert "already running, skip" in next((home / "logs").glob("*.log")).read_text()
    finally:
        os.killpg(step, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while scheduler._group_alive(step) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert scheduler.tick(home, [str(other)]) == 0 and calls.exists()  # the lock went with the step


@pytest.mark.parametrize(
    "option,variable,values",
    [([], "FISCALM_TICK_INTERVAL", ("0", "-1", "text", "2")), (["--backup"], "FISCALM_BACKUP_INTERVAL", ("59", "x"))],
)
def test_schedulers_refuse_bad_intervals_loudly(home, option, variable, values):
    for value in values:
        result = subprocess.run(
            [*FISCALM, "scheduler", *option], env={**os.environ, variable: value}, capture_output=True, timeout=10
        )
        assert result.returncode == 1 and variable.encode() in result.stderr  # a restart loop must say why


def test_scheduler_consumes_request(home, tmp_path):
    (home / "state/tick.request").touch()
    poll(home, fake_tick(tmp_path, 'echo tick > "$FISCALM_HOME/ran"'))
    assert (home / "ran").read_text().strip() == "tick"
    assert not (home / "state/tick.request").exists()


def test_scheduler_restart_waits_and_failed_tick_does_not_loop(home, tmp_path, capfd):
    command = fake_tick(tmp_path, 'echo tick >> "$FISCALM_HOME/ran"\nexit 1')
    for _ in range(2):
        poll(home, command)
    assert not (home / "ran").exists()
    (home / "state/tick.next").write_text("1")
    poll(home, command)
    assert "Scheduled tick failed" in capfd.readouterr().err
    assert (home / "ran").read_text().splitlines() == ["tick"]
    poll(home, command)
    assert (home / "ran").read_text().splitlines() == ["tick"]


def test_scheduler_shorter_interval_applies_on_start(home, tmp_path):
    (home / "state/tick.next").write_text(str(int(time.time()) + 21600))
    poll(home, fake_tick(tmp_path, 'echo tick >> "$FISCALM_HOME/ran"'))
    assert int((home / "state/tick.next").read_text()) <= int(time.time()) + 5
    assert not (home / "ran").exists()


def run_scheduler(home, tick):
    """`fiscalm scheduler` with another tick command, in its own process so it can be stopped like a container."""
    code = f"import signal, sys\nfrom fiscalm import scheduler\nsignal.signal(signal.SIGTERM, scheduler._stopped)\nscheduler.ticks(5, {tick!r})"
    (home / "state/tick.request").touch()
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not (home / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (home / "ready").exists()
        p.terminate()
        assert p.wait(timeout=15) == 0
        deadline = time.monotonic() + 5
        while not (home / "stopped").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (home / "stopped").exists()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


def test_scheduler_forwards_termination(home, tmp_path):
    body = 'trap \'echo stopped > "$FISCALM_HOME/stopped"; exit 0\' TERM\necho ready > "$FISCALM_HOME/ready"\nwhile true; do sleep 0.1; done'
    run_scheduler(home, fake_tick(tmp_path, body))


def test_scheduler_stops_the_running_step_of_a_real_tick(home, tmp_path):
    fiscalm = tmp_path / "fiscalm"
    fiscalm.write_text(f"""#!{sys.executable}
import os, signal, sys, time
from pathlib import Path
if sys.argv[1] != 'sync':
    raise SystemExit(0)
root = Path(os.environ['FISCALM_HOME'])
def stop(*args):
    (root / 'stopped').touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
(root / 'ready').touch()
while True:
    time.sleep(0.1)
""")
    fiscalm.chmod(0o700)
    tick = f"import signal, sys\nfrom fiscalm import scheduler\nsignal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\nsys.exit(scheduler.tick(fiscalm=[{str(fiscalm)!r}]))"
    run_scheduler(home, [sys.executable, "-c", tick])


def test_backup_loop_finishes_a_running_backup_and_survives_a_failure(home, monkeypatch, capsys):
    made = []

    def scheduled(keep):
        made.append(keep)
        if len(made) == 1:
            raise OSError("synthetic")
        os.kill(os.getpid(), signal.SIGTERM)  # a stop during a backup waits for it
        return home / "backups/one.zip"

    monkeypatch.setattr(scheduler.backup, "scheduled", scheduled)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        assert scheduler.backups(60, 3, sleep=lambda seconds: None) == 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    assert made == [3, 3]
    out = capsys.readouterr()
    assert "Scheduled backup failed." in out.err and "one.zip" in out.out


def test_docker_build_and_shutdown_contract():
    root = Path(__file__).parents[1]
    docker = (root / "Dockerfile").read_text()
    assert "COPY tests/synthetic_world.py tests/pdf_fake.py tests/firms.py tests/ubl_fake.py ./tests/" in docker
    assert docker.index("COPY docs/openapi.json ./docs/openapi.json") < docker.index("npm run build")
    assert docker.index("COPY app/package*.json") < docker.index("npm ci") < docker.index("COPY app ./app")
    assert "uv sync --frozen --no-dev --no-cache" in docker
    assert "FISCALM_BACKUPS: /data/backups" in (root / "compose.yaml").read_text()
    assert "chown fiscalm:fiscalm /data /secrets" in docker
    assert "stop_grace_period: 60s" in (root / "compose.yaml").read_text()


def test_runtime_image_includes_pdf_statement_tools():
    docker = (Path(__file__).parents[1] / "Dockerfile").read_text()
    runtime = docker.split("FROM python:3.11-slim-bookworm", 1)[1]
    # Statement parsing and receipt previews execute pdftotext/pdftoppm at runtime.
    assert "poppler-utils" in runtime.split("WORKDIR", 1)[0]


def test_image_installs_ai_clis_without_copying_their_logins():
    root = Path(__file__).parents[1]
    docker = (root / "Dockerfile").read_text()
    assert "COPY deploy/ai-cli/package*.json" in docker and "npm ci --omit=dev" in docker
    assert "COPY --from=ai-clis /opt/fiscalm-ai" in docker
    assert "FISCALM_AI_CLI" not in docker + (root / "compose.yaml").read_text()
    assert {"**/.claude", "**/.codex", "**/.claude.json"} <= set((root / ".dockerignore").read_text().splitlines())


def test_cli_home_is_separate_and_not_mounted_by_backup():
    root = Path(__file__).parents[1]
    plugin = shutil.which("docker") and subprocess.run(
        ["docker", "compose", "version"], capture_output=True, timeout=10
    )
    if not plugin or plugin.returncode != 0:
        pytest.skip("Docker Compose plugin is not installed")
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"], cwd=root, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    for name in ("web", "scheduler"):
        volumes = {v["target"]: v for v in config["services"][name]["volumes"]}
        assert volumes["/home/fiscalm"]["source"] == "ai-cli-home"
        assert volumes["/home/fiscalm"]["type"] == "volume"
    assert "/home/fiscalm" not in {v["target"] for v in config["services"]["backup"]["volumes"]}


def test_app_fixture_helpers_do_not_import_test_dependencies():
    root = Path(__file__).parent
    for name in ("synthetic_world", "ubl_fake", "pdf_fake", "firms"):
        tree = ast.parse((root / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                assert not any(part == "pytest" or part.startswith("test_") for part in module.split(".")), (
                    f"{name}:{node.lineno} imports test dependency {module}"
                )
