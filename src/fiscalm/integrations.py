"""Integrations: every outside connection an install configures (Setări › Integrări), any number of each kind.
A firm's own bank settings (its statement password) use the same store but are set only from Setări › Firme › Bănci
(`set_statement_password`); Integrări neither lists nor changes them.

Each kind declares its fields. Plain fields live in the ledger (`integrations` table); secret fields (API keys,
passwords, OAuth client secrets) live in 0600 files in the secrets directory, one per instance and field, and are
write-only: `listing()` and `public()` say whether a secret is set, never its value, and nothing here logs one.

An install with no integration of a kind keeps the paths it had before integrations existed (the `claude` CLI):
`default()` returns None and the caller falls back.

A kind that connects through OAuth (ANAF) also keeps the tokens the connection got as one more secret of the
instance (`OAUTH`), never shown and never typed: `put_oauth` replaces them, and removing the instance deletes them.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets as pysecrets
import sqlite3
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from fiscalm import PRODUCT, companies, secrets
from fiscalm.api import public_url
from fiscalm.bank import catalogue as banks
from fiscalm.ledger import Ledger, now

NAME_MAX = 60
VALUE_MAX = 500
SECRET_MAX = 4096


class IntegrationError(ValueError):
    """A request the store refuses; the message says which field and why, never a secret's value."""


@dataclass(frozen=True)
class Field:
    key: str
    label: str  # Romanian, shown by the settings screen
    secret: bool = False
    required: bool = False
    choices: tuple[str, ...] = ()
    labels: tuple[tuple[str, str], ...] = ()  # (choice, how it reads); a choice without one reads as itself
    kind: str = "text"  # text | url | email | emails | int | host | company (one of the configured firms)
    default: str = ""
    # Required only in some configurations: any clause whose fields all hold one of its values ("" = empty), e.g.
    # ({"provider": ("anthropic",)}, {"provider": ("openai",), "base_url": ("",)}). The app reads the same clauses.
    required_when: tuple[dict, ...] = ()
    # Asked only in some configurations, in the same clauses (e.g. a shop's eMAG fields once eMAG is chosen); empty =
    # always. The form shows and sends only the fields that apply.
    shown_when: tuple[dict, ...] = ()
    # What fiscalm does on its own with the connection, not how it connects: set from its rule in Automatizări
    # (rules.py), never listed or accepted by Integrări's form.
    rule: bool = False

    def needed(self, cfg: dict) -> bool:
        return self.required or any(
            all((cfg.get(k) or "") in vals for k, vals in c.items()) for c in self.required_when
        )


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    fields: tuple[Field, ...]
    single: bool = False  # at most one instance
    firm: bool = False  # one firm's bank setting, set from Setări › Firme › Bănci, never through Integrări
    extra: dict = field(default_factory=dict)


AI_PROVIDERS = ("anthropic", "openai", "claude-cli", "codex-cli")
PLATFORMS = ("woocommerce", "prestashop", "shopify", "emag", "custom")
ANY_PLATFORM = ({"platform": PLATFORMS},)
EMAG = ({"platform": ("emag",)},)
SHOPIFY = ({"platform": ("shopify",)},)

KINDS: dict[str, Kind] = {
    k.key: k
    for k in (
        Kind(
            "anaf",
            "ANAF e-Factura",
            (
                Field("client_id", "Client ID", required=True),
                Field("client_secret", "Client secret", secret=True, required=True),
            ),
        ),
        Kind(
            "ai",
            "Serviciu AI",
            (
                Field("provider", "Serviciu", required=True, choices=AI_PROVIDERS),
                Field("model", "Model", required_when=({"provider": ("openai",)},)),
                Field("base_url", "Adresă API", kind="url"),
                Field(
                    "api_key",
                    "Cheie API",
                    secret=True,  # a local OpenAI-compatible server may need none
                    required_when=({"provider": ("anthropic",)}, {"provider": ("openai",), "base_url": ("",)}),
                ),
            ),
        ),
        Kind(
            "mail",
            "Cont de e-mail",
            (
                # Asked in this order: the address proposes the server and the user name (Settings.tsx).
                Field("from_address", "Adresa de e-mail", kind="email", required=True),
                Field("method", "Conectare", choices=("imap", "microsoft"), default="imap"),
                Field("client_id", "Microsoft client ID"),
                Field("password", "Parolă", secret=True, required_when=({"method": ("", "imap")},)),
                Field("host", "Server IMAP", required_when=({"method": ("", "imap")},), kind="host"),
                Field("port", "Port", kind="int", default="993"),
                Field("username", "Utilizator", required_when=({"method": ("", "imap")},)),
                Field("own_addresses", "Alte adrese proprii (separate prin virgulă)", kind="emails"),
                # Reading (docs/mail-invoices.md) is the reason the account exists: the integration's Oprește stops it;
                # the folder is read, never changed.
                Field("read_folder", "Dosar citit", default="INBOX"),
            ),
        ),
        Kind(
            "shop",
            "Magazin online",
            (
                Field(
                    "platform",
                    "Platformă",
                    required=True,
                    choices=PLATFORMS,
                ),
                Field("company", "Firmă", required=True, kind="company"),
                Field("series", "Serie", required=True, shown_when=ANY_PLATFORM),
                Field(
                    "auto_issue",
                    "Emitere automată",
                    choices=("false", "true"),
                    labels=(("false", "Oprită"), ("true", "Pornită")),
                    default="false",
                    shown_when=ANY_PLATFORM,
                    rule=True,
                ),
                Field("emag_since", "Preia comenzi începând cu", shown_when=EMAG),
                Field(
                    "marketplace_country",
                    "Țara marketplace-ului",
                    choices=("RO", "BG", "HU"),
                    default="RO",
                    shown_when=EMAG,
                ),
                Field("emag_username", "Utilizator API eMAG", secret=True, required_when=EMAG, shown_when=EMAG),
                Field("emag_password", "Parolă API eMAG", secret=True, required_when=EMAG, shown_when=EMAG),
                Field(
                    "emag_pdf_base",
                    "Adresa publică pentru facturile PDF trimise la eMAG",
                    kind="url",
                    required_when=EMAG,
                    shown_when=EMAG,
                ),
                Field("shop_domain", "Domeniu Shopify", required_when=SHOPIFY, shown_when=SHOPIFY),
                Field("admin_token", "Token Admin API", secret=True, required_when=SHOPIFY, shown_when=SHOPIFY),
                Field("shopify_secret", "Secret Shopify", secret=True, required_when=SHOPIFY, shown_when=SHOPIFY),
                Field("fiscalm_url", f"Adresă {PRODUCT}", kind="url", required_when=SHOPIFY, shown_when=SHOPIFY),
                Field("webhook_url", "Adresă webhook", secret=True, kind="url", shown_when=ANY_PLATFORM),
                Field("webhook_secret", "Secret webhook", secret=True, shown_when=ANY_PLATFORM),
            ),
        ),
        Kind(
            "storage",
            "Arhivă Google Drive",
            (
                Field("company", "Firmă", required=True, kind="company"),
                Field("remote", "Remote rclone", required=True),
                Field("root", "ID dosar Google Drive", required=True),
                Field(
                    "layout",
                    "Organizarea documentelor",
                    required=True,
                    choices=("month", "quarter"),
                    labels=(
                        ("month", "Pe luni și tipuri de documente"),
                        ("quarter", "Pe trimestre, luni și tipuri de documente"),
                    ),
                    default="month",
                ),
            ),
        ),
        Kind(
            "openbanking",
            "Enable Banking",
            (
                Field(
                    "provider",
                    "Furnizor",
                    required=True,
                    choices=("enablebanking",),
                    labels=(("enablebanking", "Enable Banking"),),
                    default="enablebanking",
                ),
                Field("application_id", "ID-ul aplicației", required=True),
                Field("return_url", "Adresa principală de întoarcere", required=True, kind="url"),
                Field("return_urls", "Alte adrese de întoarcere, separate prin spațiu"),
            ),
            single=True,
        ),
        Kind(
            "bank",
            "Bancă",
            (
                Field(
                    "bank",
                    "Banca",
                    required=True,
                    choices=tuple(b.id for b in banks.CATALOGUE),
                    labels=tuple((b.id, b.name) for b in banks.CATALOGUE),
                ),
                Field("company", "Firma", required=True),
                Field("statement_password", "Parola extraselor", secret=True),
            ),
            firm=True,
        ),
    )
}

_ID = re.compile(r"^[a-z]+-[a-z0-9]{8}$")
OAUTH = "oauth"  # the secret holding an OAuth connection's tokens (JSON); not a field


def _new_id(kind: str) -> str:
    return f"{kind}-{pysecrets.token_hex(4)}"


def _db(L: Ledger):
    return L.db  # the table is part of the ledger's schema: reads never write


@contextmanager
def _write(L: Ledger):
    """Keep the independent commit required by versioned secret-file publication.

    Use the ledger transaction boundary so request-scoped authority is checked
    under the same write lock as uniqueness and current-configuration checks.
    """
    if L.db.in_transaction:
        raise RuntimeError("Integration writes require an independent transaction")
    with L.tx() as db:
        yield db


def _secret_name(iid: str, key: str, ver: str) -> str:
    """Secrets are versioned files: a write makes a new file and the committed row names the version in use, so a crash
    between the file and the row leaves the old pair intact (and at most an unreferenced file, swept later)."""
    return f"int-{iid}-{key}-{ver}"


def _versions(cfg: dict) -> dict[str, str]:
    return dict(cfg.get("_v") or {})


def _sweep(iid: str, keep: dict[str, str], *, oauth: bool = False) -> None:
    """Delete this instance's secret files the row doesn't name (replaced, cleared, or left by a crash). The OAuth
    tokens are left alone unless `oauth`: a refresh may have replaced them since `keep` was read, and `put_oauth`
    deletes the file it replaces itself."""
    wanted = {_secret_name(iid, k, v) for k, v in keep.items()}
    for f in secrets.secrets_dir().glob(f"int-{iid}-*"):
        if f.name not in wanted and (oauth or not f.name.startswith(f"int-{iid}-{OAUTH}-")):
            f.unlink(missing_ok=True)


def _check_url(v: str, f: Field) -> str:
    """http(s), a host, no user or password in it, a valid port; plain http only to a server on this machine or the
    local network. The refusal never repeats the address (it may hold credentials)."""
    try:
        u = urllib.parse.urlsplit(v)
        host, _port = u.hostname, u.port  # u.port raises on a bad port
        ok = u.scheme in ("https", "http") and bool(host) and not u.username and not u.password
    except ValueError:
        ok = False
    if not ok:
        raise IntegrationError(f"{f.label}: o adresă http(s) fără utilizator și parolă în ea") from None
    if u.scheme == "http" and not _local(host or ""):
        raise IntegrationError(f"{f.label}: http doar pentru un server local; altfel https")
    return v


def _local(host: str) -> bool:
    """A model server on the same machine or network (Ollama, LM Studio) may speak plain http."""
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or (ip.is_private and not ip.is_link_local)  # 169.254.x: cloud metadata, never


def _clean(kind: Kind, f: Field, value) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise IntegrationError(f"{f.label}: valoare nevalidă")
    v = str(value) if f.secret else str(value).strip()  # a password keeps its spaces
    limit = SECRET_MAX if f.secret else VALUE_MAX
    if len(v) > limit or any(c in v for c in "\r\n\x00"):
        raise IntegrationError(f"{f.label}: prea lung sau cu caractere nepermise")
    if not v:
        return v
    if f.choices and v not in f.choices:
        raise IntegrationError(f"{f.label}: una dintre {', '.join(f.choices)}")
    if f.kind == "url":
        return _check_url(v, f)
    if f.kind == "int" and not (v.isdigit() and 0 < int(v) < 65536):
        raise IntegrationError(f"{f.label}: un număr între 1 și 65535")
    if f.kind == "email" and not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", v):
        raise IntegrationError(f"{f.label}: o adresă de e-mail")
    if f.kind == "emails" and any(
        not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", a.strip())
        for a in v.split(",")
    ):
        raise IntegrationError(f"{f.label}: adrese de e-mail separate prin virgulă")
    if f.kind == "host" and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", v):
        raise IntegrationError(f"{f.label}: un nume de server")
    return v


def _validate(kind: Kind, cfg: dict, secret_set: dict[str, bool]) -> None:
    for f in kind.fields:
        have = secret_set.get(f.key) if f.secret else bool(cfg.get(f.key))
        if f.needed(cfg) and not have:
            raise IntegrationError(
                f"{f.label}: obligatoriu" if f.required else f"{f.label}: obligatoriu pentru această alegere"
            )
    if kind.key == "shop" and cfg.get("platform") == "emag":
        try:
            datetime.strptime(cfg["emag_since"], "%Y-%m-%d %H:%M:%S")
            origin = urllib.parse.urlsplit(cfg.get("emag_pdf_base", ""))
            if origin.scheme != "https" or origin.path not in ("", "/") or origin.query or origin.fragment:
                raise ValueError()
        except (ValueError, KeyError):
            raise IntegrationError(
                "eMAG: data de început și originea HTTPS pentru PDF trebuie completate corect"
            ) from None

    if kind.key == "shop" and cfg.get("platform") == "shopify":
        from fiscalm.commerce.shopify import DOMAIN

        if not DOMAIN.fullmatch(cfg.get("shop_domain", "")):
            raise IntegrationError("Shopify: domeniu myshopify.com nevalid")
        u = urllib.parse.urlsplit(cfg.get("fiscalm_url", ""))
        if u.path not in ("", "/") or u.query or u.fragment:
            raise IntegrationError(f"{PRODUCT}: adresa de bază fără cale sau parametri")
    if kind.key == "shop" and bool(secret_set.get("webhook_url")) != bool(secret_set.get("webhook_secret")):
        raise IntegrationError("Webhook: adresa și secretul trebuie configurate împreună")
    if kind.key == "storage":
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}:", cfg.get("remote", "")):
            raise IntegrationError("Remote rclone: numele conexiunii urmat de două puncte")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", cfg.get("root", "")):
            raise IntegrationError("ID dosar Google Drive: identificator nevalid")
    if kind.key in {"bank", "shop", "storage"}:
        from fiscalm import companies

        if cfg.get("company") not in companies.all():
            raise IntegrationError("Firma: una dintre firmele configurate")


def _row(L: Ledger, iid: str):
    if not isinstance(iid, str) or not _ID.match(iid):
        return None
    return _db(L).execute("SELECT * FROM integrations WHERE id=?", (iid,)).fetchone()


def _secrets_set(iid: str, kind: Kind, cfg: dict) -> dict[str, bool]:
    v = _versions(cfg)
    return {
        f.key: bool(v.get(f.key)) and secrets.has_secret(_secret_name(iid, f.key, v[f.key]))
        for f in kind.fields
        if f.secret
    }


def public(r) -> dict:
    """One instance as the API and the settings screen see it: plain fields, and for secrets only whether set."""
    kind = KINDS[r["kind"]]
    cfg = json.loads(r["config"])
    return {
        "id": r["id"],
        "kind": r["kind"],
        "name": r["name"],
        "enabled": bool(r["enabled"]),
        "default": bool(r["is_default"]),
        "config": {f.key: cfg.get(f.key, f.default) for f in kind.fields if not f.secret},
        "secrets": _secrets_set(r["id"], kind, cfg),
        "updated_at": r["updated_at"],
        **({"firm_scope": cfg["firm_scope"]} if kind.key in ("anaf", "mail") else {}),
    }


def listing(L: Ledger, kind: str | None = None) -> list[dict]:
    rows = (
        _db(L)
        .execute(
            "SELECT * FROM integrations" + (" WHERE kind=?" if kind else "") + " ORDER BY kind, created_at",
            (kind,) if kind else (),
        )
        .fetchall()
    )
    result = [public(r) for r in rows if r["kind"] in KINDS and not KINDS[r["kind"]].firm]
    for item in result:
        if item["kind"] == "anaf":
            from fiscalm.efactura import client as anaf

            item["connection"] = anaf.connection(instance(L, item["id"]), L=L)
        if item["kind"] == "shop" and item["config"]["platform"] == "emag":
            sync = L.db.execute(
                "SELECT reason FROM emag_orders WHERE channel=? AND company=? AND external_id='sync'",
                (item["id"], item["config"]["company"]),
            ).fetchone()
            item["sync_error"] = bool(sync and sync[0])
        if item["kind"] == "mail":
            item["reading"] = _mail_reading(L, item["id"])
    return result


def _mail_reading(L: Ledger, iid: str) -> dict:
    """An inbox's last read (mail.imap.read_all's meta): whether it worked, when one last did and, failed, its kind
    (`code`). Never the error text."""
    run = json.loads(L.get_meta(f"mail_read:{iid}") or "{}")
    return {
        "ok": run.get("ok"),
        "at": run.get("at"),
        "last_ok": L.get_meta(f"mail_read_ok:{iid}"),
    } | ({"code": run["code"]} if run.get("code") else {})


def kinds() -> list[dict]:
    """What can be added, for the settings screen: each kind's fields, which are secret, required, their choices. ANAF
    also says the Callback URL its OAuth app needs (`connect`), known before the integration is added."""
    from fiscalm.efactura import client as anaf

    try:
        origin = public_url.origin(https_only=True) or ""
    except public_url.PublicURLError:
        origin = ""
    # Addresses the install already knows are proposed, never asked: its own public address and the bank return page.
    known = {"fiscalm_url": origin, "return_url": origin and origin + "/bank/return"}
    return [
        {
            "key": k.key,
            "label": k.label,
            "single": k.single,
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "secret": f.secret,
                    "required": f.required,
                    "required_when": [{k2: list(v) for k2, v in c.items()} for c in f.required_when],
                    "shown_when": [{k2: list(v) for k2, v in c.items()} for c in f.shown_when],
                    "known": bool(known.get(f.key)),
                    "choices": list(f.choices),
                    "choice_labels": dict(f.labels),
                    "type": f.kind,
                    "default": known.get(f.key) or f.default,
                }
                for f in k.fields
                if not f.rule
            ],
        }
        | ({"connect": anaf.redirect()} if k.key == "anaf" else {})
        for k in KINDS.values()
        if not k.firm
    ]


def _split(kind: Kind, values: dict) -> tuple[dict, dict]:
    unknown = set(values) - {f.key for f in kind.fields}
    if unknown:
        raise IntegrationError(f"câmp necunoscut: {sorted(unknown)[0][:40]}")
    by = {f.key: f for f in kind.fields}
    plain = {k: _clean(kind, by[k], v) for k, v in values.items() if not by[k].secret}
    secret = {k: _clean(kind, by[k], v) for k, v in values.items() if by[k].secret}
    return plain, secret


def add(L: Ledger, kind: str, name: str, values: dict, *, firm_scope="all") -> dict:
    """A new instance of `kind`. Everything is validated first; the secrets are written while the write lock is held
    and removed again if the row can't be inserted, so a refused request leaves no row and no file. The first instance
    of a kind becomes its default."""
    k = KINDS.get(kind)
    if not k:
        raise IntegrationError("tip de integrare necunoscut")
    name = _name(name or k.label)
    plain, secret = _split(k, values)
    if kind == "shop" and plain.get("platform") == "emag" and not plain.get("emag_since"):
        plain["emag_since"] = now().replace("T", " ")[:19]
    if kind in ("anaf", "mail"):
        plain["firm_scope"] = validate_firm_scope(L, firm_scope)
    elif firm_scope != "all":
        raise IntegrationError("Acest tip nu are un domeniu de firme")
    _validate(k, plain, {f: bool(v) for f, v in secret.items()})
    iid, t = _new_id(kind), now()
    vers = {f: pysecrets.token_hex(4) for f, v in secret.items() if v}
    try:
        with _write(L) as db:
            if (
                kind == "mail"
                and plain.get("method") == "microsoft"
                and db.execute(
                    "SELECT 1 FROM integrations WHERE kind='mail' AND lower(json_extract(config,'$.from_address'))=lower(?) AND json_extract(config,'$.method')='microsoft'",
                    (plain["from_address"],),
                ).fetchone()
            ):
                raise IntegrationError("Contul Microsoft există deja; reconectează-l.")
            if k.single and db.execute("SELECT 1 FROM integrations WHERE kind=?", (kind,)).fetchone():
                raise IntegrationError(f"{k.label}: există deja; modific-o pe cea existentă")
            first = not db.execute("SELECT 1 FROM integrations WHERE kind=? AND is_default=1", (kind,)).fetchone()
            for f, ver in vers.items():
                secrets.write_secret(_secret_name(iid, f, ver), secret[f])
            db.execute(
                "INSERT INTO integrations VALUES(?,?,?,?,1,?,?,?)",
                (iid, kind, name, json.dumps(plain | {"_v": vers}), int(first), t, t),
            )
    except sqlite3.IntegrityError:
        _sweep(iid, {})
        raise IntegrationError(f"{k.label}: există deja; modific-o pe cea existentă") from None
    except BaseException:
        _sweep(iid, {})
        raise
    return public(_row(L, iid))


def update(L: Ledger, iid: str, values: dict, name: str | None = None, *, firm_scope=None) -> dict:
    """Change plain fields and replace secrets. Everything is validated before anything is written; a secret sent empty
    is kept (the screen never had its value). New secrets go to new files and the row switches to them in the same
    commit; the replaced files are deleted after it, so a failure or a crash at any point leaves a consistent pair."""
    if not isinstance(iid, str) or not _ID.match(iid):
        raise IntegrationError("integrarea nu există")
    new_name = _name(name) if name is not None else None
    fresh: dict[str, str] = {}
    dropped = None
    try:
        with _write(L) as db:
            r = db.execute("SELECT * FROM integrations WHERE id=?", (iid,)).fetchone()
            if not r:
                raise IntegrationError("integrarea nu există")
            k = KINDS[r["kind"]]
            plain, secret = _split(k, values)
            before = json.loads(r["config"])
            cfg = before | plain
            if firm_scope is not None:
                if k.key not in ("anaf", "mail"):
                    raise IntegrationError("Acest tip nu are un domeniu de firme")
                cfg["firm_scope"] = validate_firm_scope(L, firm_scope)
            if k.key == "shop" and cfg.get("platform") == "emag" and not cfg.get("emag_since"):
                cfg["emag_since"] = now().replace("T", " ")[:19]
            _validate(k, cfg, _secrets_set(iid, k, cfg) | {f: True for f, v in secret.items() if v})
            fresh = {f: pysecrets.token_hex(4) for f, v in secret.items() if v}
            for f, ver in fresh.items():
                secrets.write_secret(_secret_name(iid, f, ver), secret[f])
            cfg["_v"] = _versions(cfg) | fresh
            # Tokens belong to the OAuth app that issued them: another client id means connecting again.
            if (k.key in ("anaf", "mail") and cfg.get("client_id") != before.get("client_id")) or (
                k.key == "mail" and any(cfg.get(f) != before.get(f) for f in ("method", "from_address"))
            ):
                dropped = cfg["_v"].pop(OAUTH, None)
            db.execute(
                "UPDATE integrations SET config=?, name=?, updated_at=? WHERE id=?",
                (json.dumps(cfg), new_name or r["name"], now(), iid),
            )
    except BaseException:
        for f, ver in fresh.items():  # the row still names the old files: the new ones go
            (secrets.secrets_dir() / _secret_name(iid, f, ver)).unlink(missing_ok=True)
        raise
    _sweep(iid, cfg["_v"])
    if dropped:
        (secrets.secrets_dir() / _secret_name(iid, OAUTH, dropped)).unlink(missing_ok=True)
    return public(_row(L, iid))


def clear(L: Ledger, iid: str, key: str) -> dict:
    """Remove one optional secret (a required one can only be replaced)."""
    with _write(L) as db:
        r = _row(L, iid)
        if not r:
            raise IntegrationError("integrarea nu există")
        k = KINDS[r["kind"]]
        if not any(f.key == key and f.secret for f in k.fields):
            raise IntegrationError("câmp necunoscut")
        cfg = json.loads(r["config"])
        _validate(k, cfg, _secrets_set(iid, k, cfg) | {key: False})
        cfg["_v"] = {f: v for f, v in _versions(cfg).items() if f != key}
        db.execute("UPDATE integrations SET config=?, updated_at=? WHERE id=?", (json.dumps(cfg), now(), iid))
    _sweep(iid, cfg["_v"])
    return public(_row(L, iid))


def set_enabled(L: Ledger, iid: str, on: bool) -> dict:
    with _write(L) as db:
        if not _row(L, iid):
            raise IntegrationError("integrarea nu există")
        db.execute("UPDATE integrations SET enabled=?, updated_at=? WHERE id=?", (int(on), now(), iid))
    return public(_row(L, iid))


def make_default(L: Ledger, iid: str) -> dict:
    """The instance a feature uses when it needs one of this kind (the AI provider the reading uses)."""
    with _write(L) as db:
        r = _row(L, iid)
        if not r:
            raise IntegrationError("integrarea nu există")
        db.execute("UPDATE integrations SET is_default=0 WHERE kind=?", (r["kind"],))
        db.execute("UPDATE integrations SET is_default=1, updated_at=? WHERE id=?", (now(), iid))
    return public(_row(L, iid))


def remove(L: Ledger, iid: str) -> None:
    """Delete an instance, then its secrets. If it was the default, the oldest remaining one becomes the default."""
    with _write(L) as db:
        r = _row(L, iid)
        if not r:
            raise IntegrationError("integrarea nu există")
        db.execute("DELETE FROM integrations WHERE id=?", (iid,))
        if r["is_default"]:
            db.execute(
                "UPDATE integrations SET is_default=1 WHERE id=(SELECT id FROM integrations WHERE kind=? "
                "ORDER BY created_at LIMIT 1)",
                (r["kind"],),
            )
    _sweep(iid, {}, oauth=True)


def put_oauth(
    L: Ledger, iid: str, value: str, *, client_id: str, expect: str | None = None, replace: bool = False, guard=None
) -> bool:
    """Save an OAuth connection's tokens on instance `iid`. Saved only if the instance still exists, is enabled, still
    has `client_id`, and (unless `replace`, a new authorization) still holds the tokens version `expect` the caller read:
    a refresh that lost a race to another one, or that ran while the integration was removed, disabled or pointed at
    another app, saves nothing and returns False; so does a `guard(db)` that answers False under the same lock. The new
    file is written under the write lock and the replaced one is deleted after the commit; a refusal or a crash leaves
    the row and the file it names in agreement, and files a crash left behind are deleted by the next save."""
    ver = pysecrets.token_hex(4)
    name = _secret_name(iid, OAUTH, ver)
    old = None
    try:
        with _write(L) as db:
            r = _row(L, iid)
            if not r or not r["enabled"] or r["kind"] not in ("anaf", "mail"):
                return False
            cfg = json.loads(r["config"])
            old = _versions(cfg).get(OAUTH)
            if cfg.get("client_id") != client_id or (not replace and old != expect) or (guard and not guard(db)):
                return False
            # Under the lock no other save is writing: any token file the row doesn't name was left by a crash.
            for f in secrets.secrets_dir().glob(f"int-{iid}-{OAUTH}-*"):
                if f.name != _secret_name(iid, OAUTH, old or ""):
                    f.unlink(missing_ok=True)
            secrets.write_secret(name, value)
            cfg["_v"] = _versions(cfg) | {OAUTH: ver}
            db.execute("UPDATE integrations SET config=? WHERE id=?", (json.dumps(cfg), iid))
    except BaseException:
        (secrets.secrets_dir() / name).unlink(missing_ok=True)
        raise
    if old:
        (secrets.secrets_dir() / _secret_name(iid, OAUTH, old)).unlink(missing_ok=True)
    return True


def check_listed(L: Ledger, kind: str = "", iid: str = "") -> None:
    """Integrări (app and CLI) changes only the kinds it lists: a firm's bank setting is refused here, by kind or by
    the id of an existing one, as if it didn't exist."""
    r = _row(L, iid) if iid else None
    k = KINDS.get(r["kind"] if r else kind)
    if k and k.firm:
        raise IntegrationError("tip de integrare necunoscut" if kind else "integrarea nu există")


def statement_instance(L: Ledger, company: str, bank: str) -> Instance | None:
    """The bank setting a firm's statements at `bank` (a catalogue id) are opened with: the enabled one holding a
    password, the default first, then the oldest. Another bank's password is never this bank's."""
    found = [
        i
        for i in enabled(L, "bank")
        if i.config.get("company") == company and i.config.get("bank") == bank and "statement_password" in i.versions
    ]
    return next((i for i in found if i.default), found[0] if found else None)


def set_statement_password(L: Ledger, company: str, bank: str, password: str) -> None:
    """Replace (or set) the password of a firm's statements at a catalogue bank that sends encrypted PDF statements.
    Write-only: nothing returns or logs the value. Choosing the setting and writing it happen under one write lock, so
    two saves racing leave one setting, holding the password saved last, and that is the one statements are opened with.
    The new secret is a new file; files the row no longer names are deleted after the commit (see `update`)."""
    b = banks.BY_ID.get(bank)
    if not b or not b.reads_pdf:
        raise IntegrationError("Banca: una dintre băncile cu extrase PDF protejate prin parolă")
    field_ = next(f for f in KINDS["bank"].fields if f.key == "statement_password")
    value = _clean(KINDS["bank"], field_, password)
    if not value:
        raise IntegrationError("Parola extraselor: obligatoriu")
    ver, iid = pysecrets.token_hex(4), ""
    try:
        with _write(L) as db:
            current = statement_instance(L, company, bank)
            iid = current.id if current else _new_id("bank")
            secrets.write_secret(_secret_name(iid, field_.key, ver), value)
            if current:
                cfg = json.loads(_row(L, iid)["config"])
                cfg["_v"] = _versions(cfg) | {field_.key: ver}
                db.execute("UPDATE integrations SET config=?, updated_at=? WHERE id=?", (json.dumps(cfg), now(), iid))
            else:
                first = not db.execute("SELECT 1 FROM integrations WHERE kind='bank' AND is_default=1").fetchone()
                cfg = {"bank": bank, "company": company, "_v": {field_.key: ver}}
                t = now()
                db.execute(
                    "INSERT INTO integrations VALUES(?,?,?,?,1,?,?,?)",
                    (iid, "bank", _name(f"{b.label} {company}"), json.dumps(cfg), int(first), t, t),
                )
    except BaseException:
        if iid:
            (secrets.secrets_dir() / _secret_name(iid, field_.key, ver)).unlink(missing_ok=True)
        raise
    _sweep(iid, cfg["_v"])  # the replaced file, and any a crash left behind


def check_sources(kind: str, plain_keys, secret_keys) -> None:
    """The CLI's --set names only plain fields and --secret only secret ones, so a secret never lands in the ledger
    (or on screen) and a plain value is never hidden."""
    k = KINDS.get(kind)
    if not k:
        raise IntegrationError("tip de integrare necunoscut")
    by = {f.key: f for f in k.fields}
    for key in plain_keys:
        if key in by and by[key].secret:
            raise IntegrationError(f"{by[key].label}: e o valoare secretă; dă-o cu --secret")
    for key in secret_keys:
        if key in by and not by[key].secret:
            raise IntegrationError(f"{by[key].label}: nu e o valoare secretă; dă-o cu --set")


def _name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > NAME_MAX or any(c in name for c in "\r\n\x00"):
        raise IntegrationError(f"Nume: între 1 și {NAME_MAX} caractere")
    return name


@dataclass(frozen=True)
class Instance:
    """What a feature gets from the store: plain config and a way to read one secret, only when it needs it."""

    id: str
    kind: str
    config: dict
    default: bool = False
    versions: dict = field(default_factory=dict, repr=False)
    enabled: bool = True
    firm_scope: str | list[str] = "all"
    name: str = ""

    def covers(self, company: str) -> bool:
        return self.firm_scope == "all" or company in self.firm_scope

    def secret(self, key: str) -> str:
        if key not in self.versions:
            raise secrets.SecretError(f"{key}: not set")
        return secrets.read_secret(_secret_name(self.id, key, self.versions[key]))

    def has(self, key: str) -> bool:
        return key in self.versions and secrets.has_secret(_secret_name(self.id, key, self.versions[key]))


def _instance(r) -> Instance:
    k = KINDS[r["kind"]]
    cfg = json.loads(r["config"])
    return Instance(
        r["id"],
        r["kind"],
        {f.key: cfg.get(f.key) or f.default for f in k.fields if not f.secret},
        bool(r["is_default"]),
        _versions(cfg),
        bool(r["enabled"]),
        cfg.get("firm_scope", "all"),
        r["name"],
    )


def instance(L: Ledger, iid: str) -> Instance | None:
    """One instance by id, enabled or not (see `Instance.enabled`); None if there is none."""
    r = _row(L, iid)
    return _instance(r) if r and r["kind"] in KINDS else None


def default(L: Ledger, kind: str) -> Instance | None:
    """The enabled default instance of a kind, else the oldest enabled one, else None (the caller falls back)."""
    r = (
        _db(L)
        .execute(
            "SELECT * FROM integrations WHERE kind=? AND enabled=1 ORDER BY is_default DESC, created_at LIMIT 1",
            (kind,),
        )
        .fetchone()
    )
    return _instance(r) if r else None


def enabled(L: Ledger, *kinds: str) -> list[Instance]:
    """The enabled instances of one or more kinds, oldest first, in one query."""
    marks = ",".join("?" * len(kinds))
    return [
        _instance(r)
        for r in _db(L).execute(
            f"SELECT * FROM integrations WHERE kind IN ({marks}) AND enabled=1 ORDER BY created_at, rowid", kinds
        )
    ]


def configured(L: Ledger, kind: str) -> bool:
    """A saved instance is setup evidence even if disabled or its secret file is missing."""
    return _db(L).execute("SELECT 1 FROM integrations WHERE kind=? LIMIT 1", (kind,)).fetchone() is not None


def validate_firm_scope(L: Ledger, value: object) -> str | list[str]:
    if value == "all":
        return value
    if not isinstance(value, list) or any(not isinstance(k, str) or k not in companies.all(L) for k in value):
        raise IntegrationError("Firme: toate sau o listă de firme configurate")
    return list(dict.fromkeys(value))


def for_firm(L: Ledger, kind: str, company: str) -> list[Instance]:
    return [i for i in enabled(L, kind) if i.covers(company)]


def anaf_for_firm(L: Ledger, company: str) -> Instance | None:
    found = for_firm(L, "anaf", company)
    previous = L.get_meta(f"anaf_read_connection:{company}")
    return next((i for i in found if i.id == previous), found[0] if found else None)


def coverage(L: Ledger) -> list[dict]:
    result = []
    for firm in companies.all(L):
        selected = anaf_for_firm(L, firm)
        result.append(
            {
                "firm": firm,
                "anaf": selected.id if selected else None,
                "anaf_also": [i.id for i in for_firm(L, "anaf", firm) if not selected or i.id != selected.id],
                "mail": [i.id for i in for_firm(L, "mail", firm)],
            }
        )
    return result
