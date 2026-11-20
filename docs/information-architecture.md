# Information architecture

What fiscalm is made of, what each thing is called, and how one leads to another. New screens, API resources
and copy follow this document; a change to it comes first, in its own PR. Sections 1 to 4 describe the product model; section 5 lists work still to build.

## 1. Entities

One name per thing, used the same way in the app (Romanian), the API (English) and the code.

| App (RO) | API / code | What it is | Identified by |
|---|---|---|---|
| **Firmă** | `company` | A company whose books this install keeps (SRL, PFA). Financial records belong to exactly one, except mail awaiting placement; installation services, access and mode do not (below). | `key` (ledger) |
| **Partener** (Client, Furnizor) | `partner` | Another company or person the firm trades with. Client and Furnizor are roles, not two entities: one partner can be both. | CUI or VAT id; a name when there is none |
| **Factură primită** | `invoice` (received) | A supplier's invoice: from e-Factura, or a mailed invoice. | e-Factura message id, or document sha |
| **Factură emisă** | `issued` | An invoice the firm issued: drafted and issued in fiscalm, or read back from SPV. | id; series + number once issued |
| **Produs** | `product` | One firm's catalog entry, of kind `produs` or `serviciu`: name, optional code (SKU), unit (buc, oră, lună, kg…), default unit price and currency, VAT rate accepted by the invoicing engine, optional description, and archived flag. | `id`, within one firm |
| **Storno** | `issued` with `reverses` | An issued invoice that reverses (part of) another. Never a separate kind in lists: it shows under the invoice it reverses. | as above |
| **Bon / chitanță** | `document` | A receipt: the photo of a bon fiscal (with its card slip), or a PDF. | sha256 of the file |
| **Plată** | `payment` | An order to pay something (an invoice, a tax, the accountant): a row prepared for the bank file. | `id` (public); `item_id` (ledger) |
| **Fișier de plăți** | `payment_file` | A named batch of payments a person makes for one firm, imported in the bank as one file (in the paying bank's format; BT Go is the first). Its name is proposed as "<firm> plăți <day>" and can be changed while it is open. A firm can have several. **Deschis** until a person says "Am importat în bancă": it still takes payments, and once downloaded it says when it changed and must be downloaded again. **Importat** after that, closed. **Confirmat** when evidence proves every payment in it. An invoice's payment is in at most one file. | `id` |
| **Tranzacție** | `transaction` | One line of an account's movements, from a statement, a partial statement (CSV) or the bank connection: a debit (money out) or a credit (money in). Each one has an **explanation** (section 4) or is **de explicat**. | account + bank reference |
| **Extras** | `statement` | One bank statement of one firm: an account and a month. | firm + account + month |
| **Încasare** | `collection` | Money in for issued invoices: one credit transaction and the one or more issued invoices it covers, with any difference named (the courier's fee, a part still owed). Made by fiscalm (exact rules), from a Decontare, or by the user (section 4). An issued invoice paid in cash or by offsetting is marked, not an Încasare: there is no transaction. | the credit transaction |
| **Comandă** | `order` | An order from a shop (WooCommerce, PrestaShop, Shopify, eMAG or the API): the source of one Factură emisă, with its payment method (card, ramburs, transfer) and its AWB. Not a section and not a list: in lists an order appears only as the invoice it became, first a draft, then the issued invoice. Automatic issue happens only when the shop's rule in Automatizări (Emitere automată) is on and the installation is Live. An order that fails fiscalm's checks never becomes a draft; it raises a Sarcină. A refund after issue is a storno draft on the issued invoice. eMAG order tasks open their own sheet (`ord`). Order numbers, AWBs and buyer details are data from the shop: shown and searched, never used to build a link or choose an action. | firm + channel + the shop's order id |
| **Decontare** | `settlement` | The report a courier or card processor (a partner: FAN Courier, Sameday, Cargus, DPD, GLS, Stripe, Netopia, PayU) sends with a payout: which issued invoices one credit contains and the fee kept back. Evidence for an Încasare, not a second kind of collection. fiscalm settles from it on its own only as a whole: same firm, net equal to the credit to the cent, every line one open invoice of that firm (AGENTS.md non-negotiable 3); a partner's first three wait for a person, and every one from a payer known only by name, without CUI or IBAN. Its column meanings are saved on the partner, never as a separate "payer". | firm + partner + report reference |
| **Raport lunar** / **Dosar pentru contabil** | `report` / `pack` | One firm's month: what came in and went out, what is still missing, and the pack sent to the accountant. | firm + month |
| **Mesaj de repartizat** | `mail` | A mail that may hold a receipt and couldn't be placed on its own. It belongs to no firm until the user places it: every firm's view lists it, and placing it gives it to one. | mail key |
| **Cheltuială** | `expense` | One purchase, with every document that proves it: a received invoice, a bon fiscal or chitanță, or both when they are the same purchase (the invoice's `bon`). Listed once, whichever documents it has; its sheet shows them together with the payment or card transaction. Not stored on its own: fiscalm derives it from the invoice and the receipt. | `<co>:invoice:<id>`, else `<co>:document:<sha>` (variable components URL-escaped); sheet params `inv` / `doc` keep the raw invoice / document ids |
| **Sarcină** | `task` | Something that waits for the user's decision: a payment that fits several invoices, a mail to place, an issued invoice SPV rejected, a month ready for the accountant, an order a shop sent that fiscalm refused, a ramburs delivered but not paid after the usual days (opens `iss`), a returned parcel, a storno to decide (opens `iss`), a payment file to download, to confirm as imported or with a question about it (opens `pf`), a settlement that settles nothing or has no credit in a statement yet (opens `set`), automatic payments waiting for last month's statement (uploads it; an invoice a person adds never waits). Not stored on its own: fiscalm derives it from the entity it is about (it opens that entity's sheet), and it disappears when that entity no longer needs a decision. It belongs to its entity's firm; a mail to place belongs to none, and an install-wide problem (an integration that fails for every firm) to none either. | kind + the entity's id (firm-qualified) |
| **Regulă** (Automatizare) | `rule` | Something fiscalm does on its own, through the same guarded engine actions a user would trigger, never a new path. A rule may prepare locally (a draft, a payment in a payment file through the same checks); an outward effect (an upload to SPV) only in Live. A rule never pays, never sends mail, never marks anything paid or settled, never resolves an ambiguous match, never confirms a bank import, never regenerates a missing file: those stay decisions, and a refusal becomes a Sarcină. The rules (`rules.py`): upload issued invoices to SPV, prepare the accountant's pack, place receipts from an upload link (Bondrop), add chosen suppliers' invoices to the newest open payment file, or a new one (RON, no issues), and client payment reminders (text to copy). Recurring invoices (`recurrences`) issue an invoice each month through `issue()`; one it refuses stays a draft with a Sarcină saying why. | id |

Settings are not entities the user works with daily; they live under **Setări**, a page with its own menu
(`sec` in the URL):

| Page | `sec` | What |
|---|---|---|
| **Mod** | `mod` | Sandbox or Live, for administrators. The switch asks first, naming what changes, and confirms identity when the session's last confirmation is older than ten minutes. |
| **Cont** | `cont` | The signed-in account's password, authenticator, passkeys (Chei de acces (passkey)), recovery codes and sessions, and a count of the person's API keys linking to Linkuri și chei API. Inside the app frame; before the first firm and during setup it stands alone, with the API keys themselves. |
| **Firme** | `firme` | Firms (added by CUI), their other names, the ANAF connection and the inboxes that read each one (as the engine decides; each opens its Integrări sheet), and their **Bănci și conturi**. Everything about a firm's banks and accounts is there and nowhere else. Each bank is a card: its current accounts (the one payment files debit marked **Plățile pleacă de aici**), then **Cum ajung tranzacțiile**, its statement sources as rows, each with its state as a badge and its next step: **Conexiune directă** (Enable Banking: Conectează / Reconectează, Oprește citirea directă), **Extrase prin e-mail** (the firm's inboxes that read the bank's statement mail, each opening its sheet, or Alege un cont de e-mail) and **Încărcare manuală** (Încarcă extrasul, in Tranzacții). Every available source works at once; the first that works is marked **Principală** (`bank.connections.bank_sources`). A bank with password-protected PDF statements keeps the statement password in its e-mail row, write-only: its state, and Schimbă opening the field with Anulează and Salvează. Under the banks: **Adaugă un cont** (IBAN and currency; the bank is read from the IBAN; never the paying account) and **Conectează o bancă** (a catalogue bank whose accounts are chosen at the bank). While the firm has no lei account, **Contul din care pleacă plățile** asks for it; once set, the engine changes it only from the command line. Balances and statements are in Tranzacții. |
| **Contabil** | `contabil` | Per firm, the accountant (a partner with a CUI) and the addresses the pack is meant for. |
| **Facturare** | `facturare` | Per firm, its invoice series: code, next number and issued count, the default (new invoices start in it), Adaugă serie, Setează ca implicită, and Șterge seria only while no issued invoice, draft or recurring invoice uses it (else the engine's reason). A series' numbers never go back. |
| **Integrări** | `integrari` | Installation connections, any number of each kind: ANAF, AI service, mail, Google Drive archive, Enable Banking (open banking: the install's application, used by every firm's bank connection), shops (WooCommerce, PrestaShop, Shopify, eMAG). Only shops belong to one firm, named in their `company` field. AI, ANAF and mail setup appears here only, never on a firm's checklist. No firm's bank settings. Secrets are write-only. Each opens its sheet (`int`). An ANAF connection or an inbox serves Toate firmele (later ones too) or the firms chosen in its Firme field, asked on adding and editable later; the choice is hidden with one firm, and each row names its firms. The ANAF section warns about a firm two connections cover (the engine reads it with one) or none covers. |
| **Utilizatori** | `utilizatori` | Accounts and installation-wide roles: Administrator, Membru, Doar citire. Administrators only; each opens its sheet (`usr`). |
| **Linkuri și chei API** | `acces` | API keys (for other apps and MCP clients) and Bondrop receipt upload links: created, listed without the secret, revoked. |

The Setări overview lists these pages first; a connection that needs a person (ANAF, mail reading) shows on the
Integrări row. It ends with **Sistem**: the version and, for administrators only, the release check ("Verifică versiunile noi"),
the last check with "Verifică acum", backups and the check log, collapsed. Signing out is in Cont.
A firm's first invoice series is written in the invoice form and created with its first save; later ones are added in
Setări › Facturare or from the form's Serie nouă….

## 2. Relations

How the entities connect. A sheet links to the related entities it shows; not every relation below is shown on
both ends yet (section 5).

```
Partener ─┬─< Factură primită >─── Plată >─── Fișier de plăți
          │          │                 │
          │          ├── Bon            └──── Tranzacție (debit) >── Extras
          │          └── Storno primit
          └─< Factură emisă ─┬── Storno (emis)
                             ├── Comandă (magazin, AWB)
                             ├── Tranzacție (credit = încasare) >── Extras
                             │       └── or Decontare (raport curier / procesator) ── Tranzacție (credit)
                             └── SPV: index, stare, recipisă
Raport lunar ── every Factură, Bon, Extras, Tranzacție of that firm and month, and the Dosar
```

| From | Leads to |
|---|---|
| Factură primită | its Partener; its Plată (and the Fișier de plăți it is in); the Tranzacție that paid it, or the Bon; Storno-uri primite; the Raport of its month |
| Factură emisă | its Partener; its Comandă (shop, order number, AWB), if it came from a shop; the Tranzacție that collected it, the Decontare line that did, or the manual mark; the Storno that reverses it, or the invoice it reverses; its SPV state; each line's Produs, when linked; PDF and XML; the Raport of its month |
| Produs | its firm; the Factură emisă lines that reference it |
| Partener | its invoices (primite, emise), what is owed either way, the bank accounts it was paid from or to, its CUI data from ANAF; for a courier or processor, what it holds for the firm and its Decontări; the associations the user made for it (section 4) |
| Plată | what it pays (Factură, Taxă, contabil); its Fișier de plăți; the Tranzacție that confirmed it |
| Fișier de plăți | its payments, each with what it pays and its proof; its downloads |
| Tranzacție | what it settled (Factură, Plată, Factură emisă), or the candidates when it fits several ("Plată de lămurit"); for a credit explained by a Decontare, every invoice it collected and the fee; its Extras |
| Decontare | its Tranzacție (credit); each line's Factură emisă and AWB; the fee |
| Bon | the Factură primită of the same purchase (if any); the card Tranzacție |
| Raport lunar | every entity of the month, and what is missing |

Rules:
- **One way to name a thing.** A partner is always shown by its name (CUI under it), an invoice by partner + number
  + amount, a transaction by date + counterparty + amount. Never an internal id.
- **Every count is a door.** "3 restante", "2 de lămurit", "4 facturi" open the filtered list they count.
- **Nothing is a dead end.** Every sheet has at least one way forward (a related entity or an action) and one way
  back (the browser's back, the close button).
- **A firm is always in the URL.** Under "Toate firmele" every row carries its firm's name, and opening it switches
  nothing: the sheet acts for the row's own firm.

## 3. Navigation

### Account access

The shell is public; financial pages require a signed-in account. Sign-in is a passkey (cheie de acces) in a compatible browser over HTTPS,
offered first where it works unless the device last signed in with the password, or a username and password followed by an authenticator (TOTP) code.
**Nu mai am acces** takes the username and a recovery code; the engine then asks a password account for its password
and a new authenticator, and signs it in after the new recovery codes, while a passkey-only account replaces its key
and signs in with the new one. The first
account is set up from a one-time link, with a locally generated QR code or a passkey, and its recovery codes are
acknowledged as saved. A new installation then asks for one thing, the first firm (its CUI; ANAF fills in the rest),
and opens Acasă. It starts in Sandbox without asking; Live is chosen later in Setări › Mod. Installation services (ANAF,
mail and AI) are configured once in Setări › Integrări, including when no connection exists yet.
The firm's bank, earlier invoices and shops wait on the **Configurează** card on Acasă. A second firm is added in
Setări › Firme.

**Setări › Cont** is available to every role. **Utilizatori** is administrator-only. Roles apply to every firm.
Search, navigation and actions follow the session's capabilities; read-only accounts keep records and document
downloads while actions that change money or records are hidden or disabled. Sensitive actions open **Confirmă
identitatea** for the same account; cancelling never performs the pending action. A session change in another tab
rechecks identity and clears the previous owner's forms.

### Desktop (1100px and wider): the left column

```
[Firma ˅]                      company switcher; Toate firmele is one of its entries
[Caută            ⌘K]          searches partners, invoices (number, amount), receipts, sections
[+ Nou]                        Încarcă bon sau factură, Factură nouă, Factură de stornare, Bondrop
Acasă
Sarcini              9         every open task
Bani
  Vânzări                       issued invoices and drafts
  Cheltuieli         4 restante  received invoices, bonuri and chitanțe
  Fișiere de plăți
  Tranzacții         4         the count is De explicat (one firm only)
Firma
  Parteneri
  Produse
  Rapoarte
  Automatizări
──────────────
Setări                         a dot when a new release exists; under it, once a check has run,
  Verificat 27 oct, 09:10      the last check
```

The column is navigation only: no amounts. Amounts live on Acasă and on each section.

### Phone: the tab bar

A floating nav over the page, above the safe area: a round **Caută** on the left, a capsule with the daily work as
icons (**Acasă**, **Sarcini**, **Cheltuieli**, **Tranzacții**, each named for screen readers), and a round **+** on the
right. Sarcini and Tranzacții carry their counts as on the desktop column (every open task; De explicat, one firm only),
read in their names. The four destinations are links with `aria-current`; **+** is a button named "Nou" that opens the
same menu as "+ Nou" in a sheet (`aria-expanded`, focus moves into it, Escape and closing return focus to the button);
it is never the current page. The page scrolls under the nav and keeps its last row clear of it.

Under Toate firmele, Tranzacții shows no count and opens the last single firm chosen, as the sections below do.

The top bar at the head of every page holds the company switcher, **Mai mult** and the Setări gear. Mai mult
is a link to its own page (`tab=mai-mult`), listing Vânzări, Fișiere de plăți, Parteneri, Produse, Rapoarte and Automatizări, so every
section is at most two taps from the top of any page. It stays current on its page and on the sections it lists; the
gear is current on Setări, and Tranzacții only in the tab bar. Setări is reached only from the gear.

### The sections

| Section | Holds |
|---|---|
| **Acasă** | Titled Acasă, with the date under it, on a phone and on a desktop. The firm's dashboard: the quick actions and at most three tasks on top, then încasat, cheltuit, rămas and the accounts' balance for the chosen period (Luna curentă, 3 luni, Anul), the last six months, de încasat and de plătit with their overdue parts, what couriers and processors hold, the largest suppliers and clients, and the next 30 days from due dates. Every number links to the list it counts. Under Toate firmele, the same sums for every firm (Pe firme), one line per firm where a total would mix currencies; the quick actions wait for a firm to be chosen. For an administrator, the **Configurează** card under the quick actions lists what is not yet set up for the chosen firm: Banca, Facturi anterioare, Magazine. Each line says what it gives and links to its Setări sheet, or for Banca and Facturi anterioare to that part of the firm alone; it disappears once that item is done. The card goes away with the last line, or when hidden for that firm; Setări › Firme shows it again. |
| **Sarcini** | Every task in one paged list, not grouped, with the firm on each row when there are several. Each row opens or acts on the task's entity for the row's own firm. Client payment reminders appear here with their text to copy. |
| **Vânzări** | Issued invoices and drafts: filters (Toate, De încasat, Restante), search, month, SPV state. Factură nouă and storno start here; shop orders appear as the invoices they became. |
| **Cheltuieli** | Every Cheltuială, in two categories (header button: Încarcă bon sau factură, photos or PDFs from this device; one opens its sheet, several open Bonuri și chitanțe): **Facturi** (e-Factura and mailed invoices) and **Bonuri și chitanțe**. A purchase with both an invoice and a bon is one row in both categories, marked "Factură și bon", with one sheet: the invoice's, with the bon on it. Filters: Toate, De plătit, Restante; for bonuri, Toate and Fără factură. Search and month. In De plătit and Restante of one firm each invoice the engine lets a person act on has a box; choosing shows one bar (count and total, Marchează ca plătită, Pune în fișier). **Pune în fișier** asks which file: the newest open file (the default), another open file, or **Fișier nou** with the proposed name; with no open file, only the new file's name. An invoice's sheet offers the same. An invoice already in a file names it and links to it; one marked can't be chosen; an approved payment without a file can be put in one. The engine decides which invoices are eligible. |
| **Fișiere de plăți** (`tab=plati`) | The firm's payment files only, newest first: name, created date, payments, total and state (Deschis; Deschis, modificat după descărcare; Importat, 1 din 2 confirmate; Confirmat). No invoices and no adding here: payments are chosen in Cheltuieli. A file opens its sheet (`pf`): its payments, each with its proof; while open, Descarcă fișierul (Descarcă din nou after a download), Am importat în bancă once the current version is downloaded, removing a payment, Redenumește, and Șterge fișierul when it is empty; the engine's questions about it (a file missing from its folder, several downloads, an unknown file beside it). Confirmed payments are also in Tranzacții. |
| **Tranzacții** | Every transaction of the firm's accounts, newest first, with what explains it: **De explicat** first, then Toate, Intrări, Ieșiri, and search. Beside the title, **Încarcă**: *Încarcă extras* (the account is asked only for a CSV, when the firm has several) or *Încarcă raport de decontare*. Beside the list (below it on a phone): the accounts, one line each with its balance and last read, a bank connection ending or ended with **Reconectează banca**, and for an administrator the way to Bănci și conturi, where accounts are added; the statements by month; what couriers and processors hold. |
| **Parteneri** | One list of clients and suppliers (filters Toți, Cu sold, Cu restanțe). Header button: Partener nou, a firm added by its CUI as a supplier or a client, ANAF filling in the rest. A partner's sheet shows what is owed each way with Factură nouă and Modifică, every invoice, what it holds for the firm if it is a courier or processor, then its details, closed once it has invoices (CUI or VAT id, address, known IBANs, ANAF state on one line), then what is linked to it. |
| **Rapoarte** | One report per month per firm and the accountant's pack (anchors `#rezolvat` and `#pachet`). A month's received invoices list purchases as Cheltuieli does. |
| **Automatizări** | The rules of section 1, grouped Facturare (SPV upload, with the day after issue it sends, chosen only while it is on; each shop's Emitere automată; then each recurring invoice with Oprește / Pornește / Șterge; an invoice's sheet has Repetă lunar), Plăți (one row per supplier whose invoices go into the payment file) and Documente (the accountant's pack, each Bondrop link), each with its state, what waits on it, fiscalm's reason when it is held, and Oprește / Pornește where allowed (`can_pause`, `can_resume`). Remembered associations are listed here too. Under Toate firmele, the same groups per firm. |
| **Setări** | The overview and the pages of section 1. |

Toate firmele shows Acasă, Sarcini, Vânzări, Cheltuieli, Parteneri and Automatizări for every firm at once. The
other sections need one firm: opened under Toate firmele, they show the last single firm chosen and put it in the URL
(`co=<firm>`), so a link always names the firm it shows.

### URL

Everything the screen shows is in the query, so a link, a reload and the back button return to the same place
(`app/src/lib/nav.ts`):

```
?co=<firm|ALL>&tab=<section>&sec=<page>&<sheet>=<id>&side=&f=&month=&per=&from=&to=&for=
tab:   home (default), sarcini, vanzari, cheltuieli, plati, tranzactii, parteneri, produse, rapoarte, automatizari, setari, mai-mult
sec:   setari only: "" (overview), mod, cont, firme, contabil, facturare, integrari, utilizatori, acces (Linkuri și chei API)
side:  cheltuieli only: "" (Facturi) or bonuri (Bonuri și chitanțe)
f:     cheltuieli, vanzari, parteneri: "", open, late; tranzactii: "" (De explicat), all, in (Intrări), out (Ieșiri)
month: rapoarte: the open report (YYYY-MM); tranzactii: the month listed
per:   home only: "" (Luna curentă), 3luni, an
from, to: tranzactii only: the days a number on Acasă counts (ISO dates, both included)
sheets: inv (factură primită, or the cheltuială it belongs to), iss (factură emisă), doc (bon), mail, p (partener),
        tx (tranzacție), pf (fișier de plăți), set (decontare: firm-qualified id), ord (eMAG order task: firm-qualified id),
        new (factură nouă: "1" or a draft id), for (with new=1: the partner to invoice),
        int (setari: an integration's id, or "new:<kind>"), usr (setari › utilizatori: an account)
under Toate firmele, a sheet opened from a row or from another sheet carries that row's firm
```

One sheet at a time. Opening a related entity from a sheet pushes a history entry, so back returns to the previous
sheet, then to the list. Closing a sheet returns to the list it was opened from.

### API resources (v1)

The HTTP routes, fields and relation ids are defined in [openapi.json](openapi.json). Settings and integrations are
app-only; not every entity above has a public resource route.

### Firmă › Produse

Each firm keeps its products and services in **Produse**, under **Firma** on desktop and in the firm
menu (Mai mult) on phone, at `tab=produse`; a product opens with `prod=<id>`.
The catalog needs one firm and follows the last-single-firm fallback under Toate firmele.
A product can be archived and restored. Deletion is allowed only while no draft or issued invoice line references it;
a referenced product can only be archived. Archived entries stay on existing lines and in sales history,
but cannot be chosen for a new line.

Choosing a product on a Factură emisă draft fills the line's name, unit, unit price and VAT defaults.
The price prefills only when the product and draft currencies match; otherwise a price must be entered.
The line keeps its own values and may be edited independently. Codes are optional and need not be unique. A line may reference only a product of
that invoice's firm, or have no product. Editing or archiving a product never changes an issued invoice.
Acasă groups issued sales by product, net of storno, with quantity and invoice count for the selected
month or twelve months. Lines without a product appear as **Fără produs**. Revenue stays per currency;
quantities with different units are not added together.

## 4. Money: what moved, and what explains it

fiscalm keeps two lists that meet: **documents** say what is owed (issued and received
invoices, receipts, taxes), **transactions** say what moved in the firm's accounts. Reconciling is making the links
between them. The transaction list and sheets show those explanations.

### Every transaction has an explanation

| Explanation | For | Made by |
|---|---|---|
| **Încasare** | a credit: the issued invoices it covers, the difference named | fiscalm (exact rules), a Decontare, or the user |
| **Plată** | a debit: the payment it confirms, and so the received invoice or tax it pays | fiscalm (the payment's IBAN, amount and reference), or the user |
| **Bon** | a card debit: the receipt that proves it | fiscalm (the receipt's amount and day) |
| **Transfer între conturi** | money between two of the firm's own accounts | fiscalm (both ends seen), or the user |
| **Avans de trezorerie** | the owner's money in or out | the user, or an association the user made |
| **Fără document** | a bank fee, interest, a personal card spend: a label the user picks, kept for the accountant | the user, or an association the user made |

A transaction without one is **de explicat**. That list is the work: Acasă counts it and Tranzacții
opens on it. fiscalm proposes an explanation when it has one and never applies on its own a proposal that could be
wrong (AGENTS.md non-negotiables 2 and 3); the user confirms it or picks something else.

### The user explains what fiscalm can't

On a transaction's sheet (`tx`): **De la cine** (**Către cine** for a debit; a partner) and **Ce acoperă** (one or more of
that partner's open invoices, or a prepared payment), or a label (Fără document, Avans de trezorerie, Transfer între
conturi). A debit can also be a **Bon / chitanță** (uploaded there; its own matcher links it) or wait for its invoice
(**Aștept factura**, a note that reserves nothing). The invoice the bank details name at its exact amount is proposed
first, and a line the bank marks as its fee starts on Fără document, Comision bancar; neither is applied alone. The amounts don't have to agree: fiscalm shows the difference and the
user names it (Comision curier sau procesator, Rest de încasat, Comision bancar, Diferență de curs). A part still owed keeps the invoice open for
that part. One transaction still explains one set of documents: a second explanation replaces the first, never adds
to it. Every link the user makes can be undone from the same sheet, and the partner's sheet lists it.

From a link the user makes, fiscalm can offer to remember it: "transfers from this IBAN are Client Exemplu SRL",
"this card is personal" (Card personal). These are associations the user owns and sees on the partner's sheet, the
transaction's sheet and in Automatizări, removed with `memory-forget`; never regexes or lists in a config file. The
explanations are the `tx-link` and `tx-unlink` actions ([openapi.json](openapi.json)).

### Couriers and processors hold money for the firm

A courier collecting cash on delivery or a processor collecting card payments is a partner that holds the firm's
money until it pays out. fiscalm shows it as **La curieri și procesatori**: per partner, the issued invoices it
collected for the firm (delivered, paid by card) and has not paid out yet. The payout is one credit, minus the
partner's fee. Its Decontare says which invoices it contains; without one, the user links the credit to the invoices
on its sheet. The partner's balance goes back to zero once every collected invoice is paid out; what stays is a
parcel delivered but not paid, a refund or a fee to look at.

### Acasă, Tranzacții, Sarcini, Fișiere de plăți

- **Acasă** answers "how is the firm doing": the numbers in section 3 and at most three tasks.
- **Tranzacții** answers "what moved, and is everything explained": the list above, per account or all of them.
- **Sarcini** keeps every other decision in one list; transactions de explicat are counted on Acasă and explained in
  Tranzacții.
- **Fișiere de plăți** keeps the bank files the person made: what each holds, which to download and import, and what each imported file paid; money that already moved is checked in Tranzacții.

### Documents for a person to send

The accountant's pack is one ZIP per firm and month: fiscalm freezes its document set, a person downloads it
(**Descarcă dosarul**) and confirms it was sent (**Marchează ca trimis**). Invoice sheets offer PDF and XML downloads.
Reminder tasks show their subject and text with **Copiază textul**. There is no draft and no send action.

## 5. Planned

Not built yet:

- **Produse**: the catalog, invoice-line links and per-product sales described above.

- **Sarcini**: groups (Urgente, Săptămâna aceasta, Fără termen), filters by domain (Plăți, Facturi, Tranzacții,
  Contabil), transactions de explicat in the list, and a history of decided tasks once decisions are recorded as
  events.
- **Relations on both ends**: every relation shown in both entities' sheets, as rows naming the other entity the
  way its own list does.
- **Vânzări**: a filter by origin (Manual or each shop) and a state for ramburs invoices the courier holds; an
  order sheet (`ord`) for every shop, with a way to discard an order fiscalm refused.
- **Tranzacții**: linking a receipt to a card debit by hand.
- **Setări**: a log of what fiscalm did, newest first.
- **Automatizări**: rules written as "Dacă … atunci … dar nu dacă …", where fiscalm fixes the "dar nu" guards
  (an unusual amount) and sends the item to Sarcini instead.
