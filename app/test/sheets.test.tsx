import { act, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import axe from "axe-core";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Sheet } from "@/components/Sheets";
import { dropCopies, rowRef, rowsMayLeave, rowsStay } from "@/components/Controls";
import { at, deferred, detailResult, fixture, json, partnerResult, renderApp } from "./helpers";
import paymentFiles from "../fixtures/payment-files.json";

beforeEach(() => at("?co=NORD&tab=cheltuieli"));

describe("Sheet", () => {
  it("traps Tab and Shift+Tab inside, makes the page inert, restores focus to the opener", async () => {
    renderApp();
    const opener = (await screen.findByText("Energie Furnizare SA")).closest("a") as HTMLElement;
    opener.focus();
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    await userEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { busy: false });
    expect(dialog).toHaveFocus();
    // Opaque: the card surface, no transparency or blur (test/contrast.test.ts checks --sf in both schemes).
    expect(dialog).toHaveClass("bg-surface");
    expect(dialog.className).not.toMatch(/opacity|backdrop|bg-\S+\/\d/);
    expect(focus.mock.calls.some(([options]) => options?.preventScroll === true)).toBe(true);
    expect((await axe.run(dialog)).violations.map((v) => v.id)).toEqual([]);
    expect(document.querySelector(".page")).toHaveAttribute("inert");
    expect(document.querySelector(".tabs")).toHaveAttribute("inert");
    expect(document.querySelector('[role="status"]')).not.toHaveAttribute("inert");

    const inside = [...dialog.querySelectorAll<HTMLElement>("a[href],button:not([disabled])")];
    const [first, last] = [inside[0], inside[inside.length - 1]];
    await userEvent.tab();
    expect(first).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(last).toHaveFocus();
    await userEvent.tab();
    expect(first).toHaveFocus();
    for (let i = 0; i < inside.length + 2; i++) {
      await userEvent.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.querySelector(".page")).not.toHaveAttribute("inert");
    expect(document.querySelector(".tabs")).not.toHaveAttribute("inert");
    expect(opener).toHaveFocus();
    expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
    focus.mockRestore();
  });

  it("keeps the focus when onClose changes, and calls the latest one", async () => {
    const [a, b] = [vi.fn(), vi.fn()];
    const ui = (onClose: () => void) => (
      <>
      <div key={onClose === a ? "phone" : "desktop"}>Fundal</div>
      <Sheet onClose={onClose} label="Test">
        <input type="hidden" />
        <select disabled aria-label="Selectare indisponibilă" />
        <button type="button">Unu</button>
        <button type="button">Doi</button>
        <textarea disabled aria-label="Notă indisponibilă" />
      </Sheet>
      </>
    );
    const { rerender } = render(ui(a));
    screen.getByRole("button", { name: "Doi" }).focus();
    rerender(ui(b));
    expect(screen.getByRole("button", { name: "Doi" })).toHaveFocus();
    await waitFor(() => expect(screen.getByText("Fundal")).toHaveAttribute("inert"));
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Unu" })).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Doi" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(a).not.toHaveBeenCalled();
    expect(b).toHaveBeenCalledOnce();
    act(() => (document.querySelector(".scrim") as HTMLElement).click());
    expect(b).toHaveBeenCalledTimes(2);
  });

  it("wraps through a closed disclosure's summary, never to the fields it hides", async () => {
    render(<Sheet onClose={() => {}} label="Test">
      <button type="button">Unu</button>
      <details><summary>Setări avansate</summary><input aria-label="Ascuns" /></details>
    </Sheet>);
    const [first, summary] = [screen.getByRole("button", { name: "Unu" }), screen.getByText("Setări avansate")];
    first.focus();
    await userEvent.tab({ shift: true });
    expect(summary).toHaveFocus();
    await userEvent.tab();
    expect(first).toHaveFocus();
  });

  // A closed sheet leaves at once (focus back, page usable); only an unreachable copy fades, and never under reduced motion.
  it.each([[false, 1], [true, 0]])("closes at once and fades a copy unless motion is reduced (%s)", async (reduce, copies) => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: reduce && q.includes("reduce"), media: q, addEventListener() {}, removeEventListener() {} }));
    function Opener() {
      const [open, setOpen] = useState(false);
      return <><button type="button" onClick={() => setOpen(true)}>Deschide</button>
        {open ? <Sheet onClose={() => setOpen(false)} label="Test"><button type="button" id="unu">Unu</button></Sheet> : null}</>;
    }
    render(<Opener />);
    await userEvent.click(screen.getByRole("button", { name: "Deschide" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Deschide" })).toHaveFocus();
    expect(document.querySelector(".sheet, .scrim, [role=dialog], #unu")).toBeNull();
    await act(async () => {});
    const ghosts = document.querySelectorAll("[inert][aria-hidden=true].animate-exit-fade");
    expect(ghosts).toHaveLength(copies * 2);
    vi.unstubAllGlobals();
  });

  // A copy points at nothing live and goes when identity is checked again; a sheet showing a secret leaves no copy.
  it.each([[false, 2], [true, 0]])("copies are unlinked, dropped on demand, never of a secret (%s)", async (secret, copies) => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));
    const ui = (open: boolean) => open ? <Sheet onClose={() => {}} label="Cont"><label htmlFor="rol">Rol</label><input id="rol" name="rol" />
      {secret ? <code>https://exemplu.invalid/setup#token</code> : null}</Sheet> : null;
    const { rerender } = render(ui(true));
    rerender(ui(false));
    await act(async () => {});
    const found = document.querySelectorAll("[data-leaving]");
    expect(found).toHaveLength(copies);
    expect([...found].flatMap((c) => [...c.querySelectorAll("[for], [name], [id]")])).toEqual([]);
    dropCopies();
    expect(document.querySelectorAll("[data-leaving]")).toHaveLength(0);
    vi.unstubAllGlobals();
  });

  // A sheet that replaces another in one commit (its content read) doesn't enter again or fade the old one over it; closing it
  // later fades it like any other.
  it("replaces a sheet without motion, then fades the replacement out", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));
    const ui = (step: string) => step ? <Sheet key={step} onClose={() => {}} label={step}>{step}</Sheet> : null;
    const copies = () => document.querySelectorAll("[data-leaving]");
    const { rerender } = render(ui("Se încarcă"));
    rerender(ui("Factura"));
    await act(async () => {});
    expect(copies()).toHaveLength(0);
    expect(screen.getByRole("dialog")).toHaveStyle({ animation: "none" });
    rerender(ui(""));
    await act(async () => {});
    expect([...copies()].map((c) => [c.getAttribute("style") ?? "", c.classList.contains("animate-exit-fade")])).toEqual([["", true], ["", true]]);
    vi.unstubAllGlobals();
  });

  // Only a row an action took away closes up, where it was; a list that changes otherwise, or under reduced motion, changes at once.
  it.each([[false, false, 0], [true, true, 0], [true, false, 1]])("a leaving row collapses in place (action %s, reduced %s)", async (action, reduce, copies) => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: reduce && q.includes("reduce"), media: q, addEventListener() {}, removeEventListener() {} }));
    const list = (ids: string[]) => <div data-testid="list">{ids.map((id) => <div key={id} ref={rowRef}>{id}</div>)}</div>;
    const { rerender } = render(list(["a", "b", "c"]));
    if (action) rowsMayLeave();
    rerender(list(["a", "c"]));
    await act(async () => {});
    const rows = [...screen.getByTestId("list").children] as HTMLElement[];
    expect(rows.map((r) => [r.textContent, r.hasAttribute("inert")])).toEqual(copies ? [["a", false], ["b", true], ["c", false]] : [["a", false], ["c", false]]);
    if (copies) expect(rows[1]).toHaveClass("animate-exit-collapse");
    vi.unstubAllGlobals();
  });

  // Rows that left together keep their places; a list none of whose rows stayed (another filter or firm) is new, shown at once.
  it.each([[["a", "d"], "a b* c* d"], [["x", "y"], "x y"]])("after an action, %j shows %s", async (after, shown) => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }));
    const list = (ids: string[]) => <div data-testid="list">{ids.map((id) => <div key={id} ref={rowRef}>{id}</div>)}</div>;
    const { rerender } = render(list(["a", "b", "c", "d"]));
    rowsMayLeave();
    rerender(list(after));
    await act(async () => {});
    expect([...screen.getByTestId("list").children].map((r) => r.textContent + (r.hasAttribute("inert") ? "*" : "")).join(" ")).toBe(shown);
    // Another firm or page: no copy of the last one's rows stays over it.
    rowsStay();
    expect(document.querySelectorAll("[data-leaving]")).toHaveLength(0);
    vi.unstubAllGlobals();
  });
});

describe("Sheet being read", () => {
  // Its skeleton opens as wide and as tall as the sheet it becomes (SHEET_SHAPES), so the answer takes its place in place.
  it.each([
    ["?co=NORD&tab=cheltuieli&inv=102", "wide full"],
    ["?co=NORD&tab=parteneri&p=NORD.1001", "wide full"],
    ["?co=NORD&tab=plati&pf=1", "tall full"],
  ])("opens %s with the placement of its answer", async (page, cls) => {
    at(page);
    const reply = deferred<void>();
    const query = (url: string) => Object.fromEntries(new URL(url, "http://local").searchParams);
    renderApp({ detail: (url) => reply.promise.then(() => json(detailResult(fixture(), query(url)))),
      partnerPages: (url) => reply.promise.then(() => json(partnerResult(fixture(), query(url)))),
      paymentFile: () => reply.promise.then(() => json(paymentFiles[1])) });
    const loading = await screen.findByRole("dialog", { busy: true });
    expect(loading).toHaveClass(...cls.split(" "));
    reply.resolve();
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet).toHaveClass(...cls.split(" "));
  });
});

describe("Invoice lines", () => {
  it("leaves the section out when the document can't be read", async () => {
    at("?co=NORD&tab=vanzari&iss=5002");
    renderApp({ detail: (url) => json({ ...detailResult(fixture(), Object.fromEntries(new URL(url, "http://local").searchParams)),
      lines: { lines: [], currency: "", truncated: false, unavailable: true } }) });
    const sheet = await screen.findByRole("dialog", { busy: false });
    await waitFor(() => expect(within(sheet).queryByRole("region", { name: "Produse și servicii" })).toBeNull());
  });

  const line = { position: 1, description: "Abonament internet fibră", quantity: "2.5", unit: "buc", unit_price: "12.4", net: "31.00", vat_rate: "21", vat_category: "S", vat: "6.51", gross: "37.51" };
  // No VAT rate on the document: its VAT and total stay unknown, never the net shown as a total.
  const bare = { ...line, position: 2, description: "Taxă", quantity: "1", unit_price: "100", net: "100.00", vat_rate: null, vat: null, gross: null };
  const totals = { net: "131.00", vat: "17.51", total: "148.51", payable: "100.00",
    taxes: [{ category: "S", rate: "21", net: "31.00", vat: "6.51" }, { category: "S", rate: "11", net: "100.00", vat: "11.00" }] };
  it.each([
    ["?co=NORD&tab=cheltuieli&inv=102", "kind=invoice&id=102", { lines: [line, bare], currency: "RON", truncated: false, unavailable: false, totals }],
  ])("shows the lines of the detail it requested for %s, with the document's totals", async (page, query, lines) => {
    const urls: string[] = [];
    at(page);
    renderApp({ detail: (url) => {
      urls.push(url);
      return json({ ...detailResult(fixture(), Object.fromEntries(new URL(url, "http://local").searchParams)), lines });
    } });
    const section = within(await screen.findByRole("dialog", { busy: false })).getByRole("region", { name: "Produse și servicii" });
    const [table, footer] = within(section).getAllByRole("table");
    const cells = (t: HTMLElement) => within(t).getAllByRole("row").map((r) => [...r.children].map((c) => c.textContent));
    expect(cells(table)[1]).toEqual(["Abonament internet fibră", "2,5", "buc", "12,40", "21%", "31,00", "37,51"]);
    expect(cells(table)[2]).toEqual(["Taxă", "1", "buc", "100,00", "-", "100,00", "-"]);
    expect([...section.querySelectorAll("li")].map((li) => li.textContent)).toEqual([
      "Abonament internet fibră37,512,5 × 12,40 lei/buc, TVA 21%, fără TVA 31,00", "Taxă-1 × 100,00 lei/buc, fără TVA 100,00"]);
    expect(cells(footer)).toEqual([["Total fără TVA", "131,00"], ["TVA 21%", "6,51"], ["TVA 11%", "11,00"],
      ["Total cu TVA", "148,51"], ["Total de plată", "100,00 lei"]]);
    expect(urls).toEqual([`/web/api/detail?co=NORD&${query}&cursor=&limit=50`]);
  });
});

describe("Document corrections", () => {
  it("hides the editor for a document the engine refuses", async () => {
    const s = fixture(); const d = s.documents.find((x) => x.mail)!;
    d.can_correct = false;
    at(`?co=NORD&tab=cheltuieli&doc=${d.id}`);
    renderApp({ state: () => json(s), detail: (url) => json(detailResult(s, Object.fromEntries(new URL(url, "http://local").searchParams))) });
    const dialog = await screen.findByRole("dialog", { busy: false });
    expect(within(dialog).queryByRole("button", { name: "Corectează" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "Adaugă suma" })).toBeNull();
  });
  it.each([null, "49.01"])("offers an amount action for %s and posts exact metadata", async (amount) => {
    const s = fixture();
    const d = s.documents.find((x) => x.mail)!;
    d.amount = amount;
    d.currency = "PLN";
    at(`?co=NORD&tab=cheltuieli&doc=${d.id}`);
    const api = renderApp({ state: () => json(s), detail: (url) => json(detailResult(s, Object.fromEntries(new URL(url, "http://local").searchParams))) });
    await userEvent.click(await screen.findByRole("button", { name: amount === null ? "Adaugă suma" : "Corectează" }));
    const input = screen.getByRole("textbox", { name: "Sumă" });
    await userEvent.clear(input);
    await userEvent.type(input, "125,4");
    expect(screen.getByRole("combobox", { name: "Monedă" })).toHaveTextContent("PLN");
    await userEvent.click(screen.getByRole("button", { name: "Salvează" }));
    await waitFor(() => expect(api.posts().map((p) => p.body)).toContainEqual({ co: "NORD", id: d.id, amount: "125.40", currency: "PLN", date: d.date ?? "", number: d.number ?? d.mail?.number ?? "" }));
  });
});
