// A partner's sheet: its details (CUI, address, VAT status, the accounts it was paid to), and the actions the engine already
// has for a client: Factură nouă with it chosen, Modifică for a saved one (client-save with its id). Against a fake engine.
import { act, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import App from "@/components/App";
import { parse } from "@/lib/nav";
import type { State } from "@/lib/types";
import { facts, at, fixture, fixtureAll, invoicingData, json, server, renderApp, becomeVisible, deferred, partnerResult, summaryResult } from "./helpers";

beforeEach(() => localStorage.clear());

const ADDRESS = { street: "Str. Exemplului 1", city: "Cluj-Napoca", county: "RO-CJ", postal_code: "400001", country: "RO" };

/** NORD, with Client Alfa SRL saved for invoicing as c1 (invoicingData's client). */
function withClient(id = "NORD.5001"): State {
  const s = fixture();
  s.partners = s.partners.map((p) => (p.id === "NORD.5001"
    ? { ...p, id, client_id: "c1", vat_id: "RO5001", address: ADDRESS, country: "RO", ro_vat_status: "registered" } : p));
  return s;
}

/** Invoicing, with c1 as the engine stores it after an ANAF lookup (invented data). */
function savedInvoicing() {
  const inv = invoicingData();
  inv.clients[0] = { ...inv.clients[0], ...ANAF };
  return inv;
}
const ANAF = { reg_com: "J12/5001/2020", phone: "0700000001", iban: "RO00TEST0000000000005001", bank: "Banca Exemplu",
  vat_payer: true, efactura_registered: true, inactive: false, ro_vat_status: "registered" as const };
const CLEARED = { reg_com: "", phone: "", iban: "", bank: "", vat_payer: false, efactura_registered: false, inactive: false };
const SAVED = { ok: true, message: "Clientul a fost salvat.", id: "c1" };

/** Opens Modifică on Client Alfa SRL; returns the sheet. */
async function editAlfa() {
  const d = await screen.findByRole("dialog", { name: "Client Alfa SRL" });
  await userEvent.click(within(d).getByRole("button", { name: "Modifică" }));
  await within(d).findByLabelText("Nume");
  return d;
}

it("keeps an edited partner form through the same partner's pending refresh, failure and retry", async () => {
  const full = withClient(), pending = deferred<Response>();
  let reads = 0;
  at("?co=NORD&tab=parteneri&p=NORD.5001");
  renderApp({ state: () => json(full), invoicing: () => json(savedInvoicing()), partnerPages: (url) => {
    reads++;
    return reads === 2 ? pending.promise : json(partnerResult(full, Object.fromEntries(new URL(url, "http://local").searchParams)));
  } });
  const dialog = await editAlfa(), name = within(dialog).getByLabelText("Nume");
  await userEvent.clear(name);
  await userEvent.type(name, "Client modificat");
  await becomeVisible();
  await waitFor(() => expect(reads).toBe(2));
  expect(within(dialog).getByLabelText("Nume")).toBe(name);
  expect(name).toHaveValue("Client modificat");
  pending.resolve(json({}, 500));
  await within(dialog).findByRole("alert");
  await userEvent.click(within(dialog).getByRole("button", { name: "Reîncearcă" }));
  await waitFor(() => expect(reads).toBe(3));
  expect(within(dialog).getByLabelText("Nume")).toBe(name);
  expect(name).toHaveValue("Client modificat");
});

describe("Partenerul", () => {
  it("a supplier shows its CUI and the accounts it was paid to, and no invoicing actions", async () => {
    const s = fixture();
    s.partners = s.partners.map((p) => (p.id === "NORD.1001" ? { ...p, iban_last4: ["0101", "9999"] } : p));
    at("?co=NORD&tab=parteneri&p=NORD.1001");
    renderApp({ state: () => json(s) });
    const d = await screen.findByRole("dialog", { name: "Energie Furnizare SA" });
    expect(facts(d)).toEqual(["CUI: 1001", "Conturi bancare: •••• 0101, •••• 9999"]);
    expect(within(d).queryByRole("link", { name: "Factură nouă" })).toBeNull();
    expect(within(d).queryByRole("button", { name: "Modifică" })).toBeNull();
  });

  it("a saved client shows its address and VAT status, and Factură nouă opens with it chosen; back returns here", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    renderApp({ state: () => json(withClient()) });
    const d = await screen.findByRole("dialog", { name: "Client Alfa SRL" });
    expect(facts(d)).toEqual(["CUI: 5001", "Cod de TVA: RO5001", "Adresă: Str. Exemplului 1, 400001 Cluj-Napoca, CJ",
      "TVA: Înregistrat în scopuri de TVA în România"]);
    const link = within(d).getByRole("link", { name: "Factură nouă" });
    expect(link).toHaveAttribute("href", "?co=NORD&tab=parteneri&new=1&for=NORD.5001");
    await userEvent.click(link);
    const form = await screen.findByRole("dialog", { name: "Factură nouă" });
    await waitFor(() => expect(within(form).getByRole("combobox", { name: "Client" })).toHaveValue("c1"));
    window.history.back();
    expect(await screen.findByRole("dialog", { name: "Client Alfa SRL" })).toBeInTheDocument();
  });

  it("the invoice for a partner is saved with its client", async () => {
    at("?co=NORD&tab=parteneri&new=1&for=NORD.5001");
    const full = withClient();
    const api = renderApp({ state: () => json(summaryResult(full)),
      partnerPages: (url) => json(partnerResult(full, Object.fromEntries(new URL(url, "http://local").searchParams))),
      onPost: () => json({ ok: true, message: "Ciorna a fost salvată.", id: "d2", invoicing: invoicingData(), state: summaryResult(full) }) });
    const f = within(await screen.findByRole("dialog", { name: "Factură nouă" }));
    await waitFor(() => expect(f.getByRole("combobox", { name: "Client" })).toHaveValue("c1"));
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "50.00");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    expect(api.posts()[0].url).toContain("api/invoicing/draft-save");
    expect(api.posts()[0].body).toEqual({ co: "NORD", series: "NRD", client_id: "c1", issue_date: "2026-10-27",
      due_date: "2026-11-26", currency: "RON", notes: "", advance_invoice: false, advance_payment_date: null, iban: "RO00TEST0000000000000001",
      lines: [{ description: "Consultanță", quantity: "1", unit: "H87", unit_price: "50.00", vat_category: "S", vat_rate: "21",
        exemption: "" }] });
  });

  it("waits for a bounded partner identity and retries before mounting the prefilled form", async () => {
    const full = withClient(), late = deferred<Response>(); let fail = true;
    at("?co=NORD&tab=parteneri&new=1&for=NORD.5001");
    const api = renderApp({ state: () => json(summaryResult(full)), partnerPages: (url) => fail ? late.promise
      : json(partnerResult(full, Object.fromEntries(new URL(url, "http://local").searchParams))) });
    const dialog = await screen.findByRole("dialog", { name: "Factură nouă" });
    expect(within(dialog).queryByRole("combobox", { name: "Client" })).toBeNull();
    await act(async () => late.resolve(json({}, 503)));
    await screen.findByRole("alert"); fail = false;
    await userEvent.click(within(dialog).getByRole("button", { name: "Reîncearcă" }));
    await waitFor(() => expect(within(dialog).getByRole("combobox", { name: "Client" })).toHaveValue("c1"));
    const description = within(dialog).getByLabelText("Produs sau serviciu");
    await userEvent.type(description, "Consultanță");
    becomeVisible();
    await waitFor(() => expect(api.gets()).toHaveLength(2));
    expect(within(dialog).getByLabelText("Produs sau serviciu")).toBe(description);
    expect(description).toHaveValue("Consultanță");
  });

  it("a client known only from invoices opens the new client's form filled in, with nothing chosen", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5002");
    renderApp();
    const d = await screen.findByRole("dialog", { name: "Client Beta SRL" });
    expect(within(d).queryByRole("button", { name: "Modifică" })).toBeNull(); // not saved: nothing to change
    await userEvent.click(within(d).getByRole("link", { name: "Factură nouă" }));
    const f = within(await screen.findByRole("dialog", { name: "Factură nouă" }));
    expect(await f.findByLabelText("CUI")).toHaveValue("5002");
    expect(f.getByLabelText("Nume")).toHaveValue("Client Beta SRL");
    expect(f.getByRole("combobox", { name: "Client" })).toHaveValue("");
  });

  it("Modifică saves the client by its id and stays on the sheet, focus back on the button", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    const api = renderApp({ state: () => json(withClient()), invoicing: () => json(savedInvoicing()),
      onPost: () => json({ ...SAVED, invoicing: savedInvoicing(), state: withClient() }) });
    const d = await editAlfa();
    await waitFor(() => expect(within(d).getByLabelText("Persoană fizică")).toHaveFocus());
    expect(within(d).getByLabelText("Nume")).toHaveValue("Client Alfa SRL");
    await userEvent.type(within(d).getByLabelText(/E-mail/), "contabil@example.com");
    await userEvent.click(within(d).getByRole("button", { name: "Salvează clientul" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toContain("api/invoicing/client-save");
    // Only the e-mail changed: the identity is the same, so its VAT registration and ANAF's data stay as saved.
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "c1", client: { name: "Client Alfa SRL", cui: "5001", vat_id: "RO5001",
      private: false, country: "RO", address: ADDRESS, email: "contabil@example.com", tax_id: "", ...ANAF } });
    expect(await screen.findByText("Clientul a fost salvat.")).toBeInTheDocument();
    await waitFor(() => expect(within(d).getByRole("button", { name: "Modifică" })).toHaveFocus());
    expect(window.location.search).toBe("?co=NORD&tab=parteneri&p=NORD.5001");
  });

  it("a new CUI clears what ANAF said of the former one and starts its VAT registration over", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    const api = renderApp({ state: () => json(withClient()), invoicing: () => json(savedInvoicing()),
      onPost: () => json({ ...SAVED, invoicing: savedInvoicing(), state: withClient() }) });
    const d = await editAlfa();
    await userEvent.clear(within(d).getByLabelText("CUI"));
    await userEvent.type(within(d).getByLabelText("CUI"), "5009");
    await userEvent.clear(within(d).getByLabelText(/Cod de TVA/));
    await userEvent.type(within(d).getByLabelText(/Cod de TVA/), "RO5009");
    await userEvent.click(within(d).getByRole("button", { name: "Salvează clientul" }));
    const saves = () => api.posts().filter((p) => p.url.endsWith("/client-save")); // leaving the CUI also asked ANAF
    await waitFor(() => expect(saves()).toHaveLength(1));
    expect(saves()[0].body).toEqual({ co: "NORD", id: "c1", client: { name: "Client Alfa SRL", cui: "5009", vat_id: "RO5009",
      private: false, country: "RO", address: ADDRESS, email: "", tax_id: "", ro_vat_status: "unknown", ...CLEARED } });
  });

  it("a new CUI looked up at ANAF keeps the new data and clears only what the lookup didn't bring", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    const api = renderApp({ state: () => json(withClient()), invoicing: () => json(savedInvoicing()),
      onPost: (c) => json(c.url.includes("client-lookup")
        ? { ok: true, message: "", client: { name: "Firma Noua SRL", cui: "5009", vat_id: "", reg_com: "J12/5009/2021", vat_payer: false,
          efactura_registered: false, inactive: false } }
        : { ...SAVED, invoicing: savedInvoicing(), state: withClient() }) });
    const d = await editAlfa();
    await userEvent.clear(within(d).getByLabelText("CUI"));
    await userEvent.type(within(d).getByLabelText("CUI"), "5009");
    await userEvent.click(within(d).getByLabelText("CUI"));
    await userEvent.tab(); // leaving the CUI asks ANAF
    await waitFor(() => expect(within(d).getByLabelText("Nume")).toHaveValue("Firma Noua SRL"));
    await userEvent.click(within(d).getByRole("button", { name: "Salvează clientul" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts()[1].body).toEqual({ co: "NORD", id: "c1", client: { name: "Firma Noua SRL", cui: "5009", vat_id: "",
      private: false, country: "RO", address: ADDRESS, email: "", tax_id: "", ro_vat_status: "unknown", reg_com: "J12/5009/2021",
      vat_payer: false, efactura_registered: false, inactive: false, phone: "", iban: "", bank: "" } });
  });

  it("a CUI typed away and back is the same firm: its saved data stays", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    const api = renderApp({ state: () => json(withClient()), invoicing: () => json(savedInvoicing()),
      onPost: () => json({ ...SAVED, invoicing: savedInvoicing(), state: withClient() }) });
    const d = await editAlfa();
    await userEvent.type(within(d).getByLabelText("CUI"), "{Backspace}1");
    await userEvent.click(within(d).getByRole("button", { name: "Salvează clientul" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "c1", client: { name: "Client Alfa SRL", cui: "5001", vat_id: "RO5001",
      private: false, country: "RO", address: ADDRESS, email: "", tax_id: "", ...ANAF } });
  });

  it("a foreign client without a CUI whose VAT id changes is another firm: the former one's data is cleared", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    const DE = { ...ADDRESS, county: "", country: "DE" };
    const inv = () => {
      const x = savedInvoicing();
      x.clients[0] = { ...x.clients[0], cui: "", vat_id: "DE123456789", tax_id: "", country: "DE", address: DE,
        ro_vat_status: "not_registered" };
      return x;
    };
    const api = renderApp({ state: () => json(withClient()), invoicing: () => json(inv()),
      onPost: () => json({ ...SAVED, invoicing: inv(), state: withClient() }) });
    const d = await editAlfa();
    await userEvent.clear(within(d).getByLabelText(/Cod de TVA/));
    await userEvent.type(within(d).getByLabelText(/Cod de TVA/), "DE987654321");
    await userEvent.click(within(d).getByRole("button", { name: "Salvează clientul" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "c1", client: { name: "Client Alfa SRL", cui: "", vat_id: "DE987654321",
      tax_id: "", private: false, country: "DE", address: DE, email: "", ro_vat_status: "not_registered", ...CLEARED } });
  });

  const lookedUp = () => server({ state: () => json(withClient()), invoicing: () => json(savedInvoicing()),
    onPost: (c) => json(c.url.includes("client-lookup")
      ? { ok: true, message: "", client: { name: "Firma Noua SRL", cui: "5009", vat_id: "RO5009", reg_com: "J12/5009/2021",
        phone: "0700000009", vat_payer: true, efactura_registered: true, inactive: false } }
      : { ...SAVED, invoicing: savedInvoicing(), state: withClient() }) });
  async function lookUp5009(d: HTMLElement) {
    await userEvent.clear(within(d).getByLabelText("CUI"));
    await userEvent.type(within(d).getByLabelText("CUI"), "5009");
    await userEvent.click(within(d).getByLabelText("CUI"));
    await userEvent.tab(); // leaving the CUI asks ANAF
    await waitFor(() => expect(within(d).getByLabelText("Nume")).toHaveValue("Firma Noua SRL"));
  }

  it.each([
    ["VAT id changed", /Cod de TVA/, "RO5010", "RO5010"],
    ["CUI cleared and retyped", "CUI", "5009", "RO5009"],
  ])("discards ANAF lookup data after identity edits: %s", async (_, label, value, vat_id) => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    const api = lookedUp();
    render(<App />);
    const d = await editAlfa();
    await lookUp5009(d);
    await userEvent.clear(within(d).getByLabelText(label));
    await userEvent.type(within(d).getByLabelText(label), value);
    await userEvent.click(within(d).getByRole("button", { name: "Salvează clientul" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts()[1].body).toEqual({ co: "NORD", id: "c1", client: { name: "Firma Noua SRL", cui: "5009", vat_id,
      private: false, country: "RO", address: ADDRESS, email: "", tax_id: "", ro_vat_status: "unknown", ...CLEARED } });
  });

  it("a refused save keeps the form and what was typed", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    const refusal = "Există clienți cu aceeași identitate fiscală. Clienții trebuie corectați separat înainte de salvare.";
    const api = renderApp({ state: () => json(withClient()), invoicing: () => json(savedInvoicing()),
      onPost: () => json({ ok: false, message: refusal, id: null, invoicing: null, state: null }) });
    const d = await editAlfa();
    await userEvent.type(within(d).getByLabelText(/E-mail/), "contabil@example.com");
    await userEvent.click(within(d).getByRole("button", { name: "Salvează clientul" }));
    expect(await screen.findByText(refusal)).toBeInTheDocument();
    expect(api.posts()).toHaveLength(1);
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "c1", client: { name: "Client Alfa SRL", cui: "5001", vat_id: "RO5001",
      private: false, country: "RO", address: ADDRESS, email: "contabil@example.com", tax_id: "", ...ANAF } });
    expect(within(d).getByLabelText(/E-mail/)).toHaveValue("contabil@example.com");
    expect(within(d).getByRole("button", { name: "Salvează clientul" })).toBeEnabled();
  });

  it.each([
    ["another company's partner", "STUDIO9.6001"], ["a supplier", "NORD.1001"], ["an unknown id", "NORD.nope"],
    ["markup", '"><img src=x onerror=alert(1)>'], ["a path", "../../etc/passwd"],
  ])("a new invoice for %s chooses and fills in nothing", async (_, id) => {
    at(`?co=NORD&tab=vanzari&new=1&for=${encodeURIComponent(id)}`);
    renderApp();
    const f = within(await screen.findByRole("dialog", { name: "Factură nouă" }));
    expect(await f.findByRole("combobox", { name: "Client" })).toHaveValue("");
    expect(f.queryByLabelText("CUI")).toBeNull(); // the new client's form stays closed
    expect(document.querySelector("img[src=x]")).toBeNull();
  });

  it("a new invoice for a partner is not opened under Toate firmele", async () => {
    at("?co=ALL&tab=parteneri&new=1&for=NORD.5001");
    renderApp();
    await screen.findByRole("searchbox", { name: "Caută partener sau CUI" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Renunță drops the changes without a request", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    const api = renderApp({ state: () => json(withClient()) });
    const d = await screen.findByRole("dialog", { name: "Client Alfa SRL" });
    await userEvent.click(within(d).getByRole("button", { name: "Modifică" }));
    await userEvent.type(await within(d).findByLabelText(/E-mail/), "x@example.com");
    await userEvent.click(within(d).getByRole("button", { name: "Renunță" }));
    expect(within(d).queryByLabelText(/E-mail/)).toBeNull();
    expect(within(d).getByRole("button", { name: "Modifică" })).toHaveFocus();
    expect(api.posts()).toHaveLength(0);
  });

  it("a client whose CUI changed gets a new partner id, and the sheet follows it", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    renderApp({ state: () => json(withClient()), onPost: () => json({ ok: true, message: "Clientul a fost salvat.", id: "c1",
      invoicing: invoicingData(), state: withClient("NORD.5009") }) });
    const d = await screen.findByRole("dialog", { name: "Client Alfa SRL" });
    await userEvent.click(within(d).getByRole("button", { name: "Modifică" }));
    await userEvent.click(await within(d).findByRole("button", { name: "Salvează clientul" }));
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=parteneri&p=NORD.5009"));
    expect(await screen.findByRole("dialog", { name: "Client Alfa SRL" })).toBeInTheDocument();
  });

  it("a saved client that invoicing no longer has says so, and changes nothing", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.99999992");
    const api = renderApp();
    const d = await screen.findByRole("dialog", { name: "Client Exemplu SRL" });
    await userEvent.click(within(d).getByRole("button", { name: "Modifică" }));
    expect(await within(d).findByRole("alert")).toHaveTextContent("Clientul nu mai este salvat.");
    await userEvent.click(within(d).getByRole("button", { name: "Renunță" }));
    expect(within(d).getByRole("button", { name: "Modifică" })).toHaveFocus();
    expect(api.posts()).toHaveLength(0);
  });

  it("a private person has no CUI or address rows, and keeps its actions", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.client-client-private");
    renderApp();
    const d = await screen.findByRole("dialog", { name: "Persoană Exemplu" });
    expect(d.querySelector(".card")).toBeNull();
    expect(within(d).getByRole("link", { name: "Factură nouă" })).toHaveAttribute("href",
      "?co=NORD&tab=parteneri&new=1&for=NORD.client-client-private");
    expect(within(d).getByRole("button", { name: "Modifică" })).toBeEnabled();
  });

  it("under Toate firmele, Factură nouă and Modifică are disabled", async () => {
    at("?co=ALL&tab=parteneri&p=NORD.99999992");
    renderApp({ all: () => json(fixtureAll()) });
    const d = await screen.findByRole("dialog", { name: "Client Exemplu SRL" });
    expect(within(d).queryByRole("link", { name: "Factură nouă" })).toBeNull();
    expect(within(d).getByRole("button", { name: "Factură nouă" })).toBeDisabled();
    expect(within(d).getByRole("button", { name: "Modifică" })).toBeDisabled();
  });

  it("names a partner for a new invoice only, never for a draft", () => {
    expect(parse("?co=NORD&new=1&for=NORD.5001").for).toBe("NORD.5001");
    expect(parse("?co=NORD&new=d1&for=NORD.5001").for).toBe("");
    expect(parse("?co=NORD&for=NORD.5001").for).toBe("");
  });
});

describe("Partener nou", () => {
  // What ANAF's public registry returns for the CUI (invented): sent back unchanged with the role.
  const FOUND = { name: "Firma Noua SRL", cui: "5009", vat_id: "RO5009", tax_id: "", private: false, country: "RO", address: ADDRESS,
    email: "", ...ANAF };
  /** NORD after the save: the new partner, saved as c9. */
  const saved = (role: "supplier" | "client") => {
    const s = fixture();
    s.partners = [{ ...s.partners[0], id: "NORD.5009", name: FOUND.name, cui: "5009", roles: [role], client_id: "c9", anaf_status: null,
      anaf_error: null, received: 0, issued: 0, late: 0, payable: { total: "0.00", other: [] }, receivable: { total: "0.00", other: [] } }, ...s.partners];
    return s;
  };
  const open = async (inactive = false) => {
    at("?co=NORD&tab=parteneri");
    const api = renderApp({ strict: true, onPost: ({ url, body }) => url.endsWith("/client-lookup")
      ? json({ ok: true, message: "", id: null, client: { ...FOUND, inactive } })
      : json({ ok: true, message: "", id: "c9", state: saved((body as { role: "supplier" | "client" }).role) }) });
    await userEvent.click(await screen.findByRole("link", { name: "Partener nou" }));
    const d = await screen.findByRole("dialog", { name: "Partener nou" });
    await userEvent.type(within(d).getByLabelText("CUI"), "5009");
    return { api, d };
  };

  it.each([["supplier", "Adaugă furnizor", true], ["client", "Adaugă client", false]] as const)(
    "a CUI is enough: ANAF fills in the firm and %s saves it, then its sheet opens", async (role, button, leaveFirst) => {
      const { api, d } = await open();
      if (leaveFirst) {
        await userEvent.tab(); // leaving the CUI asks ANAF
        expect(await within(d).findByText("Firma Noua SRL")).toBeInTheDocument();
      }
      await userEvent.click(within(d).getByRole("button", { name: button })); // without a lookup yet, the press asks first
      await screen.findByRole("dialog", { name: "Firma Noua SRL" });
      expect(api.posts().map((p) => [p.url.split("/").at(-1), p.body])).toEqual([
        ["client-lookup", { co: "NORD", cui: "5009" }], ["client-save", { co: "NORD", client: FOUND, role }]]);
      expect(window.location.search).toBe("?co=NORD&tab=parteneri&p=NORD.5009");
    });

  it.each([["the CUI changes", false], ["the sheet closes", true]])("a press waiting for ANAF saves nothing when %s", async (_, close) => {
    const answer = deferred<Response>();
    at("?co=NORD&tab=parteneri&p=nou");
    const api = renderApp({ onPost: () => answer.promise });
    const d = await screen.findByRole("dialog", { name: "Partener nou" });
    await userEvent.type(within(d).getByLabelText("CUI"), "5009");
    await userEvent.click(within(d).getByRole("button", { name: "Adaugă furnizor" }));
    expect(within(d).getByRole("status")).toHaveTextContent("Se caută la ANAF");
    if (close) await userEvent.click(within(d).getByRole("button", { name: "Închide" }));
    else await userEvent.type(within(d).getByLabelText("CUI"), "1");
    await act(async () => answer.resolve(json({ ok: true, message: "", id: null, client: FOUND })));
    expect(api.posts().map((p) => p.url.split("/").at(-1))).toEqual(["client-lookup"]);
  });

  it("an inactive firm is shown and can't be added", async () => {
    const { api, d } = await open(true);
    await userEvent.tab();
    expect(await within(d).findByRole("alert")).toHaveTextContent("inactivă la ANAF");
    expect(within(d).getByRole("button", { name: "Adaugă furnizor" })).toBeDisabled();
    expect(api.posts()).toHaveLength(1);
  });

  it("waits for a firm under Toate firmele", async () => {
    at("?co=ALL&tab=parteneri");
    renderApp({ all: () => json(fixtureAll()) });
    expect(await screen.findByRole("button", { name: "Partener nou" })).toBeDisabled();
  });
});

it("the sheet shows what is owed and the invoices before the details, which stay closed", async () => {
  at("?co=NORD&tab=parteneri&p=NORD.99999992");
  renderApp();
  const d = await screen.findByRole("dialog", { name: "Client Exemplu SRL" });
  const text = d.textContent ?? "";
  expect(text.indexOf("De încasat")).toBeLessThan(text.indexOf("Facturi emise"));
  expect(text.indexOf("Facturi emise")).toBeLessThan(text.indexOf("Detalii"));
  expect(d.querySelector("details")).not.toHaveAttribute("open");
});

it("offers a retry when the invoicing data can't be read for Modifică", async () => {
  let reads = 0;
  at("?co=NORD&tab=parteneri&p=NORD.5001");
  renderApp({ state: () => json(withClient()), invoicing: () => (++reads === 1 ? json({}, 500) : json(savedInvoicing())) });
  const d = await screen.findByRole("dialog", { name: "Client Alfa SRL" });
  await userEvent.click(within(d).getByRole("button", { name: "Modifică" }));
  await userEvent.click(await within(d).findByRole("button", { name: "Reîncearcă" }));
  expect(await within(d).findByLabelText("Nume")).toHaveValue("Client Alfa SRL");
});

it("a late retry answer never replaces the invoicing data the app read meanwhile", async () => {
  const late = deferred<Response>();
  let reads = 0;
  at("?co=NORD&tab=parteneri&p=NORD.5001");
  renderApp({ state: () => json(withClient()), invoicing: () => (++reads === 1 ? json({}, 500) : reads === 2 ? late.promise : json(savedInvoicing())) });
  const d = await screen.findByRole("dialog", { name: "Client Alfa SRL" });
  await userEvent.click(within(d).getByRole("button", { name: "Modifică" }));
  await userEvent.click(await within(d).findByRole("button", { name: "Reîncearcă" }));
  await becomeVisible();
  const name = await within(d).findByLabelText("Nume");
  await userEvent.type(name, " nou");
  await act(async () => late.resolve(json({ ...invoicingData(), clients: [] })));
  expect(within(d).getByLabelText("Nume")).toHaveValue("Client Alfa SRL nou");
});
