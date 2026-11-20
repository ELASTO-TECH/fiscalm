// Romanian formatting: lei, dates, plurals, due texts.

const LUNI = ["ian", "feb", "mar", "apr", "mai", "iun", "iul", "aug", "sep", "oct", "nov", "dec"];
const LUNI_LUNG = ["ianuarie", "februarie", "martie", "aprilie", "mai", "iunie", "iulie", "august", "septembrie",
  "octombrie", "noiembrie", "decembrie"];

/** An integer count in Romanian, without decimal places. */
export function integer(n: number): string {
  return n.toLocaleString("ro-RO", { maximumFractionDigits: 0 });
}

/** 6114.2 -> "6.114,20" */
export function lei(a: string | number): string {
  const exact = typeof a === "string" ? a.trim().match(/^([+-]?)(\d+)(?:\.(\d{1,2}))?$/) : null;
  if (exact) {
    const whole = exact[2].replace(/^0+(?=\d)/, ""), bani = (exact[3] ?? "").padEnd(2, "0");
    const sign = exact[1] === "-" && BigInt(whole + bani) !== 0n ? "-" : "";
    return sign + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ".") + "," + bani;
  }
  const n = typeof a === "number" ? a : Number(a);
  if (!Number.isFinite(n)) return String(a);
  const [whole, dec] = Math.abs(n).toFixed(2).split(".");
  return (n < 0 ? "-" : "") + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ".") + "," + dec;
}

/** An exact decimal string in Romanian, never rounded: "2.5" -> "2,5", "1250" -> "1.250", ("12.4", 2) -> "12,40". */
export function decimal(a: string, places = 0): string {
  const exact = a.trim().match(/^([+-]?)(\d+)(?:\.(\d+))?$/);
  if (!exact) return a;
  const fraction = (exact[3] ?? "").replace(/0+$/, "").padEnd(places, "0");
  const whole = exact[2].replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return (exact[1] === "-" ? "-" : "") + whole + (fraction ? "," + fraction : "");
}

/** Exact minor units for API decimal strings, including signed fractional amounts. */
export function cents(a: string): bigint {
  const [whole, fraction = ""] = a.replace(/^[+-]/, "").split(".");
  const amount = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
  return a.startsWith("-") ? -amount : amount;
}

export function fromCents(a: bigint): string {
  const digits = (a < 0n ? -a : a).toString().padStart(3, "0");
  return `${a < 0n ? "-" : ""}${digits.slice(0, -2)}.${digits.slice(-2)}`;
}

/** "2026-10-24" -> "24 oct" (with the year when it is not this year's) */
export function dm(iso: string | null | undefined, today: string): string {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${LUNI[m - 1]}` + (String(y) !== today.slice(0, 4) ? ` ${y}` : "");
}

/** A bon or invoice by its shop, or "Bon din 6 oct" while the shop is unknown: never by its file. */
export function docName(d: { vendor: string; date: string | null }, today: string): string {
  return d.vendor || (d.date ? `Bon din ${dm(d.date, today)}` : "Bon");
}

/** A bon's amount with its currency, or "Sumă necunoscută" before it is read: an unknown amount is never 0,00. */
export function docAmount(d: { amount: string | null; currency: string | null }): string {
  return d.amount === null ? UNKNOWN_AMOUNT : `${lei(d.amount)} ${curOf(d.currency)}`;
}
export const UNKNOWN_AMOUNT = "Sumă necunoscută";

/** "2026-10-26T14:30:11" -> "26 oct, 14:30" */
export function dmt(s: string | null | undefined, today: string): string {
  if (!s) return "niciodată";
  const t = s.replace("T", " ");
  return t.length >= 16 ? `${dm(t.slice(0, 10), today)}, ${t.slice(11, 16)}` : dm(t.slice(0, 10), today);
}

const RO_DAY = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Bucharest", year: "numeric", month: "2-digit", day: "2-digit" });

/** The calendar day in Romania of a UTC timestamp ("2026-10-26T23:30:00Z" -> "2026-10-27"). */
export function roDay(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso.slice(0, 10) : RO_DAY.format(d);
}

/** Parts of a line, one phrase: ["Plătită", "9 sep"] -> "Plătită, 9 sep" ("Restantă" goes lower case after the first). */
export function phrase(...xs: (string | false | null | undefined)[]): string {
  return xs.filter((x): x is string => !!x)
    .map((x, i) => (i && /^\p{Lu}\p{Ll}/u.test(x) ? x[0].toLowerCase() + x.slice(1) : x)).join(", ");
}

/** A count in words, the one plural rule of the app: 1 -> `one` as given, with its article ("o plată restantă",
 * "fișierul"); 2 to 19 -> "3 plăți"; 20 and up -> "20 de plăți". As in DOOM, a number ending in 01 to 19 past 100
 * takes no "de" ("101 plăți", "120 de plăți"). */
export function nOf(n: number, one: string, few: string): string {
  if (n === 1) return one;
  const tail = n % 100;
  return n >= 20 && (tail === 0 || tail >= 20) ? `${n} de ${few}` : `${n} ${few}`;
}

function dayNum(iso: string): number {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return Date.UTC(y, m - 1, d) / 86400000;
}

/** The ISO date `n` days after `iso`. */
export function daysAfter(iso: string, n: number): string {
  return new Date((dayNum(iso) + n) * 86400000).toISOString().slice(0, 10);
}

export function daysTo(iso: string | null | undefined, today: string): number | null {
  return iso ? dayNum(iso) - dayNum(today) : null;
}

export type Tone = "" | "late" | "soon" | "ok" | "info";

/** [css class, text] for a due date. */
export function dueText(iso: string | null | undefined, today: string): [Tone, string] {
  const n = daysTo(iso, today);
  if (n === null || !iso) return ["", "Fără scadență"];
  if (n < 0) return ["late", `Restantă de ${nOf(-n, "o zi", "zile")}`];
  if (n === 0) return ["soon", "Scadentă azi"];
  if (n === 1) return ["soon", "Scadentă mâine"];
  if (n <= 3) return ["soon", `Scadentă în ${n} zile`];
  return ["", `Scadentă pe ${dayMonth(iso)}`];
}

/** "2026-11-10" -> "10 noiembrie" */
export function dayMonth(iso: string): string {
  const [, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${d} ${LUNI_LUNG[m - 1]}`;
}

const ZILE = ["Duminică", "Luni", "Marți", "Miercuri", "Joi", "Vineri", "Sâmbătă"];

/** "2026-10-27" -> "Marți, 27 octombrie": the day under Acasă's title. */
export function weekday(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${ZILE[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]}, ${dayMonth(iso)}`;
}

/** The calendar month before an ISO day, including the January year boundary. */
export function previousMonth(today: string): string {
  const [year, month] = today.split("-").map(Number);
  return new Date(Date.UTC(year, month - 2, 1)).toISOString().slice(0, 7);
}

/** "2026-09" -> "septembrie 2026" */
export function luna(month: string): string {
  const [y, m] = month.split("-");
  return `${LUNI_LUNG[Number(m) - 1]} ${y}`;
}

/** "2026-09" -> "septembrie" */
export function lunaName(month: string): string {
  return luna(month).split(" ")[0];
}

export const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** A list as Romanian says it: "A", "A și B", "A, B și C". */
export function andList(items: readonly string[]): string {
  return items.length > 1 ? `${items.slice(0, -1).join(", ")} și ${items[items.length - 1]}` : items.join("");
}

/** Amounts per currency as one phrase, never added up: "4.200,00 lei și 600,00 EUR"; lei left out when zero beside
 * another currency ("900,00 EUR"), "0,00 lei" when there is nothing. */
export function sums(s: Sums): string {
  return andList(sumParts(s));
}
export type Sums = { total: string; other: { currency: string; total: string }[] };
/** The amounts `sums` lists, one per currency. */
export function sumParts(s: Sums): string[] {
  const other = s.other.map((o) => `${lei(o.total)} ${o.currency}`);
  return [...(Number(s.total) || !other.length ? [`${lei(s.total)} lei`] : []), ...other];
}

/** A sum that is not zero, either way: turnover with a credit note can be negative. */
export const nonzero = (s: { total: string; other: unknown[] }) => Number(s.total) !== 0 || s.other.length > 0;

/** A UN/ECE unit code's Romanian name (the units the app offers); an unknown code stays as it is. */
export const UNITS: Record<string, string> = { H87: "Bucată", KGM: "Kilogram", LTR: "Litru", MTR: "Metru", HUR: "Oră", SET: "Set", XPK: "Pachet", XBX: "Cutie" };
/** A unit code's name, or undefined for a code the app has no name for: own keys only, since a code comes from a document
 * ("toString" names no unit). */
export const unitName = (code: string): string | undefined => (Object.hasOwn(UNITS, code) ? UNITS[code] : undefined);

export const curOf = (c: string | null | undefined) => (!c || c === "RON" ? "lei" : c);
export const isRon = (c: string | null | undefined) => !c || c === "RON";
