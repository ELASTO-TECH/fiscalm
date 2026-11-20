"""Scheduled work: the tick pipeline (`fiscalm tick`) and the loops that run ticks and backups in Docker.

`fiscalm tick` runs each step as its own `fiscalm` process with a time limit, so a hung or crashing step cannot hold the
tick lock or stop the steps after it. All state is under FISCALM_HOME; shadow mode is enforced by each step, which
blocks outward effects. `fiscalm scheduler` runs a tick every FISCALM_TICK_INTERVAL seconds and whenever the app
asks for one (`state/tick.request`); `fiscalm scheduler --backup` makes a backup every FISCALM_BACKUP_INTERVAL.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO

from fiscalm import backup
from fiscalm.paths import finance_home

FISCALM = (sys.executable, "-m", "fiscalm")
TIMED_OUT = 124  # the exit code timeout(1) uses, kept for the log lines people already read
POLL = 5
STOP_GRACE = 10.0  # seconds a stopped step's process group gets after SIGTERM, before SIGKILL
# Steps whose failure is logged and the tick goes on: (seconds, arguments, what the log calls the step).
RELEASE_AND_PAYMENTS = ((10, ("update-check",), "release check"), (120, ("pay", "redate"), "pay redate"))
EFACTURA = ((600, ("efactura", "sync"), "efactura sync"), (600, ("efactura", "sync-issued"), "efactura sync-issued"))
# Only after every matcher ran (sync, EFACTURA): a rule explains what they left, never what a failed one would take.
RULES = ((120, ("tx-rules",), "saved rules"),)
SHOPS_AND_UPLOADS = (
    (600, ("shopify-sync",), "Shopify sync"),
    (600, ("invoice", "tick"), "invoice tick"),
    (120, ("emag", "tick"), "eMAG tick"),
    (120, ("invoice", "partner-status"), "partner status refresh"),
)


def _write(log: BinaryIO, line: str) -> None:
    log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n".encode())


@dataclass(frozen=True)
class Run:
    """One tick: the command that runs steps, the log they write to and the descriptor holding the tick lock."""

    fiscalm: list[str]
    log: BinaryIO
    lock: int


def _group_alive(pgid: int) -> bool:
    """Whether our process group still exists; one that is gone or now belongs to someone else is not ours."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


@contextlib.contextmanager
def _stop_signals_deferred():
    """A second SIGTERM (or SIGINT) while stopping waits until the cleanup is finished, instead of cutting it short."""
    deferred = {signal.SIGTERM, signal.SIGINT}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, deferred)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _stop(child: subprocess.Popen) -> None:
    """Stop the step's whole process group, also when its leader exits first: TERM, then KILL after STOP_GRACE.
    Returns once the group is gone (bounded: a process that survives SIGKILL is left to the container's init)."""
    with _stop_signals_deferred():
        for sig, grace in ((signal.SIGTERM, STOP_GRACE), (signal.SIGKILL, 5.0)):
            _signal_group(child.pid, sig)
            deadline = time.monotonic() + grace
            while child.poll() is None or _group_alive(child.pid):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            else:
                return


def _run(run: Run, args: tuple[str, ...], seconds: float, stdout: int | None = None) -> tuple[int, str]:
    """Run one step in its own process group; its stderr (and stdout unless captured) goes to the log. The step
    inherits the tick lock, so the lock stays held while any step survives a killed tick."""
    command = [*run.fiscalm, *args]
    child = subprocess.Popen(command, stdout=stdout or run.log, stderr=run.log, process_group=0, pass_fds=(run.lock,))
    try:
        out, _ = child.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        _stop(child)
        return TIMED_OUT, ""
    except BaseException:  # stopped (SIGTERM from the scheduler): the step goes too
        _stop(child)
        raise
    return child.returncode, (out or b"").decode(errors="replace")


def _sync_summary(out: str) -> str:
    d = json.loads(out)
    acts = "; ".join(d["actions_needed"]) or "none"
    read = sum(a.get("read", 0) for a in d["mail"]["accounts"])
    return f"sync ({d['mode']}): {read} mail(s) read, {len(d['drive_archive'])} Drive change(s); actions: {acts}"


def _judge_summary(out: str) -> str:
    d = json.loads(out)
    counts = f"{d['invoices']} invoice(s) read, {len(d['suggestions'])} suggested for review"
    return "efactura judge: " + (d.get("error") or counts)


def _summarised(run: Run, args: tuple[str, ...], seconds: int, summary: Callable) -> bool:
    """Run a step and log its summary; True when it exited 0."""
    rc, out = _run(run, args, seconds, subprocess.PIPE)
    try:
        _write(run.log, summary(out))
    except (ValueError, KeyError, TypeError, AttributeError):
        _write(run.log, f"{' '.join(args)} failed (rc={rc})")
    return rc == 0


def _steps(run: Run, steps: tuple) -> bool:
    """Run each step, logging a failure and going on; True when every step exited 0."""
    ok = True
    for seconds, args, label in steps:
        rc, _ = _run(run, args, seconds)
        if rc:
            _write(run.log, f"{label} failed (rc={rc})")
            ok = False
    return ok


def _pipeline(run: Run, home: Path, today: date, check: float) -> int:
    # Accounts work before a company exists. Do not create a ledger just to clean up.
    if (home / "state" / "ledger.db").exists():
        _steps(run, ((60, ("auth", "sweep"), "authentication cleanup"),))
    # Only an explicitly empty company config skips the pipeline; a failed or hung check stops it.
    rc, _ = _run(run, ("company", "check"), check, subprocess.DEVNULL)
    if rc == 3:
        _write(run.log, "No companies configured; skipping tick.")
        return 0
    if rc:
        _write(run.log, f"company check failed (rc={rc})")
        return rc
    # Anonymous release reads are allowed in shadow; the company gate still owns startup.
    _steps(run, RELEASE_AND_PAYMENTS)
    # Mail, statements, payment files, reports (read-only and local in shadow; Drive only live).
    matched = _summarised(run, ("sync",), 900, _sync_summary)
    # ANAF received and issued invoices (read-only toward ANAF), then the AI reading of payment mails.
    if _steps(run, EFACTURA) and matched:
        _steps(run, RULES)
    else:
        _write(run.log, "saved rules skipped: a matcher step failed")
    _summarised(run, ("efactura", "judge"), 900, _judge_summary)
    # Recurrences issue locally even in shadow; shop reads import drafts; uploads and write-backs need live.
    recur = ((60, ("invoicing", "recur", "run", "--today", today.isoformat()), "invoice recurrence run"),)
    _steps(run, (*recur, *SHOPS_AND_UPLOADS))
    return 0


def _seconds(name: str, default: float) -> float:
    """A positive number of seconds, such as 15 or 0.5; nothing else (no 1m)."""
    value = os.environ.get(name) or str(default)
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", value) or float(value) <= 0:
        raise SystemExit(f"{name} must be a positive number of seconds.")
    return float(value)


def tick(home: Path | None = None, fiscalm: list[str] | None = None, today: date | None = None) -> int:
    """One scheduled run; returns the process exit code. A second tick while one runs only logs and leaves."""
    check = _seconds("FISCALM_COMPANY_CHECK_TIMEOUT", 15)
    home = home or finance_home()
    backup.refuse_unfinished_restore(home)
    for d in ("logs", "state"):
        (home / d).mkdir(parents=True, exist_ok=True, mode=0o700)
    today = today or date.today()
    with (
        open(home / "logs" / f"finance-{today:%Y-%m}.log", "ab", buffering=0) as log,
        open(home / "state" / "tick.lock", "a") as lock,
    ):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _write(log, "already running, skip")
            return 0
        return _pipeline(Run(list(fiscalm or FISCALM), log, lock.fileno()), home, today, check)


def _due(state: Path, every: int, now: int) -> int:
    """The persisted next run; a shorter interval than the one it was planned with applies at once."""
    try:
        planned = int((state / "tick.next").read_text())
    except (OSError, ValueError):
        planned = 0
    if planned <= 0 or planned > now + every:
        planned = now + every
        (state / "tick.next").write_text(str(planned))
    return planned


def _child(command: list[str]) -> int:
    child = subprocess.Popen(command, start_new_session=True)
    try:
        return child.wait()
    except BaseException:  # stopped: the running tick and its steps stop first
        with _stop_signals_deferred():
            _signal_group(child.pid, signal.SIGTERM)
            child.wait()
        raise


def _requested(state: Path) -> bool:
    """A manual check asked for a tick: a regular file at state/tick.request, consumed here. Anything else at that
    name (a link, a FIFO, a directory) is removed without acting on it, never followed."""
    request = state / "tick.request"
    try:
        mode = os.lstat(request).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(mode):
        shutil.rmtree(request, ignore_errors=True)
    else:
        request.unlink(missing_ok=True)
    if stat.S_ISREG(mode):
        return True
    print("Removed state/tick.request: not a regular file, no tick started.", file=sys.stderr, flush=True)
    return False


def ticks(every: int, command: list[str], home: Path | None = None, sleep: Callable = time.sleep) -> None:
    """Run `command` when due or requested, forever. The next due time is recorded before the run, so a crash or a
    failed tick does not cause a restart loop."""
    backup.refuse_unfinished_restore(home or finance_home())
    state = (home or finance_home()) / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    due = _due(state, every, int(time.time()))
    while True:
        now = int(time.time())
        if _requested(state) or now >= due:
            due = now + every
            (state / "tick.next.tmp").write_text(str(due))
            (state / "tick.next.tmp").replace(state / "tick.next")
            if _child(command):
                print("Scheduled tick failed; waiting until next due time.", file=sys.stderr, flush=True)
        sleep(POLL)


def backups(every: int, keep: int, sleep: Callable = time.sleep) -> int:
    """A backup now and every `every` seconds. A stop request waits for a running backup, never cuts it short."""
    backup.refuse_unfinished_restore(finance_home())
    stopping, sleeping = [False], [False]

    def stop(*_: object) -> None:
        stopping[0] = True
        if sleeping[0]:
            raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    while True:
        if (finance_home() / "state" / "ledger.db").is_file():
            try:
                print(backup.scheduled(keep), flush=True)
            except Exception:  # noqa: BLE001 - the loop must survive; the message never repeats paths or secrets
                print("Scheduled backup failed.", file=sys.stderr, flush=True)
        sleeping[0] = True
        if stopping[0]:
            return 0
        sleep(every)
        sleeping[0] = False


def _number(name: str, default: int, minimum: int) -> int:
    value = os.environ.get(name) or str(default)
    if not value.isdigit() or int(value) < minimum:
        raise SystemExit(f"{name} must be a whole number, at least {minimum}.")
    return int(value)


def _stopped(*_: object) -> None:
    raise SystemExit(0)


def register(sub: argparse._SubParsersAction) -> None:
    """`tick` and `scheduler`: they need no configured company; each step opens the ledger in its own process."""
    sub.add_parser("tick", help="run the scheduled pipeline once").set_defaults(run=run)
    s = sub.add_parser("scheduler", help="run ticks (or, with --backup, backups) on an interval, in the foreground")
    s.add_argument("--backup", action="store_true")
    s.set_defaults(run=run)


def run(a: argparse.Namespace) -> int:
    os.umask(0o077)
    if a.cmd == "tick":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
        return tick()
    if a.backup:
        every, keep = _number("FISCALM_BACKUP_INTERVAL", 86400, 60), _number("FISCALM_BACKUP_KEEP", 7, 1)
        return backups(every, keep)
    every = _number("FISCALM_TICK_INTERVAL", 21600, 5)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stopped)
    ticks(every, [*FISCALM, "tick"])
    return 0
