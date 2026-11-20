"use client";
// Setări's configuration pages: Integrări (any number of outside connections, keys write-only); Acces is in Access.tsx.
import { Button, Input, Select, Label, Form, Panel, Actions, Skeleton, sectionHeadingStyle, backLinkStyle, bannerStyle, noteLeftStyle, settingsPageStyle, surfaceStyle, useReason } from "./Controls";
import { useState } from "react";
import { useLoaded } from "./Paging";
import { getIntegrations, post } from "@/lib/api";
import { cap, dm, dmt, nOf, type Tone } from "@/lib/format";
import type { Companies, Integration, IntegrationField, IntegrationKind, Integrations, State } from "@/lib/types";
import { OpenBankingSetup } from "./OpenBankingSetup";
import { MailSheet, mailLine } from "./MailSetup";
import { FirmScopeField, anafWarnings, scopeBody, scopeLine, scopeReason } from "./FirmScope";

import { AI_CHOICES, AiCli, SubscriptionNote } from "./Ai";
import { Backup } from "./Backup";
import { AnafConnect, AnafRedirectField, AnafSetupGuide, connectionLine } from "./AnafConnect";
import { anafExpiringSoon } from "./derive";
import { useApp, useCan, useCompany } from "./context";
import { CloseX, Sheet, SheetHead } from "./Sheets";
import { Act, Empty, I, L, Loading, Row, Title, Summary } from "./ui";

const KIND_ICON: Record<string, string> = {
  anaf: "link-simple", ai: "sparkle", mail: "envelope-simple", storage: "hard-drives",
};

/** How a choice reads in the app (the value stays what the engine stores). */
const CHOICE: Record<string, string> = {
  ...AI_CHOICES, prod: "Producție", test: "Test",
  woocommerce: "WooCommerce", prestashop: "PrestaShop", shopify: "Shopify", emag: "eMAG", custom: "Alt magazin",
};
const choice = (v: string) => CHOICE[v] ?? v;

/** Integrări, read again whenever the app's state changes (after every action). `fresh` is false while a newer read
 * is on its way or after it failed: a form must not save over data that may be old. */
export function useIntegrations(): { data: Integrations | null; failed: boolean; fresh: boolean; reload: () => void } {
  const { view } = useApp();
  // A new reader (a sheet opening) waits for its own read; no data survives the app's session.
  const allowed = useCan("manage_integrations");
  const { data, failed, current, reload } = useLoaded(() => (allowed ? getIntegrations() : Promise.resolve(null)), allowed, view);
  return { data: allowed ? data ?? null : null, failed: allowed && failed, fresh: allowed && !!data && current && !failed, reload };
}

/** Whether any of the engine's clauses holds for these values ("" = empty). */
const holds = (clauses: Record<string, string[]>[], values: Record<string, string>) =>
  clauses.some((c) => Object.entries(c).every(([k, vals]) => vals.includes((values[k] ?? "").trim())));

/** Whether a field must be filled for these values (the engine's own rule, from its `required_when` clauses). */
export const needed = (f: IntegrationField, values: Record<string, string>) => f.required || holds(f.required_when, values);

/** The fields that apply to these values, and are sent: those the engine asks for now (a shop's platform fields once
 * the platform is chosen); the CLI-only provider has no key or address. */
function shown(kind: IntegrationKind, values: Record<string, string>): IntegrationField[] {
  if (kind.key === "ai" && values.provider?.endsWith("-cli")) return kind.fields.filter((f) => f.key !== "api_key" && f.key !== "base_url");
  return kind.fields.filter((f) => !f.shown_when.length || holds(f.shown_when, values));
}

/** What a row says under an integration's name, in words: default or off, what it is, a key that is missing. Never a raw
 * config value (an rclone remote, a firm's key) and never a secret. */
function summary(kind: IntegrationKind, it: Integration, several: boolean, today: string, live: boolean, firms: Companies): [string, Tone] {
  if (kind.key === "mail") return mailLine(it, today);
  if (it.sync_error) return ["Conexiunea eMAG nu a putut fi verificată. Verificarea se reia la următoarea rulare.", "soon"];
  const missing = shown(kind, it.config).filter((f) => f.secret && needed(f, it.config) && !it.secrets[f.key]).map((f) => f.label);
  if (missing.length) return [`Lipsește: ${missing.join(", ")}`, "soon"];
  const read = (k: string) => {
    const v = it.config[k];
    return v ? kind.fields.find((f) => f.key === k)?.choice_labels?.[v] ?? choice(v) : "";
  };
  const firm = it.config.company && (firms.find((c) => c.key === it.config.company)?.name ?? it.config.company);
  const what = [kind.key === "storage" ? "Google Drive" : "", read("provider"), read("platform"), it.config.model,
    it.config.host, it.config.url && hostOf(it.config.url), firm && `firma ${firm}`,
    kind.key === "anaf" ? live ? "ANAF Producție" : "ANAF Test" : ""].filter(Boolean).join(", ");
  // ANAF has no default: the engine picks each firm's connection (coverage).
  const state = !it.enabled ? "Oprită" : it.default && several && kind.key !== "anaf" ? "Implicită" : "";
  const c = it.connection;
  if (c && it.enabled) { // ANAF: whether it is connected says more than what it points at
    const [tone, title] = connectionLine(c, today);
    const until = (c.state === "connected" || c.state === "expiring") && c.expires ? `${title}, până pe ${dm(c.expires, today)}` : title;
    return [[state, until, what].filter(Boolean).join(". "), tone];
  }
  return [[state, what].filter(Boolean).join(". "), it.enabled ? "ok" : ""];
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/** What Integrări's row on Setări says: the connections that need a person, worst first, else how many there are. */
function integrationsLine(s: State, n: number | null): [string, Tone | ""] {
  const { anaf: a, mail: m } = s.system;
  const late = [a.status === "expired" ? "Conexiunea ANAF a expirat" : "",
    a.status === "error" || (a.status === "ok" && !a.authorized) ? "Eroare la citirea facturilor din ANAF" : "",
    m.state === "error" ? "E-mailul nu poate fi citit" : m.stale ? "Sincronizare e-mail întârziată" : ""].filter(Boolean);
  // red only for what fails until a person acts; a read that retries on its own or a delay is amber
  const broken = a.status === "expired" || a.reconnect || m.reconnect;
  if (late.length) return [late.join(". "), broken ? "late" : "soon"];
  if (anafExpiringSoon(a)) return [`Conexiunea ANAF expiră pe ${dm(a.expires, s.today)}`, "soon"];
  return [n === null ? "" : n ? cap(nOf(n, "o integrare", "integrări")) : "Nicio integrare", ""];
}

/** Setări's menu of configuration pages. */
export function ConfigCard() {
  const { data, failed } = useIntegrations();
  const firms = useCan("manage_firms"), integrations = useCan("manage_integrations"), users = useCan("manage_users");
  const { view } = useApp();
  const s = useCompany();
  const companies = view.companies; // the configured companies, from the state the app already has
  const [intSub, intTone] = integrationsLine(s, data ? data.items.length : null);
  return (
    <>
      <div className={sectionHeadingStyle}><h2>Configurare</h2></div>
      <Panel padded={false}>
        {firms && <Row icon="sliders" title="Mod" sub={view.live ? "Live" : "Sandbox"} to={{ tab: "setari", sec: "mod" }} />}
        <Row icon="user" title="Cont" sub="Autentificare și sesiuni" to={{ tab: "setari", sec: "cont" }} />
        {firms && <Row icon="bank" title="Firme" sub={companies.length ? cap(nOf(companies.length, "o firmă", "firme")) : ""}
          to={{ tab: "setari", sec: "firme" }} />}
        <Row icon="calculator" title="Contabil" sub="Partener și adrese de e-mail" to={{ tab: "setari", sec: "contabil" }} />
        <Row icon="receipt" title="Facturare" sub="Serii de facturi" to={{ tab: "setari", sec: "facturare" }} />
        {integrations && <Row icon="plugs" title="Integrări" sub={intSub || (data || failed ? "" : <Skeleton className="my-1 h-3 w-24" />)} subcls={intTone} badge={intTone}
          to={{ tab: "setari", sec: "integrari" }} />}
        {users && <Row icon="user" title="Utilizatori" sub="Conturi și roluri pentru toate firmele" to={{ tab: "setari", sec: "utilizatori" }} />}
        <Row icon="link-simple" title="Linkuri și chei API" sub="Bondrop și alte aplicații" to={{ tab: "setari", sec: "acces" }} />
      </Panel>
    </>
  );
}

/** Setări's last group: the version; for administrators also the release check, the last check, backups and the log. */
export function SystemCard() {
  const s = useCompany();
  const { run, busy } = useApp();
  const firms = useCan("manage_firms");
  const update = s.system.updates;
  return (
    <section aria-labelledby="sistem">
      <div className={sectionHeadingStyle}><h2 id="sistem">Sistem</h2></div>
      {firms && update?.release && <div className={`${bannerStyle} space-y-2`} role="status">
        <b>Versiune nouă disponibilă</b>
        <p>fiscalm {update.release.tag_name}. Actualizarea se pornește manual, pe server.</p>
        <a href={update.release.html_url} target="_blank" rel="noopener noreferrer">Noutățile versiunii</a>
        <p className="[overflow-wrap:anywhere]"><code>{update.command}</code></p>
      </div>}
      <Panel padded={false}>
        <Row icon="gear-six" title="Versiune" sub={`fiscalm ${s.system.version}`} />
        {update && firms && <div className="-mt-1 pr-5 pb-3 pl-16">
          <Label><Input type="checkbox" role="switch" checked={update.enabled} disabled={busy}
            onChange={(e) => void run(s.co, () => post("update-checks", { co: s.co, on: e.target.checked ? "1" : "0" }), undefined, () => false)} />
            Verifică versiunile noi</Label>
        </div>}
        {firms && <Row icon="clock" title="Ultima verificare" sub={s.system.last_run ? dmt(s.system.last_run, s.today) : "Nicio verificare încă"} />}
        {firms && <div className="-mt-1 pr-5 pb-4 pl-16"><Act action="check" body={{}} label="Verifică acum" cls="btn g" /></div>}
      </Panel>
      {firms && <Backup />}
      {firms && <details className={`${surfaceStyle} mt-3 mb-6 p-5`}>
        <Summary>Jurnalul verificărilor</Summary>
        <pre className="mt-3 mb-0 font-mono text-muted max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed">{s.system.log.join("\n")}</pre>
      </details>}
    </section>
  );
}

export function Integrari() {
  const { data, failed, reload } = useIntegrations();
  const { view } = useApp();
  return (
    <div className={settingsPageStyle}>
      <L to={{ tab: "setari" }} className={backLinkStyle} label="Înapoi la Setări"><I n="arrow-left" />Setări</L>
      <Title>Integrări</Title>
      {!data && !failed ? <Loading /> : null}
      {failed ? (
        <div className={bannerStyle} role="alert">
          <b>{data ? "Integrările nu au putut fi actualizate. Datele afișate pot fi vechi." : "Integrările nu au putut fi încărcate."}</b>
          <Button type="button" variant="secondary" onClick={reload}>Reîncearcă</Button>
        </div>
      ) : null}
      {data?.items.some((i) => i.kind === "ai" && i.enabled && i.default && i.config.provider?.endsWith("-cli")) ? <div className={`${bannerStyle} [&_p]:m-0`}><SubscriptionNote /></div> : null}
      {data?.kinds.map((k) => {
        const items = data.items.filter((i) => i.kind === k.key);
        const warnings = k.key === "anaf" ? anafWarnings(data.coverage, data.items, view.companies) : [];
        return (
          <section key={k.key} aria-labelledby={`k-${k.key}`}>
            <div className={sectionHeadingStyle}>
              <h2 id={`k-${k.key}`}>{k.label}</h2>
              {(!k.single || !items.length) && (k.key !== "ai" || data.can_manage_ai !== false)
                ? <L to={{ int: `new:${k.key}` }} label={`Adaugă: ${k.label}`} className="btn g !text-ink">
                  {k.key === "mail" && items.length ? "Adaugă încă o adresă" : "Adaugă"}</L> : null}
            </div>
            {warnings.length ? <div className={bannerStyle} role="status">
              <ul className="m-0 flex list-none flex-col gap-2 p-0">{warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            </div> : null}
            <Panel padded={false}>
              {items.length ? items.map((it) => {
                const [line, c] = summary(k, it, items.length > 1, view.today, data.live, view.companies);
                const sub = [line, scopeLine(it.firm_scope, view.companies)].filter(Boolean).join(". ");
                return <Row key={it.id} icon={KIND_ICON[k.key] ?? "plugs"} title={it.name} sub={sub} subcls={c}
                  badge={c === "late" || c === "soon" ? c : ""} to={{ int: it.id }} />;
              }) : <Empty />}
            </Panel>
          </section>
        );
      })}
    </div>
  );
}

/** An integration's sheet, mounted by App next to the other sheets (so the page behind it is inert). The form is keyed by
 * the instance's last change, so fresh data replaces the fields; it can't save while a newer read is on its way. */
export function IntegrationSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const { data, fresh, failed, reload } = useIntegrations();
  const adding = id.startsWith("new:");
  const item = adding ? data?.items.find((i) => i.kind === "openbanking" && id === `new:${i.kind}`) : data?.items.find((i) => i.id === id);
  const kind = data?.kinds.find((k) => k.key === (adding ? id.slice(4) : item?.kind));
  if (!kind || (!adding && !item)) return (
    <Sheet onClose={onClose} label="Integrare">
      <CloseX onClose={onClose} />
      <h2>Integrare</h2>
      {failed ? <div className={bannerStyle} role="alert">
        <b>Integrările nu au putut fi încărcate.</b>
        <Button type="button" variant="secondary" onClick={(e) => { e.currentTarget.closest<HTMLElement>('[role="dialog"]')?.focus(); reload(); }}>Reîncearcă</Button>
      </div> : fresh ? <p className={noteLeftStyle} role="status">{adding ? "Acest tip de integrare nu este disponibil." : "Integrarea nu mai este disponibilă."}</p> : <Loading />}
    </Sheet>
  );
  if (kind.key === "openbanking") return <OpenBankingSetup item={item} fresh={fresh} onClose={onClose} known={kind.fields.find((f) => f.key === "return_url")?.default} />;

  if (kind.key === "ai" && data?.can_manage_ai === false) return <Sheet onClose={onClose} label="Serviciu AI">
    <CloseX onClose={onClose} /><SheetHead title="Serviciu AI" /><p>Administratorul instalării configurează serviciul AI.</p>
    {item ? <p>{AI_CHOICES[item.config.provider]}</p> : null}
  </Sheet>;
  // Keyed by what the server holds, not by when it changed (a timestamp has one-second steps): any change resets it.
  const version = item ? JSON.stringify([item.name, item.config, item.secrets, item.enabled, item.firm_scope]) : "";
  if (kind.key === "mail") return <MailSheet key={`${id}:${version}`} item={item} fresh={fresh} onClose={onClose}
    actions={item ? <ItemActions kind={kind} item={item} /> : null} />;
  const others = data?.items.filter((i) => i.kind === kind.key).length ?? 0;
  return <IntegrationForm key={`${id}:${version}`} kind={kind} item={item} others={others} onClose={onClose} fresh={fresh}
    failed={failed} reload={reload} />;
}

function IntegrationForm({ kind, item, others, onClose, fresh, failed, reload }: {
  kind: IntegrationKind; item?: Integration; others: number; onClose: () => void; fresh: boolean; failed: boolean; reload: () => void;
}) {
  const { run, busy, view, route } = useApp();
  const company = useCompany();
  const permitted = useCan("manage_integrations");
  // A second ANAF connection is named on adding, so the two can be told apart in Integrări and Firme.
  const naming = !!item || (kind.key === "anaf" && others > 0);
  const [name, setName] = useState(item?.name ?? (naming ? `${kind.label} ${others + 1}` : kind.label));
  const scoped = kind.key === "anaf";
  const [scope, setScope] = useState(item?.firm_scope ?? "all");
  const own = company.co !== "ALL" ? company.co : "";
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(
    kind.fields.map((f) => [f.key, f.secret ? "" : item ? item.config[f.key] || (f.known ? f.default : "")
      : f.type === "company" ? own : f.default])));
  const fields = shown(kind, values);
  // Not asked: a new integration's firm (the one on screen) and an address the install knows. A saved one can move.
  const asked = fields.filter((f) => !(f.type === "company" && own && !item) && !(f.known && values[f.key] === f.default))
    .map((f) => f.type !== "company" ? f : { ...f, choices: view.companies.map((c) => c.key),
      choice_labels: Object.fromEntries(view.companies.map((c) => [c.key, c.name])) });
  const set = (k: string, v: string) => setValues((x) => ({ ...x, [k]: v }));
  // Only the fields that apply, and a secret only when typed (empty keeps the one saved); the name is asked only when
  // it is changed later.
  const body: Record<string, unknown> = { name, ...(scoped ? scopeBody(scope, view.companies, item?.firm_scope) : {}) };
  for (const f of fields) if (!f.secret || values[f.key]) body[`f_${f.key}`] = values[f.key];
  const empty = fields.find((f) => needed(f, values) && !values[f.key]?.trim() && !(f.secret && item?.secrets[f.key]));
  const noFirm = scoped ? scopeReason(scope, view.companies, item?.firm_scope) : "";
  const ready = fresh && name.trim() !== "" && !empty && !noFirm;
  const reason = useReason(!fresh || !permitted ? "" : !name.trim() ? "Completează „Nume”."
    : empty ? `${empty.choices.length ? "Alege" : "Completează"} „${empty.label}”.` : noFirm);
  const save = () => ready && permitted && !busy && run(company.co, () => post(item ? "integration-update" : "integration-add", {
    co: company.co, ...(item ? { id: item.id } : { kind: kind.key }), ...body,
  }), undefined, kind.key !== "anaf" ? () => { onClose(); return false; }
    : (r) => !item && r.done?.id ? { ...route, int: r.done.id } : false); // a new ANAF connection opens on its Conectează step
  const cli = kind.key === "ai" && values.provider?.endsWith("-cli");
  const actions = (
    <Actions>
      {permitted ? <Button type="submit" disabled={!ready || busy} aria-describedby={reason.id}>
        {item ? "Salvează" : kind.key === "anaf" ? "Salvează și continuă" : "Adaugă integrarea"}
      </Button> : null}
      {reason.line}
    </Actions>
  );
  const form = (
    <Form onSubmit={(e) => { e.preventDefault(); void save(); }} autoComplete="off">
      {naming ? <><Label htmlFor="int-name">Nume</Label>
        <Input id="int-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} /></> : null}
      {asked.map((f) => <FieldInput key={f.key} f={f} value={values[f.key]} saved={!!item?.secrets[f.key]} required={needed(f, values)}
        onChange={(v) => set(f.key, v)} />)}
      {scoped ? <FirmScopeField value={scope} onChange={setScope} firms={view.companies} /> : null}
      {kind.key === "shop" && item ? <p className={noteLeftStyle}>Emitere automată: {item.config.auto_issue === "true" ? "pornită" : "oprită"}.
        Se schimbă în <L to={{ co: item.config.company, tab: "automatizari" }} className="font-semibold text-ink underline">Automatizări</L>.</p> : null}
      {cli ? <AiCli key={values.provider} provider={values.provider} adding={!item} onChange={reload}
        onConnected={item ? reload : () => void save()}>{actions}</AiCli> : actions}
    </Form>
  );
  return (
    <Sheet onClose={onClose} label={item ? item.name : `Adaugă: ${kind.label}`}>
      <CloseX onClose={onClose} />
      <SheetHead title={item ? item.name : kind.label} sub={item && item.name !== kind.label ? <span className="font-normal text-muted">{kind.label}</span> : undefined} />
      {failed ? (
        <div className={bannerStyle} role="alert">
          <b>Integrarea nu a putut fi citită din nou. Salvează după ce se încarcă datele noi.</b>
          <Button type="button" variant="secondary" onClick={reload}>Reîncearcă</Button>
        </div>
      ) : null}
      {kind.key !== "anaf" ? form : item ? <AnafConnect item={item}>{form}</AnafConnect> : <><AnafSetupGuide /><AnafRedirectField r={kind.connect} />{form}</>}
      {item ? <ItemActions kind={kind} item={item} /> : null}
    </Sheet>
  );
}

/** A saved integration's own buttons: make it the default, switch it off or on, remove it. */
function ItemActions({ kind, item }: { kind: IntegrationKind; item: Integration }) {
  return (
    <Actions>
      {!kind.single && kind.key !== "anaf" && !item.default && item.enabled
        ? <Act action="integration-default" body={{ id: item.id }} label="Setează ca implicită" cls="btn g" /> : null}
      <Act action="integration-enable" body={{ id: item.id, on: item.enabled ? "0" : "1" }}
        label={item.enabled ? "Oprește" : "Pornește"} cls="btn g" />
      <Act action="integration-remove" body={{ id: item.id }} label="Șterge" cls="btn g"
        confirm={{ text: `Ștergi integrarea ${item.name}? Cheile ei se șterg de pe server.`, yes: "Șterge integrarea", danger: true }} />
    </Actions>
  );
}

function FieldInput({ f, value, saved, required, onChange }: {
  f: IntegrationField; value: string; saved: boolean; required: boolean; onChange: (v: string) => void;
}) {
  const id = `int-${f.key}`;
  if (f.choices.length) {
    return (
      <>
        <Label htmlFor={id}>{f.label}</Label>
        <Select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
          {(!required && !f.default) || !value ? <option value="">Alege</option> : null}
          {f.choices.map((c) => <option key={c} value={c}>{f.choice_labels?.[c] ?? choice(c)}</option>)}
        </Select>
      </>
    );
  }
  return (
    <>
      <Label htmlFor={id}>{f.label}{required ? "" : " (opțional)"}</Label>
      <Input id={id} value={value} onChange={(e) => onChange(e.target.value)}
        type={f.secret ? "password" : f.type === "url" ? "url" : f.type === "email" ? "email" : "text"}
        inputMode={f.type === "int" ? "numeric" : undefined} autoComplete={f.secret ? "new-password" : "off"}
        spellCheck={false} maxLength={f.secret ? 4096 : 500}
        placeholder={f.secret ? (saved ? "Salvată. Lasă gol pentru a o păstra." : "Nesalvată") : f.default || undefined} />
    </>
  );
}
