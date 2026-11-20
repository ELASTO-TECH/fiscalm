// The engine's inactive partner warnings (`partner_tasks`) as Sarcini, and its refusal to put an inactive supplier's invoice in the
// payment file (`refused[].why` "supplier_inactive", `can_approve` false) on Sarcini and the invoice sheet
// (docs/openapi.json). The app shows the engine's words with the check's date and decides nothing.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { State } from "@/lib/types";
import { at, fixture, fixtureAll, json, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

const SUPPLIER = "Furnizor inactiv la ANAF";
const SUPPLIER_BODY = "Laborator Analize SRL, verificat la ANAF pe 26 oct. Există facturi de plătit. Verifică situația furnizorului cu contabilul.";
const REFUSED = "Factura de la Laborator Analize SRL nu e în fișierul de plăți";
const INACTIVE = "Furnizorul figurează inactiv la ANAF (verificat pe 26 oct). Verifică situația cu contabilul înainte de plată.";
const card = async (title: string) => (await screen.findByText(title)).closest(".task") as HTMLElement;
const cards = (title: string) => screen.queryAllByText(title).map((b) => b.closest(".task") as HTMLElement);

function studio9(change: (s: State) => void = () => {}): State {
  const s = fixture("STUDIO9");
  change(s);
  return s;
}

async function open(query: string, s?: State) {
  at(query);
  const api = renderApp(s ? { state: () => json(s) } : { all: () => json(fixtureAll()) });
  return api;
}

describe("inactive partners in Sarcini", () => {
  it("shows the engine's task for the inactive supplier with its label, the partner, the check's date and its detail", async () => {
    expect(fixture("STUDIO9").partner_tasks).toEqual([{ kind: "inactive_supplier", id: "STUDIO9.2001", company: "STUDIO9",
      label: SUPPLIER, detail: "Există facturi de plătit. Verifică situația furnizorului cu contabilul.", checked_at: "2026-10-26" }]);
    await open("?co=STUDIO9&tab=sarcini", studio9());
    const c = await card(SUPPLIER);
    expect(c.querySelector("p")).toHaveTextContent(SUPPLIER_BODY);
    expect(c.querySelector(".ic.late")).toBeNull(); // a warning, not an answer due now
    await userEvent.click(within(c).getByRole("link", { name: "Vezi partenerul" }));
    expect(await screen.findByRole("dialog", { name: "Laborator Analize SRL" })).toBeInTheDocument();
  });

  it("keys a partner's two roles apart: one card for each task, the client one in the engine's words", async () => {
    const errors = vi.spyOn(console, "error");
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => s.partner_tasks.push({ kind: "inactive_client", id: "STUDIO9.2001", company: "STUDIO9",
      label: "Client inactiv sau radiat la ANAF", detail: "Există facturi de încasat. Verifică situația clientului.", checked_at: "2026-10-26" })));
    expect((await card("Client inactiv sau radiat la ANAF")).querySelector("p")).toHaveTextContent(
      "Laborator Analize SRL, verificat la ANAF pe 26 oct. Există facturi de încasat. Verifică situația clientului.");
    expect(cards(SUPPLIER)).toHaveLength(1);
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key/);
  });

  it("names the firm under Toate firmele", async () => {
    await open("?co=ALL&tab=sarcini");
    expect((await card(SUPPLIER)).querySelector("p")).toHaveTextContent(`STUDIO9. ${SUPPLIER_BODY}`);
    expect(cards(SUPPLIER)).toHaveLength(1);
    expect((await card(REFUSED)).querySelector("p")).toHaveTextContent(`STUDIO9. ${INACTIVE}`);
  });

  it("without the partner in the state, still says when ANAF was checked", async () => {
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => { s.partners = s.partners.filter((p) => p.id !== "STUDIO9.2001"); }));
    const c = await card(SUPPLIER);
    expect(c.querySelector("p")).toHaveTextContent("Verificat la ANAF pe 26 oct. Există facturi de plătit. Verifică situația furnizorului cu contabilul.");
    expect(within(c).queryByRole("link")).toBeNull();
  });
});

describe("an inactive supplier's invoice refused for the payment file", () => {
  it("says the engine's reason on Sarcini instead of Date neclare, next to the other reasons", async () => {
    expect(fixture("STUDIO9").refused).toEqual([expect.objectContaining({ id: "201", why: "supplier_inactive", refused_checked_at: "2026-10-26", still_inactive: true })]);
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => s.refused.push({ id: "205", why: "unclear", refused_checked_at: null, still_inactive: null,
      iban_last4: "0205", supplier: "Farmacia Sud SRL" })));
    const c = await card(REFUSED);
    expect(c.querySelector("p")).toHaveTextContent(INACTIVE);
    expect(c.querySelector(".ic.late")).toBeNull(); // a refusal to answer, not money overdue
    expect(within(c).getByRole("link", { name: "Vezi factura" })).toHaveAttribute("href", "?co=STUDIO9&tab=sarcini&inv=201");
    expect((await card("Factura de la Farmacia Sud SRL nu e în fișierul de plăți")).querySelector("p"))
      .toHaveTextContent("Date neclare. Verifică factura înainte de plată.");
  });

  it("dates the refusal with its own ANAF check, not the partner's later one", async () => {
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => {
      s.partners = s.partners.map((p) => (p.id === "STUDIO9.2001" && p.anaf_status ? { ...p, anaf_status: { ...p.anaf_status, checked_at: "2026-10-27" } } : p));
    }));
    expect((await card(REFUSED)).querySelector("p")).toHaveTextContent(INACTIVE);
  });

  it("once ANAF shows the supplier active again, says so and that the invoice can be added by hand", async () => {
    const api = await open("?co=STUDIO9&tab=sarcini", studio9((s) => {
      s.partners = s.partners.map((p) => (p.id === "STUDIO9.2001" && p.anaf_status
        ? { ...p, anaf_status: { ...p.anaf_status, checked_at: "2026-10-27", inactive: false } } : p));
      s.partner_tasks = [];
      s.refused = s.refused.map((x) => ({ ...x, why: "ready", still_inactive: false }));
      s.invoices = s.invoices.map((r) => (r.id === "201" ? { ...r, can_approve: true } : r));
    }));
    const c = await card(REFUSED);
    expect(c.querySelector("p")).toHaveTextContent("Furnizorul era inactiv la ANAF la verificarea din 26 oct. "
      + "ANAF îl arată acum ca activ, așa că poți adăuga factura manual în fișierul de plăți.");
    expect(c).not.toHaveTextContent(/Furnizorul (este|figurează) inactiv|27 oct/);
    await userEvent.click(within(c).getByRole("link", { name: "Vezi factura" }));
    const d = await screen.findByRole("dialog", { name: "Laborator Analize SRL, 230,00 lei" });
    expect(within(d).getByRole("button", { name: "Pune în fișier" })).toBeEnabled();
    expect(within(d).queryByRole("note")).toBeNull();
    expect(api.posts()).toEqual([]);
  });

  it("while the engine still refuses the invoice, says ANAF shows the supplier active and the engine's reason now", async () => {
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => {
      s.partners = s.partners.map((p) => (p.id === "STUDIO9.2001" && p.anaf_status
        ? { ...p, anaf_status: { ...p.anaf_status, checked_at: "2026-10-27", inactive: false } } : p));
      s.partner_tasks = [];
      s.refused = s.refused.map((x) => ({ ...x, why: "unclear", still_inactive: false }));
      s.invoices = s.invoices.map((r) => (r.id === "201" ? { ...r, can_approve: false } : r));
    }));
    expect((await card(REFUSED)).querySelector("p")?.textContent)
      .toBe("Furnizorul era inactiv la ANAF la verificarea din 26 oct. ANAF îl arată acum ca activ. "
        + "Date neclare. Verifică factura înainte de plată.");
  });

  it("when the engine would accept it but can't add it here (shadow mode), says ANAF shows the supplier active, promising nothing", async () => {
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => {
      s.partner_tasks = [];
      s.refused = s.refused.map((x) => ({ ...x, why: "ready", still_inactive: false }));
      s.invoices = s.invoices.map((r) => (r.id === "201" ? { ...r, can_approve: false } : r));
    }));
    expect((await card(REFUSED)).querySelector("p")?.textContent)
      .toBe("Furnizorul era inactiv la ANAF la verificarea din 26 oct. ANAF îl arată acum ca activ.");
  });

  it("when no trusted ANAF status is left, keeps the past refusal and says the current status is unknown", async () => {
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => {
      s.partners = s.partners.map((p) => (p.id === "STUDIO9.2001" ? { ...p, anaf_status: null } : p));
      s.partner_tasks = [];
      s.refused = s.refused.map((x) => ({ ...x, why: "ready", still_inactive: null }));
    }));
    const c = await card(REFUSED);
    expect(c.querySelector("p")?.textContent).toBe("Furnizorul era inactiv la ANAF la verificarea din 26 oct. "
      + "Starea actuală la ANAF nu este cunoscută. Verifică situația cu contabilul înainte de plată.");
    expect(c).not.toHaveTextContent(/Furnizorul (este|figurează) inactiv|activ\.|de mână/);
  });

  it.each([
    ["unclear", "Date neclare. Verifică factura înainte de plată."],
  ] as const)("when no trusted ANAF status is left and the engine now refuses for %s, adds that reason after the past refusal", async (why, reason) => {
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => {
      s.partners = s.partners.map((p) => (p.id === "STUDIO9.2001" ? { ...p, anaf_status: null } : p));
      s.partner_tasks = [];
      s.refused = s.refused.map((x) => ({ ...x, why, still_inactive: null }));
      s.invoices = s.invoices.map((r) => (r.id === "201" ? { ...r, can_approve: false } : r));
    }));
    expect((await card(REFUSED)).querySelector("p")?.textContent).toBe("Furnizorul era inactiv la ANAF la verificarea din 26 oct. "
      + `Starea actuală la ANAF nu este cunoscută. Verifică situația cu contabilul înainte de plată. ${reason}`);
  });

  it("disables Pune în fișier on the invoice sheet and says why once, posting nothing", async () => {
    const api = await open("?co=STUDIO9&inv=201", studio9());
    const d = await screen.findByRole("dialog", { name: "Laborator Analize SRL, 230,00 lei" });
    const add = within(d).getByRole("button", { name: "Pune în fișier" });
    expect(add).toBeDisabled();
    expect(add).toHaveAccessibleDescription(INACTIVE);
    expect(within(d).getByRole("region", { name: SUPPLIER })).toHaveTextContent(INACTIVE); // the refusal's own card
    expect(within(d).queryByRole("note")).toBeNull(); // not repeated under the button
    expect(within(d).queryByText(/Adaug factura în fișier/)).toBeNull();
    await userEvent.click(add);
    expect(api.posts()).toEqual([]);
    expect(within(d).getByRole("button", { name: "Marchează ca plătită" })).toBeEnabled();
  });

  it("says why on the sheet from can_approve alone, before the tick has refused it", async () => {
    await open("?co=STUDIO9&inv=201", studio9((s) => { s.refused = []; }));
    const d = await screen.findByRole("dialog", { name: "Laborator Analize SRL, 230,00 lei" });
    expect(within(d).getByRole("button", { name: "Pune în fișier" })).toBeDisabled();
    expect(within(d).getByRole("note")).toHaveTextContent(`Factura nu poate intra în fișierul de plăți. ${INACTIVE}`);
  });

  it("an approvable invoice keeps its enabled button and its usual note", async () => {
    const api = await open("?co=STUDIO9&inv=205", studio9());
    const d = await screen.findByRole("dialog", { name: /^Farmacia Sud SRL/ });
    expect(within(d).queryByRole("note")).toBeNull();
    await userEvent.click(within(d).getByRole("button", { name: "Pune în fișier" }));
    // STUDIO9's open file takes it; the one whose download is missing waits for its answer and is not offered.
    const chooser = await screen.findByRole("dialog", { name: "Pune în fișier" });
    expect(within(chooser).getAllByRole("radio").map((r) => r.closest("label")!.querySelector(".font-semibold")!.textContent))
      .toEqual(["STUDIO9 plăți furnizori", "Fișier nou"]);
    await userEvent.click(within(chooser).getByRole("button", { name: "Pune în fișier" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ action: "payment_file_add", file_id: 5, invoice_ids: ["205"] });
  });

  it("leads the overdue card to the invoice, where its sheet says why it can't go in a file", async () => {
    await open("?co=STUDIO9&tab=sarcini", studio9((s) => {
      s.invoices = s.invoices.map((r) => (r.id === "201" ? { ...r, due_date: "2026-10-20" } : r));
    }));
    const c = await card("Laborator Analize SRL: plată restantă");
    expect(within(c).getByRole("link", { name: "Vezi factura" })).toHaveAttribute("href", expect.stringContaining("inv=201"));
    expect(within(c).queryAllByRole("button")).toEqual([]);
  });
});
