// @vitest-environment-options {"url":"https://localhost:3000"}
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it } from "vitest";
import App from "@/components/App";
import { Authentication } from "@/components/Authentication";
import { fixtureUsers } from "@/lib/fixtures";
import type { AccountSession, AccountUser, UserRole } from "@/lib/types";
import { answer, at, deferred, fixtureIdentity, json, renderApp, server, choose } from "./helpers";

const path = `/setup#${"1".repeat(32)}.${"2".repeat(32)}`;
const link = `https://localhost:3000${path}`;
beforeEach(() => { localStorage.clear(); at("?co=NORD&tab=setari&sec=utilizatori"); });

it.each(["admin", "member", "readonly"] as UserRole[])("shows global account management only to %s capabilities", async role => {
  const api = renderApp({ principal: fixtureIdentity(role), users: () => json(fixtureUsers()) });
  await screen.findByRole("heading", { name: role === "admin" ? "Utilizatori" : "Setări", level: 1 });
  expect(!!screen.queryByRole("link", { name: "Adaugă utilizator" })).toBe(role === "admin");
  await waitFor(() => expect(api.calls.some(c => c.url === "/web/api/users")).toBe(role === "admin"));
});

it("recognizes key-only users as configured and sends reset rather than another enrolment link", async () => {
  const user = userEvent.setup(), rows = fixtureUsers(); rows[1].passkeys = 2;
  const api = renderApp({ users: () => json(rows) });
  const account = await screen.findByRole("link", { name: /Cont de lucru/ }); expect(account).toHaveTextContent("2 chei de acces");
  await user.click(account); await user.click(screen.getByRole("button", { name: "Resetează autentificarea" }));
  expect(await answer()).toContain("cheile de acces");
  await waitFor(() => expect(api.posts()).toHaveLength(1));
  expect(api.posts()[0]).toEqual({ url: "/web/api/user-reset", method: "POST", body: { id: rows[1].id } });
});

it("creates one account with the selected global role and displays its setup link once", async () => {
  const user = userEvent.setup(), pending = deferred<Response>();
  const api = renderApp({ users: () => json(fixtureUsers()), onPost: () => pending.promise });
  await user.click(await screen.findByRole("link", { name: "Adaugă utilizator" }));
  await user.type(screen.getByLabelText("Nume de utilizator"), "operator2"); await user.type(screen.getByLabelText("Nume afișat"), "Cont nou");
  await choose(screen.getByLabelText("Rol"), "readonly");
  const button = screen.getByRole("button", { name: "Creează utilizatorul" });
  act(() => { button.click(); button.click(); });
  expect(api.posts()).toEqual([{ url: "/web/api/user-create", method: "POST", body: { username: "operator2", display_name: "Cont nou", role: "readonly" } }]);
  await act(async () => pending.resolve(json({ ok: true, message: "", done: { id: "u_new", url: path } })));
  expect(await screen.findByText(link)).toBeVisible(); await user.click(within(screen.getByText(link).closest(".secret") as HTMLElement).getByRole("button", { name: "Închide" }));
  expect(screen.queryByText(link)).not.toBeInTheDocument(); expect(window.location.search).not.toContain("usr=");
});

it("keeps an administrator-guard refusal beside the unchanged role form", async () => {
  const user = userEvent.setup(), admin = fixtureUsers()[0];
  const api = renderApp({ users: () => json([admin]), onPost: () => json({ ok: false, message: "Refuz de test" }) });
  await user.click(await screen.findByRole("link", { name: /Cont de administrare/ }));
  await choose(screen.getByLabelText("Rol"), "readonly"); await user.click(screen.getByRole("button", { name: "Salvează utilizatorul" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Refuz de test");
  expect(screen.getByRole("dialog")).toBeVisible(); expect(api.posts()[0].body).toEqual({ id: admin.id, display_name: admin.display_name, role: "readonly" });
});

it.each([
  ["Dezactivează", "user-disable", {}], ["Reactivează", "user-enable", { disabled_at: "2026-11-01T00:00:00+00:00" }],
  ["Deblochează", "user-unlock", { locked_until: "2099-01-01T00:00:00+00:00" }],
  ["Creează link de configurare", "user-setup-link", {}], ["Resetează autentificarea", "user-reset", { password_totp: true }],
] as [string, string, Partial<AccountUser>][])("sends the selected account only for %s", async (label, action, change) => {
  const user = userEvent.setup(), target = { ...fixtureUsers()[1], ...change };
  const api = renderApp({ users: () => json([target]), onPost: () => json({ ok: true, message: "", done: { id: target.id, url: action.includes("link") || action.includes("reset") ? path : undefined } }) });
  await user.click(await screen.findByRole("link", { name: /Cont de lucru/ })); await user.click(screen.getByRole("button", { name: label }));
  if (action === "user-disable" || action === "user-reset") await answer();
  await waitFor(() => expect(api.posts()).toHaveLength(1));
  expect(api.posts()[0]).toEqual({ url: `/web/api/${action}`, method: "POST", body: { id: target.id } });
});

it("retries a failed account read and cancels a disable without posting", async () => {
  const user = userEvent.setup(); let failed = true;
  const api = renderApp({ users: () => failed ? json({}, 500) : json(fixtureUsers()) });
  await screen.findByRole("alert"); failed = false; await user.click(screen.getByRole("button", { name: "Reîncearcă" }));
  await user.click(await screen.findByRole("link", { name: /Cont de lucru/ })); await user.click(screen.getByRole("button", { name: "Dezactivează" }));
  expect(await answer(false)).toBe("Dezactivezi contul? Sesiunile lui se închid și nu se mai poate conecta.");
  expect(api.posts()).toEqual([]);
});

it("declines a pending question when the user leaves the account's sheet", async () => {
  const user = userEvent.setup(); const api = renderApp({ users: () => json(fixtureUsers()) });
  await user.click(await screen.findByRole("link", { name: /Cont de lucru/ })); await user.click(screen.getByRole("button", { name: "Dezactivează" }));
  await waitFor(() => expect(document.querySelector(".ask")).not.toBeNull());
  const list = new URL(location.href); list.searchParams.delete("usr");
  act(() => { window.history.pushState(null, "", list.search); window.dispatchEvent(new PopStateEvent("popstate")); });
  await waitFor(() => expect(document.querySelector(".ask")).toBeNull());
  expect(api.posts()).toEqual([]);
});

it.each(["network", "truncated"])("keeps an unconfirmed %s write warning while refreshing metadata without posting again", async mode => {
  const user = userEvent.setup(); let changed = false;
  const api = renderApp({ users: () => json(changed ? [...fixtureUsers(), { ...fixtureUsers()[1], id: "u_new", display_name: "Cont nou" }] : fixtureUsers()),
    onPost: () => { changed = true; return mode === "network" ? Promise.reject(new TypeError("interrupted")) : new Response('{"ok":', { headers: { "Content-Type": "application/json" } }); } });
  await user.click(await screen.findByRole("link", { name: "Adaugă utilizator" }));
  await user.type(screen.getByLabelText("Nume de utilizator"), "operator2"); await user.type(screen.getByLabelText("Nume afișat"), "Cont nou");
  await user.click(screen.getByRole("button", { name: "Creează utilizatorul" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Nu a sosit confirmarea.");
  await waitFor(() => expect(api.calls.filter(c => c.url === "/web/api/users")).toHaveLength(2)); expect(api.posts()).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "Închide" })); expect(await screen.findByRole("link", { name: /Cont nou/ })).toBeVisible();
  await user.click(screen.getByRole("link", { name: "Adaugă utilizator" })); expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("refreshes the current role after a successful self-demotion and hides administration", async () => {
  const user = userEvent.setup(); let identity = fixtureIdentity(); const admin = { ...fixtureUsers()[0], id: identity.me.id };
  const api = server({ identity: () => json(identity), users: () => json([admin]), onPost: () => { identity = fixtureIdentity("member"); return json({ ok: true, message: "", done: { id: admin.id } }); } });
  render(<Authentication><App /></Authentication>); await user.click(await screen.findByRole("link", { name: /Cont de administrare/ }));
  await choose(screen.getByLabelText("Rol"), "member"); await user.click(screen.getByRole("button", { name: "Salvează utilizatorul" }));
  expect(await screen.findByRole("heading", { name: "Setări", level: 1 })).toBeVisible();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(screen.queryByRole("link", { name: /Utilizatori/ })).not.toBeInTheDocument(); expect(api.posts()).toHaveLength(1);
});

it("hands a self-reset directly to setup without losing its fragment or retaining private content", async () => {
  const user = userEvent.setup(), admin = { ...fixtureUsers()[0], id: fixtureIdentity().me.id };
  const api = server({ users: () => json([admin]), onPost: c => c.url === "/web/api/user-reset"
    ? json({ ok: true, message: "", done: { url: path } }) : json({ ok: true, user: { username: admin.username } }) });
  render(<Authentication><App /></Authentication>);
  await user.click(await screen.findByRole("link", { name: new RegExp(admin.display_name) })); await user.click(screen.getByRole("button", { name: "Resetează autentificarea" }));
  await answer();
  expect(await screen.findByLabelText("Parolă nouă")).toBeVisible();
  expect(window.location.pathname).toBe("/setup"); expect(window.location.hash).toBe(""); expect(document.title).toBe("Configurează contul, fiscalm");
  expect(api.posts().map(c => [c.url, c.body])).toEqual([["/web/api/user-reset", { id: admin.id }], ["/auth/setup/inspect", { link: link.split("#")[1] }]]);
});


const active: AccountSession = { id: "c".repeat(64), method: "password_totp", created_at: "2026-11-01T08:00:00+00:00",
  last_seen_at: "2026-11-04T10:00:00+00:00", device: "Dispozitiv de test", ip: null };

it("reads and closes only the selected other account's session", async () => {
  const user = userEvent.setup(); let closed = false;
  const api = renderApp({ users: () => json(fixtureUsers()), userSessions: () => json(closed ? [] : [active]), onPost: () => { closed = true; return json({ ok: true, message: "" }); } });
  await user.click(await screen.findByRole("link", { name: /Cont de lucru/ })); await user.click(await screen.findByRole("button", { name: "Închide sesiunea" }));
  expect(await answer()).toBe("Închizi această sesiune? Pe dispozitivul ei trebuie să se conecteze din nou.");
  expect(await screen.findByText("Niciun element")).toBeVisible();
  expect(api.posts()).toEqual([{ url: "/web/api/session-close", method: "POST", body: { id: active.id } }]);
  expect(api.calls.filter(c => c.url.includes("api/user-sessions?")).map(c => c.url)).toEqual(Array(2).fill(`/web/api/user-sessions?id=${fixtureUsers()[1].id}`));
});

it("keeps an uncertain admin session-close warning after refreshing committed state", async () => {
  const user = userEvent.setup(); let closed = false;
  const api = renderApp({ users: () => json(fixtureUsers()), userSessions: () => json(closed ? [] : [active]), onPost: () => { closed = true; return Promise.reject(new TypeError("offline")); } });
  await user.click(await screen.findByRole("link", { name: /Cont de lucru/ })); await user.click(await screen.findByRole("button", { name: "Închide sesiunea" }));
  await answer();
  expect(await screen.findByRole("alert")).toHaveTextContent("Nu a sosit confirmarea."); expect(await screen.findByText("Niciun element")).toBeVisible(); expect(api.posts()).toHaveLength(1);
});

it("retries session reads and cancels a close without posting", async () => {
  const user = userEvent.setup(); let failed = true;
  const api = renderApp({ users: () => json(fixtureUsers()), userSessions: () => failed ? json({}, 500) : json([active]) });
  await user.click(await screen.findByRole("link", { name: /Cont de lucru/ })); await screen.findByRole("alert"); failed = false;
  await user.click(screen.getByRole("button", { name: "Reîncearcă" })); await user.click(await screen.findByRole("button", { name: "Închide sesiunea" }));
  await answer(false); expect(api.posts()).toEqual([]);
});

it("discards a late account A session response while account B's dialog is open", async () => {
  const user = userEvent.setup(), pending = deferred<Response>();
  renderApp({ users: () => json(fixtureUsers()), userSessions: url => url.includes(fixtureUsers()[1].id) ? pending.promise : json([{ ...active, device: "Dispozitiv B" }]) });
  await user.click(await screen.findByRole("link", { name: /Cont de lucru/ })); await user.click(screen.getByRole("button", { name: "Închide" }));
  await user.click(screen.getByRole("link", { name: /Cont de citire/ })); expect(await screen.findByText("Dispozitiv B")).toBeVisible();
  await act(async () => pending.resolve(json([active]))); expect(screen.queryByText(active.device)).not.toBeInTheDocument(); expect(screen.getByText("Dispozitiv B")).toBeVisible();
});

it("links the administrator's own sessions to Cont instead of closing a possibly rotated current id", async () => {
  const user = userEvent.setup(); const api = renderApp({ users: () => json([{ ...fixtureUsers()[0], id: fixtureIdentity().me.id }]) });
  await user.click(await screen.findByRole("link", { name: /Cont de administrare/ })); const link = screen.getByRole("link", { name: "Sesiunile contului" });
  expect(link).toHaveAttribute("href", expect.stringContaining("sec=cont")); expect(api.calls.some(c => c.url.includes("api/user-sessions?"))).toBe(false);
});
