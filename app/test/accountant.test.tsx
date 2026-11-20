import { screen, waitFor } from "@testing-library/react";
import { renderSignedIn as render, choose, choicesOf } from "./helpers";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it } from "vitest";
import App from "@/components/App";
import { allowedInShadow } from "@/lib/api";
import { at, fixture, json, server } from "./helpers";

beforeEach(() => localStorage.clear());

/** The partners load after the page: picks `cui` once it is among them. */
async function choosePartner(cui: string) {
  const select = await screen.findByLabelText("Partener");
  await waitFor(() => choose(select, cui));
}

it("opens the per-firm page and saves only the partner CUI and addresses in shadow mode", async () => {
  const s = fixture();
  s.live = false;
  s.can_manage_companies = true;
  s.accountant = { cui: "", senders: [], recipient: "" };
  const p = s.partners.find((p) => p.cui && /^\d{2,10}$/.test(p.cui))!;
  // the engine's answer carries what it saved under `done`: the page stays, never the payment file's success sheet
  const { calls } = server({ state: () => json(s), onPost: () => json({ ok: true, message: "Datele contabilului au fost salvate.", state: s,
    done: { cui: p.cui, senders: ["op@accountant.example"], recipient: "pack@accountant.example" } }) });
  at("?co=NORD&tab=setari");
  render(<App />);
  await userEvent.click(await screen.findByRole("link", { name: /Contabil/ }));
  expect(await screen.findByRole("heading", { name: "Contabil", level: 1 })).toBeInTheDocument();
  const save = screen.getByRole("button", { name: "Salvează datele contabilului" });
  expect(save).toBeDisabled();
  expect(save).toHaveAccessibleDescription("Nicio modificare de salvat.");
  await choosePartner(p.cui!);
  await userEvent.type(screen.getByLabelText("E-mailuri de la contabil"), "op@accountant.example");
  await userEvent.type(screen.getByLabelText("Adresa la care trimiți dosarul"), "pack@accountant.example");
  await userEvent.click(save);
  await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(1));
  expect(calls.find((c) => c.method === "POST")?.body).toEqual({ co: "NORD", cui: p.cui, senders: "op@accountant.example", recipient: "pack@accountant.example" });
  expect(calls.find((c) => c.method === "POST")?.url).toContain("accountant-save");
  expect(allowedInShadow("accountant-save")).toBe(true);
  expect(await screen.findByText("Datele contabilului au fost salvate.")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Contabil", level: 1 })).toBeInTheDocument();
});

it("changing or clearing the role clears its addresses, while bank details stay on the supplier", async () => {
  const s = fixture();
  s.can_manage_companies = true;
  const p = s.partners.find((p) => p.cui && /^\d{2,10}$/.test(p.cui))!;
  s.accountant = { cui: p.cui!, senders: ["op@accountant.example"], recipient: "pack@accountant.example" };
  const { calls } = server({ state: () => json(s) });
  at("?co=NORD&tab=setari&sec=contabil");
  render(<App />);
  expect(await screen.findByRole("heading", { name: "Facturi și cont bancar" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: new RegExp(p.name) })).toHaveAttribute("href", expect.stringContaining(`p=${encodeURIComponent(p.id)}`));
  expect(screen.queryByRole("button", { name: "Confirmă contul" })).not.toBeInTheDocument();
  const other = s.partners.find((x) => x.cui && /^\d{2,10}$/.test(x.cui) && x.cui !== p.cui)!;
  await choosePartner(other.cui!);
  expect(screen.getByLabelText("E-mailuri de la contabil")).toHaveValue("");
  expect(screen.getByLabelText("Adresa la care trimiți dosarul")).toHaveValue("");
  await choosePartner("");
  expect(screen.getByLabelText("E-mailuri de la contabil")).toHaveValue("");
  expect(screen.getByLabelText("Adresa la care trimiți dosarul")).toHaveValue("");
  await userEvent.click(screen.getByRole("button", { name: "Salvează datele contabilului" }));
  await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ co: "NORD", cui: "", senders: "", recipient: "" }));
});

it("shows the role to a member without allowing edits", async () => {
  const s = fixture();
  s.can_manage_companies = false;
  server({ state: () => json(s) });
  at("?co=NORD&tab=setari&sec=contabil");
  render(<App />);
  expect(await screen.findByText("Doar administratorii pot schimba datele contabilului.")).toBeInTheDocument();
  expect(screen.getByLabelText("Partener")).toBeDisabled();
  expect(screen.getByLabelText("E-mailuri de la contabil")).toBeDisabled();
  expect(screen.getByLabelText("Adresa la care trimiți dosarul")).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Salvează datele contabilului" })).not.toBeInTheDocument();
});

it("restores saved addresses when returning to the saved partner", async () => {
  const s = fixture();
  s.can_manage_companies = true;
  const [first, other] = s.partners.filter((p) => p.cui && /^\d{2,10}$/.test(p.cui));
  s.accountant = { cui: first.cui!, senders: ["op@accountant.example"], recipient: "pack@accountant.example" };
  server({ state: () => json(s) });
  at("?co=NORD&tab=setari&sec=contabil");
  render(<App />);
  await choosePartner(other.cui!);
  expect(screen.getByLabelText("Adresa la care trimiți dosarul")).toHaveValue("");
  await choosePartner(first.cui!);
  expect(screen.getByLabelText("E-mailuri de la contabil")).toHaveValue("op@accountant.example");
  expect(screen.getByLabelText("Adresa la care trimiți dosarul")).toHaveValue("pack@accountant.example");
  expect(screen.getByRole("button", { name: "Salvează datele contabilului" })).toBeDisabled();
});

it("keeps edits after a refused save and displays the refusal", async () => {
  const s = fixture();
  s.can_manage_companies = true;
  const p = s.partners.find((p) => p.cui && /^\d{2,10}$/.test(p.cui))!;
  s.accountant = { cui: p.cui!, senders: [], recipient: "" };
  server({ state: () => json(s), onPost: () => json({ ok: false, message: "Adresele de e-mail nu sunt valide.", state: s }) });
  at("?co=NORD&tab=setari&sec=contabil");
  render(<App />);
  await userEvent.type(await screen.findByLabelText("E-mailuri de la contabil"), "invalid");
  await userEvent.click(screen.getByRole("button", { name: "Salvează datele contabilului" }));
  expect(await screen.findByText("Adresele de e-mail nu sunt valide.")).toBeInTheDocument();
  expect(screen.getByLabelText("E-mailuri de la contabil")).toHaveValue("invalid");
});

it("keeps an unavailable saved partner visible by CUI", async () => {
  const s = fixture();
  s.can_manage_companies = true;
  s.accountant = { cui: "88888888", senders: [], recipient: "" };
  server({ state: () => json(s) });
  at("?co=NORD&tab=setari&sec=contabil");
  render(<App />);
  const partner = await screen.findByLabelText("Partener");
  await waitFor(async () => expect((await choicesOf(partner)).map((o) => o.text)).toContain("CUI 88888888"));
  expect(screen.getByLabelText("Partener")).toHaveValue("88888888");
  await choosePartner("");
  expect(screen.getByRole("button", { name: "Salvează datele contabilului" })).toBeEnabled();
});

it("pages and searches all accountant choices while retaining an off-page saved identity and exact POST", async () => {
  const full = fixture(), base = full.partners[0];
  full.can_manage_companies = true;
  full.partners = Array.from({ length: 151 }, (_, i) => ({ ...base, id: `NORD.${50000000 + i}`, cui: String(50000000 + i), name: `Partener ${String(i).padStart(3, "0")}` }));
  full.accountant = { cui: full.partners[149].cui!, senders: ["old@example.test"], recipient: "pack@example.test" };
  at("?co=NORD&tab=setari&sec=contabil");
  const api = server({ state: () => json(full), onPost: () => json({ ok: true, state: full }) });
  render(<App />);
  const select = await screen.findByLabelText("Partener");
  expect(await screen.findByRole("link", { name: /Partener 149/ })).toHaveAttribute("href", expect.stringContaining("p=NORD.50000149"));
  await waitFor(async () => expect(await choicesOf(select)).toHaveLength(52));
  for (let i = 1; i <= 3; i++) {
    await userEvent.click(screen.getByRole("button", { name: "Pagina următoare" }));
    await waitFor(async () => expect((await choicesOf(select)).some((o) => o.value === String(50000000 + i * 50))).toBe(true));
  }
  expect(screen.getByRole("button", { name: "Pagina următoare" })).toHaveFocus();
  await choosePartner("50000150");
  expect(screen.getByLabelText("E-mailuri de la contabil")).toHaveValue("");
  await userEvent.type(screen.getByLabelText("Caută partenerul"), "Partener 000");
  await waitFor(async () => expect((await choicesOf(select)).map((o) => o.text)).toContain("Partener 000 (CUI 50000000)"));
  expect(select).toHaveValue("50000150");
  expect(await choicesOf(select)).toHaveLength(4);
  await userEvent.type(screen.getByLabelText("E-mailuri de la contabil"), "new@example.test");
  await userEvent.click(screen.getByRole("button", { name: "Salvează datele contabilului" }));
  await waitFor(() => expect(api.posts()[0]?.body).toEqual({ co: "NORD", cui: "50000150", senders: "new@example.test", recipient: "" }));
});
