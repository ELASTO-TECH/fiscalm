// The logo on a company's invoices, in Factură nouă › Modifică datele firmei, against a fake engine: the image's bytes
// sent as they are, what is plainly not a PNG or JPEG or too heavy stopped before sending, removal after asking.
import { screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import type { Invoicing } from "@/lib/types";
import { answer, at, fixture, invoicingData, json, server, type Call, renderApp } from "./helpers";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

const withLogo = (): Invoicing => {
  const inv = invoicingData();
  inv.seller = { ...inv.seller!, logo: { sha: "a".repeat(64), type: "image/png", width: 336, height: 96 } };
  return inv;
};

async function sellerForm() {
  render(<App />);
  const d = within(await screen.findByRole("dialog", { name: /Factură nouă/ }));
  await userEvent.click(await d.findByRole("button", { name: "Modifică datele firmei de pe factură" }));
  return within(await screen.findByRole("group", { name: "Sigla pe factură" }));
}

const jpeg = (type: string) => new File([new Uint8Array([0xff, 0xd8, 0xff, 0xe0])], "sigla.jpg", { type });

const png = (size = 2000) => {
  const f = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "sigla.png", { type: "image/png" });
  Object.defineProperty(f, "size", { value: size });
  return f;
};

describe("The logo on invoices", () => {
  it("without one, says the company's name is printed instead, and uploads the image as it is", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    let now = invoicingData(); // what the engine holds, as it changes (the app reads it again after each action)
    const api = server({ invoicing: () => json(now), onPost: () => {
      now = withLogo();
      return json({ ok: true, message: "Sigla a fost încărcată.", invoicing: now, id: null, state: fixture() });
    } });
    const g = await sellerForm();
    expect(g.getByText("Fără siglă, pe factură apare numele firmei.")).toBeInTheDocument();
    const file = png();
    await userEvent.upload(document.querySelector("fieldset.logo input[type=file]") as HTMLInputElement, file);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    const c = api.posts()[0] as Call;
    expect(c.url).toBe("/web/api/invoicing/logo?co=NORD");
    expect(c.type).toBe("image/png");
    expect(c.raw).toBe(file); // the bytes as chosen, not re-encoded
    expect(await screen.findByText("Sigla a fost încărcată.")).toBeInTheDocument();
    const img = await g.findByRole("img", { name: "Sigla actuală" });
    expect(img).toHaveAttribute("src", `/web/invoicing/logo?co=NORD&sha=${"a".repeat(64)}`);
    expect(img).toHaveAttribute("width", "168"); // 336x96 fitted in 168x48, as on the invoice
    expect(g.getByRole("button", { name: "Înlocuiește sigla" })).toBeInTheDocument();
  });

  it("stops what is plainly not a PNG or JPEG, or too heavy, before sending it", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const api = server();
    const g = await sellerForm();
    const input = document.querySelector("fieldset.logo input[type=file]") as HTMLInputElement;
    await userEvent.upload(input, png(600 * 1024));
    expect(g.getByRole("alert")).toHaveTextContent("Imaginea poate avea cel mult 512 KB.");
    const webp = new File([new Uint8Array([0x52, 0x49, 0x46, 0x46])], "sigla.webp", { type: "image/webp" });
    await userEvent.upload(input, webp, { applyAccept: false });
    expect(g.getByRole("alert")).toHaveTextContent("Se acceptă o imagine PNG sau JPEG.");
    expect(api.posts()).toHaveLength(0);
  });

  it("a refusal from the engine is shown and the old logo stays", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    server({ invoicing: () => json(withLogo()),
      onPost: () => json({ ok: false, message: "Imaginea trebuie să aibă între 32 și 1200 de pixeli pe fiecare latură.", invoicing: withLogo(), id: null, state: null }) });
    const g = await sellerForm();
    await userEvent.upload(document.querySelector("fieldset.logo input[type=file]") as HTMLInputElement, png());
    // in the toast and next to the control
    await waitFor(() => expect(screen.getAllByText("Imaginea trebuie să aibă între 32 și 1200 de pixeli pe fiecare latură.")).toHaveLength(2));
    expect(g.getByRole("img", { name: "Sigla actuală" })).toBeInTheDocument();
  });

  it("removes the logo after asking", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    let now = withLogo();
    const api = server({ invoicing: () => json(now), onPost: () => {
      now = invoicingData();
      return json({ ok: true, message: "Sigla a fost eliminată.", invoicing: now, id: null, state: fixture() });
    } });
    const g = await sellerForm();
    await userEvent.click(g.getByRole("button", { name: "Elimină sigla" }));
    await answer(false);
    expect(api.posts()).toHaveLength(0); // declined
    await userEvent.click(g.getByRole("button", { name: "Elimină sigla" }));
    expect(await answer()).toBe("Elimini sigla? Facturile noi vor avea numele firmei în locul ei.");
    await waitFor(() => expect(api.posts()[0]).toEqual(expect.objectContaining({ url: "/web/api/invoicing/logo-remove", body: { co: "NORD" } })));
    expect(await g.findByText("Fără siglă, pe factură apare numele firmei.")).toBeInTheDocument();
  });

  it("a logo whose file can't be read says so, and can be replaced or removed", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const inv = invoicingData();
    inv.seller = { ...inv.seller!, logo: null, logo_unavailable: true };
    server({ invoicing: () => json(inv) });
    const g = await sellerForm();
    expect(g.getByRole("alert")).toHaveTextContent("Sigla nu mai poate fi citită.");
    expect(g.queryByRole("img")).toBeNull();
    expect(g.getByRole("button", { name: "Înlocuiește sigla" })).toBeInTheDocument();
    expect(g.getByRole("button", { name: "Elimină sigla" })).toBeInTheDocument();
  });

  it("a JPEG the browser gives no type, or image/jpg, is still sent as a JPEG", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const api = server({ onPost: () => json({ ok: true, message: "Sigla a fost încărcată.", invoicing: invoicingData(), id: null, state: fixture() }) });
    await sellerForm();
    const input = document.querySelector("fieldset.logo input[type=file]") as HTMLInputElement;
    await userEvent.upload(input, jpeg(""), { applyAccept: false });
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await userEvent.upload(input, jpeg("image/jpg"), { applyAccept: false });
    await waitFor(() => expect(api.posts()).toHaveLength(2));
    expect(api.posts().map((c) => (c as Call).type)).toEqual(["image/jpeg", "image/jpeg"]);
  });

  it("saving the company's details sends only what the engine takes, not the logo it answered with", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const api = renderApp({ invoicing: () => json(withLogo()), onPost: () => json({ ok: true, message: "", invoicing: withLogo(), id: null, state: fixture() }) });
    const d = within(await screen.findByRole("dialog", { name: /Factură nouă/ }));
    await userEvent.click(await d.findByRole("button", { name: "Modifică datele firmei de pe factură" }));
    await userEvent.click(d.getByRole("button", { name: "Salvează datele firmei" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    const seller = (api.posts()[0].body as { seller: Record<string, unknown> }).seller;
    expect(Object.keys(seller).sort()).toEqual(["address", "bank", "cui", "email", "iban", "legal_name", "phone", "reg_com", "vat_id", "vat_payer"]);
  });

  it("while an upload runs, the buttons wait; with no answer, the app reads its data again and shows the logo that landed", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    let answer!: () => void;
    let now = invoicingData();
    server({ invoicing: () => json(now), onPost: () => new Promise<Response>((_, fail) => (answer = () => fail(new TypeError("reset")))) });
    const g = await sellerForm();
    await userEvent.upload(document.querySelector("fieldset.logo input[type=file]") as HTMLInputElement, png());
    await waitFor(() => expect(g.getByRole("button", { name: "Încarcă o siglă" })).toBeDisabled());
    now = withLogo(); // it landed, but the answer was lost
    answer();
    expect(await g.findByRole("img", { name: "Sigla actuală" })).toBeInTheDocument();
  });

  it("after removing it, focus goes to the upload button", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    let now = withLogo();
    server({ invoicing: () => json(now), onPost: () => {
      now = invoicingData();
      return json({ ok: true, message: "Sigla a fost eliminată.", invoicing: now, id: null, state: fixture() });
    } });
    const g = await sellerForm();
    await userEvent.click(g.getByRole("button", { name: "Elimină sigla" }));
    await answer();
    await waitFor(() => expect(g.getByRole("button", { name: "Încarcă o siglă" })).toHaveFocus());
  });

  it("is its own section, outside the details form, described for screen readers", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    server();
    const g = await sellerForm();
    const group = screen.getByRole("group", { name: "Sigla pe factură" });
    expect(group.closest("form")).toBeNull(); // saved at once; the form's Renunță doesn't undo it
    expect(g.getByRole("button", { name: "Încarcă o siglă" })).toHaveAccessibleDescription(/PNG sau JPEG, cel mult 512 KB/);
  });

  it("a small logo is shown enlarged to the box, as the invoice prints it", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const inv = invoicingData();
    inv.seller = { ...inv.seller!, logo: { sha: "b".repeat(64), type: "image/png", width: 64, height: 32 } };
    server({ invoicing: () => json(inv) });
    const g = await sellerForm();
    const img = g.getByRole("img", { name: "Sigla actuală" });
    expect(img).toHaveAttribute("width", "96");
    expect(img).toHaveAttribute("height", "48");
  });

  it("the engine's refusal shows next to the control, a later success clears it, and focus comes back", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    let refuse = true;
    let now = invoicingData();
    server({ invoicing: () => json(now), onPost: () => {
      if (refuse) return json({ ok: false, message: "Imaginea este rotită prin metadate.", invoicing: now, id: null, state: null });
      now = withLogo();
      return json({ ok: true, message: "Sigla a fost încărcată.", invoicing: now, id: null, state: fixture() });
    } });
    const g = await sellerForm();
    const input = () => document.querySelector("fieldset.logo input[type=file]") as HTMLInputElement;
    await userEvent.upload(input(), png());
    expect(await g.findByRole("alert")).toHaveTextContent("Imaginea este rotită prin metadate.");
    await waitFor(() => expect(g.getByRole("button", { name: "Încarcă o siglă" })).toHaveFocus());
    refuse = false;
    await userEvent.upload(input(), png());
    expect(await g.findByRole("img", { name: "Sigla actuală" })).toBeInTheDocument();
    expect(g.queryByRole("alert")).toBeNull();
  });

  it("works before the install goes live (configuration)", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    const s = fixture();
    s.live = false;
    const api = server({ state: () => json(s), onPost: () => json({ ok: true, message: "Sigla a fost încărcată.", invoicing: withLogo(), id: null, state: s }) });
    await sellerForm();
    await userEvent.upload(document.querySelector("fieldset.logo input[type=file]") as HTMLInputElement, png());
    await waitFor(() => expect(api.posts()).toHaveLength(1));
  });

  it("isn't offered before the company's details exist", async () => {
    at("?co=NORD&tab=vanzari&new=1");
    renderApp({ invoicing: () => json({ ...invoicingData(), seller: null, series: [] }) });
    await screen.findByRole("heading", { name: "Completează pentru facturare" });
    expect(screen.queryByRole("group", { name: "Sigla pe factură" })).toBeNull();
  });
});
