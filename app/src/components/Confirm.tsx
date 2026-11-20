"use client";
// The app's own confirmation: what will happen, the action named by its verb, and Renunță.
import { createPortal, flushSync } from "react-dom";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Actions, Button } from "./Controls";
import { Sheet } from "./Sheets";

/** A question before a request: `text` says what will happen, `yes` is the action's verb, `danger` marks one that removes
 * or undoes something (its dialog starts on Renunță). */
export type Ask = { text: string; yes: string; danger?: boolean };

type Open = { q: Ask; answer: (yes: boolean) => void };

/** One question at a time. `ask` opens the dialog and resolves true only for its action; Renunță, Escape, the backdrop,
 * a newer question, `cancel` and the owner unmounting all resolve false. Each question answers once. Render `dialog`;
 * call `cancel` when what the question is about leaves the screen (a navigation), so a stale question never sends. */
export function useConfirm(): { ask: (q: Ask) => Promise<boolean>; cancel: () => void; dialog: ReactNode } {
  const [open, setOpen] = useState<Open | null>(null);
  const current = useRef<Open | null>(null);
  const ask = useCallback((q: Ask) => new Promise<boolean>((resolve) => {
    current.current?.answer(false);
    const o: Open = { q, answer: (yes) => {
      if (current.current !== o) return;
      current.current = null;
      // A yes (always a click) closes the dialog before the request goes on: the page is no longer inert and focus is
      // back on the opener, so whatever the answer draws can take focus. A no may come from a cleanup: no flush there.
      if (yes) flushSync(() => setOpen(null));
      else setOpen(null);
      resolve(yes);
    } };
    current.current = o;
    setOpen(o);
  }), []);
  const cancel = useCallback(() => current.current?.answer(false), []);
  useEffect(() => cancel, [cancel]);
  return { ask, cancel, dialog: open ? <Confirm key={open.q.text} {...open} /> : null };
}

// A held key repeats: the Enter that opened the dialog must not also press its action.
const noRepeat = (e: KeyboardEvent) => {
  if (e.repeat && (e.key === "Enter" || e.key === " ")) e.preventDefault();
};

/** On the page's body, so everything else (an open sheet too) turns inert under it. */
function Confirm({ q, answer }: Open) {
  return createPortal(
    <Sheet onClose={() => answer(false)} label={q.text} cls="ask">
      <div onKeyDownCapture={noRepeat}>
        <p className="mt-0 mb-5 text-base! leading-relaxed text-ink">{q.text}</p>
        <Actions>
          <Button type="button" variant={q.danger ? "danger" : "primary"} data-autofocus={q.danger ? undefined : true}
            onClick={() => answer(true)}>{q.yes}</Button>
          <Button type="button" variant="secondary" data-autofocus={q.danger ? true : undefined}
            onClick={() => answer(false)}>Renunță</Button>
        </Actions>
      </div>
    </Sheet>,
    document.body,
  );
}
