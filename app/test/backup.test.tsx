import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { at, becomeVisible, deferred, json, renderApp } from "./helpers";

it("shows the last backup and a download for administrators", async () => {
  at("?co=NORD&tab=setari");
  renderApp({ backup: () => json({ last_backup: "2026-11-03T02:00:00Z" }) });
  const link = await screen.findByRole("link", { name: "Descarcă copia de siguranță" });
  expect(link).toHaveAttribute("href", "/web/backup");
  expect(screen.getByText(/Ultima copie:/)).toBeInTheDocument();
});

it("shows the empty backup state", async () => {
  at("?co=NORD&tab=setari");
  renderApp();
  await screen.findByText("Nu există încă o copie de siguranță.");
  expect(screen.queryByRole("link", { name: "Descarcă copia de siguranță" })).toBeNull();
});

it("hides backups from members", async () => {
  at("?co=NORD&tab=setari");
  renderApp({ backup: () => json({}, 403) });
  await screen.findByRole("link", { name: /Integrări/ });
  expect(screen.queryByRole("region", { name: "Copii de siguranță" })).toBeNull();
});

it("keeps a failed read visible and preserves focus through retry", async () => {
  at("?co=NORD&tab=setari");
  let failed = true;
  renderApp({ backup: () => failed ? json({}, 500) : json({ last_backup: null }) });
  const alert = await screen.findByRole("alert");
  failed = false;
  await userEvent.click(within(alert).getByRole("button", { name: "Reîncearcă" }));
  expect(await screen.findByText("Nu există încă o copie de siguranță.")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Copii de siguranță" })).toHaveFocus();
});

it("shows no old backup or download while a retry after a failed refresh is read", async () => {
  at("?co=NORD&tab=setari");
  let reply: () => Response | Promise<Response> = () => json({ last_backup: "2026-11-03T02:00:00Z" });
  renderApp({ backup: () => reply() });
  await screen.findByRole("link", { name: "Descarcă copia de siguranță" });
  reply = () => json({}, 500);
  act(() => becomeVisible());
  const alert = await screen.findByRole("alert");
  const retry = deferred<Response>(); reply = () => retry.promise;
  await userEvent.click(within(alert).getByRole("button", { name: "Reîncearcă" }));
  expect(screen.queryByRole("link", { name: "Descarcă copia de siguranță" })).toBeNull();
  expect(screen.queryByText(/Ultima copie:/)).toBeNull();
  await act(async () => retry.resolve(json({ last_backup: null })));
  expect(await screen.findByText("Nu există încă o copie de siguranță.")).toBeInTheDocument();
});

