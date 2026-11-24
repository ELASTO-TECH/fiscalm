"""Statements and their transactions belong to company accounts, so several banks can be read side by side."""

import dataclasses
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from firms import iban, set_firm
from pdf_fake import bt_statement
from test_camt import complete, fake_pdf, parse, write, xml

from fiscalm import companies, integrations, pack, recon
from fiscalm.bank import intake as statement_intake, statements, upload
from fiscalm.ledger import finance_home
from fiscalm.mail import imap as imapread, invoices as mail_invoices

ING = "RO00INGB0000000000000009"
X1, X2, Y = "RO00XXXX0000000000000001", "RO00XXXX0000000000000002", "RO00YYYY0000000000000002"
USD = iban("RO00BTRLUSDCRT0000000001")


def nord_with(monkeypatch, *accounts, keep=True):
    """NORD with `accounts` (iban, currency, bank[, fields]) added to its own, or instead of them."""
    nord = companies.get("NORD")
    added = tuple(companies.Account(i, c, b, **(extra[0] if extra else {})) for i, c, b, *extra in accounts)
    set_firm("NORD", dataclasses.replace(nord, accounts=(nord.accounts if keep else ()) + added))


def test_an_imported_statement_records_its_accounts_and_its_transactions_carry_theirs(ledger, tmp_path):
    assert recon.ingest_file(ledger, write(tmp_path, complete()), operator=True)["status"] == "added"
    (doc,) = ledger.documents(kind="statement")
    nord = companies.get("NORD")
    assert json.loads(doc["accounts"]) == sorted([nord.ibans["RON"], nord.ibans["EUR"]])
    ledger.record_statement(parse(tmp_path, xml()))
    rows = ledger.db.execute("SELECT DISTINCT iban, company FROM statement_txs").fetchall()
    assert [tuple(r) for r in rows] == [(nord.ibans["RON"], "NORD")]


def test_all_configured_accounts_required(ledger, tmp_path):
    p = write(tmp_path, xml(currency="EUR"))
    r = recon.ingest_file(ledger, p, operator=True)
    assert r["status"] == "refused" and "missing configured accounts: RON" in r["errors"][0]
    assert not ledger.documents(kind="statement")
    p.write_bytes(complete())
    assert recon.ingest_file(ledger, p, operator=True)["status"] == "added"


@pytest.mark.parametrize(
    "accounts,data,missing",
    [
        ([(ING, "EUR", "ing")], complete(), []),  # BT's RON and EUR, no ING: complete for BT
        ([(ING, "EUR", "ing")], xml(), ["EUR 0001"]),  # BT's EUR is missing
        ([(ING, "EUR", "ing")], xml(iban=ING, currency="EUR"), []),  # ING's statement owes nothing to BT
        ([(X1, "EUR", ""), (Y, "EUR", "")], xml(iban=X1, currency="EUR"), []),  # two banks outside the catalogue
        ([(X1, "EUR", ""), (Y, "EUR", "")], xml(iban=Y, currency="EUR"), []),
        ([(X1, "EUR", ""), (X2, "EUR", "")], xml(iban=X1, currency="EUR"), ["EUR 0002"]),  # one bank's code: both owed
        ([(X1, "USD", "bt")], complete(), ["USD 0001"]),  # a configured bank wins over the IBAN's code
        ([(USD, "USD", "bt", {"opened": "2026-11"})], complete(), []),  # opened after October: not owed yet
        ([(USD, "USD", "bt", {"opened": "2026-10"})], complete(), ["USD 0001"]),  # opened that month: owed
        ([(USD, "USD", "bt", {"opened": "2026-11"})], "two periods", ["USD 0001"]),  # which month? owed
        ([(USD, "USD", "bt", {"opened": "2026-11"})], "backwards", ["USD 0001"]),  # ends before it starts: owed
        ([(USD, "USD", "bt", {"retired": "2026-10-15"})], complete(), ["USD 0001"]),  # retired later: owed
        ([(USD, "USD", "bt", {"retired": "2026-09-30"})], complete(), []),  # retired before October: not owed
    ],
)
def test_a_statement_needs_every_account_at_its_bank_and_none_elsewhere(monkeypatch, tmp_path, accounts, data, missing):
    nord_with(monkeypatch, *accounts)
    st = parse(tmp_path, data if isinstance(data, bytes) else complete())
    if data == "two periods":  # a PDF whose sections name different periods
        st = dataclasses.replace(st, one_period=False)
    if data == "backwards":  # December 1 to September 30
        st = dataclasses.replace(st, period_start=date(2026, 12, 1), period_end=date(2026, 9, 30))
    assert statement_intake.missing_accounts(st) == missing


@pytest.mark.parametrize("currencies,missing", [(("RON", "EUR"), []), (("RON",), ["EUR 0001"])])
def test_an_older_bt_pdf_is_complete_only_with_every_account(currencies, missing):
    """2021-2023 layout: `Extras numarul N` comes before the currency on each account's IBAN line."""
    ibans = companies.get("NORD").ibans
    text = "din 01/10/2026 - 31/10/2026\n" + "".join(
        f"CONT {c}CRT Valuta Cont de disponibil\nExtras numarul 6     {c}     Cod IBAN: {ibans[c]}\n"
        for c in currencies
    )
    assert statement_intake.missing_accounts(statements.parse_text(text, "NORD")) == missing


@pytest.mark.parametrize(
    "accounts,label",
    [
        (None, "Altă bancă"),  # absent metadata proves no bank
        (["RO24BTRLRONCRT0000000001"], "BT"),
        ([ING], "ING Bank"),
        (["RO24BTRLRONCRT0000000001", ING], "BT, ING Bank"),
        (["DE00ZZZZ00000000000000"], "Altă bancă"),
    ],
)
def test_the_accountants_mail_names_each_statements_bank(accounts, label):
    doc = {"kind": "statement", "accounts": json.dumps(accounts) if accounts else None}
    assert pack.statement_bank(doc) == label


# ---- one statement per account and month; a stored one that can't be read blocks, never guesses


def test_the_same_account_twice_in_a_month_is_refused_another_account_is_not(ledger, tmp_path):
    st = parse(tmp_path, xml())
    ledger.add_document(
        "same",
        "NORD",
        "2026-10",
        "statement",
        str(tmp_path / "gone.xml"),
        accounts=statement_intake.statement_ibans(st),
    )
    with pytest.raises(statements.StatementError, match="already imported for this account and month"):
        statement_intake.refuse_overlap(ledger, st, "different")
    ledger.db.execute("DELETE FROM documents WHERE sha='same'")
    ledger.db.commit()
    # another bank's statement for the same month, its file not even read
    ledger.add_document("other", "NORD", "2026-10", "statement", str(tmp_path / "gone.xml"), accounts=[ING])
    statement_intake.refuse_overlap(ledger, st, "different")


@pytest.mark.parametrize(
    "stored,refusal",
    [
        (xml(currency="EUR"), None),  # RON now, EUR before: no overlap
        (xml(), (statements.StatementError, "this account")),
        (
            None,
            (statement_intake.StoredStatementUnavailable, None),
        ),  # its file is gone: when in doubt, nothing is imported
        (b"", (statement_intake.StoredStatementUnavailable, "which accounts")),  # it reads without accounts
    ],
)
def test_a_legacy_statement_is_read_to_learn_its_accounts(ledger, tmp_path, monkeypatch, stored, refusal):
    """A statement stored before accounts were recorded is read again to learn them."""
    st = parse(tmp_path, xml())
    old = tmp_path / "old.xml"
    if stored:
        old.write_bytes(stored)
    elif stored is not None:
        monkeypatch.setattr(statements, "parse", lambda path, co: dataclasses.replace(st, accounts=[]))
    ledger.add_document("old", "NORD", "2026-10", "statement", str(old))
    if refusal is None:
        statement_intake.refuse_overlap(ledger, st, "different")
    else:
        with pytest.raises(refusal[0], match=refusal[1]):
            statement_intake.refuse_overlap(ledger, st, "different")


@pytest.mark.parametrize(
    "month,kind,status", [("2025-10", "camt", "added"), ("2026-09", "camt", "error"), ("2026-09", "pdf", "error")]
)
def test_a_missing_statement_blocks_only_its_neighbouring_months_and_is_retried(
    ledger, tmp_path, monkeypatch, month, kind, status
):
    ledger.add_document("old", "NORD", month, "statement", str(tmp_path / "missing.pdf"))
    if kind == "pdf":
        assert recon.ingest_file(ledger, fake_pdf(monkeypatch, tmp_path))["status"] == status
    else:
        assert recon.ingest_file(ledger, write(tmp_path, complete()), operator=True)["status"] == status


def test_a_months_report_reads_every_banks_statement(ledger, tmp_path, monkeypatch):
    nord_with(monkeypatch, (ING, "EUR", "ing"))
    bt, ing = write(tmp_path, complete(), "bt.xml"), write(tmp_path, xml(iban=ING, currency="EUR"), "ing.xml")
    ledger.add_document("bt", "NORD", "2026-10", "statement", str(bt), accounts=["x"])
    ledger.add_document("ing", "NORD", "2026-10", "statement", str(ing), accounts=[ING])
    view = recon.month_view(ledger, "NORD", "2026-10", [])
    text, summary = recon.build_report(ledger, "NORD", "2026-10", tmp_path)
    assert summary["transactions"] == len(parse(tmp_path, complete()).txs) + 1  # BT's and ING's
    assert ING in text and companies.get("NORD").ibans["RON"] in text
    assert view["balanced"] is True
    ing.unlink()  # one bank's file gone: the month can't be judged
    assert recon.month_view(ledger, "NORD", "2026-10", [])["balanced"] is None


# ---- which company's password a BT PDF is tried with


@pytest.mark.parametrize(
    "accounts,only,tried",
    [
        (None, "STUDIO9", ["STUDIO9"]),  # an upload is opened only with its company's password
        ("moved", None, ["NORD", "STUDIO9"]),  # a retired BT account still gets its last BT statements
        ("ing", None, ["STUDIO9"]),  # only companies that bank at BT get BT's PDFs
    ],
)
def test_a_bt_pdf_is_tried_only_with_companies_that_bank_at_bt(monkeypatch, tmp_path, accounts, only, tried):
    if accounts == "moved":
        nord_with(
            monkeypatch,
            (ING, "RON", "ing", {"pays": True}),
            *(
                (a.iban, a.currency, a.bank, {"pays": False, "retired": "2026-10-01"})
                for a in companies.get("NORD").accounts
            ),
            keep=False,
        )
    elif accounts == "ing":
        nord_with(monkeypatch, (ING, "RON", "ing", {"pays": True}), keep=False)
    keys = []

    def lookup(key):
        keys.append(key)
        raise statements.secrets.SecretError("none")

    with pytest.raises(statements.SecretsUnavailable):
        statements.open_encrypted(tmp_path / "x.pdf", lookup, only=only)
    assert keys == tried


# ---- one month's statement in several files: one per account, or daily ones, checked and stored as one


def part(lo: int, hi: int, *currencies: str, opening: str = "100.00", month: int = 10) -> statements.Statement:
    """A file of NORD's for days lo..hi: each account opens at `opening` and gains 10.00, its balances proven."""
    ibans, start = companies.get("NORD").ibans, Decimal(opening)
    accounts = [
        statements.Account(ibans[c], c, [], Decimal(0), Decimal(10), True, start + 10, date(2026, month, hi))
        for c in currencies
    ]
    return statements.Statement("NORD", date(2026, month, lo), date(2026, month, hi), accounts)


@pytest.mark.parametrize(
    "parts,refusal",
    [
        (lambda: [part(1, 31, "RON"), part(1, 31, "EUR")], None),  # one file per account
        (lambda: [part(1, 15, "RON"), part(16, 31, "RON", opening="110.00"), part(1, 31, "EUR")], None),  # daily RON
        (
            lambda: [part(1, 10, "RON"), part(12, 31, "RON", opening="110.00")],
            "full calendar month",
        ),  # the 11th is missing
        (
            lambda: [part(1, 10, "RON"), part(11, 20, "RON", opening="110.00")],
            "full calendar month",
        ),  # stops on the 20th
        (lambda: [part(1, 31, "RON"), part(1, 31, "RON", "EUR")], "appears twice"),
        (lambda: [part(12, 12, "RON"), part(12, 12, "EUR")], "full calendar month"),  # one day is not the month
        (lambda: [part(12, 12, "RON", "EUR")], "full calendar month"),  # one daily file alone neither
        (lambda: [part(1, 14, "RON"), part(15, 14, "RON"), part(15, 31, "RON")], "Cannot verify"),  # runs backwards
        (
            lambda: [part(1, 15, "RON"), part(16, 31, "RON", opening="999.00")],
            "do not chain",
        ),  # a statement between is lost
        (lambda: [part(1, 31, "RON"), part(1, 30, "EUR", month=11)], "different months"),
        (lambda: [part(1, 31, "RON"), dataclasses.replace(part(1, 31, "EUR"), one_period=False)], "one period"),
    ],
)
def test_files_of_one_month_make_one_statement_only_when_every_accounts_days_are_there_once(parts, refusal):
    parts = parts()  # built once the test's firms are set up
    if refusal:
        with pytest.raises(statements.StatementError, match=refusal):
            statement_intake.combine(parts)
        return
    st = statement_intake.combine(parts)
    assert (st.period_start, st.period_end, statement_intake.missing_accounts(st)) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
        [],
    )
    assert st.accounts == [a for p in parts for a in p.accounts]  # each file keeps its accounts and their trust


def bt_files(folder, periods=((1, 31),), currencies=("RON", "EUR")):
    """NORD's encrypted BT PDFs for October 2026, one per account and period, each crediting 10.00, balances chained."""
    nord = companies.get("NORD")
    files = []
    for c in currencies:
        balance = Decimal("100.00")
        for lo, hi in periods:
            day = f"{lo:02d}/10/2026"
            moves = [(day, f"SET-{c}-{lo}", Decimal("10.00"))]
            name = folder / f"extras-{c}-{lo:02d}.pdf"
            bt_statement(name, nord.cui, (day, f"{hi:02d}/10/2026"), [(c, nord.ibans[c], balance, moves)])
            files.append(name)
            balance += 10
    return files


DAYS = tuple((d, d) for d in range(1, 32))


@pytest.mark.parametrize("periods", [((1, 31),), DAYS], ids=["per-account", "daily"])
def test_the_operator_imports_one_months_bt_pdfs_together(ledger, tmp_path, periods):
    files = bt_files(tmp_path, periods)
    (result,) = recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")
    assert (result["status"], result["month"], result["transactions"]) == ("added", "2026-10", 2 * len(periods))
    docs = ledger.documents(kind="statement")
    assert len(docs) == len(files) and all(len(json.loads(d["accounts"])) == 1 for d in docs)
    days = ledger.db.execute("SELECT COUNT(DISTINCT day), COUNT(DISTINCT iban) FROM statement_txs").fetchone()
    assert tuple(days) == (len(periods), 2)  # every file's lines are on record, for both accounts
    again = recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")
    assert [r["status"] for r in again] == ["duplicate"]


@pytest.mark.parametrize(
    "case,refusal",
    [
        ("a day missing", "full calendar month"),
        ("an account missing", "missing configured accounts: EUR 0001"),
        ("stored before", "already imported"),
        ("kept as a receipt", "kept as another document"),  # those bytes are a document already: no takeover
        ("stored copy gone", "restore the stored document"),  # imported before, but its file is missing
    ],
)
def test_an_incomplete_or_overlapping_set_stores_nothing(ledger, tmp_path, case, refusal):
    currencies = ("RON",) if case == "an account missing" else ("RON", "EUR")
    files = bt_files(tmp_path, DAYS[:10] + ((11, 31),), currencies)
    if case == "a day missing":
        files.remove(tmp_path / "extras-EUR-05.pdf")
    if case == "stored copy gone":  # the month's per-account files, imported, then one stored copy lost
        files = bt_files(tmp_path)
        assert recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")[0]["status"] == "added"
        Path(ledger.documents(kind="statement")[0]["path"]).unlink()
    if case == "stored before":  # the same month, one file per account, imported first
        (tmp_path / "monthly").mkdir()
        monthly = recon.ingest_files(ledger, bt_files(tmp_path / "monthly"), password_lookup=lambda co: "synthetic")
        assert [r["status"] for r in monthly] == ["added"]
    if case == "kept as a receipt":
        ledger.add_document(recon.sha256(files[-1]), "NORD", "2026-10", "receipt", "/x/r.pdf", source="links:1")
    before = ledger.documents(kind="statement")
    (result,) = recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")
    assert result["status"] == ("error" if case == "stored copy gone" else "refused") and refusal in result["errors"][0]
    assert ledger.documents(kind="statement") == before and not ledger.used_txs()
    folder = recon.month_dir("NORD", "2026-10") / "extrase"
    kept = len(before) - (case == "stored copy gone")
    assert len(list(folder.iterdir()) if folder.exists() else []) == kept
    assert not list((finance_home() / "state" / "tmp").rglob("*.pdf"))  # no decrypted copy is left behind


@pytest.mark.parametrize(
    "case,status,error", [("named for another month", "refused", "another month"), ("symlinked tmp", "error", "Unsafe")]
)
def test_a_bt_pdf_is_refused_before_it_could_be_misfiled_or_decrypted_outside(ledger, tmp_path, case, status, error):
    (ron,) = bt_files(tmp_path, currencies=("RON",))
    if case == "named for another month":  # an October statement whose name says November
        ron = ron.rename(tmp_path / "extL_Nov_2026_allmerge.pdf")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (finance_home() / "state" / "tmp").symlink_to(outside)
    (result,) = recon.ingest_files(ledger, [ron], password_lookup=lambda co: "synthetic")
    assert result["status"] == status and error in result["errors"][0] and not ledger.documents(kind="statement")
    assert [p.name for p in tmp_path.rglob("*.pdf")] == [ron.name]  # only the source: no decrypted copy anywhere


INGEST = recon.ingest_statements


def bring(ledger, caller: str, files, monkeypatch, account: str = "") -> str:
    """BT PDFs in through one of the ways they arrive: "added", or the accounts the import says are missing."""
    if caller == "app":
        try:
            return upload.accept_pdfs(ledger, "NORD", "", [f.read_bytes() for f in files])["status"]
        except upload.Refused as e:
            return e.message
    results = []  # what the import said, read as it returns
    monkeypatch.setattr(recon, "ingest_statements", lambda *a, **kw: results.extend(INGEST(*a, **kw)) or results)
    if caller == "cli":
        recon.ingest_files(ledger, files)
    else:
        sender = companies.get("NORD").statement_senders[0]
        named = (imapread.Attachment("pdf", f.read_bytes(), f"extL_Oct_2026_allmerge_{f.stem}.pdf") for f in files)
        mail_invoices.handle(
            ledger, imapread.Message(account, "k@x", sender, sender.lower(), "s", "2026-11-02", tuple(named))
        )
    (result,) = results
    if result["status"] != "refused":
        return result["status"]
    return upload.incomplete(result["missing"]) if "missing" in result else upload._say(result["errors"])


@pytest.mark.parametrize("caller", ["cli", "mail", "app"])
def test_every_way_a_bt_pdf_arrives_needs_every_account_and_only_the_firms(ledger, sec, tmp_path, monkeypatch, caller):
    integrations.set_statement_password(ledger, "NORD", "bt", "synthetic")
    inbox = {"host": "imap.example.test", "username": "u", "password": "x", "from_address": "books@example.com"}
    account = integrations.add(ledger, "mail", "Synthetic inbox", inbox)["id"]
    ron, eur = bt_files(tmp_path)
    assert bring(ledger, caller, [ron], monkeypatch, account) == upload.incomplete(["EUR 0001"])
    assert not ledger.documents(kind="statement") and not ledger.db.execute("SELECT 1 FROM statement_txs").fetchone()
    nord = companies.get("NORD")
    other = [("RON", "RO94BTRLRONCRT0000000002", Decimal("0.00"), [])]  # STUDIO9's account, in a file with NORD's CUI
    foreign = bt_statement(tmp_path / "other.pdf", nord.cui, ("01/10/2026", "31/10/2026"), other)
    assert bring(ledger, caller, [ron, eur, foreign], monkeypatch, account) == upload.OTHER_ACCOUNT
    assert bring(ledger, caller, [ron, eur], monkeypatch, account) == "added"
    assert len(ledger.documents(kind="statement")) == 2
