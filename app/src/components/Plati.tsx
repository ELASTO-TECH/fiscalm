"use client";
// Fișiere de plăți: the firm's payment files, newest first, and one file's sheet (`pf`) with its payments, its download and
// "Am importat în bancă". A file is made in Cheltuieli (Pune în fișier); this screen lists files, never invoices.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { actionRole, allowedInShadow, ApiError, Declined, FIXTURES, getPaymentFile, NO_ANSWER, postFile, SESSION_CHANGED, SESSION_ENDED, type FileAction, type FileActionName } from "@/lib/api";
import { cap, curOf, dm, dmt, lei, nOf, type Tone } from "@/lib/format";
import type { Co, FilePayment, PaymentFile, PaymentFileDetail, PaymentFileQuestion } from "@/lib/types";
import { Actions, Button, Form, Input, Label, Panel, noteLeftStyle } from "./Controls";
import { useApp, useCan, useCompany } from "./context";
import { PAID_AWAITING, linkedState, type Sign } from "./derive";
import { CloseX, SHEET_SHAPES, Sheet, SheetHead, SheetLoading } from "./Sheets";
import { LoadFailed, useRetryWhenBack } from "./Paging";
import { Empty, I, L, Row, Spinner, Title } from "./ui";

const KIND: Record<FilePayment["kind"], [string, string]> = { op: ["bank", "Taxă"], efactura: ["receipt", "Factură"] };

/** "Nr. AF 2041", "Taxă, CAS noiembrie": what a payment pays. */
const what = (p: FilePayment) => {
  const ref = (p.ref || "").trim();
  return p.kind === "op" ? `${KIND.op[1]}, ${(p.details || ref).trim().slice(0, 40)}` : ref ? `Nr. ${ref}` : KIND.efactura[1];
};

/** A file's state in words, its tone and its sign (docs/copy.md: green only for what evidence proves). An open file
 * downloaded before its last change says so: the bank needs the new download. */
export function fileState(f: PaymentFile, today: string): [Tone, string, Sign | ""] {
  if (f.status === "confirmed") return ["ok", "Confirmat", "="];
  if (f.status === "imported") {
    const due = f.payment_count - f.skipped_count; // a payment no longer paid is never confirmed, and no longer waits
    return ["", f.confirmed_count ? `Importat, ${f.confirmed_count} din ${due} confirmate` : "Importat în bancă, se așteaptă extrasul", "≈"];
  }
  if (f.changed_since_download) return ["soon", "Deschis, modificat după descărcare", ""];
  return ["", f.downloaded_at ? `Deschis, descărcat pe ${dm(f.downloaded_at, today)}` : "Deschis", ""];
}

export function Plati() {
  const s = useCompany();
  return (
    <div className="min-w-0 max-w-none">
      <Title>Fișiere de plăți</Title>
      <Panel padded={false}>
        {s.files.map((f) => {
          const [tone, state, sign] = fileState(f, s.today);
          return <Row key={f.id} icon="file-text" badge={sign ? "" : tone} sign={sign} title={f.name} sub={state} subcls={tone}
            val={`${lei(f.total)} ${curOf(f.currency)}`} vsub={`${nOf(f.payment_count, "o plată", "plăți")}, ${dm(f.created_at, s.today)}`}
            to={{ tab: "plati", pf: String(f.id) }} />;
        })}
        {!s.files.length ? <Empty action={<L to={{ tab: "cheltuieli", f: "open" }} className="btn g">Alege facturi de plătit</L>} /> : null}
      </Panel>
    </div>
  );
}

/** One file's detail, read again after each of its actions and whenever the firm's state is read again (back from the
 * bank's app, another session's change): the sheet never acts on what an older read said. A read that fails, or a file
 * gone meanwhile, replaces what was shown. */
const GONE = "Fișierul nu mai există.";

function useFile(id: number) {
  const s = useCompany();
  const [file, setFile] = useState<PaymentFileDetail | null>(null);
  const [error, setError] = useState("");
  const [n, setN] = useState(0);
  useEffect(() => {
    const c = new AbortController();
    getPaymentFile(id, c.signal).then((f) => { setFile(f); setError(""); }, (e) => {
      if (c.signal.aborted) return;
      setFile(null);
      setError(e instanceof ApiError && e.status === 404 ? GONE : "Fișierul nu a putut fi încărcat.");
    });
    return () => c.abort();
  }, [id, n, s]);
  const reload = useCallback(() => setN((x) => x + 1), []);
  useRetryWhenBack(!!error && error !== GONE, reload);
  return { file, error, reload };
}

/** A file's sheet (`pf`): its payments, what changed since its download, Descarcă and Am importat în bancă while open,
 * the engine's questions about it, and its name. What the person may do is the engine's (`can_*`). */
export function PaymentFileSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const { file, error, reload } = useFile(Number(id));
  if (!file) {
    return <SheetLoading shape={SHEET_SHAPES.file} loading={!error} failed={error} onClose={onClose}
      status={error === GONE ? <p className={noteLeftStyle} role="alert">{error}</p> : <LoadFailed text={error} retry={reload} />} />;
  }
  return <FileBody key={file.id} f={file} reload={reload} onClose={onClose} />;
}

/** What this person may do with the file now: the engine's `can_*`, the role the action registry names, and the shared
 * mode policy (download, import and the missing file's answers are Live only). */
function useMay(f: PaymentFileDetail, live: boolean) {
  const mode = (a: FileActionName) => live || allowedInShadow(a);
  const rename = useCan(actionRole("payment_file_rename")), remove = useCan(actionRole("payment_file_remove"));
  const del = useCan(actionRole("payment_file_delete")), download = useCan(actionRole("payment_file_download"));
  const imp = useCan(actionRole("payment_file_import"));
  const notImported = useCan(actionRole("payment_file_not_imported"));
  const restore = useCan(actionRole("payment_file_restore")), imported = useCan(actionRole("payment_file_missing_import"));
  return {
    rename: rename && mode("payment_file_rename") && f.can_rename, remove: remove && mode("payment_file_remove") && f.can_remove,
    remove_file: del && mode("payment_file_delete") && f.can_delete, download: download && mode("payment_file_download") && f.can_download,
    import: imp && mode("payment_file_import") && f.can_import,
    notImported: notImported && mode("payment_file_not_imported"),
    answer: restore && imported && mode("payment_file_restore") && mode("payment_file_missing_import"),
  };
}
type May = ReturnType<typeof useMay>;

function FileBody({ f, reload, onClose }: { f: PaymentFileDetail; reload: () => void; onClose: () => void }) {
  const s = useCompany(); const { run, busy } = useApp();
  const may = useMay(f, s.live);
  const send = useCallback(async (body: FileAction, after?: () => void) => {
    const r = await run(f.co as Co, () => postFile(body), undefined, () => false);
    if (r?.ok) after?.();
    reload();
    return r;
  }, [run, f.co, reload]);
  const [tone, state] = fileState(f, s.today);
  const fresh = f.status === "open" && !!f.downloaded_at;
  return <Sheet onClose={busy ? () => undefined : onClose} label={f.name} cls={SHEET_SHAPES.file.cls}>
    <CloseX onClose={busy ? () => undefined : onClose} />
    <SheetHead title={f.name} sub={<>Creat pe {dm(f.created_at, s.today)}{f.imported_at ? `, importat în bancă pe ${dm(f.imported_at, s.today)}` : ""}.
      {" "}{cap(nOf(f.payment_count, "o plată", "plăți"))}, {lei(f.total)} {curOf(f.currency)}. <span className={tone === "ok" ? "text-ok" : tone === "soon" ? "text-wait" : ""}>{state}</span></>} />
    {may.rename ? <Rename f={f} send={send} /> : null}
    {f.changed_since_download && f.downloaded_at ? <p className="mt-0 mb-4 rounded-card bg-wait/10 px-3 py-2.5 text-sm leading-relaxed text-wait" role="note">
      Modificat după descărcarea din {dmt(f.downloaded_at, s.today)}. Descarcă versiunea nouă înainte de import.</p> : null}
    {f.questions.map((q, i) => <Question key={i} f={f} q={q} may={may} send={send} reload={reload} />)}
    <FileActions f={f} may={may} send={send} reload={reload} onDeleted={onClose} />
    <Panel padded={false}>
      {f.payments.map((p) => <FilePaymentRow key={p.id} p={p} today={s.today} fresh={fresh} remove={may.remove
        ? () => void send({ action: "payment_file_remove", file_id: f.id, payment_id: p.id }) : undefined} />)}
      {!f.payments.length ? <Empty /> : null}
    </Panel>
  </Sheet>;
}

type Send = (body: FileAction, after?: () => void) => Promise<unknown>;

/** Redenumește, while the file is open: the name in place of the title, saved or left as it was. */
function Rename({ f, send }: { f: PaymentFileDetail; send: Send }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(f.name);
  const { busy } = useApp();
  const ids = useId();
  if (!editing) {
    return <Button type="button" variant="quiet" className="btn -mt-3 mb-3 min-h-9! px-0! text-sm text-muted" onClick={() => { setName(f.name); setEditing(true); }}>
      <I n="pencil-simple" />Redenumește</Button>;
  }
  return <Form className="mb-4" onSubmit={(e) => { e.preventDefault(); if (name.trim()) void send({ action: "payment_file_rename", file_id: f.id, name: name.trim() }, () => setEditing(false)); }}>
    {/* While the name is saved nothing can be typed: the field closes on the answer and would drop it. */}
    <fieldset className="m-0 min-w-0 border-0 p-0" disabled={busy}>
      <Label htmlFor={`${ids}-name`}>Nume</Label>
      <Input id={`${ids}-name`} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} autoComplete="off" autoFocus />
      <Actions>
        <Button type="submit" className="btn" disabled={busy || !name.trim() || name.trim() === f.name}>Salvează</Button>
        <Button type="button" className="btn g" disabled={busy} onClick={() => setEditing(false)}>Renunță</Button>
      </Actions>
    </fieldset>
  </Form>;
}

/** Descarcă (the first time, or again after a change) and Am importat în bancă for the download that is the file as it is
 * now; Șterge fișierul for an empty open one. A download is one POST, never repeated on its own: an interrupted one may
 * already be recorded. */
function FileActions({ f, may, send, reload, onDeleted }: { f: PaymentFileDetail; may: May; send: Send; reload: () => void; onDeleted: () => void }) {
  const { busy } = useApp();
  const download = useDownload(f, reload);
  const last = f.downloads[0];
  const again = !!last;
  if (!may.download && !may.import && !may.remove_file) return null;
  return <Actions className="mb-4" role="group" aria-label="Fișier de plăți">
    {may.import && last ? <Button type="button" className="btn" disabled={busy}
      onClick={() => void send({ action: "payment_file_import", file_id: f.id, download_id: last.id })}><I n="check" />Am importat în bancă</Button> : null}
    {may.download ? <Button type="button" className={may.import ? "btn g" : "btn"} disabled={busy || download.busy} aria-busy={download.busy || undefined}
      onClick={() => void download.start()}>{download.busy ? <Spinner /> : <I n="download-simple" />}{again ? "Descarcă din nou" : "Descarcă fișierul"}</Button> : null}
    {may.remove_file ? <Button type="button" className="btn g" disabled={busy}
      onClick={() => void send({ action: "payment_file_delete", file_id: f.id }, onDeleted)}>Șterge fișierul</Button> : null}
  </Actions>;
}

/** A download, or the missing file made again (restore): its bytes come back in the answer (`done.content_base64`) and
 * are saved as the engine named them. */
function useDownload(f: PaymentFileDetail, reload: () => void) {
  const { run, say, refresh } = useApp();
  const [busy, setBusy] = useState(false);
  // The sheet's life: it ends when the sheet closes or the session changes or ends; a late answer then saves nothing.
  const life = useRef({ n: 0, mounted: true });
  useEffect(() => {
    const now = life.current;
    now.mounted = true;
    const stop = (e: Event) => { if (!(e instanceof StorageEvent) || e.key === SESSION_CHANGED || e.key === null) now.n++; };
    window.addEventListener("storage", stop);
    window.addEventListener(SESSION_ENDED, stop);
    return () => { now.n++; now.mounted = false; window.removeEventListener("storage", stop); window.removeEventListener(SESSION_ENDED, stop); };
  }, []);
  const start = async (body: FileAction = { action: "payment_file_download", file_id: f.id }) => {
    if (busy) return;
    setBusy(true);
    const asked = life.current.n;
    try {
      const r = await run(f.co as Co, () => postFile(body), undefined, () => false);
      const d = r?.ok && life.current.n === asked ? r.done : null;
      if (d?.content_base64 && d.name && !FIXTURES) save(d.content_base64, d.name, d.content_type || "text/csv");
    } catch (e) {
      if (!(e instanceof Declined)) say(e instanceof ApiError && e.status ? e.message : NO_ANSWER);
    } finally {
      if (life.current.mounted) { setBusy(false); reload(); void refresh(); }
    }
  };
  return { busy, start };
}

function save(base64: string, name: string, type: string) {
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type })), link = document.createElement("a");
  link.href = url; link.download = name; document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
}

/** The engine's question about this file, with its answers bound to the file and the version it asks about. */
function Question({ f, q, may, send, reload }: { f: PaymentFileDetail; q: PaymentFileQuestion; may: May; send: Send; reload: () => void }) {
  const s = useCompany(); const { busy } = useApp();
  const restore = useDownload(f, reload); // the file made again comes back to save, as a download does
  const where = f.import_in ?? "aplicația băncii";
  const box = "mb-4 rounded-card border border-solid border-line p-4 text-sm leading-relaxed";
  if (q.kind === "orphan") {
    return <p className={`${box} text-danger`} role="note">{q.name} nu e în evidența fiscalm. Verifică-l înainte să-l imporți în {where}.</p>;
  }
  if (q.kind === "missing") {
    return <div className={box}>
      <p className="m-0 font-semibold">Fișierul a fost importat în {where}?</p>
      <p className="mt-1 mb-0 text-muted">Fișierul ({nOf(q.count, "o plată", "plăți")}) nu mai este în fiscalm.{s.live ? "" : " Răspunzi în Live."}</p>
      {may.answer ? <Actions>
        <Button type="button" className="btn g" disabled={busy} onClick={() => void send({ action: "payment_file_missing_import", file_id: f.id, version: q.version })}>Da, l-am importat</Button>
        <Button type="button" className="btn" disabled={busy || restore.busy} onClick={() => void restore.start({ action: "payment_file_restore", file_id: f.id, version: q.version })}>Nu, refă fișierul</Button>
      </Actions> : null}
    </div>;
  }
  return <div className={box}>
    <p className="m-0 font-semibold">Ce versiune ai importat în {where}?</p>
    <p className="mt-1 mb-0 text-muted">Descărcat de {nOf(q.versions.length, "o dată", "ori")}.</p>
    {may.import || may.notImported ? <div className="mt-3 grid gap-2">
      {may.import ? q.versions.map((v) => <Button key={v.id} type="button" className="btn g" disabled={busy}
        onClick={() => void send({ action: "payment_file_import", file_id: f.id, download_id: v.id })}>
        {dmt(v.at, s.today)}: {nOf(v.count, "o plată", "plăți")}, {lei(v.total)} lei</Button>) : null}
      {/* None of them: the file stays open, and needs a new download before its import. */}
      {may.notImported && q.versions[0] ? <Button type="button" className="btn g" disabled={busy}
        onClick={() => void send({ action: "payment_file_not_imported", file_id: f.id, download_id: q.versions[0].id })}>Nu am importat niciuna</Button> : null}
    </div> : null}
  </div>;
}

/** A payment of the file, where it is; one added after the file's download is marked Nouă while the file is open. */
function FilePaymentRow({ p, today, fresh, remove }: { p: FilePayment; today: string; fresh: boolean; remove?: () => void }) {
  const { busy } = useApp();
  const late = p.state === "in_file" && !!p.due && p.due < today; // still to pay, past its due day
  const [sub, tone, sign]: [string, Tone, Sign | ""] = p.state === "paid"
    ? p.proved ? [`Plătită, ${dm(p.paid_at, today)}`, "ok", "="] : [`${PAID_AWAITING}, ${dm(p.paid_at, today)}`, "", "≈"]
    : p.state === "imported" ? ["Importată în bancă, se așteaptă extrasul", "", "≈"]
    : p.state === "skipped" ? ["Nu se mai plătește", "", ""]
    : p.state === "lost" ? [linkedState("lost"), "", ""]
    : [p.due ? `scadentă ${dm(p.due, today)}` : "", "", late ? "" : "≈"];
  // "Importată în bancă, se așteaptă extrasul. Nr. TE-101"; a payment still to pay reads "Nr. TE-101, scadentă 25 nov".
  const line = p.state === "in_file" || !sub ? [what(p), sub].filter(Boolean).join(", ") : `${sub}. ${what(p)}`;
  const row = <Row icon={KIND[p.kind][0]} title={p.payee} sub={line} subcls={tone} sign={sign} badge={late ? "late" : ""} mark={fresh && p.added_after_download ? "Nouă" : ""}
    val={`${lei(p.amount)} ${curOf(p.currency)}`} to={p.ef_id ? { tab: "plati", inv: p.ef_id } : undefined} />;
  if (!remove) return row;
  return <div className="flex min-w-0 items-center">
    <div className="min-w-0 flex-1">{row}</div>
    <Button type="button" variant="quiet" className="mr-2 size-11 shrink-0 p-0" disabled={busy} aria-label={`Scoate din fișier ${p.payee}, ${what(p)}`} onClick={remove}>
      <I n="x" /></Button>
  </div>;
}
