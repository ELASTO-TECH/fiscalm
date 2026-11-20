"""HTTP entry point shared by the two Sandbox-only development helpers."""

from __future__ import annotations

from dataclasses import replace
from functools import partial

from fiscalm import cli
from fiscalm.api import actions


def serve_engine(host: str | None = None, port: int | None = None) -> int:
    spec = actions.ACTIONS["mode-switch"]
    actions.ACTIONS["mode-switch"] = replace(spec, handler=partial(actions._mode_switch, allow_live=False))
    args = ["serve"]
    if host is not None:
        args.extend(["--host", host])
    if port is not None:
        args.extend(["--port", str(port)])
    return cli.main(args)


if __name__ == "__main__":
    raise SystemExit(serve_engine())
