"""Automatizări, Reamintiri de plată: when a reminder may be prepared, set per firm in the app (`rule-reminders`).
Synthetic companies only."""

import json

import pytest

from fiscalm import rules
from fiscalm.api import actions, state as api
from fiscalm.ledger import is_live
from fiscalm.mail import reminders as copy, reminders as r

BODY = {"after_days": "10"}


def rule(L, co="NORD"):
    return next(x for x in rules.list_rules(L, co) if x["kind"] == "client_reminders")


def test_defaults_then_the_action_saves_per_firm_before_going_live(ledger):
    assert r.settings(ledger, "NORD") == {"after_days": 7, "source": "default"}
    assert r.policy(ledger, "NORD") == (7, r.STATEMENT_MAX_AGE_DAYS)
    assert not is_live()  # configuration: allowed in shadow mode, and only for the app
    assert "rule-reminders" in actions.LOCAL_ACTIONS and "rule-reminders" not in actions.API_ACTIONS
    res = actions.act("rule-reminders", "NORD", {"co": "NORD", **BODY})
    assert res["ok"] and res["message"] == copy.SETTINGS_SAVED
    assert res["done"] == {"after_days": 10}
    assert r.settings(ledger, "NORD") == {"after_days": 10, "source": "firm"}
    assert r.policy(ledger, "NORD")[0] == 10
    assert r.settings(ledger, "STUDIO9")["source"] == "default"  # another firm keeps its own


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "after_days": "0"},
        {**BODY, "interval_days": "366"},
        {**BODY, "max_count": "11"},
        {**BODY, "after_days": " "},
        {**BODY, "after_days": "\u0661\u0660"},  # Arabic-Indic digits: ASCII only
        {**BODY, "after_days": "0007"},
        {},  # the setting is required
        {**BODY, "statement_max_age_days": "30"},  # not a setting
    ],
)
def test_a_bad_value_saves_nothing(ledger, body):
    res = actions.act("rule-reminders", "NORD", {"co": "NORD", **body})
    assert not res["ok"] and res["message"] in (copy.SETTINGS_BAD, actions.BAD)
    assert r.settings(ledger, "NORD")["source"] == "default"


@pytest.mark.parametrize("args", [{"co": "STUDIO9", **BODY}, {"co": "NORD", **BODY, "after_days": 10}])
def test_another_firm_or_a_non_string_is_refused(ledger, args):
    res = actions.act("rule-reminders", "NORD", args)
    assert not res["ok"] and res["message"] == actions.BAD
    assert r.settings(ledger, "NORD")["source"] == "default"


@pytest.mark.parametrize("value,saved", [(" 10 ", 10), ("010", 10), ("365", 365)])
def test_spaces_and_leading_zeros_are_read_as_the_number(ledger, value, saved):
    assert (
        actions.act("rule-reminders", "NORD", {"co": "NORD", **BODY, "after_days": value})["done"]["after_days"]
        == saved
    )


def test_a_broken_stored_setting_prepares_nothing_and_the_rule_says_so(ledger):
    ledger.set_meta("client_reminders:NORD", json.dumps({"after_days": 0}))
    with pytest.raises(r.Refused, match=copy.CONFIG):
        r.policy(ledger, "NORD")
    x = rule(ledger)
    assert (x["settings"], x["reason"]) == (None, "invalid_settings")
    assert rules.capabilities(x, admin=True)["can_edit"]  # saving all three repairs it
    assert actions.act("rule-reminders", "NORD", {"co": "NORD", **BODY})["ok"]
    assert rule(ledger)["settings"]["source"] == "firm"
    ledger.set_meta("client_reminders:NORD", "not json")
    with pytest.raises(r.Refused, match=copy.CONFIG):
        r.settings(ledger, "NORD")


def test_the_rule_lists_local_text_settings_in_shadow(ledger):
    x = rule(ledger)
    assert x["enabled"] and x["reason"] is None and x["last"] is None
    assert x["settings"] == {"after_days": 7, "source": "default"}


def test_only_an_admin_may_edit_it_and_the_app_sees_that(ledger):
    x = rule(ledger)
    assert rules.capabilities(x, admin=True) == {"can_pause": False, "can_resume": False, "can_edit": True}
    assert not rules.capabilities(x, admin=False)["can_edit"]
    assert not rules.capabilities(x, admin=True, bearer=True)["can_edit"]
    other = next(y for y in rules.list_rules(ledger, "NORD") if y["kind"] == "accountant_pack")
    assert other["settings"] is None and not rules.capabilities(other, admin=True)["can_edit"]
    x = next(
        y for y in api.state(ledger, "NORD", can_manage_companies=True)["rules"] if y["kind"] == "client_reminders"
    )
    assert x["can_edit"] and x["settings"]["source"] == "default"  # the app's state carries it
