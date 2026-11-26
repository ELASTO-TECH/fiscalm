"""Adding a company's accounts, any bank and currency, and what Setări shows of them. Synthetic data only."""

import json
from datetime import date, timedelta

import pytest
from firms import entries
from test_camt import xml
from test_company_config import config as config  # noqa: F401

from fiscalm import cli, companies, company_config as cc
from fiscalm.api import actions
from fiscalm.bank import csv as cs
from fiscalm.payments import engine as pay


def checked(country, body):
    """An invented IBAN with a computed checksum."""
    digits = "".join(str(int(c, 36)) for c in body + country + "00")
    return f"{country}{98 - int(digits) % 97:02d}" + body


def iban(bank_code, n):
    return checked("RO", bank_code + str(n).zfill(16))


ING_EUR = iban("INGB", 7)


def nord_accounts(ledger):
    """NORD's accounts as Setări lists them."""
    return next(c for c in cc.listing(ledger)["companies"] if c["key"] == "NORD")["accounts"]


def test_an_account_at_another_bank_is_added_next_to_the_others(config, ledger):
    r = actions.act("company-account", "ALL", {"key": "NORD", "iban": " " + ING_EUR.lower(), "currency": "eur"})
    assert r["ok"] and r["message"] == "Contul a fost adăugat."
    assert r["done"] == {"key": "NORD", "currency": "EUR", "bank": "ing", "iban_last4": ING_EUR[-4:]}
    nord = companies.get("NORD")
    assert (nord.account(ING_EUR).bank, nord.account(ING_EUR).pays) == ("ing", False)
    assert nord.ron_iban == "RO24BTRLRONCRT0000000001"  # the paying account is untouched
    assert len(entries()["companies"][0]["accounts"]) == 3


@pytest.mark.parametrize(
    "args,why",
    [
        (
            {"iban": f"RO{(int(ING_EUR[2:4]) + 1) % 100:02d}" + ING_EUR[4:], "currency": "EUR"},
            "nu este valid",
        ),  # checksum
        ({"iban": ING_EUR, "currency": "euro"}, "moneda"),
        ({"iban": ING_EUR, "currency": ""}, "moneda"),
    ],
)
def test_a_bad_account_is_refused_and_nothing_is_written(config, ledger, args, why):
    before = entries()
    r = actions.act("company-account", "ALL", {"key": "NORD"} | args)
    assert not r["ok"] and why in r["message"].lower()
    assert entries() == before


def test_another_companys_or_the_accountants_or_a_duplicate_is_refused(config, ledger):
    studio = iban("ZZZZ", 5)
    assert actions.act("company-account", "ALL", {"key": "STUDIO9", "iban": studio, "currency": "RON"})["ok"]
    r = actions.act("company-account", "ALL", {"key": "NORD", "iban": studio, "currency": "RON"})
    assert not r["ok"] and "altă firmă" in r["message"]
    ledger.add_efactura(
        {
            "message_id": "accountant",
            "company": "NORD",
            "supplier_cui": "55555555",
            "raw_path": "synthetic.xml",
            "status": "open",
            "supplier_iban": iban("CECE", 1),
        }
    )
    r = actions.act("company-account", "ALL", {"key": "NORD", "iban": iban("CECE", 1), "currency": "RON"})
    assert not r["ok"] and "partener" in r["message"]
    assert actions.act("company-account", "ALL", {"key": "NORD", "iban": ING_EUR, "currency": "EUR"})["ok"]
    r = actions.act("company-account", "ALL", {"key": "NORD", "iban": ING_EUR, "currency": "EUR"})
    assert not r["ok"] and r["message"] == "Acest IBAN este deja salvat la firmă."


def test_an_unknown_field_or_company_is_refused(config, ledger):
    assert not actions.act("company-account", "ALL", {"key": "NORD", "iban": ING_EUR, "currency": "EUR", "pays": "1"})[
        "ok"
    ]
    assert not actions.act("company-account", "ALL", {"key": "NOPE", "iban": ING_EUR, "currency": "EUR"})["ok"]
    assert not actions.act("company-account", "ALL", {"key": "NORD", "iban": ING_EUR})["ok"]


def test_setari_lists_each_account_with_its_last_statement(config, ledger):
    actions.act("company-account", "ALL", {"key": "NORD", "iban": ING_EUR, "currency": "EUR"})
    ledger.add_document("bt", "NORD", "2026-09", "statement", "/x", accounts=["RO24BTRLRONCRT0000000001"])
    ledger.add_document("ing", "NORD", "2026-10", "statement", "/y", accounts=[ING_EUR])
    accounts = nord_accounts(ledger)
    assert [(a["currency"], a["bank_name"], a["pays"], a["last_statement"]) for a in accounts] == [
        ("RON", "Banca Transilvania", True, "2026-09"),
        ("EUR", "Banca Transilvania", False, None),
        ("EUR", "ING Bank", False, "2026-10"),
    ]
    assert all(
        set(a)
        == {
            "id",
            "iban_last4",
            "currency",
            "bank",
            "bank_name",
            "pays",
            "retired",
            "opened",
            "last_statement",
            "statement_senders",
        }
        for a in accounts
    )  # never a full IBAN
    ledger.add_document("lost", "NORD", "2026-11", "statement", "/z")  # stored before accounts, its file gone
    assert [a["last_statement"] for a in nord_accounts(ledger)] == ["2026-09", None, "2026-10"]  # proves nothing


def test_a_legacy_statement_is_read_once_to_learn_its_accounts(config, ledger, tmp_path):
    actions.act("company-account", "ALL", {"key": "NORD", "iban": ING_EUR, "currency": "EUR"})
    path = tmp_path / "old.xml"
    path.write_bytes(xml())  # NORD's BT RON account only
    ledger.add_document("old", "NORD", "2026-10", "statement", str(path))  # stored under its own month
    assert [(a["currency"], a["bank"], a["last_statement"]) for a in nord_accounts(ledger)] == [
        ("RON", "bt", "2026-10"),
        ("EUR", "bt", None),
        ("EUR", "ing", None),
    ]  # never the ING account added since
    assert json.loads(ledger.document("old")["accounts"]) == ["RO24BTRLRONCRT0000000001"]  # kept: read once
    path.unlink()
    assert nord_accounts(ledger)[0]["last_statement"] == "2026-10"


@pytest.mark.parametrize(
    "value,ok",
    [
        (checked("DE", "ZZZZ" + "0" * 14), True),
        (checked("DE", "ZZZZ" + "0" * 7), False),  # checksum right, German length wrong
        (
            checked("US", "ZZZZ" + "0" * 14),
            False,
        ),  # a country whose IBAN length isn't known: refused, not saved unverified
    ],
)
def test_foreign_accounts_are_checked_by_their_countrys_length(config, ledger, value, ok):
    r = actions.act("company-account", "ALL", {"key": "NORD", "iban": value, "currency": "EUR"})
    assert r["ok"] is ok, r["message"]


@pytest.mark.parametrize("second", [("invalid", "EUR"), (ING_EUR, "EUR")])
def test_multiple_account_additions_are_validated_before_any_is_saved(config, ledger, second):
    before = companies.get("NORD", L=ledger)
    with pay.pay_lock(), ledger.tx(), pytest.raises(cc.CompanyError):
        cc.account_additions(ledger, "NORD", [(ING_EUR, "EUR"), second])
    assert companies.get("NORD", L=ledger) == before


@pytest.mark.parametrize(
    "month,refused,retired",
    [("2023-07", False, False), ("", False, False), ("2023-7", True, False), ("2023-13", True, False)]
    + [("2026-12", True, True)],  # a retired account cannot open after it was retired
)
def test_an_accounts_opening_month_is_set_from_the_cli(config, ledger, month, refused, retired):
    nord = companies.get("NORD")
    if retired:
        cc.bank(key="NORD", iban=iban("BTRL", 9), replace=True, today=date(2026, 11, 2), L=ledger)
    own = nord.ron_iban if retired else nord.ibans["EUR"]
    code = cli.main(["company", "dates", "--company", "NORD", "--iban", own, "--opened", month])
    assert (code, companies.get("NORD").account(own).opened) == ((2, "") if refused else (0, month))


@pytest.mark.parametrize(
    "who, opened, retired, said",
    [
        ("EUR", "2024-03", "2026-10-31", None),  # both dates, in the app
        ("EUR", "2024-03-14", "", None),  # the exact day; still open
        ("EUR", "2026-11", "2026-10-31", "nu se poate deschide"),  # opened after it was closed
        ("EUR", "", "31.10.2026", "AAAA-LL-ZZ"),
        ("RON", "", "2026-10-31", "Plățile pleacă din acest cont"),  # payment files debit it: replaced first
        ("STRANGER", "", "", "nu este al acestei firme"),
        ("EUR", "", "tomorrow", "o zi care a trecut"),  # a later day would stop reading the account now
        ("EUR", "2099-01", "", "o lună sau o zi care a trecut"),  # a typo would excuse it from every statement
    ],
)
def test_an_accounts_dates_are_set_in_the_app(config, ledger, who, opened, retired, said):
    nord = companies.get("NORD")
    own = nord.ibans.get(who, "RO00XXXX0000000000000001")
    before = nord.account(own)
    retired = (date.today() + timedelta(days=1)).isoformat() if retired == "tomorrow" else retired
    body = {"key": "NORD", "account": cs.account_id("NORD", own), "opened": opened, "retired": retired}
    r = actions.act("company-account-dates", "ALL", body)
    after = companies.get("NORD").account(own)
    if said:
        assert not r["ok"] and said in r["message"] and after == before
    else:
        assert r["ok"] and (after.opened, after.retired) == (opened, retired)
        mine = next(a for a in nord_accounts(ledger) if a["currency"] == "EUR" and a["bank"] == "bt")
        assert (mine["opened"], mine["retired"]) == (opened or None, retired or None)


def test_a_date_left_out_keeps_what_is_recorded(config, ledger):
    """A form opened before another person closed the account sends only the opening it changed: still closed."""
    eur = companies.get("NORD").ibans["EUR"]
    account = cs.account_id("NORD", eur)
    assert actions.act("company-account-dates", "ALL", {"key": "NORD", "account": account, "retired": "2026-10-31"})[
        "ok"
    ]
    assert actions.act("company-account-dates", "ALL", {"key": "NORD", "account": account, "opened": "2024-03"})["ok"]
    assert (companies.get("NORD").account(eur).opened, companies.get("NORD").account(eur).retired) == (
        "2024-03",
        "2026-10-31",
    )
    assert not actions.act("company-account-dates", "ALL", {"key": "NORD", "account": account})["ok"]  # nothing to set
    assert actions.act("company-account-dates", "ALL", {"key": "NORD", "account": account, "retired": ""})["ok"]
    assert not companies.get("NORD").account(eur).retired  # open again: read and owed as before
    assert cli.main(["company", "dates", "--company", "NORD", "--iban", eur]) == 2  # the CLI, the same rule


def test_choosing_the_paying_account_again_keeps_its_opening_month(config, ledger):
    ron = companies.get("NORD").ron_iban
    cc.set_dates(key="NORD", iban=ron, opened="2021-08", retired=None, today=date(2026, 11, 2), L=ledger)
    cc.bank(key="NORD", iban=ron, replace=True, today=date(2026, 11, 2), L=ledger)
    assert companies.get("NORD").account(ron).opened == "2021-08"
