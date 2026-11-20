"use client";
import { Button, Input, Label, Form, Actions, noteLeftStyle } from "./Controls";
import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import { clearSetupContinuation } from "@/lib/setup-continuation";
import { AuthError, postAuth } from "@/lib/api";
import { createPasskey, passkeysSupported, rememberMethod, type CreationOptions, type PasskeyChallenge } from "@/lib/passkeys";
import { nOf } from "@/lib/format";
import { useRequest } from "./AuthRequest";
import { nativeProof, usePasskeyName, usePasskeyRequest } from "./Passkeys";
import { qrcodegen } from "../../public/fiscalm/qrcodegen";
import { I, Loading, Summary } from "./ui";

export type Factor = { ticket: string; secret: string; uri: string };
const UNAVAILABLE = "Cererea nu a fost confirmată. Încearcă din nou.";


/** Encoded entirely in this browser. The seed is never sent to a QR service. */
export function FactorCode({ factor, finish, onDone, onRestart, onCancel, unconfirmed }: { factor: Factor; finish: (code: string) => Promise<string[]>; onDone: () => Promise<void>; onRestart: () => void; onCancel?: () => void; unconfirmed?: string }) {
  const [code, setCode] = useState(""); const [codes, setCodes] = useState<string[] | null>(null);
  const request = useRequest(unconfirmed); const id = useId(); const input = useRef<HTMLInputElement>(null);
  const qr = useMemo(() => {
    const symbol = qrcodegen.QrCode.encodeText(factor.uri, qrcodegen.QrCode.Ecc.MEDIUM);
    const path: string[] = [];
    for (let y = 0; y < symbol.size; y++) for (let x = 0; x < symbol.size; x++) if (symbol.getModule(x, y)) path.push(`M${x + 4},${y + 4}h1v1h-1z`);
    return { size: symbol.size + 8, path: path.join("") };
  }, [factor.uri]);
  useEffect(() => { input.current?.focus(); }, []);
  const submit = async (event: FormEvent) => { event.preventDefault(); const result = await request.run(() => finish(code)); setCode(""); if (result) setCodes(result); };
  if (codes) return <Codes codes={codes} onDone={onDone} />;
  return <Form onSubmit={submit} aria-busy={request.busy}>
    <p className={noteLeftStyle}>Scanează codul QR cu o aplicație de autentificare. Apoi confirmă cu un cod de șase cifre din aplicație.</p>
    <svg className="mx-auto my-5 block h-auto w-[232px] max-w-full rounded-control" role="img" aria-label="Cod QR pentru aplicația de autentificare" viewBox={`0 0 ${qr.size} ${qr.size}`} shapeRendering="crispEdges"><rect width={qr.size} height={qr.size} fill="white" /><path d={qr.path} fill="black" /></svg>
    <details className="my-5 text-sm"><Summary>Introdu cheia manual</Summary><p className="mt-2 rounded-control bg-inset p-3 font-mono wrap-anywhere select-all">{factor.secret}</p></details>
    <Label htmlFor={id}>Cod din aplicația de autentificare</Label><Input ref={input} id={id} value={code} onChange={e => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required />
    {request.feedback}<Actions><Button disabled={request.busy}>Confirmă codul</Button>
    <Button variant="secondary" type="button" disabled={request.busy} onClick={onRestart}>Reîncepe configurarea</Button>
    {onCancel && <Button variant="secondary" type="button" disabled={request.busy} onClick={onCancel}>Renunță</Button>}</Actions>
  </Form>;
}

// Codes are held only in component state and disappear when this flow closes.
export function Codes({ codes, onDone }: { codes: string[]; onDone: () => Promise<void> }) {
  const [saved, setSaved] = useState(false), [message, setMessage] = useState(""); const request = useRequest();
  const text = codes.join("\n");
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setMessage("Codurile au fost copiate."); }
    catch { setMessage("Codurile nu au putut fi copiate. Salvează-le într-un fișier."); }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([text + "\n"], { type: "text/plain;charset=utf-8" }));
    const a = document.createElement("a"); a.href = url; a.download = "fiscalm-coduri-recuperare.txt"; a.click(); URL.revokeObjectURL(url);
    setMessage("Fișierul cu coduri a fost pregătit pentru salvare.");
  };
  return <div><h2 className="mt-6 mb-2">Coduri de recuperare</h2>
    <p className={noteLeftStyle}>Codurile se afișează o singură dată și fiecare funcționează o singură dată. Păstrează-le separat de parolă și de dispozitiv.</p>
    <ul className="mx-0 my-5 grid list-none grid-cols-[repeat(auto-fit,minmax(min(100%,160px),1fr))] gap-3 rounded-control bg-inset p-4 text-sm">{codes.map(code => <li key={code}><code>{code}</code></li>)}</ul>
    <Actions><Button variant="secondary" onClick={copy}>Copiază codurile</Button><Button variant="secondary" onClick={download}>Salvează fișierul</Button></Actions>
    <p role="status">{message}</p><Label><Input type="checkbox" checked={saved} onChange={e => setSaved(e.target.checked)} />Codurile au fost salvate într-un loc sigur.</Label>
    {request.feedback}<Actions><Button disabled={!saved || request.busy} onClick={() => request.run(onDone)}>Continuă</Button></Actions>
  </div>;
}

export function PasskeyEnrollment({ begin, finish, onDone, onCancel, cancelLabel = "Renunță" }: {
  begin: (name: string, signal: AbortSignal) => Promise<PasskeyChallenge<CreationOptions>>;
  finish: (ticket: string, proof: Record<string, string>, signal: AbortSignal) => Promise<string[]>;
  onDone: () => Promise<void>; onCancel?: () => void; cancelLabel?: string;
}) {
  const [name, setName] = usePasskeyName(), [attempted, setAttempted] = useState(false), [codes, setCodes] = useState<string[] | null>(null);
  const [unconfirmed, setUnconfirmed] = useState(false);
  const request = usePasskeyRequest(), id = useId();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void request.run(async signal => {
      setAttempted(true);
      const options = await begin(name, signal); signal.throwIfAborted();
      const proof = await nativeProof(() => createPasskey(options.publicKey, signal));
      let codes: string[];
      try {
        codes = await finish(options.ticket, proof, signal);
        if (!Array.isArray(codes) || codes.length !== 10 || codes.some(code => typeof code !== "string" || !code)) throw new TypeError("Invalid recovery codes");
      }
      catch (error) { if (!(error instanceof AuthError)) setUnconfirmed(true); throw error; }
      signal.throwIfAborted();
      setCodes(codes);
    });
  };
  if (codes) return <Codes codes={codes} onDone={onDone} />;
  return <Form onSubmit={submit} aria-busy={request.busy}>
    <Label htmlFor={id}>Numele cheii de acces</Label><Input id={id} value={name} onChange={e => setName(e.target.value)} maxLength={80} autoComplete="off" required />
    {request.feedback}<Actions><Button disabled={request.busy}><I n="fingerprint" /><span>{attempted ? "Reîncepe configurarea" : "Configurează cheia de acces"}</span></Button>
      {onCancel && <Button type="button" variant="secondary" disabled={request.busy} onClick={onCancel}>{cancelLabel}</Button>}</Actions>
    {/* A full navigation discards the setup flow and rechecks the actual cookie. */}
    {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
    {unconfirmed && <p className={noteLeftStyle}>Configurarea anterioară poate fi deja salvată. Verifică rezultatul în Cont, după ce te conectezi. Dacă nu ai salvat noile coduri, generează altele. <a href="/?tab=setari&amp;sec=cont">Verifică accesul la cont</a></p>}
  </Form>;
}

/** Counted as the engine counts (passwords.normalize): NFKC, then code points, not UTF-16 units. */
export const passwordLength = (value: string) => [...value.normalize("NFKC")].length;
export function passwordProblem(password: string, confirm: string): string {
  const length = passwordLength(password);
  return length < 12 || length > 128 ? "Parola trebuie să aibă între 12 și 128 de caractere." : password !== confirm ? "Parolele nu coincid." : "";
}

/** The rule is shown before typing and checked while typing; the engine's own reason still decides. */
export function NewPassword({ password, setPassword, confirm, setConfirm, autoFocus = false }: {
  password: string; setPassword: (value: string) => void; confirm: string; setConfirm: (value: string) => void; autoFocus?: boolean;
}) {
  const id = useId(), length = passwordLength(password), short = length > 0 && length < 12, differs = confirm.length > 0 && !password.startsWith(confirm);
  return <>
    <Label htmlFor={`${id}-password`}>Parolă nouă</Label>
    <Input id={`${id}-password`} type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} aria-describedby={`${id}-rule`} autoFocus={autoFocus} required />
    <p id={`${id}-rule`} className={noteLeftStyle} aria-live="polite">{short ? `Încă ${nOf(12 - length, "un caracter", "caractere")}.` : "Între 12 și 128 de caractere."}</p>
    <Label htmlFor={`${id}-confirm`}>Repetă parola</Label>
    <Input id={`${id}-confirm`} type="password" autoComplete="new-password" value={confirm} onChange={e => setConfirm(e.target.value)} aria-describedby={`${id}-match`} required />
    <p id={`${id}-match`} className={noteLeftStyle} aria-live="polite">{differs ? "Parolele nu coincid." : ""}</p>
  </>;
}

export function SetupAccount({ link, onDone, passkeysAvailable = false }: { link: string; onDone: () => Promise<void>; passkeysAvailable?: boolean }) {
  const [user, setUser] = useState<{ username: string } | null>(null), [error, setError] = useState("");
  const [password, setPassword] = useState(""), [confirm, setConfirm] = useState(""), [factor, setFactor] = useState<Factor | null>(null);
  const request = useRequest(); const { defer } = request; const [read, setRead] = useState(0);
  const keys = passkeysAvailable && passkeysSupported(), [key, setKey] = useState(keys);
  useEffect(() => {
    let live = true;
    postAuth<{ user: { username: string } }>("setup/inspect", { link }).then(v => { if (live) setUser(v.user); }, e => { if (live) { if (e instanceof AuthError && e.status === 401) clearSetupContinuation(); defer(e); setError(e instanceof AuthError && e.status === 401 ? "Linkul nu poate fi folosit. Un administrator poate crea un link nou de configurare." : UNAVAILABLE); } });
    return () => { live = false; };
  }, [link, read, defer]);
  const begin = async (event: FormEvent) => {
    event.preventDefault(); setError("");
    const problem = passwordProblem(password, confirm);
    if (problem) { setConfirm(""); setError(problem); return; }
    const result = await request.run(() => postAuth<Factor>("setup/begin", { link, password }));
    setConfirm(""); if (result) { setPassword(""); setFactor(result); }
  };
  if (factor) return <FactorCode factor={factor} onDone={async () => { rememberMethod("password"); await onDone(); }} onRestart={() => setFactor(null)} finish={async code => (await postAuth<{ codes: string[] }>("setup/finish", { link, ticket: factor.ticket, code })).codes} />;
  if (!user) return error ? <><p role="alert">{error}</p>{request.feedback}{error === UNAVAILABLE && <Actions><Button disabled={request.busy} onClick={() => { setError(""); setRead(n => n + 1); }}>Reîncearcă</Button></Actions>}</> : <Loading />;
  const account = <p className={noteLeftStyle}>Cont: <b>{user.username}</b>.</p>;
  if (key) return <>{account}<PasskeyEnrollment onDone={async () => { rememberMethod("passkey"); await onDone(); }} onCancel={() => setKey(false)} cancelLabel="Folosește parola și codul"
    begin={(name, signal) => postAuth("setup/passkey/options", { link, name }, signal)}
    finish={async (ticket, proof, signal) => (await postAuth<{ codes: string[] }>("setup/passkey/finish", { link, ticket, ...proof }, signal)).codes} /></>;
  return <><Form onSubmit={begin} aria-busy={request.busy}>
    {account}<NewPassword password={password} setPassword={setPassword} confirm={confirm} setConfirm={setConfirm} />
    {error && <p role="alert">{error}</p>}{request.feedback}<Actions><Button disabled={request.busy}>Configurează autentificarea</Button>
  {keys && <Button type="button" variant="secondary" disabled={request.busy} onClick={() => { setPassword(""); setConfirm(""); setError(""); request.clear(); setKey(true); }}><I n="fingerprint" /><span>Folosește o cheie de acces</span></Button>}</Actions></Form></>;
}

/** One way back in: the engine decides from the verified code whether the password comes next. */
export function AccessRecovery({ onDone, onBack }: { onDone: () => Promise<void>; onBack: () => void }) {
  const [username, setUsername] = useState(""), [code, setCode] = useState(""), [password, setPassword] = useState("");
  const [ticket, setTicket] = useState(""), [account, setAccount] = useState(""), [unconfirmed, setUnconfirmed] = useState(false);
  const request = usePasskeyRequest(), id = useId();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void request.run(async signal => {
      const body: Record<string, string> = ticket ? { username: account, ticket, password } : { username, code }; setCode(""); setPassword("");
      let next: { ticket?: string };
      try { next = await postAuth<{ ticket?: string }>(ticket ? "recover/password" : "recover/code", body, signal); }
      catch (error) { if (!(error instanceof AuthError) || error.status < 400) setUnconfirmed(true); throw error; }
      signal.throwIfAborted();
      if (!ticket && next.ticket) { setAccount(body.username); setTicket(next.ticket); } else await onDone();
    });
  };
  return <Form onSubmit={submit} aria-busy={request.busy}>
    {ticket ? <><p className={noteLeftStyle}>Cont: <b>{account}</b>.</p>
      <Label htmlFor={`${id}-password`}>Parolă</Label><Input id={`${id}-password`} type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" autoFocus required /></>
    : <><Label htmlFor={`${id}-username`}>Nume de utilizator</Label><Input id={`${id}-username`} value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" maxLength={64} required />
      <Label htmlFor={`${id}-code`}>Cod de recuperare</Label><Input id={`${id}-code`} value={code} onChange={e => setCode(e.target.value)} autoComplete="one-time-code" maxLength={32} required /></>}
    {request.feedback}<Actions><Button disabled={request.busy}>Continuă</Button>
      {unconfirmed && <Button variant="secondary" type="button" disabled={request.busy} onClick={() => request.run(async signal => { signal.throwIfAborted(); await onDone(); })}>Verifică recuperarea contului</Button>}
      <Button variant="secondary" type="button" disabled={request.busy} onClick={onBack}>Înapoi la conectare</Button></Actions>
  </Form>;
}

export function RecoveryAccount({ onDone, keyOnly = false }: { onDone: () => Promise<void>; keyOnly?: boolean }) {
  if (keyOnly) return passkeysSupported() ? <><p className={noteLeftStyle}>Configurează o cheie de acces nouă. Cheile și codurile vechi nu vor mai funcționa.</p>
    <PasskeyEnrollment onDone={onDone} begin={(name, signal) => postAuth("recover/passkey/options", { name }, signal)}
      finish={async (ticket, proof, signal) => (await postAuth<{ codes: string[] }>("recover/passkey/finish", { ticket, ...proof }, signal)).codes} />
  </> : <p role="alert">Pentru a înlocui cheia de acces, folosește un browser compatibil.</p>;
  return <FactorRecovery onDone={onDone} />;
}

/** The recovery session exists only to replace the factor, so its QR code opens without asking. */
function FactorRecovery({ onDone }: { onDone: () => Promise<void> }) {
  const [factor, setFactor] = useState<Factor | null>(null), [error, setError] = useState(""), [read, setRead] = useState(0);
  const request = useRequest(); const { defer } = request;
  useEffect(() => {
    let live = true;
    postAuth<Factor>("recover/begin", {}).then(v => { if (live) setFactor(v); }, e => { if (live) { defer(e); setError(e instanceof AuthError ? e.message : UNAVAILABLE); } });
    return () => { live = false; };
  }, [read, defer]);
  if (factor) return <FactorCode factor={factor} onDone={onDone} onRestart={() => { setFactor(null); setRead(n => n + 1); }} finish={async code => (await postAuth<{ codes: string[] }>("recover/finish", { ticket: factor.ticket, code })).codes} />;
  return error ? <><p role="alert">{error}</p>{request.feedback}<Actions><Button disabled={request.busy} onClick={() => { setError(""); setRead(n => n + 1); }}>Reîncearcă</Button></Actions></> : <Loading />;
}
