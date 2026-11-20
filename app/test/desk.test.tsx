import { act, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import { says } from "@/components/Desk";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import { homeTasks } from "@/components/Home";
import { fixtureHomeTasks } from "@/lib/fixtures";
import { parts } from "@/lib/types";
import taskNord from "../fixtures/tasks-NORD.json";
import taskStudio from "../fixtures/tasks-STUDIO9.json";
import taskAll from "../fixtures/tasks-ALL.json";
import { at, fixture, fixtureAll, json, paymentFile, server, renderApp, summaryResult } from "./helpers";

// A wide window: matchMedia answers the desktop query.
beforeEach(() => {
  localStorage.clear();
  at("?co=NORD");
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: true, media: q, addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => vi.unstubAllGlobals());

it.each([
  ["ACME S.R.L.", "ACME SRL", ""], ["Studio 9 SRL", "STUDIO9", ""], ["Atelier Nord SRL", "NORD", "Atelier Nord SRL"],
  ["CA SRL", "Casa", "CA SRL"], ["CA S.A.", "CASA", "CA S.A."],
])("shows the legal name %s under %s only when it says more", (legal, name, sub) => {
  expect(says(legal, name)).toBe(sub);
});

describe("desktop", () => {
  it("keeps exact task counts, highlights and file money without historical root arrays", async () => {
    const full = fixture(); full.files = [paymentFile({ id: 9, total: "3000.00" }), paymentFile({ id: 8, total: "1871.00" })];
    const compact = summaryResult(full);
    expect(compact.invoices).toEqual([]);
    expect(compact).not.toHaveProperty("payments");
    renderApp({ state: () => json(compact) });
    await screen.findByRole("region", { name: "De plătit" });
    expect(document.querySelectorAll(".dk-home .asks .task")).toHaveLength(compact.task_summary.highlights.length);
    expect(screen.getByRole("link", { name: `Toate (${compact.task_summary.total})` })).toHaveAttribute("href", "?co=NORD&tab=sarcini");
    expect(within(screen.getByRole("navigation", { name: "Secțiuni" })).getByRole("link", { name: /^Sarcini/ })).toHaveTextContent(String(compact.task_summary.total));
    expect(screen.getByRole("link", { name: /În fișiere de plăți/ })).toHaveTextContent("4.871,00 lei");
  });
  it("keeps account settings inside the desktop navigation and Sandbox banners", async () => {
    at("?co=NORD&tab=setari&sec=cont");
    const state = fixture();
    state.live = false;
    renderApp({ state: () => json(state) });
    await waitFor(() => expect(screen.getByRole("heading", { name: "Cont", level: 1 }).closest("#main-content")).not.toBeNull());
    expect(screen.getByRole("navigation", { name: "Secțiuni" })).toBeInTheDocument();
    expect(screen.getByText("Sandbox:", { selector: "strong" }).parentElement?.parentElement)
      .toBe(document.querySelector("#main-content")?.parentElement);
  });

  it("keeps account access available before the first firm on desktop", async () => {
    const setup = { setup: "needs_company", companies: [], live: false, can_manage_companies: true };
    renderApp({ state: () => json(setup), all: () => json(setup), companies: () => json({ companies: [], palettes: [], limits: { key_pattern: "", reserved_keys: [], name_max: 100 } }) });
    await screen.findByRole("heading", { name: "Prima firmă", level: 1 });
    act(() => {
      at(screen.getByRole("link", { name: "Cont" }).getAttribute("href")!);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("heading", { name: "Cont", level: 1 })).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector(".dk")).toBeNull());
  });

  it("shows the company column and Acasă's numbers in columns, the same as on the phone", async () => {
    server({ state: () => json(fixture()) });
    const { container } = render(<App />);
    await screen.findByRole("region", { name: "De plătit" });
    expect(container.querySelector(".tabs")).toBeNull(); // no phone tab bar
    // The toast's region, a sibling of the layout, knows the column's width, so a message centres on the page beside it.
    const width = (el: Element | null) => [...(el?.classList ?? [])].find((c) => c.startsWith("[--sp:"));
    expect(width(container.querySelector('[role="status"][aria-live="polite"]'))).toBe(width(container.querySelector(".dk")));
    expect(width(container.querySelector(".dk"))).toBeDefined();
    // Titled as on the phone, with the day under it.
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^Acasă$/);
    expect(container.querySelector(".dk-home .hdr p")).toHaveTextContent("Marți, 27 octombrie");
    expect([...container.querySelectorAll(".dk-home .dash h2")].map((h) => h.textContent)).toEqual(
      ["Încasat și cheltuit", "Conturi bancare", "Facturi emise, după scadență", "Facturi primite, după scadență", "TVA colectată și deductibilă",
        "Clienți principali", "Furnizori principali", "Vânzări pe produse",
        "La curieri și procesatori", "Următoarele 30 de zile"]);
    expect(container.querySelector(".dk-home .strip, .dk-home .reg")).toBeNull(); // the old calendar moved into the 30 days
  });

  it("names the company switch with what it shows, and closes its menu when Tab leaves it", async () => {
    server({ state: () => json(fixture()) });
    const { container } = render(<App />);
    const summary = await screen.findByLabelText(/^NORD, Atelier Nord SRL, alege firma/);
    expect(summary).toHaveTextContent(/NORD\s*Atelier Nord SRL/); // the visible text, in the name
    expect(summary.querySelector(".mk")).toHaveAttribute("aria-hidden", "true"); // the mark letter isn't read
    const menu = container.querySelector(".comenu") as HTMLDetailsElement;
    await userEvent.click(summary);
    expect(menu.open).toBe(true);
    const options = within(menu).getAllByRole("link");
    await userEvent.tab();
    expect(options[0]).toHaveFocus();
    await waitFor(() => expect(menu.open).toBe(true)); // still inside: stays open
    for (let i = 1; i <= options.length; i++) await userEvent.tab();
    expect(menu).not.toContainElement(document.activeElement as HTMLElement);
    await waitFor(() => expect(menu.open).toBe(false));
  });

  it("lists the sections in the column, grouped, with the open tasks and the late invoices counted", async () => {
    at("?co=NORD&tab=cheltuieli");
    server({ state: () => json(fixture()) });
    const { container } = render(<App />);
    const nav = within(await screen.findByRole("navigation", { name: "Secțiuni" }));
    const groups = nav.getAllByRole("group").map((g) => [g.getAttribute("aria-label"),
      within(g).getAllByRole("link").map((a) => a.textContent)]);
    // Counts follow the engine, including the chart fixtures' overdue invoices.
    expect(groups).toEqual([
      [null, ["Acasă", `Sarcini${fixture().task_summary.total}`]],
      ["Bani", ["Vânzări", `Cheltuieli${fixture().payable.late} restante`, "Fișiere de plăți", `Tranzacții${fixture().transactions.to_explain}`]], // De explicat counted
      ["Firma", ["Parteneri", "Produse", "Rapoarte", "Automatizări"]],
    ]);
    expect(nav.getByRole("link", { name: /Cheltuieli/ })).toHaveAttribute("aria-current", "page");
    expect(nav.getByRole("link", { name: "Vânzări" })).toHaveAttribute("href", "?co=NORD&tab=vanzari");
    const spine = within(container.querySelector(".spine") as HTMLElement);
    // The wordmark, as on the site: a mark that is only drawn, then the product's name.
    const wm = container.querySelector(".spine .wm") as HTMLElement;
    expect(wm).toHaveTextContent(/^fiscalm$/);
    expect(wm.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(wm.querySelector("path")).toHaveAttribute("d", "M3 10.95C5.6 5.75 9.6 5.75 12 10.95H21M3 16.95H21");
    expect(spine.getByRole("link", { name: /^Setări/ })).toHaveTextContent("SetăriVerificat 27 oct, 09:10");
    const nou = spine.getByRole("button", { name: "Nou" });
    expect(nou).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(nou);
    expect(nou).toHaveAttribute("aria-expanded", "true");
    const menu = screen.getByRole("dialog", { name: "Nou" });
    expect(menu).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Nou" })).toBeNull();
    expect(nou).toHaveFocus();
    await userEvent.click(nou);
    await userEvent.click(within(screen.getByRole("dialog", { name: "Nou" })).getByRole("link", { name: "Factură nouă" }));
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&new=1");
    expect(screen.queryByRole("dialog", { name: "Nou" })).toBeNull();
    // Navigation only: no amounts, no payment button.
    expect(container.querySelector(".spine")!.textContent).not.toMatch(/lei|De plătit|Pregătește plățile/);
  });

  it("counts every task the same in the column, on Acasă and in Sarcini, though Acasă shows only the three most urgent", async () => {
    const s = fixture();
    s.to_check = [1, 2, 3, 4].map((n) => ({ ...s.to_check[0], key: `${n}`.repeat(64), subject: `Comanda ${n}`, supplier: `Comanda ${n}` }));
    const count = fixture().task_summary.total + s.to_check.length - fixture().to_check.length;
    server({ state: () => json(s) });
    const { container } = render(<App />);
    await screen.findByRole("region", { name: "De plătit" });
    const badge = () => within(container.querySelector(".spine nav") as HTMLElement).getByRole("link", { name: /^Sarcini/ })
      .querySelector(".n")?.textContent;
    expect(badge()).toBe(String(count));
    expect(container.querySelectorAll(".dk-home .asks .task")).toHaveLength(3);
    expect(container.querySelectorAll(".dk-home .asks .task .ic.late")).toHaveLength(3);
    await userEvent.click(screen.getByRole("link", { name: `Toate (${count})` }));
    await screen.findByRole("heading", { name: `Sarcini ${count}` });
    expect(container.querySelectorAll(".tasks .task")).toHaveLength(count);
    expect(badge()).toBe(String(count));
  });

  it("keeps every number the column used to show on Acasă in the cards, each opening its list", async () => {
    const s = fixture(); s.files = [paymentFile({ id: 9, total: "3000.00" }), paymentFile({ id: 8, total: "1871.00" }), paymentFile({ id: 5, status: "imported", total: "99.00" })];
    server({ state: () => json(s) });
    const { container } = render(<App />);
    await screen.findByRole("region", { name: "De plătit" });
    const pay = within(screen.getByRole("region", { name: "De plătit" }));
    expect(pay.getByRole("link", { name: /^De plătit/ })).toHaveTextContent("De plătit9.392,30leiși 20,00 EUR");
    expect(pay.getByRole("link", { name: /^De plătit/ })).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&f=open");
    expect(within(screen.getByRole("region", { name: "De încasat" })).getByRole("link", { name: /^De încasat/ }))
      .toHaveTextContent("De încasat9.300,00leiși 900,00 EUR");
    expect(within(screen.getByRole("region", { name: "Facturi primite, după scadență" })).getByRole("link", { name: /^În curs de confirmare/ }))
      .toHaveAttribute("href", "?co=NORD&tab=cheltuieli");
    expect(pay.getByRole("link", { name: /^În fișiere de plăți/ })).toHaveTextContent("4.871,00 lei");
    expect(within(container.querySelector(".dk-home .hdr") as HTMLElement).getByRole("link", { name: "Pregătește plățile" }))
      .toHaveAttribute("href", "?co=NORD&tab=cheltuieli");
    expect(container.querySelectorAll(".dk-home .money")).toHaveLength(2); // De plătit and De încasat
    expect(container.querySelector(".dk-home .totals")).toBeNull();
  });

  it("offers no new invoice under Toate firmele, where it isn't clear which company issues it", async () => {
    at("?co=ALL");
    server({});
    const { container } = render(<App />);
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(within(container.querySelector(".spine") as HTMLElement).getByRole("button", { name: "Nou" }));
    expect(within(screen.getByRole("dialog", { name: "Nou" })).getByRole("button", { name: "Factură nouă" })).toBeDisabled();
  });

  it("switches the company from the dropdown", async () => {
    server({ state: () => json(fixture()) });
    const { container } = render(<App />);
    await screen.findByRole("region", { name: "De plătit" });
    await userEvent.click(container.querySelector(".comenu summary") as HTMLElement);
    await userEvent.click(container.querySelector('.comenu .menu a[href="?co=STUDIO9"]') as HTMLElement);
    await waitFor(() => expect(window.location.search).toContain("co=STUDIO9"));
  });

  it("shows every firm under Toate firmele: one line per firm in Pe firme, and each due date naming its firm", async () => {
    at("?co=ALL");
    renderApp({});
    await screen.findByRole("region", { name: "De plătit" });
    const firms = within(screen.getByRole("region", { name: "Pe firme" })).getAllByRole("link");
    expect(firms.map((a) => a.getAttribute("href"))).toEqual(["?co=NORD", "?co=STUDIO9"]);
    const ahead = within(screen.getByRole("region", { name: "Următoarele 30 de zile" })).getAllByRole("link");
    expect(ahead.some((a) => a.textContent!.includes("NORD,"))).toBe(true);
    expect(ahead.some((a) => a.textContent!.includes("STUDIO9,"))).toBe(true);
    expect(ahead.every((a) => /^\?co=(NORD|STUDIO9)&/.test(a.getAttribute("href")!))).toBe(true);
  });

  it("lists a payment of a file missing from the folder among the next 30 days while its due date is ahead", async () => {
    const s = fixture();
    const lost = s.invoices.find((r) => r.payment_state === "lost")!;
    s.dashboard.upcoming = [{ company: "NORD", date: "2026-11-20", side: "out", kind: "invoice", id: lost.id, who: lost.supplier,
      number: lost.number, amount: lost.total, currency: "RON" }];
    renderApp({ state: () => json(s) });
    const ahead = within(await screen.findByRole("region", { name: "Următoarele 30 de zile" }));
    const row = ahead.getByRole("link", { name: new RegExp(lost.supplier) });
    expect(row).toHaveAttribute("href", `?co=NORD&inv=${lost.id}`);
    expect(row).toHaveTextContent("Scadentă pe 20 noiembrie");
    expect(row).toHaveTextContent("De plătit");
  });
});


it.each([[fixture(), taskNord], [fixture("STUDIO9"), taskStudio], [fixtureAll(), taskAll]] as const)(
  "the task API preserves the existing cards, priorities and cross-company deduplication", (state, response) => {
    const original = fixtureHomeTasks(parts(state));
    expect(homeTasks(state).total).toBe(response.total);
    expect(response.total).toBe(original.all.length);
    expect(response.urgent).toBe(original.urgent);
    expect(response.data.map((r) => r.key)).toEqual(original.all);
    expect(response.highlights.map((r) => r.key)).toEqual(original.brief);
    expect(homeTasks(state).brief.map((t) => t.key)).toEqual(original.brief);
  },
);
