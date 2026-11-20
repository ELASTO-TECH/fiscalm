"use client";
import { Actions, Form, Input, Label, Panel, Select, Textarea, sectionHeadingStyle, surfaceStyle, backLinkStyle, noteLeftStyle, settingsPageStyle } from "./Controls";
// A role on an existing partner. Bank details stay on supplier invoices and their guarded confirmation flow.
import { useCallback, useState } from "react";
import { getBrowse } from "@/lib/api";
import type { Partner, State } from "@/lib/types";
import { useCan, useCompany } from "./context";
import { PageNav, usePage } from "./Paging";
import { ChoiceSearch } from "./TransactionChoices";
import { Act, I, L, Row, Summary, Title } from "./ui";

export function Contabil() {
  const s = useCompany();
  return <div className={settingsPageStyle}>
    <L to={{ tab: "setari" }} className={backLinkStyle} label="Înapoi la Setări"><I n="arrow-left" />Setări</L>
    <Title>Contabil</Title>
    <AccountantForm key={`${s.co}:${JSON.stringify([s.accountant.cui, s.accountant.senders, s.accountant.recipient])}`} s={s} />
  </div>;
}

function AccountantForm({ s }: { s: State }) {
  const saved = s.accountant;
  const [cui, setCui] = useState(saved.cui);
  const [senders, setSenders] = useState(saved.senders.join("\n"));
  const [recipient, setRecipient] = useState(saved.recipient);
  const [original] = useState(() => saved.partner ?? s.partners.find((p) => p.cui === saved.cui));
  const [chosen, setChosen] = useState(original);
  // Editing trusted sender addresses requires an explicit administrator capability.
  const admin = useCan("manage_firms") && s.can_manage_companies === true;
  const changed = cui !== saved.cui || senders !== saved.senders.join("\n") || recipient !== saved.recipient;
  return <>
    <Form className={`${surfaceStyle} p-5 sm:p-6`} onSubmit={(e) => e.preventDefault()} aria-label="Datele contabilului">
      <AccountantChoice s={s} value={cui} original={original} selected={chosen} disabled={!admin} onChange={(value, partner) => {
        setCui(value); setChosen(partner);
        setSenders(value === saved.cui ? saved.senders.join("\n") : "");
        setRecipient(value === saved.cui ? saved.recipient : "");
      }} />
      <details className="mt-4">
        <Summary>{`Adresele de la care trimite ordinele de plată${saved.senders.length ? ` (${saved.senders.length})` : ""}`}</Summary>
        <Label htmlFor="accountant-senders">E-mailuri de la contabil</Label>
        <p className={noteLeftStyle} id="accountant-senders-note">Câte una pe linie.</p>
        <Textarea id="accountant-senders" value={senders} rows={3} maxLength={2550} disabled={!admin || !cui}
          aria-describedby="accountant-senders-note" onChange={(e) => setSenders(e.target.value)} />
      </details>
      <Label htmlFor="accountant-recipient">Adresa la care trimiți dosarul</Label>
      <Input id="accountant-recipient" type="email" value={recipient} maxLength={254} disabled={!admin || !cui}
        onChange={(e) => setRecipient(e.target.value)} />
      {admin ? <Actions><Act action="accountant-save" body={{ cui, senders, recipient }}
        label="Salvează datele contabilului" off={!changed} why="Nicio modificare de salvat." /></Actions>
        : <p className={noteLeftStyle}>Doar administratorii pot schimba datele contabilului.</p>}
    </Form>
    {chosen ? <>
      <div className={sectionHeadingStyle}><h2>Facturi și cont bancar</h2></div>
      <Panel padded={false}><Row icon="user" title={chosen.name || chosen.cui!} sub={`CUI ${chosen.cui}`} to={{ p: chosen.id }} /></Panel>
    </> : null}
    <p className={noteLeftStyle}><L to={{ tab: "parteneri" }}>Vezi partenerii</L></p>
  </>;
}

type AccountantPartner = Pick<Partner, "id" | "name" | "cui"> & Partial<Pick<Partner, "roles">>;
function AccountantChoice({ s, value, original, selected, disabled, onChange }: {
  s: State; value: string; original?: AccountantPartner; selected?: AccountantPartner; disabled: boolean;
  onChange: (cui: string, partner: AccountantPartner | undefined) => void;
}) {
  const [q, setQ] = useState("");
  const read = useCallback((cursor: string, signal?: AbortSignal) => getBrowse(s.co, "parteneri", { f: "accountant", q, cursor }, signal), [s.co, q]);
  const page = usePage(read, q ? 200 : 0);
  // an accountant invoices the firm: its suppliers come first
  const choices = [...new Map([original, selected, ...page.data].filter((p): p is AccountantPartner => !!p).map((p) => [p.cui, p])).values()]
    .sort((a, b) => Number(!!b.roles?.includes("supplier")) - Number(!!a.roles?.includes("supplier")));
  return <>
    <ChoiceSearch id="accountant-search" label="Caută partenerul" total={page.total} value={q} onChange={setQ} />
    <Label htmlFor="accountant-partner">Partener</Label>
    <Select id="accountant-partner" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value, choices.find((p) => p.cui === e.target.value))}>
      <option value="">Fără contabil</option>
      {s.accountant.cui && !choices.some((p) => p.cui === s.accountant.cui) ? <option value={s.accountant.cui}>CUI {s.accountant.cui}</option> : null}
      {choices.map((p) => <option key={p.id} value={p.cui!}>{p.name || p.cui} (CUI {p.cui})</option>)}
    </Select>
    <PageNav page={page} label="Parteneri" rows={0} />
    {!q && !page.loading && !page.error && !page.total ? <p className={noteLeftStyle}>Niciun partener cu CUI.</p> : null}
  </>;
}
