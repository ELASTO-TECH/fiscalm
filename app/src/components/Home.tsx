"use client";
import { UPLOAD_LABEL, useExpenseUpload } from "./ExpenseUpload";
import { Button, buttonStyle, buttonTone, leadStyle, noteLeftStyle, surfaceStyle } from "./Controls";
import { CodTasks, SettlementTasks } from "./Settlements";
import { useCallback, useState, type ReactElement, type ReactNode } from "react";
import { FIXTURES, actionRole, getCandidates, packFile, post } from "@/lib/api";
import { cap, curOf, daysTo, dm, dmt, lei, luna, lunaName, nOf, weekday } from "@/lib/format";
import { FirmStatementUpload } from "./Accounts";
import { coName, isAll, parts, type Ambiguous, type Invoice, type PartnerTask, type PaymentFile, type ReminderTask, type Report, type State, type TaskEntry, type View } from "@/lib/types";
import { Company, useApp, useCan, useCompany } from "./context";
import { anafExpiringSoon, findIssued, openInvoices, refusalReason, REPORT_LABEL } from "./derive";
import { ANAF_PORTAL } from "./AnafConnect";
import { NewInvoice, questionLine } from "./Tabs";
import { Dashboard } from "./Dashboard";
import { bankTasks } from "./BankConnect";
import { PageNav, usePage } from "./Paging";
import { SetupCard } from "./SetupCard";
import { Act, I, L, Task, Title, Summary } from "./ui";

/** A month's pack that can be downloaded now: ready (prepared on the tap) or prepared and not yet marked sent. */
const packToSend = (s: State) => s.reports.find((r) => r.status === "ready" || r.status === "prepared");

/** "Descarcă dosarul": a ready month is prepared on the person's tap (pack-prepare, local) and the ZIP of the version it
 * froze is downloaded; a prepared one downloads as it is, unless the month changed since (`stale`): then the tap prepares
 * a new version, and the old one is never offered. */
function PackDownload({ month, className = `${buttonStyle} ${buttonTone.primary}`, children = "Descarcă dosarul" }: {
  month: string; className?: string; children?: ReactNode;
}) {
  const { run, busy } = useApp();
  const s = useCompany();
  const canPrepare = useCan(actionRole("pack-prepare"));
  const r = s.reports.find((x) => x.month === month);
  if (!r) return null;
  if (r.status !== "ready" && r.version && !r.stale) return <a className={className} href={packFile(s.co, month, r.version)}>{children}</a>;
  if (!canPrepare) return null;
  const download = async () => {
    const res = await run(s.co, () => post("pack-prepare", { co: s.co, month }), undefined, () => false);
    const version = res?.ok ? res.state?.reports.find((x) => x.month === month)?.version : null;
    if (!version || FIXTURES) return;
    const link = document.createElement("a");
    link.href = packFile(s.co, month, version); document.body.append(link); link.click(); link.remove();
  };
  return <button type="button" className={className} disabled={busy} onClick={download}>{children}</button>;
}

export function PackButtons({ month }: { month: string }) {
  const s = useCompany();
  const r = s.reports.find(x => x.month === month);
  if (!r || (r.status !== "ready" && !r.version)) return null;
  // preparing never waits for them: the pack lists them as missing in its rezumat.csv
  // a task's own report arrives whole (api/tasks.py), a summary's with counts: the same count as `missing`
  const unexplained = r.status !== "ready" && !r.stale ? 0 : r.counts ? r.counts.card - r.counts.matched : r.bank?.card.filter((t) => !t.doc).length ?? 0;
  const prepared = r.status === "prepared" && r.version && r.prepared_at ? `Pregătit pe ${dmt(r.prepared_at, s.today)}. ` : "";
  return <>
    {prepared || r.stale || unexplained > 0 ? <p className={`${noteLeftStyle} w-full pl-11 lg:pl-0 [&_a]:font-semibold [&_a]:text-ink [&_a]:underline [&_a]:underline-offset-4`}>
      {prepared}{r.stale ? "Între timp s-au schimbat tranzacții sau documente. " : null}
      {unexplained > 0 ? <L to={{ co: s.co, tab: "tranzactii", month }}>{cap(nOf(unexplained, "o tranzacție fără document", "tranzacții fără document"))}</L> : null}
    </p> : null}
    <PackDownload month={month}>{r.stale ? "Pregătește din nou" : undefined}</PackDownload>
    {r.status === "prepared" && r.version ? <Act action="pack-sent" body={{ month, version: r.version, mark: r.mark }} label="Marchează ca trimis" cls="btn g" /> : null}
    {r.status === "sent" && r.version ? <Act action="pack-unsent" body={{ month, version: r.version, mark: r.mark }} label="Anulează marcarea" cls="btn g" /> : null}
  </>;
}

/** The pack's task, in words for what this person may do: only someone who can mark it sent is asked to send it. */
function PackTask({ r, title }: { r: Report; title: string }) {
  const sends = useCan(actionRole("pack-sent"));
  const body = !sends ? r.status === "ready" ? "Doar un membru sau un administrator poate pregăti dosarul."
    : "Doar un membru sau un administrator poate marca dosarul ca trimis."
    : r.status === "ready" ? "Descarcă-l și trimite-l contabilului." : "Trimite-l contabilului prin e-mail.";
  return <Task icon="paper-plane-tilt" title={title} body={body}><PackButtons month={r.month} /></Task>;
}

/** "Plată de lămurit": one button per invoice the debit may have paid (one when nothing on the statement proves it: only
 * the supplier's name, or only the amount); the user's pick settles it with the debit. */
export const ambiguousBody = (a: Ambiguous, today: string) =>
  `Plată de ${lei(a.amount)} ${curOf(a.currency)} pe ${dm(a.date, today)} către ${a.counterparty || a.description}. ${a.candidates.length > 1
    ? "Se potrivește cu mai multe facturi. Alege factura plătită."
    : "Extrasul nu dovedește factura plătită. Alege factura dacă aceasta este."}`;

function CandidateButtons({ candidates, tx, today }: { candidates: Ambiguous["candidates"]; tx: string; today: string }) {
  return candidates.map((c) => <Act key={c.id} action="resolve-debit" body={{ tx, target: c.id }} cls="btn g"
    label={`Factura ${c.number}, ${lei(c.amount)} ${curOf(c.currency)}, din ${dm(c.date, today)}`}
    confirm={{ text: `Plata de pe extras devine dovada facturii ${c.number}?`, yes: "Asociază plata" }} />);
}

function CandidatePages({ a, today }: { a: Ambiguous; today: string }) {
  const { co } = useCompany();
  const read = useCallback((cursor: string) => getCandidates(co, a.tx, cursor), [co, a]);
  const page = usePage(read, 0, `${co}|${a.tx}`);
  return <><PageNav page={page} label="Paginare facturi posibile" /><CandidateButtons candidates={page.data} tx={a.tx} today={today} /></>;
}

export function ResolveBtns({ a, today }: { a: Ambiguous; today: string }) {
  return <div className="grid w-full gap-2">
    {(a.candidate_count ?? a.candidates.length) > 50 ? <CandidatePages a={a} today={today} />
      : <CandidateButtons candidates={a.candidates} tx={a.tx} today={today} />}
  </div>;
}

function ReminderText({ t }: { t: ReminderTask }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  return <details className="w-full text-sm [&_p]:my-2">
    <Summary>{t.label}</Summary>
    <p><b>{t.subject}</b></p>
    <p className="whitespace-pre-wrap leading-relaxed">{t.body}</p>
    <Button type="button" className="btn" onClick={async () => {
      try {
        await navigator.clipboard.writeText(t.subject + "\n\n" + t.body);
        setCopied(true); setError(false);
      } catch { setError(true); }
    }}>{copied ? "Copiat" : "Copiază textul"}</Button>
    {error || copied ? <p role="status">{error ? "Textul nu a putut fi copiat. Selectează textul și copiază-l manual." : copied ? "Textul a fost copiat." : ""}</p> : null}
  </details>;
}

/** A credit note that names no invoice and fits several of the supplier's: the person says which one it cancels, or none
 * (credit-pick); until then every one of them stays to pay. */
function creditQuestionCard(s: State, r: Invoice, own: string | undefined): ReactElement {
  const amount = `${lei(r.total.replace("-", ""))} ${curOf(r.currency)}`;
  return <Task co={own} key={`${s.co}cq${r.id}`} icon="warning-circle" title={`${r.supplier}: ce factură anulează stornarea?`}
    body={`Factura de stornare ${r.number}, de ${amount}, nu spune ce factură anulează. Până alegi, nu anulează niciuna.`}>
    <div className="grid w-full gap-2">
      {r.credit_question?.map((c) => <Act key={c.id} action="credit-pick" body={{ id: r.id, target: c.id }} cls="btn g"
        label={`Factura ${c.number}, din ${dm(c.date, s.today)}`}
        confirm={{ text: `Factura ${c.number} nu se mai plătește?`, yes: "Stornează factura" }} />)}
      <Act action="credit-pick" body={{ id: r.id, target: "-" }} cls="btn g" label="Niciuna"
        confirm={{ text: "Stornarea nu anulează niciuna dintre aceste facturi?", yes: "Niciuna" }} />
    </div>
  </Task>;
}

function reminderCard(s: State, t: ReminderTask, own: string | undefined): ReactElement {
  const r = findIssued(s, t.id)?.[0];
  return <Task co={own} key={`${s.co}rem${t.id}`} icon="envelope-simple"
    title={r ? `${r.client}: încasare restantă` : "Încasare restantă"} late
    body="Copiază textul și trimite-l clientului din e-mail.">
    <ReminderText t={t} />
  </Task>;
}

/** The engine's warning about a partner ANAF lists as inactive (or a client it lists as removed) while invoices are still open
 * between the two: its label and detail as the engine words them, with the partner and the date of the check. Keyed on kind and
 * id, since a partner that is both supplier and client can have one of each. */
function partnerCard(s: State, t: PartnerTask, own?: string): ReactElement {
  const p = s.partners.find((x) => x.id === t.id);
  return (
    <Task co={own} key={`${s.co}${t.kind}${t.id}`} icon="warning-circle" title={t.label}
      body={(p ? `${p.name}, v` : "V") + `erificat la ANAF pe ${dm(t.checked_at, s.today)}. ${t.detail}`}>
      {p ? <L to={{ p: t.id }} className="btn g">Vezi partenerul</L> : null}
    </Task>
  );
}

/** A draft's task: its sheet, for whoever may open it (App: members). */
function DraftLink({ co, id }: { co: string; id: string }) {
  return useCan("member") ? <L to={{ co, tab: "vanzari", new: id }} className="btn g">Vezi ciorna</L> : null;
}

/** A connection's task: Setări › Integrări at that kind's section, for whoever can change integrations. */
export function IntegrationLink({ kind, children }: { kind: "anaf" | "mail"; children: ReactNode }) {
  return useCan("manage_integrations") ? <L to={{ tab: "setari", sec: "integrari" }} at={`k-${kind}`} className="btn g">{children}</L> : null;
}

export type TaskOpts = { tag?: boolean };

/** A payment file's task, answered on its sheet: a question about it (an unknown file in its folder is urgent), whether
 * its current download was imported, or that it waits to be downloaded (again, after a change). */
function fileCard(s: State, f: PaymentFile, own?: string): ReactElement {
  const where = f.import_in ?? "aplicația băncii";
  const [title, body, late] = f.question === "orphan" ? [`Un fișier necunoscut lângă ${f.name}`, `Verifică-l înainte să-l imporți în ${where}.`, true]
    : f.question === "missing" ? [`${f.name} a fost importat în ${where}?`, `Fișierul nu mai este în fiscalm.${s.live ? "" : " Răspunzi în Live."}`, false]
    : f.downloaded_at && !f.changed_since_download ? [`${f.name} a fost importat în ${where}?`, `Descărcat pe ${dmt(f.downloaded_at, s.today)}.`, false]
    : [`${f.name}: de descărcat`, `${cap(nOf(f.payment_count, "o plată", "plăți"))}, ${lei(f.total)} ${curOf(f.currency)}. Se importă în ${where}.`, false];
  return <Task co={own} key={`${s.co}pf${f.id}`} icon={late ? "warning-circle" : "tray-arrow-down"} title={title} body={body} late={late}>
    <L to={{ co: s.co, tab: "plati", pf: String(f.id) }} className="btn g">Vezi fișierul</L>
  </Task>;
}

/** A task and whether it is urgent: shown in red only for money overdue and what fails until a person acts (an exported
 * payment for a credited invoice, an unknown payment file, a connection to sign in again, an e-Factura invoice that
 * can't be read and is never read again). Questions, waits and delays that retry
 * on their own are not urgent. The engine's `tasks.references` keeps the same marks. */
export type Card = { node: ReactElement; urgent: boolean };

/** Every task card of a company. tag: Toate firmele,
 * where a company's own card carries its name and is keyed by it (the cards about mail and connections are the same for
 * both). A card is urgent when its Task is `late`, so the red mark and the count can't disagree. */
export function taskCards(s: State, { tag = false }: TaskOpts = {}): Card[] {
  const out: Card[] = [];
  const add = (node: ReactElement) => out.push({ node, urgent: !!(node.props as { late?: boolean }).late });
  const { co, today, live } = s;
  const own = tag ? coName(s) : undefined;
  for (const node of SettlementTasks({ s, co: own })) add(node);
  for (const node of CodTasks({ s, co: own })) add(node);
  // As the engine's tasks.references: a file with a question, or (Live) an open file with payments to download or import.
  for (const f of s.files.filter((f) => f.question || (live && f.status === "open" && f.payment_count > 0))) add(fileCard(s, f, own));
  for (const r of openInvoices(s).filter((r) => r.due_date && r.due_date < today)) {
    const n = -(daysTo(r.due_date, today) ?? 0);
    add(
      <Task co={own} key={`${co}late${r.id}`} icon="warning-circle" title={`${r.supplier}: plată restantă`} late
        body={`${r.number}, restantă de ${nOf(n, "o zi", "zile")}`}>
        <L to={{ inv: r.id }} className="btn g">Vezi factura</L>
      </Task>,
    );
  }
  for (const a of s.ambiguous) {
    add(
      <Task co={own} key={`${co}amb${a.tx}`} icon="warning-circle" title="Plată de lămurit" body={ambiguousBody(a, today)}>
        {a.can_resolve ? <ResolveBtns a={a} today={today} /> : null}
      </Task>,
    );
  }
  for (const r of s.invoices.filter((r) => r.credit_question?.length)) add(creditQuestionCard(s, r, own));
  for (const r of s.invoices.filter((r) => r.amount_changed && r.payment_state !== "imported")) {
    add(<Task co={own} key={`${co}credit${r.id}`} icon="warning-circle" title="Plată în fișier, cu factură de stornare" late
      body="Verifică plata în bancă. Fișierul nu se mai descarcă până lămurești situația.">
      <L to={{ inv: r.id }} className="btn g">Vezi factura</L>
    </Task>);
  }
  for (const x of s.refused) {
    const r = s.invoices.find((i) => i.id === x.id);
    if (!r) continue;
    // The step that confirms the account is on the invoice's sheet: one tap away, with room to say what to check first.
    add(
      <Task co={own} key={`${co}ref${x.id}`} icon="warning-circle" title={`Factura de la ${r.supplier} nu e în fișierul de plăți`}
        body={refusalReason(s, x)}>
        <L to={{ inv: r.id }} className="btn g">Vezi factura</L>
      </Task>,
    );
  }
  for (const t of s.reminder_tasks) add(reminderCard(s, t, own));
  for (const t of s.partner_tasks) add(partnerCard(s, t, own));
  if (s.statement_hold) {
    const { month, count } = s.statement_hold;
    add(
      <Task co={own} key={`${co}stm`} icon="bank" title={`Lipsește extrasul din ${luna(month)}`}
        body={`${cap(nOf(count, "o plată automată așteaptă", "plăți automate așteaptă"))} extrasul, pentru a evita plățile duble. Apoi intră în fișierul de plăți.`}>
        <FirmStatementUpload co={co} />
      </Task>,
    );
  }
  for (const r of s.reports) {
    const name = lunaName(r.month);
    if (r.status === "ready" || r.status === "prepared") {
      // the pack's state names the task, as in Rapoarte: "De pregătit" is prepared by the download, "De trimis" waits for the person
      add(<PackTask key={`pk${r.month}`} r={r} title={`Dosarul pe ${name}: ${REPORT_LABEL[r.status][1].toLowerCase()}`} />);
    }
  }
  for (const m of s.to_check) {
    add(
      <Task key={`m${m.key}`} icon="envelope-simple" title={m.reason === "upload-firm" ? "Pentru ce firmă e documentul?" : "Factură din e-mail de verificat"}
        body={m.reason === "upload-firm" ? "Document încărcat" : `${m.supplier || m.subject}. ${questionLine(m)}`}>
        <L to={{ mail: m.key }} className="btn g">Vezi documentul</L>
      </Task>,
    );
  }
  for (const x of s.recurring_refused) {
    add(<Task co={own} key={`${co}rec${x.id}`} icon="invoice" late title={`Factura lunară pentru ${x.client} nu a fost emisă`} body={x.why}>
      <DraftLink co={co} id={x.id} />
    </Task>);
  }
  for (const task of s.emag_tasks ?? []) {
    add(<Task co={own} key={`emag${task.id}`} icon="warning-circle" title={`Comandă eMAG ${task.external_id}`}
      body={task.reason === "return_review" ? "Verifică returul și, dacă e cazul, stornează factura inițială." : "Verifică comanda sau factura atașată."}>
      <L to={{ co, ord: `${co}:${task.id}` }} className="btn g">Vezi comanda</L>
    </Task>);
  }
  for (const task of s.order_tasks ?? []) {
    if (task.kind !== "order_vat_review" || !task.draft_id) continue;
    add(<Task co={own} key={`${co}ordvat${task.order_id}`} icon="warning-circle" title="Comandă cu TVA 0% de verificat"
      body="Confirmă sau schimbă tratamentul TVA al liniilor cu 0% din ciornă, apoi emite factura.">
      <DraftLink co={co} id={task.draft_id} />
    </Task>);
  }
  for (const node of bankTasks(s, own)) add(node);
  const a = s.system.anaf;
  if (anafExpiringSoon(a)) {
    add(<Task key="anaf-exp" icon="link-simple" title={`Conexiunea ANAF expiră pe ${dm(a.expires, today)}`}
      body="Fără ea nu se mai citesc facturile din e-Factura. Autorizează din nou cu certificatul digital.">
      <IntegrationLink kind="anaf">Reconectează ANAF</IntegrationLink>
    </Task>);
  }
  if (a.status === "error") {
    add(<Task key="anaf-err" icon="link-simple" title="Eroare la citirea facturilor din ANAF" late={a.reconnect}
      body={a.reconnect ? "Pot lipsi facturi noi. Reconectează ANAF cu certificatul digital."
        : (a.last_sync ? `Ultima citire a facturilor: ${dmt(a.last_sync, today)}. ` : "")
        + "Pot lipsi facturi noi. Citirea se reia la următoarea verificare."}>
      <IntegrationLink kind="anaf">{a.reconnect ? "Reconectează ANAF" : "Vezi conexiunea ANAF"}</IntegrationLink>
    </Task>);
  }
  if (a.status === "expired") {
    add(<Task key="anaf-off" icon="link-simple" title="Conexiunea ANAF a expirat" late
      body="Facturile noi din e-Factura nu se mai citesc până la o nouă autorizare cu certificatul digital.">
      <IntegrationLink kind="anaf">Reconectează ANAF</IntegrationLink>
    </Task>);
  }
  if (s.system.mail.stale) {
    add(<Task key="mail" icon="envelope-simple" title="Sincronizare e-mail întârziată" late={s.system.mail.reconnect}
      body={`Ultima citire a mesajelor: ${dmt(s.system.mail.last_ok, today)}. Pot lipsi documente.`}>
      <IntegrationLink kind="mail">Vezi integrarea</IntegrationLink>
    </Task>);
  }
  for (const r of s.unreadable) {
    add(<Task co={own} key={`${co}u${r.id}`} icon="warning-circle" title="Factură e-Factura ilizibilă" late
      body={`${r.listed ? `Primită pe ${dm(r.listed, today)}. ` : ""}Consult-o direct în SPV.`}>
      <a className={`${buttonStyle} ${buttonTone.secondary}`} href={ANAF_PORTAL} target="_blank" rel="noopener noreferrer">Deschide SPV</a>
    </Task>);
  }
  return out;
}

/** Acasă's quick actions, each the flow that already exists elsewhere: Factură nouă (as in Nou), a bon through a receipt link
 * (Bondrop: Setări › Acces, as in Nou), the payments to prepare (Plăți), and the accountant's message for the month that is ready
 * (as on Rapoarte). Under Toate firmele none of them knows the firm: the tiles choose the firm instead, each opening its Acasă. */
/** On a phone a two-by-two grid of tiles, the icon above the name; in a wide window one row of buttons. */
const quickActionStyle = "m-0 flex min-h-[76px] min-w-0 cursor-pointer flex-col items-start justify-between gap-2 rounded-card border border-solid border-line bg-surface p-3 text-left font-[inherit] text-sm font-semibold leading-5 text-ink no-underline transition-colors hover:bg-inset focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none min-[1100px]:min-h-[38px] min-[1100px]:flex-row min-[1100px]:items-center min-[1100px]:justify-start min-[1100px]:rounded-full min-[1100px]:px-4 min-[1100px]:py-2 min-[1100px]:text-[13px] min-[1100px]:font-medium [&_b]:grid [&_b]:size-9 [&_b]:place-items-center [&_b]:rounded-card [&_b]:bg-muted/12 [&_b]:text-ink min-[1100px]:[&_b]:size-auto min-[1100px]:[&_b]:bg-transparent [&_svg]:size-5 min-[1100px]:[&_svg]:size-4";
// an odd last tile spans the row (the upload's hidden input is not a tile)
const quickActionsStyle = "my-0 grid grid-cols-2 gap-2 min-[1100px]:flex min-[1100px]:flex-wrap [&>:nth-child(odd_of_:not(input)):last-child]:col-span-2";

export function QuickActions() {
  const { view } = useApp();
  const allowed = useCan("member");
  if (!allowed) return null;
  if (isAll(view)) {
    return (
      <nav aria-labelledby="pick-co">
        <p id="pick-co" className="mt-0 mb-2 text-[13px] text-muted">Alege firma</p>
        <div className={quickActionsStyle}>
          {parts(view).map((s) => <L key={s.co} className={quickActionStyle} to={{ co: s.co, tab: "home" }}><b><I n="bank" /></b>{coName(s)}</L>)}
        </div>
      </nav>
    );
  }
  return <CompanyActions />;
}

function CompanyActions() {
  const s = useCompany();
  const rp = packToSend(s);
  return (
    <div className={quickActionsStyle}>
      <NewInvoice className={quickActionStyle}><b><I n="plus" /></b>Factură nouă</NewInvoice>
      <UploadTile />
      <L className={quickActionStyle} to={{ tab: "cheltuieli" }}><b><I n="list-checks" /></b>Pregătește plățile</L>
      {rp ? <PackDownload month={rp.month} className={quickActionStyle}><b><I n="paper-plane-tilt" /></b>Dosar pentru contabil</PackDownload> : null}
    </div>
  );
}

/** "Încarcă bon sau factură" as a quick action (ExpenseUpload.tsx). */
function UploadTile() {
  const up = useExpenseUpload();
  return up ? (
    <>
      {up.input}
      <button type="button" className={quickActionStyle} disabled={up.busy} aria-busy={up.busy} onClick={up.pick}>
        <b><I n="camera" /></b>{up.busy ? "Se încarcă…" : UPLOAD_LABEL}
      </button>
    </>
  ) : null;
}

/** A card sees only its inputs, rather than rebuilding every other task from the legacy full-state reader. */
function cardState(source: State, patch: TaskEntry["patch"]): State {
  const empty = Object.fromEntries(Object.entries(source).filter(([, v]) => Array.isArray(v)).map(([k]) => [k, []]));
  return {
    ...source, ...empty, companies: source.companies,
    statement_hold: null,
    cod: source.cod ? { ...source.cod, tasks: [], rows: [] } : undefined,
    system: { ...source.system, anaf: { ...source.system.anaf, status: "not_set_up" }, mail: { ...source.system.mail, stale: false } },
    ...patch,
  };
}

/** Render one server-selected card using only its bounded inputs. */
export function taskCardNode(source: State, entry: TaskEntry, opts: TaskOpts = {}): ReactNode {
  const state = cardState(source, entry.patch);
  const card = taskCards(state, opts).find((c) => String(c.node.key) === entry.key);
  return card ? <Company key={entry.key} s={state}>{card.node}</Company> : null;
}

/** A highlighted task as Acasă lists it: one line, its title and what it says, red when urgent. */
export type TaskLine = { key: string; title: string; body: string; late: boolean };

/** Complete server counts and the already-selected three task highlights, each as its card would say it. */
export function homeTasks(view: View): { total: number; brief: TaskLine[]; urgent: number } {
  const ss = parts(view);
  const { total, urgent, highlights } = view.task_summary;
  return { total, urgent, brief: highlights.flatMap((entry) => {
    const source = ss.find((s) => s.co === entry.company);
    const node = source ? taskCards(cardState(source, entry.patch), { tag: ss.length > 1 }).find((c) => String(c.node.key) === entry.key)?.node : null;
    if (!node) return [];
    const p = node.props as { title?: string; body?: string; late?: boolean; co?: string };
    return [{ key: entry.key, title: p.title ?? "", body: p.co ? `${p.co}. ${p.body ?? ""}` : p.body ?? "", late: !!p.late }];
  }) };
}

/** Necesită atenție: the three tasks the engine picked, a line each opening Sarcini where they are answered, and the way to
 * all of them; nothing when there is nothing to do. */
export function Attention({ brief, all, urgent }: { brief: TaskLine[]; all: number; urgent: number }) {
  if (!all) return null;
  return (
    <section className={`${surfaceStyle} mt-6 py-1`} aria-labelledby="atentie">
      <div className="flex items-center justify-between gap-3 px-4 pt-2.5 pb-1.5">
        <h2 id="atentie" className="m-0 flex items-center gap-2 text-sm font-semibold">Necesită atenție
          {urgent ? <span className="rounded-full bg-danger/12 px-2 py-px text-[11.5px] font-semibold text-danger">{nOf(urgent, "una urgentă", "urgente")}</span> : null}</h2>
        <L to={{ tab: "sarcini" }} className="text-[13px] text-muted no-underline hover:text-ink">{`Toate (${all})`}</L>
      </div>
      <ul className="asks m-0 list-none p-0">
        {brief.map((t) => <li key={t.key} className="task border-0 border-t border-solid border-line">
          <L to={{ tab: "sarcini" }} className="flex min-h-12 items-center gap-3 px-4 py-2 text-[13px] text-ink no-underline hover:bg-inset">
            <i aria-hidden="true" className={`ic size-2 shrink-0 rounded-full ${t.late ? "late bg-danger" : "bg-wait"}`} />
            <span className="h min-w-0 flex-1"><b className="block truncate font-medium">{t.title}</b><span className="block truncate text-[12.5px] text-muted">{t.body}</span></span>
            <span className="flex text-muted [&_svg]:size-4"><I n="caret-right" /></span>
          </L>
        </li>)}
      </ul>
    </section>
  );
}

/** Acasă's title, with today's date under it, on the phone and in a wide window alike. */
export function HomeTitle({ className = "" }: { className?: string }) {
  const { view } = useApp();
  return (
    <div className={className}>
      <Title className="mb-0!">Acasă</Title>
      <p className={`${leadStyle} mb-0!`}>{weekday(view.today)}</p>
    </div>
  );
}

/** Acasă on the phone: quick actions, what is left to set up, the tasks that need a person, then the firm's numbers. */
export function Home() {
  const { view } = useApp();
  const { total, brief, urgent } = homeTasks(view);
  return (
    <div className="home pb-4">
      <HomeTitle className="mb-5" />
      <QuickActions />
      <SetupCard />
      <Attention brief={brief} all={total} urgent={urgent} />
      <Dashboard />
    </div>
  );
}
