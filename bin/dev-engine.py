"""Loopback-only development engine with invented firms and no live marker."""

from __future__ import annotations

import http.client
import io
import ipaddress
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from anaf_fake import public_registry
from dev_sandbox import serve_engine
from firms import seed

from fiscalm.api import user_cli
from fiscalm.efactura import public as anaf_public
from fiscalm.ledger import Ledger


def snapshot(source: Path) -> dict[str, int]:
    files = {}
    for path in source.rglob("*.py"):
        try:
            if not path.is_symlink():
                files[str(path)] = path.stat().st_mtime_ns
        except OSError:
            continue  # Editors can replace a file between listing and stat.
    return files


def watch() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "fiscalm"
    process = None
    previous = None
    try:
        while True:
            current = snapshot(source)
            if current != previous:
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait()
                process = subprocess.Popen([sys.executable, __file__, "--serve"])
                previous = current
                reported = False
            if process.poll() is not None and not reported:
                print("Engine stopped; waiting for the next Python edit.", file=sys.stderr)
                reported = True
            time.sleep(0.5)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()


def require_isolation() -> None:
    value = os.environ.get("FISCALM_DEV_ROOT", "")
    marker = Path(value) / ".fiscalm-dev"  # written by bin/dev into the directory it created
    if not value or marker.is_symlink() or not marker.is_file():
        raise SystemExit("Start this helper through bin/dev engine.")
    root = Path(value).resolve()
    for key, suffix in (
        ("FISCALM_HOME", "home"),
        ("FISCALM_SECRETS", "secrets"),
        ("FISCALM_BACKUPS", "backups"),
    ):
        if Path(os.environ.get(key, "")).resolve() != root / suffix:
            raise SystemExit("Development paths must stay inside the bin/dev engine directory.")
    if (root / "home/state/LIVE").exists():
        raise SystemExit("Development requires shadow mode.")


def _anaf(host: object) -> bool:
    name = (host.decode(errors="replace") if isinstance(host, bytes) else str(host or "")).lower().rstrip(".")
    return name == "anaf.ro" or name.endswith(".anaf.ro")


def offline_anaf() -> None:
    """Answer ANAF's public lookup from an invented registry and refuse every other way to ANAF: at name resolution,
    so redirects and other openers are refused too, and in proxy tunnels; proxies are switched off."""
    urlopen, getaddrinfo, set_tunnel = urllib.request.urlopen, socket.getaddrinfo, http.client.HTTPConnection.set_tunnel

    def answer(request: urllib.request.Request | str, *args: object, **kwargs: object) -> object:
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if url == anaf_public.URL and isinstance(request, urllib.request.Request):
            return io.BytesIO(public_registry(url, request.data))
        return urlopen(request, *args, **kwargs)

    def resolve(host: object, *args: object, **kwargs: object) -> object:
        if _anaf(host):
            raise OSError("The development engine never calls ANAF.")
        return getaddrinfo(host, *args, **kwargs)

    def tunnel(self: http.client.HTTPConnection, host: str, *args: object, **kwargs: object) -> None:
        if _anaf(host.rsplit(":", 1)[0] if host.count(":") == 1 else host):
            raise OSError("The development engine never calls ANAF.")
        set_tunnel(self, host, *args, **kwargs)

    # No proxy: a forwarding proxy would resolve ANAF itself, out of reach of the guard above.
    for name in [n for n in os.environ if n.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        del os.environ[name]
    urllib.request.getproxies = dict
    urllib.request.urlopen, socket.getaddrinfo, http.client.HTTPConnection.set_tunnel = answer, resolve, tunnel


def _loopback(host: object) -> bool:
    name = (host.decode(errors="replace") if isinstance(host, bytes) else str(host or "")).lower().rstrip(".")
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return name == "localhost"


def offline_network() -> None:
    """bin/verify: nothing this process sends leaves the machine (mail tests and their DNS, bank APIs, updates).
    An audit hook sees every Python socket call, TCP or UDP, and cannot be removed; programs the engine starts are
    replaced by refusing stubs on PATH (bin/verify)."""

    def refuse(event: str, args: tuple) -> None:
        if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            outside = not _loopback(args[0])
        elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            sock, address = args[0], args[1]
            outside = sock.family in (socket.AF_INET, socket.AF_INET6) and bool(address) and not _loopback(address[0])
        else:
            return
        if outside:
            raise OSError("bin/verify runs offline: loopback only.")

    sys.addaudithook(refuse)


def main() -> None:
    require_isolation()
    offline_anaf()
    if os.environ.get("FISCALM_DEV_OFFLINE") == "1":
        offline_network()
    port = int(os.environ.get("FISCALM_DEV_ENGINE_PORT", "8765"))  # bin/verify gives each worktree its own
    if sys.argv[1:] == ["--serve"]:
        serve_engine("127.0.0.1", port)
        return
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))  # Refuse an occupied port before starting the watcher.
    first_run = not (Path(os.environ["FISCALM_HOME"]) / "state/ledger.db").exists()
    with closing(Ledger()) as ledger, ledger.tx():
        if first_run:  # later runs keep the firms as the developer left them
            seed(ledger)
        _, link = user_cli.bootstrap(ledger, datetime.now(UTC))
    if link:
        user_cli.print_link(link, os.environ["FISCALM_PUBLIC_URL"])
    watch()


if __name__ == "__main__":
    main()
