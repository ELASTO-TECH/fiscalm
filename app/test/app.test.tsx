import { act, fireEvent, render as renderPlain, screen, waitFor, within } from "@testing-library/react";
import { createElement } from "react";
import { renderSignedIn as render } from "./helpers";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fixtureBrowseData, fixtureSummaryData } from "@/lib/fixtures";
import { EMPTY } from "@/components/ui";
import { getBrowse, getBootstrap, NO_ANSWER, READ_RETRY_DELAY, READ_TIMEOUT, SESSION_CHANGED } from "@/lib/api";
import type { State } from "@/lib/types";
import App from "@/components/App";
import { IdentityContext } from "@/components/context";
import { companiesConfig, summaryResult, becomeVisible, fixtureIdentity, answer, at, deferred, fixture, fixtureAll, json, payable, paymentFile, paymentFileDetail, server, renderApp, taskState, allWith, markPaidAs } from "./helpers";

beforeEach(() => {
  localStorage.clear();
  at("?co=NORD");
});

const total = (t: string) => {
  const s = fixture();
  s.payable.total = t;
  return s;
};

const taskWith = (title: string) => screen.getByText(title).closest(".task") as HTMLElement;
const loaded = () => screen.findByRole("region", { name: "De plătit" });
/** A confirmed action on Sarcini: STUDIO9's debit that fits two invoices, answered by picking one. */
const SARCINI = "?co=STUDIO9&tab=sarcini", AMBIGUOUS = "Plată de lămurit", PICKED = "Plata de pe extras devine dovada facturii CX-1009?";
const pick = () => fixture("STUDIO9");
const pickBtn = () => within(taskWith(AMBIGUOUS)).getByRole("button", { name: /^Factura CX-1009/ });

it.each(["NORD", "STUDIO9", "ALL"] as const)("keeps the %s fixture root summary identical to the engine", (co) => {
  const full = co === "ALL" ? fixtureAll() : fixture(co);
  expect(fixtureSummaryData(full)).toEqual(summaryResult(full));
});

it("reads a compact root while subsequent API lists retain complete backing identities", async () => {
  const full = fixture(), api = server({ state: () => json(full) });
  const root = (await getBootstrap(full.co)).state as State;
  expect(api.gets()[0].url).toContain("api/bootstrap?co=NORD");
  expect(root.bounded).toBe(true);
  for (const key of ["invoices", "issued", "partners", "documents", "expenses"] as const) expect(root[key]).toEqual([]);
  expect(root.payable).toEqual(full.payable);
  const found = await getBrowse(full.co, "parteneri", { q: full.partners.at(-1)!.cui ?? full.partners.at(-1)!.name! });
  expect(found.data.some((p) => p.id === full.partners.at(-1)!.id)).toBe(true);
});

describe("POST actions", () => {
  it("sends nothing when the confirm is cancelled", async () => {
    const api = server({ state: () => json(pick()) });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    await userEvent.click(pickBtn());
    expect(await answer(false)).toBe(PICKED);
    expect(api.posts()).toEqual([]);
    expect(pickBtn()).toHaveFocus();
  });

  it("gives each payment file's task its next step on that file's sheet", async () => {
    at("?co=NORD&tab=sarcini");
    const s = taskState();
    s.files = [
      paymentFile({ id: 9, name: "NORD plăți 27 oct 2026", downloaded_at: "2026-10-26T15:20:00" }),
      paymentFile({ id: 8, name: "Furnizori octombrie", downloaded_at: "2026-10-26T15:20:00", changed_since_download: true }),
      paymentFile({ id: 6, name: "Chirii", payment_count: 0, question: "orphan" }),
    ];
    renderApp({ state: () => json(s) });
    const link = (task: string) => within(taskWith(task)).getByRole("link", { name: "Vezi fișierul" });
    expect(link(await screen.findByText("NORD plăți 27 oct 2026 a fost importat în BT Go?").then((e) => e.textContent!))).toHaveAttribute("href", "?co=NORD&tab=plati&pf=9");
    expect(taskWith("NORD plăți 27 oct 2026 a fost importat în BT Go?")).toHaveTextContent("Descărcat pe 26 oct, 15:20.");
    expect(link("Furnizori octombrie: de descărcat")).toHaveAttribute("href", "?co=NORD&tab=plati&pf=8");
    expect(taskWith("Un fișier necunoscut lângă Chirii").querySelector(".ic.late")).not.toBeNull();
    expect(screen.queryAllByRole("button", { name: /importat|Descarcă/ })).toEqual([]);
  });

  it("gives every connection and file task its next step, and in Sandbox leaves payment answers out without a dead end", async () => {
    at("?co=NORD&tab=sarcini");
    const s = taskState({ live: false, unreadable: [{ id: "8000000001", listed: "2026-10-14" }], invoices: fixture().invoices });
    s.files = [paymentFile({ question: "missing" })];
    s.system = { ...s.system, anaf: { ...s.system.anaf, status: "expired" }, mail: { ...s.system.mail, stale: true, last_ok: "2026-10-20T08:00:00" } };
    renderApp({ state: () => json(s) });
    await screen.findByText("Conexiunea ANAF a expirat");
    const link = (task: string, name: string) => within(taskWith(task)).getByRole("link", { name });
    expect(link("Conexiunea ANAF a expirat", "Reconectează ANAF")).toHaveAttribute("href", "?co=NORD&tab=setari&sec=integrari#k-anaf");
    expect(link("Sincronizare e-mail întârziată", "Vezi integrarea")).toHaveAttribute("href", "?co=NORD&tab=setari&sec=integrari#k-mail");
    expect(link("Factură e-Factura ilizibilă", "Deschide SPV")).toHaveAttribute("href", "https://pfinternet.anaf.ro");
    expect(taskWith("Factură e-Factura ilizibilă")).toHaveTextContent(/Primită pe .*Consult-o direct în SPV/);
    expect(taskWith("Factură e-Factura ilizibilă")).not.toHaveTextContent("8000000001");
    // No dead end: the overdue invoice opens, and the file question says where it is answered.
    expect(link("Energie Furnizare SA: plată restantă", "Vezi factura")).toHaveAttribute("href", expect.stringContaining("inv="));
    expect(link("NORD plăți 27 oct 2026 a fost importat în BT Go?", "Vezi fișierul")).toHaveAttribute("href", "?co=NORD&tab=plati&pf=7");
    expect(taskWith("NORD plăți 27 oct 2026 a fost importat în BT Go?")).toHaveTextContent("Răspunzi în Live.");
  });

  it("opens the payment file of the task's own firm under Toate firmele", async () => {
    at("?co=ALL&tab=sarcini");
    const s = fixture("STUDIO9");
    s.files = [paymentFile({ id: 4, co: "STUDIO9", name: "STUDIO9 plăți 27 oct 2026", payment_count: 0, question: "orphan" })];
    renderApp({ all: () => json(allWith(s)) });
    const task = (await screen.findByText("Un fișier necunoscut lângă STUDIO9 plăți 27 oct 2026")).closest(".task") as HTMLElement;
    expect(within(task).getByRole("link", { name: "Vezi fișierul" })).toHaveAttribute("href", "?co=STUDIO9&tab=plati&pf=4");
  });

  it("sends one POST for a rapid double tap, on the button and on the dialog's action", async () => {
    const reply = deferred<Response>();
    const api = server({ state: () => json(pick()), onPost: () => reply.promise });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    const btn = pickBtn();
    act(() => {
      btn.click(); // both taps land before React re-renders
      btn.click();
    });
    await waitFor(() => expect(document.querySelectorAll(".ask[role=dialog]")).toHaveLength(1));
    const yes = within(document.querySelector<HTMLElement>(".ask")!).getAllByRole("button")[0];
    act(() => {
      yes.click();
      yes.click();
    });
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await act(async () => reply.resolve(json({ ok: true, message: "", state: fixture() })));
    expect(api.posts()).toHaveLength(1);
  });

  it("declines an open question when the user navigates away, and the next action still asks and posts", async () => {
    const api = server({ state: () => json(pick()) });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    await userEvent.click(pickBtn());
    await waitFor(() => expect(document.querySelector(".ask")).not.toBeNull());
    act(() => {
      window.history.pushState(null, "", "?co=NORD&tab=plati");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(document.querySelector(".ask")).toBeNull());
    act(() => {
      window.history.pushState(null, "", SARCINI);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await userEvent.click(await waitFor(() => pickBtn()));
    await answer();
    await waitFor(() => expect(api.posts()).toHaveLength(1));
  });

  it("ignores a held Enter: the key that opens the question does not answer it", async () => {
    const api = server({ state: () => json(pick()) });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    pickBtn().focus();
    await userEvent.keyboard("{Enter>4}");
    await waitFor(() => expect(screen.getByRole("button", { name: "Renunță" })).toBeInTheDocument());
    expect(api.posts()).toEqual([]);
    await userEvent.keyboard("{/Enter}{Escape}");
    expect(document.querySelector(".ask")).toBeNull();
    expect(pickBtn()).toHaveFocus();
    expect(api.posts()).toEqual([]);
  });

  it("reads the state again when the POST answers state: null", async () => {
    const api = server({ state: () => json(pick()), onPost: () => json({ ok: true, message: "Gata.", state: null }) });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    await userEvent.click(pickBtn());
    await answer();
    await waitFor(() => expect(api.gets()).toHaveLength(2));
    expect(await screen.findByText("Gata.")).toBeInTheDocument();
  });

  it("says the request may have gone through when no answer comes back, and reads the state again", async () => {
    const api = server({ state: () => json(pick()), onPost: () => Promise.reject(new TypeError("Failed to fetch")) });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    await userEvent.click(pickBtn());
    await answer();
    expect(await screen.findByText("Nu a sosit confirmarea. Verifică dacă modificarea s-a salvat înainte să încerci din nou."))
      .toBeInTheDocument();
    await waitFor(() => expect(api.gets()).toHaveLength(2));
    // the sentence stays (no toast timer) and takes focus, past a declined question, until the state is read again; nothing is sent twice
    await act(() => new Promise((r) => setTimeout(r, 5100)));
    const reload = screen.getByRole("button", { name: "Reîncarcă datele" });
    expect(reload).toHaveFocus();
    await userEvent.click(pickBtn());
    await answer(false);
    expect(screen.getByText(NO_ANSWER)).toBeInTheDocument();
    await userEvent.click(reload);
    await waitFor(() => expect(screen.queryByText(NO_ANSWER)).toBeNull());
    expect([api.posts().length, api.gets().length]).toEqual([1, 3]);
  }, 10000);

  it("keeps a lost answer's notice when a later answer brings no state and reading it fails", async () => {
    let posts = 0, reads = true;
    const api = server({
      state: () => (reads ? json(pick()) : new Response("", { status: 500 })),
      onPost: () => (++posts === 1 ? Promise.reject(new TypeError("Failed to fetch")) : ((reads = false), json({ ok: true, message: "", state: null }))),
    });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    await userEvent.click(pickBtn());
    await answer();
    await screen.findByText(NO_ANSWER);
    await waitFor(() => expect(api.gets()).toHaveLength(2));
    await userEvent.click(pickBtn());
    await answer();
    await waitFor(() => expect(api.gets()).toHaveLength(3));
    expect(screen.getByText(NO_ANSWER)).toBeInTheDocument();
    expect(api.posts()).toHaveLength(2);
  });

  it("sends an action that got a 503 once: it may have been recorded, so only the person sends it again", async () => {
    const api = server({ state: () => json(pick()), onPost: () => json({ ok: false, message: "" }, 503) });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    await userEvent.click(pickBtn());
    await answer();
    await act(() => new Promise((r) => setTimeout(r, READ_RETRY_DELAY + 200)));
    expect(api.posts()).toHaveLength(1);
  });

  it("sends nothing while offline, and says so", async () => {
    const api = server({ state: () => json(pick()) });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    await userEvent.click(pickBtn());
    expect(await screen.findByText("Fără conexiune la internet. Modificarea nu a fost trimisă.")).toBeInTheDocument();
    expect(api.posts()).toHaveLength(0);
    vi.restoreAllMocks();
  });

  it("shows the running button's verb in progress with a spinner, the others only disabled", async () => {
    const sent = deferred<Response>();
    const api = server({ state: () => json(pick()), onPost: () => sent.promise });
    at(SARCINI);
    render(<App />);
    await screen.findByText(AMBIGUOUS);
    const task = taskWith(AMBIGUOUS);
    fireEvent.click(pickBtn()); fireEvent.click(pickBtn()); // a double tap in one tick
    await answer();
    const running = await within(task).findByRole("button", { name: "Se trimite…" });
    expect(running).toHaveAttribute("aria-busy", "true");
    expect(within(task).getByRole("button", { name: /^Factura CX-1010/ })).toBeDisabled();
    await userEvent.click(running);
    expect(api.posts()).toHaveLength(1);
    sent.resolve(json({ ok: true, message: "", state: pick() }));
    await waitFor(() => expect(pickBtn()).not.toHaveAttribute("aria-busy"));
  });
});

describe("versioned reads", () => {
  it("discards a GET that started before a POST and answered after it", async () => {
    const slow = deferred<Response>();
    let n = 0;
    const api = server({
      state: () => (n++ === 0 ? json(fixture()) : slow.promise),
      onPost: () => json({ ok: true, message: "", state: total("111.00") }),
    });
    render(<App />);
    await loaded();
    act(() => becomeVisible()); // a refresh starts, its answer is delayed
    expect(api.gets()).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Ascunde" }));
    await waitFor(() => expect(payable()).toBe("111,00"));
    await act(async () => slow.resolve(json(total("999.00"))));
    expect(payable()).toBe("111,00");
  });

  it("discards a GET superseded by a newer GET", async () => {
    const first = deferred<Response>();
    let n = 0;
    renderApp({ state: () => [() => json(fixture()), () => first.promise, () => json(total("222.00"))][n++]() });
    await loaded();
    act(() => becomeVisible());
    act(() => becomeVisible());
    await waitFor(() => expect(payable()).toBe("222,00"));
    await act(async () => first.resolve(json(total("999.00"))));
    expect(payable()).toBe("222,00");
  });

  it("ignores the failure of a superseded GET", async () => {
    const first = deferred<Response>();
    let n = 0;
    renderApp({ state: () => [() => json(fixture()), () => first.promise, () => json(fixture())][n++]() });
    await loaded();
    act(() => becomeVisible());
    act(() => becomeVisible());
    await act(async () => first.resolve(json({ ok: false, message: "x" }, 500)));
    expect(screen.queryByText(/Datele nu au putut fi actualizate/)).not.toBeInTheDocument();
  });
});

describe("loading", () => {
  it("shows no firm's page while another firm's state is read", async () => {
    const studio = deferred<Response>();
    renderApp({ state: (url) => (url.includes("STUDIO9") ? studio.promise : json(fixture())) });
    await loaded();
    fireEvent.click(document.querySelector<HTMLAnchorElement>('a[href*="co=STUDIO9"]')!);
    expect(await screen.findByText("Se încarcă…")).toBeInTheDocument();
    for (const text of ["Ieși din cont", "Niciun element", "Atelier Nord"]) expect(screen.queryByText(new RegExp(text))).toBeNull();
    expect(screen.queryByRole("region", { name: "De plătit" })).toBeNull();
    await act(async () => studio.resolve(json(fixture("STUDIO9"))));
    expect(await loaded()).toBeVisible();
  });

  it("shows a list as shapes until it is read, never as empty, and keeps its rows while the same list is read again", async () => {
    at("?co=NORD&tab=cheltuieli");
    let reply = deferred<Response>(), reads = 0;
    renderApp({ browse: () => { reads++; return reply.promise; } });
    await screen.findByRole("heading", { name: "Cheltuieli", level: 1 });
    expect(screen.getByText("Se încarcă…")).toBeInTheDocument();
    expect(screen.queryByText(EMPTY)).toBeNull();
    await act(async () => reply.resolve(json(fixtureBrowseData(fixture(), "cheltuieli", {}))));
    const count = await screen.findByText(/^\d+ (?:de )?cheltuieli$/);
    reply = deferred();
    act(() => becomeVisible());  // the state is read again, and with it the list
    await waitFor(() => expect(reads).toBe(2));
    expect(count).toBeInTheDocument();
    expect(screen.queryByText("Se încarcă…")).toBeNull();
  });
});

describe("failed reads", () => {
  it("announces the first read instead of leaving a blank page", async () => {
    const pending = deferred<Response>();
    renderApp({ state: () => pending.promise });
    expect(await screen.findByRole("status")).toHaveTextContent("Se încarcă…");
    pending.resolve(json(fixture()));
    await loaded();
    expect(screen.queryByText("Se încarcă…")).toBeNull();
  });

  it("says so when a refresh fails over cached state, and retries", async () => {
    let n = 0;
    const api = renderApp({ state: () => (n++ === 1 ? json({ ok: false, message: "x" }, 500) : json(total("333.00"))) });
    await loaded();
    act(() => becomeVisible());
    expect(await screen.findByText(/Datele nu au putut fi actualizate și pot fi vechi/)).toBeInTheDocument();
    expect(payable()).toBe("333,00"); // the cached state stays on screen
    await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
    await waitFor(() => expect(screen.queryByText(/Datele nu au putut fi actualizate/)).not.toBeInTheDocument());
    expect(api.gets()).toHaveLength(3);
  });

  it("reads again and drops the stale notice once the person signs back in", async () => {
    let n = 0;
    const api = server({ state: () => (n++ === 1 ? json({}, 500) : json(total("333.00"))) });
    const signedIn = () => createElement(IdentityContext.Provider, { value: { identity: fixtureIdentity(), signOut: async () => {}, refreshIdentity: async () => {} } }, createElement(App));
    const page = renderPlain(signedIn());
    await loaded();
    act(() => becomeVisible());
    expect(await screen.findByText(/Datele nu au putut fi actualizate/)).toBeInTheDocument();
    page.rerender(signedIn()); // Authentication's re-read after signing in gives the app a new identity.
    await waitFor(() => expect(screen.queryByText(/Datele nu au putut fi actualizate/)).not.toBeInTheDocument());
    expect(api.gets()).toHaveLength(3);
  });

  it("gives up on a list read that never answers, asks it once more, says so, and reads again when the browser is back online", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    at("?co=NORD&tab=vanzari");
    let answer = false;
    const api = renderApp({ browse: (url) => (answer ? json(fixtureBrowseData(fixture(), "vanzari", Object.fromEntries(new URL(url, "http://l").searchParams))) : new Promise<Response>(() => {})) });
    await screen.findByRole("heading", { name: "Vânzări", level: 1 });
    const browses = () => api.calls.filter((c) => c.url.includes("api/browse")).length;
    await act(() => vi.advanceTimersByTimeAsync(READ_TIMEOUT));
    expect(screen.queryByRole("button", { name: "Reîncearcă" })).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(READ_RETRY_DELAY + READ_TIMEOUT));
    expect(await screen.findByText("Datele nu au putut fi încărcate.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reîncearcă" })).toBeInTheDocument();
    expect(browses()).toBe(2);
    answer = true;
    act(() => { window.dispatchEvent(new Event("online")); });
    await waitFor(() => expect(document.querySelector("main .card")).toHaveAttribute("aria-busy", "false"));
    expect(browses()).toBeGreaterThan(2);
    expect(screen.queryByText("Datele nu au putut fi încărcate.")).toBeNull();
  });

  it("asks a read again when its answer starts but its body stops arriving, and gives up on the caller's cancel", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const page = { data: [], total: 0, next_cursor: null };
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => {
      if (reads++ === 2) return new Promise<Response>((_, reject) => init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
      if (reads > 1) return Promise.resolve(json(page));
      // headers arrive, then the body stops until the request is aborted
      const body = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode("{")); init.signal?.addEventListener("abort", () => c.error(new DOMException("aborted", "AbortError"))); } });
      return Promise.resolve(new Response(body, { status: 200 }));
    }));
    const read = getBrowse("NORD", "vanzari", {});
    await act(() => vi.advanceTimersByTimeAsync(READ_TIMEOUT + READ_RETRY_DELAY));
    expect([await read, reads]).toEqual([page, 2]);
    const cancel = new AbortController(), cancelled = getBrowse("NORD", "vanzari", {}, cancel.signal);
    cancel.abort();
    await expect(cancelled).rejects.toThrow();
    await act(() => vi.advanceTimersByTimeAsync(READ_RETRY_DELAY * 2));
    expect(reads).toBe(3);
  });

  it("asks a list read again once after a 503, and shows its rows", async () => {
    at("?co=NORD&tab=vanzari");
    let n = 0;
    renderApp({ browse: (url) => (n++ ? json(fixtureBrowseData(fixture(), "vanzari", Object.fromEntries(new URL(url, "http://l").searchParams))) : json({}, 503)) });
    await waitFor(() => expect(document.querySelector("main .card")).toHaveAttribute("aria-busy", "false"));
    expect([n, screen.queryByText("Datele nu au putut fi încărcate.")]).toEqual([2, null]);
  });

  it("tells a server error from no connection on the first load, and retries", async () => {
    let n = 0;
    const offline = () => Promise.reject(new TypeError("offline"));
    // a 500 is the server's answer, never asked again; no answer at all is asked once more before it shows
    renderApp({ state: () => [() => json({ ok: false, message: "x" }, 500), offline, offline, () => json(fixture())][n++]() });
    expect(await screen.findByText("Datele nu au putut fi încărcate. Încearcă din nou.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
    expect(await screen.findByText("Serverul nu răspunde. Verifică conexiunea la internet și încearcă din nou."))
      .toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
    expect(await loaded()).toBeInTheDocument();
  });
});

describe("Acasă", () => {

  it("shows ?m= as a toast and drops it from the URL", async () => {
    at("?co=NORD&tab=plati&m=Fi%C8%99ierul%20s-a%20schimbat.#fisier");
    renderApp();
    expect(await screen.findByText("Fișierul s-a schimbat.")).toBeInTheDocument();
    expect(window.location.search).toBe("?co=NORD&tab=plati");
    expect(window.location.hash).toBe("#fisier");
  });

  it("asks once for last month's statement that automatic payments wait for, with its upload", async () => {
    at("?co=NORD&tab=sarcini");
    const s = fixture();
    s.statement_hold = { month: "2026-10", count: 2 };
    renderApp({ state: () => json(s), companies: () => json(companiesConfig()) });
    expect(await screen.findByText("Lipsește extrasul din octombrie 2026")).toBeVisible();
    expect(screen.getByText(/2 plăți automate așteaptă extrasul/)).toBeVisible();
    expect(await screen.findByRole("button", { name: "Încarcă extras" })).toBeVisible();
  });

  it("renders text from documents as text, never as HTML", async () => {
    const s = fixture();
    Object.assign(s.invoices[0], { supplier: "<img src=x onerror=alert(1)>", status: "open", due_date: "2026-10-01", currency: "RON" });
    renderApp({ state: () => json(s) });
    await loaded();
    expect(screen.getByText("<img src=x onerror=alert(1)>: plată restantă")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });
});

describe("Toate firmele", () => {
  it("reads both companies at once, sums the cards, names each company's own tasks and offers the firm in place of quick actions", async () => {
    at("?co=ALL");
    const api = renderApp();
    await loaded();
    expect(api.gets("ALL")).toHaveLength(1);
    expect(api.gets("NORD")).toHaveLength(0);
    expect(payable()).toBe("14.062,30");
    expect(document.querySelectorAll(".money")[1]).toHaveTextContent("16.750,00");
    const firms = within(screen.getByRole("navigation", { name: "Alege firma" }));
    expect(firms.queryAllByRole("button")).toEqual([]);
    expect(firms.getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["?co=NORD", "?co=STUDIO9"]);
    expect(screen.queryByText("Pregătește plățile")).toBeNull();
    expect([...document.querySelectorAll(".home .task .h p")].every((p) => /^(NORD|STUDIO9)\. /.test(p.textContent ?? ""))).toBe(true);
    await userEvent.click(screen.getByRole("link", { name: `Toate (${fixtureAll().task_summary.total})` }));
    expect(window.location.search).toBe("?co=ALL&tab=sarcini");
    const owners = [...document.querySelectorAll(".task p")].map((p) => p.textContent?.match(/^(NORD|STUDIO9)\. /)?.[1]).filter(Boolean);
    expect(new Set(owners)).toEqual(new Set(["NORD", "STUDIO9"])); // in the subtitle, no label above the title
    expect(screen.getAllByText("Factură din e-mail de verificat")).toHaveLength(1); // the same document for both: once
  });

  it("is chosen from the company menu; from Fișiere de plăți it leads to Acasă, and the screen shows the last company chosen", async () => {
    at("?co=STUDIO9");
    renderApp({ state: (url) => json(fixture(url.includes("STUDIO9") ? "STUDIO9" : "NORD")) });
    await loaded();
    const menu = () => document.querySelector(".top .comenu") as HTMLElement;
    await userEvent.click(within(menu()).getByLabelText("STUDIO9, alege firma, plăți restante la NORD"));
    await userEvent.click(within(menu()).getByRole("link", { name: /Toate firmele/ }));
    expect(window.location.search).toBe("?co=ALL");
    await userEvent.click(within(document.querySelector(".top") as HTMLElement).getByRole("link", { name: "Mai mult" }));
    await userEvent.click(within(await screen.findByRole("navigation", { name: "Mai mult" })).getByRole("link", { name: "Fișiere de plăți" }));
    expect(window.location.search).toBe("?co=STUDIO9&tab=plati");
    expect(await screen.findByRole("heading", { level: 1, name: "Fișiere de plăți" })).toBeInTheDocument();
    expect(within(menu()).getByRole("link", { name: /Toate firmele/ })).toHaveAttribute("href", "?co=ALL");
  });

  it("marks the company menu when another company has late payments, and only then", async () => {
    at("?co=NORD");
    const s = fixture();
    s.companies = s.companies.map((c) => ({ ...c, late: c.key === "STUDIO9" ? 2 : 3 }));
    server({ state: () => json(s) });
    const { unmount } = render(<App />);
    await loaded();
    expect(screen.getByLabelText("NORD, Atelier Nord SRL, alege firma, plăți restante la STUDIO9").querySelector(".mk i")).not.toBeNull();
    unmount();
    s.companies = s.companies.map((c) => ({ ...c, late: c.key === "STUDIO9" ? 0 : 3 }));
    render(<App />);
    await loaded();
    expect(screen.getByLabelText("NORD, Atelier Nord SRL, alege firma").querySelector(".mk i")).toBeNull();
  });

  it("keeps the invoices' side and filter when switching company on Facturi, and closes the menu onto its button", async () => {
    at("?co=NORD&tab=vanzari&f=late");
    renderApp({ state: (url) => json(fixture(url.includes("STUDIO9") ? "STUDIO9" : "NORD")) });
    const pick = async (from: string, to: RegExp) => {
      await userEvent.click(await screen.findByLabelText(from));
      await userEvent.click(within(document.querySelector(".top .comenu .menu") as HTMLElement).getByRole("link", { name: to }));
    };
    await pick("NORD, Atelier Nord SRL, alege firma, plăți restante la STUDIO9", /^STUDIO9/);
    expect(window.location.search).toBe("?co=STUDIO9&tab=vanzari&f=late");
    await pick("STUDIO9, alege firma, plăți restante la NORD", /^NORD/); // NORD's state is already here: no reload
    const button = screen.getByLabelText("NORD, Atelier Nord SRL, alege firma, plăți restante la STUDIO9");
    expect((button.closest("details") as HTMLDetailsElement).open).toBe(false);
    expect(button).toHaveFocus();
  });

  it("keeps Cheltuieli's category and filter when switching company", async () => {
    at("?co=NORD&tab=cheltuieli&side=bonuri&f=open");
    renderApp({ state: (url) => json(fixture(url.includes("STUDIO9") ? "STUDIO9" : "NORD")) });
    await userEvent.click(await screen.findByLabelText("NORD, Atelier Nord SRL, alege firma, plăți restante la STUDIO9"));
    const studio9 = within(document.querySelector(".top .comenu .menu") as HTMLElement).getByRole("link", { name: /^STUDIO9/ });
    expect(studio9).toHaveAttribute("href", "?co=STUDIO9&tab=cheltuieli&side=bonuri&f=open");
    await userEvent.click(studio9);
    expect(window.location.search).toBe("?co=STUDIO9&tab=cheltuieli&side=bonuri&f=open");
    expect(await screen.findByRole("link", { name: "Fără factură" })).toHaveAttribute("aria-current", "page");
  });

  it("acts for the invoice's own company and reads both again", async () => {
    at("?co=ALL&tab=cheltuieli");
    const api = server({ onPost: (c) => json({ ok: true, message: "Am marcat factura ca plătită.", state: fixture(c.body!.co as "STUDIO9"),
      next: null }) });
    render(<App />);
    await userEvent.click(await screen.findByText("Laborator Analize SRL"));
    await userEvent.click(within(await screen.findByRole("dialog", { busy: false })).getByRole("button", { name: "Marchează ca plătită" }));
    await markPaidAs("Din contul firmei");
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0].body).toEqual({ co: "STUDIO9", id: "201", method: "bank", detail: "" });
    await waitFor(() => expect(api.gets("ALL")).toHaveLength(2));
    // the sheet closes where it was opened; the toast carries the answer
    expect(await screen.findByText("Am marcat factura ca plătită.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(window.location.search).toBe("?co=ALL&tab=cheltuieli");
  });
});

describe("Fișiere de plăți", () => {
  const row = (el: HTMLElement) => el.closest(".row") as HTMLElement;
  const sign = (r: HTMLElement) => r.querySelector(".ic.g")?.textContent ?? "";
  const DL = "2026-10-26T15:20:00";
  const withFiles = () => {
    const s = fixture();
    s.files = [
      paymentFile({ id: 9, name: "NORD plăți 27 oct 2026", downloaded_at: DL, changed_since_download: true, payment_count: 3, total: "1250.40" }),
      paymentFile({ id: 8, name: "Furnizori octombrie", created_at: "2026-10-20T08:00:00", downloaded_at: DL }),
      paymentFile({ id: 5, name: "NORD plăți 6 oct 2026", created_at: "2026-10-06T08:00:00", status: "imported", imported_at: "2026-10-07",
        payment_count: 3, confirmed_count: 1, skipped_count: 1 }),
      paymentFile({ id: 2, name: "NORD plăți 1 sept 2026", created_at: "2026-09-01T08:00:00", status: "confirmed", imported_at: "2026-09-02", confirmed_count: 2 }),
    ];
    return s;
  };
  const sheetOf = async (detail: ReturnType<typeof paymentFileDetail>, s = withFiles(), onPost?: () => Response) => {
    at(`?co=NORD&tab=plati&pf=${detail.id}`);
    const api = renderApp({ state: () => json(s), paymentFile: () => json(detail), onPost: onPost ?? (() => json({ ok: true, message: "", state: s })) });
    return { api, sheet: await screen.findByRole("dialog", { name: detail.name }) };
  };
  const filePosts = (api: ReturnType<typeof renderApp>) => api.posts().map((c) => [c.url, c.body]);

  it("lists the firm's files only, newest first, each with its state, total, payments and day, opening its sheet", async () => {
    at("?co=NORD&tab=plati");
    renderApp({ state: () => json(withFiles()) });
    await screen.findByText("NORD plăți 27 oct 2026");
    const rows = screen.getAllByRole("link", { name: /lei/ }).filter((r) => r.closest(".row"));
    expect(rows.map((r) => [r.querySelector("b")!.textContent, r.querySelector(".t small")!.textContent, sign(r), r.querySelector(".v")!.textContent])).toEqual([
      ["NORD plăți 27 oct 2026", "Deschis, modificat după descărcare", "", "1.250,40 lei3 plăți, 27 oct"],
      ["Furnizori octombrie", "Deschis, descărcat pe 26 oct", "", "300,00 lei2 plăți, 20 oct"],
      ["NORD plăți 6 oct 2026", "Importat, 1 din 2 confirmate", "≈", "300,00 lei3 plăți, 6 oct"],
      ["NORD plăți 1 sept 2026", "Confirmat", "=", "300,00 lei2 plăți, 1 sep"],
    ]);
    expect(rows[0]).toHaveAttribute("href", "?co=NORD&tab=plati&pf=9");
    expect(screen.queryByText("Telecom Rapid SRL")).toBeNull(); // no invoices at this level
    expect(screen.queryByRole("button", { name: /Adaugă facturi|Descarcă/ })).toBeNull();
  });

  it("leads an empty list to the invoices to pay", async () => {
    at("?co=NORD&tab=plati");
    const s = fixture(); s.files = [];
    renderApp({ state: () => json(s) });
    expect(await screen.findByRole("link", { name: "Alege facturi de plătit" })).toHaveAttribute("href", "?co=NORD&tab=cheltuieli&f=open");
  });

  it("shows an open file changed since its download: download again first, the new payments marked, each one removable", async () => {
    const detail = paymentFileDetail({ id: 9, name: "NORD plăți 27 oct 2026", downloaded_at: DL, changed_since_download: true,
      downloads: [{ id: 31, version: "v31", at: DL }] });
    detail.payments[1].added_after_download = true;
    const { api, sheet } = await sheetOf(detail);
    expect(sheet).toHaveTextContent("Modificat după descărcarea din 26 oct, 15:20. Descarcă versiunea nouă înainte de import.");
    expect(within(within(sheet).getByRole("group", { name: "Fișier de plăți" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["Descarcă din nou"]);
    expect(row(within(sheet).getByText("Energie Furnizare SA"))).toHaveTextContent("Nouă");
    expect(row(within(sheet).getByText("Telecom Rapid SRL"))).not.toHaveTextContent("Nouă");
    await userEvent.click(within(sheet).getByRole("button", { name: "Scoate din fișier Energie Furnizare SA, Nr. EF-12" }));
    await waitFor(() => expect(filePosts(api)).toEqual([["/web/api/payment-files", { action: "payment_file_remove", file_id: 9, payment_id: "pay-b" }]]));
  });

  it("renames an open file and confirms the import of its current download, with exact bodies", async () => {
    const detail = paymentFileDetail({ id: 8, name: "Furnizori octombrie", downloaded_at: DL, downloads: [{ id: 31, version: "v31", at: DL }], can_import: true });
    const { api, sheet } = await sheetOf(detail);
    expect(within(within(sheet).getByRole("group", { name: "Fișier de plăți" })).getAllByRole("button").map((b) => b.textContent))
      .toEqual(["Am importat în bancă", "Descarcă din nou"]);
    await userEvent.click(within(sheet).getByRole("button", { name: "Redenumește" }));
    const name = within(sheet).getByRole("textbox", { name: "Nume" });
    await userEvent.clear(name);
    await userEvent.type(name, "Furnizori noiembrie");
    await userEvent.click(within(sheet).getByRole("button", { name: "Salvează" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await userEvent.click(await within(sheet).findByRole("button", { name: "Am importat în bancă" }));
    await waitFor(() => expect(filePosts(api)).toEqual([
      ["/web/api/payment-files", { action: "payment_file_rename", file_id: 8, name: "Furnizori noiembrie" }],
      ["/web/api/payment-files", { action: "payment_file_import", file_id: 8, download_id: 31 }],
    ]));
  });

  it("keeps the name from being typed while it is saved, so no edit is lost when the field closes", async () => {
    const late = deferred<Response>();
    const { sheet } = await sheetOf(paymentFileDetail({ id: 8, name: "Furnizori octombrie" }), withFiles(), () => late.promise as unknown as Response);
    await userEvent.click(within(sheet).getByRole("button", { name: "Redenumește" }));
    const name = within(sheet).getByRole("textbox", { name: "Nume" });
    await userEvent.clear(name);
    await userEvent.type(name, "A");
    await userEvent.click(within(sheet).getByRole("button", { name: "Salvează" }));
    await waitFor(() => expect(name).toBeDisabled());
    await act(async () => late.resolve(json({ ok: true, message: "", state: null })));
  });

  it("downloads the file once and saves the bytes the engine sent, never asking again on its own", async () => {
    const created = vi.fn((b: Blob) => { void b; return "blob:file"; });
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }));
    const detail = paymentFileDetail({ id: 9, name: "NORD plăți 27 oct 2026" });
    const done = { id: "", held: "", file_id: 9, download_id: 32, name: "plati-NORD.csv", content_type: "text/csv", content_base64: btoa("a;b\n") };
    const { api, sheet } = await sheetOf(detail, withFiles(), () => json({ ok: true, message: "", state: null, done }));
    await userEvent.click(within(sheet).getByRole("button", { name: "Descarcă fișierul" }));
    await waitFor(() => expect(created).toHaveBeenCalledTimes(1));
    expect(await (created.mock.calls[0][0] as Blob).text()).toBe("a;b\n");
    expect(filePosts(api)).toEqual([["/web/api/payment-files", { action: "payment_file_download", file_id: 9 }]]);
  });

  it.each([
    ["the person goes elsewhere", async () => { act(() => { window.history.pushState(null, "", "/web/?co=NORD&tab=cheltuieli"); window.dispatchEvent(new PopStateEvent("popstate")); }); }],
    ["the session changes in another tab", async () => { fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); }],
  ])("saves nothing from a download that answers after %s", async (_, leave) => {
    const created = vi.fn(() => "blob:file");
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }));
    const late = deferred<Response>();
    const { api, sheet } = await sheetOf(paymentFileDetail({ id: 9, name: "NORD plăți 27 oct 2026" }), withFiles(), () => late.promise as unknown as Response);
    await userEvent.click(within(sheet).getByRole("button", { name: "Descarcă fișierul" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await leave();
    const done = { id: "", held: "", file_id: 9, download_id: 32, name: "plati-NORD.csv", content_type: "text/csv", content_base64: btoa("a;b") };
    await act(async () => late.resolve(json({ ok: true, message: "", state: null, done })));
    expect(created).not.toHaveBeenCalled();
  });

  it("reads the open sheet again when the firm's state is read again, and says so when the file is gone", async () => {
    const first = paymentFileDetail({ id: 9, name: "NORD plăți 27 oct 2026", can_download: true });
    const second = paymentFileDetail({ id: 9, name: "NORD plăți 27 oct 2026", payments: [first.payments[0]] });
    let reply: () => Response = () => json(first);
    at("?co=NORD&tab=plati&pf=9");
    renderApp({ state: () => json(withFiles()), paymentFile: () => reply() });
    const sheet = await screen.findByRole("dialog", { name: first.name });
    expect(within(sheet).getByText("Energie Furnizare SA")).toBeVisible();
    reply = () => json(second);
    act(() => becomeVisible()); // back from the bank's app
    await waitFor(() => expect(within(sheet).queryByText("Energie Furnizare SA")).toBeNull());
    reply = () => json({ ok: false, message: "not found" }, 404); // another session deleted it
    act(() => becomeVisible());
    const gone = await screen.findByRole("dialog", { name: "Fișierul nu mai există." });
    expect(within(gone).queryAllByRole("button").map((b) => b.textContent)).toEqual([""]); // only Închide
  });

  it.each([true, false])("answers a file missing from its folder with the file and version it asks about (Live %s)", async (live) => {
    const s = withFiles(); s.live = live;
    const detail = paymentFileDetail({ id: 9, name: "NORD plăți 27 oct 2026", questions: [{ kind: "missing", version: "v30", count: 2 }], can_download: false });
    // Restore answers with the file made again, as a download does; only restore saves bytes.
    const done = { id: "", held: "", file_id: 9, download_id: 33, name: "plati-NORD.csv", content_type: "text/csv", content_base64: btoa("x") };
    const { api, sheet } = await sheetOf(detail, s, () => json({ ok: true, message: "", state: s, done }));
    if (!live) {
      expect(sheet).toHaveTextContent("Răspunzi în Live.");
      expect(within(sheet).queryByRole("button", { name: /l-am importat|refă/ })).toBeNull();
      return;
    }
    await userEvent.click(within(sheet).getByRole("button", { name: "Da, l-am importat" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    const created = vi.fn(() => "blob:file");
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }));
    await userEvent.click(await within(sheet).findByRole("button", { name: "Nu, refă fișierul" }));
    await waitFor(() => expect(created).toHaveBeenCalledTimes(1)); // the file made again is saved, as a download is
    await waitFor(() => expect(filePosts(api)).toEqual([
      ["/web/api/payment-files", { action: "payment_file_missing_import", file_id: 9, version: "v30" }],
      ["/web/api/payment-files", { action: "payment_file_restore", file_id: 9, version: "v30" }],
    ]));
  });

  it("keeps a file's payments readable for read-only accounts, without its actions", async () => {
    at("?co=NORD&tab=plati&pf=8");
    const detail = paymentFileDetail({ id: 8, name: "Furnizori octombrie", downloaded_at: DL, downloads: [{ id: 31, version: "v31", at: DL }], can_import: true });
    const api = renderApp({ state: () => json(withFiles()), paymentFile: () => json(detail), principal: fixtureIdentity("readonly") });
    const sheet = await screen.findByRole("dialog", { name: "Furnizori octombrie" });
    expect(within(sheet).getByText("Telecom Rapid SRL")).toBeVisible();
    expect(within(sheet).queryAllByRole("button").map((b) => b.textContent)).toEqual([""]); // only Închide
    expect(api.posts()).toEqual([]);
  });

  it("asks which download was imported, or that none was, with the file and the download it names", async () => {
    const versions = [{ id: 32, version: "v32", at: "2026-10-27T09:30:00", count: 3, total: "1250.40" }, { id: 31, version: "v31", at: DL, count: 2, total: "300.00" }];
    const detail = paymentFileDetail({ id: 9, name: "NORD plăți 27 oct 2026", questions: [{ kind: "downloads", versions }], can_import: true,
      downloads: versions.map(({ id, version, at }) => ({ id, version, at })) });
    const { api, sheet } = await sheetOf(detail);
    await userEvent.click(within(sheet).getByRole("button", { name: "26 oct, 15:20: 2 plăți, 300,00 lei" }));
    await waitFor(() => expect(api.posts()).toHaveLength(1));
    await userEvent.click(await within(sheet).findByRole("button", { name: "Nu am importat niciuna" }));
    await waitFor(() => expect(filePosts(api)).toEqual([
      ["/web/api/payment-files", { action: "payment_file_import", file_id: 9, download_id: 31 }],
      ["/web/api/payment-files", { action: "payment_file_not_imported", file_id: 9, download_id: 32 }],
    ]));
  });

  it("shows an imported file's payments with their proof, a payment no longer paid never confirmed, and no actions", async () => {
    const detail = paymentFileDetail({ id: 5, name: "NORD plăți 6 oct 2026", status: "imported", imported_at: "2026-10-07",
      can_add: false, can_rename: false, can_remove: false, can_download: false });
    detail.payments = [
      { ...detail.payments[0], state: "paid", proved: true, paid_at: "2026-10-09" },
      { ...detail.payments[1], state: "imported" },
      { ...detail.payments[1], id: "pay-c", payee: "Plata Renuntata", state: "skipped" },
    ];
    const { sheet } = await sheetOf(detail);
    expect([["Telecom Rapid SRL", "=", "Plătită, 9 oct"], ["Energie Furnizare SA", "≈", "Importată în bancă, se așteaptă extrasul"], ["Plata Renuntata", "", "Nu se mai plătește"]]
      .map(([name]) => { const r = row(within(sheet).getByText(name)); return [name, sign(r), r.querySelector(".t small")!.textContent!.split(". Nr.")[0]]; }))
      .toEqual([["Telecom Rapid SRL", "=", "Plătită, 9 oct"], ["Energie Furnizare SA", "≈", "Importată în bancă, se așteaptă extrasul"], ["Plata Renuntata", "", "Nu se mai plătește"]]);
    expect(within(sheet).queryByRole("group", { name: "Fișier de plăți" })).toBeNull();
    expect(within(sheet).queryByRole("button", { name: /Redenumește|Scoate/ })).toBeNull();
  });
});

describe("navigation", () => {
  it("opens Setări behind the gear", async () => {
    renderApp();
    await loaded();
    await userEvent.click(screen.getByRole("link", { name: "Setări" }));
    expect(window.location.search).toBe("?co=NORD&tab=setari");
    expect(await screen.findByRole("heading", { name: "Sistem" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verifică acum" })).toBeEnabled();
  });
});


it.each([["0.00", "200.00", "200,00"], ["50.00", "150.00", "150,00"], ["200.00", "0.00", "0,00"]])(
  "shows received credits and one remaining balance with %s paid", async (covered, left, display) => {
  const s = fixture();
  const row = s.invoices[0];
  row.credits = [{ id: "99001", number: "COR 2", amount: "-50.00" }];
  row.left = left;
  row.covered = covered;
  at(`?co=NORD&inv=${row.id}`);
  server({ state: () => json(s) });
  render(<App />);
  expect(await screen.findByText("Storno COR 2")).toBeInTheDocument();
  expect(screen.getByText("Rest de plată").nextElementSibling).toHaveTextContent(display);
  if (covered !== "0.00") expect(screen.getByText("Plătit")).toBeInTheDocument();
});


it("reads one task page at a time, retains keyboard focus and reaches a later invoice", async () => {
  at("?co=NORD&tab=sarcini");
  const s = fixture();
  const invoice = s.invoices.find((r) => r.status === "open" && r.can_approve)!;
  s.invoices = Array.from({ length: 151 }, (_, i) => ({ ...invoice, id: `scale-${i}`, supplier: `Furnizor Sintetic ${i}`, due_date: "2026-10-01" }));
  const api = renderApp({ state: () => json(s) });
  await screen.findByRole("heading", { name: /Sarcini 159/ });
  expect(document.querySelectorAll("main .task")).toHaveLength(50);
  const next = screen.getByRole("button", { name: "Pagina următoare" });
  for (let i = 0; i < 3; i++) {
    await userEvent.click(next);
    await waitFor(() => expect(next).toHaveAttribute("aria-disabled", i === 2 ? "true" : "false"));
  }
  expect(next).toHaveFocus();
  expect(document.querySelectorAll("main .task")).toHaveLength(9);
  const card = taskWith("Furnizor Sintetic 150: plată restantă");
  expect(within(card).getByRole("link", { name: "Vezi factura" })).toHaveAttribute("href", expect.stringContaining("inv=scale-150"));
  expect(api.calls.filter((c) => c.url.includes("api/tasks")).map((c) => new URL(c.url, "http://local").searchParams.get("cursor")))
    .toEqual(["", "50", "100", "150"]);

});


it("pages every ambiguous candidate and posts the chosen target from a later page", async () => {
  at("?co=STUDIO9&tab=sarcini");
  const s = fixture("STUDIO9"), a = s.ambiguous[0];
  a.candidates = Array.from({ length: 101 }, (_, i) => ({ ...a.candidates[0], id: `target-${i}`, number: `SYN${String(i).padStart(3, "0")}` }));
  const api = renderApp({ state: () => json(s) });
  const nav = within(await screen.findByRole("navigation", { name: "Paginare facturi posibile" }));
  const next = nav.getByRole("button", { name: "Pagina următoare" });
  for (let i = 0; i < 2; i++) {
    await userEvent.click(next);
    await waitFor(() => expect(next).toHaveAttribute("aria-disabled", i ? "true" : "false"));
  }
  expect(screen.getAllByRole("button", { name: /^Factura SYN/ })).toHaveLength(1);
  await userEvent.click(screen.getByRole("button", { name: /^Factura SYN100/ }));
  expect(await answer()).toBe("Plata de pe extras devine dovada facturii SYN100?");
  expect(api.posts()[0].body).toEqual({ co: "STUDIO9", tx: a.tx, target: "target-100" });
});


describe("Custom financial controls follow the session", () => {
  it.each(["home", "issued", "transaction", "receipt", "partner", "accountant", "accounts"])("keeps %s readable without offering read-only mutations", async (area) => {
    const s = fixture(); s.can_manage_companies = true;
    const issued = s.issued.find(r => r.origin === "fiscalm")!;
    Object.assign(issued, { can_upload: true, can_storno: true, can_resolve: true });
    const tx = s.transactions.rows.find(r => r.counterparty === "POPESCU ANA")!; tx.can_link = true;
    const doc = s.documents.find(r => !r.invoice && !r.mail)!;
    doc.items = { lines: [{ name: "Cablu de test", unit: "H87", quantity: "1", net: "10.00", vat: "21" }], total: "12.10", currency: "RON", payment: "cash", revision: 1, provenance: "user", verified: false, state: "to_check", proposal: null, payment_mismatch: false };
    const partner = s.partners.find(r => r.roles.includes("client"))!; partner.client_id = "c1";
    const route = { home: "", issued: `&tab=vanzari&iss=${issued.id}`, transaction: `&tab=tranzactii&tx=${tx.id}`,
      receipt: `&tab=cheltuieli&doc=${doc.id}`, partner: `&tab=parteneri&p=${partner.id}`, accountant: "&tab=setari&sec=contabil", accounts: "&tab=tranzactii" }[area];
    at("?co=NORD" + route);
    const api = renderApp({ principal: fixtureIdentity("readonly"), state: () => json(s) });
    const content = within(["issued", "transaction", "receipt", "partner"].includes(area)
      ? await screen.findByRole("dialog", { busy: false }) : await screen.findByRole("main"));
    await waitFor(() => expect(document.querySelector(".loading")).toBeNull());
    expect(content.queryByRole("button", { name: /Stornează|Trimite.*SPV|Salvează indexul|Modifică$|Asociază tranzacția|Adaugă articol|Salvează$|Confirmă contul|Retrage confirmarea|Încarcă extras|Adaugă contul/ })).toBeNull();
    if (area === "receipt") expect(content.getByRole("textbox", { name: "Denumire" })).toBeDisabled();
    if (area === "transaction") expect(content.queryByRole("combobox", { name: "Plătitor" })).toBeNull();
    if (area === "accountant") expect(await content.findByRole("textbox", { name: "E-mailuri de la contabil" })).toBeDisabled();
    if (area === "home") expect(content.queryByRole("button", { name: "Încarcă bon sau factură" })).toBeNull();
    if (area === "issued") expect(content.getByRole("link", { name: /XML/ })).toBeVisible();
    expect(api.posts()).toEqual([]);
  });

  it.each(["admin", "member", "readonly"] as const)("offers only permitted rule controls to %s even with permissive business flags", async (role) => {
    const s = fixture(); s.can_manage_companies = true;
    s.rules.forEach(r => { r.can_pause = true; r.can_edit = true; });
    at("?co=NORD&tab=automatizari");
    renderApp({ principal: fixtureIdentity(role), state: () => json(s) });
    const page = within(await screen.findByRole("main"));
    await page.findByRole("heading", { name: "Automatizări" });
    expect(!!page.queryByRole("button", { name: /Oprește regula Trimitere automată/ })).toBe(role === "admin");
    expect(!!page.queryByRole("button", { name: /Modifică regula Reamintiri/ })).toBe(role === "admin");
    expect(page.queryAllByRole("button", { name: /factura lunară/ }).length > 0).toBe(role !== "readonly");
  });
});
