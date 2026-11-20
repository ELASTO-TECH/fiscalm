"use client";
import { Button, Input, Label, Actions, Panel, rowStyle, rowTextStyle, rowIconStyle } from "./Controls";
// Recurring invoices (docs/openapi.json): a rule that issues an invoice each month from a saved draft.
// The engine computes the client, the total and the next invoice's day and issues with every check of a manual issue (a
// refusal leaves a draft and a task); the app shows them, stops, starts and deletes a rule, and makes one from a draft (Repetă lunar).
import { useState, type KeyboardEvent } from "react";
import { postInvoicing } from "@/lib/api";
import { cap, curOf, daysTo, dm, lei, luna, nOf, type Tone } from "@/lib/format";
import type { Draft, Recurrence } from "@/lib/types";
import { useApp, useCan, useCompany } from "./context";
import type { Ask } from "./Confirm";
import { I, ROW_TINT, TONE_TEXT, ICON_TINT } from "./ui";

/** Why the engine holds a rule; `manual` (stopped by someone) needs none. */
const WHY: Record<NonNullable<Recurrence["reason"]>, [Tone, string] | null> = {
  manual: null,
  missing_client: ["late", "Clientul regulii lipsește. Pentru o regulă nouă, alege un client existent."],
  missing_series: ["late", "Seria regulii nu mai există. După ce administratorul instalării reface aceeași serie, pornește regula."],
  invalid_template: ["late", "Ciorna regulii nu mai poate fi folosită. Creează o regulă nouă dintr-o ciornă salvată."],
  rate_unavailable: ["soon", "Cursul BNR nu a putut fi citit. Verificarea se reia automat."],
  unsupported_currency: ["late", "BNR nu publică un curs pentru această monedă. Pentru o regulă nouă, alege altă monedă."],
};

/** One recurring invoice: its client, amount and due days, its state and next draft, and Oprește / Pornește / Șterge when
 * The engine accepts them (`can_pause`, `can_resume`, `can_delete`). */
export function RecurrenceRow({ x, today }: { x: Recurrence; today: string }) {
  const { run, busy } = useApp();
  const allowed = useCan("member");
  const why = x.reason ? WHY[x.reason] : null;
  const tone: Tone = why?.[0] ?? (x.paused ? "" : "ok");
  const name = x.client ?? "Client șters";
  const amount = x.total ? `${lei(x.total)} ${curOf(x.template.currency)} pe lună` : "Factură lunară";
  const due = x.due_days ? `scadența la ${nOf(x.due_days, "o zi", "zile")}` : "scadența în ziua emiterii";
  const toggle = x.can_pause ? "Oprește" : x.can_resume ? "Pornește" : null;
  const state = [x.paused ? "Oprită" : "Pornită"];
  if (x.next_date) state.push(`Următoarea factură pe ${dm(x.next_date, today)}`);
  if (x.last_period) state.push(`Ultima pentru ${luna(x.last_period)}${x.last_invoice?.at.startsWith(x.last_period) ? `: ${x.last_invoice.number}` : ""}`);
  if (x.last_skipped) state.push(`${cap(nOf(x.last_skipped.count, "o lună", "luni"))} fără factură, până în ${luna(x.last_skipped.through_period)}`);
  const send = (op: "recurrence-pause" | "recurrence-resume" | "recurrence-delete", confirm?: Ask) =>
    run(x.company, () => postInvoicing(op, { co: x.company, id: x.id }), confirm, () => false);
  const remove = async () => {
    const r = await send("recurrence-delete", { text: `Ștergi factura lunară pentru ${name}? Facturile emise și ciornele rămân.`, yes: "Șterge factura lunară", danger: true });
    if (r?.ok) document.querySelector<HTMLElement>("h1")?.focus();
  };
  return (
    <div className={`row flex-wrap sm:flex-nowrap ${rowStyle}`}>
      <span className={`ic ${tone} ${rowIconStyle} ${ROW_TINT[tone] ?? ICON_TINT}`}><I n="invoice" /></span>
      <span className={`t ${rowTextStyle} basis-[calc(100%_-_3.25rem)] sm:basis-auto`}>
        <b>{name}</b>
        <small className="text-muted">{amount}, {due}.</small>
        <small className={tone === "ok" ? `ok ${TONE_TEXT.ok}` : TONE_TEXT[""]}>{state.length > 1 ? `${state.join(". ")}.` : state[0]}</small>
        {why ? <small className={`${why[0]} ${TONE_TEXT[why[0]]}`}>{why[1]}</small> : null}
      </span>
      <span className="ml-12 flex flex-wrap gap-2 sm:ml-auto sm:shrink-0">
        {allowed && toggle ? (
          <Button type="button" variant="secondary" disabled={busy} aria-label={`${toggle} factura lunară pentru ${name}`}
            onClick={() => send(x.can_pause ? "recurrence-pause" : "recurrence-resume")}>
            {toggle}
          </Button>
        ) : null}
        {allowed && x.can_delete ? (
          <Button type="button" variant="danger" disabled={busy} aria-label={`Șterge factura lunară pentru ${name}`} onClick={remove}>
            Șterge
          </Button>
        ) : null}
      </span>
    </div>
  );
}

const whole = (v: string) => (/^\d{1,3}$/.test(v.trim()) ? Number(v) : null);

/** Repetă lunar, under a saved draft: the day of the month, the due days and, if wanted, the first month. It sits inside
 * the invoice's form, so it is a group, not a form: Enter in its fields creates the rule, not saves the invoice. Its
 * buttons keep their own Enter. */
export function RepeatMonthly({ draft, onDone }: { draft: Draft; onDone: () => void }) {
  const s = useCompany();
  const { run, busy } = useApp();
  const [day, setDay] = useState(draft.issue_date.slice(8, 10).replace(/^0/, ""));
  const [dueDays, setDueDays] = useState(String(Math.max(0, daysTo(draft.due_date, draft.issue_date) ?? 0)));
  const [first, setFirst] = useState("");
  const ok = whole(day) !== null && whole(dueDays) !== null && (first === "" || /^\d{4}-\d{2}$/.test(first));
  const create = async () => {
    if (!ok || busy) return;
    const body = { co: s.co, draft_id: draft.id, day: whole(day), due_days: whole(dueDays), ...(first ? { first_period: first } : {}) };
    const r = await run(s.co, () => postInvoicing("recurrence-create", body), undefined, () => false);
    if (r?.ok) onDone();
  };
  const enter = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    void create();
  };
  return (
    <Panel id="repeat" className="my-5" role="group" aria-labelledby="repeat-h">
      <p id="repeat-h" className="mt-0 mb-2 text-base font-semibold">Repetă lunar</p>
      <div className="grid gap-x-4 sm:grid-cols-2">
        <div><Label htmlFor="repeat-day">Ziua din lună</Label>
        <Input id="repeat-day" value={day} onChange={(e) => setDay(e.target.value)} inputMode="numeric" autoComplete="off" onKeyDown={enter} /></div>
        <div><Label htmlFor="repeat-due">Scadență, în zile</Label>
        <Input id="repeat-due" value={dueDays} onChange={(e) => setDueDays(e.target.value)} inputMode="numeric" autoComplete="off" onKeyDown={enter} /></div>
        <div className="sm:col-span-2"><Label htmlFor="repeat-first">Prima lună (opțional)</Label>
        <Input id="repeat-first" type="month" value={first} onChange={(e) => setFirst(e.target.value)} onKeyDown={enter} /></div>
      </div>
      <Actions>
        <Button type="button" disabled={busy || !ok} onClick={create}>Creează regula</Button>
        <Button type="button" variant="secondary" onClick={onDone}>Renunță</Button>
      </Actions>
    </Panel>
  );
}
