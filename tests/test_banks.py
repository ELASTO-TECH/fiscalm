"""The bank catalogue: a bank is found from an IBAN, and a company's accounts carry it, whatever the bank."""

import dataclasses
import json
import threading

import pytest

from fiscalm import companies, integrations as ig, secrets
from fiscalm.bank import catalogue as banks
from fiscalm.ledger import Ledger


@pytest.mark.parametrize(
    "iban,bank",
    [
        ("RO24BTRLRONCRT0000000001", "bt"),
        ("ro00 rncb 0000 0000 0000 0001", "bcr"),  # spaces and lower case, as people paste them
        ("RO00BRDE000000000000001", "brd"),
        ("RO00INGB0000000000000001", "ing"),
        ("RO00CECE0000000000000000", "cec"),
        ("RO00XXXX0000000000000001", None),  # a bank code the catalogue doesn't hold
        ("BG00BTRL0000000000000001", None),  # BT's code under another country is not BT
        ("RO00BTR", None),
        ("", None),
        ("0000BTRL0000", None),
    ],
)
def test_a_bank_is_found_from_its_iban_code_and_an_unknown_or_malformed_one_names_none(iban, bank):
    assert getattr(banks.for_iban(iban), "id", None) == bank


def test_each_bank_has_its_published_iban_codes():
    # written out independently of the catalogue, so a wrong code in it fails here
    assert {b.id: b.iban_codes for b in banks.CATALOGUE} == {
        "bt": ("BTRL",),
        "bcr": ("RNCB",),
        "brd": ("BRDE",),
        "ing": ("INGB",),
        "raiffeisen": ("RZBR",),
        "unicredit": ("BACX",),
        "cec": ("CECE",),
        "libra": ("BREL",),
        "otp": ("OTPV",),
        "exim": ("BRMA", "EXIM"),
        "revolut": ("REVO",),
    }
    assert banks.for_iban("RO00BRMA0000000000000001").id == "exim"


def test_every_catalogue_row_is_usable():
    assert len({b.id for b in banks.CATALOGUE}) == len(banks.CATALOGUE)
    for b in banks.CATALOGUE:
        assert b.name and len(b.country) == 2 and b.iban_codes and b.statement_formats
        assert all(code.isalnum() and len(code) == 4 for code in b.iban_codes)
        assert all(provider and name for provider, name in b.open_banking)
    bt = banks.BY_ID["bt"]
    assert "bt-allmerge-pdf" in bt.statement_formats and bt.payment_file == "btgo-csv"
    assert bt.provider_name("enablebanking") == "Banca Transilvania"
    assert banks.BY_ID["otp"].provider_name("enablebanking") is None


def test_a_company_lists_its_accounts_with_their_banks_and_their_statement_senders():
    nord = companies.get("NORD")
    accounts = nord.accounts
    assert [(a.currency, a.bank, a.pays, a.retired) for a in accounts] == [
        ("RON", "bt", True, ""),
        ("EUR", "bt", False, ""),
    ]
    assert {a.iban for a in accounts} == nord.own_ibans
    assert nord.statement_senders == banks.BY_ID["bt"].statement_senders


def test_retired_and_other_banks_accounts_are_listed_and_an_empty_one_is_not():
    nord = companies.get("NORD")
    entry = {
        "key": "NORD",
        "legal_name": "N SRL",
        "cui": "11111111",
        "accounts": [
            {"iban": "RO00INGB0000000000000009", "currency": "EUR"},
            {"iban": "RO37BTRL0000000000000001", "currency": "RON", "retired": "2026-10-01"},
        ],
    }
    c = dataclasses.replace(nord, accounts=companies.accounts_from(entry, "NORD"))
    assert [(a.currency, a.bank, a.pays, a.retired) for a in c.accounts] == [
        ("EUR", "ing", False, ""),
        ("RON", "bt", False, "2026-10-01"),
    ]
    assert c.ron_iban == "" and c.paying_account is None


def test_a_firms_statement_password_is_set_from_its_bank_never_through_integrari(ledger, sec):
    assert "bank" not in {k["key"] for k in ig.kinds()}
    ig.set_statement_password(ledger, "NORD", "bt", "first-pass")
    (row,) = ig.enabled(ledger, "bank")
    assert ig.listing(ledger) == [] and secrets.bt_statement_password("NORD") == "first-pass"
    for kind, iid in (("bank", ""), ("", row.id)):
        with pytest.raises(ig.IntegrationError):
            ig.check_listed(ledger, kind, iid)
    ig.set_statement_password(ledger, "NORD", "bt", "second-pass")  # replaces the one statements are opened with
    assert [i.id for i in ig.enabled(ledger, "bank")] == [row.id]
    assert secrets.bt_statement_password("NORD") == "second-pass"
    for bank, password in (("ing", "x"), ("nope", "x"), ("bt", "")):  # ING sends no protected PDF; nothing to set
        with pytest.raises(ig.IntegrationError):
            ig.set_statement_password(ledger, "NORD", bank, password)


def test_two_connections_saving_a_first_password_at_once_keep_one_setting(ledger, sec):
    start, done = threading.Barrier(2), []

    def save(password):
        L = Ledger()  # one connection per thread, as two requests would have
        try:
            start.wait()
            ig.set_statement_password(L, "NORD", "bt", password)
            done.append(password)
        finally:
            L.close()

    ts = [threading.Thread(target=save, args=(p,)) for p in ("pass-a", "pass-b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(done) == ["pass-a", "pass-b"]  # both saves succeeded
    assert len(ig.enabled(ledger, "bank")) == 1 and len(list(sec.iterdir())) == 1  # no second row, no stray file
    assert secrets.bt_statement_password("NORD") in ("pass-a", "pass-b")


def test_another_banks_password_is_never_tried_on_a_bt_statement(ledger, sec):
    ig.add(ledger, "bank", "ING NORD", {"bank": "ing", "company": "NORD", "statement_password": "ing-pass"})
    with pytest.raises(secrets.SecretError, match="Firme"):
        secrets.bt_statement_password("NORD")
    ig.add(ledger, "bank", "BT NORD", {"bank": "bt", "company": "NORD", "statement_password": "new-bt-pass"})
    assert secrets.bt_statement_password("NORD") == "new-bt-pass"


def _switch_off(ledger, row):
    ig.set_enabled(ledger, row["id"], False)


def _clear_and_move_to_bt(ledger, row):  # no password of its own: none, never ING's
    ig.clear(ledger, row["id"], "statement_password")
    ig.update(ledger, row["id"], {"bank": "bt"})


def _drop_the_bank(ledger, row):  # a legacy row without a bank
    cfg = json.loads(ledger.db.execute("SELECT config FROM integrations WHERE id=?", (row["id"],)).fetchone()[0])
    cfg.pop("bank")
    with ledger.db:
        ledger.db.execute("UPDATE integrations SET config=? WHERE id=?", (json.dumps(cfg), row["id"]))


@pytest.mark.parametrize("bank,change", [("bt", _switch_off), ("ing", _clear_and_move_to_bt), ("bt", _drop_the_bank)])
def test_a_bt_password_comes_only_from_an_enabled_bt_row_with_its_own_password(ledger, sec, bank, change):
    row = ig.add(ledger, "bank", "Extrase", {"bank": bank, "company": "NORD", "statement_password": "int-pass"})
    change(ledger, row)
    with pytest.raises(secrets.SecretError):
        secrets.bt_statement_password("NORD")
