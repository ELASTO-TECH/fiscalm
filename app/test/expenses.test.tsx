// Cheltuieli lists purchases as the engine groups them (`expenses`): an invoice and the bon of the same purchase are one row,
// "Factură și bon", in both categories, and one sheet.
import { screen, within, act, waitFor } from "@testing-library/react";
import { renderSignedIn as render, markPaidAs, choose, choicesOf } from "./helpers";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import { AppCtx, Company, IdentityContext, type Ctx } from "@/components/context";
import { MarkPaid } from "@/components/MarkPaid";
import { parse } from "@/lib/nav";
import { SESSION_CHANGED, SESSION_ENDED } from "@/lib/api";
import { fixtureBrowseData, fixtureDetailData } from "@/lib/fixtures";
import { coName, type BrowseScreen, type DetailKind, type Invoice, type State } from "@/lib/types";
import { fixtureIdentity, browseResult, detailResult, summaryResult, deferred, becomeVisible, listLoaded, at, fixture, fixtureAll, json, paymentFile, server, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());

const rows = () => [...document.querySelectorAll(".card .row")] as HTMLElement[];
const names = () => rows().map((r) => r.querySelector("b")!.textContent);
const brico = (s: State) => s.documents.find((d) => d.vendor === "Brico Nord")!;

it.each(["invoice", "issued", "document", "mail"] as DetailKind[])("keeps populated %s sheet context identical to the engine", (kind) => {
  const full = fixtureAll(), s = full.parts.find((p) => p.co === "NORD")!;
  const id = kind === "invoice" ? "112" : kind === "document" ? brico(s).id : kind === "mail" ? s.to_check[0].key : "local-storno";
  const actual = fixtureDetailData(full, kind, id), expected = detailResult(full, { kind, id });
  for (const key of ["invoices", "issued", "documents", "expenses", "partners", "refused", "to_check", "payable", "receivable"] as const) expect(actual.state[key]).toEqual(expected.state[key]);
  expect(actual.related).toEqual(expected.related);
});

it("pages all credit notes without root history, preserving money and keyboard focus", async () => {
  const full = fixture(), invoice = full.invoices.find((r) => r.id === "102")!;
  invoice.credits = Array.from({ length: 151 }, (_, i) => ({ id: `credit-${i}`, number: `Storno-${i}`, amount: "-0.01" }));
  invoice.status = "linked"; invoice.payment_state = "held";
  at("?co=NORD&tab=cheltuieli&inv=102");
  const api = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, Object.fromEntries(new URL(url, "http://local").searchParams))) });
  const dialog = await screen.findByRole("dialog", { busy: false });
  expect(dialog).toHaveTextContent("De plătit");
  expect(dialog).toHaveAccessibleName(`${invoice.supplier}, 120,00 lei`);
  expect(within(dialog).getAllByText(/^Storno Storno-/)).toHaveLength(50);
  for (let i = 0; i < 3; i++) {
    await userEvent.click(within(dialog).getByRole("button", { name: "Pagina următoare" }));
    await waitFor(() => expect(within(dialog).queryByText("Se încarcă…")).toBeNull());
  }
  expect(within(dialog).getAllByText(/^Storno Storno-/)).toHaveLength(1);
  expect(dialog).toHaveTextContent("Storno-150");
  expect(within(dialog).getByRole("button", { name: "Pagina următoare" })).toHaveFocus();
  expect(api.posts()).toHaveLength(0);
});

it("ignores a closed document's late reply and retries an unavailable document", async () => {
  const full = fixture(), late = deferred<Response>();
  let fail = true;
  at("?co=NORD&tab=cheltuieli&inv=101");
  renderApp({ state: () => json(summaryResult(full)), browse: (url) => json(fixtureBrowseData(full, "cheltuieli", Object.fromEntries(new URL(url, "http://local").searchParams))), detail: (url) => {
    const query = Object.fromEntries(new URL(url, "http://local").searchParams);
    return query.id === "101" ? late.promise : fail ? json({}, 503) : json(detailResult(full, query));
  } });
  const loading = await screen.findByRole("dialog", { busy: true });
  expect(loading).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(await screen.findByRole("link", { name: /Telecom Rapid SRL/ }));
  await userEvent.click(await screen.findByRole("button", { name: "Reîncearcă" }));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  fail = false;
  await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
  const dialog = await screen.findByRole("dialog", { busy: false });
  expect(dialog).toHaveAccessibleName(/Telecom Rapid SRL/);
  await act(async () => late.resolve(json(detailResult(full, { kind: "invoice", id: "101" }))));
  expect(dialog).toHaveAccessibleName(/Telecom Rapid SRL/);
});

/** A document filed from a mail: Cheltuieli lists it under Facturi, never under Bonuri și chitanțe. */
const mailed = (s: State, id: string | null) => !!id && !!s.documents.find((d) => d.id === id)?.mail;

describe("Factură și bon", () => {
  it("is one row in Facturi and the same row in Bonuri și chitanțe, counted once", async () => {
    const s = fixture();
    const bon = brico(s);
    expect(s.expenses).toContainEqual({ id: "NORD:invoice:112", company: "NORD", invoice: "112", document: bon.id, sources: ["efactura", "upload"] });
    at("?co=NORD&tab=cheltuieli");
    renderApp();
    const row = (await screen.findByText("Brico Nord SRL")).closest(".row") as HTMLElement;
    expect(row).toHaveTextContent("Factură și bon, Plătită, 27 oct");
    expect(row).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&inv=112");
    const older = s.history.filter((r) => r.direction === "received").length; // invoices from before fiscalm, listed too
    expect(rows()).toHaveLength(s.expenses.filter((e) => e.invoice || mailed(s, e.document)).length + older);
    expect(rows().filter((r) => r.textContent!.includes("Factură și bon"))).toEqual([row]);

    await userEvent.click(screen.getByRole("link", { name: "Bonuri și chitanțe" }));
    await listLoaded();
    expect(screen.queryByText("Brico Nord")).toBeNull(); // the bon is no row of its own
    const same = screen.getByText("Brico Nord SRL").closest(".row") as HTMLElement;
    expect(same).toHaveTextContent("Factură și bon, Plătită, 27 oct");
    expect(same).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&side=bonuri&inv=112");
    expect(rows()).toHaveLength(s.expenses.filter((e) => e.document && !mailed(s, e.document)).length);
    expect(names()).toEqual(["Benzinaria Nord", "Brico Nord SRL", "Figma", "Google Workspace", "Hetzner", "GitHub"]);

    await userEvent.type(screen.getByRole("searchbox"), "brico");
    await listLoaded();
    expect(names()).toEqual(["Brico Nord SRL"]);
    await userEvent.clear(screen.getByRole("searchbox"));
    await listLoaded();
    await userEvent.type(screen.getByRole("searchbox"), "BN-5521");
    await listLoaded();
    expect(names()).toEqual(["Brico Nord SRL"]);
    await userEvent.clear(screen.getByRole("searchbox"));
    await listLoaded();
    await choose(screen.getByRole("combobox", { name: /Luna emiterii/ }), "2026-10");
    await listLoaded();
    expect(names()).toEqual(["Benzinaria Nord", "Brico Nord SRL", "Figma"]);
    await choose(screen.getByRole("combobox", { name: /Luna emiterii/ }), "");
    await listLoaded();

    await userEvent.click(screen.getByRole("link", { name: "Fără factură" }));
    await listLoaded();
    expect(names()).toEqual(["Benzinaria Nord", "Figma", "Google Workspace", "Hetzner", "GitHub"]);
  });

  it("opens the invoice's sheet with the bon on it, and each sheet leads to the other", async () => {
    const bon = brico(fixture());
    at("?co=NORD&tab=cheltuieli&side=bonuri");
    renderApp();
    await userEvent.click(await screen.findByText("Brico Nord SRL"));
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet).toHaveAccessibleName("Brico Nord SRL, 156,40 lei");
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&side=bonuri&inv=112");
    const img = within(sheet).getByRole("img", { name: "Document, Brico Nord" });
    expect(img).toHaveAttribute("src", `/web/docfile?co=NORD&sha=${bon.files[0].id}`);
    expect(img.closest("a")).toHaveAttribute("href", `/web/docfile?co=NORD&sha=${bon.files[0].id}`);
    expect(within(sheet).getAllByRole("link", { name: "Vezi bonul" })).toHaveLength(1);
    expect(sheet.querySelector("header + .st")).toHaveTextContent("Plătită");
    expect(within(sheet).queryByRole("button", { name: "Marchează ca plătită" })).toBeNull();
    expect(within(sheet).getByRole("link", { name: "Vezi bonul" })).toHaveAttribute("href", expect.stringContaining(`doc=${bon.id}`));

    expect(sheet).toHaveFocus();

    await userEvent.click(within(sheet).getByRole("link", { name: "Vezi bonul" }));
    expect(window.location.search).toBe(`?co=NORD&tab=cheltuieli&side=bonuri&doc=${bon.id}`);
    const own = await screen.findByRole("dialog", { name: "Brico Nord, 156,40 lei" });
    expect(own).toHaveFocus();
    expect(own).toHaveTextContent("FacturăCu factura BN-5521");
    const back = within(own).getByRole("link", { name: "Vezi factura" });
    expect(back).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&side=bonuri&inv=112");
    await userEvent.click(back);
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&side=bonuri&inv=112");
    expect(await screen.findByRole("dialog", { name: "Brico Nord SRL, 156,40 lei" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&side=bonuri");
    expect(screen.getByText("Brico Nord SRL").closest("a")).toHaveFocus(); // back on the row it was opened from
  });

  it("dates a purchase with both by its invoice's issue date, in Bonuri și chitanțe too", async () => {
    const s = fixture();
    expect(s.invoices.find((r) => r.id === "112")!.issue_date).toBe("2026-10-22");
    brico(s).date = "2026-09-30"; // paid at the till in September, invoiced in October
    brico(s).month = "2026-09";
    at("?co=NORD&tab=cheltuieli&side=bonuri");
    renderApp({ state: () => json(s) });
    await screen.findByText("Brico Nord SRL");
    const month = screen.getByRole("combobox", { name: /Luna emiterii/ });
    await choose(month, "2026-09");
    expect(names()).toEqual(["Google Workspace", "Hetzner"]);
    await choose(month, "2026-10");
    expect(names()).toEqual(["Benzinaria Nord", "Brico Nord SRL", "Figma"]);
  });

  it("still lists, on its own, an invoice or a receipt that no expense names", async () => {
    const s = fixture();
    const figma = s.documents.find((d) => d.vendor === "Figma")!;
    s.expenses = s.expenses.filter((e) => e.invoice !== "102" && e.document !== figma.id);
    expect(s.invoices.find((r) => r.id === "102")!.supplier).toBe("Telecom Rapid SRL");
    at("?co=NORD&tab=cheltuieli");
    renderApp({ state: () => json(s) });
    await screen.findByText("Brico Nord SRL");
    expect(names().filter((n) => n === "Telecom Rapid SRL")).toHaveLength(1);
    expect(rows()).toHaveLength(s.invoices.length + s.documents.filter((d) => d.mail).length + s.history.filter((r) => r.direction === "received").length);
    expect(screen.getByText("Telecom Rapid SRL").closest(".row")).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&inv=102");
    expect(screen.getByText("Telecom Rapid SRL").closest(".row")).not.toHaveTextContent("Factură și bon");
    await userEvent.type(screen.getByRole("searchbox"), "telecom");
    await listLoaded();
    expect(names()).toEqual(["Telecom Rapid SRL"]);
    await userEvent.clear(screen.getByRole("searchbox"));
    await listLoaded();

    await userEvent.click(screen.getByRole("link", { name: "Bonuri și chitanțe" }));
    await listLoaded();
    expect(names().filter((n) => n === "Figma")).toHaveLength(1);
    expect(rows()).toHaveLength(s.documents.filter((d) => !d.mail).length); // one per receipt; Brico Nord's is its invoice's row
    expect(screen.getByText("Figma").closest(".row")).toHaveAttribute("href", `?co=NORD&tab=cheltuieli&side=bonuri&doc=${figma.id}`);
    await choose(screen.getByRole("combobox", { name: /Luna emiterii/ }), "2026-10");
    await listLoaded();
    expect(names()).toContain("Figma");
    await userEvent.click(screen.getByRole("link", { name: "Fără factură" }));
    await listLoaded();
    expect(names()).toContain("Figma");
  });

  it("groups only what the engine groups in `expenses`, never by the invoice's or the bon's own links", async () => {
    const s = fixture();
    const bon = brico(s);
    const figma = s.documents.find((d) => d.vendor === "Figma")!;
    // The engine left the Brico pair separate (as it does for contradictory links) and grouped Figma with invoice 105
    s.expenses = s.expenses
      .filter((e) => e.invoice !== "112" && e.invoice !== "105" && e.document !== figma.id)
      .concat([
        { id: "NORD:invoice:112", company: "NORD", invoice: "112", document: null, sources: [] },
        { id: `NORD:document:${bon.id}`, company: "NORD", invoice: null, document: bon.id, sources: [] },
        { id: "NORD:invoice:105", company: "NORD", invoice: "105", document: figma.id, sources: [] },
      ]);
    expect(bon.invoice).toBe("112");
    expect(figma.invoice).toBeNull();
    at("?co=NORD&tab=cheltuieli");
    renderApp({ state: () => json(s) });
    expect((await screen.findByText("Brico Nord SRL")).closest(".row")).not.toHaveTextContent("Factură și bon");
    expect(screen.getByText("Servicii Web SRL").closest(".row")).toHaveTextContent("Factură și bon, În fișierul NORD plăți furnizori");
    await userEvent.click(screen.getByRole("link", { name: "Bonuri și chitanțe" }));
    await listLoaded();
    expect(screen.getByText("Brico Nord").closest(".row")).toHaveAttribute("href", `?co=NORD&tab=cheltuieli&side=bonuri&doc=${bon.id}`);
    expect(screen.queryByText("Figma")).toBeNull();
    expect(screen.getByText("Servicii Web SRL").closest(".row")).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&side=bonuri&inv=105");
    await userEvent.click(screen.getByRole("link", { name: "Fără factură" }));
    await listLoaded();
    expect(names()).toContain("Brico Nord");
    expect(names()).not.toContain("Servicii Web SRL");

    await userEvent.click(screen.getByText("Brico Nord"));
    const own = await screen.findByRole("dialog", { name: "Brico Nord, 156,40 lei" });
    expect(within(own).queryByRole("link", { name: "Vezi factura" })).toBeNull();
    expect(own).toHaveTextContent("FacturăFără factură");
    await userEvent.keyboard("{Escape}");

    // the invoice says it was paid at the till (its own status), but the engine didn't pair it: no bon on its sheet
    at("?co=NORD&tab=cheltuieli&inv=112");
    window.dispatchEvent(new PopStateEvent("popstate"));
    const sheet = await screen.findByRole("dialog", { name: "Brico Nord SRL, 156,40 lei" });
    expect(s.invoices.find((r) => r.id === "112")!.bon).toBe(bon.id);
    expect(sheet.querySelector("header + .st")).toHaveTextContent("Plătită");
    expect(within(sheet).queryByRole("link", { name: "Vezi bonul" })).toBeNull();
    expect(within(sheet).queryByRole("img")).toBeNull();
  });

  it("keeps each company's purchases apart under Toate firmele", async () => {
    const all = fixtureAll();
    at("?co=ALL&tab=cheltuieli&side=bonuri");
    renderApp();
    const row = (await screen.findByText("Brico Nord SRL")).closest(".row") as HTMLElement;
    expect(row).toHaveTextContent("Factură și bon");
    expect(row).toHaveTextContent("NORD, nr. BN-5521");
    expect(row).toHaveAttribute("href", "?co=ALL&tab=cheltuieli&side=bonuri&inv=112");
    expect(rows()).toHaveLength(all.parts.flatMap((p) => p.expenses.filter((e) => e.document && !mailed(p, e.document))).length);
    const farmacia = all.parts.find((p) => p.co === "STUDIO9")!.documents.find((d) => d.vendor === "Farmacia Sud")!;
    const other = screen.getByText("Farmacia Sud").closest(".row") as HTMLElement;
    expect(other).not.toHaveTextContent("Factură și bon"); // another total: not linked
    expect(other).toHaveAttribute("href", `?co=ALL&tab=cheltuieli&side=bonuri&doc=${farmacia.id}`);

    await userEvent.click(row);
    const sheet = await screen.findByRole("dialog", { name: "Brico Nord SRL, 156,40 lei" });
    const bon = brico(fixture());
    expect(within(sheet).getByRole("img", { name: "Document, Brico Nord" })).toHaveAttribute("src", `/web/docfile?co=NORD&sha=${bon.files[0].id}`);
    expect(within(sheet).getByRole("link", { name: "Vezi bonul" })).toHaveAttribute("href", `?co=ALL&tab=cheltuieli&side=bonuri&doc=${bon.id}`);
    await userEvent.keyboard("{Escape}");
    await userEvent.click(other);
    const studio9 = await screen.findByRole("dialog", { name: "Farmacia Sud, 48,00 lei" });
    expect(within(studio9).getByRole("img", { name: "Document, Farmacia Sud" })).toHaveAttribute("src", `/web/docfile?co=STUDIO9&sha=${farmacia.files[0].id}`);
    expect(within(studio9).queryByRole("link", { name: "Vezi factura" })).toBeNull();
  });
});

describe("Received invoices and payment confirmation", () => {
  it("lists received invoices and receipts as Cheltuieli's two categories, filtered, by month and by search", async () => {
    at("?co=NORD&tab=cheltuieli");
    renderApp();
    expect(await screen.findByText("Chirie Spatiu SRL")).toBeInTheDocument(); // paid by the bank: still listed
    expect(screen.queryByText("Figma")).toBeNull(); // a receipt: under Bonuri și chitanțe
    expect(screen.getByText("Energie Furnizare SA").closest(".row")).toHaveTextContent("De plătit, restantă de 3 zile");
    await userEvent.click(screen.getByRole("link", { name: "Restante" }));
    await listLoaded();
    expect([...document.querySelectorAll(".card .row b")].map((b) => b.textContent)).toEqual(["Energie Furnizare SA", "Papetarie Birou SRL", "Furnizor Grafic 1", "Furnizor Grafic 2", "Furnizor Grafic 3"]);
    await userEvent.click(screen.getByRole("link", { name: "De plătit" }));
    await listLoaded();
    expect(screen.queryByText("Chirie Spatiu SRL")).not.toBeInTheDocument();
    expect(screen.queryByText("Figma")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Toate" }));
    await listLoaded();
    await choose(screen.getByRole("combobox", { name: /Luna emiterii/ }), "2026-09");
    await listLoaded();
    expect([...document.querySelectorAll(".card .row b")].map((b) => b.textContent).sort())
      .toEqual(["Chirie Spatiu SRL", "Curierat Expres SRL", "Furnizor Grafic 2", "Mobilier Vechi SRL", "Tipografie Veche SRL"]);
    await choose(screen.getByRole("combobox", { name: /Luna emiterii/ }), "");
    await listLoaded();
    await userEvent.type(screen.getByRole("searchbox"), "cafea");
    await listLoaded();
    expect([...document.querySelectorAll(".card .row b")].map((b) => b.textContent)).toEqual(["Cafea Birou SRL"]);
    await userEvent.clear(screen.getByRole("searchbox"));
    await listLoaded();
    await userEvent.type(screen.getByRole("searchbox"), "zzz");
    await listLoaded();
    expect(screen.getByText("Niciun element")).toBeInTheDocument();
    await userEvent.clear(screen.getByRole("searchbox"));
    await listLoaded();
    await userEvent.click(screen.getByRole("link", { name: "Bonuri și chitanțe" }));
    await listLoaded();
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&side=bonuri");
    expect(screen.getByText("Figma").closest(".row")).toHaveTextContent("Fără factură, 3 oct");
    expect(screen.queryByText("Chirie Spatiu SRL")).toBeNull();
    expect(screen.queryByRole("link", { name: "Restante" })).toBeNull();
    await userEvent.click(screen.getByRole("link", { name: "Fără factură" }));
    await listLoaded();
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&side=bonuri&f=open");
    expect([...document.querySelectorAll(".card .row b")].map((b) => b.textContent).sort())
      .toEqual(fixture().documents.filter((d) => !d.invoice && !d.mail).map((d) => d.vendor).sort());
    await userEvent.click(screen.getByRole("link", { name: "Toate" }));
    await listLoaded();
    await choose(screen.getByRole("combobox", { name: /Luna emiterii/ }), "2026-09");
    await listLoaded();
    expect([...document.querySelectorAll(".card .row b")].map((b) => b.textContent).sort()).toEqual(["Google Workspace", "Hetzner"]);
    await choose(screen.getByRole("combobox", { name: /Luna emiterii/ }), "");
    await listLoaded();
    await userEvent.type(screen.getByRole("searchbox"), "figma");
    await listLoaded();
    expect([...document.querySelectorAll(".card .row b")].map((b) => b.textContent)).toEqual(["Figma"]);
    await userEvent.clear(screen.getByRole("searchbox"));
    await listLoaded();
    await userEvent.type(screen.getByRole("searchbox"), "zzz");
    await listLoaded();
    expect(screen.getByText("Niciun element")).toBeInTheDocument();
  });

  it("leaves an imported payment out of De plătit: it only waits for the statement", async () => {
    at("?co=NORD&tab=cheltuieli");
    const imported = fixture();
    imported.invoices.find((r) => r.id === "105")!.payment_state = "imported"; // the user imported the file in BT Go
    const states = [fixture(), imported];
    renderApp({ state: () => json(states.shift() ?? imported) });
    expect((await screen.findByText("Curierat Expres SRL")).closest(".row")).toHaveTextContent("Importată în bancă, se așteaptă extrasul");
    await userEvent.click(screen.getByRole("link", { name: "De plătit" }));
    await listLoaded();
    const shown = () => [...document.querySelectorAll(".card .row b")].map((b) => b.textContent);
    expect(shown()).not.toContain("Curierat Expres SRL");
    expect(shown()).toContain("Servicii Web SRL"); // in the file, not imported yet
    act(() => becomeVisible());
    await waitFor(() => expect(shown()).not.toContain("Servicii Web SRL"));
    await waitFor(() => expect(shown()).toContain("Mentenanta IT SRL")); // held for the statement: still to pay
  });

  it("shows an invoice the user marked paid as awaiting confirmation, outside De plătit", async () => {
    at("?co=NORD&tab=cheltuieli");
    renderApp();
    const row = () => screen.queryByText("Cafea Birou SRL")?.closest(".row");
    expect((await screen.findByText("Cafea Birou SRL")).closest(".row")).toHaveTextContent("În curs de confirmare, 27 oct");
    expect(row()).not.toHaveTextContent("pe extras");
    await userEvent.click(screen.getByRole("link", { name: "De plătit" }));
    await listLoaded();
    expect(row()).toBeUndefined();
    await userEvent.click(screen.getByRole("link", { name: "Restante" }));
    await listLoaded();
    expect(row()).toBeUndefined();
    await userEvent.click(screen.getByRole("link", { name: "Toate" }));
    await listLoaded();
    await userEvent.click(screen.getByText("Cafea Birou SRL"));
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet.querySelector("header + .st")).toHaveTextContent("În curs de confirmare, 27 oct");
    expect(within(sheet).getByRole("button", { name: "Anulează marcarea" })).toBeInTheDocument();
  });

  it("takes a received invoice out of the BT Go file, and undoes a marking", async () => {
    at("?co=NORD&tab=cheltuieli");
    const api = server();
    render(<App />);
    await userEvent.click(await screen.findByText("Servicii Web SRL"));
    await userEvent.click(within(await screen.findByRole("dialog", { busy: false })).getByRole("button", { name: "Marchează ca plătită" }));
    expect(await markPaidAs("Numerar")).toHaveTextContent("Marcarea scoate factura din fișierul de plăți.");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/withdraw", method: "POST", body: { co: "NORD", id: "105", method: "cash", detail: "" } });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await userEvent.click(screen.getByText("Cafea Birou SRL"));
    await userEvent.click(within(await screen.findByRole("dialog", { busy: false })).getByRole("button", { name: "Anulează marcarea" }));
    await waitFor(() => expect(api.posts()[1]).toEqual({ url: "/web/api/reopen", method: "POST", body: { co: "NORD", id: "108" } }));
  });

  it("names the payment file an invoice is in and opens it", async () => {
    const s = fixture(), r = s.invoices.find((x) => x.id === "105")!;
    Object.assign(r, { status: "linked", payment_state: "in_file", payment_file: { id: 7, name: "NORD furnizori" } });
    at("?co=NORD&tab=cheltuieli&inv=105");
    renderApp({ state: () => json(s) });
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet).toHaveTextContent("În fișierul NORD furnizori");
    expect(within(sheet).getByRole("link", { name: "Vezi fișierul" })).toHaveAttribute("href", "?co=NORD&tab=plati&pf=7");
  });

  // Marking paid, withdrawing and undoing a mark stay in the ledger: Sandbox offers them as Live does (the engine's can_*),
  // never the payment file, an outward effect.
  it.each([["102", "Marchează ca plătită"], ["103", "Marchează ca plătită"], ["105", "Marchează ca plătită"], ["108", "Anulează marcarea"]])(
    "offers invoice %s's local action %s in Sandbox", async (id, label) => {
      at(`?co=NORD&tab=cheltuieli&inv=${id}`);
      const sandbox = fixture();
      sandbox.live = false;
      sandbox.invoices.forEach((r) => { r.can_approve = false; });
      renderApp({ state: () => json(sandbox) });
      const sheet = await screen.findByRole("dialog", { busy: false });
      expect(within(sheet).getByRole("button", { name: label })).toBeEnabled();
      expect(within(sheet).queryByRole("button", { name: "Pune în fișier" })).toBeNull();
    });

  it.each([["102", "RON"], ["103", "EUR"]])("offers a payment file only for lei; marking %s (%s) is always there", async (id, currency) => {
    at(`?co=NORD&tab=cheltuieli&inv=${id}`);
    renderApp();
    const sheet = await screen.findByRole("dialog", { busy: false });
    if (currency === "RON") expect(within(sheet).getByRole("button", { name: "Pune în fișier" })).toBeEnabled();
    else {
      expect(within(sheet).queryByRole("button", { name: "Pune în fișier" })).not.toBeInTheDocument();
      expect(sheet).toHaveTextContent("Fișierul de plăți e doar în lei.");
    }
    expect(within(sheet).getByRole("button", { name: "Marchează ca plătită" })).toBeInTheDocument();
  });
});

describe("Choosing invoices to pay in Cheltuieli", () => {
  const box = (name: string) => screen.getByRole("checkbox", { name: new RegExp(`^Alege ${name}`) });
  const choose = async (f = "open", options: Parameters<typeof renderApp>[0] = {}) => {
    at(`?co=NORD&tab=cheltuieli&f=${f}`);
    const api = renderApp(options);
    await listLoaded();
    return api;
  };
  const put = () => userEvent.click(within(screen.getByRole("region", { name: "Facturi alese" })).getByRole("button", { name: "Pune în fișier" }));

  /** NORD with two open files, the newest the engine's default. */
  const withFiles = () => {
    const s = fixture();
    // A file the engine asks about (its download gone from the folder) takes nothing until answered: never offered.
    s.files = [paymentFile({ id: 9, name: "NORD plăți 27 oct 2026", downloaded_at: "2026-10-27T10:00:00" }), paymentFile({ id: 7, name: "NORD furnizori" }),
      paymentFile({ id: 6, name: "NORD fișier lipsă", question: "missing" })];
    s.payment_file_defaults = [{ co: "NORD", name: "NORD plăți 27 oct 2026", open_file_id: 9 }];
    return s;
  };
  const ok = (s: State) => () => json({ ok: true, message: "", state: s });
  const chooser = () => screen.findByRole("dialog", { name: /^(Pune în fișier|Fișier nou)$/ });

  it("puts the chosen lei invoices in the newest open file with one request, and names the one the engine keeps out", async () => {
    const s = withFiles();
    const api = await choose("open", { state: () => json(s), onPost: ok(s) });
    // In a file, held or in a lost file: already on their way, so they can't be chosen.
    for (const name of ["Servicii Web SRL", "Mentenanta IT SRL", "Papetarie Birou SRL"]) expect(box(name)).toBeDisabled();
    for (const name of ["Telecom Rapid SRL", "Energie Furnizare SA", "Software Licente SRL"]) await userEvent.click(box(name));
    const bar = screen.getByRole("region", { name: "Facturi alese" });
    expect(bar).toHaveTextContent("3 facturi506,40 lei + 20,00 EUR");
    await put();
    const dialog = await chooser();
    expect(within(dialog).getAllByRole("radio").map((r) => [r.closest("label")!.querySelector("b, .font-semibold")!.textContent, (r as HTMLInputElement).checked]))
      .toEqual([["NORD plăți 27 oct 2026", true], ["NORD furnizori", false], ["Fișier nou", false]]);
    expect(dialog).toHaveTextContent("Software Licente SRL, nr. SL-0091: Fișierul de plăți e doar în lei.");
    expect(dialog).toHaveTextContent("Fișierul a fost descărcat. După ce adaugi facturile, descarcă-l din nou.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Pune în fișier" }));
    await waitFor(() => expect(api.posts().map((c) => [c.url, c.body])).toEqual([
      ["/web/api/payment-files", { action: "payment_file_add", file_id: 9, invoice_ids: ["102", "101"] }]]));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Facturi alese" })).toBeNull());
  });

  it("makes a new file with the proposed name, as typed, instead of an open one", async () => {
    const s = withFiles();
    const api = await choose("open", { state: () => json(s), onPost: ok(s) });
    await userEvent.click(box("Telecom Rapid SRL"));
    await put();
    const dialog = await chooser();
    await userEvent.click(within(dialog).getByRole("radio", { name: /^Fișier nou/ }));
    const name = within(dialog).getByRole("textbox", { name: "Nume" });
    expect(name).toHaveValue("NORD plăți 27 oct 2026");
    await userEvent.clear(name);
    await userEvent.type(name, "Furnizori noiembrie");
    await userEvent.click(within(dialog).getByRole("button", { name: "Pune în fișier" }));
    await waitFor(() => expect(api.posts().map((c) => c.body)).toEqual([
      { action: "payment_file_create", co: "NORD", name: "Furnizori noiembrie", invoice_ids: ["102"] }]));
  });

  it("asks again when the chosen file is imported elsewhere meanwhile, and sends nothing until then", async () => {
    let s = withFiles();
    const api = await choose("open", { state: () => json(s), onPost: ok(s) });
    await userEvent.click(box("Telecom Rapid SRL"));
    await put();
    const dialog = await chooser();
    await userEvent.click(within(dialog).getByRole("radio", { name: /^NORD furnizori/ }));
    s = structuredClone(s);
    s.files = s.files.map((f) => f.id === 7 ? { ...f, status: "imported", imported_at: "2026-10-27" } : f);
    act(() => becomeVisible());
    await waitFor(() => expect(within(dialog).queryByRole("radio", { name: /^NORD furnizori/ })).toBeNull());
    expect(within(dialog).getAllByRole("radio").some((r) => (r as HTMLInputElement).checked)).toBe(false);
    expect(within(dialog).getByRole("button", { name: "Pune în fișier" })).toBeDisabled();
    expect(api.posts()).toEqual([]);
  });

  it("with no open file asks only for the new file's name", async () => {
    const s = fixture();
    s.files = [paymentFile({ status: "imported", imported_at: "2026-10-20" })];
    s.payment_file_defaults = [{ co: "NORD", name: "NORD plăți 27 oct 2026", open_file_id: null }];
    const api = await choose("open", { state: () => json(s), onPost: ok(s) });
    await userEvent.click(box("Telecom Rapid SRL"));
    await put();
    const dialog = await screen.findByRole("dialog", { name: "Fișier nou" });
    expect(within(dialog).queryAllByRole("radio")).toEqual([]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Pune în fișier" }));
    await waitFor(() => expect(api.posts().map((c) => c.body)).toEqual([
      { action: "payment_file_create", co: "NORD", name: "NORD plăți 27 oct 2026", invoice_ids: ["102"] }]));
  });

  it("marks the chosen invoices paid through Cum ai plătit?, each with its own POST", async () => {
    const s = fixture();
    const api = await choose("late", { state: () => json(s), onPost: () => json({ ok: true, message: "", state: s }) });
    await userEvent.click(box("Energie Furnizare SA"));
    await userEvent.click(within(screen.getByRole("region", { name: "Facturi alese" })).getByRole("button", { name: "Marchează ca plătită" }));
    const dialog = await screen.findByRole("dialog", { name: "Cum ai plătit?" });
    await userEvent.click(within(dialog).getByRole("radio", { name: /^Numerar/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Marchează ca plătită" }));
    await waitFor(() => expect(api.posts().map((c) => [c.url, c.body])).toEqual([["/web/api/mark-paid", { co: "NORD", id: "101", method: "cash", detail: "" }]]));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Facturi alese" })).toBeNull());
  });

  it("puts invoices in a file in Sandbox too, a local change, and offers no boxes under Toate", async () => {
    const sandbox = withFiles();
    sandbox.live = false;
    await choose("open", { state: () => json(sandbox) });
    await userEvent.click(box("Telecom Rapid SRL"));
    expect(within(screen.getByRole("region", { name: "Facturi alese" })).getByRole("button", { name: "Pune în fișier" })).toBeEnabled();
    await userEvent.click(screen.getByRole("link", { name: "Toate" }));
    await listLoaded();
    expect(screen.queryAllByRole("checkbox")).toEqual([]);
  });

  it("drops a chosen invoice the engine put in the file meanwhile, and sends only the others", async () => {
    const s = withFiles();
    const api = await choose("open", { state: () => json(s), onPost: ok(s) });
    for (const name of ["Telecom Rapid SRL", "Energie Furnizare SA"]) await userEvent.click(box(name));
    Object.assign(s.invoices.find((r) => r.id === "102")!, { status: "linked", payment_state: "in_file", can_approve: false, can_mark_paid: false });
    becomeVisible();
    await waitFor(() => expect(box("Telecom Rapid SRL")).toBeDisabled());
    expect(screen.getByRole("region", { name: "Facturi alese" })).toHaveTextContent("O factură386,40 lei");
    await put();
    await userEvent.click(within(await chooser()).getByRole("button", { name: "Pune în fișier" }));
    await waitFor(() => expect(api.posts().map((c) => c.body)).toEqual([{ action: "payment_file_add", file_id: 9, invoice_ids: ["101"] }]));
  });
});

describe("Bonuri", () => {
  it("opens a photographed purchase with its photo, its date, card and company", async () => {
    at("?co=NORD&tab=cheltuieli&side=bonuri");
    renderApp();
    const rows = await screen.findAllByText("Benzinaria Nord");
    expect(rows).toHaveLength(1);
    await userEvent.click(rows[0]);
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet).toHaveAccessibleName("Benzinaria Nord, 300,08 lei");
    const imgs = within(sheet).getAllByRole("img");
    expect(imgs.map((i) => i.getAttribute("alt"))).toEqual(["Document, Benzinaria Nord"]);
    const d = fixture().documents.find((x) => x.vendor === "Benzinaria Nord")!;
    expect(imgs[0]).toHaveAttribute("src", `/web/docfile?co=NORD&sha=${d.files[0].id}`);
    expect(imgs[0].closest("a")).toHaveAttribute("href", `/web/docfile?co=NORD&sha=${d.files[0].id}`);
    expect(sheet).toHaveTextContent("Data25 oct");
    expect(sheet).not.toHaveTextContent("Cu cardul");
    expect(sheet).toHaveTextContent("FirmaNORD");
    expect(sheet).toHaveTextContent("ContabilIntră în raportul pe octombrie");
    expect(window.location.search).toContain(`doc=${d.id}`);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(window.location.search).not.toContain("doc=");
  });

  it("lists a mailed invoice paid by card as an invoice and never calls it missing", async () => {
    const full = fixture(), d = full.documents.find((x) => x.mail)!;
    Object.assign(d, { card: "4242", number: "SYN-A1234", mail: { ...d.mail, number: "SYN-A1234" } });
    at("?co=NORD&tab=cheltuieli");
    renderApp({ state: () => json(full) });
    const row = (await screen.findByText(d.vendor)).closest(".row")!;
    expect(row).toHaveTextContent(/card terminat în 4242/i);
    await userEvent.click(row);
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet).not.toHaveTextContent("Fără factură");
    expect(sheet).toHaveTextContent("NumărSYN-A1234");
    expect(sheet).toHaveTextContent("PlatăCard terminat în 4242");
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("link", { name: "Bonuri și chitanțe" }));
    await listLoaded();
    expect(screen.queryByText(d.vendor)).toBeNull();
  });

  it("shows a mailed PDF as a picture of its first page, with the card's last digits", async () => {
    const d = fixture().documents.find((x) => x.vendor === "Hetzner")!;
    at(`?co=ALL&tab=cheltuieli&side=bonuri&doc=${d.id}`);
    renderApp();
    const sheet = await screen.findByRole("dialog", { busy: false });
    const img = within(sheet).getByRole("img", { name: "Document, Hetzner" });
    expect(img).toHaveAttribute("src", `/web/docfile?co=NORD&sha=${d.id}&png=1`);
    expect(img.closest("a")).toHaveAttribute("href", `/web/docfile?co=NORD&sha=${d.id}`);
    expect(sheet).toHaveTextContent("PlatăCard terminat în 4242");
  });

  it("says so when the file is not on the server", async () => {
    at("?co=NORD&tab=cheltuieli&side=bonuri");
    renderApp();
    await userEvent.click(await screen.findByText("Figma"));
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(within(sheet).queryByRole("img")).not.toBeInTheDocument();
    expect(sheet).toHaveTextContent("Fișierul lipsește de pe server.");
  });

  it("names an upload by its day while the shop is unknown, and says it waits for its payment", async () => {
    const s = fixture(), d = s.documents.find((x) => x.vendor === "Benzinaria Nord")!;
    Object.assign(d, { vendor: "", date: "2026-10-06", held: true, sent: false });
    at(`?co=NORD&tab=cheltuieli&side=bonuri&doc=${d.id}`);
    renderApp({ state: () => json(s) });
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet).toHaveAccessibleName("Bon din 6 oct, 300,08 lei");
    expect(sheet).toHaveTextContent("ContabilNu intră în dosarul contabilului până nu este asociat cu plata din Tranzacții");
  });

  it("shows an amount not read yet as unknown, never 0,00: in the list and on its sheet", async () => {
    const s = fixture(), d = s.documents.find((x) => x.vendor === "Benzinaria Nord")!;
    Object.assign(d, { vendor: "", date: "2026-10-06", amount: null });
    at("?co=NORD&tab=cheltuieli&side=bonuri");
    renderApp({ state: () => json(s) });
    const row = (await screen.findByText("Bon din 6 oct")).closest("a")!;
    expect(row).toHaveTextContent("Sumă necunoscută");
    expect(row).not.toHaveTextContent("0,00");
    await userEvent.click(row);
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet).toHaveAccessibleName("Bon din 6 oct, Sumă necunoscută");
    expect(within(sheet).getByRole("heading", { level: 2, name: "Sumă necunoscută" })).toBeInTheDocument();
  });
});


it("edits receipt lines and posts the exact revision without confirming payment", async () => {
  const s = fixture();
  const d = s.documents.find((x) => !x.invoice && !x.mail)!;
  d.items = { lines: [{ name: "Cablu USB 2", unit: "H87", quantity: "2", net: "100.00", vat: "21" }],
    total: "121.00", currency: "RON", payment: "cash", revision: 3, provenance: "ai", verified: false, state: "to_check", proposal: null, payment_mismatch: true };
  at(`?co=NORD&tab=cheltuieli&doc=${d.id}`);
  const api = renderApp({ state: () => json(s) });
  const sheet = await screen.findByRole("dialog", { busy: false });
  expect(within(sheet).getByRole("alert")).toHaveTextContent("numerar");
  await userEvent.clear(within(sheet).getByRole("textbox", { name: "Denumire" }));
  await userEvent.type(within(sheet).getByRole("textbox", { name: "Denumire" }), "Cablu USB 3");
  await userEvent.click(within(sheet).getByRole("button", { name: "Salvează" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", id: d.id, revision: 3, payload: JSON.stringify({ lines: [{ name: "Cablu USB 3", unit: "H87", quantity: "2", net: "100.00", vat: "21" }], total: "121.00", currency: "RON", payment: "cash" }) });
  expect(api.posts()[0].url).toContain("receipt-lines-save");
});

it("starts from the receipt's total, currency and card, items folded away, and saves only when asked", async () => {
  const s = fixture();
  const d = s.documents.find((x) => x.vendor === "Hetzner")!;
  d.items = null;
  at(`?co=NORD&tab=cheltuieli&doc=${d.id}`);
  const api = renderApp({ state: () => json(s) });
  const sheet = await screen.findByRole("dialog", { busy: false });
  expect(within(sheet).getByRole("textbox", { name: "Total cu TVA" })).toHaveValue(d.amount);
  expect(within(sheet).getByRole("textbox", { name: "Monedă" })).toHaveValue(d.currency);
  expect(within(sheet).getByRole("combobox", { name: "Plată" })).toHaveValue("card");
  expect(within(sheet).queryByRole("textbox", { name: "Denumire" })).toBeNull();
  expect(api.posts()).toEqual([]);
  await userEvent.click(within(sheet).getByRole("button", { name: "Completează articolele (opțional)" }));
  expect(within(sheet).getByRole("textbox", { name: "Denumire" })).toHaveFocus();
  await userEvent.click(within(sheet).getByRole("button", { name: "Salvează" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", id: d.id, revision: 0, payload: JSON.stringify({ lines: [], total: d.amount, currency: d.currency, payment: "card" }) });
});

it("shows a saved receipt as saved, never the receipt's own values over it", async () => {
  const s = fixture();
  const d = s.documents.find((x) => x.vendor === "Hetzner")!;
  d.items = { lines: [], total: "", currency: "EUR", payment: "", revision: 1, provenance: "user", verified: true, state: "to_check", proposal: null, payment_mismatch: false };
  at(`?co=NORD&tab=cheltuieli&doc=${d.id}`);
  renderApp({ state: () => json(s) });
  const sheet = await screen.findByRole("dialog", { busy: false });
  expect(within(sheet).getByRole("textbox", { name: "Total cu TVA" })).toHaveValue("");
  expect(within(sheet).getByRole("combobox", { name: "Plată" })).toHaveValue("");
});

it("confirms only the displayed receipt association", async () => {
  const s = fixture();
  const d = s.documents.find((x) => !x.invoice && !x.mail)!;
  d.items = { lines: [{ name: "Monitor", unit: "H87", quantity: "1", net: "1239.67", vat: "21" }], total: "1500.00", currency: "RON", payment: "", revision: 2, provenance: "user", verified: true, state: "proposed", proposal: s.invoices[0].id, payment_mismatch: false };
  at(`?co=NORD&tab=cheltuieli&doc=${d.id}`);
  const api = renderApp({ state: () => json(s) });
  const sheet = await screen.findByRole("dialog", { busy: false });
  const confirm = within(sheet).getByRole("button", { name: "Confirmă asocierea" });
  expect(within(sheet).getByText(`Corespunde facturii ${s.invoices[0].number}`)).toBeInTheDocument();
  expect(confirm.compareDocumentPosition(within(sheet).getByRole("button", { name: "Salvează" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  await userEvent.type(within(sheet).getByRole("textbox", { name: "Total cu TVA" }), "1");
  expect(within(sheet).queryByRole("button", { name: "Confirmă asocierea" })).toBeNull();
  await userEvent.type(within(sheet).getByRole("textbox", { name: "Total cu TVA" }), "{Backspace}");
  await userEvent.click(within(sheet).getByRole("button", { name: "Confirmă asocierea" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", id: d.id, revision: 2, invoice: s.invoices[0].id });
  expect(api.posts()[0].url).toContain("receipt-invoice-confirm");
});


it.each(["vanzari", "cheltuieli", "bonuri", "parteneri"] as BrowseScreen[])("keeps the %s fixture selector identical to the engine page", (screen) => {
  const v = fixtureAll();
  for (const query of [{}, { f: "open" }, { q: "RO 1001" }, { q: "brico" }, { cursor: "2026-10-22:1" }]) {
    expect(fixtureBrowseData(v, screen, query)).toEqual(browseResult(v, screen, query));
  }
});

it("reads purchases without root history, ignores obsolete replies and retries a failed month", async () => {
  const full = fixture(), shell = { ...full, invoices: [], documents: [], expenses: [] };
  const obsolete = deferred<Response>();
  let failMonth = true;
  at("?co=NORD&tab=cheltuieli");
  const api = renderApp({ state: () => json(shell), browse: (url) => {
    const q = Object.fromEntries(new URL(url, "http://local").searchParams);
    if (q.q === "old") return obsolete.promise;
    if (q.month === "2026-09" && failMonth) return json({}, 503);
    return json(fixtureBrowseData(full, "cheltuieli", q));
  } });
  const initial = await screen.findByRole("link", { name: /Energie Furnizare SA/ });
  expect(initial).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&inv=101");
  const search = screen.getByRole("searchbox");
  await userEvent.type(search, "old");
  await waitFor(() => expect(api.calls.some((c) => c.url.includes("q=old"))).toBe(true));
  expect(screen.queryByText("Niciun element")).toBeNull();
  await userEvent.clear(search);
  await listLoaded();
  await act(async () => obsolete.resolve(json({ data: [], total: 0, months: [], next_cursor: null })));
  expect(screen.getByRole("link", { name: /Energie Furnizare SA/ })).toHaveAttribute("href", initial.getAttribute("href"));
  await choose(screen.getByRole("combobox"), "2026-09");
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect((await choicesOf(screen.getByRole("combobox"))).map((o) => o.text)).toContain("Octombrie 2026");
  failMonth = false;
  await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
  await listLoaded();
  expect(screen.getByRole("link", { name: /Chirie Spatiu SRL/ })).toBeInTheDocument();
  expect(document.activeElement).not.toBe(document.body);
});

describe("Încarcă bon sau factură", () => {
  const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 0x10]);
  const photo = (name: string, bytes: Uint8Array<ArrayBuffer> = JPEG) => new File([bytes], name, { type: "image/jpeg" });
  const pick = (files: File[], within: ParentNode = document) => userEvent.upload(within.querySelector("input[type=file]") as HTMLInputElement, files);
  const sent = (api: ReturnType<typeof server>) => api.posts().map((c) => ({ url: c.url, files: (c.raw as FormData).getAll("files").map((f) => (f as File).name) }));
  const toast = () => document.querySelector(".toast")?.textContent;
  const sha = (i: number) => i.toString(16).padStart(64, "0");
  /** The engine's answer to each request in turn: new documents, unless `known` says the bytes were already there. */
  const engine = (answers: ((n: number) => Response | Promise<Response>)[] = []) => ({ onPost: (c: { raw?: BodyInit }) => {
    const n = (c.raw as FormData).getAll("files").length;
    return (answers.shift() ?? ((k: number) => json({ ok: true, files: Array.from({ length: k }, (_, i) => ({ sha: sha(i + 1), duplicate: false })) })))(n);
  } });
  const cheltuieli = async (answers?: Parameters<typeof engine>[0]) => {
    at("?co=NORD&tab=cheltuieli");
    const api = renderApp(engine(answers));
    await userEvent.click(await screen.findByRole("button", { name: "Încarcă bon sau factură" }));
    return api;
  };

  it("sends one file from Nou and opens its document", async () => {
    const id = fixture().documents.find((d) => /^[0-9a-f]{64}$/.test(d.id))!.id;
    const api = renderApp(engine([() => json({ ok: true, files: [{ sha: id, duplicate: false }] })]));
    await userEvent.click(await screen.findByRole("button", { name: "Nou" }));
    await pick([photo("bon.jpg")], screen.getByRole("dialog", { name: "Nou" }));
    await screen.findByRole("dialog", { busy: false, name: /Example Cloud/ });
    expect(sent(api)).toEqual([{ url: "/web/api/document-upload?co=NORD", files: ["bon.jpg"] }]);
    expect(new URLSearchParams(window.location.search).get("doc")).toBe(id);
    expect(screen.queryByRole("dialog", { name: "Nou" })).toBeNull();
  });

  it("sends several from Cheltuieli, skips what the engine wouldn't take, and opens Bonuri și chitanțe with the count", async () => {
    const api = await cheltuieli();
    await pick([photo("a.jpg"), photo("b.jpg"), photo("note.txt", new TextEncoder().encode("text"))]);
    await waitFor(() => expect(toast()).toBe("2 documente încărcate. Doar fotografii și PDF-uri de cel mult 15 MB."));
    expect(sent(api)).toEqual([{ url: "/web/api/document-upload?co=NORD", files: ["a.jpg", "b.jpg"] }]);
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli&side=bonuri");
  });

  it("under Toate firmele asks which firm, sends there and opens that firm's document", async () => {
    at("?co=ALL&tab=cheltuieli");
    const api = renderApp(engine());
    await userEvent.click(await screen.findByRole("button", { name: "Încarcă bon sau factură" }));
    const ask = within(screen.getByRole("dialog", { name: "Pentru ce firmă?" }));
    await userEvent.click(ask.getByRole("button", { name: coName(fixture("STUDIO9")) }));
    expect(screen.queryByRole("dialog", { name: "Pentru ce firmă?" })).toBeNull();
    await pick([photo("bon.jpg")]);
    await waitFor(() => expect(sent(api)).toEqual([{ url: "/web/api/document-upload?co=STUDIO9", files: ["bon.jpg"] }]));
    await waitFor(() => expect(window.location.search).toBe(`?co=STUDIO9&tab=cheltuieli&side=bonuri&doc=${sha(1)}`));
  });

  it("under Toate firmele sends nothing when the account changed after the firm was picked", async () => {
    at("?co=ALL&tab=cheltuieli");
    const api = server(engine());
    const signedIn = (id: string) => <IdentityContext.Provider value={{ identity: { ...fixtureIdentity(), me: { ...fixtureIdentity().me, id } },
      signOut: async () => {}, refreshIdentity: async () => {} }}><App /></IdentityContext.Provider>;
    const page = render(signedIn("u_example"));
    const firm = async () => {
      await userEvent.click(await screen.findByRole("button", { name: "Încarcă bon sau factură" }));
      await userEvent.click(within(screen.getByRole("dialog", { name: "Pentru ce firmă?" })).getByRole("button", { name: coName(fixture("STUDIO9")) }));
    };
    await firm();
    page.rerender(signedIn("u_other")); // the account in this tab changed
    await pick([photo("a.jpg")]);
    await firm();
    window.dispatchEvent(new StorageEvent("storage", { key: "fiscalm:session-changed" })); // another tab changed it, not read here yet
    await pick([photo("b.jpg")]);
    await firm(); // a fresh pick by the account now signed in still uploads, so the two above were refused, not slow
    await pick([photo("c.jpg")]);
    await waitFor(() => expect(sent(api)).toEqual([{ url: "/web/api/document-upload?co=STUDIO9", files: ["c.jpg"] }]));
  });

  it("never opens a document whose bytes were already known: it may belong to another company", async () => {
    await cheltuieli([() => json({ ok: true, files: [{ sha: sha(9), duplicate: true }] })]);
    await pick([photo("a.jpg")]);
    await waitFor(() => expect(toast()).toBe("Un document era deja încărcat."));
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli");
  });

  it("keeps what a first batch stored when a later one is refused, and says why", async () => {
    const stored = (n: number) => json({ ok: true, files: Array.from({ length: n }, (_, i) => ({ sha: sha(i + 1), duplicate: false })) });
    const api = await cheltuieli([stored, () => json({ ok: false }, 507)]);
    await pick(Array.from({ length: 11 }, (_, i) => photo(`p${i}.jpg`)));
    await waitFor(() => expect(toast()).toBe("10 documente încărcate. Spațiul de stocare este plin. Fișierele nu s-au trimis."));
    expect(sent(api).map((r) => r.files.length)).toEqual([10, 1]);
  });

  it("stops sending when the account changes in another tab", async () => {
    const first = deferred<Response>();
    const api = await cheltuieli([() => first.promise]);
    await pick(Array.from({ length: 11 }, (_, i) => photo(`p${i}.jpg`)));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    window.dispatchEvent(new StorageEvent("storage", { key: "fiscalm:session-changed" }));
    first.resolve(json({ ok: true, files: Array.from({ length: 10 }, (_, i) => ({ sha: sha(i + 1), duplicate: false })) }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Încarcă bon sau factură" })).toBeEnabled());
    expect(api.posts()).toHaveLength(1);
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli");
  });

  it("sends nothing more once its control is gone (the page left, or a sign-out here)", async () => {
    const first = deferred<Response>();
    const api = await cheltuieli([() => first.promise]);
    await pick(Array.from({ length: 11 }, (_, i) => photo(`p${i}.jpg`)));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await userEvent.click(screen.getAllByRole("link", { name: /^Acasă/ })[0]);
    first.resolve(json({ ok: true, files: Array.from({ length: 10 }, (_, i) => ({ sha: sha(i + 1), duplicate: false })) }));
    await new Promise((ok) => setTimeout(ok, 50));
    expect(api.posts()).toHaveLength(1);
    expect(new URLSearchParams(window.location.search).get("tab")).toBeNull();
  });

  it("sends one photo from a card payment's sheet as that payment's bon, and stays on the sheet", async () => {
    const tx = fixture().transactions.rows.find((r) => r.counterparty === "NIMBUS HOSTING INC")!.id;
    const used = "Tranzacția are deja o explicație.";
    at(`?co=NORD&tab=tranzactii&tx=${tx}`);
    const api = renderApp(engine([(n) => json({ ok: true, files: [{ sha: sha(n), duplicate: false }] }), () => json({ ok: false, message: used }, 409)]));
    const s = within(await screen.findByRole("dialog", { busy: false }));
    await userEvent.click(s.getByRole("radio", { name: "Bon / chitanță" }));
    const dialog = document.querySelector("[role=dialog]")!;
    expect(dialog.querySelector("input[type=file]")).not.toHaveAttribute("multiple");
    await pick([photo("bon.jpg")], dialog);
    await waitFor(() => expect(toast()).toBe("Bonul a fost asociat cu plata."));
    expect(sent(api)).toEqual([{ url: `/web/api/document-upload?co=NORD&tx=${tx}`, files: ["bon.jpg"] }]);
    expect(new URLSearchParams(window.location.search).get("tx")).toBe(tx);
    await pick([photo("bon.jpg")], dialog);
    await waitFor(() => expect(toast()).toBe(used));
  });

  it("says why when the engine refuses, and stays", async () => {
    await cheltuieli([() => json({ ok: false }, 507)]);
    await pick([photo("a.jpg")]);
    await waitFor(() => expect(toast()).toBe("Spațiul de stocare este plin. Fișierele nu s-au trimis."));
    expect(window.location.search).toBe("?co=NORD&tab=cheltuieli");
  });

  it.each(["readonly", "member"] as const)("is offered to %s only when the account can change things", async (role) => {
    at("?co=NORD&tab=cheltuieli");
    renderApp({ principal: fixtureIdentity(role) });
    await screen.findByRole("heading", { name: "Cheltuieli" });
    expect(document.querySelectorAll("input[type=file]").length > 0).toBe(role === "member");
    expect(!!screen.queryByRole("button", { name: "Încarcă bon sau factură" })).toBe(role === "member");
  });
});

describe("Cum ai plătit?", () => {
  // How the person paid goes with the marking; whatever the method, the marking awaits proof (the engine's rule).
  it.each([
    ["Din contul firmei", "", { method: "bank", detail: "" }, "până apare în extras."],
    ["Cu cardul firmei", "", { method: "card", detail: "0001" }, "până apare în extrasul cardului."],
    ["Numerar", "", { method: "cash", detail: "" }, "până apare bonul sau chitanța."],
    ["Din fonduri proprii", "", { method: "own", detail: "Cont de test" }, "e-mailul de plată."],
    ["Altă metodă", "Compensare cu F-12", { method: "other", detail: "Compensare cu F-12" }, "până apare dovada plății."],
  ])("marks one invoice paid %s", async (how, typed, body, proof) => {
    at("?co=NORD&tab=cheltuieli&inv=102");
    const api = renderApp();
    await userEvent.click(within(await screen.findByRole("dialog", { busy: false })).getByRole("button", { name: "Marchează ca plătită" }));
    const dialog = await screen.findByRole("dialog", { name: "Cum ai plătit?" });
    const press = within(dialog).getByRole("button", { name: "Marchează ca plătită" });
    expect(press).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("radio", { name: new RegExp(`^${how}`) }));
    expect(dialog).toHaveTextContent(proof);
    if (typed) {
      expect(press).toBeDisabled();
      await userEvent.type(within(dialog).getByRole("textbox", { name: "Cum s-a plătit" }), typed);
    }
    await userEvent.click(press);
    await waitFor(() => expect(api.posts()).toEqual([{ url: "/web/api/mark-paid", method: "POST", body: { co: "NORD", id: "102", ...body } }]));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); // the invoice sheet closes, as before
  });

  it("names the stored method on a marked invoice, still awaiting proof", async () => {
    const s = fixture(), r = s.invoices.find((x) => x.id === "108")!;
    Object.assign(r, { paid_method: "card", paid_detail: "0001" });
    at("?co=NORD&tab=cheltuieli&inv=108");
    renderApp({ state: () => json(s) });
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet.querySelector("header + .st")).toHaveTextContent("Marcată ca plătită, cu cardul");
    expect(sheet).toHaveTextContent("Rămâne în curs de confirmare până apare în extrasul cardului.");
    expect(sheet).toHaveTextContent("PlătităCu cardul firmei, card •• 0001"); // text matching folds the no-break space
  });

  const mount = (invoices: Invoice[], onDone = vi.fn()) => {
    const run = async <R,>(_co: string, send: () => Promise<R>) => { try { return await send(); } catch { return null; } };
    const ctx = { view: fixture(), route: parse("?co=NORD"), go: vi.fn(), act: vi.fn(), run, busy: false, say: vi.fn(), refresh: async () => null };
    const view = render(<AppCtx.Provider value={ctx as unknown as Ctx}><Company s={fixture()}><MarkPaid invoices={invoices} onDone={onDone} /></Company></AppCtx.Provider>);
    return { onDone, view };
  };
  const pick = (...ids: string[]) => ids.map((id) => [...fixture().invoices, ...fixture("STUDIO9").invoices].find((r) => r.id === id)!);

  it("marks several one after another, says what the engine refused and stops where an answer didn't arrive", async () => {
    const answers = [json({ ok: true, message: "", state: null }), json({ ok: false, message: "Factura nu mai este la plată.", state: null }), new Response("", { status: 502 })];
    const api = server({ onPost: () => answers.shift()! });
    const { onDone } = mount(pick("102", "105", "201", "101")); // each posted with its own firm
    await markPaidAs("Numerar");
    const out = await screen.findByRole("status");
    expect(api.posts().map((c) => [c.url, c.body])).toEqual([
      ["/web/api/mark-paid", { co: "NORD", id: "102", method: "cash", detail: "" }],
      ["/web/api/withdraw", { co: "NORD", id: "105", method: "cash", detail: "" }],
      ["/web/api/mark-paid", { co: "STUDIO9", id: "201", method: "cash", detail: "" }],
    ]);
    expect(out).toHaveTextContent(/^O factură a fost marcată ca plătită\. Nu se știe dacă a fost marcată .+\. Verifică factura\. O factură nu a fost trimisă\./);
    expect(within(out).getByRole("listitem")).toHaveTextContent(/: Factura nu mai este la plată\.$/);
    expect(onDone).not.toHaveBeenCalled();
    await userEvent.click(within(out).getByRole("button", { name: "Închide" }));
    expect(onDone).toHaveBeenCalledWith({ marked: 1, refused: [expect.stringMatching(/Factura nu mai este la plată\.$/)], unknown: expect.any(String), unsent: 1 });
  });

  // A session change keeps the sheet on screen: it says what was sent and closes; nothing unsent is sent later.
  it.each([
    ["the sheet goes away", false, (view: { unmount: () => void }) => view.unmount()],
    ["another tab changes the session", true, () => window.dispatchEvent(new StorageEvent("storage", { key: SESSION_CHANGED }))],
    ["the session ends", true, () => window.dispatchEvent(new Event(SESSION_ENDED))],
  ])("sends nothing more once %s", async (_, kept, change) => {
    const first = deferred<Response>();
    const api = server({ onPost: () => first.promise });
    const { onDone, view } = mount(pick("102", "201")); // each invoice with its own firm
    await markPaidAs("Din contul firmei");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    change(view);
    await act(async () => { first.resolve(json({ ok: true, message: "", state: null })); });
    expect(api.posts()).toHaveLength(1);
    expect(onDone).not.toHaveBeenCalled();
    if (!kept) return;
    const out = screen.getByRole("status");
    expect(out).toHaveTextContent("O factură a fost marcată ca plătită. O factură nu a fost trimisă.");
    await userEvent.click(within(out).getByRole("button", { name: "Închide" }));
    expect(onDone).toHaveBeenCalledWith({ marked: 1, refused: [], unknown: "", unsent: 1 });
    expect(api.posts()).toHaveLength(1);
  });
});

it("shows the engine's combined sources in the expense row and sheet", async () => {
  const s = fixture();
  s.expenses.find((e) => e.invoice === "112")!.sources = ["efactura", "upload_link"];
  at("?co=NORD&tab=cheltuieli");
  renderApp({ state: () => json(summaryResult(s)), browse: (url) => json(browseResult(s, "cheltuieli", Object.fromEntries(new URL(url, "http://local").searchParams))),
    detail: (url) => json(detailResult(s, Object.fromEntries(new URL(url, "http://local").searchParams))) });
  const label = await screen.findByText(/e-Factura, bon încărcat/);
  await userEvent.click(label.closest("a")!);
  const dialog = await screen.findByRole("dialog", { busy: false });
  expect(within(dialog).getByText("Sursă").nextElementSibling).toHaveTextContent("e-Factura, bon încărcat");
});
