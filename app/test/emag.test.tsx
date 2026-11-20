import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { fixtureDetailData } from "@/lib/fixtures";
import { at, detailResult, fixture, json, renderApp, summaryResult } from "./helpers";

it.each([false, true])("loads an order without root history and preserves its draft link and actions, issued=%s", async (issued) => {
  localStorage.clear();
  const full = fixture(), draft = "draft-synthetic", task = { id: "emag-task", external_id: "123", reason: "return_review", order_id: "order-synthetic", draft_id: draft };
  full.emag_tasks = [task]; full.issued = issued ? [{ ...full.issued[0], local_id: draft }] : [];
  const expected = detailResult(full, { kind: "order", id: "NORD:emag-task" });
  const actual = fixtureDetailData(full, "order", "NORD:emag-task");
  expect(actual.state.emag_tasks).toEqual(expected.state.emag_tasks);
  expect(actual.state.issued).toEqual(expected.state.issued);
  at("?co=NORD&tab=sarcini&ord=NORD%3Aemag-task");
  const api = renderApp({ state: () => json(summaryResult(full)), detail: () => json(expected) });
  const dialog = await screen.findByRole("dialog", { name: "Comandă eMAG" });
  expect(within(dialog).getByRole("link", { name: "Vezi factura" })).toHaveAttribute("href", `?co=NORD&tab=sarcini&${issued ? `iss=${full.issued[0].id}` : `new=${draft}`}`);
  await userEvent.click(within(dialog).getByRole("button", { name: issued ? "Marchează ca rezolvată" : "Pregătește ciorna de stornare" }));
  expect(api.posts().map((c) => [c.url.split("/").pop(), c.body])).toEqual([
    [issued ? "emag-resolve" : "emag-refund", { co: "NORD", id: task.id }],
  ]);
});

it("opens an eMAG return task in its own company sheet and sends nothing", async () => {
  localStorage.clear();
  at("?co=NORD&tab=sarcini");
  const s = fixture();
  s.emag_tasks = [{ id: "emag-task", external_id: "123", reason: "return_review", order_id: null, draft_id: null }];
  const api = renderApp({ state: () => json(s) });
  const link = await screen.findByRole("link", { name: "Vezi comanda" });
  expect(link.getAttribute("href")).toContain("co=NORD");
  expect(link.getAttribute("href")).toContain("ord=NORD%3Aemag-task");
  await userEvent.click(link);
  const sheet = await screen.findByRole("dialog", { name: "Comandă eMAG" });
  expect(within(sheet).getByText("Verifică returul și, dacă e cazul, stornează factura inițială.")).toBeInTheDocument();
  await userEvent.click(within(sheet).getByRole("button", { name: "Închide" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(api.posts()).toEqual([]);
});

it("never suggests a second invoice for an already imported order", async () => {
  const { emagReason } = await import("@/components/Sheets");
  for (const reason of ["order_review", "missing_vat", "shipping_tax", "api_unavailable", "awb_retry", "channel_changed", "unknown"]) {
    expect(emagReason(reason, true)).not.toMatch(/manual/);
  }
  expect(emagReason("awb_retry", true)).toBe("AWB-ul nu a putut fi preluat. Verifică livrarea din eMAG și factura existentă.");
});
