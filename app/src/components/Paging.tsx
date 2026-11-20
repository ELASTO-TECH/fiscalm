"use client";
import { Button, Input, Select, Skeleton, SkeletonRows } from "./Controls";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Page } from "@/lib/types";
import { cap, integer, luna } from "@/lib/format";
import { I } from "./ui";

/** Keep one response, discard stale reads, and reset cursors whenever the reader's filters or company change. `scope` keeps the
 * months across reads; `query` names the rows' question (firm, screen, filters, search; the scope by default): while the same
 * query and page are read again (the state changed after an action, the app came back to the front) its rows stay on screen,
 * `refreshing`, instead of blanking. Any other read (another firm, filter or page) shows nothing of the previous one. */
export function usePage<T>(read: (cursor: string, signal?: AbortSignal) => Promise<Page<T>>, delay = 0, scope: unknown = read, first?: Page<T>,
  query: unknown = scope) {
  const [position, setPosition] = useState({ read, cursors: [""] });
  const [attempt, setAttempt] = useState(0);
  const [stored, setStored] = useState<{ read: typeof read; cursor: string; result: Page<T> | null; error: boolean; attempt: number; scope: unknown; query: unknown; months: string[] }>();
  const cursors = position.read === read ? position.cursors : [""];
  if (position.read !== read) setPosition({ read, cursors });
  const cursor = cursors[cursors.length - 1];
  useEffect(() => {
    if (!cursor && first) return;
    let current = true;
    const controller = new AbortController();
    const load = () => { void read(cursor, controller.signal).then((result) => {
      if (current) setStored({ read, cursor, result, error: false, attempt, scope, query, months: result.months ?? [] });
    }, () => {
      if (current) setStored((old) => ({ read, cursor, result: old?.read === read ? old.result : null, error: true, attempt, scope, query, months: old && old.scope === scope ? old.months : [] }));
    }); };
    const timer = delay ? setTimeout(load, delay) : null;
    if (!delay) load();
    return () => { current = false; if (timer) clearTimeout(timer); controller.abort(); };
  }, [read, cursor, attempt, delay, scope, query, first]);
  const initial = !cursor ? first : undefined;
  const current = stored?.read === read && stored.cursor === cursor && stored.attempt === attempt;
  const error = !initial && current && stored.error;
  const loading = !initial && !current;
  const kept = loading && stored && stored.query === query && stored.cursor === cursor && !stored.error ? stored.result : null;
  const result = initial ?? (current && !error ? stored.result : kept);
  const total = result?.total ?? (stored?.read === read ? stored.result?.total : undefined) ?? first?.total ?? 0;
  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  useRetryWhenBack(error, retry);
  return {
    data: result?.data ?? [], refreshing: !!kept, months: stored && stored.scope === scope ? stored.months : [], total, loading, error, index: cursors.length - 1,
    previous: () => !loading && cursors.length > 1 && setPosition({ read, cursors: cursors.slice(0, -1) }),
    next: () => !loading && result?.next_cursor && setPosition({ read, cursors: [...cursors, result.next_cursor] }),
    hasNext: !!result?.next_cursor, retry,
  };
}

/** One read in three states: unknown (no `data`, not `failed`), loaded (`data`) and failed (`failed`; the data an earlier read of
 * the same `key` brought stays, for the caller to mark as old). It reads again when `key` changes (another firm or person:
 * unknown again), when `fresh` changes (the app's state moved on) and on `reload` (Reîncearcă); meanwhile the data stays on
 * screen. Only the latest read answers; `current` says the data is that read's. */
export function useLoaded<T>(read: () => Promise<T>, key: unknown, fresh?: unknown) {
  const reader = useRef(read);
  useLayoutEffect(() => { reader.current = read; });
  const [n, setN] = useState(0);
  const [got, setGot] = useState<{ key: unknown; fresh: unknown; n: number; data: T | undefined; failed: boolean }>();
  useEffect(() => {
    let live = true;
    reader.current().then((data) => { if (live) setGot({ key, fresh, n, data, failed: false }); },
      () => { if (live) setGot((old) => ({ key, fresh, n, data: old && old.key === key ? old.data : undefined, failed: true })); });
    return () => { live = false; };
  }, [key, fresh, n]);
  const mine = got?.key === key ? got : undefined;
  const reload = useCallback(() => setN((x) => x + 1), []);
  useRetryWhenBack(!!mine?.failed, reload);
  return { data: mine?.data, failed: !!mine?.failed, current: !!mine && mine.fresh === fresh && mine.n === n,
    reload };
}

/** A read that failed runs again by itself when the browser is back online or its tab comes back to the front. */
export function useRetryWhenBack(failed: boolean, retry: () => void) {
  const again = useRef(retry);
  useLayoutEffect(() => { again.current = retry; });
  useEffect(() => {
    if (!failed) return;
    const back = () => { if (document.visibilityState === "visible") again.current(); };
    window.addEventListener("online", back);
    document.addEventListener("visibilitychange", back);
    return () => { window.removeEventListener("online", back); document.removeEventListener("visibilitychange", back); };
  }, [failed]);
}

/** In place of what a read would have shown: that it failed, and Reîncearcă. */
export function LoadFailed({ retry, text = "Datele nu au putut fi încărcate." }: { retry: () => void; text?: string }) {
  return <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-4 text-sm text-muted" role="alert">
    <p className="m-0">{text}</p>
    <Button type="button" className="btn g" onClick={retry}>Reîncearcă</Button>
  </div>;
}

export function PageButtons({ total, index, loading = false, hasNext, previous, next, label = "Paginare" }: {
  total: number; index: number; loading?: boolean; label?: string; hasNext: boolean; previous: () => void; next: () => void;
}) {
  return <>
    {total > 50 ? <nav className="flex flex-wrap justify-between gap-2 px-0 py-3 [&_span]:text-xs [&_span]:tabular-nums" aria-label={label} aria-busy={loading}>
      <Button type="button" className="btn g" aria-disabled={loading || index === 0} onClick={previous}>Pagina anterioară</Button>
      <span role="status">{integer(index * 50 + 1)}–{integer(Math.min((index + 1) * 50, total))} din {integer(total)}</span>
      <Button type="button" className="btn g" aria-disabled={loading || !hasNext} onClick={next}>Pagina următoare</Button>
    </nav> : null}
  </>;
}

/** `rows`: the shapes that stand in for the page while it is read; 0 where the control it pages is already on screen (a
 * select's options), which then only says so to a screen reader. */
export function PageNav({ page, label, rows = 6 }: { page: ReturnType<typeof usePage>; label?: string; rows?: number }) {
  const group = useRef<HTMLDivElement>(null);
  return <div ref={group} tabIndex={-1}>
    <PageButtons label={label} total={page.total} index={page.index} loading={page.loading} hasNext={page.hasNext} previous={page.previous} next={page.next} />
    {page.loading && !page.refreshing ? rows ? <SkeletonRows rows={rows} /> : <span role="status" className="sr-only">Se încarcă…</span> : null}
    {page.error ? <LoadFailed retry={() => { group.current?.focus(); page.retry(); }} /> : null}
  </div>;
}

export const listCountStyle = "m-0 flex min-h-[50px] items-center border-0 border-b border-solid border-line px-4 text-[13px] text-muted";
/** A list's count over its rows ("15 cheltuieli"), its line held by a shape while the first page is read. */
export function ListCount({ page, text }: { page: ReturnType<typeof usePage>; text: string }) {
  if (page.total) return <p className={listCountStyle}>{text}</p>;
  return page.loading && !page.refreshing ? <p className={listCountStyle} aria-hidden="true"><Skeleton className="h-3.5 w-24" /></p> : null;
}

/** Above a list: the month (when the list has months) and a search, side by side: Contra's filter chip and search pill. The month's name is
 * its first choice ("Toate lunile") and its label. */
export function ListTools({ month, months, onMonth, monthLabel = "Luna", query, onQuery, label }: {
  month?: string; months?: string[]; onMonth?: (month: string) => void; monthLabel?: string;
  query: string; onQuery: (query: string) => void; label: string;
}) {
  return (
    <div className="mb-4 flex min-w-0 flex-col gap-2 sm:flex-row">
      {onMonth ? (
        <Select className="sm:w-52 sm:shrink-0" aria-label={monthLabel} value={month} onChange={(e) => onMonth(e.target.value)}>
          <option value="">Toate lunile</option>
          {months?.map((m) => <option key={m} value={m}>{cap(luna(m))}</option>)}
        </Select>
      ) : null}
      <label className="relative block min-w-0 flex-1 [&>svg]:pointer-events-none [&>svg]:absolute [&>svg]:top-1/2 [&>svg]:left-4 [&>svg]:size-4 [&>svg]:-translate-y-1/2 [&>svg]:text-muted">
        <I n="magnifying-glass" />
        <Input className="rounded-full! pl-10" type="search" value={query} onChange={(e) => onQuery(e.target.value)} placeholder={label}
          aria-label={label} autoComplete="off" enterKeyHint="search" />
      </label>
    </div>
  );
}
