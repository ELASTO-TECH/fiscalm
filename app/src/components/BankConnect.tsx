// A company's bank connections (open banking): how one reads, the button that opens the bank to confirm, and the Home
// task before access ends. The engine decides every state (fiscalm.bank.connections.status); the app only shows it.
import { Actions, Button, Input, Label, Form, linkButtonStyle, noteLeftStyle, statePillStyle } from "./Controls";
import { useEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { post, type BankConnectResult } from "@/lib/api";
import { dm, type Tone } from "@/lib/format";
import { type ActionResult, type BankAccountOffer, type BankStatus, type Done, type Integration, type State } from "@/lib/types";
import { useApp } from "./context";
import { L, Task } from "./ui";

/** A bank connection's state as its badge says it, and what to know about it: [tone, badge, line]. */
export function connectionLine(c: NonNullable<BankStatus["connection"]>, today: string): [Tone, string, string] {
  const read = c.last_read ? `Citită pe ${dm(c.last_read, today)}.` : "Încă necitită.";
  if (c.state === "expired") return ["late", "Expirată", "Accesul a expirat. Conectează din nou banca."];
  if (c.state === "error") return ["late", "Nu se citește", `${c.error ?? "Ultima citire nu a reușit."} ${read}`];
  const until = c.valid_until ? ` Accesul expiră pe ${dm(c.valid_until, today)}.` : "";
  return [c.state === "expiring" ? "soon" : "ok", c.state === "expiring" ? "Expiră curând" : "Conectată", `${read}${until}`];
}

/** The provider's own pages: where the engine (enablebanking.trusted_page) and the app agree a person may be sent. */
const PROVIDER_HOST = /^(?:[a-z0-9-]+\.)*enablebanking\.com$/;

/** The engine checks the page is the provider's; the app checks again, parsing it as the browser will (a backslash or a
 * name before the host can't point it elsewhere): https, the provider's host, no name or password, no other port. */
export function safePage(url: unknown): url is string {
  if (typeof url !== "string" || !/^[!-~]+$/.test(url) || url.includes("\\")) return false;
  try {
    const u = new URL(url);
    return u.protocol === "https:" && !u.username && !u.password && u.port === "" && PROVIDER_HOST.test(u.hostname);
  } catch {
    return false;
  }
}

/** Leaving fiscalm for the bank's page (an object, so tests can watch it without navigating). */
export const leave = { to: (url: string) => window.location.assign(url) };

/** Opens the bank's page to confirm access (read only). The engine answers with the address; anything not https is
 * never followed. Allowed before the install goes live: it moves nothing. */
export function ConnectBank({ co, bank, again }: { co: string; bank: string; again: boolean }) {
  const { run, busy, go } = useApp();
  const open = () => run<BankConnectResult>(co, () => post("bank-connect", { co, bank }), undefined, (r) => {
    const url = r.done?.url;
    if (safePage(url)) {
      go({ co, tab: "setari", sec: "firme" }, { replace: true, at: `conn-${co}` });
      leave.to(url);
    }
    return false;
  });
  return (
    <Button type="button" variant="secondary" disabled={busy} onClick={() => void open()}>
      {again ? "Reconectează banca" : "Conectează banca"}
    </Button>
  );
}

/** Home: a connection ending soon, ended, or failing to read. */
export function bankTasks(s: State, own?: string): ReactElement[] {
  return (s.bank_connections ?? []).flatMap((b) => {
    const c = b.connection;
    if (!c || c.state === "ok") return [];
    const key = `bank${s.co}${b.bank}`; // per company: under Toate firmele two companies can share a bank
    const again = b.connect === "ready" && s.co !== "ALL" ? <ConnectBank co={s.co} bank={b.bank} again /> : null;
    if (c.state === "expiring") {
      return [<Task co={own} key={key} icon="bank" title={`Accesul la ${b.name} expiră pe ${dm(c.valid_until, s.today)}`}
        body="Conectează din nou banca pentru a continua citirea tranzacțiilor. Confirmă în aplicația băncii.">{again}</Task>];
    }
    if (c.state === "expired") {
      return [<Task co={own} key={key} icon="bank" title={`Accesul la ${b.name} a expirat`} late
        body="Tranzacțiile din bancă nu se mai citesc până la reconectare. Plățile se confirmă în continuare din extrase.">{again}</Task>];
    }
    return [<Task co={own} key={key} icon="bank" title={`Tranzacțiile de la ${b.name} nu au putut fi citite`}
      body={`${c.error ?? "Ultima citire nu a reușit."}${c.last_read ? ` Ultima citire reușită: ${dm(c.last_read, s.today)}.` : ""}`} />];
  });
}


type Source = NonNullable<BankStatus["sources"]>[number];

/** One way a bank's statements reach fiscalm, as a settings row: its name, its state as a badge on the right (the
 * engine's default also says "Principală"), then what to know and what to do. */
function SourceRow({ title, tone, state, main, children }: { title: string; tone: Tone; state: string; main?: boolean; children: ReactNode }) {
  return <li className="border-0 border-t border-solid border-line py-4 first:border-t-0 first:pt-0 last:pb-0">
    <div className="flex flex-wrap items-center gap-2">
      <h5 className="m-0 mr-auto text-[13px] font-semibold leading-5">{title}</h5>
      {main ? <span className={`${pill} ${statePillStyle[""]}`}>Principală</span> : null}
      <span className={`${pill} ${statePillStyle[tone]}`}>{state}</span>
    </div>
    <div className="mt-1 flex flex-col gap-2 text-[13px] leading-5 text-muted [&_a]:text-ink">{children}</div>
  </li>;
}
const pill = "rounded-badge px-1.5 py-0.5 text-xs font-medium";

/** Conexiune directă: whether the bank can be connected, its state, and connect, reconnect or stop. */
function DirectSource({ b, co, admin, s }: { b: BankStatus; co: string; admin: boolean; s?: Source }) {
  const { run, busy, view } = useApp();
  const c = b.connection;
  const [tone, state, line]: [Tone, string, string] = c ? connectionLine(c, view.today)
    : b.connect === "not_supported" ? ["", "Indisponibilă", "Banca nu se poate conecta direct."]
    : b.connect === "not_set_up" ? ["", "Neconfigurată", "Enable Banking nu este configurat."]
    : ["", "Neconectată", "Tranzacțiile se citesc zilnic, doar pentru citire."];
  const stop = () => run(co, () => post("bank-disconnect", { co, bank: b.bank }), { text: `Oprești citirea directă de la ${b.name}? Istoricul și extrasele rămân. Accesul acordat în bancă rămâne activ până expiră sau până îl revoci din bancă.`, yes: "Oprește citirea directă", danger: true });
  const actions = !admin ? null : b.connect === "not_set_up" && !c ? <L className="btn" to={{ int: "new:openbanking" }}>Configurează Enable Banking</L>
    : <>{b.connect === "ready" ? <ConnectBank co={co} bank={b.bank} again={!!c} /> : null}
      {c ? <Button type="button" variant="secondary" disabled={busy} onClick={() => void stop()}>Oprește citirea directă</Button> : null}</>;
  return <SourceRow title="Conexiune directă" tone={tone} state={state} main={s?.default}>
    <p className={`st ${tone}`}>{line}</p>
    {b.connect !== "not_supported" && !admin ? <p>Banca o conectează un administrator.</p> : null}
    {actions && b.connect !== "not_supported" ? <Actions className="btns mt-1!">{actions}</Actions> : null}
  </SourceRow>;
}

/** Extrase prin e-mail: whether fiscalm reads this bank's statement mail and which of the firm's inboxes read it, each
 * opening its sheet for whoever can read integrations (the engine names them, by id, only to them; `inboxes` is null
 * for anyone else). */
function MailSource({ s, password, inboxes }: { s?: Source; password: ReactNode; inboxes: Integration[] | null }) {
  const named = (s?.mailboxes ?? []).map((id) => inboxes?.find((i) => i.id === id)).filter((i) => !!i);
  const [tone, state]: [Tone, string] = !s?.supported ? ["", "Indisponibilă"] : s.available ? ["ok", "Activă"] : ["", "Neconfigurată"];
  return <SourceRow title="Extrase prin e-mail" tone={tone} state={state} main={s?.default}>
    {!s?.supported ? <p>fiscalm nu citește încă extrasele acestei bănci din e-mail.</p>
      : named.length ? <p>Se citesc din {named.map((i, n) => <span key={i.id}>{n ? ", " : ""}<L className={linkButtonStyle} to={{ int: i.id }}>{i.name}</L></span>)}.</p>
      : s.available ? <p>Se citesc din contul de e-mail al firmei.</p>
      : <p>Niciun cont de e-mail pornit nu citește pentru această firmă.</p>}
    {s?.supported && !s.available && inboxes ? <Actions className="btns mt-1!"><L className="btn" to={{ tab: "setari", sec: "integrari" }}>Alege un cont de e-mail</L></Actions> : null}
    {s?.supported ? password : null}
  </SourceRow>;
}

/** A firm's statement sources at one bank, as the engine reports them (fiscalm.bank.connections.bank_sources): every
 * available source works at the same time; the first that works is the default. `password` (a bank whose PDF statements
 * are protected) goes with the statements that arrive by mail, else with the uploaded ones. */
export function BankSources({ b, co, password, inboxes }: { b: BankStatus; co: string; password?: ReactNode; inboxes: Integration[] | null }) {
  const { view } = useApp();
  const admin = view.can_manage_companies !== false;
  const source = (key: Source["source"]) => b.sources?.find((s) => s.source === key);
  const byMail = !!source("mail")?.supported;
  return <ul className="m-0 list-none p-0" aria-label={`Surse de extrase de la ${b.name}`}>
    <DirectSource b={b} co={co} admin={admin} s={source("open_banking")} />
    <MailSource s={source("mail")} password={password} inboxes={inboxes} />
    <SourceRow title="Încărcare manuală" tone="ok" state="Activă" main={source("upload")?.default}>
      <p>Oricând, din Tranzacții.{password && byMail ? " Extrasele PDF folosesc aceeași parolă." : ""}</p>
      {!byMail ? password : null}
      {admin ? <Actions className="btns mt-1!"><L className="btn" to={{ co, tab: "tranzactii" }}>Încarcă extrasul</L></Actions> : <p>Extrasele le încarcă un administrator.</p>}
    </SourceRow>
  </ul>;
}

/** A phone may return to another browser: the offer is read when this app regains focus, never from the callback URL. */
export function BankAccountChoices({ co, company, banks, onChanged }: { co: string; company: string; banks: BankStatus[]; onChanged: () => void }) {
  const [offers, setOffers] = useState<BankAccountOffer[]>([]);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const request = useRef(0);
  useEffect(() => {
    let live = true;
    const load = async () => {
      const sent = ++request.current;
      try {
        const r = await post("bank-accounts", { co, operation: "list" }) as ActionResult & { done?: Done & { offers?: BankAccountOffer[] } };
        if (live && sent === request.current) { setFailed(!r.ok); if (r.ok) setOffers(r.done?.offers ?? []); }
      } catch { if (live && sent === request.current) setFailed(true); }
    };
    void load();
    const resume = () => { if (document.visibilityState === "visible") void load(); };
    window.addEventListener("focus", load);
    window.addEventListener("pageshow", resume);
    document.addEventListener("visibilitychange", resume);
    return () => { live = false; window.removeEventListener("focus", load); window.removeEventListener("pageshow", resume); document.removeEventListener("visibilitychange", resume); };
  }, [co, revision]);
  return <>
    {failed ? <p role="alert">Conturile autorizate nu au putut fi încărcate. <button type="button" className={linkButtonStyle} onClick={() => setRevision((n) => n + 1)}>Reîncearcă</button></p> : null}
    {offers.map((offer) => <AccountChoice key={offer.id} co={co} company={company} offer={offer} name={banks.find((b) => b.bank === offer.bank)?.name ?? "Banca"}
      onDone={() => { ++request.current; setOffers((rows) => rows.filter((r) => r.id !== offer.id)); onChanged(); }} />)}
  </>;
}

function AccountChoice({ co, company, offer, name, onDone }: { co: string; company: string; offer: BankAccountOffer; name: string; onDone: () => void }) {
  const { run, busy, view } = useApp();
  const [selected, setSelected] = useState<string[]>([]);
  const [owned, setOwned] = useState(false);
  const connect = async () => {
    if (busy || !owned || !selected.length) return;
    await run(co, () => post("bank-accounts", { co, operation: "confirm", offer: offer.id, accounts: selected }), undefined,
      (r) => { if (r.ok) onDone(); return false; });
  };
  return <Form className="my-2 rounded-control bg-inset p-4" aria-label={`Alege conturile de la ${name}`} onSubmit={(e) => { e.preventDefault(); void connect(); }}>
    <h4 className="m-0 mb-2 text-sm font-semibold">Conturi autorizate la {name}</h4>
    <p>Se aleg doar conturile acestei firme, în cel mult 15 minute de la autorizare. Niciun cont nu se citește înainte de confirmare.</p>
    {offer.accounts.map((a) => <Label key={a.id}>
      <Input type="checkbox" checked={selected.includes(a.id)} disabled={busy} onChange={(e) => setSelected((keys) => e.target.checked ? [...keys, a.id] : keys.filter((k) => k !== a.id))} />
      <span>{a.currency}, cont ••••{a.last4}. {a.existing ? "Deja înregistrat la firmă." : "Cont nou, încă neînregistrat la firmă."}</span>
    </Label>)}
    <Label><Input type="checkbox" checked={owned} disabled={busy} onChange={(e) => setOwned(e.target.checked)} />
      <span>Conturile selectate aparțin firmei {company}.</span></Label>
    <p className={noteLeftStyle}>Conturile noi se adaugă la firmă. Cele nebifate nu se citesc. Accesul expiră pe {dm(offer.valid_until, view.today)}.</p>
    <Button type="submit" disabled={busy || !owned || !selected.length}>Conectează conturile selectate</Button>
  </Form>;
}
