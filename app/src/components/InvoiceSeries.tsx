"use client";
import { Actions, Button, Panel, Skeleton, SkeletonRows, backLinkStyle, rowStyle, rowTextStyle, sectionHeadingStyle, settingsPageStyle } from "./Controls";
// Setări › Facturare: a firm's invoice series. The engine decides which series is the default and whether one can be
// removed (`remove_refusal`); the app lists them and posts the choice.
import { useState } from "react";
import type { Invoicing } from "@/lib/types";
import { useApp, useCan, useCompany } from "./context";
import { InvoicingFailed, SeriesForm, useInvoicing, useInvoicingRun } from "./Invoicing";
import { Empty, I, L, Title } from "./ui";

export function Facturare() {
  const s = useCompany();
  const { data, failed, set, retry, retrying } = useInvoicing(s.co);
  return <div className={settingsPageStyle}>
    <L to={{ tab: "setari" }} className={backLinkStyle} label="Înapoi la Setări"><I n="arrow-left" />Setări</L>
    <Title>Facturare</Title>
    {data ? <SeriesList key={s.co} inv={data} set={set} /> : failed ? <InvoicingFailed retry={retry} retrying={retrying} /> : <SeriesSkeleton />}
  </div>;
}

/** The series before they are read: the heading and a card of rows where the list will be. */
function SeriesSkeleton() {
  return <section role="status"><span className="sr-only">Se încarcă…</span>
    <div className={sectionHeadingStyle}><Skeleton className="my-1 h-5 w-40" /></div>
    <Panel padded={false}><SkeletonRows rows={2} icon={false} quiet /></Panel>
  </section>;
}

function SeriesList({ inv, set }: { inv: Invoicing; set: (d: Invoicing) => void }) {
  const send = useInvoicingRun(inv.co, set);
  const { busy } = useApp();
  const member = useCan("member");
  const [adding, setAdding] = useState(false);
  const remove = (code: string) => send("series-remove", { code },
    { text: `Ștergi seria ${code}? Nicio factură nu o folosește.`, yes: "Șterge seria", danger: true }, () => false);
  return <section aria-labelledby="serii">
    <div className={sectionHeadingStyle}><h2 id="serii">Serii de facturi</h2></div>
    <Panel padded={false} className="divide-x-0 divide-y divide-solid divide-line">
      {!inv.series.length && <Empty />}
      {inv.series.map((x) => <div key={x.code} className={`${rowStyle} flex-wrap sm:flex-nowrap`}>
        <div className={`${rowTextStyle} basis-full sm:basis-0 [&_small]:text-muted`}>
          <b className="font-semibold!">{x.code}{x.default ? <span className="ml-2 inline-block rounded-control bg-inset px-1.5 align-[1px] text-xs font-semibold leading-5 text-ink">Implicită</span> : null}</b>
          <small>{`Următorul număr: ${x.next_number}`}</small>
          <small>{x.issued === 0 ? "Nicio factură emisă" : x.issued === 1 ? "O factură emisă" : `${x.issued} facturi emise`}</small>
          {member && x.remove_refusal ? <small>{x.remove_refusal}</small> : null}
        </div>
        {member ? <div className="flex flex-wrap gap-2">
          {x.default ? null : <Button variant="secondary" disabled={busy}
            onClick={() => void send("series-default", { code: x.code }, undefined, () => false)}>Setează ca implicită</Button>}
          {x.remove_refusal ? null : <Button variant="secondary" disabled={busy} onClick={() => void remove(x.code)}>Șterge seria</Button>}
        </div> : null}
      </div>)}
    </Panel>
    {member ? adding ? <SeriesForm inv={inv} set={set} onDone={() => {
      setAdding(false);
      setTimeout(() => document.getElementById("series-add")?.focus()); // the form had focus
    }} />
      : <Actions><Button id="series-add" variant="secondary" disabled={busy} onClick={() => setAdding(true)}><I n="plus" /><span>Adaugă serie</span></Button></Actions> : null}
  </section>;
}
