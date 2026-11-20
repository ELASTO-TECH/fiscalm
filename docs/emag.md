# eMAG Marketplace connector

Reads a seller's eMAG orders and returns, turns orders into invoice drafts, and attaches the issued invoice's PDF
to the order. It has not been verified with a live seller account: test it on the seller's own account before
turning on automatic issue. Reference: the [official eMAG Marketplace API](https://marketplace-api.emag.ro/api-doc).

## Setup

Add a `shop` integration with platform `emag`, the company, the invoice series, and the write-only
`emag_username` and `emag_password`. Automatic issue (`auto_issue`) is off until turned on in Automatizări. `marketplace_country` defaults to RO; BG and HU
use the same documented API, but invoices support only Romanian VAT rates, so BG/HU orders that need other rates or
cross-border tax treatment are refused.

`emag_since` defaults to the moment the integration is set up. Importing older orders requires changing it
explicitly; never move it back with `auto_issue` on unless those orders truly need new fiscal invoices. A change of
company or cutoff starts a new scan cursor.

`emag_pdf_base` (required) is the public HTTPS origin routed to fiscalm's `/emag-pdf/` capability endpoint. Expose
only `/emag-pdf/` through the public proxy, never the app or API listener, and turn off access logging for those
paths.

## Reading

`fiscalm emag tick` runs from the scheduled tick after the invoice tick, with a 120-second timeout. It reads
`order/read` and `rma/read`, in Sandbox too. Basic authentication goes only to the fixed host of the marketplace
country; redirects are refused and no URL from a response is fetched. Reads use the top-level pagination and filter
fields (attachment saves use `data`). Pages hold 100 rows; each scan starts from page one and stops with a refusal
at 1,000 pages. A per-channel process lock prevents concurrent ticks and is released after a crash; never remove
lock files while workers run.

Scans pass `createdAfter` and `modifiedAfter` (with a five-minute overlap), and fiscalm enforces the `emag_since`
cutoff itself. The cursor advances only after a complete scan, including rows refused and recorded as tasks;
transport and unknown failures hold it for a retry. A malformed row becomes a review record and doesn't stop the
others. Connection errors show as the integration's status in Setări.

## Orders

Statuses 1, 2, 3 and 4 enter the shared order intake, which keeps the company/channel/external-id uniqueness and
the manual-edit version guard. Status 0 cancels only an existing unissued draft; a cancelled order not imported yet
creates nothing. Status 5 and partial-storno flags ask for a return review. Orders fulfilled by eMAG (type 2) are
refused until invoice ownership is confirmed. A changed order that is already issued is refused rather than editing
a fiscal document. An imported order always leads to its existing draft or invoice.

Payment modes 1, 2 and 3 map to cash on delivery, transfer and card. `payment_status` is ignored for intake
(`paid=false`); fiscalm never infers a collection from it, even when eMAG collected the cash on delivery.

Amounts use `Decimal`; a product's `sale_price` is net. The order-time `vat` is a fractional rate (0.21 → 21), never
the current catalogue rate; a missing `vat` refuses. Only active product lines are invoiced. Seller-funded product
vouchers become negative net lines with the product's VAT, and their totals must reconcile with every per-product
split; unallocated, mixed or eMAG-funded vouchers refuse. SGR and recycling warranties refuse until their tax
treatment is mapped. A nonzero `shipping_tax` refuses, because the public documentation doesn't state its basis or
VAT rate. The buyer is the order's billing data, never the shipping identity or the hashed e-mail. County names map
to ISO codes by exact, diacritic-insensitive matching; Bucharest needs an explicit sector in the city field.

The current public `OrderProduct` schema omits `vat`, so a seller whose responses lack order-time VAT or shipping tax
evidence gets refusals, not zero-tax invoices.

## AWBs

AWB reservations in `awbs[].emag_id` are read through `awb/read`, checked against the order id and passed to the
shared AWB command. Several parcels or couriers refuse rather than choosing one. No AWB is purchased. The public
schema doesn't name the order-to-AWB reservation field; confirm it against the seller's courier flow before relying
on it. An AWB failure after a successful intake has its own task and never suggests issuing another invoice.

## Returns

RMA statuses 2, 3, 6 and 7 suggest a storno draft as a Sarcină that opens the order sheet (`ord`). No return creates
a credit note on its own: replacement-only and partial returns need a person to choose the invoice lines. Statuses
1, 4 and 5 suggest nothing. A finalized RMA (7) is not proof of a fiscal reversal, so returned or storno orders keep
their review until a full storno or credit note exists; a storno draft hides its return task. A person can mark a
review task resolved (Sandbox or Live); the acknowledgement is bound to the RMA ids or the refused payload, so a new
RMA or a changed refusal shows again. Nothing is sent back to eMAG about returns.

## Invoice PDF attachment

After an invoice is issued in Live, the tick records a 256-bit PDF capability and attaches its URL to the order with
`order/attachments/save` (type 1, order_type 3). The attempt is committed before the request, retried after an hour,
and marked sent on success; a failure creates a task. Pending attachments run before and after the order scan, so a
scan failure can't starve them. A crash after eMAG accepted the request may send the same URL again (at-least-once;
eMAG's duplicate handling is undocumented). Invoices issued just before a crash are recovered on the next tick.

The link expires 24 hours after success; check that eMAG copies the PDF rather than keeping the URL for buyers. A
disabled, removed or reassigned integration can't serve the capability. In Sandbox no attachment is created or sent
and capability reads are refused.

## Not supported

eMAG payout reports. A future adapter needs the net payout schema (commissions, refunds, withholding) and the
shared [settlement](settlements.md) checks. An invoice list is not proof of payout: fiscalm never marks invoices
collected from eMAG's payment metadata.
