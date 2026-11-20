import io
import json
from contextlib import contextmanager
from urllib.error import HTTPError

import pytest
from test_invoicing import DATA, PARTY, draft, setup_invoice as setup_invoice  # noqa: F401

from fiscalm import bnr
from fiscalm.efactura import public as a
from fiscalm.invoicing import engine as invoicing, workspace as invoicing_api


def xml(day, value="5", multiplier="1"):
    return f'<DataSet xmlns="http://www.bnr.ro/xsd"><Body><Cube date="{day}"><Rate currency="EUR" multiplier="{multiplier}">{value}</Rate></Cube></Body></DataSet>'.encode()


def test_rate_weekend_cache_and_multiplier(ledger):
    calls = []

    def fetch(url):
        calls.append(url)
        return xml("2026-11-02") if url == bnr.DAILY else xml("2026-10-30", "500", "100")

    assert bnr.rate(ledger, "EUR", "2026-10-31", fetch) == ("5", "2026-10-30")
    assert calls == [bnr.DAILY, "https://curs.bnr.ro/files/xml/years/nbrfxrates2026.xml"]  # the date's own year
    assert bnr.rate(ledger, "EUR", "2026-10-31", lambda _: pytest.fail("cache missed")) == ("5", "2026-10-30")
    assert bnr.rate(ledger, "RON", "2026-10-31", fetch) == ("1", "2026-10-31")


def test_january_uses_last_years_feed_even_when_this_years_file_is_missing(monkeypatch, ledger):
    @contextmanager
    def open_url(url, timeout):
        if "2026.xml" in url:
            raise HTTPError(url, 404, "missing", {}, None)
        yield io.BytesIO(xml("2025-12-31") if "2025.xml" in url else xml("2026-01-02"))

    monkeypatch.setattr(bnr.urllib.request, "urlopen", open_url)
    assert bnr.rate(ledger, "EUR", "2026-01-01", bnr.fetch_xml) == ("5", "2025-12-31")
    with pytest.raises(bnr.UnsupportedCurrency):  # a complete feed that does not publish the currency
        bnr.rate(ledger, "USD", "2026-01-01", lambda _: xml("2026-01-02"))


@pytest.mark.parametrize(
    "raw",
    [
        b"<!DOCTYPE x><x/>",
        b"<html/>",
        xml("2026-10-30", "NaN"),
        xml("2026-10-30", "-5"),
        xml("bad"),
        xml("2026-10-30", "1", "0"),
    ],
)
def test_bad_rates(raw):
    with pytest.raises(bnr.RateError):
        bnr.parse(raw, "EUR")


def response():
    return dict(
        cod=200,
        notFound=[],
        found=[
            dict(
                date_generale=dict(
                    cui=99999984,
                    data="2026-10-31",
                    denumire="Fictivă SRL",
                    nrRegCom="J00/0/2026",
                    statusRO_e_Factura=True,
                    stare_inregistrare="INREGISTRAT",
                ),
                inregistrare_scop_Tva=dict(scpTVA=True),
                inregistrare_RTVAI=dict(statusTvaIncasare=False),
                stare_inactiv=dict(statusInactivi=False),
                adresa_sediu_social=dict(
                    sdenumire_Strada="Strada Exemplu",
                    snumar_Strada="1",
                    sdenumire_Localitate="Brașov",
                    scod_JudetAuto="BV",
                    scod_Postal="000000",
                    stara="",  # what the register answers for a registered office in Romania
                ),
            )
        ],
    )


def test_lookup_and_fill(ledger):
    def fetch(url, body):
        assert url == a.URL
        assert json.loads(body) == [dict(cui=99999984, data="2026-10-31")]
        return json.dumps(response()).encode()

    profile = a.lookup("RO99999984", "2026-10-31", fetch)
    assert (profile["county"], profile["vat_id"], profile["street"]) == ("RO-BV", "RO99999984", "Strada Exemplu 1")
    cid = invoicing.fill_client(ledger, "NORD", "99999984", "2026-10-31", fetch)
    assert invoicing.fill_client(ledger, "NORD", "99999984", "2026-10-31", fetch) == cid
    invoicing.save_seller(ledger, "NORD", profile | dict(bank="Banca Fictivă", iban="RO24BTRLRONCRT0000000001"))
    assert ledger.db.execute("SELECT count(*) FROM invoice_sellers").fetchone()[0] == 1


@pytest.mark.parametrize(
    "general,address,expected",
    [
        (
            {},
            dict(scod_JudetAuto="B", sdenumire_Localitate="Sector 3 Mun. București", sdetalii_Adresa="corp A"),
            dict(city="SECTOR3", street="Strada Exemplu 1 corp A"),
        ),
        ({}, dict(stara="Romania"), dict(country="RO", county="RO-BV")),
        (
            dict(denumire=["bad"], nrRegCom={}, telefon=123),
            dict(sdenumire_Localitate={}, scod_Postal=1234, sdetalii_Adresa=["bad"]),
            dict(name="", reg_com="", city="", phone="123", postal_code="1234", street="Strada Exemplu 1"),
        ),  # only strings
    ],
)
def test_lookup_profile_fields(general, address, expected):
    data = response()
    data["found"][0]["date_generale"].update(general)
    data["found"][0]["adresa_sediu_social"].update(address)
    profile = a.lookup("99999984", "2026-10-31", lambda *_: json.dumps(data).encode())
    assert {k: profile[k] for k in expected} == expected
    assert all(isinstance(v, (str, bool)) for v in profile.values())


@pytest.mark.parametrize("country", ["GERMANIA", None, 0, [], ["ROMANIA"], {}, "absent"])
def test_lookup_refuses_a_country_that_is_not_romania(country):
    data = response()
    office = data["found"][0]["adresa_sediu_social"]
    office.pop("stara") if country == "absent" else office.update(stara=country)
    with pytest.raises(a.AnafLookupError):
        a.lookup("99999984", "2026-10-31", lambda *_: json.dumps(data).encode())


@pytest.mark.parametrize("kind", ["missing", "duplicate", "wrong", "date", "flag", "inactive", "badjson"])
def test_lookup_refusals(ledger, kind):
    data = response()
    if kind == "missing":
        data["found"] = []
    if kind == "duplicate":
        data["found"] *= 2
    if kind == "wrong":
        data["found"][0]["date_generale"]["cui"] = 99999992
    if kind == "date":
        data["found"][0]["date_generale"]["data"] = "2026-10-30"
    if kind == "flag":
        data["found"][0]["inregistrare_scop_Tva"]["scpTVA"] = "false"
    if kind == "inactive":
        data["found"][0]["stare_inactiv"]["statusInactivi"] = True
    with pytest.raises((a.AnafLookupError, invoicing.InvoiceError)):
        invoicing.fill_client(
            ledger,
            "NORD",
            "99999984",
            "2026-10-31",
            lambda *_: b"no" if kind == "badjson" else json.dumps(data).encode(),
        )
    assert ledger.db.execute("SELECT count(*) FROM invoice_clients").fetchone()[0] == 0


def test_previous_day_and_future_date(ledger):
    feeds = xml("2026-10-30", "6").replace(
        b"</Body>", xml("2026-10-29", "5").split(b"<Body>")[1].split(b"</Body>")[0] + b"</Body>"
    )
    assert bnr.rate(ledger, "EUR", "2026-10-30", lambda _: feeds) == ("5", "2026-10-29")
    with pytest.raises(bnr.RateError):
        bnr.rate(ledger, "EUR", "2999-01-01", lambda _: pytest.fail("network"))
    with pytest.raises(bnr.RateError):  # the caller's today decides what is in the future
        bnr.rate(ledger, "EUR", "2026-10-30", lambda _: pytest.fail("network"), today="2026-10-29")


def test_incomplete_midyear_source_refuses_instead_of_caching(ledger):
    with pytest.raises(bnr.RateError):
        bnr.rate(ledger, "EUR", "2026-10-30", lambda _: xml("2026-10-30"), today="2026-10-30")
    assert not ledger.db.execute("SELECT * FROM invoice_rates").fetchall()


def test_cached_inactivity_blocks_save_and_issue(setup_invoice, monkeypatch):
    L, cid = setup_invoice
    iid = draft(setup_invoice)
    inactive = PARTY | dict(cui="99999992", vat_id="RO99999992", inactive=True)
    monkeypatch.setattr(a, "lookup", lambda *_, **__: inactive)
    assert invoicing_api.act(L, "client-lookup", "NORD", dict(cui="99999992"))["ok"]
    assert a.cached(L, "99999992")[0]["inactive"]
    claim = inactive | dict(inactive=False)
    result = invoicing_api.act(L, "client-save", "NORD", dict(id=cid, client=invoicing_api.party_out(claim)))
    assert not result["ok"] and "inactiv" in result["message"]
    with pytest.raises(invoicing.InvoiceError, match="inactiv"):
        invoicing.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    assert invoicing.invoice(L, iid, company="NORD")["status"] == "draft"


@pytest.mark.parametrize("identity", [dict(cui="", vat_id="RO99999984"), dict(cui=" RO 99999984 ", vat_id="")])
def test_cached_inactivity_survives_a_failed_lookup_and_any_spelling(ledger, monkeypatch, identity):
    profile = PARTY | dict(inactive=True)
    a.remember(ledger, profile, "2026-10-31")
    monkeypatch.setattr(a, "lookup", lambda *_, **__: (_ for _ in ()).throw(a.AnafLookupError("unavailable")))
    assert not invoicing_api.act(ledger, "client-lookup", "NORD", dict(cui=PARTY["cui"]))["ok"]
    with pytest.raises(invoicing.InvoiceError, match="inactiv"):
        invoicing.save_client(ledger, "NORD", PARTY | identity | dict(inactive=False))
    assert invoicing.save_client(ledger, "NORD", PARTY | dict(cui="99999976", vat_id="RO99999976"))
    a.remember(ledger, profile | dict(inactive=False), "2026-10-30")  # an older answer changes nothing
    with pytest.raises(invoicing.InvoiceError, match="inactiv"):
        invoicing.save_client(ledger, "NORD", profile | dict(inactive=False))
    a.remember(ledger, profile | dict(inactive=False), "2026-11-01")
    assert invoicing.save_client(ledger, "NORD", profile | dict(inactive=False))


def test_a_rate_refusal_reaches_the_app(setup_invoice, monkeypatch):
    L, cid = setup_invoice
    with pytest.raises(bnr.RateError) as exc:
        bnr.parse(b"<!DOCTYPE DataSet><DataSet/>", "EUR")
    monkeypatch.setattr(bnr, "rate", lambda *a, **k: (_ for _ in ()).throw(bnr.RateError(str(exc.value))))
    body = DATA | dict(series="TEST", client_id=cid, currency="EUR")
    assert invoicing_api.act(L, "draft-save", "NORD", body)["message"] == str(exc.value)
