import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { at, fixture, fixtureIdentity, json, renderApp } from "./helpers";

it("shows release notes, the install command and posts the exact off preference", async () => {
  const s = fixture();
  s.system.updates = { enabled: true, command: "git pull && docker compose up -d --build", release: {
    tag_name: "v0.2.0", html_url: "https://github.com/ELASTO-TECH/fiscalm/releases/tag/v0.2.0", published_at: "2026-11-04T10:00:00Z" } };
  at("?co=NORD&tab=setari");
  const api = renderApp({ state: () => json(s) });
  const link = await screen.findByRole("link", { name: "Noutățile versiunii" });
  expect(link).toHaveAttribute("href", s.system.updates.release!.html_url);
  expect(screen.getByText(s.system.updates.command)).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "Versiune nouă disponibilă" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("switch", { name: "Verifică versiunile noi" }));
  expect(api.posts().map((p) => p.body)).toEqual([{ co: "NORD", on: "0" }]);
});

it("shows a member only the version: no release notice, switch, check or log", async () => {
  const s = fixture();
  s.can_manage_companies = false;
  s.system.updates = { enabled: true, command: "git pull", release: {
    tag_name: "v0.2.0", html_url: "https://github.com/ELASTO-TECH/fiscalm/releases/tag/v0.2.0", published_at: "2026-11-04T10:00:00Z" } };
  at("?co=NORD&tab=setari");
  renderApp({ state: () => json(s), principal: fixtureIdentity("member") });
  await screen.findByRole("heading", { name: "Setări" });
  expect(screen.queryByRole("switch", { name: "Verifică versiunile noi" })).toBeNull();
  expect(screen.queryByRole("switch", { name: "Statistici anonime" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Noutățile versiunii" })).toBeNull();
  expect(screen.getByText(`fiscalm ${s.system.version}`)).toBeInTheDocument();
  for (const gone of ["Ultima verificare", "Jurnalul verificărilor"]) expect(screen.queryByText(gone)).toBeNull();
  expect(screen.queryByRole("button", { name: "Verifică acum" })).toBeNull();
});

it("posts the exact telemetry preference", async () => {
  const s = fixture();
  s.system.telemetry = { enabled: true, forced_off: false };
  at("?co=NORD&tab=setari");
  const api = renderApp({ state: () => json(s) });
  await userEvent.click(await screen.findByRole("switch", { name: "Statistici anonime" }));
  expect(api.posts().map((p) => p.body)).toEqual([{ co: "NORD", on: "0" }]);
});

it("disables the telemetry switch when the server forces it off", async () => {
  const s = fixture();
  s.system.telemetry = { enabled: false, forced_off: true };
  at("?co=NORD&tab=setari");
  renderApp({ state: () => json(s) });
  const control = await screen.findByRole("switch", { name: "Statistici anonime" });
  expect(control).toBeDisabled();
  expect(control).not.toBeChecked();
});
