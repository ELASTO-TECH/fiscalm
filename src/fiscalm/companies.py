"""Company facts read from the ledger; no import-time configuration or registry cache."""

from __future__ import annotations

import builtins
import functools
import re
import unicodedata
from collections.abc import Iterator
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date

from fiscalm import PRODUCT, backup
from fiscalm.bank import catalogue as _banks
from fiscalm.config_errors import ConfigError
from fiscalm.ledger import Ledger, finance_home

# The app's colour schemes (app/src/app/globals.css, :root[data-pal=...]). A company without one gets the next in turn.
PALETTES = ("amber", "green", "blue", "violet", "rose", "slate")
PRINT_ACCENTS = {
    "amber": "#C2410C",
    "green": "#047857",
    "blue": "#1D4ED8",
    "violet": "#6D28D9",
    "rose": "#BE123C",
    "slate": "#334155",
}
KEY_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")


def normalize_iban(iban: str) -> str:
    return re.sub(r"\s+", "", iban or "").upper()


def fold(text: str) -> str:
    """Upper case without diacritics, so 'Ș', 'Ş' and 'S' read the same: PDFs and statements drop or swap them."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch)).upper()


# Romanian legal forms, written with or without dots ('S.R.L.' = 'SRL').
LEGAL_FORMS = frozenset({"SC", "SRL", "SA", "SRLD", "SNC", "SCS", "PFA"})
LEGAL_SUFFIXES = (LEGAL_FORMS - {"SC"}) | {"II", "IF", "RA"}
_TAX_LABEL = r"(?:RO|C\.?U\.?I\.?|C\.?I\.?F\.?|VAT|TIN|TAX[ \t]+ID)[ \t:.]*"


def _forms(run: list[str]) -> list[str]:
    """A run of initials as legal forms and lone letters, the split that puts the most letters in legal forms
    ('S C S R L' -> 'SC', 'SRL', never 'SCS', 'R', 'L'), so a name of legal forms alone is never read as letters."""
    best: list[tuple[int, list[str]]] = [(0, [])] * (len(run) + 1)
    for n in range(len(run) - 1, -1, -1):
        options = [(best[n + 1][0], [run[n], *best[n + 1][1]])]
        for k in range(2, min(len(run) - n, max(map(len, LEGAL_FORMS))) + 1):
            if "".join(run[n : n + k]) in LEGAL_FORMS:
                options.append((k + best[n + k][0], ["".join(run[n : n + k]), *best[n + k][1]]))
        best[n] = max(options, key=lambda o: o[0])
    return best[0][1]


def name_words(name: str) -> list[str]:
    """A name's words, folded (`fold`), with the dotted legal forms in a run of initials joined (`_forms`: 'S R L' ->
    'SRL', 'A S R L' -> 'A', 'SRL'); any other initial stays a word of its own ('ALPHA L CONSULTING')."""
    out: list[str] = []
    run: list[str] = []
    for w in [*re.findall(r"[^\W_]+", fold(name)), ""]:
        if len(w) == 1:
            run.append(w)
            continue
        out += _forms(run)
        run = []
        if w:
            out.append(w)
    return out


def name_core(name: str) -> str:
    """What identifies a name: its letters and digits without its legal forms, run together ('S.C. Atelier-Nord
    S.R.L.' -> 'ATELIERNORD')."""
    return "".join(w for w in name_words(name) if w not in LEGAL_FORMS)


def _shape(name: str) -> tuple[str, set[int]]:
    """A name as `name_regex` matches it: its letters and digits run together, and the places a space or punctuation
    may sit (between words, and between a legal form's letters)."""
    compact, cuts = "", {0}
    for w in name_words(name):
        start = len(compact)
        compact += w
        if w in LEGAL_FORMS:
            cuts.update(range(start + 1, len(compact)))
        cuts.add(len(compact))
    return compact, cuts


def names_overlap(a: str, b: str) -> bool:
    """Some text could be matched by both names (`name_regex`) in overlapping places, so a document naming one could
    be read as naming the other: their letters agree where they overlap, and an end of either that falls inside the
    other falls where the other may have a space ('ATELIER NORD' and 'NORD STUDIO SRL' both match 'ATELIER NORD
    STUDIO SRL'; 'NORD' and 'NORDIC SRL' never share a match)."""
    (ca, wa), (cb, wb) = _shape(a), _shape(b)
    for d in range(1 - len(cb), len(ca)):  # where b starts, counted from a's start
        lo, hi = max(0, d), min(len(ca), d + len(cb))
        if not ca or not cb or ca[lo:hi] != cb[lo - d : hi - d]:
            continue
        inside_a = [p for p in (d, d + len(cb)) if 0 < p < len(ca)]  # b's ends inside a
        inside_b = [p for p in (-d, len(ca) - d) if 0 < p < len(cb)]  # a's ends inside b
        if builtins.all(p in wa for p in inside_a) and builtins.all(p in wb for p in inside_b):
            return True
    return False


def name_regex(name: str) -> re.Pattern | None:
    """A firm's name as fiscalm looks for it in a folded text (`fold`): its words in order, each escaped, with up to
    three spaces or punctuation marks between them ('ATELIER-NORD' = 'ATELIER NORD'), a legal form with or without
    dots ('S.R.L.' = 'SRL'), and never inside a longer word ('NORD' is not in 'NORDIC'). None when the name has no
    letter or digit. The name is data: nothing in it is read as a pattern."""
    words = name_words(name)
    if not words:
        return None
    parts = [r"\.?\s?".join(map(re.escape, w)) if w in LEGAL_FORMS else re.escape(w) for w in words]
    return re.compile(r"(?<![^\W_])" + r"[\W_]{0,3}".join(parts) + r"(?![^\W_])")


@dataclass(frozen=True)
class Account:
    """One of a company's bank accounts, whichever bank holds it."""

    iban: str
    currency: str
    bank: str  # banks.CATALOGUE id; "" when the IBAN's bank isn't in the catalogue
    pays: bool = False  # the account fiscalm prepares the payment file for (at most one per currency)
    retired: str = ""  # ISO date the account was replaced; "" while in use
    opened: str = ""  # YYYY-MM, the first month the account existed; "" when unknown (it is owed in every month)

    @property
    def statement_senders(self) -> tuple[str, ...]:
        """Catalogue mail sources while the account is current; retired and unknown banks add none."""
        return _banks.BY_ID[self.bank].statement_senders if not self.retired and self.bank in _banks.BY_ID else ()

    def to_config(self) -> dict:
        d: dict = {"iban": self.iban, "currency": self.currency}
        if self.bank:
            d["bank"] = self.bank
        if self.pays:
            d["pays"] = True
        if self.retired:
            d["retired"] = self.retired
        if self.opened:
            d["opened"] = self.opened
        return d

    def held_between(self, first: date | None, last: date | None) -> bool:
        """The account may have existed between these days: not opened after `last`'s month, not retired before
        `first`. An unknown day or date counts as held."""
        opened_later = bool(self.opened and last and self.opened > last.strftime("%Y-%m"))
        retired_before = bool(self.retired and first and self.retired < first.isoformat())
        return not (opened_later or retired_before)


@dataclass(frozen=True)
class Company:
    key: str  # short id used in paths, the CLI and the app's URL: upper case letters, digits, - and _
    legal_name: str
    cui: str
    accounts: tuple[Account, ...]  # every bank account, current and retired, whatever the bank
    name: str = ""  # the short name the app shows; the key when empty
    palette: str = PALETTES[0]
    aliases: tuple[str, ...] = ()  # other names the firm goes by on documents ("Alte nume ale firmei"), plain text

    def validated(self) -> Company:
        """Canonical facts accepted by the ledger writer, sharing the input validators."""
        entry = asdict(self)
        entry["accounts"] = list(entry["accounts"])
        entry["aliases"] = list(entry["aliases"])
        return company_from(entry, 0)

    def named_in(self, text: str) -> bool:
        """The legal name, with or without its Romanian suffix, or an alias names the firm."""
        return self.name_at(text) >= 0

    def name_at(self, text: str, *, suffix_free: bool = True) -> int:
        """First raw-text name column, or -1 when absent."""
        return next(iter(self.name_columns(text, suffix_free=suffix_free)), -1)

    def name_columns(self, text: str, *, suffix_free: bool = True) -> tuple[int, ...]:
        """All name columns; suffix-free ownership in a document needs labelled CUI evidence."""
        folded = fold(text)
        core_allowed = suffix_free and (
            len(text.strip().splitlines()) <= 1 or self.tax_id_match(text, labelled=True) is not None
        )
        starts = {
            m.start("company") if "company" in r.groupindex else m.start()
            for r in _name_regexes(self.legal_name, self.aliases)
            if core_allowed or "company" not in r.groupindex
            for m in r.finditer(folded)
        }
        columns, offset = [], 0
        for column, char in enumerate(text):
            if not starts:
                break
            width = len(fold(char))
            if width and offset in starts:
                columns.append(column)
                starts.remove(offset)
            offset += width
        return tuple(columns)

    def tax_id_match(self, text: str, *, labelled: bool = False) -> re.Match[str] | None:
        """First whole CUI, optionally requiring an ID label; amounts and rates are not IDs."""
        return next(self.tax_id_matches(text, labelled=labelled), None)

    def tax_id_matches(self, text: str, *, labelled: bool = False) -> Iterator[re.Match[str]]:
        return (m for m in _tax_regex(self.cui).finditer(text) if not labelled or m["label"])

    @property
    def current_accounts(self) -> tuple[Account, ...]:
        return tuple(a for a in self.accounts if not a.retired)

    @property
    def statement_senders(self) -> tuple[str, ...]:
        """Catalogue senders for current accounts only, deduplicated across banks and currencies."""
        return tuple(sorted({s for a in self.accounts for s in a.statement_senders}))

    @property
    def ibans(self) -> dict[str, str]:
        """currency -> the account used for it: the one payments are prepared for, else the first current one."""
        out: dict[str, str] = {}
        for a in sorted(self.current_accounts, key=lambda a: not a.pays):
            out.setdefault(a.currency, a.iban)
        return out

    @property
    def ibans_retired(self) -> tuple[tuple[str, str, str], ...]:
        return tuple((a.iban, a.currency, a.retired) for a in self.accounts if a.retired)

    @property
    def own_ibans(self) -> set[str]:
        """Current and retired accounts remain ours regardless of transaction date."""
        return {a.iban for a in self.accounts}

    def account(self, iban: str) -> Account | None:
        """The company's account with this IBAN, current or retired."""
        iban = normalize_iban(iban)
        return next((a for a in self.accounts if a.iban == iban), None)

    @property
    def paying_account(self) -> Account | None:
        """The RON account the payment file is prepared for, or None while the company has none."""
        return next((a for a in self.current_accounts if a.pays and a.currency == "RON"), None)

    @property
    def ron_iban(self) -> str:
        a = self.paying_account
        return a.iban if a else ""

    def tax_ids(self) -> set[str]:
        """The firm's tax id as documents carry it: its CUI. A VAT payer's VAT code is normally RO and the same digits,
        so the CUI's digits find either. A special VAT code with other digits (art. 317) is not the firm's here: a
        document showing it reads as naming another firm and its ownership check refuses, the safe side."""
        return {self.cui}


@functools.lru_cache(maxsize=256)
def _tax_regex(cui: str) -> re.Pattern:
    digits = r"[ \t]*".join(map(re.escape, cui))
    # A following decimal, rate or currency-like unit makes the number unsafe as an ID.
    end = r"(?!\w|[.,]\d|[ \t]*(?:%|[€£$])|[ \t]+[A-Z]{3}\b)"
    return re.compile(rf"(?<!\w)(?P<label>(?:{_TAX_LABEL})+)?{digits}{end}", re.I)


def _legal_core(legal_name: str) -> tuple[str, str] | None:
    """Strip only a final legal form, without changing shared alias/statement normalization."""
    words = name_words(legal_name)
    for count in range(1, min(3, len(words))):
        suffix = "".join(words[-count:])
        core = " ".join(words[:-count])
        if suffix in LEGAL_SUFFIXES and name_core(core):
            return core, suffix
    return None


@functools.lru_cache(maxsize=256)
def _name_regexes(legal_name: str, aliases: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    patterns = [r for r in map(name_regex, (legal_name, *aliases)) if r]
    if parts := _legal_core(legal_name):
        core = name_regex(parts[0])
        if parts[1] not in LEGAL_FORMS:
            suffix = r"\.?\s?".join(parts[1])
            patterns.append(re.compile(core.pattern + rf"[\W_]{{1,3}}{suffix}(?![^\W_])"))
        # Suffix-free names must fill a party field, never prefix another firm's name.
        tax_label = rf"(?:{_TAX_LABEL})+(?:[A-Z]{{2}}[ \t]*)?\d"
        start = r"(?:^|[ \t]{2,}|:)[ \t]*"
        end = rf"(?=[ \t]*\r?$|[ \t]{{2,}}|[ \t]*[,;][ \t]*{tax_label}|[ \t]+{tax_label})"
        patterns.append(re.compile(start + rf"(?P<company>{core.pattern})" + end, re.M))
    return tuple(patterns)


def company_from(c: dict, n: int) -> Company:
    """One firm entry, with its palette assigned in registry order. Refuses a key the app's URLs or file paths could not hold."""
    key = str(c.get("key", ""))
    if not KEY_RE.fullmatch(key) or key == "ALL":
        raise ConfigError(f"company key {key!r}: use 1-20 upper case letters, digits, - or _ (and not ALL)")
    if not str(c.get("legal_name") or "").strip():
        raise ConfigError(f"company {key}: legal_name is missing")
    cui = re.sub(r"[^0-9]", "", str(c.get("cui") or ""))
    cui = str(int(cui)) if cui else ""
    if not 2 <= len(cui) <= 10:  # a Romanian CUI/CIF: 2 to 10 digits, the RO prefix dropped
        raise ConfigError(f"company {key}: cui must be 2 to 10 digits (got {str(c.get('cui'))[:20]!r})")
    accounts = accounts_from(c, key)
    pal = c.get("palette") or PALETTES[n % len(PALETTES)]
    if pal not in PALETTES:
        raise ConfigError(f"company {key}: palette {pal!r} is not one of {', '.join(PALETTES)}")
    return Company(
        key=key,
        legal_name=c["legal_name"],
        cui=cui,
        accounts=accounts,
        aliases=aliases_from(c, key),
        name=c.get("name") or key,
        palette=pal,
    )


ALIAS_MAX = 100  # characters of one alias
ALIASES_MAX = 20  # aliases of one firm


def aliases_from(c: dict, key: str) -> tuple[str, ...]:
    """A company's `aliases`: a list of plain names, blanks dropped. Refuses anything else, so a hand edit that
    breaks the list is noticed rather than silently matching nothing."""
    rows = c.get("aliases") or []
    if (
        not isinstance(rows, list)
        or len(rows) > ALIASES_MAX
        or not builtins.all(isinstance(a, str) and len(a.strip()) <= ALIAS_MAX for a in rows)
    ):
        raise ConfigError(
            f"company {key}: aliases must be a list of at most {ALIASES_MAX} names of at most {ALIAS_MAX} characters."
        )
    return tuple(a.strip() for a in rows if a.strip())


_IBAN_SHAPE = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{8,30}")
_ACCOUNT_KEYS = {"iban", "currency", "bank", "pays", "retired", "opened"}
MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def accounts_from(c: dict, key: str) -> tuple[Account, ...]:
    """Validate accounts, refusing duplicate IBANs, multiple paying accounts per
    currency, retired paying accounts and banks that contradict the IBAN."""

    def bad(what: str) -> ConfigError:
        return ConfigError(f"company {key}: {what}.")

    rows = c.get("accounts", [])
    if not isinstance(rows, list):
        raise bad("accounts must be a list")
    from fiscalm.validate import iban_valid

    out: list[Account] = []
    for r in rows:
        if not isinstance(r, dict) or set(r) - _ACCOUNT_KEYS or not isinstance(r.get("iban"), str):
            raise bad("invalid account")
        iban, ccy = normalize_iban(r["iban"]), r.get("currency")
        retired_on = r.get("retired") or ""
        # Any non-empty currency, as the first installs accepted (normally an ISO code such as RON or EUR).
        if not _IBAN_SHAPE.fullmatch(iban) or not isinstance(ccy, str) or not ccy.strip():
            raise bad("invalid account")
        ccy = ccy.strip().upper()
        try:
            retired_on = date.fromisoformat(retired_on).isoformat() if retired_on else ""
        except (TypeError, ValueError):
            raise bad("invalid retired account date") from None
        if retired_on and not iban_valid(iban):
            raise bad("invalid retired IBAN")
        derived = _banks.for_iban(iban)
        bank = r.get("bank", derived.id if derived else "")
        if not isinstance(bank, str) or bank and bank not in _banks.BY_ID or derived and bank != derived.id:
            raise bad(f"the bank of account ending {iban[-4:]} doesn't match its IBAN")
        pays = r.get("pays", False)
        if not isinstance(pays, bool) or pays and retired_on:
            raise bad("a retired account cannot pay")
        opened = r.get("opened") or ""
        if not isinstance(opened, str) or opened and not MONTH.fullmatch(opened):
            raise bad("invalid account opening month")
        if opened and retired_on and opened > retired_on[:7]:
            raise bad("an account cannot open after it was retired")
        out.append(Account(iban, ccy, bank, pays=pays, retired=retired_on, opened=opened))
    if len({a.iban for a in out}) != len(out):
        raise bad("the same IBAN appears twice")
    paying = [a.currency for a in out if a.pays]
    if len(set(paying)) != len(paying):
        raise bad("more than one paying account in a currency")
    # current accounts first, as people read them
    return tuple(sorted(out, key=lambda a: bool(a.retired)))


def companies_from(cfg: dict) -> dict[str, Company]:
    out: dict[str, Company] = {}
    for n, c in enumerate(cfg.get("companies") or []):
        co = company_from(c, n)
        if co.key in out:
            raise ConfigError(f"company key {co.key} appears twice")
        out[co.key] = co
    return out


def company_by_cui(cui: str) -> Company | None:
    cui = re.sub(r"\D", "", cui or "")
    for c in all().values():
        if cui == c.cui:
            return c
    return None


def company_by_iban(iban: str) -> Company | None:
    iban = (iban or "").replace(" ", "").upper()
    for c in all().values():
        if iban and iban in c.own_ibans:
            return c
    return None


def get(key: str, L: Ledger | None = None) -> Company:
    key = key.upper().replace(" ", "")
    return all(L)[key]


NEEDS_COMPANY = f"Add a company in {PRODUCT} first."
NEEDS_COMPANY_RO = f"Adaugă mai întâi o firmă în {PRODUCT}."


def require_company() -> None:
    if not all():
        raise ConfigError(NEEDS_COMPANY)


def check_configured() -> int:
    """Tick gate: an empty firm registry exits 3 before any pipeline work."""
    backup.refuse_unfinished_restore(finance_home())  # a home without a ledger yet is not an empty registry
    if not all():
        return 3
    return 0


def all(L: Ledger | None = None) -> dict[str, Company]:
    """Read one consistent registry from the caller's ledger, with no process cache."""
    cached = getattr(L, "_firm_registry", None)
    if cached is not None:
        return cached
    if L is None:
        path = finance_home() / "state/ledger.db"
        if not path.exists():
            return {}
        with closing(Ledger(path, readonly=True)) as ledger:
            return all(ledger)
    with L.tx(immediate=False):
        accounts: dict[str, list[Account]] = {}
        for r in L.db.execute("SELECT * FROM firm_accounts ORDER BY company,position"):
            accounts.setdefault(r["company"], []).append(
                Account(r["iban"], r["currency"], r["bank"], bool(r["pays"]), r["retired"], r["opened"])
            )
        children = []
        for table, field in (("firm_aliases", "alias"),):
            grouped: dict[str, list[str]] = {}
            for r in L.db.execute(f"SELECT * FROM {table} ORDER BY company,position"):
                grouped.setdefault(r["company"], []).append(r[field])
            children.append(grouped)
        (aliases,) = children
        return {
            r["key"]: Company(
                r["key"],
                r["legal_name"],
                r["cui"],
                tuple(accounts.get(r["key"], [])),
                r["name"],
                r["palette"],
                tuple(aliases.get(r["key"], [])),
            )
            for r in L.db.execute("SELECT * FROM firms ORDER BY rowid")
        }
