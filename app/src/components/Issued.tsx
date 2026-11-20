"use client";
import { Anchor, Button, Input, Label, Form, Actions, Panel, buttonStyle, buttonTone, detailListStyle, rowStyle, rowTextStyle, rowIconStyle, statusStyle, statusTextStyle, noteLeftStyle } from "./Controls";
// An invoice issued in fiscalm, in its sheet: where it stands in SPV, its files, sending it again, storno.
// What can be done comes from the engine (can_upload, can_resolve, can_storno); the app only shows it.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { getDetail, invoiceFile, postInvoicing, type InvoicingOp } from "@/lib/api";
import { dayMonth, type Tone } from "@/lib/format";
import type { InvoicingResult, Issued, SpvState } from "@/lib/types";
import { useApp, useCan, useCompany, type After } from "./context";
import type { Ask } from "./Confirm";
import { findIssued } from "./derive";
import { I, L, ICON_TINT } from "./ui";

const SPV_LABEL: Record<SpvState, [Tone, string]> = {
  not_required: ["", "Trimitere neobligatorie"],
  none: ["soon", "Netrimisă"], uploading: ["soon", "Se trimite"], uploaded: ["soon", "Trimisă, se așteaptă răspunsul ANAF"],
  accepted: ["ok", "Acceptată de ANAF"], rejected: ["late", "Respinsă de ANAF"], unknown: ["late", "Rezultat necunoscut"],
  error: ["late", "Eroare la trimitere"],
};
const TEST_LABEL: Record<SpvState, string> = {
  not_required: "Nu trebuie trimisă la ANAF Test",
  none: "Netrimisă la ANAF Test",
  uploading: "Se trimite la ANAF Test",
  uploaded: "Trimisă la ANAF Test. Se așteaptă răspunsul",
  accepted: "Acceptată de ANAF Test",
  rejected: "Respinsă de ANAF Test",
  unknown: "Nu se știe dacă factura a ajuns la ANAF Test",
  error: "Eroare la trimiterea la ANAF Test",
};
const SPV_OTHER: [Tone, string] = ["late", "Stare necunoscută"];

/** At most this many of ANAF's reasons are shown; the XML and SPV have them all. */
const MAX_ERRORS = 10;

/** The SPV state as shown: an upload the engine offers to resolve (in flight too long) reads as unknown. */
const shownState = (r: Issued): SpvState => {
  const st = r.spv?.state ?? "none";
  return st === "uploading" && r.can_resolve ? "unknown" : st;
};

export function spvTestLabel(r: Issued): string {
  return r.spv?.environment === "test"
    ? TEST_LABEL[shownState(r)] ?? "Stare necunoscută la ANAF Test" : "";
}

/** In the list, an ANAF Test upload that needs attention; the app-wide Sandbox banner says what ANAF Test is. */
const TEST_PROBLEM: Partial<Record<SpvState, string>> = {
  rejected: "respinsă de ANAF Test", unknown: "rezultat ANAF Test necunoscut", error: "eroare la trimiterea la ANAF Test",
};

/** What the list says about an invoice whose SPV upload needs attention; empty when nothing does. */
export function spvProblem(r: Issued): string {
  if (r.spv?.environment === "test") return TEST_PROBLEM[shownState(r)] ?? "";
  if (r.rejected) return "Respinsă de ANAF";
  if (r.origin !== "fiscalm") return "";
  const st = shownState(r);
  return st === "unknown" ? "Rezultat SPV necunoscut" : st === "error" ? "Eroare la trimiterea în SPV" : "";
}

/** A request for this company's invoicing; the sheet stays open unless `after` says where to go. */
function useSend() {
  const { run } = useApp();
  const s = useCompany();
  return (op: InvoicingOp, body: Record<string, unknown>, confirm?: Ask, after: After<InvoicingResult> = () => false) =>
    run(s.co, () => postInvoicing(op, { ...body, co: s.co }), confirm, after);
}

/** Where an invoice issued in fiscalm stands in SPV, and what can be done about it. Nothing for one read from SPV. */
export function IssuedSpv({ r }: { r: Issued }) {
  const allowed = useCan("member");
  const manage = useCan("manage_integrations");
  const s = useCompany();
  const { busy } = useApp();
  const send = useSend();
  const id = r.local_id;
  if (r.origin !== "fiscalm" || !id) return null;
  const st = shownState(r);
  const [tone, statusLabel] = SPV_LABEL[st] ?? SPV_OTHER;
  // The day the scheduled run sends it; once that day has passed without a send, it reads as not sent.
  const on = st === "none" ? r.spv?.upload_on : null;
  const scheduled = on && on >= s.today ? `Se trimite în SPV pe ${dayMonth(on)}` : "";
  const label = spvTestLabel(r) || scheduled || statusLabel;
  const upload = st === "none" || st === "not_required" ? "Trimite în SPV" : "Trimite din nou în SPV";
  const byLocal = (lid: string) => s.issued.find((x) => x.local_id === lid);
  const original = r.storno_of ? byLocal(r.storno_of) : undefined;
  const reversals = (r.reversed_by ?? []).map(byLocal).filter((x): x is Issued => !!x);
  return (
    <>
      <Panel className="my-4">
        <dl className={`${detailListStyle} py-2`}><dt>SPV</dt><dd className={`st whitespace-normal wrap-anywhere ${tone} ${statusTextStyle} ${statusStyle[tone]}`}>{label}</dd></dl>
        {r.spv?.errors.slice(0, MAX_ERRORS).map((e, i) => <dl key={i} className={`${detailListStyle} py-2 text-danger`}><dt>Motiv</dt><dd>{e}</dd></dl>)}
        {original ? (
          <dl className={`${detailListStyle} py-2`}><dt>Stornare pentru</dt><dd><L to={{ iss: original.id }} label={`Factura ${original.number}`}>{original.number}</L></dd></dl>
        ) : null}
        {reversals.length ? (
          <dl className={`${detailListStyle} py-2`}><dt>Stornată prin</dt><dd>
            {reversals.map((x, i) => <span key={x.id}>{i ? ", " : ""}<L to={{ iss: x.id }} label={`Factura ${x.number}`}>{x.number}</L></span>)}
          </dd></dl>
        ) : null}
      </Panel>
      {allowed && r.can_upload ? (
        <Actions>
          <Button type="button" className="btn" disabled={busy}
            onClick={() => send("upload", { id }, { text: `Trimiți factura ${r.number} în SPV?`, yes: upload })}>
            {upload}
          </Button>
        </Actions>
      ) : null}
      {allowed && !r.can_upload && ["none", "rejected", "error"].includes(st) && s.system.anaf.status !== "ok" ? (
        manage ? <Actions><L to={{ tab: "setari", sec: "integrari" }} at="k-anaf" className="btn g">Conectează ANAF pentru a trimite</L></Actions>
          : <p className={noteLeftStyle}>Conectează ANAF pentru a trimite</p>
      ) : null}
      {allowed && r.can_resolve ? <Resolve id={id} anaf={s.system.anaf.status === "ok"} /> : null}
    </>
  );
}

/** An invoice issued in fiscalm: its PDF and XML, and the storno, below its details. Nothing for one read from SPV. */
/** A fresh invoice's first step: its PDF to send (primary) and its XML, as buttons right under its state. */
export function IssuedDownloads({ r, children }: { r: Issued; children?: ReactNode }) {
  const s = useCompany();
  const id = r.local_id;
  if (r.origin !== "fiscalm" || !id) return null;
  const file = { download: true, target: "_blank", rel: "noopener noreferrer" };
  return (
    <Actions className="btns mt-0 mb-4">
      {r.pdf ? <a className={`btn ${buttonStyle} ${buttonTone.primary}`} href={invoiceFile(s.co, id, "pdf")} {...file}>Descarcă PDF</a> : null}
      <Anchor href={invoiceFile(s.co, id, "xml")} {...file}>Descarcă XML</Anchor>
      {children}
    </Actions>
  );
}

/** The invoice's files (unless `IssuedDownloads` already leads with them) and its storno. */
export function IssuedFiles({ r, links = true }: { r: Issued; links?: boolean }) {
  const allowed = useCan("member");
  const s = useCompany();
  const id = r.local_id;
  if (r.origin !== "fiscalm" || !id) return null;
  return (
    <>
      {links ? <Panel padded={false} className="my-4 divide-x-0 divide-y divide-solid divide-line">
        {r.pdf ? (
          <a className={`${rowStyle} hover:bg-inset`} href={invoiceFile(s.co, id, "pdf")} download target="_blank" rel="noopener noreferrer">
            <span className={`${rowIconStyle} ${ICON_TINT}`}><I n="file-text" /></span>
            <span className={rowTextStyle}><b>Factură PDF</b><small>Descarcă</small></span>
          </a>
        ) : null}
        <a className={`${rowStyle} hover:bg-inset`} href={invoiceFile(s.co, id, "xml")} download target="_blank" rel="noopener noreferrer">
          <span className={`${rowIconStyle} ${ICON_TINT}`}><I n="file-text" /></span>
          <span className={rowTextStyle}><b>XML e-Factura</b><small>Descarcă</small></span>
        </a>
      </Panel> : null}
      {allowed && r.can_storno ? <Storno r={r} id={id} /> : null}
    </>
  );
}

/** An upload whose outcome isn't known: fiscalm looks for it in SPV's list of sent invoices (with ANAF connected), or the
 * user looks and records what is there, so it is never sent twice. */
function Resolve({ id, anaf }: { id: string; anaf: boolean }) {
  const { busy } = useApp();
  const send = useSend();
  const [index, setIndex] = useState("");
  const ok = /^\d{1,20}$/.test(index.trim());
  return (
    <Form className="my-5 rounded-card bg-inset p-4" onSubmit={(e) => e.preventDefault()}>
      <p className={`${noteLeftStyle} mb-3!`}>
        Nu se știe dacă factura a ajuns în SPV. Verific-o în SPV înainte să o trimiți din nou.
      </p>
      <Label htmlFor="spv-index">Indexul de încărcare din SPV</Label>
      <p className={noteLeftStyle} id="spv-index-note">Îl găsești în SPV, în lista de mesaje e-Factura, la factura trimisă.</p>
      <Input id="spv-index" value={index} onChange={(e) => setIndex(e.target.value)} inputMode="numeric" autoComplete="off" maxLength={20}
        aria-describedby="spv-index-note" />
      <Actions>
        {anaf ? <Button type="button" className="btn" disabled={busy} onClick={() => send("recover-upload", { id })}>Caută în SPV</Button> : null}
        <Button type="button" className={anaf ? "btn g" : "btn"} disabled={busy || !ok}
          onClick={() => send("resolve-upload", { id, checked_in_spv: true, upload_id: index.trim() })}>Salvează indexul</Button>
        <Button type="button" className="btn g" disabled={busy}
          onClick={() => send("resolve-upload", { id, checked_in_spv: true, upload_id: null },
            { text: "Ai verificat că factura nu apare în SPV? O marchezi ca netrimisă și o poți trimite din nou.", yes: "Marchează ca netrimisă" })}>Marchează ca netrimisă</Button>
      </Actions>
    </Form>
  );
}

/** Resolve the canonical storno identity from its response context or bounded detail. A failed ALL refresh keeps the
 * existing fallback to its company; navigation while the write/read is pending is never pulled back. */
function Storno({ r, id }: { r: Issued; id: string }) {
  const s = useCompany();
  const { busy, route, go, refresh } = useApp();
  const send = useSend();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const opener = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (wasOpen.current && !open) opener.current?.focus();
    wasOpen.current = open;
  }, [open]);
  const submit = async () => {
    const at = window.location.href;
    const res = await send("storno", { id, reason: reason.trim() },
      { text: `Stornezi factura ${r.number}? Se emite o factură de stornare pentru toate liniile ei.`, yes: "Emite factura de stornare", danger: true });
    if (!res?.ok || window.location.href !== at) return;
    setOpen(false);
    setReason("");
    if (!res.id) return;
    let made = res.state ? findIssued(res.state, res.id) : null;
    if (!made) {
      try { made = findIssued((await getDetail(s.co, "issued", res.id)).state, res.id); } catch { return; }
    }
    if (!made || window.location.href !== at) return;
    const current = route.co === s.co || await refresh();
    if (window.location.href !== at) return;
    go({ ...route, co: current ? route.co : s.co, iss: made[0].id }, { replace: true });
  };
  if (!open) {
    return (
      <Actions>
        <Button ref={opener} type="button" variant="danger" onClick={() => setOpen(true)}>Stornează factura</Button>
      </Actions>
    );
  }
  return (
    <Form className="my-5 rounded-card bg-inset p-4" onSubmit={(e) => e.preventDefault()}>
      <Label htmlFor="storno-reason">Motivul stornării</Label>
      <Input id="storno-reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} autoComplete="off" autoFocus />
      <Actions>
        <Button type="button" variant="danger" disabled={busy || !reason.trim()} onClick={submit}>Emite factura de stornare</Button>
        <Button type="button" className="btn g" onClick={() => setOpen(false)}>Renunță</Button>
      </Actions>
    </Form>
  );
}
