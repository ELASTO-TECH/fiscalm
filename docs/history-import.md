# Importing a firm's history from another invoicing program

ANAF's e-Factura API lists only the last 60 days, so a firm's older invoices come from the program it used before.
fiscalm reads e-Factura XML, SPV message ZIPs, invoice PDFs, SAGA XML and SmartBill's unpaid-invoice report. It
does not connect to any invoicing program's API. An import brings invoice history, client details and a proposal
for each series' next number; another program's "paid" is never proof of payment.

## Which export to use

| Program | Invoices | Clients | Unpaid invoices |
|---|---|---|---|
| SmartBill | Rapoarte → Facturi emise → Descarcă e-Facturi trimise (XML, from 2024) or Export Saga; received: Cheltuieli → Preia e-Facturi → Descarcă e-Facturi preluate | Export Saga | Rapoarte → Facturi neîncasate → Exportă, saved as XLSX |
| Oblio | Import/Export → Oblio → Exportă toate facturile una câte una (individual files) | SAGA export | not supported: import it as history only |
| FGO | Facturare → Opțiuni → Descarcă arhiva facturi (PDF), or its SAGA sync | SAGA export | not supported |
| SAGA | Ieșiri → Tipărire → Formular PDF, which also writes the invoice XML (`F_*.xml`) | `CLI_*.xml` | collections `I_*.xml` are kept as provenance only |
| Any program | the firm's own archive of e-Factura ZIPs or PDFs | | |

Facturis and Keez have no supported export path here; use their e-Factura XML or PDFs. A combined report PDF holding
many invoices is not split: it waits in review.

## Import scope

In the app (each firm in Setări › Firme, also linked from the Configurează card on Acasă) or from the command line:

```sh
fiscalm history import COMPANY FILE --from YYYY-MM-DD [--json]
fiscalm history list COMPANY [--json]
```

Both use the same reader, which detects each file by content:

- **e-Factura XML**: one file, a ZIP of them, or a ZIP of SPV message ZIPs (the invoice and its signature).
  Issued and received are told apart by the firm's CUI. XML replaces the PDF reading of the same invoice; the PDF
  stays stored as its document.
- **PDFs**, alone or in a mixed ZIP, read like [mailed invoices](mail-invoices.md) but accepting Romanian
  suppliers. They are always history. Uncertain ones wait in a review table (not Sarcini), where a person fills in
  the fields or marks the document as not an invoice. The AI service narrows a total only in Live; in Sandbox no
  document text leaves the installation. A number that reads as a label or a date (`Data emitere: 06.02.2026`) is
  never accepted, on import or in review. When an FGO archive prints a bare `FACTURA` heading, the number is the
  one series line under it (`ABC 0065`); none, several or a longer number send the PDF to review.
- **Correcting an accepted PDF.** `fiscalm history list COMPANY --json` shows each row's `xml_sha`; for a PDF row,
  `fiscalm history reopen COMPANY SHA` puts the PDF back in the review table, where a person fixes the fields or
  marks it as not an invoice. Until then the old row stays in history and still holds back matching; the answer
  replaces or removes it. The command logs the reopen and refuses XML rows, a PDF paired with an XML, a PDF that
  filed no row in this firm and an invoice now tracked in Vânzări or Cheltuieli.
- **SAGA XML** (`F_*.xml`, `CLI_*.xml`, `I_*.xml`), standalone or in the mixed ZIP, detected by its root. The total
  is the lines' net plus VAT; non-RON amounts, nonzero excise and reverse-charge documents with nonzero VAT are left
  unreadable rather than guessed. An invoice is the same one as its e-Factura copy only with the same full number,
  date, kind and firm. Client profiles never overwrite reviewed ones, and clients without a tax id are never merged
  by name. Collections settle nothing, and a missing collection never means unpaid.
- **SmartBill unpaid XLSX**, uploaded after the invoices and detected by its `Factura`, `Data emiterii` and
  `Rest de încasat` columns. Formulas, ambiguous headers and the 1904 date system are refused. Each positive claim
  must match exactly one history issued invoice by full number and issue date; a missing, ambiguous, duplicate or
  over-total claim refuses the whole report.

The import is one transaction and can be repeated: an invoice already there is not imported twice. The series'
next number is proposed in Facturare as the highest imported number plus one and is used only after a person
confirms it, once every source is in.

## What "paid before the import" means

Every import gives a **start date**: the day from which fiscalm keeps the firm's payments. The first import sets
it, and later imports reuse the same date.

- An XML invoice dated on or after the start date is tracked as if e-Factura sync had brought it. It stays open until a
  statement shows the payment.
- A positive XML invoice dated before it, and every PDF-only invoice at any date, is **history**. It is listed in Vânzări or
  Cheltuieli by its date and on its partner's sheet, marked "Din istoric", and kept for the series' numbers and for its
  client. It adds to what was invoiced (`turnover`: a partner's Vânzări and Cheltuieli, Rapoarte's Facturat pe ani), with
  its part shown apart.
  It is never settled by a statement, never paid, never reminded, and it counts neither as paid nor as owed. An
  e-Factura XML has no paid state, and another program's "paid" is the user's claim, not proof (AGENTS.md non-negotiables 2
  and 3).
- History still counts as a **rival** in the matching. A credit or debit that names a history invoice settles
  nothing. One that fits a history invoice as well as a tracked one settles neither, because an old invoice paid late
  must not close a new one. For a recurring client with the same amount every month, this holds back the automatic
  match for up to a year after the start date, the matching window. A start date from which the bank statements are
  in keeps that short.
- A supported **unpaid** list turns an identified history invoice into an open, tracked one. That is a debt
  claimed by the user's own data, the safe direction: it hides nothing. A **paid** claim only records where it came
  from. The invoice stays history and nothing is settled.
- Sync uses the same start date for issued and received invoices: older XMLs stay in `history`, with the same
  projection, deduplication and statement-matching window. Without a start date, sync is unchanged.
- Setting the start date does not silently hide existing tracked debts. The owner can reclassify untouched open
  rows with identical XML history copies: `fiscalm history archive-open COMPANY issued ID1 ID2` and
  `fiscalm history archive-open COMPANY received ID3 ID4`. Each command is atomic and refuses rows with payment
  evidence or explanations. No payment is recorded; the original rows and files remain.
- To track an XML history invoice that really is unpaid, use `fiscalm history list COMPANY --json` to find its
  `xml_sha`, then `fiscalm history track COMPANY SHA`. This works for received and issued invoices, including
  rows archived above. It opens the full XML amount; no partial payment is inferred. There is no equivalent
  action in the app yet; the smallest addition would expose this same decision on a history invoice.

Credit notes are corrections, not new debts. XML credit notes and negative XML invoices remain in the existing
credit state regardless of the start date, so they still reduce a tracked invoice and block an obsolete payment
file. Explicitly tracking an old invoice first restores related XML credit notes held in history by earlier imports;
their original XML is revalidated by the normal credit rules. Missing evidence refuses the tracking change.

For a debt already tracked before this upgrade whose credit note was imported into history, re-import the
original credit XML or archive: `fiscalm history import COMPANY ORIGINAL_FILE --from YYYY-MM-DD`, with the firm's
existing start date. This restores corrections through the normal validation rules before preparing payment
files. The upgrade does not read or regenerate missing documents. An unrelated damaged credit cannot block
tracking another invoice; missing evidence for a related credit refuses the change.
