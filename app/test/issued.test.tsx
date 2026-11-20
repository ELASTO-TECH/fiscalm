// An invoice issued in fiscalm, in its sheet, against a fake engine: where it stands in SPV, its files, sending it
// again, recording what SPV shows when an upload's outcome is unknown, and a full storno that opens the new invoice.
import { act, screen, waitFor, within } from "@testing-library/react";
import { renderSignedIn as render, answer } from "./helpers";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/components/App";
import type { Issued, State } from "@/lib/types";
import { listLoaded, allWith, at, fixture, json, renderApp, server, detailResult, summaryResult, deferred } from "./helpers";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

/** NORD's invoices read from SPV only (the scenario's own fiscalm invoices are tested on their own below). */
function spvOnly(): State {
  const s = fixture();
  s.issued = s.issued.filter((r) => r.origin !== "fiscalm");
  return s;
}

/** NORD's state with its first issued invoice made in fiscalm, in the given SPV state. */
function withLocal(patch: Partial<Issued>, live = true): State {
  const s = spvOnly();
  s.live = live;
  s.issued[0] = { ...s.issued[0], origin: "fiscalm", local_id: "inv-1", pdf: true, can_storno: false, can_upload: false,
    storno_of: null, reversed_by: [], spv: { state: "accepted", errors: [], at: null }, ...patch };
  return s;
}

/** Before and after a full storno of the first invoice: after it, NRD 0045 reverses it. */
function stornoWorld() {
  const s = withLocal({ can_storno: true });
  const after = withLocal({ can_storno: false, reversed_by: ["inv-2"] });
  const storno: Issued = { ...after.issued[1], id: "local:inv-2", number: "NRD 0045", total: "-900.00", owed: "0.00",
    origin: "fiscalm", local_id: "inv-2", storno_of: "inv-1", reversed_by: [], can_storno: false, can_upload: false,
    pdf: true, spv: { state: "uploaded", errors: [], at: null } };
  after.issued.unshift(storno);
  return { s, after };
}

/** Opens the storno form, types a reason and issues it. */
async function storno(d: ReturnType<typeof within>) {
  await userEvent.click(d.getByRole("button", { name: "Stornează factura" }));
  await userEvent.type(d.getByLabelText("Motivul stornării"), "Comandă anulată");
  await userEvent.click(d.getByRole("button", { name: "Emite factura de stornare" }));
  await answer();
}

describe("Issued in fiscalm", () => {
  it.each([["open", "accepted", ""], ["credited", "accepted", ""], ["open", "rejected", "respinsă de ANAF Test"]] as const)(
    "an ANAF Test invoice (%s, %s) keeps its own state in the list, with no note of its own", async (status, spv, problem) => {
    const s = withLocal({ status, days_overdue: 0, spv: { state: spv, errors: [], at: null, environment: "test" } }, false);
    server({ state: () => json(s) });
    at("?co=NORD&tab=vanzari");
    render(<App />);
    const row = (await screen.findByText(s.issued[0].client)).closest(".row")!;
    expect(row).toHaveTextContent(status === "credited" ? "Stornată" : "De încasat");
    expect(!!row.querySelector(".late")).toBe(!!problem);
    expect(screen.queryByText(/valoare fiscală/)).toBeNull();
    if (problem) { expect(row).toHaveTextContent(problem); return; }
    await userEvent.click(row);
    expect(within(await screen.findByRole("dialog", { busy: false }))
      .getByText("Acceptată de ANAF Test")).toBeInTheDocument();
  });

  it("outside Vânzări an ANAF Test invoice says where it went: the month's report", async () => {
    const s = fixture();
    s.live = false;
    const r = s.issued.find((x) => x.number === "DEMO-1")!;
    r.spv = { state: "accepted", errors: [], at: null, environment: "test" };
    server({ state: () => json(s) });
    at("?co=NORD&tab=rapoarte&month=2026-10");
    render(<App />);
    expect((await screen.findByText("nr. DEMO-1")).closest(".row")).toHaveTextContent("acceptată de ANAF Test");
  });

  async function sheetFor(s: State) {
    at(`?co=NORD&tab=vanzari&iss=${s.issued[0].id}`);
    render(<App />);
    return within(await screen.findByRole("dialog", { busy: false }));
  }

  it("the engine's own invoices, as its scenario makes them: states, the list, and the storno pair linked both ways", async () => {
    server();
    at("?co=NORD&tab=vanzari");
    render(<App />);
    const row = async (n: string) => (await screen.findByText(`nr. ${n}`)).closest(".row")!;
    expect(await row("DEMO-3")).toHaveTextContent("Respinsă de ANAF");
    expect(await row("DEMO-1")).toHaveTextContent("121,00 lei");
    // Only the reversed invoice reads "Stornată"; the storno says what it is, and its sheet what it reverses.
    expect(await row("DEMO-5")).toHaveTextContent("Factură de stornare");
    expect(await row("DEMO-5")).not.toHaveTextContent("Stornată");
    expect(await row("DEMO-4")).toHaveTextContent("Stornată");
    await userEvent.click(await row("DEMO-5"));
    const storno = within(await screen.findByRole("dialog", { busy: false }));
    expect(storno.getByText("Factură de stornare pentru DEMO-4")).toBeInTheDocument();
    expect(storno.getByText("Se trimite în SPV pe 31 octombrie")).toBeInTheDocument();
    expect(storno.getByRole("button", { name: "Trimite în SPV" })).toBeInTheDocument();
    await userEvent.click(storno.getByRole("link", { name: "Factura DEMO-4" }));
    const original = within(await screen.findByRole("dialog", { busy: false }));
    await waitFor(() => expect(original.getByText("Stornată prin")).toBeInTheDocument());
    expect(original.getByRole("link", { name: "Factura DEMO-5" })).toBeInTheDocument();
  });

  it("shows SPV's answer and links its PDF and XML, and offers nothing it can't do", async () => {
    const s = withLocal({});
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    expect(d.getByText("Acceptată de ANAF")).toBeInTheDocument();
    expect(d.getByText("Emisă în fiscalm")).toBeInTheDocument();
    expect(d.getByRole("link", { name: "Descarcă PDF" })).toHaveAttribute("target", "_blank");
    expect(d.getByRole("link", { name: "Descarcă XML" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(d.queryByRole("button", { name: /Trimite/ })).toBeNull();
    expect(d.queryByRole("button", { name: "Stornează factura" })).toBeNull();
  });

  it("an invoice read from SPV has none of it", async () => {
    const s = spvOnly();
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    expect(d.getByText("e-Factura")).toBeInTheDocument();
    expect(d.queryByText("SPV")).toBeNull();
    expect(d.queryByRole("link", { name: /PDF/ })).toBeNull();
  });

  it("shows why SPV refused it, says so in the list, and sends it again", async () => {
    const s = withLocal({ rejected: true, owed: "0.00", can_upload: true,
      spv: { state: "rejected", errors: ["E001 CUI cumpărător invalid"], at: null } });
    const api = server({ state: () => json(s), onPost: () => json({ ok: true, message: "Factura a fost trimisă în SPV.", state: s }) });
    const d = await sheetFor(s);
    expect(d.getByText("Respinsă de ANAF")).toBeInTheDocument();
    expect(d.getByText("E001 CUI cumpărător invalid")).toBeInTheDocument();
    await userEvent.click(d.getByRole("button", { name: "Trimite din nou în SPV" }));
    expect(await answer()).toBe("Trimiți factura NRD 0044 în SPV?");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/invoicing/upload", method: "POST", body: { co: "NORD", id: "inv-1" } });
    expect(await screen.findByText("Factura a fost trimisă în SPV.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument(); // the sheet stays, with the new state
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Închide" }));
    expect((await screen.findByText(s.issued[0].client)).closest(".row")).toHaveTextContent("Respinsă de ANAF");
  });

  it("an invoice SPV refused isn't owed: the list shows its total, the sheet says it won't be collected", async () => {
    const s = withLocal({ rejected: true, owed: "0.00", can_mark: false, can_upload: true,
      spv: { state: "rejected", errors: [], at: null } });
    server({ state: () => json(s) });
    at("?co=NORD&tab=vanzari");
    render(<App />);
    const row = (await screen.findByText(s.issued[0].client)).closest(".row")!;
    expect(row).toHaveTextContent("Respinsă de ANAF");
    expect(row).toHaveTextContent("900,00 EUR");
    await userEvent.click(row);
    const d = within(await screen.findByRole("dialog", { busy: false }));
    expect(d.getByText("Respinsă de ANAF, nu se încasează")).toBeInTheDocument();
    expect(d.queryByRole("button", { name: /Marchează ca încasată|Numerar/ })).toBeNull();
  });

  it("an invoice SPV refused isn't under De încasat, but is under Toate, here and in its partner's sheet", async () => {
    const s = withLocal({ rejected: true, owed: "0.00", can_mark: false, spv: { state: "rejected", errors: [], at: null } });
    server({ state: () => json(s) });
    at("?co=NORD&tab=vanzari&f=open");
    render(<App />);
    await screen.findByText("Client Beta SRL");
    expect(screen.queryByText(s.issued[0].client)).toBeNull();
    await userEvent.click(screen.getByRole("link", { name: "Toate" }));
    expect(await screen.findByText(s.issued[0].client)).toBeInTheDocument();
  });

  it("an upload in flight too long reads as unknown, in the list and the sheet, with the form to settle it", async () => {
    const s = withLocal({ spv: { state: "uploading", errors: [], at: null }, can_resolve: true });
    server({ state: () => json(s) });
    at("?co=NORD&tab=vanzari");
    render(<App />);
    const row = (await screen.findByText(s.issued[0].client)).closest(".row")!;
    expect(row).toHaveTextContent("Rezultat SPV necunoscut");
    await userEvent.click(row);
    const d = within(await screen.findByRole("dialog", { busy: false }));
    expect(d.getByText("Rezultat necunoscut")).toBeInTheDocument();
    expect(d.queryByText("Se trimite")).toBeNull();
    expect(d.getByLabelText("Indexul de încărcare din SPV")).toBeInTheDocument();
  });

  it("an upload that failed says so, in the list and the sheet", async () => {
    const s = withLocal({ spv: { state: "error", errors: [], at: null }, can_upload: true });
    server({ state: () => json(s) });
    at("?co=NORD&tab=vanzari");
    render(<App />);
    const row = (await screen.findByText(s.issued[0].client)).closest(".row")!;
    expect(row).toHaveTextContent("Eroare la trimiterea în SPV");
    await userEvent.click(row);
    expect(within(await screen.findByRole("dialog", { busy: false })).getByText("Eroare la trimitere")).toBeInTheDocument();
  });

  it("a status this app doesn't know yet doesn't break the list", async () => {
    const s = withLocal({ status: "disputed" as never });
    server({ state: () => json(s) });
    at("?co=NORD&tab=vanzari");
    render(<App />);
    expect((await screen.findByText(s.issued[0].client)).closest(".row")).toHaveTextContent("disputed");
  });

  it("an invoice SPV doesn't need says so, offers no sending, and isn't flagged in the list", async () => {
    const s = withLocal({ spv: { state: "not_required", errors: [], at: null }, can_upload: false });
    server({ state: () => json(s) });
    at("?co=NORD&tab=vanzari");
    render(<App />);
    const row = (await screen.findByText(s.issued[0].client)).closest(".row")!;
    expect(row).not.toHaveTextContent(/SPV/);
    await userEvent.click(row);
    const d = within(await screen.findByRole("dialog", { busy: false }));
    expect(d.getByText("Trimitere neobligatorie")).toBeInTheDocument();
    expect(d.queryByRole("button", { name: /Trimite/ })).toBeNull();
  });

  it("an optional invoice that may still go to SPV offers sending it, not sending it again", async () => {
    const s = withLocal({ spv: { state: "not_required", errors: [], at: null }, can_upload: true });
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    expect(d.getByRole("button", { name: "Trimite în SPV" })).toBeInTheDocument();
    expect(d.queryByRole("button", { name: "Trimite din nou în SPV" })).toBeNull();
  });

  it("an invoice not sent yet says the day it goes to SPV on its own, and can still be sent now", async () => {
    const s = withLocal({ spv: { state: "none", errors: [], at: null, upload_on: "2026-11-04" }, can_upload: true });
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    expect(d.getByText("Se trimite în SPV pe 4 noiembrie")).toBeInTheDocument();
    expect(d.getByRole("button", { name: "Trimite în SPV" })).toBeInTheDocument();
  });

  it("before going live, nothing is offered: the engine says so through can_upload, can_resolve and can_storno", async () => {
    const s = withLocal({ can_upload: false, can_resolve: false, can_storno: false, spv: { state: "none", errors: [], at: null } }, false);
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    expect(d.getByText("Netrimisă")).toBeInTheDocument();
    expect(d.queryByRole("button", { name: /Trimite|Stornează|Marchează ca netrimisă/ })).toBeNull();
    expect(d.queryByLabelText("Indexul de încărcare din SPV")).toBeNull();
  });

  it("without an ANAF connection, sending isn't offered: the sheet leads to connecting ANAF", async () => {
    const s = withLocal({ can_upload: false, spv: { state: "none", errors: [], at: null } });
    s.system.anaf = { ...s.system.anaf, status: "not_set_up", authorized: false };
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    expect(d.queryByRole("button", { name: /Trimite/ })).toBeNull();
    expect(d.getByRole("link", { name: "Conectează ANAF pentru a trimite" })).toHaveAttribute("href", expect.stringContaining("sec=integrari"));
  });

  it("offers to record what SPV shows only when the engine says it may, never from the state alone", async () => {
    const s = withLocal({ spv: { state: "uploading", errors: [], at: null }, can_resolve: false });
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    expect(d.getByText("Se trimite")).toBeInTheDocument();
    expect(d.queryByLabelText("Indexul de încărcare din SPV")).toBeNull(); // still in flight
  });

  it("a state it doesn't know yet and a long list of reasons don't break the sheet", async () => {
    const s = withLocal({ spv: { state: "queued" as never, errors: Array.from({ length: 30 }, (_, i) => `Eroare ${i}`), at: null } });
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    expect(d.getByText("Stare necunoscută")).toBeInTheDocument();
    expect(d.getAllByText(/^Eroare \d+$/)).toHaveLength(10);
  });

  it("an unknown outcome is settled by what SPV shows, never by sending blindly", async () => {
    const s = withLocal({ spv: { state: "unknown", errors: [], at: null }, can_resolve: true });
    const api = server({ state: () => json(s), onPost: () => json({ ok: true, message: "Am notat indexul.", state: s }) });
    const d = await sheetFor(s);
    expect(d.getByText("Rezultat necunoscut")).toBeInTheDocument();
    expect(d.queryByRole("button", { name: /Trimite/ })).toBeNull();
    const save = d.getByRole("button", { name: "Salvează indexul" });
    await userEvent.type(d.getByLabelText("Indexul de încărcare din SPV"), "12ab");
    expect(save).toBeDisabled(); // digits only
    await userEvent.clear(d.getByLabelText("Indexul de încărcare din SPV"));
    await userEvent.type(d.getByLabelText("Indexul de încărcare din SPV"), " 4200001 ");
    await userEvent.click(save);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/invoicing/resolve-upload", method: "POST",
      body: { co: "NORD", id: "inv-1", checked_in_spv: true, upload_id: "4200001" } });
  });

  it("with ANAF connected, an unknown outcome is looked up in SPV by fiscalm", async () => {
    const s = withLocal({ spv: { state: "unknown", errors: [], at: null }, can_resolve: true });
    s.system.anaf.status = "ok";
    const api = server({ state: () => json(s), onPost: () => json({ ok: true, message: "Factura a fost găsită în SPV.", state: s }) });
    await userEvent.click((await sheetFor(s)).getByRole("button", { name: "Caută în SPV" }));
    await waitFor(() => expect(api.posts()).toEqual([{ url: "/web/api/invoicing/recover-upload", method: "POST", body: { co: "NORD", id: "inv-1" } }]));
  });

  it("'marked as not sent' asks first and sends upload_id null", async () => {
    const s = withLocal({ spv: { state: "unknown", errors: [], at: null }, can_resolve: true });
    const api = server({ state: () => json(s), onPost: () => json({ ok: true, message: "Gata.", state: s }) });
    const d = await sheetFor(s);
    await userEvent.click(d.getByRole("button", { name: "Marchează ca netrimisă" }));
    await answer(false);
    expect(api.posts()).toHaveLength(0); // declined
    await userEvent.click(d.getByRole("button", { name: "Marchează ca netrimisă" }));
    await answer();
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "inv-1", checked_in_spv: true, upload_id: null });
  });

  it("a storno takes a reason, asks first, and opens the new invoice, linked both ways", async () => {
    const { s, after } = stornoWorld();
    const api = server({ state: () => json(s), onPost: () => json({ ok: true, message: "Am emis factura de stornare NRD 0045.",
      id: "inv-2", state: after }) });
    const d = await sheetFor(s);
    await userEvent.click(d.getByRole("button", { name: "Stornează factura" }));
    const emit = d.getByRole("button", { name: "Emite factura de stornare" });
    expect(emit).toBeDisabled(); // a reason first
    await userEvent.type(d.getByLabelText("Motivul stornării"), "Comandă anulată");
    await userEvent.click(emit);
    expect(await answer()).toBe("Stornezi factura NRD 0044? Se emite o factură de stornare pentru toate liniile ei.");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/invoicing/storno", method: "POST",
      body: { co: "NORD", id: "inv-1", reason: "Comandă anulată" } });
    await waitFor(() => expect(window.location.search).toContain("iss=local%3Ainv-2"));
    const made = within(await screen.findByRole("dialog", { busy: false }));
    expect(made.getByText("Trimisă, se așteaptă răspunsul ANAF")).toBeInTheDocument();
    await userEvent.click(made.getByRole("link", { name: "Factura NRD 0044" }));
    const back = within(await screen.findByRole("dialog", { busy: false }));
    await waitFor(() => expect(back.getByText("Stornată prin")).toBeInTheDocument());
    expect(back.getByRole("link", { name: "Factura NRD 0045" })).toBeInTheDocument();
  });
  it("under Toate firmele, the new storno opens once the re-read has it", async () => {
    const { s, after } = stornoWorld();
    let now = allWith(s);
    const api = server({ all: () => json(now), onPost: () => {
      now = allWith(after);
      return json({ ok: true, message: "Am emis factura de stornare NRD 0045.", id: "inv-2", state: after });
    } });
    at(`?co=ALL&tab=vanzari&iss=${s.issued[0].id}`);
    render(<App />);
    await storno(within(await screen.findByRole("dialog", { busy: false })));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await waitFor(() => expect(window.location.search).toContain("iss=local%3Ainv-2"));
    expect(window.location.search).toContain("co=ALL");
    expect(within(await screen.findByRole("dialog", { busy: false })).getByText("Trimisă, se așteaptă răspunsul ANAF")).toBeInTheDocument();
  });

  it("under Toate firmele, if the re-read fails, the new storno opens in its company's view", async () => {
    const { s, after } = stornoWorld();
    let posted = false;
    server({ all: () => (posted ? json({ ok: false }, 500) : json(allWith(s))), state: () => json(after), onPost: () => {
      posted = true;
      return json({ ok: true, message: "Am emis factura de stornare NRD 0045.", id: "inv-2", state: after });
    } });
    at(`?co=ALL&tab=vanzari&iss=${s.issued[0].id}`);
    render(<App />);
    await storno(within(await screen.findByRole("dialog", { busy: false })));
    await waitFor(() => expect(window.location.search).toContain("co=NORD"));
    expect(window.location.search).toContain("iss=local%3Ainv-2");
    expect(within(await screen.findByRole("dialog", { busy: false })).getByText("Trimisă, se așteaptă răspunsul ANAF")).toBeInTheDocument();
  });

  it("issued but its state didn't come back: the form closes and the new storno opens after a re-read", async () => {
    const { s, after } = stornoWorld();
    let now = s;
    server({ state: () => json(now), detail: (url) => {
      const result = detailResult(now, Object.fromEntries(new URL(url, "http://local").searchParams));
      return json(result, result ? 200 : 404);
    }, onPost: () => {
      now = after;
      return json({ ok: true, message: "Am emis factura de stornare NRD 0045.", id: "inv-2", state: null, invoicing: null });
    } });
    const d = await sheetFor(s);
    await storno(d);
    await waitFor(() => expect(window.location.search).toContain("iss=local%3Ainv-2"));
    const made = within(await screen.findByRole("dialog", { busy: false }));
    expect(made.queryByRole("button", { name: "Emite factura de stornare" })).toBeNull();
    expect(made.getByText("Stornare pentru")).toBeInTheDocument();
  });

  it("follows the canonical storno id when both root and POST carry only summaries", async () => {
    const { s, after } = stornoWorld(); let now = s;
    at(`?co=NORD&tab=vanzari&iss=${s.issued[0].id}`);
    const api = renderApp({ state: () => json(summaryResult(now)),
      detail: (url) => json(detailResult(now, Object.fromEntries(new URL(url, "http://local").searchParams))),
      onPost: () => { now = after; return json({ ok: true, message: "Factura a fost stornată.", id: "inv-2", state: summaryResult(after) }); } });
    await storno(within(await screen.findByRole("dialog", { busy: false })));
    await waitFor(() => expect(window.location.search).toContain("iss=local%3Ainv-2"));
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "inv-1", reason: "Comandă anulată" });
    expect(within(await screen.findByRole("dialog", { busy: false })).getByText("Stornare pentru")).toBeInTheDocument();
  });

  it("does not reopen the invoice after closing while its canonical detail is pending", async () => {
    const { s, after } = stornoWorld(), late = deferred<Response>(); let now = s;
    at(`?co=NORD&tab=vanzari&iss=${s.issued[0].id}`);
    const api = renderApp({ state: () => json(summaryResult(now)), detail: (url) => {
      const query = Object.fromEntries(new URL(url, "http://local").searchParams);
      return query.id === "inv-2" ? late.promise : json(detailResult(now, query));
    }, onPost: () => { now = after; return json({ ok: true, id: "inv-2", state: summaryResult(after) }); } });
    await storno(within(await screen.findByRole("dialog", { busy: false })));
    await waitFor(() => expect(api.calls.some((c) => c.url.includes("kind=issued&id=inv-2"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Închide" }));
    await act(async () => late.resolve(json(detailResult(after, { kind: "issued", id: "inv-2" }))));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(new URL(location.href).searchParams.has("iss")).toBe(false);
  });

  it("a refused storno keeps the form and the reason", async () => {
    const { s } = stornoWorld();
    server({ state: () => json(s), onPost: () => json({ ok: false, message: "Factura a fost deja stornată.", state: s }) });
    const d = await sheetFor(s);
    await storno(d);
    expect(await screen.findByText("Factura a fost deja stornată.")).toBeInTheDocument();
    expect(d.getByLabelText("Motivul stornării")).toHaveValue("Comandă anulată");
    expect(window.location.search).toContain(`iss=${s.issued[0].id}`);
  });

  it("closing the storno form puts the focus back on its button", async () => {
    const { s } = stornoWorld();
    server({ state: () => json(s) });
    const d = await sheetFor(s);
    await userEvent.click(d.getByRole("button", { name: "Stornează factura" }));
    expect(d.getByLabelText("Motivul stornării")).toHaveFocus();
    await userEvent.click(d.getByRole("button", { name: "Renunță" }));
    expect(d.getByRole("button", { name: "Stornează factura" })).toHaveFocus();
  });

  it("a link made with fiscalm's id still opens the invoice after sync gave it SPV's id", async () => {
    const s = withLocal({});
    server({ state: () => json(s) });
    at("?co=NORD&tab=vanzari&iss=inv-1");
    render(<App />);
    expect(within(await screen.findByRole("dialog", { busy: false })).getByText("Acceptată de ANAF")).toBeInTheDocument();
  });
});

describe("Collection marking", () => {
  it("offers OP, Numerar, Card and Altele, in that order, and says what proof closes each", async () => {
    at("?co=NORD&tab=vanzari");
    const api = server();
    render(<App />);
    await userEvent.click(await screen.findByRole("link", { name: /Client Alfa SRL.*nr\. NRD 0042/ }));
    const sheet = within(await screen.findByRole("dialog", { busy: false }));
    const group = within(sheet.getByRole("group", { name: "Cum a fost încasată?" }));
    expect(group.getAllByRole("radio").map((r) => r.closest("label")?.querySelector(".font-semibold")?.textContent)).toEqual(["Ordin de plată (OP)", "Numerar", "Card", "Altă metodă"]);
    const mark = sheet.getByRole("button", { name: "Marchează ca încasată" });
    expect(mark).toBeDisabled(); // no way is chosen for the person
    await userEvent.click(group.getByRole("radio", { name: /^Card/ }));
    expect(sheet.getByText("Rămâne în curs de confirmare până apar banii în extras.")).toBeInTheDocument();
    await userEvent.click(mark);
    await waitFor(() => expect(api.posts()).toEqual([{ url: "/web/api/issued-mark", method: "POST", body: { co: "NORD", id: "5001", method: "card", detail: "" } }]));
  });

  it("keeps the sent way while the mark is on its way", async () => {
    at("?co=NORD&tab=vanzari&iss=5001");
    const reply = deferred<Response>();
    const api = server({ onPost: () => reply.promise });
    render(<App />);
    const sheet = within(await screen.findByRole("dialog", { busy: false }));
    await userEvent.click(sheet.getByRole("radio", { name: /^Card/ }));
    await userEvent.click(sheet.getByRole("button", { name: "Marchează ca încasată" }));
    await waitFor(() => expect(sheet.getByRole("radio", { name: /^Ordin de plată/ })).toBeDisabled());
    expect(sheet.getByRole("radio", { name: /^Card/ })).toBeChecked();
    expect(api.posts()[0].body).toEqual({ co: "NORD", id: "5001", method: "card", detail: "" });
    reply.resolve(json({ ok: true, message: "", state: fixture() }));
  });

  it("a fresh invoice leads with its PDF; collecting it waits behind Marchează ca încasată…", async () => {
    const s = withLocal({ status: "open", due_date: "2026-11-26", days_overdue: 0, can_mark: true });
    const api = server({ state: () => json(s) });
    at(`?co=NORD&tab=vanzari&iss=${s.issued[0].id}`);
    render(<App />);
    const sheet = await screen.findByRole("dialog", { busy: false });
    const d = within(sheet);
    const links = d.getAllByRole("link").map((a) => a.textContent);
    expect(links.filter((t) => /PDF|XML/.test(t ?? ""))).toEqual(["Descarcă PDF", "Descarcă XML"]); // no second copy below
    expect(sheet.querySelector("a.btn, button.btn")).toHaveTextContent("Descarcă PDF");
    expect(d.queryByRole("radio", { name: /^Ordin de plată/ })).toBeNull();
    await userEvent.click(d.getByRole("button", { name: "Marchează ca încasată…" }));
    expect(d.getByRole("group", { name: "Cum a fost încasată?" })).toHaveFocus();
    await userEvent.click(d.getByRole("radio", { name: /^Ordin de plată/ }));
    await userEvent.click(d.getByRole("button", { name: "Marchează ca încasată" }));
    await waitFor(() => expect(api.posts()).toEqual([{ url: "/web/api/issued-mark", method: "POST", body: { co: "NORD", id: s.issued[0].id, method: "bank", detail: "" } }]));
  });

  it("marks an issued invoice as collected with a note, and undoes it", async () => {
    at("?co=NORD&tab=vanzari");
    const api = server();
    render(<App />);
    expect((await screen.findByText("Client Zeta SRL")).closest(".row")).toHaveTextContent("Stornată");
    expect(screen.getByText("Client Delta SRL").closest(".row")).toHaveTextContent("Încasată, 15 sep"); // the credit's booking day, not the day it was recorded
    expect(screen.getByText("Client Epsilon SRL").closest(".row")).toHaveTextContent("În curs de confirmare");
    await userEvent.click(screen.getByRole("link", { name: /Client Alfa SRL.*nr\. NRD 0042/ }));
    const sheet = await screen.findByRole("dialog", { busy: false });
    expect(sheet).toHaveTextContent("Rest de încasat4.800,00 lei");
    // The state comes right under the amount, before the actions and the facts.
    const text = sheet.textContent ?? "";
    expect(text.indexOf("De încasat")).toBeLessThan(text.indexOf("Cum a fost încasată?"));
    expect(text.indexOf("Cum a fost încasată?")).toBeLessThan(text.indexOf("Număr"));
    await userEvent.click(within(sheet).getByRole("radio", { name: /^Altă metodă/ })); // only another way needs a note
    expect(within(sheet).getByLabelText("Cum a fost încasată")).toHaveFocus();
    const mark = within(sheet).getByRole("button", { name: "Marchează ca încasată" });
    expect(mark).toBeDisabled();
    await userEvent.type(within(sheet).getByLabelText("Cum a fost încasată"), "Cec");
    await userEvent.click(mark);
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]).toEqual({ url: "/web/api/issued-mark", method: "POST", body: { co: "NORD", id: "5001", method: "other", detail: "Cec" } });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await userEvent.click(screen.getByText("Client Epsilon SRL"));
    const marked = await screen.findByRole("dialog", { busy: false });
    expect(marked).toHaveTextContent("ÎncasareAltă metodă: compensare cu factura lor");
    expect(marked).toHaveTextContent("Rămâne în curs de confirmare până apare dovada încasării.");
    await userEvent.click(within(marked).getByRole("button", { name: "Anulează marcarea" }));
    await waitFor(() => expect(api.posts()[1].body).toEqual({ co: "NORD", id: "5005" }));
  });
});


it("pages large sales lists, searches beyond the page and opens the original sheet link", async () => {
  const s = fixture();
  s.issued = Array.from({ length: 151 }, (_, i) => ({ ...s.issued[0], id: `page-${i}`, number: `SYN${String(i).padStart(5, "0")}` }));
  at("?co=NORD&tab=vanzari");
  renderApp({ state: () => json(s) });
  let next = await screen.findByRole("button", { name: "Pagina următoare" });
  const rows = () => document.querySelectorAll('a[href*="iss=page-"]');
  expect(rows()).toHaveLength(50);
  expect(rows()[0]).toHaveAttribute("href", "?co=NORD&tab=vanzari&iss=page-0");
  await userEvent.click(next);
  await listLoaded();
  expect(rows()).toHaveLength(50);
  expect(rows()[0]).toHaveAttribute("href", "?co=NORD&tab=vanzari&iss=page-50");
  const search = screen.getByRole("searchbox", { name: "Caută firmă sau număr" });
  await userEvent.type(search, "SYN");
  await listLoaded();
  expect(rows()[0]).toHaveAttribute("href", "?co=NORD&tab=vanzari&iss=page-0");
  await userEvent.clear(search);
  await listLoaded();
  next = screen.getByRole("button", { name: "Pagina următoare" });
  expect(rows()[0]).toHaveAttribute("href", "?co=NORD&tab=vanzari&iss=page-0");
  for (let i = 0; i < 3; i++) { await userEvent.click(next); await listLoaded(); }
  expect(next).toHaveAttribute("aria-disabled", "true");
  expect(next).not.toBeDisabled();
  expect(next).toHaveFocus();
  await userEvent.keyboard("{Enter}");
  expect(rows()).toHaveLength(1);
  const previous = screen.getByRole("button", { name: "Pagina anterioară" });
  for (let i = 0; i < 3; i++) { await userEvent.click(previous); await listLoaded(); }
  expect(previous).toHaveAttribute("aria-disabled", "true");
  expect(previous).toHaveFocus();
  await userEvent.type(search, "SYN00150");
  await listLoaded();
  expect(rows()).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Pagina următoare" })).toBeNull();
  expect(rows()[0]).toHaveAttribute("href", "?co=NORD&tab=vanzari&iss=page-150");
  await userEvent.click(rows()[0]);
  expect(await screen.findByRole("dialog", { busy: false })).toHaveTextContent("SYN00150");
});

describe("invoices from before fiscalm", () => {
  it("are listed by date in Vânzări and Cheltuieli with no payment state, a PDF one opens its document", async () => {
    server({});
    at("?co=NORD&tab=vanzari");
    render(<App />);
    const sold = (await screen.findAllByText("Depozit Exemplu SRL")).map((t) => t.closest(".row")!);
    expect(sold.map((r) => r.textContent)).toEqual([expect.stringMatching(/^Depozit Exemplu SRLDin istoric, 1[23] sep611,20 leinr\. NRD 0035$/),
      expect.stringMatching(/^Depozit Exemplu SRLDin istoric, \d+ aug2\.734,55 leinr\. NRD 0031$/)]);
    expect(sold[0].tagName).toBe("DIV"); // read from XML: nothing to open, nothing to act on
    await userEvent.click(screen.getByRole("link", { name: "De încasat" }));
    await screen.findByText("Client Beta SRL");
    expect(screen.queryByText("Depozit Exemplu SRL")).toBeNull();
    await userEvent.click(screen.getByRole("link", { name: /^Cheltuieli/ }));
    const pdf = (await screen.findByText("Mobilier Vechi SRL")).closest("a")!;
    expect(pdf).toHaveTextContent("Din istoric");
    expect(pdf.getAttribute("href")).toMatch(/history-document\?co=NORD&sha=[0-9a-f]{64}$/);
  });
});
