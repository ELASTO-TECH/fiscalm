// Automatizări (docs/openapi.json): the engine's rules grouped by what they prepare, each with its
// state and why it is held, Oprește / Pornește where the engine allows it, and every firm's rules under Toate firmele.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { at, deferred, fixture, json, renderApp } from "./helpers";
import type { Rule, State } from "@/lib/types";

beforeEach(() => {
  localStorage.clear();
});

const page = () => screen.findByRole("heading", { name: "Automatizări", level: 1 });
const main = () => within(screen.getByRole("main"));
/** Each group's heading and its rows' text, in order. */
const groups = () => main().getAllByRole("region").filter(region => within(region).getAllByRole("heading").length === 1)
  .map(region => [within(region).getByRole("heading").textContent,
    Array.from(region.lastElementChild?.children ?? [], row => row.textContent)]);
const toast = () => document.querySelector(".toast")?.textContent;

/** A state whose rule `id` is changed by `patch`. */
function withRule(s: State, id: string, patch: Partial<Rule>): State {
  s.rules = s.rules.map((r) => (r.id === id ? { ...r, ...patch } : r));
  return s;
}

describe("Automatizări for one firm", () => {
  it("lists the engine's rules by group, each with its state, what waits and why it is held", async () => {
    at("?co=NORD&tab=automatizari");
    renderApp();
    await page();
    expect(groups()).toEqual([
      ["Facturare", [
        "Trimitere automată în SPVFiecare factură emisă se trimite în SPV.Pornită. Se trimite la 4 zile după emitere.ModificăOprește",
        "Persoană Exemplu48,40 lei pe lună, scadența la 15 zile.OprităPorneșteȘterge",
        "Client Exemplu SRL302,50 lei pe lună, scadența la 15 zile.Pornită. Următoarea factură pe 1 nov.OpreșteȘterge",
        "Factură lunară nouăDintr-o ciornă, cu Repetă lunar",
      ]],
      ["Încasări", [
        "Reamintiri de platăCopiază textul și trimite-l clientului din e-mail."
          + "Disponibilă după 7 zile de la scadență.Modifică",
      ]],
      ["Plăți", [
        "Telecom Rapid SRLFacturile noi se adaugă în fișierul de plăți.Oprită. La pornire, o factură intră în fișierul de plăți.Pornește",
        "Paza Obiectiv SRLFacturile noi se adaugă în fișierul de plăți.PornităOprește",
      ]],
      ["Documente", [
        "Dosarul lunar pentru contabilDescarcă dosarul și trimite-l contabilului prin e-mail.Pornită. 8 dosare pregătite.",
        "Telefonul de serviciuBondrop, Bonurile trimise de pe telefon ajung direct la firmă.Pornită. 3 fișiere primite, ultimul pe 25 oct.",
      ]],
    ]);
    // Rules without a switch lead to where they are managed.
    expect(main().getByRole("link", { name: /^Dosarul lunar pentru contabil/ })).toHaveAttribute("href", "?co=NORD&tab=rapoarte");
    expect(main().getByRole("link", { name: /^Telefonul de serviciu/ })).toHaveAttribute("href", "?co=NORD&tab=setari&sec=acces");
    expect(main().getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual([
      "Modifică regula Trimitere automată în SPV", "Oprește regula Trimitere automată în SPV", "Pornește factura lunară pentru Persoană Exemplu", "Șterge factura lunară pentru Persoană Exemplu",
      "Oprește factura lunară pentru Client Exemplu SRL", "Șterge factura lunară pentru Client Exemplu SRL",
      "Modifică regula Reamintiri de plată", "Pornește regula Telecom Rapid SRL", "Oprește regula Paza Obiectiv SRL"]);
    expect(main().getByRole("link", { name: /^Factură lunară nouă/ })).toHaveAttribute("href", "?co=NORD&tab=vanzari#drafts-h");
    expect(main().queryByText(/administratorii/)).toBeNull();
  });

  it("Oprește posts rule-pause for the rule, stays on the page and shows the engine's answer", async () => {
    at("?co=NORD&tab=automatizari");
    const after = withRule(fixture(), "NORD:spv_auto_upload", { enabled: false, reason: "paused", can_pause: false, can_resume: true,
      settings: null, can_edit: false });
    const api = renderApp({ onPost: () => json({ ok: true, message: "Regula a fost oprită.", state: after,
      done: { id: "NORD:spv_auto_upload", enabled: false } }) });
    await page();
    const btn = main().getByRole("button", { name: "Oprește regula Trimitere automată în SPV" });
    await userEvent.click(btn);
    await waitFor(() => expect(toast()).toBe("Regula a fost oprită."));
    expect(api.posts().map((c) => [c.url, c.body])).toEqual([["/web/api/rule-pause", { co: "NORD", id: "NORD:spv_auto_upload" }]]);
    expect(window.location.search).toBe("?co=NORD&tab=automatizari"); // no success sheet, no move to Acasă
    expect(screen.queryByRole("dialog")).toBeNull();
    const now = main().getByRole("button", { name: "Pornește regula Trimitere automată în SPV" });
    expect(now.closest("a, div")).toHaveTextContent("Trimitere automată în SPVFiecare factură emisă se trimite în SPV.OprităPornește");
    expect(main().queryByRole("button", { name: "Modifică regula Trimitere automată în SPV" })).toBeNull(); // off, its day has no effect
    expect(now).toBe(btn); // the same button, so focus stays on it
  });

  it("Modifică on the running SPV rule chooses its day and posts rule-spv-delay", async () => {
    at("?co=NORD&tab=automatizari");
    const after = withRule(fixture(), "NORD:spv_auto_upload", { settings: { upload_after_days: 0 } });
    const api = renderApp({ onPost: () => json({ ok: true, message: "Ziua trimiterii în SPV a fost salvată.", state: after,
      done: { upload_after_days: 0 } }) });
    await page();
    await userEvent.click(main().getByRole("button", { name: "Modifică regula Trimitere automată în SPV" }));
    await userEvent.click(main().getByRole("combobox", { name: "Când se trimite" }));
    await userEvent.click(screen.getByRole("option", { name: "La prima verificare după emitere" }));
    await userEvent.click(main().getByRole("button", { name: "Salvează" }));
    await waitFor(() => expect(toast()).toBe("Ziua trimiterii în SPV a fost salvată."));
    expect(api.posts().map((c) => [c.url, c.body])).toEqual([["/web/api/rule-spv-delay", { co: "NORD", days: "0" }]]);
    expect(main().getByText("Trimitere automată în SPV", { selector: "b" }).closest("div"))
      .toHaveTextContent("Pornită. Se trimite la prima verificare după emitere.");
    expect(main().queryByRole("combobox", { name: "Când se trimite" })).toBeNull();
  });

  it("keeps the SPV day from changing while its save is on its way", async () => {
    at("?co=NORD&tab=automatizari");
    const answer = deferred<Response>();
    const api = renderApp({ onPost: () => answer.promise });
    await page();
    await userEvent.click(main().getByRole("button", { name: "Modifică regula Trimitere automată în SPV" }));
    await userEvent.click(main().getByRole("combobox", { name: "Când se trimite" }));
    await userEvent.click(screen.getByRole("option", { name: "La 2 zile după emitere" }));
    await userEvent.click(main().getByRole("button", { name: "Salvează" }));
    expect(main().getByRole("combobox", { name: "Când se trimite" })).toBeDisabled(); // a newer choice can't be lost
    await act(async () => answer.resolve(json({ ok: false, message: "Pornește trimiterea automată în SPV ca să alegi ziua." })));
    await waitFor(() => expect(main().getByRole("combobox", { name: "Când se trimite" })).toBeEnabled());
    expect(main().getByRole("combobox", { name: "Când se trimite" })).toHaveTextContent("La 2 zile după emitere"); // a refusal keeps it open
    expect(api.posts().map((c) => c.body)).toEqual([{ co: "NORD", days: "2" }]);
  });

  it("a shop's automatic issue is a Facturare rule, stopped and started here, held in Sandbox", async () => {
    at("?co=NORD&tab=automatizari");
    const s = fixture();
    s.live = false;
    s.rules = [...s.rules, { ...s.rules[0], id: "NORD:shop_auto_issue:shop-0000abcd", kind: "shop_auto_issue", label: "Magazin Exemplu",
      reason: "shadow_mode", settings: null, can_edit: false, waiting_manual: 0 }];
    const api = renderApp({ state: () => json(s), onPost: () => json({ ok: true, message: "Regula a fost oprită.", state: s }) });
    await page();
    const row = main().getByText("Emitere automată, Magazin Exemplu", { selector: "b" }).closest("div") as HTMLElement;
    expect(row).toHaveTextContent("Comenzile din magazin devin facturi emise.Activă doar în modul Live. Setări › ModOprește");
    await userEvent.click(within(row).getByRole("button", { name: "Oprește regula Emitere automată, Magazin Exemplu" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toMatchObject({ url: "/web/api/rule-pause", body: { co: "NORD", id: "NORD:shop_auto_issue:shop-0000abcd" } });
  });

  it("Pornește posts rule-resume once, even when pressed twice, and a refusal keeps the rule as it was", async () => {
    at("?co=NORD&tab=automatizari");
    const answer = deferred<Response>();
    const api = renderApp({ onPost: () => answer.promise });
    await page();
    const btn = main().getByRole("button", { name: "Pornește regula Telecom Rapid SRL" });
    await userEvent.click(btn);
    expect(btn).toBeDisabled();
    await userEvent.click(btn);
    await act(async () => answer.resolve(json({ ok: false, message: "IBAN-ul furnizorului s-a schimbat. După verificarea lui, regula se poate porni din nou." })));
    await waitFor(() => expect(toast()).toBe("IBAN-ul furnizorului s-a schimbat. După verificarea lui, regula se poate porni din nou."));
    expect(api.posts().map((c) => [c.url, c.body])).toEqual([
      ["/web/api/rule-resume", { co: "NORD", id: "NORD:supplier_autopay:RO1002" }]]);
    expect(main().getByRole("button", { name: "Pornește regula Telecom Rapid SRL" })).toBeEnabled();
  });

  it("words each state the engine sends: manual uploads waiting, a changed account that blocks Pornește, links that stopped", async () => {
    at("?co=NORD&tab=automatizari");
    const s = fixture();
    s.rules = [
      { ...s.rules[0], enabled: false, reason: "paused", waiting_manual: 3, can_pause: false, can_resume: true, settings: null },
      ...["supplier_inactive", "invoice_refused"].map((reason, n): Rule => ({
        ...s.rules[3], id: `NORD:supplier_autopay:RO${n}`, label: `Furnizor ${n}`, enabled: n !== 0, reason: reason as Rule["reason"],
        waiting_queue: 0, can_pause: n !== 0, can_resume: false })),
      ...(["revoked", "expired", "exhausted"] as const).map((reason, n): Rule => ({
        ...s.rules[2], id: `NORD:receipt_upload:${n}`, label: `Link ${n}`, enabled: false, reason, last: null })),
      { ...s.rules[2], id: "NORD:receipt_upload:3", label: "Link 3", last: { action: "upload", date: "2026-10-20", count: 1 } },
    ];
    renderApp({ state: () => json(s) });
    await page();
    const row = (name: string) => main().getByText(name, { selector: "b" }).closest("a, div");
    expect(row("Trimitere automată în SPV")).toHaveTextContent("Oprită. 3 facturi așteaptă trimiterea manuală.Pornește");
    expect(row("Furnizor 0")).toHaveTextContent("Furnizorul figurează inactiv la ANAF. Verifică situația cu contabilul înainte de plată.");
    expect(row("Furnizor 1")).toHaveTextContent("O factură a furnizorului nu intră în fișierul de plăți. Motivul e pe factură.");
    expect([0, 1, 2].map((n) => row(`Link ${n}`)?.textContent)).toEqual([
      "Link 0Bondrop, Bonurile trimise de pe telefon ajung direct la firmă.OprităLinkul a fost revocat.",
      "Link 1Bondrop, Bonurile trimise de pe telefon ajung direct la firmă.OprităLinkul a expirat.",
      "Link 2Bondrop, Bonurile trimise de pe telefon ajung direct la firmă.OprităLimita de fișiere a fost atinsă.",
    ]);
    expect(row("Link 3")).toHaveTextContent("Pornită. Un fișier primit, ultimul pe 20 oct.");
  });

  it("shows Bondrop's way to Setări before the first link, and leaves out a group with no rules", async () => {
    at("?co=NORD&tab=automatizari");
    const s = fixture();
    s.rules = s.rules.filter((r) => r.kind === "spv_auto_upload" || r.kind === "accountant_pack");
    s.rules[0] = { ...s.rules[0], enabled: false, reason: "paused", waiting_manual: 20, can_pause: false, can_resume: true, settings: null };
    renderApp({ state: () => json(s) });
    await page();
    expect(groups().map(([h]) => h)).toEqual(["Facturare", "Documente"]);
    expect(main().getByText("Trimitere automată în SPV").closest("a, div"))
      .toHaveTextContent("Oprită. 20 de facturi așteaptă trimiterea manuală.");
    const bondrop = main().getByRole("link", { name: /^Bondrop/ });
    expect(bondrop).toHaveAttribute("href", "?co=NORD&tab=setari&sec=acces");
    expect(bondrop).toHaveTextContent("BondropBonurile trimise de pe telefon ajung direct la firmă.Setări");
  });

  it("in shadow mode says what waits for the install to go live, and still lets an administrator stop a rule", async () => {
    at("?co=NORD&tab=automatizari");
    const s = fixture();
    s.live = false;
    s.rules = s.rules.map((r) => (r.kind === "spv_auto_upload" || r.kind === "accountant_pack" ? { ...r, reason: "shadow_mode" } : r));
    const api = renderApp({ state: () => json(s), onPost: () => json({ ok: true, message: "Regula a fost oprită.", state: s }) });
    await page();
    expect(main().getByText("Trimitere automată în SPV").closest("a, div"))
      .toHaveTextContent("Trimitere automată în SPVFiecare factură emisă se trimite în SPV.Se trimite la 4 zile după emitere.Activă doar în modul Live. Setări › ModModificăOprește");
    expect(main().getByText("Dosarul lunar pentru contabil").closest("a, div")).toHaveTextContent("Activă doar în modul Live.");
    await userEvent.click(main().getByRole("button", { name: "Oprește regula Trimitere automată în SPV" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "NORD:spv_auto_upload" });
  });

  it("asks for ANAF before the SPV rule can send anything, and never calls it Pornită meanwhile", async () => {
    at("?co=NORD&tab=automatizari");
    const s = fixture();
    s.rules = s.rules.map((r) => (r.kind === "spv_auto_upload" ? { ...r, reason: "anaf_not_connected" } : r));
    renderApp({ state: () => json(s) });
    await page();
    const rule = main().getByText("Trimitere automată în SPV").closest("div") as HTMLElement;
    expect(rule).toHaveTextContent("Fiecare factură emisă se trimite în SPV.Se trimite la 4 zile după emitere.Lipsește conexiunea la ANAF. Conectează ANAF");
    expect(rule).not.toHaveTextContent("Pornită");
    expect(within(rule).getByRole("link", { name: "Conectează ANAF" })).toHaveAttribute("href", "?co=NORD&tab=setari&sec=integrari#k-anaf");
  });

  it("shows no rule switch and no note about it to someone who isn't an administrator", async () => {
    at("?co=NORD&tab=automatizari");
    const s = fixture();
    s.can_manage_companies = false;
    s.rules = s.rules.map((r) => ({ ...r, can_pause: false, can_resume: false, can_edit: false }));
    renderApp({ state: () => json(s) });
    await page();
    // Recurring invoices are drafts' configuration, which anyone with the firm can change (the engine's policy).
    expect(main().getAllByRole("button").map((b) => b.textContent)).toEqual(["Pornește", "Șterge", "Oprește", "Șterge"]);
    expect(main().queryAllByRole("button", { name: /regula/ })).toEqual([]);
    expect(main().queryByText(/Doar administratorii/)).toBeNull();
  });
});

describe("Automatizări under Toate firmele", () => {
  it("groups the rules by firm and acts for the rule's own firm", async () => {
    at("?co=ALL&tab=automatizari");
    const api = renderApp({ onPost: () => json({ ok: true, message: "Regula a fost pornită.", state: fixture("STUDIO9") }) });
    await page();
    expect(main().getAllByRole("heading").filter(h => h.tagName === "H2" || h.tagName === "H3").map((h) => `${h.tagName} ${h.textContent}`)).toEqual([
      "H2 NORD", "H3 Facturare", "H3 Încasări", "H3 Plăți", "H3 Documente", "H2 STUDIO9", "H3 Facturare", "H3 Încasări", "H3 Plăți",
      "H3 Documente"]);
    const studio = within(screen.getByRole("region", { name: "STUDIO9" }));
    expect(studio.getByText("Trimitere automată în SPV").closest("a, div")).toHaveTextContent("Oprită");
    expect(studio.getByText("Laborator Analize SRL").closest("a, div"))
      .toHaveTextContent("Furnizorul figurează inactiv la ANAF. Verifică situația cu contabilul înainte de plată.");
    expect(studio.getByRole("link", { name: /^Bondrop/ })).toHaveAttribute("href", "?co=STUDIO9&tab=setari&sec=acces");
    await userEvent.click(studio.getByRole("button", { name: "Pornește regula Trimitere automată în SPV" }));
    await waitFor(() => expect(toast()).toBe("Regula a fost pornită."));
    expect(api.posts().map((c) => [c.url, c.body])).toEqual([["/web/api/rule-resume", { co: "STUDIO9", id: "STUDIO9:spv_auto_upload" }]]);
    expect(window.location.search).toBe("?co=ALL&tab=automatizari");
    await waitFor(() => expect(api.gets("ALL").length).toBeGreaterThan(1)); // Toate firmele is read again
  });
});

describe("Automatizări, Reamintiri de plată", () => {
  it("saves the overdue-day setting locally", async () => {
    at("?co=NORD&tab=automatizari");
    const api = renderApp({ onPost: () => json({ ok: true, message: "", state: fixture() }) });
    await page();
    await userEvent.click(main().getByRole("button", { name: "Modifică regula Reamintiri de plată" }));
    const field = main().getByLabelText("Prima reamintire, la câte zile după scadență");
    await userEvent.clear(field);
    await userEvent.type(field, "10");
    await userEvent.click(main().getByRole("button", { name: "Salvează" }));
    expect(api.posts()[0].body).toEqual({ co: "NORD", after_days: "10" });
  });
});
