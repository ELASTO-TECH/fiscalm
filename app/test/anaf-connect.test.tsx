// Connecting ANAF from its integration sheet against a fake engine: the connection's state in words, its setup folded
// away once the client is saved, the exact POST, leaving only for the install's own start link, and who may start it.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { safeStart } from "@/components/AnafConnect";
import { leave } from "@/components/BankConnect";
import type { AnafConnection, Integrations } from "@/lib/types";
import { at, fixture, fixtureIntegrations, json, server, renderApp } from "./helpers";

const CALLBACK = "https://fiscalm.example.com/anaf/callback";
const LINK = "https://fiscalm.example.com/anaf/start/" + "a".repeat(43);

afterEach(() => vi.restoreAllMocks());

function withConnection(c: Partial<AnafConnection>, change: (d: Integrations) => void = () => {}): Integrations {
  const d = fixtureIntegrations();
  const anaf = d.items.find((i) => i.kind === "anaf")!;
  anaf.connection = { ...anaf.connection!, ...c };
  change(d);
  return d;
}

async function sheet(d: Integrations, opts: Parameters<typeof server>[0] = {}) {
  at(`?co=NORD&tab=setari&sec=integrari&int=${d.items.find((i) => i.kind === "anaf")!.id}`);
  const api = renderApp({ integrations: () => json(d), ...opts });
  const dialog = await screen.findByRole("dialog", { name: "ANAF e-Factura" });
  await within(dialog).findByRole("region", { name: "Conexiunea ANAF" });
  return { api, dialog: within(dialog) };
}

describe("the ANAF integration's connection", () => {
  it("a saved client shows only the state and the button; its setup folds under Setări avansate", async () => {
    const { dialog } = await sheet(withConnection({ state: "not_connected", expires: null }));
    expect(dialog.getByText("Neconectat")).toBeInTheDocument();
    expect(dialog.getByText("Facturile din e-Factura se citesc după conectare.")).toBeInTheDocument();
    expect(dialog.getByRole("button", { name: "Conectează ANAF" })).toBeEnabled();
    const advanced = dialog.getByText("Setări avansate").closest("details")!;
    expect(advanced.open).toBe(false);
    const field = dialog.getByLabelText("Adresa de redirecționare (Callback URL)") as HTMLInputElement;
    expect(field.value).toBe(CALLBACK);
    expect(field.readOnly).toBe(true);
    expect(dialog.getByText("Firme: Toate firmele. Se schimbă în Setări avansate.")).toBeInTheDocument(); // what it reads, while folded
    for (const label of ["Adresa de redirecționare (Callback URL)", "Nume", "Client ID", "Client secret", "Toate firmele"]) {
      expect(advanced).toContainElement(dialog.getByLabelText(label));
    }
    expect(advanced).toContainElement(dialog.getByRole("button", { name: "Salvează" }));
    expect(dialog.queryByRole("alert")).toBeNull();
  });

  it("shows the Callback URL on the add sheet too, before there is a Client ID", async () => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:anaf");
    const d = fixtureIntegrations();
    d.items = d.items.filter((i) => i.kind !== "anaf");
    renderApp({ integrations: () => json(d) });
    const dialog = within(await screen.findByRole("dialog", { name: "Adaugă: ANAF e-Factura" }));
    expect((dialog.getByLabelText("Adresa de redirecționare (Callback URL)") as HTMLInputElement).value).toBe(CALLBACK);
    expect(dialog.queryByRole("button", { name: /onectează ANAF/ })).toBeNull();
    const portal = dialog.getByRole("link", { name: "Deschide portalul ANAF într-un tab nou" });
    expect(portal).toHaveAttribute("href", "https://pfinternet.anaf.ro");
    expect(portal).toHaveAttribute("target", "_blank");
    expect(portal).toHaveAttribute("rel", "noopener noreferrer");
    expect(dialog.getByRole("heading", { name: "Pregătește conectarea la ANAF" })).toBeInTheDocument();
  });

  it("keeps newly saved ANAF credentials open for certificate authorization, including Enter submission", async () => {
    at("?co=NORD&tab=setari&sec=integrari&int=new:anaf");
    const data = fixtureIntegrations();
    const saved = data.items.find(i => i.kind === "anaf")!;
    saved.connection = { ...saved.connection!, state: "not_connected", expires: null };
    data.items = data.items.filter(i => i.kind !== "anaf");
    const to = vi.spyOn(leave, "to").mockImplementation(() => {});
    const api = renderApp({ integrations: () => json(data), onPost: call => {
      if (call.url.endsWith("/integration-add")) {
        data.items.push(saved);
        return json({ ok: true, message: "Integrarea a fost adăugată.", done: { id: saved.id }, state: fixture() });
      }
      return json({ ok: true, message: "", done: { url: LINK }, state: fixture() });
    } });
    const user = userEvent.setup();
    const form = within(await screen.findByRole("dialog", { name: "Adaugă: ANAF e-Factura" }));
    await user.click(form.getByLabelText("Client ID"));
    await user.paste("example-client-id");
    await user.click(form.getByLabelText("Client secret"));
    await user.paste("synthetic-client-secret");
    await user.keyboard("{Enter}");
    const savedForm = within(await screen.findByRole("dialog", { name: "ANAF e-Factura" }));
    const connect = await savedForm.findByRole("button", { name: "Conectează ANAF" });
    await waitFor(() => expect(connect).toBeEnabled());
    expect(savedForm.getByLabelText("Client secret")).toHaveValue("");
    expect(api.posts()[0].body).toEqual({ co: "NORD", kind: "anaf", name: "ANAF e-Factura", firm_scope: "all",
      f_client_id: "example-client-id", f_client_secret: "synthetic-client-secret" });
    await user.click(connect);
    await waitFor(() => expect(to).toHaveBeenCalledWith(LINK));
    expect(api.posts()[1].body).toEqual({ co: "NORD", id: saved.id });
  });

  it.each([
    ["connected", "2027-11-03", "Conectat", "Conexiunea expiră pe 3 nov 2027.", "Reconectează ANAF"],
    ["expiring", "2026-11-16", "Expiră curând", "Conexiunea expiră pe 16 nov.", "Reconectează ANAF"],
    ["expired", "2026-10-20", "Conexiunea a expirat", "Facturile noi din e-Factura nu se mai citesc până la reconectare.", "Reconectează ANAF"],
    ["error", null, "Conexiunea nu poate fi citită", "Facturile din e-Factura se citesc după reconectare.", "Reconectează ANAF"],
  ] as const)("reads %s as the engine says it", async (state, expires, title, line, button) => {
    const { dialog } = await sheet(withConnection({ state, expires }));
    expect(document.body.textContent).not.toMatch(/fixture-access|fixture-refresh|fixture-not-a-secret/);
    expect(dialog.getByLabelText(/Client secret/)).toHaveValue("");
    expect(dialog.getByText(title)).toBeInTheDocument();
    expect(dialog.getByText(line)).toBeInTheDocument();
    expect(dialog.getByRole("button", { name: button })).toBeEnabled();
  });

  it("connecting posts the integration, then leaves for the start link on this install's address", async () => {
    const to = vi.spyOn(leave, "to").mockImplementation(() => {});
    const { api, dialog } = await sheet(withConnection({ state: "not_connected", expires: null }), {
      onPost: () => json({ ok: true, message: "Urmează autentificarea cu certificatul digital la ANAF.", done: { url: LINK }, state: fixture() }),
    });
    await userEvent.click(dialog.getByRole("button", { name: "Conectează ANAF" }));
    await waitFor(() => expect(to).toHaveBeenCalledWith(LINK));
    expect(api.posts()).toHaveLength(1);
    expect(api.posts()[0]).toMatchObject({ url: "/web/api/integration-connect", method: "POST",
      body: { co: "NORD", id: fixtureIntegrations().items.find((i) => i.kind === "anaf")!.id } });
    expect(Object.keys(api.posts()[0].body!)).toEqual(["co", "id"]);
    expect(await screen.findByText("Urmează autentificarea cu certificatul digital la ANAF.")).toBeInTheDocument();
    const leaving = dialog.getByRole("button", { name: "Se deschide ANAF…" });
    expect(leaving).toBeDisabled();
    expect(leaving).toHaveAttribute("aria-busy", "true");
  });

  it("shows why the last attempt was refused, in the engine's words", async () => {
    const refusal = "ANAF a refuzat autorizarea accesului. Nu s-a schimbat nimic.";
    const { dialog } = await sheet(withConnection({ state: "not_connected", expires: null, refusal }));
    const alert = within(dialog.getByRole("alert"));
    expect(alert.getByText("ANAF nu a fost conectat")).toBeInTheDocument();
    expect(alert.getByText(refusal)).toBeInTheDocument();
    expect(dialog.getByRole("button", { name: "Conectează ANAF" })).toBeEnabled();
  });

  it("never follows an address that is not this install's start link, and shows the engine's refusal", async () => {
    const to = vi.spyOn(leave, "to").mockImplementation(() => {});
    const { dialog } = await sheet(withConnection({}), {
      onPost: () => json({ ok: true, message: "", done: { url: "https://logincert.anaf.ro/anaf-oauth2/v1/authorize" }, state: null }),
    });
    await userEvent.click(dialog.getByRole("button", { name: "Reconectează ANAF" }));
    await new Promise((r) => setTimeout(r, 50));
    expect(to).not.toHaveBeenCalled();
    expect(dialog.getByRole("button", { name: "Reconectează ANAF" })).toBeEnabled();
  });

  it("says the engine's refusal and stays", async () => {
    const to = vi.spyOn(leave, "to").mockImplementation(() => {});
    const message = "Conectarea ANAF o poate porni doar un administrator.";
    const { dialog } = await sheet(withConnection({}), { onPost: () => json({ ok: false, message, state: null }, 403) });
    await userEvent.click(dialog.getByRole("button", { name: "Reconectează ANAF" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(to).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it.each([
    ["the public address is missing", (d: Integrations) => {
      const a = d.items.find((i) => i.kind === "anaf")!;
      a.connection = { ...a.connection!, redirect_uri: null,
        redirect_error: "Adresa publică a instalării nu este setată. Administratorul serverului o setează." };
    }, fixture(), "Adresa publică a instalării nu este setată. Administratorul serverului o setează."],
    ["the integration is off", (d: Integrations) => { d.items.find((i) => i.kind === "anaf")!.enabled = false; },
      fixture(), "Integrarea este oprită. Pornește-o ca să te conectezi."],
    ["the user is not an administrator", () => {}, { ...fixture(), can_manage_companies: false }, "Conectarea o pornește un administrator."],
  ])("offers no button when %s, and says why in one line", async (_, change, state, why) => {
    const { dialog } = await sheet(withConnection({}, change), { state: () => json(state) });
    expect(dialog.queryByRole("button", { name: /onectează ANAF/ })).toBeNull();
    expect(within(dialog.getByRole("region", { name: "Conexiunea ANAF" })).getByRole("status")).toHaveTextContent(why);
    expect(dialog.getAllByText(why)).toHaveLength(1);
  });

  it("without a saved secret shows the fields in plain sight and no connect button", async () => {
    const { dialog } = await sheet(withConnection({}, (d) => { d.items.find((i) => i.kind === "anaf")!.secrets.client_secret = false; }));
    expect(dialog.queryByRole("button", { name: /onectează ANAF/ })).toBeNull();
    expect(dialog.queryByText("Setări avansate")).toBeNull();
    expect(dialog.getByLabelText("Client secret").closest("details")).toBeNull();
    expect(dialog.getByRole("button", { name: "Salvează" })).toBeInTheDocument();
  });

  it("the list row says the connection's state", async () => {
    at("?co=NORD&tab=setari&sec=integrari");
    renderApp({ integrations: () => json(withConnection({ state: "expired", expires: "2026-10-20" })) });
    const region = within(await screen.findByRole("region", { name: "ANAF e-Factura" }));
    expect(region.getByRole("link", { name: /Conexiunea a expirat/ })).toHaveTextContent("Conexiunea a expirat");
  });
});

describe("safeStart", () => {
  it.each([
    [LINK, true],
    ["http://fiscalm.example.com/anaf/start/" + "a".repeat(43), false],
    ["https://evil.example.com/anaf/start/" + "a".repeat(43), false],
    ["https://fiscalm.example.com.evil.example/anaf/start/" + "a".repeat(43), false],
    ["https://user@fiscalm.example.com/anaf/start/" + "a".repeat(43), false],
    ["https://fiscalm.example.com/anaf/start/" + "a".repeat(43) + "?next=x", false],
    ["https://fiscalm.example.com/anaf/start/short", false],
    ["https://fiscalm.example.com/web/", false],
    ["https://fiscalm.example.com\\@evil.example/anaf/start/" + "a".repeat(43), false],
    ["javascript:alert(1)", false],
    [undefined, false],
  ])("%s -> %s", (url, ok) => {
    expect(safeStart(url, CALLBACK)).toBe(ok);
  });

  it("refuses everything without a Callback URL", () => {
    expect(safeStart(LINK, null)).toBe(false);
  });
});
