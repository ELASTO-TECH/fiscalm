"use client";
import { Button, surfaceStyle, Input, Label, Actions, formTypography, noteLeftStyle, legendStyle } from "./Controls";
import { Mod } from "./Mode";
// The quieter tabs: Sarcini, Setări and the phone's Mai mult (its link is in the top bar); the answers to a mailed document in doubt;
// the Nou menu.
import { useCallback, useId, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { curOf, dm, lei } from "@/lib/format";
import type { Tab } from "@/lib/nav";
import { getBrowse } from "@/lib/api";
import { isAll, type Co, type Issued, type MailQuestion } from "@/lib/types";
import { useApp, useCan, useCompany } from "./context";
import { Acces } from "./Access";
import { Users } from "./Users";
import { Firme } from "./Companies";
import { Facturare } from "./InvoiceSeries";
export { Tasks as Sarcini } from "./Tasks";
import { Contabil } from "./Accountant";
import { ConfigCard, Integrari, SystemCard } from "./Settings";
import { CloseX, Sheet } from "./Sheets";
import { PageNav, usePage } from "./Paging";
import { Act, I, L, Row, Title } from "./ui";
import { UPLOAD_LABEL, useExpenseUpload } from "./ExpenseUpload";

/** Opens Factură nouă over the current page, for a partner when given; disabled under Toate firmele, where it isn't clear
 * which company issues it. */
export function NewInvoice({ className, onGo, partner = "", children }: {
  className: string; onGo?: () => void; partner?: string; children: ReactNode;
}) {
  const { view } = useApp();
  const allowed = useCan("member");
  if (!allowed) return null;
  return isAll(view) ? <Button type="button" className={className} variant="secondary" disabled>{children}</Button>
    : <L to={{ new: "1", for: partner }} className={className} onGo={onGo}>{children}</L>;
}

/** "Nou": a button that opens, in a sheet, what can be created, each in the flow that already makes it: Încarcă bon sau
 * factură, from this device (ExpenseUpload.tsx); Factură nouă; Storno,
 * from the issued invoice it reverses (its sheet has the storno); Bondrop, a bon or chitanță sent from the phone through a receipt link (Setări › Acces).
 * Under Toate firmele every entry waits for a company to be chosen. Never a page of its own. */
export function NewMenu({ className, children }: { className: string; children: ReactNode }) {
  const { view } = useApp();
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);
  const all = isAll(view);
  const allowed = useCan("member");
  const upload = useExpenseUpload(close);
  if (!allowed || !upload) return null;
  return (
    <>
      <button type="button" className={className} aria-label="Nou" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}>
        {children}
      </button>
      {open ? createPortal(
        <Sheet label="Nou" onClose={close}>
          <CloseX onClose={close} />
          <div className="grid gap-2 [&_.btn]:w-full [&_.btn]:justify-start [&_.btn]:text-left">
            {upload.input}
            <Button type="button" className="btn" disabled={all || upload.busy} aria-busy={upload.busy} onClick={upload.pick}>
              <I n="camera" />{upload.busy ? "Se încarcă…" : UPLOAD_LABEL}
            </Button>
            <NewInvoice className="btn g" onGo={close}><I n="plus" />Factură nouă</NewInvoice>
            {all ? <Button type="button" className="btn g" disabled><I n="arrow-counter-clockwise" />Factură de stornare</Button>
              : <StornoPick co={view.co} today={view.today} onGo={close} />}
            {all ? <Button type="button" className="btn g" disabled><I n="receipt" /><span className={newChoiceText}><b>Bondrop</b> <small>Bonuri și chitanțe de pe telefon</small></span></Button>
              : <L to={{ tab: "setari", sec: "acces" }} at="h-links" className="btn g" onGo={close}><I n="receipt" /><span className={newChoiceText}><b>Bondrop</b> <small>Bonuri și chitanțe de pe telefon</small></span></L>}
          </div>
          {all ? <p className={`${noteLeftStyle} mt-4!`}>Alege mai întâi o firmă.</p> : null}
        </Sheet>, document.body) : null}
    </>
  );
}

/** A choice's name and, under it, what it is. */
const newChoiceText = "grid min-w-0 leading-tight [&_small]:mt-0.5 [&_small]:text-xs [&_small]:font-normal [&_small]:text-muted";

/** Storno: the issued invoices the engine would reverse (`can_storno`), newest first; each opens its sheet, where the storno is. */
function StornoPick({ co, today, onGo }: { co: Co; today: string; onGo: () => void }) {
  const [open, setOpen] = useState(false);
  const read = useCallback((cursor: string, signal?: AbortSignal) => getBrowse(co, "vanzari", { f: "storno", cursor }, signal), [co]);
  const page = usePage(read);
  const empty = !page.loading && !page.error && !page.total;
  return (
    <>
      <Button type="button" className="btn g" aria-disabled={empty} aria-expanded={open && !empty} aria-controls="nou-storno"
        aria-describedby={empty ? "nou-storno-none" : undefined} onClick={() => { if (!empty) setOpen(!open); }}>
        <I n="arrow-counter-clockwise" />Factură de stornare
      </Button>
      {empty ? <p id="nou-storno-none" className={noteLeftStyle}>Nicio factură de stornat.</p> : null}
      <div id="nou-storno" className={`${surfaceStyle} overflow-hidden`} role="group" aria-label="Alege factura de stornat" hidden={!open || empty}>
        {page.loading || page.error ? <PageNav page={page} /> : null}
        {page.data.filter((r): r is Issued => !("kind" in r)).slice(0, 6).map((r) => (
          <Row key={r.id} icon="invoice" title={r.client} sub={`nr. ${r.number}, ${dm(r.issue_date, today)}`}
            val={`${lei(r.total)} ${curOf(r.currency)}`} to={{ iss: r.id }} onGo={onGo} />
        ))}
        {page.total > 6 ? <Row icon="invoice" title="Toate facturile emise" to={{ tab: "vanzari" }} onGo={onGo} /> : null}
      </div>
    </>
  );
}

const MORE: [Tab, string, string][] = [
  ["vanzari", "invoice", "Vânzări"], ["plati", "currency-circle-dollar", "Fișiere de plăți"], ["parteneri", "user", "Parteneri"],
  ["produse", "package", "Produse"],   ["rapoarte", "clipboard-text", "Rapoarte"], ["automatizari", "sparkle", "Automatizări"],
];
/** The sections the phone's tab bar leaves to Mai mult (Setări has the gear beside it in the top bar). */
export const MORE_TABS: readonly Tab[] = ["mai-mult", ...MORE.map(([k]) => k)];

/** The phone's Mai mult: the sections without a slot in the tab bar. */
export function MaiMult() {
  return (
    <div>
      <Title>Mai mult</Title>
      <nav className={`card ${surfaceStyle} mt-5 p-0`} aria-label="Mai mult">
        {MORE.map(([k, icon, label]) => <Row key={k} icon={icon} title={label} to={{ tab: k }} />)}
      </nav>
    </div>
  );
}

/** Why a mailed document waits for an answer, in one sentence. */
export function questionLine(m: MailQuestion): string {
  return {
    "upload-firm": "Pentru ce firmă e documentul?",
    "no-firm": "Pe document nu apare nicio firmă.",
    "two-firms": "Pe document apar mai multe firme.",
    "other-firm": "Documentul numește altă firmă decât cea aleasă pentru acest expeditor.",
    "buyer-unclear": "Nu reiese clar din document că firma este cumpărătorul.",
    total: "Suma totală nu reiese clar din document.",
    "outside-scope": "Această adresă de e-mail nu include firma de pe document. Pentru facturi, adaugă firma la această adresă în Setări › Integrări, apoi atribuie documentul. Extrasele și ordinele de plată se încarcă manual.",
  }[m.reason];
}

/** The answers to a mailed document in doubt: the total when several were read, whether to remember the sender, one
 * button per firm it may be for, and "Nu este factură". The engine checks each answer again (mail_invoices.answer). */
export function MailAnswer({ m }: { m: MailQuestion }) {
  const state = useCompany();
  const [total, setTotal] = useState<number | null>(m.totals.length > 1 ? null : 0);
  // The sender's next documents go the same way; not for a document outside the inbox's firms (the engine ignores it).
  const scoped = m.reason === "outside-scope" || m.reason === "upload-firm";
  const [remember, setRemember] = useState(!scoped);
  const id = `qa${useId().replace(/:/g, "")}`; // one per instance: Rapoarte and its sheet show the same question
  const firms = state.companies.filter((c) => m.companies.includes(c.key));
  const pick = m.totals.length > 1 && total === null;
  const extra = { ...(m.totals.length > 1 && total !== null ? { total: String(total) } : {}), ...(remember ? { remember: "1" } : {}) };
  return (
    <div className="min-w-0 space-y-4">
      {m.totals.length > 1 ? (
        <fieldset className="m-0 min-w-0 border-0 p-0">
          <legend className={legendStyle}>Suma totală</legend>
          {m.totals.map((t, i) => (
            <Label key={i} className="cursor-pointer">
              <Input type="radio" name={`${id}-total`} checked={total === i} onChange={() => setTotal(i)} />
              {lei(t.amount)} {curOf(t.currency)}
            </Label>
          ))}
          {pick ? <p className={noteLeftStyle} id={`${id}-pick`}>Alege mai întâi suma totală.</p> : null}
        </fieldset>
      ) : null}
      {scoped ? null : <Label className="cursor-pointer" htmlFor={`${id}-remember`}>
        <Input id={`${id}-remember`} type="checkbox" checked={remember}
          onChange={(e) => setRemember(e.target.checked)} />
        Salvează regula pentru expeditor
      </Label>}
      <Actions>
        {firms.map((c) => (
          <Act key={c.key} action={m.reason === "upload-firm" ? "upload-assign" : "mail-answer"} body={{ key: m.key, who: c.key, ...(m.reason === "upload-firm" ? {} : extra) }} label={`Atribuie ${c.name}`}
            cls={c.key === state.co ? "btn" : "btn g"} off={pick} describedBy={pick ? `${id}-pick` : undefined} />
        ))}
        {m.reason !== "upload-firm" && <Act action="mail-answer" body={{ key: m.key, who: "none", ...(remember ? { remember: "1" } : {}) }}
          label="Nu este factură" cls="btn g" />}
      </Actions>
    </div>
  );
}

export function Setari() {
  const { route } = useApp();
  const firms = useCan("manage_firms"), integrations = useCan("manage_integrations"), usersAllowed = useCan("manage_users");
  if (route.sec === "mod" && firms) return <Mod />;
  if (route.sec === "utilizatori" && usersAllowed) return <Users />;
  if (route.sec === "contabil") return <Contabil />;
  if (route.sec === "facturare") return <Facturare />;
  if (route.sec === "firme" && firms) return <Firme />;
  if (route.sec === "integrari" && integrations) return <Integrari />;
  if (route.sec === "acces") return <Acces />;
  return <Overview />;
}

function Overview() {
  return (
    <div className={`mx-auto w-full max-[1099px]:max-w-[880px] min-w-0 ${formTypography}`}>
      <Title>Setări</Title>
      <ConfigCard />
      <SystemCard />
    </div>
  );
}
