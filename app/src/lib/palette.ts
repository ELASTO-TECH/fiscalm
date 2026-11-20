// The colour schemes a company can have (the engine's companies.PALETTES). The full palettes are CSS, :root[data-pal=...]
// in globals.css; these are the few colours script needs: the status bar, a company's mark and the mark of all of them.
export const PALETTES = {
  amber: { theme: "#1f1a14", acc: "#f47427", on: "#14110c" },
  green: { theme: "#04140d", acc: "#1fc486", on: "#04140d" },
  blue: { theme: "#0a1424", acc: "#4c8ff7", on: "#06101f" },
  violet: { theme: "#160f24", acc: "#9d74f8", on: "#120a1f" },
  rose: { theme: "#22100f", acc: "#f2646f", on: "#1d0a0c" },
  slate: { theme: "#13171c", acc: "#9fb0c4", on: "#0d1116" },
} as const;
export type Palette = keyof typeof PALETTES;

export const pal = (p: string | undefined | null): Palette =>
  p && Object.prototype.hasOwnProperty.call(PALETTES, p) ? (p as Palette) : "amber";
