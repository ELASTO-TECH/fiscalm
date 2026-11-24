// Tranzacții against a fake engine: the list opens on De explicat, then Toate, Intrări and Ieșiri, with search; every
// explanation and proposal is the engine's (`transactions`) and only named here. Beside the list, the accounts (api/companies);
// beside the title, Încarcă: a statement or a settlement report.
import { act, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render, choose } from "./helpers";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import { fixtureBrowseData } from "@/lib/fixtures";
import type { State } from "@/lib/types";
import { companiesConfig as config, at, browseResult, deferred, fixture as scenario, json, server as fake, type Call } from "./helpers";

/** The scenario's state without Acasă's six months of account history (tests/synthetic_world.py HISTORY, all De explicat):
 * the lines these tests name, counted as the engine counts them. */
function fixture(co: "NORD" | "STUDIO9" = "NORD"): State {
  const s = scenario(co);
  const rows = s.transactions.rows.filter((t) => !(t.counterparty ?? "").includes("ISTORIC"));
  return { ...s, transactions: { ...s.transactions, rows, to_explain: rows.filter((t) => !t.explanation).length } };
}
const server = (o: Parameters<typeof fake>[0] = {}) =>
  fake({ state: (url: string) => json(fixture(url.includes("STUDIO9") ? "STUDIO9" : "NORD")), ...o });

function renderTransactions(options: Parameters<typeof server>[0] = {}) {
  const api = server(options);
  render(<App />);
  return api;
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

const answer = (done: object | null, message = "") => json({ ok: true, message, next: null, done, state: fixture() });

async function page(options: Parameters<typeof renderTransactions>[0] = {}) {
  const api = renderTransactions(options);
  await screen.findByRole("region", { name: "Conturi" });
  return { api, p: within(document.body) };
}

/** Încarcă, then one of its two choices; the statement's file input sits next to its button. */
async function openUpload(choice: "Încarcă extras" | "Încarcă raport de decontare") {
  await userEvent.click(screen.getByRole("button", { name: "Încarcă" }));
  const item = within(screen.getByRole("dialog", { name: "Încarcă" })).getByRole("button", { name: choice });
  if (choice === "Încarcă raport de decontare") {
    await userEvent.click(item);
    return void expect(screen.getByRole("heading", { name: choice })).toHaveFocus(); // focus stays in the sheet
  }
  return item.parentElement!.querySelector("input[type=file]") as HTMLInputElement;
}

/** The list's rows as the user reads them: name, line under it, amount, line under the amount; and where each leads. */
const list = () => screen.getByRole("navigation", { name: "Filtru" }).parentElement!.querySelector(".card") as HTMLElement;
async function rows() {
  const card = (await screen.findByRole("navigation", { name: "Filtru" })).parentElement!.querySelector(".card") as HTMLElement;
  await waitFor(() => expect(card).toHaveAttribute("aria-busy", "false"));
  return [...card.querySelectorAll(".row")].map((r) => [r.querySelector(".t b")?.textContent, r.querySelector(".t small")?.textContent,
    r.querySelector(".v")?.firstChild?.textContent, r.querySelector(".v small")?.textContent ?? "", r.getAttribute("href")]);
}

/** A row De explicat opens its own sheet (`tx`), where it is explained. */
const TX = expect.stringMatching(/^\?co=NORD&tab=tranzactii&tx=[0-9a-f]{20}$/);
const DE_EXPLICAT = [
  ["POPESCU ANA", "De explicat, 24 oct", "+500,00 lei", "cont 0001", TX],
  ["Comision administrare cont", "De explicat, 23 oct", "−15,00 lei", "cont 0001", TX],
  ["NIMBUS HOSTING INC", "De explicat, 21 oct", "−173,45 lei", "37,00 USD, cont 0001", TX],
  ["FIGMA INC", "De explicat, poate fi bonul de la Figma, 6 oct", "−74,70 lei", "16,00 USD, cont 0001", TX],
];

it.each(["NORD", "STUDIO9"] as const)("keeps API text/search/filter pages equal to the original selector for %s", (co) => {
  const full = fixture(co);
  const queries = [{}, { f: "all" }, { f: "in" }, { f: "out" }, { f: "all", q: "dobanda" }, { f: "all", q: "2.400,00" },
    { f: "all", q: "plata facturii" }, { f: "all", q: "cs-0920" }, { f: "all", month: "2026-09" },
    { f: "all", from: "2026-10-20", to: "2026-10-24" }, { f: "all", cursor: "2026-10-24:0" }];
  for (const query of queries) expect(fixtureBrowseData(full, "tranzactii", query)).toEqual(browseResult(full, "tranzactii", query));
});

it("lists a debit waiting for its invoice last in De explicat, as the engine pages it, under its own words", async () => {
  const full = fixture(), nimbus = full.transactions.rows.find((t) => t.counterparty === "NIMBUS HOSTING INC")!;
  nimbus.awaiting = { note: "", by: "admin", at: "2026-10-26T09:30:00" };
  for (const query of [{}, { cursor: ":0" }, { cursor: "2026-10-06:99" }, { f: "all" }])
    expect(fixtureBrowseData(full, "tranzactii", query)).toEqual(browseResult(full, "tranzactii", query));
  at("?co=NORD&tab=tranzactii");
  renderTransactions({ state: () => json(full) });
  expect(await rows()).toEqual([DE_EXPLICAT[0], DE_EXPLICAT[1], DE_EXPLICAT[3],
    ["NIMBUS HOSTING INC", "Aștept factura, 21 oct", "−173,45 lei", "37,00 USD, cont 0001", TX]]);
  expect(screen.getByRole("link", { name: "De explicat 4" })).toBeInTheDocument(); // still to explain
});

it("pages and searches complete explanations without root transactions or invoices, retaining URLs and focus", async () => {
  const full = fixture(), invoice = full.invoices[0], tx = full.transactions.rows.find((t) => t.direction === "debit")!;
  full.invoices = Array.from({ length: 151 }, (_, i) => ({ ...invoice, id: `read-${i}`, number: `SYN${i}` }));
  full.transactions.rows = full.invoices.map((r, i) => ({ ...tx, id: i.toString(16).padStart(20, "0"), date: "2026-10-24", amount: "12.34", orig: null,
    counterparty: `Partener sintetic ${i}`, explanation: { kind: "payment" as const, invoices: [r.id], payments: [] }, proposal: null }));
  full.transactions.to_explain = 0;
  const compact = structuredClone(full);
  compact.transactions.rows = []; compact.invoices = []; compact.issued = []; compact.documents = [];
  at("?co=NORD&tab=tranzactii&f=all");
  const api = renderTransactions({ state: () => json(compact), browse: (url) => {
    const query = Object.fromEntries(new URL(url, "http://local").searchParams);
    return json(fixtureBrowseData(full, "tranzactii", query));
  } });
  expect(await rows()).toHaveLength(50);
  const next = screen.getByRole("button", { name: "Pagina următoare" });
  for (let i = 0; i < 3; i++) { await userEvent.click(next); await rows(); }
  expect(await rows()).toEqual([["Partener sintetic 150", "Plata facturii SYN150, 24 oct", "−12,34 lei", "cont 0001", "?co=NORD&tab=tranzactii&inv=read-150&f=all"]]);
  expect(next).toHaveFocus(); expect(next).toHaveAttribute("aria-disabled", "true");
  await userEvent.type(screen.getByRole("searchbox", { name: "Caută nume, sumă sau factură" }), "SYN150");
  expect(await rows()).toHaveLength(1);
  expect(api.calls.filter((c) => c.url.includes("api/browse")).at(-1)?.url).toContain("q=syn150");
});

describe("Tranzacții", () => {
  it("opens on De explicat, counted on its filter and in the column", async () => {
    at("?co=NORD&tab=tranzactii");
    renderTransactions();
    expect(await rows()).toEqual(DE_EXPLICAT);
    const filters = within(screen.getByRole("navigation", { name: "Filtru" }));
    expect(filters.getAllByRole("link").map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["De explicat 4", "?co=NORD&tab=tranzactii"], ["Toate", "?co=NORD&tab=tranzactii&f=all"],
      ["Intrări", "?co=NORD&tab=tranzactii&f=in"], ["Ieșiri", "?co=NORD&tab=tranzactii&f=out"]]);
    expect(filters.getByRole("link", { name: "De explicat 4" })).toHaveAttribute("aria-current", "page");
  });

  it("names every explanation the engine gives, each opening what explains it", async () => {
    at("?co=NORD&tab=tranzactii&f=all");
    renderTransactions();
    expect((await rows()).slice(5)).toEqual([
      ["CLIENT DELTA SRL", "Încasarea facturii NRD 0040, 15 sep", "+3.000,00 lei", "cont 0001", "?co=NORD&tab=tranzactii&iss=5004&f=all"],
      ["BUGETUL GENERAL CONSOLIDAT", "Plată din fișierul de plăți, salarii, 10 sep", "−940,00 lei", "cont 0001", "?co=NORD&tab=plati"],
      ["CHIRIE SPATIU SRL", "Plata facturii CS-0920, 9 sep", "−2.400,00 lei", "cont 0001", "?co=NORD&tab=tranzactii&inv=111&f=all"],
      ["HETZNER ONLINE GMBH", "Bon de la Hetzner, 3 sep", "−44,80 lei", "9,00 EUR, cont 0001",
        "?co=NORD&tab=tranzactii&doc=sha-doc-NORD-2026-09-receipt-Hetzner0000&f=all"],
    ]);
  });

  it("splits money in and out, opens a link's filter and month, and searches by name, amount or invoice", async () => {
    at("?co=NORD&tab=tranzactii&f=in&month=2026-09");
    renderTransactions();
    expect((await rows()).map((r) => r[0])).toEqual(["CLIENT DELTA SRL"]);
    expect(screen.getByRole("combobox", { name: "Luna" })).toHaveValue("2026-09");
    await choose(screen.getByRole("combobox", { name: "Luna" }), "");
    expect(window.location.search).toBe("?co=NORD&tab=tranzactii&f=in");
    expect((await rows()).map((r) => r[0])).toEqual(["POPESCU ANA", "CLIENT DELTA SRL"]);
    await userEvent.click(screen.getByRole("link", { name: "Ieșiri" }));
    expect((await rows())).toHaveLength(7);
    const q = screen.getByRole("searchbox", { name: "Caută nume, sumă sau factură" });
    await userEvent.type(q, "44,80");
    expect((await rows()).map((r) => r[0])).toEqual(["HETZNER ONLINE GMBH"]);
    await userEvent.clear(q);
    await userEvent.type(q, "2400,00"); // without the thousands dot, as it is often typed
    expect((await rows()).map((r) => r[0])).toEqual(["CHIRIE SPATIU SRL"]);
    await userEvent.clear(q);
    await userEvent.type(q, "37,00"); // the card's own amount, in dollars
    expect((await rows()).map((r) => r[0])).toEqual(["NIMBUS HOSTING INC"]);
    await userEvent.clear(q);
    await userEvent.type(q, "cs-0920");
    expect((await rows()).map((r) => r[0])).toEqual(["CHIRIE SPATIU SRL"]);
    await userEvent.clear(q);
    await userEvent.type(q, "nimbus");
    expect((await rows()).map((r) => r[0])).toEqual(["NIMBUS HOSTING INC"]);
    await userEvent.type(q, " nimic");
    expect(await screen.findByText("Niciun element")).toBeInTheDocument();
  });

  it("keeps a month asked for that has no transactions, and says so", async () => {
    at("?co=NORD&tab=tranzactii&f=all&month=2026-08");
    renderTransactions();
    await screen.findByRole("navigation", { name: "Filtru" });
    expect(screen.getByRole("combobox", { name: "Luna" })).toHaveValue("2026-08");
    expect(await rows()).toEqual([]);
    expect(list()).toHaveTextContent("Niciun element");
  });

  it.each([true, false])("a settlement proposal opens the editable credit only when linkable: %s", async (canLink) => {
    const s = fixture();
    const [waits, paid] = s.transactions.rows.filter((t) => t.direction === "credit"); // newest first
    paid.explanation = { kind: "collection", issued: ["5004", "5002"], settlement: "NORD:set1" };
    waits.explanation = null;
    waits.can_link = canLink;
    waits.proposal = { kind: "settlement", settlement: "NORD:set2" };
    s.settlements = [{ id: "NORD:set1", kind: "settlement", company: "NORD", source: "csv", report_key: "R1", reference: "R1", candidate: null,
      payer: { id: "fan", name: "Curier Exemplu" }, date: "2026-09-14", currency: "RON", gross: "3010.00", fee: "10.00",
      net: "3000.00", credit: null, invoice_count: 2, confirmed_count: 3, auto_after: 3, can_confirm: false, human_confirmed: true,
      status: "collected", error: null, failed_line: null, tx_key: paid.id, sheet: { co: "NORD", set: "NORD:set1" },
      links: { transaction: paid.id, issued: ["5004", "5002"] }, lines: [] }];
    at("?co=NORD&tab=tranzactii&f=in");
    renderTransactions({ state: () => json(s) });
    expect((await rows()).map((r) => [r[0], r[1], r[4]])).toEqual([
      ["POPESCU ANA", "De explicat, decontare de verificat, 24 oct", canLink ? `?co=NORD&tab=tranzactii&tx=${waits.id}&f=in` : "?co=NORD&tab=tranzactii&set=NORD%3Aset2&f=in"],
      ["CLIENT DELTA SRL", "Decontare Curier Exemplu, 2 facturi, 15 sep", "?co=NORD&tab=tranzactii&set=NORD%3Aset1&f=in"],
    ]);
  });

  it("a debit that fits several invoices proposes them and leads, from its sheet, to Sarcini, where they are chosen", async () => {
    at("?co=STUDIO9&tab=tranzactii");
    renderTransactions({ state: () => json(fixture("STUDIO9")) });
    expect(await rows()).toEqual([["CONTABIL EXPERT SRL", "De explicat, se potrivește cu 2 facturi, 25 oct", "−350,00 lei", "cont 0002",
      expect.stringMatching(/^\?co=STUDIO9&tab=tranzactii&tx=[0-9a-f]{20}$/)]]);
    await userEvent.click(screen.getByRole("link", { name: /CONTABIL EXPERT SRL/ }));
    const sheet = within(await screen.findByRole("dialog", { busy: false }));
    expect(sheet.getByRole("link", { name: "Se potrivește cu 2 facturi" })).toHaveAttribute("href", "?co=STUDIO9&tab=sarcini");
  });

  it("says when every transaction is explained, and when there are none yet", async () => {
    const explained = fixture();
    explained.transactions = { ...explained.transactions, rows: explained.transactions.rows.filter((t) => t.explanation), to_explain: 0 };
    at("?co=NORD&tab=tranzactii");
    server({ state: () => json(explained) });
    const { unmount } = render(<App />);
    // Everything explained is good news: the shared empty state carries the check icon; no statements yet does not.
    await waitFor(() => expect(list().querySelector(".empty svg")).not.toBeNull());
    expect(within(screen.getByRole("navigation", { name: "Filtru" })).getByRole("link", { name: "De explicat" })).toBeInTheDocument();
    unmount();
    const none: State = { ...fixture(), transactions: { ...fixture().transactions, rows: [], to_explain: 0 } };
    renderTransactions({ state: () => json(none) });
    await waitFor(() => expect(list().querySelector(".empty")).toHaveTextContent("Niciun element"));
    expect(list().querySelector(".empty svg")).toBeNull();
  });

  it("says when the accounts can't be read, and reads them again on Reîncearcă", async () => {
    at("?co=NORD&tab=tranzactii");
    let fail = true;
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], accounts: [
      { id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: null }] };
    renderTransactions({ companies: () => (fail ? new Response("", { status: 500 }) : json(cfg)) });
    expect(await screen.findByText("Conturile nu au putut fi încărcate.")).toBeInTheDocument();
    expect(await rows()).toEqual(DE_EXPLICAT); // the list doesn't wait on them
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
    expect(await screen.findByRole("region", { name: "Conturi" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the accounts to everyone, Încarcă and a new account only to an administrator", async () => {
    at("?co=NORD&tab=tranzactii");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], banks: [{ bank: "bt", name: "Banca Transilvania", connect: "ready",
      connection: { state: "expiring", valid_until: "2026-11-06", last_read: "2026-10-26", accounts: ["0001"], error: null } }], accounts: [
      { id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: "2026-10" }] };
    const { p } = await page({ companies: () => json(cfg), state: () => json({ ...fixture(), can_manage_companies: false }) });
    const accounts = within(p.getByRole("region", { name: "Conturi" }));
    expect(accounts.getByText("Banca Transilvania, pentru plăți, citit pe 26 oct")).toBeInTheDocument();
    expect(accounts.getByText("Banca Transilvania: accesul expiră pe 6 nov")).toBeInTheDocument();
    expect(accounts.queryByRole("button")).toBeNull(); // no Reconectează
    expect(accounts.queryByRole("link", { name: "Bănci și conturi" })).toBeNull(); // only an administrator adds accounts
    expect(p.queryByRole("button", { name: "Încarcă" })).toBeNull();
  });

  it.each([["expiring", "Banca Transilvania: accesul expiră pe 6 nov"], ["expired", "Banca Transilvania: accesul a expirat"], ["ok", null]] as const)(
    "a bank connection %s offers Reconectează, which opens the bank's consent", async (state, line) => {
      at("?co=NORD&tab=tranzactii");
      const cfg = config();
      cfg.companies[0] = { ...cfg.companies[0], banks: [{ bank: "bt", name: "Banca Transilvania", connect: "ready",
        connection: { state, valid_until: "2026-11-06", last_read: "2026-10-26", accounts: ["0001"], error: null } }], accounts: [] };
      const { api, p } = await page({ companies: () => json(cfg), onPost: () => answer({ url: "" }) });
      const accounts = within(p.getByRole("region", { name: "Conturi" }));
      if (!line) return expect(accounts.queryByRole("button", { name: "Reconectează banca" })).toBeNull();
      expect(accounts.getByText(line)).toBeInTheDocument();
      await userEvent.click(accounts.getByRole("button", { name: "Reconectează banca" }));
      await waitFor(() => expect(api.posts()).toEqual([{ url: "/web/api/bank-connect", method: "POST", body: { co: "NORD", bank: "bt" } }]));
    });

  it("lists each account with its bank, last statement and balance, and leads to Bănci și conturi to add one", async () => {
    at("?co=NORD&tab=tranzactii");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], has_ron_account: true, iban_last4: "0001", accounts: [
      { id: "acc1", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: "2026-10" },
      { id: "acc2", iban_last4: "0002", currency: "EUR", bank: "bt", bank_name: "Banca Transilvania", pays: false, retired: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: null },
      { id: "acc3", iban_last4: "0009", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: false, retired: "2026-09-30", statement_senders: [], last_statement: "2026-09" },
      { id: "acc4", iban_last4: "0007", currency: "EUR", bank: "", bank_name: "", pays: false, retired: null, statement_senders: [], last_statement: null }] };
    const { api, p } = await page({ companies: () => json(cfg), onPost: () => answer({ key: "NORD", currency: "EUR", bank: "ing", iban_last4: "0007" }, "Contul a fost adăugat.") });
    const accounts = p.getByRole("region", { name: "Conturi" });
    expect([...accounts.querySelectorAll(".row")].map((r) => r.textContent)).toEqual([
      "Lei, cont 0001Banca Transilvania, pentru plăți, extras octombrie 202618.420,55 leila 30 sep",
      "EUR, cont 0002Banca Transilvania, niciun extras", "Lei, cont 0009Înlocuit pe 30.09.2026", "EUR, cont 0007Altă bancă, niciun extras"]);
    expect(within(accounts).getByRole("link", { name: "Bănci și conturi" })).toHaveAttribute("href", "?co=NORD&tab=setari&sec=firme#conn-NORD");
    expect(p.queryByRole("form", { name: "Adaugă un cont pentru NORD" })).toBeNull(); // added in Bănci și conturi
    expect(api.posts()).toEqual([]);
  });

  it("uploads a statement from Încarcă: the file as it is, for that company, no bank named, and says what the engine answered", async () => {
    at("?co=NORD&tab=tranzactii");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], has_ron_account: true, iban_last4: "0001", accounts: [
      { id: "acc5", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: null }] };
    const { api, p } = await page({ companies: () => json(cfg), onPost: () => json({ ok: true, message: "Extrasul a fost încărcat.", next: null, done: { status: "added" }, state: null }) });
    const input = (await openUpload("Încarcă extras"))!;
    const file = new File(["<Document/>"], "extras.xml", { type: "text/xml" });
    await userEvent.upload(input, file);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    const post = api.posts()[0];
    expect(post.url).toBe("/web/api/statement-upload?co=NORD");
    expect(post.type).toBe("application/xml");
    // the answer stays beside the button, in the sheet, until the next upload
    expect(await within(p.getByRole("dialog", { name: "Încarcă" })).findByRole("status")).toHaveTextContent("Extrasul a fost încărcat.");
  });

  it("several PDFs picked at once go in one request, as one month's statement; other files with them are not sent", async () => {
    at("?co=NORD&tab=tranzactii");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], accounts: [
      { id: "acc8", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, statement_senders: [], last_statement: null }] };
    const { api } = await page({ companies: () => json(cfg), onPost: () => json({ ok: true, message: "Extrasul a fost încărcat.", next: null, done: { status: "added" }, state: null }) });
    const input = (await openUpload("Încarcă extras"))!;
    const pdf = (name: string) => new File(["%PDF-1.7"], name, { type: "application/pdf" });
    await userEvent.upload(input, [pdf("ron.pdf"), new File(["<Document/>"], "eur.xml", { type: "text/xml" })]);
    expect(await within(screen.getByRole("dialog", { name: "Încarcă" })).findByRole("alert")).toHaveTextContent("Mai multe fișiere se încarcă împreună doar ca extrase PDF");
    expect(api.posts()).toHaveLength(0);
    await userEvent.upload(input, [pdf("ron.pdf"), pdf("eur.pdf")]);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    const post = api.posts()[0];
    expect(post.url).toBe("/web/api/statement-upload?co=NORD");
    expect((post.raw as FormData).getAll("files").map((f) => (f as File).name)).toEqual(["ron.pdf", "eur.pdf"]);
  });

  it("a refused statement says why beside the button until the next upload", async () => {
    at("?co=NORD&tab=tranzactii");
    const why = "Extrasul trebuie să cuprindă toată luna (1–30 noiembrie). Încarcă extrasul lunii încheiate.";
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], accounts: [
      { id: "acc7", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, statement_senders: [], last_statement: null }] };
    let n = 0;
    const { api, p } = await page({ companies: () => json(cfg), onPost: () => (n++ ? json({ ok: true, message: "Extrasul a fost încărcat.", next: null, done: { status: "added" }, state: null })
      : json({ ok: false, message: why, next: null, done: null, state: null }, 422)) });
    const input = (await openUpload("Încarcă extras"))!;
    const sheet = within(p.getByRole("dialog", { name: "Încarcă" }));
    await userEvent.upload(input, new File([":20:X"], "extras.sta", { type: "" }));
    expect(await sheet.findByRole("alert")).toHaveTextContent(why);
    await userEvent.upload(input, new File(["<Document/>"], "extras.xml", { type: "text/xml" }));
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(await sheet.findByRole("status")).toHaveTextContent("Extrasul a fost încărcat.");
    expect(sheet.queryByRole("alert")).toBeNull();
  });

  it("a file that isn't a PDF or XML is not sent", async () => {
    at("?co=NORD&tab=tranzactii");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], accounts: [
      { id: "acc6", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: null }] };
    const { api } = await page({ companies: () => json(cfg) });
    const input = (await openUpload("Încarcă extras"))!;
    await userEvent.upload(input, new File(["x"], "poza.png", { type: "image/png" }), { applyAccept: false });
    expect(await within(screen.getByRole("dialog", { name: "Încarcă" })).findByRole("alert")).toHaveTextContent("Se acceptă extrase PDF, CAMT (XML), MT940 sau CSV.");
    expect(api.posts()).toHaveLength(0);
  });

  it("a CSV goes on an account, its first upload asks which column is what the engine could not tell, then it is sent again", async () => {
    at("?co=NORD&tab=tranzactii");
    const cfg = config();
    cfg.companies[0] = { ...cfg.companies[0], accounts: [
      { id: "a-ron", iban_last4: "0001", currency: "RON", bank: "bt", bank_name: "Banca Transilvania", pays: true, retired: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: null },
      { id: "a-eur", iban_last4: "0002", currency: "EUR", bank: "bt", bank_name: "Banca Transilvania", pays: false, retired: null, statement_senders: ["EmailExtrase@btrl.ro"], last_statement: null }] };
    let uploads = 0;
    const { api, p } = await page({ companies: () => json(cfg), onPost: (c: Call) => {
      if (c.url.includes("statement-upload")) {
        uploads += 1;
        return uploads === 1
          ? json({ ok: false, message: "La primul extras CSV al contului, se aleg coloanele pentru dată, sumă și detalii.", state: null,
            csv: { columns: ["Data", "Detalii", "Suma"], rows: [["02.10.2026", "Chirie", "-100,00"]], account: "a-eur",
              detected: { dates: { Data: "%d.%m.%Y" }, decimals: { Suma: "," } } } }, 409)
          : json({ ok: true, message: "Extrasul a fost încărcat.", next: null, done: { status: "added" }, state: null });
      }
      return json({ ok: true, message: "Coloanele au fost salvate.", next: null, done: null, state: null });
    } });
    const input = (await openUpload("Încarcă extras"))!;
    await userEvent.upload(input, new File(["Data;Detalii;Suma\n"], "extras.csv", { type: "text/csv" }));
    const which = within(p.getByRole("form", { name: "Contul extrasului CSV" }));
    await choose(which.getByLabelText("Contul acestui extras CSV"), "a-eur");
    await userEvent.click(which.getByRole("button", { name: "Încarcă pe acest cont" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].url).toBe("/web/api/statement-upload?co=NORD&account=a-eur");
    expect(api.posts()[0].type).toBe("text/csv");
    const cols = within(await p.findByRole("form", { name: "Coloanele extrasului CSV" }));
    expect(cols.getByRole("region", { name: "Primele rânduri din fișier" })).toHaveTextContent("ChirieChirie".slice(0, 6));
    await choose(cols.getByLabelText("Data"), "Data"); // columns are always the person's pick
    expect(cols.queryByLabelText("Formatul datei")).toBeNull();
    await choose(cols.getByLabelText("Suma"), "Suma");
    expect(cols.queryByLabelText("Separatorul zecimal")).toBeNull();
    await choose(cols.getByLabelText("Detaliile plății"), "Detalii");
    await userEvent.click(cols.getByRole("button", { name: "Salvează coloanele și încarcă" }));
    await waitFor(() => expect(api.posts()).toHaveLength(3));
    expect(api.posts()[1]).toEqual({ url: "/web/api/statement-csv-format", method: "POST", body: {
      co: "NORD", account: "a-eur", date: "Data", date_format: "%d.%m.%Y", decimal: ",", amount: "Suma", description: "Detalii" } });
    expect(api.posts()[2].url).toBe("/web/api/statement-upload?co=NORD&account=a-eur");
    expect(await within(p.getByRole("dialog", { name: "Încarcă" })).findByRole("status")).toHaveTextContent("Extrasul a fost încărcat.");
  });

  it("lists only the days and the direction an Acasă number counts (from, to, f)", async () => {
    at("?co=NORD&tab=tranzactii&f=in&from=2026-10-01&to=2026-10-27");
    renderTransactions();
    expect(await screen.findByText("POPESCU ANA")).toBeInTheDocument(); // October's credit from a person
    const s = fixture();
    const want = s.transactions.rows.filter((t) => t.direction === "credit" && t.date >= "2026-10-01" && t.date <= "2026-10-27");
    expect(want.length).toBeGreaterThan(0);
    expect(document.querySelectorAll(".row.tx")).toHaveLength(want.length);
    expect(screen.queryByText("NIMBUS HOSTING INC")).toBeNull(); // a debit of the same days: not money in
  });
});

const PREVIEW = { columns: ["AWB", "Factura", "Ramburs", "Comision", "Data plata"], rows: [["1000000001", "TEST-1", "100.00", "2.00", "2026-10-25"]],
  known: [], guess: { reference: "Factura", gross: "Ramburs", fee: "Comision" }, date: "2026-10-25", orders: false, payer: "", source: "", currency: "",
  currencies: ["RON", "EUR"], payers: [{ source: "fan", name: "Curier Exemplu SRL", cui: "12345678", currency: "EUR", courier: "" }] };

it("reads the report when the file is chosen and opens with the guessed columns, only the rest to choose", async () => {
  at("?co=NORD&tab=tranzactii");
  const cfg = config();
  cfg.companies[0].accounts = [];
  const { api } = await page({ companies: () => json(cfg), onPost: () => json({ ok: false, message: "", state: null, report: PREVIEW }) });
  await openUpload("Încarcă raport de decontare");
  const file = new File(["AWB,Factura,Ramburs,Comision,Data plata\n"], "report.csv", { type: "text/csv" });
  await userEvent.upload(screen.getByLabelText("Raport de decontare"), file);
  await waitFor(() => expect(api.posts()).toHaveLength(1));
  expect(Object.fromEntries(new URL(api.posts()[0].url, "http://example.test").searchParams)).toEqual({ co: "NORD", report: "1" });
  expect(await screen.findByLabelText("Data plății din raport")).toHaveValue("2026-10-25");
  expect(screen.getByLabelText("Numărul facturii")).toHaveValue("Factura");
  expect(screen.queryByLabelText("AWB")).toBeNull(); // a firm without shop orders is never offered the AWB
  expect(screen.getByRole("button", { name: "Încarcă raportul" })).toBeDisabled();
  expect(screen.getByText("Alege curierul sau procesatorul.")).toBeVisible();
  await choose(screen.getByLabelText("Curier sau procesator"), "fan"); // the payer saved for FAN fills the rest
  expect(screen.getByLabelText("Numele partenerului din extras")).toHaveValue("Curier Exemplu SRL");
  expect(screen.getByLabelText("Moneda raportului")).toHaveValue("EUR");
  await userEvent.click(screen.getByRole("button", { name: "Încarcă raportul" }));
  await waitFor(() => expect(api.posts()).toHaveLength(2));
  expect(Object.fromEntries(new URL(api.posts()[1].url, "http://example.test").searchParams)).toEqual({ co: "NORD", report: "1", date: "2026-10-25",
    format: JSON.stringify({ source: "fan", name: "Curier Exemplu SRL", cui: "12345678", currency: "EUR", mapping: PREVIEW.guess }) });
  expect(api.posts()[1].raw).toBe(file);
});

it("clears a saved payer's CUI when another courier is chosen, keeping the name the person typed", async () => {
  at("?co=NORD&tab=tranzactii");
  const cfg = config();
  cfg.companies[0].accounts = [];
  await page({ companies: () => json(cfg), onPost: () => json({ ok: false, message: "", state: null, report: PREVIEW }) });
  await openUpload("Încarcă raport de decontare");
  await userEvent.upload(screen.getByLabelText("Raport de decontare"), new File(["x"], "report.csv", { type: "text/csv" }));
  await choose(await screen.findByLabelText("Curier sau procesator"), "fan");
  expect(screen.getByLabelText("CUI-ul partenerului (opțional)")).toHaveValue("12345678"); // shown, not folded away
  await userEvent.type(screen.getByLabelText("Numele partenerului din extras"), " Nou");
  expect(screen.getByLabelText("CUI-ul partenerului (opțional)")).toHaveValue(""); // another name, not the saved payer's CUI
  await choose(screen.getByLabelText("Curier sau procesator"), "dpd");
  expect(screen.getByLabelText("Moneda raportului")).toHaveValue("RON");
  expect(screen.getByLabelText("Numele partenerului din extras")).toHaveValue("Curier Exemplu SRL Nou");
});

it("starts each new file from its own currency, not the previous file's", async () => {
  at("?co=NORD&tab=tranzactii");
  const cfg = config();
  cfg.companies[0].accounts = [];
  const previews = [{ ...PREVIEW, currency: "EUR" }, { ...PREVIEW, payers: [] }];
  await page({ companies: () => json(cfg), onPost: () => json({ ok: false, message: "", state: null, report: previews.shift() }) });
  await openUpload("Încarcă raport de decontare");
  await userEvent.upload(screen.getByLabelText("Raport de decontare"), new File(["x"], "eur.csv", { type: "text/csv" }));
  await waitFor(() => expect(screen.getByLabelText("Moneda raportului")).toHaveValue("EUR"));
  await userEvent.upload(screen.getByLabelText("Raport de decontare"), new File(["y"], "ron.csv", { type: "text/csv" }));
  await waitFor(() => expect(screen.getByLabelText("Moneda raportului")).toHaveValue("RON"));
});

it("drops a report preview that answers after the firm changed", async () => {
  const only = (co: "NORD" | "STUDIO9", other: "NORD" | "STUDIO9") => fixture(co).transactions.rows.map((t) => t.counterparty)
    .find((name) => name && !fixture(other).transactions.rows.some((t) => t.counterparty === name))!;
  const go = async (co: "NORD" | "STUDIO9") => { // the firm's own line on screen: its page is the one shown
    window.history.pushState(null, "", `?co=${co}&tab=tranzactii`);
    window.dispatchEvent(new PopStateEvent("popstate"));
    await screen.findByText(only(co, co === "NORD" ? "STUDIO9" : "NORD"));
  };
  at("?co=STUDIO9&tab=tranzactii"); // STUDIO9 seen first, so switching back to it renders from cache at once
  const cfg = config();
  for (const c of cfg.companies) c.accounts = [];
  const pending = deferred<Response>();
  const { api } = await page({ companies: () => json(cfg), onPost: () => pending.promise });
  await go("NORD");
  await openUpload("Încarcă raport de decontare");
  await userEvent.upload(screen.getByLabelText("Raport de decontare"), new File(["x"], "report.csv", { type: "text/csv" }));
  await waitFor(() => expect(api.posts()).toHaveLength(1));
  await go("STUDIO9");
  await act(async () => { pending.resolve(json({ ok: false, message: "", state: null, report: PREVIEW })); await pending.promise; });
  expect(screen.queryByLabelText("Data plății din raport")).toBeNull();
  expect(api.posts()).toHaveLength(1);
});

it("preselects the payer saved for this header and sends only its id", async () => {
  at("?co=NORD&tab=tranzactii");
  const cfg = config();
  cfg.companies[0].accounts = [];
  const known = [{ id: "NORD.n-curier", name: "Curier Exemplu SRL", source: "fan", company_cui_column: "" }];
  const { api } = await page({ companies: () => json(cfg),
    onPost: () => json({ ok: false, message: "", state: null, report: { ...PREVIEW, known, payer: known[0].id } }) });
  await openUpload("Încarcă raport de decontare");
  await userEvent.upload(screen.getByLabelText("Raport de decontare"), new File(["x"], "report.csv", { type: "text/csv" }));
  await userEvent.click(await screen.findByRole("button", { name: "Încarcă raportul" }));
  await waitFor(() => expect(api.posts()).toHaveLength(2));
  expect(Object.fromEntries(new URL(api.posts()[1].url, "http://example.test").searchParams))
    .toEqual({ co: "NORD", report: "1", date: "2026-10-25", partner: known[0].id });
});

it("allows local report inspection before activation", async () => {
  at("?co=NORD&tab=tranzactii");
  const s = fixture(); s.live = false;
  const cfg = config(); cfg.companies[0].accounts = [];
  const { api } = await page({ state: () => json(s), companies: () => json(cfg) });
  await openUpload("Încarcă raport de decontare");
  await userEvent.upload(screen.getByLabelText("Raport de decontare"), new File(["Ref,Gross,Fee"], "report.csv"));
  await waitFor(() => expect(api.posts()).toHaveLength(1));
});
