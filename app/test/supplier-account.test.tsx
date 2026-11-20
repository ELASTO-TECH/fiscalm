import { screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { at, fixture, json, renderApp } from "./helpers";

it("shows a quiet previous account notice without blocking payment preparation", async () => {
  const s = fixture();
  const invoice = s.invoices.find((r) => r.id === "109")!;
  invoice.previous_iban_last4 = ["1234"];
  invoice.can_approve = true;
  at("?co=NORD&tab=plati&inv=109");
  renderApp({ state: () => json(s) });
  expect(await screen.findByText("IBAN diferit de cel plătit anterior (•••• 1234)")).toBeVisible();
  expect(screen.getByRole("button", { name: "Pune în fișier" })).toBeEnabled();
  expect(screen.queryByRole("checkbox")).toBeNull();
});
