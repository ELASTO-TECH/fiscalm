"use client";

import { Anchor, Button, Input, Label, Actions, surfaceStyle, noteLeftStyle } from "./Controls";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { cliLogin, type CliLogin } from "@/lib/api";

export const AI_CHOICES: Record<string, string> = {
  anthropic: "Anthropic (cheie API)", openai: "Compatibil OpenAI sau local",
  "claude-cli": "Folosește Claude Code de pe acest calculator", "codex-cli": "Folosește Codex de pe acest calculator",
};

export function SubscriptionNote() {
  return <p className={noteLeftStyle}>Pentru un server în cloud sau o instalare pentru alte firme, folosește o cheie API.</p>;
}

const LOGIN_STATE: Record<CliLogin["state"], string> = {
  idle: "", waiting: "Autentificarea este în curs.", connected: "Autentificarea a fost confirmată.",
  failed: "Autentificarea nu a reușit. Încearcă din nou.", expired: "Autentificarea a expirat. Încearcă din nou.",
  cancelled: "Autentificarea a fost anulată.", invalid_url: "Adresa primită de la aplicația de autentificare nu este permisă.",
};

/** The official CLI's own login, in two steps. `children`: the form's button, shown when the login doesn't add the
 * integration itself: adding a new one, a confirmed login calls `onConnected` instead. */
export function AiCli({ provider, adding = false, onChange, onConnected, children }: {
  provider: string; adding?: boolean; onChange?: () => void; onConnected?: () => void; children?: ReactNode;
}) {
  const [session, setSession] = useState<CliLogin | null>(null);
  const [copied, setCopied] = useState<"" | "done" | "failed">("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const inflight = useRef(false);
  const generation = useRef(0);
  const notified = useRef(true); // only a login in progress since this opened reports its confirmation
  const name = provider === "claude-cli" ? "Claude Code" : "Codex";
  const refresh = useCallback(async () => {
    if (inflight.current) return;
    const current = ++generation.current;
    try {
      const next = await cliLogin(provider);
      if (current === generation.current) { setSession((old) => ({ ...old, ...next })); setFailed(false); }
    } catch { if (current === generation.current) setFailed(true); }
  }, [provider]);
  useEffect(() => {
    let live = true;
    cliLogin(provider).then((next) => {
      if (live) { setSession(next); setFailed(false); notified.current = next.state !== "waiting"; }
    }, () => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [provider]);
  const waiting = session?.state === "waiting";
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => window.clearInterval(timer);
  }, [waiting, refresh]);
  useEffect(() => {
    if (session?.state === "connected" && !notified.current) {
      notified.current = true; (onConnected ?? onChange)?.();
      cliLogin(provider).then((next) => setSession(next), () => setFailed(true));
    }
  }, [session?.state, onChange, onConnected, provider]);

  const act = async (op: "start" | "code" | "cancel" | "logout") => {
    if (inflight.current) return;
    inflight.current = true;
    generation.current += 1;
    if (op === "start") { notified.current = false; setCopied(""); }
    setBusy(true);
    try {
      const next = await cliLogin(provider, op, op === "code" ? code : "");
      setSession((old) => ({ ...old, ...next }));
      setCode("");
      setFailed(false);
      if (op === "logout") { onChange?.(); setSession(await cliLogin(provider)); }
    } catch { setFailed(true); }
    finally { inflight.current = false; setBusy(false); }
  };
  const page = provider === "claude-cli" ? "Claude" : "OpenAI";
  const copyLink = async () => {
    try {
      if (!navigator.clipboard) throw new Error("no clipboard"); // plain http: not a secure context
      await navigator.clipboard.writeText(session?.url ?? ""); setCopied("done");
    } catch { setCopied("failed"); }
  };
  const flow = session?.provider === provider && waiting;
  // A new integration is added by the login itself (onConnected); the form's button stays for a CLI already logged in
  // or not installed, and for a saved integration.
  const finish = !adding || session?.readiness === "ready" || session?.readiness === "missing";
  return <>
    <section className={`${surfaceStyle} my-4 space-y-4 p-5`} aria-label={`Autentificare ${name}`}>
      {flow ? <ol className="m-0 list-none space-y-5 p-0">
        <li>
          <p><b>1. Autentifică-te pe pagina {page}.</b></p>
          {session.url ? <Actions className="btns !mt-3">
            <Anchor variant="primary" href={session.url} target="_blank" rel="noopener noreferrer">Deschide pagina {page}</Anchor>
            <Button type="button" variant="secondary" onClick={() => void copyLink()}>{copied === "done" ? "Link copiat" : "Copiază linkul"}</Button>
          </Actions> : <p role="status">Se așteaptă linkul de la {name}.</p>}
          {copied === "failed" && session.url ? <Input className="mt-3" readOnly value={session.url} aria-label={`Linkul paginii ${page}`}
            onFocus={(e) => e.currentTarget.select()} /> : null}
        </li>
        {session.code && !session.needs_code ? <li><p><b>2. Introdu pe pagina {page} codul <code>{session.code}</code>.</b></p></li> : null}
        {session.needs_code ? <li>
          <Label htmlFor="ai-login-code" className="!font-semibold">2. Copiază codul afișat de {page} și lipește-l aici.</Label>
          <Input id="ai-login-code" type="password" autoComplete="off" value={code} onChange={(e) => setCode(e.target.value)} maxLength={2048}
            onKeyDown={(e) => {
              if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
              e.preventDefault();
              if (!busy && code.trim()) void act("code");
            }} />
        </li> : null}
      </ol> : session?.readiness === "missing" ? <p>{name} nu este instalat. Rulează <code>{session.install_command}</code>, apoi reîncearcă.</p>
        : session?.readiness === "ready" && session.state !== "connected" ? <p>{name} este autentificat.</p> : null}
      <p role="status" className="empty:hidden">{session && !flow ? LOGIN_STATE[session.state] : ""}</p>
      {flow ? <Actions>
        {session.needs_code ? <Button type="button" disabled={busy || !code.trim()} onClick={() => void act("code")}>Confirmă</Button> : null}
        <Button type="button" variant="secondary" disabled={busy} onClick={() => void act("cancel")}>Anulează</Button>
      </Actions> : <Actions className="btns !mt-0">
        {session?.readiness !== "ready" ? <Button type="button" disabled={busy || !session || session.readiness === "missing"} onClick={() => void act("start")}>Conectează {name}</Button> : null}
        {session?.readiness === "ready" ? <Button type="button" variant="secondary" disabled={busy} onClick={() => void act("logout")}>Deconectează {name}</Button> : null}
        {session?.readiness === "missing" ? <Button type="button" variant="secondary" onClick={() => void refresh()}>Verifică din nou</Button> : null}
      </Actions>}
      {failed ? <p role="alert">Starea autentificării nu a putut fi verificată. Verificarea se reia.</p> : null}
      <SubscriptionNote />
    </section>
    {finish ? children : null}
  </>;
}
