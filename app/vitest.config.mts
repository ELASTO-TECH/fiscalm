import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { url: "https://localhost:3000" } },
    setupFiles: ["./test/setup.ts"],
    include: ["test/**/*.test.{ts,tsx}"],
    // above the 4 s that findBy/waitFor wait (test/setup.ts), so a slow query fails with its own message, not a timeout
    testTimeout: 15000,
  },
});
