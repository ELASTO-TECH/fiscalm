"use client";
import { Actions, Button, Form, Panel, sectionHeadingStyle, rowStyle, rowTextStyle, backLinkStyle, bannerStyle, noteLeftStyle, settingsPageStyle } from "./Controls";
import { useContext, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, AuthError, getIdentity, getTokens, NO_ANSWER, postAuth, SESSION_ENDED } from "@/lib/api";
import { dmt, nOf } from "@/lib/format";
import { PRODUCT } from "@/lib/product";
import type { AccountSession, Identity, UserRole } from "@/lib/types";
import { useConfirm } from "./Confirm";
import { IdentityContext, useCan } from "./context";
import { I, L, Loading, Row, Title } from "./ui";
import { Tokens } from "./Access";
import { Codes, FactorCode, NewPassword, passwordProblem, type Factor } from "./Enrollment";
import { OwnPasskeys } from "./Passkeys";
import { useRequest } from "./AuthRequest";

export const ROLE_NAMES: Record<UserRole, string> = { admin: "Administrator", member: "Membru", readonly: "Doar citire" };

/** Cont inside the app's shell; with `back` (before the first firm, during setup) it stands alone and links back to the app. */
export function UserAccount({ back }: { back?: string }) {
  const session = useContext(IdentityContext)!;
  const [account, setAccount] = useState<Identity | null>(null), [read, setRead] = useState(0);
  const [error, setError] = useState(""), [failed, setFailed] = useState(false), [busy, setBusy] = useState(false);
  const sending = useRef(false); const owner = session.identity.me.id; const { ask, dialog } = useConfirm();
  useEffect(() => {
    let live = true; document.title = `Cont, ${PRODUCT}`;
    getIdentity().then(value => {
      if (!live) return;
      if (value.me.id !== owner || value.auth.recovery_only) { window.dispatchEvent(new Event(SESSION_ENDED)); return; }
      setAccount(value); setFailed(false);
    }, error => {
      if (!live) return;
      setFailed(true);
      if (error instanceof ApiError && error.status === 401) void session.refreshIdentity().catch(() => {});
    });
    return () => { live = false; };
  }, [read, owner, session.refreshIdentity]);
  const changed = async () => {
    setRead(n => n + 1);
    try { await session.refreshIdentity(); } catch { setFailed(true); }
  };
  const close = async (id?: string) => {
    if (sending.current) return;
    sending.current = true;
    const yes = await ask(id ? { text: "Închizi această sesiune? Pe dispozitivul ei trebuie să te conectezi din nou.", yes: "Închide sesiunea", danger: true }
      : { text: "Închizi celelalte sesiuni ale contului? Pe acele dispozitive trebuie să te conectezi din nou.", yes: "Închide celelalte sesiuni", danger: true });
    if (!yes) { sending.current = false; return; }
    setBusy(true); setError("");
    try {
      await postAuth(id ? "me/session-close" : "me/sessions-close-others", id ? { id } : {});
      await changed();
    } catch (e) {
      setError(e instanceof AuthError ? e.message === "Conectarea nu a reușit." ? "Sesiunea nu a putut fi închisă." : e.message : NO_ANSWER); setRead(n => n + 1);
    } finally { sending.current = false; setBusy(false); }
  };
  return <div className={settingsPageStyle}>
    {back ? <a className={backLinkStyle} href={back}><I n="arrow-left" />Înapoi la aplicație</a>
      : <L to={{ tab: "setari" }} className={backLinkStyle} label="Înapoi la Setări"><I n="arrow-left" />Setări</L>}<Title>Cont</Title>
    <p className="mb-5 break-words text-lg font-medium">{session.identity.me.display_name}</p>
    <Panel><p>Nume de utilizator: <b>{session.identity.me.username}</b>.</p><p>Rol: {ROLE_NAMES[session.identity.me.role]}.</p></Panel>
    {error && <p role="alert">{error}</p>}
    {failed && <div className={bannerStyle} role="alert"><p>Datele contului nu au putut fi actualizate.</p><Button variant="secondary" onClick={() => setRead(n => n + 1)}>Reîncearcă</Button></div>}
    {!account && !failed && <Loading />}
    {account && <>
      <div className={sectionHeadingStyle}><h2>Autentificare</h2></div><Panel>
        {account.factors.password_totp && <p>Parola și aplicația de autentificare sunt configurate.</p>}
        {account.factors.passkeys.length > 0 && <p>Chei de acces configurate: {account.factors.passkeys.length}.</p>}
        {!account.factors.password_totp && !account.factors.passkeys.length && <p>Configurarea autentificării nu este încheiată.</p>}
        <p>Coduri de recuperare disponibile: {account.recovery_codes_remaining}.</p>
        <Credentials passwordAvailable={account.factors.password_totp} onChanged={changed} />
      </Panel>
      <OwnPasskeys keys={account.factors.passkeys} passwordAvailable={account.factors.password_totp} available={account.factors.passkeys_available} onChanged={changed} />
      <AccountSessions account={account} busy={busy} close={close} />
    </>}
    {back ? <Tokens /> : <ApiKeys owner={owner} />}<Actions className="btns mt-8!"><SignOut /></Actions>{dialog}
  </div>;
}

function Credentials({ onChanged, passwordAvailable }: { onChanged: () => Promise<void>; passwordAvailable: boolean }) {
  const [mode, setMode] = useState<"" | "password" | "totp" | "codes">("");
  const [factor, setFactor] = useState<Factor | null>(null), [codes, setCodes] = useState<string[] | null>(null), [message, setMessage] = useState("");
  const request = useRequest(NO_ANSWER);
  const done = async () => { await onChanged(); setCodes(null); setFactor(null); setMode(""); };
  const choose = (value: typeof mode) => { setMessage(""); request.clear(); setMode(value); };
  if (codes) return <Codes codes={codes} onDone={done} />;
  if (factor) return <FactorCode factor={factor} unconfirmed={NO_ANSWER} onDone={done} onCancel={() => { setFactor(null); setMode(""); }} onRestart={() => setFactor(null)}
    finish={async code => {
      try { return (await postAuth<{ codes: string[] }>("me/totp/finish", { ticket: factor.ticket, code })).codes; }
      catch (e) { await onChanged(); throw e; }
    }} />;
  if (mode === "password") return <PasswordChange onUnconfirmed={onChanged} onCancel={() => setMode("")} onDone={async () => { await done(); setMessage("Parola a fost schimbată."); }} />;
  return <>
    {message && <p role="status">{message}</p>}
    {mode ? <div className="mt-4">
      <p className={noteLeftStyle}>{mode === "totp" ? "După confirmarea noului cod, celelalte sesiuni se închid și codurile de recuperare se înlocuiesc." : "Codurile vechi nu vor mai funcționa. Salvează noile coduri separat de parolă. Le vezi o singură dată."}</p>
      {request.feedback}<Actions>
        <Button disabled={request.busy} onClick={async () => {
          if (mode === "totp") { const value = await request.run(() => postAuth<Factor>("me/totp/begin", {})); if (value) setFactor(value); }
          else { const value = await request.run(() => postAuth<{ codes: string[] }>("me/recovery-codes", {})); if (value) setCodes(value.codes); else await onChanged(); }
        }}>{mode === "totp" ? "Configurează altă aplicație" : "Generează coduri noi"}</Button>
        <Button variant="secondary" disabled={request.busy} onClick={() => setMode("")}>Renunță</Button>
      </Actions>
    </div> : <Actions>
      {passwordAvailable && <><Button variant="secondary" onClick={() => choose("password")}>Schimbă parola</Button>
      <Button variant="secondary" onClick={() => choose("totp")}>Schimbă aplicația de autentificare</Button></>}
      <Button variant="secondary" onClick={() => choose("codes")}>Înlocuiește codurile de recuperare</Button>
    </Actions>}
  </>;
}

function PasswordChange({ onDone, onCancel, onUnconfirmed }: { onDone: () => Promise<void>; onCancel: () => void; onUnconfirmed: () => Promise<void> }) {
  const [password, setPassword] = useState(""), [confirm, setConfirm] = useState(""), [error, setError] = useState("");
  const request = useRequest(NO_ANSWER);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError("");
    const problem = passwordProblem(password, confirm);
    if (problem) { setConfirm(""); setError(problem); return; }
    const done = await request.run(() => postAuth("me/password-change", { password })); setConfirm("");
    if (done) { setPassword(""); await onDone(); } else await onUnconfirmed();
  };
  return <Form onSubmit={submit} aria-busy={request.busy}>
    <p className={noteLeftStyle}>Celelalte sesiuni se închid.</p>
    <NewPassword password={password} setPassword={setPassword} confirm={confirm} setConfirm={setConfirm} autoFocus />
    {error && <p role="alert">{error}</p>}{request.feedback}<Actions>
      <Button disabled={request.busy}>Salvează parola</Button><Button variant="secondary" type="button" disabled={request.busy} onClick={onCancel}>Renunță</Button>
    </Actions>
  </Form>;
}

/** In the app the keys live in Linkuri și chei API; Cont only counts the person's own active ones. */
function ApiKeys({ owner }: { owner: string }) {
  const allowed = useCan("own_tokens"), [active, setActive] = useState<number | null>(null);
  useEffect(() => {
    let live = true;
    if (allowed) getTokens().then(rows => { if (live) setActive(rows.filter(t => t.user_id === owner && !t.revoked_at).length); }, () => {});
    return () => { live = false; };
  }, [allowed, owner]);
  if (!allowed) return null;
  return <Panel padded={false} className="mt-6"><Row icon="plugs" title="Chei API" to={{ tab: "setari", sec: "acces" }}
    sub={active === null ? "" : active ? nOf(active, "1 cheie activă", "chei active") : "Nicio cheie activă"} /></Panel>;
}

function AccountSessions({ account, busy, close }: { account: Identity; busy: boolean; close: (id?: string) => Promise<void> }) {
  return <>
      <div className={sectionHeadingStyle}><h2>Sesiuni</h2></div>
      <Panel padded={false} className="divide-x-0 divide-y divide-solid divide-line">
        {!account.sessions.some(item => item.current) && <div className={rowStyle}><span className={rowTextStyle}><b>Sesiunea curentă</b><small>Acest dispozitiv.</small></span></div>}
        {account.sessions.map(item => <div className={`${rowStyle} flex-wrap sm:flex-nowrap`} key={item.id}>
        <SessionInfo item={item} title={item.current ? "Sesiunea curentă" : "Altă sesiune"} />
        {!item.current && <Button variant="secondary" disabled={busy} onClick={() => close(item.id)}>Închide sesiunea</Button>}
      </div>)}</Panel>
      {account.sessions.some(item => !item.current) && <Actions><Button variant="secondary" disabled={busy} onClick={() => close()}>Închide celelalte sesiuni</Button></Actions>}
  </>;
}

export function SessionInfo({ item, title, today = "" }: { item: AccountSession; title: string; today?: string }) {
  const methods = { password_totp: "Parolă și cod din aplicația de autentificare", passkey: "Cheie de acces", recovery: "Parolă și cod de recuperare", recovery_code: "Cod de recuperare" };
  const method = Object.hasOwn(methods, item.method) ? methods[item.method as keyof typeof methods] : "Metodă de conectare necunoscută";
  return <span className={`${rowTextStyle} break-words`}><b>{title}</b><small>{item.device}</small>
    <small>{method}.</small>
    <small>Ultima activitate: {dmt(item.last_seen_at, today)}. Adresă IP: {item.ip || "necunoscută"}.</small></span>;
}

export function SignOut() {
  const session = useContext(IdentityContext);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (!session) return null;
  return <><Button className="btn g" variant="secondary" type="button" disabled={busy} onClick={async () => {
    setBusy(true); setError("");
    try { await session.signOut(); } catch { setError("Ieșirea din cont nu a fost confirmată. Încearcă din nou."); }
    finally { setBusy(false); }
  }}>Ieși din cont</Button>{error && <p role="alert" className="basis-full">{error}</p>}</>;
}

