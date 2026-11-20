import ast
import inspect
import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fiscalm import bnr, companies
from fiscalm.invoicing import (
    branding,
    canvas as invoice_canvas,
    canvas as invoice_font,
    copy as invoice_copy,
    engine as i,
    pdf as invoice_pdf,
    recurring,
    spv as invoice_spv,
    ubl,
    workspace as invoicing_api,
)
from fiscalm.ledger import Ledger

PARTY = dict(
    name="Firma Fictivă SRL",
    cui="99999984",
    country="RO",
    street="Strada Exemplu 1",
    city="Brașov",
    county="RO-BV",
    postal_code="000000",
    vat_payer=True,
    vat_id="RO99999984",
    iban="RO24BTRLRONCRT0000000001",
    bank="Banca Fictivă",
    reg_com="J00/0000/2026",
    email="office@example.invalid",
    phone="0000000000",
)
LINE = dict(
    description="Servicii de încercare", quantity="1", unit="HUR", unit_price="100", vat_category="S", vat_rate="21"
)
DATA = dict(issue_date="2026-10-31", due_date="2026-11-30", currency="RON", notes="", lines=[LINE])
UPLOAD_DAY = "2026-11-04"  # spv.auto_upload sends an invoice issued on DATA's date from this day
PDF_TEXT = "Factura conține caractere care nu pot fi afișate în PDF."


@pytest.fixture
def setup_invoice(ledger, monkeypatch):
    def unavailable(_):
        raise bnr.RateError("BNR unavailable in synthetic test")

    monkeypatch.setattr(bnr, "fetch_xml", unavailable)
    i.save_seller(ledger, "NORD", PARTY)
    cid = i.save_client(ledger, "NORD", PARTY | dict(name="Client Fictiv SRL", cui="99999992", vat_id="RO99999992"))
    i.create_series(ledger, "NORD", "TEST", 42)
    return ledger, cid


def test_invoice_uses_known_seller_without_a_separate_setup(setup_invoice):
    L, cid = setup_invoice
    own = companies.get("NORD", L)
    profile = {k: v for k, v in PARTY.items() if k not in {"iban", "bank", "email"}}
    profile.update(cui=own.cui, vat_id="RO" + own.cui)
    with L.tx() as db:
        db.execute("DELETE FROM invoice_sellers WHERE company='NORD'")
        db.execute("UPDATE firms SET lookup_profile=? WHERE key='NORD'", (json.dumps(profile),))
    workspace = invoicing_api.workspace(L, "NORD")
    assert workspace["seller"]["cui"] == own.cui
    assert workspace["seller"]["iban"] == own.ron_iban
    iid = i.save_draft(L, "NORD", "TEST", cid, DATA)
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert i.invoice(L, iid, company="NORD")["data"]["seller"]["cui"] == own.cui
    assert not L.db.execute("SELECT 1 FROM invoice_sellers WHERE company='NORD'").fetchone()


def draft(setup_invoice, data=None):
    L, cid = setup_invoice
    return i.save_draft(L, "NORD", "TEST", cid, data or DATA)


def confirm_rate(L, iid):
    """Give rendering/corpus fixtures a trusted synthetic BNR selection before issue."""
    inv = i.invoice(L, iid, company="NORD")
    data = inv["data"]
    if data["currency"] != "RON":
        with L.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO invoice_rates VALUES(?,?,?,?)",
                (
                    data["currency"],
                    i.chargeability_date(data | {"lines": inv["lines"]}),
                    data["exchange_rate"],
                    data["exchange_date"],
                ),
            )


def draft_with_confirmed_rate(setup_invoice, data=None):
    iid = draft(setup_invoice, data)
    confirm_rate(setup_invoice[0], iid)
    return iid


def next_number(L):
    return L.db.execute("SELECT next_number FROM invoice_series").fetchone()[0]


def race(path, run, n=2):
    """Release `run(ledger, k)` on n connections at once; return what succeeded and the refusals' messages."""
    barrier = Barrier(n)

    def one(k):
        other = Ledger(path)
        try:
            barrier.wait(timeout=5)
            return True, run(other, k)
        except i.InvoiceError as exc:
            return False, str(exc)
        finally:
            other.close()

    with ThreadPoolExecutor(n) as pool:
        results = list(pool.map(one, range(n)))
    return [v for ok, v in results if ok], [v for ok, v in results if not ok]


def test_decimal_comma_draft(setup_invoice):
    data = DATA | dict(
        currency="EUR",
        exchange_rate="4,9753",
        exchange_date="2026-10-30",
        lines=[LINE | dict(quantity="12,50", unit_price="2,125", vat_rate="21,0")],
    )
    inv = i.invoice(setup_invoice[0], draft(setup_invoice, data), company="NORD")
    assert (inv["lines"][0]["quantity"], inv["lines"][0]["unit_price"]) == ("12.50", "2.125")
    assert inv["data"]["exchange_rate"] == "4.9753"
    assert (inv["data"]["totals"]["net"], inv["data"]["totals"]["total"]) == ("26.56", "32.14")


@pytest.mark.parametrize(
    "field,value",
    [("exchange_rate", "1.234,50"), ("quantity", "1,234.50"), ("unit_price", "12,50,0"), ("unit_price", "1 234,50")],
)
def test_ambiguous_decimal_input_refuses_draft(setup_invoice, field, value):
    data = DATA | dict(currency="EUR", exchange_rate="4.9753", exchange_date="2026-10-30")
    if field == "exchange_rate":
        data[field] = value
    else:
        data["lines"] = [LINE | {field: value}]
    with pytest.raises(i.InvoiceError, match="Cantitățile și sumele trebuie scrise ca numere zecimale finite."):
        draft(setup_invoice, data)
    assert setup_invoice[0].db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0


def test_issue_and_immutable_snapshot(setup_invoice):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    assert i.invoice(L, iid, company="NORD")["number"] is None
    issued = i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    assert issued["number"] == 42
    assert issued["data"]["totals"]["total"] == "121.00"
    i.save_seller(L, "NORD", PARTY | dict(name="Changed"))
    i.save_client(L, "NORD", PARTY | dict(name="Changed client", cui="99999992", vat_id="RO99999992"), cid)
    assert i.invoice(L, iid, company="NORD")["data"]["seller"]["name"] == PARTY["name"]
    assert i.invoice(L, iid, company="NORD")["data"]["client"]["name"] == "Client Fictiv SRL"
    for op in (
        lambda: i.issue(L, iid, "2026-10-31", company="NORD", version=1),
        lambda: i.delete_draft(L, iid, company="NORD"),
        lambda: i.save_draft(L, "NORD", "TEST", cid, DATA, iid),
    ):
        with pytest.raises(i.InvoiceError):
            op()


def test_edit_delete_and_failure_consumes_no_number(setup_invoice):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    with pytest.raises(i.InvoiceError):
        i.issue(L, iid, "2026-10-30", company="NORD", version=1)
    i.save_draft(L, "NORD", "TEST", cid, DATA | dict(lines=[LINE | dict(unit_price="2")]), iid, version=1)
    assert i.invoice(L, iid, company="NORD")["data"]["totals"]["total"] == "2.42"
    i.delete_draft(L, iid, company="NORD")
    assert i.issue(L, draft(setup_invoice), "2026-10-31", company="NORD", version=1)["number"] == 42
    # Numbering follows issue dates: an earlier day after #42 is refused and takes no number.
    with pytest.raises(i.InvoiceError, match="ultimei facturi"):
        i.issue(L, draft(setup_invoice, DATA | dict(issue_date="2026-10-30")), "2026-10-30", company="NORD", version=1)
    assert next_number(L) == 43


def test_two_connections_number_consecutively(setup_invoice):
    L, _ = setup_invoice
    ids = [draft(setup_invoice), draft(setup_invoice)]
    won, refused = race(
        L.path, lambda other, k: i.issue(other, ids[k], "2026-10-31", company="NORD", version=1)["number"]
    )
    assert sorted(won) == [42, 43] and not refused
    assert next_number(L) == 44


def test_same_draft_race_consumes_one_number(setup_invoice):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    won, refused = race(L.path, lambda other, _: i.issue(other, iid, "2026-10-31", company="NORD", version=1)["number"])
    assert won == [42] and len(refused) == 1
    assert i.issue(L, draft(setup_invoice), "2026-10-31", company="NORD", version=1)["number"] == 43


def test_two_connections_save_same_version_only_once(setup_invoice):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    notes = ["First session", "Second session"]

    def save(other, k):
        i.save_draft(other, "NORD", "TEST", cid, DATA | dict(notes=notes[k]), iid, version=1)
        return notes[k]

    won, refused = race(L.path, save)
    inv = i.invoice(L, iid, company="NORD")
    assert inv["version"] == 2
    assert won == [inv["data"]["notes"]] and refused == [i.STALE_DRAFT]
    assert inv["lines"][0]["unit_price"] == "100"


@pytest.mark.parametrize(
    "change",
    [
        dict(vat_category="S", vat_rate="19"),
        dict(vat_rate="0"),
        dict(vat_category="E", vat_rate="0"),
        dict(quantity="0"),
        dict(unit_price="-1"),
        dict(unit_price=1.5),
        dict(unit_price="NaN"),
        dict(unit_price="1e-2"),
        dict(unit_price="0.0000000000001"),
        dict(unit=""),
        dict(description=""),
        dict(vat_category="X"),
    ],
)
def test_refuse_invalid_lines(change):
    with pytest.raises(i.InvoiceError):
        i.calculate([LINE | change], True)


def test_line_refusals_say_linie():
    with pytest.raises(i.InvoiceError) as refused:
        i.calculate([], True)
    assert str(refused.value) == "Factura are nevoie de cel puțin o linie."
    with pytest.raises(i.InvoiceError) as refused:
        i.calculate([{k: v for k, v in LINE.items() if k != "unit_price"}], True)
    assert str(refused.value) == "Fiecare linie are nevoie de descriere, cantitate, unitate, preț și TVA."


def test_category_rounding_and_nonpayer():
    _, totals = i.calculate([LINE | dict(unit_price=".03")] * 3, True)
    assert totals["vat"] == "0.02"
    with pytest.raises(i.InvoiceError):
        i.calculate([LINE], False)
    with pytest.raises(i.InvoiceError):
        i.calculate([LINE | dict(vat_category="O", vat_rate="0", exemption_reason="other")], False)
    assert (
        i.calculate([LINE | dict(vat_category="O", vat_rate="0", exemption_reason="art. 310")], False)[1]["vat"]
        == "0.00"
    )


def test_canonical_rate_and_bounded_product():
    rows, totals = i.calculate([LINE, LINE | dict(vat_rate="21.00")], True)
    assert len(totals["taxes"]) == 1
    assert {r["vat_rate"] for r in rows} == {"21"}
    with pytest.raises(i.InvoiceError):
        i.calculate([LINE | dict(quantity="1000000000000000", unit_price="1000000000000000")], True)


def test_company_and_series_refusals(setup_invoice):
    L, cid = setup_invoice
    with pytest.raises(i.InvoiceError):
        i.create_series(L, "NORD", "TEST", 1)
    with pytest.raises(i.InvoiceError):
        i.save_draft(L, "STUDIO9", "TEST", cid, DATA)
    with pytest.raises(i.InvoiceError):
        i.save_client(L, "STUDIO9", PARTY, cid)
    assert i.save_client(L, "NORD", PARTY | dict(cui="99999992", vat_id="RO99999992")) == cid
    iid = draft(setup_invoice)
    for op in (
        lambda: i.invoice(L, iid, company="STUDIO9"),
        lambda: i.issue(L, iid, DATA["issue_date"], company="STUDIO9", version=1),
        lambda: i.delete_draft(L, iid, company="STUDIO9"),
    ):
        with pytest.raises(i.InvoiceError):
            op()
    assert next_number(L) == 42


def test_new_invoices_start_in_the_chosen_series(setup_invoice):
    L, _ = setup_invoice
    i.create_series(L, "NORD", "ALT", 7)
    assert i.default_series(L, "NORD") == "ALT"  # without a choice, the first by code
    i.set_default_series(L, "NORD", "TEST")
    assert [(x["code"], x["default"]) for x in invoicing_api.workspace(L, "NORD")["series"]] == [
        ("ALT", False),
        ("TEST", True),
    ]
    with pytest.raises(i.InvoiceError):
        i.set_default_series(L, "NORD", "NONE")
    i.remove_series(L, "NORD", "TEST")
    assert i.default_series(L, "NORD") == "ALT"
    i.create_series(L, "NORD", "TEST", 1)  # a removed series' choice does not come back with it
    assert i.default_series(L, "NORD") == "ALT"


@pytest.mark.parametrize(
    "use, refusal",
    [
        ("issued", "Seria are facturi emise."),
        ("draft", "O ciornă folosește seria."),
        ("recurring", "O factură recurentă folosește seria."),
    ],
)
def test_a_series_in_use_is_never_removed(setup_invoice, use, refusal):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    if use == "issued":
        i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    if use == "recurring":
        recurring.create(L, "NORD", iid, 31, 15, "2026-10-01", first_period="2026-10")
        i.delete_draft(L, iid, company="NORD")
    assert invoicing_api.workspace(L, "NORD")["series"][0]["remove_refusal"] == refusal
    with pytest.raises(i.SeriesRefusal, match=refusal):
        i.remove_series(L, "NORD", "TEST")
    assert next_number(L) == (43 if use == "issued" else 42)


def test_removing_a_series_and_saving_a_draft_in_it_never_both_succeed(setup_invoice):
    L, cid = setup_invoice
    ops = [
        lambda other: i.remove_series(other, "NORD", "TEST"),
        lambda other: i.save_draft(other, "NORD", "TEST", cid, DATA),
    ]
    won, refused = race(L.path, lambda other, k: ops[k](other) or "removed")
    assert len(won) == 1 and len(refused) == 1
    series = L.db.execute("SELECT count(*) FROM invoice_series").fetchone()[0]
    drafts = L.db.execute("SELECT count(*) FROM invoices").fetchone()[0]
    assert (series, drafts) == ((0, 0) if won == ["removed"] else (1, 1))


@pytest.mark.parametrize("change", [dict(county="RO-XX"), dict(name="x\x00"), dict(phone=1)])
def test_party_validation(change, ledger):
    with pytest.raises(i.InvoiceError):
        i.save_client(ledger, "NORD", PARTY | change)


@pytest.mark.parametrize(
    "profile, message",
    [
        (PARTY | dict(cui="1"), "CUI-ul firmei trebuie să aibă între 2 și 10 cifre."),
        (PARTY | dict(iban=""), "Lipsesc IBAN-ul și banca firmei."),
        (
            {k: v for k, v in PARTY.items() if k != "vat_payer"},
            "Lipsește alegerea: firma este sau nu plătitoare de TVA.",
        ),
    ],
)
def test_seller_refusals_name_the_firm(ledger, profile, message):
    """Approved copy (AGENTS.md): the refusal says it is the firm's data."""
    with pytest.raises(i.InvoiceError) as refused:
        i.save_seller(ledger, "NORD", profile)
    assert str(refused.value) == message


@pytest.mark.parametrize(
    "payer, given, kept", [(True, "", "RO99999984"), (True, "RO123", "RO99999984"), (False, "RO99999984", "")]
)
def test_seller_vat_code_follows_the_cui(ledger, payer, given, kept):
    i.save_seller(ledger, "NORD", PARTY | dict(vat_payer=payer, vat_id=given))
    row = ledger.db.execute("SELECT data FROM invoice_sellers WHERE company='NORD'").fetchone()
    assert json.loads(row["data"])["vat_id"] == kept


def test_issue_derives_bank_and_vat_code_from_an_older_profile(setup_invoice):
    L, _ = setup_invoice
    stale = PARTY | dict(vat_payer=False, vat_id="RO99999984", bank="Banca Greșită")
    with L.tx() as db:
        db.execute("UPDATE invoice_sellers SET data=? WHERE company='NORD'", (json.dumps(stale),))
    exempt = DATA | dict(lines=[LINE | dict(vat_category="O", vat_rate="0", exemption_reason="art. 310")])
    seller = i.issue(L, draft(setup_invoice, exempt), "2026-10-31", company="NORD", version=1)["data"]["seller"]
    assert (seller["bank"], seller["vat_id"]) == ("Banca Transilvania", "")


def test_private_client_and_sector(ledger):
    profile = {k: v for k, v in PARTY.items() if k not in {"cui", "vat_id"}}
    with pytest.raises(i.InvoiceError):
        i.save_client(ledger, "NORD", profile)
    i.save_client(
        ledger, "NORD", profile | dict(private=True, vat_payer=False, county="RO-B", city="Sector 3 Mun. București")
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(notes=1),
        dict(unknown="x"),
        dict(notes="x" * 301),
        dict(lines=[LINE | dict(unknown="x")]),
        dict(lines=[LINE | dict(net="100")]),
    ],
)
def test_draft_type_and_content_refusals(setup_invoice, change):
    with pytest.raises(i.InvoiceError):
        draft(setup_invoice, DATA | change)


def test_cjk_draft_and_issue_refuse_without_number(setup_invoice):
    L, _ = setup_invoice
    with pytest.raises(i.InvoiceError, match=f"^{re.escape(PDF_TEXT)}$"):
        draft(setup_invoice, DATA | dict(lines=[LINE | dict(description="中文")]))
    iid = draft(setup_invoice)
    payload = i.invoice(L, iid, company="NORD")["data"] | dict(notes="中文")
    L.db.execute("UPDATE invoices SET data=? WHERE id=?", (json.dumps(payload), iid))
    L.db.commit()
    with pytest.raises(i.InvoiceError, match=f"^{re.escape(PDF_TEXT)}$"):
        i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert next_number(L) == 42


def test_series_increment_failure_rolls_back_number(setup_invoice):
    L, _ = setup_invoice
    iid = draft(setup_invoice)
    L.db.execute(
        "CREATE TRIGGER block_increment BEFORE UPDATE OF next_number ON invoice_series WHEN NEW.next_number != OLD.next_number BEGIN SELECT RAISE(IGNORE); END"
    )
    L.db.commit()
    with pytest.raises(i.InvoiceError, match="Seria a fost modificată"):
        i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert i.invoice(L, iid, company="NORD")["status"] == "draft"
    assert next_number(L) == 42


def test_stale_tab_cannot_save_or_issue(setup_invoice):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    seen = invoicing_api.workspace(L, "NORD")["drafts"][0]["version"]
    other = Ledger(L.path)
    try:
        i.save_draft(other, "NORD", "TEST", cid, DATA | dict(notes="New contents"), iid, version=seen)
    finally:
        other.close()
    before = i.invoice(L, iid, company="NORD")
    with pytest.raises(i.InvoiceError, match=i.STALE_DRAFT):
        i.save_draft(L, "NORD", "TEST", cid, DATA, iid, version=seen)
    with pytest.raises(i.InvoiceError, match=i.STALE_DRAFT):
        i.issue(L, iid, DATA["issue_date"], company="NORD", version=seen)
    assert i.invoice(L, iid, company="NORD") == before
    assert next_number(L) == 42
    assert i.issue(L, iid, DATA["issue_date"], company="NORD", version=2)["number"] == 42


def test_missing_client_refuses_issue_and_save(setup_invoice):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    with L.tx() as db:
        db.execute("DELETE FROM invoice_clients WHERE id=?", (cid,))
    for op in (
        lambda: i.issue(L, iid, DATA["issue_date"], company="NORD", version=1),
        lambda: i.save_draft(L, "NORD", "TEST", cid, DATA, iid, version=1),
    ):
        with pytest.raises(i.InvoiceError, match="Clientul trebuie să fie al acestei firme."):
            op()
    assert i.invoice(L, iid, company="NORD")["status"] == "draft"
    assert next_number(L) == 42


def test_draft_version_refusals_are_distinct(setup_invoice):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    for op in (
        lambda: i.save_draft(L, "NORD", "TEST", cid, DATA, iid),
        lambda: i.issue(L, iid, DATA["issue_date"], company="NORD"),
    ):
        with pytest.raises(i.DraftConflict, match=i.VERSION_REQUIRED) as exc:
            op()
        assert exc.value.code == "version_required"
    for value in (0, -1, True, "1"):
        with pytest.raises(i.DraftConflict, match=i.INVALID_VERSION):
            i.save_draft(L, "NORD", "TEST", cid, DATA, iid, version=value)
    for target, co in [("missing", "NORD"), (iid, "STUDIO9")]:
        with pytest.raises(i.InvoiceError, match=i.MISSING_DRAFT):
            i.save_draft(L, co, "TEST", cid, DATA, target, version=1)
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    with pytest.raises(i.InvoiceError, match=i.MISSING_DRAFT):
        i.save_draft(L, "NORD", "TEST", cid, DATA, iid, version=1)


def test_delete_optional_version_preserves_newer_draft(setup_invoice):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    i.save_draft(L, "NORD", "TEST", cid, DATA, iid, version=1)
    with pytest.raises(i.DraftConflict, match=i.STALE_DRAFT):
        i.delete_draft(L, iid, company="NORD", version=1)
    assert i.invoice(L, iid, company="NORD")["version"] == 2
    i.delete_draft(L, iid, company="NORD", version=2)
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0


def test_unrelated_action_does_not_validate_draft_version(setup_invoice):
    result = invoicing_api.act(setup_invoice[0], "auto-upload", "NORD", dict(on=False, version=0))
    assert result["ok"] and result["message"] == "Trimiterea automată în SPV a fost oprită."  # approved toast


def test_no_english_in_invoice_refusals():
    english = re.compile(
        r"\b(required|requires|must|cannot|missing|invalid|exceeds|too long|Use|Choose|Only|Expected"
        r"|unavailable|contains|returned|response|supported)\b"
    )
    for module in (i, invoicing_api, ubl, branding, invoice_spv, invoice_pdf, invoice_font, invoice_canvas, bnr):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name not in {"InvoiceError", "RateError", "check"} or (name == "check" and module is not ubl):
                continue
            for part in ast.walk(node.args[2] if name == "check" else node.args[0]):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    assert not english.search(part.value), (module.__name__, part.value)


@pytest.mark.parametrize(
    "key,limit,expected",
    [
        ("tax_id", 100, "Codul fiscal local poate avea cel mult 100 de caractere."),
        ("name", 200, "Numele poate avea cel mult 200 de caractere."),
        ("street", 150, "Strada poate avea cel mult 150 de caractere."),
        ("city", 50, "Localitatea poate avea cel mult 50 de caractere."),
        ("postal_code", 20, "Codul poștal poate avea cel mult 20 de caractere."),
        ("reg_com", 1000, "Numărul din registrul comerțului poate avea cel mult 1000 de caractere."),
        ("email", 100, "Adresa de email poate avea cel mult 100 de caractere."),
        ("phone", 100, "Telefonul poate avea cel mult 100 de caractere."),
        ("account_name", 200, "Titularul contului poate avea cel mult 200 de caractere."),
    ],
)
def test_length_refusals_name_the_field(setup_invoice, key, limit, expected):
    L = setup_invoice[0]
    assert i.save_client(L, "NORD", PARTY | {key: "x" * limit})  # the limit itself is allowed
    with pytest.raises(i.InvoiceError) as exc:
        i.save_client(L, "NORD", PARTY | {key: "x" * (limit + 1)})
    assert str(exc.value) == invoice_copy.refusal(str(exc.value)) == expected  # the app shows it as raised


def test_supply_place_refusal_reaches_the_app(setup_invoice):
    L, cid = setup_invoice
    body = DATA | dict(
        series="TEST",
        client_id=cid,
        supply_country="DE",
        lines=[LINE | dict(vat_category="G", vat_rate="0", exemption="Export")],
    )
    assert "locul livrării trebuie să fie România" in invoicing_api.act(L, "draft-save", "NORD", body)["message"]
