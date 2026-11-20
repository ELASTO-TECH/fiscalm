"use client";
import { Anchor, Button, Input, Label, Actions, Textarea, noteLeftStyle } from "./Controls";
import { useState } from "react";
import { post } from "@/lib/api";
import { parts, type ActionResult, type Done, type Integration } from "@/lib/types";
import { useApp } from "./context";
import { CloseX, Sheet, SheetHead } from "./Sheets";
import { L, TONE_TEXT, Summary } from "./ui";

type Check = { key: string; ok: boolean; message: string };
type SetupResult = ActionResult & { done?: Done & { certificate?: string; return_urls?: string[]; checks?: Check[] } | null };
const STEPS = ["Contul Enable Banking", "Certificatul aplicației", "Datele aplicației", "Accesul la conturi", "Verificarea configurării"];

function CopyValue({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); setCopied(true); setFailed(false); }
    catch { setFailed(true); }
  };
  return <div className="space-y-2">
    <Label>{label}<Textarea className="mt-2" readOnly value={value} rows={value.includes("BEGIN CERTIFICATE") ? 4 : 2} /></Label>
    <Button type="button" variant="secondary" onClick={() => void copy()}>{copied ? "Copiat" : `Copiază ${label.toLowerCase()}`}</Button>
    {failed ? <p role="status">Copierea nu a reușit. Selectează textul și copiază-l manual.</p> : null}
  </div>;
}

/** The install's own provider account: five explicit steps, no network or key generation on opening the guide. */
/** `known`: the install's own return address (FISCALM_PUBLIC_URL), proposed instead of asked. */
export function OpenBankingSetup({ item, fresh, onClose, known = "" }: { item?: Integration; fresh: boolean; onClose: () => void; known?: string }) {
  const { view, run, busy } = useApp();
  const [step, setStep] = useState(item ? 3 : 0);
  const [certificate, setCertificate] = useState("");
  const [application, setApplication] = useState(item?.config.application_id ?? "");
  const [primary, setPrimary] = useState(item?.config.return_url || known);
  const [extra, setExtra] = useState(item?.config.return_urls ?? "");
  const [saved, setSaved] = useState(!!item);
  const [checks, setChecks] = useState<Check[]>([]);
  const admin = !!parts(view)[0]?.can_manage_companies;
  const operation = (op: "prepare" | "check") => run<SetupResult>(view.co,
    () => post("bank-setup", { co: view.co, operation: op }), undefined, (r) => {
      if (op === "prepare") { setCertificate(r.done?.certificate ?? ""); if (!primary) setPrimary(r.done?.return_urls?.[0] ?? ""); }
      else setChecks(r.done?.checks ?? []);
      return false;
    });
  const save = () => run(view.co, () => post(item ? "integration-update" : "integration-add", {
    co: view.co, ...(item ? { id: item.id } : { kind: "openbanking" }), name: "Enable Banking",
    f_provider: "enablebanking", f_application_id: application, f_return_url: primary, f_return_urls: extra,
  }), undefined, () => { setSaved(true); setChecks([]); setStep(3); return false; });
  const complete = checks.length > 0 && checks.every((c) => c.ok) && checks.at(-1)?.key === "return";
  return <Sheet onClose={onClose} label="Configurare Enable Banking" cls="wide">
    <CloseX onClose={onClose} /><SheetHead title="Conectarea la bănci" />
    {step === 0 ? <p>Enable Banking citește conturile pentru fiscalm. Parolele bancare nu ajung în fiscalm.</p> : null}
    {!admin ? <p className={noteLeftStyle}>Configurarea o poate face un administrator.</p> : null}
    <p>Pasul {step + 1} din 5</p><progress className="my-4 h-1 w-full accent-action" aria-label="Progresul configurării" max={5} value={step + 1} />
    <div className="grid min-w-0 gap-6 md:grid-cols-[200px_minmax(0,1fr)]">
      <nav className="flex flex-wrap gap-2 md:flex-col md:items-stretch" aria-label="Pașii configurării">{STEPS.map((title, i) => <Button type="button" variant="quiet" className="justify-start text-left aria-[current=step]:bg-selected aria-[current=step]:text-ink" key={title}
        aria-label={`${i + 1}. ${title}`} aria-current={step === i ? "step" : undefined} onClick={() => setStep(i)}><span>{i + 1}.</span> <span className="hidden md:inline">{title}</span></Button>)}</nav>
      <section className="min-w-0 space-y-4" aria-labelledby="bank-step-title"><h3 id="bank-step-title" className="m-0 text-lg font-semibold">{STEPS[step]}</h3>
        {step === 0 ? <><p>Creează un cont Enable Banking pentru această instalare, cu adresa de e-mail.</p>
          <Anchor className="btn g" href="https://enablebanking.com/sign-in" target="_blank" rel="noreferrer">Deschide Enable Banking</Anchor>
          <p className={noteLeftStyle}>Contul gratuit citește doar conturile proprii, în modul restricted. fiscalm găzduit pentru clienți necesită nivelul comercial.</p></> : null}
        {step === 1 ? <><p>În Enable Banking, deschide API applications, apoi Add a new application. Alege Production și importul certificatului public. Numele aplicației este fiscalm.</p>
          <Button type="button" disabled={busy || !admin} onClick={() => void operation("prepare")}>Pregătește certificatul</Button>
          <p className={noteLeftStyle}>fiscalm generează cheia pe server. Cheia privată rămâne pe server; copiezi doar certificatul.</p>
          {certificate ? <><CopyValue label="Certificatul" value={certificate} />
            {[primary, ...extra.split(/\s+/)].filter(Boolean).map((url) => <CopyValue key={url} label="Adresa de întoarcere" value={url} />)}</> : null}</> : null}
        {step === 2 ? <><p>Salvează aici ID-ul primit în Enable Banking. Înregistrează fiecare adresă de întoarcere și în aplicația Enable Banking.</p>
          <div>
            <Label htmlFor="eb-app">ID-ul aplicației</Label><Input id="eb-app" value={application} onChange={(e) => { setApplication(e.target.value); setSaved(false); setChecks([]); }} />
            <details open={!known} className="my-3"><Summary>Adrese de întoarcere</Summary>
            <Label htmlFor="eb-primary">Adresa principală de întoarcere</Label><Input id="eb-primary" value={primary} onChange={(e) => { setPrimary(e.target.value); setSaved(false); setChecks([]); }} />
            <Label htmlFor="eb-extra">Alte adrese de întoarcere</Label><Input id="eb-extra" value={extra} onChange={(e) => { setExtra(e.target.value); setSaved(false); setChecks([]); }} />
            <p className={noteLeftStyle}>Adrese separate prin spațiu, de exemplu adresa Tailscale. Pe telefon, folosește o adresă HTTPS accesibilă de pe telefon.</p></details>
            <Button type="button" disabled={busy || !admin || !fresh || !application || !primary || (saved && !item)} onClick={() => void save()}>Salvează aplicația</Button>
          </div></> : null}
        {step === 3 ? <><p>În panoul Enable Banking, autorizează conturile proprii pentru aplicația fiscalm, în modul restricted.</p>
          <p>La Banca Transilvania, alege BT Go și confirmă în aplicația BT Go. Mai târziu, conectarea din fiscalm cere încă o confirmare bancară.</p></> : null}
        {step === 4 ? <>
          <Button type="button" disabled={busy || !admin || !saved} onClick={() => void operation("check")}>Verifică configurarea</Button>
          <ul className="m-0 list-none space-y-1 p-0 text-sm" aria-live="polite">{checks.map((c) => <li key={c.key} className={`${c.ok ? "ok" : "late"} ${TONE_TEXT[c.ok ? "ok" : "late"]}`}>{c.ok ? "✓ " : ""}{c.message}</li>)}</ul>
          {complete ? <><p className="font-semibold text-ok">Configurarea este verificată.</p><L className="btn" to={{ tab: "setari", sec: "firme" }}>Deschide firmele</L></> : null}</> : null}
        <Actions spread><Button type="button" variant="secondary" disabled={step === 0} onClick={() => setStep(step - 1)}>Înapoi</Button>
          {step < 4 ? <Button type="button" onClick={() => setStep(step + 1)}>Continuă</Button> : <Button type="button" variant="secondary" onClick={onClose}>Închide</Button>}</Actions>
      </section>
    </div>
  </Sheet>;
}
