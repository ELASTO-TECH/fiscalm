# fiscalm for WooCommerce

Sends WooCommerce orders and refunds to fiscalm, which drafts or issues the invoices, and shows customers their
invoice. PHP 8.1+, WordPress 7.0,
WooCommerce 11.2.1, MySQL/MariaDB; HPOS and legacy orders. Copy this directory
(without tests, vendor or .runtime) into `wp-content/plugins/fiscalm` and activate.
Licence: [GPL-2.0-or-later](LICENSE.md), separately from the AGPL engine.

## Setup

In WooCommerce → fiscalm enter the HTTPS installation URL, write token, company
key, shop integration ID and the sending status. Create the shop integration in
fiscalm with the same company/series. `processing` also accepts orders going
straight to `completed` (downloadable products). A completed-only trigger waits
for completion. Credentials are write-only; blank preserves the existing secret.
They live in a non-autoloaded WordPress option: protect the database and backups.
A compromised administrator/plugin can read them. Uninstall removes the option;
revocation in fiscalm is still necessary for leaked or backed-up credentials.
Redirects and runtime HTTP are refused. Use a dedicated revocable write token.

Company/channel/destination must stay stable after sending. Each order retains
its original binding; changing the configured URL/company prevents old orders
from transmitting the current token to a different destination. Internal immutable
WooCommerce order IDs are external IDs; formatted order numbers are not used.

B2B metadata keys are comma-separated, first non-empty wins. Defaults include
`_billing_cui`, `billing_cui`, `_billing_vat`, `billing_vat`, `_billing_vat_number`
and the standard company field. Set the keys used by the installed checkout
extension. Romanian counties become `RO-XX`; a missing county is refused.

Courier identifiers were checked against published plugin source (no implementation
copied): SamedayCourier Shipping 2.3.1 exposes its AWB repository; GLS Shipping 1.5.6
uses one-element `_gls_tracking_codes`; wc-dpd 9.5.2 uses
`dpd_export_package_number`; Cargus 1.6.0 uses `_cargus_awb`; HgE FAN 1.0.12 uses
`_hgezlpfcr_awb_number`. Legacy scalar aliases are also accepted. Configure an exact
metadata key and courier to override detection. Multiple GLS parcel codes are
refused. Some extensions use private tables, so awaiting AWBs are checked every
six hours for seven days as well as on order saves. Confirm the installed version.

## Behavior and refusals

Historical order totals/currency are used, never current product prices. Shipping,
fees and discounts are separate lines. VAT is checked against the engine's group
rounding (gross prices preserve charged gross); unsupported compound/stacked taxes,
precision/total mismatches or overflow refuse. No currency conversion occurs here.
`paid` requires an allowlisted card gateway, paid date and transaction ID; other
methods remain unconfirmed. fiscalm never settles an invoice from this flag.

Action Scheduler handles jobs, exponential backoff and at most eight network
retries. Inspect WooCommerce → Status → Scheduled Actions and the order notice.
“Trimite comanda în fiscalm” reuses the external ID. Changed issued orders return
`order_changed`; they are not silently replaced. A persisted pending payload can
recover the remote ID after a timeout before cancellation.

Refunds select the original invoice's 1-based whole-line positions. The plugin
persists WooCommerce item-ID/position mappings only after accepted intake. It checks
that the issued invoice version still matches the intake version, includes the
item's discount line and refuses partial quantities/amount-only refunds. Discounted
VAT categories may require all remaining category lines together, enforced by
fiscalm. A refund request is attempted once: after uncertain transport, check
fiscalm manually before any further action. It never invokes a payment gateway.

Invoice PDFs are proxied server-side and require a logged-in owning customer or
shop manager. Optional customer email links contain no API token. Guest customers
need manual invoice delivery; logged-out account owners are sent to login. Internal
errors never appear in customer emails. SPV state is shown to managers in Romanian.

## Webhooks

Configure the REST URL the plugin displays as the shop integration's `webhook_url` in fiscalm, and the same secret
(at least 32 printable characters without spaces) as `webhook_secret` in both places. fiscalm sends events only in
Live. The headers, signature and payload are specified in the `x-shop-webhooks` section of
[openapi.json](../../docs/openapi.json); the plugin refuses unsigned events, events older than 300 seconds, tampered
bodies and events for another binding. An invoice event makes the plugin fetch the order's current state under the
order lock, so a replayed or out-of-order event can't apply old data. A busy or failed refresh returns 503 so fiscalm
retries. An old refusal can't replace a later accepted order.

## Tests and dependency gate

Run `bin/e2e.sh`: isolated WordPress 7.0 + WooCommerce 11.2.1 + MariaDB + contract
fixture, PHPUnit on real WooCommerce CRUD with HPOS, followed by real fiscalm in
Sandbox. The real stage is enabled when `src/fiscalm/commerce/orders.py` exists; it does
not replace a failed real stage with a mock. `WC_TEST_PHP_VERSION=8.1` checks the
minimum PHP version (default 8.3). Containers and volumes are removed on exit.
A temporary shop port is bound only to loopback. All credentials are generated per
run; external HTTP, mail and WP cron are disabled during tests. No live marker is
created. Signed HTTP callbacks/PDF responses use the fixture: Sandbox
forbids the engine from sending outbound webhooks or issuing real invoices.
`vendor/bin/phpcs` checks shipped PHP against WordPress-Core.

The real-engine stage seeds one synthetic issued invoice through a test-only fixture listener in the test
container, without creating LIVE (the public issue endpoint is asserted to refuse in Sandbox), then creates a real
unnumbered storno draft and checks its total and the PDF bytes. The fixture listener is never part of fiscalm's API
or image.

## Known limits

- Partial quantities cannot be represented by the whole-line refund API. A refund
  of one unit from a two-unit line requires manual review; the plugin never expands
  that into a full-line refund. Discount allocation is also conservative.
- Zero-VAT treatment needs an explicit category/exemption, absent from the fixed
  order body. A VAT-paying seller's zero-rate lines currently refuse in fiscalm.
- There is no event-specific refund idempotency key. Automatic refund retries stay
  disabled after a request attempt even though repeating a still-pending selection
  currently returns its existing draft.
