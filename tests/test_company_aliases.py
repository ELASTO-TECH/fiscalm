"""A firm's other names ("Alte nume ale firmei"): plain aliases fiscalm escapes, saved from Setări, Firme. Synthetic
names only."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from firms import entries, set_entries
from test_api import _api, _get_json, server as server  # noqa: F401
from test_companies import co
from test_company_config import config as config  # noqa: F401

from fiscalm import companies, company_config as cc, docs
from fiscalm.api import actions
from fiscalm.companies import companies_from, name_regex
from fiscalm.config_errors import ConfigError
from fiscalm.payments import orders as ops


@pytest.mark.parametrize(
    "name,text,found",
    [
        ("Atelier Nord S.R.L.", "factura către ATELIER NORD SRL, CUI", True),  # dotted legal form = undotted
        ("Atelier Nord", "client: Atelier-Nord s.r.l.", True),  # punctuation between words
        ("Atelier Nord", "ATELIERNORD", True),  # words run together
        ("Alfa SRL", "ALFA S.R.L.", True),  # an undotted legal form = dotted
        ("Alfa SRL", "ALFA S. R. L.", True),
        ("Alpha L Consulting", "ALPHA L CONSULTING", True),  # a lone initial stays a word
        ("Alpha L Consulting", "ALPHA R CONSULTING", False),
        ("Ștefan Grup", "STEFAN GRUP", True),  # diacritics dropped in the PDF
        ("Ştefan Grup", "ȘTEFAN GRUP", True),  # cedilla and comma below read the same
        ("Nord", "NORDIC SRL", False),  # never inside a longer word
        ("Nord", "SNORD", False),
        ("Atelier Nord", "ATELIER DE NORD", False),  # another word between
        ("Atelier Nord", "ATELIER          NORD", False),  # far apart: not one name
        ("A+B (Nord)", "A+B (NORD)", True),  # regex characters are text
        ("A+B (Nord)", "AAB NORD", False),
        ("Nord.*", "NORD SRL", True),  # '.*' is punctuation, not 'anything'
        ("Nord.*", "NORDIC", False),
    ],
)
def test_a_name_is_looked_for_as_text(name, text, found):
    assert bool(name_regex(name).search(companies.fold(text))) is found


def test_a_name_without_letters_or_digits_matches_nothing():
    assert name_regex(".*") is None and name_regex("  ") is None


def test_the_legal_name_and_the_aliases_name_the_firm_as_text():
    aliases = ["Alfa C", " ", "(Grup)+"]
    c = companies_from({"companies": [co("ALFA", legal_name="Alfa Consult S.R.L.", aliases=aliases)]})["ALFA"]
    assert c.aliases == ("Alfa C", "(Grup)+")
    assert c.named_in("ALFA CONSULT SRL") and c.named_in("Alfa-C") and not c.named_in("ALFA")
    assert c.named_in("GRUP") and not c.named_in("GRUPGRUP")  # a name is never a pattern


@pytest.mark.parametrize("bad", ["Alfa", ["x" * 101], ["A"] * 21])
def test_a_broken_aliases_list_is_refused(bad):
    with pytest.raises(ConfigError, match="aliases"):
        companies_from({"companies": [co("ALFA", aliases=bad)]})


def test_an_alias_is_saved_and_files_documents(config, ledger):
    entries()
    assert not docs.inspect_text("Bon fiscal NORD ATELIERE").mentions
    r = actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": "  Nord   Ateliere "})
    assert r["ok"] and r["message"] == "Numele a fost adăugat."
    assert r["done"] == {"key": "NORD", "aliases": ["Atelier Nord", "Nord Ateliere"]}
    after = entries()
    entry = after["companies"][0]
    assert entry["aliases"] == ["Atelier Nord", "Nord Ateliere"]
    assert docs.inspect_text("Bon fiscal NORD ATELIERE").mentions == ["NORD"]
    assert docs.inspect_text("Factura ATELIER-NORD").mentions == ["NORD"]
    r = actions.act("company-alias-remove", "ALL", {"key": "NORD", "alias": "Nord Ateliere"})
    assert r["ok"] and r["message"] == "Numele a fost șters." and r["done"]["aliases"] == ["Atelier Nord"]
    assert companies.get("NORD").aliases == ("Atelier Nord",) and not docs.inspect_text("NORD ATELIERE").mentions


OP = """PLATITOR {payer}
Cod de identificare fiscala
11111111
platitor RO24BTRLRONCRT0000000001 BTRLRO22
beneficiar RO00ZZZZ0000000000000009 ZZZZROBU
"""


def test_a_payment_order_payer_may_be_written_as_an_alias(config, ledger):
    """ops.parse_op_text refuses an order whose payer name isn't the firm's; an alias counts as the firm's name."""

    def refused(payer):
        return any("payer name" in e for e in ops.parse_op_text(OP.format(payer=payer)).errors)

    assert refused("NORD ATELIERE SRL")
    assert actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": "Nord Ateliere"})["ok"]
    assert not refused("NORD ATELIERE SRL") and not refused("Atelier Nord S.R.L.")


@pytest.mark.parametrize(
    "alias,why",
    [
        ("S.R.L.", cc.ALIAS_SHORT),  # only a legal form: would name every firm
        ("AB", cc.ALIAS_SHORT),
        ("AB SRL", cc.ALIAS_SHORT),
        ("S.C. S.R.L. S.C. S.R.L.", cc.ALIAS_SHORT),  # read as SC SRL SC SRL, never as SCS R L
        ("A S.C. S.R.L.", cc.ALIAS_SHORT),
        ("Atelier Nord SRL", cc.ALIAS_SAVED),  # the legal name itself
        ("atelier-nord s.r.l.", cc.ALIAS_SAVED),
        ("Studio 9", cc.ALIAS_OTHER),  # another firm's alias
        ("Studio", cc.ALIAS_OTHER),  # part of another firm's legal name
        ("Studio 9 Atelier", cc.ALIAS_OTHER),  # contains another firm's name
        ("Studio9", cc.ALIAS_OTHER),  # the same letters run together: a document could name both
    ],
)
def test_an_alias_that_could_file_a_document_wrongly_is_refused(config, ledger, alias, why):
    before = entries()
    r = actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": alias})
    assert not r["ok"] and r["message"] == why
    assert entries() == before


def test_a_duplicate_alias_and_a_full_list_are_refused(config, ledger):
    assert actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": "Nord Ateliere"})["ok"]
    r = actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": "NORD-ATELIERE"})
    assert not r["ok"] and r["message"] == cc.ALIAS_SAVED
    for n in range(companies.ALIASES_MAX - 2):  # NORD has one alias in the example config
        assert actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": f"Nord Ateliere {n}"})["ok"]
    r = actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": "Nord Ateliere X"})
    assert not r["ok"] and r["message"] == cc.ALIAS_FULL


@pytest.mark.parametrize(
    "body",
    [
        {"key": "NORD", "alias": "Nord\nAteliere"},  # a control character
        {"key": "NORD", "alias": "N" * 101},
        {"key": "NORD", "alias": "Nord Ateliere", "pattern": "x"},
        {"key": "ALL", "alias": "Nord Ateliere"},
        {"key": "NOPE", "alias": "Nord Ateliere"},
        {"alias": "Nord Ateliere"},
    ],
)
def test_a_malformed_request_is_refused_and_nothing_is_written(config, ledger, body):
    before = entries()
    for action in ("company-alias-add", "company-alias-remove"):
        assert not actions.act(action, "ALL", body)["ok"]
    assert entries() == before


def test_removing_a_name_that_is_not_saved_is_refused(config, ledger):
    r = actions.act("company-alias-remove", "ALL", {"key": "NORD", "alias": "Nord Ateliere"})
    assert not r["ok"] and "nu mai este salvat" in r["message"]


def test_setari_lists_the_names_and_suggests_the_legal_name_without_its_form(config, ledger, server):
    _, listing = _get_json(server, "/web/api/companies")
    nord = listing["companies"][0]
    assert (nord["aliases"], nord["alias_suggestion"]) == (["Atelier Nord"], "")
    studio = listing["companies"][1]
    assert (studio["aliases"], studio["alias_suggestion"]) == (["Studio 9"], "")
    assert listing["limits"]["alias_max"] == 100 and listing["limits"]["aliases_max"] == 20
    code, r = _api(server, "company-alias-remove", {"key": "NORD", "alias": "Atelier Nord"})
    assert code == 200 and r["ok"]
    _, listing = _get_json(server, "/web/api/companies")
    assert (listing["companies"][0]["aliases"], listing["companies"][0]["alias_suggestion"]) == ([], "Atelier Nord")


@pytest.mark.parametrize(
    "legal,suggested",
    [
        ("ATELIER NORD S.R.L.", "ATELIER NORD"),
        ("S.C. Alfa Consult SRL", "Alfa Consult"),
        ("Beta Grup SA", "Beta Grup"),
        ("Gama PFA", "Gama"),
        ("Delta Consult", ""),  # no legal form to drop: the legal name already names the firm
        ("AB SRL", ""),  # what is left would be too short
    ],
)
def test_the_suggestion_is_the_legal_name_without_its_form(legal, suggested):
    c = companies_from({"companies": [co("ALFA", legal_name=legal)]})["ALFA"]
    assert cc.alias_suggestion(c) == suggested


def test_an_overlapping_alias_never_lets_a_document_name_two_firms(config, ledger):
    """Another firm's alias 'AtelierNord' and NORD's legal name 'Atelier Nord SRL' would both match
    'ATELIERNORD SRL'; the alias is refused, so ownership never has to choose."""
    assert actions.act("company-alias-remove", "ALL", {"key": "NORD", "alias": "Atelier Nord"})["ok"]
    r = actions.act("company-alias-add", "ALL", {"key": "STUDIO9", "alias": "AtelierNord"})
    assert not r["ok"] and r["message"] == cc.ALIAS_OTHER
    assert docs.inspect_text("ATELIERNORD SRL").mentions == ["NORD"]


@pytest.mark.parametrize(
    "theirs,mine",
    [
        ("Nordsa Grup SRL", "Nord SA Grup"),  # the legal form's letters run into a word: both match NORDSAGRUP SRL
        ("Lumina Studio SRL", "Casa Lumina"),  # one ends where the other starts: both match CASA LUMINA STUDIO SRL
        ("Lumina Studio SRL", "LuminaStudio"),
    ],
)
def test_names_that_one_text_could_match_are_refused(config, ledger, theirs, mine):
    """Whatever text matches both names, a document could be filed under either firm."""
    cfg = entries()
    cfg["companies"][1]["legal_name"] = theirs
    cfg["companies"][1]["aliases"] = []
    set_entries(cfg)

    assert companies.names_overlap(mine, theirs) and companies.names_overlap(theirs, mine)
    r = actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": mine})
    assert not r["ok"] and r["message"] == cc.ALIAS_OTHER


@pytest.mark.parametrize(
    "a,b,both",
    [
        ("Nord", "Nordic SRL", False),  # never inside a longer word
        ("Nord SA", "Nord SRL", False),  # different legal forms are different names
        ("Alfa Grup", "Beta Grup SRL", False),
        ("Grup Alfa", "Alfa SRL", True),  # GRUP ALFA SRL
        ("Atelier Nord", "Nord", True),
    ],
)
def test_overlap_is_exactly_what_one_text_can_match(a, b, both):
    assert companies.names_overlap(a, b) is both and companies.names_overlap(b, a) is both
    if both:  # and a text matched by both exists
        merged = {"Grup Alfa": "GRUP ALFA SRL", "Atelier Nord": "ATELIER NORD"}[a]
        assert name_regex(a).search(merged) and name_regex(b).search(merged)


def test_two_racing_adds_of_overlapping_names_save_one(config, ledger):
    with ThreadPoolExecutor(2) as pool:
        got = list(
            pool.map(
                lambda a: actions.act("company-alias-add", "ALL", a),
                [{"key": "NORD", "alias": "Lumina Grup"}, {"key": "STUDIO9", "alias": "Lumina Grup SRL"}],
            )
        )
    assert sorted(r["ok"] for r in got) == [False, True]
    saved = [c["aliases"] for c in entries()["companies"]]
    assert sum("Lumina Grup" in " ".join(a) for a in saved) == 1


def test_the_alias_stays_out_of_the_log(config, ledger, home):
    assert actions.act("company-alias-add", "ALL", {"key": "NORD", "alias": "Nord Ateliere"})["ok"]
    log = "".join(p.read_text() for p in (home / "logs").glob("*.log"))
    assert "company-alias-add NORD" in log and "Ateliere" not in log
