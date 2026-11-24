"""The first-three confirmation gate: stored bank evidence, a person's explicit confirmation, web and v1 access."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal

import pikepdf
import pytest
from conftest import go_live
from test_api import (
    _api,  # noqa: F401
    server as server,
)
from test_api_v1 import request
from test_camt import SUPPLIER_IBAN, detail, entry, xml
from test_issued import CLIENT_IBAN
from test_settlements import store_bank

from fiscalm import companies, recon
from fiscalm.api import tokens
from fiscalm.api.v1 import Error
from fiscalm.bank import statements
from fiscalm.invoicing.workspace import authorize
from fiscalm.settlements import engine as confirm, engine as settlements

URL = "/companies/NORD/actions/settlement-confirm"
PAYER_CUI = "Synthetic Courier SRL CUI 12345678"  # how a CUI-identified payer appears on a credit


def batch(L, n, company="NORD", payer="Synthetic Courier SRL", multiplier=1, cui="", iban=""):
    """A one-invoice report and its statement credit; the payer is identified by `cui` or `iban`, else by name only."""
    mid = f"{company}-batch-{n}"
    L.add_issued(
        dict(
            message_id=mid,
            company=company,
            number=mid,
            issue_date="2026-10-01",
            doc_type="invoice",
            currency="RON",
            total=f"{100 * multiplier}.00",
            status="open",
            raw_path="",
        )
    )
    rid = settlements.import_report(
        L,
        company,
        f"Invoice,Gross,Fee\n{mid},{100 * multiplier}.00,{2 * multiplier}.00\n".encode(),
        dict(
            source="fan",
            report_key=f"batch-{n}",
            date="2026-10-10",
            currency="RON",
            payer_name=payer,
            payer_cui=cui,
            payer_iban=iban,
            mapping={"reference": "Invoice", "gross": "Gross", "fee": "Fee"},
        ),
    )
    tx = statements.Tx(
        date(2026, 10, 12),
        "Credit",
        f"Batch {n}",
        "credit",
        Decimal(98) * multiplier,
        "RON",
        companies.get(company).ron_iban,
        ref=f"BATCH-{n}",
        counterparty=f"{payer} CUI {cui}" if cui else payer,
        counterparty_iban=iban,
    )
    st = statements.Statement(company, None, None, [statements.Account(tx.iban, "RON", [tx])])
    return rid, st, {"id": rid, "tx": statements.tx_key(tx), "confirmed": "yes"}


def count(L):
    return L.db.execute("SELECT count(*) FROM settlement_confirmations").fetchone()[0]


def report(L, rid):
    return next(r for r in settlements.state(L, "NORD") if r["id"] == rid)


def pdf_statement(home, closing="98.00", payer="Synthetic Courier SRL"):
    header = "Data Descriere".ljust(63) + "Debit".rjust(5) + "Credit".rjust(16)
    lines = [
        f"CUI:{companies.get('NORD').cui}",  # older BT statements print no space
        "din 01/10/2026 - 31/10/2026",
        "CONT 123 Valuta",
        "RON Cod IBAN: " + companies.get("NORD").ron_iban,
        header,
        (" " * 12 + "SOLD ANTERIOR").ljust(64) + "0.00",
        "12/10/2026 Incasare OP".ljust(79) + "98.00",
        f"Transfer;{payer};" + CLIENT_IBAN + ";REF: pdf-1",
        (" " * 12 + "RULAJ TOTAL CONT").ljust(64) + "0.00" + (" " * 11) + "98.00",
        (" " * 12 + "SOLD FINAL CONT").ljust(64) + closing,
        "CONT 124 Valuta",  # NORD's other BT account, without lines: the statement holds every account
        "EUR Cod IBAN: " + companies.get("NORD").ibans["EUR"],
        header,
        (" " * 12 + "SOLD ANTERIOR").ljust(64) + "0.00",
        (" " * 12 + "RULAJ TOTAL CONT").ljust(64) + "0.00" + (" " * 11) + "0.00",
        (" " * 12 + "SOLD FINAL CONT").ljust(64) + "0.00",
    ]
    path = home / "extL_Oct_2026_allmerge.pdf"
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(
                F1=pdf.make_indirect(
                    pikepdf.Dictionary(
                        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Courier
                    )
                )
            )
        )
        text = "BT /F1 10 Tf 12 TL 40 740 Td " + " ".join("(" + line + ") Tj T*" for line in lines) + " ET"
        page.Contents = pdf.make_stream(text.encode())
        pdf.save(path, encryption=pikepdf.Encryption(owner="synthetic-owner", user="synthetic", R=6))
    return path


def stored_pdf(L, home, where, closing="98.00", pin=True, payer="Synthetic Courier SRL"):
    """A decrypted copy of the PDF statement in the ledger; `pin` records its checksum as a stored copy's."""
    path = home / where
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.open(pdf_statement(home, closing, payer), password="synthetic") as pdf:
        pdf.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    L.add_document(digest, "NORD", "2026-10", "statement", str(path))
    if pin:
        L.set_meta("statement_stored_sha:" + digest, digest)


@pytest.mark.parametrize("identity", [{"cui": "12345678"}, {"iban": SUPPLIER_IBAN}], ids=["cui", "iban"])
def test_first_three_need_confirmation_fourth_auto(ledger, home, identity):
    go_live(home)
    for n in range(1, 5):
        rid, st, body = batch(ledger, n, multiplier=n, **identity)
        st, _ = store_bank(ledger, st)
        body["tx"] = statements.tx_key(st.txs[0])
        result = settlements.match_statement(ledger, st)
        if n <= 3:
            assert result == [] and count(ledger) == n - 1
            r = report(ledger, rid)
            assert r["error"] == "confirmation_required" and r["can_confirm"] and r["auto_after"] == 3
            assert r["credit"]["amount"] == f"{98 * n}.00" and r["invoice_count"] == 1
            assert confirm.perform(ledger, "NORD", body, "user synthetic")["ok"]
            assert count(ledger) == n
            assert not confirm.perform(ledger, "NORD", body, "user synthetic")["ok"]
        else:
            assert result == ["NORD-batch-4"] and count(ledger) == 3
    other, st, _ = batch(ledger, 5, "STUDIO9", **identity)
    store_bank(ledger, st)
    assert settlements.match_statement(ledger, st) == []
    other, st, _ = batch(ledger, 6, payer="Another Synthetic Courier SRL", cui="23456789")
    store_bank(ledger, st)
    assert settlements.match_statement(ledger, st) == []


def test_payer_known_only_by_name_always_waits(ledger, home):
    """Trust is earned by a CUI or IBAN only: a name can be shared or reused, so no count of confirmations settles alone."""
    go_live(home)
    for n in range(1, 6):
        rid, st, body = batch(ledger, n, multiplier=n)
        st, _ = store_bank(ledger, st)
        body["tx"] = statements.tx_key(st.txs[0])
        assert settlements.match_statement(ledger, st) == []
        r = report(ledger, rid)
        assert (r["error"], r["can_confirm"], r["auto_after"]) == ("confirmation_required", True, None)
        assert confirm.perform(ledger, "NORD", body, "user synthetic")["ok"]
    assert count(ledger) == 5


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("wrong_company", None),
        ("stale_tx", None),
        ("ambiguous", "credit_missing_or_ambiguous"),
        ("collected", "line_1_invoice_not_open_or_amount_differs"),
        ("cent", "credit_missing_or_ambiguous"),
        ("extra", None),
        ("no_actor", None),
    ],
)
def test_refusals_never_count(ledger, home, monkeypatch, failure, reason):
    rid, st, body = batch(ledger, 1)
    if failure != "shadow":
        go_live(home)
    if failure == "wrong_company":
        body["co"] = "STUDIO9"
    if failure == "stale_tx":
        body["tx"] = "0" * 20
    if failure == "ambiguous":
        st.accounts[0].txs.append(st.txs[0])
    if failure == "collected":
        ledger.mark_issued_collected("NORD-batch-1", "earlier", "previous")
    if failure == "cent":
        st.txs[0].amount = Decimal("98.01")
    if failure == "extra":
        body["confirmed_by"] = "fake user"
    monkeypatch.setattr(confirm, "bank_evidence", lambda *args, **kwargs: st)
    assert not confirm.perform(ledger, "NORD", body, "" if failure == "no_actor" else "user synthetic")["ok"]
    assert count(ledger) == 0 and not ledger.db.execute("SELECT * FROM settlement_lines").fetchall()
    assert ledger.db.execute("SELECT error FROM settlement_reports").fetchone()[0] == reason


def test_confirmation_reads_immutable_company_bank_evidence(ledger, home):
    rid, _, body = batch(ledger, 1)
    data = xml(entries=entry(detail("98.00"), amount="98.00", flow="CRDT"), closing="1098.00")
    data = (
        data.replace(b"Example Supplier SRL", b"Synthetic Courier SRL")
        .replace(b"<Cdtr>", b"<Dbtr>")
        .replace(b"</Cdtr>", b"</Dbtr>")
        .replace(b"CdtrAcct", b"DbtrAcct")
        .replace(b"2026-10-25", b"2026-10-12")
    )
    path = home / "NORD" / "statements" / "synthetic.xml"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    ledger.add_document(hashlib.sha256(data).hexdigest(), "NORD", "2026-10", "statement", str(path))
    st = confirm.bank_evidence(ledger, "NORD")
    body["tx"] = statements.tx_key(st.txs[0])
    go_live(home)
    assert settlements.match_statement(ledger, st) == []
    assert report(ledger, rid)["credit"]["id"] == body["tx"]
    assert confirm.perform(ledger, "NORD", body, "user synthetic")["ok"] and count(ledger) == 1
    _, _, body = batch(ledger, 2)
    path.write_bytes(data + b" ")
    assert not confirm.perform(ledger, "NORD", body, "user synthetic")["ok"] and count(ledger) == 1


def test_identical_bank_credits_remain_ambiguous(ledger, home):
    rid, st, _ = batch(ledger, 1)
    _, path = store_bank(ledger, st)
    data = path.read_bytes() + b"\n"
    duplicate = path.with_name("duplicate.xml")
    duplicate.write_bytes(data)
    ledger.add_document(hashlib.sha256(data).hexdigest(), "NORD", "2026-10", "statement", str(duplicate))
    assert settlements.match_statement(ledger, st) == []
    assert report(ledger, rid)["error"] == "credit_missing_or_ambiguous"
    assert count(ledger) == 0 and not ledger.used_txs()


def test_crash_rolls_back_confirmation_and_collection(ledger, home):
    rid, st, body = batch(ledger, 1)
    go_live(home)
    ledger.db.execute(
        "CREATE TRIGGER fail_confirm BEFORE INSERT ON settlement_confirmations BEGIN SELECT RAISE(ABORT,'crash'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        ledger.settle_report(
            rid, st, confirmed_by="user synthetic", expected_tx=body["tx"], _confirmation=confirm._HUMAN_CONFIRMATION
        )
    assert count(ledger) == 0 and not ledger.used_txs()
    assert ledger.issued("NORD-batch-1")["status"] == "open"
    assert not ledger.db.execute("SELECT * FROM settlement_lines").fetchall()
    assert not ledger.db.execute("SELECT * FROM settlement_fees").fetchall()


@pytest.mark.parametrize("closing", ["98.00", "99.00"])
def test_pdf_ingest_hashes_stored_copy_and_confirms(ledger, home, closing):
    rid, _, body = batch(ledger, 1)
    result = recon.ingest_statement(ledger, pdf_statement(home, closing), home, password_lookup=lambda co: "synthetic")
    assert result["status"] == "added" and result["balanced"]
    st = confirm.bank_evidence(ledger, "NORD")
    if closing != "98.00":
        assert not st.txs
        return
    assert len(st.txs) == 1 and st.txs[0].amount == Decimal("98.00") and st.txs[0].direction == "credit"
    body["tx"] = statements.tx_key(st.txs[0])
    go_live(home)
    assert settlements.match_statement(ledger, st) == []
    assert confirm.perform(ledger, "NORD", body, "user synthetic")["ok"]
    assert count(ledger) == 1


def test_legacy_pdf_requires_person_to_pin_copy(ledger, home):
    rid, _, body = batch(ledger, 1)
    recon.ingest_statement(ledger, pdf_statement(home), home, password_lookup=lambda co: "synthetic")
    doc = ledger.documents(company="NORD", kind="statement")[0]
    with ledger.tx() as db:
        db.execute("DELETE FROM meta WHERE key=?", ("statement_stored_sha:" + doc["sha"],))
    st = confirm.bank_evidence(ledger, "NORD")
    body["tx"] = statements.tx_key(st.txs[0])
    assert st.legacy_proofs
    go_live(home)
    assert settlements.match_statement(ledger, st) == []
    assert report(ledger, rid)["error"] == "confirmation_required"
    assert confirm.perform(ledger, "NORD", body, "user synthetic")["ok"]
    assert ledger.get_meta("statement_stored_sha:" + doc["sha"])
    assert not confirm.bank_evidence(ledger, "NORD", months={"2026-10"}).legacy_proofs


def test_unrelated_old_bad_statement_does_not_block_batch(ledger, home):
    rid, st, body = batch(ledger, 1)
    store_bank(ledger, st)
    ledger.add_document("old-missing", "NORD", "2024-01", "statement", str(home / "NORD" / "missing.pdf"))
    assert settlements.match_statement(ledger, st) == []
    assert report(ledger, rid)["error"] == "confirmation_required"


def test_stale_preview_is_removed(ledger, home):
    rid, st, body = batch(ledger, 1)
    go_live(home)
    _, path = store_bank(ledger, st)
    settlements.match_statement(ledger, st)
    assert report(ledger, rid)["can_confirm"]
    path.unlink()
    settlements.match_statement(ledger, st)
    r = report(ledger, rid)
    assert r["error"] == "statement_unavailable" and r["credit"] is None and not r["can_confirm"]


def test_actor_string_alone_cannot_bypass_human_adapter(ledger, home):
    rid, st, body = batch(ledger, 1)
    go_live(home)
    with pytest.raises(settlements.SettlementError, match="^confirmation_refused$"):
        ledger.settle_report(rid, st, confirmed_by="automated caller", expected_tx=body["tx"])
    assert count(ledger) == 0 and not ledger.used_txs()


def test_bank_snapshot_changed_before_commit_refuses(ledger, home):
    rid, st, _ = batch(ledger, 1)
    go_live(home)
    proof = confirm.BankEvidence(
        st.company, st.period_start, st.period_end, st.accounts, {}, frozenset({"2026-10"}), ()
    )
    ledger.add_document("new-statement", "NORD", "2026-10", "statement", str(home / "NORD" / "new.xml"))
    with pytest.raises(settlements.SettlementError, match="^statement_changed$"):
        ledger.settle_report(rid, proof)
    assert count(ledger) == 0 and not ledger.used_txs()


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("outside_company_dir", "statement_unavailable"),
        ("unbalanced", "credit_missing_or_ambiguous"),
        ("legacy", "statement_confirmation_required"),
        ("changed", "statement_unavailable"),
        ("missing", "credit_missing_or_ambiguous"),
    ],
)
def test_trained_payer_never_uses_unverified_current_statement(ledger, home, failure, reason):
    go_live(home)
    for n in range(1, 4):
        _, st, body = batch(
            ledger, n, multiplier=n + 1, cui="12345678"
        )  # trained: identified, so only the statement guards refuse
        st, _ = store_bank(ledger, st)
        body["tx"] = statements.tx_key(st.txs[0])
        assert confirm.perform(ledger, "NORD", body, "synthetic user")["ok"]
    rid, current, _ = batch(ledger, 4, cui="12345678")  # 98.00, the PDF statement's credit
    if failure == "outside_company_dir":  # balanced and pinned: only its place outside NORD's directory refuses it
        stored_pdf(ledger, home, "outside.pdf", payer=PAYER_CUI)
    elif failure in ("unbalanced", "legacy"):
        stored_pdf(
            ledger,
            home,
            "NORD/statements/current.pdf",
            closing="99.00" if failure == "unbalanced" else "98.00",
            pin=failure != "legacy",
            payer=PAYER_CUI,
        )
    elif failure == "changed":
        _, path = store_bank(ledger, current)
        path.write_bytes(path.read_bytes() + b" ")
    assert settlements.match_statement(ledger, current) == []
    assert ledger.issued("NORD-batch-4")["status"] == "open" and count(ledger) == 3
    assert report(ledger, rid)["error"] == reason


def test_stale_request_does_not_discard_verified_preview(ledger, home):
    go_live(home)
    rid, st, body = batch(ledger, 1)
    st, _ = store_bank(ledger, st)
    settlements.match_statement(ledger, st)
    assert not confirm.perform(ledger, "NORD", body | {"tx": "0" * 20}, "synthetic user")["ok"]
    r = report(ledger, rid)
    assert r["can_confirm"] and count(ledger) == 0


def test_automatic_writer_requires_stored_evidence(ledger):
    rid, st, _ = batch(ledger, 1)
    with pytest.raises(settlements.SettlementError, match="^statement_unavailable$"):
        ledger.settle_report(rid, st)
    assert count(ledger) == 0 and not ledger.used_txs()


def person_key(L, name="Writer"):
    return tokens.create(L, name, "write", created_by="u_admin", user_id="u_admin")["token"]


def test_web_and_v1_authorization(token_owner, ledger, home, server, monkeypatch):
    rid, st, body = batch(ledger, 1)
    monkeypatch.setattr(confirm, "bank_evidence", lambda L, co, current=None, **kwargs: current or st)
    reader = tokens.create(ledger, "Reader", "read", user_id=token_owner)["token"]
    script = tokens.create(ledger, "Unattributed script", "write", user_id=token_owner)["token"]
    writer = person_key(ledger)
    assert request(server, reader, URL, "POST", body)[0] == 403
    ledger.db.execute("UPDATE api_tokens SET user_id=NULL WHERE name='Unattributed script'")
    ledger.db.commit()
    assert request(server, script, URL, "POST", body)[0] == 401
    assert _api(server, "settlement-confirm", body | {"co": "NORD"}, Origin="https://evil.example")[0] == 403
    assert request(server, writer, "/companies/STUDIO9/actions/settlement-confirm", "POST", body)[0] == 409
    assert request(server, writer, URL, "POST", {k: v for k, v in body.items() if k != "confirmed"})[0] == 409
    assert count(ledger) == 0
    assert request(server, writer, URL, "POST", body)[0] == 200
    assert count(ledger) == 1
    rid, st, body = batch(ledger, 2)
    assert _api(server, "settlement-confirm", body | {"co": "NORD"})[1]["ok"]
    assert count(ledger) == 2
    rid, st, body = batch(ledger, 3)
    made = tokens.verify(ledger, writer)

    def revoked():
        tokens.revoke(ledger, made["id"])
        authorize(ledger, made)

    with pytest.raises(Error):
        confirm.perform(ledger, "NORD", body, "user synthetic", revoked)
    assert count(ledger) == 2


@pytest.mark.parametrize("revoked", [False, True])
def test_app_user_revoked_during_bank_read_cannot_confirm(ledger, home, server, monkeypatch, revoked):
    """A non-admin app user confirms; one removed while the bank evidence is read cannot."""
    go_live(home)
    _, st, body = batch(ledger, 1)

    def evidence(*args, **kwargs):
        if revoked:
            with closing(sqlite3.connect(ledger.path)) as db, db:
                db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='u_member'")
        return st

    monkeypatch.setattr(confirm, "bank_evidence", evidence)
    assert _api(server, "settlement-confirm", body | {"co": "NORD"}, user="unknown")[0] == 401
    status, answer = _api(server, "settlement-confirm", body | {"co": "NORD"}, user="member")
    assert (status, count(ledger)) == ((403, 0) if revoked else (200, 1))
    assert revoked or answer["ok"]


def test_removed_api_key_owner_cannot_confirm(ledger, home, server, monkeypatch):
    go_live(home)
    _, st, body = batch(ledger, 1)
    writer = person_key(ledger, "Person client")

    def remove(*args, **kwargs):
        with closing(sqlite3.connect(ledger.path)) as db, db:
            db.execute("UPDATE users SET disabled_at='2026-11-04' WHERE id='u_admin'")
        return st

    monkeypatch.setattr(confirm, "bank_evidence", remove)
    assert request(server, writer, URL, "POST", body)[0] == 401
    assert count(ledger) == 0
