"""Settlement reports: identity, every check on a batch (AGENTS.md rule 3), all-or-nothing collection, the CLI."""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from email.message import EmailMessage

import pytest
from test_camt import detail, entry, xml
from test_mt940 import block

from fiscalm import companies
from fiscalm.api import actions, state as api
from fiscalm.bank import camt, statements
from fiscalm.bank.intake import identity
from fiscalm.cli import main
from fiscalm.invoicing import issued
from fiscalm.ledger import Ledger, finance_home
from fiscalm.settlements import engine as settlements
from fiscalm.settlements.engine import _HUMAN_CONFIRMATION
from fiscalm.settlements.reports import ReportError

REPORT = b"Ref,Gross,Fee\nTEST-1,100.00,2.00\nTEST-2,100.00,2.00\n"
SPEC = dict(
    source="fan",
    report_key="report1",
    date="2026-10-10",
    currency="RON",
    payer_name="Synthetic Courier SRL",
    mapping={"reference": "Ref", "gross": "Gross", "fee": "Fee"},
)


def store_bank(L, st):
    """Store a balanced synthetic CAMT as immutable evidence, returning its parsed transaction keys."""
    t = st.txs[0]
    data = xml(
        iban=t.iban,
        entries=entry(detail(str(t.amount), t.ref or "synthetic-credit"), amount=str(t.amount), flow="CRDT"),
        closing=str(Decimal(1000) + t.amount),
    )
    data = (
        data.replace(b"Example Supplier SRL", t.counterparty.encode())
        .replace(b"<Cdtr>", b"<Dbtr>")
        .replace(b"</Cdtr>", b"</Dbtr>")
        .replace(b"CdtrAcct", b"DbtrAcct")
        .replace(b"2026-10-25", t.date.isoformat().encode())
    )
    last = calendar.monthrange(t.date.year, t.date.month)[1]
    data = (
        data.replace(b"2026-10-01", t.date.replace(day=1).isoformat().encode())
        .replace(b"2026-10-31", t.date.replace(day=last).isoformat().encode())
        .replace(b"2026-10-26", t.date.isoformat().encode())
    )
    digest = hashlib.sha256(data).hexdigest()
    path = finance_home() / st.company / "statements" / f"{digest}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    L.add_document(digest, st.company, t.date.strftime("%Y-%m"), "statement", str(path))
    return camt.parse_camt(data, st.company), path


def collect(L, rid, st):
    marker = finance_home() / "state" / "LIVE"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return L.settle_report(
        rid,
        st,
        confirmed_by="synthetic-user",
        expected_tx=statements.tx_key(st.txs[0]),
        _confirmation=_HUMAN_CONFIRMATION,
    )


def setup(L):
    for n in (1, 2):
        L.add_issued(
            dict(
                message_id=f"settlement-{n}",
                company="NORD",
                number=f"TEST-{n}",
                issue_date="2026-10-01",
                currency="RON",
                total="100.00",
                doc_type="invoice",
                status="open",
                raw_path="",
            )
        )
    return settlements.import_report(L, "NORD", REPORT, SPEC)


def statement(**kw):
    args = (
        dict(
            date=date(2026, 10, 12),
            title="Credit",
            description="Payout",
            direction="credit",
            amount=Decimal("196.00"),
            currency="RON",
            iban=companies.get("NORD").ron_iban,
            counterparty="Synthetic Courier SRL",
        )
        | kw
    )
    return statements.Statement("NORD", None, None, [statements.Account(args["iban"], "RON", [statements.Tx(**args)])])


def edit_report(L, **changes):
    """Rewrite the stored report the way an older or tampered row reads, past the import checks."""
    with L.tx() as db:
        data = json.loads(db.execute("SELECT data FROM settlement_reports").fetchone()[0])
        db.execute("UPDATE settlement_reports SET data=?", (json.dumps(data | changes),))


def confirmations(L):
    return L.db.execute("SELECT count(*) FROM settlement_confirmations").fetchone()[0]


@pytest.mark.parametrize("day", [date(2026, 10, 10), date(2026, 10, 24)], ids=["report-day", "14-days-later"])
def test_batch_human_confirmation(ledger, day):
    rid = setup(ledger)
    assert collect(ledger, rid, statement(date=day)) == ["settlement-1", "settlement-2"]
    assert len(ledger.used_txs()) == 1
    assert confirmations(ledger) == 1
    assert ledger.db.execute("SELECT amount FROM settlement_fees").fetchone()[0] == "4.00"
    assert all(not r["collected_from_iban"] for r in ledger.issued_rows())
    assert {r["collected_into_iban"] for r in ledger.issued_rows()} == {
        companies.get("NORD").ron_iban
    }  # where it landed
    with pytest.raises(settlements.SettlementError, match="^report_unavailable$"):
        collect(ledger, rid, statement(date=day))


def _reserve_credit(L):
    with L.tx() as db:
        L._reserve(db, statements.tx_key(statement().txs[0]), "other")


LEDGER_CHANGES = {
    "ambiguous": lambda L: L.add_issued(
        dict(message_id="other", company="NORD", number="TEST-2", doc_type="invoice", status="open", raw_path="")
    ),
    "collected": lambda L: L.mark_issued_collected("settlement-2", "proof", "earlier"),
    "marked": lambda L: L.mark_issued_manual("settlement-2", "paid in cash", "cash"),
    "amount": lambda L: L.db.execute("UPDATE issued SET total='101.00' WHERE message_id='settlement-2'"),
    "other_firm_invoice": lambda L: L.db.execute("UPDATE issued SET company='STUDIO9' WHERE message_id='settlement-2'"),
    "duplicate_line": lambda L: edit_report(L, lines=[dict(reference="TEST-1", gross="100.00", fee="2.00")] * 2),
    "used": _reserve_credit,
    "crash": lambda L: L.db.execute(
        "CREATE TRIGGER fail_fee BEFORE INSERT ON settlement_fees BEGIN SELECT RAISE(ABORT, 'crash'); END"
    ),
}
STATEMENT_CHANGES = {
    "untrusted": lambda st: setattr(st.accounts[0], "trustworthy", False),
    "company": lambda st: setattr(st, "company", "STUDIO9"),
}


@pytest.mark.parametrize(
    "failure,credit,reason",
    [
        ("cent", {"amount": Decimal("196.01")}, "credit_missing_or_ambiguous"),
        ("payer", {"counterparty": "Other SRL"}, "credit_missing_or_ambiguous"),
        ("own_account", lambda: {"counterparty_iban": companies.get("NORD").ron_iban}, "credit_missing_or_ambiguous"),
        ("account", {"iban": "unconfigured-account"}, "credit_missing_or_ambiguous"),
        ("early", {"date": date(2026, 10, 9)}, "credit_missing_or_ambiguous"),
        ("late", {"date": date(2026, 10, 25)}, "credit_missing_or_ambiguous"),
        ("currency", {"currency": "EUR"}, "credit_missing_or_ambiguous"),
        ("debit", {"direction": "debit"}, "credit_missing_or_ambiguous"),
        ("untrusted", {}, "credit_missing_or_ambiguous"),
        ("company", {}, "wrong_company"),
        ("ambiguous", {}, "line_2_invoice_missing_or_ambiguous"),
        ("other_firm_invoice", {}, "line_2_invoice_missing_or_ambiguous"),
        ("collected", {}, "line_2_invoice_not_open_or_amount_differs"),
        ("marked", {}, "line_2_invoice_not_open_or_amount_differs"),
        ("amount", {}, "line_2_invoice_not_open_or_amount_differs"),
        ("duplicate_line", {}, "line_2_invoice_not_open_or_amount_differs"),
        ("used", {}, "credit_used"),
        ("crash", {}, None),
    ],
)
def test_refuses_everything(ledger, failure, credit, reason):
    rid = setup(ledger)
    st = statement(**(credit() if callable(credit) else credit))
    STATEMENT_CHANGES.get(failure, lambda st: None)(st)
    LEDGER_CHANGES.get(failure, lambda L: None)(ledger)
    before = set(ledger.used_txs())
    if reason:
        with pytest.raises(settlements.SettlementError, match=f"^{reason}$"):
            collect(ledger, rid, st)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            collect(ledger, rid, st)
    assert ledger.issued("settlement-1")["status"] == "open"
    assert ledger.used_txs() == before
    assert not ledger.db.execute("SELECT * FROM settlement_fees").fetchall()
    assert not ledger.db.execute("SELECT * FROM settlement_lines").fetchall()


def test_race(ledger):
    rid = setup(ledger)

    def run(_):
        L = Ledger(ledger.path)
        try:
            return collect(L, rid, statement())
        except settlements.SettlementError:
            return []
        finally:
            L.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(map(len, results)) == [0, 2]
    assert ledger.db.execute("SELECT count(*) FROM settlement_lines").fetchone()[0] == 2
    assert len(ledger.used_txs()) == 1 and confirmations(ledger) == 1


def test_a_report_is_one_firm_payer_and_reference(ledger):
    setup(ledger)
    with pytest.raises(sqlite3.IntegrityError):
        settlements.import_report(ledger, "NORD", REPORT, SPEC)
    with pytest.raises(sqlite3.IntegrityError):  # same firm, payer and reference: other bytes or parser change nothing
        settlements.import_report(ledger, "NORD", b"\xef\xbb\xbf" + REPORT, SPEC | {"source": "dpd"})
    settlements.import_report(
        ledger, "NORD", REPORT.replace(b"100.00", b"101.00"), SPEC | {"payer_name": "Another Synthetic Courier SRL"}
    )
    settlements.import_report(ledger, "STUDIO9", REPORT, SPEC)
    assert len(settlements.state(ledger, "NORD")) == 2 and len(settlements.state(ledger, "STUDIO9")) == 1


def test_failed_report_blocks_single_collection(ledger):
    setup(ledger)
    assert issued.match_statement(ledger, statement(amount=Decimal("100.00"))) == []
    assert settlements.claimed_credit(ledger, "NORD", statement().txs[0])


def test_competing_reports_and_reused_credit(ledger):
    rid = setup(ledger)
    row = ledger.db.execute("SELECT * FROM settlement_reports").fetchone()
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO settlement_reports(id,company,source,report_key,digest,raw,data) VALUES(?,?,?,?,?,?,?)",
            ("competing", "NORD", "fan", "other", "other", row["raw"], row["data"]),
        )
    with pytest.raises(settlements.SettlementError, match="^competing_reports$"):
        collect(ledger, rid, statement())
    assert not ledger.used_txs()


@pytest.mark.parametrize(
    "identity,right",
    [
        ({"payer_iban": "synthetic-payer-account"}, {"counterparty_iban": "synthetic-payer-account"}),
        ({"payer_cui": "12345678"}, {"counterparty": "Renamed Payout SRL CUI 12345678"}),
    ],
    ids=["iban", "cui"],
)
def test_payer_iban_never_falls_back_to_name(ledger, identity, right):
    """A configured IBAN or CUI decides the payer: the report's payer name neither suffices nor is needed."""
    rid = setup(ledger)
    edit_report(ledger, **identity)
    with pytest.raises(settlements.SettlementError, match="^credit_missing_or_ambiguous$"):
        collect(ledger, rid, statement())
    assert collect(ledger, rid, statement(**right))


@pytest.mark.parametrize(
    "identity,wrong,right",
    [  # same words, another legal form or another alphabet's letters: another firm
        ({}, {"counterparty": "Synthetic Courier SA"}, "Synthetic Courier S.R.L."),
        ({"payer_name": "Synthetic 北京 SRL"}, {"counterparty": "Synthetic 上海 SRL"}, "SYNTHETIC 北京"),
        ({"payer_name": "Atelier Sa Maison SRL"}, {"counterparty": "Atelier Maison"}, "Atelier Sa Maison"),
        ({"payer_name": "Atelier Sa SRL"}, {"counterparty": "Atelier"}, "Atelier Sa"),
        ({"payer_cui": "12345678"}, {"counterparty": "Other SRL", "description": "CUI 12345678"}, "Other 12345678"),
        ({"payer_cui": "123456789"}, {"counterparty": "Other GmbH DE123456789"}, "Other RO123456789"),
        ({"payer_cui": "12345678"}, {"counterparty": "Other Firm ID 12345678X"}, "Other Firm CUI:12345678"),
    ],
    ids=["legal_form", "alphabet", "inner_word", "form_word", "memo", "foreign_vat", "longer_id"],
)
def test_first_report_needs_its_own_payer(ledger, identity, wrong, right):
    """With no confirmation gate, only the payer itself can bring a batch's credit."""
    rid = setup(ledger)
    edit_report(ledger, **identity)
    with pytest.raises(settlements.SettlementError, match="^credit_missing_or_ambiguous$"):
        collect(ledger, rid, statement(**wrong))
    assert collect(ledger, rid, statement(counterparty=right))


def test_report_that_may_be_the_payers_competes(ledger):
    """A CUI report the credit names only in its memo still competes with a name report for that credit."""
    rid = setup(ledger)
    spec = SPEC | {"report_key": "by-cui", "payer_cui": "12345678"}
    other = settlements.import_report(ledger, "NORD", b"\xef\xbb\xbf" + REPORT, spec)
    st = statement(description="CUI 12345678")
    for report in (rid, other):
        with pytest.raises(settlements.SettlementError):
            collect(ledger, report, st)
    assert not ledger.used_txs() and {r["status"] for r in ledger.issued_rows()} == {"open"}


def test_refused_batch_credit_stays_out_of_single_invoice_matching(ledger):
    """A credit the batch refuses (its CUI only in the memo) is still its payer's: no single invoice takes it."""
    setup(ledger)
    edit_report(ledger, payer_cui="12345678")
    ledger.add_issued(
        dict(
            message_id="courier-bill",
            company="NORD",
            number="TEST-9",
            issue_date="2026-10-01",
            currency="RON",
            total="196.00",
            doc_type="invoice",
            status="open",
            raw_path="",
            client_name="Synthetic Courier SRL",
            client_cui="12345678",
        )
    )
    assert issued.match_statement(ledger, statement(description="CUI 12345678")) == []
    assert {r["status"] for r in ledger.issued_rows()} == {"open"} and not ledger.used_txs()


def test_retired_gate_reason_is_replaced(ledger):
    """A report waiting under the retired payer gate gets today's reason when its credit becomes ambiguous."""
    setup(ledger)
    with ledger.tx() as db:
        db.execute("UPDATE settlement_reports SET error='confirmation_required'")
    store_bank(ledger, statement())
    store_bank(ledger, statement(ref="second"))
    settlements.match_statement(ledger, statement())
    assert settlements.state(ledger, "NORD")[0]["error"] == "credit_missing_or_ambiguous"


def test_single_writer_cannot_take_batch_credit(ledger):
    rid = setup(ledger)
    collect(ledger, rid, statement())
    ledger.add_issued(dict(message_id="third", company="NORD", number="TEST-3", status="open", raw_path=""))
    assert not ledger.mark_issued_collected("third", "statement", statements.tx_key(statement().txs[0]))


def test_specific_error_survives_other_statements(ledger):
    setup(ledger)
    ledger.mark_issued_collected("settlement-2", "earlier", "earlier")
    store_bank(ledger, statement())
    settlements.match_statement(ledger, statement())
    reason = settlements.state(ledger, "NORD")[0]["error"]
    assert reason == "line_2_invoice_not_open_or_amount_differs"
    settlements.match_statement(ledger, statement(amount=Decimal("99.00")))
    assert settlements.state(ledger, "NORD")[0]["error"] == reason


def test_cli_withdraw(ledger):
    """Only a pending report of its own firm is withdrawn; its identity is then free for a corrected import."""
    rid = setup(ledger)
    assert main(["settlement", "withdraw", rid, "--company", "STUDIO9"]) == 1
    assert main(["settlement", "withdraw", rid, "--company", "NORD"]) == 0
    assert main(["settlement", "withdraw", rid, "--company", "NORD"]) == 1
    assert settlements.state(ledger, "NORD") == []
    assert ledger.db.execute("SELECT status FROM settlement_reports WHERE id=?", (rid,)).fetchone()[0] == "withdrawn"
    new = settlements.import_report(ledger, "NORD", REPORT, SPEC)
    with pytest.raises(settlements.SettlementError, match="^report_unavailable$"):
        collect(ledger, rid, statement())
    collect(ledger, new, statement())
    assert not ledger.withdraw_report(new, "NORD")


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("payer_cui", 12345678, "must be strings"),
        ("payer_cui", "12345", "digits only"),
        ("payer_cui", "(12345)", "digits only"),
        ("payer_iban", str.lower, "valid and normalized"),
        ("payer_iban", lambda s: s[:4] + " " + s[4:], "valid and normalized"),
        ("payer_iban", lambda s: s[:2] + "00" + s[4:], "valid and normalized"),
    ],
    ids=["cui-int", "cui-short", "cui-text", "iban-lower", "iban-space", "iban-check"],
)
def test_invalid_payer_refuses_at_import(ledger, field, value, reason):
    value = value(companies.get("NORD").ron_iban) if callable(value) else value
    with pytest.raises(ReportError, match=reason):
        settlements.import_report(ledger, "NORD", REPORT, SPEC | {field: value})


def test_malformed_invoice_becomes_task(ledger):
    setup(ledger)
    with ledger.tx() as db:
        db.execute("UPDATE issued SET issue_date='bad' WHERE message_id='settlement-2'")
    store_bank(ledger, statement())
    assert settlements.match_statement(ledger, statement()) == []
    assert settlements.state(ledger, "NORD")[0]["error"] == "line_2_invalid_invoice"
    assert not ledger.used_txs()


def test_state_exposes_safe_report_task_and_fees(ledger):
    rid = setup(ledger)
    settlements.match_statement(ledger, statement(counterparty="Other SRL"))
    s = api.state(ledger, "NORD", datetime(2026, 10, 15, tzinfo=UTC))
    assert s["settlement_tasks"] == [
        {
            "kind": "settlement_review",
            "id": rid,
            "company": "NORD",
            "reason": "credit_missing_or_ambiguous",
            "line": None,
            "sheet": {"co": "NORD", "set": rid},
            "credit": None,
            "invoice_count": 2,
            "net": "196.00",
            "currency": "RON",
        }
    ]
    assert "raw" not in s["settlements"][0] and "payer_iban" not in s["settlements"][0]
    collect(ledger, rid, statement())
    s = api.state(ledger, "NORD", datetime(2026, 10, 15, tzinfo=UTC))
    assert s["settlement_tasks"] == []
    assert s["settlement_fees"][0]["amount"] == "4.00"


def test_mail_import_and_duplicate(home, ledger):
    spec = SPEC | dict(report_key="mail1", mail_sender="reports@example.invalid", attachment="../report.csv")
    cfg = home / "spec.json"
    cfg.write_text(json.dumps(spec))
    msg = EmailMessage()
    msg["From"] = "reports@example.invalid"
    msg.set_content("Untrusted text")
    msg.add_attachment(b"Ref,Gross,Fee\nTEST-1,100.00,2.00\n", maintype="text", subtype="csv", filename="../report.csv")
    mail = home / "message.eml"
    mail.write_bytes(msg.as_bytes())
    args = ["settlement", "import", str(mail), "--mail", "--company", "NORD", "--spec", str(cfg)]
    assert main(args) == 0
    assert settlements.state(ledger, "NORD")[0]["report_key"] == "mail1"
    assert not (home.parent / "report.csv").exists()
    assert main(args) == 1
    assert main(["settlement", "list", "--company", "NORD"]) == 0
    spec["mail_sender"] = "other@example.invalid"
    cfg.write_text(json.dumps(spec))
    assert main(args) == 1
    assert len(settlements.state(ledger, "NORD")) == 1


def test_malformed_spec_safe_message(home, capsys):
    cfg = home / "bad.json"
    cfg.write_text("[]")
    assert main(["settlement", "import", str(home / "unused"), "--company", "NORD", "--spec", str(cfg)]) == 1
    assert "Specification must be an object" in capsys.readouterr().out


@pytest.mark.parametrize("amount", ["196.00", "196.01"])
def test_stored_mt940_exact_credit_and_missing_next_month(ledger, amount):
    rid = setup(ledger)
    edit_report(ledger, date="2026-10-25")
    value = amount.replace(".", ",")
    data = block(
        closing="C261031RON" + str(Decimal(1000) + Decimal(amount)).replace(".", ","),
        lines=[f":61:2610251025C{value}NTRFNONREF//batch", ":86:?20Payout?32Synthetic Courier SRL"],
    )
    digest = hashlib.sha256(data).hexdigest()
    path = finance_home() / "NORD" / "statements" / "statement.dat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    ledger.add_document(digest, "NORD", "2026-10", "statement", str(path))
    assert settlements.window_months("2026-10-25") == {"2026-10", "2026-11"}
    proof = settlements.bank_evidence(ledger, "NORD")
    if amount == "196.01":
        with pytest.raises(settlements.SettlementError, match="credit_missing_or_ambiguous"):
            collect(ledger, rid, proof)
        assert not ledger.used_txs()
        assert all(r["status"] == "open" for r in ledger.issued_rows())
    else:
        assert settlements.match_statement(ledger, proof) == ["settlement-1", "settlement-2"]
        with pytest.raises(settlements.SettlementError, match="report_unavailable"):
            collect(ledger, rid, proof)
        assert len(ledger.used_txs()) == 1


@pytest.mark.parametrize("metadata", ["valid", "missing", "changed", "escape"])
def test_csv_evidence_spans_months_and_refuses_unsafe_metadata(ledger, tmp_path, metadata):
    rid = setup(ledger)
    account = companies.get("NORD").ron_iban
    mapping = {
        "date": "Date",
        "amount": "Amount",
        "description": "Details",
        "counterparty": "Payer",
        "date_format": "%Y-%m-%d",
        "decimal": ".",
    }
    data = (
        b"Date,Amount,Details,Payer\n2026-09-30,1.00,Other,Other SRL\n2026-10-11,196.00,Payout,Synthetic Courier SRL\n"
    )
    path = finance_home() / "NORD" / "statements" / "partial.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    side = path.with_suffix(".csv.json")
    side.write_text(json.dumps({"iban": account, "mapping": mapping}))
    ledger.add_document(identity(data, (account, mapping)), "NORD", "2026-09", "statement", str(path))
    if metadata == "missing":
        side.write_text("{}")
    elif metadata == "changed":
        side.write_text(json.dumps({"iban": account, "mapping": mapping | {"decimal": ","}}))
    elif metadata == "escape":
        outside = tmp_path / "outside.json"
        outside.write_text(side.read_text())
        side.unlink()
        side.symlink_to(outside)
    if metadata != "valid":
        with pytest.raises(settlements.SettlementError, match="statement_unavailable"):
            settlements.bank_evidence(ledger, "NORD")
        return
    proof = settlements.bank_evidence(ledger, "NORD")
    assert len(proof.settlement_txs) == 1
    assert len(proof.versions) == 1
    assert settlements.verify(ledger, ledger.db.execute("SELECT * FROM settlement_reports").fetchone(), proof)[1] == [
        "settlement-1",
        "settlement-2",
    ]
    store_bank(ledger, statement())
    proof = settlements.bank_evidence(ledger, "NORD")
    assert len(proof.versions) == 2
    with pytest.raises(settlements.SettlementError, match="credit_missing_or_ambiguous"):
        collect(ledger, rid, proof)
    assert not ledger.used_txs()


def test_settlement_reference_and_nearest_credit_are_display_only(ledger):
    rid = setup(ledger)
    payer = "Synthetic Courier SRL " + companies.get("STUDIO9").ron_iban
    tx = statement(amount=Decimal("194.00"), counterparty=payer).txs[0]
    edit_report(ledger, payer_iban=companies.get("STUDIO9").ron_iban)
    tx.counterparty_iban = companies.get("STUDIO9").ron_iban
    ledger.record_statement(statement(amount=tx.amount, counterparty=payer, counterparty_iban=tx.counterparty_iban))
    ledger.record_statement(statement(amount=Decimal("196.00"), currency="EUR"))
    ledger.record_statement(statement(amount=Decimal("196.00"), counterparty="Other SRL"))
    with ledger.tx() as db:
        db.execute("UPDATE settlement_reports SET report_key='u' || substr(digest,1,24)")
    reserved = ledger.used_txs()
    row = settlements.state(ledger, "NORD")[0]
    assert row["reference"] is None
    assert row["candidate"] == {
        "id": statements.tx_key(tx),
        "date": "2026-10-12",
        "amount": "194.00",
        "currency": "RON",
        "payer": statements.masked(payer),
    }
    assert companies.get("STUDIO9").ron_iban not in json.dumps(row)
    assert row["credit"] is None and not row["can_confirm"]
    assert ledger.used_txs() == reserved
    assert ledger.db.execute("SELECT status FROM settlement_reports WHERE id=?", (rid,)).fetchone()[0] == "pending"


@pytest.mark.parametrize("status", ["pending", "reserved", "collected", "other_firm"])
def test_withdraw_report_action_preserves_reserved_credit(ledger, status):
    rid = setup(ledger)
    if status == "collected":
        collect(ledger, rid, statement())
    elif status == "reserved":
        with ledger.tx() as db:
            ledger._reserve(db, statements.tx_key(statement().txs[0]), "other proof")
    before = ledger.used_txs()
    result = actions.act("settlement-withdraw", "STUDIO9" if status == "other_firm" else "NORD", {"id": rid})
    assert result["ok"] == (status in ("pending", "reserved"))
    assert ledger.used_txs() == before
    assert bool(settlements.state(ledger, "NORD")) == (not result["ok"])


@pytest.mark.parametrize(
    "identified,problem,expected",
    [
        (False, "", None),  # a first report from a payer known only by name settles once every check passes
        (True, "", None),
        (True, "amount", "line_2_invoice_not_open_or_amount_differs"),
        (True, "missing_credit", "credit_missing_or_ambiguous"),
    ],
)
def test_withdraw_rechecks_survivor(ledger, identified, problem, expected):
    survivor = setup(ledger)
    payer = "Synthetic Courier SRL CUI 12345678" if identified else SPEC["payer_name"]
    if identified:
        edit_report(ledger, payer_cui="12345678")
    competitor = settlements.import_report(
        ledger,
        "NORD",
        b"\xef\xbb\xbf" + REPORT,
        SPEC | {"report_key": "competitor", "payer_cui": "12345678" if identified else ""},
    )
    proof, path = store_bank(ledger, statement(counterparty=payer))
    settlements.match_statement(ledger, proof)
    assert all(r["error"] == "competing_reports" for r in settlements.state(ledger, "NORD") if r["status"] == "pending")
    if problem == "amount":
        with ledger.tx() as db:
            db.execute("UPDATE issued SET total='101.00' WHERE message_id='settlement-2'")
    elif problem == "missing_credit":
        with ledger.tx() as db:
            db.execute("DELETE FROM documents WHERE path=?", (str(path),))
    assert actions.act("settlement-withdraw", "NORD", {"id": competitor})["ok"]
    row = next(r for r in settlements.state(ledger, "NORD") if r["id"] == survivor)
    assert row["error"] == expected
    assert not row["can_confirm"] and confirmations(ledger) == 0
    assert row["status"] == ("collected" if expected is None else "pending")
    assert len(ledger.used_txs()) == (1 if expected is None else 0)
    assert not actions.act("settlement-withdraw", "NORD", {"id": competitor})["ok"]
    settlements.match_statement(ledger, proof)
    assert len(ledger.used_txs()) == (1 if expected is None else 0)
