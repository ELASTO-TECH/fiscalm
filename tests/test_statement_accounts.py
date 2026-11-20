"""Statements and their transactions belong to company accounts, so several banks can be read side by side."""

import dataclasses
import json

import pytest
from firms import set_firm
from test_camt import complete, fake_pdf, parse, write, xml

from fiscalm import companies, pack, recon
from fiscalm.bank import intake as statement_intake, statements

ING = "RO00INGB0000000000000009"
X1, X2, Y = "RO00XXXX0000000000000001", "RO00XXXX0000000000000002", "RO00YYYY0000000000000002"


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
    ],
)
def test_a_statement_needs_every_account_at_its_bank_and_none_elsewhere(monkeypatch, tmp_path, accounts, data, missing):
    nord_with(monkeypatch, *accounts)
    assert statement_intake.missing_accounts(parse(tmp_path, data)) == missing


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
