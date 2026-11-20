import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { PALETTES } from "@/lib/palette";
import { PRODUCT } from "@/lib/product";
import "../globals.css";

export const metadata: Metadata = {
  title: PRODUCT,
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

// Before the first paint: the colours of the last company shown (App.tsx keeps its palette in localStorage). The page is
// public; the manifest contains no account or session credential.
const KEYS = JSON.stringify(Object.keys(PALETTES));
const BOOT = `(function(){try{var p=localStorage.getItem('fiscalm:pal');if(${KEYS}.indexOf(p)<0)p='amber';
document.documentElement.dataset.pal=p}catch(e){}})()`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ro" data-pal="amber" suppressHydrationWarning>
      <head>
        {/* The status bar is the page's background (globals.css --bg). */}
        <meta name="theme-color" media="(prefers-color-scheme: dark)" content="#111516" />
        <meta name="theme-color" media="(prefers-color-scheme: light)" content="#f2f4f4" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
        <meta name="apple-mobile-web-app-title" content={PRODUCT} />
        <link rel="manifest" href="manifest.webmanifest" />
        <link rel="apple-touch-icon" href="/fiscalm/icon-180.png" />
        <link rel="icon" type="image/svg+xml" href="/fiscalm/icon.svg" />
        <link rel="icon" type="image/png" sizes="192x192" href="/fiscalm/icon-192.png" />
        <link rel="preload" href="/fiscalm/fonts/mona-sans-vf.woff2" as="font" type="font/woff2" crossOrigin="" />
        <script dangerouslySetInnerHTML={{ __html: BOOT }} />
      </head>
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
