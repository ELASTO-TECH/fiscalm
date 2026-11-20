// Company, tab and open sheet live in the URL query (?co=&tab=&side=&sec=&tx=&inv=&iss=&mail=&doc=&int=&p=&pf=&new=&for=&month=&f=&per=&from=&to=). Changes go through
// history.pushState, so switching never reloads the page and the back button closes a sheet or returns to the previous tab.
"use client";
import { useCallback, useSyncExternalStore } from "react";
import { PRODUCT } from "./product";
import type { Co, CoView } from "./types";

/** The sections of docs/information-architecture.md, section 3, and the phone's "Mai mult" list (mai-mult). */
export const TABS = ["home", "sarcini", "vanzari", "cheltuieli", "plati", "tranzactii", "parteneri", "produse", "rapoarte", "automatizari", "setari",
  "mai-mult"] as const;
export type Tab = (typeof TABS)[number];
/** Tabs that can show every company at once (Mai mult is only links, so Toate firmele stays on through it); the others need
 * one and show the last company chosen. */
export const BOTH: readonly Tab[] = ["home", "sarcini", "vanzari", "cheltuieli", "parteneri", "automatizari", "mai-mult"];

/** Setări's pages: its overview (""), Firme, Integrări, Linkuri și chei API (acces). */
export const SECTIONS = ["firme", "contabil", "facturare", "mod", "integrari", "acces", "utilizatori", "cont"] as const;
/** Acasă's periods in the URL (`per`), besides the current month (""): Dashboard.tsx names them. */
export const PERS = ["3luni", "6luni", "12luni", "an"] as const;
export type Section = "" | (typeof SECTIONS)[number];

const TAB_NAMES: Record<Tab, string> = {
  home: "Acasă", sarcini: "Sarcini", vanzari: "Vânzări", cheltuieli: "Cheltuieli", plati: "Fișiere de plăți", tranzactii: "Tranzacții", parteneri: "Parteneri", produse: "Produse",
  rapoarte: "Rapoarte", automatizari: "Automatizări", setari: "Setări", "mai-mult": "Mai mult",
};
const SECTION_NAMES: Record<(typeof SECTIONS)[number], string> = { mod: "Mod", firme: "Firme", contabil: "Contabil", facturare: "Facturare", integrari: "Integrări", acces: "Linkuri și chei API", utilizatori: "Utilizatori", cont: "Cont" };

/** The window's title: the page, the app, the company ("Cheltuieli, fiscalm, ACME"); without one under Toate firmele. */
export function pageTitle(r: Pick<Route, "tab" | "sec">, company?: string): string {
  return [r.sec ? SECTION_NAMES[r.sec] : TAB_NAMES[r.tab], PRODUCT, company].filter(Boolean).join(", ");
}

/** What makes a different page, with its own title, rather than a sheet over one or a filter of it. */
export const pageKey = (r: Pick<Route, "tab" | "sec" | "month">): string => `${r.tab}|${r.sec}|${r.tab === "rapoarte" ? r.month : ""}`;

export type Route = {
  co: CoView;
  tab: Tab;
  sec: Section;
  /** Sheets: a received invoice, an issued invoice, a mail to place, a receipt. */
  set: string;
  ord: string;
  /** A transaction's sheet (`transactions.rows[].id`): what explains it, and the user's explanation. */
  tx: string;
  inv: string;
  iss: string;
  mail: string;
  doc: string;
  /** Integrări: an instance's sheet (its id), or "new:<kind>" for the form that adds one. */
  int: string;
  usr: string;
  /** A partner's sheet (`partners[].id`). */
  p: string;
  /** A payment file's sheet (`files[].id`). */
  pf: string;
  /** A product's sheet (`products[].id`), or "nou" for Adaugă produs. */
  prod: string;
  /** Factură nouă: "1" for a new invoice, else the id of the draft being edited. */
  new: string;
  /** A new invoice for this partner (`partners[].id`): its saved client is chosen, else the new client's form is filled. */
  for: string;
  /** Rapoarte: the open report (YYYY-MM). Tranzacții: the month shown ("" for all). */
  month: string;
  /** Cheltuieli: Facturi ("") or Bonuri și chitanțe. */
  side: "" | "bonuri";
  /** Cheltuieli, Vânzări and Parteneri: the filter (all, still to pay or collect, overdue). Tranzacții: De explicat (""),
   * Toate (all), Intrări (in), Ieșiri (out). Produse: active ("") or archived. */
  f: "" | "open" | "late" | "all" | "in" | "out" | "archived";
  /** Acasă: the period its numbers cover, this month (""), the last three months or this year. */
  per: "" | (typeof PERS)[number];
  /** Tranzacții: the days a number on Acasă counts (ISO, both included), with `f` for money in or out. */
  from: string;
  to: string;
};
export type To = Partial<Route> & { co: CoView };

/** The route fields that open a sheet over the page: the one list every "is a sheet open" check reads. */
export const SHEETS = ["set", "ord", "tx", "inv", "iss", "mail", "doc", "int", "usr", "p", "pf", "prod", "new"] as const satisfies readonly (keyof Route)[];
/** The open sheet's route field, or "" with none open. */
export const openSheet = (r: Route): (typeof SHEETS)[number] | "" => SHEETS.find((k) => r[k]) ?? "";

/** No sheet open. */
export const NO_SHEET = { set: "", ord: "", tx: "", inv: "", iss: "", mail: "", doc: "", int: "", usr: "", p: "", pf: "", prod: "", new: "", for: "" } as const;

const EVENT = "fiscalm:nav";
const KEY = "fiscalm:co";

const KEY_RE = /^[A-Z0-9][A-Z0-9_-]{0,19}$/;
/** A company key as the engine's config allows it, or ALL; anything else is "". */
export const coKey = (c: string | null | undefined): CoView => {
  const k = (c || "").toUpperCase();
  return k === "ALL" || KEY_RE.test(k) ? k : "";
};

/** The last single company chosen (Plăți, Tranzacții and Rapoarte show it when Toate firmele is on); "" before any. */
export function lastCo(): Co {
  try {
    const k = coKey(localStorage.getItem(KEY));
    return k === "ALL" ? "" : k;
  } catch {
    return "";
  }
}

export function rememberCo(co: CoView) {
  try {
    if (co && co !== "ALL") localStorage.setItem(KEY, co);
  } catch {
    // private mode: the default company is fine
  }
}

/** A company that is no longer in the config: not offered again. */
export function forgetCo() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // private mode
  }
}

/** The view a tab shows: one company, or ALL where the tab can show all; "" when no company is known yet
 * (the app then reads ALL to learn the companies and picks one). */
const company = (co: CoView, tab: Tab): CoView => (co === "ALL" && !BOTH.includes(tab) ? lastCo() : co);

/** The page (pageKey) the user just asked for by pressing a link or a search result: its h1 takes focus once it is shown.
 * Back, forward, an action's redirect or any other move to another page forgets it, so focus stays where it was. */
let asked = "";

/** Whether the user asked for this page (see `asked`); answers once, then forgets. */
export function takeFocus(page: string): boolean {
  const yes = asked !== "" && asked === page;
  asked = "";
  return yes;
}

function subscribe(cb: () => void) {
  const pop = () => {
    asked = "";
    cb();
  };
  window.addEventListener("popstate", pop);
  window.addEventListener(EVENT, cb);
  return () => {
    window.removeEventListener("popstate", pop);
    window.removeEventListener(EVENT, cb);
  };
}

/** The address's #section, followed like the route (a link to the same page without it changes only this). */
export const useHash = (): string => useSyncExternalStore(subscribe, () => window.location.hash, () => "");

/** A calendar day as ISO (2026-10-27), or "": the format and a real date (no 2026-02-31). */
const isoDay = (d: string | null): string =>
  d && /^\d{4}-\d{2}-\d{2}$/.test(d) && !Number.isNaN(Date.parse(`${d}T00:00:00Z`))
    && new Date(`${d}T00:00:00Z`).toISOString().slice(0, 10) === d ? d : "";

export function parse(search: string): Route {
  const q = new URLSearchParams(search);
  const t = q.get("tab") || "home";
  const tab = (TABS as readonly string[]).includes(t) ? (t as Tab) : "home";
  const co = coKey(q.get("co")) || lastCo();
  const f = q.get("f");
  const sec = q.get("sec") || "";
  return {
    co: company(co, tab), tab, sec: tab === "setari" && (SECTIONS as readonly string[]).includes(sec) ? (sec as Section) : "",
    set: q.get("set") || "", ord: q.get("ord") || "", tx: q.get("tx") || "", inv: q.get("inv") || "", iss: q.get("iss") || "", mail: q.get("mail") || "", doc: q.get("doc") || "", int: tab === "setari" ? q.get("int") || "" : "",
    usr: tab === "setari" && sec === "utilizatori" ? q.get("usr") || "" : "",
    p: q.get("p") || "", pf: /^\d{1,12}$/.test(q.get("pf") || "") ? q.get("pf")! : "",
    prod: /^[\w.-]{1,64}$/.test(q.get("prod") || "") ? q.get("prod")! : "", new: q.get("new") || "", for: q.get("new") === "1" ? q.get("for") || "" : "",
    month: q.get("month") || "", side: tab === "cheltuieli" && q.get("side") === "bonuri" ? "bonuri" : "",
    f: tab === "tranzactii" ? (f === "all" || f === "in" || f === "out" ? f : "") : tab === "produse" ? (f === "archived" ? f : "")
      : f === "open" || f === "late" ? f : "",
    per: tab === "home" ? PERS.find((p) => p === q.get("per")) ?? "" : "",
    from: tab === "tranzactii" ? isoDay(q.get("from")) : "", to: tab === "tranzactii" ? isoDay(q.get("to")) : "",
  };
}

export function href(r: To, at = ""): string {
  const tab = r.tab ?? "home";
  const co = company(r.co, tab);
  const q = new URLSearchParams(co ? { co } : {});
  if (tab !== "home") q.set("tab", tab);
  for (const k of ["side", "sec", "set", "ord", "tx", "inv", "iss", "mail", "doc", "int", "usr", "p", "pf", "prod", "new", "for", "month", "f", "per", "from", "to"] as const) if (r[k]) q.set(k, r[k]);
  return `?${q}${at ? `#${at}` : ""}`;
}

/** `focus`: the user pressed a link to this place; if it is another page, its h1 takes focus (App.tsx). */
export type GoOpts = { at?: string; replace?: boolean; focus?: boolean };

export function useRoute(): [Route | null, (to: To, opts?: GoOpts) => void] {
  const search = useSyncExternalStore(subscribe, () => window.location.search, () => null);
  const go = useCallback((to: To, opts: GoOpts = {}) => {
    const was = parse(window.location.search);
    const url = window.location.pathname + href(to, opts.at);
    if (opts.replace) window.history.replaceState(null, "", url);
    else window.history.pushState(null, "", url);
    // A correction on the same page (the company filled in) keeps what the user asked for; another page replaces it.
    const page = pageKey(parse(window.location.search));
    if (page !== pageKey(was)) asked = opts.focus ? page : "";
    window.dispatchEvent(new Event(EVENT));
    // A section (#fisier) scrolls into view once rendered; another tab or company starts at the top; a sheet keeps the scroll.
    if (opts.at) requestAnimationFrame(() => requestAnimationFrame(() => document.getElementById(opts.at!)?.scrollIntoView()));
    else if (was.co !== to.co || was.tab !== (to.tab ?? "home") || was.sec !== (to.sec ?? "") || was.month !== (to.month ?? "")) {
      window.scrollTo(0, 0);
    }
  }, []);
  return [search === null ? null : parse(search), go];
}
