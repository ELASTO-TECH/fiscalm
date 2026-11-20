"use client";
import { Panel, rowIconStyle, rowStyle, rowTextStyle } from "./Controls";
import { useCallback } from "react";
import { getPartnerPages } from "@/lib/api";
import type { MemoryKey, Page, RememberedRow, State } from "@/lib/types";
import { Company } from "./context";
import { PageNav, usePage } from "./Paging";
import { Act, I, ICON_TINT } from "./ui";

export const KEPT_BY: Record<MemoryKey, string> = { iban: "Contul", card: "Cardul", name: "Partenerul" };

/** Exact remembered choices and their complete labels, one current page per company or partner. */
export function RememberedList({ s, partner, first }: { s: State; partner?: string; first?: Page<RememberedRow> }) {
  const read = useCallback(async (cursor: string, signal?: AbortSignal) =>
    (await getPartnerPages(s.co, { ...(partner ? { id: partner } : {}), collection: "remembered", cursor }, signal)).pages.remembered!, [s, partner]);
  const page = usePage(read, 0, read, first);
  if (!page.total && !page.loading && !page.error) return null;
  return <Panel padded={false}>
    <PageNav page={page} label="Reguli salvate" />
    {page.data.map(({ id, memory: m, text }) => <div key={id} className={`${rowStyle} flex-wrap sm:flex-nowrap`}>
      <span className={`${rowIconStyle} ${ICON_TINT}`}><I n="clock" /></span>
      <span className={rowTextStyle}><b>{`${KEPT_BY[m.by]} ${m.shown}`}</b><small>{text}</small></span>
      <Company s={s}><Act action="memory-forget" body={{ id: m.id }} label="Șterge asocierea" cls="btn g" /></Company>
    </div>)}
  </Panel>;
}
