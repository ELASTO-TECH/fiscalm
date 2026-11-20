import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { at, fixture, json, renderApp } from "./helpers";

it.each([
  { live: false, to: "Live", from: "Sandbox", yes: "Treci în Live", body: { mode: "live", expected: "sandbox" }, message: "Live este activ." },
  { live: true, to: "Sandbox", from: "Live", yes: "Treci în Sandbox", body: { mode: "sandbox", expected: "live" }, message: "Sandbox este activ." },
])("tapping $to asks first, posts only on yes and shows the engine's answer", async ({ live, to, from, yes, body, message }) => {
  at("?co=NORD&tab=setari&sec=mod");
  let now = live; // the engine's mode, changed by the POST
  const api = renderApp({ state: () => json({ ...fixture(), live: now }), onPost: () => { now = !live; return json({ ok: true, message, next: null, done: null, state: null }); } });
  expect(await screen.findByRole("radio", { name: from })).toHaveAccessibleDescription("Activ");
  await userEvent.click(screen.getByRole("radio", { name: to }));
  await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Renunță" }));
  expect(api.posts()).toEqual([]);
  await waitFor(() => expect(screen.getByRole("radio", { name: from })).toBeChecked());
  await userEvent.click(screen.getByRole("radio", { name: from }));
  expect(screen.queryByRole("dialog")).toBeNull(); // the active mode asks nothing
  await userEvent.click(screen.getByRole("radio", { name: to }));
  await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: yes }));
  await waitFor(() => expect(api.posts()).toEqual([{ url: "/web/api/mode-switch", method: "POST", body: { ...body, confirmed: "yes", request_id: expect.stringMatching(/^[0-9a-f]{32}$/) } }]));
  await waitFor(() => expect(screen.getByRole("radio", { name: to })).toHaveAccessibleDescription("Activ"));
  expect(document.querySelector(".toast")?.textContent).toBe(message);
});

it("shows the mode in force after a refused switch", async () => {
  at("?co=NORD&tab=setari&sec=mod");
  let now = false; // another session switched to Live meanwhile
  renderApp({ state: () => json({ ...fixture(), live: now }), onPost: () => { now = true; return json({ ok: false, message: "Modul a fost deja schimbat.", next: null, done: null, state: null }); } });
  await userEvent.click(await screen.findByRole("radio", { name: "Live" }));
  await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Treci în Live" }));
  await waitFor(() => expect(screen.getByRole("radio", { name: "Live" })).toHaveAccessibleDescription("Activ"));
});

it("reads the mode again when the switch's answer is lost, and sends nothing more", async () => {
  at("?co=NORD&tab=setari&sec=mod");
  let now = false;
  const api = renderApp({ state: () => json({ ...fixture(), live: now }), onPost: () => { now = true; return new Response("", { status: 502 }); } });
  await userEvent.click(await screen.findByRole("radio", { name: "Live" }));
  await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Treci în Live" }));
  await waitFor(() => expect(screen.getByRole("radio", { name: "Live" })).toHaveAccessibleDescription("Activ"));
  expect(api.posts()).toHaveLength(1);
});
