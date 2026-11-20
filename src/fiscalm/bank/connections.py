"""Connecting a company's bank through open banking: start, the bank's return, and the install's setup checks.

Read only: account information, never a payment. The person confirms in their bank (BT: in the BT Go app); the bank
sends the browser back to the install's return address with a `state` fiscalm issued and a one-time `code`. A state is
used once, within STATE_TTL, for the company and bank it was issued for. Only the company's own accounts are kept
from what the bank shares after the starting administrator selects them; unselected accounts are never read. The session is a
secret file; the ledger keeps which accounts it reads and until when. See docs/self-hosting.md.
"""

from __future__ import annotations

import json
import re
import secrets as pysecrets
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from fiscalm import companies, company_config as cc, integrations, secrets
from fiscalm.api import public_url
from fiscalm.bank import catalogue as banks, enablebanking as eb
from fiscalm.ledger import Ledger, now
from fiscalm.payments import engine as pay
from fiscalm.validate import iban_valid

STATE_TTL = 15 * 60
CONSENT_DAYS = 90  # what Romanian banks give; Enable Banking caps it per bank
_STATE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_ID = re.compile(r"^bc-[a-f0-9]{16}$")

NOT_SET_UP = "Open banking nu este configurat. Se configurează în Setări, la Integrări."
NOT_SUPPORTED = "Banca aceasta nu se poate conecta prin aplicația ei. Tranzacțiile vin din extrase."
STATE_BAD = "Legătura cu banca a expirat sau a fost deja folosită. Conectarea se pornește din nou din fiscalm."
DENIED = "Conectarea a fost anulată în bancă. Nu s-a schimbat nimic."


NOT_HTTPS = "Adresa de întoarcere trebuie să fie https (sau localhost, pe calculatorul pe care rulează fiscalm)."


def safe_return(url: str) -> bool:
    """The bank's code and state travel to the return address: https, or plain http only to this very machine."""
    if not isinstance(url, str) or not re.fullmatch(r"[!-~]+", url) or "\\" in url:
        return False
    try:
        u = urllib.parse.urlsplit(url)
        _ = u.port
    except ValueError:
        return False
    return (
        u.path == "/bank/return"
        and not u.query
        and not u.fragment
        and "?" not in url
        and "#" not in url
        and u.username is None
        and u.password is None
        and bool(u.hostname)
        and (u.scheme == "https" or u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))
    )


def return_addresses(inst: integrations.Instance) -> list[str]:
    """Only the administrator's configured addresses may receive a bank code."""
    addresses = [inst.config.get("return_url", ""), *inst.config.get("return_urls", "").split()]
    return list(dict.fromkeys(u for u in addresses if u))


def choose_return(inst: integrations.Instance, origin: str | None) -> str:
    addresses = return_addresses(inst)
    if not addresses or not all(safe_return(u) for u in addresses):
        raise eb.BankError(NOT_HTTPS, "return_url")
    if origin is None:  # CLI/internal callers have no browser; use the configured primary address.
        return addresses[0]
    for address in addresses:
        u = urllib.parse.urlsplit(address)
        if origin == f"{u.scheme}://{u.netloc}":
            return address  # return the stored value, never a URL composed from the request
    raise eb.BankError(
        "Adresa folosită pentru fiscalm nu are o adresă de întoarcere configurată. "
        "Administratorul o poate adăuga în Setări › Integrări › Enable Banking.",
        "return_url",
    )


def settings(L: Ledger) -> integrations.Instance | None:
    return integrations.default(L, "openbanking")


def client(L: Ledger) -> eb.Client:
    inst = settings(L)
    if not inst or not inst.config.get("application_id"):
        raise eb.BankError(NOT_SET_UP, "not_set_up")
    return eb.Client(inst.config["application_id"])


def checks(L: Ledger, api: eb.Client | None = None) -> list[dict]:
    """The setup guide's checks, in order; each {key, ok, message}. Stops at the first that fails: the next ones depend
    on it. Nothing here changes anything."""
    out: list[dict] = []

    def add(key: str, ok: bool, message: str) -> bool:
        out.append({"key": key, "ok": ok, "message": message})
        return ok

    if not add(
        "key",
        secrets.has_secret(eb.KEY),
        "Cheia aplicației este pe server."
        if secrets.has_secret(eb.KEY)
        else "Lipsește cheia aplicației. Se pregătește din Setări › Integrări › Enable Banking.",
    ):
        return out
    inst = settings(L)
    if not add(
        "application",
        bool(inst and inst.config.get("application_id")),
        "ID-ul aplicației este salvat."
        if inst and inst.config.get("application_id")
        else "Lipsește ID-ul aplicației din Enable Banking.",
    ):
        return out
    try:
        app = (api or client(L)).application()
    except eb.BankError as e:
        add("recognised", False, "Enable Banking nu recunoaște aplicația: " + e.message)
        return out
    add("recognised", True, f"Enable Banking recunoaște aplicația „{str(app.get('name') or '')[:60]}”.")
    if not add(
        "production",
        app.get("environment") == "PRODUCTION",
        "Aplicația este în producție."
        if app.get("environment") == "PRODUCTION"
        else "Aplicația este în sandbox: în Enable Banking trebuie aleasă varianta Production.",
    ):
        return out
    if not add(
        "active",
        bool(app.get("active")),
        "Aplicația este activă."
        if app.get("active")
        else "Aplicația nu este încă activă: accesul la conturile firmei se dă o dată, în panoul Enable Banking.",
    ):
        return out
    addresses = return_addresses(inst) if inst else []
    if not add(
        "https",
        all(safe_return(u) for u in addresses),
        "Adresa de întoarcere este sigură." if all(safe_return(u) for u in addresses) else NOT_HTTPS,
    ):
        return out
    add(
        "return",
        all(u in (app.get("redirect_urls") or []) for u in addresses),
        "Adresa de întoarcere este trecută în aplicație."
        if all(u in (app.get("redirect_urls") or []) for u in addresses)
        else "Lipsește adresa de întoarcere a acestui fiscalm din aplicația Enable Banking.",
    )
    return out


def prepare(L: Ledger) -> dict:
    """Prepare public setup values only on an administrator's request, without contacting the provider."""
    if not secrets.has_secret(eb.KEY) and not secrets.has_secret(eb.CERTIFICATE):
        certificate = eb.generate_key()
    elif secrets.has_secret(eb.KEY) and secrets.has_secret(eb.CERTIFICATE):
        certificate = eb.certificate()
    else:
        raise eb.BankError("Verifică certificatul și cheia aplicației pe server.")
    inst = settings(L)
    addresses = return_addresses(inst) if inst else []
    if not addresses:
        try:
            base = public_url.origin()
        except public_url.PublicURLError:
            raise eb.BankError(NOT_HTTPS) from None
        addresses = [base + "/bank/return"] if base else []
    return {"certificate": certificate, "return_urls": addresses}


def _expire_offers(L: Ledger) -> None:
    with L.tx() as db:
        cutoff = time.time() - STATE_TTL
        for row in db.execute("SELECT id FROM bank_account_offers WHERE created_at<?", (cutoff,)):
            (secrets.secrets_dir() / offer_secret(row["id"])).unlink(missing_ok=True)
        db.execute("DELETE FROM bank_account_offers WHERE created_at<?", (cutoff,))


def start(
    L: Ledger,
    company: str,
    bank: str,
    today: date,
    api: eb.Client | None = None,
    *,
    created_by: str,
    origin: str | None = None,
) -> str:
    """The bank's page to open for `company` and `bank`. Records a single-use state for the return."""
    if not created_by:
        raise eb.BankError(STATE_BAD, "state")
    if company not in companies.all(L):
        raise eb.BankError(NOT_SUPPORTED)
    b = banks.BY_ID.get(bank)
    name = b.provider_name(eb.PROVIDER) if b else None
    if not name:
        raise eb.BankError(NOT_SUPPORTED, "not_supported")
    inst = settings(L)
    if not inst or not inst.config.get("return_url"):
        raise eb.BankError(NOT_SET_UP, "not_set_up")
    redirect = choose_return(inst, origin)
    api = api or client(L)
    state = pysecrets.token_urlsafe(32)
    with L.tx() as db:
        db.execute(
            "DELETE FROM bank_auth_owners WHERE state IN (SELECT state FROM bank_auth_states WHERE created_at < ?)",
            (time.time() - STATE_TTL,),
        )
        db.execute("DELETE FROM bank_auth_states WHERE created_at < ?", (time.time() - STATE_TTL,))
        db.execute(
            "INSERT INTO bank_auth_states(state,company,bank,created_at) VALUES(?,?,?,?)",
            (state, company, bank, time.time()),
        )
        db.execute(
            "INSERT INTO bank_auth_owners VALUES(?,?)",
            (state, created_by),
        )
    _expire_offers(L)
    until = datetime.combine(today + timedelta(days=CONSENT_DAYS), datetime.min.time(), UTC)
    return api.start_auth(name, b.country, redirect, state, until.isoformat())


def _take_state(L: Ledger, state: str, may_connect: Callable[[str], bool]) -> tuple[str, str, str] | None:
    """The company and bank a state was issued for, once: a second use, an old one or an unknown one gets None."""
    if not isinstance(state, str) or not _STATE.fullmatch(state):
        return None
    with L.tx() as db:
        row = db.execute(
            "SELECT s.company, s.bank, s.created_at, o.created_by FROM bank_auth_states s "
            "JOIN bank_auth_owners o ON o.state=s.state WHERE s.state=? AND s.used=0",
            (state,),
        ).fetchone()
        if not row or time.time() - row["created_at"] > STATE_TTL:
            return None
        if not may_connect(row["created_by"]):
            return None
        if not db.execute("UPDATE bank_auth_states SET used=1 WHERE state=? AND used=0", (state,)).rowcount:
            return None
    return row["company"], row["bank"], row["created_by"]


ACCOUNT_BAD = "Banca nu a comunicat IBAN-ul, moneda și identificatorul fiecărui cont. Conectarea se poate relua."


def _choice(company: companies.Company, bank: str, account: object, api: eb.Client) -> dict:
    if not isinstance(account, dict):
        raise eb.BankError(ACCOUNT_BAD)
    ident = account.get("account_id")
    raw = ident.get("iban") if isinstance(ident, dict) else None
    iban = companies.normalize_iban(raw) if isinstance(raw, str) else ""
    uid = account.get("uid")
    b = banks.for_iban(iban)
    if (
        not iban_valid(iban)
        or not b
        or b.id != bank
        or not isinstance(uid, str)
        or not re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", uid)
    ):
        raise eb.BankError(ACCOUNT_BAD)
    own = company.account(iban)
    currency = account.get("currency") or (own.currency if own else api.account_details(uid).get("currency"))
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise eb.BankError(ACCOUNT_BAD)
    return {"id": pysecrets.token_hex(8), "iban": iban, "currency": currency, "uid": uid}


def _choices(company: companies.Company, bank: str, accounts: object, api: eb.Client) -> list[dict]:
    """Provider data stays untrusted. Missing new-account currency is fetched from details, never guessed as RON."""
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 50:
        raise eb.BankError(ACCOUNT_BAD)
    out, seen, uids = [], set(), set()
    for raw in accounts:
        a = _choice(company, bank, raw, api)
        if a["iban"] in seen or a["uid"] in uids:
            raise eb.BankError(ACCOUNT_BAD)
        seen.add(a["iban"])
        uids.add(a["uid"])
        out.append(a)
    return out


def offer_secret(offer: str) -> str:
    if not isinstance(offer, str) or not _ID.fullmatch(offer):
        raise eb.BankError(STATE_BAD, "state")
    return "enablebanking-offer-" + offer


def finish(L: Ledger, query: dict, api: eb.Client | None = None, *, may_connect: Callable[[str], bool]) -> dict:
    """The public callback stages an offer only. The authenticated creator must choose accounts before any read.

    No browser cookie is required: a phone bank app may return to a browser separate from the installed web app.
    The single-use state still binds company, bank and creator; the callback never activates a connection.
    """
    taken = _take_state(L, query.get("state", ""), may_connect)
    if not taken:
        raise eb.BankError(STATE_BAD, "state")
    company, bank, creator = taken
    if query.get("error"):
        raise eb.BankError(DENIED, "denied")
    code = query.get("code", "")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{8,512}", code):
        raise eb.BankError(STATE_BAD, "code")
    api = api or client(L)
    session = api.create_session(code)
    sid = session.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", sid):
        raise eb.BankError("Banca nu a deschis accesul. Conectarea se poate porni din nou.", "session")
    choices = _choices(companies.get(company, L=L), bank, session.get("accounts"), api)
    until = str((session.get("access") or {}).get("valid_until") or "")[:10]
    try:
        date.fromisoformat(until)
    except ValueError:
        raise eb.BankError("Banca nu a comunicat data expirării accesului.") from None
    offer = "bc-" + pysecrets.token_hex(8)
    name = offer_secret(offer)
    secrets.write_secret(name, sid)
    try:
        with L.tx() as db:
            if not db.execute("SELECT 1 FROM bank_auth_states WHERE state=? AND used=1", (query["state"],)).fetchone():
                raise eb.BankError(STATE_BAD, "state")  # a local stop also cancels callbacks already in flight
            db.execute(
                "INSERT INTO bank_account_offers VALUES(?,?,?,?,?,?,?,0)",
                (offer, company, bank, creator, json.dumps(choices), until, time.time()),
            )
    except BaseException:
        (secrets.secrets_dir() / name).unlink(missing_ok=True)
        raise
    return {"company": company, "bank": bank, "count": len(choices)}


def pending(L: Ledger, company: str, creator: str) -> list[dict]:
    """Only the starting administrator can see the choice, masked; no provider UID or session is projected."""
    own = companies.get(company, L=L)
    return [
        {
            "id": r["id"],
            "bank": r["bank"],
            "valid_until": r["valid_until"],
            "accounts": [
                {
                    "id": a["id"],
                    "last4": a["iban"][-4:],
                    "currency": a["currency"],
                    "existing": bool(own.account(a["iban"])),
                }
                for a in json.loads(r["accounts"])
            ],
        }
        for r in L.db.execute(
            "SELECT * FROM bank_account_offers WHERE company=? AND created_by=? "
            "AND used=0 AND created_at>=? ORDER BY created_at DESC",
            (company, creator, time.time() - STATE_TTL),
        )
    ]


def _selected(L: Ledger, row, selected: list[str]) -> tuple[companies.Company, companies.Company, list[dict]]:
    choices = {a["id"]: a for a in json.loads(row["accounts"])}
    if not selected or len(selected) != len(set(selected)) or any(x not in choices for x in selected):
        raise eb.BankError("Se aleg doar conturile oferite de bancă, fără repetări.")
    own = companies.get(row["company"], L=L)
    read, additions = [], []
    for key in selected:
        a = choices[key]
        current = own.account(a["iban"])
        if current and (current.retired or current.currency != a["currency"] or current.bank != row["bank"]):
            raise eb.BankError("Un cont ales este retras sau datele lui nu corespund firmei.")
        if not current:
            additions.append((a["iban"], a["currency"]))
        read.append({"iban": a["iban"], "uid": a["uid"]})
    return own, cc.account_additions(L, row["company"], additions), read


def confirm(
    L: Ledger,
    company: str,
    offer: str,
    selected: list[str],
    creator: str,
    authorize: Callable[[], None] = lambda: None,
    *,
    today: date,
) -> dict:
    """Atomically register the chosen accounts, consume the offer and activate exactly their provider UIDs.

    A fresh session file is published before the row switches; old files are removed only after the outer commit.
    The payment lock is acquired before the ledger transaction, as for manual account edits.
    """
    offer_name = offer_secret(offer)
    if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected):
        raise eb.BankError(STATE_BAD)
    name, old_name = None, None
    with pay.pay_lock():
        try:
            with L.tx() as db:
                authorize()
                row = db.execute(
                    "SELECT * FROM bank_account_offers WHERE id=? AND company=? AND created_by=? "
                    "AND used=0 AND created_at>=?",
                    (offer, company, creator, time.time() - STATE_TTL),
                ).fetchone()
                if not row or row["valid_until"] < today.isoformat() or not settings(L):
                    raise eb.BankError(STATE_BAD, "state")
                own, updated, read = _selected(L, row, selected)
                old = db.execute(
                    "SELECT id,session_version FROM bank_connections WHERE company=? AND bank=?", (company, row["bank"])
                ).fetchone()
                cid, version = old["id"] if old else "bc-" + pysecrets.token_hex(8), pysecrets.token_hex(16)
                name = session_secret(cid, version)
                sid = secrets.read_secret(offer_name)
                secrets.write_secret(name, sid)
                L.save_company(updated, expected=own)
                db.execute(
                    "INSERT INTO bank_connections(id,company,bank,provider,accounts,valid_until,created_at,session_version) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(company,bank) DO UPDATE SET accounts=excluded.accounts, "
                    "valid_until=excluded.valid_until, created_at=excluded.created_at, session_version=excluded.session_version, "
                    "last_error=NULL",
                    (cid, company, row["bank"], eb.PROVIDER, json.dumps(read), row["valid_until"], now(), version),
                )
                if db.execute("UPDATE bank_account_offers SET used=1 WHERE id=? AND used=0", (offer,)).rowcount != 1:
                    raise eb.BankError(STATE_BAD, "state")
                old_name = session_secret(old["id"], old["session_version"]) if old else None
        except BaseException:
            if name:
                (secrets.secrets_dir() / name).unlink(missing_ok=True)
            raise
    # Both account registration and connection activation have committed before any old secret is removed.
    for obsolete in (old_name, offer_name):
        if obsolete:
            (secrets.secrets_dir() / obsolete).unlink(missing_ok=True)
    return {
        "company": company,
        "bank": row["bank"],
        "read": [a["iban"][-4:] for a in read],
        "valid_until": row["valid_until"],
    }


def session_secret(connection_id: str, version: str) -> str:
    if not _ID.fullmatch(connection_id) or not re.fullmatch(r"[a-f0-9]{0,32}", version):
        raise eb.BankError("Conexiune necunoscută.")
    return f"enablebanking-session-{connection_id}" + (f"-{version}" if version else "")


def disconnect(L: Ledger, company: str, bank: str, authorize: Callable[[], None]) -> None:
    """Stop future reads locally, preserving all imported evidence and other banks. Never contact the provider."""
    obsolete = []
    with pay.pay_lock(), L.tx() as db:
        authorize()
        row = db.execute(
            "SELECT id,session_version FROM bank_connections WHERE company=? AND bank=?", (company, bank)
        ).fetchone()
        if row:
            obsolete.append(session_secret(row["id"], row["session_version"]))
        obsolete.extend(
            offer_secret(r["id"])
            for r in db.execute("SELECT id FROM bank_account_offers WHERE company=? AND bank=?", (company, bank))
        )
        db.execute(
            "DELETE FROM bank_auth_owners WHERE state IN (SELECT state FROM bank_auth_states WHERE company=? AND bank=?)",
            (company, bank),
        )
        for table in ("bank_auth_states", "bank_account_offers", "bank_connections"):
            db.execute(f"DELETE FROM {table} WHERE company=? AND bank=?", (company, bank))
    for name in obsolete:
        (secrets.secrets_dir() / name).unlink(missing_ok=True)


def session(L: Ledger, connection_id: str) -> str:
    """The current session of a connection (the secret file its row names)."""
    row = L.db.execute("SELECT session_version FROM bank_connections WHERE id=?", (connection_id,)).fetchone()
    if not row:
        raise eb.BankError("Conexiune necunoscută.")
    return secrets.read_secret(session_secret(connection_id, row["session_version"]))


def connections(L: Ledger, company: str | None = None) -> list[dict]:
    """Every connection (of a company): bank, accounts' last four digits, until when, last read and error."""
    q, args = "SELECT * FROM bank_connections", ()
    if company:
        q, args = q + " WHERE company=?", (company,)
    return [
        {
            "id": r["id"],
            "company": r["company"],
            "bank": r["bank"],
            "accounts": [a["iban"][-4:] for a in json.loads(r["accounts"])],
            "valid_until": r["valid_until"],
            "last_read": r["last_read"],
            "last_error": r["last_error"],
        }
        for r in L.db.execute(q + " ORDER BY company, bank", args)
    ]


EXPIRING_DAYS = 14  # a reconnect is asked for this long before the bank's consent ends


def status(
    L: Ledger, company: companies.Company, today: date, *, all_banks: bool = False, inboxes: bool = False
) -> list[dict]:
    """Each catalogue bank holding a current account of `company`, as fiscalm shows it: whether it can be connected
    (`connect`: ready, not_set_up: open banking isn't configured on this install, not_supported: the provider can't
    reach the bank) and its connection, if any (`state`: ok, expiring within EXPIRING_DAYS, expired, error). A bank
    outside the catalogue uses statements only. all_banks includes the whole catalogue, current banks first.
    Says nothing secret: no session, no full IBAN; the inboxes reading statement mail only with `inboxes` (for
    whoever may read integrations)."""
    rows = {r["bank"]: r for r in L.db.execute("SELECT * FROM bank_connections WHERE company=?", (company.key,))}
    found = integrations.enabled(L, "openbanking", "mail")  # one query: Toate firmele asks for every firm
    inst = next((i for i in found if i.kind == "openbanking"), None)
    set_up = bool(inst and inst.config.get("application_id") and inst.config.get("return_url"))
    # a statement mail reaches a firm only through an inbox whose scope covers it (mail.invoices._scoped_statement)
    mailboxes = [i.id for i in found if i.kind == "mail" and i.covers(company.key)]
    ids = dict.fromkeys([a.bank for a in company.current_accounts] + (list(banks.BY_ID) if all_banks else []))
    out = []
    for bank in ids:
        b = banks.BY_ID.get(bank)
        connect = "not_supported" if not b or not b.provider_name(eb.PROVIDER) else "ready" if set_up else "not_set_up"
        r = rows.get(bank)
        conn = None
        if r:
            until = r["valid_until"] or ""
            state = (
                "expired"
                if until and until < today.isoformat() or r["last_error"] in _ENDED
                else "error"
                if r["last_error"]
                else "expiring"
                if until and until <= (today + timedelta(days=EXPIRING_DAYS)).isoformat()
                else "ok"
            )
            conn = {
                "state": state,
                "valid_until": until or None,
                "last_read": r["last_read"],
                "accounts": [x["iban"][-4:] for x in json.loads(r["accounts"])],
                "error": eb._REFUSALS.get(r["last_error"], "Ultima citire nu a reușit. Citirea se reia mâine.")
                if r["last_error"]
                else None,
            }
        sources = bank_sources(b, conn, mailboxes, set_up, inboxes)
        out.append(
            {
                "bank": bank,
                "name": b.name if b else "Altă bancă",
                "connect": connect,
                "connection": conn,
                "sources": sources,
            }
        )
    return out


def bank_sources(
    bank: banks.Bank | None, connection: dict | None, mailboxes: list[str], configured: bool, inboxes: bool = False
) -> list[dict]:
    """Every bank keeps all three routes, all working at once; the first functional route is the default, without
    disabling fallbacks. Mail says whether the bank mails statements fiscalm reads (`supported`) and, with `inboxes`,
    the firm's inboxes that read them (integration ids)."""
    mails = bool(bank and bank.statement_senders)
    usable = [
        bool(configured and connection and connection["state"] in ("ok", "expiring")),
        mails and bool(mailboxes),
        True,
    ]
    selected = usable.index(True)
    out = [
        {"source": source, "available": works, "default": n == selected}
        for n, (source, works) in enumerate(zip(("open_banking", "mail", "upload"), usable, strict=True))
    ]
    out[1]["supported"] = mails
    if inboxes:
        out[1]["mailboxes"] = mailboxes if mails else []
    return out


_ENDED = ("EXPIRED_SESSION", "REVOKED_SESSION", "CLOSED_SESSION")  # the consent is over: only a reconnect helps
