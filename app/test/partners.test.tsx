// Parteneri: the list under Facturi, a partner's sheet, and the way between a partner and their invoices (both ways,
// with the back button), against a fake engine.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fixtureBrowseData, fixturePartnerPagesData } from "@/lib/fixtures";
import { PARTNER_COLLECTIONS } from "@/lib/types";
import { beforeEach, describe, expect, it } from "vitest";
import { listLoaded, at, fixture, fixtureAll, json, savedClientState, renderApp, summaryResult, partnerResult, detailResult } from "./helpers";

beforeEach(() => localStorage.clear());

const list = () => document.querySelector("main .card") as HTMLElement;
const sheet = () => screen.getByRole("dialog");

function largePartner() {
  const s = fixture(), p = s.partners.find((p) => p.id === "NORD.1001")!;
  const invoice = s.invoices.find((r) => r.partner === p.id)!, sale = s.issued.find((r) => r.status === "collected")!;
  s.invoices = Array.from({ length: 151 }, (_, i) => ({ ...invoice, id: `scale-in-${i}`, number: `Primita-${i}` }));
  s.issued = Array.from({ length: 151 }, (_, i) => ({ ...sale, id: `scale-out-${i}`, number: `Emisa-${i}`, partner: p.id }));
  Object.assign(p, { received: 151, issued: 151, payable: { total: "58346.40", other: [] }, roles: ["supplier", "client"] });
  p.held = s.issued.map((r) => ({ invoice: r.id, amount: "0.10", currency: "RON" }));
  s.transactions.remembered = Array.from({ length: 151 }, (_, i) => ({ id: `memory-${i}`, by: "name" as const, shown: `Cheie ${i}`,
    kind: "documents" as const, partner: p.id, reason: null, note: "", who_by: "admin", at: "2026-10-27" }));
  const memory = s.transactions.remembered[0];
  s.transactions.remembered.push({ ...memory, id: "other", partner: "NORD.1002" }, { ...memory, id: "unassigned", partner: null });
  return s;
}

it.each(PARTNER_COLLECTIONS)("keeps %s fixture pages identical to the engine with complete identities", (collection) => {
  const full = largePartner();
  for (const cursor of ["", "50"]) {
    const query = { id: "NORD.1001", collection, cursor };
    // Empty collections have no continuation cursor.
    if (cursor && !fixturePartnerPagesData(full, { ...query, cursor: "" }).pages[collection]!.next_cursor) continue;
    const actual = fixturePartnerPagesData(full, query), expected = partnerResult(full, query);
    expect(actual.pages).toEqual(expected.pages);
    expect(actual.state.partners).toEqual(expected.state.partners);
  }
});

it("reads all partner histories without root rows and keeps later invoice links and focus", async () => {
  const full = largePartner();
  at("?co=NORD&tab=parteneri&p=NORD.1001");
  const api = renderApp({ state: () => json(summaryResult(full)),
    partnerPages: (url) => json(partnerResult(full, Object.fromEntries(new URL(url, "http://local").searchParams))),
    detail: (url) => json(detailResult(full, Object.fromEntries(new URL(url, "http://local").searchParams))) });
  const dialog = await screen.findByRole("dialog", { name: "Energie Furnizare SA" });
  expect(dialog).toHaveTextContent("De plătit58.346,40 lei");
  expect(dialog).toHaveTextContent("151 de facturi primite, 151 de facturi emise");
  expect(within(within(dialog).getByRole("region", { name: "Reguli salvate" })).getByText("Partenerul Cheie 0")).toBeVisible();
  const received = within(dialog).getByRole("region", { name: "Facturi primite" });
  const held = within(dialog).getByRole("heading", { name: "La curieri și procesatori" }).closest("section")!;
  expect(within(received).getAllByRole("link")).toHaveLength(50);
  expect(within(held).getAllByRole("link")).toHaveLength(50);
  expect(api.calls.filter((c) => c.url.includes("api/partner-pages"))).toHaveLength(1);
  for (const [region, label] of [[received, "Facturi primite"], [held, "La curieri și procesatori"]] as const) {
    for (let i = 0; i < 3; i++) {
      await userEvent.click(within(region).getByRole("button", { name: "Pagina următoare" }));
      await waitFor(() => expect(within(region).getByRole("navigation", { name: label })).toHaveAttribute("aria-busy", "false"));
    }
    expect(within(region).getAllByRole("link")).toHaveLength(1);
    expect(within(region).getByRole("button", { name: "Pagina următoare" })).toHaveFocus();
  }
  expect(within(held).getByRole("link", { name: /Emisa-150/ })).toHaveAttribute("href", "?co=NORD&tab=parteneri&iss=scale-out-150");
  const last = within(received).getByRole("link", { name: /Primita-150/ });
  expect(last).toHaveAttribute("href", "?co=NORD&tab=parteneri&inv=scale-in-150");
  await userEvent.click(last);
  expect(await screen.findByRole("dialog", { name: "Energie Furnizare SA, 386,40 lei" })).toBeInTheDocument();
});

it("pages every company memory with an empty root history and forgets the exact selected association", async () => {
  const full = largePartner();
  at("?co=NORD&tab=automatizari");
  const api = renderApp({ state: () => json(summaryResult(full)),
    partnerPages: (url) => json(partnerResult(full, Object.fromEntries(new URL(url, "http://local").searchParams))) });
  const section = await screen.findByRole("region", { name: "Reguli salvate" });
  await within(section).findByText("Partenerul Cheie 0");
  for (let i = 0; i < 3; i++) {
    await userEvent.click(within(section).getByRole("button", { name: "Pagina următoare" }));
    await waitFor(() => expect(within(section).getByRole("navigation")).toHaveAttribute("aria-busy", "false"));
  }
  expect(within(section).getAllByRole("button", { name: "Șterge asocierea" })).toHaveLength(3);
  const row = within(section).getByText("Partenerul Cheie 150").closest("div")!;
  await userEvent.click(within(row as HTMLElement).getByRole("button", { name: "Șterge asocierea" }));
  expect(api.posts()[0].url).toBe("/web/api/memory-forget");
  expect(api.posts()[0].body).toEqual({ co: "NORD", id: "memory-150" });
});

describe("Parteneri", () => {
  it("is a page of its own, with filters and search by name or CUI", async () => {
    at("?co=NORD&tab=parteneri");
    renderApp();
    expect(await screen.findByText("Parteneri", { selector: ".ttl" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Facturi" })).toBeNull(); // no Emise / Primite switch
    await screen.findByRole("searchbox", { name: "Caută partener sau CUI" });
    await listLoaded();
    const energie = within(list()).getByRole("link", { name: /Energie Furnizare SA/ });
    expect(energie).toHaveTextContent("Furnizor, CUI 1001");
    expect(energie).toHaveTextContent("386,40 lei");
    expect(energie).toHaveTextContent("De plătit");
    expect(within(list()).getByRole("link", { name: /Client Alfa SRL/ })).toHaveTextContent("De încasat");
    await userEvent.click(screen.getByRole("link", { name: "Cu restanțe" }));
    await listLoaded();
    expect(window.location.search).toBe("?co=NORD&tab=parteneri&f=late");
    expect(within(list()).getAllByRole("link").map((a) => a.querySelector("b")?.textContent).sort())
      .toEqual(["Client Alfa SRL", "Client Exemplu SRL", "Client Grafic 1", "Client Grafic 2", "Client Grafic 3", "Energie Furnizare SA", "Furnizor Grafic 1", "Furnizor Grafic 2", "Furnizor Grafic 3", "Papetarie Birou SRL"]);
    await userEvent.click(screen.getByRole("link", { name: "Toți" }));
    await listLoaded();
    await userEvent.type(screen.getByRole("searchbox", { name: "Caută partener sau CUI" }), "5002");
    await listLoaded();
    expect(within(list()).getAllByRole("link").map((a) => a.querySelector("b")?.textContent)).toEqual(["Client Beta SRL"]);
  });

  it("opens a partner with what is owed and every invoice with them", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.1001");
    renderApp();
    const s = await screen.findByRole("dialog", { name: "Energie Furnizare SA" });
    expect(within(s).getByRole("heading", { name: "Energie Furnizare SA" })).toBeInTheDocument();
    expect(s.querySelector(".who")).toHaveTextContent(/^Furnizor$/);
    expect(s).toHaveTextContent("CUI1001");
    expect(s).toHaveTextContent("De plătit386,40 lei");
    expect(within(s).queryByText("De încasat")).toBeNull(); // a supplier only: nothing to collect from them
    const inv = within(within(s).getByRole("region", { name: "Facturi primite" })).getAllByRole("link");
    expect(inv).toHaveLength(1);
    expect(inv[0]).toHaveTextContent("Nr. EF-55120"); // named by its number here: the partner is the sheet's title
    expect(inv[0]).not.toHaveTextContent("Energie Furnizare SA");
    expect(inv[0]).toHaveAttribute("href", "?co=NORD&tab=parteneri&inv=101");
  });

  it("goes from an invoice to its partner and back with the back button", async () => {
    at("?co=NORD&tab=cheltuieli&inv=101");
    renderApp();
    const link = await screen.findByRole("link", { name: "Partener: Energie Furnizare SA" });
    expect(link).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&p=NORD.1001");
    await userEvent.click(link);
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&p=NORD.1001");
    await screen.findByRole("dialog", { name: "Energie Furnizare SA" });
    await userEvent.click(within(sheet()).getByRole("link", { name: /Nr\. EF-55120/ }));
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&inv=101");
    window.history.back();
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&p=NORD.1001"));
    window.history.back();
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&inv=101"));
  });

  it("links an issued invoice to its client", async () => {
    at("?co=NORD&tab=vanzari&iss=5002");
    renderApp();
    expect(await screen.findByRole("link", { name: "Partener: Client Beta SRL" }))
      .toHaveAttribute("href", "?co=NORD&tab=vanzari&p=NORD.5002");
  });

  it.each([false, true])("under Toate firmele names each partner's company even if the registry changed: %s", async (changed) => {
    at("?co=ALL&tab=parteneri");
    renderApp({ browse: () => {
      const page = fixtureBrowseData(fixtureAll(), "parteneri", {});
      return json({ ...page, data: page.data.map((p) => changed && p.company === "STUDIO9" ? { ...p, company: "NOU" } : p) });
    } });
    await screen.findByRole("searchbox", { name: "Caută partener sau CUI" });
    await listLoaded();
    expect(within(list()).getByRole("link", { name: /Laborator Analize SRL/ })).toHaveTextContent(`${changed ? "NOU" : "STUDIO9"}, furnizor`);
    expect(within(list()).getByRole("link", { name: /Energie Furnizare SA/ })).toHaveTextContent("NORD, furnizor");
  });

  it("a partner with no name on any invoice shows its CUI", async () => {
    const s = fixture();
    s.partners = s.partners.map((p) => (p.id === "NORD.1001" ? { ...p, name: "" } : p));
    at("?co=NORD&tab=parteneri&p=NORD.1001");
    renderApp({ state: () => json(s) });
    expect(await screen.findByRole("dialog", { name: "CUI 1001" })).toBeInTheDocument();
  });

  it("renders a saved client without empty invoice or balance rows", async () => {
    const s = savedClientState();
    at("?co=NORD&tab=parteneri&p=NORD.99999968");
    renderApp({ state: () => json(s) });
    const dialog = await screen.findByRole("dialog", { name: "Exemplu SRL" });
    expect(dialog.querySelector(".who")).toHaveTextContent(/^Client$/);
    expect(dialog.querySelectorAll(".card")).toHaveLength(1); // its details only
    expect(dialog.querySelector(".card")).toHaveTextContent("CUI99999968");
    expect(within(dialog).queryByText("Facturi")).toBeNull();
    expect(within(dialog).queryByText("De încasat")).toBeNull();
    expect(within(dialog).queryByRole("region", { name: "Facturi emise" })).toBeNull();
  });

  it("an unknown partner opens nothing", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.nope");
    renderApp();
    await screen.findByRole("searchbox", { name: "Caută partener sau CUI" });
    await listLoaded();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

it("deletes a payer's saved report format after a confirmation, posting its exact identity", async () => {
  const full = fixture(), p = full.partners[0];
  p.report_formats = [{ signature: "f".repeat(64), source: "fan", mapping: { reference: "Factura", gross: "Ramburs", fee: "Comision" }, courier: "" }];
  at(`?co=NORD&tab=parteneri&p=${encodeURIComponent(p.id)}`);
  const api = renderApp({ state: () => json(summaryResult(full)),
    partnerPages: (url) => json(partnerResult(full, Object.fromEntries(new URL(url, "http://local").searchParams))) });
  await userEvent.click(await screen.findByRole("button", { name: "Șterge formatul" }));
  await userEvent.click(within(await screen.findByRole("dialog", { name: "Ștergi formatul? La următorul raport alegi din nou coloanele." })).getByRole("button", { name: "Șterge formatul" }));
  await waitFor(() => expect(api.posts()).toHaveLength(1));
  expect(api.posts()[0].body).toEqual({ co: "NORD", partner: p.id, signature: "f".repeat(64) });
});
