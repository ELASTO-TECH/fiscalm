// Navigation v2 routes (docs/information-architecture.md, section 3): every section has its own tab in the URL, links
// saved before it land on the same page with the rest of their query, and a page that needs one company names it.
import { act, screen, waitFor } from "@testing-library/react";
import { renderSignedIn as render } from "./helpers";
import { beforeEach, describe, expect, it } from "vitest";
import App from "@/components/App";
import { NO_SHEET, SHEETS, href, openSheet, parse } from "@/lib/nav";
import { at, fixture, json, server, renderApp } from "./helpers";

beforeEach(() => {
  localStorage.clear();
  at("?co=NORD");
});

/** Each tab and the title its page shows. */
const PAGES: [string, string][] = [
  ["vanzari", "Vânzări"], ["cheltuieli", "Cheltuieli"], ["plati", "Fișiere de plăți"], ["tranzactii", "Tranzacții"], ["parteneri", "Parteneri"],
  ["rapoarte", "Rapoarte"], ["setari", "Setări"],
];

/** Tab names from before navigation v2: no alias, they open Acasă like any unknown tab. */
const OLD = ["facturi", "sistem", "btgo", "conta", "documente", "extrase", "banca"];

function open(query: string) {
  act(() => {
    at(`?co=NORD&${query}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

describe("routes", () => {
  it("gives every section its own tab, and links to it as it is", () => {
    for (const [tab] of PAGES) {
      expect(parse(`?co=NORD&tab=${tab}`).tab).toBe(tab);
      expect(href({ co: "NORD", tab: tab as never, f: "late" })).toBe(`?co=NORD&tab=${tab}&f=late`);
    }
    expect(href(parse("?co=NORD&tab=cheltuieli&side=bonuri&doc=d1"))).toBe("?co=NORD&tab=cheltuieli&side=bonuri&doc=d1");
    expect(parse("?co=NORD&tab=vanzari&side=bonuri").side).toBe(""); // a side only Cheltuieli has
    expect(parse("?co=NORD&tab=nope").tab).toBe("home");
    expect(parse("?co=NORD&tab=constructor").tab).toBe("home");
  });

  // One list says which route fields are sheets: every field NO_SHEET clears but `for` (a new invoice's partner) opens one.
  it.each(Object.keys(NO_SHEET).filter((k) => k !== "for"))("knows the %s sheet is open", (k) => {
    expect(SHEETS).toContain(k);
    expect(openSheet({ ...parse("?co=NORD"), [k]: "1" })).toBe(k);
  });

  it("has no aliases for the old tab names", () => {
    for (const old of OLD) expect(parse(`?co=NORD&tab=${old}`).tab).toBe("home");
    expect(parse("?co=NORD&tab=facturi&side=emise").tab).toBe("home");
  });

  it("gives Tranzacții its own filters, Intrări and Ieșiri for a month among them, and Bancă no longer", () => {
    for (const f of ["in", "out", "all"]) {
      expect(href(parse(`?co=NORD&tab=tranzactii&f=${f}&month=2026-10&iss=5004`))).toBe(`?co=NORD&tab=tranzactii&iss=5004&month=2026-10&f=${f}`);
    }
    expect(parse("?co=NORD&tab=tranzactii&f=late").f).toBe("");
    expect(parse("?co=NORD&tab=cheltuieli&f=in").f).toBe(""); // only Tranzacții has Intrări and Ieșiri
    expect(parse("?co=NORD&tab=banca").tab).toBe("home");
    expect(parse("?co=NORD&tab=extrase").tab).toBe("home");
  });

  it("keeps Setări's pages and integration sheet as Sistem had them", () => {
    for (const sec of ["", "firme", "contabil", "integrari", "acces", "utilizatori", "cont"]) expect(parse(`?co=NORD&tab=setari&sec=${sec}`).sec).toBe(sec);
    expect(href(parse("?co=NORD&tab=setari&sec=utilizatori&usr=u_example"))).toBe("?co=NORD&tab=setari&sec=utilizatori&usr=u_example");
    expect(parse("?co=NORD&tab=plati&usr=u_example").usr).toBe("");
    expect(parse("?co=NORD&tab=setari&sec=nope").sec).toBe("");
    expect(parse("?co=NORD&tab=plati&sec=acces").sec).toBe(""); // only Setări has pages
    expect(parse("?co=NORD&tab=setari&sec=integrari&int=new:ai").int).toBe("new:ai");
    expect(parse("?co=NORD&tab=plati&int=ai-00000001").int).toBe(""); // only Setări has the integration sheet
  });

  it("shows every company at once only on Acasă, Vânzări, Cheltuieli and Parteneri, and keeps it through Mai mult", () => {
    localStorage.setItem("fiscalm:co", "STUDIO9");
    for (const tab of ["home", "vanzari", "cheltuieli", "parteneri", "mai-mult"]) expect(parse(`?co=ALL&tab=${tab}`).co).toBe("ALL");
    for (const tab of ["plati", "tranzactii", "rapoarte", "setari"]) expect(parse(`?co=ALL&tab=${tab}`).co).toBe("STUDIO9");
  });

  it("names the company in the URL when a page that needs one is opened under Toate firmele, keeping its anchor", async () => {
    localStorage.setItem("fiscalm:co", "STUDIO9");
    at("?co=ALL&tab=plati#fisier");
    renderApp();
    await waitFor(() => expect(window.location.search).toBe("?co=STUDIO9&tab=plati"));
    expect(window.location.hash).toBe("#fisier");
    expect(await screen.findByText("Fișiere de plăți", { selector: ".ttl" })).toBeInTheDocument();
  });

  it("keeps the anchor whichever way the company is chosen: none remembered, or one that is gone", async () => {
    for (const [remembered, query] of [["", "tab=plati#fisier"], ["GONE", "tab=rapoarte#pachet"], ["", "tab=rapoarte#rezolvat"]]) {
      localStorage.clear();
      if (remembered) localStorage.setItem("fiscalm:co", remembered);
      at(`?co=ALL&${query}`);
      server({ state: (url) => (url.includes("GONE") ? json({ ok: false, message: "" }, 400) : json(fixture())) });
      const { unmount } = render(<App />);
      const [tab, anchor] = query.split("#");
      await waitFor(() => expect(window.location.search).toBe(`?co=NORD&${tab}`));
      expect(window.location.hash).toBe(`#${anchor}`);
      unmount();
    }
  });

  it("shows each section's page, from its own link and from an old one", async () => {
    renderApp();
    await screen.findByRole("region", { name: "De plătit" });
    for (const [tab, title] of PAGES) {
      open(`tab=${tab}`);
      expect(await screen.findByText(title, { selector: ".ttl" })).toBeInTheDocument();
    }
  });
});
