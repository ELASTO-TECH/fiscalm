"""A company's bank accounts as one list, whatever the bank and currency, read from either config shape."""

import pytest

from fiscalm import companies
from fiscalm.config_errors import ConfigError

BT, BT2, ING = "RO37BTRL0000000000000001", "RO10BTRL0000000000000002", "RO00INGB0000000000000003"


def entry(accounts):
    return {"key": "ALFA", "legal_name": "Alfa SRL", "cui": "12345674", "accounts": accounts}


def test_the_accounts_shape_holds_any_bank_and_currency():
    c = companies.company_from(
        entry(
            [
                {"iban": "ro37 btrl 0000 0000 0000 0001", "currency": "RON"},
                {"iban": ING, "currency": "RON", "pays": True},
                {"iban": BT2, "currency": "EUR"},
            ]
        ),
        0,
    )
    assert [(a.iban, a.bank, a.pays) for a in c.accounts] == [(BT, "bt", False), (ING, "ing", True), (BT2, "bt", False)]
    assert c.ron_iban == ING and c.ibans == {"RON": ING, "EUR": BT2}  # the paying account answers for its currency
    assert c.account(" " + BT.lower()).bank == "bt" and c.account("RO00XXXX0000000000000009") is None


def test_a_company_may_have_no_account_yet_and_an_iban_outside_the_catalogue_has_no_bank():
    c = companies.company_from(entry([]), 0)
    assert c.accounts == () and c.ron_iban == "" and c.paying_account is None and c.ibans == {}
    c = companies.company_from(entry([{"iban": "DE00ZZZZ00000000000000", "currency": "EUR"}]), 0)
    assert c.accounts[0].bank == ""


@pytest.mark.parametrize(
    "accounts,why",
    [
        ([{"iban": BT, "currency": "RON"}, {"iban": BT.lower(), "currency": "EUR"}], "twice"),
        ([{"iban": BT, "currency": "RON", "pays": True}, {"iban": ING, "currency": "RON", "pays": True}], "paying"),
        ([{"iban": BT, "currency": "RON", "pays": True, "retired": "2026-01-01"}], "retired account cannot pay"),
        ([{"iban": BT, "currency": "RON", "bank": "ing"}], "doesn't match"),
        ([{"iban": BT, "currency": "RON", "bank": "nope"}], "doesn't match"),
        ([{"iban": BT, "currency": " "}], "invalid account"),
        ([{"iban": "RO00", "currency": "RON"}], "invalid account"),
        ([{"iban": BT, "currency": "RON", "note": "x"}], "invalid account"),
        ([{"iban": BT, "currency": "RON", "pays": "yes"}], "cannot pay"),
        ([{"iban": BT, "currency": "RON", "retired": "ieri"}], "date"),
        ([{"iban": "RO00BTRL0000000000000001", "currency": "RON", "retired": "2026-01-01"}], "retired IBAN"),
        ("RO", "list"),
    ],
)
def test_an_ambiguous_or_broken_account_list_is_refused(accounts, why):
    with pytest.raises(ConfigError, match=why):
        companies.company_from(entry(accounts), 0)
