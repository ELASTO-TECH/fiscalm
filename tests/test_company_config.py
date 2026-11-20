"""Company onboarding uses only synthetic registries and isolated config files."""

import csv
import http.client
import importlib.util
import json
import os
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Event

import pikepdf
import pytest
from conftest import go_live
from firms import entries, set_entries
from payment_cases import download_batch
from test_api import _api, _get_json, server as server  # noqa: F401
from test_efactura import anaf_zip, msg, ubl
from test_issued import _credit, _st

from fiscalm import cli, companies, company_config as cc, integrations, recon, secrets
from fiscalm.api import actions, mcp, schema as api_schema, state as api, v1 as api_v1
from fiscalm.bank import statements
from fiscalm.config_errors import ConfigError
from fiscalm.efactura import inbox as efactura, public as anaf_public
from fiscalm.invoicing import issued, workspace
from fiscalm.ledger import is_live
from fiscalm.mail import imap as imapread, invoices as mail_invoices
from fiscalm.payments import btgo, engine as pay, orders as ops
from fiscalm.payments.orders import PaymentItem

TODAY = date(2026, 1, 1)
NORD_RON = "RO24BTRLRONCRT0000000001"  # NORD's paying account in the example config
NORD_EUR = "RO71BTRLEURCRT0000000001"


@pytest.fixture
def config(home, monkeypatch):
    monkeypatch.setattr(
        cc,
        "lookup",
        lambda cui, today: dict(
            cui=cui, legal_name="Firma Exemplu SRL", address="Strada Exemplu 1", vat_payer=True, inactive=False
        ),
    )
    return home


def fake_iban(suffix="0001"):
    """An invented account at BT (the bank code decides the payment file format), with a computed checksum."""
    body = "BTRL" + "0" * 12 + suffix
    digits = "".join(str(int(c, 36)) for c in body + "RO00")
    return f"RO{98 - int(digits) % 97:02d}" + body


OLD = fake_iban("0008")


def ron(iban, **extra):
    return dict(iban=iban, currency="RON", **extra)


def set_accounts(config, *accounts, key="NORD"):
    """Give one configured company exactly these accounts, in the config's `accounts` shape."""
    cfg = entries()
    next(c for c in cfg["companies"] if c["key"] == key)["accounts"] = list(accounts)
    set_entries(cfg)


def replace_old_account(config):
    """NORD paid from OLD and has replaced it with fake_iban(); OLD stays NORD's as a retired account."""
    set_accounts(config, ron(OLD, pays=True))
    cc.bank(key="NORD", iban=fake_iban(), replace=True, today=TODAY)


def payment(company="NORD", ref="EX1", kind="op"):
    return PaymentItem(
        company,
        kind,
        "Furnizor Exemplu",
        fake_iban("0002"),
        "ZZZZROBU",
        "",
        Decimal("1.00"),
        "Exemplu",
        TODAY,
        ref,
        "",
        "fake" + ref,
    )


def encrypted_pdf(path, password):
    with pikepdf.Pdf.new() as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(owner=password, user=password))
    return path


def btgo_errors(**cells):
    row = dict.fromkeys(btgo.HEADER, "") | {"OrderNumber": "1"} | cells
    return btgo.validate_text(",".join(btgo.HEADER) + "\r\n" + ",".join(row.values()) + "\r\n", "NORD")


def test_add_update_and_reload(config, ledger, server):
    before = entries()
    code, result = _api(server, "company-lookup", {"cui": "12345674"})
    assert code == 200 and result["ok"] and result["done"]["legal_name"] == "Firma Exemplu SRL"
    assert entries() == before
    code, result = _api(server, "company-add", {"cui": "RO12345674", "name": "Exemplu"})
    assert code == 200 and result["ok"] and result["message"] == "Firma a fost adăugată."
    key = result["done"]["key"]
    assert key == "EXEMPLU" and result["done"]["palette"] == "blue"
    assert key in [c["key"] for c in result["state"]["companies"]]
    assert api.state(ledger, key)["co"] == key
    _, listing = _get_json(server, "/web/api/companies")
    assert {k: v for k, v in listing["companies"][-1].items() if k != "banks"} == result["done"] | {
        "has_data": False,
        "has_ron_account": False,
        "iban_last4": "",
        "accounts": [],
        "aliases": [],
        "alias_suggestion": "Firma Exemplu",
        "history": {"start": None, "first_statement": None, "issued": 0, "received": 0},
    }
    assert listing["limits"]["reserved_keys"] == ["ALL"]
    assert _get_json(server, "/c/wrong/api/companies")[0] == 404
    _, result = _api(server, "company-update", {"key": key, "name": "Alt nume", "palette": "rose"})
    assert result["ok"] and result["message"] == "Firma a fost actualizată."
    assert companies.get(key).name == "Alt nume" and companies.get(key).cui == "12345674"
    entries()
    ledger.db.execute("INSERT INTO packs(company, month, status) VALUES(?,?,?)", (key, "2026-01", "built"))
    ledger.db.commit()
    assert cc.listing(ledger)["companies"][-1]["has_data"]


def test_add_asks_anaf_again_and_refuses_an_inactive_or_unconfirmed_firm(config, monkeypatch):
    calls = []

    def lookup(cui, today):
        calls.append(cui)
        return dict(cui=cui, legal_name="Exemplu SRL", address="Strada 1", vat_payer=True, inactive=len(calls) > 1)

    monkeypatch.setattr(cc, "lookup", lookup)
    old = entries()
    assert actions.act("company-lookup", "", {"cui": "12345674"})["ok"]
    result = actions.act("company-add", "", {"cui": "12345674"})  # the preview said active; ANAF now says inactive
    assert not result["ok"] and "inactivă" in result["message"] and calls == ["12345674", "12345674"]
    monkeypatch.setattr(cc, "lookup", lambda cui, today: (_ for _ in ()).throw(cc.AnafSilent(cc.SILENT)))
    silent = actions.act("company-add", "", {"cui": "12345674"})
    assert (silent["ok"], silent["message"], silent["done"]) == (False, cc.SILENT, {"retry": True})
    assert entries() == old


def test_duplicate_cui_and_key_collisions(config):
    cui = next(iter(companies.all().values())).cui
    with pytest.raises(cc.CompanyError, match="deja configurată"):
        cc.change(today=TODAY, cui=cui)
    first = cc.change(today=TODAY, cui="12345674", name="ALL")
    second = cc.change(today=TODAY, cui="12345682", name="ALL")
    assert first["key"] == "ALL_1" and second["key"] == "ALL_2"
    assert companies.KEY_RE.fullmatch(second["key"])


@pytest.mark.parametrize(
    "action,body",
    [
        ("company-add", {"cui": "12345674", "path": "/tmp/no"}),
        ("company-add", {"cui": "12345674", "legal_name": "Forged"}),
        ("company-update", {"key": "ALL", "name": "x"}),
        ("company-update", {"key": "EXEMPLU", "cui": "12345674"}),
        ("company-add", {"cui": "ALL"}),
        ("company-add", {"cui": "12345674", "palette": "invalid"}),
        ("company-add", {"cui": "12345674", "name": ""}),
        ("company-add", {}),
    ],
)
def test_bad_requests(config, server, action, body):
    before = entries()
    assert not _api(server, action, body)[1]["ok"]
    assert entries() == before


def test_company_settings_are_configuration_for_the_app_only():
    """Allowed before going live (nothing outward), never offered on the scoped API."""
    for action in (
        "company-lookup",
        "company-add",
        "company-update",
        "company-bank",
        "company-account",
        "company-statement-password",
        "company-alias-add",
        "company-alias-remove",
    ):
        assert action in actions.LOCAL_ACTIONS and action not in actions.API_ACTIONS


@pytest.mark.parametrize(
    "cuis,outcome",
    [
        (("12345674", "12345682"), {"SAME", "SAME_1"}),
        (("12345674", "12345674"), {"SAME", "Firma este deja configurată."}),
    ],
)
def test_two_racing_adds_save_each_firm_once_under_its_own_key(config, cuis, outcome):
    def add(cui):
        try:
            return cc.change(today=TODAY, cui=cui, name="Same")["key"]
        except cc.CompanyError as e:
            return str(e)

    with ThreadPoolExecutor(2) as pool:
        assert set(pool.map(add, cuis)) == outcome
    assert len(entries()["companies"]) == 2 + len(set(cuis))


def test_cli_parity(config, ledger, capsys):
    a = cli.build_parser().parse_args(["company", "add", "--cui", "12345674"])
    assert a.fn(a, ledger) == 0
    assert json.loads(capsys.readouterr().out)["key"] == "FIRMA_EXEMPLU_SRL"


def test_missing_bank_refuses_payment_files(config):
    key = cc.change(today=TODAY, cui="12345674")["key"]
    with pytest.raises(btgo.CsvError, match="IBAN"):
        btgo.build_rows([payment(company=key)], TODAY)
    assert companies.company_by_iban("") is None


@pytest.mark.parametrize(
    "variant",
    [
        "ok",
        "no-cod",
        "bad-cod",
        "null-cod",
        "inactive",
        "down",
        "not-found",
        "ambiguous",
        "wrong-cui",
        "missing-flag",
        "long-name",
        "deep-json",
    ],
)
def test_anaf_lookup(monkeypatch, variant):
    row = {
        "date_generale": {
            "cui": 12345682 if variant == "wrong-cui" else 12345674,
            "data": "2026-01-01",
            "denumire": "x" * 201 if variant == "long-name" else "Exemplu SRL",
            "adresa": "Strada 1",
            "nrRegCom": "J00/123/2026",
            "statusRO_e_Factura": True,
            "stare_inregistrare": "INREGISTRAT",
        },
        "adresa_sediu_social": {
            "stara": "",
            "sdenumire_Strada": "Strada",
            "snumar_Strada": "1",
            "scod_JudetAuto": "CJ",
            "sdenumire_Localitate": "Localitate Exemplu",
        },
        "inregistrare_RTVAI": {"statusTvaIncasare": False},
        "inregistrare_scop_Tva": {"scpTVA": True},
        "stare_inactiv": {"statusInactivi": variant == "inactive"},
    }
    if variant == "missing-flag":
        del row["stare_inactiv"]["statusInactivi"]
    envelope = {"found": [row] * (2 if variant == "ambiguous" else 1), "notFound": []}
    if variant == "not-found":
        envelope = {"found": [], "notFound": [12345674]}
    if variant != "no-cod":
        envelope["cod"] = 500 if variant == "bad-cod" else None if variant == "null-cod" else 200
    body = b"[" * 2000 + b"]" * 2000 if variant == "deep-json" else json.dumps(envelope).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return body

    def fetch(request, timeout):
        assert request.full_url == anaf_public.URL and timeout == 20
        if variant == "down":
            raise OSError("unavailable")
        return Response()

    monkeypatch.setattr(anaf_public.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(anaf_public.time, "sleep", lambda _: None)
    if variant in ("ok", "no-cod", "inactive"):
        result = anaf_public.lookup_company("RO12345674", "2026-01-01")
        profile = result.pop("invoice_profile")
        assert profile["vat_payer"] is True and profile["street"] == "Strada 1"
        assert (profile["reg_com"], profile["county"], profile["country"]) == ("J00/123/2026", "RO-CJ", "RO")
        assert result == dict(
            cui="12345674", legal_name="Exemplu SRL", address="Strada 1", vat_payer=True, inactive=variant == "inactive"
        )
    else:
        with pytest.raises(cc.CompanyError) as refused:
            anaf_public.lookup_company("12345674", "2026-01-01")
        unknown = variant == "not-found"
        assert (type(refused.value), str(refused.value)) == (
            (cc.CompanyError, cc.UNKNOWN.format(cui="12345674")) if unknown else (cc.AnafSilent, cc.SILENT)
        )


def test_development_engine_never_reaches_anaf(home, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "bin"))
    spec = importlib.util.spec_from_file_location("dev_engine", Path(__file__).parents[1] / "bin/dev-engine.py")
    dev_engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dev_engine)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("network"))
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)  # each patched name is restored after the test
    monkeypatch.setattr(http.client.HTTPConnection, "set_tunnel", http.client.HTTPConnection.set_tunnel)
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setattr(anaf_public.time, "sleep", lambda _: None)
    dev_engine.offline_anaf()
    assert "HTTP_PROXY" not in os.environ  # direct: every host, a redirect's too, is resolved here
    assert not any(
        isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in urllib.request.build_opener().handlers
    )
    found = anaf_public.lookup_company("RO12345674", "2026-01-01")
    assert (found["legal_name"], found["vat_payer"], found["inactive"]) == ("Firma Inventată 12345674 SRL", True, False)
    for host in (
        "api.anaf.ro",
        "API.ANAF.RO.",
        "anaf.ro",
        b"webservicesp.anaf.ro",
    ):  # what a redirect or any other opener resolves
        with pytest.raises(OSError, match="never calls ANAF"):
            socket.getaddrinfo(host, 443)
    with pytest.raises(OSError, match="never calls ANAF"):
        http.client.HTTPSConnection("proxy.example").set_tunnel("logincert.anaf.ro.", 443)


def test_every_entry_point_reads_an_edit_made_by_another_process(config, ledger, server):
    _get_json(server, "/web/api/companies")
    added = cc.change(today=TODAY, cui="12345674")
    code, state = _get_json(server, "/web/api/state?co=" + added["key"])
    assert code == 200 and state["co"] == added["key"]
    cfg = entries()
    cfg["companies"][0]["name"] = "V1 name"
    set_entries(cfg)
    route, params = api_v1.match("/api/v1/companies", "GET")
    assert any(c["name"] == "V1 name" for c in api_v1.execute(ledger, route, params, {})["data"])
    cfg["companies"][0]["name"] = "MCP name"
    set_entries(cfg)
    mcp.dispatch(ledger, {}, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert companies.get("NORD").name == "MCP name"


def test_scoped_v1_does_not_advertise_or_accept_install_actions(ledger):
    spec = api_schema.openapi()
    enum = spec["paths"]["/api/v1/companies/{co}/actions/{action}"]["post"]["parameters"][1]["schema"]["enum"]
    assert "company-add" not in enum
    with pytest.raises(api_v1.Error):
        api_v1.run_action(ledger, "NORD", "company-add", {"cui": "12345674"})


@pytest.mark.parametrize("source", ["ledger", "integration", "meta", "directory", "logs", "efactura", "_catchup-old"])
def test_retired_keys_are_never_reused(config, ledger, home, source):
    key = "LOGS" if source == "logs" else "RETIRED"  # fiscalm's own folders count too, whatever their case
    if source == "ledger":
        ledger.db.execute("INSERT INTO packs(company,month,status) VALUES(?, '2026-01', 'built')", (key,))
        ledger.db.commit()
    elif source == "integration":
        ledger.db.execute(
            "INSERT INTO integrations VALUES('retired','bank','Fake',?,0,0,'now','now')",
            (json.dumps({"company": key}),),
        )
        ledger.db.commit()
    elif source == "meta":
        ledger.set_meta("btgo_import:" + key.lower() + ":2026-01", "retired")
    else:
        (home / {"directory": key, "logs": "logs"}.get(source, f"{source}/{key}")).mkdir(parents=True, exist_ok=True)
    assert cc.change(cui="12345674", name=key, today=TODAY)["key"] == key + "_1"


def test_action_scope_types_cui_and_audit(config, server, monkeypatch):
    messages = []
    monkeypatch.setattr(actions, "log", messages.append)
    code, result = _api(server, "company-add", {"cui": " ro 12345674 ", "name": "Exemplu", "co": "ignored"})
    assert code == 200 and result["ok"] and result["state"]["co"] == "ALL"
    assert messages == ["company-add EXEMPLU"]
    assert _api(server, "company-add", {"cui": 12345682})[0] == 400
    assert not _api(server, "company-add", {"cui": "12345670"})[1]["ok"]


def test_statement_password_is_set_from_firme_write_only_in_shadow(config, server, monkeypatch, sec):
    messages = []
    monkeypatch.setattr(actions, "log", messages.append)
    password = "parola-inventata-1"

    def bt():
        banks = _get_json(server, "/web/api/companies")[1]["companies"][0]["banks"]
        assert "statement_password" not in next(b for b in banks if b["bank"] == "ing")
        return next(b for b in banks if b["bank"] == "bt")["statement_password"]

    assert bt() is False
    code, result = _api(server, "company-statement-password", {"key": "NORD", "bank": "bt", "password": password})
    assert code == 200 and result["ok"] and not is_live() and bt() is True
    assert secrets.bt_statement_password("NORD") == password
    assert messages == ["web: company-statement-password NORD bt"]
    assert password not in json.dumps(result) + json.dumps(_get_json(server, "/web/api/companies")[1])
    refused = _api(server, "integration-add", {"kind": "bank", "name": "X", "f_bank": "bt", "f_company": "NORD"})[1]
    assert not refused["ok"] and integrations.listing(integrations.Ledger()) == []


@pytest.mark.parametrize(
    "body",
    [
        {"key": "UNKNOWN", "bank": "bt", "password": "x"},
        {"key": "NORD", "bank": "ing", "password": "x"},
        {"key": "NORD", "bank": "bt", "password": ""},
        {"key": "NORD", "bank": "bt", "password": "x", "co": "NORD", "path": "/no"},
    ],
)
def test_statement_password_refusals_save_nothing(config, server, sec, body):
    assert not _api(server, "company-statement-password", body)[1]["ok"]
    assert integrations.enabled(integrations.Ledger(), "bank") == []


def test_unconfigured_statement_password_is_skipped(config, ledger, home, monkeypatch, tmp_path):
    monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "secrets"))
    for key in companies.all():
        integrations.add(
            ledger, "bank", "BT " + key, {"bank": "bt", "company": key, "statement_password": "configured"}
        )
    path = encrypted_pdf(tmp_path / "foreign.pdf", "foreign")
    with pytest.raises(statements.StatementError):
        statements.open_encrypted(path)
    added = cc.change(cui="12345674", today=TODAY)["key"]
    with pytest.raises(statements.StatementError):
        statements.open_encrypted(path)
    integrations.add(ledger, "bank", "BT " + added, {"bank": "bt", "company": added, "statement_password": "other"})
    with pytest.raises(statements.StatementError):
        statements.open_encrypted(path)


def test_configured_bank_with_missing_secret_fails_closed(ledger, monkeypatch, tmp_path):
    monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "empty"))
    bank = integrations.Instance(
        "fake", "bank", {"bank": "bt", "company": "NORD"}, versions={"statement_password": "missing"}
    )
    monkeypatch.setattr(integrations, "enabled", lambda L, kind: [bank])
    assert secrets.has_statement_password("NORD")
    with pytest.raises(statements.SecretsUnavailable):
        statements.open_encrypted(tmp_path / "unused.pdf")


def test_missing_statement_password_retries_mail(config, ledger, home, monkeypatch, tmp_path):
    monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "empty"))
    ledger.db.execute(
        "INSERT INTO integrations VALUES('disabled','bank','Fake',?,0,0,'now','now')",
        (json.dumps({"company": "NORD"}),),
    )  # a disabled bank, no password
    ledger.db.commit()
    path = encrypted_pdf(tmp_path / "allmerge.pdf", "fake")
    with pytest.raises(statements.SecretsUnavailable):
        statements.open_encrypted(path)
    go_live(home)
    sender = next(s for c in companies.all().values() for s in c.statement_senders)
    message = imapread.Message(
        "a",
        "k",
        sender,
        sender.lower(),
        "s",
        "2026-01-01",
        (imapread.Attachment("pdf", path.read_bytes(), "allmerge.pdf"),),
    )
    with pytest.raises(RuntimeError):  # not ingested: the reader keeps the mail to try again
        mail_invoices.handle(ledger, message)
    assert not ledger.documents(kind="statement")


@pytest.mark.parametrize("cui", ["RO-12345674", " ro 12345674 ", "ro​12345674", "﻿RO 1234-5674‍"])
def test_cui_paste_normalization(cui):
    assert cc.normalize_cui(cui) == "12345674"


@pytest.mark.parametrize("cui", [12345674, "RO00"])
def test_cui_normalization_refuses_non_text_and_zero(cui):
    with pytest.raises(cc.CompanyError):
        cc.normalize_cui(cui)


@pytest.fixture
def bank_config(config):
    """No company has a paying account yet; NORD keeps its EUR one."""
    cfg = entries()
    for c in cfg["companies"]:
        c["accounts"] = [a for a in c["accounts"] if not a.get("pays")]
    set_entries(cfg)
    return config


@pytest.mark.usefixtures("bank_config")
@pytest.mark.parametrize("live", [False, True])
def test_company_bank_shadow_listing_audit_and_preservation(config, ledger, server, monkeypatch, home, live):
    messages = []
    monkeypatch.setattr(actions, "log", messages.append)
    iban = fake_iban()
    if live:
        go_live(home)
    entries()
    code, result = _api(
        server, "company-bank", {"key": "NORD", "iban": " ".join(iban.lower()[i : i + 4] for i in range(0, 24, 4))}
    )
    assert code == 200 and result["ok"]
    assert result["message"] == "Contul firmei a fost salvat."
    assert result["done"] == {"key": "NORD", "has_ron_account": True, "iban_last4": "0001"}
    assert is_live() == live
    cfg = entries()
    # the EUR account and unknown fields are kept
    assert {(a["iban"], a["currency"], a.get("pays", False)) for a in cfg["companies"][0]["accounts"]} == {
        (iban, "RON", True),
        (NORD_EUR, "EUR", False),
    }
    assert messages == ["company-bank NORD iban_last4=0001"]
    _, listing = _get_json(server, "/web/api/companies")
    assert listing["companies"][0]["has_ron_account"]
    assert listing["companies"][0]["iban_last4"] == "0001"
    assert iban not in json.dumps(listing) + json.dumps(result) + "".join(messages)
    assert companies.company_by_iban(iban).key == "NORD"
    assert not pay.bank_change_blocked(ledger, "NORD")


@pytest.mark.parametrize(
    "body",
    [
        {"key": "NORD", "iban": "RO00ZZZZ0000000000000001"},
        {"key": "NORD", "iban": ""},
        {"key": "NORD", "iban": "GB82WEST12345698765432"},
        {"key": "UNKNOWN", "iban": fake_iban()},
        {"key": "NORD"},
        {"key": "NORD", "iban": fake_iban(), "path": "/no"},
    ],
)
@pytest.mark.usefixtures("bank_config")
def test_company_bank_invalid(config, server, body):
    before = entries()
    assert not _api(server, "company-bank", body)[1]["ok"]
    assert entries() == before


@pytest.mark.parametrize("pending", ["download", "missing", "journal", "orphan", "legacy"])
@pytest.mark.usefixtures("bank_config")
def test_company_bank_unresolved_named_file(config, ledger, home, pending):
    if pending in ("orphan", "legacy"):
        path = pay.to_pay_dir() / ("payments-NORD.csv" if pending == "orphan" else "payments-NORD-2026-01.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    else:
        ident = ledger.create_payment_file("NORD", "Unresolved")
        path = pay.named_path(ledger.payment_file(ident))
        if pending == "journal":
            ledger.set_meta(f"payment_import:{ident}", "{}")
        else:
            with ledger.tx() as db:
                db.execute("UPDATE payment_files SET path=? WHERE id=?", (str(path), ident))
            if pending == "download":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
    before = entries()
    result = actions.act("company-bank", "", {"key": "NORD", "iban": fake_iban()})
    assert not result["ok"] and entries() == before


@pytest.mark.usefixtures("bank_config")
def test_company_bank_rechecks_after_payment_lock(config, ledger):
    entered = Event()

    def change():
        entered.set()
        return actions.act("company-bank", "", {"key": "NORD", "iban": fake_iban()})

    with ThreadPoolExecutor(1) as pool:
        with pay.pay_lock():
            future = pool.submit(change)
            assert entered.wait(5)
            ident = ledger.create_payment_file("NORD", "Concurrent import")
            ledger.set_meta(f"payment_import:{ident}", "{}")
        assert not future.result(timeout=5)["ok"]


def test_payment_lock_reloads_source_account(config, ledger):

    set_accounts(config, ron(fake_iban(), pays=True))
    with pay.pay_lock():
        assert companies.get("NORD").ron_iban == fake_iban()


@pytest.mark.usefixtures("bank_config")
def test_imported_archive_allows_account_change(config, ledger, home):
    item = payment()
    ledger.add_payment(item)
    ledger.mark_exported([item.item_id], str(home / "NORD/plati/payments-NORD-2026-01-01.csv"))
    assert not pay.bank_change_blocked(ledger, "NORD")
    assert actions.act("company-bank", "", {"key": "NORD", "iban": fake_iban()})["ok"]


@pytest.mark.usefixtures("bank_config")
def test_company_bank_concurrent_duplicate_refused(config, ledger):
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda key: actions.act("company-bank", "", {"key": key, "iban": fake_iban()}), ["NORD", "STUDIO9"]
            )
        )
    assert sorted(r["ok"] for r in results) == [False, True]


@pytest.mark.usefixtures("bank_config")
def test_company_bank_refuses_other_open_file_after_one_import(config, ledger, home):
    go_live(home)
    assert actions.act("company-bank", "", {"key": "NORD", "iban": fake_iban()})["ok"]
    snapshots = []
    for ref in ("EX1", "EX2"):
        item = payment(ref=ref)
        assert ledger.add_payment(item)
        snapshots.append(download_batch(ledger, [item.item_id], today=TODAY))
    one, first = snapshots[0]
    assert pay.import_file(ledger, one, "NORD", first["download_id"], TODAY)
    assert not actions.act("company-bank", "", {"key": "NORD", "iban": fake_iban("0003")})["ok"]
    two, second = snapshots[1]
    assert pay.import_file(ledger, two, "NORD", second["download_id"], TODAY)
    assert cc.bank(key="NORD", iban=fake_iban("0003"), replace=True, today=TODAY)


@pytest.mark.parametrize("currency,replace", [("EUR", False), ("USD", True)])
def test_same_company_other_currency_refused(config, ledger, currency, replace):
    set_accounts(config, dict(iban=fake_iban(), currency=currency), *([ron(NORD_RON, pays=True)] if replace else []))
    before = entries()
    with pytest.raises(cc.CompanyError, match="alt cont"):
        cc.bank(key="NORD", iban=fake_iban(), replace=replace, today=TODAY)
    assert entries() == before


def test_existing_bank_http_refusal_is_private(config, ledger, server, monkeypatch):
    messages = []
    monkeypatch.setattr(actions, "log", messages.append)
    before = entries()
    # a suffix no configured account has: the state names the firm's own accounts by their last four digits
    tried = fake_iban("7311")
    code, result = _api(server, "company-bank", {"key": "NORD", "iban": tried})
    assert code == 200 and not result["ok"] and result["message"] == cc.BANK_SET
    assert entries() == before
    text = json.dumps(result) + "".join(messages)
    assert tried not in text and tried[-4:] not in text
    assert not _api(server, "company-bank", {"key": "NORD", "iban": tried, "replace": True})[1]["ok"]


def test_cli_replacement_keeps_old_account(config, ledger, capsys):
    set_accounts(config, ron(OLD, pays=True))
    args = ["company", "bank", "--company", "NORD", "--iban", fake_iban()]
    a = cli.build_parser().parse_args(args)
    assert a.fn(a, ledger) == 2
    a = cli.build_parser().parse_args(args + ["--replace"])
    assert a.fn(a, ledger) == 0
    assert companies.company_by_iban(OLD).key == "NORD"
    retired = [a for a in entries()["companies"][0]["accounts"] if a.get("retired")]
    assert retired == [dict(iban=OLD, currency="RON", bank="bt", retired=date.today().isoformat())]
    assert fake_iban() not in capsys.readouterr().out

    assert OLD in companies.get("NORD").own_ibans


def test_a_payees_account_is_refused(bank_config, ledger):
    """An IBAN a payment was prepared to is a partner's (the invoices' partners: test_related_company_invoice_account)."""
    ledger.db.execute(
        "INSERT INTO payments (item_id,company,kind,month,amount,status,created_at,payee_iban) "
        "VALUES ('test','STUDIO9','op','2026-01','1.00','pending','now',?)",
        (" ".join(fake_iban().lower()[i : i + 4] for i in range(0, 24, 4)),),
    )
    ledger.db.commit()
    with pytest.raises(cc.CompanyError, match="partener"):
        cc.bank(key="NORD", iban=fake_iban())


def test_pending_payment_uses_new_account(bank_config, ledger, home):
    go_live(home)
    item = payment()
    ledger.add_payment(item)
    cc.bank(key="NORD", iban=fake_iban())
    ident, _ = download_batch(ledger, [item.item_id], today=TODAY)
    with pay.named_path(ledger.payment_file(ident)).open(newline="") as f:
        assert next(csv.DictReader(f))["SourceAccountNumber"] == fake_iban()


def test_retired_account_never_becomes_supplier_or_client(config, ledger):
    replace_old_account(config)
    ledger.db.execute("""INSERT INTO issued
        (message_id,company,raw_path,status,created_at,doc_type,number,issue_date,currency,total,client_name,client_cui)
        VALUES ('i','NORD','','open','now','invoice','EX1','2026-01-01','RON','1190.00','BETA SOFTWARE SRL','66666666')""")
    ledger.db.execute(
        """INSERT INTO efactura
        (message_id,company,raw_path,status,created_at,doc_type,currency,supplier_iban,ibans,issues)
        VALUES ('s','NORD','','open','now','invoice','RON',?,?,'[]')""",
        (OLD, json.dumps([OLD])),
    )
    ledger.db.commit()
    credit = _credit("1190.00", date(2026, 1, 10), iban=OLD)
    assert issued.match_statement(ledger, _st(credit)) == []
    assert ledger.issued("i")["status"] == "open"
    with pytest.raises(efactura.ApproveError, match="own accounts"):
        efactura.payment_item(ledger, "s")


def test_ops_retired_account_and_masked_errors(config, ledger):
    replace_old_account(config)
    text = f"""PLATITOR Atelier Nord SRL
Cod de identificare fiscala
11111111
platitor {OLD} ZZZZROBU
beneficiar RO00ZZZZ0000000000000009 ZZZZROBU
"""
    errors = ops.parse_op_text(text).errors
    assert not any("not a known" in e for e in errors)
    unknown = fake_iban("0007")
    errors = ops.parse_op_text(text.replace(OLD, unknown)).errors
    assert any("0007 is not a known" in e for e in errors)
    assert unknown not in str(errors) and "RO00ZZZZ0000000000000009" not in str(errors)


def test_btgo_masks_invalid_target(ledger):
    errors = btgo_errors(TargetAccountNumber="RO00ZZZZ0000000000000009")
    assert any("IBAN 0009 fails" in e for e in errors)
    assert "RO00ZZZZ0000000000000009" not in str(errors)


@pytest.mark.parametrize("source", ["retired", "EUR", "RON", ""])
def test_btgo_source_must_be_a_ron_account_of_the_company(config, ledger, source):
    """A retired RON account stays valid: rows exported from it before the change keep debiting it (pay._item)."""
    accounts = {"RON": fake_iban(), "EUR": fake_iban("0002"), "retired": fake_iban("0003"), "": ""}
    set_accounts(
        config,
        ron(fake_iban(), pays=True),
        dict(iban=fake_iban("0002"), currency="EUR"),
        ron(fake_iban("0003"), retired="2026-01-01"),
    )

    errors = btgo_errors(SourceAccountNumber=accounts[source])
    assert any("source account" in e for e in errors) == (source in ("EUR", ""))


@pytest.mark.parametrize(
    "owner,currency,replace",
    [
        ("STUDIO9", "RON", True),
        ("NORD", "RON", False),
        ("NORD", "EUR", True),
    ],
)
def test_retired_bank_refusals(bank_config, ledger, owner, currency, replace):
    set_accounts(bank_config, dict(iban=fake_iban(), currency=currency, retired="2026-01-01"), key=owner)
    before = entries()
    with pytest.raises(cc.CompanyError):
        cc.bank(key="NORD", iban=fake_iban(), replace=replace, today=date(2026, 1, 2))
    assert entries() == before


@pytest.mark.parametrize(
    "table,column,cui_column",
    [
        ("efactura", "supplier_iban", "supplier_cui"),
        ("issued", "collected_from_iban", "client_cui"),
    ],
)
@pytest.mark.parametrize("own_partner", [True, False])
def test_related_company_invoice_account(bank_config, ledger, table, column, cui_column, own_partner):
    cfg = entries()
    cui = next(c["cui"] for c in cfg["companies"] if c["key"] == "STUDIO9")
    ledger.db.execute(
        f"""INSERT INTO {table}
        (message_id,company,raw_path,status,created_at,{column},{cui_column})
        VALUES ('related','NORD','','open','now',?,?)""",
        (fake_iban(), cui if own_partner else cfg["companies"][0]["cui"]),
    )
    ledger.db.commit()
    if own_partner:
        assert cc.bank(key="STUDIO9", iban=fake_iban())["has_ron_account"]
    else:
        with pytest.raises(cc.CompanyError, match="partener"):
            cc.bank(key="STUDIO9", iban=fake_iban())


@pytest.mark.parametrize(
    "iban,retired,error",
    [
        (" \t" + fake_iban().lower() + "\n", "2026-01-01", None),
        ("RO00" + fake_iban()[4:], "2026-01-01", "invalid retired IBAN"),
        (fake_iban(), "bad-date", "invalid retired account date"),
    ],
)
def test_retired_account_load_validation(config, iban, retired, error):
    cfg = entries()
    cfg["companies"][0]["accounts"].append(ron(iban, retired=retired))
    if error:
        with pytest.raises(ConfigError, match=error):
            companies.companies_from(cfg)
    else:
        assert companies.companies_from(cfg)["NORD"].ibans_retired == ((fake_iban(), "RON", "2026-01-01"),)


def test_statement_stored_issue_masks_account(ledger, home, monkeypatch):
    out = home / "decrypted.pdf"
    out.write_bytes(b"synthetic")
    st = statements.Statement(
        "NORD",
        TODAY,
        date(2026, 1, 31),
        [statements.Account(fake_iban(), "RON", total_debit=Decimal("1.00"), total_credit=Decimal("0.00"))],
    )
    monkeypatch.setattr(statements, "decrypt_to", lambda *a, **kw: ("NORD", "2026-01", out))
    monkeypatch.setattr(statements, "parse", lambda *a: st)
    (home / "encrypted.pdf").write_bytes(b"%PDF-1.7 synthetic encrypted source")
    result = recon.ingest_statement(ledger, home / "encrypted.pdf", home)
    assert not result["balanced"]
    issues = json.loads(ledger.documents(kind="statement")[0]["issues"])
    assert issues == ["parser totals mismatch on 0001"]


def test_supplier_issue_masks_invalid_account(ledger):
    invalid = "RO00ZZZZ0000000000000009"
    inv = efactura.parse_ubl(ubl(iban=invalid))
    assert inv.issues == ["payee account 0009 is not a valid IBAN"]
    efactura.store(ledger, "NORD", msg("9001"), anaf_zip(ubl(iban=invalid), "9001"))
    assert json.loads(ledger.efactura("9001")["issues"]) == inv.issues


def test_replacing_the_paying_account_keeps_every_other_account(config, ledger):
    ing = "RO00INGB0000000000000009"
    set_accounts(
        config,
        ron(fake_iban(), pays=True),
        ron(ing),
        dict(iban=fake_iban("0002"), currency="EUR"),
        ron(fake_iban("0003"), retired="2026-01-01"),
    )

    cc.bank(key="NORD", iban=fake_iban("0003"), replace=True, today=date(2026, 2, 1))  # the retired one comes back
    nord = companies.get("NORD")
    assert nord.ron_iban == fake_iban("0003")
    assert {(a.iban, a.pays, a.retired) for a in nord.accounts} == {
        (fake_iban("0003"), True, ""),
        (ing, False, ""),
        (fake_iban("0002"), False, ""),
        (fake_iban(), False, "2026-02-01"),
    }
    assert len(entries()["companies"][0]["accounts"]) == 4


@pytest.mark.parametrize("known", [False, True])
def test_invoice_prefill_uses_stored_facts_without_confirming(config, ledger, monkeypatch, known):
    profile = dict(
        name="Firma Exemplu SRL",
        cui="12345674",
        reg_com="J00/123/2026",
        street="Strada Exemplu 1",
        city="Localitate Exemplu",
        county="RO-CJ",
        country="RO",
        postal_code="000000",
        vat_payer=True,
        vat_on_collection=False,
        vat_id="RO12345674",
    )
    if known:
        monkeypatch.setattr(
            cc, "lookup", lambda *_: dict(legal_name=profile["name"], inactive=False, invoice_profile=profile)
        )
        key = cc.change(cui="12345674", today=TODAY, L=ledger)["key"]
    else:
        key = "EXEMPLU"
        ledger.save_company(companies.Company(key, profile["name"], profile["cui"], ()))
    cc.change(key=key, name="Alt nume scurt", today=TODAY, L=ledger)
    prefill = workspace.workspace(ledger, key)
    assert prefill["seller"] is None
    fields = prefill["seller_prefill"]
    assert (fields["legal_name"], fields["cui"]) == (profile["name"], profile["cui"])
    assert fields["vat_payer"] is (True if known else None)
    # Before the firm has an account, a VAT status the registry knows already chooses the lines' VAT.
    assert ("S21" in [c["key"] for c in prefill["vat"]["choices"]]) is known
    assert {"RO", "DE", "1A"} <= set(prefill["countries"])
    assert fields["address"]["street"] == (profile["street"] if known else "")
    assert fields["reg_com"] == (profile["reg_com"] if known else "")
    assert all(fields[k] == "" for k in ("iban", "bank", "email"))
    assert not ledger.db.execute("SELECT 1 FROM invoice_sellers WHERE company=?", (key,)).fetchone()
