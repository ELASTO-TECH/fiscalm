import { PHASE_DEVELOPMENT_SERVER, PHASE_PRODUCTION_BUILD } from "next/constants";
import { describe, expect, it, vi } from "vitest";
import config from "../next.config";

describe("fixtures stay out of production", () => {
  it("fails a production build asked for fixtures", () => {
    vi.stubEnv("NEXT_PUBLIC_FIXTURES", "1");
    expect(() => config(PHASE_PRODUCTION_BUILD)).toThrow(/NEXT_PUBLIC_FIXTURES/);
  });

  it("swaps the fixtures module for the stub in a production build, not in development", () => {
    vi.stubEnv("NEXT_PUBLIC_FIXTURES", "");
    expect(config(PHASE_PRODUCTION_BUILD).turbopack).toEqual({ resolveAlias: { "./fixtures": "./src/lib/no-fixtures.ts" } });
    expect(config(PHASE_DEVELOPMENT_SERVER).turbopack).toEqual({});
  });

  it("reads fixtures only under next dev, whatever NEXT_PUBLIC_FIXTURES says", async () => {
    vi.stubEnv("NEXT_PUBLIC_FIXTURES", "1");
    vi.stubEnv("NODE_ENV", "production");
    vi.resetModules();
    expect((await import("@/lib/api")).FIXTURES).toBe(false);
    vi.stubEnv("NODE_ENV", "development");
    vi.resetModules();
    expect((await import("@/lib/api")).FIXTURES).toBe(true);
  });

  it("the stub has the fixtures module's exports and no data", async () => {
    const [real, stub] = await Promise.all([import("@/lib/fixtures"), import("@/lib/no-fixtures")]);
    expect(Object.keys(stub).sort()).toEqual(Object.keys(real).sort());
    await expect(stub.fixtureState()).rejects.toThrow();
    expect(stub.FIXTURE_PAGE).toBe("");
  });
});
