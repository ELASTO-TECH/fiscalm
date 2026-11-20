import { expectTypeOf, it } from "vitest";
import type { Invoicing, Recurrence } from "../src/lib/types";

it("matches the recurrence read contract", () => {
  expectTypeOf<Invoicing["recurrences"]>().toEqualTypeOf<Recurrence[]>();
  expectTypeOf<Recurrence["template"]["notes"]>().toEqualTypeOf<string | undefined>();
  expectTypeOf<Extract<keyof Recurrence["template"], "exchange_rate" | "exchange_date">>().toEqualTypeOf<never>();
});
