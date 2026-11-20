"""Relay official CLI login prompts, never their credentials or raw output."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

from fiscalm import ai

TIMEOUT = 180
DOMAINS = {"claude-cli": ("anthropic.com", "claude.ai", "claude.com"), "codex-cli": ("openai.com", "chatgpt.com")}


def login_url(value: str, provider: str) -> bool:
    try:
        u = urlsplit(value)
        return bool(
            u.scheme == "https"
            and u.hostname
            and not u.username
            and not u.password
            and u.port in (None, 443)
            and not u.fragment
            and any(u.hostname == d or u.hostname.endswith("." + d) for d in DOMAINS[provider])
            and not any("token" in k.lower() for k in parse_qs(u.query))
            and len(value) <= 2048
        )
    except (ValueError, KeyError):
        return False


class Login:
    """One per HTTP server. A deadline kills the whole child group, including a stalled CLI."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.child: subprocess.Popen | None = None
        self.timer: threading.Timer | None = None
        self.provider = ""
        self.state = "idle"
        self.url = ""
        self.code = ""
        self.needs_code = False

    def snapshot(self) -> dict:
        with self.lock:
            return dict(
                provider=self.provider, state=self.state, url=self.url, code=self.code, needs_code=self.needs_code
            )

    def _stop(self, state: str) -> None:
        with self.lock:
            if self.timer:
                self.timer.cancel()
            if self.child and self.child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=3)
            self.state, self.url, self.code, self.needs_code = state, "", "", False

    def cancel(self) -> None:
        self._stop("cancelled")

    def start(self, provider: str) -> dict:
        if provider not in DOMAINS:
            raise ValueError("invalid_provider")
        with self.lock:
            if self.child and self.child.poll() is None:
                raise ValueError("login_busy")
            exe = os.environ.get("FISCALM_CLAUDE", "claude") if provider == "claude-cli" else "codex"
            args = [exe, "auth", "login", "--claudeai"] if provider == "claude-cli" else [exe, "login", "--device-auth"]
            try:
                self.child = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    bufsize=0,
                )
            except OSError:
                raise ValueError("cli_missing") from None
            if self.timer:
                self.timer.cancel()
            self.provider, self.state, self.url, self.code, self.needs_code = provider, "waiting", "", "", False
            child = self.child
            self.timer = threading.Timer(TIMEOUT, self._expire, (child,))
            self.timer.daemon = True
            self.timer.start()
            threading.Thread(target=self._read, args=(child, provider), daemon=True).start()
            return self.snapshot()

    def _expire(self, child: subprocess.Popen) -> None:
        with self.lock:
            if child is self.child and self.state == "waiting":
                self._stop("expired")

    def _prompt(self, text: str, provider: str) -> None:
        # A terminal hyperlink (OSC 8) keeps its target, whatever text it shows; other escapes go.
        text = re.sub(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)", r" \1 ", text)
        text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;]*[A-Za-z]", "", text)
        for value in re.findall(r"https?://[^\s<>\x1b]+(?=\s)", text):
            value = value.rstrip(".,);'")
            if not login_url(value, provider):
                self._stop("invalid_url")
                return
            self.url = value
        match = re.search(
            r"(?i:(?:device|one[- ]time|your)\s+code)\b[^\n]{0,100}?\s*(?<![A-Za-z0-9_-])([A-Z0-9]{4}-[A-Z0-9]{4,5})(?![A-Za-z0-9_-])",
            text,
        )
        if match:
            self.code = match[1]
        self.needs_code = bool(re.search(r"paste[^\n]{0,60}code", text, re.I))

    def _read(self, child: subprocess.Popen, provider: str) -> None:
        pending, total = "", 0
        try:
            while chunk := child.stdout.read(512):
                total += len(chunk)
                with self.lock:
                    if child is not self.child or self.state != "waiting":
                        break
                    if total > 65536:
                        self._stop("failed")
                        break
                    pending = (pending + chunk.decode("utf-8", errors="replace"))[-8192:]
                    self._prompt(pending, provider)
            child.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            with self.lock:
                if child is self.child and self.state == "waiting":
                    self._stop("failed")
        finally:
            with self.lock:
                if child is self.child and self.state == "waiting":
                    self._stop(
                        "connected" if child.returncode == 0 and ai.cli_status(provider) == "ready" else "failed"
                    )
            for stream in (child.stdin, child.stdout):
                if stream:
                    stream.close()

    def submit(self, code: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._~+/#=-]{1,2048}", code):
            raise ValueError("invalid_code")
        with self.lock:
            if not self.child or self.child.poll() is not None or not self.needs_code or self.state != "waiting":
                raise ValueError("login_not_waiting")
            try:
                self.child.stdin.write((code + "\n").encode())
                self.child.stdin.flush()
            except OSError:
                raise ValueError("login_failed") from None

    def logout(self, provider: str) -> None:
        if provider not in DOMAINS:
            raise ValueError("invalid_provider")
        with self.lock:
            if self.child and self.child.poll() is None:
                raise ValueError("login_busy")
            args = (
                [os.environ.get("FISCALM_CLAUDE", "claude"), "auth", "logout"]
                if provider == "claude-cli"
                else ["codex", "logout"]
            )
            try:
                result = ai.run_cli(args, timeout=3)
                if result.returncode:
                    raise ValueError("logout_failed")
            except (OSError, subprocess.SubprocessError):
                raise ValueError("logout_failed") from None
            self._stop("idle")
