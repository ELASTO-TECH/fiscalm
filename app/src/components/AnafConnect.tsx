// The ANAF integration's connection, in its sheet: the Callback URL the ANAF OAuth app needs, the connection's state,
// and "Conectează ANAF", which opens ANAF's certificate login. The engine decides every state (anaf.connection) and every
// refusal, the last attempt's included; the app only shows them and follows the start link the engine gives, on the
// install's own address.
import { Actions, Anchor, Button, Input, Label, bannerStyle, noteLeftStyle } from "./Controls";
import { type ReactNode, useEffect, useId, useState } from "react";
import { post, type BankConnectResult } from "@/lib/api";
import { dm, type Tone } from "@/lib/format";
import type { AnafConnection, AnafRedirect, Integration } from "@/lib/types";
import { leave } from "./BankConnect";
import { scopeLine } from "./FirmScope";
import { useApp, useCompany } from "./context";
import { I, L, ROW_TINT, Spinner, Summary } from "./ui";

/** The ANAF portal (SPV): a fixed address, never one built from data. */
export const ANAF_PORTAL = "https://pfinternet.anaf.ro";

/** How the connection reads: [tone, what it is, what follows]. */
export function connectionLine(c: AnafConnection, today: string): [Tone | "", string, string] {
  if (c.state === "connected") return ["ok", "Conectat", `Conexiunea expiră pe ${dm(c.expires, today)}.`];
  if (c.state === "expiring") return ["soon", "Expiră curând", `Conexiunea expiră pe ${dm(c.expires, today)}.`];
  if (c.state === "expired") return ["late", "Conexiunea a expirat", "Facturile noi din e-Factura nu se mai citesc până la reconectare."];
  if (c.state === "error") return ["late", "Conexiunea nu poate fi citită", "Facturile din e-Factura se citesc după reconectare."];
  return ["", "Neconectat", "Facturile din e-Factura se citesc după conectare."];
}

/** The engine answers with a start link on this install's own public address (the origin of the Callback URL): anything
 * else is never followed. Parsed as the browser will: https, that exact origin, the start path, no name or password. */
export function safeStart(url: unknown, redirect: string | null): url is string {
  if (typeof url !== "string" || !redirect || !/^[!-~]+$/.test(url) || url.includes("\\")) return false;
  try {
    const u = new URL(url);
    const r = new URL(redirect);
    return u.protocol === "https:" && u.origin === r.origin && !u.username && !u.password && !u.search && !u.hash
      && /^\/anaf\/start\/[A-Za-z0-9_-]{40,64}$/.test(u.pathname);
  } catch {
    return false;
  }
}

/** The Callback URL to register at ANAF, or why there is none yet. Shown on the add sheet too: ANAF asks for it
 * before it gives the Client ID. */
export function AnafRedirectField({ r }: { r: AnafRedirect | undefined }) {
  const id = useId();
  if (!r) return null;
  if (!r.redirect_uri) {
    return <div className={bannerStyle} role="status"><b>{r.redirect_error}</b></div>;
  }
  return (
    <div className="mt-2">
      <Label htmlFor={id}>Adresa de redirecționare (Callback URL)</Label>
      <Input id={id} value={r.redirect_uri} readOnly spellCheck={false} onFocus={(e) => e.currentTarget.select()} />
      <p className={noteLeftStyle}>Copiaz-o exact așa la Callback URL, în aplicația OAuth de pe anaf.ro.</p>
    </div>
  );
}

/** Prepare the ANAF application's credentials before authorizing this install with the certificate. */
export function AnafSetupGuide() {
  return <section className="my-5 flex flex-col gap-3 text-sm leading-relaxed" aria-labelledby="anaf-setup-title">
    <h3 id="anaf-setup-title" className="m-0 text-base font-semibold">Pregătește conectarea la ANAF</h3>
    <p>Ai nevoie de Client ID și Client Secret din portalul ANAF. În acest pas intri cu utilizator și parolă. Certificatul și PIN-ul se cer după salvarea integrării.</p>
    <Anchor className="btn g self-start" href={ANAF_PORTAL} target="_blank" rel="noopener noreferrer">Deschide portalul ANAF într-un tab nou</Anchor>
    <details className="rounded-control bg-inset p-3"><Summary>Pașii din portalul ANAF</Summary><ol className="mt-3 mb-0 space-y-3 pl-5">
      <li>Autentifică-te cu utilizatorul și parola contului ANAF pentru API-uri, apoi deschide <b>Editare profil OAuth → Gestionare aplicații</b>. Dacă ai doar certificat SPV, creează și contul pentru API-uri:{" "}
        <a href="https://www.anaf.ro/InregOauth" target="_blank" rel="noopener noreferrer">înregistrează-te pentru API-uri</a>.</li>
      <li>Înregistrează aplicația <b>fiscalm</b> pentru serviciul <b>E-Factura</b>. La <b>Callback URL</b>, copiază adresa de mai jos.
        Dacă folosești o aplicație existentă, adaugă adresa fără să le ștergi pe celelalte.</li>
      <li>Pentru o aplicație nouă, apasă <b>Generare Client ID</b>. Deschide detaliile aplicației. Revino în acest tab și copiază
        <b> Client ID</b> și <b>Client Secret</b> în câmpurile de mai jos.</li>
      <li>Apasă <b>Salvează și continuă</b>, apoi <b>Conectează ANAF</b>. În browserul în care ai certificatul digital,
        confirmă certificatul și introdu PIN-ul la ANAF. Revii automat în fiscalm.</li>
    </ol></details>
  </section>;
}

/** The connection of a saved ANAF integration and the button that starts it (an administrator, the integration on,
 * its client saved, a Callback URL). Allowed before the install goes live: connecting sends nothing to SPV. Once the
 * client is saved, `children` (the integration's own fields) and the Callback URL fold under "Setări avansate": a
 * reconnection needs only the state and the button. */
export function AnafConnect({ item, children }: { item: Integration; children: ReactNode }) {
  const { run, busy, view } = useApp();
  const id = useId();
  const s = useCompany();
  const [opening, setOpening] = useState(false);
  // Back from ANAF's page can restore this one as it was left: the button must work again.
  useEffect(() => {
    const restored = (e: PageTransitionEvent) => { if (e.persisted) setOpening(false); };
    window.addEventListener("pageshow", restored);
    return () => window.removeEventListener("pageshow", restored);
  }, []);
  const c = item.connection;
  if (!c) return children;
  const [tone, title, line] = connectionLine(c, view.today);
  const firms = scopeLine(item.firm_scope, view.companies); // folded with the fields below once the client is saved
  const again = c.state !== "not_connected";
  const client = !!item.config.client_id && !!item.secrets.client_secret;
  const admin = !!s.can_manage_companies;
  const ready = admin && item.enabled && client && !!c.redirect_uri;
  const open = async () => {
    setOpening(true);
    let leaving = false;
    await run<BankConnectResult>(s.co, () => post("integration-connect", { co: s.co, id: item.id }), undefined, (r) => {
      const url = r.done?.url;
      if (safeStart(url, c.redirect_uri)) { leaving = true; leave.to(url); }
      return false;
    });
    if (!leaving) setOpening(false); // while the browser leaves for ANAF, the button keeps saying so
  };
  // Without a client the fields below are the next step; nothing else needs saying.
  const why = !client ? "" : !admin ? "Conectarea o pornește un administrator."
    : !item.enabled ? "Integrarea este oprită. Pornește-o ca să te conectezi."
    : c.redirect_error ?? "";
  const setup = <div className="mt-2 flex flex-col gap-4">
    <p className={noteLeftStyle}>{view.live ? "Facturile se trimit la ANAF Producție." : "Facturile se trimit la ANAF Test, fără valoare fiscală."} Mediul urmează modul din Setări. Facturile primite și emise se citesc din Producție.</p>
    <p className={noteLeftStyle}>Trimiterea automată în SPV se alege pe fiecare firmă, în <L to={{ tab: "automatizari" }} className="font-semibold text-ink underline">Automatizări</L>.</p>
    {client && !c.redirect_uri ? null : <AnafRedirectField r={c} />}
    {children}
  </div>;
  return <>
    <section className="mb-6 flex flex-col gap-4" aria-labelledby={id}>
      <h2 id={id} className="m-0">Conexiunea ANAF</h2>
      <div className="flex items-start gap-3 rounded-control bg-inset p-4">
        <span className={`grid size-9 shrink-0 place-items-center rounded-control [&_svg]:size-4 ${tone} ${ROW_TINT[tone] ?? "bg-surface"}`}><I n="link-simple" /></span>
        <div className="min-w-0"><b>{title}</b><p className="mt-1 text-muted">{line}</p>
          {firms && client ? <p className="mt-1 text-muted">Firme: {firms}. Se schimbă în Setări avansate.</p> : null}</div>
      </div>
      {c.refusal ? <div className="flex items-start gap-3 rounded-control bg-inset p-4" role="alert">
        <span className={`late grid size-9 shrink-0 place-items-center rounded-control [&_svg]:size-4 ${ROW_TINT.late}`}><I n="warning-circle" /></span>
        <div className="min-w-0"><b>ANAF nu a fost conectat</b><p className="mt-1 text-muted">{c.refusal}</p></div>
      </div> : null}
      {ready ? <div>
        <Actions className="btns mt-0!">
          <Button type="button" variant={c.state === "connected" ? "secondary" : "primary"}
            disabled={busy || opening} aria-busy={opening} onClick={() => void open()}>
            {opening ? <Spinner /> : null}
            {opening ? "Se deschide ANAF…" : again ? "Reconectează ANAF" : "Conectează ANAF"}
          </Button>
        </Actions>
        <p className={noteLeftStyle}>Ai nevoie de certificatul digital și de PIN.</p>
      </div> : why ? <p className={noteLeftStyle} role="status">{why}</p> : null}
    </section>
    {client ? <details><Summary>Setări avansate</Summary>{setup}</details> : setup}
  </>;
}
