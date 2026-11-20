"use client";
// Acasă's Vânzări pe produse: the period's sales by product (the engine's `periods[p].product_sales`, in the same bootstrap
// read as the rest of Acasă), ranked by revenue, one list per currency and never added across currencies or units.
// Under Toate firmele every firm's rows carry its name (`names`). A negative total (more storno than sales) gets no bar.
// A firm the engine marks incomplete says so beside the title.
import { useId } from "react";
import { curOf, lei, nOf, unitName } from "@/lib/format";
import type { ProductSales as Sales, ProductSalesRow } from "@/lib/types";
import { BarList } from "./Charts";
import { surfaceStyle } from "./Controls";
import { Empty } from "./ui";

type Row = ProductSalesRow & { company: string };

/** The rows of each currency, largest revenue first, at most `top` per currency, lei first; rows with no revenue left out. */
export function ranked(rows: Row[], top = 5): [string, Row[]][] {
  const by = new Map<string, Row[]>();
  for (const r of rows.filter((x) => Number(x.revenue) !== 0)) by.set(r.currency, [...(by.get(r.currency) ?? []), r]);
  return [...by].sort(([a], [b]) => (a === "RON" ? -1 : b === "RON" ? 1 : a.localeCompare(b)))
    .map(([c, rs]) => [c, [...rs].sort((x, y) => Number(y.revenue) - Number(x.revenue)).slice(0, top)]);
}

/** A unit by its Romanian name, or its code when the app has no name for it. */
const unit = (code: string) => unitName(code)?.toLowerCase() ?? code;

export function ProductSales({ sales, label, names, className = "" }: { sales: Sales[]; label: string; names?: Record<string, string>; className?: string }) {
  const id = useId();
  const lists = ranked(sales.flatMap((s) => s.totals.map((r) => ({ ...r, company: s.company }))));
  const missing = sales.reduce((n, s) => n + s.unavailable_count, 0);
  const complete = sales.every((s) => s.complete);
  return (
    <section className={`${surfaceStyle} p-4 min-[1100px]:p-5 ${className}`} aria-labelledby={id}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h2 id={id} className="m-0 text-sm font-semibold">Vânzări pe produse</h2>
        <span className="flex items-center gap-2 text-[12.5px] text-muted">
          {complete ? null : <span className="rounded-full bg-inset px-2 py-px text-[11.5px] font-medium">Incomplet</span>}{label}
        </span>
      </div>
      {lists.length ? lists.map(([currency, rows]) => (
        <div key={currency} className="[&+&]:mt-4">
          {lists.length > 1 ? <p className="mt-0 mb-1 text-[12.5px] font-medium text-muted">{`În ${curOf(currency)}`}</p> : null}
          <BarList tone="in" rows={rows.map((r) => ({
            key: `${r.company}|${r.product_id ?? ""}|${r.name}|${r.unit}`, size: Number(r.revenue), value: `${lei(r.revenue)} ${curOf(currency)}`,
            // the same name twice in a list is the same product in two units (or "Fără produs"): the unit tells them apart
            name: <>{rows.filter((x) => x.name === r.name && x.company === r.company).length > 1 ? `${r.name} (${unit(r.unit)})` : r.name}
              <span className="ml-1.5 text-muted">{names ? `${names[r.company] ?? r.company}, ` : ""}{nOf(r.invoice_count, "o factură", "facturi")}</span></>,
          }))} />
        </div>
      )) : <Empty />}
      {complete ? null : <p className="mt-3 mb-0 text-[12.5px] leading-snug text-muted">
        {`Lipsesc rândurile din ${nOf(missing, "o factură", "facturi")} din SPV.`}</p>}
    </section>
  );
}
