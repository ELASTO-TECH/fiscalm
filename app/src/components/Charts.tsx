"use client";
// fiscalm's charts: columns with a line over them, a single-series area line, a segmented bar and a ranked bar list, drawn in
// SVG with the app's tokens, so light and dark follow the theme. Each chart names itself (<title>/<desc>) and carries its numbers
// in a table for screen readers; the picture is a summary of that table, never the only place a number is. A tooltip opens on
// hover, focus or a first tap and Escape hides it until the pointer or the focus leaves (WCAG 1.4.13). Entry is one calm rise,
// none under reduced motion; amounts never move.
import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { motion } from "./Controls";
import type { Route } from "@/lib/nav";
import { integer } from "@/lib/format";
import { L } from "./ui";

/** What a mark's colour says: money in (also a balance's line) or out (globals.css --chart-in, --chart-out), the net, or nothing. */
export type Tone = "in" | "out" | "ink" | "muted";
const FILL: Record<Tone, string> = { in: "fill-chart-in", out: "fill-chart-out", ink: "fill-ink", muted: "fill-muted" };
const BG: Record<Tone, string> = { in: "bg-chart-in", out: "bg-chart-out", ink: "bg-ink", muted: "bg-muted" };
const COLOR: Record<Tone, string> = { in: "var(--chart-in)", out: "var(--chart-out)", ink: "var(--ink)", muted: "var(--mu)" };
const STROKE: Record<Tone, string> = { in: "stroke-chart-in", out: "stroke-chart-out", ink: "stroke-ink", muted: "stroke-muted" };
/** A mark whose numbers are not proven: its colour in stripes, so it reads as unfinished, not as a lighter value. */
const hatch = "bg-[repeating-linear-gradient(135deg,currentColor_0_2px,transparent_2px_4px)]";
export const swatchStyle = "inline-block size-2 shrink-0 rounded-badge";
export const Swatch = ({ tone, line = false, striped = false }: { tone: Tone; line?: boolean; striped?: boolean }) =>
  <i aria-hidden="true" className={`${swatchStyle} ${striped ? `${hatch} text-muted` : BG[tone]} ${line ? "h-0.5! w-3!" : ""}`} />;

/** A round scale over lo..hi that always holds zero: up to about 5 ticks of 1, 2 or 5 times a power of ten. */
export function niceScale(lo: number, hi: number): { lo: number; hi: number; ticks: number[] } {
  const a = Math.min(lo, 0), b = Math.max(hi, 0);
  const span = b - a || 1;
  const p = 10 ** Math.floor(Math.log10(span / 4));
  const step = Math.max(1, ([1, 2, 5, 10].find((k) => k * p >= span / 4) ?? 10) * p);
  const l = Math.floor(a / step) * step, h = Math.max(Math.ceil(b / step) * step, l + step);
  const ticks: number[] = [];
  for (let t = l; t <= h + step / 2; t += step) ticks.push(Math.round(t));
  return { lo: l, hi: h, ticks };
}

/** An axis label: whole lei with thousands, millions shortened ("1,2 mil."). */
export const axisLabel = (n: number) => (Math.abs(n) >= 1e6 ? `${(n / 1e6).toLocaleString("ro-RO", { maximumFractionDigits: 1 })} mil.` : integer(n));

/** The drawing's width, followed as its box changes: ResizeObserver, or the window's resize where it is missing. */
function useWidth<E extends HTMLElement>(): [React.RefObject<E | null>, number] {
  const ref = useRef<E>(null);
  const [w, setW] = useState(600);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => { if (el.clientWidth) setW(Math.max(200, el.clientWidth)); };
    measure();
    if (typeof ResizeObserver !== "function") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const watch = new ResizeObserver(measure);
    watch.observe(el);
    return () => watch.disconnect();
  }, []);
  return [ref, w];
}

/** The tooltip's index: shown on hover or focus, Escape keeps it hidden until both leave. */
function useTip() {
  const [at, setAt] = useState<number | null>(null);
  const [quiet, setQuiet] = useState(false);
  useEffect(() => {
    if (at === null) return;
    const esc = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") setQuiet(true); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [at]);
  const leave = () => { setAt(null); setQuiet(false); };
  return { at: quiet ? null : at, show: setAt, leave, quiet };
}

/** A tooltip's content: its heading, one line per value with the mark's swatch, and a remark (a month not proven yet). */
export type TipLine = { label: string; value: string; tone?: Tone; line?: boolean };
function Tip({ title, lines, note, left, width, gap = 12 }: { title: string; lines: TipLine[]; note?: string; left: number; width: number; gap?: number }) {
  // beside its mark, on the side with more room, so the mark it describes stays visible
  const pos = left < width / 2 ? { left: left + gap } : { right: width - left + gap };
  return (
    <div aria-hidden="true" data-tip="" style={pos} className={`absolute top-0 z-[2] flex flex-col gap-1 whitespace-nowrap rounded-control border border-solid border-line bg-surface px-2.5 py-2 text-[12.5px] text-ink shadow-pop ${motion.fade}`}>
      <b className="font-semibold">{title}</b>
      {lines.map((l) => <span key={l.label} className="flex items-center gap-1.5">{l.tone ? <Swatch tone={l.tone} line={l.line} /> : null}{l.label}<em className="ml-auto pl-4 font-semibold not-italic tabular-nums">{l.value}</em></span>)}
      {note ? <span className="text-muted">{note}</span> : null}
    </div>
  );
}

/** The table a chart stands for, read by screen readers instead of the picture. */
export type ChartTable = { caption: string; head: string[]; rows: { key: string; cells: string[] }[] };
function SrTable({ t }: { t: ChartTable }) {
  return (
    <div className="sr-only"><table>
      <caption>{t.caption}</caption>
      <thead><tr>{t.head.map((h) => <th key={h} scope="col">{h}</th>)}</tr></thead>
      <tbody>{t.rows.map((r) => <tr key={r.key}>{r.cells.map((c, i) => (i ? <td key={i}>{c}</td> : <th key={i} scope="row">{c}</th>))}</tr>)}</tbody>
    </table></div>
  );
}

/** The legend: every series named beside its mark, so colour is never the only way to tell them apart. */
export function Legend({ items }: { items: { label: string; tone: Tone; line?: boolean; striped?: boolean }[] }) {
  return <ul className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 p-0 text-[12.5px] text-muted" aria-hidden="true">
    {items.map((i) => <li key={i.label} className="flex items-center gap-1.5"><Swatch tone={i.tone} line={i.line} striped={i.striped} />{i.label}</li>)}
  </ul>;
}

/** One column group: its values per series (null: not drawn), the line's point, what it says in the tooltip, `faded` when
 * the numbers are not proven (drawn lighter, with `note`), and where a tap on it leads. */
export type Column = { key: string; label: string; tick: string; values: (number | null)[]; line?: number | null;
  tip: TipLine[]; faded?: boolean; note?: string; to?: Partial<Route>; current?: boolean };

const PAD = { t: 8, b: 26, r: 4 };
/** A column's path: square at the baseline, its far end rounded 3px, upwards for a positive value and downwards below zero. */
const col = (x: number, end: number, w: number, base: number) => {
  const r = Math.min(3, Math.abs(base - end), w / 2), k = end <= base ? 1 : -1;
  return `M${x},${base}V${end + k * r}Q${x},${end} ${x + r},${end}H${x + w - r}Q${x + w},${end} ${x + w},${end + k * r}V${base}Z`;
};

/** Columns per period, one per series side by side, and an optional line over them on the same scale (the net). */
export function ColumnChart({ title, desc, series, line, columns, height = 200, table }: {
  title: string; desc: string; series: { label: string; tone: Tone }[]; line?: { label: string; tone?: Tone };
  columns: Column[]; height?: number; table: ChartTable;
}) {
  const id = useId();
  const [ref, w] = useWidth<HTMLDivElement>();
  const tip = useTip();
  const all = columns.flatMap((c) => [...c.values, c.line ?? null]).filter((v): v is number => v !== null);
  const s = niceScale(Math.min(0, ...all), Math.max(0, ...all));
  const axis = Math.max(...s.ticks.map((t) => axisLabel(t).length)) * 6.6 + 8;
  const iw = w - axis - PAD.r, ih = height - PAD.t - PAD.b, band = iw / Math.max(1, columns.length);
  const bar = Math.max(3, Math.min(24, (band * 0.6) / series.length - 2));
  const y = (v: number) => PAD.t + ih - ((v - s.lo) / (s.hi - s.lo)) * ih;
  const cx = (i: number) => axis + band * i + band / 2;
  const pts = columns.map((c, i) => (c.line === null || c.line === undefined ? null : `${cx(i)},${y(c.line)}`));
  const runs = pts.reduce<string[][]>((acc, p) => { if (p === null) acc.push([]); else acc[acc.length - 1].push(p); return acc; }, [[]]).filter((r) => r.length);
  const active = tip.at === null ? null : columns[tip.at];
  return (
    <div ref={ref} className="relative min-w-0" onMouseLeave={tip.leave}>
      <svg viewBox={`0 0 ${w} ${height}`} style={{ width: w, height }} role="img" aria-labelledby={`${id}t`} aria-describedby={`${id}d`} className="block overflow-visible">
        <title id={`${id}t`}>{title}</title><desc id={`${id}d`}>{desc}</desc>
        <defs>{series.map((x, k) => <pattern key={k} id={`${id}h${k}`} width={4} height={4} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width={1.6} height={4} className={FILL[x.tone]} /></pattern>)}</defs>
        {s.ticks.map((t) => <g key={t}>
          <line x1={axis} x2={w - PAD.r} y1={y(t)} y2={y(t)} className={t === 0 ? "stroke-muted/50" : "stroke-line"} />
          <text x={axis - 8} y={y(t) + 4} textAnchor="end" className="fill-muted text-[11px]">{axisLabel(t)}</text>
        </g>)}
        <g className={motion.rise}>
          {columns.map((c, i) => <g key={c.key} className={c.faded ? "faded" : ""}>
            {c.values.map((v, k) => (v === null ? null : <path key={k} className={c.faded ? STROKE[series[k].tone] : FILL[series[k].tone]}
              fill={c.faded ? `url(#${id}h${k})` : undefined} strokeWidth={c.faded ? 1 : undefined}
              d={col(cx(i) - (series.length * (bar + 2)) / 2 + k * (bar + 2) + 1, y(v), bar, y(0))} />))}
          </g>)}
          {line ? runs.map((r) => <polyline key={r[0]} points={r.join(" ")} fill="none" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" className="stroke-ink" />) : null}
          {line ? columns.map((c, i) => (c.line === null || c.line === undefined ? null : <circle key={c.key} cx={cx(i)} cy={y(c.line)} r={tip.at === i ? 5 : 3.5} strokeWidth={2} className="fill-ink stroke-surface" />)) : null}
        </g>
        {columns.map((c, i) => <text key={c.key} x={cx(i)} y={height - 8} textAnchor="middle"
          className={`text-[11.5px] ${c.current ? "fill-ink font-semibold" : "fill-muted"}`}>{c.tick}</text>)}
      </svg>
      <div className="absolute top-0 bottom-0 grid" style={{ left: axis, right: PAD.r, gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}>
        {columns.map((c, i) => <Band key={c.key} c={c} i={i} tip={tip} />)}
      </div>
      {active && tip.at !== null ? <Tip title={active.label} lines={active.tip} note={active.note} left={cx(tip.at)} width={w} gap={band / 2} /> : null}
      <SrTable t={table} />
    </div>
  );
}

/** A column's hit area, the whole band: a link for one firm (its numbers in its name), else a picture only; on touch the
 * first tap shows its tooltip and the second follows the link. The tooltip itself can be hovered (WCAG 1.4.13). */
function Band({ c, i, tip }: { c: Column; i: number; tip: ReturnType<typeof useTip> }) {
  const touch = useRef(false);
  const open = useRef(false); // whether this tooltip was open before the tap: a touch's focus opens it before the click
  const cls = "block h-full rounded-control outline-offset-[-2px] hover:bg-ink/4";
  const name = [c.label, ...c.tip.map((t) => `${t.label.toLowerCase()} ${t.value}`), c.note].filter(Boolean).join(", ");
  const events = {
    onMouseEnter: () => tip.show(i), onFocus: () => tip.show(i), onBlur: tip.leave,
    onPointerDown: (e: { pointerType: string }) => { touch.current = e.pointerType === "touch"; open.current = tip.at === i; },
    onClickCapture: (e: MouseEvent) => {
      if (!touch.current || open.current) return;
      e.preventDefault(); e.stopPropagation(); tip.show(i);
    },
  };
  return c.to ? <span className="contents" {...events}><L className={cls} to={c.to} label={name}>{null}</L></span>
    : <span className={cls} aria-hidden="true" {...events} onClick={() => tip.show(i)} />;
}

/** One series over time: an area under a line, its points dated; keyboard arrows move the tooltip along it. Shown as a
 * small multiple (one per account, never two currencies on one scale). */
export function AreaChart({ title, desc, points, height = 120, value, tone = "in", table, ticks }: {
  title: string; desc: string; points: { key: string; label: string; v: number }[]; height?: number;
  value: (v: number) => string; tone?: Tone; table: ChartTable; ticks?: { at: number; label: string }[];
}) {
  const id = useId();
  const [ref, w] = useWidth<HTMLDivElement>();
  const tip = useTip();
  const vs = points.map((p) => p.v);
  const lo = Math.min(...vs), hi = Math.max(...vs), pad = (hi - lo || Math.abs(hi) || 1) * 0.12;
  const b = ticks?.length ? 20 : 4, ih = height - 6 - b;
  const x = (i: number) => 4 + ((w - 8) * i) / Math.max(1, points.length - 1);
  const y = (v: number) => 6 + ih - ((v - (lo - pad)) / (hi + pad - (lo - pad))) * ih;
  const line = points.map((p, i) => `${x(i)},${y(p.v)}`).join(" ");
  const near = (e: React.PointerEvent) => {
    if ((e.target as Element).closest("[data-tip]")) return; // reading the tooltip keeps its point
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    tip.show(Math.round(((e.clientX - r.left - 4) / Math.max(1, r.width - 8)) * (points.length - 1)));
  };
  const keys = (e: KeyboardEvent) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    tip.show(Math.min(points.length - 1, Math.max(0, (tip.at ?? points.length - 1) + d)));
  };
  const at = tip.at === null ? null : Math.min(points.length - 1, Math.max(0, tip.at));
  if (!points.length) return null;
  return (
    <div ref={ref} className="relative min-w-0 rounded-control focus-visible:outline-2 focus-visible:outline-focus" tabIndex={0} role="group"
      aria-label={title} onPointerMove={near} onPointerDown={near} onPointerLeave={tip.leave} onFocus={() => tip.show(points.length - 1)} onBlur={tip.leave} onKeyDown={keys}>
      <svg viewBox={`0 0 ${w} ${height}`} style={{ width: w, height }} role="img" aria-labelledby={`${id}t`} aria-describedby={`${id}d`} className="block overflow-visible">
        <title id={`${id}t`}>{title}</title><desc id={`${id}d`}>{desc}</desc>
        {/* the area fades from the line's colour down to nothing */}
        <defs><linearGradient id={`${id}f`} x1={0} x2={0} y1={0} y2={1}>
          <stop offset="0" style={{ stopColor: COLOR[tone], stopOpacity: 0.3 }} /><stop offset="1" style={{ stopColor: COLOR[tone], stopOpacity: 0 }} />
        </linearGradient></defs>
        <g className={motion.rise}>
          <polygon points={`${x(0)},${6 + ih} ${line} ${x(points.length - 1)},${6 + ih}`} fill={`url(#${id}f)`} />
          <polyline points={line} fill="none" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" className={STROKE[tone]} />
          <circle cx={x(at ?? points.length - 1)} cy={y(points[at ?? points.length - 1].v)} r={4} strokeWidth={2} className={`${FILL[tone]} stroke-surface`} />
        </g>
        {ticks?.map((t) => <text key={t.at} x={x(t.at)} y={height - 4} textAnchor={t.at === 0 ? "start" : t.at === points.length - 1 ? "end" : "middle"} className="fill-muted text-[11px]">{t.label}</text>)}
      </svg>
      {at !== null ? <Tip title={points[at].label} lines={[{ label: title, value: value(points[at].v) }]} left={x(at)} width={w} /> : null}
      <SrTable t={table} />
    </div>
  );
}

/** One whole split into its parts (lei only), with a 2px gap between them; the legend beside it carries every number.
 * `cls`: each part's fill, one hue stepping darker with what it means (days late). */
export function SegmentBar({ label, parts }: { label: string; parts: { key: string; value: number; cls: string }[] }) {
  return (
    <div role="img" aria-label={label} className="flex h-2.5 gap-0.5 overflow-hidden rounded-badge bg-inset">
      {parts.filter((p) => p.value > 0).map((p) => <span key={p.key} className={`${p.cls} ${motion.fade}`} style={{ flex: `${p.value} 1 0` }} />)}
    </div>
  );
}

/** A ranked list: each row its name and amount in words, a bar under it sized against the first; the bars are decoration. */
export function BarList({ rows, tone = "ink" }: { rows: { key: string; name: ReactNode; value: string; size: number; to?: Partial<Route> }[]; tone?: Tone }) {
  const top = Math.max(...rows.map((r) => r.size), 1);
  return (
    <ol className="m-0 grid list-none gap-1 p-0">
      {rows.map((r) => {
        const inner = <>
          <span className="min-w-0 truncate">{r.name}</span><b className="font-semibold whitespace-nowrap tabular-nums">{r.value}</b>
          {r.size > 0 ? <i aria-hidden="true" className={`col-span-full h-1 rounded-badge ${BG[tone]} ${motion.fade}`} style={{ width: `${Math.max(2, (r.size / top) * 100)}%` }} /> : null}
        </>;
        const cls = "grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1.5 rounded-control px-2 py-2 -mx-2 text-[13px] text-ink no-underline";
        return <li key={r.key}>{r.to ? <L className={`${cls} hover:bg-inset`} to={r.to}>{inner}</L> : <div className={cls}>{inner}</div>}</li>;
      })}
    </ol>
  );
}
