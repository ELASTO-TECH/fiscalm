// Firma › Produse against a fake engine: the firm's catalog, its search and archive, the empty catalog's first step,
// and each product action's exact body.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fixtureProduct, fixtureProducts } from "@/lib/fixtures";
import type { Product } from "@/lib/types";
import { beforeEach, describe, expect, it } from "vitest";
import { answer, at, deferred, fixture, json, renderApp, type Call } from "./helpers";

beforeEach(() => localStorage.clear());

const names = () => [...document.querySelectorAll("main .card .row b")].map((b) => b.textContent);
/** The engine's GET api/products: one product by id, or a page of the active or archived ones matching q. */
async function catalog(url: string, rows?: Product[]) {
  const q = Object.fromEntries(new URL(url, "http://local").searchParams);
  if (q.id) return rows ? json({ product: rows.find((p) => p.id === q.id) }) : json({ product: await fixtureProduct("NORD", q.id) });
  const page = rows ? { data: rows, total: rows.length, next_cursor: null } : await fixtureProducts("NORD", q);
  return json({ company: "NORD", products: page.data, total: page.total, next_cursor: page.next_cursor });
}
const open = (rows?: Product[], onPost: (c: Call) => Response | Promise<Response> = () => json({ ok: true, message: "Gata.", state: fixture() })) => {
  at("?co=NORD&tab=produse");
  return renderApp({ products: (url) => catalog(url, rows), onPost });
};

describe("Produse", () => {
  it("lists the firm's active products, asks the engine for a name or code, and keeps the archived apart", async () => {
    const api = open();
    await waitFor(() => expect(names()).toEqual(["Consultanță fiscală", "Cutie de carton 40x30", "Abonament mentenanță", "Carte tipărită"]));
    await userEvent.type(screen.getByRole("searchbox", { name: "Caută produs sau cod" }), "crt-7");
    await waitFor(() => expect(names()).toEqual(["Carte tipărită"]));
    expect(api.calls.at(-1)?.url).toBe("/web/api/products?co=NORD&q=crt-7&f=&cursor=&limit=50");
    await userEvent.click(screen.getByRole("link", { name: "Arhivate" }));
    await userEvent.clear(screen.getByRole("searchbox", { name: "Caută produs sau cod" }));
    await waitFor(() => expect(names()).toEqual(["Cutie de carton 60x40"]));
  });

  it("an empty catalog invites the first product, whose sheet adds it and holds its fields while it is saved", async () => {
    const held = deferred<Response>();
    const api = open([], () => held.promise);
    await userEvent.click(await screen.findByRole("link", { name: "Adaugă primul produs" }));
    const d = within(await screen.findByRole("dialog", { name: "Produs nou" }));
    await userEvent.click(d.getByRole("button", { name: "Adaugă produsul" }));
    expect(d.getByLabelText("Denumire")).toHaveFocus(); // nothing sent without a name and a price
    expect(api.posts()).toHaveLength(0);
    await userEvent.type(d.getByLabelText("Denumire"), "Curs de contabilitate");
    await userEvent.type(d.getByLabelText("Preț unitar"), "99,90");
    await userEvent.click(d.getByRole("button", { name: "Adaugă produsul" }));
    await waitFor(() => expect(api.posts()).toEqual([{ url: "/web/api/product-add", method: "POST", body: { co: "NORD",
      kind: "produs", name: "Curs de contabilitate", code: "", unit: "H87", unit_price: "99.90", currency: "RON", vat_category: "S", vat_rate: "21",
      exemption: "", description: "" } }]));
    expect(d.getByLabelText("Preț unitar")).toBeDisabled(); // what was sent is what gets saved
    await act(async () => held.resolve(json({ ok: true, message: "Produsul a fost salvat.", state: fixture() })));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("a product's sheet saves its changes and archives it; deleting is offered only while no invoice uses it", async () => {
    const api = open();
    await userEvent.click(await screen.findByRole("link", { name: /Consultanță fiscală/ }));
    const d = within(await screen.findByRole("dialog", { name: "Consultanță fiscală" }));
    expect(d.queryByRole("button", { name: "Șterge" })).toBeNull(); // used on an invoice
    await userEvent.clear(d.getByLabelText("Preț unitar"));
    await userEvent.type(d.getByLabelText("Preț unitar"), "380");
    await userEvent.click(d.getByRole("button", { name: "Salvează" }));
    await waitFor(() => expect(api.posts()[0]).toEqual({ url: "/web/api/product-update", method: "POST", body: { co: "NORD", id: "1", version: 1,
      kind: "serviciu", name: "Consultanță fiscală", code: "CONS-01", unit: "HUR", unit_price: "380.00", currency: "RON", vat_category: "S", vat_rate: "21",
      exemption: "", description: "" } }));
    await userEvent.click(await screen.findByRole("link", { name: /Carte tipărită/ }));
    const unused = within(await screen.findByRole("dialog", { name: "Carte tipărită" }));
    await userEvent.click(unused.getByRole("button", { name: "Arhivează" }));
    await waitFor(() => expect(api.posts()[1]).toEqual({ url: "/web/api/product-archive", method: "POST", body: { co: "NORD", id: "4", version: 1, archived: true } }));
    await userEvent.click(await screen.findByRole("link", { name: /Carte tipărită/ }));
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Carte tipărită" })).getByRole("button", { name: "Șterge" }));
    expect(await answer()).toBe("Ștergi produsul Carte tipărită?");
    await waitFor(() => expect(api.posts()[2]).toEqual({ url: "/web/api/product-delete", method: "POST", body: { co: "NORD", id: "4", version: 1 } }));
  });
});

it("a product's link that fails to read offers another read, and only an unknown id says the product is gone", async () => {
  let fail = true;
  at("?co=NORD&tab=produse&prod=77");
  renderApp({ products: (url) => url.includes("id=77") ? (fail ? json({ ok: false }, 500) : json({ ok: false }, 404)) : catalog(url, []) });
  const d = within(await screen.findByRole("dialog"));
  const again = await d.findByRole("button", { name: "Reîncearcă" });
  fail = false;
  await userEvent.click(again);
  expect(await screen.findByText("Produsul nu mai există în catalogul firmei.")).toBeInTheDocument();
});
