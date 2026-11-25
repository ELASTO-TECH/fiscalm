"use client";
// Bottom sheets: a received invoice, an issued invoice, a mail to place, a receipt, the success sheet after approve.
import { formTypography, fadeOut, motion, reveal, revealAfter, ActionColumn, Button, Form, Label, Input, Panel, Actions, Skeleton, SkeletonRows, controlHeight, detailListStyle, sectionHeadingStyle, amountStyle, noteLeftStyle, statePillStyle, statusStyle, statusTextStyle, legendStyle } from "./Controls";
import { Fragment, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type Ref } from "react";
import { actionRole, docFile, mailFile } from "@/lib/api";
import { AddToFile, useCanFile } from "./AddToFile";
import { curOf, dm, dmt, docAmount, docName, isRon, lei, lunaName, phrase, UNKNOWN_AMOUNT } from "@/lib/format";
import { PRODUCT } from "@/lib/product";
import { coName, type CardInvoice, type CollectedMethod, type DetailKind, type Invoice, type Issued } from "@/lib/types";
import { DocumentDetail, DetailPages } from "./DocumentDetail";
import { useApp, useCan, useCompany } from "./context";
import { expenseSource, maskedIban, bonOf, supplierInactive, findIssued, invoiceOf, issuedState, receiptState, receivedState, refusalOf, refusalReason, refusedNow } from "./derive";
import { InvoiceLines } from "./InvoiceLines";
import { cardName, COLLECTED_METHODS, collectedOf, MarkPaid, MethodList, methodOf } from "./MarkPaid";
import { DocumentFields } from "./DocumentFields";
import { RefusedAccount } from "./SupplierAccount";
import { MailAnswer, questionLine } from "./Tabs";
import { IssuedDownloads, IssuedFiles, IssuedSpv } from "./Issued";
import { PartnerLink } from "./Partners";
import { Act, Big, I, L, Off } from "./ui";

/** A sheet's own text sets paragraphs flush (formTypography); a remark under its buttons keeps a gap. */
const actionNote = `${noteLeftStyle} mt-3!`;
const sheetTitleStyle = "m-0 text-lg! leading-7! font-medium! tracking-tight wrap-anywhere";


/** A sheet rises from the bottom of a phone; in a wide window it is a panel on the right (`wide`: a broader one, for an
 * editor or an invoice's table of lines) or, for
 * Caută (`find`) and a confirmation (`ask`, above any other sheet; `ask tall` for a longer form), a dialog in the middle;
 * `full`: as tall as a phone allows whatever it holds, for a sheet whose length depends on its data (SHEET_SHAPES);
 * `page` (Factură nouă, Contra's create screen) takes the whole window. Its surface is opaque in both schemes. */
const sheetStyle = "fixed z-[9] overflow-auto overscroll-contain border border-solid border-line bg-surface shadow-pop px-6 pt-6 min-[1100px]:pt-8 text-ink outline-none [&_.hd]:pr-12";
/** It enters from the bottom edge of a phone; in a wide window a panel or a dialog rises a few pixels as it fades in, Caută
 * grows like a menu and a page fades in. */
const desk = { rise: "min-[1100px]:motion-safe:animate-enter-rise", pop: "min-[1100px]:motion-safe:animate-enter-pop", fade: "min-[1100px]:motion-safe:animate-enter-fade" };
const PHONE = `${motion.sheet} inset-x-0 bottom-0 mx-auto max-h-[92vh] rounded-t-panel border-b-0 pb-[calc(1.5rem+env(safe-area-inset-bottom))]`;
function placement(cls: string[]): string {
  if (cls.includes("find")) return `${PHONE} ${desk.pop} min-h-[70vh] max-w-[620px] min-[1100px]:top-[12vh] min-[1100px]:bottom-auto min-[1100px]:min-h-0 min-[1100px]:max-h-[72vh] min-[1100px]:w-[640px] min-[1100px]:max-w-[calc(100vw-32px)] min-[1100px]:rounded-panel min-[1100px]:border-b min-[1100px]:pb-6`;
  if (cls.includes("ask")) return `${PHONE} ${desk.rise} z-[11]! max-w-[620px] ${cls.includes("tall") ? "min-[1100px]:top-[8vh] min-[1100px]:max-h-[84vh]" : "min-[1100px]:top-[30vh]"} min-[1100px]:bottom-auto min-[1100px]:w-[520px] min-[1100px]:max-w-[calc(100vw-32px)] min-[1100px]:rounded-panel min-[1100px]:border-b min-[1100px]:pb-6`;
  if (cls.includes("page")) return `${PHONE} ${desk.fade} max-w-[620px] min-[1100px]:inset-0 min-[1100px]:max-w-none min-[1100px]:max-h-none min-[1100px]:rounded-none min-[1100px]:border-0 min-[1100px]:px-[30px] min-[1100px]:pb-8`;
  const width = cls.includes("wide") ? "max-w-[880px] min-[1100px]:w-[880px]" : "max-w-[620px] min-[1100px]:w-[480px]";
  const tall = cls.includes("full") ? "max-[1099px]:min-h-[92vh]" : "";
  return `${PHONE} ${desk.rise} ${width} ${tall} min-[1100px]:inset-y-0 min-[1100px]:right-0 min-[1100px]:left-auto min-[1100px]:max-h-none min-[1100px]:max-w-[calc(100vw-32px)] min-[1100px]:rounded-none min-[1100px]:border-y-0 min-[1100px]:border-r-0 min-[1100px]:pb-6`;
}

const FOCUSABLE = 'a[href],button,input,select,textarea,summary,[tabindex]';

/** Keep replacement layouts inert too, without resetting the sheet's opener or keyboard position. */
function inertBackground(sheet: HTMLElement): () => void {
  const inerted = new Set<HTMLElement>();
  const restore = () => { for (const el of inerted) el.removeAttribute("inert"); inerted.clear(); };
  const hide = () => {
    if (document.querySelector(".auth-checking")) { restore(); return; }
    if (sheet.closest("[inert]")) return;
    for (const el of sheet.parentElement?.children ?? []) {
      if (el instanceof HTMLElement && el !== sheet && !el.classList.contains("scrim") && !el.matches('[role="status"]') && !el.hasAttribute("inert")) {
        el.setAttribute("inert", "");
        inerted.add(el);
      }
    }
  };
  hide();
  const observer = new MutationObserver(hide);
  observer.observe(document.body, { childList: true, subtree: true });
  return () => {
    observer.disconnect();
    restore();
  };
}

/** Sheets being removed right now, and how many sheets have opened so far. A sheet that opens while another is being
 * removed takes its place (its content read, a step further): it doesn't enter again, and the old one doesn't fade out
 * over it. StrictMode's second run of the same sheet counts as neither. */
const leaving = new Set<HTMLElement>();
let opened = 0;

/** Scrim and dialog. It enters (Controls.tsx `motion`) and fades out as it closes (`fadeOut`). Focus moves in on open (to its `data-autofocus` element if it has one) and back to the opener on close; Tab and Shift+Tab stay inside; the rest
 * of the page is inert (the toast region stays live); Escape closes. The layout effect captures the opener before child
 * passive effects can move focus. Runs once per sheet: a new onClose is read
 * through a ref, so re-renders never reset the focus. */
export function Sheet({ onClose, cls = "", label, busy = false, children }: { onClose: () => void; cls?: string; label: string; busy?: boolean; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null), scrim = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);
  useLayoutEffect(() => {
    const sheet = ref.current;
    if (!sheet) return;
    if (!leaving.delete(sheet)) {
      opened++;
      if (leaving.size) {
        for (const el of [sheet, scrim.current]) el?.style.setProperty("animation", "none");
        reveal(sheet.children);
      }
    }
    const opener = document.activeElement as HTMLElement | null;
    const restore = inertBackground(sheet);
    (sheet.querySelector<HTMLElement>("[data-autofocus]") ?? sheet).focus({ preventScroll: true });
    const key = (e: KeyboardEvent) => {
      if (sheet.closest("[inert]") || document.querySelector(".auth-checking")) return;
      // An Escape a control inside already used (an open Select's list) closes only that.
      if (e.key === "Escape" && !e.defaultPrevented) {
        closeRef.current();
        return;
      }
      if (e.key !== "Tab") return;
      const els = [...sheet.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.tabIndex >= 0 && !el.matches(":disabled, input[type=hidden]") && !el.closest("[inert], [hidden], details:not([open]) > :not(summary)"));
      if (!els.length) {
        e.preventDefault();
        sheet.focus();
        return;
      }
      const [first, last] = [els[0], els[els.length - 1]];
      const at = document.activeElement;
      if (!sheet.contains(at)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && (at === first || at === sheet)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && at === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", key);
    const shade = scrim.current;
    return () => {
      document.removeEventListener("keydown", key);
      restore();
      if (opener?.isConnected) opener.focus({ preventScroll: true });
      leaving.add(sheet);
      queueMicrotask(() => leaving.delete(sheet));
      const seen = opened;
      fadeOut([shade, sheet], sheet.parentElement, ["sheet", "scrim"], () => opened !== seen);
    };
  }, []);
  // A sheet up to 480px wide is a phone-width column on every screen: its buttons too (wide and search sheets keep rows).
  const narrow = !cls.includes("wide") && !cls.includes("find");
  return (
    <>
      <div ref={scrim} className={`scrim ${motion.fade} fixed inset-0 ${cls.includes("ask") ? "z-[10]" : "z-[8]"} cursor-pointer bg-black/55 min-[1100px]:bg-black/30`} onClick={() => closeRef.current()} aria-hidden="true" />
      <div className={`sheet ${cls} ${sheetStyle} ${placement(cls.split(" "))} ${formTypography}`} role="dialog" aria-modal="true" aria-label={label} aria-busy={busy} tabIndex={-1} ref={ref}>
        {narrow ? <ActionColumn>{children}</ActionColumn> : children}
      </div>
    </>
  );
}

/** The sheet's close button: no box of its own, a grey only on hover. It never sits over content: CloseX gives it a row of
 * its own at the top (the sheet's head, SheetHead, rises onto that line and leaves it room; a head with an amount only from
 * 640px, where a long amount still fits beside it); a sheet whose first row is its
 * own header (Caută's field) puts it at that row's end instead. */
export function CloseButton({ onClose }: { onClose: () => void }) {
  return (
    <button type="button" className="x grid size-11 cursor-pointer place-items-center rounded-control border-0 bg-transparent p-0 text-muted hover:bg-inset hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus [&_svg]:size-[18px]" onClick={onClose} aria-label="Închide">
      <I n="x" />
    </button>
  );
}

export function CloseX({ onClose }: { onClose: () => void }) {
  return (
    <div className="closex -mx-6 -mt-6 mb-1 flex justify-end px-3 pt-3 min-[1100px]:-mt-8 min-[1100px]:pt-5"><CloseButton onClose={onClose} /></div>
  );
}

/** Every sheet's head: its amount (`sign`ed, in `cur`) or its title, then whom it concerns, a partner's link when the engine
 * knows one (`sub`). `titleRef` makes the title a focus target. */
export function SheetHead({ amount, cur = "", sign = "", title, sub, titleRef }: {
  amount?: string; cur?: string; sign?: string; title?: ReactNode; sub?: ReactNode; titleRef?: Ref<HTMLHeadingElement>;
}) {
  return (
    <header className={headStyle(amount !== undefined)}>
      {amount !== undefined ? <div className={heroStyle}>{sign}<Big a={amount} /><span className="cur">{cur}</span></div>
        : <h2 className={sheetTitleStyle} tabIndex={titleRef ? -1 : undefined} ref={titleRef}>{title}</h2>}
      {sub ? <div className={subStyle}>{sub}</div> : null}
    </header>
  );
}
const headStyle = (amount: boolean) => `mb-5 min-w-0 ${amount ? "sm:[.closex+&]:-mt-10 sm:[.closex+&]:pr-14" : "[.closex+&]:-mt-10 [.closex+&]:pr-14"}`;
/** The same rise for a skeleton, whose head is inside its own box: the box rises, the head leaves the room. */
const skeletonRise = (amount: boolean) => (amount ? "sm:[.closex+&]:-mt-10 sm:[&>header]:pr-14" : "[.closex+&]:-mt-10 [&>header]:pr-14");
const heroStyle = `${amountStyle} text-4xl! [&_.dec]:text-2xl! [overflow-wrap:anywhere]`;
const subStyle = "who mt-1 text-sm font-semibold leading-relaxed text-ink wrap-anywhere";

/** What a sheet holds before it is read, in the order it shows it, so its skeleton has the answer's size and place.
 * `cls`: the sheet's placement (Sheet), so a wide sheet opens wide. `sub`: lines under the head (a third one only on a
 * phone, where they wrap more). `panels`: cards of detail rows, one row each; `facts`: one card of facts; `list`: a card
 * of list rows, under a section heading when `heading`; `fields`: a form's labelled fields; `answer`: a mail's question
 * (its checkbox and buttons); `total`: a card with a total (a settlement's net); `doc`: a document's first page, as on its own sheet (`file`) or a mail's. A sheet whose length
 * depends on its data (a partner's invoices, an invoice's lines) is `full` (Sheet), so its skeleton is as tall. */
export type SheetShape = {
  cls?: string; head: "amount" | "title" | "none"; sub?: number; pill?: boolean; note?: boolean; total?: boolean; actions?: number; answer?: number; doc?: "file" | "mail";
  panels?: number[]; facts?: number; heading?: boolean; list?: number; fields?: number;
};
export const SHEET_SHAPES = {
  invoice: { cls: "wide full", head: "amount", sub: 1, pill: true, actions: 2, panels: [5] },
  issued: { cls: "wide full", head: "amount", sub: 1, pill: true, actions: 1, panels: [4, 3] },
  transaction: { cls: "full", head: "amount", sub: 1, panels: [3] },
  document: { cls: "full", head: "amount", sub: 1, doc: "file", panels: [2] },
  mail: { cls: "full", head: "amount", sub: 1, note: true, answer: 3, doc: "mail", panels: [3] },
  settlement: { cls: "full", head: "title", sub: 1, total: true, facts: 2, heading: true, list: 2 },
  order: { head: "title", panels: [1], actions: 2 },
  partner: { cls: "wide full", head: "title", sub: 1, facts: 3, heading: true, list: 3 },
  file: { cls: "tall full", head: "title", sub: 3, list: 4 },
  product: { head: "title", sub: 1, fields: 6 },
  form: { head: "none", fields: 8 },
} as const satisfies Record<DetailKind | "partner" | "file" | "product" | "form", SheetShape>;

/** One line of text not read yet: the shape sits in a line box of the text's own typography (`cls`), so it is as tall. */
function Line({ cls = "", w = "w-3/5" }: { cls?: string; w?: string }) {
  return <div className={cls}><Skeleton className={`inline-block! h-[0.85em] align-middle ${w}`} /></div>;
}
const rowCls = `${detailListStyle} py-2`;
const buttons = (n: number) => Array.from({ length: n }, (_, i) => <Skeleton key={i} className={`${controlHeight} min-w-32 rounded-full`} />);

/** A sheet's content before it is read (`shape`, SHEET_SHAPES), drawn with the answer's own blocks: the head in the hero's
 * line height, the state pill, buttons at their height, detail rows, a document's first page in A4 proportions. */
export function SheetSkeleton({ shape }: { shape: SheetShape }) {
  const { head, sub = 0, panels = [], facts = 0, list = 0, fields = 0, actions = 0, answer = 0 } = shape;
  return <div ref={revealAfter} role="status" className={head === "none" ? "" : skeletonRise(head === "amount")}>
    <span className="sr-only">Se încarcă…</span>
    {head !== "none" ? <header className={headStyle(head === "amount")}>
      {head === "amount" ? <Line cls={heroStyle} w="w-40" /> : <Line cls={sheetTitleStyle} w="w-48" />}
      {Array.from({ length: sub }, (_, i) => <Line key={i} cls={`${subStyle} ${i ? "mt-0!" : ""} ${i > 1 ? "min-[1100px]:hidden" : ""}`} w={i ? "w-4/5" : "w-44"} />)}
    </header> : null}
    {shape.pill ? <Skeleton className="mt-0 mb-4 h-7 w-52 rounded-badge" /> : null}
    {shape.note ? <Line cls={`${noteLeftStyle} leading-5!`} w="w-4/5" /> : null}
    {actions ? <Actions>{buttons(actions)}</Actions> : null}
    {answer ? <div className="my-4 space-y-4"><Line cls="text-[15px] leading-[23px]" w="w-56" /><Actions>{buttons(answer)}</Actions></div> : null}
    {shape.doc === "file" ? <div className="my-5"><Skeleton className="aspect-[210/297] max-h-[70vh] w-full" /><Line cls="mt-2 text-center text-sm leading-relaxed" w="mx-auto w-20" /></div>
      : shape.doc ? <Skeleton className="my-4 aspect-[210/297] max-h-[60vh] w-full" /> : null}
    {shape.total ? <Panel><Line cls="leading-5" w="w-20" /><Line cls={amountStyle} w="w-36" /><Line cls="leading-5" w="w-24" /></Panel> : null}
    {panels.map((n, p) => <Panel key={p} className="my-4">
      {Array.from({ length: n }, (_, i) => <dl key={i} className={rowCls}><dt><Line w="w-20" /></dt><dd><Line /></dd></dl>)}
    </Panel>)}
    {facts ? <Panel className="card my-4"><dl className={detailListStyle}>
      {Array.from({ length: facts }, (_, i) => <Fragment key={i}><dt><Line w="w-20" /></dt><dd><Line w="w-2/5" /></dd></Fragment>)}
    </dl></Panel> : null}
    {list ? <>
      {shape.heading ? <div className={sectionHeadingStyle}><Line cls="text-[21px] leading-7" w="w-44" /></div> : null}
      <Panel padded={false} className="card my-4"><SkeletonRows rows={list} quiet /></Panel>
    </> : null}
    {Array.from({ length: fields }, (_, i) => <div key={i} className="mb-4"><Line cls={`${legendStyle} mb-1.5!`} w="w-28" /><Skeleton className={`${controlHeight} w-full rounded-field`} /></div>)}
  </div>;
}

/** A sheet whose content is being read (`loading`) or could not be (`status`, named by `failed`): the loaded sheet's
 * placement and shape. */
export function SheetLoading({ shape, loading, status, failed = "Datele nu au putut fi încărcate.", onClose }: {
  shape: SheetShape; loading: boolean; status: ReactNode; failed?: string; onClose: () => void;
}) {
  return <Sheet onClose={onClose} cls={shape.cls} busy={loading} label={loading ? "Se încarcă…" : failed}>
    <CloseX onClose={onClose} />{loading ? <SheetSkeleton shape={shape} /> : status}
  </Sheet>;
}

export function DetailSheet({ kind, id, onClose, children }: { kind: DetailKind; id: string; onClose: () => void; children: ReactNode }) {
  return <DocumentDetail kind={kind} id={id} pending={(status, loading) => <SheetLoading shape={SHEET_SHAPES[kind]} loading={loading} status={status} onClose={onClose} />}>{children}</DocumentDetail>;
}

export function InvoiceSheet({ id, onClose }: { id: string; onClose: () => void }) {
  return <DetailSheet kind="invoice" id={id} onClose={onClose}><ReceivedSheet id={id} onClose={onClose} /></DetailSheet>;
}

/** How the person said they paid, in full: "Cu cardul firmei, card •• 4821", "Din banii mei: Ana Pop". */
function paidHow(r: Invoice): string {
  const title = methodOf(r.paid_method!).title;
  if (!r.paid_detail) return title;
  return r.paid_method === "card" ? `${title}, ${cardName(r.paid_detail).toLowerCase()}` : `${title}: ${r.paid_detail}`;
}

/** "Marchează ca plătită" opens "Cum ai plătit?" for this invoice; closing it without sending changes nothing. */
function MarkPaidButton({ r }: { r: Invoice }) {
  const [open, setOpen] = useState(false);
  const allowed = useCan(actionRole(r.status === "linked" ? "withdraw" : "mark-paid"));
  const { busy } = useApp();
  if (!allowed) return null;
  return <>
    <Button type="button" className="btn g" disabled={busy} aria-haspopup="dialog" onClick={() => setOpen(true)}>Marchează ca plătită</Button>
    {open ? <MarkPaid invoices={[r]} onDone={() => setOpen(false)} /> : null}
  </>;
}

/** "Pune în fișier" for this invoice: the same chooser as Cheltuieli's bar. */
function PutInFile({ r }: { r: Invoice }) {
  const s = useCompany(); const { busy } = useApp();
  const [open, setOpen] = useState(false);
  return <>
    <Button type="button" className="btn" disabled={busy} aria-haspopup="dialog" onClick={() => setOpen(true)}>Pune în fișier</Button>
    {open ? <AddToFile s={s} invoices={[r]} onDone={() => setOpen(false)} /> : null}
  </>;
}

function ReceivedSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const s = useCompany();
  const canFile = useCanFile(s);
  const r = s.invoices.find((x) => x.id === id)!;
  const { today } = s;
  // The bon of the same purchase, as the engine groups them ("Factură și bon"): shown on this sheet, with a way to its own.
  const bon = bonOf(s, id);
  // Autopay left it out (an account no statement or administrator vouched for, or unclear data): why, and what to do.
  const refused = refusalOf(s, id);
  // The state as the lists show it, in the same words and colour (derive's table).
  const [c, label, detail] = receivedState(r, today);
  const state = phrase(label, detail);
  const ron = isRon(r.currency);
  const cur = curOf(r.currency);
  let note = "";
  let btns: ReactNode = null;
  if (r.status === "marked") {
    if (r.can_reopen) {
      btns = <Actions><Act action="reopen" body={{ id }} label="Anulează marcarea" cls="btn g" /></Actions>;
    }
  } else if (r.status === "linked") {
    if (!r.can_withdraw && r.payment_state === "imported") note = "Dacă nu se mai plătește, șterge plata din aplicația băncii.";
    if (r.can_withdraw || r.payment_file) {
      btns = <Actions>{r.can_withdraw ? <MarkPaidButton r={r} /> : null}
        {r.payment_file ? <L className="btn g" to={{ co: r.company, tab: "plati", pf: String(r.payment_file.id) }}>Vezi fișierul</L> : null}</Actions>;
    }
  } else if (r.status === "open" && ron) {
    // Pune în fișier follows the shared mode policy; where it can't be used, it says why.
    const files = canFile;
    const inactive = files && supplierInactive(s, r);
    btns = (
      <>
        <Actions>
          {files && r.can_approve ? <PutInFile r={r} />
            : files && refusedNow(refused) ? <Off cls="btn" why={refusalReason(s, refused)}>Pune în fișier</Off>
            : inactive ? <Button type="button" className="btn" disabled>Pune în fișier</Button> : null}
          {r.can_mark_paid ? <MarkPaidButton r={r} /> : null}
        </Actions>
        {!r.can_approve && inactive && !refusedNow(refused) ? (
          <p className={actionNote} role="note">
            {`Factura nu poate intra în fișierul de plăți. ${inactive}`}
          </p>
        ) : null}
      </>
    );
  } else if (r.status === "open") {
    if (r.can_mark_paid) {
      btns = <Actions><MarkPaidButton r={r} /></Actions>;
    }
    note = "Fișierul de plăți e doar în lei.";
  }
  return (
    <Sheet onClose={onClose} cls={SHEET_SHAPES.invoice.cls} label={`${r.supplier}, ${lei(r.total)} ${cur}`}>
      <CloseX onClose={onClose} />
      <SheetHead amount={r.total} cur={cur} sub={<PartnerLink id={r.partner} name={r.supplier} cui={r.supplier_cui} />} />
      <StateLine tone={c}>{state}</StateLine>
      {r.status === "marked" && r.paid_method ? <p className={noteLeftStyle}>{methodOf(r.paid_method).proof}</p> : null}
      {refused && r.status === "open" ? <RefusedAccount x={refused} /> : null}
      {r.previous_iban_last4?.length ? <p className={noteLeftStyle}>IBAN diferit de cel plătit anterior ({r.previous_iban_last4.map(maskedIban).join(", ")})</p> : null}
      {btns}
      {note ? <p className={noteLeftStyle}>{note}</p> : null}
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Număr</dt><dd>{r.number}</dd></dl>
        <dl className={`${detailListStyle} py-2`}><dt>Data emiterii</dt><dd>{dm(r.issue_date, today)}</dd></dl>
        <dl className={`${detailListStyle} py-2`}><dt>Scadență</dt><dd>{dm(r.due_date, today) || "-"}</dd></dl>
        {r.paid_method ? <dl className={`${detailListStyle} py-2`}><dt>Plătită</dt><dd>{paidHow(r)}</dd></dl> : null}
        {ron ? <dl className={`${detailListStyle} py-2`}><dt>Cont plătitor</dt><dd>{coName(s)}, contul în lei</dd></dl> : null}
        <dl className={`${detailListStyle} py-2`}><dt>Sursă</dt><dd>{expenseSource(s, "invoice", r.id)}</dd></dl>
      </Panel>
      <InvoiceLines />
      {r.credits?.length && r.status === "linked" ? <p className={noteLeftStyle}>Factura are o factură de stornare. Verifică în bancă plata din fișier înainte să plătești din nou.</p> : null}
      {r.credits?.length ? <Panel className="my-4">
        {r.credits.map((credit) => <dl className={`${detailListStyle} py-2`} key={credit.id}><dt>Storno {credit.number}</dt><dd>{lei(credit.amount)} {cur}</dd></dl>)}
        {(!r.covered || r.covered === "0.00") ? <dl className={`${detailListStyle} py-2`}><dt>Rest de plată</dt><dd>{lei(r.left)} {cur}</dd></dl> : null}
      </Panel> : null}
      <DetailPages />
      {r.credit_note ? <p className={noteLeftStyle}>Factura stornată nu a fost identificată. Verifică referința.</p> : null}
      {r.covered && r.covered !== "0.00" ? (
        <Panel className="my-4">
          <dl className={`${detailListStyle} py-2`}><dt>Plătit</dt><dd>{lei(r.covered)} {cur}</dd></dl>
          <dl className={`${detailListStyle} py-2`}><dt>Rest de plată</dt><dd>{lei(r.left)} {cur}</dd></dl>
        </Panel>
      ) : null}
      {bon ? <DocFiles d={bon} small /> : null}
      {bon ? <Actions><L to={{ doc: bon.id }} className="btn g">Vezi bonul</L></Actions> : null}
    </Sheet>
  );
}

/** A mailed document in doubt is nobody's yet: under Toate firmele it opens with the first company that is one of its
 * answers (its buttons name the firms). */
export function MailSheet({ mailKey, onClose }: { mailKey: string; onClose: () => void }) {
  return <DetailSheet kind="mail" id={mailKey} onClose={onClose}><MailBody mailKey={mailKey} onClose={onClose} /></DetailSheet>;
}

function MailBody({ mailKey, onClose }: { mailKey: string; onClose: () => void }) {
  const s = useCompany();
  const m = s.to_check.find((x) => x.key === mailKey);
  if (!m) return null;
  const title = m.reason === "upload-firm" ? "Pentru ce firmă e documentul?" : m.supplier || m.subject || "Document din e-mail";
  const fileUrl = (png = false) => m.reason === "upload-firm" && m.file ? docFile(s.co, { id: m.key, type: m.file === "pdf" ? "pdf" : "image" }, png) : mailFile(s.co, m.key, png);
  const one = m.totals.length === 1 ? m.totals[0] : null;
  return (
    <Sheet onClose={onClose} cls={SHEET_SHAPES.mail.cls} label={title}>
      <CloseX onClose={onClose} />
      <DetailPages />
      {one ? <SheetHead amount={one.amount} cur={curOf(one.currency)} sub={title} /> : <SheetHead title={title} />}
      {m.reason !== "upload-firm" && <p className={noteLeftStyle}>{questionLine(m)}</p>}
      <div className="my-4"><MailAnswer m={m} /></div>
      {m.file ? (
        <a href={fileUrl()} target="_blank" rel="noopener noreferrer">
          {/* eslint-disable-next-line @next/next/no-img-element -- a token-scoped URL from the engine, not an optimisable asset */}
          <img src={fileUrl(true)} alt={m.reason === "upload-firm" ? "Documentul încărcat" : `Documentul de la ${title}`} loading="lazy"
            className="my-4 block max-h-[60vh] w-full rounded-control bg-inset object-contain" />
        </a>
      ) : null}
      <Panel className="my-4">
        {m.reason !== "upload-firm" && <dl className={`${detailListStyle} py-2`}><dt>De la</dt><dd>{m.sender}</dd></dl>}
        {m.reason !== "upload-firm" && <dl className={`${detailListStyle} py-2`}><dt>Subiect</dt><dd>{m.subject || "-"}</dd></dl>}
        <dl className={`${detailListStyle} py-2`}><dt>Primit</dt><dd>{dmt(m.at, s.today)}</dd></dl>
        {m.date ? <dl className={`${detailListStyle} py-2`}><dt>Data documentului</dt><dd>{dm(m.date, s.today)}</dd></dl> : null}
        {m.number ? <dl className={`${detailListStyle} py-2`}><dt>Număr</dt><dd>{m.number}</dd></dl> : null}
      </Panel>
    </Sheet>
  );
}

/** A receipt paid with the company card: its photo or its PDF's first page, large;
 * a tap opens the file itself. */
export function DocSheet({ id, onClose }: { id: string; onClose: () => void }) {
  return <DetailSheet kind="document" id={id} onClose={onClose}><DocBody id={id} onClose={onClose} /></DetailSheet>;
}

/** A receipt's files, each opening the file itself: large on its own sheet, `small` under the invoice of the same purchase. */
function DocFiles({ d, small }: { d: CardInvoice; small?: boolean }) {
  const s = useCompany();
  return (
    <div className={`my-5 grid items-start gap-3 ${small ? "grid-cols-[repeat(auto-fit,minmax(min(100%,150px),1fr))]" : "grid-cols-[repeat(auto-fit,minmax(min(100%,220px),1fr))]"}`}>
      {d.files.map((f) => (
        <figure key={f.id} className="m-0 min-w-0">
          <a href={docFile(s.co, f)} target="_blank" rel="noopener noreferrer">
            {/* eslint-disable-next-line @next/next/no-img-element -- a token-scoped URL from the engine, not an optimisable asset */}
            <img src={docFile(s.co, f, f.type === "pdf")} alt={`Document, ${docName(d, s.today)}`} loading="lazy"
              className={`block w-full rounded-control bg-inset object-contain ${small ? "max-h-[260px]" : "max-h-[70vh]"}`} />
          </a>
          <figcaption className="mt-2 text-sm leading-relaxed text-muted wrap-anywhere" style={{ textAlign: "center" }}>Document</figcaption>
        </figure>
      ))}
    </div>
  );
}

function DocBody({ id, onClose }: { id: string; onClose: () => void }) {
  const s = useCompany();
  const d = s.documents.find((x) => x.id === id)!;
  const cur = curOf(d.currency);
  const invoice = invoiceOf(s, id);
  const name = docName(d, s.today);
  return (
    <Sheet onClose={onClose} cls={SHEET_SHAPES.document.cls} label={`${name}, ${docAmount(d)}`}>
      <CloseX onClose={onClose} />
      <DetailPages />
      {d.amount === null ? <SheetHead title={UNKNOWN_AMOUNT} sub={name} /> : <SheetHead amount={d.amount} cur={cur} sub={name} />}
      <DocumentFields d={d} preview={<DocFiles d={d} />} />
      {invoice ? <Actions><L to={{ inv: invoice.id }} className="btn g">Vezi factura</L></Actions> : null}
      {d.files.length ? <DocFiles d={d} /> : (
        <p className={noteLeftStyle}>Fișierul lipsește de pe server.</p>
      )}
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Data</dt><dd>{dm(d.date, s.today) || "-"}</dd></dl>
        {invoice || d.document_type === "receipt" ? <dl className={`${detailListStyle} py-2`}><dt>Factură</dt><dd>{receiptState(invoice)}</dd></dl> : null}
        <dl className={`${detailListStyle} py-2`}><dt>Sursă</dt><dd>{expenseSource(s, "document", d.id)}</dd></dl>
        {d.number || d.mail?.number ? <dl className={`${detailListStyle} py-2`}><dt>Număr</dt><dd>{d.number || d.mail?.number}</dd></dl> : null}
        {d.card ? <dl className={`${detailListStyle} py-2`}><dt>Plată</dt><dd>Card terminat în {d.card}</dd></dl> : null}
        <dl className={`${detailListStyle} py-2`}><dt>Firma</dt><dd>{coName(s)}</dd></dl>
      </Panel>
      {d.mail ? (
        <Panel className="my-4">
          <dl className={`${detailListStyle} py-2`}><dt>Din e-mail de la</dt><dd>{d.mail.sender}</dd></dl>
          <dl className={`${detailListStyle} py-2`}><dt>Subiect</dt><dd>{d.mail.subject || "-"}</dd></dl>
          {d.mail.date ? <dl className={`${detailListStyle} py-2`}><dt>Primit</dt><dd>{dm(d.mail.date, s.today)}</dd></dl> : null}
        </Panel>
      ) : null}
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Contabil</dt><dd className={`st ${d.sent ? "ok" : ""} whitespace-normal wrap-anywhere ${statusTextStyle} ${statusStyle[d.sent ? "ok" : ""]}`}>{d.sent ? "Trimisă" : d.held ? "Nu intră în dosarul contabilului până nu este asociat cu plata din Tranzacții" : `Intră în raportul pe ${lunaName(d.month)}`}</dd></dl>
        {d.note ? <dl className={`${detailListStyle} py-2`}><dt>Notă</dt><dd>{d.note}</dd></dl> : null}
      </Panel>
    </Sheet>
  );
}

/** An issued invoice: what the client owes, how it was collected; "Marchează ca încasată" with a short note, and its undo. */
export function IssuedSheet({ id, onClose }: { id: string; onClose: () => void }) {
  return <DetailSheet kind="issued" id={id} onClose={onClose}><IssuedBody id={id} onClose={onClose} /></DetailSheet>;
}

function IssuedBody({ id, onClose }: { id: string; onClose: () => void }) {
  const s = useCompany();
  const r = findIssued(s, id)![0];
  const { today } = s;
  const cur = curOf(r.currency);
  const [c, label, detail] = issuedState(r, today, r.storno_of ? findIssued(s, r.storno_of)?.[0].number : null);
  const how = r.how === "statement" ? "Din extrasul de cont" : r.how === "manual" ? collectedHow(r) : "";
  // Before its due day the person sends the invoice; collection choices wait behind one press.
  const fresh = r.can_mark && !!r.due_date && r.due_date > today;
  const lead = fresh && r.origin === "fiscalm" && !!r.local_id;
  const [marking, setMarking] = useState(!fresh);
  const canMark = useCan(actionRole("issued-mark")) && r.can_mark;
  // Folded, "Marchează ca încasată…" sits in one group with the downloads.
  const unfold = canMark && !marking
    ? <Button type="button" className="btn g" aria-expanded={false} onClick={() => setMarking(true)}>Marchează ca încasată…</Button> : null;
  return (
    <Sheet onClose={onClose} cls={SHEET_SHAPES.issued.cls} label={`${r.client}, ${lei(r.total)} ${cur}`}>
      <CloseX onClose={onClose} />
      <SheetHead amount={r.total} cur={cur} sub={<PartnerLink id={r.partner} name={r.client} cui={r.client_cui} />} />
      {r.rejected ? <StateLine tone="late">Respinsă de ANAF, nu se încasează</StateLine> : <StateLine tone={c}>{phrase(label, detail)}</StateLine>}
      {r.status === "marked" && r.collected_method ? <p className={noteLeftStyle}>{collectedOf(r.collected_method).proof}</p> : null}
      {lead ? <IssuedDownloads r={r}>{unfold}</IssuedDownloads> : unfold ? <Actions className="btns mt-0 mb-5">{unfold}</Actions> : null}
      {canMark && marking ? <MarkCollected r={r} folded={fresh} /> : null}
      {r.can_unmark ? (
        <Actions><Act action="issued-unmark" body={{ id }} label="Anulează marcarea" cls="btn g" /></Actions>
      ) : null}
      <IssuedSpv r={r} />
      <DetailPages />
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Număr</dt><dd>{r.number}</dd></dl>
        <dl className={`${detailListStyle} py-2`}><dt>Data emiterii</dt><dd>{dm(r.issue_date, today)}</dd></dl>
        <dl className={`${detailListStyle} py-2`}><dt>Scadență</dt><dd>{dm(r.due_date, today) || "-"}</dd></dl>
        {how ? <dl className={`${detailListStyle} py-2`}><dt>Încasare</dt><dd>{how}</dd></dl> : null}
        <dl className={`${detailListStyle} py-2`}><dt>Sursă</dt><dd>{r.origin === "fiscalm" ? `Emisă în ${PRODUCT}` : "e-Factura"}</dd></dl>
      </Panel>
      <InvoiceLines />
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>Total</dt><dd>{lei(r.total)} {cur}</dd></dl>
        {Number(r.credited) ? <dl className={`${detailListStyle} py-2`}><dt>Stornat</dt><dd>{lei(r.credited)} {cur}</dd></dl> : null}
        {Number(r.covered) ? <dl className={`${detailListStyle} py-2`}><dt>Încasat</dt><dd>{lei(r.covered)} {cur}</dd></dl> : null}
        <dl className={`${detailListStyle} py-2`}><dt>Rest de încasat</dt><dd>{lei(r.owed)} {cur}</dd></dl>
      </Panel>
      <IssuedFiles r={r} links={!lead} />
    </Sheet>
  );
}

/** How the person said the client paid: "OP", "Altele: compensare cu factura lor". */
function collectedHow(r: Issued): string {
  if (!r.collected_method) return "Marcată manual";
  const title = collectedOf(r.collected_method).title;
  return r.note ? `${title}: ${r.note}` : title;
}

/** How an issued invoice was collected outside the statement: OP, Numerar, Card or Altele with a note ("Anulează
 * marcarea" undoes it). `folded` (not yet due): the choices opened behind "Marchează ca încasată…", so they take the
 * focus. A marking stays "În curs de confirmare"; it never claims bank proof, and each way says what proof closes it. */
function MarkCollected({ r, folded }: { r: Issued; folded: boolean }) {
  const { busy } = useApp(); // while a mark is sent, the choice it sent can't change under it
  const [method, setMethod] = useState<CollectedMethod | null>(null);
  const [note, setNote] = useState("");
  const group = useRef<HTMLFieldSetElement>(null);
  const ids = useId();
  useEffect(() => { if (folded) group.current?.focus(); }, [folded]);
  const detail = method === "other" ? note.trim() : "";
  const why = !method ? "Alege cum a fost încasată." : method === "other" && !detail ? "Scrie cum a fost încasată." : "";
  return (
    <Form className="my-5" onSubmit={(e) => e.preventDefault()}>
      <fieldset ref={group} tabIndex={-1} className="m-0 border-0 p-0 outline-none" disabled={busy}>
        <legend className={legendStyle}>Cum a fost încasată?</legend>
        <MethodList methods={COLLECTED_METHODS} value={method} onChange={setMethod} name={`${ids}-method`} />
        {method === "other" ? <>
          <Label htmlFor={`${ids}-note`}>Cum a fost încasată</Label>
          <Input id={`${ids}-note`} value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} autoComplete="off" autoFocus />
        </> : null}
      </fieldset>
      <Actions>
        <Act action="issued-mark" body={{ id: r.id, method: method ?? "", detail }} label="Marchează ca încasată" cls="btn w-full!" off={!!why} why={why} />
      </Actions>
      {method ? <p className="mt-2! text-muted">{collectedOf(method).proof}</p> : null}
    </Form>
  );
}

/** A sheet's state, right under its head, in the lists' words and colour. */
function StateLine({ tone, children }: { tone: string; children: ReactNode }) {
  return (
    <p className={`st ${tone} mt-0 mb-4 flex w-fit max-w-full items-start gap-2 rounded-badge px-2 py-1 ${statusTextStyle} ${statePillStyle[tone] ?? statePillStyle[""]}`}>
      <span className="mt-[7px] size-2 shrink-0 rounded-full bg-current" aria-hidden="true" /><span className="min-w-0 wrap-anywhere">{children}</span>
    </p>
  );
}


export function OrderSheet({ id, onClose }: { id: string; onClose: () => void }) {
  return <DetailSheet kind="order" id={id} onClose={onClose}><OrderBody id={id} onClose={onClose} /></DetailSheet>;
}

function OrderBody({ id, onClose }: { id: string; onClose: () => void }) {
  const s = useCompany();
  const task = s.emag_tasks!.find((t) => `${s.co}:${t.id}` === id)!;
  return <Sheet onClose={onClose} label="Comandă eMAG">
      <CloseX onClose={onClose} />
      <SheetHead title={`Comandă eMAG ${task.external_id}`} />
      <DetailPages />
      <Panel><p className="m-0 text-sm leading-relaxed">{emagReason(task.reason, !!(task.order_id || task.draft_id))}</p></Panel>
      <Actions>
      {task.reason === "return_review" && task.order_id && <Act action="emag-refund" body={{ id: task.id }} label="Pregătește ciorna de stornare" />}
      <Act action="emag-resolve" body={{ id: task.id }} label="Marchează ca rezolvată" />
      {task.draft_id && <L to={s.issued.some((r) => r.local_id === task.draft_id)
        ? { co: s.co, iss: s.issued.find((r) => r.local_id === task.draft_id)!.id }
        : { co: s.co, new: task.draft_id }} className="btn">Vezi factura</L>}
      </Actions>
    </Sheet>;
}


export function emagReason(reason: string, imported = false): string {
  if (imported && !["return_review", "attachment_retry", "auto_issue_refused", "awb_retry"].includes(reason)) {
    return "Comanda are deja o ciornă sau o factură. Verifică factura existentă.";
  }
  const reasons: Record<string, string> = {
    awb_retry: "AWB-ul nu a putut fi preluat. Verifică livrarea din eMAG și factura existentă.",
    missing_vat: "TVA-ul produselor lipsește din comandă. Emite factura manual.",
    return_review: "Verifică returul și, dacă e cazul, stornează factura inițială.",
    attachment_retry: "Factura nu a fost atașată în eMAG. Atașarea se reia automat la fiecare oră.",
    shipping_tax: "TVA-ul transportului nu este precizat. Emite factura manual.",
    voucher_allocation: "Reducerea nu poate fi repartizată pe produse. Emite factura manual.",
    voucher_funding: "Nu este clar cine suportă reducerea. Emite factura manual.",
    voucher_tax: "TVA-ul reducerii nu poate fi verificat. Emite factura manual.",
    county: "Județul adresei de facturare nu poate fi identificat.",
    sector: "Sectorul adresei de facturare nu poate fi identificat.",
    order_review: "Verifică datele comenzii și facturile existente înainte să emiți factura.",
    auto_issue_refused: "Emitere automată refuzată. Verifică ciorna facturii.",
  };
  return reasons[reason] ?? "Verifică datele comenzii și facturile existente înainte să emiți factura.";
}
