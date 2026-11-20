"use client";
// Caută: bounded API matches for the company on screen or all companies; sections stay local.
import { Button, noteStyle } from "./Controls";
import { useContext, useEffect, useId, useMemo, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cap, curOf, dm, docAmount, docName, lei, nOf, phrase } from "@/lib/format";
import { getSearch } from "@/lib/api";
import { href, type Route } from "@/lib/nav";
import { parts, type Capabilities, type SearchResults, type View } from "@/lib/types";
import { IdentityContext, useApp } from "./context";
import { fold } from "./derive";
import { partnerName, roles } from "./Partners";
import { CloseButton, Sheet } from "./Sheets";
import { EMPTY, I, targetOf, typing } from "./ui";

export type Hit = { key: string; title: string; sub: string; to: Partial<Route> };
export type Group = { title: string; hits: Hit[] };

const SECTIONS: [string, Partial<Route>, string, (keyof Capabilities)?][] = [
  ["Acasă", { tab: "home" }, ""], ["Sarcini", { tab: "sarcini" }, ""], ["Vânzări", { tab: "vanzari" }, ""],
  ["Cheltuieli", { tab: "cheltuieli" }, ""], ["Bonuri și chitanțe", { tab: "cheltuieli", side: "bonuri" }, "Cheltuieli"],
  ["Fișiere de plăți", { tab: "plati" }, ""], ["Tranzacții", { tab: "tranzactii" }, ""], ["Parteneri", { tab: "parteneri" }, ""],
  ["Produse", { tab: "produse" }, ""],   ["Rapoarte", { tab: "rapoarte" }, ""], ["Automatizări", { tab: "automatizari" }, ""], ["Setări", { tab: "setari" }, ""],
  ["Firme", { tab: "setari", sec: "firme" }, "Setări", "manage_firms"], ["Integrări", { tab: "setari", sec: "integrari" }, "Setări", "manage_integrations"],
  ["Linkuri și chei API", { tab: "setari", sec: "acces" }, "Setări", "own_tokens"], ["Bondrop", { tab: "setari", sec: "acces" }, "Setări", "member"],
  ["Serii de facturi", { tab: "setari", sec: "facturare" }, "Setări"],
  ["Cont", { tab: "setari", sec: "cont" }, "Setări"], ["Utilizatori", { tab: "setari", sec: "utilizatori" }, "Setări", "manage_users"],
];
const wordsOf = (query: string) => fold(query.trim()).replace(/\bro\s*(?=\d)/g, "").split(/\s+/).filter(Boolean);

/** Format bounded API matches with the same labels and sheet targets; sections stay local. */
export function search(view: View, query: string, can: Partial<Capabilities>, found?: SearchResults): Group[] {
  const words = wordsOf(query);
  const sections: Group = { title: "Secțiuni", hits: SECTIONS.filter(([t, , sub, need = "read"]) => can[need] === true && words.every((w) => fold(`${t} ${sub}`).includes(w)))
    .map(([title, to, sub]) => ({ key: `s${title}`, title, sub, to })) };
  if (!found || words.join("").length < 2) return sections.hits.length ? [sections] : [];
  const tag = (co: string) => parts(view).length > 1 ? view.companies.find((c) => c.key === co)?.name ?? co : "";
  const money = (a: string, c: string | null) => `${lei(a)} ${curOf(c)}`;
  const groups: Group[] = [sections, {
    title: "Parteneri", hits: found.partners.map((p) => ({ key: `p${p.id}`, title: partnerName(p),
      sub: phrase(roles(p), p.cui && p.name ? `CUI ${p.cui}` : "", tag(p.company)), to: { p: p.id } })),
  }, {
    title: "Facturi emise", hits: found.issued.map((r) => ({ key: `e${r.id}`, title: r.client,
      sub: phrase(`nr. ${r.number}`, money(r.total, r.currency), dm(r.issue_date, view.today), tag(r.company)), to: { iss: r.id } })),
  }, {
    title: "Facturi primite", hits: found.invoices.map((r) => ({ key: `i${r.id}`, title: r.supplier,
      sub: phrase(`nr. ${r.number}`, money(r.total, r.currency), dm(r.issue_date, view.today), tag(r.company)), to: { inv: r.id } })),
  }, {
    title: "Bonuri și chitanțe", hits: found.documents.map((d) => ({ key: `d${d.id}`, title: docName(d, view.today),
      sub: phrase(docAmount(d), dm(d.date, view.today), tag(d.company)), to: { doc: d.id } })),
  }];
  return groups.filter((g) => g.hits.length);
}

/** "⌘K" on a Mac, "Ctrl K" elsewhere. */
const shortcut = () => (typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘K" : "Ctrl K");

/** The button that opens Caută; with `keys` (the desktop column), ⌘K or Ctrl+K opens it too, unless the user is typing or
 * a dialog is already open. */
export function SearchButton({ className, keys = false, children }: { className: string; keys?: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!keys) return;
    const f = (e: globalThis.KeyboardEvent) => {
      if (e.key.toLowerCase() !== "k" || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      if (e.isComposing || e.keyCode === 229 || typing(e.target) || document.querySelector('[role="dialog"]')) return;
      e.preventDefault();
      setOpen(true);
    };
    document.addEventListener("keydown", f);
    return () => document.removeEventListener("keydown", f);
  }, [keys]);
  return (
    <>
      <button type="button" className={className} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}
        aria-keyshortcuts={keys ? "Meta+K Control+K" : undefined}>
        {children}
        {keys ? <kbd aria-hidden="true">{shortcut()}</kbd> : null}
      </button>
      {open ? createPortal(<Finder onClose={() => setOpen(false)} />, document.body) : null}
    </>
  );
}

/** The dialog: a combobox and its grouped listbox. Arrows move through the results, Enter opens one, Escape closes. */
function Finder({ onClose }: { onClose: () => void }) {
  const { view, route, go } = useApp();
  const identity = useContext(IdentityContext);
  const id = useId();
  const [query, setQuery] = useState("");
  const [at, setAt] = useState(0);
  const [result, setResult] = useState<{ view: View; query: string; data?: SearchResults; error?: boolean } | null>(null);
  const [retry, setRetry] = useState(0);
  const ready = wordsOf(query).join("").length >= 2;
  const current = result?.view === view && result.query === query ? result : null;
  const loading = ready && !current;
  useEffect(() => {
    if (!ready) return;
    let current = true;
    const controller = new AbortController();
    const timer = setTimeout(() => getSearch(view.co, query, controller.signal).then(
      (data) => current && setResult({ view, query, data }),
      () => current && setResult({ view, query, error: true }),
    ), 200);
    return () => { current = false; clearTimeout(timer); controller.abort(); };
  }, [view, query, ready, retry]);
  const groups = useMemo(() => search(view, query, identity?.identity.me.can ?? {}, current?.data), [view, query, identity, current?.data]);
  const hits = useMemo(() => groups.flatMap((g) => g.hits), [groups]);
  const active = hits.length ? Math.min(at, hits.length - 1) : -1;
  const opt = (i: number) => `${id}-o${i}`;
  useEffect(() => {
    if (active >= 0) document.getElementById(opt(active))?.scrollIntoView?.({ block: "nearest" });
  });
  const open = (h: Hit) => {
    onClose();
    go(targetOf(route, h.to), { focus: true });
  };
  const key = (e: KeyboardEvent) => {
    // An Enter or an arrow that an input method (IME) is still composing belongs to it, not to the list.
    if (!hits.length || e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setAt((active + (e.key === "ArrowDown" ? 1 : hits.length - 1)) % hits.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      open(hits[active]);
    }
  };
  const click = (e: MouseEvent, h: Hit) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    open(h);
  };
  let n = 0;
  return (
    <Sheet label="Caută" cls="find" onClose={onClose}>
      <div className="mb-4 flex items-center gap-2">
        <label className="m-0 flex min-h-12 min-w-0 flex-1 items-center gap-2 rounded-control border border-solid border-line bg-canvas px-3 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus [&>svg]:size-[18px] [&>svg]:text-muted">
          <I n="magnifying-glass" />
          <input className="min-h-11 w-full min-w-0 flex-1 border-0 bg-transparent p-0 font-[inherit] text-base text-ink outline-none placeholder:text-muted" data-autofocus role="combobox" aria-label="Caută" aria-expanded={hits.length > 0} aria-controls={`${id}-list`}
            aria-autocomplete="list" aria-activedescendant={active >= 0 ? opt(active) : undefined} value={query}
            onChange={(e) => {
              setResult(null);
              setQuery(e.target.value);
              setAt(0);
            }}
            onKeyDown={key} placeholder="Parteneri, facturi, sume, bonuri…" autoComplete="off" spellCheck={false} enterKeyHint="go" />
        </label>
        <CloseButton onClose={onClose} />
      </div>
      <div className="[&_ul]:m-0 [&_ul]:list-none [&_ul]:p-0 [&_.fh]:px-3 [&_.fh]:pt-4 [&_.fh]:pb-2 [&_.fh]:text-xs [&_.fh]:font-medium [&_.fh]:text-muted" id={`${id}-list`} role="listbox" aria-label="Rezultate">
        {groups.map((g, gi) => (
          <ul key={g.title} role="group" aria-labelledby={`${id}-g${gi}`}>
            <li role="presentation" id={`${id}-g${gi}`} className="fh">{g.title}</li>
            {g.hits.map((h) => {
              const i = n++;
              return (
                <li key={h.key} role="presentation">
                  <a className="flex min-h-14 flex-col justify-center gap-1 rounded-control px-3 py-2 text-ink no-underline hover:bg-inset aria-selected:bg-selected [&_b]:text-sm [&_b]:font-medium [&_small]:text-xs [&_small]:text-muted [&_small]:break-words" role="option" id={opt(i)} aria-selected={i === active} tabIndex={-1} href={href(targetOf(route, h.to))}
                    aria-label={h.sub ? `${h.title}, ${h.sub}` : undefined}
                    onClick={(e) => click(e, h)} onMouseMove={() => i !== active && setAt(i)}>
                    <b>{h.title}</b>
                    {h.sub ? <small>{h.sub}</small> : null}
                  </a>
                </li>
              );
            })}
          </ul>
        ))}
      </div>
      {/* What a query found, announced as it changes: the count only to screen readers (the list shows it), "nothing" to all. */}
      <p role="status" className={hits.length && !loading && !current?.error ? "vh sr-only" : noteStyle}>
        {loading ? "Se încarcă…" : current?.error ? "Datele nu au putut fi încărcate. Încearcă din nou." : !query.trim() ? "" : hits.length ? cap(nOf(hits.length, "un rezultat", "rezultate")) : EMPTY}
      </p>
      {current?.error ? <Button type="button" className="btn g" onClick={(e) => { e.currentTarget.closest<HTMLElement>('[role="dialog"]')?.querySelector<HTMLInputElement>('[role="combobox"]')?.focus(); setResult(null); setRetry((n) => n + 1); }}>Reîncearcă</Button> : null}
    </Sheet>
  );
}
