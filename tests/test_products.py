"""Synthetic catalog writes, firm isolation, schema upgrades and stale edits."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from test_api import _api, _get_json, server as server  # noqa: F401
from test_invoicing import DATA, LINE, setup_invoice as setup_invoice  # noqa: F401
from test_issued import anaf_zip, sent
from ubl_fake import ubl as external_ubl

from fiscalm import cli, invoice_lines, migrations
from fiscalm.api import actions, schema
from fiscalm.invoicing import engine as i, issued, recurring, ubl, workspace
from fiscalm.ledger import Ledger
from fiscalm.products import engine as p, stats

PRODUCT = dict(
    kind="produs",
    name="Obiect exemplu",
    unit="H87",
    unit_price="12.30",
    currency="RON",
    vat_category="S",
    vat_rate="21",
)


def test_catalog_lifecycle_and_firm_isolation(ledger):
    row = p.write(ledger, "NORD", "product-add", PRODUCT)
    assert row["version"] == 1 and row["can_delete"]
    assert p.catalog(ledger, "STUDIO9")["products"] == []
    with pytest.raises(p.ProductError):
        p.get(ledger, "STUDIO9", row["id"])
    changed = p.write(ledger, "NORD", "product-update", dict(id=row["id"], version=1, name="Nou"))
    assert changed["name"] == "Nou" and changed["unit_price"] == "12.30"
    with pytest.raises(p.ProductError):
        p.write(ledger, "NORD", "product-update", dict(id=row["id"], version=1, name="Vechi"))
    archived = p.write(ledger, "NORD", "product-archive", dict(id=row["id"], version=2, archived=True))
    assert archived["archived"]
    p.write(ledger, "NORD", "product-delete", dict(id=row["id"], version=3))
    assert p.catalog(ledger, "NORD")["products"] == []


@pytest.mark.parametrize(
    "fields",
    [
        dict(unit_price="NaN"),
        dict(unit_price="1.001"),
        dict(unit_price="-1"),
        dict(vat_rate="19"),
        dict(vat_category="E", vat_rate="0"),
        dict(vat_category="Z", vat_rate="11"),
        dict(unit="../"),
        dict(name=""),
    ],
)
def test_invalid_defaults_refuse(ledger, fields):
    with pytest.raises(p.ProductError):
        p.write(ledger, "NORD", "product-add", PRODUCT | fields)
    assert not p.catalog(ledger, "NORD")["products"]


def test_referenced_product_cannot_be_deleted(ledger):
    row = p.write(ledger, "NORD", "product-add", PRODUCT)
    with ledger.tx() as db:
        db.execute("INSERT INTO product_references VALUES(?,?,?)", ("NORD", row["id"], "synthetic-draft"))
    assert not p.get(ledger, "NORD", row["id"])["can_delete"]
    with pytest.raises(p.ProductError):
        p.write(ledger, "NORD", "product-delete", dict(id=row["id"], version=1))
    assert p.write(ledger, "NORD", "product-archive", dict(id=row["id"], version=1, archived=True))["archived"]


def test_concurrent_product_edits_refuse_stale_version(ledger):
    row = p.write(ledger, "NORD", "product-add", PRODUCT)
    barrier = Barrier(2)

    def edit(name):
        other = Ledger(ledger.path)
        try:
            barrier.wait()
            p.write(other, "NORD", "product-update", dict(id=row["id"], version=1, name=name))
            return True
        except p.ProductError:
            return False
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(edit, ["Unu", "Doi"])) == [False, True]


def test_product_action_cli_and_contract(ledger, capsys):
    result = actions.act("product-add", "NORD", {"co": "NORD", **PRODUCT})
    assert result["ok"] and result["done"]["product"]["name"] == PRODUCT["name"]
    assert not actions.act("product-add", "NORD", PRODUCT, require=lambda need: False)["ok"]
    assert cli.main(["product", "list", "--company", "NORD"]) == 0
    assert PRODUCT["name"] in capsys.readouterr().out
    assert schema.openapi()["components"]["schemas"]["Product"]["properties"]["unit_price"]["type"] == "string"


def test_catalog_upgrade_previous_schema(tmp_path, monkeypatch):
    path = tmp_path / "previous.db"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "SCHEMA_VERSION", migrations.MIGRATIONS.index(migrations.product_catalog))
        old = Ledger(path)
        old.set_meta("synthetic-proof", "kept")
        old.close()
    upgraded = Ledger(path)
    try:
        assert upgraded.get_meta("synthetic-proof") == "kept"
        assert p.catalog(upgraded, "NORD")["products"] == []
        assert migrations.read_version(path) == migrations.SCHEMA_VERSION
    finally:
        upgraded.close()


def test_product_http_contract_and_member_permissions(server):
    code, result = _api(server, "product-add", {"co": "NORD", **PRODUCT}, user="member")
    assert code == 200 and result["ok"]
    product = result["done"]["product"]
    code, listed = _get_json(server, "/web/api/products?co=NORD")
    assert code == 200 and listed["products"] == [product]
    code, detail = _get_json(server, "/web/api/products?co=NORD&id=" + product["id"])
    assert code == 200 and detail == {"product": product}
    assert _get_json(server, "/web/api/products?co=STUDIO9&id=" + product["id"])[0] == 404
    assert _get_json(server, "/web/api/products?co=ALL")[0] == 400
    assert _api(server, "product-add", {"co": "NORD", **PRODUCT}, user="readonly")[0] == 403
    assert not _api(server, "product-archive", {"co": "NORD", "id": product["id"], "version": 1})[1]["ok"]

    code, result = _api(server, "product-archive", {"co": "NORD", "id": product["id"], "version": 1, "archived": True})
    assert code == 200 and result["done"]["product"]["archived"]
    code, result = _api(server, "product-archive", {"co": "NORD", "id": product["id"], "version": 2, "archived": False})
    assert code == 200 and not result["done"]["product"]["archived"]


def test_paged_catalog_and_active_picker(ledger):
    first = p.write(ledger, "NORD", "product-add", PRODUCT | {"code": "SKU-A", "name": "A"})
    second = p.write(ledger, "NORD", "product-add", PRODUCT | {"name": "B"})
    page = p.list_page(ledger, "NORD", {"limit": "1"})
    assert page == {"company": "NORD", "products": [first], "total": 2, "next_cursor": "1"}
    assert p.list_page(ledger, "NORD", {"cursor": "1", "limit": "1"})["products"] == [second]
    assert p.list_page(ledger, "NORD", {"q": "sku-a"})["products"] == [first]
    archived = p.write(ledger, "NORD", "product-archive", {"id": first["id"], "version": 1, "archived": True})
    assert p.list_page(ledger, "NORD", {"f": "archived"})["products"] == [archived]
    assert workspace.workspace(ledger, "NORD")["products"] == [second]
    assert workspace.workspace(ledger, "STUDIO9")["products"] == []
    with pytest.raises(p.ProductError):
        p.write(ledger, "NORD", "product-update", {"id": second["id"], "version": 10**30, "name": "Too late"})


def test_schema_gate_does_not_open_payment_file_array_route(server):
    assert _api(server, "payment_file_create", {"co": "NORD", "name": "Example", "invoice_ids": []})[0] == 400


def test_linked_line_is_a_snapshot_and_storno_keeps_its_product(setup_invoice):
    L, cid = setup_invoice
    product = p.write(L, "NORD", "product-add", PRODUCT)
    data = DATA | {
        "lines": [
            LINE | dict(product_id=product["id"], description="Denumire pe factură", unit_price="7.50", vat_rate="11")
        ]
    }
    iid = i.save_draft(L, "NORD", "TEST", cid, data)
    p.write(
        L,
        "NORD",
        "product-update",
        dict(id=product["id"], version=1, name="Nou", unit="KGM", unit_price="99.00", vat_category="Z", vat_rate="0"),
    )
    p.write(L, "NORD", "product-archive", dict(id=product["id"], version=2, archived=True))
    i.save_draft(L, "NORD", "TEST", cid, data, draft_id=iid, version=1)
    i.issue(L, iid, DATA["issue_date"], company="NORD", version=2)
    before, xml = i.invoice(L, iid, company="NORD"), ubl.generate(L, iid, company="NORD")
    assert before["lines"][0] == data["lines"][0] | {"net": "7.50"}
    assert invoice_lines.sheet(L, "NORD", "issued", {"local_id": iid})["lines"][0]["product_id"] == product["id"]
    p.write(L, "NORD", "product-update", dict(id=product["id"], version=3, name="Alt nume", unit_price="999.00"))
    assert i.invoice(L, iid, company="NORD") == before
    assert ubl.generate(L, iid, company="NORD") == xml
    credit = i.storno(L, iid, "2026-11-01", "Retur", company="NORD")
    assert credit["lines"][0]["product_id"] == product["id"] and credit["lines"][0]["net"] == "-7.50"


@pytest.mark.parametrize("state", ["foreign", "archived", "missing"])
def test_new_line_refuses_unavailable_or_foreign_product_atomically(setup_invoice, state):
    L, cid = setup_invoice
    product = p.write(L, "STUDIO9" if state == "foreign" else "NORD", "product-add", PRODUCT)
    if state == "archived":
        p.write(L, "NORD", "product-archive", dict(id=product["id"], version=1, archived=True))
    valid = p.write(L, "NORD", "product-add", PRODUCT)
    data = DATA | {
        "lines": [
            LINE | {"product_id": valid["id"]},
            LINE | {"product_id": "f" * 32 if state == "missing" else product["id"]},
        ]
    }
    with pytest.raises(i.InvoiceError):
        i.save_draft(L, "NORD", "TEST", cid, data)
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0
    assert not p.used(L, "NORD", product["id"]) and not p.used(L, "NORD", valid["id"])


def test_ever_used_product_cannot_be_deleted_after_unlinking_and_deleting_draft(setup_invoice):
    L, cid = setup_invoice
    product = p.write(L, "NORD", "product-add", PRODUCT)
    iid = i.save_draft(L, "NORD", "TEST", cid, DATA | {"lines": [LINE | {"product_id": product["id"]}]})
    i.save_draft(L, "NORD", "TEST", cid, DATA, draft_id=iid, version=1)
    i.delete_draft(L, iid, company="NORD", version=2)
    with pytest.raises(p.ProductError):
        p.write(L, "NORD", "product-delete", dict(id=product["id"], version=1))


def test_product_deletion_racing_a_linked_draft_has_one_winner(setup_invoice):
    L, cid = setup_invoice
    product = p.write(L, "NORD", "product-add", PRODUCT)
    barrier = Barrier(2)

    def write(op):
        other = Ledger(L.path)
        try:
            barrier.wait()
            if op == "draft":
                i.save_draft(other, "NORD", "TEST", cid, DATA | {"lines": [LINE | {"product_id": product["id"]}]})
            else:
                p.write(other, "NORD", "product-delete", dict(id=product["id"], version=1))
            return True
        except (i.InvoiceError, p.ProductError):
            return False
        finally:
            other.close()

    with ThreadPoolExecutor(2) as pool:
        saved, deleted = list(pool.map(write, ("draft", "delete")))
    assert saved != deleted
    assert L.db.execute("SELECT count(*) FROM invoices").fetchone()[0] == int(saved)
    assert p.used(L, "NORD", product["id"]) == saved


def test_invoice_product_link_http_contract(setup_invoice, server):
    L, cid = setup_invoice
    product = p.write(L, "NORD", "product-add", PRODUCT)
    body = DATA | dict(co="NORD", series="TEST", client_id=cid, lines=[LINE | {"product_id": product["id"]}])
    code, result = _api(server, "invoicing/draft-save", body)
    assert code == 200 and result["ok"]
    assert i.invoice(L, result["id"], company="NORD")["lines"][0]["product_id"] == product["id"]


def test_archived_product_keeps_existing_recurring_billing(setup_invoice):
    L, cid = setup_invoice
    product = p.write(L, "NORD", "product-add", PRODUCT)
    data = DATA | {"lines": [LINE | {"product_id": product["id"]}]}
    source = i.save_draft(L, "NORD", "TEST", cid, data)
    rid = recurring.create(L, "NORD", source, 1, 15, "2026-10-15", first_period="2026-11")
    i.delete_draft(L, source, company="NORD", version=1)
    p.write(L, "NORD", "product-archive", dict(id=product["id"], version=1, archived=True))
    invoices = recurring.run(L, "2026-11-01")
    assert len(invoices) == 1
    billed = i.invoice(L, invoices[0], company="NORD")
    assert (billed["status"], billed["lines"][0]["product_id"], billed["lines"][0]["unit_price"]) == (
        "issued",
        product["id"],
        LINE["unit_price"],
    )
    assert not L.db.execute("SELECT paused FROM invoice_recurrences WHERE id=?", (rid,)).fetchone()[0]
    with pytest.raises(i.InvoiceError):
        i.save_draft(L, "NORD", "TEST", cid, data)


def _sales_invoice(L, ident, day, lines, co="NORD", currency="RON", draft=False, total="12.10"):
    with L.tx() as db:
        db.execute(
            "INSERT INTO invoices(id,company,series,number,status,data) VALUES(?,?,?,?,?,?)",
            (
                ident,
                co,
                ident,
                None if draft else 1,
                "draft" if draft else "issued",
                json.dumps({"issue_date": day, "currency": currency, "totals": {"total": total}}),
            ),
        )
        db.executemany(
            "INSERT INTO invoice_lines VALUES(?,?,?)",
            [
                (ident, n, json.dumps(dict(description="Exemplu", unit="H87", quantity="1", net="10.00") | row))
                for n, row in enumerate(lines, 1)
            ],
        )


def test_product_sales_exclude_production_rejection_but_keep_sandbox(ledger):
    product = p.write(ledger, "NORD", "product-add", PRODUCT)
    for ident, environment in (("accepted", None), ("test-rejected", "test"), ("prod-rejected", "prod")):
        _sales_invoice(ledger, ident, "2026-11-01", [{"product_id": product["id"]}])
        if environment:
            with ledger.tx() as db:
                db.execute(
                    "INSERT INTO invoice_submissions(invoice_id,environment,status,xml_sha) VALUES(?,?,'rejected','fake')",
                    (ident, environment),
                )
    sales = stats.sales(ledger, "NORD", "2026-11", 12)
    assert sales["complete"] and sales["unavailable_count"] == 0
    (row,) = sales["totals"]
    assert (row["revenue"], row["quantity"], row["invoice_count"]) == ("20.00", "2", 2)
    assert stats.period(sales, "2026-11-01", "2026-11-19")["totals"] == sales["totals"]


def test_product_sales_signed_lines_and_distinct_invoices(ledger):
    product = p.write(ledger, "NORD", "product-add", PRODUCT)
    line = {"product_id": product["id"]}
    _sales_invoice(ledger, "first", "2026-10-01", [line | {"quantity": "2"}, line | {"net": "5.00"}])
    _sales_invoice(ledger, "storno", "2026-11-01", [line | {"net": "-5.00", "quantity": "-1"}], total="-6.05")
    _sales_invoice(
        ledger,
        "unlinked",
        "2026-11-02",
        [
            {"net": "3.00", "unit": "HUR", "quantity": "0.5"},
            {"net": "4.00", "unit": "HUR", "quantity": "0.5", "product_id": ""},
        ],
    )
    _sales_invoice(ledger, "draft", "2026-11-02", [line], draft=True)
    _sales_invoice(ledger, "foreign", "2026-11-02", [line | {"net": "999.00"}], co="STUDIO9")
    _sales_invoice(ledger, "future", "2026-12-01", [line])
    p.write(
        ledger, "NORD", "product-update", {"id": product["id"], "version": 1, "name": "Nou", "unit_price": "999.00"}
    )
    p.write(ledger, "NORD", "product-archive", {"id": product["id"], "version": 2, "archived": True})
    result = stats.sales(ledger, "NORD", "2026-11", 12)
    assert result["from"] == "2025-12-01" and result["to"] == "2026-11-30" and result["complete"]
    assert result["totals"] == [
        dict(
            product_id=product["id"],
            name="Nou",
            currency="RON",
            unit="H87",
            revenue="10.00",
            quantity="2",
            invoice_count=2,
        ),
        dict(
            product_id=None,
            name="Fără produs",
            currency="RON",
            unit="HUR",
            revenue="7.00",
            quantity="1.0",
            invoice_count=1,
        ),
    ]
    assert len(result["months"]) == 12 and result["months"][0]["products"] == []
    assert stats.sales(ledger, "NORD", "2026-10", 1)["totals"][0]["revenue"] == "15.00"


def test_product_sales_keep_currencies_and_units_separate_and_queries_batched(ledger):
    product = p.write(ledger, "NORD", "product-add", PRODUCT)
    line = {"product_id": product["id"]}
    for n, (currency, unit) in enumerate((("RON", "H87"), ("EUR", "H87"), ("RON", "HUR"))):
        _sales_invoice(ledger, str(n), "2026-11-01", [line | {"unit": unit}], currency=currency)
    counts = []
    for count in (1, 12):
        queries = []
        ledger.db.set_trace_callback(queries.append)
        try:
            result = stats.sales(ledger, "NORD", "2026-11", count)
        finally:
            ledger.db.set_trace_callback(None)
        counts.append(sum(q.lstrip().upper().startswith("SELECT") for q in queries))
        assert len(result["totals"]) == 3
    assert counts == [2, 2]


@pytest.mark.parametrize("month,count", [("2026-13", 1), ("../2026-01", 1), ("2026-01", 3), ("2026-01", True)])
def test_product_sales_refuse_invalid_period(ledger, month, count):
    with pytest.raises(p.ProductError):
        stats.sales(ledger, "NORD", month, count)


def test_product_sales_http_scope_and_period(server):
    code, data = _get_json(server, "/web/api/product-stats?co=NORD&month=2024-02&months=12", user="readonly")
    assert code == 200 and data["from"] == "2023-03-01" and data["to"] == "2024-02-29"
    assert _get_json(server, "/web/api/product-stats?co=ALL")[0] == 400
    assert _get_json(server, "/web/api/product-stats?co=NORD&months=3")[0] == 400


def _sales_xml(number, root="Invoice", total="24.20", net="20.00", quantity="2"):
    tag = "InvoicedQuantity" if root == "Invoice" else "CreditedQuantity"
    line = f"""<cac:{root}Line><cbc:{tag} unitCode="H87">{quantity}</cbc:{tag}>
      <cbc:LineExtensionAmount currencyID="RON">{net}</cbc:LineExtensionAmount>
      <cac:Item><cbc:Name>Obiect exemplu</cbc:Name><cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID>
      <cbc:Percent>21</cbc:Percent></cac:ClassifiedTaxCategory></cac:Item>
      <cac:Price><cbc:PriceAmount currencyID="RON">10.00</cbc:PriceAmount></cac:Price></cac:{root}Line>"""
    return external_ubl(number=number, root=root, total=total, issue="2026-11-01").replace(
        f"</{root}>".encode(), (line + f"</{root}>").encode()
    )


def test_product_sales_external_credit_notes_and_unavailable_lines(ledger):
    first = _sales_xml("EXTERNAL-1")
    for mid, xml in (
        ("7001", first),
        ("7002", _sales_xml("EXTERNAL-2", root="CreditNote")),
        ("7003", _sales_xml("EXTERNAL-3", total="-12.10", net="-10.00", quantity="-1")),
        ("7004", first),
    ):
        issued.store(ledger, "NORD", sent(mid), anaf_zip(xml, mid), track_history=True)
    result = stats.sales(ledger, "NORD", "2026-11", 1)
    assert result["complete"] and result["unavailable_count"] == 0
    assert result["totals"] == [
        dict(
            product_id=None,
            name="Fără produs",
            currency="RON",
            unit="H87",
            revenue="-10.00",
            quantity="-1",
            invoice_count=3,
        )
    ]
    with ledger.tx() as db:
        db.execute("UPDATE issued SET raw_path='missing.zip' WHERE message_id='7001'")
    result = stats.sales(ledger, "NORD", "2026-11", 1)
    assert not result["complete"] and result["unavailable_count"] == 1
    assert not result["months"][0]["complete"]


def test_product_sales_deduplicate_authored_and_spv_copy(ledger):
    issued.store(ledger, "NORD", sent("7001"), anaf_zip(_sales_xml("EXTERNAL-1")), track_history=True)
    _sales_invoice(ledger, "local", "2026-11-01", [{"net": "20.00", "quantity": "2"}])
    with ledger.tx() as db:
        db.execute("INSERT INTO invoice_sent_links VALUES('7001','local','prod')")
    result = stats.sales(ledger, "NORD", "2026-11", 1)
    assert result["complete"] and result["totals"][0]["revenue"] == "20.00"
    assert result["totals"][0]["invoice_count"] == 1


def test_product_sales_preserve_external_decimal_quantity(ledger):
    quantity = "99999999999999999.123456789012345678"
    xml = _sales_xml("PRECISE-1", quantity=quantity)
    issued.store(ledger, "NORD", sent("7001"), anaf_zip(xml), track_history=True)
    assert stats.sales(ledger, "NORD", "2026-11", 1)["totals"][0]["quantity"] == quantity


def test_product_sales_mark_unparsed_spv_message_incomplete(ledger):
    result = issued.store(ledger, "NORD", sent("7001", "202611031200"), anaf_zip(b"<broken>"))
    assert result["status"] == "unparsed"
    report = stats.sales(ledger, "NORD", "2026-11", 1)
    assert not report["complete"] and report["unavailable_count"] == 1
    assert not report["months"][0]["complete"] and report["totals"] == []
    assert stats.sales(ledger, "NORD", "2026-10", 1)["complete"]


@pytest.mark.parametrize("total,net,quantity", [("24.20", "20.00", "2"), ("-24.20", "-20.00", "-2")])
def test_product_sales_deduplicate_unlinked_authored_spv_copy(ledger, total, net, quantity):
    xml = _sales_xml("EXTERNAL-1", total=total, net=net, quantity=quantity)
    issued.store(ledger, "NORD", sent("7001"), anaf_zip(xml), track_history=True)
    _sales_invoice(ledger, "EXTERNAL", "2026-11-01", [{"net": net, "quantity": quantity}], total=total)
    assert ledger.db.execute("SELECT count(*) FROM invoice_sent_links").fetchone()[0] == 0
    report = stats.sales(ledger, "NORD", "2026-11", 1)
    assert report["complete"] and report["totals"][0]["revenue"] == net
    assert report["totals"][0]["invoice_count"] == 1


@pytest.mark.parametrize("net,quantity", [("20.00", "2"), ("-20.00", "-2")])
def test_product_sales_credit_note_reduces_sales_even_with_negative_payable(ledger, net, quantity):
    xml = _sales_xml("CREDIT-1", root="CreditNote", total="-24.20", net=net, quantity=quantity)
    issued.store(ledger, "NORD", sent("7001"), anaf_zip(xml), track_history=True)
    report = stats.sales(ledger, "NORD", "2026-11", 1)
    assert report["totals"][0]["revenue"] == "-20.00"
    assert report["totals"][0]["quantity"] == "-2"


def test_product_sales_periods_keep_units_counts_and_today(ledger):
    for ident, day, lines, currency in (
        ("oct", "2026-10-01", [{"net": "15.00", "quantity": "2"}], "RON"),
        ("nov", "2026-11-02", [{"net": "-5.00", "quantity": "-1"}], "RON"),
        ("eur", "2026-11-03", [{"net": "7.00", "quantity": "0.5"}], "EUR"),
        ("hours", "2026-11-04", [{"unit": "HUR", "net": "9.00", "quantity": "1.5"}], "RON"),
        ("future", "2026-11-21", [{"net": "999.00"}], "RON"),
    ):
        _sales_invoice(ledger, ident, day, lines, currency=currency)
    report = stats.sales(ledger, "NORD", "2026-11", 12, today=date(2026, 11, 19))
    result = stats.period(report, "2026-10-01", "2026-11-19")
    assert report["to"] == "2026-11-19" and result["complete"] and result["unavailable_count"] == 0
    assert {
        (r["currency"], r["unit"]): (r["revenue"], r["quantity"], r["invoice_count"]) for r in result["totals"]
    } == {
        ("RON", "H87"): ("10.00", "1", 2),
        ("EUR", "H87"): ("7.00", "0.5", 1),
        ("RON", "HUR"): ("9.00", "1.5", 1),
    }
    report["months"][-1].update(complete=False, unavailable_count=1)
    assert not stats.period(report, "2026-11-01", "2026-11-19")["complete"]
    assert stats.period(report, "2026-11-01", "2026-11-19")["unavailable_count"] == 1
    assert stats.period(report, "2026-10-01", "2026-10-31")["complete"]


def test_product_sales_refuse_fractional_cent_line_nets(ledger):
    issued.store(ledger, "NORD", sent("7001"), anaf_zip(_sales_xml("FRACTIONAL", net="20.005")), track_history=True)
    report = stats.sales(ledger, "NORD", "2026-11", 12)
    selected = stats.period(report, "2026-10-01", "2026-11-30")
    assert report["totals"] == selected["totals"] == []
    assert not report["complete"] and not selected["complete"]
    assert report["unavailable_count"] == selected["unavailable_count"] == 1
