"use client";
import { formTypography, noteLeftStyle, ActionColumn, Button, Input, Label, Form, Actions, dropCopies } from "./Controls";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ApiError, AuthError, CANCEL_REAUTHENTICATION, getBootstrap, getIdentity, identityKey, suspendRequests, HTTPS_REQUIRED, postAuth, REAUTHENTICATE, SESSION_CHANGED, SESSION_ENDED, type Reauthentication } from "@/lib/api";
import { clearSetupContinuation, setupContinuation } from "@/lib/setup-continuation";
import { nOf } from "@/lib/format";
import { PRODUCT } from "@/lib/product";
import { Brand } from "./Brand";
import { parse } from "@/lib/nav";
import type { Bootstrap, Identity } from "@/lib/types";
import { passkeysSupported, rememberedMethod, rememberMethod } from "@/lib/passkeys";
import { PASSKEY_FLOW, PasskeySignIn, type PasskeyFlow } from "./Passkeys";
import { SetupAccount, RecoveryAccount, AccessRecovery } from "./Enrollment";
import { SignOut } from "./UserAccount";
import { CloseX, Sheet } from "./Sheets";
import { BootstrapContext, IdentityContext } from "./context";
import { I, Loading } from "./ui";

const UNAVAILABLE = "Nu te poți conecta acum. Încearcă din nou.";

/** All identity reads share one generation, so expiry, sign-out and newer reads invalidate late replies. */
function useIdentity(bootstrap: boolean) {
  const [initial, setInitial] = useState<Bootstrap | null>(null);
  const [identity, setIdentity] = useState<Identity | null>();
  const [error, setError] = useState(""); const [checking, setChecking] = useState(true);
  const [passkeysAvailable, setPasskeysAvailable] = useState(false);
  // An ended session keeps the signed-in screen (inert under a sign-in sheet) instead of discarding the work on it.
  const [expired, setExpired] = useState(false);
  // strict: an explicit re-read (another tab's change, a retry) is pending; until it confirms an identity, any 401 discards the screen.
  const generation = useRef(0), live = useRef(true), shown = useRef<Identity | null | undefined>(undefined), strict = useRef(false);
  const update = useCallback((value: Identity | null | undefined) => {
    generation.current++; shown.current = value; strict.current = false; suspendRequests(false);
    setIdentity(value); setExpired(false); setError(""); setChecking(false);
  }, []);
  const keeps = () => !!shown.current && !shown.current.auth.recovery_only && !strict.current;
  /** The kept screen sends nothing (api.ts) until a read confirms who is signed in. */
  const expire = () => { suspendRequests(true); setChecking(false); setExpired(true); };
  /** keep: a refusal while the same screen is open ends the session but keeps the screen; another tab's change never does. */
  const read = useCallback((keep = false) => {
    const current = ++generation.current;
    return (bootstrap && !keep ? getBootstrap(parse(window.location.search).co || "ALL", true).then(result => {
      if (live.current && current === generation.current) setInitial(result);
      return result.identity;
    }) : getIdentity()).then(value => {
      if (live.current && current === generation.current) { shown.current = value; strict.current = false; suspendRequests(false); setIdentity(value); setExpired(false); setError(""); setPasskeysAvailable(value.factors.passkeys_available); return value; }
      return null;
    }, e => {
      if (live.current && current === generation.current) {
        if (e instanceof ApiError && e.status === 401 && keep && keeps()) expire();
        else if (e instanceof ApiError && e.status === 401) { shown.current = null; strict.current = false; suspendRequests(false); setIdentity(null); setExpired(false); setPasskeysAvailable(e.passkeysAvailable); }
        else setError(e instanceof ApiError && e.status === 426 ? HTTPS_REQUIRED : UNAVAILABLE);
      }
      throw e;
    }).finally(() => { if (live.current && current === generation.current) setChecking(false); });
  }, [bootstrap]);
  const refresh = useCallback(() => { strict.current = true; setChecking(true); setError(""); return read(); }, [read]);
  const revalidate = useCallback(() => read(true), [read]);
  useEffect(() => {
    live.current = true;
    const ended = () => { if (!keeps()) update(null); else { generation.current++; expire(); } };
    window.addEventListener(SESSION_ENDED, ended);
    void read().catch(() => {});
    return () => { live.current = false; suspendRequests(false); window.removeEventListener(SESSION_ENDED, ended); };
  }, [read, update]);
  const reload = useCallback(async () => { await refresh(); }, [refresh]);
  return { initial, identity, update, error, checking, refresh, reload, revalidate, passkeysAvailable, expired };
}

/** `pending`: what shows while the session is not known yet (the first paint, a re-check): never a sign-in or a signed-in
 * screen, which only an answer can choose. */
export function Authentication({ children, pending = <Loading />, bootstrap = false }: { children: ReactNode; pending?: ReactNode; bootstrap?: boolean }) {
  const [setupLink, setSetupLink] = useState<string | null>(null);
  const setupRead = useRef(false);
  useEffect(() => {
    const changed = () => setSetupLink(setupContinuation(SESSION_CHANGED));
    if (!setupRead.current) { setupRead.current = true; changed(); }
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const { initial, identity, update, error, checking, refresh, reload: refreshIdentity, revalidate, passkeysAvailable, expired } = useIdentity(bootstrap);
  const [signIn, setSignIn] = useState(0);
  useEffect(() => {
    const native = new Set<AbortSignal>(); let focusDeferred = false;
    const nativeFlow = (event: Event) => {
      const { signal, active } = (event as CustomEvent<PasskeyFlow>).detail;
      if (active) native.add(signal); else native.delete(signal);
      if (!native.size && focusDeferred) { focusDeferred = false; void revalidate().catch(() => {}); }
    };
    // Coming back fires focus and visibilitychange together: one routine read answers both (only these, never a sign-in's).
    let returning: Promise<unknown> | null = null;
    // Returning to this tab preserves its own setup/recovery code acknowledgement.
    const refresh = (background = false) => {
      window.dispatchEvent(new Event(CANCEL_REAUTHENTICATION));
      // A routine return keeps the confirmed page visible; errors and session changes still block it.
      if (!background) { void refreshIdentity().catch(() => {}); return; }
      returning ??= revalidate().catch(() => {}).finally(() => { returning = null; });
    };
    const visible = () => {
      if (document.visibilityState !== "visible" || setupLink !== null || identity?.auth.recovery_only) return;
      if (native.size) { focusDeferred = true; return; }
      refresh(true);
    };
    const changed = (event: StorageEvent) => {
      if (event.key !== SESSION_CHANGED && event.key !== null) return;
      if (setupLink !== null || identity?.auth.recovery_only) {
        // Another tab changed the session; discard secrets from this account flow.
        clearSetupContinuation(); setSetupLink(null); update(undefined);
        if (window.location.pathname === "/setup") window.history.replaceState(window.history.state, "", "/");
      }
      if (!identity) setSignIn(n => n + 1);  // Discards typed passwords, codes and recovery tickets on this screen.
      refresh();
    };
    window.addEventListener(PASSKEY_FLOW, nativeFlow); window.addEventListener("storage", changed); window.addEventListener("focus", visible);
    document.addEventListener("visibilitychange", visible);
    return () => { window.removeEventListener(PASSKEY_FLOW, nativeFlow); window.removeEventListener("storage", changed); window.removeEventListener("focus", visible); document.removeEventListener("visibilitychange", visible); };
  }, [setupLink, identity, refreshIdentity, revalidate, update]);
  useEffect(() => { if (setupLink !== null) document.title = `Configurează contul, ${PRODUCT}`; else if (!identity || identity.auth.recovery_only) document.title = `Conectare, ${PRODUCT}`; }, [identity, setupLink]);
  const [notice, setNotice] = useState(""), [renewed, setRenewed] = useState(false);
  // A sheet fading out never outlives the account it was opened under, nor shows through a check of who is signed in.
  const viewer = identity?.me.id;
  useLayoutEffect(() => { if (checking || error) dropCopies(); }, [checking, error]);
  useLayoutEffect(() => dropCopies, [viewer]);
  useEffect(() => { if (renewed) { const timer = setTimeout(() => setRenewed(false), 6000); return () => clearTimeout(timer); } }, [renewed]);
  const signedIn = async () => { setNotice(""); if (setupLink !== null) window.history.replaceState(window.history.state, "", "/"); clearSetupContinuation(); setSetupLink(null); update(undefined); await refreshIdentity(); };
  const signOut = async () => {
    try { await postAuth("signout", {}); }
    catch (e) { if (!(e instanceof ApiError && e.status === 401)) throw e; }
    update(null);
  };
  const feedback = <><p role="alert">{error}</p>{error !== HTTPS_REQUIRED && <Actions className="btns mt-0!"><Button onClick={() => { void refreshIdentity().catch(() => {}); }}>Reîncearcă</Button></Actions>}</>;
  if (setupLink === null && identity && !identity.auth.recovery_only) {
    const blocked = checking || !!error;
    return <IdentityContext.Provider key={identity.me.id} value={{ identity, signOut, refreshIdentity }}>
      <div inert={blocked || expired} style={{ display: blocked ? "none" : "contents" }}><Confirmation identity={identity} readIdentity={refresh}><BootstrapContext.Provider value={initial && identityKey(initial.identity) === identityKey(identity) ? initial : null}>{children}</BootstrapContext.Provider></Confirmation></div>
      {expired && !blocked && <SessionEnded identity={identity} readIdentity={revalidate} renewed={() => setRenewed(true)} />}
      {renewed && <p role="status" className="fixed inset-x-4 bottom-4 z-50 mx-auto max-w-[420px] rounded-control bg-ink p-3 text-center text-sm text-canvas">Te-ai conectat din nou.</p>}
      {blocked && (error ? <main className={`auth-checking grid min-h-svh place-content-center justify-items-center gap-4 bg-canvas px-4 py-8 text-center ${formTypography}`}>{feedback}</main>
        : <div className="auth-checking">{pending}</div>)}
    </IdentityContext.Provider>;
  }
  if (identity === undefined && !error) return pending;
  return <main className="min-h-svh grid place-items-center bg-canvas px-4 py-8"><section className={`w-full max-w-[420px] rounded-card border border-solid border-line bg-surface p-6 sm:p-8 [&_.wm]:mb-6 [&_.wm]:text-2xl [&_h1]:m-0 [&_h1]:mb-6 [&_h1]:text-3xl [&_h1]:font-semibold [&_h1]:tracking-tight [&_form]:mt-5 [&_p]:mx-0 ${formTypography}${setupLink !== null ? " auth-enrollment" : ""}`} aria-labelledby="auth-title"><ActionColumn>
    <Brand />{error ? <><Title>Conectare</Title>{feedback}</> : setupLink !== null ? <><Title>Configurează contul</Title><SetupAccount key={setupLink} link={setupLink} passkeysAvailable={passkeysAvailable} onDone={signedIn} /></>
        : identity?.auth.recovery_only ? <><Title>Recuperează contul</Title><RecoveryAccount keyOnly={identity.auth.method === "recovery_code"} onDone={identity.auth.method === "recovery_code" ? async () => { rememberMethod("passkey"); setNotice("Cheia de acces a fost configurată. Conectează-te cu ea."); update(null); } : signedIn} /><IdentityContext.Provider value={{ identity, signOut, refreshIdentity }}><Actions><SignOut /></Actions></IdentityContext.Provider></>
        : <SignIn key={signIn} notice={notice} passkeysAvailable={passkeysAvailable} onDone={signedIn} />}
  </ActionColumn></section></main>;
}

const Title = ({ children }: { children: string }) => <h1 id="auth-title">{children}</h1>;

/** Recovery is its own job from its first screen: the title says so before any code is checked. */
function SignIn({ notice, passkeysAvailable, onDone }: { notice: string; passkeysAvailable: boolean; onDone: () => Promise<void> }) {
  const [recovery, setRecovery] = useState(false);
  if (recovery) return <><Title>Recuperează contul</Title><AccessRecovery onDone={onDone} onBack={() => setRecovery(false)} /></>;
  return <><Title>Conectare</Title>{notice && <p role="status">{notice}</p>}<PasswordCode passkeysAvailable={passkeysAvailable} onDone={onDone} onRecover={() => setRecovery(true)} /></>;
}

function usePasswordCode(onDone: () => Promise<void>, stepUp: boolean, known: string) {
  const [username, setUsername] = useState(known);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [ticket, setTicket] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [delay, setDelay] = useState(0);
  useEffect(() => { if (delay) { const timer = setTimeout(() => setDelay(n => Math.max(0, n - 1)), 1000); return () => clearTimeout(timer); } }, [delay]);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (busy || delay) return;
    setBusy(true); setError("");
    try {
      if (ticket) { await postAuth(stepUp ? "step-up/totp" : "totp", { ticket, code }); await onDone(); }
      else { const result = await postAuth<{ ticket: string }>(stepUp ? "step-up/password" : "password", stepUp ? { password } : { username, password }); setTicket(result.ticket); }
    } catch (e) {
      setError(e instanceof AuthError ? e.message : UNAVAILABLE);
      if (e instanceof AuthError && e.retryAfter > 0) setDelay(e.retryAfter);
    } finally { setPassword(""); setCode(""); setBusy(false); }
  };
  return { username, setUsername, password, setPassword, code, setCode, ticket, busy, error, delay, submit,
    restart: () => { setTicket(""); setCode(""); setError(""); } };
}

/** A passkey comes first where it can work, unless this device last signed in with the password. */
/** `children`: more buttons (Renunță, Ieși din cont), last in the same column as the other ways in. */
export function PasswordCode({ onDone, onRecover, stepUp = false, passkeysAvailable = false, passwordAvailable = true, username = "", children }: { onDone: () => Promise<void>; onRecover?: () => void; stepUp?: boolean; passkeysAvailable?: boolean; passwordAvailable?: boolean; username?: string; children?: ReactNode }) {
  const available = passkeysAvailable && passkeysSupported();
  const [key, setKey] = useState(() => !passwordAvailable || (available && (stepUp || rememberedMethod() !== "password")));
  const switchMethod = key ? passwordAvailable : available;
  return <ActionColumn>{key ? available ? <PasskeySignIn stepUp={stepUp} onDone={async () => { rememberMethod("passkey"); await onDone(); }} /> : <p role="alert">Confirmă cu o cheie de acces, într-un browser compatibil.</p> : <PasswordForm onDone={async () => { rememberMethod("password"); await onDone(); }} stepUp={stepUp} username={username} />}
    {(switchMethod || onRecover || children) && <Actions className="btns mt-2!">
      {switchMethod && <Button variant="secondary" type="button" onClick={() => setKey(!key)}>{!key && <I n="fingerprint" />}<span>{key ? "Folosește parola și codul" : "Folosește o cheie de acces"}</span></Button>}
      {children}
      {onRecover && <Button variant="quiet" type="button" onClick={onRecover}>Nu mai am acces</Button>}
    </Actions>}
  </ActionColumn>;
}

function PasswordForm({ onDone, stepUp, username }: { onDone: () => Promise<void>; stepUp: boolean; username: string }) {
  const form = usePasswordCode(onDone, stepUp, username);
  const id = useId();
  const code = useRef<HTMLInputElement>(null);
  useEffect(() => { if (form.ticket) code.current?.focus(); }, [form.ticket]);
  return <Form aria-label={stepUp ? "Confirmarea identității" : "Conectare"} onSubmit={form.submit} aria-busy={form.busy}>
    <p className={noteLeftStyle}>{form.ticket ? "Găsești codul în aplicația de autentificare." : stepUp ? "Confirmă cu parola și codul din aplicația de autentificare." : "Conectează-te cu parola și codul din aplicația de autentificare."}</p>
    {form.ticket ? <><Label htmlFor={`${id}-code`}>Cod din aplicația de autentificare</Label>
      <Input ref={code} id={`${id}-code`} value={form.code} onChange={e => form.setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required />
    </> : <>{username ? <p className={noteLeftStyle}>Cont: <b>{username}</b>.</p> : !stepUp && <><Label htmlFor={`${id}-username`}>Nume de utilizator</Label>
      <Input id={`${id}-username`} value={form.username} onChange={e => form.setUsername(e.target.value)} autoComplete="username" maxLength={64} required /></>}
      <Label htmlFor={`${id}-password`}>Parolă</Label>
      <Input id={`${id}-password`} type="password" value={form.password} onChange={e => form.setPassword(e.target.value)} autoComplete="current-password" data-autofocus={stepUp || username ? "" : undefined} required />
    </>}
    {form.error && <p role="alert">{form.error}</p>}
    {form.delay > 0 && <p role="status">Încearcă din nou peste {nOf(form.delay, "o secundă", "secunde")}.</p>}
    <Actions><Button type="submit" disabled={form.busy || form.delay > 0}>{form.ticket ? stepUp ? "Confirmă identitatea" : "Intră în cont" : "Continuă"}</Button>
      {form.ticket && <Button variant="quiet" type="button" disabled={form.busy} onClick={form.restart}>{stepUp ? "Introdu parola din nou" : "Folosește alt cont"}</Button>}</Actions>
  </Form>;
}


type ConfirmationPending = { finish: (accepted: boolean) => void; owner: string };

/** A challenge belongs to the currently displayed account; cancellation never repeats the operation. */
function useConfirmation(identity: Identity, readIdentity: () => Promise<Identity | null>) {
  const [pending, setPending] = useState<ConfirmationPending | null>(null);
  const active = useRef<ConfirmationPending | null>(null);
  // Registered during the commit, not after paint: a refusal answered while the screen is already visible still finds its sheet.
  useLayoutEffect(() => {
    let live = true;
    const ask = (event: Event) => {
      const request = (event as CustomEvent<Reauthentication>).detail;
      event.preventDefault();
      if (active.current || request.signal?.aborted) { request.finish(false); return; }
      const cancel = () => next.finish(false);
      const next: ConfirmationPending = { owner: identity.me.id, finish: accepted => {
        if (active.current !== next) return;
        active.current = null; request.signal?.removeEventListener("abort", cancel);
        request.finish(accepted); if (live) setPending(null);
      } };
      active.current = next; request.signal?.addEventListener("abort", cancel, { once: true });
      void readIdentity().then(current => {
        if (!live || active.current !== next) return;
        if (!current || current.me.id !== next.owner || current.auth.recovery_only) next.finish(false);
        else setPending(next);
      }, () => next.finish(false));
    };
    const cancel = () => active.current?.finish(false);
    window.addEventListener(REAUTHENTICATE, ask); window.addEventListener(CANCEL_REAUTHENTICATION, cancel);
    return () => { live = false; window.removeEventListener(REAUTHENTICATE, ask); window.removeEventListener(CANCEL_REAUTHENTICATION, cancel); cancel(); };
  }, [identity.me.id, readIdentity]);
  const verified = async () => {
    const next = pending; if (!next || active.current !== next) return;
    const current = await readIdentity();
    if (active.current !== next) return;
    const accepted = current?.me.id === next.owner && !current.auth.recovery_only;
    next.finish(accepted);
  };
  return { pending, verified };
}

function Confirmation({ identity, readIdentity, children }: { identity: Identity; readIdentity: () => Promise<Identity | null>; children: ReactNode }) {
  const { pending, verified } = useConfirmation(identity, readIdentity);
  return <>{children}{pending && createPortal(<Sheet label="Confirmă identitatea" onClose={() => pending.finish(false)}>
    <CloseX onClose={() => pending.finish(false)} /><div className="[&_p]:mx-0"><h2>Confirmă identitatea</h2>
    <p className={noteLeftStyle}>Cont: <b>{identity.me.username}</b>. Confirmă din nou identitatea ca să continui.</p>
    <PasswordCode stepUp onDone={verified} passkeysAvailable={identity.factors.passkeys_available && identity.factors.passkeys.length > 0} passwordAvailable={identity.factors.password_totp}>
      <Button type="button" variant="secondary" onClick={() => pending.finish(false)}>Renunță</Button></PasswordCode></div>
  </Sheet>, document.body)}</>;
}

/** The same account unlocks its kept screen; another account replaces it (keyed by me.id). Nothing failed is resent. */
function SessionEnded({ identity, readIdentity, renewed }: { identity: Identity; readIdentity: () => Promise<Identity | null>; renewed: () => void }) {
  const signedIn = async () => { const current = await readIdentity(); if (current?.me.id === identity.me.id) renewed(); };
  return createPortal(<Sheet label="Sesiunea a expirat" onClose={() => {}}><div className="[&_p]:mx-0"><h2>Sesiunea a expirat</h2>
    <PasswordCode onDone={signedIn} username={identity.me.username} passkeysAvailable={identity.factors.passkeys_available && identity.factors.passkeys.length > 0} passwordAvailable={identity.factors.password_totp}><SignOut /></PasswordCode></div>
  </Sheet>, document.body);
}
