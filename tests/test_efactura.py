"""e-Factura: synthetic UBL only (fake CUIs and IBANs from synthetic ledger fixtures)."""

import io
import json
import sys
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import anaf_fake
import pytest
from firms import iban as synthetic_iban
from payment_cases import download_batch
from synthetic_world import ubl_lines, ubl_totals

from fiscalm import companies, invoice_lines, recon
from fiscalm.api import lists, state as api
from fiscalm.bank import statements
from fiscalm.efactura import client as anaf, inbox as efactura
from fiscalm.ledger import Ledger
from fiscalm.payments import engine as pay
from fiscalm.payments.orders import PaymentFileError

NORD_CUI = "11111111"


SUPPLIER_IBAN = synthetic_iban("RO00INGB0000999900001111")
OTHER_IBAN = synthetic_iban("RO00BTRL0000999900002222")


def ubl(
    number="RMBU 2222602",
    total="250.00",
    due="2026-11-05",
    iban=SUPPLIER_IBAN,
    buyer=NORD_CUI,
    supplier="CENTRUL MEDICAL EXEMPLU SRL",
    supplier_cui="RO99999999",
    root="Invoice",
    currency="RON",
    issue="2026-10-20",
    extra_pm="",
) -> bytes:
    ns = f"urn:oasis:names:specification:ubl:schema:xsd:{root}-2"
    pm = (
        (
            f"<cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode><cac:PayeeFinancialAccount>"
            f"<cbc:ID>{iban}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"
        )
        if iban
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="{ns}" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>{number}</cbc:ID><cbc:IssueDate>{issue}</cbc:IssueDate>{f"<cbc:DueDate>{due}</cbc:DueDate>" if due else ""}
 <cbc:DocumentCurrencyCode>{currency}</cbc:DocumentCurrencyCode>
 <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme><cbc:CompanyID>{supplier_cui}</cbc:CompanyID>
  </cac:PartyTaxScheme><cac:PartyLegalEntity><cbc:RegistrationName>{supplier}</cbc:RegistrationName>
  </cac:PartyLegalEntity></cac:Party></cac:AccountingSupplierParty>
 <cac:AccountingCustomerParty><cac:Party><cac:PartyLegalEntity><cbc:RegistrationName>Atelier Nord SRL
  </cbc:RegistrationName><cbc:CompanyID>{buyer}</cbc:CompanyID></cac:PartyLegalEntity></cac:Party>
 </cac:AccountingCustomerParty>{pm}{extra_pm}
 <cac:LegalMonetaryTotal><cbc:TaxInclusiveAmount currencyID="{currency}">{total}</cbc:TaxInclusiveAmount>
  <cbc:PayableAmount currencyID="{currency}">{total}</cbc:PayableAmount></cac:LegalMonetaryTotal>
</{root}>""".encode()


def anaf_zip(xml: bytes, mid="3001") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{mid}.xml", xml)
        z.writestr(f"semnatura_{mid}.xml", b"<Signature/>")
    return buf.getvalue()


def msg(mid, listed="202610211200"):
    return {"id": mid, "id_solicitare": f"5{mid}", "data_creare": listed, "tip": "FACTURA PRIMITA"}


# ------------------------------------------------------------------ parsing


def test_parse_invoice_fields():
    inv = efactura.parse_ubl(ubl())
    assert (inv.doc_type, inv.number, inv.issue_date, inv.due_date, inv.total, inv.currency) == (
        "invoice",
        "RMBU 2222602",
        "2026-10-20",
        "2026-11-05",
        "250.00",
        "RON",
    )
    assert inv.supplier_cui == "99999999" and inv.buyer_cui == NORD_CUI
    assert inv.supplier_iban == SUPPLIER_IBAN and inv.issues == []


@pytest.mark.parametrize("total", ["1E+99999", "NaN", "Infinity", "9" * 129])
def test_ubl_amount_cannot_expand_without_bound(total):
    with pytest.raises(efactura.UblError):
        efactura.parse_ubl(ubl(total=total))


def test_parse_flags_bad_iban_and_reads_payment_due_date():
    pm = (
        "<cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode><cbc:PaymentDueDate>2026-12-01"
        "</cbc:PaymentDueDate><cac:PayeeFinancialAccount><cbc:ID>RO00XXXX1</cbc:ID></cac:PayeeFinancialAccount>"
        "</cac:PaymentMeans>"
    )
    inv = efactura.parse_ubl(ubl(due="", iban="", extra_pm=pm))
    assert inv.due_date == "2026-12-01" and inv.ibans == [] and "not a valid IBAN" in inv.issues[0]


def test_parse_messy_payee_accounts_seen_in_real_invoices():
    b = synthetic_iban("RO00BRDE0000999900003333")

    def pm(acct):
        return (
            "<cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode><cac:PayeeFinancialAccount>"  # noqa: E731
            f"<cbc:ID>{acct}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"
        )

    inv = efactura.parse_ubl(
        ubl(iban="", extra_pm=pm(f"{SUPPLIER_IBAN},{b}") + pm(f"{SUPPLIER_IBAN}LEI") + pm(f"{b}\n "))
    )
    assert inv.ibans == [SUPPLIER_IBAN, b] and inv.issues == []


@pytest.mark.parametrize(
    "evil",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><Invoice>&e;</Invoice>',
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><Invoice>&b;</Invoice>',
    ],
)
def test_xml_with_dtd_is_refused(evil):
    with pytest.raises(efactura.UblError, match="DTD"):
        efactura.parse_ubl(evil)


def test_non_utf8_and_foreign_roots_refused():
    with pytest.raises(efactura.UblError, match="UTF-8"):
        efactura.parse_ubl('<?xml version="1.0" encoding="UTF-16"?><a/>'.encode("utf-16"))
    with pytest.raises(efactura.UblError, match="not a UBL"):
        efactura.parse_ubl(b"<Order/>")


def test_zip_must_hold_one_invoice_and_no_paths():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.xml", ubl())
        z.writestr("semnatura_1.xml", b"<S/>")
    with pytest.raises(efactura.UblError, match="found 0"):
        efactura.invoice_from_zip(buf.getvalue())
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(3):
            z.writestr(f"{i}.xml", b" " * (9 * 1024 * 1024))  # small compressed, big inflated
    with pytest.raises(efactura.UblError, match="found 3"):
        efactura.invoice_from_zip(many.getvalue())
    bad = bytearray(anaf_zip(ubl()))
    i = bad.index(b"Invoice")
    bad[i : i + 7] = b"Xnvoice"  # stored member corrupted: CRC error on read
    with pytest.raises(efactura.UblError, match="unreadable"):
        efactura.invoice_from_zip(bytes(bad))
    inv, sha = efactura.invoice_from_zip(anaf_zip(ubl()))
    assert inv.number == "RMBU 2222602" and len(sha) == 64


# ------------------------------------------------------------------ sync and ledger


def _connected(**kw):
    L = Ledger()
    try:
        return anaf_fake.connect(L, **kw)
    finally:
        L.close()


def _fake_anaf(monkeypatch, messages, blobs, downloads=None):
    _connected()
    monkeypatch.setattr(anaf, "list_received", lambda cif, s, e, t: messages.get(cif, []))

    def dl(mid, tok):
        if downloads is not None:
            downloads.append(mid)
        return blobs[mid]

    monkeypatch.setattr(anaf, "download", dl)


def test_sync_stores_raw_zip_and_dedupes_on_message_id(ledger, home, sec, monkeypatch):
    downloads = []
    blobs = {
        "3001": anaf_zip(ubl()),
        "3002": anaf_zip(ubl(root="CreditNote", number="CN 1")),
        "3003": anaf_zip(ubl()),
        "3004": b"PK\x03\x04garbage",
    }
    _fake_anaf(monkeypatch, {NORD_CUI: [msg(m) for m in blobs]}, blobs, downloads)
    res = efactura.sync(ledger)
    assert not res["errors"] and len(res["new"]) == 4
    rows = {r["message_id"]: r for r in ledger.efactura_rows()}
    assert rows["3001"]["status"] == "open" and rows["3001"]["supplier_iban"] == SUPPLIER_IBAN
    assert rows["3002"]["status"] == "credit"
    assert rows["3003"]["status"] == "duplicate"  # same invoice re-sent under a new message id
    assert rows["3004"]["parse_error"]
    assert (home / "efactura" / "NORD" / "2026-10" / "3001.zip").read_bytes() == blobs["3001"]
    efactura.sync(ledger)
    assert sorted(downloads) == ["3001", "3002", "3003", "3004"]  # never downloaded twice
    assert [r["message_id"] for r in efactura.unpaid(ledger)] == ["3001"]
    assert [r["message_id"] for r in efactura.unreadable(ledger)] == ["3004"]


def test_sync_is_quiet_before_authorization(ledger, home, sec):
    res = efactura.sync(ledger)
    assert res.get("not_authorized") and not res["errors"] and not ledger.get_meta("efactura_last_error")


def test_accountant_invoices_use_the_supplier_payment_path(ledger, home, sec, monkeypatch):
    blobs = {"3010": anaf_zip(ubl(supplier="ACCOUNTANT SRL", supplier_cui="RO55555555", number="AA 4669"))}
    _fake_anaf(monkeypatch, {NORD_CUI: [msg("3010")]}, blobs)
    efactura.sync(ledger)
    assert ledger.efactura("3010")["status"] == "open"
    assert len(efactura.unpaid(ledger)) == 1


# ------------------------------------------------------------------ matching


def _tx(amount, d, counterparty="", iban="", desc="", currency="RON", orig=None):
    t = statements.Tx(
        date=d,
        title="Plata OP",
        description=desc or f"Plata;{counterparty};{iban};REF: R{amount}",
        direction="debit",
        amount=Decimal(amount),
        currency=currency,
        iban="RO24BTRLRONCRT0000000001",
        counterparty=counterparty,
        counterparty_iban=iban,
        ref=f"R{amount}",
    )
    if orig:
        t.orig_amount, t.orig_currency = Decimal(orig[0]), orig[1]
    return t


def _stored(ledger, monkeypatch, **invoices):
    blobs = {mid: anaf_zip(x) for mid, x in invoices.items()}
    _fake_anaf(monkeypatch, {NORD_CUI: [msg(m) for m in blobs]}, blobs)
    efactura.sync(ledger)


def test_transfer_to_supplier_iban_marks_paid_once(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    st = statements.Statement(
        "NORD",
        None,
        None,
        [
            statements.Account(
                "x",
                "RON",
                [
                    _tx(
                        "250.00",
                        date(2026, 10, 30),
                        "CENTRUL MEDICAL",
                        SUPPLIER_IBAN,
                        desc="plata factura RMBU 2222603",
                    )
                ],
            )
        ],
    )
    assert efactura.match_statement(ledger, st) == ["3002"]  # the transfer names this invoice
    assert efactura.match_statement(ledger, st) == []  # the same debit never pays a second invoice
    assert ledger.efactura("3001")["status"] == "open"


def test_contradictory_iban_never_matches_even_with_a_name_word(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    t = _tx("250.00", date(2026, 10, 30), "MEDICAL OTHER SRL", OTHER_IBAN, desc="MEDICAL EXEMPLU plata")
    card = _tx("250.00", date(2026, 10, 30), desc="Plata la POS MEDICAL OTHER")  # one shared word is not enough
    st = _debit_st(t, card)
    assert efactura.match_statement(ledger, st) == []
    exact = _tx("250.00", date(2026, 10, 30), "S.C. CENTRUL MEDICAL EXEMPLU S.R.L.", OTHER_IBAN, desc="plata")
    st.accounts[0].txs.append(exact)  # another account, but the transfer names exactly this firm
    assert efactura.match_statement(ledger, st) == ["3001"]


def test_ambiguous_or_named_elsewhere_debits_settle_nothing(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(),
            "3002": ubl(number="RMBU 2222603", issue="2026-10-21"),
            "3003": ubl(number="RMBU 2222604", issue="2026-10-22"),
        },
    )
    ledger.mark_efactura_paid("3003", "Owner", None)
    plain = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii")
    closed = _tx("250.00", date(2026, 10, 31), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata RMBU 2222604")
    st = _debit_st(plain, closed)
    assert efactura.match_statement(ledger, st) == []  # two open invoices fit; the other names a paid one
    assert {r["status"] for r in efactura.unpaid(ledger)} == {"open"} and len(efactura.unpaid(ledger)) == 2


def _debit_st(*txs):
    return statements.Statement("NORD", None, None, [statements.Account("x", "RON", list(txs))])


def test_user_mark_waits_until_a_matching_debit_confirms_it(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    assert ledger.mark_efactura_paid("3001", "user: already paid (page)")
    assert [r["message_id"] for r in efactura.awaiting(ledger)] == ["3001"]
    other = _tx("249.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata")  # another amount
    assert efactura.match_statement(ledger, _debit_st(other)) == []
    assert efactura.awaiting(ledger) and ledger.efactura("3001")["paid_tx"] is None
    t = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata")
    assert efactura.match_statement(ledger, _debit_st(t)) == ["3001"]
    r = ledger.efactura("3001")
    assert (
        r["status"] == "paid"
        and r["paid_tx"]
        and r["paid_note"].startswith("statement ")
        and not efactura.awaiting(ledger)
    )
    assert efactura.reopen(ledger, "3001") is None  # proved by the statement: no undo


def test_user_mark_never_takes_the_debit_of_an_open_invoice(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    ledger.mark_efactura_paid("3001", "user: already paid (page)")
    plain = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii")
    assert efactura.match_statement(ledger, _debit_st(plain)) == []  # fits both: settles neither
    assert ledger.efactura("3002")["status"] == "open" and efactura.awaiting(ledger)
    named = _tx("250.00", date(2026, 10, 31), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata RMBU 2222603")
    assert efactura.match_statement(ledger, _debit_st(named)) == ["3002"]  # the one it names
    assert [r["message_id"] for r in efactura.awaiting(ledger)] == ["3001"]
    # with 3002 paid by its named debit, the plain one fits the user's mark alone: it confirms it
    assert efactura.match_statement(ledger, _debit_st(plain, named)) == ["3001"]


def test_a_mail_proof_is_confirmed_by_its_later_debit_once(ledger, home, sec, monkeypatch):
    import threading

    from fiscalm.ledger import Ledger

    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    efactura.judged(ledger, "3001", ["imap:proof"])
    assert efactura.confirm_by_mail(ledger, "3001", "imap:proof", "mail: plata confirmata")
    plain = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii")
    assert efactura.match_statement(ledger, _debit_st(plain)) == []  # fits the proved one and the open one
    assert ledger.efactura("3001")["paid_tx"] == "ef-proof:imap:proof" and ledger.ambiguous("NORD")
    assert ledger.mark_efactura_skipped("3002", "nu e de plata")
    assert not ledger.mark_efactura_paid("3001", "statement", "0" * 20)  # a debit on no record never replaces a mail
    twins = [plain, _tx("250.00", date(2026, 10, 31), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata 2")]
    ledger.record_statement(_debit_st(*twins))
    gate, won = threading.Barrier(2), []

    def caller(t):  # two connections, each sure its debit is the invoice's
        L = Ledger()
        gate.wait()
        won.append(L.mark_efactura_paid("3001", f"statement {t.date}", statements.tx_key(t), to_iban=SUPPLIER_IBAN))
        L.close()

    threads = [threading.Thread(target=caller, args=(t,)) for t in twins]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(won) == [False, True]
    paid_tx = ledger.efactura("3001")["paid_tx"]
    assert paid_tx in {statements.tx_key(t) for t in twins}
    assert {"ef-proof:imap:proof", paid_tx} <= ledger.used_txs()  # the mail still proves nothing else
    assert ledger.paid_proofs("NORD")["efactura 3001"] == paid_tx
    assert efactura.match_statement(ledger, _debit_st(*twins)) == [] and ledger.efactura("3001")["paid_tx"] == paid_tx
    assert not efactura.confirm_by_mail(ledger, "3002", "imap:proof", "mail: plata confirmata")


def test_a_reopen_during_matching_never_hides_a_rival(ledger, home, sec, monkeypatch):
    """The matcher read open invoices, another connection reopened marked B, then the matcher read marked ones: B was in
    neither and the debit settled mail-proved A alone. It reads every candidate under the write lock now."""
    import threading

    from fiscalm.ledger import Ledger

    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    efactura.judged(ledger, "3001", ["imap:proof"])
    assert efactura.confirm_by_mail(ledger, "3001", "imap:proof", "mail: plata confirmata")
    assert ledger.mark_efactura_paid("3002", "user: already paid (page)")
    other = threading.Thread(target=lambda: efactura.reopen(Ledger(), "3002"))
    marked = efactura._marked

    def reopen_then_read(L, company=None):  # between rivals' read of the open rows and of the marked ones
        if sys._getframe(1).f_code.co_name == "_open_invoices" and not other.ident:
            other.start()
            other.join(timeout=1)  # blocked by the matcher's lock: it stops waiting and the matcher goes on
        return marked(L, company)

    monkeypatch.setattr(efactura, "_marked", reopen_then_read)
    t = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii")
    assert efactura.match_statement(ledger, _debit_st(t)) == []
    other.join()
    assert ledger.efactura("3002")["status"] == "open" and statements.tx_key(t) not in ledger.used_txs()


@pytest.mark.parametrize("case", ["credited", "withdrawn", "unreadable twin"])
def test_a_mail_proved_invoice_takes_its_debit_like_an_open_one(ledger, home, sec, monkeypatch, case):
    credit = ubl(number="COR 2", total="-50.00").replace(
        b"<cbc:DocumentCurrencyCode>",
        b"<cbc:Note>Storno factura seria TEST nr. 0001 din data 20/10/2026</cbc:Note><cbc:DocumentCurrencyCode>",
    )
    _stored(ledger, monkeypatch, **{"3001": ubl(number="TEST 1"), **({"3002": credit} if case == "credited" else {})})
    if case == "withdrawn":  # its payment-file row stays, skipped: the debit is still the invoice's
        efactura.approve(ledger, "3001", origin="person")
        assert efactura.withdraw(ledger, "3001", "user: already paid (page)") is None
    if case == "unreadable twin":  # a mail proved an invoice fiscalm can't read: no amount, never a rival
        ledger.db.execute(
            "INSERT INTO efactura(message_id,company,raw_path,status,created_at,parse_error,paid_tx) "
            "VALUES('3009','NORD','x.zip','paid','2026-10-20','synthetic','ef-proof:imap:other')"
        )
        ledger.db.execute("INSERT INTO settled_tx VALUES('ef-proof:imap:other','efactura 3009','2026-10-21')")
        ledger.db.commit()
        efactura.judged(ledger, "3009", ["imap:other"])  # a proof as valid as any: only unreadable
    proved = "3001"
    if case == "credited":  # 3001's rest (200, netted once) is a rival of a mail-proved 200 invoice, marked or not
        _stored(ledger, monkeypatch, **{"3003": ubl(number="TEST 3", total="200.00")})
        proved = "3003"
    efactura.judged(ledger, proved, ["imap:proof"])
    assert efactura.confirm_by_mail(ledger, proved, "imap:proof", "mail: plata confirmata")
    if case == "credited":
        t = _tx("200.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii")
        assert efactura.rivals(ledger, "NORD")(t) == {("partial", "3001"), ("efactura", "3003")}
        assert ledger.mark_efactura_paid("3001", "user: platita")
        assert efactura.rivals(ledger, "NORD")(t) == {("partial", "3001"), ("efactura", "3003")}
        assert efactura.match_statement(ledger, _debit_st(t)) == [] and statements.tx_key(t) not in ledger.used_txs()
        return
    t = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii")
    assert efactura.match_statement(ledger, _debit_st(t)) == ["3001"]
    assert ledger.efactura("3001")["paid_tx"] == statements.tx_key(t)


def test_withdrawn_invoice_is_confirmed_by_its_debit(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    item_id = efactura.approve(ledger, "3001", origin="person")["item_id"]
    assert efactura.withdraw(ledger, "3001", "user: already paid (page)") is None
    assert ledger.payment(item_id)["status"] == "skipped" and efactura.awaiting(ledger)
    t = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata")
    assert efactura.match_statement(ledger, _debit_st(t)) == ["3001"] and not efactura.awaiting(ledger)


def test_debit_fitting_a_user_mark_and_an_approved_invoice_settles_neither(ledger, home, sec, monkeypatch):
    # The invoice matcher once deferred to the payments path, which then paid the approved one

    _stored(ledger, monkeypatch, **{"2999": ubl(number="RMBU 0", issue="2026-09-01")})
    assert ledger.mark_efactura_paid(
        "2999", "statement: supplier account", "supplier-account-proof", to_iban=SUPPLIER_IBAN
    )
    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    ledger.mark_efactura_paid("3001", "user: already paid (page)")
    item_id = efactura.approve(ledger, "3002", origin="person")["item_id"]
    st = _debit_st(_tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii"))
    for _ in range(2):  # every sync matches the statement again, in this order
        assert recon.auto_mark_paid(ledger, st) == [] and efactura.match_statement(ledger, st) == []
    assert ledger.payment(item_id)["status"] == "pending" and efactura.awaiting(ledger)
    st.accounts[0].txs.append(
        _tx("250.00", date(2026, 10, 31), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata RMBU 2222603")
    )
    assert recon.auto_mark_paid(ledger, st) == [item_id]  # the named one pays its invoice
    # the plain one stays unproved: a debit to an approved invoice's IBAN and amount is the payments path's
    assert efactura.match_statement(ledger, st) == [] and efactura.awaiting(ledger)


def test_withdrawn_mark_and_an_open_twin_share_no_debit_and_replays_settle_nothing(ledger, home, sec, monkeypatch):

    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    efactura.approve(ledger, "3001", origin="person")
    assert efactura.withdraw(ledger, "3001", "user: already paid (page)") is None
    plain = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii")
    st = _debit_st(plain)
    assert recon.auto_mark_paid(ledger, st) == [] and efactura.match_statement(ledger, st) == []
    assert ledger.efactura("3002")["status"] == "open" and efactura.awaiting(ledger)
    st.accounts[0].txs.append(
        _tx("250.00", date(2026, 10, 31), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata RMBU 2222602")
    )
    assert efactura.match_statement(ledger, st) == [
        "3001",
        "3002",
    ]  # named confirms the mark, the plain one fits 3002 alone
    for _ in range(2):
        assert recon.auto_mark_paid(ledger, st) == [] and efactura.match_statement(ledger, st) == []
    assert len(ledger.used_txs()) == 2


def test_one_debit_settles_one_thing_even_when_the_key_is_reused(ledger, home, sec, monkeypatch):
    # A reserved tx key once still updated another row; INSERT OR IGNORE kept only the first
    from fiscalm.ledger import Ledger

    _stored(ledger, monkeypatch, **{"2999": ubl(number="RMBU 0", issue="2026-09-01")})
    assert ledger.mark_efactura_paid(
        "2999", "statement: supplier account", "supplier-account-proof", to_iban=SUPPLIER_IBAN
    )
    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(),
            "3002": ubl(number="RMBU 2222603", issue="2026-10-21"),
            "3003": ubl(number="RMBU 2222604", issue="2026-10-22"),
        },
    )
    item_id = efactura.approve(ledger, "3003", origin="person")["item_id"]
    assert ledger.mark_efactura_paid("3001", "statement x", "k1")
    assert not ledger.mark_efactura_paid("3002", "statement x", "k1") and ledger.efactura("3002")["status"] == "open"
    assert not ledger.mark_paid(item_id, "statement x", "k1") and ledger.payment(item_id)["status"] == "pending"
    assert ledger.mark_paid(item_id, "statement y", "k2")
    assert not ledger.mark_efactura_paid("3002", "statement y", "k2") and ledger.efactura("3002")["status"] == "open"
    assert not ledger.mark_efactura_paid("3001", "statement z", "k3")  # already paid: k3 stays free
    assert "k3" not in ledger.used_txs() and ledger.mark_efactura_paid("3002", "statement z", "k3")
    # two callers with their own connection, both sure the key is still free
    import threading

    _stored(ledger, monkeypatch, **{"3004": ubl(number="RMBU 2222605"), "3005": ubl(number="RMBU 2222606")})
    gate, won = threading.Barrier(2), []

    def caller(mid):
        L = Ledger()
        gate.wait()
        won.append(L.mark_efactura_paid(mid, "statement w", "k4"))
        L.close()

    threads = [threading.Thread(target=caller, args=(m,)) for m in ("3004", "3005")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(won) == [False, True]
    assert sorted(ledger.efactura(m)["status"] for m in ("3004", "3005")) == ["open", "paid"]


def test_cli_mark_paid_without_a_mail_waits_for_confirmation(ledger, home, sec, monkeypatch):
    from fiscalm import cli

    _stored(ledger, monkeypatch, **{"3001": ubl()})
    assert cli.main(["efactura", "mark-paid", "3001", "--note", "platit din aplicatie"]) == 0
    assert ledger.efactura("3001")["paid_note"] == "user: platit din aplicatie" and efactura.awaiting(ledger)


def test_debit_of_an_approved_invoice_is_left_to_the_payments_path(ledger, home, sec, monkeypatch):

    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    item_id = efactura.approve(ledger, "3001", origin="person")["item_id"]
    t = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata RMBU 2222602")
    st = _debit_st(t)
    for _ in range(2):  # the statement is matched again on every sync
        assert recon.auto_mark_paid(ledger, st) in ([item_id], [])
        assert efactura.match_statement(ledger, st) == []
    assert ledger.payment(item_id)["status"] == "paid" and ledger.efactura("3002")["status"] == "open"


def test_debit_that_paid_an_invoice_never_pays_an_approved_one(ledger, home, sec, monkeypatch):

    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    t = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata RMBU 2222602")
    st = _debit_st(t)
    assert efactura.match_statement(ledger, st) == ["3001"]
    item_id = efactura.approve(ledger, "3002", origin="person")["item_id"]
    assert recon.auto_mark_paid(ledger, st) == [] and ledger.payment(item_id)["status"] == "pending"


def test_two_approved_twins_are_not_paid_by_one_debit_even_on_reprocessing(ledger, home, sec, monkeypatch):

    _stored(ledger, monkeypatch, **{"2999": ubl(number="RMBU 0", issue="2026-09-01")})
    assert ledger.mark_efactura_paid(
        "2999", "statement: supplier account", "supplier-account-proof", to_iban=SUPPLIER_IBAN
    )
    _stored(ledger, monkeypatch, **{"3001": ubl(), "3002": ubl(number="RMBU 2222603", issue="2026-10-21")})
    ids = [efactura.approve(ledger, m, origin="person")["item_id"] for m in ("3001", "3002")]
    t = _tx("250.00", date(2026, 10, 30), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata servicii")
    st = _debit_st(t)
    assert recon.auto_mark_paid(ledger, st) == [] and recon.auto_mark_paid(ledger, st) == []  # ambiguous
    named = _tx("250.00", date(2026, 10, 31), "CENTRUL MEDICAL", SUPPLIER_IBAN, desc="plata RMBU 2222603")
    st.accounts[0].txs.append(named)
    # The named debit pays its invoice; then the plain one fits a single invoice and pays it.
    assert sorted(recon.auto_mark_paid(ledger, st)) == sorted(ids)
    assert recon.auto_mark_paid(ledger, st) == []
    assert ledger.payment(ids[1])["paid_note"].startswith("statement 2026-10-31")


def _known_name(ledger, t, mid: str) -> None:
    """The user answered once that this statement name is the invoice's supplier (efactura.resolve_debit remembers it)."""
    name = efactura.line_keys(t, companies.get("NORD").own_ibans)["name"]
    partner = efactura.supplier_partner("NORD", ledger.efactura(mid))
    ledger.remember("NORD", "name", name, {"kind": "documents", "partner": partner, "reason": None, "note": None}, None)


def test_card_payment_found_by_name_settles_only_once_its_name_is_remembered(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(supplier="ZEP ELECTRONIC COMPONENTS SRL", total="99.90"),
            "3002": ubl(supplier="ZEP ELECTRONIC COMPONENTS SRL", total="45.00", number="Z2"),
        },
    )
    other = _tx("99.91", date(2026, 10, 21), desc="Plata la POS ZEP ELECTRONIC LODZ")
    far = _tx("99.90", date(2026, 9, 1), desc="Plata la POS ZEP ELECTRONIC LODZ")  # long before the invoice
    st = _debit_st(other, far)
    assert efactura.match_statement(ledger, st) == []
    card = _tx(
        "99.90",
        date(2026, 10, 18),
        counterparty="ZEP ELECTRONIC LODZ",
        desc="Plata la POS non-BT ZEP ELECTRONIC LODZ POL 12345678 valoare",
    )
    st.accounts[0].txs.append(card)
    assert efactura.match_statement(ledger, st) == []  # a name alone proves nothing yet: it waits for the user
    assert [a["tx_key"] for a in ledger.ambiguous("NORD")] == [efactura.tx_key(card)]
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    assert efactura.resolve_debit(ledger, "NORD", efactura.tx_key(card), "3001") == ("efactura", "3001")
    assert [m["key_kind"] for m in ledger.memories("NORD")] == ["name"]  # the answer is remembered
    st.accounts[0].txs.append(
        _tx(
            "45.00",
            date(2026, 10, 19),
            counterparty="ZEP ELECTRONIC LODZ",
            desc="Plata la POS non-BT ZEP ELECTRONIC LODZ",
        )
    )
    assert efactura.match_statement(ledger, st) == ["3002"]
    assert "name" in ledger.efactura("3002")["paid_note"]
    abroad = _tx("12.00", date(2026, 10, 25), counterparty="ZEP ELECTRONIC LODZ", desc="Plata la POS non-BT ZEP")
    recon.classify(ledger, "NORD", [abroad])
    assert abroad.category == statements.DOMESTIC  # Factura vine prin e-Factura: no receipt is asked for


def test_a_card_debit_never_counts_for_a_bon_paid_invoice_by_a_name_alone(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="ALPHA ELECTRONIC SRL", total="99.90")})
    ledger.db.execute("UPDATE efactura SET status='paid', paid_tx='bon:sha-bon' WHERE message_id='3001'")
    ledger.db.commit()
    card = _tx("99.90", date(2026, 10, 21), counterparty="BETA ELECTRONIC", desc="Plata la POS BETA ELECTRONIC")
    efactura.match_statement(ledger, _debit_st(card))
    assert efactura.tx_key(card) not in ledger.used_txs()  # left free for its own invoice, and asked about


def test_short_card_descriptors_match_but_only_near_the_invoice_date(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(supplier="LUMEN AMBIENT SRL", total="486.00", issue="2026-09-04"),
            "3002": ubl(
                supplier="VALEA STORE S.R.L.",
                total="931.90",
                issue="2026-09-08",
                number="MN1",
                supplier_cui="RO77777777",
            ),
        },
    )
    late = _tx("931.90", date(2026, 9, 25), desc="Plata la POS non-BT cu card VISA EPOS PayU*valea.example BUCURESTI")
    st = _debit_st(late)
    assert efactura.match_statement(ledger, st) == []  # 17 days later: not proof enough without an IBAN
    st.accounts[0].txs += [
        _tx("486.00", date(2026, 9, 4), desc="Plata la POS EPOS MID 600000000001RON NTP*lumen RO"),
        _tx("931.90", date(2026, 9, 9), desc="Plata la POS non-BT cu card VISA EPOS PayU*valea.example"),
    ]
    assert efactura.match_statement(ledger, st) == []  # each fits by its short descriptor: both wait for the user
    assert len(ledger.ambiguous("NORD")) == 3  # and the late one too, by its amount alone


@pytest.mark.parametrize(
    "invoices,debit,offered",
    [
        (
            {"3001": "56.27"},
            _tx("56.27", date(2026, 10, 20), desc="Plata la POS EPOS SHOPX7K2"),
            ["3001"],
        ),  # card, same day
        ({"3001": "11.22"}, _tx("11.22", date(2026, 11, 4), desc="Plata clienti speciali VDF123"), ["3001"]),  # D+15
        (
            {"3001": "11.22", "3002": "11.22"},
            _tx("11.22", date(2026, 10, 21), desc="Debit direct VDF123"),
            ["3001", "3002"],
        ),
        ({"3001": "11.22"}, _tx("11.22", date(2026, 12, 20), desc="Debit direct VDF123"), []),  # two months later
        ({"3001": "11.22"}, _tx("11.22", date(2026, 10, 21), desc="Plata BUGETUL DE STAT"), []),  # a tax: never
    ],
)
def test_a_debit_fitting_invoices_by_amount_alone_waits_for_a_person(
    ledger, home, sec, monkeypatch, invoices, debit, offered
):
    """Nothing in the bank's text names the supplier: no matcher settles it; the invoices of exactly its amount are
    offered as a Plată de lămurit, and the person's pick settles the one chosen."""
    _stored(ledger, monkeypatch, **{m: ubl(total=v, number=f"N{m}", issue="2026-10-20") for m, v in invoices.items()})
    assert efactura.match_statement(ledger, _debit_st(debit)) == [] and debit_key(debit) not in ledger.used_txs()
    recorded = ledger.ambiguous("NORD")
    assert [sorted(c[1] for c in json.loads(a["candidates"])) for a in recorded] == ([offered] if offered else [])
    if offered:
        assert efactura.resolve_debit(ledger, "NORD", debit_key(debit), offered[0]) == ("efactura", offered[0])
        assert ledger.efactura(offered[0])["paid_tx"] == debit_key(debit) and not ledger.ambiguous("NORD")


debit_key = efactura.tx_key


def test_an_amount_alone_never_overrides_a_named_invoice_nor_teaches_a_name(ledger, home, sec, monkeypatch):
    """Review: a debit naming another (paid) invoice, or moving money between the firm's own accounts, is offered
    nothing; a pick made on the amount alone remembers no name or account for the supplier; bank text shown masked."""
    numbers = {"3001": "N3001", "3002": "N3002", "3003": "INV-2026-0042", "3004": "123-456"}
    _stored(ledger, monkeypatch, **{m: ubl(total="11.22", number=n, issue="2026-10-20") for m, n in numbers.items()})
    ledger.db.execute("UPDATE efactura SET status='paid' WHERE message_id IN ('3002', '3003', '3004')")
    ledger.db.commit()
    named = _tx("11.22", date(2026, 10, 21), desc="Plata factura N3002 VDF123")
    dashed = _tx("11.22", date(2026, 10, 21), desc="Plata factura INV-2026-0042")
    digits = _tx("11.22", date(2026, 10, 21), desc="Plata factura 123-456")
    own = _tx("11.22", date(2026, 10, 21), iban=companies.get("NORD").ibans["EUR"], desc="Transfer intern")
    card = _tx("11.22", date(2026, 10, 22), "PAYPROC*XYZ", desc="Plata la POS PAYPROC*XYZ RO49AAAA1B31007593840000")
    assert efactura.match_statement(ledger, _debit_st(named, dashed, digits, own, card)) == []
    assert [a["tx_key"] for a in ledger.ambiguous("NORD")] == [debit_key(card)]
    (shown,) = api.ambiguous(ledger, "NORD")
    assert "RO49AAAA1B31007593840000" not in shown["description"]
    assert efactura.resolve_debit(ledger, "NORD", debit_key(card), "3001") == ("efactura", "3001")
    assert ledger.memories("NORD") == []
    # a date in a debit's text is never read as an invoice number (the matchers' rule is digits as written)
    assert efactura._named({"20261021"}, _tx("1.00", date(2026, 10, 21), desc="Plata din 2026-10-21")) == set()


def test_eur_invoice_matches_original_amount(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(currency="EUR", total="20.00", supplier="FURNIZOR EXEMPLU SRL")})
    t = _tx(
        "101.50",
        date(2026, 10, 22),
        counterparty="FURNIZOR EXEMPLU",
        desc="Plata la POS non-BT FURNIZOR EXEMPLU",
        orig=("20.00", "EUR"),
    )
    _known_name(ledger, t, "3001")
    st = _debit_st(t)
    assert efactura.match_statement(ledger, st) == ["3001"]


# ------------------------------------------------------------------ approve -> BT Go


def test_approve_builds_payment_from_xml_only(ledger, home, sec, monkeypatch):
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()

    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(),
            "3002": ubl(number="X 2", iban=""),
            "3003": ubl(number="X 3", iban=OTHER_IBAN, issue="2026-10-22"),
        },
    )
    res = efactura.approve(ledger, "3001", origin="person")
    p = ledger.payment(res["item_id"])
    assert (p["kind"], p["payee_iban"], p["amount"], p["doc_ref"]) == (
        "efactura",
        SUPPLIER_IBAN,
        "250.00",
        "RMBU 2222602",
    )
    assert ledger.efactura("3001")["status"] == "linked" and efactura.unpaid(ledger)[0]["status"] == "linked"
    with pytest.raises(efactura.ApproveError, match="not an open"):
        efactura.approve(ledger, "3001", origin="person")
    with pytest.raises(efactura.ApproveError, match="IBAN"):
        efactura.approve(ledger, "3002", origin="person")
    assert efactura.payment_item(ledger, "3003").payee_iban == OTHER_IBAN
    two = (
        "<cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode><cac:PayeeFinancialAccount>"
        f"<cbc:ID>{OTHER_IBAN}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"
    )
    _stored(ledger, monkeypatch, **{"3004": ubl(number="X 4", supplier_cui="RO88888888", extra_pm=two)})
    with pytest.raises(efactura.ApproveError, match="several IBANs"):
        efactura.approve(ledger, "3004", origin="person")
    ident, _ = download_batch(ledger, [res["item_id"]], today=date.today())
    with pay.named_path(ledger.payment_file(ident)).open() as stream:
        assert SUPPLIER_IBAN in stream.read()
    ledger.mark_paid(res["item_id"], "statement")
    assert [r["message_id"] for r in efactura.unpaid(ledger)] == ["3002", "3004", "3003"]


# ------------------------------------------------------------------ tokens


def test_access_token_refreshes_before_expiry(ledger, sec, monkeypatch):
    t0 = datetime(2026, 11, 1, 8, 0)
    iid = anaf_fake.connect(ledger, access=anaf_fake.jwt(t0 + timedelta(days=90)), refresh="r1", now=t0)
    st = anaf.token_state(ledger, t0)
    assert st["authorized"] and st["refresh_days_left"] == 365 and "r1" not in json.dumps(st)
    (token_file,) = sec.glob(f"int-{iid}-oauth-*")
    assert token_file.stat().st_mode & 0o777 == 0o600
    fake = anaf_fake.TokenEndpoint({"access_token": anaf_fake.jwt(t0 + timedelta(days=170)), "refresh_token": "r2"})
    monkeypatch.setattr(anaf, "_post", fake)
    assert anaf.access_token(t0 + timedelta(days=10), L=ledger) == anaf_fake.tokens(ledger)["access_token"]
    assert not fake.forms
    anaf.access_token(t0 + timedelta(days=80), L=ledger)  # 10 days left: refresh
    assert fake.forms == [{"grant_type": "refresh_token", "refresh_token": "r1", "token_content_type": "jwt"}]
    tok = anaf_fake.tokens(ledger)
    assert tok["refresh_token"] == "r2" and tok["refresh_expires_at"].startswith("2027-11-01")  # not extended
    assert [p.name for p in sec.glob(f"int-{iid}-oauth-*")] != [token_file.name]  # the old file is gone
    assert len(list(sec.glob(f"int-{iid}-oauth-*"))) == 1
    with pytest.raises(anaf.AuthMissing):
        anaf.access_token(t0 + timedelta(days=366), L=ledger)


def test_download_rejects_json_errors(monkeypatch):
    monkeypatch.setattr(anaf, "_get", lambda *a: (200, b'{"eroare":"S-au facut deja 10 descarcari"}', "json"))
    with pytest.raises(anaf.AnafError, match="10 descarcari"):
        anaf.download("3001", "tok")
    with pytest.raises(anaf.AnafError, match="numeric"):
        anaf.download("1&x=2", "tok")


def test_list_walks_pages_and_treats_no_messages_as_empty(monkeypatch):
    pages = {
        1: {"mesaje": [msg("1"), {"id": "2", "tip": "FACTURA TRIMISA"}], "numar_total_pagini": 2},
        2: {"mesaje": [msg("3")], "numar_total_pagini": 2},
    }
    monkeypatch.setattr(anaf, "_get", lambda path, p, t: (200, json.dumps(pages[p["pagina"]]).encode(), ""))
    now = datetime(2026, 10, 26)
    assert [m["id"] for m in anaf.list_received("1", now - timedelta(days=59), now, "t")] == ["1", "3"]
    monkeypatch.setattr(anaf, "_get", lambda *a: (200, b'{"eroare":"Nu exista mesaje in intervalul selectat"}', ""))
    assert anaf.list_received("1", now, now, "t") == []
    monkeypatch.setattr(anaf, "_get", lambda *a: (200, b'{"eroare":"Nu aveti drept in SPV pentru CIF=1"}', ""))
    with pytest.raises(anaf.AnafError, match="drept"):
        anaf.list_received("1", now, now, "t")


# ------------------------------------------------------------------ nudges and page


def test_fiscalm_shows_unpaid_efactura(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    inv = {i["id"]: i for i in api.state(ledger, "NORD")["invoices"]}["3001"]
    assert (
        inv["supplier"] == "CENTRUL MEDICAL EXEMPLU SRL" and inv["due_date"].endswith("-11-05") and inv["can_approve"]
    )  # named-file preparation is local in Sandbox
    (home / "state" / "LIVE").touch()
    assert {i["id"]: i for i in api.state(ledger, "NORD")["invoices"]}["3001"]["can_approve"]


# ------------------------------------------------------------------ card payments: evidence for the thread


def _mail(sender, subject, text, d=date(2026, 10, 21)):
    return {"key": f"nm:{sender}|{subject}", "sender": sender, "subject": subject, "text": text, "date": d}


def test_evidence_lists_supplier_and_processor_mails_until_judged(ledger, home, sec, monkeypatch):
    from fiscalm import cli

    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(supplier="LUMEN AMBIENT SRL", total="486.00", number="F-LUM 30550"),
            "3002": ubl(supplier="FURNIZOR LINISTIT SRL", total="10.00", number="X1", supplier_cui="RO5"),
        },
    )
    inbox = [
        _mail("Lumen Ambient <shop@lumen.example>", "Comanda ta", "Plata primita. Total 486,00 lei"),
        _mail("NETOPIA Payments <no@netopia-payments.com>", "Plata inregistrata", "Lumen Ambient 486,00"),
        _mail("Lumen Ambient <news@lumen.example>", "Reduceri de toamna", "promo"),  # no amount, no number
        _mail("Newsletter <x@other.example>", "Lumen", "promo"),
        _mail("owner@example.com", "Fw: Lumen", "forwarded 486,00"),
    ]
    read = lambda q: inbox  # noqa: E731
    ev = efactura.evidence(ledger, read=read)
    assert [e["message_id"] for e in ev] == ["3001"]  # nothing for the supplier without mails
    assert [m["from"].split()[0] for m in ev[0]["mails"]] == ["Lumen", "NETOPIA"]
    efactura.judged(ledger, "3001", [inbox[1]["key"]])
    assert [m["from"].split()[0] for m in efactura.evidence(ledger, read=read)[0]["mails"]] == ["Lumen"]
    monkeypatch.setattr(efactura, "evidence", lambda L, read=None: [])
    assert cli.main(["efactura", "mark-paid", "3001", "--mail", inbox[0]["key"], "--note", "mail: plata primita"]) == 0
    assert ledger.efactura("3001")["status"] == "paid"


def test_evidence_survives_a_busy_mail_mirror(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})

    def boom(q):
        raise RuntimeError("mail server busy")

    assert efactura.evidence(ledger, read=boom) == []


@pytest.mark.parametrize("marked", [False, True])
def test_forged_supplier_mail_is_only_a_suggestion(ledger, home, sec, monkeypatch, marked):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="LUMEN AMBIENT SRL", total="486.00")})
    if marked:
        ledger.mark_efactura_paid("3001", "user: paid")
    before = dict(ledger.efactura("3001"))
    mail = _mail("Lumen Ambient <attacker@example.invalid>", "Payment received", "Total 486,00 lei")
    answer = {"decisions": [{"message_id": "3001", "paid": True, "mail_key": mail["key"], "reason": "paid"}]}
    result = efactura.judge(ledger, ask=lambda p: answer, read=lambda q: [mail])
    assert dict(ledger.efactura("3001")) == before
    assert result["suggestions"][0]["message_id"] == "3001"
    assert not ledger.used_txs()


def test_a_confirmation_racing_an_undo_is_never_reopened(ledger, home, sec, monkeypatch):
    from fiscalm.ledger import Ledger

    _stored(ledger, monkeypatch, **{"3001": ubl(supplier="LUMEN AMBIENT SRL", total="486.00", number="F-LUM 30550")})
    ledger.mark_efactura_paid("3001", "user: already paid (page)")
    read = ledger.efactura

    def stale(mid):  # reopen reads the mark, then a statement confirms it from another connection
        r = read(mid)
        other = Ledger()
        assert other.mark_efactura_paid(mid, "statement 2026-10-20 REF 1", "tx-proof")
        other.close()
        return r

    monkeypatch.setattr(ledger, "efactura", stale)
    assert efactura.reopen(ledger, "3001") is None
    monkeypatch.undo()
    r = ledger.efactura("3001")
    assert r["status"] == "paid" and r["paid_tx"] == "tx-proof"


def test_marketplace_order_mail_naming_the_seller_is_evidence(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{"3001": ubl(supplier="Mobilier Exemplu SRL", total="203.98", number="RIC064256/2026", issue="2026-09-06")},
    )
    order = _mail(
        '"eMAG.ro" <comenzi@emag.ro>',
        "Confirmare înregistrare comandă #500328194",
        "Plata cu cardul a fost acceptată. Produse vândute de Mobilier Exemplu: Kit de montare 1 buc 203,98 LEI",
        d=date(2026, 8, 18),
    )  # 19 days before the invoice
    ev = efactura.evidence(ledger, read=lambda q: [order])
    assert ev and ev[0]["mails"][0]["subject"].startswith("Confirmare")


def test_autopay_supplier_invoices_go_into_bt_go_by_themselves(ledger, home, sec, monkeypatch):

    _stored(ledger, monkeypatch, **{"3001": ubl(issue="2026-09-15", number="RMBU 1"), "3002": ubl(number="RMBU 2")})
    ledger.mark_efactura_paid("3001", "statement", "supplier-account-proof", to_iban=SUPPLIER_IBAN)
    efactura.set_autopay(ledger, "3001")
    res = efactura.autopay_new(ledger)
    assert [r.get("message_id") for r in res if r.get("auto")] == ["3002"]
    assert ledger.efactura("3002")["status"] == "linked"
    # A new invoice of the same supplier with another IBAN is refused once, and stays open.
    _stored(ledger, monkeypatch, **{"3003": ubl(number="RMBU 3", iban=OTHER_IBAN, issue="2026-10-25")})  # sync tries
    assert ledger.efactura("3003")["status"] == "linked"


def test_only_automatic_payments_wait_for_last_months_statement(ledger, home, sec, live, monkeypatch):
    """A person who adds an invoice knows whether they paid it; the autopay rule waits for last month's statement."""
    from fiscalm.api import tasks

    ledger.db.execute(
        "INSERT INTO bank_connections(id,company,bank,provider,accounts,valid_until,created_at) "
        "VALUES ('bc-test','NORD','bt','enablebanking','[]','2099-12-31','2026-10-01')"
    )  # a firm that receives statements, none for last month
    ledger.db.commit()
    invoices = {"3001": ubl(issue="2026-09-15", number="RMBU 1"), "3002": ubl(number="RMBU 2")}
    _stored(ledger, monkeypatch, **invoices, **{"3003": ubl(number="RMBU 3", issue="2026-10-25")})
    ledger.mark_efactura_paid("3001", "statement", "supplier-account-proof", to_iban=SUPPLIER_IBAN)
    manual = efactura.approve(ledger, "3002", origin="person")["item_id"]
    download_batch(ledger, [manual], today=date.today())
    efactura.set_autopay(ledger, "3001")
    efactura.autopay_new(ledger)  # queues 3003 and builds the files
    rows = [ledger.payment(ledger.efactura(m)["item_id"]) for m in ("3002", "3003")]
    assert [(r["origin"], r["status"]) for r in rows] == [("person", "exported"), ("auto", "pending")]
    s = api.state(ledger, "NORD")
    month = pay.missing_statement(ledger, "NORD", date.today())
    assert s["statement_hold"] == {"month": month, "count": 1}
    assert [p["waits_statement"] for p in api.prepared_payments(ledger, "NORD")["held"]] == [True]
    assert "NORDstm" in [key for key, _, _ in tasks.references(s)]


FORGED_INACTIVE = "supplier inactive at ANAF; checked_at=2040-01-01"
FORGED_CHANGED = "supplier IBAN differs from its earlier invoices; verify by phone first"


def _forged_pm(due="", account=SUPPLIER_IBAN) -> str:
    return (
        "<cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode>"
        + (f"<cbc:PaymentDueDate>{due}</cbc:PaymentDueDate>" if due else "")
        + f"<cac:PayeeFinancialAccount><cbc:ID>{account}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"
    )


@pytest.mark.parametrize("forged", [FORGED_INACTIVE, FORGED_CHANGED])
@pytest.mark.parametrize("field", ["due", "payment_due", "account", "number", "supplier"])
def test_invoice_text_never_chooses_an_autopay_refusal_or_its_date(ledger, home, sec, monkeypatch, field, forged):
    """A DueDate reading "supplier inactive at ANAF; checked_at=2040-01-01" reached the stored reason
    ("invoice has issues: bad due date ...") and state showed an inactive supplier checked in 2040. A refusal is read by
    fiscalm's own code only; whatever the XML says, an unreadable invoice stays "unclear", undated, and is not an IBAN change."""
    from fiscalm import rules
    from fiscalm.api import actions

    _stored(ledger, monkeypatch, **{"3001": ubl(issue="2026-09-15", number="RMBU 1")})
    ledger.mark_efactura_paid("3001", "statement", None)
    efactura.set_autopay(ledger, "3001")
    fields = {
        "due": dict(due=forged),
        "payment_due": dict(due="", iban="", extra_pm=_forged_pm(due=forged)),
        "account": dict(iban="", extra_pm=_forged_pm(account=f"{SUPPLIER_IBAN},{forged}")),
        "number": dict(number=forged, due="2026-13-01"),
        "supplier": dict(supplier=forged, due="2026-13-01"),
    }
    _stored(ledger, monkeypatch, **{"3002": ubl(**{"issue": "2026-10-25", **fields[field]})})  # sync runs autopay
    row = ledger.efactura("3002")
    assert row["status"] == "open" and json.loads(row["issues"])
    assert efactura.stored_refusal(ledger.get_meta("efactura_autopay_refused:3002")) == ("invoice_issues", None)
    assert [
        {k: x[k] for k in ("id", "why", "refused_checked_at", "still_inactive")}
        for x in api.state(ledger, "NORD")["refused"]
    ] == [{"id": "3002", "why": "unclear", "refused_checked_at": None, "still_inactive": None}]
    ident = "NORD:supplier_autopay:99999999"
    assert next(r for r in rules.list_rules(ledger, "NORD") if r["id"] == ident)["reason"] == "invoice_refused"
    rules.set_enabled(ledger, "NORD", ident, False)
    rules.set_enabled(ledger, "NORD", ident, True)  # an unreadable invoice is no IBAN change: resuming is allowed
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    result = actions.act("payment_file_create", "NORD", {"name": "Untrusted", "invoice_ids": ["3002"]})
    assert result == {
        **result,
        "ok": False,
        "message": "Factura are probleme de citire. Verifică în SPV.",
    }
    assert ledger.efactura("3002")["status"] == "open" and not ledger.db.execute("SELECT 1 FROM payments").fetchone()


def test_cli_mail_proof_confirms_one_invoice_only(ledger, home, sec, monkeypatch, capsys):
    from types import SimpleNamespace

    from fiscalm import cli

    _stored(
        ledger,
        monkeypatch,
        **{
            "3001": ubl(supplier="LUMEN AMBIENT SRL", total="486.00", number="F-LUM 30550"),
            "3002": ubl(supplier="LUMEN AMBIENT SRL", total="486.00", number="F-LUM 30561"),
        },
    )
    two = SimpleNamespace(message_ids=["3001", "3002"], mail=["nm:proof"], note="mail Lumen")
    assert cli.cmd_efactura_paid(two, ledger) == 2  # one mail never confirms two invoices
    assert {ledger.efactura(m)["status"] for m in ("3001", "3002")} == {"open"}
    one = SimpleNamespace(message_ids=["3001"], mail=["nm:proof"], note="mail Lumen")
    assert cli.cmd_efactura_paid(one, ledger) == 0 and ledger.efactura("3001")["status"] == "paid"
    again = SimpleNamespace(message_ids=["3002"], mail=["nm:proof"], note="mail Lumen")
    assert cli.cmd_efactura_paid(again, ledger) == 1 and ledger.efactura("3002")["status"] == "open"


# ------------------------------------------------------------------ ambiguous debits: recorded, the user picks one


def _supplier_twins(ledger, amount="350.00", first=3384, account="CECE0000000000000001"):
    """Two monthly supplier invoices of the same amount, paid by a transfer that names neither (the real case)."""
    from fiscalm.payments.orders import PaymentItem

    iban = synthetic_iban("RO00" + account)
    ids = []
    for n, d in ((first, date(2026, 9, 1)), (first + 1, date(2026, 10, 1))):
        item = PaymentItem(
            "NORD",
            "efactura",
            "ACCOUNTANT SRL",
            iban,
            "CECEROBU",
            "",
            Decimal(amount),
            f"Factura AA {n}",
            d,
            f"AA {n}",
            "",
            f"sha-aa-{n}",
        )
        assert ledger.add_payment(item)
        ids.append(item.item_id)
    plain = _tx(amount, date(2026, 10, 20), "ACCOUNTANT SRL", iban, desc="plata servicii contabilitate")
    return ids, plain, _debit_st(plain)


def _match(ledger, st):

    return recon.auto_mark_paid(ledger, st) + efactura.match_statement(ledger, st)


def test_ambiguous_debit_is_recorded_refreshed_and_dropped(ledger, home, sec):
    ids, plain, st = _supplier_twins(ledger)
    for _ in range(2):  # every sync matches again: one record, refreshed
        assert _match(ledger, st) == []
        [a] = ledger.ambiguous("NORD")
        assert a["tx_key"] == statements.tx_key(plain) and json.loads(a["tx"])["amount"] == "350.00"
        assert sorted(c[1] for c in json.loads(a["candidates"])) == sorted(ids)
    [shown] = api.ambiguous(ledger, "NORD")
    assert [c["number"] for c in shown["candidates"]] == ["AA 3384", "AA 3385"] and shown[
        "counterparty"
    ] == "ACCOUNTANT SRL"
    st.accounts[0].txs.append(
        _tx("350.00", date(2026, 10, 21), "ACCOUNTANT SRL", plain.counterparty_iban, desc="plata AA 3385")
    )
    assert sorted(_match(ledger, st)) == sorted(ids)  # the named one pays its invoice, the plain one the other
    assert ledger.ambiguous() == [] and api.ambiguous(ledger, "NORD") == []
    ledger.set_ambiguous(statements.tx_key(plain), "NORD", plain.to_dict(), [["payment", ids[0]]])
    assert ledger.ambiguous() == []  # a reserved debit is never recorded


def test_resolve_settles_the_chosen_candidate_with_the_debit(ledger, home, sec):
    from fiscalm import cli

    ids, plain, st = _supplier_twins(ledger)
    _match(ledger, st)
    key = statements.tx_key(plain)
    for co, tx, target in (
        ("NORD", key, "3001"),
        ("STUDIO9", key, ids[1]),
        ("NORD", "nope", ids[1]),
        ("NORD", "", ids[1]),
    ):
        with pytest.raises(efactura.ResolveError):
            efactura.resolve_debit(ledger, co, tx, target)
    assert cli.main(["pay", "resolve", key, ids[1]]) == 0
    assert ledger.payment(ids[1])["status"] == "paid" and ledger.payment(ids[0])["status"] == "pending"
    assert "chosen by the user" in ledger.payment(ids[1])["paid_note"]
    assert ledger.db.execute("SELECT what FROM settled_tx WHERE tx_key=?", (key,)).fetchone()[0] == f"payment {ids[1]}"
    assert ledger.ambiguous() == [] and cli.main(["pay", "resolve", key, ids[0]]) == 1  # the debit is used
    assert _match(ledger, st) == [] and ledger.payment(ids[0])["status"] == "pending"  # never the same debit again


def test_resolve_refuses_a_used_debit_or_a_stale_candidate(ledger, home, sec):
    ids, plain, st = _supplier_twins(ledger)
    _match(ledger, st)
    (home / "state" / "LIVE").touch()
    key = statements.tx_key(plain)
    assert ledger.mark_paid(ids[0], "paid another way")  # stale: the candidate no longer fits
    [shown] = api.ambiguous(ledger, "NORD")
    assert [c["id"] for c in shown["candidates"]] == [ids[1]]  # a page refresh no longer offers it
    with pytest.raises(efactura.ResolveError):
        efactura.resolve_debit(ledger, "NORD", key, ids[0])
    assert len(ledger.ambiguous()) == 1  # kept: the other candidate still fits
    assert efactura.resolve_debit(ledger, "NORD", key, ids[1]) == ("payment", ids[1])
    ids2, plain2, st2 = _supplier_twins(ledger, "410.00", 4001, "CECE0000000000000002")
    _match(ledger, st2)
    key2 = statements.tx_key(plain2)
    with ledger.tx() as db:  # reserved by something else since it was recorded
        db.execute("INSERT INTO settled_tx VALUES(?,?,?)", (key2, "payment other", "now"))
    with pytest.raises(efactura.ResolveError):
        efactura.resolve_debit(ledger, "NORD", key2, ids2[0])
    assert {ledger.payment(i)["status"] for i in ids2} == {"pending"}
    assert ledger.ambiguous() == [] and api.ambiguous(ledger, "NORD") == []  # nothing left to choose


def test_two_concurrent_resolves_of_one_debit_have_one_winner(ledger, home, sec):
    import threading

    from fiscalm.ledger import Ledger

    ids, plain, st = _supplier_twins(ledger)
    _match(ledger, st)
    (home / "state" / "LIVE").touch()
    key, gate, won = statements.tx_key(plain), threading.Barrier(2), []

    def caller(target):
        L = Ledger()
        gate.wait()
        try:
            won.append(efactura.resolve_debit(L, "NORD", key, target)[1])
        except efactura.ResolveError:
            won.append(None)
        L.close()

    threads = [threading.Thread(target=caller, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(won, key=str) == sorted([None, next(w for w in won if w)], key=str)
    assert sorted(ledger.payment(i)["status"] for i in ids) == ["paid", "pending"]
    assert ledger.db.execute("SELECT COUNT(*) FROM settled_tx WHERE tx_key=?", (key,)).fetchone()[0] == 1


def test_rows_from_before_the_full_tax_id_get_it_from_their_own_zip(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(supplier_cui="DE123456789")})
    assert ledger.efactura("3001")["supplier_tax_id"] == "DE123456789"
    with ledger.tx() as db:
        db.execute("UPDATE efactura SET supplier_tax_id=NULL")  # as a ledger written before the column existed
    assert efactura.fill_tax_ids(ledger) == 1
    assert ledger.efactura("3001")["supplier_tax_id"] == "DE123456789" and efactura.fill_tax_ids(ledger) == 0


def test_company_not_authorised_is_warning_and_other_companies_continue(ledger, home, sec, monkeypatch):
    from fiscalm import companies

    _fake_anaf(monkeypatch, {}, {})
    calls = []

    def received(cui, *args):
        calls.append(cui)
        if cui == NORD_CUI:
            raise anaf.CompanyUnauthorized("fake denial")
        return []

    monkeypatch.setattr(anaf, "list_received", received)
    for _ in range(2):
        result = efactura.sync(ledger)
        assert not result["errors"] and len(result["warnings"]) == 1
    assert len(calls) == 2 * len(companies.all())


def test_company_access_transport_classification(monkeypatch, sec, home):
    _connected()
    import urllib.error

    def refused(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, None)

    monkeypatch.setattr(anaf.urllib.request, "urlopen", refused)
    with pytest.raises(anaf.CompanyUnauthorized):
        anaf.list_received(NORD_CUI, datetime(2026, 1, 1), datetime(2026, 1, 2), "fake")


def test_issued_company_not_authorised_is_warning(ledger, home, sec, monkeypatch, capsys):
    from argparse import Namespace

    from fiscalm import cli

    _fake_anaf(monkeypatch, {}, {})
    calls = []

    def received(cui, *args):
        calls.append(cui)
        if cui == NORD_CUI:
            raise anaf.CompanyUnauthorized("fake denial")
        return []

    monkeypatch.setattr(anaf, "list_sent", received)
    assert cli.cmd_efactura_sync_issued(Namespace(json=False), ledger) == 0
    assert len(calls) == 2 and "Warning: NORD:" in capsys.readouterr().out
    assert not ledger.get_meta("issued_last_error")


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Nu aveti drept de acces pentru CIF-ul cerut", anaf.CompanyUnauthorized),
        ("Nu aveți drept de acces pentru CIF-ul cerut", anaf.CompanyUnauthorized),
        ("Unexpected service error", anaf.AnafError),
    ],
)
def test_company_access_json_classification(monkeypatch, message, expected):
    monkeypatch.setattr(anaf, "_get", lambda *a: (200, json.dumps({"eroare": message}).encode(), "application/json"))
    with pytest.raises(expected) as exc:
        anaf.list_received(NORD_CUI, datetime(2026, 1, 1), datetime(2026, 1, 2), "fake")
    assert type(exc.value) is expected


@pytest.mark.parametrize("module,listing", [("efactura", "list_received"), ("issued", "list_sent")])
@pytest.mark.parametrize("case", ["new", "previously-listed", "all-denied", "recovered"])
def test_spv_denial_persistence_and_status(ledger, home, sec, monkeypatch, capsys, module, listing, case):
    from argparse import Namespace

    from fiscalm import cli
    from fiscalm.api import state as api
    from fiscalm.invoicing import issued

    mod = efactura if module == "efactura" else issued
    _fake_anaf(monkeypatch, {}, {})

    def listed(cui, *args):
        if case != "recovered" and (case == "all-denied" or cui == NORD_CUI):
            raise anaf.CompanyUnauthorized("fake denial")
        return []

    monkeypatch.setattr(anaf, listing, listed)
    if case == "previously-listed":
        # Record the successful empty listing through the real sync path.
        with monkeypatch.context() as m:
            m.setattr(anaf, listing, lambda *a: [])
            mod.sync(ledger)
        assert ledger.get_meta(module + "_listed:NORD") == "1"
    ledger.set_meta(module + "_last_error", "old failure")
    ledger.set_meta(module + "_warnings", "old warning")
    res = mod.sync(ledger)
    assert api.state(ledger, "NORD")["system"]["anaf"]["reconnect"] is (case in ("previously-listed", "all-denied"))
    if case in ("previously-listed", "all-denied"):
        assert res["errors"] and "SPV access" in ledger.get_meta(module + "_last_error")
    elif case == "new":
        assert not res["errors"] and res["warnings"]
        assert not ledger.get_meta(module + "_last_error")  # a successful read clears stale errors despite warnings
    else:
        assert not res["errors"] and not res["warnings"]
        assert not ledger.get_meta(module + "_last_error")
        assert not ledger.get_meta(module + "_warnings")
    if case in ("new", "all-denied"):
        assert "NORD:" in ledger.get_meta(module + "_warnings")
        assert any("NORD:" in w for w in api.state(ledger, "NORD")["system"]["anaf"]["warnings"])
        cli.cmd_status(Namespace(json=True), ledger)
        assert any("NORD:" in w for w in json.loads(capsys.readouterr().out)["spv_warnings"])
        cli.cmd_status(Namespace(json=False), ledger)
        assert "SPV warning: NORD:" in capsys.readouterr().out

    command = cli.cmd_efactura_sync if module == "efactura" else cli.cmd_efactura_sync_issued
    assert command(Namespace(json=True), ledger) == (2 if case in ("previously-listed", "all-denied") else 0)
    capsys.readouterr()


@pytest.mark.parametrize("module,listing", [("efactura", "list_received"), ("issued", "list_sent")])
def test_spv_existing_download_proves_previous_access(ledger, home, sec, monkeypatch, module, listing):
    from fiscalm.invoicing import issued

    _fake_anaf(monkeypatch, {}, {})
    ledger.db.execute(
        f"INSERT INTO {module}(message_id,company,raw_path,status,created_at,parse_error) "
        "VALUES('98765','NORD','missing.zip','duplicate','2026-01-01','synthetic')"
    )
    ledger.db.commit()

    def listed(cui, *args):
        if cui == NORD_CUI:
            raise anaf.CompanyUnauthorized("fake denial")
        return []

    monkeypatch.setattr(anaf, listing, listed)
    mod = efactura if module == "efactura" else issued
    assert mod.sync(ledger)["errors"]
    assert ledger.get_meta(module + "_listed:NORD") == "1"


def test_account_proof_is_atomic_with_two_connection_reservation(ledger, home, sec, monkeypatch):
    import threading

    from fiscalm.ledger import Ledger

    _stored(ledger, monkeypatch, **{"3001": ubl(number="A1"), "3002": ubl(number="B2", iban=OTHER_IBAN)})
    gate, won = threading.Barrier(2), []

    def settle(mid, iban):
        other = Ledger()
        try:
            gate.wait()
            if other.mark_efactura_paid(mid, "statement", "shared-proof", to_iban=iban):
                won.append(iban)
        finally:
            other.close()

    threads = [threading.Thread(target=settle, args=pair) for pair in [("3001", SUPPLIER_IBAN), ("3002", OTHER_IBAN)]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1
    assert [r["iban"] for r in ledger.db.execute("SELECT iban FROM supplier_ibans")] == won
    assert len(ledger.used_txs()) == 1


def test_resolved_exact_account_records_bank_evidence(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl(number="A1"), "3002": ubl(number="A2")})
    (home / "state").mkdir(exist_ok=True)
    (home / "state" / "LIVE").touch()
    t = _tx("250.00", date(2026, 10, 21), "CENTRUL MEDICAL EXEMPLU SRL", SUPPLIER_IBAN)
    assert efactura.match_statement(ledger, _debit_st(t)) == []
    efactura.resolve_debit(ledger, "NORD", efactura.tx_key(t), "3001")
    assert [r["iban"] for r in ledger.db.execute("SELECT iban FROM supplier_ibans")] == [SUPPLIER_IBAN]
    assert efactura.payment_item(ledger, "3002").doc_ref == "A2"


def test_account_evidence_failure_rolls_back_the_settlement(ledger, home, sec, monkeypatch):
    import sqlite3

    _stored(ledger, monkeypatch, **{"3001": ubl(number="A1")})
    ledger.db.execute(
        "CREATE TRIGGER fail_account BEFORE INSERT ON supplier_ibans BEGIN SELECT RAISE(ABORT, 'crash'); END"
    )
    ledger.db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="crash"):
        ledger.mark_efactura_paid("3001", "statement", "bank-proof", to_iban=SUPPLIER_IBAN)
    assert ledger.efactura("3001")["status"] == "open"
    assert not ledger.used_txs()
    assert not ledger.db.execute("SELECT 1 FROM supplier_ibans").fetchone()


def test_empty_supplier_never_records_statement_account(ledger, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    ledger.db.execute("UPDATE efactura SET supplier_cui='' WHERE message_id='3001'")
    ledger.db.commit()
    assert ledger.mark_efactura_paid("3001", "statement", "bank-proof", to_iban=SUPPLIER_IBAN)
    assert not ledger.db.execute("SELECT * FROM supplier_ibans").fetchall()


def test_inactive_supplier_precedes_account_proof_and_issues(ledger, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    monkeypatch.setattr(efactura.PaymentFacts, "inactive", lambda *args: {"checked_at": "2026-10-21"})
    row = dict(ledger.efactura("3001")) | {"issues": '["synthetic issue"]'}
    with pytest.raises(efactura.ApproveError, match="supplier inactive at ANAF") as exc:
        efactura.check_payment(ledger, row)
    assert (exc.value.code, exc.value.checked_at) == ("supplier_inactive", "2026-10-21")
    assert efactura.current_refusal(ledger, row) == "supplier_inactive"
    with pytest.raises(efactura.ApproveError, match="only RON"):
        efactura.check_payment(ledger, row | {"currency": "EUR"})
    with pytest.raises(efactura.ApproveError, match="not an open"):
        efactura.check_payment(ledger, row | {"status": "paid"})


def test_the_payment_file_note_names_no_bank(ledger, home, sec, monkeypatch):
    """An approved invoice, and one put back after an undo, say they are in the payment file, not in one bank's app."""
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    efactura.approve(ledger, "3001", origin="person")
    assert ledger.efactura("3001")["paid_note"] == "added to the payment file by the user"
    assert efactura.withdraw(ledger, "3001", "user: already paid (page)") is None
    assert efactura.reopen(ledger, "3001") == "queued"
    assert ledger.efactura("3001")["paid_note"].startswith("added to the payment file by the user")
    assert "BT Go" not in ledger.efactura("3001")["paid_note"]


@pytest.mark.parametrize(
    "case,left,grouped",
    [
        ("full", "0.00", True),
        ("partial", "200.00", True),
        ("missing", "250.00", False),
        ("firm", "250.00", False),
        ("supplier", "250.00", False),
        ("currency", "250.00", False),
        ("over", "250.00", False),
        ("ambiguous", "250.00", False),
    ],
)
def test_received_credit_groups_only_one_matching_invoice(ledger, home, monkeypatch, case, left, grouped):
    credit = ubl(
        number="COR 2",
        total="-50.00" if case == "partial" else "-251.00" if case == "over" else "-250.00",
        buyer="22222222" if case == "firm" else NORD_CUI,
        supplier_cui="RO88888888" if case == "supplier" else "RO99999999",
        currency="EUR" if case == "currency" else "RON",
    )
    note = f"<cbc:Note>Storno factura seria TEST nr. {'999' if case == 'missing' else '0001'} din data 20/10/2026</cbc:Note>"
    if case == "ambiguous":
        note = "<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>TEST 1</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference>"
    credit = credit.replace(b"<cbc:DocumentCurrencyCode>", note.encode() + b"<cbc:DocumentCurrencyCode>", 1)
    _stored(ledger, monkeypatch, **{"3001": ubl(number="TEST 1"), "3002": credit})
    if case == "ambiguous":
        _stored(ledger, monkeypatch, **{"3003": ubl(number="TEST 1", issue="2026-10-19")})
    rows = {r["id"]: r for r in api.state(ledger, "NORD")["invoices"]}
    assert rows["3001"]["left"] == left
    assert bool(rows["3001"]["credits"]) is grouped
    if case != "firm":
        assert ("3002" not in rows) is grouped
        if not grouped:
            assert rows["3002"]["credit_note"] and not rows["3002"]["can_mark_paid"]
    if grouped:
        with pytest.raises(efactura.ApproveError, match="credit note"):
            efactura.payment_item(ledger, "3001")
        assert (
            efactura.match_statement(
                ledger, _debit_st(_tx("250.00", date(2026, 10, 21), "CENTRUL MEDICAL EXEMPLU SRL", SUPPLIER_IBAN))
            )
            == []
        )
    assert sum((Decimal(r["total"]) for r in efactura.unpaid(ledger, "NORD")), Decimal(0)) == Decimal(left) + (
        Decimal("250") if case == "ambiguous" else 0
    )


def test_product_credit_reference_reads_only_own_company_archive(ledger, home, monkeypatch):
    ref = b"<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>TEST 1</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference>"
    credit = ubl(root="CreditNote", number="COR 2", total="50.00").replace(b"</CreditNote>", ref + b"</CreditNote>")
    _stored(ledger, monkeypatch, **{"3001": ubl(number="TEST 1"), "3002": credit})
    ledger.db.execute("UPDATE efactura SET billing_ref=NULL WHERE message_id='3002'")
    ledger.db.commit()
    efactura.fill_credit_references(ledger)
    assert efactura.credited(ledger, "NORD") == {"3001": Decimal("50")}
    ledger.db.execute(
        "UPDATE efactura SET raw_path=?,billing_ref=NULL WHERE message_id='3002'", (str(home / "outside.zip"),)
    )
    ledger.db.commit()
    (home / "outside.zip").write_bytes(anaf_zip(credit))
    efactura.fill_credit_references(ledger)
    assert efactura.credited(ledger, "NORD") == {}


@pytest.mark.parametrize("exported", [False, True])
@pytest.mark.parametrize("start", [None, "2026-11-01"])
def test_credit_withdraws_unimported_payment_and_never_exports_original_amount(
    ledger, home, monkeypatch, exported, start
):
    (home / "state" / "LIVE").touch()
    _stored(ledger, monkeypatch, **{"3001": ubl(number="TEST 1")})
    item = efactura.approve(ledger, "3001", origin="person")["item_id"]
    ident = ledger.create_payment_file("NORD", "Credited invoice")
    assert ledger.attach_payment_file(ident, "NORD", item)
    if exported:
        snapshot = pay.download_file(ledger, ident, "NORD", date.today(), lambda: None)
        original = pay.named_path(ledger.payment_file(ident)).read_bytes()
    credit = ubl(number="COR 2", total="-50.00").replace(
        b"</Invoice>",
        b"<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>TEST 1</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference></Invoice>",
    )
    if start:
        ledger.set_meta("history_start:NORD", start)
    _stored(ledger, monkeypatch, **{"3002": credit})
    with pytest.raises(PaymentFileError):
        pay.download_file(ledger, ident, "NORD", date.today(), lambda: None)
    if exported:
        assert pay.named_path(ledger.payment_file(ident)).read_bytes() == original
        assert ledger.payment(item)["status"] == "exported"
        assert pay.import_file(ledger, ident, "NORD", snapshot["download_id"], date.today())
        assert (
            efactura.match_statement(
                ledger, _debit_st(_tx("250.00", date(2026, 10, 21), "CENTRUL MEDICAL EXEMPLU SRL", SUPPLIER_IBAN))
            )
            == []
        )
        assert recon.auto_mark_paid(
            ledger, _debit_st(_tx("250.00", date(2026, 10, 21), "CENTRUL MEDICAL EXEMPLU SRL", SUPPLIER_IBAN))
        )
    else:
        assert not pay.named_path(ledger.payment_file(ident)).exists()
        assert ledger.payment(item)["status"] == "pending"
        assert efactura.unpaid(ledger, "NORD")[0]["total"] == "200.00"


def test_received_credit_with_multiple_original_references_refuses_grouping(ledger, monkeypatch):
    refs = b"".join(
        b"<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>"
        + number
        + b"</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference>"
        for number in (b"TEST 1", b"TEST 2")
    )
    credit = ubl(root="CreditNote", number="COR 2", total="50.00").replace(b"</CreditNote>", refs + b"</CreditNote>")
    _stored(ledger, monkeypatch, **{"3001": ubl(number="TEST 1"), "3002": credit})
    assert efactura.credited(ledger, "NORD") == {}


@pytest.mark.parametrize("proved", [False, True])
def test_full_credit_preserves_payment_proof_but_cancels_unconfirmed_mark(ledger, monkeypatch, proved):
    _stored(ledger, monkeypatch, **{"3001": ubl(number="TEST 1")})
    assert ledger.mark_efactura_paid("3001", "statement" if proved else "user: paid", "bank-proof" if proved else None)
    credit = ubl(number="COR 2", total="-250.00").replace(
        b"</Invoice>", b"<cbc:Note>Storno factura seria TEST nr. 1 din data 20/10/2026</cbc:Note></Invoice>"
    )
    _stored(ledger, monkeypatch, **{"3002": credit})
    row = next(r for r in api.state(ledger, "NORD")["invoices"] if r["id"] == "3001")
    assert row["status"] == ("paid" if proved else "skipped")
    assert row["left"] == "0.00" and len(row["credits"]) == 1
    assert ledger.efactura("3001")["status"] == "paid"


@pytest.mark.parametrize("reference_date,credit_date", [("2025-10-20", "2026-10-21"), ("2026-10-20", "2026-10-19")])
def test_received_credit_refuses_reused_number_or_original_after_credit(
    ledger, monkeypatch, reference_date, credit_date
):
    reference = f"<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>TEST 1</cbc:ID><cbc:IssueDate>{reference_date}</cbc:IssueDate></cac:InvoiceDocumentReference></cac:BillingReference>"
    credit = ubl(root="CreditNote", number="COR 2", total="250.00", issue=credit_date).replace(
        b"</CreditNote>", reference.encode() + b"</CreditNote>"
    )
    _stored(ledger, monkeypatch, **{"3001": ubl(number="TEST 1"), "3002": credit})
    assert efactura.credited(ledger, "NORD") == {}
    assert efactura.unpaid(ledger, "NORD")[0]["total"] == "250.00"


@pytest.mark.parametrize("settle", ["import", "statement"])
def test_credit_hold_is_scoped_to_its_file_and_keeps_downloaded_evidence(ledger, home, monkeypatch, settle):
    (home / "state" / "LIVE").touch()
    _stored(ledger, monkeypatch, **{"3001": ubl(number="TEST 1", total="100.00")})
    first = efactura.approve(ledger, "3001", origin="person")["item_id"]
    one, snapshot = download_batch(ledger, [first], today=date.today())
    _stored(ledger, monkeypatch, **{"3002": ubl(number="TEST 2")})
    second = efactura.approve(ledger, "3002", origin="person")["item_id"]
    two = ledger.create_payment_file("NORD", "Credit hold")
    assert ledger.attach_payment_file(two, "NORD", second)
    credit = ubl(number="COR 3", total="-50.00").replace(
        b"</Invoice>", b"<cbc:Note>Storno factura seria TEST nr. 2 din data 20/10/2026</cbc:Note></Invoice>"
    )
    _stored(ledger, monkeypatch, **{"3003": credit})
    if settle == "import":
        assert pay.import_file(ledger, one, "NORD", snapshot["download_id"], date.today())
        assert not ledger.get_meta(f"payment_import:{one}")
    else:
        tx = _tx("100.00", date(2026, 10, 21), "CENTRUL MEDICAL EXEMPLU SRL", SUPPLIER_IBAN)
        ledger.record_statement(_debit_st(tx))
        assert ledger.mark_paid(first, "statement", statements.tx_key(tx))
    with pytest.raises(PaymentFileError):
        pay.download_file(ledger, two, "NORD", date.today(), lambda: None)
    assert len(pay.file_downloads(ledger, one)) == 1
    assert not pay.named_path(ledger.payment_file(two)).exists()


def test_shadow_mark_changes_ledger_without_rewriting_a_download(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"3001": ubl()})
    (home / "state" / "LIVE").touch()
    item = efactura.approve(ledger, "3001", origin="person")["item_id"]
    ident, _ = download_batch(ledger, [item], today=date.today())
    path = pay.named_path(ledger.payment_file(ident))
    before = path.read_bytes()
    (home / "state" / "LIVE").unlink()
    assert efactura.withdraw(ledger, "3001", "user: paid") is None
    assert path.read_bytes() == before and len(pay.file_downloads(ledger, ident)) == 1
    assert ledger.payment(item)["status"] == "skipped"


# ------------------------------------------------------------------ invoice lines on the sheet

LINES = [
    ("Găzduire web, pachet anual", "1", "H87", "480.00", "480.00", "21"),
    ("Ore suport", "2.5", "XYZ", "55", "137.50", "21"),
]


@pytest.mark.parametrize("root", ["Invoice", "CreditNote"])
def test_sheet_lines_come_from_the_stored_xml(ledger, home, sec, monkeypatch, root):
    _stored(ledger, monkeypatch, **{"3001": ubl(root=root, extra_pm=ubl_lines(LINES, root))})
    lines = invoice_lines.sheet(ledger, "NORD", "invoice", {"id": "3001"})
    assert lines == {
        "lines": [
            dict(position=1, description=LINES[0][0], quantity="1", unit="buc", unit_price="480.00", net="480.00",
                 vat_rate="21", vat_category="S", vat="100.80", gross="580.80"),
            dict(position=2, description="Ore suport", quantity="2.5", unit="XYZ", unit_price="55", net="137.50",
                 vat_rate="21", vat_category="S", vat="28.88", gross="166.38"),
        ],
        "currency": "RON",
        "truncated": False,
        "unavailable": False,
        "totals": None,  # no TaxTotal: never summed from the lines
    }  # fmt: skip
    many = efactura.safe_xml(ubl(extra_pm=ubl_lines(LINES * 101)))
    assert len(invoice_lines.from_xml(many)["lines"]) == 200 and invoice_lines.from_xml(many)["truncated"]
    with pytest.raises(efactura.UblError):  # line 201 is checked too: a refusal is whole
        invoice_lines.from_xml(
            efactura.safe_xml(ubl(extra_pm=ubl_lines(LINES * 100 + [("Ore", "1", "H87", "5", "NaN", "21")])))
        )


ARCHIVES = ["utf-8 name", "truncated", "method", "lzma", "corrupt lzma", "bzip2", "corrupt deflate", "crc"]


def _damaged(kind: str) -> bytes:
    """ZIPs that fail while opened, listed or read, or use a method ANAF does not: a UTF-8 flagged name with an
    invalid byte, a cut-off archive, an unknown method, LZMA and bzip2 (valid or corrupt), a corrupt deflate stream
    and a CRC-32 that does not match."""
    methods = {"lzma": zipfile.ZIP_LZMA, "corrupt lzma": zipfile.ZIP_LZMA, "bzip2": zipfile.ZIP_BZIP2}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", methods.get(kind, zipfile.ZIP_DEFLATED)) as z:
        z.writestr("invoice.xml", ubl(extra_pm=ubl_lines(LINES)))
        z.writestr("semnatura_1.xml", b"<Signature/>")
    data = bytearray(buf.getvalue())
    central, local = data.find(b"PK\x01\x02"), data.find(b"PK\x03\x04")  # the invoice member, listed first
    if kind == "utf-8 name":
        data[central + 9] |= 0x08  # general purpose bit 11: the name is UTF-8
        data[central + 46] = 0xFF
    elif kind == "method":
        data[local + 8] = data[central + 10] = 99
    elif kind == "corrupt lzma":
        data[45] = 0  # the first byte of the LZMA properties, after the local header and the name
    elif kind == "corrupt deflate":
        data[local + 45 : local + 60] = b"\xff" * 15
    elif kind == "crc":
        data[central + 16] ^= 0xFF
    return bytes(data[:-40] if kind == "truncated" else data)


@pytest.mark.parametrize(
    "case",
    ["dtd", "amount", "outside", "other company", "missing", *ARCHIVES]
    + [f"base {b!r}" for b in ("NaN", "1e3", "", "0", "-2")],
)
def test_sheet_lines_refuse_rather_than_guess(ledger, home, sec, monkeypatch, case):
    _stored(ledger, monkeypatch, **{"3001": ubl(extra_pm=ubl_lines(LINES))})
    stored = Path(ledger.efactura("3001")["raw_path"])
    if case == "dtd":
        stored.write_bytes(anaf_zip(b'<!DOCTYPE x [<!ENTITY e "x">]>' + ubl(extra_pm=ubl_lines(LINES))[38:]))
    elif case == "amount":
        stored.write_bytes(anaf_zip(ubl(extra_pm=ubl_lines([("Ore", "1", "HUR", "1e3", "1000", "21")]))))
    elif case.startswith("base"):
        base = f"<cbc:BaseQuantity>{case[6:-1]}</cbc:BaseQuantity></cac:Price>"
        stored.write_bytes(anaf_zip(ubl(extra_pm=ubl_lines(LINES).replace("</cac:Price>", base, 1))))
    elif case in ARCHIVES:
        with pytest.raises(efactura.UblError):  # every caller of the shared reader gets the refusal
            efactura.xml_from_zip(_damaged(case))
        stored.write_bytes(_damaged(case))
    elif case == "outside":
        outside = home / "elsewhere.zip"  # a ledger path outside the company's e-Factura directory
        outside.write_bytes(stored.read_bytes())
        ledger.db.execute("UPDATE efactura SET raw_path=? WHERE message_id='3001'", (str(outside),))
    elif case == "other company":
        ledger.db.execute("UPDATE efactura SET company='STUDIO9' WHERE message_id='3001'")
    else:
        stored.unlink()
    ledger.db.commit()
    assert invoice_lines.sheet(ledger, "NORD", "invoice", {"id": "3001"}) == invoice_lines.unavailable()
    if case == "utf-8 name":  # the sheet still opens
        assert lists.detail(ledger, "NORD", {"kind": "invoice", "id": "3001"})["lines"] == invoice_lines.unavailable()


MIXED = [("Carte", "2", "H87", "50.00", "100.00", "11"), ("Licență", "1", "H87", "200.00", "200.00", "21")]
RON_TAX = '<cac:TaxTotal><cbc:TaxAmount currencyID="RON">264.67</cbc:TaxAmount></cac:TaxTotal>'


@pytest.mark.parametrize(
    "currency, extra, expected",
    [
        ("RON", "", ("300.00", "53.00", "353.00", [("11", "100.00", "11.00"), ("21", "200.00", "42.00")])),
        ("EUR", RON_TAX, ("300.00", "53.00", "353.00", [("11", "100.00", "11.00"), ("21", "200.00", "42.00")])),
        ("RON", "malformed", None),
        ("EUR", "RON payable", None),  # an amount in another currency is not labelled with this one
        ("EUR", "RON subtotal", None),
        ("RON", RON_TAX, ("300.00", "53.00", "353.00", [("11", "100.00", "11.00"), ("21", "200.00", "42.00")])),
        ("RON", "two tax totals", None),  # XML order would choose
    ],
)
def test_sheet_totals_are_the_documents_own(currency, extra, expected):
    xml = (RON_TAX if extra == RON_TAX else "") + ubl_totals(MIXED, currency) + ubl_lines(MIXED)
    edits = {
        "malformed": (">53.00<", ">53,00<"),
        "RON payable": ('<cbc:PayableAmount currencyID="EUR">', '<cbc:PayableAmount currencyID="RON">'),
        "RON subtotal": ('<cbc:TaxableAmount currencyID="EUR">', '<cbc:TaxableAmount currencyID="RON">'),
        "two tax totals": (
            "<cac:LegalMonetaryTotal>",
            ubl_totals(MIXED).split("<cac:LegalMonetaryTotal>")[0] + "<cac:LegalMonetaryTotal>",
        ),
    }
    if extra in edits:
        xml = xml.replace(*edits[extra], 1)
    tree = efactura.safe_xml(ubl(currency=currency, total="353.00", extra_pm=xml))
    totals = invoice_lines.from_xml(tree)["totals"]
    if expected is None:
        assert totals is None
    else:
        net, vat, payable, taxes = expected
        assert (totals["net"], totals["vat"], totals["payable"]) == (net, vat, payable)
        assert [(t["rate"], t["net"], t["vat"]) for t in totals["taxes"]] == taxes


def test_sheet_lines_price_per_base_quantity_has_no_unit_price():
    base = "<cbc:BaseQuantity>12</cbc:BaseQuantity></cac:Price>"
    tree = efactura.safe_xml(ubl(extra_pm=ubl_lines(LINES).replace("</cac:Price>", base, 1)))
    assert [r["unit_price"] for r in invoice_lines.from_xml(tree)["lines"]] == [None, "55"]


def test_sync_keeps_a_damaged_message_and_stores_the_next(ledger, home, sec, monkeypatch):
    downloads, blobs = [], {"3001": _damaged("corrupt lzma"), "3002": anaf_zip(ubl())}
    _fake_anaf(monkeypatch, {NORD_CUI: [msg(m) for m in blobs]}, blobs, downloads)
    efactura.sync(ledger)
    efactura.sync(ledger)
    assert sorted(downloads) == ["3001", "3002"]  # the damaged one is not fetched again
    assert ledger.efactura("3001")["parse_error"] == "ZIP member compression method 14 refused"
    assert ledger.efactura("3002")["status"] == "open" and ledger.efactura("3002")["parse_error"] is None
