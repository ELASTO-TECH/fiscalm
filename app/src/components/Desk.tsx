"use client";
// The desktop layout (a wide window): the navigation column on the left, the page on the right. Acasă there is the phone's
// Acasă in columns; the other tabs keep their mobile components.
import { useEffect, useRef, useSyncExternalStore } from "react";
import { andList, cap, dmt, nOf } from "@/lib/format";
import { BOTH, type Tab } from "@/lib/nav";
import { PALETTES, pal } from "@/lib/palette";
import { Skeleton, buttonStyle, buttonTone, markerStyle, motion, useMarker } from "./Controls";
import { Brand } from "./Brand";
import { FirmMark } from "./CompanyLogo";
import { isAll, parts, type Companies, type CoView, type View } from "@/lib/types";
import { useApp } from "./context";
import { Dashboard } from "./Dashboard";
import { Attention, HomeTitle, QuickActions, homeTasks } from "./Home";
import { SearchButton } from "./Search";
import { NewMenu } from "./Tabs";
import { SetupCard } from "./SetupCard";
import { I, L } from "./ui";

const WIDE = "(min-width: 1100px)";

function subscribe(cb: () => void) {
  if (typeof window.matchMedia !== "function") return () => {};
  const m = window.matchMedia(WIDE);
  m.addEventListener("change", cb);
  return () => m.removeEventListener("change", cb);
}

/** True in a window wide enough for the desktop layout (false while prerendering and where matchMedia is missing). */
export function useWide(): boolean {
  return useSyncExternalStore(subscribe, () => typeof window.matchMedia === "function" && window.matchMedia(WIDE).matches,
    () => false);
}

/** "A, B și C": the names of a few companies, or their number. */
export function names(cs: Companies): string {
  if (cs.length > 3) return nOf(cs.length, "o firmă", "firme");
  return andList(cs.map((c) => c.name));
}

/** A company's mark: its first letter, and its first digits if the name has any (ACME: A, STUDIO9: S9). */
export const markOf = (name: string): string =>
  (name.match(/[\p{L}\p{N}]/u)?.[0] ?? "?").toUpperCase() + (name.match(/\d+/)?.[0]?.slice(0, 2) ?? "");

/** The legal forms a legal name may end with, as words (S.R.L. is the words s, r, l). */
const FORMS = [["s", "r", "l"], ["s", "r", "l", "d"], ["s", "a"], ["srl"], ["srld"], ["sa"], ["pfa"], ["ii"], ["if"]];
/** A name's words, lowercased, without a trailing legal form: "Studio 9 SRL" and "STUDIO9" both read "studio9". */
const bare = (x: string): string => {
  const w = x.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const form = FORMS.find((f) => w.length > f.length && f.every((t, i) => w[w.length - f.length + i] === t));
  return (form ? w.slice(0, -form.length) : w).join("");
};
/** A company's legal name as its subtitle, only when it says more than its name (not "ACME S.R.L." under "ACME SRL";
 * still "CA SRL" under "Casa"). */
export const says = (legal: string, name: string) => (bare(legal) === bare(name) ? "" : legal);

/** Name, subtitle, mark and its colours (a company's palette, or all of theirs) for a company or for all of them. */
function look(cs: Companies, co: CoView) {
  const c = cs.find((x) => x.key === co);
  if (c) {
    return { name: c.name, sub: says(c.legal_name, c.name), mark: markOf(c.name), logo: c.logo, pal: pal(c.palette), style: undefined };
  }
  const accs = cs.slice(0, 4).map((x) => PALETTES[pal(x.palette)].acc);
  const stops = accs.map((a, i) => `${a} ${(i * 100) / accs.length}% ${((i + 1) * 100) / accs.length}%`).join(", ");
  return {
    name: "Toate firmele", sub: names(cs), pal: undefined, logo: null,
    mark: cs.length === 2 ? cs.map((x) => markOf(x.name)[0]).join("+") : String(cs.length),
    style: { background: `linear-gradient(135deg, ${stops})`, color: "#14110c" },
  };
}

/** One line of the company switch: its button and each choice in its menu. */
const choice = "flex min-h-11 items-center gap-2 rounded-field px-3 py-2 text-ink hover:bg-inset focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";
/** A company's mark (FirmMark): its logo, or its letters on its colour (`--co`), the one place the company's palette shows. */
const markStyle = "size-[38px] text-xs";
/** A name on one line, cut with an ellipsis (the whole name in its title), and its subtitle as one muted line. */
const nameStyle = "flex min-w-0 flex-col leading-tight [&_b]:truncate [&_b]:text-[13px] [&_b]:leading-5 [&_b]:font-normal [&_small]:truncate [&_small]:text-[13px] [&_small]:leading-5 [&_small]:text-muted";

/** The company switch: the current company, a menu with the others. A dot on it when another company has late
 * payments, so they show without opening it. Toate firmele from a tab that shows one company leads to Acasă. */
export function CoMenu({ bar = false }: { bar?: boolean }) {
  const { route, view } = useApp();
  const ref = useRef<HTMLDetailsElement>(null);
  const cs = view.companies;
  const late = Object.fromEntries(cs.map((c) => [c.key, c.late]));
  const lateElsewhere = route.co === "ALL" ? [] : cs.filter((c) => c.key !== route.co && c.late).map((c) => c.name);
  const shut = () => ref.current?.removeAttribute("open");
  // A choice closes the menu; focus goes back to its button rather than staying on a hidden link.
  const chose = () => {
    shut();
    ref.current?.querySelector("summary")?.focus();
  };
  useEffect(() => {
    const f = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) shut();
    };
    const k = (e: KeyboardEvent) => {
      if (e.key === "Escape" && ref.current?.open) {
        shut();
        ref.current.querySelector("summary")?.focus();
      }
      // Tab out of the open menu closes it, so it doesn't stay over the page; a click elsewhere does the same above.
      if (e.key === "Tab" && ref.current?.open) {
        setTimeout(() => {
          if (ref.current && !ref.current.contains(document.activeElement)) shut();
        });
      }
    };
    document.addEventListener("click", f);
    document.addEventListener("keydown", k);
    return () => {
      document.removeEventListener("click", f);
      document.removeEventListener("keydown", k);
    };
  }, []);
  const options: CoView[] = [...cs.map((c) => c.key), ...(cs.length > 1 ? ["ALL"] : [])];
  const me = look(cs, route.co);
  const across = lateElsewhere.length > 3 ? nOf(lateElsewhere.length, "o firmă", "firme") : names(cs.filter((c) => lateElsewhere.includes(c.name)));
  return (
    <details className={`comenu relative ${bar ? "min-w-0 flex-1" : ""}`} ref={ref}>
      {/* The name starts with what the button shows (its name and subtitle), so a voice command that says them finds it. */}
      <summary className={`${choice} list-none cursor-pointer [&::-webkit-details-marker]:hidden [[open]>&]:bg-inset ${bar ? "w-max max-w-full pr-3" : "min-h-[67px] border border-solid border-line bg-surface"} [&>svg]:size-4 [&>svg]:text-muted ${bar ? "" : "[&>svg]:ml-auto"}`} title={me.name} aria-label={`${[me.name, me.sub].filter(Boolean).join(", ")}, alege firma${lateElsewhere.length ? `, plăți restante la ${across}` : ""}`}>
        <FirmMark co={route.co} logo={me.logo} letters={me.mark} pal={me.pal} style={me.style} className={`${markStyle}${route.co === "ALL" ? " text-[11px]" : ""}`}>{lateElsewhere.length ? <i className="absolute -top-[3px] -right-[3px] size-2.5 rounded-full bg-danger ring-2 ring-canvas" /> : null}</FirmMark>
        <span className={`nm ${nameStyle}`}><b>{me.name}</b>{me.sub ? <small>{me.sub}</small> : null}</span>
        <I n="caret-down" />
      </summary>
      <div className={`menu ${motion.popOpen} absolute top-[calc(100%+4px)] z-[5] max-h-[70dvh] overflow-y-auto rounded-card border border-solid border-line bg-surface p-1 text-ink shadow-pop ${bar ? "left-0 w-[min(320px,calc(100vw-28px))] origin-top-left" : "inset-x-0 origin-top"}`}>
        {options.map((c) => {
          const o = look(cs, c);
          return (
            <L key={c} to={c === "ALL" && !BOTH.includes(route.tab) ? { co: c, tab: "home" }
              : { co: c, tab: route.tab, side: route.side, f: route.f, per: route.per }} onGo={chose}
              className={`${choice} no-underline [&>svg]:ml-auto [&>svg]:size-4`} current={c === route.co}>
              <FirmMark co={c} logo={o.logo} letters={o.mark} pal={o.pal} style={o.style} className={`${markStyle}${c === "ALL" ? " text-[11px]" : ""}`} />
              <span className={`nm ${nameStyle}`} title={o.name}><b>{o.name}</b>{c !== "ALL" && late[c] ? <small>{cap(nOf(late[c], "o plată restantă", "plăți restante"))}</small> : o.sub ? <small>{o.sub}</small> : null}</span>
              {c === route.co ? <I n="check" /> : null}
            </L>
          );
        })}
      </div>
    </details>
  );
}

const GROUPS: [string, [Tab, string, string][]][] = [
  ["", [["home", "house", "Acasă"], ["sarcini", "list-checks", "Sarcini"]]],
  ["Bani", [["vanzari", "invoice", "Vânzări"], ["cheltuieli", "receipt", "Cheltuieli"], ["plati", "currency-circle-dollar", "Fișiere de plăți"],
    ["tranzactii", "bank", "Tranzacții"]]],
  ["Firma", [["parteneri", "user", "Parteneri"], ["produse", "package", "Produse"], ["rapoarte", "clipboard-text", "Rapoarte"], ["automatizari", "sparkle", "Automatizări"]]],
];

/** A row of the column (Contra's: 38px, 14px text, 16px icon, 12px to the text): flat, a small muted icon, a grey fill on hover and a firmer one on the current page. */
const navItem = "relative flex min-h-[38px] w-full items-center gap-3 rounded-control border-0 bg-transparent px-2.5 py-1.5 font-[inherit] text-sm leading-5 text-ink no-underline cursor-pointer hover:bg-inset aria-[current=page]:font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus [&>svg]:size-4 [&>svg]:text-muted aria-[current=page]:[&>svg]:text-brand";
const spineStyle = "spine fixed inset-y-0 left-0 z-[2] flex w-[var(--sp)] flex-col gap-3 overflow-y-auto border-0 border-r border-solid border-line bg-side px-3 py-5 text-ink";
const groupStyle = "mt-4 mb-1 ml-2.5 flex items-center gap-3 text-[9px] leading-4 font-medium uppercase tracking-[0.12em] text-muted after:h-px after:flex-1 after:bg-line";
/** A new release: a dot beside Setări (here and in the phone's top bar). */
export const updateDot = "inline-block size-[7px] shrink-0 rounded-full bg-current";

/** The counts beside Sarcini (every open task) and Tranzacții (De explicat, one firm only), in this column and on the
 * phone's tab bar. */
export function navCounts(view: View): { sarcini: number; tranzactii: number } {
  return { sarcini: view.task_summary.total, tranzactii: isAll(view) ? 0 : view.transactions?.to_explain ?? 0 };
}

/** The left column, navigation only (the amounts are on Acasă): company, Caută, Nou, the sections with their counts, and
 * Setări with the last check. */
export function Spine() {
  const { route, view } = useApp();
  const ss = parts(view);
  const n = navCounts(view);
  const counts: Partial<Record<Tab, string>> = {
    sarcini: String(n.sarcini || ""), tranzactii: String(n.tranzactii || ""),
    cheltuieli: view.payable.late ? nOf(view.payable.late, "una restantă", "restante") : "",
  };
  const checked = ss.map((s) => s.system.last_check).filter(Boolean).sort().pop();
  const on = route.tab === "setari";
  const { box, mark } = useMarker<HTMLElement>("nav [aria-current=page], .sys[aria-current=page]", route.tab);
  return (
    <aside className={spineStyle} ref={box}>
      <span ref={mark} className={`${markerStyle} rounded-control bg-selected`} aria-hidden="true" />
      <Brand />
      <CoMenu />
      <NewMenu className={`nou ${buttonStyle} ${buttonTone.primary} w-full`}><I n="plus" />Nou</NewMenu>
      <SearchButton className={`find ${navItem} -mb-2 [&_kbd]:ml-auto [&_kbd]:font-[inherit] [&_kbd]:text-xs [&_kbd]:text-muted`} keys><I n="magnifying-glass" />Caută</SearchButton>
      <nav className="flex flex-col gap-0.5" aria-label="Secțiuni">
        {GROUPS.map(([group, items]) => (
          <div key={group} role="group" aria-label={group || undefined} className="flex flex-col gap-0.5">
            {group ? <h2 className={groupStyle} aria-hidden="true">{group}</h2> : null}
            {items.map(([k, icon, label]) => (
              <L key={k} to={{ tab: k }} className={navItem} current={k === route.tab}>
                <I n={icon} />
                {label}
                {counts[k] ? <span className="n ml-auto text-xs font-normal text-muted tabular-nums">{counts[k]}</span> : null}
              </L>
            ))}
          </div>
        ))}
      </nav>
      <L className={`sys ${navItem} mt-auto flex-wrap`} to={{ tab: "setari" }} current={on}>
        <I n="gear-six" />
        Setări
        {ss.some((s) => s.system.updates?.release) && <span className={`update-dot ${updateDot}`} role="img" aria-label="Versiune nouă disponibilă" />}
        {checked ? <small className="w-full pl-7 text-xs font-normal text-muted">Verificat {dmt(checked, view.today)}</small> : null}
      </L>
    </aside>
  );
}

/** The column before the state is read (App.tsx ShellSkeleton): the same rows in the same places, the firm and Nou as shapes,
 * nothing to press, so the real column replaces it without a move. */
export function SpineSkeleton() {
  return (
    <aside className={`${spineStyle} pointer-events-none`} aria-hidden="true">
      <Brand />
      <span className={`${choice} min-h-[67px] border border-solid border-line bg-surface`}>
        <Skeleton className="size-[38px]" /><span className="flex flex-col gap-1.5"><Skeleton className="h-3.5 w-24" /><Skeleton className="h-3 w-32" /></span>
      </span>
      <Skeleton className="h-[38px] w-full rounded-full" />
      <span className={`${navItem} -mb-2`}><I n="magnifying-glass" />Caută</span>
      <span className="flex flex-col gap-0.5">
        {GROUPS.map(([group, items]) => (
          <span key={group} className="flex flex-col gap-0.5">
            {group ? <span className={groupStyle}>{group}</span> : null}
            {items.map(([k, icon, label]) => <span key={k} className={navItem}><I n={icon} />{label}</span>)}
          </span>
        ))}
      </span>
      <span className={`${navItem} mt-auto`}><I n="gear-six" />Setări</span>
    </aside>
  );
}

/** Acasă in a wide window: its title and the quick actions, what is left to set up, the tasks that need a person, then
 * the same numbers as on the phone (Dashboard), laid out in columns. */
export function HomeDesk() {
  const { view } = useApp();
  const { total, brief, urgent } = homeTasks(view);
  return (
    <div className="dk-home">
      <div className="hdr flex flex-col gap-5">
        <HomeTitle />
        <QuickActions />
      </div>
      <SetupCard />
      <Attention brief={brief} all={total} urgent={urgent} />
      <Dashboard />
    </div>
  );
}
