"""Connecting a bank through open banking: the install's checks, starting, and the bank's return. No network: Enable
Banking is a fake. Synthetic data only."""

import base64
import contextlib
import dataclasses
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from firms import iban as synthetic_iban, set_firm
from test_api import (
    _api,
    _req,  # noqa: F401
    server as server,
)

from fiscalm import (
    companies,
    company_config,
    integrations as ig,
)
from fiscalm.api import actions, state as api
from fiscalm.bank import connections as bc, enablebanking as eb, statements
from fiscalm.ledger import Ledger, now

NEW_ACCOUNT = synthetic_iban("RO00BTRLRONCRT0000000003")
NORD_RON, NORD_EUR = "RO24BTRLRONCRT0000000001", "RO71BTRLEURCRT0000000001"


def _add_application(ledger):
    ig.add(
        ledger,
        "openbanking",
        "Enable Banking",
        {
            "provider": "enablebanking",
            "application_id": "app-1",
            "return_url": "https://fiscalm.example.invalid/bank/return",
        },
    )
    return ledger


@pytest.fixture(scope="session")
def _bank_key(tmp_path_factory):
    """One RSA-4096 key per worker: openssl takes seconds. Tests about making the key still call generate_key."""
    d = tmp_path_factory.mktemp("bank-key") / "secrets"
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("FISCALM_SECRETS", str(d))
        eb.generate_key()
    return d


@pytest.fixture
def setup(ledger, sec, _bank_key):
    sec.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in (eb.KEY, eb.CERTIFICATE):
        shutil.copy2(_bank_key / name, sec / name)
    return _add_application(ledger)


class Fake:
    """Enable Banking as the tests want it: records calls, answers from a table."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], dict(answers or {})

    def __call__(self, method, url, body, headers):
        assert headers["Authorization"].startswith("Bearer ") and url.startswith(eb.API)
        path = url[len(eb.API) :]
        self.calls.append((method, path, body))
        return self.answers.get((method, path.split("?")[0]), (404, {"error": "NOT_FOUND"}))


def client(fake):
    return eb.Client("app-1", http=fake)


APP = {
    "name": "fiscalm",
    "environment": "PRODUCTION",
    "active": True,
    "redirect_urls": ["https://fiscalm.example.invalid/bank/return"],
}


def test_the_key_is_made_once_private_and_signs_requests(ledger, sec):
    eb.generate_key()
    _add_application(ledger)
    key = sec / eb.KEY
    assert oct(key.stat().st_mode & 0o777) == "0o600" and "BEGIN CERTIFICATE" in eb.certificate()
    with pytest.raises(eb.BankError):
        eb.generate_key()  # never replaced: the registered application would stop working
    token = eb.signed_jwt("app-1", now=1000)
    head, body, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"typ": "JWT", "alg": "RS256", "kid": "app-1"}
    assert json.loads(base64.urlsafe_b64decode(pad(body)))["exp"] == 4600
    pub = subprocess.run(
        ["openssl", "x509", "-pubkey", "-noout"], input=eb.certificate().encode(), capture_output=True, check=True
    ).stdout
    (sec.parent / "pub.pem").write_bytes(pub)
    (sec.parent / "sig").write_bytes(base64.urlsafe_b64decode(pad(sig)))
    ok = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(sec.parent / "pub.pem"), "-signature", str(sec.parent / "sig")],
        input=(head + "." + body).encode(),
        capture_output=True,
    )
    assert ok.returncode == 0  # the certificate pasted in Enable Banking verifies what fiscalm signs


def test_checks_walk_the_guide_and_stop_at_the_first_gap(ledger, sec):
    assert [(c["key"], c["ok"]) for c in bc.checks(ledger)] == [("key", False)]
    eb.generate_key()
    assert [(c["key"], c["ok"]) for c in bc.checks(ledger)] == [("key", True), ("application", False)]
    ig.add(
        ledger,
        "openbanking",
        "EB",
        {
            "provider": "enablebanking",
            "application_id": "app-1",
            "return_url": "https://fiscalm.example.invalid/bank/return",
        },
    )
    inactive = Fake({("GET", "/application"): (200, APP | {"active": False})})
    assert [(c["key"], c["ok"]) for c in bc.checks(ledger, client(inactive))][-1] == ("active", False)
    other = Fake({("GET", "/application"): (200, APP | {"redirect_urls": ["https://elsewhere.invalid/"]})})
    assert bc.checks(ledger, client(other))[-1] == {
        "key": "return",
        "ok": False,
        "message": "Lipsește adresa de întoarcere a acestui fiscalm din aplicația Enable Banking.",
    }
    good = Fake({("GET", "/application"): (200, APP)})
    assert (
        all(c["ok"] for c in bc.checks(ledger, client(good))) and bc.checks(ledger, client(good))[-1]["key"] == "return"
    )


def test_a_bank_outside_open_banking_or_an_unset_install_is_refused(ledger, sec):
    with pytest.raises(eb.BankError) as e:
        bc.start(
            ledger,
            "NORD",
            "bt",
            date(2026, 11, 3),
            client(Fake()),
            created_by="u_admin",
        )
    assert e.value.message == bc.NOT_SET_UP
    with pytest.raises(eb.BankError) as e:
        bc.start(
            ledger,
            "NORD",
            "otp",
            date(2026, 11, 3),
            client(Fake()),
            created_by="u_admin",
        )
    assert e.value.message == bc.NOT_SUPPORTED


def connect(setup, accounts, session="sess-0001", until="2027-02-01T10:00:00Z"):
    fake = Fake(
        {
            ("POST", "/auth"): (200, {"url": "https://tilisy.enablebanking.com/ais/start"}),
            ("POST", "/sessions"): (
                200,
                {"session_id": session, "accounts": accounts, "access": {"valid_until": until}},
            ),
        }
    )
    url = bc.start(
        setup,
        "NORD",
        "bt",
        date(2026, 11, 3),
        client(fake),
        created_by="u_admin",
    )
    state = fake.calls[0][2]["state"]
    return fake, url, state


def acct(iban, uid):
    return {"uid": uid, "currency": "EUR" if "EUR" in iban else "RON", "account_id": {"iban": iban}}


def test_connecting_keeps_only_the_companys_accounts_and_the_session_is_a_secret(setup, sec):
    fake, url, state = connect(
        setup, [acct(NORD_RON, "u1"), acct(NORD_EUR, "u2"), acct("RO94BTRLRONCRT0000000002", "u3")]
    )
    assert url == "https://tilisy.enablebanking.com/ais/start"
    body = fake.calls[0][2]
    assert body["aspsp"] == {"name": "Banca Transilvania", "country": "RO"} and body["psu_type"] == "business"
    assert body["redirect_url"] == "https://fiscalm.example.invalid/bank/return"
    r = bc.finish(
        setup,
        {"state": state, "code": "code-12345"},
        client(fake),
        may_connect=lambda name: name == "u_admin",
    )
    assert r == {"company": "NORD", "bank": "bt", "count": 3}
    assert not bc.connections(setup)
    (offer,) = bc.pending(setup, "NORD", "u_admin")
    public = json.dumps(offer)
    assert NORD_RON not in public and "sess-0001" not in public and '"uid"' not in public
    assert not bc.pending(setup, "NORD", "Another")
    r = bc.confirm(
        setup, "NORD", offer["id"], [a["id"] for a in offer["accounts"][:2]], "u_admin", today=date(2026, 11, 4)
    )
    assert r["read"] == ["0001", "0001"]
    (conn,) = bc.connections(setup)
    assert conn["accounts"] == ["0001", "0001"] and conn["valid_until"] == "2027-02-01"
    assert bc.session(setup, conn["id"]) == "sess-0001"
    assert not bc.pending(setup, "NORD", "u_admin")


@pytest.mark.parametrize("change", ["reuse", "old", "unknown", "malformed", "denied", "bad code"])
def test_the_return_is_refused_unless_its_state_is_fresh_and_unused(setup, sec, change):
    fake, _, state = connect(setup, [acct(NORD_RON, "u1")])
    query = {"state": state, "code": "code-12345"}
    if change == "reuse":
        bc.finish(setup, query, client(fake), may_connect=lambda name: name == "u_admin")
    elif change == "old":
        setup.db.execute("UPDATE bank_auth_states SET created_at=?", (time.time() - bc.STATE_TTL - 1,))
        setup.db.commit()
    elif change == "unknown":
        query["state"] = "x" * 43
    elif change == "malformed":
        query["state"] = "../../etc"
    elif change == "denied":
        query = {"state": state, "error": "access_denied"}
    else:
        query["code"] = "<script>"
    with pytest.raises(eb.BankError):
        bc.finish(setup, query, client(fake), may_connect=lambda name: name == "u_admin")
    if change != "reuse":
        assert not bc.connections(setup)
    assert ("POST", "/sessions", {"code": "code-12345"}) not in fake.calls[1 if change != "reuse" else 2 :]


def test_selected_new_accounts_are_added_without_typing_and_unselected_accounts_are_never_read(setup):
    fake, _, state = connect(setup, [acct(NEW_ACCOUNT, "u3"), acct(NORD_RON, "u1")])
    bc.finish(setup, {"state": state, "code": "code-12345"}, client(fake), may_connect=lambda name: True)
    assert not bc.connections(setup) and not companies.get("NORD", L=setup).account(NEW_ACCOUNT)
    (offer,) = bc.pending(setup, "NORD", "u_admin")
    bc.confirm(setup, "NORD", offer["id"], [offer["accounts"][0]["id"]], "u_admin", today=date(2026, 11, 4))
    assert companies.get("NORD", L=setup).account(NEW_ACCOUNT).pays is False
    (row,) = setup.db.execute("SELECT accounts FROM bank_connections").fetchall()
    assert json.loads(row[0]) == [{"iban": NEW_ACCOUNT, "uid": "u3"}]


def _sessions(sec):
    return [p for p in sec.iterdir() if p.name.startswith("enablebanking-session-")]


def _offer(setup, accounts, session="sess-0001"):
    fake, _, state = connect(setup, accounts, session)
    bc.finish(setup, {"state": state, "code": "code-12345"}, client(fake), may_connect=lambda name: True)
    return bc.pending(setup, "NORD", "u_admin")[0]


def _confirm(setup, offer):
    return bc.confirm(
        setup, "NORD", offer["id"], [a["id"] for a in offer["accounts"]], "u_admin", today=date(2026, 11, 4)
    )


def test_stopping_a_bank_keeps_history_other_banks_and_keys_but_cancels_pending_access(setup, sec):
    _confirm(setup, _offer(setup, [acct(NORD_RON, "u1")]))
    _offer(setup, [acct(NORD_RON, "u2")], "sess-0002")
    _, _, state = connect(setup, [acct(NORD_RON, "u3")])
    tx = statements.Tx(date(2026, 11, 3), "OP", "Synthetic proof", "debit", Decimal("1.00"), "RON", NORD_RON)
    setup.record_statement(statements.Statement("NORD", None, None, [statements.Account(NORD_RON, "RON", [tx])]))
    with setup.tx() as db:
        db.execute(
            "INSERT INTO bank_connections(id,company,bank,provider,accounts,valid_until,created_at) "
            "VALUES('bc-1111111111111111','NORD','ing','enablebanking','[]','2027-02-01',?)",
            (now(),),
        )
    before = companies.get("NORD", L=setup)
    assert _sessions(sec) and bc.pending(setup, "NORD", "u_admin")

    def revoked():
        raise PermissionError("Synthetic revocation at commit")

    with pytest.raises(PermissionError):
        bc.disconnect(setup, "NORD", "bt", revoked)
    assert _sessions(sec) and bc.pending(setup, "NORD", "u_admin")
    body = {"co": "NORD", "bank": "bt"}
    assert not actions.act("bank-disconnect", "NORD", body, created_by="u_admin", require=lambda _: False)["ok"]
    assert _sessions(sec)
    for args in (body | {"session": "supplied"}, {"co": "NORD"}):
        assert not actions.act("bank-disconnect", "NORD", args, created_by="u_admin")["ok"]
    for _ in range(2):  # safe to retry, including in shadow mode
        assert actions.act("bank-disconnect", "NORD", body, created_by="u_admin")["ok"]
    assert [c["bank"] for c in bc.connections(setup)] == ["ing"]
    assert companies.get("NORD", L=setup) == before and (sec / eb.KEY).exists()
    assert len(setup.account_lines("NORD", "2026-11-01")) == 1
    assert not _sessions(sec) and not list(sec.glob("enablebanking-offer-*"))
    assert not bc.pending(setup, "NORD", "u_admin")
    with pytest.raises(eb.BankError):
        bc.finish(setup, {"state": state, "code": "code-12345"}, may_connect=lambda _: True)


def test_stopping_a_bank_during_its_callback_cannot_recreate_pending_access(setup, sec, monkeypatch):
    fake, _, state = connect(setup, [acct(NORD_RON, "u1")])
    api = client(fake)
    create = api.create_session

    def stopped(code):
        session = create(code)
        bc.disconnect(setup, "NORD", "bt", lambda: None)
        return session

    monkeypatch.setattr(api, "create_session", stopped)
    with pytest.raises(eb.BankError):
        bc.finish(setup, {"state": state, "code": "code-12345"}, api, may_connect=lambda _: True)
    assert not bc.pending(setup, "NORD", "u_admin") and not list(sec.glob("enablebanking-offer-*"))


def test_reconnecting_switches_only_after_confirmation_and_a_crash_preserves_the_old_connection(
    setup, sec, monkeypatch
):
    _confirm(setup, _offer(setup, [acct(NORD_RON, "u1")]))
    (first,) = bc.connections(setup)
    offer = _offer(setup, [acct(NORD_RON, "u9"), acct(NEW_ACCOUNT, "u3")], "sess-0002")
    assert bc.session(setup, first["id"]) == "sess-0001"
    with monkeypatch.context() as m:
        m.setattr(bc, "now", lambda: (_ for _ in ()).throw(RuntimeError("synthetic crash before commit")))
        with pytest.raises(RuntimeError):
            _confirm(setup, offer)
    assert bc.session(setup, first["id"]) == "sess-0001" and len(_sessions(sec)) == 1
    assert not companies.get("NORD", L=setup).account(NEW_ACCOUNT)
    assert bc.pending(setup, "NORD", "u_admin")
    _confirm(setup, offer)
    (again,) = bc.connections(setup)
    assert again["id"] == first["id"] and bc.session(setup, again["id"]) == "sess-0002"
    assert len(_sessions(sec)) == 1


def test_connect_is_an_install_action_not_on_the_public_api():
    assert "bank-connect" in actions.LOCAL_ACTIONS and "bank-connect" not in actions.API_ACTIONS


def test_the_return_page_says_what_happened_and_nothing_more(setup, sec, server, monkeypatch):
    fake, _, state = connect(setup, [acct(NORD_RON, "u1"), acct("RO94BTRLRONCRT0000000002", "u3")])
    monkeypatch.setattr(bc, "client", lambda L: client(fake))
    code, headers, raw = _req(
        server,
        "GET",
        f"/bank/return?state={state}&code=code-12345&extra=x",
        headers={"Cookie": "fiscalm_bank=synthetic-browser-123456789"},
    )
    page = raw.decode()
    assert code == 200 and "Conturile se pot alege în fiscalm" in page and "15 minute" in page
    assert NORD_RON not in page and "sess-" not in page and "/c/" not in page  # no IBAN, session or app token
    assert headers["x-frame-options"] == "DENY" and headers["cache-control"] == "no-store"
    code, _, raw = _req(server, "GET", f"/bank/return?state={state}&code=code-12345")  # used once
    assert code == 400 and bc.STATE_BAD in raw.decode()


def test_two_setups_at_once_never_replace_a_key(ledger, sec):
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: _try(eb.generate_key), range(2)))
    assert sorted(type(r).__name__ for r in results) == ["BankError", "str"]
    assert eb.certificate() == next(r for r in results if isinstance(r, str))
    assert sorted(p.name for p in sec.iterdir() if not p.name.startswith(".enablebanking.lock")) == [
        eb.CERTIFICATE,
        eb.KEY,
    ]


def _try(fn):
    try:
        return fn()
    except eb.BankError as e:
        return e


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://fiscalm.example.invalid/bank/return", True),
        ("http://localhost:8080/bank/return", True),
        ("http://fiscalm.example.invalid/bank/return", False),
        ("http://192.168.1.5/bank/return", False),
        ("https://fiscalm.example.invalid/other", False),
        ("https://fiscalm.example.invalid/bank/return?x=1", False),
    ],
)
def test_the_return_address_carries_the_code_only_over_https_or_to_this_machine(url, ok):
    assert bc.safe_return(url) is ok


@pytest.mark.parametrize(
    "status,error,code", [(401, "EXPIRED_SESSION", "EXPIRED_SESSION"), (400, "SECRET\nINJECTED", "refused")]
)
def test_refusals_read_as_what_to_do_and_unknown_codes_or_redirects_go_nowhere(setup, status, error, code):
    fake = Fake({("GET", "/application"): (status, {"error": error})})
    with pytest.raises(eb.BankError) as e:
        client(fake).application()
    assert (
        e.value.code == code
        and e.value.message == eb._REFUSALS.get(code, e.value.message)
        and error not in e.value.message
    )
    assert eb._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil.invalid/") is None
    with pytest.raises(eb.BankError):
        eb._http("GET", "https://evil.invalid/x", None, {})


# ------------------------------------------------------------------ what fiscalm shows about a company's banks


def _connected(ledger, until, error=None, last_read="2026-11-02"):
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO bank_connections(id,company,bank,provider,accounts,valid_until,created_at,last_read,"
            "last_error) VALUES('bc-0000000000000001','NORD','bt','enablebanking',?,?,?,?,?)",
            (json.dumps([{"iban": NORD_RON, "uid": "u1"}]), until, now(), last_read, error),
        )


def _with_ing_and_otp(monkeypatch):
    nord = companies.get("NORD")
    extra = (
        companies.Account("RO00INGB0000000000000009", "RON", "ing"),
        companies.Account("RO00OTPV0000000000000009", "RON", "otp"),
        companies.Account("RO00ZZZZ0000000000000009", "RON", ""),
        companies.Account("RO88RNCB0000000000000009", "RON", "bcr", retired="2026-01-01"),
    )
    set_firm("NORD", dataclasses.replace(nord, accounts=nord.accounts + extra))
    return companies.get("NORD")


def test_each_bank_of_the_company_says_whether_it_can_be_connected(ledger, sec, monkeypatch):
    nord = _with_ing_and_otp(monkeypatch)
    out = bc.status(ledger, nord, date(2026, 11, 3))
    # one row per current bank: BT once for two accounts, unknown banks included, no retired bank
    assert [(b["bank"], b["connect"], b["connection"]) for b in out] == [
        ("bt", "not_set_up", None),
        ("ing", "not_set_up", None),
        ("otp", "not_supported", None),
        ("", "not_supported", None),
    ]
    ig.add(
        ledger,
        "openbanking",
        "Enable Banking",
        {
            "provider": "enablebanking",
            "application_id": "app-1",
            "return_url": "https://fiscalm.example.invalid/bank/return",
        },
    )
    assert [b["connect"] for b in bc.status(ledger, nord, date(2026, 11, 3))] == [
        "ready",
        "ready",
        "not_supported",
        "not_supported",
    ]


@pytest.mark.parametrize(
    "until,error,state",
    [
        ("2027-01-31", None, "ok"),
        ("2026-11-17", None, "expiring"),  # 14 days: ask now
        ("2026-11-18", None, "ok"),
        ("2026-11-03", None, "expiring"),  # its last day
        ("", None, "ok"),  # no end given: nothing to warn about, and nothing invented
        ("2026-11-02", None, "expired"),
        ("2026-11-01", "ASPSP_RATE_LIMIT_EXCEEDED", "expired"),  # ended beats a passing error
        ("2027-01-31", "REVOKED_SESSION", "expired"),  # withdrawn in the bank: only a reconnect helps
        ("2027-01-31", "EXPIRED_SESSION", "expired"),
        ("2027-01-31", "CLOSED_SESSION", "expired"),
        ("2027-01-31", "ASPSP_RATE_LIMIT_EXCEEDED", "error"),
        ("2027-01-31", "refused", "error"),
    ],
)
def test_a_connection_says_when_it_needs_a_reconnect(ledger, until, error, state):
    _connected(ledger, until, error)
    (b,) = bc.status(ledger, companies.get("NORD"), date(2026, 11, 3))
    c = b["connection"]
    assert (c["state"], c["valid_until"], c["last_read"], c["accounts"]) == (
        state,
        until or None,
        "2026-11-02",
        [NORD_RON[-4:]],
    )
    assert (c["error"] is None) == (error is None)
    if error:
        assert c["error"] in eb._REFUSALS.values() or c["error"].startswith("Ultima citire")
    assert NORD_RON not in json.dumps(b)  # never the whole IBAN


def test_the_state_lists_connected_banks_and_companies_list_every_bank(ledger):
    assert api.state(ledger, "NORD")["bank_connections"] == []
    _connected(ledger, "2026-11-10")
    conns = api.state(ledger, "NORD")["bank_connections"]
    assert [c["bank"] for c in conns] == ["bt"] and api.state(ledger, "STUDIO9")["bank_connections"] == []
    nord = next(c for c in company_config.listing(ledger)["companies"] if c["key"] == "NORD")
    assert {b["bank"] for b in nord["banks"]} == set(bc.banks.BY_ID)
    assert nord["banks"][0]["connection"]["accounts"] == ["0001"]


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://tilisy.enablebanking.com/ais/start?sessionid=x", True),
        ("https://enablebanking.com/x", True),
        ("http://tilisy.enablebanking.com/x", False),
        ("https://bank.example.invalid@attacker.example.invalid/login", False),
        ("https://tilisy.enablebanking.com@attacker.example.invalid/", False),
        ("https://synthetic-user:dummy-password@tilisy.enablebanking.com/", False),
        ("https://enablebanking.com.attacker.example/", False),
        ("https://evilenablebanking.com/", False),
        ("https://tilisy.enablebanking.com:8443/", False),
        ("https://tilisy.enablebanking.com:99999/", False),
        ("javascript:alert(1)", False),
        (
            "https://attacker.example.invalid\\.enablebanking.com/login",
            False,
        ),  # a browser goes to attacker.example.invalid
        ("https://tilisy.enablebanking.com\t/x", False),
        ("https://tilisy.enablebanking.com/x y", False),
        ("https://TILISY.enablebanking.com/x", True),
    ],
)
def test_only_enable_bankings_own_page_is_ever_given_to_open(setup, url, ok):
    assert eb.trusted_page(url) is ok
    fake = Fake({("POST", "/auth"): (200, {"url": url})})
    if ok:
        assert client(fake).start_auth("BT", "RO", "https://x.invalid/bank/return", "s", "2027-01-01") == url
    else:
        with pytest.raises(eb.BankError):
            client(fake).start_auth("BT", "RO", "https://x.invalid/bank/return", "s", "2027-01-01")


@pytest.mark.parametrize(
    "origin,allowed",
    [
        ("https://phone.example.invalid", True),
        ("https://fiscalm.example.invalid", True),
        ("https://attacker.example.invalid", False),
        ("https://phone.example.invalid/path", False),
    ],
)
def test_connection_chooses_only_a_configured_return_for_the_browser_origin(setup, origin, allowed):
    inst = bc.settings(setup)
    ig.update(setup, inst.id, inst.config | {"return_urls": "https://phone.example.invalid/bank/return"})
    fake = Fake({("POST", "/auth"): (200, {"url": "https://enablebanking.com/start"})})
    if allowed:
        bc.start(
            setup,
            "NORD",
            "bt",
            date(2026, 11, 4),
            client(fake),
            origin=origin,
            created_by="u_admin",
        )
        assert fake.calls[0][2]["redirect_url"] == origin + "/bank/return"
    else:
        with pytest.raises(eb.BankError):
            bc.start(
                setup,
                "NORD",
                "bt",
                date(2026, 11, 4),
                client(fake),
                origin=origin,
                created_by="u_admin",
            )
        assert not fake.calls


def test_browser_origin_reaches_bank_connection_but_body_cannot_choose_a_return(setup, server, monkeypatch):
    origin = server.origin
    inst = bc.settings(setup)
    ig.update(setup, inst.id, inst.config | {"return_urls": origin + "/bank/return"})
    fake = Fake({("POST", "/auth"): (200, {"url": "https://enablebanking.com/start"})})
    monkeypatch.setattr(bc, "client", lambda L: client(fake))
    status, result = _api(server, "bank-connect", {"co": "NORD", "bank": "bt"}, Origin=origin)
    assert status == 200 and result["ok"]
    assert fake.calls[0][2]["redirect_url"] == origin + "/bank/return"
    status, result = _api(server, "bank-connect", {"co": "NORD", "bank": "bt", "return_url": origin}, Origin=origin)
    assert not result["ok"] and len(fake.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://user@fiscalm.invalid/bank/return",
        "https://fiscalm.invalid:99999/bank/return",
        "https://fiscalm.invalid/bank/return?",
        "https://fiscalm.invalid\\evil/bank/return",
    ],
)
def test_return_addresses_refuse_browser_parser_ambiguities(url):
    assert not bc.safe_return(url)


def test_consent_return_requires_a_still_authorized_creator(setup):
    fake, _, state = connect(setup, [acct(NORD_RON, "u1")])
    with pytest.raises(eb.BankError):
        bc.finish(setup, {"state": state, "code": "code-12345"}, client(fake), may_connect=lambda name: False)
    assert not bc.pending(setup, "NORD", "u_admin") and [c[1] for c in fake.calls] == ["/auth"]


def test_phone_return_can_use_another_browser_but_only_the_creator_can_confirm(setup, server, monkeypatch):
    fake, _, state = connect(setup, [acct(NORD_RON, "u1")])
    monkeypatch.setattr(bc, "client", lambda L: client(fake))
    status, headers, body = _req(server, "GET", f"/bank/return?state={state}&code=code-12345")
    assert status == 200 and "set-cookie" not in headers and not bc.connections(setup)
    offer = actions.act("bank-accounts", "NORD", {"co": "NORD", "operation": "list"}, created_by="u_admin")["done"][
        "offers"
    ][0]
    args = {"co": "NORD", "operation": "confirm", "offer": offer["id"], "accounts": [offer["accounts"][0]["id"]]}
    assert not actions.act("bank-accounts", "NORD", args, created_by="Another")["ok"]
    assert not actions.act("bank-accounts", "NORD", args, created_by="u_admin", require=lambda need: False)["ok"]
    assert actions.act("bank-accounts", "NORD", args, created_by="u_admin")["ok"]
    assert not actions.act("bank-accounts", "NORD", args, created_by="u_admin")["ok"]


def test_setup_prepares_a_public_certificate_once_without_provider_access(ledger, sec, monkeypatch):
    monkeypatch.setenv("FISCALM_PUBLIC_URL", "https://fiscalm.example.invalid")
    first = actions.act("bank-setup", "NORD", {"co": "NORD", "operation": "prepare"})
    second = actions.act("bank-setup", "NORD", {"co": "NORD", "operation": "prepare"})
    assert first["ok"] and first["done"] == second["done"]
    assert first["done"]["return_urls"] == ["https://fiscalm.example.invalid/bank/return"]
    assert "BEGIN CERTIFICATE" in first["done"]["certificate"] and "PRIVATE KEY" not in json.dumps(first)


def test_setup_checks_use_the_same_engine_as_cli_and_require_administrator(setup, monkeypatch):
    fake = Fake({("GET", "/application"): (200, APP)})
    monkeypatch.setattr(bc, "client", lambda L: client(fake))
    body = {"co": "NORD", "operation": "check"}
    assert actions.act("bank-setup", "NORD", body, require=lambda need: False)["ok"] is False
    result = actions.act("bank-setup", "NORD", body)
    assert result["ok"] and all(c["ok"] for c in result["done"]["checks"])
    assert fake.calls == [("GET", "/application", None)]


@pytest.mark.parametrize(
    "connected,mail,expected",
    [(True, True, "open_banking"), (False, True, "mail"), (False, False, "upload")],
)
def test_bank_default_is_the_first_functional_source_without_disabling_fallbacks(
    setup, mail_account, connected, mail, expected
):
    ledger = setup
    if connected:
        _connected(ledger, "2027-01-31", None)
    if not mail:
        ig.set_enabled(ledger, mail_account["id"], False)
    (bank,) = bc.status(ledger, companies.get("NORD"), date(2026, 11, 3))
    assert [s["source"] for s in bank["sources"]] == ["open_banking", "mail", "upload"]
    assert [s["source"] for s in bank["sources"] if s["default"]] == [expected]
    assert bank["sources"][-1]["available"] is True


@pytest.mark.parametrize("scope,read", [("all", True), (["NORD"], True), (["STUDIO9"], False)])
def test_statements_by_mail_count_only_the_inboxes_that_cover_the_firm(setup, mail_account, scope, read):
    ig.update(setup, mail_account["id"], {}, firm_scope=scope)
    (bank,) = bc.status(setup, companies.get("NORD"), date(2026, 11, 3), inboxes=True)
    mail = bank["sources"][1]
    assert mail == {
        "source": "mail",
        "available": read,
        "default": read,
        "supported": True,
        "mailboxes": [mail_account["id"]] if read else [],
    }


def test_a_disabled_provider_is_not_a_functional_source_even_with_an_unexpired_consent(setup):
    ledger = setup
    _connected(ledger, "2027-01-31", None)
    ig.set_enabled(ledger, bc.settings(ledger).id, False)
    (bank,) = bc.status(ledger, companies.get("NORD"), date(2026, 11, 3))
    assert [s["source"] for s in bank["sources"] if s["default"]] == ["upload"]


@pytest.mark.parametrize("failure", ["foreign", "retired", "currency", "duplicate", "unknown", "expired", "revoked"])
def test_confirmation_refuses_the_whole_selection_without_partial_accounts_or_connections(setup, failure):
    accounts = [acct(NEW_ACCOUNT, "u3"), acct(NORD_RON, "u1")]
    if failure == "foreign":
        accounts[1] = acct("RO94BTRLRONCRT0000000002", "u2")
    elif failure == "currency":
        accounts[1]["currency"] = "USD"
    elif failure == "retired":
        own = companies.get("NORD", L=setup)
        setup.save_company(
            dataclasses.replace(
                own, accounts=tuple(dataclasses.replace(a, retired="2026-11-01", pays=False) for a in own.accounts)
            ),
            expected=own,
        )
    offer = _offer(setup, accounts)
    selected = [a["id"] for a in offer["accounts"]]
    if failure == "duplicate":
        selected.append(selected[0])
    elif failure == "unknown":
        selected.append("not-offered")
    elif failure == "expired":
        with setup.tx() as db:
            db.execute("UPDATE bank_account_offers SET created_at=0")

    def authorize():
        if failure == "revoked":
            raise PermissionError("synthetic revocation")

    with pytest.raises((eb.BankError, company_config.CompanyError, PermissionError)):
        bc.confirm(setup, "NORD", offer["id"], selected, "u_admin", authorize, today=date(2026, 11, 4))
    assert not companies.get("NORD", L=setup).account(NEW_ACCOUNT) and not bc.connections(setup)
    assert setup.db.execute("SELECT used FROM bank_account_offers").fetchone()[0] == 0


def test_two_connections_racing_confirm_the_offer_only_once(setup):
    offer = _offer(setup, [acct(NEW_ACCOUNT, "u3")])

    def attempt(_):
        with contextlib.closing(Ledger(setup.path)) as other:
            try:
                _confirm(other, offer)
                return True
            except eb.BankError:
                return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]
    assert len(bc.connections(setup)) == 1


@pytest.mark.parametrize("currency", ["USD", None])
def test_missing_currency_uses_provider_details_or_refuses_instead_of_guessing_ron(setup, currency):
    account = acct(NEW_ACCOUNT, "u3")
    account.pop("currency")
    fake, _, state = connect(setup, [account])
    fake.answers[("GET", "/accounts/u3/details")] = (200, {"currency": currency})
    if currency:
        bc.finish(setup, {"state": state, "code": "code-12345"}, client(fake), may_connect=lambda name: True)
        (offer,) = bc.pending(setup, "NORD", "u_admin")
        assert offer["accounts"][0]["currency"] == currency and not bc.connections(setup)
    else:
        with pytest.raises(eb.BankError):
            bc.finish(setup, {"state": state, "code": "code-12345"}, client(fake), may_connect=lambda name: True)
        assert not bc.pending(setup, "NORD", "u_admin")


def test_a_firm_without_accounts_can_choose_every_catalogue_bank_without_changing_its_accounts(setup):
    own = companies.get("NORD", L=setup)
    setup.save_company(dataclasses.replace(own, accounts=()), expected=own)
    nord = next(c for c in company_config.listing(setup)["companies"] if c["key"] == "NORD")
    assert nord["accounts"] == [] and {b["bank"] for b in nord["banks"]} == set(bc.banks.BY_ID)
    assert next(b for b in nord["banks"] if b["bank"] == "bt")["connect"] == "ready"
    assert all(b["sources"][-1]["default"] for b in nord["banks"])
    assert bc.status(setup, companies.get("NORD", L=setup), date(2026, 11, 4)) == []
