"use client";
import { Actions, Button, Form, Input, Label, linkButtonStyle, noteLeftStyle } from "./Controls";
// Setări › Firme, a firm's other names: how it is written on documents besides its legal name ("Atelier Nord" for
// "ATELIER NORD S.R.L."). The engine looks for each as plain text (case, diacritics and punctuation aside) and refuses
// one that is too short or overlaps another firm's name; the app only sends what was typed.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { postCompany, type CompanyResult } from "@/lib/api";
import type { CompaniesConfig, CompanyEntry } from "@/lib/types";
import { useApp } from "./context";

export function CompanyNames({ c, config, reload }: { c: CompanyEntry; config: CompaniesConfig; reload: () => void }) {
  const { busy, run } = useApp();
  // The engine offers the legal name without its legal form while the firm has no other name
  const [alias, setAlias] = useState(c.alias_suggestion ?? "");
  const aliases = c.aliases ?? [];
  const clean = alias.trim().replace(/\s+/g, " ");
  const send = async (action: "company-alias-add" | "company-alias-remove", name: string) => {
    const r = await run<CompanyResult>("ALL", () => postCompany(action, { key: c.key, alias: name }), undefined, () => false);
    reload();
    return r;
  };
  const id = `names-${c.key}`;
  // the button pressed goes away with its name (or the form with the last free place): once the list read again is
  // shown, focus moves to the field, else to the heading
  const refocusing = useRef(false);
  const refocus = () => {
    refocusing.current = true;
  };
  useEffect(() => {
    if (!refocusing.current) return;
    refocusing.current = false;
    (document.getElementById(`${id}-new`) ?? document.getElementById(id))?.focus();
  }, [c.aliases, id]);
  const add = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !clean) return;
    const r = await send("company-alias-add", clean);
    if (r?.ok) {
      setAlias("");
      refocus();
    }
  };
  const remove = async (name: string) => {
    const r = await send("company-alias-remove", name);
    if (r?.ok) refocus();
  };
  const full = aliases.length >= (config.limits.aliases_max ?? Infinity);
  return (
    <section className="border-0 border-t border-solid border-line p-5 sm:p-6" aria-labelledby={id}>
      <h3 className="m-0 text-base font-semibold focus:outline-none" id={id} tabIndex={-1}>Alte nume ale firmei</h3>
      <p className={noteLeftStyle} id={`${id}-note`}>
        Un document este al firmei când are pe el CUI-ul ei, denumirea de la ANAF sau unul dintre aceste nume.
      </p>
      {aliases.length ? (
        <ul className="my-4 list-none p-0 [&>li]:flex [&>li]:min-h-11 [&>li]:border-0 [&>li]:border-b [&>li]:border-solid [&>li]:border-line [&>li]:items-center [&>li]:justify-between [&>li]:gap-3 [&_span]:min-w-0 [&_span]:break-words">
          {aliases.map((a) => (
            <li key={a}>
              <span>{a}</span>
              <button type="button" className={linkButtonStyle} disabled={busy} aria-label={`Șterge numele ${a}`}
                onClick={() => remove(a)}>Șterge</button>
            </li>
          ))}
        </ul>
      ) : null}
      {full ? null : (
        <Form className="m-0" onSubmit={add} aria-label={`Adaugă un nume pentru ${c.name}`}>
          <Label htmlFor={`${id}-new`}>Nume nou</Label>
          <Input id={`${id}-new`} value={alias} onChange={(e) => setAlias(e.target.value)} autoComplete="off"
            maxLength={config.limits.alias_max ?? 100} aria-describedby={`${id}-note`} />
          <Actions>
            <Button type="submit" variant="secondary" disabled={busy || !clean}>Adaugă numele</Button>
          </Actions>
        </Form>
      )}
    </section>
  );
}
