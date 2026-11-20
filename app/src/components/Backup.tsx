"use client";
import { Anchor, Button, Panel, bannerStyle } from "./Controls";
import { backupUrl, getBackup } from "@/lib/api";
import { dmt } from "@/lib/format";
import { useApp } from "./context";
import { Loading } from "./ui";
import { useLoaded } from "./Paging";

export function Backup() {
  const { view } = useApp();
  const { data, failed, current, reload } = useLoaded(getBackup, "backup", view);
  if (data === null) return null;
  if (data === undefined && !failed) return <Loading rows={1} />;
  return <section aria-label="Copii de siguranță">
    <h3 className="mx-0 mt-5 mb-2 text-base font-semibold" tabIndex={-1}>Copii de siguranță</h3>
    {failed && !current ? <Loading rows={1} /> : failed ? <div className={bannerStyle} role="alert">
      <b>Copiile de siguranță nu au putut fi încărcate.</b>
      <Button type="button" variant="secondary" onClick={(e) => {
        e.currentTarget.closest("section")?.querySelector("h3")?.focus();
        reload();
      }}>Reîncearcă</Button>
    </div> : data ? <Panel className="flex flex-wrap items-center justify-between gap-3">
      <p className="m-0 text-sm text-muted">{data.last_backup ? `Ultima copie: ${dmt(data.last_backup, view.today)}.` : "Nu există încă o copie de siguranță."}</p>
      {data.last_backup && <Anchor href={backupUrl()} download>Descarcă copia de siguranță</Anchor>}
    </Panel> : <Loading />}
  </section>;
}
