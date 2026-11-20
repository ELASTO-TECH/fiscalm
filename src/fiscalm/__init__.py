"""fiscalm's engine: bookkeeping for small Romanian companies (invoices, bank, accountant)."""

from importlib.metadata import version

__version__ = version("fiscalm")
# The product's name as users see it (titles, the PWA manifest, messages). Decided 2026-11-02.
PRODUCT = "fiscalm"


def main() -> None:
    import sys

    args = sys.argv[1:]
    if args == ["--version"]:
        print(f"{PRODUCT} {__version__}")
        return
    from fiscalm.setup import apply_home, split_home

    home, _ = split_home(args)  # a repeated --home stops here, before anything is opened
    if home:  # before the CLI's imports, so none of them sees the default home
        apply_home(home)
    from fiscalm.cli import main as cli_main

    raise SystemExit(cli_main())
