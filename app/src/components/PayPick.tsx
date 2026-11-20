"use client";
// Choosing received invoices to pay in Cheltuieli › De plătit and Restante: a box on each invoice the engine lets the person
// act on, and one bar with what was chosen and what can be done with it.
import { useCallback, useState, type ReactNode } from "react";
import { nOf, cap } from "@/lib/format";
import { parts, type Invoice, type State } from "@/lib/types";
import { allowedInShadow } from "@/lib/api";
import { buttonStyle, buttonTone, Input, rowRef } from "./Controls";
import { useApp, useCan } from "./context";
import { AddToFile, totalOf, useCanFile } from "./AddToFile";
import { MarkPaid } from "./MarkPaid";

const key = (r: Invoice) => `${r.company}:${r.id}`;

/** The state of an invoice's own company in what the route shows (one firm, or all of them). */
export function useStateOf(): (r: Invoice) => State | undefined {
  const { view } = useApp();
  return useCallback((r: Invoice) => parts(view).find((x) => x.co === r.company), [view]);
}

/** Putting an invoice in a payment file follows the shared mode policy for its actions. */
const filesIn = (s: State | undefined) => !!s && (s.live || allowedInShadow("payment_file_add"));

/** Whether a person can choose it: open, and the engine lets them mark it paid or put it in a file. In a file already,
 * held or marked, it can't be chosen and its row says where it is. */
export const pickable = (r: Invoice, s: State | undefined) => r.status === "open" && (r.can_mark_paid || (filesIn(s) && r.can_approve));

/** The chosen invoices of one list (`place`: the firm and filter shown), by company and id, kept across
 * its pages. Another place starts with nothing chosen, for good: coming back does not bring an old choice back. `rows`:
 * the invoices the list just read; a chosen one is taken as read now, and dropped once the engine no longer lets this
 * person choose it (put in the file, held, marked meanwhile). The engine re-checks every POST. */
export function usePicked(place: string, rows: Invoice[]) {
  const [state, setState] = useState<{ place: string; picked: Map<string, Invoice> }>({ place, picked: new Map() });
  if (state.place !== place) setState({ place, picked: new Map() });
  const picked = state.place === place ? state.picked : new Map<string, Invoice>();
  const toggle = useCallback((r: Invoice, on: boolean) => setState((x) => {
    const next = new Map(x.place === place ? x.picked : []);
    if (on) next.set(key(r), r); else next.delete(key(r));
    return { place, picked: next };
  }), [place]);
  const clear = useCallback(() => setState({ place, picked: new Map() }), [place]);
  const stateOf = useStateOf(), write = useCan("member");
  const fresh = new Map(rows.map((r) => [key(r), r]));
  const list = [...picked.keys()].map((k) => fresh.get(k) ?? picked.get(k)!).filter((r) => write && pickable(r, stateOf(r)));
  const has = (r: Invoice) => list.some((x) => key(x) === key(r));
  return { has, toggle, clear, place, list };
}
export type Picked = ReturnType<typeof usePicked>;

/** One invoice's row with its box. `whole`: the row is not a link (inside a sheet), so all of it toggles the box;
 * otherwise the row still opens the invoice. */
export function PickRow({ r, sel, whole = false, children }: { r: Invoice; sel: Picked; whole?: boolean; children: ReactNode }) {
  const { busy } = useApp();
  const write = useCan("member"), s = useStateOf()(r);
  const can = write && pickable(r, s);
  const on = can && sel.has(r);
  const box = <Input type="checkbox" className="size-5! disabled:opacity-40" checked={on} disabled={!can || busy}
    aria-label={`Alege ${r.supplier}, nr. ${r.number}`} onChange={(e) => sel.toggle(r, e.target.checked)} />;
  const boxCell = "grid min-h-11 w-11 shrink-0 place-items-center self-stretch pl-3";
  const row = <div className="min-w-0 flex-1 [&>.row]:pl-2">{children}</div>;
  return whole
    ? <label ref={rowRef} className="m-0 flex min-w-0 cursor-pointer items-center has-disabled:cursor-default"><span className={boxCell}>{box}</span>{row}</label>
    : <div ref={rowRef} className="flex min-w-0 items-center"><label className={`${boxCell} cursor-pointer has-disabled:cursor-default`}>{box}</label>{row}</div>;
}

/** The chosen invoices' bar: ink on the page, so its controls draw their focus ring in the page's colour. */
export const barStyle = "[&_:focus-visible]:outline-canvas! fixed inset-x-2 bottom-[calc(var(--tabbar)+env(safe-area-inset-bottom)+8px)] z-[6] flex items-center gap-1 rounded-card bg-ink py-2 pr-1.5 pl-3 text-canvas shadow-pop min-[1100px]:bottom-6 min-[1100px]:left-[calc(var(--sp,0px)+24px)] min-[1100px]:right-6 min-[1100px]:mx-auto min-[1100px]:max-w-[720px] min-[1100px]:gap-3 min-[1100px]:pr-2";
const barButton = `${buttonStyle} px-2! whitespace-nowrap! text-[13px]! min-[1100px]:px-3!`;

/** The bar over the list while something is chosen, on one row: how many and how much, Marchează ca plătită (the
 * "Cum ai plătit?" sheet, when the engine lets each of them be marked) and Pune în fișier (the file chooser, for the
 * invoices the engine lets in a file; it names the others' reasons when it answers). */
export function PickedBar({ sel }: { sel: Picked }) {
  const { busy } = useApp();
  const stateOf = useStateOf();
  const s = sel.list[0] ? stateOf(sel.list[0]) : undefined;
  const files = useCanFile(s);
  const [marking, setMarking] = useState<Invoice[] | null>(null);
  const [filing, setFiling] = useState<Invoice[] | null>(null);
  const list = sel.list;
  const mark = list.length > 0 && list.every((r) => r.can_mark_paid);
  const approve = list.filter((r) => r.can_approve);
  return <>
    {list.length ? <div className={barStyle} role="region" aria-label="Facturi alese">
      <p className="m-0 min-w-0 flex-1 leading-tight" aria-live="polite">
        <b className="block truncate text-[13px]">{cap(nOf(list.length, "o factură", "facturi"))}</b>
        <small className="block text-[11.5px] tabular-nums tracking-tight [overflow-wrap:anywhere] opacity-75">{totalOf(list)}</small>
      </p>
      <button type="button" className={`${barButton} border-transparent bg-canvas/12 text-canvas hover:bg-canvas/20`} aria-haspopup="dialog"
        disabled={busy || !mark} onClick={() => setMarking(list)}>Marchează ca plătită</button>
      {files ? <button type="button" className={`${barButton} ${buttonTone.primary}`} aria-haspopup="dialog" disabled={busy || !approve.length}
        onClick={() => setFiling(approve)}>Pune în fișier</button> : null}
    </div> : null}
    {marking ? <MarkPaid invoices={marking} onDone={(r) => { setMarking(null); if (r) sel.clear(); }} /> : null}
    {filing && s ? <AddToFile s={s} invoices={filing} left={list.filter((r) => !filing.includes(r))} onDone={(sent) => { setFiling(null); if (sent) sel.clear(); }} /> : null}
  </>;
}
