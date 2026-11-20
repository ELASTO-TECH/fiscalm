"""A4 invoice representation with subset Inter fonts and deterministic pagination."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fiscalm import companies
from fiscalm.invoicing import branding
from fiscalm.invoicing.canvas import BODY, INK, MUTED, TINT, Canvas
from fiscalm.invoicing.engine import draft_seller, invoice, invoice_number, money, ron_totals
from fiscalm.invoicing.errors import InvoiceError
from fiscalm.invoicing.labels import COUNTY_NAMES, UNITS
from fiscalm.ledger import Ledger


def romanian(value: str, kind: str = "amount", currency: str = "") -> str:
    """Format all printable dates and decimals without binary floating point."""
    if kind == "date":
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    number = Decimal(value)
    raw = money(number) if kind == "amount" else format(number, "f")
    if kind != "amount" and "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    if kind == "price":
        whole, _, fraction = raw.partition(".")
        raw = whole + "." + fraction.ljust(2, "0")
    whole, dot, fraction = raw.partition(".")
    grouped = format(int(whole), ",").replace(",", ".")
    if number < 0 and int(whole) == 0:
        grouped = "-" + grouped
    return grouped + ("," + fraction if dot else "") + (" " + currency if currency else "")


def generate(
    L: Ledger, invoice_id: str, *, company: str, preview: bool = False, logo: branding.Logo | None = None
) -> bytes:
    """Render frozen invoice facts; optional normalized logo bytes need no branding service."""
    inv = invoice(L, invoice_id, company=company)
    if (inv["status"] == "draft") != preview:
        raise InvoiceError("Factura trebuie emisă înainte de exportul PDF.")
    data = dict(inv["data"])
    if preview:
        data["seller"] = draft_seller(L, inv)
    pdf = _InvoicePDF(L, inv, data, company, preview, logo)
    pdf.facts()
    pdf.parties()
    pdf.columns()
    pdf.lines()
    pdf.tax_groups()
    pdf.tax_layout()
    pdf.tax_table()
    pdf.totals_block()
    pdf.payment()
    return pdf.footer()


class _InvoicePDF:
    """Render one invoice in document order, retaining pagination between sections."""

    def __init__(self, L: Ledger, inv: dict, data: dict, company: str, preview: bool, logo: branding.Logo | None):
        self.inv, self.data, self.preview, self.logo = inv, data, preview, logo
        self.c = Canvas()
        self.seller = self.data["seller"]
        self.vat_payer = self.seller["vat_payer"]
        self.currency = self.data["currency"]
        self.totals = self.data["totals"]
        self.rate = Decimal(self.data["exchange_rate"]) if self.currency != "RON" else Decimal(1)
        self.accent = companies.PRINT_ACCENTS[companies.get(company, L=L).palette]
        self.title = (
            "Ciornă" if self.preview else ("Factură de stornare" if self.data.get("billing_ref") else "Factură")
        )
        self.number = "Fără număr" if self.preview else "Nr. " + invoice_number(self.inv)
        self.unit = "lei" if self.currency == "RON" else self.currency
        self.y = 0.0

        self.new_page(True)

    def label(self, value: str, x: float, baseline: float, right: float | None = None) -> None:
        self.c.text(value.upper(), x, baseline, 6.75, "Medium", MUTED, right=right, tracking=0.405)

    def new_page(self, first: bool = False) -> None:
        if not first:
            self.c.pages.append([])
            if self.logo:
                scale = min(100 / self.logo.width, 24 / self.logo.height)
                self.c.pages[-1].append(
                    f"q {self.logo.width * scale} 0 0 {self.logo.height * scale} 45 776 cm /Logo Do Q"
                )
            else:
                self.c.ellipsis(self.seller["name"], 45, 787, 10, "SemiBold", INK, width=260)
            self.c.text(self.number, 0, 787, 9.75, fill=MUTED, right=550)
            self.y = 750
            return
        header_bottom = 757.0
        if self.logo:
            w, h = self.logo.width, self.logo.height
            scale = min(168 / w, 48 / h)
            self.c.pages[-1].append(f"q {w * scale} 0 0 {h * scale} 45 {800 - h * scale} cm /Logo Do Q")
        else:
            rows = self.c.wrap(self.seller["name"], 260, 13.5, "SemiBold")
            for n, row in enumerate(rows):
                self.c.text(row, 45, 787 - n * 17, 13.5, "SemiBold", INK)
            header_bottom = 787 - (len(rows) - 1) * 17
        self.c.text(self.title, 0, 783, 19.5, "SemiBold", INK, right=550, width=243)
        self.c.text(self.number, 0, 766, 9.75, fill=MUTED, right=550)
        self.y = min(733, header_bottom - 24)

    def ensure(self, height: float) -> bool:
        if self.y - height < 63:
            self.new_page()
            return True
        return False

    def vat_label(self, category: str, rate: str) -> str:
        return (
            romanian(rate, "quantity") + "%"
            if category in {"S", "Z"}
            else {
                "E": "Scutit",
                "AE": "Taxare inversă",
                "K": "Intracomunitar",
                "G": "Export",
                "O": "În afara sferei TVA",
            }[category]
        )

    def legal_mention(self, row: dict) -> str:
        reason = row.get("exemption_reason", "")
        if row["vat_category"] == "O" and reason in {"art. 310", "Scutire conform art. 310"}:
            return "Regimul special de scutire pentru întreprinderile mici conform art. 310 din Codul fiscal"
        return {
            "VATEX-EU-AE": "Taxare inversă",
            "VATEX-EU-IC": "Livrare intracomunitară scutită de TVA conform art. 294 alin. (2) din Codul fiscal",
            "VATEX-EU-G": "Export de bunuri scutit de TVA conform art. 294 alin. (1) din Codul fiscal",
        }.get(reason, reason)

    def facts(self) -> None:
        self.c.rule(45, self.y, 550, self.accent, 2)
        self.y -= 22
        facts = [("Data emiterii", romanian(self.data["issue_date"], "date"))]
        if self.data.get("delivery_date") and self.data["delivery_date"] != self.data["issue_date"]:
            facts += [
                (
                    "Data livrării facturii inițiale" if self.data.get("billing_ref") else "Data livrării",
                    romanian(self.data["delivery_date"], "date"),
                )
            ]
        if self.data.get("advance_payment_date"):
            facts += [
                (
                    "Data încasării avansului (factura inițială)"
                    if self.data.get("billing_ref")
                    else "Data încasării avansului",
                    romanian(self.data["advance_payment_date"], "date"),
                )
            ]
        facts += [
            ("Scadență", romanian(self.data["due_date"], "date")),
            ("Total de plată", romanian(self.totals["total"], currency=self.unit)),
        ]
        fact_width = 505 / len(facts)
        fact_height = 0
        for n, (name, value) in enumerate(facts):
            x = 45 + n * fact_width
            rows = self.c.wrap(name.upper(), fact_width - 12, 6.75, "Medium")
            for pos, row in enumerate(rows):
                self.label(row, x, self.y - pos * 10)
            baseline = self.y - len(rows) * 10 - 5
            size = 12 if n == len(facts) - 1 else 9.375
            self.c.text(
                value, x, baseline, size, "SemiBold", self.accent if n == len(facts) - 1 else INK, width=fact_width - 8
            )
            fact_height = max(fact_height, len(rows) * 10 + size + 5)
        self.y -= fact_height + 24

    def party_rows(self, role: str, party: dict) -> list[tuple[str, float, str, str]]:
        county = party.get("county", "")
        county = ("județul " + COUNTY_NAMES.get(county[3:], county)) if county.startswith("RO-") else county
        values = [(role.upper(), 6.75, "Medium", MUTED), (party["name"], 9.75, "SemiBold", INK)]
        values += [
            (
                ", ".join(
                    label + " " + party[k]
                    for k, label in [("cui", "CUI"), ("vat_id", "Cod TVA"), ("tax_id", "Cod fiscal")]
                    if party.get(k)
                ),
                8.25,
                "Regular",
                BODY,
            )
        ]
        if party.get("reg_com"):
            values += [("Reg. Com. " + party["reg_com"], 8.25, "Regular", BODY)]
        values += [
            (
                ", ".join(
                    filter(
                        None,
                        [
                            party.get("street"),
                            party.get("city"),
                            county,
                            party.get("postal_code"),
                            party.get("country"),
                        ],
                    )
                ),
                8.25,
                "Regular",
                BODY,
            )
        ]
        values += [(", ".join(party[k] for k in ["email", "phone"] if party.get(k)), 8.25, "Regular", BODY)]
        return [
            (row, size, weight, fill)
            for value, size, weight, fill in values
            if value
            for row in self.c.wrap(value, 237.5, size, weight)
        ]

    def parties(self) -> None:
        blocks = [self.party_rows("Furnizor", self.seller), self.party_rows("Client", self.data["client"])]
        for n in range(max(map(len, blocks))):
            self.ensure(15)
            for x, block in zip([45, 312.5], blocks, strict=True):
                if n < len(block):
                    value, size, weight, fill = block[n]
                    if n == 0:
                        self.label(value, x, self.y)
                    else:
                        self.c.text(value, x, self.y, size, weight, fill)
            self.y -= 14 if n < 2 else 12
        self.y -= 24

    def columns(self) -> None:
        # Right edges define numeric columns; header text uses these same anchors.
        self.xs, self.widths = [45, 70, 231, 259, 318, 392, 461], [22, 155, 25, 53, 68, 63, 89]
        if not self.vat_payer:
            self.xs, self.widths = [45, 70, 288, 316, 389, 475], [22, 212, 25, 67, 80, 75]
        for k, field in [(3, "quantity"), (4, "unit_price"), (5, "net")]:
            kind = "quantity" if k == 3 else "price" if k == 4 else "amount"
            needed = max(self.c.measure(romanian(row[field], kind), 8.25) + 6 for row in self.inv["lines"])
            extra = min(max(0, needed - self.widths[k]), max(0, self.widths[1] - 60))
            self.widths[1] -= extra
            for j in range(2, k + 1):
                self.xs[j] -= extra
            self.widths[k] += extra

    def header(self) -> None:
        self.ensure(49)
        labels = [
            "#",
            "Descriere",
            "UM",
            "Cantitate",
            "Preț unitar fără TVA" if self.vat_payer else "Preț unitar",
            "Valoare",
            "Cota TVA",
        ]
        self.c.rect(45, self.y - 32, 505, 32, TINT)
        # Non-VAT sellers have six columns; the final VAT label is omitted.
        for k, (x, w, name) in enumerate(zip(self.xs, self.widths, labels, strict=False)):
            for n, row in enumerate(self.c.wrap(name.upper(), w - 4, 6.75, "Medium")):
                self.label(row, x + 3, self.y - 11 - n * 9, right=x + w - 3 if k >= 3 else None)
        self.c.rule(45, self.y - 32, 550)
        self.y -= 47

    def lines(self) -> None:
        self.header()
        units = {u["code"]: u["label"] for u in UNITS}
        for pos, row in enumerate(self.inv["lines"], 1):
            values = [
                str(pos),
                row["description"],
                units.get(row["unit"], row["unit"]),
                romanian(row["quantity"], "quantity"),
                romanian(row["unit_price"], "price"),
                romanian(row["net"]),
                self.vat_label(row["vat_category"], row["vat_rate"]),
            ]
            # Non-VAT sellers omit the final VAT cell, just like the header.
            columns = [
                [v] if k in {0, 3, 4, 5} else self.c.wrap(v, w - 6, 8.25, "Medium" if k == 1 else "Regular")
                for k, (v, w) in enumerate(zip(values, self.widths, strict=False))
            ]
            height = max(map(len, columns)) * 12 + 15
            if self.ensure(min(height, 620)):
                self.header()
            for n in range(max(map(len, columns))):
                if self.ensure(12):
                    self.header()
                for k, (x, w, col) in enumerate(zip(self.xs, self.widths, columns, strict=True)):
                    if n < len(col):
                        self.c.text(
                            col[n],
                            x + 3,
                            self.y,
                            8.25,
                            "Medium" if k == 1 else "Regular",
                            MUTED if k == 0 else BODY,
                            right=x + w - 3 if k >= 3 else None,
                            width=w - 6,
                        )
                self.y -= 12
            self.y -= 4
            self.c.rule(45, self.y, 550)
            self.y -= 11
        self.y -= 7

    def tax_groups(self) -> None:
        self.summary_mentions: set[str] = set()
        self.groups = []
        if self.vat_payer:
            for g in sorted(
                self.totals["taxes"],
                key=lambda g: (g["category"] not in {"S", "Z"}, -Decimal(g["rate"]), g["category"]),
            ):
                if g["category"] in {"S", "Z"}:
                    self.groups.append((g, "", g["net"]))
                else:
                    lines = [
                        r
                        for r in self.inv["lines"]
                        if r["vat_category"] == g["category"] and Decimal(r["vat_rate"]) == Decimal(g["rate"])
                    ]
                    reasons = sorted({self.legal_mention(r) for r in lines})
                    for reason in reasons:
                        net = sum((Decimal(r["net"]) for r in lines if self.legal_mention(r) == reason), Decimal(0))
                        self.groups.append((g, reason, money(net)))

    def tax_values(self, group: dict, net: str) -> list[str]:
        values = [romanian(net), romanian(group["vat"])]
        if self.currency != "RON":
            values.append(romanian(money(Decimal(group["vat"]) * self.rate)))
        return values

    def tax_layout(self) -> None:
        self.summary_top = self.y
        self.tax_edges = [145, 198, 251, 304] if self.currency != "RON" else [150, 227, 304]
        self.tax_widths = [self.tax_edges[0] - 45] + [
            b - a for a, b in zip(self.tax_edges, self.tax_edges[1:], strict=False)
        ]
        for g, _, net in self.groups:
            values = self.tax_values(g, net)
            for k, value in enumerate(values, 1):
                self.tax_widths[k] = max(self.tax_widths[k], self.c.measure(value, 6) + 12)
        self.tax_edges = []
        for w in self.tax_widths:
            self.tax_edges.append((self.tax_edges[-1] if self.tax_edges else 45) + w)
        self.tax_width = self.tax_edges[-1] - 45

    def tax_header(self) -> None:
        self.label("TVA pe cote", 45, self.y)
        self.y -= 12
        self.c.rect(45, self.y - 24, self.tax_width, 24, TINT)
        names = ["Cota TVA", "Bază (" + self.unit + ")", "TVA (" + self.unit + ")"]
        if self.currency != "RON":
            names += ["TVA (LEI)"]
        for k, (name, edge) in enumerate(zip(names, self.tax_edges, strict=True)):
            left = 45 if k == 0 else self.tax_edges[k - 1]
            for n, row in enumerate(self.c.wrap(name.upper(), edge - left - 9, 6.75, "Medium")):
                self.label(row, left + 6, self.y - 9 - n * 9, right=edge - 6 if k else None)
        self.y -= 37

    def tax_table(self) -> None:
        if self.groups:
            if self.ensure(88):
                self.summary_top = self.y
            self.tax_header()
        self.summary_start_page = len(self.c.pages)
        for g, reason, net in self.groups:
            name = self.vat_label(g["category"], g["rate"])
            if reason and len({r for h, r, _ in self.groups if h["category"] == g["category"]}) > 1:
                name += ": " + reason
                self.summary_mentions.add(reason)
            rows = self.c.wrap(name, self.tax_edges[0] - 57, 7.5)
            if self.ensure(min(len(rows) * 11 + 12, 610)):
                self.tax_header()
            for n, row in enumerate(rows):
                if self.ensure(11):
                    self.tax_header()
                self.c.text(row, 51, self.y, 7.5)
                if n == 0:
                    values = self.tax_values(g, net)
                    for k, (value, edge) in enumerate(zip(values, self.tax_edges[1:], strict=True), 1):
                        self.c.text(value, 0, self.y, 7.5, right=edge - 6, width=edge - self.tax_edges[k - 1] - 12)
                self.y -= 11
            self.c.rule(45, self.y + 3, 45 + self.tax_width)
            self.y -= 9

    def totals_block(self) -> None:
        tax_end, tax_page = self.y, len(self.c.pages)
        # Entire totals block moves together when the available height is insufficient.
        summary = [("Total fără TVA", self.totals["net"]), ("TVA", self.totals["vat"])] if self.vat_payer else []
        total_height = len(summary) * 21 + 49 + (48 if self.currency != "RON" else 0)
        if (
            self.tax_width <= 259
            and (self.summary_top if tax_page == self.summary_start_page else 710) - total_height >= 63
        ):
            # If the tax table continued, its last page still has a free right column.
            self.y = self.summary_top if tax_page == self.summary_start_page else 710
        else:
            self.y = tax_end
        self.ensure(total_height)
        totals_page = len(self.c.pages)
        for name, value in summary:
            self.c.text(name, 331, self.y, 8.25)
            self.c.text(romanian(value, currency=self.unit), 0, self.y, 8.25, "Medium", INK, right=550, width=121)
            self.y -= 21
        self.c.rule(331, self.y + 3, 550, INK, 1.125)
        self.y -= 18
        self.c.text("Total de plată", 331, self.y, 9.75, "SemiBold", INK)
        self.c.text(
            romanian(self.totals["total"], currency=self.unit),
            0,
            self.y,
            13.5,
            "SemiBold",
            self.accent,
            right=550,
            width=121,
        )
        self.y -= 23
        if self.currency != "RON":
            converted = ron_totals(self.data)
            ron = [("Total în lei", converted["total"])]
            if self.vat_payer:
                ron += [("TVA în lei", converted["vat"])]
            for name, value in ron:
                self.c.text(name, 331, self.y, 7.5, fill=MUTED)
                self.c.text(romanian(value, currency="lei"), 0, self.y, 7.5, fill=MUTED, right=550, width=121)
                self.y -= 13
            self.c.text("Curs BNR din " + romanian(self.data["exchange_date"], "date"), 331, self.y, 7.5, fill=MUTED)
            self.c.text(
                romanian(str(self.rate), "quantity") + " lei/" + self.currency, 0, self.y, 7.5, fill=MUTED, right=550
            )
            self.y -= 13
        if totals_page == tax_page:
            self.y = min(self.y, tax_end)
        self.y -= 24

    def mentions(self) -> list[str]:
        mentions = sorted(
            ({self.legal_mention(r) for r in self.inv["lines"]} | set(self.data.get("mentions", [])))
            - {""}
            - self.summary_mentions
        )
        if self.data.get("billing_ref"):
            mentions.insert(
                0,
                "Stornare pentru factura "
                + self.data["billing_ref"]
                + " din "
                + romanian(self.data["original_issue_date"], "date"),
            )
        if self.currency != "RON" and self.vat_payer:
            delta = Decimal(money(Decimal(self.totals["vat"]) * self.rate)) - sum(
                (Decimal(money(Decimal(g["vat"]) * self.rate)) for g in self.totals["taxes"]), Decimal(0)
            )
            if delta:
                mentions += [
                    "Diferență de rotunjire TVA în lei: "
                    + romanian(money(delta), currency="lei")
                    + ". Totalul TVA în lei este calculat din totalul TVA al facturii."
                ]
        if self.data.get("notes"):
            mentions.append(self.data["notes"])
        mentions = list(dict.fromkeys(mentions))
        return mentions

    def payment(self) -> None:
        mentions = self.mentions()
        iban = self.seller["iban"]
        pay = [
            "Transfer bancar în contul",
            "IBAN: " + " ".join(iban[n : n + 4] for n in range(0, len(iban), 4)),
            self.seller["bank"],
        ]
        self.ensure(66)
        self.label("Plată", 45, self.y)
        if mentions:
            self.label("Mențiuni", 312.5, self.y)
        self.y -= 17
        left_rows = [row for v in pay for row in self.c.wrap(v, 237.5)]
        right_rows = [row for v in mentions for row in self.c.wrap(v, 237.5)]
        for n in range(max(len(left_rows), len(right_rows))):
            self.ensure(12)
            for x, rows in [(45, left_rows), (312.5, right_rows)]:
                if n < len(rows):
                    self.c.text(rows[n], x, self.y)
            self.y -= 12

    def footer(self) -> bytes:
        pages = self.c.pages[:]
        for index, page in enumerate(pages):
            self.c.pages = [page]
            if self.preview:
                self.c.used["SemiBold"].update(map(ord, "CIORNĂ"))
                self.c.pages[-1].append(
                    "q /Draft gs 0 g BT /F3 80 Tf 0 Tc .866 .5 -.5 .866 130 340 Tm <"
                    + "CIORNĂ".encode("utf-16-be").hex()
                    + "> Tj ET Q"
                )
            self.c.rule(45, 42, 550)
            self.c.text(self.seller["name"] + ", CUI " + self.seller["cui"], 45, 27, 6.75, fill=MUTED, width=380)
            self.c.text(f"Pagina {index + 1} din {len(pages)}", 0, 27, 6.75, fill=MUTED, right=550)
        self.c.pages = pages
        return self.c.save(self.logo)


def render(L: Ledger, invoice_id: str, *, company: str, preview: bool = False) -> bytes:
    """Resolve branding by snapshot for issued documents and current pointer for drafts."""
    data = invoice(L, invoice_id, company=company)["data"]
    try:
        logo = (
            branding.current_logo(L, company)
            if preview
            else branding.logo_for(L, company, data["logo_sha"])
            if data.get("logo_sha")
            else None
        )
    except (InvoiceError, OSError, ValueError):
        raise InvoiceError(branding.UNAVAILABLE) from None
    return generate(L, invoice_id, company=company, preview=preview, logo=logo)
