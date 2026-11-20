"use client";
import { Button, Skeleton, SkeletonRows, controlHeight, markerStyle, pageTabsStyle, motion, replay, rowsMayLeave, rowsStay, useMarker, bannerStyle, iconButtonStyle, buttonTone, surfaceStyle } from "./Controls";
import { SignOut, UserAccount } from "./UserAccount";
import { SettlementSheet } from "./Settlements";
import { useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ComponentType } from "react";
import { createPortal } from "react-dom";
import { ApiError, CANCEL_REAUTHENTICATION, NO_ANSWER, getBootstrap, identityKey, post, type Action } from "@/lib/api";
import { BOTH, NO_SHEET, coKey, parse, openSheet, forgetCo, pageKey, pageTitle, rememberCo, takeFocus, useRoute, type Tab } from "@/lib/nav";
import { pal } from "@/lib/palette";
import { isAll, isSetup, parts, type ActionResult, type Co, type CoView, type SetupState, type View } from "@/lib/types";
import { nOf } from "@/lib/format";
import { useConfirm, type Ask } from "./Confirm";
import { AppCtx, BootstrapContext, Company, IdentityContext, useApp, useCan, type After, type Ctx } from "./context";
import { Facturi } from "./Facturi";
import { Home } from "./Home";
import { PaymentFileSheet, Plati } from "./Plati";
import { Rapoarte } from "./Rapoarte";
import { CoMenu, HomeDesk, Spine, SpineSkeleton, navCounts, updateDot, useWide } from "./Desk";
import { OrderSheet, DocSheet, InvoiceSheet, IssuedSheet, MailSheet } from "./Sheets";
import { PartnerSheet } from "./Partners";
import { Products } from "./Products";
import { listCountStyle } from "./Paging";
import { TxSheet } from "./TxSheet";
import { NewInvoiceSheet } from "./Invoicing";
import { IntegrationSheet } from "./Settings";
import { SearchButton } from "./Search";
import { Setup } from "./Setup";
import { Automatizari } from "./Automatizari";
import { MORE_TABS, MaiMult, NewMenu, Sarcini, Setari } from "./Tabs";
import { Tranzactii } from "./Tranzactii";
import { I, L, Spinner, titleStyle, typing } from "./ui";

const PAL_KEY = "fiscalm:pal"; // the last colour scheme, for the first paint (layout.tsx)

/** The phone's page: one column between the top bar and the tab bar. */
/** Signing out while the install cannot be read: below the notice, at a readable width. */
const signOutStyle = "mx-auto my-4 flex max-w-[420px] flex-col gap-2 px-4";
/** The desktop page beside the column, in the app and in its skeleton (ShellSkeleton), so both start in the same place. */
const deskMain = "ml-[var(--sp)] min-h-screen bg-canvas px-[30px] pt-[38px] pb-8";
const pageStyle = "page mx-auto max-w-[560px] px-3.5 pt-[calc(12px+env(safe-area-inset-top))] pb-[calc(var(--tabbar)+env(safe-area-inset-bottom)+40px)] min-[1000px]:max-w-[1040px]";
/** The desktop column's width (Desk.tsx Spine), for the page beside it and for the toast, which centres on that page. */
const spine = "[--sp:297px]";
/** The phone's floating nav: three pieces over the page, which scrolls under them, fitting a 320px screen (48px rounds,
 * 44px tabs). Full circles and a capsule here only,
 * the one exception to the radius scale. */
/** The floating nav's row, shared with its skeleton (ShellSkeleton) so the two stay in the same place. */
const dockBar = "pointer-events-none fixed inset-x-2 bottom-[calc(8px+env(safe-area-inset-bottom))] z-[7] mx-auto flex h-14 max-w-[560px] items-center justify-between gap-1";
const dockPiece = "pointer-events-auto border border-solid border-dock-line bg-dock/95 shadow-pop backdrop-blur-md";
const dockRound = "grid size-12 shrink-0 cursor-pointer place-items-center rounded-full p-0 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus [&_svg]:size-[22px] disabled:cursor-default disabled:opacity-40";
const tabSlot = "relative grid size-11 place-items-center rounded-full text-muted no-underline hover:text-ink aria-[current=page]:text-brand focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-focus [&_svg]:size-[22px]";

/** A count on a tab's icon (Sarcini, Tranzacții): in its tab's top right corner, never over the next tab, and not a tap target. */
const badgeStyle = "pointer-events-none absolute top-0 right-0 grid h-5 min-w-5 place-items-center rounded-full bg-action px-1 text-xs font-bold leading-none text-action-ink tabular-nums ring-2 ring-dock";
/** A top bar link on its own page (Mai mult, Setări). */
const topCurrent = "aria-[current=page]:bg-selected aria-[current=page]:text-ink";

const LOAD_FAILED = "Datele nu au putut fi încărcate. Încearcă din nou.";
const OFFLINE = "Serverul nu răspunde. Verifică conexiunea la internet și încearcă din nou.";

/** The phone's top bar: the company, Mai mult (current on the sections it lists) and the Setări gear. */
function Top() {
  const { route, view } = useApp();
  const more = MORE_TABS.includes(route.tab);
  return (
    <header className="top mb-6 flex items-center gap-1 border-0 border-b border-solid border-line pb-3">
      <CoMenu bar />
      <L to={{ tab: "mai-mult" }} className={`${iconButtonStyle} ${buttonTone.quiet} ${topCurrent}`} label="Mai mult" current={more}>
        <I n={more ? "list:bold" : "list"} />
      </L>
      <L to={{ tab: "setari" }} className={`${iconButtonStyle} ${buttonTone.quiet} relative ${topCurrent}`} label="Setări" current={route.tab === "setari"}>
        <I n={route.tab === "setari" ? "gear-six:fill" : "gear-six"} />
        {parts(view)[0]?.system.updates?.release && <span className={`update-dot ${updateDot} absolute top-2 right-2`} role="img" aria-label="Versiune nouă disponibilă" />}
      </L>
    </header>
  );
}

/** The phone's floating nav: Caută on the left; in a capsule the daily work, icons only (Acasă, Sarcini, Cheltuieli,
 * Tranzacții), Sarcini and Tranzacții with their counts (Desk.tsx navCounts); Nou on the right. It floats --tabbar above
 * the safe area's edge, as wide as the page's column at most (globals.css scrolls a focused element clear of it; the page's padding
 * keeps its last row clear). */
function TabBar() {
  const { route, view } = useApp();
  const t = route.tab;
  const n = navCounts(view);
  const { box, mark } = useMarker<HTMLElement>(":scope > [aria-current=page]", t);
  const slot = (to: Tab, icon: string, label: string, count = 0, what = "") => (
    <L to={{ tab: to }} className={tabSlot} current={t === to} label={count ? `${label}, ${what}` : label}>
      <I n={icon + (t === to ? ":fill" : "")} />
      {count ? <span className={badgeStyle} aria-hidden="true">{count > 99 ? "99+" : count}</span> : null}
    </L>
  );
  return (
    <div className={`tabs ${dockBar}`}>
      <SearchButton className={`${dockPiece} ${dockRound} text-ink`}><I n="magnifying-glass" /><span className="sr-only">Caută</span></SearchButton>
      <nav ref={box} className={`${dockPiece} relative flex items-center gap-0.5 rounded-full p-1`} aria-label="Secțiuni">
        <span ref={mark} className={`${markerStyle} rounded-full bg-dock-selected`} aria-hidden="true" />
        {slot("home", "house", "Acasă")}
        {slot("sarcini", "list-checks", "Sarcini", n.sarcini, nOf(n.sarcini, "una deschisă", "deschise"))}
        {slot("cheltuieli", "receipt", "Cheltuieli")}
        {slot("tranzactii", "bank", "Tranzacții", n.tranzactii, `${n.tranzactii} de explicat`)}
      </nav>
      <NewMenu className={`plus pointer-events-auto border-0 bg-action text-action-ink shadow-pop hover:opacity-90 ${dockRound}`}><I n="plus" /></NewMenu>
    </div>
  );
}

/** What a list page holds above and in its card, so the shapes before its firm's state is read stand where it will:
 * `button` beside the title (`tight`: the title keeps its margin, Tranzacții), page `tabs`, `filters`, a `month` select and
 * the search field, the card's `count` line and its column header (`header`, from 1100px), and Tranzacții's side column. */
type PageShape = { button?: boolean; tight?: boolean; tabs?: number; filters?: number; month?: boolean; search?: boolean; count?: boolean; header?: boolean; aside?: boolean };
const PAGE_SHAPES: Partial<Record<Tab, PageShape>> = {
  cheltuieli: { button: true, tabs: 2, filters: 3, month: true, search: true, count: true, header: true },
  vanzari: { button: true, filters: 3, month: true, search: true, count: true, header: true },
  parteneri: { button: true, filters: 3, search: true },
  tranzactii: { button: true, tight: true, filters: 4, month: true, search: true, count: true, aside: true },
  plati: { tight: true },
};
const control = "h-11 min-[1100px]:h-[42px]";
const noStore = () => () => {};
/** The tab the address names, once the page runs: the prerendered page doesn't know it, so it draws the plain shapes. */
const useAddressTab = () => useSyncExternalStore(noStore, () => parse(location.search).tab, () => null);

/** What a page shows until its firm's state is read: its title, its tools and a card of rows, as shapes in their places. */
function PageSkeleton() {
  const tab = useAddressTab();
  const shape = tab ? PAGE_SHAPES[tab] : undefined;
  if (!shape) return <>
    <Skeleton className="mt-1 mb-6 h-8 w-44" />
    <div className={`${surfaceStyle} rounded-panel!`}><SkeletonRows rows={6} quiet /></div>
  </>;
  const list = <>
    {shape.filters ? <div className="my-4 flex flex-wrap gap-1">{Array.from({ length: shape.filters }, (_, i) => <Skeleton key={i} className={`${control} w-20 rounded-control`} />)}</div> : null}
    {shape.search ? <div className="mb-4 flex min-w-0 flex-col gap-2 sm:flex-row">
      {shape.month ? <Skeleton className={`${control} rounded-field sm:w-52`} /> : null}<Skeleton className={`${control} min-w-0 rounded-full sm:flex-1`} />
    </div> : null}
    <div className={`${surfaceStyle} rounded-panel!`}>
      {shape.count ? <p className={listCountStyle}><Skeleton className="h-3.5 w-24" /></p> : null}
      {shape.header ? <div className="hidden min-h-[42px] items-center border-0 border-b border-solid border-line px-4 min-[1100px]:flex"><Skeleton className="h-3 w-1/2" /></div> : null}
      <SkeletonRows rows={6} quiet />
    </div>
  </>;
  return <div className={shape.aside ? "grid min-w-0 grid-cols-1 gap-x-6 xl:grid-cols-[minmax(0,1fr)_300px]" : ""}>
    <div className={`col-span-full flex flex-wrap items-center justify-between ${shape.tight ? "gap-3" : "gap-x-4 gap-y-2"}`}>
      <div className={`${titleStyle} ${shape.tight ? "" : "mb-0!"}`}><Skeleton className="inline-block! h-[0.85em] w-44 align-middle" /></div>
      {shape.button ? <Skeleton className={`${controlHeight} w-28 rounded-full`} /> : null}
    </div>
    {shape.tabs ? <div className={pageTabsStyle}>{Array.from({ length: shape.tabs }, (_, i) => <span key={i} className="inline-flex min-h-11 items-center min-[1100px]:min-h-[42px]"><Skeleton className="h-3.5 w-20" /></span>)}</div> : null}
    {shape.aside ? <div className="min-w-0">{list}</div> : list}
  </div>;
}

/** The app before it knows who is signed in or what the firm's state is (the first paint, a firm being read): the phone's top
 * bar and floating nav, or the desktop column, in their places, with shapes where the data goes. Nothing in it can be
 * pressed and nothing in it is an account's: no sign-in, no Ieși din cont, no firm, no counts, no Nou. Wide or narrow by
 * CSS, so the prerendered page is right before any script runs. */
export function ShellSkeleton() {
  return (
    <div aria-busy="true">
      <p role="status" className="sr-only">Se încarcă…</p>
      <div className={`hidden min-[1100px]:block ${spine}`}>
        <SpineSkeleton />
        <div className={deskMain}><div className="mx-auto w-full max-w-[1440px] min-w-0"><PageSkeleton /></div></div>
      </div>
      <div className="min-[1100px]:hidden">
        <div className={pageStyle}>
          <div className="mb-6 flex items-center gap-1 border-0 border-b border-solid border-line pb-3 text-muted" aria-hidden="true">
            <span className="flex min-h-11 min-w-0 flex-1 items-center gap-2 px-3 py-2">
              <Skeleton className="size-[38px]" /><span className="flex flex-col gap-1.5"><Skeleton className="h-3.5 w-20" /><Skeleton className="h-3 w-28" /></span>
            </span>
            <span className={`${iconButtonStyle} ${buttonTone.quiet}`}><I n="list" /></span>
            <span className={`${iconButtonStyle} ${buttonTone.quiet}`}><I n="gear-six" /></span>
          </div>
          <PageSkeleton />
        </div>
        <div className={`${dockBar} text-muted`} aria-hidden="true">
          <span className={`${dockPiece} ${dockRound}`}><I n="magnifying-glass" /></span>
          <span className={`${dockPiece} flex items-center gap-0.5 rounded-full p-1`}>
            {["house", "list-checks", "receipt", "bank"].map((icon) => <span key={icon} className={tabSlot}><I n={icon} /></span>)}
          </span>
          <span className={`${dockPiece} ${dockRound}`}><I n="plus" /></span>
        </div>
      </div>
    </div>
  );
}

/** One message at a time, gone after 5 seconds (keyed by the caller, so a new message restarts it). */
function Toast({ text }: { text: string }) {
  const [shown, setShown] = useState(true);
  useEffect(() => {
    const t = setTimeout(() => setShown(false), 5000);
    return () => clearTimeout(t);
  }, []);
  return shown ? <div className={`toast ${toastStyle}`}>{text}</div> : null;
}

const NOT_SENT = "Fără conexiune la internet. Modificarea nu a fost trimisă.";
const toastStyle = "fixed left-1/2 top-[calc(14px+env(safe-area-inset-top))] z-10 w-max max-w-[calc(100%-24px)] -translate-x-1/2 rounded-card bg-ink px-4 py-3 text-sm font-semibold text-canvas shadow-xl min-[1100px]:top-auto min-[1100px]:bottom-6 min-[1100px]:left-[calc(50%+var(--sp,0px)/2)] min-[1100px]:max-w-[calc(100%-var(--sp,0px)-48px)]";

/** A POST without an answer: what may have happened stays on screen, over any sheet (outside its inert background), with the
 * way to read the state again and see whether it was recorded. Nothing is sent again. */
function NoAnswer({ reload }: { reload: () => Promise<void> }) {
  const [reading, setReading] = useState(false);
  // An open sheet keeps Tab inside itself: the way to check takes focus, so a keyboard reaches it too.
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => button.current?.focus({ preventScroll: true }), []);
  const again = () => { setReading(true); void reload().finally(() => setReading(false)); };
  return createPortal(
    <div role="status" aria-live="assertive" className={`${toastStyle} flex flex-wrap items-center gap-3`}>
      <span className="min-w-0 flex-1 basis-56">{NO_ANSWER}</span>
      <Button ref={button} type="button" variant="secondary" disabled={reading} aria-busy={reading || undefined} onClick={again}>
        {reading ? <Spinner /> : null}{reading ? "Se reîncarcă…" : "Reîncarcă datele"}
      </Button>
    </div>, document.body);
}

const BODY: Record<Tab, ComponentType> = {
  home: Home, sarcini: Sarcini, vanzari: Facturi, cheltuieli: Facturi, plati: Plati, tranzactii: Tranzactii, parteneri: Facturi, produse: Products, rapoarte: Rapoarte,
  automatizari: Automatizari, setari: Setari, "mai-mult": MaiMult,
};

export default function App() {
  const initial = useContext(BootstrapContext);
  const session = useContext(IdentityContext);
  const entered = useRef<string | null>(null);
  const currentSession = useRef(session);
  useLayoutEffect(() => { currentSession.current = session; }, [session]);
  const canManageCompanies = useCan("manage_firms");
  const canWrite = useCan("member"), canIntegrate = useCan("manage_integrations");
  const [route, go] = useRoute();
  const [views, setViews] = useState<Partial<Record<CoView, View | SetupState>>>(() => initial?.state ? { [isSetup(initial.state) ? parse(window.location.search).co || "ALL" : initial.state.co]: initial.state } : {});
  // Why the last read failed, per view: "" when it did not.
  const [errors, setErrors] = useState<Partial<Record<CoView, string>>>({});
  const [busy, setBusy] = useState(false);
  const inflight = useRef(false); // busy for taps in the same tick, before React re-renders, and while a question is open
  const { ask, cancel, dialog } = useConfirm();
  // A navigation (Back, a link) takes away what the open question is about: it is declined, never sent later.
  const place = route ? JSON.stringify(route) : "";
  useEffect(() => cancel, [place, cancel]);
  // Every read and every POST takes a number per view; a read answers only if nothing started after it, so an
  // older GET never overwrites a POST's state or a newer GET's.
  const seq = useRef<Record<CoView, number>>({});
  const next = (c: CoView) => (seq.current[c] = (seq.current[c] ?? 0) + 1);
  const [toast, setToast] = useState<{ text: string; n: number } | null>(null);
  const [gone, setGone] = useState(() => {
    if (!initial?.state || isSetup(initial.state)) return "";
    const selected = parse(window.location.search).co;
    return selected && selected !== "ALL" && !initial.state.companies.some(c => c.key === selected) ? selected : "";
  });
  // The view whose last POST got no answer: its notice stays until the person reads the state again.
  const [lost, setLost] = useState<CoView | null>(null);
  // No company known yet (first visit, or the remembered one is gone): read ALL to learn them, then pick one.
  const co = route ? (route.co && route.co !== gone ? route.co : "ALL") : undefined;
  const wide = useWide();
  const requestPage = JSON.stringify(route);
  // A replaced page/layout cannot receive a one-time result, so its waiting confirmation is cancelled.
  useLayoutEffect(() => () => { window.dispatchEvent(new Event(CANCEL_REAUTHENTICATION)); }, [requestPage, wide]);

  const say = useCallback((text: string) => setToast((t) => ({ text, n: (t?.n ?? 0) + 1 })), []);

  const load = useCallback((c: CoView): Promise<View | SetupState | null> => {
    const n = next(c);
    return getBootstrap(c).then(
      (result) => {
        if (seq.current[c] !== n) return null;
        if (!session || identityKey(result.identity) !== identityKey(currentSession.current?.identity ?? session.identity)) {
          void session?.refreshIdentity().catch(() => {});
          return null;
        }
        const s = result.state;
        if (!s) return null;
        if (!isSetup(s) && s.co !== c) { forgetCo(); setGone(c); return null; }
        setViews((x) => ({ ...x, [c]: s }));
        setErrors((x) => ({ ...x, [c]: "" }));
        return s;
      },
      (e) => {
        if (seq.current[c] !== n) return null;
        if (e instanceof ApiError && (e.status === 400 || e.status === 404) && c !== "ALL") {
          // A company the server no longer has (a link or a remembered choice): start again from all of them.
          forgetCo();
          setGone(c);
          return null;
        }
        setErrors((x) => ({ ...x, [c]: e instanceof ApiError && e.status === 0 ? OFFLINE : LOAD_FAILED }));
        return null;
      },
    );
  }, [session]);

  // The engine's redirect after a stale download brings a message in ?m=: a toast, then gone from the URL.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const m = q.get("m");
    if (!m) return;
    q.delete("m");
    window.history.replaceState(window.history.state, "", `${window.location.pathname}?${q}${window.location.hash}`);
    // The URL is only readable after hydration (a static export prerenders without it), hence the effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    say(m);
  }, [say]);

  useEffect(() => {
    if (!co) return;
    rememberCo(co);
    if (entered.current === co) return;
    const seeded = entered.current === null && initial?.state && (isSetup(initial.state) || initial.state.co === co);
    entered.current = co;
    if (!seeded) void load(co);
  }, [co, load, initial]);

  // Coming back to the app (from the bank's app, e-mail) or back online: read the state again.
  useEffect(() => {
    if (!co) return;
    const f = () => document.visibilityState === "visible" && load(co);
    document.addEventListener("visibilitychange", f);
    window.addEventListener("online", f);
    return () => { document.removeEventListener("visibilitychange", f); window.removeEventListener("online", f); };
  }, [co, load]);

  // Signing in again (after an expired session) reads the identity anew; a state read that failed meanwhile is retried.
  const identity = useContext(IdentityContext)?.identity, failed = co ? !!errors[co] : false;
  const knownIdentity = useRef(identity);
  useEffect(() => {
    if (knownIdentity.current === identity) return;
    knownIdentity.current = identity;
    if (co && (failed || !views[co])) void load(co);
  }, [identity, co, failed, load, views]);

  const shown = co ? views[co] : undefined;
  const view = isSetup(shown) ? undefined : shown;
  const companies = view?.companies;
  // Cont picks its frame once, when the state or its read error first arrives, and keeps it until the route leaves Cont or the firm changes:
  // switching frames would remount it and lose a one-time answer (recovery codes) or a pending request.
  const onCont = route?.tab === "setari" && route.sec === "cont";
  const [kept, keep] = useState<{ co: CoView; frame: "bare" | "app" } | null>(null);
  const frame = !onCont || !co ? null : kept?.co === co ? kept.frame
    : !shown && !errors[co] ? null : !view || isSetup(shown) ? "bare" : "app";
  const chosen = frame && co ? { co, frame } : null;
  if (chosen?.co !== kept?.co || chosen?.frame !== kept?.frame) keep(chosen);

  // Pick a company when the route names none, or names one that is gone; ALL with a single company is that company.
  useEffect(() => {
    if (!route || !companies?.length) return;
    const one = companies.length === 1 || !BOTH.includes(route.tab);
    // Every correction keeps the section the link points at (#fisier, #pachet).
    const keep = { replace: true, at: window.location.hash.slice(1) || undefined };
    if (route.co === "" || route.co === gone || route.co !== "ALL" && !companies.some(c => c.key === route.co)) go({ ...route, co: one ? companies[0].key : "ALL" }, keep);
    else if (route.co === "ALL" && companies.length === 1) go({ ...route, co: companies[0].key }, keep);
    // A page that needs one company, opened under Toate firmele, shows the last one chosen and names it in the URL.
    else if (route.co !== "ALL" && coKey(new URLSearchParams(window.location.search).get("co")) === "ALL") go({ ...route }, keep);
  }, [route, companies, gone, go]);

  // Company colours, title. The status bar is the page's background (layout.tsx).
  useEffect(() => {
    if (!route || !companies?.length) return;
    const c = companies.find((x) => x.key === route.co);
    const p = pal((c ?? companies[0]).palette);
    document.documentElement.dataset.pal = p;
    document.title = pageTitle(route, c?.name);
    try {
      localStorage.setItem(PAL_KEY, p);
    } catch {
      // private mode: the first paint uses the default colours
    }
  }, [route, companies]);

  // A saved link with a section (?tab=plati#fisier): scroll there once the first state is on screen.
  const ready = !!view;

  // Another page the user asked for (a tab, a Setări page, a report, a search result): focus moves to its h1, so a keyboard or
  // screen reader starts at the top of what changed instead of on a link that is gone. Never on the first page, never on back
  // or forward or an action's redirect (its toast speaks), never from under a sheet or out of a field the user is typing in.
  const pageNow = route ? pageKey(route) : "";
  const shownPage = useRef("");
  const sheetOpen = !!(route && openSheet(route));
  useEffect(() => {
    if (!ready || !pageNow) return;
    const was = shownPage.current;
    const h1 = document.querySelector<HTMLElement>("main h1");
    // Until the page's company is read, its h1 isn't on screen: the page counts as shown once it is.
    if (!h1 || pageNow === was) return;
    shownPage.current = pageNow;
    if (takeFocus(pageNow) && was && !sheetOpen && !typing(document.activeElement)) h1.focus({ preventScroll: true });
  });
  // Another page fades in where the last one was; the first one fades in over the shapes it replaces.
  const faded = useRef("");
  useLayoutEffect(rowsStay, [pageNow, co]);
  useLayoutEffect(() => {
    if (!ready || !pageNow) return;
    if (faded.current && faded.current !== pageNow) replay(document.getElementById("main-content"));
    faded.current = pageNow;
  }, [ready, pageNow]);
  useEffect(() => {
    if (ready && window.location.hash) document.getElementById(window.location.hash.slice(1))?.scrollIntoView();
  }, [ready]);

  const run = useCallback(async <R extends ActionResult>(c: Co, send: () => Promise<R>, question?: Ask,
    after?: After<R>): Promise<R | null> => {
    if (!route || inflight.current) return null;
    // Offline the request would fail anyway: it is not sent, and the person is told so for certain.
    if (!navigator.onLine) {
      say(NOT_SENT);
      return null;
    }
    inflight.current = true;
    if (question && !(await ask(question))) {
      inflight.current = false;
      return null;
    }
    setBusy(true);
    const shown = co ?? route.co;
    next(c);
    next(shown);
    try {
      const r = await send();
      // An earlier lost answer goes away only when the state of its own view arrives, which then shows what was recorded.
      const seen = (v: CoView) => setLost((x) => (x === v ? null : x));
      if (r.state) {
        if (r.ok) rowsMayLeave();
        next(c);
        setViews((x) => ({ ...x, [c]: r.state! }));
        setErrors((x) => ({ ...x, [c]: "" }));
        seen(c);
      }
      // Toate firmele shows both companies: read it again; so does a company whose new state did not come back.
      if (r.ok) rowsMayLeave();
      if (shown !== c || !r.state) void load(shown).then((v) => { if (v) seen(shown); });
      if (r.message) say(r.message);
      if (!r.ok) return r;
      const sheet = openSheet(route);
      const to = after?.(r);
      if (to === false) {
        // stay: the caller keeps its sheet open
      } else if (to) {
        go(to, { replace: !!sheet });
      } else if (r.next) {
        go({ co: c, tab: r.next.tab as Tab }, { at: r.next.at ?? undefined });
      } else if (sheet) {
        go({ ...route, ...NO_SHEET }, { replace: true });
      }
      return r;
    } catch {
      // It may have been recorded: never sent again by itself; the notice stays until the state is read again.
      setLost(shown);
      void load(shown);
      return null;
    } finally {
      inflight.current = false;
      setBusy(false);
    }
  }, [route, co, go, say, load, ask]);

  // An action's `done` is its caller's to read.
  const act = useCallback(async (c: Co, action: Action, body: Record<string, string | number>, question?: Ask) => {
    await run(c, () => post(action, { ...body, co: c }), question);
  }, [run]);

  // What the route shows when the read starts, not when the caller began: an upload may finish after a firm switch.
  const shownNow = useRef(co);
  useEffect(() => { shownNow.current = co; }, [co]);
  const refresh = useCallback(() => {
    const c = shownNow.current;
    return c ? load(c).then((v) => (isSetup(v) ? null : v)) : Promise.resolve(null);
  }, [load]);
  // Non-null whenever route and view are, the only case that renders the provider.
  const ctx = useMemo<Ctx | null>(() => (route && view ? { view, route, go, act, run, busy, refresh, say } : null),
    [route, view, go, act, run, busy, refresh, say]);

  const close = useCallback(() => route && go({ ...route, ...NO_SHEET }), [go, route]);

  if (!route || !co) return <ShellSkeleton />;
  const error = co ? errors[co] : "";
  if (onCont && !frame) return <ShellSkeleton />;
  if (frame === "bare") return <main className={pageStyle}><UserAccount back={`/?co=${encodeURIComponent(co)}&tab=setari`} /></main>;
  if (isSetup(shown)) {
    return (
      <Setup s={shown}
        onAdded={(key, s) => {
          // A read sent before the add would bring the setup state back: it no longer answers.
          for (const c of new Set<CoView>(["ALL", co, ...(Object.keys(seq.current) as CoView[])])) next(c);
          // No view may still hold the setup state: the company may be the one the URL already names.
          setViews((x) => {
            const y = Object.fromEntries(Object.entries(x).filter(([, v]) => !isSetup(v)));
            return s ? { ...y, ALL: s } : y;
          });
        }}
        onFinish={(key, message) => {
          go({ co: key, tab: "home" }, { replace: true });
          void load(key);
          if (message) say(message);
        }}
        onStale={() => void load(co)} />
    );
  }
  if (!view) {
    return error ? (
      <div className={pageStyle}>
        <div className={bannerStyle} role="alert">
          <b>{error}</b>
          <Button type="button" className="btn g" onClick={() => co && load(co)}>Reîncearcă</Button>
        </div>
        <div className={signOutStyle}><SignOut /></div>
      </div>
    ) : <ShellSkeleton />;
  }
  // Until a company is picked (the effect above), a tab that shows one company has nothing to draw.
  if (co !== route.co || (view.co === "ALL" && !BOTH.includes(route.tab))) return <ShellSkeleton />;
  const Body = wide && route.tab === "home" ? HomeDesk : BODY[route.tab];
  const page = <main id="main-content" tabIndex={-1} className={`min-w-0 scroll-mt-4 ${motion.fade}`}>{route.tab === "setari" && route.sec === "cont" ? <UserAccount /> : <Body />}</main>;
  const notes = (
    <>
      {!view.live ? (
        <p className={bannerStyle}>
          <strong>Sandbox:</strong> facturile se trimit doar la ANAF Test.
          {canManageCompanies ? <> <L to={{ tab: "setari", sec: "mod" }} className="whitespace-nowrap font-semibold text-ink">Treci în Live</L></> : null}
        </p>
      ) : null}
      {error ? (
        <p className="stale mx-0.5 mt-2.5 mb-0 flex flex-wrap items-center gap-2.5 text-[13px] text-muted" role="status">
          Datele nu au putut fi actualizate și pot fi vechi.
          <Button type="button" variant="quiet" onClick={refresh}>Reîncearcă</Button>
        </p>
      ) : null}
      {isAll(view) ? page : <Company s={view}>{page}</Company>}
    </>
  );
  return (
    <AppCtx.Provider value={ctx}>
      <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-control focus:bg-surface focus:p-3">Mergi la conținut</a>
      <div className={spine} role="status" aria-live="polite">{toast ? <Toast key={toast.n} text={toast.text} /> : null}</div>
      {lost ? <NoAnswer reload={() => load(lost).then((v) => { if (v) setLost(null); })} /> : null}
      {wide ? (
        <div className={`dk ${spine}`}>
          <Spine />
          <div className={`dk-main ${deskMain}`}><div className="mx-auto w-full max-w-[1440px] min-w-0">{notes}</div></div>
        </div>
      ) : (
        <>
          <div className={pageStyle}>
            <Top />
            {notes}
          </div>
          <TabBar />
        </>
      )}
      {route.ord ? <OrderSheet id={route.ord} onClose={close} />
        : route.set ? <SettlementSheet id={route.set} onClose={close} />
        : route.tx ? <TxSheet key={route.tx} id={route.tx} onClose={close} />
        : route.inv ? <InvoiceSheet id={route.inv} onClose={close} />
          : route.iss ? <IssuedSheet key={route.iss} id={route.iss} onClose={close} />
            : route.mail ? <MailSheet key={route.mail} mailKey={route.mail} onClose={close} />
              : route.doc ? <DocSheet id={route.doc} onClose={close} />
                : route.p ? <PartnerSheet id={route.p} onClose={close} />
                : route.pf && !isAll(view) ? <Company s={view}><PaymentFileSheet key={route.pf} id={route.pf} onClose={close} /></Company>
                  : route.new && canWrite && !isAll(view) ? <Company s={view}><NewInvoiceSheet id={route.new} onClose={close} /></Company>
                    : route.int && canIntegrate && route.tab === "setari" && !isAll(view)
                      ? <Company s={view}><IntegrationSheet id={route.int} onClose={close} /></Company> : null}
      {dialog}
    </AppCtx.Provider>
  );
}
