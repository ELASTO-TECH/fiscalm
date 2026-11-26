"use client";
import { ExpenseUploadButton } from "./ExpenseUpload";
import { Panel, filterLinkStyle, pageTabStyle, pageTabsStyle } from "./Controls";
// Cheltuieli (received invoices, and receipts), Vânzări (issued invoices) and Parteneri, for one company or all of them.
import { useCallback, useState } from "react";
import { cap, curOf, dm, docAmount, docName, lei, nOf, phrase } from "@/lib/format";
import type { Route, Tab } from "@/lib/nav";
import { isAll, parts, type BrowsePurchase, type CardInvoice, type HistoryInvoice, type Invoice, type Issued } from "@/lib/types";
import { useApp, useCan } from "./context";
import { getBrowse, historyDocument } from "@/lib/api";
import { ListCount, ListTools, PageNav, usePage } from "./Paging";
import { sourceText, REFUSED_WHY, issuedOwed, issuedSign, issuedState, owed, receiptState, receivedSign, receivedState, refusalOf, refusedNow } from "./derive";
import { PickRow, PickedBar, usePicked, type Picked } from "./PayPick";
import { NewPartnerButton, PartnerList } from "./Partners";
import { DraftList } from "./Invoicing";
import { spvProblem, spvTestLabel } from "./Issued";
import { NewInvoice } from "./Tabs";
import { Company } from "./context";
import { Empty, I, L, Row, Title, tableColumns } from "./ui";

type Page = Extract<Tab, "cheltuieli" | "vanzari" | "parteneri"> | "bonuri";
const FILTERS: Record<Page, [Route["f"], string][]> = {
  cheltuieli: [["", "Toate"], ["open", "De plătit"], ["late", "Restante"]],
  bonuri: [["", "Toate"], ["open", "Fără factură"]],
  vanzari: [["", "Toate"], ["open", "De încasat"], ["late", "Restante"]],
  parteneri: [["", "Toți"], ["open", "Cu sold"], ["late", "Cu restanțe"]],
};
const TITLE = { cheltuieli: "Cheltuieli", vanzari: "Vânzări", parteneri: "Parteneri" };
/** A list's count over its table (Contra's "2 invoices"), and its columns from 1100px (ui.tsx tableColumns). */
const COUNTED: Record<Exclude<Page, "parteneri">, [string, string]> = {
  vanzari: ["o factură", "facturi"], cheltuieli: ["o cheltuială", "cheltuieli"], bonuri: ["un document", "documente"],
};
/** The fourth column holds the number, and under Toate firmele the firm before it. */
const COLUMNS: Record<Exclude<Page, "parteneri">, [string, string, string]> = {
  vanzari: ["Client", "Stare", "Suma"], cheltuieli: ["Furnizor", "Stare", "Suma"], bonuri: ["Document", "Detalii", "Suma"],
};
const columnsOf = (screen: Exclude<Page, "parteneri">, tag: boolean) => {
  const [name, state, sum] = COLUMNS[screen];
  return [name, state, tag ? "Firma, număr" : "Număr", sum];
};

/** Marks a purchase the engine proves by both its invoice and its bon: one row, in both of Cheltuieli's categories. */
export const BOTH = "Factură și bon";
/** The state of an invoice from before fiscalm (docs/copy.md): no payment state. */
export const HISTORY = "Din istoric";

/** A received e-Factura invoice: its state from `receivedState` (green only when the engine's `proved`), and its due date or
 * the day it was paid. `byNumber`: inside its
 * partner's sheet, where the partner's name would repeat, the invoice is named by its number and date. `both`: the
 * invoice's purchase also has its bon (the engine's `expenses`); the row is marked so and still opens the invoice. */
export function ReceivedRow({ r, today, tag, byNumber, both, still, source }: {
  r: Invoice; today: string; tag?: boolean; byNumber?: boolean; both?: boolean; still?: boolean; source?: string;
}) {
  const { view } = useApp();
  // Its company's state says why a held payment waits, as the sheet does.
  const co = parts(view).find((x) => x.co === r.company);
  const [c, label, detail] = receivedState(r, today);
  // An open invoice the engine keeps out of the payment file says so, as on Plăți; its sheet says why.
  const refused = co && r.status === "open" ? refusalOf(co, r.id) : null;
  return (
    <Row icon="receipt" title={byNumber ? `Nr. ${r.number}` : r.supplier} sub={phrase(label, detail, source)} subcls={c}
      val={`${lei(owed(r) && (Number(r.covered) || r.credits?.length) ? r.left : r.total)} ${curOf(r.currency)}`} vsub={byNumber ? dm(r.issue_date, today) : phrase(tag && r.company, `nr. ${r.number}`)}
      to={still ? undefined : { inv: r.id }} badge={r.overdue ? "late" : c === "soon" && owed(r) ? "soon" : ""} mark={both ? BOTH : refusedNow(refused) ? REFUSED_WHY[refused.why][0] : ""}
      sign={refusedNow(refused) ? "≠" : receivedSign(r)} />
  );
}

/** A document and its payment details. Only a receipt expecting an invoice may say "Fără factură". */
export function CardRow({ d, today, tag, alone, source }: { d: CardInvoice; today: string; tag?: boolean; alone?: boolean; source?: string }) {
  return (
    <Row icon="credit-card" title={docName(d, today)} sub={phrase(alone && d.document_type === "receipt" && receiptState(null), d.card ? `Card terminat în ${d.card}` : "", dm(d.date, today), source)}
      val={docAmount(d)} vsub={tag ? d.company : ""} to={{ doc: d.id }} />
  );
}

/** An invoice that came by e-mail (a supplier outside SPV): in Facturi, opened as its document. Its payment is matched
 * from the statement, not tracked here, so it is neither "De plătit" nor late. */
export function MailRow({ d, today, tag, source }: { d: CardInvoice; today: string; tag?: boolean; source?: string }) {
  return (
    <Row icon="envelope-simple" title={docName(d, today)} sub={phrase(source || "e-mail", d.card ? `Card terminat în ${d.card}` : "", dm(d.date, today))}
      val={docAmount(d)} vsub={phrase(tag && d.company, d.number ? `nr. ${d.number}` : "")} to={{ doc: d.id }} />
  );
}

/** An issued invoice: its state from `issuedState`, overdue in red, and an SPV upload that needs attention. An invoice sent to
 * ANAF Test says so, unless `noted`: in Vânzări the app-wide Sandbox banner already says it; then the row keeps
 * its own state and only a Test upload that needs attention. `byNumber` as for ReceivedRow. */
export function IssuedRow({ r, today, tag, byNumber, noted }: { r: Issued; today: string; tag?: boolean; byNumber?: boolean; noted?: boolean }) {
  // A storno's original is named on its sheet, which loads the pair; the list's page may not hold it.
  const [c, label, detail] = issuedState(r, today);
  const late = r.days_overdue > 0;
  const problem = spvProblem(r);
  return (
    <Row icon="invoice" title={byNumber ? `Nr. ${r.number}` : r.client} sub={r.spv?.environment === "test" ? phrase(label, detail, noted ? problem : spvTestLabel(r)) : problem || phrase(label, detail)}
      subcls={late || problem ? "late" : c}
      val={`${lei(issuedOwed(r) ? r.owed : r.total)} ${curOf(r.currency)}`}
      vsub={byNumber ? dm(r.issue_date, today) : phrase(tag && r.company, `nr. ${r.number}`)} to={{ iss: r.id }} badge={late || problem ? "late" : ""}
      sign={issuedSign(r)} />
  );
}

/** An invoice from before fiscalm (history import): listed by its date with no payment state, so it is never owed, late or
 * acted on. A PDF one opens its document for those who manage firms (the import's own permission). */
export function HistoryRow({ r, today, tag, byNumber }: { r: HistoryInvoice; today: string; tag?: boolean; byNumber?: boolean }) {
  const canOpen = useCan("manage_firms") && !!r.document;
  return (
    <Row icon={r.direction === "issued" ? "invoice" : "receipt"} title={byNumber ? `Nr. ${r.number}` : r.party_name || `Nr. ${r.number}`}
      sub={byNumber ? HISTORY : phrase(HISTORY, dm(r.issue_date, today))} badge="quiet"
      val={`${lei(r.total)} ${curOf(r.currency)}`} vsub={byNumber ? dm(r.issue_date, today) : phrase(tag && r.company, `nr. ${r.number}`)}
      file={canOpen && r.document ? historyDocument(r.company, r.document) : undefined} />
  );
}

/** Vânzări, Parteneri, or Cheltuieli in its two categories, one row per purchase (the engine's `expenses`): Facturi lists the
 * purchases with an invoice, Bonuri și chitanțe those with a receipt. A purchase with both is the same row, marked
 * "Factură și bon", in both categories; "Fără factură" leaves it out. */
export function Facturi() {
  const { route, view } = useApp();
  const tab = route.tab as keyof typeof TITLE;
  const page: Page = tab === "cheltuieli" && route.side === "bonuri" ? "bonuri" : tab;
  return (
    <div className="min-w-0 max-w-none">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <Title className="mb-0!">{TITLE[tab]}</Title>
        {tab === "cheltuieli" ? <ExpenseUploadButton /> : tab === "parteneri" ? <NewPartnerButton />
          : isAll(view) ? null : <NewInvoice className="btn primary"><I n="plus" />Factură nouă</NewInvoice>}
      </div>
      {tab === "cheltuieli" ? (
        <nav className={pageTabsStyle} aria-label="Cheltuieli">
          {([["", "Facturi"], ["bonuri", "Bonuri și chitanțe"]] as const).map(([side, label]) => (
            <L key={side} to={{ tab, side }} className={pageTabStyle} current={route.side === side}>{label}</L>
          ))}
        </nav>
      ) : null}
      <nav className="my-4 flex flex-wrap gap-1" aria-label="Filtru">
        {FILTERS[page].map(([f, label]) => (
          <L key={f} to={{ tab, side: route.side, f }} className={filterLinkStyle} current={route.f === f}>{label}</L>
        ))}
      </nav>
      {page === "parteneri" ? <PartnerList /> : <InvoiceList screen={page} />}
    </div>
  );
}

function InvoiceList({ screen }: { screen: Exclude<Page, "parteneri"> }) {
  const { view, route } = useApp();
  const [month, setMonth] = useState("");
  const [query, setQuery] = useState("");
  const read = useCallback((cursor: string, signal?: AbortSignal) => getBrowse(view.co, screen, { cursor, q: query, f: route.f, month }, signal), [view, screen, query, route.f, month]);
  const page = usePage(read, query ? 200 : 0, `${view.co}|${screen}`, undefined, `${view.co}|${screen}|${route.f}|${query}|${month}`);
  const ss = parts(view), tag = ss.length > 1;
  // De plătit and Restante of one firm: each invoice can be chosen, to mark paid or put in the payment file.
  const sel = usePicked(`${view.co}|${route.f}`, page.data.flatMap((r) => "invoice" in r && r.invoice ? [r.invoice] : []));
  const pick = screen === "cheltuieli" && !tag && (route.f === "open" || route.f === "late") ? sel : undefined;
  return <>
    {screen === "vanzari" && !tag ? <Company s={ss[0]}><DraftList /></Company> : null}
    <ListTools month={month} months={page.months} onMonth={setMonth} monthLabel="Luna emiterii"
      query={query} onQuery={setQuery} label="Caută firmă sau număr" />
    {pick ? <PickedBar key={pick.place} sel={pick} /> : null}
    <Panel padded={false} aria-busy={page.loading} className={`card tbl rounded-panel! ${pick?.list.length ? "mb-24" : ""}`}>
      <ListCount page={page} text={cap(nOf(page.total, ...COUNTED[screen]))} />
      {(page.total || page.loading) && !pick ? <div className={`hidden min-h-[42px] items-center gap-x-4 border-0 border-b border-solid border-line px-4 text-[13px] text-muted min-[1100px]:grid ${tableColumns}`} aria-hidden="true">
        <span />{columnsOf(screen, tag).map((c, i) => <span key={c} className={i === 3 ? "text-right" : ""}>{c}</span>)}
      </div> : null}
      <PageNav page={page} />
      <div className="divide-x-0 divide-y divide-solid divide-line/60">
        {page.data.map((r) => "kind" in r ? <HistoryRow key={r.id} r={r} today={view.today} tag={tag} />
          : "invoice" in r ? <PurchaseRow key={r.id} r={r} today={view.today} tag={tag} pick={pick} />
          : <IssuedRow key={`e${r.id}`} r={r} today={view.today} tag={tag} noted />)}
      </div>
      {!page.loading && !page.error && !page.total ? <Empty action={!tag && !route.f && !query && !month ? <FirstStep screen={screen} /> : null} /> : null}
    </Panel>
  </>;
}

/** The first thing to do on an empty Vânzări or Cheltuieli of one company. */
function FirstStep({ screen }: { screen: Exclude<Page, "parteneri"> }) {
  const member = useCan("member");
  if (screen !== "vanzari") return <ExpenseUploadButton />;
  return member ? <L to={{ new: "1" }} className="btn"><I n="plus" />Emite prima factură</L> : null;
}

/** A purchase in Cheltuieli. `pick`: its invoice has a box (De plătit, and the same list in Facturi de plătit, where
 * `still` keeps the row from opening the invoice over the sheet: all of it toggles the box). */
export function PurchaseRow({ r, today, tag, pick, still = false }: { r: BrowsePurchase; today: string; tag: boolean; pick?: Picked; still?: boolean }) {
  if (r.invoice) {
    const row = <ReceivedRow r={r.invoice} today={today} tag={tag} both={!!r.document} still={still} source={sourceText(r.sources)} />;
    return pick ? <PickRow r={r.invoice} sel={pick} whole={still}>{row}</PickRow> : row;
  }
  return r.document?.document_type === "invoice" ? <MailRow d={r.document} today={today} tag={tag} source={sourceText(r.sources)} />
    : r.document ? <CardRow d={r.document} today={today} tag={tag} alone source={sourceText(r.sources)} /> : null;
}
