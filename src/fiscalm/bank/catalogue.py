"""The banks fiscalm knows: how to recognise one from an IBAN and what each can offer (a live connection, statement
formats, the sender of its statement mails, a payment file).

Data, not rules: adding a bank is a new row here, plus a parser registered under a format id if it has its own
statement layout. A bank that isn't here can still be used through CAMT statements; it just has no catalogue row.
See docs/self-hosting.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Wording:
    """How a bank names its own lines in the text of its statements (its PDF, its CSV export), for the parsers of those
    formats; formats with codes (CAMT, MT940, open banking) say the same with codes. Upper or lower case as printed."""

    fee_titles: tuple[str, ...] = ()  # a line whose title starts with one is the bank's fee or package
    fee_phrases: tuple[str, ...] = ()  # ...or whose details hold one (compared upper case)
    interest_titles: tuple[str, ...] = ()  # interest the bank pays or charges
    own_terminal_titles: tuple[str, ...] = ()  # a card payment at the bank's own terminal, so in the bank's country
    other_terminal_marks: tuple[str, ...] = ()  # ...unless its title holds one of these (another bank's terminal)


@dataclass(frozen=True)
class Bank:
    id: str  # what fiscalm stores (integrations, config); never shown
    name: str  # what people read
    country: str  # ISO 3166 alpha-2, the IBAN's first two letters
    iban_codes: tuple[str, ...]  # the IBAN's bank code (characters 5 to 8)
    open_banking: tuple[tuple[str, str], ...] = ()  # (provider, the provider's name for this bank)
    statement_formats: tuple[str, ...] = ("camt053",)
    statement_senders: tuple[str, ...] = ()  # addresses its statement mails come from
    payment_file: str = ""  # its payment file format (payfiles.FORMATS id); "" when none: its payments are held
    short: str = ""  # how people abbreviate it, e.g. in the accountant's mail; the name when empty
    wording: Wording = Wording()  # its own lines' names in its PDF and CSV statements

    @property
    def label(self) -> str:
        return self.short or self.name

    @property
    def reads_pdf(self) -> bool:
        return any(f.endswith("-pdf") for f in self.statement_formats)

    def provider_name(self, provider: str) -> str | None:
        """The bank's name at an open banking provider, or None when that provider can't reach it."""
        return dict(self.open_banking).get(provider)


# The State Treasury's BIC, the same for every Romanian budget account (IBAN bank code TREZ): a national constant.
TREASURY_BIC = "TREZROBU"

_EB = "enablebanking"

CATALOGUE: tuple[Bank, ...] = (
    Bank(
        "bt",
        "Banca Transilvania",
        "RO",
        ("BTRL",),
        ((_EB, "Banca Transilvania"),),
        ("bt-allmerge-pdf", "camt053"),
        ("EmailExtrase@btrl.ro",),
        "btgo-csv",
        "BT",
        Wording(
            fee_titles=("Pachet", "Comision"),
            fee_phrases=("COMISION PLATA",),
            own_terminal_titles=("Plata la POS",),
            other_terminal_marks=("non-BT",),
        ),
    ),
    Bank("bcr", "BCR", "RO", ("RNCB",), ((_EB, "Banca Comerciala Romana"),)),
    Bank("brd", "BRD", "RO", ("BRDE",), ((_EB, "BRD"),)),
    Bank("ing", "ING Bank", "RO", ("INGB",), ((_EB, "ING"),)),
    Bank("raiffeisen", "Raiffeisen Bank", "RO", ("RZBR",), ((_EB, "Raiffeisen"),)),
    Bank("unicredit", "UniCredit Bank", "RO", ("BACX",), ((_EB, "UniCredit Bank"),)),
    Bank("cec", "CEC Bank", "RO", ("CECE",), ((_EB, "CEC Bank"),)),
    Bank("libra", "Libra Internet Bank", "RO", ("BREL",), ((_EB, "Libra Internet Bank"),)),
    Bank("otp", "OTP Bank", "RO", ("OTPV",)),
    # BRMA: the former Banca Românească's code, which Exim's accounts carry; EXIM: Eximbank's own
    Bank("exim", "Exim Banca Românească", "RO", ("BRMA", "EXIM"), ((_EB, "Exim Banca Românească"),)),
    Bank("revolut", "Revolut", "RO", ("REVO",), ((_EB, "Revolut"),)),
)

BY_ID: dict[str, Bank] = {b.id: b for b in CATALOGUE}
assert len(BY_ID) == len(CATALOGUE), "bank ids must be unique"
_BY_CODE: dict[tuple[str, str], Bank] = {(b.country, code): b for b in CATALOGUE for code in b.iban_codes}
assert len(_BY_CODE) == sum(len(b.iban_codes) for b in CATALOGUE), "an IBAN bank code belongs to one bank"


def for_iban(iban: str) -> Bank | None:
    """The catalogue bank an IBAN belongs to, from its country and bank code; None when it isn't in the catalogue.
    Only reads the code: it says which bank, not that the IBAN is valid (validate.iban_valid does that)."""
    s = re.sub(r"\s+", "", iban or "").upper()
    if len(s) < 8 or not s[:2].isalpha():
        return None
    return _BY_CODE.get((s[:2], s[4:8]))
