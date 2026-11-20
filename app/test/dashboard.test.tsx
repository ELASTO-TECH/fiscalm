// Acasă as the firm's dashboard (docs/information-architecture.md, section 4; docs/openapi.json):
// The engine's sums, shown as they are, every number opening the list it counts. Fixture data only.
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { niceScale } from "@/components/Charts";
import { sums } from "@/lib/format";
import { href, parse } from "@/lib/nav";
import { fixtureDashboardData, fixtureDashboardPages, fixtureIdentity } from "@/lib/fixtures";
import { coName, type AgingBucket, type AgingKey, type CashStatus, type ChartMonth, type DashboardCollection, type ProductSales, type ProductSalesRow, type State, type Sums, type VatMonth } from "@/lib/types";
import { at, dashboardResult, fixture, fixtureAll, json, paymentFile, renderApp } from "./helpers";

beforeEach(() => {
  localStorage.clear();
  at("?co=NORD");
});

const loaded = () => screen.findByRole("region", { name: "De plătit" });
const flows = () => within(screen.getByRole("region", { name: "Încasat și cheltuit" }));
/** The period's three figures, and the remarks under the chart. */
const figures = () => flows().getAllByRole("link").filter((a) => a.closest(".figs") || !a.closest("nav, .relative"));
const region = (name: string) => within(screen.getByRole("region", { name }));
const money = (total: string, other: Sums["other"] = []): Sums => ({ total, other });
const minus = (a: string, b: string) => (Number(a) - Number(b)).toFixed(2);
/** A chart month as the engine sends it: proven numbers only when complete, what the lines show always. */
function month(m: string, status: CashStatus, cin: string, cout: string, { other = [], now = false }: { other?: Sums["other"]; now?: boolean } = {}): ChartMonth {
  const observed = { in: money(cin, other), out: money(cout), net: money(minus(cin, cout), other) };
  const done = status === "complete";
  const zero = money("0.00");
  return { month: m, in_progress: now, cash_status: status, observed, invoiced: zero, collected: zero, received: zero, paid: zero,
    in: done ? observed.in : null, out: done ? observed.out : null, net: done ? observed.net : null };
}
const withMonths = (s: State, ms: ChartMonth[]): State => ({ ...s, dashboard: { ...s.dashboard, charts: { ...s.dashboard.charts, months: ms } } });

it.each(["upcoming", "held_money"] as const)("keeps %s fixture pages equal to the engine for one firm and ALL", (collection) => {
  for (const v of [fixture(), fixtureAll()]) {
    expect(fixtureDashboardData(v, collection)).toEqual(dashboardResult(v, collection));
  }
});

it.each(["upcoming", "held_money"] as const)("pages %s from bundled metadata without full source lists or initial requests", async (collection: DashboardCollection) => {
  const full = fixture();
  full.dashboard.upcoming = Array.from({ length: 151 }, (_, i) => ({ ...full.dashboard.upcoming[0], id: `synt${i}`,
    date: "2026-11-01", who: `Scadență ${String(i).padStart(3, "0")}`, number: `SYN-${i}` }));
  full.partners = Array.from({ length: 151 }, (_, i) => ({ ...full.partners[0], id: `NORD.synt${i}`, name: `Curier ${String(i).padStart(3, "0")}`,
    held: Array.from({ length: 151 }, (_, n) => ({ invoice: `synt${n}`, currency: "RON", amount: "0.10" })) }));
  fixtureDashboardPages(full);
  const compact = { ...full, bounded: true, partners: [], dashboard: { ...full.dashboard, upcoming: [] } };
  const calls = renderApp({ state: () => json(compact), dashboardPage: (url) => {
    const q = new URL(url, "http://local").searchParams;
    return json(fixtureDashboardData(full, q.get("collection") as DashboardCollection, q.get("cursor") ?? ""));
  } });
  await loaded();
  const name = collection === "upcoming" ? "Următoarele 30 de zile" : "La curieri și procesatori";
  const section = within(screen.getByRole("heading", { name }).closest("section")!);
  await waitFor(() => expect(section.getAllByRole("link")).toHaveLength(50));
  expect(calls.calls.filter((c) => c.url.includes("api/dashboard-list"))).toHaveLength(0);
  const nav = within(section.getByRole("navigation", { name }));
  const next = nav.getByRole("button", { name: "Pagina următoare" });
  for (const start of [51, 101, 151]) {
    await userEvent.click(next);
    await waitFor(() => expect(nav.getByRole("status")).toHaveTextContent(`${start}–${Math.min(start + 49, 151)} din 151`));
    await waitFor(() => expect(section.queryByText("Se încarcă…")).not.toBeInTheDocument());
    await waitFor(() => expect(next).toHaveAttribute("aria-disabled", start === 151 ? "true" : "false"));
  }
  expect(next).toHaveFocus();
  const row = section.getAllByRole("link");
  expect(row).toHaveLength(1);
  expect(row[0]).toHaveClass("rule");
  expect(row[0]).toHaveAttribute("href", collection === "upcoming" ? "?co=NORD&inv=synt150" : "?co=NORD&p=NORD.synt150");
  if (collection === "held_money") expect(row[0]).toHaveTextContent("15,10 lei");
  expect(calls.calls.filter((c) => c.url.includes("api/dashboard-list"))).toHaveLength(3);
  expect(fixtureDashboardData(full, collection, "0:49")).toEqual(dashboardResult(full, collection, "0:49"));
});

describe("Acasă's numbers", () => {
  it("shows încasat, cheltuit and rămas for this month, each opening Tranzacții for the period's days and money in or out", async () => {
    const s = fixture(); s.dashboard.through = "2026-10-24"; // the last account line before today
    renderApp({ state: () => json(s) });
    await loaded();
    const links = figures();
    expect(links.map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Încasat4.700,00lei", "?co=NORD&tab=tranzactii&f=in&from=2026-10-01&to=2026-10-27"],
      ["Cheltuit4.463,15lei", "?co=NORD&tab=tranzactii&f=out&from=2026-10-01&to=2026-10-27"],
      ["Sold net236,85lei", "?co=NORD&tab=tranzactii&f=all&from=2026-10-01&to=2026-10-27"],
      ["59 de tranzacții de explicat", "?co=NORD&tab=tranzactii"], // Tranzacții's own count, opening De explicat
    ]);
    expect(flows().getByText("Tranzacții până pe 24 oct.")).toBeInTheDocument();
    // the list Tranzacții opens is the lines the sum counts
    await userEvent.click(links[0]);
    expect(window.location.search).toBe("?co=NORD&tab=tranzactii&f=in&from=2026-10-01&to=2026-10-27");
    expect(parse(window.location.search)).toMatchObject({ tab: "tranzactii", f: "in", from: "2026-10-01", to: "2026-10-27" });
  });

  it("switches the period in the URL, and the numbers follow it: 3 luni, then the year with its euro apart", async () => {
    renderApp();
    await loaded();
    const period = within(screen.getByRole("navigation", { name: "Perioada" }));
    expect(period.getByRole("link", { name: "Luna curentă" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(period.getByRole("link", { name: "3 luni" }));
    expect(window.location.search).toBe("?co=NORD&per=3luni");
    expect(flows().getByRole("link", { name: /^Încasat/ })).toHaveTextContent("Încasat31.850,00lei");
    expect(flows().getByRole("link", { name: /^Încasat/ })).toHaveAttribute("href", "?co=NORD&tab=tranzactii&f=in&from=2026-08-01&to=2026-10-27");
    await userEvent.click(period.getByRole("link", { name: "Anul" }));
    expect(window.location.search).toBe("?co=NORD&per=an");
    expect(flows().getByRole("link", { name: /^Încasat/ })).toHaveTextContent("Încasat80.000,00leiși 1.680,00 EUR");
    expect(flows().getByRole("link", { name: /^Încasat/ })).toHaveAttribute("href", "?co=NORD&tab=tranzactii&f=in&from=2026-01-01&to=2026-10-27");
    expect(region("Furnizori principali").getAllByRole("link")[0]).toHaveTextContent("Mobilier Vechi SRL");
  });

  it("says where the account lines stop, or that they come with the first statement", async () => {
    const s = fixture();
    s.dashboard.through = null;
    renderApp({ state: () => json(s) });
    await loaded();
    expect(flows().getByText("Niciun extras încă.")).toBeInTheDocument();
  });

  it("under Toate firmele, a firm without account lines is named, not the whole install", async () => {
    at("?co=ALL");
    const s = fixtureAll();
    s.dashboard.through = null; // the engine's earliest firm: none while one firm has no account line
    const studio = s.parts.find((x) => x.co === "STUDIO9")!;
    studio.dashboard.through = null;
    s.parts.find((x) => x.co === "NORD")!.dashboard.through = "2026-10-24";
    renderApp({ all: () => json(s) });
    await loaded();
    expect(flows().queryByText("Niciun extras încă.")).toBeNull();
    expect(flows().getByText("Tranzacții până pe 24 oct.")).toBeInTheDocument();
    expect(flows().getByText(`Fără tranzacții în această perioadă: ${coName(studio)}.`)).toBeInTheDocument();
  });

  it("draws the period's months, each opening Tranzacții for it, with every number and its state in a table", async () => {
    at("?co=NORD&per=3luni");
    const s = withMonths(fixture(), [month("2026-08", "complete", "6900.00", "9400.00", { other: [{ currency: "EUR", total: "600.00" }] }),
      month("2026-09", "partial", "13350.00", "9884.80"), month("2026-10", "partial", "4700.00", "4463.15", { now: true })]);
    renderApp({ state: () => json(s) });
    await loaded();
    const chart = flows().getByRole("img", { name: "Încasat și cheltuit pe lună" });
    expect(chart).toHaveAccessibleDescription(/linie pentru soldul net al lunilor dovedite de extrase/);
    const cols = flows().getAllByRole("link").filter((a) => a.closest(".relative"));
    expect(cols.map((a) => a.getAttribute("href"))).toEqual(["08", "09", "10"].map((m) => `?co=NORD&tab=tranzactii&month=2026-${m}&f=all`));
    expect(cols[1]).toHaveAccessibleName("Septembrie 2026, încasat 13.350,00 lei, cheltuit 9.884,80 lei, sold net 3.465,20 lei, Lună incompletă");
    const table = flows().getByRole("table", { name: "Încasat, cheltuit și sold net pe lună" });
    expect(table.parentElement).toHaveClass("sr-only"); // a table keeps its width: the block around it is clipped
    expect(within(table).getAllByRole("row").slice(1).map((r) => r.textContent)).toEqual([
      "August 20266.900,00 lei și 600,00 EUR9.400,00 lei-2.500,00 lei și 600,00 EURDovedită de extrase",
      "Septembrie 202613.350,00 lei9.884,80 lei3.465,20 leiLună incompletă",
      "Octombrie 20264.700,00 lei4.463,15 lei236,85 leiLuna în curs",
    ]);
    // only a proven month has a point on the net line, and the months not proven are drawn lighter
    expect(chart.querySelectorAll("circle")).toHaveLength(1);
    expect(chart.querySelectorAll("g.faded")).toHaveLength(2);
    expect(flows().getByText("Graficul arată doar sumele în lei.")).toBeInTheDocument();
  });

  it.each([["partial", true], ["none", true], ["complete", false]] as const)(
    "says when the period's figures are not proven by complete statements (%s)", async (status, said) => {
      const s = fixture();
      s.dashboard.periods.month.cash_status = status;
      renderApp({ state: () => json(s) });
      await loaded();
      expect(flows().queryByText("Sumele arată doar tranzacțiile citite până acum.") !== null).toBe(said);
    });

  it("shows a month's tooltip on focus and hides it with Escape until the focus leaves", async () => {
    renderApp();
    await loaded();
    const col = flows().getAllByRole("link").filter((a) => a.closest(".relative"))[0];
    const tip = () => flows().queryByText(col.getAttribute("aria-label")!.split(",")[0], { selector: "b" });
    expect(tip()).toBeNull();
    act(() => col.focus());
    expect(tip()).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(tip()).toBeNull();
    await userEvent.tab();
    act(() => col.focus());
    expect(tip()).toBeInTheDocument();
    // the pointer alike: Escape hides the hovered month's tooltip wherever the focus is, until the pointer leaves
    act(() => col.blur());
    await userEvent.hover(col);
    expect(tip()).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(tip()).toBeNull();
    await userEvent.unhover(col.closest(".relative")!);
    await userEvent.hover(col);
    expect(tip()).toBeInTheDocument();
  });

  it("on touch, shows a month's tooltip on the first tap and opens its month on the second", async () => {
    renderApp();
    await loaded();
    const col = flows().getAllByRole("link").filter((a) => a.closest(".relative"))[0];
    const before = window.location.search;
    const tap = () => { fireEvent.pointerDown(col, { pointerType: "touch" }); act(() => col.focus()); fireEvent.click(col); };
    tap(); // a touch focuses the band before its click: the focus must not count as the first tap
    expect(window.location.search).toBe(before);
    expect(flows().getByText(col.getAttribute("aria-label")!.split(",")[0], { selector: "b" })).toBeInTheDocument();
    tap();
    expect(window.location.search).toBe(col.getAttribute("href"));
  });

  it("draws no months until one has statement lines", async () => {
    const s = withMonths(fixture(), [month("2026-09", "none", "0.00", "0.00"), month("2026-10", "none", "0.00", "0.00")]);
    renderApp({ state: () => json(s) });
    await loaded();
    expect(flows().queryByRole("img", { name: "Încasat și cheltuit pe lună" })).toBeNull();
  });

  it("keeps the period when the firm changes", async () => {
    at("?co=NORD&per=an");
    renderApp({ state: (url) => json(fixture(url.includes("STUDIO9") ? "STUDIO9" : "NORD")) });
    await loaded();
    const other = [...document.querySelectorAll(".comenu .menu a")].map((a) => a.getAttribute("href"));
    expect(other).toContain("?co=STUDIO9&per=an");
  });

  it("gives each account its balance from the statement that proves it, never a total across currencies", async () => {
    renderApp();
    await loaded();
    const rows = region("Conturi bancare").getAllByRole("link");
    expect(rows.map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Cont în lei•••• 0001, la 30 sep, din extras18.420,55 lei", "?co=NORD&tab=tranzactii"],
      ["Cont în EUR•••• 0001, la 30 sep, din extras1.240,00 EUR", "?co=NORD&tab=tranzactii"],
    ]);
  });

  it("says when no statement proves a balance", async () => {
    at("?co=STUDIO9");
    const s = fixture("STUDIO9");
    s.dashboard.balances = s.dashboard.balances.map((b) => ({ ...b, balance: null, day: null }));
    renderApp({ state: () => json(s) });
    await loaded();
    expect(region("Conturi bancare").getByRole("link")).toHaveTextContent("Cont în lei•••• 0002, disponibil după primul extras complet");
  });

  it("draws each account's proven closings on its own scale, with the same days in a table, and sums balances only per currency", async () => {
    const s = fixture();
    s.dashboard.charts.balance_history = [{ company: "NORD", iban_last4: "0001", currency: "EUR", bank: "bt",
      points: [{ day: "2026-08-31", balance: "900.00" }, { day: "2026-09-30", balance: "1240.00" }] }];
    s.dashboard.charts.balance_total = { total: "18420.55", other: [{ currency: "EUR", total: "1240.00" }], partial: true, day: "2026-09-30" };
    renderApp({ state: () => json(s) });
    await loaded();
    const charts = region("Conturi bancare").getAllByRole("img");
    expect(charts.map((c) => c.getAttribute("aria-describedby") && c.textContent)).toEqual(
      ["Soldul contului •••• 0001Soldurile de închidere din extrase, de pe 31 aug până pe 30 sep."]);
    const table = region("Conturi bancare").getByRole("table", { name: "Soldul contului •••• 0001" });
    expect(within(table).getAllByRole("row").slice(1).map((r) => r.textContent)).toEqual(["31 aug900,00 EUR", "30 sep1.240,00 EUR"]);
    // an account without a proven balance: the total says so beside its figure
    const tile = region("Solduri bancare");
    expect(tile.getByRole("link", { name: /^Solduri bancare/ })).toHaveTextContent("Solduri bancareIncomplet18.420,55leiși 1.240,00 EUR");
    expect(tile.getByText(/^Un cont fără sold din extras\./)).toBeInTheDocument();
  });

  it("keeps a balance's tooltip on its day while the pointer moves over the tooltip", async () => {
    const s = fixture();
    s.dashboard.charts.balance_history = [{ company: "NORD", iban_last4: "0001", currency: "RON", bank: "bt",
      points: [{ day: "2026-08-31", balance: "900.00" }, { day: "2026-09-30", balance: "1240.00" }] }];
    renderApp({ state: () => json(s) });
    await loaded();
    const area = region("Conturi bancare").getByRole("group", { name: "Soldul contului •••• 0001" });
    act(() => area.focus()); // the newest day
    const tip = area.querySelector("[data-tip]")!;
    expect(tip).toHaveTextContent("30 sep");
    fireEvent.pointerMove(tip, { clientX: 0 }); // x 0 is the first day: moving on the tooltip must not pick it
    expect(area.querySelector("[data-tip]")).toHaveTextContent("30 sep");
    fireEvent.pointerMove(area, { clientX: 0 });
    expect(area.querySelector("[data-tip]")).toHaveTextContent("31 aug");
  });

  it("opens the largest native-RON suppliers and clients on their partner sheets", async () => {
    renderApp();
    await loaded();
    const sup = region("Furnizori principali").getAllByRole("link");
    expect(sup[0]).toHaveTextContent("Mentenanta IT SRLo factură1.500,00 lei");
    expect(sup[0]).toHaveAttribute("href", "?co=NORD&p=NORD.1007");
    const cli = region("Clienți principali").getAllByRole("link");
    expect(cli.map((a) => a.textContent)).toEqual(
      ["Client Exemplu SRL6 facturi847,00 lei", "Client Grafic 0o factură650,00 lei",
        "Client Grafic 1o factură650,00 lei", "Client Grafic 4o factură650,00 lei", "Client Beta SRLo factură403,00 lei"]);
  });

  it("lists the next 30 days by date, each opening its invoice, a payment opening Plăți", async () => {
    const s = fixture();
    s.dashboard.upcoming.push({ company: "NORD", date: "2026-11-25", side: "out", kind: "payment", id: "op-9", who: "BUGETUL GENERAL CONSOLIDAT",
      number: "OP 9", amount: "610.00", currency: "RON" });
    renderApp({ state: () => json(s) });
    await loaded();
    const rows = region("Următoarele 30 de zile").getAllByRole("link");
    expect(rows[0]).toHaveTextContent("Telecom Rapid SRLScadentă în 2 zile, TR-2210120,00 leiDe plătit");
    expect(rows[0]).toHaveAttribute("href", "?co=NORD&inv=102");
    expect(rows[1]).toHaveTextContent("Client Beta SRL");
    expect(rows[1]).toHaveAttribute("href", "?co=NORD&iss=5002");
    expect(rows[1]).toHaveTextContent("De încasat");
    expect(rows.at(-1)).toHaveAttribute("href", "?co=NORD&tab=plati");
  });

  it("says there is nothing due when nothing is", async () => {
    const s = fixture();
    s.dashboard.upcoming = [];
    renderApp({ state: () => json(s) });
    await loaded();
    expect(region("Următoarele 30 de zile").getByText("Niciun element")).toBeInTheDocument();
  });

  it("counts the transactions de explicat as Tranzacții does, opening its De explicat", async () => {
    const s = fixture();
    expect(s.dashboard.to_explain).toBe(s.transactions.to_explain);
    renderApp({ state: () => json({ ...s, dashboard: { ...s.dashboard, to_explain: 3 } }) });
    await loaded();
    expect(flows().getByRole("link", { name: "3 tranzacții de explicat" })).toHaveAttribute("href", "?co=NORD&tab=tranzactii");
  });
});

describe("TVA estimată", () => {
  const vat = (month: string, net: string, partial: boolean, now = false): VatMonth => ({ month, estimate: true, basis: "recorded_invoices",
    in_progress: now, partial, missing_count: partial ? 4 : 0, collected: money("900.00"), deductible: money("400.00"),
    net: { total: net, other: now ? [{ currency: "EUR", total: "156.20" }] : [] } });

  it("labels VAT as an estimate from the recorded invoices, never the declaration, and says which invoices it lacks", async () => {
    const s = fixture();
    s.dashboard.charts.vat = [vat("2026-09", "500.00", false), vat("2026-10", "46.64", true, true)];
    renderApp({ state: () => json(s) });
    await loaded();
    const card = region("TVA colectată și deductibilă");
    expect(card.getByText("Estimare")).toBeInTheDocument();
    expect(card.getByText("Incomplet")).toBeInTheDocument(); // a month without every invoice's VAT
    expect(within(card.getByRole("table")).getAllByRole("row").map((r) => r.textContent)).toEqual([
      "TVASeptembrieOctombrie", "Colectată900,00 lei900,00 lei", "Deductibilă400,00 lei400,00 lei", "De plată500,00 lei46,64 lei și 156,20 EUR"]);
    expect(card.getByText("Din facturile înregistrate, nu din declarație. Octombrie: lipsesc datele de TVA din 4 facturi.")).toBeInTheDocument();
    // the tile: this month's estimate, with Incomplet while invoices lack VAT data
    expect(screen.getByRole("region", { name: "TVA estimată" })).toHaveTextContent("TVA estimatăIncomplet46,64leiși 156,20 EUROctombrie, luna în curs, fără datele din 4 facturi");
  });
});

describe("Open invoices by days late", () => {
  it("splits what is open into buckets that open their lists, late in red, Fără scadență only when it holds one", async () => {
    const s = fixture();
    const b = (bucket: AgingKey, count: number, total: string): AgingBucket => ({ bucket, count, total, other: [] });
    s.dashboard.charts.aging.receivables = { confirming: { count: 0, total: "0.00", other: [] },
      buckets: [b("not_due", 4, "645.00"), b("1_30", 3, "5405.00"), b("31_60", 0, "0.00"), b("over_60", 1, "200.00"), b("unknown_due", 0, "0.00")] };
    renderApp({ state: () => json(s) });
    await loaded();
    const card = region("Facturi emise, după scadență");
    const rows = card.getAllByRole("link");
    expect(rows.map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Nescadente4645,00 lei", "?co=NORD&tab=vanzari&f=open"],
      ["Restante 1-30 de zile35.405,00 lei", "?co=NORD&tab=vanzari&f=late"],
      ["Restante 31-60 de zile00,00 lei", "?co=NORD&tab=vanzari&f=late"],
      ["Restante peste 60 de zile1200,00 lei", "?co=NORD&tab=vanzari&f=late"],
    ]);
    expect(rows.map((a) => a.querySelector("b")!.classList.contains("text-danger"))).toEqual([false, true, false, true]);
    expect(card.getByRole("img", { name: /^Facturi emise, după scadență: nescadente 645,00 lei, restante 1-30 de zile 5\.405,00 lei/ })).toBeInTheDocument();
    expect(card.queryByText("Bara arată doar sumele în lei.")).toBeNull(); // only lei here; with euro the bar says so
  });
});

describe("Vânzări pe produse", () => {
  const row = (name: string, currency: string, revenue: string, unit = "H87", product_id: string | null = name): ProductSalesRow =>
    ({ product_id, name, currency, unit, revenue, quantity: "2", invoice_count: 3 });
  const sales = (company: string, totals: ProductSalesRow[], complete = true): ProductSales =>
    ({ company, complete, unavailable_count: complete ? 0 : 2, totals });

  it("ranks the period's products by revenue, one list per currency, units apart, and says what it lacks", async () => {
    at("?co=NORD&per=an");
    const s = fixture();
    s.dashboard.periods.year.product_sales = [sales("NORD", [row("Licență", "EUR", "900.00"), row("Fără produs", "RON", "120.00", "HUR", null),
      row("Consultanță", "RON", "4800.00", "HUR"), row("Fără produs", "RON", "60.00", "H87", null), row("Anulat", "RON", "0.00")], false)];
    renderApp({ state: () => json(s) });
    await loaded();
    const card = region("Vânzări pe produse");
    expect(card.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Consultanță3 facturi4.800,00 lei",
      "Fără produs (oră)3 facturi120,00 lei", "Fără produs (bucată)3 facturi60,00 lei", "Licență3 facturi900,00 EUR"]);
    expect(card.getByText("În EUR")).toBeInTheDocument(); // never added to the lei
    expect(card.getByText("Incomplet")).toBeInTheDocument();
    expect(card.getByText("Lipsesc rândurile din 2 facturi din SPV.")).toBeInTheDocument();
    expect(card.getByText("Anul")).toBeInTheDocument();
  });

  it("keeps a unit code it has no name for as it is, and draws no bar for a negative total", async () => {
    const s = fixture();
    s.dashboard.periods.month.product_sales = [sales("NORD", [row("Fără produs", "RON", "300.00", "toString", null),
      row("Fără produs", "RON", "100.00", "H87", null), row("Retur", "RON", "-50.00")])];
    renderApp({ state: () => json(s) });
    await loaded();
    const items = region("Vânzări pe produse").getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(["Fără produs (toString)3 facturi300,00 lei", "Fără produs (bucată)3 facturi100,00 lei", "Retur3 facturi-50,00 lei"]);
    expect(items.map((li) => li.querySelectorAll("i").length)).toEqual([1, 1, 0]);
  });

  it("names each firm's products under Toate firmele, and says nothing is missing when every firm is complete", async () => {
    at("?co=ALL");
    const v = fixtureAll();
    v.dashboard.periods.month.product_sales = [sales("NORD", [row("Consultanță", "RON", "800.00")]), sales("STUDIO9", [row("Fotografie", "RON", "1200.00")])];
    renderApp({ all: () => json(v) });
    await loaded();
    const card = region("Vânzări pe produse");
    const [nord, studio] = v.parts.map(coName);
    expect(card.getAllByRole("listitem").map((li) => li.textContent)).toEqual([`Fotografie${studio}, 3 facturi1.200,00 lei`, `Consultanță${nord}, 3 facturi800,00 lei`]);
    expect(card.queryByText("Incomplet")).toBeNull();
  });
});

describe("Acasă under Toate firmele", () => {
  it("adds the firms' sums per currency as figures, and gives every firm its own line that opens its Acasă", async () => {
    at("?co=ALL&per=an");
    const v = fixtureAll();
    renderApp({ all: () => json(v) });
    await loaded();
    expect(flows().queryAllByRole("link").filter((a) => a.closest(".figs"))).toEqual([]); // the list needs one firm
    expect(flows().queryAllByRole("link").filter((a) => a.closest(".relative"))).toEqual([]); // so do the months; the table carries the numbers
    expect(document.querySelector(".figs")!.children[0]).toHaveTextContent("Încasat118.550,00leiși 1.680,00 EUR"); // NORD + STUDIO9
    const firms = region("Pe firme").getAllByRole("link");
    expect(firms.map((a) => a.getAttribute("href"))).toEqual(["?co=NORD&per=an", "?co=STUDIO9&per=an"]);
    expect(firms[0]).toHaveTextContent(`NORDÎncasat ${sums(v.parts[0].dashboard.periods.year.in)}`);
    const balances = region("Conturi bancare").getAllByRole("link").map((a) => a.textContent);
    expect(balances).toEqual([expect.stringMatching(/^NORD, cont în lei/), expect.stringMatching(/^NORD, cont în EUR/),
      expect.stringMatching(/^STUDIO9, cont în lei/)]);
    expect(region("Furnizori principali").getAllByRole("link").every((a) => /^\?co=(NORD|STUDIO9)&p=/.test(a.getAttribute("href")!))).toBe(true);
  });

  it("names any number of firms, each with its own line", async () => {
    at("?co=ALL");
    const v = fixtureAll();
    const extra = ["NORD2", "SUD", "EST"].map((co) => ({ ...structuredClone(v.parts[1]), co }));
    v.parts.push(...extra);
    v.companies = [...v.companies, ...extra.map((p) => ({ key: p.co, name: p.co, legal_name: `${p.co} SRL`, palette: "slate", late: 0, logo: null }))];
    for (const p of v.parts) p.companies = v.companies;
    renderApp({ all: () => json(v) });
    await loaded();
    expect(region("Pe firme").getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(
      ["?co=NORD", "?co=STUDIO9", "?co=NORD2", "?co=SUD", "?co=EST"]);
  });
});

describe("the period and the money filters in the URL", () => {
  it("keeps per on Acasă only, and money in or out and the days on Tranzacții only", () => {
    expect(parse("?co=NORD&per=3luni").per).toBe("3luni");
    expect(parse("?co=NORD&per=an").per).toBe("an");
    expect(parse("?co=NORD&per=12").per).toBe("");
    expect(parse("?co=NORD&tab=vanzari&per=an").per).toBe("");
    expect(parse("?co=NORD&tab=tranzactii&f=in").f).toBe("in");
    expect(parse("?co=NORD&tab=vanzari&f=in").f).toBe("");
    expect(href({ co: "NORD", tab: "home", per: "an" })).toBe("?co=NORD&per=an");
    expect(parse("?co=NORD&tab=tranzactii&from=2026-08-01&to=2026-10-27")).toMatchObject({ from: "2026-08-01", to: "2026-10-27" });
    expect(parse("?co=NORD&tab=tranzactii&from=../x&to=2026-1-1")).toMatchObject({ from: "", to: "" });
    expect(parse("?co=NORD&tab=vanzari&from=2026-08-01").from).toBe("");
    expect(parse("?co=NORD&tab=tranzactii&from=2026-02-31&to=2026-99-99")).toMatchObject({ from: "", to: "" });
    expect(parse("?co=NORD&tab=tranzactii&from=2028-02-29").from).toBe("2028-02-29");
  });

  it("rounds the chart's scale to steps of 1, 2 or 5 times a power of ten, always holding zero", () => {
    expect(niceScale(0, 0)).toEqual({ lo: 0, hi: 1, ticks: [0, 1] });
    expect(niceScale(0, 13800)).toEqual({ lo: 0, hi: 15000, ticks: [0, 5000, 10000, 15000] });
    expect(niceScale(-2500, 9800)).toEqual({ lo: -5000, hi: 10000, ticks: [-5000, 0, 5000, 10000] });
    expect(niceScale(-900, -100).ticks).toEqual([-1000, -500, 0]);
  });

  it("says amounts per currency without adding them", () => {
    expect(sums({ total: "4200.00", other: [{ currency: "EUR", total: "600.00" }] })).toBe("4.200,00 lei și 600,00 EUR");
    expect(sums({ total: "0.00", other: [{ currency: "EUR", total: "900.00" }] })).toBe("900,00 EUR");
    expect(sums({ total: "0.00", other: [] })).toBe("0,00 lei");
    expect(sums({ total: "-350.00", other: [] })).toBe("-350,00 lei");
  });
});

describe("Pending amounts and urgent tasks", () => {
  it.each([
    ["1934.00", "În curs de confirmare21.934,00 lei și 20,00 EUR"],
    ["0.00", "În curs de confirmare220,00 EUR"],
  ])("shows what was marked paid without proof apart from the open buckets, in its currencies, with RON %s", async (confirming, expected) => {
    const s = fixture();
    s.dashboard.charts.aging.payables.confirming = { count: 2, total: confirming, other: [{ currency: "EUR", total: "20.00" }] };
    renderApp({ state: () => json(s) });
    await loaded();
    const line = region("Facturi primite, după scadență").getByRole("link", { name: /^În curs de confirmare/ });
    expect(line.textContent).toBe(expected);
    expect(line).toHaveAttribute("href", "?co=NORD&tab=cheltuieli");
  });

  it("shows De plătit and De încasat with their counts, each opening its list filtered", async () => {
    const s = fixture(); s.files = [paymentFile({ id: 9, total: "3000.00" }), paymentFile({ id: 8, total: "1871.00" }), paymentFile({ id: 5, status: "imported", total: "99.00" })];
    renderApp({ state: () => json(s) });
    await loaded();
    const pay = screen.getByRole("region", { name: "De plătit" });
    const collect = screen.getByRole("region", { name: "De încasat" });
    expect(within(pay).getByRole("link", { name: /^De plătit/ })).toHaveTextContent("De plătit9.392,30leiși 20,00 EUR");
    expect(within(pay).getByRole("link", { name: /^De plătit/ })).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&f=open");
    // what the open payment files hold; an imported file's money is in the bank already
    expect(within(pay).getByRole("link", { name: /^În fișiere de plăți/ })).toHaveAttribute("href", "?co=NORD&tab=plati");
    expect(within(pay).getByRole("link", { name: /^În fișiere de plăți/ })).toHaveTextContent("4.871,00 lei");
    expect(within(pay).getByRole("link", { name: /Scadente în 7 zile/ })).toHaveTextContent("2");
    // the overdue part: what the engine counts in `late`, with its amount (`late_total`)
    expect(within(pay).getByRole("link", { name: /restante/ })).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&f=late");
    expect(within(pay).getByRole("link", { name: /restante/ })).toHaveTextContent("6 restante4.701,30 lei");
    expect(within(collect).getByRole("link", { name: /restante/ })).toHaveTextContent("6 restante7.355,00 lei");
    expect(within(collect).getByRole("link", { name: /^De încasat/ })).toHaveTextContent("De încasat9.300,00leiși 900,00 EUR");
    expect(within(collect).queryByRole("link", { name: /^În curs de confirmare/ })).toBeNull();
    await userEvent.click(within(collect).getByRole("link", { name: /restante/ }));
    expect(window.location.search).toBe("?co=NORD&tab=vanzari&f=late");
    expect(await screen.findByRole("link", { name: /Client Alfa SRL.*nr\. NRD 0042/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Client Beta SRL.*nr\./ })).not.toBeInTheDocument();
  });

  it("shows the three most urgent tasks, then all of them", async () => {
    renderApp();
    await loaded();
    expect(document.querySelector(".home .lead")).toBeNull(); // no day line: the tasks and the numbers say it
    expect(screen.getByRole("heading", { name: /^Necesită atenție\s*7 urgente$/ })).toBeInTheDocument();
    expect([...document.querySelectorAll(".home .task .h b")].map((b) => b.textContent)).toEqual(
      ["Energie Furnizare SA: plată restantă", "Furnizor Grafic 1: plată restantă", "Furnizor Grafic 2: plată restantă"]);
    expect(document.querySelectorAll(".home .task .ic.late")).toHaveLength(3);
    await userEvent.click(screen.getByRole("link", { name: `Toate (${fixture().task_summary.total})` }));
    expect(window.location.search).toBe("?co=NORD&tab=sarcini");
    expect(document.querySelectorAll(".task")).toHaveLength(fixture().task_summary.total);
  });

  it("shows the first tasks under Sarcini when none is urgent", async () => {
    const s = fixture("STUDIO9");
    s.payable.late = 0;
    s.ambiguous = [];
    s.refused = [];
    s.unreadable = [];
    s.invoices = s.invoices.filter((r) => !(r.status === "open" && r.due_date && r.due_date < s.today));
    s.system.anaf = { ...s.system.anaf, authorized: true, error: null };
    s.system.mail.stale = false;
    at("?co=STUDIO9");
    renderApp({ state: () => json(s) });
    await loaded();
    const n = document.querySelectorAll(".home .task").length;
    expect(n).toBeGreaterThan(0);
    expect(document.querySelectorAll(".home .task .ic.late")).toHaveLength(0);
    expect(screen.getByRole("heading", { name: "Necesită atenție" })).toBeInTheDocument();
  });
});

describe("Configurează", () => {
  const setup = { left: [{ key: "bank", int: null }, { key: "history", int: null }], hidden: false } as const;
  const card = () => screen.findByRole("region", { name: "Configurează" });

  it("lists what is left for the firm, each line opening its place in Setări, and hides only after confirmation", async () => {
    const after = { ...fixture(), setup: { ...setup, hidden: true } };
    const api = renderApp({ state: () => json({ ...fixture(), setup }), onPost: () => json({ ok: true, message: "", state: after }) });
    const lines = within(await card()).getAllByRole("link");
    expect(lines.map((a) => [a.querySelector("b")?.textContent, a.getAttribute("href")])).toEqual([
      ["Banca", href({ ...parse(""), co: "NORD", tab: "setari", sec: "firme" }, "conn-NORD")],
      ["Facturi anterioare", href({ ...parse(""), co: "NORD", tab: "setari", sec: "firme" }, "hist-NORD")],
    ]);
    expect(lines.every((a) => a.querySelector("small")?.textContent)).toBe(true); // each says what it gives
    await userEvent.click(within(await card()).getByRole("button", { name: "Ascunde" })); // one tap, no question
    await waitFor(() => expect(screen.queryByRole("region", { name: "Configurează" })).toBeNull());
    expect(api.posts().map((p) => [p.url, p.body])).toEqual([["/web/api/setup-card-dismiss", { co: "NORD" }]]);
  });

  it("once hidden, Setări › Firme shows it again", async () => {
    at("?co=NORD&tab=setari&sec=firme");
    const hidden = { ...fixture(), setup: { ...setup, hidden: true } };
    const api = renderApp({ state: () => json(hidden), companies: () => json({ companies: [{ key: "NORD", name: "NORD", legal_name: "Atelier Nord SRL",
      cui: "11111110", palette: "amber", has_data: true }], palettes: ["amber"], limits: { key_pattern: "", reserved_keys: [], name_max: 100 } }),
    onPost: () => json({ ok: true, message: "", state: { ...hidden, setup } }) });
    await userEvent.click(await screen.findByRole("button", { name: "Arată cardul Configurează pentru NORD" }));
    expect(api.posts().map((p) => [p.url, p.body])).toEqual([["/web/api/setup-card-show", { co: "NORD" }]]);
  });

  it.each([["a member", "member", "?co=NORD"], ["Toate firmele", "admin", "?co=ALL"]] as const)("is not shown to %s", async (_, role, url) => {
    at(url);
    renderApp({ state: () => json({ ...fixture(), setup }), principal: fixtureIdentity(role) });
    await loaded();
    expect(screen.queryByRole("region", { name: "Configurează" })).toBeNull();
  });
});
