"use client";
import { useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import CURRENCIES from "@/lib/document-currencies.json";
import type { CardInvoice } from "@/lib/types";
import { useCan } from "./context";
import { Actions, Button, Input, Label, Select } from "./Controls";
import { Sheet } from "./Sheets";
import { Act } from "./ui";

/** Metadata only. The engine refuses corrections to proof or sent documents. */
export function DocumentFields({ d, preview }: { d: CardInvoice; preview: ReactNode }) {
  const allowed = useCan("member");
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState(d.amount ?? "");
  const [currency, setCurrency] = useState(d.currency);
  const [date, setDate] = useState(d.date ?? "");
  const [number, setNumber] = useState(d.number ?? d.mail?.number ?? "");
  if (!allowed || d.can_correct === false) return null;
  const unknown = d.amount === null;
  const [whole, cents = ""] = amount.split(".");
  const total = /^-?\d{1,12}(\.\d{0,2})?$/.test(amount) ? `${whole}.${cents.padEnd(2, "0")}` : "";
  return <>
    {unknown && d.mail ? <p className="text-muted">Totalul nu a putut fi citit din document.</p> : null}
    <Actions><Button variant={unknown ? "primary" : "quiet"} onClick={() => setOpen(true)}>{unknown ? "Adaugă suma" : "Corectează"}</Button></Actions>
    {open ? createPortal(<Sheet cls="wide ask tall min-[1100px]:w-[880px]!" onClose={() => setOpen(false)} label={unknown ? "Adaugă suma" : "Corectează documentul"}>
      <h2>{unknown ? "Adaugă suma" : "Corectează documentul"}</h2>
      <div className="grid gap-6 min-[1100px]:grid-cols-2">
        <div className="hidden min-[1100px]:block">{preview}</div>
        <div>
          <Label>Sumă<Input autoFocus inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value.replace(",", "."))} /></Label>
          <Label>Monedă<Select value={currency} onChange={(e) => setCurrency(e.target.value)}>{CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}</Select></Label>
          <Label>Data documentului<Input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></Label>
          <Label>Număr document<Input value={number} maxLength={100} onChange={(e) => setNumber(e.target.value)} /></Label>
          <Actions><Act action="document-correct" label="Salvează" off={!total || !currency}
            body={{ id: d.id, amount: total, currency, date, number }} />
            <Button variant="quiet" onClick={() => setOpen(false)}>Închide</Button></Actions>
        </div>
      </div>
    </Sheet>, document.body) : null}
  </>;
}
