"use client";
import { Actions, Panel, amountStyle, detailListStyle, sectionHeadingStyle } from "./Controls";
import { curOf, dm, lei, nOf } from "@/lib/format";
import { type Settlement, type State } from "@/lib/types";
import { useCompany } from "./context";
import { CloseX, DetailSheet, SHEET_SHAPES, Sheet, SheetHead } from "./Sheets";
import { DetailPages } from "./DocumentDetail";
import { Act, L, Task } from "./ui";

export function SettlementTasks({ s, co }: { s: State; co?: string }) {
  return (s.settlements ?? []).filter((r) => r.status === "pending").map((r) => (
    <Task key={`${s.co}set${r.id}`} co={co} icon="bank" title={r.can_confirm ? "Decontare de confirmat" : "Decontare de verificat"}
      body={[`${nOf(r.invoice_count, "O factură", "facturi")}, net ${lei(r.net)} ${curOf(r.currency)}, de la ${r.payer.name}.`, details(r, s.today)].filter(Boolean).join(" ")}>
      {r.can_confirm && r.credit ? <Confirm r={r} /> : null}
      <L to={{ co: s.co, set: r.id }} className="btn g">Vezi decontarea</L>
    </Task>
  ));
}

export function CodTasks({ s, co }: { s: State; co?: string }) {
  return (s.cod?.tasks ?? []).map((t) => {
    const awb = s.cod?.rows.find((r) => r.id === t.id)?.awb.number;
    const returned = t.kind === "cod_returned";
    return <Task key={`${s.co}${t.kind}${t.id}`} co={co} late={!returned} icon={returned ? "arrow-counter-clockwise" : "clock"}
      title={returned ? "Colet returnat" : "Ramburs întârziat"}
      body={`${awb ? `AWB ${awb}. ` : ""}${returned ? "Verifică dacă ai nevoie de o factură de stornare." : "Rambursul nu este confirmat în extras după livrare."}`}>
      <L to={{ co: s.co, iss: t.invoice }} className="btn g">Vezi factura</L>
    </Task>;
  });
}

/** The person's answer to a waiting batch, on its card and its sheet alike: the engine re-checks the credit it names. */
function Confirm({ r }: { r: Settlement }) {
  return <Act action="settlement-confirm" body={{ id: r.id, tx: r.credit!.id, confirmed: "yes" }} label="Confirmă decontarea" />;
}

function details(r: Settlement, today: string) {
  if (r.status === "collected") return "Intrarea în cont apare în extras.";
  if (r.can_confirm && r.credit) return `Așteaptă confirmarea ta: intrare de ${lei(r.credit.amount)} ${curOf(r.credit.currency)} pe ${dm(r.credit.date, today)}.`;
  const reasons: Record<string, string> = {
    statement_unavailable: "Extrasul încărcat nu poate fi citit. Încarcă din nou extrasul corect.",
    statement_changed: "Extrasul s-a schimbat. Verifică noua intrare înainte de confirmare.",
    credit_unavailable: "Intrarea verificată nu mai este disponibilă. Verifică extrasul perioadei.",
    invoice_changed: "O factură s-a schimbat. Verifică facturile din raport.",
    report_changed: "Raportul s-a schimbat între timp. Verifică liniile înainte de confirmare.",
    confirmation_refused: "Intrarea din extras s-a schimbat între timp. Verifică intrarea înainte de confirmare.",
    report_unavailable: "Raportul nu mai este disponibil pentru confirmare.",
    credit_used: "Intrarea în cont este deja folosită pentru altă încasare.",
    competing_reports: "Mai multe rapoarte cer aceeași intrare. Șterge raportul greșit.",
    wrong_company: "Raportul și extrasul sunt pentru firme diferite.",
    invalid_report: "Raportul nu poate fi citit. Șterge-l și încarcă raportul corect.",
  };
  if (r.failed_line) return `Linia ${r.failed_line}: ${lineError(r)}`;
  if (r.error === "credit_missing_or_ambiguous" && r.candidate) {
    if (r.candidate.amount === r.net) return "Mai multe intrări pot corespunde raportului. Verifică extrasul.";
    return `Suma netă ${lei(r.net)} ${curOf(r.currency)} nu se potrivește; cea mai apropiată: ${lei(r.candidate.amount)} ${curOf(r.candidate.currency)} pe ${dm(r.candidate.date, today)}, de la ${r.candidate.payer}.`;
  }
  if (r.error === "credit_missing_or_ambiguous") return "Nicio intrare de la acest partener în cele 14 zile de la raport. Încarcă extrasul perioadei.";
  return reasons[r.error || ""] || "Raportul nu poate fi confirmat. Șterge-l și încarcă raportul corect.";
}

/** Why a report line names no single open invoice, with the value it carries when this page of lines has it. */
function lineError(r: Settlement) {
  const line = r.lines.find((l) => l.position === r.failed_line), error = r.error ?? "";
  if (error.endsWith("awb_unknown")) return `${line?.awb ? `AWB ${line.awb}` : "AWB-ul"} nu corespunde niciunei comenzi.`;
  if (error.endsWith("order_unknown")) return `${line?.order ? `comanda ${line.order}` : "comanda"} nu există în comenzile firmei.`;
  if (error.endsWith("invoice_missing_or_ambiguous")) return !line?.reference ? "factura nu poate fi găsită."
    : line.awb || line.order ? `factura ${line.reference} nu există, apare de mai multe ori sau nu e a acestei comenzi.`
      : `factura ${line.reference} nu există sau numărul apare la mai multe facturi.`;
  return error.endsWith("invalid_invoice") ? "factura are date nevalide." : "factura nu este deschisă sau suma diferă.";
}

export function SettlementSheet({ id, onClose }: { id: string; onClose: () => void }) {
  return <DetailSheet kind="settlement" id={id} onClose={onClose}><SettlementBody id={id} onClose={onClose} /></DetailSheet>;
}

function SettlementBody({ id, onClose }: { id: string; onClose: () => void }) {
  const s = useCompany();
  const r = s.settlements!.find((x) => x.id === id)!;
  return <Sheet label="Decontare" cls={SHEET_SHAPES.settlement.cls} onClose={onClose}>
    <CloseX onClose={onClose} />
    <SheetHead title="Decontare" sub={<span className="font-normal text-muted">{r.partner ? <L to={{ co: s.co, p: r.partner }}>{r.payer.name}</L> : r.payer.name}</span>} />
    <Panel>
      <p className="mt-0 mb-2 text-sm text-muted">Total net</p>
      <div className={amountStyle}>{lei(r.net)} <span className="cur">{curOf(r.currency)}</span></div>
      <p className="mt-2 mb-0 text-sm text-muted">{nOf(r.invoice_count, "o factură", "facturi")}</p>
    </Panel>
    <Panel className="my-4">
      <dl className={detailListStyle}>
        {r.reference && <><dt>Referință</dt><dd>{r.reference}</dd></>}
        <dt>Comision curier sau procesator</dt><dd>{lei(r.fee)} {curOf(r.currency)}</dd>
      </dl>
      <p className="mt-4 mb-0 text-sm leading-relaxed">{details(r, s.today)}</p>
    </Panel>
    <div className={sectionHeadingStyle}><h3 className="m-0 text-base font-semibold">Facturile din raport</h3></div>
    <Panel padded={false}>
      <DetailPages />
      <ul className="m-0 list-none divide-x-0 divide-y divide-solid divide-line p-0">
        {r.lines.map((line) => <li className="flex items-start justify-between gap-4 p-4 text-sm leading-relaxed" key={line.position}>
          <span className="min-w-0 wrap-anywhere">{line.invoice ? <L to={{ co: s.co, iss: line.invoice }}>Factura {line.reference || line.order || line.awb || "emisă"}</L> : line.reference || line.awb || line.order}</span>
          <span className="shrink-0 tabular-nums">{lei(line.gross)} {curOf(r.currency)}</span>
        </li>)}
      </ul>
    </Panel>
    {r.can_confirm && r.credit && <Actions><Confirm r={r} /></Actions>}
    {!r.can_confirm && r.status === "pending" && <Actions><Act action="settlement-withdraw" body={{ id: r.id }}
      label="Șterge raportul" cls="btn g" confirm={{ text: "Ștergi raportul greșit? Facturile rămân neîncasate.", yes: "Șterge raportul", danger: true }} /></Actions>}
    {(r.tx_key || r.credit?.id || r.candidate?.id) && <p><L to={{ co: s.co, tx: r.tx_key || r.credit?.id || r.candidate?.id }}>Vezi tranzacția</L></p>}
    {r.status === "collected" && <p>{r.human_confirmed ? "Decontare confirmată." : "Decontare confirmată automat, pe baza raportului și a extrasului."}</p>}
  </Sheet>;
}
