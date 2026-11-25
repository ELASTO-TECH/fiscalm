"""Statements and their transactions belong to company accounts, so several banks can be read side by side."""

import dataclasses
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from firms import iban, set_firm
from pdf_fake import bt_statement
from test_camt import complete, fake_pdf, parse, write, xml

from fiscalm import companies, integrations, pack, recon
from fiscalm.bank import banking_days, coverage, intake as statement_intake, statements, upload
from fiscalm.ledger import finance_home
from fiscalm.mail import imap as imapread, invoices as mail_invoices
from fiscalm.payments import engine as pay
from fiscalm.pdftext import pdf_text

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
    october = date(2026, 10, 1), date(2026, 10, 31)
    assert all(coverage.covered(ledger, "NORD", i, *october, trusted_only=True) for i in nord.ibans.values())
    ledger.record_statement(parse(tmp_path, xml()))
    rows = ledger.db.execute("SELECT DISTINCT iban, company FROM statement_txs").fetchall()
    assert [tuple(r) for r in rows] == [(nord.ibans["RON"], "NORD")]


def test_a_statement_of_some_accounts_is_imported_and_names_those_still_to_come(ledger, tmp_path):
    r = recon.ingest_file(ledger, write(tmp_path, xml(currency="EUR")), operator=True)
    assert (r["status"], r["missing"]) == ("added", ["RON 0001"])
    assert statement_intake.month_missing(ledger, "NORD", "2026-10") == ["RON 0001"]


def test_an_account_in_another_currency_than_the_firms_is_refused(ledger, tmp_path):
    ron = companies.get("NORD").ibans["RON"]
    r = recon.ingest_file(ledger, write(tmp_path, xml(iban=ron, currency="EUR")), operator=True)
    assert r["status"] == "refused" and "currency differs" in r["errors"][0] and not ledger.documents()
    # nor does it stand as a neighbouring month's evidence
    wrong = SimpleNamespace(st=parse(tmp_path, xml(iban=ron, currency="EUR")))
    assert recon._checked({("NORD", "2026-10"): [wrong]}, None) == []


def test_a_statement_learned_from_its_file_names_accounts_only_for_its_own_month(ledger, tmp_path, monkeypatch):
    """A row stored before accounts were recorded whose file reads as another month proves no account for its own."""
    october = str(write(tmp_path, complete()))
    ledger.add_document("sep", "NORD", "2026-09", "statement", october)
    assert statement_intake.learn_accounts(ledger, ledger.document("sep")) == []
    assert (
        ledger.document("sep")["accounts"] is None and statement_intake.month_missing(ledger, "NORD", "2026-09") is None
    )
    ledger.add_document("oct", "NORD", "2026-10", "statement", october)
    assert len(statement_intake.learn_accounts(ledger, ledger.document("oct"))) == 2
    # STUDIO9's October read for NORD (a PDF reads under the firm it is opened for): accounts that aren't NORD's
    stranger = statements.Statement("NORD", date(2026, 10, 1), date(2026, 10, 31), [statements.Account(X1, "EUR")])
    ledger.add_document("x", "NORD", "2026-10", "statement", "/x/x.pdf")
    monkeypatch.setattr(statements, "parse", lambda path, co: stranger)
    assert statement_intake.learn_accounts(ledger, ledger.document("x")) == []
    ledger.add_document("y", "NORD", "2026-10", "statement", "/x/y.xml", accounts=[X1])  # stored naming a stranger
    assert statement_intake.month_missing(ledger, "NORD", "2026-10") is None


def test_the_pack_takes_a_months_statements_only_with_every_account_of_their_banks(ledger, tmp_path):
    """October's RON alone doesn't ride along with November's pack; with October's EUR both go, and a statement
    that names no account leaves its month waiting."""
    nord = companies.get("NORD")
    ron, eur = nord.ibans["RON"], nord.ibans["EUR"]
    ledger.add_document("oct-ron", "NORD", "2026-10", "statement", "/x/oct-ron.xml", accounts=[ron])
    ledger.add_document("nov", "NORD", "2026-11", "statement", "/x/nov.xml", accounts=[ron, eur])
    assert [d["sha"] for d in pack.pending_docs(ledger, "NORD", "2026-11")] == ["nov"]
    ledger.add_document("oct-eur", "NORD", "2026-10", "statement", "/x/oct-eur.xml", accounts=[eur])
    assert {d["sha"] for d in pack.pending_docs(ledger, "NORD", "2026-11")} == {"oct-ron", "oct-eur", "nov"}
    ledger.add_document("dec", "NORD", "2026-12", "statement", str(tmp_path / "gone.xml"))  # accounts never recorded
    assert statement_intake.month_missing(ledger, "NORD", "2026-12") is None and not pack.ready(
        ledger, "NORD", "2026-12"
    )
    assert "dec" not in {d["sha"] for d in pack.pending_docs(ledger, "NORD", "2027-01")}  # nor rides along later


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
    st = recon.month_statement(ledger.documents(company="NORD", month="2026-10", kind="statement"), "NORD")
    assert len(st.txs) == len(parse(tmp_path, complete()).txs) + 1  # BT's and ING's
    assert {ING, companies.get("NORD").ibans["RON"]} <= {a.iban for a in st.accounts}
    assert recon.month_view(ledger, "NORD", "2026-10", [])["balanced"] is True
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
    october = date(2026, 10, 1), date(2026, 10, 31)  # and their days add up to the month, proven by the balances
    assert all(
        coverage.covered(ledger, "NORD", json.loads(d["accounts"])[0], *october, trusted_only=True) for d in docs
    )
    again = recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")
    assert [r["status"] for r in again] == ["duplicate"]


@pytest.mark.parametrize("firm", ["NORD", "STUDIO9"])
def test_the_operator_imports_a_plain_bt_pdf_under_any_name_and_a_csv_is_refused_on_its_own(ledger, tmp_path, firm):
    """A BT24 download: no password, any name. The CUI in its header names the firm, so no password is read."""
    co = companies.get(firm)
    accounts = [(c, i, Decimal("100.00"), [("05/10/2026", f"SET-{c}", Decimal("10.00"))]) for c, i in co.ibans.items()]
    plain = bt_statement(tmp_path / "statement_5.pdf", co.cui, ("01/10/2026", "31/10/2026"), accounts, password=None)
    wise = tmp_path / "statement_USD.csv"
    wise.write_text("Date,Amount\n2026-01-01,1.00\n")

    def no_password(key):
        raise AssertionError("a plain PDF needs no password")

    bad = bt_statement(tmp_path / "Extras.pdf", co.cui, ("31/02/2026", "31/03/2026"), accounts, password=None)
    amount = [(c, i, o, [(d, "X valoare tranzactie: 1.2.3 RON", a) for d, _, a in m]) for c, i, o, m in accounts]
    worse = bt_statement(tmp_path / "Extras2.pdf", co.cui, ("01/10/2026", "31/10/2026"), amount, password=None)
    csv, date_, amount_, pdf = recon.ingest_files(ledger, [wise, bad, worse, plain], password_lookup=no_password)
    assert (csv["status"], csv["errors"]) == ("refused", [recon._NOT_ACCOUNT_CSV])
    assert date_["status"] == amount_["status"] == "refused"  # an impossible date or amount: that file alone
    assert (pdf["status"], pdf["company"], pdf["month"]) == ("added", firm, "2026-10")
    assert not ledger.payments()  # never read as a payment order


def test_mail_never_imports_an_unprotected_pdf(ledger, sec, tmp_path, monkeypatch):
    """A statement mail proves its sender only by the password its PDF needs: a plain one is nobody's."""
    integrations.set_statement_password(ledger, "NORD", "bt", "synthetic")
    inbox = {"host": "imap.example.test", "username": "u", "password": "x", "from_address": "books@example.com"}
    account = integrations.add(ledger, "mail", "Synthetic inbox", inbox)["id"]
    nord = companies.get("NORD")
    accounts = [(c, i, Decimal("100.00"), []) for c, i in nord.ibans.items()]
    plain = bt_statement(tmp_path / "p.pdf", nord.cui, ("01/10/2026", "31/10/2026"), accounts, password=None)
    assert bring(ledger, "mail", [plain], monkeypatch, account) == "not_ours"
    assert not ledger.documents(kind="statement")


@pytest.mark.parametrize(
    "day,banking",
    [
        (date(2026, 4, 10), False),  # Orthodox Good Friday (Easter 12 April 2026)
        (date(2024, 5, 6), False),  # Orthodox Easter Monday (5 May 2024, not the Western 31 March)
        (date(2024, 6, 24), False),  # Rusalii Monday, Easter + 50
        (date(2025, 1, 6), False),  # Bobotează, a holiday since 2024
        (date(2023, 1, 6), True),  # not yet in 2023
        (date(2017, 4, 14), True),  # Good Friday became a holiday in 2018
        (date(2008, 6, 16), True),  # Rusalii Monday became a holiday in 2009 (Legea 202/2008)
        (date(2009, 6, 8), False),
        (date(2026, 11, 30), False),
        (date(2026, 10, 1), True),
    ],
)
def test_banking_days_follow_romanian_legal_holidays(day, banking):
    assert banking_days.banking_day(day) is banking


def test_a_neighbouring_month_on_record_for_the_account_must_show_where_it_closed(ledger, tmp_path):
    """Review: April is on record for RON and EUR, but its file shows only EUR: May's chain can't be checked."""
    nord = companies.get("NORD")
    eur = [("EUR", nord.ibans["EUR"], Decimal("90.00"), [("01/04/2022", "A-EUR", Decimal("10.00"))])]
    folder = recon.month_dir("NORD", "2022-04") / "extrase"
    folder.mkdir(parents=True)
    april = bt_statement(folder / "april.pdf", nord.cui, ("01/04/2022", "30/04/2022"), eur, password=None)
    ledger.add_document("a" * 64, "NORD", "2022-04", "statement", str(april), accounts=list(nord.ibans.values()))
    ledger.set_meta("statement_stored_sha:" + "a" * 64, recon.sha256(april))
    (result,) = recon.ingest_files(ledger, [month_file(tmp_path, *MAY)], password_lookup=lambda co: "synthetic")
    assert result["status"] == "error" and "does not show this account" in result["errors"][0]


def test_a_neighbouring_month_counts_only_as_its_stored_bytes(ledger, tmp_path):
    """Review: April's stored copy replaced by another file with the same balance proves nothing for May."""
    stored = recon.ingest_files(
        ledger, [month_file(tmp_path, "01/04/2022", "30/04/2022", "90.00")], password_lookup=lambda co: "synthetic"
    )
    assert stored[0]["status"] == "added"
    (doc,) = ledger.documents(kind="statement")
    (tmp_path / "other").mkdir()
    other = month_file(tmp_path / "other", "01/04/2022", "30/04/2022", "90.00", password=None)  # same balances
    Path(doc["path"]).write_bytes(other.read_bytes().replace(b"M-RON", b"X-RON"))
    (result,) = recon.ingest_files(ledger, [month_file(tmp_path, *MAY)], password_lookup=lambda co: "synthetic")
    assert result["status"] == "error" and "restore" in result["errors"][0]


def month_file(folder, lo: str, hi: str, opening: str = "100.00", *first, password="synthetic"):
    """NORD's BT PDF for lo..hi, each section under its own period: `first` sections, then RON and EUR, each opening at
    `opening` and crediting 10.00 on `lo`."""
    nord = companies.get("NORD")
    own = [(c, nord.ibans[c], Decimal(opening), [(lo, f"M-{c}", Decimal("10.00"))], (lo, hi)) for c in ("RON", "EUR")]
    return bt_statement(folder / f"extras-{lo.replace('/', '')}.pdf", nord.cui, (lo, hi), [*first, *own], password)


def usd(opening: str, lo: str = "10/10/2026"):
    return ("USD", USD, Decimal(opening), [(lo, "M-USD", Decimal("5.00"))], (lo, "31/10/2026"))


MAY = ("02/05/2022", "31/05/2022")
OCTOBER = ("01/10/2026", "31/10/2026", "100.00")


@pytest.mark.parametrize(
    "case,files,usd_fields,refusal",
    [
        ("1 May 2022 is a Sunday", [MAY], None, None),
        ("1 December is a holiday", [("02/12/2021", "31/12/2021")], None, None),
        ("2 May 2022 is a banking day", [("03/05/2022", "31/05/2022")], None, "full calendar month"),
        ("chains with April", [("01/04/2022", "30/04/2022", "90.00"), MAY], None, None),
        ("April closes elsewhere", [("01/04/2022", "30/04/2022"), MAY], None, "chain"),
        ("May first, then April", [MAY, ("01/04/2022", "30/04/2022")], None, "chain"),
        ("USD opened on the 10th at zero", [(*OCTOBER, usd("0.00"))], {"opened": "2026-10-10"}, None),
        ("USD opened on the 10th, not at zero", [(*OCTOBER, usd("7.00"))], {"opened": "2026-10-10"}, "full calendar"),
        ("USD opened in October, day unknown", [(*OCTOBER, usd("0.00"))], {"opened": "2026-10"}, "full calendar"),
        ("USD opened on the 8th, a Thursday", [(*OCTOBER, usd("0.00"))], {"opened": "2026-10-08"}, "full calendar"),
        ("USD not marked opened", [(*OCTOBER, usd("0.00"))], {}, "full calendar month"),
        ("USD retired on 1 May, still to come for May", [MAY], {"retired": "2022-05-01"}, None),
    ],
)
def test_a_month_starts_after_the_1st_only_when_no_day_with_movements_is_left_out(
    ledger, tmp_path, monkeypatch, case, files, usd_fields, refusal
):
    """BT prints the month from its first banking day, or from the day an account opened: that is the whole month
    when the days before could hold no movement and the balances open where the previous stored month closed."""
    if usd_fields is not None:
        nord_with(monkeypatch, (USD, "USD", "bt", usd_fields))
    *before, (lo, hi, *rest) = files
    for b in before:  # the previous month, imported first
        assert (
            recon.ingest_files(ledger, [month_file(tmp_path, *b)], password_lookup=lambda co: "synthetic")[0]["status"]
            == "added"
        )
    (result,) = recon.ingest_files(
        ledger, [month_file(tmp_path, lo, hi, *rest)], password_lookup=lambda co: "synthetic"
    )
    if refusal:
        assert result["status"] == "refused" and refusal in result["errors"][0]
        assert len(ledger.documents(kind="statement")) == len(before)
    else:
        assert (result["status"], result["month"]) == ("added", hi[6:] + "-" + hi[3:5])
        assert proven_month(ledger, result["month"])  # from the 1st: the days before held no movement


def proven_month(ledger, month):
    """Every account of the month's stored statements is covered from its 1st to its last day, proven by balances."""
    first = date.fromisoformat(month + "-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    docs = [d for d in ledger.documents(kind="statement") if d["month"] == month]
    ibans = {i for d in docs for i in json.loads(d["accounts"])}
    return bool(ibans) and all(coverage.covered(ledger, "NORD", i, first, last, trusted_only=True) for i in ibans)


def quarter(folder, pages=True):
    """NORD's Q2 2022 in one BT PDF: RON and EUR each month, chained, and USD from the 10th of May, when it opened."""
    nord, sections, balance = companies.get("NORD"), [], Decimal("100.00")
    for month, last in (("04", 30), ("05", 31), ("06", 30)):
        days = (f"01/{month}/2022", f"{last}/{month}/2022")
        sections += [
            (c, nord.ibans[c], balance, [(days[0], f"Q-{c}-{month}", Decimal("10.00"))], days) for c in ("RON", "EUR")
        ]
        lo = "10/05/2022" if month == "05" else days[0]
        if month != "04":
            usd_opening = Decimal(0) if month == "05" else Decimal("5.00")
            sections += [(("USD", USD, usd_opening, [(lo, f"Q-USD-{month}", Decimal("5.00"))], (lo, days[1])))]
        balance += 10
    return bt_statement(folder / "statement_Q2.pdf", nord.cui, ("01/04/2022", "30/06/2022"), sections, pages=pages)


@pytest.mark.parametrize("pages", [True, False], ids=["a month per page", "months on one page"])
def test_a_quarterly_bt_pdf_is_stored_as_its_three_months_or_refused(ledger, sec, tmp_path, monkeypatch, pages):
    """Each month is split out by its pages, read again, and imported as that month's statement, with the accounts
    owed that month (USD opened in May is not owed for April)."""
    nord_with(monkeypatch, (USD, "USD", "bt", {"opened": "2022-05-10"}))
    files = [quarter(tmp_path, pages)]
    results = recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")
    if not pages:
        assert [r["status"] for r in results] == ["refused"] and "several months" in results[0]["errors"][0]
        assert not ledger.documents(kind="statement")
        return
    assert [(r["status"], r["month"], r["transactions"]) for r in results] == [
        ("added", "2022-04", 2),
        ("added", "2022-05", 3),
        ("added", "2022-06", 3),
    ]
    assert sorted(d["month"] for d in ledger.documents(kind="statement")) == ["2022-04", "2022-05", "2022-06"]
    again = recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")
    assert [r["status"] for r in again] == ["duplicate"] * 3
    integrations.set_statement_password(ledger, "NORD", "bt", "synthetic")
    with pytest.raises(upload.Refused) as e:  # the app stores one month per upload
        upload.accept_pdfs(ledger, "NORD", "", [files[0].read_bytes()])
    assert "mai multe luni" in e.value.message


def daily(folder, days, balance="100.00", first=1):
    """NORD's older daily BT PDFs, one per day with movements (`dd/mm/yyyy`), numbered from `first`: RON and EUR each
    gain 10.00 a day, balances chained."""
    nord, files, balance = companies.get("NORD"), [], Decimal(balance)
    for n, day in enumerate(days, start=first):
        own = [(c, nord.ibans[c], balance, [(day, f"D-{c}-{n}", Decimal("10.00"))], (day,), n) for c in nord.ibans]
        files.append(bt_statement(folder / f"daily-{day.replace('/', '')}.pdf", nord.cui, (day, day), own))
        balance += 10
    return files


JANUARY = ["13/01/2020", "20/01/2020", "28/01/2020"]


@pytest.mark.parametrize(
    "case,refusal",
    [
        ("January, then February's first", None),
        ("January alone", "last statement"),
        ("a numbered statement lost", "full calendar month"),
        ("balances that don't chain", "do not chain"),
        ("February's first is not the next number", "last statement"),
        ("January's first follows a December not on record", "first or last statement"),
    ],
)
def test_older_daily_bt_pdfs_make_a_month_only_when_numbers_and_balances_leave_no_statement_out(
    ledger, tmp_path, case, refusal
):
    """BT issued them only for days with movements, numbered one by one: the days between need no statement, but
    every number must be there, the balances must chain, and the month's last one is proven by the next month's first."""
    late = "December" in case
    files = daily(tmp_path, JANUARY, first=2 if late else 1)
    if case == "a numbered statement lost":
        files.pop(1)
    if case == "balances that don't chain":
        files[1:] = daily(tmp_path, JANUARY[1:], "500.00", first=2)
    if case != "January alone":
        number = 5 if "next number" in case or late else 4
        files += daily(tmp_path, ["05/02/2020"], "130.00", first=number)
    january, *february = recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")
    if refusal:
        assert january["status"] == "refused" and refusal in january["errors"][0]
        assert not [d for d in ledger.documents(kind="statement") if d["month"] == "2020-01"]
        return
    assert (january["status"], january["month"], january["transactions"]) == ("added", "2020-01", 6)
    assert proven_month(ledger, "2020-01")  # the days between statements too
    # February's own last statement waits for March's first
    assert february[0]["status"] == "refused" and "last statement" in february[0]["errors"][0]


@pytest.mark.parametrize(
    "case,files",
    [
        ("a new year's next number", [("02/12/2019", 1), ("20/12/2019", 2), ("15/01/2020", 3), ("05/02/2020", 4)]),
        ("a number skipped on consecutive days", [("13/01/2020", 1), ("14/01/2020", 3), ("05/02/2020", 4)]),
    ],
)
def test_only_the_next_number_of_the_same_year_proves_nothing_was_left_out(ledger, tmp_path, case, files):
    """Review: numbering may restart with a year, so January proves nothing about December's last days; and two
    statements on consecutive days must be numbered one after the other."""
    paths = [daily(tmp_path, [day], str(100 + 10 * n), first=number)[0] for n, (day, number) in enumerate(files)]
    first, *_ = recon.ingest_files(ledger, paths, password_lookup=lambda co: "synthetic")
    assert first["status"] == "refused" and ("last statement" in first["errors"][0]) == ("year" in case)
    month = files[0][0][6:] + "-" + files[0][0][3:5]
    assert month not in {d["month"] for d in ledger.documents(kind="statement")}


def test_an_account_twice_in_a_quarterlys_month_refuses_that_month(ledger, tmp_path):
    """Review: two April sections of RON, one unproven (a line outside its period), never crash the import, and never
    store April; May, whole and proven, is imported."""
    ron, april = companies.get("NORD").ibans["RON"], ("01/04/2022", "30/04/2022")
    sections = [("RON", ron, Decimal(100), [(day, day, Decimal(10))], april) for day in ("05/04/2022", "05/05/2022")]
    sections += [("RON", ron, Decimal(110), [], ("01/05/2022", "31/05/2022"))]
    pdf = bt_statement(tmp_path / "q.pdf", "11111111", ("01/04/2022", "31/05/2022"), sections, pages=True)
    results = recon.ingest_files(ledger, [pdf], password_lookup=lambda co: "synthetic")
    assert [(r["status"], r.get("month")) for r in results] == [("refused", None), ("added", "2022-05")]
    assert "appears twice" in results[0]["errors"][0]
    assert [d["month"] for d in ledger.documents(kind="statement")] == ["2022-05"]


def test_a_neighbour_is_what_is_stored_or_what_passes_its_own_checks(ledger, tmp_path):
    """Review: a second April that is refused (April is on record) proves nothing for May; a file without a period
    is refused alone."""
    stored = month_file(tmp_path, "01/04/2022", "30/04/2022", "90.00")  # closes at 100.00
    assert recon.ingest_files(ledger, [stored], password_lookup=lambda co: "synthetic")[0]["status"] == "added"
    (tmp_path / "again").mkdir()
    other = month_file(tmp_path / "again", "01/04/2022", "30/04/2022", "190.00")  # closes at 200.00
    nord = companies.get("NORD")
    dateless = bt_statement(
        tmp_path / "extL_Jun_2022_allmerge.pdf", nord.cui, ("", ""), [("RON", nord.ibans["RON"], Decimal(0), [])]
    )
    results = recon.ingest_files(
        ledger, [other, month_file(tmp_path, *MAY, "200.00"), dateless], password_lookup=lambda co: "synthetic"
    )
    assert [r["status"] for r in results] == ["refused"] * 3
    assert "chain" in results[2]["errors"][0] and "period" in results[0]["errors"][0]


@pytest.mark.parametrize("final,trusted", [("next page", True), ("missing", False)])
def test_a_final_balance_after_a_page_break_still_proves_the_month(ledger, tmp_path, monkeypatch, final, trusted):
    """The final balance alone on the next page, after the footer and the bank's info block, still closes the
    account; a statement without one proves nothing. A statement stored before, read again, records its lines."""
    nord, month = companies.get("NORD"), ("01/10/2026", "31/10/2026")
    moves = [("05/10/2026", "P-1", Decimal("2000.00")), ("12/10/2026", "P-2", Decimal("-1000.00"))]
    sections = [(c, i, Decimal("10000.00"), moves) for c, i in nord.ibans.items()]
    pdf = bt_statement(tmp_path / "oct.pdf", nord.cui, month, sections, final=final)
    st = statements.parse_text(pdf_text(statements.decrypt_to(pdf, tmp_path / "d", lambda co: "synthetic")[2]), "NORD")
    assert [a.trustworthy for a in st.accounts] == [trusted, trusted]
    real_trust = statements._pdf_trust
    monkeypatch.setattr(
        statements, "_pdf_trust", lambda accounts, *a: [setattr(x, "trustworthy", False) for x in accounts]
    )
    assert recon.ingest_files(ledger, [pdf], password_lookup=lambda co: "synthetic")[0]["status"] == "added"
    assert not ledger.db.execute("SELECT 1 FROM statement_txs").fetchone()  # stored by an earlier version: no lines
    monkeypatch.setattr(statements, "_pdf_trust", real_trust)
    assert recon.ingest_files(ledger, [pdf], password_lookup=lambda co: "synthetic")[0]["status"] == "duplicate"
    lines = ledger.db.execute("SELECT COUNT(*) FROM statement_txs").fetchone()[0]
    assert lines == (4 if trusted else 0)
    assert proven_month(ledger, "2026-10") == trusted  # its coverage is the new reading's, once matched again
    # unproven, its lines never reach the matchers: November's automatic payments wait for October
    assert pay.missing_statement(ledger, "NORD", date(2026, 11, 6)) == ("" if trusted else "2026-10")


@pytest.mark.parametrize("shape", ["one file", "a month set"])
def test_a_stored_copy_ingested_again_is_its_own_duplicate(ledger, tmp_path, shape):
    """An encrypted PDF is stored decrypted, under the digest of what arrived: the stored copy read again (after a
    parser fix) is that document, so its lines are recorded again and nothing new is stored."""
    nord = companies.get("NORD")
    if shape == "one file":
        sections = [
            (c, i, Decimal("100.00"), [("05/10/2026", f"S-{c}", Decimal("10.00"))]) for c, i in nord.ibans.items()
        ]
        files = [bt_statement(tmp_path / "oct.pdf", nord.cui, ("01/10/2026", "31/10/2026"), sections)]
    else:
        files = bt_files(tmp_path)
    assert recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")[0]["status"] == "added"
    docs = ledger.documents(kind="statement")
    ledger.db.execute("DELETE FROM statement_txs")  # as a parser that read no line left them
    ledger.db.commit()
    copies = [tmp_path / f"copy-{n}.pdf" for n in range(len(docs))]  # e.g. a backup of the stored copies
    for doc, copy in zip(docs, copies, strict=True):
        copy.write_bytes(Path(doc["path"]).read_bytes())
    (again,) = recon.ingest_files(ledger, copies, password_lookup=lambda co: "synthetic")
    assert again["status"] == "duplicate" and ledger.documents(kind="statement") == docs
    assert ledger.db.execute("SELECT COUNT(*) FROM statement_txs").fetchone()[0] == 2
    # the copy also kept as a receipt, or the stored file no longer the copy: nothing is matched
    ledger.add_document(recon.sha256(copies[0]), "NORD", "2026-10", "receipt", "/x/r.pdf", source="links:1")
    assert recon.ingest_files(ledger, copies[:1], password_lookup=lambda co: "synthetic")[0]["status"] == "refused"
    ledger.db.execute("DELETE FROM documents WHERE kind='receipt'")
    ledger.db.commit()
    Path(docs[0]["path"]).write_bytes(b"%PDF-1.4 changed")
    assert recon.ingest_files(ledger, copies[:1], password_lookup=lambda co: "synthetic")[0]["status"] == "error"


def test_a_stored_copy_is_read_once_under_the_imports_own_home(ledger, tmp_path, monkeypatch):
    """Review: the copy is identified by the bytes that were parsed, not by the file read again later; and an import
    into another data directory finds its stored copies there."""
    home, (ron, eur) = tmp_path / "home", bt_files(tmp_path)
    assert recon.ingest_files(ledger, [ron, eur], home, lambda co: "synthetic")[0]["status"] == "added"
    copies = []
    for n, doc in enumerate(ledger.documents(kind="statement")):
        copies.append(tmp_path / f"copy-{n}.pdf")
        copies[-1].write_bytes(Path(doc["path"]).read_bytes())
    decrypt = statements.decrypt_to

    def then_swapped(path, *a, **kw):  # the source replaced right after it was read
        out = decrypt(path, *a, **kw)
        next(c for c in copies if c.name == Path(path).name).write_bytes(b"%PDF-1.4 another file")
        return out

    monkeypatch.setattr(statements, "decrypt_to", then_swapped)
    assert recon.ingest_files(ledger, copies, home, lambda co: "synthetic")[0]["status"] == "duplicate"
    linked = tmp_path / "linked.pdf"  # a source that became a symlink after it was routed: never followed
    linked.symlink_to(Path(ledger.documents(kind="statement")[0]["path"]))
    (result,) = recon.ingest_statements(ledger, [linked], home, lambda co: "synthetic")
    assert result["status"] == "error" and not ledger.used_txs()


@pytest.mark.parametrize(
    "case,refusal",
    [
        ("a day missing", "full calendar month"),
        ("stored before", "already imported"),
        ("kept as a receipt", "kept as another document"),  # those bytes are a document already: no takeover
        ("stored copy gone", "restore the stored document"),  # imported before, but its file is missing
    ],
)
def test_an_incomplete_or_overlapping_set_stores_nothing(ledger, tmp_path, case, refusal):
    files = bt_files(tmp_path, DAYS[:10] + ((11, 31),))
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
    """BT PDFs in through one of the ways they arrive: "added", or what the person reads for a refusal."""
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
    return upload._say(result["errors"])


@pytest.mark.parametrize("caller", ["cli", "mail", "app"])
def test_every_way_a_bt_pdf_arrives_takes_one_account_at_a_time_and_only_the_firms(
    ledger, sec, tmp_path, monkeypatch, caller
):
    integrations.set_statement_password(ledger, "NORD", "bt", "synthetic")
    inbox = {"host": "imap.example.test", "username": "u", "password": "x", "from_address": "books@example.com"}
    account = integrations.add(ledger, "mail", "Synthetic inbox", inbox)["id"]
    ron, eur = bt_files(tmp_path)
    nord = companies.get("NORD")
    other = [("RON", "RO94BTRLRONCRT0000000002", Decimal("0.00"), [])]  # STUDIO9's account, in a file with NORD's CUI
    foreign = bt_statement(tmp_path / "other.pdf", nord.cui, ("01/10/2026", "31/10/2026"), other)
    assert bring(ledger, caller, [ron, eur, foreign], monkeypatch, account) == upload.OTHER_ACCOUNT
    assert not ledger.documents(kind="statement") and not ledger.db.execute("SELECT 1 FROM statement_txs").fetchone()
    assert bring(ledger, caller, [ron], monkeypatch, account) == "added"  # RON alone: EUR is still to come
    assert statement_intake.month_missing(ledger, "NORD", "2026-10") == ["EUR 0001"]
    assert not pack.ready(ledger, "NORD", "2026-10")
    assert bring(ledger, caller, [eur], monkeypatch, account) == "added"
    assert (
        statement_intake.month_missing(ledger, "NORD", "2026-10") == [] and len(ledger.documents(kind="statement")) == 2
    )


def test_an_account_added_while_a_set_is_checked_is_still_to_come(ledger, tmp_path, monkeypatch):
    """An account added while the set is checked is owed by the month: the set is stored, and says it is missing."""
    files = bt_files(tmp_path)
    place = recon._place

    def added_meanwhile(*args):  # after the first checks, before the transaction: an administrator adds USD at BT
        if not companies.get("NORD").account(USD):
            nord_with(monkeypatch, (USD, "USD", "bt"))
        return place(*args)

    monkeypatch.setattr(recon, "_place", added_meanwhile)
    (result,) = recon.ingest_files(ledger, files, password_lookup=lambda co: "synthetic")
    assert (result["status"], result["missing"]) == ("added", ["USD 0001"])
