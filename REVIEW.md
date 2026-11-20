# Review rules for fiscalm

Failure here looks like: an invoice or payment shown as paid without bank proof, money settled twice or against
the wrong firm, a real IBAN or secret in output, or a Sandbox install that reaches ANAF, a bank or a mailbox.

Run only the items whose trigger matches the diff. AGENTS.md's non-negotiables (NN) always apply.

## Always check

- **A writer that marks something paid, settled, collected or linked** (`mark_paid`, `reopen`, matchers,
  `add_document`, settlement or `tx-link` code, a `tx=` proof) → one statement transaction settles one item or one
  verified batch; the proof is real evidence, not a string shaped like one; a takeover or undo cannot hand a linked
  item to a second debit (NN 2, 3).
- **A new or changed `UPDATE`/`INSERT` in `ledger.py` or a settling writer, or a read followed by a write** → the
  `WHERE` re-checks the state read (e.g. `ledger.unlinked`), it runs in one `BEGIN IMMEDIATE`, and a two-connection
  test proves only one wins.
- **Bank free text (MT940/CAMT names, merchant keys) reaching a report, CSV, pack, log or projection** → it goes
  through the shared mask (`recon._name`, `statements.masked`) on every output path, grouped and spaced numbers
  included (NN 7).
- **An `await` in `app/src/**` followed by state updates, or a request while the form stays editable** → an answer
  that arrives after the input, firm, route or session changed is dropped, and a test with a deferred answer proves it.
- **A key, lookup or POST that can serve several firms** (`co=ALL`, Toate firmele, task links, uploads) → the key or
  lookup includes the company, and a POST carries the item's own `co`, not the last firm picked.
- **`migrations.py`, `baseline.sql`, `SCHEMA_VERSION` or a changed key formula** → appended, never an edited
  released migration; the upgrade test seeds the real previous schema; stored rows keep their keys.
- **Payment files, ANAF upload, webhooks, marketplace writes, the `LIVE` marker or dev engine startup** → the effect
  goes through the shared mode policy (`Action.effect`, `efactura.client.outward_allowed()`); nothing outward
  without `state/LIVE`; uploads in Sandbox only to ANAF TEST; development engines stay in Sandbox (NN 8).
- **A file path built from a message id, file name, sidecar, backup member or ledger row** → allowlisted id,
  resolved on the opened file, inside the company's own root, symlink swaps refused (NN 5).

Every PR (policy from the setup-verify skill, not a past incident): the body carries verify evidence (`.claude/skills/verify`), not "tests pass". Missing proof for a
behaviour change is blocking.

## Ignore

- More edge-case tests for a simple rule already tested once at its boundaries (AGENTS.md: tests, once each).
- Asserting exact Romanian copy where the string is not the behaviour; wording polish (one copy pass later).
- `app/fixtures/` (generated, uncommitted). Contract and lockfile changes are reviewed as changes, not regeneration noise.
- Problems that predate the PR or fail closed: mention once as pre-existing, not blocking.

## Severity

Blocking = wrong behaviour, a broken non-negotiable, data loss, security, a broken migration, missing proof.
Everything else is a suggestion. Max 5 suggestions. Mark pre-existing problems as such.
