"use client";
// Acasă's dashboard (docs/information-architecture.md, section 4): how the firm is doing. Every number is the engine's
// (`dashboard`, docs/openapi.json); the app only draws it (Charts.tsx), says when a number is not proven yet (a month without
// complete statements, VAT estimated from the recorded invoices), and every number opens the list it counts.
import { filterLinkStyle, sectionHeadingStyle, surfaceStyle } from "./Controls";
import { useCallback, useId, type ReactNode } from "react";
import { getDashboardPage } from "@/lib/api";
import { PageNav, usePage } from "./Paging";
import { cap, cents, curOf, fromCents, dm, dueText, lei, luna, lunaName, nOf, sums } from "@/lib/format";
import type { Route } from "@/lib/nav";
import { coName, isAll, parts, type AgingKey, type Aging as AgingData, type ChartMonth, type Dashboard as Dash, type DashboardCollection, type DashboardRows, type Money, type Page, type Period, type State, type Sums, type TopPartner, type Upcoming, type VatMonth } from "@/lib/types";
import { useApp } from "./context";
import { ProductSales } from "./ProductSales";
import { AreaChart, BarList, ColumnChart, Legend, SegmentBar, type Column, type Tone } from "./Charts";
import { Big, Empty, I, L, Row, Sums as SumsText } from "./ui";

/** The period in the URL (`per`), the engine's name for it, its label, and how many months the chart draws (the year:
 * January to today, at least three). */
const PERIODS: [Route["per"], Period, string, number][] = [
  ["", "month", "Luna curentă", 3], ["3luni", "quarter", "3 luni", 3], ["6luni", "half_year", "6 luni", 6],
  ["12luni", "trailing_year", "12 luni", 12], ["an", "year", "Anul", 0],
];
const periodRow = (per: Route["per"]) => PERIODS.find(([p]) => p === per) ?? PERIODS[0];
export const periodOf = (per: Route["per"]): Period => periodRow(per)[1];

/** Children of the dashboard that span its columns in a wide window. */
const span = "min-[1100px]:col-span-full";
const card = `${surfaceStyle} p-4 min-[1100px]:p-5`;
const cardHead = "mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-2 [&_h2]:m-0 [&_h2]:text-sm [&_h2]:font-semibold";
const remark = "mt-3 mb-0 text-[12.5px] leading-snug text-muted [&_a]:font-semibold [&_a]:text-ink";
/** A number the engine does not prove: beside its figure, never in place of it. */
const unproven = "rounded-full bg-inset px-2 py-px text-[11.5px] font-medium text-muted";

function Section({ title, children, className = "" }: { title: string; children: ReactNode; className?: string }) {
  const id = useId();
  return (
    <section className={`min-w-0 ${className}`} aria-labelledby={id}>
      <div className={sectionHeadingStyle}><h2 id={id}>{title}</h2></div>
      {children}
    </section>
  );
}

/** A card with its own small heading: the dashboard's tiles and charts. */
function Card({ title, aside, children, className = "" }: { title: string; aside?: ReactNode; children: ReactNode; className?: string }) {
  const id = useId();
  return (
    <section className={`${card} ${className}`} aria-labelledby={id}>
      <div className={cardHead}><h2 id={id}>{title}</h2>{aside}</div>
      {children}
    </section>
  );
}

/** Luna curentă, 3, 6 and 12 luni, Anul: links, so the period is in the URL and the back button returns to it. */
function PeriodSwitch() {
  const { route } = useApp();
  return (
    <nav className="-mx-1 flex max-w-full gap-0.5 overflow-x-auto px-1 [scrollbar-width:none]" aria-label="Perioada">
      {PERIODS.map(([per, , label]) => (
        <L key={per} to={{ tab: "home", per }} className={`${filterLinkStyle} shrink-0 whitespace-nowrap px-2.5! min-[1100px]:min-h-8! min-[1100px]:px-3!`} current={route.per === per}>{label}</L>
      ))}
    </nav>
  );
}

/** "lună incompletă" or "luna în curs": why a month's numbers are not its final ones; "" when they are. */
export function monthNote(m: ChartMonth): string {
  if (m.cash_status === "complete") return "";
  if (m.in_progress) return "Luna în curs";
  return m.cash_status === "partial" ? "Lună incompletă" : "Niciun extras";
}

/** A month's three numbers as the chart draws them: the proven ones, else what the lines show so far (drawn lighter). */
const shown = (m: ChartMonth) => m.cash_status === "complete" && m.in && m.out && m.net
  ? { in: m.in, out: m.out, net: m.net } : m.observed;

/** The months the period's chart draws, the last one today's. */
function chartMonths(d: Dash, per: Route["per"]): ChartMonth[] {
  const n = periodRow(per)[3];
  const ms = d.charts.months;
  if (n) return ms.slice(-n);
  const year = ms[ms.length - 1]?.month.slice(0, 4);
  return ms.slice(-Math.max(3, ms.filter((m) => m.month.startsWith(year)).length));
}

/** Money in and out per month, lei only, and the net as a line where the month is proven; a month not proven is lighter
 * and says why. A month opens Tranzacții for it (one firm). The table under it has every currency. */
function CashChart({ d, all, per }: { d: Dash; all: boolean; per: Route["per"] }) {
  const ms = chartMonths(d, per);
  const columns: Column[] = ms.map((m) => {
    const s = shown(m), none = m.cash_status === "none" && !m.in_progress;
    return {
      key: m.month, label: cap(luna(m.month)), tick: luna(m.month).slice(0, 3), current: m.in_progress,
      values: none ? [null, null] : [Number(s.in.total), Number(s.out.total)],
      line: m.cash_status === "complete" ? Number(s.net.total) : null,
      faded: m.cash_status !== "complete", note: monthNote(m) || undefined,
      tip: [{ label: "Încasat", value: sums(s.in), tone: "in" }, { label: "Cheltuit", value: sums(s.out), tone: "out" },
        { label: "Sold net", value: sums(s.net), tone: "ink", line: true }],
      to: all ? undefined : { tab: "tranzactii", f: "all", month: m.month },
    };
  });
  const other = ms.some((m) => { const s = shown(m); return s.in.other.length || s.out.other.length; });
  return (
    <>
      <div className="mb-3"><Legend items={[{ label: "Încasat", tone: "in" }, { label: "Cheltuit", tone: "out" }, { label: "Sold net", tone: "ink", line: true },
        ...(columns.some((c) => c.faded) ? [{ label: "Fără extrase complete", tone: "muted" as const, striped: true }] : [])]} /></div>
      <ColumnChart title="Încasat și cheltuit pe lună" desc="Coloane pentru încasat și cheltuit în lei, linie pentru soldul net al lunilor dovedite de extrase."
        series={[{ label: "Încasat", tone: "in" }, { label: "Cheltuit", tone: "out" }]} line={{ label: "Sold net" }} columns={columns}
        height={220} table={{ caption: "Încasat, cheltuit și sold net pe lună", head: ["Luna", "Încasat", "Cheltuit", "Sold net", "Stare"],
          rows: ms.map((m) => { const s = shown(m); return { key: m.month, cells: [cap(luna(m.month)), sums(s.in), sums(s.out), sums(s.net), monthNote(m) || "Dovedită de extrase"] }; }) }} />
      {other ? <p className={remark}>Graficul arată doar sumele în lei.</p> : null}
    </>
  );
}

/** Încasat, cheltuit, sold net: what moved in the accounts in the period, each opening Tranzacții for the period's days and
 * money in or out (one firm); a period with a month not proven says so beside the figures. Then the months, drawn. */
function CashFlow({ d, ss, all, per }: { d: Dash; ss: State[]; all: boolean; per: Route["per"] }) {
  const p = d.periods[periodOf(per)];
  const fig = (label: string, s: Sums, f: "in" | "out" | "all") => {
    const icon = { in: "arrow-down-left:bold", out: "arrow-up-right:bold", all: "" }[f];
    const tone = { in: "text-chart-in", out: "text-chart-out", all: "text-ink" }[f];
    const inner = <>
      <span className="flex items-center gap-1.5 text-[13px] font-medium text-muted [&_svg]:size-3.5">{icon ? <span className={`flex ${tone}`}><I n={icon} /></span> : null}{label}</span>
      <span className="amt text-xl leading-7 font-semibold tracking-tight [font-stretch:110%] tabular-nums min-[1100px]:text-2xl [&_.dec]:text-sm [&_.cur]:ml-1 [&_.cur]:text-xs [&_.cur]:font-normal [&_.cur]:text-muted"><Big a={s.total} /><span className="cur">lei</span></span>
      {s.other.map((o) => <span key={o.currency} className="text-[12.5px] text-muted">și {lei(o.total)} {o.currency}</span>)}
    </>;
    const cls = "flex min-w-0 flex-col text-ink no-underline";
    return all ? <div key={label} className={cls}>{inner}</div>
      : <L key={label} className={cls} to={{ tab: "tranzactii", f, from: p.from, to: p.to }}>{inner}</L>;
  };
  const read = all ? ss.flatMap((s) => s.dashboard.through ?? []).sort() : d.through ? [d.through] : [];
  const none = all ? ss.filter((s) => s.dashboard.through === null) : [];
  const through = read[0] ?? null;
  const today = ss[0]?.today ?? p.to;
  return (
    <Card title="Încasat și cheltuit" aside={<PeriodSwitch />} className="min-[1100px]:col-span-2">
      <div className="figs mb-5 flex flex-wrap gap-x-6 gap-y-3 min-[1100px]:gap-x-10">
        {fig("Încasat", p.in, "in")}
        {fig("Cheltuit", p.out, "out")}
        {fig("Sold net", p.left, "all")}
      </div>
      {p.cash_status !== "complete" && through !== null
        ? <p className="mt-0 mb-4 flex items-center gap-2 text-[12.5px] text-muted"><span className={unproven}>{p.cash_status === "partial" ? "Incomplet" : "Fără extrase"}</span>Sumele arată doar tranzacțiile citite până acum.</p> : null}
      {d.charts.months.some((m) => m.cash_status !== "none" || m.in_progress) ? <CashChart d={d} all={all} per={per} /> : null}
      {through === null ? <p className={remark}>Niciun extras încă.</p>
        : through < p.to ? <p className={remark}>{`Tranzacții până pe ${dm(through, today)}.`}</p> : null}
      {through !== null && none.length ? <p className={remark}>{`Fără tranzacții în această perioadă: ${none.map(coName).join(", ")}.`}</p> : null}
      {d.to_explain ? <p className={remark}><L to={{ tab: "tranzactii" }}>{cap(nOf(d.to_explain, "o tranzacție de explicat", "tranzacții de explicat"))}</L></p> : null}
    </Card>
  );
}

/** One of the key numbers: its name and amount (lei, other currencies under it) opening its list, then its lines, each
 * opening the list it counts. */
function Kpi({ title, s, to, badge, money = false, children }: { title: string; s: Sums; to?: Partial<Route>; badge?: string; money?: boolean; children: ReactNode }) {
  const head = <>
    <span className="flex flex-wrap items-center gap-x-1 gap-y-1 text-[13px] font-medium text-muted [&_svg]:size-3.5">{title}{to ? <I n="caret-right" /> : null}{badge ? <span className={`ml-auto ${unproven}`}>{badge}</span> : null}</span>
    <span className="amt mt-1 text-xl leading-7 font-semibold tracking-tight [font-stretch:110%] tabular-nums min-[1100px]:text-[26px] min-[1100px]:leading-8 [&_.dec]:text-sm [&_.cur]:ml-1 [&_.cur]:text-xs [&_.cur]:font-normal [&_.cur]:text-muted"><Big a={s.total} /><span className="cur">lei</span></span>
    {s.other.map((o) => <span key={o.currency} className="text-[12.5px] text-muted">și {lei(o.total)} {o.currency}</span>)}
  </>;
  const door = "flex min-w-0 flex-col text-ink no-underline";
  return (
    <section aria-label={title} className={`${money ? "money " : ""}${card} flex min-w-0 flex-col`}>
      {to ? <L className={door} to={to}>{head}</L> : <div className={door}>{head}</div>}
      <div className="mt-2 grid gap-0.5 text-[12.5px] leading-snug text-muted [&_a]:-mx-1.5 [&_a]:flex [&_a]:flex-wrap [&_a]:justify-between [&_a]:gap-x-2 [&_a]:rounded-control [&_a]:px-1.5 [&_a]:py-1 [&_a]:text-muted [&_a]:no-underline [&_a:hover]:bg-inset [&_a:hover]:text-ink [&_b]:ml-auto [&_b]:font-semibold [&_b]:text-ink [&_b.late]:text-danger">{children}</div>
    </section>
  );
}

/** De încasat / De plătit's lines: due in 7 days, and the overdue part with its count (`late_total`, `late`). */
function DueLines({ m, list }: { m: Money; list: Partial<Route> }) {
  return <>
    <L to={{ ...list, f: "open" }}><span>Scadente în 7 zile</span><b>{m.due_soon}</b></L>
    <L to={{ ...list, f: "late" }}><span>{m.late ? cap(nOf(m.late, "una restantă", "restante")) : "Restante"}</span>
      <b className={m.late ? "late" : ""}>{sums({ total: m.late_total, other: m.late_other })}</b></L>
  </>;
}

/** De plătit, De încasat, Solduri bancare and this month's VAT estimate. What the payment files hold needs one firm. */
function Kpis({ d, all, today }: { d: Dash; all: boolean; today: string }) {
  const { view, route } = useApp();
  const b = d.charts.balance_total;
  const s = isAll(view) ? null : view;
  const vat = d.charts.vat.at(-1);
  return (
    <div className={`grid grid-cols-2 gap-2 min-[560px]:gap-3 min-[1100px]:order-first min-[1100px]:grid-cols-4 min-[1100px]:gap-4 ${span}`}>
      <Kpi money title="De plătit" s={view.payable} to={{ co: route.co, tab: "cheltuieli", f: "open" }}>
        <DueLines m={view.payable} list={{ co: route.co, tab: "cheltuieli" }} />
        <InFiles s={s} />
      </Kpi>
      <Kpi money title="De încasat" s={view.receivable} to={{ co: route.co, tab: "vanzari", f: "open" }}>
        <DueLines m={view.receivable} list={{ co: route.co, tab: "vanzari" }} />
      </Kpi>
      <Kpi title="Solduri bancare" s={b} badge={b.partial ? "Incomplet" : undefined} to={all ? undefined : { tab: "tranzactii" }}>
        <span className="py-1">{b.day ? `${b.partial ? "Un cont fără sold din extras. " : ""}La ${dm(b.day, today)}, din extras` : "Disponibil după primul extras complet"}</span>
      </Kpi>
      {vat ? <Kpi title="TVA estimată" s={vat.net} badge={vat.partial ? "Incomplet" : undefined}>
        <span className="py-1">{vatLine(vat)}</span>
      </Kpi> : null}
    </div>
  );
}

/** A VAT month's line: which month, whether it is still running, how many invoices carry no VAT data. */
const vatLine = (v: VatMonth) => [cap(lunaName(v.month)) + (v.in_progress ? ", luna în curs" : ""),
  v.partial ? `fără datele din ${nOf(v.missing_count, "o factură", "facturi")}` : ""].filter(Boolean).join(", ");

/** VAT estimated from the recorded invoices, the previous month and this one: collected, deductible and what is left to pay,
 * per currency. An estimate, never the declaration. */
function Vat({ vat }: { vat: VatMonth[] }) {
  return (
    <Card title="TVA colectată și deductibilă" aside={<span className="flex gap-1.5">{vat.some((v) => v.partial) ? <span className={unproven}>Incomplet</span> : null}<span className={unproven}>Estimare</span></span>}>
      <table className="m-0 w-full border-collapse text-[13px] [&_td]:py-1.5 [&_td]:pl-2 [&_td]:text-right [&_td]:align-top [&_td]:tabular-nums [&_th]:py-1.5 [&_th]:text-left [&_th]:align-top [&_th]:font-normal [&_thead_th]:font-medium [&_thead_th]:text-muted [&_thead_th:not(:first-child)]:text-right">
        <thead><tr><th scope="col"><span className="sr-only">TVA</span></th>{vat.map((v) => <th key={v.month} scope="col">{cap(lunaName(v.month))}</th>)}</tr></thead>
        <tbody>
          <tr><th scope="row" className="text-muted">Colectată</th>{vat.map((v) => <td key={v.month}><SumsText s={v.collected} /></td>)}</tr>
          <tr><th scope="row" className="text-muted">Deductibilă</th>{vat.map((v) => <td key={v.month}><SumsText s={v.deductible} /></td>)}</tr>
          <tr className="border-0 border-t border-solid border-line"><th scope="row" className="font-semibold!">De plată</th>{vat.map((v) => <td key={v.month} className="font-semibold"><SumsText s={v.net} /></td>)}</tr>
        </tbody>
      </table>
      <p className={remark}>{["Din facturile înregistrate, nu din declarație.",
        ...vat.filter((v) => v.partial).map((v) => `${cap(lunaName(v.month))}: lipsesc datele de TVA din ${nOf(v.missing_count, "o factură", "facturi")}.`)].join(" ")}</p>
    </Card>
  );
}

/** What the firm's open payment files hold, per currency: money prepared but not imported in the bank yet. */
function InFiles({ s }: { s: State | null }) {
  const open = (s?.files ?? []).filter((f) => f.status === "open" && f.payment_count);
  if (!open.length) return null;
  const by = new Map<string, bigint>();
  for (const f of open) by.set(curOf(f.currency), (by.get(curOf(f.currency)) ?? 0n) + cents(f.total));
  const text = [...by].map(([c, n]) => `${lei(fromCents(n))} ${c}`).join(" și ");
  return <L to={{ tab: "plati" }}><span>În fișiere de plăți</span><b>{text}</b></L>;
}

/** Each current account: its balance as the newest statement proves it, and its proven closings over time (one small chart
 * per account, never two currencies on one scale). */
function Balances({ d, all, today }: { d: Dash; all: boolean; today: string }) {
  const history = (b: Dash["balances"][number]) => d.charts.balance_history.find((h) => h.company === b.company && h.bank === b.bank && h.iban_last4 === b.iban_last4 && h.currency === b.currency);
  return (
    <Card title="Conturi bancare">
      {d.balances.length ? <div className="-mx-4 min-[1100px]:-mx-5">{d.balances.map((b) => {
        const name = `Cont în ${curOf(b.currency)}`;
        const when = b.balance !== null && b.day ? `la ${dm(b.day, today)}, din extras` : "disponibil după primul extras complet";
        const h = history(b);
        const pts = (h?.points ?? []).map((p) => ({ key: p.day, label: dm(p.day, today), v: Number(p.balance) }));
        return <div key={`${b.company}${b.currency}${b.iban_last4}`} className="border-0 border-t border-solid border-line first:border-t-0">
          <Row cls="rule" icon="bank" to={{ co: b.company, tab: "tranzactii" }}
            title={all ? `${b.company}, ${name.charAt(0).toLowerCase()}${name.slice(1)}` : name}
            sub={`•••• ${b.iban_last4}, ${when}`} val={b.balance !== null ? `${lei(b.balance)} ${curOf(b.currency)}` : ""} />
          {pts.length > 1 ? <div className="px-4 pb-3 min-[1100px]:px-5"><AreaChart title={`Soldul contului •••• ${b.iban_last4}`}
            desc={`Soldurile de închidere din extrase, de pe ${pts[0].label} până pe ${pts[pts.length - 1].label}.`} points={pts} height={56}
            value={(v) => `${lei(v.toFixed(2))} ${curOf(b.currency)}`}
            table={{ caption: `Soldul contului •••• ${b.iban_last4}`, head: ["Ziua", "Sold"], rows: (h?.points ?? []).map((p) => ({ key: p.day, cells: [dm(p.day, today), `${lei(p.balance)} ${curOf(b.currency)}`] })) }} /></div> : null}
        </div>;
      })}</div> : <Empty />}
    </Card>
  );
}

const AGING: Record<AgingKey, { label: string; cls: string; late: boolean }> = {
  not_due: { label: "Nescadente", cls: "bg-muted/45", late: false },
  "1_30": { label: "Restante 1-30 de zile", cls: "bg-danger/45", late: true },
  "31_60": { label: "Restante 31-60 de zile", cls: "bg-danger/75", late: true },
  over_60: { label: "Restante peste 60 de zile", cls: "bg-danger", late: true },
  unknown_due: { label: "Fără scadență", cls: "bg-muted/20", late: false },
};

/** Open issued or received invoices by days past due (invoices only: De plătit also counts payments without one, such as
 * a tax order, so the two can differ): one bar of the lei, then each bucket with its count and amounts, opening its list
 * (Fără scadență only when it holds an invoice). What a person marked paid without proof is apart, under the buckets:
 * awaiting its proof, never settled. */
function AgingCard({ title, a, side }: { title: string; a: AgingData; side: "vanzari" | "cheltuieli" }) {
  const { route } = useApp();
  const rows = a.buckets.filter((b) => b.bucket !== "unknown_due" || b.count);
  return (
    <Card title={title}>
      <SegmentBar label={`${title}: ${rows.map((b) => `${AGING[b.bucket].label.toLowerCase()} ${sums(b)}`).join(", ")}`}
        parts={rows.map((b) => ({ key: b.bucket, value: Number(b.total), cls: AGING[b.bucket].cls }))} />
      <ul className="mt-3 mb-0 grid list-none gap-0.5 p-0">
        {rows.map((b) => <li key={b.bucket}><L to={{ co: route.co, tab: side, f: AGING[b.bucket].late ? "late" : "open" }}
          className="-mx-2 flex items-start gap-2 rounded-control px-2 py-1.5 text-[13px] text-ink no-underline hover:bg-inset">
          <i aria-hidden="true" className={`mt-1.5 size-2 shrink-0 rounded-badge ${AGING[b.bucket].cls}`} />
          <span className="min-w-0 flex-1">{AGING[b.bucket].label}<span className="ml-1.5 text-muted">{b.count}</span></span>
          <b className={`max-w-[55%] text-right font-semibold tabular-nums ${AGING[b.bucket].late && b.count ? "text-danger" : ""}`}><SumsText s={b} /></b>
        </L></li>)}
      </ul>
      {rows.some((b) => b.other.length) ? <p className={remark}>Bara arată doar sumele în lei.</p> : null}
      {a.confirming.count ? <L to={{ co: route.co, tab: side }} className="mt-2 -mx-2 flex justify-between gap-2 rounded-control border-0 border-t border-solid border-line px-2 pt-2.5 pb-1.5 text-[13px] text-muted no-underline hover:bg-inset">
        <span>În curs de confirmare<span className="ml-1.5">{a.confirming.count}</span></span><b className="font-semibold text-ink"><SumsText s={a.confirming} /></b></L> : null}
    </Card>
  );
}

/** The largest clients or suppliers of the period, each opening its partner's sheet; bars in money in's or out's colour. */
function Top({ title, rows, all, label, tone }: { title: string; rows: TopPartner[]; all: boolean; label: string; tone: Tone }) {
  return (
    <Card title={title} aside={<span className="text-[12.5px] text-muted">{label}</span>}>
      {rows.length ? <BarList tone={tone} rows={rows.map((e) => ({
        key: `${e.company}${e.partner ?? e.name}`, value: sums(e), size: Number(e.total),
        name: <>{e.name}<span className="ml-1.5 text-muted">{all ? `${e.company}, ` : ""}{nOf(e.count, "o factură", "facturi")}</span></>,
        to: e.partner ? { co: e.company, p: e.partner } : undefined,
      }))} /> : <Empty />}
    </Card>
  );
}

/** Toate firmele: one line per firm, any number of them, with its own sums (never added across currencies); each opens
 * that firm's Acasă for the same period. */
function Firms({ ss, period }: { ss: State[]; period: Period }) {
  const { route } = useApp();
  return (
    <Section title="Pe firme" className={span}>
      <div className={`card ${surfaceStyle} py-0`}>
        {ss.map((s) => {
          const q = s.dashboard.periods[period];
          return (
            <Row cls="rule" key={s.co} icon="bank" title={coName(s)} to={{ co: s.co, tab: "home", per: route.per }}
              sub={`Încasat ${sums(q.in)}, cheltuit ${sums(q.out)}`} val={sums(q.left)} vsub="Sold net" />
          );
        })}
      </div>
    </Section>
  );
}

const ICON: Record<Upcoming["kind"], string> = { invoice: "receipt", payment: "currency-circle-dollar", issued: "invoice" };

function useDashboardPage<C extends DashboardCollection>(collection: C) {
  const { view } = useApp();
  const first = view.dashboard_pages[collection] as Page<DashboardRows[C]>;
  const read = useCallback((cursor: string, signal?: AbortSignal) => cursor
    ? getDashboardPage(view.co, collection, cursor, signal) : Promise.resolve(first), [view.co, collection, first]);
  return usePage(read, 0, read, first);
}

/** Every complete partner/currency aggregate opens the partner's held invoices and reports. */
export function HeldMoney() {
  const { view } = useApp();
  const page = useDashboardPage("held_money");
  return <section className={span}><div className={sectionHeadingStyle}><h2>La curieri și procesatori</h2></div><div className={`card ${surfaceStyle} py-0`} aria-busy={page.loading}>
    <PageNav page={page} label="La curieri și procesatori" />
    {page.data.map((r) => <Row cls="rule" key={`${r.company}:${r.id}`} icon="bank" title={r.name ?? ""}
      sub={parts(view).length > 1 ? r.company : undefined} val={`${lei(r.amount)} ${curOf(r.currency)}`} to={{ co: r.company, p: r.partner }} />)}
    {!page.loading && !page.error && !page.total ? <Empty /> : null}
  </div></section>;
}

/** What falls due in the next 30 days, by date: each row opens its invoice (or Plăți for a payment). */
function Ahead({ all, today }: { all: boolean; today: string }) {
  const page = useDashboardPage("upcoming");
  const to = (u: Upcoming): Partial<Route> => (u.kind === "invoice" ? { co: u.company, inv: u.id }
    : u.kind === "issued" ? { co: u.company, iss: u.id } : { co: u.company, tab: "plati" });
  return (
    <Section title="Următoarele 30 de zile" className={span}>
      <div className={`card ${surfaceStyle} py-0`} aria-busy={page.loading}>
        <PageNav page={page} label="Următoarele 30 de zile" />
        {page.data.map((u) => {
          const [tone, due] = dueText(u.date, today);
          const what = [all ? u.company : "", due, u.number].filter(Boolean).join(", ");
          return (
            <Row cls="rule" key={`${u.company}${u.kind}${u.id}`} icon={ICON[u.kind]} title={u.who} sub={what} subcls={tone}
              val={`${lei(u.amount)} ${curOf(u.currency)}`} vsub={u.side === "in" ? "De încasat" : "De plătit"} to={to(u)} />
          );
        })}
        {!page.loading && !page.error && !page.total ? <Empty ok /> : null}
      </div>
    </Section>
  );
}

/** Acasă's numbers, the same on the phone and in a wide window (the layout differs in CSS only): money in and out over the
 * months (first on a phone), the key numbers (first in a wide window), the accounts, what is open by days late, who the
 * firm traded with most and what it sold. */
export function Dashboard() {
  const { view, route } = useApp();
  const ss = parts(view);
  const all = isAll(view);
  const d = view.dashboard;
  const period = periodOf(route.per);
  const p = d.periods[period];
  const label = periodRow(route.per)[2];
  return (
    <div className="dash mt-6 grid grid-cols-1 gap-3 min-[1100px]:mt-8 min-[1100px]:grid-cols-3 min-[1100px]:items-start min-[1100px]:gap-4">
      <CashFlow d={d} ss={ss} all={all} per={route.per} />
      <Kpis d={d} all={all} today={view.today} />
      <Balances d={d} all={all} today={view.today} />
      <div className={`grid gap-3 min-[1100px]:gap-4 ${d.charts.vat.length ? "min-[1100px]:grid-cols-3" : "min-[1100px]:grid-cols-2"} ${span}`}>
        <AgingCard title="Facturi emise, după scadență" a={d.charts.aging.receivables} side="vanzari" />
        <AgingCard title="Facturi primite, după scadență" a={d.charts.aging.payables} side="cheltuieli" />
        {d.charts.vat.length ? <Vat vat={d.charts.vat} /> : null}
      </div>
      {all ? <Firms ss={ss} period={period} /> : null}
      <div className={`grid gap-3 min-[1100px]:grid-cols-2 min-[1100px]:gap-4 min-[1400px]:grid-cols-3 ${span}`}>
        <Top title="Clienți principali" rows={p.clients} all={all} label={label} tone="in" />
        <Top title="Furnizori principali" rows={p.suppliers} all={all} label={label} tone="out" />
        <ProductSales sales={p.product_sales} label={label} names={all ? Object.fromEntries(ss.map((x) => [x.co, coName(x)])) : undefined} className="min-[1100px]:col-span-2 min-[1400px]:col-span-1" />
      </div>
      <HeldMoney />
      <Ahead all={all} today={view.today} />
    </div>
  );
}
