"use client";
import { Button, Input, Select, Label, Form, Actions, Panel, detailListStyle, noteLeftStyle, sectionHeadingStyle } from "./Controls";
// A transaction's sheet (`tx`): what moved, what explains it, and for a row De explicat (or the user's own explanation)
// the form that explains it: De la cine (Către cine for a debit), Ce acoperă and, when the amounts differ, what the difference is
// (docs/information-architecture.md section 4). The engine decides what can be linked, what closes and what stays owed
// (`tx-link`); the app lists the partner's open invoices, shows the difference and posts the choice.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { post } from "@/lib/api";
import { cents, fromCents, curOf, dm, lei, lunaName, phrase } from "@/lib/format";
import { type Label as TransactionLabel, type LinkKind, type MemoryKey, type Partner, type Reason, type Transaction,
  type TxLink, type CoveredRow, type TransactionChoices, type TransactionInvoiceChoice, type TransactionChoiceRows } from "@/lib/types";
import { useApp, useCan, useCompany } from "./context";
import { NEW, partnerName } from "./Partners";
import { KEPT_BY } from "./RememberedList";
import { CloseX, DetailSheet, SHEET_SHAPES, Sheet, SheetHead } from "./Sheets";
import { DetailPages, useDetail } from "./DocumentDetail";
import { PageNav } from "./Paging";
import { ChoiceSearch, useChoicePage, useSelected } from "./TransactionChoices";
import { useExpenseUpload } from "./ExpenseUpload";
import { Act, I, L, Row } from "./ui";

const transactionFormStyle = "mt-5 min-w-0 [&_fieldset]:m-0 [&_fieldset]:mb-5 [&_fieldset]:min-w-0 [&_fieldset]:border-0 [&_fieldset]:p-0 [&_legend]:mb-3 [&_legend]:text-base [&_legend]:font-semibold";
const transactionChoiceStyle = "min-h-11 cursor-pointer rounded-control bg-inset px-3 py-3 has-[:checked]:bg-selected";

/** A difference's name, as the user picks it; a part still owed reads by the transaction's direction. */
export function reasonLabel(r: Reason, direction: Transaction["direction"]): string {
  return { rest: direction === "credit" ? "Rest de încasat" : "Rest de plătit", courier_fee: "Comision curier sau procesator",
    bank_fee: "Comision bancar", fx: "Diferență de curs", other: "Altceva" }[r];
}

/** What the user says a transaction is, as the sheet offers it. */
export const KIND_LABEL: Record<LinkKind, string> = { documents: "Factură emisă", label: "Fără document",
  transfer: "Transfer între conturi", advance: "Avans de trezorerie" };
/** A debit's answers: a bon or chitanță uploaded from here explains it (document-upload with `tx`); one whose invoice
 * hasn't come yet waits for it (tx-await), reserving nothing. */
type Kind = LinkKind | "receipt" | "await";
const DEBIT_LABEL: Record<Kind, string> = { documents: "Factură", await: "Aștept factura", receipt: "Bon / chitanță",
  label: "Fără document", transfer: "Transfer între conturi", advance: "Avans de trezorerie" };
/** Fără document's labels (fiscalm.links.LABELS). */
export const LABEL_NAME: Record<TransactionLabel, string> = { bank_fee: "Comision bancar", interest: "Dobândă",
  personal: "Cheltuială personală", other: "Altceva" };

export function TxSheet({ id, onClose }: { id: string; onClose: () => void }) {
  return <DetailSheet kind="transaction" id={id} onClose={onClose}><TxBody key={id} id={id} onClose={onClose} /></DetailSheet>;
}

function TxBody({ id, onClose }: { id: string; onClose: () => void }) {
  const allowed = useCan("member");
  const s = useCompany();
  const t = s.transactions.rows.find((r) => r.id === id)!;
  // the explanation being changed (its id): a different one arriving meanwhile shows instead of the form
  const [editing, setEditing] = useState<string | null>(null);
  const credit = t.direction === "credit";
  const cur = curOf(t.currency);
  const who = t.counterparty || t.details || (credit ? "Intrare în cont" : "Ieșire din cont");
  const link = t.explanation && "link" in t.explanation ? t.explanation.link : undefined;
  const what = useDetail().transaction!.said;
  return (
    <Sheet onClose={onClose} cls={SHEET_SHAPES.transaction.cls} label={`${who}, ${credit ? "+" : "−"}${lei(t.amount)} ${cur}`}>
      <CloseX onClose={onClose} />
      <SheetHead amount={t.amount} sign={credit ? "+" : "−"} cur={cur} sub={who} />
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Data</dt><dd>{dm(t.date, s.today)}</dd></dl>
        {t.account ? <dl className={`${detailListStyle} py-2`}><dt>Cont</dt><dd>{`cont ${t.account}`}</dd></dl> : null}
        {t.orig ? <dl className={`${detailListStyle} py-2`}><dt>Suma originală</dt><dd>{`${lei(t.orig.amount)} ${curOf(t.orig.currency)}`}</dd></dl> : null}
        {t.details && t.details !== who ? <dl className={`${detailListStyle} py-2`}><dt>Detalii</dt><dd>{t.details}</dd></dl> : null}
        {t.awaiting && !link ? <dl className={`${detailListStyle} py-2`}><dt>Aștept factura</dt>
          <dd>{phrase(t.awaiting.note, dm(t.awaiting.at.slice(0, 10), s.today))}</dd></dl> : null}
        {what && !link ? (
          <dl className={`${detailListStyle} py-2`}><dt>{what.proposal ? "Propunere" : "Explicație"}</dt>
            <dd>{what.to ? <L to={what.to}>{what.text}</L> : what.text}</dd></dl>
        ) : null}
      </Panel>
      {t.explanation?.kind === "collection" && t.explanation.settlement ? <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Comision curier sau procesator</dt><dd>{lei(s.settlements?.find((r) => r.id === (t.explanation?.kind === "collection" ? t.explanation.settlement : null))?.fee ?? "0.00")} {cur}</dd></dl>
      </Panel> : null}
      {t.card && !t.card_owner ? allowed && <CardOwner t={t} /> : link && (!allowed || editing !== link.id) ? <Linked key={link.id} t={t} link={link} onChange={() => setEditing(link.id)} />
        : allowed && t.can_link ? <Explain key={link?.id ?? ""} t={t} link={link} onDone={() => setEditing(null)}
          onCancel={link ? () => setEditing(null) : undefined} />
        : null}
    </Sheet>
  );
}

function CardOwner({ t }: { t: Transaction }) {
  const { view } = useApp();
  return <section aria-labelledby="card-owner">
    <div className={sectionHeadingStyle}><h2 id="card-owner">Titularul cardului •••• {t.card}</h2></div>
    <Actions>
      {/* the firm whose account it paid from is the likely owner: first and the main button */}
      {[...view.companies].sort((a, b) => Number(b.key === t.company) - Number(a.key === t.company)).map((co) =>
        <Act key={co.key} action="card-owner" body={{ tx: t.id, owner: co.key }} label={`Cardul firmei ${co.name}`} cls={co.key === t.company ? "btn" : "btn g"} />)}
      <Act action="card-owner" body={{ tx: t.id, owner: "personal" }} label="Card personal" cls="btn g" />
    </Actions>
  </section>;
}

/** A part that replaces another inside the open sheet takes the focus when the element that had it went with it (on the
 * sheet's first render the Sheet has already set the focus in its layout effect). */
function useTakeFocus<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (el && !el.closest(".sheet")?.contains(document.activeElement)) el.focus();
  }, []);
  return ref;
}

/** The user's explanation: what it covers or says the transaction is, when and by whom; changed or undone where it is
 * recorded (`link.tx`, for a transfer maybe the other end). */
function Linked({ t, link, onChange }: { t: Transaction; link: TxLink; onChange: () => void }) {
  const allowed = useCan("member");
  const s = useCompany();
  const heading = useTakeFocus<HTMLHeadingElement>();
  return (
    <section aria-labelledby="tx-linked">
      <div className={sectionHeadingStyle}><h2 id="tx-linked" tabIndex={-1} ref={heading}>{link.kind === "documents" ? "Ce acoperă" : link.kind === "receipt" ? DEBIT_LABEL.receipt : KIND_LABEL[link.kind]}</h2></div>
      {link.kind === "documents" ? <Covered t={t} link={link} /> : <Marked t={t} />}
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Explicată</dt><dd>{[dm(link.at.slice(0, 10), s.today),
          link.rule ? BY_RULE[link.rule] : link.by].filter(Boolean).join(", ")}</dd></dl>
      </Panel>
      {allowed && t.can_link ? (
        <Actions>
          {/* a transfer is changed where it is recorded (its documents would be the other direction's) */}
          {link.tx === t.id && link.kind !== "receipt" ? <Button type="button" className="btn g" onClick={onChange}>Schimbă explicația</Button> : null}
          <Act action="tx-unlink" body={{ tx: link.tx, was: link.id }} label="Anulează asocierea" cls="btn g" confirm={{ text: "Anulezi asocierea? Tranzacția și ce acoperea revin la starea de dinainte.", yes: "Anulează asocierea", danger: true }} />
        </Actions>
      ) : null}
    </section>
  );
}

/** Who made an explanation no person made (links.apply_rules). */
const BY_RULE = { saved: "după regula salvată", bank: "după extrasul băncii", transfer: "după ambele capete ale transferului" };

/** What a label, an advance, a transfer or a bon says; a transfer's other end and a bon open their own sheets. */
function Marked({ t }: { t: Transaction }) {
  const s = useCompany();
  const e = t.explanation;
  const other = e?.kind === "transfer" && e.other ? s.transactions.rows.find((x) => x.id === e.other) : undefined;
  const bon = useDetail().transaction?.said;
  const rows: [string, ReactNode][] = e?.kind === "label" ? [["Tip", LABEL_NAME[e.label]], ["Notă", e.note ?? ""]]
    : e?.kind === "receipt" ? [["Bon", bon?.to ? <L to={bon.to}>{bon.text}</L> : bon?.text ?? ""]]
    : e?.kind === "advance" ? [["Administratorul sau asociatul", e.who]]
    : [["Cealaltă parte", other ? <L to={{ tx: other.id }}>{[other.account ? `cont ${other.account}` : "", dm(other.date, s.today)]
      .filter(Boolean).join(", ")}</L> : "Nu apare în conturile firmei"]];
  return (
    <Panel className="my-4">
      {rows.filter(([, v]) => v).map(([k, v]) => <dl key={k} className={`${detailListStyle} py-2`}><dt>{k}</dt><dd>{v}</dd></dl>)}
    </Panel>
  );
}

/** What documents a link covers: each part with what is left of it, the partner and the named difference. */
function Covered({ t, link }: { t: Transaction; link: TxLink }) {
  const s = useCompany();
  const rows = useDetail().related.data as CoveredRow[];
  const credit = t.direction === "credit";
  const partner = s.partners.find((p) => p.id === link.partner);
  const diff = cents(link.difference);
  return <>
    <Panel padded={false} className="my-4"><DetailPages />{rows.map((row) => row.kind === "invoice"
      ? <Row key={`i-${row.id}`} icon={credit ? "invoice" : "receipt"} title={`Factura ${row.number}`}
        sub={row.closed || cents(row.left) === 0n ? "Acoperită integral" : `Rămâne ${lei(row.left)} ${curOf(row.currency)} ${credit ? "de încasat" : "de plătit"}`}
        subcls={row.closed ? "ok" : ""} val={`${lei(row.amount)} ${curOf(row.currency)}`} to={credit ? { iss: row.id } : { inv: row.id }} />
      : <Row key={`p-${row.id}`} icon="bank" title={row.payment.payee} sub={row.payment.details || row.payment.ref || "Plată pregătită"} to={{ tab: "plati" }} />)}</Panel>
    <Panel className="my-4">
      {partner ? <dl className={`${detailListStyle} py-2`}><dt>{credit ? "Plătitor" : "Beneficiar"}</dt><dd><L to={{ p: partner.id }}>{partnerName(partner)}</L></dd></dl> : null}
      {diff !== 0n && link.reason ? <dl className={`${detailListStyle} py-2`}><dt>Diferența</dt>
        <dd>{`${lei(fromCents(diff < 0n ? -diff : diff))} ${curOf(link.currency)}, ${link.reason === "other" ? link.note : reasonLabel(link.reason as Reason, t.direction)}`}</dd></dl> : null}
    </Panel>
  </>;
}

/** Options are replaced page by page; the parent retains only explicitly selected records. */
function Covers({ initial, initialPartner, picked, payment, partner, t, currency, disabled, onPick, onPay }: {
  initial: TransactionChoices["pages"]; initialPartner: string; picked: string[]; payment: string; partner: string;
  t: Transaction; currency?: string; disabled: boolean; onPick: (row: TransactionInvoiceChoice) => void;
  onPay: (row: TransactionChoiceRows["payments"]) => void;
}) {
  const s = useCompany();
  const [q, setQ] = useState(""), [payQ, setPayQ] = useState("");
  const offered = useChoicePage(t.id, "invoices", !q && partner === initialPartner ? initial.invoices : undefined, q, partner, !partner);
  const prepared = useChoicePage(t.id, "payments", !payQ ? initial.payments : undefined, payQ);
  // none of exactly this amount: the section stays hidden, a search over the whole file finds one paid with a fee
  const [look, setLook] = useState(false);
  const searchable = t.direction === "debit" && (t.currency === "RON" || t.orig?.currency === "RON");
  return <fieldset disabled={disabled}>
    <legend>Ce acoperă</legend>
    <ChoiceSearch id="tx-invoice-search" label="Caută facturile" total={offered.total} value={q} onChange={setQ} />
    <PageNav page={offered} label="Facturi" />
    {!partner ? <p className={noteLeftStyle}>Alege partenerul.</p>
      : !offered.loading && !offered.error && !offered.total ? <p className={noteLeftStyle}>Partenerul nu are facturi deschise în {curOf(t.currency)}.</p> : null}
    {offered.data.map((r) => <Label key={r.id} className={transactionChoiceStyle} htmlFor={`tx-i-${r.id}`}>
      <Input id={`tx-i-${r.id}`} type="checkbox" checked={picked.includes(r.id) && !payment} onChange={() => onPick(r)}
        disabled={!!currency && currency !== r.currency} />
      <span className="min-w-0 flex-1 wrap-anywhere">{`Factura ${r.number}${r.date ? `, ${dm(r.date, s.today)}` : ""}`}</span>{" "}
      <span className="ml-auto shrink-0 text-xs text-muted tabular-nums">{`${cents(r.left) !== cents(r.total) ? "rest " : ""}${lei(r.left)} ${curOf(r.currency)}`}</span>
    </Label>)}
    {!prepared.total && !payQ && !look && searchable && !prepared.loading
      ? <p className="mt-3"><Button type="button" className="btn g" onClick={() => setLook(true)}>Caută o plată pregătită</Button></p> : null}
    {prepared.total || payQ || look ? <><p className="mt-5 mb-3 text-base font-semibold">Plăți pregătite</p>
      <ChoiceSearch id="tx-payment-search" label="Caută plățile pregătite" total={prepared.total} value={payQ} onChange={setPayQ} always focus={look} />
      <PageNav page={prepared} label="Plăți pregătite" />
      {prepared.data.map((row) => <Label key={row.id} className={transactionChoiceStyle} htmlFor={`tx-p-${row.id}`}>
        <Input id={`tx-p-${row.id}`} type="checkbox" checked={payment === row.id} onChange={() => onPay(row)} />
        <span className="min-w-0 flex-1 wrap-anywhere">{row.payment.payee}{row.payment.details || row.payment.ref ? `, ${row.payment.details || row.payment.ref}` : ""}</span>{" "}
        <span className="ml-auto shrink-0 text-xs text-muted tabular-nums">{`${lei(row.payment.amount)} lei`}</span>
      </Label>)}</> : null}
  </fieldset>;
}

/** The amounts side by side and, when they differ, what the difference is: only the reasons the engine says fit it. */
function Difference({ t, money, docs, cur, what, reasons, reason, note, onReason, onNote }: {
  t: Transaction; money: string; docs: bigint; cur: string; what: string; reasons: Reason[]; reason: Reason | "";
  note: string; onReason: (r: Reason) => void; onNote: (n: string) => void;
}) {
  const diff = cents(money) - docs;
  return (
    <>
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Tranzacția</dt><dd>{`${lei(money)} ${cur}`}</dd></dl>
        <dl className={`${detailListStyle} py-2`}><dt>{what}</dt><dd>{`${lei(fromCents(docs))} ${cur}`}</dd></dl>
        {diff !== 0n ? <dl className={`${detailListStyle} py-2`}><dt>Diferență</dt>
          <dd>{`${lei(fromCents(diff < 0n ? -diff : diff))} ${cur} ${diff < 0n ? "mai puțin" : "mai mult"}`}</dd></dl> : null}
      </Panel>
      {diff !== 0n ? (
        <fieldset>
          <legend>Motiv diferență</legend>
          {reasons.map((r) => (
            <Label key={r} className={transactionChoiceStyle} htmlFor={`tx-r-${r}`}>
              <Input id={`tx-r-${r}`} type="radio" name="tx-reason" checked={reason === r} onChange={() => onReason(r)} />
              {reasonLabel(r, t.direction)}
            </Label>
          ))}
          {reason === "other" ? (
            <>
              <Label htmlFor="tx-note">Descrierea diferenței</Label>
              <Input id="tx-note" value={note} onChange={(e) => onNote(e.target.value)} maxLength={200} autoComplete="off" />
            </>
          ) : null}
        </fieldset>
      ) : null}
    </>
  );
}

function LinkForm({ t, link, onDone, onCancel }: {
  t: Transaction; link?: TxLink; onDone: () => void; onCancel?: () => void;
}) {
  const s = useCompany();
  const { run, busy } = useApp();
  const initial = useDetail().transaction!.choices;
  const [was] = useState(link?.id ?? "");
  const offer = link ? undefined : initial.proposed?.data[0];
  const [initialPartner] = useState(link?.partner ?? offer?.partner ?? t.partner ?? "");
  const [partner, setPartner] = useState(initialPartner);
  const [partnerChoice, setPartnerChoice] = useState(s.partners.find((p) => p.id === initialPartner));
  const [q, setQ] = useState("");
  const partners = useChoicePage(t.id, "partners", !q ? initial.partners : undefined, q);
  const restored = useSelected(t.id, initial.selected!);
  const [readySelection, setReadySelection] = useState(!initial.selected!.next_cursor);
  const [chosen, setChosen] = useState(initial.selected!.data.length || offer?.partner !== initialPartner ? initial.selected!.data : [offer]);
  if (!readySelection && !restored.loading && !restored.error) {
    setChosen(restored.data[0]); setReadySelection(true);
  }
  const [payment, setPayment] = useState<TransactionChoiceRows["payments"] | null>(null);
  const [reason, setReason] = useState<Reason | "">(link?.kind === "documents" ? (link.reason as Reason | null) ?? "" : "");
  const [note, setNote] = useState(link?.kind === "documents" ? link.note : "");
  const [keep, setKeep] = useState(false);
  const credit = t.direction === "credit", by = keyFor(t, "documents", "");
  // a card's original amount by its size, as the engine reads it (links.plan)
  const amountIn = (c: string) => t.currency === c ? t.amount : t.orig?.currency === c ? t.orig.amount.replace("-", "") : null;
  const one = payment ? "RON" : chosen[0]?.currency;
  const money = one ? amountIn(one) ?? "0.00" : t.amount;
  const docs = payment ? cents(payment.payment.amount) : chosen.reduce((n, r) => n + cents(r.left), 0n);
  const diff = one ? cents(money) - docs : 0n;
  const reasons = diff !== 0n ? s.transactions.reasons[t.direction][diff < 0n ? "less" : "more"].filter((r) => !(payment && r === "rest")) : [];
  const send = () => void run(s.co, () => post("tx-link", {
    co: s.co, tx: link?.tx ?? t.id, invoices: payment ? "" : chosen.map((r) => r.id).join(","), payment: payment?.payment.item_id ?? "",
    ...(credit && chosen.some((r) => r.partner !== partner) ? { holder: partner } : {}),
    reason: diff !== 0n ? reason : "", note: diff !== 0n && reason === "other" ? note.trim() : "", was, remember: keep && by ? by : "",
  }), undefined, (r) => { if (r.ok) onDone(); return false; });
  return <Form className={transactionFormStyle} onSubmit={(e) => e.preventDefault()}>
    <fieldset><legend>{credit ? "Plătitor" : "Beneficiar"}</legend>
      <ChoiceSearch id="tx-partner-search" label="Caută partenerul" total={partners.total} value={q} onChange={setQ} />
      <PageNav page={partners} label="Parteneri" />
      <Select id="tx-partner" aria-label={credit ? "Plătitor" : "Beneficiar"} value={partner} disabled={!readySelection} onChange={(e) => {
        setPartner(e.target.value); setPartnerChoice(partners.data.find((p) => p.id === e.target.value)); setChosen([]);
      }}>
        <option value="">Alege partenerul</option>
        {partner && !partners.data.some((p) => p.id === partner) ? <option value={partner}>{partnerChoice ? partnerName(partnerChoice) : partner}</option> : null}
        {partners.data.map((p) => <option key={p.id} value={p.id}>{partnerName(p)}</option>)}
      </Select>
      {credit ? null : <p className="mt-3"><L to={{ p: NEW }}>Furnizor nou</L></p>}
    </fieldset>
    {!readySelection ? <PageNav page={restored} /> : null}
    <Covers key={partner} initial={initial} initialPartner={initialPartner} picked={chosen.map((r) => r.id)} payment={payment?.id ?? ""}
      partner={partner} t={t} currency={chosen[0]?.currency} disabled={!readySelection}
      onPick={(row) => { setPayment(null); setChosen((old) => (old.some((r) => r.id === row.id) ? old.filter((r) => r.id !== row.id) : [...old, row]).sort((a, b) => a.position - b.position)); }}
      onPay={(row) => { setChosen([]); setPayment(payment?.id === row.id ? null : row); }} />
    {readySelection && one ? <Difference t={t} money={money} docs={docs} cur={curOf(one)} reasons={reasons} reason={reason} note={note}
      what={payment ? "Plata pregătită" : chosen.length > 1 ? "Facturile alese" : "Factura aleasă"} onReason={setReason} onNote={setNote} /> : null}
    {by ? <Keep t={t} by={by} on={keep} onChange={setKeep} /> : null}
    <Actions>
      {/* a missing choice is named by the engine's refusal (links.REFUSED), not by a silent disabled button */}
      <Button type="button" className="btn" disabled={busy || !readySelection} onClick={send}>Asociază tranzacția</Button>
      {onCancel ? <Button type="button" className="btn g" onClick={onCancel}>Renunță</Button> : null}
    </Actions>
  </Form>;
}

/** What the transaction is, then its form: documents (LinkForm), a bon (Receipt) or a label, a transfer, an advance
 * (MarkForm). A line the bank marks as its fee or interest starts on Fără document with that label; a card payment no
 * partner or invoice points to starts on its bon (a merchant abroad has no invoice to cover). */
function Explain({ t, link, onDone, onCancel }: { t: Transaction; link?: TxLink; onDone: () => void; onCancel?: () => void }) {
  const kept = t.proposal?.kind === "remembered" ? t.proposal.as : undefined;
  const proposed = useDetail().transaction!.choices.proposed?.data[0];
  const offer = link ? undefined : proposed;
  // a bon only explains a debit nothing explains yet (its matcher skips the user's explanations), so not when changing one
  const debit = t.direction === "debit";
  const labels: Partial<Record<Kind, string>> = !debit ? KIND_LABEL : link ? { ...KIND_LABEL, documents: DEBIT_LABEL.documents } : DEBIT_LABEL;
  const [kind, setKind] = useState<Kind>(link?.kind ?? (debit && t.awaiting ? "await" : kept?.kind
    ?? (t.bank_says && !offer ? "label" : debit && t.card && !t.partner && !offer && !t.proposal ? "receipt" : "documents")));
  const first = useTakeFocus<HTMLInputElement>();
  const s = useCompany();
  const { run, busy } = useApp();
  // applied only by this tap; the sheet stays and shows the explanation, as after the forms below
  const confirm = (id: string) => void run(s.co, () => post("tx-link", { co: s.co, tx: t.id, invoices: id }), undefined,
    (r) => { if (r.ok) onDone(); return false; });
  return (
    <>
      {offer ? <Actions>
        <Button type="button" className="btn" disabled={busy} onClick={() => confirm(offer.id)}>{`Confirmă: factura ${offer.number}`}</Button>
      </Actions> : null}
      <div className={transactionFormStyle}><fieldset>
        <legend>Tip tranzacție</legend>
        {(Object.keys(labels) as Kind[]).map((k) => (
          <Label key={k} className={transactionChoiceStyle} htmlFor={`tx-k-${k}`}>
            <Input id={`tx-k-${k}`} ref={k === kind ? first : undefined} type="radio" name="tx-kind" checked={kind === k}
              onChange={() => setKind(k)} />
            {labels[k]}
          </Label>
        ))}
      </fieldset></div>
      {kind === "documents" ? <LinkForm t={t} link={link} onDone={onDone} onCancel={onCancel} />
        : kind === "receipt" ? <Receipt t={t} />
        : kind === "await" ? <Awaiting t={t} />
        : <MarkForm key={kind} t={t} kind={kind} link={link} onDone={onDone} onCancel={onCancel} />}
    </>
  );
}

/** Bon / chitanță: one photo, uploaded as this debit's bon, or a file already uploaded with no data of its own yet
 * (`tx-receipt`); either takes the debit's amount, day and card. */
function Receipt({ t }: { t: Transaction }) {
  const up = useExpenseUpload(undefined, t.id);
  const unread = useDetail().transaction?.choices.receipts?.data ?? [];
  return <>
    {up ? <Actions>
      {up.input}
      <Button type="button" className="btn" disabled={up.busy} aria-busy={up.busy} onClick={up.pick}>
        <I n="camera" />{up.busy ? "Se încarcă…" : "Încarcă bonul"}
      </Button>
    </Actions> : null}
    {unread.length ? <section aria-label="Bonuri încărcate fără date" className="grid gap-2">
      <h3 className={sectionHeadingStyle}>Sau alege un bon deja încărcat</h3>
      {unread.map((d) => <div key={d.id} className="flex flex-wrap items-center justify-between gap-2">
        <L to={{ doc: d.id }}>{`Bon încărcat în ${lunaName(d.month)}`}</L>
        <Act action="tx-receipt" body={{ tx: t.id, id: d.id }} label="Este bonul acestei plăți" cls="btn g" />
      </div>)}
    </section> : null}
  </>;
}

/** Aștept factura: the debit stays to explain, last in De explicat, with an optional note; it can stop waiting. */
function Awaiting({ t }: { t: Transaction }) {
  const [note, setNote] = useState("");
  if (t.awaiting) return <Actions>
    <Act action="tx-await" body={{ tx: t.id, on: "0" }} label="Nu mai aștept factura" cls="btn g" />
  </Actions>;
  return <div className={transactionFormStyle}>
    <Label htmlFor="tx-await">Ce factură aștepți (opțional)</Label>
    <Input id="tx-await" value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} autoComplete="off" />
    <Actions><Act action="tx-await" body={{ tx: t.id, on: "1", note: note.trim() }} label="Aștept factura" /></Actions>
  </div>;
}

const MARK: Record<Exclude<LinkKind, "documents">, string> = { label: "Marchează fără document", transfer: "Marchează ca transfer",
  advance: "Marchează ca avans" };

/** Fără document (a label and a note for the accountant), Transfer între conturi, Avans (who). */
function MarkForm({ t, kind, link, onDone, onCancel }: {
  t: Transaction; kind: Exclude<LinkKind, "documents">; link?: TxLink; onDone: () => void; onCancel?: () => void;
}) {
  const s = useCompany();
  const { run, busy } = useApp();
  const [was] = useState(link?.id ?? "");
  const kept = t.proposal?.kind === "remembered" && t.proposal.as.kind === kind ? t.proposal.as : undefined;
  const same = link?.kind === kind ? link : kept;
  const [label, setLabel] = useState<TransactionLabel | "">(kind === "label" ? (same?.reason as TransactionLabel | null) ?? t.bank_says ?? "" : "");
  const [text, setText] = useState(same?.note ?? "");
  // a Fără document or Avans rule applies on its own: saved unless unticked (the bank's own fee needs none)
  const [keep, setKeep] = useState(!t.bank_says);
  const by = kind === "transfer" ? undefined : keyFor(t, kind, label);
  const words = text.trim();
  const send = () => void run(s.co, () => post("tx-link", {
    co: s.co, tx: link?.tx ?? t.id, kind, reason: kind === "label" ? label : "", note: kind === "label" ? words : "",
    who: kind === "advance" ? words : "", was, remember: keep && by ? by : "",
  }), undefined, (r) => {
    if (r.ok) onDone();
    return false;
  });
  return (
    <Form className={transactionFormStyle} onSubmit={(e) => e.preventDefault()}>
      {kind === "label" ? (
        <fieldset>
          <legend>Fără document</legend>
          {(Object.keys(LABEL_NAME) as TransactionLabel[]).map((l) => (
            <Label key={l} className={transactionChoiceStyle} htmlFor={`tx-l-${l}`}>
              <Input id={`tx-l-${l}`} type="radio" name="tx-label" checked={label === l} onChange={() => setLabel(l)} />
              {LABEL_NAME[l]}
            </Label>
          ))}
          <Label htmlFor="tx-mark">Notă pentru contabil</Label>
          <Input id="tx-mark" value={text} onChange={(e) => setText(e.target.value)} maxLength={200} autoComplete="off" />
        </fieldset>
      ) : kind === "advance" ? (
        <>
          <Label htmlFor="tx-mark">Administratorul sau asociatul</Label>
          <Input id="tx-mark" value={text} onChange={(e) => setText(e.target.value)} maxLength={100} autoComplete="off" />
        </>
      ) : <p className={noteLeftStyle}>Cealaltă parte se marchează doar dacă se găsește fără ambiguitate în alt cont al firmei.</p>}
      {by ? <Keep t={t} by={by} on={keep} onChange={setKeep} auto /> : null}
      <Actions>
        <Button type="button" className="btn" disabled={busy} onClick={send}>{MARK[kind]}</Button>
        {onCancel ? <Button type="button" className="btn g" onClick={onCancel}>Renunță</Button> : null}
      </Actions>
    </Form>
  );
}

/** What an explanation is remembered by (fiscalm.links.memory_keys): a personal card by its card, else the account,
 * else the merchant; nothing for a transfer. */
function keyFor(t: Transaction, kind: LinkKind, label: TransactionLabel | ""): MemoryKey | undefined {
  if (kind === "label" && label === "personal" && t.remember.card) return "card";
  return t.remember.iban ? "iban" : t.remember.name ? "name" : undefined;
}

/** Ține minte: the next transactions of this account, card or merchant get the same explanation on their own
 * (`auto`: Fără document, Avans; also the ones still to explain) or as a proposal (an invoice: picking it stays a person's). */
function Keep({ t, by, on, onChange, auto = false }: { t: Transaction; by: MemoryKey; on: boolean; onChange: (on: boolean) => void; auto?: boolean }) {
  return (
    <div className="my-5 border-0 border-t border-solid border-line pt-4">
      <Label className={transactionChoiceStyle} htmlFor="tx-keep">
        <Input id="tx-keep" type="checkbox" checked={on} onChange={(e) => onChange(e.target.checked)} aria-describedby="tx-keep-about" />
        Salvează regula
      </Label>
      <p className={noteLeftStyle} id="tx-keep-about">{`${by === "name" && t.card && t.direction === "debit" ? "Comerciantul" : KEPT_BY[by]} ${t.remember[by]}: regula ${auto ? "explică automat tranzacțiile următoare și pe cele încă neexplicate" : "se propune la tranzacțiile următoare"}.`}</p>
    </div>
  );
}

/** What a remembered explanation proposes, in words. */
export function keptText(k: { kind: LinkKind; reason: string | null; note: string | null }, partner?: Partner): string {
  if (k.kind === "label") return `Fără document, ${LABEL_NAME[k.reason as TransactionLabel].toLowerCase()}`;
  if (k.kind === "advance") return `Avans, ${k.note ?? ""}`;
  return partner ? `De la ${partnerName(partner)}` : KIND_LABEL[k.kind];
}
