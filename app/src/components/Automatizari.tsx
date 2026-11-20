"use client";
import { Actions, Button, Form, Label, Panel, Select, rowIconStyle, rowStyle, rowTextStyle, sectionHeadingStyle, settingsPageStyle } from "./Controls";
// Automatizări: the engine's rules (`rules` in api/state), grouped by what they prepare, each with its state, what it last did and
// why it is held. The engine decides every state, reason and whether a rule can be stopped or started; the app only words them.
import { useState, type FormEvent, type ReactNode } from "react";
import { post } from "@/lib/api";
import { dm, nOf, type Tone } from "@/lib/format";
import type { Route } from "@/lib/nav";
import { coName, isAll, parts, type Rule, type State } from "@/lib/types";
import { useApp, useCan } from "./context";
import { REFUSED_WHY } from "./derive";
import { RecurrenceRow } from "./Recurring";
import { ABOUT as REMINDERS_ABOUT, ReminderRule, TITLE as REMINDERS } from "./ReminderRule";
import { RememberedList } from "./RememberedList";
import { I, L, ROW_TINT, TONE_TEXT, Title, ICON_TINT } from "./ui";

/** A rule's row that opens where it is set: its focus ring inside, like `Row`'s. */
const linkRow = `${rowStyle} flex-wrap text-ink sm:flex-nowrap hover:bg-inset focus-visible:rounded-card focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-focus`;

/** The page's groups, in order, and the kinds each one holds. */
const GROUPS: [string, Rule["kind"][]][] = [
  ["Facturare", ["spv_auto_upload", "shop_auto_issue"]], ["Încasări", ["client_reminders"]], ["Plăți", ["supplier_autopay"]], ["Documente", ["accountant_pack", "receipt_upload"]],
];

/** Why the engine holds a rule, in words; `paused` needs none (the state says Oprită), an unknown code shows nothing. */
const WHY: Partial<Record<NonNullable<Rule["reason"]>, [Tone, string]>> = {
  shadow_mode: ["", "Activă doar în modul Live."],
  anaf_not_connected: ["", "Lipsește conexiunea la ANAF."],
  supplier_inactive: ["late", REFUSED_WHY.supplier_inactive[1]],
  invoice_refused: ["late", "O factură a furnizorului nu intră în fișierul de plăți. Motivul e pe factură."],
  revoked: ["", "Linkul a fost revocat."],
  expired: ["", "Linkul a expirat."],
  exhausted: ["", "Limita de fișiere a fost atinsă."],
  invalid_settings: ["late", "Setările reamintirilor nu sunt valide. Până sunt salvate din nou, nu se pregătește nicio reamintire."],
};

type Shown = { icon: string; title: string; about: string; mark?: string; to?: Partial<Route> };

function shown(r: Rule): Shown {
  switch (r.kind) {
    case "spv_auto_upload":
      return { icon: "paper-plane-tilt", title: "Trimitere automată în SPV", about: "Fiecare factură emisă se trimite în SPV." };
    case "shop_auto_issue":
      return { icon: "plugs", title: `Emitere automată, ${r.label || "magazin"}`, about: "Comenzile din magazin devin facturi emise.",
        to: { co: r.company, tab: "setari", sec: "integrari", int: r.id.split(":")[2] } };
    case "accountant_pack":
      return { icon: "envelope-simple", title: "Dosarul lunar pentru contabil", about: "Descarcă dosarul și trimite-l contabilului prin e-mail.",
        to: { co: r.company, tab: "rapoarte" } };
    case "receipt_upload":
      return { icon: "link-simple", title: r.label || "Bondrop", mark: "Bondrop", about: "Bonurile trimise de pe telefon ajung direct la firmă.",
        to: { co: r.company, tab: "setari", sec: "acces" } };
    case "supplier_autopay":
      return { icon: "currency-circle-dollar", title: r.label || "Furnizor", about: "Facturile noi se adaugă în fișierul de plăți." };
    case "client_reminders":
      return { icon: "clock", title: REMINDERS, about: REMINDERS_ABOUT };
  }
}

/** Reasons that keep the whole rule from running: it is never "Pornită" then, its reason says what it waits for. */
const WAITS: Rule["reason"][] = ["shadow_mode", "anaf_not_connected"];

/** The rule's state, then what waits on it and what it last did: "Oprită. 3 facturi așteaptă trimiterea manuală." Empty
 * for a rule that waits with nothing else to say. */
function status(r: Rule, today: string): string {
  const xs = [r.enabled ? WAITS.includes(r.reason) ? "" : "Pornită" : "Oprită"].filter(Boolean);
  if (r.waiting_manual) xs.push(`${nOf(r.waiting_manual, "O factură așteaptă", "facturi așteaptă")} trimiterea manuală`);
  if (r.waiting_queue) xs.push(`La pornire, ${nOf(r.waiting_queue, "o factură intră", "facturi intră")} în fișierul de plăți`);
  if (r.settings && "upload_after_days" in r.settings) xs.push(`Se trimite ${sendDay(r.settings.upload_after_days)}`);
  const last = r.last;
  if (last?.count && r.kind === "accountant_pack") {
    xs.push(nOf(last.count, "Un dosar pregătit", "dosare pregătite") + (last.date ? `, ultimul pe ${dm(last.date, today)}` : ""));
  }
  if (last?.count && r.kind === "receipt_upload") {
    xs.push(nOf(last.count, "Un fișier primit", "fișiere primite") + (last.date ? `, ultimul pe ${dm(last.date, today)}` : ""));
  }
  return xs.length > 1 || r.settings && "upload_after_days" in r.settings ? `${xs.join(". ")}.` : xs[0] ?? "";
}

/** When the SPV rule sends an invoice, as its row and its choice say it. */
const sendDay = (n: number) => n === 0 ? "la prima verificare după emitere" : n === 1 ? "la o zi după emitere" : `la ${n} zile după emitere`;

/** One rule: its name, what it does, its state and why it is held, Oprește or Pornește when the engine allows it, and
 * Modifică when it has settings to change (the SPV rule only while it runs: off, its day has no effect). */
function RuleRow({ r, today }: { r: Rule; today: string }) {
  const [editing, setEditing] = useState(false);
  const { run, busy } = useApp();
  const x = shown(r);
  const why = r.reason ? WHY[r.reason] : undefined;
  const tone: Tone = why?.[0] === "late" ? "late" : r.enabled && !r.reason ? "ok" : "";
  const allowed = useCan("manage_rules"), integrate = useCan("manage_integrations"), mode = useCan("manage_firms");
  const days = r.settings && "upload_after_days" in r.settings ? r.settings.upload_after_days : null;
  const edit = allowed && !!r.can_edit && days !== null && !editing;
  const change = allowed && (r.can_pause || r.can_resume);
  const verb = r.can_pause ? "Oprește" : "Pornește";
  const inner = (
    <>
      <span className={`${rowIconStyle} ${tone} ${ROW_TINT[tone] ?? ICON_TINT}`}><I n={x.icon} /></span>
      <span className={rowTextStyle}>
        <b>{x.title}</b>
        <small className="text-muted">{x.mark ? <><span className="mr-1.5 inline-block rounded-control bg-inset px-1.5 align-[1px] text-xs font-semibold leading-5 text-ink">{x.mark}</span><span className="sr-only">, </span></> : null}{x.about}</small>
        {status(r, today) ? <small className={TONE_TEXT[tone === "ok" ? "ok" : ""]}>{status(r, today)}</small> : null}
        {why ? <small className={TONE_TEXT[why[0]]}>{why[1]}{r.reason === "anaf_not_connected" && integrate ? <>{" "}
          <L to={{ tab: "setari", sec: "integrari" }} at="k-anaf" className="font-semibold text-ink underline">Conectează ANAF</L></> : null}
          {r.reason === "shadow_mode" && mode ? <>{" "}<L to={{ tab: "setari", sec: "mod" }} className="font-semibold text-ink underline">Setări › Mod</L></> : null}</small> : null}
      </span>
      {edit || change ? <span className={edit && change ? "flex w-full shrink-0 gap-2 pl-12 sm:w-auto sm:pl-0" : "contents"}>
        {edit ? <Button id={`spv-${r.company}-edit`} type="button" variant="secondary" disabled={busy} aria-label={`Modifică regula ${x.title}`}
          onClick={() => setEditing(true)}>Modifică</Button> : null}
        {change ? (
          <Button type="button" variant="secondary" disabled={busy} aria-label={`${verb} regula ${x.title}`}
            onClick={() => run(r.company, () => post(r.can_pause ? "rule-pause" : "rule-resume", { co: r.company, id: r.id }), undefined, () => false)}>
            {verb}
          </Button>
        ) : null}
      </span> : null}
    </>
  );
  const row = x.to && !change ? <L to={x.to} className={linkRow}>{inner}</L> : <div className={`${rowStyle} flex-wrap sm:flex-nowrap`}>{inner}</div>;
  if (!editing || days === null) return row;
  return <>{row}<SpvDay r={r} days={days} onClose={() => {
    setEditing(false);
    setTimeout(() => document.getElementById(`spv-${r.company}-edit`)?.focus());
  }} /></>;
}

/** The SPV rule's day, chosen under its row; the engine refuses it while the rule is off. */
function SpvDay({ r, days, onClose }: { r: Rule; days: number; onClose: () => void }) {
  const { run, busy } = useApp();
  const [day, setDay] = useState(String(days));
  const id = `spv-${r.company}-day`;
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const res = await run(r.company, () => post("rule-spv-delay", { co: r.company, days: day }), undefined, () => false);
    if (res?.ok) onClose();
  };
  return (
    <Form className="border-0 border-t border-solid border-line bg-inset p-5 sm:p-6" onSubmit={save} aria-label="Modifică regula Trimitere automată în SPV">
      <Label htmlFor={id}>Când se trimite</Label>
      <Select id={id} value={day} disabled={busy} onChange={(e) => setDay(e.target.value)}>
        {[0, 1, 2, 3, 4, 5].map((n) => <option key={n} value={String(n)}>{sendDay(n).replace(/^la/, "La")}</option>)}
      </Select>
      <p className="mt-2 text-sm text-muted">Zilele până la trimitere lasă timp pentru corecturi. Toate sunt în termenul de 5 zile lucrătoare.</p>
      <Actions>
        <Button type="submit" disabled={busy}>Salvează</Button>
        <Button type="button" variant="secondary" disabled={busy} onClick={onClose}>Renunță</Button>
      </Actions>
    </Form>
  );
}

/** Bondrop before its first link: where links are made. */
function NoLinks({ co }: { co: string }) {
  const allowed = useCan("member");
  if (!allowed) return null;
  return (
    <L to={{ co, tab: "setari", sec: "acces" }} className={linkRow}>
      <span className={`${rowIconStyle} ${ICON_TINT}`}><I n="link-simple" /></span>
      <span className={rowTextStyle}><b>Bondrop</b><small className="text-muted">Bonurile trimise de pe telefon ajung direct la firmă.</small></span>
      <span className="shrink-0 text-right text-sm font-semibold text-muted">Setări</span>
    </L>
  );
}

/** Recurring invoices start from a saved draft (its sheet has Repetă lunar): the way to Vânzări's drafts. */
function NewRecurring({ co }: { co: string }) {
  const allowed = useCan("member");
  if (!allowed) return null;
  return (
    <L to={{ co, tab: "vanzari" }} at="drafts-h" className={linkRow}>
      <span className={`${rowIconStyle} ${ICON_TINT}`}><I n="plus" /></span>
      <span className={rowTextStyle}><b>Factură lunară nouă</b><small className="text-muted">Dintr-o ciornă, cu Repetă lunar</small></span>
    </L>
  );
}

/** A company's rules by group, Facturare with its recurring invoices; a group with nothing in it is left out, except
 * Facturare and Documente's ways to a new recurring invoice and to Bondrop. */
function Groups({ s, head }: { s: State; head: (name: string, id: string) => ReactNode }) {
  const kept = (s.record_counts?.remembered ?? s.transactions.remembered.length) ? (
    <section key="kept" aria-labelledby={`r-${s.co}-kept`}>
      {head("Reguli salvate", `r-${s.co}-kept`)}
      <RememberedList s={s} />
    </section>
  ) : null;
  return [...GROUPS.map(([name, kinds]) => {
    const rules = s.rules.filter((r) => kinds.includes(r.kind));
    const invoicing = kinds.includes("spv_auto_upload");
    const bondrop = kinds.includes("receipt_upload") && !rules.some((r) => r.kind === "receipt_upload");
    if (!rules.length && !bondrop && !invoicing) return null;
    const id = `r-${s.co}-${name}`;
    return (
      <section key={name} aria-labelledby={id}>
        {head(name, id)}
        <Panel padded={false}>
          {rules.map((r) => (r.kind === "client_reminders"
            ? <ReminderRule key={r.id} r={r} today={s.today} why={r.reason ? WHY[r.reason] : undefined} />
            : <RuleRow key={r.id} r={r} today={s.today} />))}
          {invoicing ? s.recurrences.map((x) => <RecurrenceRow key={x.id} x={x} today={s.today} />) : null}
          {invoicing ? <NewRecurring co={s.co} /> : null}
          {bondrop ? <NoLinks co={s.co} /> : null}
        </Panel>
      </section>
    );
  }), kept];
}

export function Automatizari() {
  const { view } = useApp();
  const all = isAll(view);
  return (
    <div className={settingsPageStyle}>
      <Title>Automatizări</Title>
      {all ? parts(view).map((s) => (
        <section key={s.co} aria-labelledby={`r-${s.co}`}>
          <div className={sectionHeadingStyle}><h2 id={`r-${s.co}`}>{coName(s)}</h2></div>
          <Groups s={s} head={(name, id) => <h3 id={id} className="mb-3 mt-6 text-base font-semibold">{name}</h3>} />
        </section>
      )) : <Groups s={view} head={(name, id) => <div className={sectionHeadingStyle}><h2 id={id}>{name}</h2></div>} />}
    </div>
  );
}
