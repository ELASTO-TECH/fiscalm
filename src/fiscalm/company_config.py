"""Company configuration shared by fiscalm and the CLI; never edits ledger identities."""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from datetime import date

from fiscalm import companies, integrations
from fiscalm.bank import catalogue as banks
from fiscalm.ledger import Ledger, finance_home
from fiscalm.validate import IBAN_LENGTHS, iban_valid


class CompanyError(ValueError):
    pass


class AnafSilent(CompanyError):
    """ANAF gave no usable answer (network, timeout, 5xx, a malformed reply): the CUI may be right; ask again later."""


BAD = "Datele firmei nu sunt valide."
SILENT = "ANAF nu răspunde acum. Încearcă din nou peste câteva minute."
UNKNOWN = "ANAF nu cunoaște CUI-ul {cui}. Verifică cifrele."


def lookup(cui: str, today: date) -> dict:
    from fiscalm.efactura import public as anaf_public

    return anaf_public.lookup_company(cui, today.isoformat())


def known_facts(L: Ledger, key: str) -> dict:
    """Stored registry facts for editable proposals; never confirms a seller or fetches data."""
    company = companies.get(key, L)
    row = L.db.execute("SELECT lookup_profile FROM firms WHERE key=?", (key,)).fetchone()
    profile = json.loads(row[0]) if row[0] else {}
    account = company.paying_account
    bank = banks.BY_ID.get(account.bank) if account else None
    return profile | dict(
        name=company.legal_name,
        cui=company.cui,
        country="RO",
        vat_payer=profile.get("vat_payer"),
        iban=account.iban if account else "",
        bank=bank.name if bank else "",
    )


def normalize_cui(cui: str) -> str:
    if isinstance(cui, str):
        cui = re.sub(r"[\s\u200b-\u200d\ufeff-]+", "", cui).upper()
    if not isinstance(cui, str) or not re.fullmatch(r"(?:RO)?[0-9]{2,10}", cui):
        raise CompanyError(BAD)
    cui = str(int(cui.removeprefix("RO")))
    if len(cui) < 2:
        raise CompanyError(BAD)
    return cui


@contextmanager
def editing(L: Ledger | None = None):
    if L is None:
        with closing(Ledger()) as ledger, editing(ledger):
            yield ledger
        return
    with L.tx():
        try:
            yield L
        except sqlite3.IntegrityError:
            raise CompanyError(BAD) from None


def _banks_status(L: Ledger, c, inboxes: bool) -> list[dict]:
    """Each bank as Firme › Bănci shows it; a bank with password-protected PDF statements also says whether the firm's
    statement password is set (never the password). The inboxes reading its statement mail are named (by integration
    id) only to whoever may read the integrations (`inboxes`); anyone else only learns whether mail is a source."""
    from fiscalm.bank import connections as bank_connect

    out = bank_connect.status(L, c, date.today(), all_banks=True, inboxes=inboxes)
    for b in out:
        if (bank := banks.BY_ID.get(b["bank"])) and bank.reads_pdf:
            b["statement_password"] = integrations.statement_instance(L, c.key, b["bank"]) is not None
    return out


def listing(L: Ledger, *, inboxes: bool = False) -> dict:
    from fiscalm import history

    configured = companies.all(L)
    tables = [
        r[0]
        for r in L.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if any(c[1] == "company" for c in L.db.execute(f'PRAGMA table_info("{r[0]}")'))
    ]
    return {
        "companies": [
            dict(
                key=c.key,
                name=c.name,
                legal_name=c.legal_name,
                cui=c.cui,
                palette=c.palette,
                aliases=list(c.aliases),
                alias_suggestion=alias_suggestion(c),
                has_ron_account=bool(c.ron_iban),
                iban_last4=c.ron_iban[-4:],
                accounts=accounts_out(L, c),
                banks=_banks_status(L, c, inboxes),
                history=history.summary(L, c.key),
                has_data=any(
                    L.db.execute(f'SELECT 1 FROM "{t}" WHERE company=? LIMIT 1', (c.key,)).fetchone() for t in tables
                ),
            )
            for c in configured.values()
        ],
        "palettes": list(companies.PALETTES),
        "limits": {
            "key_pattern": companies.KEY_RE.pattern,
            "reserved_keys": ["ALL"],
            "name_max": 100,
            "alias_max": companies.ALIAS_MAX,
            "aliases_max": companies.ALIASES_MAX,
        },
    }


def accounts_out(L: Ledger, c) -> list[dict]:
    """A company's accounts as Setări shows them: last four digits only, the bank, which pays, which is retired, and
    the latest month a stored statement covers it (one stored before accounts were recorded is read to learn its
    accounts; one that can't be read proves none)."""
    from fiscalm.bank.intake import learn_accounts

    months: dict[str, str] = {}
    for d in L.documents(company=c.key, kind="statement"):
        if str(d["path"]).endswith(".csv"):  # partial: never the account's latest statement
            continue
        for i in learn_accounts(
            L, d
        ):  # a statement stored before accounts were recorded is read once; unreadable: none
            months[i] = max(months.get(i, ""), d["month"])
    from fiscalm.bank.csv import account_id

    return [
        dict(
            id=account_id(c.key, a.iban),
            iban_last4=a.iban[-4:],
            currency=a.currency,
            bank=a.bank,
            bank_name=banks.BY_ID[a.bank].name if a.bank in banks.BY_ID else "",
            pays=a.pays,
            retired=a.retired or None,
            last_statement=months.get(a.iban) or None,
            statement_senders=list(a.statement_senders),
        )
        for a in c.accounts
    ]


def change(
    *,
    cui: str | None = None,
    key: str | None = None,
    name: str | None = None,
    palette: str | None = None,
    today: date,
    L: Ledger | None = None,
) -> dict:
    """Validate external facts before taking the ledger lock; recheck identities before saving."""
    if (
        name is not None
        and (not isinstance(name, str) or not name.strip() or len(name) > 100)
        or palette is not None
        and palette not in companies.PALETTES
    ):
        raise CompanyError(BAD)
    initial = companies.all(L)
    profile = None
    if cui is not None:
        cui = normalize_cui(cui)
        if cui in {c.cui for c in initial.values()}:
            raise CompanyError("Firma este deja configurată.")
        if not valid_cui(cui):
            raise CompanyError(BAD)
        profile = lookup(cui, today)  # server-side even after the preview
        if profile["inactive"]:
            raise CompanyError("Firma este inactivă la ANAF. Verifică situația ei înainte să o adaugi.")
    with editing(L) as L:
        current = companies.all(L)
        if profile is not None:
            if cui in {str(int(c.cui)) for c in current.values()}:
                raise CompanyError("Firma este deja configurată.")
            if not initial and current:
                raise CompanyError("Prima firmă a fost deja adăugată.")
            name = (name or profile["legal_name"][:100]).strip()
            stem = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
            stem = re.sub("[^A-Z0-9_-]+", "_", stem).strip("_-")[:20] or "FIRMA"
            key, n = stem, 1
            reserved = {k.casefold() for k in reserved_keys() | set(current)}
            while key == "ALL" or key.casefold() in reserved:
                suffix = f"_{n}"
                key, n = stem[: 20 - len(suffix)] + suffix, n + 1
            palette = palette or next(
                (p for p in companies.PALETTES if p not in {c.palette for c in current.values()}),
                companies.PALETTES[len(current) % len(companies.PALETTES)],
            )
            entry = dict(key=key, name=name, legal_name=profile["legal_name"], cui=cui, palette=palette, accounts=[])
            updated = companies.company_from(entry, len(current))
        else:
            if key not in current or name is None and palette is None:
                raise CompanyError(BAD)
            updated = dataclasses.replace(
                current[key], name=name.strip() if name else current[key].name, palette=palette or current[key].palette
            )
        L.save_company(
            updated, expected=current.get(key), lookup_profile=profile.get("invoice_profile") if profile else None
        )
    from fiscalm.api.actions import log

    log(f"company-{'add' if profile is not None else 'update'} {key}")
    c = updated
    return dict(key=c.key, name=c.name, legal_name=c.legal_name, cui=c.cui, palette=c.palette)


ALIAS_SHORT = "Numele este prea scurt: sunt necesare cel puțin trei litere sau cifre, în afară de forma juridică."
ALIAS_SAVED = "Numele este deja salvat la firmă."
ALIAS_OTHER = "Numele aparține altei firme."
ALIAS_FULL = f"O firmă poate avea cel mult {companies.ALIASES_MAX} de alte nume."


_FORM = r"(?:S\.?\s?R\.?\s?L\.?(?:\s?-?\s?D\.?)?|S\.?\s?A\.?|P\.?\s?F\.?\s?A\.?|S\.?\s?N\.?\s?C\.?|S\.?\s?C\.?\s?S\.?)"


def alias_suggestion(c) -> str:
    """The firm's legal name without its legal form ('ATELIER NORD S.R.L.' -> 'ATELIER NORD', 'S.C. Alfa SRL' ->
    'Alfa'), offered as its first other name while it has none; "" when nothing is left to drop or what is left is
    too short to be safe."""
    if c.aliases:
        return ""
    name = re.sub(r"^\s*S\.?\s?C\.?\s+", "", c.legal_name, flags=re.I)
    name = re.sub(r"[\s,]+" + _FORM + r"\s*$", "", name, flags=re.I).strip(" ,")
    return "" if name == c.legal_name.strip() or len(companies.name_core(name)) < 3 else name


def _alias_refusal(alias: str, key: str, current: dict) -> str | None:
    """Why an alias can't be saved, or None. It must keep three letters or digits once its legal forms are gone (a
    bare 'SRL' would name every firm), be new to the firm (punctuation, case and diacritics aside), and never overlap
    another firm's legal name or alias: no text may match both (`companies.names_overlap`), and, more broadly, neither
    core may contain the other ('Nord' and 'Nordic SRL'). Either way a document could be filed under the wrong firm."""
    core = companies.name_core(alias)
    if len(core) < 3:
        return ALIAS_SHORT
    own = current[key]
    words = companies.name_words(alias)
    if any(companies.name_words(n) == words for n in (own.legal_name, *own.aliases)):
        return ALIAS_SAVED
    for other in current.values():
        for n in (other.legal_name, *other.aliases) if other.key != key else ():
            theirs = companies.name_core(n)
            if theirs and (core in theirs or theirs in core) or companies.names_overlap(alias, n):
                return ALIAS_OTHER
    return None


def clean_alias(alias: object) -> str:
    """One alias as it is stored: spaces collapsed; refuses a non-string, a control character or a long one."""
    if not isinstance(alias, str) or re.search(r"[\x00-\x1f\x7f-\x9f]", alias):
        raise CompanyError(BAD)
    alias = " ".join(alias.split())
    if not alias or len(alias) > companies.ALIAS_MAX:
        raise CompanyError(BAD)
    return alias


def set_alias(*, key: str, alias: str, add: bool, L: Ledger | None = None) -> dict:
    """Add or remove one of a firm's other names (`aliases`), in a ledger transaction. fiscalm looks for them as plain
    text (companies.name_regex), never as patterns."""
    alias = clean_alias(alias)
    with editing(L) as L:
        current = companies.all(L)
        if key not in current:
            raise CompanyError(BAD)
        aliases = list(current[key].aliases)
        if add:
            refusal = _alias_refusal(alias, key, current)
            if refusal:
                raise CompanyError(refusal)
            if len(aliases) >= companies.ALIASES_MAX:
                raise CompanyError(ALIAS_FULL)
            aliases.append(alias)
        else:
            if alias not in aliases:
                raise CompanyError("Numele nu mai este salvat la firmă.")
            aliases.remove(alias)
        L.save_company(dataclasses.replace(current[key], aliases=tuple(aliases)), expected=current[key])
    from fiscalm.api.actions import log

    log(f"company-alias-{'add' if add else 'remove'} {key}")  # the name itself stays out of the log
    return dict(key=key, aliases=aliases)


def valid_cui(cui: str) -> bool:
    digits = cui.zfill(10)
    check = sum(int(a) * int(b) for a, b in zip(digits[:-1], "753217532", strict=True)) * 10 % 11 % 10
    return check == int(digits[-1])


def reserved_keys() -> set[str]:
    """Retired identities remain reserved wherever company-scoped data survives."""
    keys: set[str] = set()
    L = Ledger()
    try:
        for row in L.db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            table = row[0].replace('"', '""')
            if any(c[1] == "company" for c in L.db.execute(f'PRAGMA table_info("{table}")')):
                keys.update(r[0] for r in L.db.execute(f'SELECT DISTINCT company FROM "{table}"') if r[0])
        for row in L.db.execute("SELECT config FROM integrations"):
            keys.update(_named_keys(json.loads(row[0])))
        for row in L.db.execute("SELECT key FROM meta"):
            keys.update(row[0].split(":")[1:])
    finally:
        L.close()
    home = finance_home()
    keys.update(p.name for p in home.iterdir() if p.is_dir())
    for parent in [home / "efactura", *home.glob("_catchup-*"), *home.glob("_backfill-*")]:
        if parent.is_dir():
            keys.update(p.name for p in parent.iterdir() if p.is_dir())
    return keys


def _named_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_named_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_named_keys(v) for v in value))
    return {value} if isinstance(value, str) else set()


BANK_SET = "Contul în lei este deja adăugat. Administratorul îl poate schimba din linia de comandă."


def bank(*, key: str, iban: str, replace: bool = False, today: date | None = None, L: Ledger | None = None) -> dict:
    """Set the company's paying RON account; only the CLI may explicitly replace it, and the old one stays the
    company's as a retired account. Every other account of the company is kept as it is."""
    from fiscalm.payments import engine as pay

    iban = companies.normalize_iban(iban)
    if not re.fullmatch(r"RO[0-9]{2}[A-Z]{4}[A-Z0-9]{16}", iban) or not iban_valid(iban):
        raise CompanyError("IBAN-ul nu este valid. E nevoie de IBAN-ul unui cont în lei, la o bancă din România.")
    with pay.pay_lock(), editing(L) as L:
        current = companies.all(L)
        if key not in current:
            raise CompanyError(BAD)
        own = current[key]
        old = own.paying_account
        if old and not replace:
            raise CompanyError(BANK_SET)
        # A retired RON account may come back only through an explicit replacement.
        if any(a.iban == iban for a in own.accounts if not (replace and a.retired and a.currency == "RON")) and not (
            old and old.iban == iban
        ):
            raise CompanyError("Acest IBAN este deja salvat la firmă. E nevoie de alt cont în lei.")
        _not_anothers(L, current, key, iban)
        if old and old.iban != iban and today is None:
            raise ValueError("replacement requires today")
        accounts = [
            dataclasses.replace(a, pays=False, retired=today.isoformat()) if old and a == old else a
            for a in own.accounts
            if a.iban != iban
        ]
        b = banks.for_iban(iban)
        accounts.append(companies.Account(iban, "RON", b.id if b else "", pays=True))
        L.save_company(dataclasses.replace(own, accounts=tuple(accounts)), expected=own)
    from fiscalm.api.actions import log

    log(f"company-bank {key} iban_last4={iban[-4:]}")
    return dict(key=key, has_ron_account=True, iban_last4=iban[-4:])


def _not_anothers(L: Ledger, current: dict, key: str, iban: str, paying: bool = True) -> None:
    """An IBAN a company adds is never another company's, a partner's (a supplier's or client's
    account on record); a pending payment file blocks changing the paying account. Caller holds pay_lock and the
    ledger write lock."""
    from fiscalm.payments import engine as pay

    own = current[key]
    if any(iban in c.own_ibans for co, c in current.items() if co != key):
        raise CompanyError("IBAN-ul este deja folosit de altă firmă.")
    reason = pay.bank_change_reason(L, key) if paying else None
    if reason:
        raise CompanyError(reason)
    for table, column, cui_column in (
        ("payments", "payee_iban", None),
        ("efactura", "supplier_iban", "supplier_cui"),
        ("issued", "collected_from_iban", "client_cui"),
    ):
        partner = cui_column or "''"
        if any(
            iban == companies.normalize_iban(r[0])
            for r in L.db.execute(f"SELECT {column}, {partner} FROM {table} WHERE {column} IS NOT NULL")
            if not cui_column or re.sub(r"\D", "", r[1] or "") != own.cui
        ):
            raise CompanyError("IBAN-ul este folosit de un partener. Este necesar contul firmei.")


ACCOUNT_SHAPE = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}")


def account_additions(L: Ledger, key: str, additions: list[tuple[str, str]]) -> companies.Company:
    """Validate all new accounts before saving any. Caller holds the payment lock and ledger write transaction.

    Manual entry and bank consent use the same identity, country, currency and partner guards. Existing accounts,
    including retired ones, are never replaced or silently revived; these additions never become paying accounts.
    """
    current = companies.all(L)
    if key not in current:
        raise CompanyError(BAD)
    own = current[key]
    for raw, currency in additions:
        iban = companies.normalize_iban(raw)
        currency = currency.strip().upper() if isinstance(currency, str) else ""
        if not ACCOUNT_SHAPE.fullmatch(iban) or not iban_valid(iban):
            raise CompanyError("IBAN-ul nu este valid.")
        if iban[:2] not in IBAN_LENGTHS:
            raise CompanyError("fiscalm primește conturi din UE, Spațiul Economic European, Elveția și Regatul Unit.")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise CompanyError("Moneda contului trebuie scrisă cu trei litere, de exemplu RON sau EUR.")
        if own.account(iban):
            raise CompanyError("Acest IBAN este deja salvat la firmă.")
        _not_anothers(L, current, key, iban, paying=False)
        b = banks.for_iban(iban)
        own = dataclasses.replace(own, accounts=(*own.accounts, companies.Account(iban, currency, b.id if b else "")))
    return own


def add_account(*, key: str, iban: str, currency: str, L: Ledger | None = None) -> dict:
    """Add a non-paying account through the same rules that validate a bank's selected accounts."""
    from fiscalm.payments import engine as pay

    with pay.pay_lock(), editing(L) as L:
        own = companies.all(L).get(key)
        updated = account_additions(L, key, [(iban, currency)])
        L.save_company(updated, expected=own)
    account = updated.accounts[-1]
    from fiscalm.api.actions import log

    log(f"company-account {key} {account.currency} iban_last4={account.iban[-4:]}")
    return dict(key=key, currency=account.currency, bank=account.bank, iban_last4=account.iban[-4:])
