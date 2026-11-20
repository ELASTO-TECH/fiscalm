// Setări › Integrări against a fake engine: the list, the forms, the exact POST bodies, keys never prefilled, and
// configuration allowed before the install goes live.
import { screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render, answer, choose, choicesOf } from "./helpers";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import { at, deferred, fixture, type Call, fixtureIdentity, fixtureIntegrations, json, server, renderApp } from "./helpers";

/** The fixture's integration ids, by kind (the engine numbers them). */
const ID = Object.fromEntries(fixtureIntegrations().items.map((i) => [i.kind, i.id])) as Record<"ai" | "anaf" | "mail" | "storage", string>;

beforeEach(() => localStorage.clear());

const page = () => screen.findByRole("region", { name: "Serviciu AI" });
const sheet = () => screen.getByRole("dialog");

describe("Integrări", () => {
  it("is reached from Setări, with the count of what is configured", async () => {
    at("?co=NORD&tab=setari");
    const s = fixture();
    s.system.anaf.days_left = 90; // nothing to warn about: the row counts what is configured
    renderApp({ state: () => json(s) });
    const row = await screen.findByRole("link", { name: /Integrări/ });
    await waitFor(() => expect(row).toHaveTextContent("4 integrări"));
    expect(row).toHaveAttribute("href", "?co=NORD&tab=setari&sec=integrari");
    await userEvent.click(row);
    expect(window.location.search).toBe("?co=NORD&tab=setari&sec=integrari");
    await page();
    expect(screen.getByRole("link", { name: "Înapoi la Setări" })).toHaveAttribute("href", "?co=NORD&tab=setari");
  });

  it("lists every kind with its instances and says what each points at", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    renderApp();
    await page();
    const ai = screen.getByRole("region", { name: "Serviciu AI" });
    const claude = within(ai).getByRole("link", { name: /Claude/ });
    expect(claude).toHaveTextContent("Anthropic (cheie API), claude-opus-5-5");
    expect(claude).not.toHaveTextContent("Implicită"); // one of its kind: nothing to choose between
    expect(screen.queryByRole("region", { name: "Webhook" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Adaugă: Webhook" })).toBeNull();
    expect(within(screen.getByRole("region", { name: "Arhivă Google Drive" })).getByRole("link", { name: /Arhiva documentelor/ }))
      .toHaveTextContent(/Google Drive, firma NORD$/);
    expect(screen.queryByRole("region", { name: "Bancă" })).toBeNull(); // a firm's bank is set in Firme › Bănci
    expect(within(screen.getByRole("region", { name: "ANAF e-Factura" })).getByRole("link", { name: /Expiră curând/ }))
      .toHaveTextContent(/Expiră curând, până pe 16 nov\. ANAF Producție\. Toate firmele$/);
  });

  it("names the default when a kind has several, and the ones switched off", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    const d = fixtureIntegrations();
    d.items.push({ id: "ai-00000009", kind: "ai", name: "Local", enabled: false, default: false,
      config: { provider: "openai", model: "qwen3", base_url: "http://127.0.0.1:11434/v1" }, secrets: { api_key: false },
      updated_at: "2026-10-27T09:30:00" });
    renderApp({ integrations: () => json(d) });
    await page();
    const ai = screen.getByRole("region", { name: "Serviciu AI" });
    expect(within(ai).getByRole("link", { name: /Claude/ })).toHaveTextContent("Implicită. Anthropic (cheie API), claude-opus-5-5");
    expect(within(ai).getByRole("link", { name: /Local/ })).toHaveTextContent("Oprită. Compatibil OpenAI sau local, qwen3");
  });

  it("edits an instance: the key is never shown, sent only when typed, and the body is exact", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    const api = renderApp({ onPost: () => json({ ok: true, message: "Modificările au fost salvate.", state: fixture() }) });
    await page();
    await userEvent.click(screen.getByRole("link", { name: /Claude/ }));
    expect(window.location.search).toBe(`?co=NORD&tab=setari&sec=integrari&int=${ID.ai}`);
    expect(within(sheet()).getByText("Serviciu AI")).toBeInTheDocument();
    const key = within(sheet()).getByLabelText("Cheie API") as HTMLInputElement; // Anthropic: required (the engine's rule)
    expect(key.type).toBe("password");
    expect(key.value).toBe("");
    expect(key.placeholder).toBe("Salvată. Lasă gol pentru a o păstra.");
    const model = within(sheet()).getByLabelText("Model (opțional)");
    await userEvent.clear(model);
    await userEvent.type(model, "claude-sonnet-5-5");
    await userEvent.click(within(sheet()).getByRole("button", { name: "Salvează" }));
    expect(api.posts()[0].url).toContain("api/integration-update");
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: ID.ai, name: "Claude", f_provider: "anthropic",
      f_model: "claude-sonnet-5-5", f_base_url: "" });
    expect(await screen.findByText("Modificările au fost salvate.")).toBeInTheDocument();
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=setari&sec=integrari"));
    await userEvent.click(screen.getByRole("link", { name: /Claude/ }));
    await userEvent.type(within(sheet()).getByLabelText("Cheie API"), "sk-new-key");
    await userEvent.click(within(sheet()).getByRole("button", { name: "Salvează" }));
    expect(api.posts()[1].body?.f_api_key).toBe("sk-new-key");
  });

  it("adds one: the button waits for the required fields, then posts the kind and the fields", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    const api = renderApp({ onPost: () => json({ ok: true, message: "Integrarea a fost adăugată.", state: fixture() }) });
    await page();
    await userEvent.click(screen.getByRole("link", { name: "Adaugă: Arhivă Google Drive" }));
    expect(within(sheet()).queryByText("Integrare nouă")).not.toBeInTheDocument();
    const add = within(sheet()).getByRole("button", { name: "Adaugă integrarea" });
    expect(add).toBeDisabled();
    expect(within(sheet()).queryByLabelText("Firmă")).toBeNull(); // the firm on screen, sent without asking
    await userEvent.type(within(sheet()).getByLabelText("Remote rclone"), "arhiva:");
    await userEvent.type(within(sheet()).getByLabelText("ID dosar Google Drive"), "dosar1");
    expect(add).toBeEnabled();
    await userEvent.click(add);
    expect(api.posts()[0].body).toEqual({ co: "NORD", kind: "storage", name: "Arhivă Google Drive", f_company: "NORD",
      f_remote: "arhiva:", f_root: "dosar1", f_layout: "month" });
  });

  it("hides the key and address for the CLI provider", async () => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:ai");
    renderApp();
    await page();
    expect(within(sheet()).getByLabelText("Cheie API (opțional)")).toBeInTheDocument();
    await choose(within(sheet()).getByLabelText("Serviciu"), "claude-cli");
    expect(within(sheet()).queryByLabelText("Cheie API (opțional)")).toBeNull();
    expect(within(sheet()).queryByLabelText("Adresă API (opțional)")).toBeNull();
  });

  it("asks for what the chosen provider needs, and sends nothing the form hides", async () => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:ai");
    const api = renderApp({ cliStatus: () => json({ provider: "", state: "idle", url: "", code: "", needs_code: false, readiness: "ready" }) });
    await page();
    const f = within(sheet());
    await choose(f.getByLabelText("Serviciu"), "anthropic");
    expect(f.getByLabelText("Cheie API")).toBeInTheDocument(); // required for a hosted provider
    expect(f.getByLabelText("Model (opțional)")).toBeInTheDocument();
    await choose(f.getByLabelText("Serviciu"), "openai");
    expect(f.getByLabelText("Model")).toBeInTheDocument(); // a compatible server must name its model
    expect(f.getByLabelText("Cheie API")).toBeInTheDocument(); // ...and needs a key at the default address
    await userEvent.type(f.getByLabelText("Adresă API (opțional)"), "http://127.0.0.1:11434/v1");
    expect(f.getByLabelText("Cheie API (opțional)")).toBeInTheDocument(); // a local server may need none
    await userEvent.type(f.getByLabelText("Model"), "qwen3");
    await userEvent.type(f.getByLabelText("Cheie API (opțional)"), "typed-then-hidden");
    await choose(f.getByLabelText("Serviciu"), "claude-cli");
    await userEvent.click(f.getByRole("button", { name: "Adaugă integrarea" }));
    expect(api.posts()[0].body).toEqual({ co: "NORD", kind: "ai", name: "Serviciu AI", f_provider: "claude-cli", f_model: "qwen3" });
  });

  it("reopens only after its own fresh read instead of reusing another reader's data", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    let hold: (() => void) | null = null;
    let n = 0;
    renderApp({
      integrations: () => (++n <= 2 ? json(fixtureIntegrations())
        : new Promise((ok) => { hold = () => ok(json(fixtureIntegrations())); })),
      onPost: () => json({ ok: true, message: "Modificările au fost salvate.", state: fixture() }),
    });
    await page();
    await userEvent.click(screen.getByRole("link", { name: /Arhiva documentelor/ }));
    await userEvent.click(within(sheet()).getByRole("button", { name: "Salvează" }));
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=setari&sec=integrari"));
    await userEvent.click(screen.getByRole("link", { name: /Arhiva documentelor/ })); // reopened before the new read
    expect(within(sheet()).queryByRole("button", { name: "Salvează" })).toBeNull();
    expect(within(sheet()).getByRole("status")).toBeVisible();
    await waitFor(() => expect(hold).not.toBeNull());
    hold!();
    await waitFor(() => expect(within(sheet()).getByRole("button", { name: "Salvează" })).toBeEnabled());
  });

  it("a sheet whose own read fails says so and retries", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    let n = 0;
    renderApp({ integrations: () => (++n === 2 ? json({ ok: false }, 500) : json(fixtureIntegrations())) });
    await page();
    await userEvent.click(screen.getByRole("link", { name: /Claude/ })); // the sheet's own read is the 2nd: it fails
    const alert = await within(sheet()).findByRole("alert");
    expect(alert).toHaveTextContent("Integrările nu au putut fi încărcate.");
    expect(within(sheet()).queryByRole("button", { name: "Salvează" })).toBeNull();
    await userEvent.click(within(alert).getByRole("button", { name: "Reîncearcă" }));
    await waitFor(() => expect(within(sheet()).getByRole("button", { name: "Salvează" })).toBeEnabled());
  });

  it("fresh data from the server replaces what the form showed, even within the same second", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    let model = "claude-opus-5-5";
    renderApp({ integrations: () => {
      const d = fixtureIntegrations();
      d.items[0].config.model = model; // same updated_at: only the content differs
      return json(d);
    } });
    await page();
    await userEvent.click(screen.getByRole("link", { name: /Claude/ }));
    await waitFor(() => expect(within(sheet()).getByLabelText("Model (opțional)")).toHaveValue("claude-opus-5-5"));
    model = "claude-sonnet-5-5";
    await userEvent.click(within(sheet()).getByRole("button", { name: "Închide" }));
    await userEvent.click(screen.getByRole("link", { name: /Claude/ }));
    await waitFor(() => expect(within(sheet()).getByLabelText("Model (opțional)")).toHaveValue("claude-sonnet-5-5"));
  });

  it("back closes the sheet, then returns to Setări", async () => {
    at("?co=NORD&tab=setari");
    renderApp();
    await userEvent.click(await screen.findByRole("link", { name: /Integrări/ }));
    await page();
    await userEvent.click(screen.getByRole("link", { name: /Claude/ }));
    expect(window.location.search).toBe(`?co=NORD&tab=setari&sec=integrari&int=${ID.ai}`);
    window.history.back();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(window.location.search).toBe("?co=NORD&tab=setari&sec=integrari");
    window.history.back();
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=setari"));
  });

  it("adds a second ANAF named and for the firms left checked, and warns per firm what the engine reads twice or not at all", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    const d = fixtureIntegrations();
    const anaf = d.items.find((i) => i.kind === "anaf")!;
    d.items.push({ ...anaf, id: "anaf-2", name: "ANAF Studio", firm_scope: ["STUDIO9"] });
    d.coverage = [{ firm: "NORD", anaf: null, anaf_also: [], mail: [] },
      { firm: "STUDIO9", anaf: anaf.id, anaf_also: ["anaf-2"], mail: [] }];
    const api = renderApp({ integrations: () => json(d), onPost: () => json({ ok: true, message: "Integrarea a fost adăugată.", state: fixture() }) });
    const region = within(await screen.findByRole("region", { name: "ANAF e-Factura" }));
    expect(region.getByRole("status")).toHaveTextContent("NORD nu este în nicio conexiune ANAF.");
    expect(region.getByRole("status")).toHaveTextContent("STUDIO9 este în 2 conexiuni ANAF: ANAF e-Factura, ANAF Studio. Se citește prin ANAF e-Factura.");
    expect(region.getByRole("link", { name: /^ANAF Studio/ })).toHaveTextContent(/ANAF Producție\. STUDIO9$/);
    expect(region.getByRole("link", { name: /^ANAF Studio/ })).not.toHaveTextContent("Implicită"); // the engine picks per firm
    await userEvent.click(screen.getByRole("link", { name: "Adaugă: ANAF e-Factura" }));
    expect(within(sheet()).getByLabelText("Nume")).toHaveValue("ANAF e-Factura 3");
    await userEvent.type(within(sheet()).getByLabelText("Client ID"), "client-2");
    await userEvent.type(within(sheet()).getByLabelText("Client secret"), "synthetic-secret-2");
    await userEvent.click(within(sheet()).getByLabelText("Toate firmele"));
    await userEvent.click(within(sheet()).getByLabelText("NORD"));
    await userEvent.click(within(sheet()).getByLabelText("STUDIO9"));
    const save = within(sheet()).getByRole("button", { name: "Salvează și continuă" });
    expect(save).toBeDisabled();
    expect(save).toHaveAccessibleDescription("Alege cel puțin o firmă.");
    await userEvent.click(within(sheet()).getByLabelText("STUDIO9"));
    await userEvent.click(save);
    expect(api.posts()[0].body).toEqual({ co: "NORD", kind: "anaf", name: "ANAF e-Factura 3", firm_scope: ["STUDIO9"],
      f_client_id: "client-2", f_client_secret: "synthetic-secret-2" });
  });

  it("works before the install goes live, while money actions stay off", async () => {
    at(`?co=NORD&tab=setari&sec=integrari&int=${ID.storage}`);
    const s = fixture();
    s.live = false;
    const api = renderApp({ state: () => json(s), onPost: () => json({ ok: true, message: "Integrarea a fost oprită.", state: s }) });
    await page();
    await userEvent.click(within(sheet()).getByRole("button", { name: "Oprește" }));
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: ID.storage, on: "0" });
  });

  it("asks before removing, and posts nothing when declined", async () => {
    at(`?co=NORD&tab=setari&sec=integrari&int=${ID.storage}`);
    const api = server();
    render(<App />);
    await page();
    await userEvent.click(within(sheet()).getByRole("button", { name: "Șterge" }));
    expect(await answer(false)).toBe("Ștergi integrarea Arhiva documentelor? Cheile ei se șterg de pe server.");
    expect(api.posts()).toHaveLength(0);
    await userEvent.click(within(sheet()).getByRole("button", { name: "Șterge" }));
    await answer();
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: ID.storage });
  });

  it("says when the list can't be read and offers to try again", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    let n = 0;
    renderApp({ integrations: () => (++n === 1 ? json({ ok: false }, 500) : json(fixtureIntegrations())) });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Integrările nu au putut fi încărcate.");
    await userEvent.click(within(alert).getByRole("button", { name: "Reîncearcă" }));
    await page();
  });

  it("a section that isn't a Setări page falls back to Setări", async () => {
    at("?co=NORD&tab=setari&sec=nope");
    renderApp();
    expect(await screen.findByRole("heading", { name: "Configurare" })).toBeInTheDocument();
  });
});

it("saves a firm's archive destination and a fixed layout", async () => {
  at("?co=NORD&tab=setari&sec=integrari&int=new:storage");
  const api = renderApp();
  await screen.findByRole("dialog", { name: "Adaugă: Arhivă Google Drive" });
  const form = within(sheet());
  await userEvent.type(form.getByLabelText("Remote rclone"), "drive:");
  await userEvent.type(form.getByLabelText("ID dosar Google Drive"), "folder");
  await choose(form.getByLabelText("Organizarea documentelor"), "quarter");
  expect((await choicesOf(form.getByLabelText("Organizarea documentelor"))).map((o) => o.text)).toContain("Pe luni și tipuri de documente");
  await userEvent.click(form.getByRole("button", { name: "Adaugă integrarea" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", kind: "storage", name: "Arhivă Google Drive",
    f_company: "NORD", f_remote: "drive:", f_root: "folder", f_layout: "quarter" });
});

it.each([ID.ai, "ai-deleted", "new:unknown"])("keeps a direct link closeable through loading/failure (%s)", async (id) => {
  at(`?co=NORD&tab=setari&sec=integrari&int=${id}`);
  const pending = deferred<void>();
  let fail = true;
  renderApp({ integrations: () => pending.promise.then(() => fail ? json({}, 500) : json(fixtureIntegrations())) });
  const d = await screen.findByRole("dialog", { name: "Integrare" });
  expect(within(d).getByRole("status")).toBeInTheDocument();
  pending.resolve();
  await within(d).findByRole("alert");
  fail = false;
  await userEvent.click(within(d).getByRole("button", { name: "Reîncearcă" }));
  if (id === ID.ai) await screen.findByRole("dialog", { name: "Claude" });
  else await within(d).findByText(id.startsWith("new:") ? "Acest tip de integrare nu este disponibil." : "Integrarea nu mai este disponibilă.");
  expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  await userEvent.tab();
  expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});


it.each([["claude-cli", "code"], ["claude-cli", "cancel"], ["claude-cli", "add"], ["codex-cli", "cancel"]])("relays %s login: %s", async (provider, op) => {
  at(`?co=NORD&tab=setari&sec=integrari&int=${op === "add" ? "new:ai" : ID.ai}`);
  const data = fixtureIntegrations();
  data.items.find((i) => i.kind === "ai")!.config = { provider };
  let state = "idle";
  const result = () => ({ provider, state, url: state === "waiting" ? "https://claude.ai/login" : "", code: state === "waiting" && provider === "codex-cli" ? "ABCD-EFGH" : "", needs_code: state === "waiting" && provider === "claude-cli", readiness: state === "connected" ? "ready" : "not_logged_in" });
  const api = renderApp({ integrations: () => json(data), cliStatus: () => json(result()), onPost: (c) => {
    if (!c.url.includes("integration-cli")) return json({ ok: true, message: "" });
    state = c.body?.op === "start" ? "waiting" : c.body?.op === "code" ? "connected" : "cancelled";
    return json(result());
  } });
  const name = provider === "claude-cli" ? "Claude Code" : "Codex";
  if (op === "add") await choose(await screen.findByLabelText("Serviciu"), provider);
  expect(screen.queryByRole("button", { name: "Adaugă integrarea" })).toBeNull(); // the login adds it
  await userEvent.click(await screen.findByRole("button", { name: `Conectează ${name}` }));
  const link = await screen.findByRole("link", { name: `Deschide pagina ${provider === "claude-cli" ? "Claude" : "OpenAI"}` });
  expect(link).toHaveAttribute("href", "https://claude.ai/login");
  expect(link).toHaveAttribute("target", "_blank");
  if (provider === "codex-cli") expect(screen.getByText("ABCD-EFGH")).toBeInTheDocument();
  if (op !== "cancel") {
    await userEvent.type(screen.getByLabelText(/lipește-l aici/), "pasted-code");
    await userEvent.click(screen.getByRole("button", { name: "Confirmă" }));
    expect(api.posts()[1].body).toEqual({ op: "code", provider, code: "pasted-code" });
  }
  if (op === "add") {
    await waitFor(() => expect(api.posts()[2]?.body).toEqual({ co: "NORD", kind: "ai", name: "Serviciu AI", f_provider: provider, f_model: "" }));
  } else if (op === "code") {
    expect(await screen.findByText("Autentificarea a fost confirmată.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: `Deconectează ${name}` })).toBeInTheDocument();
  } else {
    await userEvent.click(screen.getByRole("button", { name: "Anulează" }));
    expect(api.posts()[1].body).toEqual({ op: "cancel", provider });
    expect(await screen.findByText("Autentificarea a fost anulată.")).toBeInTheDocument();
  }
  expect(api.posts()[0].body).toEqual({ op: "start", provider });
});

it("shows the install command for a missing CLI and keeps AI read-only for non-admins", async () => {
  at(`?co=NORD&tab=setari&sec=integrari&int=${ID.ai}`);
  const data = fixtureIntegrations();
  data.items.find((i) => i.kind === "ai")!.config = { provider: "claude-cli" };
  server({ integrations: () => json(data), cliStatus: () => json({ provider: "", state: "idle", url: "", code: "", needs_code: false, readiness: "missing", install_command: "docker compose up -d --build" }) });
  const r = render(<App />);
  expect(await screen.findByText("docker compose up -d --build")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Conectează Claude Code" })).toBeDisabled();
  r.unmount();
  data.can_manage_ai = false;
  renderApp({ integrations: () => json(data) });
  expect(await screen.findByText("Administratorul instalării configurează serviciul AI.")).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Serviciu" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Conectează Claude Code" })).toBeNull();
});

it("never reuses administrative integration data after another account mounts the app", async () => {
  at("?co=NORD&tab=setari&sec=integrari"); server(); const first = render(<App />);
  await page(); await userEvent.click(screen.getByRole("link", { name: /Claude/ }));
  await within(sheet()).findByLabelText("Model (opțional)"); first.unmount();
  const identity = fixtureIdentity("readonly"); identity.me.id = "fixture-another-user";
  const api = server(); render(<App />, { identity });
  await screen.findByRole("heading", { name: "Setări" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Model (opțional)")).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /^Integrări/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /^Firme/ })).not.toBeInTheDocument();
  expect(api.calls.some(c => /api\/(integrations|backup)/.test(c.url))).toBe(false);
});

it("honours the server's integration capability for a member when no administrator exists", async () => {
  const identity = fixtureIdentity("member"); identity.me.can.manage_integrations = true;
  at("?co=NORD&tab=setari&sec=integrari"); const api = server(); render(<App />, { identity });
  await page(); expect(api.calls.some(c => c.url.endsWith("api/integrations"))).toBe(true);
});

const GMAIL = { provider: "Gmail", host: "imap.gmail.com", port: 993, username: "facturi.nord@gmail.com", app_password_required: true,
  app_password_url: "https://myaccount.google.com/apppasswords", hint: "", supported: true, hosts: ["imap.gmail.com"] };
const mailEngine = (detected: object, tests: string[]) => (c: Call) => c.url.endsWith("api/mail-detect")
  ? json({ ok: true, message: "", done: { id: "", held: "", ...detected } })
  : c.url.endsWith("api/mail-test") ? json({ ok: true, message: "", done: { id: "", held: "", code: tests.shift(), host: "imap.gmail.com", port: 993 } })
  : json({ ok: true, message: "Datele au fost salvate.", state: fixture() });

it("a new inbox asks the address, explains the app password, and saves only after a test or on purpose", async () => {
  at("?co=NORD&tab=setari&sec=integrari&int=new:mail");
  const api = renderApp({ onPost: mailEngine(GMAIL, ["app_password_required", "ok"]) });
  const form = within(await screen.findByRole("dialog", { name: "Adaugă o adresă de e-mail" }));
  expect(form.queryByLabelText(/Parola/)).toBeNull();
  await userEvent.type(form.getByLabelText("Adresa de e-mail"), "facturi.nord@gmail.com");
  await userEvent.click(form.getByRole("button", { name: "Continuă" }));
  expect(await form.findByText("Cont Gmail")).toBeInTheDocument();
  expect(form.getByRole("link", { name: /Creează parola în Gmail/ })).toHaveAttribute("href", GMAIL.app_password_url);
  expect(form.queryByRole("button", { name: "Salvează" })).toBeNull();
  await userEvent.type(form.getByLabelText("Parola de aplicație"), "parola-exemplu");
  await userEvent.click(form.getByRole("button", { name: "Testează conexiunea" }));
  expect(await form.findByText("Gmail nu acceptă parola contului.")).toBeInTheDocument();
  expect(form.getByRole("button", { name: "Salvează oricum" })).toBeEnabled();
  await userEvent.click(form.getByRole("button", { name: "Testează conexiunea" }));
  expect(await form.findByText("Conexiune verificată.")).toBeInTheDocument();
  await userEvent.click(form.getByRole("button", { name: "Salvează" }));
  expect(api.posts().map((c) => c.body)).toEqual([
    { co: "NORD", address: "facturi.nord@gmail.com" },
    ...Array(2).fill({ co: "NORD", from_address: "facturi.nord@gmail.com", password: "parola-exemplu", username: "facturi.nord@gmail.com", read_folder: "INBOX" }),
    { co: "NORD", kind: "mail", name: "facturi.nord@gmail.com", f_from_address: "facturi.nord@gmail.com", f_password: "parola-exemplu",
      f_host: "imap.gmail.com", f_port: "993", f_username: "facturi.nord@gmail.com", f_own_addresses: "", f_read_folder: "INBOX",
      firm_scope: "all" },
  ]);
});

it("an answer that arrives after an edit is dropped: the settings on screen keep their own detection and test", async () => {
  at("?co=NORD&tab=setari&sec=integrari&int=new:mail");
  const late = deferred<Response>();
  const engine = mailEngine(GMAIL, []);
  renderApp({ onPost: (c) => c.url.endsWith("api/mail-test") ? late.promise : engine(c) });
  const form = within(await screen.findByRole("dialog", { name: "Adaugă o adresă de e-mail" }));
  await userEvent.type(form.getByLabelText("Adresa de e-mail"), "facturi.nord@gmail.com{Enter}");
  await userEvent.type(await form.findByLabelText("Parola de aplicație"), "parola-veche");
  await userEvent.click(form.getByRole("button", { name: "Testează conexiunea" }));
  await userEvent.type(form.getByLabelText("Parola de aplicație"), "-noua");
  late.resolve(json({ ok: true, message: "", done: { id: "", held: "", code: "ok", host: "imap.gmail.com", port: 993 } }));
  await waitFor(() => expect(form.getByRole("button", { name: "Testează conexiunea" })).toHaveTextContent("Testează conexiunea"));
  expect(form.queryByText("Conexiune verificată.")).toBeNull();
  expect(form.queryByRole("button", { name: "Salvează" })).toBeNull();
});

it("a saved inbox whose address is changed and changed back keeps its server when the detection answers late", async () => {
  const mail = fixtureIntegrations().items.find((i) => i.kind === "mail")!;
  at(`?co=NORD&tab=setari&sec=integrari&int=${mail.id}`);
  const late = deferred<Response>();
  renderApp({ onPost: () => late.promise });
  const address = await screen.findByLabelText("Adresa de e-mail");
  await userEvent.type(address, "x");
  await userEvent.tab(); // blur: the changed address is detected
  await userEvent.type(address, "{Backspace}");
  late.resolve(json({ ok: true, message: "", done: { id: "", held: "", ...GMAIL, username: "x" } }));
  await waitFor(() => expect(within(sheet()).getByRole("button", { name: "Testează conexiunea" })).toBeEnabled());
  expect(within(sheet()).getByLabelText("Server IMAP")).toHaveValue(mail.config.host);
});

it("Proton is said calmly, with the forwarding tip, and nothing to test", async () => {
  at("?co=NORD&tab=setari&sec=integrari&int=new:mail");
  renderApp({ onPost: mailEngine({ ...GMAIL, provider: "Proton Mail", app_password_required: false, app_password_url: "", supported: false, method: "imap" }, []) });
  const form = within(await screen.findByRole("dialog", { name: "Adaugă o adresă de e-mail" }));
  await userEvent.type(form.getByLabelText("Adresa de e-mail"), "facturi@proton.me{Enter}");
  expect(await form.findByRole("status")).toHaveTextContent(/Proton Mail nu se poate conecta direct.*redirecționează spre ea/);
  expect(form.queryByRole("alert")).toBeNull();
  expect(form.queryByLabelText(/Parola/)).toBeNull();
  expect(form.queryByRole("button", { name: /Testează|Salvează/ })).toBeNull();
});

const MICROSOFT = { ...GMAIL, provider: "Microsoft", host: "", username: "facturi@outlook.com", app_password_required: false,
  app_password_url: "", method: "microsoft" };
/** The engine's Microsoft sign-in: `answers` for start then each poll, in order (fiscalm.mail.microsoft). */
// Like the engine, a start that names an existing inbox (by id) and also a scope is refused.
const microsoftEngine = (answers: object[]) => (c: Call) => c.url.endsWith("api/mail-microsoft-start") && c.body?.id && c.body.firm_scope
  ? json({ ok: false, message: "Conectarea Microsoft nu a reușit.", done: { code: "invalid_settings" } })
  : c.url.includes("api/mail-microsoft-") ? json({ ok: true, message: "", done: { held: "", ...answers.shift() } }) : mailEngine(MICROSOFT, [])(c);
const CODE = { code: "authorization_pending", id: "mail-9", user_code: "KX7M2Q9TB", verification_uri: "https://example.invalid/x", expires_in: 900, interval: 5 };

describe("a Microsoft inbox", () => {
  beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
  afterEach(() => { vi.useRealTimers(); });

  it("signs in with a code on Microsoft's own page, for the firms chosen first, waits on its own, then saves", async () => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:mail");
    const api = renderApp({ onPost: microsoftEngine([CODE, { code: "slow_down", id: "mail-9", interval: 10 }, { code: "connected", id: "mail-9" }]) });
    const form = within(await screen.findByRole("dialog", { name: "Adaugă o adresă de e-mail" }));
    await userEvent.type(form.getByLabelText("Adresa de e-mail"), "facturi@outlook.com{Enter}");
    expect(await form.findByText("Cont Microsoft")).toBeInTheDocument();
    expect(form.queryByLabelText(/Parola/)).toBeNull();
    await userEvent.click(form.getByLabelText("Toate firmele"));
    await userEvent.click(form.getByLabelText("NORD")); // the inbox is created for STUDIO9 alone, before it can read
    await userEvent.click(form.getByRole("button", { name: "Conectează contul Microsoft" }));
    expect(await form.findByText("KX7M2Q9TB")).toBeInTheDocument();
    expect(form.getByRole("link", { name: /Deschide pagina Microsoft/ })).toHaveAttribute("href", "https://microsoft.com/devicelogin");
    await vi.advanceTimersByTimeAsync(5000);
    await vi.advanceTimersByTimeAsync(10000);
    expect(await form.findByText("Conectată.")).toBeInTheDocument();
    await userEvent.click(form.getByRole("button", { name: "Salvează" }));
    expect(api.posts().map((c) => c.body)).toEqual([
      { co: "NORD", address: "facturi@outlook.com" },
      { co: "NORD", address: "facturi@outlook.com", firm_scope: ["STUDIO9"] },
      { co: "NORD", id: "mail-9" }, { co: "NORD", id: "mail-9" },
      { co: "NORD", id: "mail-9", name: "facturi@outlook.com", f_own_addresses: "", firm_scope: ["STUDIO9"] },
    ]);
  });

  it("stops at the code's expiry and starts again on request, for the inbox the first start created", async () => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:mail");
    const api = renderApp({ onPost: microsoftEngine([{ ...CODE, expires_in: 6 }, { code: "authorization_pending", id: "mail-9", interval: 5 }, CODE]) });
    const form = within(await screen.findByRole("dialog", { name: "Adaugă o adresă de e-mail" }));
    await userEvent.type(form.getByLabelText("Adresa de e-mail"), "facturi@outlook.com{Enter}");
    await userEvent.click(await form.findByRole("button", { name: "Conectează contul Microsoft" }));
    expect(form.getByLabelText("Toate firmele")).toBeDisabled(); // the inbox exists with these firms until it is saved
    await vi.advanceTimersByTimeAsync(5000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(await form.findByText("Codul a expirat.")).toBeInTheDocument();
    await userEvent.click(form.getByRole("button", { name: "Încearcă din nou" }));
    expect(await form.findByText("KX7M2Q9TB")).toBeInTheDocument();
    expect(api.posts().filter((c) => c.url.endsWith("api/mail-microsoft-start")).map((c) => c.body)).toEqual([
      { co: "NORD", address: "facturi@outlook.com", firm_scope: "all" }, { co: "NORD", address: "facturi@outlook.com", id: "mail-9" }]);
  });

  it.each([
    ["admin_consent_required", /administratorului Microsoft 365 al firmei/],
    ["not_configured", /nu e configurat pe acest server/],
    ["authorization_declined", /refuzat/],
  ])("says who acts next when Microsoft answers %s", async (code, words) => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:mail");
    renderApp({ onPost: (c) => c.url.endsWith("api/mail-microsoft-start")
      ? json({ ok: false, message: "Conectarea Microsoft nu a reușit.", done: { code } }) : microsoftEngine([])(c) });
    const form = within(await screen.findByRole("dialog", { name: "Adaugă o adresă de e-mail" }));
    await userEvent.type(form.getByLabelText("Adresa de e-mail"), "facturi@outlook.com{Enter}");
    await userEvent.click(await form.findByRole("button", { name: "Conectează contul Microsoft" }));
    expect(await form.findByRole("alert")).toHaveTextContent(words);
    expect(form.getByRole("button", { name: "Încearcă din nou" })).toBeEnabled();
  });

  it("a cancelled step-up while waiting returns to the button, without a Microsoft error", async () => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:mail");
    renderApp({ onPost: (c) => c.url.endsWith("api/mail-microsoft-poll") ? json({ ok: false, message: "", cancelled: true }) : microsoftEngine([CODE])(c) });
    const form = within(await screen.findByRole("dialog", { name: "Adaugă o adresă de e-mail" }));
    await userEvent.type(form.getByLabelText("Adresa de e-mail"), "facturi@outlook.com{Enter}");
    await userEvent.click(await form.findByRole("button", { name: "Conectează contul Microsoft" }));
    expect(await form.findByText("KX7M2Q9TB")).toHaveFocus();
    await vi.advanceTimersByTimeAsync(5000);
    expect(await form.findByRole("button", { name: "Conectează contul Microsoft" })).toBeEnabled();
    expect(form.queryByRole("alert")).toBeNull();
  });

  it("a saved password inbox changed to a Microsoft address is sent to Adaugă, not signed in", async () => {
    const mail = fixtureIntegrations().items.find((i) => i.kind === "mail")!;
    at(`?co=NORD&tab=setari&sec=integrari&int=${mail.id}`);
    renderApp({ onPost: microsoftEngine([]) });
    const address = await screen.findByLabelText("Adresa de e-mail");
    await userEvent.clear(address);
    await userEvent.type(address, "facturi@outlook.com{Enter}");
    expect(await within(sheet()).findByText("Un cont Microsoft se adaugă ca adresă nouă.")).toBeInTheDocument();
    expect(within(sheet()).queryByRole("button", { name: /Conectează contul Microsoft|Salvează|Testează/ })).toBeNull();
  });

  it("whose consent ended asks to reconnect in the list, and reconnects that inbox", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    const d = fixtureIntegrations();
    const mail = d.items.find((i) => i.kind === "mail")!;
    d.items.push({ ...mail, id: "mail-9", name: "facturi@outlook.com", secrets: {},
      config: { from_address: "facturi@outlook.com", method: "microsoft" },
      reading: { ok: false, at: "2026-10-27T08:00:00", last_ok: null, code: "reconnect_required" } });
    const api = renderApp({ integrations: () => json(d), onPost: microsoftEngine([CODE]) });
    const row = within(await screen.findByRole("region", { name: "Cont de e-mail" })).getByRole("link", { name: /facturi@outlook/ });
    expect(row).toHaveTextContent("Reconectează contul Microsoft");
    await userEvent.click(row);
    await userEvent.click(within(sheet()).getByRole("button", { name: "Reconectează contul Microsoft" }));
    expect(await within(sheet()).findByText("KX7M2Q9TB")).toBeInTheDocument();
    expect(api.posts()[0].body).toEqual({ co: "NORD", address: "facturi@outlook.com", id: "mail-9" });
  });
});

it("lists each inbox with how its reading goes, and tests a saved one by its id", async () => {
  at("?co=NORD&tab=setari&sec=integrari");
  const d = fixtureIntegrations();
  const mail = d.items.find((i) => i.kind === "mail")!;
  d.items.push({ ...mail, id: "mail-2", name: "office@exemplu.ro", reading: { ok: false, at: "2026-10-27T08:00:00", last_ok: null, code: "timeout" } },
    { ...mail, id: "mail-3", name: "arhiva@exemplu.ro", enabled: false });
  const api = renderApp({ integrations: () => json(d), onPost: mailEngine({}, ["wrong_password"]) });
  const inboxes = within(await screen.findByRole("region", { name: "Cont de e-mail" }));
  expect(inboxes.getByRole("link", { name: /E-mail/ })).toHaveTextContent("Conectată. Citită pe 27 oct, 09:10");
  expect(inboxes.getByRole("link", { name: /office@/ })).toHaveTextContent("Serverul nu a răspuns la timp.");
  expect(inboxes.getByRole("link", { name: /arhiva@/ })).toHaveTextContent("Oprită");
  expect(screen.getByRole("link", { name: "Adaugă: Cont de e-mail" })).toHaveTextContent("Adaugă încă o adresă");
  await userEvent.click(inboxes.getByRole("link", { name: /office@/ }));
  await userEvent.click(within(sheet()).getByRole("button", { name: "Testează conexiunea" }));
  expect(await within(sheet()).findByText("Parola nu este corectă.")).toBeInTheDocument();
  expect(api.posts()[0].body).toEqual({ co: "NORD", id: "mail-2" });
});

it("changes a saved inbox's firms without a new test", async () => {
  at(`?co=NORD&tab=setari&sec=integrari&int=${ID.mail}`);
  const api = renderApp({ onPost: () => json({ ok: true, message: "Modificările au fost salvate.", state: fixture() }) });
  await userEvent.click(await screen.findByLabelText("Toate firmele"));
  const form = within(sheet());
  await userEvent.click(form.getByLabelText("STUDIO9"));
  await userEvent.click(form.getByRole("button", { name: "Salvează" }));
  expect(api.posts()[0].url).toContain("api/integration-update");
  expect(api.posts()[0].body).toEqual({ co: "NORD", id: ID.mail, name: "facturi@example.com", f_from_address: "facturi@example.com", f_host: "imap.example.com",
    f_port: "993", f_username: "facturi", f_own_addresses: "", f_read_folder: "INBOX", firm_scope: ["NORD"] });
});

it("with one firm, an inbox has no Firme choice, and a saved scope of no firm still saves other changes unsent", async () => {
  at("?co=NORD&tab=setari&sec=integrari");
  const s = fixture();
  s.companies = s.companies.slice(0, 1);
  const d = fixtureIntegrations();
  d.items.find((i) => i.kind === "mail")!.firm_scope = [];
  const api = renderApp({ state: () => json(s), integrations: () => json(d),
    onPost: () => json({ ok: true, message: "Modificările au fost salvate.", state: fixture() }) });
  const inboxes = within(await screen.findByRole("region", { name: "Cont de e-mail" }));
  expect(inboxes.getByRole("link", { name: /^E-mail/ })).not.toHaveTextContent("Nicio firmă");
  await userEvent.click(inboxes.getByRole("link", { name: /^E-mail/ }));
  expect(within(sheet()).queryByText("Firme")).toBeNull();
  await userEvent.click(within(sheet()).getByText("Setări avansate"));
  expect(within(sheet()).queryByLabelText("Citește facturile")).toBeNull(); // Oprește is the one switch
  await userEvent.type(within(sheet()).getByLabelText("Alte adrese proprii (separate prin virgulă)"), "plati@exemplu.ro");
  await userEvent.click(within(sheet()).getByRole("button", { name: "Salvează" }));
  expect(api.posts()[0].body).not.toHaveProperty("firm_scope");
  expect(api.posts()[0].body).toMatchObject({ id: ID.mail, f_own_addresses: "plati@exemplu.ro" });
});

it("a new shop asks its platform first, then only that platform's fields, for the firm on screen", async () => {
  at("?co=NORD&tab=setari&sec=integrari&int=new:shop");
  const api = renderApp();
  await screen.findByRole("dialog", { name: "Adaugă: Magazin online" });
  const form = within(sheet());
  const labels = () => form.getAllByLabelText(/./).filter((e) => e.id.startsWith("int-")).map((e) => e.id.slice(4));
  expect(labels()).toEqual(["platform"]);
  await choose(form.getByLabelText("Platformă"), "woocommerce");
  expect(labels()).toEqual(["platform", "series", "webhook_url", "webhook_secret"]); // auto-issue is a rule, in Automatizări
  await choose(form.getByLabelText("Platformă"), "shopify");
  expect(labels()).not.toContain("fiscalm_url"); // the install's own address is known
  expect(labels()).not.toContain("emag_username");
  await userEvent.type(form.getByLabelText("Serie"), "WEB");
  await userEvent.type(form.getByLabelText("Domeniu Shopify"), "exemplu.myshopify.com");
  await userEvent.type(form.getByLabelText("Token Admin API"), "token-exemplu");
  await userEvent.type(form.getByLabelText("Secret Shopify"), "secret-exemplu");
  await userEvent.click(form.getByRole("button", { name: "Adaugă integrarea" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", kind: "shop", name: "Magazin online", f_platform: "shopify", f_company: "NORD",
    f_series: "WEB", f_shop_domain: "exemplu.myshopify.com", f_admin_token: "token-exemplu",
    f_shopify_secret: "secret-exemplu", f_fiscalm_url: "https://fiscalm.example.com" });
});

it("a saved archive can move to another firm, picked from the configured firms", async () => {
  at(`?co=NORD&tab=setari&sec=integrari&int=${ID.storage}`);
  const api = renderApp();
  await screen.findByRole("dialog", { name: "Arhiva documentelor" });
  const form = within(sheet());
  await choose(form.getByLabelText("Firmă"), "STUDIO9");
  await userEvent.click(form.getByRole("button", { name: "Salvează" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", id: ID.storage, name: "Arhiva documentelor", f_company: "STUDIO9",
    f_remote: fixtureIntegrations().items[3].config.remote, f_root: fixtureIntegrations().items[3].config.root, f_layout: "month" });
});
