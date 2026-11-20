"""Parteneri: one partner per CUI (or per name without its legal form), both roles, what is owed each way. Synthetic
data only (tests/synthetic_world.py)."""

from datetime import date
from decimal import Decimal

import pytest
import synthetic_world as scenario
from test_invoicing import DATA, PARTY
from test_links import _record, _tx

from fiscalm import links
from fiscalm.api import state as api, transaction_choices
from fiscalm.bank import statements
from fiscalm.invoicing import engine as engine, issued, workspace
from fiscalm.ledger import LinkRefused


@pytest.fixture
def world(ledger, home, monkeypatch):
    return scenario.build(ledger, home, monkeypatch.setattr)


def test_partner_id_by_tax_id_else_by_name_keeping_the_legal_form():
    assert api.partner_id("ACME", "RO 1234 5678", "Orice SRL") == "ACME.12345678"
    assert api.partner_id("ACME", "12345678", "x") == api.partner_id("ACME", "ro12345678", "y")
    assert api.partner_id("ACME", "", "S.C. Alfa Consult S.R.L.") == api.partner_id("ACME", None, "ALFA CONSULT SRL")
    assert api.partner_id("ACME", "", "Alfa Consult S R L") == "ACME.n-alfa-consult-srl"
    assert api.partner_id("ACME", "", "ALFA SRL") != api.partner_id("ACME", "", "ALFA SA")  # two firms
    assert api.partner_id("ACME", "", "ALFA SC SRL") != api.partner_id("ACME", "", "ALFA SRL")  # SC only as a prefix
    assert api.partner_id("ACME", "", "") is None and api.partner_id("ACME", None, "  ") is None
    assert api.partner_id("A", "123", "x") != api.partner_id("B", "123", "x")  # one partner per company


@pytest.mark.parametrize(
    "raw, token",
    [
        ("DE123456789", "DE123456789"),
        ("FRAB123456789", "FRAB123456789"),
        ("RO123456789", "123456789"),
        ("123456789", "123456789"),
        ("de 123-456", "DE123456"),
    ],
)
def test_foreign_vat_ids_keep_their_country(raw, token):
    assert api.tax_id(raw) == token
    assert len({api.partner_id("A", x, "n") for x in ("DE123456789", "FR123456789", "RO123456789")}) == 3


def _inv(supplier, cui, date_, total="10.00"):
    return {
        "supplier": supplier,
        "supplier_cui": cui,
        "issue_date": date_,
        "status": "paid",
        "payment_state": None,
        "currency": "RON",
        "total": total,
        "overdue": False,
        "credits": [],
        "credit_note": False,
        "partner": None,
    }


def test_an_invoice_without_cui_joins_the_one_partner_with_that_name():
    invs = [_inv("Alfa Consult SRL", "12345678", "2026-10-01"), _inv("ALFA CONSULT S.R.L.", None, "2026-10-05")]
    ps = api.link_partners("A", invs, [])
    assert [r["partner"] for r in invs] == ["A.12345678", "A.12345678"] and len(ps) == 1 and ps[0]["received"] == 2


def test_an_ambiguous_name_stays_apart():
    invs = [
        _inv("Alfa SRL", "111", "2026-10-01"),
        _inv("Alfa SRL", "222", "2026-10-02"),
        _inv("Alfa SRL", None, "2026-10-03"),
    ]
    api.link_partners("A", invs, [])
    assert [r["partner"] for r in invs] == ["A.111", "A.222", "A.n-alfa-srl"]


def test_an_empty_name_never_hides_a_known_one():
    invs = [_inv("Alfa SRL", "111", "2026-10-01"), _inv("", "111", "2026-10-09")]
    p = api.link_partners("A", invs, [])[0]
    assert p["name"] == "Alfa SRL" and p["last"] == "2026-10-09"


def _old(direction, name, cui, sha, total="100.00", credit_note=False):
    party = {"id": "history:" + sha, "direction": direction, "party_name": name, "party_cui": cui, "partner": None}
    return party | {"issue_date": "2025-03-01", "currency": "RON", "total": total, "credit_note": credit_note}


def test_history_invoices_join_partners_by_cui_and_private_persons_stay_apart():
    invs = [_inv("Alfa SRL", "111", "2026-10-01"), _inv("Alfa SRL", None, "2026-10-02")]
    old = [
        _old("received", "ALFA S.R.L.", "111", "a"),
        _old("received", "Alfa SRL", None, "b", "40.00", credit_note=True),  # joins by its unique name
        _old("issued", "Ion Pop", "1800101123456", "c"),  # a person's CNP is never shown, nor merged
        _old("received", "Ion Pop", "1800101123456", "d"),
        _old("received", "Ion Pop", None, "e"),
        _old("received", "Alfa SRL", "222", "f"),  # another Alfa in history never moves the tracked invoices
    ]
    ps = {p["id"]: p for p in api.link_partners("A", invs, [], None, old)}
    assert [r["partner"] for r in invs] == ["A.111", "A.111"]
    assert [r["partner"] for r in old] == ["A.111", "A.111"] + [
        f"A.{k}-history:{x}" for k, x in [("issued", "c"), ("received", "d"), ("received", "e")]
    ] + ["A.222"]
    assert [r["party_cui"] for r in old[2:5]] == [None] * 3 and ps["A.issued-history:c"]["roles"] == ["client"]
    alfa = ps["A.111"]
    assert (alfa["received"], alfa["payable"]["total"], alfa["last"]) == (4, "0.00", "2026-10-02")
    assert (
        alfa["turnover"]["expenses"]["total"] == "80.00" and alfa["turnover"]["history"]["expenses"]["total"] == "60.00"
    )


def test_every_invoice_names_its_partner_and_every_partner_its_invoices(ledger, world):
    s = api.state(ledger, "NORD")
    partners = {p["id"]: p for p in s["partners"]}
    for r in s["invoices"]:
        assert r["partner"] in partners and "supplier" in partners[r["partner"]]["roles"], r["id"]
    for r in s["issued"]:
        assert r["partner"] in partners and "client" in partners[r["partner"]]["roles"], r["id"]
    for p in partners.values():
        old = [r["direction"] for r in s["history"] if r["partner"] == p["id"]]  # history counts too, never owes
        assert p["received"] == sum(r["partner"] == p["id"] for r in s["invoices"]) + old.count("received")
        assert p["issued"] == sum(r["partner"] == p["id"] for r in s["issued"]) + old.count("issued")


def test_what_each_partner_is_owed_adds_up_to_the_company_totals(ledger, world):
    s = api.state(ledger, "NORD")
    owed_out = sum(Decimal(p["receivable"]["total"]) for p in s["partners"])
    assert owed_out == Decimal(s["receivable"]["total"])  # issued, in lei, still to collect
    eur = sum(Decimal(o["total"]) for p in s["partners"] for o in p["receivable"]["other"] if o["currency"] == "EUR")
    assert eur == sum(Decimal(o["total"]) for o in s["receivable"]["other"] if o["currency"] == "EUR")
    energie = next(p for p in s["partners"] if p["name"] == "Energie Furnizare SA")
    assert energie["payable"] == {"total": "386.40", "other": []} and energie["late"] == 1 and energie["cui"] == "1001"


def test_a_partner_seen_as_supplier_and_client_is_one_partner(ledger, world):
    from datetime import date

    from synthetic_world import _ef

    s0 = api.state(ledger, "NORD")
    client = next(r for r in s0["issued"] if r["client_cui"])
    _ef(
        ledger,
        "190",
        "NORD",
        client["client"],
        f"RO{client['client_cui']}",
        "X-1",
        date(2026, 10, 20),
        date(2026, 11, 20),
        "10.00",
    )
    s = api.state(ledger, "NORD")
    p = next(p for p in s["partners"] if p["id"] == client["partner"])
    assert p["roles"] == ["supplier", "client"] and p["received"] == 1 and p["issued"] >= 1


def test_partners_are_newest_first_and_carry_no_internal_fields(ledger, world):
    ps = api.state(ledger, "NORD")["partners"]
    assert [p["last"] for p in ps] == sorted((p["last"] for p in ps), reverse=True)
    assert all(
        set(p)
        == {
            "id",
            "company",
            "name",
            "cui",
            "roles",
            "last",
            "received",
            "issued",
            "late",
            "payable",
            "receivable",
            "turnover",
            "vat_id",
            "address",
            "country",
            "iban_last4",
            "ro_vat_status",
            "client_id",
            "anaf_status",
            "anaf_error",
            "anaf_eligible",
            "autopay",
            "can_set_autopay",
            "not_mailed",
            "can_set_not_mailed",
            "held",
            "report_formats",
        }
        for p in ps
    )


@pytest.mark.parametrize("a, b", [("Łukasz Trans SRL", "ukasz Trans SRL"), ("北京 SRL", "上海 SRL")])
def test_names_in_any_alphabet_stay_apart(a, b):
    assert api.partner_id("A", "", a) != api.partner_id("A", "", b)
    assert api.partner_id("A", "", "北京") is not None


def test_the_newest_named_invoice_names_the_partner_across_both_directions():
    invs = [_inv("", "111", "2026-10-09"), _inv("Old Name SRL", "111", "2026-10-01")]
    issued = [
        {
            "client": "New Name SRL",
            "client_cui": "111",
            "issue_date": "2026-10-05",
            "status": "collected",
            "currency": "RON",
            "owed": "0.00",
            "total": "10.00",
            "credited": "0.00",
            "days_overdue": 0,
            "partner": None,
        }
    ]
    p = api.link_partners("A", invs, issued)[0]
    assert p["name"] == "New Name SRL" and p["last"] == "2026-10-09"


def test_foreign_vat_ids_from_xml_stay_apart(ledger, home, monkeypatch):
    """Through the real parser, DE/FR/RO ids with the same digits are three partners."""
    from datetime import date

    from synthetic_world import _ef

    from fiscalm.efactura import inbox as efactura

    for mid, raw, name in (
        ("901", "DE123456789", "Kunde GmbH"),
        ("902", "FR123456789", "Client SAS"),
        ("903", "RO123456789", "Client SRL"),
    ):
        assert efactura.tax_token(raw) in ("DE123456789", "FR123456789", "123456789")
        _ef(ledger, mid, "NORD", name, raw, f"X-{mid}", date(2026, 10, 1), date(2026, 10, 30), "10.00")
        with ledger.tx() as db:
            db.execute("UPDATE efactura SET supplier_tax_id=? WHERE message_id=?", (efactura.tax_token(raw), mid))
    s = api.state(ledger, "NORD")
    ids = {r["id"]: r["partner"] for r in s["invoices"] if r["id"] in ("901", "902", "903")}
    assert len(set(ids.values())) == 3 and ids["901"] == "NORD.DE123456789"


def test_the_parser_keeps_the_full_tax_id():
    from test_efactura import ubl

    from fiscalm.efactura import inbox as efactura

    inv = efactura.parse_ubl(ubl(supplier_cui="DE123456789"))
    assert inv.supplier_cui == "123456789" and inv.supplier_tax_id == "DE123456789"  # matching keeps its digits


def _client(L, cid="saved", **fields):
    from fiscalm.invoicing.engine import save_client

    return save_client(
        L,
        "NORD",
        dict(
            name="Exemplu SRL",
            cui="99999992",
            country="RO",
            street="Strada Exemplu 1",
            city="Brașov",
            county="RO-BV",
            ro_vat_status="registered",
        )
        | fields,
        cid,
    )


def _invoice(L, cui="99999992", name="Exemplu SRL"):
    from datetime import date

    scenario._ef(L, "991", "NORD", name, cui, "TEST-1", date(2026, 10, 1), None, "10.00")
    L.db.execute("UPDATE efactura SET supplier_tax_id=? WHERE message_id='991'", (api.tax_id(cui),))
    L.db.commit()


def test_saved_client_without_invoice_has_details_and_zero_balances(ledger):
    _client(ledger)
    (p,) = api.state(ledger, "NORD")["partners"]
    assert p["client_id"] == "saved" and p["roles"] == ["client"]
    assert p["received"] == p["issued"] == p["late"] == 0 and p["last"] == ""
    assert p["payable"] == p["receivable"] == {"total": "0.00", "other": []}
    assert p["country"] == "RO" and p["address"]["city"] == "Brașov" and p["ro_vat_status"] == "registered"


def test_saved_client_merges_by_cui_without_changing_invoice_id(ledger):
    _invoice(ledger)
    before = api.state(ledger, "NORD")
    _client(ledger, cui="ro 99999992", name="Nume nou SRL")
    after = api.state(ledger, "NORD")
    (p,) = after["partners"]
    assert p["client_id"] == "saved" and p["roles"] == ["supplier", "client"]
    assert p["id"] == before["partners"][0]["id"] == before["invoices"][0]["partner"] == after["invoices"][0]["partner"]


@pytest.mark.parametrize("cui", ["99999984", ""])
def test_same_name_different_or_missing_cui_does_not_merge_saved_client(ledger, cui):
    _invoice(ledger)
    _client(ledger, cui=cui, private=not bool(cui))
    ps = api.state(ledger, "NORD")["partners"]
    assert len(ps) == 2 and sum(p["client_id"] == "saved" for p in ps) == 1


@pytest.mark.parametrize("vat, count", [("de 999 999 999", 1), ("FRAB999999999", 2), ("", 2)])
def test_foreign_saved_client_merges_only_by_vat(ledger, vat, count):
    _invoice(ledger, "DE999999999")
    _client(ledger, cui="999999999", country="DE", county="", vat_id=vat)
    ps = api.state(ledger, "NORD")["partners"]
    assert len(ps) == count and sum(p["client_id"] == "saved" for p in ps) == 1


def test_all_companies_keeps_saved_clients_with_their_own_company(ledger):
    _client(ledger)
    ps = [p for part in api.state(ledger, "ALL")["parts"] for p in part["partners"]]
    assert [(p["company"], p["client_id"]) for p in ps] == [("NORD", "saved")]
    assert api.state(ledger, "STUDIO9")["partners"] == []


def test_state_exposes_only_known_iban_suffixes(ledger):
    import json

    _invoice(ledger)
    state = api.state(ledger, "NORD")
    iban = ledger.efactura("991")["supplier_iban"]
    assert state["partners"][0]["iban_last4"] == [iban[-4:]]
    assert iban not in json.dumps(state)


def test_imported_party_address_is_available_without_reading_zip(ledger, home):
    from test_efactura import anaf_zip, ubl

    from fiscalm.efactura import inbox as efactura
    from fiscalm.invoicing import issued

    xml = ubl().replace(
        b"<cac:Party>",
        b"<cac:Party><cac:PostalAddress><cbc:StreetName>Strada Test 2</cbc:StreetName>"
        b"<cbc:CityName>Test</cbc:CityName><cac:Country><cbc:IdentificationCode>RO</cbc:IdentificationCode>"
        b"</cac:Country></cac:PostalAddress>",
    )
    for module, mid in ((efactura, "992"), (issued, "993")):
        module.store(ledger, "NORD", {"id": mid, "data_creare": "202610010900"}, anaf_zip(xml))
    for path in home.rglob("*.zip"):
        path.unlink()
    ps = api.state(ledger, "NORD")["partners"]
    assert len(ps) == 2 and all(p["address"]["street"] == "Strada Test 2" and p["country"] == "RO" for p in ps)


@pytest.mark.parametrize("outside", [False, True])
def test_legacy_zip_address_is_confined_to_company_storage(ledger, home, outside):
    from test_efactura import anaf_zip, ubl

    from fiscalm.efactura import inbox as efactura

    xml = ubl().replace(
        b"<cac:Party>", b"<cac:Party><cac:PostalAddress><cbc:CityName>Test</cbc:CityName></cac:PostalAddress>"
    )
    efactura.store(ledger, "NORD", {"id": "994", "data_creare": "202610010900"}, anaf_zip(xml))
    source = ledger.efactura("994")["raw_path"]
    if outside:
        from pathlib import Path

        target = home / "efactura" / "STUDIO9" / "old.zip"
        target.parent.mkdir(parents=True)
        Path(source).rename(target)
        Path(source).symlink_to(target)
    ledger.db.execute("UPDATE efactura SET party_address=NULL WHERE message_id='994'")
    ledger.db.commit()
    efactura.fill_party_addresses(ledger)
    (p,) = api.state(ledger, "NORD")["partners"]
    assert p["address"] is None if outside else p["address"]["city"] == "Test"


def _legacy(L, home, mid="994"):
    from test_efactura import anaf_zip, ubl

    from fiscalm.efactura import inbox as efactura

    xml = ubl().replace(
        b"<cac:Party>", b"<cac:Party><cac:PostalAddress><cbc:CityName>Test</cbc:CityName></cac:PostalAddress>"
    )
    efactura.store(L, "NORD", {"id": mid, "data_creare": "202610010900"}, anaf_zip(xml))
    L.db.execute("UPDATE efactura SET party_address=NULL WHERE message_id=?", (mid,))
    L.db.commit()
    from pathlib import Path

    return Path(L.efactura(mid)["raw_path"])


def test_partner_projection_never_reads_legacy_zip(ledger, home, monkeypatch):
    # Partner identity stays ledger-only; the separate VAT chart reads declared tax evidence.
    import os
    import zipfile

    _legacy(ledger, home)

    def fail(*args, **kwargs):
        pytest.fail("partner projection opened an invoice file")

    open_file = os.open

    def guarded_open(path, *args, **kwargs):
        if str(path).endswith(".zip"):
            fail()
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(zipfile, "ZipFile", fail)
    assert api.entity_rows(ledger, "NORD")["partners"][0]["address"] is None


def test_address_backfill_marks_success_missing_and_unreadable_once(ledger, home, monkeypatch):
    from fiscalm import storage
    from fiscalm.efactura import inbox as efactura

    _legacy(ledger, home)
    _legacy(ledger, home, "995").unlink()
    _legacy(ledger, home, "996").write_bytes(b"broken zip")
    assert efactura.fill_party_addresses(ledger) == 3
    assert api.state(ledger, "NORD")["partners"][0]["address"]["city"] == "Test"
    assert ledger.efactura("995")["party_address"] == ledger.efactura("996")["party_address"] == "{}"

    def fail(*args):
        pytest.fail("backfill retried a marked file")

    monkeypatch.setattr(storage, "read_file", fail)
    assert efactura.fill_party_addresses(ledger) == 0


def test_backfill_through_symlinked_home(ledger, home, monkeypatch):
    from fiscalm.efactura import inbox as efactura

    path = _legacy(ledger, home)
    link = home.parent / (home.name + "-link")
    link.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("FISCALM_HOME", str(link))
    ledger.db.execute("UPDATE efactura SET raw_path=? WHERE message_id='994'", (str(link / path.relative_to(home)),))
    ledger.db.commit()
    efactura.fill_party_addresses(ledger)
    assert api.state(ledger, "NORD")["partners"][0]["address"]["city"] == "Test"


def test_backfill_refuses_month_directory_linked_to_other_company(ledger, home):
    from fiscalm.efactura import inbox as efactura

    path = _legacy(ledger, home)
    target = home / "efactura" / "STUDIO9" / "month"
    target.parent.mkdir(parents=True)
    path.parent.rename(target)
    path.parent.symlink_to(target, target_is_directory=True)
    efactura.fill_party_addresses(ledger)
    assert ledger.efactura("994")["party_address"] == "{}"


@pytest.mark.parametrize("guard", ["nofollow", "realpath"])
def test_safe_open_guards_independently_refuse_swaps(home, monkeypatch, guard):
    import os

    from fiscalm.storage import read_file

    root = home / "files"
    month = root / "month"
    month.mkdir(parents=True)
    path = month / "invoice"
    path.write_bytes(b"original")
    other = home / "other"
    other.mkdir()
    (other / "invoice").write_bytes(b"foreign")
    inside = root / "target"
    inside.write_bytes(b"inside")
    original = os.open

    def swap(p, flags):
        if guard == "nofollow":
            path.unlink()
            path.symlink_to(inside)  # realpath check alone would allow this
        else:
            month.rename(root / "old-month")
            month.symlink_to(other, target_is_directory=True)  # final component is a regular file
        return original(p, flags)

    monkeypatch.setattr(os, "open", swap)
    assert read_file(path, [root], 100) is None


def _raw_client(L, cid="saved", **fields):
    import json

    d = (
        dict(name="Exemplu SRL", cui="99999992", country="RO", street="Strada Test", city="Test", county="RO-BV")
        | fields
    )
    L.db.execute("INSERT INTO invoice_clients(id,company,data) VALUES(?,?,?)", (cid, "NORD", json.dumps(d)))
    L.db.commit()


def test_duplicate_saved_identities_never_attach_to_supplier(ledger):
    _invoice(ledger)
    _raw_client(ledger, "one")
    _raw_client(ledger, "two")
    ps = api.state(ledger, "NORD")["partners"]
    assert len(ps) == 3 and len({p["id"] for p in ps}) == 3
    assert next(p for p in ps if p["received"])["client_id"] is None


def test_legacy_ro_prefix_merges(ledger):
    _invoice(ledger)
    _raw_client(ledger, cui="RO 99999992")
    (p,) = api.state(ledger, "NORD")["partners"]
    assert p["client_id"] == "saved"


def test_foreign_local_id_never_joins_romanian_supplier(ledger):
    _invoice(ledger, "999999999")
    _raw_client(ledger, country="DE", cui="999999999", tax_id="999999999")
    assert len(api.state(ledger, "NORD")["partners"]) == 2


def test_newest_address_across_received_and_issued(ledger):
    import json
    from datetime import date

    _invoice(ledger)
    scenario._issued(ledger, "995", "NORD", "OUT", "Exemplu SRL", "99999992", date(2026, 10, 2), None, "10.00")
    ledger.db.execute("UPDATE efactura SET party_address=?", (json.dumps({"city": "Older"}),))
    ledger.db.execute("UPDATE issued SET party_address=?", (json.dumps({"city": "Newer"}),))
    ledger.db.commit()
    (p,) = api.state(ledger, "NORD")["partners"]
    assert p["address"]["city"] == "Newer"
    ledger.db.execute("UPDATE efactura SET issue_date='2026-10-03'")
    ledger.db.commit()
    assert api.state(ledger, "NORD")["partners"][0]["address"]["city"] == "Older"


def test_saved_client_changed_cui_leaves_original_supplier(ledger):
    _invoice(ledger)
    _client(ledger)
    _client(ledger, cui="99999984")
    ps = api.state(ledger, "NORD")["partners"]
    assert len(ps) == 2
    assert next(p for p in ps if p["received"])["client_id"] is None
    assert next(p for p in ps if p["client_id"])["cui"] == "99999984"


@pytest.mark.parametrize("cui", ["0000000000000", "1990101123456", "8990101123456", ""])
def test_private_partner_addresses_are_null(ledger, cui):
    import json

    from fiscalm.api.v1 import public_state

    _invoice(ledger, cui)
    ledger.db.execute("UPDATE efactura SET party_address=?", (json.dumps({"street": "Private home"}),))
    ledger.db.commit()
    s = api.state(ledger, "NORD")
    assert all(p["address"] is None for p in s["partners"])
    assert "Private home" not in json.dumps(public_state(s, ledger))
    from fiscalm.api import mcp_tools

    assert "Private home" not in json.dumps(mcp_tools.call(ledger, "read", "get_state", {"co": "NORD"}))


def test_shared_identity_never_deduplicates_foreign_local_ids(ledger):
    from fiscalm.identity import partner_identity
    from fiscalm.invoicing.engine import _identity, save_client

    profile = dict(
        name="Firma GmbH", cui="", vat_id="", tax_id="12345", country="DE", street="Test", city="Test", county=""
    )
    a = save_client(ledger, "NORD", profile)
    b = save_client(ledger, "NORD", profile)
    assert a != b
    assert _identity(profile) == partner_identity("DE", "", "") == ""


@pytest.mark.parametrize(
    "cui,country,vat", [("99999992", "RO", ""), ("", "DE", "DE999999999"), ("99999992", "DE", "RO99999992")]
)
def test_saved_client_partner_id_survives_first_invoice(ledger, cui, country, vat):
    _client(ledger, cui=cui, country=country, vat_id=vat, county="RO-BV" if country == "RO" else "")
    (before,) = api.state(ledger, "NORD")["partners"]
    _invoice(ledger, vat or cui)
    (after,) = api.state(ledger, "NORD")["partners"]
    assert before["id"] == after["id"] == f"NORD.{api.tax_id(vat or cui)}"


def test_xml_address_fields_are_bounded():
    from test_efactura import ubl

    from fiscalm.efactura import inbox as efactura

    xml = ubl().replace(
        b"<cac:Party>",
        b"<cac:Party><cac:PostalAddress><cbc:StreetName>"
        + b"x" * 151
        + b"</cbc:StreetName><cbc:CityName>"
        + b"y" * 51
        + b"</cbc:CityName><cbc:PostalZone>"
        + b"z" * 21
        + b"</cbc:PostalZone></cac:PostalAddress>",
    )
    inv = efactura.parse_ubl(xml)
    assert inv.supplier_address["street"] == "x" * 150
    assert inv.buyer_address["city"] == "y" * 50
    assert inv.buyer_address["postal_code"] == "z" * 20


def test_address_backfill_also_caches_issued_buyers(ledger, home):
    import json

    from test_efactura import anaf_zip, ubl

    from fiscalm.efactura import inbox as efactura
    from fiscalm.invoicing import issued

    xml = ubl().replace(
        b"<cac:Party>", b"<cac:Party><cac:PostalAddress><cbc:CityName>Buyer city</cbc:CityName></cac:PostalAddress>"
    )
    issued.store(ledger, "NORD", {"id": "997", "data_creare": "202610010900"}, anaf_zip(xml))
    ledger.db.execute("UPDATE issued SET party_address=NULL WHERE message_id='997'")
    ledger.db.commit()
    assert efactura.fill_party_addresses(ledger) == 1
    assert json.loads(ledger.issued("997")["party_address"])["city"] == "Buyer city"


def test_a_partner_saved_as_a_supplier_stays_one_until_it_is_saved_as_a_client(ledger):
    client = workspace.party_out(
        dict(name="Exemplu SRL", cui="99999992", country="RO", street="Strada Exemplu 1", city="Brașov", county="RO-BV")
    )
    save = lambda **body: workspace.act(ledger, "client-save", "NORD", dict(client=client) | body)  # noqa: E731
    roles = lambda: [p["roles"] for p in api.state(ledger, "NORD")["partners"]]  # noqa: E731
    saved = save(role="supplier")
    assert saved["message"] == "Furnizorul a fost salvat." and roles() == [["supplier"]]
    assert save(id=saved["id"])["ok"] and roles() == [["supplier"]]  # Modifică keeps what it was saved as
    assert save()["id"] == saved["id"] and roles() == [["supplier", "client"]]  # the invoice form's new client
    assert not save(role="courier")["ok"]


def test_adding_a_firm_already_saved_keeps_its_details_and_adds_the_role(ledger):
    cid = _client(ledger, cid=None, email="contabil@example.com", iban="RO35TEST0000000000000000", bank="Banca Fictivă")
    looked_up = workspace.party_out(
        dict(name="EXEMPLU SRL", cui="99999992", country="RO", street="Strada Alta 2", city="Brașov", county="RO-BV")
    )
    result = workspace.act(ledger, "client-save", "NORD", dict(client=looked_up, role="supplier"))
    (saved,) = workspace.workspace(ledger, "NORD")["clients"]
    assert result["id"] == saved["id"] == cid and saved["name"] == "Exemplu SRL"
    assert (saved["email"], saved["iban"], saved["address"]["street"]) == (
        "contabil@example.com",
        "RO35TEST0000000000000000",
        "Strada Exemplu 1",
    )
    assert api.state(ledger, "NORD")["partners"][0]["roles"] == ["supplier", "client"]


@pytest.mark.parametrize("same_name", [False, True])
def test_private_clients_keep_their_invoices_and_credit_choices(ledger, same_name):
    engine.save_seller(ledger, "NORD", PARTY)
    engine.create_series(ledger, "NORD", "TEST", 1)
    ids = []
    for n in (0, 1, 0):
        cid = _client(
            ledger,
            cid=f"private{n}",
            name="Persoană Exemplu" if same_name else f"Persoană Test {n}",
            cui="",
            vat_id="",
            private=True,
        )
        iid = engine.save_draft(ledger, "NORD", "TEST", cid, DATA)
        engine.issue(ledger, iid, DATA["issue_date"], company="NORD", version=1)
        ids.append(next(mid for mid, local in engine.sent_links(ledger, "NORD").items() if local == iid))
    full = api.state(ledger, "NORD")
    tx = dict(direction="credit", currency="RON", explanation=None)
    assert len(full["partners"]) == 2
    choices = transaction_choices.records(full, tx, "partners", "")
    assert {p["client_id"] for p in choices} == {"private0", "private1"}
    actual = api.item_partners(ledger, "NORD")
    for n, mid in enumerate(ids[:2]):
        p = next(p for p in choices if p["client_id"] == f"private{n}")
        assert p["cui"] is None and p["issued"] == (2 if n == 0 else 1)
        assert actual["issued " + mid] == p["id"]
        assert {r["id"] for r in transaction_choices.records(full, tx, "invoices", p["id"])} == set(
            ids[::2] if n == 0 else ids[1:2]
        )
    (key,) = _record(ledger, "NORD", _tx(1, amount="242.00"))
    with pytest.raises(LinkRefused, match="partner"):
        links.link(ledger, "NORD", key, invoices=ids, partners=actual)
    original = engine.sent_links(ledger, "NORD")[ids[0]]
    engine.storno(ledger, original, DATA["issue_date"], "Corecție de test", company="NORD")
    assert ids[0] in issued.credit_links(ledger, "NORD").values()


def test_imported_private_invoices_never_merge_by_placeholder_or_name(ledger):
    for mid in ("8801", "8802"):
        scenario._issued(
            ledger, mid, "NORD", f"TEST-{mid}", "Persoană Exemplu", "0000000000000", date(2026, 10, 1), None, "10.00"
        )
    full = api.state(ledger, "NORD")
    assert len(full["partners"]) == 2
    assert len({r["partner"] for r in full["issued"]}) == 2
    assert all(p["cui"] is None for p in full["partners"])


@pytest.mark.parametrize("cui", ["0000000000000", ""])
def test_private_credit_needs_human_evidence_not_placeholder_name_or_shared_iban(ledger, cui):
    tx = _tx(1, counterparty="Persoană Exemplu", description="0000000000000 TEST-8801")
    scenario._issued(ledger, "8803", "NORD", "TEST-8801", "Persoană Exemplu", cui, date(2026, 10, 1), None, "100.00")
    st = statements.Statement("NORD", None, None, [statements.Account("x", "RON", [tx])])
    assert issued.match_statement(ledger, st) == []
    assert ledger.issued("8803")["status"] == "open"
    # A possible payment still suppresses reminder text, even though it cannot settle automatically.
    assert issued.credit_from_client(ledger, "NORD", "Persoană Exemplu", cui, tx) == "name"
    assert api.partner_id("NORD", cui, "Persoană Exemplu") == "NORD.n-persoana-exemplu"


@pytest.mark.parametrize("other_name", ["Persoană Test Alfa", "Persoană Test Beta"])
@pytest.mark.parametrize("taxes", [("0000000000000", "0000000000000"), ("1" * 13, "2" * 13)])
def test_private_credit_note_placeholder_does_not_prove_same_client(ledger, other_name, taxes):
    for mid, name, kind, ref in (
        ("8901", "Persoană Test Alfa", "invoice", None),
        ("8902", other_name, "credit_note", "TEST-8901"),
    ):
        scenario._issued(
            ledger,
            mid,
            "NORD",
            f"TEST-{mid}",
            name,
            taxes[0 if kind == "invoice" else 1],
            date(2026, 10, 1),
            None,
            "10.00",
            doc_type=kind,
            ref=ref,
        )
    assert issued.credit_links(ledger, "NORD") == {"8902": None}


def test_private_invoice_remains_a_rival_for_automatic_firm_collection(ledger):
    for mid, cui in (("8911", ""), ("8912", "99999992")):
        scenario._issued(ledger, mid, "NORD", f"TEST-{mid}", "Persoană Exemplu", cui, date(2026, 10, 1), None, "100.00")
    tx = _tx(1, counterparty="Persoană Exemplu", description="Plata")
    st = statements.Statement("NORD", None, None, [statements.Account("x", "RON", [tx])])
    assert issued.match_statement(ledger, st) == []
    assert all(ledger.issued(mid)["status"] == "open" for mid in ("8911", "8912"))


def test_private_partner_suggestions_ignore_placeholder_and_historical_iban(ledger):
    for mid, name in (("8921", "Persoană Test Alfa"), ("8922", "Persoană Test Beta")):
        scenario._issued(ledger, mid, "NORD", f"TEST-{mid}", name, "0000000000000", date(2026, 10, 1), None, "100.00")
    tx = _tx(
        1, counterparty="Persoană Test Beta", description="0000000000000", counterparty_iban="RO49BTRLRONCRT0000000022"
    )
    ledger.db.execute("UPDATE issued SET collected_from_iban=? WHERE message_id='8921'", (tx.counterparty_iban,))
    ledger.db.commit()
    partners = api.item_partners(ledger, "NORD")
    assert links.suggest(ledger, "NORD", [tx], partners) == [partners["issued 8922"]]
