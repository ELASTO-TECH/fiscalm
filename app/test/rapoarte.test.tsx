// Rapoarte lists a month's received invoices and receipts as the engine groups them (`expenses`), like Cheltuieli: an invoice
// and its bon in the same month are one row, "Factură și bon", counted once. The accountant's pack stays the engine's `docs`.
import { act, render as renderPlain, screen, within, waitFor } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import { Sums } from "@/components/ui";
import { sums } from "@/lib/format";
import { fixtureBrowseData, fixtureReportPagesData } from "@/lib/fixtures";
import type { ReportScreen, State } from "@/lib/types";
import { at, browseResult, deferred, fixture, json, server, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());

const reportLoaded = () => waitFor(() => expect(screen.queryAllByText("Se încarcă…")).toHaveLength(0));

const brico = (s: State) => s.documents.find((d) => d.vendor === "Brico Nord")!;
const report = (s: State, month: string) => s.reports.find((r) => r.month === month)!;
/** A section of the month view: its heading's count and the card under it. */
const section = (name: string) => {
  const head = screen.getByRole("heading", { name }).parentElement as HTMLElement;
  return { count: head.querySelector("span")?.textContent, card: head.nextElementSibling as HTMLElement };
};
const rowsOf = (card: HTMLElement) => [...card.querySelectorAll(".row")] as HTMLElement[];
const names = (card: HTMLElement) => rowsOf(card).map((r) => r.querySelector("b")!.textContent);
const links = (card: HTMLElement) => rowsOf(card).map((r) => r.getAttribute("href"));

describe("Rapoarte, Facturi primite", () => {
  it("shows an invoice and its bon in the same month as one row, and every other invoice and receipt once", async () => {
    const s = fixture();
    const oct = report(s, "2026-10");
    const bon = brico(s);
    expect(oct.received).toContain("112");
    expect(oct.documents).toContain(bon.id);
    expect(s.expenses).toContainEqual({ id: "NORD:invoice:112", company: "NORD", invoice: "112", document: bon.id, sources: ["efactura", "upload"] });
    at("?co=NORD&tab=rapoarte&month=2026-10");
    renderApp();
    expect(await screen.findByText("Raport pentru octombrie 2026")).toBeInTheDocument();
    await reportLoaded();
    const { count, card } = section("Facturi primite");

    const row = within(card).getByText("Brico Nord SRL").closest(".row") as HTMLElement;
    expect(row).toHaveTextContent("Factură și bon, Plătită, 27 oct");
    expect(row).toHaveAttribute("href", "?co=NORD&tab=rapoarte&inv=112&month=2026-10");
    expect(within(card).queryByText("Brico Nord")).toBeNull(); // the bon is no row of its own
    expect(rowsOf(card).filter((r) => r.textContent!.includes("Factură și bon"))).toEqual([row]);

    // every invoice and every other receipt of the month, once, each opening its own sheet
    const docs = s.documents.filter((d) => oct.documents.includes(d.id) && d.id !== bon.id);
    expect(links(card)).toEqual([
      ...s.invoices.filter((r) => oct.received.includes(r.id)).map((r) => `?co=NORD&tab=rapoarte&inv=${r.id}&month=2026-10`),
      ...docs.map((d) => `?co=NORD&tab=rapoarte&doc=${d.id}&month=2026-10`),
    ]);
    expect(names(card).slice(-3)).toEqual(["Benzinaria Nord", "Example Cloud", "Figma"]);
    expect(rowsOf(card)).toHaveLength(oct.received.length + oct.documents.length - 1);
    expect(count).toBe(String(rowsOf(card).length));

    // the pack is the engine's list, untouched: the bon is still one of its documents
    expect(names(section("Dosar pentru contabil").card)).toEqual(["Extrasul de cont", ...oct.docs.map((d) => d.vendor)]);
    expect(names(section("Dosar pentru contabil").card)).toContain("Brico Nord");
  });

  it("opens the grouped row on the invoice's sheet, with the bon on it and a way to the bon's own sheet", async () => {
    const bon = brico(fixture());
    at("?co=NORD&tab=rapoarte&month=2026-10");
    renderApp();
    await screen.findByText("Raport pentru octombrie 2026");
    await reportLoaded();
    await userEvent.click(within(section("Facturi primite").card).getByText("Brico Nord SRL"));
    const sheet = await screen.findByRole("dialog", { name: "Brico Nord SRL, 156,40 lei" });
    expect(window.location.search).toBe("?co=NORD&tab=rapoarte&inv=112&month=2026-10");
    expect(sheet.querySelector("header + .st")).toHaveTextContent("Plătită");
    expect(within(sheet).getByRole("img", { name: "Document, Brico Nord" })).toHaveAttribute("src", `/web/docfile?co=NORD&sha=${bon.files[0].id}`);
    await userEvent.click(within(sheet).getByRole("link", { name: "Vezi bonul" }));
    expect(window.location.search).toBe(`?co=NORD&tab=rapoarte&doc=${bon.id}&month=2026-10`);
    const own = await screen.findByRole("dialog", { name: "Brico Nord, 156,40 lei" });
    expect(within(own).getByRole("link", { name: "Vezi factura" })).toHaveAttribute("href", "?co=NORD&tab=rapoarte&inv=112&month=2026-10");
    await userEvent.keyboard("{Escape}");
    expect(window.location.search).toBe("?co=NORD&tab=rapoarte&month=2026-10");
  });

  it("counts the pair once in the month's line, the same as the month view", async () => {
    const s = fixture();
    const oct = report(s, "2026-10");
    expect(oct.received.length + oct.documents.length).toBe(16);
    at("?co=NORD&tab=rapoarte");
    renderApp();
    const line = await screen.findByRole("link", { name: /^Octombrie 2026/ }) as HTMLElement;
    expect(line).toHaveTextContent("15 cheltuieli, 12 facturi emise"); // includes the aging invoices and the two client reminders
    expect(screen.getByRole("link", { name: /^Septembrie 2026/ })).toHaveTextContent("5 cheltuieli, 5 facturi emise");
    expect(screen.getByRole("link", { name: /^August 2026/ })).toHaveTextContent("O cheltuială");
  });

  it("keeps each part in its own month when the invoice and its bon fall in different months, without the tag", async () => {
    const s = fixture();
    const bon = brico(s);
    const oct = report(s, "2026-10");
    oct.documents = oct.documents.filter((id) => id !== bon.id);
    report(s, "2026-09").documents.push(bon.id); // paid at the till on 30 September, invoiced in October
    at("?co=NORD&tab=rapoarte&month=2026-10");
    renderApp({ state: () => json(s) });
    await screen.findByText("Raport pentru octombrie 2026");
    await reportLoaded();
    let { count, card } = section("Facturi primite");
    expect(within(card).getByText("Brico Nord SRL").closest(".row")).toHaveTextContent("Plătită, 27 oct");
    expect(card).not.toHaveTextContent("Factură și bon");
    expect(within(card).queryByText("Brico Nord")).toBeNull();
    expect(count).toBe("15");
    expect(rowsOf(card)).toHaveLength(15);

    await userEvent.click(screen.getByRole("link", { name: "Înapoi la rapoarte" }));
    await userEvent.click(screen.getByText("Septembrie 2026"));
    await screen.findByText("Raport pentru septembrie 2026");
    await reportLoaded();
    ({ count, card } = section("Facturi primite"));
    const own = within(card).getByText("Brico Nord").closest(".row") as HTMLElement;
    expect(own).toHaveAttribute("href", `?co=NORD&tab=rapoarte&doc=${bon.id}&month=2026-09`);
    expect(own).not.toHaveTextContent("Factură și bon");
    expect(own).not.toHaveTextContent("Fără factură"); // its invoice is October's
    expect(within(card).queryByText("Brico Nord SRL")).toBeNull();
    expect(count).toBe("6");
    expect(rowsOf(card)).toHaveLength(6);
  });

  it("still lists, once and on its own, an invoice or a receipt that no expense names, beside the grouped pair", async () => {
    const s = fixture();
    const figma = s.documents.find((d) => d.vendor === "Figma")!;
    s.expenses = s.expenses.filter((e) => e.invoice !== "102" && e.document !== figma.id);
    expect(s.invoices.find((r) => r.id === "102")!.supplier).toBe("Telecom Rapid SRL");
    at("?co=NORD&tab=rapoarte&month=2026-10");
    renderApp({ state: () => json(s) });
    await screen.findByText("Raport pentru octombrie 2026");
    await reportLoaded();
    const { count, card } = section("Facturi primite");
    const telecom = within(card).getAllByText("Telecom Rapid SRL").map((x) => x.closest(".row") as HTMLElement);
    expect(telecom).toHaveLength(1);
    expect(telecom[0]).toHaveAttribute("href", "?co=NORD&tab=rapoarte&inv=102&month=2026-10");
    expect(telecom[0]).not.toHaveTextContent("Factură și bon");
    const own = within(card).getAllByText("Figma").map((x) => x.closest(".row") as HTMLElement);
    expect(own).toHaveLength(1);
    expect(own[0]).toHaveAttribute("href", `?co=NORD&tab=rapoarte&doc=${figma.id}&month=2026-10`);
    expect(within(card).getByText("Brico Nord SRL").closest(".row")).toHaveTextContent("Factură și bon, Plătită, 27 oct");
    expect(within(card).queryByText("Brico Nord")).toBeNull();
    expect(rowsOf(card)).toHaveLength(15);
    expect(count).toBe("15");
  });

  it("keeps an invoice and a bon apart when the engine does, even though the invoice names the bon", async () => {
    const s = fixture();
    const bon = brico(s);
    // The engine left the Brico pair separate, as it does for contradictory links
    s.expenses = s.expenses
      .filter((e) => e.invoice !== "112")
      .concat([
        { id: "NORD:invoice:112", company: "NORD", invoice: "112", document: null, sources: [] },
        { id: `NORD:document:${bon.id}`, company: "NORD", invoice: null, document: bon.id, sources: [] },
      ]);
    expect(s.invoices.find((r) => r.id === "112")!.bon).toBe(bon.id);
    at("?co=NORD&tab=rapoarte&month=2026-10");
    renderApp({ state: () => json(s) });
    await screen.findByText("Raport pentru octombrie 2026");
    await reportLoaded();
    const { count, card } = section("Facturi primite");
    expect(card).not.toHaveTextContent("Factură și bon");
    expect(within(card).getByText("Brico Nord SRL").closest(".row")).toHaveAttribute("href", "?co=NORD&tab=rapoarte&inv=112&month=2026-10");
    expect(within(card).getByText("Brico Nord").closest(".row")).toHaveAttribute("href", `?co=NORD&tab=rapoarte&doc=${bon.id}&month=2026-10`);
    expect(rowsOf(card)).toHaveLength(16);
    expect(count).toBe("16");
  });
});

describe("Rapoarte", () => {
  it("phrases an unreadable bank error without exposing the API code", async () => {
    at("?co=NORD&tab=rapoarte&month=2026-09");
    const state = fixture();
    const report = state.reports.find((r) => r.month === "2026-09")!;
    report.bank!.error = "unreadable";
    renderApp({ state: () => json(state) });
    expect((await screen.findAllByText("Primit, dar nu poate fi citit")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/unreadable/)).not.toBeInTheDocument();
  });

  it("lists each month with its checks and what is missing", async () => {
    at("?co=NORD&tab=rapoarte");
    renderApp();
    const sep = await screen.findByRole("link", { name: /^Septembrie 2026/ }) as HTMLElement;
    // anything open names the month, never its pack's "De pregătit"
    expect(sep).toHaveTextContent("2 de rezolvat");
    expect(sep).not.toHaveTextContent("De pregătit");
    expect(sep).toHaveTextContent("5 cheltuieli, 5 facturi emise, extras verificat, 5 din 7 tranzacții asociate");
    expect(sep).toHaveTextContent("Mai lipsesc: 2 facturi");
    const oct = screen.getByRole("link", { name: /^Octombrie 2026/ }) as HTMLElement;
    expect(oct).toHaveTextContent("1 de rezolvat");
    expect(oct).toHaveTextContent("Mai lipsesc: o factură din e-mail de verificat");
    expect(screen.getByRole("link", { name: /^August 2026/ })).toHaveTextContent("Trimis");
  });

  it("shows the month and prepares the local accountant ZIP", async () => {
    at("?co=NORD&tab=rapoarte&month=2026-09");
    const api = server();
    render(<App />);
    expect(await screen.findByText("Raport pentru septembrie 2026")).toBeInTheDocument();
    await reportLoaded();
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(
      ["De rezolvat", "Facturi primite", "Facturi emise", "Extras și plăți", "Confirmări de plată", "Dosar pentru contabil"]);
    expect(screen.getByText("Raport pentru septembrie 2026").nextElementSibling).toHaveTextContent("2 de rezolvat");
    expect(section("Dosar pentru contabil").card.previousElementSibling).toHaveTextContent("De pregătit");
    // each open debit once, under De rezolvat, named by how it left the account; Extras și plăți keeps the matched ones
    const open = section("De rezolvat").card;
    expect(rowsOf(open).map((x) => x.textContent)).toEqual([
      expect.stringMatching(/^NIMBUS HOSTING INCPlată cu cardul, 22 sep, 37.00 USD173,45 leiLipsește factura/),
      expect.stringMatching(/^Exemplu Imobiliare SRLTransfer, 25 sep1\.200,00 leiLipsește factura/)]);
    expect(names(section("Extras și plăți").card)).toEqual(["Extrasul de cont", ...Array(5).fill("HETZNER ONLINE")]);
    // a treasury advance from the statement: a transfer from the company's account (to whoever took it), not a card payment
    const advance = screen.getByText("Transfer din contul firmei, de decontat").closest(".row")!;
    expect(advance).toHaveTextContent("Avans de trezorerie, 10 sep");
    expect(advance).toHaveTextContent("500,00 lei");
    // preparing doesn't wait for the open debits: their count links to them, next to the one action
    expect(screen.getByRole("button", { name: "Descarcă dosarul" }).previousElementSibling).toHaveTextContent("2 tranzacții fără document");
    await userEvent.click(screen.getByRole("button", { name: "Descarcă dosarul" }));
    expect(document.querySelector(".ask")).toBeNull(); // preparing asks nothing
    await waitFor(() => expect(api.posts()[0].body).toEqual({ co: "NORD", month: "2026-09" }));
  });

  it("says so when nothing is left to solve", async () => {
    at("?co=NORD&tab=rapoarte&month=2026-08");
    renderApp();
    await screen.findByRole("heading", { name: "De rezolvat" });
    await waitFor(() => expect(document.getElementById("rezolvat")!.nextElementSibling).toHaveTextContent("Niciun element"));
  });
});

const reportScreens: ReportScreen[] = ["report_received", "report_issued", "report_tasks", "report_bank", "report_advances", "report_docs", "report_not_mailed"];
it.each(reportScreens)("keeps fixture and engine report selectors equal for %s", (screen) => {
  const s = fixture();
  for (const month of ["2026-10", "2026-09"]) {
    const query = { month };
    expect(fixtureBrowseData(s, screen, query)).toEqual(browseResult(s, screen, query));
  }
});

it("pages a report without root invoice rows, keeps its complete count, URL and boundary focus", async () => {
  const full = fixture(), base = full.invoices[0], r = report(full, "2026-10");
  full.invoices = Array.from({ length: 151 }, (_, i) => ({ ...base, id: `report-${i}`, supplier: `Furnizor sintetic ${i}`, bon: null }));
  full.expenses = full.invoices.map((x) => ({ id: `NORD:invoice:${x.id}`, company: full.co, invoice: x.id, document: null, sources: [] }));
  Object.assign(r, { received: full.invoices.map((x) => x.id), documents: [], issued: [], to_check: [], docs: [], not_mailed: [], bank: null });
  full.to_check = []; full.ambiguous = [];
  const compact = structuredClone(full);
  compact.bounded = true;
  compact.invoices = []; compact.documents = []; compact.expenses = []; compact.issued = [];
  const head = report(compact, r.month);
  Object.assign(head, { received: [], counts: { received: 151, documents: 0, issued: 0, to_check: 0, docs: 0, not_mailed: 0, purchases: 151, card: 0, matched: 0, advances: 0, ambiguous: 0, statements: 0 } });
  const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
  at("?co=NORD&tab=rapoarte&month=2026-10#pachet");
  const api = renderApp({ state: () => json(compact), reportPages: () => json(fixtureReportPagesData(full, r.month)), browse: (url) => {
    const q = Object.fromEntries(new URL(url, "http://local").searchParams);
    return json(fixtureBrowseData(full, q.screen as ReportScreen, q));
  } });
  await screen.findByText("Raport pentru octombrie 2026");
  await reportLoaded();
  await waitFor(() => expect(scroll.mock.instances).toContain(document.getElementById("pachet")));
  expect(api.calls.filter((c) => c.url.includes("api/report-pages"))).toHaveLength(1);
  expect(api.calls.filter((c) => c.url.includes("api/browse"))).toHaveLength(0);
  const card = section("Facturi primite").card;
  expect(section("Facturi primite").count).toBe("151");
  expect(rowsOf(card)).toHaveLength(50);
  const next = within(card).getByRole("button", { name: "Pagina următoare" });
  for (let i = 0; i < 3; i++) { await userEvent.click(next); await reportLoaded(); }
  expect(api.calls.filter((c) => c.url.includes("api/browse"))).toHaveLength(3);
  expect(rowsOf(card)).toHaveLength(1);
  expect(within(card).getByText("Furnizor sintetic 150").closest("a")).toHaveAttribute("href", "?co=NORD&tab=rapoarte&inv=report-150&month=2026-10");
  expect(next).toHaveFocus(); expect(next).toHaveAttribute("aria-disabled", "true");
  await userEvent.keyboard("{Enter}"); expect(rowsOf(card)).toHaveLength(1);
  await userEvent.click(screen.getByRole("link", { name: "Înapoi la rapoarte" }));
  expect(screen.getByRole("link", { name: /^Octombrie 2026/ })).toHaveTextContent("151 de cheltuieli");
});

it("retries a failed initial read without losing focus and ignores a late report after changing month", async () => {
  const old = deferred<Response>(); let first = true;
  at("?co=NORD&tab=rapoarte&month=2026-10");
  renderApp({ reportPages: (url) => {
    const month = new URL(url, "http://local").searchParams.get("month")!;
    if (month === "2026-10") { if (first) { first = false; return json({}, 500); } return old.promise; }
    return json(fixtureReportPagesData(fixture(), month));
  } });
  const retry = await screen.findByRole("button", { name: "Reîncearcă" });
  const holder = retry.closest("[tabindex]")!;
  await userEvent.click(retry);
  expect(holder).toHaveFocus();
  await userEvent.click(screen.getByRole("link", { name: "Înapoi la rapoarte" }));
  await userEvent.click(screen.getByText("Septembrie 2026"));
  await screen.findByText("Raport pentru septembrie 2026"); await reportLoaded();
  const expected = names(section("Facturi primite").card);
  await act(async () => { old.resolve(json(fixtureReportPagesData(fixture(), "2026-10"))); await old.promise; });
  expect(names(section("Facturi primite").card)).toEqual(expected);
  expect(screen.queryByText("Raport pentru octombrie 2026")).not.toBeInTheDocument();
});

it("keeps report sections mounted during an action refresh and scrolls the saved anchor only once", async () => {
  const pending = deferred<Response>(), s = fixture(); let reads = 0;
  const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
  at("?co=NORD&tab=rapoarte&month=2026-09#pachet");
  renderApp({ reportPages: () => ++reads === 1 ? json(fixtureReportPagesData(s, "2026-09")) : pending.promise,
    onPost: () => json({ ok: true, message: "", state: structuredClone(s) }) });
  await screen.findByText("Raport pentru septembrie 2026"); await reportLoaded();
  await waitFor(() => expect(scroll.mock.instances).toContain(document.getElementById("pachet")));
  const headings = screen.getAllByRole("heading", { level: 2 }), calls = scroll.mock.calls.length;
  await userEvent.click(screen.getByRole("button", { name: "Descarcă dosarul" }));
  await waitFor(() => expect(reads).toBe(2));
  expect(screen.getAllByRole("heading", { level: 2 })).toEqual(headings);
  expect(section("Facturi primite").card.querySelectorAll(".row").length).toBeGreaterThan(0);
  await act(async () => { pending.resolve(json(fixtureReportPagesData(s, "2026-09"))); await pending.promise; });
  await reportLoaded();
  expect(screen.getAllByRole("heading", { level: 2 })).toEqual(headings);
  expect(scroll).toHaveBeenCalledTimes(calls);
});

it("keeps credits in their currency, labels a card refund as income, and names an awaited invoice", async () => {
  const s = fixture(), r = report(s, "2026-09");
  r.bank!.card = [
    { date: "2026-09-12", who: "Client Exemplu", kind: "transfer", amount: "100.00", currency: "EUR", direction: "credit", orig: "", doc: null, awaiting: false },
    { date: "2026-09-13", who: "Retur Exemplu", kind: "card", amount: "46.00", currency: "RON", direction: "credit", orig: "", doc: null, awaiting: false },
    { date: "2026-09-14", who: "Chirie Exemplu", kind: "transfer", amount: "43.00", currency: "RON", direction: "debit", orig: "", doc: null, awaiting: true },
  ];
  at("?co=NORD&tab=rapoarte&month=2026-09");
  renderApp({ state: () => json(s) });
  await screen.findByText("Raport pentru septembrie 2026");
  await reportLoaded();
  const card = section("De rezolvat").card;
  expect(within(card).getByText("100,00 EUR")).toBeInTheDocument();
  for (const name of ["Client Exemplu", "Retur Exemplu"]) {
    const row = within(card).getByText(name).closest(".row")!;
    expect(row).toHaveTextContent("Încasare");
    expect(row).toHaveTextContent("Fără explicație");
  }
  expect(within(card).getByText("Aștept factura")).toBeInTheDocument();
});

it("lists what was invoiced per year and month, marking the part from history", async () => {
  const s = fixture(), none = { total: "0.00", other: [] }, sum = (total: string) => ({ total, other: [] });
  const t = (sales: string, old: string) => ({ sales: sum(sales), expenses: none, history: { sales: sum(old), expenses: none } });
  s.turnover = { years: [{ year: "2025", turnover: t("300.00", "100.00"), months: [
    { month: "2025-12", turnover: t("200.00", "0.00") }, { month: "2025-03", turnover: t("100.00", "100.00") }] }] };
  at("?co=NORD&tab=rapoarte");
  renderApp({ state: () => json(s) });
  const table = await screen.findByRole("table");
  expect([...table.querySelectorAll("tbody tr")].map((r) => r.textContent)).toEqual([
    "Total300,00 leidin istoric 100,00 lei0,00 lei", "Decembrie200,00 lei0,00 lei", "Martie100,00 leidin istoric 100,00 lei0,00 lei"]);
  // A narrow column wraps between amounts, never between a figure and its currency.
  expect([...table.querySelectorAll("tbody tr:first-child td .whitespace-nowrap")].map((x) => x.textContent)).toEqual(["300,00 lei", "100,00 lei", "0,00 lei"]);
});

it("says amounts as sums does, each one whole: three currencies, and none in lei", () => {
  for (const s of [{ total: "10.00", other: [{ currency: "EUR", total: "2.00" }, { currency: "USD", total: "3.00" }] }, { total: "0.00", other: [{ currency: "EUR", total: "2.00" }] }]) {
    const { container, unmount } = renderPlain(<Sums s={s} />);
    expect(container.textContent).toBe(sums(s));
    expect([...container.querySelectorAll(".whitespace-nowrap")].map((x) => x.textContent).join("|")).toBe(sums(s).split(/, | și /).join("|"));
    unmount();
  }
});
