"""Scope controls transport only; invoice identity, snapshots and exports stay intact."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime

import pytest
from test_invoicing import DATA, LINE, PARTY, UPLOAD_DAY, draft, setup_invoice as setup_invoice  # noqa: F401

from fiscalm import companies, integrations
from fiscalm.api import state as api
from fiscalm.efactura import client as anaf
from fiscalm.efactura.inbox import tax_token
from fiscalm.invoicing import engine as i, issued, pdf as invoice_pdf, spv, ubl, workspace as invoicing_api
from fiscalm.invoicing.copy import refusal

FOREIGN = dict(country="DE", county="", vat_id="DE999999999", cui="FOREIGN-TEST", ro_vat_status="not_registered")
PRIVATE = FOREIGN | dict(vat_id="", cui="", private=True)


@pytest.fixture(autouse=True)
def anaf_setup(ledger):
    integrations.add(ledger, "anaf", "Synthetic ANAF", {"client_id": "fake", "client_secret": "fake"})


def app_row(L, iid):
    return next(r for r in api.state(L, "NORD")["issued"] if r["local_id"] == iid)


@pytest.mark.parametrize(
    "client,supply,expected",
    [
        (FOREIGN | dict(country="US", vat_id="", cui="", tax_id="LOCAL 123"), "RO", "not_required"),
        (PRIVATE, "RO", "required"),
        (PRIVATE, "DE", "not_required"),
        (FOREIGN | dict(vat_id="RO99999984", cui="99999984", ro_vat_status="unknown"), "DE", "required"),
        (FOREIGN | dict(efactura_registered=True), "RO", "optional"),
        ({}, "DE", "required"),
    ],
    ids=["tax-id-only", "private", "private-abroad", "ro-vat-abroad", "registered", "ro-buyer-abroad"],
)
def test_scope_and_transport(setup_invoice, home, client, supply, expected):
    L, cid = setup_invoice
    (home / "state" / "LIVE").touch()
    i.save_client(L, "NORD", PARTY | client, cid)
    iid = i.save_draft(L, "NORD", "TEST", cid, DATA | dict(supply_country=supply))
    inv = i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert spv.scope(inv) == expected
    calls = []

    def transport(*args):
        calls.append(args)
        return b'<header index_incarcare="10001"/>'

    if expected != "required":
        assert spv.auto_upload(L, iid, transport, company="NORD", today=UPLOAD_DAY)["status"] == "not_required"
        assert calls == []
    if expected == "not_required":
        with pytest.raises(i.InvoiceError, match="nu se transmite"):
            spv.upload(L, iid, transport, company="NORD")
        assert calls == [] and spv.submission(L, iid) is None
    else:
        assert spv.upload(L, iid, transport, company="NORD")["status"] == "uploaded"
        assert len(calls) == 1
    assert ubl.generate(L, iid, company="NORD")
    assert invoice_pdf.generate(L, iid, company="NORD").startswith(b"%PDF")


def test_k_required_contract(setup_invoice):
    L, _ = setup_invoice
    assert invoicing_api.workspace(L, "NORD", "2026-10-31")["required"]["client_if_k"] == ["address.county"]


def test_every_county_choice_is_accepted_by_the_engine(setup_invoice):
    L, _ = setup_invoice
    choices = invoicing_api.workspace(L, "NORD", "2026-10-31")["counties"]
    assert {c["county"] for c in choices} == i.COUNTIES
    for c in choices:
        assert i._party(PARTY | dict(county=c["county"], city=c["city"] or "Localitate"))["county"] == c["county"]


def test_refusals_reach_app_verbatim():
    for message in [
        "Pentru categoria K, lipsește regiunea clientului (BR-RO-211).",
        "Pentru un client străin înregistrat în România, lipsesc codul TVA RO și CUI-ul românesc corespunzător.",
        "Factura nu se transmite în SPV pentru această operațiune.",
    ]:
        assert refusal(message) == message


@pytest.mark.parametrize("supply", ["", "ro", "EL"])
def test_invalid_supply_cannot_disable_reporting(setup_invoice, supply):
    L, cid = setup_invoice
    with pytest.raises(i.InvoiceError, match="două litere"):
        i.save_draft(L, "NORD", "TEST", cid, DATA | dict(supply_country=supply))


@pytest.mark.parametrize(
    "registered,connected,expected_upload", [(False, True, False), (True, True, True), (True, False, False)]
)
def test_k_auto_skip_api_capability_and_existing_state(
    setup_invoice, home, sec, registered, connected, expected_upload
):
    L, cid = setup_invoice
    if connected:  # without an ANAF connection the upload is never offered
        tokens = anaf._merged({"access_token": "tok", "refresh_token": "r"}, None, datetime.now())
        assert integrations.put_oauth(L, anaf.instance(L).id, json.dumps(tokens), client_id="fake", replace=True)
    (home / "state" / "LIVE").touch()
    i.save_client(
        L,
        "NORD",
        PARTY
        | dict(
            country="DE",
            county="Berlin",
            vat_id="DE999999999",
            cui="DE999999999",
            efactura_registered=registered,
            ro_vat_status="not_registered",
        ),
        cid,
    )
    iid = i.save_draft(
        L,
        "NORD",
        "TEST",
        cid,
        DATA
        | dict(
            delivery_date="2026-10-29",
            lines=[LINE | dict(vat_category="K", vat_rate="0", exemption_reason="VATEX-EU-IC")],
        ),
    )
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)

    def no_network(*args):
        pytest.fail("Auto-upload sent a non-required invoice")

    assert spv.auto_upload(L, iid, no_network, company="NORD", today=UPLOAD_DAY)["status"] == "not_required"
    L.set_meta("invoice_upload_error:" + iid, "old failure")
    row = app_row(L, iid)
    assert row["spv"]["state"] == "not_required"
    assert row["can_upload"] is expected_upload
    assert row["pdf"] is True
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,?,?)",
            (iid, anaf.env(), "unknown", "historical"),
        )
    assert spv.auto_upload(L, iid, no_network, company="NORD", today=UPLOAD_DAY)["status"] == "unknown"
    row = app_row(L, iid)
    assert row["spv"]["state"] == "unknown" and row["can_upload"] is False


@pytest.mark.parametrize("status", [None, "registered"])
def test_registration_status_not_printed_code(setup_invoice, home, status):
    L, cid = setup_invoice
    (home / "state" / "LIVE").touch()
    profile = PARTY | dict(country="DE", county="", vat_id="DE999999999", cui="99999984")
    if status is not None:
        profile["ro_vat_status"] = status
    i.save_client(L, "NORD", profile, cid)
    # Simulate clients stored before the field existed, without a migration/write.
    if status is None:
        with L.tx() as db:
            db.execute("UPDATE invoice_clients SET data=? WHERE id=?", (json.dumps(profile), cid))
    before = L.db.total_changes
    client = invoicing_api.workspace(L, "NORD")["clients"][0]
    assert client["ro_vat_status"] == (status or "unknown")
    assert L.db.total_changes == before
    iid = i.save_draft(L, "NORD", "TEST", cid, DATA)
    inv = i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert spv.scope(inv) == "required"
    calls = []

    def transport(method, url, body):
        calls.append(url)
        return b'<header index_incarcare="10001"/>'

    assert spv.auto_upload(L, iid, transport, company="NORD", today=UPLOAD_DAY)["status"] == "uploaded"
    assert len(calls) == 1 and "extern=DA" in calls[0]


def test_lowercase_ro_identity_normalized_before_validation(setup_invoice):
    L, cid = setup_invoice
    profile = PARTY | dict(country="DE", county="", vat_id=" ro99999984 ", cui="99999984")
    i.save_client(L, "NORD", profile, cid)
    saved = invoicing_api.workspace(L, "NORD")["clients"][0]
    assert saved["vat_id"] == "RO99999984"
    with pytest.raises(i.InvoiceError, match="CUI-ul românesc"):
        i.save_client(L, "NORD", profile | dict(cui="99999985"), cid)
    with pytest.raises(i.InvoiceError, match="contrazice"):
        i.save_client(L, "NORD", profile | dict(ro_vat_status="not_registered"), cid)
    with pytest.raises(
        i.InvoiceError,
        match=r"^Lipsește înregistrarea în scopuri de TVA în România: necunoscută, înregistrat sau nestabilit și neînregistrat\.$",
    ):
        i.save_client(L, "NORD", profile | dict(ro_vat_status="no"), cid)


@pytest.mark.parametrize("category", ["K", "G"])
def test_goods_dispatch_requires_ro_supply(setup_invoice, category):
    L, cid = setup_invoice
    i.save_client(L, "NORD", PARTY | dict(country="DE", county="Berlin", vat_id="DE999999999"), cid)
    with pytest.raises(i.InvoiceError, match="locul livrării trebuie să fie România"):
        i.save_draft(
            L,
            "NORD",
            "TEST",
            cid,
            DATA
            | dict(
                supply_country="DE",
                delivery_date="2026-10-29",
                lines=[LINE | dict(vat_category=category, vat_rate="0", exemption_reason="Exempt")],
            ),
        )


@pytest.mark.parametrize("registered,expected", [(False, "not_required"), (True, "optional")])
def test_live_storno_without_submission_for_non_required_original(setup_invoice, home, registered, expected):
    L, cid = setup_invoice
    (home / "state" / "LIVE").touch()
    i.save_client(
        L,
        "NORD",
        PARTY
        | dict(
            country="DE",
            county="",
            vat_id="DE999999999",
            cui="99999984",
            ro_vat_status="not_registered",
            efactura_registered=registered,
        ),
        cid,
    )
    iid = i.save_draft(L, "NORD", "TEST", cid, DATA)
    original = i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert spv.scope(original) == expected
    assert app_row(L, iid)["can_storno"] is True
    # A submission of any non-accepted status still blocks reversal, even for optional scope.
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,?,?)",
            (iid, anaf.env(), "unknown", "test"),
        )
    assert app_row(L, iid)["can_storno"] is False
    with pytest.raises(i.InvoiceError, match="ANAF să accepte factura"):
        i.storno(L, iid, "2026-11-01", "Return", company="NORD")
    with L.tx() as db:
        db.execute("DELETE FROM invoice_submissions WHERE invoice_id=?", (iid,))
    reversal = i.storno(L, iid, "2026-11-01", "Return", company="NORD")
    assert reversal["data"]["client"] == original["data"]["client"]
    assert reversal["data"].get("supply_country", "RO") == original["data"].get("supply_country", "RO")
    assert spv.scope(reversal) == expected
    assert (
        spv.auto_upload(
            L, reversal["id"], lambda *_: pytest.fail("unexpected upload"), company="NORD", today=UPLOAD_DAY
        )["status"]
        == "not_required"
    )
    assert spv.submission(L, reversal["id"]) is None
    assert app_row(L, iid)["can_storno"] is False


def test_scope_uses_same_vat_normalization_as_identity():
    buyer = dict(country="US", vat_id=" ro 99999984 ", ro_vat_status="not_registered")
    assert i._identity(buyer) == "99999984"
    assert spv.scope(dict(data=dict(client=buyer, supply_country="US"))) == "required"


# ---- identity: a draft never brings its own snapshots, a sent message links to one invoice or none


@pytest.mark.parametrize(
    "data",
    [{"currency": "RON"}] + [DATA | {key: "untrusted"} for key in ("seller", "client", "billing_ref", "totals")],
    ids=["no-lines", "seller", "client", "billing_ref", "totals"],
)
def test_a_draft_cannot_skip_its_lines_or_supply_its_snapshots(setup_invoice, data):
    with pytest.raises(i.InvoiceError):
        draft(setup_invoice, data)


@pytest.mark.parametrize("candidates", [2, 1])
def test_ambiguous_local_identity_not_linked(setup_invoice, monkeypatch, candidates):
    L, cid = setup_invoice
    i.create_series(L, "NORD", "SECOND")
    ids = []
    for series in ["TEST", "SECOND"]:
        ids.append(i.save_draft(L, "NORD", series, cid, DATA))
        i.issue(L, ids[-1], DATA["issue_date"], company="NORD", version=1)
    # Two local invoices with the same identity as the sent one: linking either could hide the other's debt.
    monkeypatch.setattr(
        i, "invoice_number", lambda inv: "SAME-1" if candidates == 2 or inv["id"] == ids[0] else "OTHER-1"
    )
    client = i.invoice(L, ids[0], company="NORD")["data"]["client"]
    row = dict(
        message_id="123",
        company="NORD",
        number="SAME-1",
        issue_date=DATA["issue_date"],
        currency="RON",
        total="121.00",
        client_cui=client["cui"],
        client_tax_id=tax_token(client["vat_id"]),
        raw_path="/synthetic/not-opened",
        xml_sha="synthetic",
        status="open",
        created_at="2026-10-31",
    )
    with L.tx() as db:
        db.execute(f"INSERT INTO issued({','.join(row)}) VALUES({','.join('?' for _ in row)})", list(row.values()))
    i.link_sent(L, "NORD", "123", PARTY["cui"], "SAME-1", DATA["issue_date"])
    assert L.db.execute("SELECT count(*) FROM invoice_sent_links WHERE message_id='123'").fetchone()[0] == (
        candidates == 1
    )


def test_store_and_link_are_atomic(setup_invoice, monkeypatch):
    """Collected as issued, then its SPV copy arrives: evidence linked to it, never a second collectable record."""
    L, cid = setup_invoice
    seller = PARTY | dict(cui=companies.get("NORD").cui)
    seller["vat_id"] = "RO" + seller["cui"]
    i.save_seller(L, "NORD", seller)
    iid = draft(setup_invoice)
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    own = i.import_id("local", iid)
    assert L.mark_issued_collected(own, "statement", "tx-1")
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("invoice.xml", ubl.generate(L, iid, company="NORD"))
    original = i.link_sent

    def fail_before_link(L, co, mid, *args):
        assert L.issued(mid) is not None
        raise RuntimeError("interrupted")

    monkeypatch.setattr(i, "link_sent", fail_before_link)
    with pytest.raises(RuntimeError):
        issued.store(L, "NORD", dict(id="123", data_creare=DATA["issue_date"]), raw.getvalue())
    assert L.issued("123") is None and i.sent_links(L, "NORD") == {own: iid}
    assert spv.submission(L, iid, environment="prod") is None  # fiscalm's own row is no SPV acceptance
    monkeypatch.setattr(i, "link_sent", original)
    issued.store(L, "NORD", dict(id="123", data_creare=DATA["issue_date"]), raw.getvalue())
    assert (L.issued("123")["status"], L.issued(own)["status"]) == ("duplicate", "collected")
    assert i.sent_links(L, "NORD") == {own: iid}
    assert L.db.execute("SELECT invoice_id FROM invoice_sent_links WHERE message_id='123'").fetchone()[0] == iid
    assert spv.submission(L, iid, environment="prod")["status"] == "accepted"
    assert L.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 1
