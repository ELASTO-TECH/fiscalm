"""Synthetic payments and explicit named-file batches shared by lifecycle tests."""

from datetime import date
from decimal import Decimal

from firms import iban as synthetic_iban

from fiscalm.payments import engine as pay
from fiscalm.payments.orders import PaymentItem

D0 = date(2026, 10, 26)


def _op(ledger, n, amount="100.00", company="NORD", details="CAS"):
    item = PaymentItem(
        company,
        "op",
        "BUGETUL GENERAL CONSOLIDAT",
        synthetic_iban(f"RO00TREZ{n:016d}"),
        "TREZROBU",
        "11111111" if company == "NORD" else "33333333",
        Decimal(amount),
        details,
        date(2026, 10, 20),
        f"OP {n} 20/10/2026",
        "",
        f"sha{company}{n}",
    )
    assert ledger.add_payment(item)
    return item.item_id


def download_batch(ledger, items, company="NORD", today=D0):
    ident = ledger.create_payment_file(company, "Synthetic batch")
    for item in items:
        assert ledger.attach_payment_file(ident, company, item)
    return ident, pay.download_file(ledger, ident, company, today, lambda: None)
