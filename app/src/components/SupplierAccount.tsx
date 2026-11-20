import { useId } from "react";
import { Panel, detailListStyle } from "./Controls";
import type { Refused } from "@/lib/types";
import { useCompany } from "./context";
import { REFUSED_WHY, maskedIban, refusalReason } from "./derive";

export function RefusedAccount({ x }: { x: Refused }) {
  const s = useCompany();
  const uid = useId();
  const label = REFUSED_WHY[x.why][0];
  const sentence = refusalReason(s, x);
  const account = x.why !== "unclear";
  return (
    <section className="my-5" aria-labelledby={`${uid}-why`}>
      <Panel className="my-4">
        <div className={`mb-3 text-sm leading-relaxed [&_p]:mb-0 [&_p]:text-muted ${x.why === "ready" ? "[&_b]:text-ok" : "[&_b]:text-danger"}`}>
          <b id={`${uid}-why`}>{label}</b>
          <p>{sentence}</p>
        </div>
        {account ? (
          <>
            <dl className={`${detailListStyle} py-2`}><dt>Furnizor</dt><dd>{x.supplier || "-"}</dd></dl>
            <dl className={`${detailListStyle} py-2`}><dt>Cont bancar</dt><dd>{maskedIban(x.iban_last4)}</dd></dl>
          </>
        ) : null}
      </Panel>
    </section>
  );
}
