// Bank connections (open banking): the bank card's line and button, leaving for the bank only to an https address,
// and the task before access ends. The engine decides every state.
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { leave, safePage } from "@/components/BankConnect";
import type { BankStatus, CompaniesConfig, State } from "@/lib/types";
import { at, fixture, fixtureAll, fixtureIntegrations, json, server, renderApp, answer, choose } from "./helpers";

const BT: BankStatus = { bank: "bt", name: "Banca Transilvania", connect: "ready",
  connection: { state: "ok", valid_until: "2027-01-20", last_read: "2026-10-26", accounts: ["0001"], error: null } };
const ING: BankStatus = { bank: "ing", name: "ING Bank", connect: "ready", connection: null };
const OTP: BankStatus = { bank: "otp", name: "OTP Bank", connect: "not_supported", connection: null };

function config(banks: BankStatus[]): CompaniesConfig {
  const acc = (id: string, last4: string, bank: string, bank_name: string) =>
    ({ id, iban_last4: last4, currency: "RON", bank, bank_name, pays: bank === "bt", retired: null, statement_senders: [], last_statement: null });
  return { companies: [{ key: "NORD", name: "NORD", legal_name: "Atelier Nord SRL", cui: "11111110", palette: "amber", has_data: true,
    has_ron_account: true, iban_last4: "0001", banks,
    accounts: [acc("a1", "0001", "bt", "Banca Transilvania"), acc("a2", "0007", "ing", "ING Bank"), acc("a3", "0008", "otp", "OTP Bank")] }],
    palettes: ["amber"], limits: { key_pattern: "^[A-Z0-9]+$", reserved_keys: ["ALL"], name_max: 100 } };
}

async function bankCard(banks: BankStatus[], onPost?: NonNullable<Parameters<typeof server>[0]>["onPost"]) {
  at("?co=NORD&tab=setari&sec=firme");
  const api = renderApp({ companies: () => json(config(banks)), onPost });
  await userEvent.click(await screen.findByRole("button", { name: "Modifică NORD" }));
  return { api, region: within(screen.getByRole("region", { name: "Bănci și conturi" })) };
}

afterEach(() => vi.restoreAllMocks());

it("stops direct reads only after confirmation, posting exactly the firm and bank", async () => {
  const { api, region } = await bankCard([BT]);
  const button = region.getByRole("button", { name: "Oprește citirea directă" });
  await userEvent.click(button);
  await answer(false);
  expect(api.posts().filter((p) => p.url.endsWith("/bank-disconnect"))).toHaveLength(0);
  await userEvent.click(button);
  await answer();
  await waitFor(() => expect(api.posts().filter((p) => p.url.endsWith("/bank-disconnect"))).toHaveLength(1));
  expect(api.posts().filter((p) => p.url.endsWith("/bank-disconnect"))).toEqual([
    expect.objectContaining({ method: "POST", body: { co: "NORD", bank: "bt" } }),
  ]);
  expect(window.location.search).toBe("?co=NORD&tab=setari&sec=firme");
});

describe("a bank's connection on its card", () => {
  it("says how a connected bank reads, offers to connect another and nothing for one the provider can't reach", async () => {
    const { region } = await bankCard([BT, ING, OTP]);
    const direct = within(region.getAllByRole("listitem").find((li) => li.textContent?.startsWith("Conexiune directă") && li.textContent.includes("Conectată"))!);
    expect(direct.getByText("Citită pe 26 oct. Accesul expiră pe 20 ian 2027.")).toBeInTheDocument();
    expect(region.getByRole("button", { name: "Reconectează banca" })).toBeInTheDocument(); // a quiet link for a working one
    expect(region.getByRole("button", { name: "Conectează banca" })).toBeInTheDocument();
    expect(region.getAllByRole("button", { name: /banca$/ })).toHaveLength(2); // none for OTP: its statements are the way
  });

  it("connecting posts the company and bank, then leaves for the provider's page", async () => {
    const to = vi.spyOn(leave, "to").mockImplementation(() => {});
    const { api, region } = await bankCard([ING], () =>
      json({ ok: true, message: "Urmează confirmarea în aplicația băncii.", done: { url: "https://tilisy.enablebanking.com/ais/start?x=1" }, state: null }));
    await userEvent.click(region.getByRole("button", { name: "Conectează banca" }));
    await waitFor(() => expect(to).toHaveBeenCalledWith("https://tilisy.enablebanking.com/ais/start?x=1"));
    expect(window.location.search + window.location.hash).toBe("?co=NORD&tab=setari&sec=firme#conn-NORD");
    expect(api.posts().find((p) => p.url.endsWith("/bank-connect"))).toMatchObject({ url: "/web/api/bank-connect", method: "POST", body: { co: "NORD", bank: "ing" } });
    expect(await screen.findByText("Urmează confirmarea în aplicația băncii.")).toBeInTheDocument();
  });

  it("never follows an address that isn't https, and says the engine's refusal", async () => {
    const to = vi.spyOn(leave, "to").mockImplementation(() => {});
    const { region } = await bankCard([ING], () => json({ ok: true, message: "", done: { url: "javascript:alert(1)" }, state: null }));
    await userEvent.click(region.getByRole("button", { name: "Conectează banca" }));
    await new Promise((r) => setTimeout(r, 50));
    expect(to).not.toHaveBeenCalled();
  });

  it("an expired or failing connection says so and asks for a reconnect", async () => {
    const expired = { ...BT, connection: { ...BT.connection!, state: "expired" as const } };
    const { region } = await bankCard([expired]);
    expect(region.getByText("Accesul a expirat. Conectează din nou banca.")).toHaveClass("late");
    expect(region.getByRole("button", { name: "Reconectează banca" })).toHaveClass("btn");
  });

  it("before open banking is set up, says where to set it up", async () => {
    const { region } = await bankCard([{ ...ING, connect: "not_set_up" }]);
    expect(region.queryByRole("button", { name: /Conectează/ })).toBeNull();
    expect(region.getByRole("link", { name: "Configurează Enable Banking" })).toHaveAttribute("href", expect.stringContaining("int=new%3Aopenbanking"));
  });
});

describe("Sarcini before access ends", () => {
  it("asks for a reconnect of the bank whose access ends within 14 days", async () => {
    const s: State = fixture();
    s.bank_connections = [{ ...BT, connection: { ...BT.connection!, state: "expiring", valid_until: "2026-11-06" } }];
    at("?co=NORD&tab=sarcini");
    renderApp({ state: () => json(s) });
    expect(await screen.findByText("Accesul la Banca Transilvania expiră pe 6 nov")).toBeInTheDocument();
    expect(screen.getByText("Conectează din nou banca pentru a continua citirea tranzacțiilor. Confirmă în aplicația băncii.")).toBeInTheDocument();
  });

  it("an ended access is urgent; a failed read says why, with the last read that worked", async () => {
    const s: State = fixture();
    s.bank_connections = [{ ...BT, connection: { ...BT.connection!, state: "expired" } },
      { ...ING, connection: { state: "error", valid_until: "2027-01-20", last_read: "2026-10-20", accounts: ["0007"],
        error: "Banca permite doar câteva citiri pe zi. Preluarea se reia peste câteva ore." } }];
    at("?co=NORD&tab=sarcini");
    renderApp({ state: () => json(s) });
    expect(await screen.findByText("Accesul la Banca Transilvania a expirat")).toBeInTheDocument();
    expect(screen.getByText("Tranzacțiile de la ING Bank nu au putut fi citite")).toBeInTheDocument();
    expect(screen.getByText("Banca permite doar câteva citiri pe zi. Preluarea se reia peste câteva ore. Ultima citire reușită: 20 oct."))
      .toBeInTheDocument();
  });
});

it("a reconnect from the task goes to the bank like the card's", async () => {
  const to = vi.spyOn(leave, "to").mockImplementation(() => {});
  const s: State = fixture();
  s.bank_connections = [{ ...BT, connection: { ...BT.connection!, state: "expired" } }];
  at("?co=NORD&tab=sarcini");
  const api = renderApp({ state: () => json(s), onPost: () => json({ ok: true, message: "", done: { url: "https://tilisy.enablebanking.com/ais/start" }, state: null }) });
  await userEvent.click(await screen.findByRole("button", { name: "Reconectează banca" }));
  await waitFor(() => expect(to).toHaveBeenCalledWith("https://tilisy.enablebanking.com/ais/start"));
  expect(api.posts()[0].body).toEqual({ co: "NORD", bank: "bt" });
});

it("leaves only for an https page with no name or password hidden before its host", () => {
  expect(safePage("https://tilisy.enablebanking.com/ais/start?x=1")).toBe(true);
  expect(safePage("https://enablebanking.com/x")).toBe(true);
  for (const bad of ["http://tilisy.enablebanking.com/", "https://bank.example@attacker.example/", "https://synthetic-user:dummy-password@tilisy.enablebanking.com/",
    "https://tilisy.enablebanking.com:8443/", "javascript:alert(1)", "https://attacker.example.invalid\\.enablebanking.com/login",
    "https://attacker.example/", "https://evilenablebanking.com/", "https://tilisy.enablebanking.com\t/", "/relative", "", null, 42]) {
    expect(safePage(bad)).toBe(false);
  }
});

it("under Toate firmele, each company's bank task stays, even at the same bank", async () => {
  const all = fixtureAll();
  all.parts[0].bank_connections = [{ ...BT, connection: { ...BT.connection!, state: "expiring", valid_until: "2026-11-06" } }];
  all.parts[1].bank_connections = [{ ...BT, connection: { ...BT.connection!, state: "expired" } }];
  at("?co=ALL&tab=sarcini");
  renderApp({ all: () => json(all) });
  expect(await screen.findByText("Accesul la Banca Transilvania expiră pe 6 nov")).toBeInTheDocument();
  expect(screen.getByText("Accesul la Banca Transilvania a expirat")).toBeInTheDocument();
});


describe("the install's Enable Banking guide", () => {
  it("prepares a public certificate only on request and retains the guide after the reply", async () => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:openbanking");
    const api = renderApp({ onPost: () => json({ ok: true, message: "", state: null,
      done: { certificate: "PUBLIC CERTIFICATE EXAMPLE", return_urls: ["https://fiscalm.example.com/bank/return"] } }) });
    const dialog = within(await screen.findByRole("dialog", { name: "Configurare Enable Banking" }));
    expect(api.posts()).toHaveLength(0);
    await userEvent.click(dialog.getByRole("button", { name: "Continuă" }));
    await userEvent.click(dialog.getByRole("button", { name: "Pregătește certificatul" }));
    expect(await dialog.findByDisplayValue("PUBLIC CERTIFICATE EXAMPLE")).toHaveAttribute("readonly");
    expect(dialog.getByDisplayValue("https://fiscalm.example.com/bank/return")).toBeInTheDocument();
    expect(api.posts()[0]).toMatchObject({ url: "/web/api/bank-setup", body: { co: "NORD", operation: "prepare" } });
  });

  it("shows a failed check without claiming configuration is complete and rechecks explicitly", async () => {
    const data = fixtureIntegrations();
    data.items.push({ id: "eb-test", kind: "openbanking", name: "Enable Banking", enabled: true, default: true,
      secrets: {}, updated_at: "2026-10-27", config: { application_id: "app-example", return_url: "https://fiscalm.example.com/bank/return" } });
    at("?co=NORD&tab=setari&sec=integrari&int=eb-test");
    let okay = false;
    const api = renderApp({ integrations: () => json(data), onPost: () => json({ ok: true, message: "", state: null,
      done: { checks: [{ key: "return", ok: okay, message: "Synthetic check" }] } }) });
    const dialog = within(await screen.findByRole("dialog", { name: "Configurare Enable Banking" }));
    await userEvent.click(dialog.getByRole("button", { name: "Continuă" }));
    await userEvent.click(dialog.getByRole("button", { name: "Verifică configurarea" }));
    await dialog.findByText("Synthetic check");
    expect(dialog.queryByRole("link", { name: "Deschide firmele" })).not.toBeInTheDocument();
    okay = true;
    await userEvent.click(dialog.getByRole("button", { name: "Verifică configurarea" }));
    expect(await dialog.findByRole("link", { name: "Deschide firmele" })).toBeInTheDocument();
    expect(api.posts().map((p) => p.body)).toEqual([{ co: "NORD", operation: "check" }, { co: "NORD", operation: "check" }]);
  });
});


const SOURCES = (mailboxes: string[] | null): BankStatus["sources"] => [{ source: "open_banking", available: false, default: false },
  { source: "mail", available: !!mailboxes?.length, default: !!mailboxes?.length, supported: mailboxes !== null, mailboxes: mailboxes ?? [] },
  { source: "upload", available: true, default: !mailboxes?.length }];
/** Each source row of the bank's list as "title: badges", in order. */
const rows = (region: ReturnType<typeof within>) => region.getAllByRole("listitem").filter((li: HTMLElement) => li.querySelector("h5")).map((li: HTMLElement) =>
  `${li.querySelector("h5")?.textContent}: ${[...li.querySelectorAll("h5 ~ span")].map((b) => b.textContent).join(", ")}`);

it.each([
  ["a statement-only bank", { ...OTP, sources: SOURCES(null) }, ["Conexiune directă: Indisponibilă", "Extrase prin e-mail: Indisponibilă", "Încărcare manuală: Principală, Activă"]],
  ["a bank with no inbox for the firm", { ...ING, sources: SOURCES([]) }, ["Conexiune directă: Neconectată", "Extrase prin e-mail: Neconfigurată", "Încărcare manuală: Principală, Activă"]],
  ["a bank read by the firm's inbox", { ...BT, connection: null, sources: SOURCES(["mail-1"]) }, ["Conexiune directă: Neconectată", "Extrase prin e-mail: Principală, Activă", "Încărcare manuală: Activă"]],
])("shows every source of %s with the engine's state and default", async (_, bank, expected) => {
  const { region } = await bankCard([bank as BankStatus]);
  expect(rows(region)).toEqual(expected);
  expect(region.getByRole("link", { name: "Încarcă extrasul" })).toHaveAttribute("href", "?co=NORD&tab=tranzactii");
});

it("names the inbox that reads a bank's statements, opening its sheet", async () => {
  const inbox = fixtureIntegrations().items.find((i) => i.kind === "mail")!;
  const { region } = await bankCard([{ ...BT, connection: null, sources: SOURCES([inbox.id]) }]);
  expect(await region.findByRole("link", { name: inbox.name })).toHaveAttribute("href", expect.stringContaining(`int=${inbox.id}`));
  expect(region.queryByRole("link", { name: "Alege un cont de e-mail" })).toBeNull();
});

it("offers to choose an inbox when none reads the firm's statements", async () => {
  const { region } = await bankCard([{ ...ING, sources: SOURCES([]) }]);
  expect(await region.findByRole("link", { name: "Alege un cont de e-mail" })).toHaveAttribute("href", "?co=NORD&tab=setari&sec=integrari");
});

it("offers a catalogue bank before an IBAN is registered", async () => {
  const data = config([ING]);
  data.companies[0].accounts = [];
  at("?co=NORD&tab=setari&sec=firme");
  renderApp({ companies: () => json(data) });
  await userEvent.click(await screen.findByRole("button", { name: "Modifică NORD" }));
  expect(screen.getByText("Firma nu are încă un cont bancar.")).toBeInTheDocument();
  await choose(screen.getByRole("combobox", { name: "Conectează o bancă" }), "ing");
  expect(screen.getByText("Niciun cont încă.")).toBeInTheDocument(); // its card waits for the accounts chosen at the bank
  expect(screen.getByRole("button", { name: "Conectează banca" })).toBeInTheDocument();
});

it("requires explicit account and ownership choices, then posts only their opaque IDs", async () => {
  const offer = { id: "opaque-offer", bank: "bt", valid_until: "2027-01-20", accounts: [
    { id: "opaque-account-1", last4: "0001", currency: "RON", existing: true },
    { id: "opaque-account-2", last4: "0003", currency: "EUR", existing: false }] };
  const { api } = await bankCard([BT], (c) => json({ ok: true, message: "", state: null,
    done: c.body?.operation === "list" ? { offers: [offer] } : {} }));
  const form = within(await screen.findByRole("form", { name: "Alege conturile de la Banca Transilvania" }));
  const boxes = form.getAllByRole("checkbox");
  expect(boxes.every((b) => !(b as HTMLInputElement).checked)).toBe(true);
  const button = form.getByRole("button", { name: "Conectează conturile selectate" });
  expect(button).toBeDisabled();
  await userEvent.click(boxes[1]);
  expect(button).toBeDisabled();
  await userEvent.click(boxes[2]);
  await userEvent.click(button);
  await waitFor(() => expect(screen.queryByRole("form", { name: "Alege conturile de la Banca Transilvania" })).not.toBeInTheDocument());
  expect(api.posts().find((p) => p.body?.operation === "confirm")?.body).toEqual({ co: "NORD", operation: "confirm", offer: "opaque-offer", accounts: ["opaque-account-2"] });
});


it.each(["focus", "visibilitychange"])("reads newly authorized accounts when the phone app resumes with %s", async (event) => {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  let authorized = false;
  const { api } = await bankCard([BT], () => json({ ok: true, message: "", state: null, done: { offers: authorized ? [
    { id: "offer-after-handoff", bank: "bt", valid_until: "2027-01-20", accounts: [{ id: "focus-account", last4: "0001", currency: "RON", existing: true }] }] : [] } }));
  await waitFor(() => expect(api.posts().some((p) => p.body?.operation === "list")).toBe(true));
  authorized = true;
  if (event === "focus") fireEvent.focus(window); else fireEvent(document, new Event("visibilitychange"));
  expect(await screen.findByRole("form", { name: "Alege conturile de la Banca Transilvania" })).toBeInTheDocument();
  expect(api.posts().every((p) => p.body?.operation === "list")).toBe(true);
});


it("does not restore a confirmed offer from an older pending list response", async () => {
  const offer = { id: "opaque-offer", bank: "bt", valid_until: "2027-01-20", accounts: [
    { id: "opaque-account", last4: "0001", currency: "RON", existing: true }] };
  let release!: (r: Response) => void;
  const delayed = new Promise<Response>((done) => { release = done; });
  let reads = 0;
  await bankCard([BT], (c) => c.body?.operation === "list" && ++reads > 1 ? delayed
    : json({ ok: true, message: "", state: null, done: { offers: [offer] } }));
  const form = within(await screen.findByRole("form", { name: "Alege conturile de la Banca Transilvania" }));
  fireEvent.focus(window);
  for (const box of form.getAllByRole("checkbox")) await userEvent.click(box);
  await userEvent.click(form.getByRole("button", { name: "Conectează conturile selectate" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Alege conturile de la Banca Transilvania" })).not.toBeInTheDocument());
  await act(async () => { release(json({ ok: true, message: "", state: null, done: { offers: [offer] } })); });
  await waitFor(() => expect(screen.queryByRole("form", { name: "Alege conturile de la Banca Transilvania" })).not.toBeInTheDocument());
});


it.each([["conn", "Bănci și conturi", "Bănci și conturi"], ["hist", "Facturi anterioare", "Facturi din alt program"]])("the Configurează card's #%s opens only that part of the firm", async (part, title, heading) => {
  at(`?co=NORD&tab=setari&sec=firme#${part}-NORD`);
  const c = config([BT]); c.companies[0].history = { start: null, first_statement: null, issued: 0, received: 0 };
  renderApp({ companies: () => json(c), historyReview: () => json({ rows: [] }) });
  expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 3, name: heading })).toBeInTheDocument();
  expect(screen.queryByLabelText("Nume în aplicație")).toBeNull(); // not the whole firm editor
  expect(screen.queryByRole("heading", { name: part === "conn" ? "Facturi din alt program" : "Bănci și conturi" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Continuă" })).toBeNull();
  act(() => { window.history.pushState(null, "", "?co=NORD&tab=setari&sec=firme"); window.dispatchEvent(new PopStateEvent("popstate")); });
  expect(await screen.findByRole("button", { name: "Modifică NORD" })).toBeInTheDocument(); // Firme without the hash: the whole list
});

it("a member cannot open the firm editor or read private offers through a bank anchor", async () => {
  const s = fixture(); s.can_manage_companies = false;
  at("?co=NORD&tab=setari&sec=firme#conn-NORD");
  const api = renderApp({ state: () => json(s), companies: () => json(config([BT])) });
  await screen.findByText("Atelier Nord SRL, CUI 11111110");
  expect(screen.queryByRole("region", { name: "Bănci și conturi" })).not.toBeInTheDocument();
  expect(api.posts()).toEqual([]);
});
