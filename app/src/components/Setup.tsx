"use client";
// The install's first firm: the one question after the account. Only administrators can add it.
import { formTypography, buttonStyle, buttonTone, Button, bannerStyle, noteLeftStyle } from "./Controls";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { NO_ANSWER, getCompanies, postCompany, type CompanyResult } from "@/lib/api";
import type { CompaniesConfig, SetupState, View } from "@/lib/types";
import { Brand } from "./Brand";
import { SignOut } from "./UserAccount";
import { AddCompanyForm, unanswered } from "./Companies";

function SetupPage({ children }: { children: ReactNode }) {
  return <div className="mx-auto max-w-[660px] px-4"><main className={`setup mx-auto flex w-full max-w-[620px] flex-col gap-6 px-1 pb-8 pt-3 sm:pt-8 ${formTypography}`}>
    <header className="flex flex-wrap items-center justify-between gap-4 border-0 border-b border-solid border-line pb-5 [&_.wm]:m-0 [&_.wm]:text-2xl"><Brand /><nav className="ml-auto flex items-center gap-2" aria-label="Cont"><a className={`${buttonStyle} ${buttonTone.quiet}`} href="?tab=setari&sec=cont">Cont</a><SignOut /></nav></header>
    {children}
  </main></div>;
}

export function Setup({ s, onAdded, onFinish, onStale }: {
  s: SetupState;
  onAdded: (key: string, state: View | null) => void;
  /** Open the new company; `message` is the engine's last word (the firm added), for the app's toast. */
  onFinish: (key: string, message?: string) => void;
  /** An add was refused (e.g. a company was added meanwhile): read the state again. */
  onStale: () => void;
}) {
  const [config, setConfig] = useState<CompaniesConfig | null>(null);
  const [failed, setFailed] = useState(false);
  const [n, setN] = useState(0);
  const [busy, setBusy] = useState(false);
  const inflight = useRef(false); // busy for taps in the same tick, before React re-renders
  const [said, setSaid] = useState<{ text: string; ok: boolean } | null>(null);
  const manage = s.can_manage_companies;

  useEffect(() => {
    if (!manage) return;
    let live = true;
    getCompanies().then((d) => {
      if (!live) return;
      setConfig(d);
      setFailed(false);
    }, () => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [manage, n]);

  /** One request at a time; the engine's message is shown on this page (there is no app around it for a toast). */
  const send = useCallback(async (action: Parameters<typeof postCompany>[0], body: Record<string, string>) => {
    if (inflight.current) return null;
    inflight.current = true;
    setBusy(true);
    setSaid(null); // the same message twice is announced twice
    try {
      const r = await postCompany(action, body);
      // The lookup's answer shows in the form: the firm found, or the refusal under the CUI.
      setSaid(r.message && action !== "company-lookup" ? { text: r.message, ok: r.ok } : null);
      if (action === "company-add" && !r.ok) onStale();
      return r;
    } catch {
      if (action === "company-lookup") return unanswered(); // shown under the CUI; a lookup saves nothing
      setSaid({ text: NO_ANSWER, ok: false }); // an add may have been saved
      if (action === "company-add") onStale();
      return null;
    } finally {
      inflight.current = false;
      setBusy(false);
    }
  }, [onStale]);

  const added = (r: CompanyResult) => {
    const key = r.done?.key;
    if (!key) return onStale();
    onAdded(key, (r.state as View | null | undefined) ?? null);
    onFinish(key, r.message);
  };

  if (!manage) return <SetupPage><p>Administratorul configurează încă această instalare.</p></SetupPage>;
  return <SetupPage>
    <h1 className="m-0 mt-2 text-3xl font-semibold tracking-tight leading-tight" tabIndex={-1}>Prima firmă</h1>
    {failed ? (
      <div className={bannerStyle} role="alert">
        <b>Firmele nu au putut fi încărcate.</b>
        <Button type="button" variant="secondary" onClick={() => setN((x) => x + 1)}>Reîncearcă</Button>
      </div>
    ) : config ? <AddCompanyForm config={config} busy={busy} send={send} onDone={added} /> : <p role="status">Se încarcă datele firmei…</p>}
    <p className={noteLeftStyle} role="status">{said?.ok ? said.text : ""}</p>
    <p className={noteLeftStyle} role="alert">{said && !said.ok ? said.text : ""}</p>
  </SetupPage>;
}
