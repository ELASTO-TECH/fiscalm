"use client";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { getDetail } from "@/lib/api";
import type { DetailKind, StateDetail } from "@/lib/types";
import { AppCtx, Company, useApp } from "./context";
import { PageNav, usePage } from "./Paging";

const DetailPage = createContext<{ page: ReturnType<typeof usePage<StateDetail>>; detail: StateDetail } | null>(null);

export const useDetail = () => useContext(DetailPage)!.detail;

/** The sheet retains one document while its current page or the same document refreshes. */
export function DocumentDetail({ kind, id, pending, children }: {
  kind: DetailKind; id: string; pending: (status: ReactNode, loading: boolean) => ReactNode; children: ReactNode;
}) {
  const app = useApp();
  const { view } = app;
  const scope = `${view.co}|${kind}|${id}`;
  const read = useCallback(async (cursor: string, signal?: AbortSignal) => {
    const result = await getDetail(view.co, kind, id, cursor, signal);
    return { data: [result], total: result.related.total, next_cursor: result.related.next_cursor };
  }, [view, kind, id]);
  const page = usePage(read, 0, scope);
  const [previous, setPrevious] = useState<{ scope: string; data: StateDetail }>();
  const incoming = page.data[0];
  if (incoming && previous?.data !== incoming) setPrevious({ scope, data: incoming });
  const detail = incoming ?? (previous?.scope === scope ? previous.data : undefined);
  if (!detail) return pending(<PageNav page={page} />, page.loading);
  return <AppCtx.Provider value={{ ...app, view: detail.state }}>
    <Company s={detail.state}><DetailPage.Provider value={{ page, detail }}>{children}</DetailPage.Provider></Company>
  </AppCtx.Provider>;
}

export function DetailPages() {
  const page = useContext(DetailPage)?.page;
  return page && (page.total > 50 || page.loading || page.error) ? <PageNav page={page} /> : null;
}
