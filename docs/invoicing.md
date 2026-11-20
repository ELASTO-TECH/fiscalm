# Invoice issuing

A guide for contributors to `src/fiscalm/invoicing/`. Setting up the ANAF connection is in
[efactura-anaf.md](efactura-anaf.md); HTTP routes and request bodies are in [openapi.json](openapi.json).

## 1. Scope and invariants

`invoicing/engine.py` authors invoices: sellers, clients, series, drafts, issuing, storno. `invoicing/ubl.py`
writes CIUS-RO XML, `invoicing/pdf.py` the PDF, `invoicing/spv.py` submits to SPV and records the answers.

Issuing (and a storno) also writes the invoice's row in the `issued` collection ledger, in the same transaction,
in Sandbox and Live alike: parsed from the invoice's own XML exactly as a copy read from SPV would be, marked
`engine.LOCAL` in `upload_id`, with no ZIP, and linked to the invoice. That row is what the client owes and what a
collection settles; a storno's row is a credit note that reduces its original. The copy SPV later lists as sent is
stored as its `duplicate`: linked as evidence (it marks the production submission accepted), never collected.
Invoices issued before schema 24 get that row on the first run after the upgrade (`issued.backfill_own_rows`,
called by `cli.main` before any command): the migration only leaves a marker, because a migration cannot import the
engine code that builds the row. One already stored from SPV without a link stays as it is.

- Only drafts can be edited or deleted. An issued invoice is a frozen snapshot: changing a seller or client
  profile never changes it, and its number is never reused.
- Issuing never depends on the network. A failed upload never rolls back an issued invoice.
- No command sends mail or moves money. PDF and XML are downloads a person sends.
- Every id-based command and engine call takes the company; an id from another company is refused.
- Pure rules receive today's date as an argument; only the CLI reads the clock, when `--today` is omitted.
- `InvoiceError` carries a refusal a person can read.

## 2. Model and numbering

- `save_seller(L, company, profile)`: `name`, `cui`, `reg_com`, `vat_payer` (boolean), `vat_id`, `street`, `city`,
  `county` (`RO-XX`), `postal_code`, `country`, `iban`, `bank`, `email`, `phone`.
- `save_client(L, company, profile, client_id=None)` returns a client id. Address fields as for the seller. Identity
  is `cui` or `vat_id`, or none with `private=true` for a private person. A business identity is unique within a
  company; private people get independent ids.
- `create_series(L, company, code, start=1)` creates a series and its first number. Continuing a numbering from
  another program is an explicit `start`; nothing is inferred from SPV.
- `set_default_series(L, company, code)` chooses the series new invoices start in (`default_series`; without a
  choice, the first by code). `remove_series(L, company, code)` removes a series only while no issued invoice, draft
  or recurring invoice uses it; `series_list` gives each series' refusal. A series' numbers never go back and issued
  invoices are never renumbered.
- `save_draft(L, company, series, client_id, data, draft_id=None, *, version=None)` creates or replaces a draft.
  `data`: `issue_date`, `due_date`, `currency`, optional `notes`, `exchange_rate`, `exchange_date` and `lines`.
  A line: `description`, `quantity`, `unit`, `unit_price`, `vat_category`, `vat_rate`, optional
  `exemption_reason`. Amounts and quantities are decimal strings; JSON numbers are read as strings, never floats.
  Fields are allow-listed and type-checked; control characters are refused; counties must be real `RO-XX` codes and
  Bucharest is written `SECTOR1` to `SECTOR6` (never guessed). Notes hold 300 characters; a line description and an
  exemption reason 100 (`LINE_DESCRIPTION_MAX`, because the description becomes the XML item name, BT-153, whose
  CIUS-RO limit is 100). Prices and quantities take up to 12 decimals, an application bound; amounts above 10^15
  and exponent notation are refused. An optional `iban` chooses the account the invoice asks to be paid into
  (`issuing_account`: a current account of the same firm, checked at save, preview and issue); without it, the seller
  profile's. `account_choices` lists the accounts an invoice can name and, per currency, the one a new invoice starts
  with: the profile's in its currency, else the first account in that currency. A saved draft shows the account it
  issues with (`draft_account`); a storno draft repeats its original's seller. The issued invoice keeps the account
  in its seller snapshot, on its PDF and in `PayeeFinancialAccount`.
- `issue(L, draft_id, today, *, company, version)` requires `today` to equal the issue date. It builds and validates
  the whole XML first, then freezes parties and totals and takes the next number in one SQLite transaction. A stale
  `version` or a second issue refuses without consuming a number.
- `invoice(L, id, *, company)` returns the invoice with its decoded `data` and lines; `delete_draft` refuses
  issued invoices. `invoice_number(invoice)` formats the public number as `SERIES-NUMBER`; the separator keeps
  series `ABC1` number 2 apart from series `ABC` number 12. XML, PDF, storno references and SPV checks all use it.
- `invoicing/recurring.py` keeps recurring templates; `fiscalm invoice recur run` issues due ones through `issue()`, in
  Sandbox too, once per rule and month. A refused issue keeps the draft and becomes a Sarcină saying why. It never submits:
  SPV uploads follow the auto-upload rule and the mode.

## 3. VAT, currency and exchange rates

`calculate(lines, vat_payer)` is the one calculation rule. Line nets round to two decimals, half up; VAT is
computed once per category and rate on the sum of rounded nets. Shop orders may carry `gross_unit_price` per line:
then the net still rounds per line and the category's VAT is gross minus net. All lines of a category use the same
mode. Negative quantities are discounts with non-negative prices; a category base can't be negative.

Categories and rates: S at 21 or 11; Z, E, AE, K, G and O at zero. E, AE, K, G and O need an exemption reason or
VATEX code. A seller that is not a VAT payer uses O with the art. 310 mention. O cannot mix with other categories.
K needs a delivery date, a buyer VAT id and a real client region before numbering. A seller flagged
`vat_on_collection` gets the `TVA la încasare` mention, only on invoices whose lines are all S; mixed S treatments
refuse.

Any three-letter currency is accepted with a positive rate published strictly before the tax point.
`bnr.rate(L, currency, day)` fetches it from curs.bnr.ro (daily feed, then the yearly archive; only when no rate published before the day exists and the day is
1 to 10 January, the previous year's archive), parses the XML with DTDs refused, matches elements by local name across
both BNR namespaces, normalises multipliers to RON per unit and caches only final selections (a publication on or
after the day exists). Weekends and holidays take the last publication before the day; future dates refuse.
`ron_totals` gives the RON totals for XML, the app and the PDF: VAT is the invoice-currency VAT converted, and the
RON total is converted net plus converted VAT. This is not a D300 calculation.

Legal basis, condensed (sources: the ANAF consolidated Fiscal Code and norms, OMF 4092/2022, VAT Directive):

- Exchange rate: Fiscal Code art. 290(2) with norm 35(1): a BNR publication is valid from the next day, so the
  rate is the latest one published strictly before the tax point (art. 281–282). Only one tax point per invoice is
  represented; separate advances at different rates are not combined into one rate.
- RON VAT: art. 319(23) converts VAT stated in another currency under art. 290, so the frozen invoice-currency VAT
  is converted rather than recomputed from a rounded RON base. BR-53 only requires BT-111 to be present when BT-6
  is; BR-RO-Z2 allows two decimals.
- Client identity: art. 316(1) and 319(20)(f); a Romanian VAT id attached to another CUI is refused. EU VAT ids
  carry their country prefix (Directive art. 215, Greece as EL); format checks are local, there is no VIES lookup.
- Cash accounting: art. 319(20)(p) requires the mention only when collection determines chargeability; E, K, G,
  AE and O are excluded (art. 282(6)). The seller flag is a declaration, not an eligibility check.
- Art. 310 is the small-business exemption, not an option for a VAT payer. Non-EU B2B services placed at the
  recipient (art. 278(2)) use O with that reason; the buyer's country alone never selects it.
- Advance receipt: BT-7 is written (BT-7 and BT-8 are mutually exclusive) and the date is on the PDF
  (art. 319(20)(c)).
- O invoices follow BR-O-02: no VAT scheme, the seller's CUI without prefix as BT-32.
- Northern Ireland: a GB address with an XI VAT id is accepted for K goods only.

## 4. UBL / CIUS-RO and the official validators

`ubl.generate(L, invoice_id, *, company)` writes a frozen invoice as UBL 2.1 Invoice type 380 with CIUS-RO
CustomizationID 1.0.1 (the 1.0.9 rule package keeps that id). `ubl.validate(bytes)` checks the subset fiscalm
writes: totals, category bases, VAT and reasons, addresses, length limits and required fields; it is not the full
official Schematron. Allowances, charges and prepayments refuse. Foreign-currency invoices carry RON tax totals.

Buyer identity, under `cac:AccountingCustomerParty/cac:Party`:

| Buyer | Element | Value |
|---|---|---|
| EU company with a VAT id | `cac:PartyTaxScheme/cbc:CompanyID`, scheme VAT | full VAT id with country prefix |
| Company with a fiscal id, including non-EU without VAT | `cac:PartyLegalEntity/cbc:CompanyID` (BT-47) | `cui` as entered; the VAT id when there is no `cui` |
| Private person without a fiscal id | `cac:PartyLegalEntity/cbc:CompanyID` | `0000000000000` |

A company must supply its identity; the private-person placeholder is never used for one.

Contributors can run the official validators offline:

```sh
uv run python tools/validate_cius_ro.py [--offline] invoice.xml ...
```

It runs the OASIS UBL 2.1 XSD, the CEN EN16931 1.3.8 Schematron and the Romanian
`ROeFactura-UBL-validation-Invoice_v1.0.9.xslt`, the pair phive-rules registers for CIUS-RO 1.0.9. URLs and SHA-256
pins are in the script; a hash mismatch refuses to run. Downloads are cached in
`$HOME/.cache/fiscalm-cius-ro-validation` (`FISCALM_CIUS_CACHE`, which must be outside the repository) and never
enter git. The Romanian stylesheet comes from phive-rules and is not authenticated against a fresh download from
the Ministry of Finance; an offline test checks that its assertions match the archived Ministry package. Saxon runs
with extensions, DTDs, XInclude and external reads disabled. Warnings fail the corpus. Licences: CEN artefacts
EUPL-1.2, phive-rules Apache-2.0, OASIS schemas under OASIS notices, Saxon-HE MPL-2.0. The application never
downloads or runs these tools.

The corpus test issues synthetic invoices (and their storno) through the real paths and validates each:

```sh
FISCALM_CIUS_CORPUS="$HOME/.cache/fiscalm-cius-ro-validation/review-corpus" \
  nice -n 10 uv run pytest -q -n 2 tests/test_cius_ro_official.py
```

Without the cache the artefact checks are skipped. Passing these validators is not SPV acceptance: ANAF also
looks up the recipient, and its public validator answers `ERRIdentif` for foreign company ids.

## 5. PDF

`invoicing.pdf.render(L, invoice_id, *, company)` returns A4 PDF bytes from the frozen invoice. It embeds subset
Inter (Regular, Medium, SemiBold; SIL OFL 1.1, licence in `invoicing/fonts/inter/`) with a Unicode map so Romanian
text can be extracted, wraps long fields and paginates lines. It shows totals, RON conversion, the VAT summary, the
bank account, exemption reasons and the reverse-charge or non-payer mentions. A character the font lacks refuses.

## 6. Storno and refund drafts

`storno(L, invoice_id, today, reason, lines=None, *, company)` issues a new numbered type-380 invoice with
negative quantities and a BillingReference to the original (ANAF's invoice-type guide v2.9 allows a negative 380).
`lines` lists 1-based positions of whole original lines, or all of them. Parties, tax treatment and exchange rate
are copied from the original; VAT left on the original is allocated so the totals reverse exactly. Each original
line is reserved once, in the same transaction as the new number, so two connections can't reverse it twice.
Refused: drafts, a storno of a storno, duplicate or already reversed lines, and a date before the original's.
A fully reversed original gets status `storno`; its content does not change.

The original must have been accepted by SPV. Without any submission, a storno is allowed only when the invoice
did not need submission or the installation is in Sandbox. A rejected invoice is never reversed: it keeps its
number and signed answer, and the correction is a new draft issued with a new number.

A refund draft (`invoice_refund_drafts`) is an unnumbered storno selection prepared for a shop order's refund or
return (`commerce/`). It can't be edited and is issued with the same version and reversal guards under its own id.

## 7. SPV submission

### Environments

ANAF has a TEST and a production API. The environment follows the installation's mode
(`efactura/client.py` `env()` and `outward_allowed()`):

| | Sandbox | Live |
|---|---|---|
| Reading received and issued invoices | production | production |
| Manual upload, status polling, `recover` | ANAF TEST | production |
| Automatic upload after issuing | no | yes, when the company's auto-upload is on |

A submission is identified by invoice and environment: TEST history never blocks production, and an accepted
answer from TEST is not a fiscal transmission. `FISCALM_ANAF_ENV` overrides the environment for development and
tests only. Answers from TEST are kept but never imported as issued invoices; the invoice itself is collectable
from the moment it is issued.

### Deciding and uploading

Issuing never uploads: a person reviews the invoice first and can send it at once with `invoice upload`.
`spv.upload_on(L, invoice)` is the day `invoice tick` (`spv.auto_upload`) sends it on its own: `upload_after_days` (0 to 5
calendar days, default 4, inside ANAF's 5 working days, set per firm with its auto-upload rule in Automatizări
(`rule-spv-delay`, offered only while the rule is on)) after the issue date. It is set only in Live, only for invoices issued in Live
(`issued_live` frozen in the snapshot), only when the company's auto-upload is on (default on; `fiscalm invoice
auto-upload`) and was on at issue, and only when `spv.scope(invoice)` is `required`; otherwise it is null and only
an explicit `invoice upload` sends the invoice. The app shows the day on the invoice (`spv.upload_on`).

`scope` is the single transmission decision. Unless a foreign client is explicitly declared
`ro_vat_status: "not_registered"`, an invoice is `required`; Romanian buyers and buyers with a Romanian VAT id always
are. `supply_country` is the declared place of supply (default `RO`), never inferred from an address. Optional
upload is allowed for a foreign B2B buyer marked `efactura_registered: true`. A foreign Romanian VAT id must match
the client's numeric `cui`. Invoices that need no submission are still issued and exported normally.

The upload route is `upload`, or `uploadb2c` for a buyer without a fiscal id; a buyer outside Romania adds
`extern=DA`. A committed `uploading` reservation precedes the request, and an upload holds
`<FISCALM_HOME>/state/spv-upload.lock` until its outcome is written. A timeout or malformed answer becomes
`unknown`; a crash can leave `uploading`. Neither is retried automatically, because ANAF has no idempotency key. A
known rejection can be retried explicitly; automatic processing never repeats one. A rejected invoice is never
reversed with a storno: it is corrected with a new draft and a new number (section 6). An invoice SPV accepted is
never uploaded again: after an upgrade, fiscalm may generate different XML or PDF for the same invoice, and that is
not a reason to resubmit it. Only rule codes from ANAF's
error text are kept, since it can echo bank details.

`invoicing.poll(L)` checks pending uploads in the current environment and stores the signed answer under
`<FISCALM_HOME>/efactura/<company>/emise/<issue-month>/`. The accepted XML must match the uploaded SHA-256. Files are
written through no-follow directory descriptors and published atomically; a signed file is never overwritten. A
crash can leave an unused hidden `.part` file.

### Recovery

For `unknown` or `uploading`, check SPV first, then:

- `fiscalm invoice recover ID --company CO --from YYYY-MM-DD --to YYYY-MM-DD` searches the signed sent invoices in
  that inclusive range and needs exactly one match by seller CUI, number, date and checksum;
- `fiscalm invoice resolve ID --company CO --upload-id N --checked-in-spv` records an upload you found, or
  `--not-uploaded --checked-in-spv` records that none happened. An `uploading` row can be resolved only while no
  upload holds the lock.

### Connecting ANAF: how the OAuth return is bound

ANAF's procedure leaves `scope` and `state` empty (sending a `state` broke the certificate login), so fiscalm binds
the return another way (`efactura/client.py`):

1. `integration-connect` (an administrator) records an attempt (integration, Client ID, callback URL, who, when)
   and returns a start link `/anaf/start/<random>`; only its SHA-256 is stored, and a new start cancels older ones.
2. Opening the link, once and within 10 minutes, sets an `HttpOnly; Secure; SameSite=Lax` cookie scoped to
   `/anaf/callback` and redirects to logincert.anaf.ro.
3. `GET /anaf/callback` reads only `code` and `error`, claims the attempt named by the cookie once (a guarded
   `UPDATE`), checks the starter is still an administrator and the integration, Client ID and callback URL are
   unchanged, and exchanges the code (Basic client authentication, `token_content_type=jwt`).
4. `_verify` then lists one day of received invoices with the new token and needs at least one of the
   installation's firms to answer. The cookie proves a pending attempt in this browser, not whose certificate made
   the code; this check refuses a certificate without SPV rights for these firms. The tokens are saved only if no
   newer attempt started meanwhile. Any refusal changes nothing.

Access tokens last 90 days and are refreshed 14 days before they expire; the refresh token lasts 365 days and
ends the authorization (a reconnect is asked for 30 days before). Two processes refreshing at once keep the first
saved pair. Tokens never appear in app state, listings, the callback page, `fiscalm efactura auth-status` or logs.
Connecting is configuration and works in Sandbox: it calls the OAuth server and makes read-only listings.

## 8. CLI

Commands are under `fiscalm invoice` (`--help` lists each command's options):

```sh
fiscalm invoice seller seller.json
fiscalm invoice client client.json
fiscalm invoice series create --company EXAMPLE --code ABC --start 42
fiscalm invoice series list --company EXAMPLE
fiscalm invoice series default --company EXAMPLE --code ABC
fiscalm invoice series remove --company EXAMPLE --code ABC     # only while no invoice, draft or recurring invoice uses it
fiscalm invoice draft draft.json                       # replace one: --id ID --version N
fiscalm invoice show INVOICE_ID --company EXAMPLE
fiscalm invoice issue INVOICE_ID --company EXAMPLE --version 1 --today 2026-10-31
fiscalm invoice xml INVOICE_ID --company EXAMPLE --output invoice.xml
fiscalm invoice pdf INVOICE_ID --company EXAMPLE --output invoice.pdf
fiscalm invoice storno INVOICE_ID --company EXAMPLE --today 2026-11-01 --reason "Return" --lines 1
fiscalm invoice upload INVOICE_ID --company EXAMPLE
fiscalm invoice poll
fiscalm invoice auto-upload --company EXAMPLE off
fiscalm invoice rate EUR --date 2026-10-31
fiscalm invoice lookup-client --company EXAMPLE --cui 99999991 --date 2026-10-31
fiscalm invoice recur run --today 2026-11-01
```

Seller and client files are `{ "company": "EXAMPLE", "profile": { … } }`; a draft file is
`{ "company": "EXAMPLE", "series": "ABC", "client_id": "…", "data": { … } }`. `--version` comes from `show` and is
required with `--id` and for `issue`. Output files must not exist. `show` prints a summary without profile input
or bank details. `invoice tick` runs `spv.auto_upload` for issued invoices that were issued in Live (`issued_live`) and have no
submission in the current environment, uploading those whose `upload_on` day has come, then polls; the scheduled tick calls it. `invoice partner-status` refreshes partners' VAT and inactive
status from ANAF's public lookup. No option weakens a mode guard or takes a URL from input.

`efactura/public.py` `lookup(cui, day)` uses ANAF's public v9 taxpayer service (no credentials, one request per
second, 20-second timeout) and the registered-office address. Inactive, ambiguous or malformed answers refuse.
A seller's bank details are always entered by hand. Tests inject every transport and never reach a real host.

## 9. Known limits

- Checked against ANAF TEST (2026-11-10), each accepted: `upload` of a domestic B2B invoice with standard VAT, a
  storno of it, an EUR invoice, and a category K invoice to an EU company with `extern=DA`; `uploadb2c` for a
  private person. An invoice altered in transit came back `nok` with its rule id and was recorded as rejected;
  the unaltered invoice was accepted on a second upload. The art. 310 exemption is covered only by fake-transport
  tests, and nothing has been uploaded to production.
- Local XSD and Schematron validation is not SPV acceptance; foreign company ids fail ANAF's public validator
  with `ERRIdentif`.
- The five-working-day transmission deadline has no reminder.
- The client model can't record a CNP-identified person in the mandatory register; don't mark such operators
  `private`.
- `supply_country` is a declaration; fiscalm does not determine the place of supply.
- One tax point per invoice: advances and their regularisation at different rates are not supported.
