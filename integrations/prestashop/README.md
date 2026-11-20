# fiscalm for PrestaShop

Sends PrestaShop orders, AWBs, cancellations and credit slips to fiscalm, which drafts or issues the invoices, and
shows staff the invoice and its SPV state. PHP 8.1+, PrestaShop 8.x/9.x,
PHP cURL and BCMath required. Module code is AFL-3.0 (see
`fiscalm/LICENSE`); the rest of fiscalm remains AGPL-3.0-only. No runtime package
manager or third-party PHP library is needed.

Copy `fiscalm/` to the shop's `modules/fiscalm/`, install through Module Manager,
and configure separately for each shop. Set HTTPS URL, write token, company key,
channel (the enabled fiscalm shop integration's ID), trigger order-state ID and
webhook secret (at least 32 characters). Blank secret fields retain saved values;
secrets are never returned in HTML or API error messages. PrestaShop stores them
in its configuration database; protect that database and backups. Use a dedicated
revocable token. Redirects are refused. Company, URL and channel cannot change
after the first queued order, preventing a retry from invoicing in another company.

Company and VAT number/CUI use the invoice address's standard `company`,
`vat_number` fields. Enable B2B in PrestaShop's customer preferences.
Company invoices without a fiscal identifier are refused. Currency and historical
order prices are preserved, with decimal arithmetic. Shipping and single-rate cart discounts are separate lines. Wrapping is held for
review until its historical VAT rate can be established. Mixed-rate cart discounts or totals
that do not reconcile are refused for manual review. Unknown payment modules use
`other` with the module name as processor; payment claims never settle an invoice.

Courier modules that write PrestaShop's `order_carrier.tracking_number` work without
configuration (including installations of Romanian courier modules that use this
standard field). Versions storing an AWB in another column can configure that exact
column. Multiple tracking numbers are ambiguous and are omitted. The official Sameday adapter also reads the prefixed `sameday_awb` table when
the order uses `samedaycourier`, and watches its ObjectModel hooks. Conflicting
standard and module numbers are refused. Interface checked against
[sameday-courier/prestashop-plugin](https://github.com/sameday-courier/prestashop-plugin/blob/master/classes/SamedayAwb.php);
no vendor code is copied. Other proprietary Fan Courier, Cargus or DPD schemas
require synchronization into the standard tracking field or a configured column;
confirm their exact installed version before adding an adapter.

Order hooks only persist jobs, so checkout does not wait on fiscalm. Both standard back-office tracking updates and ObjectModel courier updates enqueue AWB changes.

The order's fiscalm panel shows sync state, invoice and SPV state. PDF downloads
are proxied by a staff-only admin controller, with shop, employee permission and
CSRF checks. The bearer never reaches a customer or a URL. Grant access to the
hidden fiscalm PDF tab only to staff who can read orders.

The queue is durable in `fiscalm_job`, isolated per shop, with one worker lock,
immutable intake snapshots, exponential backoff and at most eight attempts.
Network/429/5xx responses retry; other refusals remain blocked. In production run
this command every minute using the shop owner's scheduler:

```sh
php /path/to/prestashop/modules/fiscalm/cron.php /path/to/prestashop SHOP_ID
```

The worker also catches up recent orders in the configured state and refreshes invoice state. State/AWB hooks never send HTTP. The settings button retries blocked
requests after the operator checks fiscalm. Orders refused locally during mapping are remapped only after an explicit retry; submitted snapshots are retained.
Refunds use stable PrestaShop credit-slip IDs and preserve the posted line order.
The server creates an unnumbered draft for review, not an automatic issued credit. Whole product positions (including
selected whole lines of a larger order) can be credited; quantity-only/amount-only
partial lines, discount allocation and partial shipping require manual review.
Refund and cancellation sends are persisted as uncertain before the HTTP call and never retried
automatically, even after a crash. The order panel lists every pending/refused/uncertain request. After checking fiscalm, the settings page can explicitly close one uncertain request without sending it again. Known local refusals remain blocked and can be retried. The module does not refund money or send mail.

## Webhooks

Set the shop integration's `webhook_url` in fiscalm to the module's webhook address and use the same webhook
secret (at least 32 characters) in both places. fiscalm sends events only in Live; in Sandbox the cron worker
refreshes invoice and SPV state instead. The headers, signature and body are specified in the `x-shop-webhooks`
section of [openapi.json](../../docs/openapi.json). The module accepts at most 300 seconds of clock skew, deduplicates
event ids after a successful refresh, and treats an event only as a trigger for an authenticated read of the order
(`id`, `invoice_id`, `status`, optionally `invoice_number` and `spv_status`); event fields never supply invoice URLs
or overwrite invoice state. A refusal notification never cancels an accepted order or blocks a corrected retry.

## Known limits

- Refunds cover whole invoice positions only; quantity-partial refunds, ambiguous discount allocation and
  historical wrapping VAT need manual review.
- A refund or cancellation request that may have been sent is never retried automatically, even though fiscalm
  treats an exact replay as idempotent. Cancelling an issued invoice is refused by fiscalm.
- Foreign billing addresses without an administrative subdivision are held: the order contract requires a county
  for every country.

## Verification

Run `bin/e2e.sh` for both PrestaShop versions, or `PS_VERSION=9.0.1 bin/e2e.sh`
for one. Rootless Docker only; no published ports. Disposable MariaDB, actual
PrestaShop module install and order hook, intake mock plus real fiscalm
commerce routes in Sandbox. Every credential is randomly generated for the run.
Containers and temporary databases are removed on exit. Unit mapping tests cover
inclusive/exclusive prices, discounts, shipping, rounding, B2B/B2C and whole-line
and partial-quantity refund decisions. The same command then provisions a fake seller/series/channel and a private write token,
submits a real mapped PrestaShop order to fiscalm, reads the persisted draft, replays
the same order, verifies that issuing is refused in Sandbox, updates AWB and cancels the draft.
A private test fixture then seeds one synthetic historical issued invoice directly
in the isolated ledger, without LIVE or SPV upload. The module reads real PDF
bytes and the invoice number, and creates an exact unnumbered refund draft.
No LIVE marker is created. No ANAF, mail or payment endpoint is contacted.
