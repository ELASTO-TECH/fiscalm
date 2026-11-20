"use client";
import { Children, Fragment, createContext, isValidElement, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type KeyboardEvent, type ReactElement, type ReactNode } from "react";
// Controls draws its own two glyphs: ui.tsx, whose `I` names the others, imports this file.
import { CaretDown, Check } from "@phosphor-icons/react";

// The app's controls. Their class names (btn, form, btns, card) stay as hooks for tests; the look is in the utilities.

/** Contra's control heights in the desktop layout (from 1100px): buttons and chips 38px, fields and tabs 42px; a phone keeps
 * 44px touch targets. */
export const controlHeight = "min-h-11 min-[1100px]:min-h-[38px]";
export const fieldHeight = "min-h-11 min-[1100px]:min-h-[42px]";
const buttonBaseStyle = "m-0 box-border shadow-none text-center inline-flex flex-none items-center justify-center gap-2 rounded-full border border-solid font-[inherit] text-[13px] font-medium leading-5 no-underline whitespace-normal cursor-pointer transition-colors motion-reduce:transition-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:opacity-50 disabled:cursor-not-allowed aria-disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0";
export const buttonStyle = `${buttonBaseStyle} ${controlHeight} w-auto px-4 py-2`;
export const iconButtonStyle = `${buttonBaseStyle} size-11 p-0 min-[1100px]:size-[38px]`;
export const buttonTone = {
  primary: "border-action bg-action text-action-ink hover:opacity-90 active:opacity-80",
  secondary: "border-line bg-surface text-ink hover:bg-inset active:bg-selected",
  danger: "border-danger bg-surface text-danger hover:bg-inset active:bg-selected",
  quiet: "border-transparent bg-transparent text-muted hover:bg-inset hover:text-ink",
};
export function Button({ className = "btn", variant, ...props }: ComponentProps<"button"> & { variant?: keyof typeof buttonTone }) {
  const tone = variant ?? (className.split(" ").includes("g") ? "secondary" : className.includes("lnk") ? "quiet" : "primary");
  return <button {...props} className={`${className} ${buttonStyle} ${buttonTone[tone]}`} />;
}

export function Anchor({ className = "btn g", variant = "secondary", ...props }: ComponentProps<"a"> & { variant?: keyof typeof buttonTone }) {
  return <a {...props} className={`${className} ${buttonStyle} ${buttonTone[variant]}`} />;
}

export const controlStyle = "min-h-11 min-[1100px]:min-h-[42px] w-full min-w-0 rounded-field border border-solid border-line bg-surface px-4 py-2.5 font-[inherit] text-base min-[1100px]:text-[13px] font-normal leading-5 text-ink placeholder:text-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:opacity-50";
export function Input({ className = "", type, ...props }: ComponentProps<"input">) {
  const check = type === "radio" || type === "checkbox";
  return <input {...props} type={type} className={`${className} ${check ? "size-4 shrink-0 accent-action" : controlStyle}`} />;
}
export function Textarea({ className = "", ...props }: ComponentProps<"textarea">) {
  return <textarea {...props} className={`${className} ${controlStyle} min-h-28 resize-y`} />;
}
/** One choice of a `Select`, read from its `<option>` children. */
type Choice = { value: string; label: ReactNode; text: string; disabled: boolean };
const textOf = (n: ReactNode): string => typeof n === "string" || typeof n === "number" ? String(n)
  : Array.isArray(n) ? n.map(textOf).join("") : isValidElement(n) ? textOf((n.props as { children?: ReactNode }).children) : "";
function choicesOf(children: ReactNode): Choice[] {
  const out: Choice[] = [];
  const walk = (nodes: ReactNode) => Children.forEach(nodes, (c) => {
    if (!isValidElement(c)) return;
    const p = c.props as ComponentProps<"option">;
    if (c.type === Fragment) walk(p.children);
    else if (c.type === "option") out.push({ value: String(p.value ?? textOf(p.children)), label: p.children, text: textOf(p.children), disabled: !!p.disabled });
  });
  walk(children);
  return out;
}

/** Its trigger: a field's height, radius and padding, the choice on the left and a thin muted caret on the right, which turns
 * while the list is open. */
const selectTriggerStyle = "flex cursor-pointer items-center gap-2 text-left [&>svg]:size-3.5 [&>svg]:shrink-0 [&>svg]:text-muted [&>svg]:transition-transform motion-reduce:[&>svg]:transition-none aria-expanded:[&>svg]:rotate-180";
/** Its list: fixed to the window right under the trigger (above it when there is no room below), so no scrolling table or
 * sheet clips it; at least as wide, a hairline and a soft shadow. */
export const selectListStyle = "fixed z-[30] m-0 max-h-72 list-none overflow-y-auto rounded-field border border-solid border-line bg-surface p-1 text-ink shadow-pop";
export const selectOptionStyle = "flex min-h-11 cursor-pointer items-center gap-3 rounded-control px-3 text-[15px] min-[1100px]:min-h-9 min-[1100px]:text-[13px] aria-disabled:cursor-default aria-disabled:opacity-50 aria-selected:font-medium data-[active]:bg-inset [&>svg]:ml-auto [&>svg]:size-4 [&>svg]:shrink-0";
type Place = { left: number; width: number; top?: number; bottom?: number; maxHeight: number };

/** A choice among a few: the app's own list instead of the browser's (Contra's). Its props are a native select's as the app
 * uses them: `value`, `onChange` reading `e.target.value`, `<option>` children, `disabled`, `required`, an object ref; values
 * go through React state, never a form's own submission. WAI-ARIA's select-only combobox, named by its field's label (or
 * aria-label) with the choice as its value: focus stays on the trigger; arrows, Home and End move, Enter or Space picks,
 * Escape closes (only the list), letters jump to a choice, and Tab, a click outside or focus leaving close it. */
export function Select({ className = "", value = "", onChange, children, disabled, id, required, ref, ...props }:
  Omit<ComponentProps<"button">, "value" | "onChange"> & { value?: string; required?: boolean; onChange?: (e: { target: { value: string } }) => void }) {
  const choices = choicesOf(children);
  const own = useId();
  const trigger = id ?? `${own}-trigger`, list = `${own}-list`;
  const [open, setOpen] = useState(false);
  const [activeValue, setActiveValue] = useState<string | null>(null);
  const [place, setPlace] = useState<Place | null>(null);
  const [listName, setListName] = useState<ReturnType<typeof nameOf>>({});
  const wrap = useRef<HTMLDivElement>(null), button = useRef<HTMLButtonElement | null>(null);
  const typed = useRef({ text: "", at: 0 });
  const current = choices.findIndex((c) => c.value === value);
  const usable = (i: number) => i >= 0 && i < choices.length && !choices[i].disabled;
  const step = (from: number, by: number) => {
    for (let i = from + by; i >= 0 && i < choices.length; i += by) if (usable(i)) return i;
    return from;
  };
  // The highlighted choice by its value, so options that load or change while the list is open never shift it.
  const found = choices.findIndex((c) => c.value === activeValue);
  const active = usable(found) ? found : usable(current) ? current : step(-1, 1);
  const off = () => !!button.current?.matches(":disabled");
  const close = () => { setOpen(false); typed.current.text = ""; };
  /** Whether the trigger is still in sight: in the window and inside every scrolling box around it. */
  const inSight = (r: DOMRect) => {
    if (!r.width && !r.height) return true; // no box to judge by (not laid out)
    if (r.bottom <= 0 || r.top >= window.innerHeight) return false;
    for (let el = button.current?.parentElement; el; el = el.parentElement) {
      if (!/(auto|scroll|hidden)/.test(getComputedStyle(el).overflowY)) continue;
      const b = el.getBoundingClientRect();
      if (r.bottom <= b.top || r.top >= b.bottom) return false;
    }
    return true;
  };
  /** Under the trigger, or above it when there is more room there; never taller than that room (at most 288px). */
  const measure = () => {
    const r = button.current?.getBoundingClientRect();
    if (!r) return;
    if (!inSight(r)) { close(); return; }
    const below = window.innerHeight - r.bottom - 8, above = r.top - 8;
    setPlace(below < 300 && above > below ? { left: r.left, width: r.width, bottom: window.innerHeight - r.top + 4, maxHeight: Math.min(288, above) }
      : { left: r.left, width: r.width, top: r.bottom + 4, maxHeight: Math.min(288, below) });
  };
  /** What names the trigger and its list, the choice staying the value: its field's `<label for>` (given an id), else the
   * text of a label around it without the choice shown inside. */
  const nameOf = (el = button.current): { "aria-labelledby"?: string; "aria-label"?: string } => {
    if (props["aria-label"]) return { "aria-label": props["aria-label"] };
    const label = document.querySelector<HTMLLabelElement>(`label[for="${CSS.escape(trigger)}"]`);
    if (label) {
      if (!label.id) label.id = `${trigger}-label`;
      return { "aria-labelledby": label.id };
    }
    const around = el?.closest("label");
    const text = around ? (around.textContent ?? "").replace(el?.textContent ?? "", "").trim() : "";
    return text ? { "aria-label": text } : {};
  };
  const show = (at = current) => {
    if (off()) return;
    button.current?.focus();
    // An entry (globals.css enter-*) still running around the trigger (a sheet rising, the page fading) ends now: the list is placed in the
    // window and must sit over everything, at once.
    const anchor = button.current;
    for (const a of document.getAnimations?.() ?? []) {
      const target = (a.effect as KeyframeEffect | null)?.target;
      if (anchor && target?.contains(anchor) && a instanceof CSSAnimation && a.animationName.startsWith("enter-")) a.finish();
    }
    measure();
    setListName(nameOf());
    setActiveValue(usable(at) ? choices[at].value : null);
    setOpen(true);
  };
  const pick = (i: number) => {
    if (!usable(i) || off()) return;
    close();
    button.current?.focus();
    if (choices[i].value !== value) onChange?.({ target: { value: choices[i].value } });
  };
  const move = (i: number) => { if (usable(i)) setActiveValue(choices[i].value); };
  /** Letters typed within 700ms make one prefix; the same letter again cycles through the choices it starts. */
  const ahead = (key: string) => {
    const t = typed.current, now = Date.now(), k = key.toLowerCase();
    const prefix = now - t.at < 700 ? t.text + k : k;
    t.at = now;
    t.text = [...prefix].every((c) => c === k) ? k : prefix;
    const from = t.text.length === 1 ? active + 1 : Math.max(active, 0);
    const order = [...choices.keys()].map((n) => (n + from) % choices.length);
    return order.find((i) => usable(i) && choices[i].text.toLowerCase().startsWith(t.text)) ?? -1;
  };
  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) close(); };
    document.addEventListener("mousedown", outside);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      document.removeEventListener("mousedown", outside);
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  });
  useEffect(() => { if (open) document.getElementById(`${own}-${active}`)?.scrollIntoView?.({ block: "nearest" }); }, [open, active, own]);
  const key = (e: KeyboardEvent<HTMLButtonElement>) => {
    const k = e.key;
    if (e.nativeEvent.isComposing) return;
    if (k === "Tab" || (open && off())) { close(); return; }
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (!open) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(k)) { e.preventDefault(); show(); }
      else if (k.length === 1) { const i = ahead(k); if (i >= 0) { e.preventDefault(); show(i); } }
      return;
    }
    const to = k === "ArrowDown" ? step(active, 1) : k === "ArrowUp" ? step(active, -1) : k === "Home" ? step(-1, 1)
      : k === "End" ? step(choices.length, -1) : k.length === 1 && k !== " " ? ahead(k) : null;
    if (to !== null) { e.preventDefault(); move(to); }
    else if (k === "Enter" || k === " ") { e.preventDefault(); pick(active); }
    else if (k === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
  };
  return (
    <div ref={wrap} className={`relative min-w-0 ${className}`}
      onBlur={(e) => { if (open && !wrap.current?.contains(e.relatedTarget as Node | null)) close(); }}>
      <button {...props} ref={(el) => {
        button.current = el;
        if (el && !props["aria-label"] && !props["aria-labelledby"]) for (const [k, v] of Object.entries(nameOf(el))) el.setAttribute(k, v);
        if (typeof ref === "function") ref(el); else if (ref) ref.current = el;
      }}
        id={trigger} type="button" role="combobox" value={value} disabled={disabled}
        aria-required={required || props["aria-required"] || undefined} aria-haspopup="listbox" aria-expanded={open} aria-controls={list}
        aria-activedescendant={open && active >= 0 ? `${own}-${active}` : undefined}
        onClick={() => (open ? close() : show())} onKeyDown={key} className={`${controlStyle} ${selectTriggerStyle}`}>
        <span className="min-w-0 flex-1 truncate">{choices[current]?.label ?? ""}</span>
        <CaretDown weight="regular" aria-hidden="true" />
      </button>
      {open && place ? (
        <ul id={list} role="listbox" {...listName} className={`${selectListStyle} ${motion.pop} ${place.bottom === undefined ? "origin-top" : "origin-bottom"}`}
          style={{ left: place.left, minWidth: place.width, top: place.top, bottom: place.bottom, maxHeight: place.maxHeight }}>
          {choices.map((c, i) => (
            <li key={`${c.value}-${i}`} id={`${own}-${i}`} role="option" data-value={c.value} aria-selected={i === current}
              aria-disabled={c.disabled || undefined} data-active={i === active ? "" : undefined} className={selectOptionStyle}
              onMouseDown={(e) => e.preventDefault()} onMouseMove={() => move(i)}
              onClick={(e) => { e.preventDefault(); pick(i); }}>
              <span className="min-w-0">{c.label}</span>{i === current ? <Check weight="regular" aria-hidden="true" /> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function Label({ className = "", ...props }: ComponentProps<"label">) {
  return <label {...props} className={`${className} mx-0 mt-4 mb-2 block text-[13px] font-semibold leading-5 text-ink first:mt-0 has-[input[type=checkbox]]:flex has-[input[type=radio]]:flex has-[input]:items-start has-[input]:gap-3`} />;
}
/** A fieldset's legend, set like a field's `Label`. */
export const legendStyle = "mb-2 p-0 text-[13px] font-semibold leading-5 text-ink";
export function Form({ className = "form", ...props }: ComponentProps<"form">) {
  return <form {...props} className={`${className} m-0 p-0 min-w-0`} />;
}
/** Inside it every `Actions` is one column of full-width buttons at every width: a narrow card whose buttons are
 * alternatives to one another (sign-in, recovery, account setup, the upload link page). */
const ColumnContext = createContext(false);
export function ActionColumn({ children }: { children: ReactNode }) {
  return <ColumnContext.Provider value>{children}</ColumnContext.Provider>;
}
/** One group of buttons, the only way to lay buttons out: one column of full-width buttons on a phone (and in an
 * `ActionColumn`), a row of content-width buttons from the content's left edge from 640px. `spread`: always a row,
 * its two ends apart (Înapoi / Înainte). One group per set of choices: don't stack separate groups. */
export function Actions({ className = "btns", spread = false, children, ...props }: ComponentProps<"div"> & { spread?: boolean }) {
  const column = useContext(ColumnContext);
  const layout = spread ? "flex-wrap items-center justify-between" : column ? "flex-col items-stretch" : "flex-col items-stretch sm:flex-row sm:flex-wrap sm:items-center";
  return <div {...props} className={`${className} mt-5 flex gap-2 ${layout}`}><Reasons>{children}</Reasons></div>;
}

/** Why a disabled control can't be used: one short muted line under its group of buttons, which describes the control.
 * `more`: a link after the reason (Setări › Mod), passed as a constant element so it registers once. */
type Reason = { why: string; more?: ReactElement };
const ReasonsContext = createContext<{ add: (r: Reason) => () => void; base: string } | null>(null);
const reasonStyle = "m-0 basis-full text-[13px] font-normal leading-snug text-muted [&_a]:font-semibold [&_a]:text-ink [&_a]:underline [&_a]:underline-offset-4";
/** One id per reason text in a group, so two buttons with the same reason point at its one line. */
const reasonId = (base: string, why: string) => `${base}-${[...why].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7).toString(36)}`;

function ReasonLine({ id, r, className = "" }: { id: string; r: Reason; className?: string }) {
  return <span className={`${reasonStyle} block ${className}`}><span id={id}>{r.why}</span>{r.more ? <> {r.more}</> : null}</span>;
}

/** A group of buttons (`Actions`, a task's answers): the reasons of its disabled buttons follow them, each text once. */
export function Reasons({ children, className = "" }: { children: ReactNode; className?: string }) {
  const base = useId(), seq = useRef(0);
  const [list, setList] = useState<{ key: number; r: Reason }[]>([]);
  const add = useCallback((r: Reason) => {
    const key = ++seq.current;
    setList((l) => [...l, { key, r }]);
    return () => setList((l) => l.filter((x) => x.key !== key));
  }, []);
  const value = useMemo(() => ({ add, base }), [add, base]);
  const shown = list.filter((x, i) => list.findIndex((y) => y.r.why === x.r.why) === i);
  return <ReasonsContext.Provider value={value}>
    {children}
    {shown.map(({ r }) => <ReasonLine key={r.why} id={reasonId(base, r.why)} r={r} className={className} />)}
  </ReasonsContext.Provider>;
}

/** Says `why` (when not empty) for a disabled control: under its group, or right after it outside one (a line of its own in a
 * flex row or a grid). Returns the id that describes the control and the line to render after it. */
export function useReason(why: string, more?: ReactElement): { id?: string; line: ReactNode } {
  const group = useContext(ReasonsContext), own = useId();
  const add = group?.add;
  useLayoutEffect(() => (add && why ? add({ why, more }) : undefined), [add, why, more]);
  if (!why) return { line: null };
  if (group) return { id: reasonId(group.base, why), line: null };
  return { id: own, line: <ReasonLine id={own} r={{ why, more }} className="order-last col-span-full" /> };
}
export function Panel({ padded = true, className = padded ? "card pad" : "card", ...props }: ComponentProps<"div"> & { padded?: boolean }) {
  return <div {...props} className={`${className} ${surfaceStyle} m-0 overflow-visible ${padded ? "p-5 sm:p-6" : "p-0"}`} />;
}

export const formTypography = "text-[15px] leading-[23px] text-ink [&_p]:my-0 [&_p]:text-[13px] [&_p]:leading-5 [&_.note]:mx-0 [&_.note]:text-sm [&_.note]:text-muted [&_.note]:text-left [&_h2]:text-[21px] [&_h2]:leading-7 [&_h2]:font-medium [&_h3]:text-[15px] [&_h3]:font-medium [&_[role=alert]]:text-danger [&_fieldset]:min-w-0";

// Reused by semantic sections and links that should keep their native element.
export const surfaceStyle = "min-w-0 rounded-card border border-solid border-line bg-surface shadow-card";
export const sectionHeadingStyle = "mx-0 mt-6 mb-3 flex flex-wrap items-baseline justify-between gap-2 [&_h2]:m-0 [&_h2]:text-[21px] [&_h2]:leading-7 [&_h2]:font-medium [&_a]:text-sm [&_a]:text-muted [&_a]:no-underline [&_a:hover]:text-ink";
export const amountStyle = "my-0 text-3xl font-bold tracking-tight [font-stretch:110%] tabular-nums [&_.dec]:text-xl [&_.cur]:ml-1 [&_.cur]:text-sm [&_.cur]:font-normal";
/** A small remark under a card or a list (centred), or beside a form (`noteLeftStyle`). */
export const noteStyle = "mx-1.5 mt-3.5 mb-0 text-center text-[13px] font-normal leading-relaxed text-muted";
export const noteLeftStyle = "mx-0 my-2 text-left text-[13px] font-normal leading-relaxed text-muted";
/** A notice at the top of a page or sheet: its first line in bold. `bannerLateStyle`: something is wrong and needs a person. */
export const bannerStyle = "mt-0 mb-4 rounded-card border border-solid border-line bg-surface px-4 py-3 text-sm leading-snug text-ink [&>b]:block [&>button]:mt-3";
export const bannerLateStyle = "mt-0 mb-3 flex items-center gap-2.5 rounded-card border border-solid border-danger/30 bg-danger/12 px-4 py-3 text-sm font-semibold leading-snug text-danger [&_svg]:size-5 [&_svg]:shrink-0";
/** A secondary action read as a link inside text or a list (Șterge, Adaugă încă una). */
export const linkButtonStyle = "inline cursor-pointer border-0 bg-transparent p-0 py-1.5 font-[inherit] text-sm font-semibold text-ink underline decoration-ink/30 underline-offset-4 disabled:cursor-default disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

/** "← Setări": back to the page a section belongs to, above its title. */
export const backLinkStyle = "mb-3 inline-flex min-h-11 items-center gap-1.5 text-sm font-semibold text-muted no-underline hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus [&_svg]:size-[18px]";
/** The sentence under a page's title. */
export const leadStyle = "mx-0 mt-1 mb-5 text-sm leading-relaxed text-muted";
/** A state in a few words (Pregătit, Trimisă, Respinsă): coloured by its tone, never by itself alone. */
export const statusStyle: Record<string, string> = { "": "", ok: "text-ok", late: "text-danger", soon: "text-wait", info: "text-info" };
export const statusTextStyle = "text-[13px] font-medium";
/** A sheet's state under its head, as a small badge: the state's colour on a faint tint of it (test/contrast.test.ts). */
export const statePillStyle: Record<string, string> = {
  "": "bg-inset text-ink", ok: "bg-ok/8 text-ok", late: "bg-danger/8 text-danger", soon: "bg-wait/8 text-wait", info: "bg-info/8 text-info",
};

/** A page's sections (Cheltuieli: Facturi, Bonuri și chitanțe): underlined tabs over the page's filters. A filter (Toate, De
 * plătit) is a quiet tab: the current one is filled grey, in ink and medium weight (Contra's), so not by colour alone. */
export const pageTabsStyle = "mt-4 flex gap-6 border-0 border-b border-solid border-line";
export const pageTabStyle = "-mb-px inline-flex min-h-11 min-[1100px]:min-h-[42px] items-center border-0 border-b-2 border-solid border-transparent text-[13px] font-medium text-muted no-underline hover:text-ink aria-[current=page]:border-ink aria-[current=page]:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";
export const filterLinkStyle = "inline-flex min-h-11 min-[1100px]:min-h-[42px] items-center justify-center gap-2 rounded-control px-4 py-2 text-[13px] font-normal text-muted no-underline hover:text-ink aria-[current=page]:bg-selected aria-[current=page]:font-medium aria-[current=page]:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";
/** A table of figures: header in muted text, rows ruled, numbers in `numCellStyle` cells. */
export const dataTableStyle = "m-0 w-full border-collapse text-sm leading-relaxed [&_th]:px-2 [&_td]:px-2 [&_th:first-child]:pl-0 [&_td:first-child]:pl-0 [&_th:last-child]:pr-0 [&_td:last-child]:pr-0 [&_th]:py-2.5 [&_td]:py-2.5 [&_thead_th]:pt-0 [&_thead_th]:pb-2 [&_th]:align-top [&_td]:align-top [&_th]:text-left [&_thead_th]:text-[13px] [&_thead_th]:font-medium [&_thead_th]:text-muted [&_tbody_tr]:border-0 [&_tbody_tr]:border-t [&_tbody_tr]:border-solid [&_tbody_tr]:border-line";
export const numCellStyle = "text-right! tabular-nums whitespace-nowrap";
export const detailListStyle = "m-0 grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-x-4 gap-y-3 text-sm leading-relaxed [&_dt]:text-muted [&_dd]:m-0 [&_dd]:min-w-0 [&_dd]:break-words";

/** Calm motion (globals.css): a dialog or the page fades in, a menu grows from its trigger, a dialog rises a few pixels, a
 * phone's sheet rises from the bottom edge. Each runs only when motion is welcome (`motion-safe:`). */
export const motion = {
  fade: "motion-safe:animate-enter-fade",
  pop: "motion-safe:animate-enter-pop",
  /** A `<details>` menu's list, which the browser hides without removing it: it grows each time its details opens. */
  popOpen: "[[open]>&]:motion-safe:animate-enter-pop",
  rise: "motion-safe:animate-enter-rise",
  sheet: "motion-safe:animate-enter-sheet",
};
const still = () => typeof window.matchMedia !== "function" || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
/** Run an element's entry again (the page's fade when another page replaces it in the same element). */
export function replay(el: HTMLElement | null) {
  if (!el || still()) return;
  el.style.animation = "none";
  void el.offsetWidth;
  el.style.animation = "";
}

const SECRET = "code, input[type=password], input[autocomplete=one-time-code]";
/** A copy names nothing and points at nothing: no ids, roles, labels for fields, names or references to other ids. */
function unlink(ghost: HTMLElement) {
  const refs = ["id", "role", "for", "name", "aria-labelledby", "aria-describedby", "aria-controls", "aria-activedescendant"];
  for (const n of [ghost, ...ghost.querySelectorAll("*")]) for (const a of refs) n.removeAttribute(a);
}
let generation = 0;
/** Signed out, another account or identity being checked again: every copy goes at once, and none still pending appears. */
export function dropCopies() {
  generation++;
  for (const el of document.querySelectorAll("[data-leaving]")) el.remove();
}

/** A copy keeps none of the inline animations its original had (an entry switched off, a fade-in still running). */
function stillCopy(ghost: HTMLElement) {
  for (const n of [ghost, ...ghost.querySelectorAll<HTMLElement>("[style]")]) n.style.removeProperty("animation");
}

/** Fade out what is leaving the page: called as `els` are being removed, it copies them (inert, hidden from assistive
 * technology, without ids, roles or the `drop` hooks the app and its tests look for) and, once the removal is done, puts the
 * copies in `parent` for the exit and then removes them. Nothing if the elements stayed (a re-run effect), if `skip()` says
 * something took their place, or under reduced motion. The real elements are already gone, so focus, input and the next
 * screen never wait. */
export function fadeOut(els: (HTMLElement | null)[], parent: Element | null, drop: string[] = [], skip = () => false) {
  // Something hidden (a sheet under identity revalidation) leaves hidden; a secret (a setup link or key in <code>, a
  // password, a one-time code) is never copied.
  const shown = els.filter((el): el is HTMLElement => !!el && (typeof el.checkVisibility !== "function" || el.checkVisibility()));
  if (!parent || !shown.length || still() || shown.some((el) => el.querySelector(SECRET))) return;
  const age = generation;
  const ghosts = shown.map((el) => {
    const ghost = el.cloneNode(true) as HTMLElement;
    unlink(ghost);
    ghost.classList.remove(...drop, ...[...ghost.classList].filter((c) => c.includes("animate-")));
    stillCopy(ghost);
    ghost.classList.add("pointer-events-none", "animate-exit-fade");
    // globals.css hides it with the sheets while identity is revalidated.
    ghost.setAttribute("data-leaving", "");
    ghost.setAttribute("inert", "");
    ghost.setAttribute("aria-hidden", "true");
    return { ghost, top: el.scrollTop };
  });
  queueMicrotask(() => {
    if (age !== generation || !parent.isConnected || shown.some((el) => el.isConnected) || skip()) return;
    for (const { ghost, top } of ghosts) {
      parent.append(ghost);
      ghost.scrollTop = top;
      const gone = () => ghost.remove();
      ghost.addEventListener("animationend", gone);
      setTimeout(gone, 400);
    }
  });
}

/** Elements that take the place of a skeleton or of a sheet's first content fade in, through the token in an inline style
 * that is dropped once it has run. */
export function reveal(els: Iterable<Element>) {
  if (still()) return;
  for (const el of els) {
    if (!(el instanceof HTMLElement)) continue;
    el.style.animation = "var(--animate-enter-fade)";
    el.addEventListener("animationend", () => { el.style.animation = ""; }, { once: true });
  }
}

/** True while the commit that removed a skeleton runs: the rows it puts on screen fade in (`rowRef`). */
let skeletonGone = false;

/** A skeleton's ref (React 19 ref cleanup): when it goes and its container stays, what took its place there fades in. */
export function revealAfter(el: HTMLElement | null) {
  const parent = el?.parentElement;
  if (!el || !parent) return;
  return () => {
    const before = new Set(parent.children);
    if (!skeletonGone) queueMicrotask(() => { skeletonGone = false; });
    skeletonGone = true;
    queueMicrotask(() => { if (parent.isConnected && !el.isConnected) reveal([...parent.children].filter((c) => !before.has(c))); });
  };
}

/** Rows may leave their list (`rowRef`) until this time: an action's answer just arrived (App.tsx `run`). A list that changes
 * for any other reason (a filter, a search, another page) changes at once. */
let leaveUntil = 0;
export const rowsMayLeave = () => { leaveUntil = Date.now() + 2000; };
/** Another page or firm: its lists change at once, and no copy of the last one's rows or sheets stays over them. */
export const rowsStay = () => { leaveUntil = 0; dropCopies(); };
type Gone = { el: HTMLElement; parent: HTMLElement; next: ChildNode | null; h: number; ghost: HTMLElement };
let gone: Gone[] = [];
const before = new Map<HTMLElement, Set<Element>>();

/** Puts back the rows that left in one commit, each where it was (rows that left together keep their order), unless more
 * than five left. A list none of whose rows stayed is a new list, shown at once. */
function settle() {
  const all = gone, lists = new Map(before);
  gone = [];
  before.clear();
  const stays = (p: HTMLElement) => [...p.children].some((c) => lists.get(p)?.has(c));
  // A row inside another row that left goes with it; a page or firm changed meanwhile (rowsStay) shows its lists at once.
  const rows = all.filter((g) => g.parent.isConnected && !g.el.isConnected && stays(g.parent));
  if (Date.now() > leaveUntil || rows.length > 5) return;
  const ghosts = new Map(rows.map((g) => [g.el as ChildNode, g.ghost]));
  for (const { parent, next, h, ghost } of rows.reverse()) {
    unlink(ghost);
    stillCopy(ghost);
    ghost.classList.add("pointer-events-none", "overflow-hidden", "animate-exit-collapse");
    ghost.setAttribute("data-leaving", "");
    ghost.setAttribute("inert", "");
    ghost.setAttribute("aria-hidden", "true");
    ghost.style.setProperty("--h", `${h}px`);
    const at = next && ghosts.has(next) ? ghosts.get(next)! : next;
    parent.insertBefore(ghost, at?.parentNode === parent ? at : null);
    const drop = () => ghost.remove();
    ghost.addEventListener("animationend", drop);
    setTimeout(drop, 400);
  }
}

/** A list row's ref, on the element that is the list's own child. A row that replaces a skeleton fades in. A row an action
 * took away (put in a payment file, resolved) fades and closes its height where it was, as an unreachable copy, so the rows
 * below move up instead of jumping. */
export function rowRef(el: HTMLElement | null) {
  if (!el) return;
  if (skeletonGone) reveal([el]);
  return () => {
    const parent = el.parentElement;
    if (!parent || Date.now() > leaveUntil || gone.length > 10 || still()) return;
    if (!gone.length) queueMicrotask(settle);
    if (!before.has(parent)) before.set(parent, new Set(parent.children));
    gone.push({ el, parent, next: el.nextSibling, h: el.offsetHeight, ghost: el.cloneNode(true) as HTMLElement });
  };
}

/** A navigation's marker: one shape under the current item (`pick` finds it in `box`) that slides when another becomes
 * current, instead of the highlight jumping. It snaps into place the first time and whenever it was hidden. */
export const markerStyle = "pointer-events-none absolute top-0 left-0 motion-safe:transition-[transform,width,height] motion-safe:duration-(--motion-slide) motion-safe:ease-calm";
export function useMarker<B extends HTMLElement>(pick: string, current: string) {
  const box = useRef<B>(null), mark = useRef<HTMLSpanElement>(null), placed = useRef(false);
  useLayoutEffect(() => {
    const b = box.current, m = mark.current;
    if (!b || !m) return;
    const place = () => {
      const cur = b.querySelector<HTMLElement>(pick);
      m.hidden = !cur;
      if (!cur) { placed.current = false; return; }
      if (!placed.current) m.style.transition = "none";
      Object.assign(m.style, { transform: `translate(${cur.offsetLeft}px, ${cur.offsetTop}px)`, width: `${cur.offsetWidth}px`, height: `${cur.offsetHeight}px` });
      if (!placed.current) { void m.offsetWidth; m.style.transition = ""; placed.current = true; }
    };
    place();
    if (typeof ResizeObserver !== "function") return;
    // The column, and the current item itself (Setări gains its "Verificat" line while current).
    const watch = new ResizeObserver(place);
    watch.observe(b);
    const cur = b.querySelector(pick);
    if (cur) watch.observe(cur);
    return () => watch.disconnect();
  }, [pick, current]);
  return { box, mark };
}

/** A shape that stands in for content not read yet: the inset grey, pulsing unless the person asked for less motion. Its size
 * is the content's (`className`), so nothing moves when the answer replaces it. Never an empty state: a list that is loading
 * shows these, and "Niciun element" only once the answer says so. */
export function Skeleton({ className = "h-4 w-24" }: { className?: string }) {
  return <span className={`block shrink-0 rounded-control bg-inset motion-safe:animate-pulse ${className}`} aria-hidden="true" />;
}

/** `rows` list rows not read yet, each as tall as a real one (`rowStyle`): an icon, a name and a line under it, an amount. Its
 * status says "Se încarcă…" to a screen reader, unless the page around it already says so (`quiet`). */
export function SkeletonRows({ rows = 6, icon = true, quiet = false }: { rows?: number; icon?: boolean; quiet?: boolean }) {
  return <div ref={revealAfter} role={quiet ? undefined : "status"} aria-hidden={quiet || undefined} className="divide-x-0 divide-y divide-solid divide-line/60">
    {quiet ? null : <span className="sr-only">Se încarcă…</span>}
    {Array.from({ length: rows }, (_, i) => <div key={i} className={rowStyle}>
      {icon ? <Skeleton className="size-8" /> : null}
      <span className="flex min-w-0 flex-1 flex-col gap-1.5"><Skeleton className={i % 2 ? "h-3.5 w-2/5" : "h-3.5 w-1/2"} /><Skeleton className="h-3 w-1/4" /></span>
      <Skeleton className="h-3.5 w-16" />
    </div>)}
  </div>;
}

// Native row parts also work around embedded edit/revoke controls, without a wrapping link.
/** A list row: Contra's table row, 56px with its divider, 16px from the card's edges. */
export const rowStyle = "flex min-h-14 min-w-0 items-center gap-3 px-4 py-2 text-[13px] no-underline";
export const rowTextStyle = "min-w-0 flex-1 [&_b]:block [&_b]:whitespace-normal [&_b]:break-words [&_b]:font-normal [&_small]:block [&_small]:text-xs [&_small]:leading-relaxed";
export const rowIconStyle = "grid size-8 shrink-0 place-items-center rounded-control [&_svg]:size-4";
/** A Setări page (Firme, Integrări, Acces…): the overview's width, so moving between them keeps the column; a note
 * before a card or after one keeps its distance from it. */
export const settingsPageStyle = `mx-auto w-full max-[1099px]:max-w-[880px] min-w-0 ${formTypography} [&_p]:mx-0 [&>p:not(:last-child)]:mb-4 [&_section>p:not(:last-child)]:mb-3 [&>div+p]:mt-3 [&>section+p]:mt-3`;
