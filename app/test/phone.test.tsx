// Navigation v2 on a phone (docs/information-architecture.md, section 3): the tab bar's five slots, the Nou menu, Mai mult,
// and the sections that came with it (Sarcini; Automatizări has its own tests).
import { act, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render, answer } from "./helpers";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import App from "@/components/App";
import { parse } from "@/lib/nav";
import { at, companiesConfig, taskState, fixture, json, server, renderApp } from "./helpers";
import type { State } from "@/lib/types";

beforeEach(() => {
  localStorage.clear();
  at("?co=NORD");
});

const bar = () => within(screen.getByRole("navigation", { name: "Secțiuni" }));
const dock = () => within(document.querySelector(".tabs") as HTMLElement);
const loaded = () => screen.findByRole("region", { name: "De plătit" });
/** The Nou entry for Bondrop: its name, then what it is. */
const BONDROP = "Bondrop Bonuri și chitanțe de pe telefon";

/** NORD with four mails to place instead of one: its other 9 open tasks (the payment file's import question, the unknown
 * file, the late invoice, the refused one, two client reminders, the pack to send, ANAF expiring, the unreadable
 * invoice) + 4 = 13. */
function fourMails(): State {
  const s = fixture();
  s.to_check = [1, 2, 3, 4].map((n) => ({ ...s.to_check[0], key: `${n}`.repeat(64), subject: `Comanda ${n}`, supplier: `Comanda ${n}` }));
  return s;
}

function open(query: string) {
  act(() => {
    at(query);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

describe("tab bar", () => {
  it("has five slots for the daily work, with counts, and keeps Mai mult current on the sections it lists", async () => {
    renderApp();
    await loaded();
    // Caută, the capsule of icon-only tabs (each named, its count read in the name), Nou.
    expect([...document.querySelectorAll(".tabs > *")].map((a) => a.getAttribute("aria-label") ?? a.textContent)).toEqual(["Caută", "Secțiuni", "Nou"]);
    expect(bar().getAllByRole("link").map((a) => a.getAttribute("aria-label")))
      .toEqual(["Acasă", `Sarcini, ${fixture().task_summary.total} deschise`, "Cheltuieli", "Tranzacții, 59 de explicat"]);
    expect(bar().getByRole("link", { name: `Sarcini, ${fixture().task_summary.total} deschise` }).querySelector("span[aria-hidden]")).toHaveTextContent(String(fixture().task_summary.total)); // the visible count
    expect(bar().getByRole("link", { name: "Acasă" })).toHaveAttribute("aria-current", "page");
    // The current tab says so beyond colour, as Shopify's does: its icon filled, the others hairline.
    const glyph = (name: string) => bar().getByRole("link", { name }).querySelector("svg")?.innerHTML;
    const homeCurrent = glyph("Acasă");
    expect(bar().getByRole("link", { name: "Tranzacții, 59 de explicat" })).toHaveAttribute("href", "?co=NORD&tab=tranzactii");
    // Floating over the safe area's edge, within --tabbar (globals.css scrolls a focused element clear of it); dimmed by colour.
    expect(document.querySelector(".tabs")).toHaveClass("fixed", "bottom-[calc(8px+env(safe-area-inset-bottom))]", "h-14");
    expect(bar().getByRole("link", { name: "Cheltuieli" })).toHaveClass("text-muted");
    expect(bar().getByRole("link", { name: "Cheltuieli" }).className).not.toMatch(/opacity/);
    // Mai mult is in the top bar, beside the gear; Setări is reached only from the gear.
    const top = within(document.querySelector(".top") as HTMLElement);
    await userEvent.click(top.getByRole("link", { name: "Mai mult" }));
    expect(top.getByRole("link", { name: "Mai mult" })).toHaveAttribute("aria-current", "page");
    const more = within(screen.getByRole("navigation", { name: "Mai mult" }));
    expect(more.getAllByRole("link").map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Vânzări", "?co=NORD&tab=vanzari"], ["Fișiere de plăți", "?co=NORD&tab=plati"], ["Parteneri", "?co=NORD&tab=parteneri"], ["Produse", "?co=NORD&tab=produse"],
      ["Rapoarte", "?co=NORD&tab=rapoarte"], ["Automatizări", "?co=NORD&tab=automatizari"],
    ]);
    await userEvent.click(more.getByRole("link", { name: "Vânzări" }));
    expect(await screen.findByText("Vânzări", { selector: ".ttl" })).toBeInTheDocument();
    expect(top.getByRole("link", { name: "Mai mult" })).toHaveAttribute("aria-current", "page");
    expect(bar().queryByRole("link", { current: "page" })).toBeNull();
    open("?co=NORD&tab=setari");
    expect(await screen.findByText("Setări", { selector: ".ttl" })).toBeInTheDocument();
    expect(top.getByRole("link", { name: "Setări" })).toHaveAttribute("aria-current", "page");
    expect(top.getByRole("link", { name: "Mai mult" })).not.toHaveAttribute("aria-current");
    open("?co=NORD&tab=tranzactii");
    expect(await screen.findByText("Tranzacții", { selector: ".ttl" })).toBeInTheDocument();
    expect(bar().getByRole("link", { name: /^Tranzacții/ })).toHaveAttribute("aria-current", "page");
    expect(glyph("Acasă")).not.toBe(homeCurrent); // Acasă no longer current: its hairline icon again
  });

  it.each([[0, null, "Tranzacții"], [100, "99+", "Tranzacții, 100 de explicat"]])(
    "shows %i to explain on Tranzacții as %s", async (count, badge, name) => {
      const s = fixture();
      s.transactions = { ...s.transactions!, to_explain: count };
      renderApp({ state: () => json(s) });
      await loaded();
      const link = bar().getByRole("link", { name });
      expect(link.querySelector("span[aria-hidden]")?.textContent ?? null).toBe(badge);
    });

  it("keeps Toate firmele through Mai mult, and names a company only for a section that needs one", async () => {
    localStorage.setItem("fiscalm:co", "STUDIO9");
    at("?co=ALL");
    renderApp();
    await loaded();
    await userEvent.click(within(document.querySelector(".top") as HTMLElement).getByRole("link", { name: "Mai mult" }));
    expect(window.location.search).toBe("?co=ALL&tab=mai-mult");
    // De explicat counts one firm's transactions: none under Toate firmele, as in the desktop column.
    expect(bar().getByRole("link", { name: "Tranzacții" })).toHaveAttribute("href", "?co=STUDIO9&tab=tranzactii");
    const more = within(await screen.findByRole("navigation", { name: "Mai mult" }));
    expect(more.getByRole("link", { name: "Fișiere de plăți" })).toHaveAttribute("href", "?co=STUDIO9&tab=plati");
    expect(more.getByRole("link", { name: "Automatizări" })).toHaveAttribute("href", "?co=ALL&tab=automatizari");
    expect(more.getByRole("link", { name: "Vânzări" })).toHaveAttribute("href", "?co=ALL&tab=vanzari");
    await userEvent.click(more.getByRole("link", { name: "Parteneri" }));
    expect(window.location.search).toBe("?co=ALL&tab=parteneri");
    const rows = [...document.querySelectorAll("main .card .row")].map((r) => r.textContent ?? "");
    expect(rows.some((t) => t.includes("NORD"))).toBe(true);
    expect(rows.some((t) => t.includes("STUDIO9"))).toBe(true);
  });

  it("Nou is a button that opens a sheet with Factură nouă, and gives focus back when closed", async () => {
    renderApp();
    await loaded();
    const nou = dock().getByRole("button", { name: "Nou" });
    expect(nou).toHaveAttribute("aria-expanded", "false");
    expect(nou).not.toHaveAttribute("aria-current");
    await userEvent.click(nou);
    const menu = screen.getByRole("dialog", { name: "Nou" });
    expect(nou).toHaveAttribute("aria-expanded", "true");
    expect(menu).toHaveFocus();
    await userEvent.click(within(menu).getByRole("button", { name: "Închide" }));
    expect(screen.queryByRole("dialog", { name: "Nou" })).toBeNull();
    expect(nou).toHaveFocus();
    expect(nou).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(nou);
    const sheet = within(screen.getByRole("dialog", { name: "Nou" }));
    await userEvent.tab();
    expect(sheet.getByRole("button", { name: "Închide" })).toHaveFocus();
    await userEvent.tab();
    expect(sheet.getByRole("button", { name: "Încarcă bon sau factură" })).toHaveFocus();
    await userEvent.tab();
    expect(sheet.getByRole("link", { name: "Factură nouă" })).toHaveFocus();
    await userEvent.tab();
    expect(sheet.getByRole("button", { name: "Factură de stornare" })).toHaveFocus();
    await userEvent.tab();
    expect(sheet.getByRole("link", { name: BONDROP })).toHaveFocus();
    await userEvent.tab(); // focus stays in the sheet
    expect(sheet.getByRole("button", { name: "Închide" })).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(sheet.getByRole("link", { name: BONDROP })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(nou).toHaveFocus();
    await userEvent.click(nou);
    await userEvent.click(within(screen.getByRole("dialog", { name: "Nou" })).getByRole("link", { name: "Factură nouă" }));
    expect(window.location.search).toBe("?co=NORD&new=1");
  });

  it("offers nothing to create under Toate firmele, where it isn't clear which company it is for", async () => {
    at("?co=ALL");
    renderApp();
    await loaded();
    await userEvent.click(dock().getByRole("button", { name: "Nou" }));
    const sheet = within(screen.getByRole("dialog", { name: "Nou" }));
    for (const name of ["Încarcă bon sau factură", "Factură nouă", "Factură de stornare", BONDROP]) expect(sheet.getByRole("button", { name })).toBeDisabled();
    expect(sheet.getByText("Alege mai întâi o firmă.")).toBeInTheDocument();
  });

  it("Storno lists the invoices the engine would reverse, and one opens its sheet with the storno, closing Nou", async () => {
    renderApp();
    await loaded();
    const nou = dock().getByRole("button", { name: "Nou" });
    await userEvent.click(nou);
    const sheet = within(screen.getByRole("dialog", { name: "Nou" }));
    const storno = sheet.getByRole("button", { name: "Factură de stornare" });
    expect(storno).toHaveAttribute("aria-expanded", "false");
    expect(sheet.queryByRole("group", { name: "Alege factura de stornat" })).toBeNull(); // hidden until opened
    await userEvent.click(storno);
    expect(storno).toHaveAttribute("aria-expanded", "true");
    const list = within(sheet.getByRole("group", { name: "Alege factura de stornat" }));
    const links = list.getAllByRole("link");
    expect(links).toHaveLength(1); // only DEMO-1 has can_storno in the fixture
    const own = fixture().issued.find((r) => r.local_id === "local-accepted")!.id; // its collectable row
    expect(links[0]).toHaveAttribute("href", `?co=NORD&iss=${own}`);
    expect(links[0].textContent).toBe("Client Exemplu SRLnr. DEMO-1, 27 oct121,00 lei");
    await userEvent.click(links[0]);
    expect(window.location.search).toBe(`?co=NORD&iss=${own}`);
    expect(screen.queryByRole("dialog", { name: "Nou" })).toBeNull();
    expect(within(await screen.findByRole("dialog", { busy: false })).getByRole("button", { name: "Stornează factura" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(nou).toHaveFocus();
  });

  it("keeps the newest six eligible storno links when eligible invoices are beyond root history", async () => {
    const full = fixture(), base = full.issued.find((r) => r.can_storno)!;
    const days = ["01", "07", "02", "06", "03", "05", "04"];
    full.issued = Array.from({ length: 151 }, (_, i) => ({ ...base, id: `late-storno-${i}`, local_id: `local-${i}`, number: `ST${i}`, can_storno: i >= 144,
      issue_date: i >= 144 ? `2026-10-${days[i - 144]}` : base.issue_date }));
    const api = renderApp({ state: () => json(full) });
    await loaded();
    await userEvent.click(dock().getByRole("button", { name: "Nou" }));
    await userEvent.click(within(screen.getByRole("dialog", { name: "Nou" })).getByRole("button", { name: "Factură de stornare" }));
    await screen.findByRole("link", { name: /ST149/ });
    const list = within(screen.getByRole("group", { name: "Alege factura de stornat" }));
    expect(list.getAllByRole("link")).toHaveLength(7);
    expect(list.getAllByRole("link").slice(0, 6).map((a) => new URL((a as HTMLAnchorElement).href).searchParams.get("iss")))
      .toEqual([145, 147, 149, 150, 148, 146].map((i) => `late-storno-${i}`));
    expect(list.getByRole("link", { name: "Toate facturile emise" })).toBeInTheDocument();
    expect(api.calls.filter((c) => c.url.includes("f=storno"))).toHaveLength(1);
    await userEvent.click(list.getByRole("link", { name: /ST149/ }));
    expect(window.location.search).toContain("iss=late-storno-149");
    expect(await screen.findByRole("dialog", { busy: false })).toHaveTextContent("ST149");
  });

  it("Storno is disabled, and says why, when no issued invoice can be reversed", async () => {
    const s = fixture();
    s.issued = s.issued.map((r) => ({ ...r, can_storno: false }));
    renderApp({ state: () => json(s) });
    await loaded();
    await userEvent.click(dock().getByRole("button", { name: "Nou" }));
    const sheet = within(screen.getByRole("dialog", { name: "Nou" }));
    await waitFor(() => expect(sheet.getByRole("button", { name: "Factură de stornare" })).toHaveAttribute("aria-disabled", "true"));
    expect(sheet.getByRole("button", { name: "Factură de stornare" })).toHaveAccessibleDescription("Nicio factură de stornat.");
  });

  it("Bondrop opens the receipt links in Setări › Linkuri și chei API", async () => {
    renderApp();
    await loaded();
    await userEvent.click(dock().getByRole("button", { name: "Nou" }));
    const link = within(screen.getByRole("dialog", { name: "Nou" })).getByRole("link", { name: BONDROP });
    expect(link).toHaveAttribute("href", "?co=NORD&tab=setari&sec=acces#h-links");
    await userEvent.click(link);
    expect(window.location.search).toBe("?co=NORD&tab=setari&sec=acces");
    expect(screen.queryByRole("dialog", { name: "Nou" })).toBeNull();
    expect(await screen.findByRole("heading", { name: "Bondrop" })).toBeInTheDocument();
  });
});

describe("sections", () => {
  it("has a tab for each new section; Sarcini, Automatizări and Mai mult show every company at once", () => {
    localStorage.setItem("fiscalm:co", "STUDIO9");
    for (const tab of ["sarcini", "automatizari", "mai-mult"]) expect(parse(`?co=NORD&tab=${tab}`).tab).toBe(tab);
    for (const tab of ["sarcini", "automatizari", "mai-mult"]) expect(parse(`?co=ALL&tab=${tab}`).co).toBe("ALL");
  });

  it("Sarcini lists every task, one under another, with the count Acasă shows too", async () => {
    const s = fourMails(), count = fixture().task_summary.total + s.to_check.length - fixture().to_check.length;
    renderApp({ state: () => json(s) });
    await loaded();
    // Acasă shows three tasks and leads to the complete list.
    expect(screen.queryByText("Comanda 4", { exact: false })).toBeNull();
    await userEvent.click(screen.getByRole("link", { name: `Toate (${count})` }));
    expect(window.location.search).toBe("?co=NORD&tab=sarcini");
    await screen.findByRole("heading", { name: `Sarcini ${count}` });
    expect(document.querySelectorAll(".tasks > *")).toHaveLength(count);
    expect(document.querySelector(".car")).toBeNull(); // no carousel
    for (const n of [1, 2, 3, 4]) expect(screen.getByText(`Comanda ${n}. Pe document nu apare nicio firmă.`)).toBeInTheDocument();
  });

  it("Sarcini says when nothing waits", async () => {
    renderApp({ state: () => json(taskState({ reminder_tasks: [] })) });
    open("?co=NORD&tab=sarcini");
    expect(await screen.findByText("Sarcini", { selector: ".ttl" })).toHaveTextContent(/^Sarcini$/);
    expect(await screen.findByText("Niciun element")).toBeInTheDocument();
  });

  it("an empty list of a new firm offers its first step; a filter that hides everything offers none", async () => {
    const s = fixture();
    s.system.anaf = { ...s.system.anaf, status: "not_set_up", authorized: false };
    renderApp({ state: () => json(s), companies: () => json(companiesConfig()), browse: () => json({ data: [], total: 0, next_cursor: null, months: [] }) });
    await loaded();
    const empty = () => document.querySelector("main .empty") as HTMLElement;
    for (const [tab, step] of [["vanzari", "Emite prima factură"], ["cheltuieli", "Încarcă bon sau factură"], ["tranzactii", "Încarcă extrasul"],
      ["parteneri", "Partener nou"], ["vanzari&f=late", ""], ["parteneri&f=open", ""], ["tranzactii&from=2026-10-01&to=2026-10-31", ""]]) {
      open(`?co=NORD&tab=${tab}`);
      await waitFor(() => expect(empty()?.textContent).toBe(`Niciun element${step}`));
    }
  });

  it("Sarcini under Toate firmele names each company's tasks and acts for the task's own company", async () => {
    at("?co=ALL&tab=sarcini");
    const [a] = fixture("STUDIO9").ambiguous;
    const api = server({ onPost: () => json({ ok: true, message: "", state: fixture("STUDIO9") }) });
    render(<App />);
    await screen.findByRole("heading", { name: /^Sarcini \d/ });
    const texts = [...document.querySelectorAll(".tasks .task p")].map((p) => p.textContent ?? "");
    expect(texts.some((t) => t.startsWith("NORD. "))).toBe(true);
    const card = (await screen.findByText(/^STUDIO9\. Plată de 350,00 lei/)).closest(".task") as HTMLElement;
    await userEvent.click(within(card).getByRole("button", { name: "Factura CX-1010, 350,00 lei, din 21 oct" }));
    await answer();
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toMatch(/api\/resolve-debit$/);
    expect(api.posts()[0].body).toEqual({ co: "STUDIO9", tx: a.tx, target: "204" });
  });

  it("a credit note naming no invoice asks which of the supplier's invoices it cancels", async () => {
    const s = fixture("NORD"), [a, b] = s.invoices.filter((r) => !r.credit_note);
    const question = [a, b].map((r) => ({ id: r.id, number: r.number, amount: r.total, date: r.issue_date }));
    s.invoices = [...s.invoices, { ...a, id: "123", number: "TT-13", supplier: "Tipografia Test SRL", total: "-480.00",
      status: "skipped", credit_note: true, credits: [], credit_question: question }];
    at("?co=NORD&tab=sarcini");
    const api = server({ state: () => json(s), onPost: () => json({ ok: true, message: "", state: s }) });
    render(<App />);
    const card = (await screen.findByText("Tipografia Test SRL: ce factură anulează stornarea?")).closest(".task") as HTMLElement;
    expect(within(card).getByRole("button", { name: "Niciuna" })).toBeTruthy();
    await userEvent.click(within(card).getByRole("button", { name: new RegExp(`^Factura ${b.number}, din `) }));
    await answer();
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toMatch(/api\/credit-pick$/);
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "123", target: b.id });
  });

});
