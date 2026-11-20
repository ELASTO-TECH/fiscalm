// The one plural rule (copy decision 2026-11-02): 1 -> the singular with its article, 2-19 -> the plural,
// 20 and up -> "de"; a number ending in 01-19 past 100 takes no "de".
import { describe, expect, it } from "vitest";
import { monthLabel, receivedState } from "@/components/derive";
import type { Invoice, Report } from "@/lib/types";
import { andList, cents, fromCents, lei, dueText, nOf, sumParts, sums } from "@/lib/format";

it.each([["90071992547409.93", "90.071.992.547.409,93"], ["-0.01", "-0,01"], ["6114.2", "6.114,20"]])("keeps decimal money exact: %s", (amount, displayed) => {
  expect(lei(amount)).toBe(displayed);
  expect(lei(fromCents(cents(amount)))).toBe(displayed);
});

describe("nOf", () => {
  it("says one as the singular given, then counts", () => {
    expect(nOf(1, "o plată restantă", "plăți restante")).toBe("o plată restantă");
    expect(nOf(1, "fișierul", "fișiere")).toBe("fișierul");
    expect(nOf(0, "o plată", "plăți")).toBe("0 plăți");
    expect(nOf(2, "o plată", "plăți")).toBe("2 plăți");
    expect(nOf(19, "o plată", "plăți")).toBe("19 plăți");
  });

  it("puts de from 20 on, except after 101-119 of each hundred", () => {
    expect(nOf(20, "o plată", "plăți")).toBe("20 de plăți");
    expect(nOf(99, "o plată", "plăți")).toBe("99 de plăți");
    expect(nOf(100, "o plată", "plăți")).toBe("100 de plăți");
    expect(nOf(101, "o plată", "plăți")).toBe("101 plăți");
    expect(nOf(119, "o plată", "plăți")).toBe("119 plăți");
    expect(nOf(120, "o plată", "plăți")).toBe("120 de plăți");
    expect(nOf(1000, "o plată", "plăți")).toBe("1000 de plăți");
  });
});

describe("dueText", () => {
  it("counts the days late with the same rule", () => {
    expect(dueText("2026-10-23", "2026-10-24")).toEqual(["late", "Restantă de o zi"]);
    expect(dueText("2026-10-21", "2026-10-24")).toEqual(["late", "Restantă de 3 zile"]);
    expect(dueText("2026-10-04", "2026-10-24")).toEqual(["late", "Restantă de 20 de zile"]);
  });
});

it("lists firms and amounts as Romanian says them", () => {
  expect(["", "NORD", "NORD și STUDIO9", "NORD, STUDIO9 și SUD"]).toEqual([[], ["NORD"], ["NORD", "STUDIO9"], ["NORD", "STUDIO9", "SUD"]].map(andList));
  expect(sums({ total: "10.00", other: [{ currency: "EUR", total: "2.00" }, { currency: "USD", total: "3.00" }] })).toBe("10,00 lei, 2,00 EUR și 3,00 USD");
  expect(sumParts({ total: "0.00", other: [{ currency: "EUR", total: "2.00" }, { currency: "USD", total: "2.00" }] })).toEqual(["2,00 EUR", "2,00 USD"]);
});

describe("red only for money overdue", () => {
  it.each(["held", "in_file", "lost"] as const)("an overdue invoice %s stays red: the engine counts it unpaid", (payment_state) => {
    expect(receivedState({ status: "linked", overdue: true, payment_state } as Invoice, "2026-11-01")[0]).toBe("late");
  });

  it("a month with questions left is a step to take, not a failure", () => {
    expect(monthLabel({ status: "preparing", counts: { open: 2 } } as Report)).toEqual(["soon", "2 de rezolvat"]);
  });
});
