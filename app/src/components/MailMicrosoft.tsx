"use client";
// A Microsoft inbox's sign-in (fiscalm.mail.microsoft): the engine asks Microsoft for a short code, the person types it on
// Microsoft's own page in another tab and accepts read-only access, and this screen asks the engine until the answer
// comes or the code expires. The engine decides every outcome; the screen names it and offers the next step.
import { Button, buttonStyle, buttonTone, noteLeftStyle } from "./Controls";
import { useEffect, useRef, useState } from "react";
import { post } from "@/lib/api";
import type { ActionResult, FirmScope, MicrosoftCode, MicrosoftSignIn } from "@/lib/types";
import { useCompany } from "./context";
import { I, Spinner } from "./ui";

/** Microsoft's device sign-in page: fixed here, never a link taken from an answer. */
export const DEVICE_LOGIN = "https://microsoft.com/devicelogin";
const USER_CODE = /^[A-Z0-9-]{4,16}$/i;
const resultStyle = "mt-4 border-0 border-l-[3px] border-solid pl-3 text-sm leading-snug [&>b]:block";

type Failure = Exclude<MicrosoftCode, "authorization_pending" | "slow_down" | "connected">;
/** A sign-in that did not finish, in words: what happened and the next step. `a`: the inbox's address. */
const FAILED: Partial<Record<Failure, (a: string) => [string, string]>> = {
  authorization_declined: () => ["Accesul a fost refuzat în contul Microsoft.", "Încearcă din nou și acceptă accesul pe pagina Microsoft."],
  expired_token: () => ["Codul a expirat.", "Încearcă din nou pentru un cod nou."],
  admin_consent_required: () => ["Organizația Microsoft 365 a firmei cere aprobarea unui administrator.",
    "Cere-i administratorului Microsoft 365 al firmei să aprobe aplicația fiscalm pentru organizație. Apoi încearcă din nou."],
  tenant_policy_blocked: () => ["Regulile organizației Microsoft 365 a firmei nu permit conectarea.",
    "Cere-i administratorului Microsoft 365 al firmei să permită accesul fiscalm. Apoi încearcă din nou."],
  not_configured: () => ["Contul Microsoft nu e configurat pe acest server.", "Cere-i administratorului serverului să-l configureze."],
  account_mismatch: (a) => ["Ai intrat cu alt cont Microsoft.", `Încearcă din nou și intră cu ${a}.`],
  reconnect_required: () => ["Conexiunea cu Microsoft s-a încheiat.", "Reconectează contul."],
};
const failed = (code: string, address: string): [string, string] =>
  FAILED[code as Failure]?.(address) ?? ["Conectarea la Microsoft nu a reușit.", "Încearcă din nou peste câteva minute."];

type Phase = { k: "idle" } | { k: "starting" } | { k: "waiting"; id: string; code: string; until: number; interval: number }
  | { k: "connected"; id: string } | { k: "failed"; code: string };
type Waiting = Extract<Phase, { k: "waiting" }>;
type Answer = ActionResult & { done?: MicrosoftSignIn | null };

/** A started sign-in's code, when the engine's answer carries a usable one; it works for `expires_in` seconds from now. */
const fresh = (r: MicrosoftSignIn | null | undefined): Waiting | null => r?.id && r.user_code && USER_CODE.test(r.user_code) && r.expires_in
  ? { k: "waiting", id: r.id, code: r.user_code, until: Date.now() + r.expires_in * 1000, interval: 5 } : null;
/** When to ask next: after the interval, or at the expiry if that comes first. */
const nextAsk = (w: Waiting) => Math.min(w.interval * 1000, Math.max(0, w.until - Date.now()));
const expired = (w: Waiting) => Date.now() >= w.until;

/** The sign-in for `address`. `id`: the saved inbox it reconnects (`again`); `urgent` makes it the step to take.
 * `onConnected` gets the inbox's id once Microsoft accepted, once per sign-in. */
/** `scope`: a new inbox's firms, stored when the engine creates it, so it never reads for firms it wasn't given. The
 * first start creates the inbox: a retry signs in that one by its id, without a scope (the engine refuses one there).
 * `onCreated`: the inbox exists, with that scope. */
export function MicrosoftConnect({ address, id, again, urgent, disabled, scope, onCreated, onConnected }: {
  address: string; id?: string; again: boolean; urgent: boolean; disabled: boolean; scope: { firm_scope?: FirmScope };
  onCreated: () => void; onConnected: (id: string) => void;
}) {
  const co = useCompany().co;
  const made = useRef(id);
  const [phase, setPhase] = useState<Phase>({ k: "idle" });
  // Each start begins a new round; an answer from an older round (or after the sheet closed) changes nothing.
  const round = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => { round.current++; clearTimeout(timer.current); }, []);
  // While a code waits: ask again after the engine's interval, and stop at the code's expiry.
  const wait = (mine: number, w: Waiting) => {
    setPhase(w);
    timer.current = setTimeout(() => void ask(mine, w), nextAsk(w));
  };
  const ask = async (mine: number, w: Waiting) => {
    if (mine !== round.current) return;
    if (expired(w)) return settle(mine, { code: "expired_token" }, null);
    try {
      const r = await post("mail-microsoft-poll", { co, id: w.id }) as Answer;
      if (r.cancelled) return void (mine === round.current && setPhase({ k: "idle" })); // the step-up was cancelled: start again
      settle(mine, r.done, w);
    } catch {
      settle(mine, null, null);
    }
  };
  const settle = (mine: number, r: MicrosoftSignIn | null | undefined, polled: Waiting | null) => {
    if (mine !== round.current) return;
    const code = r?.code ?? "connection_failed";
    if (code === "connected" && r?.id) { setPhase({ k: "connected", id: r.id }); onConnected(r.id); return; }
    if (code === "authorization_pending" || code === "slow_down") {
      const w = polled ?? fresh(r);
      if (w) return wait(mine, { ...w, interval: Math.max(5, r?.interval ?? w.interval) });
    }
    setPhase({ k: "failed", code: code === "connected" || code === "authorization_pending" || code === "slow_down" ? "connection_failed" : code });
  };
  const start = async () => {
    clearTimeout(timer.current);
    const mine = ++round.current;
    setPhase({ k: "starting" });
    try {
      const r = await post("mail-microsoft-start", { co, address, ...(made.current ? { id: made.current } : scope) }) as Answer;
      if (r.cancelled) return setPhase({ k: "idle" }); // the step-up was cancelled: nothing started
      if (!made.current && r.done?.id) { made.current = r.done.id; onCreated(); }
      settle(mine, r.done, null);
    } catch {
      settle(mine, null, null);
    }
  };

  if (phase.k === "connected") return <div className={`${resultStyle} border-ok`} role="status">
    <b className="text-ok">Conectată.</b><p className="mt-1 text-muted">fiscalm citește {address} doar pentru documente.</p></div>;
  if (phase.k === "waiting") return <Code address={address} code={phase.code} until={phase.until} />;
  const [title, next] = phase.k === "failed" ? failed(phase.code, address) : ["", ""];
  return <>
    {title ? <div className={`${resultStyle} border-danger`} role="alert"><b className="text-danger">{title}</b>
      <p className="mt-1 text-muted">{next}</p></div> : null}
    <div className="mt-4 flex flex-wrap gap-2">
      <Button type="button" variant={urgent || title ? "primary" : "secondary"} disabled={disabled || phase.k === "starting"}
        aria-busy={phase.k === "starting"} onClick={() => void start()}>
        {phase.k === "starting" ? <><Spinner />Se cere codul…</> : title ? "Încearcă din nou" : again ? "Reconectează contul Microsoft" : "Conectează contul Microsoft"}
      </Button>
    </div>
    {!title ? <p className={noteLeftStyle}>fiscalm primește doar acces de citire.</p> : null}
  </>;
}

/** The code to type on Microsoft's page, large and copyable, and until when it works. */
function Code({ address, code, until }: { address: string; code: string; until: number }) {
  const [copied, setCopied] = useState<"" | "yes" | "no">("");
  const box = useRef<HTMLElement>(null);
  useEffect(() => box.current?.focus(), []); // the button that started it is gone: focus lands on the code
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("no clipboard"); // plain http: not a secure context
      await navigator.clipboard.writeText(code);
      setCopied("yes");
    } catch {
      const range = document.createRange();
      if (box.current) range.selectNodeContents(box.current);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
      setCopied("no");
    }
  };
  const at = new Date(until).toLocaleTimeString("ro-RO", { hour: "2-digit", minute: "2-digit" });
  return <section className="mt-4 flex flex-col gap-3 rounded-control bg-inset p-4" aria-label="Codul pentru Microsoft">
    <p>Deschide pagina Microsoft, scrie codul și intră cu {address}.</p>
    <div className="flex flex-wrap items-center gap-3">
      <code ref={box} tabIndex={0} className="select-all font-mono text-3xl font-bold tracking-[0.12em] text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">{code}</code>
      <Button type="button" variant="secondary" onClick={() => void copy()}><I n="copy" />{copied === "yes" ? "Copiat" : "Copiază codul"}</Button>
    </div>
    {copied === "no" ? <p className={noteLeftStyle} role="alert">Codul nu s-a putut copia automat. Este selectat. Copiază-l manual.</p> : null}
    <a className={`btn self-start ${buttonStyle} ${buttonTone.primary}`} href={DEVICE_LOGIN} target="_blank" rel="noopener noreferrer">
      Deschide pagina Microsoft<I n="arrow-up-right" /></a>
    <p className="flex items-center gap-2 text-muted" role="status"><Spinner />Se așteaptă conectarea. Codul este valabil până la {at}.</p>
  </section>;
}
