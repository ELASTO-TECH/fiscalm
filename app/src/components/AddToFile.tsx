"use client";
// "Pune în fișier": the chosen invoices of one firm go into a payment file the person picks, from every place that offers it
// (Cheltuieli's bar, an invoice's sheet). The newest open file is the default, then the other open files, then a new file
// with the name the engine proposes; with no open file only the new one is asked. One request for all of them: the
// engine adds each it allows and says what it refused.
import { useId, useState } from "react";
import { createPortal } from "react-dom";
import { allowedInShadow, postFile, type FileAction } from "@/lib/api";
import { cap, cents, curOf, fromCents, lei, nOf } from "@/lib/format";
import type { Co, Invoice, PaymentFile, State } from "@/lib/types";
import { Actions, Button, Input, Label } from "./Controls";
import { useApp, useCan } from "./context";
import { maskedIban, notInFile } from "./derive";
import { CloseX, Sheet, SheetHead } from "./Sheets";

/** The longest name the engine keeps (its CHECK on payment_files.name). */
const NAME_MAX = 200;

/** The firm's open files that can take payments, newest first, the engine's default first. A file the engine asks about
 * (its download gone from the folder, downloaded several times, an unknown file beside it) takes nothing until answered:
 * whether it was imported is not known, and a payment added now could be taken as imported with it. */
export function openFiles(s: State): PaymentFile[] {
  const open = s.files.filter((f) => f.status === "open" && !f.question);
  const first = s.payment_file_defaults.find((d) => d.co === s.co)?.open_file_id;
  return [...open.filter((f) => f.id === first), ...open.filter((f) => f.id !== first)];
}

/** Whether this person may put invoices in a file here: a member, and the shared mode policy (Live, or local in Sandbox). */
export function useCanFile(s: State | undefined): boolean {
  const member = useCan("member");
  return !!s && member && (s.live || (allowedInShadow("payment_file_add") && allowedInShadow("payment_file_create")));
}

const fileLine = (f: PaymentFile) => `Deschis, ${nOf(f.payment_count, "o plată", "plăți")}, ${lei(f.total)} ${curOf(f.currency)}`;

/** The chooser over the list. `onDone(true)` once the engine took the request (the list then clears its choice). */
export function AddToFile({ s, invoices, left = [], onDone }: { s: State; invoices: Invoice[]; left?: Invoice[]; onDone: (sent: boolean) => void }) {
  const { run, busy } = useApp();
  const ids = useId();
  const files = openFiles(s);
  const [choice, setChoice] = useState<number | "new">(files[0]?.id ?? "new");
  const [name, setName] = useState(s.payment_file_defaults.find((d) => d.co === s.co)?.name ?? "");
  const [sending, setSending] = useState(false);
  // An invoice listing several IBANs that no evidence decides: the person picks the account; fiscalm remembers it once paid.
  const [picks, setPicks] = useState<Record<string, number>>({});
  const asked = invoices.filter((r) => r.accounts.length > 0);
  // A pick must be one of the accounts offered now: a refresh that narrows the list asks again.
  const unpicked = asked.some((r) => !r.accounts.some((a) => a.index === picks[r.id]));
  // A file chosen and then imported or deleted elsewhere is no longer offered: nothing is chosen until the person picks again.
  const chosen = files.find((f) => f.id === choice);
  const fresh = choice === "new" || !files.length;
  const lost = !fresh && !chosen;
  const title = files.length ? "Pune în fișier" : "Fișier nou";
  const sum = invoices.length === 1 ? `${invoices[0].supplier}, ${lei(invoices[0].left)} ${curOf(invoices[0].currency)}`
    : `${cap(nOf(invoices.length, "o factură", "facturi"))}, ${totalOf(invoices)}`;
  const send = async () => {
    const invoice_ids = invoices.map((r) => r.id);
    if (lost || unpicked) return;
    // Every pick the person made is sent, also for an invoice a refresh no longer asks about: what they chose is paid,
    // never a default that arrived while the sheet was open (the engine still pays only an account the invoice lists).
    const chosenAccounts = invoices.filter((r) => picks[r.id] !== undefined).map((r) => [r.id, picks[r.id]]);
    const accounts = chosenAccounts.length ? { accounts: Object.fromEntries(chosenAccounts) } : {};
    // The amount shown for each invoice goes along: a credit note arriving meanwhile changes it, and the engine refuses.
    const shown = { amounts: Object.fromEntries(invoices.map((r) => [r.id, r.left])) };
    const body: FileAction = fresh || !chosen ? { action: "payment_file_create", co: s.co as Co, name: name.trim(), invoice_ids, ...accounts, ...shown }
      : { action: "payment_file_add", file_id: chosen.id, invoice_ids, ...accounts, ...shown };
    setSending(true);
    const r = await run(s.co as Co, () => postFile(body), undefined, () => false);
    setSending(false);
    if (r?.ok) onDone(true);
  };
  const close = sending ? () => undefined : () => onDone(false);
  const nameField = <>
    <Label htmlFor={`${ids}-name`}>Nume</Label>
    <Input id={`${ids}-name`} value={name} onChange={(e) => setName(e.target.value)} maxLength={NAME_MAX} autoComplete="off"
      data-autofocus={files.length ? undefined : true} />
  </>;
  return createPortal(
    <Sheet onClose={close} label={title} cls={asked.length ? "ask tall" : "ask"} busy={sending}>
      <CloseX onClose={close} />
      <SheetHead title={title} sub={sum} />
      <fieldset className="m-0 border-0 p-0" disabled={sending}>
        {files.length ? <>
          <legend className="sr-only">Fișierul de plăți</legend>
          <div className="overflow-hidden rounded-card border border-solid border-line">
            {files.map((f, i) => <label key={f.id} className={optionStyle}>
              <Input type="radio" name={`${ids}-file`} className="mt-1" checked={choice === f.id} onChange={() => setChoice(f.id)}
                data-autofocus={i === 0 ? true : undefined} />
              <span className="min-w-0"><span className="block font-semibold wrap-anywhere">{f.name}</span>
                <span className="block text-sm text-muted">{fileLine(f)}</span></span>
            </label>)}
            <label className={optionStyle}>
              <Input type="radio" name={`${ids}-file`} className="mt-1" checked={choice === "new"} onChange={() => setChoice("new")} />
              <span className="min-w-0 flex-1"><span className="block font-semibold">Fișier nou</span>
                {choice === "new" ? null : <span className="block text-sm text-muted wrap-anywhere">{name}</span>}</span>
            </label>
          </div>
          {choice === "new" ? nameField : null}
        </> : nameField}
      </fieldset>
      {asked.map((r) => <fieldset key={r.id} className="m-0 mt-4 border-0 p-0" disabled={sending}>
        <legend className="mb-2 text-sm font-semibold">Contul în care plătești {r.supplier}, nr. {r.number}</legend>
        <div className="overflow-hidden rounded-card border border-solid border-line">
          {r.accounts.map((a) => <label key={a.index} className={optionStyle}>
            <Input type="radio" name={`${ids}-acct-${r.id}`} className="mt-1" checked={picks[r.id] === a.index}
              onChange={() => setPicks((p) => ({ ...p, [r.id]: a.index }))} />
            <span className="min-w-0">{a.bank} {maskedIban(a.ending)}</span>
          </label>)}
        </div>
      </fieldset>)}
      {left.length ? <div className="mt-3 text-sm text-muted">
        <p className="m-0">{cap(nOf(left.length, "o factură aleasă nu intră", "facturi alese nu intră"))} în fișier:</p>
        <ul className="m-0 mt-1 pl-5">{left.map((r) => <li key={r.id}>{r.supplier}, nr. {r.number}: {notInFile(s, r)}</li>)}</ul>
      </div> : null}
      {chosen?.downloaded_at ? <p className="mt-3 mb-0 text-sm text-muted">Fișierul a fost descărcat. După ce adaugi facturile, descarcă-l din nou.</p> : null}
      <Actions>
        <Button type="button" className="btn w-full!" disabled={sending || busy || lost || unpicked || (fresh && !name.trim())} aria-busy={sending || undefined}
          onClick={() => void send()}>Pune în fișier</Button>
      </Actions>
    </Sheet>,
    document.body,
  );
}

const optionStyle = "flex cursor-pointer items-start gap-3 border-0 border-t border-solid border-line px-4 py-3 first:border-t-0 has-[:checked]:bg-selected";

/** "1.626,50 lei", "120,00 lei + 20,00 EUR": what the chosen invoices still have to be paid, by currency. */
export function totalOf(list: Invoice[]): string {
  const by = new Map<string, bigint>();
  for (const r of list) by.set(curOf(r.currency), (by.get(curOf(r.currency)) ?? 0n) + cents(r.left));
  return [...by].map(([cur, c]) => `${lei(fromCents(c))} ${cur}`).join(" + ");
}
