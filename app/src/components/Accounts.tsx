"use client";
import { Actions, Button, Form, Input, Label, Panel, Select, noteLeftStyle, sectionHeadingStyle, statusStyle, statusTextStyle, legendStyle } from "./Controls";
// A company's bank accounts as Tranzacții shows them beside its statements: one line per account with its balance and when
// it was last read, a bank connection ending or ended with its Reconectează, a new account; and the page's Încarcă, which
// takes a statement (the engine tells the account from the file; a CSV asks) or a settlement report.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { post, postStatement, postStatements, statementType, type CsvPreview, type StatementResult } from "@/lib/api";
import { cap, curOf, dm, lei, luna, type Tone } from "@/lib/format";
import type { BankStatus, CompanyAccount, CompanyEntry, Dashboard } from "@/lib/types";
import { ReportUpload } from "./ReportUpload";
import { ConnectBank } from "./BankConnect";
import { useCompanies } from "./Companies";
import { useApp, useCan } from "./context";
import { CloseX, Sheet } from "./Sheets";
import { I, L, ROW_TINT, Row } from "./ui";

export type Balance = Dashboard["balances"][number];

const accountName = (a: CompanyAccount) => `${cap(curOf(a.currency))}, cont ${a.iban_last4}`;

/** Under an account's name: its bank, whether payments leave from it, and when it was last read (by the bank connection
 * that reads it, else its latest statement). */
function accountLine(a: CompanyAccount, bank: BankStatus | undefined, today: string): string {
  if (a.retired) return `Înlocuit pe ${a.retired.split("-").reverse().join(".")}`;
  const c = bank?.connection;
  const read = c?.last_read && c.accounts.includes(a.iban_last4) ? `citit pe ${dm(c.last_read, today)}`
    : a.last_statement ? `extras ${luna(a.last_statement)}` : "niciun extras";
  return [a.bank_name || "Altă bancă", a.pays ? "pentru plăți" : "", read].filter(Boolean).join(", ");
}

/** An account's balance, only when its currency and last four digits name one current account (two that share them get
 * none, rather than each other's). */
function balanceOf(a: CompanyAccount, accounts: CompanyAccount[], balances: Balance[]): Balance | undefined {
  const same = (x: { currency: string; iban_last4: string }) => x.currency === a.currency && x.iban_last4 === a.iban_last4;
  if (a.retired || accounts.filter((x) => !x.retired && same(x)).length !== 1) return undefined;
  const found = balances.filter(same);
  return found.length === 1 ? found[0] : undefined;
}

/** A connection that needs a person: [tone, text], or null while it reads. */
function connectionNeed(b: BankStatus, today: string): [Tone, string] | null {
  const c = b.connection;
  if (!c || c.state === "ok") return null;
  if (c.state === "expiring") return ["soon", `${b.name}: accesul expiră pe ${dm(c.valid_until, today)}`];
  if (c.state === "expired") return ["late", `${b.name}: accesul a expirat`];
  return ["soon", `${b.name}: ${c.error ?? "ultima citire nu a reușit"}`];
}

/** The company's accounts, one line each; a bank connection ending or ended, with Reconectează for an administrator (the
 * same bank-connect as Setări, Firme: it opens the bank to confirm read-only access); and, for an administrator, the way to
 * Bănci și conturi, where accounts are added. */
export function Accounts({ c, accounts, manage, balances = [] }: {
  c: CompanyEntry; accounts: CompanyAccount[]; manage: boolean; balances?: Balance[];
}) {
  const { view } = useApp();
  return (
    <section aria-labelledby={`acc-${c.key}`}>
      <div className={sectionHeadingStyle}><h2 id={`acc-${c.key}`}>Conturi</h2></div>
      {(c.banks ?? []).map((b) => {
        const need = connectionNeed(b, view.today);
        if (!need) return null;
        const again = manage && b.connect === "ready" && b.connection?.state !== "error";
        return (
          <div key={b.bank} className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-control bg-inset px-4 py-3">
            <span className={`${statusTextStyle} ${statusStyle[need[0]]}`}>{need[1]}</span>
            {again ? <ConnectBank co={c.key} bank={b.bank} again /> : null}
          </div>
        );
      })}
      <Panel padded={false}>
        {/* the order the engine gives: two accounts may share their last digits and currency */}
        {accounts.map((a, n) => {
          const bal = balanceOf(a, accounts, balances);
          return <Row key={n} icon="bank" title={accountName(a)} sub={accountLine(a, c.banks?.find((b) => b.bank === a.bank), view.today)}
            val={bal?.balance != null ? `${lei(bal.balance)} ${curOf(a.currency)}` : ""} vsub={bal?.day ? `la ${dm(bal.day, view.today)}` : ""} />;
        })}
        {!accounts.length ? <p className={`${noteLeftStyle} px-3 py-2`}>Firma nu are încă un cont bancar.</p> : null}
      </Panel>
      {manage ? <p className={`${noteLeftStyle} mt-3`}>Conturile se adaugă în <L to={{ co: c.key, tab: "setari", sec: "firme" }} at={`conn-${c.key}`}
        className="font-semibold text-ink underline">Bănci și conturi</L>.</p> : null}
    </section>
  );
}

/** Tranzacții's Încarcă, for an administrator: a sheet with Încarcă extras and Încarcă raport de decontare. The report
 * keeps its own flow and every check (ReportUpload); only where it starts moved here. */
export function UploadMenu({ c, accounts, reload, label = "Încarcă" }: { c: CompanyEntry; accounts: CompanyAccount[]; reload: () => void; label?: string }) {
  const [open, setOpen] = useState<"" | "menu" | "report">("");
  const close = () => setOpen("");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (open === "report") heading.current?.focus(); }, [open]); // the menu button it replaces had focus
  return (
    <>
      <Button type="button" variant="secondary" aria-haspopup="dialog" aria-expanded={!!open} onClick={() => setOpen("menu")}>{label}</Button>
      {open ? createPortal(
        <Sheet label={open === "report" ? "Încarcă raport de decontare" : "Încarcă"} onClose={close}>
          <CloseX onClose={close} />
          {open === "report" ? <>
            <h2 className="m-0 text-xl font-semibold focus:outline-none" tabIndex={-1} ref={heading}>Încarcă raport de decontare</h2>
            <ReportUpload co={c.key} reload={() => { reload(); close(); }} />
          </> : <div className="grid gap-2 [&_.btn]:w-full [&_.btn]:justify-start">
            <StatementUpload co={c.key} accounts={accounts.filter((a) => !a.retired)} reload={reload} />
            <Button type="button" className="btn g" onClick={() => setOpen("report")}><I n="file-text" />Încarcă raport de decontare</Button>
          </div>}
        </Sheet>, document.body) : null}
    </>
  );
}

const NOT_STATEMENT = "Se acceptă extrase PDF, CAMT (XML), MT940 sau CSV.";
const SEVERAL_PDFS = "Mai multe fișiere se încarcă împreună doar ca extrase PDF ale aceleiași luni.";
const DATE_FORMATS: [string, string][] = [["%d.%m.%Y", "31.10.2026"], ["%Y-%m-%d", "2026-10-31"], ["%d/%m/%Y", "31/10/2026"],
  ["%d-%m-%Y", "31-10-2026"], ["%m/%d/%Y", "10/31/2026"]];

/** "Încarcă extras": the file picked goes to the engine as it is, for this company and no bank named; the engine reads its
 * type and accounts from the content, refuses another company's, and imports it like a statement from mail. Only a CSV
 * names no account, so it goes on one (asked when the company has several); its first upload brings back its columns, and
 * the person says which is which, once per account. Several PDFs picked at once are one month's BT
 * statement in parts (one per account, or daily): the engine imports them together or not at all. */
/** The statement upload on the firm's current accounts, for a person who manages firms; nothing for anyone else. */
export function FirmStatementUpload({ co }: { co: string }) {
  const { data, reload } = useCompanies();
  const manage = useCan("manage_firms");
  const c = data?.companies.find((c) => c.key === co);
  return manage && c ? <StatementUpload co={co} accounts={(c.accounts ?? []).filter((a) => !a.retired)} reload={reload} /> : null;
}

export function StatementUpload({ co, accounts, reload }: { co: string; accounts: CompanyAccount[]; reload: () => void }) {
  const { busy, run } = useApp();
  const input = useRef<HTMLInputElement>(null);
  const [csv, setCsv] = useState<{ file: File; account: string; preview: CsvPreview | null } | null>(null);
  const [said, setSaid] = useState<{ ok: boolean; message: string } | null>(null);
  const upload = async (f: File, account = "") => {
    setSaid(null);
    const type = statementType(f);
    const r = await run<StatementResult>(co, async () => (type ? postStatement(co, "", f, type, account)
      : { ok: false, message: NOT_STATEMENT, next: null, done: null, state: null }), undefined, () => false);
    reload();
    if (r && !r.ok && "csv" in r && r.csv) return setCsv({ file: f, account, preview: r.csv });
    setCsv(null);
    if (r?.message) setSaid({ ok: r.ok, message: r.message });
  };
  const picked = async (files: File[]) => {
    if (input.current) input.current.value = "";
    const f = files[0];
    if (!f) return;
    if (files.length > 1) { // one month's BT PDFs, one per account or one per day: imported together or not at all
      setSaid(null);
      setCsv(null);
      const pdfs = files.every((x) => statementType(x) === "application/pdf");
      const r = await run<StatementResult>(co, async () => (pdfs ? postStatements(co, files)
        : { ok: false, message: SEVERAL_PDFS, next: null, done: null, state: null }), undefined, () => false);
      reload();
      if (r?.message) setSaid({ ok: r.ok, message: r.message });
      return;
    }
    const isCsv = statementType(f) === "text/csv";
    if (isCsv && accounts.length > 1) return setCsv({ file: f, account: "", preview: null });
    await upload(f, isCsv ? accounts[0]?.id ?? "" : "");
  };
  const preview = csv?.preview;
  return (
    <>
      <Button type="button" className="btn" disabled={busy} onClick={() => input.current?.click()}><I n="bank" />Încarcă extras</Button>
      <Input ref={input} type="file" hidden accept=".pdf,.xml,.sta,.mt940,.940,.txt,.csv,application/pdf,application/xml,text/xml,text/csv"
        multiple onChange={(e) => picked(Array.from(e.target.files ?? []))} tabIndex={-1} aria-hidden="true" />
      {/* the engine's answer stays beside the button until the next upload; the toast alone was gone before it was read */}
      {said ? <div className="flex items-start gap-3 rounded-control bg-inset p-4 text-sm leading-relaxed" role={said.ok ? "status" : "alert"}>
        <span className={`grid size-9 shrink-0 place-items-center rounded-control [&_svg]:size-4 ${ROW_TINT[said.ok ? "ok" : "late"]}`}>
          <I n={said.ok ? "check-circle" : "warning-circle"} /></span>
        <p className="m-0 min-w-0 self-center">{said.message}</p>
      </div> : null}
      {csv && !csv.preview ? (
        <Form className="mt-2 w-full min-w-0" aria-label="Contul extrasului CSV"
          onSubmit={(e) => { e.preventDefault(); if (csv.account) upload(csv.file, csv.account); }}>
          <Label htmlFor={`csv-acc-${co}`}>Contul acestui extras CSV</Label>
          <Select id={`csv-acc-${co}`} value={csv.account} onChange={(e) => setCsv({ ...csv, account: e.target.value })}>
            <option value="">Alege contul</option>
            {accounts.map((a) => <option key={a.id} value={a.id}>{`${accountName(a)}, ${a.bank_name || "Altă bancă"}`}</option>)}
          </Select>
          <Actions>
            <Button type="submit" className="btn g" disabled={busy || !csv.account}>Încarcă pe acest cont</Button>
            <Button type="button" className="btn g" onClick={() => setCsv(null)}>Renunță</Button>
          </Actions>
        </Form>
      ) : null}
      {csv && preview ? <CsvColumns co={co} preview={preview} onCancel={() => setCsv(null)}
        onSaved={() => upload(csv.file, preview.account)} /> : null}
    </>
  );
}

/** Which CSV column is what, picked once for an account, from its columns and first rows. What the engine read from the
 * whole file (a chosen date column's format, a chosen amount column's decimal separator) is not asked; it checks the rest. */
function CsvColumns({ co, preview, onSaved, onCancel }: {
  co: string; preview: CsvPreview; onSaved: () => void; onCancel: () => void;
}) {
  const { busy, run } = useApp();
  const { dates, decimals } = preview.detected;
  // the columns are the person's to say (a value date or a balance beside the right one reads just as well); only how
  // the chosen ones are written is taken from the file
  const [m, setM] = useState<Record<string, string>>({ date_format: "%d.%m.%Y", decimal: "," });
  const [split, setSplit] = useState(false);
  const knownFormat = dates[m.date];
  const amounts = (split ? [m.debit, m.credit] : [m.amount]).map((c) => decimals[c ?? ""]);
  const knownDecimal = amounts.every((d) => d && d === amounts[0]) ? amounts[0] : undefined;
  const id = (k: string) => `csv-${preview.account}-${k}`;
  const pick = (k: string, label: string, optional = false) => (
    <>
      <Label htmlFor={id(k)}>{label}{optional ? " (opțional)" : ""}</Label>
      <Select id={id(k)} value={m[k] ?? ""} onChange={(e) => setM({ ...m, [k]: e.target.value })}>
        <option value="">{optional ? "Nu are" : "Alege coloana"}</option>
        {preview.columns.map((c) => <option key={c} value={c}>{c}</option>)}
      </Select>
    </>
  );
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const known = { date_format: knownFormat ?? m.date_format, decimal: knownDecimal ?? m.decimal };
    const body = Object.fromEntries(Object.entries({ ...m, ...known }).filter(([k, v]) => v && (split ? k !== "amount" : !["debit", "credit"].includes(k))));
    const r = await run(co, () => post("statement-csv-format", { co, account: preview.account, ...body }), undefined, () => false);
    if (r?.ok) onSaved();
  };
  return (
    <Form className="mt-4! w-full min-w-0" onSubmit={save} aria-label="Coloanele extrasului CSV">
      <p className={noteLeftStyle}>Coloanele alese se păstrează pentru următoarele extrase.</p>
      <div className="my-4 max-w-full overflow-auto rounded-control border border-solid border-line focus-visible:outline-2 focus-visible:outline-focus [&_table]:w-full [&_table]:border-collapse [&_th]:bg-inset [&_th]:p-3 [&_th]:text-left [&_td]:p-3 [&_td]:text-sm [&_td]:whitespace-nowrap" role="region" aria-label="Primele rânduri din fișier" tabIndex={0}>
        <table>
          <thead><tr>{preview.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
          <tbody>{preview.rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j}>{v}</td>)}</tr>)}</tbody>
        </table>
      </div>
      {pick("date", "Data")}
      {knownFormat ? null : <>
        <Label htmlFor={id("date_format")}>Formatul datei</Label>
        <Select id={id("date_format")} value={m.date_format} onChange={(e) => setM({ ...m, date_format: e.target.value })}>
          {DATE_FORMATS.map(([f, example]) => <option key={f} value={f}>{example}</option>)}
        </Select>
      </>}
      <fieldset className="m-0 mt-4 min-w-0 border-0 p-0">
        <legend className={legendStyle}>Suma</legend>
        <Label className="cursor-pointer"><Input type="radio" name={id("mode")} checked={!split} onChange={() => setSplit(false)} /> O coloană, cu minus la plăți</Label>
        <Label className="cursor-pointer"><Input type="radio" name={id("mode")} checked={split} onChange={() => setSplit(true)} /> Coloane separate pentru debit și credit</Label>
      </fieldset>
      {split ? <>{pick("debit", "Debit (plăți)")}{pick("credit", "Credit (încasări)")}</> : pick("amount", "Suma")}
      {knownDecimal ? null : <>
        <Label htmlFor={id("decimal")}>Separatorul zecimal</Label>
        <Select id={id("decimal")} value={m.decimal} onChange={(e) => setM({ ...m, decimal: e.target.value })}>
          <option value=",">Virgulă (1.250,50)</option>
          <option value=".">Punct (1,250.50)</option>
        </Select>
      </>}
      {pick("description", "Detaliile plății")}
      {pick("counterparty", "Plătitor / beneficiar", true)}
      {pick("counterparty_iban", "IBAN partener", true)}
      {pick("reference", "Referința băncii", true)}
      <Actions>
        <Button type="submit" className="btn" disabled={busy}>Salvează coloanele și încarcă</Button>
        <Button type="button" className="btn g" onClick={onCancel}>Renunță</Button>
      </Actions>
    </Form>
  );
}
