import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

// A static export: the engine serves the public app shell at / and /setup, with data under /web/.
// Only `next dev` reads app/fixtures. Every other phase (the production build) swaps src/lib/fixtures.ts for an empty
// stub, so no synthetic data ships, and a build asked for fixtures fails instead of quietly shipping them.
export default function config(phase: string): NextConfig {
  const dev = phase === PHASE_DEVELOPMENT_SERVER;
  if (!dev && process.env.NEXT_PUBLIC_FIXTURES) {
    throw new Error("NEXT_PUBLIC_FIXTURES is set: fixtures are for `next dev` only, a build never includes them.");
  }
  return {
    output: dev ? undefined : "export",
    experimental: { cpus: 2 },
    images: { unoptimized: true },
    poweredByHeader: false,
    devIndicators: false,
    allowedDevOrigins: process.env.FISCALM_DEV_ORIGIN ? [new URL(process.env.FISCALM_DEV_ORIGIN).hostname] : undefined,
    agentRules: false, // the repo keeps one AGENTS.md at its root
    turbopack: dev ? {} : { resolveAlias: { "./fixtures": "./src/lib/no-fixtures.ts" } },
  };
}
