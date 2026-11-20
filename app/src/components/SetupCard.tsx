"use client";
// Acasă's Configurează card: what an administrator has not set up yet for the selected firm. Every line opens the
// place in Setări where it is set up; the engine decides what is left (`setup`) and drops a line once it is done.
// Closed, it stays closed for that firm until Setări › Firme shows it again.
import { Panel, sectionHeadingStyle } from "./Controls";
import type { SetupCard as Card } from "@/lib/types";
import { useApp, useCan } from "./context";
import { Act, Row } from "./ui";

const LINES: Record<Card["left"][number]["key"], { title: string; sub: string; icon: string }> = {
  bank: { title: "Banca", sub: "Import automat de tranzacții", icon: "bank" },
  history: { title: "Facturi anterioare", sub: "Import din vechiul program de facturare", icon: "file-text" },
  shops: { title: "Magazine", sub: "Comenzile devin facturi", icon: "plugs" },
};

export function SetupCard() {
  const { view } = useApp();
  const admin = useCan("manage_firms");
  const card = "setup" in view ? view.setup : null;
  if (!card || card.hidden || !admin || view.co === "ALL") return null;
  const co = view.co;
  return (
    <section aria-labelledby="configureaza">
      <div className={sectionHeadingStyle}>
        <h2 id="configureaza">Configurează</h2>
        <Act action="setup-card-dismiss" body={{}} label="Ascunde" cls="btn lnk" />
      </div>
      <Panel padded={false} className="card min-[1100px]:grid min-[1100px]:grid-cols-3">
        {card.left.map(({ key, int }) => <Row key={key} icon={LINES[key].icon} title={LINES[key].title} sub={LINES[key].sub}
          to={int ? { co, tab: "setari", sec: "integrari", int } : { co, tab: "setari", sec: "firme" }}
          at={int ? undefined : `${key === "bank" ? "conn" : "hist"}-${co}`} />)}
      </Panel>
    </section>
  );
}
