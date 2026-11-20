# Shopify for fiscalm

No plugin runs in Shopify: fiscalm uses Admin GraphQL **2026-10**, a custom-app Admin
token and authenticated webhooks. There is no public Shopify app or OAuth installer.

## Self-hosted custom app, step by step

1. Serve fiscalm behind HTTPS. Keep the listener private behind the proxy; expose
   POST `/hooks/shopify/<integration-id>` to Shopify without bearer authentication.
   It authenticates raw bytes with Shopify's HMAC instead. Configure a request size
   limit of 256 KiB and a request timeout at the proxy. Do not log headers/bodies.
2. In Shopify's **Dev Dashboard**, create an app, configure Admin scopes
   `read_orders`, `write_orders` and `read_companies` (B2B), and for payouts
   `read_shopify_payments_accounts` and `read_shopify_payments_payouts`; create/release its version,
   and install it on the store. Customer/address access must be granted; missing
   billing details refuse rather than inventing a buyer. Standard order access is
   limited to the last 60 days; request `read_all_orders` for older backfill.
3. Obtain an Admin access token through Shopify's documented custom-app auth flow.
   Apps and stores owned by the same organization can use the client credentials
   grant (`POST https://<store>.myshopify.com/admin/oauth/access_token`,
   `grant_type=client_credentials`, client ID and client secret). Keep these values
   in a secret manager. Tokens expire: arrange renewal outside fiscalm, then replace
   `admin_token` using the same write-only integration update. fiscalm does not
   currently refresh this grant. Existing admin-created legacy custom apps can
   retain their Admin token. Since January 2026 new apps use the Dev Dashboard;
   there is no new legacy “reveal permanent token” step.
4. Create the company and an invoice series in fiscalm first. Add a `shop`
   integration with platform `shopify`, company, series, `shop_domain` (the exact
   lower-case `<store>.myshopify.com`, no scheme/path), and `fiscalm_url` (the
   installation's base URL, no query or credentials). Leave `auto_issue=false`.
   Supply `admin_token` and `shopify_secret` (the app client secret used by Shopify
   for webhook signing) as secrets. They are write-only, stored in 0600 files.
   CLI: `fiscalm integration add shop --name 'Synthetic Shopify' --set platform=shopify
   --set company=NORD --set series=TEST --set shop_domain=synthetic-shop.myshopify.com
   --set fiscalm_url=https://books.example.test --secret admin_token --secret shopify_secret`.
   With multiple secrets, CLI input is one line per secret on stdin in argument order, supplied from a
   secret manager; see `fiscalm integration add --help`. Never put tokens in arguments.
5. Register JSON subscriptions for `orders/create`, `orders/updated`,
   `refunds/create`, `fulfillments/create` at the HTTPS hook URL above. Use the
   matching app's secret, not fiscalm's outbound `webhook_secret`. The shop-domain
   header must match configuration. Payload URLs/company IDs are ignored.
6. Run `fiscalm shopify-sync` and inspect the draft in fiscalm. Review taxes,
   currency, client and totals before enabling automatic issue. The installed tick
   runs this reconciliation in Sandbox too. Turn on `auto_issue=true` only
   when reviewed and Live. Nothing pays, sends mail or marks an invoice collected.
7. On a live tick, issued-live invoices write the number and an authenticated
   fiscalm sheet URL into order metafield `fiscalm.invoice` (JSON). This is a link
   for authorized fiscalm users, never a public PDF or bearer credential. Repeated
   sets are idempotent; a crash before recording success repeats the same value.

## Mapping and refusals

Webhook HMAC is base64 HMAC-SHA256 over the exact bytes, checked before any
write. Verified hooks only journal the configured channel's order ID, then return
200 within Shopify's five-second deadline. In Sandbox they acknowledge without any
write. The tick fetches current GraphQL data, so reordered/replayed payloads do
not reapply stale snapshots. All line, shipping and refund-line connections paginate;
an updatedAt recheck rejects torn reads. Intake and the accepted revision commit
atomically. A slow older reader cannot replace a newer accepted revision.

Each tick reads the newest 50 orders with overlap, plus the next page of a durable
historical sweep cursor. Queued IDs survive crashes and failures. Processing has a
120-second budget per channel (each request has a 15-second timeout); unattempted webhook IDs take priority over sweep work, then attempted IDs retry.
Unchanged accepted revisions skip refetching. Queue sequences retain a webhook
that arrives while an older snapshot is importing. Invoice-link
writeback runs independently before reconciliation, including when pagination
fails. `read_all_orders` governs historical access. Large backfills span ticks.
Auto issue applies only on first intake of orders placed today, in Live and
opted in. Enabling it never issues existing drafts or older backfilled orders. The Docker
`scheduler` service runs a tick every `FISCALM_TICK_INTERVAL` seconds (six hours by
default) and when someone presses "Verifică acum" in the app; orders processed after
midnight remain drafts for manual issue. For quicker intake, lower the interval or
press "Verifică acum".

Invoices use **shopMoney in the shop currency**, never presentmentMoney. Shopify
records the merchant's sale in that currency; the shopper's converted display price
is not a second sale or reliable invoice exchange rate. fiscalm's normal BNR rules
still apply for foreign shop currencies. The calculated invoice must exactly equal
Shopify's original shop total before auto issue. Edited orders refuse for review.

Original line prices, allocated discounts (including order-level allocations) and
discounted shipping become ordinary engine lines. `taxesIncluded` chooses gross
or net calculation. Rates and tax treatment follow the same refusals as API intake:
unsupported rates, multiple taxes on one line and ambiguous zero VAT refuse.
Gift-card redemption is payment metadata, never a discount or collection proof.
Gift-card **sales** refuse until voucher tax classification is explicit. B2B
purchasing company name and location taxRegistrationId are used when available;
a company name without fiscal identity refuses. Person buyers use billing details.
Romanian province codes map to RO-XX. Missing billing data refuses.

A successful fulfillment's sole courier/tracking pair becomes the AWB; several
numbers refuse for manual review. Cancellation deletes only the guarded unissued
draft. Whole-order and whole-line partial refunds create unnumbered, idempotent storno
drafts when their totals match exactly. Partial quantities, shipping-only refunds
and ambiguous discount allocations require manual line selection through the order refund API.
No refund is automatically issued, sent to SPV or paid. Refund totals, original
invoice and all writes remain scoped to the channel's company.

A hook returns 200 after verified durable queueing (or a Sandbox no-op). Invalid
signatures/headers/IDs and queue failures return generic 409
`shopify_retry_or_review`. Review refusals occur asynchronously and do not cause
Shopify to delete a subscription after repeated failed deliveries. `shopify-sync`
reports imported/refused counts without payloads or secrets; pending IDs remain
for retry. Correct Shopify source data or import a reviewed order through the
regular API. Connector refusals have no task of their own in the app. Throttling,
GraphQL errors and redirects refuse; subsequent ticks retry. No payload selects a
domain, and the connector never calls a money mutation. Handled refund IDs retain
the storno draft/invoice reference; deleting or issuing that draft never regenerates
it and cannot block a later AWB or refund.

## Official references

- [Dev Dashboard custom apps](https://shopify.dev/docs/apps/build/dev-dashboard/create-apps-using-dev-dashboard)
- [Client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant)
- [Legacy admin tokens](https://shopify.dev/docs/apps/build/authentication-authorization/legacy/admin-custom-apps)
- [HTTPS webhook validation](https://shopify.dev/docs/apps/build/webhooks/subscribe/https)
- [GraphQL Order](https://shopify.dev/docs/api/admin-graphql/2026-10/objects/Order)

## Shopify Payments payouts

`fiscalm shopify-sync` also reads Shopify Payments payouts issued after the channel was connected and turns each
complete, consistent payout into a settlement report, which collects invoices only through the shared
[settlement](../../docs/settlements.md) checks. Shopify's paid flag alone never collects an invoice. Scopes, limits and
refusals: [self-hosting, Shopify Payments payouts](../../docs/self-hosting.md#shopify-payments-payouts).
