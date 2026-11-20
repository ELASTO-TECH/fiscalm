// Any number of companies, from the engine's config: the switch, the colours, the first visit with no company in the
// URL, a single-company install and a company that is no longer configured.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { FirmMark } from "@/components/CompanyLogo";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { pal } from "@/lib/palette";
import type { AllState, Companies, State } from "@/lib/types";
import { at, fixture, fixtureAll, json, renderApp } from "./helpers";

const GAMA = { key: "GAMA", name: "Gama", legal_name: "GAMA SERVICII SRL", palette: "blue", late: 1, logo: null };

/** The fixture's companies plus Gama, a third one. */
/** A third firm, as the engine reports it: a mailed document with no firm on it may be for any firm, Gama too. */
function three(s: State): State {
  return { ...s, companies: [...s.companies, GAMA], to_check: s.to_check.map((m) => ({ ...m, companies: [...m.companies, GAMA.key] })) };
}

function only(s: State, cs: Companies): State {
  return { ...s, companies: cs };
}

const loaded = () => screen.findByRole("region", { name: "De plătit" });
const menu = () => document.querySelector(".top .comenu") as HTMLElement;

beforeEach(() => {
  localStorage.clear();
});

describe("companies from the config", () => {
  it("lists every company and Toate firmele, each with its own mark and colours", async () => {
    at("?co=NORD");
    renderApp({ state: () => json(three(fixture())) });
    await loaded();
    await waitFor(() => expect(document.documentElement.dataset.pal).toBe("amber")); // set by an effect
    await userEvent.click(within(menu()).getByLabelText(/^NORD, Atelier Nord SRL, alege firma/));
    const links = within(menu().querySelector(".menu") as HTMLElement).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["?co=NORD", "?co=STUDIO9", "?co=GAMA", "?co=ALL"]);
    const gama = links[2];
    expect(gama).toHaveTextContent("GGama");
    expect(gama).toHaveTextContent("O plată restantă");
    expect(gama.querySelector(".mk")).toHaveAttribute("data-pal", "blue"); // its colour is --co under that palette
    expect(links[3]).toHaveTextContent("3Toate firmeleNORD, STUDIO9 și Gama");
  });

  it("shows a firm's logo by its sha where it has one, its letters otherwise and when the logo fails", async () => {
    at("?co=GAMA");
    const sha = "b".repeat(64);
    const companies = [{ ...fixture().companies[0], logo: sha }, fixture().companies[1], { ...GAMA, logo: sha }];
    renderApp({ state: (url) => json({ ...three(fixture()), co: url.includes("co=NORD") ? "NORD" : "GAMA", companies }) });
    await loaded();
    const button = within(menu()).getByLabelText(/^Gama, GAMA SERVICII SRL, alege firma/);
    expect(button.querySelector(".mk img")).toHaveAttribute("src", `/web/invoicing/logo?co=GAMA&sha=${sha}`);
    expect(button.querySelector(".mk i")).not.toBeNull(); // the late-elsewhere dot stays on the logo
    await userEvent.click(button);
    const links = within(menu().querySelector(".menu") as HTMLElement).getAllByRole("link");
    expect(links.map((a) => a.querySelector(".mk")?.textContent)).toEqual(["", "S9", "", "3"]); // NORD has the same artwork
    fireEvent.error(links[2].querySelector("img") as HTMLImageElement);
    expect(links[2].querySelector(".mk")).toHaveTextContent(/^G$/);
    expect(links[2].querySelector(".mk")).toHaveAttribute("data-pal", "blue");
    fireEvent.error(button.querySelector("img") as HTMLImageElement); // the button's own mark, reused for the next firm
    await userEvent.click(links[0]);
    const nord = await screen.findByLabelText(/^NORD, Atelier Nord SRL, alege firma/);
    expect(nord.querySelector(".mk img")).toHaveAttribute("src", `/web/invoicing/logo?co=NORD&sha=${sha}`);
  });

  it("tries a logo again when it comes back after another one", () => {
    const [a, b] = ["a".repeat(64), "b".repeat(64)];
    const { container, rerender } = render(<FirmMark co="GAMA" logo={a} letters="G" pal="blue" className="" />);
    fireEvent.error(container.querySelector("img") as HTMLImageElement);
    expect(container.querySelector("img")).toBeNull();
    rerender(<FirmMark co="GAMA" logo={b} letters="G" pal="blue" className="" />);
    rerender(<FirmMark co="GAMA" logo={a} letters="G" pal="blue" className="" />);
    expect(container.querySelector("img")).toHaveAttribute("src", `/web/invoicing/logo?co=GAMA&sha=${a}`);
  });

  it("takes a company's colours and name when it is chosen", async () => {
    at("?co=GAMA");
    const s = { ...three(fixture()), co: "GAMA" };
    renderApp({ state: () => json(s) });
    await loaded();
    await waitFor(() => expect(document.documentElement.dataset.pal).toBe("blue")); // set by an effect
    expect(document.title).toBe("Acasă, fiscalm, Gama");
    expect(localStorage.getItem("fiscalm:pal")).toBe("blue");
    expect(within(menu()).getByLabelText(/^Gama, GAMA SERVICII SRL, alege firma, plăți restante la NORD și STUDIO9$/)).toBeInTheDocument();
  });

  it("offers one button per company for a mailed document in doubt, with the company's name", async () => {
    at("?co=NORD&tab=rapoarte&month=2026-10");
    renderApp({ state: () => json(three(fixture())) });
    const answers = (await screen.findByRole("button", { name: "Atribuie NORD" })).parentElement!;
    expect(within(answers).getAllByRole("button").map((b) => b.textContent))
      .toEqual(["Atribuie NORD", "Atribuie STUDIO9", "Atribuie Gama", "Nu este factură"]);
  });
});

describe("a single company", () => {
  it("has no Toate firmele, and a link to it opens the company", async () => {
    at("?co=ALL");
    const s = only(fixture(), fixture().companies.slice(0, 1));
    const all: AllState = { ...fixtureAll(), companies: s.companies, parts: [s] };
    const api = renderApp({ state: () => json(s), all: () => json(all) });
    await loaded();
    await waitFor(() => expect(window.location.search).toBe("?co=NORD"));
    await waitFor(() => expect(api.gets("NORD").length).toBeGreaterThan(0)); // the read may follow the URL
    await userEvent.click(await within(menu()).findByLabelText(/^NORD, Atelier Nord SRL, alege firma/)); // after the switch renders
    expect(within(menu()).queryByRole("link", { name: /Toate firmele/ })).toBeNull();
  });
});

describe("the first visit", () => {
  it("with no company in the URL or remembered, reads all of them and shows them on Acasă", async () => {
    at("");
    const api = renderApp();
    await loaded();
    expect(api.gets("ALL")).toHaveLength(1);
    await waitFor(() => expect(window.location.search).toBe("?co=ALL"));
  });

  it("on a tab that shows one company, opens the first company in the config", async () => {
    at("?tab=plati");
    const api = renderApp();
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=plati"));
    expect(api.gets("ALL")).toHaveLength(1);
    await waitFor(() => expect(api.gets("NORD").length).toBeGreaterThan(0));
  });

  it.each([400, 404])("forgets a company the server no longer has (%s) and starts again from all of them", async (status) => {
    localStorage.setItem("fiscalm:co", "OLD");
    at("?co=OLD&tab=plati");
    const api = renderApp({ state: (url) => (url.includes("co=OLD") ? json({ ok: false, message: "Cererea nu a putut fi procesată." }, status) : json(fixture())) });
    await waitFor(() => expect(window.location.search).toBe("?co=NORD&tab=plati"));
    expect(localStorage.getItem("fiscalm:co")).toBe("NORD");
    expect(api.gets("ALL")).toHaveLength(1);
    expect(screen.queryByRole("alert")).toBeNull(); // not an error for the user
  });

  it("refuses a company key that could not be one, without asking the server", async () => {
    at("?co=..%2Fetc&tab=home");
    const api = renderApp();
    await loaded();
    expect(api.calls.some((c) => c.url.includes("etc"))).toBe(false);
  });
});

describe("palettes", () => {
  it("falls back to amber for an unknown or inherited name", () => {
    expect(pal("blue")).toBe("blue");
    for (const bad of ["constructor", "__proto__", "toString", "", null, undefined, "pink"]) expect(pal(bad)).toBe("amber");
  });
});
