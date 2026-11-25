"""A synthetic fiscalm world (fake companies, fake suppliers and clients, fake IBANs) for the API tests and the app.

  PYTHONPATH=src python tests/synthetic_world.py [out dir]   # writes the app's fixtures (default app/fixtures)

The fixtures are generated, never committed (`npm run fixtures` in app/ runs this). They are the same on every run:
the world is built around a fixed day, NOW, with fiscalm's clock pinned to it (pin_clock), for the tests too, so
they pass on any day of the month.

Never real data: the company config is synthetic ledger fixtures and every value below is made up.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from xml.sax.saxutils import escape

from ubl_fake import ubl

from fiscalm import companies, dashboard, integrations as ig
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.ledger import Ledger, finance_home
from fiscalm.payments import engine as pay
from fiscalm.products import engine as products

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 27, 9, 30)

# Mailed invoices (fiscalm.mail.invoices): one filed for NORD, one in doubt (no firm on it). MAIL_KEY is its digest.
CLOUD_PDF_LINES = [
    "INVOICE",
    "Invoice number: CL-2026-1042",
    "Date: 2026-10-20",
    "Bill to: Atelier Nord SRL, VAT RO11111111",
    "Subtotal 40.50 EUR",
    "VAT 8.51 EUR",
    "Total 49.01 EUR",
]
AIR_PDF_LINES = [
    "RECEIPT",
    "Booking reference: QX7PL2",
    "Date: 2026-10-24",
    "Passenger: Ana Exemplu",
    "Total paid 249.90 EUR",
]


# A received invoice with more lines than the sheet shows at first (invented products and prices).
MAINTENANCE_LINES = [
    ("Mentenanță preventivă stații de lucru", "8", "HUR", "75.00", "600.00", "21"),
    ("Configurare rețea și firewall", "3", "HUR", "90.00", "270.00", "21"),
    ("Înlocuire SSD 1 TB", "2", "H87", "95.00", "190.00", "21"),
    ("Manual de administrare a rețelelor", "1", "H87", "108.65", "108.65", "11"),
    ("Cablu de rețea Cat6", "15", "MTR", "2.00", "30.00", "21"),
    ("Deplasare la sediul clientului", "1", "H87", "25.00", "25.00", "21"),
    (
        "Mentenanță lunară a serverului de fișiere: actualizări de securitate, verificarea copiilor de rezervă "
        "și raport de stare",
        "1",
        "H87",
        "25.00",
        "25.00",
        "21",
    ),
]


def _mail_key() -> str:
    import hashlib

    from pdf_fake import text_pdf

    return hashlib.sha256(text_pdf(AIR_PDF_LINES)).hexdigest()


MAIL_KEY = _mail_key()


def _iban(bban: str) -> str:
    n = int("".join(str(int(c, 36)) for c in bban + "RO00"))
    return f"RO{98 - n % 97:02d}{bban}"


def _ef(
    L,
    mid,
    co,
    supplier,
    cui,
    number,
    issue: date,
    due: date | None,
    total,
    currency="RON",
    created: datetime | None = None,
    **kw,
):
    iban = _iban(f"BTRL{int(mid):016d}")
    L.db.execute(
        "INSERT INTO efactura(message_id,company,doc_type,number,issue_date,due_date,currency,total,supplier_name,"
        "supplier_cui,supplier_iban,bic,ibans,issues,raw_path,xml_sha,parse_error,status,paid_at,paid_note,paid_tx,"
        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            mid,
            co,
            "invoice",
            number,
            issue.isoformat(),
            due.isoformat() if due else None,
            currency,
            total,
            supplier,
            cui,
            iban,
            "",
            json.dumps([iban]),
            "[]",
            f"/nonexistent/efactura/{mid}.zip",
            f"sha-ef-{mid}",
            kw.get("parse_error"),
            kw.get("status", "open"),
            kw.get("paid_at"),
            kw.get("paid_note"),
            kw.get("paid_tx"),
            (created or datetime.combine(issue, datetime.min.time()) + timedelta(hours=7)).isoformat(
                timespec="seconds"
            ),
        ),
    )
    L.db.commit()


def _issued(
    L, mid, co, number, client, cui, issue: date, due: date | None, total, currency="RON", doc_type="invoice", ref=None
):
    L.add_issued(
        {
            "message_id": mid,
            "company": co,
            "upload_id": f"u{mid}",
            "listed_at": issue.strftime("%Y%m%d0900"),
            "raw_path": f"/nonexistent/emise/{mid}.zip",
            "doc_type": doc_type,
            "number": number,
            "issue_date": issue.isoformat(),
            "due_date": due.isoformat() if due else None,
            "currency": currency,
            "total": total,
            "client_name": client,
            "client_cui": cui,
            "billing_ref": ref,
            "xml_sha": f"sha-is-{mid}",
            "issues": "[]",
            "status": "credit" if doc_type == "credit_note" else "open",
        }
    )


def ubl_lines(rows: list[tuple], root: str = "Invoice") -> str:
    """UBL line elements for (description, quantity, unit code, unit price, net, VAT %) rows."""
    qty = "InvoicedQuantity" if root == "Invoice" else "CreditedQuantity"
    return "".join(
        f'<cac:{root}Line><cbc:ID>{n}</cbc:ID><cbc:{qty} unitCode="{unit}">{q}</cbc:{qty}>'
        f"<cbc:LineExtensionAmount>{net}</cbc:LineExtensionAmount><cac:Item><cbc:Name>{name}</cbc:Name>"
        f"<cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>{vat}</cbc:Percent></cac:ClassifiedTaxCategory>"
        f"</cac:Item><cac:Price><cbc:PriceAmount>{price}</cbc:PriceAmount></cac:Price></cac:{root}Line>"
        for n, (name, q, unit, price, net, vat) in enumerate(rows, 1)
    )


def ubl_totals(rows: list[tuple], currency: str = "RON") -> str:
    """TaxTotal (one subtotal per VAT rate) and LegalMonetaryTotal for ubl_lines rows, rounded per rate."""
    with localcontext() as ctx:
        ctx.prec = 60
        bases: dict[str, Decimal] = {}
        for *_, net, vat in rows:
            bases[vat] = bases.get(vat, Decimal(0)) + Decimal(net)
        taxes = {r: (n * Decimal(r) / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) for r, n in bases.items()}
        net, vat = sum(bases.values()), sum(taxes.values())
    cur = f'currencyID="{currency}"'
    subtotals = "".join(
        f"<cac:TaxSubtotal><cbc:TaxableAmount {cur}>{bases[r]}</cbc:TaxableAmount><cbc:TaxAmount {cur}>{taxes[r]}"
        f"</cbc:TaxAmount><cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>{r}</cbc:Percent></cac:TaxCategory>"
        "</cac:TaxSubtotal>"
        for r in bases
    )
    return (
        f"<cac:TaxTotal><cbc:TaxAmount {cur}>{vat}</cbc:TaxAmount>{subtotals}</cac:TaxTotal><cac:LegalMonetaryTotal>"
        f"<cbc:TaxExclusiveAmount {cur}>{net}</cbc:TaxExclusiveAmount><cbc:TaxInclusiveAmount {cur}>{net + vat}"
        f"</cbc:TaxInclusiveAmount><cbc:PayableAmount {cur}>{net + vat}</cbc:PayableAmount></cac:LegalMonetaryTotal>"
    )


def store_lines(L, home: Path, table: str, mid: str, co: str, rows: list[tuple], currency: str = "RON") -> None:
    """Keep a UBL ZIP with these lines and their totals for an invoice built straight into the ledger; its header
    stays as built."""

    xml = (
        '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" '
        'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
        'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
        f"<cbc:DocumentCurrencyCode>{currency}</cbc:DocumentCurrencyCode>{ubl_totals(rows, currency)}"
        f"{ubl_lines(rows)}</Invoice>"
    )
    path = home / "efactura" / co / "2026-10" / f"{mid}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{mid}.xml", xml)
    L.db.execute(f"UPDATE {table} SET raw_path=? WHERE message_id=?", (str(path), mid))
    L.db.commit()


def _op(L, n, co, amount, issue: date, kind="op", details="CAS"):
    from fiscalm.payments.orders import PaymentItem

    if kind == "efactura":
        item = PaymentItem(
            co,
            "efactura",
            "ACCOUNTANT SRL",
            _iban("CECE0000000000000001"),
            "CECEROBU",
            "",
            Decimal(amount),
            f"Factura AA {n}",
            issue,
            f"AA {n}",
            "",
            f"sha-aa-{co}-{n}",
        )
    else:
        item = PaymentItem(
            co,
            "op",
            "BUGETUL GENERAL CONSOLIDAT",
            _iban(f"TREZ{n:016d}"),
            "TREZROBU",
            "11111111" if co == "NORD" else "33333333",
            Decimal(amount),
            details,
            issue,
            f"OP {n} {issue:%d/%m/%Y}",
            "",
            f"sha-op-{co}-{n}",
        )
    assert L.add_payment(item)
    return item.item_id


def _lose(L, co, ids, today: date, now: datetime):
    """Rows of an old payment file that is gone from the folder: imported or not? The user must say."""

    gone = pay.to_pay_dir() / f"payments-{co}-{today:%Y-%m}-1.csv"
    with L.tx() as db:
        for i in ids:
            db.execute(
                "UPDATE payments SET status='exported', csv_path=?, exported_at=? WHERE item_id=?",
                (str(gone), now.isoformat(timespec="seconds"), i),
            )


def _imported(L, co, ids, day: date):
    """Rows of a file the user imported on `day`: its archive copy in <CO>/plati."""
    from fiscalm.ledger import finance_home

    archive = finance_home() / co / "plati" / f"payments-{co}-{day}.csv"
    with L.tx() as db:
        for i in ids:
            db.execute("UPDATE payments SET status='exported', csv_path=? WHERE item_id=?", (str(archive), i))


def _view(co, mo, months):
    """recon.month_view without a statement PDF."""
    m1, m2 = months[1], months[2]
    base = {
        "company": co,
        "month": mo,
        "statement": False,
        "balanced": None,
        "matched": [],
        "missing": [],
        "domestic": 0,
        "advances": [],
    }

    def row(d, who, amt, orig="", doc="factura.pdf"):
        return {
            "date": d,
            "who": who,
            "kind": "card",
            "amount": amt,
            "orig": orig,
            "doc": doc,
        }  # noqa: E731

    if co == "NORD" and mo == m1:
        return base | {
            "statement": True,
            "balanced": True,
            "domestic": 24,
            "matched": [row(f"{m1}-0{i}", "HETZNER ONLINE", "45.10", "9.00 EUR", "Hetzner.pdf") for i in range(2, 7)],
            "missing": [
                {
                    "date": f"{m1}-22",
                    "who": "NIMBUS HOSTING INC",
                    "kind": "card",
                    "amount": "173.45",
                    "orig": "37.00 USD",
                },
                {
                    "date": f"{m1}-25",
                    "who": "Exemplu Imobiliare SRL",
                    "kind": "transfer",
                    "amount": "1200.00",
                    "orig": "",
                },
            ],
            "advances": [{"date": f"{m1}-10", "amount": "500.00"}],
        }
    if mo == m2:  # STUDIO9's totals don't match the bank's
        return base | {
            "statement": True,
            "balanced": co == "NORD",
            "domestic": 20 if co == "NORD" else 6,
            "matched": [row(f"{m2}-03", "GITHUB", "19.80", "4.00 USD", "GitHub.pdf")],
        }
    return base


class FixedDate(date):
    """`date` whose today() is the scenario's day: the modules that date files by the real clock see the same day as
    the world, so the tests don't depend on the calendar (on the 1st, "4 days ago" is last month)."""

    day: date = NOW.date()

    @classmethod
    def today(cls) -> date:
        return cls.day


def pin_clock(patch, now: datetime) -> None:
    """Every real-clock read in fiscalm that the scenario reaches answers `now`'s day."""
    from fiscalm import ledger
    from fiscalm.efactura import inbox as efactura
    from fiscalm.invoicing import issued

    patch(ledger, "now", lambda: now.isoformat(timespec="seconds"))
    FixedDate.day = now.date()
    for mod in (pay, efactura, issued):
        patch(mod, "date", FixedDate)


def build(L, home: Path, patch, now: datetime | None = None, *, integrations_configured: bool = True) -> dict:
    """Fill the ledger in `home` with every case the app shows, around `now` (default: NOW, the clock pinned to it).
    `patch(obj, name, value)` replaces the outside world (ANAF token, Gmail mirror, statement parsing, mail preview,
    the API's clock). Returns the keys the tests need."""
    from fiscalm import companies
    from fiscalm.api import state as api
    from fiscalm.bank import statements
    from fiscalm.efactura import client as anaf, inbox as efactura

    now = now or NOW
    pin_clock(patch, now)
    for co, cui in {"NORD": "1022", "STUDIO9": "2003"}.items():
        if co in companies.all():
            L.set_meta(
                f"accountant:{co}",
                json.dumps(dict(cui=cui, senders=["ops@accountant.example"], recipient="ops@accountant.example")),
            )
    today = now.date()
    patch(api, "clock", lambda: now)
    months = api.months_back(2, today)
    m1, m2 = months[1], months[2]
    d1 = date.fromisoformat(f"{m1}-01")
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "LIVE").touch()
    ago = lambda days: today - timedelta(days=days)  # noqa: E731
    soon = lambda days: today + timedelta(days=days)  # noqa: E731

    # NORD received: an overdue, a due-soon and an EUR invoice, still open
    _ef(L, "101", "NORD", "Energie Furnizare SA", "RO1001", "EF-55120", ago(15), ago(3), "386.40")
    _ef(L, "102", "NORD", "Telecom Rapid SRL", "RO1002", "TR-2210", ago(12), soon(2), "120.00")
    store_lines(
        L,
        home,
        "efactura",
        "102",
        "NORD",
        [
            ("Abonament internet fibră, octombrie", "1", "H87", "74.37", "74.37", "21"),
            ("Abonament telefonie mobilă", "2", "H87", "12.40", "24.80", "21"),
        ],
    )
    _ef(L, "103", "NORD", "Software Licente SRL", "RO1003", "SL-0091", ago(5), soon(10), "20.00", "EUR")
    # A layout stress case, not a plausible invoice: the longest amount and a long description the sheet accepts.
    long = "123456789012345678.123456"
    name = "Abonament anual la platforma de proiectare colaborativă, licență pentru echipă, cu suport prioritar"
    store_lines(L, home, "efactura", "103", "NORD", [(name, "1", "H87", long, long, "21")], "EUR")
    # Imported and open named batches are attached below, after their proof data is seeded.
    _ef(L, "104", "NORD", "Curierat Expres SRL", "RO1004", "CE-7781", d1 + timedelta(days=4), soon(5), "595.00")
    efactura.approve(L, "104", origin="person")
    _op(L, 1, "NORD", "1250.00", d1 + timedelta(days=19))
    # A downloaded batch whose membership changes before import.
    _ef(L, "105", "NORD", "Servicii Web SRL", "RO1005", "SW-1203", ago(9), soon(6), "871.00")
    efactura.approve(L, "105", origin="person")
    _op(L, 2, "NORD", "3200.00", d1 + timedelta(days=19), details="TVA")
    _op(L, 3, "NORD", "800.00", d1 + timedelta(days=12), kind="efactura")
    # a payment file gone from the folder, and rows held until the user answers about it
    _ef(L, "106", "NORD", "Papetarie Birou SRL", "RO1006", "PB-0412", ago(20), ago(1), "64.90")
    efactura.approve(L, "106", origin="person")
    lost = [L.efactura("106")["item_id"], _op(L, 4, "NORD", "410.00", d1 + timedelta(days=19), details="Impozit")]
    _lose(L, "NORD", lost, today, now)
    _ef(L, "107", "NORD", "Mentenanta IT SRL", "RO1007", "MI-3301", ago(4), soon(14), "1500.00")
    store_lines(L, home, "efactura", "107", "NORD", MAINTENANCE_LINES)
    efactura.approve(L, "107", origin="person")
    # marked paid by the user, refused (IBAN changed), unreadable, paid last month by statement
    _ef(L, "108", "NORD", "Cafea Birou SRL", "RO1008", "CB-118", ago(10), soon(4), "89.00")
    L.mark_efactura_paid("108", "user: already paid (page)")
    _ef(L, "109", "NORD", "Paza Obiectiv SRL", "RO1009", "PO-0001", ago(6), soon(20), "700.00")
    L.db.execute(
        "INSERT INTO supplier_ibans SELECT company,supplier_cui,?,'statement:synthetic-fixture' FROM efactura WHERE message_id='109'",
        (_iban("BTRL0000000000009999"),),
    )
    L.db.commit()
    _ef(L, "110", "NORD", "", "", "", today, None, "0", parse_error="no Invoice element")
    # paid last month by two debits of NORD's statement, on record (Ledger.record_statement) as they would be after a sync
    rent, wages = (
        statements.Tx(
            date=d1 + timedelta(days=n),
            title="Plata OP",
            description=what,
            direction="debit",
            amount=Decimal(amount),
            currency="RON",
            iban="RO24BTRLRONCRT0000000001",
            ref=ref,
            counterparty=who,
        )
        for n, what, amount, ref, who in (
            (8, "Chirie CS-0920", "2400.00", "OP111", "CHIRIE SPATIU SRL"),
            (9, "Salarii", "940.00", "OP105", "BUGETUL GENERAL CONSOLIDAT"),
        )
    )
    L.record_statement(statements.Statement("NORD", None, None, [statements.Account("x", "RON", [rent, wages])]))
    _ef(
        L,
        "111",
        "NORD",
        "Chirie Spatiu SRL",
        "RO1011",
        "CS-0920",
        d1 + timedelta(days=1),
        d1 + timedelta(days=10),
        "2400.00",
    )
    L.mark_efactura_paid("111", f"statement {rent.date} REF OP111", statements.tx_key(rent))
    L.db.execute("UPDATE efactura SET paid_at=? WHERE message_id='111'", (f"{m1}-09T10:00:00",))
    L.db.commit()
    paid = _op(L, 5, "NORD", "940.00", d1 + timedelta(days=5), details="Salarii")
    _imported(L, "NORD", [paid], d1 + timedelta(days=6))  # from a file imported last month
    L.mark_paid(paid, f"statement {wages.date} REF OP105", statements.tx_key(wages))  # a statement debit proves it

    # STUDIO9 received: refused by autopay, no statement has proved its account yet (an admin confirms it)
    _ef(L, "201", "STUDIO9", "Laborator Analize SRL", "RO2001", "LA-77", ago(3), soon(12), "230.00")
    L.set_meta("efactura_autopay_refused:201", "no proved payment to this IBAN yet; confirm the supplier account first")
    _ef(L, "202", "STUDIO9", "Echipamente Test SRL", "RO2002", "ET-905", ago(8), soon(9), "1240.50")
    efactura.approve(L, "202", origin="auto")  # by the autopay rule: it waits for the statement
    _op(L, 6, "STUDIO9", "610.00", today.replace(day=1) + timedelta(days=4))
    L.db.execute(
        "INSERT INTO bank_connections(id,company,bank,provider,accounts,valid_until,created_at) "
        "VALUES ('bc-fixture','STUDIO9','bt','enablebanking','[]',?,?)",
        ((today + timedelta(days=70)).isoformat(), today.isoformat()),
    )
    paid = _op(L, 7, "STUDIO9", "150.00", d1 + timedelta(days=5))
    L.mark_paid(
        paid, "marked paid by the user"
    )  # `fiscalm pay mark-paid`: paid in the user's word, no statement proves it
    # STUDIO9: two monthly accounting invoices of the same amount and one debit that names neither: the user picks
    _ef(L, "203", "STUDIO9", "Contabil Expert SRL", "RO2003", "CX-1009", ago(35), soon(3), "350.00")
    _ef(L, "204", "STUDIO9", "Contabil Expert SRL", "RO2003", "CX-1010", ago(6), soon(24), "350.00")
    iban = L.efactura("203")["supplier_iban"]
    L.db.execute("UPDATE efactura SET supplier_iban=?, ibans=? WHERE message_id='204'", (iban, json.dumps([iban])))
    L.db.commit()
    debit = statements.Tx(
        date=ago(2),
        title="Plata OP",
        description="Plata servicii contabilitate",
        direction="debit",
        amount=Decimal("350.00"),
        currency="RON",
        iban="RO94BTRLRONCRT0000000002",
        counterparty="CONTABIL EXPERT SRL",
        counterparty_iban=iban,
        ref="OP77",
    )
    efactura.match_statement(L, statements.Statement("STUDIO9", None, None, [statements.Account("x", "RON", [debit])]))

    # issued: open (one overdue, one due soon, one in EUR), collected by statement, marked by the user, credited
    _issued(L, "5001", "NORD", "NRD 0042", "Client Alfa SRL", "RO5001", ago(40), ago(10), "4800.00")
    _issued(L, "5002", "NORD", "NRD 0043", "Client Beta SRL", "RO5002", ago(9), soon(5), "1250.00")
    store_lines(
        L,
        home,
        "issued",
        "5001",
        "NORD",
        [
            ("Dezvoltare aplicație web, etapa a doua", "40", "HUR", "85.00", "3400.00", "21"),
            ("Instruire echipă", "1", "DAY", "566.94", "566.94", "21"),
        ],
    )
    _issued(L, "5003", "NORD", "NRD 0044", "Client Gamma GmbH", "", ago(4), soon(26), "900.00", "EUR")
    _issued(
        L,
        "5004",
        "NORD",
        "NRD 0040",
        "Client Delta SRL",
        "RO5004",
        d1 + timedelta(days=2),
        d1 + timedelta(days=17),
        "3000.00",
    )
    paid_in = statements.Tx(
        date=d1 + timedelta(days=14),
        title="Incasare OP",
        description="Plata factura NRD 0040",
        direction="credit",
        amount=Decimal("3000.00"),
        currency="RON",
        iban="RO24BTRLRONCRT0000000001",
        ref="IN5004",
        counterparty="CLIENT DELTA SRL",
    )
    L.record_statement(statements.Statement("NORD", None, None, [statements.Account("x", "RON", [paid_in])]))
    L.mark_issued_collected("5004", f"statement {paid_in.date} cui REF IN5004", statements.tx_key(paid_in))
    _issued(
        L,
        "5005",
        "NORD",
        "NRD 0041",
        "Client Epsilon SRL",
        "RO5005",
        d1 + timedelta(days=8),
        d1 + timedelta(days=23),
        "650.00",
    )
    L.mark_issued_manual("5005", "user: compensare cu factura lor", "other")
    _issued(
        L,
        "5006",
        "NORD",
        "NRD 0039",
        "Client Zeta SRL",
        "RO5006",
        d1 + timedelta(days=1),
        d1 + timedelta(days=16),
        "300.00",
    )
    _issued(
        L,
        "5007",
        "NORD",
        "NRD 0039-S",
        "Client Zeta SRL",
        "RO5006",
        d1 + timedelta(days=6),
        None,
        "300.00",
        doc_type="credit_note",
        ref="NRD 0039",
    )
    _issued(L, "6001", "STUDIO9", "STD 0007", "Clinica Omega SRL", "RO6001", ago(20), ago(5), "2100.00")
    _issued(L, "6002", "STUDIO9", "STD 0008", "Clinica Omega SRL", "RO6001", ago(2), soon(28), "2100.00")

    # documents: last-but-one month sent for both companies, last month ready for NORD (STUDIO9 waits for its statement)
    def doc(co, mo, kind, vendor="", day=3, note="", amount="10.00", sent=False):
        sha = f"sha-doc-{co}-{mo}-{kind}-{vendor}".ljust(40, "0")
        L.add_document(
            sha,
            co,
            mo,
            kind,
            f"/nonexistent/{co}/{mo}/{vendor or 'extras'}.pdf",
            vendor=vendor or None,
            doc_date=f"{mo}-{day:02d}",
            amount=amount,
            currency="EUR",
            # a statement holds every account of the firm's banks, so its month's pack is complete
            accounts=[a.iban for a in companies.get(co, L=L).current_accounts] if kind == "statement" else None,
        )
        L.db.execute(
            "UPDATE documents SET created_at=? WHERE sha=?",
            (f"{mo}-{day:02d}T08:00:00" if mo != months[0] else (now - timedelta(days=2)).isoformat(), sha),
        )
        if sent:
            L.db.execute("UPDATE documents SET sent_at=? WHERE sha=?", (f"{m1}-05T09:00:00", sha))
        L.db.commit()
        if note:
            L.set_document_note(sha, note)

    for co, vendor in (("NORD", "GitHub"), ("STUDIO9", "GitHub")):
        doc(co, m2, "statement", sent=True)
        doc(co, m2, "receipt", vendor, 8, amount="4.00", sent=True)
        L.set_meta(f"pack_shas:{co}:{m2}", json.dumps([d["sha"] for d in L.documents(company=co, month=m2)]))
        L.set_pack(co, m2, "sent", note=f"sent {m1}-05 to the accountant")
    doc("NORD", m1, "statement")
    doc("NORD", m1, "receipt", "Hetzner", 2, amount="9.00")
    doc("NORD", m1, "receipt", "Google Workspace", 5, note="abonament anual", amount="72.00")
    doc("NORD", months[0], "receipt", "Figma", 3, amount="15.00")
    # Hetzner's invoice came by mail as a PDF, in fiscalm's own folder, paid with the card ending 4242
    pdf = home / "NORD" / m1 / "facturi" / f"{m1}-02_Hetzner_Invoice.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 fake invoice")
    L.db.execute("UPDATE documents SET path=?, card_last4='4242' WHERE vendor='Hetzner'", (str(pdf),))
    L.db.commit()

    # photographed bons: one paid by card, one whose shop also sent the e-Factura (linked: paid at the till), and one
    # whose invoice has another total (left apart)
    def photo(co, shop, total, nr, back):
        day = ago(back).isoformat()
        f = home / co / day[:7] / "bonuri" / f"bon-{nr}.jpg"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(f"fake jpeg, {shop}".encode())
        sha = f"sha-bon-{co}-{nr}".ljust(40, "0")
        L.add_document(
            sha,
            co,
            day[:7],
            "receipt",
            str(f),
            vendor=shop,
            doc_date=day,
            amount=total,
            currency="RON",
            issues=[],
            source="manual",
        )
        return sha

    photo("NORD", "Benzinaria Nord", "300.08", "0035", 2)
    brico = photo("NORD", "Brico Nord", "156.40", "0071", 6)
    photo("STUDIO9", "Farmacia Sud", "48.00", "0112", 4)
    _ef(L, "112", "NORD", "Brico Nord SRL", "RO1012", "BN-5521", ago(5), ago(5), "156.40")
    _ef(L, "205", "STUDIO9", "Farmacia Sud SRL", "RO2005", "FS-3310", ago(3), soon(11), "49.50")
    L.link_bon(brico, "112", "plătit la casă, bon 0071")
    bank_lines(L, today, d1)

    stamp = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
    _add_integrations(L)
    if integrations_configured:
        exp = (now + timedelta(days=20, hours=1)).isoformat(timespec="seconds")
        iid = ig.add(
            L, "anaf", "ANAF e-Factura", {"client_id": "fixture-client-id", "client_secret": "fixture-not-a-secret"}
        )["id"]
        ig.put_oauth(
            L,
            iid,
            json.dumps(
                {
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                    "access_expires_at": exp,
                    "refresh_expires_at": exp,
                    "authorized_at": "2025-11-16T10:00:00",
                    "refreshed_at": stamp,
                }
            ),
            client_id="fixture-client-id",
            replace=True,
        )
    mail_id = ig.add(
        L,
        "mail",
        "E-mail",
        {
            "host": "imap.example.com",
            "username": "facturi",
            "password": "fixture-not-a-secret",
            "from_address": "facturi@example.com",
        },
    )["id"]
    L.set_meta(f"mail_read:{mail_id}", json.dumps({"ok": True, "at": stamp}))
    L.set_meta(f"mail_read_ok:{mail_id}", stamp)
    # mailed invoices: one filed for NORD, one with no firm on it waiting in Sarcini
    from pdf_fake import text_pdf

    from fiscalm.mail import imap as imapread, invoices as mail_invoices

    for mid, sender, subject, lines in (
        (
            "cl-1042@cloud.example",
            "Example Cloud <billing@cloud.example>",
            "Your invoice CL-2026-1042",
            CLOUD_PDF_LINES,
        ),
        ("qx7pl2@air.example", "Example Air <receipts@air.example>", "Your receipt QX7PL2", AIR_PDF_LINES),
    ):
        msg = imapread.Message(
            ig.enabled(L, "mail")[0].id,
            mid,
            sender,
            sender.split("<")[1][:-1],
            subject,
            ago(2).isoformat(),
            (imapread.Attachment("pdf", text_pdf(lines)),),
        )
        mail_invoices.handle(L, msg)

    # the system: ANAF connected and expiring in 20 days, Gmail mirror up to date, a log
    if not integrations_configured:
        ig.remove(L, mail_id)
    for k in ("efactura_last_sync", "last_run_at", "last_check_at"):
        L.set_meta(k, stamp)
    real_connection = anaf.connection
    patch(anaf, "connection", lambda inst, at=None, L=None: real_connection(inst, at or now, L))
    patch(api, "_month_view", lambda L_, co, mo, tx_rows=None: _view(co, mo, months))
    log = home / "logs" / f"finance-{today:%Y-%m}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "".join(f"{today} 0{h}:30:0{h} tick: sync ok, 0 new mail(s) in /data/fiscalm/inbox\n" for h in range(8, 10))
    )
    invoicing_data(L, patch, today)
    bank_history(L, patch, today)
    history_import(L, today)
    partner_registry(L, today)
    automations(L, patch, now)
    # Keep the dashboard scenario's balance stable as local SPV states evolve.
    local_owed = sum(
        (
            Decimal(r["owed"])
            for r in api._issued(L, "NORD", today, True)
            if r["origin"] == "fiscalm" and api.issued_owed(r)
        ),
        Decimal(0),
    )
    with L.tx() as db:
        db.execute(
            "UPDATE issued SET total=? WHERE message_id=?", (format(Decimal("1250.00") - local_owed, ".2f"), "5002")
        )
    named_payment_cases(L, today)
    return {"months": months, "lost": lost}


def bank_lines(L, today: date, d1: date) -> None:
    """NORD's other statement lines, on record as after a sync: a card payment its receipt explains (Hetzner, the same
    amount), one whose receipt is found only by the shop's name (Figma, a proposal), and three Tranzacții can't
    explain yet: a foreign card payment without a receipt, the bank's fee and money in from a person."""
    from fiscalm.bank import statements

    def tx(day, direction, amount, who, what, ref, **kw):
        return statements.Tx(
            date=day,
            title=kw.pop("title", "Plata OP"),
            description=what,
            direction=direction,
            amount=Decimal(amount),
            currency="RON",
            iban="RO24BTRLRONCRT0000000001",
            ref=ref,
            counterparty=who,
            **kw,
        )

    card = dict(title="Plata la POS non-BT", card_last4="0001", country="US")
    lines = [
        tx(
            d1 + timedelta(days=2),
            "debit",
            "44.80",
            "HETZNER ONLINE GMBH",
            "Plata la POS HETZNER",
            "C1",
            orig_amount=Decimal("9.00"),
            orig_currency="EUR",
            **card,
        ),
        tx(
            today - timedelta(days=21),
            "debit",
            "74.70",
            "FIGMA INC",
            "Plata la POS FIGMA",
            "C2",
            orig_amount=Decimal("16.00"),
            orig_currency="USD",
            **card,
        ),
        tx(
            today - timedelta(days=6),
            "debit",
            "173.45",
            "NIMBUS HOSTING INC",
            "Plata la POS NIMBUS",
            "C3",
            orig_amount=Decimal("37.00"),
            orig_currency="USD",
            **card,
        ),
        tx(
            today - timedelta(days=4),
            "debit",
            "15.00",
            "",
            "Comision administrare cont",
            "F1",
            title="Comision",
            category=statements.BANK_FEE,
        ),  # as BT's reader sets it
        tx(today - timedelta(days=3), "credit", "500.00", "POPESCU ANA", "Transfer", "IN77", title="Incasare OP"),
    ]
    L.record_statement(statements.Statement("NORD", None, None, [statements.Account("x", "RON", lines)]))
    L.assign_card("NORD", statements.tx_key(lines[0]), "NORD", "fixture")


# Acasă's six months: what came in and went out of each account, per month (oldest first, this month so far last).
# Invented amounts, none equal to an open invoice's, so no line could settle anything if a matcher ever read them.
HISTORY = {
    ("NORD", "RO24BTRLRONCRT0000000001"): (
        [8400, 11250, 6900, 13800, 10350, 4200],
        [7150, 8900, 9400, 10100, 6500, 2950],
    ),
    ("NORD", "RO71BTRLEURCRT0000000001"): ([0, 0, 600, 0, 0, 0], [0, 0, 0, 0, 0, 0]),
    ("STUDIO9", "RO94BTRLRONCRT0000000002"): (
        [3100, 2900, 4200, 3600, 2100, 1050],
        [2600, 2300, 2950, 3100, 2400, 1900],
    ),
}

# Who the history's lines are with (invented): Tranzacții lists them, De explicat, since no invoice of theirs is on record.
HISTORY_PARTIES = {
    "credit": ("CLIENT ISTORIC UNU SRL", "CLIENT ISTORIC DOI SRL", "CLIENT ISTORIC TREI SRL"),
    "debit": ("FURNIZOR ISTORIC UNU SRL", "FURNIZOR ISTORIC DOI SRL", "FURNIZOR ISTORIC TREI SRL"),
}


def history_import(L, today: date) -> None:
    """NORD's invoices from the program it used before fiscalm, imported with this month as the start date: two issued
    and three received invoices dated before it are history (no payment state), one of them read from its PDF."""
    import io

    from pdf_fake import text_pdf

    from fiscalm import history

    start, nord = today.replace(day=1), "RO11111111"
    ago = lambda days: (start - timedelta(days=days)).isoformat()  # noqa: E731

    def sold(number: str, days: int, total: str) -> bytes:
        return ubl(
            number=number, issue=ago(days), due="", total=total, client="Depozit Exemplu SRL", client_cui="RO1014"
        )

    def bought(number: str, days: int, total: str) -> bytes:
        xml = ubl(number=number, issue=ago(days), due="", total=total, client_cui=nord, supplier_cui="RO1013")
        return xml.replace(b"Atelier Nord SRL", b"Tipografie Veche SRL", 1)

    pdf = ["FACTURA MV-118", f"Data: {start - timedelta(days=24):%d.%m.%Y}", "Furnizor: Mobilier Vechi SRL"]
    pdf += ["CUI: RO1015", "Client: Atelier Nord SRL", f"CUI: {nord}", "Total 3120.00 RON"]
    members = {
        "emise/NRD 0031.xml": sold("NRD 0031", 52, "2734.55"),
        "emise/NRD 0035.xml": sold("NRD 0035", 18, "611.20"),
        "primite/TV-0907.xml": bought("TV-0907", 45, "1487.30"),
        "primite/TV-0951.xml": bought("TV-0951", 9, "96.10"),
        "primite/MV-118.pdf": text_pdf(pdf),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    history.import_file(L, "NORD", buf.getvalue(), start.isoformat(), today)


def _chart_xml(account, first, last, opening, txs):
    totals = {d: sum((t.amount for t in txs if t.direction == d), Decimal(0)) for d in ("credit", "debit")}
    closing = opening + totals["credit"] - totals["debit"]
    balances = "".join(
        f'<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp><Amt Ccy="{account.currency}">{abs(amount):.2f}</Amt>'
        f"<CdtDbtInd>{'CRDT' if amount >= 0 else 'DBIT'}</CdtDbtInd><Dt><Dt>{day}</Dt></Dt></Bal>"
        for code, amount, day in (("OPBD", opening, first), ("CLBD", closing, last))
    )
    entries = "".join(
        f'<Ntry><Amt Ccy="{account.currency}">{t.amount:.2f}</Amt>'
        f"<CdtDbtInd>{'CRDT' if t.direction == 'credit' else 'DBIT'}</CdtDbtInd><Sts>BOOK</Sts>"
        f"<BookgDt><Dt>{t.date}</Dt></BookgDt><AcctSvcrRef>chart-{n}</AcctSvcrRef></Ntry>"
        for n, t in enumerate(txs)
    )
    return (
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>'
        f"<Acct><Id><IBAN>{account.iban}</IBAN></Id><Ccy>{account.currency}</Ccy></Acct>{balances}{entries}"
        "</Stmt></BkToCstmrStmt></Document>"
    ).encode(), closing


def _chart_document(L, home, co, account, first, xml):
    path = home / co / "chart-statements" / f"{first:%Y-%m}-{account.currency}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(xml)
    L.add_document(
        f"chart-{co}-{account.currency}-{first:%Y-%m}",
        co,
        first.strftime("%Y-%m"),
        "statement",
        str(path),
        accounts=[account.iban],
    )


def bank_history(L, patch, today: date) -> None:
    """Six months of account lines on record, as statements would leave them (Ledger.record_statement), and the
    balances the newest statement proves (NORD's; STUDIO9 has no statement for last month, so none is known)."""

    for (co, iban), (ins, outs) in HISTORY.items():
        currency = "EUR" if "EUR" in iban else "RON"
        txs = []
        for back, got, spent in zip(range(5, -1, -1), ins, outs, strict=True):
            first = dashboard._first_of(today, back)
            for n, (direction, total) in enumerate((("credit", got), ("debit", spent))):
                parts = [Decimal(total) * Decimal(w) for w in ("0.5", "0.3", "0.2")] if total else []
                for i, amount in enumerate(parts):
                    day = first + timedelta(days=3 + 7 * i + n)
                    if day <= today:
                        who = HISTORY_PARTIES[direction][i]
                        txs.append(
                            statements.Tx(
                                date=day,
                                title="Plata OP" if direction == "debit" else "Incasare OP",
                                description=f"{who.title()} {day:%m/%Y}",
                                direction=direction,
                                amount=amount.quantize(Decimal("0.01")),
                                currency=currency,
                                iban=iban,
                                counterparty=who,
                                ref=f"H{co}{day:%m%d}{n}{i}",
                            )
                        )
        L.record_statement(statements.Statement(co, None, None, [statements.Account(iban, currency, txs)]))
    first = dashboard._first_of(today, 1)
    last = today.replace(day=1) - timedelta(days=1)
    for account in companies.get("NORD").current_accounts:
        txs = [
            statements.Tx.from_dict(json.loads(r["tx"]))
            for r in L.account_lines("NORD", first.isoformat())
            if r["iban"] == companies.normalize_iban(account.iban) and r["day"] <= last.isoformat()
        ]
        closing = Decimal("18420.55") if account.currency == "RON" else Decimal("1240.00")
        net = sum((t.amount if t.direction == "credit" else -t.amount for t in txs), Decimal(0))
        xml, _ = _chart_xml(account, first, last, closing - net, txs)
        _chart_document(L, finance_home(), "NORD", account, first, xml)


def invoicing_data(L, patch, today: date) -> None:
    """Stable local invoices, separate from SPV imports, with no external calls."""
    from types import SimpleNamespace

    from fiscalm.efactura import client as anaf
    from fiscalm.invoicing import engine as i

    seller = dict(
        name="Atelier Exemplu SRL",
        cui="99999981",
        vat_payer=True,
        vat_id="RO99999981",
        reg_com="J00/0001/2026",
        street="Strada Exemplu 10",
        city="Brașov",
        county="RO-BV",
        postal_code="000000",
        country="RO",
        iban="RO24BTRLRONCRT0000000001",
        bank="Banca Exemplu",
        email="facturi@example.invalid",
        phone="0000000000",
    )
    profile = {k: seller[k] for k in ("reg_com", "street", "city", "county", "postal_code", "country", "vat_payer")}
    profile.update(vat_payer=False, vat_id="", vat_on_collection=False)
    with L.tx() as db:
        db.execute("UPDATE firms SET lookup_profile=? WHERE key='STUDIO9'", (json.dumps(profile),))
    i.save_seller(L, "NORD", seller)
    i.create_series(L, "NORD", "DEMO", 1)
    i.create_series(L, "NORD", "SERV", 50)
    for cid, party in [
        ("client-ro", dict(name="Client Exemplu SRL", cui="99999992", vat_id="RO99999992", ro_vat_status="registered")),
        (
            "client-eu",
            dict(name="Exemplu Europa GmbH", cui="", vat_id="DE999999999", country="DE", city="Berlin", county=""),
        ),
        ("client-private", dict(name="Persoană Exemplu", cui="", vat_id="", private=True, vat_payer=False)),
    ]:
        i.save_client(L, "NORD", seller | party, cid)
    line = dict(
        description="Servicii de proiectare",
        quantity="1",
        unit="HUR",
        unit_price="100.00",
        vat_category="S",
        vat_rate="21",
    )
    data = dict(
        issue_date=today.isoformat(),
        due_date=(today + timedelta(days=30)).isoformat(),
        currency="RON",
        notes="",
        lines=[line],
    )
    ids = iter(
        [
            "local-accepted",
            "local-uploaded",
            "local-rejected",
            "local-reversed",
            "local-storno",
            "local-draft",
            "local-reminded",
            "local-overdue",
        ]
    )
    patch(i, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    patch(products, "uuid4", lambda: SimpleNamespace(hex="catalog-design"))
    product = products.write(
        L,
        "NORD",
        "product-add",
        dict(
            kind="serviciu",
            name="Servicii de proiectare",
            unit="HUR",
            unit_price="100.00",
            currency="RON",
            vat_category="S",
            vat_rate="21",
        ),
    )
    linked = data | dict(lines=[line | dict(product_id=product["id"])])
    for status in ["accepted", "uploaded", "rejected", "accepted"]:
        iid = i.save_draft(L, "NORD", "DEMO", "client-ro", linked)
        i.issue(L, iid, today.isoformat(), company="NORD", version=1)
        errors = ["BR-CO-17"] if status == "rejected" else []
        with L.tx() as db:
            db.execute(
                "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha,errors) VALUES(?,?,?,?,?)",
                (iid, anaf.env(), status, "fixture-hash", json.dumps(errors)),
            )
    i.storno(L, "local-reversed", today.isoformat(), "Anulare servicii", company="NORD")
    i.save_draft(
        L,
        "NORD",
        "SERV",
        "client-ro",
        data
        | dict(
            delivery_date=today.isoformat(),
            lines=[
                line,
                line | dict(description="Materiale demonstrative", vat_rate="11"),
                line
                | dict(
                    description="Servicii cu taxare inversă",
                    vat_category="AE",
                    vat_rate="0",
                    exemption_reason="VATEX-EU-AE",
                ),
            ],
        ),
    )
    client_reminders(L, patch, today, data)
    recurring_rules(L, patch, today, data)


def recurring_rules(L, patch, today: date, data: dict) -> None:
    """Two monthly invoices prepared as drafts: one on, one stopped. Their source drafts are deleted after (the rule keeps
    its own copy), so Vânzări's drafts don't change."""
    from types import SimpleNamespace

    from fiscalm.invoicing import engine as i, recurring

    sources, rules = iter(["recurring-source-1", "recurring-source-2"]), iter(["rule-maintenance", "rule-hosting"])
    patch(i, "uuid4", lambda: SimpleNamespace(hex=next(sources)))
    patch(recurring, "uuid4", lambda: SimpleNamespace(hex=next(rules)))
    made = []
    for client, what, price, day in (
        ("client-ro", "Mentenanță lunară", "250.00", 1),
        ("client-private", "Găzduire site", "40.00", 15),
    ):
        line = data["lines"][0] | dict(description=what, unit_price=price, unit="H87", quantity="1")
        source = i.save_draft(L, "NORD", "DEMO", client, data | dict(lines=[line]))
        made.append(recurring.create(L, "NORD", source, day, 15, today.isoformat()))
        i.delete_draft(L, source, company="NORD", version=1)
    recurring.change(L, "NORD", made[1], "pause")


def client_reminders(L, patch, today: date, data: dict) -> None:
    """Two overdue invoices issued in fiscalm, each a Sarcină: one with a reminder ready to prepare, one whose last attempt
    left no answer (pending, sequence 1). The statements here are fakes without recorded coverage, so each card warns that
    they are missing, and hold no credit from the client; everything else is fiscalm's own eligibility check."""
    from fiscalm.invoicing import engine as i
    from fiscalm.mail import reminders

    i.create_series(L, "NORD", "REST", 1)
    for days in (26, 24):  # issued this month, so last month's report keeps its counts
        issue = today - timedelta(days=days)
        iid = i.save_draft(
            L,
            "NORD",
            "REST",
            "client-ro",
            data
            | dict(
                issue_date=issue.isoformat(),
                due_date=(issue + timedelta(days=10)).isoformat(),
                lines=[data["lines"][0] | dict(description="Mentenanță lunară", unit_price="250.00")],
            ),
        )
        i.issue(L, iid, issue.isoformat(), company="NORD", version=1)
    patch(reminders.StatementView, "credits", lambda self, since, today: [])
    reminders.eligible(L, "NORD", "local-reminded", today)


def partner_registry(L, today: date) -> None:
    """ANAF's public registry as the tick left it: a checked supplier, an inactive one, a stale check that failed since,
    a CUI ANAF doesn't know and a client never checked yet. Invented firms and CUIs (checksum-valid, as fiscalm requires)."""
    from fiscalm.efactura import inbox as efactura, public as anaf_public
    from fiscalm.invoicing import engine as i

    place = dict(street="Strada Exemplu 20", city="Cluj-Napoca", county="RO-CJ", postal_code="000000", country="RO")
    for cid, name, cui in (
        ("client-unknown", "Depozit Exemplu SRL", "1014"),
        ("client-new", "Atelier Partener SRL", "1022"),
    ):
        i.save_client(L, "NORD", place | dict(name=name, cui=cui, vat_id=""), cid)
    ago = lambda days: (today - timedelta(days=days)).isoformat()  # noqa: E731
    base = dict(
        vat_payer=True,
        vat_since=None,
        efactura_registered=True,
        inactive=False,
        inactive_register_removed_at=None,
        registration_status="INREGISTRAT din data 01.03.2015",
        vat_on_collection=False,
    )
    for cui, status in (
        ("1006", base | dict(checked_at=ago(2), vat_since="2019-03-01")),
        ("2001", base | dict(checked_at=ago(1), vat_payer=False, inactive=True)),
        ("99999992", base | dict(checked_at=ago(9), vat_on_collection=True)),
    ):
        anaf_public.store_status(L, cui, status)
    # The tick's autopay refused the inactive supplier's invoice, with fiscalm's reason (efactura.payment_item).
    L.set_meta(
        "efactura_autopay_refused:201",
        efactura.refusal_record(
            efactura.ApproveError(
                "supplier_inactive", f"supplier inactive at ANAF; checked_at={ago(1)}", checked_at=ago(1)
            )
        ),
    )
    with L.tx() as db:
        db.execute("UPDATE partner_anaf SET attempted_at=?, error='unavailable' WHERE cui='99999992'", (ago(0),))
        db.execute("INSERT INTO partner_anaf(cui,attempted_at,error) VALUES('1014',?,'not_found')", (ago(0),))


def automations(L, patch, now: datetime) -> None:
    """Automatizări's rules: suppliers chosen for the payment file (one paused with an invoice that would go in, one whose
    account changed, one inactive at ANAF), STUDIO9's SPV upload paused, and a Bondrop link used three times. Invented."""
    from types import SimpleNamespace

    from fiscalm.api import uploads as upload_links
    from fiscalm.efactura import inbox as efactura
    from fiscalm.invoicing import spv as invoice_spv

    at = now.replace(tzinfo=UTC).isoformat(timespec="seconds")
    patch(upload_links, "timestamp", lambda: at)
    for mid in ("102", "109", "201"):
        efactura.set_autopay(L, mid)
    efactura.set_autopay_enabled(L, "NORD", "RO1002", False)
    invoice_spv.set_auto_upload(L, "STUDIO9", False)
    real = upload_links.secrets
    upload_links.secrets = SimpleNamespace(
        token_hex=lambda n: "f" * (2 * n)
    )  # a stable id for this link only; its secret is never shown
    try:
        link = upload_links.create(L, "NORD", "Telefonul de serviciu")["id"]
    finally:
        upload_links.secrets = real
    with L.tx() as db:
        db.execute(
            "UPDATE upload_links SET uploads=3, last_used_at=? WHERE id=?",
            (f"{now - timedelta(days=2):%Y-%m-%dT%H:%M:%S}+00:00", link),
        )


def _add_integrations(L) -> None:
    """Two invented instances (and, when configured, the ANAF one build adds after them); no real key anywhere."""
    from fiscalm import integrations as ig

    if not any(i["kind"] == "ai" for i in ig.listing(L)):
        ig.add(
            L, "ai", "Claude", {"provider": "anthropic", "model": "claude-opus-5-5", "api_key": "sk-fixture-not-a-key"}
        )
        ig.add(
            L,
            "storage",
            "Arhiva documentelor",
            {"company": "NORD", "remote": "archive:", "root": "fixture-folder", "layout": "month"},
        )


def integrations_view(L) -> dict:
    """Integrări as the app reads it (GET api/integrations)."""
    from fiscalm import integrations as ig
    from fiscalm.ledger import is_live

    _add_integrations(L)
    return {"kinds": ig.kinds(), "items": ig.listing(L), "coverage": ig.coverage(L), "live": is_live()}


def sheet_lines(L) -> dict:
    """The lines the detail request gives each invoice sheet that has them, keyed kind:id, for the app's fixtures."""
    from fiscalm import invoice_lines
    from fiscalm.api import state as api

    out = {}
    for co in ("NORD", "STUDIO9"):
        full = api.state(L, co, can_manage_companies=True)
        for kind, rows in (("invoice", full["invoices"]), ("issued", full["issued"])):
            for row in rows:
                lines = invoice_lines.sheet(L, co, kind, row)
                if not lines["unavailable"]:
                    out[f"{kind}:{row['id']}"] = lines
    return out


def _named_payment_batch(L: Ledger, co: str, name: str, items: list[str]) -> int:
    ident = L.create_payment_file(co, name)
    for item in items:
        with L.tx() as db:
            db.execute(
                "UPDATE payments SET status='pending',csv_path=NULL,file_id=NULL,paid_at=NULL,paid_note=NULL,origin='person' WHERE item_id=?",
                (item,),
            )
        assert L.attach_payment_file(ident, co, item)
    return ident


def _named_nord_cases(L: Ledger, today: date, ops: dict[int, str]) -> None:
    short = companies.get("NORD", L).name
    invoice = lambda mid: L.efactura(mid)["item_id"]  # noqa: E731
    changed = _named_payment_batch(L, "NORD", f"{short} plăți furnizori", [invoice("105"), ops[2]])
    pay.download_file(L, changed, "NORD", today, lambda: None)
    extra = next(r["item_id"] for r in L.payments(company="NORD") if r["doc_ref"] == "AA 3")
    with L.tx() as db:
        db.execute("UPDATE payments SET status='pending',csv_path=NULL,origin='person' WHERE item_id=?", (extra,))
    assert L.attach_payment_file(changed, "NORD", extra)
    _named_payment_batch(L, "NORD", f"{short} plăți de pregătit", [invoice("107")])
    imported = _named_payment_batch(L, "NORD", f"{short} plăți importate", [invoice("104"), ops[1], ops[4]])
    snapshot = pay.download_file(L, imported, "NORD", today, lambda: None)
    assert pay.import_file(L, imported, "NORD", snapshot["download_id"], today)
    debit = statements.Tx(
        date=today,
        title="Plată sintetică",
        description="OP 1",
        direction="debit",
        amount=Decimal("1250.00"),
        currency="RON",
        iban=companies.get("NORD").ron_iban,
        ref="named-file-proof",
    )
    L.record_statement(statements.Statement("NORD", None, None, [statements.Account("x", "RON", [debit])]))
    assert L.mark_paid(ops[1], "statement", statements.tx_key(debit))
    assert L.mark_skipped(ops[4], "user: paid another way")
    proof = L.paid_proofs("NORD")["payment " + ops[5]]
    with L.tx() as db:
        db.execute("DELETE FROM settled_tx WHERE what=?", ("payment " + ops[5],))
    confirmed = _named_payment_batch(L, "NORD", f"{short} plăți confirmate", [ops[5]])
    snapshot = pay.download_file(L, confirmed, "NORD", today, lambda: None)
    assert pay.import_file(L, confirmed, "NORD", snapshot["download_id"], today)
    assert L.mark_paid(ops[5], "statement", proof)


def _named_studio_cases(L: Ledger, today: date, ops: dict[int, str]) -> None:
    short = companies.get("STUDIO9", L).name
    invoice = lambda mid: L.efactura(mid)["item_id"]  # noqa: E731
    _named_payment_batch(L, "STUDIO9", f"{short} plăți furnizori", [invoice("202"), ops[6]])
    with L.tx() as db:
        db.execute("UPDATE payments SET origin='auto' WHERE item_id=?", (invoice("202"),))
    _ef(L, "206", "STUDIO9", "Materiale Sintetice SRL", "RO2016", "MS-1", today, today + timedelta(days=8), "90.00")
    missing = pay.add_invoices(L, "STUDIO9", ["206"], name=f"{short} fișier lipsă", approve_invoice=efactura.approve)
    pay.download_file(L, missing, "STUDIO9", today, lambda: None)
    pay.named_path(L.payment_file(missing)).unlink()


def named_payment_cases(L: Ledger, today: date) -> None:
    """Exercise named files with real engine writes, synthetic payments and statement evidence."""
    ops = {int(r["doc_ref"].split()[1]): r["item_id"] for r in L.payments() if r["kind"] == "op"}
    _named_nord_cases(L, today, ops)
    _named_studio_cases(L, today, ops)


def _chart_older_lines(L, co, account, today):
    for back in range(9, 5, -1):
        first = dashboard._first_of(today, back)
        scale = Decimal("0.05") if account.currency == "EUR" else Decimal(1)
        amounts = [Decimal(4200 + (back % 4) * 800) * scale, Decimal(4800 + (back % 3) * 700) * scale]
        txs = [
            statements.Tx(
                date=first + timedelta(days=8 + n),
                direction=direction,
                amount=amount,
                currency=account.currency,
                iban=account.iban,
                title="Operațiune exemplu",
                counterparty=HISTORY_PARTIES[direction][0],
                description="Serie pentru grafice",
                ref=f"chart-{co}-{account.currency}-{back}-{n}",
            )
            for n, (direction, amount) in enumerate(zip(("credit", "debit"), amounts, strict=True))
        ]
        L.record_statement(
            statements.Statement(co, first, None, [statements.Account(account.iban, account.currency, txs)])
        )


def chart_tax_document(L, table, row, home, vat):
    if table not in ("efactura", "issued"):
        raise ValueError("unknown fixture table")
    kind = "CreditNote" if row["doc_type"] == "credit_note" else "Invoice"
    firm = companies.get(row["company"])
    received = table == "efactura"
    client_name = firm.legal_name if received else row["client_name"]
    client_cui = firm.cui if received else row["client_tax_id"] or row["client_cui"]
    supplier_cui = row["supplier_tax_id"] or row["supplier_cui"] if received else firm.cui
    supplier_name = row["supplier_name"] if received else firm.legal_name
    xml = ubl(
        number=row["number"],
        issue=row["issue_date"],
        due=row["due_date"] or "",
        total=row["total"],
        currency=row["currency"],
        root=kind,
        client=client_name,
        client_cui=client_cui,
        supplier_cui=supplier_cui,
    )
    xml = xml.replace(b"Atelier Nord SRL", escape(supplier_name).encode(), 1)
    tax = (
        f'<cac:TaxTotal><cbc:TaxAmount currencyID="{row["currency"]}">{abs(vat):.2f}</cbc:TaxAmount></cac:TaxTotal>'
    ).encode()
    xml = xml.replace(b"<cac:LegalMonetaryTotal>", tax + b"<cac:LegalMonetaryTotal>")
    # Invented line detail reconciles the recorded gross and declared tax.
    net = abs(Decimal(row["total"])) - abs(vat)
    if net > 0:
        rate = f"{abs(vat) * Decimal(100) / net:.2f}"
        lines = ubl_lines([("Servicii exemplu", "1", "H87", f"{net:.2f}", f"{net:.2f}", rate)], kind)
        xml = xml.replace(f"</{kind}>".encode(), lines.encode() + f"</{kind}>".encode())
    sha = hashlib.sha256(xml).hexdigest()
    path = home / "efactura" / row["company"] / "chart-vat" / (sha + ".zip")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("invoice.xml", xml)
    L.db.execute(f"UPDATE {table} SET raw_path=?,xml_sha=? WHERE message_id=?", (str(path), sha, row["message_id"]))
    L.db.commit()


def chart_vat_fixtures(L, home, today):
    since = dashboard._first_of(today, 1).isoformat()
    for table, rows in (("efactura", L.efactura_rows()), ("issued", L.issued_rows())):
        for row in rows:
            if (
                row["parse_error"]
                or row["status"] == "duplicate"
                or not since <= (row["issue_date"] or "") <= today.isoformat()
            ):
                continue
            if Path(row["raw_path"]).exists() or row["message_id"] == "101":
                continue
            vat = (abs(Decimal(row["total"])) * Decimal(21) / Decimal(121)).quantize(Decimal("0.01"))
            chart_tax_document(L, table, row, home, vat)


def chart_fixtures(L, home, today):
    """Eight complete months, a partial month, two unknown months and the unfinished current month."""
    for co, company in companies.all(L).items():
        for account in company.current_accounts:
            _chart_older_lines(L, co, account, today)
            lines = [
                r
                for r in L.account_lines(co, dashboard._first_of(today, 9).isoformat())
                if r["iban"] == companies.normalize_iban(account.iban)
            ]
            txs = [statements.Tx.from_dict(json.loads(r["tx"])) for r in lines]
            past = [t for t in txs if t.date < today.replace(day=1)]
            target = (
                Decimal("1240.00") if account.currency == "EUR" else Decimal("18420.55" if co == "NORD" else "6500.00")
            )
            opening = target - sum((t.amount if t.direction == "credit" else -t.amount for t in past), Decimal(0))
            for back in range(9, 0, -1):
                first = dashboard._first_of(today, back)
                last = dashboard._first_of(today, back - 1) - timedelta(days=1)
                monthly = [t for t in past if first <= t.date <= last]
                xml, opening = _chart_xml(account, first, last, opening, monthly)
                if back == 4 and (co == "STUDIO9" or account.currency == "EUR"):
                    continue
                _chart_document(L, home, co, account, first, xml)
                if first < dashboard._first_of(today, 1):
                    sha = f"chart-{co}-{account.currency}-{first:%Y-%m}"
                    sent = (last + timedelta(days=5)).isoformat()
                    L.db.execute("UPDATE documents SET sent_at=? WHERE sha=?", (sent, sha))
                    if not L.pack(co, first.strftime("%Y-%m")):
                        L.set_pack(co, first.strftime("%Y-%m"), "sent", note=f"sent {sent}")
                    key = f"pack_shas:{co}:{first:%Y-%m}"
                    shas = json.loads(L.get_meta(key, "[]"))
                    L.set_meta(key, json.dumps(sorted(set(shas) | {sha})))
                    L.db.commit()

    # One open invoice per aging bucket on each side, in every synthetic firm.
    for firm, co in enumerate(companies.all(L)):
        for n, days in enumerate((-14, 12, 45, 85, None)):
            due = today - timedelta(days=days) if days is not None else None
            issue = today - timedelta(days=max(days or 0, 0) + 10)
            _ef(
                L, str(8100 + firm * 1000 + n), co, f"Furnizor Grafic {n}", f"90000{n}", f"CG-{n}", issue, due, "350.00"
            )
            _issued(
                L, str(8200 + firm * 1000 + n), co, f"VG-{n}", f"Client Grafic {n}", f"90001{n}", issue, due, "650.00"
            )
    chart_vat_fixtures(L, home, today)


def write_fixtures(out: Path, *, integrations_configured: bool = True) -> None:
    """The app's fixtures: the state of each company and of both, and the mail to place, around NOW.
    Everything it replaces (environment, fiscalm's clock, the outside world) is put back afterwards."""
    sys.path.insert(0, str(ROOT / "src"))
    from fiscalm import ledger
    from fiscalm.api import lists, state as api
    from fiscalm.efactura import inbox as efactura
    from fiscalm.invoicing import workspace as invoicing_api

    env = {k: os.environ.get(k) for k in ("FISCALM_HOME", "FISCALM_SECRETS", "FISCALM_PUBLIC_URL")}
    undo = []

    def patch(obj, name, value):
        undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.update(
                FISCALM_HOME=tmp,
                FISCALM_SECRETS=str(Path(tmp) / "secrets"),
                FISCALM_PUBLIC_URL="https://fiscalm.example.com",
            )
            from fiscalm import integrations
            from fiscalm.mail import imap as imapread, invoices as mail_invoices

            for mod in (ledger, efactura, integrations, imapread, mail_invoices):  # what fiscalm records as "now"
                patch(mod, "now", lambda: NOW.isoformat(timespec="seconds"))
            ids = iter(range(1, 100))
            patch(integrations, "_new_id", lambda kind: f"{kind}-{next(ids):08d}")  # stable fixture ids
            L = ledger.Ledger()
            from firms import seed

            seed(L)
            try:
                build(L, Path(tmp), patch, NOW, integrations_configured=integrations_configured)
                chart_fixtures(L, Path(tmp), NOW.date())
                out.mkdir(parents=True, exist_ok=True)
                (out / "prepared-payments.json").write_text(
                    json.dumps(
                        {co: api.prepared_payments(L, co) for co in ("NORD", "STUDIO9")}, ensure_ascii=False, indent=1
                    )
                    + "\n"
                )
                (out / "payment-files.json").write_text(
                    json.dumps(
                        {
                            str(f["id"]): api.payment_file_detail(L, f["id"])
                            for co in ("NORD", "STUDIO9")
                            for f in pay.named_files(L, co)
                        },
                        ensure_ascii=False,
                        indent=1,
                    )
                    + "\n"
                )
                for co in ("NORD", "STUDIO9", api.ALL):
                    (out / f"state-{co}.json").write_text(
                        json.dumps(api.state(L, co, can_manage_companies=True), ensure_ascii=False, indent=1) + "\n"
                    )
                    (out / f"summary-{co}.json").write_text(
                        json.dumps(lists.summary(L, co, NOW, can_manage_companies=True), ensure_ascii=False, indent=1)
                        + "\n"
                    )
                    (out / f"tasks-{co}.json").write_text(
                        json.dumps(
                            lists.task_page(L, co, {"limit": "200"}, can_manage_companies=True),
                            ensure_ascii=False,
                            indent=1,
                        )
                        + "\n"
                    )
                for co in ("NORD", "STUDIO9"):
                    (out / f"invoicing-{co}.json").write_text(
                        json.dumps(invoicing_api.workspace(L, co), ensure_ascii=False, indent=1) + "\n"
                    )
                (out / "lines.json").write_text(json.dumps(sheet_lines(L), ensure_ascii=False, indent=1) + "\n")
                (out / "integrations.json").write_text(
                    json.dumps(integrations_view(L), ensure_ascii=False, indent=1) + "\n"
                )
            finally:
                L.close()
    finally:
        for obj, name, value in reversed(undo):
            setattr(obj, name, value)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--not-set-up"]
    write_fixtures(
        Path(args[0]) if args else ROOT / "app" / "fixtures", integrations_configured="--not-set-up" not in sys.argv
    )
