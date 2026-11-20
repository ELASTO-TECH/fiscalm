"use client";
import { Actions, Button, Input, Label, Panel, sectionHeadingStyle, detailListStyle, bannerLateStyle, noteLeftStyle } from "./Controls";
// Parteneri: who the company trades with (the engine's `partners`), the list under Facturi and a partner's sheet, which
// leads to every invoice with them; each invoice leads back here (docs/information-architecture.md).
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { andList, cap, curOf, dm, lei, nOf, nonzero, phrase, sums } from "@/lib/format";
import { ApiError, getBrowse, getPartnerPages, post, postInvoicing } from "@/lib/api";
import { coName, isAll, parts, type Client, type Co, type InvoicingResult, type Partner, type State, type PartnerPages, type PartnerRows, type PartnerCollection, type Page, type Turnover } from "@/lib/types";
import { Company, useApp, useCan, useCompany } from "./context";
import { ListTools, PageNav, usePage } from "./Paging";
import { HistoryRow, IssuedRow, ReceivedRow } from "./Facturi";
import { ClientForm, editable, InvoicingFailed, useInvoicing } from "./Invoicing";
import { CloseX, SHEET_SHAPES, Sheet, SheetHead, SheetLoading } from "./Sheets";
import { NewInvoice } from "./Tabs";
import { TxRow } from "./Tranzactii";
import { RememberedList } from "./RememberedList";
import { REPORT_FIELDS } from "./ReportUpload";
import { Act, Empty, I, L, Off, Row, Summary, Sums } from "./ui";

const ROLE: Record<string, string> = { supplier: "Furnizor", client: "Client", courier: "Curier", processor: "Procesator" };

/** The name to show: the partner's, else its tax id; a partner known by neither has no id at all (the engine). */
export const partnerName = (p: Pick<Partner, "name" | "cui">) => p.name || `CUI ${p.cui}`;

/** "Furnizor", "Client" or "Furnizor și client". */
export const roles = (p: Pick<Partner, "roles">) => andList(p.roles.map((r) => ROLE[r] ?? r));

const owes = (m: Partner["payable"]) => Number(m.total) > 0 || m.other.length > 0;

/** "386,40 lei" or "20,00 EUR", the first amount of a balance (lei first). */
function first(m: Partner["payable"]): string {
  return Number(m.total) > 0 ? `${lei(m.total)} lei` : m.other.length ? `${lei(m.other[0].total)} ${curOf(m.other[0].currency)}` : "";
}

/** Every amount of a balance: "386,40 lei, 20,00 EUR și 5,00 USD". */
function all(m: Partner["payable"]): string {
  return andList([Number(m.total) > 0 ? `${lei(m.total)} lei` : "", ...m.other.map((o) => `${lei(o.total)} ${curOf(o.currency)}`)]
    .filter(Boolean));
}

/** "Vânzări 1.428,00 lei", "cheltuieli 595,00 lei": what was invoiced with the partner over time, history included (the
 * engine's `turnover`), never what is owed. */
const invoiced = (t: Turnover) => [nonzero(t.sales) && `Vânzări ${sums(t.sales)}`, nonzero(t.expenses) && `Cheltuieli ${sums(t.expenses)}`];

/** A partner's row; a firm ANAF lists as inactive stands out (the engine's latest registry check). */
export function PartnerRow({ p, tag }: { p: Partner; tag?: string }) {
  const [val, vsub] = owes(p.payable) ? [first(p.payable), "De plătit"] : owes(p.receivable) ? [first(p.receivable), "De încasat"] : ["", ""];
  const inactive = p.anaf_status?.inactive === true;
  return (
    <Row icon={inactive ? "warning-circle" : "user"} title={partnerName(p)} sub={phrase(tag, roles(p), p.cui && p.name ? `CUI ${p.cui}` : "", ...invoiced(p.turnover))}
      val={val} vsub={vsub} to={{ p: p.id }} badge={p.late || inactive ? "late" : ""} mark={inactive ? "Inactivă la ANAF" : ""}
      cls={inactive ? "inactive" : ""} />
  );
}

/** Facturi › Parteneri: everyone, those with a balance, those with overdue invoices; search by name or CUI. */
export function PartnerList() {
  const { view, route } = useApp();
  const [query, setQuery] = useState("");
  const ss = parts(view);
  const read = useCallback((cursor: string, signal?: AbortSignal) => getBrowse(view.co, "parteneri", { cursor, q: query, f: route.f }, signal), [view, query, route.f]);
  const page = usePage(read, query ? 200 : 0, `${view.co}|${route.f}|${query}`);
  return (
    <>
      <ListTools query={query} onQuery={setQuery} label="Caută partener sau CUI" />
      <Panel padded={false} aria-busy={page.loading}>
        <PageNav page={page} />
        {page.data.map((p) => {
          const source = ss.find((s) => s.co === p.company);
          return <PartnerRow key={p.id} p={p} tag={ss.length > 1 ? source ? coName(source) : p.company : ""} />;
        })}
        {!page.loading && !page.error && !page.total ? <Empty action={!isAll(view) && !route.f && !query ? <NewPartnerButton /> : null} /> : null}
      </Panel>
    </>
  );
}

/** The route of Parteneri › Partener nou (`p=nou`); no partner id is a bare word. */
export const NEW = "nou";

/** Parteneri's header button: a firm added by its CUI, for one company at a time. */
export function NewPartnerButton() {
  const { view } = useApp();
  if (!useCan("member")) return null;
  return isAll(view) ? <Off cls="btn" why="Alege mai întâi o firmă.">Partener nou</Off> : <L to={{ p: NEW }} className="btn">Partener nou</L>;
}

/** A partner's sheet, or Partener nou. */
export function PartnerSheet({ id, onClose }: { id: string; onClose: () => void }) {
  return id === NEW ? <NewPartner onClose={onClose} /> : <KnownPartner id={id} onClose={onClose} />;
}

const CUI_FORMAT = /^(RO)?\s*\d{2,10}$/i;

/** Partener nou: the CUI, and what ANAF's public registry says of it (`client-lookup`, data shown, never followed) saved as a
 * supplier or a client with `client-save`, the invoice form's own actions. Leaving the CUI asks ANAF; a press asks first when
 * it hasn't yet. The new partner's sheet opens in its place. */
function NewPartner({ onClose }: { onClose: () => void }) {
  const { view, route, run, busy } = useApp();
  const co = view.co;
  const [cui, setCui] = useState("");
  const [found, setFound] = useState<{ cui: string; client: Client }>();
  const [problem, setProblem] = useState("");
  const asking = useRef<{ cui: string; answer: Promise<Client | null> }>(undefined);
  const [pending, setPending] = useState("");
  // What is in the field now, and whether the sheet is still open: an answer for anything else changes nothing.
  const typed = useRef(""), open = useRef(true);
  useEffect(() => {
    open.current = true; // again after a development remount (Strict Mode)
    return () => {
      open.current = false;
    };
  }, []);
  const current = (c: string) => open.current && typed.current === c;
  const asked = cui.trim();
  const shown = found?.cui === asked ? found.client : undefined;
  const lookup = (): Promise<Client | null> => {
    if (shown) return Promise.resolve(shown);
    if (asking.current?.cui === asked) return asking.current.answer;
    setProblem("");
    setPending(asked);
    const answer = postInvoicing("client-lookup", { co, cui: asked }).catch(() => null).then((r) => {
      const c = r?.ok && r.client ? r.client as Client : null;
      if (asking.current?.cui === asked) asking.current = undefined;
      if (!open.current) return null;
      setPending((x) => (x === asked ? "" : x));
      if (c) setFound({ cui: asked, client: c }); // kept by its CUI: shown only while that CUI is in the field
      else if (current(asked)) setProblem(r?.message || "ANAF nu a răspuns. Încearcă din nou.");
      return c;
    });
    asking.current = { cui: asked, answer };
    return answer;
  };
  const add = async (role: "supplier" | "client") => {
    const client = await lookup();
    if (!client || client.inactive || !current(asked)) return; // the CUI changed or the sheet closed meanwhile
    await run(co, () => postInvoicing("client-save", { co, client, role }), undefined, (r) => {
      const p = r.state?.partners.find((x) => x.client_id === r.id);
      return { ...route, p: p?.id ?? "" };
    });
  };
  const off = busy || !CUI_FORMAT.test(asked) || !!shown?.inactive;
  return (
    <Sheet onClose={onClose} label="Partener nou">
      <CloseX onClose={onClose} />
      <SheetHead title="Partener nou" sub={isAll(view) ? null : <span className="font-normal text-muted">{coName(view)}</span>} />
      {isAll(view) ? <p className={noteLeftStyle}>Alege mai întâi o firmă.</p> : (
        <form className="my-5" onSubmit={(e) => {
          e.preventDefault();
          if (CUI_FORMAT.test(asked)) void lookup();
        }}>
          <Label htmlFor="partner-cui">CUI</Label>
          <Input id="partner-cui" data-autofocus value={cui} inputMode="numeric" autoComplete="off" aria-describedby={problem ? "partner-problem" : undefined}
            onChange={(e) => {
              typed.current = e.target.value.trim();
              setCui(e.target.value);
              setProblem("");
            }} onBlur={() => CUI_FORMAT.test(asked) && void lookup()} />
          {pending === asked && asked ? <p className={noteLeftStyle} role="status">Se caută la ANAF…</p> : null}
          {problem ? <p id="partner-problem" className={noteLeftStyle} role="alert">{problem}</p> : null}
          {shown ? (
            <Panel className="card my-4" aria-live="polite">
              <p className="m-0 font-semibold">{shown.name}</p>
              <p className="m-0 mt-1 text-sm text-muted">{phrase(where(shown.address), shown.vat_payer ? "plătitor de TVA" : "neplătitor de TVA")}</p>
              {shown.inactive ? <p className="m-0 mt-2 text-sm text-danger" role="alert">Firma figurează ca inactivă la ANAF și nu poate fi adăugată.</p> : null}
            </Panel>
          ) : null}
          <Actions>
            <Button type="button" className="btn" disabled={off} onClick={() => void add("supplier")}>Adaugă furnizor</Button>
            <Button type="button" className="btn g" disabled={off} onClick={() => void add("client")}>Adaugă client</Button>
          </Actions>
        </form>
      )}
    </Sheet>
  );
}

/** A partner's sheet: who they are, what is owed each way, and every invoice with them (each opens its own sheet; back
 * returns here). */
function KnownPartner({ id, onClose }: { id: string; onClose: () => void }) {
  const { view } = useApp();
  const scope = `${view.co}|${id}`;
  const read = useCallback(async (_cursor: string, signal?: AbortSignal) => {
    try {
      const result = await getPartnerPages(view.co, { id }, signal);
      return { data: [result], total: 1, next_cursor: null };
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return { data: [], total: 0, next_cursor: null };
      throw e;
    }
  }, [view, id]);
  const page = usePage(read, 0, scope);
  const [previous, setPrevious] = useState<{ scope: string; data: PartnerPages }>();
  const incoming = page.data[0];
  if (incoming && previous?.data !== incoming) setPrevious({ scope, data: incoming });
  const data = incoming ?? (previous?.scope === scope ? previous.data : undefined);
  if (!page.loading && !page.error && !incoming) return null;
  if (!data) return <SheetLoading shape={SHEET_SHAPES.partner} loading={page.loading} status={<PageNav page={page} />} onClose={onClose} />;
  const s = data.state;
  return <PartnerBody s={s} p={s.partners[0]} initial={data} status={page.loading || page.error ? <PageNav page={page} /> : null} onClose={onClose} />;
}

function PartnerHistory<C extends PartnerCollection>({ s, partner, collection, first, children, cls = "card", heading }: {
  s: State; partner: string; collection: C; first: Page<PartnerRows[C]>; cls?: string; heading?: ReactNode; children: (row: PartnerRows[C]) => ReactNode;
}) {
  const read = useCallback(async (cursor: string, signal?: AbortSignal) =>
    (await getPartnerPages(s.co, { id: partner, collection, cursor }, signal)).pages[collection]!, [s, partner, collection]);
  const page = usePage(read, 0, read, first);
  const label = { invoices: "Facturi primite", issued: "Facturi emise", transactions: "Tranzacții asociate", held: "La curieri și procesatori",
    settlements: "Decontări", remembered: "Reguli salvate", report_formats: "Formate de raport salvate" }[collection];
  return <Panel padded={cls.includes("pad")} className={`${cls} my-4`}>{heading}<PageNav page={page} label={label} />{page.data.map(children)}</Panel>;
}

const VAT: Record<string, string> = { registered: "Înregistrat în scopuri de TVA în România",
  not_registered: "Nestabilit și neînregistrat în scopuri de TVA în România" };

/** "Strada Exemplu 10, 400001 Cluj-Napoca, CJ", and the country when it isn't Romania. */
function where(a: NonNullable<Partner["address"]>): string {
  return [a.street, [a.postal_code, a.city].filter(Boolean).join(" "), a.county.replace(/^RO-/, ""), a.country !== "RO" ? a.country : ""]
    .filter(Boolean).join(", ");
}

/** The engine's last refresh failure, in words (never the code). */
const ANAF_ERROR: Record<NonNullable<Partner["anaf_error"]>, string> = {
  unavailable: "Ultima verificare n-a primit răspuns.",
  not_found: "ANAF nu găsește acest CUI.",
  invalid_response: "Răspunsul ANAF de la ultima verificare nu a putut fi citit.",
};

const yesNo = (b: boolean | null) => (b === null ? "" : b ? "Da" : "Nu");

/** What ANAF's public registry said at the engine's last check, as rows of the details card: an unknown flag is left out.
 * Nothing for private persons and foreign partners (the engine doesn't look them up). */
function anafRows(p: Partner, today: string): [string, string][] {
  const a = p.anaf_status;
  if (!p.anaf_eligible || !a) return [];
  const rows: [string, string][] = [
    ["Plătitor de TVA", a.vat_payer && a.vat_since ? `Da, din ${dm(a.vat_since, today)}` : yesNo(a.vat_payer)],
    ["e-Factura", yesNo(a.efactura_registered)], ["TVA la încasare", yesNo(a.vat_on_collection)], ["Inactiv", yesNo(a.inactive)],
  ];
  return rows.filter(([, v]) => v);
}

/** One line under the details: the date of the ANAF data fiscalm holds and, muted, how the last check failed; a firm never
 * checked says so. */
function AnafLine({ p, today }: { p: Partner; today: string }) {
  if (!p.anaf_eligible) return null;
  const a = p.anaf_status;
  const failed = p.anaf_error ? ANAF_ERROR[p.anaf_error] : a ? "" : "Nu a fost verificat încă la ANAF.";
  return <p className={`${noteLeftStyle} mt-3!`}>{a ? <span className="text-ink">{`Date ANAF din ${dm(a.checked_at, today)}.`}</span> : null}{a && failed ? " " : ""}{failed}</p>;
}

function PartnerBody({ s, p, initial, status, onClose }: { s: State; p: Partner; initial: PartnerPages; status: ReactNode; onClose: () => void }) {
  const { view } = useApp();
  const allowed = useCan("member");
  const [editing, setEditing] = useState(false);
  const edit = useRef<HTMLButtonElement>(null);
  const pages = initial.pages;
  const received = pages.invoices!.total, issued = pages.issued!.total;
  const tag = parts(view).length > 1;
  const done = () => {
    setEditing(false);
    setTimeout(() => edit.current?.focus());
  };
  const facts: [string, string][] = [
    ["CUI", p.cui ?? ""], ["Cod de TVA", p.vat_id && p.vat_id !== p.cui ? p.vat_id : ""], ["Adresă", p.address ? where(p.address) : ""],
    ["TVA", VAT[p.ro_vat_status ?? ""] ?? ""],
    [p.iban_last4.length > 1 ? "Conturi bancare" : "Cont bancar", p.iban_last4.map((x) => `•••• ${x}`).join(", ")],
  ];
  const anaf = anafRows(p, s.today);
  const inactive = p.anaf_status?.inactive === true;
  const name = useRef<HTMLHeadingElement>(null);
  return (
    <Sheet onClose={onClose} label={partnerName(p)} cls={SHEET_SHAPES.partner.cls}>
      <CloseX onClose={onClose} />
      <SheetHead title={partnerName(p)} titleRef={name} sub={<span className="font-normal text-muted">{phrase(roles(p), tag ? coName(s) : "")}</span>} />
      {status}
      {allowed && editing ? <Company s={s}><ClientEdit p={p} onDone={done} /></Company> : <>
        {inactive ? <p className={`banner ${bannerLateStyle} shadow-none`}><I n="warning-circle:fill" />Firma figurează ca inactivă la ANAF.</p> : null}
        {received || issued ? <Panel className="card my-4">
          <dl className={detailListStyle}>
            {p.roles.includes("supplier") ? <div className="contents">
              <dt>De plătit</dt><dd className={p.late && owes(p.payable) ? "late text-danger tabular-nums" : "tabular-nums"}>{all(p.payable) || "Nimic"}</dd>
            </div> : null}
            {issued ? <div className="contents"><dt>De încasat</dt><dd className="tabular-nums">{all(p.receivable) || "Nimic"}</dd></div> : null}
            <dt>Facturi</dt><dd>{cap(phrase(received ? nOf(received, "o factură primită", "facturi primite") : "",
              issued ? nOf(issued, "o factură emisă", "facturi emise") : ""))}</dd>
            {(["sales", "expenses"] as const).filter((k) => nonzero(p.turnover[k])).map((k) => <div key={k} className="contents">
              <dt>{k === "sales" ? "Vânzări" : "Cheltuieli"}</dt><dd className="tabular-nums"><Sums s={p.turnover[k]} />
                {nonzero(p.turnover.history[k]) ? <span className="block text-muted">din care <Sums s={p.turnover.history[k]} /> din istoric</span> : null}</dd>
            </div>)}
          </dl>
        </Panel> : null}
        {p.roles.includes("client") || p.client_id ? (
          <Actions>
            <NewInvoice className="btn" partner={p.id}>Factură nouă</NewInvoice>
            {allowed && p.client_id ? <Button ref={edit} type="button" className="btn g" disabled={isAll(view)} onClick={() => setEditing(true)}>Modifică</Button> : null}
          </Actions>
        ) : null}
        {pages.held!.total ? <section><div className={sectionHeadingStyle}><h2>La curieri și procesatori</h2></div>
          <PartnerHistory s={s} partner={p.id} collection="held" first={pages.held!}>{(h) => <Row key={h.invoice} icon="receipt"
            title={h.number || "Factură emisă"} val={`${lei(h.amount)} ${curOf(h.currency)}`} to={{ co: s.co, iss: h.invoice }} />}</PartnerHistory></section> : null}
        {received ? (
          <section aria-labelledby="p-in">
            <div className={sectionHeadingStyle}><h2 id="p-in">Facturi primite</h2></div>
            <PartnerHistory s={s} partner={p.id} collection="invoices" first={pages.invoices!}>{(r) => "kind" in r
              ? <HistoryRow key={r.id} r={r} today={s.today} byNumber /> : <ReceivedRow key={r.id} r={r} today={s.today} byNumber />}</PartnerHistory>
          </section>
        ) : null}
        {issued ? (
          <section aria-labelledby="p-out">
            <div className={sectionHeadingStyle}><h2 id="p-out">Facturi emise</h2></div>
            <PartnerHistory s={s} partner={p.id} collection="issued" first={pages.issued!}>{(r) => "kind" in r
              ? <HistoryRow key={r.id} r={r} today={s.today} byNumber /> : <IssuedRow key={r.id} r={r} today={s.today} byNumber />}</PartnerHistory>
          </section>
        ) : null}
        {pages.settlements!.total ? <PartnerHistory s={s} partner={p.id} collection="settlements" first={pages.settlements!} cls="">
          {(r) => <Row key={r.id} icon="bank" title={r.reference ? `Decontare ${r.reference}` : `Decontare din ${dm(r.date, s.today)}`} val={`${lei(r.net)} ${curOf(r.currency)}`} to={{ co: s.co, set: r.id }} />}</PartnerHistory> : null}
        {facts.some(([, v]) => v) || p.anaf_eligible ? (
          <details className="my-4" open={!received && !issued}>
            <Summary>Detalii</Summary>
            <Panel className="card my-3">
              <dl className={detailListStyle}>
                {[...facts, ...anaf].filter(([, v]) => v).map(([k, v]) => <div key={k} className="contents"><dt>{k}</dt><dd className={k === "Inactiv" && inactive ? "late text-danger" : ""}>{v}</dd></div>)}
              </dl>
              <AnafLine p={p} today={s.today} />
            </Panel>
          </details>
        ) : null}
        {p.autopay !== null && p.autopay !== undefined ? <Tick co={s.co} id={`autopay-${p.id}`} label="Pune automat în fișierul de plăți" on={p.autopay}
          shown="Se pune automat în fișierul de plăți."
          can={!!p.can_set_autopay} action="rule-supplier-autopay" body={{ cui: p.cui ?? "" }}
          about="După verificarea facturii. Fișierul îl imporți tu în bancă." /> : null}
        {p.not_mailed !== null && p.not_mailed !== undefined ? <Tick co={s.co} id={`not-mailed-${p.id}`} label="Exclude din dosarul contabilului"
          shown="Exclus din dosarul contabilului." on={p.not_mailed} can={!!p.can_set_not_mailed} action="rule-partner-not-mailed" body={{ partner: p.id }} /> : null}
        {pages.remembered!.total ? (
          <section aria-labelledby="p-kept">
            <div className={sectionHeadingStyle}><h2 id="p-kept">Reguli salvate</h2></div>
            <RememberedList s={s} partner={p.id} first={pages.remembered!} />
          </section>
        ) : null}
        {pages.report_formats!.total ? <PartnerHistory s={s} partner={p.id} collection="report_formats" first={pages.report_formats!} cls="card pad" heading={<h2>Formate de raport salvate</h2>}>
          {(f) => <div key={f.signature} className="flex flex-wrap items-center justify-between gap-3 py-2">
            <p className="m-0 min-w-0 flex-1 basis-56 wrap-anywhere">{Object.entries(f.mapping).map(([k, v]) => `${REPORT_FIELDS.find(([key]) => key === k)?.[1] ?? "Coloană"}: ${v}`).join(", ")}</p>
            <Act action="settlement-format-delete" body={{ partner: p.id, signature: f.signature }} label="Șterge formatul" cls="btn g"
              confirm={{ text: "Ștergi formatul? La următorul raport alegi din nou coloanele.", yes: "Șterge formatul", danger: true }} />
          </div>}</PartnerHistory> : null}
        {pages.transactions!.total ? (
          <section aria-labelledby="p-tx">
            <div className={sectionHeadingStyle}><h2 id="p-tx">Tranzacții asociate</h2></div>
            <PartnerHistory s={s} partner={p.id} collection="transactions" first={pages.transactions!}>{(r) => <TxRow key={r.id} t={r.transaction} s={s} description={r.said} />}</PartnerHistory>
          </section>
        ) : null}
      </>}
    </Sheet>
  );
}

/** Modifică: the saved client's details in the invoicing form, saved with its id (`client-save`). A changed CUI can give the
 * partner a new id; the sheet follows it. */
function ClientEdit({ p, onDone }: { p: Partner; onDone: () => void }) {
  const s = useCompany();
  const { route, go } = useApp();
  const { data, failed, set, retry, retrying } = useInvoicing(s.co);
  const box = useRef<HTMLFieldSetElement>(null);
  const c = data?.clients.find((x) => x.id === p.client_id);
  const shown = !!data;
  useEffect(() => {
    if (shown) box.current?.querySelector<HTMLElement>("input, button")?.focus();
  }, [shown]);
  const saved = (_: string, r: InvoicingResult) => {
    const moved = r.state?.partners.find((x) => x.client_id === p.client_id);
    if (moved && moved.id !== p.id) go({ ...route, p: moved.id }, { replace: true });
    onDone();
  };
  if (!data) return failed ? <InvoicingFailed retry={retry} retrying={retrying} /> : null;
  return (
    <div className="mt-5">
      <fieldset className="m-0 min-w-0 rounded-card border border-solid border-line p-4" ref={box}>
        <legend className="px-1 text-base font-semibold">Client</legend>
        {c ? <ClientForm inv={data} set={set} id={c.id} initial={editable(c)} onSaved={saved} onCancel={onDone} />
          : <>
            <p className={noteLeftStyle} role="alert">Clientul nu mai este salvat.</p>
            <Actions><Button type="button" className="btn g" onClick={onDone}>Renunță</Button></Actions>
          </>}
      </fieldset>
    </div>
  );
}

/** The partner's name in an invoice's sheet: a link to the partner when the engine knows one. */
/** A tick on the partner's sheet: a setting of this firm, posted at once; the sheet stays open and the tick changes in
 * place with the state the engine sends back. For an administrator, allowed before the install goes live (it prepares, never
 * pays or sends). Anyone else sees only `shown`, when the setting is on. */
function Tick({ co, id, label, shown, on, can, about, action, body }: { co: Co; id: string; label: string; shown: string; on: boolean;
  can: boolean; about?: string; action: "rule-supplier-autopay" | "rule-partner-not-mailed"; body: Record<string, string> }) {
  const { run, busy } = useApp();
  const allowed = useCan("manage_rules");
  const change = (v: boolean) => void run(co, () => post(action, { co, ...body, on: v ? "1" : "0" }), undefined, () => false);
  if (!can || !allowed) return on ? <p className={`${noteLeftStyle} my-4`}>{shown}</p> : null;
  const note = about ?? "";
  return (
    <Panel className="card my-4">
      <Label className="cursor-pointer" htmlFor={id}>
        <Input id={id} type="checkbox" checked={on} disabled={busy} aria-describedby={note ? `${id}-about` : undefined}
          onChange={(e) => change(e.target.checked)} />
        {label}
      </Label>
      {note ? <p className={`${noteLeftStyle} mt-2! pl-6`} id={`${id}-about`}>{note}</p> : null}
    </Panel>
  );
}

export function PartnerLink({ id, name, cui }: { id: string | null; name: string; cui?: string | null }) {
  const shown = name || (cui ? `CUI ${cui}` : "");
  return id ? <L to={{ p: id }} className="inline-flex items-center gap-1 text-ink no-underline hover:underline [&_svg]:size-3.5 [&_svg]:text-muted" label={`Partener: ${shown}`}>{shown}<I n="caret-right" /></L>
    : <>{shown}</>;
}

