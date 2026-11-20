// The public receipt page, /u/<id>.<secret>: who the link sends to, and sending photos and PDFs. The link's secret is
// in the page's own path; nothing else of the engine is reachable with it (docs/openapi.json).
import { FIXTURES } from "./api";

/** The engine's limits: files per request and bytes per file. */
export const MAX_FILES = 10;
export const MAX_FILE = 15 * 1024 * 1024;
/** Bytes per request we send: far under the engine's 40 MiB, so a request finishes inside the engine's deadline (30 s + 1 s per
 * 256 KiB) on a phone's upload speed, and a failure costs little of the link's hourly quota. */
export const MAX_BATCH = 8 * 1024 * 1024;

const fx = () => import("./fixtures");

/** "/u/<id>.<secret>/" from the page's path, or null when the path isn't a link the engine could have made. */
export function uploadBase(path: string): string | null {
  const m = path.match(/^\/u\/([0-9a-f]{16}\.[0-9a-f]{64})\/?$/);
  return m ? `/u/${m[1]}/` : null;
}

export type LinkInfo = { company: string; label: string };

/** The link's company and label; null when the engine doesn't know the link (wrong, expired, revoked or used up). */
export async function linkInfo(base: string): Promise<LinkInfo | null> {
  if (FIXTURES) return (await fx()).fixtureLinkInfo();
  const r = await fetch(`${base}api/info`, { cache: "no-store", redirect: "error", headers: { Accept: "application/json" } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`info ${r.status}`);
  return r.json();
}

/** One request: 200 only when the engine confirms every file (`{ok: true, files}` with one entry each). A redirect (a hotel
 * Wi-Fi's login page), another body or no answer at all is 0: the files are not known to have arrived. */
export async function sendFiles(base: string, files: File[]): Promise<number> {
  if (FIXTURES) return (await fx()).fixtureSendFiles(files);
  const body = new FormData();
  for (const f of files) body.append("files", f, f.name);
  try {
    const r = await fetch(`${base}api/files`, { method: "POST", body, cache: "no-store", redirect: "error",
      headers: { Accept: "application/json" } });
    if (r.status !== 200) return r.status;
    const data: unknown = await r.json().catch(() => null);
    const ok = !!data && typeof data === "object" && "ok" in data && data.ok === true
      && "files" in data && Array.isArray(data.files) && data.files.length === files.length;
    return ok ? 200 : 0;
  } catch {
    return 0;
  }
}

/** Files in the order chosen, cut into requests the engine accepts. A file over MAX_FILE is never in a batch. */
export function batches(files: File[]): File[][] {
  const out: File[][] = [];
  let cur: File[] = [];
  let size = 0;
  for (const f of files) {
    if (f.size > MAX_FILE) continue;
    if (cur.length && (cur.length === MAX_FILES || size + f.size > MAX_BATCH)) {
      out.push(cur);
      cur = [];
      size = 0;
    }
    cur.push(f);
    size += f.size;
  }
  if (cur.length) out.push(cur);
  return out;
}

/** Whether the engine would take the file, by its first bytes, with the engine's own rule (`receipt_upload.sniff`): JPEG, PNG,
 * PDF, or an ISO box `ftyp` whose size is sane (16 up to the bytes read, at most 4096) and whose main or compatible
 * brands include heic/heix. A name or a type the browser guessed doesn't count. Checked before sending, so one file
 * The engine would refuse doesn't stop the others in its batch. */
export async function accepted(f: File): Promise<boolean> {
  const b = new Uint8Array(await f.slice(0, 4096).arrayBuffer());
  const at = (i: number, ...xs: number[]) => xs.every((x, j) => b[i + j] === x);
  const text = (i: number, s: string) => at(i, ...[...s].map((c) => c.charCodeAt(0)));
  if (at(0, 0xff, 0xd8, 0xff) || at(0, 0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a) || text(0, "%PDF-")) return true;
  if (b.length < 16 || !text(4, "ftyp")) return false;
  const end = ((b[0] << 24) >>> 0) + (b[1] << 16) + (b[2] << 8) + b[3];
  if (end < 16 || end > Math.min(b.length, 4096)) return false;
  for (const i of [8, ...Array.from({ length: Math.max(0, Math.ceil((end - 16) / 4)) }, (_, k) => 16 + 4 * k)]) {
    if (text(i, "heic") || text(i, "heix")) return true;
  }
  return false;
}

/** What to tell the sender for a status the engine answered (docs/openapi.json lists them). */
export function failure(status: number): string {
  switch (status) {
    case 404: return "Linkul Bondrop nu mai este valabil. Cere un link nou persoanei care ți l-a trimis.";
    case 413: return "Fișierele sunt prea mari. Trimite-le pe rând.";
    case 415: return "Trimite doar fotografii (JPEG, PNG, HEIC) și PDF-uri.";
    case 409: return "Fișierul nu s-a putut salva pe server. Încearcă din nou mai târziu.";
    case 429: return "S-au trimis multe fișiere prin acest link. Încearcă din nou mai târziu.";
    case 408: return "Conexiunea este prea lentă pentru aceste fișiere. Încearcă să trimiți dintr-un loc cu semnal mai bun.";
    case 503: return "Serverul este ocupat. Încearcă din nou peste câteva secunde.";
    case 507: return "Spațiul de stocare este plin. Fișierele nu s-au trimis.";
    case 0: return "Nu s-a confirmat primirea. Verifică conexiunea și încearcă din nou.";
    default: return "Fișierele nu au putut fi trimise. Încearcă din nou.";
  }
}
