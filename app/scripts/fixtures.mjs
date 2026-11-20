// Writes app/fixtures/*.json from tests/synthetic_world.py (synthetic data around a fixed day; never committed).
// Uses only this checkout's engine venv.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const repo = fileURLToPath(new URL("../../", import.meta.url));
// The app consumes only this bounded projection of the engine's API contract.
const contract = JSON.parse(readFileSync(new URL("../../docs/openapi.json", import.meta.url), "utf8"));
writeFileSync(new URL("../src/lib/action-effects.json", import.meta.url),
  JSON.stringify(contract["x-app-action-effects"], null, 2) + "\n");
writeFileSync(new URL("../src/lib/action-roles.json", import.meta.url),
  JSON.stringify(contract["x-app-action-roles"], null, 2) + "\n");
writeFileSync(new URL("../src/lib/document-currencies.json", import.meta.url),
  JSON.stringify(contract["x-app-document-currencies"], null, 2) + "\n");
const python = `${repo}.venv/bin/python`;
if (!existsSync(python)) {
  console.error("No fiscalm venv (../.venv): run `uv sync` at the repo root.");
  process.exit(1);
}
execFileSync(python, [`${repo}tests/synthetic_world.py`, `${repo}app/fixtures`], {
  stdio: "inherit",
  env: { ...process.env, PYTHONPATH: `${repo}src` },
});
