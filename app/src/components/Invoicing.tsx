"use client";
import { Button, Input, Select, Label, Form, Textarea, Actions, Panel, formTypography, surfaceStyle, sectionHeadingStyle, detailListStyle, buttonStyle, buttonTone, bannerStyle, noteLeftStyle } from "./Controls";
// Factură nouă: the sheet that drafts and issues an invoice (POST api/invoicing/<op>). The engine decides everything that
// matters (numbers, VAT, totals, rates, what SPV gets); the form collects the fields, saves the draft and shows the
// totals the engine computed for it.
import { createPortal } from "react-dom";
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, LOGO_MAX, draftPreview, getInvoicing, getPartnerPages, logoUrl, postCompany, postInvoicing, postLogo, type InvoicingOp } from "@/lib/api";
import { BankAccountForm, useSend } from "./Companies";
import { imageKind } from "@/lib/logo";
import { curOf, dayMonth, daysAfter, dm, fromCents, lei } from "@/lib/format";
import type { Address, Client, Draft, InvoiceLine, InvoiceAccount, Invoicing, InvoicingResult, Partner, Seller } from "@/lib/types";
import { useApp, useCan, useCompany, type After } from "./context";
import { useConfirm, type Ask } from "./Confirm";
import { CloseX, SHEET_SHAPES, Sheet, SheetHead, SheetSkeleton } from "./Sheets";
import { RepeatMonthly } from "./Recurring";
import { ProductField, ProductSheet, lineFrom, productFrom } from "./ProductForm";
import { PageNav, usePage } from "./Paging";
import { Row, Summary } from "./ui";

/** The invoice form's series choice that opens the new series' fields instead of choosing one. */
const NEW_SERIES = "+new";

const invoiceFormStyle = `${formTypography} my-5 min-w-0 [&_fieldset]:min-w-0 [&_fieldset]:border-0 [&_fieldset]:p-0 [&_legend]:mb-3 [&_legend]:text-base [&_legend]:font-semibold [&_[aria-invalid=true]]:border-danger`;

/** The invoicing data of a company, read again whenever the app's state changes (after every action). Only that company's:
 * null while it loads, even if another company's data is still in hand; `failed` when its last read failed. */
export function useInvoicing(co: string): { data: Invoicing | null; failed: boolean; set: (d: Invoicing) => void; retry: () => void; retrying: boolean } {
  const { view } = useApp();
  const [data, setData] = useState<Invoicing | null>(null);
  const [failedCo, setFailedCo] = useState("");
  // Reîncearcă asks for read `attempt`; it is busy until a read made for that attempt settles, even one a refresh replaced.
  const [attempt, setAttempt] = useState(0);
  const [settled, setSettled] = useState(0);
  useEffect(() => {
    let live = true;
    const mine = attempt;
    getInvoicing(co).then((d) => {
      if (live) {
        setData(d);
        setFailedCo("");
      }
    }, () => live && setFailedCo(co)).finally(() => setSettled((n) => Math.max(n, mine)));
    return () => {
      live = false;
    };
  }, [co, view, attempt]);
  // Only the latest read answers, so a late reply never overwrites a newer one.
  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { data: data?.co === co ? data : null, failed: failedCo === co, set: setData, retry, retrying: attempt > settled };
}

/** The invoicing data could not be read: say so and offer another read. */
export function InvoicingFailed({ retry, retrying }: { retry: () => void; retrying: boolean }) {
  return <div className="mt-5" role="alert"><p className={noteLeftStyle}>Datele de facturare nu au putut fi încărcate.</p>
    <Button type="button" variant="secondary" disabled={retrying} aria-busy={retrying} onClick={retry}>Reîncearcă</Button></div>;
}

const NO_ADDRESS: Address = { street: "", city: "", county: "", postal_code: "", country: "RO" };

/** The engine's VAT choice for a line, matched by its category, rate and exemption; none when the line (a draft saved
 * elsewhere, a rate since changed) matches no choice: then the user picks one, the app never guesses. */
const choiceOf = (inv: Invoicing, l: InvoiceLine) => inv.vat.choices.find((c) => c.category === l.vat_category
  && Number(c.rate) === Number(l.vat_rate) && c.exemption === l.exemption);

/** A line as the engine takes it: only these fields go back, whatever else a saved draft's line carries. */
const LINE_KEYS = ["description", "quantity", "unit", "unit_price", "vat_category", "vat_rate", "exemption"] as const;
const lineOut = (l: InvoiceLine) => ({ ...Object.fromEntries(LINE_KEYS.map((k) => [k, l[k]])), ...(l.gross_unit_price !== undefined ? { gross_unit_price: l.gross_unit_price } : {}),
  ...(l.product_id ? { product_id: l.product_id } : {}) });

/** The seller's fields the engine takes on save: what it adds when it answers (the logo, whether it can be read) stays out. */
const SELLER_KEYS = ["legal_name", "cui", "vat_payer", "vat_on_collection", "vat_id", "reg_com", "address", "iban", "bank", "email", "phone"] as const;
const sellerOut = (s: Record<string, unknown>) => Object.fromEntries(SELLER_KEYS.map((k) => [k, s[k]]));

/** A dotted path's value is given ("address.county"; a boolean counts once declared, true or false). */
function given(o: unknown, path: string): boolean {
  const v = path.split(".").reduce<unknown>((x, k) => (x && typeof x === "object" ? (x as Record<string, unknown>)[k] : undefined), o);
  return typeof v === "boolean" || (typeof v === "string" && v.trim() !== "") || typeof v === "number";
}

type SellerForm = Record<string, unknown> & { address: Address; vat_payer: boolean | null };

/** The seller's fields the engine requires, as dotted paths (the engine's `required` and the conditions it names; it checks again
 * on save). An older engine without the lists: only the VAT status, which the form never assumes. */
function sellerRequired(inv: Invoicing, seller: SellerForm): string[] {
  const r = inv.required;
  if (!r) return ["vat_payer"];
  return [...r.seller, ...(seller.address.country === "RO" ? r.seller_if_ro : []), ...(seller.vat_payer ? r.seller_if_vat_payer : [])];
}

/** The seller's required fields the form still lacks. */
const sellerMissing = (inv: Invoicing, seller: SellerForm): string[] => sellerRequired(inv, seller).filter((p) => !given(seller, p));

/** The client's fields the engine requires whatever else is filled in, as dotted paths (its `required` list for the
 * country), and whether one of the ways the engine identifies a client (`client_identity_any_of`) is given; none of those ways
 * is required alone. What the engine requires only given other answers (a VAT payer's code, a firm registered for VAT in
 * Romania, how a code is written) is left to the engine, which says so when saving: the form doesn't repeat those rules.
 * The country counts from either place. An older engine without the lists: only the name. */
function clientRules(inv: Invoicing, c: NewClient): { paths: string[]; identified: boolean } {
  const r = inv.required;
  if (!r) return { paths: ["name"], identified: true };
  const country = c.country || c.address.country;
  return { paths: country === "RO" ? r.client.RO : r.client.other,
    identified: r.client_identity_any_of.some((p) => (p === "private:true" ? c.private : given(c, p))) };
}

/** The client's fields the engine requires and the form still lacks, as dotted paths; `identity` when none of the ways the engine
 * identifies a client is given. Judged on what the form would send (`shown`), not on what it hides. */
function clientMissing(inv: Invoicing, form: NewClient): string[] {
  const c = shown(form);
  const { paths, identified } = clientRules(inv, c);
  const country = c.country || c.address.country;
  const withCountry = { ...c, country, address: { ...c.address, country } };
  return [...(identified ? [] : ["identity"]), ...paths.filter((p) => !given(withCountry, p))];
}

/** What a required field that is still empty says under it (Romanian copy follows AGENTS.md). */
const FILL = "Câmpul este obligatoriu.";
const PICK_DATE = "Lipsește data.";

/** A form's fields still missing after a press, by the id of their control: the message under each. */
type Lacking = Map<string, string>;

/** The control of a field: its element by id, else the first control of the group standing for it (`data-field`). */
function controlOf(id: string): HTMLElement | null {
  const el = document.getElementById(id) ?? document.querySelector<HTMLElement>(`[data-field="${id}"]`);
  const field = "input, select, textarea, [role=combobox]";
  return el?.matches(field) ? el : el?.querySelector<HTMLElement>(`${field}, a[href]`) ?? null;
}

/** Focuses the first of these fields on screen (in the page's order), once the render that marks them is done, so its
 * message is read with it. False when one of them is not on screen. */
function focusFirst(ids: string[]): boolean {
  const all = ids.map(controlOf);
  const els = all.filter((el): el is HTMLElement => !!el)
    .sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
  if (els.length) setTimeout(() => els[0].focus());
  return els.length === all.length;
}

const UNSHOWN = "Lipsesc date obligatorii care nu apar în formular.";

/** A form's refusal to send while the engine's required fields are missing. `complete()` is false whenever one is (whether or
 * not its control is on screen, so nothing incomplete is sent); it marks them and focuses the first one shown. `unshown`:
 * after such a press, one of them has no control, which the form says on its own. */
function useRefusal(ids: string[]): { tried: boolean; unshown: boolean; complete: () => boolean } {
  const [tried, setTried] = useState(false);
  const [hidden, setHidden] = useState(false);
  const complete = () => {
    if (!ids.length) return true;
    setTried(true);
    setHidden(!focusFirst(ids));
    return false;
  };
  return { tried, unshown: tried && hidden && ids.length > 0, complete };
}

/** Said by the buttons when a missing field has no control on the form. */
function Unshown({ id, on }: { id: string; on: boolean }) {
  return on ? <p id={id} className="mt-2 mb-0 text-sm leading-relaxed text-danger" role="alert">{UNSHOWN}</p> : null;
}

/** The attributes that tie a control to its message while it is missing. */
const marked = (lacking: Lacking, id: string, also?: string) => {
  const by = [also, lacking.has(id) ? `${id}-err` : undefined].filter(Boolean).join(" ");
  return { "aria-invalid": lacking.has(id) ? true : undefined, "aria-describedby": by || undefined };
};

/** The message under a missing field. */
function Err({ lacking, id }: { lacking: Lacking; id: string }) {
  const m = lacking.get(id);
  return m ? <p id={`${id}-err`} className="mt-2 mb-0 text-sm leading-relaxed text-danger">{m}</p> : null;
}

/** A new line with the engine's default VAT choice for this seller. */
function emptyLine(inv: Invoicing): InvoiceLine {
  const c = inv.vat.choices.find((x) => x.key === inv.vat.default) ?? inv.vat.choices[0];
  return { description: "", quantity: "1", unit: inv.units[0]?.code ?? "H87", unit_price: "", vat_category: c.category,
    vat_rate: c.rate, exemption: c.exemption };
}

/** A request through the app's one-at-a-time flow, keeping the invoicing data it returns. */
export function useInvoicingRun(co: string, set: (d: Invoicing) => void) {
  const { run } = useApp();
  return useCallback(async (op: InvoicingOp, body: Record<string, unknown>, confirm?: Ask, after?: After<InvoicingResult>) => {
    const r = await run(co, () => postInvoicing(op, { ...body, co }), confirm, after);
    if (r?.invoicing) set(r.invoicing);
    return r;
  }, [co, run, set]);
}

/** The sheet: the company's details first if it has none on its invoices, else the invoice form. */
export function NewInvoiceSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const s = useCompany();
  const { route } = useApp();
  const identity = id === "1" ? route.for : "";
  const readPartner = useCallback(async (_cursor: string, signal?: AbortSignal) => {
    try {
      const result = await getPartnerPages(s.co, { id: identity, collection: "remembered" }, signal);
      const p = result.state.partners[0];
      const rows = p && (p.roles.includes("client") || p.client_id) ? [p] : [];
      return { data: rows, total: rows.length, next_cursor: null };
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return { data: [], total: 0, next_cursor: null };
      throw e;
    }
  }, [s.co, identity]);
  const empty = useMemo(() => !identity ? { data: [], total: 0, next_cursor: null } : undefined, [identity]);
  const source = usePage(readPartner, 0, readPartner, empty);
  const { data, failed, set, retry, retrying } = useInvoicing(s.co);
  const [editSeller, setEditSeller] = useState(false);
  const canAddAccount = useCan("manage_firms") && s.can_manage_companies === true;
  // A firm whose invoices lack only its account: the form opens and takes the IBAN with the invoice. It stays open
  // once shown, so what is typed survives the account being saved.
  const [accountOnly, setAccountOnly] = useState(false);
  const lacksOnlyAccount = !!data && !data.seller && !data.accounts.length && canAddAccount
    && sellerMissing(data, data.seller_prefill).every((p) => p === "iban" || p === "bank");
  if (lacksOnlyAccount && !accountOnly) setAccountOnly(true);
  const ready = !!data?.seller || accountOnly;
  // The control that switched the seller's form in or out is gone: focus goes to the form's first field, or back to that control.
  const focusAfter = useRef("");
  const seller = (on: boolean) => {
    if (on !== editSeller) focusAfter.current = on ? "seller-legal_name" : "inv-edit-seller";
    setEditSeller(on);
  };
  useEffect(() => {
    if (!focusAfter.current) return;
    (document.getElementById(focusAfter.current) ?? document.getElementById("inv-client"))?.focus();
    focusAfter.current = "";
  }, [editSeller]);
  // Only a client of this company: a supplier, another company's partner or an unknown id chooses nothing.
  const partner = source.data[0];
  const unsaved = useRef(false);
  const label = id === "1" ? "Factură nouă" : "Ciornă de factură";
  const { ask, dialog } = useConfirm();
  // Escape, the backdrop and the close button all come here: unsaved edits are not dropped without asking.
  const close = async () => {
    if (unsaved.current && !(await ask({ text: "Închizi fără să salvezi? Modificările nu se păstrează.", yes: "Închide fără salvare", danger: true }))) return;
    onClose();
  };
  // A reload or leaving the page asks too, through the browser's own question.
  useEffect(() => {
    const leaving = (e: BeforeUnloadEvent) => {
      if (unsaved.current) e.preventDefault();
    };
    window.addEventListener("beforeunload", leaving);
    return () => window.removeEventListener("beforeunload", leaving);
  }, []);
  return (
    <Sheet onClose={close} label={label} cls="page">
      <CloseX onClose={close} />
      <SheetHead title={label} sub={<span className="font-normal text-muted">{s.companies.find((c) => c.key === s.co)?.name ?? s.co}</span>} />
      {failed && !data ? <InvoicingFailed retry={retry} retrying={retrying} /> : null}
      {(!data && !failed) || source.loading ? <SheetSkeleton shape={SHEET_SHAPES.form} /> : source.error ? <PageNav page={source} /> : null}
      {data && (!ready || editSeller) ? <Setup key={data.accounts.length} inv={data} set={set} editing={editSeller} onDone={() => seller(false)} /> : null}
      {data && ready && !editSeller ? <SeriesBehind inv={data} set={set} /> : null}
      {data && ready && !editSeller && !source.loading && !source.error ? (
        <InvoiceForm key={id + (partner?.id ?? "")} inv={data} set={set} draftId={id === "1" ? "" : id} unsavedRef={unsaved}
          onEditSeller={() => seller(true)} partner={partner} anyClient={!identity} />
      ) : null}
      {dialog}
    </Sheet>
  );
}

/** A new series: its code and first number, created by the engine (`series-create`). Inline, not a form of its own, so
 * it also fits inside the invoice form. `onDone` gets the new code, or null when cancelled. The fields are locked while the
 * request is on its way, so the code it reports is the one that was created. */
export function SeriesForm({ inv, set, onDone }: { inv: Invoicing; set: (d: Invoicing) => void; onDone: (code: string | null) => void }) {
  const send = useInvoicingRun(inv.co, set);
  const { busy } = useApp();
  const id = useId();
  const [code, setCode] = useState("");
  const [start, setStart] = useState("1");
  const add = async () => {
    const r = await send("series-create", { code, start: Number(start) }, undefined, () => false);
    if (r?.ok) onDone(code);
  };
  return <div role="group" aria-labelledby={`${id}-h`} className="col-span-full mt-4 rounded-card border border-solid border-line bg-inset p-4 sm:p-5">
    <h3 id={`${id}-h`} className="mt-0 mb-3">Serie nouă</h3>
    <div className="grid grid-cols-2 gap-x-4">
      <div>
        <Label htmlFor={`${id}-code`}>Serie</Label>
        <Input id={`${id}-code`} value={code} maxLength={20} autoComplete="off" spellCheck={false} autoFocus disabled={busy}
          onChange={(e) => setCode(e.target.value.toUpperCase())} />
      </div>
      <div>
        <Label htmlFor={`${id}-start`}>Primul număr</Label>
        <Input id={`${id}-start`} value={start} inputMode="numeric" disabled={busy} aria-describedby={`${id}-note`} onChange={(e) => setStart(e.target.value)} />
      </div>
    </div>
    <p id={`${id}-note`} className={noteLeftStyle}>După prima factură emisă, numerotarea seriei nu se mai schimbă.</p>
    <Actions>
      <Button type="button" disabled={busy || !code} onClick={() => void add()}>Adaugă seria</Button>
      <Button type="button" variant="secondary" disabled={busy} onClick={() => onDone(null)}>Renunță</Button>
    </Actions>
  </div>;
}

/** A series fiscalm has issued nothing in whose next number the imported invoices already used: the engine refuses to
 * issue in it until it continues after them, which is one press here (the engine checks again). */
function SeriesBehind({ inv, set }: { inv: Invoicing; set: (d: Invoicing) => void }) {
  const s = useCompany();
  const send = useInvoicingRun(s.co, set);
  const { busy } = useApp();
  const behind = inv.series.filter((x) => x.imported != null && x.imported >= x.next_number && x.issued === 0);
  if (!behind.length) return null;
  return (
    <>
      {behind.map((x) => (
        <div key={x.code} className={`my-4! ${bannerStyle}`} role="status">
          <b>{`Facturile importate din seria ${x.code} ajung la numărul ${x.imported}.`}</b>
          <Button type="button" className="btn g" disabled={busy} onClick={async () => {
            const r = await send("series-continue", { code: x.code, next_number: (x.imported ?? 0) + 1 }, undefined, () => false);
            if (r?.ok) setTimeout(() => document.getElementById("inv-client")?.focus()); // the banner and its button are gone
          }}>{`Continuă seria ${x.code} de la ${(x.imported ?? 0) + 1}`}</Button>
        </div>
      ))}
    </>
  );
}

/** What the invoice form holds, by field. */
type FormValues = { clientId: string; series: string; currency: string; dueDate: string; lines: InvoiceLine[];
  delivery: string; advance: boolean; advanceDate: string; notes: string; iban: string };

/** The form for a saved draft (or a new invoice). A client no longer on the list (deleted, or added again under a new
 * id) is left unpicked: the user picks it again and the rest of the draft stays. The engine refuses the old id; the app
 * never guesses the new one. */
function formOf(inv: Invoicing, d: Draft | undefined): FormValues {
  return { clientId: d && inv.clients.some((c) => c.id === d.client_id) ? d.client_id : "",
    series: d?.series ?? (inv.series.find((x) => x.default) ?? inv.series[0])?.code ?? inv.series_new?.code ?? "", currency: d?.currency ?? "RON",
    dueDate: d?.due_date ?? "", lines: d?.lines.length ? d.lines : [emptyLine(inv)], delivery: d?.delivery_date ?? "",
    advance: !!d?.advance_invoice, advanceDate: d?.advance_payment_date ?? "", notes: d?.notes ?? "",
    iban: d?.iban ?? "" };
}

/** The form's fields by their label: the value compared after a reload, and how the user's own value reads. */
const FIELDS: [string, (f: FormValues) => unknown, (f: FormValues, inv: Invoicing) => string][] = [
  ["Client", (f) => f.clientId, (f, inv) => inv.clients.find((c) => c.id === f.clientId)?.name ?? ""],
  ["Serie", (f) => f.series, (f) => f.series],
  ["Monedă", (f) => f.currency, (f) => f.currency],
  ["Scadență", (f) => f.dueDate, (f, inv) => (f.dueDate ? dm(f.dueDate, inv.today) : "")],
  ["Produse și servicii", (f) => f.lines.map(lineOut), (f, inv) => f.lines.map((l) => [l.description,
    `${l.quantity} ${inv.units.find((u) => u.code === l.unit)?.label ?? l.unit} × ${l.unit_price}`, choiceOf(inv, l)?.label ?? ""]
    .filter(Boolean).join(", ")).join("; ")],
  ["Data livrării", (f) => f.delivery, (f, inv) => (f.delivery ? dm(f.delivery, inv.today) : "")],
  ["Factură de avans", (f) => [f.advance, f.advance ? f.advanceDate : ""],
    (f, inv) => (!f.advance ? "nu" : f.advanceDate ? `da, încasat pe ${dm(f.advanceDate, inv.today)}` : "da")],
  ["Mențiuni", (f) => f.notes, (f) => f.notes],
  ["Cont", (f) => f.iban, (f, inv) => { const a = inv.invoice_accounts?.accounts.find((x) => x.iban === f.iban); return a ? accountLabel(a) : ""; }],
];

/** The labels of the fields that differ between two forms. */
const differing = (a: FormValues, b: FormValues) =>
  FIELDS.filter(([, v]) => JSON.stringify(v(a)) !== JSON.stringify(v(b))).map(([label]) => label);

/** What invoicing needs once per company: its details on the invoice (address, IBAN, VAT status). */
function Setup({ inv, set, editing = false, onDone }: { inv: Invoicing; set: (d: Invoicing) => void; editing?: boolean; onDone?: () => void }) {
  const s = useCompany();
  const send = useInvoicingRun(s.co, set);
  const { busy } = useApp();
  const canAddAccount = useCan("manage_firms") && s.can_manage_companies === true;
  const sendCompany = useSend(() => { getInvoicing(s.co).then(set, () => undefined); });
  const [seller, setSeller] = useState<Omit<Seller, "vat_payer"> & { vat_payer: boolean | null }>(() => inv.seller ? {
    ...inv.seller, iban: accountOf(inv, inv.seller.iban)?.iban ?? "" } : structuredClone(inv.seller_prefill));
  const [showAll, setShowAll] = useState(editing);
  const [requested] = useState(() => new Set([...sellerMissing(inv, seller), ...(inv.required?.seller_if_vat_payer ?? []).filter((p) => !given(seller, p))]));
  // After a press with fields missing, each one says so until it is filled in. Required is what the engine's lists say.
  const need = new Set(sellerRequired(inv, seller));
  // The bank's name comes from the account's IBAN when the bank catalogue knows it (the engine fills it in); asked only
  // otherwise. Without an account there is no bank to name either: the account's note says what to do for both.
  const bankOf = accountOf(inv, seller.iban)?.bank_name ?? "";
  const missing = sellerMissing(inv, seller).map((p) => (p === "vat_payer" ? "seller-vat" : `seller-${p.split(".").pop()}`))
    .filter((id) => id !== "seller-bank" || (inv.accounts.length && !bankOf));
  const { tried, unshown, complete } = useRefusal(missing);
  const lacking: Lacking = new Map(tried ? missing.map((id) => [id, id === "seller-vat" ? "Lipsește alegerea: firma este sau nu plătitoare de TVA."
    : id === "seller-iban" && !inv.accounts.length ? NO_ACCOUNT : id === "seller-iban" ? "Lipsește contul." : FILL]) : []);
  const field = (k: keyof typeof seller, label: string, opt = false, auto = "off") => (
    (showAll || requested.has(k)) && <Field id={`seller-${k}`} label={label} opt={opt} req={need.has(k)} value={String(seller[k] ?? "")}
      onChange={(v) => setSeller({ ...seller, [k]: v })} auto={auto} lacking={lacking} />
  );
  const addr = (k: keyof Address, label: string, opt = false, auto = "off") => (
    (showAll || requested.has(`address.${k}`)) && <Field id={`seller-${k}`} label={label} opt={opt} req={need.has(`address.${k}`)} value={seller.address[k]} auto={auto} lacking={lacking}
      onChange={(v) => setSeller({ ...seller, address: { ...seller.address, [k]: v } })} />
  );
  const vat = marked(lacking, "seller-vat");
  const saveSeller = async () => {
    if (!complete()) return;
    const r = await send("seller-save", { seller: sellerOut(seller) }, undefined, () => false);
    if (r?.ok) onDone?.();
  };
  if (!inv.accounts.length && canAddAccount) return <BankAccountForm
    c={{ key: s.co, name: s.companies.find((c) => c.key === s.co)?.name ?? s.co }}
    busy={busy} send={sendCompany} title="Contul pe factură" />;
  return (
    <>
      {editing && inv.seller ? <LogoField inv={inv} set={set} /> : null}
      {!inv.seller || editing ? (
        <Form className={invoiceFormStyle} onSubmit={(e) => e.preventDefault()} aria-labelledby="setup-seller" noValidate>
          <h3 id="setup-seller" className="mt-0 mb-4 text-base font-semibold">{showAll ? "Datele firmei pe factură" : "Completează pentru facturare"}</h3>
          <div className="grid gap-x-4 sm:grid-cols-2">
            {field("legal_name", "Denumire", false, "organization")}
            {field("cui", "CUI")}
            {field("reg_com", "Nr. Registrul Comerțului", true)}
            {/* A radio group: the question is required, and while unanswered the group and each choice say so. */}
            {showAll || requested.has("vat_payer") ? <fieldset className="col-span-full m-0 mt-5 mb-3" data-field="seller-vat" role="radiogroup" aria-required={need.has("vat_payer") || undefined} {...vat}>
              <legend>TVA</legend>
              <Label className="min-h-11 cursor-pointer"><Input type="radio" name="vat-payer" checked={seller.vat_payer === true}
                required={need.has("vat_payer")} {...vat} onChange={() => setSeller({ ...seller, vat_payer: true })} /> Plătitor de TVA</Label>
              <Label className="min-h-11 cursor-pointer"><Input type="radio" name="vat-payer" checked={seller.vat_payer === false}
                required={need.has("vat_payer")} {...vat} onChange={() => setSeller({ ...seller, vat_payer: false, vat_id: "", vat_on_collection: false })} /> Neplătitor de TVA</Label>
              <Err lacking={lacking} id="seller-vat" />
              {seller.vat_payer ? (
                <Label className="min-h-11 cursor-pointer"><Input type="checkbox" checked={!!seller.vat_on_collection}
                  onChange={(e) => setSeller({ ...seller, vat_on_collection: e.target.checked })} /> TVA la încasare</Label>
              ) : null}
            </fieldset> : null}
            {addr("street", "Adresă", false, "street-address")}
            {bySector(seller.address) ? null : addr("city", "Localitate", false, "address-level2")}
            {seller.address.country !== "RO" ? addr("county", "Regiune", true, "address-level1")
              : showAll || requested.has("address.county") ? <CountyField inv={inv} id="seller-county" req={need.has("address.county")} lacking={lacking}
                address={seller.address} onChange={(address) => setSeller({ ...seller, address })} /> : null}
            {addr("postal_code", "Cod poștal", true, "postal-code")}
            {showAll || requested.has("iban") ? <div className="col-span-full">
            <AccountPick inv={inv} co={s.co} iban={seller.iban} lacking={lacking} need={need.has("iban")}
              onPick={(a) => setSeller({ ...seller, iban: a?.iban ?? "", bank: a ? a.bank_name || (seller.iban === a.iban ? seller.bank : "") : "" })} />
            </div> : null}
            {inv.accounts.length && !bankOf ? field("bank", "Banca") : null}
            {field("email", "E-mail", true, "email")}
          </div>
          <Actions>
            {/* Enabled with fields missing: a press says which, by each of them, and goes to the first. The engine checks again. */}
            <Button type="button" className="btn" disabled={busy} onClick={saveSeller}
              aria-describedby={unshown ? "seller-unshown" : undefined}>{showAll ? "Salvează datele firmei" : "Continuă"}</Button>
            {!showAll ? <Button type="button" className="btn g" onClick={() => setShowAll(true)}>Modifică datele firmei</Button> : null}
            {editing ? <Button type="button" className="btn g" onClick={onDone}>Renunță</Button> : null}
          </Actions>
          <Unshown id="seller-unshown" on={unshown} />
        </Form>
      ) : null}
    </>
  );
}

/** The logo printed on the company's invoices: shown, replaced or removed. The engine checks the image (type, size,
 * dimensions); the app only stops what is plainly not a PNG or JPEG or too heavy, before sending it. */

function LogoField({ inv, set }: { inv: Invoicing; set: (d: Invoicing) => void }) {
  const s = useCompany();
  const { busy, run } = useApp();
  const send = useInvoicingRun(s.co, set);
  const input = useRef<HTMLInputElement>(null);
  const [problem, setProblem] = useState("");
  const logo = inv.seller?.logo ?? null;
  const broken = !logo && !!inv.seller?.logo_unavailable;
  const button = useRef<HTMLButtonElement>(null);
  const upload = async (file: File | undefined) => {
    setProblem("");
    if (!file) return;
    // The type from the first bytes: a browser may give none, or image/jpg, for a real JPEG. The engine checks it again.
    const kind = await imageKind(file);
    if (!kind) return setProblem("Se acceptă o imagine PNG sau JPEG.");
    if (file.size > LOGO_MAX) return setProblem("Imaginea poate avea cel mult 512 KB.");
    // With no answer, the app reads its state again and the invoicing data follows (useInvoicing), so nothing to do here.
    const r = await run(s.co, () => postLogo(s.co, file, kind), undefined, () => false);
    if (r?.invoicing) set(r.invoicing);
    if (r && !r.ok) setProblem(r.message); // the engine's reason, next to the control as well as in the toast
    setTimeout(() => button.current?.focus()); // the button was disabled while sending
  };
  return (
    <fieldset className={`logo m-0 mb-6 p-5 ${surfaceStyle}`}>
      <legend className="mt-0 mb-4 text-base font-semibold">Sigla pe factură</legend>
      {logo ? (
        // eslint-disable-next-line @next/next/no-img-element -- a token-scoped URL from the engine, not an optimisable asset
        <img className="mt-1 mb-2 box-content block rounded-control bg-white p-1.5" src={logoUrl(s.co, logo.sha)} alt="Sigla actuală" width={Math.round(logo.width * Math.min(48 / logo.height, 168 / logo.width))}
          height={Math.round(logo.height * Math.min(48 / logo.height, 168 / logo.width))} />
      ) : broken ? (
        <p className={noteLeftStyle} role="alert">Sigla nu mai poate fi citită. Până o încarci din nou sau o elimini, facturile nu se pot emite.</p>
      ) : <p className={noteLeftStyle}>Fără siglă, pe factură apare numele firmei.</p>}
      <p className={noteLeftStyle} id="logo-hint">PNG sau JPEG, cel mult 512 KB, între 32 și 1200 de pixeli pe latură. Apare sus în stânga pe facturi și se salvează imediat.</p>
      <Input ref={input} type="file" accept="image/png,image/jpeg" hidden
        onChange={(e) => {
          void upload(e.target.files?.[0]);
          e.target.value = "";
        }} />
      {problem ? <p className={noteLeftStyle} role="alert">{problem}</p> : null}
      <Actions>
        <Button ref={button} type="button" className="btn g" disabled={busy} aria-describedby="logo-hint" onClick={() => input.current?.click()}>
          {logo || broken ? "Înlocuiește sigla" : "Încarcă o siglă"}
        </Button>
        {logo || broken ? (
          <Button type="button" className="btn g" disabled={busy}
            onClick={async () => {
              const r = await send("logo-remove", {}, { text: "Elimini sigla? Facturile noi vor avea numele firmei în locul ei.", yes: "Elimină sigla", danger: true }, () => false);
              if (r?.ok) setProblem("");
              else if (r) setProblem(r.message);
              setTimeout(() => button.current?.focus()); // its own button may be gone, and was disabled while sending
            }}>
            Elimină sigla
          </Button>
        ) : null}
      </Actions>
    </fieldset>
  );
}

function InvoiceForm({ inv, set, draftId, unsavedRef, onEditSeller, partner, anyClient }: {
  inv: Invoicing; set: (d: Invoicing) => void; draftId: string; unsavedRef: { current: boolean }; onEditSeller: () => void; partner?: Partner;
  /** Opened for no partner in particular: the firm's only client is the one. */
  anyClient: boolean;
}) {
  const s = useCompany();
  const { busy, run } = useApp();
  const send = useInvoicingRun(s.co, set);
  const saved: Draft | undefined = inv.drafts.find((d) => d.id === draftId);
  // A firm without an account on its invoices: its IBAN is written here and saved as the firm's account with the first save.
  const needsAccount = !inv.seller && !inv.accounts.length;
  // Saved, the account still lacks its bank's name when the bank catalogue doesn't know it: asked here too, never by
  // leaving the form.
  const needsBank = !inv.seller && !needsAccount;
  const [iban, setIban] = useState("");
  const [bank, setBank] = useState("");
  const [id, setId] = useState(draftId);
  const [init] = useState(() => formOf(inv, saved));
  const [series, setSeries] = useState(init.series);
  // The company's first series is written here and created with the first save: the engine proposes its code and number.
  const creating = !inv.series.length;
  const [addingSeries, setAddingSeries] = useState(false);
  const [start, setStart] = useState(String(inv.series_new?.next_number ?? 1));
  // A new invoice for a partner: its saved client, else the new client's form filled in with what the engine knows of it.
  const listed = (cid: string) => inv.clients.some((c) => c.id === cid);
  // Opened for no partner, the firm's only client is the one: between several, or for a partner that isn't a client,
  // the user picks; the form never guesses.
  const [forClient] = useState(() => (partner?.client_id && listed(partner.client_id) ? partner.client_id
    : anyClient && !saved && inv.clients.length === 1 ? inv.clients[0].id : ""));
  const [clientId, setClientId] = useState(saved ? init.clientId : forClient);
  // The new client's form, while open, stands for the client: the invoice's own buttons save it first.
  const [adding, setAdding] = useState(!inv.clients.length || (!!partner && !forClient));
  const saveClient = useRef<(() => Promise<string | null>) | null>(null);
  // A new invoice proposes the due date after the usual term and the lines of the client's latest invoice, until the
  // user changes them; the engine says what is usual, the form never sends a proposal the user hasn't seen.
  const [proposing, setProposing] = useState({ due: !saved, lines: !saved });
  const lastOf = (cid: string) => inv.clients.find((c) => c.id === cid)?.last_invoice;
  const dueAfter = (cid: string, from: string) => (from ? daysAfter(from, lastOf(cid)?.due_days ?? inv.due_days) : "");
  const linesOf = (cid: string) => lastOf(cid)?.lines.map((l) => ({ ...l })) ?? [emptyLine(inv)];
  const gone = saved && !clientId && !listed(saved.client_id) ? saved.client.name : null;
  // An invoice is issued today, whatever day its draft was saved on: the engine refuses any other issue date.
  const issueDate = inv.today;
  const [dueDate, setDueDate] = useState(saved ? init.dueDate : dueAfter(forClient, issueDate));
  // The client's latest lines come with their currency: their prices are in it.
  const [currency, setCurrency] = useState(saved || !forClient ? init.currency : lastOf(forClient)?.currency ?? init.currency);
  const [lines, setLines] = useState<InvoiceLine[]>(saved || !forClient ? init.lines : linesOf(forClient));
  const [notes, setNotes] = useState(init.notes);
  const [delivery, setDelivery] = useState(init.delivery);
  const [advance, setAdvance] = useState(init.advance);
  const [advanceDate, setAdvanceDate] = useState(init.advanceDate);
  // The account on this invoice: a saved draft's is the one the engine issues it with; a new invoice follows the
  // engine's default for its currency until one is picked.
  const [chosenIban, setChosenIban] = useState(init.iban);
  const account = chosenIban || (saved ? "" : inv.invoice_accounts?.default[currency] ?? "");
  const choosable = inv.invoice_accounts?.accounts ?? [];
  const accountKnown = choosable.some((a) => a.iban === account);
  const [dirty, setDirty] = useState(!saved);
  const [notIssued, setNotIssued] = useState(false);
  // Repetă lunar's fields, open under the saved draft (as the engine holds it now).
  const [repeat, setRepeat] = useState(false);
  const repeatDraft = repeat && id && !dirty ? inv.drafts.find((d) => d.id === id) : undefined;
  const [focusLine, setFocusLine] = useState<number | null>(null);
  // Produs nou, over the invoice, for this line: the form stays as it is under it and the line takes the saved product.
  const [newProduct, setNewProduct] = useState<number | null>(null);
  const products = inv.products ?? [];
  const named = (pid?: string | null) => products.find((p) => p.id === pid)?.name;
  const closeNewProduct = (i: number) => {
    setNewProduct(null);
    requestAnimationFrame(() => document.getElementById(`l${i}-d`)?.focus());
  };
  useEffect(() => {
    if (focusLine !== null) document.getElementById(`l${focusLine}-d`)?.focus();
  }, [focusLine, lines.length]);
  // Every edit takes a number; a save knows which version it sent, so an edit made while it was on its way stays unsaved.
  const rev = useRef(0);
  const [version, setVersion] = useState(saved?.version);
  // The engine refused a save, issue or delete because the draft changed elsewhere: its message, until the draft is reloaded.
  const [stale, setStale] = useState("");
  // After a reload that replaced what the user had typed: their version, so nothing they wrote is lost without them seeing it.
  const [reloaded, setReloaded] = useState<{ mine: FormValues; theirs: [string, string][]; changed: string[]; typed: boolean; untouched: boolean } | null>(null);
  const [reloadProblem, setReloadProblem] = useState("");
  // One reload at a time: a second click, or a second answer, can't replace what the first one set aside.
  const [reloading, setReloading] = useState(false);
  const reloadingRef = useRef(false);
  const values = (): FormValues => ({ clientId, series, currency, dueDate, lines, delivery, advance, advanceDate, notes, iban: account });
  // The form as it is on screen now, for a reload's answer: the fields stay editable while it is on its way.
  const latest = useRef({ values: values(), dirty });
  useLayoutEffect(() => {
    latest.current = { values: values(), dirty };
  });
  const fill = (f: FormValues) => {
    setClientId(f.clientId);
    setSeries(f.series);
    setCurrency(f.currency);
    setDueDate(f.dueDate);
    setLines(f.lines);
    setDelivery(f.delivery);
    setAdvance(f.advance);
    setAdvanceDate(f.advanceDate);
    setNotes(f.notes);
    setChosenIban(f.iban);
  };
  const refused = (r: InvoicingResult | null) => {
    if (r?.code === "stale_draft") setStale(r.message);
  };
  const draft = inv.drafts.find((d) => d.id === id);
  // A draft saved on another day is saved again with today's date before it is issued.
  const redate = !!draft && draft.issue_date !== issueDate;
  const needsDelivery = lines.some((l) => choiceOf(inv, l)?.needs_delivery_date);
  // An intra-community supply (K) needs fields of the client's that the engine checks only at issue: said here, before that.
  const picked = inv.clients.find((x) => x.id === clientId);
  const clientLacksForK = !!picked && lines.some((l) => l.vat_category === "K")
    && (inv.required?.client_if_k ?? []).some((p) => !given(picked, p));
  const change = <T,>(f: (v: T) => void) => (v: T) => {
    f(v);
    rev.current += 1;
    setReloaded((x) => x && (x.untouched ? { ...x, untouched: false } : x));
    unsavedRef.current = true; // something typed: closing asks first
    setDirty(true);
  };
  const editLines = (next: InvoiceLine[]) => {
    setProposing((p) => ({ ...p, lines: false }));
    change(setLines)(next);
  };
  const pickClient = (cid: string) => {
    change(setClientId)(cid);
    if (proposing.due) setDueDate(dueAfter(cid, issueDate));
    if (proposing.lines) {
      setLines(linesOf(cid));
      setCurrency(lastOf(cid)?.currency ?? "RON");
    }
  };
  const pickDueDate = (day: string) => {
    setProposing((p) => ({ ...p, due: false }));
    change(setDueDate)(day);
  };
  // A line priced with VAT (a shop order's) keeps the amount the customer paid: its price field edits that amount and
  // a new VAT rate only changes how the engine splits it, so no edit turns a gross price into a net one.
  const setLine = (i: number, patch: Partial<InvoiceLine>) => editLines(lines.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const linePrice = (l: InvoiceLine) => l.gross_unit_price ?? l.unit_price;
  const setPrice = (i: number, price: string) => setLine(i, lines[i].gross_unit_price !== undefined ? { gross_unit_price: price } : { unit_price: price });
  // A draft's delivery date stays on it, shown and editable, even once no line needs one (a K line changed to another VAT).
  const showsDelivery = needsDelivery || !!init.delivery || !!delivery;
  // What a draft needs before it is sent, in the form's order: the field's id and what it says under it once pressed.
  const missing: [string, string][] = [
    ...(clientId || adding ? [] : [["inv-client", "Lipsește clientul."] as [string, string]]),
    ...(series ? [] : [["inv-series", "Lipsește seria."] as [string, string]]),
    ...(creating && !/^[1-9]\d*$/.test(start) ? [["inv-start", FILL] as [string, string]] : []),
    ...(needsAccount && !iban.trim() ? [["inv-iban", FILL] as [string, string]] : []),
    ...(needsBank && !bank.trim() ? [["inv-bank", FILL] as [string, string]] : []),
    ...(dueDate ? [] : [["inv-due", PICK_DATE] as [string, string]]),
    ...lines.flatMap((l, i) => [[`l${i}-d`, l.description.trim() ? "" : FILL], [`l${i}-q`, l.quantity.trim() ? "" : FILL],
      [`l${i}-p`, linePrice(l).trim() ? "" : FILL], [`l${i}-v`, choiceOf(inv, l) ? "" : "Lipsește TVA-ul."]] as [string, string][])
      .filter(([, m]) => m),
    ...(needsDelivery && !delivery ? [["inv-delivery", PICK_DATE] as [string, string]] : []),
  ];
  const { tried, unshown, complete } = useRefusal(missing.map(([k]) => k));
  const lacking: Lacking = new Map(tried ? missing : []);
  const req = (id: string, also?: string) => ({ "aria-required": true, ...marked(lacking, id, also) });
  const body = (client: string) => ({ ...(id ? { id, version } : {}), series, client_id: client, issue_date: issueDate, due_date: dueDate, currency,
    lines: lines.map(lineOut), notes, ...(showsDelivery ? { delivery_date: delivery } : {}),
    advance_invoice: advance, advance_payment_date: advance && advanceDate ? advanceDate : null,
    ...(saved?.supply_country ? { supply_country: saved.supply_country } : {}), ...(account ? { iban: account } : {}) });
  /** Save; `clean` says nothing changed while the save was on its way (only then is what's saved what's on screen). */
  const save = async () => {
    const failed = { ok: false, id: null, clean: false, invoicing: null, version };
    // What is on screen now is what goes: an edit from here on stays unsaved. Choosing the client just saved is one edit.
    const before = rev.current;
    const client = adding ? await saveClient.current?.() : clientId;
    if ((adding && !client) || !complete() || !client) return failed;
    if (needsAccount) {
      const a = await run("ALL", () => postCompany("company-bank", { key: s.co, iban: iban.replace(/\s+/g, "").toUpperCase() }), undefined, () => false);
      if (!a?.ok) return failed;
      const fresh = await getInvoicing(s.co).catch(() => null);
      if (fresh) set(fresh);
      if (!fresh?.seller) {
        setTimeout(() => document.getElementById("inv-bank")?.focus()); // its bank's name comes next, in the form
        return failed;
      }
    }
    if (needsBank) {
      const seller = sellerOut({ ...inv.seller_prefill, iban: (inv.accounts.find((a) => a.pays) ?? inv.accounts[0]).iban, bank: bank.trim() });
      if (!(await send("seller-save", { seller }, undefined, () => false))?.ok) return failed;
    }
    const sent = before + (adding ? 1 : 0);
    const made = creating ? await send("series-create", { code: series, start: Number(start) }, undefined, () => false) : null;
    if (made && !made.ok) return failed;
    const r = await send("draft-save", body(client), undefined, () => false);
    const clean = rev.current === sent;
    refused(r);
    if (r?.ok && r.id) {
      setId(r.id);
      setVersion(r.version);
      setReloaded(null);
      setDirty(!clean);
      if (clean) unsavedRef.current = false;
    }
    return { ok: !!r?.ok, id: r?.id ?? null, clean, invoicing: r?.invoicing ?? null, version: r?.version };
  };
  /** Saves first when needed, then asks with the engine's total for exactly what is saved, then issues that draft. */
  const issue = async () => {
    setNotIssued(false);
    if (!adding && !complete()) return;
    const r = dirty || !id || adding || redate ? await save() : { ok: true, id, clean: true, invoicing: inv, version };
    if (!r.ok || !r.id) return;
    if (!r.clean) return setNotIssued(true); // an edit made during the save is not on the saved draft: nothing is issued
    const d = (r.invoicing ?? inv).drafts.find((x) => x.id === r.id);
    const who = d?.client.name ?? inv.clients.find((c) => c.id === clientId)?.name ?? ""; // the client the draft was saved with
    const total = d ? `, ${lei(d.totals.total)} ${curOf(d.currency)}` : "";
    // The series, not a number: another issue may take the next number between this question and the answer.
    const confirmText = `Emiți factura în seria ${series} către ${who}${total}?`
      + (inv.upload_after_days === null ? "" : inv.upload_after_days === 0 ? " Se trimite automat în SPV."
        : ` Se trimite automat în SPV peste ${inv.upload_after_days === 1 ? "o zi" : `${inv.upload_after_days} zile`}.`);
    refused(await send("issue", { id: r.id, version: r.version }, { text: confirmText, yes: "Emite factura" },
      (x) => (x.id ? { co: s.co, tab: "vanzari", iss: x.id } : undefined)));
  };
  /** Reads the draft again and shows it as saved now, with its new version. What the user has on screen when the answer
   * arrives (typed before or during the read) and differs is kept aside: shown, restorable while they haven't edited
   * since, and closing the form asks first until they use it, save or close. Nothing changes when the draft can't be
   * read, and a reload already on its way is not started again. */
  const reload = async () => {
    if (reloadingRef.current) return;
    reloadingRef.current = true;
    setReloading(true);
    setReloadProblem("");
    try {
      let fresh: Invoicing;
      try {
        fresh = await getInvoicing(s.co);
      } catch {
        return setReloadProblem("Ciorna nu a putut fi reîncărcată. Încearcă din nou.");
      }
      set(fresh);
      const d = fresh.drafts.find((x) => x.id === id);
      if (!d) return setReloadProblem("Ciorna nu mai există: a fost ștearsă sau emisă.");
      const { values: mine, dirty: typed } = latest.current;
      const now = formOf(fresh, d);
      const changed = differing(mine, now);
      fill(now);
      setVersion(d.version);
      setStale("");
      setDirty(false);
      unsavedRef.current = typed && changed.length > 0; // their text is only aside now: closing still asks
      rev.current += 1; // a save still on its way is not what's on screen now
      setReloaded({ mine, changed, typed, untouched: true,
        theirs: FIELDS.filter(([label]) => changed.includes(label)).map(([label, , text]) => [label, text(mine, inv)]) });
    } finally {
      reloadingRef.current = false;
      setReloading(false);
    }
  };
  /** Puts back what the user had typed, on top of the draft's new version: saving it then replaces the other edit. */
  const keepMine = () => {
    if (!reloaded) return;
    fill(reloaded.mine);
    rev.current += 1;
    unsavedRef.current = true;
    setDirty(true);
    setReloaded(null);
  };
  return (
    <Form className={invoiceFormStyle} onSubmit={(e) => e.preventDefault()} noValidate>
      <div className="grid min-w-0 gap-6 md:grid-cols-[minmax(0,1fr)_minmax(240px,0.65fr)] min-[1100px]:mx-auto min-[1100px]:max-w-[1280px] min-[1100px]:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] min-[1100px]:gap-10">
        <div className="min-w-0">
          <ClientPicker inv={inv} set={set} value={clientId} onChange={pickClient} onSaved={change(setClientId)} lacking={lacking} adding={adding}
            onEdit={() => { unsavedRef.current = true; }}
            setAdding={setAdding} bind={(f) => { saveClient.current = f; }} prefill={partner && !forClient ? asClient(partner) : undefined} notice={gone === null ? undefined
            : `Clientul ciornei${gone ? `, ${gone},` : ""} nu mai este în lista de clienți. Alege-l din nou sau adaugă-l. Restul ciornei rămâne neschimbat.`} />
          <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3">
            <div>
              <Label htmlFor="inv-series">Serie</Label>
              {creating ? (
                <Input id="inv-series" {...req("inv-series")} value={series} maxLength={20} autoComplete="off" spellCheck={false}
                  onChange={(e) => change(setSeries)(e.target.value.toUpperCase())} />
              ) : (
                <Select id="inv-series" {...req("inv-series")} value={series} disabled={addingSeries}
                  onChange={(e) => (e.target.value === NEW_SERIES ? setAddingSeries(true) : change(setSeries)(e.target.value))}>
                  {inv.series.map((x) => <option key={x.code} value={x.code}>{x.code} (nr. {x.next_number})</option>)}
                  <option value={NEW_SERIES}>Serie nouă…</option>
                </Select>
              )}
              <Err lacking={lacking} id="inv-series" />
            </div>
            {addingSeries ? <SeriesForm inv={inv} set={set} onDone={(code) => {
              setAddingSeries(false);
              if (code) change(setSeries)(code);
              setTimeout(() => document.getElementById("inv-series")?.focus());
            }} /> : null}
            {creating ? (
              <div>
                <Label htmlFor="inv-start">Primul număr</Label>
                <Input id="inv-start" {...req("inv-start")} value={start} inputMode="numeric" onChange={(e) => change(setStart)(e.target.value)} />
                <Err lacking={lacking} id="inv-start" />
              </div>
            ) : null}
            <div>
              <Label htmlFor="inv-currency">Monedă</Label>
              <Select id="inv-currency" value={currency} onChange={(e) => {
                setProposing((p) => ({ ...p, lines: false }));
                change(setCurrency)(e.target.value);
              }}>
                <option value="RON">Lei (RON)</option>
                <option value="EUR">Euro (EUR)</option>
                {/* A currency only an earlier invoice used is shown as it is, never as another one. */}
                {currency !== "RON" && currency !== "EUR" ? <option value={currency}>{currency}</option> : null}
              </Select>
            </div>
            {/* With one account there is nothing to choose, unless the draft's account is no longer one of the firm's. */}
            {inv.invoice_accounts && (choosable.length > 1 || !accountKnown) ? (
              <div className="col-span-full">
                <Label htmlFor="inv-account">Cont</Label>
                <Select id="inv-account" value={accountKnown ? account : ""} onChange={(e) => change(setChosenIban)(e.target.value)}>
                  {!accountKnown ? <option value="">Alege contul</option> : null}
                  {choosable.map((a) => <option key={a.iban} value={a.iban}>{accountLabel(a)}</option>)}
                </Select>
              </div>
            ) : null}
            <div>
              <Label htmlFor="inv-date">Data emiterii</Label>
              <output id="inv-date" className="flex min-h-11 items-center">{`Azi, ${dayMonth(issueDate)}`}</output>
            </div>
            <div>
              <Label htmlFor="inv-due">Scadență</Label>
              <Input id="inv-due" type="date" {...req("inv-due")} value={dueDate} min={issueDate} onChange={(e) => pickDueDate(e.target.value)} />
              <Err lacking={lacking} id="inv-due" />
            </div>
            {needsAccount ? (
              <div className="col-span-full">
                <Label htmlFor="inv-iban">IBAN-ul firmei</Label>
                <Input id="inv-iban" {...req("inv-iban")} value={iban} maxLength={42} autoComplete="off" spellCheck={false}
                  data-1p-ignore data-lpignore="true" data-bwignore="true" onChange={(e) => change(setIban)(e.target.value)} />
                <Err lacking={lacking} id="inv-iban" />
              </div>
            ) : null}
            {needsBank ? (
              <div className="col-span-full">
                <Label htmlFor="inv-bank">Banca</Label>
                <Input id="inv-bank" {...req("inv-bank")} value={bank} maxLength={100} autoComplete="off"
                  onChange={(e) => change(setBank)(e.target.value)} />
                <Err lacking={lacking} id="inv-bank" />
              </div>
            ) : null}
        </div>
      <fieldset className="m-0 mt-6">
        <legend>Produse și servicii</legend>
        {lines.map((l, i) => (
          <div className={`mb-4 min-w-0 bg-inset p-4 ${surfaceStyle}`} key={i} role="group" aria-label={`Linia ${i + 1}`}>
            <Label htmlFor={`l${i}-d`}>Produs sau serviciu</Label>
            <ProductField id={`l${i}-d`} {...req(`l${i}-d`)} value={l.description} linked={!!l.product_id} maxLength={inv.line_description_max} products={products}
              units={inv.units} onType={(description) => setLine(i, { description, ...(named(l.product_id) === description ? {} : { product_id: null }) })}
              onPick={(p) => editLines(lines.map((x, j) => (j === i ? lineFrom(p, x, currency) : x)))} onNew={() => setNewProduct(i)} />
            <Err lacking={lacking} id={`l${i}-d`} />
            <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.3fr)] gap-3">
              <div>
                <Label htmlFor={`l${i}-q`}>Cantitate</Label>
                <Input id={`l${i}-q`} {...req(`l${i}-q`)} inputMode="decimal" value={l.quantity} onChange={(e) => setLine(i, { quantity: e.target.value })} />
                <Err lacking={lacking} id={`l${i}-q`} />
              </div>
              <div>
                <Label htmlFor={`l${i}-u`}>UM</Label>
                <Select id={`l${i}-u`} aria-required value={l.unit} onChange={(e) => setLine(i, { unit: e.target.value })}>
                  {inv.units.map((u) => <option key={u.code} value={u.code}>{u.label}</option>)}
                </Select>
              </div>
              <div>
                <Label htmlFor={`l${i}-p`}>{l.gross_unit_price !== undefined ? "Preț cu TVA" : "Preț unitar"}</Label>
                <Input id={`l${i}-p`} {...req(`l${i}-p`)} inputMode="decimal" value={linePrice(l)} onChange={(e) => setPrice(i, e.target.value)} />
                <Err lacking={lacking} id={`l${i}-p`} />
              </div>
            </div>
            <Label htmlFor={`l${i}-v`}>TVA</Label>
            <Select id={`l${i}-v`} {...req(`l${i}-v`)} value={choiceOf(inv, l)?.key ?? ""} onChange={(e) => {
              const o = inv.vat.choices.find((x) => x.key === e.target.value);
              if (o) setLine(i, { vat_category: o.category, vat_rate: o.rate, exemption: o.exemption });
            }}>
              {!choiceOf(inv, l) ? <option value="">Alege TVA-ul</option> : null}
              {inv.vat.choices.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
            </Select>
            <Err lacking={lacking} id={`l${i}-v`} />
            {lines.length > 1 ? (
              <Button type="button" variant="quiet" onClick={() => {
                editLines(lines.filter((_, j) => j !== i));
                setFocusLine(Math.min(i, lines.length - 2));
              }}>
                Șterge linia {i + 1}
              </Button>
            ) : null}
          </div>
        ))}
        <Button type="button" className="btn g" onClick={() => {
          editLines([...lines, emptyLine(inv)]);
          setFocusLine(lines.length);
        }}>Adaugă o linie</Button>
      </fieldset>
      {newProduct !== null && lines[newProduct] ? createPortal(<ProductSheet inv={inv} start={productFrom(inv, lines[newProduct], currency)}
        onClose={() => closeNewProduct(newProduct)} onSaved={(p) => {
          editLines(latest.current.values.lines.map((x, j) => (j === newProduct ? lineFrom(p, x, latest.current.values.currency) : x)));
          closeNewProduct(newProduct);
        }} />, document.body) : null}
      {showsDelivery ? (
        <>
          <Label htmlFor="inv-delivery">{needsDelivery ? "Data livrării" : "Data livrării (opțional)"}</Label>
          <Input id="inv-delivery" type="date" {...req("inv-delivery")} value={delivery} onChange={(e) => change(setDelivery)(e.target.value)} />
          <Err lacking={lacking} id="inv-delivery" />
        </>
      ) : null}
      <Label className="min-h-11 cursor-pointer"><Input type="checkbox" checked={advance}
        onChange={(e) => change(setAdvance)(e.target.checked)} /> Factură de avans</Label>
      {advance ? (
        <>
          <Label htmlFor="inv-advance">Data încasării avansului (opțional)</Label>
          <Input id="inv-advance" type="date" value={advanceDate} onChange={(e) => change(setAdvanceDate)(e.target.value)} />
        </>
      ) : null}
      <Label htmlFor="inv-notes">Mențiuni (opțional)</Label>
      <Textarea id="inv-notes" value={notes} maxLength={300} rows={2} onChange={(e) => change(setNotes)(e.target.value)} />
      </div>
        <aside className="min-w-0 md:sticky md:top-0 md:self-start min-[1100px]:rounded-panel min-[1100px]:bg-inset min-[1100px]:p-8" aria-label="Totaluri și emitere">
          <Paper inv={inv} series={series} clientId={adding ? "" : clientId} adding={adding} dueDate={dueDate} lines={lines}>
            <TotalsCard inv={inv} draft={dirty || redate ? undefined : draft} lines={lines} currency={currency} />
          </Paper>
          <Actions>
            {/* An unchanged draft saves too: the engine asks for that when the client's details changed since the draft was saved.
              With fields missing both stay enabled: a press says which, by each of them, and goes to the first. */}
            <Button type="button" className="btn g" disabled={busy || !!stale} onClick={save}
              aria-describedby={[stale ? "inv-stale" : "", unshown ? "inv-unshown" : ""].filter(Boolean).join(" ") || undefined}>Salvează ciorna</Button>
            <Button type="button" className="btn" disabled={busy || clientLacksForK || !!stale} onClick={issue}
              aria-describedby={[stale ? "inv-stale" : "", clientLacksForK ? "inv-k-region" : "",
                unshown ? "inv-unshown" : ""].filter(Boolean).join(" ") || undefined}>Emite factura</Button>
          </Actions>
          <Unshown id="inv-unshown" on={unshown} />
        </aside>
      </div>
      {clientLacksForK ? (
        <p id="inv-k-region" className={noteLeftStyle} role="status">Pentru o livrare intracomunitară, completează și regiunea din adresa clientului. Adaugă din nou clientul, cu regiunea completată.</p>
      ) : null}
      {stale ? (
        <div className={noteLeftStyle} role="alert">
          <p id="inv-stale">{stale}</p>
          <Button type="button" className="btn g" disabled={busy || reloading} aria-busy={reloading} onClick={reload}>Reîncarcă ciorna</Button>
          {reloadProblem ? <p>{reloadProblem}</p> : null}
        </div>
      ) : null}
      {reloaded ? (
        <div className={noteLeftStyle} role="status">
          <p>{"Ciorna salvată a fost reîncărcată."
            + (!reloaded.changed.length ? "" : `${reloaded.typed ? " Varianta din formular diferă la: " : " S-au modificat: "}`
              + `${reloaded.changed.map((x) => x.toLowerCase()).join(", ")}.`)}</p>
          {reloaded.typed && reloaded.changed.length ? (
            <>
              {reloaded.untouched ? <Button type="button" className="btn g" onClick={keepMine}>Folosește varianta din formular</Button> : null}
              <details>
                <Summary>Vezi varianta din formular</Summary>
                <dl>{reloaded.theirs.map(([label, text]) => <div key={label}><dt>{label}</dt><dd>{text}</dd></div>)}</dl>
              </details>
            </>
          ) : null}
        </div>
      ) : null}
      {notIssued ? (
        <p className={noteLeftStyle} role="alert">Factura nu a fost emisă: s-a modificat în timp ce se salva. Verifică totalurile, apoi emite din nou factura.</p>
      ) : null}

      {id && !dirty ? (
        <Actions>
          <a className={`${buttonStyle} ${buttonTone.secondary}`} href={draftPreview(s.co, id)} target="_blank" rel="noopener noreferrer">Previzualizare PDF</a>
          <Button type="button" className="btn g" disabled={busy}
            onClick={async () => refused(await send("draft-delete", { id, version }, { text: "Ștergi ciorna?", yes: "Șterge ciorna", danger: true },
              () => ({ co: s.co, tab: "vanzari" })))}>
            Șterge ciorna
          </Button>
          <Button type="button" className="btn g" aria-expanded={repeat} aria-controls="repeat" onClick={() => setRepeat(!repeat)}>
            Repetă lunar
          </Button>
        </Actions>
      ) : null}
      {repeatDraft ? <RepeatMonthly draft={repeatDraft} onDone={() => setRepeat(false)} /> : null}
      {!inv.seller ? null : <Button type="button" id="inv-edit-seller" variant="quiet" onClick={onEditSeller}>Modifică datele firmei de pe factură</Button>}
    </Form>
  );
}

/** A decimal as the engine reads it ("12,5", "3.25"), scaled to an integer: null for anything else. */
function scaled(v: string): { n: bigint; scale: number } | null {
  const m = v.trim().match(/^([+-]?)(\d*)(?:[.,](\d*))?$/);
  if (!m || !(m[2] + (m[3] ?? ""))) return null;
  const fraction = m[3] ?? "";
  const n = BigInt((m[2] || "0") + fraction);
  return { n: m[1] === "-" ? -n : n, scale: fraction.length };
}

/** n / 10^scale rounded half away from zero to cents, as the engine's ROUND_HALF_UP does. */
function toCents(n: bigint, scale: number): bigint {
  if (scale <= 2) return n * 10n ** BigInt(2 - scale);
  const d = 10n ** BigInt(scale - 2);
  const q = (n < 0n ? -n : n) * 2n + d;
  const r = q / (2n * d);
  return n < 0n ? -r : r;
}

type LiveTotals = { net: string; total: string; byVat: { category: string; rate: string; vat: string }[] };

/** The totals of the lines on screen, in exact decimals, for display only: each line's net rounded to cents, VAT per
 * category and rate on the sum of its nets. Null when a line can't be read, or its price includes VAT (the engine's own
 * rounding then applies): the engine computes the invoice's totals again when it saves. */
function liveTotals(lines: InvoiceLine[]): LiveTotals | null {
  const groups = new Map<string, { category: string; rate: string; net: bigint }>();
  for (const l of lines) {
    const q = scaled(l.quantity || "0"), p = scaled(l.unit_price || "0"), r = scaled(l.vat_rate || "0");
    if (!q || !p || !r || l.gross_unit_price !== undefined) return null;
    while (r.scale > 0 && r.n % 10n === 0n) [r.n, r.scale] = [r.n / 10n, r.scale - 1]; // "21.00" is "21"
    const key = `${l.vat_category} ${r.n}e-${r.scale}`;
    const g = groups.get(key) ?? { category: l.vat_category, rate: l.vat_rate, net: 0n };
    g.net += toCents(q.n * p.n, q.scale + p.scale);
    groups.set(key, g);
  }
  let net = 0n, vat = 0n;
  const byVat = [...groups.values()].map((g) => {
    const r = scaled(g.rate)!;
    const v = toCents(g.net * r.n, r.scale + 4); // net in cents × rate %, back to cents
    net += g.net;
    vat += v;
    return { category: g.category, rate: g.rate, vat: fromCents(v) };
  });
  return { net: fromCents(net), total: fromCents(net + vat), byVat };
}

/** From 1100px, the invoice as it is being written (Contra's create screen): who issues it and to whom, its series, its
 * due date and its lines as typed, around the totals. Only a picture of the form; the engine numbers and renders the PDF. */
function Paper({ inv, series, clientId, adding, dueDate, lines, children }: {
  inv: Invoicing; series: string; clientId: string; adding: boolean; dueDate: string; lines: InvoiceLine[]; children: ReactNode;
}) {
  // While a new client is being added, the invoice goes to that one, not to the one picked before.
  const client = adding ? "Client nou" : inv.clients.find((c) => c.id === clientId)?.name;
  const unit = (code: string) => inv.units.find((u) => u.code === code)?.label ?? code;
  const filled = lines.filter((l) => l.description || l.unit_price);
  return (
    <div className="paper min-w-0 min-[1100px]:mb-4 min-[1100px]:rounded-card min-[1100px]:border min-[1100px]:border-solid min-[1100px]:border-line min-[1100px]:bg-surface min-[1100px]:p-8">
      <div className="hidden min-[1100px]:block" aria-hidden="true">
        <p className="m-0 mb-4 text-[15px] font-medium">Factură {series ? `seria ${series}` : ""}</p>
        <dl className="m-0 mb-6 grid grid-cols-2 gap-4 text-[13px] [&_dt]:text-muted [&_dd]:m-0 [&_dd]:wrap-anywhere">
          <div><dt>Furnizor</dt><dd>{inv.seller?.legal_name ?? inv.seller_prefill.legal_name ?? ""}</dd></div>
          <div><dt>Client</dt><dd>{client ?? "Alege clientul"}</dd></div>
          {dueDate ? <div><dt>Scadență</dt><dd>{dm(dueDate, inv.today)}</dd></div> : null}
        </dl>
        {filled.length ? <ul className="m-0 mb-4 list-none divide-x-0 divide-y divide-solid divide-line/60 border-0 border-y border-solid border-line p-0 text-[13px]">
          {filled.map((l, i) => <li key={i} className="flex justify-between gap-4 py-2.5"><span className="min-w-0 wrap-anywhere">{l.description || "Fără descriere"}</span>
            <span className="shrink-0 text-muted tabular-nums">{l.quantity} {unit(l.unit)} × {l.unit_price}</span></li>)}
        </ul> : null}
      </div>
      {children}
    </div>
  );
}

/** The invoice's totals as the lines on screen make them; with the saved draft (`draft`, when the form matches it) the
 * engine's own totals, its RON conversion and what it says about the invoice. Each VAT group by the engine's label. */
function TotalsCard({ inv, draft, lines, currency }: { inv: Invoicing; draft?: Draft; lines: InvoiceLine[]; currency: string }) {
  const live = liveTotals(lines);
  const shown = draft ? { net: draft.totals.net, total: draft.totals.total, byVat: draft.totals.by_vat } : live;
  const cur = curOf(draft?.currency ?? currency);
  const group = (category: string, rate: string) => {
    const c = inv.vat.choices.find((x) => x.category === category && Number(x.rate) === Number(rate));
    return c ? c.label : `TVA ${Number(rate)}%`;
  };
  return (
    <>
      {shown ? (
      <Panel className="mb-4 min-[1100px]:in-[.paper]:mb-0 min-[1100px]:in-[.paper]:border-0 min-[1100px]:in-[.paper]:bg-transparent min-[1100px]:in-[.paper]:p-0! min-[1100px]:in-[.paper]:[&_dd]:text-right" role="group" aria-label="Totaluri">
        <dl className={`${detailListStyle} py-2`}><dt>Total fără TVA</dt><dd>{lei(shown.net)} {cur}</dd></dl>
        {shown.byVat.map((v) => (
          <dl className={`${detailListStyle} py-2`} key={`${v.category}${v.rate}`}><dt>{group(v.category, v.rate)}</dt><dd>{lei(v.vat)} {cur}</dd></dl>
        ))}
        <dl className={`${detailListStyle} mt-3 border-0 border-t border-solid border-line pt-4`}><dt><b>Total</b></dt><dd className="text-lg font-semibold tabular-nums">{lei(shown.total)} {cur}</dd></dl>
        {draft?.totals.total_ron && draft.exchange_rate ? (
          <>
            {draft.totals.vat_ron ? <dl className={`${detailListStyle} py-2`}><dt>TVA în lei</dt><dd>{lei(draft.totals.vat_ron)} lei</dd></dl> : null}
            <dl className={`${detailListStyle} py-2`}><dt>Total în lei</dt><dd>{lei(draft.totals.total_ron)} lei</dd></dl>
            {draft.chargeability_date ? <dl className={`${detailListStyle} py-2`}><dt>TVA exigibil la</dt><dd>{dm(draft.chargeability_date, inv.today)}</dd></dl> : null}
            <dl className={`${detailListStyle} py-2`}><dt>Curs BNR din {dm(draft.exchange_rate.date, inv.today)}</dt>
              <dd>{draft.exchange_rate.rate.replace(".", ",")} lei/{draft.currency}</dd></dl>
          </>
        ) : null}
      </Panel>
      ) : null}
      {draft?.warnings?.length ? draft.warnings.map((w) => <p key={w} className={noteLeftStyle} role="status">{w}</p>) : null}
      {draft?.mentions?.length
        ? <p className={noteLeftStyle}>Pe factură apare și: {draft.mentions.map((m) => m.replace(/\.$/, "")).join("; ")}.</p> : null}
    </>
  );
}

/** Without what ANAF's lookup added that the form doesn't show, so a changed CUI never carries another firm's details. */
const LOOKUP_ONLY = ["reg_com", "phone", "iban", "bank", "account_name", "vat_payer", "efactura_registered", "inactive"];
const LOOKUP_FLAGS = ["vat_payer", "efactura_registered", "inactive"];
/** What the engine identifies a client by (its fiscal identity), and whether it is a private person. */
const IDENTITY = ["country", "cui", "vat_id", "tax_id", "private"] as const;
function unseen<T extends Partial<Client>>(c: T): T {
  return Object.fromEntries(Object.entries(c).filter(([k]) => !LOOKUP_ONLY.includes(k))) as T;
}
/** What the client form sends of the fields it shows: a private person has no codes and nothing ANAF said of a firm, a
 * Romanian client no local foreign tax code (the engine doesn't take one for a Romanian client). */
function shown(c: NewClient): NewClient {
  return c.private ? { ...unseen(c), cui: "", vat_id: "", tax_id: "", ro_vat_status: "unknown" }
    : c.country === "RO" ? { ...c, tax_id: "" } : c;
}
/** A client's fiscal identity as one comparable value. */
const who = (c: Partial<Client>): string => JSON.stringify(IDENTITY.map((k) => c[k] ?? ""));

/** Hands the invoice form the save of the new client's form it holds: it gives the client's id, or null when not saved. */
type ClientSave = (save: () => Promise<string | null>) => void;
type NewClient = Omit<Client, "id" | "last_used" | "last_invoice">;
/** What a partner found only on invoices brings to a new client: its name, a Romanian CUI, the address on its invoices. */
const asClient = (p: Partner): Partial<NewClient> => ({ name: p.name, ...(p.cui && /^\d{2,10}$/.test(p.cui) ? { cui: p.cui } : {}),
  ...(p.address ? { address: { ...p.address }, country: p.address.country || p.country || "RO" } : {}) });
/** The client form's control for a field the engine names, where its id isn't `client-<last part of the path>`. */
const CLIENT_FIELD: Record<string, string> = { vat_id: "client-vat", tax_id: "client-tax" };
/** A saved client as the form changes it: without its id and last use, which the engine keeps. */
export const editable = (c: Client): NewClient => ({ ...(Object.fromEntries(Object.entries(c)
  .filter(([k]) => !["id", "last_used", "last_invoice"].includes(k))) as NewClient), address: { ...NO_ADDRESS, ...c.address } });
const blankClient = (): NewClient => ({ name: "", cui: "", vat_id: "", private: false, country: "RO", address: { ...NO_ADDRESS }, email: "" });

/** Choose a known client, or add one; `prefill` opens the new client's form already filled in, `notice` says why the
 * client has to be chosen again. */
function ClientPicker({ inv, set, value, onChange, onSaved, onEdit, prefill, notice, lacking, adding, setAdding, bind }: {
  inv: Invoicing; set: (d: Invoicing) => void; value: string; onChange: (v: string) => void;
  /** The new client, saved by the invoice's own press: chosen as it is, the invoice keeps what is on screen. */
  onSaved: (v: string) => void; prefill?: Partial<NewClient>;
  notice?: string; lacking: Lacking; adding: boolean; setAdding: (on: boolean) => void; bind: ClientSave; onEdit: () => void;
}) {
  // Opening the new client's form removes its button, and Renunță removes the form: focus follows to what replaced them.
  const box = useRef<HTMLFieldSetElement>(null);
  const switched = useRef(false);
  const toggle = (on: boolean) => {
    switched.current = true;
    setAdding(on);
  };
  useEffect(() => {
    if (!switched.current) return;
    switched.current = false;
    const to = adding ? box.current?.querySelector(":scope > .sub")?.querySelector<HTMLElement>("input, [role=combobox]")
      : document.getElementById("inv-client-add") ?? document.getElementById("inv-client");
    to?.focus();
  }, [adding]);
  const added = (id: string) => {
    onSaved(id);
    setAdding(false);
    setTimeout(() => document.getElementById("inv-client")?.focus());
  };
  // Without a list, the new client's form stands for the field: the message and the focus go to it.
  const listed = inv.clients.length > 0;
  return (
    <fieldset className="m-0" ref={box} data-field="inv-client"
      aria-describedby={!listed && lacking.has("inv-client") ? "inv-client-err" : undefined}>
      <legend>Client</legend>
      {listed ? (
        <Select id="inv-client" aria-label="Client" value={value} onChange={(e) => onChange(e.target.value)} aria-required
          {...marked(lacking, "inv-client", notice ? "inv-client-gone" : undefined)}>
          <option value="">Alege clientul</option>
          {inv.clients.map((x) => <option key={x.id} value={x.id}>{x.name}{x.cui ? `, CUI ${x.cui}` : x.vat_id ? `, ${x.vat_id}` : ""}</option>)}
        </Select>
      ) : null}
      <Err lacking={lacking} id="inv-client" />
      {notice ? <p id="inv-client-gone" className={noteLeftStyle} role="status">{notice}</p> : null}
      {!adding ? <Button type="button" id="inv-client-add" variant="quiet" onClick={() => toggle(true)}>Adaugă un client nou</Button> : (
        <ClientForm inv={inv} set={set} initial={{ ...blankClient(), ...prefill }} onSaved={added} bind={bind} onEdit={onEdit}
          onCancel={inv.clients.length ? () => toggle(false) : undefined} />
      )}
    </fieldset>
  );
}

/** A client's details: a new one, by CUI from ANAF's public data or by hand (a private person has no CUI), or with `id` a
 * saved one, changed. The engine checks what it accepts; the form only leaves out what it hides. */
export function ClientForm({ inv, set, initial, id, onSaved, onCancel, bind, onEdit }: {
  inv: Invoicing; set: (d: Invoicing) => void; initial: NewClient; id?: string; onSaved: (id: string, r: InvoicingResult) => void; onCancel?: () => void;
  /** Inside the invoice form: the invoice's buttons save the client through this, and the form has no save button. */
  bind?: ClientSave;
  /** Called whenever the client's details differ from what the form opened with. */
  onEdit?: () => void;
}) {
  const s = useCompany();
  const { busy } = useApp();
  const send = useInvoicingRun(s.co, set);
  const [c, setC] = useState(initial);
  const countries = useMemo(() => countryNames(inv.countries), [inv.countries]);
  const opened = useRef(c);
  useEffect(() => {
    if (c !== opened.current) onEdit?.();
  }, [c, onEdit]);
  // After a press with fields missing, each one says so until it is filled in; a missing identity is said at the CUI.
  // Required is only what the engine's lists say: none of the ways to identify a client is required alone.
  const need = new Set(clientRules(inv, shown(c)).paths);
  const missing = clientMissing(inv, c).map((p): [string, string] => p === "identity"
    ? ["client-cui", "Completează CUI-ul, codul de TVA sau codul fiscal local ori bifează „Persoană fizică”."]
    : [CLIENT_FIELD[p] ?? `client-${p.split(".").pop()}`, FILL]);
  const { tried, unshown, complete } = useRefusal(missing.map(([k]) => k));
  const lacking: Lacking = new Map(tried ? [...missing].reverse() : []); // the identity's message wins at the CUI
  // The identity ANAF's lookup described: what it added is kept only while the client is saved under exactly that identity.
  const [looked, setLooked] = useState<string | null>(null);
  // ANAF is asked once the CUI is written (on leaving its field), once per CUI.
  const lookedUp = useRef(initial.cui.trim());
  // A press that comes with leaving the CUI waits for its lookup, then saves what the lookup filled in.
  const pending = useRef<Promise<unknown> | null>(null);
  const latest = useRef<() => Promise<string | null>>(async () => null);
  const cuiNow = useRef(initial.cui);
  const [saving, setSaving] = useState(false);
  // A read beside the app's one-at-a-time actions, so the buttons stay pressable while it runs. When ANAF can't answer,
  // nothing is filled in and the fields stay as typed.
  const lookup = async () => {
    const asked = c;
    const r = await postInvoicing("client-lookup", { cui: asked.cui, co: s.co }).catch(() => null);
    // An answer about a CUI no longer in the field is about another firm: it fills in nothing.
    if (!r?.ok) lookedUp.current = ""; // a lookup that failed is asked again on leaving the field
    if (r?.ok && r.client && cuiNow.current === asked.cui) {
      // An address line ANAF leaves empty doesn't erase the one typed; its codes always replace the former firm's.
      const address = Object.fromEntries(Object.entries(r.client.address ?? {}).filter(([, v]) => v !== ""));
      setC((x) => ({ ...x, ...r.client, address: { ...x.address, ...address } }));
      const found = { ...asked, ...r.client };
      setLooked(found.cui ? who(found) : null);
    }
  };
  const save = async (): Promise<string | null> => {
    if (pending.current) {
      await pending.current;
      pending.current = null;
      await new Promise((ok) => setTimeout(ok)); // the form as the lookup left it
      return latest.current();
    }
    if (!complete() || c.inactive) return null;
    // A private person has no CUI or VAT id, and nothing ANAF said about a firm goes with them.
    // What the form hides doesn't go: a private person has no codes, a Romanian client no local foreign tax code.
    // The Romanian VAT registration only exists for a foreign firm; a hidden "not registered" must never skip SPV.
    // A saved client keeps its status while its identity stays; a new identity starts over, like a new client.
    const same = !!id && IDENTITY.every((k) => (c[k] ?? "") === (initial[k] ?? ""));
    const own = same ? { ...initial, ...c } : looked !== null && looked === who(c) ? c : unseen(c);
    const client = c.private ? shown(c)
      : c.country === "RO" ? { ...own, tax_id: "", ro_vat_status: same ? initial.ro_vat_status ?? "unknown" : "unknown" } : own;
    // The engine merges a saved client's stored data under what is sent: what ANAF said of the former identity is cleared.
    const cleared = id ? Object.fromEntries(LOOKUP_ONLY.filter((k) => k in initial && !(k in client))
      .map((k) => [k, LOOKUP_FLAGS.includes(k) ? false : ""])) : {};
    setSaving(true); // what is sent is what stays on screen: the fields wait for the answer
    const r = await send("client-save", { ...(id ? { id } : {}), client: { ...cleared, ...client } }, undefined, () => false)
      .finally(() => setSaving(false));
    if (!r?.ok || !r.id) return null;
    onSaved(r.id, r);
    return r.id;
  };
  useLayoutEffect(() => {
    latest.current = save;
    cuiNow.current = c.cui;
    bind?.(save);
  });
  const addr = (k: keyof Address, label: string, opt = false) => (
    <Field id={`client-${k}`} label={label} opt={opt} req={need.has(`address.${k}`)} lacking={lacking} value={c.address[k]} onChange={(v) => setC({ ...c, address: { ...c.address, [k]: v } })} />
  );
  return (
    <div className="sub my-4 rounded-control bg-inset p-4">
      <fieldset className="m-0 min-w-0 border-0 p-0" disabled={saving} aria-busy={saving || undefined}>
      <Label className="min-h-11 cursor-pointer"><Input id="client-private" type="checkbox" checked={c.private} onChange={(e) => setC({ ...c, private: e.target.checked })} /> Persoană fizică</Label>
      {!c.private ? (
        <div className="flex flex-wrap items-end gap-3 [&>div]:min-w-0 [&>div]:flex-1">
          <Field id="client-cui" label="CUI" req={need.has("cui")} lacking={lacking} value={c.cui} onChange={(v) => setC({ ...unseen(c), cui: v })}
            onBlur={() => {
              const cui = c.cui.trim();
              if (busy || cui === lookedUp.current || !/^(RO)?\s*\d{2,10}$/i.test(cui)) return;
              lookedUp.current = cui;
              pending.current = lookup();
            }} />
        </div>
      ) : null}
      {!c.private ? (
        <Field id="client-vat" label={c.country === "RO" ? "Cod de TVA (RO…)" : "Cod de TVA (de exemplu DE123456789)"} opt={c.country === "RO" && !c.vat_payer}
          req={need.has("vat_id")} lacking={lacking} value={c.vat_id} onChange={(v) => setC({ ...c, vat_id: v.toUpperCase().replace(/\s+/g, "") })} />
      ) : null}
      {!c.private && c.country !== "RO" ? (
        <Field id="client-tax" label="Cod fiscal local (de exemplu CHE-123.456.789)" opt req={need.has("tax_id")} lacking={lacking}
          value={c.tax_id ?? ""} onChange={(v) => setC({ ...c, tax_id: v })} />
      ) : null}
      {!c.private && c.country !== "RO" ? (
        <>
          <Label htmlFor="client-rovat">Înregistrare în scopuri de TVA în România</Label>
          <Select id="client-rovat" value={c.ro_vat_status ?? "unknown"}
            onChange={(e) => setC({ ...c, ro_vat_status: e.target.value as NonNullable<Client["ro_vat_status"]> })}>
            <option value="unknown">Necunoscută</option>
            <option value="registered">Înregistrat în scopuri de TVA în România</option>
            <option value="not_registered">Nestabilit și neînregistrat în scopuri de TVA în România</option>
          </Select>
          <p className={noteLeftStyle}>Facturile se trimit în SPV, cu excepția celor către un client declarat nestabilit și neînregistrat în România.</p>
        </>
      ) : null}
      <Field id="client-name" label="Nume" req={need.has("name")} lacking={lacking} value={c.name} onChange={(v) => setC({ ...c, name: v })} />
      {addr("street", "Adresă")}
      {bySector(c.address) ? null : addr("city", "Localitate")}
      {c.country !== "RO" ? addr("county", "Regiune", true) : <CountyField inv={inv} id="client-county" req={need.has("address.county")}
        lacking={lacking} address={c.address} onChange={(address) => setC({ ...c, address })} />}
      <Label htmlFor="client-country">Țara</Label>
      <Select id="client-country" value={c.country} aria-required={need.has("country") || undefined} {...marked(lacking, "client-country")}
        onChange={(e) => setC({ ...c, country: e.target.value, address: { ...c.address, country: e.target.value } })}>
        {countries.some(([code]) => code === c.country) ? null : <option value={c.country}>{c.country || "Alege țara"}</option>}
        {countries.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
      </Select>
      <Err lacking={lacking} id="client-country" />
      <Field id="client-email" label="E-mail" opt req={need.has("email")} value={c.email} onChange={(v) => setC({ ...c, email: v })} />
      {c.inactive ? <p id="client-inactive" className={noteLeftStyle} role="alert">Firma figurează ca inactivă la ANAF și nu poate fi adăugată ca client.</p> : null}
      <Actions>
        {/* Enabled with fields missing: a press says which, by each of them, and goes to the first. The engine checks again. */}
        {bind ? null : <Button type="button" className="btn" disabled={busy || !!c.inactive} onClick={save}
          aria-describedby={[c.inactive ? "client-inactive" : "", unshown ? "client-unshown" : ""].filter(Boolean).join(" ") || undefined}>
          Salvează clientul</Button>}
        {onCancel ? <Button type="button" className="btn g" onClick={onCancel}>Renunță</Button> : null}
      </Actions>
      <Unshown id="client-unshown" on={unshown} />
      </fieldset>
    </div>
  );
}

/** A text field. `req`: the engine requires it (the party forms pass what its lists say; otherwise required unless `opt`,
 * which its label says). `auto` is its autofill token, for
 * the company's own details only ("off" for another party's, which the browser would fill with the user's). */
const NO_ACCOUNT = "Adaugă întâi contul firmei în Tranzacții.";

/** The company's account a saved IBAN names, whatever its spacing or case (older profiles were saved as typed). */
const accountOf = (inv: Invoicing, iban: string) => {
  const n = iban.replace(/\s+/g, "").toUpperCase();
  return inv.accounts.find((a) => a.iban === n);
};

/** An account as a choice: its currency, bank and the end of its IBAN. */
const accountLabel = (a: { currency: string; bank_name: string; iban: string }) =>
  [a.currency, a.bank_name || "Altă bancă", `cont ${a.iban.slice(-4)}`].join(", ");

/** The account the invoice asks to be paid into: one of the company's accounts (the engine refuses any other IBAN). */
function AccountPick({ inv, co, iban, need, lacking, onPick }: {
  inv: Invoicing; co: string; iban: string; need: boolean; lacking: Lacking; onPick: (a: InvoiceAccount | null) => void;
}) {
  if (!inv.accounts.length) {
    return (
      <div data-field="seller-iban">
        <p className={noteLeftStyle} role="note">
          Firma nu are încă un cont bancar. Contul se adaugă în <a href={`?co=${co}&tab=tranzactii`}
            {...marked(lacking, "seller-iban")}>Tranzacții</a>, apoi se alege aici pentru facturi.
        </p>
        <Err lacking={lacking} id="seller-iban" />
      </div>
    );
  }
  const mark = marked(lacking, "seller-iban");
  const known = inv.accounts.some((a) => a.iban === iban);
  return (
    <>
      <Label htmlFor="seller-iban">Contul pe factură</Label>
      <Select id="seller-iban" value={known ? iban : ""} aria-required={need || undefined} {...mark}
        onChange={(e) => onPick(inv.accounts.find((a) => a.iban === e.target.value) ?? null)}>
        {!known ? <option value="">Alege contul</option> : null}
        {inv.accounts.map((a) => (
          <option key={a.iban} value={a.iban}>
            {accountLabel(a)}
          </option>
        ))}
      </Select>
      <Err lacking={lacking} id="seller-iban" />
    </>
  );
}

/** The engine's countries by their Romanian name, Romania first: the code is what is sent. Kosovo's code (1A) is not a
 * region the browser names. */
function countryNames(codes: string[]): [string, string][] {
  const name = (code: string) => {
    try {
      return new Intl.DisplayNames(["ro"], { type: "region" }).of(code === "1A" ? "XK" : code) ?? code;
    } catch {
      return code;
    }
  };
  const rest = codes.filter((code) => code !== "RO").map((code): [string, string] => [code, name(code)])
    .sort((a, b) => a[1].localeCompare(b[1], "ro"));
  return [["RO", name("RO")], ...rest];
}

/** A Bucharest address's sector, which the engine takes as its city ("SECTOR1"); undefined elsewhere. */
const sectorOf = (a: Address) => a.city.match(/^sector\s*([1-6])$/i)?.[1];
const bySector = (a: Address) => a.county === "RO-B" && !!sectorOf(a);

/** A Romanian address's county, picked by name from the engine's list, Bucharest by sector: the engine's code is sent. */
function CountyField({ inv, id, address, onChange, req, lacking }: {
  inv: Invoicing; id: string; address: Address; onChange: (a: Address) => void; req: boolean; lacking: Lacking;
}) {
  const at = inv.counties.findIndex((c) => c.county === address.county && (!c.city || c.city === `SECTOR${sectorOf(address)}`));
  return (
    <div className="mt-4 min-w-0 first:mt-0">
      <Label htmlFor={id}>Județ</Label>
      <Select id={id} value={at < 0 ? "" : String(at)} aria-required={req || undefined} {...marked(lacking, id)} onChange={(e) => {
        const c = inv.counties[Number(e.target.value)];
        if (c) onChange({ ...address, county: c.county, city: c.city ?? (sectorOf(address) ? "" : address.city) });
      }}>
        {at < 0 ? <option value="">Alege județul</option> : null}
        {inv.counties.map((c, i) => <option key={c.label} value={i}>{c.label}</option>)}
      </Select>
      <Err lacking={lacking} id={id} />
    </div>
  );
}

function Field({ id, label, value, onChange, onBlur, opt = false, req = !opt, numeric = false, auto = "off", lacking }: {
  id: string; label: string; value: string; onChange: (v: string) => void; onBlur?: () => void; opt?: boolean; req?: boolean;
  numeric?: boolean; auto?: string; lacking?: Lacking;
}) {
  const none: Lacking = new Map();
  return (
    <div className="mt-4 min-w-0 first:mt-0">
      <Label htmlFor={id}>{label}{opt ? " (opțional)" : ""}</Label>
      <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} onBlur={onBlur} inputMode={numeric ? "numeric" : undefined}
        autoComplete={auto} spellCheck={false} aria-required={req || undefined} {...marked(lacking ?? none, id)} />
      <Err lacking={lacking ?? none} id={id} />
    </div>
  );
}

/** Vânzări, above the list: the drafts waiting to be issued. */
export function DraftList() {
  const allowed = useCan("member");
  const s = useCompany();
  const { data } = useInvoicing(s.co);
  const drafts = data?.drafts ?? [];
  if (!allowed || !drafts.length) return null;
  return (
    <section aria-labelledby="drafts-h">
      <div className={sectionHeadingStyle}><h2 id="drafts-h">Ciorne</h2></div>
      <Panel padded={false} className="my-4">
        {drafts.map((d) => (
          <Row key={d.id} icon="file-text" title={d.client.name || "Fără client"} to={{ new: d.id }}
            sub={`Ciornă, seria ${d.series}`} val={`${lei(d.totals.total)} ${curOf(d.currency)}`} vsub={dm(d.updated_at, s.today)} />
        ))}
      </Panel>
    </section>
  );
}
