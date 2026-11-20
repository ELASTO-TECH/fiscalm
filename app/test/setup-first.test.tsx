// An install with no company yet: the engine answers api/state with its setup state, and the first company is added here.
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import type { CompaniesConfig, SetupState } from "@/lib/types";
import { at, deferred, fixture, fixtureAll, json, server, type Call, renderApp } from "./helpers";

const SETUP: SetupState = { setup: "needs_company", companies: [], live: false, can_manage_companies: true };
const config = (): CompaniesConfig => ({ companies: [], palettes: ["amber", "green"], limits: { key_pattern: "", reserved_keys: [], name_max: 100 } });
const found = { cui: "33333330", legal_name: "FIRMA NOUĂ SRL", address: "Str. Exemplu 3, Iași", vat_payer: true, inactive: false };
const added = { key: "NOUA", name: "Noua", legal_name: "FIRMA NOUĂ SRL", cui: "33333330", palette: "amber", has_ron_account: false };

/** The engine before and after the first company: setup until company-add, then the new company's state. `after` answers
 * the company's first read once it is added, `second` the second read of the company's state (the first is the setup). */
function install(after?: { promise: Promise<Response> }, second?: { promise: Promise<Response> }) {
  let ready = false;
  let reads = 0;
  let afterUsed = false;
  const s = { ...fixture(), co: "NOUA", companies: [{ key: "NOUA", name: "Noua", legal_name: "FIRMA NOUĂ SRL", palette: "amber", late: 0, logo: null }] };
  const api = server({
    state: () => {
      reads += 1;
      if (second && reads === 2) return second.promise;
      if (ready && after && !afterUsed) {
        afterUsed = true;
        return after.promise;
      }
      return json(ready ? s : SETUP);
    },
    all: () => json(ready ? { ...fixtureAll(), companies: s.companies, parts: [s] } : SETUP),
    companies: () => json({ ...config(), companies: ready ? [{ ...added, has_data: false, accounts: [], banks: [{ bank: "bt", name: "Banca Transilvania", connect: "not_set_up", connection: null }] }] : [] }),
    onPost: (c: Call) => {
      const action = c.url.split("/").pop();
      if (action === "company-lookup") return json({ ok: true, message: "", next: null, done: found, state: SETUP });
      if (action === "company-add") {
        ready = true;
        return json({ ok: true, message: "Firma a fost adăugată.", next: null, done: added, state: { ...fixtureAll(), companies: s.companies, parts: [s] } });
      }
      return json({ ok: true, message: "", next: null, done: { offers: [] }, state: null });
    },
  });
  return api;
}

async function addFirst() {
  await userEvent.type(await screen.findByLabelText("CUI"), "RO33333330");
  await userEvent.click(screen.getByRole("button", { name: "Caută la ANAF" }));
  await userEvent.click(await screen.findByRole("button", { name: "Adaugă firma" }));
  await waitFor(() => expect(window.location.search + window.location.hash).toBe("?co=NOUA"));
}

/** Acasă of the new firm, inside the app's frame. */
const onHome = () => waitFor(() => expect(document.querySelector("#main-content")).not.toBeNull());

beforeEach(() => localStorage.clear());

describe("the first company", () => {
  it("opens Acasă once the firm is added, asking nothing else", async () => {
    at("");
    const api = install();
    const app = render(<App />);
    expect(await screen.findByRole("heading", { name: "Prima firmă" })).toBeInTheDocument();
    await addFirst();
    await onHome();
    expect(api.posts().map((p) => [p.url.split("/").pop(), p.body])).toEqual([
      ["company-lookup", { cui: "RO33333330" }], ["company-add", { cui: "33333330" }],
    ]);
    app.unmount();
    render(<App />); // PWA/browser reopened at the URL left by setup
    await onHome();
    expect(screen.queryByRole("heading", { name: "Prima firmă" })).toBeNull();
  });

  it("shows a refused add where the form is, keeps the firm found, and reads the state again", async () => {
    at("");
    const REFUSED = "Prima firmă a fost deja adăugată.";
    const api = renderApp({ state: () => json(SETUP), all: () => json(SETUP), companies: () => json(config()),
      onPost: (c: Call) => (c.url.endsWith("company-lookup")
        ? json({ ok: true, message: "", next: null, done: found, state: SETUP })
        : json({ ok: false, message: REFUSED, next: null, done: null, state: null })) });
    await userEvent.type(await screen.findByLabelText("CUI"), "33333330");
    await userEvent.click(screen.getByRole("button", { name: "Caută la ANAF" }));
    const before = api.gets("ALL").length;
    await userEvent.click(await screen.findByRole("button", { name: "Adaugă firma" }));
    expect(await screen.findByText(REFUSED)).toHaveAttribute("role", "alert");
    expect(screen.getByRole("heading", { name: "Prima firmă" })).toBeInTheDocument();
    expect(screen.getByText("FIRMA NOUĂ SRL")).toBeInTheDocument();
    await waitFor(() => expect(api.gets("ALL").length).toBe(before + 1));
  });

  it("says when no answer came back", async () => {
    at("");
    renderApp({ state: () => json(SETUP), all: () => json(SETUP), companies: () => json(config()),
      onPost: () => { throw new TypeError("offline"); } });
    await userEvent.type(await screen.findByLabelText("CUI"), "33333330");
    await userEvent.click(screen.getByRole("button", { name: "Caută la ANAF" }));
    // A lookup saves nothing: said under the CUI, and asked again.
    expect(await screen.findByText("Căutarea nu s-a încheiat. Încearcă din nou.")).toHaveAttribute("role", "alert");
    expect(screen.getByRole("button", { name: "Încearcă din nou" })).toBeEnabled();
  });

  it("sends one request for two taps in the same moment", async () => {
    at("");
    const api = install();
    render(<App />);
    await userEvent.type(await screen.findByLabelText("CUI"), "33333330");
    const button = screen.getByRole("button", { name: "Caută la ANAF" });
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(await screen.findByRole("button", { name: "Adaugă firma" })).toBeInTheDocument();
    expect(api.posts().map((p) => p.url.split("/").pop())).toEqual(["company-lookup"]);
  });

  it("says the same refusal again when it comes back a second time", async () => {
    at("");
    const REFUSED = "CUI-ul nu este valid.";
    const second = deferred<Response>();
    let n = 0;
    renderApp({ state: () => json(SETUP), all: () => json(SETUP), companies: () => json(config()),
      onPost: () => (n++ === 0 ? json({ ok: false, message: REFUSED, next: null, done: null, state: null }) : second.promise) });
    await userEvent.type(await screen.findByLabelText("CUI"), "33333331");
    await userEvent.click(screen.getByRole("button", { name: "Caută la ANAF" }));
    await screen.findByText(REFUSED);
    fireEvent.submit(screen.getByRole("form", { name: "Caută firma" }));
    await waitFor(() => expect(screen.queryByText(REFUSED)).toBeNull()); // gone while the request is out, so it is read again
    second.resolve(json({ ok: false, message: REFUSED, next: null, done: null, state: null }));
    expect(await screen.findByText(REFUSED)).toHaveAttribute("role", "alert");
  });

  it("opens Acasă even when the URL already names the new firm", async () => {
    localStorage.setItem("fiscalm:co", "NOUA");
    at("?co=NOUA");
    install();
    render(<App />);
    await addFirst();
    await onHome();
  });

  it("leaves setup while the new firm's state is still on its way", async () => {
    at("?co=NOUA");
    const slow = deferred<Response>();
    install(slow);
    render(<App />);
    await addFirst();
    expect(screen.queryByRole("heading", { name: "Prima firmă" })).toBeNull();
    slow.resolve(json({ ...fixture(), co: "NOUA", companies: [{ key: "NOUA", name: "Noua", legal_name: "FIRMA NOUĂ SRL", palette: "amber", late: 0, logo: null }] }));
    await onHome();
  });

  it("drops a setup read sent before the firm was added", async () => {
    at("?co=NOUA");
    const stale = deferred<Response>(), slow = deferred<Response>();
    install(slow, stale);
    render(<App />);
    await screen.findByLabelText("CUI");
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    act(() => void document.dispatchEvent(new Event("visibilitychange")));
    await addFirst();
    await act(async () => stale.resolve(json(SETUP)));
    expect(screen.queryByRole("heading", { name: "Prima firmă" })).toBeNull();
    slow.resolve(json({ ...fixture(), co: "NOUA", companies: [{ key: "NOUA", name: "Noua", legal_name: "FIRMA NOUĂ SRL", palette: "amber", late: 0, logo: null }] }));
    await onHome();
  });

  it("isn't offered to someone who can't manage companies", async () => {
    at("");
    const api = renderApp({ state: () => json({ ...SETUP, can_manage_companies: false }), all: () => json({ ...SETUP, can_manage_companies: false }) });
    expect(await screen.findByText("Administratorul configurează încă această instalare.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ieși din cont" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Cont" })).toHaveAttribute("href", "?tab=setari&sec=cont");
    expect(screen.queryByLabelText("CUI")).toBeNull();
    expect(api.calls.some((c) => c.url.includes("api/companies"))).toBe(false);
  });

  it("is what a remembered company's link shows on an install that has none", async () => {
    at("?co=NORD&tab=cheltuieli");
    install();
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Prima firmă" })).toBeInTheDocument();
  });
});

it("keeps the created first company and retries its logo without adding it twice", async () => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:preview", revokeObjectURL: () => {} }));
  let attempts = 0;
  const upload = deferred<Response>();
  const api = server({ state: () => json(SETUP), companies: () => json(config()), onPost: c => {
    if (c.url.endsWith("company-lookup")) return json({ ok: true, done: found, state: SETUP });
    if (c.url.endsWith("company-add")) return json({ ok: true, done: added, state: null });
    if (c.url.includes("invoicing/logo?")) return ++attempts === 1 ? upload.promise : json({ ok: true, state: null });
    return json({ ok: true, state: null });
  } });
  render(<App />);
  await userEvent.type(await screen.findByLabelText("CUI"), "33333330{Enter}");
  await screen.findByRole("button", { name: "Adaugă firma" });
  const file = new File([new Uint8Array([137, 80, 78, 71])], "unrelated.png", { type: "image/png" });
  await userEvent.upload(screen.getByLabelText("Sigla firmei"), file);
  await screen.findByAltText("Sigla aleasă");
  await userEvent.click(screen.getByRole("button", { name: "Adaugă firma" }));
  await waitFor(() => expect(attempts).toBe(1));
  upload.resolve(json({ ok: false, message: "Sigla nu poate fi citită.", state: null }));
  await screen.findByText("Sigla nu poate fi citită.");
  expect(screen.getByLabelText("CUI")).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
  await waitFor(() => expect(attempts).toBe(2));
  expect(api.posts().filter(c => c.url.endsWith("company-add")).map(c => c.body)).toEqual([{ cui: "33333330" }]);
  expect(api.posts().filter(c => c.url.includes("invoicing/logo?"))).toEqual([
    expect.objectContaining({ url: "/web/api/invoicing/logo?co=NOUA", raw: file, type: "image/png" }),
    expect.objectContaining({ url: "/web/api/invoicing/logo?co=NOUA", raw: file, type: "image/png" }),
  ]);
});
