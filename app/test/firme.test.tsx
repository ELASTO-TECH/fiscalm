// Setări › Firme against a fake engine: the configured companies, adding one by its CUI after ANAF's public data is shown
// (an inactive firm can't be added), and renaming or recolouring one. Install-wide: no POST names a company.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { companiesConfig as config, at, deferred, fixture, fixtureAll, fixtureIntegrations, json, type Call, renderApp, choose } from "./helpers";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

const found = { cui: "33333330", legal_name: "FIRMA NOUĂ SRL", address: "Str. Exemplu 3, Iași", vat_payer: true, inactive: false };
const answer = (done: object | null, message = "") => json({ ok: true, message, next: null, done, state: fixtureAll() });

async function page(options: Parameters<typeof renderApp>[0] = {}) {
  const api = renderApp(options);
  await screen.findByRole("heading", { name: "2 firme" });
  return { api, p: within(document.body) };
}

describe("Firme", () => {
  it("is the first thing in Setări › Configurare", async () => {
    at("?co=NORD&tab=setari");
    renderApp({ companies: () => json(config()) });
    expect(await screen.findByText(`fiscalm ${fixture().system.version}`)).toBeInTheDocument();
    const row = await screen.findByRole("link", { name: /^Firme/ });
    expect(row).toHaveAttribute("href", "?co=NORD&tab=setari&sec=firme");
    expect(row).toHaveTextContent("2 firme");
  });

  it("offers adding and changing companies only to whom the engine says may manage them", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { p } = await page({ companies: () => json(config()), state: () => json({ ...fixture(), can_manage_companies: false }) });
    expect(p.getByText("Atelier Nord SRL, CUI 11111110")).toBeInTheDocument();
    expect(p.queryByRole("button", { name: "Adaugă o firmă" })).toBeNull();
    expect(p.queryByRole("button", { name: "Modifică STUDIO9" })).toBeNull();
    expect(p.getByText("Doar administratorul instalării adaugă și modifică firmele.")).toBeInTheDocument();
  });

  it("adds a company after showing what ANAF says, with a name and the colour picked", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { api, p } = await page({ companies: () => json(config()), all: () => json(fixtureAll()),
      onPost: (c: Call) => (c.url.endsWith("company-lookup") ? answer(found)
        : answer({ key: "NOUA", name: "Noua", legal_name: "FIRMA NOUĂ SRL", cui: "33333330", palette: "blue" }, "Firma a fost adăugată.")) });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    await userEvent.type(p.getByLabelText("CUI"), "RO 3333 3330");
    await userEvent.click(p.getByRole("button", { name: "Caută la ANAF" }));
    await waitFor(() => expect(api.posts()[0]).toEqual({ url: "/web/api/company-lookup", method: "POST", body: { cui: "RO 3333 3330" } }));
    const status = await p.findByRole("form", { name: "Adaugă firma" });
    expect(status).toHaveTextContent("FIRMA NOUĂ SRL");
    expect(status).toHaveTextContent("Plătitor de TVA");
    expect(status).toHaveTextContent("CUI 33333330");
    expect(p.getByLabelText("Albastru")).not.toBeChecked(); // no colour until one is picked: the engine has the default
    await userEvent.type(p.getByLabelText("Nume în aplicație (opțional)"), "Noua");
    await userEvent.click(p.getByLabelText("Violet"));
    await userEvent.click(p.getByRole("button", { name: "Adaugă firma" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    // The engine takes only the normalised CUI it found, never the firm's details from the app
    expect(api.posts()[1]).toEqual({ url: "/web/api/company-add", method: "POST", body: { cui: "33333330", name: "Noua", palette: "violet" } });
    expect(await screen.findByText("Firma a fost adăugată.")).toBeInTheDocument();
    await waitFor(() => expect(p.getByRole("button", { name: "Adaugă o firmă" })).toHaveFocus());
  });

  it("without a colour picked, the engine chooses it", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { api, p } = await page({ companies: () => json(config()), onPost: (c: Call) => (c.url.endsWith("company-lookup") ? answer(found) : answer({ key: "X" })) });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
    await userEvent.click(await p.findByRole("button", { name: "Adaugă firma" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts()[1].body).toEqual({ cui: "33333330" });
  });

  it("an answer for a CUI no longer in the field is dropped, so the firm added is the one on screen", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const slow = deferred<Response>();
    const { api, p } = await page({ companies: () => json(config()), onPost: () => slow.promise });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    await userEvent.type(p.getByLabelText("CUI"), "123{Enter}");
    await userEvent.type(p.getByLabelText("CUI"), "4567"); // typed while ANAF answers for 123
    slow.resolve(answer({ ...found, cui: "123" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 50));
    expect(p.queryByText("FIRMA NOUĂ SRL")).toBeNull();
    expect(p.queryByRole("button", { name: "Adaugă firma" })).toBeNull();
  });

  it("a refused add keeps the firm on screen", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { p } = await page({ companies: () => json(config()), onPost: (c: Call) => (c.url.endsWith("company-lookup") ? answer(found)
      : json({ ok: false, message: "Firma este deja configurată.", done: null, state: null })) });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
    await userEvent.click(await p.findByRole("button", { name: "Adaugă firma" }));
    expect(await screen.findByText("Firma este deja configurată.")).toBeInTheDocument();
    expect(p.getByText("FIRMA NOUĂ SRL")).toBeInTheDocument();
  });

  it("a colour the engine offers that the app can't show isn't offered", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { p } = await page({ companies: () => json({ ...config(), palettes: [...config().palettes, "teal"] }) });
    await userEvent.click(p.getByRole("button", { name: "Modifică STUDIO9" }));
    expect(p.getAllByRole("radio")).toHaveLength(6);
  });

  it("an inactive firm is shown but can't be added", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { api, p } = await page({ companies: () => json(config()), onPost: () => answer({ ...found, inactive: true }) });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
    expect(await p.findByText("Firma figurează ca inactivă la ANAF și nu poate fi adăugată.")).toBeInTheDocument();
    expect(p.queryByRole("button", { name: "Adaugă firma" })).toBeNull();
    expect(api.posts()).toHaveLength(1);
  });

  it("a changed CUI forgets what was found for the old one", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { p } = await page({ companies: () => json(config()), onPost: () => answer(found) });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
    await p.findByText("FIRMA NOUĂ SRL");
    await userEvent.type(p.getByLabelText("CUI"), "1");
    expect(p.queryByText("FIRMA NOUĂ SRL")).toBeNull();
    expect(p.queryByRole("button", { name: "Adaugă firma" })).toBeNull();
  });

  it.each([
    ["ANAF gave no answer: try again with the same CUI", { retry: true }, "Încearcă din nou", undefined],
    ["ANAF does not know the CUI: check the digits", null, "Caută la ANAF", "true"],
  ])("a refused lookup shows its reason under the CUI: %s", async (_, done, again, invalid) => {
    at("?co=NORD&tab=setari&sec=firme");
    const { api, p } = await page({ companies: () => json(config()), onPost: () => json({ ok: false, message: "Refuzul ANAF", done, state: null }) });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    const cui = p.getByLabelText("CUI");
    await userEvent.type(cui, "RO 123{Enter}");
    expect(await p.findByRole("alert")).toHaveTextContent("Refuzul ANAF");
    expect(cui).toHaveAccessibleDescription("Refuzul ANAF");
    expect(cui).toHaveValue("RO 123");
    expect(cui.getAttribute("aria-invalid") ?? undefined).toBe(invalid);
    expect(p.queryByRole("button", { name: "Adaugă firma" })).toBeNull();
    await userEvent.click(p.getByRole("button", { name: again }));
    await waitFor(() => expect(api.posts().map((c) => c.body)).toEqual([{ cui: "RO 123" }, { cui: "RO 123" }]));
  });

  it("a lookup that gets no answer at all says so under the CUI and can be asked again", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { api, p } = await page({ companies: () => json(config()), onPost: () => new Response("<html>502</html>", { status: 502 }) });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
    expect(await p.findByRole("alert")).toHaveTextContent("Căutarea nu s-a încheiat. Încearcă din nou.");
    await userEvent.click(p.getByRole("button", { name: "Încearcă din nou" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
  });

  it("renames and recolours a company, sending only what changed, then returns to it", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { api, p } = await page({ companies: () => json(config()), onPost: () => answer({ key: "STUDIO9", name: "Studio 9", palette: "green" }, "Firma a fost actualizată.") });
    await userEvent.click(p.getByRole("button", { name: "Modifică STUDIO9" }));
    const form = within(p.getByRole("form", { name: "Modifică STUDIO9" }));
    const save = form.getByRole("button", { name: "Salvează" });
    expect(save).toBeDisabled(); // nothing changed yet
    await userEvent.clear(form.getByLabelText("Nume în aplicație"));
    await userEvent.type(form.getByLabelText("Nume în aplicație"), "Studio 9");
    await userEvent.click(form.getByLabelText("Roz"));
    await userEvent.click(save);
    await waitFor(() => expect(api.posts()[0]).toEqual({ url: "/web/api/company-update", method: "POST",
      body: { key: "STUDIO9", name: "Studio 9", palette: "rose" } }));
    await waitFor(() => expect(p.queryByRole("form", { name: "Modifică STUDIO9" })).toBeNull());
    await waitFor(() => expect(p.getByRole("button", { name: "Modifică STUDIO9" })).toHaveFocus());
  });

  it("works before the install goes live (configuration only): adding stays possible", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const s = fixture();
    s.live = false; // the company shown is in shadow mode
    const { api, p } = await page({ companies: () => json(config()), state: () => json(s), onPost: () => answer(found) });
    // the shadow banner says Sandbox in one line, with the way to Setări › Mod
    expect(screen.getByText("Sandbox:").parentElement).toHaveTextContent("Sandbox: facturile se trimit doar la ANAF Test. Treci în Live");
    expect(screen.getByRole("link", { name: "Treci în Live" })).toHaveAttribute("href", expect.stringContaining("sec=mod"));
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(await p.findByRole("button", { name: "Adaugă firma" })).toBeEnabled();
  });

  it("shows the current company list before enabling edits, ignoring an older read", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const list = deferred<Response>();
    let reads = 0;
    const api = renderApp({ companies: () => ++reads === 1 ? list.promise : json(config()) });
    await screen.findByRole("heading", { name: "Firme", level: 2 });
    expect(screen.queryByRole("button", { name: "Adaugă o firmă" })).toBeNull();
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    await act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(api.gets("NORD")).toHaveLength(2));
    expect(await screen.findByRole("button", { name: "Adaugă o firmă" })).toBeInTheDocument();
    expect(screen.getByText("Atelier Nord SRL, CUI 11111110")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Modifică STUDIO9" })).toBeInTheDocument();
    await act(async () => { list.resolve(json({ ...config(), companies: [] })); await list.promise; });
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(screen.getByRole("heading", { name: "2 firme" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Modifică STUDIO9" })).toBeInTheDocument();
  });

  it("while a request runs, Renunță waits; what was found is read out; a pasted CUI isn't cut", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const slow = deferred<Response>();
    const { p } = await page({ companies: () => json(config()), onPost: () => slow.promise });
    await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
    const live = p.getAllByRole("status").find((x) => x.classList.contains("vh"))!;
    expect(live).toHaveTextContent(""); // there before anything is found, so the change is announced
    await userEvent.type(p.getByLabelText("CUI"), "RO 3333 3330 - copiat{Enter}");
    expect(p.getByLabelText("CUI")).toHaveValue("RO 3333 3330 - copiat");
    expect(p.getByRole("button", { name: "Renunță" })).toBeDisabled();
    slow.resolve(answer(found));
    await waitFor(() => expect(live).toHaveTextContent("FIRMA NOUĂ SRL, CUI 33333330"));
    expect(p.getByRole("button", { name: "Renunță" })).toBeEnabled();
  });

  it("shows each company's account only by its last 4 digits, and sets a first one by its own form", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], has_ron_account: true, iban_last4: "0001" };
    cfg.companies[1] = { ...cfg.companies[1], has_ron_account: false, iban_last4: "" };
    const { api, p } = await page({ companies: () => json(cfg), onPost: () => answer({ key: "STUDIO9", has_ron_account: true, iban_last4: "0000" }, "Contul firmei a fost salvat.") });
    await userEvent.click(p.getByRole("button", { name: "Modifică STUDIO9" }));
    const bank = within(p.getByRole("form", { name: "Contul în lei pentru STUDIO9" }));
    expect(bank.getByText("Nu are încă un cont în lei.")).toBeInTheDocument();
    await userEvent.type(bank.getByLabelText("IBAN"), "ro49 aaaa 1b31 0075 9384 0000{Enter}"); // Enter saves this form only
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/company-bank", method: "POST", body: { key: "STUDIO9", iban: "RO49AAAA1B31007593840000" } });
    expect(await screen.findByText("Contul firmei a fost salvat.")).toBeInTheDocument();
    await waitFor(() => expect(bank.getByLabelText("IBAN")).toHaveValue(""));
    expect(document.body.textContent).not.toContain("RO49AAAA1B31007593840000"); // gone once saved
  });

  it("a firm says which ANAF connection and which inboxes read it, as the engine decides", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const d = fixtureIntegrations();
    const anaf = d.items.find((i) => i.kind === "anaf")!, mail = d.items.find((i) => i.kind === "mail")!;
    d.items.push({ ...anaf, id: "anaf-2", name: "ANAF Studio" });
    d.coverage = [{ firm: "NORD", anaf: null, anaf_also: [], mail: [mail.id] },
      { firm: "STUDIO9", anaf: anaf.id, anaf_also: ["anaf-2"], mail: [] }];
    const { p } = await page({ companies: () => json(config()), integrations: () => json(d) });
    await userEvent.click(p.getByRole("button", { name: "Modifică STUDIO9" }));
    const part = within(await p.findByRole("region", { name: "Integrări" }));
    expect(part.getByRole("link", { name: anaf.name })).toHaveAttribute("href", expect.stringContaining(`int=${anaf.id}`));
    expect(part.getByText(/Este și în/)).toHaveTextContent("Este și în ANAF Studio. Lasă firma într-una singură.");
    expect(part.getByRole("link", { name: "Nicio adresă" })).toBeInTheDocument();
    await userEvent.click(p.getByRole("button", { name: "Renunță" }));
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const nord = within(await p.findByRole("region", { name: "Integrări" }));
    expect(nord.getByRole("link", { name: "Neconectat" })).toBeInTheDocument();
    expect(nord.getByRole("link", { name: mail.name })).toBeInTheDocument();
  });

  it("marks the account payments leave from on its bank, and never offers to replace it from the app", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], has_ron_account: true, iban_last4: "0001", banks: [
      { bank: "bt", name: "Banca Transilvania", connect: "ready", connection: null }], accounts: [
      { id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, opened: null, statement_senders: [], last_statement: null },
      { id: "acc2", iban_last4: "0002", currency: "EUR", bank: "bt", bank_name: "Banca Transilvania", pays: false, retired: null, opened: null, statement_senders: [], last_statement: null }] };
    const { p } = await page({ companies: () => json(cfg) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const list = within(p.getByRole("list", { name: "Conturi" }));
    const rows = list.getAllByRole("listitem").map((li) => li.textContent?.split("Date cont")[0]); // each with its Date cont
    expect(rows).toEqual(["RON ••••0001Plățile pleacă de aici", "EUR ••••0002"]);
    expect(p.queryByRole("form", { name: "Contul în lei pentru NORD" })).toBeNull();
  });

  it("the engine's refusal is shown and the IBAN kept to fix; Enter in the name saves only the name", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[1] = { ...cfg.companies[1], has_ron_account: false, iban_last4: "" };
    const { api, p } = await page({ companies: () => json(cfg), onPost: (c: Call) => (c.url.endsWith("company-bank")
      ? json({ ok: false, message: "IBAN-ul nu este valid.", done: null, state: null }) : answer({ key: "STUDIO9" }, "Firma a fost actualizată.")) });
    await userEvent.click(p.getByRole("button", { name: "Modifică STUDIO9" }));
    const bank = within(p.getByRole("form", { name: "Contul în lei pentru STUDIO9" }));
    await userEvent.type(bank.getByLabelText("IBAN"), "RO00{Enter}");
    expect(await screen.findByText("IBAN-ul nu este valid.")).toBeInTheDocument();
    expect(bank.getByLabelText("IBAN")).toHaveValue("RO00");
    const name = within(p.getByRole("form", { name: "Modifică STUDIO9" })).getByLabelText("Nume în aplicație");
    await userEvent.type(name, " Nou{Enter}");
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts()[1].url).toBe("/web/api/company-update");
  });

  it("an older engine without account fields shows no account section", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { p } = await page({ companies: () => json(config()) });
    await userEvent.click(p.getByRole("button", { name: "Modifică STUDIO9" }));
    expect(p.getByLabelText("Nume în aplicație")).toBeInTheDocument(); // the editor itself opens
    expect(p.queryByRole("region", { name: "Bănci și conturi" })).toBeNull();
    expect(p.queryByRole("form", { name: /^Contul în lei pentru|^Adaugă un cont pentru/ })).toBeNull();
  });

  it("adds an account by IBAN in Bănci și conturi, at any bank and currency, and points to Tranzacții for balances", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], has_ron_account: true, iban_last4: "0001", banks: [
      { bank: "bt", name: "Banca Transilvania", connect: "ready", connection: null }], accounts: [
      { id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, opened: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: "2026-10" }] };
    const { api, p } = await page({ companies: () => json(cfg), onPost: (c: Call) => (c.url.endsWith("company-account")
      ? answer({ key: "NORD", currency: "USD", bank: "bt", iban_last4: "0007" }, "Contul a fost adăugat.") : json({ ok: true, message: "", state: null, done: { offers: [] } })) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const conn = within(p.getByRole("region", { name: "Bănci și conturi" }));
    expect(conn.getByRole("button", { name: "Conectează banca" })).toBeInTheDocument();
    expect(conn.getByRole("link", { name: "Tranzacții" })).toHaveAttribute("href", "?co=NORD&tab=tranzactii");
    await userEvent.click(conn.getByText("Adaugă un cont"));
    const form = within(p.getByRole("form", { name: "Adaugă un cont pentru NORD" }));
    await userEvent.type(form.getByLabelText("IBAN-ul contului nou"), "RO71INGBUSD0000000000007");
    expect(form.getByLabelText("Moneda")).toHaveValue("EUR"); // another bank's layout says nothing: the firm has lei
    await userEvent.clear(form.getByLabelText("IBAN-ul contului nou"));
    await userEvent.type(form.getByLabelText("IBAN-ul contului nou"), "ro71 btrl usdcrt 0000000007");
    expect(form.getByLabelText("Moneda")).toHaveValue("USD"); // a BT IBAN names it after the bank code
    await userEvent.click(form.getByRole("button", { name: "Adaugă contul" }));
    const added = () => api.posts().filter((c) => c.url.endsWith("company-account"));
    await waitFor(() => expect(added()).toEqual([{ url: "/web/api/company-account", method: "POST",
      body: { key: "NORD", iban: "RO71BTRLUSDCRT0000000007", currency: "USD" } }]));
    await waitFor(() => expect(form.getByLabelText("IBAN-ul contului nou")).toHaveValue(""));
  });

  it("sets when an account opened and closed, sending only what changed; a closed account keeps its own list", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], has_ron_account: true, iban_last4: "0001", banks: [
      { bank: "bt", name: "Banca Transilvania", connect: "ready", connection: null }], accounts: [
      { id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, opened: null, statement_senders: [], last_statement: null },
      // at a bank outside the catalogue: no bank card lists it once closed
      { id: "acc2", iban_last4: "0002", currency: "EUR", bank: "", bank_name: "", pays: false, retired: "2026-10-31", opened: "2024-03", statement_senders: [], last_statement: null }] };
    const pending = deferred<Response>();
    const { api, p } = await page({ companies: () => json(cfg), onPost: (c: Call) => (c.url.endsWith("company-account-dates")
      ? pending.promise : json({ ok: true, message: "", state: null, done: { offers: [] } })) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const closed = within(p.getByRole("list", { name: "Conturi închise" }));
    expect(closed.getByText("Altă bancă, deschis din 03.2024, închis pe 31.10.2026")).toBeInTheDocument();
    await userEvent.click(closed.getByText("Date cont"));
    const form = within(p.getByRole("form", { name: "Datele contului EUR ••••0002" }));
    expect(form.getByRole("button", { name: "Salvează datele" })).toBeDisabled(); // nothing changed yet
    await userEvent.clear(form.getByLabelText(/Deschis din/));
    await userEvent.type(form.getByLabelText(/Deschis din/), "2024-03-14");
    await userEvent.click(form.getByRole("button", { name: "Salvează datele" }));
    const saved = () => api.posts().filter((c) => c.url.endsWith("company-account-dates"));
    // the closing day it didn't touch stays out: another person's change to it is never undone
    await waitFor(() => expect(saved()).toEqual([{ url: "/web/api/company-account-dates", method: "POST",
      body: { key: "NORD", account: "acc2", opened: "2024-03-14" } }]));
    // a day typed while the answer is on its way is kept; the field saved shows the engine's date again (here the
    // list read again still says 03.2024, as another person may have set it meanwhile) and stays editable
    await userEvent.clear(form.getByLabelText("Închis pe"));
    await userEvent.type(form.getByLabelText("Închis pe"), "2026-10-30");
    await act(async () => pending.resolve(await answer({ key: "NORD", iban_last4: "0002", opened: "2024-03-14", retired: "2026-10-31" }, "Datele contului au fost salvate.")));
    await waitFor(() => expect(form.getByLabelText(/Deschis din/)).toHaveValue("2024-03"));
    expect(form.getByLabelText(/Deschis din/)).not.toHaveAttribute("readonly");
    expect(form.getByLabelText("Închis pe")).toHaveValue("2026-10-30");
    expect(form.getByRole("button", { name: "Salvează datele" })).toBeEnabled();
  });

  it("lists a firm in one row with one button, and sets a bank's statement password write-only in its Bănci", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], banks: [
      { bank: "bt", name: "Banca Transilvania", connect: "ready", connection: null, statement_password: false },
      { bank: "ing", name: "ING Bank", connect: "ready", connection: null }], accounts: [
      { id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, opened: null, statement_senders: [], last_statement: null },
      { id: "acc2", iban_last4: "0002", currency: "EUR", bank: "bt", bank_name: "Banca Transilvania", pays: false, retired: null, opened: null, statement_senders: [], last_statement: null },
      { id: "acc3", iban_last4: "0007", currency: "EUR", bank: "ing", bank_name: "ING Bank", pays: false, retired: null, opened: null, statement_senders: [], last_statement: null }] };
    const { api, p } = await page({ companies: () => json(cfg), onPost: () => answer({}, "Parola extraselor a fost salvată.") });
    expect(p.getByText("2 bănci, 3 conturi")).toBeInTheDocument();
    expect(p.queryByRole("link", { name: /Bănci și conturi/ })).toBeNull();
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    expect(p.getAllByRole("form", { name: /^Parola extraselor/ })).toHaveLength(1); // ING sends no protected PDF
    const form = within(p.getByRole("form", { name: "Parola extraselor de la Banca Transilvania" }));
    const field = form.getByLabelText("Parola extraselor PDF") as HTMLInputElement;
    expect(field.type).toBe("password");
    expect(form.getByText("Lipsește: extrasele PDF nu se pot citi.")).toBeInTheDocument();
    expect(form.queryByRole("button", { name: "Anulează" })).toBeNull();
    await userEvent.type(field, "parola-inventata");
    await userEvent.click(form.getByRole("button", { name: "Salvează" }));
    const sent = () => api.posts().filter((c) => c.url.endsWith("company-statement-password")); // the editor also lists bank offers
    await waitFor(() => expect(sent()).toEqual([{ url: "/web/api/company-statement-password", method: "POST",
      body: { key: "NORD", bank: "bt", password: "parola-inventata" } }]));
    await waitFor(() => expect(field).toHaveValue(""));
    expect(field).toHaveFocus();
  });

  it("keeps a statement password typed while the previous one was saving", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    let saved = false;
    let release!: (r: Response) => void;
    const cfg = () => { const c = config(); c.companies[0] = { ...c.companies[0], banks: [{ bank: "bt", name: "Banca Transilvania", connect: "ready", connection: null, statement_password: saved }],
      accounts: [{ id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, opened: null, statement_senders: [], last_statement: null }] }; return c; };
    const { p } = await page({ companies: () => json(cfg()), onPost: (c) => c.url.endsWith("company-statement-password")
      ? new Promise<Response>((done) => { release = (r) => { saved = true; done(r); }; }) : answer({}) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const form = within(p.getByRole("form", { name: "Parola extraselor de la Banca Transilvania" }));
    const field = form.getByLabelText("Parola extraselor PDF");
    await userEvent.type(field, "prima");
    await userEvent.click(form.getByRole("button", { name: "Salvează" }));
    await waitFor(() => expect(release).toBeTypeOf("function"));
    await userEvent.type(field, "-a-doua");
    await act(async () => { release(answer({}, "Parola extraselor a fost salvată.")); });
    await waitFor(() => expect(p.getByText("Parola e salvată.")).toBeInTheDocument());
    expect(p.getByLabelText("Parola extraselor PDF")).toHaveValue("prima-a-doua");
  });

  it("shows a saved statement password only as its state, and Anulează closes its field without saving", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], banks: [{ bank: "bt", name: "Banca Transilvania", connect: "ready", connection: null, statement_password: true }], accounts: [
      { id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, opened: null, statement_senders: [], last_statement: null }] };
    const { api, p } = await page({ companies: () => json(cfg) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    expect(p.getByText("Parola e salvată.")).toBeInTheDocument();
    expect(p.queryByLabelText("Parola extraselor PDF")).toBeNull();
    await userEvent.click(p.getByRole("button", { name: "Schimbă parola" }));
    await userEvent.type(p.getByLabelText("Parola extraselor PDF"), "alta-parola");
    await userEvent.click(p.getByRole("button", { name: "Anulează" }));
    expect(p.queryByLabelText("Parola extraselor PDF")).toBeNull();
    expect(p.getByRole("button", { name: "Schimbă parola" })).toHaveFocus();
    expect(api.posts().filter((c) => c.url.endsWith("company-statement-password"))).toEqual([]);
  });

  it.each([["export.zip", "application/zip", "2026-03-17", "2026-03-01"], ["unpaid.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", null, "-01-01"]])("imports history with a start date and the original file (%s)", async (name, type, first, proposed) => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], history: { start: null, first_statement: first, issued: 0, received: 0 } };
    const done = "Facturile au fost importate: 21 de facturi în istoric, o factură urmărită de la data de început.";
    const { api, p } = await page({ companies: () => json(cfg), onPost: () => json({ ok: true, message: done, next: null, done: {}, state: null }) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const section = within(p.getByRole("region", { name: "Facturi din alt program" }));
    const button = section.getByRole("button", { name: "Importă facturi" });
    const date = section.getByLabelText("Data de început"); // the month of the oldest statement, else 1 January
    expect((date as HTMLInputElement).value).toBe(first ? proposed : `${fixture().today.slice(0, 4)}${proposed}`);
    expect(button).toBeEnabled();
    await userEvent.clear(date);
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(/Alege data de început pentru a putea importa facturile\.$/);
    expect(section.getByText("Facturi vechi, încă neîncasate")).toBeInTheDocument();
    await userEvent.type(date, "2099-01-01"); // after today: refused before any upload
    expect(button).toBeDisabled();
    await userEvent.clear(date);
    await userEvent.type(date, "2026-10-01");
    expect(button).toBeEnabled();
    const input = section.getByRole("button", { name: "Importă facturi" }).closest("section")!
      .querySelector("input[type=file]") as HTMLInputElement;
    await userEvent.upload(input, new File(["PK"], name, { type }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toBe("/web/api/history-upload?co=NORD&start=2026-10-01");
    expect(api.posts()[0].type).toBe(type);
    expect(await screen.findByText(done)).toBeInTheDocument();
  });

  it("reviews uncertain PDF fields in one table and posts the exact answer", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], history: { start: "2026-10-01", issued: 0, received: 0 } };
    const row = { sha: "a".repeat(64), name: "scan.pdf", direction: "issued", number: "TEST-9", issue_date: "2023-10-01",
      total: "119.00", currency: "RON", party_name: "Firma Exemplu SRL", party_tax_id: "77777777", reason: "unreadable" };
    let pending = true;
    const { api, p } = await page({ companies: () => json(cfg), historyReview: () => json({ rows: pending ? [row] : [] }),
      onPost: () => { pending = false; return answer(null); } });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const table = within(await screen.findByRole("region", { name: "Documente de verificat" }));
    expect(table.getByRole("link", { name: "scan.pdf" })).toHaveAttribute("href", `/web/history-document?co=NORD&sha=${row.sha}`);
    await choose(table.getByLabelText("Rolul firmei"), "received");
    await userEvent.click(table.getByRole("button", { name: "Confirmă factura" }));
    await waitFor(() => expect(api.posts()[0]).toEqual({ url: "/web/api/history-review", method: "POST", body: {
      co: "NORD", sha: row.sha, target: "NORD", direction: "received", number: "TEST-9", issue_date: "2023-10-01",
      total: "119.00", currency: "RON", party_name: "Firma Exemplu SRL", party_tax_id: "77777777",
    } }));
  });

  it("after the first import the start date is the firm's and only shown, with what is in history", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], history: { start: "2026-10-01", issued: 21, received: 1 } };
    const { api, p } = await page({ companies: () => json(cfg) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const section = within(p.getByRole("region", { name: "Facturi din alt program" }));
    expect(section.queryByLabelText("Data de început")).toBeNull();
    expect(section.getByText(/^Data de început/)).toHaveTextContent("Data de început: 01.10.2026. În istoric: 21 de facturi emise și o factură primită.");
    expect(section.getByRole("link", { name: "21 de facturi emise" })).toHaveAttribute("href", "?co=NORD&tab=vanzari");
    expect(section.getByRole("link", { name: "o factură primită" })).toHaveAttribute("href", "?co=NORD&tab=cheltuieli");
    const input = section.getByRole("button", { name: "Importă facturi" }).closest("section")!
      .querySelector("input[type=file]") as HTMLInputElement;
    await userEvent.upload(input, new File(["x"], "poza.png", { type: "image/png" }), { applyAccept: false });
    expect(await screen.findByText("Se acceptă XML, o arhivă ZIP cu facturi sau lista SmartBill de facturi neîncasate ca XLSX.")).toBeInTheDocument();
    expect(api.posts()).toHaveLength(0);
  });

  it("adds the firm's other name the engine suggests from the ANAF name, as plain text, and says it was added", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], aliases: [], alias_suggestion: "Atelier Nord" };
    const { api, p } = await page({ companies: () => json(cfg), onPost: () => answer({ key: "NORD", aliases: ["Atelier Nord"] }, "Numele a fost adăugat.") });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const names = within(p.getByRole("region", { name: "Alte nume ale firmei" }));
    expect(names.getByText("Un document este al firmei când are pe el CUI-ul ei, denumirea de la ANAF sau unul dintre aceste nume."))
      .toBeInTheDocument();
    const input = names.getByLabelText("Nume nou");
    expect(input).toHaveValue("Atelier Nord"); // prefilled from the legal name, its legal form dropped
    await userEvent.clear(input);
    await userEvent.type(input, "  Nord   (Atelier.*) ");
    await userEvent.click(names.getByRole("button", { name: "Adaugă numele" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/company-alias-add", method: "POST",
      body: { key: "NORD", alias: "Nord (Atelier.*)" } }); // sent as typed, spaces aside: the engine escapes it
    expect(await screen.findByText("Numele a fost adăugat.")).toBeInTheDocument();
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("lists the other names, removes one, and moves the focus to the field", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], aliases: ["Atelier Nord", "Nord Ateliere"], alias_suggestion: "" };
    const { api, p } = await page({ companies: () => json(cfg), onPost: () => answer({ key: "NORD", aliases: ["Atelier Nord"] }, "Numele a fost șters.") });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const names = within(p.getByRole("region", { name: "Alte nume ale firmei" }));
    expect(names.getByLabelText("Nume nou")).toHaveValue("");
    await userEvent.click(names.getByRole("button", { name: "Șterge numele Nord Ateliere" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/company-alias-remove", method: "POST",
      body: { key: "NORD", alias: "Nord Ateliere" } });
    expect(await screen.findByText("Numele a fost șters.")).toBeInTheDocument();
    await waitFor(() => expect(names.getByLabelText("Nume nou")).toHaveFocus());
  });

  it("with the last free place taken the form goes and the focus moves to the heading", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.limits = { ...cfg.limits, aliases_max: 2 };
    cfg.companies[0] = { ...cfg.companies[0], aliases: ["Atelier Nord"], alias_suggestion: "" };
    const full = { ...cfg, companies: [{ ...cfg.companies[0], aliases: ["Atelier Nord", "Nord Ateliere"] }, cfg.companies[1]] };
    const later = deferred<Response>(); // the list is read again slowly: focus waits for it
    let n = 0;
    const { p } = await page({ companies: () => (n++ ? later.promise : json(cfg)), onPost: () => answer({ key: "NORD", aliases: full.companies[0].aliases }, "Numele a fost adăugat.") });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const names = within(p.getByRole("region", { name: "Alte nume ale firmei" }));
    await userEvent.type(names.getByLabelText("Nume nou"), "Nord Ateliere{Enter}");
    expect(await screen.findByText("Numele a fost adăugat.")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 120));
    expect(names.getByLabelText("Nume nou")).toBeInTheDocument(); // still the old list
    await act(async () => later.resolve(json(full)));
    await waitFor(() => expect(names.queryByLabelText("Nume nou")).toBeNull());
    await waitFor(() => expect(p.getByRole("heading", { name: "Alte nume ale firmei" })).toHaveFocus());
  });

  it("shows the engine's refusal of a name and keeps what was typed", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], aliases: [], alias_suggestion: "" };
    const { p } = await page({ companies: () => json(cfg), onPost: () => json({ ok: false, message: "Numele aparține altei firme.", next: null, done: null, state: null }) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    const names = within(p.getByRole("region", { name: "Alte nume ale firmei" }));
    await userEvent.type(names.getByLabelText("Nume nou"), "Studio{Enter}");
    expect(await screen.findByText("Numele aparține altei firme.")).toBeInTheDocument();
    expect(names.getByLabelText("Nume nou")).toHaveValue("Studio");
  });

  it("an older engine that doesn't list other names shows no such section", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const { p } = await page({ companies: () => json(config()) });
    await userEvent.click(p.getByRole("button", { name: "Modifică NORD" }));
    expect(p.queryByRole("region", { name: "Alte nume ale firmei" })).toBeNull();
  });
});


it("keeps a cancelled or invalid replacement, and clears a pending logo when the CUI changes", async () => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:preview", revokeObjectURL: () => {} }));
  at("?co=NORD&tab=setari&sec=firme");
  const { api, p } = await page({ companies: () => json(config()), onPost: c => c.url.endsWith("company-lookup") ? answer(found) : answer({ key: "NOUA" }) });
  await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
  await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
  await p.findByRole("button", { name: "Adaugă firma" });
  const file = new File([new Uint8Array([137, 80, 78, 71])], "image.png", { type: "image/png" });
  const input = p.getByLabelText("Sigla firmei");
  await userEvent.upload(input, file);
  await p.findByAltText("Sigla aleasă");
  expect(p.getByRole("button", { name: "Caută la ANAF" })).toBeDisabled();
  await userEvent.upload(input, []);
  expect(p.getByAltText("Sigla aleasă")).toBeInTheDocument();
  await userEvent.upload(input, new File(["bad"], "bad.png", { type: "image/png" }));
  await p.findByText("Se acceptă o imagine PNG sau JPEG.");
  expect(p.getByAltText("Sigla aleasă")).toBeInTheDocument();
  await userEvent.upload(input, new File([new Uint8Array(512 * 1024 + 1)], "large.png", { type: "image/png" }));
  await p.findByText("Imaginea poate avea cel mult 512 KB.");
  await userEvent.clear(p.getByLabelText("CUI"));
  await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
  await p.findByRole("button", { name: "Adaugă firma" });
  expect(p.queryByAltText("Sigla aleasă")).toBeNull();
  await userEvent.upload(p.getByLabelText("Sigla firmei"), file);
  await p.findByAltText("Sigla aleasă");
  await userEvent.click(p.getByRole("button", { name: "Elimină sigla" }));
  await userEvent.click(p.getByRole("button", { name: "Adaugă firma" }));
  await waitFor(() => expect(api.posts().at(-1)?.url).toBe("/web/api/company-add"));
  expect(api.posts().some(c => c.url.includes("invoicing/logo?"))).toBe(false);
});

it("reads the shown firm's state again once a new firm's logo is saved, even after a switch", async () => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:preview", revokeObjectURL: () => {} }));
  at("?co=NORD&tab=setari&sec=firme");
  const sha = "c".repeat(64);
  const upload = deferred<Response>();
  let saved = false;
  const noua = { key: "NOUA", name: "Noua", legal_name: "FIRMA NOUĂ SRL", palette: "blue", late: 0, logo: sha };
  const { api, p } = await page({
    companies: () => json(config()),
    state: (url) => {
      const s = fixture(url.includes("co=STUDIO9") ? "STUDIO9" : "NORD");
      return json(saved ? { ...s, companies: [...s.companies, noua] } : s);
    },
    onPost: (c) => c.url.includes("invoicing/logo?") ? upload.promise
      : c.url.endsWith("company-lookup") ? answer(found) : answer({ key: "NOUA" }),
  });
  await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
  await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
  await p.findByRole("button", { name: "Adaugă firma" });
  await userEvent.upload(p.getByLabelText("Sigla firmei"), new File([new Uint8Array([137, 80, 78, 71])], "image.png", { type: "image/png" }));
  await p.findByAltText("Sigla aleasă");
  await userEvent.click(p.getByRole("button", { name: "Adaugă firma" }));
  await waitFor(() => expect(api.posts().at(-1)?.url).toContain("invoicing/logo?"));
  await userEvent.click(p.getByLabelText(/^NORD, Atelier Nord SRL, alege firma/));
  await userEvent.click(document.querySelector('.comenu .menu a[href^="?co=STUDIO9"]') as HTMLElement);
  await p.findByLabelText(/^STUDIO9, alege firma/);
  saved = true;
  await act(async () => upload.resolve(json({ ok: true, message: "", state: null })));
  await waitFor(() => expect(document.querySelector('.comenu a[href^="?co=NOUA"] .mk img')).toHaveAttribute("src", `/web/invoicing/logo?co=NOUA&sha=${sha}`));
  expect(window.location.search).toContain("co=STUDIO9");
});

it("blocks a repeated lookup while reading a logo and recovers when CUI changes", async () => {
  at("?co=NORD&tab=setari&sec=firme");
  const { p } = await page({ companies: () => json(config()), onPost: () => answer(found) });
  await userEvent.click(p.getByRole("button", { name: "Adaugă o firmă" }));
  await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
  await p.findByRole("button", { name: "Adaugă firma" });
  const slow = deferred<ArrayBuffer>();
  const file = new File(["pending"], "image.png", { type: "image/png" });
  vi.spyOn(file, "slice").mockReturnValue({ arrayBuffer: () => slow.promise } as Blob);
  await userEvent.upload(p.getByLabelText("Sigla firmei"), file);
  expect(p.getByRole("button", { name: "Caută la ANAF" })).toBeDisabled();
  expect(p.getByRole("button", { name: "Adaugă firma" })).toBeDisabled();
  await userEvent.clear(p.getByLabelText("CUI"));
  await userEvent.type(p.getByLabelText("CUI"), "33333330{Enter}");
  expect(await p.findByRole("button", { name: "Adaugă firma" })).toBeEnabled();
  await act(async () => slow.resolve(new Uint8Array([137, 80, 78, 71]).buffer));
  expect(p.queryByAltText("Sigla aleasă")).toBeNull();
});
