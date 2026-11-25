// A transaction's sheet (`tx`) against a fake engine: Plătitor, Ce acoperă, the named difference, the exact tx-link and
// tx-unlink bodies, the user's explanation with what is left of each invoice, and the partner's sheet listing it.
import { act, cleanup, screen, within, waitFor } from "@testing-library/react";
import { renderSignedIn as render, answer, choose, choicesOf } from "./helpers";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import type { State, TxLink } from "@/lib/types";
import { fixtureDetailData, fixturePreparedPayments, fixtureTransactionChoicesData } from "@/lib/fixtures";
import { at, choicesResult, deferred, detailResult, fixture, fixtureAll, json, server, renderApp, summaryResult } from "./helpers";

const POPESCU = fixture().transactions.rows.find((r) => r.counterparty === "POPESCU ANA")!.id; // +500,00 lei, De explicat
const NIMBUS = fixture().transactions.rows.find((r) => r.counterparty === "NIMBUS HOSTING INC")!.id; // −173,45 lei, De explicat

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

async function sheet() {
  return within(await screen.findByRole("dialog", { busy: false }));
}

/** NORD with the +500 credit explained by the user: 500 of NRD 0042 (4.800), the rest still owed. */
function linked(): State {
  const s = fixture();
  const t = s.transactions.rows.find((x) => x.id === POPESCU)!;
  const link: TxLink = { id: "a1b2c3d4e5f60718", tx: POPESCU, kind: "documents", other: null, partner: "NORD.5001", parts: [{ id: "5001", amount: "500.00", closed: false }],
    payments: [], reason: "rest", difference: "-4300.00", currency: "RON", note: "", rule: null, by: "admin", at: "2026-10-26T09:30:00" };
  t.explanation = { kind: "collection", issued: ["5001"], settlement: null, link };
  t.partner = "NORD.5001";
  const r = s.issued.find((x) => x.id === "5001")!;
  Object.assign(r, { covered: "500.00", owed: "4300.00", left: "4300.00" });
  s.transactions.to_explain -= 1;
  return s;
}

function manyInvoices(explained = false): State {
  const s = explained ? linked() : fixture(), t = s.transactions.rows.find((r) => r.id === POPESCU)!;
  const invoice = s.issued.find((r) => r.id === "5001")!;
  s.issued = Array.from({ length: 151 }, (_, i) => ({ ...invoice, id: String(10000 + i), number: `SYN ${String(i).padStart(3, "0")}`,
    status: explained ? "collected" : "open", issue_date: "2026-10-01", left: explained ? "0.00" : "0.01", total: "0.01" }));
  t.partner = invoice.partner; t.amount = explained ? "1.51" : "0.02";
  if (explained && t.explanation && "link" in t.explanation && t.explanation.link) {
    t.explanation.link.parts = s.issued.map((r) => ({ id: r.id, amount: "0.01", closed: true }));
    if (t.explanation.kind === "collection") t.explanation.issued = s.issued.map((r) => r.id);
    t.explanation.link.difference = "0.00"; t.explanation.link.reason = null;
  }
  return s;
}

const query = (url: string) => Object.fromEntries(new URL(url, "http://local").searchParams);

it.each([false, true])("keeps transaction detail and choice fixtures equal to complete engine selectors, linked=%s", (explained) => {
  const full = manyInvoices(explained), q = { kind: "transaction", id: POPESCU };
  const actual = fixtureDetailData(full, "transaction", POPESCU), expected = detailResult(full, q);
  expect(actual.transaction).toEqual(expected.transaction); expect(actual.related).toEqual(expected.related);
  for (const collection of ["partners", "invoices", "payments", "selected"] as const) {
    const cursor = expected.transaction.choices[collection].total > 50 ? "50" : "";
    expect(fixtureTransactionChoicesData(full, { id: POPESCU, collection, cursor })).toEqual(choicesResult(full, { id: POPESCU, collection, cursor }));
  }
});

it("keeps invoice selections across API pages and clears choices locally when the partner is cleared", async () => {
  const full = manyInvoices(), partner = full.issued[0].partner!;
  at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
  const api = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url))),
    transactionChoices: (url) => json(choicesResult(full, query(url))), onPost: () => json({ ok: false, message: "Refuzat" }) });
  const s = await sheet(), u = userEvent.setup();
  const next = () => within(s.getByRole("navigation", { name: "Facturi" })).getByRole("button", { name: "Pagina următoare" });
  expect(s.getAllByRole("checkbox", { name: /^Factura SYN/ })).toHaveLength(50);
  for (let round = 0; round < 2; round++) {
    await u.click(s.getByRole("checkbox", { name: /Factura SYN 000/ }));
    for (let i = 0; i < 3; i++) { await u.click(next()); await waitFor(() => expect(s.queryByText("Se încarcă…")).toBeNull()); }
    expect(next()).toHaveFocus();
    expect(s.getAllByRole("checkbox", { name: /^Factura SYN/ })).toHaveLength(1);
    await u.click(s.getByRole("checkbox", { name: /Factura SYN 150/ }));
    expect(s.getByText("Facturile alese").nextSibling).toHaveTextContent("0,02 lei");
    if (!round) {
      const reads = api.calls.filter((c) => c.url.includes("transaction-choices")).length;
      await choose(s.getByRole("combobox", { name: "Plătitor" }), "");
      expect(s.queryAllByRole("checkbox", { name: /^Factura SYN/ })).toHaveLength(0);
      expect(api.calls.filter((c) => c.url.includes("transaction-choices"))).toHaveLength(reads);
      await choose(s.getByRole("combobox", { name: "Plătitor" }), partner);
    }
  }
  await u.click(s.getByRole("button", { name: "Asociază tranzacția" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", tx: POPESCU, invoices: "10000,10150", payment: "", reason: "", note: "", was: "", remember: "" });
});

it("restores every existing selected invoice before saving, retrying a failed continuation", async () => {
  const full = manyInvoices(true), late = deferred<Response>(); let fail = true;
  at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
  const api = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url))),
    transactionChoices: (url) => fail ? late.promise.then((r) => r.clone()) : json(choicesResult(full, query(url))), onPost: () => json({ ok: false, message: "Refuzat" }) });
  const s = await sheet();
  for (let i = 0; i < 3; i++) {
    await userEvent.click(s.getByRole("button", { name: "Pagina următoare" }));
    await waitFor(() => expect(s.queryByText("Se încarcă…")).toBeNull());
  }
  expect(s.getByRole("link", { name: /Factura SYN 150/ })).toHaveAttribute("href", `?co=NORD&tab=tranzactii&iss=10150`);
  await userEvent.click(s.getByRole("button", { name: "Schimbă explicația" }));
  expect(s.getByRole("button", { name: "Asociază tranzacția" })).toBeDisabled();
  expect(s.queryByText("Facturile alese")).toBeNull();
  await act(async () => late.resolve(json({}, 500)));
  await userEvent.click(await screen.findByRole("button", { name: "Reîncearcă" }));
  fail = false;
  await userEvent.click(await screen.findByRole("button", { name: "Reîncearcă" }));
  await waitFor(() => expect(s.getByRole("button", { name: "Asociază tranzacția" })).toBeEnabled());
  expect(s.getByText("Facturile alese").nextSibling).toHaveTextContent("1,51 lei");
  await userEvent.click(s.getByRole("button", { name: "Asociază tranzacția" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", tx: POPESCU, invoices: full.issued.map((r) => r.id).join(","), payment: "",
    reason: "", note: "", was: "a1b2c3d4e5f60718", remember: "" });
});

it("searches every partner and pages prepared payments while posting the original private payment key", async () => {
  const full = fixture(), t = full.transactions.rows.find((r) => r.id === NIMBUS)!, p = full.partners[0], payment = fixturePreparedPayments(full.co)[0];
  t.partner = null;
  full.partners = Array.from({ length: 151 }, (_, i) => ({ ...p, id: `NORD.pick-${i}`, name: `Furnizor ${i}`, roles: ["supplier"] }));
  const prepared = Array.from({ length: 151 }, (_, i) => ({ ...payment, item_id: `private-payment-${i}`, payee: `Beneficiar ${i}`, amount: t.amount }));
  at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
  const api = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url), prepared)),
    transactionChoices: (url) => json(choicesResult(full, query(url), prepared)), onPost: () => json({ ok: false, message: "Refuzat" }) });
  const s = await sheet(), u = userEvent.setup();
  await u.click(s.getByRole("radio", { name: "Factură" }));
  await u.type(s.getByRole("textbox", { name: "Caută partenerul" }), "Furnizor 150");
  await waitFor(async () => expect((await choicesOf(s.getByRole("combobox", { name: "Beneficiar" }))).map((o) => o.text)).toContain("Furnizor 150"));
  await choose(s.getByRole("combobox", { name: "Beneficiar" }), "NORD.pick-150");
  for (let i = 0; i < 3; i++) {
    await u.click(within(s.getByRole("navigation", { name: "Plăți pregătite" })).getByRole("button", { name: "Pagina următoare" }));
    await waitFor(() => expect(s.queryByText("Se încarcă…")).toBeNull());
  }
  await u.click(s.getByRole("checkbox", { name: /^Beneficiar 150/ }));
  await u.click(s.getByRole("button", { name: "Asociază tranzacția" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", tx: NIMBUS, invoices: "", payment: "private-payment-150", reason: "", note: "", was: "", remember: "" });
});

it("proposes the one invoice the details name at its exact amount, confirmed in one tap", async () => {
  const full = fixture(), t = full.transactions.rows.find((r) => r.id === POPESCU)!, invoice = full.issued.find((r) => r.id === "5001")!;
  const other = full.partners.find((p) => p.id !== invoice.partner && p.roles.includes("client"))!;
  // the bank text points to another client: the form follows the proposed invoice's partner, so it shows what it ticks
  Object.assign(t, { details: `Plata factura ${invoice.number.replace(" ", "-")}`, amount: invoice.left, partner: other.id });
  const detail = detailResult(full, { kind: "transaction", id: POPESCU });
  expect(detail.transaction.choices.proposed.data.map((r: { id: string }) => r.id)).toEqual(["5001"]);
  expect(fixtureDetailData(full, "transaction", POPESCU).transaction).toEqual(detail.transaction);
  at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
  let after: State | null = null;
  const api = renderApp({ state: () => json(summaryResult(after ?? full)), detail: (url) => json(after ? detailResult(after, query(url)) : detail),
    transactionChoices: (url) => json(choicesResult(full, query(url))),
    onPost: () => { after = linked(); return json({ ok: true, message: "Tranzacția a fost asociată cu factura NRD 0042.", next: null, done: null,
      linked: { id: "a1b2c3d4e5f60718" }, state: summaryResult(after) }); } });
  const s = await sheet();
  expect(s.getByRole("checkbox", { name: new RegExp(`Factura ${invoice.number}`) })).toBeChecked(); // the form starts from it too
  expect(s.getByRole("combobox", { name: "Plătitor" })).toHaveValue(invoice.partner);
  await userEvent.click(s.getByRole("button", { name: `Confirmă: factura ${invoice.number}` }));
  expect(api.posts().map((c) => [c.url, c.body])).toEqual([["/web/api/tx-link", { co: "NORD", tx: POPESCU, invoices: "5001" }]]);
  expect(await screen.findByText("Tranzacția a fost asociată cu factura NRD 0042.")).toBeInTheDocument();
  // the sheet stays on the transaction and shows its explanation: no success sheet, no move to Acasă
  expect(await (await sheet()).findByRole("button", { name: "Schimbă explicația" })).toBeInTheDocument();
  expect(new URLSearchParams(location.search).get("tx")).toBe(POPESCU);
});

it("opens another firm's transaction from a metadata-only ALL view and posts for that firm", async () => {
  const full = fixtureAll(), s = full.parts.find((p) => p.co === "STUDIO9")!, t = s.transactions.rows.find((t) => t.can_link && !t.explanation && (!t.card || t.card_owner))!;
  at(`?co=ALL&tx=${t.id}`);
  const api = renderApp({ all: () => json({ ...full, bounded: true, parts: full.parts.map((s) => summaryResult(s)) }),
    detail: (url) => json(detailResult(full, query(url))), transactionChoices: (url) => json(choicesResult(full, query(url))), onPost: () => json({ ok: false, message: "Refuzat" }) });
  const dialog = await sheet();
  await userEvent.click(dialog.getByRole("radio", { name: "Fără document" }));
  await userEvent.click(dialog.getByRole("radio", { name: "Comision bancar" }));
  await userEvent.click(dialog.getByRole("button", { name: "Marchează fără document" }));
  expect(api.posts()[0].body).toEqual({ co: "STUDIO9", tx: t.id, kind: "label", reason: "bank_fee", note: "", who: "", was: "", remember: "iban" }); // saved by default
});

describe("a transaction's sheet", () => {
  it.each([fixture, linked])("restores keyboard focus after closing an explanation (%#)", async (state) => {
    at("?co=NORD&tab=tranzactii&f=all");
    renderApp({ state: () => json(state()) });
    const opener = await screen.findByRole("link", { name: /POPESCU ANA/ });
    opener.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("dialog", { busy: false })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(opener).toHaveFocus();
  });

  it.each([false, true])("explains the partner’s own invoice even when it is also a courier: %s", async (courier) => {
    at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
    const initial = fixture();
    if (courier) initial.partners.find((p) => p.id === "NORD.5001")!.roles.push("courier");
    const fake = renderApp({ state: () => json(initial), onPost: () => json({ ok: true, message: "Tranzacția a fost asociată cu factura NRD 0042. Restul facturii NRD 0042 rămâne de încasat.",
      next: null, done: null, linked: { id: "a1b2c3d4e5f60718" }, state: linked() }) });
    const s = await sheet();
    expect(s.getByText("POPESCU ANA")).toBeInTheDocument();
    expect(s.getByRole("group", { name: "Plătitor" })).toBeInTheDocument();
    const button = s.getByRole("button", { name: "Asociază tranzacția" });
    const who = s.getByRole("combobox", { name: "Plătitor" });
    expect((await choicesOf(who)).map((o) => o.text)).toContain("Client Alfa SRL");
    expect(within(who).queryByRole("option", { name: "Energie Furnizare SA" })).toBeNull(); // money in: clients only
    await choose(who, "NORD.5001");
    const covers = within(s.getByRole("group", { name: "Ce acoperă" }));
    await userEvent.click(covers.getByRole("checkbox", { name: /Factura NRD 0042/ }));
    expect(s.getByText("Tranzacția").nextSibling).toHaveTextContent("500,00 lei");
    expect(s.getByText("Factura aleasă").nextSibling).toHaveTextContent("4.800,00 lei");
    expect(s.getByText("Diferență").nextSibling).toHaveTextContent("4.300,00 lei mai puțin");
    const why = within(s.getByRole("group", { name: "Motiv diferență" }));
    expect(why.getAllByRole("radio").map((r) => r.parentElement!.textContent)).toEqual(
      ["Comision curier sau procesator", "Comision bancar", "Rest de încasat", "Diferență de curs", "Altceva"]);
    await userEvent.click(why.getByRole("radio", { name: "Altceva" }));
    await userEvent.type(s.getByRole("textbox", { name: "Descrierea diferenței" }), "avans");
    await userEvent.click(why.getByRole("radio", { name: "Rest de încasat" }));
    await userEvent.click(button);
    expect(fake.posts().map((c) => [c.url, c.body])).toEqual([["/web/api/tx-link", {
      co: "NORD", tx: POPESCU, invoices: "5001", payment: "", reason: "rest", note: "", was: "", remember: "" }]]);
    expect(await screen.findByText("Tranzacția a fost asociată cu factura NRD 0042. Restul facturii NRD 0042 rămâne de încasat."))
      .toBeInTheDocument();
    // the sheet stays open and shows the explanation the engine sent back
    const after = await sheet();
    expect(after.getByRole("heading", { name: "Ce acoperă" })).toBeInTheDocument();
  });

  it("shows the user's explanation with what is left, and undoes it with tx-unlink", async () => {
    at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
    const fake = server({ state: () => json(linked()), onPost: () => json({ ok: true, message: "Asocierea a fost anulată.", next: null,
      done: null, state: fixture() }) });
    render(<App />);
    const s = await sheet();
    const part = s.getByRole("link", { name: /Factura NRD 0042/ });
    expect(part).toHaveTextContent("Rămâne 4.300,00 lei de încasat");
    expect(part).toHaveAttribute("href", "?co=NORD&tab=tranzactii&iss=5001");
    expect(s.getByText("Plătitor").nextSibling).toHaveTextContent("Client Alfa SRL");
    expect(s.getByText("Diferența").nextSibling).toHaveTextContent("4.300,00 lei, Rest de încasat");
    expect(s.getByText("Explicată").nextSibling).toHaveTextContent("26 oct, admin");
    await userEvent.click(s.getByRole("button", { name: "Schimbă explicația" }));
    expect(s.getByRole("combobox", { name: "Plătitor" })).toHaveValue("NORD.5001");
    expect(s.getByRole("radio", { name: "Factură emisă" })).toHaveFocus(); // the button that had it is gone
    expect(s.getByRole("checkbox", { name: /Factura NRD 0042/ })).toBeChecked();
    expect(s.getByRole("checkbox", { name: /Factura NRD 0042/ }).parentElement).toHaveTextContent("4.800,00 lei");
    await userEvent.click(s.getByRole("button", { name: "Renunță" }));
    expect(s.getByRole("heading", { name: "Ce acoperă" })).toHaveFocus();
    await userEvent.click(s.getByRole("button", { name: "Anulează asocierea" }));
    expect(await answer()).toBe("Anulezi asocierea? Tranzacția și ce acoperea revin la starea de dinainte.");
    await waitFor(() => expect(fake.posts()).toHaveLength(1));
    expect(fake.posts().map((c) => [c.url, c.body])).toEqual([["/web/api/tx-unlink", { tx: POPESCU, was: "a1b2c3d4e5f60718", co: "NORD" }]]);
    expect(await screen.findByText("Asocierea a fost anulată.")).toBeInTheDocument();
  });

  it("starts from the partner the bank's text points to, and a debit can pay a prepared payment", async () => {
    const s0 = fixture();
    s0.transactions.rows.find((x) => x.id === NIMBUS)!.partner = "NORD.1003";
    const prepared = fixturePreparedPayments(s0.co);
    const pay = prepared[1];
    pay.amount = "173.45";
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    const fake = renderApp({ state: () => json(s0), detail: (url) => json(detailResult(s0, query(url), prepared)), transactionChoices: (url) => json(choicesResult(s0, query(url), prepared)) });
    const s = await sheet();
    expect(s.getByRole("combobox", { name: "Beneficiar" })).toHaveValue("NORD.1003");
    // the supplier's invoice is in euro, the card paid 37,00 USD: nothing of it fits
    expect(s.getByText("Partenerul nu are facturi deschise în lei.")).toBeInTheDocument();
    // only a prepared payment of the debit's own amount in lei is offered, not the rest of the file
    expect(s.getAllByRole("checkbox", { name: /lei$/ })).toHaveLength(1);
    await userEvent.click(s.getByRole("checkbox", { name: `${pay.payee}, ${pay.details || pay.ref} 173,45 lei` }));
    expect(s.getByText("Plata pregătită").nextSibling).toHaveTextContent("173,45 lei");
    expect(s.queryByRole("group", { name: "Motiv diferență" })).toBeNull();
    // one paid with a fee is found by a search over the whole file, and the difference is named
    const other = prepared[0];
    await userEvent.type(s.getByRole("textbox", { name: "Caută plățile pregătite" }), other.payee);
    await userEvent.click(await s.findByRole("checkbox", { name: new RegExp(`^${other.payee}`) }));
    expect(within(s.getByRole("group", { name: "Motiv diferență" })).queryByRole("radio", { name: "Rest de plătit" })).toBeNull();
    await userEvent.click(s.getByRole("radio", { name: "Altceva" }));
    await userEvent.type(s.getByRole("textbox", { name: "Descrierea diferenței" }), "comision");
    await userEvent.click(s.getByRole("button", { name: "Asociază tranzacția" }));
    expect(fake.posts()[0].body).toEqual({ co: "NORD", tx: NIMBUS, invoices: "", payment: other.item_id, reason: "other",
      note: "comision", was: "", remember: "" });
  });

  it("offers no form when the engine says the row can't be linked (shadow mode)", async () => {
    const s0 = fixture();
    s0.live = false;
    for (const t of s0.transactions.rows) t.can_link = false;
    at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
    renderApp({ state: () => json(s0) });
    const s = await sheet();
    expect(s.getByText("POPESCU ANA")).toBeInTheDocument();
    expect(s.queryByRole("button", { name: "Asociază tranzacția" })).toBeNull();

  });

  it("lists the partner's linked transactions on its sheet, and the part still owed on the invoice", async () => {
    at("?co=NORD&tab=parteneri&p=NORD.5001");
    renderApp({ state: () => json(linked()) });
    const s = await sheet();
    const list = within(s.getByRole("region", { name: "Tranzacții asociate" }));
    expect(list.getByRole("link", { name: /POPESCU ANA/ })).toHaveAttribute("href", `?co=NORD&tab=parteneri&tx=${POPESCU}`);
    expect(list.getByRole("link", { name: /POPESCU ANA/ })).toHaveTextContent("Încasarea parțială a facturii NRD 0042");
    await userEvent.click(within(s.getByRole("region", { name: "Facturi emise" })).getByRole("link", { name: /NRD 0042/ }));
    const inv = await sheet();
    expect(inv.getByText("Încasat").nextSibling).toHaveTextContent("500,00 lei");
    expect(inv.getByText("Rest de încasat").nextSibling).toHaveTextContent("4.300,00 lei");
  });

  it("shows what was paid and what is left on a received invoice paid in part, and lists what is left", async () => {
    const s0 = fixture();
    Object.assign(s0.invoices.find((r) => r.id === "101")!, { covered: "200.00", left: "186.40" });
    at("?co=NORD&tab=cheltuieli&inv=101");
    renderApp({ state: () => json(s0) });
    const s = await sheet();
    expect(s.getByText("Plătit").nextSibling).toHaveTextContent("200,00 lei");
    expect(s.getByText("Rest de plată").nextSibling).toHaveTextContent("186,40 lei");
    await waitFor(() => expect(document.querySelector('a.row[href*="inv=101"] .v')?.firstChild?.textContent).toBe("186,40 lei"));
  });

  it("replaces the explanation it opened on, and never one another session made since", async () => {
    at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
    const states = [linked()];
    const fake = renderApp({ state: () => json(states[0]), onPost: () => {
      // another session replaced the explanation meanwhile: the engine refuses and sends the new one
      const now = linked();
      const t = now.transactions.rows.find((x) => x.id === POPESCU)!;
      (t.explanation as { link: TxLink }).link.id = "ffffffffffffffff";
      return json({ ok: false, message: "Explicația tranzacției s-a schimbat între timp.",
        next: null, done: null, state: now });
    } });
    const s = await sheet();
    await userEvent.click(s.getByRole("button", { name: "Schimbă explicația" }));
    await userEvent.click(s.getByRole("radio", { name: "Comision curier sau procesator" }));
    await userEvent.click(s.getByRole("button", { name: "Asociază tranzacția" }));
    expect(fake.posts().map((c) => c.body)).toEqual([{ co: "NORD", tx: POPESCU, invoices: "5001", payment: "",
      reason: "courier_fee", note: "", was: "a1b2c3d4e5f60718", remember: "" }]);
    expect(await screen.findByText("Explicația tranzacției s-a schimbat între timp.")).toBeInTheDocument();
    // the new explanation shows; the old choices are not sent again with its id
    expect(await s.findByRole("button", { name: "Schimbă explicația" })).toBeInTheDocument();
    expect(s.queryByRole("button", { name: "Asociază tranzacția" })).toBeNull();
  });

  it("keeps what is picked in one currency, and counts a card in its original one", async () => {
    const s0 = fixture();
    const t = s0.transactions.rows.find((x) => x.id === NIMBUS)!; // −173,45 lei, 37,00 USD
    t.orig = { amount: "20.00", currency: "EUR" };
    t.partner = "NORD.1003";
    Object.assign(s0.invoices.find((r) => r.id === "103")!, { left: "20.00", covered: "0.00" }); // SL-0091, 20,00 EUR
    s0.invoices.push({ ...s0.invoices.find((r) => r.id === "103")!, id: "199", number: "SL-0199", currency: "RON", total: "173.45",
      left: "173.45" });
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    renderApp({ state: () => json(s0) });
    const s = await sheet();
    await userEvent.click(s.getByRole("checkbox", { name: /Factura SL-0091/ }));
    expect(s.getByRole("checkbox", { name: /Factura SL-0199/ })).toBeDisabled();
    expect(s.getByText("Tranzacția").nextSibling).toHaveTextContent("20,00 EUR");
    expect(s.queryByText("Diferență")).toBeNull(); // 20 EUR for 20 EUR
    await userEvent.click(s.getByRole("checkbox", { name: /Factura SL-0091/ }));
    await userEvent.click(s.getByRole("checkbox", { name: /Factura SL-0199/ }));
    expect(s.getByText("Tranzacția").nextSibling).toHaveTextContent("173,45 lei");
  });

  it.each([
    // a rule is saved by default where it applies on its own: by the personal card, else by the merchant; never a transfer
    ["Fără document", "Marchează fără document", { kind: "label", reason: "personal", note: "masă de protocol", who: "", remember: "card" }],
    ["Transfer între conturi", "Marchează ca transfer", { kind: "transfer", reason: "", note: "", who: "", remember: "" }],
    ["Avans de trezorerie", "Marchează ca avans", { kind: "advance", reason: "", note: "", who: "Administrator Exemplu", remember: "name" }],
  ])("marks what no document explains: %s", async (kind, button, body) => {
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    const fake = renderApp();
    const s = await sheet();
    await userEvent.click(s.getByRole("radio", { name: kind }));
    if (body.kind === "label") { // incomplete: the engine's refusal names what is missing
      await userEvent.click(s.getByRole("button", { name: button }));
      expect(fake.posts().pop()!.body).toEqual({ co: "NORD", tx: NIMBUS, kind: "label", reason: "", note: "", who: "", was: "", remember: "name" });
      await userEvent.click(s.getByRole("radio", { name: "Cheltuială personală" }));
      await userEvent.type(s.getByRole("textbox", { name: "Notă pentru contabil" }), "masă de protocol");
    }
    if (body.kind === "advance") await userEvent.type(s.getByRole("textbox", { name: "Administratorul sau asociatul" }), "Administrator Exemplu");
    await userEvent.click(s.getByRole("button", { name: button }));
    expect(fake.posts().at(-1)!.body).toEqual({ co: "NORD", tx: NIMBUS, ...body, was: "" });
  });

  it("names a receipt already uploaded, with no data yet, as a card payment's bon", async () => {
    const full = fixture(), d = full.documents.find((x) => x.vendor === "Benzinaria Nord")!;
    Object.assign(d, { vendor: "", date: null, amount: null, held: true, sent: false, invoice: null });
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    const fake = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url))),
      transactionChoices: (url) => json(choicesResult(full, query(url))) });
    const s = await sheet();
    const picker = s.getByRole("region", { name: "Bonuri încărcate fără date" });
    expect(within(picker).getAllByRole("button", { name: "Este bonul acestei plăți" })).toHaveLength(1);
    await userEvent.click(within(picker).getByRole("button", { name: "Este bonul acestei plăți" }));
    expect(fake.posts().at(-1)!.body).toEqual({ co: "NORD", tx: NIMBUS, id: d.id });
    expect(fake.posts().at(-1)!.url).toContain("tx-receipt");
  });

  it("starts a card payment on its invoices when the engine proposes some", async () => {
    const full = fixture(), t = full.transactions.rows.find((r) => r.id === NIMBUS)!;
    t.proposal = { kind: "invoices", invoices: [full.invoices[0].id] };
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url))),
      transactionChoices: (url) => json(choicesResult(full, query(url))) });
    expect((await sheet()).getByRole("radio", { name: "Factură" })).toBeChecked();
  });

  it("offers a debit's answers, waits for its invoice, and starts a bank fee on its label", async () => {
    const full = fixture(), t = full.transactions.rows.find((r) => r.id === NIMBUS)!;
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    const fake = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url))),
      transactionChoices: (url) => json(choicesResult(full, query(url))) });
    const s = await sheet();
    expect(s.getAllByRole("radio").slice(0, 6).map((r) => r.closest("label")!.textContent)).toEqual(["Factură", "Aștept factura",
      "Bon / chitanță", "Fără document", "Transfer între conturi", "Avans de trezorerie"]);
    // a card payment nothing points to starts on its bon
    expect(s.getByRole("radio", { name: "Bon / chitanță" })).toBeChecked();
    expect(s.getByRole("button", { name: "Încarcă bonul" })).toBeInTheDocument();
    await userEvent.click(s.getByRole("radio", { name: "Factură" }));
    expect(s.getByRole("link", { name: "Furnizor nou" })).toBeInTheDocument();
    // no prepared payment of this amount: no list, one way to search the file
    expect(s.queryByText("Plăți pregătite")).toBeNull();
    await userEvent.click(s.getByRole("button", { name: "Caută o plată pregătită" }));
    expect(s.getByRole("textbox", { name: "Caută plățile pregătite" })).toHaveFocus();
    expect(s.queryByRole("button", { name: "Aștept factura" })).toBeNull(); // its own kind, not a second button under the partner
    await userEvent.click(s.getByRole("radio", { name: "Aștept factura" }));
    await userEvent.click(s.getByRole("button", { name: "Aștept factura" })); // one tap: the note is optional
    expect(fake.posts()[0].body).toEqual({ co: "NORD", tx: NIMBUS, on: "1", note: "" });
    cleanup();
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    t.awaiting = { note: "", by: "admin", at: "2026-10-26T09:30:00" };
    const waiting = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url))),
      transactionChoices: (url) => json(choicesResult(full, query(url))) });
    const w = await sheet();
    expect(w.getByRole("radio", { name: "Aștept factura" })).toBeChecked(); // it opens on what it is
    await userEvent.click(w.getByRole("button", { name: "Nu mai aștept factura" }));
    expect(waiting.posts()[0].body).toEqual({ co: "NORD", tx: NIMBUS, on: "0" });
    t.awaiting = null;
    t.bank_says = "bank_fee";
    cleanup();
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    const fee = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url))),
      transactionChoices: (url) => json(choicesResult(full, query(url))) });
    await userEvent.click((await sheet()).getByRole("button", { name: "Marchează fără document" })); // row → Marchează
    expect(fee.posts()[0].body).toEqual({ co: "NORD", tx: NIMBUS, kind: "label", reason: "bank_fee", note: "", who: "", was: "", remember: "" });
    cleanup();
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    const link: TxLink = { id: "c1b2c3d4e5f60718", tx: NIMBUS, kind: "label", other: null, partner: null, parts: [], payments: [],
      reason: "bank_fee", difference: "0.00", currency: "RON", note: "", rule: "bank", by: "", at: "2026-10-26T09:30:00" };
    t.explanation = { kind: "label", label: "bank_fee", note: null, link };
    renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, query(url))),
      transactionChoices: (url) => json(choicesResult(full, query(url))) });
    // the bank's own fee line explained itself: the sheet says so, and it can be changed or undone like any other
    expect((await sheet()).getByText("Explicată").nextSibling).toHaveTextContent("26 oct, după extrasul băncii");
    expect((await sheet()).getByRole("button", { name: "Anulează asocierea" })).toBeInTheDocument();
    await userEvent.click((await sheet()).getByRole("button", { name: "Schimbă explicația" }));
    expect((await sheet()).queryByRole("radio", { name: "Bon / chitanță" })).toBeNull(); // a bon can't replace an explanation
  });

  it("shows a transfer on both ends, and undoes it where it was made", async () => {
    const s0 = fixture();
    const link: TxLink = { id: "b1b2c3d4e5f60718", tx: NIMBUS, kind: "transfer", other: POPESCU, partner: null, parts: [],
      payments: [], reason: null, difference: "0.00", currency: "RON", note: "", rule: null, by: "admin", at: "2026-10-26T09:30:00" };
    for (const [id, other] of [[NIMBUS, POPESCU], [POPESCU, NIMBUS]]) {
      s0.transactions.rows.find((x) => x.id === id)!.explanation = { kind: "transfer", other, link };
    }
    at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
    const fake = server({ state: () => json(s0) });
    render(<App />);
    const s = await sheet();
    expect(s.getByRole("heading", { name: "Transfer între conturi" })).toBeInTheDocument();
    expect(s.getByRole("link", { name: "cont 0001, 21 oct" })).toHaveAttribute("href", `?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    expect(s.queryByRole("button", { name: "Schimbă explicația" })).toBeNull(); // changed where it is recorded
    await userEvent.click(s.getByRole("button", { name: "Anulează asocierea" }));
    await answer();
    await waitFor(() => expect(fake.posts()).toHaveLength(1));
    expect(fake.posts()[0].body).toEqual({ tx: NIMBUS, was: "b1b2c3d4e5f60718", co: "NORD" });
  });

  it.each([
    ["credit", null, { name: "CLIENT EXEMPLU" }, "Partenerul CLIENT EXEMPLU"],
    ["debit", null, { name: "FURNIZOR EXEMPLU" }, "Partenerul FURNIZOR EXEMPLU"],
    ["debit", "0001", { name: "MAGAZIN EXEMPLU" }, "Comerciantul MAGAZIN EXEMPLU"],
    ["credit", "0001", { name: "RETUR EXEMPLU" }, "Partenerul RETUR EXEMPLU"],
    ["credit", null, { iban: "••••0002", name: "CLIENT EXEMPLU" }, "Contul ••••0002"],
  ] as const)("names the remembered counterparty: %s %s %j", async (direction, card, remember, noun) => {
    const s0 = fixture();
    Object.assign(s0.transactions.rows.find((x) => x.id === POPESCU)!, { direction, card, card_owner: card ? "NORD" : null, remember });
    at(`?co=NORD&tab=tranzactii&tx=${POPESCU}`);
    renderApp({ state: () => json(s0) });
    const s = await sheet();
    if (direction === "debit" && card) await userEvent.click(s.getByRole("radio", { name: "Factură" }));
    expect(s.getByRole("checkbox", { name: "Salvează regula" })).toHaveAccessibleDescription(
      `${noun}: regula se propune la tranzacțiile următoare.`); // an invoice stays a person's pick
  });

  it("remembers an explanation for the transaction's card when asked, and deletes what is remembered", async () => {
    const s0 = fixture();
    const t = s0.transactions.rows.find((x) => x.id === NIMBUS)!;
    t.remember = { card: "••••0001", name: "NIMBUS HOSTING INC" };
    s0.transactions.remembered = [{ id: "m1", by: "name", shown: "FIGMA INC", kind: "label", partner: null, reason: "other",
      note: "abonament", who_by: "admin", at: "2026-10-26T09:30:00" }];
    at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
    const fake = renderApp({ state: () => json(s0), onPost: () => json({ ok: true, message: "", next: null, done: null, state: s0 }) });
    const s = await sheet();
    await userEvent.click(s.getByRole("radio", { name: "Fără document" }));
    await userEvent.click(s.getByRole("radio", { name: "Cheltuială personală" }));
    expect(s.getByRole("checkbox", { name: "Salvează regula" })).toHaveAccessibleDescription(
      "Cardul ••••0001: regula explică automat tranzacțiile următoare și pe cele încă neexplicate.");
    expect(s.getByRole("checkbox", { name: "Salvează regula" })).toBeChecked();
    await userEvent.click(s.getByRole("button", { name: "Marchează fără document" }));
    expect(fake.posts()[0].body).toMatchObject({ kind: "label", reason: "personal", remember: "card" });
    at("?co=NORD&tab=automatizari");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await userEvent.click(await screen.findByRole("button", { name: "Șterge asocierea" }));
    expect(fake.posts()[1]).toMatchObject({ url: "/web/api/memory-forget", body: { id: "m1", co: "NORD" } });
  });
});

describe("Plată de lămurit", () => {
  it("shows a debit that fits several invoices and posts { co, tx, target } for the one the user picks", async () => {
    at("?co=STUDIO9&tab=sarcini");
    const s = fixture("STUDIO9");
    const [a] = s.ambiguous;
    const api = server({ state: () => json(s),
      onPost: () => json({ ok: true, message: "Factura CX-1010 este confirmată ca plătită, cu plata de pe extras ca dovadă.", state: s }) });
    render(<App />);
    const card = (await screen.findByText("Plată de lămurit")).closest(".task") as HTMLElement;
    expect(card).toHaveTextContent(
      "Plată de 350,00 lei pe 25 oct către CONTABIL EXPERT SRL. Se potrivește cu mai multe facturi. Alege factura plătită.");
    expect(within(card).getAllByRole("button").map((b) => b.textContent)).toEqual(
      ["Factura CX-1009, 350,00 lei, din 22 sep", "Factura CX-1010, 350,00 lei, din 21 oct"]);
    await userEvent.click(within(card).getByRole("button", { name: "Factura CX-1010, 350,00 lei, din 21 oct" }));
    expect(await answer()).toBe("Plata de pe extras devine dovada facturii CX-1010?");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "STUDIO9", tx: a.tx, target: "204" });
    expect(await screen.findByText("Factura CX-1010 este confirmată ca plătită, cu plata de pe extras ca dovadă.")).toBeInTheDocument();
  });

  it("names the statement currency of a foreign-currency debit", async () => {
    at("?co=STUDIO9&tab=sarcini");
    const s = fixture("STUDIO9");
    s.ambiguous[0].currency = "EUR";
    renderApp({ state: () => json(s) });
    const card = (await screen.findByText("Plată de lămurit")).closest(".task") as HTMLElement;
    expect(card).toHaveTextContent("Plată de 350,00 EUR pe 25 oct către CONTABIL EXPERT SRL.");
  });

  it.each([true, false])("is answered under De rezolvat in its month, in Sandbox too, or opens its transaction: %s", async (canResolve) => {
    at("?co=STUDIO9&tab=rapoarte&month=2026-10");
    const s = fixture("STUDIO9");
    s.live = false;
    s.ambiguous[0].can_resolve = canResolve;
    renderApp({ state: () => json(s) });
    const row = (await screen.findByText("Plată de lămurit")).closest(".row") as HTMLElement;
    expect(row).toHaveTextContent("Plată de 350,00 lei pe 25 oct către CONTABIL EXPERT SRL.");
    expect(row.parentElement!.querySelectorAll("button")).toHaveLength(canResolve ? 2 : 0);
    if (!canResolve) expect(row).toHaveAttribute("href", expect.stringContaining(`tx=${s.ambiguous[0].tx}`));
  });
});


it.each(["NORD", "personal"])("asks for an unknown card owner without confirming payment: %s", async (owner) => {
  const initial = fixture();
  const t = initial.transactions.rows.find((r) => r.id === NIMBUS)!;
  t.card_owner = null;
  at(`?co=NORD&tab=tranzactii&tx=${NIMBUS}`);
  const fake = renderApp({ state: () => json(initial), onPost: () => json({ ok: false, message: "Refuzat" }) });
  const s = await sheet();
  expect(s.queryByRole("button", { name: "Asociază tranzacția" })).toBeNull();
  const label = owner === "personal" ? "Card personal" : `Cardul firmei ${initial.companies.find((c) => c.key === owner)!.name}`;
  await userEvent.click(s.getByRole("button", { name: label }));
  await waitFor(() => expect(fake.posts().map((c) => [c.url, c.body])).toEqual([
    ["/web/api/card-owner", { co: "NORD", tx: NIMBUS, owner }],
  ]));
});
