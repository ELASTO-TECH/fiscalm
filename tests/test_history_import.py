"""Importing a firm's history as e-Factura XML: direction, dedup, start date, refusals. Synthetic data only."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import threading
import zipfile
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path

import pytest
from firms import iban as synthetic_iban
from pdf_fake import text_pdf
from test_issued import _credit, _st, anaf_zip, ubl
from test_settlement_import import workbook

from fiscalm import ai, cli, history, history_invoices, history_pdf, integrations
from fiscalm.api import lists, state as api
from fiscalm.bank import statements
from fiscalm.efactura import inbox as efactura
from fiscalm.invoicing import issued
from fiscalm.ledger import Ledger

TODAY = date(2026, 11, 3)
NOW = datetime(2026, 11, 3, 9, 0)
START = "2026-10-01"
NORD = "RO11111111"


SUPPLIER_IBAN = synthetic_iban("RO00INGB0000999900007777")


def supplier_ubl(number="FZ 100", issue="2026-09-12", total="595.00", supplier="RO77777777", iban=SUPPLIER_IBAN, **kw):
    """A supplier invoice to NORD: same builder, the parties swapped, the supplier's IBAN in PaymentMeans."""
    xml = ubl(
        number=number, issue=issue, total=total, supplier_cui=supplier, client="Atelier Nord SRL", client_cui=NORD, **kw
    ).replace(b"Atelier Nord SRL", b"Furnizor Test SRL", 1)
    means = (
        f"<cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode><cac:PayeeFinancialAccount>"
        f"<cbc:ID>{iban}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>"
    ).encode()
    return xml.replace(b"<cac:LegalMonetaryTotal>", means + b"<cac:LegalMonetaryTotal>")


def debit(amount, d, desc="Plata furnizor", iban=SUPPLIER_IBAN):
    return statements.Tx(
        date=d,
        title="Plata OP",
        description=desc,
        direction="debit",
        amount=Decimal(amount),
        currency="RON",
        iban="RO24BTRLRONCRT0000000001",
        ref=f"D{amount}{d}",
        counterparty="FURNIZOR TEST SRL",
        counterparty_iban=iban,
    )


def zipped(*members: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def symlinked(name: str, data: bytes | str) -> bytes:
    """A ZIP whose only member is marked as a symbolic link."""
    link, buf = zipfile.ZipInfo(name), io.BytesIO()
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(link, data)
    return buf.getvalue()


def export() -> bytes:
    """What an invoicing program exports: issued XMLs in a folder, a supplier invoice as SPV gives it, a PDF."""
    return zipped(
        ("facturi/NRD 0001.xml", ubl(number="NRD 0001", issue="2026-08-03", total="1190.00")),
        ("facturi/NRD 0002.xml", ubl(number="NRD 0002", issue="2026-10-05", total="238.00")),
        ("facturi/note.txt", b"not an invoice"),
        ("primite/4100000001.zip", anaf_zip(supplier_ubl(), "4100000001")),
    )


def run(ledger, data, start=START, company="NORD"):
    return history.import_file(ledger, company, data, start, TODAY)


def count(ledger, table, company="NORD"):
    return ledger.db.execute(f"SELECT COUNT(*) FROM {table} WHERE company=?", (company,)).fetchone()[0]


def test_direction_by_the_firms_cui_and_history_before_the_start(ledger, home):
    res = run(ledger, export())
    assert (res.issued, res.received, res.history_issued, res.history_received, res.duplicates) == (1, 0, 1, 1, 0)
    (old,) = history.rows(ledger, "NORD", "issued")
    assert (old["number"], old["party_name"], old["party_cui"], old["total"]) == (
        "NRD 0001",
        "BETA SOFTWARE SRL",
        "66666666",
        "1190.00",
    )
    (supplier,) = history.rows(ledger, "NORD", "received")
    assert (supplier["number"], supplier["party_cui"]) == ("FZ 100", "77777777")
    (tracked,) = issued.issued(ledger, "NORD", TODAY)["rows"]  # dated from the start: tracked, as if synced
    assert (tracked["number"], tracked["status"]) == ("NRD 0002", "neincasata")
    assert len(tracked["message_id"]) == 30 and tracked["message_id"].isdigit()
    assert ledger.issued(tracked["message_id"])["upload_id"] == history.IMPORTED
    assert history.start_date(ledger, "NORD") == START
    for r in history.rows(ledger, "NORD"):  # raw files named by their own hash, under the firm's history folder
        path = home / "history" / "NORD" / r["issue_date"][:7] / f"{r['xml_sha']}.zip"
        assert r["raw_path"] == str(path) and path.is_file()
    again = run(ledger, export())  # a reimport changes nothing
    assert (again.issued, again.history_issued, again.history_received, again.duplicates) == (0, 0, 0, 3)
    assert count(ledger, "history") == 2 and count(ledger, "issued") == 1


def test_issued_dedup_in_both_orders(ledger, home):
    """Synced from ANAF first (other bytes, same number and date): the import skips it. Imported as history first:
    a later sync keeps it in history."""
    synced = ubl(number="NRD 0002", issue="2026-10-05", total="238.00").replace(b"<cbc:ID>", b" <cbc:ID>", 1)
    issued.store(ledger, "NORD", {"id": "7001", "data_creare": "202610061200"}, anaf_zip(synced))
    res = run(ledger, export())
    assert (res.issued, res.duplicates) == (0, 1)
    assert [r["message_id"] for r in ledger.issued_rows(company="NORD")] == ["7001"]
    issued.store(
        ledger,
        "NORD",
        {"id": "7002", "data_creare": "202608041200"},
        anaf_zip(ubl(number="NRD 0001", issue="2026-08-03", total="1190.00")),
    )
    assert [r["number"] for r in history.rows(ledger, "NORD", "issued")] == ["NRD 0001"]
    assert count(ledger, "history") == 2


def test_history_is_listed_beside_tracked_invoices_but_never_owed(ledger, home):
    def browse(screen, **query):
        return lists.browse(ledger, "NORD", {"screen": screen, **query}, NOW)

    before = api.state(ledger, "NORD", NOW)
    run(ledger, export())
    after = api.state(ledger, "NORD", NOW)
    assert [(r["number"], r["kind"], r["direction"]) for r in after["history"]] == [
        ("NRD 0001", "history", "issued"),
        ("FZ 100", "history", "received"),
    ]
    sales = browse("vanzari")["data"]  # by date, newest first, beside the tracked invoice
    assert [(r["number"], r.get("kind")) for r in sales] == [("NRD 0002", None), ("NRD 0001", "history")]
    assert [r["number"] for r in browse("cheltuieli")["data"]] == ["FZ 100"]
    assert [r["number"] for r in browse("vanzari", q="66666666", month="2026-08")["data"]] == ["NRD 0001"]
    for f in ("open", "late", "storno"):  # never owed, late or reversed
        assert all(r.get("kind") != "history" for r in browse("vanzari", f=f)["data"])
    assert browse("cheltuieli", f="open")["data"] == [] and browse("bonuri")["data"] == []
    tracked_receivable = Decimal(before["receivable"]["total"]) + Decimal("238.00")
    assert Decimal(after["receivable"]["total"]) == tracked_receivable and after["payable"] == before["payable"]
    issued.store(  # synced from SPV: the tracked copy is listed, the history one no longer
        ledger,
        "NORD",
        {"id": "7002", "data_creare": "202608041200"},
        anaf_zip(ubl(number="NRD 0001", issue="2026-08-03", total="1190.00")),
    )
    assert [r.get("kind") for r in browse("vanzari")["data"]] == [None, "history"]


def test_history_counts_in_invoice_charts_but_never_in_balances_or_debts(ledger, home):
    run(ledger, export())
    s = api.state(ledger, "NORD", NOW)
    beta, supplier = (next(p for p in s["partners"] if p["id"] == pid) for pid in ("NORD.66666666", "NORD.77777777"))
    turnover = beta["turnover"]  # NRD 0002 tracked (238.00, still owed) and NRD 0001 from history (1190.00)
    assert (beta["issued"], beta["receivable"]["total"], turnover["sales"]["total"]) == (2, "238.00", "1428.00")
    assert turnover["history"]["sales"]["total"] == "1190.00"
    assert supplier["roles"] == ["supplier"] and supplier["payable"]["total"] == "0.00"  # known only from history
    assert (
        supplier["turnover"]["expenses"]
        == supplier["turnover"]["history"]["expenses"]
        == {"total": "595.00", "other": []}
    )
    pages = lists.partner_pages(ledger, "NORD", {"id": "NORD.66666666"}, NOW)["pages"]
    assert [(r["number"], r.get("kind")) for r in pages["issued"]["data"]] == [
        ("NRD 0002", None),
        ("NRD 0001", "history"),
    ]
    (year,) = s["turnover"]["years"]
    months = [(m["month"], m["turnover"]["sales"]["total"], m["turnover"]["history"]) for m in year["months"]]
    none, sold, bought = (
        {"total": "0.00", "other": []},
        {"total": "1190.00", "other": []},
        {"total": "595.00", "other": []},
    )
    assert months == [
        ("2026-10", "238.00", {"sales": none, "expenses": none}),
        ("2026-09", "0.00", {"sales": none, "expenses": bought}),
        ("2026-08", "1190.00", {"sales": sold, "expenses": none}),
    ]
    keys = ("payable", "receivable", "task_summary", "upcoming_count", "invoice_counts")
    shown = lists.summary(ledger, "NORD", NOW)
    owed = {p["id"]: (p["payable"], p["receivable"], p["late"]) for p in s["partners"]}
    ledger.db.execute("DELETE FROM history WHERE company='NORD'")
    bare = lists.summary(ledger, "NORD", NOW)  # History changes invoice charts, never cash or debts.
    assert {k: shown[k] for k in keys} == {k: bare[k] for k in keys}
    before, after = shown["dashboard"], bare["dashboard"]
    for key in ("balances", "months"):
        assert before[key] == after[key]
    for key in ("aging", "balance_total", "balance_history"):
        assert before["charts"][key] == after["charts"][key]
    for old, new in zip(before["charts"]["months"], after["charts"]["months"], strict=True):
        for key in ("in", "out", "net", "observed", "cash_status"):
            assert old[key] == new[key]
        expected_sales = "1190.00" if old["month"] == "2026-08" else "0.00"
        expected_expenses = "595.00" if old["month"] == "2026-09" else "0.00"
        assert Decimal(old["invoiced"]["total"]) - Decimal(new["invoiced"]["total"]) == Decimal(expected_sales)
        assert Decimal(old["received"]["total"]) - Decimal(new["received"]["total"]) == Decimal(expected_expenses)
    for p in api.state(ledger, "NORD", NOW)["partners"]:
        assert owed[p["id"]] == (p["payable"], p["receivable"], p["late"])


def test_turnover_stays_the_same_once_sync_brings_the_history_invoices(ledger, home):
    sold, bought = (
        ubl(number="NRD 0001", issue="2026-08-03", total="1190.00"),
        supplier_ubl("FZ 200", "2026-08-10", "100.00"),
    )
    credits = (
        ubl(root="CreditNote", number="NRD 0009", total="50.00", ref="NRD 0001", issue="2026-09-20"),
        supplier_ubl("FZ 201", "2026-09-05", "20.00", root="CreditNote", ref="FZ 200"),
    )
    run(ledger, zipped(*((f"{n}.xml", xml) for n, xml in enumerate((sold, bought, *credits)))))

    def figures():
        s = api.state(ledger, "NORD", NOW)
        months = [
            (m["month"], m["turnover"]["sales"]["total"], m["turnover"]["expenses"]["total"])
            for m in s["turnover"]["years"][0]["months"]
        ]
        mine = {p["id"]: (p["turnover"]["sales"]["total"], p["turnover"]["expenses"]["total"]) for p in s["partners"]}
        return months, mine["NORD.66666666"], mine["NORD.77777777"]

    before = figures()  # a credit note counts on its own date, never netted into the invoice it reduces
    assert before == (
        [("2026-09", "-50.00", "-20.00"), ("2026-08", "1190.00", "100.00")],
        ("1140.00", "0.00"),
        ("0.00", "80.00"),
    )
    meta = lambda n: {"id": str(n), "data_creare": "202611011200"}  # noqa: E731
    for n, xml in enumerate((sold, credits[0]), 7300):
        issued.store(ledger, "NORD", meta(n), anaf_zip(xml))
    for n, xml in enumerate((bought, credits[1]), 7400):
        efactura.store(ledger, "NORD", meta(n), anaf_zip(xml))
    assert figures() == before  # whichever copy fiscalm keeps


def test_a_client_known_only_by_a_credit_note_stays_a_partner_once_it_is_synced(ledger, home):
    credit = ubl(root="CreditNote", number="NRD 0009", total="50.00", ref="NRD 0001", issue="2026-09-20")
    run(ledger, zipped(("credit.xml", credit)))

    def client():
        (p,) = api.state(ledger, "NORD", NOW)["partners"]
        return p["id"], p["roles"], p["issued"], p["turnover"]["sales"]["total"]

    assert client() == ("NORD.66666666", ["client"], 0, "-50.00")  # imported as a tracked credit note
    issued.store(ledger, "NORD", {"id": "7500", "data_creare": "202611011200"}, anaf_zip(credit))
    assert client() == ("NORD.66666666", ["client"], 0, "-50.00")


def test_history_is_never_matched_but_a_tracked_invoice_is(ledger, home):
    run(ledger, export())
    collected = issued.match_statement(
        ledger, _st(_credit("1190.00", date(2026, 10, 20)), _credit("238.00", date(2026, 10, 21)))
    )
    assert len(collected) == 1
    assert [r["status"] for r in ledger.issued_rows(company="NORD")] == ["collected"]
    assert ledger.db.execute("SELECT COUNT(*) FROM settled_tx").fetchone()[0] == 1


def test_a_credit_naming_a_history_invoice_collects_nothing(ledger, home):
    """OLD (history) and NEW (tracked): same client, same amount. A credit naming OLD must not collect NEW."""
    run(
        ledger,
        zipped(
            ("o.xml", ubl(number="NRD 0001", issue="2026-09-20", total="500.00")),
            ("n.xml", ubl(number="NRD 0002", issue="2026-10-05", total="500.00")),
        ),
    )
    assert (
        issued.match_statement(
            ledger, _st(_credit("500.00", date(2026, 10, 20), desc="Incasare;BETA SOFTWARE SRL;plata fact NRD 0001"))
        )
        == []
    )
    # a credit that names nothing fits the old one as well: it collects nothing either
    assert issued.match_statement(ledger, _st(_credit("500.00", date(2026, 10, 21)))) == []
    # one naming the new invoice collects it
    assert (
        len(
            issued.match_statement(
                ledger, _st(_credit("500.00", date(2026, 10, 22), desc="Incasare;BETA SOFTWARE SRL;fact NRD 0002"))
            )
        )
        == 1
    )


def test_a_debit_naming_or_fitting_a_history_invoice_pays_nothing(ledger, home, monkeypatch):
    monkeypatch.setattr("fiscalm.ledger.is_live", lambda: True)
    run(
        ledger,
        zipped(
            ("o.xml", supplier_ubl(number="FZ 100", issue="2026-09-12")),
            ("n.xml", supplier_ubl(number="FZ 200", issue="2026-10-02")),
        ),
    )
    assert efactura.match_statement(ledger, _st(debit("595.00", date(2026, 10, 10), "Plata factura FZ 100"))) == []
    st = _st(debit("595.00", date(2026, 10, 11)))
    assert efactura.match_statement(ledger, st) == []
    (amb,) = api.ambiguous(ledger, "NORD", live=True)  # recorded; only the tracked invoice is offered
    assert [c["number"] for c in amb["candidates"]] == ["FZ 200"]
    old = next(r for r in history.rivals(ledger, "NORD", "received") if r["number"] == "FZ 100")
    with pytest.raises(efactura.ResolveError):
        efactura.resolve_debit(ledger, "NORD", amb["tx"], old["message_id"])
    assert [r["status"] for r in ledger.efactura_rows(company="NORD")] == ["open"]


def test_foreign_suppliers_with_the_same_digits_are_two_suppliers(ledger, home):
    a = supplier_ubl(number="INV-1", issue="2026-08-01", supplier="DE12345678")
    b = supplier_ubl(number="INV-1", issue="2026-08-01", supplier="FR12345678")
    res = run(ledger, zipped(("a.xml", a), ("b.xml", b)))
    assert (res.history_received, res.duplicates) == (2, 0)
    # tracked: one synced from ANAF, the other imported; and the history copy stops counting only for its twin
    efactura.store(ledger, "NORD", {"id": "7101", "data_creare": "202608021200"}, anaf_zip(a))
    assert [r["party_tax_id"] for r in history.rows(ledger, "NORD", "received")] == ["DE12345678", "FR12345678"]


def test_received_dedup_in_both_orders(ledger, home):
    xml = supplier_ubl(number="FZ 300", issue="2026-10-03")
    efactura.store(ledger, "NORD", {"id": "7201", "data_creare": "202610041200"}, anaf_zip(xml))
    assert run(ledger, xml).duplicates == 1  # synced, then imported
    other = supplier_ubl(number="FZ 301", issue="2026-10-04")
    run(ledger, other)  # imported (tracked), then synced: the synced copy is the duplicate
    efactura.store(ledger, "NORD", {"id": "7202", "data_creare": "202610051200"}, anaf_zip(other))
    assert ledger.efactura("7202")["status"] == "duplicate"


@pytest.mark.parametrize("direction", ["issued", "received"])
def test_a_sync_storing_while_an_import_holds_the_lock_sees_it(ledger, home, direction):
    """The duplicate check runs under the write lock: a sync that started while an import was writing waits for it,
    then stores its copy as a duplicate, never as a second open invoice."""
    xml = (
        ubl(number="NRD 0002", issue="2026-10-05", total="238.00")
        if direction == "issued"
        else supplier_ubl(number="FZ 400", issue="2026-10-06")
    )
    store, rows = (
        (issued.store, ledger.issued_rows) if direction == "issued" else (efactura.store, ledger.efactura_rows)
    )
    importer = Ledger(timeout=30)
    importer.db.execute("BEGIN IMMEDIATE")
    run(importer, xml)  # nested in the open transaction: written, not committed
    done = []
    t = threading.Thread(
        target=lambda: done.append(
            store(
                Ledger(timeout=30),
                "NORD",
                {"id": "7301", "data_creare": "202610071200"},
                anaf_zip(xml.replace(b"<cbc:ID>", b" <cbc:ID>", 1)),
            )
        )
    )
    t.start()
    t.join(0.5)
    importer.db.execute("COMMIT")
    importer.close()
    t.join()
    assert done[0]["status"] == "duplicate"
    assert [r["status"] for r in rows(company="NORD")].count("open") == 1


def test_a_negative_invoice_is_a_credit_note_and_compact_dates_are_dates(ledger, home):
    """IssueDate 20260920 is 2026-09-20: before the start, so history, never a tracked open invoice."""
    res = run(
        ledger,
        zipped(
            ("s.xml", ubl(number="NRD 0003", issue="2026-07-01", total="-100.00")),
            ("c.xml", ubl(number="NRD 0010", issue="20260920", due="20261005")),
        ),
    )
    assert (res.issued, res.history_issued) == (1, 1)
    credit = ledger.issued_rows(company="NORD")[0]
    (compact,) = history.rows(ledger, "NORD")
    assert (credit["doc_type"], credit["total"]) == ("credit_note", "100.00")
    assert (compact["issue_date"], compact["due_date"]) == ("2026-09-20", "2026-10-05")


def test_another_firms_invoice_refuses_everything(ledger, home):
    foreign = ubl(number="X 9", supplier_cui="RO33333333")  # STUDIO9 to a client: not NORD's
    with pytest.raises(history.Refused) as e:
        run(ledger, zipped(("a.xml", ubl(number="NRD 0001", issue="2026-08-03")), ("b.xml", foreign)))
    assert e.value.code == "other_firm" and "X 9" in str(e.value)
    assert count(ledger, "history") == 0 and history.start_date(ledger, "NORD") is None
    assert not (home / "history").exists()


def test_an_invoice_from_the_firm_to_itself_is_refused(ledger, home):
    with pytest.raises(history.Refused, match="not this firm"):
        run(ledger, ubl(client_cui=NORD))


def test_the_same_export_imports_into_the_other_firm_as_received(ledger, home):
    """A NORD invoice to STUDIO9: NORD's issued, STUDIO9's received; each firm's import stands alone."""
    xml = ubl(number="NRD 0005", issue="2026-08-10", client="Studio 9 SRL", client_cui="RO33333333")
    run(ledger, xml)
    res = run(ledger, xml, company="STUDIO9")
    assert res.history_received == 1
    assert [r["direction"] for r in history.rows(ledger, "STUDIO9")] == ["received"]


@pytest.mark.parametrize("name", ["../evil.xml", "a/../../evil.xml", "/etc/evil.xml", "C:/evil.xml", "a\\..\\evil.xml"])
def test_names_that_leave_their_folder_refuse_the_zip(ledger, home, name):
    with pytest.raises(history.Refused) as e:
        run(ledger, zipped(("ok.xml", ubl(issue="2026-08-03")), (name, ubl(number="NRD 0009", issue="2026-08-04"))))
    assert e.value.code == "unsafe_zip" and count(ledger, "history") == 0


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_links_encrypted_members_and_names_used_twice_refuse_the_zip(ledger, home):
    twice = zipped(("a.xml", ubl(issue="2026-08-03")), ("a.xml", ubl(number="NRD 0009", issue="2026-08-04")))
    encrypted = bytearray(zipped(("enc.xml", b"xx")))
    for sig, at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # set "encrypted" in both headers
        encrypted[encrypted.index(sig) + at] |= 0x1
    for data in (symlinked("link.xml", "/etc/passwd"), bytes(encrypted), twice):
        with pytest.raises(history.Refused) as e:
            run(ledger, data)
        assert e.value.code == "unsafe_zip"


def test_inner_message_zips_get_the_same_checks_and_budget(ledger, home, monkeypatch):
    with pytest.raises(history.Refused) as e:
        run(ledger, zipped(("m.zip", symlinked("1.xml", ubl(issue="2026-08-03")))))
    assert e.value.code == "unsafe_zip"
    with pytest.raises(history.Refused) as e:
        run(ledger, zipped(("m.zip", zipped(("../1.xml", ubl(issue="2026-08-03"))))))
    assert e.value.code == "unsafe_zip"
    big = ubl(issue="2026-08-03").replace(b"<cbc:ID>", b" " * 600_000 + b"<cbc:ID>", 1)
    monkeypatch.setattr(history, "MAX_INFLATED", 1_000_000)  # each inner ZIP is tiny; what it inflates counts
    with pytest.raises(history.Refused) as e:
        run(ledger, zipped(("1.zip", zipped(("1.xml", big))), ("2.zip", zipped(("2.xml", big)))))
    assert e.value.code == "too_big"


def test_a_link_out_of_the_firms_history_folder_refuses(ledger, home):
    (home / "history" / "STUDIO9" / "2026-08").mkdir(parents=True)
    (home / "history" / "NORD").mkdir()
    (home / "history" / "NORD" / "2026-08").symlink_to(home / "history" / "STUDIO9" / "2026-08")
    with pytest.raises(history.Refused) as e:
        run(ledger, ubl(issue="2026-08-03"))
    assert e.value.code == "storage" and count(ledger, "history") == 0
    assert not list((home / "history" / "STUDIO9" / "2026-08").iterdir())


def test_sizes_and_counts_are_capped(ledger, home, monkeypatch):
    monkeypatch.setattr(history, "MAX_ENTRIES", 2)
    with pytest.raises(history.Refused) as e:
        run(ledger, zipped(*((f"{i}.xml", b"<x/>") for i in range(3))))
    assert e.value.code == "too_many"
    monkeypatch.setattr(history, "MAX_FILE", 100)
    with pytest.raises(history.Refused) as e:
        run(ledger, export())
    assert e.value.code == "too_big"


def test_inflated_bytes_are_capped_not_the_declared_size(ledger, home, monkeypatch):
    bomb = b"<a>" + b" " * 2_000_000 + b"</a>"  # compresses to a few KB
    monkeypatch.setattr(history, "MAX_MEMBER", 100_000)
    res = run(ledger, zipped(("ok.xml", ubl(issue="2026-08-03")), ("bomb.xml", bomb)))
    assert res.history_issued == 1 and res.unreadable == ["bomb.xml: too large"]
    monkeypatch.setattr(history, "MAX_MEMBER", 1_000_000)
    monkeypatch.setattr(history, "MAX_INFLATED", 1_000_000)
    with pytest.raises(history.Refused) as e:
        run(ledger, zipped(*((f"{i}.xml", bomb[:900_000]) for i in range(2))))
    assert e.value.code == "too_big"


def test_dtd_and_non_invoices_are_listed_not_imported(ledger, home):
    dtd = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
    res = run(
        ledger,
        zipped(
            ("ok.xml", ubl(issue="2026-08-03")),
            ("dtd.xml", dtd),
            ("saga.xml", b"<Facturi><Factura/></Facturi>"),
            ("semnatura_1.xml", b"<Signature/>"),
        ),
    )
    assert res.history_issued == 1 and res.unreadable_count == 2
    assert res.unreadable[0] == "dtd.xml: XML with a DTD refused"
    with pytest.raises(history.Refused) as e:
        run(ledger, dtd)
    assert e.value.code == "no_invoices"


def test_archives_nest_one_level_only(ledger, home):
    inner = zipped(("NRD 0007.xml", ubl(number="NRD 0007", issue="2026-08-05")))
    deeper = zipped(("deeper.zip", inner))  # a message ZIP holding a ZIP: no invoice XML in it
    res = run(ledger, zipped(("one.zip", inner), ("two.zip", deeper)))
    assert res.history_issued == 1
    assert res.unreadable == ["two.zip: expected one invoice XML in the ZIP, found 0"]


def test_not_a_zip_or_xml(ledger, home):
    for data, code in ((b"unknown", "unsupported_file"), (b"PK\x03\x04garbage", "bad_zip")):
        with pytest.raises(history.Refused) as e:
            run(ledger, data)
        assert e.value.code == code


def test_start_date_is_set_once_and_never_after_today(ledger, home):
    with pytest.raises(history.Refused, match="after today"):
        run(ledger, export(), start="2026-11-04")
    for bad in ("1 oct", "20261001", "2026-W40-1", "2026-10-1"):  # only YYYY-MM-DD: compared as a string
        with pytest.raises(history.Refused) as e:
            run(ledger, export(), start=bad)
        assert e.value.code == "bad_start"
    run(ledger, export())
    with pytest.raises(history.Refused, match="already 2026-10-01"):
        run(ledger, export(), start="2026-09-01")


def test_two_connections_importing_at_once_store_each_invoice_once(ledger, home):
    data, errors = export(), []

    def go():
        L = Ledger(timeout=30)
        try:
            run(L, data)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
        finally:
            L.close()

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert count(ledger, "history") == 2 and count(ledger, "issued") == 1


def test_cli_imports_and_lists(ledger, home, tmp_path, capsys):
    f = tmp_path / "export.zip"
    f.write_bytes(export())
    assert cli.main(["history", "import", "NORD", str(f), "--from", START, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["issued"], out["history_issued"], out["history_received"]) == (1, 1, 1)
    assert cli.main(["history", "import", "NORD", str(tmp_path / "missing.zip"), "--from", START]) == 1
    assert "does not exist" in capsys.readouterr().err  # a missing file is not called unsupported
    f.write_bytes(b"unknown")
    assert cli.main(["history", "import", "NORD", str(f), "--from", START]) == 1
    assert history.MESSAGES["unsupported_file"] in capsys.readouterr().err
    assert cli.main(["history", "list", "NORD"]) == 0
    assert "start 2026-10-01, 1 issued and 1 received in history" in capsys.readouterr().out


def test_oversized_inner_xmls_count_toward_the_budget_too(ledger, home, monkeypatch):
    """Refused as too large or not, what an inner ZIP inflated counts: many tiny ZIPs can't inflate gigabytes."""
    monkeypatch.setattr(efactura, "MAX_XML", 100_000)
    monkeypatch.setattr(history, "MAX_INFLATED", 1_000_000)
    huge = b"<a>" + b" " * 500_000 + b"</a>"
    with pytest.raises(history.Refused) as e:
        run(ledger, zipped(*((f"{i}.zip", zipped(("x.xml", huge))) for i in range(12))))
    assert e.value.code == "too_big"


def test_a_planted_temporary_file_or_link_is_never_written_through(ledger, home):
    xml = ubl(issue="2026-08-03")
    sha = hashlib.sha256(xml).hexdigest()
    target = home / "victim.zip"
    target.write_bytes(b"keep")
    folder = home / "history" / "NORD" / "2026-08"
    folder.mkdir(parents=True)
    (folder / f"{sha}.part").symlink_to(target)
    run(ledger, xml)
    assert target.read_bytes() == b"keep" and (folder / f"{sha}.zip").is_file()
    (folder / f"{sha}.zip").unlink()
    (folder / f"{sha}.zip").symlink_to(target)  # the final name itself a link: refused
    ledger.db.execute("DELETE FROM history")
    ledger.db.commit()
    with pytest.raises(history.Refused) as e:
        run(ledger, xml)
    assert e.value.code == "storage" and target.read_bytes() == b"keep"


def test_history_waits_for_payment_lock_before_reserving_database(ledger, monkeypatch):
    attempted = threading.Event()
    errors = []
    original_lock = history.pay_lock

    @contextmanager
    def observed_lock():
        attempted.set()
        with original_lock():
            yield

    def import_supplier():
        other = Ledger(ledger.path, timeout=0.2)
        try:
            run(other, supplier_ubl(issue="2026-10-20"))
        except Exception as exc:
            errors.append(exc)
        finally:
            other.close()

    monkeypatch.setattr(history, "pay_lock", observed_lock)
    worker = threading.Thread(target=import_supplier)
    try:
        with original_lock():
            worker.start()
            assert attempted.wait(3)
            # The exporter can still reserve SQLite while the history import waits for its file lock.
            ledger.set_meta("exporter_progress", "recorded")
    finally:
        worker.join(3)
    assert not worker.is_alive() and errors == []
    assert count(ledger, "efactura") == 1


@pytest.mark.parametrize("layout", ["fgo", "smartbill"])
@pytest.mark.parametrize("way", ["issued", "received"])
def test_mixed_history_pdf_is_never_tracked_and_xml_wins(ledger, layout, way):

    own, other = ("Atelier Nord SRL", "11111111"), ("Firma Exemplu SRL", "77777777")
    seller, buyer = (own, other) if way == "issued" else (other, own)
    lines = ["FACTURA TEST-42", "Data: 03.11.2026"]
    for label, party in (("Furnizor", seller), ("Client", buyer)):
        lines += (
            [f"{label}: {party[0]} CUI: RO{party[1]}"]
            if layout == "fgo"
            else [f"{label}: {party[0]}", f"CUI: RO{party[1]}"]
        )
    raw = text_pdf(lines + ["Total 119.00 RON"])
    res = run(ledger, zipped(("not-an-extension", raw)))
    assert (res.history_issued, res.history_received, res.review) == ((1, 0, 0) if way == "issued" else (0, 1, 0))
    assert ledger.db.execute("SELECT COUNT(*) FROM issued").fetchone()[0] == 0
    assert ledger.db.execute("SELECT COUNT(*) FROM efactura").fetchone()[0] == 0
    xml = (
        ubl(number="TEST-42", issue="2026-11-03", total="119.00", client_cui="RO77777777")
        if way == "issued"
        else supplier_ubl(number="TEST-42", issue="2026-11-03", total="119.00")
    )
    res = run(ledger, zipped(("copy.pdf", raw), ("wrong.pdf", xml)))
    assert (res.issued, res.received, res.duplicates) == ((1, 0, 1) if way == "issued" else (0, 1, 1))
    assert not history.rows(ledger, "NORD")
    assert ledger.db.execute("SELECT COUNT(*) FROM history_documents WHERE status='accepted'").fetchone()[0] == 1


def test_empty_pdf_review_is_separate_and_answer_is_once(ledger):

    raw = text_pdf([])
    res = run(ledger, zipped(("scan", raw)))
    assert res.review == 1 and not history.rows(ledger, "NORD")
    assert not ledger.db.execute("SELECT 1 FROM mail_questions").fetchone()
    q = history_pdf.pending(ledger, "NORD")[0]
    assert history_pdf.document(ledger, "NORD", q["sha"]) == raw
    data = dict(
        direction="issued",
        number="TEST-43",
        issue_date="2023-11-03",
        total="119.00",
        currency="RON",
        party_name="Firma Exemplu SRL",
        party_tax_id="77777777",
    )
    history_pdf.answer(ledger, "NORD", q["sha"], data)
    with pytest.raises(history_pdf.ReviewError):
        history_pdf.answer(ledger, "NORD", q["sha"], data)
    assert len(history.rows(ledger, "NORD")) == 1
    assert not history_pdf.pending(ledger, "NORD")


HISTORY_TEXT = ["Firma Exemplu SRL      Atelier Nord SRL", "RO77777777      RO11111111", "03.11.2026", "119,00 lei"]
FACTURA = ["Factura SB-0099", *HISTORY_TEXT]


@pytest.mark.parametrize(
    ("lines", "fields"),
    [
        (FACTURA, {"confidence": "low"}),
        (FACTURA, {"seller_tax_id": "RO88888888"}),  # a partner id it doesn't print
        (FACTURA, {"seller_tax_id": "7777"}),  # a fragment of the printed one
        (FACTURA, {"currency": "EUR"}),  # the document prints lei
        ([*FACTURA[:4], "Curs BNR 1 EUR = 4,97 RON", "Total 119,00"], {"currency": "EUR"}),  # a total without currency
        ([*FACTURA, "Proiect pentru Studio 9 SRL"], {}),  # another of the firms on it
        (FACTURA, {"seller_tax_id": "SRL"}),  # not a firm's id
        (  # the rules read the partner; the AI's other, unprinted id contradicts it
            ["FACTURA", "SB-0099", "Furnizor: Firma Exemplu SRL CUI: RO77777777", "Client: Atelier Nord SRL"]
            + ["Data: 03.11.2026", "Total 119,00 lei"],
            {"seller_tax_id": "RO88888888"},
        ),
        (FACTURA, {"currency": ""}),  # a total it gave without its currency
        (["Factura SB-0099", *HISTORY_TEXT[:2], "Data: 03.11.2026", "Data: 03.12.2026", "119,00 lei"], {}),  # two dates
        (["Aviz de insotire SB-0099", *HISTORY_TEXT], {}),  # no invoice title
        (
            [
                "Factura SB-0099",
                "Bill to: Firma Exemplu SRL RO77777777",
                "Ship to: Atelier Nord SRL",
                *HISTORY_TEXT[2:],
            ],
            {"buyer_tax_id": ""},  # only its delivery address
        ),
        (FACTURA, {"issue_date": "03.11.2026"}),  # a field failing its rule
        (FACTURA + ["Total 120,00 RON"], {}),  # the rules found another total
        (FACTURA + ["Total 120,00 RON", "Total 121,00 RON", "Subtotal 119,00 RON"], {}),  # none of their totals
        (FACTURA + ["Factura SB-0100"], {}),  # two invoices in one PDF: the rules cleared the number
        (["Factura Seria SB nr 0099", *HISTORY_TEXT], {"number": "0099"}),  # a separately printed series
    ],
)
def test_history_pdf_ai_reading_in_doubt_waits_in_review(ledger, monkeypatch, lines, fields):
    history_ai(ledger, monkeypatch, fields)
    assert run(ledger, zipped(("factura.pdf", text_pdf(lines)))).review == 1
    (q,) = history_pdf.pending(ledger, "NORD")
    assert (q["reason"], q["direction"], q["party_name"]) == ("unsure", "received", "Firma Exemplu SRL")


@pytest.mark.parametrize("head", [["Proforma SB-0099"], ["Adjustment SB-0099", "Total -119,00 lei"]])
def test_a_proforma_or_negative_total_is_never_filed_by_an_ai_reading(ledger, monkeypatch, head):
    history_ai(ledger, monkeypatch, {})
    assert run(ledger, zipped(("p.pdf", text_pdf([*head, *HISTORY_TEXT])))).review == 1


@pytest.mark.parametrize("lines", [FACTURA, []])  # a text PDF, and a scan read from its first page's picture
def test_history_pdf_read_by_ai_in_sandbox(ledger, monkeypatch, lines):
    history_ai(ledger, monkeypatch, {})
    res = run(ledger, zipped(("factura.pdf", text_pdf(lines))))
    assert (res.history_received, res.review) == (1, 0)
    row = history.rows(ledger, "NORD")[0]
    got = tuple(row[k] for k in ("direction", "number", "issue_date", "total", "party_tax_id"))
    assert got == ("received", "SB-0099", "2026-11-03", "119.00", "77777777")


def history_ai(ledger, monkeypatch, fields):
    integrations.add(
        ledger, "ai", "Local AI", {"provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "t"}
    )
    answer = {
        "doc_type": "invoice",
        "buyer_name": "Atelier Nord SRL",
        "buyer_tax_id": "RO11111111",
        "seller_name": "Firma Exemplu SRL",
        "seller_tax_id": "RO77777777",
        "number": "SB-0099",
        "issue_date": "2026-11-03",
        "total": "119.00",
        "currency": "RON",
        "card_last4": "",
        "confidence": "high",
    } | fields
    monkeypatch.setattr(ai, "ask_json", lambda L, system, prompt, schema, image=None: answer)


@pytest.mark.parametrize("numbers", [("NRD 1", "NRD 2"), ("A-0042", "B-0042")])
@pytest.mark.parametrize("way", ["issued", "received"])
def test_pdf_number_identity_preserves_series_and_short_numbers(ledger, numbers, way):
    for number in numbers:
        raw = text_pdf([number])
        run(ledger, raw)
        sha = hashlib.sha256(raw).hexdigest()
        history_pdf.answer(
            ledger,
            "NORD",
            sha,
            dict(
                direction=way,
                number=number,
                issue_date="2023-11-03",
                total="119.00",
                currency="RON",
                party_name="Firma Exemplu SRL",
                party_tax_id="77777777",
            ),
        )
    assert len(history.rows(ledger, "NORD")) == 2
    xml = (
        ubl(number=numbers[0], issue="2023-11-03", total="119.00")
        if way == "issued"
        else supplier_ubl(number=numbers[0], issue="2023-11-03", total="119.00")
    )
    run(ledger, xml)
    rows = history.rows(ledger, "NORD")
    assert {r["number"] for r in rows} == set(numbers)
    assert len(rows) == 2  # only the first PDF projection became XML
    assert next(r for r in rows if r["number"] == numbers[1])["raw_path"].endswith(".pdf")


def test_current_pdf_does_not_block_synced_xml_and_normalized_copy_stops_counting(ledger):
    for number in ("NRD-0042",):
        raw = text_pdf(
            [
                f"FACTURA {number}",
                "Data: 03.11.2026",
                "Furnizor: Atelier Nord SRL CUI: RO11111111",
                "Client: Firma Exemplu SRL CUI: RO77777777",
                "Total 119.00 RON",
            ]
        )
        run(ledger, raw)
    assert len(history.rivals(ledger, "NORD", "issued")) == 1
    issued.store(
        ledger,
        "NORD",
        {"id": "901", "id_solicitare": "x"},
        anaf_zip(ubl(number="NRD0042", issue="2026-11-03", total="119.00")),
    )
    assert not history.rows(ledger, "NORD")
    assert not history.rivals(ledger, "NORD", "issued")


def test_pdf_extraction_runs_before_ledger_write_lock(ledger, monkeypatch):
    original = history_pdf.fields

    def extract(L, raw, company):
        assert not L.db.in_transaction
        other = Ledger(L.path, timeout=0.1)
        try:
            other.set_meta("concurrent_pdf_read", "ok")
        finally:
            other.close()
        return original(L, raw, company)

    monkeypatch.setattr(history_pdf, "fields", extract)
    run(ledger, text_pdf([]))
    assert ledger.get_meta("concurrent_pdf_read") == "ok"


def test_separately_printed_series_requires_review_instead_of_incomplete_number(ledger):
    raw = text_pdf(
        [
            "FACTURA Seria NRD nr. 0042",
            "Data: 03.11.2026",
            "Furnizor: Atelier Nord SRL CUI: RO11111111",
            "Client: Firma Exemplu SRL CUI: RO77777777",
            "Total 119.00 RON",
        ]
    )
    assert run(ledger, raw).review == 1
    assert not history.rows(ledger, "NORD")


def test_multiple_invoices_in_one_pdf_wait_for_review(ledger):
    raw = text_pdf(
        [
            "FACTURA TEST-41",
            "Data: 03.11.2023",
            "Furnizor: Atelier Nord SRL CUI: RO11111111",
            "Client: Firma Exemplu SRL CUI: RO77777777",
            "Total 119.00 RON",
            "FACTURA TEST-42",
            "Data: 04.11.2023",
            "Total 119.00 RON",
        ]
    )
    result = run(ledger, zipped(("all.pdf", raw)))
    assert result.review == 1 and not history.rows(ledger, "NORD")


def saga_xml(number="TEST-42", issue="03.11.2023", supplier=NORD, buyer="RO77777777", value="100.00"):
    return (
        f"<Facturi><Factura><Antet><FurnizorNume>Firma Emitenta SRL</FurnizorNume><FurnizorCIF>{supplier}</FurnizorCIF>"
        f"<ClientNume>Firma Client SRL</ClientNume><ClientCIF>{buyer}</ClientCIF><FacturaNumar>{number}</FacturaNumar>"
        f"<FacturaData>{issue}</FacturaData></Antet><Detalii><Continut><Linie><Valoare>{value}</Valoare>"
        "<TVA>19.00</TVA></Linie></Continut></Detalii></Factura></Facturi>"
    ).encode()


@pytest.mark.parametrize("way", ["issued", "received"])
def test_saga_detects_content_and_uses_cui_start_date_and_series(ledger, home, way):
    parties = {} if way == "issued" else dict(supplier="RO77777777", buyer=NORD)
    old = saga_xml(**parties)
    current = saga_xml(number="TEST-43", issue="03.11.2026", **parties)
    data = zipped(("arbitrary.bin", old), ("also-not-xml.pdf", current))
    result = run(ledger, data)
    assert getattr(result, f"history_{way}") == 1 and getattr(result, way) == 1
    assert run(ledger, data).duplicates == 2
    row = history.rows(ledger, "NORD")[0]
    assert row["total"] == "119.00"
    with zipfile.ZipFile(row["raw_path"]) as stored:
        source_sha = stored.read("source.sha256").decode()
    with zipfile.ZipFile(home / "history" / "NORD" / "2026-10" / (source_sha + ".zip")) as original:
        assert original.read(source_sha + ".xml") == old
    if way == "issued":
        assert history.series_numbers(ledger, "NORD")["TEST"]["max"] == 43
        assert not ledger.db.execute("SELECT 1 FROM invoice_series").fetchone()  # proposal only


def test_saga_clients_and_collection_claims_are_idempotent_and_settle_nothing(ledger):
    invoice = saga_xml(issue="03.11.2026")
    clients = b"<Clienti><Linie><Denumire>Firma Client SRL</Denumire><Cod_fiscal>RO77777777</Cod_fiscal><Tara>RO</Tara></Linie></Clienti>"
    clients = clients.replace(
        b"</Clienti>", b"<Linie><Denumire>Client Fara Identitate</Denumire><Tara>RO</Tara></Linie></Clienti>"
    )
    collections = b"<Incasari><Linie><Data>04.11.2026</Data><Numar>CH-1</Numar><Suma>119.00</Suma><FacturaNumar>TEST-42</FacturaNumar></Linie></Incasari>"
    data = zipped(("invoice", invoice), ("clients", clients), ("collections", collections))
    result = run(ledger, data)
    assert result.records == 3 and result.issued == 1
    assert run(ledger, data).records == 0
    assert ledger.db.execute("SELECT status FROM issued").fetchone()[0] == "open"
    assert not ledger.db.execute("SELECT 1 FROM settled_tx").fetchone()
    assert ledger.db.execute("SELECT count(*) FROM invoice_clients").fetchone()[0] == 2
    assert (
        json.loads(ledger.db.execute("SELECT data FROM invoice_clients WHERE identity IS NOT NULL").fetchone()[0])[
            "vat_id"
        ]
        == "RO77777777"
    )
    assert ledger.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'history_source:%'").fetchone()[0] == 3
    assert run(ledger, clients).records == 0  # standalone files use the same path


def test_saga_foreign_firm_refuses_the_whole_archive(ledger):
    data = zipped(("own", saga_xml()), ("other", saga_xml(supplier="RO33333333", buyer="RO77777777")))
    with pytest.raises(history.Refused, match="not this firm's"):
        run(ledger, data)
    assert not history.rows(ledger, "NORD") and history.start_date(ledger, "NORD") is None


@pytest.mark.parametrize(
    "header",
    [
        b"<FacturaMoneda>EUR</FacturaMoneda>",
        b"<FacturaTaxareInversa>Da</FacturaTaxareInversa>",
        b"<FacturaAccize>5.00</FacturaAccize>",
    ],
)
def test_saga_unverified_amount_semantics_never_create_a_debt(ledger, header):
    raw = saga_xml(issue="03.11.2026").replace(b"</Antet>", header + b"</Antet>")
    with pytest.raises(history.Refused):
        run(ledger, raw)
    assert not ledger.db.execute("SELECT 1 FROM issued").fetchone()


@pytest.mark.parametrize("way", ["issued", "received"])
def test_saga_and_efactura_same_full_number_date_and_firm_track_once(ledger, way):
    parties = {} if way == "issued" else dict(supplier="RO77777777", buyer=NORD)
    run(ledger, saga_xml(issue="03.11.2026", **parties))
    xml = (
        ubl(number="TEST-42", issue="2026-11-03", total="119.00")
        if way == "issued"
        else supplier_ubl(number="TEST-42", issue="2026-11-03", total="119.00")
    )
    result = run(ledger, xml)
    assert result.duplicates == 1 and not result.issued and not result.received


def unpaid_xlsx(
    rows=(("TEST-42", "03.11.2023", "0.00"),), header=("Factura", "Data emiterii", "Rest de încasat"), date1904=False
):
    sheet = []
    for i, row in enumerate((header, *rows), 1):
        cells = []
        for j, value in enumerate(row):
            ref = f"{chr(65 + j)}{i}"
            cell = (
                f'<c r="{ref}"><v>{value}</v></c>'
                if isinstance(value, int)
                else f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
            cells.append(cell)
        sheet.append(f'<row r="{i}">{"".join(cells)}</row>')
    data = io.BytesIO(workbook("".join(sheet)))
    with zipfile.ZipFile(data, "a") as z:
        rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{rel}">'
            f'<workbookPr date1904="{int(date1904)}"/><sheets><sheet name="Facturi" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(  # a real workbook lists its sheets through its relationships
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
    return data.getvalue()


def test_smartbill_xlsx_claims_are_provenance_not_collection(ledger):
    run(ledger, ubl(number="TEST-42", issue="2023-11-03", total="119.00"))
    raw = unpaid_xlsx()
    result = run(ledger, raw)
    assert result.records == 1 and result.issued == 0
    assert len(history.rows(ledger, "NORD")) == 1
    assert not ledger.db.execute("SELECT 1 FROM settled_tx").fetchone()
    assert run(ledger, raw).records == 0


@pytest.mark.parametrize(
    "options",
    [
        {"header": ("Factura", "Data emiterii", "Rest de încasat (RON)")},
        {"rows": (("TEST-42", "03.11.2023", "=1+1"),)},
        {"date1904": True},
    ],
)
def test_unverified_or_unsafe_unpaid_xlsx_is_refused(ledger, options):
    with pytest.raises(history.Refused):
        run(ledger, unpaid_xlsx(**options))
    assert history.start_date(ledger, "NORD") is None


@pytest.mark.parametrize("source", ["xml", "pdf"])
def test_positive_unpaid_claim_opens_history_without_claimed_partial_settlement(ledger, source):
    raw = (
        ubl(number="TEST-42", issue="2023-11-03", total="119.00")
        if source == "xml"
        else text_pdf(
            [
                "FACTURA TEST-42",
                "Data: 03.11.2023",
                "Furnizor: Atelier Nord SRL CUI: RO11111111",
                "Client: Firma Exemplu SRL CUI: RO77777777",
                "Total 119.00 RON",
            ]
        )
    )
    run(ledger, raw)
    run(ledger, ubl(number="TEST-43", issue="2023-11-03", total="119.00"))
    report = unpaid_xlsx((("TEST-42", "03.11.2023", "59,00"), ("TEST-43", "03.11.2023", "0.00")))
    result = run(ledger, report)
    assert result.issued == 1 and result.records == 2
    assert [r["number"] for r in history.rows(ledger, "NORD")] == ["TEST-43"]
    invoice = ledger.issued_rows(company="NORD")[0]
    assert (invoice["status"], invoice["total"]) == ("open", "119.00")  # no claimed partial payment deducted
    assert not ledger.db.execute("SELECT 1 FROM settled_tx").fetchone()
    assert run(ledger, report).issued == 0
    assert len(ledger.issued_rows(company="NORD")) == 1
    ledger.mark_issued_manual(invoice["message_id"], "synthetic claim", "other")
    run(ledger, unpaid_xlsx((("TEST-42", "03.11.2023", "1.00"),)))
    assert ledger.issued(invoice["message_id"])["status"] == "marked"


@pytest.mark.parametrize(
    "rows",
    [
        (("TEST-42", "03.11.2023", "119.00"), ("UNKNOWN-9", "03.11.2023", "1.00")),
        (("TEST-42", "03.11.2023", "120.00"),),
        (("TEST4-2", "03.11.2023", "119.00"),),  # separators are part of the invoice identity
        (("TEST-42", "03.11.2023", "119.00"), ("TEST-42", "03.11.2023", "0.00")),
    ],
)
def test_unpaid_list_refuses_as_a_whole(ledger, rows):
    run(ledger, ubl(number="TEST-42", issue="2023-11-03", total="119.00"))
    with pytest.raises(history.Refused):
        run(ledger, unpaid_xlsx(rows))
    assert not ledger.issued_rows(company="NORD")
    assert len(history.rows(ledger, "NORD")) == 1
    assert not ledger.db.execute("SELECT 1 FROM meta WHERE key LIKE 'history_unpaid:%'").fetchone()


def test_unpaid_rechecks_if_another_connection_imports_the_invoice(ledger, monkeypatch):
    xml = ubl(number="TEST-42", issue="2023-11-03", total="119.00")
    run(ledger, xml)
    original = history._promotion_source

    def concurrent(L, company, row):
        assert not L.db.in_transaction
        source = original(L, company, row)
        other = Ledger(L.path, timeout=0.1)
        try:
            issued.store(other, company, {"id": "601", "id_solicitare": "synthetic"}, anaf_zip(xml), track_history=True)
        finally:
            other.close()
        return source

    monkeypatch.setattr(history, "_promotion_source", concurrent)
    assert run(ledger, unpaid_xlsx((("TEST-42", "03.11.2023", "119.00"),))).issued == 0
    assert len(ledger.issued_rows(company="NORD")) == 1


def test_unpaid_never_regenerates_a_missing_source(ledger):
    run(ledger, ubl(number="TEST-42", issue="2023-11-03", total="119.00"))
    row = history.rows(ledger, "NORD")[0]
    source = Path(row["raw_path"])
    source.unlink()
    with pytest.raises(history.Refused):
        run(ledger, unpaid_xlsx((("TEST-42", "03.11.2023", "119.00"),)))
    assert not source.exists() and not ledger.issued_rows(company="NORD")


@pytest.mark.parametrize("way", ["issued", "received"])
def test_archive_companions_open_from_history_without_extra_invoices(ledger, way):
    xml = ubl(issue="2026-09-12") if way == "issued" else supplier_ubl()
    raw = text_pdf(["A document whose fields need not be read twice"])
    data = zipped(
        ("2026.09.12_TEST_1_Partner.pdf", raw),
        ("2026.09.12_TEST_2_Partner.xml", xml),
        ("2026.09.12_TEST_3_Partner.xml", b'<Signature xmlns="http://www.w3.org/2000/09/xmldsig#"/>'),
    )
    result = run(ledger, data)
    assert getattr(result, f"history_{way}") == 1
    assert result.review == result.duplicates == result.unreadable_count == 0
    row = api._history(ledger, "NORD")[0]
    assert history_pdf.document(ledger, "NORD", row["document"]) == raw
    assert run(ledger, data).duplicates == 1
    assert count(ledger, "issued") == count(ledger, "efactura") == 0


@pytest.mark.parametrize("multiline", [False, True])
def test_archive_pdf_columns_and_totals_link_to_xml_even_with_different_filename(ledger, multiline):
    name = "Firma Exemplu" if multiline else "Firma Exemplu SRL"
    lines = [
        "Factura TEST-42",
        "Data emitere: 12.09.2026",
        "Data scadenta: 30.09.2026",
        "",
        f"{'Furnizor':<60}Client",
        f"{'Atelier Nord SRL':<60}{name}",
    ]
    if multiline:
        lines += [f"{'CUI: RO11111111':<60}SRL", f"{'Reg. Com.: TEST':<60}CUI: RO77777777"]
    else:
        lines += [f"{'CUI: RO11111111':<60}CUI: RO77777777"]
    raw = text_pdf(lines + ["TOTAL 1 190.00 LEI"])
    run(ledger, ubl(number="TEST-42", issue="2026-09-12", total="1190.00", client_cui="RO77777777"))
    result = run(ledger, zipped(("unrelated.pdf", raw)))
    assert result.duplicates == 1 and result.review == 0
    (row,) = api._history(ledger, "NORD")
    assert history_pdf.document(ledger, "NORD", row["document"]) == raw
    assert count(ledger, "history") == 1


@pytest.mark.parametrize("extra", ["TOTAL 238.00 LEI", "Furnizor             Client"])
def test_archive_pdf_with_ambiguous_columns_or_totals_stays_in_review(ledger, extra):
    raw = text_pdf(
        [
            "Factura TEST-42",
            "Data emitere: 12.09.2026",
            "",
            f"{'Furnizor':<60}Client",
            f"{'Atelier Nord SRL':<60}Firma Exemplu SRL",
            f"{'CUI: RO11111111':<60}CUI: RO77777777",
            "TOTAL 119.00 LEI",
            extra,
        ]
    )
    assert run(ledger, raw).review == 1
    assert history.rows(ledger, "NORD") == []


@pytest.mark.parametrize("number", ["Seria AB nr. 42", "fiscala", "proforma"])
def test_archive_pdf_without_a_certain_full_number_stays_in_review(ledger, number):
    raw = text_pdf(
        [
            f"Factura {number}",
            "Data emitere: 12.09.2026",
            "",
            f"{'Furnizor':<60}Client",
            f"{'Atelier Nord SRL':<60}Firma Exemplu SRL",
            f"{'CUI: RO11111111':<60}CUI: RO77777777",
            "TOTAL 119.00 LEI",
        ]
    )
    assert run(ledger, raw).review == 1
    assert history.rows(ledger, "NORD") == []


def archive_pdf(head: list[str], name: str = "Firma Exemplu SRL") -> bytes:
    """An FGO-style archive PDF for NORD as supplier, with `head` above the two party columns."""
    parties = [f"{'Furnizor':<60}Client", f"{'Atelier Nord SRL':<60}{name}", f"{'CUI: RO11111111':<60}CUI: RO77777777"]
    return text_pdf(head + parties + ["TOTAL 119.00 LEI"])


FGO_BARE = ["              FACTURA" + " " * 37 + "Data emitere:      12.09.2026", " " * 57 + "Data scadenta"]


@pytest.mark.parametrize(
    "head,number",
    [
        (FGO_BARE + ["              ABC 0065" + " " * 54 + "30.09.2026"], "ABC 0065"),
        ([f"{'Factura':<20}Data emitere   12.09.2026", f"{'ABC 0042':<20}Data Scadenta   30.09.2026"], "ABC 0042"),
        (["Factura TEST-42", "Data emitere: 12.09.2026"], "TEST-42"),
        (["FACTURA", f"{'TEST-42':<20}Data Scadenta: 30.09.2026", "Data emitere: 12.09.2026"], "TEST-42"),
    ],
)
def test_archive_pdf_number_is_never_a_label_and_xml_still_supersedes_it(ledger, head, number):
    assert run(ledger, archive_pdf(head)).history_issued == 1
    assert [r["number"] for r in history.rows(ledger, "NORD")] == [number]
    run(ledger, ubl(number=number.replace(" ", ""), issue="2026-09-12", total="119.00", client_cui="RO77777777"))
    assert count(ledger, "history") == 1 and not history_pdf.pending(ledger, "NORD")


@pytest.mark.parametrize("series", [["ABC 0065", "XYZ 0066"], ["Data scadenta 30.09.2026"], ["ABC 0065/2026"]])
def test_archive_pdf_with_bare_heading_and_no_single_series_stays_in_review(ledger, monkeypatch, series):
    seller = {"seller_name": "Atelier Nord SRL", "seller_tax_id": "RO11111111", "buyer_name": "Firma Exemplu SRL"}
    history_ai(
        ledger, monkeypatch, seller | {"buyer_tax_id": "RO77777777", "number": "ABC 0065", "issue_date": "2026-09-12"}
    )
    assert run(ledger, archive_pdf(FGO_BARE + series)).review == 1
    assert history.rows(ledger, "NORD") == []


def test_owner_corrects_or_removes_a_pdf_history_row_atomically(ledger, capsys):
    raw = archive_pdf(["Factura TEST-42", "Data emitere: 12.09.2026"])
    run(ledger, raw)
    sha = hashlib.sha256(raw).hexdigest()
    for wrong in ("Data emitere: 12.09.2026", "2026-09-12"):  # a label or a date is never the number
        with pytest.raises(history_pdf.ReviewError):
            history_pdf.correct(ledger, "NORD", sha, wrong)
    assert history_pdf.correct(ledger, "NORD", sha, "TEST-43") == "corrected"
    assert [r["number"] for r in history.rows(ledger, "NORD")] == ["TEST-43"]
    run(ledger, ubl(number="TEST-50", issue="2026-09-12", total="119.00", client_cui="RO77777777"))
    assert cli.main(["history", "correct", "NORD", sha, "--number", "TEST 50"]) == 0  # the XML already has it
    assert [r["number"] for r in history.rows(ledger, "NORD")] == ["TEST-50"] and count(ledger, "history") == 1
    assert cli.main(["history", "correct", "NORD", sha, "--remove"]) == 1  # no PDF row left to fix
    assert history_pdf.document(ledger, "NORD", sha) == raw and not history_pdf.pending(ledger, "NORD")


def test_two_connections_removing_one_pdf_row_one_wins(ledger):
    raw = archive_pdf(["Factura TEST-42", "Data emitere: 12.09.2026"])
    run(ledger, raw)
    results = []

    def remove():
        try:
            results.append(history_pdf.correct(Ledger(timeout=30), "NORD", hashlib.sha256(raw).hexdigest(), None))
        except history_pdf.ReviewError:
            results.append("refused")

    threads = [threading.Thread(target=remove) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["refused", "removed"] and history.rows(ledger, "NORD") == []
    assert count(ledger, "issued") == count(ledger, "efactura") == 0


def test_correct_refuses_xml_companion_and_tracked_rows(ledger):
    run(ledger, ubl(number="TEST-50", issue="2026-09-12", total="119.00", client_cui="RO77777777"))
    (xml_row,) = history.rows(ledger, "NORD")
    raw = archive_pdf(["Factura TEST-42", "Data emitere: 12.09.2026"])
    run(ledger, raw)
    pdf = history.rows(ledger, "NORD")[1]
    companion = archive_pdf(["Factura TEST-44", "Data emitere: 12.09.2026"])
    run(ledger, companion)
    history_pdf.attach(ledger, "NORD", xml_row["xml_sha"], companion)  # later paired with an XML
    history.track(ledger, "NORD", xml_row["xml_sha"])
    ledger.db.execute("UPDATE issued SET number='TEST-42'")  # the PDF's invoice is now tracked
    for sha in (xml_row["xml_sha"], pdf["xml_sha"], hashlib.sha256(companion).hexdigest()):
        with pytest.raises(history_pdf.ReviewError):
            history_pdf.correct(ledger, "NORD", sha, None)
    assert count(ledger, "history") == 3 and not history_pdf.pending(ledger, "NORD")


def test_correct_refuses_a_number_for_a_partner_without_tax_id(ledger):
    raw = archive_pdf(["Factura TEST-42", "Data emitere: 12.09.2026"])
    run(ledger, raw)
    ledger.db.execute("UPDATE history SET party_tax_id=''")  # as a manual review may leave it
    with pytest.raises(history_pdf.ReviewError):
        history_pdf.correct(ledger, "NORD", hashlib.sha256(raw).hexdigest(), "TEST-43")
    assert [r["number"] for r in history.rows(ledger, "NORD")] == ["TEST-42"]


def test_received_invoice_tracked_by_cui_alone_is_known(ledger):
    run(ledger, supplier_ubl(number="FZ 7", issue="2026-10-12"))
    ledger.db.execute("UPDATE efactura SET supplier_tax_id=NULL")
    data = dict(direction="received", number="FZ 7", issue_date="2026-10-12", party_tax_id="77777777")
    assert history_pdf.tracked(ledger, "NORD", data)


def test_oversized_archive_companion_is_not_attached_truncated(ledger, monkeypatch):
    xml = ubl(issue="2026-09-12")
    monkeypatch.setattr(history, "MAX_MEMBER", len(xml) + 1)
    data = zipped(("TEST_1_Partner.pdf", b"%PDF-" + b" " * len(xml)), ("TEST_2_Partner.xml", xml))
    result = run(ledger, data)
    assert result.history_issued == 1 and result.unreadable_count == 1
    assert api._history(ledger, "NORD")[0]["document"] is None


@pytest.mark.parametrize("way", ["issued", "received"])
@pytest.mark.parametrize(
    "start,day,historic",
    [(START, "2026-09-30", True), (START, START, False), (START, "2026-10-02", False), (None, "2026-09-30", False)],
)
def test_sync_start_boundary(ledger, way, start, day, historic):
    if start:
        ledger.set_meta("history_start:NORD", start)
    xml = (ubl if way == "issued" else supplier_ubl)(issue=day)
    store = issued.store if way == "issued" else efactura.store
    for mid in ("8101", "8102"):  # another message for the same invoice also stays one history row
        result = store(ledger, "NORD", {"id": mid}, anaf_zip(xml))
    table = "issued" if way == "issued" else "efactura"
    assert count(ledger, "history") == int(historic)
    assert count(ledger, table) == (0 if historic else 2)
    assert result["status"] == ("history" if historic else "duplicate")
    assert len(history.rivals(ledger, "NORD", way)) == int(historic)


@pytest.mark.parametrize("way", ["issued", "received"])
def test_owner_archives_existing_open_only_after_start_is_set(ledger, way):
    xml = (ubl if way == "issued" else supplier_ubl)(issue="2026-09-01")
    store = issued.store if way == "issued" else efactura.store
    store(ledger, "NORD", {"id": "8201"}, anaf_zip(xml))
    run(ledger, xml)  # start set after sync: conservatively keep existing tracked debt
    table = "issued" if way == "issued" else "efactura"
    assert ledger.db.execute(f"SELECT status FROM {table}").fetchone()[0] == "open"
    # Recreate the real-install overlap using the shared importer storage, synthetic XML only.
    inv, sha = efactura.invoice_from_zip(anaf_zip(xml))
    with ledger.tx():
        history_invoices.store(
            ledger, "NORD", way, inv, sha, Path(ledger.db.execute(f"SELECT raw_path FROM {table}").fetchone()[0])
        )
    with pytest.raises(ValueError):
        history.archive_open(ledger, "NORD", way, ["8201", "missing"])
    assert ledger.db.execute(f"SELECT status FROM {table}").fetchone()[0] == "open"
    history.archive_open(ledger, "NORD", way, ["8201"])
    assert len(history.rows(ledger, "NORD", way)) == 1
    assert not ledger.db.execute("SELECT 1 FROM settled_tx").fetchone()
    row = history.rows(ledger, "NORD", way)[0]
    if way == "issued":
        assert run(ledger, unpaid_xlsx(((row["number"], row["issue_date"], row["total"]),))).issued == 1
    else:
        assert cli.main(["history", "track", "NORD", row["xml_sha"]]) == 0
    assert not history.rows(ledger, "NORD", way)
    assert ledger.db.execute(f"SELECT status FROM {table}").fetchone()[0] == "open"


@pytest.mark.parametrize("way", ["issued", "received"])
def test_sync_history_can_be_explicitly_tracked(ledger, way):
    ledger.set_meta("history_start:NORD", START)
    xml = (ubl if way == "issued" else supplier_ubl)(issue="2026-09-01")
    (issued.store if way == "issued" else efactura.store)(ledger, "NORD", {"id": "8301"}, anaf_zip(xml))
    row = history.rows(ledger, "NORD", way)[0]
    history.track(ledger, "NORD", row["xml_sha"])
    assert not history.rows(ledger, "NORD", way)
    table = "issued" if way == "issued" else "efactura"
    assert ledger.db.execute(f"SELECT status FROM {table}").fetchone()[0] == "open"


def test_import_sets_start_while_sync_is_parsing(ledger, monkeypatch):
    ready, resume = threading.Event(), threading.Event()
    original = issued.invoice_from_zip
    errors = []
    xml = ubl(issue="2026-09-01")

    def paused(raw):
        result = original(raw)
        ready.set()
        assert resume.wait(5)
        return result

    def sync():
        other = Ledger(ledger.path)
        try:
            issued.store(other, "NORD", {"id": "8401"}, anaf_zip(xml))
        except Exception as exc:
            errors.append(exc)
        finally:
            other.close()

    monkeypatch.setattr(issued, "invoice_from_zip", paused)
    worker = threading.Thread(target=sync)
    worker.start()
    try:
        assert ready.wait(5)
        run(ledger, xml)
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert count(ledger, "issued") == 0 and len(history.rows(ledger, "NORD")) == 1


@pytest.mark.parametrize(
    "way,status", [("issued", "marked"), ("issued", "collected"), ("received", "paid"), ("received", "linked")]
)
def test_archive_refuses_payment_state(ledger, way, status):
    xml = (ubl if way == "issued" else supplier_ubl)(issue="2026-09-01")
    (issued.store if way == "issued" else efactura.store)(ledger, "NORD", {"id": "8501"}, anaf_zip(xml))
    ledger.set_meta("history_start:NORD", START)
    inv, sha = efactura.invoice_from_zip(anaf_zip(xml))
    table = "issued" if way == "issued" else "efactura"
    with ledger.tx():
        history_invoices.store(ledger, "NORD", way, inv, sha, Path("unused"))
        ledger.db.execute(f"UPDATE {table} SET status=?", (status,))
    with pytest.raises(ValueError):
        history.archive_open(ledger, "NORD", way, ["8501"])
    assert ledger.db.execute(f"SELECT status FROM {table}").fetchone()[0] == status


@pytest.mark.parametrize("way", ["issued", "received"])
@pytest.mark.parametrize("original", ["tracked", "history", "legacy"])
def test_old_storno_still_reduces_tracked_or_reopened_invoice(ledger, way, original):
    build = ubl if way == "issued" else supplier_ubl
    store = issued.store if way == "issued" else efactura.store
    if original == "history":
        ledger.set_meta("history_start:NORD", START)
    store(ledger, "NORD", {"id": "8601"}, anaf_zip(build(number="OLD 1", issue="2026-09-01", total="119.00")))
    ledger.set_meta("history_start:NORD", START)
    credit = build(number="COR 1", issue="2026-09-02", total="-119.00").replace(
        b"</Invoice>",
        b"<cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>OLD 1</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference></Invoice>",
    )
    store(ledger, "NORD", {"id": "8602"}, anaf_zip(credit))
    table = "issued" if way == "issued" else "efactura"
    if original != "tracked":
        # Simulate a credit retained by older history imports, before the new policy.
        inv, sha = efactura.invoice_from_zip(anaf_zip(credit))
        row = ledger.db.execute(f"SELECT * FROM {table} WHERE message_id='8602'").fetchone()
        with ledger.tx():
            history_invoices.store(ledger, "NORD", way, inv, sha, Path(row["raw_path"]))
            ledger.db.execute(f"DELETE FROM {table} WHERE message_id='8602'")
        if original == "history":
            history.track(ledger, "NORD", history.rows(ledger, "NORD", way)[0]["xml_sha"])
        else:
            run(ledger, credit)  # documented repair for already-tracked legacy debts
    invoice = ledger.db.execute(f"SELECT message_id FROM {table} WHERE doc_type='invoice'").fetchone()[0]
    credits = issued._credited(ledger, "NORD") if way == "issued" else efactura.credited(ledger, "NORD")
    assert credits == {invoice: Decimal("119.00")}
    if way == "received":
        assert efactura.unpaid(ledger, "NORD") == []


@pytest.mark.parametrize("related", [False, True])
@pytest.mark.parametrize("way,method", [("issued", "track"), ("issued", "unpaid"), ("received", "track")])
def test_tracking_only_requires_related_history_credit_evidence(ledger, home, related, way, method):
    build = ubl if way == "issued" else supplier_ubl
    run(ledger, build(number="OLD 1", issue="2026-09-01", total="119.00"))
    xml = build(number="COR 1", issue="2026-09-02", total="-119.00")
    inv = efactura.parse_ubl(xml)
    inv.billing_ref = "OLD 1" if related else "OTHER 2"
    with ledger.tx():
        history_invoices.store(ledger, "NORD", way, inv, "a" * 64, home / "history" / "NORD" / "missing.zip")
    target = next(r for r in history.rows(ledger, "NORD", way) if r["number"] == "OLD 1")

    def change():
        if method == "unpaid":
            return run(ledger, unpaid_xlsx(((target["number"], target["issue_date"], target["total"]),)))
        return history.track(ledger, "NORD", target["xml_sha"])

    if related:
        with pytest.raises(history.Refused):
            change()
        assert len(history.rows(ledger, "NORD", way)) == 2
    else:
        change()
        assert [r["number"] for r in history.rows(ledger, "NORD", way)] == ["COR 1"]
    assert count(ledger, "issued" if way == "issued" else "efactura") == int(not related)
