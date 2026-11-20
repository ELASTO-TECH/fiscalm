// Contrast, computed from globals.css in both schemes: muted and state text reach 4.5:1 on each surface they sit on, a
// row's sign or icon 3:1 on its tinted circle, the focus ring 3:1, a company's mark its letters 4.5:1; a card's rows draw their ring inside; the phone's tab bar never
// covers a focused element.
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse, type AnyNode } from "postcss";
import { ICON_TINT, ROW_NEW, ROW_TINT } from "@/components/ui";
import { barStyle } from "@/components/PayPick";
import { bannerLateStyle, filterLinkStyle, sectionHeadingStyle, statePillStyle } from "@/components/Controls";

/** Tailwind's colour names (globals.css @theme) for the tokens. */
const TOKEN: Record<string, string> = { ok: "ok", wait: "wait", danger: "no", info: "info", "money-in": "in", "money-out": "out", action: "acc", muted: "mu" };

const css = readFileSync(resolve(__dirname, "../src/app/globals.css"), "utf8");

type RGB = [number, number, number];
const hex = (h: string): RGB => {
  const x = h.trim().replace(/^#(\w)(\w)(\w)$/, "#$1$1$2$2$3$3"); // #fff
  return [1, 3, 5].map((i) => parseInt(x.slice(i, i + 2), 16) / 255) as RGB;
};
/** `a` at `t` (0..1) over `b`: what color-mix(in srgb, a t, transparent) shows on b. */
const over = (a: RGB, b: RGB, t: number): RGB => a.map((x, i) => x * t + b[i] * (1 - t)) as RGB;
const lum = (c: RGB) => {
  const [r, g, b] = c.map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a: RGB, b: RGB) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};
const vars = (body: string): Record<string, string> => Object.fromEntries([...body.matchAll(/--([\w-]+):([^;}]+)/g)].map(([, k, x]) => [k, x]));

/** Each palette's company colour, from its [data-pal=...] rule. */
const palettes = [...css.matchAll(/(?::root,)?\[data-pal=(\w+)\]\{([^}]*)\}/g)].map(([, name, body]) => ({ name, v: vars(body) }));
const dark = vars(css.match(/:root\{color-scheme:dark light;([^}]*)\}/)![1]);
const light = { ...dark, ...vars(css.match(/@media\(prefers-color-scheme:light\)\{:root\{([^}]*)\}\}/)![1]) };
/** A token's colour: a hex, another token, or a share of one over the page. */
function color(v: Record<string, string>, k: string): RGB {
  const x = v[k].trim();
  const ref = /^var\(--([\w-]+)\)$/.exec(x);
  const mix = /^color-mix\(in srgb,var\(--([\w-]+)\) (\d+)%,transparent\)$/.exec(x);
  return ref ? color(v, ref[1]) : mix ? over(color(v, mix[1]), color(v, "bg"), Number(mix[2]) / 100) : hex(x);
}
const schemes = { dark, light };
/** Where text sits: the page, cards and sheets, icon fills, chips, the selected item in the desktop column. */
const SURFACES = ["bg", "sf", "sf2", "chip", "sel"];

describe("contrast in both schemes", () => {
  it("keeps the sheet's surface opaque in both schemes", () => {
    // Sheets draw on --sf (Sheets.tsx sheetStyle, bg-surface); test/sheets.test.tsx checks the dialog uses it.
    for (const v of Object.values(schemes)) expect(v.sf).toMatch(/^#[a-f0-9]{3}(?:[a-f0-9]{3})?$/i);
  });

  it("covers all six palettes, each mark's letters at least 4.5:1 on its colour", () => {
    expect(palettes.map((p) => p.name).sort()).toEqual(["amber", "blue", "green", "rose", "slate", "violet"]);
    for (const { name, v } of palettes) expect(ratio(hex(v.co), hex(v["co-ink"])), name).toBeGreaterThanOrEqual(4.5);
  });

  it("text and muted text are at least 4.5:1 on every surface", () => {
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const s of SURFACES) {
        for (const t of ["ink", "mu"]) expect(ratio(color(v, t), color(v, s)), `${scheme}, ${t} on ${s}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("the states (ok, wait, no, info) are at least 4.5:1 on the page, a card, and ok on a new row's tint", () => {
    const tint = Number(/^bg-ok\/(\d+)$/.exec(ROW_NEW)![1]) / 100;
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const st of ["ok", "wait", "no", "info"]) {
        for (const s of ["bg", "sf"]) expect(ratio(color(v, st), color(v, s)), `${scheme}, ${st} on ${s}`).toBeGreaterThanOrEqual(4.5);
      }
      expect(ratio(color(v, "ok"), over(color(v, "ok"), color(v, "sf"), tint)), `${scheme}, new row`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("a late banner's text is at least 4.5:1 on its tint, on the page and on a card", () => {
    const t = Number(/bg-danger\/(\d+)/.exec(bannerLateStyle)![1]) / 100;
    expect(bannerLateStyle.split(" ")).toContain("text-danger");
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const s of ["bg", "sf"]) expect(ratio(color(v, "no"), over(color(v, "no"), color(v, s), t)), `${scheme}, over ${s}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("a row's state colour (its sign or icon) is at least 3:1 on its tinted circle, on a card and on the page", () => {
    // The tints, as ui.tsx ROW_TINT draws them: `bg-<token>/<share> text-<token>`.
    // A sign is a 20px glyph of weight 650 (large text from 18.66px bold) and an icon a graphic: large text and non-text contrast, 3:1.
    const tints = Object.entries(ROW_TINT).map(([cls, c]) => {
      const [, bg, pct, fg] = /^bg-([\w-]+)\/(\d+) text-([\w-]+)$/.exec(c)!;
      return { cls, bg: TOKEN[bg], t: Number(pct) / 100, fg: TOKEN[fg] };
    });
    expect(tints.map((x) => x.cls).sort()).toEqual(["approx", "eq", "in", "info", "late", "ne", "ok", "out", "quiet", "soon"]);
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const { cls, bg, t, fg } of tints) {
        for (const s of ["sf", "bg"]) {
          const circle = over(color(v, bg), color(v, s), t);
          expect(ratio(color(v, fg), circle), `${scheme}, ${cls} on its tint over ${s}`).toBeGreaterThanOrEqual(3);
        }
      }
    }
  });

  it("a row's plain icon is at least 3:1 on its grey tile, and money in (a credit's amount) 4.5:1 where rows sit", () => {
    const [, pct] = /^bg-muted\/(\d+) text-muted$/.exec(ICON_TINT)!;
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const s of ["sf", "bg"]) expect(ratio(color(v, "mu"), over(color(v, "mu"), color(v, s), Number(pct) / 100)), `${scheme}, icon over ${s}`).toBeGreaterThanOrEqual(3);
      for (const s of ["sf", "bg", "sf2"]) expect(ratio(color(v, "in"), color(v, s)), `${scheme}, money in on ${s}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("a button's text is at least 4.5:1 on the accent; the focus ring at least 3:1 on every surface", () => {
    for (const [scheme, v] of Object.entries(schemes)) {
      expect(ratio(color(v, "acc-ink"), color(v, "acc")), scheme).toBeGreaterThanOrEqual(4.5);
      for (const s of SURFACES) expect(ratio(color(v, "focus"), color(v, s)), `${scheme}, ring on ${s}`).toBeGreaterThanOrEqual(3);
    }
    // The ring is the focus token, in the base rule and in every utility (outline-focus).
    expect(css).toContain(":focus-visible{outline:2px solid var(--focus)");
    expect(css).toMatch(/--color-focus:\s*var\(--focus\);/); // the utilities' outline-focus is the same token
    expect(css.match(/outline:2px solid var\(--acc\)/g)).toBeNull();
  });

  it("rings focus in the page's colour on the chosen invoices' ink bar, at least 3:1 in both schemes", () => {
    expect(barStyle.split(" ")).toContain("[&_:focus-visible]:outline-canvas!");
    for (const [scheme, v] of Object.entries(schemes)) expect(ratio(color(v, "bg"), color(v, "ink")), scheme).toBeGreaterThanOrEqual(3);
  });

  it("marks the current filter by weight as well as colour: medium ink text, which clears 4.5:1 on every surface", () => {
    expect(filterLinkStyle.split(" ")).toEqual(expect.arrayContaining(["font-normal", "aria-[current=page]:font-medium", "aria-[current=page]:text-ink"]));
  });

  it("rounds every component on the one scale (badge, control, card); full circles only for dots and round controls", () => {
    const dir = resolve(__dirname, "../src/components");
    for (const f of readdirSync(dir)) {
      const code = readFileSync(resolve(dir, f), "utf8").replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, ""); // classes, not prose
      const off = code.match(/\brounded-(?:[trblse]{1,2}-)?(?:xs|sm|md|lg|xl|2xl|3xl|\[[^\]]*\])(?![\w-])|(?<![\w-])rounded(?:-[trblse]{1,2})?(?=[\s"`])/g);
      expect(off, f).toBeNull();
    }
  });

  it("dims a section's link by a colour, not by opacity", () => {
    expect(sectionHeadingStyle.split(" ")).toContain("[&_a]:text-muted");
    expect(sectionHeadingStyle).not.toMatch(/opacity/);
  });

  it("the brand (the mark, the current navigation icon) is at least 3:1 where it sits, and is not the proved state's colour", () => {
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const s of ["bg", "sf", "side", "sel"]) expect(ratio(color(v, "brand"), color(v, s)), `${scheme}, brand on ${s}`).toBeGreaterThanOrEqual(3);
      expect(v.brand, scheme).not.toBe(v.ok);
    }
    expect(css).toMatch(/--color-brand:\s*var\(--brand\);/);
  });

  it("money in and out on Acasă's chart are at least 3:1 on a card in both schemes, and neither is a status colour", () => {
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const k of ["in", "out"]) {
        expect(ratio(color(v, k), color(v, "sf")), `${scheme}, ${k}`).toBeGreaterThanOrEqual(3);
        for (const st of ["ok", "wait", "no", "acc"]) expect(v[k], `${scheme}, ${k} vs ${st}`).not.toBe(v[st]);
      }
    }
  });
});

describe("a sheet's state pill", () => {
  it("its text is at least 4.5:1 on its tint, over the sheet", () => {
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const [tone, c] of Object.entries(statePillStyle)) {
        if (!tone) continue; // ink on the inset colour, checked above
        const [, bg, pct, fg] = /^bg-([\w-]+)\/(\d+) text-([\w-]+)$/.exec(c)!;
        const pill = over(color(v, TOKEN[bg]), color(v, "sf"), Number(pct) / 100);
        expect(ratio(color(v, TOKEN[fg]), pill), `${scheme}, ${tone}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});

describe("the hero panel", () => {
  it("its text, muted text and money in and out are at least 4.5:1 on it in both schemes", () => {
    for (const [scheme, v] of Object.entries(schemes)) {
      for (const t of ["hero-ink", "hero-mu", "hero-in", "hero-out"]) {
        expect(ratio(color(v, t), color(v, "hero")), `${scheme}, ${t}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});

/** The last unconditional declaration, including rules inside cascade layers but excluding media queries. */
function decl(sel: string, prop: string): string {
  let found = "";
  parse(css).walkRules(sel, rule => {
    for (let parent: AnyNode | undefined = rule.parent; parent; parent = parent.parent) {
      if (parent.type === "atrule" && parent.name !== "layer") return;
    }
    rule.walkDecls(prop, declaration => { found = declaration.value; });
  });
  if (!found) throw new Error(`no ${prop} in ${sel}`);
  return found;
}
const px = (v: string) => {
  const m = /^([\d.]+)px$/.exec(v);
  if (!m) throw new Error(`not a length in px: ${v}`);
  return Number(m[1]);
};
describe("the phone's tab bar", () => {
  it("keeps a focused element above it: scroll padding at least its height (--tabbar), which holds the Nou button", () => {
    expect(css).toMatch(/@media\(max-width:1099\.98px\)\{html\{scroll-padding-bottom:calc\(var\(--tabbar\) \+ \d+px \+ env\(safe-area-inset-bottom\)\)\}\}/);
    // test/phone.test.tsx checks the bar is --tabbar tall at the bottom edge; its tallest item is Nou, 48px.
    expect(px(decl(":root", "--tabbar"))).toBeGreaterThanOrEqual(48 + 2 * 8);
  });

  it("stands off the page in both schemes: its edge 3:1 on the page, icons 4.5:1, the current tab's brand icon and the counts 3:1", () => {
    for (const [scheme, v] of Object.entries(schemes)) {
      expect(ratio(color(v, "dock-ln"), color(v, "bg")), `${scheme}, edge`).toBeGreaterThanOrEqual(3);
      expect(ratio(color(v, "mu"), color(v, "dock")), `${scheme}, icon`).toBeGreaterThanOrEqual(4.5);
      expect(ratio(color(v, "brand"), color(v, "dock-sel")), `${scheme}, current icon`).toBeGreaterThanOrEqual(3);
      expect(ratio(color(v, "acc"), color(v, "dock")), `${scheme}, count`).toBeGreaterThanOrEqual(3);
    }
  });
});
