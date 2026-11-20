"use client";
import { ActionColumn, Actions, Button, Panel, bannerLateStyle, noteLeftStyle, surfaceStyle } from "./Controls";
// The public receipt page (/u/<id>.<secret>): anyone holding the link photographs receipts or picks PDFs and sends them
// into one company's inbox. It shows nothing of the company but its name and the link's label.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { nOf } from "@/lib/format";
import { MAX_FILE, accepted, batches, failure, linkInfo, sendFiles, uploadBase, type LinkInfo } from "@/lib/upload";
import { I } from "./ui";

/** A chosen file; `ok` is null while its first bytes are being checked. */
type Picked = { key: number; file: File; url: string; big: boolean; ok: boolean | null };

/** The page is used one-handed on a phone: its few actions are large. */
const BIG = "min-h-14 text-base [&_svg]:size-5";
const ACCEPT = "image/jpeg,image/png,image/heic,image/heif,.heic,.heif,application/pdf";

/** A whole batch that failed this way may still go through one file at a time: too slow for the engine's deadline (408),
 * more files than the link has left (409; one at a time, those that fit go and the next gets 404), or no confirmation (0).
 * After a 0 a file the engine did keep is sent again: it's stored once, but counts again against the link's limits. */
const SPLIT = new Set([0, 408, 409]);

export default function Upload() {
  // undefined while reading; then the link's base and what the engine says about it (null: not a link it knows)
  const [link, setLink] = useState<{ base: string; info: LinkInfo } | null | undefined>(undefined);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    const base = uploadBase(window.location.pathname);
    (base ? linkInfo(base) : Promise.resolve(null)).then((info) => {
      if (live) setLink(base && info ? { base, info } : null);
    }, () => live && setFailed(true));
    return () => {
      live = false;
    };
  }, []);
  if (failed) {
    return (
      <Shell>
        <h1>Pagina nu s-a putut încărca</h1>
        <p className={noteLeftStyle}>Verifică conexiunea și încearcă din nou.</p>
        <Actions><Button type="button" className={BIG} onClick={() => window.location.reload()}>Reîncearcă</Button></Actions>
      </Shell>
    );
  }
  if (link === undefined) return <Shell><p className={noteLeftStyle} role="status">Se încarcă…</p></Shell>;
  if (!link) {
    return (
      <Shell>
        <h1>Linkul Bondrop nu mai este valabil</h1>
        <p className={noteLeftStyle}>Cere un link nou persoanei care ți l-a trimis.</p>
      </Shell>
    );
  }
  return <Sender base={link.base} info={link.info} />;
}

function Shell({ children }: { children: ReactNode }) {
  return <main className="mx-auto w-full max-w-[620px] px-5 pt-[max(2rem,env(safe-area-inset-top))] pb-12 text-ink [&_h1]:mx-0 [&_h1]:mb-3 [&_h1]:text-2xl [&_h1]:font-semibold [&_h1]:tracking-tight [&_p]:mx-0 [&_p]:text-sm"><ActionColumn>{children}</ActionColumn></main>;
}

function Sender({ base, info }: { base: string; info: LinkInfo }) {
  const [picked, setPicked] = useState<Picked[]>([]);
  const [busy, setBusy] = useState<{ done: number; of: number } | null>(null);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(0);
  const camera = useRef<HTMLInputElement>(null);
  const files = useRef<HTMLInputElement>(null);
  const choose = useRef<HTMLButtonElement>(null);
  const result = useRef<HTMLParagraphElement>(null);
  const banner = useRef<HTMLParagraphElement>(null);
  const n = useRef(0);
  const urls = useRef(new Set<string>());
  // The tab names the feature and, once the link is known, the firm it sends to.
  useEffect(() => {
    document.title = `Bondrop, ${info.company}`;
  }, [info.company]);
  useEffect(() => {
    const all = urls.current;
    return () => all.forEach((u) => URL.revokeObjectURL(u));
  }, []);
  const drop = (p: Picked) => {
    if (p.url) {
      URL.revokeObjectURL(p.url);
      urls.current.delete(p.url);
    }
  };

  const add = (list: FileList | null) => {
    if (!list?.length) return;
    setError("");
    setSent(0);
    const more: Picked[] = [...list].map((file) => {
      const url = file.type.startsWith("image/") ? URL.createObjectURL(file) : "";
      if (url) urls.current.add(url);
      return { key: n.current++, file, url, big: file.size > MAX_FILE, ok: null };
    });
    setPicked((p) => [...p, ...more]);
    for (const m of more) {
      const set = (ok: boolean) => setPicked((p) => p.map((x) => (x.key === m.key ? { ...x, ok } : x)));
      accepted(m.file).then(set, () => set(false));
    }
  };
  const remove = (key: number, i: number) => {
    setPicked((p) => {
      const gone = p.find((x) => x.key === key);
      if (gone) drop(gone);
      return p.filter((x) => x.key !== key);
    });
    // Focus goes to the next file's button, or back to choosing files.
    setTimeout(() => (document.querySelectorAll<HTMLButtonElement>("[data-upload-remove]")[i] ?? choose.current)?.focus());
  };

  const ready = picked.filter((p) => !p.big && p.ok === true);
  const checking = picked.some((p) => p.ok === null);
  const send = async () => {
    const groups = batches(ready.map((p) => p.file));
    const total = groups.reduce((a, g) => a + g.length, 0);
    let done = 0;
    let stop: number | null = null; // the status that stopped sending
    setError("");
    setBusy({ done, of: total });
    const arrived = (g: File[]) => {
      done += g.length;
      setBusy({ done, of: total });
      // What arrived leaves the list (and its preview the memory); what didn't stays, to send again.
      setPicked((p) => p.filter((x) => {
        if (!g.includes(x.file)) return true;
        drop(x);
        return false;
      }));
    };
    for (const g of groups) {
      const status = await sendFiles(base, g);
      if (status === 200) {
        arrived(g);
        continue;
      }
      if (g.length > 1 && SPLIT.has(status)) {
        for (const f of g) {
          const one = await sendFiles(base, [f]);
          if (one !== 200) {
            stop = one;
            break;
          }
          arrived([f]);
        }
      } else {
        stop = status;
      }
      if (stop !== null) break;
    }
    setBusy(null);
    setSent(done);
    if (stop !== null) setError(failure(stop));
    setTimeout(() => (banner.current ?? result.current)?.focus());
  };

  const status = sent && !error ? `${delivered(sent)}.` : busy ? `Se trimit… ${busy.done} din ${busy.of}` : "";
  return (
    <Shell>
      <h1>{info.company}</h1>
      <p className={noteLeftStyle}>Bonurile fotografiate și facturile PDF ajung direct în contabilitatea firmei.</p>
      <p className="mt-2 mb-6 text-sm text-muted">{info.label}</p>
      <input ref={camera} type="file" accept="image/*" capture="environment" hidden onChange={(e) => {
        add(e.target.files);
        e.target.value = "";
      }} />
      <input ref={files} type="file" accept={ACCEPT} multiple hidden onChange={(e) => {
        add(e.target.files);
        e.target.value = "";
      }} />
      <Panel className="grid gap-3">
        <Button type="button" className={BIG} disabled={!!busy} onClick={() => camera.current?.click()}><I n="camera" />Fotografiază un bon</Button>
        <Button ref={choose} type="button" variant="secondary" className={BIG} disabled={!!busy} onClick={() => files.current?.click()}>
          <I n="image" />Alege fișiere
        </Button>
      </Panel>
      {/* One live region, always there, so what changes in it is read out. */}
      <p ref={result} className={`my-4 flex items-center gap-2 text-sm font-medium ${sent && !error ? "text-ok" : "text-muted"}`} role="status" tabIndex={-1}>
        {sent && !error ? <I n="check-circle:fill" /> : null}{status}
      </p>
      {error ? <p ref={banner} className={`${bannerLateStyle} my-4 font-medium leading-relaxed`} role="alert" tabIndex={-1}>{sent ? `${delivered(sent)}. ` : ""}{error}</p> : null}
      {picked.length ? (
        <>
          <ul className={`my-5 list-none divide-x-0 divide-y divide-solid divide-line p-0 ${surfaceStyle}`} aria-label="Fișiere alese">
            {picked.map((p, i) => (
              <li key={p.key} className="flex items-center gap-3 p-4">
                <Thumb url={p.url} pdf={p.file.type === "application/pdf"} />
                <span className="min-w-0 flex-1 text-sm leading-relaxed">
                  <b className="block font-medium wrap-anywhere">{p.file.name}</b>
                  <small className={`block text-xs ${p.big || p.ok === false ? "text-danger" : "text-muted"}`}>
                    {p.big ? "Peste 15 MB, nu se poate trimite" : p.ok === false ? "Nu e fotografie sau PDF, nu se poate trimite" : size(p.file.size)}
                  </small>
                </span>
                <Button type="button" data-upload-remove variant="quiet" className="size-11 p-0" disabled={!!busy} aria-label={`Scoate ${p.file.name}`} onClick={() => remove(p.key, i)}>
                  <I n="x" />
                </Button>
              </li>
            ))}
          </ul>
          <Button type="button" className={`${BIG} w-full`} disabled={!!busy || checking || !ready.length} onClick={send}>
            {busy ? "Se trimit…" : `Trimite ${nOf(ready.length, "fișierul", "fișiere")}`}
          </Button>
        </>
      ) : null}
    </Shell>
  );
}

/** A preview of a chosen photo; an icon for a PDF or a photo the browser can't show (HEIC in most browsers). */
/** "A fost trimis un fișier", "Au fost trimise 3 fișiere": what reached the engine, the verb of the button. */
function delivered(n: number): string {
  return `${n === 1 ? "A fost trimis" : "Au fost trimise"} ${nOf(n, "un fișier", "fișiere")}`;
}

function Thumb({ url, pdf }: { url: string; pdf: boolean }) {
  const [broken, setBroken] = useState(false);
  if (!url || broken) return <span className="grid size-12 shrink-0 place-items-center rounded-control bg-inset text-muted"><I n={pdf ? "file-text" : "image"} /></span>;
  // eslint-disable-next-line @next/next/no-img-element -- a local preview of the chosen file
  return <img src={url} alt="" width={48} height={48} className="size-12 shrink-0 rounded-control object-cover" decoding="async" loading="lazy" onError={() => setBroken(true)} />;
}

function size(b: number): string {
  return b < 1024 * 1024 ? `${Math.max(1, Math.round(b / 1024))} KB` : `${(b / 1024 / 1024).toFixed(1).replace(".", ",")} MB`;
}
