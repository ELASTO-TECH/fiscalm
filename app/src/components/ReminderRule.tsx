"use client";
import { Actions, Button, Form, Input, Label, rowStyle, rowTextStyle, rowIconStyle } from "./Controls";
// Automatizări › Încasări, Reamintiri de plată: when the engine offers a payment reminder for an overdue issued invoice (a
// Sarcină, then a draft a person sends), set per firm. The engine checks the numbers and saves all three or none.
import { Fragment, useState, type FormEvent } from "react";
import { post } from "@/lib/api";
import { nOf } from "@/lib/format";
import type { ReminderSettings, Rule } from "@/lib/types";
import { useApp, useCan } from "./context";
import { I, ROW_TINT, TONE_TEXT, ICON_TINT } from "./ui";

export const TITLE = "Reamintiri de plată";
export const ABOUT = "Copiază textul și trimite-l clientului din e-mail.";
const FIELDS: [keyof Omit<ReminderSettings, "source">, string, number][] = [
  ["after_days", "Prima reamintire, la câte zile după scadență", 365],
];

/** "Prima după 7 zile de la scadență, apoi la 14 zile, cel mult 3 pe factură." */
function when(s: ReminderSettings): string {
  return `Disponibilă după ${nOf(s.after_days, "o zi", "zile")} de la scadență.`;
}

export function ReminderRule({ r, why }: { r: Rule; today: string; why?: [string, string] }) {
  const { run, busy } = useApp();
  const allowed = useCan("manage_rules");
  const [form, setForm] = useState<Record<string, string> | null>(null);
  const s = r.settings && "after_days" in r.settings ? r.settings : null;
  const id = `rem-${r.company}`;
  // broken settings start empty: saving all three repairs them, never a silent default
  const open = () => setForm(Object.fromEntries(FIELDS.map(([k]) => [k, s ? String(s[k]) : ""])));
  const close = () => {
    setForm(null);
    setTimeout(() => document.getElementById(`${id}-edit`)?.focus());
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !form || !allowed) return;
    const body = Object.fromEntries(FIELDS.map(([k]) => [k, form[k].trim()]));
    const res = await run(r.company, () => post("rule-reminders", { co: r.company, ...body }), undefined, () => false);
    if (res?.ok) close();
  };
  return (
    <>
      <div className={`${rowStyle} flex-wrap sm:flex-nowrap`}>
        <span className={`${rowIconStyle} ${why?.[0] ?? ""} ${ROW_TINT[why?.[0] ?? ""] ?? ICON_TINT}`}><I n="clock" /></span>
        <span className={rowTextStyle}>
          <b>{TITLE}</b>
          <small className="text-muted">{ABOUT}</small>
          {s ? <small className="text-muted">{when(s)}</small> : null}
          {why ? <small className={TONE_TEXT[why[0] as keyof typeof TONE_TEXT]}>{why[1]}</small> : null}
        </span>
        {allowed && r.can_edit && !form ? (
          <Button id={`${id}-edit`} type="button" variant="secondary" disabled={busy} aria-label={`Modifică regula ${TITLE}`}
            onClick={open}>Modifică</Button>
        ) : null}
      </div>
      {allowed && form ? (
        <Form className="border-0 border-t border-solid border-line bg-inset p-5 sm:p-6" onSubmit={save} aria-label={`Modifică regula ${TITLE}`}>
          {FIELDS.map(([k, label, max]) => (
            <Fragment key={k}>
              <Label htmlFor={`${id}-${k}`}>{label}</Label>
              <Input id={`${id}-${k}`} type="number" inputMode="numeric" min={1} max={max} step={1} required value={form[k]}
                onChange={(e) => setForm({ ...form, [k]: e.target.value })} autoFocus={k === "after_days"} />
            </Fragment>
          ))}
          <Actions>
            <Button type="submit" disabled={busy}>Salvează</Button>
            <Button type="button" variant="secondary" disabled={busy} onClick={close}>Renunță</Button>
          </Actions>
        </Form>
      ) : null}
    </>
  );
}
