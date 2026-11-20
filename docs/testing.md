# Testing

How the engine and the app are tested, what a test is for. AGENTS.md sets the
rules; this file says how.

## Running

| When | Command | What runs |
|---|---|---|
| While working | `bin/test.sh quick` | Ruff on the Python files changed against `origin/dev` (staged, unstaged and untracked); the changed `tests/test_*.py` files, last failures first; the app tests Vitest finds affected when `app/` changed, or all of them when an app file was deleted. With no changed test file it says so and re-runs only what failed last time. Seconds. A change confined to engine code or a test helper can run no Python test at all: a green `quick` does not verify it, `full` does. |
| Before merge | `bin/test.sh full` | Ruff on `src tests tools bin`; every Python test with branch coverage of `src/fiscalm` and its total on one line; `tests_private` if present; then the app's lint, typecheck, tests and build. About five minutes with 2 workers. CI and `bin/release` run the same command. |

Extra arguments go to pytest: `bin/test.sh quick tests/test_settlements.py -k payer` runs only what you name,
last failures first. Both commands run under `nice -n 10` with 2 workers; `FISCALM_TEST_WORKERS` changes the
workers, `FISCALM_TEST_BASE` the branch `quick` compares against (it fails if that ref is missing: `git fetch origin`),
`FISCALM_TEST_APP=0` skips the app part of `full`. App packages are reinstalled (`npm ci`) only when
`app/package-lock.json` changed. For a single app test file: `cd app && npx vitest run test/invoicing.test.tsx`.

## What a test is here

The shape is closer to the testing trophy than the pyramid: most value comes from tests that run real code together
on a real SQLite ledger, with only the outside world faked. Four kinds, by what they prove:

| Kind | Proves | Example | Fakes |
|---|---|---|---|
| Rule | One decision, all its cases: a parser, a money rule, a matcher. Pure input to output. | `efactura.rivals`, `camt` parsing, VAT rounding | none |
| Contract | A shape strangers rely on doesn't drift: API state and actions, OpenAPI, UBL against CIUS-RO, the copy catalogue. | `test_api_schema`, `test_cius_ro_official` | none |
| Flow | A user-visible path end to end through the engine: statement in, invoice settled, file journalled. Includes the races and crashes. | upload a statement and settle, two connections settling at once | ANAF, IMAP, Drive, clock |
| App | The app shows the state and posts the exact body. Exact POST bodies. | `app/test/*.test.tsx` | fiscalm (`app/test/helpers.ts`) |

No browser end-to-end suite: the API contract tests and the app tests meet at `docs/openapi.json`.

## Writing tests

- **Name by product area and behaviour**, never by review round: `test_settlements.py::test_a_report_naming_another_firm_refuses_the_whole_batch`.
  One file per area (statements, matching, settlements, invoicing, e-Factura, payments, app API, settings, self-hosting).
- **One rule, one place.** A rule is tested where it lives, with all its cases, once. A flow test checks that the rule
  is wired in, with one case, not every case again through HTTP.
- **Builders, not setup.** Data comes from small builders (an invoice, a statement, a credit, a company) with
  defaults and keyword overrides; a test states only what matters to it. `tests/synthetic_world.py` is the shared
  world for API and app fixtures.
- **Parametrize cases, don't copy tests.** Ids stay short (`conftest.py` shortens long byte or text parameters).
- **Property tests (Hypothesis) for parsers and money:** round trips (format then parse gives the amount back), sums
  that must match to the cent, parsers that refuse instead of crashing on any bytes. Small `max_examples` in the
  suite, a deeper profile on demand.
- **Golden files where a whole document is the contract:** UBL XML, the OpenAPI document, the Romanian copy catalogue.
  One file, reviewed in the diff, instead of dozens of string asserts.
- **No sleeps, no network, no real data.** The clock is passed in; servers poll every 10 ms; ANAF, IMAP and
  Drive are fakes. Synthetic values only (AGENTS.md rule 6).
- **Slow by nature stays rare:** the official CIUS-RO validator (Java, Saxon) and PDF rendering are few tests, and the
  validator tests skip when its artifacts are absent.

## Invariants that always keep a test

Consolidation may merge or rewrite these tests, never drop them. Each needs its refusal path tested as hard as the
happy path.

| AGENTS.md | Tests |
|---|---|
| 1. Never moves money, never sends mail | Drafts and payment files only. **Gap:** no guard test yet that the engine has no send or pay path (to add). |
| 2. When in doubt, do nothing | A debit that fits several invoices settles none (`efactura.rivals`); a marked-paid invoice waits for proof. |
| 3. One transaction settles one item, or one verified batch | Settlement checks (firm, payer, window, net to the cent, every line), the three-confirmations rule, and two connections racing that must not double-settle. |
| 4. Crash-safe file moves | Journal first, finished on the next run; a crash between the two. |
| 5. Untrusted input stays data | DTDs refused, path escapes (`..`, symlinks) refused, size limits, mail and PDF text never chosen as an action. |
| 7. No secret in logs or output | Tokens, passwords and IBANs absent from logs and errors. |
| 8. Shadow mode has no outward effects | Actions refused without `state/LIVE` unless explicitly allowed by their registry; no outward upload or nudge. Configured AI is allowed in both modes; mail access is always read-only. |

## Every PR that changes tests

The PR body shows before and after: test count, full-run seconds, branch coverage of each touched module
(after `bin/test.sh full`, `uv run coverage report --include='src/fiscalm/<area>/*'` lists it per module). Branch coverage of a touched module doesn't go down, the
mutation score of a touched sampled module doesn't go down, and every invariant above keeps its test.

Mutation runs are slow, so they are sampled, not run on every change: mutmut 3 on a copy of the checkout, test
selection limited to the files that exercise the sampled functions, `nice -n 10 mutmut run --max-children 2`.

## Release scan

Before publishing a new repository from a cleaned tree, run:

```sh
uv run --script bin/release-scan --denylist "$FISCALM_HOME/config/release-denylist.txt"
uv run --script bin/release-scan --history --denylist "$FISCALM_HOME/config/release-denylist.txt"
```

The external UTF-8 denylist has one literal term per line; blank lines and `#` comments
are ignored. Never commit it or real configuration. Default scope is tracked working
contents; fetch relevant refs before scanning locally reachable history. Exit codes:
0 clean, 1 findings, 2 incomplete. `--no-denylist` is a baseline, never release approval.
Review findings before copying a clean tree to the new public repository; private
history, PRs and reviews stay private. Binary archives are not unpacked or OCRed.

Synthetic exemptions require a reason and exact paths in `tests/synthetic-ids.txt`;
secret exemptions are exact fake values in `tests/release-allow-secrets.txt`. Public metadata
assignments in that file require an exact path and SHA-256 of `key + NUL + value`, plus a reason;
these pins exempt only assignment heuristics, never token or URL-credential detection.
Binary assets require exact paths and blob hashes in `tests/release-assets.txt`.
Changed bytes invalidate pins. Do not weaken these policies to hide findings.

Zero findings alone does not prove release safety. `tests_private/` and tracked
ignored files always fail the release scan.
