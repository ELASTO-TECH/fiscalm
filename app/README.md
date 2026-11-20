# fiscalm app

fiscalm is a bookkeeping app for configured companies: a Next.js static export (Next 16, React 19, TypeScript, App Router).
The engine decides; the app only displays the state and posts user actions. The API is in
[`docs/openapi.json`](../docs/openapi.json).

## Develop

Start it from the repository root with `bin/dev` (fixtures) or `bin/dev engine` (a local engine), as the
[development guide](../docs/development.md) describes: the app needs the engine's venv (`../.venv`), which `bin/dev`
installs. Open http://localhost:3000/ (use `localhost`, not `127.0.0.1`: Next blocks dev resources cross-origin).

`npm run fixtures` (run by `dev`, `typecheck`, `test` and `build`) writes `fixtures/*.json` from
`tests/synthetic_world.py` with the engine's venv (`../.venv`): a synthetic
world around a fixed day, never committed. Under `next dev` the app reads them instead of the API, and POSTs answer
like the engine without changing anything. Only `next dev` reads them: the production build replaces `src/lib/fixtures.ts`
with an empty stub (`next.config.ts`), fails if `NEXT_PUBLIC_FIXTURES` is set, and `scripts/check-no-fixtures.mjs`
(part of `npm run build`) checks known fixture markers in `out/`.

App tests require `../.venv` too: the fake task/search APIs and list parity checks call the engine’s collectors.

Checks: `bin/test.sh full` at the repository root ([testing](../docs/testing.md#running)) runs `npm run lint`,
`npm run typecheck`, `npm test` (vitest + Testing Library in jsdom, `test/`, against a fake engine: POST payloads,
confirm, double taps, stale reads, failed reads, Toate firmele, Facturi filters, the issued invoice's note, reports,
the sheets' focus trap, fixtures kept out) and `npm run build`; `bin/test.sh quick` runs the app tests affected by
your changes.

## Build

```sh
npm run build        # -> out/index.html, out/_next/..., out/fiscalm/...
```

The Docker image runs `npm ci && npm run build` here on every build.

## How the engine serves it

- `GET /` and `/setup`: the public `out/index.html` shell; account setup secrets stay in the URL fragment.
- `GET /_next/...` and `/fiscalm/...`: public code, fonts and icons from `out/`, without financial data.
- `/web/api/me` identifies the cookie session and its current role. Data/actions use `/web/api/...`; document reads use `/web/docfile` and `/web/mailfile`.
- Payment downloads use the Origin-checked JSON `POST /web/api/download {co, v}` and a browser blob.
- The manifest contains no credential. Every browser mutation and sign-in endpoint requires the configured HTTPS origin. Local engine development uses `bin/dev engine` and its temporary HTTPS proxy, as described in `docs/development.md`.

Everything runs locally: no CDN, no Google Fonts, no analytics. Mona Sans (OFL, the authors' variable woff2 from github/mona-sans v2.0.27, unchanged) is in `public/fiscalm/fonts`,
the icons are Phosphor (`@phosphor-icons/react`), bundled.

## Layout

- `src/app/`: the Next.js routes. `(app)/page.tsx` is the whole signed-in app, `(public)/u/` the receipt-upload page
  (Bondrop); `globals.css` holds the tokens (see Design).
- `src/components/`: one file per screen or sheet, named after it (`Home.tsx` for Acasă, `Tasks.tsx` for Sarcini,
  `Tranzactii.tsx`, `Automatizari.tsx`, `Settings.tsx` and its pages, `Sheets.tsx` for the entity sheets), plus the
  shared pieces. The entry points: `Authentication.tsx` (the account gate: the app mounts only after `/web/api/me`
  confirms a session), `App.tsx` (state per view, the POST flow, the top bar and the phone's tab bar), `Desk.tsx` (the
  desktop column) and `context.tsx` (`useApp()`, and `<Company>` / `useCompany()`, so everything inside posts for that
  company). Shared controls are in `Controls.tsx` and `ui.tsx`.
- `src/lib/`: no React screens. `api.ts` (reads and POSTs), `types.ts` (the API types, mirroring the contract),
  `nav.ts` (company, tab, sheet and filters in the URL), `format.ts` (money, dates, plurals), and
  `action-effects.json` and `action-roles.json`, written from the contract by `npm run fixtures`.
- `test/`: vitest tests against a fake fiscalm (`test/helpers.ts`), and the build checks run by `npm run build`.

Screens and navigation are described in [information-architecture.md](../docs/information-architecture.md);
Romanian copy follows [copy.md](../docs/copy.md).

## Keep in mind

- The CSS minifier (lightningcss) reorders declarations inside a rule: `font-feature-settings: "tnum"` has its own rule,
  since the `font` shorthand would reset it.

## Design

One design system, in Tailwind 4 (official PostCSS integration, with its Preflight reset). `src/app/globals.css` holds only the tokens (surface,
ink, muted, line, action, the states and the company palettes, dark and light) and a few base rules; every component
carries its look in utilities. Shared pieces live in `src/components/Controls.tsx` (`Button` with primary,
secondary, quiet and danger; `Input`, `Select`, `Textarea`, `Label`, `Form`, `Actions`, `Panel`; styles for notes,
banners, back links, page tabs, filters, rows and detail lists) and `ui.tsx` (`Row`, `Task`, `Empty`, `Title`, the state
tints). Use them instead of new utility strings or CSS rules.

- Contra's metrics, measured at 1440px (the table is in PR #672): text 13px/20px (lists, buttons, fields, table headers),
  14px nav rows, 15px/23px in forms and Setări, titles 26px (page), 21px (section), 18px (dialog), 9px uppercase labels and
  badges. In the desktop layout buttons and chips are 38px, fields and tabs 42px, nav rows 38px, list rows 56px, the
  column 297px; a phone keeps 44px targets with the same type, radii and padding.
- One surface: `bg-surface`, a 1px `border-line`, no card shadow. Radii as tokens: `rounded-badge` 3px, `rounded-control`
  6px (tabs, menu and nav rows), `rounded-field` 8px (inputs, selects, the firm switch), `rounded-card` 12px (panels,
  menus), `rounded-panel` 16px (list tables, sheets, dialogs); buttons, chips and search fields are pills
  (`rounded-full`), as are dots and the phone's floating nav (test/contrast.test.ts keeps every component on the scale).
- The primary button and focus are the ink itself (`action`: near-black in light, near-white in dark, the same system
  inverted); at most one primary action per group. Colour belongs to states and money only, plus `brand`, the app icon's
  teal, which only the mark and the current navigation item's icon carry (never buttons, cards or the hero).
- States (`ok` done, `wait` on its way, `danger` late or refused, `info` only informative) as text or a small glyph; only
  what needs a person (late, refused, due soon) gets a light tint, never a solid block. Money in and out have their own hues
  (`money-in`, `money-out`) on amounts and arrows; `--co` marks only the company.
- One key-numbers card per page at most (`hero`): Contra's bordered balance card, on the surface's colours.
- Motion only shows where something comes from (`motion`, `fadeOut` in `Controls.tsx`, tokens in `globals.css`): a phone's
  sheet rises from the bottom edge, a desktop panel or dialog rises a few pixels as it fades in, a menu grows from its
  trigger, a page fades in; a closed sheet is gone at once while an unreachable copy fades. The navigation's marker
  (`useMarker`) slides to the current item, content that replaces a skeleton fades in (`revealAfter`), and a row an
  action took away closes up where it was (`rowRef`). 150-220ms, ease-out, opacity
  and small transforms, never on amounts; under reduced motion nothing moves.
- Choices are `Select` (`Controls.tsx`): the app's own list, never the browser's popup, with a native select's props;
  its trigger is a field with a thin caret, its list a card under it (tests pick with `choose`, read with `choicesOf`).
- Loading has three states: unknown, loaded, failed. Unknown shows shapes (`Skeleton`, `SkeletonRows`, `SheetSkeleton`,
  the app's `ShellSkeleton`) as tall as the content, never an empty state, a sign-in, an account action or another firm's
  data; a list read again for the same question (`usePage` `query`) keeps its rows. Under reduced motion they don't pulse.
- Page sections are underlined tabs (`pageTabStyle`); filters are quiet tabs, the current one filled grey, in ink
  and medium weight (`filterLinkStyle`).
- Buttons go in `Actions`, one group per set of choices (never two groups stacked). On a phone a group is one column of
  full-width buttons; from 640px a row of content-width buttons from the content's left edge. Narrow surfaces (the sign-in
  card, the upload link page, sheets up to 480px) wrap their content in `ActionColumn`: one full-width column at every
  width, a quiet button in it reading as a centred link.
- Desktop (from 1100px): `App.tsx` owns one column, at most 1440px, centred beside the navigation with 32px minimum gutters. Page banners and content share it; pages must not add desktop width caps or centring. Narrower forms may sit inside it, aligned left.
- Phone first: 44px targets, the same 26px page title; a sheet rises from the bottom on a phone and
  is a side panel from 1100px.

Some class names (`row`, `card`, `btn`, `sheet`, `tabs`, `late`, …) remain only as hooks for tests and focus handling;
no CSS targets them.
