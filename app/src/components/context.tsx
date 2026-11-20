"use client";
import { createContext, useContext, type ReactNode } from "react";
import type { Action } from "@/lib/api";
import type { Ask } from "./Confirm";
import type { GoOpts, Route, To } from "@/lib/nav";
import { isAll, type Bootstrap, type ActionResult, type Capabilities, type Identity, type Co, type State, type View } from "@/lib/types";

/** Only normal cookie sessions provide capabilities to private application controls. */
export const BootstrapContext = createContext<Bootstrap | null>(null);

export const IdentityContext = createContext<{ identity: Identity; signOut: () => Promise<void>; refreshIdentity: () => Promise<void> } | null>(null);

export function useCan(need: keyof Capabilities): boolean {
  const session = useContext(IdentityContext);
  return !!session && !session.identity.auth.recovery_only && session.identity.me.can[need] === true;
}

/** Where to go after a successful request: a place, false to stay (the sheet keeps its form), undefined for the usual rule
 * (close the sheet, follow `next`). */
export type After<R extends ActionResult> = (r: R) => To | false | undefined;

export type Ctx = {
  /** What the route shows: one company, or all of them (co=ALL, Acasă and Facturi). */
  view: View;
  route: Route;
  go: (to: To, opts?: GoOpts) => void;
  /** A POST for one real company; a question asks first, in the app's confirmation dialog. */
  act: (co: Co, action: Action, body: Record<string, string | number>, confirm?: Ask) => Promise<void>;
  /** Any request that changes something for one company, with the same rules as `act`: one at a time, confirm first,
   * the new state applied, the message as a toast. Returns the answer, or null if it didn't run or didn't arrive. */
  run: <R extends ActionResult>(co: Co, send: () => Promise<R>, confirm?: Ask, after?: After<R>) => Promise<R | null>;
  busy: boolean;
  /** A result message survives a refreshed or replaced page component. */
  say: (text: string) => void;
  /** Reads the state again; null when the read failed or a newer read or POST superseded it. */
  refresh: () => Promise<View | null>;
};

export const AppCtx = createContext<Ctx | null>(null);

export function useApp(): Ctx {
  const c = useContext(AppCtx);
  if (!c) throw new Error("useApp outside AppCtx");
  return c;
}

const CompanyCtx = createContext<State | null>(null);

/** Everything under it acts for this company (its buttons post with its `co`). */
export function Company({ s, children }: { s: State; children: ReactNode }) {
  return <CompanyCtx.Provider value={s}>{children}</CompanyCtx.Provider>;
}

/** The company state of the nearest <Company>, else the route's own (a single-company tab). */
export function useCompany(): State {
  const s = useContext(CompanyCtx);
  const { view } = useApp();
  if (s) return s;
  if (isAll(view)) throw new Error("useCompany: no company under Toate firmele");
  return view;
}
