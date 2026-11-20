"use client";
// "Cum ai plătit?": marking received invoices paid, with how the person paid. A fact, never proof: every marking stays
// "În curs de confirmare" until a statement or a mail proves it (the engine decides).
import { createPortal } from "react-dom";
import { useContext, useEffect, useId, useRef, useState } from "react";
import { post, SESSION_CHANGED, SESSION_ENDED } from "@/lib/api";
import { cap, curOf, lei, nOf } from "@/lib/format";
import type { CollectedMethod, Invoice, PaidMethod } from "@/lib/types";
import { Actions, Button, Input, Label, Select, useReason } from "./Controls";
import { IdentityContext, useApp, useCompany } from "./context";
import { CloseX, Sheet, SheetHead } from "./Sheets";

/** One way to pay or collect, as a sheet names it, with its short line and what proof closes a marking made that way. */
export type Method<T extends string> = { value: T; title: string; line: string; proof: string };

export const PAID_METHODS: Method<PaidMethod>[] = [
  { value: "bank", title: "Din contul firmei", line: "Transfer din aplicația băncii", proof: "Rămâne în curs de confirmare până apare în extras." },
  { value: "card", title: "Cu cardul firmei", line: "", proof: "Rămâne în curs de confirmare până apare în extrasul cardului." },
  { value: "cash", title: "Numerar", line: "Cu bon sau chitanță", proof: "Rămâne în curs de confirmare până apare bonul sau chitanța." },
  { value: "own", title: "Din fonduri proprii", line: "Decont de cheltuieli", proof: "Rămâne în curs de confirmare până apare bonul, chitanța sau e-mailul de plată." },
  { value: "other", title: "Altă metodă", line: "Compensare sau alt tip de plată", proof: "Rămâne în curs de confirmare până apare dovada plății." },
];
export const methodOf = (m: PaidMethod) => PAID_METHODS.find((x) => x.value === m)!;

/** How a client paid an issued invoice, in the order the maintainer set (2026-11-18). Only a statement credit closes a
 * marking; cash never reaches the statement, so it says so instead of promising proof. */
export const COLLECTED_METHODS: Method<CollectedMethod>[] = [
  { value: "bank", title: "Ordin de plată (OP)", line: "Transfer în contul firmei", proof: "Rămâne în curs de confirmare până apare încasarea în extras." },
  { value: "cash", title: "Numerar", line: "Cu chitanță sau bon", proof: "Rămâne în curs de confirmare. Încasările în numerar nu apar în extras." },
  { value: "card", title: "Card", line: "La POS sau online", proof: "Rămâne în curs de confirmare până apar banii în extras." },
  { value: "other", title: "Altă metodă", line: "Compensare sau alt tip de încasare", proof: "Rămâne în curs de confirmare până apare dovada încasării." },
];
export const collectedOf = (m: CollectedMethod) => COLLECTED_METHODS.find((x) => x.value === m)!;

/** The ways as one radio list, each with its title and short line; `line` replaces a way's line (the chosen card). */
export function MethodList<T extends string>({ methods, value, onChange, name, line }: {
  methods: Method<T>[]; value: T | null; onChange: (m: T) => void; name: string; line?: (m: Method<T>) => string;
}) {
  return <div className="overflow-hidden rounded-card border border-solid border-line">
    {methods.map((m, i) => <label key={m.value}
      className="flex cursor-pointer items-start gap-3 border-0 border-t border-solid border-line px-4 py-3 first:border-t-0 has-[:checked]:bg-selected">
      <Input type="radio" name={name} className="mt-1" checked={value === m.value} onChange={() => onChange(m.value)}
        data-autofocus={i === 0 ? true : undefined} />
      <span className="min-w-0">
        <span className="block font-semibold">{m.title}</span>
        <span className="block text-sm text-muted">{line ? line(m) : m.line}</span>
      </span>
    </label>)}
  </div>;
}
export const cardName = (last4: string) => `Card ••\u00a0${last4}`; // the digits never wrap away from their dots

/** What `MarkPaid` did: how many invoices were marked, the engine's refusals, the invoice whose answer didn't arrive (it
 * may have been recorded) and how many were not sent after it. */
export type MarkPaidResult = { marked: number; refused: string[]; unknown: string; unsent: number };

const name = (r: Invoice) => `${r.supplier}, nr. ${r.number}`;
const DETAIL_MAX = 200;

/** The sheet over everything else (on the page's body, so an open invoice sheet turns inert under it). `invoices`: one or
 * many, each posted with its own company: `withdraw` for one in the payment file, `mark-paid` otherwise, one after another,
 * stopping when an answer doesn't arrive or the sheet goes away. `onDone` runs once: null when closed without sending,
 * else the result. One invoice follows the usual rule after its answer (the sheet it was opened from closes); several stay
 * where they were, and the sheet shows what was refused before it closes. */
export function MarkPaid({ invoices, onDone }: { invoices: Invoice[]; onDone: (result: MarkPaidResult | null) => void }) {
  const s = useCompany(); const { run, busy } = useApp();
  const me = useContext(IdentityContext)?.identity.me.display_name ?? "";
  const [method, setMethod] = useState<PaidMethod | null>(null);
  const [card, setCard] = useState(s.firm_cards.length === 1 ? s.firm_cards[0] : "");
  const [who, setWho] = useState(me);
  const [note, setNote] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<MarkPaidResult | null>(null);
  const mounted = useRef(true), stopped = useRef(false);
  const ids = useId();
  // Another person's session (another tab signed in or out, or this one ended) stops it at once: nothing more is sent
  // with their cookie and this person's answer, and the sheet says what was sent and closes as usual.
  useEffect(() => {
    mounted.current = true;
    const stop = (e: Event) => { if (!(e instanceof StorageEvent) || e.key === SESSION_CHANGED || e.key === null) stopped.current = true; };
    window.addEventListener("storage", stop);
    window.addEventListener(SESSION_ENDED, stop);
    return () => { mounted.current = false; window.removeEventListener("storage", stop); window.removeEventListener(SESSION_ENDED, stop); };
  }, []);
  const detail = method === "card" ? card : method === "own" ? who.trim() : method === "other" ? note.trim() : "";
  const why = !method ? "Alege cum ai plătit."
    : method === "card" && s.firm_cards.length && !card ? "Alege cardul."
      : method === "own" && !detail ? "Scrie cine a plătit."
        : method === "other" && !detail ? "Scrie cum s-a plătit." : "";
  const reason = useReason(running ? "" : why);
  const one = invoices.length === 1;
  const send = async () => {
    if (!method || why || running) return;
    const out: MarkPaidResult = { marked: 0, refused: [], unknown: "", unsent: invoices.length };
    setRunning(true);
    for (const r of invoices) {
      if (!mounted.current || stopped.current) break; // the sheet went away (another firm, another page), the session changed
      const action = r.status === "linked" ? "withdraw" : "mark-paid";
      const answer = await run(r.company, () => post(action, { co: r.company, id: r.id, method, detail }), undefined,
        one ? undefined : () => false);
      out.unsent--;
      // No answer: the marking may have gone through. Say so, send nothing more; the state read again shows where it is.
      if (!answer) { out.unknown = name(r); break; }
      if (answer.ok) out.marked++;
      else out.refused.push(`${name(r)}: ${answer.message}`);
    }
    if (!mounted.current) return;
    setRunning(false);
    if (one || (!out.refused.length && !out.unknown && !out.unsent)) onDone(out);
    else setResult(out);
  };
  const close = () => onDone(result);
  const sub = one ? `${invoices[0].supplier}, ${lei(invoices[0].total)} ${curOf(invoices[0].currency)}` : cap(nOf(invoices.length, "o factură", "facturi"));
  return createPortal(
    <Sheet onClose={running ? () => undefined : close} label="Cum ai plătit?" cls="ask tall" busy={running}>
      <CloseX onClose={running ? () => undefined : close} />
      <SheetHead title="Cum ai plătit?" sub={sub} />
      {result ? <Outcome result={result} onClose={close} /> : <>
        <fieldset className="m-0 border-0 p-0" disabled={running}>
          <legend className="sr-only">Cum ai plătit?</legend>
          <MethodList methods={PAID_METHODS} value={method} onChange={setMethod} name={`${ids}-method`}
            line={(m) => m.value === "card" ? cardLine(s.firm_cards, card) : m.line} />
          {method === "card" && s.firm_cards.length > 1 ? <>
            <Label htmlFor={`${ids}-card`}>Cardul</Label>
            <Select id={`${ids}-card`} value={card} onChange={(e) => setCard(e.target.value)}>
              <option value="">Alege cardul</option>
              {s.firm_cards.map((c) => <option key={c} value={c}>{cardName(c)}</option>)}
            </Select>
          </> : null}
          {method === "own" ? <>
            <Label htmlFor={`${ids}-who`}>Cine a plătit</Label>
            <Input id={`${ids}-who`} value={who} onChange={(e) => setWho(e.target.value)} maxLength={DETAIL_MAX} autoComplete="name" />
          </> : null}
          {method === "other" ? <>
            <Label htmlFor={`${ids}-note`}>Cum s-a plătit</Label>
            <Input id={`${ids}-note`} value={note} onChange={(e) => setNote(e.target.value)} maxLength={DETAIL_MAX} autoComplete="off" />
          </> : null}
        </fieldset>
        <Actions>
          <Button type="button" className="btn w-full!" disabled={!!why || running || busy} aria-busy={running || undefined}
            aria-describedby={reason.id} onClick={() => void send()}>Marchează ca plătită</Button>
          {reason.line}
        </Actions>
        {invoices.some((r) => r.status === "linked") ? <p className="mt-3! text-muted">{one ? "Marcarea scoate factura din fișierul de plăți." : "Marcarea scoate din fișierul de plăți facturile aflate în el."}</p> : null}
        {method ? <p className="mt-2! text-muted">{methodOf(method).proof}</p> : null}
      </>}
    </Sheet>,
    document.body,
  );
}

/** The card choice's line: the firm's one card, or how many it has; nothing when none is on record yet. */
function cardLine(cards: string[], chosen: string): string {
  if (chosen) return cardName(chosen);
  return cards.length > 1 ? "Alege cardul" : "";
}

/** After several: how many were marked, which the engine refused and why, and an invoice whose answer didn't arrive. */
function Outcome({ result, onClose }: { result: MarkPaidResult; onClose: () => void }) {
  const marked = !result.marked ? "Nicio factură nu a fost marcată ca plătită."
    : `${cap(nOf(result.marked, "o factură", "facturi"))} ${result.marked === 1 ? "a fost marcată ca plătită" : "au fost marcate ca plătite"}.`;
  return <div role="status">
    <p>{marked}{result.unknown ? ` Nu se știe dacă a fost marcată ${result.unknown}. Verifică factura.` : ""}
      {result.unsent ? ` ${cap(nOf(result.unsent, "o factură", "facturi"))} ${result.unsent === 1 ? "nu a fost trimisă" : "nu au fost trimise"}.` : ""}</p>
    {result.refused.length ? <ul className="m-0 mt-2 pl-5 text-sm">{result.refused.map((x) => <li key={x}>{x}</li>)}</ul> : null}
    <Actions><Button type="button" className="btn g" data-autofocus onClick={onClose}>Închide</Button></Actions>
  </div>;
}
