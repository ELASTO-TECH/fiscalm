"use client";
import { Button, Input, Select, Label, Actions, formTypography, surfaceStyle } from "./Controls";
import { useState } from "react";
import type { CardInvoice, ReceiptLine } from "@/lib/types";
import { useCan, useCompany } from "./context";
import { Act, L } from "./ui";
import { UNITS as units, unitName } from "@/lib/format";

const emptyLine = (): ReceiptLine => ({ name: "", unit: "H87", quantity: "", net: "", vat: "" });
const fields: [keyof ReceiptLine, string][] = [["name", "Denumire"], ["unit", "Unitate"], ["quantity", "Cantitate"], ["net", "Valoare fără TVA"], ["vat", "TVA (%)"]];
/** The amounts the engine accepts (`receipt_lines.number`); anything else is not prefilled. */
const decimalAmount = /^\d{1,12}(\.\d{1,4})?$/;
const filled = (line: ReceiptLine) => !!(line.name || line.quantity || line.net || line.vat);

/** What the editor shows first: exactly what was saved, or, before any save, what the receipt itself says. A prefilled
 * value is saved only when the person saves. */
function start(d: CardInvoice) {
  const saved = d.items;
  if (saved?.revision) return { lines: saved.lines, total: saved.total, currency: saved.currency, payment: saved.payment };
  return { lines: [], total: d.amount !== null && decimalAmount.test(d.amount) ? d.amount : "", currency: d.currency, payment: d.card ? "card" : "" };
}

export function ReceiptItems({ d }: { d: CardInvoice }) {
  const allowed = useCan("member");
  const s = useCompany();
  const saved = d.items;
  const initial = start(d);
  const [lines, setLines] = useState<ReceiptLine[]>(initial.lines);
  const [total, setTotal] = useState(initial.total);
  const [currency, setCurrency] = useState(initial.currency);
  const [payment, setPayment] = useState(initial.payment);
  const [open, setOpen] = useState<boolean | "now">(initial.lines.length > 0);
  const dirty = JSON.stringify({ lines, total, currency, payment }) !== JSON.stringify(initial);
  const linked = !!d.invoice;
  const proposal = !linked && !dirty ? s.invoices.find((r) => r.id === saved?.proposal) : undefined;
  if (linked && !lines.length) return null;
  const setLine = (i: number, key: keyof ReceiptLine, value: string) => setLines(lines.map((row, j) => j === i ? { ...row, [key]: value } : row));
  const expand = () => { setOpen("now"); if (!lines.length) setLines([emptyLine()]); };
  return <section aria-label="Datele bonului" className={`my-5 grid gap-4 p-4 sm:p-5 ${surfaceStyle} ${formTypography}`}>
    {proposal ? <div className="grid gap-3 rounded-control bg-inset p-3">
      <p className="font-semibold">Corespunde facturii {proposal.number}</p>
      <Actions className="btns mt-0"><Act action="receipt-invoice-confirm" body={{ id: d.id, revision: saved?.revision ?? 0, invoice: proposal.id }} label="Confirmă asocierea" />
        <L to={{ inv: proposal.id }} className="btn g">Vezi factura</L></Actions>
    </div> : null}
    {saved?.provenance === "ai" ? <p>Articole extrase automat. Verifică-le pe bon.</p> : null}
    {saved?.payment_mismatch ? <p role="alert">Bonul indică numerar, dar extrasul confirmă o plată cu cardul.</p> : null}
    <fieldset className="m-0 grid min-w-0 gap-4 border-0 p-0" disabled={linked || !allowed}>
      <div className="grid grid-cols-2 gap-x-3 [&_label]:mt-0 [&_label:last-child]:col-span-2">
        <Label className="grid gap-2">Total cu TVA<Input inputMode="decimal" value={total} onChange={(e) => setTotal(e.target.value.replace(",", "."))} /></Label>
        <Label className="grid gap-2">Monedă<Input value={currency} maxLength={3} onChange={(e) => setCurrency(e.target.value.toUpperCase())} /></Label>
        <Label className="grid gap-2">Plată<Select value={payment} onChange={(e) => setPayment(e.target.value)}>
          <option value="">Necunoscută</option><option value="cash">Numerar</option><option value="card">Card</option>
        </Select></Label>
      </div>
      {!open ? (allowed && !linked ? <Button type="button" variant="quiet" className="justify-self-start" aria-expanded={false} onClick={expand}>
        Completează articolele <span className="font-normal text-muted">(opțional)</span></Button> : null) : <div className="grid gap-3">
        <h3 className="m-0">Articole</h3>
        {lines.map((line, i) => <div className="grid grid-cols-2 gap-x-3 rounded-control bg-inset p-3 [&_label]:m-0 [&_label]:mb-3 [&_label:first-of-type]:col-span-2" key={i}>
          <h4 className="col-span-2 mt-0 mb-3 text-sm font-semibold">Articolul {i + 1}</h4>
          {fields.map(([key, label]) => <Label key={key} className="grid content-start gap-2">{label}{key === "unit" ? <Select value={line.unit} onChange={(e) => setLine(i, "unit", e.target.value)}>
            <option value="">Necunoscută</option>{Object.entries(units).map(([code, name]) => <option key={code} value={code}>{name}</option>)}
            {line.unit && !unitName(line.unit) ? <option value={line.unit}>Unitate din document ({line.unit})</option> : null}
          </Select> : <Input value={line[key]} maxLength={160} autoFocus={open === "now" && i === 0 && key === "name"} inputMode={key === "name" ? "text" : "decimal"}
            onChange={(e) => setLine(i, key, key === "name" ? e.target.value : e.target.value.replace(",", "."))} />}</Label>)}
          {allowed && !linked ? <Button type="button" variant="quiet" className="col-span-2 justify-self-start" onClick={() => setLines(lines.filter((_, j) => j !== i))}>Elimină articolul {i + 1}</Button> : null}
        </div>)}
        {allowed && !linked ? <Button type="button" className="btn g justify-self-start" disabled={lines.length >= 40} onClick={() => setLines([...lines, emptyLine()])}>Adaugă articol</Button> : null}
      </div>}
    </fieldset>
    {!linked ? <Actions className="btns mt-0"><Act action="receipt-lines-save" label="Salvează"
      body={{ id: d.id, revision: saved?.revision ?? 0, payload: JSON.stringify({ lines: lines.filter(filled), total, currency, payment }) }} /></Actions> : null}
  </section>;
}
