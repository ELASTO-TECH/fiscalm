// Recurring invoices (docs/openapi.json): their rows in Automatizări with the engine's client,
// total and next invoice, Oprește / Pornește / Șterge, the task of one not issued, and Repetă lunar under a saved draft.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { Recurrence, State } from "@/lib/types";
import { at, deferred, fixture, fixtureIdentity, json, type Call, renderApp, answer } from "./helpers";

beforeEach(() => localStorage.clear());

const page = () => screen.findByText("Automatizări", { selector: ".ttl" });
const main = () => within(screen.getByRole("main"));
const toast = () => document.querySelector(".toast")?.textContent;
const row = (name: string) => main().getByText(name, { selector: "b" }).closest(".row") as HTMLElement;
/** NORD's recurring invoices: the fixture's stopped one, then its running one. */
const recurrences = () => fixture().recurrences;

describe("recurring invoices in Automatizări", () => {
  it("Oprește posts recurrence-pause for the firm, stays on the page and reads the state again", async () => {
    at("?co=NORD&tab=automatizari");
    const api = renderApp({ onPost: () => json({ ok: true, message: "Regula a fost oprită.", id: "rule-maintenance", recurrences: [] }) });
    await page();
    const before = api.gets("NORD").length;
    await userEvent.click(main().getByRole("button", { name: "Oprește factura lunară pentru Client Exemplu SRL" }));
    await waitFor(() => expect(toast()).toBe("Regula a fost oprită."));
    expect(api.posts().map((c) => [c.url, c.body])).toEqual([
      ["/web/api/invoicing/recurrence-pause", { co: "NORD", id: "rule-maintenance" }]]);
    await waitFor(() => expect(api.gets("NORD").length).toBe(before + 1)); // the answer has no state: it is read again
    expect(window.location.search).toBe("?co=NORD&tab=automatizari");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Pornește posts recurrence-resume, and the engine's refusal is shown as it says it", async () => {
    at("?co=NORD&tab=automatizari");
    const refusal = "Seria regulii nu mai există. După ce administratorul instalării reface aceeași serie, pornește regula.";
    const api = renderApp({ onPost: () => json({ ok: false, message: refusal, id: "rule-hosting" }) });
    await page();
    await userEvent.click(main().getByRole("button", { name: "Pornește factura lunară pentru Persoană Exemplu" }));
    await waitFor(() => expect(toast()).toBe(refusal));
    expect(api.posts().map((c) => [c.url, c.body])).toEqual([
      ["/web/api/invoicing/recurrence-resume", { co: "NORD", id: "rule-hosting" }]]);
  });

  it("Șterge asks first, sends nothing when declined, and gives focus to the page's title after", async () => {
    at("?co=NORD&tab=automatizari");
    const api = renderApp({ onPost: () => json({ ok: true, message: "Regula a fost ștearsă.", id: "rule-hosting", recurrences: [] }) });
    await page();
    const del = main().getByRole("button", { name: "Șterge factura lunară pentru Persoană Exemplu" });
    await userEvent.click(del);
    expect(await answer(false)).toBe("Ștergi factura lunară pentru Persoană Exemplu? Facturile emise și ciornele rămân.");
    expect(api.posts()).toEqual([]);
    await userEvent.click(del);
    await answer();
    await waitFor(() => expect(toast()).toBe("Regula a fost ștearsă."));
    expect(api.posts().map((c) => [c.url, c.body])).toEqual([
      ["/web/api/invoicing/recurrence-delete", { co: "NORD", id: "rule-hosting" }]]);
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1 }));
  });

  it("words what the engine says of a rule: why it is held, a month made and months missed, a client or total it no longer has", async () => {
    at("?co=NORD&tab=automatizari");
    const [stopped, running] = recurrences();
    const s: State = fixture();
    s.recurrences = [
      { ...stopped, client: null, total: null, reason: "missing_client", can_resume: false },
      { ...running, id: "r2", client: "Client Doi SRL", due_days: 0, reason: "rate_unavailable", last_period: "2026-09",
        last_invoice: { id: "i2", number: "NRD-44", client: "Client Doi SRL", at: "2026-09-01T00:00:00" },
        last_skipped: { recurrence: "r2", company: "NORD", from_period: "2026-07", through_period: "2026-08", count: 2, at: "2026-09-01T00:00:00" } },
      { ...stopped, id: "r3", client: "Client Trei SRL", reason: "missing_series", can_resume: false, last_period: "2026-10",
        last_invoice: { id: "i3", number: "NRD-40", client: "Client Trei SRL", at: "2026-09-01T00:00:00" } }, // October's was refused
      { ...stopped, id: "r4", client: "Client Patru SRL", reason: "invalid_template" },
      { ...stopped, id: "r5", client: "Client Cinci SRL", reason: "unsupported_currency", template: { ...stopped.template, currency: "EUR" } },
    ] satisfies Recurrence[];
    renderApp({ state: () => json(s) });
    await page();
    expect(row("Client șters").textContent).toBe("Client ștersFactură lunară, scadența la 15 zile.Oprită"
      + "Clientul regulii lipsește. Pentru o regulă nouă, alege un client existent.Șterge"); // the engine won't start it
    expect(row("Client Doi SRL").textContent).toBe("Client Doi SRL302,50 lei pe lună, scadența în ziua emiterii."
      + "Pornită. Următoarea factură pe 1 nov. Ultima pentru septembrie 2026: NRD-44. 2 luni fără factură, până în august 2026."
      + "Cursul BNR nu a putut fi citit. Verificarea se reia automat.OpreșteȘterge");
    expect(row("Client Doi SRL").querySelector(".ic")).toHaveClass("soon");
    expect(row("Client Trei SRL")).toHaveTextContent("Ultima pentru octombrie 2026.Seria regulii nu mai există.");
    expect(row("Client Patru SRL")).toHaveTextContent("Ciorna regulii nu mai poate fi folosită. Creează o regulă nouă dintr-o ciornă salvată.");
    expect(row("Client Cinci SRL")).toHaveTextContent("48,40 EUR pe lună");
    expect(row("Client Cinci SRL")).toHaveTextContent("BNR nu publică un curs pentru această monedă. Pentru o regulă nouă, alege altă monedă.");
  });

  it.each(["member", "readonly"] as const)("one the engine didn't issue is a task saying why, which opens its draft for a %s", async (role) => {
    at("?co=NORD&tab=sarcini");
    const s: State = fixture();
    const why = "Data emiterii nu poate fi înaintea ultimei facturi din serie.";
    s.recurring_refused = [{ id: "d1", client: "Client Exemplu SRL", recurrence: "rule-maintenance", why, at: "2026-10-27T00:00:00" }];
    renderApp({ state: () => json(s), principal: fixtureIdentity(role) });
    expect(await screen.findByText("Factura lunară pentru Client Exemplu SRL nu a fost emisă")).toBeInTheDocument();
    expect(screen.getByText(why)).toBeInTheDocument();
    const link = screen.queryByRole("link", { name: "Vezi ciorna" }); // the sheet opens for members only
    expect(link?.getAttribute("href") ?? null).toBe(role === "member" ? "?co=NORD&tab=vanzari&new=d1" : null);
  });

  it("offers only what the engine accepts, whatever the rule's state says", async () => {
    at("?co=NORD&tab=automatizari");
    const [stopped, running] = recurrences();
    const s: State = fixture();
    s.recurrences = [
      { ...stopped, client: "Client Unu SRL", can_resume: false },
      { ...stopped, id: "r2", client: "Client Doi SRL", can_resume: true, can_delete: false },
      { ...running, id: "r3", client: "Client Trei SRL", can_pause: false, can_resume: false },
    ] satisfies Recurrence[];
    renderApp({ state: () => json(s) });
    await page();
    const names = (n: string) => within(row(n)).queryAllByRole("button").map((b) => b.getAttribute("aria-label"));
    expect(names("Client Unu SRL")).toEqual(["Șterge factura lunară pentru Client Unu SRL"]);
    expect(names("Client Doi SRL")).toEqual(["Pornește factura lunară pentru Client Doi SRL"]);
    expect(names("Client Trei SRL")).toEqual(["Șterge factura lunară pentru Client Trei SRL"]);
  });

  it("under Toate firmele, a recurring invoice's switch posts for its own firm", async () => {
    at("?co=ALL&tab=automatizari");
    const api = renderApp({ onPost: () => json({ ok: true, message: "Regula a fost pornită.", id: "rule-hosting" }) });
    await page();
    const nord = within(screen.getByRole("region", { name: "NORD" }));
    expect(within(screen.getByRole("region", { name: "STUDIO9" })).queryByText("Persoană Exemplu")).toBeNull();
    expect(within(screen.getByRole("region", { name: "STUDIO9" })).getByRole("link", { name: /^Factură lunară nouă/ }))
      .toHaveAttribute("href", "?co=STUDIO9&tab=vanzari#drafts-h");
    await userEvent.click(nord.getByRole("button", { name: "Pornește factura lunară pentru Persoană Exemplu" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "rule-hosting" });
    expect(window.location.search).toBe("?co=ALL&tab=automatizari");
  });
});

describe("Repetă lunar under a saved draft", () => {
  /** The draft's sheet, with Repetă lunar's fields open. */
  async function open() {
    at("?co=NORD&tab=vanzari&new=d1");
    const d = await screen.findByRole("dialog", { name: "Ciornă de factură" });
    const btn = await within(d).findByRole("button", { name: "Repetă lunar" });
    expect(btn).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    return within(within(d).getByRole("group", { name: "Repetă lunar" }));
  }
  const created = () => json({ ok: true, message: "Regula a fost creată.", id: "new-rule", recurrences: [] });

  it("starts from the draft's day and due days, and posts recurrence-create with them and the first month", async () => {
    const api = renderApp({ onPost: created });
    const g = await open();
    expect(g.getByLabelText("Ziua din lună")).toHaveValue("27"); // the draft is dated 27 oct
    expect(g.getByLabelText("Scadență, în zile")).toHaveValue("30"); // due 26 nov
    await userEvent.clear(g.getByLabelText("Ziua din lună"));
    await userEvent.type(g.getByLabelText("Ziua din lună"), "31");
    await userEvent.type(g.getByLabelText("Prima lună (opțional)"), "2026-12");
    await userEvent.click(g.getByRole("button", { name: "Creează regula" }));
    await waitFor(() => expect(toast()).toBe("Regula a fost creată."));
    expect(api.posts().map((c: Call) => [c.url, c.body])).toEqual([["/web/api/invoicing/recurrence-create",
      { co: "NORD", draft_id: "d1", day: 31, due_days: 30, first_period: "2026-12" }]]);
    await waitFor(() => expect(screen.queryByRole("group", { name: "Repetă lunar" })).toBeNull()); // done: the fields close
    expect(screen.getByRole("dialog", { name: "Ciornă de factură" })).toBeInTheDocument(); // the draft stays open
  });

  it("without a first month leaves the choice to the engine; Enter creates the rule and doesn't save the invoice", async () => {
    const api = renderApp({ onPost: created });
    const g = await open();
    await userEvent.type(g.getByLabelText("Scadență, în zile"), "{Enter}");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toBe("/web/api/invoicing/recurrence-create");
    expect(api.posts()[0].body).toEqual({ co: "NORD", draft_id: "d1", day: 27, due_days: 30 });
  });

  it("Enter on Renunță closes the fields and sends nothing; Enter on Creează regula sends once", async () => {
    const api = renderApp({ onPost: created });
    let g = await open();
    g.getByRole("button", { name: "Renunță" }).focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("group", { name: "Repetă lunar" })).toBeNull());
    expect(api.posts()).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Repetă lunar" }));
    g = within(screen.getByRole("group", { name: "Repetă lunar" }));
    g.getByRole("button", { name: "Creează regula" }).focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(toast()).toBe("Regula a fost creată."));
    expect(api.posts().map((c: Call) => c.url)).toEqual(["/web/api/invoicing/recurrence-create"]);
  });

  it("waits for whole numbers, sends once, and keeps the fields open when the engine refuses", async () => {
    const answer = deferred<Response>();
    const api = renderApp({ onPost: () => answer.promise });
    const g = await open();
    const create = g.getByRole("button", { name: "Creează regula" });
    await userEvent.clear(g.getByLabelText("Ziua din lună"));
    expect(create).toBeDisabled();
    await userEvent.type(g.getByLabelText("Ziua din lună"), "1,5");
    expect(create).toBeDisabled();
    await userEvent.clear(g.getByLabelText("Ziua din lună"));
    await userEvent.type(g.getByLabelText("Ziua din lună"), "40"); // the engine says what it accepts
    await userEvent.click(create);
    await userEvent.click(create);
    const refusal = "Ziua trebuie să fie între 1 și 31, iar termenul între 0 și 365 de zile.";
    await act(async () => answer.resolve(json({ ok: false, message: refusal, id: null })));
    await waitFor(() => expect(toast()).toBe(refusal));
    expect(api.posts()).toHaveLength(1);
    expect(screen.getByRole("group", { name: "Repetă lunar" })).toBeInTheDocument();
  });

  it("isn't offered while the draft has unsaved changes", async () => {
    renderApp();
    const d = within(await screen.findByRole("dialog", { name: "Ciornă de factură" }));
    await d.findByRole("button", { name: "Repetă lunar" });
    await userEvent.type(d.getByLabelText("Produs sau serviciu"), " extra");
    expect(d.queryByRole("button", { name: "Repetă lunar" })).toBeNull();
  });
});
