// The tick on a supplier in its sheet: "Pune automat în fișierul de plăți" posts rule-supplier-autopay for this firm and
// says what the engine answered; only an administrator can change it, and a partner the firm never received an e-Factura
// invoice from has nothing to tick.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { State } from "@/lib/types";
import { withPartner, at, json, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

async function open(state: State, id: string, name: string) {
  at(`?co=NORD&tab=parteneri&p=${id}`);
  const ON = "De acum, facturile de la Curierat Expres SRL se pun automat în fișierul de plăți.";
  const api = renderApp({ state: () => json(state), onPost: () => json({ ok: true, message: ON, state: withPartner(id, { autopay: true }) }) });
  return { api, d: within(await screen.findByRole("dialog", { name })) };
}

describe("Parteneri: plata automată", () => {
  it("ticking a supplier posts its CUI for this firm and repeats the engine's answer", async () => {
    const s = withPartner("NORD.1004", { autopay: false, can_set_autopay: true });
    const cui = s.partners.find((p) => p.id === "NORD.1004")!.cui!;
    const { api, d } = await open(s, "NORD.1004", "Curierat Expres SRL");
    const box = d.getByRole("checkbox", { name: "Pune automat în fișierul de plăți" });
    expect(box).not.toBeChecked();
    await userEvent.click(box);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/rule-supplier-autopay", method: "POST", body: { co: "NORD", cui, on: "1" } });
    expect(await screen.findByText("De acum, facturile de la Curierat Expres SRL se pun automat în fișierul de plăți.")).toBeInTheDocument();
    await waitFor(() => expect(d.getByRole("checkbox", { name: "Pune automat în fișierul de plăți" })).toBeChecked());
  });

  it("only an administrator can change it", async () => {
    const { d } = await open(withPartner("NORD.1004", { autopay: true, can_set_autopay: false }), "NORD.1004", "Curierat Expres SRL");
    expect(d.queryByRole("checkbox", { name: "Pune automat în fișierul de plăți" })).toBeNull();
    expect(d.getByText("Se pune automat în fișierul de plăți.")).toBeInTheDocument();
  });

  it("a partner with no e-Factura invoice has nothing to tick", async () => {
    const { d } = await open(withPartner("NORD.1004", { autopay: null, can_set_autopay: false }), "NORD.1004", "Curierat Expres SRL");
    expect(d.queryByRole("checkbox", { name: "Pune automat în fișierul de plăți" })).toBeNull();
  });
});

it("Nu pune în dosarul contabilului posts the partner for this firm", async () => {
  at("?co=NORD&tab=parteneri&p=NORD.n-hetzner");
  const api = renderApp({ onPost: () => json({ ok: true, message: "ok", state: withPartner("NORD.n-hetzner", { not_mailed: true }) }) });
  await userEvent.click(within(await screen.findByRole("dialog", { name: "Hetzner" })).getByRole("checkbox", { name: "Exclude din dosarul contabilului" }));
  await waitFor(() => expect(api.posts()[0]).toEqual({ url: "/web/api/rule-partner-not-mailed", method: "POST",
    body: { co: "NORD", partner: "NORD.n-hetzner", on: "1" } }));
});
