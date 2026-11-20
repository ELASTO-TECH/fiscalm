// ANAF and mail reading as the engine reports them (system.anaf.status, system.mail.state): a connection that was never set up
// raises no warning; an expired or failing one still does, on Setări in the Integrări row. Invented data around the fixture.
import { cleanup, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { State } from "@/lib/types";
import { at, fixture, json, renderApp } from "./helpers";

beforeEach(() => {
  localStorage.clear();
});

type Anaf = State["system"]["anaf"];
type Gmail = State["system"]["mail"];
const NOT_SET_UP_ANAF: Anaf = { status: "not_set_up", authorized: false, expires: null, days_left: null, last_sync: null, error: null, warnings: [], reconnect: false };
const NOT_SET_UP_GMAIL: Gmail = { state: "not_set_up", last_ok: null, stale: false, reconnect: false };

function withSystem(anaf: Partial<Anaf>, gmail?: Gmail, co: "NORD" | "STUDIO9" = "NORD"): State {
  const s = fixture(co);
  s.system.anaf = { ...s.system.anaf, ...anaf };
  if (gmail) s.system.mail = gmail;
  return s;
}

/** STUDIO9 without its other urgent tasks, so a connection's task is among the three Acasă shows. */
function calm(anaf: Partial<Anaf>, gmail?: Gmail): State {
  const s = withSystem(anaf, gmail, "STUDIO9");
  s.payable.late = 0;
  s.ambiguous = [];
  s.refused = [];
  s.unreadable = [];
  s.invoices = s.invoices.filter((r) => !(r.status === "open" && r.due_date && r.due_date < s.today));
  s.files = [];
  s.reports = [];
  s.statement_hold = null;
  if (!gmail) s.system.mail.stale = false;
  return s;
}

async function home(s: State) {
  at(`?co=${s.co}`);
  renderApp({ state: () => json(s) });
  await screen.findByRole("region", { name: "De plătit" });
}

/** Sarcini: every task, from the same derivation as Acasă's urgent ones. */
async function sarcini(s: State) {
  at(`?co=${s.co}&tab=sarcini`);
  renderApp({ state: () => json(s) });
  await screen.findByRole("heading", { name: /^Sarcini \d/ });
}

async function setari(s: State) {
  at(`?co=${s.co}&tab=setari`);
  renderApp({ state: () => json(s) });
  await screen.findByText("Ultima verificare");
}

const integrari = () => screen.getByRole("link", { name: /^Integrări/ });

const homeTasks = () => [...document.querySelectorAll(".home .task b")].map((b) => b.textContent);

const ANAF_TASKS = /Conexiunea ANAF|Eroare la citirea facturilor din ANAF/;

describe("integrations not set up", () => {
  it("raises no ANAF or Gmail task on Acasă", async () => {
    await home(calm(NOT_SET_UP_ANAF, NOT_SET_UP_GMAIL));
    expect(screen.queryByText(ANAF_TASKS)).toBeNull();
    expect(screen.queryByText("Sincronizare e-mail întârziată")).toBeNull();
    expect(document.querySelectorAll(".home .task .ic.late")).toHaveLength(0);
  });

  it("raises no ANAF or Gmail task on Sarcini", async () => {
    await sarcini(withSystem(NOT_SET_UP_ANAF, NOT_SET_UP_GMAIL));
    expect(document.querySelectorAll(".task").length).toBeGreaterThan(0);
    expect(screen.queryByText(ANAF_TASKS)).toBeNull();
    expect(screen.queryByText("Sincronizare e-mail întârziată")).toBeNull();
  });

  it("shows no connection warning on Setări when no connection is set up, and still links to Integrări", async () => {
    await setari(withSystem(NOT_SET_UP_ANAF, NOT_SET_UP_GMAIL));
    expect(screen.getByRole("heading", { name: "Setări", level: 1 })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["Configurare", "Sistem"]);
    expect(integrari()).toHaveAttribute("href", expect.stringContaining("sec=integrari"));
    expect(integrari()).not.toHaveTextContent(/ANAF|e-mail/);
  });

  it("says on the Integrări row that the mail reading is late", async () => {
    await setari(withSystem(NOT_SET_UP_ANAF, { state: "ok", last_ok: "2026-10-20T09:00:00", stale: true, reconnect: false }));
    expect(integrari()).toHaveTextContent("Sincronizare e-mail întârziată");
  });
});

const EXPIRED = "Facturile noi din e-Factura nu se mai citesc până la o nouă autorizare cu certificatul digital.";

describe("ANAF expired", () => {
  const anaf: Partial<Anaf> = { status: "expired", authorized: true, expires: "2026-10-20", days_left: -7 };

  it("warns among Acasă's urgent tasks, on Sarcini and on Setări, and says expired, not expiring soon", async () => {
    await home(calm(anaf));
    expect(screen.getByRole("heading", { name: /^Necesită atenție/ })).toBeInTheDocument();
    expect(homeTasks()).toContain("Conexiunea ANAF a expirat");
    expect(screen.getByText("Conexiunea ANAF a expirat").closest(".task")).toHaveTextContent(EXPIRED);
    expect(screen.queryByText(/Conexiunea ANAF expiră pe/)).toBeNull();
    cleanup();
    await sarcini(withSystem(anaf));
    expect(screen.getByText("Conexiunea ANAF a expirat").closest(".task")).toHaveTextContent(EXPIRED);
    cleanup();
    await setari(withSystem(anaf));
    expect(integrari()).toHaveTextContent("Conexiunea ANAF a expirat");
    expect(integrari()).not.toHaveTextContent(/expiră pe/);
  });

  it("raises nothing for an integration saved but never connected", async () => {
    const never: Partial<Anaf> = { status: "not_connected", authorized: false, expires: null, days_left: null };
    await home(calm(never));
    expect(homeTasks().join()).not.toMatch(ANAF_TASKS);
    cleanup();
    await setari(withSystem(never));
    expect(integrari()).not.toHaveTextContent(ANAF_TASKS);
  });
});

describe("ANAF failing", () => {
  const error = "ANAF credentials could not be read; check the token file or authorize again.";
  const anaf: Partial<Anaf> = { status: "error", authorized: false, expires: null, days_left: null, last_sync: null, error, reconnect: true };

  it.each([
    [true, "Pot lipsi facturi noi. Reconectează ANAF cu certificatul digital.", "Reconectează ANAF"],
    [false, "Pot lipsi facturi noi. Citirea se reia la următoarea verificare.", "Vezi conexiunea ANAF"],
  ])("says what to do, red only when ANAF must be connected again (reconnect: %s)", async (reconnect, body, link) => {
    await home(calm({ ...anaf, reconnect }));
    expect(homeTasks()).toContain("Eroare la citirea facturilor din ANAF");
    cleanup();
    await sarcini(withSystem({ ...anaf, reconnect }));
    const task = screen.getByText("Eroare la citirea facturilor din ANAF").closest(".task")!;
    expect(task).toHaveTextContent(body);
    expect(within(task as HTMLElement).getByRole("link", { name: link })).toHaveAttribute("href", expect.stringContaining("sec=integrari"));
    expect(!!task.querySelector(".ic.late")).toBe(reconnect);
    expect(screen.queryByText(/niciodată/)).toBeNull();
    expect(screen.queryByText("Conexiunea ANAF a expirat")).toBeNull();
  });

  it("says on the Integrări row that the invoices couldn't be read", async () => {
    await setari(withSystem(anaf));
    expect(integrari()).toHaveTextContent("Eroare la citirea facturilor din ANAF");
    expect(integrari()).not.toHaveTextContent(/expiră pe/);
  });
});

describe("ANAF connected", () => {
  it("reminds about an authorization ending within 30 days", async () => {
    await sarcini(withSystem({ status: "ok", authorized: true, expires: "2026-11-16", days_left: 20 }));
    expect(screen.getByText("Conexiunea ANAF expiră pe 16 nov")).toBeInTheDocument();
    expect(screen.queryByText("Conexiunea ANAF a expirat")).toBeNull();
  });
});
