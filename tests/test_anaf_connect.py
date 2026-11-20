"""Connecting ANAF from fiscalm: the admin's start, the single-use start link and its cookie, ANAF's return, and every
refusal. ANAF is a fake token endpoint; no network. Synthetic data only."""

import json
import threading
import time
import urllib.parse

import anaf_fake
import pytest
from firms import set_firms
from test_api import _api, _get_json, _req, server as server  # noqa: F401  (server is a fixture)

from fiscalm import integrations as ig, secrets
from fiscalm.api import actions
from fiscalm.efactura import client as anaf
from fiscalm.ledger import Ledger

PUBLIC = "https://fiscalm.example.invalid"
REAL_LIST = anaf.list_received
CALLBACK = PUBLIC + "/anaf/callback"


@pytest.fixture
def sec(sec, monkeypatch):
    monkeypatch.setenv("FISCALM_PUBLIC_URL", PUBLIC)
    return sec


@pytest.fixture
def admins(server):
    return server


@pytest.fixture(autouse=True)
def rights(monkeypatch):
    """The SPV check after the exchange: by default the certificate reads the install's firms. Records each call."""
    calls = []
    monkeypatch.setattr(anaf, "list_received", lambda cif, start, end, token: calls.append((cif, token)) or [])
    return calls


@pytest.fixture
def endpoint(monkeypatch):
    fake = anaf_fake.TokenEndpoint()
    monkeypatch.setattr(anaf, "_post", fake)
    return fake


def _log() -> str:
    from fiscalm.ledger import finance_home

    logs = finance_home() / "logs"
    return "\n".join(p.read_text() for p in logs.glob("*.log")) if logs.exists() else ""


def _start(port, iid, user="admin"):
    code, r = _api(port, "integration-connect", {"id": iid}, user=user)
    assert code == 200, r
    return r


def _open(port, link):
    """Open the start link as the browser does; returns (ANAF's page, the cookie value)."""
    path = urllib.parse.urlsplit(link).path
    code, headers, _ = _req(port, "GET", path)
    assert code == 302, code
    cookie = headers["set-cookie"]
    name, _, rest = cookie.partition("=")
    value = rest.split(";", 1)[0]
    assert name == anaf.COOKIE
    return headers["location"], value, cookie


def _back(port, query, cookie):
    return _req(
        port, "GET", f"/anaf/callback?{query}", headers={"Cookie": f"other=1; {anaf.COOKIE}={cookie}"} if cookie else {}
    )


def test_the_whole_connection_from_the_sheet_to_anafs_return(ledger, sec, admins, endpoint):
    iid = anaf_fake.setup(ledger)
    listed = _get_json(admins, "/web/api/integrations")[1]
    (item,) = [i for i in listed["items"] if i["kind"] == "anaf"]
    assert item["connection"] == {
        "state": "not_connected",
        "expires": None,
        "connected_at": None,
        "refusal": None,
        "redirect_uri": CALLBACK,
        "redirect_error": None,
    }
    r = _start(admins, iid)
    assert r["ok"] and r["message"] == "Urmează autentificarea cu certificatul digital la ANAF."
    link = r["done"]["url"]
    assert link.startswith(PUBLIC + "/anaf/start/") and len(link) > len(PUBLIC) + 40
    page, browser, cookie = _open(admins, link)
    u = urllib.parse.urlsplit(page)
    assert f"{u.scheme}://{u.netloc}{u.path}" == anaf.AUTHORIZE_URL
    assert dict(urllib.parse.parse_qsl(u.query)) == {
        "response_type": "code",
        "client_id": anaf_fake.CLIENT_ID,
        "redirect_uri": CALLBACK,
        "token_content_type": "jwt",
    }  # no state
    assert all(
        part in cookie
        for part in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/anaf/callback", f"Max-Age={anaf.STATE_TTL}")
    )
    refresh = anaf_fake.jwt(anaf.datetime(2027, 11, 3, 9, 0))
    endpoint.answers.append({"access_token": "access-synthetic", "refresh_token": refresh})
    code, headers, raw = _back(admins, "code=one-time-code-123&state=ignored&next=https://elsewhere.invalid", browser)
    body = raw.decode()
    assert code == 303 and "ANAF este conectat" in body and "Conexiunea expiră pe 3 noiembrie 2027." in body
    assert '<a href="/?tab=setari&amp;sec=integrari">Revino în fiscalm</a>' in body
    assert headers["location"] == "/?tab=setari&sec=integrari"
    assert headers["referrer-policy"] == "no-referrer"
    assert endpoint.forms == [
        {
            "grant_type": "authorization_code",
            "code": "one-time-code-123",
            "redirect_uri": CALLBACK,
            "token_content_type": "jwt",
        }
    ]
    assert "Max-Age=0" in headers["set-cookie"] and headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY" and "elsewhere" not in body
    # Saved with the integration's secrets, never shown: not in the page, the listing, the state or the log.
    tok = anaf_fake.tokens(ledger)
    assert tok["access_token"] == "access-synthetic" and tok["refresh_expires_at"].startswith("2027-11-03")
    listed = json.dumps(_get_json(admins, "/web/api/integrations")[1])
    state = json.dumps(_get_json(admins, "/web/api/state?co=NORD")[1])
    for text in (body, listed, state, _log()):
        assert "access-synthetic" not in text and refresh not in text and "one-time-code-123" not in text
        assert browser not in text and link.rsplit("/", 1)[1] not in text
    (item,) = [i for i in json.loads(listed)["items"] if i["kind"] == "anaf"]
    assert item["connection"]["state"] == "connected" and item["connection"]["expires"] == "2027-11-03"
    assert item["secrets"] == {"client_secret": True}  # the tokens are not a field
    # The same return again: the attempt is used.
    code, _, raw = _back(admins, "code=one-time-code-123", browser)
    assert code == 400 and anaf.STATE_BAD in raw.decode() and len(endpoint.forms) == 1


def test_connecting_is_configuration_and_works_in_shadow_mode(ledger, sec, admins):
    from fiscalm.ledger import is_live

    assert not is_live() and "integration-connect" in actions.LOCAL_ACTIONS
    assert "integration-connect" not in actions.API_ACTIONS  # never a bearer key's action
    iid = anaf_fake.setup(ledger)
    code, r = _api(admins, "integration-connect", {"co": "NORD", "id": iid})  # the app's body: the company it shows
    assert code == 200 and r["ok"] and r["done"]["url"].startswith(PUBLIC + "/anaf/start/")


def test_only_an_administrator_starts_it(ledger, sec, admins):
    iid = anaf_fake.setup(ledger)
    code, r = _api(admins, "integration-connect", {"id": iid}, user="member")
    assert code == 403 and r == {"ok": False, "message": "Conectarea ANAF o poate porni doar un administrator."}
    assert not ledger.db.execute("SELECT 1 FROM anaf_auth_states").fetchone()
    assert _api(admins, "integration-connect", {"id": iid}, user="unknown")[0] == 401


def test_an_administrator_removed_meanwhile_cannot_finish(ledger, sec, admins, endpoint):
    iid = anaf_fake.setup(ledger)
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    ledger.db.execute("UPDATE users SET role='member' WHERE id='u_admin'")
    ledger.db.commit()
    code, _, raw = _back(admins, "code=one-time-code-123", browser)
    assert code == 303 and "doar un administrator" in raw.decode() and not endpoint.forms
    assert anaf_fake.tokens(ledger) is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"id": "anaf-00000000"},
        {"id": "x"},
        {"id": "ok", "co": "XX"},
        {"id": "ok", "url": "https://elsewhere.invalid"},
        {"id": "ok", "next": "/web/"},
    ],
)
def test_the_start_takes_an_integration_id_and_nothing_else(ledger, sec, admins, body):
    iid = anaf_fake.setup(ledger)
    body = {k: (iid if v == "ok" else v) for k, v in body.items()}
    code, r = _api(admins, "integration-connect", body)
    assert not r["ok"] and not ledger.db.execute("SELECT 1 FROM anaf_auth_states").fetchone()


@pytest.mark.parametrize(
    "setting,message",
    [
        (None, anaf.NO_PUBLIC_URL),
        ("http://fiscalm.example.invalid", anaf.BAD_PUBLIC_URL),
        ("http://localhost:8790", anaf.BAD_PUBLIC_URL),
        ("https://fiscalm.example.invalid/app", anaf.BAD_PUBLIC_URL),
        ("https://user@fiscalm.example.invalid", anaf.BAD_PUBLIC_URL),
    ],
)
def test_the_redirect_address_is_the_public_https_origin_or_nothing(ledger, sec, admins, monkeypatch, setting, message):
    if setting is None:
        monkeypatch.delenv("FISCALM_PUBLIC_URL")
    else:
        monkeypatch.setenv("FISCALM_PUBLIC_URL", setting)
    iid = anaf_fake.setup(ledger)
    (item,) = [i for i in _get_json(admins, "/web/api/integrations")[1]["items"] if i["kind"] == "anaf"]
    assert item["connection"]["redirect_uri"] is None and item["connection"]["redirect_error"] == message
    assert "FISCALM" not in message  # the owner reads it; the setting's name goes to the log
    code, r = _api(admins, "integration-connect", {"id": iid})
    assert r == {"ok": False, "message": message, "next": None, "done": None, "state": r["state"]}


def test_a_trailing_slash_in_the_public_address_is_one_redirect(sec, monkeypatch):
    monkeypatch.setenv("FISCALM_PUBLIC_URL", PUBLIC + "/")
    assert anaf.redirect_uri() == CALLBACK


@pytest.mark.parametrize("change", ["disabled", "removed", "no-secret"])
def test_a_disabled_removed_or_incomplete_integration_cannot_start(ledger, sec, change):
    iid = anaf_fake.setup(ledger)
    if change == "disabled":
        ig.set_enabled(ledger, iid, False)
    elif change == "removed":
        ig.remove(ledger, iid)
    else:
        for p in sec.glob(f"int-{iid}-client_secret-*"):
            p.unlink()
    with pytest.raises(anaf.ConnectError) as e:
        anaf.start(ledger, iid, "Admin")
    assert e.value.message == (anaf.NO_CLIENT if change == "no-secret" else anaf.NOT_SET_UP)


def test_another_integration_kind_cannot_be_connected(ledger, sec):
    other = ig.add(ledger, "ai", "Local", {"provider": "claude-cli"})["id"]
    with pytest.raises(anaf.ConnectError, match="oprită sau nu există"):
        anaf.start(ledger, other, "Admin")


@pytest.mark.parametrize("cookie", [None, "", "x" * 43, "wrong-" + "a" * 40, "bad cookie;"])
def test_a_return_without_this_browsers_cookie_is_refused(ledger, sec, admins, endpoint, cookie):
    iid = anaf_fake.setup(ledger)
    _open(admins, _start(admins, iid)["done"]["url"])
    code, headers, raw = _back(admins, "code=one-time-code-123", cookie)
    assert code == 400 and anaf.STATE_BAD in raw.decode() and not endpoint.forms
    assert "Max-Age=0" in headers["set-cookie"]


def test_a_return_before_the_start_link_was_opened_is_refused(ledger, sec, admins, endpoint):
    _start(admins, anaf_fake.setup(ledger))
    code, _, raw = _back(admins, "code=one-time-code-123", "a" * 43)
    assert code == 400 and not endpoint.forms


def test_the_start_link_works_once(ledger, sec, admins):
    link = _start(admins, anaf_fake.setup(ledger))["done"]["url"]
    _open(admins, link)
    code, headers, raw = _req(admins, "GET", urllib.parse.urlsplit(link).path)
    assert code == 400 and anaf.STATE_BAD in raw.decode() and "set-cookie" not in headers and "location" not in headers


@pytest.mark.parametrize(
    "path",
    [
        "/anaf/start/",
        "/anaf/start/short",
        "/anaf/start/" + "a" * 43,
        "/anaf/start/../../web/api/state",
        "/anaf/start/" + "%2F" * 20,
    ],
)
def test_an_unknown_start_link_is_refused(ledger, sec, admins, path):
    code, headers, raw = _req(admins, "GET", path)
    assert code == 400 and "location" not in headers and anaf.STATE_BAD in raw.decode()


def test_a_new_start_cancels_the_earlier_one(ledger, sec, admins, endpoint):
    iid = anaf_fake.setup(ledger)
    _, first, _ = _open(admins, _start(admins, iid)["done"]["url"])
    _start(admins, iid)
    code, _, _ = _back(admins, "code=one-time-code-123", first)
    assert code == 400 and not endpoint.forms


@pytest.mark.parametrize("stage", ["link", "return"])
def test_an_attempt_expires(ledger, sec, admins, endpoint, monkeypatch, stage):
    iid = anaf_fake.setup(ledger)
    link = _start(admins, iid)["done"]["url"]
    real = time.time
    if stage == "link":
        monkeypatch.setattr(anaf.time, "time", lambda: real() + anaf.STATE_TTL + 1)
        code, headers, _ = _req(admins, "GET", urllib.parse.urlsplit(link).path)
        assert code == 400 and "location" not in headers
        return
    _, browser, _ = _open(admins, link)
    monkeypatch.setattr(anaf.time, "time", lambda: real() + anaf.STATE_TTL + 1)
    code, _, raw = _back(admins, "code=one-time-code-123", browser)
    assert code == 400 and anaf.STATE_BAD in raw.decode() and not endpoint.forms


@pytest.mark.parametrize("error", [*anaf.OAUTH_ERRORS, "untrusted-secret-value"])
def test_anaf_refusing_or_the_person_cancelling_changes_nothing(ledger, sec, admins, endpoint, error):
    iid = anaf_fake.connect(ledger, access="kept", now=anaf.datetime.now())
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    code, headers, raw = _back(admins, f"error={error}&error_description=%3Cscript%3Ealert(1)%3C/script%3E", browser)
    body = raw.decode()
    reason = error if error in anaf.OAUTH_ERRORS else "unknown"
    message = anaf.OAUTH_ERRORS.get(error, anaf.OAUTH_REFUSED)
    assert code == 303 and headers["location"] == f"/?tab=setari&sec=integrari&int={iid}" and message in body
    assert "<script>alert" not in body and "untrusted-secret-value" not in body + _log()
    assert "anaf-callback: refused oauth_" + reason in _log()
    assert not endpoint.forms and anaf_fake.tokens(ledger)["access_token"] == "kept"
    code, headers, _ = _back(admins, "code=one-time-code-123", browser)  # the attempt is used up by the refusal
    assert code == 400 and "location" not in headers


def test_the_sheet_says_why_the_last_attempt_was_refused_until_the_next_one(ledger, sec, admins, endpoint):
    iid = anaf_fake.setup(ledger)

    def said(now=None):
        return anaf.connection(anaf.instance(ledger), now, ledger)["refusal"]

    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    assert said() is None and _back(admins, "error=access_denied", browser)[0] == 303
    (item,) = [i for i in _get_json(admins, "/web/api/integrations")[1]["items"] if i["kind"] == "anaf"]
    assert item["connection"]["refusal"] == said() == anaf.DENIED
    assert said(anaf.datetime.now() + anaf.timedelta(seconds=anaf.STATE_TTL + 1)) is None  # no longer recent
    _start(admins, iid)
    assert said() is None  # a new attempt replaces it


@pytest.mark.parametrize(
    "answer",
    [
        {"error": "invalid_grant"},
        {"access_token": ""},
        {"access_token": "a"},  # no refresh token on a new authorization
        {"access_token": "a", "refresh_token": 5},
        "http-400",
        "timeout",
    ],
)
def test_a_failed_code_exchange_saves_nothing_and_says_what_to_do(ledger, sec, admins, endpoint, answer):
    import urllib.error

    iid = anaf_fake.setup(ledger)
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    endpoint.answers.append(
        urllib.error.HTTPError(anaf.TOKEN_URL, 400, "Bad Request", {}, None)
        if answer == "http-400"
        else TimeoutError()
        if answer == "timeout"
        else answer
    )
    code, _, raw = _back(admins, "code=one-time-code-123", browser)
    assert code == 303 and anaf.EXCHANGE in raw.decode()
    assert anaf_fake.tokens(ledger) is None and "invalid_grant" not in raw.decode()
    assert "anaf-callback: refused exchange" in _log()


@pytest.mark.parametrize("code_value", ["", "short", "a b c d e f g h", "x" * 5000])
def test_a_malformed_code_is_refused_before_anaf(ledger, sec, admins, endpoint, code_value):
    iid = anaf_fake.setup(ledger)
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    code, _, _ = _back(admins, "code=" + urllib.parse.quote(code_value), browser)
    assert code == 303 and not endpoint.forms


@pytest.mark.parametrize("change", ["client-id", "disabled", "removed", "public-url"])
def test_a_setup_changed_during_the_login_is_refused(ledger, sec, admins, endpoint, monkeypatch, change):
    iid = anaf_fake.setup(ledger)
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    if change == "client-id":
        ig.update(ledger, iid, {"client_id": "another-app"})
    elif change == "disabled":
        ig.set_enabled(ledger, iid, False)
    elif change == "removed":
        ig.remove(ledger, iid)
    else:
        monkeypatch.setenv("FISCALM_PUBLIC_URL", "https://other.example.invalid")
    code, _, raw = _back(admins, "code=one-time-code-123", browser)
    assert code == 303 and anaf.CHANGED in raw.decode() and not endpoint.forms


def test_a_removal_while_anaf_answers_saves_no_tokens(ledger, sec, admins, monkeypatch):
    iid = anaf_fake.setup(ledger)
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])

    def answer(url, body, headers):
        other = Ledger()
        try:
            ig.remove(other, iid)
        finally:
            other.close()
        return {"access_token": "late", "refresh_token": "late"}

    monkeypatch.setattr(anaf, "_post", answer)
    code, _, raw = _back(admins, "code=one-time-code-123", browser)
    assert code == 303 and anaf.CHANGED in raw.decode()
    assert not list(sec.glob(f"int-{iid}-*"))


def test_two_returns_racing_exchange_the_code_once(ledger, sec, monkeypatch):
    iid = anaf_fake.setup(ledger)
    link = anaf.start(ledger, iid, "Admin")
    _, browser = anaf.open_start(ledger, link.rsplit("/", 1)[1])
    calls, results = [], []
    monkeypatch.setattr(
        anaf, "_post", lambda url, body, headers: calls.append(1) or {"access_token": "a", "refresh_token": "r"}
    )
    barrier = threading.Barrier(2)

    def back():
        L = Ledger()
        try:
            barrier.wait(5)
            results.append(anaf.finish(L, {"code": "one-time-code-123"}, browser, lambda name: True))
        except anaf.ConnectError as e:
            results.append(e.code)
        finally:
            L.close()

    threads = [threading.Thread(target=back) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(calls) == 1 and sorted(results, key=str) == [
        "state",
        {"expires": anaf_fake.tokens(ledger)["refresh_expires_at"][:10]},
    ]


def test_reconnecting_replaces_the_tokens_and_leaves_one_file(ledger, sec, admins, endpoint):
    iid = anaf_fake.connect(ledger, access="old", now=anaf.datetime.now())
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    endpoint.answers.append({"access_token": "new", "refresh_token": "new-r"})
    assert _back(admins, "code=one-time-code-123", browser)[0] == 303
    assert anaf_fake.tokens(ledger)["access_token"] == "new"
    assert len(list(sec.glob(f"int-{iid}-oauth-*"))) == 1


def test_only_hashes_of_the_link_and_cookie_are_stored(ledger, sec, admins):
    link = _start(admins, anaf_fake.setup(ledger))["done"]["url"]
    _, browser, _ = _open(admins, link)
    rows = [tuple(r) for r in ledger.db.execute("SELECT * FROM anaf_auth_states")]
    dump = json.dumps(rows)
    assert link.rsplit("/", 1)[1] not in dump and browser not in dump and "u_admin" in dump


@pytest.mark.parametrize("days,state", [(400, "connected"), (25, "expiring"), (-1, "expired")])
def test_the_sheet_says_until_when_and_when_to_reconnect(ledger, sec, days, state):
    now = anaf.datetime(2026, 11, 3, 9, 0)
    anaf_fake.connect(ledger, refresh=anaf_fake.jwt(now + anaf.timedelta(days=days)), now=now)
    c = anaf.connection(anaf.instance(ledger), now)
    assert c["state"] == state and c["connected_at"] == "2026-11-03"
    assert c["expires"] == (now + anaf.timedelta(days=days)).date().isoformat()


def test_the_tokens_never_leave_through_the_cli(ledger, sec, capsys):
    from fiscalm import cli

    anaf_fake.connect(ledger, access="access-synthetic", refresh="refresh-synthetic")
    ledger.close()
    assert cli.main(["efactura", "auth-status"]) == 0
    assert cli.main(["integration"]) == 0
    out = capsys.readouterr().out
    assert "access-synthetic" not in out and "refresh-synthetic" not in out and '"state": "connected"' in out


def test_the_old_authorize_command_is_gone():
    from fiscalm import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["efactura", "authorize"])
    for name in ("serve_authorization", "tailscale_name", "tls_files", "CALLBACK_PORT", "TOKENS"):
        assert not hasattr(anaf, name)
    assert secrets  # the module is still used for the integration's own files


def test_a_certificate_without_rights_for_the_firms_replaces_nothing(
    ledger, sec, admins, endpoint, monkeypatch, rights
):
    """Someone else's code (their own certificate, this install's client) opened in the administrator's browser while
    a connection is pending: ANAF's return carries no state, so fiscalm checks the token reads the install's firms."""
    iid = anaf_fake.connect(ledger, access="kept", now=anaf.datetime.now())
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])

    def denied(cif, start, end, token):
        rights.append((cif, token))
        raise anaf.CompanyUnauthorized("ANAF refused access for this company")

    monkeypatch.setattr(anaf, "list_received", denied)
    endpoint.answers.append({"access_token": "attackers-access", "refresh_token": "attackers-refresh"})
    code, _, raw = _back(admins, "code=attackers-code-1", browser)
    assert code == 303 and anaf.NO_RIGHTS in raw.decode()
    from fiscalm import companies

    assert {c for c, _ in rights} == {co.cui for co in companies.all().values()}
    assert all(t == "attackers-access" for _, t in rights)
    assert anaf_fake.tokens(ledger)["access_token"] == "kept" and len(list(sec.glob(f"int-{iid}-oauth-*"))) == 1


def test_one_readable_firm_is_enough_and_a_failed_check_saves_nothing(ledger, sec, admins, endpoint, monkeypatch):
    from fiscalm import companies

    first, *_ = [co.cui for co in companies.all().values()]
    iid = anaf_fake.setup(ledger)

    def some(cif, start, end, token):
        if cif == first:
            raise anaf.CompanyUnauthorized("no")
        return []

    monkeypatch.setattr(anaf, "list_received", some)
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    endpoint.answers.append({"access_token": "a", "refresh_token": "r"})
    assert _back(admins, "code=one-time-code-123", browser)[0] == 303
    monkeypatch.setattr(anaf, "list_received", lambda *a: (_ for _ in ()).throw(anaf.AnafError("ANAF rate limit")))
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    endpoint.answers.append({"access_token": "b", "refresh_token": "r2"})
    code, _, raw = _back(admins, "code=one-time-code-456", browser)
    assert code == 303 and anaf.VERIFY in raw.decode() and anaf_fake.tokens(ledger)["access_token"] == "a"


def test_an_older_callback_finishing_last_never_overwrites_a_newer_connection(ledger, sec, monkeypatch):
    iid = anaf_fake.setup(ledger)
    _, old_browser = anaf.open_start(ledger, anaf.start(ledger, iid, "Admin").rsplit("/", 1)[1])
    newer_done = []

    def post(url, body, headers):
        code = dict(urllib.parse.parse_qsl(body.decode()))["code"]
        if code == "older-code-123":  # while ANAF is slow to answer the first return, a second connection completes
            L = Ledger()
            try:
                _, b = anaf.open_start(L, anaf.start(L, iid, "Admin").rsplit("/", 1)[1])
                newer_done.append(anaf.finish(L, {"code": "newer-code-123"}, b, lambda n: True))
            finally:
                L.close()
        return {"access_token": code, "refresh_token": code}

    monkeypatch.setattr(anaf, "_post", post)
    with pytest.raises(anaf.ConnectError) as e:
        anaf.finish(ledger, {"code": "older-code-123"}, old_browser, lambda n: True)
    assert e.value.message == anaf.CHANGED and newer_done
    assert anaf_fake.tokens(ledger)["access_token"] == "newer-code-123"


@pytest.mark.parametrize("bad", ["has\nnewline", "has space", "x" * 9000, "tab\there"])
def test_a_malformed_token_is_never_saved(ledger, sec, admins, endpoint, bad):
    iid = anaf_fake.connect(ledger, access="kept", now=anaf.datetime.now())
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    endpoint.answers.append({"access_token": bad, "refresh_token": "r"})
    code, _, raw = _back(admins, "code=one-time-code-123", browser)
    assert code == 303 and anaf.EXCHANGE in raw.decode() and anaf_fake.tokens(ledger)["access_token"] == "kept"
    endpoint.answers.append({"access_token": "fine", "refresh_token": bad})  # a refresh answering the same way
    with pytest.raises(anaf.AnafError, match="malformed token") as e:
        anaf.refresh(ledger)
    assert bad not in str(e.value) and anaf_fake.tokens(ledger)["access_token"] == "kept"


def test_a_header_the_transport_refuses_never_carries_the_token_into_an_error(ledger, sec):
    anaf_fake.setup(ledger)
    with pytest.raises(anaf.AnafError) as e:
        anaf._get("descarcare", {"id": "1"}, "secret-token\nInjected: yes")
    assert "secret-token" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_token_files_a_crash_left_behind_go_with_the_next_save(ledger, sec):
    iid = anaf_fake.connect(ledger, now=anaf.datetime.now())
    (sec / f"int-{iid}-oauth-deadbeef").write_text("{}")  # written, then a crash before the row named it
    anaf_fake.connect(ledger, access="next", now=anaf.datetime.now())
    (only,) = sec.glob(f"int-{iid}-oauth-*")
    assert json.loads(only.read_text())["access_token"] == "next"


def test_a_refresh_elsewhere_between_reading_the_row_and_the_file_is_not_an_error(ledger, sec, monkeypatch):
    anaf_fake.connect(ledger, access="first", now=anaf.datetime.now())
    stale = anaf.instance(ledger)
    anaf_fake.connect(ledger, access="second", now=anaf.datetime.now())  # deletes the file `stale` names
    for read in (
        lambda: anaf.access_token(L=ledger) == "second",
        lambda: anaf.token_state(ledger)["status"] == "connected",
    ):
        first = iter([stale])
        monkeypatch.setattr(
            anaf, "instance", lambda L, company=None, first=first: next(first, None) or ig.default(L, "anaf")
        )
        assert read()


def test_connecting_needs_a_firm(ledger, sec, monkeypatch):

    set_firms({})
    with pytest.raises(anaf.ConnectError) as e:
        anaf.start(ledger, anaf_fake.setup(ledger), "Admin")
    assert e.value.message == anaf.NO_COMPANY


@pytest.mark.parametrize(
    "answer,ok",
    [
        ((200, b'{"eroare":"Nu exista mesaje in intervalul selectat"}', "application/json"), True),
        ((200, b'{"mesaje":[],"numar_total_pagini":1}', "application/json"), True),
        ((200, b'{"eroare":"Nu aveti drept de acces pentru CIF=11111111"}', "application/json"), False),
        ("403", False),
    ],
)
def test_the_spv_check_through_anafs_own_answers(ledger, sec, admins, endpoint, monkeypatch, answer, ok):
    """The check below list_received: only the HTTP transport is fake (ANAF's 200 'no messages', a denial in a 200
    body, an HTTP 403), with the environment and the integration as fiscalm reads them."""
    import io
    import urllib.error

    monkeypatch.setattr(anaf, "list_received", REAL_LIST)
    urls = []

    class Answer(io.BytesIO):
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        if isinstance(req, str) or not req.full_url.startswith(anaf.API["prod"]):
            raise AssertionError("only SPV's list is called here")
        urls.append(req.full_url)
        assert req.get_header("Authorization") == "Bearer verified-access"
        if answer == "403":
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        return Answer(answer[1])

    monkeypatch.setattr(anaf.urllib.request, "urlopen", urlopen)
    iid = anaf_fake.setup(ledger)
    _, browser, _ = _open(admins, _start(admins, iid)["done"]["url"])
    endpoint.answers.append({"access_token": "verified-access", "refresh_token": "r"})
    code, _, raw = _back(admins, "code=one-time-code-123", browser)
    assert code == 303 and urls and all("listaMesajePaginatieFactura" in u and "filtru=P" in u for u in urls)
    assert (anaf_fake.tokens(ledger) is not None) is ok
    if not ok:
        assert anaf.NO_RIGHTS in raw.decode()


def test_multiple_callbacks_bind_each_instances_tokens(ledger, sec, admins, endpoint):
    a = anaf_fake.setup(ledger)
    b = ig.add(ledger, "anaf", "Second", {"client_id": anaf_fake.CLIENT_ID, "client_secret": anaf_fake.CLIENT_SECRET})[
        "id"
    ]
    links = [_start(admins, iid)["done"]["url"] for iid in (a, b)]
    for iid, link in zip((a, b), links, strict=True):
        url, cookie, _ = _open(admins, link)
        endpoint.answers.append({"access_token": iid, "refresh_token": "synthetic-refresh"})
        assert _back(admins, "code=synthetic-code", cookie)[0] == 303
        assert anaf._tokens(ig.instance(ledger, iid))["access_token"] == iid


@pytest.mark.parametrize("module", ["received", "issued"])
def test_sync_selects_one_connection_per_firm_and_continues_after_error(ledger, sec, monkeypatch, module):
    from fiscalm.efactura import inbox
    from fiscalm.invoicing import issued

    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    a = anaf_fake.connect(ledger)
    b = ig.add(ledger, "anaf", "Second", {"client_id": "second", "client_secret": "synthetic"}, firm_scope=["STUDIO9"])[
        "id"
    ]
    ig.update(ledger, a, {}, firm_scope=["NORD"])
    tok = anaf._merged({"access_token": "second-token", "refresh_token": "synthetic"}, None, anaf.datetime.now())
    assert ig.put_oauth(ledger, b, json.dumps(tok), client_id="second", replace=True)
    calls = []

    def listing(cui, start, end, token):
        calls.append(token)
        if token == "tok":
            raise anaf.AnafError("synthetic refusal")
        return []

    monkeypatch.setattr(anaf, "list_received" if module == "received" else "list_sent", listing)
    result = (inbox if module == "received" else issued).sync(ledger)
    assert calls == ["tok", "second-token"]
    assert any("NORD" in e for e in result["errors"])
    assert ledger.get_meta("anaf_read_connection:STUDIO9") == b
    ig.update(ledger, b, {}, firm_scope=[])
    result = (inbox if module == "received" else issued).sync(ledger)
    assert any("STUDIO9" in w for w in result["warnings"])


def test_success_clears_transient_error_despite_uncovered_firm(ledger, sec, monkeypatch):
    from fiscalm.efactura import inbox

    monkeypatch.setenv("FISCALM_ANAF_ENV", "prod")
    iid = anaf_fake.connect(ledger)
    ig.update(ledger, iid, {}, firm_scope=["NORD"])

    def fail(*args):
        raise anaf.AnafError("synthetic transient")

    monkeypatch.setattr(anaf, "list_received", fail)
    inbox.sync(ledger)
    assert ledger.get_meta("efactura_last_error:NORD")
    monkeypatch.setattr(anaf, "list_received", lambda *a: [])
    inbox.sync(ledger)
    assert not ledger.get_meta("efactura_last_error:NORD")
    assert not ledger.get_meta("efactura_last_error")
    assert "STUDIO9" in ledger.get_meta("efactura_warnings:STUDIO9")
    assert not ledger.get_meta("efactura_warnings:NORD")
