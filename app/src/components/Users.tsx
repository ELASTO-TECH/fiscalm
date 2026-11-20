"use client";
import { Actions, Button, Form, Input, Label, Panel, Select, sectionHeadingStyle, rowStyle, backLinkStyle, bannerStyle, noteLeftStyle, settingsPageStyle } from "./Controls";
import { useContext, useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { getUserSessions, getUsers, NO_ANSWER, postUser, type UserResult, type UserAction } from "@/lib/api";
import { dmt, nOf } from "@/lib/format";
import { NO_SHEET } from "@/lib/nav";
import type { AccountUser, UserRole } from "@/lib/types";
import { Secret } from "./Access";
import { useLoaded } from "./Paging";
import { useConfirm, type Ask } from "./Confirm";
import { IdentityContext, useApp, useCan } from "./context";
import { CloseX, Sheet, SheetHead } from "./Sheets";
import { ROLE_NAMES, SessionInfo } from "./UserAccount";
import { Empty, I, L, Loading, Row, Title } from "./ui";

const RESET: Ask = { yes: "Resetează autentificarea", danger: true, text: "Resetezi autentificarea contului? Sesiunile se închid, iar parola, configurarea aplicației de autentificare, cheile de acces și codurile de recuperare se șterg. Contul trebuie configurat din nou." };
type Send = (action: UserAction, body: Record<string, string>, confirm?: Ask) => Promise<(UserResult & { refreshFailed?: boolean }) | null>;

function useUsers() {
  const session = useContext(IdentityContext)!;
  const { data, failed, reload } = useLoaded(async () => ({ rows: await getUsers(), at: Date.now() }), "users");
  const rows = data?.rows ?? null, at = data?.at ?? 0;
  const [busy, setBusy] = useState(false); const sending = useRef(false);
  const { ask, cancel, dialog } = useConfirm();
  const send: Send = async (action, body, confirm) => {
    if (sending.current) return null;
    sending.current = true;
    if (confirm && !(await ask(confirm))) { sending.current = false; return null; }
    setBusy(true);
    try {
      const result = await postUser(action, body);
      if (result.cancelled) return null;
      if (!result.ok) { await session.refreshIdentity().catch(() => {}); return { ...result, message: result.message || "Modificarea nu a fost salvată." }; }
      let refreshFailed = false;
      if (action === "user-reset" && body.id === session.identity.me.id && result.done?.url) {
        window.history.pushState(null, "", result.done.url); window.dispatchEvent(new HashChangeEvent("hashchange"));
      } else {
        reload();
        try { await session.refreshIdentity(); } catch { refreshFailed = true; }
      }
      return { ...result, refreshFailed };
    } catch { reload(); return { ok: false, message: NO_ANSWER }; }
    finally { sending.current = false; setBusy(false); }
  };
  return { rows, busy, at, failed, send, dialog, cancel, retry: reload };
}

function accountStatus(user: AccountUser, today: string, at: number): string {
  if (user.disabled_at) return "Dezactivat";
  if (user.locked_until && Date.parse(user.locked_until) > at) return `Blocat până pe ${dmt(user.locked_until, today)}`;
  if (user.password_totp) return user.passkeys ? `Parolă și cod, ${nOf(user.passkeys, "o cheie de acces", "chei de acces")}` : "Parolă și cod";
  return user.passkeys ? nOf(user.passkeys, "O cheie de acces", "chei de acces") : "Configurare neîncheiată";
}

export function Users() {
  return useCan("manage_users") ? <UserManagement /> : null;
}

function UserManagement() {
  const { route, go, view } = useApp(); const data = useUsers();
  // A question belongs to the account sheet it was asked from: leaving that sheet (Back, another account) declines it.
  useEffect(() => data.cancel, [route.usr, data.cancel]);
  const close = () => go({ ...route, ...NO_SHEET });
  return <div className={settingsPageStyle}>
    <L to={{ tab: "setari" }} className={backLinkStyle}><I n="arrow-left" />Setări</L>
    <div className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2"><Title className="mb-0!">Utilizatori</Title><L to={{ usr: "new" }} className="btn">Adaugă utilizator</L></div>
    {data.failed && <div className={bannerStyle} role="alert"><p>Lista utilizatorilor nu a putut fi încărcată.</p><Button variant="secondary" onClick={data.retry}>Reîncearcă</Button></div>}
    <Panel padded={false}>{!data.rows && !data.failed && <Loading rows={1} />}{data.rows?.map(user => <Row key={user.id} icon="user" title={user.display_name} sub={`${user.username}, ${ROLE_NAMES[user.role]}. ${accountStatus(user, view.today, data.at)}`} to={{ usr: user.id }} />)}</Panel>
    {route.usr && createPortal(<UserSheet key={route.usr} user={data.rows?.find(user => user.id === route.usr)} adding={route.usr === "new"}
      loaded={data.rows !== null} busy={data.busy} send={data.send} today={view.today} at={data.at} close={close} />, document.body)}
    {data.dialog}
  </div>;
}

function UserSheet({ user, adding, loaded, busy, send, today, at, close }: {
  user?: AccountUser; adding: boolean; loaded: boolean; busy: boolean; send: Send; today: string; at: number; close: () => void;
}) {
  const owner = useContext(IdentityContext)!.identity.me.id;
  const [link, setLink] = useState(""), [error, setError] = useState("");
  const configured = !!user && (user.password_totp || user.passkeys > 0);
  const issue = async (action: UserAction, body: Record<string, string>, confirm?: Ask) => {
    setError(""); const result = await send(action, body, confirm);
    if (result) setError(!result.ok ? result.message : result.refreshFailed ? "Datele contului nu au putut fi actualizate." : "");
    if (result?.ok && result.done?.url) setLink(new URL(result.done.url, window.location.origin).href);
  };
  return <Sheet label={adding ? "Adaugă utilizator" : user?.display_name ?? "Utilizator"} busy={busy} onClose={close}>
    <CloseX onClose={close} /><SheetHead title={adding ? "Adaugă utilizator" : user?.display_name ?? "Utilizator"} />
    {error && <p className={`${noteLeftStyle} text-danger`} role="alert">{error}</p>}
    {link ? <><p className={noteLeftStyle}>Linkul funcționează o singură dată și expiră în 72 de ore. Trimite-l pe un canal de încredere.</p><Secret value={link} what="linkul" onDone={close} /></>
      : adding || user ? <><UserForm user={user} busy={busy} send={issue} />
        {user && <>
          <p className={noteLeftStyle}>{user.last_signin ? `Ultima conectare: ${dmt(user.last_signin, today)}.` : "Contul nu a fost folosit pentru conectare."}</p>
          <Actions>
            <Button variant="secondary" disabled={busy} onClick={() => issue(user.disabled_at ? "user-enable" : "user-disable", { id: user.id }, user.disabled_at ? undefined
              : { text: "Dezactivezi contul? Sesiunile lui se închid și nu se mai poate conecta.", yes: "Dezactivează contul", danger: true })}>{user.disabled_at ? "Reactivează" : "Dezactivează"}</Button>
            {!user.disabled_at && user.locked_until && Date.parse(user.locked_until) > at && <Button variant="secondary" disabled={busy} onClick={() => issue("user-unlock", { id: user.id })}>Deblochează</Button>}
            {!user.disabled_at && <Button variant="secondary" disabled={busy} onClick={() => issue(configured ? "user-reset" : "user-setup-link", { id: user.id }, configured ? RESET : undefined)}>{configured ? "Resetează autentificarea" : "Creează link de configurare"}</Button>}
          </Actions>
          {user.id === owner ? <L to={{ tab: "setari", sec: "cont" }} className="btn g">Sesiunile contului</L>
            : <UserSessions user={user} busy={busy} close={issue} today={today} />}
        </>}
      </> : loaded ? <p role="alert">Utilizatorul nu mai este disponibil.</p> : <Loading />}
  </Sheet>;
}

function UserSessions({ user, busy, close, today }: { user: AccountUser; busy: boolean; close: (action: UserAction, body: Record<string, string>, confirm?: Ask) => Promise<void>; today: string }) {
  const { data: rows, failed, reload } = useLoaded(() => getUserSessions(user.id), user.id, user.disabled_at);
  return <section aria-label="Sesiunile utilizatorului"><div className={sectionHeadingStyle}><h2>Sesiuni</h2></div>
    {failed && <div className={bannerStyle} role="alert"><p>Sesiunile nu au putut fi actualizate.</p><Button variant="secondary" disabled={busy} onClick={reload}>Reîncearcă</Button></div>}
    <Panel padded={false}>{!rows && !failed && <Loading rows={1} />}{rows?.length === 0 && <Empty />}
      {rows?.map(item => <div className={`${rowStyle} flex-wrap sm:flex-nowrap`} key={item.id}><SessionInfo item={item} title="Sesiune activă" today={today} />
        <Button variant="secondary" disabled={busy} onClick={async () => {
          await close("session-close", { id: item.id }, { text: "Închizi această sesiune? Pe dispozitivul ei trebuie să se conecteze din nou.", yes: "Închide sesiunea", danger: true }); reload();
        }}>Închide sesiunea</Button>
      </div>)}</Panel>
  </section>;
}

function UserForm({ user, busy, send }: { user?: AccountUser; busy: boolean; send: (action: UserAction, body: Record<string, string>) => Promise<void> }) {
  const [username, setUsername] = useState(""), [name, setName] = useState(user?.display_name ?? ""), [role, setRole] = useState<UserRole>(user?.role ?? "member");
  const submit = (event: FormEvent) => {
    event.preventDefault(); if (busy) return;
    void send(user ? "user-update" : "user-create", { ...(user ? { id: user.id } : { username: username.trim() }), display_name: name.trim(), role });
  };
  return <Form onSubmit={submit} aria-busy={busy}>
    {user ? <p className={noteLeftStyle}>Nume de utilizator: <b>{user.username}</b>.</p> : <><Label htmlFor="user-username">Nume de utilizator</Label><Input data-autofocus id="user-username" value={username} onChange={e => setUsername(e.target.value)} autoComplete="off" maxLength={64} required /></>}
    <Label htmlFor="user-name">Nume afișat</Label><Input id="user-name" value={name} onChange={e => setName(e.target.value)} maxLength={100} required />
    <Label htmlFor="user-role">Rol</Label><Select id="user-role" value={role} onChange={e => setRole(e.target.value as UserRole)}>{Object.entries(ROLE_NAMES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Select>
    <p className={noteLeftStyle}>Rolul se aplică tuturor firmelor.</p>
    <Actions><Button disabled={busy}>{user ? "Salvează utilizatorul" : "Creează utilizatorul"}</Button></Actions>
  </Form>;
}
