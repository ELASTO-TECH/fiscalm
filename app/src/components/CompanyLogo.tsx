"use client";
import { Button, Input, noteLeftStyle, legendStyle } from "./Controls";
import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { LOGO_MAX, logoUrl, postLogo } from "@/lib/api";
import { I } from "./ui";
import { useApp } from "./context";
import { imageKind } from "@/lib/logo";

/** A firm's mark: with a logo (its sha), the whole logo fitted on a white tile, never cropped or stretched, so dark and
 * coloured logos stay legible in both themes; without one, or when it fails to load, its letters on its own colour
 * (`--co`, from `pal`). `className` sizes the tile; `children` (a dot) sit on top of either. */
export function FirmMark({ co, logo, letters, pal, style, className, children }: {
  co: string; logo?: string | null; letters: string; pal?: string; style?: CSSProperties; className: string; children?: ReactNode;
}) {
  // The URL that failed, only while it is still the one shown.
  const [failed, setFailed] = useState("");
  const url = logo ? logoUrl(co, logo) : "";
  if (failed && url !== failed) setFailed(""); // another or no logo: a later return to this one is tried again
  const src = url !== failed ? url : "";
  return src ? <span className={`mk relative grid shrink-0 place-items-center rounded-control border border-solid border-line bg-white p-[3px] ${className}`} aria-hidden="true">
    {/* eslint-disable-next-line @next/next/no-img-element -- company-owned logo served by the engine */}
    <img className="size-full object-contain" src={src} alt="" onError={() => setFailed(url)} />{children}
  </span> : <span className={`mk relative grid shrink-0 place-items-center rounded-control bg-[var(--co)] font-semibold text-[var(--co-ink)] ${className}`}
    data-pal={pal} style={style} aria-hidden="true">{letters}{children}</span>;
}

export type PendingLogo = { file: File; preview: string; kind: "image/png" | "image/jpeg" };

/** Local selection only. The caller binds these bytes to a server-returned company key. */
export function LogoPicker({ value, onChange, onBusy, disabled }: {
  value: PendingLogo | null; onChange: (logo: PendingLogo | null) => void; onBusy: (busy: boolean) => void; disabled: boolean;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const sequence = useRef(0);
  const [problem, setProblem] = useState("");
  useEffect(() => {
    if (!value) return;
    return () => URL.revokeObjectURL(value.preview);
  }, [value]);
  useEffect(() => () => { sequence.current += 1; }, []);
  const choose = async (file?: File) => {
    if (!file) return; // Cancelling the chooser preserves the previous selection.
    const n = ++sequence.current;
    setProblem(""); onBusy(true);
    try {
      if (file.size > LOGO_MAX) throw new Error("Imaginea poate avea cel mult 512 KB.");
      const kind = await imageKind(file);
      if (!kind) throw new Error("Se acceptă o imagine PNG sau JPEG.");
      if (n === sequence.current) onChange({ file, kind, preview: URL.createObjectURL(file) });
    } catch (e) {
      if (n === sequence.current) setProblem(e instanceof Error ? e.message : "Imaginea nu poate fi citită.");
    } finally { if (n === sequence.current) onBusy(false); }
  };
  return <fieldset className="m-0 min-w-0 border-0 p-0" disabled={disabled}>
    <legend className={legendStyle}>Sigla firmei (opțional)</legend>
    <div className="flex items-start gap-4">
      {value ? /* eslint-disable-next-line @next/next/no-img-element -- local file preview */
        <img className="size-16 shrink-0 rounded-control bg-white object-contain p-1" src={value.preview} alt="Sigla aleasă" /> : <span className="grid size-16 shrink-0 place-items-center rounded-control bg-inset text-muted" aria-hidden="true"><I n="image" /></span>}
      <div className="min-w-0"><p id={id} className={`${noteLeftStyle} mx-0 mt-0`}>PNG sau JPEG, maximum 512 KB. 32–1200 px pe latură. Apare și pe facturi.</p>
      <Input ref={input} type="file" accept="image/png,image/jpeg" hidden aria-label="Sigla firmei" onChange={e => {
        void choose(e.target.files?.[0]); e.target.value = "";
      }} />
      <div className="mt-2 flex flex-wrap gap-2">
        <Button type="button" variant="secondary" aria-describedby={id} onClick={() => input.current?.click()}>{value ? "Înlocuiește sigla" : "Încarcă o siglă"}</Button>
        {value ? <Button type="button" variant="quiet" onClick={() => { sequence.current += 1; onBusy(false); onChange(null); setProblem(""); }}>Elimină sigla</Button> : null}
      </div></div>
    </div>
    {problem ? <p className={noteLeftStyle} role="alert">{problem}</p> : null}
  </fieldset>;
}

/** Existing company logo writes retain the selected bytes for an explicit retry. */
export function CompanyLogoEditor({ company, onSaved }: { company: string; onSaved?: (sha?: string) => void }) {
  const { run, busy } = useApp();
  const [logo, setLogo] = useState<PendingLogo | null>(null);
  const [reading, setReading] = useState(false);
  const [message, setMessage] = useState("");
  return <div className="mt-6 border-0 border-t border-solid border-line pt-5">
    <LogoPicker value={logo} onChange={setLogo} onBusy={setReading} disabled={busy} />
    {logo && <Button type="button" variant="secondary" disabled={busy || reading} onClick={async () => {
      const result = await run(company, () => postLogo(company, logo.file, logo.kind), undefined, () => false);
      if (result?.ok) { setLogo(null); onSaved?.(result.invoicing?.seller?.logo?.sha); setMessage("Sigla a fost salvată."); }
    }}>Salvează sigla</Button>}
    <p role="status">{message}</p>
  </div>;
}
