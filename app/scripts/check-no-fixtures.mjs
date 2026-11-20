// After `next build`: fail if anything from app/fixtures (generated just before by `npm run fixtures`) or the fixtures
// module made it into out/.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const out = join(root, "out");

const markers = new Set(["Fixture: ", "nimic nu s-a schimbat", "<rect x='40' y='40'"]);
for (const f of ["state-NORD.json", "state-STUDIO9.json"]) {
  const s = JSON.parse(readFileSync(join(root, "fixtures", f), "utf8"));
  for (const r of s.invoices) markers.add(r.number).add(r.supplier);
  for (const r of s.issued) markers.add(r.number).add(r.client);
  for (const d of s.documents) markers.add(d.vendor);
  for (const m of s.to_check) markers.add(m.subject).add(m.sender);
}
for (const l of Object.values(JSON.parse(readFileSync(join(root, "fixtures", "lines.json"), "utf8")))) for (const r of l.lines) markers.add(r.description);
markers.delete("");
for (const m of markers) if (m.length < 5) markers.delete(m); // "Figma" stays; a two-letter number would match code

const files = (d) => readdirSync(d).flatMap((n) => (statSync(join(d, n)).isDirectory() ? files(join(d, n)) : [join(d, n)]));
const hits = [];
for (const f of files(out).filter((f) => /\.(js|html|txt|json)$/.test(f))) {
  const text = readFileSync(f, "utf8");
  for (const m of markers) if (text.includes(m)) hits.push(`${f.slice(root.length)}: ${JSON.stringify(m)}`);
}
if (hits.length) {
  console.error(`Fixture data in the production build:\n${hits.join("\n")}`);
  process.exit(1);
}
console.log(`No fixture data in out/ (${markers.size} markers checked).`);
