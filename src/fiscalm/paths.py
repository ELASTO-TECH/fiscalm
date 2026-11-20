"""Where an installation keeps its data and secrets; the Docker image sets both to its volumes."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(variable: str, fallback: str) -> Path:
    """An XDG base directory; a relative or empty value is invalid by the specification and ignored."""
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else Path.home() / fallback


def finance_home() -> Path:
    """$FISCALM_HOME, else ${XDG_DATA_HOME:-~/.local/share}/fiscalm."""
    return Path(os.environ.get("FISCALM_HOME") or _xdg("XDG_DATA_HOME", ".local/share") / "fiscalm").expanduser()


def secrets_dir() -> Path:
    """$FISCALM_SECRETS, else ${XDG_CONFIG_HOME:-~/.config}/fiscalm/secrets."""
    return Path(
        os.environ.get("FISCALM_SECRETS") or _xdg("XDG_CONFIG_HOME", ".config") / "fiscalm" / "secrets"
    ).expanduser()
