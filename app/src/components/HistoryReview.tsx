"use client";
import { Button, Input, Select, Label, noteLeftStyle } from "./Controls";
import { useEffect, useState } from "react";
import { getHistoryReview, historyDocument, post } from "@/lib/api";
import type { CompanyEntry, HistoryReviewRow } from "@/lib/types";
import { nOf } from "@/lib/format";
import { useApp } from "./context";

export function HistoryReview({ company, reload }: { company: CompanyEntry; reload: () => void }) {
  const { view } = useApp();
  const [rows, setRows] = useState<HistoryReviewRow[]>([]);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    getHistoryReview(company.key).then((r) => { if (active) { setRows(r.rows); setFailed(false); } })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [company, view]);
  if (failed) return <p role="status">Documentele de verificat nu s-au putut încărca.</p>;
  if (!rows.length) return null;
  return <section aria-label="Documente de verificat">
    <h4 className="m-0 text-sm font-semibold">{nOf(rows.length, "Un document de verificat", "documente de verificat")}</h4>
    <p className={noteLeftStyle}>Completează doar câmpurile nesigure. Când confirmi, factura rămâne în istoric. Plata rămâne neconfirmată.</p>
    <div className="overflow-x-auto"><table className="w-full border-collapse text-sm [&_td]:border-0 [&_td]:border-b [&_td]:border-solid [&_td]:border-line [&_td]:p-2.5 [&_td]:align-top [&_td:first-child]:[overflow-wrap:anywhere] [&_th]:border-0 [&_th]:border-b [&_th]:border-solid [&_th]:border-line [&_th]:p-2.5 [&_th]:text-left [&_th]:align-top max-[600px]:[&_thead]:sr-only max-[600px]:[&_tr]:block max-[600px]:[&_tr]:border-0 max-[600px]:[&_tr]:border-b max-[600px]:[&_tr]:border-solid max-[600px]:[&_tr]:border-line max-[600px]:[&_td]:block max-[600px]:[&_td]:border-0 max-[600px]:[&_td]:px-0">
      <thead><tr><th scope="col">Document</th><th scope="col">Datele facturii</th><th scope="col">Verificare</th></tr></thead>
      <tbody>{rows.map((row) => <ReviewRow key={row.sha} row={row} company={company} reload={reload} />)}</tbody>
    </table></div>
  </section>;
}

function ReviewRow({ row, company, reload }: { row: HistoryReviewRow; company: CompanyEntry; reload: () => void }) {
  const { view, busy, run } = useApp();
  const [data, setData] = useState({ target: company.key, direction: row.direction, number: row.number,
    issue_date: row.issue_date, total: row.total, currency: row.currency || "RON", party_name: row.party_name, party_tax_id: row.party_tax_id });
  const change = (key: keyof typeof data, value: string) => setData((old) => ({ ...old, [key]: value }));
  const save = async (discard: boolean) => {
    await run(company.key, () => post("history-review", { co: company.key, sha: row.sha, ...data,
      direction: discard ? "none" : data.direction }), undefined, () => false);
    reload();
  };
  const field = (key: keyof typeof data, label: string, type = "text") => <Label>{label}
    <Input type={type} value={data[key]} onChange={(e) => change(key, e.target.value)} /></Label>;
  return <tr>
    <td><a href={historyDocument(company.key, row.sha)} target="_blank" rel="noreferrer">{row.name}</a></td>
    <td><div className="grid grid-cols-2 gap-2.5 [&_label]:m-0 [&_label]:flex [&_label]:flex-col [&_label]:gap-1">
      <Label>Firma<Select value={data.target} onChange={(e) => change("target", e.target.value)}>
        {view.companies.map((c) => <option key={c.key} value={c.key}>{c.name}</option>)}
      </Select></Label>
      <Label>Rolul firmei<Select value={data.direction} onChange={(e) => change("direction", e.target.value)}>
        <option value="">Alege rolul</option><option value="issued">Furnizor</option><option value="received">Client</option>
      </Select></Label>
      {field("number", "Număr")}{field("issue_date", "Data emiterii", "date")}{field("total", "Total")}
      {field("currency", "Monedă")}{field("party_name", "Partener")}{field("party_tax_id", "CUI partener")}
    </div></td>
    <td><div className="flex flex-col gap-2 max-[600px]:flex-row max-[600px]:flex-wrap"><Button type="button" disabled={busy} onClick={() => save(false)}>Confirmă factura</Button>
      <Button type="button" variant="secondary" disabled={busy} onClick={() => save(true)}>Nu este factură</Button></div></td>
  </tr>;
}
