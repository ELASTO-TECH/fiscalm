# Payment files

fiscalm prepares bank files. A person downloads a file, imports it into the bank's app, checks it and signs there.
Each firm can have several named files. Files have no monthly period. Payments appear in a file's detail, while
the list shows files, totals and status.

An open file accepts payments and can be renamed. Downloading keeps it open; a later edit means another download
is needed. A file marked imported is closed. Only statement or mail evidence can confirm its payments. A payment
marked paid by a person still awaits proof; skipped payments do not count as confirmed.

The chooser offers open files without an unanswered question, or creates a file and adds invoices atomically.
The engine supplies an editable name from the firm's short name and the date, adding a suffix when needed.
Automatic supplier rules use the newest open file without questions or previous downloads, or create one. They
leave any file the person has already downloaded untouched.

Downloads are recorded per file with their membership revision, payment IDs and value date. Import confirmation
accepts the latest recorded download only while its membership is current. A missing file, several downloads or
an orphan file requires an explicit answer. Questions freeze edits. Import and deletion moves are journalled
before changing disk files and recovered on the next run.

Sandbox allows local file creation and edits. Generating or downloading a bank file requires Live.

## Which format

The format comes from the account the payment debits: the firm's paying account (`pays: true`) and its bank's
catalogue entry (`src/fiscalm/bank/catalogue.py`, `Bank.payment_file`).

| Format id | Bank | Accounts | File |
|---|---|---|---|
| `btgo-csv` | Banca Transilvania (BT Go) | RON | `<id>.csv` |

A bank without a supported format gets no bank file. Payments stay pending and the download explains why.
Once exported, a payment keeps the account it was exported from; a changed paying account cannot redirect it.

The app uses `/web/api/payment-files` and `/web/api/payment-files/{id}`. Exact fields and actions are generated in
`docs/openapi.json`. CLI downloads also require a file ID: `fiscalm pay build <file_id>`. Import confirmation takes
both that ID and its recorded download ID: `fiscalm pay imported <file_id> <download_id>`.

## Adding a format

1. Add a module under `src/fiscalm/payments/` with `build(items, value_date) -> bytes`,
   `check(data, company) -> list[str]` and `value_date(data) -> date | None`. Building must be deterministic and
   recheck every row's source account, bank and currency. Refuse uncertainty with `PaymentFileError`.
2. Register it in `payments/files.py` `FORMATS` with its ID, bank app name, extension and supported currencies.
3. Put its ID on the bank's catalogue row (`Bank.payment_file`).
4. Test rows and refusals with synthetic data, plus per-file download and import with the new format.

The lifecycle and journal logic use the registered format. No bank-specific action or screen is needed.
