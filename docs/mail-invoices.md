# Invoices that arrive by e-mail

Invoices that never reach SPV (foreign suppliers: cloud, software subscriptions, marketplaces, airlines, anything
sent only by e-mail) come into fiscalm from the firm's mailbox. This note says what fiscalm does and why.

fiscalm reads Microsoft mailboxes through Microsoft Graph and other providers over read-only IMAP.
Neither needs a forwarding address or an inbound mail server on the installation. The firm comes from the document or a remembered sender, never from a guess, and rules are remembered
per sender, not written as patterns.

## What fiscalm does

**Source: the mail integration, read-only.** The same `mail` account (Setări, Integrări) that holds
mail reading has two fields: *Citire facturi* (on by default) and *Dosar citit* (default `INBOX`). To read
only some mail, make a filter or label in the mail provider (a `Facturi` folder) and point *Dosar citit* at it;
otherwise leave `INBOX`. Microsoft reads only Inbox, regardless of the IMAP folder setting.
Any number of accounts may read (one per firm, or one shared).

Neither transport moves, deletes, flags, marks as read or sends. The IMAP reader opens the folder with `EXAMINE` (read-only) and
fetches with `BODY.PEEK`, so even `\Seen` is untouched. Each run is bounded: it looks at the last 30 days (a message
not read by then is never read), at most 200 messages, none over 25 MB (from `RFC822.SIZE` before any download, and
again on every literal the server sends), 10 attachments of up to 15 MB each, and an absolute 2-minute budget per
account that every socket read checks; what is left is read on the next tick. A FETCH reply counts only for the UID
and item asked for. A message is remembered as seen by account, folder, UIDVALIDITY and UID, and by Message-ID (a
message copied into two folders is read once), only after its documents are filed, so a crash reads it again; filing
is idempotent by the attachment's SHA-256. A message that can't be fetched, parsed or filed is retried after the new
ones, three times at most.

Microsoft uses the public-client device flow with delegated `Mail.Read`, `User.Read` for account verification,
and `offline_access` for refresh. No password, secret or public callback is required; see
[self-hosting](self-hosting.md) for the install's public client ID. Expired or revoked consent shows
`reconnect_required` in the inbox's reading status. Tenant consent and device-flow policy failures have distinct codes.

The Graph reader fetches MIME through GET only, then uses the same parser, intake callback, attachment limits and
seen tracking. Initial sync starts with the last 30 days; delta tokens then track additions and changes since that
sync. Each run allows 200 MIME messages, 2,000 metadata pages and a two-minute budget checked between requests
(each HTTP request has a 20-second timeout). Old snapshot pages are scanned without fetching MIME. A cursor is
acknowledged only after its page has been handled; retries replay it safely. Missing or malformed messages get
three attempts, oversized mail is skipped, and outages stop the run without consuming attempts. A reset after an
expired delta token starts again from the last 30 days. No mailbox state changes.

**Which mails are invoices.** Only attachments are read, never the body, never a link. An attachment is a candidate
when its bytes are a PDF (`%PDF-`) or a UBL Invoice (XML, parsed with DTDs refused). Mails from the install's
own addresses are skipped; a current account's bank statement (a BT-style bank: its "allmerge" PDFs, imported
together and refused unless they hold every account the firm had at that bank that month) and a firm's
accountant's OPs (PDFs named op…) go to statement and payment-order intake instead. Payment-confirmation mails are
found by an IMAP search of the same folders; Microsoft payment-proof search is not yet supported (#637). No Sent-folder scan or draft write is performed. A PDF counts as an invoice when its text carries a fiscal document title (factură, invoice, receipt,
Rechnung, facture, …) and a total. That list is a fixed rule of the document's content, like the proforma check,
not a setting; the mail's subject plays no part. The fiscal title, or a matching UBL invoice, also distinguishes an invoice from a receipt: invoices appear in Facturi, receipts in Bonuri și chitanțe until linked to an invoice. Card details describe payment and do not change that type.

**Refused, never filed:** proformas, credit notes (or a negative total), documents of a Romanian supplier (a Romanian tax id that is not one
of the firms': its invoice is in SPV, and the e-Factura copy is the legal one), and documents the firm itself issued
(the number of one of its issued invoices, with its own tax id on it).

**Which firm.** The existing ownership rule (`docs.inspect_text`: the firm's name, its aliases, its CUI/VAT id on the
document) says which firms are named; the label nearest each mention (Bill to, Customer, Client, Cumpărător… or
Seller, Supplier, Furnizor…) says where it stands. A firm named only as the seller is never filed (it is the firm's own
sale). Exactly one firm, named as the buyer: that firm. For UBL, the buyer party's full tax id (a foreign `DE…` id is
never a Romanian CUI) and name, which must agree. A sender the user has tied to a firm ("mailurile de la acest
expeditor sunt ale firmei X") fills in a document that names no firm, or names it without a clear role; a document
that names another firm than its sender's is in doubt.

**Fields.** UBL gives supplier, number, dates, total and currency directly. From a PDF: the total on a "total"
line with its currency, the issue date on a dated line, the number after the document's title; the supplier is the
sender's display name. When the administrator configures the AI integration, that consents to document text (or, for a scanned PDF
without text, a picture of its first page) being sent to its provider in Sandbox and Live. It is asked once per PDF
the rules could not settle (no firm, an unclear buyer, two firms, no single total, no text) and proposes the fields
(`fiscalm.ai_extract`). Fixed rules check each one: an ISO date, a positive two-decimal total printed in the text in
a currency the document shows, a known currency, and buyer and seller matched to the firms by CUI or name and printed
on the document. It never overrules the rules: a firm they found must include its buyer, a total they found must
include its total. A confident reading may file the document or refuse it (proforma, credit note, the firm as seller,
a Romanian seller); a low-confidence one only fills the question's number, date and supplier.

**In doubt, one question (Sarcini).** No firm on the document, a firm without a clear buyer's role, two firms, a sender tied to another firm, or a total
that isn't clear: the document waits in Sarcini as one question with the answers to pick (each firm, each total
found, "Nu e factură"), and a choice to remember the sender. Nothing is filed by a guess (AGENTS.md rules 2 and 3).
A remembered sender is stored as that exact address, never as a pattern.

**Where it goes.** Into Cheltuieli, Facturi, as a received invoice "din e-mail" (a document of the firm, with the
mail's sender, subject and date), in the month's accountant pack like every receipt, and matched to card debits by
the existing receipt matching (exact amount and currency within the window, then the merchant's name). It is not put
in a payment file: the payment files are domestic RON transfers and these suppliers are paid by card or by their own
collection.

**Duplicates.** The same attachment (SHA-256) is filed once, whatever mail it came in. The same firm, number and
total, or with no number the same firm, sender, date, total and currency, is a duplicate and is not filed again.

**Sandbox.** Reading the mailbox and filing locally have no outward effect and run in Sandbox too, like
the bank feed. Turning reading on is configuration (an `integration-*` action, allowed before
going live). Answering questions and using the configured AI integration are also available in Sandbox; evidence and financial guards still apply.

## Not done here

- Paying a mailed invoice by bank transfer (a foreign IBAN with no statement proof is never trusted).
- Reading invoices in the body of a mail or behind a link ("download your invoice"): a mail never chooses a URL.
- Gmail/Microsoft OAuth instead of an app password.

Multiple mail accounts can serve the same firms. New accounts default to all firms, including firms added later.
An explicit list serves only the selected firms; administrators can change it in Integrări.
For Microsoft mail, the selected scope is saved before sign-in begins. Reconnecting an existing inbox preserves its scope; edit the integration to change it.

An inbox files invoices, statements and payment orders only for firms within its scope. A document naming another
firm becomes a Sarcină question. Remembered sender ties cannot extend the inbox's scope. Pending answers recheck
the current scope under the ledger write lock. Mail access stays read-only.

A statement or OP refused for scope can be uploaded manually after correcting the scope. Mail already read is not automatically read again.
