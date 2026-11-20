// What ANAF's public registry said of a partner at the engine's last check (`anaf_status`, `anaf_error`, `anaf_eligible`): rows in
// the partner's details, a marker in the list for an inactive firm only. The app shows the engine's answer and decides nothing.
import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { State } from "@/lib/types";
import { facts, withPartner, at, fixture, fixtureAll, json, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());

async function sheet(query: string, name: string, state?: State) {
  at(query);
  renderApp(state ? { state: () => json(state) } : { all: () => json(fixtureAll()) });
  return screen.findByRole("dialog", { name });
}

describe("Parteneri: ANAF", () => {
  it("a checked firm shows what ANAF said and when, with VAT since its start date", async () => {
    const d = await sheet("?co=NORD&tab=parteneri&p=NORD.1006", "Papetarie Birou SRL", fixture());
    expect(facts(d)).toEqual(["CUI: 1006", "Cont bancar: •••• 0106", "Plătitor de TVA: Da, din 1 mar 2019", "e-Factura: Da",
      "TVA la încasare: Nu", "Inactiv: Nu", "Date ANAF din 25 oct."]);
    expect(d.querySelector(".banner")).toBeNull();
  });

  it("an inactive firm says so above its details and in red on its row", async () => {
    const d = await sheet("?co=ALL&tab=parteneri&p=STUDIO9.2001", "Laborator Analize SRL");
    expect(d.querySelector(".banner")).toHaveTextContent("Firma figurează ca inactivă la ANAF.");
    expect(facts(d)).toEqual(["CUI: 2001", "Cont bancar: •••• 0201", "Plătitor de TVA: Nu", "e-Factura: Da", "TVA la încasare: Nu",
      "Inactiv: Da", "Date ANAF din 26 oct."]);
    const value = within(d).getByText("Inactiv", { selector: "dt" }).nextElementSibling;
    expect(value).toHaveTextContent("Da");
    expect(value).toHaveClass("late");
  });

  it("a failed refresh keeps the last answer and its date on one line, and says what went wrong in words", async () => {
    const d = await sheet("?co=NORD&tab=parteneri&p=NORD.99999992", "Client Exemplu SRL", fixture());
    expect(facts(d).slice(-5)).toEqual(["Plătitor de TVA: Da", "e-Factura: Da", "TVA la încasare: Da", "Inactiv: Nu",
      "Date ANAF din 18 oct. Ultima verificare n-a primit răspuns."]);
    expect(d.textContent).not.toContain("unavailable");
  });

  it.each([
    ["not_found", "ANAF nu găsește acest CUI."],
    ["invalid_response", "Răspunsul ANAF de la ultima verificare nu a putut fi citit."],
  ] as const)("shows a readable ANAF failure without invented registry values: %s", async (anaf_error, message) => {
    const d = await sheet("?co=NORD&tab=parteneri&p=NORD.1014", "Depozit Exemplu SRL",
      withPartner("NORD.1014", { anaf_error }));
    expect(facts(d).at(-1)).toBe(message);
    expect(facts(d).join(" ")).not.toMatch(/Plătitor de TVA|e-Factura|Inactiv|Date ANAF/);
    expect(d.textContent).not.toContain(anaf_error);
  });

  it("lists the partner's history invoices with its own and adds them to what was invoiced, never to what is owed", async () => {
    const d = await sheet("?co=NORD&tab=parteneri&p=NORD.1014", "Depozit Exemplu SRL", fixture());
    const card = within(d).getByText("Vânzări", { selector: "dt" }).closest("dl")!;
    expect(card).toHaveTextContent("De încasatNimic");
    expect(card).toHaveTextContent("Vânzări3.345,75 leidin care 3.345,75 lei din istoric");
    const rows = [...within(d).getByRole("heading", { name: "Facturi emise" }).parentElement!.nextElementSibling!.querySelectorAll(".row")];
    expect(rows.map((r) => r.querySelector("b")!.textContent + r.querySelector("small")!.textContent)).toEqual(
      ["Nr. NRD 0035Din istoric", "Nr. NRD 0031Din istoric"]);
  });

  it("shows a turnover a credit note made negative, and its history part", async () => {
    const minus = { total: "-100.00", other: [] }, none = { total: "0.00", other: [] };
    const turnover = { sales: minus, expenses: none, history: { sales: minus, expenses: none } };
    const d = await sheet("?co=NORD&tab=parteneri&p=NORD.1014", "Depozit Exemplu SRL", withPartner("NORD.1014", { turnover }));
    expect(within(d).getByText("Vânzări", { selector: "dt" }).nextElementSibling).toHaveTextContent(
      /^-100,00 leidin care -100,00 lei din istoric$/);
  });

  it("a firm never checked gets one quiet line and no invented values", async () => {
    const d = await sheet("?co=NORD&tab=parteneri&p=NORD.1022", "Atelier Partener SRL", fixture());
    const shown = facts(d);
    expect(shown.at(-1)).toBe("Nu a fost verificat încă la ANAF.");
    expect(shown.join(" ")).not.toMatch(/Plătitor de TVA|e-Factura|TVA la încasare|Inactiv|Date ANAF/);
  });

  it("a flag ANAF didn't give is left out, and VAT without a known start is a plain Da", async () => {
    const s = fixture();
    const base = s.partners.find((p) => p.id === "NORD.1006")!.anaf_status!;
    const d = await sheet("?co=NORD&tab=parteneri&p=NORD.1006", "Papetarie Birou SRL", withPartner("NORD.1006",
      { anaf_status: { ...base, vat_since: null, efactura_registered: null, inactive: null, vat_on_collection: null } }));
    expect(facts(d)).toEqual(["CUI: 1006", "Cont bancar: •••• 0106", "Plătitor de TVA: Da", "Date ANAF din 25 oct."]);
  });

  it.each([
    ["a private person", "NORD.client-client-private", "Persoană Exemplu"],
    ["a foreign partner", "NORD.DE999999999", "Exemplu Europa GmbH"],
  ])("%s shows nothing about ANAF", async (_, id, name) => {
    const d = await sheet(`?co=NORD&tab=parteneri&p=${id}`, name, fixture());
    expect(d.textContent).not.toMatch(/ANAF|Plătitor de TVA|e-Factura|Inactiv/);
  });

  it("the engine decides who is looked up: a partner it doesn't check shows nothing even beside a cached answer", async () => {
    const s = fixture();
    const status = s.partners.find((p) => p.id === "NORD.1006")!.anaf_status;
    const d = await sheet("?co=NORD&tab=parteneri&p=NORD.DE999999999", "Exemplu Europa GmbH",
      withPartner("NORD.DE999999999", { anaf_eligible: false, anaf_error: "not_found", anaf_status: { ...status!, inactive: false } }));
    expect(d.textContent).not.toMatch(/Date ANAF|ANAF nu găsește|Plătitor de TVA/);
  });

  it("the list marks only inactive firms", async () => {
    at("?co=ALL&tab=parteneri");
    renderApp({ all: () => json(fixtureAll()) });
    const lab = await screen.findByRole("link", { name: /Laborator Analize SRL/ });
    expect(lab).toHaveTextContent("Laborator Analize SRLInactivă la ANAF, STUDIO9, furnizor, CUI 2001, cheltuieli 230,00 lei230,00 leiDe plătit");
    expect(lab.querySelector(".ic")).toHaveClass("late");
    const marked = screen.getAllByRole("link").filter((a) => a.textContent?.includes("Inactivă la ANAF"));
    expect(marked).toEqual([lab]);
    const normal = screen.getByRole("link", { name: /Papetarie Birou SRL/ });
    expect(within(normal).queryByText(/ANAF/)).toBeNull();
  });
});
