"use client";
// Firma › Produse: the firm's catalog (docs/information-architecture.md, Firmă › Produse); a row opens its sheet
// (ProductForm.tsx) to change, archive, restore or delete it.
import { createPortal } from "react-dom";
import { useCallback, useEffect, useState } from "react";
import { ApiError, getProduct, getProducts } from "@/lib/api";
import { curOf, lei } from "@/lib/format";
import { NO_SHEET } from "@/lib/nav";
import { isAll, type Co, type Invoicing, type Product } from "@/lib/types";
import { Button, Panel, filterLinkStyle, noteLeftStyle } from "./Controls";
import { useApp, useCan } from "./context";
import { InvoicingFailed, useInvoicing } from "./Invoicing";
import { ListTools, PageNav, usePage } from "./Paging";
import { KIND, NEW_PRODUCT, ProductSheet, unitOf, vatLabel } from "./ProductForm";
import { CloseX, SHEET_SHAPES, Sheet, SheetSkeleton } from "./Sheets";
import { Empty, I, L, Off, Row, Title, tableColumns } from "./ui";

/** Firma › Produse: Active or Arhivate, searched by name or code. */
export function Products() {
  const { view, route, go } = useApp();
  if (isAll(view)) return null; // one firm's catalog: Toate firmele opens the last firm chosen (nav.ts)
  return <ProductList key={view.co} co={view.co} archived={route.f === "archived"} sheet={route.prod}
    close={() => go({ ...route, ...NO_SHEET })} />;
}

/** The sheet's product: the listed row, else read by its id (a link). `missing` only when the engine has none by that id
 * (404); a read that failed otherwise is `failed`, with `retry`. */
function useProduct(co: Co, id: string, listed?: Product) {
  const [read, setRead] = useState<{ id: string; product?: Product; failed?: boolean }>();
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (listed || !id || id === NEW_PRODUCT) return;
    const ask = new AbortController();
    getProduct(co, id, ask.signal).then((product) => setRead({ id, product }),
      (e) => !ask.signal.aborted && setRead({ id, failed: !(e instanceof ApiError && e.status === 404) }));
    return () => ask.abort();
  }, [co, id, listed, attempt]);
  const mine = !listed && read?.id === id ? read : undefined;
  return { product: listed ?? mine?.product, missing: !!mine && !mine.product && !mine.failed, failed: !!mine?.failed,
    retry: () => { setRead(undefined); setAttempt((n) => n + 1); } };
}

function ProductList({ co, archived, sheet, close }: { co: Co; archived: boolean; sheet: string; close: () => void }) {
  const { view } = useApp();
  const inv = useInvoicing(co);
  const [query, setQuery] = useState("");
  const f = archived ? "archived" : "";
  // `view`: a new state after an action reads the page again
  const read = useCallback((cursor: string, signal?: AbortSignal) => getProducts(co, { q: query, f, cursor }, signal), [co, query, f, view]); // eslint-disable-line react-hooks/exhaustive-deps
  const page = usePage(read, query ? 200 : 0, `${co}|${f}|${query}`);
  const { product, missing, failed, retry } = useProduct(co, sheet, page.data.find((p) => p.id === sheet));
  const ready = sheet === NEW_PRODUCT || product || missing;
  return (
    <div className="min-w-0 max-w-none">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <Title className="mb-0!">Produse</Title>
        <AddProductButton />
      </div>
      <nav className="my-4 flex flex-wrap gap-1" aria-label="Filtru">
        {([["", "Active"], ["archived", "Arhivate"]] as const).map(([to, label]) => (
          <L key={to} to={{ tab: "produse", f: to }} className={filterLinkStyle} current={f === to}>{label}</L>
        ))}
      </nav>
      <ListTools query={query} onQuery={setQuery} label="Caută produs sau cod" />
      <Panel padded={false} aria-busy={page.loading} className="card tbl rounded-panel!">
        {page.total ? <div className={`hidden min-h-[42px] items-center gap-x-4 border-0 border-b border-solid border-line px-4 text-[13px] text-muted min-[1100px]:grid ${tableColumns}`} aria-hidden="true">
          <span /><span>Denumire</span><span>Tip, UM și TVA</span><span>Cod</span><span className="text-right">Preț</span>
        </div> : null}
        <PageNav page={page} rows={4} />
        <div className="divide-x-0 divide-y divide-solid divide-line/60">
          {page.data.map((p) => <ProductRow key={p.id} p={p} inv={inv.data} />)}
        </div>
        {!page.loading && !page.error && !page.total ? <Empty action={!archived && !query ? <AddProductButton first /> : null} /> : null}
      </Panel>
      {sheet ? createPortal(
        inv.data && ready ? <ProductSheet key={sheet} inv={inv.data} product={product} missing={missing} onClose={close} />
          : <Sheet onClose={close} label="Produs"><CloseX onClose={close} />{inv.failed ? <InvoicingFailed retry={inv.retry} retrying={inv.retrying} />
            : failed ? <div className="mt-5" role="alert"><p className={noteLeftStyle}>Produsul nu a putut fi încărcat.</p>
              <Button type="button" variant="secondary" onClick={retry}>Reîncearcă</Button></div> : <SheetSkeleton shape={SHEET_SHAPES.product} />}</Sheet>,
        document.body) : null}
    </div>
  );
}

/** A product's row; its unit and VAT by their names once the invoicing data has them. */
function ProductRow({ p, inv }: { p: Product; inv: Invoicing | null }) {
  return <Row icon={p.kind === "serviciu" ? "wrench" : "package"} title={p.name}
    sub={[KIND[p.kind], inv ? unitOf(inv, p.unit) : "", inv ? vatLabel(inv, p) : ""].filter(Boolean).join(", ")}
    val={`${lei(p.unit_price)} ${curOf(p.currency)}`} vsub={p.code} to={{ prod: p.id }} badge={p.archived ? "quiet" : ""} />;
}

/** Adaugă produs: for one firm, for those who may change records. `first`: the empty catalog's invitation. */
function AddProductButton({ first = false }: { first?: boolean }) {
  const { view } = useApp();
  if (!useCan("member")) return null;
  const label = first ? "Adaugă primul produs" : "Adaugă produs";
  return isAll(view) ? <Off cls="btn" why="Alege mai întâi o firmă.">{label}</Off>
    : <L to={{ prod: NEW_PRODUCT }} className={first ? "btn" : "btn primary"}><I n="plus" />{label}</L>;
}

