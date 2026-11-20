// After `next build`: the public receipt page (out/u.html) must make no request beyond its assets and the link's own
// routes, and the owner app (out/index.html) keeps what it needs. A relative manifest on u.html would carry the link's
// secret in its path.
import { readFileSync } from "node:fs";

const read = (f) => readFileSync(new URL(`../out/${f}`, import.meta.url), "utf8");
const u = read("u.html");
const app = read("index.html");
const problems = [
  [/rel="manifest"/.test(u), "u.html links a manifest"],
  [u.includes("fiscalm:pal"), "u.html runs the app's palette script"],
  [!/name="referrer" content="no-referrer"/.test(u), "u.html lacks the no-referrer meta"],
  [!/rel="manifest"/.test(app), "index.html lost its manifest"],
  [!app.includes("fiscalm:pal"), "index.html lost its palette script"],
].filter(([bad]) => bad).map(([, what]) => what);
if (problems.length) {
  console.error(`Public page check failed:\n${problems.join("\n")}`);
  process.exit(1);
}
console.log("Public page: no manifest, no app script, no referrer; the app keeps both.");
