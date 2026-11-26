import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { lunaName } from "@/lib/format";
import { at, fixture, fixtureIdentity, json, renderApp, taskState } from "./helpers";

beforeEach(() => { localStorage.clear(); at("?co=NORD&tab=sarcini"); });

it.each([false, true])("copies reminder text locally with LIVE=%s and never posts", async (live) => {
  const s = taskState(); s.live = live;
  const user = userEvent.setup();
  const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
  const api = renderApp({ state: () => json(s) });
  const summaries = await screen.findAllByText("Text pentru reamintire");
  await user.click(summaries[0]);
  const details = summaries[0].closest("details")!;
  await user.click(within(details).getByRole("button", { name: "Copiază textul" }));
  const t = s.reminder_tasks[0];
  expect(copy).toHaveBeenCalledWith(t.subject + "\n\n" + t.body);
  expect(within(details).getByRole("status")).toHaveTextContent("Textul a fost copiat.");
  expect(api.posts()).toEqual([]);
});

it("shows the engine's warning on a reminder whose statements are behind, the usual line otherwise", async () => {
  const s = taskState();
  s.reminder_tasks = [{ ...s.reminder_tasks[0], warning: "Extrasele sunt în urmă." }, { ...s.reminder_tasks[0], id: "other", warning: null }];
  renderApp({ state: () => json(s) });
  expect(await screen.findByText("Extrasele sunt în urmă.")).toBeInTheDocument();
  expect(screen.getAllByText("Copiază textul și trimite-l clientului din e-mail.")).toHaveLength(1);
});

it("prepares the company ZIP then confirms exactly the displayed snapshot", async () => {
  at("?co=NORD&tab=rapoarte&month=2026-09");
  const initial = fixture(); initial.live = false;
  const prepared = structuredClone(initial);
  const report = prepared.reports.find(r => r.month === "2026-09")!;
  report.status = "prepared"; report.version = "a".repeat(64); report.prepared_at = "2026-10-03T14:20:00"; report.mark = "m1";
  const api = renderApp({ state: () => json(initial),
    onPost: () => json({ ok: true, message: "", state: prepared }) });
  const opened: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { opened.push(this.getAttribute("href")!); });
  await userEvent.click(await screen.findByRole("button", { name: "Descarcă dosarul" }));
  expect(api.posts()[0].body).toEqual({ co: "NORD", month: "2026-09" });
  // One tap prepares and downloads the version the engine froze; the same ZIP stays a link after.
  const zip = "/web/packfile?co=NORD&month=2026-09&v=" + "a".repeat(64);
  await waitFor(() => expect(opened).toEqual([zip]));
  expect(await screen.findByRole("link", { name: "Descarcă dosarul" })).toHaveAttribute("href", zip);
  await userEvent.click(screen.getByRole("button", { name: "Marchează ca trimis" })); // one tap: Anulează marcarea undoes it
  expect(document.querySelector(".ask")).toBeNull();
  await waitFor(() => expect(api.posts()).toHaveLength(2));
  expect(api.posts()[1].url).toContain("pack-sent");
  expect(api.posts()[1].body).toEqual({ co: "NORD", month: "2026-09", version: "a".repeat(64), mark: "m1" });
});

it("undoes a sent mark on the month", async () => {
  at("?co=NORD&tab=rapoarte&month=2026-09");
  const s = fixture(); s.live = false;
  Object.assign(s.reports.find(r => r.month === "2026-09")!, { status: "sent", version: "a".repeat(64), sent_on: "2026-10-04", mark: "m2" });
  const api = renderApp({ state: () => json(s), onPost: () => json({ ok: true, message: "", state: s }) });
  await userEvent.click(await screen.findByRole("button", { name: "Anulează marcarea" }));
  await waitFor(() => expect(api.posts()[0].url).toContain("pack-unsent"));
  expect(api.posts()[0].body).toEqual({ co: "NORD", month: "2026-09", version: "a".repeat(64), mark: "m2" });
});

it("offers a readonly account the prepared ZIP but not the prepare", async () => {
  at("?co=NORD&tab=sarcini");
  const s = fixture();
  const ready = s.reports.find((r) => r.status === "ready")!;
  const prepared = { ...ready, month: "2026-08", status: "prepared" as const, version: "b".repeat(64) };
  s.reports = [...s.reports.filter((r) => r.month !== prepared.month), prepared];
  const api = renderApp({ state: () => json(s), principal: fixtureIdentity("readonly") });
  await screen.findByText(`Dosarul pe ${lunaName(ready.month)}: de pregătit`);
  expect(screen.getByRole("link", { name: "2 tranzacții fără document" })).toHaveAttribute("href", `?co=NORD&tab=tranzactii&month=${ready.month}`); // a task's report has no counts
  expect(screen.queryByRole("button", { name: "Descarcă dosarul" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Descarcă dosarul" })).toHaveAttribute("href", "/web/packfile?co=NORD&month=2026-08&v=" + "b".repeat(64));
  expect(api.posts()).toEqual([]);
});

it("prepares a changed month again instead of offering its old ZIP", async () => {
  at("?co=NORD&tab=sarcini");
  const initial = fixture(); initial.live = false;
  const old = initial.reports.find((r) => r.status === "ready")!;
  Object.assign(old, { status: "prepared", version: "a".repeat(64), prepared_at: "2026-10-03T14:20:00", stale: true });
  const again = structuredClone(initial);
  Object.assign(again.reports.find((r) => r.month === old.month)!, { version: "c".repeat(64), prepared_at: "2026-11-11T09:05:00", stale: false });
  const api = renderApp({ state: () => json(initial), onPost: () => json({ ok: true, message: "", state: again }) });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  expect(await screen.findByText(/^Pregătit pe 3 oct, 14:20\. Între timp/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Descarcă dosarul" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Pregătește din nou" }));
  expect(api.posts()[0].url).toContain("pack-prepare");
  expect(api.posts()[0].body).toEqual({ co: "NORD", month: old.month });
  const link = await screen.findByRole("link", { name: "Descarcă dosarul" });
  expect(link).toHaveAttribute("href", `/web/packfile?co=NORD&month=${old.month}&v=${"c".repeat(64)}`);
  expect(screen.getByText(/^Pregătit pe .*09:05\.$/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Pregătește din nou" })).not.toBeInTheDocument();
});

it("tells a readonly account who sends the pack, not to mark it sent", async () => {
  const s = fixture();
  const r = s.reports.find((x) => x.status === "ready")!;
  Object.assign(r, { status: "prepared", version: "b".repeat(64), prepared_at: "2026-10-03T14:20:00", stale: false });
  renderApp({ state: () => json(s), principal: fixtureIdentity("readonly") });
  expect(await screen.findByText("Doar un membru sau un administrator poate marca dosarul ca trimis.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Marchează ca trimis" })).not.toBeInTheDocument();
});
