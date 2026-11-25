"""Minimal one-page text PDFs that pdftotext reads: synthetic invoices and BT statements for tests."""

from __future__ import annotations

import pikepdf


def text_pdf(lines: list[str], *, font: str = "Helvetica") -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream = "BT /F1 11 Tf 50 800 Td 14 TL " + " ".join(f"({esc(x)}) '" for x in lines) + " ET"
    data = stream.encode("cp1252", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream",
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} /Encoding /WinAnsiEncoding >>".encode("ascii"),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def bt_statement(path, cui: str, period: tuple[str, str], accounts, password: str | None = "synthetic"):
    """A synthetic encrypted BT statement PDF laid out as pdftotext reads BT's: the holder's CUI, the period
    (dd/mm/yyyy, dd/mm/yyyy) and one section per account, (currency, iban, opening, lines), each line
    (dd/mm/yyyy, reference, Decimal amount: below zero a debit). `password=None`: a plain PDF, as BT24 downloads."""

    def at(text: str, amount, end: int) -> str:  # an amount right-aligned under its column (Debit ends at 68)
        shown = f"{abs(amount):,.2f}"
        return text.ljust(end - len(shown)) + shown

    lines = ["BANCA TRANSILVANIA S.A.", f"CUI:{cui}", f"EXTRAS CONT Numarul: 6 din {period[0]} - {period[1]}"]
    for currency, iban, opening, moves in accounts:
        debit = sum((-a for *_, a in moves if a < 0), opening * 0)
        credit = sum((a for *_, a in moves if a > 0), opening * 0)
        lines += ["CONT 123 Valuta", f"{currency} Cod IBAN: {iban}"]
        lines += ["Data Descriere".ljust(63) + "Debit".rjust(5) + "Credit".rjust(16)]
        lines += [at(" " * 12 + "SOLD ANTERIOR", opening, 68)]
        for day, ref, amount in moves:
            lines += [
                at(f"{day} Transfer", amount, 68 if amount < 0 else 84),
                f"Plata;Synthetic Partner SRL;REF: {ref}",
            ]
        lines += [at(at(" " * 12 + "RULAJ TOTAL CONT", debit, 68), credit, 84)]
        lines += [at(" " * 12 + "SOLD FINAL CONT", opening + credit - debit, 68)]
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 72 + 12 * len(lines)))
        font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Courier)
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pdf.make_indirect(font)))
        top = 36 + 12 * len(lines)
        text = f"BT /F1 10 Tf 12 TL 40 {top} Td " + " ".join("(" + ln + ") Tj T*" for ln in lines) + " ET"
        page.Contents = pdf.make_stream(text.encode())
        crypt = pikepdf.Encryption(owner="synthetic-owner", user=password, R=6) if password else False
        pdf.save(path, encryption=crypt)
    return path
