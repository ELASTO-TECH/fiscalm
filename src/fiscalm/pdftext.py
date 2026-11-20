"""pdftotext wrapper plus small helpers shared by parsers."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

PDFTOTEXT = shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
TEXT_TIMEOUT = 30
MAX_TEXT = 8 * 1024 * 1024
# Set limits in a fresh interpreter, avoiding preexec_fn in the threaded HTTP server.
_TEXT_LIMITS = """import os, resource, signal, sys
for kind, limit in ((resource.RLIMIT_AS, 512 * 1024 * 1024),
                    (resource.RLIMIT_CPU, 30), (resource.RLIMIT_FSIZE, int(sys.argv[1])),
                    (resource.RLIMIT_CORE, 0)):
    resource.setrlimit(kind, (limit, limit))
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
os.execv(sys.argv[2], sys.argv[2:])
"""


def pdf_text(path: str | Path | bytes, layout: bool = True) -> str:
    """Extract bounded UTF-8 text from a path or verified PDF bytes; refuse without leaking parser diagnostics."""
    args = [PDFTOTEXT, "-enc", "UTF-8"]
    if layout:
        args.append("-layout")
    try:
        args += ["-" if isinstance(path, bytes) else str(Path(path).resolve()), "-"]
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            subprocess.run(
                [sys.executable, "-I", "-c", _TEXT_LIMITS, str(MAX_TEXT + 1), *args],
                input=path if isinstance(path, bytes) else None,
                stdout=out,
                stderr=err,
                timeout=TEXT_TIMEOUT,
                check=True,
            )
            out.seek(0)
            data = out.read(MAX_TEXT + 1)
            if len(data) > MAX_TEXT or err.seek(0, 2) > MAX_TEXT:
                raise ValueError("output limit")
            return data.decode("utf-8")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise RuntimeError("PDF text extraction failed or exceeded its safety limits.") from None


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_dmy(s: str) -> date:
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a dd/mm/yyyy date: {s!r}")


def parse_amount(s: str) -> Decimal:
    """Parse '1,174.22' / '1 681' / '350.00' / '-4.80' (BT and OP style: ',' thousands)."""
    t = s.strip().replace(" ", "").replace(",", "")
    return Decimal(t)


def _preview_limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))


def first_page_png(data: bytes, dpi: int = 90) -> bytes | None:
    """The first page of a PDF as a PNG (pdftoppm), for previews; None if it can't be rendered."""
    tool = shutil.which("pdftoppm")
    if not tool:
        return None
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.pdf"
        src.write_bytes(data)
        # -scale-to caps the picture's longest side: a tiny PDF can ask for a huge page.
        try:
            proc = subprocess.run(
                [
                    tool,
                    "-png",
                    "-r",
                    str(dpi),
                    "-scale-to",
                    "2000",
                    "-f",
                    "1",
                    "-l",
                    "1",
                    "-singlefile",
                    str(src),
                    str(Path(d) / "out"),
                ],
                capture_output=True,
                timeout=30,
                preexec_fn=_preview_limits,
            )
        except subprocess.TimeoutExpired:
            return None
        out = Path(d) / "out.png"
        return out.read_bytes() if proc.returncode == 0 and out.exists() else None
