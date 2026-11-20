"use client";
import { Actions, Button, Form, Input, Label, Panel, Select, sectionHeadingStyle, surfaceStyle, rowIconStyle, rowStyle, rowTextStyle, backLinkStyle, bannerStyle, noteLeftStyle, settingsPageStyle, legendStyle } from "./Controls";
// Setări › Linkuri și chei API (sec=acces): who else can reach this install. Bondrop's receipt links (per company: send photos and PDFs, see nothing) and
// API keys (install-wide, for API v1 and MCP clients). A secret is shown once, right after it is made, and never again.
import { useContext, useEffect, useRef, useState, type FormEvent } from "react";
import { useLoaded } from "./Paging";
import { getTokens, getUploadLinks, NO_ANSWER, postAccess, type AccessResult } from "@/lib/api";
import { dm, nOf, roDay } from "@/lib/format";
import { coName, type ApiToken, type UploadLink } from "@/lib/types";
import { IdentityContext, useApp, useCan, useCompany } from "./context";
import { useConfirm, type Ask } from "./Confirm";
import { Empty, I, L, Loading, ROW_TINT, TONE_TEXT, Title, ICON_TINT } from "./ui";

const DAYS: [string, string][] = [["7", "7 zile"], ["30", "30 de zile"], ["90", "90 de zile"], ["365", "un an"]];

/** Receipt links follow the current company (another one starts unknown) and its changed state. */
function useLinks(co: string) {
  const { view } = useApp();
  const { data, failed, reload } = useLoaded(() => getUploadLinks(co), co, view);
  return { links: data ?? null, failed, reload };
}

export function Acces() {
  const receipts = useCan("member");
  return <div className={settingsPageStyle}>
    <L to={{ tab: "setari" }} className={backLinkStyle} label="Înapoi la Setări"><I n="arrow-left" />Setări</L><Title>Linkuri și chei API</Title>
    {receipts && <ReceiptLinks />}<Tokens />
  </div>;
}

function ReceiptLinks() {
  const s = useCompany(); const { links, failed, reload } = useLinks(s.co);
  return <>
    {failed && <div className={bannerStyle} role="alert"><b>Linkurile Bondrop nu au putut fi actualizate.</b><Button variant="secondary" onClick={reload}>Reîncearcă</Button></div>}
    <Links links={links} failed={failed} company={coName(s)} reload={reload} />
  </>;
}

/** A request for this company, staying on the page; the answer carries what was made. */
function useSend(reload: () => void) {
  const { run } = useApp();
  const s = useCompany();
  return async (action: "upload-link-create" | "upload-link-revoke", body: Record<string, unknown>, confirm?: Ask) => {
    const r = await run<AccessResult>(s.co, () => postAccess(action, { ...body, co: s.co }), confirm, () => false);
    reload();
    return r;
  };
}

function Links({ links, failed, company, reload }: { links: UploadLink[] | null; failed: boolean; company: string; reload: () => void }) {
  const { busy } = useApp();
  const send = useSend(reload);
  const opener = useRef<HTMLButtonElement>(null);
  const [adding, setAdding] = useState(false);
  const [made, setMade] = useState("");
  // Focus goes back to the section's "new" button after cancelling, after "Gata" and after a revoke.
  const back = () => setTimeout(() => opener.current?.focus());
  const [label, setLabel] = useState("");
  const [days, setDays] = useState("30");
  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !label.trim()) return;
    const r = await send("upload-link-create", { label: label.trim(), expires_in_days: Number(days) });
    if (r?.ok && r.done?.path) {
      setAdding(false);
      // The engine's public address for receipt links when the install has one; else the address the app is open on
      setMade(r.done.url ?? `${window.location.origin}${r.done.path}`);
      setLabel("");
    }
  };
  const now = new Date().toISOString();
  const sorted = [...(links ?? [])].sort((a, b) => state(a, now).order - state(b, now).order || b.created_at.localeCompare(a.created_at));
  return (
    <section aria-labelledby="h-links">
      <div className={sectionHeadingStyle}>
        <h2 id="h-links">Bondrop</h2>
        {!adding && !made ? <Button ref={opener} type="button" variant="secondary" onClick={() => setAdding(true)}>Link nou</Button> : null}
      </div>
      <p className={noteLeftStyle}>Cine are linkul poate trimite documente pentru {company}, fără să vadă contabilitatea.</p>
      {adding ? (
        <Form className={`${surfaceStyle} mb-3 p-5 sm:p-6`} onSubmit={create} aria-label="Link nou">
          <Label htmlFor="link-label">Nume</Label>
          <Input id="link-label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={100} autoComplete="off" autoFocus
            placeholder="De exemplu: telefonul de serviciu" aria-describedby="link-label-hint" />
          <p id="link-label-hint" className={noteLeftStyle}>Numele apare pe pagina Bondrop.</p>
          <Label htmlFor="link-days">Valabil</Label>
          <Select id="link-days" value={days} onChange={(e) => setDays(e.target.value)}>
            {DAYS.map(([d, text]) => <option key={d} value={d}>{text}</option>)}
          </Select>
          <Actions>
            <Button type="submit" disabled={busy || !label.trim()}>Creează linkul</Button>
            <Button type="button" variant="secondary" disabled={busy} onClick={() => {
              setAdding(false);
              back();
            }}>Renunță</Button>
          </Actions>
        </Form>
      ) : null}
      {made ? <Secret value={made} what="linkul" onDone={() => {
        setMade("");
        back();
      }} /> : null}
      <Panel padded={false}>
        {links === null ? (failed ? null : <Loading rows={1} />) : sorted.length ? sorted.map((l) => {
          const st = state(l, now);
          return (
            <div key={l.id} className={`${rowStyle} flex-wrap sm:flex-nowrap`}>
              <span className={`${rowIconStyle} ${ROW_TINT[st.tone] ?? ICON_TINT}`}><I n="link-simple" /></span>
              <span className={rowTextStyle}>
                <b>{l.label}</b>
                <small className={TONE_TEXT[st.tone]}>{st.text}</small>
              </span>
              {st.order === 0 ? (
                <Button type="button" variant="secondary" disabled={busy} aria-label={`Revocă linkul ${l.label}`}
                  onClick={async () => {
                    await send("upload-link-revoke", { id: l.id }, { text: `Revoci linkul „${l.label}”? Nu se mai pot trimite bonuri prin el.`, yes: "Revocă linkul", danger: true });
                    back();
                  }}>
                  Revocă
                </Button>
              ) : null}
            </div>
          );
        }) : <Empty />}
      </Panel>
    </section>
  );
}

/** Usable links first, then those that can't take more (expired or used up), then revoked. `now` is an ISO instant. */
function state(l: UploadLink, now: string): { order: number; tone: "" | "late" | "ok"; text: string } {
  const today = roDay(now);
  const got = l.uploads ? `, ${nOf(l.uploads, "un fișier primit", "fișiere primite")}` : "";
  if (l.revoked_at) return { order: 2, tone: "", text: `Revocat pe ${dm(roDay(l.revoked_at), today)}${got}` };
  if (new Date(l.expires_at).getTime() <= new Date(now).getTime()) {
    return { order: 1, tone: "", text: `Expirat pe ${dm(roDay(l.expires_at), today)}${got}` };
  }
  if (l.max_files !== null && l.uploads >= l.max_files) {
    return { order: 1, tone: "", text: `Limita de fișiere atinsă, ${nOf(l.uploads, "un fișier primit", "fișiere primite")}` };
  }
  const left = l.max_files !== null ? `, mai primește ${nOf(l.max_files - l.uploads, "un fișier", "fișiere")}` : "";
  return { order: 0, tone: "ok", text: `Activ până pe ${dm(roDay(l.expires_at), today)}${left}` };
}

export function Tokens() {
  return useCan("own_tokens") ? <TokenList /> : null;
}

function useTokens() {
  const session = useContext(IdentityContext)!, owner = session.identity.me.id;
  const administrator = useCan("manage_tokens");
  const { data, failed, reload } = useLoaded(async () => ({ rows: await getTokens(), today: roDay(new Date().toISOString()) }), `${owner}|${administrator}`);
  const tokens = data?.rows ?? null, today = data?.today ?? "";
  const [error, setError] = useState(""), [busy, setBusy] = useState(false); const sending = useRef(false);
  const { ask, dialog } = useConfirm();
  const send = async (action: "token-create" | "token-revoke", body: Record<string, string>, confirm?: Ask) => {
    if (sending.current) return null;
    sending.current = true;
    if (confirm && !(await ask(confirm))) { sending.current = false; return null; }
    setBusy(true); setError("");
    try {
      const result = await postAccess(action, body);
      if (result.cancelled) return null;
      if (!result.ok) { setError(result.message || "Modificarea nu a fost salvată."); await session.refreshIdentity().catch(() => {}); }
      reload(); return result;
    } catch { setError(NO_ANSWER); reload(); return null; }
    finally { sending.current = false; setBusy(false); }
  };
  return { tokens: tokens?.filter(t => administrator || t.user_id === owner) ?? null, today, failed, error, busy, reload, send, dialog, clearError: () => setError("") };
}

function TokenList() {
  const { tokens, today, failed, error, busy, reload, send, dialog, clearError } = useTokens();
  const canWrite = useCan("member");
  const opener = useRef<HTMLButtonElement>(null);
  const [adding, setAdding] = useState(false);
  const [made, setMade] = useState("");
  // Focus goes back to the section's "new" button after cancelling, after "Gata" and after a revoke.
  const back = () => setTimeout(() => opener.current?.focus());
  const create = async (name: string, scope: "read" | "write") => {
    const r = await send("token-create", { name, scope });
    if (r?.ok && r.done?.token) { setAdding(false); setMade(r.done.token); }
  };

  return (
    <section aria-labelledby="h-tokens">
      <div className={sectionHeadingStyle}>
        <h2 id="h-tokens">Chei API</h2>
        {!adding && !made ? <Button ref={opener} type="button" variant="secondary" onClick={() => { clearError(); setAdding(true); }}>Cheie nouă</Button> : null}
      </div>
      <p className={noteLeftStyle}>O cheie are cel mult drepturile contului care o creează, la toate firmele.</p>
      {failed && <div className={bannerStyle} role="alert"><p>Cheile API nu au putut fi actualizate.</p><Button variant="secondary" onClick={reload}>Reîncearcă</Button></div>}
      {error && <p className={`${noteLeftStyle} text-danger`} role="alert">{error}</p>}
      {adding && <TokenForm busy={busy} canWrite={canWrite} create={create} cancel={() => { setAdding(false); clearError(); back(); }} />}
      {made ? <Secret value={made} what="cheia" onDone={() => {
        setMade("");
        back();
      }} /> : null}
      <TokenRows tokens={tokens} failed={failed} today={today} busy={busy} revoke={async (id, name) => {
        await send("token-revoke", { id }, { text: `Revoci cheia „${name}”? Programele care o folosesc pierd accesul.`, yes: "Revocă cheia", danger: true }); back();
      }} />
      {dialog}
    </section>
  );
}

function TokenRows({ tokens, failed, today, busy, revoke }: { tokens: ApiToken[] | null; failed: boolean; today: string; busy: boolean; revoke: (id: string, name: string) => Promise<void> }) {
  const owner = useContext(IdentityContext)!.identity.me.id, canWrite = useCan("member");
  const sorted = [...(tokens ?? [])].sort((a, b) => Number(!!a.revoked_at) - Number(!!b.revoked_at) || b.created_at.localeCompare(a.created_at));
  return <Panel padded={false}>
        {tokens === null ? (failed ? null : <Loading rows={1} />) : sorted.length ? sorted.map((t) => (
          <div key={t.id} className={`${rowStyle} flex-wrap sm:flex-nowrap`}>
            <span className={`${rowIconStyle} ${ICON_TINT}`}><I n="plugs" /></span>
            <span className={rowTextStyle}>
              <b>{t.name}</b>
              <small>{t.revoked_at ? `Revocată pe ${dm(roDay(t.revoked_at), today)}`
                : `${t.scope === "write" && (canWrite || t.user_id !== owner) ? "Citire și acțiuni" : "Doar citire"}, ${t.last_used_at ? `folosită ultima dată pe ${dm(roDay(t.last_used_at), today)}` : "nefolosită încă"}`}
                </small><small>Proprietar: {t.owner?.display_name || "indisponibil"}.</small>
            </span>
            {!t.revoked_at ? (
              <Button type="button" variant="secondary" disabled={busy} aria-label={`Revocă cheia ${t.name}`}
                onClick={async () => {
                  await revoke(t.id, t.name);
                }}>
                Revocă
              </Button>
            ) : null}
          </div>
        )) : <Empty />}
      </Panel>;
}

function TokenForm({ busy, canWrite, create, cancel }: { busy: boolean; canWrite: boolean; create: (name: string, scope: "read" | "write") => Promise<void>; cancel: () => void }) {
  const [name, setName] = useState(""), [scope, setScope] = useState<"read" | "write">("read");
  const submit = (event: FormEvent) => { event.preventDefault(); if (!busy && name.trim()) void create(name.trim(), canWrite ? scope : "read"); };
  return <Form className={`${surfaceStyle} mb-3 p-5 sm:p-6`} onSubmit={submit} aria-label="Cheie nouă">
          <Label htmlFor="token-name">Nume</Label>
          <Input id="token-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} autoComplete="off" autoFocus
            placeholder="De exemplu: asistentul AI" />
          <fieldset className="m-0 mt-4 min-w-0 border-0 p-0 [&>label]:mt-2">
            <legend className={legendStyle}>Drepturi</legend>
            <Label><Input type="radio" name="scope" checked={!canWrite || scope === "read"} onChange={() => setScope("read")} /> Doar citire</Label>
            {canWrite && <Label><Input type="radio" name="scope" checked={scope === "write"} onChange={() => setScope("write")} /> Citire și acțiuni</Label>}
          </fieldset>
          <Actions>
            <Button type="submit" disabled={busy || !name.trim()}>Creează cheia</Button>
            <Button type="button" variant="secondary" disabled={busy} onClick={cancel}>Renunță</Button>
          </Actions>
        </Form>;
}

/** A secret, shown once: copy it (or select it when the browser won't copy), then it's gone from the page. */
export function Secret({ value, what, onDone }: { value: string; what: "linkul" | "cheia"; onDone: () => void }) {
  const [copied, setCopied] = useState<"" | "yes" | "no">("");
  const button = useRef<HTMLButtonElement>(null);
  const code = useRef<HTMLElement>(null);
  useEffect(() => button.current?.focus(), []);
  const one = what === "linkul";
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("no clipboard"); // plain http: not a secure context
      await navigator.clipboard.writeText(value);
      setCopied("yes");
    } catch {
      // Select it, so a long press or Ctrl+C copies it by hand.
      const range = document.createRange();
      if (code.current) range.selectNodeContents(code.current);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
      setCopied("no");
    }
  };
  return (
    <Panel className="secret mb-3">
      <p className={noteLeftStyle}>{one ? "Linkul" : "Cheia"} se afișează doar acum.</p>
      <code className="my-3 block font-mono select-all max-w-full break-all rounded-control bg-inset p-3 text-sm focus-visible:outline-2 focus-visible:outline-focus" ref={code} tabIndex={0}>{value}</code>
      {copied === "no" ? (
        <p className={noteLeftStyle} role="alert">{one ? "Linkul nu s-a putut copia automat. Este selectat. Copiază-l manual."
          : "Cheia nu s-a putut copia automat. Este selectată. Copiaz-o manual."}</p>
      ) : null}
      <Actions>
        <Button ref={button} type="button" onClick={copy}>{copied === "yes" ? "Copiat" : one ? "Copiază linkul" : "Copiază cheia"}</Button>
        <Button type="button" variant="secondary" onClick={onDone}>Închide</Button>
      </Actions>
    </Panel>
  );
}
