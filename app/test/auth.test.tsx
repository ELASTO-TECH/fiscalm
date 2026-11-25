// @vitest-environment-options {"url":"https://localhost:3000"}
import { renderToString } from "react-dom/server";
import { hydrateRoot } from "react-dom/client";
import { createPortal } from "react-dom";
import { useContext, useEffect, useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import App, { ShellSkeleton } from "@/components/App";
import { AppCtx, IdentityContext, useCan } from "@/components/context";
import { Act } from "@/components/ui";
import { parse } from "@/lib/nav";
import type { Action } from "@/lib/api";
import type { Identity, UserRole } from "@/lib/types";
import { assertPasskey, createPasskey, passkeysSupported, suggestedPasskeyName, type CreationOptions } from "@/lib/passkeys";
import { Sheet } from "@/components/Sheets";
import { Codes, PasskeyEnrollment } from "@/components/Enrollment";
import { SignOut, UserAccount } from "@/components/UserAccount";
import { Authentication, PasswordCode } from "@/components/Authentication";
import { base, getBootstrap, postAccess, postFile, HTTPS_REQUIRED, REAUTHENTICATE, SESSION_CHANGED, SESSION_ENDED, postAuth } from "@/lib/api";
import { answer, at, deferred, fixture, fixtureIdentity, json, renderSignedIn, server } from "./helpers";

const page = <Authentication bootstrap pending={<ShellSkeleton />}><App /></Authentication>;

beforeEach(() => { at(""); sessionStorage.clear(); localStorage.clear(); });

const nativeBytes = () => new Uint8Array([251, 255]).buffer;
class NativeAttestation {
  clientDataJSON = nativeBytes(); attestationObject = nativeBytes();
  getTransports() { return ["internal", "hybrid"]; }
}
class NativeAssertion {
  clientDataJSON = nativeBytes(); authenticatorData = nativeBytes(); signature = nativeBytes();
  userHandle: ArrayBuffer | null = nativeBytes();
}
class NativeKey {
  rawId = nativeBytes();
  constructor(public response: NativeAttestation | NativeAssertion) {}
}
function nativeBrowser() {
  vi.stubGlobal("isSecureContext", true); vi.stubGlobal("PublicKeyCredential", NativeKey);
  vi.stubGlobal("AuthenticatorAttestationResponse", NativeAttestation); vi.stubGlobal("AuthenticatorAssertionResponse", NativeAssertion);
  const credentials = { create: vi.fn().mockResolvedValue(new NativeKey(new NativeAttestation())), get: vi.fn().mockResolvedValue(new NativeKey(new NativeAssertion())) };
  vi.stubGlobal("navigator", new Proxy(navigator, { get: (target, key) => key === "credentials" ? credentials : Reflect.get(target, key, target) })); return credentials;
}
const nativeCreation: CreationOptions = { challenge: "-_8", rp: { id: "localhost", name: "fiscalm" }, user: { id: "-_8", name: "example", displayName: "Cont de test" },
  pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -8 }, { type: "public-key", alg: -257 }],
  attestation: "none", authenticatorSelection: { residentKey: "required", userVerification: "required" }, excludeCredentials: [{ type: "public-key", id: "-_8" }], timeout: 300000 };
const nativeAssertion = { challenge: "-_8", rpId: "localhost", userVerification: "required" as const, timeout: 300000 };
const passkeyMetadata = (id = "-_8"): Identity["factors"]["passkeys"][number] => ({ id, name: "Cheie de test", created_at: "2026-11-05T00:00:00+00:00", last_used_at: null, backup_eligible: false, backed_up: false });

it("preserves server WebAuthn requirements and serializes native binary proofs as unpadded base64url", async () => {
  const native = nativeBrowser(), { signal } = new AbortController();
  expect(passkeysSupported()).toBe(true);
  expect(await createPasskey(nativeCreation, signal)).toEqual({ credential_id: "-_8", client_data: "-_8", attestation: "-_8", transports: '["internal","hybrid"]' });
  expect(native.create).toHaveBeenCalledExactlyOnceWith({ signal, publicKey: { ...nativeCreation, challenge: nativeBytes(),
    user: { ...nativeCreation.user, id: nativeBytes() }, excludeCredentials: [{ type: "public-key", id: nativeBytes() }] } });
  expect(await assertPasskey(nativeAssertion, signal)).toEqual({ credential_id: "-_8", user_handle: "-_8", client_data: "-_8", authenticator_data: "-_8", signature: "-_8" });
  expect(native.get).toHaveBeenCalledExactlyOnceWith({ signal, publicKey: { ...nativeAssertion, challenge: nativeBytes() } });
});

it.each(["create", "get"] as const)("refuses null and wrong native %s response types", async method => {
  const native = nativeBrowser(), { signal } = new AbortController();
  const run = () => method === "create" ? createPasskey(nativeCreation, signal) : assertPasskey(nativeAssertion, signal);
  native[method].mockResolvedValueOnce(null).mockResolvedValueOnce(new NativeKey(method === "create" ? new NativeAssertion() : new NativeAttestation()));
  await expect(run()).rejects.toBeInstanceOf(TypeError); await expect(run()).rejects.toBeInstanceOf(TypeError);
  if (method === "get") { const response = new NativeAssertion(); response.userHandle = null; native.get.mockResolvedValue(new NativeKey(response)); await expect(run()).rejects.toBeInstanceOf(TypeError); }
});

it.each(["create", "get"] as const)("cancels native %s before prompting and refuses a late proof after cancellation", async method => {
  const native = nativeBrowser(), abort = new AbortController(); abort.abort();
  const run = (signal: AbortSignal) => method === "create" ? createPasskey(nativeCreation, signal) : assertPasskey(nativeAssertion, signal);
  await expect(run(abort.signal)).rejects.toMatchObject({ name: "AbortError" }); expect(native[method]).not.toHaveBeenCalled();
  const pending = deferred<NativeKey>(), next = new AbortController(); native[method].mockReturnValue(pending.promise);
  const result = run(next.signal); next.abort(); pending.resolve(new NativeKey(method === "create" ? new NativeAttestation() : new NativeAssertion()));
  await expect(result).rejects.toMatchObject({ name: "AbortError" }); expect(native[method]).toHaveBeenCalledTimes(1);
});

it("does not prompt in an insecure context or hide native cancellation errors", async () => {
  const native = nativeBrowser(), { signal } = new AbortController(); vi.stubGlobal("isSecureContext", false);
  expect(passkeysSupported()).toBe(false); await expect(createPasskey(nativeCreation, signal)).rejects.toMatchObject({ name: "NotSupportedError" });
  expect(native.create).not.toHaveBeenCalled(); vi.stubGlobal("isSecureContext", true);
  native.get.mockRejectedValue(new DOMException("Declined", "NotAllowedError"));
  await expect(assertPasskey(nativeAssertion, signal)).rejects.toMatchObject({ name: "NotAllowedError" }); expect(native.get).toHaveBeenCalledTimes(1);
});

it.each(["passkey/finish", "setup/passkey/finish", "step-up/passkey/finish", "recover/passkey/finish", "me/passkey/finish", "me/passkey/remove", "me/passkey/rename", "recover/code"] as const)("announces only a confirmed %s and forwards its cancellation signal", async path => {
  let accepted = false; const api = server({ onPost: () => json({ ok: accepted }, accepted ? 200 : 401) });
  const changed = vi.spyOn(Storage.prototype, "setItem"), { signal } = new AbortController();
  await expect(postAuth(path, { ticket: "fixture-ticket" }, signal)).rejects.toMatchObject({ status: 401 }); expect(changed).not.toHaveBeenCalled();
  accepted = true; await postAuth(path, { ticket: "fixture-ticket" }, signal);
  expect(api.posts()).toEqual([0, 1].map(() => ({ url: `/auth/${path}`, method: "POST", body: { ticket: "fixture-ticket" } })));
  expect(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.signal).toBe(signal);
  expect(changed).toHaveBeenCalledExactlyOnceWith(SESSION_CHANGED, expect.any(String));
});
const privateApp = <Authentication><p>Conținut privat</p><SignOut /></Authentication>;

it("offers server-enabled passkeys, clears the password on switching, and sends exact discoverable sign-in proofs", async () => {
  nativeBrowser(); const user = userEvent.setup(); let signedIn = false;
  const api = server({ identity: () => signedIn ? json(fixtureIdentity()) : json({ methods: { passkey: true } }, 401),
    onPost: c => { if (c.url.endsWith("/options")) return json({ ok: true, ticket: "fixture-key", publicKey: nativeAssertion }); signedIn = true; return json({ ok: true }); } });
  render(privateApp); await user.click(await screen.findByRole("button", { name: "Folosește parola și codul" })); await user.type(screen.getByLabelText("Parolă"), "Synthetic password 2026");
  await user.click(screen.getByRole("button", { name: "Folosește o cheie de acces" })); expect(screen.queryByLabelText("Parolă")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Intră în cont cu o cheie de acces" }));
  expect(await screen.findByText("Conținut privat")).toBeVisible();
  expect(api.posts()).toEqual([{ url: "/auth/passkey/options", method: "POST", body: {} }, { url: "/auth/passkey/finish", method: "POST",
    body: { ticket: "fixture-key", credential_id: "-_8", user_handle: "-_8", client_data: "-_8", authenticator_data: "-_8", signature: "-_8" } }]);
});

it.each([["passkey", "Intră în cont cu o cheie de acces"], ["password", "Continuă"]])("opens with the %s this device used last and remembers the next one", async (last, first) => {
  nativeBrowser(); const user = userEvent.setup(); if (last === "password") localStorage.setItem("fiscalm:sign-in-method", "password");
  server({ identity: () => json({ methods: { passkey: true } }, 401), onPost: () => json({ ok: true, ticket: "fixture-key", publicKey: nativeAssertion }) });
  render(privateApp); expect(await screen.findByRole("button", { name: first })).toBeVisible();
  if (last === "passkey") { await user.click(screen.getByRole("button", { name: first })); await waitFor(() => expect(localStorage.getItem("fiscalm:sign-in-method")).toBe("passkey")); }
});

it.each(["server", "browser"])("hides passkey sign-in when the %s does not support it", async unavailable => {
  nativeBrowser(); if (unavailable === "browser") vi.stubGlobal("isSecureContext", false);
  server({ identity: () => json({ methods: { passkey: unavailable !== "server" } }, 401) }); render(privateApp);
  await screen.findByLabelText("Parolă"); expect(screen.queryByRole("button", { name: "Folosește o cheie de acces" })).toBeNull();
});

it("keeps server-advertised passkey sign-in available after leaving a normally authenticated page", async () => {
  nativeBrowser(); const user = userEvent.setup(); server(); render(privateApp);
  await user.click(await screen.findByRole("button", { name: "Ieși din cont" }));
  expect(await screen.findByRole("button", { name: "Intră în cont cu o cheie de acces" })).toBeVisible();
});

it.each(["cancel", "refuse", "unreadable"])("keeps passkey %s failures outside the app without an automatic retry", async failure => {
  const native = nativeBrowser(), user = userEvent.setup(); if (failure === "cancel") native.get.mockRejectedValue(new DOMException("Declined", "NotAllowedError"));
  const api = server({ identity: () => json({ methods: { passkey: true } }, 401), onPost: c => c.url.endsWith("/options")
    ? json({ ok: true, ticket: "fixture-key", publicKey: nativeAssertion }) : failure === "unreadable" ? new Response("{", { status: 200 }) : json({ ok: false, message: "Refuz de test" }, 401) });
  render(privateApp); await user.click(await screen.findByRole("button", { name: "Intră în cont cu o cheie de acces" }));
  expect(await screen.findByRole("alert")).toBeVisible(); expect(screen.queryByText("Conținut privat")).toBeNull();
  expect(api.posts().map(c => c.url)).toEqual(failure === "cancel" ? ["/auth/passkey/options"] : ["/auth/passkey/options", "/auth/passkey/finish"]);
  await user.click(screen.getByRole("button", { name: "Folosește parola și codul" })); expect(screen.getByLabelText("Parolă")).toHaveValue("");
});

it.each(["unmount", "account-change"])("aborts a pending native sign-in on %s, ignoring its late proof", async change => {
  const native = nativeBrowser(), pending = deferred<NativeKey>(), nextIdentity = deferred<Response>(), user = userEvent.setup(); native.get.mockReturnValue(pending.promise); let other = false;
  const api = server({ identity: () => other ? nextIdentity.promise : json({ methods: { passkey: true } }, 401),
    onPost: () => json({ ok: true, ticket: "fixture-key", publicKey: nativeAssertion }) });
  const page = render(privateApp); await user.click(await screen.findByRole("button", { name: "Intră în cont cu o cheie de acces" }));
  await waitFor(() => expect(native.get).toHaveBeenCalledOnce());
  if (change === "unmount") page.unmount(); else { other = true; fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); }
  expect(native.get.mock.calls[0][0].signal.aborted).toBe(true);
  await act(async () => pending.resolve(new NativeKey(new NativeAssertion()))); expect(api.posts().map(c => c.url)).toEqual(["/auth/passkey/options"]);
  if (change === "account-change") { expect(screen.queryByText("Conținut privat")).toBeNull(); await act(async () => nextIdentity.resolve(json(fixtureIdentity("member")))); expect(await screen.findByText("Conținut privat")).toBeVisible(); }
});

it("preserves same-account passkey step-up through native prompt focus and retries the original operation once", async () => {
  const native = nativeBrowser(), pending = deferred<NativeKey>(), user = userEvent.setup(); native.get.mockReturnValue(pending.promise);
  const identity = fixtureIdentity(); identity.factors.password_totp = false; identity.auth.method = "passkey";
  identity.factors.passkeys = [passkeyMetadata()]; let fresh = false;
  const api = server({ identity: () => json(identity), onPost: c => c.url.endsWith("/options") ? json({ ok: true, ticket: "fixture-step", publicKey: nativeAssertion })
    : c.url.endsWith("/passkey/finish") ? (fresh = true, json({ ok: true })) : fresh ? json({ ok: true }) : reauth() });
  render(<Authentication><input aria-label="Denumire" defaultValue="Formular păstrat" /></Authentication>); await screen.findByLabelText("Denumire");
  let operation!: ReturnType<typeof postAuth>; act(() => { operation = postAuth("me/recovery-codes", {}); });
  await user.click(await screen.findByRole("button", { name: "Confirmă cu o cheie de acces" })); expect(screen.queryByLabelText("Parolă")).toBeNull();
  expect(screen.queryByRole("button", { name: "Nu mai am acces" })).toBeNull();
  await waitFor(() => expect(native.get).toHaveBeenCalledOnce()); fireEvent(window, new Event("focus"));
  expect(native.get.mock.calls[0][0].signal.aborted).toBe(false); expect(screen.getByRole("dialog")).toBeVisible();
  await act(async () => pending.resolve(new NativeKey(new NativeAssertion()))); await expect(operation).resolves.toMatchObject({ ok: true });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); expect(screen.getByLabelText("Denumire")).toHaveValue("Formular păstrat");
  expect(api.posts().map(c => c.url)).toEqual(["/auth/me/recovery-codes", "/auth/step-up/passkey/options", "/auth/step-up/passkey/finish", "/auth/me/recovery-codes"]);
});
const account = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.type(screen.getByLabelText("Nume de utilizator"), "example");
  await user.type(screen.getByLabelText("Parolă"), "Synthetic local password 2026");
  await user.click(screen.getByRole("button", { name: "Continuă" }));
};

it("waits for me and renders private content only for a normal session", async () => {
  const pending = deferred<Response>();
  const api = server({ identity: () => pending.promise }); render(privateApp);
  expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Nume de utilizator")).not.toBeInTheDocument();
  await act(async () => pending.resolve(json(fixtureIdentity("readonly"))));
  expect(await screen.findByText("Conținut privat")).toBeVisible();
  expect(api.calls).toEqual([{ url: "/web/api/me", method: "GET", body: undefined }]);
});

it("requires TOTP after the password ticket, retains the factor step on refusal, and posts exact bodies", async () => {
  const user = userEvent.setup(); let signedIn = false;
  const api = server({ identity: () => json(fixtureIdentity(), signedIn ? 200 : 401), onPost: call => {
    if (call.url === "/auth/password") return json({ ok: true, ticket: "fixture-ticket" });
    if (call.body?.code === "123456") return json({ ok: false, message: "Refuz de test" }, 401);
    signedIn = true; return json({ ok: true });
  } });
  render(privateApp); await screen.findByLabelText("Nume de utilizator");
  expect(screen.getByText("fiscalm").querySelector("svg path")).toBeInTheDocument();
  await account(user);
  const code = await screen.findByLabelText("Cod din aplicația de autentificare");
  expect(code).toHaveFocus(); expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Parolă")).not.toBeInTheDocument();
  await user.type(code, "123456"); await user.click(screen.getByRole("button", { name: "Intră în cont" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Refuz de test"); expect(code).toBeVisible();
  await user.type(code, "654321"); await user.click(screen.getByRole("button", { name: "Intră în cont" }));
  expect(await screen.findByText("Conținut privat")).toBeVisible();
  expect(api.posts()).toEqual([
    { url: "/auth/password", method: "POST", body: { username: "example", password: "Synthetic local password 2026" } },
    { url: "/auth/totp", method: "POST", body: { ticket: "fixture-ticket", code: "123456" } },
    { url: "/auth/totp", method: "POST", body: { ticket: "fixture-ticket", code: "654321" } },
  ]);
  expect(vi.mocked(fetch).mock.calls[0][1]).toMatchObject({ credentials: "same-origin", cache: "no-store" });
});

it("shows password refusals without opening the app and can switch accounts after a ticket", async () => {
  const user = userEvent.setup(); let refused = true;
  server({ identity: () => json({}, 401), onPost: () => refused ? json({ ok: false, message: "Refuz de test" }, 401) : json({ ok: true, ticket: "fixture-ticket" }) });
  render(privateApp); await screen.findByLabelText("Nume de utilizator"); await account(user);
  expect(await screen.findByRole("alert")).toHaveTextContent("Refuz de test"); expect(screen.getByLabelText("Parolă")).toHaveValue("");
  refused = false; await user.type(screen.getByLabelText("Parolă"), "Synthetic local password 2026");
  await user.click(screen.getByRole("button", { name: "Continuă" }));
  await user.click(await screen.findByRole("button", { name: "Folosește alt cont" }));
  expect(screen.getByLabelText("Parolă")).toHaveValue(""); expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
});

it("does not treat a server error as a sign-in refusal, and retries me", async () => {
  const user = userEvent.setup(); let status = 503;
  server({ identity: () => json(fixtureIdentity(), status) }); render(privateApp);
  expect(await screen.findByRole("alert")).toBeVisible(); expect(screen.queryByLabelText("Nume de utilizator")).not.toBeInTheDocument();
  status = 200; await user.click(screen.getByRole("button", { name: "Reîncearcă" }));
  expect(await screen.findByText("Conținut privat")).toBeVisible();
});

it("refuses recovery-only sessions and plain HTTP before financial reads", async () => {
  const restricted = fixtureIdentity(); restricted.auth.recovery_only = true;
  const api = server({ identity: () => json(restricted) }); const page = render(privateApp);
  await screen.findByRole("heading", { name: "Recuperează contul" }); expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument(); page.unmount();
  vi.stubGlobal("location", new URL("http://localhost:3000")); api.calls.length = 0; render(privateApp);
  expect(await screen.findByRole("alert")).toHaveTextContent(HTTPS_REQUIRED); expect(api.calls).toEqual([]);
});

it.each(["same", "other", "signout"] as const)("keeps the screen under a sign-in sheet when a read loses its session, then %s", async next => {
  const user = userEvent.setup(); let ended = false, signedIn = false;
  const other = fixtureIdentity(); other.me = { ...other.me, id: "u_other", username: "altul" };
  const current = () => {
    const identity = signedIn && next === "other" ? other : fixtureIdentity();
    identity.sessions = [{ ...sessionMetadata(true), id: (signedIn ? "c" : "a").repeat(64) }];
    return identity;
  };
  const api = server({ state: () => json({}, 401), identity: () => !ended || signedIn ? json(current()) : json({}, 401),
    onPost: c => c.url === "/auth/password" ? json({ ok: true, ticket: "fixture-password" }) : (signedIn = true, json({ ok: true })) });
  render(<Authentication><input aria-label="Denumire" /></Authentication>); await user.type(await screen.findByLabelText("Denumire"), "Nesalvat");
  ended = true; await act(async () => { await expect(getBootstrap("NORD")).rejects.toMatchObject({ status: 401 }); });
  const sheet = await screen.findByRole("dialog", { name: "Sesiunea a expirat" });
  expect(screen.getByLabelText("Denumire")).toHaveValue("Nesalvat"); expect(screen.getByLabelText("Denumire").closest("[inert]")).not.toBeNull();
  expect(within(sheet).getByText("example")).toBeVisible(); expect(within(sheet).queryByLabelText("Nume de utilizator")).toBeNull(); expect(within(sheet).queryByRole("button", { name: "Nu mai am acces" })).toBeNull();
  if (next === "signout") {
    fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED }));
    expect(await screen.findByRole("heading", { name: "Conectare" })).toBeVisible(); expect(screen.queryByLabelText("Denumire")).toBeNull(); return;
  }
  await user.type(within(sheet).getByLabelText("Parolă"), "Synthetic local password 2026"); await user.click(within(sheet).getByRole("button", { name: "Continuă" }));
  await user.type(await within(sheet).findByLabelText("Cod din aplicația de autentificare"), "123456"); await user.click(within(sheet).getByRole("button", { name: "Intră în cont" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByLabelText("Denumire")).toHaveValue(next === "same" ? "Nesalvat" : ""); expect(screen.getByLabelText("Denumire").closest("[inert]")).toBeNull();
  if (next === "same") expect(screen.getByRole("status")).toHaveTextContent("Te-ai conectat din nou.");
  expect(api.posts().map(c => c.url)).toEqual(["/auth/password", "/auth/totp"]);
});

it("lets another tab's sign-out win over a later focus re-read, and never leaves the loading screen up", async () => {
  const reads: ReturnType<typeof deferred<Response>>[] = []; let first = true;
  server({ identity: () => { if (first) { first = false; return json(fixtureIdentity()); } const next = deferred<Response>(); reads.push(next); return next.promise; } });
  render(privateApp); await screen.findByText("Conținut privat");
  fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); fireEvent(window, new Event("focus"));
  act(() => window.dispatchEvent(new Event(SESSION_ENDED)));
  await act(async () => { for (const read of reads) read.resolve(json({}, 401)); });
  expect(await screen.findByLabelText("Nume de utilizator")).toBeVisible(); expect(screen.queryByText("Conținut privat")).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("sends nothing from the kept screen until the same user is confirmed again", async () => {
  const user = userEvent.setup(); let ended = false;
  const api = server({ identity: () => ended ? json({}, 401) : json(fixtureIdentity()), onPost: c => c.url === "/auth/password" ? json({ ok: true, ticket: "fixture-password" }) : (ended = false, json({ ok: true })) });
  render(privateApp); await screen.findByText("Conținut privat"); ended = true;
  act(() => window.dispatchEvent(new Event(SESSION_ENDED))); const sheet = await screen.findByRole("dialog", { name: "Sesiunea a expirat" });
  await act(async () => { await expect(getBootstrap("NORD")).rejects.toMatchObject({ status: 401 }); }); expect(api.calls.filter(c => c.url.includes("bootstrap"))).toEqual([]);
  await user.type(within(sheet).getByLabelText("Parolă"), "Synthetic local password 2026"); await user.click(within(sheet).getByRole("button", { name: "Continuă" }));
  await user.type(await within(sheet).findByLabelText("Cod din aplicația de autentificare"), "123456"); await user.click(within(sheet).getByRole("button", { name: "Intră în cont" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); await act(async () => { await getBootstrap("NORD").catch(() => {}); });
  expect(api.calls.filter(c => c.url.includes("bootstrap"))).toHaveLength(1);
});

it("ignores replies to requests sent before the session ended or was renewed", async () => {
  const user = userEvent.setup(), before = deferred<Response>(), during = deferred<Response>(); let reads = 0, ended = false;
  server({ state: () => ++reads === 1 ? before.promise : during.promise, identity: () => ended ? json({}, 401) : json(fixtureIdentity()),
    onPost: c => c.url === "/auth/password" ? json({ ok: true, ticket: "fixture-password" }) : (ended = false, json({ ok: true })) });
  render(privateApp); await screen.findByText("Conținut privat");
  const old = getBootstrap("NORD").catch(() => null), late = getBootstrap("NORD").catch(() => null); ended = true;
  act(() => window.dispatchEvent(new Event(SESSION_ENDED))); const sheet = await screen.findByRole("dialog", { name: "Sesiunea a expirat" });
  await act(async () => { before.resolve(reauth()); await old; });
  expect(screen.queryByRole("dialog", { name: "Confirmă identitatea" })).toBeNull(); expect(screen.getByText("Conținut privat")).toBeInTheDocument();
  await user.type(within(sheet).getByLabelText("Parolă"), "Synthetic local password 2026"); await user.click(within(sheet).getByRole("button", { name: "Continuă" }));
  await user.type(await within(sheet).findByLabelText("Cod din aplicația de autentificare"), "123456"); await user.click(within(sheet).getByRole("button", { name: "Intră în cont" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await act(async () => { during.resolve(json({}, 401)); await late; }); expect(screen.queryByRole("dialog")).toBeNull();
});

it("returns a cancelled step-up as cancelled, with no message, to actions and downloads", async () => {
  const user = userEvent.setup(); server({ onPost: () => reauth() }); render(privateApp); await screen.findByText("Conținut privat");
  let action!: ReturnType<typeof postAccess>; act(() => { action = postAccess("token-create", { name: "test", scope: "read" }); });
  await user.click(within(await screen.findByRole("dialog", { name: "Confirmă identitatea" })).getByRole("button", { name: "Renunță" }));
  await expect(action).resolves.toEqual({ ok: false, reauth: true, cancelled: true }); await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  let file!: ReturnType<typeof postFile>; act(() => { file = postFile({ action: "payment_file_download", file_id: 7 }); });
  await user.click(within(await screen.findByRole("dialog", { name: "Confirmă identitatea" })).getByRole("button", { name: "Renunță" }));
  await expect(file).resolves.toEqual({ ok: false, reauth: true, cancelled: true });
});

it("keeps the screen when a return to the tab finds the session gone, but leaves it when another tab signs out", async () => {
  let ended = false; server({ identity: () => ended ? json({}, 401) : json(fixtureIdentity()) }); render(privateApp);
  await screen.findByText("Conținut privat"); ended = true;
  fireEvent(window, new Event("focus")); expect(await screen.findByRole("dialog", { name: "Sesiunea a expirat" })).toBeVisible(); expect(screen.getByText("Conținut privat")).toBeInTheDocument();
  fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); await waitFor(() => expect(screen.queryByText("Conținut privat")).toBeNull());
  expect(document.title).toBe("Conectare, fiscalm"); expect(base()).toBe("/web/");
});

it("ends the server session on sign-out, while unconfirmed sign-out keeps a retry available", async () => {
  const user = userEvent.setup(); let refused = true;
  const api = server({ onPost: () => json({ ok: !refused }, refused ? 503 : 200) }); render(privateApp);
  await user.click(await screen.findByRole("button", { name: "Ieși din cont" }));
  expect(await screen.findByRole("alert")).toBeVisible(); expect(screen.getByText("Conținut privat")).toBeVisible();
  refused = false; await user.click(screen.getByRole("button", { name: "Ieși din cont" }));
  expect(await screen.findByLabelText("Nume de utilizator")).toBeVisible();
  expect(api.posts()).toEqual([0, 1].map(() => ({ url: "/auth/signout", method: "POST", body: {} })));
});

it("honours Retry-After without resubmitting credentials and counts the wait down", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true }); const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const api = server({ onPost: () => new Response(JSON.stringify({ ok: false }), { status: 429, headers: { "Retry-After": "2" } }) });
  render(<PasswordCode onDone={vi.fn()} />); await account(user);
  expect(screen.getByRole("button", { name: "Continuă" })).toBeDisabled();
  await act(async () => vi.advanceTimersByTime(1000)); expect(screen.getByRole("status")).toHaveTextContent("o secundă");
  await act(async () => vi.advanceTimersByTime(1000)); expect(screen.getByRole("button", { name: "Continuă" })).toBeEnabled();
  expect(api.posts()).toHaveLength(1);
});


it("does not revive a session from a late me response after expiry", async () => {
  const pending = deferred<Response>(); server({ identity: () => pending.promise }); render(privateApp);
  act(() => window.dispatchEvent(new Event(SESSION_ENDED)));
  await screen.findByLabelText("Nume de utilizator");
  await act(async () => pending.resolve(json(fixtureIdentity())));
  expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
});

it("also leaves private content when sign-out reports an already expired session", async () => {
  const user = userEvent.setup(); server({ onPost: () => json({ ok: false }, 401) }); render(privateApp);
  await user.click(await screen.findByRole("button", { name: "Ieși din cont" }));
  expect(await screen.findByLabelText("Nume de utilizator")).toBeVisible();
  expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
});


it("keeps sign-out available while the first firm is asked", async () => {
  const user = userEvent.setup();
  server({ all: () => json({ setup: "needs_company", companies: [], live: false, can_manage_companies: true }) });
  render(<Authentication><App /></Authentication>);
  await screen.findByRole("heading", { name: "Prima firmă" });
  await user.click(await screen.findByRole("button", { name: "Ieși din cont" }));
  expect(await screen.findByLabelText("Nume de utilizator")).toBeVisible();
});


it.each(["password", "totp"])("does not open private content when the %s fetch is rejected", async phase => {
  const user = userEvent.setup();
  const api = server({ identity: () => json({}, 401), onPost: call => {
    if (phase === "totp" && call.url === "/auth/password") return json({ ok: true, ticket: "fixture-ticket" });
    return Promise.reject(new TypeError("Synthetic offline response"));
  } });
  render(privateApp); await screen.findByLabelText("Nume de utilizator"); await account(user);
  if (phase === "totp") {
    await user.type(await screen.findByLabelText("Cod din aplicația de autentificare"), "123456");
    await user.click(screen.getByRole("button", { name: "Intră în cont" }));
  }
  expect(await screen.findByRole("alert")).toBeVisible();
  expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
  expect(screen.getByLabelText(phase === "totp" ? "Cod din aplicația de autentificare" : "Parolă")).toHaveValue("");
  expect(api.posts()).toHaveLength(phase === "totp" ? 2 : 1);
});

it("keeps sign-out retryable when fetch rejects without confirming closure", async () => {
  const user = userEvent.setup(); const api = server({ onPost: () => Promise.reject(new TypeError("Synthetic offline response")) });
  render(privateApp); await user.click(await screen.findByRole("button", { name: "Ieși din cont" }));
  expect(await screen.findByRole("alert")).toBeVisible(); expect(screen.getByText("Conținut privat")).toBeVisible();
  expect(screen.getByRole("button", { name: "Ieși din cont" })).toBeEnabled();
  expect(api.posts()).toEqual([{ url: "/auth/signout", method: "POST", body: {} }]);
});


it("keeps sign-out available when firm data fails to load", async () => {
  const user = userEvent.setup(); at("?co=NORD");
  server({ state: () => json({}, 503) }); render(<Authentication><App /></Authentication>);
  expect(await screen.findByRole("alert")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Ieși din cont" }));
  expect(await screen.findByLabelText("Nume de utilizator")).toBeVisible();
});

const enrolment = { ok: true, ticket: "fixture-enrolment", secret: "A".repeat(32), uri: `otpauth://totp/fiscalm:fixture?secret=${"A".repeat(32)}&issuer=fiscalm` };
const recoveryCodes = Array.from("ABCDEFGHIJ", first => `${first}AAA-AAAA-AAAA-AAAA`);
const setupLink = `${"1".repeat(32)}.${"2".repeat(32)}`;

it("sets up a passkey with exact owner-bound bodies and keeps recovery codes behind acknowledgement", async () => {
  nativeBrowser(); const user = userEvent.setup(); window.history.replaceState(null, "", `/setup#${setupLink}`); let finished = false;
  const api = server({ identity: () => finished ? json(fixtureIdentity()) : json({ methods: { passkey: true } }, 401), onPost: c => {
    if (c.url.endsWith("/inspect")) return json({ ok: true, user: { username: "fixture" } });
    if (c.url.endsWith("/options")) return json({ ok: true, ticket: "fixture-create", publicKey: nativeCreation });
    finished = true; return json({ ok: true, codes: recoveryCodes });
  } });
  render(privateApp); await user.click(await screen.findByRole("button", { name: "Folosește parola și codul" })); await user.type(screen.getByLabelText("Parolă nouă"), "Unsent password 2026");
  await user.click(screen.getByRole("button", { name: "Folosește o cheie de acces" })); expect(screen.queryByLabelText("Parolă nouă")).toBeNull();
  expect(screen.getByLabelText("Numele cheii de acces")).toHaveValue("Dispozitiv Passkey"); await user.click(screen.getByRole("button", { name: "Configurează cheia de acces" }));
  expect(await screen.findByText(recoveryCodes[0])).toBeVisible(); expect(sessionStorage.length).toBe(0); expect(screen.queryByText("Conținut privat")).toBeNull();
  expect(screen.queryByRole("button", { name: "Folosește parola și codul" })).toBeNull();
  expect(screen.getByRole("button", { name: "Continuă" })).toBeDisabled(); fireEvent(window, new Event("focus")); expect(screen.getByText(recoveryCodes[0])).toBeVisible();
  await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Continuă" })); expect(await screen.findByText("Conținut privat")).toBeVisible();
  expect(window.location.hash).toBe(""); expect(api.posts()).toEqual([
    { url: "/auth/setup/inspect", method: "POST", body: { link: setupLink } },
    { url: "/auth/setup/passkey/options", method: "POST", body: { link: setupLink, name: "Dispozitiv Passkey" } },
    { url: "/auth/setup/passkey/finish", method: "POST", body: { link: setupLink, ticket: "fixture-create", credential_id: "-_8", client_data: "-_8", attestation: "-_8", transports: '["internal","hybrid"]' } },
  ]);
});

it.each(["refused", "unreadable", "incomplete"])("offers only an explicit fresh setup after a %s passkey finish", async failure => {
  const native = nativeBrowser(), user = userEvent.setup(); window.history.replaceState(null, "", `/setup#${setupLink}`); let options = 0, finish = 0;
  const api = server({ identity: () => json({ methods: { passkey: true } }, 401), onPost: c => {
    if (c.url.endsWith("/inspect")) return json({ ok: true, user: { username: "fixture" } });
    if (c.url.endsWith("/options")) return ++options === 2 && failure !== "refused" ? json({ ok: false }, 401) : json({ ok: true, ticket: `fixture-create-${options}`, publicKey: nativeCreation });
    return ++finish === 1 ? failure === "unreadable" ? new Response("{", { status: 200 }) : failure === "incomplete" ? json({ ok: true }) : json({ ok: false }, 401) : json({ ok: true, codes: recoveryCodes });
  } });
  render(privateApp); await screen.findByLabelText("Numele cheii de acces");
  await user.clear(screen.getByLabelText("Numele cheii de acces")); await user.type(screen.getByLabelText("Numele cheii de acces"), "Cheie de test"); await user.click(screen.getByRole("button", { name: "Configurează cheia de acces" }));
  expect(await screen.findByRole("alert")).toBeVisible(); expect(finish).toBe(1); expect(screen.queryByText("Conținut privat")).toBeNull();
  if (failure !== "refused") expect(screen.getByRole("link", { name: "Verifică accesul la cont" })).toHaveAttribute("href", "/?tab=setari&sec=cont");
  await user.click(screen.getByRole("button", { name: "Reîncepe configurarea" }));
  if (failure !== "refused") {
    await waitFor(() => expect(screen.getByRole("button", { name: "Reîncepe configurarea" })).toBeEnabled());
    expect(options).toBe(2); expect(native.create).toHaveBeenCalledTimes(1); expect(finish).toBe(1);
    expect(screen.getByRole("link", { name: "Verifică accesul la cont" })).toBeVisible();
    expect(screen.getByText(/Configurarea anterioară poate fi deja salvată/)).toBeVisible();
  }
  else { expect(await screen.findByText(recoveryCodes[0])).toBeVisible(); expect(finish).toBe(2); }
  expect(api.posts().filter(c => c.url.endsWith("/options")).map(c => c.body)).toEqual(Array.from({ length: 2 }, () => ({ link: setupLink, name: "Cheie de test" })));
});

it("aborts setup immediately on a cross-tab session change before a late native key resolves", async () => {
  const native = nativeBrowser(), proof = deferred<NativeKey>(), identity = deferred<Response>(), user = userEvent.setup(); window.history.replaceState(null, "", `/setup#${setupLink}`); let changed = false; native.create.mockReturnValue(proof.promise);
  const api = server({ identity: () => changed ? identity.promise : json({ methods: { passkey: true } }, 401), onPost: c => c.url.endsWith("/inspect") ? json({ ok: true, user: { username: "fixture" } }) : json({ ok: true, ticket: "fixture-create", publicKey: nativeCreation }) });
  render(privateApp); await screen.findByLabelText("Numele cheii de acces"); await user.clear(screen.getByLabelText("Numele cheii de acces")); await user.type(screen.getByLabelText("Numele cheii de acces"), "Cheie de test"); await user.click(screen.getByRole("button", { name: "Configurează cheia de acces" }));
  await waitFor(() => expect(native.create).toHaveBeenCalledOnce()); expect(screen.getByRole("button", { name: "Folosește parola și codul" })).toBeDisabled(); changed = true; fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); expect(native.create.mock.calls[0][0].signal.aborted).toBe(true); expect(sessionStorage.length).toBe(0);
  await act(async () => proof.resolve(new NativeKey(new NativeAttestation()))); expect(api.posts().filter(c => c.url.endsWith("/finish"))).toEqual([]);
  await act(async () => identity.resolve(json(fixtureIdentity("member")))); expect(await screen.findByText("Conținut privat")).toBeVisible();
});
const newPassword = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.type(await screen.findByLabelText("Parolă nouă"), "Synthetic local password 2026");
  await user.type(screen.getByLabelText("Repetă parola"), "Synthetic local password 2026");
  await user.click(screen.getByRole("button", { name: "Configurează autentificarea" }));
};

it("hydrates the server shell at a setup URL without changing the first client markup", async () => {
  window.history.replaceState(null, "", `/setup#${setupLink}`);
  server({ identity: () => json({}, 401), onPost: () => json({ ok: true, user: { username: "fixture" } }) });
  const node = document.createElement("div"); document.body.append(node);
  const browserWindow = window; vi.stubGlobal("window", undefined);
  node.innerHTML = renderToString(privateApp); vi.stubGlobal("window", browserWindow);
  expect(node.querySelector(".auth-enrollment")).toBeNull();
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  const recover = vi.fn(); let root: ReturnType<typeof hydrateRoot>;
  await act(async () => { root = hydrateRoot(node, privateApp, { onRecoverableError: recover }); });
  expect(await within(node).findByLabelText("Parolă nouă")).toBeVisible();
  expect(recover).not.toHaveBeenCalled(); expect(errors).not.toHaveBeenCalled();
  await act(async () => root.unmount()); node.remove();
});

it("resumes two reloads with only the link, restarting unfinished factor enrollment", async () => {
  vi.spyOn(performance, "getEntriesByType").mockReturnValue([{ type: "reload" } as PerformanceNavigationTiming]);
  const user = userEvent.setup(); window.history.replaceState(null, "", `/setup#${setupLink}`);
  const api = server({ identity: () => json({}, 401), onPost: call => json(call.url.endsWith("/begin") ? enrolment : { ok: true, user: { username: "fixture" } }) });
  let page = render(privateApp); await newPassword(user);
  await screen.findByLabelText("Cod din aplicația de autentificare");
  for (let i = 0; i < 2; i++) {
    page.unmount(); page = render(privateApp);
    expect(await screen.findByLabelText("Parolă nouă")).toHaveValue("");
    expect(screen.queryByLabelText("Cod din aplicația de autentificare")).toBeNull();
  }
  expect(api.posts()).toEqual([
    { url: "/auth/setup/inspect", method: "POST", body: { link: setupLink } },
    { url: "/auth/setup/begin", method: "POST", body: { link: setupLink, password: "Synthetic local password 2026" } },
    ...Array.from({ length: 2 }, () => ({ url: "/auth/setup/inspect", method: "POST", body: { link: setupLink } })),
  ]);
  const saved = sessionStorage.getItem("fiscalm:setup-continuation")!;
  expect(Object.keys(JSON.parse(saved)).sort()).toEqual(["link", "session", "until"]);
});

it.each(["absent", "expired", "session", "navigation", "invalid"] as const)("does not resume a %s continuation or inspect an empty link", async reason => {
  vi.spyOn(performance, "getEntriesByType").mockReturnValue([{ type: reason === "navigation" ? "navigate" : "reload" } as PerformanceNavigationTiming]);
  window.history.replaceState(null, "", "/setup");
  if (reason !== "absent") sessionStorage.setItem("fiscalm:setup-continuation", JSON.stringify({
    link: reason === "invalid" ? "bad" : setupLink, until: Date.now() + (reason === "expired" ? -1 : 60000), session: reason === "session" ? "previous" : null,
  }));
  const api = server({ identity: () => json({}, 401) }); render(privateApp);
  expect(await screen.findByRole("form", { name: "Conectare" })).toBeVisible();
  expect(api.posts()).toEqual([]); expect(sessionStorage.length).toBe(0);
});

it.each([false, true])("binds setup, retains its code gate, and clears it for an external session change=%s", async external => {
  const user = userEvent.setup(); let finished = false; let wrong = true;
  window.history.replaceState(null, "", `/setup#${setupLink}`);
  const api = server({ identity: () => { const identity = fixtureIdentity(finished ? "member" : "admin"); identity.me.id = finished ? "fixture-b" : "fixture-a"; return json(identity); }, onPost: call => {
    if (call.url === "/auth/setup/inspect") return json({ ok: true, user: { username: "fixture" } });
    if (call.url === "/auth/setup/begin") return json(enrolment);
    if (wrong) return json({ ok: false, message: "Refuz de test" }, 401);
    finished = true; return json({ ok: true, codes: recoveryCodes });
  } });
  render(<Authentication><p>Conținut privat</p><IdentityContext.Consumer>{session => <span>{session?.identity.me.id}</span>}</IdentityContext.Consumer></Authentication>); await newPassword(user);
  expect(window.location.hash).toBe(""); expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
  const input = await screen.findByLabelText("Cod din aplicația de autentificare"); expect(input).toHaveFocus();
  expect(screen.getByRole("img", { name: "Cod QR pentru aplicația de autentificare" })).toBeVisible();
  await user.type(input, "123456"); await user.click(screen.getByRole("button", { name: "Confirmă codul" }));
  expect(await screen.findByRole("alert")).toBeVisible(); expect(input).toHaveValue("");
  wrong = false; await user.type(input, "654321"); await user.click(screen.getByRole("button", { name: "Confirmă codul" }));
  expect(await screen.findByText(recoveryCodes[0])).toBeVisible(); expect(sessionStorage.length).toBe(0); expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Continuă" })).toBeDisabled();
  fireEvent(window, new Event("focus")); expect(screen.getByText(recoveryCodes[0])).toBeVisible();
  if (external) fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED }));
  else { await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Continuă" })); }
  expect(await screen.findByText("Conținut privat")).toBeVisible(); expect(screen.queryByText(recoveryCodes[0])).toBeNull(); expect(screen.getByText("fixture-b")).toBeVisible(); expect(screen.queryByText("fixture-a")).not.toBeInTheDocument(); expect(window.location.pathname).toBe("/");
  expect(api.posts()).toEqual([
    { url: "/auth/setup/inspect", method: "POST", body: { link: setupLink } },
    { url: "/auth/setup/begin", method: "POST", body: { link: setupLink, password: "Synthetic local password 2026" } },
    ...["123456", "654321"].map(code => ({ url: "/auth/setup/finish", method: "POST", body: { link: setupLink, ticket: enrolment.ticket, code } })),
  ]);
  expect(api.calls.filter(c => c.url.includes(setupLink))).toEqual([]);
});

it.each([401, 503])("refuses an unusable setup link and retries an unconfirmed inspection (%s)", async status => {
  const user = userEvent.setup(); window.history.replaceState(null, "", `/setup#${setupLink}`);
  const api = server({ identity: () => json({}, 401), onPost: () => json({ ok: false }, status) }); render(privateApp);
  expect(await screen.findByRole("alert")).toBeVisible(); expect(screen.queryByLabelText("Parolă nouă")).not.toBeInTheDocument();
  expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument(); expect(window.location.hash).toBe("");
  if (status === 401) expect(sessionStorage.length).toBe(0);
  if (status === 503) { await user.click(screen.getByRole("button", { name: "Reîncearcă" })); await screen.findByRole("alert"); expect(api.posts()).toHaveLength(2); }
});

const recoveryEntry = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(await screen.findByRole("button", { name: "Nu mai am acces" })); expect(screen.getByRole("heading", { name: "Recuperează contul" })).toBeVisible();
  await user.type(screen.getByLabelText("Nume de utilizator"), "example"); await user.type(screen.getByLabelText("Cod de recuperare"), recoveryCodes[0]);
  await user.click(screen.getByRole("button", { name: "Continuă" }));
};

it.each([false, true])("recovers a password account with code then password and signs in after the saved codes, external session change=%s", async external => {
  const user = userEvent.setup(); let session: "none" | "recovery" | "normal" = "none";
  const restricted = fixtureIdentity(); restricted.auth.recovery_only = true;
  const api = server({ identity: () => session === "recovery" ? json(restricted) : session === "normal" ? json(fixtureIdentity()) : json({}, 401), onPost: call => {
    if (call.url === "/auth/recover/code") return json({ ok: true, ticket: "fixture-recovery" });
    if (call.url === "/auth/recover/password") { session = "recovery"; return json({ ok: true, recovery: true }); }
    if (call.url === "/auth/recover/begin") return json(enrolment);
    session = external ? "none" : "normal"; return json({ ok: true, codes: recoveryCodes });
  } });
  render(privateApp); await recoveryEntry(user);
  expect(await screen.findByText("example")).toBeVisible(); expect(screen.queryByLabelText("Cod de recuperare")).toBeNull();
  await user.type(screen.getByLabelText("Parolă"), "Synthetic local password 2026"); await user.click(screen.getByRole("button", { name: "Continuă" }));
  await user.type(await screen.findByLabelText("Cod din aplicația de autentificare"), "654321");
  await user.click(screen.getByRole("button", { name: "Confirmă codul" })); await screen.findByText(recoveryCodes[0]);
  expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
  fireEvent(window, new Event("focus")); expect(screen.getByText(recoveryCodes[0])).toBeVisible();
  if (external) { fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); expect(await screen.findByLabelText("Nume de utilizator")).toBeVisible(); }
  else { await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Continuă" })); expect(await screen.findByText("Conținut privat")).toBeVisible(); }
  expect(screen.queryByText(recoveryCodes[0])).not.toBeInTheDocument();
  expect(api.posts()).toEqual([
    { url: "/auth/recover/code", method: "POST", body: { username: "example", code: recoveryCodes[0] } },
    { url: "/auth/recover/password", method: "POST", body: { username: "example", ticket: "fixture-recovery", password: "Synthetic local password 2026" } },
    { url: "/auth/recover/begin", method: "POST", body: {} },
    { url: "/auth/recover/finish", method: "POST", body: { ticket: enrolment.ticket, code: "654321" } },
  ]);
});

it("discards an idle recovery password step when another tab changes the session", async () => {
  const user = userEvent.setup(); const api = server({ identity: () => json({}, 401), onPost: () => json({ ok: true, ticket: "fixture-recovery" }) });
  render(privateApp); await recoveryEntry(user); await user.type(await screen.findByLabelText("Parolă"), "Unsent password 2026");
  fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED }));
  expect(await screen.findByRole("button", { name: "Nu mai am acces" })).toBeVisible(); expect(screen.queryByDisplayValue("Unsent password 2026")).toBeNull();
  expect(api.posts()).toHaveLength(1);
});

it("offers one recovery link on every sign-in step and none in step-up", async () => {
  nativeBrowser(); const user = userEvent.setup(); localStorage.setItem("fiscalm:sign-in-method", "password");
  server({ identity: () => json({ methods: { passkey: true } }, 401), onPost: () => json({ ok: true, ticket: "fixture-password" }) });
  render(privateApp); await screen.findByLabelText("Nume de utilizator"); await account(user);
  await screen.findByLabelText("Cod din aplicația de autentificare");
  expect(screen.getAllByRole("button", { name: /recuper|acces/i }).map(b => b.textContent)).toEqual(["Folosește o cheie de acces", "Nu mai am acces"]);
});

const restrictedKeyIdentity = () => {
  const identity = fixtureIdentity(); identity.auth = { via: "cookie", method: "recovery_code", recovery_only: true };
  identity.factors.password_totp = false; return identity;
};

it("recovers a key-only account without a password, replaces its key and requires fresh sign-in after saved codes", async () => {
  nativeBrowser(); const user = userEvent.setup(); let recovery = false;
  const api = server({ identity: () => recovery ? json(restrictedKeyIdentity()) : json({ methods: { passkey: true } }, 401), onPost: c => {
    if (c.url === "/auth/recover/code") { recovery = true; return json({ ok: true, recovery: true }); }
    if (c.url.endsWith("/options")) return json({ ok: true, ticket: "fixture-recover-key", publicKey: nativeCreation });
    recovery = false; return json({ ok: true, codes: recoveryCodes });
  } });
  render(privateApp); await recoveryEntry(user); expect(await screen.findByLabelText("Numele cheii de acces")).toHaveValue("Dispozitiv Passkey"); await user.clear(screen.getByLabelText("Numele cheii de acces")); await user.type(screen.getByLabelText("Numele cheii de acces"), "Cheie nouă");
  expect(screen.queryByText("Conținut privat")).toBeNull(); expect(screen.queryByLabelText("Parolă")).toBeNull(); expect(screen.queryByLabelText("Cod din aplicația de autentificare")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Configurează cheia de acces" })); expect(await screen.findByText(recoveryCodes[0])).toBeVisible();
  fireEvent(window, new Event("focus")); expect(screen.getByRole("button", { name: "Continuă" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Continuă" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Cheia de acces a fost configurată."); expect(screen.queryByText("Conținut privat")).toBeNull();
  expect(api.posts()).toEqual([
    { url: "/auth/recover/code", method: "POST", body: { username: "example", code: recoveryCodes[0] } },
    { url: "/auth/recover/passkey/options", method: "POST", body: { name: "Cheie nouă" } },
    { url: "/auth/recover/passkey/finish", method: "POST", body: { ticket: "fixture-recover-key", credential_id: "-_8", client_data: "-_8", attestation: "-_8", transports: '["internal","hybrid"]' } },
  ]);
});

it.each(["refused", "unreadable"])("keeps a %s code-only recovery outside finance and never resends its code automatically", async failure => {
  nativeBrowser(); const user = userEvent.setup(); let recovery = false;
  const api = server({ identity: () => recovery ? json(restrictedKeyIdentity()) : json({ methods: { passkey: true } }, 401), onPost: () => {
    if (failure === "refused") return json({ ok: false, message: "Refuz de test" }, 401);
    recovery = true; return new Response("{", { status: 200 });
  } });
  render(privateApp); await recoveryEntry(user);
  expect(await screen.findByRole("alert")).toBeVisible(); expect(screen.getByLabelText("Cod de recuperare")).toHaveValue(""); expect(screen.queryByText("Conținut privat")).toBeNull();
  if (failure === "unreadable") { await user.click(screen.getByRole("button", { name: "Verifică recuperarea contului" })); expect(await screen.findByLabelText("Numele cheii de acces")).toBeVisible(); }
  else expect(screen.queryByRole("button", { name: "Verifică recuperarea contului" })).toBeNull();
  expect(api.posts()).toEqual([{ url: "/auth/recover/code", method: "POST", body: { username: "example", code: recoveryCodes[0] } }]);
});

it.each(["recovery", "recovery_code"] as const)("chooses the replacement UI from session provenance %s before any financial read", async method => {
  nativeBrowser(); const identity = restrictedKeyIdentity(); identity.auth.method = method;
  const api = server({ identity: () => json(identity), onPost: () => json(enrolment) }); render(privateApp); await screen.findByRole("heading", { name: "Recuperează contul" });
  if (method === "recovery_code") expect(screen.getByLabelText("Numele cheii de acces")).toBeVisible(); else expect(await screen.findByLabelText("Cod din aplicația de autentificare")).toBeVisible();
  expect(screen.queryByText("Conținut privat")).toBeNull(); expect(api.calls.filter(c => c.method === "GET")).toEqual([{ url: "/web/api/me", method: "GET", body: undefined }]);
  expect(api.posts().map(c => c.url)).toEqual(method === "recovery" ? ["/auth/recover/begin"] : []);
});

it("does not complete a late recovery-code response after a cross-tab account change", async () => {
  nativeBrowser(); const user = userEvent.setup(), response = deferred<Response>(), identity = deferred<Response>(); let changed = false;
  const api = server({ identity: () => changed ? identity.promise : json({ methods: { passkey: true } }, 401), onPost: () => response.promise }); render(privateApp);
  await recoveryEntry(user);
  changed = true; fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); expect(vi.mocked(fetch).mock.calls.find(c => c[0] === "/auth/recover/code")?.[1]?.signal?.aborted).toBe(true);
  await act(async () => response.resolve(json({ ok: true }))); expect(api.calls.filter(c => c.method === "GET")).toHaveLength(2);
  await act(async () => identity.resolve(json(fixtureIdentity("readonly")))); expect(await screen.findByText("Conținut privat")).toBeVisible(); expect(screen.queryByLabelText("Numele cheii de acces")).toBeNull();
});

it("keeps code copying failures honest and downloads the one-time set locally", async () => {
  const user = userEvent.setup(); const onDone = vi.fn(async () => {});
  const clipboard = vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("Synthetic clipboard refusal"));
  const create = vi.fn((blob: Blob) => { expect(blob.type).toBe("text/plain;charset=utf-8"); return "blob:fixture-codes"; }), revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  render(<Codes codes={recoveryCodes} onDone={onDone} />);
  await user.click(screen.getByRole("button", { name: "Copiază codurile" }));
  expect(screen.getByRole("status")).toHaveTextContent("Codurile nu au putut fi copiate.");
  expect(clipboard).toHaveBeenCalledWith(recoveryCodes.join("\n"));
  await user.click(screen.getByRole("button", { name: "Salvează fișierul" }));
  const anchor = click.mock.instances[0] as HTMLAnchorElement;
  expect(anchor.href).toBe("blob:fixture-codes"); expect(anchor.download).toBe("fiscalm-coduri-recuperare.txt");
  const text = await new Promise(resolve => { const r = new FileReader(); r.onload = () => resolve(r.result); r.readAsText(create.mock.calls[0][0]); });
  expect(text).toBe(recoveryCodes.join("\n") + "\n"); expect(revoke).toHaveBeenCalledWith("blob:fixture-codes");
  expect(screen.getByRole("button", { name: "Continuă" })).toBeDisabled(); expect(onDone).not.toHaveBeenCalled();
  await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Continuă" })); expect(onDone).toHaveBeenCalledOnce();
});

it("replaces the setup form and clears the address fragment when another link opens in the same tab", async () => {
  const user = userEvent.setup(); const other = `${"3".repeat(32)}.${"4".repeat(32)}`;
  window.history.replaceState(null, "", `/setup#${setupLink}`);
  const api = server({ identity: () => json({}, 401), onPost: call => json({ ok: true, user: { username: call.body?.link === other ? "fixture-b" : "fixture-a" } }) });
  render(privateApp); await user.type(await screen.findByLabelText("Parolă nouă"), "Unsubmitted password");
  act(() => { window.location.hash = `#${other}`; });
  expect(await screen.findByText("fixture-b")).toBeVisible();
  expect(screen.getByLabelText("Parolă nouă")).toHaveValue(""); expect(window.location.hash).toBe("");
  expect(api.posts().map(c => c.body)).toEqual([{ link: setupLink }, { link: other }]);
  expect(JSON.parse(sessionStorage.getItem("fiscalm:setup-continuation")!).link).toBe(other);
});

it.each(["setup", "recover"])("explicitly restarts an expired or exhausted %s enrolment with a fresh ticket", async flow => {
  const user = userEvent.setup(); let starts = 0;
  if (flow === "setup") window.history.replaceState(null, "", `/setup#${setupLink}`);
  const identity = fixtureIdentity(); identity.auth.recovery_only = true;
  const api = server({ identity: () => flow === "setup" ? json({}, 401) : json(identity), onPost: call => {
    if (call.url.endsWith("/inspect")) return json({ ok: true, user: { username: "fixture" } });
    if (call.url.endsWith("/begin")) return json({ ...enrolment, ticket: `ticket-${++starts}` });
    return call.body?.ticket === "ticket-1" ? json({ ok: false }, 401) : json({ ok: true, codes: recoveryCodes });
  } });
  render(privateApp);
  // Recovery begins on its own, and again when the person restarts it; setup waits for the new password each time.
  const begin = async () => { if (flow === "setup") await newPassword(user); };
  await begin(); await user.type(await screen.findByLabelText("Cod din aplicația de autentificare"), "123456");
  await user.click(screen.getByRole("button", { name: "Confirmă codul" })); await screen.findByRole("alert");
  expect(starts).toBe(1); expect(api.posts().filter(c => c.url.endsWith("/finish"))).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "Reîncepe configurarea" }));
  if (flow === "setup") expect(screen.queryByRole("img", { name: "Cod QR pentru aplicația de autentificare" })).not.toBeInTheDocument();
  await waitFor(() => expect(starts).toBe(flow === "setup" ? 1 : 2)); // A restart never retries an unconfirmed finish.
  await begin(); await user.type(await screen.findByLabelText("Cod din aplicația de autentificare"), "654321");
  await user.click(screen.getByRole("button", { name: "Confirmă codul" })); expect(await screen.findByText(recoveryCodes[0])).toBeVisible();
  expect(screen.queryByText("Conținut privat")).not.toBeInTheDocument();
  expect(api.posts().filter(c => c.url.endsWith("/begin")).map(c => c.body)).toEqual([0, 1].map(() => flow === "setup" ? { link: setupLink, password: "Synthetic local password 2026" } : {}));
  expect(api.posts().filter(c => c.url.endsWith("/finish")).map(c => c.body)).toEqual(["123456", "654321"].map((code, i) => ({ ...(flow === "setup" ? { link: setupLink } : {}), ticket: `ticket-${i + 1}`, code })));
});

it("honours Retry-After before another setup-link inspection", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true }); window.history.replaceState(null, "", `/setup#${setupLink}`);
  const api = server({ identity: () => json({}, 401), onPost: () => new Response(JSON.stringify({ ok: false }), { status: 429, headers: { "Retry-After": "2" } }) });
  render(privateApp); await screen.findByRole("alert"); const retry = screen.getByRole("button", { name: "Reîncearcă" });
  expect(retry).toBeDisabled(); await act(async () => vi.advanceTimersByTime(1000)); await act(async () => vi.advanceTimersByTime(1000)); expect(retry).toBeEnabled();
  expect(api.posts()).toHaveLength(1);
});

it.each([
  ["admin", ["mark-paid", "rule-pause", "token-create"]],
  ["member", ["mark-paid", "token-create"]],
  ["readonly", ["token-create"]],
  ["none", []],
  ["recovery", []],
] as const)("shows shared action controls only for the %s principal's server capabilities", async (role, visible) => {
  const user = userEvent.setup(); const write = vi.fn(async () => {});
  const state = fixture();
  const ctx = { view: state, route: parse("?co=NORD"), go: vi.fn(), act: write, run: async () => null, busy: false, say: vi.fn(), refresh: async () => null };
  const actions: Action[] = ["mark-paid", "rule-pause", "token-create"];
  const controls = <AppCtx.Provider value={ctx}>{actions.map(action => <Act key={action} action={action} body={{ id: "fixture-item" }} label={action} />)}</AppCtx.Provider>;
  if (role === "none") render(controls);
  else {
    const identity = fixtureIdentity(role === "recovery" ? "admin" : role as UserRole);
    identity.auth.recovery_only = role === "recovery";
    renderSignedIn(controls, { identity });
  }
  expect(screen.queryAllByRole("button").map(button => button.textContent)).toEqual(visible);
  if (visible.length) {
    await user.click(screen.getByRole("button", { name: visible[0] }));
    expect(write).toHaveBeenCalledWith("NORD", visible[0], { id: "fixture-item" }, undefined);
  }
});

it.each(["admin", "member", "readonly"] as const)("gates new-invoice controls and its direct URL for %s", async role => {
  at("?co=NORD&new=1"); const api = server();
  renderSignedIn(<App />, { identity: fixtureIdentity(role) });
  await screen.findByRole("navigation", { name: "Secțiuni" });
  if (role === "readonly") {
    expect(screen.queryByRole("button", { name: "Nou" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.calls.some(c => c.url.includes("api/invoicing"))).toBe(false);
  } else {
    expect(screen.getByRole("button", { name: "Nou" })).toBeInTheDocument();
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  }
});


const reauth = () => json({ ok: false, reauth: true, message: "Confirmă din nou identitatea ca să continui." }, 401);
const sensitiveBody = { co: "NORD", name: "Conexiune de test", scope: "read" };
const proveIdentity = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.type(screen.getByLabelText("Parolă"), "Synthetic local password 2026");
  await user.click(screen.getByRole("button", { name: "Continuă" }));
  await user.type(await screen.findByLabelText("Cod din aplicația de autentificare"), "654321");
  await user.click(screen.getByRole("button", { name: "Confirmă identitatea" }));
};

it("keeps the private form, requires both factors and retries exactly once with the original body", async () => {
  const user = userEvent.setup(); let writes = 0, passwords = 0, codes = 0;
  const api = server({ onPost: c => {
    if (c.url === "/web/api/token-create") return ++writes === 1 ? reauth() : json({ ok: true, state: null });
    if (c.url === "/auth/step-up/password") return ++passwords === 1 ? json({ ok: false, message: "Refuz de test" }, 401) : json({ ok: true, ticket: "fixture-step-up" });
    return ++codes === 1 ? json({ ok: false, message: "Refuz de test" }, 401) : json({ ok: true });
  } });
  render(<Authentication><input aria-label="Denumire" defaultValue="" /></Authentication>);
  await user.type(await screen.findByLabelText("Denumire"), "Formular păstrat");
  let request!: ReturnType<typeof postAccess>;
  act(() => { request = postAccess("token-create", sensitiveBody); });
  await screen.findByRole("dialog", { name: "Confirmă identitatea" });
  expect(screen.getByLabelText("Parolă")).toHaveFocus(); expect(screen.getByLabelText("Denumire")).toHaveValue("Formular păstrat");
  expect(screen.queryByLabelText("Nume de utilizator")).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Parolă"), "Wrong password 2026"); await user.click(screen.getByRole("button", { name: "Continuă" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Refuz de test"); expect(writes).toBe(1);
  await proveIdentity(user); expect(await screen.findByRole("alert")).toHaveTextContent("Refuz de test"); expect(writes).toBe(1);
  expect(screen.queryByRole("button", { name: /recuperare|alt cont/ })).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Cod din aplicația de autentificare"), "654321"); await user.click(screen.getByRole("button", { name: "Confirmă identitatea" }));
  await expect(request).resolves.toMatchObject({ ok: true });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(screen.getByLabelText("Denumire")).toHaveValue("Formular păstrat");
  expect(api.posts().map(c => [c.url, c.body])).toEqual([
    ["/web/api/token-create", sensitiveBody], ["/auth/step-up/password", { password: "Wrong password 2026" }],
    ["/auth/step-up/password", { password: "Synthetic local password 2026" }],
    ["/auth/step-up/totp", { ticket: "fixture-step-up", code: "654321" }],
    ["/auth/step-up/totp", { ticket: "fixture-step-up", code: "654321" }], ["/web/api/token-create", sensitiveBody],
  ]);
});

it("cancels only the top confirmation sheet and restores the underlying form and focus", async () => {
  const closed = vi.fn(); const api = server({ onPost: reauth });
  let request!: ReturnType<typeof postAccess>;
  render(<Authentication><Sheet label="Formular" onClose={closed}><input aria-label="Denumire" defaultValue="Păstrat" />
    <button onClick={() => { request = postAccess("token-create", sensitiveBody); }}>Salvează</button></Sheet></Authentication>);
  await userEvent.click(await screen.findByRole("button", { name: "Salvează" }));
  await screen.findByRole("dialog", { name: "Confirmă identitatea" });
  expect(screen.getByRole("dialog", { name: "Formular" }).closest("[inert]")).not.toBeNull();
  fireEvent.keyDown(document, { key: "Escape" }); await expect(request).resolves.toMatchObject({ ok: false });
  expect(closed).not.toHaveBeenCalled(); expect(screen.getByRole("button", { name: "Salvează" })).toHaveFocus();
  expect(screen.getByRole("dialog", { name: "Formular" }).closest("[inert]")).toBeNull();
  expect(api.posts()).toHaveLength(1); fireEvent.keyDown(document, { key: "Escape" }); expect(closed).toHaveBeenCalledOnce();
});

it.each(["before", "after"])("does not repeat the operation for another account detected %s proof", async phase => {
  const user = userEvent.setup(); let reads = 0;
  const other = fixtureIdentity("readonly"); other.me.id = "fixture-other-user";
  const api = server({ identity: () => json(++reads >= (phase === "before" ? 2 : 3) ? other : fixtureIdentity()),
    onPost: c => c.url === "/web/api/token-create" ? reauth() : json({ ok: true, ticket: "fixture-step-up" }) });
  render(<Authentication><p>Conținut privat</p><input aria-label="Denumire" defaultValue="" /></Authentication>); await screen.findByText("Conținut privat");
  await user.type(screen.getByLabelText("Denumire"), "Formularul primului cont");
  let request!: ReturnType<typeof postAccess>; act(() => { request = postAccess("token-create", sensitiveBody); });
  if (phase === "after") { await screen.findByRole("dialog"); await proveIdentity(user); }
  await expect(request).resolves.toMatchObject({ ok: false });
  expect(api.posts().filter(c => c.url === "/web/api/token-create")).toHaveLength(1);
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByLabelText("Denumire")).toHaveValue("");
});

it("listens for a step-up as soon as the signed-in screen mounts", async () => {
  let handled: boolean | undefined;
  // A child's mount effect runs before any passive effect of the screen around it.
  const Asks = () => { useEffect(() => { handled = !window.dispatchEvent(new CustomEvent(REAUTHENTICATE, { cancelable: true, detail: { finish: () => {} } })); }, []); return <p>Conținut privat</p>; };
  server(); render(<Authentication><Asks /></Authentication>); await waitFor(() => expect(handled).toBe(true));
});

it("does not open another confirmation loop when the retried request is refused again", async () => {
  const user = userEvent.setup();
  const api = server({ onPost: c => c.url === "/web/api/token-create" ? reauth() : json({ ok: true, ticket: "fixture-step-up" }) });
  render(privateApp); await screen.findByText("Conținut privat");
  let request!: ReturnType<typeof postAccess>; act(() => { request = postAccess("token-create", sensitiveBody); });
  await screen.findByRole("dialog"); await proveIdentity(user); await expect(request).resolves.toMatchObject({ ok: false });
  expect(api.posts().filter(c => c.url === "/web/api/token-create")).toHaveLength(2);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(screen.getByText("Conținut privat")).toBeVisible();
});


it.each([
  ["page", "before"], ["layout", "before"],
  ["page", "after"], ["layout", "after"],
])("cancels a request when its source %s is replaced %s the refusal", async (kind, timing) => {
  let wide = false, resize = () => {};
  vi.stubGlobal("matchMedia", (media: string) => ({ matches: wide, media,
    addEventListener(event: string, listener: () => void) { if (event === "change") resize = listener; }, removeEventListener() {},
  }));
  const slow = deferred<Response>();
  at("?co=NORD&tab=setari&sec=acces"); const api = server({ onPost: () => timing === "before" ? slow.promise : reauth() });
  render(<Authentication><App /></Authentication>); await screen.findByRole("heading", { name: "Linkuri și chei API" });
  let request!: ReturnType<typeof postAccess>; act(() => { request = postAccess("token-create", sensitiveBody); });
  if (timing === "after") await screen.findByRole("dialog", { name: "Confirmă identitatea" });
  act(() => { if (kind === "page") { at("?co=NORD&tab=plati"); window.dispatchEvent(new PopStateEvent("popstate")); } else { wide = true; resize(); } });
  if (timing === "before") await act(async () => slow.resolve(reauth()));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await expect(request).resolves.toMatchObject({ ok: false });
  expect(screen.queryByLabelText("Nume de utilizator")).not.toBeInTheDocument(); expect(api.posts()).toHaveLength(1);
});


const sessionMetadata = (current: boolean): Identity["sessions"][number] => ({ id: current ? "a".repeat(64) : "b".repeat(64), current,
  method: "password_totp", created_at: "2026-11-01T09:00:00+00:00", last_seen_at: "2026-11-04T12:30:00+00:00", device: "Navigare de test", ip: null });

it.each(["admin", "member", "readonly"] as UserRole[])("shows own account and sessions to %s without user-management reads", async role => {
  at("?co=NORD&tab=setari&sec=cont"); const identity = { ...fixtureIdentity(role), sessions: [sessionMetadata(true), sessionMetadata(false)] };
  const api = server({ identity: () => json(identity) }); renderSignedIn(<App />, { identity });
  expect(await screen.findByText("Sesiunea curentă")).toBeVisible(); expect(screen.getByText("Altă sesiune")).toBeVisible();
  expect(screen.getByRole("button", { name: "Ieși din cont" })).toBeVisible(); expect(api.calls.some(c => c.url.includes("api/users"))).toBe(false);
  for (const name of ["Schimbă parola", "Schimbă aplicația de autentificare", "Înlocuiește codurile de recuperare"]) expect(screen.getByRole("button", { name })).toBeVisible();
});

it("shows Cont on a phone inside the app, with the current session and a count of the person's API keys", async () => {
  at("?co=NORD&tab=setari&sec=cont"); const identity = fixtureIdentity("member");
  const keys = [{ id: "k1", user_id: identity.me.id, revoked_at: null }, { id: "k2", user_id: identity.me.id, revoked_at: "2026-11-01T00:00:00Z" }, { id: "k3", user_id: "someone-else", revoked_at: null }];
  server({ identity: () => json(identity), tokens: () => json(keys) }); renderSignedIn(<App />, { identity });
  expect(await screen.findByText("Sesiunea curentă")).toBeVisible(); expect(screen.getByRole("navigation", { name: "Secțiuni" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Înapoi la Setări" })).toHaveAttribute("href", "?co=NORD&tab=setari");
  expect(await screen.findByRole("link", { name: /^Chei API.*1 cheie activă/ })).toHaveAttribute("href", "?co=NORD&tab=setari&sec=acces");
  expect(screen.queryByRole("region", { name: "Chei API" })).not.toBeInTheDocument(); expect(screen.queryByRole("button", { name: "Actualizează" })).not.toBeInTheDocument();
});

it.each(["late", "failed, then read again"])("keeps one Cont when the state arrives %s, so recovery codes shown on it stay", async (kind) => {
  at("?co=NORD&tab=setari&sec=cont"); const user = userEvent.setup(), state = deferred<Response>(); let reads = 0;
  const api = server({ state: () => kind === "late" ? state.promise : ++reads === 1 ? json({}, 500) : json(fixture()), onPost: () => json({ ok: true, codes: recoveryCodes }) });
  renderSignedIn(<App />);
  if (kind === "late") {
    await waitFor(() => expect(api.calls.some(c => c.url.includes("bootstrap"))).toBe(true)); expect(screen.queryByRole("heading", { name: "Cont" })).not.toBeInTheDocument();
    await act(async () => state.resolve(json(fixture())));
  }
  await user.click(await screen.findByRole("button", { name: "Înlocuiește codurile de recuperare" })); await user.click(screen.getByRole("button", { name: "Generează coduri noi" }));
  expect(await screen.findByText(recoveryCodes[0])).toBeVisible();
  if (kind !== "late") { await act(async () => { document.dispatchEvent(new Event("visibilitychange")); }); await waitFor(() => expect(reads).toBe(2)); }
  expect(screen.getByText(recoveryCodes[0])).toBeVisible(); expect(!!screen.queryByRole("navigation", { name: "Secțiuni" })).toBe(kind === "late");
  expect(api.posts()).toEqual([{ url: "/auth/me/recovery-codes", method: "POST", body: {} }]);
});

it.each(["admin", "member", "readonly"] as UserRole[])("manages only owned passkeys with exact proofs and bodies for %s", async role => {
  nativeBrowser(); const user = userEvent.setup(), identity = fixtureIdentity(role); identity.factors.password_totp = false; identity.factors.passkeys = [passkeyMetadata("old")];
  identity.factors.passkeys[0].backup_eligible = role !== "admin"; identity.factors.passkeys[0].backed_up = role === "readonly";
  const api = server({ identity: () => json(identity), onPost: c => {
    if (c.url.endsWith("/options")) return json({ ok: true, ticket: "fixture-own-key", publicKey: nativeCreation });
    if (c.url.endsWith("/finish")) identity.factors.passkeys.push(passkeyMetadata());
    if (c.url.endsWith("/rename")) identity.factors.passkeys[1].name = c.body?.name as string;
    if (c.url.endsWith("/remove")) identity.factors.passkeys = identity.factors.passkeys.filter(k => k.id !== c.body?.credential_id);
    return json({ ok: true });
  } }); renderSignedIn(<UserAccount back="/" />, { identity });
  const section = within(await screen.findByRole("region", { name: "Chei de acces (passkey)" })); expect(section.getByRole("button", { name: "Elimină cheia" })).toBeDisabled();
  expect(section.getByText(role === "readonly" ? "Cheie sincronizată." : role === "member" ? "Cheia permite sincronizarea." : "Cheie păstrată pe dispozitiv.")).toBeVisible();
  await user.click(section.getByRole("button", { name: "Adaugă o cheie de acces" })); expect(section.getByLabelText("Numele cheii de acces")).toHaveValue("Dispozitiv Passkey"); await user.clear(section.getByLabelText("Numele cheii de acces")); await user.type(section.getByLabelText("Numele cheii de acces"), "Cheie nouă");
  await user.click(section.getByRole("button", { name: "Configurează cheia de acces" })); await waitFor(() => expect(section.getAllByRole("button", { name: "Redenumește" })).toHaveLength(2));
  await user.click(section.getAllByRole("button", { name: "Redenumește" })[1]); const input = section.getByLabelText("Numele cheii de acces"); expect(input).toHaveValue("Cheie de test"); await user.clear(input); await user.type(input, "Cheie secundară");
  await user.click(section.getByRole("button", { name: "Salvează numele" })); await section.findByText("Cheie secundară");
  await user.click(section.getAllByRole("button", { name: "Elimină cheia" })[0]);
  expect(await answer()).toMatch(/^Elimini cheia de acces „.+”\? Nu mai poate fi folosită pentru conectare\.$/); await waitFor(() => expect(section.getAllByRole("button", { name: "Elimină cheia" })).toHaveLength(1));
  expect(section.getByRole("button", { name: "Elimină cheia" })).toBeDisabled();
  expect(api.posts()).toEqual([
    { url: "/auth/me/passkey/options", method: "POST", body: { name: "Cheie nouă" } },
    { url: "/auth/me/passkey/finish", method: "POST", body: { ticket: "fixture-own-key", credential_id: "-_8", client_data: "-_8", attestation: "-_8", transports: '["internal","hybrid"]' } },
    { url: "/auth/me/passkey/rename", method: "POST", body: { credential_id: "-_8", name: "Cheie secundară" } },
    { url: "/auth/me/passkey/remove", method: "POST", body: { credential_id: "old" } },
  ]);
});

it.each(["refused", "unreadable"])("refreshes owned key metadata after a %s write without replaying it", async failure => {
  const user = userEvent.setup(), identity = fixtureIdentity(); identity.factors.passkeys = [passkeyMetadata()];
  const api = server({ identity: () => json(identity), onPost: () => { if (failure === "unreadable") { identity.factors.passkeys[0].name = "Nume înregistrat"; return new Response("{", { status: 200 }); } return json({ ok: false, message: "Refuz de test" }, 400); } });
  renderSignedIn(<UserAccount back="/" />); const section = within(await screen.findByRole("region", { name: "Chei de acces (passkey)" }));
  await user.click(section.getByRole("button", { name: "Redenumește" })); await user.click(section.getByRole("button", { name: "Salvează numele" }));
  await waitFor(() => expect(section.getAllByRole("alert").length).toBeGreaterThan(0));
  if (failure === "unreadable") { await section.findByText("Nume înregistrat"); await user.click(section.getByRole("button", { name: "Renunță" })); expect(section.getByRole("alert")).toHaveTextContent("Modificarea anterioară poate fi deja salvată"); }
  expect(api.posts()).toEqual([{ url: "/auth/me/passkey/rename", method: "POST", body: { credential_id: "-_8", name: "Cheie de test" } }]);
});

it.each(["rejected", "late success"])("reconciles an already committed key write after same-account cancellation and a %s response", async reply => {
  const user = userEvent.setup(), pending = deferred<Response>(), identity = fixtureIdentity(); identity.factors.passkeys = [passkeyMetadata()];
  const api = server({ identity: () => json(identity), onPost: () => { identity.factors.passkeys[0].name = "Nume înregistrat"; return pending.promise.then(response => { if (reply === "rejected") throw new DOMException("Cancelled after commit", "AbortError"); return response; }); } });
  render(<Authentication><UserAccount back="/" /></Authentication>); const section = within(await screen.findByRole("region", { name: "Chei de acces (passkey)" }));
  await user.click(section.getByRole("button", { name: "Redenumește" })); const input = section.getByLabelText("Numele cheii de acces"); await user.clear(input); await user.type(input, "Nume înregistrat");
  await user.click(section.getByRole("button", { name: "Salvează numele" })); await waitFor(() => expect(api.posts()).toHaveLength(1));
  const signal = vi.mocked(fetch).mock.calls.find(c => String(c[0]).endsWith("/rename"))?.[1]?.signal;
  fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); await waitFor(() => expect(signal?.aborted).toBe(true));
  await waitFor(() => expect(section.getByText("Cheie de test")).toBeVisible());
  await act(async () => { pending.resolve(json({ ok: true })); });
  await waitFor(() => expect(section.getByText("Nume înregistrat")).toBeVisible());
  await user.click(section.getByRole("button", { name: "Renunță" }));
  expect(section.getByRole("alert")).toHaveTextContent("Modificarea anterioară poate fi deja salvată");
  expect(api.posts()).toEqual([{ url: "/auth/me/passkey/rename", method: "POST", body: { credential_id: "-_8", name: "Nume înregistrat" } }]);
});

it("drops a native own-key proof after its source unmounts", async () => {
  const native = nativeBrowser(), pending = deferred<NativeKey>(), user = userEvent.setup(); native.create.mockReturnValue(pending.promise);
  const api = server({ onPost: () => json({ ok: true, ticket: "fixture-own-key", publicKey: nativeCreation }) });
  const view = renderSignedIn(<UserAccount back="/" />); await user.click(await screen.findByRole("button", { name: "Adaugă o cheie de acces" }));
  await user.clear(screen.getByLabelText("Numele cheii de acces")); await user.type(screen.getByLabelText("Numele cheii de acces"), "Cheie nouă"); await user.click(screen.getByRole("button", { name: "Configurează cheia de acces" }));
  await waitFor(() => expect(native.create).toHaveBeenCalledTimes(1)); const signal = native.create.mock.calls[0][0].signal as AbortSignal; view.unmount();
  await act(async () => { pending.resolve(new NativeKey(new NativeAttestation())); }); expect(signal.aborted).toBe(true);
  expect(api.posts()).toEqual([{ url: "/auth/me/passkey/options", method: "POST", body: { name: "Cheie nouă" } }]);
});

it.each(["admin", "member", "readonly"] as UserRole[])("shows truthful key-only account methods and recovery-code sessions for %s", async role => {
  const identity = fixtureIdentity(role); identity.auth.method = "passkey"; identity.factors.password_totp = false; identity.factors.passkeys = [passkeyMetadata()];
  identity.sessions = [{ ...sessionMetadata(true), method: "passkey" }, { ...sessionMetadata(false), method: "recovery_code" }];
  const api = server({ identity: () => json(identity) }); renderSignedIn(<UserAccount back="/" />, { identity });
  expect(await screen.findByText("Chei de acces configurate: 1.")).toBeVisible(); expect(screen.getByText("Cheie de acces.")).toBeVisible(); expect(screen.getByText("Cod de recuperare.")).toBeVisible();
  for (const name of ["Schimbă parola", "Schimbă aplicația de autentificare"]) expect(screen.queryByRole("button", { name })).toBeNull();
  expect(screen.getByRole("button", { name: "Înlocuiește codurile de recuperare" })).toBeVisible(); expect(api.posts()).toEqual([]);
});

it.each([false, true])("closes other sessions with exact owner-only fields, all=%s", async all => {
  const user = userEvent.setup(); let closed = false;
  const api = server({ identity: () => json({ ...fixtureIdentity(), sessions: closed ? [sessionMetadata(true)] : [sessionMetadata(true), sessionMetadata(false)] }),
    onPost: () => { closed = true; return json({ ok: true }); } });
  renderSignedIn(<UserAccount back="/" />);
  await user.click(await screen.findByRole("button", { name: all ? "Închide celelalte sesiuni" : "Închide sesiunea" }));
  await answer();
  await waitFor(() => expect(screen.queryByText("Altă sesiune")).not.toBeInTheDocument());
  expect(api.posts()).toEqual([{ url: all ? "/auth/me/sessions-close-others" : "/auth/me/session-close", method: "POST", body: all ? {} : { id: "b".repeat(64) } }]);
});

it("retains an uncertain session-close warning while refreshing without repeating the POST", async () => {
  const user = userEvent.setup(); let closed = false;
  const api = server({ identity: () => json({ ...fixtureIdentity(), sessions: closed ? [sessionMetadata(true)] : [sessionMetadata(true), sessionMetadata(false)] }),
    onPost: () => { closed = true; return new Response('{"ok":', { headers: { "Content-Type": "application/json" } }); } });
  renderSignedIn(<UserAccount back="/" />); await user.click(await screen.findByRole("button", { name: "Închide sesiunea" }));
  await answer();
  expect(await screen.findByRole("alert")).toHaveTextContent("Nu a sosit confirmarea.");
  await waitFor(() => expect(screen.queryByText("Altă sesiune")).not.toBeInTheDocument()); expect(api.posts()).toHaveLength(1);
});

it("retries failed session metadata and cancels a close without posting", async () => {
  const user = userEvent.setup(); let failed = true;
  const api = server({ identity: () => failed ? json({}, 500) : json({ ...fixtureIdentity(), sessions: [sessionMetadata(false)] }) });
  renderSignedIn(<UserAccount back="/" />); await screen.findByRole("alert"); failed = false; await user.click(screen.getByRole("button", { name: "Reîncearcă" }));
  await user.click(await screen.findByRole("button", { name: "Închide sesiunea" }));
  expect(await answer(false)).toBe("Închizi această sesiune? Pe dispozitivul ei trebuie să te conectezi din nou."); expect(api.posts()).toEqual([]);
});

it("does not show metadata from another cookie owner", async () => {
  const ended = vi.fn(); window.addEventListener(SESSION_ENDED, ended);
  const identity = fixtureIdentity(); identity.me = { ...identity.me, id: "u_other", display_name: "Alt cont" };
  server({ identity: () => json(identity) }); renderSignedIn(<UserAccount back="/" />);
  await waitFor(() => expect(ended).toHaveBeenCalledOnce()); expect(screen.queryByText("Alt cont")).not.toBeInTheDocument();
  window.removeEventListener(SESSION_ENDED, ended);
});


it("opens the account before any company exists and signs out through the current cookie", async () => {
  at("?tab=setari&sec=cont"); const user = userEvent.setup();
  const api = server({ state: () => json({ setup: "needs_company", companies: [], live: false, can_manage_companies: true }),
    identity: () => json({ ...fixtureIdentity(), sessions: [sessionMetadata(true)] }) });
  render(<Authentication><App /></Authentication>); await user.click(await screen.findByRole("button", { name: "Ieși din cont" }));
  expect(await screen.findByLabelText("Nume de utilizator")).toBeVisible();
  expect(api.posts()).toEqual([{ url: "/auth/signout", method: "POST", body: {} }]);
});


it.each([["ligatures", "\ufb03".repeat(8), true], ["astral", "\u{1F600}".repeat(80), true], ["too long", "\u{1F600}".repeat(129), false]])("counts a %s password as the engine does", async (_, password, sent) => {
  const user = userEvent.setup(); const api = server(); renderSignedIn(<UserAccount back="/" />);
  await user.click(await screen.findByRole("button", { name: "Schimbă parola" }));
  fireEvent.change(screen.getByLabelText("Parolă nouă"), { target: { value: password } }); fireEvent.change(screen.getByLabelText("Repetă parola"), { target: { value: password } });
  await user.click(screen.getByRole("button", { name: "Salvează parola" }));
  if (sent) await waitFor(() => expect(api.posts()).toEqual([{ url: "/auth/me/password-change", method: "POST", body: { password } }]));
  else { expect(await screen.findByRole("alert")).toHaveTextContent("între 12 și 128"); expect(api.posts()).toEqual([]); }
});

it("checks password repetition locally and sends only the new password once", async () => {
  const user = userEvent.setup(); const api = server(); renderSignedIn(<UserAccount back="/" />);
  await user.click(await screen.findByRole("button", { name: "Schimbă parola" })); expect(screen.getByText("Între 12 și 128 de caractere.")).toBeVisible();
  await user.type(screen.getByLabelText("Parolă nouă"), "Replacement"); expect(screen.getByText("Încă un caracter.")).toBeVisible();
  await user.type(screen.getByLabelText("Parolă nouă"), " password 2026"); await user.type(screen.getByLabelText("Repetă parola"), "different");
  expect(screen.getByText("Parolele nu coincid.")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Salvează parola" })); expect(await screen.findByRole("alert")).toBeVisible(); expect(api.posts()).toEqual([]);
  expect(screen.getByLabelText("Parolă nouă")).toHaveValue("Replacement password 2026"); expect(screen.getByLabelText("Repetă parola")).toHaveValue("");
  await user.type(screen.getByLabelText("Repetă parola"), "Replacement password 2026");
  await user.click(screen.getByRole("button", { name: "Salvează parola" })); expect(await screen.findByRole("status")).toHaveTextContent("Parola a fost schimbată.");
  expect(api.posts()).toEqual([{ url: "/auth/me/password-change", method: "POST", body: { password: "Replacement password 2026" } }]);
  expect(screen.queryByLabelText("Parolă nouă")).not.toBeInTheDocument();
});

it.each([400, 0])("shows the engine's refusal/uncertainty and clears only the confirmation, status=%s", async status => {
  const user = userEvent.setup(); const api = server({ onPost: () => status ? json({ ok: false, message: "Refuz de test" }, status) : Promise.reject(new TypeError("offline")) });
  renderSignedIn(<UserAccount back="/" />); await user.click(await screen.findByRole("button", { name: "Schimbă parola" }));
  for (const label of ["Parolă nouă", "Repetă parola"]) await user.type(screen.getByLabelText(label), "Replacement password 2026");
  await user.click(screen.getByRole("button", { name: "Salvează parola" })); expect(await screen.findByRole("alert")).toHaveTextContent(status ? "Refuz de test" : "Nu a sosit confirmarea.");
  expect(screen.getByLabelText("Parolă nouă")).toHaveValue("Replacement password 2026"); expect(screen.getByLabelText("Repetă parola")).toHaveValue(""); expect(api.posts()).toHaveLength(1);
});

it("generates recovery codes once even for two synchronous clicks and requires saved acknowledgement", async () => {
  const user = userEvent.setup(), pending = deferred<Response>(); const api = server({ onPost: () => pending.promise }); renderSignedIn(<UserAccount back="/" />);
  await user.click(await screen.findByRole("button", { name: "Înlocuiește codurile de recuperare" })); expect(api.posts()).toEqual([]);
  const button = screen.getByRole("button", { name: "Generează coduri noi" }); act(() => { button.click(); button.click(); });
  expect(api.posts()).toEqual([{ url: "/auth/me/recovery-codes", method: "POST", body: {} }]);
  await act(async () => pending.resolve(json({ ok: true, codes: recoveryCodes })));
  expect(await screen.findByText(recoveryCodes[0])).toBeVisible(); expect(screen.getByRole("button", { name: "Continuă" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox")); await user.click(screen.getByRole("button", { name: "Continuă" }));
  expect(await screen.findByRole("button", { name: "Înlocuiește codurile de recuperare" })).toBeVisible(); expect(screen.queryByText(recoveryCodes[0])).not.toBeInTheDocument();
});

it("restarts refused own TOTP enrolment explicitly and uses the replacement ticket only", async () => {
  const user = userEvent.setup(); let begins = 0; const api = server({ onPost: c => c.url.endsWith("/begin") ? json({ ...enrolment, ticket: `own-${++begins}` })
    : c.body?.ticket === "own-1" ? json({ ok: false, message: "Refuz de test" }, 401) : json({ ok: true, codes: recoveryCodes }) });
  renderSignedIn(<UserAccount back="/" />); await user.click(await screen.findByRole("button", { name: "Schimbă aplicația de autentificare" }));
  await user.click(screen.getByRole("button", { name: "Configurează altă aplicație" }));
  await user.type(await screen.findByLabelText("Cod din aplicația de autentificare"), "123456"); await user.click(screen.getByRole("button", { name: "Confirmă codul" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Refuz de test");
  await user.click(screen.getByRole("button", { name: "Reîncepe configurarea" })); await user.click(screen.getByRole("button", { name: "Configurează altă aplicație" }));
  await user.type(await screen.findByLabelText("Cod din aplicația de autentificare"), "654321"); await user.click(screen.getByRole("button", { name: "Confirmă codul" }));
  expect(await screen.findByText(recoveryCodes[0])).toBeVisible(); expect(api.posts().map(c => [c.url, c.body])).toEqual([
    ["/auth/me/totp/begin", {}], ["/auth/me/totp/finish", { ticket: "own-1", code: "123456" }], ["/auth/me/totp/begin", {}], ["/auth/me/totp/finish", { ticket: "own-2", code: "654321" }],
  ]);
});

it.each([false, true])("uses the same-account proof before regenerating codes, accepted=%s", async accepted => {
  const user = userEvent.setup(); let generation = 0; const api = server({ onPost: c => c.url === "/auth/me/recovery-codes" ? ++generation === 1 ? reauth() : json({ ok: true, codes: recoveryCodes })
    : c.url.endsWith("/password") ? json({ ok: true, ticket: "proof" }) : json({ ok: true }) });
  render(<Authentication><UserAccount back="/" /></Authentication>);
  await user.click(await screen.findByRole("button", { name: "Înlocuiește codurile de recuperare" })); await user.click(screen.getByRole("button", { name: "Generează coduri noi" }));
  await screen.findByRole("dialog", { name: "Confirmă identitatea" });
  if (accepted) { await proveIdentity(user); expect(await screen.findByText(recoveryCodes[0])).toBeVisible(); }
  else { await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Renunță" })); await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull()); expect(screen.queryByRole("alert")).toBeNull(); expect(screen.queryByText(recoveryCodes[0])).not.toBeInTheDocument(); }
  expect(api.posts().filter(c => c.url === "/auth/me/recovery-codes").map(c => c.body)).toEqual(accepted ? [{}, {}] : [{}]);
});


it("refreshes after unreadable code generation without retrying or leaking the old warning into another flow", async () => {
  const user = userEvent.setup(); const api = server({ onPost: () => new Response('{"ok":', { headers: { "Content-Type": "application/json" } }) });
  renderSignedIn(<UserAccount back="/" />); await user.click(await screen.findByRole("button", { name: "Înlocuiește codurile de recuperare" }));
  await user.click(screen.getByRole("button", { name: "Generează coduri noi" })); expect(await screen.findByRole("alert")).toHaveTextContent("Nu a sosit confirmarea.");
  await waitFor(() => expect(api.calls.filter(c => c.url === "/web/api/me")).toHaveLength(2)); expect(api.posts()).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "Renunță" })); await user.click(screen.getByRole("button", { name: "Schimbă aplicația de autentificare" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument(); expect(api.posts()).toHaveLength(1);
});


function DirtyPrivate() {
  const [draft, setDraft] = useState(""); const session = useContext(IdentityContext);
  const allowed = useCan("member");
  return <><label>Ciornă locală<input value={draft} onChange={e => setDraft(e.target.value)} /></label>
    <p>{session?.identity.me.id}</p>{allowed && <button>Acțiune privată</button>}</>;
}

it.each(["focus", "visible", "storage"])("rechecks on %s without blanking on return and preserves same-user edits after demotion", async trigger => {
  const pending = deferred<Response>(); let reads = 0;
  server({ identity: () => ++reads === 1 ? json(fixtureIdentity()) : pending.promise });
  render(<Authentication><DirtyPrivate /></Authentication>);
  const input = await screen.findByLabelText("Ciornă locală"); await userEvent.type(input, "Nesalvat");
  if (trigger === "visible") { vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible"); fireEvent(document, new Event("visibilitychange")); }
  else if (trigger === "storage") fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED, newValue: "untrusted-hint" }));
  else fireEvent(window, new Event("focus"));
  if (trigger === "storage") { expect(input).not.toBeVisible(); expect(document.querySelector(".auth-checking")).toBeTruthy(); }
  else { expect(input).toBeVisible(); expect(input).toHaveFocus(); expect(document.querySelector(".auth-checking")).toBeNull(); }
  await act(async () => pending.resolve(json(fixtureIdentity("readonly"))));
  await waitFor(() => expect(input).toBeVisible()); expect(input).toHaveValue("Nesalvat");
  expect(screen.queryByRole("button", { name: "Acțiune privată" })).toBeNull(); expect(reads).toBe(2);
});

it.each([200, 401])("clears previous-owner forms and ignores an older identity response, status=%s", async status => {
  const older = deferred<Response>(), newer = deferred<Response>(); let reads = 0;
  server({ identity: () => ++reads === 1 ? json(fixtureIdentity()) : reads === 2 ? older.promise : newer.promise });
  render(<Authentication><DirtyPrivate /></Authentication>);
  await userEvent.type(await screen.findByLabelText("Ciornă locală"), "Contul anterior");
  fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED })); fireEvent(window, new Event("focus"));
  const next = fixtureIdentity("member"); next.me.id = "u_next";
  await act(async () => newer.resolve(json(next)));
  expect(await screen.findByText("u_next")).toBeVisible(); expect(screen.getByLabelText("Ciornă locală")).toHaveValue("");
  await act(async () => older.resolve(json(fixtureIdentity(), status)));
  expect(screen.queryByText("u_example")).toBeNull(); expect(screen.getByText("u_next")).toBeVisible();
});

it("keeps private data suspended after an unconfirmed identity refresh and retries safely", async () => {
  let fail = false; server({ identity: () => json(fixtureIdentity(), fail ? 503 : 200) });
  render(<Authentication><DirtyPrivate /></Authentication>);
  const input = await screen.findByLabelText("Ciornă locală"); await userEvent.type(input, "Nesalvat"); fail = true;
  fireEvent(window, new Event("focus")); await screen.findByRole("alert"); expect(input).not.toBeVisible();
  fail = false; await userEvent.click(screen.getByRole("button", { name: "Reîncearcă" }));
  await waitFor(() => expect(input).toBeVisible()); expect(input).toHaveValue("Nesalvat");
});

it.each(["expiry", "signout"])("never revives a private session from refresh replies after %s", async ending => {
  const pending = deferred<Response>(), signingOut = deferred<Response>(); let reads = 0;
  server({ identity: () => ++reads === 1 ? json(fixtureIdentity()) : pending.promise, onPost: () => signingOut.promise }); render(privateApp);
  await screen.findByText("Conținut privat");
  if (ending === "signout") await userEvent.click(screen.getByRole("button", { name: "Ieși din cont" }));
  fireEvent(window, new Event("focus"));
  if (ending === "expiry") act(() => window.dispatchEvent(new Event(SESSION_ENDED)));
  else await act(async () => signingOut.resolve(json({ ok: true })));
  await screen.findByLabelText(ending === "expiry" ? "Parolă" : "Nume de utilizator");
  await act(async () => pending.resolve(json(fixtureIdentity())));
  // Expiry keeps the screen inert under the sign-in sheet; a late reply never unlocks it.
  if (ending === "expiry") { expect(screen.getByRole("dialog", { name: "Sesiunea a expirat" })).toBeVisible(); expect(screen.getByText("Conținut privat").closest("[inert]")).not.toBeNull(); }
  else expect(screen.queryByText("Conținut privat")).toBeNull();
});

it("publishes only a random hint after successful session changes, tolerates blocked storage, and ignores unrelated keys", async () => {
  const storage = vi.spyOn(Storage.prototype, "setItem"); const api = server(); render(privateApp);
  await screen.findByText("Conținut privat");
  fireEvent(window, new StorageEvent("storage", { key: "theme" })); expect(api.calls.filter(c => c.url.endsWith("/me"))).toHaveLength(1);
  await postAuth("password", { username: "example", password: "never-store-this" }); expect(storage).not.toHaveBeenCalled();
  await postAuth("totp", { ticket: "never-store-ticket", code: "123456" });
  expect(storage).toHaveBeenCalledExactlyOnceWith(SESSION_CHANGED, expect.stringMatching(/^[a-f0-9-]{36}$/));
  storage.mockImplementation(() => { throw new Error("storage blocked"); }); await expect(postAuth("signout", {})).resolves.toMatchObject({ ok: true });
});


function PortalPrivate() {
  const [open, setOpen] = useState(false);
  return <><button onClick={() => setOpen(true)}>Deschide formularul</button>{open && createPortal(
    <Sheet label="Formular privat" onClose={() => setOpen(false)}><input aria-label="Ciornă în portal" /></Sheet>, document.body)}</>;
}

it("leaves retry and keyboard navigation usable while a portalled sheet is suspended", async () => {
  let fail = false; server({ identity: () => json(fixtureIdentity(), fail ? 503 : 200) });
  render(<Authentication><PortalPrivate /></Authentication>);
  await userEvent.click(await screen.findByRole("button", { name: "Deschide formularul" }));
  const input = screen.getByLabelText("Ciornă în portal"); await userEvent.type(input, "Nesalvat");
  fail = true; fireEvent(window, new Event("focus")); await screen.findByRole("alert");
  const retry = screen.getByRole("button", { name: "Reîncearcă" });
  await waitFor(() => expect(retry.closest("[inert]")).toBeNull());
  const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
  document.dispatchEvent(tab); expect(tab.defaultPrevented).toBe(false);
  fail = false; await userEvent.click(retry); await waitFor(() => expect(document.querySelector(".auth-checking")).toBeNull());
  expect(input).toHaveValue("Nesalvat"); expect(screen.getByRole("dialog")).toBeVisible();
});

it("does not expire a newer account when an unmounted own-account read later returns 401", async () => {
  const old = deferred<Response>(); let reads = 0;
  const newer = fixtureIdentity("member"); newer.me.id = "u_next";
  server({ identity: () => ++reads === 1 ? json(fixtureIdentity()) : reads === 2 ? old.promise : json(newer) });
  render(<Authentication><UserAccount back="/" /></Authentication>);
  await waitFor(() => expect(reads).toBe(2)); fireEvent(window, new StorageEvent("storage", { key: SESSION_CHANGED }));
  await screen.findByRole("heading", { name: "Cont" }); await waitFor(() => expect(reads).toBeGreaterThan(3));
  await act(async () => old.resolve(json({}, 401)));
  expect(screen.queryByLabelText("Nume de utilizator")).toBeNull(); expect(screen.getByRole("heading", { name: "Cont" })).toBeVisible();
});


it("ignores a cancelled financial read's late 401 after a newer identity is confirmed", async () => {
  const old = deferred<Response>(); let reads = 0;
  const newer = fixtureIdentity("member"); newer.me.id = "u_next";
  server({ identity: () => json(++reads === 1 ? fixtureIdentity() : newer), state: () => old.promise });
  render(<Authentication><DirtyPrivate /></Authentication>); await screen.findByText("u_example");
  const request = getBootstrap("NORD").catch(error => error);
  fireEvent(window, new Event("focus")); await screen.findByText("u_next");
  await act(async () => old.resolve(json({}, 401))); await request;
  expect(screen.getByText("u_next")).toBeVisible(); expect(screen.queryByLabelText("Nume de utilizator")).toBeNull();
});


it.each([
  ["Macintosh; Intel Mac OS X", 0, "Mac"], ["Macintosh; Intel Mac OS X", 5, "iPad"],
  ["iPad", 1, "iPad"], ["iPhone; CPU iPhone OS", 5, "iPhone"],
  ["Windows NT 10.0", 0, "Windows"], ["Linux; Android 14", 5, "Android"],
  ["X11; Linux x86_64", 0, "Linux"], ["", 0, "Dispozitiv"],
])("suggests only a coarse device label for %s", (userAgent, maxTouchPoints, device) => {
  expect(suggestedPasskeyName({ userAgent, maxTouchPoints })).toBe(`${device} Passkey`);
});

it("hydrates passkey enrollment safely and preserves edits and intentional clearing across renders", async () => {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Macintosh; Intel Mac OS X");
  const begin = vi.fn().mockRejectedValue(new Error("stop before native prompt"));
  const form = <PasskeyEnrollment begin={begin} finish={vi.fn()} onDone={vi.fn()} />;
  const node = document.createElement("div"); document.body.append(node);
  node.innerHTML = renderToString(form); expect(node.querySelector("input")).toHaveValue("");
  const recover = vi.fn(); let root: ReturnType<typeof hydrateRoot>;
  await act(async () => { root = hydrateRoot(node, form, { onRecoverableError: recover }); });
  const input = within(node).getByLabelText("Numele cheii de acces");
  expect(input).toHaveValue("Mac Passkey"); expect(input).toHaveAttribute("maxlength", "80"); expect(recover).not.toHaveBeenCalled();
  for (const value of ["Numele meu", ""]) {
    fireEvent.change(input, { target: { value } });
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Windows NT 10.0");
    await act(async () => root.render(<PasskeyEnrollment begin={begin} finish={vi.fn()} onDone={vi.fn()} />)); expect(input).toHaveValue(value);
    if (value) { fireEvent.submit(input.closest("form")!); await waitFor(() => expect(begin).toHaveBeenCalledWith(value, expect.any(AbortSignal))); }
    else { await userEvent.click(within(node).getByRole("button", { name: "Reîncepe configurarea" })); expect(begin).toHaveBeenCalledTimes(1); }
  }
  await act(async () => root.unmount()); node.remove();
});

it("shows only the app's shell, never a sign-in or an account action, until the session and the firm's state are known", async () => {
  // The prerendered page (the first paint, before any script) is the shell too.
  for (const text of ["Conectare", "Ieși din cont"]) expect(renderToString(page)).not.toContain(text);
  at("?co=NORD"); const me = deferred<Response>(), state = deferred<Response>();
  const api = server({ identity: () => me.promise, state: () => state.promise });
  render(page);
  const shell = () => {
    expect(screen.getByText("Se încarcă…")).toBeInTheDocument(); expect(screen.queryAllByRole("button")).toEqual([]);
    for (const text of ["Conectare", "Ieși din cont", "Niciun element"]) expect(screen.queryByText(text)).toBeNull();
  };
  shell();
  await act(async () => me.resolve(json(fixtureIdentity())));
  await waitFor(() => expect(api.gets("NORD")).toHaveLength(1));
  shell();
  await act(async () => state.resolve(json(fixture())));
  expect(await screen.findByRole("region", { name: "De plătit" })).toBeVisible(); expect(api.gets()).toHaveLength(1);
});

it("reads the identity once when focus and visibility both say the tab is back", async () => {
  const api = server(); render(page); await screen.findByRole("region", { name: "De plătit" });
  const reads = () => api.calls.filter((c) => c.url.includes("api/me")).length, before = reads();
  await act(async () => { window.dispatchEvent(new Event("focus")); document.dispatchEvent(new Event("visibilitychange")); });
  expect(reads()).toBe(before + 1);
});

it("loads Acasă in one request and reads one bootstrap on a firm switch", async () => {
  at("?co=NORD"); const api = server(); render(page);
  await screen.findByRole("region", { name: "De plătit" });
  expect(api.calls.map(c => c.url)).toEqual(["/web/api/bootstrap?co=NORD"]);
  at("?co=STUDIO9"); fireEvent(window, new PopStateEvent("popstate"));
  await waitFor(() => expect(api.gets("STUDIO9")).toHaveLength(1));
  await screen.findByRole("region", { name: "De plătit" });
  expect(api.calls.map(c => c.url)).toEqual(["/web/api/bootstrap?co=NORD", "/web/api/bootstrap?co=STUDIO9"]);
});

it.each(["role", "session"])("rejects firm data after a %s change before refreshing identity", async change => {
  at("?co=NORD"); let reads = 0; const confirmed = deferred<Response>();
  const next = fixtureIdentity(change === "role" ? "readonly" : "admin");
  if (change === "session") next.sessions = [{ ...sessionMetadata(true), id: "c".repeat(64) }];
  const api = server({ identity: () => ++reads === 1 ? json(fixtureIdentity()) : reads === 2 ? json(next) : confirmed.promise });
  render(page); await screen.findByRole("button", { name: "Nou" });
  at("?co=STUDIO9"); fireEvent(window, new PopStateEvent("popstate"));
  await waitFor(() => expect(api.gets("STUDIO9")).toHaveLength(2));
  // The mismatched firm's data is absent even from the kept, hidden screen.
  expect(document.querySelector('[aria-label="De plătit"]')).toBeNull();
  await act(async () => confirmed.resolve(json(next)));
  expect(await screen.findByRole("region", { name: "De plătit" })).toBeVisible();
  expect(api.gets("STUDIO9")).toHaveLength(3);
});


it("discards a bootstrap held across another tab's cookie change", async () => {
  const state = deferred<Response>(); server({ state: () => state.promise });
  const pending = getBootstrap("NORD");
  localStorage.setItem(SESSION_CHANGED, "another-session-generation");
  state.resolve(json(fixture()));
  await expect(pending).rejects.toMatchObject({ status: 409 });
});

it("never seeds a newly identified account from the previous account's bootstrap", async () => {
  at("?co=NORD"); let changed = false; const state = deferred<Response>();
  const other = fixtureIdentity(); other.me.id = "u_other";
  server({ identity: () => json(changed ? other : fixtureIdentity()), state: () => changed ? state.promise : json(fixture()) });
  render(page); await screen.findByRole("region", { name: "De plătit" });
  changed = true; fireEvent(window, new Event("focus"));
  await waitFor(() => expect(screen.getByText("Se încarcă…")).toBeVisible());
  expect(screen.queryByRole("region", { name: "De plătit" })).toBeNull();
  await act(async () => state.resolve(json(fixture())));
  expect(await screen.findByRole("region", { name: "De plătit" })).toBeVisible();
});
