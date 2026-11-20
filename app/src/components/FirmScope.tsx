"use client";
// Which firms an ANAF connection or an inbox serves (`firm_scope`): every firm, those added later too, or the ones
// chosen. The engine decides which connection reads each firm (`coverage`); the app only shows it and the choice.
import { Input, Label, legendStyle } from "./Controls";
import type { Companies, Coverage, FirmScope, Integration } from "@/lib/types";

/** How a connection's firms read on its row: "Toate firmele" or their names. Nothing with a single firm. */
export function scopeLine(scope: FirmScope | undefined, firms: Companies): string {
  if (firms.length < 2 || scope === undefined) return "";
  if (scope === "all") return "Toate firmele";
  const names = firms.filter((c) => scope.includes(c.key)).map((c) => c.name);
  return names.length ? names.join(", ") : "Nicio firmă";
}

/** Whether two scopes name the same firms (the engine may list them in another order). */
export function sameScope(a: FirmScope, b: FirmScope): boolean {
  if (a === "all" || b === "all") return a === b;
  return a.length === b.length && a.every((k) => b.includes(k));
}

/** Whether the person chose this scope: there is a choice (two firms or more) and it differs from the saved one (none
 * for a new connection). An unchanged saved scope is never sent again or questioned: the engine keeps it. */
const chosen = (scope: FirmScope, firms: Companies, saved?: FirmScope) => firms.length > 1 && (saved === undefined || !sameScope(scope, saved));

/** Why a chosen scope can't be saved: no firm at all (Oprește pauses a connection). */
export const scopeReason = (scope: FirmScope, firms: Companies, saved?: FirmScope) =>
  chosen(scope, firms, saved) && scope !== "all" && !scope.length ? "Alege cel puțin o firmă." : "";

/** The scope as sent: only a chosen one. A new connection without one is "all" for the engine. */
export const scopeBody = (scope: FirmScope, firms: Companies, saved?: FirmScope): { firm_scope?: FirmScope } =>
  chosen(scope, firms, saved) ? { firm_scope: scope } : {};

/** "Firme": Toate firmele, checked by default; unchecking it lists every firm, all checked, to leave some out. */
export function FirmScopeField({ value, onChange, firms, disabled = false }: {
  value: FirmScope; onChange: (v: FirmScope) => void; firms: Companies; disabled?: boolean;
}) {
  if (firms.length < 2) return null;
  const toggle = (key: string, on: boolean) => {
    const chosen = value === "all" ? [] : value;
    onChange(firms.map((c) => c.key).filter((k) => k === key ? on : chosen.includes(k)));
  };
  return (
    <fieldset className="m-0 mt-4 min-w-0 border-0 p-0" disabled={disabled}>
      <legend className={legendStyle}>Firme</legend>
      <Label className="cursor-pointer !mt-0"><Input type="checkbox" checked={value === "all"}
        onChange={(e) => onChange(e.target.checked ? "all" : firms.map((c) => c.key))} />Toate firmele</Label>
      {value !== "all" ? <div className="pl-7">
        {firms.map((c) => <Label key={c.key} className="cursor-pointer !mt-3"><Input type="checkbox" checked={value.includes(c.key)}
          onChange={(e) => toggle(c.key, e.target.checked)} />{c.name}</Label>)}
      </div> : null}
    </fieldset>
  );
}

/** The ANAF section's per-firm warnings: a firm two connections cover (the engine reads it with one), and, once there
 * is a connection, a firm none covers. */
export function anafWarnings(coverage: Coverage[] | undefined, items: Integration[], firms: Companies): string[] {
  if (!coverage || !items.some((i) => i.kind === "anaf" && i.enabled)) return [];
  const name = (id: string) => items.find((i) => i.id === id)?.name ?? id;
  const firm = (key: string) => firms.find((c) => c.key === key)?.name ?? key;
  return coverage.flatMap((r) => r.anaf === null ? [`${firm(r.firm)} nu este în nicio conexiune ANAF. Facturile ei din e-Factura nu se citesc.`]
    : r.anaf_also.length ? [`${firm(r.firm)} este în ${r.anaf_also.length + 1} conexiuni ANAF: ${[r.anaf, ...r.anaf_also].map(name).join(", ")}. Se citește prin ${name(r.anaf)}. Lasă firma într-una singură.`]
    : []);
}
