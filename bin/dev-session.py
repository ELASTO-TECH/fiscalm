"""Persistent Sandbox development: fast-forward dev, Next HMR and Python restarts.

Run under a user service with Restart=always and the Sandbox's existing environment.
The loopback proxy belongs behind the installation's tailnet HTTPS listener.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    env = dict(os.environ)
    if home := env.get("FISCALM_DEV_GIT_HOME"):
        env.update(HOME=home, XDG_CONFIG_HOME=str(Path(home) / ".config"))
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, env=env, text=True, stderr=subprocess.PIPE, timeout=20
    ).strip()


def require_sandbox() -> None:
    home = Path(os.environ.get("FISCALM_HOME", "")).resolve()
    if not os.environ.get("FISCALM_HOME"):
        raise SystemExit("Use the separate Sandbox data directory, never the live installation.")
    if (home / "state/LIVE").exists():
        raise SystemExit("Persistent development requires Sandbox mode.")
    if git("branch", "--show-current") != "dev":
        raise SystemExit("Persistent development follows the dev branch only.")
    if git("status", "--porcelain"):
        raise SystemExit("The dev checkout must be clean; make fixes in feature worktrees.")


def update() -> bool:
    if git("status", "--porcelain") or git("branch", "--show-current") != "dev":
        print("Automatic update paused: dev checkout changed locally.", flush=True)
        return False
    git("fetch", "--quiet", "origin", "dev")
    if git("rev-parse", "HEAD") == git("rev-parse", "origin/dev"):
        return False
    git("merge-base", "--is-ancestor", "HEAD", "origin/dev")
    git("merge", "--ff-only", "origin/dev")
    print(f"Development updated to {git('rev-parse', '--short', 'HEAD')}", flush=True)
    return True


def stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        pass


def start(*args: str) -> subprocess.Popen:
    return subprocess.Popen(args, cwd=ROOT, start_new_session=True)


def sources() -> dict[str, int]:
    return {str(p): p.stat().st_mtime_ns for p in (ROOT / "src").rglob("*.py") if p.is_file()}


def run() -> None:
    engine = start(str(ROOT / ".venv/bin/python"), str(ROOT / "bin/dev_sandbox.py"))
    children = [engine]
    try:
        children.append(start("node", "bin/dev-proxy.mjs"))
        children.append(
            start(
                "node",
                "app/node_modules/next/dist/bin/next",
                "dev",
                "app",
                "--hostname",
                "127.0.0.1",
                "--port",
                os.environ["FISCALM_DEV_APP_PORT"],
            )
        )
        previous, fetched = sources(), time.monotonic()
        while True:
            time.sleep(1)
            if (Path(os.environ["FISCALM_HOME"]) / "state/LIVE").exists():
                raise SystemExit("Development stopped: this installation left Sandbox mode.")
            current = sources()
            if current != previous:
                stop(engine)
                children.remove(engine)
                engine = start(str(ROOT / ".venv/bin/python"), str(ROOT / "bin/dev_sandbox.py"))
                children.append(engine)
                previous = current
            if any(p.poll() is not None for p in children):
                raise SystemExit("Development child stopped; the service will restart it.")
            if time.monotonic() - fetched >= 15:
                fetched = time.monotonic()
                try:
                    if update():
                        return  # The service restarts from the newly merged code and dependencies.
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    print(
                        "Automatic update failed; keeping the current version. Check origin/dev and Git access.",
                        flush=True,
                    )
    finally:
        for process in reversed(children):
            stop(process)


def main() -> None:
    os.chdir(ROOT)
    require_sandbox()
    os.environ.update(
        FISCALM_DEV_PERSISTENT="1", FISCALM_TRUST_PROXY="1", FISCALM_SCHEDULER="1", NEXT_PUBLIC_FISCALM_ENGINE="1"
    )
    os.environ.pop("NEXT_PUBLIC_FIXTURES", None)
    os.environ["FISCALM_DEV_ORIGIN"] = os.environ["FISCALM_PUBLIC_URL"]
    os.environ["FISCALM_HOST"] = "127.0.0.1"
    os.environ["FISCALM_PORT"] = os.environ["FISCALM_DEV_ENGINE_PORT"]
    subprocess.run(["uv", "sync", "--locked"], check=True)
    # npm ci only when dependencies changed, not after every Python edit or service restart.
    lock = ROOT / "app/package-lock.json"
    stamp = ROOT / "app/node_modules/.fiscalm-lock"
    if not stamp.exists() or stamp.read_bytes() != lock.read_bytes():
        subprocess.run(["npm", "ci", "--prefix", "app", "--no-audit", "--no-fund"], check=True)
        stamp.write_bytes(lock.read_bytes())
    subprocess.run(["npm", "run", "fixtures"], cwd=ROOT / "app", check=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run()


if __name__ == "__main__":
    main()
