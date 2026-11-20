// The site's state signs in lists: "=" matched, "≈" almost, "≠" doesn't match, drawn in the icon's place and hidden from
// screen readers; the state stays in words. States that are none of these (due, late, a question) keep their icon.
// Every expectation follows from the state the test gives the engine's fake, never from what the page happened to draw.
import { screen, within } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import { cap, luna } from "@/lib/format";
import type { AllState, Bank, Invoice, Issued, FilePayment, State } from "@/lib/types";
import { at, fixture, fixtureAll, json, paymentFile, paymentFileDetail, server, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

const row = (el: HTMLElement) => el.closest(".row") as HTMLElement;
/** The row's sign, or "" when it shows its icon. */
const sign = (r: HTMLElement) => r.querySelector(".ic.g")?.textContent ?? "";
const day = (today: string, n: number) => new Date(Date.parse(today) + n * 864e5).toISOString().slice(0, 10);

/** NORD's fixture with its lists emptied, so only the rows a test adds are drawn. */
function bare(): State {
  const s = fixture();
  Object.assign(s, { invoices: [], issued: [], expenses: [], refused: [] });
  s.files = [];
  return s;
}

function invoice(s: State, supplier: string, x: Partial<Invoice>): Invoice {
  const base = fixture().invoices[0];
  return { ...base, id: supplier, supplier, partner: null, status: "open", payment_state: null, item_id: null, paid_at: null,
    overdue: false, proved: false, bon: null, issue_date: day(s.today, -10), due_date: day(s.today, 20), currency: "RON", ...x };
}

describe("state signs", () => {
  it("a received invoice: proved paid =, paid without proof or awaiting its proof ≈, due or a question none", async () => {
    const s = bare();
    const t = s.today;
    // [supplier, the invoice's state, its sign]
    const cases: [string, Partial<Invoice>, string][] = [
      ["Dovedita Extras SRL", { status: "paid", proved: true, paid_at: `${t}T09:00:00` }, "="],
      ["Platita Manual SRL", { status: "paid", proved: false, paid_at: `${t}T09:00:00` }, "≈"],
      ["Marcata Buton SRL", { status: "marked", paid_at: `${t}T09:00:00` }, "≈"],
      ["In Fisier SRL", { status: "linked", payment_state: "in_file", item_id: "f1" }, "≈"],
      ["Importata BT SRL", { status: "linked", payment_state: "imported", item_id: "f2" }, "≈"],
      ["Retinuta Plata SRL", { status: "linked", payment_state: "held", item_id: "f3" }, ""],
      ["Fisier Pierdut SRL", { status: "linked", payment_state: "lost", item_id: "f4" }, ""],
      ["In Fisier Restanta SRL", { status: "linked", payment_state: "in_file", item_id: "f5", overdue: true, due_date: day(t, -3) }, ""],
      ["Restanta Deschisa SRL", { status: "open", overdue: true, due_date: day(t, -3) }, ""],
      ["Scadenta Curand SRL", { status: "open", due_date: day(t, 2) }, ""],
    ];
    s.invoices = cases.map(([name, x]) => invoice(s, name, x));
    at("?co=NORD&tab=cheltuieli");
    renderApp({ state: () => json(s) });
    await screen.findByText("Dovedita Extras SRL");
    for (const [name, , want] of cases) {
      const r = row(screen.getByText(name));
      expect(sign(r), name).toBe(want);
      if (want) {
        expect(r.querySelector(".ic.g"), name).toHaveAttribute("aria-hidden", "true");
        expect(r.querySelector(".ic.g"), name).toHaveClass(want === "=" ? "eq" : "approx");
      } else expect(r.querySelector(".ic svg"), name).not.toBeNull();
    }
    // A row draws its focus ring inside itself, so a card that clips its rows never hides it.
    expect(row(screen.getByText("Dovedita Extras SRL"))).toHaveClass("focus-visible:outline-offset-[-2px]");
    // "Plătită" only with proof: paid on a person's word reads as a marked one.
    expect(row(screen.getByText("Dovedita Extras SRL"))).toHaveTextContent("Plătită");
    expect(row(screen.getByText("Platita Manual SRL"))).toHaveTextContent("În curs de confirmare");
    expect(row(screen.getByText("Marcata Buton SRL"))).toHaveTextContent("În curs de confirmare");
    for (const name of ["In Fisier Restanta SRL", "Restanta Deschisa SRL"]) {
      expect(row(screen.getByText(name)).querySelector(".ic"), name).toHaveClass("late");
    }
  });

  it("an issued invoice: collected =, awaiting confirmation ≈, refused by SPV ≠, overdue or open none", async () => {
    const s = bare();
    const base = fixture().issued[0];
    const cases: [string, Partial<Issued>, string][] = [
      ["Client Incasat SRL", { status: "collected", owed: "0.00" }, "="],
      ["Client Marcat SRL", { status: "marked", owed: "0.00" }, "≈"],
      ["Client Respins SRL", { status: "open", rejected: true, spv: { state: "rejected", errors: ["eroare"], at: null } }, "≠"],
      ["Client Restant SRL", { status: "open", days_overdue: 5 }, ""],
      ["Client Deschis SRL", { status: "open" }, ""],
    ];
    s.issued = cases.map(([client, x], i) => ({ ...base, id: `i${i}`, client, partner: null, days_overdue: 0, rejected: false,
      spv: { state: "accepted", errors: [], at: null }, ...x }));
    at("?co=NORD&tab=vanzari");
    renderApp({ state: () => json(s) });
    await screen.findByText("Client Incasat SRL");
    for (const [name, , want] of cases) expect(sign(row(screen.getByText(name))), name).toBe(want);
    expect(row(screen.getByText("Client Respins SRL")).querySelector(".ic.g")).toHaveClass("ne");
    expect(row(screen.getByText("Client Respins SRL"))).toHaveTextContent("Respinsă de ANAF");
    expect(row(screen.getByText("Client Restant SRL")).querySelector(".ic")).toHaveClass("late");
  });

  it("Fișiere de plăți: a file's payments ≈, = only when a statement proves it, an overdue one in red", async () => {
    const s = bare();
    const t = s.today;
    const listed = (payee: string, state: FilePayment["state"], proved = false, due: string | null = null): FilePayment =>
      ({ ...paymentFileDetail().payments[0], id: payee, payee, due, paid_at: state === "paid" ? `${t}T09:00:00` : null, state, proved });
    const detail = paymentFileDetail({ payments: [listed("Plata In Fisier", "in_file"), listed("Plata Restanta", "in_file", false, day(t, -2)),
      listed("Plata Importata", "imported"), listed("Plata Dovedita", "paid", true), listed("Plata Notata", "paid")] });
    s.files = [paymentFile()];
    at("?co=NORD&tab=plati&pf=7");
    renderApp({ state: () => json(s), paymentFile: () => json(detail) });
    await screen.findByText("Plata Notata");
    const cases: [string, string][] = [["Plata In Fisier", "≈"], ["Plata Restanta", ""], ["Plata Importata", "≈"], ["Plata Dovedita", "="], ["Plata Notata", "≈"]];
    for (const [name, want] of cases) expect(sign(row(screen.getByText(name))), name).toBe(want);
    expect(row(screen.getByText("Plata Restanta")).querySelector(".ic")).toHaveClass("late");
    // A payment marked paid by hand: no "=", and not "Plătită" until a statement proves it.
    const unproved = row(screen.getByText("Plata Notata"));
    expect(unproved).toHaveTextContent("În curs de confirmare");
    expect(unproved).not.toHaveTextContent("Plătită");
    // Green only when proved.
    expect(row(screen.getByText("Plata Dovedita")).querySelector("small")).toHaveClass("ok");
    expect(unproved.querySelector("small")).not.toHaveClass("ok");
  });

  it("Tranzacții, Extrase lunare: a statement whose totals agree =, totals that differ ≠, unread, unchecked or missing none", async () => {
    const s = bare();
    const bank = (x: Partial<Bank>): Bank => ({ statement: true, error: null, balanced: null, card: [], efactura_suppliers: 0,
      advances: [], ...x });
    const report = s.reports[0];
    const cases: [string, Bank, string][] = [
      ["2026-05", bank({ balanced: true }), "="],
      ["2026-06", bank({ balanced: false }), "≠"],
      ["2026-07", bank({ balanced: null }), ""],
      ["2026-08", bank({ error: "format" }), ""],
      ["2026-09", bank({ statement: false }), ""],
    ];
    s.reports = cases.map(([month, b]) => ({ ...report, month, bank: b }));
    at("?co=NORD&tab=tranzactii");
    renderApp({ state: () => json(s) });
    const card = (await screen.findByRole("heading", { name: "Extrase lunare" })).parentElement!.nextElementSibling as HTMLElement;
    const month = (m: string) => row(within(card).getByText(cap(luna(m))));
    for (const [m, , want] of cases) expect(sign(month(m)), m).toBe(want);
    expect(month("2026-06").querySelector(".ic.g")).toHaveClass("ne");
  });
});

describe("a received invoice paid without proof: În curs de confirmare, never Plătită or green", () => {
  /** Adds to NORD's state a copy of its statement-proved invoice 111 that nothing proves (`proved: false`), with the same
   * partner and in the same month's report. */
  function withUnproved(s: State): Invoice {
    const proved = s.invoices.find((r) => r.id === "111")!;
    expect(proved).toMatchObject({ status: "paid", proved: true });
    s.invoices.push({ ...proved, id: "199", supplier: "Platita Manual SRL", number: "PM-199", proved: false });
    s.reports = s.reports.map((r) => (r.received.includes("111") ? { ...r, received: [...r.received, "199"] } : r));
    return proved;
  }
  /** The row's sign, whether its state words are green, and the words. */
  const look = (r: HTMLElement) => ({ sign: sign(r), green: r.querySelector("small")!.classList.contains("ok"),
    paid: /Plătită/.test(r.querySelector("small")!.textContent ?? "") });
  const proved = { sign: "=", green: true, paid: true };
  const unproved = { sign: "≈", green: false, paid: false };

  it("in Cheltuieli and in the ALL view", async () => {
    const s = fixture();
    withUnproved(s);
    at("?co=NORD&tab=cheltuieli");
    server({ state: () => json(s) });
    const { unmount } = render(<App />);
    expect(look(row(await screen.findByText("Platita Manual SRL")))).toEqual(unproved);
    expect(look(row(screen.getByText("Chirie Spatiu SRL")))).toEqual(proved);
    unmount();

    const all: AllState = fixtureAll();
    withUnproved(all.parts.find((p) => p.co === "NORD")!);
    at("?co=ALL&tab=cheltuieli");
    renderApp({ all: () => json(all) });
    expect(look(row(await screen.findByText("Platita Manual SRL")))).toEqual(unproved);
    expect(look(row(screen.getByText("Chirie Spatiu SRL")))).toEqual(proved);
  });

  it("in Rapoarte", async () => {
    const s = fixture();
    withUnproved(s);
    const month = s.reports.find((r) => r.received.includes("199"))!.month;
    at(`?co=NORD&tab=rapoarte&month=${month}`);
    renderApp({ state: () => json(s) });
    const card = (await screen.findByRole("heading", { name: "Facturi primite" })).parentElement!.nextElementSibling as HTMLElement;
    expect(look(row(await within(card).findByText("Platita Manual SRL")))).toEqual(unproved);
    expect(look(row(within(card).getByText("Chirie Spatiu SRL")))).toEqual(proved);
  });

  it("in the invoice's own sheet: the state line is green only when proved", async () => {
    const s = fixture();
    withUnproved(s);
    const stateOf = async (id: string) => {
      at(`?co=NORD&tab=cheltuieli&inv=${id}`);
      server({ state: () => json(s) });
      const { unmount } = render(<App />);
      const line = (await screen.findByRole("dialog", { busy: false })).querySelector("header + .st") as HTMLElement; // right under the amount
      const out = { text: line.textContent ?? "", green: line.classList.contains("ok") };
      unmount();
      return out;
    };
    expect(await stateOf("199")).toEqual({ text: expect.stringMatching(/^În curs de confirmare/), green: false });
    expect(await stateOf("111")).toEqual({ text: expect.stringMatching(/^Plătită/), green: true });
  });

  it("in the partner's sheet", async () => {
    const s = fixture();
    const { partner, supplier } = withUnproved(s);
    at(`?co=NORD&tab=parteneri&p=${partner}`);
    renderApp({ state: () => json(s) });
    const d = await screen.findByRole("dialog", { name: supplier });
    expect(look(row(within(d).getByText("Nr. PM-199")))).toEqual(unproved);
    expect(look(row(within(d).getByText("Nr. CS-0920")))).toEqual(proved);
  });
});
