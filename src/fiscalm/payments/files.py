"""Payment file formats: the bulk-payment file a bank's app imports, which fiscalm prepares and a person imports and
signs in their bank. fiscalm never sends a payment (AGENTS.md); a format only turns payment rows into a file.

Which format a payment uses comes from the account it debits: the bank's catalogue row names it
(banks.Bank.payment_file). A bank without one gets no file; its payments are held and said so, never written in
another bank's format. Today there is one format, BT Go's CSV. To add one, see docs/payment-files.md: a module with
`build`, `check` and `value_date`, a row in FORMATS, and its id on the bank's catalogue row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from fiscalm.bank import catalogue as banks
from fiscalm.companies import Account
from fiscalm.payments import btgo
from fiscalm.payments.orders import PaymentFileError, PaymentItem


@dataclass(frozen=True)
class PaymentFormat:
    id: str  # what the bank's catalogue row names (banks.Bank.payment_file); never shown
    app: str  # where people import the file, as they know it: shown in fiscalm ("importă-l în BT Go")
    extension: str  # of the file people download, with its dot
    currencies: tuple[str, ...]  # the accounts it can debit
    build: Callable[[list[PaymentItem], date], bytes]  # one company's rows, dated; refuses anything unsure
    check: Callable[[bytes, str], list[str]]  # what's wrong with a file of this format for a company ([] = fine)
    value_date: Callable[[bytes], date | None]  # the date a file it built pays on


FORMATS: dict[str, PaymentFormat] = {
    "btgo-csv": PaymentFormat("btgo-csv", "BT Go", ".csv", ("RON",), btgo.build, btgo.check, btgo.value_date),
}
assert all(b.payment_file in FORMATS for b in banks.CATALOGUE if b.payment_file), "a bank names an unknown format"
EXTENSIONS = tuple(sorted({f.extension for f in FORMATS.values()}))


def for_account(account: Account | None) -> PaymentFormat | None:
    """The format a payment from `account` is prepared in, or None: no account, a bank outside the catalogue or
    without a format, or a currency its format can't debit."""
    bank = banks.BY_ID.get(account.bank) if account else None
    fmt = FORMATS.get(bank.payment_file) if bank else None
    return fmt if fmt and account.currency in fmt.currencies else None


def why_not(account: Account | None) -> str:
    """Why `account` gets no payment file, for the CLI and logs (for_account returned None)."""
    if not account:
        return "no paying account: add the company's account payments are made from"
    bank = banks.BY_ID.get(account.bank)
    if not bank or not bank.payment_file:
        return (
            f"no payment file format for the paying account's bank ({bank.name if bank else 'not in the catalogue'}): "
            "pay from the bank's app, or add a format (docs/payment-files.md)"
        )
    return f"the {FORMATS[bank.payment_file].app} file can't debit a {account.currency} account"


__all__ = ["FORMATS", "EXTENSIONS", "PaymentFormat", "PaymentFileError", "for_account", "why_not"]
