"use client";
import { Button, Panel, Skeleton, bannerStyle, filterLinkStyle, sectionHeadingStyle } from "./Controls";
// Tranzacții (formerly Bancă): every transaction of the company's accounts, newest first, with what explains it, opening on
// De explicat; Încarcă beside the title; the accounts and the monthly statements beside the list (below it on a phone).
// The engine decides every explanation and proposal (`transactions`); the app only names them and links to what they point at.
import { useCallback, useState } from "react";
import { getBrowse } from "@/lib/api";
import { ListCount, ListTools, PageNav, usePage } from "./Paging";
import { cap, curOf, dm, docName, lei, luna, nOf, phrase, type Tone } from "@/lib/format";
import type { Route } from "@/lib/nav";
import type { CompanyEntry, State, Transaction, TransactionSaid } from "@/lib/types";
import { Accounts, UploadMenu } from "./Accounts";
import { HeldMoney } from "./Dashboard";
import { LABEL_NAME, keptText } from "./TxSheet";
import { useCompanies } from "./Companies";
import { useApp, useCan, useCompany } from "./context";
import { bankLine, bankSign, fold } from "./derive";
import { Empty, L, Row, Title } from "./ui";

const FILTERS: [Route["f"], string][] = [["", "De explicat"], ["all", "Toate"], ["in", "Intrări"], ["out", "Ieșiri"]];

type Said = TransactionSaid;

/** How a transaction's explanation (or, without one, the engine's proposal) reads, and where it leads. */
export function said(t: Transaction, s: State): Said | null {
  const inv = (id: string) => s.invoices.find((r) => r.id === id);
  const iss = (id: string) => s.issued.find((r) => r.id === id);
  const doc = (id: string | null) => (id ? s.documents.find((d) => d.id === id) : undefined);
  const e = t.explanation;
  // the user's link that leaves part of its one invoice owed says so
  const partly = e && "link" in e && e.link?.parts.some((p) => !p.closed) ? " parțială" : "";
  if (e?.kind === "payment") {
    const r = e.invoices.length === 1 ? inv(e.invoices[0]) : undefined;
    if (r) return { text: partly ? `Plata parțială a facturii ${r.number}` : `Plata facturii ${r.number}`, to: { inv: r.id } };
    if (e.invoices.length > 1) return { text: `Plata a ${nOf(e.invoices.length, "o factură", "facturi")}` };
    const p = e.payments[0];
    return { text: phrase("Plată din fișierul de plăți", p?.details || p?.ref), to: { tab: "plati" } };
  }
  if (e?.kind === "collection") {
    const set = e.settlement ? s.settlements?.find((x) => x.id === e.settlement) : undefined;
    if (set) return { text: `Decontare ${set.payer.name}, ${nOf(e.issued.length, "o factură", "facturi")}`, to: { set: set.id } };
    const r = e.issued.length === 1 ? iss(e.issued[0]) : undefined;
    if (r) return { text: partly ? `Încasarea parțială a facturii ${r.number}` : `Încasarea facturii ${r.number}`, to: { iss: r.id } };
    return { text: `Încasarea a ${nOf(e.issued.length, "o factură", "facturi")}` };
  }
  if (e?.kind === "receipt") {
    const d = doc(e.document);
    const r = e.invoices.length === 1 ? inv(e.invoices[0]) : undefined;
    if (d) return { text: d.vendor ? `Bon de la ${d.vendor}` : docName(d, s.today), to: { doc: d.id } };
    if (r) return { text: `Bon pentru factura ${r.number}`, to: { inv: r.id } };
    return { text: "Bon" };
  }
  if (e?.kind === "label") return { text: `Fără document, ${LABEL_NAME[e.label].toLowerCase()}` };
  if (e?.kind === "advance") return { text: `Avans, ${e.who}` };
  if (e?.kind === "transfer") return { text: "Transfer între conturi" };
  const p = t.proposal;
  if (p?.kind === "invoices") {
    return { text: `Se potrivește cu ${nOf(p.invoices.length, "o factură", "facturi")}`, to: { tab: "sarcini" }, proposal: true };
  }
  if (p?.kind === "settlement") return { text: "Decontare de verificat", to: { set: p.settlement }, proposal: true };
  if (p?.kind === "remembered") {
    return { text: `Regulă salvată: ${keptText(p.as, s.partners.find((x) => x.id === p.as.partner)).toLowerCase()}`, proposal: true };
  }
  if (p?.kind === "receipt") {
    const d = doc(p.document);
    return { text: d ? `Poate fi bonul de la ${d.vendor}` : "Poate fi un bon", to: d ? { doc: d.id } : undefined, proposal: true };
  }
  return null;
}

/** One transaction: who (the bank's text), what explains it or "De explicat", the signed amount and the account. One the
 * engine explained opens what explains it, a credit a Decontare waits on opens it; one De explicat or explained by the user
 * opens its own sheet. */
export function TxRow({ t, s, description }: { t: Transaction; s: State; description?: TransactionSaid | null }) {
  const what = description === undefined ? said(t, s) : description;
  const credit = t.direction === "credit";
  const who = t.counterparty || t.details || (credit ? "Intrare în cont" : "Ieșire din cont");
  const tone: Tone = t.explanation ? "ok" : "";
  const sub = phrase(what && !what.proposal ? what.text : t.awaiting ? "Aștept factura" : "De explicat", what?.proposal ? what.text : "",
    dm(t.date, s.today));
  const orig = t.orig ? `${lei(t.orig.amount)} ${curOf(t.orig.currency)}` : "";
  return (
    <Row icon={credit ? "arrow-down-left" : "arrow-up-right"} title={who} sub={sub} subcls={tone} badge={tone}
      val={`${credit ? "+" : "−"}${lei(t.amount)} ${curOf(t.currency)}`}
      vsub={phrase(orig, t.account ? `cont ${t.account}` : "")} cls="tx"
      to={(t.explanation && !("link" in t.explanation && t.explanation.link)) || (t.proposal?.kind === "settlement" && !t.can_link) ? what?.to : { tx: t.id }} />
  );
}

export function Tranzactii() {
  const s = useCompany();
  const { route, go, view } = useApp();
  const [query, setQuery] = useState("");
  // a month asked for in the link is kept even without transactions: the list then says so
  const month = /^\d{4}-(0[1-9]|1[0-2])$/.test(route.month) ? route.month : "";
  const q = fold(query.trim());
  const filter = route.f === "" || route.f === "in" || route.f === "out" ? route.f : "all";
  const read = useCallback((cursor: string, signal?: AbortSignal) => getBrowse(s.co, "tranzactii", { cursor, f: filter, month, from: route.from, to: route.to, q }, signal), [s, filter, month, route.from, route.to, q]);
  const page = usePage(read, q ? 200 : 0, s.co, undefined, `${s.co}|${filter}|${month}|${route.from}|${route.to}|${q}`);
  const months = [...new Set([...page.months, ...(month ? [month] : [])])].sort().reverse();
  const pick = (m: string) => go({ co: route.co, tab: "tranzactii", f: route.f, month: m }, { replace: true });
  const companies = useCompanies();
  const c = companies.data?.companies.find((x) => x.key === s.co);
  const manage = useCan("manage_firms") && view.can_manage_companies === true;
  const unfiltered = route.f === "" && !q && !month && !route.from && !route.to;
  return (
    <div className="grid min-w-0 grid-cols-1 gap-x-6 xl:grid-cols-[minmax(0,1fr)_300px]">
      <div className="col-span-full flex flex-wrap items-center justify-between gap-3">
        <Title>Tranzacții</Title>
        {manage && c ? <UploadMenu key={c.key} c={c} accounts={c.accounts ?? []} reload={companies.reload} />
          : manage && !companies.data && !companies.failed ? <Skeleton className="h-11 w-28 rounded-full min-[1100px]:h-[38px]" /> : null}
      </div>
      <div className="min-w-0">
        <nav className="my-4 flex flex-wrap gap-1" aria-label="Filtru">
          {FILTERS.map(([f, label]) => (
            <L key={f} to={{ tab: "tranzactii", f, month }} className={filterLinkStyle} current={route.f === f}>
              {label}{f === "" && s.transactions.to_explain ? <>{" "}<small className="n">{s.transactions.to_explain}</small></> : null}
            </L>
          ))}
        </nav>
        <ListTools month={month} months={months} onMonth={pick} query={query} onQuery={setQuery} label="Caută nume, sumă sau factură" />
        <Panel padded={false} aria-busy={page.loading} className="card rounded-panel!">
          <ListCount page={page} text={cap(nOf(page.total, "o tranzacție", "tranzacții"))} />
          <PageNav page={page} />
          <div className="divide-x-0 divide-y divide-solid divide-line/60">
            {page.data.map((row) => <TxRow key={row.id} t={row.transaction} s={s} description={row.said} />)}
          </div>
          {!page.loading && !page.error && !page.total ? <Empty ok={!!page.months.length && unfiltered}
            action={!page.months.length && unfiltered && manage && c ? <UploadMenu key={c.key} c={c} accounts={c.accounts ?? []} reload={companies.reload} label="Încarcă extrasul" /> : null} /> : null}
        </Panel>
      </div>
      <aside className="min-w-0" aria-label="Conturi și extrase">
        <CompanyAccounts {...companies} c={c} manage={manage} />
        <Statements s={s} />
        <HeldMoney />
      </aside>
    </div>
  );
}

/** The company's accounts (api/companies), the same read as Încarcă. */
function CompanyAccounts({ data, failed, reload, c, manage }: ReturnType<typeof useCompanies> & { c: CompanyEntry | undefined; manage: boolean }) {
  const s = useCompany();
  // as Firme: a failed read keeps what was shown and says it may be old, with a retry
  const banner = failed ? (
    <div className={bannerStyle} role="alert">
      <b>{data ? "Lista nu a putut fi actualizată. Datele afișate pot fi vechi." : "Conturile nu au putut fi încărcate."}</b>
      <Button type="button" className="btn g" onClick={reload}>Reîncearcă</Button>
    </div>
  ) : null;
  return (
    <>
      {banner}
      {c?.accounts ? <Accounts c={c} accounts={c.accounts} manage={manage} balances={s.dashboard.balances} /> : null}
    </>
  );
}

/** The statements of the months before this one (the engine's reports carry them), as Bancă showed them. */
function Statements({ s }: { s: State }) {
  const months = s.reports.filter((r) => r.bank).map((r) => ({ month: r.month, v: r.bank! }));
  return (
    <>
      <div className={sectionHeadingStyle}><h2 id="h-extrase">Extrase lunare</h2></div>
      <Panel padded={false}>
        {months.length ? months.map(({ month, v }) => {
          const name = cap(luna(month));
          if (v.error) return <Row key={month} icon="bank" title={name} sub={bankLine(v)[0]} subcls="late" />;
          if (!v.statement) return <Row key={month} icon="bank" title={name} sub="Extrasul nu a sosit încă" />;
          const [c, t]: [Tone, string] = v.balanced ? ["ok", "Totalurile se potrivesc"]
            : v.balanced === false ? ["late", "Diferențe de verificat"] : ["", "Neverificat încă"];
          return <Row key={month} icon="bank" title={name} sub={`Primit. ${t}`} subcls={c} badge={c} sign={bankSign(v)} />;
        }) : <Empty />}
      </Panel>
    </>
  );
}
