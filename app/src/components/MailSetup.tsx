"use client";
// Setări › Integrări › an inbox. Adding one asks the address (the engine names the provider and its server), then the
// password, then tests the connection read-only (fiscalm.mail.setup); a saved one can be changed and tested again.
// A Microsoft address signs in on Microsoft's page instead (MailMicrosoft.tsx); no password is asked.
import { Anchor, Button, Input, Label, Form, Actions, bannerStyle, noteLeftStyle, statePillStyle, useReason } from "./Controls";
import { useRef, useState, type ReactNode } from "react";
import { post } from "@/lib/api";
import { dmt, type Tone } from "@/lib/format";
import type { ActionResult, Integration, MailProvider, MailTest, MailTestCode } from "@/lib/types";
import { useApp, useCan, useCompany } from "./context";
import { MicrosoftConnect } from "./MailMicrosoft";
import { FirmScopeField, sameScope, scopeBody, scopeReason } from "./FirmScope";
import { CloseX, Sheet, SheetHead } from "./Sheets";
import { I, Summary } from "./ui";

/** What an inbox's row and sheet say about its reading: [text, tone]. */
export function mailLine(it: Integration, today: string): [string, Tone] {
  const r = it.reading;
  if (!it.enabled) return ["Oprită", ""];
  if (r?.code === "reconnect_required") return ["Reconectează contul Microsoft", "late"];
  if (r?.ok === false) {
    // The read's kind in the words of a failed test (the engine stores only connection and folder kinds).
    const why = FAILED[r.code as Exclude<MailTestCode, "ok">]?.(it.name, it.config.read_folder || "INBOX")[0] ?? "Ultima citire nu a reușit.";
    return [`${why}${r.last_ok ? ` Citită ultima dată pe ${dmt(r.last_ok, today)}` : ""}`, "soon"];
  }
  if (r?.last_ok) return [`Conectată. Citită pe ${dmt(r.last_ok, today)}`, "ok"];
  return ["Încă necitită", ""];
}

/** A failed test in words: what went wrong and the next step. `p`: the provider's name. */
const FAILED: Record<Exclude<MailTestCode, "ok">, (p: string, folder: string) => [string, string]> = {
  wrong_password: () => ["Parola nu este corectă.", "Verifică parola și testează din nou."],
  app_password_required: (p) => [`${p} nu acceptă parola contului.`, "Creează o parolă de aplicație și scrie-o în locul parolei contului."],
  host_unreachable: () => ["Serverul nu răspunde.", "Verifică serverul în Setări avansate. Dacă e corect, testează din nou peste câteva minute."],
  tls_error: () => ["Conexiunea securizată nu a reușit.", "Verifică serverul în Setări avansate."],
  folder_missing: (_, folder) => [`Dosarul ${folder} nu există.`, "Verifică numele dosarului în Setări avansate."],
  timeout: () => ["Serverul nu a răspuns la timp.", "Testează din nou peste câteva minute."],
  invalid_settings: () => ["Setările nu sunt complete.", "Verifică adresa, parola și Setări avansate. Se folosește doar portul 993."],
  unsupported_provider: (p) => [`${p} nu se poate conecta cu parolă.`, ""],
  protocol_error: () => ["Serverul a răspuns neașteptat.", "Testează din nou. Dacă se repetă, verifică serverul în Setări avansate."],
};

/** The provider's own page, only when it is https without a name or password in it. */
function safeUrl(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && !u.username && !u.password ? u.href : null;
  } catch {
    return null;
  }
}

const ADDRESS = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
type Settings = { host: string; username: string; read_folder: string; own_addresses: string };
const resultStyle = "mt-4 border-0 border-l-[3px] border-solid pl-3 text-sm leading-snug [&>b]:block";

/** An inbox's sheet. `actions`: a saved one's pause and remove buttons, from Integrări. */
export function MailSheet({ item, fresh, actions, onClose }: { item?: Integration; fresh: boolean; actions?: ReactNode; onClose: () => void }) {
  const { run, busy, view } = useApp();
  const co = useCompany().co;
  const permitted = useCan("manage_integrations");
  const saved: Settings = { host: item?.config.host ?? "", username: item?.config.username ?? "", read_folder: item?.config.read_folder || "INBOX",
    own_addresses: item?.config.own_addresses ?? "" };
  const [address, setAddress] = useState(item?.config.from_address ?? "");
  const [found, setFound] = useState<{ for: string; p: MailProvider } | null>(item ? { for: item.config.from_address, p: known(item) } : null);
  const [password, setPassword] = useState("");
  const [s, setS] = useState<Settings>(saved);
  const [scope, setScope] = useState(item?.firm_scope ?? "all");
  const [result, setResult] = useState<MailTest | null>(null);
  const [pending, setPending] = useState<"" | "detect" | "test">("");
  const [trouble, setTrouble] = useState("");
  // A new Microsoft inbox exists once Microsoft accepted the sign-in: its id, for the save that follows.
  const [linked, setLinked] = useState("");
  // A new Microsoft inbox exists from its first sign-in attempt, with the firms chosen then; they stay as shown until
  // the sign-in finishes, and change afterwards with Salvează.
  const [created, setCreated] = useState(false);
  const p = found && found.for === address.trim() ? found.p : null;
  // A saved password inbox can't become a Microsoft one: its sign-in starts as a new inbox, from Adaugă.
  const switching = !!item && item.config.method !== "microsoft" && p?.method === "microsoft";
  const ms = p?.method === "microsoft" && !switching;
  const imap = !!p?.supported && p.method !== "microsoft";
  const linkChanged = ms ? !item && !!linked : !item || !!password || address.trim() !== item.config.from_address
    || (["host", "username", "read_folder"] as const).some((k) => s[k] !== saved[k]);
  const dirty = linkChanged || s.own_addresses !== saved.own_addresses
    || !sameScope(scope, item?.firm_scope ?? "all");
  const firms = scopeBody(scope, view.companies, item?.firm_scope);
  const noFirm = scopeReason(scope, view.companies, item?.firm_scope);
  const ok = result?.code === "ok";
  const needsTest = !ms && linkChanged && !ok;
  // Every edit starts a new round: an answer that comes back after one describes settings no longer on screen.
  const round = useRef(0);
  const change = (f: () => void) => { round.current++; f(); setResult(null); setTrouble(""); };

  const detect = async () => {
    const a = address.trim();
    if (!ADDRESS.test(a)) return setTrouble("Scrie adresa de e-mail completă, de exemplu facturi@firma.ro.");
    setPending("detect");
    const mine = round.current;
    try {
      const r = await post("mail-detect", { co, address: a }) as ActionResult & { done?: MailProvider };
      if (mine !== round.current) return;
      if (!r.done || r.done.code) return setTrouble("Adresa de e-mail nu este corectă.");
      const d = r.done;
      setFound({ for: a, p: d });
      setS((x) => ({ ...x, host: d.host, username: d.username }));
    } catch {
      if (mine === round.current) setTrouble("Adresa nu a putut fi verificată. Reîncearcă.");
    } finally {
      setPending("");
    }
  };
  const test = async () => {
    if (!p) return;
    setPending("test");
    setTrouble("");
    // A saved inbox, unchanged, is tested with its saved password; a new server is tried only when the person changed it.
    const body = item && !linkChanged ? { co, id: item.id } : { co, from_address: address.trim(), password,
      ...(item || s.host !== p.host ? { host: s.host.trim() } : {}), username: s.username.trim(), read_folder: s.read_folder.trim() || "INBOX" };
    const mine = round.current;
    try {
      const r = await post("mail-test", body) as ActionResult & { done?: MailTest };
      if (mine !== round.current) return;
      const done = r.done ?? { code: "protocol_error" };
      setResult(done);
      if (done.code === "ok" && done.host) setS((x) => ({ ...x, host: done.host ?? x.host }));
    } catch {
      if (mine === round.current) setTrouble("Testul nu a putut porni. Reîncearcă.");
    } finally {
      setPending("");
    }
  };
  const saveMicrosoft = () => run(co, () => post("integration-update", { co, id: item?.id ?? linked, name: address.trim().slice(0, 60),
    f_own_addresses: s.own_addresses.trim(), ...firms }), undefined, () => { onClose(); return false; });
  const save = () => ms ? saveMicrosoft() : run(co, () => post(item ? "integration-update" : "integration-add", {
    co, ...(item ? { id: item.id } : { kind: "mail" }), name: address.trim().slice(0, 60), f_from_address: address.trim(),
    ...(password ? { f_password: password } : {}), f_host: s.host.trim(), f_port: "993", f_username: s.username.trim(),
    f_own_addresses: s.own_addresses.trim(), f_read_folder: s.read_folder.trim() || "INBOX", ...firms,
  }), undefined, () => { onClose(); return false; });

  const noPassword = !ms && !password && (!item || linkChanged);
  const why = useReason(!fresh || !permitted || pending ? "" : imap && noPassword ? "Scrie parola." : (imap || ms) ? noFirm : "");
  const [line, tone] = item ? mailLine(item, view.today) : ["", ""];
  const submit = () => {
    if (!p) void detect();
    else if (needsTest && !noPassword) void test();
    else if (!needsTest && dirty && !noFirm) void save();
  };
  return (
    <Sheet onClose={onClose} label={item ? item.name : "Adaugă o adresă de e-mail"}>
      <CloseX onClose={onClose} />
      <SheetHead title={item ? item.name : "Adaugă o adresă de e-mail"}
        sub={line ? <span className={`inline-block rounded-badge px-1.5 py-0.5 text-xs font-medium ${statePillStyle[tone]}`}>{line}</span> : undefined} />
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }} autoComplete="off">
        <Label htmlFor="mail-address">Adresa de e-mail</Label>
        <Input id="mail-address" type="email" inputMode="email" value={address} maxLength={254} spellCheck={false} data-autofocus
          readOnly={!!linked || (!!item && ms)}
          onChange={(e) => change(() => setAddress(e.target.value))}
          onBlur={() => { if (!p && !pending && ADDRESS.test(address.trim())) void detect(); }} />
        {p ? <Found p={p} /> : null}
        {p && !p.supported ? <Unsupported p={p} /> : null}
        {switching ? <div className={`${bannerStyle} mt-4 mb-0`} role="status"><b>Un cont Microsoft se adaugă ca adresă nouă.</b>
          <p className="mt-1 text-muted">Închide și alege Adaugă încă o adresă.</p></div> : null}
        {ms && permitted ? <MicrosoftConnect key={address.trim()} address={address.trim()} id={item?.id} again={!!item} scope={item ? {} : firms}
          urgent={!item || item.reading?.code === "reconnect_required"} disabled={!fresh || busy || !!noFirm}
          onCreated={() => setCreated(true)} onConnected={setLinked} /> : null}
        {imap || ms ? <FirmScopeField value={scope} onChange={setScope} firms={view.companies} disabled={created && !linked} /> : null}
        {ms && (item || linked) ? <Advanced s={s} imap={false} set={(k, v) => change(() => setS((x) => ({ ...x, [k]: v })))} /> : null}
        {imap ? <>
          {p.app_password_required ? <AppPassword p={p} /> : null}
          <Label htmlFor="mail-password">{p.app_password_required ? "Parola de aplicație" : "Parola"}</Label>
          <Input id="mail-password" type="password" value={password} maxLength={4096} autoComplete="new-password" spellCheck={false}
            placeholder={item?.secrets.password ? "Salvată. Lasă gol pentru a o păstra." : undefined}
            onChange={(e) => change(() => setPassword(e.target.value))} />
          <Advanced s={s} imap set={(k, v) => change(() => setS((x) => ({ ...x, [k]: v })))} />
        </> : null}
        {trouble ? <p className={noteLeftStyle} role="alert">{trouble}</p> : null}
        {result && p ? <Result r={result} p={p} folder={s.read_folder || "INBOX"} /> : null}
        {permitted ? <Actions>
          {!p ? <Button type="submit" disabled={!fresh || !!pending}>{pending === "detect" ? "Se verifică adresa…" : "Continuă"}</Button> : null}
          {imap ? <Button type="button" variant={needsTest ? "primary" : "secondary"} disabled={!fresh || !!pending || busy || noPassword}
            aria-describedby={why.id} onClick={() => void test()}>{pending === "test" ? "Se testează…" : ok ? "Testează din nou" : "Testează conexiunea"}</Button> : null}
          {(imap && !needsTest) || (ms && (item || linked)) ? <Button type="submit" disabled={!fresh || busy || !!pending || !dirty || !!noFirm}
            aria-describedby={why.id}>Salvează</Button> : null}
          {imap && needsTest && result ? <Button type="button" variant="secondary" disabled={!fresh || busy || !!pending || noPassword || !!noFirm}
            onClick={() => void save()}>Salvează oricum</Button> : null}
          {why.line}
        </Actions> : null}
      </Form>
      {actions}
    </Sheet>
  );
}

/** A saved inbox's server, without asking the engine again. */
const known = (it: Integration): MailProvider => ({ provider: it.config.method === "microsoft" ? "Microsoft" : "", host: it.config.host ?? "",
  port: 993, username: it.config.username ?? "", app_password_required: false, app_password_url: "", hint: "", supported: true,
  hosts: [it.config.host ?? ""], method: it.config.method === "microsoft" ? "microsoft" : "imap" });

function Found({ p }: { p: MailProvider }) {
  if (!p.provider || !p.supported) return null;
  return <p className="mt-2 mb-0 flex items-center gap-1.5 text-sm font-semibold text-ok [&_svg]:size-4" role="status">
    <I n="check-circle:fill" />{p.provider === "IMAP" ? `Server ${p.host}` : `Cont ${p.provider}`}</p>;
}

function AppPassword({ p }: { p: MailProvider }) {
  const url = safeUrl(p.app_password_url);
  return <div className={`${bannerStyle} mt-4 mb-0 space-y-2`}>
    <b>{p.provider} cere o parolă de aplicație.</b>
    <p>Parola contului nu merge aici. Creează o parolă de aplicație și copiaz-o mai jos. Dacă pagina nu are opțiunea,
      activează întâi verificarea în doi pași.</p>
    {url ? <Anchor href={url} target="_blank" rel="noopener noreferrer">Creează parola în {p.provider}<I n="arrow-up-right" /></Anchor> : null}
  </div>;
}

/** A provider fiscalm can't read (Proton): said calmly, with the way around it. */
function Unsupported({ p }: { p: MailProvider }) {
  return <div className={`${bannerStyle} mt-4 mb-0 space-y-2`} role="status">
    <b>{p.provider || "Această adresă"} nu se poate conecta direct.</b>
    <p className="text-muted">Fă o adresă separată pentru documente, de exemplu facturi.firma@gmail.com, și redirecționează spre ea
      facturile și extrasele din căsuța principală. Apoi adaug-o aici.</p>
  </div>;
}

function Result({ r, p, folder }: { r: MailTest; p: MailProvider; folder: string }) {
  if (r.code === "ok") return <div className={`${resultStyle} border-ok`} role="status"><b className="text-ok">Conexiune verificată.</b></div>;
  const [title, next] = FAILED[r.code](p.provider && p.provider !== "IMAP" ? p.provider : "Furnizorul", folder);
  const url = r.code === "app_password_required" ? safeUrl(p.app_password_url) : null;
  return <div className={`${resultStyle} border-danger`} role="alert"><b className="text-danger">{title}</b>
    {next ? <p className="mt-1 text-muted">{next}</p> : null}
    {url ? <p className="mt-1"><a href={url} target="_blank" rel="noopener noreferrer">Creează parola în {p.provider}</a></p> : null}
  </div>;
}

/** `imap`: the server fields too; a Microsoft inbox has no server to set. */
function Advanced({ s, imap, set }: { s: Settings; imap: boolean; set: (k: keyof Settings, v: string) => void }) {
  const field = (k: keyof Settings, label: string) => <>
    <Label htmlFor={`mail-${k}`}>{label}</Label>
    <Input id={`mail-${k}`} value={s[k]} maxLength={500} spellCheck={false} onChange={(e) => set(k, e.target.value)} />
  </>;
  return <details className="mt-4">
    <Summary>Setări avansate {imap ? <span className="font-normal text-muted">{s.host ? `${s.host}, port 993` : "port 993"}</span> : null}</Summary>
    {imap ? <>{field("host", "Server IMAP")}{field("username", "Utilizator")}{field("read_folder", "Dosar citit")}</> : null}
    {field("own_addresses", "Alte adrese proprii (separate prin virgulă)")}
  </details>;
}
