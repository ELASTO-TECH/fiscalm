// Display helpers over the API state: lookups and words. Every rule about money comes from the engine.
import type { Bank, CardInvoice, CardPayment, Expense, ExpenseSource, Invoice, Issued, PaidMethod, Refused, Report, ReportStatus, State, View } from "@/lib/types";
import { parts } from "@/lib/types";
import { dm, dueText, luna, previousMonth, nOf, phrase, type Tone } from "@/lib/format";

export const openInvoices = (s: State) => s.invoices.filter((r) => r.status === "open");
/** Paid by the user ("Marchează ca plătită"), not yet confirmed by a statement or a mail. */
export const marked = (s: State) => s.invoices.filter((r) => r.status === "marked");
export const PAID_AWAITING = "În curs de confirmare";

/** The engine's current reason for an automation refusal. */
export const REFUSED_WHY: Record<Refused["why"], [string, string]> = {
  unclear: ["Date neclare", "Date neclare. Verifică factura înainte de plată."],
  supplier_inactive: ["Furnizor inactiv la ANAF", "Furnizorul figurează inactiv la ANAF. Verifică situația cu contabilul înainte de plată."],
  // The refusal no longer holds. Autopay does not retry it automatically.
  ready: ["Pregătită", "Factura poate intra în fișierul de plăți."],
};
/** The engine still refuses to pay it (any reason but ready). */
export const refusedNow = (x: Refused | null): x is Refused => !!x && x.why !== "ready";
/** The engine's reason for an open invoice autopay left out, if it did; an unknown reason reads as unclear. */
export function refusalOf(s: Pick<State, "refused">, id: string): Refused | null {
  const x = s.refused.find((r) => r.id === id);
  return x ? { ...x, why: x.why in REFUSED_WHY ? x.why : "unclear" } : null;
}
/** The account as the app shows it: never more than its last four characters. */
export const maskedIban = (last4: string) => `•••• ${last4}`;

const INACTIVE_CHECK = "Verifică situația cu contabilul înainte de plată.";
const inactiveSince = (checked: string | null, today: string) =>
  `Furnizorul figurează inactiv la ANAF${checked ? ` (verificat pe ${dm(checked, today)})` : ""}. ${INACTIVE_CHECK}`;

/** Why autopay left an invoice out, in full: its Sarcini card and its sheet. `why` is the engine's current reason. An inactive
 * supplier is dated with the ANAF check the refusal was recorded with (`refused_checked_at`), never the partner's later
 * checks, and only `still_inactive` true says it is inactive now. False says ANAF shows it active, then the engine's reason
 * when one still holds, or that the invoice can be added by hand when the engine allows it (`can_approve`). Null (no trusted
 * status left) keeps the past refusal, says the current status is unknown, then the engine's reason when another one holds. */
export function refusalReason(s: State, x: Refused): string {
  const why = x.why in REFUSED_WHY ? x.why : "unclear";
  if (x.still_inactive === true) return inactiveSince(x.refused_checked_at, s.today);
  if (x.still_inactive === null) {
    if (!x.refused_checked_at) return REFUSED_WHY[why][1];
    const past = `Furnizorul era inactiv la ANAF la verificarea din ${dm(x.refused_checked_at, s.today)}. Starea actuală la ANAF nu este cunoscută. ${INACTIVE_CHECK}`;
    return why === "ready" || why === "supplier_inactive" ? past : `${past} ${REFUSED_WHY[why][1]}`;
  }
  const was = `Furnizorul era inactiv la ANAF${x.refused_checked_at ? ` la verificarea din ${dm(x.refused_checked_at, s.today)}` : ""}. `;
  if (why !== "ready" && why !== "supplier_inactive") return `${was}ANAF îl arată acum ca activ. ${REFUSED_WHY[why][1]}`;
  return s.invoices.find((r) => r.id === x.id)?.can_approve
    ? `${was}ANAF îl arată acum ca activ, așa că poți adăuga factura manual în fișierul de plăți.`
    : `${was}ANAF îl arată acum ca activ.`;
}

/** Why the engine keeps an open received invoice out of the payment file now, in the engine's words (efactura.APPROVE_RO), or null.
 * The engine decides: `can_approve` is false and its cache lists the supplier inactive, as the partner's last check
 * (dated with that check) or, for a partner missing from this state, a refusal the engine says is still current. */
export function supplierInactive(s: State, r: Invoice): string | null {
  if (r.status !== "open" || r.can_approve) return null;
  const a = s.partners.find((p) => p.id === r.partner)?.anaf_status;
  if (a?.inactive) return inactiveSince(a.checked_at, s.today);
  const x = s.refused.find((x) => x.id === r.id && x.still_inactive === true);
  return x ? inactiveSince(x.refused_checked_at, s.today) : null;
}

/** Why an open received invoice can't go in the payment file now (the engine's `can_approve` false), for a list that chose it. */
export function notInFile(s: State, r: Invoice): string {
  const x = refusalOf(s, r.id);
  if (refusedNow(x)) return refusalReason(s, x);
  if (r.currency && r.currency !== "RON") return "Fișierul de plăți e doar în lei.";
  return supplierInactive(s, r) ?? "Factura nu poate intra acum în fișierul de plăți.";
}

/** An issued invoice by its id, or by fiscalm's id for it (kept in links made before sync matched it to SPV). */
export function findIssued(v: View, id: string | null | undefined): [Issued, State] | null {
  for (const s of parts(v)) {
    const r = id ? s.issued.find((x) => x.id === id) ?? s.issued.find((x) => x.local_id === id) : undefined;
    if (r) return [r, s];
  }
  return null;
}

/** One purchase as the engine groups it (`expenses`): its received invoice, its receipt, or both ("Factură și bon"). */
export type Purchase = { id: Expense["id"]; sources: ExpenseSource[]; invoice: Invoice | null; doc: CardInvoice | null };

/** The company's purchases, one per expense. The grouping is the engine's alone: the app never pairs an invoice with a receipt.
 * A reference this state doesn't hold is left out, and a purchase left with neither is skipped. An invoice or receipt no
 * expense names (a state that missed one) is still listed, on its own, never paired. */
export function purchases(s: State): Purchase[] {
  const invoices = new Map(s.invoices.map((r) => [r.id, r]));
  const docs = new Map(s.documents.map((d) => [d.id, d]));
  const seen = new Set<object>();
  const out: Purchase[] = s.expenses.flatMap((e) => {
    const invoice = (e.invoice && invoices.get(e.invoice)) || null;
    const doc = (e.document && docs.get(e.document)) || null;
    if ((invoice && seen.has(invoice)) || (doc && seen.has(doc)) || !(invoice || doc)) return [];
    for (const x of [invoice, doc]) if (x) seen.add(x);
    return [{ id: e.id, sources: e.sources, invoice, doc }];
  });
  for (const r of s.invoices) if (!seen.has(r)) out.push({ id: `invoice:${r.id}`, sources: [], invoice: r, doc: null });
  for (const d of s.documents) if (!seen.has(d)) out.push({ id: `document:${d.id}`, sources: [], invoice: null, doc: d });
  return out;
}

/** A month's received invoices and receipts (the report's `received` and `documents`), one per purchase as `purchases`
 * groups them, so an invoice and its bon in the same month are one item. Each part counts only in the month that lists it:
 * when the invoice and its bon fall in different months, each month keeps its own part, alone. What goes in the
 * accountant's pack stays the engine's `docs`. */
export function reportPurchases(s: State, r: Report): Purchase[] {
  const received = new Set(r.received);
  const documents = new Set(r.documents);
  return purchases(s).flatMap((p) => {
    const invoice = p.invoice && received.has(p.invoice.id) ? p.invoice : null;
    const doc = p.doc && documents.has(p.doc.id) ? p.doc : null;
    return invoice || doc ? [{ id: p.id, sources: p.sources, invoice, doc }] : [];
  });
}

/** The receipt of the same purchase as this invoice, when the engine groups them (`expenses`); else null. The invoice's own
 * `bon` doesn't count: where the engine keeps them apart, the sheets and the lists do too. */
export function bonOf(s: State, invoice: string): CardInvoice | null {
  return purchases(s).find((p) => p.invoice?.id === invoice)?.doc ?? null;
}

/** The invoice of the same purchase as this receipt, as for `bonOf`; else null. */
export function invoiceOf(s: State, doc: string): Invoice | null {
  return purchases(s).find((p) => p.doc?.id === doc)?.invoice ?? null;
}

// The states of invoices, receipts and reports, in words and tones: the one table the lists, the sheets and Plăți read
// (docs/copy.md, "Stări"). The engine decides every state; these only name it. Green ("ok") only for what evidence proves.

export type Hold = "" | "statement";

const HELD: Record<Hold, string> = {
  statement: "Lipsește extrasul lunii trecute",
  "": "De plătit",
};

/** Where a linked invoice's or a payment's payment is, in words. `hold` says why a held one waits; a held one is
 * still to pay. Imported is paid on the person's word only: it waits for the statement. An unknown state shows as itself. */
export function linkedState(state: Invoice["payment_state"], hold: Hold = "", today?: string): string {
  switch (state) {
    case "held": {
      if (hold !== "statement" || !today) return HELD[hold] ?? HELD[""];
      return `Lipsește extrasul din ${luna(previousMonth(today))}`;
    }
    case "in_file":
      return "În fișierul de plăți";
    case "lost":
      return "În fișierul de plăți, import neconfirmat";
    case "imported":
      return "Importată în bancă, se așteaptă extrasul";
    default:
      return state ?? "";
  }
}

/** Primite: still to pay, as the engine counts it in `payable`: open, or in the payment file but not imported yet.
 * An imported payment is paid by the user and only waits for the statement. */
export const owed = (r: Invoice) => r.status === "open" || (r.status === "linked" && r.payment_state !== "imported");

/** A received invoice: its tone, its state and the detail beside it (its due date, the day it was paid). "Plătită", in green,
 * only with the engine's `proved`; paid on a person's word alone (marked, or any paid without proof) stays "În curs de
 * confirmare" until a statement or a mail proves it. */
/** A marking's method in a list's words: "Marcată ca plătită, cu cardul". */
const PAID_HOW: Record<PaidMethod, string> = {
  bank: "din contul firmei", card: "cu cardul", cash: "numerar", own: "din fonduri proprii", other: "prin altă metodă",
};

export function receivedState(r: Invoice, today: string, hold?: Hold): [Tone, string, string] {
  if (r.credit_note) return ["", "Factură de stornare, neasociată", ""];
  if (r.status === "skipped") return ["", r.credits?.length ? "Stornată" : "Nu se mai plătește", dm(r.paid_at, today)];
  if (r.status === "paid" && r.proved) return ["ok", "Plătită", dm(r.paid_at, today)];
  if (r.status === "marked" && r.paid_method) return ["", "Marcată ca plătită", PAID_HOW[r.paid_method]];
  if (r.status === "paid" || r.status === "marked") return ["", PAID_AWAITING, dm(r.paid_at, today)];
  if (r.status === "linked") {
    // In a named file, before its import: the file is where it is (held or exported alike).
    const named = r.payment_file && (r.payment_state === "held" || r.payment_state === "in_file") ? `În fișierul ${r.payment_file.name}` : "";
    return [r.overdue ? "late" : "", named || linkedState(r.payment_state, hold, today), ""];
  }
  if (r.status !== "open") return ["", r.status, ""]; // a status this app doesn't know yet shows as itself
  const [c, due] = dueText(r.due_date, today);
  return [c, "De plătit", due];
}

const ISSUED_STATE: Record<Exclude<Issued["status"], "open">, [Tone, string]> = {
  collected: ["ok", "Încasată"], marked: ["", PAID_AWAITING], credited: ["", "Stornată"],
};

/** An issued invoice, as `receivedState`. A storno invoice reads "Factură de stornare", and names the invoice it reverses
 * (`original`, its number) where it is known: its sheet. Only the reversed invoice reads "Stornată". A status this app
 * doesn't know yet shows as itself. */
export function issuedState(r: Issued, today: string, original?: string | null): [Tone, string, string] {
  if (r.storno_of) return ["", original ? `Factură de stornare pentru ${original}` : "Factură de stornare", ""];
  if (r.status === "open") {
    const [c, due] = dueText(r.due_date, today);
    return [r.days_overdue > 0 ? "late" : c, "De încasat", due];
  }
  const [c, label] = ISSUED_STATE[r.status] ?? ["", r.status];
  return [c, label, r.status === "credited" ? "" : dm(r.collected_on ?? r.collected_at, today)];
}

/** Still owed by the client: the same rule as the engine's `api.issued_owed` (an invoice SPV refused isn't owed). */
export const issuedOwed = (r: Issued): boolean => r.status === "open" && !r.rejected;

/** A receipt: the invoice of the same purchase, as the engine groups them (`invoiceOf`), or none. */
export const receiptState = (invoice: Invoice | null): string => invoice ? `Cu factura ${invoice.number}` : "Fără factură";

/** The accountant's pack for a month: not ready yet (its statement isn't in), ready to prepare, prepared and waiting to be
 * sent (by a person, from their mail), sent. The steps read like "De plătit": what is left to do. */
export const REPORT_LABEL: Record<ReportStatus, [Tone, string]> = {
  preparing: ["", "În lucru"], ready: ["soon", "De pregătit"], prepared: ["soon", "De trimis"], sent: ["ok", "Trimis"],
};

/** A month in a word: "2 de rezolvat" while the engine counts anything open in it (`counts.open`, the rows of De
 * rezolvat), otherwise its pack's state. A sent month stays "Trimis". */
export function monthLabel(r: Report): [Tone, string] {
  const open = r.counts?.open ?? 0;
  return open && r.status !== "sent" ? ["soon", `${open} de rezolvat`] : REPORT_LABEL[r.status];
}

/** A debit that needs a document, named by how it left the account. */
export const DEBIT_KIND: Record<CardPayment["kind"], string> = { card: "Plată cu cardul", transfer: "Transfer" };

/** A month that has ended (its statement is due). */
export const past = (s: State, r: Report) => r.month < s.today.slice(0, 7);

/** "Mai lipsesc: 2 facturi, o factură din e-mail de verificat", or "" */
export function missing(s: State, r: Report): string {
  const out = [];
  if (past(s, r) && !r.bank?.statement) out.push("extrasul");
  const n = r.counts ? r.counts.card - r.counts.matched : r.bank?.card.filter((t) => !t.doc).length ?? 0;
  if (n) out.push(nOf(n, "o factură", "facturi"));
  const mails = r.counts?.to_check ?? r.to_check.length;
  if (mails) out.push(nOf(mails, "o factură din e-mail de verificat", "facturi din e-mail de verificat"));
  return out.length ? `Mai lipsesc: ${phrase(...out)}` : "";
}

/** The site's state signs, drawn next to a state's words: "=" matched (paid, collected, found in a statement), "≈" almost
 * (marked paid, or in the payment file, until the statement or a mail proves it), "≠" doesn't match (a refusal, totals that
 * differ). A state that is none of these (due, late, missing, unreadable) gets no sign. */
export type Sign = "=" | "≈" | "≠";

/** A received invoice: paid and proved (a statement, a payment mail or its till receipt, the engine's `proved`) is "="; paid
 * without proof reads "În curs de confirmare" like a marked one, and both are "≈", as is one in the payment file and not overdue. An overdue one keeps its red badge, and a lost file ("Fișierul a fost importat?") or a held payment is a question,
 * not an almost. */
export function receivedSign(r: Invoice): Sign | "" {
  if (r.status === "paid" || r.status === "marked") return r.status === "paid" && r.proved ? "=" : "≈";
  return r.status === "linked" && !r.overdue && r.payment_state !== "held" && r.payment_state !== "lost" ? "≈" : "";
}

/** An issued invoice: collected "=", collected but awaiting confirmation "≈", refused by SPV "≠"; an overdue one keeps its
 * red badge. */
export const issuedSign = (r: Issued): Sign | "" =>
  r.rejected ? "≠" : r.days_overdue > 0 ? "" : r.status === "collected" ? "=" : r.status === "marked" ? "≈" : "";

/** A month's statement: totals that agree "=", totals that differ "≠"; unread or not checked yet, none. */
export const bankSign = (b: Bank | null): Sign | "" =>
  !b?.statement || b.error ? "" : b.balanced ? "=" : b.balanced === false ? "≠" : "";

const UNREAD: Partial<Record<NonNullable<Bank["error"]>, string>> = {
  missing: "Primit, dar fișierul lipsește de pe server", password: "Primit, dar nu se deschide cu parola firmei",
  format: "Primit, dar formatul nu poate fi citit" };

/** A month's statement in words, and its tone. */
export function bankLine(b: Bank | null): [string, "" | "ok" | "late"] {
  if (!b || !b.statement) return ["Extrasul nu a sosit încă", ""];
  if (b.error) return [UNREAD[b.error] ?? "Primit, dar nu poate fi citit", "late"];
  return b.balanced ? ["Primit. Totalurile se potrivesc", "ok"] : b.balanced === false ? ["Primit. Diferențe de verificat", "late"]
    : ["Primit, neverificat încă", ""];
}

/** The month pack ready to compose, if any. */
export const readyReport = (s: State) => s.reports.find((r) => r.status === "ready");

/** ANAF's connection is about to expire: the engine calls it authorized and not expired (`status`), and its authorization
 * ends within 30 days, the engine's first expiry reminder. Whether it is set up, expired or failing is the engine's `status`. */
export const anafExpiringSoon = (a: State["system"]["anaf"]) =>
  (a.status === "ok" || a.status === "error") && a.authorized && a.days_left !== null && a.days_left <= 30;

/** Search text: lower case, without diacritics ("Șantier" finds "santier"). */
export const fold = (s: string) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

/** Evidence labels are shared by expense rows and every expense sheet. */
const SOURCE_LABELS: Record<ExpenseSource, string> = {
  efactura: "e-Factura", mail: "e-mail", upload_link: "bon încărcat",
  upload: "încărcat manual", history: "istoric",
};
export function sourceText(sources: ExpenseSource[] = []): string {
  return sources.map((code) => SOURCE_LABELS[code]).join(", ");
}
export function expenseSource(s: State, field: "invoice" | "document", id: string): string {
  return sourceText(s.expenses.find((e) => e[field] === id)?.sources) || "-";
}
