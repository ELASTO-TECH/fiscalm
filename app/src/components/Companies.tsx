"use client";
// Setări › Firme: the companies this install keeps the books of. A company is added by its CUI from ANAF's public data
// (the engine looks it up again when adding and never takes the firm's details from the app); its name in the app and its
// colours can change later, its key and CUI can't. Nothing is removed here: a company's records are tied to its key.
import { Button, Input, Select, Label, Form, Actions, Panel, statePillStyle, rowStyle, rowTextStyle, sectionHeadingStyle, backLinkStyle, statusStyle, statusTextStyle, bannerStyle, noteLeftStyle, settingsPageStyle, detailListStyle, linkButtonStyle, legendStyle } from "./Controls";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLoaded } from "./Paging";
import { getCompanies, historyType, postLogo, postCompany, postHistory, type CompanyResult } from "@/lib/api";
import { cap, nOf } from "@/lib/format";
import { useHash } from "@/lib/nav";
import { PALETTES, pal } from "@/lib/palette";
import type { CompaniesConfig, CompanyAccount, CompanyEntry, CompanyHistory, CompanyLookup } from "@/lib/types";
import { BankAccountChoices, BankSources } from "./BankConnect";
import { HistoryReview } from "./HistoryReview";
import { CompanyLogoEditor, FirmMark, LogoPicker, type PendingLogo } from "./CompanyLogo";
import { CompanyNames } from "./CompanyNames";
import { useIntegrations } from "./Settings";
import { useApp, useCan } from "./context";
import { Act, I, L, Loading, Title, Summary } from "./ui";

/** How a palette reads in the app (the engine stores its key). */
/** A heading inside a company's card (Băncile firmei, Alte nume ale firmei). */
const partHeading = "m-0 text-base font-semibold";
/** One part of a company opened for editing, under a line. */
const part = "border-0 border-t border-solid border-line p-5 first:border-t-0 sm:p-6";

const PALETTE_NAME: Record<string, string> = {
  amber: "Portocaliu", green: "Verde", blue: "Albastru", violet: "Violet", rose: "Roz", slate: "Gri",
};

/** The companies, read again whenever the app's state changes (after every action) and on `reload`. */
export function useCompanies() {
  const { view } = useApp();
  const { data, failed, reload } = useLoaded(getCompanies, "companies", view);
  return { data: data ?? null, failed, reload };
}

/** An install-wide request: one at a time like every other, its answer carrying Toate firmele's state. */
/** A lookup that got no answer at all (offline, a proxy error): shown like ANAF's silence, and asked again. */
export const unanswered = (): CompanyResult => ({ ok: false, message: "Căutarea nu s-a încheiat. Încearcă din nou.", next: null, state: null,
  done: { id: "", held: "", retry: true } });

export function useSend(reload: () => void) {
  const { run } = useApp();
  return async (action: Parameters<typeof postCompany>[0], body: Record<string, string>) => {
    // A lookup changes nothing and its refusal shows under the CUI: no toast, no reload.
    if (action === "company-lookup") return postCompany(action, body).catch(unanswered);
    const r = await run<CompanyResult>("ALL", () => postCompany(action, body), undefined, () => false);
    reload();
    return r;
  };
}

export function Firme() {
  const { data, failed, reload } = useCompanies();
  // The session capability and current metadata must both allow company changes.
  const { view } = useApp();
  const manage = useCan("manage_firms") && view.can_manage_companies === true;
  // A link to a firm's banks (#conn-CO) or earlier invoices (#hist-CO), from Acasă's Configurează card, opens only that part.
  const hash = useHash(); // going to Firme without it shows the whole list again
  const only = manage ? (["conn", "hist"] as const).find((p) => hash === `#${p}-${view.co}`) : undefined;
  const [editing, setEditing] = useState("");
  const [adding, setAdding] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const back = () => setTimeout(() => opener.current?.focus());
  const backTo = (key: string) => setTimeout(() => document.getElementById(`edit-${key}`)?.focus());
  const mine = data?.companies.find((c) => c.key === view.co);
  const logos = Object.fromEntries(view.companies.map((c) => [c.key, c.logo]));
  if (only && mine && data) return (
    <div className={settingsPageStyle}>
      <L to={{ co: view.co }} className={backLinkStyle} label="Înapoi la Acasă"><I n="arrow-left" />Acasă</L>
      <Title>{only === "conn" ? "Bănci și conturi" : "Facturi anterioare"}</Title>
      {failed ? <div className={bannerStyle} role="alert">
        <b>Datele nu au putut fi actualizate. Datele afișate pot fi vechi.</b>
        <Button type="button" variant="secondary" onClick={reload}>Reîncearcă</Button>
      </div> : null}
      <Panel padded={false}><EditCompany c={mine} config={data} reload={reload} only={only} onDone={() => undefined} /></Panel>
    </div>
  );
  const setup = "setup" in view ? view.setup : null;
  return (
    <div className={settingsPageStyle}>
      <L to={{ tab: "setari" }} className={backLinkStyle} label="Înapoi la Setări"><I n="arrow-left" />Setări</L>
      <Title>Firme</Title>
      {!data && !failed ? <Loading rows={view.companies.length || 1} /> : null}
      {failed ? (
        <div className={bannerStyle} role="alert">
          <b>{data ? "Lista nu a putut fi actualizată. Datele afișate pot fi vechi." : "Firmele nu au putut fi încărcate."}</b>
          <Button type="button" variant="secondary" onClick={reload}>Reîncearcă</Button>
        </div>
      ) : null}
      <section aria-labelledby="h-firme">
        <div className={sectionHeadingStyle}>
          <h2 id="h-firme">{data ? cap(nOf(data.companies.length, "o firmă", "firme")) : "Firme"}</h2>
          {!adding && data && manage ? <Button ref={opener} type="button" variant="secondary" onClick={() => setAdding(true)}>Adaugă o firmă</Button> : null}
        </div>
        {adding && data ? <div className="mb-3">
          <AddCompany config={data} reload={reload} onDone={() => {
            setAdding(false);
            back();
          }} />
        </div> : null}
        <Panel padded={false}>
          {data?.companies.map((c) => (editing === c.key
            ? <EditCompany key={c.key} c={c} config={data} reload={reload} onDone={() => {
              setEditing("");
              backTo(c.key);
            }} />
            : (
              <div key={c.key} className={`${rowStyle} flex-wrap sm:flex-nowrap`}>
                <FirmMark co={c.key} logo={logos[c.key]} letters={c.name.trim().charAt(0).toUpperCase()} pal={pal(c.palette)} className="size-11 text-xl" />
                <span className={rowTextStyle}>
                  <b>{c.name}</b>
                  <small>{c.legal_name}{c.cui ? `, CUI ${c.cui}` : ""}</small>
                  {c.accounts ? <small>{banksLine(c.accounts)}</small> : null}
                </span>
                {manage ? (
                  <Button id={`edit-${c.key}`} type="button" variant="secondary" aria-label={`Modifică ${c.name}`}
                    onClick={() => setEditing(c.key)}>Modifică</Button>
                ) : null}
              </div>
            )))}
        </Panel>
      </section>
      {manage && mine && setup?.hidden ? <Actions>
        <Act action="setup-card-show" body={{}} label={`Arată cardul Configurează pentru ${mine.name}`} cls="btn g" />
      </Actions> : null}
      {!manage ? <p className={noteLeftStyle}>Doar administratorul instalării adaugă și modifică firmele.</p> : null}
    </div>
  );
}

/** The palettes as radio buttons, each shown in its colour and named: only those the engine offers that the app can show. */
function PalettePicker({ value, onChange, palettes, optional = false }: {
  value: string; onChange: (p: string) => void; palettes: string[]; optional?: boolean;
}) {
  return <fieldset className="m-0 mt-5 border-0 p-0">
    <legend className={legendStyle}>{optional ? "Culoare (opțional)" : "Culoarea firmei"}</legend>
    <div className="flex flex-wrap gap-2">{palettes.filter(p => Object.hasOwn(PALETTES, p)).map(p => <label key={p} title={PALETTE_NAME[p]} className="m-0! relative grid size-11 cursor-pointer place-items-center rounded-control border border-solid border-transparent has-[:checked]:border-ink has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-focus has-[:focus-visible]:outline-offset-2">
      <input type="radio" name="palette" value={p} checked={value === p} onChange={() => onChange(p)} aria-label={PALETTE_NAME[p]} className="absolute inset-0 m-0 size-full cursor-pointer opacity-0" />
      <span data-pal={pal(p)} className="pointer-events-none grid size-8 place-items-center rounded-full bg-[var(--co)] text-[var(--co-ink)] [&_svg]:size-4" aria-hidden="true">{value === p && <I n="check" />}</span>
    </label>)}</div>
  </fieldset>;
}

function AddCompany({ config, reload, onDone }: { config: CompaniesConfig; reload: () => void; onDone: () => void }) {
  const { busy, refresh } = useApp();
  const send = useSend(reload);
  // The state read after company-add can finish before the logo is saved: read the shown view again for the switch.
  const done = () => { void refresh(); onDone(); };
  return <AddCompanyForm config={config} busy={busy} send={send} onDone={done} onCancel={onDone} />;
}

export type SendCompany = (action: Parameters<typeof postCompany>[0], body: Record<string, string>) => Promise<CompanyResult | null>;

/** Look a CUI up at ANAF, then add that firm: in Firme, and as an install's first company (no Renunță there). */
export function AddCompanyForm({ config, busy, send, onDone, onCancel }: {
  config: CompaniesConfig; busy: boolean; send: SendCompany; onDone: (r: CompanyResult) => void; onCancel?: () => void;
}) {
  const [logo, setLogo] = useState<PendingLogo | null>(null);
  const [created, setCreated] = useState<CompanyResult | null>(null);
  const [readingLogo, setReadingLogo] = useState(false);
  const [saving, setSaving] = useState(false);
  const lock = useRef(false);
  const [problem, setProblem] = useState("");
  const [cui, setCui] = useState("");
  const [found, setFound] = useState<CompanyLookup | null>(null);
  // ANAF's refusal of the typed CUI; `retry` when ANAF gave no answer and the same CUI may be asked again.
  const [refused, setRefused] = useState<{ text: string; retry: boolean } | null>(null);
  const [looking, setLooking] = useState(false);
  const [name, setName] = useState("");
  // No colour until one is picked: the engine chooses the default (the first unused, then in turn).
  const [palette, setPalette] = useState("");
  // What the field held when the lookup went: an answer for a CUI no longer in the field is dropped.
  const typed = useRef("");
  const lookup = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || looking || saving || readingLogo || created || found || !cui.trim()) return;
    setFound(null);
    setRefused(null);
    setLooking(true);
    const asked = cui;
    const r = await send("company-lookup", { cui: cui.trim() }).finally(() => setLooking(false));
    if (typed.current !== asked) return;
    if (r?.ok && r.done?.legal_name && r.done.cui) {
      const d = r.done;
      setFound({ cui: d.cui!, legal_name: d.legal_name!, address: d.address ?? "", vat_payer: !!d.vat_payer, inactive: !!d.inactive });
    } else if (r && !r.ok) setRefused({ text: r.message, retry: r.done?.retry === true });
  };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    if (!found || found.inactive || busy || readingLogo || lock.current) return;
    lock.current = true; setSaving(true); setProblem("");
    try {
      const r = created ?? await send("company-add", { cui: found.cui, ...(name.trim() ? { name: name.trim() } : {}), ...(palette ? { palette } : {}) });
      if (!r?.ok) return;
      setCreated(r);
      if (logo) {
        const key = r.done?.key;
        if (!key) { setProblem("Firma a fost adăugată, dar sigla nu a fost salvată. Adaug-o din Modifică."); return; }
        const saved = await postLogo(key, logo.file, logo.kind);
        if (!saved.ok) { setProblem(saved.message); return; }
      }
      onDone(r);
    } catch { setProblem("Sigla nu a putut fi salvată. Reîncearcă pentru aceeași firmă."); }
    finally { lock.current = false; setSaving(false); }
  };
  const cancel = onCancel ? <Button type="button" variant="secondary" disabled={busy || looking || saving} onClick={() => created ? onDone(created) : onCancel()}>{created ? "Continuă fără siglă" : "Renunță"}</Button> : null;
  return (
    <Panel>
      <Form className="m-0" onSubmit={lookup} aria-label="Caută firma">
        <Label htmlFor="co-cui">CUI</Label>
        <div className="flex flex-wrap items-center gap-2 [&>input]:flex-1 [&>input]:basis-40">
          <Input id="co-cui" disabled={saving || !!created} value={cui} autoComplete="off" inputMode="text" maxLength={32}
            aria-invalid={refused && !refused.retry ? true : undefined} aria-describedby={refused ? "co-cui-refused" : undefined}
            onChange={(e) => {
              typed.current = e.target.value;
              setCui(e.target.value);
              setFound(null);
              setRefused(null);
              setLogo(null);
              setReadingLogo(false);
            }} />
          <Button type="submit" variant="secondary" disabled={busy || looking || saving || readingLogo || !!created || !!found || !cui.trim()}>
            {looking ? "Se caută…" : refused?.retry ? "Încearcă din nou" : "Caută la ANAF"}</Button>
        </div>
        {refused ? <p id="co-cui-refused" className="m-0 mt-2 text-sm leading-relaxed text-danger" role="alert">{refused.text}</p> : null}
      </Form>
      <p className="vh sr-only" role="status">{found ? `${found.legal_name}, CUI ${found.cui}` : ""}</p>
      {found ? (
        <Form onSubmit={add} aria-label="Adaugă firma">
          <div className="my-5 grid gap-1 rounded-control bg-inset p-4 text-sm [&_span]:text-muted">
            <b>{found.legal_name}</b>
            <span>CUI {found.cui}</span>
            <span>{found.address}</span>
            <span>{found.vat_payer ? "Plătitor de TVA" : "Neplătitor de TVA"}</span>
          </div>
          {found.inactive ? <p className={noteLeftStyle} role="alert">Firma figurează ca inactivă la ANAF și nu poate fi adăugată.</p> : (
            <>
              <div className="mt-5 flex flex-col gap-2">
              <h3 className={partHeading}>Identitatea firmei</h3>
              <Label htmlFor="co-name">Nume în aplicație (opțional)</Label>
              <Input id="co-name" disabled={saving || !!created} value={name} onChange={(e) => setName(e.target.value)} maxLength={config.limits.name_max}
                autoComplete="off" placeholder={found.legal_name} />
              <fieldset disabled={saving || !!created} className="m-0 min-w-0 border-0 p-0">
                <PalettePicker value={palette} onChange={setPalette} palettes={config.palettes} optional />
              </fieldset>
              <LogoPicker key={found.cui} value={logo} onChange={setLogo} onBusy={setReadingLogo} disabled={saving} />
              </div>
            </>
          )}
          {created ? <p className={noteLeftStyle} role="status">Firma a fost adăugată. Sigla se salvează pentru {created.done?.name || found.legal_name}.</p> : null}
          {problem ? <p className={noteLeftStyle} role="alert">{problem}</p> : null}
          <Actions>
            {!found.inactive ? <Button type="submit" disabled={busy || saving || readingLogo}>{saving ? "Se salvează…" : created ? "Reîncearcă" : "Adaugă firma"}</Button> : null}
            {cancel}
            {created && !onCancel ? <Button type="button" variant="secondary" disabled={saving} onClick={() => onDone(created)}>Continuă fără siglă</Button> : null}
          </Actions>
        </Form>
      ) : cancel ? <Actions>{cancel}</Actions> : null}
    </Panel>
  );
}

/** A company opened for editing: its identity, names, banks and earlier invoices; `only` one of the last two. */
export function EditCompany({ c, config, reload, onDone, only }: {
  c: CompanyEntry; config: CompaniesConfig; reload: () => void; onDone: () => void; only?: "conn" | "hist";
}) {
  const { busy } = useApp();
  const send = useSend(reload);
  const [name, setName] = useState(c.name);
  const [palette, setPalette] = useState(c.palette);
  const changed = name.trim() !== c.name || palette !== c.palette;
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !changed || !name.trim()) return;
    const r = await send("company-update", { key: c.key, ...(name.trim() !== c.name ? { name: name.trim() } : {}),
      ...(palette !== c.palette ? { palette } : {}) });
    if (r?.ok) onDone();
  };
  const bank = <>
    {c.accounts && c.banks ? <div className={part}><BanksAndAccounts c={c} reload={reload} titled={!!only} /></div> : null}
    {c.has_ron_account === false ? <BankAccountForm c={c} busy={busy} send={send} title="Contul din care pleacă plățile" /> : null}
  </>;
  const history = c.history ? <div className={part}><HistoryImport c={c} history={c.history} reload={reload} /></div> : null;
  if (only) return <div>{only === "conn" ? bank : history}</div>;
  return (
    <div>
    <Form className="p-5 sm:p-6" onSubmit={save} aria-label={`Modifică ${c.name}`}>
      <p className={noteLeftStyle}>{c.legal_name}{c.cui ? `, CUI ${c.cui}` : ""}</p>
      <CompanyLogoEditor company={c.key} />
      <Label htmlFor={`co-${c.key}-name`}>Nume în aplicație</Label>
      <Input id={`co-${c.key}-name`} value={name} onChange={(e) => setName(e.target.value)} maxLength={config.limits.name_max}
        autoComplete="off" autoFocus />
      <PalettePicker value={palette} onChange={setPalette} palettes={config.palettes} />
      <Actions>
        <Button type="submit" disabled={busy || !changed || !name.trim()}>Salvează</Button>
        <Button type="button" variant="secondary" disabled={busy} onClick={() => { setName(c.name); setPalette(c.palette); onDone(); }}>Renunță</Button>
      </Actions>
    </Form>
    {c.aliases ? <CompanyNames c={c} config={config} reload={reload} /> : null}
    <FirmIntegrations co={c.key} />
    {bank}
    {history}
    </div>
  );
}

/** Which ANAF connection reads the firm's e-Factura invoices and which inboxes file for it (the engine's `coverage`),
 * each opening its sheet; for whoever can change integrations. */
function FirmIntegrations({ co }: { co: string }) {
  const { data } = useIntegrations();
  const row = data?.coverage?.find((r) => r.firm === co);
  if (!data || !row) return null;
  const link = (id: string) => {
    const it = data.items.find((i) => i.id === id);
    return it ? <L key={id} className={linkButtonStyle} to={{ int: id }}>{it.name}</L> : null;
  };
  const list = (ids: string[]) => ids.map((id, n) => <span key={id}>{n ? ", " : ""}{link(id)}</span>);
  return (
    <section className={`${part} flex flex-col gap-3`} aria-labelledby={`int-${co}`}>
      <h3 className={partHeading} id={`int-${co}`}>Integrări</h3>
      <dl className={detailListStyle}>
        <dt>ANAF</dt>
        <dd>{row.anaf ? link(row.anaf) : <L className={linkButtonStyle} to={{ tab: "setari", sec: "integrari" }}>Neconectat</L>}
          {row.anaf && row.anaf_also.length ? <span className="mt-1 block text-wait">Este și în {list(row.anaf_also)}. Lasă firma într-una singură.</span> : null}</dd>
        <dt>E-mail</dt>
        <dd>{row.mail.length ? list(row.mail) : <L className={linkButtonStyle} to={{ tab: "setari", sec: "integrari" }}>Nicio adresă</L>}</dd>
      </dl>
    </section>
  );
}

/** A company's first RON account (the engine sets it once; a change is the administrator's, from the command line). */
export function BankAccountForm({ c, busy, send, title = "Cont în lei, pentru fișierul de plăți" }: {
  c: { key: string; name: string }; busy: boolean; send: SendCompany; title?: string;
}) {
  const [iban, setIban] = useState("");
  const clean = iban.replace(/\s+/g, "").toUpperCase();
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !clean) return;
    const r = await send("company-bank", { key: c.key, iban: clean });
    if (r?.ok) setIban("");
  };
  return (
    <Form className={part} onSubmit={save} aria-label={`Contul în lei pentru ${c.name}`}>
      <h3 className={partHeading}>{title}</h3>
      <p className={noteLeftStyle}>Nu are încă un cont în lei.</p>
      <Label htmlFor={`co-${c.key}-iban`}>IBAN</Label>
      <Input id={`co-${c.key}-iban`} value={iban} onChange={(e) => setIban(e.target.value)} autoComplete="off" spellCheck={false}
        inputMode="text" maxLength={42} data-1p-ignore data-lpignore="true" data-bwignore="true" />
      <Actions>
        <Button type="submit" variant="secondary" disabled={busy || !clean}>Salvează contul</Button>
      </Actions>
    </Form>
  );
}

/** "2 bănci, 3 conturi": how many banks hold the current accounts, and how many accounts. */
function banksLine(accounts: CompanyAccount[]): string {
  const now = accounts.filter((a) => !a.retired);
  const banks = new Set(now.map((a) => a.bank || `?${a.bank_name}`));
  return now.length ? `${cap(nOf(banks.size, "o bancă", "bănci"))}, ${nOf(now.length, "un cont", "conturi")}` : "Niciun cont";
}

/** The password a bank's PDF statements open with: write-only (typed, sent, cleared; never shown). Missing, its field
 * is open; saved, a row with its state and Schimbă, which opens the field with Anulează and Salvează. */
function StatementPassword({ co, bank, name, set, reload }: { co: string; bank: string; name: string; set: boolean; reload: () => void }) {
  const { busy } = useApp();
  const send = useSend(reload);
  const [editing, setEditing] = useState(false);
  const [password, setPassword] = useState("");
  const [done, setDone] = useState(0);
  const change = useRef<HTMLButtonElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const id = `pw-${co}-${bank}`;
  const open = !set || editing;
  // after Salvează or Anulează, focus goes to what stands in the form's place: Schimbă, or the field while still missing
  useEffect(() => { if (done) (change.current ?? field.current)?.focus(); }, [done, set]);
  const typed = useRef(""); // what the field holds now, read when a save answers
  const type = (value: string) => { typed.current = value; setPassword(value); };
  const close = () => { setEditing(false); type(""); setDone((n) => n + 1); };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !password) return;
    const sent = password;
    const r = await send("company-statement-password", { key: co, bank, password: sent });
    if (r?.ok) { if (typed.current === sent) close(); else setEditing(true); } // a newer one stays, its field open
  };
  const state = <p id={`${id}-state`} className={`st ${set ? "ok" : "late"} m-0 ${statusTextStyle} ${statusStyle[set ? "ok" : "late"]}`}>
    {set ? "Parola e salvată." : "Lipsește: extrasele PDF nu se pot citi."}</p>;
  if (!open) return <div className="mt-1 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-control bg-inset px-3 py-2">
    <div><b className="block text-[13px] font-semibold text-ink">Parola extraselor PDF</b>{state}</div>
    <Button ref={change} type="button" variant="secondary" onClick={() => setEditing(true)}>Schimbă parola</Button>
  </div>;
  return (
    <Form className="mt-1 rounded-control bg-inset px-3 py-3" onSubmit={save} aria-label={`Parola extraselor de la ${name}`}>
      <Label htmlFor={id}>Parola extraselor PDF</Label>
      <Input ref={field} id={id} type="password" value={password} onChange={(e) => type(e.target.value)} autoComplete="off" spellCheck={false}
        required maxLength={4096} aria-describedby={`${id}-state`} autoFocus={editing} data-1p-ignore data-lpignore="true" data-bwignore="true" />
      <div className="mt-2">{state}</div>
      <Actions className="btns mt-3">
        {editing ? <Button type="button" variant="secondary" disabled={busy} onClick={close}>Anulează</Button> : null}
        <Button type="submit" disabled={busy}>Salvează</Button>
      </Actions>
    </Form>
  );
}

/** Setări › Firme, Bănci și conturi: everything about the firm's banks in one place. Each bank with its current accounts
 * (the one payment files debit is marked) and the ways its statements reach fiscalm; an account added by IBAN, or a bank
 * connected so its accounts can be chosen at the bank. Balances and statements are in Tranzacții. */
export function BanksAndAccounts({ c, reload, titled = false }: { c: CompanyEntry; reload: () => void; titled?: boolean }) {
  const [chosen, setChosen] = useState("");
  // each account's Date cont, kept here: closing or reopening moves its row to the other list, and what was typed (and
  // whether the editor was open) goes with it
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const row = (a: CompanyAccount) => <AccountRow key={a.id} c={c} a={a} reload={reload} draft={drafts[a.id] ?? NO_DRAFT}
    setDraft={(change) => setDrafts((all) => ({ ...all, [a.id]: change(all[a.id] ?? NO_DRAFT) }))} />;
  const inboxes = useIntegrations().data?.items ?? null;
  const banks = c.banks ?? [];
  const accounts = (c.accounts ?? []).filter((a) => !a.retired);
  const active = new Set(accounts.map((a) => a.bank));
  const closed = (c.accounts ?? []).filter((a) => a.retired);
  const shown = banks.filter((b) => active.has(b.bank) || b.bank === chosen);
  const connectable = banks.filter((b) => !active.has(b.bank) && b.connect !== "not_supported");
  return (
    <section className="flex flex-col gap-3" aria-labelledby={`conn-${c.key}`}>
      {/* on its own page the page title already says it; the heading stays for screen readers and as the focus target */}
      <h3 className={`${partHeading} focus:outline-none ${titled ? "sr-only" : ""}`} id={`conn-${c.key}`} tabIndex={-1}>Bănci și conturi</h3>
      <BankAccountChoices co={c.key} company={c.legal_name || c.name} banks={banks} onChanged={reload} />
      {shown.map((b) => (
        <div key={b.bank} className="rounded-card border border-solid border-line p-4 sm:p-5">
          <h4 className="m-0 text-[15px] font-semibold leading-6">{b.name || "Altă bancă"}</h4>
          <p id={`acc-${c.key}-${b.bank}`} className={groupStyle}>Conturi</p>
          <ul className="m-0 mb-4 list-none p-0" aria-labelledby={`acc-${c.key}-${b.bank}`}>
            {accounts.filter((a) => a.bank === b.bank).map(row)}
            {!active.has(b.bank) ? <li className="py-2 text-sm text-muted">Niciun cont încă.</li> : null}
          </ul>
          <p className={groupStyle}>Cum ajung tranzacțiile</p>
          <BankSources b={b} co={c.key} inboxes={inboxes}
            password={b.statement_password !== undefined ? <StatementPassword co={c.key} bank={b.bank} name={b.name} set={b.statement_password} reload={reload} /> : undefined} />
        </div>
      ))}
      {!shown.length ? <p className={noteLeftStyle}>Firma nu are încă un cont bancar.</p> : null}
      {closed.length ? <>
        {/* every closed account, whatever its bank: one whose bank has no current account keeps its dates to correct */}
        <p id={`closed-${c.key}`} className={groupStyle}>Conturi închise</p>
        <ul className="m-0 list-none p-0" aria-labelledby={`closed-${c.key}`}>
          {closed.map(row)}
        </ul>
      </> : null}
      <AddAccount c={c} accounts={accounts} reload={reload} />
      {connectable.length ? <>
        <Label htmlFor={`bank-${c.key}`}>Conectează o bancă</Label>
        <Select id={`bank-${c.key}`} value={chosen} onChange={(e) => setChosen(e.target.value)}>
          <option value="">Alege banca</option>
          {connectable.map((b) => <option key={b.bank} value={b.bank}>{b.name}</option>)}
        </Select>
      </> : null}
      <p className={noteLeftStyle}>Soldurile și extrasele sunt în <L className={linkButtonStyle} to={{ co: c.key, tab: "tranzactii" }}>Tranzacții</L>.</p>
    </section>
  );
}

/** "deschis din 03.2024, închis pe 31.10.2026": the dates a person set for an account, or "". */
function datesLine(a: CompanyAccount): string {
  const opened = a.opened ? `deschis din ${a.opened.split("-").reverse().join(".")}` : "";
  return [opened, a.retired ? `închis pe ${ddmmyyyy(a.retired)}` : ""].filter(Boolean).join(", ");
}

/** One account in Bănci și conturi: its currency and last digits, its dates, whether payments leave from it, Date cont. */
/** What a person typed in an account's Date cont (null: untouched, the engine's date shows) and whether it is open. */
type Draft = { opened: string | null; retired: string | null; open: boolean };
const NO_DRAFT: Draft = { opened: null, retired: null, open: false };
type DraftProps = { draft: Draft; setDraft: (change: (d: Draft) => Draft) => void };

function AccountRow({ c, a, reload, ...draft }: { c: CompanyEntry; a: CompanyAccount; reload: () => void } & DraftProps) {
  // a closed account is listed with the others closed, whatever its bank: the bank tells two alike apart
  const line = [a.retired ? a.bank_name || "Altă bancă" : "", datesLine(a)].filter(Boolean).join(", ");
  return (
    <li className="flex flex-wrap items-center gap-2 border-0 border-t border-solid border-line py-2 text-sm first:border-t-0">
      <span className={`mr-auto ${a.retired ? "text-muted" : ""}`}>
        {a.currency} ••••{a.iban_last4}{line ? <small className="block text-xs text-muted">{line}</small> : null}
      </span>
      {a.pays ? <span className={`rounded-badge px-1.5 py-0.5 text-xs font-medium ${statePillStyle.ok}`}>Plățile pleacă de aici</span> : null}
      <AccountDates c={c} a={a} reload={reload} {...draft} />
    </li>
  );
}

/** Date cont: the month or day an account opened and the day it closed ("" forgets one). A field nobody touched shows
 * the date the engine has and is never sent, so a form shown before another person's change never undoes it; a
 * touched one sends what it holds. After a save a field shows the engine's date again, unless it was typed in since.
 * The engine checks both: an account can't open after it closed, closes only on a day already past or today, and the
 * one payment files debit closes only once another takes its place. A statement of a month the account wasn't open
 * doesn't owe it. */
function AccountDates({ c, a, reload, draft, setDraft }: { c: CompanyEntry; a: CompanyAccount; reload: () => void } & DraftProps) {
  const { busy } = useApp();
  const send = useSend(reload);
  const { opened, retired } = draft; // null: untouched, show the engine's
  const id = `dates-${c.key}-${a.id}`;
  const changed: Record<string, string> = {
    ...(opened !== null && opened.trim() !== (a.opened ?? "") ? { opened: opened.trim() } : {}),
    ...(retired !== null && retired !== (a.retired ?? "") ? { retired } : {}),
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const sent = changed;
    if (busy || !Object.keys(sent).length) return;
    const r = await send("company-account-dates", { key: c.key, account: a.id, ...sent });
    if (!r?.ok) return;
    setDraft((d) => ({ // typed in since: kept
      ...d,
      opened: d.opened !== null && d.opened.trim() === sent.opened ? null : d.opened,
      retired: d.retired !== null && d.retired === sent.retired ? null : d.retired,
    }));
  };
  return (
    <details className="basis-full" open={draft.open} onToggle={(e) => {
      const open = e.currentTarget.open;
      setDraft((d) => (d.open === open ? d : { ...d, open }));
    }}>
      <Summary>Date cont</Summary>
      <Form className="mt-2" onSubmit={save} aria-label={`Datele contului ${a.currency} ••••${a.iban_last4}`}>
        <Label htmlFor={`${id}-opened`}>Deschis din (luna AAAA-LL sau ziua AAAA-LL-ZZ)</Label>
        <Input id={`${id}-opened`} value={opened ?? a.opened ?? ""} onChange={(e) => { const v = e.target.value; setDraft((d) => ({ ...d, opened: v })); }} autoComplete="off"
          maxLength={10} placeholder="2024-03" />
        <Label htmlFor={`${id}-retired`}>Închis pe</Label>
        <Input id={`${id}-retired`} type="date" value={retired ?? a.retired ?? ""} onChange={(e) => { const v = e.target.value; setDraft((d) => ({ ...d, retired: v })); }} />
        <Actions>
          <Button type="submit" className="btn g" disabled={busy || !Object.keys(changed).length}>Salvează datele</Button>
        </Actions>
      </Form>
    </details>
  );
}

/** A group's caption inside a bank's card (Conturi, Cum ajung tranzacțiile). */
const groupStyle = "mt-3 mb-1 text-xs font-semibold uppercase tracking-wide text-muted";

const CURRENCIES = ["RON", "EUR", "USD", "GBP", "CHF"];

/** Adding an account at any bank, in any currency (the engine checks the IBAN, says which bank it is from and refuses
 * another company's, the accountant's or a partner's; it never becomes the paying account). The currency starts as the
 * one a Banca Transilvania IBAN names, else EUR once the firm has lei. */
function AddAccount({ c, accounts, reload }: { c: CompanyEntry; accounts: CompanyAccount[]; reload: () => void }) {
  const { busy } = useApp();
  const send = useSend(reload);
  const [iban, setIban] = useState("");
  const [picked, setPicked] = useState("");
  const clean = iban.replace(/\s+/g, "").toUpperCase();
  const named = clean.slice(4, 8) === "BTRL" ? CURRENCIES.find((x) => clean.slice(8, 11) === x) : undefined;
  const currency = picked || named || (accounts.some((a) => a.currency === "RON") ? "EUR" : "RON");
  const add = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !clean) return;
    const r = await send("company-account", { key: c.key, iban: clean, currency });
    if (r?.ok) { setIban(""); setPicked(""); }
  };
  return (
    <details>
      <Summary>Adaugă un cont</Summary>
      <Form className="mt-2" onSubmit={add} aria-label={`Adaugă un cont pentru ${c.name}`}>
        <Label htmlFor={`acc-${c.key}-iban`}>IBAN-ul contului nou</Label>
        <Input id={`acc-${c.key}-iban`} value={iban} onChange={(e) => setIban(e.target.value)} autoComplete="off"
          spellCheck={false} maxLength={42} data-1p-ignore data-lpignore="true" data-bwignore="true" />
        <Label htmlFor={`acc-${c.key}-ccy`}>Moneda</Label>
        <Select id={`acc-${c.key}-ccy`} value={currency} onChange={(e) => setPicked(e.target.value)}>
          {CURRENCIES.map((x) => <option key={x} value={x}>{x === "RON" ? "Lei (RON)" : x}</option>)}
        </Select>
        <Actions>
          <Button type="submit" className="btn g" disabled={busy || !clean}>Adaugă contul</Button>
        </Actions>
      </Form>
    </details>
  );
}

const NOT_HISTORY = "Se acceptă XML, o arhivă ZIP cu facturi sau lista SmartBill de facturi neîncasate ca XLSX.";
const ddmmyyyy = (iso: string) => iso.split("-").reverse().join(".");

/** "Importă facturi": the e-Factura invoices of the program used before fiscalm (one XML or a ZIP, also SPV's), sent
 * as they are; the engine reads them, refuses another firm's, and keeps those dated before the start date as history.
 * The start date is asked until the first import, then it is the firm's and only shown. */
export function HistoryImport({ c, history, reload }: { c: CompanyEntry; history: CompanyHistory; reload: () => void }) {
  const { busy, run, view } = useApp();
  const input = useRef<HTMLInputElement>(null);
  const today = view.today;
  // Proposed: the first day of the month of the oldest statement, else 1 January.
  const [start, setStart] = useState(history.start ?? (history.first_statement ? `${history.first_statement.slice(0, 7)}-01` : `${today.slice(0, 4)}-01-01`));
  // ISO dates compare as strings; a future one would only be refused after the upload
  const day = history.start ?? (/^\d{4}-\d{2}-\d{2}$/.test(start) && start <= today ? start : "");
  const picked = async (f: File | undefined) => {
    if (input.current) input.current.value = "";
    if (!f || !day) return;
    const type = historyType(f);
    await run(c.key, async () => (type ? postHistory(c.key, day, f, type)
      : { ok: false, message: NOT_HISTORY, next: null, done: null, state: null }), undefined, () => false);
    reload();
  };
  const kept = history.issued + history.received;
  return (
    <section className="flex flex-col gap-3" aria-labelledby={`hist-${c.key}`}>
      <h3 className={`${partHeading} focus:outline-none`} id={`hist-${c.key}`} tabIndex={-1}>Facturi din alt program</h3>
      <p className={noteLeftStyle} id={`hist-${c.key}-note`}>
        O singură arhivă ZIP cu facturile emise și primite, ca PDF sau XML. Formatul se recunoaște din conținut.
        PDF-urile rămân în istoric, indiferent de dată.
      </p>
      {history.start ? (
        <p className={`${noteLeftStyle} [&_a]:font-semibold [&_a]:text-ink [&_a]:underline [&_a]:underline-offset-4`}>{`Data de început: ${ddmmyyyy(history.start)}. `}
          {kept ? <>În istoric: <L to={{ co: c.key, tab: "vanzari" }}>{nOf(history.issued, "o factură emisă", "facturi emise")}</L> și{" "}
            <L to={{ co: c.key, tab: "cheltuieli" }}>{nOf(history.received, "o factură primită", "facturi primite")}</L>.</>
            : "Nicio factură în istoric."}</p>
      ) : (
        <>
          <Label htmlFor={`hist-${c.key}-start`}>Data de început</Label>
          <Input id={`hist-${c.key}-start`} type="date" value={start} max={today} onChange={(e) => setStart(e.target.value)}
            aria-describedby={`hist-${c.key}-start-note`} />
          <p className={noteLeftStyle} id={`hist-${c.key}-start-note`}>Facturile de dinainte apar fără stare de plată. Primul import fixează data.</p>
        </>
      )}
      <Actions>
        <Button type="button" variant="secondary" disabled={busy || !day} aria-describedby={`hist-${c.key}-note${!history.start && !day ? ` hist-${c.key}-import-note` : ""}`}
          onClick={() => input.current?.click()}>Importă facturi</Button>
      </Actions>
      {!history.start && !day ? <p className={noteLeftStyle} id={`hist-${c.key}-import-note`}>Alege data de început pentru a putea importa facturile.</p> : null}
      <details className="rounded-control bg-inset p-3">
        <Summary>Facturi vechi, încă neîncasate</Summary>
        <p className="mt-2 text-[13px] leading-relaxed text-muted">Din SmartBill, exportă lista Facturi neîncasate ca XLSX și încarc-o aici, după facturi.
          Facturile din listă trec din istoric la facturi de încasat.</p>
      </details>
      <HistoryReview company={c} reload={reload} />
      <Input ref={input} type="file" hidden accept=".zip,.xml,.xlsx,application/zip,application/xml,text/xml,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        onChange={(e) => picked(e.target.files?.[0])} tabIndex={-1} aria-hidden="true" />
    </section>
  );
}

