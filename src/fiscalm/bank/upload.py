"""A statement uploaded in fiscalm for one company and, optionally, one of its banks.

The bytes are untrusted: the type is read from the content, not the name; the file goes to a private temporary
path fiscalm names; a BT PDF is opened only with that company's password; a CAMT must name that company's accounts;
when a bank is given every account of the statement must be at that bank. Anything else is refused before a byte is
stored. What is accepted goes through the same import as a statement from mail or the command line (overlap,
completeness, matching), so an upload is never a way around a rule."""

from __future__ import annotations

import calendar
import os
import secrets
import shutil
import time
from datetime import date
from pathlib import Path

from fiscalm import companies
from fiscalm.bank import catalogue as banks, statements
from fiscalm.bank.camt import MAX_XML
from fiscalm.ledger import Ledger, finance_home
from fiscalm.pack import LUNI

MAX_PDF = 20 * 1024 * 1024
MAX_FILES = 100  # PDFs uploaded together: a month of daily statements for a few accounts
TYPES = {
    "application/pdf": "pdf",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "mt940",
    "application/octet-stream": "mt940",
    "text/csv": "csv",
    "application/vnd.ms-excel": "csv",
}

ADDED = "Extrasul a fost încărcat."
DUPLICATE = "Extrasul era deja încărcat."
NOT_A_STATEMENT = "Fișierul nu este un extras de cont PDF, CAMT (XML), MT940 sau CSV."
PICK_ACCOUNT = "Un extras CSV se încarcă pe un cont: trebuie ales contul din banca lui."
NEEDS_COLUMNS = "E primul CSV al acestui cont: trebuie alese coloanele pentru dată, sumă și detalii."
TOO_BIG = "Fișierul este prea mare pentru un extras."
WRONG_COMPANY = "Extrasul nu este al acestei firme sau parola extraselor nu îl deschide."
WRONG_BANK = "Extrasul are conturi de la altă bancă decât cea aleasă."
INCOMPLETE = (
    "Extrasul nu cuprinde {accounts}. Încarcă extrasul cu toate conturile firmei de la această bancă "
    "sau, împreună, extrasele fiecărui cont. "
    "Dacă firma a deschis contul după luna extrasului, administratorul trece luna deschiderii din linia de comandă."
)
NOT_READ = "Extrasul nu a putut fi citit. Verifică fișierul, apoi încearcă din nou."
NO_PASSWORD = "Parola extraselor acestei firme lipsește sau nu poate fi citită. Se adaugă în Setări › Firme › Bănci."
NOT_SAVED = "Extrasul nu a putut fi încărcat: {why}"


def pdf_support() -> str:
    """Name catalogue PDF readers, including the case where none is available."""
    names = [b.label for b in banks.CATALOGUE if b.reads_pdf]
    readers = (", ".join(names[:-1]) + " și " + names[-1]) if len(names) > 1 else "".join(names)
    return (
        f"fiscalm citește extrase PDF doar de la {readers}. " if readers else "fiscalm nu citește extrase PDF. "
    ) + "Pentru alte bănci se încarcă extrasul CAMT (XML) sau MT940."


class Refused(Exception):
    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


def kind_of(data: bytes, content_type: str) -> str:
    """'pdf' or 'xml', from the content; the declared type has to agree."""
    declared = TYPES.get((content_type or "").split(";")[0].strip().lower())
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    from fiscalm.bank.csv import MAX_CSV
    from fiscalm.bank.mt940 import MAX_MT940, looks_like

    found = (
        "pdf"
        if head.startswith(b"%PDF-")
        else "xml"
        if head.startswith(b"<")
        else "mt940"
        if looks_like(data)
        else "csv"
        if declared == "csv" and b"\x00" not in data[:4096]
        else None
    )
    if not declared or found is None or found != declared:
        raise Refused(415, NOT_A_STATEMENT)
    if len(data) > {"pdf": MAX_PDF, "xml": MAX_XML, "mt940": MAX_MT940, "csv": MAX_CSV}[found]:
        raise Refused(413, TOO_BIG)
    return found


def _checks(company: str, bank: str):
    """What an uploaded statement must be before anything is stored: every account the company's (a PDF that isn't
    encrypted opens with any password, and its text naming the company proves nothing about its accounts), every
    account at `bank` when one is given, and every current account the company holds at those banks present."""
    from fiscalm.bank.intake import refuse_incomplete

    co = companies.get(company)

    def check(st) -> None:
        if not st.accounts:
            raise statements.StatementError(NOT_READ)
        for a in st.accounts:
            own = co.account(a.iban)
            if not own:
                raise statements.StatementError(OTHER_ACCOUNT)
            b = own.bank or (banks.for_iban(a.iban).id if banks.for_iban(a.iban) else "")
            if bank and b != bank:
                raise statements.StatementError(WRONG_BANK)
        refuse_incomplete(st)  # a CSV speaks for its one account and its own days only

    return check


def _confined(company: str) -> Path:
    """The data root's company directory and a fresh private staging directory for this upload, refusing a symlink
    anywhere on the way, so a stored or decrypted copy never lands outside the data root."""
    home = finance_home()
    root = home.resolve()
    for p in (home / company, home / "state", home / "state" / "tmp"):
        if p.is_symlink() or (p.exists() and not p.resolve().is_relative_to(root)):
            raise Refused(409, NOT_SAVED.format(why="dosarul de date nu este sigur"))
    tmp = home / "state" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    for old in tmp.glob("upload-*"):  # what a crash left: never older than an hour while an upload runs
        if old.is_dir() and not old.is_symlink() and time.time() - old.stat().st_mtime > 3600:
            shutil.rmtree(old, ignore_errors=True)
    staging = tmp / f"upload-{secrets.token_hex(12)}"
    staging.mkdir(mode=0o700)
    return staging


STORED = "Un extras încărcat mai demult nu mai poate fi citit. Verifică arhiva."
OTHER_ACCOUNT = (
    "Extrasul are un cont care nu este al acestei firme. Încarcă-l la firma contului sau adaugă contul la Conturi."
)
PARTIAL = "Extrasul trebuie să cuprindă toată luna (1–{last} {month}). Încarcă extrasul lunii încheiate."
RESTATE = "Extrasul nu are toate zilele sau soldurile lunii. Descarcă din bancă extrasul lunii întregi, într-un singur fișier."
# Refusals people can act on, by what the import said; anything else reads as NOT_READ, so no path, file name or
# parser text from the file ever leaves.
KNOWN = (
    (  # first: the column names it lists come from the file and may contain any other key
        "CSV columns not found",
        "Fișierul CSV nu are coloanele alese pentru acest cont. Descarcă-l din bancă în același format.",
    ),
    ("files of different months", "Fișierele sunt din luni diferite. Încarcă împreună extrasele unei singure luni."),
    (
        "Statement set must cover",
        "Extrasele nu cuprind toate zilele lunii pentru fiecare cont. Încarcă toate extrasele lunii.",
    ),
    ("appears twice", "Un cont apare de două ori pentru aceleași zile. Încarcă fiecare extras o singură dată."),
    ("every file must name one period", RESTATE),
    ("do not chain", "Soldurile extraselor nu se leagă: lipsește un extras. Încarcă toate extrasele lunii."),
    ("Overlapping statement", "Extrasul se suprapune cu unul încărcat deja pentru același cont."),
    ("already imported", "Extrasul pentru acest cont și această lună este deja încărcat."),
    ("missing configured accounts", "Extrasul nu cuprinde toate conturile firmei de la această bancă."),
    ("Cannot read a stored statement", STORED),
    ("Cannot tell which accounts", STORED),
    ("Cannot verify adjacent", STORED),
    ("does not belong to the selected company", OTHER_ACCOUNT),
    ("not a current account of the company", OTHER_ACCOUNT),
    ("different months", "Extrasul cuprinde luni diferite. Încarcă un extras pentru fiecare lună."),
    ("same calendar month", "Extrasul cuprinde luni diferite. Încarcă un extras pentru fiecare lună."),
    ("final opening (60F)", RESTATE),
    ("do not chain", RESTATE),
    ("one opening (OPBD", RESTATE),
    ("add up", "Tranzacțiile din extras nu dau soldurile lui. Descarcă din nou extrasul din bancă."),
    (
        "require manual review",
        "Extrasul are o plată stornată, returnată sau cu comision inclus, pe care fiscalm nu o importă automat. "
        "Încarcă extrasul lunii în alt format, de exemplu CSV.",
    ),
    ("too large", TOO_BIG),
    ("Expected CAMT.053", "Fișierul XML nu este un extras CAMT.053. Descarcă din bancă extrasul în format CAMT.053."),
    ("Unsupported statement", NOT_A_STATEMENT),
    ("cannot determine statement month", "Extrasul PDF nu arată perioada. Descarcă din bancă extrasul complet."),
    ("PDF account IBAN missing", "Extrasul PDF nu arată IBAN-ul contului. Descarcă din bancă extrasul complet."),
    ("PDF account currency", "Extrasul PDF nu arată clar moneda contului. Descarcă din bancă extrasul complet."),
    ("CSV encoding", "Fișierul CSV are o codare necunoscută. Salvează-l ca UTF-8, apoi încarcă-l din nou."),
    ("CSV has no rows", "Fișierul CSV nu are rânduri sau are prea multe. Încarcă extrasul unei singure luni."),
    (
        "CSV date",
        "O dată din fișierul CSV nu are formatul ales pentru acest cont. Verifică fișierul descărcat din bancă.",
    ),
    ("CSV amount", "O sumă din fișierul CSV nu poate fi citită. Verifică fișierul descărcat din bancă."),
    ("CSV row", "Un rând din fișierul CSV nu are o singură sumă. Verifică fișierul descărcat din bancă."),
    ("CSV has identical rows", "Fișierul CSV are rânduri identice. Descarcă-l din bancă cu coloana de referință."),
    ("Unsafe statement storage", NOT_SAVED.format(why="dosarul de date nu este sigur")),
    (
        "Not enough storage space",
        "Nu mai este spațiu pentru extras. Eliberează spațiu pe server, apoi încearcă din nou.",
    ),
)
OWN = (WRONG_BANK, WRONG_COMPANY, OTHER_ACCOUNT, NOT_READ)


def incomplete(accounts: list[str]) -> str:
    """INCOMPLETE naming each missing account ("EUR 0001") by currency and last four characters."""
    named = ", ".join(a.replace(" ", " ••••") for a in accounts)
    return INCOMPLETE.format(accounts=("contul " if len(accounts) == 1 else "conturile ") + named)


def _say(errors) -> str:
    text = " ".join(errors or [])
    if text in OWN:
        return text
    return next((m for k, m in KNOWN if k in text), NOT_READ)


def _refusal(e: statements.StatementError) -> str:
    """What a person reads for a statement the import refused."""
    if isinstance(e, statements.PartialMonth):
        last = calendar.monthrange(e.month.year, e.month.month)[1]
        return PARTIAL.format(last=last, month=LUNI[e.month.month - 1])
    if isinstance(e, statements.MissingAccounts):
        return incomplete(e.accounts)
    return _say([str(e)])


def accept(L: Ledger, company: str, bank: str, data: bytes, content_type: str, account: str = "") -> dict:
    """Import one uploaded statement for `company` (and `bank`, "" for any of its banks). Returns the import's result
    with a Romanian `message`; raises Refused for what is never imported."""
    if company not in companies.all(L) or (bank and bank not in banks.BY_ID):
        raise Refused(400, NOT_A_STATEMENT)
    kind = kind_of(data, content_type)
    if kind == "pdf" and bank and not banks.BY_ID[bank].reads_pdf:
        raise Refused(422, pdf_support())
    csv_account = None
    if kind == "csv":  # a CSV names no account: it is uploaded on one, read with the columns picked for it
        from fiscalm.bank import csv as cs

        acct = cs.account_by_id(company, account) if account else None
        if not acct or (bank and acct.bank != bank):
            raise Refused(400, PICK_ACCOUNT)
        mapping = cs.saved_mapping(L, company, acct.iban)
        if mapping is None:
            try:
                sample = cs.preview(data)
            except statements.StatementError as e:
                raise Refused(422, _refusal(e)) from None
            raise Refused(409, NEEDS_COLUMNS, sample | {"account": account})
        csv_account = (acct.iban, mapping)
    staging = _confined(company)
    try:
        # fiscalm names the file; nothing from the request reaches a path. The BT parser reads the month from a
        # bank-style name or, failing that, from the text.
        path = _write(staging / f"upload-allmerge.{kind}", data)
        if kind == "pdf":
            return _answer(_pdfs(L, company, bank, staging, [path]), company)
        from fiscalm.bank.intake import StoredStatementUnavailable, ingest

        try:
            result = ingest(L, path, company, _checks(company, bank), csv_account)
        except StoredStatementUnavailable as e:
            raise Refused(409, _refusal(e)) from None
        except statements.StatementError as e:
            raise Refused(422, _refusal(e)) from None
        return _answer(result, company)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def accept_pdfs(L: Ledger, company: str, bank: str, files: list[bytes]) -> dict:
    """Import several BT PDFs uploaded together for `company`: one month's files, one per account or one per day,
    checked as one statement and stored together or not at all (recon.ingest_statements)."""
    if company not in companies.all(L) or (bank and bank not in banks.BY_ID) or not 0 < len(files) <= MAX_FILES:
        raise Refused(400, NOT_A_STATEMENT)
    for data in files:
        if kind_of(data, "application/pdf") != "pdf":
            raise Refused(415, NOT_A_STATEMENT)
    if bank and not banks.BY_ID[bank].reads_pdf:
        raise Refused(422, pdf_support())
    staging = _confined(company)
    try:
        paths = [_write(staging / f"upload-{i}-allmerge.pdf", data) for i, data in enumerate(files)]
        return _answer(_pdfs(L, company, bank, staging, paths), company)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write(path: Path, data: bytes) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _pdfs(L: Ledger, company: str, bank: str, staging: Path, paths: list[Path]) -> dict:
    """BT PDFs opened only with the company's password, read as one statement (one file: itself)."""
    from fiscalm import recon

    try:
        (result,) = recon.ingest_statements(
            L,
            paths,
            finance_home(),
            only=company,
            check=_checks(company, bank),
            source="statement-import",
            tmp_dir=staging,
            one_set=True,
        )
    except (ValueError, ArithmeticError, IndexError, KeyError):  # a damaged or hostile PDF's text
        raise Refused(422, NOT_READ) from None
    if result["status"] == "not_ours":  # no password opened it, or what opened isn't this firm's statement
        said = _say(result.get("errors"))
        raise Refused(422, WRONG_COMPANY if said == NOT_READ else said)
    if result["status"] == "error":
        errors = result.get("errors") or []
        raise Refused(409, _say(errors) if _say(errors) != NOT_READ else NO_PASSWORD)
    if result["status"] == "refused":
        missing = result.get("missing")  # the engine's own list, never words read back from an error text
        if result.get("partial"):  # the engine's own month
            raise Refused(422, _refusal(statements.PartialMonth("PDF", date.fromisoformat(result["partial"]))))
        raise Refused(422, incomplete(missing) if missing else _say(result.get("errors")))
    return result


def _answer(result: dict, company: str) -> dict:
    if result.get("company") != company:  # defence in depth: the import named another company
        raise Refused(422, WRONG_COMPANY)
    result["message"] = DUPLICATE if result["status"] == "duplicate" else ADDED
    result.pop("file", None)
    result.pop("unprotected", None)  # no server path leaves fiscalm
    return result
