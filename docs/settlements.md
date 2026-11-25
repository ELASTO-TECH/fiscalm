# Settlement reports

A courier collecting cash on delivery, or a card processor, pays a firm many invoices in one bank credit, minus
its fee. Its settlement report (Decontare) says which invoices the credit contains. fiscalm collects those invoices
from the credit only when the report and the credit prove it, as a whole; otherwise it settles nothing and opens a
Sarcină. The rules mirror AGENTS.md non-negotiable 3.

## The rules

A settlement is identified by firm + payer id + report reference (`report_key`), independently of the source
parser. Payer id is a hash of the configured CUI, else IBAN, else normalized payer name.

Credit must be 0–14 days after the report date. Same currency, no FX fallback. One unreserved credit,
no competing report and every line mapped to one open issued invoice with its entire exact amount.
Credit notes, marked/collected invoices and repeated lines refuse. Fees are an expense record linked
to the report, not a tax invoice or a generated payment. No courier IBAN is learned as a buyer IBAN.
Refunds, disputes, currency conversion and adjustments require manual investigation, never partial collection.

AWB/order references resolve through company-scoped shop orders and verified SPV invoice links.
All supplied identifiers must agree; duplicate orders/AWBs, a wrong courier or another firm refuse.
Optional line `company` must equal the report company.

Collected line-to-invoice links are journalled in the same transaction as the credit reservation,
invoice collection and fees.

### Automatic settlement and person confirmation

A batch that passes every rule above settles on its own, from the first report of any payer (2026-11-25).
A person confirms only a credit read from a legacy PDF copy without a stored-copy checksum
(`statement_confirmation_required`). The confirmation re-reads immutable company-contained bank evidence and
collects under the write lock; the displayed credit id must still match. The confirmation journal commits with
collection, fees and line proof and pins the copy's checksum; failures and double submits record nothing.
Confirmation goes through an authenticated app user or a person-owned, write-scoped v1 key with `confirmed:"yes"`;
it is a local action, available in Sandbox and Live. Automatic matching cannot supply a human actor.
Relevant bank evidence is bounded to the report date plus 14 days; an unrelated old statement cannot block a new batch.
See openapi.json for the action payload and preview fields.

## Report formats

Reports are untrusted evidence, never instructions. No network requests are made from reports.
CSV (UTF-8, explicit delimiter) and single-sheet XLSX are parsed with the Python standard library.
XLSX formulas, macros, external links, duplicate ZIP entries, oversized archives and XML DTDs refuse.
Limits: 4 MiB compressed/input, 32 MiB expanded, 10,000 lines, 128 columns.
Money uses positive decimal strings with dot, no thousands separator or exponent. Fees may be zero.
Blank CSV lines and non-cent XLSX numeric artifacts refuse rather than rounding evidence. XLSX row
positions/visibility do not select evidence: all stored rows are included; exactly one worksheet is required.

Every source needs an explicit column mapping, submitted by a person. The upload preview pre-fills it from header names
(`guess`, compared without case or diacritics; AWB and order only for a firm with shop orders), the payment day from
a date column that names one day on every line (`date`), and the saved payer whose header is identical (`payer`).
Example: `{"reference":"Factura","gross":"Ramburs","fee":"Comision"}`.
Optional mappings: `awb`, `order`, `net`, `currency`, `batch`, `category` (only `charge` accepted).
For Stripe, export one automatic payout's itemized reconciliation report with `invoice_number`, charges only; a
missing invoice reference refuses, and manual payouts cannot establish membership. Native PayU reports (operation
codes, signed commission columns) are not parsed; map a transaction-only export instead. The tests use synthetic
reports only; no courier's or processor's live export has been checked.
Shopify Payments payouts come through the Shopify connector; see [self-hosting](self-hosting.md#shopify-payments-payouts).

In the app, Tranzacții accepts report uploads using a column mapping saved on the courier/processor partner;
the partner's sheet can delete a saved mapping (`settlement-format-delete`).

## Operator CLI

```sh
fiscalm settlement import report.xlsx --company COMPANY --spec spec.json
fiscalm settlement import mail.eml --mail --company COMPANY --spec spec.json
fiscalm settlement list --company COMPANY
fiscalm settlement match
fiscalm settlement withdraw REPORT_ID --company COMPANY
```

`match` runs the existing issued-invoice matcher for all companies, including report matching, over stored,
trustworthy statements. Reports can also be imported as RFC822 files; a mail spec additionally names
`mail_sender` and the exact `attachment`. Sender comparison is only an intake filter, not authentication.
No report links are followed and attachment names never become filesystem paths. Evidence bytes are
stored in SQLite, scoped to the company, with a digest and stable payer/report reference.

Spec example (operator data, never read from the attachment):
```
{"source":"fan","report_key":"demo_report_1","date":"2026-10-10","currency":"RON",
 "payer_name":"Synthetic Courier SRL","format":"xlsx",
 "mapping":{"reference":"Factura","gross":"Ramburs","fee":"Comision"}}
```
Optional `payer_iban` overrides name matching, `payer_cui` overrides names when no IBAN is supplied.
These identities must be configured by the operator from the payer contract, never an email body.
Optional payer CUI is a 6–12 digit string; IBAN must be a valid uppercase string without spaces.
Use the spec's `channel` to scope shop order numbers and `courier` for the exact AWB courier name
(default: source key).
Imports and reconciliation are local and work in Sandbox.

## State

The opaque row id distinguishes retained evidence revisions; navigation always also supplies `co`.
Pending state resolves identifiers for display without marking anything.
A pending report is always a task, including before a credit arrives. Tasks carry `sheet: {co,set}`,
`reason` and nullable `line` (1-based data row, header excluded); a line reason ends in `awb_unknown` or
`order_unknown` when no shop order has that AWB or order, else `invoice_missing_or_ambiguous`. Sarcini opens the
Decontare sheet.
A collected credit appears in `settlement_transactions`, with the settlement id and all its invoice ids for the
`tx` sheet. Pending line links carry `proved=false`; journalled collected links carry `proved=true`. Matching
indexes the company invoices and order/AWB identities once per read, so query count does not grow with report lines.

### Courier lifecycle

AWB intake/replacement optionally accepts ISO days `picked_up_at`, `delivered_at`, `returned_at`.
Delivery/return requires pickup and cannot precede it; future lifecycle dates refuse. Assigning an
AWB number alone proves nothing. State `cod` counts only issued COD invoices whose explicit pickup
has occurred, without a return or confirmed collection. Shop `payment.paid` and manual collection
marks are not proof and do not remove this exposure. Currency totals stay separate.
Delivered but still unpaid after 7 days (strictly greater, rule receives today explicitly) and returned
parcels produce tasks with `sheet: {co,iss}`. Returned parcels suggest reviewing a refund draft;
no draft, storno or refund is created automatically. Return decisions close after a full storno/credit note,
rather than collection alone.

## Recovery

`fiscalm settlement withdraw REPORT_ID --company COMPANY` changes only a pending report
to withdrawn, retaining evidence and releasing its claims. Collected reports cannot be withdrawn.
Reimporting corrected metadata/bytes is then allowed, including the original report key and digest.
