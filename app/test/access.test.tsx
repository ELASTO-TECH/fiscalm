// @vitest-environment-options {"url":"https://localhost:3000"}
// Setări › Linkuri și chei API against a fake engine: receipt links for the company and API keys for the install, each secret shown
// once after it is made, revoking with a confirm, and the exact POST bodies.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "@/components/App";
import { Authentication } from "@/components/Authentication";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiToken, UploadLink, UserRole } from "@/lib/types";
import { at, deferred, fixture, fixtureIdentity, json, type Call, renderApp, server, answer, choose } from "./helpers";

// "Active" and "expired" are judged against the clock: pinned to the fixtures' day.
beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-27T10:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const link = (patch: Partial<UploadLink>): UploadLink => ({ id: "l1", company: "NORD", label: "Telefonul de serviciu",
  created_at: "2026-10-01T08:00:00Z", expires_at: "2026-12-01T08:00:00Z", revoked_at: null, uploads: 0, last_used_at: null,
  max_files: 100, ...patch });
const token = (patch: Partial<ApiToken>): ApiToken => ({ id: "t1", name: "Asistentul AI", scope: "read",
  created_at: "2026-10-10T08:00:00Z", last_used_at: null, revoked_at: null, created_by: null, user_id: "u_example", owner: { id: "u_example", display_name: "Cont de test" }, ...patch });

const ok = (done: object | null = null) => json({ ok: true, message: "", state: fixture(), done });

async function page(options: Parameters<typeof renderApp>[0] = {}) {
  const api = renderApp(options);
  await screen.findByRole("heading", { name: "Chei API" });
  return { api, p: within(document.body) };
}

describe("Linkuri și chei API", () => {
  it("shows a firm's links as shapes until they are read, never as empty or as another firm's", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const studio = deferred<Response>();
    renderApp({ uploadLinks: (url) => (url.includes("STUDIO9") ? studio.promise : json([link({})])), state: (url) => json(fixture(url.includes("STUDIO9") ? "STUDIO9" : "NORD")) });
    expect(await screen.findByText("Telefonul de serviciu")).toBeVisible();
    await act(async () => { window.history.pushState(null, "", "/web/?co=STUDIO9&tab=setari&sec=acces"); window.dispatchEvent(new PopStateEvent("popstate")); });
    await screen.findByRole("heading", { name: "Linkuri și chei API" });
    expect(screen.queryByText("Telefonul de serviciu")).toBeNull();
    expect(within(screen.getByRole("region", { name: "Bondrop" })).getByText("Se încarcă…")).toBeInTheDocument();
    await act(async () => studio.resolve(json([])));
    expect(within(screen.getByRole("region", { name: "Bondrop" })).getByText("Niciun element")).toBeVisible();
  });

  it("is reached from Setări › Configurare", async () => {
    at("?co=NORD&tab=setari");
    renderApp();
    const row = await screen.findByRole("link", { name: /^Linkuri și chei API/ });
    expect(row).toHaveAttribute("href", "?co=NORD&tab=setari&sec=acces");
  });

  it("names Bondrop and says what it does; with no link yet, says how to start", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { p } = await page({ uploadLinks: () => json([]) });
    const links = within(await p.findByRole("region", { name: "Bondrop" }));
    expect(links.getByText("Cine are linkul poate trimite documente pentru NORD, fără să vadă contabilitatea.")).toBeInTheDocument();
    expect(await links.findByText("Niciun element")).toBeInTheDocument();
    await userEvent.click(links.getByRole("button", { name: "Link nou" }));
    expect(links.getByLabelText("Nume")).toHaveAccessibleDescription("Numele apare pe pagina Bondrop.");
  });

  it("lists the company's links, active first, and the install's keys", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { api, p } = await page({
      uploadLinks: () => json([
        link({ id: "old", label: "Deplasare", revoked_at: "2026-09-20T10:00:00Z", created_at: "2026-09-01T08:00:00Z", uploads: 4 }),
        link({ id: "gone", label: "Expirat", expires_at: "2026-10-01T08:00:00Z", uploads: 1 }),
        link({ uploads: 12 }),
      ]),
      tokens: () => json([token({ last_used_at: "2026-10-26T22:30:00Z" }), token({ id: "t2", name: "Export", scope: "write", revoked_at: "2026-09-01T08:00:00Z" })]),
    });
    const links = within(await p.findByRole("region", { name: "Bondrop" }));
    await links.findByText("Telefonul de serviciu"); // the lists arrive after the headings
    await p.findByText("Revocată pe 1 sep");
    const rows = links.getAllByText(/Telefonul|Deplasare|Expirat$/).map((x) => x.textContent);
    expect(rows).toEqual(["Telefonul de serviciu", "Expirat", "Deplasare"]);
    expect(links.getByText("Activ până pe 1 dec, mai primește 88 de fișiere")).toBeInTheDocument();
    expect(links.getByText("Revocat pe 20 sep, 4 fișiere primite")).toBeInTheDocument();
    expect(links.getByText("Expirat pe 1 oct, un fișier primit")).toBeInTheDocument();
    expect(links.getAllByRole("button", { name: /Revocă linkul/ })).toHaveLength(1); // only the active one
    const keys = within(screen.getByRole("region", { name: "Chei API" }));
    // 22:30 UTC on 26 Oct is 27 Oct in Romania
    expect(keys.getByText("Doar citire, folosită ultima dată pe 27 oct")).toBeInTheDocument();
    expect(keys.getByText("Revocată pe 1 sep")).toBeInTheDocument();
    expect(api.calls.some((c) => c.url.endsWith("api/upload-links?co=NORD"))).toBe(true);
  });

  it.each([false, true])("makes a link (all firms: %s), shows it once and forgets it after Gata", async (allFirms) => {
    at("?co=NORD&tab=setari&sec=acces");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { api, p } = await page({ onPost: () => ok({ id: "l9", path: "/u/l9.s3cr3t" }) });
    const links = within(await p.findByRole("region", { name: "Bondrop" }));
    await userEvent.click(links.getByRole("button", { name: "Link nou" }));
    const create = links.getByRole("button", { name: "Creează linkul" });
    expect(create).toBeDisabled(); // a name first
    await userEvent.type(links.getByLabelText("Nume"), " Telefonul lui Andrei ");
    if (allFirms) await choose(links.getByLabelText("Firmă"), "all");
    await choose(links.getByLabelText("Valabil"), "90");
    await userEvent.click(create);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/upload-link-create", method: "POST",
      body: { co: "NORD", label: "Telefonul lui Andrei", expires_in_days: 90, ...(allFirms ? { all_firms: true } : {}) } });
    const url = `${window.location.origin}/u/l9.s3cr3t`;
    expect(await links.findByText(url)).toBeInTheDocument();
    expect(links.getByText("Linkul se afișează doar acum.")).toBeInTheDocument();
    expect(links.getByRole("button", { name: "Copiază linkul" })).toHaveFocus(); // ready to copy
    await userEvent.click(links.getByRole("button", { name: "Copiază linkul" }));
    expect(writeText).toHaveBeenCalledWith(url);
    expect(links.getByRole("button", { name: "Copiat" })).toBeInTheDocument();
    await userEvent.click(links.getByRole("button", { name: "Închide" }));
    expect(links.queryByText(url)).toBeNull();
    await waitFor(() => expect(links.getByRole("button", { name: "Link nou" })).toHaveFocus());
  });

  it("a link that took all the files it may is shown as used up, with nothing to revoke", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { p } = await page({ uploadLinks: () => json([link({ uploads: 100, max_files: 100 })]) });
    expect(await p.findByText("Limita de fișiere atinsă, 100 de fișiere primite")).toBeInTheDocument();
    expect(p.queryByRole("button", { name: /Revocă linkul/ })).toBeNull();
  });

  it("Enter in the name makes the link", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { api, p } = await page({ onPost: () => ok({ id: "l9", path: "/u/l9.x" }) });
    await userEvent.click(p.getByRole("button", { name: "Link nou" }));
    await userEvent.type(p.getByLabelText("Nume"), "Telefon{Enter}");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
  });

  it("where clipboard access is unavailable, the secret is selected and it says so", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    const { p } = await page({ onPost: () => ok({ id: "t9", token: "fiscalm_synthetic_test_token_0123456789", scope: "read" }) });
    await userEvent.click(p.getByRole("button", { name: "Cheie nouă" }));
    await userEvent.type(p.getByLabelText("Nume"), "AI");
    await userEvent.click(p.getByRole("button", { name: "Creează cheia" }));
    await userEvent.click(await p.findByRole("button", { name: "Copiază cheia" }));
    expect(p.getByRole("alert")).toHaveTextContent("Cheia nu s-a putut copia automat. Este selectată. Copiaz-o manual.");
    expect(window.getSelection()?.toString()).toBe("fiscalm_synthetic_test_token_0123456789");
  });

  it("revokes a key after asking, and focus returns to the section", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { api, p } = await page({ tokens: () => json([token({})]), onPost: () => ok({ revoked: true }) });
    await userEvent.click(await p.findByRole("button", { name: "Revocă cheia Asistentul AI" }));
    expect(await answer()).toBe("Revoci cheia „Asistentul AI”? Programele care o folosesc pierd accesul.");
    await waitFor(() => expect(api.posts()[0]).toEqual({ url: "/web/api/token-revoke", method: "POST", body: { id: "t1" } }));
    await waitFor(() => expect(p.getByRole("button", { name: "Cheie nouă" })).toHaveFocus());
  });

  it("says who made each key", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { p } = await page({ tokens: () => json([token({ created_by: "contabil" })]) });
    expect(await p.findByText("Proprietar: Cont de test.")).toBeInTheDocument();
  });


  it("shows the install's public address for a new link when the engine gives one", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { p } = await page({ onPost: () => ok({ id: "l9", path: "/u/l9.s", url: "https://bonuri.exemplu.ro/u/l9.s" }) });
    await userEvent.click(p.getByRole("button", { name: "Link nou" }));
    await userEvent.type(p.getByLabelText("Nume"), "Telefon{Enter}");
    expect(await p.findByText("https://bonuri.exemplu.ro/u/l9.s")).toBeInTheDocument();
  });

  it("revokes a link after asking", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { api, p } = await page({ uploadLinks: () => json([link({})]),
      onPost: () => json({ ok: true, message: "Linkul a fost revocat.", state: fixture(), done: { id: "l1", revoked: true } }) });
    const revoke = await p.findByRole("button", { name: "Revocă linkul Telefonul de serviciu" });
    await userEvent.click(revoke);
    await answer(false);
    expect(api.posts()).toHaveLength(0);
    await userEvent.click(revoke);
    expect(await answer()).toBe("Revoci linkul „Telefonul de serviciu”? Nu se mai pot trimite bonuri prin el.");
    await waitFor(() => expect(api.posts()[0]).toEqual({ url: "/web/api/upload-link-revoke", method: "POST",
      body: { co: "NORD", id: "l1" } }));
    expect(await screen.findByText("Linkul a fost revocat.")).toBeInTheDocument();
  });

  it("makes an API key with the rights chosen, shown once", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { api, p } = await page({ onPost: () => ok({ id: "t9", token: "fiscalm_synthetic_test_token_0123456789", scope: "write" }) });
    const keys = within(await p.findByRole("region", { name: "Chei API" }));
    await userEvent.click(keys.getByRole("button", { name: "Cheie nouă" }));
    await userEvent.type(keys.getByLabelText("Nume"), "Asistentul AI");
    expect(keys.getByLabelText("Doar citire")).toBeChecked(); // the safer default
    await userEvent.click(keys.getByLabelText("Citire și acțiuni"));
    await userEvent.click(keys.getByRole("button", { name: "Creează cheia" }));
    await waitFor(() => expect(api.posts()[0]).toEqual({ url: "/web/api/token-create", method: "POST",
      body: { name: "Asistentul AI", scope: "write" } }));
    expect(await keys.findByText("fiscalm_synthetic_test_token_0123456789")).toBeInTheDocument();
    expect(keys.getByText("Cheia se afișează doar acum.")).toBeInTheDocument();
  });

  it("a refused key shows the engine's message and keeps the form", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const { p } = await page({ onPost: () => json({ ok: false, message: "Numele cheii are cel mult 100 de caractere." }) });
    const keys = within(await p.findByRole("region", { name: "Chei API" }));
    await userEvent.click(keys.getByRole("button", { name: "Cheie nouă" }));
    await userEvent.type(keys.getByLabelText("Nume"), "x");
    await userEvent.click(keys.getByRole("button", { name: "Creează cheia" }));
    expect(await screen.findByText("Numele cheii are cel mult 100 de caractere.")).toBeInTheDocument();
    expect(keys.getByLabelText("Nume")).toHaveValue("x");
    expect(keys.queryByRole("status")).toBeNull();
  });

  it("works before the install goes live (configuration only)", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    const s = fixture();
    s.live = false;
    const { api, p } = await page({ state: () => json(s), onPost: (c: Call) => ok(c.url.endsWith("token-create") ? { id: "t9", token: "fiscalm_x" } : null) });
    const keys = within(await p.findByRole("region", { name: "Chei API" }));
    await userEvent.click(keys.getByRole("button", { name: "Cheie nouă" }));
    await userEvent.type(keys.getByLabelText("Nume"), "Test");
    await userEvent.click(keys.getByRole("button", { name: "Creează cheia" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
  });

  it("says so when the lists can't be read, and retries", async () => {
    at("?co=NORD&tab=setari&sec=acces");
    let fail = true;
    renderApp({ tokens: () => (fail ? json({ ok: false }, 500) : json([token({})])) });
    expect(await screen.findByText("Cheile API nu au putut fi actualizate.")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
    expect(await screen.findByText("Asistentul AI")).toBeInTheDocument();
  });
});


it.each(["admin", "member", "readonly"] as UserRole[])("shows user-owned keys and role-limited scopes for %s", async role => {
  at("?co=NORD&tab=setari&sec=acces"); const user = userEvent.setup();
  const api = renderApp({ principal: fixtureIdentity(role), tokens: () => json([token({}), token({ id: "other", name: "Alt proprietar", user_id: "u_other", owner: { id: "u_other", display_name: "Alt cont" } })]) });
  expect(await screen.findByText("Asistentul AI")).toBeVisible(); expect(!!screen.queryByText("Alt proprietar")).toBe(role === "admin");
  expect(api.calls.some(c => c.url.includes("api/upload-links?"))).toBe(role === "admin");
  expect(!!screen.queryByRole("region", { name: "Bondrop" })).toBe(role === "admin");
  await user.click(screen.getByRole("button", { name: "Cheie nouă" })); expect(!!screen.queryByLabelText("Citire și acțiuni")).toBe(role !== "readonly");
});

it("creates a read-only key before any company and retains its one-time secret after a metadata-read failure", async () => {
  at("?tab=setari&sec=cont"); const user = userEvent.setup(); let created = false;
  const api = server({ identity: () => json(fixtureIdentity("readonly")), all: () => json({ setup: "needs_company", companies: [], live: false, can_manage_companies: false }),
    tokens: () => created ? json({}, 500) : json([]), onPost: () => { created = true; return ok({ id: "new", token: "fiscalm_fixture_once" }); } });
  render(<Authentication><App /></Authentication>); await user.click(await screen.findByRole("button", { name: "Cheie nouă" }));
  await user.type(screen.getByLabelText("Nume"), "Export"); await user.click(screen.getByRole("button", { name: "Creează cheia" }));
  expect(await screen.findByText("fiscalm_fixture_once")).toBeVisible(); expect(await screen.findByRole("alert")).toHaveTextContent("Cheile API nu au putut fi actualizate.");
  expect(api.posts()).toEqual([{ url: "/web/api/token-create", method: "POST", body: { name: "Export", scope: "read" } }]);
  expect(api.calls.some(c => c.url.includes("api/upload-links?"))).toBe(false);
  await user.click(within(screen.getByText("fiscalm_fixture_once").closest(".secret") as HTMLElement).getByRole("button", { name: "Închide" })); expect(screen.queryByText("fiscalm_fixture_once")).not.toBeInTheDocument();
});

it("keeps an uncertain token mutation warning while refreshing without replay", async () => {
  at("?co=NORD&tab=setari&sec=acces"); const user = userEvent.setup(); let changed = false;
  const api = renderApp({ tokens: () => json(changed ? [token({ name: "Cheie înregistrată" })] : []), onPost: () => { changed = true; return new Response('{"ok":', { headers: { "Content-Type": "application/json" } }); } });
  await user.click(await screen.findByRole("button", { name: "Cheie nouă" })); await user.type(screen.getByLabelText("Nume"), "Cheie înregistrată");
  await user.click(screen.getByRole("button", { name: "Creează cheia" })); expect(await screen.findByRole("alert")).toHaveTextContent("Nu a sosit confirmarea.");
  expect(await screen.findByText("Cheie înregistrată", { selector: "b" })).toBeVisible(); expect(api.posts()).toHaveLength(1);
});

it("publishes one key for synchronous double-submit and prevents cancellation while pending", async () => {
  at("?co=NORD&tab=setari&sec=acces"); const user = userEvent.setup(), pending = deferred<Response>(); const api = renderApp({ onPost: () => pending.promise });
  await user.click(await screen.findByRole("button", { name: "Cheie nouă" })); await user.type(screen.getByLabelText("Nume"), "Conexiune");
  const button = screen.getByRole("button", { name: "Creează cheia" }); act(() => { button.click(); button.click(); }); expect(api.posts()).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Renunță" })).toBeDisabled(); await act(async () => pending.resolve(ok({ id: "new", token: "fiscalm_fixture_once" })));
  expect(await screen.findByText("fiscalm_fixture_once")).toBeVisible();
});
