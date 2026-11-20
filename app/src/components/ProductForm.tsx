"use client";
// A product's form, as its sheet in Firma › Produse and over an invoice whose line adds one in place, and an invoice
// line's product picker (docs/information-architecture.md, Firmă › Produse).
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { postProduct, type ProductAction } from "@/lib/api";
import { curOf, lei } from "@/lib/format";
import { coName, type InvoiceLine, type Invoicing, type Product, type ProductFields, type ProductResult } from "@/lib/types";
import type { Ask } from "./Confirm";
import { Actions, Button, Input, Label, Select, Textarea, noteLeftStyle, selectListStyle, selectOptionStyle } from "./Controls";
import { useApp, useCan, useCompany } from "./context";
import { CloseX, Sheet, SheetHead } from "./Sheets";
import { I } from "./ui";

/** The route of Adaugă produs (`prod=nou`). */
export const NEW_PRODUCT = "nou";
export const KIND = { produs: "Produs", serviciu: "Serviciu" } as const;
export const unitOf = (inv: Pick<Invoicing, "units">, code: string) => inv.units.find((u) => u.code === code)?.label ?? code;
export const vatOf = (inv: Pick<Invoicing, "vat">, p: Pick<Product, "vat_category" | "vat_rate" | "exemption">) =>
  inv.vat.choices.find((c) => c.category === p.vat_category && Number(c.rate) === Number(p.vat_rate) && c.exemption === p.exemption);
/** The VAT as a line names it; one the firm's choices don't offer (saved before it became a VAT payer) by its exemption
 * or rate, so it never reads as none. */
export const vatLabel = (inv: Pick<Invoicing, "vat">, p: Pick<Product, "vat_category" | "vat_rate" | "exemption">) =>
  vatOf(inv, p)?.label ?? (p.exemption || `TVA ${p.vat_rate}%`);
/** "350,00 lei / oră". */
export const priceOf = (inv: Pick<Invoicing, "units">, p: Product) => `${lei(p.unit_price)} ${curOf(p.currency)} / ${unitOf(inv, p.unit)}`;

/** A line filled from a product: its name, unit and VAT, and its price only in the invoice's own currency (never
 * converted); `product_id` links the line, whose values stay its own to edit. */
export function lineFrom(p: Product, line: InvoiceLine, currency: string): InvoiceLine {
  const next: InvoiceLine = { ...line, description: p.name, unit: p.unit, vat_category: p.vat_category, vat_rate: p.vat_rate,
    exemption: p.exemption, product_id: p.id, ...(p.currency === currency ? { unit_price: p.unit_price } : {}) };
  delete next.gross_unit_price;
  return next;
}

/** A blank product, or one started from an invoice line (its name, unit, price and VAT) in the invoice's currency. */
export function productFrom(inv: Invoicing, line?: InvoiceLine, currency = "RON"): ProductFields {
  const c = inv.vat.choices.find((x) => x.key === inv.vat.default) ?? inv.vat.choices[0];
  return { kind: "produs", name: line?.description.trim() ?? "", code: "", unit: line?.unit || inv.units[0]?.code || "H87",
    unit_price: line?.unit_price.trim() ?? "", currency, vat_category: line?.vat_category ?? c.category, vat_rate: line?.vat_rate ?? c.rate,
    exemption: line?.exemption ?? c.exemption, description: "" };
}

const FIELDS: (keyof ProductFields)[] = ["kind", "name", "code", "unit", "unit_price", "currency", "vat_category", "vat_rate", "exemption", "description"];
const fieldsOf = (p: ProductFields): ProductFields => Object.fromEntries(FIELDS.map((k) => [k, p[k]])) as ProductFields;

/** "220" or "99,9" as the engine takes a price, "220.00" and "99.90"; null when it isn't one. */
export function price(typed: string): string | null {
  const m = /^(\d+)(?:[.,](\d{1,2}))?$/.exec(typed.trim());
  return m ? `${m[1]}.${(m[2] ?? "").padEnd(2, "0")}` : null;
}

/** A product's sheet, Adaugă produs (`product` absent), or the same form over an invoice (`start`, `onSaved`): the saved
 * product goes to `onSaved` and the sheet under it stays open. */
export function ProductSheet({ inv, product, start, missing = false, onClose, onSaved }: {
  inv: Invoicing; product?: Product; start?: ProductFields; missing?: boolean; onClose: () => void; onSaved?: (p: Product) => void;
}) {
  const s = useCompany(); const { run, busy } = useApp();
  const canWrite = useCan("member");
  const [form, setForm] = useState<ProductFields>(() => product ? fieldsOf(product) : start ?? productFrom(inv));
  const [tried, setTried] = useState(false);
  const title = missing ? "Produs" : product ? product.name : "Produs nou";
  const set = (patch: Partial<ProductFields>) => setForm((f) => ({ ...f, ...patch }));
  const lacking = { name: !form.name.trim(), unit_price: price(form.unit_price) === null };
  // An answer that arrives after this sheet closed changes nothing under it.
  const open = useRef(true);
  useEffect(() => {
    open.current = true; // again after a development remount (Strict Mode)
    return () => { open.current = false; };
  }, []);
  const send: Send = (action, body, ask) => run(s.co, () => postProduct(action, { co: s.co, ...body }), ask, (r) => {
    // over an invoice only this sheet closes, through onSaved; on Produse the route closes it
    if (onSaved && open.current && r.done?.product) onSaved(r.done.product);
    return onSaved ? false : undefined;
  });
  const save = async () => {
    setTried(true);
    if (lacking.name || lacking.unit_price) {
      document.getElementById(lacking.name ? "prod-name" : "prod-price")?.focus();
      return;
    }
    const fields = { ...form, name: form.name.trim(), code: form.code.trim(), description: form.description.trim(), unit_price: price(form.unit_price)! };
    await send(product ? "product-update" : "product-add", product ? { id: product.id, version: product.version, ...fields } : fields);
  };
  return (
    <Sheet onClose={onClose} label={title} busy={busy}>
      <CloseX onClose={onClose} />
      <SheetHead title={title} sub={<span className="font-normal text-muted">{coName(s)}</span>} />
      {missing ? <p className={noteLeftStyle}>Produsul nu mai există în catalogul firmei.</p> : (
        <form className="my-5" noValidate onSubmit={(e) => { e.preventDefault(); void save(); }}>
          {/* nothing changes while a save is on its way: what is sent is what the sheet closes on */}
          <fieldset className="m-0 border-0 p-0" disabled={!canWrite || busy}>
            <ProductInputs inv={inv} form={form} set={set} tried={tried} lacking={lacking} />
          </fieldset>
          {canWrite ? <ProductActions product={product} busy={busy} send={send} onClose={onClose} /> : null}
        </form>
      )}
    </Sheet>
  );
}

function ProductInputs({ inv, form, set, tried, lacking }: {
  inv: Invoicing; form: ProductFields; set: (p: Partial<ProductFields>) => void; tried: boolean; lacking: { name: boolean; unit_price: boolean };
}) {
  const vat = vatOf(inv, form);
  const err = (id: string, on: boolean, text: string) => tried && on ? <p id={`${id}-err`} role="alert" className={noteLeftStyle}>{text}</p> : null;
  const req = (id: string, on: boolean) => ({ "aria-required": true, "aria-invalid": tried && on || undefined, "aria-describedby": tried && on ? `${id}-err` : undefined });
  return <>
    <Label htmlFor="prod-kind">Tip</Label>
    <Select id="prod-kind" value={form.kind} onChange={(e) => set({ kind: e.target.value as ProductFields["kind"] })}>
      <option value="produs">Produs</option><option value="serviciu">Serviciu</option>
    </Select>
    <Label htmlFor="prod-name">Denumire</Label>
    <Input id="prod-name" data-autofocus value={form.name} maxLength={inv.line_description_max} autoComplete="off" {...req("prod-name", lacking.name)}
      onChange={(e) => set({ name: e.target.value })} />
    {err("prod-name", lacking.name, "Completează denumirea.")}
    <div className="mt-4 grid grid-cols-2 gap-x-3 gap-y-4">
      <div><Label htmlFor="prod-code">Cod (opțional)</Label>
        <Input id="prod-code" value={form.code} maxLength={64} autoComplete="off" onChange={(e) => set({ code: e.target.value })} /></div>
      <div><Label htmlFor="prod-unit">UM</Label>
        <Select id="prod-unit" value={form.unit} onChange={(e) => set({ unit: e.target.value })}>
          {inv.units.map((u) => <option key={u.code} value={u.code}>{u.label}</option>)}
        </Select></div>
      <div><Label htmlFor="prod-price">Preț unitar</Label>
        <Input id="prod-price" inputMode="decimal" value={form.unit_price} autoComplete="off" {...req("prod-price", lacking.unit_price)}
          onChange={(e) => set({ unit_price: e.target.value })} />
        {err("prod-price", lacking.unit_price, "Scrie prețul, de exemplu 120,50.")}</div>
      <div><Label htmlFor="prod-currency">Monedă</Label>
        <Select id="prod-currency" value={form.currency} onChange={(e) => set({ currency: e.target.value })}>
          <option value="RON">Lei (RON)</option><option value="EUR">Euro (EUR)</option>
          {form.currency !== "RON" && form.currency !== "EUR" ? <option value={form.currency}>{form.currency}</option> : null}
        </Select></div>
    </div>
    <Label htmlFor="prod-vat">TVA</Label>
    <Select id="prod-vat" value={vat?.key ?? ""} onChange={(e) => {
      const o = inv.vat.choices.find((x) => x.key === e.target.value);
      if (o) set({ vat_category: o.category, vat_rate: o.rate, exemption: o.exemption });
    }}>
      {!vat ? <option value="">Alege TVA-ul</option> : null}
      {inv.vat.choices.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
    </Select>
    <Label htmlFor="prod-description">Descriere (opțional)</Label>
    <Textarea id="prod-description" value={form.description} maxLength={500} onChange={(e) => set({ description: e.target.value })} />
  </>;
}

type Send = (action: ProductAction, body: Record<string, unknown>, ask?: Ask) => Promise<ProductResult | null>;

/** Salvează; for a saved product also Arhivează or Restaurează, and Șterge while no invoice line uses it. */
function ProductActions({ product, busy, send, onClose }: { product?: Product; busy: boolean; send: Send; onClose: () => void }) {
  const guard = product ? { id: product.id, version: product.version } : {};
  return <Actions className="btns mt-6">
    <Button type="submit" variant="primary" disabled={busy}>{product ? "Salvează" : "Adaugă produsul"}</Button>
    {!product ? <Button type="button" variant="quiet" onClick={onClose}>Anulează</Button>
      : <Button type="button" variant="secondary" disabled={busy} onClick={() => void send("product-archive", { ...guard, archived: !product.archived })}>
        {product.archived ? "Restaurează" : "Arhivează"}</Button>}
    {product?.can_delete ? <Button type="button" variant="danger" disabled={busy}
      onClick={() => void send("product-delete", guard, { text: `Ștergi produsul ${product.name}?`, yes: "Șterge produsul", danger: true })}>Șterge</Button> : null}
  </Actions>;
}

const fold = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
/** The products whose name or code holds what is typed, all of them for an empty field. */
export const matching = (products: Product[], typed: string) => {
  const q = fold(typed.trim());
  return q ? products.filter((p) => fold(`${p.name} ${p.code}`).includes(q)) : products;
};

/** An invoice line's name as WAI-ARIA's editable combobox with a list: typing writes the line's name and searches the
 * firm's products; the caret or a click opens the whole list. Each product fills the line (`onPick`); the last option,
 * always there, is Produs nou (`onNew`). Arrows move, Enter picks, Escape closes only the list, Tab leaves. */
export function ProductField({ id, value, linked, products, units, maxLength, onType, onPick, onNew, ...field }: {
  id: string; value: string; linked: boolean; products: Product[]; units: Invoicing["units"]; maxLength: number; onType: (text: string) => void;
  onPick: (p: Product) => void; onNew: () => void; "aria-required"?: boolean; "aria-invalid"?: boolean; "aria-describedby"?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [typed, setTyped] = useState(false); // after typing, the list shows what matches; opened otherwise, every product
  const shown = typed ? matching(products, value) : products;
  const count = shown.length + 1; // the products, then Produs nou
  const list = `${id}-list`, option = (i: number) => `${id}-o${i}`;
  // the active option stays in sight as arrows move through a list longer than its box
  useEffect(() => { if (open && active >= 0) document.getElementById(option(active))?.scrollIntoView?.({ block: "nearest" }); });
  const show = (all: boolean) => { setTyped(!all); setActive(-1); setOpen(true); };
  const choose = (i: number) => {
    setOpen(false);
    if (i < shown.length) onPick(shown[i]); else onNew();
  };
  const key = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) { show(true); setActive(e.key === "ArrowDown" ? 0 : products.length); return; } // the whole list, Produs nou last
      setActive((a) => (e.key === "ArrowDown" ? (a + 1) % count : (a <= 0 ? count : a) - 1));
    } else if (e.key === "Enter" && open && active >= 0) {
      e.preventDefault();
      choose(active);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      e.stopPropagation(); // the list, not the sheet
      setOpen(false);
    }
  };
  return <>
    <div className="relative min-w-0" onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false); }}>
      <Input id={id} {...field} role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={list}
        aria-activedescendant={open && active >= 0 ? option(active) : undefined} value={value} maxLength={maxLength}
        autoComplete="off" placeholder="Alege din produse sau scrie" className="pr-12"
        onChange={(e) => { onType(e.target.value); show(false); }} onClick={() => !open && show(true)} onKeyDown={key} />
      <button type="button" tabIndex={-1} aria-label="Produse" aria-controls={list} aria-expanded={open}
        className="absolute top-0 right-0 grid h-full w-11 cursor-pointer place-items-center border-0 bg-transparent text-muted [&_svg]:size-3.5"
        onMouseDown={(e) => e.preventDefault()} onClick={() => { if (open) setOpen(false); else { document.getElementById(id)?.focus(); show(true); } }}>
        <I n="caret-down" />
      </button>
      {open ? (
        <ul id={list} role="listbox" aria-label="Produse" className={`${selectListStyle} absolute! top-full right-0 left-0 mt-1`}>
          {shown.map((p, i) => (
            <li key={p.id} id={option(i)} role="option" aria-selected={i === active} data-active={i === active ? "" : undefined}
              className={selectOptionStyle} onMouseDown={(e) => e.preventDefault()} onMouseMove={() => setActive(i)} onClick={() => choose(i)}>
              <span className="min-w-0 flex-1 truncate">{p.name}</span>
              <small className="shrink-0 text-xs text-muted tabular-nums">{priceOf({ units }, p)}</small>
            </li>
          ))}
          <li id={option(shown.length)} role="option" aria-selected={active === shown.length} data-active={active === shown.length ? "" : undefined}
            className={`${selectOptionStyle} font-medium ${shown.length ? "mt-1 border-0 border-t border-solid border-line" : ""} [&>svg]:ml-0!`}
            onMouseDown={(e) => e.preventDefault()} onMouseMove={() => setActive(shown.length)} onClick={() => choose(shown.length)}>
            <I n="plus" /><span>Produs nou</span>
          </li>
        </ul>
      ) : null}
    </div>
    {linked ? <p className={`${noteLeftStyle} mb-0 inline-flex items-center gap-1.5 [&_svg]:size-3.5`}><I n="package" />Din Produse</p> : null}
  </>;
}
