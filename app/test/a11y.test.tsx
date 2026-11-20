// Accessibility of what the other suites don't cover. Page structure for assistive technology: one h1 per page, a window
// title per section, focus on the new page's h1 when the user opens it with a link or a search result (never on the first
// page, on back or forward, after an action's redirect or from under a sheet), and focus that follows the invoice form's
// switches. Actions that can't be used now are real disabled buttons that say why, and the narrow phone layout lets names
// and states wrap.
import { act, fireEvent, render as plain, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render, markPaidAs } from "./helpers";
import axe from "axe-core";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import { Row } from "@/components/ui";
import { Select } from "@/components/Controls";
import { Sheet } from "@/components/Sheets";
import { useState } from "react";
import { acts, at, fixture, invoicingData, json, paymentFile, paymentFileDetail, server, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());

function open(query: string) {
  act(() => {
    at(`?co=NORD&${query}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

/** Query, the page's h1, the window's title. */
const PAGES: [string, string, string][] = [
  ["", "Acasă", "Acasă, fiscalm, NORD"],
  ["tab=sarcini", `Sarcini ${fixture().task_summary.total}`, "Sarcini, fiscalm, NORD"], // with the number of tasks
  ["tab=vanzari", "Vânzări", "Vânzări, fiscalm, NORD"],
  ["tab=cheltuieli", "Cheltuieli", "Cheltuieli, fiscalm, NORD"],
  ["tab=plati", "Fișiere de plăți", "Fișiere de plăți, fiscalm, NORD"],
  ["tab=tranzactii", "Tranzacții", "Tranzacții, fiscalm, NORD"],
  ["tab=parteneri", "Parteneri", "Parteneri, fiscalm, NORD"],
  ["tab=rapoarte", "Rapoarte", "Rapoarte, fiscalm, NORD"],
  ["tab=rapoarte&month=2026-09", "Raport pentru septembrie 2026", "Rapoarte, fiscalm, NORD"],
  ["tab=automatizari", "Automatizări", "Automatizări, fiscalm, NORD"],
  ["tab=setari", "Setări", "Setări, fiscalm, NORD"],
  ["tab=setari&sec=firme", "Firme", "Firme, fiscalm, NORD"],
  ["tab=setari&sec=integrari", "Integrări", "Integrări, fiscalm, NORD"],
  ["tab=setari&sec=acces", "Linkuri și chei API", "Linkuri și chei API, fiscalm, NORD"],
  ["tab=mai-mult", "Mai mult", "Mai mult, fiscalm, NORD"],
];

const h1 = () => within(screen.getByRole("main")).getAllByRole("heading", { level: 1 });
const tabBar = () => screen.getByRole("navigation", { name: "Secțiuni" });
/** Lets effects and timers queued by the last render run. */
const settle = () => act(() => new Promise((ok) => setTimeout(ok)));

describe("page structure", () => {
  it("gives every page one h1 and the window a title naming the section and the company", async () => {
    at("?co=NORD");
    server();
    const { container } = render(<App />);
    await screen.findByRole("region", { name: "De plătit" });
    for (const [query, title, windowTitle] of PAGES) {
      open(query);
      await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual([title]));
      expect(document.title).toBe(windowTitle);
      expect((await axe.run(container)).violations.map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target) })), query).toEqual([]);
    }
  });

  it("names no company in the window's title under Toate firmele", async () => {
    at("?co=ALL&tab=cheltuieli");
    renderApp();
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual(["Cheltuieli"]));
    await waitFor(() => expect(document.title).toBe("Cheltuieli, fiscalm"));
  });

  it("leaves focus alone on the first page, and moves it to the h1 of a page the user opens with a link", async () => {
    at("?co=NORD");
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    expect(document.activeElement).toBe(document.body);
    await userEvent.click(within(tabBar()).getByRole("link", { name: "Cheltuieli" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Cheltuieli" })));
    expect(document.activeElement).toHaveAttribute("tabindex", "-1"); // focusable from script, not a Tab stop
    await userEvent.click(screen.getByRole("link", { name: "Mai mult" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Mai mult" })));
    await userEvent.click(within(screen.getByRole("main")).getByRole("link", { name: /^Fișiere de plăți/ }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Fișiere de plăți" })));
    // A filter is the same page: focus stays on what was pressed.
    await userEvent.click(within(tabBar()).getByRole("link", { name: "Cheltuieli" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Cheltuieli" })));
    const filter = within(screen.getByRole("navigation", { name: "Filtru" })).getAllByRole("link")[1];
    await userEvent.click(filter);
    await settle();
    expect(document.activeElement).toBe(filter);
  });

  it("leaves focus where it is on back and forward", async () => {
    at("?co=NORD");
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(within(tabBar()).getByRole("link", { name: "Cheltuieli" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Cheltuieli" })));
    await userEvent.click(within(tabBar()).getByRole("link", { name: `Sarcini, ${fixture().task_summary.total} deschise` }));
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual([`Sarcini ${fixture().task_summary.total}`]));
    const pressed = within(tabBar()).getByRole("link", { name: "Acasă" });
    pressed.focus();
    act(() => window.history.back());
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual(["Cheltuieli"]));
    await settle();
    expect(document.activeElement).toBe(pressed);
    act(() => window.history.forward());
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual([`Sarcini ${fixture().task_summary.total}`]));
    await settle();
    expect(document.activeElement).toBe(pressed);
    // Nor any other page the browser opens from its history.
    open("tab=setari&sec=acces");
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual(["Linkuri și chei API"]));
    await settle();
    expect(document.activeElement).toBe(pressed);
  });

  it("leaves focus alone when an action sends the user to another page, and says what it did", async () => {
    at("?co=NORD&tab=cheltuieli");
    server({ onPost: () => json({ ok: true, message: "Am marcat factura ca plătită.", state: fixture(), next: { tab: "plati" } }) });
    render(<App />);
    await userEvent.click(await screen.findByText("Servicii Web SRL"));
    await userEvent.click(within(await screen.findByRole("dialog", { busy: false })).getByRole("button", { name: "Marchează ca plătită" }));
    await markPaidAs("Din contul firmei");
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual(["Fișiere de plăți"]));
    expect(window.location.search).toBe("?co=NORD&tab=plati");
    expect(screen.getByText("Am marcat factura ca plătită.").closest("[aria-live]")).not.toBeNull();
    await settle();
    expect(document.activeElement).not.toBe(h1()[0]);
  });

  it("closes Caută, then focuses the h1 of the section a result opens", async () => {
    at("?co=NORD");
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(screen.getByRole("button", { name: "Caută" }));
    const box = within(await screen.findByRole("dialog", { name: "Caută" })).getByRole("combobox", { name: "Caută" });
    expect(document.activeElement).toBe(box);
    await userEvent.type(box, "rapoarte{Enter}");
    expect(screen.queryByRole("dialog", { name: "Caută" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: "Rapoarte" })));
    expect(window.location.search).toBe("?co=NORD&tab=rapoarte");
  });

  it("does not take focus from a field the user is typing in when the page changes under it", async () => {
    at("?co=NORD");
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    const link = within(tabBar()).getByRole("link", { name: "Cheltuieli" });
    await userEvent.click(screen.getByRole("button", { name: "Caută" }));
    const box = within(await screen.findByRole("dialog", { name: "Caută" })).getByRole("combobox", { name: "Caută" });
    await userEvent.type(box, "fac");
    fireEvent.click(link); // a click that leaves focus where it is, unlike a real one
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual(["Cheltuieli"]));
    await settle();
    expect(document.activeElement).toBe(box);
  });

  it("does not take focus from a sheet opened with the new page", async () => {
    at("?co=NORD");
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    open("tab=vanzari&new=1");
    const sheet = await screen.findByRole("dialog", { name: "Factură nouă" });
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual(["Vânzări"]));
    await settle();
    expect(sheet.contains(document.activeElement)).toBe(true);
  });

  it("focuses the dated h1 of Acasă in a wide window", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: true, media: q, addEventListener() {}, removeEventListener() {} }));
    at("?co=NORD&tab=cheltuieli");
    renderApp();
    await waitFor(() => expect(h1().map((h) => h.textContent)).toEqual(["Cheltuieli"]));
    expect(document.activeElement).toBe(document.body);
    await userEvent.click(within(screen.getByRole("navigation", { name: "Secțiuni" })).getByRole("link", { name: "Acasă" }));
    await waitFor(() => expect(document.activeElement).toBe(h1()[0]));
    expect(h1()).toHaveLength(1);
    expect(document.title).toBe("Acasă, fiscalm, NORD");
  });
});

describe("Factură nouă: focus follows what replaces the pressed control", () => {
  async function form() {
    at("?co=NORD&tab=vanzari&new=1");
    renderApp();
    const d = await screen.findByRole("dialog", { name: "Factură nouă" });
    await waitFor(() => expect(d.querySelector("form")).not.toBeNull());
    return within(d);
  }

  it("Adaugă un client nou focuses the new client's first field; Renunță goes back to the button", async () => {
    const f = await form();
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    expect(document.activeElement).toBe(f.getByLabelText("Persoană fizică"));
    await userEvent.click(f.getByRole("button", { name: "Renunță" }));
    expect(document.activeElement).toBe(f.getByRole("button", { name: "Adaugă un client nou" }));
  });

  it("Modifică datele firmei focuses the company's first field; Renunță goes back to the button", async () => {
    const f = await form();
    await userEvent.click(f.getByRole("button", { name: "Modifică datele firmei de pe factură" }));
    expect(document.activeElement).toBe(f.getByLabelText("Denumire"));
    await userEvent.click(f.getByRole("button", { name: "Renunță" }));
    await waitFor(() => expect(document.activeElement).toBe(f.getByRole("button", { name: "Modifică datele firmei de pe factură" })));
  });

  it("opening the sheet with the new client's form already open leaves focus where the sheet puts it", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    renderApp({ invoicing: () => json({ ...invoicingData(), clients: [] }) });
    const d = await screen.findByRole("dialog", { name: "Factură nouă" });
    await waitFor(() => expect(within(d).getByLabelText("Persoană fizică")).toBeInTheDocument());
    expect(document.activeElement).not.toBe(within(d).getByLabelText("Persoană fizică"));
  });
});

describe("an action that can't be used now", () => {
  it("on Acasă, an action that can be used is enabled", async () => {
    at("?co=NORD");
    renderApp();
    const a = within(await screen.findByText("Pregătește plățile").then(() => acts()));
    expect(a.getByRole("button", { name: "Dosar pentru contabil" })).toBeEnabled(); // a pack is ready
    expect(document.querySelector('[aria-disabled="true"]')).toBeNull();
  });

  it("on Acasă has no accountant tile until a pack is ready", async () => {
    at("?co=STUDIO9");
    const s = fixture("STUDIO9"); s.reports = [];
    renderApp({ state: () => json(s) });
    const a = within(await screen.findByText("Pregătește plățile").then(() => acts()));
    expect(a.queryByRole("button", { name: "Dosar pentru contabil" })).toBeNull();
  });

  it("in Sandbox, leaves out a payment file's download and import instead of disabling them", async () => {
    at("?co=NORD&tab=plati&pf=7");
    const s = fixture();
    s.live = false;
    s.files = [paymentFile({ downloaded_at: "2026-10-26T15:20:00" })];
    const detail = paymentFileDetail({ downloaded_at: "2026-10-26T15:20:00", downloads: [{ id: 3, version: "v3", at: "2026-10-26T15:20:00" }], can_import: true });
    renderApp({ state: () => json(s), paymentFile: () => json(detail) });
    const sheet = await screen.findByRole("dialog", { name: detail.name });
    expect(within(sheet).queryByRole("group", { name: "Fișier de plăți" })).toBeNull();
    for (const name of [/^Descarcă/, "Am importat în bancă"]) expect(within(sheet).queryAllByRole("button", { name })).toEqual([]);
    expect(within(sheet).getByRole("button", { name: "Redenumește" })).toBeEnabled(); // local work stays
    expect(document.querySelector("button:disabled")).toBeNull();
  });
});

describe("a narrow phone", () => {
  it("lets a list row's name and state wrap instead of being cut", () => {
    const { container } = plain(<Row icon="receipt" title="Furnizor cu un nume foarte lung SRL" sub="De plătit, scadentă pe 6 noiembrie" val="1.500,00 lei" />);
    const text = container.querySelector(".row .t") as HTMLElement;
    expect(text).toHaveClass("[&_b]:whitespace-normal");
    expect(text).toHaveClass("[&_small]:block");
    expect(text.className).not.toMatch(/truncate|line-clamp/);
    expect(text).toHaveClass("[&_b]:[overflow-wrap:anywhere]"); // a long word breaks too
    expect(container.querySelector(".row .v")).toHaveClass("[overflow-wrap:anywhere]");
  });
});

describe("the app's select", () => {
  it("keeps its label as its name and its list's, keeps the highlight on its value when options change, and picks nothing once disabled", async () => {
    function Firms({ list, off }: { list: string[]; off: boolean }) {
      const [v, setV] = useState("");
      return <><label htmlFor="f">Firma</label><fieldset disabled={off}><Select id="f" value={v} onChange={(e) => setV(e.target.value)}>
        <option value="">Alege</option>{list.map((x) => <option key={x} value={x}>{x}</option>)}
      </Select></fieldset></>;
    }
    const { rerender } = plain(<Firms list={["NORD", "STUDIO9", "ZETA"]} off={false} />);
    const box = screen.getByRole("combobox", { name: "Firma" });
    box.focus();
    await userEvent.keyboard("{ArrowDown}{End}");
    expect(screen.getByRole("listbox", { name: "Firma" })).toBeInTheDocument();
    expect(box.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: "ZETA" }).id);
    rerender(<Firms list={["ZETA", "NORD"]} off={false} />); // options load while it is open: ZETA stays highlighted
    expect(box.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: "ZETA" }).id);
    await userEvent.keyboard("{Enter}");
    expect(box).toHaveValue("ZETA");
    expect(screen.getByRole("combobox", { name: "Firma" })).toBe(box); // the choice is its value, not its name
    await userEvent.click(box);
    rerender(<Firms list={["ZETA", "NORD"]} off />);
    await userEvent.click(screen.getByRole("option", { name: "NORD" }));
    expect(box).toHaveValue("ZETA");
  });

  it("in a sheet, a first Escape closes only its list, a second the sheet", async () => {
    const onClose = vi.fn();
    plain(<Sheet label="Foaie" onClose={onClose}><Select aria-label="Moneda" value="RON"><option value="RON">Lei</option><option value="EUR">Euro</option></Select></Sheet>);
    const box = screen.getByRole("combobox", { name: "Moneda" });
    await userEvent.click(box);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(box).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("inside a label around it, is named by the label's own text and a pick closes its list", async () => {
    function Pay() {
      const [v, setV] = useState("cash");
      return <label>Plată <Select value={v} onChange={(e) => setV(e.target.value)}><option value="cash">Numerar</option><option value="card">Card</option></Select></label>;
    }
    plain(<Pay />);
    const box = screen.getByRole("combobox", { name: "Plată" });
    await userEvent.click(box);
    expect(screen.getByRole("listbox", { name: "Plată" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("option", { name: "Card" }));
    expect(box).toHaveValue("card");
    expect(box).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("closes on a click outside", async () => {
    plain(<><Select aria-label="Luna" value=""><option value="">Toate</option></Select><p>Text</p></>);
    await userEvent.click(screen.getByRole("combobox", { name: "Luna" }));
    await userEvent.click(screen.getByText("Text")); // not focusable: only the outside click can close it
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("closes when focus leaves it", async () => {
    plain(<><Select aria-label="Luna" value=""><option value="">Toate</option></Select><button type="button">Altceva</button></>);
    await userEvent.click(screen.getByRole("combobox", { name: "Luna" }));
    expect(screen.getByRole("listbox", { name: "Luna" })).toBeInTheDocument();
    act(() => screen.getByRole("button", { name: "Altceva" }).focus());
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  function Month() {
    const [v, setV] = useState("");
    return <><label htmlFor="m">Luna</label><Select id="m" value={v} onChange={(e) => setV(e.target.value)}>
      <option value="">Toate lunile</option><option value="2026-11">Noiembrie 2026</option>
      <option value="2026-10" disabled>Octombrie 2026</option><option value="2026-09">Septembrie 2026</option>
    </Select><button type="button">După</button></>;
  }
  it("is a select-only combobox: keys move and pick, letters jump, Escape and a click outside close, focus stays on it", async () => {
    plain(<Month />);
    const box = screen.getByRole("combobox", { name: "Luna" });
    expect(box).toHaveAttribute("aria-expanded", "false");
    expect(box).toHaveTextContent("Toate lunile");
    box.focus();
    await userEvent.keyboard("{ArrowDown}");
    const list = screen.getByRole("listbox");
    expect(box).toHaveAttribute("aria-expanded", "true");
    expect(within(list).getByRole("option", { name: "Toate lunile" })).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{ArrowDown}{ArrowDown}"); // the disabled October is skipped
    expect(box.getAttribute("aria-activedescendant")).toBe(within(list).getByRole("option", { name: "Septembrie 2026" }).id);
    await userEvent.keyboard("{Enter}");
    expect(box).toHaveValue("2026-09");
    expect(box).toHaveFocus();
    expect(screen.queryByRole("listbox")).toBeNull();
    await userEvent.keyboard("n"); // a letter opens on the choice it starts
    expect(box.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: "Noiembrie 2026" }).id);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(box).toHaveValue("2026-09");
    expect(box).toHaveFocus();
    await userEvent.click(box);
    await userEvent.click(screen.getByRole("button", { name: "După" }));
    expect(screen.queryByRole("listbox")).toBeNull();
    await userEvent.click(box);
    await userEvent.click(screen.getByRole("option", { name: "Toate lunile" }));
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
  });
});
