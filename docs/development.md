# Development

Prerequisites: Git, Bash, Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js 22
with npm, and `openssl` for `bin/dev engine`. Java 17+ is optional: the official invoice-validator tests skip without
its artifacts. Verified on Linux/amd64; the scripts use only stock tools, so macOS should work but is not verified.
All examples use invented firms. Never point development at an installation's data or secrets.

## Start

```sh
git clone https://github.com/ELASTO-TECH/fiscalm.git
cd fiscalm
git switch dev
bin/dev
```

`bin/dev` installs the locked Python packages, installs the app's packages when `app/package-lock.json` changed,
generates fixtures and starts Next.js at http://localhost:3000 (use `localhost`, not `127.0.0.1`). Edit `app/src/`
and the page reloads. Reads come from synthetic JSON; actions simulate answers without changing anything, and
nothing is stored. Ctrl-C stops it.

## App with a local engine, in shadow

```sh
bin/dev engine           # bin/dev engine --fresh discards its data and starts over
```

Open https://localhost:3000 and accept the one-day localhost certificate. The first run prints a one-time setup
link for the administrator; open it, set up the account and the first firm. Everything lives in `.dev/engine/` inside
the clone (ignored by git): later runs keep the account, the two invented firms and what you did with them. If you
lose the setup link before finishing, run `bin/dev engine --fresh`. The engine and its proxy start only in a directory
holding the `.fiscalm-dev` file `bin/dev` writes: a guard against starting them on the wrong directory by accident,
not proof of who made it. What keeps them off a real installation is that the engine refuses a `state/LIVE` marker.

A loopback HTTPS proxy on 3000 sends app assets to Next.js on 3001 and `/web/`, `/auth/` and API requests to the
engine on 8765; it pins the Host and replaces forwarded headers itself, so the browser cookie keeps its Secure and
`__Host-` protections. App edits reload immediately; the engine restarts after Python edits under `src/fiscalm/`.
Ports 3000, 3001 and 8765 must be free. Ctrl-C stops all three. The engine never calls ANAF: it answers
the public CUI lookup from an invented registry (`tests/anaf_fake.py`) and refuses every other `anaf.ro` address.

No scheduler runs in development: "check now" in the app only leaves `state/tick.request`, which nothing picks up
unless you run `fiscalm scheduler` (or one tick with `fiscalm tick`) against the same home. One tick, from the clone:

```sh
env FISCALM_HOME="$PWD/.dev/engine/home" FISCALM_SECRETS="$PWD/.dev/engine/secrets" \
  FISCALM_BACKUPS="$PWD/.dev/engine/backups" uv run fiscalm tick
```

It logs to `.dev/engine/home/logs/`. A tick started this way is not offline: it can refresh partners from ANAF.

Shadow mode allows local work and refuses outward effects, including usable payment files. Do not add a LIVE marker
for development: both `bin/dev engine` and `bin/dev-session.py` refuse a request to switch to Live before writing anything. The trusted proxy is loopback-only; do not expose it or copy its setting to a deployment.

## Checks

`bin/test.sh quick` while working, `bin/test.sh full` once on the final head before merging (CI runs the same).
What each runs and its options are in [testing](testing.md#running).

## Adding a test

Put it in the existing test file of the product area (`tests/test_settlements.py`, `app/test/invoicing.test.tsx`)
and name it after the behaviour. Data comes from builders and fakes, never real data: `tests/conftest.py` gives every
test its own temporary `FISCALM_HOME` and a `ledger` fixture; `tests/anaf_fake.py`, `tests/imap_fake.py` and
`tests/pdf_fake.py` stand in for the outside world; `tests/firms.py` holds the invented firms;
`tests/synthetic_world.py` the shared world that `npm run fixtures` turns into `app/fixtures/*.json` for the app and
its tests (generated, never committed). App tests use `app/test/helpers.ts`. Run it with
`bin/test.sh quick`; [testing](testing.md) says what a test is for and which rules always keep one.

## Where a change goes

- `src/fiscalm/`: engine rules and storage. Use the existing product area: `bank/`, `commerce/`, `settlements/`, `invoicing/`, `efactura/`, `mail/` or `payments/`.
- `src/fiscalm/api/`: app/public API projections, actions and HTTP transport. Generate `docs/openapi.json` from `api.schema.openapi()` when the contract changes.
- `app/src/`: rendering and interaction. Business decisions stay in the engine. Formatting lives in `app/src/lib/format.ts`.
- `tests/` and `app/test/`: synthetic fixtures and behaviour tests. Never commit generated app fixtures or real company data.
- `bin/` and `deploy/`: local tools and installation. Runtime data stays outside the clone.

The code and Romanian copy rules are in [AGENTS.md](../AGENTS.md); the app-specific guide is [app/README.md](../app/README.md). New user-facing wording needs maintainer approval. Keep API field descriptions in the generator, not a second field reference.

## First PR

Read [CONTRIBUTING.md](../CONTRIBUTING.md) and [CLA.md](../CLA.md), pick a small issue, branch from `dev`, and change one behaviour with its relevant test. Run the checks above, open a draft PR targeting `dev`, and include results and proposed wording. UI changes include before/after screenshots at about 390 and 1280 px with synthetic data. Sign the CLA with the approved comment when prompted; address blocking review findings before marking ready.
