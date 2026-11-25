"""Issued e-Factura invoices (sent messages) and their collections: synthetic UBL and fake ANAF answers only."""

import io
import json
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal

import anaf_fake
import pytest
from firms import iban as synthetic_iban
from test_efactura import _damaged
from test_invoicing import DATA, LINE, draft, race, setup_invoice as setup_invoice  # noqa: F401
from ubl_fake import ubl

from fiscalm import links
from fiscalm.api import state as api
from fiscalm.bank import statements
from fiscalm.efactura import client as anaf, inbox as efactura
from fiscalm.invoicing import engine as invoicing, issued
from fiscalm.ledger import Ledger

NORD_CUI, STUDIO9_CUI = "11111111", "33333333"


CLIENT_IBAN = synthetic_iban("RO00INGB0000999900004444")
OTHER_IBAN = synthetic_iban("RO00BTRL0000999900005555")


def anaf_zip(xml: bytes, mid="7001") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{mid}.xml", xml)
        z.writestr(f"semnatura_{mid}.xml", b"<Signature/>")
    return buf.getvalue()


def sent(mid, listed="202610111200"):
    return {"id": mid, "id_solicitare": f"5{mid}", "data_creare": listed, "tip": "FACTURA TRIMISA"}


def received(mid, listed="202610111200"):
    return {"id": mid, "id_solicitare": f"5{mid}", "data_creare": listed, "tip": "FACTURA PRIMITA"}


@pytest.fixture
def sec(sec):
    L = Ledger()
    anaf_fake.connect(L)
    L.close()


class FakeAnaf:
    """Answers `_get` like ANAF: list pages per (cif, filtru), downloads by id; records every call."""

    def __init__(self, lists, blobs, per_page=2):
        self.lists, self.blobs, self.per_page, self.calls = lists, blobs, per_page, []

    def __call__(self, path, params, token):
        self.calls.append((path, dict(params)))
        if path == "descarcare":
            return 200, self.blobs[str(params["id"])], "application/zip"
        msgs = self.lists.get((params["cif"], params["filtru"]), [])
        if not msgs:
            return 200, json.dumps({"eroare": f"Nu exista mesaje in ultimele zile pentru {params['cif']}"}).encode(), ""
        pages = max(1, -(-len(msgs) // self.per_page))
        p = params["pagina"]
        chunk = msgs[(p - 1) * self.per_page : p * self.per_page]
        return 200, json.dumps({"mesaje": chunk, "numar_total_pagini": pages}).encode(), "application/json"

    def downloads(self):
        return [p["id"] for path, p in self.calls if path == "descarcare"]


def _fake(monkeypatch, lists, blobs, per_page=2):
    f = FakeAnaf(lists, blobs, per_page)
    monkeypatch.setattr(anaf, "_get", f)
    return f


def _stored(ledger, monkeypatch, **invoices):
    blobs = {mid: anaf_zip(x, mid) for mid, x in invoices.items()}
    _fake(monkeypatch, {(NORD_CUI, "T"): [sent(m) for m in blobs]}, blobs)
    res = issued.sync(ledger)
    assert not res["errors"]
    return res


def _credit(amount, d, counterparty="BETA SOFTWARE SRL", iban=CLIENT_IBAN, desc=None, currency="RON"):
    return statements.Tx(
        date=d,
        title="Incasare OP",
        description=desc if desc is not None else f"Incasare;{counterparty};{iban};REF: C{amount}{d}",
        direction="credit",
        amount=Decimal(amount),
        currency=currency,
        iban="RO24BTRLRONCRT0000000001",
        ref=f"C{amount}{d}",
        counterparty=counterparty,
        counterparty_iban=iban,
    )


def _st(*txs, company="NORD"):
    return statements.Statement(company, None, None, [statements.Account("x", "RON", list(txs))])


def _row(ledger, mid, company="NORD", today=date(2026, 10, 27)):
    return next(r for r in issued.issued(ledger, company, today)["rows"] if r["message_id"] == mid)


# ------------------------------------------------------------------ ANAF listing: filter, window, paging


def test_list_sent_uses_filter_T_pages_and_keeps_only_sent(monkeypatch):
    msgs = [sent(str(i)) for i in range(5)] + [received("99")]
    f = _fake(monkeypatch, {(NORD_CUI, "T"): msgs}, {}, per_page=2)
    end = datetime(2026, 10, 27, 8, 0)
    out = anaf.list_sent(NORD_CUI, end - efactura.WINDOW, end, "tok")
    assert [m["id"] for m in out] == ["0", "1", "2", "3", "4"]  # the PRIMITA one on page 3 is dropped
    lists = [p for path, p in f.calls if path == "listaMesajePaginatieFactura"]
    assert [p["pagina"] for p in lists] == [1, 2, 3] and {p["filtru"] for p in lists} == {"T"}
    span = lists[0]["endTime"] - lists[0]["startTime"]
    assert 0 < span <= 60 * 86400 * 1000  # ANAF refuses more than 60 days


def test_sync_window_is_the_60_day_one(ledger, home, sec, monkeypatch):
    seen = []
    monkeypatch.setattr(anaf, "list_sent", lambda cif, s, e, t: seen.append((cif, s, e)) or [])
    now_dt = datetime(2026, 10, 27, 8, 30)
    issued.sync(ledger, now_dt)
    assert sorted(c for c, _, _ in seen) == sorted([NORD_CUI, STUDIO9_CUI])
    for _, s, e in seen:
        assert e <= now_dt and timedelta(days=59) < e - s <= timedelta(days=60)


def test_sync_both_directions_without_mixing(ledger, home, sec, monkeypatch):
    out_xml = ubl()
    in_xml = ubl(number="FZ 1", supplier_cui="RO99999999", client="Atelier Nord SRL", client_cui=NORD_CUI)
    studio9_xml = ubl(number="S9 0001", supplier_cui="RO33333333", total="500.00")
    blobs = {"7001": anaf_zip(out_xml, "7001"), "8001": anaf_zip(in_xml, "8001"), "7101": anaf_zip(studio9_xml, "7101")}
    f = _fake(
        monkeypatch,
        {(NORD_CUI, "T"): [sent("7001")], (NORD_CUI, "P"): [received("8001")], (STUDIO9_CUI, "T"): [sent("7101")]},
        blobs,
    )
    assert not efactura.sync(ledger)["errors"]
    assert not issued.sync(ledger)["errors"]
    assert [r["message_id"] for r in ledger.efactura_rows()] == ["8001"]
    assert sorted(r["message_id"] for r in ledger.issued_rows()) == ["7001", "7101"]
    assert ledger.issued("7101")["company"] == "STUDIO9" and ledger.issued("7001")["company"] == "NORD"
    r = ledger.issued("7001")
    assert (
        r["client_name"],
        r["client_cui"],
        r["number"],
        r["issue_date"],
        r["due_date"],
        r["currency"],
        r["total"],
    ) == ("BETA SOFTWARE SRL", "66666666", "NRD 0042", "2026-10-10", "2026-10-25", "RON", "1190.00")
    assert json.loads(r["issues"]) == []
    assert (home / "efactura" / "NORD" / "emise" / "2026-10" / "7001.zip").read_bytes() == blobs["7001"]
    assert (home / "efactura" / "NORD" / "2026-10" / "8001.zip").exists()
    assert not (home / "efactura" / "NORD" / "2026-10" / "7001.zip").exists()
    # received views never see issued rows
    assert [r["message_id"] for r in efactura.unpaid(ledger)] == ["8001"]
    assert sorted(f.downloads()) == ["7001", "7101", "8001"]


def test_download_is_idempotent_and_duplicates_and_bad_zips_are_kept_apart(ledger, home, sec, monkeypatch):
    blobs = {"7001": anaf_zip(ubl(), "7001"), "7002": anaf_zip(ubl(), "7002"), "7003": b"PK\x03\x04garbage"}
    f = _fake(monkeypatch, {(NORD_CUI, "T"): [sent(m) for m in blobs]}, blobs)
    assert len(issued.sync(ledger)["new"]) == 3
    issued.sync(ledger)
    assert sorted(f.downloads()) == ["7001", "7002", "7003"]  # never downloaded twice
    assert ledger.issued("7002")["status"] == "duplicate"
    v = issued.issued(ledger, "NORD", date(2026, 10, 27))
    assert [r["message_id"] for r in v["rows"]] == ["7001"] and [u["message_id"] for u in v["unreadable"]] == ["7003"]


def test_failed_downloads_stop_after_three_a_day(ledger, home, sec, monkeypatch):
    monkeypatch.setattr(anaf, "list_sent", lambda cif, s, e, t: [sent("7001")] if cif == NORD_CUI else [])
    calls = []

    def dl(mid, tok):
        calls.append(mid)
        raise anaf.AnafError("boom")

    monkeypatch.setattr(anaf, "download", dl)
    now_dt = datetime(2026, 10, 27, 8, 30)
    for _ in range(5):
        res = issued.sync(ledger, now_dt)
    assert len(calls) == 3 and ledger.issued("7001") is None
    # no fourth attempt today, but the missing invoice is still reported, not cleared as a success
    assert res["errors"] == ["NORD: message 7001 failed to download 3 times today; retried tomorrow"]
    assert "7001" in ledger.get_meta("issued_last_error")
    assert issued.sync(ledger, now_dt)["new"] == [] and len(calls) == 3
    assert "7001" in ledger.get_meta("issued_last_error")


def test_list_error_is_reported_and_quiet_before_authorization(ledger, home, tmp_path, monkeypatch):
    monkeypatch.setenv("FISCALM_SECRETS", str(tmp_path / "nosecrets"))
    assert issued.sync(ledger).get("not_authorized")
    anaf_fake.connect(ledger)
    monkeypatch.setattr(anaf, "_get", lambda p, q, t: (200, b'{"eroare": "CIF introdus nu este autorizat"}', ""))
    res = issued.sync(ledger)
    assert len(res["errors"]) == 2 and "nu este autorizat" in ledger.get_meta("issued_last_error")
    assert not ledger.get_meta("efactura_last_error")  # the received side's error stays its own


def test_wrong_supplier_cui_is_flagged(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(supplier_cui="RO12345678")})
    assert "not NORD" in json.loads(ledger.issued("7001")["issues"])[0]


# ------------------------------------------------------------------ credit notes


def test_credit_notes_reduce_what_is_owed(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{
            "7001": ubl(),
            "7002": ubl(root="CreditNote", number="NRD 0050", total="190.00", ref="NRD 0042", issue="2026-10-15"),
            "7003": ubl(number="NRD 0043", total="300.00", issue="2026-10-12"),
            "7004": ubl(
                number="NRD 0051", total="-300.00", ref="NRD0043", issue="2026-10-16"
            ),  # storno: negative invoice
            "7005": ubl(root="CreditNote", number="NRD 0052", total="10.00", ref="NOPE 1", issue="2026-10-17"),
        },
    )
    assert ledger.issued("7004")["doc_type"] == "credit_note" and ledger.issued("7004")["total"] == "300.00"
    v = issued.issued(ledger, "NORD", date(2026, 10, 27))
    rows = {r["message_id"]: r for r in v["rows"]}
    assert (rows["7001"]["owed"], rows["7001"]["credited"], rows["7001"]["status"]) == (
        "1000.00",
        "190.00",
        "neincasata",
    )
    assert rows["7003"]["status"] == "stornata" and rows["7003"]["owed"] == "0.00"
    notes = {n["message_id"]: n["applied_to"] for n in v["credit_notes"]}
    assert notes == {"7002": "7001", "7004": "7003", "7005": None}
    assert v["totals"]["not_collected"] == {"RON": "1000.00"}
    assert v["totals"]["issued_month"] == {"RON": f"{1190 + 300 - 190 - 300 - 10:.2f}"}
    # the statement credit for what is still owed collects it; the full original amount does not
    full = _credit("1190.00", date(2026, 10, 20))
    assert issued.match_statement(ledger, _st(full)) == []
    assert issued.match_statement(ledger, _st(_credit("1000.00", date(2026, 10, 21)))) == ["7001"]
    assert issued.match_statement(ledger, _st(_credit("300.00", date(2026, 10, 21)))) == []  # nothing owed on 7003


# ------------------------------------------------------------------ collections from statement credits


def test_credit_from_client_name_collects_once(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(), "7002": ubl(number="NRD 0043", issue="2026-10-11")})
    st = _st(
        _credit("1190.00", date(2026, 10, 20), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN + ";fact NRD 0043")
    )
    assert issued.match_statement(ledger, st) == ["7002"]  # the credit names this invoice
    assert issued.match_statement(ledger, st) == []  # one credit never settles a second invoice
    assert ledger.issued("7001")["status"] == "open"
    r = _row(ledger, "7002")
    assert (r["status"], r["how"], r["owed"]) == ("incasata", "statement", "0.00")
    assert ledger.issued("7002")["collected_from_iban"] == CLIENT_IBAN
    assert (
        ledger.issued("7002")["collected_into_iban"] == "RO24BTRLRONCRT0000000001"
    )  # the company account it landed in


def test_ambiguous_credit_settles_nothing(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(), "7002": ubl(number="NRD 0043", issue="2026-10-11")})
    st = _st(_credit("1190.00", date(2026, 10, 20), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN + ";servicii"))
    assert issued.match_statement(ledger, st) == []
    assert {r["status"] for r in issued.issued(ledger, "NORD")["rows"]} == {"neincasata"}


@pytest.mark.parametrize(
    "client,cui,counterparty,desc,how",
    [
        ("ION POPESCU", "", "POPESCU ION", "plata NRD 0010", "ref"),  # a private buyer, the invoice named
        ("ION POPESCU", "", "ION POPESCU", "", "name"),  # a private buyer by name
        ("BETA SOFTWARE SRL", "RO66666666", "BETA SOFTW", "servicii", "amount"),  # abbreviated, one of its amount
        ("BETA SOFTWARE SRL", "RO66666666", "MAMA HOLDING SRL", "Plata factura NRD 0010", "ref"),  # someone else pays
        ("BETA SOFTWARE SRL", "RO66666666", "GAMMA SRL", "Plata factura NRD 0010", "ref"),  # another client pays it
    ],
)
def test_a_credit_collects_the_one_invoice_it_fits(
    ledger, home, sec, monkeypatch, client, cui, counterparty, desc, how
):
    _stored(ledger, monkeypatch, **{"7001": ubl(number="NRD 0010", total="350.00", client=client, client_cui=cui)})
    _stored(ledger, monkeypatch, **{"7002": ubl(number="NRD 0011", total="1200.00", client="GAMMA SRL")})
    t = _credit("350.00", date(2026, 10, 20), counterparty=counterparty, iban=OTHER_IBAN, desc=desc)
    assert issued.match_statement(ledger, _st(t)) == ["7001"]
    row = ledger.issued("7001")
    assert f" {how} " in row["collected_note"] and row["collected_from_iban"] is None  # no client CUI shown


def test_two_credits_of_one_amount_collect_nothing_by_amount(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl()})
    a = _credit("1190.00", date(2026, 10, 20), counterparty="ALFA", iban=OTHER_IBAN, desc="x")
    b = _credit("1190.00", date(2026, 10, 21), counterparty="OMEGA", iban=OTHER_IBAN, desc="y")
    assert issued.match_statement(ledger, _st(a, b)) == []
    assert issued.match_statement(ledger, _st(a, a)) == []  # two lines alike (one key) are two credits too
    _stored(ledger, monkeypatch, **{"7002": ubl(number="NRD 0043", total="5.00", client="GAMMA LABS SRL")})
    gamma = _credit("1190.00", date(2026, 10, 20), counterparty="GAMMA LABS SRL", iban=OTHER_IBAN, desc="x")
    assert statements.tx_key(gamma) == statements.tx_key(a)  # another payer on a line keyed like a's
    assert issued.match_statement(ledger, _st(a, gamma, b)) == []  # a and b still both claim it
    assert issued.match_statement(ledger, _st(a, _credit("1190.00", date(2026, 10, 22)))) == ["7001"]  # the client's
    assert "name" in ledger.issued("7001")["collected_note"]


def test_a_credit_never_trades_its_client_for_an_amount(ledger, home, sec, monkeypatch):
    one = dict(total="100.00", issue="2026-10-11")
    _stored(ledger, monkeypatch, **{"7001": ubl(client="ALPHA SOFT SRL", **one)})
    _stored(
        ledger, monkeypatch, **{"7002": ubl(number="NRD 0043", client="BETA LABS SRL", client_cui="RO77777777", **one)}
    )
    alpha = [
        _credit("100.00", date(2026, 10, d), counterparty="ALPHA SOFT SRL", iban="", desc=f"p{d}") for d in (20, 21)
    ]
    assert issued.match_statement(ledger, _st(*alpha)) == ["7001"]  # the second Alpha credit never pays Beta
    assert issued.match_statement(ledger, _st(*alpha)) == []  # nor on the next sync, once Alpha's is closed


@pytest.mark.parametrize("gamma_first", [True, False])
def test_an_account_shown_on_the_statement_pays_no_one_else_by_amount(ledger, home, sec, monkeypatch, gamma_first):
    _stored(ledger, monkeypatch, **{"7001": ubl(total="200.00", client="BETA LABS SRL", client_cui="RO77777777")})
    _stored(ledger, monkeypatch, **{"7002": ubl(number="NRD 0043", total="100.00", client="GAMMA SRL")})
    gamma = _credit("100.00", date(2026, 10, 20), counterparty="X", iban=OTHER_IBAN, desc="CUI 66666666")
    other = _credit("200.00", date(2026, 10, 21), counterparty="XYZ", iban=OTHER_IBAN, desc="servicii")
    assert issued.match_statement(ledger, _st(*((gamma, other) if gamma_first else (other, gamma)))) == ["7002"]
    partial = _credit("40.00", date(2026, 10, 22), counterparty="BETA LABS SRL", iban=CLIENT_IBAN, desc="avans")
    abbreviated = _credit("200.00", date(2026, 10, 22), counterparty="BL", iban=CLIENT_IBAN, desc="x")
    assert issued.match_statement(ledger, _st(partial, abbreviated)) == []  # its partial payment showed Beta


@pytest.mark.parametrize(
    "numbers,desc,collected",
    [
        (("NRD 0042", "NRD 0043"), "comanda 0042", []),
        (("NRD 0042", "NRD 0043"), "NRD 0042 si NRD 0043", []),
        (("NRD 0042", "NRD 0043"), "NRD 0042", ["7001"]),
        (("10", "99"), "servicii 2026-10-20", []),  # a date is no invoice number
    ],
)
def test_only_one_whole_identifier_picks_among_amount_twins(ledger, home, sec, monkeypatch, numbers, desc, collected):
    _stored(ledger, monkeypatch, **{"7001": ubl(number=numbers[0]), "7002": ubl(number=numbers[1], client="GAMMA SRL")})
    anonymous = _credit("1190.00", date(2026, 10, 21), counterparty="Y", iban="", desc="plata")
    named = _credit("1190.00", date(2026, 10, 20), counterparty="NEW PAYER", iban=OTHER_IBAN, desc=desc)
    assert issued.match_statement(ledger, _st(anonymous, named)) == collected
    assert issued.match_statement(ledger, _st(anonymous, named)) == []  # the same statement again: the same answer


@pytest.mark.parametrize(
    "numbers,desc,collected",
    [(("10", "99"), "servicii 2026-10-20", []), (("NRD 0042", "NRD 0043"), "Plata factura 0042", ["7001"])],
)
def test_a_number_without_series_never_chooses_between_a_clients_invoices(
    ledger, home, sec, monkeypatch, numbers, desc, collected
):
    _stored(ledger, monkeypatch, **{"7001": ubl(number=numbers[0], client="ION POPESCU", client_cui="")})
    _stored(ledger, monkeypatch, **{"7002": ubl(number=numbers[1], client="ION POPESCU", client_cui="")})
    t = _credit("1190.00", date(2026, 10, 20), counterparty="ION POPESCU", iban="", desc=desc)
    assert issued.match_statement(ledger, _st(t)) == collected  # digits of a series invoice still pick


def test_two_connections_matching_one_statement_collect_once(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl()})
    st = _st(_credit("1190.00", date(2026, 10, 20), counterparty="X", iban=OTHER_IBAN, desc="servicii"))
    won, _ = race(ledger.path, lambda other, k: issued.match_statement(other, st))
    assert sorted(won) == [[], ["7001"]]
    assert ledger.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 1


def test_the_choice_and_the_write_hold_one_write_lock(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl()})
    seen, build = [], issued._Pool.__init__

    def locked(self, L, *a):  # another connection can't take the write lock while the choice is made
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            sqlite3.connect(L.path, timeout=0).execute("BEGIN IMMEDIATE")
        seen.append(True)
        build(self, L, *a)

    monkeypatch.setattr(issued._Pool, "__init__", locked)
    assert issued.match_statement(ledger, _st(_credit("1190.00", date(2026, 10, 20)))) == ["7001"] and seen == [True]


def test_a_private_name_from_a_client_account_collects_nothing(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(client="GAMMA LABS SRL", client_cui="RO77777777", total="50.00")})
    _stored(ledger, monkeypatch, **{"7002": ubl(number="NRD 0043", client="ION POPESCU", client_cui="")})
    gamma = _credit("50.00", date(2026, 10, 20), counterparty="X", iban=OTHER_IBAN, desc="CUI 77777777")
    assert issued.match_statement(ledger, _st(gamma)) == ["7001"]
    person = _credit("1190.00", date(2026, 10, 21), counterparty="ION POPESCU", iban=OTHER_IBAN, desc="")
    assert issued.match_statement(ledger, _st(person)) == []


def _owed(*totals):
    return {f"70{i + 1:02d}": ubl(number=f"NRD 00{i + 50}", total=t, issue="2026-10-11") for i, t in enumerate(totals)}


@pytest.mark.parametrize(
    "totals,desc,collected",
    [
        (("400.00", "600.00"), "plata", ["7001", "7002"]),  # the client's two open invoices, one transfer
        (("400.00", "600.00", "300.00", "700.00"), "plata", []),  # 400+600 and 300+700: which ones is a guess
        (("400.00", "600.00", "300.00", "700.00"), "NRD 0050 NRD 0051", ["7001", "7002"]),  # it names them
        (("400.00", "600.00", "300.00", "700.00"), "0050 0051", []),  # digits alone pick no set
        (("100.00",) * 6 + ("400.00",), "plata", ["7001", "7002", "7003", "7004", "7005", "7006", "7007"]),  # all
        (("1000.00", "400.00", "600.00"), "plata", ["7001"]),  # one invoice of the amount comes first
    ],
)
def test_one_credit_collects_the_one_exact_set_of_its_clients_invoices(
    ledger, home, sec, monkeypatch, totals, desc, collected
):
    _stored(ledger, monkeypatch, **_owed(*totals))
    t = _credit("1000.00", date(2026, 10, 20), desc=desc)
    assert sorted(issued.match_statement(ledger, _st(t))) == collected
    if collected:
        assert {ledger.issued(m)["collected_tx"] for m in collected} == {statements.tx_key(t)}
        assert ledger.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 1


def test_a_set_needs_its_client_and_one_credit(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **_owed("400.00", "600.00"))
    anonymous = _credit("1000.00", date(2026, 10, 20), counterparty="XYZ", iban=OTHER_IBAN, desc="NRD 0050 NRD 0051")
    assert issued.match_statement(ledger, _st(anonymous)) == []
    twins = [_credit("1000.00", date(2026, 10, d)) for d in (20, 21)]  # both would pay the same pair: neither does
    assert issued.match_statement(ledger, _st(*twins)) == []


def test_a_set_is_collected_whole_or_not_at_all(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **_owed("400.00", "600.00", "300.00"))
    assert ledger.mark_issued_collected("7002", "statement", "tx-earlier")
    assert ledger.mark_issued_collected(("7001", "7002"), "statement", "tx-set") is False
    assert ledger.issued("7001")["status"] == "open" and "tx-set" not in ledger.used_txs()
    won, _ = race(
        ledger.path, lambda other, k: other.mark_issued_collected(("7001", "7003")[k:] + ("7001",)[:k], "s", f"tx-{k}")
    )
    assert sorted(won) == [False, True] and ledger.issued("7001")["status"] == "collected"
    assert ledger.db.execute("SELECT count(*) FROM settled_tx WHERE tx_key LIKE 'tx-_'").fetchone()[0] == 1


def test_cui_on_credit_and_then_the_learned_iban(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{
            "7001": ubl(client="GAMMA LABS SRL"),
            "7002": ubl(client="GAMMA LABS SRL", number="NRD 0060", issue="2026-10-14", total="400.00"),
        },
    )
    # the bank shows a trading name, not the legal one; the CUI is in the details
    first = _credit(
        "1190.00", date(2026, 10, 20), counterparty="GMLABS", desc="Incasare;GMLABS;" + CLIENT_IBAN + ";CUI 66666666"
    )
    assert issued.match_statement(ledger, _st(first)) == ["7001"] and "cui" in ledger.issued("7001")["collected_note"]
    later = _credit("400.00", date(2026, 10, 24), counterparty="GMLABS", desc="Incasare;GMLABS;" + CLIENT_IBAN + ";")
    assert issued.match_statement(ledger, _st(later)) == ["7002"] and "iban" in ledger.issued("7002")["collected_note"]


def test_wrong_client_amount_debit_or_own_transfer_never_collects(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{"7001": ubl(), "7002": ubl(number="NRD 0043", client="GAMMA LABS SRL", client_cui="RO77777777")},
    )
    gamma = _credit(
        "1190.00", date(2026, 10, 11), iban=OTHER_IBAN, desc="Incasare;GMLABS;CUI 77777777", counterparty="X"
    )
    assert issued.match_statement(ledger, _st(gamma)) == ["7002"]  # OTHER_IBAN is now GAMMA's account
    other = _credit("1190.00", date(2026, 10, 20), counterparty="DELTA SOFTWARE SRL", iban=OTHER_IBAN, desc="servicii")
    card_refund = _credit("1190.00", date(2026, 10, 20), counterparty="BETA SOFTWARE SRL", iban="", desc="RETUR")
    card_refund.card_last4 = "4242"
    # no account it came from: a refund without its card number, interest the bank's text doesn't name
    no_account = _credit("1190.00", date(2026, 10, 20), counterparty="", iban="", desc="RETUR")
    amount = _credit("1189.99", date(2026, 10, 20))
    early = _credit("1190.00", date(2026, 9, 1))
    debit = _credit("1190.00", date(2026, 10, 20))
    debit.direction = "debit"
    own = _credit("1190.00", date(2026, 10, 20), iban="RO71BTRLEURCRT0000000001")
    for t in (other, card_refund, no_account, amount, early, debit, own):
        assert issued.match_statement(ledger, _st(t)) == []
    assert ledger.issued("7001")["status"] == "open"
    series = _credit("1190.00", date(2026, 10, 21), desc="plata CARD 0042")  # the client's own words: no refund
    assert issued.match_statement(ledger, _st(series)) == ["7001"]


def test_credit_that_settled_one_invoice_never_settles_another(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl()})
    t = _credit("1190.00", date(2026, 10, 20))
    assert issued.match_statement(ledger, _st(t)) == ["7001"]
    # a second identical invoice arrives later: the same statement line, re-read on the next tick, is spent
    _stored(ledger, monkeypatch, **{"7001": ubl(), "7009": ubl(number="NRD 0099", issue="2026-10-18")})
    assert issued.match_statement(ledger, _st(t)) == []
    assert ledger.issued("7009")["status"] == "open"
    assert ledger.mark_issued_collected("7009", "x", statements.tx_key(t)) is False


def test_eur_invoice_collected_in_eur(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(currency="EUR", total="200.00")})
    assert issued.match_statement(ledger, _st(_credit("200.00", date(2026, 10, 20), currency="RON"))) == []
    assert issued.match_statement(ledger, _st(_credit("200.00", date(2026, 10, 20), currency="EUR"))) == ["7001"]


def test_studio9_credits_never_collect_nord_invoices(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl()})
    assert issued.match_statement(ledger, _st(_credit("1190.00", date(2026, 10, 20)), company="STUDIO9")) == []


# ------------------------------------------------------------------ manual mark, view


def test_manual_mark_and_undo(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(), "7002": ubl(number="NRD 0043", issue="2026-10-11")})
    assert issued.mark_collected(ledger, "7001", "other", " compensare,\n cu factura lor ")
    r = _row(ledger, "7001")
    assert (r["status"], r["how"], r["can_unmark"], r["collected_note"], r["collected_method"]) == (
        "marcata",
        "manual",
        True,
        "user: compensare, cu factura lor",
        "other",
    )
    assert issued.mark_collected(ledger, "7001", "cash") is False  # already marked
    assert issued.unmark_collected(ledger, "7001") and _row(ledger, "7001")["status"] == "neincasata"
    assert issued.unmark_collected(ledger, "7001") is False
    # the user's mark waits for the statement: a credit that doesn't fit leaves it marked, a matching one confirms it
    assert issued.mark_collected(ledger, "7001", "bank")
    assert _row(ledger, "7001")["collected_method"] == "bank"
    other = _credit("999.00", date(2026, 10, 20), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN + ";NRD 0042")
    assert issued.match_statement(ledger, _st(other)) == [] and _row(ledger, "7001")["status"] == "marcata"
    t = _credit("1190.00", date(2026, 10, 20), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN + ";NRD 0042")
    assert issued.match_statement(ledger, _st(t)) == ["7001"]
    r = _row(ledger, "7001")
    assert (r["status"], r["how"]) == ("incasata", "statement")
    # a statement-proved collection can't be undone by hand
    assert issued.unmark_collected(ledger, "7001") is False and _row(ledger, "7001")["status"] == "incasata"


def test_marked_invoice_never_takes_a_credit_that_fits_an_open_one(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(), "7002": ubl(number="NRD 0043", issue="2026-10-11")})
    assert issued.mark_collected(ledger, "7001", "cash")
    t = _credit("1190.00", date(2026, 10, 20), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN)
    assert issued.match_statement(ledger, _st(t)) == []  # fits both: settles neither
    assert (ledger.issued("7001")["status"], ledger.issued("7002")["status"]) == ("marked", "open")
    with pytest.raises(ValueError):
        ledger.mark_issued_manual("7002", "  ", "other")


def test_view_status_days_overdue_and_totals(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{
            "7001": ubl(due="2026-10-20"),  # overdue 7 days
            "7002": ubl(number="NRD 0043", total="500.00", issue="2026-10-20", due="2026-11-19"),  # not due yet
            "7003": ubl(number="NRD 0044", total="80.00", issue="2026-10-21", due=""),  # no due date
            "7004": ubl(number="NRD 0030", total="100.00", issue="2026-09-01", due="2026-09-15", currency="EUR"),
            "7005": ubl(number="NRD 0031", total="250.00", issue="2026-09-02", due="2026-09-10"),
        },
    )
    issued.mark_collected(ledger, "7005", "other", "compensare")
    v = issued.issued(ledger, "NORD", date(2026, 10, 27))
    rows = {r["message_id"]: r for r in v["rows"]}
    assert [r["message_id"] for r in v["rows"]] == ["7003", "7002", "7001", "7005", "7004"]  # newest first
    assert (rows["7001"]["days_overdue"], rows["7002"]["days_overdue"], rows["7003"]["days_overdue"]) == (7, 0, 0)
    assert rows["7004"]["days_overdue"] == 42 and rows["7005"]["days_overdue"] == 0
    assert v["totals"] == {
        "issued_month": {"RON": "1770.00"},
        "not_collected": {"RON": "1770.00", "EUR": "100.00"},
        "overdue": {"RON": "1190.00", "EUR": "100.00"},
        "not_collected_count": 4,
        "overdue_count": 2,
    }
    assert issued.issued(ledger, "STUDIO9")["rows"] == []


def test_cli_issued_lists_uncollected_and_all(ledger, home, sec, monkeypatch, capsys):
    from fiscalm import cli

    _stored(ledger, monkeypatch, **{"7001": ubl(), "7002": ubl(number="NRD 0043", issue="2026-10-11")})
    issued.mark_collected(ledger, "7002", "cash")
    ledger.close()
    assert cli.main(["efactura", "issued", "--company", "NORD"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["message_id"] for r in out[0]["rows"]] == ["7001"]
    assert cli.main(["efactura", "issued", "--all"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [o["company"] for o in out] == ["NORD", "STUDIO9"] and len(out[0]["rows"]) == 2
    assert cli.main(["efactura", "uncollect", "7002"]) == 0
    assert cli.main(["efactura", "collected", "7002", "--method", "other", "--note", "compensare"]) == 0


def test_window_never_exceeds_60_days_across_a_dst_change(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Europe/Bucharest")
    time.tzset()
    try:
        f = _fake(monkeypatch, {}, {})
        end = datetime(2026, 10, 27, 8, 0)  # summer time ended on 25 Oct: the window spans the change
        for lister in (anaf.list_sent, anaf.list_received):
            lister(NORD_CUI, end - efactura.WINDOW, end, "tok")
        for _, p in f.calls:
            assert p["endTime"] - p["startTime"] == int(efactura.WINDOW.total_seconds() * 1000)
        assert [p["filtru"] for _, p in f.calls] == ["T", "P"]
    finally:
        monkeypatch.undo()
        time.tzset()


# ------------------------------------------------------------------ review regressions (PR #13)


def test_shared_first_word_is_not_the_same_client(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(client="BETA SERVICES SRL", client_cui="RO77777777")})
    other = _credit(
        "1190.00",
        date(2026, 10, 20),
        counterparty="BETA TECHNOLOGIES SRL",
        iban=OTHER_IBAN,
        desc="Incasare;BETA TECHNOLOGIES SRL;" + OTHER_IBAN + ";servicii BETA SERVICES",
    )
    # not the client: it collects only as the one invoice of its amount, and its account is not learned
    assert issued.match_statement(ledger, _st(other)) == ["7001"]
    assert "amount" in ledger.issued("7001")["collected_note"] and not ledger.issued("7001")["collected_from_iban"]
    # the exact name (legal forms aside) is the client
    same = _credit("1190.00", date(2026, 10, 21), counterparty="S.C. BETA SERVICES S.R.L.", iban=OTHER_IBAN)
    assert issued.credit_from_client(ledger, "NORD", "BETA SERVICES SRL", "RO77777777", same) == "name"


def test_credit_naming_a_closed_short_number_settles_nothing(ledger, home, sec, monkeypatch):
    _stored(ledger, monkeypatch, **{"7001": ubl(number="NRD 42"), "7002": ubl(number="NRD 43", issue="2026-10-11")})
    assert ledger.mark_issued_collected("7001", "statement 2026-10-15", "tx-earlier")
    t = _credit("1190.00", date(2026, 10, 20), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN + ";factura NRD 42")
    assert issued.match_statement(ledger, _st(t)) == []
    assert ledger.issued("7002")["status"] == "open"
    # the open one, named by its short number (any spacing, leading zeros), is collected
    t2 = _credit("1190.00", date(2026, 10, 21), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN + ";fact NRD-043")
    assert issued.match_statement(ledger, _st(t2)) == ["7002"]


def test_short_numbers_do_not_match_longer_ones():
    rx = issued._ident_re("NRD 42")
    assert rx.search("FACT NRD 42") and rx.search("NRD0042") and rx.search("PLATA NRD 42 SI 43")
    assert not rx.search("NRD 420") and not rx.search("NRD 142") and not rx.search("XELT 42") and not rx.search("NRD 4")
    assert issued._ident_re("") is None and issued._ident_re("FARA NUMAR") is None


def test_debit_and_credit_keys_differ_and_old_debit_keys_stay_valid(ledger, home, sec, monkeypatch):
    import hashlib

    _stored(ledger, monkeypatch, **{"7001": ubl()})
    credit = _credit("1190.00", date(2026, 10, 20))
    debit = _credit("1190.00", date(2026, 10, 20))
    debit.direction = "debit"
    assert statements.tx_key(debit) != statements.tx_key(credit)
    # a debit's key is the one reserved before directions were keyed: existing settled_tx rows still hold
    legacy = hashlib.sha256(
        f"{debit.iban}|{debit.date}|{debit.amount}|{debit.ref}|{debit.description}".encode()
    ).hexdigest()[:20]
    assert statements.tx_key(debit) == legacy
    assert ledger.mark_paid("P1", "statement", legacy) is False  # no such payment: reserve it directly
    with ledger.tx() as db:
        db.execute("INSERT INTO settled_tx VALUES(?,?,?)", (legacy, "payment P1", "2026-10-20"))
    assert statements.tx_key(debit) in ledger.used_txs()
    # the debit reserved by a supplier payment does not block the same-looking credit from collecting
    assert issued.match_statement(ledger, _st(credit)) == ["7001"]
    assert statements.tx_key(credit) in ledger.used_txs()


def test_credit_note_without_identity_refuses_even_the_same_client_name(ledger, home, sec, monkeypatch):
    _stored(
        ledger,
        monkeypatch,
        **{
            "7001": ubl(number="NRD 0042", client="ALPHA SRL", client_cui="RO66666666"),
            "7002": ubl(
                root="CreditNote",
                number="NRD 0050",
                total="190.00",
                ref="NRD 0042",
                client="OTHER CLIENT SRL",
                client_cui="",
                issue="2026-10-15",
            ),
            "7003": ubl(number="NRD 0043", client="ALPHA SRL", client_cui="", total="300.00", issue="2026-10-12"),
            "7004": ubl(
                root="CreditNote",
                number="NRD 0051",
                total="100.00",
                ref="NRD 0043",
                client="S.C. Alpha S.R.L.",
                client_cui="",
                issue="2026-10-16",
            ),
        },
    )
    v = issued.issued(ledger, "NORD", date(2026, 10, 27))
    notes = {n["message_id"]: n["applied_to"] for n in v["credit_notes"]}
    assert notes == {"7002": None, "7004": None}
    rows = {r["message_id"]: r for r in v["rows"]}
    assert rows["7001"]["owed"] == "1190.00" and rows["7003"]["owed"] == "300.00"


@pytest.mark.parametrize("bad", ["../../escaped", "7001/../../x", "/nonexistent-fiscalm/x", "", "7001.zip"])
def test_non_numeric_message_ids_never_reach_the_disk(ledger, home, sec, monkeypatch, bad):
    xml = anaf_zip(ubl(), "7001")
    with pytest.raises(ValueError):
        issued.store(ledger, "NORD", sent(bad), xml)
    with pytest.raises(ValueError):
        efactura.store(ledger, "NORD", received(bad), xml)
    with pytest.raises(ValueError):
        issued.raw_path("../NORD", "7001", "202610")
    monkeypatch.setattr(anaf, "list_sent", lambda cif, s, e, t: [sent(bad)] if cif == NORD_CUI else [])
    monkeypatch.setattr(anaf, "list_received", lambda cif, s, e, t: [received(bad)] if cif == NORD_CUI else [])
    monkeypatch.setattr(anaf, "download", lambda mid, tok: xml)  # even a download that would answer
    assert "non-numeric" in issued.sync(ledger)["errors"][0]
    assert "non-numeric" in efactura.sync(ledger)["errors"][0]
    assert not list(home.parent.glob("escaped*")) and not list(home.glob("escaped*"))
    assert ledger.issued_rows() == [] and ledger.efactura_rows() == []


def test_cli_refuses_another_way_without_a_note(ledger, home, sec, monkeypatch, capsys):
    from fiscalm import cli

    _stored(ledger, monkeypatch, **{"7001": ubl()})
    ledger.close()
    assert cli.main(["efactura", "collected", "7001", "--method", "other", "--note", "   "]) == 2
    assert "--note" in capsys.readouterr().err


def test_lookback_is_13_months(ledger, home, sec, monkeypatch):
    seen = []
    for month in ("2025-08", "2025-09", "2026-10"):
        p = home / f"{month}.pdf"
        p.write_bytes(b"%PDF")
        ledger.add_document(f"sha{month}", "NORD", month, "statement", str(p))
    monkeypatch.setattr(statements, "parse", lambda path, co: seen.append(path.stem) or _st(company=co))
    issued.match_recent_statements(ledger, today=date(2026, 10, 27))
    assert sorted(seen) == ["2025-09", "2026-10"]  # September 2025 is 13 months back; August is out


def test_window_never_exceeds_60_days_across_spring_dst(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Europe/Bucharest")
    time.tzset()
    try:
        f = _fake(monkeypatch, {}, {})
        end = datetime(2026, 4, 10, 8, 0)  # summer time began on 29 March: the window spans the change
        for lister in (anaf.list_sent, anaf.list_received):
            lister(NORD_CUI, end - efactura.WINDOW, end, "tok")
        for _, p in f.calls:
            assert p["endTime"] - p["startTime"] == int(efactura.WINDOW.total_seconds() * 1000)
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("fails", [True, False])
def test_tick_continues_after_sync_issued_fails(tmp_path, fails):
    """A failed step is logged and the tick goes on; saved rules run only after every matcher ran."""
    from fiscalm import scheduler

    calls = tmp_path / "calls"
    fake = tmp_path / "fiscalm"
    fake.write_text(f"""#!/bin/bash
echo "$*" >> {calls}
case "$*" in
  "efactura sync-issued") exit {2 if fails else 0} ;;
  sync) echo '{{"mode":"shadow","actions_needed":[],"fetched":[],"ingested":[],"missing_attachments":[],"drive_archive":[]}}' ;;
  "efactura judge") echo '{{"invoices":0,"suggestions":[]}}' ;;
esac
exit 0
""")
    fake.chmod(0o755)
    fh = tmp_path / "fh"
    (fh / "state").mkdir(parents=True)
    assert scheduler.tick(fh, [str(fake)]) == 0
    ran = calls.read_text().splitlines()
    assert ran.index("efactura sync-issued") < ran.index("efactura judge") < ran.index("invoice partner-status")
    assert ("tx-rules" in ran) is not fails and (fails or ran.index("efactura sync-issued") < ran.index("tx-rules"))
    assert not {"check", "dispatch", "notify"} & set(ran)  # no agent thread
    log = next((fh / "logs").glob("finance-*.log")).read_text()
    assert ("efactura sync-issued failed (rc=2)" in log) is fails and ("saved rules skipped" in log) is fails


def test_full_identifier_wins_over_bare_digits(ledger, home, sec, monkeypatch):
    # A credit naming NRD 0042 (already closed) must not collect STD 0042 through its digits
    _stored(ledger, monkeypatch, **{"7001": ubl(number="NRD 0042"), "7002": ubl(number="STD 0042", issue="2026-10-11")})
    assert ledger.mark_issued_collected("7001", "statement 2026-10-15", "tx-earlier")
    t = _credit("1190.00", date(2026, 10, 20), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN + ";factura NRD 0042")
    assert issued.match_statement(ledger, _st(t)) == []
    assert ledger.issued("7002")["status"] == "open"
    # bare digits alone still name an invoice when no full identifier is written
    t2 = _credit("1190.00", date(2026, 10, 21), desc="Incasare;BETA SOFTWARE SRL;" + CLIENT_IBAN + ";factura 0042")
    assert issued.match_statement(ledger, _st(t2)) == []  # 0042 names both: NRD is closed, so nothing is settled
    assert ledger.issued("7002")["status"] == "open"


def test_a_lone_initial_is_part_of_the_name_not_a_legal_form(ledger, home, sec, monkeypatch):
    from fiscalm.efactura import inbox as efactura

    assert not efactura.same_firm("ALPHA L CONSULTING SRL", "ALPHA R CONSULTING SRL")
    assert efactura.same_firm("S.C. ALPHA L CONSULTING S.R.L.", "ALPHA L CONSULTING SRL")
    assert efactura.same_firm("FURNIZOR EXEMPLU CALCULATOARE SRL", "S. C. FURNIZOR EXEMPLU CALCULATOARE S.R.L.")
    assert efactura.firm_core("A B C SRL") == ["ABC"] and efactura.firm_core("S.A. ALPHA") == ["ALPHA"]
    _stored(
        ledger,
        monkeypatch,
        **{
            "7001": ubl(number="NRD 0042", client="ALPHA R CONSULTING SRL", client_cui=""),
            "7002": ubl(
                root="CreditNote",
                number="NRD 0050",
                total="190.00",
                ref="NRD 0042",
                client="ALPHA L CONSULTING SRL",
                client_cui="",
                issue="2026-10-15",
            ),
        },
    )
    v = issued.issued(ledger, "NORD", date(2026, 10, 27))
    assert {n["message_id"]: n["applied_to"] for n in v["credit_notes"]} == {"7002": None}
    t = _credit(
        "1190.00",
        date(2026, 10, 20),
        counterparty="ALPHA L CONSULTING SRL",
        iban=OTHER_IBAN,
        desc="Incasare;ALPHA L CONSULTING SRL;" + OTHER_IBAN + ";",
    )
    assert issued.credit_from_client(ledger, "NORD", "ALPHA R CONSULTING SRL", "", t) == ""


@pytest.mark.parametrize("bad", ["²", "1" * 31, "٣", "0x1", "1e3"])
def test_unicode_and_too_long_ids_are_skipped_not_crashing_the_sync(ledger, home, sec, monkeypatch, bad):
    from fiscalm.efactura import inbox as efactura

    assert not efactura.valid_id(bad)
    monkeypatch.setattr(anaf, "list_sent", lambda cif, s, e, t: [sent(bad)] if cif == NORD_CUI else [])
    monkeypatch.setattr(anaf, "download", lambda mid, tok: (_ for _ in ()).throw(AssertionError("never downloaded")))
    issued.sync(ledger)  # no exception, nothing stored
    assert not ledger.issued_rows()


def test_sync_keeps_a_damaged_message_and_stores_the_next(ledger, home, sec, monkeypatch):
    blobs = {"7001": _damaged("corrupt lzma"), "7002": anaf_zip(ubl(), "7002")}
    f = _fake(monkeypatch, {(NORD_CUI, "T"): [sent(m) for m in blobs]}, blobs)
    issued.sync(ledger)
    issued.sync(ledger)
    assert sorted(f.downloads()) == ["7001", "7002"]  # the damaged one is not fetched again
    assert ledger.issued("7001")["parse_error"] == "ZIP member compression method 14 refused"
    assert ledger.issued("7002")["status"] == "open" and ledger.issued("7002")["parse_error"] is None


# ------------------------------------------------------------------ invoices issued in fiscalm: their own row


@pytest.fixture
def own(setup_invoice):
    """Issued in a Sandbox install (no LIVE marker) and never read back from SPV: 121.00 + 242.00."""
    L, _ = setup_invoice
    iid = draft(setup_invoice, DATA | dict(lines=[LINE, LINE | dict(unit_price="200")]))
    invoicing.issue(L, iid, DATA["issue_date"], company="NORD", version=1)
    return L, iid, invoicing.import_id("local", iid)


def _client_credit(amount, day=2):
    return _st(_credit(amount, date(2026, 11, day), counterparty="Client Fictiv SRL"))


@pytest.mark.parametrize("how", ["marked", "collected"])
def test_an_invoice_issued_in_sandbox_is_collectable_at_once(own, how):
    L, iid, mid = own
    [row] = api._issued(L, "NORD", date(2026, 11, 2), False)
    assert (row["id"], row["local_id"], row["owed"], row["can_mark"]) == (mid, iid, "363.00", True)
    if how == "marked":
        assert issued.mark_collected(L, mid, "cash")
    else:
        assert issued.match_statement(L, _client_credit("363.00")) == [mid]
    assert L.issued(mid)["status"] == how


@pytest.mark.parametrize("lines,owed", [(None, "0.00"), ([2], "121.00")])
def test_a_storno_reduces_its_invoice_before_any_spv_copy(own, lines, owed):
    L, iid, mid = own
    invoicing.storno(L, iid, "2026-11-01", "Retur", lines, company="NORD")
    row = next(r for r in api._issued(L, "NORD", date(2026, 11, 2), False) if r["id"] == mid)
    assert row["owed"] == owed and links.need(L, "NORD")("issued " + mid) == Decimal(owed)
    assert issued.match_statement(L, _client_credit("363.00")) == []  # the original amount settles nothing
    if lines:
        assert issued.match_statement(L, _client_credit(owed, 3)) == [mid]


def test_two_connections_collecting_an_own_row_settle_it_once(own):
    L, _, mid = own
    won, _ = race(L.path, lambda other, k: other.mark_issued_collected(mid, "statement", f"tx-{k}"))
    assert sorted(won) == [False, True]
    assert L.db.execute("SELECT count(*) FROM settled_tx").fetchone()[0] == 1


def test_reparse_leaves_an_own_row_alone(own):
    L, _, mid = own
    L.db.execute("UPDATE issued SET parse_error='synthetic' WHERE message_id=?", (mid,))
    assert issued.reparse(L) == [] and L.issued(mid)["raw_path"] == ""


@pytest.mark.parametrize(
    ("method", "detail", "code"),
    [("own", "", "method"), ("", "", "method"), ("other", "  ", "detail"), ("other", "x" * 201, "long")],
)
def test_mark_refuses_an_unknown_method_or_a_missing_note(ledger, home, sec, monkeypatch, method, detail, code):
    _stored(ledger, monkeypatch, **{"7001": ubl()})
    with pytest.raises(efactura.PaidMethodError) as e:
        issued.mark_collected(ledger, "7001", method, detail)
    assert e.value.code == code and _row(ledger, "7001")["status"] == "neincasata"
