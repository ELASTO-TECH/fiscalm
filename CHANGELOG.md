# Changelog

Product versions describe fiscalm releases. The API and the ledger schema have their own
version numbers. Back up before upgrading; see [self-hosting](docs/self-hosting.md).

## 0.1.6 (2026-11-26)

- Ledger schema 45, upgraded automatically at startup. Back up first.
- A debit with the exact amount of exactly one candidate settles on its own, even when the
  bank text names no supplier; a name match no longer waits for a first confirmation. Only
  real ambiguity goes to a person.
- A stored statement copy ingested again is recorded and matched as a duplicate.
- Settlement reports: real amounts, one report per payout, refund lines flagged, multi-sheet
  XLSX, presets for Romanian couriers and processors, reports by mail from named senders.
  A batch that passes every check settles from the first report.
- Credit notes naming no invoice cancel the one invoice they exactly reverse; a partly
  credited invoice pays its net remaining amount.
- AI proposes transaction explanations and applies only sure labels; a payment mail it reads
  as sure proof settles its invoice, anything less waits in Sarcini for one tap.
- Receipts: no more line-by-line transcription; an uploaded receipt can be named as a card
  payment's bon.
- Bank: single-account and partial statement uploads, account opening and closing dates in
  Setări › Bănci și conturi, reminders that warn when statements are behind.

## 0.1.5 (2026-11-25)

- A BT statement whose final balance falls alone on the next page is read correctly and
  its lines are matched. Statements stored before can be fixed with `fiscalm ingest` on
  the same file.
- Quarterly and older daily BT PDFs are imported.
- `fiscalm ingest` reads plain BT PDFs under any name; one bad file is refused, not the
  whole import.
- A BT month may start on its first banking day or on the day an account was opened.

## 0.1.4 (2026-11-25)

- Older BT PDFs are read, and accounts opened after a statement's month are not required
  for it (`fiscalm company opened`).
- One month of BT PDFs can be imported together, one per account or daily, in
  `fiscalm ingest` and in Încarcă extras; all files are stored or none.
- Every BT PDF, from mail, the CLI or the app, must hold all of the firm's BT accounts.
- Unassigned uploads open from Toate firmele.

## 0.1.3 (2026-11-20)

- Ledger schema 39, upgraded automatically at startup. Back up first.
- One receipt upload link can serve every firm of the install; the buyer on the receipt
  decides the firm.
- Bank statements accepted in the app now go into the accountant's pack.
- BT statements that print `CUI:<n>` without a space are accepted.
- History import never files a label or a date as an invoice number; such PDFs wait in
  review. New `fiscalm history correct` fixes or removes a wrong history PDF row.

## 0.1.1 (2026-11-20)

- Anonymous daily install statistics in Live, on by default. Administrators can turn them
  off in Setări › Sistem or with `FISCALM_TELEMETRY=0`.

## 0.1.0 (2026-11-20)

First public release. User-facing release notes are published with the
[GitHub Release](https://github.com/ELASTO-TECH/fiscalm/releases).

- Ledger schema 38. Later releases upgrade it with recorded, transactional migrations at
  startup; older code refuses a newer ledger.
- Any number of companies, with their bank accounts, accountant and integrations.
- Accounts with password plus TOTP or passkeys, recovery codes, installation-wide roles,
  user-owned API tokens, an HTTP API and an MCP server.
- Received and issued invoices read from ANAF e-Factura; payments and collections matched
  against bank statements.
- Invoicing: sellers, clients, products, managed invoice series, a bank account per invoice,
  drafts, PDF and CIUS-RO XML, storno, recurring invoices
  and submission to SPV (ANAF TEST in Sandbox, production in Live).
- Bank statements: BT PDF, CAMT.053, MT940 and mapped CSV uploads, statements from mail, and
  transactions read through Enable Banking.
- Supplier invoices read from mail over read-only IMAP; expenses show their document source.
- Acasă charts for cash flow, balances and estimated VAT.
- Settlement reports from marketplaces, couriers and Shopify Payments, collecting a whole batch
  of invoices against one bank credit.
- Orders from Shopify, eMAG, WooCommerce and PrestaShop, invoiced from fiscalm.
- History import: e-Factura XML and ZIP, SAGA, PDF and SmartBill unpaid-invoice exports.
- Payment files for BT Go and the accountant's monthly pack as a ZIP to download and send.
- Sandbox and Live modes, a Docker Compose install (web, scheduler and backup services), backups and a daily
  release check.
