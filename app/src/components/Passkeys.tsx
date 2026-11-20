"use client";
import { Button, Input, Label, Form, Actions, Panel, noteLeftStyle, rowStyle, rowTextStyle, sectionHeadingStyle } from "./Controls";
import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { AuthError, CANCEL_REAUTHENTICATION, NO_ANSWER, postAuth } from "@/lib/api";
import { assertPasskey, createPasskey, suggestedPasskeyName, passkeysSupported, type AssertionOptions, type CreationOptions, type PasskeyChallenge } from "@/lib/passkeys";
import { dmt } from "@/lib/format";
import type { AccountPasskey } from "@/lib/types";
import { Empty, I } from "./ui";
import { useRequest } from "./AuthRequest";
import { useConfirm } from "./Confirm";

const subscribeDevice = () => () => {};
const serverName = () => "";
const deviceName = () => suggestedPasskeyName(navigator);

export function usePasskeyName(savedName?: string) {
  const suggestion = useSyncExternalStore(subscribeDevice, deviceName, serverName);
  // null means untouched; an intentionally empty string must stay empty.
  const [edited, setName] = useState<string | null>(savedName ?? null);
  return [edited ?? suggestion, setName] as const;
}

// Only suppress focus rechecks caused by the native prompt. Account changes still invalidate the flow.
export const PASSKEY_FLOW = "fiscalm:passkey-flow";
export type PasskeyFlow = { signal: AbortSignal; active: boolean };

export function usePasskeyRequest() {
  const active = useRef<AbortController | null>(null);
  const request = useRequest(NO_ANSWER);
  useEffect(() => {
    const cancel = () => {
      const controller = active.current;
      if (controller) {
        controller.abort();
        window.dispatchEvent(new CustomEvent<PasskeyFlow>(PASSKEY_FLOW, { detail: { signal: controller.signal, active: false } }));
      }
    };
    window.addEventListener(CANCEL_REAUTHENTICATION, cancel);
    return () => { window.removeEventListener(CANCEL_REAUTHENTICATION, cancel); cancel(); };
  }, []);
  const run = async (operation: (signal: AbortSignal) => Promise<void>) => {
    if (active.current || request.busy) return;
    const controller = new AbortController(); active.current = controller;
    window.dispatchEvent(new CustomEvent<PasskeyFlow>(PASSKEY_FLOW, { detail: { signal: controller.signal, active: true } }));
    try { await request.run(() => operation(controller.signal)); }
    finally {
      window.dispatchEvent(new CustomEvent<PasskeyFlow>(PASSKEY_FLOW, { detail: { signal: controller.signal, active: false } }));
      if (active.current === controller) active.current = null;
    }
  };
  return { ...request, run };
}

/** Native refusals happen before any finish POST; a failed finish keeps the uncertainty warning. */
export async function nativeProof<T>(get: () => Promise<T>): Promise<T> {
  try { return await get(); }
  catch (error) {
    if (error instanceof DOMException && ["NotAllowedError", "AbortError"].includes(error.name)) {
      throw new AuthError(0, "Cererea pentru cheia de acces a fost anulată sau nu a fost confirmată pe dispozitiv.", 0);
    }
    throw new AuthError(0, "Cheia de acces nu a putut fi folosită în acest browser. Încearcă din nou sau alege altă metodă de conectare.", 0);
  }
}

export function PasskeySignIn({ stepUp, onDone }: { stepUp: boolean; onDone: () => Promise<void> }) {
  const request = usePasskeyRequest();
  const submit = () => request.run(async signal => {
    const prefix = stepUp ? "step-up/passkey" : "passkey";
    const options = await postAuth<PasskeyChallenge<AssertionOptions>>(`${prefix}/options`, {}, signal);
    signal.throwIfAborted();
    const proof = await nativeProof(() => assertPasskey(options.publicKey, signal));
    await postAuth(`${prefix}/finish`, { ticket: options.ticket, ...proof }, signal);
    signal.throwIfAborted(); await onDone();
  });
  return <div aria-busy={request.busy}>
    {request.feedback}<Actions><Button disabled={request.busy} onClick={submit}><I n="fingerprint" /><span>{stepUp ? "Confirmă cu o cheie de acces" : "Intră în cont cu o cheie de acces"}</span></Button></Actions>
  </div>;
}

export function OwnPasskeys({ keys, passwordAvailable, available, onChanged }: {
  keys: AccountPasskey[]; passwordAvailable: boolean; available: boolean; onChanged: () => Promise<void>;
}) {
  const [editing, setEditing] = useState<AccountPasskey | "new" | null>(null), [unconfirmed, setUnconfirmed] = useState(false), [message, setMessage] = useState("");
  const request = usePasskeyRequest(), live = useRef(true), { ask, dialog } = useConfirm();
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const change = (operation: (signal: AbortSignal, writing: () => void) => Promise<void>, success: string) => request.run(async signal => {
    let writing = false; setMessage("");
    try { await operation(signal, () => { writing = true; }); signal.throwIfAborted(); }
    catch (error) {
      if (!live.current) throw error;
      if (writing) { if (signal.aborted || !(error instanceof AuthError) || error.status < 400) setUnconfirmed(true); await onChanged(); }
      if (signal.aborted) throw error;
      if (error instanceof AuthError && error.message === "Conectarea nu a reușit.") { await onChanged(); throw new AuthError(error.status, "Cheia de acces nu a putut fi modificată.", error.retryAfter); }
      throw error;
    }
    setEditing(null); setMessage(success); await onChanged();
  });
  const save = (name: string) => change(async (signal, writing) => {
    if (editing === "new") {
      const options = await postAuth<PasskeyChallenge<CreationOptions>>("me/passkey/options", { name }, signal); signal.throwIfAborted();
      const proof = await nativeProof(() => createPasskey(options.publicKey, signal)); writing();
      await postAuth("me/passkey/finish", { ticket: options.ticket, ...proof }, signal);
    } else if (editing) { writing(); await postAuth("me/passkey/rename", { credential_id: editing.id, name }, signal); }
  }, editing === "new" ? "Cheia de acces a fost adăugată." : "Cheia de acces a fost redenumită.");
  const remove = async (key: AccountPasskey) => {
    if (request.busy || !(await ask({ text: `Elimini cheia de acces „${key.name}”? Nu mai poate fi folosită pentru conectare.`, yes: "Elimină cheia", danger: true }))) return;
    void change(async (signal, writing) => { writing(); await postAuth("me/passkey/remove", { credential_id: key.id }, signal); }, "Cheia de acces a fost eliminată.");
  };
  return <section aria-label="Chei de acces (passkey)"><div className={sectionHeadingStyle}><h2>Chei de acces (passkey)</h2></div>
    {unconfirmed && <p role="alert">Modificarea anterioară poate fi deja salvată. Verifică lista înainte să încerci din nou.</p>}{request.feedback}{message && <p role="status">{message}</p>}
    {!available && <p className={noteLeftStyle}>Pentru cheile de acces, folosește o adresă HTTPS cu nume de domeniu.</p>}
    <Panel padded={false} className="divide-x-0 divide-y divide-solid divide-line">{!keys.length && <Empty />}{keys.map(key => <div className={`${rowStyle} flex-wrap sm:flex-nowrap`} key={key.id}><div className={`${rowTextStyle} basis-full sm:basis-0 [&_small]:text-muted`}><b>{key.name}</b>
      <small>Creată: {dmt(key.created_at, "")}. {key.last_used_at ? `Ultima utilizare: ${dmt(key.last_used_at, "")}.` : "Nu a fost folosită pentru conectare."}</small>
      <small>{key.backed_up ? "Cheie sincronizată." : key.backup_eligible ? "Cheia permite sincronizarea." : "Cheie păstrată pe dispozitiv."}</small></div>
      <div className="flex flex-wrap gap-2"><Button variant="secondary" disabled={!available || request.busy || !!editing} onClick={() => { request.clear(); setEditing(key); }}>Redenumește</Button>
        <Button variant="secondary" disabled={!available || request.busy || !!editing || (!passwordAvailable && keys.length === 1)} onClick={() => remove(key)}>Elimină cheia</Button></div>
    </div>)}</Panel>
    {!passwordAvailable && keys.length === 1 && <p className={noteLeftStyle}>Adaugă altă cheie înainte să o elimini pe ultima.</p>}
    {editing ? <PasskeyEditor key={editing === "new" ? "new" : editing.id} item={editing} busy={request.busy} save={save} cancel={() => { request.clear(); setEditing(null); }} />
      : available && passkeysSupported() ? <Actions><Button variant="secondary" disabled={request.busy} onClick={() => { request.clear(); setEditing("new"); }}><I n="fingerprint" /><span>Adaugă o cheie de acces</span></Button></Actions>
      : available && <p className={noteLeftStyle}>Pentru a adăuga o cheie de acces, folosește un browser compatibil.</p>}
    {dialog}
  </section>;
}

function PasskeyEditor({ item, busy, save, cancel }: { item: AccountPasskey | "new"; busy: boolean; save: (name: string) => Promise<void>; cancel: () => void }) {
  const [name, setName] = usePasskeyName(item === "new" ? undefined : item.name), id = useId(); const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  return <Form aria-busy={busy} onSubmit={e => { e.preventDefault(); void save(name); }}>
    <Label htmlFor={id}>Numele cheii de acces</Label><Input ref={input} id={id} value={name} onChange={e => setName(e.target.value)} maxLength={80} autoComplete="off" required />
    <Actions><Button disabled={busy}>{item === "new" && <I n="fingerprint" />}<span>{item === "new" ? "Configurează cheia de acces" : "Salvează numele"}</span></Button>
      <Button variant="secondary" type="button" disabled={busy} onClick={cancel}>Renunță</Button></Actions>
  </Form>;
}
