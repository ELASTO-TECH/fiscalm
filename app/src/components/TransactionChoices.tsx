"use client";
import { Input, Label } from "./Controls";
import { useCallback, useMemo, useState } from "react";
import { getTransactionChoices } from "@/lib/api";
import type { Page, TransactionChoiceCollection, TransactionChoiceRows } from "@/lib/types";
import { useCompany } from "./context";
import { usePage } from "./Paging";

const EMPTY: Page<never> = { data: [], total: 0, next_cursor: null };

export function useChoicePage<K extends TransactionChoiceCollection>(id: string, collection: K, first: Page<TransactionChoiceRows[K]> | undefined,
  q = "", partner?: string, empty = false) {
  const { co } = useCompany();
  const read = useCallback(async (cursor: string, signal?: AbortSignal) => {
    if (empty) return EMPTY;
    if (!cursor && first) return first;
    const result = await getTransactionChoices(co, { id, collection, cursor, ...(q ? { q } : {}), ...(partner ? { partner } : {}) }, signal);
    return result.pages[collection]!;
  }, [co, id, collection, q, partner, empty, first]);
  return usePage(read, q ? 200 : 0, read, empty ? EMPTY : first);
}

/** Only the user's existing selection is retained; historical option pages are replaced independently. */
export function useSelected(id: string, initial: Page<TransactionChoiceRows["selected"]>) {
  const { co } = useCompany();
  const [first] = useState(initial);
  const ready = useMemo(() => !first.next_cursor ? { data: [first.data], total: 1, next_cursor: null } : undefined, [first]);
  const read = useCallback(async (_cursor: string, signal?: AbortSignal) => {
    const rows = [...first.data];
    let cursor = first.next_cursor;
    while (cursor) {
      const result = await getTransactionChoices(co, { id, collection: "selected", cursor, limit: "200" }, signal);
      rows.push(...result.pages.selected!.data);
      cursor = result.pages.selected!.next_cursor;
    }
    return { data: [rows], total: 1, next_cursor: null };
  }, [co, id, first]);
  return usePage(read, 0, read, ready);
}

export function ChoiceSearch({ id, label, total, value, onChange, always = false, focus = false }: {
  id: string; label: string; total: number; value: string; onChange: (q: string) => void; always?: boolean; focus?: boolean;
}) {
  const [shown, setShown] = useState(always || total > 50 || !!value);
  if (!shown && (total > 50 || value)) setShown(true);
  return shown ? <><Label htmlFor={id}>{label}</Label>
    <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} maxLength={200} autoComplete="off" autoFocus={focus} /></> : null;
}
