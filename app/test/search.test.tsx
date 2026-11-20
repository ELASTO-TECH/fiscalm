// Caută (docs/information-architecture.md, section 3): bounded API search in a dialog
// with a combobox and a grouped listbox; ⌘K / Ctrl+K opens it on the desktop.
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { View } from "@/lib/types";
import { fixtureSearchData } from "@/lib/fixtures";
import App from "@/components/App";
import { search } from "@/components/Search";
import { at, deferred, fixture, fixtureAll, fixtureIdentity, json, searchResult, server, renderApp } from "./helpers";

beforeEach(() => {
  localStorage.clear();
  at("?co=NORD");
});
afterEach(() => vi.unstubAllGlobals());

const find = (view: View, q: string) => search(view, q, fixtureIdentity().me.can, searchResult(view, q));

async function typeQuery(box: HTMLElement, query: string) {
  await userEvent.type(box, query);
  await waitFor(() => expect(within(screen.getByRole("dialog", { name: "Caută" })).getByRole("status")).not.toHaveTextContent("Se încarcă"));
}

const titles = (q: string, v = fixture()) => find(v, q).map((g) => [g.title, g.hits.map((h) => h.title)]);

describe("search", () => {
  it("finds partners, invoices both ways and receipts by name, number or CUI, ignoring diacritics and case", () => {
    expect(titles("alfa")).toEqual([["Parteneri", ["Client Alfa SRL"]], ["Facturi emise", ["Client Alfa SRL"]]]);
    expect(titles("ef-551")).toEqual([["Facturi primite", ["Energie Furnizare SA"]]]);
    expect(titles("BENZIN")).toEqual([["Parteneri", ["Benzinaria Nord"]], ["Bonuri și chitanțe", ["Benzinaria Nord"]]]);
    expect(titles("1012")).toEqual([["Parteneri", ["Brico Nord SRL"]], ["Facturi primite", ["Brico Nord SRL"]]]);
    expect(titles("plati")).toEqual([["Secțiuni", ["Fișiere de plăți"]]]);
    expect(titles("chei api")).toEqual([["Secțiuni", ["Linkuri și chei API"]]]);
    expect(titles("sarcini")).toEqual([["Secțiuni", ["Sarcini"]]]);
    expect(titles("automatizari")).toEqual([["Secțiuni", ["Automatizări"]]]);
    expect(titles("alfa nrd")).toEqual([["Facturi emise", ["Client Alfa SRL"]]]); // every word must match
  });

  it("finds amounts typed the Romanian way or with a dot, exactly once the bani are typed", () => {
    expect(titles("4.800,00")).toEqual([["Facturi emise", ["Client Alfa SRL"]]]);
    expect(titles("4800")).toEqual([["Facturi emise", ["Client Alfa SRL"]]]);
    expect(titles("386,4")).toEqual([["Facturi primite", ["Energie Furnizare SA"]]]);
    expect(titles("386.40")).toEqual([["Facturi primite", ["Energie Furnizare SA"]]]);
    expect(titles("38,6")).toEqual([]); // 38 lei is not 386
    expect(titles("156,40")).toEqual([["Facturi primite", ["Brico Nord SRL"]], ["Bonuri și chitanțe", ["Brico Nord"]]]);
    expect(titles("121")).toEqual([["Facturi emise", ["Client Exemplu SRL", "Client Exemplu SRL", "Client Exemplu SRL",
      "Client Exemplu SRL", "Client Exemplu SRL"]]]); // the storno's -121,00 too
  });

  it("finds a negative amount only among negative ones; an amount without a sign finds both", () => {
    const storno = [["Facturi emise", ["Client Exemplu SRL"]]];
    expect(titles("-121,00")).toEqual(storno);
    expect(titles("-121")).toEqual(storno);
    expect(titles("−121,00")).toEqual(storno); // the typographic minus
    expect(find(fixture(), "-121,00")[0].hits.map((h) => h.to)).toEqual([{ iss: "local-storno" }]);
    expect(titles("-4800")).toEqual([]); // 4.800,00 is positive
    expect(find(fixture(), "121,00")[0].hits).toHaveLength(5); // the four invoices and the storno
  });

  it("finds a tax id with or without its RO prefix, on partners and on invoices alike", () => {
    const brico = [["Parteneri", ["Brico Nord SRL"]], ["Facturi primite", ["Brico Nord SRL"]]];
    expect(titles("1012")).toEqual(brico);
    expect(titles("RO1012")).toEqual(brico);
    expect(titles("ro 1012")).toEqual(brico);
    const s = fixture();
    s.invoices = s.invoices.map((r) => (r.supplier_cui === "1012" ? { ...r, supplier_cui: "RO 1012" } : r));
    s.partners = s.partners.filter((p) => p.cui !== "1012");
    expect(titles("RO1012", s)).toEqual([["Facturi primite", ["Brico Nord SRL"]]]); // stored with the prefix and a space
    expect(titles("1012", s)).toEqual([["Facturi primite", ["Brico Nord SRL"]]]);
  });

  it("shows only sections below two characters, and every section with no query", () => {
    expect(titles("a")).toEqual([["Secțiuni", ["Acasă", "Sarcini", "Vânzări", "Bonuri și chitanțe", "Fișiere de plăți", "Tranzacții", "Parteneri",
      "Rapoarte", "Automatizări", "Setări", "Firme", "Integrări", "Linkuri și chei API", "Bondrop", "Serii de facturi", "Cont", "Utilizatori"]]]);
    expect(find(fixture(), "")[0].hits).toHaveLength(19);
  });

  it("finds Bondrop as a section, opening Setări › Linkuri și chei API", () => {
    const [g] = find(fixture(), "bondrop");
    expect(g.title).toBe("Secțiuni");
    expect(g.hits.map((h) => [h.title, h.sub, h.to])).toEqual([["Bondrop", "Setări", { tab: "setari", sec: "acces" }]]);
  });

  it("searches every company under Toate firmele and names the company on each result", () => {
    const g = find(fixtureAll(), "omega");
    expect(g.map((x) => x.title)).toEqual(["Parteneri", "Facturi emise"]);
    expect(g[0].hits[0].sub).toBe("Client, CUI 6001, STUDIO9");
    expect(g[1].hits.map((h) => h.sub)).toEqual(["nr. STD 0008, 2.100,00 lei, 25 oct, STUDIO9", "nr. STD 0007, 2.100,00 lei, 7 oct, STUDIO9"]);
    expect(find(fixtureAll(), "nord").flatMap((x) => x.hits.map((h) => h.sub)).every((s) => s.endsWith("NORD"))).toBe(true);
    expect(find(fixture(), "omega")).toEqual([]); // one company: only its own
  });
});

describe("Search account capabilities", () => {
  it.each(["admin", "member", "readonly"] as const)("offers only permitted sections to %s, using session capabilities", async (role) => {
    renderApp({ principal: fixtureIdentity(role) });
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(screen.getByRole("button", { name: "Caută" }));
    const dialog = within(screen.getByRole("dialog", { name: "Caută" }));
    const titles = dialog.getAllByRole("option").map(el => el.textContent);
    for (const name of ["Firme", "Integrări", "Utilizatori"]) expect(titles.includes(name + "Setări")).toBe(role === "admin");
    expect(titles.includes("BondropSetări")).toBe(role !== "readonly");
    expect(dialog.getByRole("option", { name: "Cont, Setări" })).toHaveAttribute("href", "?co=NORD&tab=setari&sec=cont");
    expect(dialog.getByRole("option", { name: "Linkuri și chei API, Setări" })).toBeVisible();
  });

  it("honors explicit integration capability without deriving it from role or company metadata", () => {
    const can = { ...fixtureIdentity("member").me.can, manage_integrations: true };
    const hits = search(fixture(), "", can)[0].hits;
    expect(hits.some(hit => hit.title === "Integrări")).toBe(true);
    expect(hits.some(hit => hit.title === "Firme" || hit.title === "Utilizatori")).toBe(false);
    expect(search(fixture(), "", {})).toEqual([]);
  });
});

describe("Caută on a phone", () => {
  it("opens from the header, moves with the arrows, opens a result with Enter and gives focus back on Escape", async () => {
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    const open = screen.getByRole("button", { name: "Caută" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(open);
    const dialog = within(screen.getByRole("dialog", { name: "Caută" }));
    const box = dialog.getByRole("combobox", { name: "Caută" });
    expect(box).toHaveFocus();
    expect(open).toHaveAttribute("aria-expanded", "true");
    await typeQuery(box, "client alfa");
    const list = dialog.getByRole("listbox", { name: "Rezultate" });
    expect(box).toHaveAttribute("aria-controls", list.id);
    expect(within(list).getAllByRole("group").map((g) => g.getAttribute("aria-labelledby") && document.getElementById(g.getAttribute("aria-labelledby")!)?.textContent))
      .toEqual(["Parteneri", "Facturi emise"]);
    const options = within(list).getAllByRole("option");
    expect(options.map((o) => o.getAttribute("href"))).toEqual(["?co=NORD&p=NORD.5001", "?co=NORD&iss=5001"]);
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    expect(box).toHaveAttribute("aria-activedescendant", options[0].id);
    await userEvent.keyboard("{ArrowDown}");
    expect(box).toHaveAttribute("aria-activedescendant", options[1].id);
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{ArrowDown}"); // wraps
    expect(box).toHaveAttribute("aria-activedescendant", options[0].id);
    await userEvent.keyboard("{ArrowUp}");
    expect(box).toHaveAttribute("aria-activedescendant", options[1].id);
    await userEvent.keyboard("{Enter}");
    expect(window.location.search).toBe("?co=NORD&iss=5001");
    expect(screen.queryByRole("dialog", { name: "Caută" })).toBeNull();
    expect(await screen.findByRole("dialog", { busy: false })).toHaveAccessibleName("Client Alfa SRL, 4.800,00 lei");
    await userEvent.click(open);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Caută" })).toBeNull();
    expect(open).toHaveFocus();
  });

  it("leaves Enter and the arrows to an input method that is still composing", async () => {
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(screen.getByRole("button", { name: "Caută" }));
    const box = screen.getByRole("combobox");
    await typeQuery(box, "alfa");
    const first = box.getAttribute("aria-activedescendant");
    for (const init of [{ isComposing: true }, { keyCode: 229 }]) {
      expect(fireEvent.keyDown(box, { key: "ArrowDown", ...init })).toBe(true); // not taken
      expect(box).toHaveAttribute("aria-activedescendant", first!);
      expect(fireEvent.keyDown(box, { key: "Enter", ...init })).toBe(true);
      expect(screen.getByRole("dialog", { name: "Caută" })).toBeInTheDocument();
      expect(window.location.search).toBe("?co=NORD");
    }
    fireEvent.keyDown(box, { key: "Enter" }); // composition over: Enter opens the result
    expect(window.location.search).toBe("?co=NORD&p=NORD.5001");
  });

  it("lets a modified or middle click open a result the browser's way, in a new tab", async () => {
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(screen.getByRole("button", { name: "Caută" }));
    await typeQuery(screen.getByRole("combobox"), "alfa");
    const option = screen.getAllByRole("option")[0];
    for (const init of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { button: 1 }]) {
      expect(fireEvent.click(option, init)).toBe(true); // default not prevented: the browser follows the href
      expect(window.location.search).toBe("?co=NORD");
      expect(screen.getByRole("dialog", { name: "Caută" })).toBeInTheDocument();
    }
    expect(fireEvent.click(option)).toBe(false); // a plain click stays in the app
    expect(window.location.search).toBe("?co=NORD&p=NORD.5001");
  });

  it("keeps focus inside, opens a tapped result, and says when nothing matches", async () => {
    at("?co=NORD&tab=vanzari&f=open");
    renderApp();
    await screen.findByText("Vânzări", { selector: ".ttl" });
    await userEvent.click(screen.getByRole("button", { name: "Caută" }));
    const dialog = within(screen.getByRole("dialog", { name: "Caută" }));
    const box = dialog.getByRole("combobox");
    await userEvent.tab();
    expect(dialog.getByRole("button", { name: "Închide" })).toHaveFocus();
    await userEvent.tab(); // the options are reached with the arrows, not Tab
    expect(box).toHaveFocus();
    await typeQuery(box, "zzzz");
    expect(box).toHaveAttribute("aria-expanded", "false");
    expect(dialog.getByRole("status")).toHaveTextContent("Niciun element");
    await userEvent.clear(box);
    expect(dialog.getByRole("status")).toHaveTextContent(/^$/); // nothing asked yet: nothing announced
    await typeQuery(box, "brico");
    expect(dialog.getByRole("status")).toHaveTextContent(`${dialog.getAllByRole("option").length} rezultate`);
    expect(dialog.getByRole("status")).toHaveClass("vh"); // the list shows it; the count is for screen readers
    await userEvent.click(dialog.getByRole("option", { name: "Brico Nord SRL, Furnizor, CUI 1012" }));
    // A sheet opens over the page it was opened from, keeping its filter.
    expect(window.location.search).toBe("?co=NORD&tab=vanzari&p=NORD.1012&f=open");
  });

  it("under Toate firmele, opens another company's result over the page, in Toate firmele", async () => {
    at("?co=ALL");
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(screen.getByRole("button", { name: "Caută" }));
    await typeQuery(screen.getByRole("combobox"), "LA-77");
    const option = screen.getByRole("option", { name: "Laborator Analize SRL, nr. LA-77, 230,00 lei, 24 oct, STUDIO9" });
    expect(option).toHaveAttribute("href", "?co=ALL&inv=201");
    await userEvent.keyboard("{Enter}");
    expect(window.location.search).toBe("?co=ALL&inv=201");
    expect(await screen.findByRole("dialog", { busy: false })).toHaveAccessibleName(/^Laborator Analize SRL/);
  });

  it("under Toate firmele, opens a section that needs one company in the last company chosen", async () => {
    localStorage.setItem("fiscalm:co", "STUDIO9");
    at("?co=ALL");
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(screen.getByRole("button", { name: "Caută" }));
    await typeQuery(screen.getByRole("combobox"), "tranz");
    const option = screen.getByRole("option", { name: "Tranzacții" });
    expect(option).toHaveAttribute("href", "?co=STUDIO9&tab=tranzactii");
    await userEvent.type(screen.getByRole("combobox"), "{Enter}");
    expect(window.location.search).toBe("?co=STUDIO9&tab=tranzactii");
  });
});

describe("Caută on the desktop", () => {
  beforeEach(() => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: true, media: q, addEventListener() {}, removeEventListener() {} }));
  });

  it("is in the column under Nou, and ⌘K or Ctrl+K opens it, but not while typing in a field", async () => {
    at("?co=NORD&tab=parteneri");
    server();
    const { container } = render(<App />);
    await screen.findByRole("searchbox", { name: "Caută partener sau CUI" });
    const button = within(container.querySelector(".spine") as HTMLElement).getByRole("button", { name: /^Caută/ });
    expect(button).toHaveAttribute("aria-keyshortcuts", "Meta+K Control+K");
    expect(button.previousElementSibling).toHaveAccessibleName("Nou");
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(screen.getByRole("dialog", { name: "Caută" })).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveFocus();
    await userEvent.keyboard("{Control>}k{/Control}"); // already open: nothing more
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.keyboard("{Control>}k{/Control}");
    expect(screen.getByRole("dialog", { name: "Caută" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("searchbox", { name: "Caută partener sau CUI" }));
    await userEvent.keyboard("{Control>}k{/Control}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("leaves ⌘K and Ctrl+K to any editable region and to a composing input method", async () => {
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    await act(async () => {}); // Flush mounted keyboard listeners before synchronous key events.
    for (const value of ["", "true", "plaintext-only", "TRUE"]) {
      const region = document.createElement("div");
      region.setAttribute("contenteditable", value);
      const inner = document.createElement("span"); // editable by inheritance
      region.append(inner);
      document.body.append(region);
      for (const target of [region, inner]) {
        expect(fireEvent.keyDown(target, { key: "k", ctrlKey: true })).toBe(true);
        expect(fireEvent.keyDown(target, { key: "k", metaKey: true })).toBe(true);
      }
      region.remove();
    }
    expect(screen.queryByRole("dialog")).toBeNull();
    const off = document.createElement("div");
    off.setAttribute("contenteditable", "false");
    document.body.append(off);
    expect(fireEvent.keyDown(document.body, { key: "k", ctrlKey: true, isComposing: true })).toBe(true);
    expect(fireEvent.keyDown(document.body, { key: "k", ctrlKey: true, keyCode: 229 })).toBe(true);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(fireEvent.keyDown(off, { key: "k", ctrlKey: true })).toBe(false); // not editable: the shortcut is ours
    expect(screen.getByRole("dialog", { name: "Caută" })).toBeInTheDocument();
    off.remove();
  });
});


it.each(["", "a", "alfa", "alfa nrd", "RO 1012", "4.800,00", "386.40", "38,6", "121", "−121,00", "omega", "nord", "\ufeffro\u00a01012"])(
  "matches the original browser selector for %s, including global date order", (query) => {
    const view = fixtureAll(), expected = fixtureSearchData(view, query), actual = searchResult(view, query);
    for (const key of ["partners", "issued", "invoices", "documents"] as const) {
      expect(actual[key].map((r) => r.id)).toEqual(expected[key].map((r) => r.id));
    }
  },
);

it("ignores obsolete search responses and retries a failed read without claiming no results", async () => {
  const old = deferred<Response>();
  let failing = true;
  const api = renderApp({ search: (url) => {
    const q = new URL(url, "http://local").searchParams.get("q")!;
    return q === "alfa" ? old.promise : failing ? json({}, 500) : json(searchResult(fixture(), q));
  } });
  await screen.findByRole("region", { name: "De plătit" });
  await userEvent.click(screen.getByRole("button", { name: "Caută" }));
  const box = screen.getByRole("combobox");
  await userEvent.type(box, "alfa");
  await waitFor(() => expect(api.calls.some((c) => c.url.includes("q=alfa"))).toBe(true));
  await userEvent.clear(box);
  await typeQuery(box, "brico");
  expect(within(screen.getByRole("dialog", { name: "Caută" })).getByRole("status")).not.toHaveTextContent("Niciun element");
  failing = false;
  await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
  expect(box).toHaveFocus();
  await screen.findByRole("option", { name: "Brico Nord SRL, Furnizor, CUI 1012" });
  old.resolve(json(searchResult(fixture(), "alfa")));
  await waitFor(() => expect(screen.queryByRole("option", { name: /Client Alfa/ })).toBeNull());
});


it("keeps the JavaScript two-code-unit threshold for a single emoji", () => {
  const s = fixture(); s.documents[0].vendor = "🧾";
  expect(searchResult(s, "🧾").documents.map((r) => r.id)).toEqual([s.documents[0].id]);
});
