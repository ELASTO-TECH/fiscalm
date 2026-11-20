"use client";
// The building blocks: icons, rows, action buttons, task cards, amounts.
import { Button, Reasons, buttonStyle, buttonTone, rowStyle, rowTextStyle, rowIconStyle, rowRef, SkeletonRows, useReason } from "./Controls";
import {
  ArrowCounterClockwise, ArrowDownLeft, ArrowLeft, ArrowRight, ArrowUpRight, Bank, Calculator, Camera, CaretDown, CaretRight, Check, CheckCircle, ClipboardText, Clock, Copy, CreditCard, CurrencyCircleDollar,
  DownloadSimple, EnvelopeSimple, FileText, Fingerprint, GearSix, HardDrives, House, Image, Invoice, LinkSimple, List, ListChecks, MagnifyingGlass,
  Package, PaperPlaneTilt, PencilSimple, Plugs, Plus, Receipt, SlidersHorizontal, Sparkle, TrayArrowDown, User, WarningCircle, Wrench, X, type Icon,
} from "@phosphor-icons/react";
import { Fragment, useRef, useState, type MouseEvent, type ReactNode, type Ref } from "react";
import { useApp, useCan, useCompany } from "./context";
import type { Sign } from "./derive";
import type { Ask } from "./Confirm";
import { lei, sumParts, type Sums as Amounts, type Tone } from "@/lib/format";
import { actionRole, allowedInShadow, type Action } from "@/lib/api";
import { NO_SHEET, href, type Route, type To } from "@/lib/nav";

const ICONS: Record<string, Icon> = {
  "arrow-counter-clockwise": ArrowCounterClockwise, "arrow-down-left": ArrowDownLeft, "arrow-left": ArrowLeft, "arrow-right": ArrowRight, "arrow-up-right": ArrowUpRight, bank: Bank, calculator: Calculator, "caret-down": CaretDown, "caret-right": CaretRight, check: Check,
  "check-circle": CheckCircle, "clipboard-text": ClipboardText, clock: Clock, copy: Copy, "credit-card": CreditCard,
  "currency-circle-dollar": CurrencyCircleDollar, "download-simple": DownloadSimple, "envelope-simple": EnvelopeSimple,
  "file-text": FileText, fingerprint: Fingerprint, "gear-six": GearSix, house: House, invoice: Invoice, "link-simple": LinkSimple,
  list: List, "list-checks": ListChecks, "magnifying-glass": MagnifyingGlass, "paper-plane-tilt": PaperPlaneTilt, receipt: Receipt,
  "tray-arrow-down": TrayArrowDown, user: User, "warning-circle": WarningCircle, x: X, plugs: Plugs, sparkle: Sparkle,
  "hard-drives": HardDrives, plus: Plus, "pencil-simple": PencilSimple, camera: Camera, image: Image, sliders: SlidersHorizontal,
  package: Package, wrench: Wrench,
};

/** Phosphor icon by name, in Contra's hairline: the light weight by default, "check:bold" one step heavier (regular),
 * "check-circle:fill" filled only where the fill says something (a done check, a warning). */
const WEIGHT = { "": "light", bold: "regular", fill: "fill" } as const;
export function I({ n }: { n: string }) {
  const [name, weight] = n.split(":");
  const C = ICONS[name];
  return C ? <C weight={WEIGHT[(weight ?? "") as keyof typeof WEIGHT] ?? "light"} aria-hidden="true" /> : null;
}

/** A disclosure's summary (Adaugă un cont, Detalii): one look everywhere, a caret that turns when it opens instead of the
 * browser's triangle. */
export function Summary({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <summary className={`${summaryStyle} ${className}`}><I n="caret-right:bold" />{children}</summary>;
}
const summaryStyle = "flex min-h-11 w-fit max-w-full cursor-pointer list-none items-center gap-2 rounded-control text-sm font-semibold text-ink [&::-webkit-details-marker]:hidden [&>svg]:size-3.5 [&>svg]:text-muted [&>svg]:transition-transform motion-reduce:[&>svg]:transition-none [details[open]>&>svg]:rotate-90 hover:[&>svg]:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

/** A page's title: its one h1, which the app focuses when the user opens the page with a link (App.tsx), so it takes focus but not a Tab stop. */
export function Title({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <h1 className={`ttl ${titleStyle} ${className}`} tabIndex={-1}>{children}</h1>;
}
/** A page's h1: Contra's page title, the same on every page; a count after it (Sarcini 11) is quieter. */
export const titleStyle = "mx-0 mt-0 mb-2 text-[26px] leading-8 font-medium tracking-tight focus:outline-none [&_small]:ml-1 [&_small]:text-[21px] [&_small]:font-medium [&_small]:text-muted";

/** 5400.2 -> 5.400<span class=dec>,20</span> */
export function Big({ a }: { a: string }) {
  const [whole, dec] = lei(a).split(",");
  return (
    <>
      {whole}
      <span className="dec">,{dec}</span>
    </>
  );
}

/** Where an in-app link goes from `route`: a sheet without a tab opens over the current page; anything else keeps the company. */
export function targetOf(route: Route, to: Partial<Route>): To {
  const sheet = to.tab === undefined && (to.set || to.ord || to.tx || to.inv || to.iss || to.mail || to.doc || to.int || to.usr || to.p || to.prod || to.new);
  return sheet ? { ...route, ...NO_SHEET, ...to } : { co: route.co, ...to };
}

/** Typing in a field or an editable region (any contenteditable value but "false", inherited): shortcuts and focus moves
 * leave it alone. Browsers say so in isContentEditable; where it is missing, the attribute is read the same way. */
export function typing(el: EventTarget | null): boolean {
  if (!(el instanceof Element)) return false;
  if (el.closest("input, textarea, select, [role=combobox]")) return true;
  if (el instanceof HTMLElement && typeof el.isContentEditable === "boolean") return el.isContentEditable;
  for (let x: Element | null = el; x; x = x.parentElement) {
    const v = x.getAttribute("contenteditable")?.toLowerCase();
    if (v === "" || v === "true" || v === "plaintext-only") return true;
    if (v === "false") return false;
  }
  return false;
}

/** An in-app link: a real href (copy, open in a new tab), a click that only changes the query. As a button ("btn") it is
 * secondary unless it is the page's one primary action ("btn primary"). A sheet (inv, iss, mail, doc, int)
 * without a tab opens over the page it was tapped on, keeping its filters. */
export function L({ to, at, className, children, label, onGo, current, ref }: {
  to: Partial<Route>; at?: string; className?: string; children: ReactNode; label?: string; onGo?: () => void; current?: boolean; ref?: Ref<HTMLAnchorElement>;
}) {
  const { route, go } = useApp();
  const target = targetOf(route, to);
  const click = (e: MouseEvent) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    onGo?.();
    go(target, { at, focus: true });
  };
  return (
    <a className={className?.split(" ").includes("btn") ? `${className} ${buttonStyle} ${buttonTone[className.split(" ").includes("primary") ? "primary" : "secondary"]}` : className} href={href(target, at)} onClick={click} ref={ref} aria-label={label} aria-current={current ? "page" : undefined}>
      {children}
    </a>
  );
}

/** In a list marked `.tbl` (TableList), from 1100px a row lays its parts out as Contra's table columns: icon, name, state,
 * number, amount. The name and state spans, and the amount's, give their parts to the row's grid, each in its column. */
/** The same columns, for the list's header row (keep both strings equal: Tailwind reads them literally). */
export const tableColumns = "min-[1100px]:grid-cols-[32px_minmax(0,1.3fr)_minmax(0,1.7fr)_minmax(0,0.8fr)_minmax(0,0.8fr)]";
const tableRowStyle = "min-[1100px]:in-[.tbl]:grid min-[1100px]:in-[.tbl]:gap-x-4 min-[1100px]:in-[.tbl]:grid-cols-[32px_minmax(0,1.3fr)_minmax(0,1.7fr)_minmax(0,0.8fr)_minmax(0,0.8fr)]";
const tableCells = "min-[1100px]:in-[.tbl]:contents min-[1100px]:in-[.tbl]:[&>*]:self-center min-[1100px]:in-[.tbl]:[&>small]:mt-0 min-[1100px]:in-[.tbl]:[&>small]:text-[13px]";

const SIGN_CLS: Record<Sign, string> = { "=": "eq", "≈": "approx", "≠": "ne" };

/** A row's tile behind its icon or sign. Only what needs a person (late, refused, soon due, approximate) is tinted in its
 * state's colour; what is fine, informative or money in and out keeps a grey tile with the colour in the glyph alone.
 * test/contrast.test.ts checks each glyph against its tile in both schemes. */
export const ROW_TINT: Record<string, string> = {
  eq: "bg-muted/12 text-ok", ok: "bg-muted/12 text-ok",
  approx: "bg-wait/16 text-wait", soon: "bg-wait/16 text-wait",
  ne: "bg-danger/16 text-danger", late: "bg-danger/16 text-danger",
  info: "bg-muted/12 text-info", in: "bg-muted/12 text-money-in", out: "bg-muted/12 text-money-out",
  /** Something without a state (an invoice from history): quieter than any row that still asks for something. */
  quiet: "bg-muted/12 text-muted",
};
/** A row's icon tile without a state: grey, so colour in a list points only at what needs attention. */
export const ICON_TINT = "bg-muted/12 text-muted";
const MONEY_ICON: Record<string, string> = { "arrow-down-left": "in", "arrow-up-right": "out" };
/** A state said in words, under a row's name. */
export const TONE_TEXT: Record<Tone, string> = { "": "text-muted", late: "text-danger", soon: "text-wait", ok: "text-ok", info: "text-info" };
/** A row just added (a new key, a new link), for a moment. */
export const ROW_NEW = "bg-ok/8";

/** `mark`: a short tag before the text under the name ("Factură și bon"), read as part of it. `sign`: the state's sign
 * (derive.ts `Sign`), drawn in the icon's place and hidden from screen readers, which read the state in words. A row
 * that opens something draws its focus ring inside, so a card that clips it never hides it. */
export function Row({ icon, title, sub = "", subcls = "", val = "", vsub = "", to, file, at, badge = "", cls = "", mark = "", sign = "", onGo, onClick }: {
  icon: string; title: string; sub?: ReactNode; subcls?: Tone; val?: string; vsub?: string; to?: Partial<Route>; file?: string; at?: string;
  badge?: Tone | "quiet"; cls?: string; mark?: string; sign?: Sign | ""; onGo?: () => void; onClick?: () => void;
}) {
  // `file`: the row opens a document the engine serves, in a new tab, instead of a place in the app. `onClick`: a sheet here.
  const rowClass = `row ${cls} ${rowStyle} ${tableRowStyle} text-ink ${cls.split(" ").includes("rule") ? "items-start [&>.v]:self-center" : ""} ${to || file || onClick ? "hover:bg-inset focus-visible:rounded-card focus-visible:outline-offset-[-2px]" : ""}`;
  // The state colours the icon's tile (red overdue, amber due soon, green done); the text under the name says it in words.
  const inner = (
    <>
      {sign ? <span className={`ic g ${SIGN_CLS[sign]} ${rowIconStyle} text-lg font-semibold leading-none ${ROW_TINT[SIGN_CLS[sign]]}`} aria-hidden="true">{sign}</span>
        : <span className={`ic ${badge} ${rowIconStyle} ${ROW_TINT[badge || MONEY_ICON[icon]] ?? ICON_TINT}`}><I n={icon} /></span>}
      <span className={`t ${rowTextStyle} [&_b]:[overflow-wrap:anywhere] ${tableCells}`}>
        <b className="min-[1100px]:in-[.tbl]:col-start-2 min-[1100px]:in-[.tbl]:row-start-1">{title}</b>
        {sub || mark ? (
          <small className={`${subcls} ${TONE_TEXT[subcls]} mt-0.5 min-w-0 [overflow-wrap:anywhere] min-[1100px]:in-[.tbl]:col-start-3 min-[1100px]:in-[.tbl]:row-start-1`}>
            {mark ? <><span className="tag mr-1.5 inline-block rounded-control bg-inset px-1.5 align-[1px] text-xs font-semibold leading-5 text-ink">{mark}</span>{sub ? <span className="sr-only">, </span> : null}</> : null}
            {sub}
          </small>
        ) : null}
      </span>
      {val ? (
        <span className={`v max-w-[45%] text-right font-medium tabular-nums [overflow-wrap:anywhere] ${tableCells} ${val.startsWith("+") ? "text-money-in" : ""}`}>
          <span className="min-[1100px]:in-[.tbl]:col-start-5 min-[1100px]:in-[.tbl]:row-start-1">{val}</span>
          {vsub ? <small className="mt-0.5 block text-xs font-normal text-muted min-w-0 [overflow-wrap:anywhere] min-[1100px]:in-[.tbl]:col-start-4 min-[1100px]:in-[.tbl]:row-start-1 min-[1100px]:in-[.tbl]:text-left">{vsub}</small> : null}
        </span>
      ) : null}
    </>
  );
  if (file) return <a href={file} target="_blank" rel="noreferrer" className={rowClass} ref={rowRef}>{inner}</a>;
  if (onClick) return <button type="button" onClick={onClick} className={`${rowClass} w-full cursor-pointer border-0 bg-transparent text-left font-[inherit]`} ref={rowRef}>{inner}</button>;
  return to ? <L to={to} at={at} className={rowClass} onGo={onGo} ref={rowRef}>{inner}</L> : <div className={rowClass} ref={rowRef}>{inner}</div>;
}

/** What a button says while its request runs (docs/copy.md): its verb in progress, "Marchează ca plătită" → "Se marchează…". */
const IN_PROGRESS: Record<string, string> = {
  Marchează: "Se marchează", Adaugă: "Se adaugă", Salvează: "Se salvează", Șterge: "Se șterge", Confirmă: "Se confirmă",
  Pune: "Se adaugă", Anulează: "Se anulează", Verifică: "Se verifică", Pregătește: "Se pregătește", Atribuie: "Se atribuie", Folosește: "Se salvează",
  Setează: "Se salvează",
};
export const inProgress = (label: string) => `${IN_PROGRESS[label.split(" ")[0]] ?? "Se trimite"}…`;

/** The shared mark of a request on its way, beside the button's words. */
export function Spinner() {
  return <span className="size-4 shrink-0 rounded-full border-2 border-solid border-current border-r-transparent motion-safe:animate-spin" aria-hidden="true" />;
}

/** A POST action button for the company it sits under: asks its `confirm` question first when given, disabled while one runs or
 * when `off` says so, with `why` shown under its group. Absent in Sandbox when the action has an outward effect (the shared
 * policy, `allowedInShadow`): the app-wide banner says once what Sandbox is. The button whose request runs shows a spinner and its verb in progress. */
export function Act({ action, body, label, cls = "btn", confirm, inner, off = false, why = "", describedBy }: {
  action: Action; body: Record<string, string | number>; label: string; cls?: string; confirm?: Ask; inner?: ReactNode;
  off?: boolean; why?: string; describedBy?: string;
}) {
  const { act, busy } = useApp();
  const s = useCompany();
  const [mine, setMine] = useState(false);
  const pressed = useRef(false); // a second tap in the same tick neither sends nor resets the first one's progress
  const permitted = useCan(actionRole(action));
  const shadow = !s.live && !allowedInShadow(action);
  const reason = useReason(permitted && off ? why : "");
  if (!permitted || shadow) return null;
  // busy only once a question is answered: while it is open the button keeps its words
  const sending = mine && busy;
  const send = async () => {
    if (pressed.current) return;
    pressed.current = true;
    setMine(true);
    try { await act(s.co, action, body, confirm); } finally { pressed.current = false; setMine(false); }
  };
  return <>
    <Button type="button" className={cls} disabled={off || busy} aria-busy={sending || undefined}
      aria-describedby={[describedBy, reason.id].filter(Boolean).join(" ") || undefined}
      onClick={() => void send()}>
      {sending ? <Spinner /> : inner}
      {sending ? inProgress(label) : label}
    </Button>
    {reason.line}
  </>;
}

/** An action that can't be used now, kept in its place: a disabled button, with why shown under its group. */
export function Off({ cls, why, children }: { cls: string; why: string; children: ReactNode }) {
  const { id, line } = useReason(why);
  return (
    <>
      <button type="button" className={`${cls} off ${cls.split(" ").includes("btn") ? `${buttonStyle} ${buttonTone.secondary}` : ""}`} disabled aria-describedby={id}>{children}</button>
      {line}
    </>
  );
}

/** A task: its icon (tinted red only when it is urgent), what it is, and its answers beside it in a wide window. Under
 * Toate firmele a company's own task carries its name. */
export function Task({ icon, title, body, late, co, children }: {
  icon: string; title: string; body: string; late?: boolean; co?: string; children?: ReactNode;
}) {
  return (
    <div ref={rowRef} className="task flex min-w-0 flex-col items-stretch gap-4 border-0 border-b border-solid border-line py-4 lg:flex-row lg:items-center">
      <div className="h flex min-w-0 flex-1 items-start gap-3">
        <span className={`ic grid size-8 shrink-0 place-items-center rounded-control [&_svg]:size-4 ${late ? `late ${ROW_TINT.late}` : ICON_TINT}`}><I n={icon} /></span>
        <div className="min-w-0">
          <b className="block text-sm font-medium">{title}</b>
          <p className="mt-1 mb-0 text-sm leading-relaxed text-muted">{co ? `${co}. ${body}` : body}</p>
        </div>
      </div>
      {children ? <div className="btns m-0 flex flex-wrap justify-start gap-2 lg:max-w-[48%] lg:justify-end"><Reasons className="lg:text-right">{children}</Reasons></div> : null}
    </div>
  );
}

/** What every empty list says, filtered or not (docs/copy.md); `ok` marks an empty list as good news. `action`: the next step
 * under it, given only when nothing exists yet (a filter or a search that hides everything gets none). */
export const EMPTY = "Niciun element";

export function Empty({ ok, action }: { ok?: boolean; action?: ReactNode }) {
  return (
    <div className="empty flex flex-wrap items-center gap-3 px-5 py-5 text-sm leading-relaxed text-muted [&>.ok]:grid [&>.ok]:size-9 [&>.ok]:place-items-center [&>.ok]:rounded-full [&>.ok]:bg-ok/18 [&>.ok]:text-ok [&_svg]:size-5">
      {ok ? <span className="ok" aria-hidden="true"><I n="check-circle:fill" /></span> : null}
      {EMPTY}
      {action ? <div className="basis-full">{action}</div> : null}
    </div>
  );
}

/** Content not read yet: skeleton rows in its place (Controls.tsx), never an empty state or a sentence that jumps away. */
export function Loading({ rows = 3 }: { rows?: number }) {
  return <SkeletonRows rows={rows} />;
}

/** `sums` as text a narrow column can wrap: between the amounts, never inside one ("3.216,10" stays with its "lei"). */
export function Sums({ s }: { s: Amounts }) {
  const parts = sumParts(s);
  return <>{parts.map((p, i) => <Fragment key={p}>{i ? (i < parts.length - 1 ? ", " : " și ") : null}<span className="whitespace-nowrap">{p}</span></Fragment>)}</>;
}
