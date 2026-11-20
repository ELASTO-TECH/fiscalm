# Changelog

Product versions describe fiscalm releases. The API and the ledger schema have their own
version numbers. Back up before upgrading; see [self-hosting](docs/self-hosting.md).

## Unreleased

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
