"""A4 invoice representation with subset Inter fonts and deterministic pagination."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fiscalm import companies
from fiscalm.invoicing import branding
from fiscalm.invoicing.canvas import BODY, INK, MUTED, PANEL, RULE, TINT, WHITE, Canvas, tint
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
    pdf.parties()
    pdf.columns()
    pdf.lines()
    pdf.tax_groups()
    pdf.tax_layout()
    pdf.tax_table()
    pdf.totals_block()
    pdf.payment()
    return pdf.footer()


LEFT, RIGHT, WIDTH = 45, 550, 505
BOTTOM = 63  # lowest baseline above the footer
TOTALS_X = 340  # left edge of the totals column
PANEL_BOTTOM = 54  # the payment panel sits just above the footer rule on the last page


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
        self.wash = tint(self.accent, 0.1)
        self.title = (
            "Ciornă" if self.preview else ("Factură de stornare" if self.data.get("billing_ref") else "Factură")
        )
        self.number = "Fără număr" if self.preview else "Nr. " + invoice_number(self.inv)
        # A credit note or a negative total is owed by nobody: it is a "Total", never "De plată", and has no due pill.
        self.owed = not self.data.get("billing_ref") and Decimal(self.totals["total"]) >= 0
        self.unit = "lei" if self.currency == "RON" else self.currency
        self.y = 0.0

        self.first_page()

    def label(self, value: str, x: float, baseline: float, right: float | None = None) -> None:
        self.c.text(value.upper(), x, baseline, 6.75, "Medium", MUTED, right=right, tracking=0.405)

    def first_page(self) -> None:
        """Logo or seller name, the title and number at left; what is owed, and by when, at right."""
        if self.logo:
            w, h = self.logo.width, self.logo.height
            scale = min(150 / w, 36 / h)
            self.c.pages[-1].append(f"q {w * scale} 0 0 {h * scale} {LEFT} {797 - h * scale} cm /Logo Do Q")
            title = 797 - h * scale - 44
        else:
            rows = self.c.wrap(self.seller["name"], 270, 12.75, "SemiBold")
            for n, row in enumerate(rows):
                self.c.text(row, LEFT, 787 - n * 16, 12.75, "SemiBold", INK)
            title = 787 - (len(rows) - 1) * 16 - 47
        self.c.text(self.title, LEFT, title, 30, "SemiBold", INK, width=270, tracking=-1.05)
        number = self.number
        if not self.preview:
            number += " din " + romanian(self.data["issue_date"], "date")
        self.c.text(number, LEFT, title - 20, 9.75, fill=MUTED, width=270)
        bottom = title - 24
        self.hero(bottom)
        self.c.rule(LEFT, bottom - 22.5, RIGHT)
        self.y = bottom - 45

    def hero(self, bottom: float) -> None:
        """The total in large accent type above the due-date pill, right-aligned on the header's bottom edge."""
        baseline = bottom + 4
        if self.owed:
            pill = "Scadență " + romanian(self.data["due_date"], "date")
            width = self.c.measure(pill, 8.25, "Medium") + 16.5
            self.c.round_rect(RIGHT - width, bottom, width, 17, 8.5, fill=self.wash)
            self.c.text(pill, RIGHT - width + 8.25, bottom + 5.6, 8.25, "Medium", self.accent)
            baseline = bottom + 28
        amount = romanian(self.totals["total"])
        natural = self.c.measure(amount, 33, "SemiBold", -1.155) + 3 + self.c.measure(self.unit, 13.5, "Medium")
        scale = min(1, 210 / natural)  # a long amount shrinks with its currency, keeping the header's right column
        unit_x = RIGHT - self.c.measure(self.unit, 13.5 * scale, "Medium")
        self.c.text(self.unit, unit_x, baseline, 13.5 * scale, "Medium", MUTED)
        size, tracking = 33 * scale, -1.155 * scale
        self.c.text(amount, 0, baseline, size, "SemiBold", self.accent, right=unit_x - 3 * scale, tracking=tracking)
        self.label("De plată" if self.owed else "Total", 0, baseline + 33 * scale * 0.73 + 9, right=RIGHT)

    def new_page(self) -> None:
        self.c.pages.append([])
        if self.logo:
            scale = min(100 / self.logo.width, 24 / self.logo.height)
            self.c.pages[-1].append(f"q {self.logo.width * scale} 0 0 {self.logo.height * scale} 45 776 cm /Logo Do Q")
        else:
            self.c.ellipsis(self.seller["name"], 45, 787, 10, "SemiBold", INK, width=260)
        self.c.text(self.number, 0, 787, 9.75, fill=MUTED, right=RIGHT)
        self.y = 750

    def ensure(self, height: float) -> bool:
        if self.y - height < BOTTOM:
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

    def party_rows(self, party: dict, width: float) -> list[tuple[str, float, str, str]]:
        county = party.get("county", "")
        county = ("județul " + COUNTY_NAMES.get(county[3:], county)) if county.startswith("RO-") else county
        ids = [("cui", "CUI"), ("vat_id", "Cod TVA"), ("tax_id", "Cod fiscal")]
        place = [party.get("street"), party.get("city"), county, party.get("postal_code"), party.get("country")]
        values = [(party["name"], 9.75, "SemiBold", INK)] + [
            (value, 7.5, "Regular", BODY)
            for value in [
                ", ".join(label + " " + party[k] for k, label in ids if party.get(k)),
                "Reg. Com. " + party["reg_com"] if party.get("reg_com") else "",
                ", ".join(filter(None, place)),
                ", ".join(party[k] for k in ["email", "phone"] if party.get(k)),
            ]
        ]
        return [
            (row, size, weight, fill)
            for value, size, weight, fill in values
            if value
            for row in self.c.wrap(value, width, size, weight)
        ]

    def party_card(self, role: str, party: dict, width: float) -> list[tuple[float, str, float, str, str]]:
        """A card's rows as (baseline offset from the card's inner top, text, size, weight, colour)."""
        card, offset = [(5.0, role.upper(), 6.75, "Medium", MUTED)], 5.0
        for n, (row, size, weight, fill) in enumerate(self.party_rows(party, width)):
            offset += 16 if n == 0 else 13 if weight == "Regular" and card[-1][3] == "SemiBold" else 11
            card.append((offset, row, size, weight, fill))
        return card

    def dates_card(self, width: float) -> list[tuple[float, str, float, str, str]]:
        storno = self.data.get("billing_ref")
        facts = [("Data emiterii", self.data["issue_date"])]
        if self.data.get("delivery_date") and self.data["delivery_date"] != self.data["issue_date"]:
            facts.append(("Data livrării inițiale" if storno else "Data livrării", self.data["delivery_date"]))
        if self.data.get("advance_payment_date"):
            name = "Data încasării avansului (factura inițială)" if storno else "Data încasării avansului"
            facts.append((name, self.data["advance_payment_date"]))
        facts.append(("Scadență", self.data["due_date"]))
        card, offset = [], -9.0
        for name, value in facts:
            for row in self.c.wrap(name.upper(), width, 6.75, "Medium"):
                offset += 9 if card and card[-1][2] == 6.75 else 14
                card.append((offset, row, 6.75, "Medium", MUTED))
            offset += 13
            card.append((offset, romanian(value, "date"), 9.375, "SemiBold", INK))
        return card

    def parties(self) -> None:
        """Furnizor, Client and the dates in three bordered cards; a card too tall for the page continues on the next."""
        pad, gap, dates = 13.5, 10.5, 120
        party = (WIDTH - dates - 2 * gap) / 2
        lefts = [LEFT, LEFT + party + gap, RIGHT - dates]
        widths = [party, party, dates]
        cards = [
            self.party_card("Furnizor", self.seller, party - 2 * pad),
            self.party_card("Client", self.data["client"], party - 2 * pad),
            self.dates_card(dates - 2 * pad),
        ]
        while any(cards):
            room = self.y - (BOTTOM - 8) - 2 * pad  # a card ends a little above the footer rule
            fits = [[row for row in card if row[0] + 3 <= room] for card in cards]
            if not any(fits):
                self.new_page()
                continue
            height = max(row[0] for card in fits for row in card) + 3 + 2 * pad
            for x, w, rows in zip(lefts, widths, fits, strict=True):
                if not rows:  # a card already finished on an earlier page
                    continue
                self.c.round_rect(x, self.y - height, w, height, 7.5, stroke=RULE)
                for offset, row, size, weight, fill in rows:
                    tracking = 0.405 if size == 6.75 else 0
                    self.c.text(row, x + pad, self.y - pad - offset, size, weight, fill, tracking=tracking)
            # Rows left over continue in the same columns, from the top of the next page's cards.
            cards = [card[len(done) :] for card, done in zip(cards, fits, strict=True)]
            cards = [[(o - card[0][0] + 5, *rest) for o, *rest in card] if card else [] for card in cards]
            self.y -= height
            if any(cards):
                self.new_page()
        self.y -= 21

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
        labels = ["Nr.", "Descriere", "UM", "Cant."]
        labels += (
            ["Preț unitar fără TVA", "Valoare fără TVA", "Cotă TVA"] if self.vat_payer else ["Preț unitar", "Valoare"]
        )
        cells = [
            self.c.wrap(name.upper(), w - 4, 6.75, "SemiBold") for name, w in zip(labels, self.widths, strict=True)
        ]
        most = max(map(len, cells))
        height = 10 + 9 * most
        self.ensure(height + 26)
        self.c.round_rect(LEFT, self.y - height, WIDTH, height, 6, fill=self.wash)
        for k, (x, w, rows) in enumerate(zip(self.xs, self.widths, cells, strict=True)):
            for n, row in enumerate(rows):
                right = x + w - 3 if k >= 3 else None
                baseline = self.y - 12 - 4.5 * (most - len(rows)) - n * 9  # shorter labels sit centred
                self.c.text(row, x + 3, baseline, 6.75, "SemiBold", self.accent, right=right, tracking=0.405)
        self.y -= height

    def lines(self) -> None:
        """One row per line: light zebra, a hairline under each; the header repeats on every page the table reaches."""
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
            height = 14 + 12 * max(map(len, columns))
            if self.ensure(height):
                self.header()
            if pos % 2:
                self.c.rect(LEFT, self.y - height, WIDTH, height, TINT)
            for k, (x, w, col) in enumerate(zip(self.xs, self.widths, columns, strict=True)):
                for n, value in enumerate(col):
                    self.c.text(
                        value,
                        x + 3,
                        self.y - 16 - n * 12,
                        8.25,
                        "Medium" if k == 1 else "Regular",
                        MUTED if k == 0 else INK if k == 1 else BODY,
                        right=x + w - 3 if k >= 3 else None,
                        width=w - 6,
                    )
            self.y -= height
            self.c.rule(LEFT, self.y, RIGHT)
        self.y -= 26

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
        self.tax_widths = [self.tax_edges[0] - LEFT] + [
            b - a for a, b in zip(self.tax_edges, self.tax_edges[1:], strict=False)
        ]
        for g, _, net in self.groups:
            values = self.tax_values(g, net)
            for k, value in enumerate(values, 1):
                self.tax_widths[k] = max(self.tax_widths[k], self.c.measure(value, 6) + 12)
        self.tax_edges = []
        for w in self.tax_widths:
            self.tax_edges.append((self.tax_edges[-1] if self.tax_edges else LEFT) + w)
        self.tax_width = self.tax_edges[-1] - LEFT

    def tax_header(self) -> None:
        self.label("TVA pe cote", LEFT, self.y)
        self.y -= 17
        names = ["Cotă TVA", "Bază (" + self.unit + ")", "TVA (" + self.unit + ")"]
        if self.currency != "RON":
            names += ["TVA (LEI)"]
        rows = 1
        for k, (name, edge) in enumerate(zip(names, self.tax_edges, strict=True)):
            left = LEFT if k == 0 else self.tax_edges[k - 1] + 6
            wrapped = self.c.wrap(name.upper(), edge - left, 6.75, "Medium")
            rows = max(rows, len(wrapped))
            for n, row in enumerate(wrapped):
                self.label(row, left, self.y - n * 9, right=edge if k else None)
        self.y -= 9 * (rows - 1) + 6
        self.c.rule(LEFT, self.y, LEFT + self.tax_width)
        self.y -= 14

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
            rows = self.c.wrap(name, self.tax_edges[0] - LEFT - 6, 7.5)
            if self.ensure(min(len(rows) * 11 + 12, 610)):
                self.tax_header()
            for n, row in enumerate(rows):
                if self.ensure(11):
                    self.tax_header()
                self.c.text(row, LEFT, self.y, 7.5)
                if n == 0:
                    values = self.tax_values(g, net)
                    for k, (value, edge) in enumerate(zip(values, self.tax_edges[1:], strict=True), 1):
                        self.c.text(value, 0, self.y, 7.5, right=edge, width=edge - self.tax_edges[k - 1] - 6)
                self.y -= 11
            self.c.rule(LEFT, self.y + 3, LEFT + self.tax_width)
            self.y -= 9

    def totals_block(self) -> None:
        tax_end, tax_page = self.y, len(self.c.pages)
        # Entire totals block moves together when the available height is insufficient.
        summary = [("Total fără TVA", self.totals["net"]), ("TVA", self.totals["vat"])] if self.vat_payer else []
        total_height = len(summary) * 18 + 50 + (39 if self.currency != "RON" else 0)
        if (
            self.tax_width <= TOTALS_X - 36 - LEFT
            and (self.summary_top if tax_page == self.summary_start_page else 710) - total_height >= BOTTOM
        ):
            # If the tax table continued, its last page still has a free right column.
            self.y = self.summary_top if tax_page == self.summary_start_page else 710
        else:
            self.y = tax_end
        self.ensure(total_height)
        totals_page = len(self.c.pages)
        for name, value in summary:
            self.c.text(name, TOTALS_X + 3, self.y, 8.25)
            self.c.text(romanian(value, currency=self.unit), 0, self.y, 8.25, "Medium", INK, right=RIGHT - 3, width=121)
            self.y -= 18
        self.grand_total()
        if self.currency != "RON":
            self.fx_lines()
        if totals_page == tax_page:
            self.y = min(self.y, tax_end)
        self.y -= 24

    def grand_total(self) -> None:
        """The amount owed, white on an accent box: a "Total" for a credit note or a negative total."""
        top = self.y - 1
        self.c.round_rect(TOTALS_X, top - 42, RIGHT - TOTALS_X, 42, 7.5, fill=self.accent)
        name = "Total de plată" if self.owed else "Total"
        self.c.text(name, TOTALS_X + 13.5, top - 24.5, 9, "Medium", WHITE)
        room = RIGHT - TOTALS_X - 27 - self.c.measure(name, 9, "Medium") - 12
        total = romanian(self.totals["total"], currency=self.unit)
        self.c.text(total, 0, top - 27, 18, "SemiBold", WHITE, right=RIGHT - 13.5, width=room)
        self.y = top - 42 - 16

    def fx_lines(self) -> None:
        converted = ron_totals(self.data)
        ron = [("Total în lei", converted["total"])]
        if self.vat_payer:
            ron += [("TVA în lei", converted["vat"])]
        for name, value in ron:
            self.c.text(name, TOTALS_X + 3, self.y, 7.5, fill=MUTED)
            self.c.text(romanian(value, currency="lei"), 0, self.y, 7.5, fill=MUTED, right=RIGHT - 3, width=121)
            self.y -= 13
        self.c.text(
            "Curs BNR din " + romanian(self.data["exchange_date"], "date"), TOTALS_X + 3, self.y, 7.5, fill=MUTED
        )
        rate = romanian(str(self.rate), "quantity") + " lei/" + self.currency
        self.c.text(rate, 0, self.y, 7.5, fill=MUTED, right=RIGHT - 3)
        self.y -= 13

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

    def block(self, name: str, values: list[str]) -> None:
        """A labelled run of full-width text rows that continues on the next page when it must."""
        rows = [row for v in values for row in self.c.wrap(v, WIDTH)]
        if rows:
            self.ensure(30)
            self.label(name, LEFT, self.y)
            self.y -= 17
            for row in rows:
                self.ensure(12)
                self.c.text(row, LEFT, self.y, 8.25)
                self.y -= 12
            self.y -= 12

    def payment(self) -> None:
        """Mențiuni in their own block, then the IBAN and bank in a grey panel just above the last page's footer."""
        self.block("Mențiuni", self.mentions())
        iban = self.seller["iban"]
        iban = " ".join(iban[n : n + 4] for n in range(0, len(iban), 4))
        iban_width = min(330, self.c.measure(iban, 12.75, "SemiBold", 0.77))
        bank = self.c.wrap(self.seller["bank"], WIDTH - 33 - iban_width - 24, 9.75, "SemiBold")
        if len(bank) > 5:
            # A bank name longer than today's 100-character cap (saved before it) prints in full above the panel,
            # which then holds the IBAN alone and always fits.
            self.block("Bancă", [self.seller["bank"]])
            bank = []
        height = 48 + 13 * max(0, len(bank) - 1)
        if self.y - 12 < PANEL_BOTTOM + height:
            self.new_page()
        top = PANEL_BOTTOM + height
        self.c.round_rect(LEFT, PANEL_BOTTOM, WIDTH, height, 9, fill=PANEL)
        self.label("IBAN", LEFT + 16.5, top - 18)
        self.c.text(iban, LEFT + 16.5, top - 35, 12.75, "SemiBold", INK, width=330, tracking=0.77)
        if bank:
            self.label("Bancă", 0, top - 18, right=RIGHT - 16.5)
        for n, row in enumerate(bank):
            self.c.text(row, 0, top - 35 - 13 * n, 9.75, "SemiBold", INK, right=RIGHT - 16.5)

    def footer(self) -> bytes:
        pages = self.c.pages[:]
        for index, page in enumerate(pages):
            self.c.pages = [page]
            self.c.rect(0, 836, 595, 6, self.accent)
            if self.preview:
                self.c.used["SemiBold"].update(map(ord, "CIORNĂ"))
                self.c.pages[-1].append(
                    "q /Draft gs 0 g BT /F3 80 Tf 0 Tc .866 .5 -.5 .866 130 340 Tm <"
                    + "CIORNĂ".encode("utf-16-be").hex()
                    + "> Tj ET Q"
                )
            self.c.rule(LEFT, 42, RIGHT)
            self.c.text(self.seller["name"] + ", CUI " + self.seller["cui"], LEFT, 27, 6.75, fill=MUTED, width=380)
            self.c.text(f"Pagina {index + 1} din {len(pages)}", 0, 27, 6.75, fill=MUTED, right=RIGHT)
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
