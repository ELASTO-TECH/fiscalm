// Factură nouă against a fake engine: the way in from Facturi › Emise, the setup a company needs once, drafting,
// the totals the engine computes, issuing with its confirm, clients from ANAF, and refusals before going live.
import { act, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render, choose, choicesOf } from "./helpers";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import type { Invoicing, Product } from "@/lib/types";
import { answer as decide, at, becomeVisible, deferred, fixture, invoicingData, json, server, type Call, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());

const sheet = () => screen.getByRole("dialog");
const CLOSE_UNSAVED = "Închizi fără să salvezi? Modificările nu se păstrează.";
/** Leaves a client's CUI field, which asks ANAF about it. */
async function lookUp(cui: HTMLElement) {
  await userEvent.click(cui);
  await userEvent.tab();
}
/** What was sent besides ANAF lookups, which leaving a CUI field asks. */
const sent = (api: { posts: () => Call[] }) => api.posts().filter((p) => !p.url.endsWith("/client-lookup"));
/** The sheet, once its form is there (the invoicing data arrives after the sheet opens). */
async function form() {
  const d = await screen.findByRole("dialog", { name: /Factură nouă|Ciornă de factură/ });
  await waitFor(() => expect(d.querySelector("form")).not.toBeNull());
  return within(d);
}

/** Open one invoice form with the shared fake API; tests supply only the state they need. */
async function openInvoice(id: string, options: Parameters<typeof renderApp>[0] = {}) {
  at(`?co=NORD&tab=vanzari&new=${id}`);
  const api = renderApp(options);
  return { api, f: await form() };
}

/** A firm whose invoices lack only its account, everything else known; `ready` is its invoicing once the account is saved. */
function lacksAccount() {
  const full = invoicingData();
  const known = Object.fromEntries(Object.entries(full.seller!).filter(([k]) => !k.startsWith("logo"))) as Invoicing["seller_prefill"];
  const ready = { ...full, series: [], series_new: { code: "AN", next_number: 1 } };
  return { known, ready, inv: { ...ready, seller: null, accounts: [], seller_prefill: { ...known, iban: "", bank: "" } } as Invoicing };
}

/** The engine's answer to an invoicing POST: ok, a message, the invoicing data after it, the id it made. */
const answer = (inv: Invoicing, message: string, id: string | null = null, extra = {}) =>
  json({ ok: true, message, invoicing: inv, id, version: inv.drafts.find((d) => d.id === id)?.version, state: fixture(), ...extra });

/** Runs an action, then waits until the sheet's read of invoicing that follows it (one after every action) has
 * landed, so what the test checks next is what stays on screen, not a moment before that read replaces it. */
async function settled(api: { calls: Call[] }, action: () => Promise<void>) {
  const reads = () => api.calls.filter((c) => c.url.includes("api/invoicing?")).length;
  const before = reads();
  await action();
  await waitFor(() => expect(reads()).toBeGreaterThan(before));
  await act(() => new Promise((ok) => setTimeout(ok)));
}

describe("Factură nouă", () => {
  it("starts from Facturi › Emise, where drafts wait too", async () => {
    at("?co=NORD&tab=vanzari");
    renderApp();
    const link = within(await screen.findByRole("main")).getByRole("link", { name: "Factură nouă" });
    expect(link).toHaveAttribute("href", "?co=NORD&tab=vanzari&new=1");
    const drafts = await screen.findByRole("region", { name: "Ciorne" });
    expect(within(drafts).getByRole("link", { name: /Client Alfa SRL/ })).toHaveAttribute("href",
      "?co=NORD&tab=vanzari&new=d1");
    expect(within(drafts).getByRole("link", { name: /Client Alfa SRL/ })).toHaveTextContent("2.420,00 lei");
  });

  it("offers another read when the invoicing data can't be read", async () => {
    let down = true;
    at("?co=NORD&tab=vanzari&new=1");
    renderApp({ invoicing: () => (down ? json({}, 503) : json(invoicingData())) });
    const d = await screen.findByRole("dialog", { name: "Factură nouă" });
    const retry = await within(d).findByRole("button", { name: "Reîncearcă" });
    down = false;
    await userEvent.click(retry);
    expect(await within(d).findByRole("button", { name: "Emite factura" })).toBeInTheDocument();
  });

  it("frees Reîncearcă once its read settles, even when a refresh replaced it", async () => {
    const retried = deferred<Response>();
    let phase = "down", stalled = 0;
    at("?co=NORD&tab=vanzari&new=1");
    renderApp({ invoicing: () => (phase === "down" ? json({}, 500) : phase === "retry" ? retried.promise
      : (stalled++, new Promise<Response>(() => {}))) });
    const d = await screen.findByRole("dialog", { name: "Factură nouă" });
    const retry = await within(d).findByRole("button", { name: "Reîncearcă" });
    phase = "retry";
    await userEvent.click(retry);
    expect(retry).toBeDisabled();
    phase = "stalled";
    await becomeVisible();
    await waitFor(() => expect(stalled).toBeGreaterThan(0));
    await act(async () => retried.resolve(json({}, 500)));
    await waitFor(() => expect(within(d).getByRole("button", { name: "Reîncearcă" })).toBeEnabled());
  });

  it("is not offered under Toate firmele, where it isn't clear which company issues it", async () => {
    at("?co=ALL&tab=vanzari");
    renderApp();
    await screen.findByText("Vânzări", { selector: ".ttl" });
    expect(screen.queryByRole("link", { name: "Factură nouă" })).toBeNull();
  });

  it("offers registry facts as editable fields without saving them", async () => {
    const inv = invoicingData();
    inv.seller = null;
    inv.seller_prefill = { ...inv.seller_prefill, cui: "12345674", reg_com: "J00/123/2026", vat_payer: false,
      address: { country: "RO", county: "RO-CJ", city: "Localitate Exemplu", street: "Strada Exemplu 1", postal_code: "000000" } };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv) });
    expect(f.queryByLabelText("CUI")).toBeNull();
    expect(f.queryByLabelText("Adresă")).toBeNull();
    expect(f.getByLabelText("Contul pe factură")).toBeInTheDocument();
    await userEvent.click(f.getByRole("button", { name: "Modifică datele firmei" }));
    expect(f.getByLabelText("CUI")).toHaveValue("12345674");
    expect(f.getByLabelText("Adresă")).toHaveValue("Strada Exemplu 1");
    expect(f.getByLabelText("Neplătitor de TVA")).toBeChecked();
    await userEvent.clear(f.getByLabelText("Adresă"));
    await userEvent.type(f.getByLabelText("Adresă"), "Alt sediu");
    expect(f.getByLabelText("Adresă")).toHaveValue("Alt sediu");
    expect(api.posts()).toHaveLength(0);
  });

  it.each([true, false])("skips company details when the engine has a seller; series exists: %s", async (hasSeries) => {
    const inv = invoicingData();
    if (!hasSeries) inv.series = [];
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv) });
    expect(f.queryByLabelText("CUI")).toBeNull();
    expect(f.getByLabelText("Client")).toBeInTheDocument(); // the invoice form, with or without a series yet
    expect(api.posts()).toHaveLength(0);
  });

  it("asks for the company's details first, and keeps the sheet open while setting up", async () => {
    let current: Invoicing = { ...invoicingData(), seller: null, series: [] }; // what the engine holds, as it changes
    const { api, f } = await openInvoice("1", { invoicing: () => json(current), onPost: () => {
      current = { ...invoicingData(), series: [] };
      return answer(current, "Am salvat datele firmei.");
    } });
    expect(f.getByRole("heading", { name: "Completează pentru facturare" })).toBeInTheDocument();
    const save = f.getByRole("button", { name: /Salvează datele firmei|Continuă/ });
    expect(save).toBeEnabled(); // reachable by Tab: a press says what is missing
    await userEvent.clear(f.getByLabelText("CUI"));
    await userEvent.type(f.getByLabelText("CUI"), "11111111");
    await userEvent.type(f.getByLabelText("Adresă"), "Str. Exemplului 1");
    await choose(f.getByLabelText("Contul pe factură"), "RO00TEST0000000000000001");
    await userEvent.type(f.getByLabelText("Localitate"), "Cluj-Napoca");
    await choose(f.getByLabelText("Județ"), "Cluj");
    await userEvent.type(f.getByLabelText("Banca"), "Banca Exemplu");
    await userEvent.click(save); // the VAT status is declared, never assumed
    expect(api.posts()).toHaveLength(0);
    await waitFor(() => expect(f.getByLabelText("Plătitor de TVA")).toHaveFocus());
    const vat = f.getByRole("radiogroup", { name: "TVA" });
    expect(vat).toBeRequired();
    expect(vat).toBeInvalid();
    expect(vat).toHaveAccessibleDescription("Lipsește alegerea: firma este sau nu plătitoare de TVA.");
    for (const choice of [f.getByLabelText("Plătitor de TVA"), f.getByLabelText("Neplătitor de TVA")]) {
      expect(choice).toBeRequired();
      expect(choice).toBeInvalid();
      expect(choice).toHaveAccessibleDescription("Lipsește alegerea: firma este sau nu plătitoare de TVA."); // read with the choice it lands on
    }
    await userEvent.click(f.getByLabelText("Plătitor de TVA"));
    expect(f.queryByLabelText(/Cod de TVA/)).toBeNull(); // RO and the CUI: the engine writes it
    expect(f.queryByText("Lipsește alegerea: firma este sau nu plătitoare de TVA.")).toBeNull(); // said until it is answered
    for (const el of [vat, f.getByLabelText("Plătitor de TVA"), f.getByLabelText("Neplătitor de TVA")]) {
      expect(el).not.toHaveAttribute("aria-invalid");
      expect(el).not.toHaveAttribute("aria-describedby");
    }
    await userEvent.click(save);
    const body = api.posts()[0].body as Record<string, { cui: string; vat_payer: boolean; address: { street: string } }>;
    expect(api.posts()[0].url).toContain("api/invoicing/seller-save");
    expect(body.seller.cui).toBe("11111111");
    expect(body.seller.vat_payer).toBe(true);
    expect(body.seller.address.street).toBe("Str. Exemplului 1");
    expect(window.location.search).toBe("?co=NORD&tab=vanzari&new=1"); // still here: the invoice is next
    expect(await f.findByLabelText("Serie")).toBeInTheDocument();
  });

  it("a company's first series is proposed in the invoice form and created by the first issue, before its draft", async () => {
    const after = invoicingData();
    const { api, f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), series: [], series_new: { code: "NRD", next_number: 43 } }),
      onPost: (c: Call) => answer(after, "Gata.", c.url.endsWith("series-create") ? null : c.url.endsWith("issue") ? "5002" : "d9") });
    expect(f.getByLabelText("Serie")).toHaveValue("NRD");
    await userEvent.clear(f.getByLabelText("Primul număr"));
    await userEvent.type(f.getByLabelText("Primul număr"), "44");
    await choose(f.getByLabelText("Client"), "c1");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    await decide();
    await waitFor(() => expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["series-create", "draft-save", "issue"]));
    expect(api.posts()[0].body).toEqual({ co: "NORD", code: "NRD", start: 44 });
    expect(api.posts()[1].body).toMatchObject({ series: "NRD", client_id: "c1" });
  });

  it("an edit made while the first series is created is not issued", async () => {
    const held = deferred<Response>();
    const { api, f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), series: [], series_new: { code: "NRD", next_number: 1 } }),
      onPost: (c: Call) => (c.url.endsWith("series-create") ? held.promise : answer(invoicingData(), "Gata.", c.url.endsWith("issue") ? "5002" : "d9")) });
    await choose(f.getByLabelText("Client"), "c1");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    await userEvent.type(f.getByLabelText("Preț unitar"), "9"); // while the series is on its way
    await act(async () => held.resolve(answer(invoicingData(), "Seria a fost creată.")));
    await waitFor(() => expect(f.getByRole("alert")).toHaveTextContent("Factura nu a fost emisă: s-a modificat în timp ce se salva."));
    expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["series-create", "draft-save"]);
  });

  it("a refused first series saves no draft", async () => {
    const { api, f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), series: [], series_new: { code: "AN", next_number: 1 } }),
      onPost: () => json({ ok: false, message: "Seria există deja.", id: null, state: fixture() }) });
    await choose(f.getByLabelText("Client"), "c1");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    expect(await screen.findByText("Seria există deja.")).toBeInTheDocument();
    expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["series-create"]);
  });

  it("a series behind the imported invoices continues after them, in one press", async () => {
    const inv = invoicingData();
    const behind = { ...inv, series: inv.series.map((x, n) => (n ? x : { ...x, issued: 0, imported: x.next_number + 4 })) };
    const code = behind.series[0].code;
    const next = behind.series[0].next_number + 5;
    let now: Invoicing = behind; // after the press, the engine's series continues after the imports
    const { api, f } = await openInvoice("1", { invoicing: () => json(now), onPost: () => {
      now = { ...behind, series: behind.series.map((x, n) => (n ? x : { ...x, next_number: next })) };
      return answer(now, "Seria a fost continuată.");
    } });
    expect(f.getByText(`Facturile importate din seria ${code} ajung la numărul ${next - 1}.`)).toBeInTheDocument();
    await userEvent.click(f.getByRole("button", { name: `Continuă seria ${code} de la ${next}` }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toMatchObject({ url: "/web/api/invoicing/series-continue", body: { co: "NORD", code, next_number: next } });
    expect(await screen.findByText("Seria a fost continuată.")).toBeInTheDocument();
    await waitFor(() => expect(f.queryByRole("button", { name: `Continuă seria ${code} de la ${next}` })).toBeNull());
    await waitFor(() => expect(document.activeElement?.id).toBe("inv-client")); // focus goes on to the form
  });

  it("marks the invoice's required fields, keeps its buttons reachable, and a press says what is missing, from the first", async () => {
    const inv = invoicingData();
    inv.clients.push({ ...inv.clients[0], id: "c2", name: "Client Beta SRL", cui: "5002" });
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv) });
    for (const label of ["Client", "Serie", "Scadență", "Produs sau serviciu", "Cantitate", "UM", "Preț unitar", "TVA"]) {
      expect(f.getByLabelText(label)).toBeRequired();
    }
    expect(f.getByLabelText("Mențiuni (opțional)")).not.toBeRequired();
    for (const name of ["Salvează ciorna", "Emite factura"]) {
      const button = f.getByRole("button", { name });
      expect(button).toBeEnabled();
      await userEvent.click(button);
      await waitFor(() => expect(f.getByLabelText("Client")).toHaveFocus());
      f.getByLabelText("Client").blur();
    }
    expect(api.posts()).toHaveLength(0); // nothing sent: the engine would refuse it too
    expect(f.getByLabelText("Client")).toHaveAccessibleDescription("Lipsește clientul.");
    for (const label of ["Produs sau serviciu", "Preț unitar"]) {
      expect(f.getByLabelText(label)).toBeInvalid();
      expect(f.getByLabelText(label)).toHaveAccessibleDescription("Câmpul este obligatoriu.");
    }
    for (const label of ["Scadență", "Cantitate", "TVA"]) expect(f.getByLabelText(label)).toBeValid(); // given already
    await choose(f.getByLabelText("Client"), "c1");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(f.getByLabelText("Produs sau serviciu")).toHaveFocus()); // the next one still missing
    expect(f.getByLabelText("Client")).toBeValid();
  });

  it("without any client, the invoice's own press checks the new client's form first and goes to what it lacks", async () => {
    const { api, f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), clients: [] }) });
    expect(f.queryByRole("button", { name: "Salvează clientul" })).toBeNull();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    expect(api.posts()).toHaveLength(0);
    await waitFor(() => expect(f.getByLabelText("CUI")).toHaveFocus());
    expect(f.getByLabelText("CUI")).toHaveAccessibleDescription("Completează CUI-ul, codul de TVA sau codul fiscal local ori bifează „Persoană fizică”.");
  });

  it("the preview names the client picked, and no client while a new one is being added", async () => {
    const inv = invoicingData();
    const { f } = await openInvoice("1", { invoicing: () => json(inv) });
    await choose(f.getByLabelText("Client"), inv.clients[0].id);
    const paper = document.querySelector(".paper") as HTMLElement;
    expect(paper).toHaveTextContent(inv.clients[0].name);
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    expect(paper).not.toHaveTextContent(inv.clients[0].name);
    expect(paper).toHaveTextContent("Client nou");
  });

  it("a client's missing identity is said at its CUI, and the save goes to the engine once it is given", async () => {
    const inv = invoicingData();
    inv.required = { seller: [], seller_if_ro: [], seller_if_vat_payer: [], client: { RO: ["name", "address.street"], other: ["name"] },
      client_identity_any_of: ["cui", "vat_id", "tax_id", "private:true"] };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat clientul.", "c9") });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    const save = f.getByRole("button", { name: "Salvează ciorna" });
    expect(save).toBeEnabled();
    expect(f.getByLabelText("Nume")).toBeRequired();
    expect(f.getByLabelText("E-mail (opțional)")).not.toBeRequired();
    await userEvent.click(save);
    expect(api.posts()).toHaveLength(0);
    await waitFor(() => expect(f.getByLabelText("CUI")).toHaveFocus());
    expect(f.getByLabelText("CUI")).toHaveAccessibleDescription("Completează CUI-ul, codul de TVA sau codul fiscal local ori bifează „Persoană fizică”.");
    expect(f.getByLabelText("Nume")).toHaveAccessibleDescription("Câmpul este obligatoriu.");
    expect(f.getByLabelText("Adresă")).toBeInvalid();
    await userEvent.click(f.getByLabelText("Persoană fizică"));
    await userEvent.type(f.getByLabelText("Nume"), "Ion Exemplu");
    await userEvent.type(f.getByLabelText("Adresă"), "Str. C 3");
    await userEvent.click(save);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toContain("api/invoicing/client-save");
  });

  /** The engine's `required` lists, as its workspace returns them (docs/openapi.json). */
  const REQUIRED = (): NonNullable<Invoicing["required"]> => ({
    seller: ["legal_name", "cui", "vat_payer", "address.street", "address.city", "address.country", "iban", "bank"],
    seller_if_ro: ["address.county"], seller_if_vat_payer: [],
    client: { RO: ["name", "address.street", "address.city", "country", "address.county"], other: ["name", "address.street", "address.city", "country"] },
    client_if_vat_payer: ["vat_id"], client_if_k: ["address.county"], client_if_ro_vat_registered: ["cui", "vat_id"],
    draft_if_advance_payment_date: ["advance_invoice:true"], client_identity_any_of: ["cui", "vat_id", "tax_id", "private:true"] });

  /** Opens the new client's form with the engine's lists and fills in a foreign firm's name and address. */
  async function foreignClient(inv: Invoicing) {
    const api = renderApp({ invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat clientul.", "c9") });
    const f = await form();
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    const country = f.getByLabelText("Țara");
    expect((await choicesOf(country)).slice(0, 2).map((o) => [o.value, o.text])).toEqual([["RO", "România"], ["AF", "Afganistan"]]);
    await choose(country, "CH");
    expect(country).toHaveTextContent("Elveția");
    for (const [label, v] of [["Nume", "Kunde AG"], ["Adresă", "Beispielstrasse 1"], ["Localitate", "Zürich"]]) {
      await userEvent.type(f.getByLabelText(label), v);
    }
    return { api, f };
  }

  it("requires none of a client's codes alone: a foreign firm with only its local tax code is saved", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const inv = { ...invoicingData(), required: REQUIRED() };
    const { api, f } = await foreignClient(inv);
    // The engine takes any one of CUI, VAT code or local tax code (client_identity_any_of): none of them is required alone.
    for (const label of ["CUI", "Cod de TVA (de exemplu DE123456789)", "Cod fiscal local (de exemplu CHE-123.456.789) (opțional)"]) {
      expect(f.getByLabelText(label)).not.toBeRequired();
    }
    for (const label of ["Nume", "Adresă", "Localitate", "Țara"]) expect(f.getByLabelText(label)).toBeRequired();
    expect(f.getByLabelText("Regiune (opțional)")).not.toBeRequired(); // only an intra-community supply needs it, at issue
    await userEvent.type(f.getByLabelText("Cod fiscal local (de exemplu CHE-123.456.789) (opțional)"), "CHE-123.456.789");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0].body as { client: object }).client).toMatchObject({ country: "CH", tax_id: "CHE-123.456.789", cui: "", vat_id: "" });
  });

  it("leaves what a VAT registration requires to the engine: a German firm registered in Romania with only its VAT code is sent", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const inv = { ...invoicingData(), required: REQUIRED() };
    const { api, f } = await foreignClient(inv);
    const country = f.getByLabelText("Țara");
    await choose(country, "DE");
    await choose(f.getByLabelText("Înregistrare în scopuri de TVA în România"), "registered");
    // The engine asks for a Romanian CUI only with a Romanian VAT code (client_if_ro_vat_registered is its rule, not the form's).
    expect(f.getByLabelText("CUI")).not.toBeRequired();
    expect(f.getByLabelText("Cod de TVA (de exemplu DE123456789)")).not.toBeRequired();
    await userEvent.type(f.getByLabelText("Cod de TVA (de exemplu DE123456789)"), "DE123456789");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "NORD", client: { name: "Kunde AG", cui: "", vat_id: "DE123456789", private: false,
      country: "DE", ro_vat_status: "registered", email: "",
      address: { street: "Beispielstrasse 1", city: "Zürich", county: "", postal_code: "", country: "DE" } } });
  });

  it("a conditional requirement the engine refuses is said as the engine says it, and the form stays open", async () => {
    const REFUSED = "Pentru un client străin înregistrat în România, lipsesc codul TVA RO și CUI-ul românesc corespunzător.";
    const inv = { ...invoicingData(), required: REQUIRED() };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv),
      onPost: () => json({ ok: false, message: REFUSED, invoicing: inv, id: null, state: fixture() }) });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    const country = f.getByLabelText("Țara");
    await choose(country, "DE");
    for (const [label, v] of [["Nume", "Kunde GmbH"], ["Adresă", "Beispielstrasse 1"], ["Localitate", "Berlin"],
      ["Cod de TVA (de exemplu DE123456789)", "RO18547290"]]) await userEvent.type(f.getByLabelText(label), v);
    await choose(f.getByLabelText("Înregistrare în scopuri de TVA în România"), "registered");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(await screen.findByText(REFUSED)).toBeInTheDocument();
    expect(f.getByRole("button", { name: "Salvează ciorna" })).toBeInTheDocument();
  });

  it("a Romanian client's identity never counts a local foreign tax code the form doesn't send", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const inv = { ...invoicingData(), required: REQUIRED() };
    const { api, f } = await foreignClient(inv);
    await userEvent.type(f.getByLabelText("Cod fiscal local (de exemplu CHE-123.456.789) (opțional)"), "CHE-123.456.789");
    const country = f.getByLabelText("Țara");
    await choose(country, "RO");
    await choose(f.getByLabelText("Județ"), "Cluj");
    expect(f.getByLabelText("CUI")).not.toBeRequired(); // a VAT code alone identifies it too
    expect(f.getByLabelText("Cod de TVA (RO…) (opțional)")).not.toBeRequired();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    expect(api.posts()).toHaveLength(0);
    await waitFor(() => expect(f.getByLabelText("CUI")).toHaveFocus());
    expect(f.getByLabelText("CUI")).toHaveAccessibleDescription("Completează CUI-ul, codul de TVA sau codul fiscal local ori bifează „Persoană fizică”.");
  });

  it("never sends a form missing a field the engine requires that has no control on it, and says so", async () => {
    const inv = { ...invoicingData(), required: { ...REQUIRED(), client: { RO: [...REQUIRED().client.RO, "phone"], other: ["name"] } } };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat clientul.", "c9") });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    for (const [label, v] of [["CUI", "18547290"], ["Nume", "Firma Exemplu SRL"], ["Adresă", "Str. B 2"], ["Localitate", "Iași"]]) {
      await userEvent.type(f.getByLabelText(label), v);
    }
    await choose(f.getByLabelText("Județ"), "Iași");
    const save = f.getByRole("button", { name: "Salvează ciorna" });
    await userEvent.click(save);
    expect(sent(api)).toHaveLength(0); // the phone has no field here: nothing incomplete goes to the engine
    const said = "Lipsesc date obligatorii care nu apar în formular.";
    expect(f.getByRole("alert")).toHaveTextContent(said);
  });

  it("the company's details: a field the engine requires with no control on the form blocks the save and is said", async () => {
    const inv = { ...invoicingData(), seller: null, series: [], required: { ...REQUIRED(), seller: [...REQUIRED().seller, "phone"] } };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv) });
    for (const [label, v] of [["CUI", "11111111"], ["Adresă", "Str. Exemplului 1"], ["Localitate", "Cluj-Napoca"]]) {
      await userEvent.type(f.getByLabelText(label), v);
    }
    await choose(f.getByLabelText("Județ"), "Cluj");
    await choose(f.getByLabelText("Contul pe factură"), "RO00TEST0000000000000001");
    await userEvent.type(f.getByLabelText("Banca"), "Banca Exemplu");
    await userEvent.click(f.getByLabelText("Neplătitor de TVA"));
    const save = f.getByRole("button", { name: /Salvează datele firmei|Continuă/ });
    await userEvent.click(save);
    expect(api.posts()).toHaveLength(0);
    expect(save).toHaveAccessibleDescription("Lipsesc date obligatorii care nu apar în formular.");
  });

  it("the invoice's account is picked from the company's accounts, and a known bank fills its name", async () => {
    const { api, f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), seller: null, series: [] }),
      onPost: () => answer({ ...invoicingData(), series: [] }, "Am salvat datele firmei.") });
    const pick = f.getByLabelText("Contul pe factură") as HTMLSelectElement;
    expect((await choicesOf(pick)).map((o) => o.text)).toEqual([
      "Alege contul", "RON, Altă bancă, cont 0001", "EUR, Banca Transilvania, cont 0001"]);
    await choose(pick, "RO00BTRLEURCRT0000000001");
    expect(f.queryByLabelText("Banca")).toBeNull(); // the catalogue names it from the IBAN: nothing to ask
    for (const [label, v] of [["CUI", "11111111"], ["Adresă", "Str. A 1"], ["Localitate", "Arad"]]) {
      await userEvent.type(f.getByLabelText(label), v);
    }
    await choose(f.getByLabelText("Județ"), "Arad");
    await userEvent.click(f.getByLabelText("Neplătitor de TVA"));
    await userEvent.click(f.getByRole("button", { name: /Salvează datele firmei|Continuă/ }));
    const seller = (api.posts()[0].body as { seller: { iban: string; bank: string } }).seller;
    expect([seller.iban, seller.bank]).toEqual(["RO00BTRLEURCRT0000000001", "Banca Transilvania"]);
  });

  it("adds a missing bank account inline and returns to invoice setup", async () => {
    let inv = { ...invoicingData(), seller: null, series: [], accounts: [] } as Invoicing;
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => {
      inv = invoicingData();
      return json({ ok: true, message: "", state: fixture() });
    } });
    await userEvent.type(f.getByLabelText("IBAN"), "RO00TEST0000000000000001");
    await userEvent.click(f.getByRole("button", { name: "Salvează contul" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/company-bank", method: "POST",
      body: { key: "NORD", iban: "RO00TEST0000000000000001" } });
    await waitFor(() => expect(f.getByLabelText("Client")).toBeInTheDocument());
  });

  it("a firm lacking only its account opens the form, which saves the IBAN as the firm's account first", async () => {
    const { ready } = lacksAccount();
    let inv = lacksAccount().inv;
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: (c: Call) => {
      if (!c.url.endsWith("company-bank")) return answer(inv, "Gata.", "d9");
      inv = ready;
      return json({ ok: true, message: "Contul firmei a fost salvat.", state: fixture() });
    } });
    expect(f.getByLabelText("Client")).toHaveValue("c1"); // the firm's only client
    expect([f.getByLabelText("Serie"), f.getByLabelText("Primul număr")].map((x) => (x as HTMLInputElement).value)).toEqual(["AN", "1"]);
    expect(f.queryByRole("button", { name: "Modifică datele firmei de pe factură" })).toBeNull();
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(f.getByLabelText("IBAN-ul firmei")).toHaveFocus());
    expect(api.posts()).toHaveLength(0);
    await userEvent.type(f.getByLabelText("IBAN-ul firmei"), "ro00 test 0000 0000 0000 0001");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(3));
    expect(api.posts().slice(0, 2)).toEqual([
      { url: "/web/api/company-bank", method: "POST", body: { key: "NORD", iban: "RO00TEST0000000000000001" } },
      { url: "/web/api/invoicing/series-create", method: "POST", body: { code: "AN", start: 1, co: "NORD" } }]);
    expect(api.posts()[2]).toMatchObject({ url: "/web/api/invoicing/draft-save", body: { series: "AN", client_id: "c1" } });
  });

  it("an account whose bank fiscalm doesn't know asks for the bank's name in the form, keeping the invoice", async () => {
    const { known, ready } = lacksAccount();
    const account = { iban: "RO00TEST0000000000000001", currency: "RON", bank: "", bank_name: "", pays: true };
    let inv = lacksAccount().inv;
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: (c: Call) => {
      if (c.url.endsWith("company-bank")) inv = { ...inv, accounts: [account] }; // saved, its bank unnamed
      else if (c.url.endsWith("seller-save")) inv = ready;
      return c.url.includes("/invoicing/") ? answer(inv, "Gata.", "d9") : json({ ok: true, message: "Gata.", state: fixture() });
    } });
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.type(f.getByLabelText("IBAN-ul firmei"), account.iban);
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(f.getByLabelText("Banca")).toHaveFocus());
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("Consultanță");
    await userEvent.type(f.getByLabelText("Banca"), "Banca Exemplu");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(4));
    expect(api.posts()[1]).toEqual({ url: "/web/api/invoicing/seller-save", method: "POST",
      body: { seller: { ...known, iban: account.iban, bank: "Banca Exemplu" }, co: "NORD" } });
    expect(api.posts().slice(2).map((p) => p.url.split("/").pop())).toEqual(["series-create", "draft-save"]);
  });

  it("autofills the company's own details, never another party's", async () => {
    const { f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), seller: null, series: [] }) });
    await userEvent.click(f.getByRole("button", { name: "Modifică datele firmei" }));
    const tokens: [string, string][] = [["Denumire", "organization"], ["Adresă", "street-address"], ["Localitate", "address-level2"],
["Cod poștal (opțional)", "postal-code"], ["E-mail (opțional)", "email"],
      ["CUI", "off"]];
    for (const [label, token] of tokens) expect(f.getByLabelText(label)).toHaveAttribute("autocomplete", token);
  });

  it("the client's form keeps autofill off: the browser would fill in the user's own details", async () => {
    const { f } = await openInvoice("1");
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    for (const label of ["Nume", "Adresă", "Localitate", "E-mail (opțional)"]) expect(f.getByLabelText(label)).toHaveAttribute("autocomplete", "off");
  });

  it("uses the engine workspace limit for the line name", async () => {
    const { f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), line_description_max: 37 }) });
    const description = f.getByLabelText("Produs sau serviciu");
    expect(description).toHaveAttribute("maxlength", "37");
    await userEvent.type(description, "x".repeat(38));
    expect(description).toHaveValue("x".repeat(37));
  });

  it("saves a draft with exactly what was entered and shows the totals the engine computed", async () => {
    const after = invoicingData();
    after.drafts.push({ ...after.drafts[0], id: "d2", lines: [], totals: { net: "100.00", vat: "0.00", total: "100.00",
      by_vat: [{ category: "AE", rate: "0", base: "100.00", vat: "0.00" }], total_ron: null } });
    const { api, f } = await openInvoice("1", { onPost: () => answer(after, "Am salvat ciorna.", "d2") });
    expect(screen.getByLabelText("Totaluri")).toHaveTextContent("Total0,00 lei");
    await choose(f.getByRole("combobox", { name: "Client" }), "c1");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.clear(f.getByLabelText("Cantitate"));
    await userEvent.type(f.getByLabelText("Cantitate"), "2");
    await userEvent.type(f.getByLabelText("Preț unitar"), "50.00");
    expect(screen.getByLabelText("Totaluri")).toHaveTextContent("TVA 21%21,00 lei"); // live, before any save
    expect(screen.getByLabelText("Totaluri")).toHaveTextContent("Total121,00 lei");
    await choose(f.getByLabelText("TVA"), "AE");
    await settled(api, () => userEvent.click(f.getByRole("button", { name: "Salvează ciorna" })));
    expect(api.posts()[0].url).toContain("api/invoicing/draft-save");
    expect(api.posts()[0].body).toEqual({ co: "NORD", series: "NRD", client_id: "c1", issue_date: "2026-10-27",
      due_date: "2026-11-26", currency: "RON", notes: "", advance_invoice: false, advance_payment_date: null, iban: "RO00TEST0000000000000001", 
      lines: [{ description: "Consultanță", quantity: "2", unit: "H87", unit_price: "50.00", vat_category: "AE", vat_rate: "0",
        exemption: "VATEX-EU-AE" }] });
    const totals = screen.getByLabelText("Totaluri");
    expect(totals).toHaveTextContent("Total fără TVA100,00 lei");
    expect(totals).toHaveTextContent("Taxare inversă0,00 lei"); // the engine's label, not a code
    expect(totals).toHaveTextContent("Total100,00 lei");
    expect(window.location.search).toBe("?co=NORD&tab=vanzari&new=1"); // saving keeps the form open
  });

  it("proposes the usual due date and the client's latest lines until they are changed, and sends them as shown", async () => {
    const inv = invoicingData();
    inv.due_days = 14;
    const monthly = { description: "Abonament lunar", quantity: "1", unit: "H87", unit_price: "500.00", vat_category: "S",
      vat_rate: "21", exemption: "" };
    inv.clients = [{ ...inv.clients[0], last_invoice: { due_days: 10, currency: "EUR", lines: [monthly] } },
      { ...inv.clients[0], id: "c2", name: "Client Beta SRL", cui: "5002", last_invoice: null }];
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", "d9") });
    const due = f.getByLabelText("Scadență");
    expect(due).toHaveValue("2026-11-10"); // 27 Oct + the company's 14 days
    await choose(f.getByLabelText("Client"), "c1");
    expect(due).toHaveValue("2026-11-06");
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("Abonament lunar");
    expect(f.getByLabelText("Preț unitar")).toHaveValue("500.00");
    expect(f.getByLabelText("Monedă")).toHaveValue("EUR"); // the prices' currency comes with them
    await choose(f.getByLabelText("Client"), "c2");
    expect(due).toHaveValue("2026-11-10");
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("");
    expect(f.getByLabelText("Monedă")).toHaveValue("RON");
    await choose(f.getByLabelText("Client"), "c1");
    await userEvent.clear(due);
    await userEvent.type(due, "2026-12-01");
    await userEvent.clear(f.getByLabelText("Preț unitar"));
    await userEvent.type(f.getByLabelText("Preț unitar"), "450");
    await choose(f.getByLabelText("Client"), "c2"); // what the user changed stays
    expect(due).toHaveValue("2026-12-01");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    expect(api.posts()[0].body).toEqual({ co: "NORD", series: "NRD", client_id: "c2", issue_date: "2026-10-27",
      due_date: "2026-12-01", currency: "EUR", notes: "", advance_invoice: false, advance_payment_date: null, iban: "RO00BTRLEURCRT0000000001", 
      lines: [{ ...monthly, unit_price: "450" }] });
  });

  it("a client's earlier currency is shown as it is sent, and changing it keeps the lines from then on", async () => {
    const inv = invoicingData();
    const line = { description: "Licență", quantity: "1", unit: "H87", unit_price: "90.00", vat_category: "S", vat_rate: "21", exemption: "" };
    inv.clients = [{ ...inv.clients[0], last_invoice: { due_days: 30, currency: "USD", lines: [line] } },
      { ...inv.clients[0], id: "c2", name: "Client Beta SRL", cui: "5002", last_invoice: null }];
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", "d9") });
    await choose(f.getByLabelText("Client"), "c1");
    expect(f.getByLabelText("Monedă")).toHaveTextContent("USD");
    await choose(f.getByLabelText("Monedă"), "EUR");
    await choose(f.getByLabelText("Client"), "c2");
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("Licență");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    expect(api.posts()[0].body).toMatchObject({ client_id: "c2", currency: "EUR", lines: [line] });
  });

  it("issues after saying what it means, then opens the invoice", async () => {
    const { api, f } = await openInvoice("d1", { onPost: (c: Call) => (c.url.includes("issue") ? answer(invoicingData(), "Am emis factura NRD 45.", "5002")
      : answer(invoicingData(), "Am salvat ciorna.", "d1")) });
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("Servicii de dezvoltare");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    expect(await decide()).toBe("Emiți factura în seria NRD către Client Alfa SRL, 2.420,00 lei? Se trimite automat în SPV peste 4 zile.");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["issue"]); // the saved draft, unchanged: no second save
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "d1", version: 1 });
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=vanzari&iss=5002"));
  });

  it("saves changes first when issuing an edited draft, and stops if the save is refused", async () => {
    const { api, f } = await openInvoice("d1", { onPost: () => json({ ok: false, message: "Scadența e înainte de data emiterii.", invoicing: invoicingData(),
      id: null, state: fixture() }) });
    await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "Proiect X");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["draft-save"]); // refused: nothing issued
    expect(await screen.findByText("Scadența e înainte de data emiterii.")).toBeInTheDocument();
  });

  it.each([[2, 2], [3, 2], [8, 7]])("uses the written version from the response (workspace %s, written %s)", async (refreshedVersion, writtenVersion) => {
    const newer = invoicingData();
    newer.drafts[0].version = refreshedVersion;
    const { api, f } = await openInvoice("d1", { onPost: () => answer(newer, "Ciorna a fost salvată.", "d1", { version: writtenVersion }) });
    await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "Local");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    await decide();
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts()[0].body).toMatchObject({ id: "d1", version: 1 });
    expect(api.posts()[1].body).toEqual({ co: "NORD", id: "d1", version: writtenVersion });
  });

  describe("a draft changed elsewhere", () => {
    const STALE = "Ciorna a fost modificată. Trebuie reîncărcată înainte de a continua.";
    /** d1 as another session left it: version 2, with its own note. */
    const elsewhere = () => {
      const d = invoicingData();
      d.drafts[0] = { ...d.drafts[0], version: 2, notes: "Din altă sesiune" };
      return d;
    };
    /** The engine refusing a stale version, as invoicing_api answers it. */
    const refusal = (inv: Invoicing) => json({ ok: false, message: STALE, code: "stale_draft", invoicing: inv, id: "d1", state: fixture() });
    /** The fake engine: GET api/invoicing reads `now.inv`; the test switches it when "another session" saves. */
    const workspace = () => {
      const now = { inv: invoicingData() };
      return { now, invoicing: () => json(now.inv) };
    };
    const d1Body = (version: number, notes: string) => ({ co: "NORD", id: "d1", version, series: "NRD", client_id: "c1",
      issue_date: "2026-10-27", due_date: "2026-11-26", currency: "RON", notes, advance_invoice: false, advance_payment_date: null, iban: "RO00TEST0000000000000001", 
      lines: [{ description: "Servicii de dezvoltare", quantity: "10", unit: "HUR", unit_price: "200.00", vat_category: "S",
        vat_rate: "21", exemption: "" }] });

    it("refuses the save, stops further saves, and reloads the draft without losing what the user typed", async () => {
      const { now, invoicing } = workspace();
      const { api, f } = await openInvoice("d1", { invoicing, onPost: (c) => {
        if (c.body?.version === 1) {
          now.inv = elsewhere();
          return refusal(now.inv);
        }
        return answer(now.inv, "Ciorna a fost salvată.", "d1", { version: 3 });
      } });
      await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "Local");
      await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
      const alert = await waitFor(() => {
        const el = f.getAllByRole("alert").find((x) => x.textContent?.includes(STALE));
        expect(el).toBeDefined();
        return el as HTMLElement;
      });
      expect(api.posts().map((p) => p.body)).toEqual([d1Body(1, "Local")]);
      // every retry would be refused: the form waits for the reload instead
      expect(f.getByRole("button", { name: "Salvează ciorna" })).toBeDisabled();
      expect(f.getByRole("button", { name: "Emite factura" })).toBeDisabled();
      expect(f.getByLabelText("Mențiuni (opțional)")).toHaveValue("Local");

      await userEvent.click(within(alert).getByRole("button", { name: "Reîncarcă ciorna" }));
      expect(f.getByLabelText("Mențiuni (opțional)")).toHaveValue("Din altă sesiune");
      expect(f.getByText("Ciorna salvată a fost reîncărcată. Varianta din formular diferă la: mențiuni.")).toBeInTheDocument();
      expect(f.queryByText(STALE)).toBeNull();
      await userEvent.click(f.getByText("Vezi varianta din formular"));
      expect(f.getByText("Mențiuni").nextSibling).toHaveTextContent("Local");

      // the user's text goes back on top of version 2, and saving it says so
      await userEvent.click(f.getByRole("button", { name: "Folosește varianta din formular" }));
      expect(f.getByLabelText("Mențiuni (opțional)")).toHaveValue("Local");
      await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
      await waitFor(() => expect(api.posts()).toHaveLength(2));
      expect(api.posts()[1].body).toEqual(d1Body(2, "Local"));
      expect(await screen.findByText("Ciorna a fost salvată.")).toBeInTheDocument();
    });

    it("keeps the user's version on view, but no longer offers to put it back, once they edit the reloaded draft", async () => {
      const { now, invoicing } = workspace();
      const { f } = await openInvoice("d1", { invoicing, onPost: () => {
        now.inv = elsewhere();
        return refusal(now.inv);
      } });
      await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "Local");
      await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
      await userEvent.click(await f.findByRole("button", { name: "Reîncarcă ciorna" }));
      await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), ".");
      expect(f.queryByRole("button", { name: "Folosește varianta din formular" })).toBeNull();
      expect(f.getByText("Vezi varianta din formular")).toBeInTheDocument();
    });

    it("reads the draft once however often reload is pressed, and keeps what the user types while it reads", async () => {
      const { now } = workspace();
      let held: ReturnType<typeof deferred<void>> | null = null; // set after the refusal: the reload's read waits for it
      const { api, f } = await openInvoice("d1", { invoicing: () => (held ? held.promise.then(() => json(now.inv)) : json(now.inv)), onPost: () => {
        now.inv = elsewhere();
        return refusal(now.inv);
      } });
      await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "Local");
      await settled(api, () => userEvent.click(f.getByRole("button", { name: "Salvează ciorna" })));
      const reload = await f.findByRole("button", { name: "Reîncarcă ciorna" });
      const reads = () => api.calls.filter((c) => c.method === "GET" && c.url.includes("api/invoicing?")).length;
      const before = reads();
      const read = deferred<void>();
      held = read;
      await userEvent.click(reload);
      expect(reload).toBeDisabled();
      await userEvent.click(reload);
      await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), " și mai mult");
      read.resolve();
      expect(await f.findByText("Ciorna salvată a fost reîncărcată. Varianta din formular diferă la: mențiuni.")).toBeInTheDocument();
      expect(reads()).toBe(before + 1);
      expect(f.getByLabelText("Mențiuni (opțional)")).toHaveValue("Din altă sesiune");
      await userEvent.click(f.getByText("Vezi varianta din formular"));
      expect(f.getByText("Mențiuni").nextSibling).toHaveTextContent("Local și mai mult");
      await userEvent.click(f.getByRole("button", { name: "Folosește varianta din formular" }));
      expect(f.getByLabelText("Mențiuni (opțional)")).toHaveValue("Local și mai mult");
    });

    it("asks before closing while the user's version is only kept aside", async () => {
      at("?co=NORD&tab=vanzari&new=d1");
      const { now, invoicing } = workspace();
      server({ invoicing, onPost: () => {
        now.inv = elsewhere();
        return refusal(now.inv);
      } });
      render(<App />);
      const f = await form();
      await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "Local");
      await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
      await userEvent.click(await f.findByRole("button", { name: "Reîncarcă ciorna" }));
      expect(await f.findByText("Ciorna salvată a fost reîncărcată. Varianta din formular diferă la: mențiuni.")).toBeInTheDocument();
      await userEvent.click(f.getByRole("button", { name: "Închide" }));
      expect(await decide(false)).toBe(CLOSE_UNSAVED);
      expect(window.location.search).toContain("new=d1"); // declined: still open, the version still there
      await userEvent.keyboard("{Escape}");
      expect(await decide(false)).toBe(CLOSE_UNSAVED);
      expect(window.location.search).toContain("new=d1");
      expect(f.getByRole("button", { name: "Folosește varianta din formular" })).toBeInTheDocument();
      await userEvent.keyboard("{Escape}");
      expect(await decide()).toBe(CLOSE_UNSAVED);
      await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=vanzari"));
    });

    it("closes without asking after a reload when nothing the user typed was set aside", async () => {
      at("?co=NORD&tab=vanzari&new=d1");
      const { now, invoicing } = workspace();
      server({ invoicing, onPost: () => {
        now.inv = elsewhere();
        return refusal(now.inv);
      } });
      render(<App />);
      const f = await form();
      await userEvent.click(f.getByRole("button", { name: "Șterge ciorna" }));
      expect(await decide()).toBe("Ștergi ciorna?");
      await userEvent.click(await f.findByRole("button", { name: "Reîncarcă ciorna" }));
      expect(await f.findByText("Ciorna salvată a fost reîncărcată. S-au modificat: mențiuni.")).toBeInTheDocument();
      await userEvent.click(f.getByRole("button", { name: "Închide" }));
      await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=vanzari"));
    });

    it("refuses issuing a draft changed since it was opened, and issues the reloaded version after the user checks it", async () => {
      const { now, invoicing } = workspace();
      const { api, f } = await openInvoice("d1", { invoicing, onPost: (c) => {
        if (c.body?.version === 1) {
          now.inv = elsewhere();
          return refusal(now.inv);
        }
        return json({ ok: true, message: "Factura NRD 45 a fost emisă.", invoicing: now.inv, id: "5002", state: fixture() });
      } });
      await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
      await decide();
      await userEvent.click(await f.findByRole("button", { name: "Reîncarcă ciorna" }));
      // nothing typed here: only what changed is said, there is no "your version" to keep
      expect(f.getByText("Ciorna salvată a fost reîncărcată. S-au modificat: mențiuni.")).toBeInTheDocument();
      expect(f.queryByText("Vezi varianta din formular")).toBeNull();
      await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
      await decide();
      await waitFor(() => expect(api.posts()).toHaveLength(2));
      expect(api.posts().map((p) => p.body)).toEqual([{ co: "NORD", id: "d1", version: 1 }, { co: "NORD", id: "d1", version: 2 }]);
    });

    it("says so when the draft is gone, and keeps the user's text in the form", async () => {
      const { now, invoicing } = workspace();
      const { f } = await openInvoice("d1", { invoicing, onPost: () => {
        now.inv = { ...invoicingData(), drafts: [] };
        return refusal(elsewhere());
      } });
      await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "Local");
      await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
      await userEvent.click(await f.findByRole("button", { name: "Reîncarcă ciorna" }));
      expect(await f.findByText("Ciorna nu mai există: a fost ștearsă sau emisă.")).toBeInTheDocument();
      expect(f.getByLabelText("Mențiuni (opțional)")).toHaveValue("Local");
      expect(f.getByRole("button", { name: "Salvează ciorna" })).toBeDisabled();
    });

    it("says so when the draft can't be read again, and changes nothing", async () => {
      let down = false; // the engine stops answering reads after the refusal
      const { f } = await openInvoice("d1", { invoicing: () => (down ? json({}, 500) : json(invoicingData())), onPost: () => {
        down = true;
        return refusal(elsewhere());
      } });
      await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "Local");
      await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
      await userEvent.click(await f.findByRole("button", { name: "Reîncarcă ciorna" }));
      expect(await f.findByText("Ciorna nu a putut fi reîncărcată. Încearcă din nou.")).toBeInTheDocument();
      expect(f.getByLabelText("Mențiuni (opțional)")).toHaveValue("Local");
      expect(f.getByRole("button", { name: "Reîncarcă ciorna" })).toBeEnabled();
    });

    it("deletes only the version on screen", async () => {
        const { api, f } = await openInvoice("d1", { onPost: () => refusal(elsewhere()) });
      await userEvent.click(f.getByRole("button", { name: "Șterge ciorna" }));
      await decide();
      await waitFor(() => expect(api.posts()).toHaveLength(1));
      expect(api.posts()[0]).toMatchObject({ url: expect.stringMatching(/invoicing\/draft-delete$/), body: { co: "NORD", id: "d1", version: 1 } });
      expect(await f.findByRole("button", { name: "Reîncarcă ciorna" })).toBeInTheDocument();
    });
  });

  it("asks nothing and issues nothing when the confirm is declined", async () => {
    const { api, f } = await openInvoice("d1");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    await decide(false);
    expect(api.posts()).toHaveLength(0);
  });

  it("adds a client from ANAF's data and selects it", async () => {
    const saved = invoicingData();
    saved.clients.push({ ...saved.clients[0], id: "c2", name: "Client Nou SRL", cui: "7777" });
    let added = false; // like the engine, reads after the save list the new client too
    const { api, f } = await openInvoice("1", { invoicing: () => json(added ? saved : invoicingData()), onPost: (c: Call) => {
      if (c.url.includes("client-lookup")) {
        return answer(invoicingData(), "", null, { client: { name: "Client Nou SRL", cui: "7777", address: { street: "Bd. Test 2", city: "Iași" } } });
      }
      added = true;
      return answer(saved, "Am salvat clientul.", "c2");
    } });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("CUI"), "7777");
    await lookUp(f.getByLabelText("CUI"));
    expect(api.posts()[0].body).toEqual({ co: "NORD", cui: "7777" });
    await waitFor(() => expect(f.getByLabelText("Nume")).toHaveValue("Client Nou SRL"));
    expect(f.getByLabelText("Adresă")).toHaveValue("Bd. Test 2");
    await settled(api, () => userEvent.click(f.getByRole("button", { name: "Salvează ciorna" })));
    expect((api.posts()[1].body as { client: { name: string; cui: string } }).client).toMatchObject({ name: "Client Nou SRL", cui: "7777" });
    expect(f.getByRole("combobox", { name: "Client" })).toHaveValue("c2");
  });

  it("a new client is saved by the invoice's own press, once ANAF has filled it in from the CUI just typed", async () => {
    const found = { name: "Client Nou SRL", cui: "18547290", vat_id: "", vat_payer: false, inactive: false,
      address: { street: "", city: "Iași", county: "RO-IS" } }; // what ANAF leaves empty doesn't erase what was typed
    const { api, f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), clients: [] }), onPost: (c: Call) => {
      const op = c.url.split("/").pop();
      return op === "client-lookup" ? answer(invoicingData(), "", null, { client: found })
        : answer(invoicingData(), "Gata.", op === "client-save" ? "c9" : op === "issue" ? "5002" : "d9");
    } });
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.type(f.getByLabelText("Adresă"), "Str. Scrisă 1");
    await userEvent.type(f.getByLabelText("CUI"), "18547290");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" })); // leaving the CUI for the button asks ANAF first
    await decide();
    await waitFor(() => expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["client-lookup", "client-save", "draft-save", "issue"]));
    expect((api.posts()[1].body as { client: object }).client).toMatchObject({ name: "Client Nou SRL", cui: "18547290",
      address: { street: "Str. Scrisă 1", city: "Iași", county: "RO-IS", postal_code: "", country: "RO" } });
    expect(api.posts()[2].body).toMatchObject({ client_id: "c9", lines: [{ description: "Consultanță", unit_price: "100" }] });
  });

  it("keeps what ANAF's lookup found when saving the client, and drops it when the CUI changes", async () => {
    const found = { name: "Client Nou SRL", cui: "7777", reg_com: "J22/7/2021", phone: "0700000000", vat_payer: true,
      efactura_registered: true, inactive: false, address: { street: "Bd. Test 2", city: "Iași" } };
    const { api, f } = await openInvoice("1", { onPost: (c: Call) => (!c.url.includes("client-lookup")
      ? answer(invoicingData(), "Am salvat clientul.", null) : c.body?.cui === "7777" ? answer(invoicingData(), "", null, { client: found })
        : json({ ok: false, message: "ANAF nu a găsit firma.", id: null })) });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("CUI"), "7777");
    await lookUp(f.getByLabelText("CUI"));
    await waitFor(() => expect(f.getByLabelText("Nume")).toHaveValue("Client Nou SRL"));
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect((api.posts()[1].body as { client: object }).client).toMatchObject({ reg_com: "J22/7/2021", phone: "0700000000",
      vat_payer: true, efactura_registered: true });
    await userEvent.type(f.getByLabelText("CUI"), "8"); // another firm now: ANAF's details for 7777 don't go with it
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    const third = (sent(api)[1].body as { client: Record<string, unknown> }).client;
    expect(third.cui).toBe("77778");
    for (const k of ["reg_com", "phone", "vat_payer", "efactura_registered", "inactive"]) expect(third).not.toHaveProperty(k);
  });

  it("an edit made while the new client is saved is not issued", async () => {
    const held = deferred<Response>();
    const { api, f } = await openInvoice("1", { invoicing: () => json({ ...invoicingData(), clients: [] }),
      onPost: (c: Call) => (c.url.endsWith("client-save") ? held.promise : answer(invoicingData(), "Gata.", "d9")) });
    await userEvent.type(f.getByLabelText("Nume"), "Client Nou SRL");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    await userEvent.type(f.getByLabelText("Preț unitar"), "9"); // while the client is on its way
    expect(f.getByLabelText("Nume")).toBeDisabled(); // what goes is what stays: the client waits for its answer
    await act(async () => held.resolve(answer(invoicingData(), "Clientul a fost salvat.", "c9")));
    await waitFor(() => expect(f.getByRole("alert")).toHaveTextContent("Factura nu a fost emisă: s-a modificat în timp ce se salva."));
    expect(sent(api).map((p) => p.url.split("/").pop())).toEqual(["client-save", "draft-save"]);
  });

  it("a new client saved by the invoice keeps the invoice as it is on screen", async () => {
    const inv = invoicingData();
    const line = { description: "Abonament", quantity: "1", unit: "H87", unit_price: "50.00", vat_category: "S", vat_rate: "21", exemption: "" };
    inv.clients = [{ ...inv.clients[0], last_invoice: { due_days: 7, currency: "EUR", lines: [line] } }];
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv),
      onPost: (c: Call) => answer(inv, "Gata.", c.url.endsWith("client-save") ? "c9" : "d9") });
    await choose(f.getByLabelText("Client"), "c1"); // its lines, currency and term come in
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("Nume"), "Client Nou SRL");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    expect(sent(api)[1].body).toMatchObject({ client_id: "c9", currency: "EUR", due_date: "2026-11-03", lines: [line] });
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("Abonament");
  });

  it("issuing with a new client added over a chosen one names the client the draft was saved with", async () => {
    const after = invoicingData();
    after.drafts.push({ ...after.drafts[0], id: "d9", client_id: "c9", client: { name: "Client Nou SRL", cui: "5009" } });
    const { f } = await openInvoice("1", { onPost: (c: Call) => answer(after, "Gata.", c.url.endsWith("client-save") ? "c9" : "d9") });
    await choose(f.getByLabelText("Client"), "c1");
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("Nume"), "Client Nou SRL");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    expect(await decide(false)).toMatch(/^Emiți factura în seria NRD către Client Nou SRL,/);
  });

  it("ANAF's answer about a CUI since changed fills in nothing", async () => {
    const late = deferred<Response>();
    const { f } = await openInvoice("1", { onPost: (c: Call) => (c.url.endsWith("/client-lookup") ? late.promise : answer(invoicingData(), "", null)) });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("CUI"), "7777");
    await lookUp(f.getByLabelText("CUI"));
    await userEvent.type(f.getByLabelText("CUI"), "8");
    await act(async () => late.resolve(answer(invoicingData(), "", null, { client: { name: "Firma 7777 SRL", cui: "7777", address: {} } })));
    expect(f.getByLabelText("CUI")).toHaveValue("77778");
    expect(f.getByLabelText("Nume")).toHaveValue("");
  });

  it("an inactive firm can't be added as a client", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "", null,
      { client: { name: "Firma Inactivă SRL", cui: "6666", inactive: true, address: { street: "Str. A 1", city: "Arad" } } }) });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("CUI"), "6666");
    await lookUp(f.getByLabelText("CUI"));
    expect(await f.findByText("Firma figurează ca inactivă la ANAF și nu poate fi adăugată ca client.")).toBeInTheDocument();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    expect(api.posts()).toHaveLength(1); // only the lookup
  });

  it("a seller who doesn't pay VAT can only issue without it", async () => {
    const inv = invoicingData();
    inv.seller = { ...inv.seller!, vat_payer: false, vat_id: "" };
    inv.vat = { default: "O", choices: [{ key: "O", label: "Neplătitor de TVA (art. 310)", category: "O", rate: "0",
      exemption: "art. 310 Cod fiscal", needs_delivery_date: false }] }; // the engine's choices for this seller
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", "d9") });
    const vat = f.getByLabelText("TVA") as HTMLSelectElement;
    expect((await choicesOf(vat)).map((o) => o.text)).toEqual(["Neplătitor de TVA (art. 310)"]);
    await choose(f.getByRole("combobox", { name: "Client" }), "c1");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    // the first line carries the engine's default for this seller, not a standard rate
    expect((api.posts()[0].body as { lines: { vat_category: string; exemption: string }[] }).lines[0])
      .toMatchObject({ vat_category: "O", vat_rate: "0", exemption: "art. 310 Cod fiscal" });
  });

  it("an intra-community supply asks for the delivery date and sends it", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "Am salvat ciorna.", "d3") });
    await choose(f.getByRole("combobox", { name: "Client" }), "c1");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Piese");
    await userEvent.type(f.getByLabelText("Preț unitar"), "10");
    expect(f.queryByLabelText("Data livrării")).toBeNull();
    await choose(f.getByLabelText("TVA"), "K");
    const save = f.getByRole("button", { name: "Salvează ciorna" });
    await userEvent.click(save); // the date is needed first
    expect(api.posts()).toHaveLength(0);
    const date = f.getByLabelText("Data livrării");
    await waitFor(() => expect(date).toHaveFocus());
    expect(date).toBeInvalid();
    expect(date).toHaveAccessibleDescription("Lipsește data.");
    await userEvent.type(date, "2026-10-20");
    expect(date).toBeValid();
    await userEvent.click(save);
    expect(api.posts()[0].body).toMatchObject({ delivery_date: "2026-10-20",
      lines: [{ vat_category: "K", vat_rate: "0", exemption: "VATEX-EU-IC" }] });
  });

  it("names a VAT group as the engine labels it, without adding to it", async () => {
    at("?co=NORD&tab=vanzari&new=d1");
    const inv = invoicingData();
    renderApp({ invoicing: () => json(inv) });
    const totals = (await form()).getByRole("group", { name: "Totaluri" });
    expect(totals).toHaveTextContent("TVA 21%");
    expect(totals).not.toHaveTextContent("TVA TVA");
  });

  it("an intra-community supply to a client without a region says so before issuing, and only then", async () => {
    at("?co=NORD&tab=vanzari&new=d1");
    const NOTE = "Pentru o livrare intracomunitară, completează și regiunea din adresa clientului. Adaugă din nou clientul, cu regiunea completată.";
    const inv = invoicingData();
    const de = { street: "Musterstraße 1", city: "Berlin", county: "", postal_code: "10115", country: "DE" };
    inv.required = { seller: [], seller_if_ro: [], seller_if_vat_payer: [], client: { RO: ["name"], other: ["name"] },
      client_identity_any_of: ["cui", "vat_id"], client_if_k: ["address.county"] };
    inv.clients = [...inv.clients, { id: "c2", name: "Kunde GmbH", cui: "", vat_id: "DE999999999", private: false, country: "DE",
      address: de, email: "", last_used: "2026-10-20" }];
    inv.drafts[0] = { ...inv.drafts[0], client_id: "c2", delivery_date: "2026-10-20",
      lines: [{ ...inv.drafts[0].lines[0], vat_category: "K", vat_rate: "0", exemption: "VATEX-EU-IC" }] };
    server({ invoicing: () => json(inv) });
    const { unmount } = render(<App />);
    let f = await form();
    expect(f.getByText(NOTE)).toBeInTheDocument();
    expect(f.getByRole("button", { name: "Emite factura" })).toBeDisabled();
    expect(f.getByRole("button", { name: "Emite factura" })).toHaveAccessibleDescription(NOTE);
    await choose(f.getByLabelText("TVA"), "S21"); // not K: nothing to say
    expect(f.queryByText(NOTE)).toBeNull();
    unmount();

    inv.clients[1] = { ...inv.clients[1], address: { ...de, county: "Berlin" } };
    render(<App />);
    f = await form();
    expect(f.queryByText(NOTE)).toBeNull();
    expect(f.getByRole("button", { name: "Emite factura" })).toBeEnabled();
  });

  it("an edit made while saving is not issued", async () => {
    let release: (() => void) | null = null;
    const { api, f } = await openInvoice("d1", { onPost: (c: Call) => (c.url.includes("draft-save")
      ? new Promise((ok) => { release = () => ok(answer(invoicingData(), "Am salvat ciorna.", "d1")); })
      : answer(invoicingData(), "Am emis factura.", "5002")) });
    await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "A");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" })); // saves first: the save is on its way
    await waitFor(() => expect(release).not.toBeNull());
    await userEvent.clear(f.getByLabelText("Preț unitar")); // an edit while saving
    await userEvent.type(f.getByLabelText("Preț unitar"), "999");
    release!();
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 50));
    expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["draft-save"]); // not issued
    expect(f.getByRole("alert")).toHaveTextContent("Factura nu a fost emisă: s-a modificat în timp ce se salva.");
  });

  it("a foreign client's country goes in one field and in the address", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "Am salvat clientul.", "c9") });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("Nume"), "Kunde GmbH");
    await userEvent.type(f.getByLabelText("Adresă"), "Hauptstr. 1");
    await userEvent.type(f.getByLabelText("Localitate"), "Berlin");
    const country = f.getByLabelText("Țara");
    await choose(country, "DE");
    expect(f.getByLabelText("Regiune (opțional)")).toBeInTheDocument();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    expect((api.posts()[0].body as { client: { country: string; address: { country: string } } }).client)
      .toMatchObject({ country: "DE", address: { country: "DE" } });
  });

  it("a line that matches none of the engine's VAT choices asks for one instead of showing another", async () => {
    const inv = invoicingData();
    inv.drafts[0].lines[0] = { ...inv.drafts[0].lines[0], vat_category: "E", vat_rate: "0", exemption: "Scutit (formulare veche)" };
    const { api, f } = await openInvoice("d1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", "d1") });
    expect(f.getByLabelText("TVA")).toHaveValue("");
    expect(f.getByLabelText("TVA")).toHaveTextContent("Alege TVA-ul"); // its empty choice, shown on the trigger
    await userEvent.click(f.getByRole("button", { name: "Emite factura" })); // not issued with a VAT the app would guess
    expect(api.posts()).toHaveLength(0);
    await waitFor(() => expect(f.getByLabelText("TVA")).toHaveFocus());
    expect(f.getByLabelText("TVA")).toHaveAccessibleDescription("Lipsește TVA-ul.");
    await choose(f.getByLabelText("TVA"), "S21");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0].body as { lines: object[] }).lines[0]).toMatchObject({ vat_category: "S", vat_rate: "21", exemption: "" });
  });

  it("sends back only the line fields the engine takes", async () => {
    const inv = invoicingData();
    (inv.drafts[0].lines[0] as Record<string, unknown>).net = "2000.00"; // a field the engine may add to what it returns
    const { api, f } = await openInvoice("d1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", "d1") });
    await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "x");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(Object.keys((api.posts()[0].body as { lines: object[] }).lines[0]).sort())
      .toEqual(["description", "exemption", "quantity", "unit", "unit_price", "vat_category", "vat_rate"]);
  });

  it("a company that doesn't pay VAT says so, and the engine's required fields decide when it can be saved", async () => {
    const inv: Invoicing = { ...invoicingData(), seller: null, series: [], required: {
      seller: ["legal_name", "cui", "vat_payer", "address.street", "address.city", "address.country", "iban", "bank"],
      seller_if_ro: ["address.county"], seller_if_vat_payer: [],
      client: { RO: ["name"], other: ["name"] }, client_identity_any_of: ["cui", "vat_id", "private:true"] } };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer({ ...invoicingData(), series: [] }, "Am salvat datele firmei.") });
    const save = f.getByRole("button", { name: /Salvează datele firmei|Continuă/ });
    for (const [label, v] of [["CUI", "11111111"], ["Adresă", "Str. A 1"], ["Localitate", "Arad"]]) {
      await userEvent.type(f.getByLabelText(label), v);
    }
    await choose(f.getByLabelText("Contul pe factură"), "RO00TEST0000000000000001");
    await userEvent.type(f.getByLabelText("Banca"), "B");
    await userEvent.click(f.getByLabelText("Plătitor de TVA"));
    await userEvent.click(save); // a Romanian address needs its county (the engine's `required`)
    expect(api.posts()).toHaveLength(0);
    const county = f.getByLabelText("Județ");
    await waitFor(() => expect(county).toHaveFocus());
    expect(county).toBeInvalid();
    expect(county).toHaveAccessibleDescription("Câmpul este obligatoriu.");
    expect(f.getByLabelText("Banca")).toBeValid();
    await choose(county, "Arad");
    expect(county).toBeValid(); // filled in: no longer said
    expect(county).not.toHaveAttribute("aria-describedby");
    await userEvent.click(f.getByLabelText("Neplătitor de TVA"));
    await userEvent.click(save);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0].body as { seller: { vat_payer: boolean; vat_id: string } }).seller).toMatchObject({ vat_payer: false, vat_id: "" });
  });

  it("the company's details can be changed later, from the form", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "Am salvat datele firmei.") });
    await userEvent.click(f.getByRole("button", { name: "Modifică datele firmei de pe factură" }));
    expect(f.getByLabelText("CUI")).toHaveValue("11111111");
    expect(f.getByLabelText("Plătitor de TVA")).toBeChecked();
    await userEvent.type(f.getByLabelText("E-mail (opțional)"), "x");
    await userEvent.click(f.getByRole("button", { name: /Salvează datele firmei|Continuă/ }));
    await waitFor(() => expect(api.posts()[0].url).toContain("seller-save"));
    expect(await f.findByRole("button", { name: "Emite factura" })).toBeInTheDocument(); // back to the invoice
  });

  it("a saved IBAN written with spaces or lower case reopens on its account and is sent as the account is", async () => {
    const inv = invoicingData();
    inv.seller = { ...inv.seller!, iban: " ro00 test 0000 0000 0000 0001 " };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer(invoicingData(), "Am salvat datele firmei.") });
    await userEvent.click(f.getByRole("button", { name: "Modifică datele firmei de pe factură" }));
    expect(f.getByLabelText("Contul pe factură")).toHaveValue("RO00TEST0000000000000001");
    await userEvent.click(f.getByRole("button", { name: /Salvează datele firmei|Continuă/ }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0].body as { seller: { iban: string } }).seller.iban).toBe("RO00TEST0000000000000001");
  });

  it("a saved IBAN that isn't one of the company's accounts is asked again, never sent", async () => {
    const inv = invoicingData();
    inv.seller = { ...inv.seller!, iban: "RO00FAKE0000000000000000" };
    inv.required = REQUIRED(); // the engine's list: the IBAN is required
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv) });
    await userEvent.click(f.getByRole("button", { name: "Modifică datele firmei de pe factură" }));
    const pick = f.getByLabelText("Contul pe factură");
    expect(pick).toHaveValue("");
    await userEvent.click(f.getByRole("button", { name: /Salvează datele firmei|Continuă/ }));
    expect(api.posts()).toHaveLength(0);
    await waitFor(() => expect(pick).toHaveFocus());
    expect(pick).toHaveAccessibleDescription("Lipsește contul.");
  });

  it("without account-management access, Save points to the account screen", async () => {
    const inv = { ...invoicingData(), seller: null, series: [], accounts: [], required: REQUIRED() };
    const state = fixture();
    state.can_manage_companies = false;
    const { api, f } = await openInvoice("1", { state: () => json(state), invoicing: () => json(inv) });
    for (const [label, v] of [["CUI", "11111111"], ["Adresă", "Str. A 1"], ["Localitate", "Arad"]]) {
      await userEvent.type(f.getByLabelText(label), v);
    }
    await choose(f.getByLabelText("Județ"), "Arad");
    await userEvent.click(f.getByLabelText("Neplătitor de TVA"));
    const save = f.getByRole("button", { name: /Salvează datele firmei|Continuă/ });
    await userEvent.click(save);
    expect(api.posts()).toHaveLength(0);
    const link = f.getByRole("link", { name: "Tranzacții" });
    await waitFor(() => expect(link).toHaveFocus());
    expect(link).toHaveAccessibleDescription("Adaugă întâi contul firmei în Tranzacții.");
    expect(save).not.toHaveAccessibleDescription(/Lipsesc date obligatorii/);
  });

  it("a foreign client carries its VAT id; a private person sends none", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "Am salvat clientul.", "c9") });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    const country = f.getByLabelText("Țara");
    await choose(country, "DE");
    await userEvent.type(f.getByLabelText("Cod de TVA (de exemplu DE123456789)"), "de 123 456 789");
    await userEvent.type(f.getByLabelText("Nume"), "Kunde GmbH");
    await userEvent.type(f.getByLabelText("Adresă"), "Hauptstr. 1");
    await userEvent.type(f.getByLabelText("Localitate"), "Berlin");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    expect((sent(api)[0].body as { client: object }).client).toMatchObject({ vat_id: "DE123456789", country: "DE" });
    await waitFor(() => expect(f.getByLabelText("Produs sau serviciu")).toHaveFocus()); // on to what the invoice still lacks
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("CUI"), "123");
    await userEvent.click(f.getByLabelText("Persoană fizică"));
    await userEvent.type(f.getByLabelText("Nume"), "Ion Popescu");
    await userEvent.type(f.getByLabelText("Adresă"), "Str. B 2");
    await userEvent.type(f.getByLabelText("Localitate"), "Iași");
    await choose(f.getByLabelText("Județ"), "Iași");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    expect((sent(api)[1].body as { client: object }).client).toMatchObject({ private: true, cui: "", vat_id: "" });
  });

  it("the issue date is today's: an older draft is saved with it before it is issued", async () => {
    const inv = invoicingData();
    inv.drafts[0].issue_date = "2026-10-20";
    const { api, f } = await openInvoice("d1", { invoicing: () => json(inv), onPost: () => answer(invoicingData(), "Am salvat ciorna.", "d1") });
    expect(f.getByLabelText("Data emiterii")).toHaveTextContent("Azi, 27 octombrie");
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toMatchObject({ url: "/web/api/invoicing/draft-save", body: { id: "d1", issue_date: "2026-10-27" } });
  });

  it("closing with unsaved edits asks first", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    server();
    render(<App />);
    const f = await form();
    const leaving = () => !window.dispatchEvent(new Event("beforeunload", { cancelable: true })); // true: the browser asks
    expect(leaving()).toBe(false);
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Ceva");
    expect(leaving()).toBe(true); // a reload asks too
    await userEvent.click(f.getByRole("button", { name: "Închide" }));
    expect(await decide(false)).toBe(CLOSE_UNSAVED);
    expect(window.location.search).toContain("new=1"); // declined: still open
    await userEvent.keyboard("{Escape}");
    expect(await decide()).toBe(CLOSE_UNSAVED);
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=vanzari"));
  });

  it("a county is picked by name and Bucharest by sector, which stands for the city", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "Am salvat clientul.", "c9") });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.click(f.getByLabelText("Persoană fizică"));
    await userEvent.type(f.getByLabelText("Nume"), "Ion Popescu");
    await userEvent.type(f.getByLabelText("Adresă"), "Str. B 2");
    await userEvent.type(f.getByLabelText("Localitate"), "Otopeni");
    await choose(f.getByLabelText("Județ"), "București, Sectorul 3");
    expect(f.queryByLabelText("Localitate")).toBeNull();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    expect((sent(api)[0].body as { client: { address: object } }).client.address).toMatchObject({ county: "RO-B", city: "SECTOR3" });
  });

  it("adding a line puts the cursor in its description", async () => {
    const { f } = await openInvoice("1");
    await userEvent.click(f.getByRole("button", { name: "Adaugă o linie" }));
    await waitFor(() => expect(document.getElementById("l1-d")).toHaveFocus());
  });

  it("an advance invoice sends the flag and the date the advance came in; unticked, neither", async () => {
    const { api, f } = await openInvoice("d1", { onPost: () => answer(invoicingData(), "Am salvat ciorna.", "d1") });
    await userEvent.click(f.getByLabelText("Factură de avans"));
    await userEvent.type(f.getByLabelText("Data încasării avansului (opțional)"), "2026-10-20");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toMatchObject({ advance_invoice: true, advance_payment_date: "2026-10-20" });
    await userEvent.click(f.getByLabelText("Factură de avans"));
    expect(f.queryByLabelText("Data încasării avansului (opțional)")).toBeNull();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts()[1].body).toMatchObject({ advance_invoice: false, advance_payment_date: null });
  });

  it("shows the VAT in lei, the BNR rate's date and what else the invoice will say, as the engine computed them", async () => {
    const inv = invoicingData();
    inv.drafts[0] = { ...inv.drafts[0], currency: "EUR", exchange_rate: { rate: "4.9753", date: "2026-10-26" },
      totals: { ...inv.drafts[0].totals, total_ron: "12040.23", vat_ron: "2089.63" }, chargeability_date: "2026-10-27",
      mentions: ["TVA la încasare"] };
    const { f } = await openInvoice("d1", { invoicing: () => json(inv) });
    const totals = f.getByRole("group", { name: "Totaluri" });
    expect(totals).toHaveTextContent("TVA în lei2.089,63 lei");
    expect(totals).toHaveTextContent("Total în lei12.040,23 lei");
    expect(totals).toHaveTextContent("Curs BNR din 26 oct4,9753 lei/EUR");
    expect(f.getByText("Pe factură apare și: TVA la încasare.")).toBeInTheDocument();
  });

  it("a VAT payer declares cash-accounting VAT; switching to non-payer takes the box away and clears it", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "Am salvat datele firmei.") });
    await userEvent.click(f.getByRole("button", { name: "Modifică datele firmei de pe factură" }));
    await userEvent.click(f.getByLabelText("TVA la încasare"));
    await userEvent.click(f.getByRole("button", { name: /Salvează datele firmei|Continuă/ }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0].body as { seller: object }).seller).toMatchObject({ vat_payer: true, vat_on_collection: true });
    await userEvent.click(await f.findByRole("button", { name: "Modifică datele firmei de pe factură" }));
    await userEvent.click(f.getByLabelText("TVA la încasare"));
    await userEvent.click(f.getByLabelText("Neplătitor de TVA"));
    expect(f.queryByLabelText("TVA la încasare")).toBeNull();
    await userEvent.click(f.getByRole("button", { name: /Salvează datele firmei|Continuă/ }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect((api.posts()[1].body as { seller: object }).seller).toMatchObject({ vat_payer: false, vat_on_collection: false });
  });

  it("a client outside Romania can carry a local tax code; back to RO or a private person, none is sent", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "Am salvat clientul.", "c9") });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    expect(f.queryByLabelText(/Cod fiscal local/)).toBeNull(); // RO by default
    const country = f.getByLabelText("Țara");
    await choose(country, "CH");
    await userEvent.type(f.getByLabelText(/Cod fiscal local/), "CHE-123.456.789");
    await userEvent.type(f.getByLabelText("Nume"), "Kunde AG");
    await userEvent.type(f.getByLabelText("Adresă"), "Bahnhofstr. 1");
    await userEvent.type(f.getByLabelText("Localitate"), "Zürich");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    expect((sent(api)[0].body as { client: object }).client).toMatchObject({ tax_id: "CHE-123.456.789", country: "CH" });
    // a second client: the code typed for CH, then the country back to RO: the hidden code isn't sent
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    const c2 = f.getByLabelText("Țara");
    await choose(c2, "CH");
    await userEvent.type(f.getByLabelText(/Cod fiscal local/), "CHE-999");
    await choose(c2, "RO");
    await userEvent.type(f.getByLabelText("CUI"), "18547290");
    await userEvent.type(f.getByLabelText("Nume"), "Client RO SRL");
    await userEvent.type(f.getByLabelText("Adresă"), "Str. A 1");
    await userEvent.type(f.getByLabelText("Localitate"), "Arad");
    await choose(f.getByLabelText("Județ"), "Arad");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    expect((sent(api)[1].body as { client: object }).client).toMatchObject({ country: "RO", tax_id: "" });
    // a private person: no codes at all
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    const c3 = f.getByLabelText("Țara");
    await choose(c3, "CH");
    await userEvent.type(f.getByLabelText(/Cod fiscal local/), "CHE-555");
    await userEvent.click(f.getByLabelText("Persoană fizică"));
    await userEvent.type(f.getByLabelText("Nume"), "Hans Muster");
    await userEvent.type(f.getByLabelText("Adresă"), "Gasse 2");
    await userEvent.type(f.getByLabelText("Localitate"), "Bern");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(3));
    expect((sent(api)[2].body as { client: object }).client).toMatchObject({ private: true, cui: "", vat_id: "", tax_id: "" });
  });

  it("a saved advance invoice comes back ticked with its date, and saves unchanged", async () => {
    const inv = invoicingData();
    inv.drafts[0] = { ...inv.drafts[0], advance_invoice: true, advance_payment_date: "2026-10-20" };
    const { api, f } = await openInvoice("d1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", "d1") });
    expect(f.getByLabelText("Factură de avans")).toBeChecked();
    expect(f.getByLabelText("Data încasării avansului (opțional)")).toHaveValue("2026-10-20");
    await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "x");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toMatchObject({ advance_invoice: true, advance_payment_date: "2026-10-20" });
  });

  it("a Romanian VAT payer saved by its RO CUI alone is sent: the engine derives its VAT code", async () => {
    const inv = invoicingData();
    inv.required = { seller: [], seller_if_ro: [], seller_if_vat_payer: [], client: { RO: ["name"], other: ["name"] },
      client_identity_any_of: ["cui", "vat_id", "tax_id", "private:true"], client_if_vat_payer: ["vat_id"] };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: (c: Call) => (c.url.includes("client-lookup")
      ? answer(inv, "", null, { client: { name: "Firma TVA SRL", vat_payer: true, address: { street: "Str. B 2", city: "Iași", county: "RO-IS" } } })
      : answer(inv, "Am salvat clientul.", "c9")) });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await userEvent.type(f.getByLabelText("CUI"), "RO18547290");
    await lookUp(f.getByLabelText("CUI"));
    await waitFor(() => expect(f.getByLabelText("Nume")).toHaveValue("Firma TVA SRL"));
    // The engine takes the VAT code from an RO CUI before it asks a VAT payer for one (client_if_vat_payer is its rule).
    expect(f.getByLabelText("Cod de TVA (RO…)")).toHaveValue("");
    expect(f.getByLabelText("Cod de TVA (RO…)")).not.toBeRequired();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts().filter((p) => p.url.includes("client-save"))).toHaveLength(1));
    expect(api.posts().find((p) => p.url.includes("client-save"))?.body).toEqual({ co: "NORD", client: { name: "Firma TVA SRL",
      cui: "RO18547290", vat_id: "", private: false, country: "RO", vat_payer: true, tax_id: "", ro_vat_status: "unknown", email: "",
      address: { street: "Str. B 2", city: "Iași", county: "RO-IS", postal_code: "", country: "RO" } } });
  });

  it("shows when the VAT becomes chargeable, and hides the engine's mentions while the form has unsaved edits", async () => {
    const inv = invoicingData();
    inv.drafts[0] = { ...inv.drafts[0], currency: "EUR", exchange_rate: { rate: "4.9753", date: "2026-10-17" },
      totals: { ...inv.drafts[0].totals, total_ron: "12040.23", vat_ron: "2089.63" }, chargeability_date: "2026-10-20",
      mentions: ["TVA la încasare."] };
    const { f } = await openInvoice("d1", { invoicing: () => json(inv) });
    expect(f.getByRole("group", { name: "Totaluri" })).toHaveTextContent("TVA exigibil la20 oct");
    expect(f.getByText("Pe factură apare și: TVA la încasare.")).toBeInTheDocument(); // one full stop
    await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "x");
    expect(f.queryByText(/Pe factură apare și/)).toBeNull();
  });

  it("a foreign client says whether it is registered for VAT in Romania: unknown by default; registered needs its Romanian codes", async () => {
    const inv = invoicingData();
    inv.required = { seller: [], seller_if_ro: [], seller_if_vat_payer: [], client: { RO: ["name"], other: ["name"] },
      client_identity_any_of: ["cui", "vat_id", "tax_id", "private:true"], client_if_ro_vat_registered: ["cui", "vat_id"] };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat clientul.", "c9") });
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    expect(f.queryByLabelText("Înregistrare în scopuri de TVA în România")).toBeNull(); // RO: no question
    const country = f.getByLabelText("Țara");
    await choose(country, "DE");
    const status = f.getByLabelText("Înregistrare în scopuri de TVA în România");
    expect(status).toHaveValue("unknown"); // never assumed: unknown is sent to SPV
    await userEvent.type(f.getByLabelText("Cod de TVA (de exemplu DE123456789)"), "DE123456789");
    await userEvent.type(f.getByLabelText("Nume"), "Kunde GmbH");
    await choose(status, "not_registered");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect((api.posts()[0].body as { client: object }).client).toMatchObject({ country: "DE", ro_vat_status: "not_registered" });
  });

  it("a Romanian VAT choice never outlives the client it was made for: Renunță clears it, private and RO clients send unknown", async () => {
    const inv = invoicingData();
    inv.required = { seller: [], seller_if_ro: [], seller_if_vat_payer: [], client: { RO: ["name"], other: ["name"] },
      client_identity_any_of: ["cui", "vat_id", "tax_id", "private:true"], client_if_ro_vat_registered: ["cui", "vat_id"] };
    const { api, f } = await openInvoice("1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat clientul.", "c9") });
    const foreign = async () => {
      const country = f.getByLabelText("Țara");
      await choose(country, "DE");
      await choose(f.getByLabelText("Înregistrare în scopuri de TVA în România"), "not_registered");
    };
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await foreign();
    await userEvent.click(f.getByRole("button", { name: "Renunță" }));
    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    expect(f.getByLabelText("Țara")).toHaveValue("RO");
    const country = f.getByLabelText("Țara");
    await choose(country, "DE");
    expect(f.getByLabelText("Înregistrare în scopuri de TVA în România")).toHaveValue("unknown");

    await choose(f.getByLabelText("Înregistrare în scopuri de TVA în România"), "not_registered");
    await userEvent.click(f.getByLabelText("Persoană fizică"));
    await userEvent.type(f.getByLabelText("Nume"), "Ana Exemplu");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(1));
    expect((sent(api)[0].body as { client: object }).client).toMatchObject({ private: true, country: "DE", ro_vat_status: "unknown" });

    await userEvent.click(f.getByRole("button", { name: "Adaugă un client nou" }));
    await foreign();
    const back = f.getByLabelText("Țara");
    await choose(back, "RO");
    await userEvent.type(f.getByLabelText("CUI"), "99999992");
    await userEvent.type(f.getByLabelText("Nume"), "Client Exemplu SRL");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(sent(api)).toHaveLength(2));
    expect((sent(api)[1].body as { client: object }).client).toMatchObject({ country: "RO", ro_vat_status: "unknown" });
  });

  it("hides the engine's warnings while the form has unsaved edits, and sends the saved place of supply back unchanged", async () => {
    const inv = invoicingData();
    inv.drafts[0] = { ...inv.drafts[0], warnings: ["Factura de avans se emite târziu față de încasare."], supply_country: "DE" };
    const { api, f } = await openInvoice("d1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", inv.drafts[0].id) });
    expect(f.getByText("Factura de avans se emite târziu față de încasare.")).toBeInTheDocument();
    await userEvent.type(f.getByLabelText("Mențiuni (opțional)"), "x");
    expect(f.queryByText("Factura de avans se emite târziu față de încasare.")).toBeNull();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toMatch(/draft-save$/);
    expect(api.posts()[0].body).toMatchObject({ supply_country: "DE" });
  });

  /** The draft d1 as the engine takes it back, for a client id. */
  const draftBody = (client_id: string) => ({ co: "NORD", id: "d1", version: 1, series: "NRD", client_id, issue_date: "2026-10-27",
    due_date: "2026-11-26", currency: "RON", notes: "", advance_invoice: false, advance_payment_date: null, iban: "RO00TEST0000000000000001", 
    lines: [{ description: "Servicii de dezvoltare", quantity: "10", unit: "HUR", unit_price: "200.00", vat_category: "S",
      vat_rate: "21", exemption: "" }] });

  it("a draft whose client was deleted and added again asks for the client again, and keeps the rest", async () => {
    const GONE = "Clientul ciornei, Client Alfa SRL, nu mai este în lista de clienți. Alege-l din nou sau adaugă-l. Restul ciornei rămâne neschimbat.";
    // The draft still names c1; the same firm is back under a new id. The engine refuses c1 ("Clientul trebuie să fie al acestei firme.").
    const inv = invoicingData();
    inv.clients = [{ ...inv.clients[0], id: "c9" }];
    const after = structuredClone(inv);
    after.drafts[0].client_id = "c9";
    const { api, f } = await openInvoice("d1", { invoicing: () => json(inv), onPost: (c: Call) => (c.body?.client_id === "c9"
      ? answer(after, "Am salvat ciorna.", "d1")
      : json({ ok: false, message: "Clientul trebuie să fie al acestei firme.", invoicing: inv, id: null, state: fixture() })) });
    expect(f.getByText(GONE)).toBeInTheDocument();
    expect(f.getByLabelText("Client")).toHaveValue(""); // what is shown is what would be sent: no client yet
    expect(f.getByLabelText("Client")).toHaveAccessibleDescription(GONE);
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("Servicii de dezvoltare");
    for (const name of ["Emite factura", "Salvează ciorna"]) { // never sent with a client no longer on the list
      await userEvent.click(f.getByRole("button", { name }));
      await waitFor(() => expect(f.getByLabelText("Client")).toHaveFocus());
      f.getByLabelText("Client").blur();
    }
    expect(api.posts()).toHaveLength(0);
    expect(f.getByLabelText("Client")).toBeInvalid();
    expect(f.getByLabelText("Client")).toHaveAccessibleDescription(`${GONE} Lipsește clientul.`);
    await choose(f.getByLabelText("Client"), "c9");
    expect(f.queryByText(GONE)).toBeNull();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toContain("api/invoicing/draft-save");
    expect(api.posts()[0].body).toEqual(draftBody("c9"));
    expect(await screen.findByText("Am salvat ciorna.")).toBeInTheDocument();
    expect(f.queryByText(GONE)).toBeNull();
  });

  it("a draft whose client was added again with new details saves again unchanged, as the engine asks before issuing", async () => {
    const CHANGED = "Clientul a fost modificat. Ciorna trebuie salvată din nou înainte de emitere.";
    const { api, f } = await openInvoice("d1", { onPost: (c: Call) => (c.url.endsWith("/issue")
      ? json({ ok: false, message: CHANGED, invoicing: invoicingData(), id: null, state: fixture() })
      : answer(invoicingData(), "Am salvat ciorna.", "d1")) });
    await userEvent.click(f.getByRole("button", { name: "Emite factura" }));
    await decide();
    expect(await screen.findByText(CHANGED)).toBeInTheDocument();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["issue", "draft-save"]);
    expect(api.posts()[1].body).toEqual(draftBody("c1"));
    expect(await screen.findByText("Am salvat ciorna.")).toBeInTheDocument();
  });

  it("closing the form returns to the list, and back reopens it", async () => {
    at("?co=NORD&tab=vanzari");
    renderApp();
    await userEvent.click(within(await screen.findByRole("main")).getByRole("link", { name: "Factură nouă" }));
    await form();
    await userEvent.click(within(sheet()).getByRole("button", { name: "Închide" }));
    expect(window.location.search).toBe("?co=NORD&tab=vanzari");
  });
});

it("preserves gross-price evidence when saving an existing commerce draft", async () => {
  const inv = invoicingData();
  inv.drafts[0].lines[0].gross_unit_price = "121";
  const { api, f } = await openInvoice("d1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", "d1") });
  await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
  await waitFor(() => expect(api.calls.some(c => c.url.includes("draft-save"))).toBe(true));
  const saved = api.calls.find(c => c.url.includes("draft-save"))!;
  expect((saved.body as {lines: {gross_unit_price?: string}[]}).lines[0].gross_unit_price).toBe("121");
});

it("changing a draft's VAT keeps the gross prices the customer paid and the delivery date", async () => {
  const inv = invoicingData();
  const gross = { ...inv.drafts[0].lines[0], quantity: "1", unit_price: "100.000000000000", gross_unit_price: "121.00" };
  inv.drafts[0] = { ...inv.drafts[0], delivery_date: "2026-10-05", lines: [gross, { ...gross, description: "Livrare" }] };
  inv.products = [{ id: "7", company: "NORD", kind: "produs", name: "Cană", code: "", unit: "H87", unit_price: "9.00", currency: "EUR",
    vat_category: "S", vat_rate: "21", exemption: "", description: "", archived: false, version: 1, can_delete: false }];
  const { api, f } = await openInvoice("d1", { invoicing: () => json(inv), onPost: () => answer(inv, "Am salvat ciorna.", "d1") });
  const [first, second] = f.getAllByRole("group", { name: /^Linia / });
  expect(within(first).getByLabelText("Preț cu TVA")).toHaveValue("121.00");
  await choose(within(first).getByLabelText("TVA"), "S11");
  await userEvent.clear(within(second).getByLabelText("Preț cu TVA"));
  await userEvent.type(within(second).getByLabelText("Preț cu TVA"), "50");
  await userEvent.clear(within(second).getByLabelText("Produs sau serviciu"));
  await userEvent.type(within(second).getByLabelText("Produs sau serviciu"), "can");
  await userEvent.click(screen.getByRole("option", { name: /Cană/ })); // priced in EUR: the line keeps its own price
  await userEvent.click(f.getByRole("button", { name: "Adaugă o linie" }));
  const third = f.getAllByRole("group", { name: /^Linia / })[2];
  await userEvent.type(within(third).getByLabelText("Produs sau serviciu"), "Ambalaj");
  await userEvent.type(within(third).getByLabelText("Preț cu TVA"), "30");
  await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
  await waitFor(() => expect(api.posts()).toHaveLength(1));
  expect(api.posts()[0].body).toMatchObject({ delivery_date: "2026-10-05", lines: [
    { vat_category: "S", vat_rate: "11", unit_price: "121.00", gross_unit_price: "121.00" },
    { description: "Cană", unit_price: "50", gross_unit_price: "50", product_id: "7" },
    { description: "Ambalaj", vat_rate: "21", unit_price: "30", gross_unit_price: "30" }] });
});

describe("Serii de facturi", () => {
  /** Two series: DEMO, the default, with issued invoices; FREE, used by nothing. */
  const twoSeries = (): Invoicing => ({ ...invoicingData(), series: [
    { code: "DEMO", next_number: 6, issued: 5, default: true, remove_refusal: "Seria are facturi emise." },
    { code: "FREE", next_number: 1, issued: 0, default: false, remove_refusal: null }] });

  async function openPage(onPost?: (c: Call) => Response | Promise<Response>) {
    at("?co=NORD&tab=setari&sec=facturare");
    const api = renderApp({ invoicing: () => json(twoSeries()), onPost });
    const list = await screen.findByRole("region", { name: "Serii de facturi" });
    await within(list).findByText("FREE");
    return { api, list: within(list) };
  }

  it("lists each series; a series in use says why it stays, the others can become the default or go", async () => {
    const { api, list } = await openPage(() => answer(twoSeries(), "Gata."));
    expect(list.getByText("Implicită")).toBeInTheDocument();
    expect(list.getByText("Următorul număr: 6")).toBeInTheDocument();
    expect(list.getByText("5 facturi emise")).toBeInTheDocument();
    expect(list.getByText("Seria are facturi emise.")).toBeInTheDocument();
    expect(list.getAllByRole("button", { name: "Șterge seria" })).toHaveLength(1); // only FREE's
    expect(list.getAllByRole("button", { name: "Setează ca implicită" })).toHaveLength(1);
    await userEvent.click(list.getByRole("button", { name: "Setează ca implicită" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toMatchObject({ url: "/web/api/invoicing/series-default", body: { co: "NORD", code: "FREE" } });
    await userEvent.click(list.getByRole("button", { name: "Șterge seria" }));
    await decide();
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts()[1]).toMatchObject({ url: "/web/api/invoicing/series-remove", body: { co: "NORD", code: "FREE" } });
  });

  it("adds a series with its first number; its fields wait while it is created", async () => {
    const held = deferred<Response>();
    const { api, list } = await openPage(() => held.promise);
    await userEvent.click(list.getByRole("button", { name: "Adaugă serie" }));
    await userEvent.type(list.getByLabelText("Serie"), "av");
    await userEvent.clear(list.getByLabelText("Primul număr"));
    await userEvent.type(list.getByLabelText("Primul număr"), "7");
    await userEvent.click(list.getByRole("button", { name: "Adaugă seria" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual(expect.objectContaining({ url: "/web/api/invoicing/series-create", body: { co: "NORD", code: "AV", start: 7 } }));
    expect(list.getByLabelText("Serie")).toBeDisabled(); // what was sent is what gets created
    expect(list.getByLabelText("Primul număr")).toBeDisabled();
    await act(async () => held.resolve(answer(twoSeries(), "Seria a fost creată.")));
    await waitFor(() => expect(list.queryByRole("button", { name: "Adaugă seria" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(list.getByRole("button", { name: "Adaugă serie" })));
  });

  it("a new invoice starts in the default series, and Serie nouă… adds one and chooses it", async () => {
    const inv = twoSeries();
    const made: Invoicing = { ...inv, series: [...inv.series, { code: "NOU", next_number: 1, issued: 0, default: false, remove_refusal: null }] };
    let now = { ...inv, series: [...inv.series].reverse() }; // the default is not the first listed
    const { api, f } = await openInvoice("1", { invoicing: () => json(now), onPost: () => { now = made; return answer(made, "Seria a fost creată."); } });
    expect(f.getByLabelText("Serie")).toHaveValue("DEMO");
    await choose(f.getByLabelText("Serie"), "Serie nouă…");
    const group = within(f.getByRole("group", { name: "Serie nouă" }));
    await userEvent.type(group.getByLabelText("Serie"), "NOU");
    await userEvent.click(group.getByRole("button", { name: "Adaugă seria" }));
    await waitFor(() => expect(f.queryByRole("group", { name: "Serie nouă" })).toBeNull());
    expect(api.posts()[0]).toMatchObject({ url: "/web/api/invoicing/series-create", body: { co: "NORD", code: "NOU", start: 1 } });
    expect(f.getByLabelText("Serie")).toHaveValue("NOU");
  });
});

describe("Contul de pe factură", () => {
  const RON = "RO00TEST0000000000000001", EUR = "RO00BTRLEURCRT0000000001";

  it("starts with the engine's account for the currency, follows the currency until one is picked, and saves the pick", async () => {
    const { api, f } = await openInvoice("1", { onPost: () => answer(invoicingData(), "Gata.", "d9") });
    expect((await choicesOf(f.getByLabelText("Cont"))).map((c) => c.text)).toEqual(["RON, Banca Exemplu, cont 0001", "EUR, Banca Transilvania, cont 0001"]);
    expect(f.getByLabelText("Cont")).toHaveValue(RON);
    await choose(f.getByLabelText("Monedă"), "EUR");
    expect(f.getByLabelText("Cont")).toHaveValue(EUR);
    await choose(f.getByLabelText("Cont"), RON); // a RON account on a EUR invoice, chosen on purpose
    await choose(f.getByLabelText("Monedă"), "RON");
    await choose(f.getByLabelText("Monedă"), "EUR");
    expect(f.getByLabelText("Cont")).toHaveValue(RON);
    await choose(f.getByLabelText("Client"), "c1");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Consultanță");
    await userEvent.type(f.getByLabelText("Preț unitar"), "100");
    await settled(api, () => userEvent.click(f.getByRole("button", { name: "Salvează ciorna" })));
    expect(api.posts()[0].body).toMatchObject({ currency: "EUR", iban: RON });
  });

  it("a saved draft shows the account the engine issues it with, whatever its currency", async () => {
    const inv = invoicingData(); // d1 is in RON; the engine says it issues with the EUR account
    const { f } = await openInvoice("d1", { invoicing: () => json({ ...inv, drafts: inv.drafts.map((d) => ({ ...d, iban: EUR })) }) });
    expect(f.getByLabelText("Cont")).toHaveValue(EUR);
  });

  it("a draft whose account is no longer the firm's asks for another, even with one account left", async () => {
    const inv = invoicingData();
    const one = { ...inv, invoice_accounts: { accounts: [inv.invoice_accounts!.accounts[0]], default: { RON, EUR: RON } },
      drafts: inv.drafts.map((d) => ({ ...d, iban: EUR })) }; // EUR retired since the draft was saved
    const { api, f } = await openInvoice("d1", { invoicing: () => json(one), onPost: () => answer(one, "Gata.", "d1") });
    expect(f.getByLabelText("Cont")).toHaveTextContent("Alege contul");
    await choose(f.getByLabelText("Cont"), RON);
    await settled(api, () => userEvent.click(f.getByRole("button", { name: "Salvează ciorna" })));
    expect(api.posts()[0].body).toMatchObject({ id: "d1", iban: RON });
  });
});

describe("Factură nouă: produse pe linie", () => {
  const product = (over: Partial<Product> = {}): Product => ({ id: "7", company: "NORD", kind: "serviciu", name: "Consultanță fiscală", code: "CONS-01",
    unit: "HUR", unit_price: "350.00", currency: "RON", vat_category: "S", vat_rate: "11", exemption: "", description: "", archived: false, version: 1,
    can_delete: false, ...over });
  const withProducts = (products: Product[]) => ({ invoicing: () => json({ ...invoicingData(), products }) });
  const draftLines = (api: { posts: () => Call[] }) => api.posts().find((p) => p.url.endsWith("/draft-save"))?.body?.lines;

  it("a product picked from the line's list fills the line, which stays editable and sends its product", async () => {
    const { api, f } = await openInvoice("1", { ...withProducts([product(), product({ id: "8", name: "Carte tipărită", currency: "EUR", unit: "H87" })]),
      onPost: () => answer(invoicingData(), "Ciorna a fost salvată.", "d9") });
    await choose(f.getByLabelText("Client"), "c1");
    const field = f.getByLabelText("Produs sau serviciu");
    await userEvent.click(field); // the whole list, Produs nou last
    expect(within(screen.getByRole("listbox", { name: "Produse" })).getAllByRole("option").map((o) => o.textContent))
      .toEqual(["Consultanță fiscală350,00 lei / oră", "Carte tipărită350,00 EUR / buc", "Produs nou"]);
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(field).toHaveValue("Consultanță fiscală");
    expect([f.getByLabelText("UM"), f.getByLabelText("Preț unitar"), f.getByLabelText("TVA")].map((x) => (x as HTMLInputElement).value)).toEqual(["HUR", "350.00", "S11"]);
    await userEvent.clear(f.getByLabelText("Preț unitar"));
    await userEvent.type(f.getByLabelText("Preț unitar"), "300");
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(draftLines(api)).toEqual([{ description: "Consultanță fiscală", quantity: "1", unit: "HUR", unit_price: "300",
      vat_category: "S", vat_rate: "11", exemption: "", product_id: "7" }]));
  });

  it("a product priced in another currency fills the line without its price", async () => {
    const { f } = await openInvoice("1", withProducts([product({ currency: "EUR" })]));
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "cons");
    await userEvent.click(screen.getByRole("option", { name: /Consultanță fiscală/ }));
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("Consultanță fiscală");
    expect(f.getByLabelText("Preț unitar")).toHaveValue("");
  });

  it("Produs nou opens over the invoice from the line, saves the product, links it and keeps the draft", async () => {
    const { api, f } = await openInvoice("1", { ...withProducts([]),
      onPost: (c: Call) => c.url.endsWith("/product-add") ? json({ ok: true, message: "Produsul a fost salvat.", state: fixture(),
        done: { product: product({ ...c.body, id: "12", can_delete: true }) } }) : answer(invoicingData(), "Gata.", "d9") });
    await choose(f.getByLabelText("Client"), "c1");
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Montaj rafturi");
    await userEvent.type(f.getByLabelText("Preț unitar"), "220");
    await userEvent.click(screen.getByRole("button", { name: "Produse" }));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Produs nou"]); // an empty catalog: only Produs nou
    await userEvent.click(screen.getByRole("option", { name: "Produs nou" }));
    const added = within(await screen.findByRole("dialog", { name: "Produs nou" }));
    expect(added.getByLabelText("Denumire")).toHaveValue("Montaj rafturi");
    expect(added.getByLabelText("Preț unitar")).toHaveValue("220");
    await userEvent.type(added.getByLabelText("Cod (opțional)"), "MONT-1");
    await userEvent.click(added.getByRole("button", { name: "Adaugă produsul" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Produs nou" })).toBeNull());
    expect(api.posts()[0]).toEqual({ url: "/web/api/product-add", method: "POST", body: { co: "NORD",
      kind: "produs", name: "Montaj rafturi", code: "MONT-1", unit: "H87", unit_price: "220.00", currency: "RON", vat_category: "S", vat_rate: "21",
      exemption: "", description: "" } });
    expect(screen.getByRole("dialog", { name: "Factură nouă" })).toBeInTheDocument(); // still the invoice, as it was
    expect(f.getByLabelText("Client")).toHaveValue("c1");
    expect(f.getByText("Din Produse")).toBeInTheDocument();
    await userEvent.click(f.getByRole("button", { name: "Salvează ciorna" }));
    await waitFor(() => expect(draftLines(api)).toEqual([{ description: "Montaj rafturi", quantity: "1", unit: "H87", unit_price: "220.00",
      vat_category: "S", vat_rate: "21", exemption: "", product_id: "12" }]));
  });

  it("Produs nou closed while its save is on its way leaves the invoice's lines as they are", async () => {
    const held = deferred<Response>();
    const { f } = await openInvoice("1", { ...withProducts([]), onPost: (c: Call) => c.url.endsWith("/product-add") ? held.promise : answer(invoicingData(), "Gata.", "d9") });
    await userEvent.type(f.getByLabelText("Produs sau serviciu"), "Montaj");
    await userEvent.type(f.getByLabelText("Preț unitar"), "220");
    await userEvent.click(screen.getByRole("button", { name: "Produse" }));
    await userEvent.click(screen.getByRole("option", { name: "Produs nou" }));
    const added = within(await screen.findByRole("dialog", { name: "Produs nou" }));
    await userEvent.click(added.getByRole("button", { name: "Adaugă produsul" }));
    await userEvent.click(added.getByRole("button", { name: "Închide" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Produs nou" })).toBeNull());
    await act(async () => held.resolve(json({ ok: true, message: "Produsul a fost salvat.", state: fixture(), done: { product: product({ id: "12", name: "Altceva" }) } })));
    expect(f.getByLabelText("Produs sau serviciu")).toHaveValue("Montaj");
    expect(f.queryByText("Din Produse")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Factură nouă" })).toBeInTheDocument();
  });

  it("arrows keep the active product in sight in a long list", async () => {
    const seen = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    const { f } = await openInvoice("1", withProducts(Array.from({ length: 12 }, (_, i) => product({ id: String(i), name: `Produs ${i}` }))));
    await userEvent.click(f.getByLabelText("Produs sau serviciu"));
    await userEvent.keyboard("{ArrowUp}"); // from the top, round to the last option: Produs nou
    expect(seen.mock.contexts.at(-1)).toHaveTextContent("Produs nou");
    seen.mockRestore();
    await userEvent.keyboard("zzz{Escape}{ArrowUp}{Enter}"); // reopened after a search that matched nothing: still Produs nou
    expect(await screen.findByRole("dialog", { name: "Produs nou" })).toBeInTheDocument();
  });
});
