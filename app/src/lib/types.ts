// The engine API shapes, as in docs/openapi.json. Amounts are strings with two decimals.

export type UserRole = "admin" | "member" | "readonly";
export type Capabilities = Record<"read" | "member" | "write" | "manage_users" | "manage_firms" | "manage_tokens" | "own_tokens" | "manage_rules" | "manage_integrations" | "connect_integrations", boolean>;
export type AccountSession = { id: string; method: string; created_at: string; last_seen_at: string; device: string; ip: string | null };
export type AccountPasskey = { id: string; name: string; backup_eligible: boolean; backed_up: boolean; created_at: string; last_used_at: string | null };
export type Identity = {
  me: { id: string; username: string; display_name: string; role: UserRole; can: Capabilities };
  auth: { via: "cookie"; method: "password_totp" | "passkey" | "recovery" | "recovery_code"; recovery_only: boolean };
  factors: { password_totp: boolean; passkeys_available: boolean; passkeys: AccountPasskey[] };
  recovery_codes_remaining: number;
  sessions: (AccountSession & { current: boolean })[];
};

/** Global account metadata; never credentials. */
export type AccountUser = { id: string; username: string; display_name: string; role: UserRole;
  disabled_at: string | null; locked_until: string | null; password_totp: boolean; passkeys: number; last_signin: string | null };

/** A company's key, from the engine's config (upper case letters, digits, - and _). */
export type Co = string;
/** A company, or all of them ("ALL", on Acasă and Facturi only). */
export type CoView = Co;

export type DetailKind = "invoice" | "issued" | "document" | "mail" | "settlement" | "order" | "transaction";
export type CoveredRow = { kind: "invoice"; id: string; number: string; currency: string; amount: string; left: string; closed: boolean }
  | { kind: "payment"; id: string; payment: PaidTo };
export type TransactionInvoiceChoice = { id: string; number: string; date: string | null; left: string; total: string;
  currency: string; partner: string | null; selected: boolean; position: number };
export type TransactionChoiceRows = { partners: Partner; invoices: TransactionInvoiceChoice; selected: TransactionInvoiceChoice;
  proposed: TransactionInvoiceChoice;
  payments: { id: string; payment: Payment } };
/** What can be paged on its own; `proposed` comes only with the first bundle. */
export type TransactionChoiceCollection = Exclude<keyof TransactionChoiceRows, "proposed">;
export type TransactionChoices = { pages: { [K in keyof TransactionChoiceRows]?: Page<TransactionChoiceRows[K]> } };
export type TransactionChoiceQuery = { id: string; collection?: TransactionChoiceCollection; partner?: string; q?: string; cursor?: string; limit?: string };
export type StateDetail = { state: State; related: Page<NonNullable<Invoice["credits"]>[number] | Issued | Settlement["lines"][number] | CoveredRow>;
  transaction?: { said: TransactionSaid | null; parts_count: number; payments_count: number; choices: TransactionChoices["pages"] };
  lines?: InvoiceLines };
/** An invoice's lines as written: decimal strings, a unit label or code, display only. `vat` and `gross` are the engine's
 * net × rate, rounded to the cent. */
export type SheetLine = { position: number; description: string; quantity: string; unit: string; unit_price: string | null;
  net: string; vat_rate: string | null; vat_category: string; vat: string | null; gross: string | null };
/** The document's own totals, as written on it; one tax per VAT subtotal. */
export type LineTotals = { net: string; vat: string; total: string; payable: string;
  taxes: { category: string; rate: string | null; net: string; vat: string }[] };
export type InvoiceLines = { lines: SheetLine[]; currency: string; truncated: boolean; unavailable: boolean; totals: LineTotals | null };
export const PARTNER_COLLECTIONS = ["invoices", "issued", "transactions", "held", "settlements", "remembered", "report_formats"] as const;
export type PartnerCollection = typeof PARTNER_COLLECTIONS[number];
export type RememberedRow = { id: string; memory: Remembered; text: string };
export type PartnerRows = { invoices: Invoice | HistoryInvoice; issued: Issued | HistoryInvoice; transactions: TransactionBrowseRow;
  held: NonNullable<Partner["held"]>[number] & { number: string | null }; settlements: Settlement;
  remembered: RememberedRow; report_formats: NonNullable<Partner["report_formats"]>[number] };
export type PartnerPages = { state: State; pages: { [K in PartnerCollection]?: Page<PartnerRows[K]> } };
export type PartnerQuery = { id?: string; collection?: PartnerCollection; cursor?: string };

/** How a person paid a received invoice they mark paid: the firm's account, a firm card, cash, their own money, another way. */
export type PaidMethod = "bank" | "card" | "cash" | "own" | "other";
/** How a client paid an issued invoice someone marked: the received side's words, without "own". */
export type CollectedMethod = Exclude<PaidMethod, "own">;

export type Invoice = {
  credits?: { id: string; number: string; amount: string }[];
  credit_note?: boolean;
  id: string;
  company: Co;
  supplier: string;
  supplier_cui: string | null;
  /** The partner's id (`partners[].id`), null when the invoice names no supplier. */
  partner: string | null;
  number: string;
  issue_date: string | null;
  due_date: string | null;
  currency: string | null;
  total: string;
  /** marked: paid by the user's tap, awaiting a statement or a mail (can be undone); paid: confirmed; skipped: not to be paid. */
  status: "open" | "linked" | "marked" | "paid" | "skipped";
  payment_state: "held" | "in_file" | "lost" | "imported" | null;
  /** The payment file its payment is in, if any. */
  payment_file?: { id: number; name: string } | null;
  item_id: string | null;
  paid_at: string | null;
  /** How the person who marked it said they paid (`mark-paid`), null when unsaid: a fact, never proof. */
  paid_method: PaidMethod | null;
  /** card: its last four digits; own: who paid; other: the person's note. */
  paid_detail: string | null;
  /** Past due and not paid by the user yet (the engine's rule, the same as `late`). */
  overdue: boolean;
  previous_iban_last4: string[];
  /** The invoice lists several IBANs and no evidence decides which one is paid: a person picks one (by `index`).
   * `ending`: the IBAN's last characters, as many as tell the accounts apart (4 or more), never the whole IBAN. */
  accounts: { index: number; bank: string; ending: string }[];
  can_approve: boolean;
  can_mark_paid: boolean;
  can_withdraw: boolean;
  can_reopen: boolean;
  /** Paid, and a statement debit, a payment mail or the till's bon fiscal proves it (the engine decides). */
  proved: boolean;
  /** Paid at the till: the id of the bon fiscal (in `documents`) that proves it. */
  bon: string | null;
  /** What debits the user linked to it settle ("0.00" when none), and what is still to pay (the engine's `left`). */
  covered: string;
  left: string;
};

/** A receipt's file, served by the engine's docfile: an uploaded photo or PDF, or a mailed PDF. */
export type DocFile = { id: string; type: "image" | "pdf" };

export type ReceiptLine = { name: string; unit: string; quantity: string; net: string; vat: string };
export type ReceiptItems = { lines: ReceiptLine[]; total: string; currency: string; payment: string; provenance: string;
  revision: number; verified: boolean; proposal: string | null; state: string; payment_mismatch: boolean };

/** A receipt: an invoice from mail, or an uploaded photo or file. */
export type CardInvoice = {
  document_type: "invoice" | "receipt";
  can_correct?: boolean;
  number?: string | null;
  partner?: string | null;
  items?: ReceiptItems | null;
  id: string;
  company: Co;
  vendor: string;
  date: string | null;
  month: string;
  /** Null until the document is read: unknown, never zero (`docAmount`). */
  amount: string | null;
  currency: string;
  note: string;
  sent: boolean;
  /** An upload no card payment names yet: it stays out of the accountant's pack until it is uploaded from that payment. */
  held: boolean;
  /** The card's last 4 digits, when known. */
  card: string | null;
  /** What the engine can show, bon first; [] when the file is not on the server. */
  files: DocFile[];
  /** A bon linked to the shop's e-Factura: that invoice's id. */
  invoice: string | null;
  /** Filed from a mail (fiscalm.mail.invoices): the mail it came in; null for photos and uploads. */
  mail: { sender: string; subject: string; date: string | null; number: string | null } | null;
};

/** One purchase from stored evidence; at least one reference is present. */
export type ExpenseSource = "efactura" | "mail" | "upload_link" | "upload" | "history";
export type Expense = {
  sources: ExpenseSource[];
  id: string;
  company: Co;
  invoice: string | null;
  document: string | null;
};

export type ReminderTask = {
  kind: "client_reminder"; id: string; label: string; subject: string; body: string;
};

export type PartnerTask = {
  kind: "inactive_supplier" | "inactive_client"; id: string; company: Co; label: string; detail: string; checked_at: string;
};

/** An invoice from before fiscalm (history import), listed beside the tracked ones with no payment state (contract: History). */
export type HistoryInvoice = {
  sources: ["history"];
  id: string; kind: "history"; company: Co; direction: "issued" | "received"; number: string; party_name: string;
  party_cui: string | null; issue_date: string; currency: string; total: string; credit_note: boolean; document: string | null;
  partner: string | null;
};

export type Issued = SalesOrderFields & {
  id: string;
  company: Co;
  number: string;
  client: string;
  client_cui: string | null;
  partner: string | null;
  issue_date: string | null;
  due_date: string | null;
  currency: string;
  total: string;
  credited: string;
  /** What credits the user linked to it settle ("0.00" when none); `owed` is net of it, `left` what a credit can still cover. */
  covered: string;
  owed: string;
  left: string;
  status: "open" | "collected" | "marked" | "credited";
  days_overdue: number;
  collected_at: string | null;
  /** The booking day of the statement credit that collected it; `collected_at` is when that was recorded. */
  collected_on: string | null;
  how: "statement" | "manual" | null;
  /** For a person's mark: how they said the client paid. A fact, never proof. */
  collected_method: CollectedMethod | null;
  /** For a mark collected another way (other): the person's note; else "". */
  note: string;
  can_mark: boolean;
  can_unmark: boolean;
  /** Issued in fiscalm, or read from SPV (issued elsewhere). */
  origin?: "fiscalm" | "spv";
  /** The id fiscalm's invoicing knows it by (its files, storno, upload); null for an invoice read from SPV. */
  local_id?: string | null;
  /** Refused by SPV: not a receivable (owed is zero) until it is sent again and accepted. */
  rejected?: boolean;
  /** Where an invoice issued in fiscalm stands in SPV (one read from SPV is always accepted). */
  spv?: { environment?: "prod" | "test"; state: SpvState; errors: string[]; at: string | null;
    /** Not yet sent: the day the scheduled run sends it on its own; null when only a person can send it. */
    upload_on?: string | null };
  can_storno?: boolean;
  can_upload?: boolean;
  pdf?: boolean;
  storno_of?: string | null;
  reversed_by?: string[];
  /** The engine would accept resolve-upload now: the outcome is unknown, or the upload has been in flight too long. */
  can_resolve: boolean;
};

/** `not_required`: the engine doesn't send it to SPV (a foreign client declared neither established nor VAT-registered in Romania). */
export type SpvState = "not_required" | "none" | "uploading" | "uploaded" | "accepted" | "rejected" | "unknown" | "error";

/** Someone the company trades with: a supplier, a client or both, by CUI (or by name when there is none). */
export type Partner = {
  held?: { invoice: string; amount: string; currency: string }[];
  report_formats?: { signature: string; source: string; mapping: Record<string, string>; courier?: string }[];
  id: string;
  company: Co;
  name: string;
  cui: string | null;
  vat_id: string | null;
  address: { street: string; city: string; county: string; postal_code: string; country: string } | null;
  country: string | null;
  iban_last4: string[];
  /** Whether the supplier's invoices go into this firm's payment file by themselves; null when the firm has no e-Factura
   * invoice from it. `can_set_autopay`: an administrator may change it (rule-supplier-autopay). */
  autopay?: boolean | null;
  can_set_autopay?: boolean;
  /** Its documents stay out of the accountant's pack; null when the firm has no document from it. `can_set_not_mailed`:
   * an administrator may change it (rule-partner-not-mailed). */
  not_mailed?: boolean | null;
  can_set_not_mailed?: boolean;
  anaf_status: {
    checked_at: string;
    vat_payer: boolean | null;
    vat_since: string | null;
    efactura_registered: boolean | null;
    inactive: boolean | null;
    inactive_register_removed_at: string | null;
    registration_status: string;
    vat_on_collection: boolean | null;
  } | null;
  anaf_error: "unavailable" | "not_found" | "invalid_response" | null;
  /** A Romanian firm the engine looks up at ANAF; false for private persons, foreign partners and invalid ids. */
  anaf_eligible: boolean;
  ro_vat_status: "unknown" | "registered" | "not_registered" | null;
  client_id: string | null;
  roles: ("supplier" | "client" | "courier" | "processor")[];
  /** The newest invoice's date. */
  last: string;
  received: number;
  issued: number;
  /** Overdue invoices either way. */
  late: number;
  payable: { total: string; other: { currency: string; total: string }[] };
  receivable: { total: string; other: { currency: string; total: string }[] };
  turnover: Turnover;
};

export type Payment = {
  item_id: string;
  kind: "op" | "efactura";
  payee: string;
  ref: string | null;
  details: string | null;
  amount: string;
  due: string | null;
  ef_id: string | null;
  paid_at: string | null;
  /** Held only because fiscalm queued it on its own and last month's statement is missing (`statement_hold`). */
  waits_statement: boolean;
};

/** A payment file's state (docs/information-architecture.md): open until a person says it was imported in the bank, then
 * imported, then confirmed once evidence proves every payment it still pays. */
export type PaymentFileStatus = "open" | "imported" | "confirmed";

/** One payment file of a firm, as the list shows it (`files`, newest first). A file is something the person made: a
 * name, the day it was created and its payments. `changed_since_download`: an open file downloaded before its last
 * change, so the bank needs the new download. */
export type PaymentFile = {
  id: number; co: Co; name: string; created_at: string; status: PaymentFileStatus;
  payment_count: number; confirmed_count: number; skipped_count: number; total: string; currency: string;
  changed_since_download: boolean; downloaded_at: string | null; imported_at: string | null;
  /** The engine's open question about the file, answered on its sheet. */
  question: PaymentFileQuestion["kind"] | null;
  /** Where the file is imported, as people know it ("BT Go"); null when the paying bank has no file format. */
  import_in: string | null;
};

/** A payment of one file, where it is. `proved` only when evidence (a statement debit) proves it paid; paid on a
 * person's word it is false. `added_after_download`: it is one of the changes the last download lacks. */
export type FilePayment = {
  id: string; payee: string; kind: "op" | "efactura"; ref: string | null; details: string | null; amount: string;
  currency: string | null; due: string | null; ef_id: string | null;
  state: "in_file" | "imported" | "paid" | "skipped" | "lost"; proved: boolean; paid_at: string | null;
  added_after_download: boolean;
};

/** A question about one file the engine can't answer alone: its download is gone from its folder (`missing`), or a
 * file nobody made lies in its folder (`orphan`). Downloading a file again is never a question. */
export type PaymentFileQuestion =
  | { kind: "missing"; version: string; count: number }
  | { kind: "orphan"; name: string };

/** A file with its payments and downloads (GET payment-files/<id>), and what the person may do with it now: the engine
 * decides from the file's state, the person's role and Live or Sandbox. */
export type PaymentFileDetail = PaymentFile & {
  payments: FilePayment[];
  downloads: { id: number; version: string; at: string }[];
  questions: PaymentFileQuestion[];
  can_add: boolean; can_rename: boolean; can_remove: boolean; can_delete: boolean; can_download: boolean; can_import: boolean;
};

/** The add-to-file chooser's start: the name a new file gets, and the newest open file (null with none). */
export type PaymentFileDefaults = { co: Co; name: string; open_file_id: number | null };

export type Money = { total: string; other: { currency: string; total: string }[]; due_soon: number; late: number;
  /** The overdue part of `total` (lei) and `other`, by the rule `late` counts. */
  late_total: string; late_other: { currency: string; total: string }[] };

/** Amounts per currency, never mixed: lei in `total`, the others in `other` (docs/openapi.json, Acasă). */
export type Sums = { total: string; other: { currency: string; total: string }[] };

/** What was invoiced over time, never a balance: issued (sales) and received (expenses) invoices, history included;
 * `history` is the part of each sum that came from invoices before fiscalm (contract: Turnover). */
export type Turnover = { sales: Sums; expenses: Sums; history: { sales: Sums; expenses: Sums } };

export type TopPartner = Sums & { partner: string | null; company: Co; name: string; count: number };

/** Acasă's numbers for one period: what came in and went out of the accounts, and who the firm traded with most. */
export type DashboardPeriod = { from: string; to: string; in: Sums; out: Sums; left: Sums; suppliers: TopPartner[]; clients: TopPartner[];
  /** complete only when every month in the period is (ChartMonth). */
  cash_status: CashStatus;
  /** Each firm's sales by product in the period. */
  product_sales: ProductSales[] };

/** One firm's sales by product (docs/openapi.json): revenue without VAT, storno signed, one row per product, currency
 * and unit, never added across them; a line without a product is "Fără produs". `complete` false when
 * `unavailable_count` invoices from SPV had no readable lines. */
export type ProductSalesRow = { product_id: string | null; name: string; currency: string; unit: string; revenue: string; quantity: string; invoice_count: number };
export type ProductSales = { company: Co; complete: boolean; unavailable_count: number; totals: ProductSalesRow[] };

export type Period = "month" | "quarter" | "half_year" | "trailing_year" | "year";

/** A month's cash is known only when trustworthy statements cover every current account for all of it; until then the
 * lines seen so far are `observed`, never the month's numbers. */
export type CashStatus = "complete" | "partial" | "none";
export type ChartMonth = {
  month: string; in_progress: boolean; cash_status: CashStatus;
  /** Non-null only when `cash_status` is complete. */
  in: Sums | null; out: Sums | null; net: Sums | null;
  observed: { in: Sums; out: Sums; net: Sums };
  /** Issued invoices (net of storno) and what was collected on them; received invoices and what was paid. */
  invoiced: Sums; collected: Sums; received: Sums; paid: Sums;
};
export type AgingKey = "not_due" | "1_30" | "31_60" | "over_60" | "unknown_due";
export type AgingBucket = Sums & { bucket: AgingKey; count: number };
/** Open invoices by days past due, every bucket in that order; what a person marked paid without proof is apart, in `confirming`. */
export type Aging = { buckets: AgingBucket[]; confirming: Sums & { count: number } };
/** An account's proven closing balances, oldest first, never interpolated. */
export type BalanceSeries = { company: Co; iban_last4: string; currency: string; bank: string; points: { day: string; balance: string }[] };
/** VAT from the recorded invoices: an estimate, never the declaration; `partial` when `missing_count` invoices carry no VAT data. */
export type VatMonth = { month: string; estimate: true; basis: "recorded_invoices"; in_progress: boolean; partial: boolean; missing_count: number;
  collected: Sums; deductible: Sums; net: Sums };
export type DashboardCharts = {
  /** Twelve months, oldest first, the last the one with today. */
  months: ChartMonth[];
  aging: { receivables: Aging; payables: Aging };
  balance_history: BalanceSeries[];
  /** The newest proven balance of each account, per currency; `partial` when an account has none, `day` the oldest day used. */
  balance_total: Sums & { partial: boolean; day: string | null };
  /** The previous month, then this one. */
  vat: VatMonth[];
};

export type Upcoming = {
  company: Co; date: string; side: "in" | "out"; kind: "invoice" | "payment" | "issued"; id: string; who: string; number: string;
  amount: string; currency: string;
};

export type HeldMoneyRow = { id: string; company: Co; partner: string; name: string | null; currency: string; amount: string };
export type DashboardRows = { upcoming: Upcoming; held_money: HeldMoneyRow };
export type DashboardCollection = keyof DashboardRows;
export type DashboardPages = { [C in DashboardCollection]: Page<DashboardRows[C]> };

/** Acasă (docs/openapi.json): sums the engine makes, the app only shows them. */
export type Dashboard = {
  periods: Record<Period, DashboardPeriod>;
  months: { month: string; in: Sums; out: Sums }[];
  /** One per current account; balance and day null when no recent statement proves it. */
  balances: { company: Co; iban_last4: string; currency: string; bank: string; balance: string | null; day: string | null }[];
  /** The newest account line counted, or null when there is none. */
  through: string | null;
  upcoming: Upcoming[];
  /** De explicat: Tranzacții's own count (`transactions.to_explain`). */
  to_explain: number;
  charts: DashboardCharts;
};

export type PackDoc = { kind: "statement" | "invoice"; vendor: string | null; date: string | null; note: string };

/** A debit that needs a document: how it left the account (`kind`) names it, "Plată cu cardul" or "Transfer". */
export type CardPayment = { date: string; who: string; kind: "card" | "transfer"; amount: string; orig: string; doc: string | null; currency: string; direction: "debit" | "credit"; awaiting: boolean };

export type Bank = {
  statement: boolean;
  /** Why a received statement can't be read; null when it can. */
  error: "missing" | "password" | "format" | "unreadable" | null;
  balanced: boolean | null;
  card: CardPayment[];
  efactura_suppliers: number;
  advances: { date: string; amount: string }[];
};

export type ReportStatus = "preparing" | "ready" | "prepared" | "sent";

export type Report = {
  month: string;
  status: ReportStatus;
  sent_on: string | null;
  /** When the unsent version was prepared. */
  prepared_at: string | null;
  /** The unsent version no longer matches the month: pack-prepare makes a new one. */
  stale: boolean;
  version: string | null;
  /** The send/undo revision shown: pack-sent and pack-unsent pass it back. */
  mark: string;
  docs: PackDoc[];
  doc_count?: number;
  counts?: Record<"received" | "documents" | "issued" | "to_check" | "docs" | "not_mailed" | "purchases" | "card" | "matched" | "advances" | "ambiguous" | "statements" | "open", number>;
  /** The suppliers whose documents of the month stay out of the pack (their partner's "Nu pune în dosarul contabilului"). */
  not_mailed: string[];
  received: string[];
  documents: string[];
  issued: string[];
  to_check: string[];
  /** null for the current month (its statement comes next month). */
  bank: Bank | null;
};

export type News = {
  kind: "invoice" | "document" | "statement" | "rule";
  title: string;
  at: string;
  /** The document's or invoice's own date; null for mails. */
  date: string | null;
  ref: string | null;
  result: string;
};

/** Every configured company, in config order: names for the switch, its colour scheme, its overdue count. */
/** `logo`: the SHA-256 of the firm's current invoice logo, null without one. */
export type Companies = { key: Co; name: string; legal_name: string; palette: string; late: number; logo: string | null }[];

export type Rule = {
  id: string;
  kind: "spv_auto_upload" | "shop_auto_issue" | "accountant_pack" | "receipt_upload" | "supplier_autopay" | "client_reminders";
  company: Co;
  enabled: boolean;
  label: string | null;
  reason: "paused" | "shadow_mode" | "anaf_not_connected" | "supplier_inactive" | "invoice_refused" | "revoked" | "expired" | "exhausted" | "invalid_settings" | null;
  last: { action: string; date: string | null; count: number } | null;
  /** Reamintiri de plată: when a reminder may be prepared, and whether the firm set it or it is the default (null when
   * the stored setting is broken); Trimitere automată în SPV, only while it is on: the days after issue it sends an
   * invoice. Null for other rules. */
  settings?: ReminderSettings | SpvSettings | null;
  waiting_manual: number;
  waiting_queue: number;
  can_pause: boolean;
  can_resume: boolean;
  /** Its settings can be changed here (`rule-reminders`, `rule-spv-delay`); older engine: absent. */
  can_edit?: boolean;
};

export type ReminderSettings = { after_days: number; source: "firm" | "default" };
export type SpvSettings = { upload_after_days: number };

/** Acasă's Configurează card: what is not yet set up for this firm (administrators; null once done, hidden when dismissed). `int`: the
 * Setări › Integrări sheet for shops; null for bank and history. */
export type SetupCard = { left: { key: "bank" | "history" | "shops"; int: string | null }[]; hidden: boolean };

export type State = {
  bounded?: boolean;
  setup?: SetupCard | null;
  upcoming_count?: number;
  recurrences: Recurrence[];
  /** Drafts a recurring invoice made that the engine did not issue, still drafts: each a task with its reason. */
  recurring_refused: { id: string; client: string; recurrence: string; why: string; at: string }[];

  rules: Rule[];
  can_manage_companies?: boolean;
  co: Co;
  live: boolean;
  today: string;
  companies: Companies;
  payable: Money & { confirming: string; confirming_other: Money["other"] };
  receivable: Money;
  dashboard: Dashboard;
  dashboard_pages: DashboardPages;
  task_summary: TaskPage;
  invoices: Invoice[];
  /** The last four digits of the cards a person said are this firm's: the card choices when marking paid. */
  firm_cards: string[];
  documents: CardInvoice[];
  expenses: Expense[];
  issued: Issued[];
  history: HistoryInvoice[];
  partners: Partner[];
  accountant: { cui: string; senders: string[]; recipient: string; partner?: Pick<Partner, "id" | "name" | "cui"> | null };
  record_counts?: Record<string, number>;
  invoice_counts?: { open: number; marked: number };
  /** The firm's payment files, newest first. */
  files: PaymentFile[];
  payment_file_defaults: PaymentFileDefaults[];
  /** Automatic payments held for last month's statement of the paying account: one task, released by the upload. */
  statement_hold: { month: string; count: number } | null;
  /** The company's connected banks; Home asks for a reconnect before access ends (older engine: absent). */
  bank_connections?: BankStatus[];
  reports: Report[];
  /** Per year, newest first, with each month that has an invoice. */
  turnover: { years: { year: string; turnover: Turnover; months: { month: string; turnover: Turnover }[] }[] };
  to_check: MailQuestion[];
  news: News[];
  system: {
    version: string;
    telemetry: { enabled: boolean; forced_off: boolean };
    updates: { enabled: boolean; release: { tag_name: string; html_url: string; published_at: string } | null; command: string };
    configuration_error?: string | null;
    /** not_set_up also covers a deliberately disabled ANAF integration. */
    anaf: { status: "not_set_up" | "not_connected" | "ok" | "expired" | "error"; warnings?: string[]; authorized: boolean; expires: string | null; days_left: number | null; last_sync: string | null; error: string | null;
      /** Needs a person: credentials that can't be read or SPV access refused (other errors retry on their own). */
      reconnect: boolean };
    // Mirror health; independent of draft-mail credentials.
    /** Reading the mail accounts (Integrări, Citire facturi). */
    mail: { state: "not_set_up" | "ok" | "error" | "unknown"; last_ok: string | null; stale: boolean;
      /** An account must be signed in again (Microsoft); other failed reads retry on their own. */
      reconnect: boolean };
    last_run: string | null;
    last_check: string | null;
    log: string[];
  };
  /** Open invoices autopay left out, with the current reason and masked account. */
  refused: Refused[];
  unreadable: { id: string; listed: string | null }[];
  /** Statement debits that fit several invoices and settled none: the user picks the one each paid (resolve-debit). */
  ambiguous: Ambiguous[];
  /** Tranzacții: the statement transactions of the last 13 months, newest first, and how many have no explanation. */
  transactions: { rows: Transaction[]; to_explain: number; reasons: Record<Transaction["direction"], { less: Reason[]; more: Reason[] }>;
    remembered: Remembered[] };
  settlements?: Settlement[];
  settlement_tasks?: { kind: "settlement_review"; id: string; company: Co; reason: string; line: number | null; credit: SettlementCredit | null; invoice_count: number; net: string; currency: string; sheet: { co: Co; set: string } }[];
  settlement_transactions?: { id: string; company: Co; settlement: string; sheet: { co: Co; tx: string }; links: { settlement: string; issued: string[] }; fee: string; currency: string }[];
  cod?: { count: number; totals: { currency: string; total: string }[]; overdue_days: number;
    rows: { id: string; company: Co; invoice: string; awb: Awb; waiting: boolean; returned: boolean; collected: boolean; overdue: boolean }[];
    tasks: { kind: "cod_returned" | "cod_overdue"; id: string; company: Co; invoice: string; sheet: { co: Co; iss: string }; suggestion: "review_refund_draft" | null }[] };
  settlement_fees?: { report_id: string; amount: string; currency: string; kind: "settlement_fee" }[];
  /** Connector refusals and return decisions, company-bound. */
  emag_tasks?: { id: string; external_id: string; reason: string; order_id: string | null; draft_id: string | null }[];
  /** Evidence-checked reminder text for a human to copy and send. */
  reminder_tasks: ReminderTask[];
  orders?: CommerceOrder[];
  order_tasks?: {kind: "order_refused" | "order_vat_review"; id: string; company: Co; order_id: string; draft_id: string | null; sheet: "ord"; reason: {code: string; message: string}; label: string; action: "order-discard" | null}[];
  sales_origins?: SalesOrigin[];
  cod_receivable?: {total: string; other: {currency: string; total: string}[]; count: number};
  /** Cache-only partner warnings: an inactive supplier with invoices to pay, an inactive or removed client with invoices
   * to collect. Key on kind + id because one partner can hold both roles. `checked_at` is ANAF's last check. */
  partner_tasks: PartnerTask[];
};

/** What the engine already decided explains a transaction (docs/openapi.json, `transactions`). */
export type Explanation =
  | { kind: "payment"; invoices: string[]; payments: PaidTo[]; link?: TxLink }
  | { kind: "collection"; issued: string[]; settlement: string | null; link?: TxLink }
  | { kind: "receipt"; invoices: string[]; document: string | null; link?: TxLink }
  | { kind: "label"; label: Label; note: string | null; link: TxLink }
  | { kind: "advance"; who: string; link: TxLink }
  | { kind: "transfer"; other: string | null; link: TxLink };
/** Fără document: what a transaction no document explains is (fiscalm.links.LABELS). */
export type Label = "bank_fee" | "interest" | "personal" | "other";
/** What the user says a transaction is (tx-link `kind`). */
export type LinkKind = "documents" | "label" | "transfer" | "advance";
export type PaidTo = { kind: string; payee: string; ref: string; details: string };
/** What the difference between a transaction and its documents is (fiscalm.links.REASONS). */
export type Reason = "rest" | "courier_fee" | "bank_fee" | "fx" | "other";
/** The user's own explanation of a transaction (tx-link): the parts of invoices it settles, the named difference, who and when. */
export type TxLink = {
  id: string;
  /** The transaction it is recorded on: a transfer's other end shows it too, and is changed or undone from it. */
  tx: string;
  /** "receipt": a bon uploaded from the debit's sheet (document-upload with `tx`), never posted to tx-link. */
  kind: LinkKind | "receipt";
  /** A transfer's other end, when it was found; for a receipt, its document. */
  other: string | null;
  partner: string | null;
  parts: { id: string; amount: string; closed: boolean }[];
  payments: PaidTo[];
  reason: Reason | Label | null;
  /** The transaction less what its documents had left: negative when less money came or went. */
  difference: string;
  /** What the parts and the difference are counted in: the documents' currency (for a card, maybe its original one). */
  currency: string;
  note: string;
  /** Made by no person: a saved rule, the bank's own fee or interest line, or both ends of a transfer between the
   * firm's accounts. Undoing it leaves the line to people. */
  rule: "saved" | "bank" | "transfer" | null;
  by: string;
  at: string;
};
/** What the engine would suggest for a transaction without an explanation; never applied on its own. */
export type Proposal =
  | { kind: "invoices"; invoices: string[] }
  | { kind: "receipt"; document: string }
  | { kind: "settlement"; settlement: string }
  | { kind: "remembered"; memory: string; as: { kind: LinkKind; partner: string | null; reason: string | null; note: string | null } };
/** What the user asked to be remembered (Ține minte): kept for an account, a card or a merchant. */
export type Remembered = {
  id: string; by: MemoryKey; shown: string; kind: LinkKind; partner: string | null; reason: string | null; note: string;
  who_by: string; at: string;
};
export type MemoryKey = "iban" | "card" | "name";

export type Transaction = {
  id: string;
  company: Co;
  date: string;
  direction: "debit" | "credit";
  amount: string;
  currency: string;
  /** The company account's last four digits. */
  account: string | null;
  source: "statement" | "bank";
  /** The bank's text: data, shown as text. */
  counterparty: string | null;
  details: string | null;
  card: string | null;
  card_owner: string | null;
  orig: { amount: string; currency: string } | null;
  explanation: Explanation | null;
  proposal: Proposal | null;
  /** What the bank's own codes or words say the line is: the sheet's default answer, never an explanation. */
  bank_says: "bank_fee" | "interest" | null;
  /** Aștept factura (`tx-await`): a debit still to explain whose invoice has not arrived. */
  awaiting: { note: string; by: string; at: string } | null;
  /** The user's explanation's partner, else the one the bank's text points to: Plătitor's default, never an explanation. */
  partner: string | null;
  /** Can be explained, or its explanation changed, on its sheet now (the engine decides). */
  can_link: boolean;
  /** What it can be remembered by (Ține minte), as shown: an account or a card by its last four digits, a merchant. */
  remember: Partial<Record<MemoryKey, string>>;
};

export type Ambiguous = {
  candidate_count?: number;
  tx: string;
  date: string;
  amount: string;
  currency: string;
  counterparty: string;
  description: string;
  month: string;
  can_resolve: boolean;
  candidates: { id: string; kind: "efactura" | "payment"; number: string; amount: string; currency: string; date: string | null }[];
};

/** An open invoice autopay left out of the payment file (docs/openapi.json). */
// Unknown reasons retain the existing display fallback while the API adds reasons.
// When the refusal was recorded for an inactive supplier, or the engine finds it inactive now, `refused_checked_at` is the date of
// the ANAF check it was recorded with (else today's check) and `still_inactive` whether the engine's cache lists the supplier
// inactive now (false once a later check found it active, null when no trusted status remains); otherwise both are null.
export type Refused = {
  id: string; iban_last4: string; supplier: string;
  why: "unclear" | "supplier_inactive" | "ready" | string;
  refused_checked_at: string | null; still_inactive: boolean | null;
};

/** co=ALL: every company's state, with payable and receivable summed by the engine. Read only. */
export type AllState = {
  bounded?: boolean;
  upcoming_count?: number;
  can_manage_companies?: boolean;
  co: "ALL";
  live: boolean;
  today: string;
  companies: Companies;
  payable: Money & { confirming: string; confirming_other: Money["other"] };
  receivable: Money;
  dashboard: Dashboard;
  dashboard_pages: DashboardPages;
  task_summary: TaskPage;
  parts: State[];
};

export type View = State | AllState;

/** True for the view of all companies (a company's key is never ALL: the engine refuses it in the config). */
export const isAll = (v: View): v is AllState => "parts" in v;

export const parts = (v: View): State[] => (isAll(v) ? v.parts : [v]);

/** The name the app shows for a company's state (its key if the config has no name for it). */
export const coName = (s: State): string => s.companies.find((c) => c.key === s.co)?.name ?? s.co;

export type Done = { id: string; held: string };

/** Integrări (GET api/integrations): what can be added, and what is configured. Secrets only as set or not set. */
export type IntegrationField = {
  key: string; label: string; secret: boolean; required: boolean; choices: string[]; choice_labels?: Record<string, string>; type: "text" | "url" | "email" | "emails" | "int" | "host" | "company";
  default: string;
  /** Required only in some configurations: any clause whose fields all hold one of its values ("" = empty). */
  required_when: Record<string, string[]>[];
  /** Asked only in some configurations, in the same clauses; empty = always. */
  shown_when: Record<string, string[]>[];
  /** The install knows the value (`default`): it isn't asked while it holds it. */
  known: boolean;
};
/** Where ANAF sends the browser back (the Callback URL its OAuth app needs), or why there is none yet. */
export type AnafRedirect = { redirect_uri: string | null; redirect_error: string | null };
/** The ANAF integration's connection (anaf.connection): never a token. `expires` is the day the authorization ends;
 * `expiring` is within 30 days of it, `error` means the saved connection can't be read. `refusal`: why the last
 * attempt to connect did not finish, in the engine's words, while it is recent. */
export type AnafConnection = AnafRedirect & {
  state: "not_connected" | "connected" | "expiring" | "expired" | "error"; expires: string | null; connected_at: string | null;
  refusal?: string | null;
};
/** mail-detect (fiscalm.mail.setup.detect): a provider's fixed settings for an address; `code` only when the address is not one. */
export type MailProvider = { code?: "invalid_settings"; provider: string; host: string; port: number; username: string;
  app_password_required: boolean; app_password_url: string; hint: string; supported: boolean; hosts: string[]; method?: MailMethod };
/** How an address connects: a password over IMAP, or a Microsoft sign-in. `supported` false: not at all (Proton). */
export type MailMethod = "imap" | "microsoft";
/** mail-microsoft-start and mail-microsoft-poll (fiscalm.mail.microsoft): a sign-in in progress, connected, or why not. */
export type MicrosoftCode = "authorization_pending" | "slow_down" | "connected" | "authorization_declined" | "expired_token"
  | "admin_consent_required" | "tenant_policy_blocked" | "not_configured" | "account_mismatch" | "reconnect_required"
  | "invalid_settings" | "connection_changed" | "invalid_scopes" | "connection_failed";
export type MicrosoftSignIn = { code: MicrosoftCode; id?: string; user_code?: string; verification_uri?: string; expires_in?: number; interval?: number };
/** mail-test: a read-only LOGIN and EXAMINE; on `ok`, the server that answered. */
export type MailTestCode = "ok" | "wrong_password" | "app_password_required" | "host_unreachable" | "tls_error" | "folder_missing"
  | "timeout" | "invalid_settings" | "unsupported_provider" | "protocol_error";
export type MailTest = { code: MailTestCode; host?: string; port?: number; username?: string };
export type IntegrationKind = { key: string; label: string; single: boolean; fields: IntegrationField[]; connect?: AnafRedirect };
export type Integration = {
  cli_status?: "ready" | "missing" | "not_logged_in";
  sync_error?: boolean;
  /** ANAF only. */
  connection?: AnafConnection;
  /** Mail only: the inbox's last read (`ok` null before the first) and a failed one's kind, never its error text. */
  reading?: { ok: boolean | null; at: string | null; last_ok: string | null; code?: Exclude<MailTestCode, "ok"> | MicrosoftCode };
  id: string; kind: string; name: string; enabled: boolean; default: boolean; config: Record<string, string>;
  secrets: Record<string, boolean>; updated_at: string;
  /** ANAF and mail only: the firms it serves. "all" includes firms added later; [] serves none. */
  firm_scope?: FirmScope;
};
export type FirmScope = "all" | Co[];
/** Per configured firm, in company order: the ANAF connection the engine reads it with (null: none covers it), the
 * other enabled ANAF connections that also cover it, and the enabled inboxes that file for it. */
export type Coverage = { firm: Co; anaf: string | null; anaf_also: string[]; mail: string[] };
export type Integrations = { can_manage_ai?: boolean; kinds: IntegrationKind[]; items: Integration[]; live: boolean; coverage?: Coverage[] };

export type ActionResult = {
  ok: boolean;
  message: string;
  /** The person cancelled the step-up this action asked for; nothing was done and nothing is shown. */
  cancelled?: boolean;
  state?: State | null;
  next?: { tab: string; at?: string | null } | null;
  done?: Done | null;
  /** tx-link: the explanation it recorded (`explanation.link.id`), never an invoice. */
  linked?: { id: string };
};

// ------------------------------------------------------------------ acces (docs/openapi.json)

/** A link through which anyone holding it can send receipt photos and PDFs into one company's inbox. Never its secret. */
export type UploadLink = {
  id: string; company: string; label: string; created_at: string; expires_at: string; revoked_at: string | null;
  uploads: number; last_used_at: string | null; max_files: number | null;
};

/** A key for API v1 and MCP clients, install-wide. Never its secret. */
export type ApiToken = {
  id: string; name: string; scope: "read" | "write"; created_at: string; last_used_at: string | null; revoked_at: string | null;
  /** Account owner, and audit actor (null for server CLI creation). */
  user_id: string | null; owner: { id: string; display_name: string } | null;
  created_by: string | null;
};

/** What creating a link or a key answers: its secret, shown once. */
export type Created = { id: string; path?: string; url?: string; token?: string; scope?: string; revoked?: boolean };

// ------------------------------------------------------------------ invoicing (GET api/invoicing, docs/openapi.json)

export type Address = { street: string; city: string; county: string; postal_code: string; country: string };

export type Seller = {
  legal_name: string; cui: string; vat_payer: boolean;
  /** "TVA la încasare": declared, only for a VAT payer; the engine adds the mention where the law allows it. */
  vat_on_collection?: boolean; vat_id: string; reg_com: string; address: Address;
  iban: string; bank: string; email: string; phone: string;
  /** The logo on the company's invoices (read-only here: it changes through its own upload). The engine always sends it and
   * `logo_unavailable` with a seller; optional only so an older engine without logos still type-checks. */
  logo?: SellerLogo | null;
  /** A logo is set but its file can't be read: none is printed, and it can be uploaded again or removed. */
  logo_unavailable?: boolean;
};

export type SellerLogo = { sha: string; type: "image/png" | "image/jpeg"; width: number; height: number };

/** `imported`: the highest number of the series the firm used outside fiscalm (fiscalm.history), null if none.
 * `default`: new invoices start in it. `remove_refusal`: why the engine keeps it (an issued invoice, a draft, a recurring
 * invoice), null when it can be removed. */
export type Series = { code: string; next_number: number; issued: number; default: boolean; remove_refusal: string | null; imported?: number | null };
/** A series the imported invoices use and fiscalm doesn't have yet: it continues from `next_number`. */

export type Client = {
  id: string; name: string; cui: string; vat_id: string; private: boolean; country: string; address: Address; email: string;
  last_used: string | null;
  /** The term, currency and lines of the latest invoice issued to this client, which a new invoice to it starts from. */
  last_invoice?: { due_days: number; currency: string; lines: InvoiceLine[] } | null;
  /** From ANAF's public data; kept by the engine when the client is edited. */
  reg_com?: string; phone?: string; iban?: string; bank?: string; account_name?: string; vat_payer?: boolean;
  efactura_registered?: boolean;
  ro_vat_status?: "unknown" | "registered" | "not_registered";
  /** Inactive at ANAF: the engine refuses to save it as a client. */
  inactive?: boolean;
  /** A local tax number for a client outside the EU (CH, US): not a VAT id. */
  tax_id?: string;
};

/** One line of an invoice, as the user enters it; amounts and quantities as decimal strings. */
export type InvoiceLine = {
  description: string; quantity: string; unit: string; unit_price: string; vat_category: string; vat_rate: string; gross_unit_price?: string;
  exemption: string;
  /** The firm's product the line was filled from; the line keeps its own values. */
  product_id?: string | null;
};

/** One firm's catalog entry (Produs, docs/openapi.json `Product`): what a new invoice line starts from. `unit_price` has two
 * decimals, net of VAT; `unit` is an invoicing unit code. `version` guards every write; `can_delete` only while no invoice
 * line references it (otherwise it can only be archived). */
export type Product = {
  id: string; company: Co; kind: "produs" | "serviciu"; name: string; code: string; unit: string; unit_price: string; currency: string;
  vat_category: string; vat_rate: string; exemption: string; description: string; archived: boolean; version: number; can_delete: boolean;
};
/** What a person types for a product: the catalog entry without what the engine keeps. */
export type ProductFields = Omit<Product, "id" | "company" | "archived" | "version" | "can_delete">;
/** GET api/products: one page of the firm's active or archived products. */
export type Products = { company: Co; products: Product[]; total: number; next_cursor: string | null };
/** A product action's answer: the product as saved (none after a delete). */
export type ProductResult = ActionResult & { done?: { product?: Product; id?: string } | null };

export type Totals = {
  net: string; vat: string; total: string; by_vat: { category: string; rate: string; base: string; vat: string }[];
  total_ron: string | null;
  /** The VAT in lei for an invoice in another currency (the engine's own conversion; the XML says the same). */
  vat_ron?: string | null;
};

export type Draft = SalesOrderFields & {
  id: string; series: string; client_id: string; client: Partial<Client> & { name: string }; issue_date: string;
  due_date: string; currency: string; exchange_rate: { rate: string; date: string } | null; lines: InvoiceLine[];
  notes: string; totals: Totals; updated_at: string; version: number; /** The IBAN the draft issues with; a chosen account no longer current comes back as stored, "" for the profile's. */ iban: string;
  /** Needed when a line is an intra-community supply (its VAT choice says needs_delivery_date). */
  delivery_date?: string | null;
  /** An invoice for an advance received for its whole base; then the date it was received. */
  advance_invoice?: boolean;
  advance_payment_date?: string | null;
  /** The engine's: the date the VAT becomes chargeable (the rate is the BNR's for it), and what else the invoice will say. */
  chargeability_date?: string | null;
  mentions?: string[];
  /** The engine's warnings for the draft (it still saves and issues), e.g. an advance invoice issued late. */
  warnings?: string[];
  /** The VAT place of supply (ISO code, RO by default). The app doesn't edit it; it sends back what was saved. */
  supply_country?: string;
};

/** A VAT treatment a line can have, as the engine offers it for this seller (no tax rule lives in the app). */
export type VatChoice = {
  key: string; label: string; category: string; rate: string; exemption: string; needs_delivery_date: boolean;
};

/** Which fields the engine requires, as dotted paths ("address.county"); the app reads it, the engine still checks it. */
export type Required = {
  seller: string[]; seller_if_ro: string[]; seller_if_vat_payer: string[];
  client: { RO: string[]; other: string[] }; client_identity_any_of: string[];
  /** A Romanian client registered for VAT needs these too; an advance date needs the advance-invoice flag. */
  client_if_vat_payer?: string[]; draft_if_advance_payment_date?: string[];
  /** A foreign client registered for VAT in Romania needs its Romanian CUI and VAT code; K needs the client's region. */
  client_if_ro_vat_registered?: string[]; client_if_k?: string[];
};

export type Recurrence = {
  id: string; company: Co; client_id: string; series: string; day: number; due_days: number;
  template: { currency: string; notes?: string; lines: Record<string, string>[]; supply_country?: string };
  start_period: string; paused: boolean; reason: "manual" | "missing_client" | "missing_series" | "invalid_template" | "rate_unavailable" | "unsupported_currency" | null;
  last_skipped: { recurrence: string; company: Co; from_period: string; through_period: string; count: number; at: string } | null;
  last_period: string | null; skipped_periods: number; backlog_policy: "current_only";
  /** The engine's, for display: the saved client's name (null once it is gone), the template's total in its currency (null when
   * it no longer validates) and the day the next draft is prepared (null while stopped). */
  client: string | null; total: string | null; next_date: string | null;
  /** The invoice the rule last issued on its own: its number, the client as issued, when. */
  last_invoice: { id: string; number: string; client: string; at: string } | null;
  /** What the engine accepts now: Oprește, Pornește, Șterge. */
  can_pause: boolean; can_resume: boolean; can_delete: boolean;
};

export type InvoiceAccount = { iban: string; currency: string; bank: string; bank_name: string; pays: boolean };

export type Invoicing = {
  recurrences: Recurrence[];
  seller_prefill: Omit<Seller, "vat_payer"> & { vat_payer: boolean | null };
  co: Co; live: boolean; today: string; seller: (Seller & { logo: { sha: string; type: "image/png" | "image/jpeg"; width: number; height: number } | null; logo_unavailable: boolean }) | null; auto_upload: boolean;
  /** Days after issue the scheduled run sends a new invoice to SPV (the firm's ANAF integration); null when it sends none. */
  upload_after_days: number | null; series: Series[]; /** The company's first series as proposed, until it has one. */
  series_new?: { code: string; next_number: number } | null; clients: Client[];
  /** The term of the company's latest issued invoice (30 days before any): a new invoice's due date starts from it. */
  due_days: number;
  drafts: Draft[]; line_description_max: number; units: { code: string; label: string }[];
  /** The firm's active products, for an invoice line's picker. */
  products?: Product[];
  /** A Romanian address's counties by name; Bucharest once per sector, whose `city` the engine requires with it. */
  counties: { county: string; city: string | null; label: string }[];
  /** The country codes a client's address may carry (ISO 3166-1 alpha-2, and 1A for Kosovo). */
  countries: string[];
  /** The company's current accounts: the only IBANs an invoice may carry. */
  accounts: InvoiceAccount[];
  /** The accounts an invoice can name and, per currency, the one a new invoice starts with; null before the seller is saved. */
  invoice_accounts: { accounts: { iban: string; currency: string; bank_name: string }[]; default: Record<string, string> } | null;
  vat: { choices: VatChoice[]; default: string };
  required?: Required;
};

/** The answer to POST api/invoicing/<op>: an action's answer plus the invoicing data after it and the affected id. */
/** `invoicing` and `state` can be null after a committed action whose refresh failed (the action still happened). */
export type InvoicingResult = ActionResult & { recurrences?: Recurrence[]; invoicing?: Invoicing | null; id?: string | null; version?: number; code?: string; client?: Partial<Client> };

/** One of a company's accounts in GET api/companies: never a full IBAN. */
export type CompanyAccount = {
  /** Opaque: names the account for a CSV upload without its IBAN. */
  id: string; iban_last4: string; currency: string; bank: string; bank_name: string; pays: boolean; retired: string | null;
  last_statement: string | null; statement_senders: string[];
};

// ------------------------------------------------------------------ firme (GET api/companies, docs/openapi.json)

/** A configured company as Firme manages it: display fields only, never bank accounts or other private config. */
/** A bank holding a company's current account, as the engine says it can be connected (fiscalm.bank.connections.status). */
export type BankStatus = {
  bank: string; name: string; connect: "ready" | "not_set_up" | "not_supported";
  /** Banks with password-protected PDF statements only: whether the firm's statement password is set (never the value). */
  statement_password?: boolean;
  /** Every available source works at once; `default` is the first that works. Mail only: whether fiscalm reads the
   * bank's statement mail (`supported`) and the ids of the firm's inboxes that read it. */
  sources?: { source: "open_banking" | "mail" | "upload"; available: boolean; default: boolean; supported?: boolean; mailboxes?: string[] }[];
  connection: {
    state: "ok" | "expiring" | "expired" | "error"; valid_until: string | null; last_read: string | null;
    /** The last four digits of each account read; never a whole IBAN. */
    accounts: string[];
    /** Why the last read failed, in Romanian; null when it worked. */
    error: string | null;
  } | null;
};

/** A private, expiring choice offered only to the administrator who started consent. */
export type BankAccountOffer = {
  id: string; bank: string; valid_until: string;
  accounts: { id: string; last4: string; currency: string; existing: boolean }[];
};

export type CompanyEntry = {
  key: string; name: string; legal_name: string; cui: string; palette: string; has_data: boolean;
  /** The account payments are made from (the payment file debits it): whether it is set, and only its last 4 digits, never the whole IBAN. */
  has_ron_account?: boolean; iban_last4?: string; accounts?: CompanyAccount[];
  /** Catalogue options and current banks, with the install’s source availability. */
  banks?: BankStatus[];
  /** Its other names on documents, plain text, and the legal name without its legal form, offered while it has none. */
  aliases?: string[]; alias_suggestion?: string;
  /** Invoices imported from the program used before fiscalm (fiscalm.history): the start date (null until the first
   * import) and how many invoices from before it are kept as history (older engine: absent). */
  history?: CompanyHistory;
};

/** `first_statement`: the firm's oldest statement day, from which the start date is proposed. */
export type CompanyHistory = { start: string | null; first_statement?: string | null; issued: number; received: number };

export type CompaniesConfig = {
  companies: CompanyEntry[];
  palettes: string[];
  limits: { key_pattern: string; reserved_keys: string[]; name_max: number; alias_max?: number; aliases_max?: number };
};

/** What ANAF's public data says about a CUI before it is added (nothing is saved). */
export type CompanyLookup = { cui: string; legal_name: string; address: string; vat_payer: boolean; inactive: boolean };

/** GET api/state before the first company; ordinary State resumes after addition. */
export const isSetup = (v: unknown): v is SetupState => !!v && (v as { setup?: unknown }).setup === "needs_company";

export type SetupState = {
  setup: "needs_company";
  companies: [];
  live: boolean;
  can_manage_companies: boolean;
};

export type Awb = { courier: string; number: string; picked_up_at?: string; delivered_at?: string; returned_at?: string };
export type Settlement = {
  partner?: string;
  id: string; kind: "settlement"; company: Co; source: string; report_key: string; reference: string | null; candidate: (SettlementCredit & { payer: string }) | null;
  payer: { id: string; name: string }; date: string; currency: string; gross: string; fee: string; net: string;
  credit: SettlementCredit | null; invoice_count: number; can_confirm: boolean; human_confirmed: boolean;
  status: "pending" | "collected" | "withdrawn"; error: string | null; failed_line: number | null; tx_key: string | null;
  sheet: { co: Co; set: string }; links: { transaction: string | null; issued: string[] };
  lines: { position: number; reference: string | null; awb: string | null; order: string | null; gross: string; fee: string; invoice: string | null; proved: boolean }[];
};

export type SalesOrderFields = {
  order_id?: string | null; order_sheet_id?: string | null; sales_origin?: string;
  order_number?: string | null; awb?: {courier: string; number: string} | null; cod_at_courier?: boolean;
};
export type CommerceOrder = {
  id: string; company: Co; channel: string; external_id: string; sheet_id: string;
  status: "draft" | "issued" | "cancelled" | "refused";
  body: Record<string, unknown>; draft_id: string | null; invoice_id: string | null;
  invoice_summary: {currency: string; totals: {net: string; vat: string; total: string}} | null;
  reason: {code: string; message: string} | null; channel_name: string | null; company_name: string;
  awb: {courier: string; number: string} | null;
  delivery: {picked_up_at: string | null; returned_at: string | null};
  cod_at_courier: boolean; can_discard: boolean; can_cancel: boolean;
  links: {draft: string | null; invoice: string | null; pdf: string | null};
};
export type SalesOrigin = {id: string; company: Co | null; label: string};

export type SettlementCredit = { id: string; date: string; amount: string; currency: string };

/** A mailed document in doubt (fiscalm.mail.invoices): what was read on it and the answers to pick. Untrusted text. */
export type MailQuestion = {
  key: string;
  subject: string;
  sender: string;
  at: string;
  date: string | null;
  supplier: string;
  number: string | null;
  /** no-firm: no firm on it; two-firms: several; other-firm: the sender's remembered firm isn't the one named;
   * total: one firm, but no single total; buyer-unclear: a firm is named, but not clearly as the buyer;
   * outside-scope: the inbox it came to doesn't serve the firm named (a statement or OP offers no firm). */
  reason: "upload-firm" | "no-firm" | "two-firms" | "other-firm" | "total" | "buyer-unclear" | "outside-scope";
  /** The firms it may be for: the answers offered. */
  companies: Co[];
  /** The totals read on it; one is picked when there are several, [] when none was found. */
  totals: { amount: string; currency: string }[];
  /** pdf: mailfile previews it; null: nothing to show. */
  file: "pdf" | "jpg" | "png" | "heic" | null;
};


export type Page<T> = { data: T[]; total: number; next_cursor: string | null; months?: string[] };
export type TaskEntry = { key: string; company: Co; urgent: boolean; patch: Partial<State> };
export type TaskPage = Page<TaskEntry> & { urgent: number; highlights: TaskEntry[] };


export type SearchResults = {
  partners: Pick<Partner, "id" | "company" | "name" | "cui" | "vat_id" | "roles" | "last">[];
  issued: Pick<Issued, "id" | "company" | "client" | "number" | "total" | "owed" | "currency" | "issue_date">[];
  invoices: Pick<Invoice, "id" | "company" | "supplier" | "number" | "total" | "currency" | "issue_date">[];
  documents: Pick<CardInvoice, "id" | "company" | "vendor" | "amount" | "currency" | "date" | "month">[];
};

export type BrowsePurchase = Pick<Expense, "id" | "company" | "sources"> & { invoice: Invoice | null; document: CardInvoice | null };
export const REPORT_SCREENS = ["report_received", "report_issued", "report_tasks", "report_bank", "report_advances", "report_docs", "report_not_mailed"] as const;
export type ReportScreen = typeof REPORT_SCREENS[number];
type ReportData = { purchase: BrowsePurchase; issued: Issued; card: Bank["card"][number]; bank: Bank; mail: MailQuestion; ambiguous: Ambiguous; advance: Bank["advances"][number]; document: PackDoc; not_mailed: string };
export type ReportSectionRow = { [K in keyof ReportData]: { id: string; company: Co; kind: K; data: ReportData[K] } }[keyof ReportData];
export type TransactionSaid = { text: string; to?: { inv?: string; iss?: string; doc?: string; set?: string; tab?: "plati" | "sarcini" }; proposal?: boolean };
export type TransactionBrowseRow = { id: string; company: Co; transaction: Transaction; said: TransactionSaid | null };
export type BrowseRows = Record<ReportScreen, ReportSectionRow> & { vanzari: Issued | HistoryInvoice; cheltuieli: BrowsePurchase | HistoryInvoice; bonuri: BrowsePurchase; parteneri: Partner; tranzactii: TransactionBrowseRow };
export type ReportPages = Page<{ screen: ReportScreen; page: Page<ReportSectionRow> }>;
export type BrowseScreen = keyof BrowseRows;
export type BrowseQuery = { cursor?: string; q?: string; f?: string; month?: string; from?: string; to?: string };

export type HistoryReviewRow = { sha: string; name: string; direction: string; number: string; issue_date: string; total: string; currency: string; party_name: string; party_tax_id: string; reason: string };


/** One cookie-bound identity and the selected firm's dashboard, including its first pages. */
export type Bootstrap = { identity: Identity; state: View | SetupState | null; live: boolean };
