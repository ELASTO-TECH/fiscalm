import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "../globals.css";

// The public receipt page's own root: no manifest, no app icons, no palette script. Its URL holds a link's secret, so it
// makes no request beyond its assets and the link's two routes (a relative manifest would carry the secret in its path).
export const metadata: Metadata = {
  title: "Bondrop", // the page names the firm once it knows it (Upload)
  robots: { index: false, follow: false },
  referrer: "no-referrer",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, viewportFit: "cover" };

export default function PublicLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ro">
      <body>{children}</body>
    </html>
  );
}
