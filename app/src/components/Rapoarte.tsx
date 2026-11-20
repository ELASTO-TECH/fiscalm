"use client";
import { Actions, Panel, formTypography, rowTextStyle, sectionHeadingStyle, rowStyle, backLinkStyle, leadStyle, noteLeftStyle, statusStyle, statusTextStyle } from "./Controls";
// Rapoarte: one report per company per month (the engine's `reports`), the list and one month's detail.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { getBrowse, getReportPages } from "@/lib/api";
import { andList, cap, curOf, dm, dmt, lei, luna, lunaName, nOf, nonzero, phrase } from "@/lib/format";
import type { Report, ReportScreen, ReportSectionRow, ReportPages, State, Turnover } from "@/lib/types";
import { useApp, useCompany } from "./context";
import { DEBIT_KIND, REPORT_LABEL, bankLine, bankSign, missing, monthLabel, reportPurchases } from "./derive";
import { CardRow, IssuedRow, ReceivedRow } from "./Facturi";
import { PackButtons, ResolveBtns, ambiguousBody } from "./Home";
import { PageNav, usePage } from "./Paging";
import { MailAnswer, questionLine } from "./Tabs";
import { Empty, I, L, Row, Summary, Sums, Title } from "./ui";

/** "3 cheltuieli, o factură emisă, extras verificat, 5 din 6 tranzacții asociate". Cheltuieli counts purchases, as the
 * month's list shows them: an invoice and its bon are one. */
function checks(s: State, r: Report): string {
  const out = [nOf(r.counts?.purchases ?? reportPurchases(s, r).length, "o cheltuială", "cheltuieli"),
    nOf(r.counts?.issued ?? r.issued.length, "o factură emisă", "facturi emise")];
  const b = r.bank;
  if (b?.error) out.push("Extras necitit");
  else if (b?.statement) out.push(b.balanced ? "Extras verificat" : b.balanced === false ? "Extras cu diferențe" : "Extras primit");
  const card = r.counts?.card ?? b?.card.length ?? 0;
  if (card) out.push(`${r.counts?.matched ?? b!.card.filter((t) => t.doc).length} din ${nOf(card, "1 tranzacție asociată", "tranzacții asociate")}`);
  return cap(phrase(...out));
}

/** The month's word (`monthLabel`), or with `pack` its accountant pack's state. */
function StatusPill({ r, pack }: { r: Report; pack?: boolean }) {
  const [c, t] = pack ? REPORT_LABEL[r.status] : monthLabel(r);
  return <span className={`st ${c} ${statusTextStyle} ${statusStyle[c]}`}>{t}</span>;
}

export function Rapoarte() {
  const s = useCompany();
  const { route } = useApp();
  const open = s.reports.find((r) => r.month === route.month);
  if (open) return <ReportDetail s={s} r={open} />;
  return (
    <div className={`w-full min-w-0 ${formTypography}`}>
      <Title>Rapoarte</Title>
      <Panel padded={false} className="mt-5!">
        {s.reports.map((r) => {
          const gap = missing(s, r);
          return (
            <L key={r.month} to={{ tab: "rapoarte", month: r.month }} className={`${rowStyle} hover:bg-inset focus-visible:outline-2 focus-visible:outline-focus`}>
              <span className={rowTextStyle}>
                <span className="flex flex-wrap items-center justify-between gap-2"><b>{cap(luna(r.month))}</b><StatusPill r={r} /></span>
                <small>{checks(s, r)}</small>
                {gap ? <small className="text-wait">{gap}</small> : null}
              </span>
              <I n="caret-right" />
            </L>
          );
        })}
      </Panel>
      <Invoiced s={s} />
    </div>
  );
}

/** One amount of the table and, under it, the part that came from invoices before fiscalm. */
function Cell({ t, side }: { t: Turnover; side: "sales" | "expenses" }) {
  return <td><Sums s={t[side]} />{nonzero(t.history[side]) ? <small className="block text-xs text-muted">din istoric <Sums s={t.history[side]} /></small> : null}</td>;
}

/** What was invoiced per year and month (the engine's `turnover`), history included and marked: never what is owed, paid
 * or explained, which the months above say. */
function Invoiced({ s }: { s: State }) {
  const years = s.turnover.years;
  if (!years.length) return null;
  return <section aria-labelledby="pe-ani">
    <div className={sectionHeadingStyle}><h2 id="pe-ani">Facturat pe ani</h2></div>
    <Panel className="card">
      {years.map((y, i) => <details key={y.year} open={i === 0} className="[&+&]:mt-3">
        <Summary>{y.year}</Summary>
        {/* Amounts stay whole (Sums), so a narrow phone scrolls the table inside its card, never the page. */}
        <div className="overflow-x-auto"><table className="mt-2 w-full border-collapse text-sm tabular-nums [&_td]:border-0 [&_td]:border-t [&_td]:border-solid [&_td]:border-line [&_td]:py-2 [&_td]:pl-3 [&_td]:text-right [&_td]:align-top [&_th]:border-0 [&_th]:py-2 [&_th]:text-left [&_th]:align-top [&_th]:font-normal [&_tbody_th]:border-t [&_tbody_th]:border-solid [&_tbody_th]:border-line [&_thead_th]:text-muted [&_thead_th+th]:text-right">
          <thead><tr><th scope="col">Luna</th><th scope="col">Vânzări</th><th scope="col">Cheltuieli</th></tr></thead>
          <tbody>
            <tr className="font-semibold"><th scope="row" className="font-semibold!">Total</th><Cell t={y.turnover} side="sales" /><Cell t={y.turnover} side="expenses" /></tr>
            {y.months.map((m) => <tr key={m.month}><th scope="row">{cap(lunaName(m.month))}</th><Cell t={m.turnover} side="sales" /><Cell t={m.turnover} side="expenses" /></tr>)}
          </tbody>
        </table></div>
      </details>)}
    </Panel>
  </section>;
}

function ReportDetail({ s, r }: { s: State; r: Report }) {
  const read = useCallback((_: string, signal?: AbortSignal) => getReportPages(s.co, r.month, signal), [s, r.month]);
  const scope = `${s.co}|${r.month}`;
  const initial = usePage(read, 0, scope);
  const [previous, setPrevious] = useState<{ scope: string; data: ReportPages["data"] }>({ scope, data: [] });
  if (initial.data.length && previous.data !== initial.data) setPrevious({ scope, data: initial.data });
  const pages = initial.data.length ? initial.data : previous.scope === scope ? previous.data : [];
  const scrolled = useRef("");
  useEffect(() => {
    const target = window.location.hash.slice(1);
    if (!pages.length || scrolled.current === scope || !["pachet", "rezolvat"].includes(target)) return;
    const frame = requestAnimationFrame(() => { document.getElementById(target)?.scrollIntoView(); scrolled.current = scope; });
    return () => cancelAnimationFrame(frame);
  }, [pages.length, scope]);
  return (
    <div className={`w-full min-w-0 ${formTypography}`}>
      <L to={{ tab: "rapoarte" }} className={backLinkStyle} label="Înapoi la rapoarte"><I n="arrow-left" />Rapoarte</L>
      <Title>Raport pentru {luna(r.month)}</Title>
      <p className={leadStyle}><StatusPill r={r} /></p>

      <PageNav page={initial} label="Rapoarte" />
      {pages.length ? <ReportContent s={s} r={r} initial={pages} /> : null}
    </div>
  );
}

function ReportContent({ s, r, initial }: { s: State; r: Report; initial: ReportPages["data"] }) {
  const b = r.bank;
  const [line, tone] = bankLine(b);
  return <>
      <ReportSection s={s} r={r} initial={initial} screen="report_tasks" label="De rezolvat" count id="rezolvat" ok empty />
      <ReportSection s={s} r={r} initial={initial} screen="report_received" label="Facturi primite" count empty />
      <ReportSection s={s} r={r} initial={initial} screen="report_issued" label="Facturi emise" count empty />
      <ReportSection s={s} r={r} initial={initial} screen="report_bank" label="Extras și plăți"
        before={<Row icon="bank" title="Extrasul de cont" sub={line} subcls={tone} badge={tone} sign={bankSign(b)} />} />
      <ReportSection s={s} r={r} initial={initial} screen="report_advances" label="Confirmări de plată" empty />

      <div className={sectionHeadingStyle} id="pachet"><h2>Dosar pentru contabil</h2><StatusPill r={r} pack /></div>
      <Panel padded={false}>
        {!(r.counts?.statements ?? r.docs.filter((x) => x.kind === "statement").length) && r.status !== "sent"
          ? <Row icon="bank" title="Extrasul de cont" sub="Dosarul se pregătește după sosirea extrasului." /> : null}
        <ReportSection s={s} r={r} initial={initial} screen="report_docs" label="Dosar pentru contabil" inline />
        <ReportSection s={s} r={r} initial={initial} screen="report_not_mailed" label="Nu se trimit contabilului" inline />
        {r.status === "sent" ? (
          <Row icon="paper-plane-tilt" title={`Dosarul pe ${luna(r.month).split(" ")[0]}`}
            sub={`Trimis${r.sent_on ? ", " + dm(r.sent_on, s.today) : ""}`} subcls="ok" badge="ok" />
        ) : null}
        {r.status === "ready" || r.status === "prepared" || r.status === "sent" ?
          <Actions className="btns mt-0! px-5 py-4"><PackButtons month={r.month} /></Actions> : null}
        {s.accountant.recipient ? <p className={`${noteLeftStyle} px-3 pb-4`}>Destinatar: {s.accountant.recipient}</p> : null}
      </Panel>
  </>;
}

/** Later pages refresh through browse; the first page intentionally remains the initial bundle until a state refresh. */
function ReportSection({ s, r, initial, screen, label, empty, ok, count, id, inline, before }: {
  s: State; r: Report; initial: ReportPages["data"]; screen: ReportScreen; label: string; empty?: boolean; ok?: boolean; count?: boolean; id?: string; inline?: boolean; before?: ReactNode;
}) {
  const first = initial.find((entry) => entry.screen === screen)!.page;
  const read = useCallback((cursor: string, signal?: AbortSignal) => cursor ? getBrowse(s.co, screen, { month: r.month, cursor }, signal) : Promise.resolve(first), [s.co, first, r.month, screen]);
  const page = usePage(read, 0, read, first);
  const contents = <>
    {before}<PageNav page={page} label={label} />
    {screen === "report_not_mailed" ? page.data.length ? <Row icon="file-text" title="Nu se trimit contabilului"
      sub={`Documentele de la ${andList(page.data.flatMap((x) => x.kind === "not_mailed" ? [x.data] : []))}`} /> : null
      : page.data.map((row) => <ReportRow key={row.id} row={row} s={s} r={r} />)}
    {!page.loading && !page.error && !page.total && empty ? <Empty ok={ok} /> : null}
  </>;
  return inline ? contents : <>
    <div className={sectionHeadingStyle} id={id}><h2>{label}</h2>{count ? <span>{page.total || ""}</span> : null}</div>
    <Panel padded={false} aria-busy={page.loading}>{contents}</Panel>
  </>;
}

function ReportRow({ row, s, r }: { row: ReportSectionRow; s: State; r: Report }) {
  const { today } = s;
  switch (row.kind) {
    case "purchase": return row.data.invoice ? <ReceivedRow r={row.data.invoice} today={today} both={!!row.data.document} />
      : row.data.document ? <CardRow d={row.data.document} today={today} /> : null;
    case "issued": return <IssuedRow r={row.data} today={today} />;
    case "card": { // unresolved ones only under De rezolvat; Extras și plăți lists the ones with their document
      const t = row.data;
      return <Row icon={t.kind === "card" ? "credit-card" : "bank"} title={t.who} sub={`${t.direction === "credit" ? "Încasare" : DEBIT_KIND[t.kind]}, ${dm(t.date, today)}${t.orig ? ", " + t.orig : ""}`}
        subcls={t.doc || t.awaiting ? "" : "soon"} val={`${lei(t.amount)} ${curOf(t.currency)}`} vsub={t.doc ?? (t.awaiting ? "Aștept factura" : t.direction === "credit" ? "Fără explicație" : "Lipsește factura")}
        badge={t.doc ? "ok" : ""} sign={t.doc ? "=" : ""} />;
    }
    case "bank": { // a statement still to come is a step to take; one that can't be read or doesn't add up is a failure
      const [line, tone] = bankLine(row.data);
      return <Row icon="bank" title="Extrasul de cont" sub={line} subcls={tone || "soon"} badge={tone || "soon"} sign={bankSign(row.data)} />;
    }
    case "advance": return <Row icon="user" title={`Avans de trezorerie, ${dm(row.data.date, today)}`} sub="Transfer din contul firmei, de decontat" val={`${lei(row.data.amount)} lei`} />;
    case "document": {
      const x = row.data, stm = x.kind === "statement";
      return <Row icon={stm ? "bank" : "file-text"} title={stm ? "Extrasul de cont" : x.vendor ?? ""}
        sub={stm ? "Primit" : [dm(x.date, today), x.note].filter(Boolean).join(" ")} />;
    }
    case "mail": case "ambiguous": return <ReportQuestion row={row} s={s} r={r} />;
    case "not_mailed": return null;
  }
}

/** A question of the month, answered here as on Acasă (both answers are local, allowed in Sandbox too); a debit that can't
 * be picked here opens its transaction. */
function ReportQuestion({ row, s, r }: { row: Extract<ReportSectionRow, { kind: "mail" | "ambiguous" }>; s: State; r: Report }) {
  const { today } = s;
  if (row.kind === "mail") {
    const m = row.data;
    return <div><Row icon="envelope-simple" title={m.supplier || m.subject} sub={`Din e-mail, ${dmt(m.at, today)}. ${questionLine(m)}`}
      to={{ tab: "rapoarte", month: r.month, mail: m.key }} />
      <div className="px-5 pb-4"><MailAnswer m={m} /></div></div>;
  }
  const a = row.data;
  return <div><Row icon="bank" title="Plată de lămurit" sub={ambiguousBody(a, today)} subcls="soon" val={`${lei(a.amount)} ${curOf(a.currency)}`}
    to={a.can_resolve ? undefined : { tx: a.tx }} />
    {a.can_resolve ? <div className="px-5 pb-4"><ResolveBtns a={a} today={today} /></div> : null}</div>;
}
