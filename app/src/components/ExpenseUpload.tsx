"use client";
// "Încarcă bon sau factură": receipt photos and invoice PDFs from this device, straight into the company's receipts
// through api/document-upload, in the batches the Bondrop page sends. On a phone the browser offers the camera too.
import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { SESSION_CHANGED, SESSION_ENDED, postDocuments } from "@/lib/api";
import { nOf } from "@/lib/format";
import { href } from "@/lib/nav";
import { MAX_FILE, accepted, batches, failure } from "@/lib/upload";
import { coName, isAll, parts, type Co } from "@/lib/types";
import { Button } from "./Controls";
import { IdentityContext, useApp, useCan } from "./context";
import { CloseX, Sheet } from "./Sheets";
import { I, targetOf } from "./ui";

export const UPLOAD_LABEL = "Încarcă bon sau factură";
const SKIPPED = "Doar fotografii și PDF-uri de cel mult 15 MB.";
const BUSY = "S-au încărcat multe fișiere în ultima oră. Încearcă din nou mai târziu.";

/** The picker and what follows it: one new document opens its sheet, several open Bonuri și chitanțe with a count. `done` runs
 * once the files are sent (the Nou menu closes then). With `tx` (a card payment's sheet) one photo is that payment's bon:
 * the engine links them and the sheet stays, showing it explained. The batches stop when the account changes (here or in another tab)
 * or the control leaves the page, and a finished upload moves the page only if the user is still where it started. Under Toate
 * firmele `pickFor` takes the firm the person chose, and the upload then opens that firm's documents. Null for an account that can't
 * change anything. */
export function useExpenseUpload(done?: () => void, tx?: string): { pick: () => void; pickFor: (firm: Co) => void; busy: boolean; input: ReactNode; company: boolean } | null {
  const { view, route, go, say, refresh } = useApp();
  const allowed = useCan("member");
  const account = useContext(IdentityContext)?.identity.me.id ?? "";
  const ref = useRef<HTMLInputElement>(null);
  // What is current when a request returns: the account, the page and whether this control is still on it.
  const now = useRef({ account, route, mounted: true });
  useEffect(() => { Object.assign(now.current, { account, route }); }, [account, route]);
  useEffect(() => {
    const current = now.current;
    current.mounted = true;
    return () => { current.mounted = false; };
  }, []);
  const [busy, setBusy] = useState(false);
  const pickedFirm = useRef<{ co: Co; account: string } | null>(null); // the firm picked under Toate firmele, by whom
  useEffect(() => { // a session change here or in another tab forgets the pick, even before this tab reads the new account
    const forget = (e: Event) => { if (!(e instanceof StorageEvent) || e.key === SESSION_CHANGED || e.key === null) pickedFirm.current = null; };
    window.addEventListener("storage", forget);
    window.addEventListener(SESSION_ENDED, forget);
    return () => { window.removeEventListener("storage", forget); window.removeEventListener(SESSION_ENDED, forget); };
  }, []);
  if (!allowed) return null;
  const own = isAll(view) ? null : view.co;

  async function send(chosen: File[]) {
    // Under a firm always that firm; under Toate firmele the one just picked, used once and only by the account that picked it.
    const picked = pickedFirm.current;
    pickedFirm.current = null;
    const co = own ?? (picked?.account === now.current.account ? picked.co : null);
    if (!co || !chosen.length) return;
    const start = { ...now.current };
    let changed = false;
    const stop = (e: Event) => { if (!(e instanceof StorageEvent) || e.key === SESSION_CHANGED || e.key === null) changed = true; };
    // A control that is gone (its page left, or the signed-in tree replaced by a sign-out) sends and says nothing more.
    const over = () => changed || !now.current.mounted || now.current.account !== start.account;
    window.addEventListener("storage", stop);
    window.addEventListener(SESSION_ENDED, stop);
    setBusy(true);
    try {
      const ok: File[] = [];
      for (const f of chosen) if (f.size <= MAX_FILE && await accepted(f)) ok.push(f);
      const added: string[] = [];
      let known = 0, refused: number | null = null, why = "";
      for (const batch of tx ? [ok.slice(0, 1)].filter((one) => one.length) : batches(ok)) {
        if (over()) return;
        const r = await postDocuments(co, batch, tx);
        if (r.status !== 200) { refused = r.status; why = r.message ?? ""; break; }
        for (const f of r.files) if (f.duplicate) known++; else added.push(f.sha);
      }
      const linked = !!tx && !!ok.length && refused === null;
      if (over()) return;
      if (added.length || linked) await refresh();
      if (over()) return;
      done?.();
      if (tx) return say(linked ? "Bonul a fost asociat cu plata." : refused === null ? SKIPPED : why || failure(refused));
      const notes = [refused !== null ? (refused === 429 ? BUSY : failure(refused)) : "",
        known ? nOf(known, "Un document era deja încărcat.", "documente erau deja încărcate.") : "", ok.length < chosen.length ? SKIPPED : ""];
      const note = notes.filter(Boolean).join(" ");
      const stay = href(now.current.route) !== href(start.route); // the same place, even if the page re-rendered meanwhile
      if (added.length === 1 && !note && !stay) go(own ? targetOf(start.route, { doc: added[0] }) : { co, tab: "cheltuieli", side: "bonuri", doc: added[0] }, { focus: true });
      else if (added.length && !stay) go({ co, tab: "cheltuieli", side: "bonuri" }, { focus: true });
      say(added.length === 1 && !note ? "Documentul a fost încărcat."
        : added.length ? `${nOf(added.length, "Un document încărcat", "documente încărcate")}.${note ? ` ${note}` : ""}` : note || SKIPPED);
    } finally {
      window.removeEventListener("storage", stop);
      window.removeEventListener(SESSION_ENDED, stop);
      setBusy(false);
    }
  }

  return {
    busy,
    company: !!own,
    pick: () => { if (own && !busy) ref.current?.click(); },
    pickFor: (co: Co) => { if (!busy) { pickedFirm.current = { co, account }; ref.current?.click(); } },
    input: (
      <input ref={ref} type="file" multiple={!tx} accept="image/*,application/pdf" hidden tabIndex={-1} aria-hidden="true"
        onChange={(e) => { const files = [...(e.target.files ?? [])]; e.target.value = ""; void send(files); }} />
    ),
  };
}

/** The primary button of Cheltuieli's header. Under Toate firmele it first asks which firm the documents are for. */
export function ExpenseUploadButton() {
  const { view } = useApp();
  const up = useExpenseUpload();
  const [asking, setAsking] = useState(false);
  if (!up) return null;
  const close = () => setAsking(false);
  return (
    <>
      {up.input}
      <Button type="button" variant="secondary" disabled={up.busy} aria-busy={up.busy} aria-haspopup={up.company ? undefined : "dialog"}
        onClick={() => up.company ? up.pick() : setAsking(true)}>
        <I n="camera" />{up.busy ? "Se încarcă…" : UPLOAD_LABEL}
      </Button>
      {asking ? createPortal(
        <Sheet label="Pentru ce firmă?" onClose={close}>
          <CloseX onClose={close} />
          <h2 className="mt-0 mb-4 text-lg font-semibold">Pentru ce firmă?</h2>
          <div className="grid gap-2 [&_.btn]:w-full [&_.btn]:justify-start">
            {parts(view).map((s) => <Button key={s.co} type="button" className="btn g" onClick={() => { close(); up.pickFor(s.co); }}>{coName(s)}</Button>)}
          </div>
        </Sheet>, document.body) : null}
    </>
  );
}
