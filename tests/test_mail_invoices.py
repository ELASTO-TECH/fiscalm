"""Mailed invoices (fiscalm.mail.invoices): filed for their firm, refused, or asked about once. Synthetic data only:
the firms are NORD (Atelier Nord SRL, CUI 11111111) and STUDIO9 (Studio 9 SRL, CUI 33333333) from the example config."""

import hashlib
import json
import threading
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import call, patch

import pytest
from conftest import go_live
from firms import set_firms
from imap_fake import Fake, mail, plain_connect
from pdf_fake import text_pdf

from fiscalm import ai, ai_extract, companies, history_pdf, integrations, recon
from fiscalm.api import browse, state as api
from fiscalm.bank import statements
from fiscalm.ledger import Ledger
from fiscalm.mail import imap as imapread, invoices as mi

TODAY = date(2026, 11, 3)
PDF_BYTES = b"%PDF-1.4 synthetic "


@pytest.fixture(autouse=True)
def source_inbox(ledger):
    with ledger.tx() as db:
        db.execute(
            "INSERT INTO integrations VALUES('mail-00000001','mail','Synthetic inbox',?,1,0,'2026-01-01','2026-01-01')",
            (
                json.dumps(
                    {
                        "firm_scope": "all",
                        "from_address": "fixture@example.invalid",
                        "host": "imap.example.invalid",
                        "username": "fixture",
                        "method": "microsoft",
                        "client_id": "synthetic",
                    }
                ),
            ),
        )


def invoice(
    *lines,
    buyer="Bill to: Atelier Nord SRL, VAT RO11111111",
    total="Total 12.10 EUR",
    number="Invoice number: INV-2026-0042",
    title="INVOICE",
):
    return text_pdf(
        [
            x
            for x in (
                title,
                number,
                "Date: 2026-10-28",
                buyer,
                "Subtotal 10.00 EUR",
                "VAT 2.10 EUR",
                total,
                "Amount due 0.00 EUR",
                *lines,
            )
            if x
        ]
    )


def reading(*, fail=False, **fields):
    """A fake AI provider answering one extraction (fiscalm.ai_extract.SCHEMA); `fail` raises instead."""
    answer = {
        "doc_type": "invoice",
        "buyer_name": "Atelier Nord SRL",
        "buyer_tax_id": "RO11111111",
        "seller_name": "",
        "seller_tax_id": "",
        "number": "INV-2026-0042",
        "issue_date": "2026-10-28",
        "total": "12.10",
        "currency": "EUR",
        "card_last4": "",
        "confidence": "high",
    } | fields

    def ask(system, prompt, schema, image=None):
        if fail:
            raise RuntimeError("down")
        ask.calls.append((prompt, image))
        return answer

    ask.calls = []
    return ask


def message(*atts, address="billing@cloud.example", mid="m1@cloud.example", name="Example Cloud"):
    return imapread.Message(
        "mail-00000001",
        mid,
        f"{name} <{address}>",
        address,
        "Your invoice",
        "2026-11-02",
        tuple(imapread.Attachment(k, d) for k, d in atts),
    )


def docs_of(L, co=None):
    q = "SELECT * FROM documents" + (" WHERE company=?" if co else "")
    return [dict(r) for r in L.db.execute(q, (co,) if co else ())]


def test_end_to_end_from_the_mailbox(ledger, home):
    integrations.set_enabled(ledger, "mail-00000001", False)  # only this test's own inboxes are read
    fake = Fake()
    try:
        pdf = invoice()
        fake.add(mail(attachments=(("../../../etc/cron.d/x.pdf", "application/pdf", pdf),)), TODAY)
        row = integrations.add(
            ledger,
            "mail",
            "Synthetic",
            {
                "host": "127.0.0.1",
                "port": str(fake.port),
                "username": "reader",
                "password": "fake-password",
                "from_address": "books@firm.example",
            },
        )
        second = integrations.add(
            ledger,
            "mail",
            "Second inbox",
            {
                "host": "127.0.0.1",
                "port": str(fake.port),
                "username": "reader",
                "password": "fake-password",
                "from_address": "other@firm.example",
            },
        )
        out = mi.run(ledger, TODAY, connect=plain_connect)
        assert out["accounts"] == [
            {"account": r["id"], "ok": True, "read": 1, "left": 0, "errors": 0} for r in (row, second)
        ]
        (d,) = docs_of(ledger)
        sha = hashlib.sha256(pdf).hexdigest()
        assert (d["company"], d["kind"], d["amount"], d["currency"], d["doc_date"], d["month"], d["vendor"]) == (
            "NORD",
            "receipt",
            "12.10",
            "EUR",
            "2026-10-28",
            "2026-10",
            "Example Cloud",
        )
        assert d["source"] == f"mail:{row['id']}" and d["path"] == str(
            home.resolve() / "NORD" / "documents" / "mail" / f"{sha}.pdf"
        )
        link = ledger.db.execute("SELECT * FROM mail_documents").fetchone()
        assert (link["number"], link["address"], link["subject"]) == (
            "INV-2026-0042",
            "billing@cloud.example",
            "Your invoice",
        )
        assert [r[0] for r in ledger.db.execute("SELECT result FROM mail_seen ORDER BY rowid")] == [
            "filed:1",
            "duplicate:1",
        ]
        assert not (home / "etc").exists()
        mi.run(ledger, TODAY, connect=plain_connect)
        assert len(docs_of(ledger)) == 1
    finally:
        fake.close()


def test_a_card_debit_matches_the_mailed_invoice(ledger):
    mi.handle(ledger, message(("pdf", invoice())))
    tx = statements.Tx(
        date(2026, 10, 29),
        "POS",
        "",
        "debit",
        Decimal("59.10"),
        "RON",
        "RO00",
        orig_amount=Decimal("12.10"),
        orig_currency="EUR",
        counterparty="CLOUD EXAMPLE",
    )
    assert recon.match_receipts([tx], recon._receipts(ledger, "NORD"))[0][0] == "exact"


@pytest.mark.parametrize(
    "pdf,result",
    [
        (invoice(title="PROFORMA INVOICE"), "proforma"),
        (invoice(title="CREDIT NOTE"), "credit-note"),
        (invoice("Supplier: Furnizor Local SRL, CUI 22222222"), "efactura"),
        (invoice("Seller VAT RO22222222"), "efactura"),
        (text_pdf(["Newsletter", "Bill to: Atelier Nord SRL", "Total 12.10 EUR"]), "not-invoice"),
        (b"%PDF-1.4\nnot really a pdf", "unreadable"),
    ],
)
def test_refusals_file_nothing_and_ask_nothing(ledger, pdf, result):
    assert mi.handle(ledger, message(("pdf", pdf))) == f"{result}:1"
    assert docs_of(ledger) == [] and mi.questions(ledger) == []


def test_an_invoice_the_firm_issued_is_not_a_purchase(ledger):
    ledger.db.execute(
        "INSERT INTO issued(message_id,company,number,raw_path,status,created_at) VALUES('x','NORD','INV-2026-0042','x','open','t')"
    )
    ledger.db.commit()
    assert mi.handle(ledger, message(("pdf", invoice()))) == "issued:1"


def test_own_mail_is_skipped_and_statement_or_accountant_mail_never_filed_as_invoice(ledger):
    from fiscalm import companies

    integrations.add(
        ledger,
        "mail",
        "Own",
        {
            "host": "imap.example.test",
            "username": "u",
            "password": "x",
            "from_address": "owner@example.com",
            "own_addresses": "alias@example.com",
        },
    )
    ledger.set_meta(
        "accountant:STUDIO9", json.dumps({"cui": "", "senders": ["ops@accountant.example"], "recipient": ""})
    )
    bank = companies.all()["NORD"].statement_senders
    assert bank  # the example config's bank publishes its statement sender
    for address, result in (
        ("owner@example.com", "elsewhere"),
        ("alias@example.com", "elsewhere"),
        ("ops@accountant.example", "op:none"),
        (bank[0].lower(), "statement:none"),
    ):
        assert mi.handle(ledger, message(("pdf", invoice()), address=address)) == result, address
    assert docs_of(ledger) == []


def test_no_firm_asks_once_and_the_answer_can_be_remembered(ledger, home):
    pdf = invoice(buyer="Bill to: Someone")
    assert mi.handle(ledger, message(("pdf", pdf))) == "asked:1"
    assert mi.handle(ledger, message(("pdf", pdf), mid="again@x")) == "duplicate:1"  # one question per document
    (q,) = mi.questions(ledger)
    assert q["data"]["reason"] == "no-firm" and q["data"]["companies"] == ["NORD", "STUDIO9"]
    assert q["data"]["totals"] == [["12.10", "EUR"]] and docs_of(ledger) == []
    assert mi.answer(ledger, q["sha"], "STUDIO9", remember=True, by="ana") == {"status": "filed", "company": "STUDIO9"}
    (d,) = docs_of(ledger)
    assert d["company"] == "STUDIO9" and "firm not on the document" in d["issues"]
    assert not list((home / "mail" / "pending").iterdir())
    with pytest.raises(mi.AnswerError):  # double submit
        mi.answer(ledger, q["sha"], "STUDIO9")
    # The sender's next document without a firm goes to STUDIO9 on its own; one naming NORD is in doubt.
    assert (
        mi.handle(ledger, message(("pdf", invoice(buyer="", number="Invoice number: INV-2026-0043")), mid="n2"))
        == "filed:1"
    )
    assert mi.handle(ledger, message(("pdf", invoice(number="Invoice number: INV-2026-0044")), mid="n3")) == "asked:1"
    assert [q["data"]["reason"] for q in mi.questions(ledger)] == ["other-firm"]


def test_not_an_invoice_remembered_skips_the_sender(ledger):
    mi.handle(ledger, message(("pdf", invoice(buyer=""))))
    (q,) = mi.questions(ledger)
    assert mi.answer(ledger, q["sha"], "none", remember=True) == {"status": "not-invoice"}
    assert mi.handle(ledger, message(("pdf", invoice()), mid="n2")) == "not-invoice-sender:1"
    assert docs_of(ledger) == []


def test_two_firms_offer_only_those_two(ledger):
    mi.handle(ledger, message(("pdf", invoice("Ship to: Studio 9 SRL"))))
    (q,) = mi.questions(ledger)
    assert q["data"]["reason"] == "two-firms" and q["data"]["companies"] == ["NORD", "STUDIO9"]
    with pytest.raises(mi.AnswerError):
        mi.answer(ledger, q["sha"], "OTHER")
    assert docs_of(ledger) == []


@pytest.mark.parametrize("live_mode", [False, True])
def test_several_totals_need_the_user_or_a_checked_ai_answer(ledger, home, live_mode):
    if live_mode:
        go_live(home)
    two = invoice("Total paid 15.00 EUR")
    assert mi.handle(ledger, message(("pdf", two))) == "asked:1"
    (q,) = mi.questions(ledger)
    assert q["data"]["reason"] == "total" and q["data"]["totals"] == [["15.00", "EUR"], ["12.10", "EUR"]]
    with pytest.raises(mi.AnswerError):
        mi.answer(ledger, q["sha"], "NORD")  # which total?
    mi.answer(ledger, q["sha"], "NORD", total=1)
    assert [(d["amount"], d["currency"]) for d in docs_of(ledger)] == [("12.10", "EUR")]
    # A confident AI reading narrows to one of the totals found: filed. An amount the rules didn't find counts for nothing.
    three = invoice("Total paid 16.00 EUR", number="Invoice number: INV-2026-0050")
    ask = reading(number="INV-2026-0050", total="16.00")
    assert mi.handle(ledger, message(("pdf", three), mid="a1"), ask=ask) == "filed:1"
    four = invoice("Total paid 17.00 EUR", number="Invoice number: INV-2026-0051")
    assert mi.handle(ledger, message(("pdf", four), mid="a2"), ask=reading(total="999.00")) == "asked:1"
    down = invoice("Total paid 18.00 EUR", number="Invoice number: INV-9")
    assert mi.handle(ledger, message(("pdf", down), mid="a3"), ask=reading(fail=True)) == "asked:1"


SAAS = [
    "Receipt",
    "Example SaaS Inc        Atelier Nord SRL",
    "Receipt number SAAS-7781",
    "Paid on 2026-10-28",
    "Charged $49.00 to Visa 4242",
]


@pytest.mark.parametrize("live_mode", [False, True])
def test_a_confident_ai_reading_files_what_the_rules_could_not(ledger, home, live_mode):
    if live_mode:
        go_live(home)
    ask = reading(seller_name="Example SaaS Inc", number="SAAS-7781", total="49.00", currency="USD", card_last4="4242")
    assert mi.handle(ledger, message(("pdf", text_pdf(SAAS))), ask=ask) == "filed:1"
    (doc,) = docs_of(ledger)
    got = (doc["company"], doc["amount"], doc["currency"], doc["vendor"], doc["card_last4"])
    assert got == ("NORD", "49.00", "USD", "Example SaaS Inc", "4242")
    assert mi.questions(ledger) == [] and "Charged $49.00" in ask.calls[0][0]


@pytest.mark.parametrize(
    ("fields", "result"),
    [
        ({"confidence": "low"}, "asked:1"),  # low confidence: only the number and supplier fill the question
        ({"total": "50.00"}, "asked:1"),  # an amount the document doesn't print
        ({"currency": "EUR"}, "asked:1"),  # a currency the document doesn't show
        ({"buyer_name": "Studio 9 SRL", "buyer_tax_id": "RO33333333"}, "asked:1"),  # a firm not on the document
        ({"buyer_name": "Atelier Nord SRL", "buyer_tax_id": "RO33333333"}, "asked:1"),  # id and name disagree
        ({"buyer_name": "Atelier Nord SRL", "buyer_tax_id": "RO44444444"}, "asked:1"),  # an id none of the firms'
        ({"total": "-49.00"}, "asked:1"),
        # A refusal only the AI sees keeps the question: only the rules refuse.
        ({"doc_type": "proforma"}, "asked:1"),
        ({"doc_type": "credit_note"}, "asked:1"),
        ({"seller_tax_id": "RO44444444"}, "asked:1"),
        ({"seller_name": "Atelier Nord SRL", "buyer_name": "", "buyer_tax_id": ""}, "asked:1"),
    ],
)
def test_an_ai_reading_never_overrules_the_document(ledger, fields, result):
    base = {"seller_name": "Example SaaS Inc", "number": "SAAS-7781", "total": "49.00", "currency": "USD"}
    assert mi.handle(ledger, message(("pdf", text_pdf(SAAS))), ask=reading(**base | fields)) == result
    assert docs_of(ledger) == []
    if fields == {"confidence": "low"}:
        (q,) = mi.questions(ledger)
        assert (q["data"]["number"], q["data"]["supplier"], q["data"]["totals"]) == (
            "SAAS-7781",
            "Example SaaS Inc",
            [],
        )


@pytest.mark.parametrize(
    ("field", "value", "kept"),
    [
        ("issue_date", "2026-02-30", ""),
        ("issue_date", "28.10.2026", ""),
        ("total", "12.345", ""),
        ("total", "0.00", ""),
        ("total", "49", "49.00"),
        ("currency", "usd", "USD"),
        ("currency", "XBT", ""),
        ("number", "Data: 2026", ""),
        ("card_last4", "42", ""),
    ],
)
def test_ai_fields_pass_fixed_rules_or_stay_empty(ledger, field, value, kept):
    got = ai_extract.check(reading(**{field: value})("", "", {}), companies.all(ledger))
    assert (getattr(got, field), got.rejected) == (kept, () if kept else (field,))
    assert ai_extract.check(reading(card_last4="2026")("", "", {}), {}, "Date 2026-10-28\nTotal 1.00").card_last4 == ""


@pytest.mark.parametrize(
    ("doc", "fields"),
    [
        (invoice(buyer="Atelier Nord SRL"), {"total": "999.00"}),  # a total it doesn't print: its buyer counts neither
        (invoice("Total paid 15.00 EUR"), {"total": "15.00", "buyer_tax_id": "RO44444444"}),  # an id none of ours
        (invoice("Order ABC-1984", buyer="Atelier Nord SRL", number=""), {"number": "ABC-1984"}),  # our own number
        (text_pdf(["INVOICE", "Atelier Nord SRL", "Total 12.10"]), {}),  # a currency the document never shows
        (invoice("Ship to: Atelier", "Nord SRL", buyer="Bill to: Foreign Customer Ltd"), {}),  # a wrapped name
        (invoice("Ship to: Atelier Nord SRL", buyer="Bill to: Foreign Customer Ltd"), {}),  # only its delivery address
        (invoice("Ship to:  Atelier Nord SRL", buyer="Bill to: Foreign Customer Ltd"), {}),
        (invoice(buyer="Atelier Nord SRL", total="Total 12.10 EUR MegaCloud"), {"seller_name": "Cloud"}),
        (invoice(buyer="Atelier Nord SRL"), {"seller_name": "Invented Vendor"}),  # a supplier it doesn't print
        (text_pdf(["INVOICE", "Atelier Nord SRL", "Total 12.10"]), {"total": "", "currency": ""}),
        (invoice(buyer="Atelier Nord SRL"), {"total": "", "currency": "USD"}),  # another currency than the rules'
        (
            invoice("Order ABC-1984", "Total paid 15.00 EUR", number=""),
            {"buyer_name": "", "buyer_tax_id": "", "number": "ABC-1984", "total": "15.00"},
        ),
        (
            invoice(buyer="Customer: Other Client SA", total="Ship to: Atelier Nord SRL"),
            {"buyer_name": "Other Client SA"},
        ),
    ],
)
def test_a_contradicted_or_unsure_ai_reading_keeps_the_question(ledger, doc, fields):
    ledger.db.execute(
        "INSERT INTO issued(message_id,company,number,raw_path,status,created_at) VALUES('x','NORD','ABC-1984','x','open','t')"
    )
    ledger.db.commit()
    assert mi.handle(ledger, message(("pdf", doc)), ask=reading(**fields)) == "asked:1"


def test_a_number_the_ai_read_never_matches_another_document(ledger):
    assert mi.handle(ledger, message(("pdf", invoice()))) == "filed:1"
    other = invoice(buyer="Atelier Nord SRL", number="", title="INVOICE Ref INV-2026-0042")
    asked = mi.handle(ledger, message(("pdf", other), address="a@b.example"), ask=reading(confidence="low"))
    (q,) = mi.questions(ledger)
    assert asked == "asked:1" and api._to_check(ledger, "NORD")[-1]["number"] == "INV-2026-0042"
    assert mi.answer(ledger, q["sha"], "NORD")["status"] == "filed"


def test_a_scanned_invoice_is_read_from_its_picture(ledger):
    scan = text_pdf([])  # a page without text, as a scanner makes
    assert mi.handle(ledger, message(("pdf", scan)), ask=reading(fail=True)) == "unreadable:1"
    ask = reading(seller_name="Beispiel GmbH", seller_tax_id="DE123456789", number="R-2026-17", total="80.00")
    assert mi.handle(ledger, message(("pdf", scan)), ask=ask) == "filed:1"
    assert ask.calls[0][1][0] == "image/png" and ask.calls[0][1][1].startswith(b"\x89PNG")
    (doc,) = docs_of(ledger)
    assert (doc["company"], doc["amount"], doc["currency"], doc["doc_date"]) == ("NORD", "80.00", "EUR", "2026-10-28")
    low = text_pdf([" "])  # a low-confidence reading of a scan waits as a question, with what it read
    assert mi.handle(ledger, message(("pdf", low), mid="m2"), ask=reading(confidence="low")) == "asked:1"
    (q,) = mi.questions(ledger)
    assert (q["data"]["reason"], q["data"]["number_read"], q["data"]["totals"]) == ("no-firm", "INV-2026-0042", [])


@pytest.mark.parametrize(
    ("text", "total", "currency", "ok"),
    [
        ("Charged EUR 12.00\nFee USD 1.00", "12.00", "USD", False),  # the amount in another currency
        ("Charged EUR 12.00\nFee USD 1.00", "12.00", "EUR", True),
        ("Refund USD -12.00", "12.00", "USD", False),  # a negative amount
        ("Paid 12.00\nAll prices in EUR", "12.00", "EUR", True),
        ("Paid 12.00\nFee USD 1.00\nEUR", "12.00", "EUR", False),  # no currency on its line, two on the document
        ("Charged 12.10", "12.10", "EUR", False),  # no currency anywhere: the AI's alone counts for nothing
        ("Subtotal 10.00 EUR\nCharged EUR 12.10", "10.00", "EUR", False),
        ("VAT 2.10 EUR\nCharged 12.10 EUR", "2.10", "EUR", False),
        ("VAT 2.10 EUR\nTotal incl. VAT 12.10 EUR", "12.10", "EUR", True),
    ],
)
def test_an_ai_total_needs_its_amount_printed_with_its_currency(text, total, currency, ok):
    x = ai_extract.Extraction("invoice", "", "", "", "", "", "", "", "", total, currency, "", True)
    assert mi._agreed_totals(mi.Doc("pdf", text=text), x) == ([(total, currency)] if ok else None)


def test_duplicates_are_filed_once(ledger):
    assert mi.handle(ledger, message(("pdf", invoice()))) == "filed:1"
    assert mi.handle(ledger, message(("pdf", invoice()), mid="fwd")) == "duplicate:1"  # same bytes
    other_bytes = invoice("Thank you")  # same firm, number and total
    assert mi.handle(ledger, message(("pdf", other_bytes), mid="re")) == "duplicate:1"
    assert len(docs_of(ledger)) == 1


UBL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>DE-77</cbc:ID><cbc:IssueDate>2026-10-20</cbc:IssueDate><cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
 <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
 <cac:AccountingSupplierParty><cac:Party><cac:PostalAddress><cac:Country><cbc:IdentificationCode>{country}</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
  <cac:PartyTaxScheme><cbc:CompanyID>{supplier}</cbc:CompanyID></cac:PartyTaxScheme>
  <cac:PartyLegalEntity><cbc:RegistrationName>Beispiel Software GmbH</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party></cac:AccountingSupplierParty>
 <cac:AccountingCustomerParty><cac:Party><cac:PartyTaxScheme><cbc:CompanyID>RO11111111</cbc:CompanyID></cac:PartyTaxScheme>
  <cac:PartyLegalEntity><cbc:RegistrationName>Atelier Nord SRL</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party></cac:AccountingCustomerParty>
 <cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="EUR">49.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
</Invoice>"""


@pytest.mark.parametrize("title", ["Rechnung DE-77", "Reference DE-77"])
def test_ubl_fields_win_and_its_pdf_is_the_copy_kept(ledger, title):
    xml = UBL.format(country="DE", supplier="DE123456789").encode()
    pdf = text_pdf([title, "Atelier Nord SRL", "Gesamtbetrag 49,00 EUR", "Zwischensumme 41,18 EUR"])
    assert mi.handle(ledger, message(("xml", xml), ("pdf", pdf))) == "filed:1,same-as-pdf:1"
    (d,) = docs_of(ledger)
    assert (d["vendor"], d["amount"], d["currency"], d["doc_date"]) == (
        "Beispiel Software GmbH",
        "49.00",
        "EUR",
        "2026-10-20",
    )
    assert d["path"].endswith(".pdf")
    assert api._documents(ledger, "NORD")[0]["document_type"] == "invoice"
    with ledger.tx() as db:
        db.execute("DELETE FROM meta WHERE key=?", ("document-type:" + d["sha"],))
    assert api._documents(ledger, "NORD")[0]["document_type"] == "invoice"
    assert (
        mi.handle(ledger, message(("xml", UBL.format(country="RO", supplier="RO22222222").encode()), mid="ro"))
        == "efactura:1"
    )
    dtd = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]>' + xml[38:]
    assert mi.handle(ledger, message(("xml", dtd), mid="dtd")) == "not-invoice:1"


def test_a_tampered_pending_file_is_never_filed(ledger, home):
    mi.handle(ledger, message(("pdf", invoice(buyer=""))))
    (q,) = mi.questions(ledger)
    (home / "mail" / "pending" / f"{q['sha']}.pdf").write_bytes(b"%PDF-1.4 other")
    with pytest.raises(mi.AnswerError):
        mi.answer(ledger, q["sha"], "NORD")
    assert docs_of(ledger) == [] and len(mi.questions(ledger)) == 1


def test_a_remembered_sender_answers_its_waiting_questions(ledger):
    mi.handle(ledger, message(("pdf", invoice(buyer=""))))
    mi.handle(ledger, message(("pdf", invoice(buyer="", number="Invoice number: INV-2")), mid="m2"))
    first, _ = mi.questions(ledger)
    mi.answer(ledger, first["sha"], "NORD", remember=True)
    assert mi.recheck(ledger) == 1
    assert mi.questions(ledger) == [] and len(docs_of(ledger, "NORD")) == 2


def test_two_connections_answering_at_once_file_one_document(ledger, home):
    mi.handle(ledger, message(("pdf", invoice(buyer=""))))
    (q,) = mi.questions(ledger)
    results, barrier = [], threading.Barrier(2)

    def go(who):
        L = Ledger()
        try:
            barrier.wait()
            results.append(mi.answer(L, q["sha"], who))
        except mi.AnswerError as e:
            results.append(str(e))
        finally:
            L.close()

    threads = [threading.Thread(target=go, args=(w,)) for w in ("NORD", "STUDIO9")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(isinstance(r, dict) for r in results) == [False, True]
    assert len(docs_of(ledger)) == 1
    assert json.loads(ledger.db.execute("SELECT answer FROM mail_questions").fetchone()[0])["who"] in (
        "NORD",
        "STUDIO9",
    )


@pytest.mark.parametrize(
    "label",
    [
        "do zapłaty",
        "Razem do zapłaty",
        "Zapłacono",
        "suma brutto",
        "fizetendő",
        "végösszeg",
        "k úhradě",
        "celkem",
        "celkom",
        "сума за плащане",
        "общо",
        "total a pagar",
        "importe total",
        "valor total",
        "te betalen",
        "totaal",
    ],
)
def test_eu_total_labels(label):
    assert mi.totals(f"{label}: 63.41 RON") == [("63.41", "RON")]


@pytest.mark.parametrize("net", ["netto", "Wartość netto", "Total netto", "suma netto"])
def test_polish_payable_total_excludes_net_and_vat_table(net):
    text = f"{net}: 40.00 RON\nRazem: 40.00 10.00 50.00\nRazem do zapłaty: 63.41 RON\nZapłacono: 63.41 RON\nSłownie do zapłaty: sześćdziesiąt trzy"
    assert mi.totals(text) == [("63.41", "RON")]


@pytest.mark.parametrize(
    "buyer,seller",
    [
        ("nabywca", "sprzedawca"),
        ("odbiorca", "sprzedawca"),
        ("vevő", "eladó"),
        ("odběratel", "dodavatel"),
        ("купувач", "доставчик"),
        ("comprador", "vendedor"),
        ("koper", "verkoper"),
    ],
)
def test_eu_party_labels(ledger, buyer, seller):
    assert mi.roles(f"{buyer}: Atelier Nord SRL", ["NORD"]) == {"NORD": "buyer"}
    assert mi.roles(f"{seller}: Atelier Nord SRL", ["NORD"]) == {"NORD": "seller"}
    assert mi.roles(f"{buyer}: Atelier Nord SRL\n{seller}: Atelier Nord SRL", ["NORD"]) == {"NORD": ""}


def test_polish_invoice_files_payable_not_vat_table(ledger):
    raw = text_pdf(
        [
            "FAKTURA",
            "Invoice SYN-PL-42",
            "Sprzedawca: Example Cloud",
            "Nabywca: Atelier Nord SRL",
            "Total netto: 40.00 RON",
            "Razem: 40.00 10.00 50.00",
            "Razem do zaplaty: 63.41 RON",
            "Zaplacono: 63.41 RON",
            "Paid by Visa ending 4242",
        ]
    )
    assert mi.handle(ledger, message(("pdf", raw))) == "filed:1"
    row = ledger.documents(company="NORD", kind="receipt")[0]
    assert (row["amount"], row["currency"], row["card_last4"]) == ("63.41", "RON", "4242")


@pytest.mark.parametrize(
    "line",
    [
        "Celkem bez DPH: 1000.00 CZK",
        "DPH celkem 210.00 CZK",
        "DPH: celkem 210.00 CZK",
        "DPH 21% celkem 210.00 CZK",
        "Celkom bez DPH 1000.00 EUR",
        "Celkom DPH 210.00 EUR",
        "Общо без ДДС 100.00 BGN",
        "ДДС общо 20.00 BGN",
        "Общо (ДДС): 20.00 BGN",
        "Общо 20% ДДС: 20.00 BGN",
        "Totaal BTW 21.00 EUR",
        "Totaal (BTW): 21.00 EUR",
        "Totaal 21% BTW: 21.00 EUR",
        "BTW totaal 21.00 EUR",
    ],
)
def test_local_net_or_vat_total_is_not_payable(line):
    assert mi.totals(line) == []


@pytest.mark.parametrize("label", ["Celkem s DPH", "Celkem vč. DPH", "Celkom s DPH", "Общо с ДДС", "Totaal incl. BTW"])
def test_local_gross_totals_are_not_excluded(label):
    assert mi.totals(f"{label}: 121.00 EUR") == [("121.00", "EUR")]


def test_czech_net_total_never_files_as_the_payable_amount(ledger):
    raw = text_pdf(
        [
            "INVOICE SYN-CZ-42",
            "Dodavatel: Example Cloud",
            "Odberatel: Atelier Nord SRL",
            "Celkem bez DPH 1000.00 CZK",
            "DPH 21% 210.00 CZK",
            "K zaplaceni 1210.00 CZK",
        ]
    )
    assert mi.handle(ledger, message(("pdf", raw))) == "asked:1"
    assert docs_of(ledger) == []


def test_suffix_free_city_in_another_buyers_address_is_not_ours(ledger):
    set_firms(companies.all() | {"NORD": replace(companies.get("NORD"), legal_name="Cluj S.R.L.", aliases=())})
    lines = [
        "INVOICE SYN-CITY-42",
        "Bill to",
        "Other Client SA",
        "Str. Exemplu 3",
        "Cluj",
        "Romania",
        "Total 12.10 EUR",
    ]
    assert not companies.get("NORD").named_in("\n".join(lines))
    assert mi.roles("\n".join(lines), ["NORD"]) == {"NORD": ""}
    assert mi.handle(ledger, message(("pdf", text_pdf(lines)))) == "asked:1"
    assert docs_of(ledger) == []


def test_totals_reader():
    text = "Total 1.234,56 EUR\nSubtotal 1.000,00 EUR\nTotal VAT 234,56 EUR\nAmount due 0,00 EUR\nTotal: $ 12.00"
    assert mi.totals(text) == [("1234.56", "EUR"), ("12.00", "USD")]
    assert mi.totals("Total de plată 100,00 lei") == [("100.00", "RON")]
    assert mi.totals("Total 50.00\nPaid in RON") == [("50.00", "RON")]
    assert mi.totals("Total 1\u00a0234,56 EUR") == [("1234.56", "EUR")]


def test_without_an_ai_integration_no_ai_is_asked(ledger, monkeypatch):
    from fiscalm import ai

    monkeypatch.setattr(ai, "available", lambda L: True)  # a CLI on the machine is not a configured integration

    def never(*a, **k):
        raise AssertionError("asked an AI that isn't configured")

    monkeypatch.setattr(ai, "ask_json", never)
    assert mi.handle(ledger, message(("pdf", invoice("Total paid 15.00 EUR")))) == "asked:1"


def test_a_firm_added_later_is_offered_for_a_document_without_a_firm(ledger):
    mi.handle(ledger, message(*(("pdf", invoice(buyer="", number=f"Invoice SYN-{i}")) for i in range(2))))
    gama = replace(companies.all()["STUDIO9"], key="GAMA", legal_name="Gama Servicii SRL", cui="66666666", accounts=())
    set_firms(companies.all() | {"GAMA": gama})
    with patch.object(companies, "all", wraps=companies.all) as registry:
        questions = mi.questions(ledger)
        assert registry.call_args_list == [call(ledger)]
    assert len(questions) == 2 and all(q["data"]["companies"] == ["NORD", "STUDIO9", "GAMA"] for q in questions)
    assert mi.answer(ledger, questions[0]["sha"], "GAMA")["company"] == "GAMA"


@pytest.mark.parametrize("live_mode", [False, True])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("source", ["mail", "history"])
def test_document_ai_respects_configuration_and_history_shadow_boundary(
    ledger, home, monkeypatch, live_mode, configured, source
):
    if live_mode:
        go_live(home)
    if configured:
        integrations.add(
            ledger, "ai", "Local AI", {"provider": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "test"}
        )
    calls = []

    def ask(L, system, prompt, schema, image=None):
        calls.append(prompt)
        if "doc_type" in schema["properties"]:
            return reading(total="15.00")(system, prompt, schema)
        return {"total": "15.00", "currency": "EUR"}

    monkeypatch.setattr(ai, "ask_json", ask)
    raw = invoice("Total paid 15.00 EUR")
    allowed = configured and (source == "mail" or live_mode)
    if source == "mail":
        assert mi.handle(ledger, message(("pdf", raw))) == ("filed:1" if configured else "asked:1")
    else:
        assert history_pdf.fields(ledger, raw, "NORD")["total"] == ("15.00" if allowed else "")
    assert len(calls) == int(allowed)
    if calls:
        assert "Total paid 15.00 EUR" in calls[0]


@pytest.mark.parametrize(
    "lines",
    [
        [
            "INVOICE",
            "Invoice number: S-1",
            "Seller: Atelier Nord SRL, VAT RO11111111",
            "Bill to: Foreign Customer Ltd",
            "Total 12.10 EUR",
        ],
        [
            "INVOICE S-2",
            "Seller                                   Bill to",
            "Atelier Nord SRL                         Foreign Customer Ltd",
            "Total 12.10 EUR",
        ],
        ["INVOICE S-3", "Furnizor: Atelier Nord SRL          Client: Foreign Customer Ltd", "Total 12.10 EUR"],
    ],
)
def test_the_firm_as_seller_is_never_its_purchase(ledger, lines):
    assert mi.handle(ledger, message(("pdf", text_pdf(lines)))) == "issued:1"
    assert docs_of(ledger) == [] and mi.questions(ledger) == []


@pytest.mark.parametrize(
    "lines,role",
    [
        (
            [
                f"{left:<45}{right}"
                for left, right in zip(
                    [
                        "Acme Cloud, Inc",
                        "1 Market Street",
                        "Suite 10",
                        "San Francisco",
                        "United States",
                        "support@acme.example",
                        "",
                        "",
                    ],
                    [
                        "Bill to",
                        "Atelier Nord SRL",
                        "Str. Exemplu 3",
                        "bl.2, ap.9",
                        "400000 Cluj",
                        "Romania",
                        "billing@example.ro",
                        "RO TIN 11111111",
                    ],
                    strict=True,
                )
            ],
            "buyer",
        ),
        (["Bill to", "Atelier Nord SRL", "", "footer", "footer", "footer", "Footer: Atelier Nord SRL"], "buyer"),
        (["Ship to", "Atelier Nord SRL"], ""),
        (["Seller", "Atelier Nord SRL"], "seller"),
        (["Furnizor", "Atelier Nord SRL"], "seller"),
        (["Bill to", "Atelier Nord SRL", "Seller", "Atelier Nord SRL"], ""),
        (["Bill to", "Atelier Nord SRL", "Ship to", "Atelier Nord SRL"], ""),
        (["Bill to", "Foreign Acme Ltd", "", "address", "address", "Atelier Nord SRL"], ""),
    ],
)
def test_party_blocks_and_unlabelled_mentions(ledger, lines, role):
    assert mi.roles("\n".join(lines), ["NORD"]) == {"NORD": role}
    pdf = text_pdf(["INVOICE BLOCK-42", *lines, "Total 12.10 EUR"])
    expected = {"buyer": "filed:1", "seller": "issued:1", "": "asked:1"}[role]
    assert mi.handle(ledger, message(("pdf", pdf))) == expected


@pytest.mark.parametrize(
    "label,printed,tax,role",
    [
        ("Bill to", "Invented Soft", "RO TIN 11111111", "buyer"),
        ("Bill to", "Foreign Cloud", "RO 1111 1111", "buyer"),
        ("Bill to", "Foreign Cloud", "CUI11111111", "buyer"),
        ("Bill to", "Foreign Cloud", "RO TIN 11111111", "buyer"),
        ("Bill to", "Foreign Cloud", "RO TIN 111111112", ""),
        ("Seller", "Invented Soft", "RO TIN 11111111", "seller"),
        ("Ship to", "Invented Soft", "RO TIN 11111111", ""),
    ],
)
def test_party_block_without_legal_form_or_alias(ledger, label, printed, tax, role):
    set_firms(companies.all() | {"NORD": replace(companies.get("NORD"), legal_name="Invented Soft S.R.L.", aliases=())})
    left = [
        "Example Cloud, Inc",
        "1 Market Street",
        "Suite 10",
        "San Francisco",
        "United States",
        "support@example.invalid",
        "",
        "",
    ]
    right = [label, printed, "Str. Exemplu 3", "bl.2, ap.9", "400000 Cluj", "Romania", "billing@example.invalid", tax]
    lines = [f"{a:<45}{b}" for a, b in zip(left, right, strict=True)]
    text = "\n".join(lines)
    assert mi.roles(text, ["NORD"]) == {"NORD": role}
    # Courier preserves the fixed column positions in the reported pdftotext layout.
    raw = text_pdf(["INVOICE LEGAL-42", *lines, "Total 12.10 EUR"], font="Courier")
    assert mi.read_pdf(raw).roles == {"NORD": role}
    assert mi.handle(ledger, message(("pdf", raw))) == {"buyer": "filed:1", "seller": "issued:1", "": "asked:1"}[role]


@pytest.mark.parametrize(
    "ending", ["400100 Cluj-Napoca", "VAT 400100.00 RON", "VAT 400100%", "VAT 400100 RON", "VAT 400100€"]
)
def test_deep_postal_code_or_tax_amount_is_not_a_buyer_tax_id(ledger, ending):
    set_firms(companies.all() | {"NORD": replace(companies.get("NORD"), cui="400100", aliases=())})
    text = "\n".join(["Bill to", "Other Client SA", *["Address line"] * 5, ending])
    assert mi.roles(text, ["NORD"]) == {"NORD": ""}


def test_city_below_an_already_named_buyer_is_not_ours_even_with_issuer_cui(ledger):
    set_firms(companies.all() | {"NORD": replace(companies.get("NORD"), legal_name="Cluj S.R.L.", aliases=())})
    lines = [
        "INVOICE CITY-43",
        "Cluj",
        "RO TIN 11111111",
        "",
        "Bill to: Other Client SA",
        "Cluj",
        "Romania",
        "Total 12.10 EUR",
    ]
    assert mi.roles("\n".join(lines), ["NORD"]) == {"NORD": ""}
    assert mi.handle(ledger, message(("pdf", text_pdf(lines)))) == "asked:1"


def test_suffix_free_seller_still_conflicts_with_full_legal_buyer(ledger):
    set_firms(companies.all() | {"NORD": replace(companies.get("NORD"), aliases=())})
    assert mi.roles("Bill to: Atelier Nord SRL\nSeller: Atelier Nord", ["NORD"]) == {"NORD": ""}


def test_unlabelled_address_column_does_not_inherit_inline_buyer(ledger):
    set_firms(companies.all() | {"NORD": replace(companies.get("NORD"), legal_name="Cluj S.R.L.", aliases=())})
    assert mi.roles("Bill to: Other Client SA      Address: Cluj", ["NORD"]) == {"NORD": ""}


def test_equidistant_party_labels_are_unclear(ledger):
    text = "Bill to: Atelier Nord SRL\n\nSeller    Bill to\n     Atelier Nord SRL"
    assert mi.roles(text, ["NORD"]) == {"NORD": ""}


@pytest.mark.parametrize("label", ["TIN", "Tax ID"])
def test_generic_tax_labels_are_not_romanian_history_ids(ledger, label):
    raw = text_pdf(
        [
            "INVOICE TEST-42",
            f"Seller: Example Cloud Inc, {label}: 77777777",
            "Bill to: Atelier Nord SRL",
            "Total 12.10 EUR",
        ]
    )
    assert history_pdf.fields(ledger, raw, "NORD")["party_tax_id"] == ""


@pytest.mark.parametrize(
    "body",
    [
        [f"{'':45}Bill to", f"{'Atelier Nord SRL':45}Foreign Acme Ltd", "", "Atelier Nord SRL - registered office"],
        [f"{'Bill to':45}From", f"{'Foreign Acme Ltd':45}Atelier Nord SRL"],
        ["Issuer: Atelier Nord SRL", "Customer: Foreign Acme Ltd", "c/o Atelier Nord SRL"],
        [
            "Bill to: Foreign Acme Ltd",
            "1 Market St",
            "Item consulting 100.00",
            "Total 100.00 EUR",
            "Payment terms 30 days",
            "Pay to Atelier Nord SRL",
        ],
        [
            "Customer",
            "Foreign Acme Ltd",
            "Description Qty",
            "Consulting 1",
            "Thank you for working with Atelier Nord SRL",
        ],
        ["Bill to: Foreign Acme", "Remit to Atelier Nord SRL"],
        ["Customer: Foreign Acme Ltd", *["1 Market St"] * 6, "Atelier Nord SRL"],
    ],
)
def test_third_party_buyer_blocks_never_make_our_letterhead_a_purchase(ledger, body):
    raw = text_pdf(["INVOICE SELF-42", "Atelier Nord SRL", "", *body, "Total 100.00 EUR"])
    assert mi.read_pdf(raw).roles.get("NORD") != "buyer"
    assert mi.handle(ledger, message(("pdf", raw))) != "filed:1"


def test_same_line_buyer_and_seller_mentions_are_unclear(ledger):
    lines = ["INVOICE CONFLICT-42", "Bill to: Atelier Nord SRL      Seller: Atelier Nord SRL", "Total 12.10 EUR"]
    assert mi.roles("\n".join(lines), ["NORD"]) == {"NORD": ""}
    assert mi.handle(ledger, message(("pdf", text_pdf(lines)))) == "asked:1"


def test_the_firm_beside_a_seller_on_the_same_line_is_the_buyer(ledger):
    pdf = text_pdf(["INVOICE B-1", "Seller: Example Cloud Inc      Bill to: Atelier Nord SRL", "Total 12.10 EUR"])
    assert mi.handle(ledger, message(("pdf", pdf))) == "filed:1"


def test_a_firm_named_without_a_role_is_asked(ledger):
    pdf = text_pdf(["INVOICE U-1", "Atelier Nord SRL", "Total 12.10 EUR"])
    assert mi.handle(ledger, message(("pdf", pdf))) == "asked:1"
    (q,) = mi.questions(ledger)
    assert q["data"]["reason"] == "buyer-unclear" and q["data"]["companies"] == ["NORD"]


@pytest.mark.parametrize("total", ["Total -12.10 EUR", "Total (12.10) EUR", "Total (EUR 12.10)"])
def test_a_negative_total_is_a_credit(ledger, total):
    assert mi.handle(ledger, message(("pdf", invoice(total=total)))) == "credit-note:1"


def test_a_line_with_two_currencies_gives_no_total(ledger):
    assert mi.totals("Total 10.00 EUR 50.00 RON") == []


def test_ubl_tax_identities_are_compared_whole(ledger):
    foreign = UBL.format(country="DE", supplier="DE123456789").replace("RO11111111", "DE11111111")
    assert mi.handle(ledger, message(("xml", foreign.encode()))) == "asked:1"  # a foreign id beside the firm's name
    assert mi.questions(ledger)[0]["data"]["reason"] == "buyer-unclear"
    ro = UBL.format(country="", supplier="RO22222222").replace(
        "<cac:PostalAddress><cac:Country><cbc:IdentificationCode></cbc:IdentificationCode></cac:Country></cac:PostalAddress>",
        "",
    )
    assert mi.handle(ledger, message(("xml", ro.encode()), mid="ro2")) == "efactura:1"
    own = UBL.format(country="DE", supplier="RO33333333")
    assert mi.handle(ledger, message(("xml", own.encode()), mid="own")) == "issued:1"  # STUDIO9 sold it


def test_an_ai_never_reads_over_the_ubl_of_its_pdf(ledger):
    foreign = UBL.format(country="DE", supplier="DE123456789").replace("RO11111111", "DE11111111").encode()
    pdf = text_pdf(["Rechnung DE-77", "Atelier Nord SRL", "Gesamtbetrag 49,00 EUR"])
    ask = reading(total="49.00")
    assert mi.handle(ledger, message(("xml", foreign), ("pdf", pdf)), ask=ask) == "asked:1,same-as-pdf:1"
    assert ask.calls == []


def test_a_pdf_that_disagrees_with_its_ubl_is_judged_on_its_own(ledger):
    xml = UBL.format(country="DE", supplier="DE123456789").encode()
    pdf = text_pdf(["Rechnung", "Ref. DE-77", "Bill to: Studio 9 SRL", "Gesamtbetrag 12,00 EUR"])
    out = mi.handle(ledger, message(("pdf", pdf), ("xml", xml)))
    assert "same-as-pdf" not in out
    assert {d["company"]: d["amount"] for d in docs_of(ledger)} in ({"NORD": "49.00", "STUDIO9": "12.00"},)


def test_numberless_copies_without_a_date_are_one_document(ledger):
    a = text_pdf(["RECEIPT", "Bill to: Atelier Nord SRL", "Total 9.99 EUR"])
    b = text_pdf(["RECEIPT", "Bill to: Atelier Nord SRL", "Total 9.99 EUR", "Thank you"])
    assert mi.handle(ledger, message(("pdf", a))) == "filed:1"
    assert mi.handle(ledger, message(("pdf", b), mid="m2")) == "duplicate:1"


def test_an_answer_and_its_filing_commit_together(ledger, monkeypatch):
    mi.handle(ledger, message(("pdf", invoice(buyer=""))))
    (q,) = mi.questions(ledger)

    real = mi._insert

    def crash(*a):
        raise OSError("disk full")

    monkeypatch.setattr(mi, "_insert", crash)
    with pytest.raises(OSError):
        mi.answer(ledger, q["sha"], "NORD", remember=True)
    assert len(mi.questions(ledger)) == 1 and docs_of(ledger) == []
    assert ledger.db.execute("SELECT COUNT(*) FROM mail_senders").fetchone()[0] == 0
    monkeypatch.setattr(mi, "_insert", real)
    assert mi.answer(ledger, q["sha"], "NORD")["status"] == "filed"


def test_a_delivery_address_never_makes_the_firm_the_buyer(ledger):
    pdf = text_pdf(["INVOICE D-1", "Bill to: Foreign Customer Ltd", "Ship to: Atelier Nord SRL", "Total 12.10 EUR"])
    assert mi.handle(ledger, message(("pdf", pdf))) == "asked:1"
    assert mi.questions(ledger)[0]["data"]["reason"] == "buyer-unclear"


def test_a_pdf_naming_the_firm_as_seller_never_pairs_with_a_ubl(ledger):
    xml = UBL.format(country="DE", supplier="DE123456789").encode()
    pdf = text_pdf(["INVOICE DE-77", "Seller: Atelier Nord SRL", "Total 49.00 EUR"])
    out = mi.handle(ledger, message(("pdf", pdf), ("xml", xml)))
    assert "same-as-pdf" not in out and "issued:1" in out


def test_a_numeric_supplier_id_with_a_foreign_country_is_not_romanian(ledger):
    xml = UBL.format(country="DE", supplier="123456789").encode()
    assert mi.handle(ledger, message(("xml", xml))) == "filed:1"
    bare = UBL.format(country="", supplier="22222222").replace(
        "<cac:PostalAddress><cac:Country><cbc:IdentificationCode></cbc:IdentificationCode></cac:Country></cac:PostalAddress>",
        "",
    )
    assert mi.handle(ledger, message(("xml", bare.encode()), mid="bare")) == "efactura:1"


def test_remembering_the_same_firm_settles_an_unclear_role(ledger):
    unclear = lambda n: text_pdf([f"INVOICE R-{n}", "Atelier Nord SRL", f"Total 1{n}.00 EUR"])  # noqa: E731
    mi.handle(ledger, message(("pdf", unclear(1))))
    mi.handle(ledger, message(("pdf", unclear(2)), mid="m2"))
    first, second = mi.questions(ledger)
    mi.answer(ledger, first["sha"], "NORD")
    ledger.db.execute("INSERT INTO mail_senders VALUES('billing@cloud.example','STUDIO9','ana','t')")
    ledger.db.commit()
    assert mi.recheck(ledger) == 0 and len(mi.questions(ledger)) == 1  # another firm for this sender: still asked
    ledger.db.execute("UPDATE mail_senders SET company='NORD'")
    ledger.db.commit()
    assert mi.recheck(ledger) == 1 and mi.questions(ledger) == []


def test_two_questions_for_one_invoice_answered_at_once_file_it_once(ledger):
    a = text_pdf(["INVOICE Q-9", "Total 30.00 EUR"])
    b = text_pdf(["INVOICE Q-9", "Total 30.00 EUR", "Copy"])
    mi.handle(ledger, message(("pdf", a)))
    mi.handle(ledger, message(("pdf", b), mid="m2"))
    qa, qb = mi.questions(ledger)
    results, barrier = [], threading.Barrier(2)

    def go(sha):
        L = Ledger()
        try:
            barrier.wait()
            results.append(mi.answer(L, sha, "NORD")["status"])
        finally:
            L.close()

    threads = [threading.Thread(target=go, args=(q["sha"],)) for q in (qa, qb)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["duplicate", "filed"] and len(docs_of(ledger)) == 1


@pytest.mark.parametrize(
    "sender,names,want",
    [
        (
            "EmailExtrase@btrl.ro",
            ["notice.pdf", "ALLMERGE_1.pdf"],
            ("statement", ["ALLMERGE_1.pdf"]),
        ),  # BT: only allmerge
        ("ops@accountant.example", ["op12.pdf", "factura.pdf"], ("op", ["op12.pdf"])),
        ("Office <evil-ops@accountant.example>", ["op12.pdf"], None),  # exact addresses only
    ],
)
def test_statement_and_accountant_mail_go_to_their_intake(ledger, sender, names, want):
    ledger.set_meta("accountant:NORD", json.dumps({"cui": "", "senders": ["ops@accountant.example"], "recipient": ""}))
    import email.utils

    address = email.utils.parseaddr(sender)[1].lower()
    msg = imapread.Message(
        "mail-00000001",
        "k@x",
        sender,
        address,
        "s",
        "2026-11-02",
        tuple(imapread.Attachment("pdf", PDF_BYTES + n.encode(), n) for n in names),
    )
    got = mi.known(ledger, msg)
    assert (got and (got[0], [a.name for a in got[1]])) == (want or None)


def test_a_statement_not_readable_yet_is_read_again(ledger, monkeypatch):
    from fiscalm import recon

    monkeypatch.setattr(recon, "ingest_statement", lambda *a, **k: {"status": "error"})
    msg = imapread.Message(
        "mail-00000001",
        "k@x",
        "EmailExtrase@btrl.ro",
        "emailextrase@btrl.ro",
        "s",
        "2026-11-02",
        (imapread.Attachment("pdf", PDF_BYTES, "allmerge.pdf"),),
    )
    with pytest.raises(RuntimeError):  # imapread keeps it to retry
        mi.handle(ledger, msg)


def test_history_reader_reuses_fields_but_accepts_romanian_parties(ledger):
    raw = text_pdf(
        [
            "FACTURA TEST-42",
            "Data: 03.11.2023",
            "Furnizor: Furnizor Exemplu SRL",
            "CUI: RO77777777",
            "Client: Atelier Nord SRL",
            "CUI: RO11111111",
            "Total 119.00 RON",
        ]
    )
    assert mi.read_pdf(raw).refused == "efactura"
    doc = mi.read_pdf(raw, allow_ro_supplier=True)
    assert not doc.refused
    assert (doc.number, doc.date, doc.roles) == ("TEST-42", "2023-11-03", {"NORD": "buyer"})
    assert mi.pdf_party(doc.text, "seller") == ("Furnizor Exemplu SRL", "77777777")
    assert mi.pdf_total(ledger, doc) == ("119.00", "RON")


def test_history_reader_refuses_ambiguous_party_blocks():
    text = "Furnizor: Firma Unu\nCUI: RO77777777\nCUI: RO88888888\nClient: Atelier Nord SRL"
    assert mi.pdf_party(text, "seller") == ("", "")


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Furnizor: Firma Unu\nCUI: RO77777777\nClient: Persoana Fizica\nStr. X 2\nTotal 10.00 RON\nSoftware Exemplu CIF 22222222",
            ("", ""),
        ),
        ("Furnizor: Firma Unu SRL CUI: RO77777777\nStr. Exemplu 1, Cluj", ("Firma Unu SRL", "77777777")),
        ("Furnizor: Firma Unu SRL       Client: Firma Doi SRL\nCUI: RO77777777       CUI: RO88888888", ("", "")),
    ],
)
def test_history_party_block_boundaries(text, expected):
    role = "buyer" if "Persoana" in text else "seller"
    assert mi.pdf_party(text, role) == expected


@pytest.mark.parametrize(
    "title,expected", [("Invoice", "invoice"), ("Bon fiscal", "receipt"), ("Receipt for invoice", "receipt")]
)
@pytest.mark.parametrize("card", [None, "4242"])
@pytest.mark.parametrize("legacy", [False, True])
def test_mailed_document_classification_follows_content(ledger, title, expected, card, legacy):
    raw = invoice(f"Paid by Visa ending {card}" if card else "", title=title, number="Invoice reference: SYN-TYPE-9")
    assert mi.handle(ledger, message(("pdf", raw))) == "filed:1"
    sha = hashlib.sha256(raw).hexdigest()
    if legacy:
        with ledger.tx() as db:
            db.execute("DELETE FROM meta WHERE key=?", ("document-type:" + sha,))
    with (
        closing(Ledger(ledger.path, readonly=True)) as reader,
        patch.object(mi, "document_type", wraps=mi.document_type) as classify,
    ):
        doc = api._documents(reader, "NORD")[0]
        assert api._documents(reader, "NORD")[0]["document_type"] == expected
        assert classify.call_count == int(legacy)  # Older files are parsed once, even through read-only projections.
    assert (doc["document_type"], doc["card"]) == (expected, card)
    part = {"invoices": [], "documents": [doc], "expenses": [{"invoice": None, "document": sha}]}
    assert len(list(browse._purchases(part, False))) == (expected == "invoice")
    assert len(list(browse._purchases(part, True))) == (expected == "receipt")


@pytest.mark.parametrize("legacy", [False, True])
def test_ubl_pdf_type_survives_a_pending_answer(ledger, legacy):
    xml = UBL.format(country="DE", supplier="DE123456789").replace("RO11111111", "DE999999999").encode()
    pdf = text_pdf(["Reference DE-77", "Atelier Nord SRL", "Gesamtbetrag 49,00 EUR"])
    assert mi.handle(ledger, message(("xml", xml), ("pdf", pdf))) == "asked:1,same-as-pdf:1"
    [q] = mi.questions(ledger)
    if legacy:
        data = q["data"]
        data.pop("fiscal_type", None)
        with ledger.tx() as db:
            db.execute("UPDATE mail_questions SET data=? WHERE sha=?", (json.dumps(data), q["sha"]))
    assert mi.answer(ledger, q["sha"], "NORD")["status"] == "filed"
    assert api._documents(ledger, "NORD")[0]["document_type"] == "invoice"


@pytest.mark.parametrize("legacy", [False, True])
def test_mailed_ubl_is_an_invoice(ledger, legacy):
    raw = UBL.format(country="DE", supplier="DE123456789").encode()
    assert mi.handle(ledger, message(("xml", raw))) == "filed:1"
    if legacy:
        with ledger.tx() as db:
            db.execute("DELETE FROM meta WHERE key=?", ("document-type:" + hashlib.sha256(raw).hexdigest(),))
    assert api._documents(ledger, "NORD")[0]["document_type"] == "invoice"


@pytest.mark.parametrize("label", ["Invoice reference", "Invoice no.", "Invoice #", "Document number", "Nr. factură"])
def test_invoice_number_labels(ledger, label):
    assert mi.NUMBER.search(f"{label}: SYN-A1234")[1] == "SYN-A1234"
    printed_label = label.replace("ă", "a")  # The minimal WinAnsi PDF builder cannot encode ă.
    raw = invoice(number=f"{printed_label}: SYN-A1234", total="Total 12.10 RON")
    assert mi.read_pdf(raw).number == "SYN-A1234"
    assert mi.handle(ledger, message(("pdf", raw))) == "filed:1"
    assert ledger.db.execute("SELECT number FROM mail_documents").fetchone()[0] == "SYN-A1234"


@pytest.mark.parametrize("number", ["NRD-0042", "NORD0042", "A-0042"])
def test_number_keeps_series_beginning_with_number_label(number):
    assert mi.read_pdf(text_pdf([f"FACTURA {number}"])).number == number


@pytest.mark.parametrize("remembered", [False, True])
def test_inbox_scope_refuses_other_firms_and_pending_answers(ledger, remembered):
    integrations.update(ledger, "mail-00000001", {}, firm_scope=["STUDIO9"])
    if remembered:
        with ledger.tx() as db:
            db.execute("INSERT INTO mail_senders VALUES('billing@cloud.example','NORD','test','2026-11-03')")
    assert mi.handle(ledger, message(("pdf", invoice()))) == "asked:1"
    [question] = mi.questions(ledger)
    assert question["data"]["companies"] == []
    with pytest.raises(mi.AnswerError):
        mi.answer(ledger, question["sha"], "NORD", remember=True)
    assert not docs_of(ledger)
    integrations.update(ledger, "mail-00000001", {}, firm_scope="all")
    # Explicit scope questions keep their document's firm, not a guessed replacement.
    assert mi.questions(ledger)[0]["data"]["companies"] == ["NORD"]


def test_scope_is_rechecked_inside_the_filing_transaction(ledger, monkeypatch):
    original = mi._store

    def narrow(*args):
        path = original(*args)
        other = Ledger(ledger.path)
        try:
            integrations.update(other, "mail-00000001", {}, firm_scope=[])
        finally:
            other.close()
        return path

    monkeypatch.setattr(mi, "_store", narrow)
    with pytest.raises(mi.AnswerError):
        mi.handle(ledger, message(("pdf", invoice())))
    assert not docs_of(ledger)


@pytest.mark.parametrize("kind", ["statement", "op"])
def test_statements_and_ops_outside_inbox_scope_become_questions(ledger, monkeypatch, kind):
    from types import SimpleNamespace

    integrations.update(ledger, "mail-00000001", {}, firm_scope=["STUDIO9"])
    msg = message(("pdf", PDF_BYTES))
    monkeypatch.setattr(mi, "known", lambda *args: (kind, list(msg.attachments), ["NORD"]))
    if kind == "statement":
        monkeypatch.setattr(statements, "decrypt_to", lambda path, *a, **kw: ("NORD", "2026-11", path))

        def parse(*args):
            assert not ledger.db.in_transaction  # decryption/parsing never holds the app write lock
            return statements.Statement("NORD", date(2026, 11, 1), date(2026, 11, 30), [])

        monkeypatch.setattr(statements, "parse", parse)
    else:
        monkeypatch.setattr(
            recon, "parse_payment_pdf", lambda *a: SimpleNamespace(ok=True, item=SimpleNamespace(company="NORD"))
        )
    assert "refused" in mi.handle(ledger, msg)
    assert len(mi.questions(ledger)) == 1
    assert not ledger.documents() and not ledger.payments()


def test_dismissing_scope_question_does_not_suppress_a_sender_in_other_inboxes(ledger):
    integrations.update(ledger, "mail-00000001", {}, firm_scope=["STUDIO9"])
    mi.handle(ledger, message(("pdf", invoice())))
    question = mi.questions(ledger)[0]
    mi.answer(ledger, question["sha"], "none", remember=True)
    assert mi.sender_firm(ledger, "billing@cloud.example") == (False, None)
    integrations.update(ledger, "mail-00000001", {}, firm_scope="all")
    assert mi.handle(ledger, message(("pdf", invoice(number="Invoice SYN-OTHER")), mid="other")) == "filed:1"


@pytest.mark.parametrize(
    "title,buyer,expected",
    [
        ("INVOICE", "Seller: Atelier Nord SRL, VAT RO11111111", "issued:1"),
        ("Newsletter", "Atelier Nord SRL, VAT RO11111111", "not-invoice:1"),
    ],
)
def test_scope_preserves_existing_document_refusals(ledger, title, buyer, expected):
    integrations.update(ledger, "mail-00000001", {}, firm_scope=["STUDIO9"])
    assert mi.handle(ledger, message(("pdf", invoice(title=title, buyer=buyer, number="Reference SYN-42")))) == expected
    assert not mi.questions(ledger) and not docs_of(ledger)
