"""Bounded reads of untrusted stored files, checked on the opened descriptor."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_file(path: str | Path, roots: list[Path], max_size: int) -> tuple[str, bytes] | None:
    """Read only within already resolved roots, then re-check the opened file before reading."""
    # Callers anchor roots before inspecting any untrusted path. Never resolve them again after a swap.
    try:
        f = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not any(f.is_relative_to(r) for r in roots):
        return None
    try:
        fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        real = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_size or not any(real.is_relative_to(r) for r in roots):
            return None
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read(max_size + 1)
        return (real.name, data) if len(data) <= max_size else None
    except OSError:
        return None
    finally:
        os.close(fd)
