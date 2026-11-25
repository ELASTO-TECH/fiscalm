import { clearSetupContinuation } from "./setup-continuation";
import ACTION_ROLES from "./action-roles.json";
import ACTION_EFFECTS from "./action-effects.json";
import type { Bootstrap, PaymentFileDetail, AccountSession, AccountUser, Capabilities, Identity, DetailKind, StateDetail, PartnerPages, PartnerQuery, TransactionChoiceQuery, TransactionChoices } from "./types";
// Cookie-authenticated app requests use /web/. Fixture development reads app/fixtures instead.
import type { ActionResult, DashboardCollection, DashboardRows, Ambiguous, BrowseQuery, BrowseRows, BrowseScreen, ReportPages, ApiToken, Co, CompaniesConfig, CompanyEntry, CompanyLookup, CoView, Created, DocFile, Done, Integrations, Invoicing, InvoicingResult, SearchResults, Page, Product, ProductResult, Products, TaskPage, UploadLink } from "./types";

/** Only `next dev`: the production build fails if asked for fixtures and replaces ./fixtures with a stub (next.config.ts). */
export const FIXTURES = process.env.NODE_ENV === "development" && process.env.NEXT_PUBLIC_FISCALM_ENGINE !== "1";

/** The fixtures module; next.config.ts swaps it for an empty stub in a production build. */
const fx = () => import("./fixtures");

export async function getTransactionChoices(co: CoView, query: TransactionChoiceQuery, signal?: AbortSignal): Promise<TransactionChoices> {
  if (FIXTURES) return (await fx()).fixtureTransactionChoices(co, query);
  const r = await call(`${base()}api/transaction-choices?${new URLSearchParams({ co, limit: "50", ...query })}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `transaction-choices ${r.status}`);
  return r.json();
}

export async function getDetail(co: CoView, kind: DetailKind, id: string, cursor = "", signal?: AbortSignal): Promise<StateDetail> {
  if (FIXTURES) return (await fx()).fixtureDetail(co, kind, id, cursor);
  const r = await call(`${base()}api/detail?${new URLSearchParams({ co, kind, id, cursor, limit: "50" })}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `detail ${r.status}`);
  return r.json();
}

export async function getPartnerPages(co: CoView, query: PartnerQuery, signal?: AbortSignal): Promise<PartnerPages> {
  if (FIXTURES) return (await fx()).fixturePartnerPages(co, query);
  const r = await call(`${base()}api/partner-pages?${new URLSearchParams({ co, ...query, limit: "50" })}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `partner-pages ${r.status}`);
  return r.json();
}

/** App data has one fixed prefix; credentials never come from a page path. */
export const base = (): string => "/web/";

/** status 0: no answer at all (offline, server down). */
export class ApiError extends Error {
  constructor(public status: number, message: string, public passkeysAvailable = false) {
    super(message);
  }
}

/** A POST sent with no answer back: it may have been recorded, so the user checks before sending it again. */
export const NO_ANSWER = "Nu a sosit confirmarea. Verifică dacă modificarea s-a salvat înainte să încerci din nou.";

export const CANCEL_REAUTHENTICATION = "fiscalm:cancel-reauthentication";
export const REAUTHENTICATE = "fiscalm:reauthenticate";
export type Reauthentication = { finish: (accepted: boolean) => void; signal?: AbortSignal | null };

export const SESSION_CHANGED = "fiscalm:session-changed";
export const SESSION_ENDED = "fiscalm:session-ended";
export const HTTPS_REQUIRED = "Conectarea necesită HTTPS. Deschide aplicația prin adresa HTTPS configurată.";

async function needsConfirmation(response: Response): Promise<boolean> {
  if (response.status !== 401) return false;
  const data: unknown = await response.clone().json().catch(() => null);
  return !!data && typeof data === "object" && "reauth" in data && data.reauth === true;
}

function confirmIdentity(signal?: AbortSignal | null): Promise<boolean> {
  if (typeof window === "undefined" || signal?.aborted) return Promise.resolve(false);
  return new Promise(finish => {
    const handled = !window.dispatchEvent(new CustomEvent<Reauthentication>(REAUTHENTICATE, { cancelable: true, detail: { finish, signal } }));
    if (!handled) finish(false);
  });
}

/** A confirmation the person cancelled is their choice, not a failure: the refusal carries no message to show. */
const declined = () => new Response(JSON.stringify({ ok: false, reauth: true, cancelled: true }), { status: 401, headers: { "Content-Type": "application/json" } });

let suspended = false, epoch = 0;
/** While a session has ended under a kept screen, only sign-in and identity reads go out; no polling, reload or late follow-up.
 * Each change starts a new epoch: a reply to a request sent before it cannot confirm, retry or end the session. */
export function suspendRequests(value: boolean) { if (value !== suspended) epoch++; suspended = value; }

/** A step-up the person cancelled: not a failure, nothing to show. */
export class Declined extends ApiError { constructor() { super(401, ""); } }

/** How long a read may take, answer included, before the screen says it failed and offers Reîncearcă. */
export const READ_TIMEOUT = 15_000;
/** The pause before a read's one retry. */
export const READ_RETRY_DELAY = 800;

const pause = (ms: number, signal?: AbortSignal | null) => new Promise<void>((resolve, reject) => {
  const stop = () => { clearTimeout(timer); reject(signal?.reason); };
  const timer = setTimeout(() => { signal?.removeEventListener("abort", stop); resolve(); }, ms);
  signal?.addEventListener("abort", stop, { once: true });
});

/** One read, its answer read whole within READ_TIMEOUT: a body that stops arriving fails here, where it can be asked again. */
async function readOnce(url: string, options: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timer = setTimeout(abort, READ_TIMEOUT);
  if (options.signal?.aborted) abort(); else options.signal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const body = [204, 205, 304].includes(response.status) ? null : await response.arrayBuffer();
    return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}

/** A read (GET) has a deadline and, after a failure that may pass (no answer, timeout, 502, 503, 504), one more try.
 * Anything else is sent once, as it is: a POST with no answer may have been recorded (NO_ANSWER), so only the person sends it again. */
async function request(url: string, options: RequestInit): Promise<Response> {
  if ((options.method ?? "GET").toUpperCase() !== "GET") return fetch(url, options);
  const started = epoch;
  try {
    const response = await readOnce(url, options);
    if (![502, 503, 504].includes(response.status)) return response;
  } catch (error) {
    if (options.signal?.aborted) throw error;
  }
  await pause(READ_RETRY_DELAY, options.signal);
  if (started !== epoch) throw new Error("superseded");
  return readOnce(url, options);
}

async function call(url: string, init: RequestInit = {}, authRoute = false): Promise<Response> {
  if (suspended && !authRoute) throw new ApiError(401, `suspended: ${url.split("?")[0]}`);
  const options: RequestInit = { cache: "no-store", credentials: "same-origin", ...init, headers: { Accept: "application/json", ...init.headers } };
  const requestWindow = typeof window === "undefined" ? undefined : window;
  let sourceChanged = false;
  const cancel = () => { sourceChanged = true; };
  // Capture the source lifetime before the first response, including a delayed reauth refusal.
  requestWindow?.addEventListener(CANCEL_REAUTHENTICATION, cancel);
  try {
    const started = epoch;
    let response = await request(url, options);
    let reauth = await needsConfirmation(response);
    if (started !== epoch) return reauth ? declined() : response;
    // Only an explicit refusal can be retried, once, while its source and account are still current.
    if (reauth && !sourceChanged && !url.startsWith("/auth/step-up/")) {
      if (!(await confirmIdentity(init.signal)) || sourceChanged || init.signal?.aborted) return declined();
      response = await request(url, options);
      reauth = await needsConfirmation(response);
    }
    if (response.status === 401 && !authRoute && !reauth && !sourceChanged) requestWindow?.dispatchEvent(new Event(SESSION_ENDED));
    return response;
  } catch {
    throw new ApiError(0, `no answer: ${url.split("?")[0]}`);
  } finally {
    requestWindow?.removeEventListener(CANCEL_REAUTHENTICATION, cancel);
  }
}

export class AuthError extends ApiError {
  constructor(status: number, message: string, public retryAfter: number) { super(status, message); }
}

export async function getIdentity(): Promise<Identity> {
  if (FIXTURES) return (await fx()).fixtureIdentity();
  if (typeof location !== "undefined" && location.protocol !== "https:") throw new ApiError(426, HTTPS_REQUIRED);
  // Identity readers own expiry handling so an older refusal cannot end a newer session.
  const r = await call(`${base()}api/me`, {}, true);
  if (!r.ok) {
    const body = r.status === 401 ? await r.json().catch(() => null) : null;
    throw new ApiError(r.status, `me ${r.status}`, body?.methods?.passkey === true);
  }
  return r.json();
}

/** The one-way current-session digest already exposed by /me; never a credential. */
export const identityKey = (identity: Identity): string => JSON.stringify([
  identity.me.id, identity.me.role, identity.auth.method, identity.auth.recovery_only,
  identity.sessions.find(s => s.current)?.id,
]);

export async function getBootstrap(co: CoView, identityRead = false): Promise<Bootstrap> {
  if (FIXTURES) {
    const f = await fx();
    const state = await f.fixtureSummary(co);
    return { identity: f.fixtureIdentity(), state, live: state.live };
  }
  if (typeof location !== "undefined" && location.protocol !== "https:") throw new ApiError(426, HTTPS_REQUIRED);
  const marker = () => { try { return localStorage.getItem(SESSION_CHANGED); } catch { return null; } };
  const started = marker();
  const r = await call(`${base()}api/bootstrap?${new URLSearchParams({ co })}`, {}, identityRead);
  if (!r.ok) {
    const body = r.status === 401 ? await r.json().catch(() => null) : null;
    throw new ApiError(r.status, `bootstrap ${r.status}`, body?.methods?.passkey === true);
  }
  const result: Bootstrap = await r.json();
  if (started !== marker()) throw new ApiError(409, "session changed");
  return result;
}

type PasskeyPath = "passkey/options" | "passkey/finish"
  | `${"step-up" | "setup" | "me" | "recover"}/passkey/${"options" | "finish"}`
  | "me/passkey/rename" | "me/passkey/remove";
export async function postAuth<T = { ok: boolean }>(path: PasskeyPath | "password" | "totp" | "recover/code" | "recover/password" | "step-up/password" | "step-up/totp" | "setup/inspect" | "setup/begin" | "setup/finish" | "recover/begin" | "recover/finish" | "me/password-change" | "me/totp/begin" | "me/totp/finish" | "me/recovery-codes" | "me/session-close" | "me/sessions-close-others" | "signout", body: Record<string, string>, signal?: AbortSignal): Promise<T> {
  const r = await call(`/auth/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal }, true);
  const data: T & { ok?: boolean; message?: string; cancelled?: boolean } = await r.json();
  if (!r.ok || !data.ok) throw new AuthError(r.status, data.cancelled ? "" : data.message || "Conectarea nu a reușit.", Math.min(86400, Math.max(0, Number(r.headers.get("Retry-After")))) || 0);
  if (["totp", "recover/code", "recover/password", "step-up/totp", "setup/finish", "recover/finish", "signout", "me/session-close", "me/password-change", "me/totp/finish",
    "passkey/finish", "step-up/passkey/finish", "setup/passkey/finish", "recover/passkey/finish", "me/passkey/finish", "me/passkey/remove", "me/passkey/rename"].includes(path)) {
    clearSetupContinuation();
    // The storage event reaches other tabs only. The value carries no identity or credential.
    try { localStorage.setItem(SESSION_CHANGED, crypto.randomUUID()); } catch { /* Visibility still rechecks when storage is unavailable. */ }
  }
  return data;
}

/** One current page of tasks, with complete counts; the cursor belongs to this company's current read. */
export async function getTasks(co: CoView, cursor = ""): Promise<TaskPage> {
  if (FIXTURES) return (await fx()).fixtureTasks(co, cursor);
  const q = new URLSearchParams({ co, cursor, limit: "50" });
  const r = await call(`${base()}api/tasks?${q}`);
  if (!r.ok) throw new ApiError(r.status, `tasks ${r.status}`);
  return r.json();
}

export async function getCandidates(co: Co, tx: string, cursor: string): Promise<Page<Ambiguous["candidates"][number]>> {
  if (FIXTURES) return (await fx()).fixtureCandidates(co, tx, cursor);
  const q = new URLSearchParams({ co, collection: "candidates", cursor, tx, limit: "50" });
  const r = await call(`${base()}api/state-list?${q}`);
  if (!r.ok) throw new ApiError(r.status, `state-list ${r.status}`);
  return r.json();
}

/** Search the complete entities, independently of the browser's currently loaded page. */
export async function getSearch(co: CoView, q: string, signal?: AbortSignal): Promise<SearchResults> {
  if (FIXTURES) return (await fx()).fixtureSearch(co, q);
  const r = await call(`${base()}api/search?${new URLSearchParams({ co, q })}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `search ${r.status}`);
  return r.json();
}

/** The list's filters run on complete entities; the browser retains one page and exact counts. */
export async function getBrowse<S extends BrowseScreen>(co: CoView, screen: S, query: BrowseQuery, signal?: AbortSignal): Promise<Page<BrowseRows[S]>> {
  if (FIXTURES) return (await fx()).fixtureBrowse(co, screen, query);
  const q = new URLSearchParams({ co, screen, ...query, limit: "50" });
  const r = await call(`${base()}api/browse?${q}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `browse ${r.status}`);
  return r.json();
}

/** Initial report sections share one complete server projection; later pages use browse independently. */
export async function getReportPages(co: Co, month: string, signal?: AbortSignal): Promise<ReportPages> {
  if (FIXTURES) return (await fx()).fixtureReportPages(co, month);
  const r = await call(`${base()}api/report-pages?${new URLSearchParams({ co, month, limit: "50" })}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `report-pages ${r.status}`);
  return r.json();
}

/** First dashboard pages come with totals; only continuation reads need another projection. */
export async function getDashboardPage<C extends DashboardCollection>(co: CoView, collection: C, cursor: string, signal?: AbortSignal): Promise<Page<DashboardRows[C]>> {
  if (FIXTURES) return (await fx()).fixtureDashboardPage(co, collection, cursor);
  const r = await call(`${base()}api/dashboard-list?${new URLSearchParams({ co, collection, cursor, limit: "50" })}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `dashboard-list ${r.status}`);
  return r.json();
}

/** A mailed document waiting for an answer (mailfile); png=1 asks the engine for a picture of its first page. */
export function mailFile(co: Co, key: string, png = false): string {
  if (FIXTURES) return png ? FIXTURE_IMG : "#";
  const q = new URLSearchParams({ co, key, ...(png ? { png: "1" } : {}) });
  return `${base()}mailfile?${q}`;
}

/** A receipt's file (docfile); png=1 asks for a picture of a PDF's first page. */
export function docFile(co: Co, f: DocFile, png = false): string {
  if (FIXTURES) return f.type === "pdf" ? FIXTURE_IMG : FIXTURE_BON;
  const q = new URLSearchParams({ co, sha: f.id, ...(png ? { png: "1" } : {}) });
  return `${base()}docfile?${q}`;
}

/** The payment files' own actions (POST payment-files): exact bodies, the firm resolved by the engine from the file, so only
 * `payment_file_create` names it (docs/openapi.json). */
export type FileAction =
  | { action: "payment_file_add"; file_id: number; invoice_ids: string[]; accounts?: Record<string, number>; amounts?: Record<string, string> }
  | { action: "payment_file_create"; co: Co; name: string; invoice_ids: string[]; accounts?: Record<string, number>; amounts?: Record<string, string> }
  | { action: "payment_file_rename"; file_id: number; name: string }
  | { action: "payment_file_remove"; file_id: number; payment_id: string }
  | { action: "payment_file_delete"; file_id: number }
  | { action: "payment_file_download"; file_id: number }
  | { action: "payment_file_import"; file_id: number; download_id: number }
  | { action: "payment_file_restore" | "payment_file_missing_import"; file_id: number; version: string };
export type FileActionName = FileAction["action"];
/** create answers with the new file; download with the file's bytes, which the app saves and never sends again. */
export type FileResult = ActionResult & { done?: (Done & { file_id?: number; download_id?: number; name?: string; content_type?: string; content_base64?: string }) | null };

export async function postFile(body: FileAction): Promise<FileResult> {
  if (FIXTURES) return (await fx()).fixturePostFile(body);
  const r = await call(`${base()}api/payment-files`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data: FileResult | null = await r.json().catch(() => null);
  if (!data) throw new ApiError(r.status, `${body.action} ${r.status}`);
  return data;
}

export async function getPaymentFile(id: number, signal?: AbortSignal): Promise<PaymentFileDetail> {
  if (FIXTURES) return (await fx()).fixturePaymentFile(id);
  const r = await call(`${base()}api/payment-files/${encodeURIComponent(String(id))}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `payment-files ${r.status}`);
  return r.json();
}

export type UserAction = "user-create" | "user-update" | "user-disable" | "user-enable" | "user-unlock" | "user-setup-link" | "user-reset" | "session-close";
export type Action = "upload-assign" | "document-correct" | "mode-switch" | UserAction | "history-review" | "emag-refund" | "emag-resolve" | "mark-paid" | "reopen" | "withdraw" | "pack-prepare" | "pack-sent" | "pack-unsent" | "check" | "mail-answer"
  | "settlement-confirm" | "settlement-withdraw" | "settlement-format-delete" | "settlement-format-senders" | "issued-mark" | "issued-unmark" | "resolve-debit" | "mail-proof" | "credit-pick" | "card-owner" | "tx-link" | "tx-unlink" | "tx-await" | "tx-receipt" | "memory-forget" | ProductAction | ConfigAction;
export type ProductAction = "product-add" | "product-update" | "product-archive" | "product-delete";


/** Configuration action names; shadow policy comes from the generated effect contract. */
export const CONFIG_ACTIONS = ["telemetry-settings", "update-checks", "setup-card-dismiss", "setup-card-show", "accountant-save", "rule-pause", "rule-resume", "rule-supplier-autopay", "rule-partner-not-mailed", "rule-reminders", "rule-spv-delay", "mail-detect", "mail-test", "mail-microsoft-start", "mail-microsoft-poll", "integration-add", "integration-update", "integration-clear", "integration-enable",
  "integration-default", "integration-remove", "upload-link-create", "upload-link-revoke", "token-create", "token-revoke",
  "company-lookup", "company-add", "company-update", "company-bank", "company-account", "company-account-dates", "company-statement-password", "company-alias-add",
  "company-alias-remove", "statement-csv-format", "bank-connect", "bank-disconnect", "bank-accounts", "bank-setup",
  "integration-connect"] as const;
export type ConfigAction = (typeof CONFIG_ACTIONS)[number];
export const actionRole = (action: Action | FileActionName): keyof Capabilities => (ACTION_ROLES as Record<string, string>)[action] as keyof Capabilities;
export const allowedInShadow = (a: Action | FileActionName) => (ACTION_EFFECTS as Record<string, string>)[a] === "local";

export type InvoicingOp = "logo-remove" | "seller-save" | "series-create" | "series-continue" | "series-default" | "series-remove" | "client-lookup" | "client-save" | "draft-save" | "draft-delete"
  | "issue" | "storno" | "upload" | "resolve-upload" | "recover-upload" | "auto-upload" | "recurrence-create" | "recurrence-pause" | "recurrence-resume" | "recurrence-delete";

export async function getInvoicing(co: Co): Promise<Invoicing> {
  if (FIXTURES) return (await fx()).fixtureInvoicing(co);
  const r = await call(`${base()}api/invoicing?co=${encodeURIComponent(co)}`);
  if (!r.ok) throw new ApiError(r.status, `invoicing ${r.status}`);
  return r.json();
}

/** POST api/invoicing/<op>: nested JSON (an invoice's lines), up to 64 KB. */
export async function postInvoicing(op: InvoicingOp, body: Record<string, unknown> & { co: Co }): Promise<InvoicingResult> {
  if (FIXTURES) return (await fx()).fixtureInvoicingPost(op, body);
  const r = await call(`${base()}api/invoicing/${op}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let data: InvoicingResult | null = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!data) throw new ApiError(r.status, `invoicing ${op} ${r.status}`);
  return data;
}

/** The most a logo may weigh (the engine checks it too, and its content and dimensions). */
export const LOGO_MAX = 512 * 1024;

/** POST api/invoicing/logo: the image's bytes as they are, typed PNG or JPEG. */
export async function postLogo(co: Co, file: File, type: "image/png" | "image/jpeg"): Promise<InvoicingResult> {
  if (FIXTURES) return (await fx()).fixtureInvoicingPost("logo", { co });
  const r = await call(`${base()}api/invoicing/logo?co=${encodeURIComponent(co)}`, {
    method: "POST", headers: { "Content-Type": type }, body: file,
  });
  let data: InvoicingResult | null = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!data) throw new ApiError(r.status, `logo ${r.status}`);
  return data;
}

/** The type the engine reads a statement as, from its name: it checks the content agrees. */
export type StatementType = "application/pdf" | "application/xml" | "text/plain" | "text/csv";
export const statementType = (f: File): StatementType | null =>
  /\.pdf$/i.test(f.name) || f.type === "application/pdf" ? "application/pdf"
    : /\.xml$/i.test(f.name) || /xml$/.test(f.type) ? "application/xml"
      : /\.(sta|mt940|940|txt)$/i.test(f.name) ? "text/plain"
        : /\.csv$/i.test(f.name) || f.type === "text/csv" ? "text/csv" : null;

/** A CSV's first upload on an account: its columns and first rows, for picking which is which, and per column the date
 * format or decimal separator that alone reads every row. */
export type CsvPreview = { columns: string[]; rows: string[][]; account: string;
  detected: { dates: Record<string, string>; decimals: Record<string, string> } };
export type StatementResult = ActionResult & { csv?: CsvPreview };

/** Dev fixtures: the accounts whose first CSV already asked for its columns (the next upload imports). */
const fixtureCsvAsked = new Set<string>();

/** POST api/statement-upload: one statement's bytes for a company, one of its banks and, for a CSV, one account. */
export async function postStatement(co: Co, bank: string, file: File, type: StatementType, account = ""): Promise<StatementResult> {
  if (FIXTURES) return (type === "text/csv" && !fixtureCsvAsked.has(account) && fixtureCsvAsked.add(account)
    ? { ok: false, message: "La primul extras CSV al contului, se aleg coloanele pentru dată, sumă și detalii.", next: null, done: null, state: null,
      csv: { columns: ["Data", "Detalii", "Suma", "Beneficiar"], rows: [["02.10.2026", "Chirie birou", "-1.250,50", "Imobil SRL"],
        ["15.10.2026", "Incasare factura 12", "3.000,00", "Client SRL"]], account,
        detected: { dates: { Data: "%d.%m.%Y" }, decimals: { Suma: "," } } } }
    : { ok: true, message: "Extrasul a fost încărcat.", next: null, done: null, state: null });
  const q = new URLSearchParams({ co, ...(bank ? { bank } : {}), ...(account ? { account } : {}) });
  const r = await call(`${base()}api/statement-upload?${q}`, { method: "POST", headers: { "Content-Type": type }, body: file });
  let data: StatementResult | null = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!data) throw new ApiError(r.status, `statement-upload ${r.status}`);
  return data;
}

/** POST api/statement-upload with several BT PDFs of one month (one per account, or daily) as multipart `files`: the
 * engine checks them as one statement and imports all of them or none. */
export async function postStatements(co: Co, files: File[]): Promise<StatementResult> {
  if (FIXTURES) return { ok: true, message: "Extrasul a fost încărcat.", next: null, done: null, state: null };
  const body = new FormData();
  for (const f of files) body.append("files", f, f.name);
  const r = await call(`${base()}api/statement-upload?${new URLSearchParams({ co })}`, { method: "POST", body });
  let data: StatementResult | null = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!data) throw new ApiError(r.status, `statement-upload ${r.status}`);
  return data;
}

/** POST api/document-upload: receipt photos and invoice PDFs for a company, as one multipart request (docs/openapi.json).
 * The engine's status, and on 200 each file's document id and whether those bytes were already known (then the document
 * may belong to another company, so it is never opened from here). Anything else from a 200 is 0. With `tx`, the one
 * file is that card payment's bon; a 409 then carries the engine's reason. */
export async function postDocuments(co: Co, files: File[], tx?: string): Promise<{ status: number; files: { sha: string; duplicate: boolean }[]; message?: string }> {
  if (FIXTURES) return { status: 200, files: (await (await import("./fixtures")).fixtureDocumentIds(co, files.length)).map((sha) => ({ sha, duplicate: false })) };
  const body = new FormData();
  for (const f of files) body.append("files", f, f.name);
  try {
    const r = await call(`${base()}api/document-upload?${new URLSearchParams(tx ? { co, tx } : { co })}`, { method: "POST", body });
    if (r.status === 409 && tx) {
      const data: unknown = await r.json().catch(() => null);
      const message = data && typeof data === "object" && "message" in data && typeof data.message === "string" ? data.message : undefined;
      return { status: 409, files: [], message };
    }
    if (r.status !== 200) return { status: r.status, files: [] };
    const data: unknown = await r.json().catch(() => null);
    const list = data && typeof data === "object" && "ok" in data && data.ok === true && "files" in data && Array.isArray(data.files)
      ? data.files.filter((f: unknown): f is { sha: string; duplicate: boolean } => !!f && typeof f === "object" && "sha" in f
        && typeof f.sha === "string" && /^[0-9a-f]{64}$/.test(f.sha) && "duplicate" in f && typeof f.duplicate === "boolean") : [];
    return list.length === files.length ? { status: 200, files: list } : { status: 0, files: [] };
  } catch {
    return { status: 0, files: [] };
  }
}

/** The type the engine reads an import of invoices as, from its name: it checks the content agrees. */
export const HISTORY_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
export type HistoryType = "application/zip" | "application/xml" | typeof HISTORY_XLSX;
export const historyType = (f: File): HistoryType | null =>
  /\.xlsx$/i.test(f.name) || f.type === HISTORY_XLSX ? HISTORY_XLSX : /\.zip$/i.test(f.name) || /zip/.test(f.type) ? "application/zip"
    : /\.xml$/i.test(f.name) || /xml$/.test(f.type) ? "application/xml" : null;

/** POST api/history-upload: e-Factura invoices from another program (one XML or a ZIP) for a company, with its start
 * date (docs/openapi.json). */
export async function postHistory(co: Co, start: string, file: File, type: HistoryType): Promise<ActionResult> {
  if (FIXTURES) return { ok: true, message: "Facturile au fost importate: 120 de facturi în istoric, 4 facturi urmărite de la data de început.",
    next: null, done: null, state: null };
  const r = await call(`${base()}api/history-upload?${new URLSearchParams({ co, start })}`,
    { method: "POST", headers: { "Content-Type": type }, body: file });
  let data: ActionResult | null = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!data) throw new ApiError(r.status, `history-upload ${r.status}`);
  return data;
}

/** The company's logo as the engine serves it (a given version, so a new one isn't hidden by the cache). */
export function logoUrl(co: Co, sha?: string): string {
  return FIXTURES ? "" : `${base()}invoicing/logo?${new URLSearchParams({ co, ...(sha ? { sha } : {}) })}`;
}

/** A file of an invoice issued in fiscalm (its PDF or XML), or a draft's PDF preview. */
export function invoiceFile(co: Co, id: string, kind: "pdf" | "xml"): string {
  return FIXTURES ? "#" : `${base()}invoicing/${encodeURIComponent(id)}.${kind}?co=${encodeURIComponent(co)}`;
}

export function draftPreview(co: Co, draft: string): string {
  return FIXTURES ? "#" : `${base()}invoicing/preview.pdf?${new URLSearchParams({ co, draft })}`;
}

export async function getIntegrations(): Promise<Integrations> {
  if (FIXTURES) return (await fx()).fixtureIntegrations();
  const r = await call(`${base()}api/integrations`);
  if (!r.ok) throw new ApiError(r.status, `integrations ${r.status}`);
  return r.json();
}

/** Body always has a real company (`co`): the engine refuses ALL for a company's action. */
export async function postInstall(action: "mode-switch", body: Record<string, string>): Promise<ActionResult> {
  return send(action, body);
}

export async function post(action: Action, body: Record<string, unknown> & { co: Co }): Promise<ActionResult> {
  return send(action, body);
}

/** POST api/<action> with a JSON body; only `post` (a company's action) and `postCompany` (install-wide) call it. */
async function send(action: Action, body: Record<string, unknown>): Promise<ActionResult> {
  if (FIXTURES) return (await fx()).fixturePost(action, body);
  const r = await call(`${base()}api/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data: ActionResult | null = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!data) throw new ApiError(r.status, `${action} ${r.status}`);
  return data;
}

/** `bank-connect`: the bank's page to open, where the person confirms read-only access (docs/openapi.json). Also
 * `integration-connect`: the single-use start link on the install's own address, which goes on to ANAF's login. */
export type BankConnectResult = ActionResult & { done?: (Done & { url?: string }) | null };

/** Creating or revoking a link or an API key: the answer carries what was made (`done`), the secret shown once. */
export type AccessResult = ActionResult & { done?: (Done & Created) | null };

export function postAccess(action: "upload-link-create" | "upload-link-revoke", body: Record<string, unknown> & { co: Co }): Promise<AccessResult>;
export function postAccess(action: "token-create" | "token-revoke", body: Record<string, unknown>): Promise<AccessResult>;
export async function postAccess(action: "upload-link-create" | "upload-link-revoke" | "token-create" | "token-revoke", body: Record<string, unknown>): Promise<AccessResult> {
  return (await send(action, body)) as AccessResult;
}

export async function getUsers(): Promise<AccountUser[]> {
  if (FIXTURES) return (await fx()).fixtureUsers();
  const r = await call(`${base()}api/users`);
  if (!r.ok) throw new ApiError(r.status, `users ${r.status}`);
  return r.json();
}

export async function getUserSessions(id: string): Promise<AccountSession[]> {
  if (FIXTURES) return [];
  const r = await call(`${base()}api/user-sessions?${new URLSearchParams({ id })}`);
  if (!r.ok) throw new ApiError(r.status, `user-sessions ${r.status}`);
  return r.json();
}

export type UserResult = Omit<ActionResult, "done"> & { done?: { id?: string; url?: string } | null };
export async function postUser(action: UserAction, body: Record<string, string>): Promise<UserResult> {
  return await send(action, body) as UserResult;
}

/** Adding, checking or renaming a company: install-wide, so the answer's `state` is Toate firmele's. */
export type CompanyResult = ActionResult & { done?: (Done & Partial<CompanyEntry> & Partial<CompanyLookup> & { retry?: boolean }) | null };

export async function postCompany(action: "company-lookup" | "company-add" | "company-update" | "company-bank" | "company-account"
  | "company-account-dates" | "company-statement-password" | "company-alias-add" | "company-alias-remove", body: Record<string, string>): Promise<CompanyResult> {
  return (await send(action, body)) as CompanyResult; // install-wide: no company named
}

export async function getCompanies(): Promise<CompaniesConfig> {
  if (FIXTURES) return (await fx()).fixtureCompanies();
  const r = await call(`${base()}api/companies`);
  if (!r.ok) throw new ApiError(r.status, `companies ${r.status}`);
  return r.json();
}

/** One page of the firm's products (Firma › Produse): active, or archived with `f`, searched by name or code. */
export async function getProducts(co: Co, query: { q?: string; f?: string; cursor?: string }, signal?: AbortSignal): Promise<Page<Product>> {
  if (FIXTURES) return (await fx()).fixtureProducts(co, query);
  const r = await call(`${base()}api/products?${new URLSearchParams({ co, ...query, limit: "50" })}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `products ${r.status}`);
  const page: Products = await r.json();
  return { data: page.products, total: page.total, next_cursor: page.next_cursor };
}

/** One product by its id, for a sheet opened from a link. */
export async function getProduct(co: Co, id: string, signal?: AbortSignal): Promise<Product> {
  if (FIXTURES) return (await fx()).fixtureProduct(co, id);
  const r = await call(`${base()}api/products?${new URLSearchParams({ co, id })}`, { signal });
  if (!r.ok) throw new ApiError(r.status, `products ${r.status}`);
  return (await r.json()).product;
}

/** A product action (`product-add`, `-update`, `-archive`, `-delete`): flat fields, `version` for a saved product. */
export function postProduct(action: ProductAction, body: Record<string, unknown> & { co: Co }): Promise<ProductResult> {
  return send(action, body);
}

export async function getUploadLinks(co: Co): Promise<UploadLink[]> {
  if (FIXTURES) return (await fx()).fixtureUploadLinks(co);
  const r = await call(`${base()}api/upload-links?co=${encodeURIComponent(co)}`);
  if (!r.ok) throw new ApiError(r.status, `upload-links ${r.status}`);
  return r.json();
}

/** Own credential metadata; administrators receive every owner. Never the bearer secret. */
export async function getTokens(): Promise<ApiToken[]> {
  if (FIXTURES) return (await fx()).fixtureTokens();
  const r = await call(`${base()}api/tokens`);
  if (!r.ok) throw new ApiError(r.status, `tokens ${r.status}`);
  return r.json();
}

/** The fixtures' grey first page and receipt photo, loaded once in development. */
let [FIXTURE_IMG, FIXTURE_BON] = ["", ""];
if (FIXTURES) void fx().then((m) => ([FIXTURE_IMG, FIXTURE_BON] = [m.FIXTURE_PAGE, m.FIXTURE_BON]));

/** Install-wide backup metadata; 403 means this session is not an administrator. */
export async function getBackup(): Promise<{ last_backup: string | null } | null> {
  if (FIXTURES) return { last_backup: "2026-11-03T02:00:00+00:00" };
  const r = await call(`${base()}api/backup`);
  if (r.status === 403) return null;
  if (!r.ok) throw new ApiError(r.status, `backup ${r.status}`);
  return r.json();
}

export function backupUrl(): string {
  return FIXTURES ? "#" : `${base()}backup`;
}

export const packFile = (co: Co, month: string, version: string) =>
  FIXTURES ? "#" : `${base()}packfile?co=${encodeURIComponent(co)}&month=${encodeURIComponent(month)}&v=${encodeURIComponent(version)}`;

export async function getHistoryReview(co: Co): Promise<{ rows: import("./types").HistoryReviewRow[] }> {
  if (FIXTURES) return (await fx()).fixtureHistoryReview(co);
  const r = await call(`${base()}api/history-review?${new URLSearchParams({ co })}`);
  if (!r.ok) throw new ApiError(r.status, "history-review");
  return r.json();
}
export function historyDocument(co: Co, sha: string): string {
  return `${base()}history-document?${new URLSearchParams({ co, sha })}`;
}


export type CliLogin = {
  provider: string; state: "idle" | "waiting" | "connected" | "failed" | "expired" | "cancelled" | "invalid_url";
  url: string; code: string; needs_code: boolean;
  readiness?: "ready" | "missing" | "not_logged_in"; install_command?: string;
  ok?: boolean; message?: string;
};

export async function cliLogin(provider: string, op?: "start" | "code" | "cancel" | "logout", code = ""): Promise<CliLogin> {
  if (FIXTURES) return { provider, state: op === "start" ? "waiting" : "idle", url: op === "start" ? "https://claude.ai/login" : "", code: "", needs_code: false, readiness: "not_logged_in", install_command: "docker compose up -d --build", ok: true };
  const response = await call(`${base()}api/integration-cli?provider=${encodeURIComponent(provider)}`, op ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ op, provider, ...(code ? { code } : {}) }),
  } : {});
  if (!response.ok) throw new ApiError(response.status, "cli login");
  const result: CliLogin = await response.json();
  return result.provider && result.provider !== provider ? { ...result, state: "idle", url: "", code: "", needs_code: false } : result;
}

