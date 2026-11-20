// Mailed invoices (fiscalm.mail.invoices): filed ones in Cheltuieli › Facturi, the ones in doubt answered from Sarcini.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { State } from "@/lib/types";
import { at, fixture, json, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());

describe("mailed invoices", () => {
  it("lists a mailed invoice in Facturi only, opening its document", async () => {
    const cloud = fixture().documents.find((d) => d.mail)!;
    at("?co=NORD&tab=cheltuieli");
    renderApp();
    expect((await screen.findByText(cloud.vendor)).closest(".row")).toHaveAttribute("href", `?co=NORD&tab=cheltuieli&doc=${cloud.id}`);
    await userEvent.click(screen.getByRole("link", { name: "Bonuri și chitanțe" }));
    expect(screen.queryByText(cloud.vendor)).toBeNull();
  });

  it.each([["NORD", { who: "NORD", total: "0", remember: "1" }], ["none", { who: "none", remember: "1" }]])(
    "answers a question with %s, the total picked and the sender remembered", async (_, body) => {
      const s: State = fixture();
      const q = { ...s.to_check[0], totals: [{ amount: "249.90", currency: "EUR" }, { amount: "209.92", currency: "EUR" }] };
      s.to_check = [q];
      // Opened over Rapoarte, which shows the same question: the sheet's controls are its own.
      at(`?co=NORD&tab=rapoarte&month=2026-10&mail=${q.key}`);
      const api = renderApp({ state: () => json(s) });
      const dialog = within(await screen.findByRole("dialog", { busy: false }));
      if (body.who === "NORD") {
        expect(dialog.getByRole("button", { name: "Atribuie NORD" })).toBeDisabled(); // the total first
        await userEvent.click(dialog.getByText("249,90 EUR"));
        expect(dialog.getByRole("checkbox", { name: "Salvează regula pentru expeditor" })).toBeChecked(); // on by default
        await userEvent.click(dialog.getByRole("button", { name: "Atribuie NORD" }));
      } else {
        await userEvent.click(dialog.getByRole("button", { name: "Nu este factură" }));
      }
      expect(api.posts().map((c) => c.body)).toEqual([{ co: "NORD", key: q.key, ...body }]);
    });

  it("a document outside the inbox's firms is dismissed without a sender rule", async () => {
    const s: State = fixture();
    const q = { ...s.to_check[0], reason: "outside-scope" as const, companies: [] };
    s.to_check = [q];
    at(`?co=NORD&tab=rapoarte&month=2026-10&mail=${q.key}`);
    const api = renderApp({ state: () => json(s) });
    const dialog = within(await screen.findByRole("dialog", { busy: false }));
    expect(dialog.queryByRole("checkbox", { name: "Salvează regula pentru expeditor" })).toBeNull();
    await userEvent.click(dialog.getByRole("button", { name: "Nu este factură" }));
    expect(api.posts().map((c) => c.body)).toEqual([{ co: "NORD", key: q.key, who: "none" }]);
  });
});

it("assigns an uploaded photo from its preview without a sender rule", async () => {
  const s = fixture();
  const q = { ...s.to_check[0], reason: "upload-firm" as const, companies: ["NORD", "STUDIO9"], totals: [], file: "jpg" as const };
  s.to_check = [q];
  at(`?co=NORD&tab=sarcini&mail=${q.key}`);
  const api = renderApp({ state: () => json(s) });
  const dialog = within(await screen.findByRole("dialog", { name: "Pentru ce firmă e documentul?", busy: false }));
  expect(dialog.queryByRole("checkbox")).toBeNull();
  expect(dialog.queryByRole("button", { name: "Nu este factură" })).toBeNull();
  expect(dialog.getByRole("img")).toHaveAttribute("src", expect.stringContaining("docfile?"));
  await userEvent.click(dialog.getByRole("button", { name: "Atribuie STUDIO9" }));
  expect(api.posts()).toEqual([{ url: "/web/api/upload-assign", method: "POST", body: { co: "NORD", key: q.key, who: "STUDIO9" } }]);
});
