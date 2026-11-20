"use client";
import { useId, useState } from "react";
import { cents, curOf, decimal } from "@/lib/format";
import type { LineTotals, SheetLine } from "@/lib/types";
import { Button, dataTableStyle, numCellStyle, surfaceStyle } from "./Controls";
import { useDetail } from "./DocumentDetail";

const FIRST = 5;
const note = "mx-0 mt-3 mb-0 text-[13px] leading-relaxed text-muted";
const dash = "-";
const rate = (r: string | null) => r === null ? dash : `${decimal(r)}%`;
const money = (a: string | null) => a === null ? dash : decimal(a, 2);

/** What the invoice lists, read-only, from the sheet's detail, with the document's own totals under it. A table where
 * the section is wide enough for its seven columns; narrower, each line takes two rows and the totals stay a table.
 * Nothing when the document can't be read. */
export function InvoiceLines() {
  const lines = useDetail().lines;
  const [all, setAll] = useState(false);
  const title = useId();
  if (!lines || lines.unavailable) return null;
  const cur = curOf(lines.currency);
  const long = lines.lines.length > FIRST + 1;
  const shown = all || !long ? lines.lines : lines.lines.slice(0, FIRST);
  return <section aria-labelledby={title} className={`@container my-4 p-5 sm:p-6 ${surfaceStyle}`}>
    <div className="mb-2 flex items-baseline justify-between gap-3">
      <h3 id={title} className="m-0 text-base font-semibold">Produse și servicii</h3>
      {lines.lines.length ? <span className="text-[13px] text-muted">Sume în {cur}</span> : null}
    </div>
    <div className="hidden overflow-x-auto @2xl:block">
      <table className={dataTableStyle}>
        <thead><tr>
          <th scope="col">Denumire</th><th scope="col" className={numCellStyle}>Cant.</th><th scope="col">U.M.</th>
          <th scope="col" className={numCellStyle}>Preț unitar</th><th scope="col" className={numCellStyle}>TVA</th>
          <th scope="col" className={numCellStyle}>Valoare fără TVA</th><th scope="col" className={numCellStyle}>Total cu TVA</th>
        </tr></thead>
        <tbody>{shown.map((l) => <Row key={l.position} l={l} />)}</tbody>
      </table>
    </div>
    <ul className="m-0 list-none p-0 @2xl:hidden">{shown.map((l) => <Compact key={l.position} l={l} cur={cur} />)}</ul>
    {long ? <Button type="button" className="btn g mt-3" aria-expanded={all} onClick={() => setAll(!all)}>
      {all ? "Arată mai puține linii" : `Arată toate cele ${lines.lines.length} linii`}</Button> : null}
    {lines.truncated ? <p className={note}>Se afișează primele {lines.lines.length} de linii. Restul sunt în documentul facturii.</p> : null}
    {lines.totals ? <Totals t={lines.totals} cur={cur} /> : null}
  </section>;
}

function Row({ l }: { l: SheetLine }) {
  return <tr>
    <th scope="row" className="min-w-40 font-normal text-ink wrap-anywhere">{l.description}</th>
    <td className={numCellStyle}>{decimal(l.quantity)}</td>
    <td className="whitespace-nowrap">{l.unit}</td>
    <td className={numCellStyle}>{l.unit_price ? decimal(l.unit_price, 2) : dash}</td>
    <td className={numCellStyle}>{rate(l.vat_rate)}</td>
    <td className={numCellStyle}>{decimal(l.net, 2)}</td>
    <td className={`${numCellStyle} font-semibold`}>{money(l.gross)}</td>
  </tr>;
}

function Compact({ l, cur }: { l: SheetLine; cur: string }) {
  // The unit goes with the price ("8 × 75,00 lei/oră"), so a singular unit label reads right for any quantity.
  const unit = l.unit ? `/${l.unit}` : "";
  const amount = l.unit_price ? `${decimal(l.quantity)} × ${decimal(l.unit_price, 2)} ${cur}${unit}` : `Cantitate ${decimal(l.quantity)}${l.unit ? ` (${l.unit})` : ""}`;
  // The total sits beside the description while both fit; a long one moves under it and wraps rather than overflow.
  return <li className="flex flex-wrap items-baseline justify-between gap-x-4 border-0 border-t border-solid border-line py-3 first:border-t-0 first:pt-1">
    <span className="min-w-0 flex-[1_1_10rem] text-sm leading-relaxed text-ink wrap-anywhere">{l.description}</span>
    <span className="ml-auto max-w-full text-right text-sm font-semibold tabular-nums wrap-anywhere">{money(l.gross)}</span>
    <span className="min-w-0 basis-full text-[13px] leading-relaxed text-muted tabular-nums wrap-anywhere">
      {amount}{l.vat_rate ? `, TVA ${rate(l.vat_rate)}` : ""}, fără TVA {decimal(l.net, 2)}
    </span>
  </li>;
}

/** The document's totals as written: VAT per rate when it has several, the amount to pay last. */
function Totals({ t, cur }: { t: LineTotals; cur: string }) {
  const taxes = t.taxes.length > 1 ? t.taxes.map((g) => [`TVA ${rate(g.rate)}`, g.vat]) : [[t.taxes[0]?.rate ? `TVA ${rate(t.taxes[0].rate)}` : "TVA", t.vat]];
  const rows = [["Total fără TVA", t.net], ...taxes, ...(cents(t.total) !== cents(t.payable) ? [["Total cu TVA", t.total]] : [])];
  // Labels stay on one line; an amount too long for a phone wraps rather than leave the card.
  const total = `${numCellStyle} whitespace-normal! wrap-anywhere`;
  return <table className={`${dataTableStyle} mt-4 border-0 border-t border-solid border-line @2xl:ml-auto @2xl:w-auto @2xl:min-w-80`}>
    <caption className="sr-only">Totalul facturii</caption>
    <tbody className="[&_tr]:border-t-0! [&_th]:whitespace-nowrap [&_td]:py-1! [&_th]:py-1! [&_th]:font-normal [&_th]:text-muted [&_tr:first-child>*]:pt-3!">
      {rows.map(([label, amount], i) => <tr key={i}><th scope="row">{label}</th><td className={total}>{money(amount)}</td></tr>)}
      <tr className="text-base font-bold [&>*]:pt-2!"><th scope="row" className="text-ink! font-bold!">Total de plată</th>
        <td className={total}>{money(t.payable)} {cur}</td></tr>
    </tbody>
  </table>;
}
