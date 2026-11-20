"""The series' next number after a history import: proposed from the highest imported number, confirmed by the user,
never a number the old program used. Synthetic data only."""

from datetime import date

import pytest
from test_history_import import zipped
from test_invoicing import (
    DATA,  # noqa: F401
    setup_invoice as setup_invoice,
)
from test_issued import anaf_zip, ubl

from fiscalm import history
from fiscalm.invoicing import engine as i, issued, workspace as invoicing_api


def imported(ledger, *numbers, start="2026-10-01"):
    xmls = [(f"{n}.xml", ubl(number=n, issue=f"2026-09-{k + 1:02d}")) for k, n in enumerate(numbers)]
    return history.import_file(ledger, "NORD", zipped(*xmls), start, date(2026, 11, 3))


def synced(ledger, mid, number, issue, **fields):
    issued.store(
        ledger,
        "NORD",
        {"id": mid, "data_creare": issue.replace("-", "") + "1200"},
        anaf_zip(ubl(number=number, issue=issue, **fields)),
    )


@pytest.mark.parametrize(
    "number,parts",
    [
        ("NRD 0042", ("NRD", 42)),
        ("NRD-0042", ("NRD", 42)),
        ("nrd0042", ("NRD", 42)),
        ("FCT2025-001", ("FCT2025", 1)),
        ("AB-C/7", ("AB-C", 7)),
        ("AB--42", ("AB-", 42)),
        ("AB- 42", ("AB", 42)),  # fiscalm's '<code>-<number>'
        ("NRD-1234567890123", None),
        ("NRD-999999999999", ("NRD", 999999999999)),
        ("NRD-" + "0" * 4300 + "7", ("NRD", 7)),
        ("NRD-0", None),
        ("2025-001", None),
        ("NRD", None),
        (None, None),
        ("X" * 21 + "1", None),
    ],
)
def test_split_number(number, parts):
    assert history.split_number(number) == parts


def test_numbers_come_from_history_and_from_issued_e_factura(ledger, home):
    imported(ledger, "NRD 0040", "NRD 0042", "ALT 7")
    synced(ledger, "7501", "NRD 0045", "2026-10-09")  # synced, after the start
    # A sent invoice whose supplier is another firm never moves the numbers.
    synced(ledger, "7701", "NRD-999999999999", "2026-10-09", supplier_cui="RO77777777")
    assert "supplier CUI 77777777 is not NORD" in ledger.issued("7701")["issues"]
    assert history.series_numbers(ledger, "NORD") == {"NRD": {"max": 45, "count": 3}, "ALT": {"max": 7, "count": 1}}
    assert history.series_numbers(ledger, "STUDIO9") == {}


def test_a_first_series_continues_the_imported_one_used_most_else_takes_the_firms_initials(ledger, home):
    assert invoicing_api.workspace(ledger, "NORD")["series_new"] == {"code": "AN", "next_number": 1}  # Atelier Nord SRL
    imported(ledger, "ALT 0007", "NRD 0040", "NRD 0042")
    assert invoicing_api.workspace(ledger, "NORD")["series_new"] == {"code": "NRD", "next_number": 43}
    i.create_series(ledger, "NORD", "NRD", 43)
    assert invoicing_api.workspace(ledger, "NORD")["series_new"] is None
    for name, code in (("Studio9 S.R.L.", "STU"), ("S.C. Ștefan & Fiii S.A.", "SF"), ("9 Arte SRL", "F"), ("SRL", "F")):
        assert i.first_series({}, name)["code"] == code


def test_the_workspace_shows_how_far_imports_reached_in_each_series(setup_invoice, home):
    L, _ = setup_invoice  # series TEST from 42, nothing imported in it
    imported(L, "NRD 0040", "NRD 0042", "TEST 0050")
    assert invoicing_api.workspace(L, "NORD")["series"] == [
        {"code": "TEST", "next_number": 42, "issued": 0, "default": True, "remove_refusal": None, "imported": 50}
    ]


def test_a_series_never_starts_on_a_number_the_imports_used(ledger, home):
    """Code 'AB-' issues 'AB--42': it is AB-'s number 42, never AB's."""
    imported(ledger, "NRD 0042", "AB--42")
    assert history.series_numbers(ledger, "NORD")["AB-"] == {"max": 42, "count": 1}
    for code in ("NRD", "AB-"):
        with pytest.raises(i.SeriesRefusal):
            i.create_series(ledger, "NORD", code, 42)
    i.create_series(ledger, "NORD", "NRD", 43)
    i.create_series(ledger, "NORD", "AB", 1)  # a series nothing imported names starts anywhere


def test_continue_moves_a_series_past_the_imports_once_and_only_forward(setup_invoice, home):
    L, _ = setup_invoice
    imported(L, "TEST 0050")
    act = lambda n: invoicing_api.act(L, "series-continue", "NORD", {"code": "TEST", "next_number": n})  # noqa: E731
    assert (
        act(50)["message"]
        == "Facturile importate din seria TEST ajung la numărul 50. Seria continuă de la cel puțin 51."
    )
    assert act(51)["ok"]
    assert L.db.execute("SELECT next_number FROM invoice_series WHERE code='TEST'").fetchone()[0] == 51
    again = act(51)  # a second press: already there, nothing changes
    assert (again["ok"], again["message"]) == (False, "Seria TEST continuă deja de la 51.")
    assert (
        invoicing_api.act(L, "series-continue", "NORD", {"code": "NOPE", "next_number": 9})["message"]
        == "Seria lipsește."
    )
    for bad in (
        {"code": "TEST", "next_number": "52"},
        {"code": "TEST", "next_number": 0},
        {"code": "x y", "next_number": 52},
    ):
        assert not invoicing_api.act(L, "series-continue", "NORD", bad)["ok"]


def test_continue_says_continued_and_refuses_once_fiscalm_issued_in_the_series(setup_invoice, home):
    L, cid = setup_invoice  # not live: continuing a series is allowed before going live
    assert (
        invoicing_api.act(L, "series-continue", "NORD", {"code": "TEST", "next_number": 60})["message"]
        == "Seria a fost continuată."
    )
    iid = i.save_draft(L, "NORD", "TEST", cid, DATA)
    i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    r = invoicing_api.act(L, "series-continue", "NORD", {"code": "TEST", "next_number": 70})
    assert r["message"] == "Seria TEST are deja facturi emise în fiscalm, iar numerotarea ei nu se mai schimbă."


def test_issuing_never_reuses_a_number_the_imports_used(setup_invoice, home):
    """Series TEST created at 42 before the import; the old program had reached 45: the draft stays a draft."""
    L, cid = setup_invoice
    imported(L, "TEST 0045")
    iid = i.save_draft(L, "NORD", "TEST", cid, DATA)
    with pytest.raises(i.InvoiceError) as e:
        i.issue(L, iid, "2026-10-31", company="NORD", version=1)
    assert str(e.value) == (
        "Numărul 42 din seria TEST a fost deja folosit în facturile importate. "
        "Seria se continuă de la 46, apoi factura se emite din nou."
    )
    assert i.invoice(L, iid, company="NORD")["status"] == "draft"
    i.continue_series(L, "NORD", "TEST", 46)
    assert i.issue(L, iid, "2026-10-31", company="NORD", version=1)["number"] == 46


def test_fiscalms_own_invoices_never_block_the_series_and_storno_never_reuses_an_imported_number(setup_invoice, home):
    L, cid = setup_invoice
    first = i.save_draft(L, "NORD", "TEST", cid, DATA)
    i.issue(L, first, "2026-10-31", company="NORD", version=1)
    synced(L, "7601", "TEST-42", "2026-10-31")  # fiscalm's own invoice back from SPV
    iid = i.save_draft(L, "NORD", "TEST", cid, DATA)
    assert i.issue(L, iid, "2026-10-31", company="NORD", version=1)["number"] == 43
    imported(L, "TEST 0050")
    with pytest.raises(i.SeriesRefusal, match="Numărul 44 din seria TEST"):
        i.storno(L, first, "2026-10-31", "Retur", company="NORD")
    assert L.db.execute("SELECT next_number FROM invoice_series WHERE code='TEST'").fetchone()[0] == 44
