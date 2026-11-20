// The public receipt page against a fake engine: the link's company and label, an unknown link, choosing photos and
// PDFs, files the engine wouldn't take (too big, not a photo or PDF), batches the engine accepts, a slow link retried one file at
// a time, an answer that isn't the engine's confirmation, and what stays to resend after a refusal.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Upload from "@/components/Upload";
import { accepted, batches, MAX_FILE, uploadBase } from "@/lib/upload";

const LINK = `/u/0123456789abcdef.${"ab".repeat(32)}/`;

type Sent = { url: string; files: string[] };
type Answer = number | { status: number; body: unknown } | "throw";

/** A fake engine for one link: info, then each POST answered in turn by `answers` (the engine's confirmation when they run out). */
function link({ info = 200, answers = [] as Answer[] } = {}) {
  const sent: Sent[] = [];
  const fetch = vi.fn(async (url: string, init: RequestInit = {}) => {
    if (init.method === "POST") {
      const names = (init.body as FormData).getAll("files").map((f) => (f as File).name);
      sent.push({ url, files: names });
      const a = answers.shift() ?? 200;
      if (a === "throw") throw new TypeError("network");
      if (typeof a === "object") return new Response(JSON.stringify(a.body), { status: a.status });
      return new Response(JSON.stringify(a === 200 ? { ok: true, files: names.map((_, i) => ({ sha: `h${i}` })) } : { ok: false }), { status: a });
    }
    return info === 200
      ? new Response(JSON.stringify({ company: "Firma Exemplu SRL", label: "Telefonul de serviciu" }), { status: 200 })
      : new Response("", { status: info });
  });
  vi.stubGlobal("fetch", fetch);
  return { fetch, sent };
}

const JPEG = [0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1];
const PDF = [...Buffer.from("%PDF-1.7\n%..")];
const WEBP = [...Buffer.from("RIFF\0\0\0\0WEBP")];

const file = (name: string, size = 1000, bytes = JPEG, type = "image/jpeg") => {
  const f = new File([new Uint8Array(bytes)], name, { type });
  Object.defineProperty(f, "size", { value: size });
  return f;
};

const pickInput = () => document.querySelector("input[multiple]") as HTMLInputElement;
const ready = async (n: number) => screen.findByRole("button", { name: `Trimite ${n === 1 ? "fișierul" : `${n} fișiere`}` });

beforeEach(() => {
  window.history.replaceState(null, "", LINK);
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() }));
});
afterEach(() => vi.unstubAllGlobals());

describe("The receipt page", () => {
  it("reads the link from its own path only, in the form the engine makes", () => {
    expect(uploadBase(LINK)).toBe(LINK);
    expect(uploadBase(LINK.slice(0, -1))).toBe(LINK);
    expect(uploadBase("/u/abc123.S3cr3t_x")).toBeNull();
    expect(uploadBase(`${LINK}../web/`)).toBeNull();
    expect(uploadBase("/web/")).toBeNull();
  });

  it("an unknown, expired or revoked link says so", async () => {
    link({ info: 404 });
    render(<Upload />);
    expect(await screen.findByRole("heading", { name: "Linkul Bondrop nu mai este valabil" })).toBeInTheDocument();
    expect(screen.getByText("Cere un link nou persoanei care ți l-a trimis.")).toBeInTheDocument();
  });

  it("shows whom the link sends to, and sends the photos and PDFs chosen", async () => {
    const api = link();
    render(<Upload />);
    expect(await screen.findByRole("heading", { name: "Firma Exemplu SRL" })).toBeInTheDocument();
    expect(screen.getByText("Telefonul de serviciu")).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Bondrop, Firma Exemplu SRL"));
    expect(api.fetch).toHaveBeenCalledWith(`${LINK}api/info`, expect.objectContaining({ redirect: "error" }));
    await userEvent.upload(pickInput(), [file("bon.jpg"), file("factura.pdf", 2_500_000, PDF, "application/pdf")]);
    const list = within(screen.getByRole("list", { name: "Fișiere alese" }));
    expect(list.getByText("factura.pdf")).toBeInTheDocument();
    expect(list.getByText("2,4 MB")).toBeInTheDocument();
    await userEvent.click(await ready(2));
    expect(await screen.findByText("Au fost trimise 2 fișiere.")).toBeInTheDocument();
    expect(api.sent).toEqual([{ url: `${LINK}api/files`, files: ["bon.jpg", "factura.pdf"] }]);
    expect(screen.queryByRole("list", { name: "Fișiere alese" })).toBeNull();
    expect(screen.getByRole("status")).toHaveFocus(); // the result, read out where focus lands
    expect(URL.revokeObjectURL).toHaveBeenCalled(); // previews of what arrived are let go
  });

  it("a file over 15 MB or that isn't a photo or PDF is marked and never sent; one can be taken out", async () => {
    const api = link();
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [file("mare.jpg", MAX_FILE + 1), file("poza.jpg", 1000, WEBP), // WebP bytes under a JPEG name
      file("a.jpg"), file("b.jpg")]);
    expect(screen.getByText("Peste 15 MB, nu se poate trimite")).toBeInTheDocument();
    expect(await screen.findByText("Nu e fotografie sau PDF, nu se poate trimite")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Scoate b.jpg" }));
    await userEvent.click(await ready(1));
    await waitFor(() => expect(api.sent).toHaveLength(1));
    expect(api.sent[0].files).toEqual(["a.jpg"]);
    expect(await screen.findByText("A fost trimis un fișier.")).toBeInTheDocument();
    expect(screen.getByText("mare.jpg")).toBeInTheDocument(); // stays, to be taken out
    expect(screen.getByText("poza.jpg")).toBeInTheDocument();
  });

  it("taking a file out moves focus to the next one", async () => {
    link();
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [file("a.jpg"), file("b.jpg")]);
    await userEvent.click(screen.getByRole("button", { name: "Scoate a.jpg" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Scoate b.jpg" })).toHaveFocus());
    await userEvent.click(screen.getByRole("button", { name: "Scoate b.jpg" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Alege fișiere" })).toHaveFocus());
  });

  it("an answer that isn't the engine's confirmation (a Wi-Fi login page) keeps every file", async () => {
    const api = link({ answers: [{ status: 200, body: { ok: true } }] });
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [file("bon.jpg")]);
    await userEvent.click(await ready(1));
    expect(await screen.findByRole("alert")).toHaveTextContent("Nu s-a confirmat primirea. Verifică conexiunea și încearcă din nou.");
    expect(screen.getByText("bon.jpg")).toBeInTheDocument();
    expect(screen.queryByText(/fost trimis/)).toBeNull();
    expect(api.fetch.mock.calls.find(([, i]) => (i as RequestInit).method === "POST")?.[1]).toMatchObject({ redirect: "error" });
  });

  it("a batch too slow for the engine's deadline goes again one file at a time", async () => {
    const api = link({ answers: [408] });
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [file("a.jpg"), file("b.jpg"), file("c.jpg")]);
    await userEvent.click(await ready(3));
    expect(await screen.findByText("Au fost trimise 3 fișiere.")).toBeInTheDocument();
    expect(api.sent.map((s) => s.files)).toEqual([["a.jpg", "b.jpg", "c.jpg"], ["a.jpg"], ["b.jpg"], ["c.jpg"]]);
  });

  it("when even one file is too slow, says it's the connection and keeps what's left", async () => {
    link({ answers: [408, 200, 408] });
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [file("a.jpg"), file("b.jpg"), file("c.jpg")]);
    await userEvent.click(await ready(3));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A fost trimis un fișier. Conexiunea este prea lentă pentru aceste fișiere. Încearcă să trimiți dintr-un loc cu semnal mai bun.");
    expect(within(screen.getByRole("list", { name: "Fișiere alese" })).getAllByRole("listitem")).toHaveLength(2);
  });

  it("more files than the link has left: one at a time, those that fit go, then the link says it's done", async () => {
    const api = link({ answers: [409, 200, 200, 404] });
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [file("a.jpg"), file("b.jpg"), file("c.jpg")]);
    await userEvent.click(await ready(3));
    expect(await screen.findByRole("alert")).toHaveTextContent("Au fost trimise 2 fișiere. Linkul Bondrop nu mai este valabil. Cere un link nou persoanei care ți l-a trimis.");
    expect(api.sent.map((s) => s.files)).toEqual([["a.jpg", "b.jpg", "c.jpg"], ["a.jpg"], ["b.jpg"], ["c.jpg"]]);
    expect(screen.getByRole("alert")).toHaveFocus();
  });

  it("no confirmation for a batch: one at a time", async () => {
    const api = link({ answers: [{ status: 200, body: "<html>login</html>" }] });
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [file("a.jpg"), file("b.jpg")]);
    await userEvent.click(await ready(2));
    expect(await screen.findByText("Au fost trimise 2 fișiere.")).toBeInTheDocument();
    expect(api.sent.map((s) => s.files.length)).toEqual([2, 1, 1]);
  });

  for (const [status, text] of [[415, "Trimite doar fotografii (JPEG, PNG, HEIC) și PDF-uri."],
    [503, "Serverul este ocupat. Încearcă din nou peste câteva secunde."]] as const) {
    it(`a ${status} isn't retried file by file`, async () => {
      const api = link({ answers: [status] });
      render(<Upload />);
      await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
      await userEvent.upload(pickInput(), [file("a.jpg"), file("b.jpg")]);
      await userEvent.click(await ready(2));
      expect(await screen.findByRole("alert")).toHaveTextContent(text);
      expect(api.sent).toHaveLength(1);
    });
  }

  it("Trimite waits until every file chosen has been checked", async () => {
    link();
    let finish!: () => void;
    const slow = file("a.jpg");
    const gate = new Promise<void>((ok) => (finish = ok));
    const real = slow.slice.bind(slow);
    slow.slice = ((...a: Parameters<File["slice"]>) => {
      const part = real(...a);
      return Object.assign(part, { arrayBuffer: async () => { await gate; return Blob.prototype.arrayBuffer.call(real(...a)); } });
    }) as File["slice"];
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [slow]);
    expect(screen.getByRole("button", { name: /^Trimite/ })).toBeDisabled();
    finish();
    expect(await ready(1)).toBeEnabled();
  });

  it("sends many files in batches the engine accepts, and keeps what was refused to send again", async () => {
    const api = link({ answers: [200, 429] });
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), Array.from({ length: 12 }, (_, i) => file(`bon${i + 1}.jpg`)));
    await userEvent.click(await ready(12));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Au fost trimise 10 fișiere. S-au trimis multe fișiere prin acest link. Încearcă din nou mai târziu.");
    expect(api.sent.map((s) => s.files.length)).toEqual([10, 2]); // a quota isn't retried file by file
    const left = within(screen.getByRole("list", { name: "Fișiere alese" }));
    expect(left.getAllByRole("listitem").map((li) => li.querySelector("b")?.textContent)).toEqual(["bon11.jpg", "bon12.jpg"]);
    expect(screen.getByRole("button", { name: "Trimite 2 fișiere" })).toBeEnabled();
  });

  it("no answer at all keeps every file and says to check the connection", async () => {
    link({ answers: ["throw", "throw"] });
    render(<Upload />);
    await screen.findByRole("heading", { name: "Firma Exemplu SRL" });
    await userEvent.upload(pickInput(), [file("bon.jpg")]);
    await userEvent.click(await ready(1));
    expect(await screen.findByRole("alert")).toHaveTextContent("Nu s-a confirmat primirea. Verifică conexiunea și încearcă din nou.");
    expect(screen.getByText("bon.jpg")).toBeInTheDocument();
  });
});

describe("batches and file types", () => {
  it("never puts more than 10 files or 8 MB in one request", () => {
    const sized = (n: number, mb: number) => Array.from({ length: n }, (_, i) => file(`f${i}`, mb * 1024 * 1024));
    expect(batches(sized(3, 3)).map((b) => b.length)).toEqual([2, 1]);
    expect(batches(sized(2, 14)).map((b) => b.length)).toEqual([1, 1]);
    expect(batches(Array.from({ length: 21 }, (_, i) => file(`s${i}`))).map((b) => b.length)).toEqual([10, 10, 1]);
    expect(batches([file("x", MAX_FILE + 1)])).toEqual([]);
  });

  it("knows a photo or PDF by its first bytes, with the engine's own rule", async () => {
    /** An ftyp box: its size, the main brand, a minor version and the compatible brands. */
    const ftyp = (main: string, ...compat: string[]) => {
      const size = 16 + 4 * compat.length;
      return [0, 0, 0, size, ...Buffer.from(`ftyp${main}\0\0\0\0${compat.join("")}`)];
    };
    const heic = ftyp("heic", "mif1");
    const mif1 = ftyp("mif1", "heic"); // a HEIF photo whose main brand is mif1: the engine takes it
    const avif = ftyp("avif", "mif1");
    const short = [...Buffer.from("\0\0\0\x0cftypheic")]; // 12 bytes, box too small: the engine refuses it
    const badSize = [0, 0, 0x20, 0, ...Buffer.from("ftypheic\0\0\0\0")]; // size past what was read
    expect(await accepted(file("a.heif", 100, mif1))).toBe(true);
    expect(await accepted(file("b.heic", 100, short))).toBe(false);
    expect(await accepted(file("c.heic", 100, badSize))).toBe(false);
    const png = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0];
    for (const [bytes, ok] of [[JPEG, true], [png, true], [PDF, true], [heic, true], [avif, false], [WEBP, false]] as const) {
      expect(await accepted(file("x", 100, [...bytes]))).toBe(ok);
    }
    expect(await accepted(file("factura.pdf", 100, [...Buffer.from("not a pdf at all")], "application/pdf"))).toBe(false);
  });
});
