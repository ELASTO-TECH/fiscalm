"use client";
import { Button, Input, Select, Label, Actions, formTypography, legendStyle } from "./Controls";
import { useEffect, useRef, useState } from "react";
import { base, type StatementResult } from "@/lib/api";
import { useApp } from "./context";
import { Off, Summary } from "./ui";

/** A payer the firm saved for another header: what a new format from the same courier or processor starts from. */
type SavedPayer = { source: string; name: string; cui: string; currency: string; courier: string };
/** The engine's reading of a report it can't place yet: its header and first rows, the payers saved for this exact header
 * (`known`) or for others (`payers`), the currencies in use, and what the file says (`guess`, `date`, `payer`, `source`,
 * `currency`), which only pre-fill the form. */
export type ReportPreview = { columns: string[]; rows: string[][]; known: { id: string; name: string; source: string; company_cui_column: string }[];
  guess: Record<string, string>; date: string; orders: boolean; payer: string; source: string; currency: string;
  currencies: string[]; payers: SavedPayer[] };
const SOURCES = [["fan", "FAN Courier"], ["sameday", "Sameday"], ["cargus", "Cargus"], ["dpd", "DPD"], ["gls", "GLS"],
  ["stripe", "Stripe"], ["netopia", "Netopia"], ["payu", "PayU"], ["courier", "Alt curier"], ["processor", "Alt procesator"]];
export const REPORT_FIELDS = [["reference", "Numărul facturii"], ["order", "Numărul comenzii"], ["awb", "AWB"], ["gross", "Suma încasată de la client"],
  ["fee", "Comisionul reținut"], ["net", "Suma netă"], ["batch", "Referința decontării"], ["date", "Data fiecărei plăți"], ["currency", "Moneda"], ["category", "Categoria operațiunii (Stripe)"], ["company_cui", "CUI-ul firmei (opțional)"]];
const SHOP = new Set(["order", "awb"]);
const REQUIRED = new Set(["reference", "order", "awb", "gross", "fee"]);
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** What still keeps "Încarcă raportul" off, first thing first; "" when the form can be sent. */
function missing(p: ReportPreview, day: string, partner: string, f: { source: string; name: string; courier: string; currency: string; mapping: Record<string, string> }) {
  const m = f.mapping;
  if (!day) return "Alege data plății.";
  if (partner) return "";
  if (!f.source) return "Alege curierul sau procesatorul.";
  if (!f.name.trim()) return "Scrie numele partenerului din extras.";
  if (!/^[A-Z]{3}$/.test(f.currency)) return "Scrie moneda raportului, de exemplu RON.";
  if (!m.reference && !m.order && !m.awb) return p.orders ? "Alege coloana cu factura, comanda sau AWB-ul." : "Alege coloana cu numărul facturii.";
  if (!m.gross) return "Alege coloana cu suma încasată.";
  if (!m.fee) return "Alege coloana cu comisionul.";
  return f.source === "courier" && m.awb && !f.courier.trim() ? "Scrie numele curierului din comenzi." : "";
}

/** "Încarcă raport de decontare" (Tranzacții, Încarcă): choosing the file reads its columns; the form opens with what the
 * engine guessed, and only the rest left to choose. Same upload as statements; its report flag tells partner tables from bank CSVs. */
export function ReportUpload({ co, reload }: { co: string; reload: () => void }) {
  const { busy, run, view } = useApp();
  const input = useRef<HTMLInputElement>(null);
  const partnerField = useRef<HTMLButtonElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ReportPreview | null>(null);
  const [day, setDay] = useState("");
  const [partner, setPartner] = useState("");
  const [source, setSource] = useState("");
  const [name, setName] = useState("");
  const [courier, setCourier] = useState("");
  const [cui, setCui] = useState("");
  const [currency, setCurrency] = useState("RON");
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const filled = useRef<SavedPayer | null>(null);
  useEffect(() => { if (preview) partnerField.current?.focus(); }, [preview]);
  const fields = REPORT_FIELDS.filter(([key]) => preview?.orders || !SHOP.has(key));
  const shown = fields.filter(([key]) => REQUIRED.has(key) || preview?.guess[key]);
  const more = fields.filter((f) => !shown.includes(f));
  const why = preview ? missing(preview, day, partner, { source, name, courier, currency, mapping }) : "";
  const send = async (f: File, query: Record<string, string>) => {
    const q = new URLSearchParams({ co, report: "1", ...query });
    const r = await run(co, async () => {
      const response = await fetch(`${base()}api/statement-upload?${q}`, { method: "POST", body: f,
        headers: { "Content-Type": /\.xlsx$/i.test(f.name) ? XLSX : "text/csv" } });
      return await response.json() as StatementResult & { report?: ReportPreview };
    }, undefined, () => false);
    if (r && "report" in r && r.report) {
      const p = r.report as ReportPreview;
      setPreview(p); setDay(p.date); setPartner(p.payer); setMapping(p.guess); setCurrency(p.currency || "RON");
      pick(p, p.source, p.currency || "RON");
    }
    if (r?.ok) { setFile(null); setPreview(null); reload(); }
  };
  /** Choosing the courier or processor fills in the payer the firm already saved for it, when there is exactly one;
   * another choice clears each value the earlier fill wrote and the person left as it was, never what they typed. */
  const pick = (p: ReportPreview, id: string, shown = currency) => {
    setSource(id);
    const was = filled.current, saved = p.payers.filter((x) => x.source === id), next = saved.length === 1 ? saved[0] : null;
    filled.current = next;
    const keep = (value: string, old: string | undefined, empty = "") => (was && value === old ? empty : value);
    setName(next ? next.name : keep(name, was?.name)); setCui(next ? next.cui : keep(cui, was?.cui));
    setCourier(next ? next.courier : keep(courier, was?.courier));
    if (!p.currency) setCurrency(next?.currency || keep(shown, was?.currency, "RON"));
  };
  const upload = () => {
    if (!file) return;
    const format = () => JSON.stringify({ source, name: name.trim(), cui, currency, mapping, ...(courier.trim() ? { courier: courier.trim() } : {}) });
    void send(file, partner ? { date: day, partner } : { date: day, format: format() });
  };
  const column = ([key, label]: string[]) => <div key={key}>
    <Label htmlFor={`report-${co}-${key}`}>{label}</Label>
    <Select id={`report-${co}-${key}`} value={mapping[key] ?? ""} onChange={(e) => {
      const next = { ...mapping }; if (e.target.value) next[key] = e.target.value; else delete next[key]; setMapping(next);
    }}>
      <option value="">Alege coloana</option>
      {preview!.columns.map((c) => <option key={c} value={c}>{c}</option>)}
    </Select>
  </div>;
  return <div className={`mt-4 min-w-0 space-y-4 ${formTypography}`}>
      <Button type="button" className={preview ? "btn g" : "btn"} disabled={busy} onClick={() => input.current?.click()}>
        {file ? "Alege alt fișier" : "Alege fișierul (CSV sau XLSX)"}</Button>
      {file ? <p className="wrap-anywhere text-sm text-muted">{file.name}</p> : null}
      <Input ref={input} type="file" hidden accept=".csv,.xlsx" aria-label="Raport de decontare" onChange={(e) => {
        const f = e.target.files?.[0] ?? null;
        e.target.value = ""; setFile(f); setPreview(null); setPartner(""); setMapping({}); setDay("");
        if (f) void send(f, {});
      }} />
      {preview ? <>
        <Label htmlFor={`report-day-${co}`}>Data plății din raport</Label>
        <Input id={`report-day-${co}`} type="date" value={day} max={view.today} onChange={(e) => setDay(e.target.value)} />
        <Label htmlFor={`report-partner-${co}`}>Partenerul care a trimis raportul</Label>
        <Select ref={partnerField} id={`report-partner-${co}`} value={partner} onChange={(e) => setPartner(e.target.value)}>
          <option value="">Format nou</option>
          {preview.known.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select>
        {!partner ? <>
          <Label htmlFor={`report-source-${co}`}>Curier sau procesator</Label>
          <Select id={`report-source-${co}`} value={source} onChange={(e) => pick(preview, e.target.value)}>
            <option value="">Alege curierul sau procesatorul</option>{SOURCES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </Select>
          {source === "courier" && mapping.awb ? <>
            <Label htmlFor={`report-courier-${co}`}>Numele curierului din comenzi</Label>
            <Input id={`report-courier-${co}`} value={courier} onChange={(e) => setCourier(e.target.value)} maxLength={100} />
          </> : null}
          <Label htmlFor={`report-name-${co}`}>Numele partenerului din extras</Label>
          <Input id={`report-name-${co}`} value={name} onChange={(e) => {
            setName(e.target.value); // another name is another payer: the CUI filled in for the saved one goes
            if (filled.current && e.target.value !== filled.current.name && cui === filled.current.cui) setCui("");
          }} maxLength={200} />
          <div className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
            <div><Label htmlFor={`report-cui-${co}`}>CUI-ul partenerului (opțional)</Label>
              <Input id={`report-cui-${co}`} value={cui} onChange={(e) => setCui(e.target.value)} maxLength={12} /></div>
            <div><Label htmlFor={`report-cur-${co}`}>Moneda raportului</Label>
              <Input id={`report-cur-${co}`} list={`report-curs-${co}`} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} maxLength={3} />
              <datalist id={`report-curs-${co}`}>{preview.currencies.map((c) => <option key={c} value={c} />)}</datalist></div>
          </div>
          <fieldset className="m-0 mt-5 min-w-0 border-0 p-0">
            <legend className={legendStyle}>Coloanele raportului</legend>
            <div className="grid gap-x-4 gap-y-1 sm:grid-cols-2">{shown.map(column)}</div>
            {more.length ? <details className="mt-3">
              <Summary>Alte detalii</Summary>
              <div className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-2">{more.map(column)}</div>
            </details> : null}
          </fieldset>
        </> : null}
        <div className="max-w-full overflow-auto rounded-control border border-solid border-line [&_table]:w-full [&_table]:border-collapse [&_table]:text-xs [&_th]:bg-inset [&_th]:font-semibold [&_td]:border-0 [&_td]:border-t [&_td]:border-solid [&_td]:border-line [&_th]:px-3 [&_th]:py-3 [&_td]:px-3 [&_td]:py-3 [&_th]:text-left [&_td]:text-left [&_th]:whitespace-nowrap [&_td]:whitespace-nowrap" role="region" aria-label="Primele rânduri din raport" tabIndex={0}><table>
          <thead><tr>{preview.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>{preview.rows.map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j}>{v}</td>)}</tr>)}</tbody>
        </table></div>
        <Actions>{why ? <Off cls="btn" why={why}>Încarcă raportul</Off>
          : <Button type="button" disabled={busy} onClick={upload}>Încarcă raportul</Button>}</Actions>
      </> : null}
  </div>;
}
