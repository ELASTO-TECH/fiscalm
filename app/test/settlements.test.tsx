import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Settlement } from "@/lib/types";
import { fixtureDetailData } from "@/lib/fixtures";
import { at, detailResult, fixture, json, renderApp, summaryResult } from "./helpers";

const id = "a".repeat(32), tx = "b".repeat(20);
export const settlement = (): Settlement => ({
  id, kind: "settlement", company: "NORD", source: "fan", report_key: "BATCH-1", reference: "BATCH-1", candidate: null,
  payer: { id: "synthetic-payer", name: "Curier Exemplu SRL" }, date: "2026-10-30", currency: "RON",
  gross: "300.00", fee: "6.00", net: "294.00", status: "pending", error: "confirmation_required", failed_line: null,
  tx_key: null, invoice_count: 2, confirmed_count: 1, auto_after: 3, can_confirm: true, human_confirmed: false,
  credit: { id: tx, date: "2026-10-31", amount: "294.00", currency: "RON" },
  sheet: { co: "NORD", set: id }, links: { transaction: null, issued: ["5001", "5002"] },
  lines: [{ position: 1, reference: "NRD 0042", awb: null, order: null, gross: "100.00", fee: "2.00", invoice: "5001", proved: false },
    { position: 2, reference: "NRD 0043", awb: null, order: null, gross: "200.00", fee: "4.00", invoice: "5002", proved: false }],
});

beforeEach(() => { localStorage.clear(); at("?co=NORD&tab=sarcini"); });
afterEach(() => vi.unstubAllGlobals());

it("pages settlement lines without root history, keeping complete money, links and confirmation", async () => {
  const full = fixture(), report = settlement();
  Object.assign(report, { invoice_count: 151, gross: "302.00", fee: "1.51", net: "300.49", credit: { ...report.credit!, amount: "300.49" } });
  report.lines = Array.from({ length: 151 }, (_, i) => ({ ...report.lines[0], position: i + 1, reference: `SYN-${i}`, gross: "2.00", fee: "0.01" }));
  full.settlements = [report];
  for (const cursor of ["", "150"]) {
    const actual = fixtureDetailData(full, "settlement", id, cursor), expected = detailResult(full, { kind: "settlement", id, cursor });
    expect(actual.state.settlements).toEqual(expected.state.settlements);
    expect(actual.related).toEqual(expected.related);
  }
  at(`?co=NORD&tab=sarcini&set=${id}`);
  const api = renderApp({ state: () => json(summaryResult(full)), detail: (url) => json(detailResult(full, Object.fromEntries(new URL(url, "http://local").searchParams))) });
  const dialog = await screen.findByRole("dialog", { name: "Decontare" });
  expect(within(dialog).getAllByRole("link", { name: /^Factura SYN-/ })).toHaveLength(50);
  for (let i = 0; i < 3; i++) {
    await userEvent.click(within(dialog).getByRole("button", { name: "Pagina următoare" }));
    await waitFor(() => expect(within(dialog).queryByText("Se încarcă…")).toBeNull());
  }
  expect(within(dialog).getByText("151 de facturi")).toBeVisible();
  expect(within(dialog).getByText("Total net").nextElementSibling).toHaveTextContent(/^300,49 lei$/);
  expect(within(dialog).getByText("Comision curier sau procesator").nextElementSibling).toHaveTextContent(/^1,51 lei$/);
  expect(within(dialog).getAllByRole("link", { name: /^Factura SYN-/ })).toHaveLength(1);
  expect(within(dialog).getByRole("link", { name: "Factura SYN-150" })).toHaveAttribute("href", "?co=NORD&tab=sarcini&iss=5001");
  expect(within(dialog).getByRole("button", { name: "Pagina următoare" })).toHaveFocus();
  await userEvent.click(within(dialog).getByRole("button", { name: "Confirmă decontarea" }));
  expect(api.posts().map((c) => c.body)).toEqual([{ co: "NORD", id, tx, confirmed: "yes" }]);
});

it("confirms exactly the displayed credit from the task card, the sheet one link away", async () => {
  const s = fixture(); s.settlements = [settlement()];
  const onPost = vi.fn(({ body }: { body: unknown }) => {
    expect(body).toEqual({ co: "NORD", id, tx, confirmed: "yes" });
    s.settlements![0].human_confirmed = true; s.settlements![0].status = "collected"; s.settlements![0].can_confirm = false;
    return json({ ok: true, message: "Decontare confirmată.", next: null, done: null, state: s });
  });
  renderApp({ state: () => json(s), onPost });
  const u = userEvent.setup();
  const card = (await screen.findByText("Decontare de confirmat")).closest(".task") as HTMLElement;
  expect(card).toHaveTextContent("2 facturi, net 294,00 lei, de la Curier Exemplu SRL. Așteaptă confirmarea ta: intrare de 294,00 lei pe 31 oct. După încă 2 confirmări, următoarele decontări care trec verificările se confirmă automat.");
  expect(within(card).getByRole("link", { name: "Vezi decontarea" })).toHaveAttribute("href", `?co=NORD&tab=sarcini&set=${id}`);
  await u.click(within(card).getByRole("button", { name: "Confirmă decontarea" }));
  await waitFor(() => expect(onPost).toHaveBeenCalledTimes(1));
  expect(screen.getByRole("status")).toHaveTextContent("Decontare confirmată.");
});

it("allows verified local confirmation in shadow mode", async () => {
  const s = fixture(); s.settlements = [settlement()]; s.live = false;
  const onPost = vi.fn();
  at(`?co=NORD&tab=sarcini&set=${id}`); renderApp({ state: () => json(s), onPost });
  for (const button of await screen.findAllByRole("button", { name: "Confirmă decontarea" })) expect(button).toBeEnabled();
  expect(onPost).not.toHaveBeenCalled();
});

it("counts nothing toward settling on its own for a payer without CUI or IBAN", async () => {
  const s = fixture(); s.settlements = [{ ...settlement(), auto_after: null, confirmed_count: 5 }];
  at(`?co=NORD&tab=sarcini&set=${id}`); renderApp({ state: () => json(s) });
  const dialog = await screen.findByRole("dialog", { busy: false });
  expect(dialog).not.toHaveTextContent("Încă");
  expect(within(dialog).getByRole("button", { name: "Confirmă decontarea" })).toBeEnabled();
});

it.each([
  ["invoice_not_open_or_amount_differs", "Linia 2: factura nu este deschisă sau suma diferă."],
  ["awb_unknown", "Linia 2: AWB 1000000001 nu corespunde niciunei comenzi."],
  ["order_unknown", "Linia 2: comanda 77 nu există în comenzile firmei."],
  ["invoice_missing_or_ambiguous", "Linia 2: factura NRD 0043 nu există, apare de mai multe ori sau nu e a acestei comenzi."],
])("names the line's real cause (%s) and offers no confirmation without a verified credit", async (reason, text) => {
  const s = fixture(), r = settlement();
  r.lines[1] = { ...r.lines[1], awb: "1000000001", order: "77", invoice: null };
  s.settlements = [{ ...r, credit: null, can_confirm: false, failed_line: 2, error: `line_2_${reason}` }];
  at(`?co=NORD&tab=sarcini&set=${id}`); renderApp({ state: () => json(s) });
  expect(await screen.findByRole("dialog", { busy: false })).toHaveTextContent(text);
  expect((await screen.findByText("Decontare de verificat")).closest(".task")).toHaveTextContent(text);
  expect(screen.queryByRole("button", { name: "Confirmă decontarea" })).not.toBeInTheDocument();
});


it("distinguishes automatic reconciliation from a human confirmation", async () => {
  const s = fixture(); s.settlements = [{ ...settlement(), status: "collected", human_confirmed: false, can_confirm: false, invoice_count: 1, credit: null, tx_key: tx }];
  at(`?co=NORD&tab=sarcini&set=${id}`); renderApp({ state: () => json(s) });
  const dialog = await screen.findByRole("dialog", { name: "Decontare" });
  expect(within(dialog).getByText("o factură")).toBeVisible();
  expect(within(dialog).getByText("Total net").nextElementSibling).toHaveTextContent(/^294,00 lei$/);
  expect(dialog).toHaveTextContent("Decontare confirmată automat, pe baza raportului și a extrasului.");
  expect(dialog).toHaveTextContent("Intrarea în cont apare în extras.");
  expect(screen.queryByRole("button", { name: "Confirmă decontarea" })).not.toBeInTheDocument();
});


it.each([false, true])("opens the invoice for a COD warning, returned=%s", async (returned) => {
  const s = fixture(), invoice = s.issued[0].id;
  s.cod = { count: returned ? 0 : 1, totals: [], overdue_days: 7,
    rows: [{ id: "order-cod", company: "NORD", invoice, awb: { courier: "fan", number: "AWB-SYNTHETIC" }, waiting: !returned, returned, collected: false, overdue: !returned }],
    tasks: [{ kind: returned ? "cod_returned" : "cod_overdue", id: "order-cod", company: "NORD", invoice,
      sheet: { co: "NORD", iss: invoice }, suggestion: returned ? "review_refund_draft" : null }] };
  const onPost = vi.fn(); renderApp({ state: () => json(s), onPost });
  const title = await screen.findByText(returned ? "Colet returnat" : "Ramburs întârziat");
  const card = title.closest(".task")!;
  expect(card).toHaveTextContent("AWB AWB-SYNTHETIC.");
  expect(card).toHaveTextContent(returned ? "Verifică dacă ai nevoie de o factură de stornare." : "Rambursul nu este confirmat în extras după livrare.");
  await userEvent.click(within(card as HTMLElement).getByRole("link", { name: "Vezi factura" }));
  expect(new URL(location.href).searchParams.get("iss")).toBe(invoice);
  expect(await screen.findByRole("dialog", { busy: false })).toHaveTextContent(s.issued[0].number);
  expect(onPost).not.toHaveBeenCalled();
});


it("shows a generated reference as absent, compares the nearest credit and withdraws only the report", async () => {
  const s = fixture(); s.settlements = [{ ...settlement(), reference: null, error: "credit_missing_or_ambiguous", credit: null, can_confirm: false,
    candidate: { id: tx, date: "2026-10-31", amount: "294.01", currency: "RON", payer: "Curier Exemplu SRL" } }];
  at(`?co=NORD&tab=sarcini&set=${id}`); const api = renderApp({ state: () => json(s) });
  const dialog = await screen.findByRole("dialog", { busy: false });
  expect(dialog).not.toHaveTextContent("Referință");
  expect(dialog).toHaveTextContent("Suma netă 294,00 lei nu se potrivește; cea mai apropiată: 294,01 lei");
  expect(within(dialog).getByRole("link", { name: "Vezi tranzacția" })).toHaveAttribute("href", `?co=NORD&tab=sarcini&tx=${tx}`);
  await userEvent.click(within(dialog).getByRole("button", { name: "Șterge raportul" }));
  await userEvent.click(within(screen.getByRole("dialog", { name: "Ștergi raportul greșit? Facturile rămân neîncasate." })).getByRole("button", { name: "Șterge raportul" }));
  expect(api.posts().map(c => c.body)).toEqual([{ co: "NORD", id }]);
});

it("distinguishes unreadable statements from a missing matching credit", async () => {
  const s = fixture(); s.settlements = [{ ...settlement(), error: "statement_unavailable", credit: null, can_confirm: false }];
  at(`?co=NORD&tab=sarcini&set=${id}`); renderApp({ state: () => json(s) });
  expect(await screen.findByRole("dialog", { busy: false })).toHaveTextContent("Extrasul încărcat nu poate fi citit.");
  expect(screen.queryByRole("button", { name: "Confirmă decontarea" })).not.toBeInTheDocument();
});
