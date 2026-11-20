"use client";
import { Input, backLinkStyle, settingsPageStyle } from "./Controls";
import { useRef, useState } from "react";
import { postInstall } from "@/lib/api";
import { useConfirm, type Ask } from "./Confirm";
import { useApp } from "./context";
import { I, L, Title } from "./ui";

/** What the switch changes, asked before it is sent. */
const ASK: Record<string, Ask> = {
  live: { text: "Treci în Live? Facturile emise se trimit la ANAF Producție și fișierele de plăți se pot descărca.", yes: "Treci în Live" },
  sandbox: { text: "Treci în Sandbox? Facturile se trimit doar la ANAF Test și fișierele de plăți nu se mai pot descărca.", yes: "Treci în Sandbox" },
};

/** Tapping the other mode asks its question; a yes switches, a no leaves the active mode chosen. */
export function ModeChoice({ live, onSaved }: { live: boolean; onSaved: () => void | Promise<void> }) {
  const { say } = useApp();
  const { ask, dialog } = useConfirm();
  const current = live ? "live" : "sandbox";
  const [mode, setMode] = useState(current);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const requestId = useRef(crypto.randomUUID().replaceAll("-", ""));
  const save = async (next: string) => {
    if (lock.current || next === current) return;
    lock.current = true;
    setMode(next);
    try {
      if (!await ask(ASK[next])) { setMode(current); return; }
      setBusy(true);
      const r = await postInstall("mode-switch", { mode: next, expected: live ? "live" : "sandbox", confirmed: "yes", request_id: requestId.current });
      // A toast: the switch re-renders this page, which would drop a message kept here.
      if (r.message) say(r.message);
      if (!r.ok) setMode(current);
      await onSaved(); // a refused switch shows the mode in force now
    } catch { setMode(current); say("Modul nu a putut fi confirmat."); void onSaved(); }
    finally { lock.current = false; setBusy(false); }
  };
  return <div className="flex flex-col gap-6">
    <fieldset className="m-0 grid grid-cols-1 gap-3 border-0 p-0 sm:grid-cols-2">
      <legend className="sr-only">Live sau Sandbox</legend>
      {[{ value: "sandbox", title: "Sandbox", text: "Facturile se trimit doar la ANAF Test." }, { value: "live", title: "Live", text: "Facturile se trimit la ANAF Producție. Se pot pregăti fișiere de plăți." }].map(option => <label key={option.value} className="cursor-pointer rounded-card border border-solid border-line bg-surface p-5 has-[:checked]:border-action has-[:checked]:bg-selected has-[:disabled]:cursor-wait">
        <span className="flex items-center gap-3 font-semibold"><Input type="radio" name="mode" aria-label={option.title} checked={mode === option.value} onChange={() => void save(option.value)} disabled={busy}
          aria-describedby={option.value === current ? "mode-active" : undefined} />{option.title}
          {option.value === current ? <span id="mode-active" className="ml-auto rounded-control bg-inset px-2 text-xs font-semibold leading-6 text-ink">Activ</span> : null}</span>
        <p className="mt-3! text-muted">{option.text}</p>
      </label>)}
    </fieldset>
    {dialog}
  </div>;
}

export function Mod() {
  const { view, refresh } = useApp();
  return <div className={settingsPageStyle}><L to={{ tab: "setari" }} className={backLinkStyle}><I n="arrow-left" />Setări</L><Title>Live sau Sandbox</Title>
    <ModeChoice key={String(view.live)} live={view.live} onSaved={() => void refresh()} />
  </div>;
}
