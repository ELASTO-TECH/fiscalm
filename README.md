# fiscalm

fiscalm is a self-hosted bookkeeping and reconciliation app for small Romanian companies.
It keeps track of what the company has to pay and collect, checks it against the bank, and
prepares what the accountant needs. One installation can hold any number of companies.
The engine is Python and keeps a SQLite ledger on your server; the web app is in Romanian and
works first on a phone.

## What it does

- Reads the invoices the companies receive and issue through ANAF e-Factura.
- Issues invoices: drafts, PDF, CIUS-RO XML, submission to SPV, storno, recurring invoices.
- Reads bank statements (uploaded, from mail, or through open banking) and matches payments
  and collections to invoices; settles marketplace and courier payouts from their reports.
- Reads supplier invoices that arrive by mail (read-only IMAP).
- Prepares payment files for the bank's app and the accountant's monthly pack as a ZIP.
- Imports orders from online shops (Shopify, eMAG, WooCommerce, PrestaShop) and invoices them.
- Imports a company's earlier invoices from another program.
- Accounts with password plus TOTP or passkeys, installation-wide roles, API tokens,
  an HTTP API and an MCP server.

## What it never does

- It moves no money. It prepares a payment file; a person imports it into the bank's app and signs there.
- It sends no mail and never writes to a mail account. A person downloads invoices or the accountant's
  ZIP and sends them; payment reminders are text to copy.
- When unsure, it settles nothing. A bank line that fits several invoices pays none of them, and an
  invoice a person marks paid waits for a statement or a mail that proves it.

## Status of 0.1.0

This is the first public release. The test suite runs on synthetic data and fakes for every outside
service. What that does not prove:

- ANAF's TEST environment has accepted invoices sent from fiscalm to a Romanian company, a private person and an
  EU company, a storno and an invoice in a foreign currency. ANAF rejected an invoice altered while it was
  being sent, fiscalm recorded the rejection, and ANAF accepted the unaltered invoice when it was sent again. A
  seller outside VAT, fiscalm's own ANAF connection screen and production are unverified against ANAF. Local
  XSD and Schematron validation is not SPV acceptance.
- Live ANAF, open-banking, IMAP, AI, Drive and shop connections are not exercised by the tests.
  Check each connection you use before relying on it.

Limits:

- Docker Compose is the only install path. It has been run on Linux/amd64; other hosts and architectures are
  unverified. No prebuilt container image is published.
- Payment files: one format, BT Go CSV for RON accounts at Banca Transilvania
  ([payment files](docs/payment-files.md)). Payments from other banks are listed for a person to pay by hand.
- Statements: BT monthly PDF, CAMT.053 XML, MT940 and CSV with columns mapped per account
  (a CSV is a partial import), plus transactions read through Enable Banking.
- It does not replace the accountant.

## Try it in five minutes

```sh
git clone https://github.com/ELASTO-TECH/fiscalm.git && cd fiscalm
bin/dev
```

This needs uv and Node 22. It installs the locked dependencies and serves the app at
http://localhost:3000 with invented fixture data: no ledger, no accounts, no HTTPS, nothing saved.
See [development](docs/development.md) for `bin/dev engine`, which runs a real engine in shadow on a synthetic
ledger kept in `.dev/`, and for `bin/test.sh quick` and `bin/test.sh full`.

## Install

Docker Compose: `docker compose up -d --build` installs; an upgrade is a backup, `git pull`, then the same command. It runs three services: `web` (the app and
API), `scheduler` (a tick every six hours by default, `FISCALM_TICK_INTERVAL`, and on "Verifică acum") and `backup`.
An HTTPS reverse proxy goes in front. Follow the [installation and operations guide](docs/self-hosting.md),
then [connect ANAF](docs/efactura-anaf.md).

## Sandbox and Live

A new installation starts in Sandbox. Everything that stays on the server works there, and invoice
uploads go only to ANAF TEST; payment files, production uploads, shop write-backs and webhooks are
blocked. An administrator switches to Live in Setări › Mod and confirms it in a dialog that says what changes;
a session whose identity was last confirmed over ten minutes ago confirms it again first.

## What leaves your server

Sandbox is not offline: reads from the services below also run in Sandbox.

| Host | When | How to turn it off |
|---|---|---|
| `logincert.anaf.ro` | Connecting ANAF (OAuth) and refreshing its token | Don't connect ANAF |
| `api.anaf.ro/prod` | Each tick reads received and issued invoices; in Live, uploads and status checks | Don't connect ANAF |
| `api.anaf.ro/test` | In Sandbox, invoice uploads and status checks | Don't connect ANAF |
| `webservicesp.anaf.ro` | Looking up a company or client by CUI; each tick, partners' VAT and inactive status (CUIs only, no credentials) | No switch |
| `curs.bnr.ro` | Exchange rate, when a foreign-currency invoice, shop order or due recurring invoice needs one that is not cached yet | No switch; with only RON documents no request is made |
| `api.github.com` | Daily check for a newer release (anonymous) | "Verifică versiunile noi" in Setări |
| Your AI provider (Anthropic, an OpenAI-compatible server, or the Claude Code or Codex CLI) | Text of mails and documents that rules can't decide, receipt images | Choose no AI service |
| Your IMAP server | Reading invoices, statements and payment orders from mail | Turn off reading on the mail account |
| `api.enablebanking.com` | Connecting a bank and reading its transactions | Don't set up open banking |
| Google Drive, through rclone | Copying documents to the accountancy folders (Live) | Don't add a Drive archive |
| Your Shopify store | Reading orders, refunds, fulfillments and Shopify Payments payouts; in Live, writing the invoice number and a fiscalm link (for signed-in users) to the order | Don't add the shop |
| `marketplace-api.emag.*` | Reading orders, returns and AWBs; in Live, attaching the invoice link to the order. eMAG then downloads the invoice PDF, with its amounts and both parties' details, through that link, which works for 24 hours after it is attached | Don't add the shop |
| Your shop's webhook address | Order and invoice events (Live) | Leave the webhook address empty |

## Documentation

- [Installation and operations](docs/self-hosting.md)
- [Connecting ANAF e-Factura](docs/efactura-anaf.md) (Romanian)
- [API and MCP guide](docs/api.md) and the [OpenAPI contract](docs/openapi.json)
- [Screens and navigation](docs/information-architecture.md)
- Areas: [invoicing](docs/invoicing.md), [payment files](docs/payment-files.md),
  [settlements](docs/settlements.md), [mail invoices](docs/mail-invoices.md),
  [history import](docs/history-import.md), [eMAG](docs/emag.md)
- Shop plugins: [WooCommerce](integrations/woocommerce/README.md),
  [PrestaShop](integrations/prestashop/README.md), [Shopify](integrations/shopify/README.md)
- Development: [local development](docs/development.md), [testing](docs/testing.md),
  [the app](app/README.md), [Romanian copy guide](docs/copy.md)

## Contributing, security, licence

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md). `main` holds the latest release; `dev` holds the next one.

Licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only).
The maintaining company is named in [CLA.md](CLA.md). Site: [fiscalm.ro](https://fiscalm.ro).
