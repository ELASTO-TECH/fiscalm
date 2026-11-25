# Self-hosting fiscalm

fiscalm keeps invoices, statement reconciliation and the accountant's pack on your
server. It prepares payment files and document downloads; a human imports payments and
sends mail. There is no payment or mail-sending endpoint.

Start in Sandbox with invented data, verify the integrations you need, then
enable live mode. ANAF, email, bank import and storage integrations require their
own setup; installing the app does not configure those services.

## Requirements

fiscalm installs with Docker: Docker with Linux containers, Compose v2, Git and internet access to
build dependencies. A Linux desktop or server can host it.
The verified host is Linux/amd64 with rootless Docker; the build and runtime checks
below have not established support for Docker Desktop on macOS/Windows, WSL2,
Linux/arm64 or other architectures. Multi-platform base-image manifests alone are
not an end-to-end test of fiscalm on those platforms. The image includes the Poppler tools
(`pdftotext` and `pdftoppm`) that PDF statement parsing and receipt previews use. Allow disk
space for documents and backups. The Python service invokes a selected AI CLI as a child process. fiscalm is
licensed under AGPL-3.0-only (see `LICENSE`).

## Docker quick start

Install the release source from the public `main` branch:

```sh
git clone https://github.com/ELASTO-TECH/fiscalm.git
cd fiscalm
# main contains the latest release.
git switch main
# Replace this origin with the HTTPS address of your reverse proxy.
cat > .env <<'EOF'
FISCALM_PUBLIC_URL=https://fiscalm.example
FISCALM_TRUST_PROXY=1
EOF
docker compose build
docker compose run --rm --no-deps web init
docker compose up -d --wait
curl -fsS http://127.0.0.1:8790/healthz
```

For reproducibility, select a reviewed
release tag or source commit before building. Keep the source directory for Compose
commands; keep the same Compose project name when moving it.

Configure the reverse proxy for that origin before opening the setup link. The
HTTPS origin must be set before `init` and before the web listener starts; the
loopback HTTP endpoint below is only a liveness check.

The build compiles the static app in a Node stage and installs locked Python
packages in a slim runtime. All base images are pinned by multi-platform digest;
update those digests deliberately for security fixes. There is no published fiscalm
container image. `FISCALM_IMAGE` selects the local image name. `fiscalm --version` in the container reports
the version from `pyproject.toml`.
`/healthz` reports process liveness, not working credentials or integrations.

Without company flags, `init` creates a private install with no companies. Save the one-time setup link and API token to a password
manager when shown: repeated init does not reveal them again. Open the setup link
under your HTTPS origin, choose a password of 12–128 characters, enrol an authenticator
with its QR code and confirm a TOTP code. Save the ten recovery codes shown once. Add the first company by CUI in the app as an admin,
then set its first RON account. Until a company exists, accounting commands refuse
with `Add a company in fiscalm first.` The setup state disappears immediately
after addition, without restarting the server. No payment file can be built until
the RON account is configured and live mode is enabled. Explicit company flags remain supported for CLI
provisioning. For a fresh installation, pass company flags to the **first** `init`
instead of running the empty-ledger `init` above (synthetic data):

```sh
fiscalm init --company-key EXAMPLE --cui 12345674 --legal-name "Example SRL" --iban RO35TEST0000000000000000
# With Compose: docker compose run --rm --no-deps web init --company-key EXAMPLE --cui 12345674 --legal-name "Example SRL" --iban RO35TEST0000000000000000
```

After an empty-ledger `init`, add firms in fiscalm. Existing firm identities are never replaced by `init`.

Init prints credentials to stdout; Docker captures that
output in container logs. Use `--rm` for init, restrict Docker/log access, and do
not copy initialization output into shared logs or tickets. Before configuring HTTPS, SSH forwarding can provide a
private local check: `ssh -L 8790:127.0.0.1:8790 user@server`.

Compose publishes only to host loopback. The explicit `0.0.0.0` listener is inside
the container network. Do not publish it directly to the internet. The image runs
as uid 10001; named volumes retain ownership. Data is in the `data` volume and
credentials in `secrets`; scheduled snapshots use `/data/backups` in the data volume. Both volumes must be private
(mode 700); volumes created by an image before this requirement fix with
`docker compose run --rm --no-deps --entrypoint chmod web 700 /data /secrets`.
The scheduler (`fiscalm scheduler`) shares the data and credentials and runs `fiscalm tick`
every `FISCALM_TICK_INTERVAL` seconds (six hours by default), and within five seconds of a manual check. Its tick lock
prevents overlapping runs. The next due time survives restarts; a fresh scheduler
waits one interval before its first automatic tick. Failed ticks log and wait
until due again. Container shutdown forwards SIGTERM to a running tick and
allows 60 seconds for it to stop. To change the interval or enable a trusted proxy,
put these non-secret settings in a local `.env` file (ignored by git):

```ini
FISCALM_PUBLIC_URL=https://fiscalm.example
FISCALM_TICK_INTERVAL=21600
FISCALM_TRUST_PROXY=1
```

`FISCALM_PUBLIC_URL` is the install's public HTTPS address (the hostname of the reverse proxy below), set once. Upload
links are built on it, and ANAF sends the browser back to `<it>/anaf/callback` after the certificate login.

Then recreate with `docker compose up -d --wait`. `FISCALM_PORT` changes the host-published
port. To override other runtime settings, use a Compose override file with
`environment` and any required read-only mounts on **all three** services.

`init` never copies credentials from another account. Open the setup link,
configure password+TOTP or a passkey, save the recovery codes and continue directly
to adding the first firm, without another login. Passkeys need a DNS HTTPS `FISCALM_PUBLIC_URL`,
for example a Tailscale HTTPS name; password+TOTP also works on an HTTPS IP
address. Configure the HTTPS proxy before opening the link.

Only an Administrator adds the first firm. Other signed-in roles see that the
administrator is still configuring the install. After the account, setup asks only for
the first firm, by CUI, then opens Acasă. A new install runs in Sandbox (ANAF Test) until
an administrator selects Live in **Setări › Mod** and confirms the switch. ANAF, bank, mail, earlier invoices, shops and AI are set up in Setări;
until they are, the **Configurează** card on Acasă lists them for the chosen firm, each linking
to its place in Setări. Mail is read-only. Further firms are added in **Setări › Firme**.

Check liveness with `curl -fsS http://127.0.0.1:8790/healthz` (use the configured
port if changed).
For diagnostics use `docker compose ps` and `docker compose logs web scheduler backup`; each tick also
appends to `logs/finance-YYYY-MM.log` in the data volume.

## Configuration

Use the same environment for CLI, web and scheduler. Firm facts, accounts and
aliases live in `state/ledger.db`. Add firms by CUI in fiscalm and edit their settings
there; no company configuration file is read or imported. `fiscalm init` prepares an
empty ledger for the setup flow. Optional company flags create the first firm only.
`fiscalm --home PATH` selects the ledger and supplies default secret paths for an isolated install.

### Environment variables


| Variable | Engine default | Purpose |
|---|---|---|
| `FISCALM_HOME` | `${XDG_DATA_HOME:-~/.local/share}/fiscalm` | Ledger (`state/ledger.db`), documents, logs, live marker, integration settings |
| `FISCALM_SECRETS` | `${XDG_CONFIG_HOME:-~/.config}/fiscalm/secrets` | TOTP seeds, statement passwords and ANAF credentials |
| `FISCALM_PUBLIC_URL` | unset | Public HTTPS origin for browser sign-in and integration callbacks |
| `FISCALM_HOST` | `127.0.0.1` | Listener address; `tailscale` resolves local Tailscale IPv4 |
| `FISCALM_PORT` | `8790` | Listener port; CLI `serve --host/--port` overrides host/port env |
| `FISCALM_TRUST_PROXY` | unset/off | `1`, `true` or `yes` trusts X-Forwarded-Host/Proto/For; the proxy must replace all three, never pass client values through |
| `FISCALM_APP_OUT` | clone's `app/out` | Built static export |
| `FISCALM_COMPANY_CHECK_TIMEOUT` | `15` | Seconds the tick waits for its company check (a number such as `15` or `0.5`; `1m` is refused) |
| `FISCALM_TICK_INTERVAL` | `21600` | `fiscalm scheduler` seconds between ticks, integer at least 5 |
| `FISCALM_BACKUP_INTERVAL` | `86400` | `fiscalm scheduler --backup` seconds between backups, integer at least 60 |
| `FISCALM_BACKUP_KEEP` | `7` | Scheduled backups kept, at least 1 |
| `FISCALM_BACKUPS` | `<FISCALM_HOME>/backups` | Scheduled local archives; Compose sets `/data/backups` |
| `FISCALM_MCP_TOKEN` | unset | Bearer credential injected into a local stdio MCP process |
| `FISCALM_ANAF_ENV` | `prod` | ANAF host selection: `prod` or `test` |
| `FISCALM_RCLONE` | `~/.local/bin/rclone` | Optional archive executable |
| `FISCALM_CLAUDE` | `claude` | Claude Code executable override (explicit AI integration required) |

### Firms and accounts

Use fiscalm, Setări, Firme to add firms by CUI, choose their display names and colours,
and manage their bank accounts and aliases. The ledger stores those facts together
with their transactions. One account per currency can prepare payments; a replaced
account stays attached to the firm for historical matching. An IBAN cannot belong
to two firms. Company identities and CUIs cannot be rewritten by initialization.
The accountant is configured per firm in Setări, Contabil.

Statement mail senders come from the bank catalogue for current accounts.
Retired accounts and banks without a published sender add no mail source.

### Secrets and access

The secrets directory is private (0700), files 0600, never committed:

```text
<FISCALM_SECRETS>/
  int-<id>-<field>-<version>      # an integration's secret field (the ANAF client secret, a bank's statement password)
  int-<id>-oauth-<version>        # an OAuth connection's tokens (ANAF), written and refreshed by fiscalm
```

Microsoft inboxes use delegated Graph `Mail.Read`, `User.Read` (to verify the signed-in
address) and `offline_access`, with public-client device login through tenant `common`.
No redirect URI or client secret is needed. The product client ID is empty until its
maintainer registers the app. Set `client_id` in `<FISCALM_HOME>/config/microsoft.json`
(JSON object), or override it with `FISCALM_MICROSOFT_CLIENT_ID`. An empty override disables
connection. Tokens and pending device codes use the same private OAuth files as ANAF.
Tenants may require an administrator's consent once (`admin_consent_required`), or block
device login through Conditional Access (`tenant_policy_blocked`). Expired or revoked
consent requires reconnection. No Microsoft password or IMAP write permission is requested.

Other mail access is read-only IMAP over TLS, configured in Setări › Integrări:
host, port, username, password, own address and aliases. Citire facturi enables
reading the selected folder (default INBOX). fiscalm never requests APPEND/write
capabilities, changes message flags, writes drafts, or sends mail.

Client invoices expose PDF and XML downloads. Accountant reports offer
**Descarcă dosarul**, which prepares the pack and downloads it, then **Marchează ca trimis**.
Preparing freezes that company's exact document set and ZIP version: `<firma>-<luna>.zip`
with readable file names and `rezumat.csv`, every transaction of the month and what explains it. Confirmation
marks only those documents as sent; later arrivals wait for a later pack. No mail
account is needed. CLI: `fiscalm pack build --company EXAMPLE --month 2026-10`,
then `fiscalm pack mark-sent --company EXAMPLE --month 2026-10 --version SHA256`.
The build result names the local ZIP; the version is its SHA-256.
Payment reminders show evidence-checked text with **Copiază textul**, in live and
shadow. Copying never marks a payment collected or sends a message.

The account schema stores stable user IDs and installation-wide roles. It has no
legacy link-name mapping or per-company access table/flag. Product schema upgrades
preserve accounts and their session, setup-link, recovery-code and token ownership
relationships; an integrity failure rolls the account-table change back.

Successful sign-in or setup closes the session previously presented by that browser,
including when switching accounts. Replacement is atomic: a failed attempt keeps
the previous session; other devices stay signed in.

The app confirms the current identity when returning to a tab and after a session
change in another tab. Private controls and open sheets are suspended while that
read is unconfirmed; a failed read offers **Reîncearcă**. Same-account edits remain,
and changing accounts clears the previous private view. Cross-tab notification
stores only a random nonce, never credentials. Returning to setup/recovery keeps
its code acknowledgement; another tab changing the session discards the old flow.

Accounts, roles, password hashes, session hashes and recovery-code hashes live in
the ledger. `init` creates the first Administrator and prints a setup link that
expires after 72 hours and works once. The secret is in its URL fragment. TOTP
seeds live only in private `auth-totp-<user id>-<version>` files in FISCALM_SECRETS.
Each installation and restore check needs its own secrets directory.

Passkeys are bound to the DNS host of `FISCALM_PUBLIC_URL`; an HTTPS IP address
does not support them. The credential id, public key, counter and backup metadata
stay in the ledger, while the private key stays with the authenticator. Changing
the public host requires key re-enrolment or an administrator/server CLI reset.
Code-only passkey recovery is restricted to verified key replacement; it never
grants financial access or step-up, and finalization closes every session.
The sign-in screen offers the passkey first where it can work, unless this device
last signed in with the password; otherwise choose **Folosește o cheie de acces**, then
confirm it on the device. Existing owned keys can also confirm the same account for sensitive actions.
This choice requires both server availability and a compatible browser. Returning
from its native prompt preserves the pending confirmation; changing accounts cancels it.

The browser setup page removes the setup secret from the address bar immediately.
On a DNS HTTPS install with a compatible browser, **Folosește o cheie de acces**
creates and verifies the account's first passkey instead of configuring a password
and TOTP. Name the key, confirm it on the device and save the ten recovery codes
before continuing. If the completion answer is lost, **Verifică accesul la cont**
opens an explicit check; sign in with the new key if necessary and replace unsaved
codes in **Cont**. Setup is never resubmitted automatically.
**Cont** shows configured key counts and each session's actual sign-in method.
Password/TOTP change controls apply to an existing configured pair. Administrators
see key-only users as configured and can reset them through a new setup link;
the reset confirmation includes removal of their passkeys.
In **Cont › Chei de acces**, every role can add a key on a compatible device,
rename it or remove it after confirming identity. An additional key enables
sign-in when the first device is unavailable. The last key cannot be removed
without another complete sign-in method. After an unreadable write response,
refresh the list and check the result before trying again.
For password-and-TOTP setup, the QR code is generated locally, without an external QR service; manual
entry of the same key is also available. If the five-minute enrolment expires or
five code attempts are used, choose **Reîncepe configurarea** and start a fresh
enrolment. This is explicit; an unconfirmed completion is never retried on its own.
After TOTP confirmation, the ten recovery
codes appear once and can be copied or saved as a text file. Save them separately
from the password and confirm they are saved before continuing.

If the authenticator or the passkey device is lost, choose **Nu mai am acces** on the
sign-in screen and enter the username and a saved recovery code. The engine decides
what follows; until recovery completes there are no financial views.
For a password account it asks for the password, then a new authenticator and saves
new recovery codes. Completion closes every other session and signs in on this
browser. For an account using only passkeys it opens key replacement: name and verify
a new key, save the new codes, then sign in with that key. Password recovery also
removes every passkey of the account; key recovery keeps only the new key. Either way
the old authenticator, the old keys and the old codes no longer work. If the first recovery response
is lost, **Verifică recuperarea contului** checks the existing session without spending
another code.

**Setări › Cont** is available to every normal role. It shows account metadata,
remaining recovery-code count and the account's sessions. Close an individual other
session or all other sessions after confirming the action and, when requested,
confirming the current identity again. **Ieși din cont** closes the current browser
session. Before the first company, the setup header links to **Cont**. An
unconfirmed close refreshes metadata and keeps a warning; it never repeats the
mutation automatically. The same page changes the password, configures a replacement
authenticator and generates a new recovery-code set. Each requires recent identity
confirmation. Password changes close other sessions; authenticator replacement
closes other sessions and replaces recovery codes after a successful new TOTP.
Save the replacement codes before continuing. Failed or expired enrolment can be
restarted explicitly; an unconfirmed mutation is never repeated automatically.

In **Setări › Utilizatori**, an Administrator can inspect another account’s active
sessions and close them individually after confirming identity when requested. The
administrator’s own sessions link to **Cont**, where current-cookie sign-out remains
separate from closing other sessions. An uncertain close refreshes the selected
account’s session list and keeps the warning without repeating the request.

The server CLI works in Sandbox, without a company configured:

```sh
fiscalm user list
fiscalm token create --user admin --name "Reporting" --scope read
fiscalm user create operator --role member --display-name "Operator"
fiscalm user create reader --role readonly
fiscalm user setup-link admin
fiscalm user reset operator
fiscalm user unlock operator
fiscalm user role operator member
fiscalm user disable operator
fiscalm user enable operator
```

`create` prints the new account's setup link. `setup-link` replaces an unused or
expired link for an account awaiting setup. `reset` removes the account's factors,
recovery codes and browser access immediately and prints a new setup link; server
access can recover the last administrator this way. A configured account requires
an explicit reset. The last working administrator cannot be disabled or demoted.
Passwords are entered only in the browser, never in CLI arguments. Administrator,
Membru and Doar citire apply to every company in the installation. API and MCP tokens
belong to an account and never exceed its current role; a disabled owner cannot
authenticate. `fiscalm token list` includes each token’s owner. No ownerless token
works. Token creation and revocation are audited without storing secrets.

**Setări › Utilizatori** lists global account roles, factor status, last sign-in,
and lock state. Administrators can add accounts, change names and roles,
disable/reactivate or unlock accounts, and create one-time setup/reset links.
A reset of the current account opens its new setup flow directly. The server
keeps the last-working-administrator guard; no company-by-company grants exist.
The browser administration API lists account roles, factor status, last sign-in,
lock deadline and active sessions. Creating/updating/disabling users, issuing a new
setup link, resetting/unlocking an account or closing its session requires a fresh
administrator sign-in (10 minutes). When it is older, the browser keeps the current
form and opens **Confirmă identitatea** for the same account's password plus TOTP
or an owned passkey, when available.
It retries the refused operation once after confirmation. **Renunță** leaves the
operation unperformed; a lost network response never triggers an automatic retry.
A reset preserves the account and role, clears
its factors and sessions, and reveals a new setup link once. API audit metadata
records the administrator's stable ID without secrets.

Normal accounts list/create/revoke their own API tokens in **Setări › Linkuri și chei API**;
before the first company, in **Setări › Cont**.
Read-only accounts can create only read tokens; other roles may choose read/write.
Creation requires recent authentication. Administrators can inspect and revoke all
keys, with their live role checked at the revocation write. The server CLI remains
available for administrators choosing an explicit owner with `--user`. The UI
shows each key’s owner and displays its secret only once. An unconfirmed mutation
refreshes metadata and keeps a warning without automatically repeating the POST.
Bondrop controls require Membru or Administrator; Doar citire never reads the firm’s
upload-link data or offers a write token.

Browser actions, statement/report uploads and history imports recheck the current
session and role in every outer ledger write transaction. API v1 and MCP do the same
for the token, its owner and effective scope. Parsing and other I/O do not hold the
database write lock; nested writes share one atomic authority decision. Disabling,
demoting or revoking a credential before the next transaction prevents its writes.

The HTTP listener requires a configured HTTPS FISCALM_PUBLIC_URL before it starts.
Browser sign-in requires HTTPS, including loopback. The cookie is Secure,
HttpOnly, SameSite=Strict and scoped to its host. A password alone never signs in.
For password/TOTP accounts, recovery uses the password and one unused recovery code,
replaces TOTP, displays new recovery codes and closes every session. Sign in again with the replacement.
No login or recovery mail is sent.

Every normal account can inspect and close its own sessions or regenerate recovery
codes through the account API after recent authentication. Closing other sessions
keeps the current one; closing the current session clears its cookie. Regeneration
reveals ten new recovery codes once and invalidates all previous codes. These local
operations also work in Sandbox and for read-only accounts.

Normal accounts of every role with a configured password/TOTP pair can replace
the password or TOTP after fresh confirmation with that pair or an owned passkey.
Password replacement closes other browser sessions and pending authentication tickets. TOTP replacement keeps the old factor active
until a new code is verified, then closes other sessions and displays new recovery
codes. A failed replacement preserves the working sign-in pair. Removing the only
password/TOTP pair requires an administrator reset and a new setup link.

The scheduled tick removes expired or revoked sessions, tickets and setup links,
including before any company is configured. Failed sign-in audit events are kept
for 90 days; older security events remain, with their IP addresses cleared. Active
sessions last at most 30 days, so their IPs are removed earlier with the session.
It also removes abandoned and superseded TOTP files and interrupted private writes,
only for user IDs in this installation. Files belonging to other user IDs and other
integrations are untouched. `fiscalm auth sweep` runs the same local cleanup manually.

`FISCALM_PUBLIC_URL` must be an HTTPS origin without a path, credentials, query or
fragment; one trailing slash is stripped. Receipt links use it (HTTP is accepted
only for loopback); without it they use the app origin. Invalid origins refuse
link creation before a credential is stored. ANAF requires HTTPS and the registered `<origin>/anaf/callback`.

The initial API write token is shown once; only its hash is stored. Create and
revoke further tokens as described in the [API/MCP guide](api.md).

#### Add companies after installation

fiscalm's company configuration API can preview an ANAF CUI, add an active firm and update
its display name or palette, including in Sandbox. CLI parity:
`fiscalm company add --cui 12345674 [--name Exemplu] [--palette blue]`.
The CLI and API share validation, ANAF lookup, locking, backups and atomic persistence.
See the generated contract's `x-app-actions` metadata in [openapi.json](openapi.json).

Company changes commit atomically to the ledger and are visible to web, CLI and
scheduler reads immediately. New firms start without a paying RON account; add one
in fiscalm before preparing payment files. Backup and restore carry all firm facts
in the ledger snapshot.

#### Document archive

In Setări › Integrări, add an Arhivă Google Drive for each firm: company key,
rclone remote name (for example `drive:`), Google Drive root folder ID and layout.
The remote must be a Google Drive remote configured on the server; credentials stay
in rclone's private configuration. Local filesystem paths and inline backend
configuration are refused. Give each firm its own root.

Layouts are fixed: `month` creates `YYYY-MM/Cheltuieli` and `YYYY-MM/Extrase` as
needed; `quarter` creates `YYYY/Qn/YYYY-MM/<kind>`. Filenames are preserved, including
account-specific statement filenames. Exactly one enabled storage integration must
match the firm; defaults do not override this rule. Missing, disabled or ambiguous
configuration refuses the archive.

Only live mode uploads or calls rclone. Dry runs make no remote requests.
Sources must be regular files within the firm's own data directory, checked on
the opened descriptor, at most 50 MiB. A private staged copy prevents path swaps
from changing the upload. Files already present are compared by MD5 and never
overwritten; conflicting or ambiguous names refuse and retry on the next run.
Successful copies are recorded in the ledger; Drive folder IDs are rediscovered
on retries, without a separate folder map. Changing a destination or layout affects
unarchived documents only; previously archived documents are not copied again.

Going live archives the complete eligible ledger backlog, including history imports;
there is no start-month cutoff. The old empty income folder is not created.
Archive sources are company-owned files only. Phone receipts use Bondrop;
unreviewed uploads remain excluded until reviewed and copied into the firm's storage.
If duplicate Drive folders exist, an administrator must consolidate them under the
configured root before retrying. Folder lookups are cached within a run; no cache
survives a run or a configuration change. The remote type is checked before use.

## HTTPS reverse proxy

Use a hostname you control and replace `fiscalm.example` below. Keep the backend
reachable only by the proxy. Set `FISCALM_TRUST_PROXY=1` in Compose's `.env` and
recreate the services. With trust enabled, the proxy **must replace**
X-Forwarded-Host, X-Forwarded-Proto and X-Forwarded-For. Never pass through or append
client-supplied values: X-Forwarded-For must contain only the client's address as
seen by the proxy, or the client could choose its sign-in rate-limit budget.
X-Forwarded-Host must include the public port when it is not 443. These headers
are ignored by default.
App/API/MCP Origin checks use the configured public origin. Keep authentication
request bodies, Cookie and Authorization headers out of proxy logs. Receipt and
integration capability URLs also stay private. Do not add third-party analytics.

Caddy (automatic certificate management when DNS and inbound ports permit):

```caddyfile
fiscalm.example {
    header Strict-Transport-Security "max-age=31536000"
    reverse_proxy 127.0.0.1:8790 {
        header_up Host fiscalm.example
        header_up X-Forwarded-Host fiscalm.example
        header_up X-Forwarded-Proto https
    }
}
```

Do not enable Caddy access logging for this site. Also configure the proxy's
error logging to discard or redact credential-bearing URLs. nginx (provision certificates
separately and replace the certificate paths):

```nginx
server {
    listen 80;
    server_name fiscalm.example;
    access_log off;
    error_log /dev/null;
    return 301 https://fiscalm.example$request_uri;
}
server {
    listen 443 ssl;
    add_header Strict-Transport-Security "max-age=31536000" always;
    server_name fiscalm.example;
    ssl_certificate /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;
    access_log off;
    error_log /dev/null;
    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header Host fiscalm.example;
        proxy_set_header X-Forwarded-Host fiscalm.example;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

These examples assume the proxy is on the host. A containerized proxy needs a
private shared network and must replace forwarded headers the same way. HTML has
a CSP that allows only local resources and hashes the export's inline bootstrap
scripts, forbids framing, and sends no referrers. `/healthz` is intentionally
unauthenticated; it reports process liveness only, with no financial/config data.

## Going live and integrations

An administrator selects **Sandbox** or **Live** in **Setări › Mod** and confirms the switch in a dialog
that names what changes. When the session's last identity confirmation is older than ten minutes, the
browser first asks for it again with the existing password+TOTP or passkey. The app then shows which mode is active.
The change is audited with the acting account. No CLI command is needed.
The ANAF upload/status environment follows the mode: Sandbox → TEST, Live → production.
Inbox reads stay in production in either mode. `FISCALM_ANAF_ENV` is only a development/test override
for both environments; omit it on self-hosted installs. The commands below remain available to server operators.

```sh
docker compose exec web fiscalm integration
docker compose exec web fiscalm live status
docker compose exec web fiscalm live on
# To disable outward effects:
docker compose exec web fiscalm live off
```

`live on` refuses an empty firm registry. It creates
`<FISCALM_HOME>/state/LIVE`, the only marker; `off` removes it.
Sandbox allows every operation confined to the ledger and this installation's
storage: explanations and remembered rules, settlement and receipt confirmations, card owners,
marking paid (awaiting proof), statement/report uploads, history imports, firms and integrations.
It allows manual SPV/ANAF uploads only to ANAF TEST and refuses production uploads, nudges, webhooks, marketplace write-backs
and payment-file generation/downloads. Local work retains every authorization and money guard. Read-only integrations can also contact external services in Sandbox; Sandbox is not
an offline mode. ANAF reads, IMAP intake and bank reads use their respective credentials and
configuration. Adding a firm by CUI contacts ANAF’s public service without credentials. The configured AI integration also works in Sandbox:
its configuration by the administrator consents to document text being sent to that provider. AI only assists
where rules cannot decide; checked answers never choose the firm or settle a payment.

Existing payment files encountered in Sandbox are journalled and moved to
`.shadow/*.blocked` under the payment folder, preserving their bytes
but taking them out of circulation. Unpaid rows then appear as a missing file; changing the paying
account stays blocked until that state is resolved. Activation alone does not restore them;
a previously committed live import/merge journal can still finish its authorized recovery.
A changed file, invalid journal or symlink stops recovery for manual investigation.

New init stays in Sandbox. Repeated init preserves an already-live install.
`check` may queue a tick in either mode. Live still never sends money
or mail. Configure external services under [configuration](#configuration) and follow [ANAF setup](efactura-anaf.md). Integrations are set up in the app, Setări › Integrări; ANAF is connected
there with "Conectează ANAF", which needs `FISCALM_PUBLIC_URL` ([configuration](#configuration)). `integration` reports prerequisites and the
ANAF connection's state without network requests or secret values. Optional rclone is not bundled in the core Docker image; extend it and mount its config if needed.

### AI providers and Docker

Setări › Integrări › Serviciu AI offers no AI, an Anthropic API key,
an OpenAI-compatible or local server, Claude Code, and Codex. Choosing an integration is explicit consent
for document processing, including in Sandbox. There is no automatic CLI fallback. Existing installs that
relied on that fallback must select a provider once after upgrading; disabling every AI integration disables AI.

An API key or an OpenAI-compatible/local server is recommended. Ollama on the host needs a `/v1` base URL
reachable from the container; `127.0.0.1` inside Docker points to the container, not the host. Restrict the
server's listener to the installation. Receipt images need an API provider with image support; CLIs handle text.

The Docker image installs both official npm packages from `deploy/ai-cli/package-lock.json`: Claude Code 2.1.316
and Codex 0.166.0. CLI auto-updates are disabled.
No CLI is downloaded by the web app, and no login or credential is included in an image. Users build
images locally with `docker compose build`; this project does not publish them.

For personal installs, select “Folosește Claude Code de pe acest calculator” or “Folosește Codex de pe acest
calculator”. The subscription is for a personal computer or a home/office server. A cloud server (VPS) or
an installation serving other firms should use an API key. This note is always shown for CLI choices;
fiscalm makes no IP, ASN or cloud-metadata lookup.

Administrators can connect and disconnect in the web UI, including in Sandbox. fiscalm relays the official
CLI's login prompts: only provider-owned HTTPS URLs and a short device code are shown. Claude's pinned flow also uses the official `claude.com` / `platform.claude.com` domains. A pasted authorization
code is sent directly to the CLI's stdin. No raw CLI output or credential is returned or logged. Only one
login can run at a time; it expires after three minutes, and cancellation kills its process group. Status
and logout also use the official CLI. fiscalm never uses setup-token or sets CLAUDE_CODE_OAUTH_TOKEN.

The CLI writes its own configuration and credentials. Web and scheduler share the named
`ai-cli-home` volume at `/home/fiscalm`. It is outside `/data` and `/secrets`, is not mounted by the backup
service, and is not part of fiscalm backups. fiscalm does not read or copy those files. Back up or reauthenticate
the CLI separately if the volume is replaced.

Terminal login is an alternative:

```sh
docker compose exec -it web claude auth login --claudeai
docker compose exec -it web codex login --device-auth
```

If an executable is missing, Integrări shows `docker compose up -d --build`, which rebuilds the image with both CLIs.

Judge requests use structured JSON and checked answers, with no firm choice or settlement by AI.
Claude Code runs with built-in tools disabled. Codex 0.166.0 has no switch removing every tool: the maintainer
explicitly accepted read-only sandboxing with shell and web search disabled; other tools remain exposed by
the CLI. User configuration, apps, plugins and agent tools are disabled for judge calls, and untrusted
text is supplied as document data. A failed call or invalid JSON produces no decision.

## Statement imports

Supported bank statements: BT monthly PDF (encrypted or unprotected) and ISO 20022 CAMT.053 XML
(camt.053.001.02 through .08), selected by content. Older BT exports come as one PDF per account, or as daily PDFs
of one account: pass all of a month's files to one `fiscalm ingest FILE...`, or pick them together in Încarcă extras. They are checked as one statement (the
whole calendar month, every account the firm held at BT that month, each day once, no gap, balances chaining from
file to file) and stored together, or none is stored. A BT PDF is recognised by "allmerge" in its name,
by its password, or by its text (an unprotected BT statement under any name, whose header CUI names the firm; no
password is read for it). `fiscalm ingest` refuses a CSV on its own: it is uploaded on its account. A BT account's
month may start after the 1st, as older statements print it, only when no day with movements is left out: the days
before are no banking day (weekends and Romanian legal holidays, Orthodox Easter included, `bank.banking_days`), or
the account opened on that day (`fiscalm company opened --month YYYY-MM-DD`; a month alone is not enough) at a zero
balance. The month's accounts are those held from its 1st. A BT statement of several months (quarterly) is split by
its pages into one statement per month, each stored and checked as that month's, with the accounts owed that month; a
file whose pages can't be split that way, or whose parts don't read back to exactly its sections, is refused (and the
app takes one month per upload). Older daily BT statements (`EXTRAS CONT Numarul: N din dd/mm/yyyy`, one per day with
movements) make a month when every number is there and balances chain from file to file; the month's first one is
number 1 or follows the previous month's last, and its last one is followed by the next month's first, so a month of
daily statements is imported together with its neighbours, the latest one waiting for the next month. Numbering that
restarts with a new year proves where January starts, not where December ended: such a December is refused (import
its monthly statement instead). Its opening balance must be proven by the
statement and equal the previous month's stored closing balance for that account, when one is on record, whichever
of the two months is imported first. CAMT.053 is accepted by an administrator in Tranzacții, or from the operator CLI:
`fiscalm ingest FILE` (store and reconcile) or `fiscalm statement FILE` (inspect). Mail and public
receipt links do not accept CAMT. XML is unsigned text: a matching IBAN and consistent balances
do not authenticate its author. Only full calendar months are accepted; a different document
covering an already imported account-period is refused.
Only booked entries reconcile. Each account must belong to the configured company and its OPBD (or PRCD)/CLBD
balances must agree with parsed credits and debits; an untrustworthy account settles nothing.
Batches require individual amounts in the account currency and stable bank references. Reversals,
missing references, mixed batch directions and unsupported precision require manual review.
MT940 follows the same rules as CAMT: the accounts are the company's, opening and closing balances chain and add
up, one full calendar month, booked lines only; reversals are refused. CSV exports are read with the columns picked
once per account in fiscalm (date, amount or debit and credit, details, optionally the other party, its IBAN and the
reference); a CSV names no account, so it is uploaded on one, and it carries no balances, so its document says its
totals can't be checked. A CSV is a partial import: it proves only the days it holds (CSVs of one account add up day
by day, overlapping days are refused), it never stands for the month's statement (automatic payments still wait for
one), and two identical rows are refused (map the bank's reference column). Amounts must be written as the mapping
says (thousands only by threes, at most two decimals). The same CSV read on another account or with other columns is
another document. MT940 needs final opening and closing balances (60F, 62F), no missing days between daily
statements, and lines that add up to the balances. MT940 and CSV are accepted from the operator CLI (MT940) and from an administrator's upload
in fiscalm (both); never from mail or public receipt links. A stored CSV keeps its account and columns beside it, so
it can be read again.

## Backup and restore

Commands run inside the containers, with the installation's own paths (`/data`, `/secrets`):

```sh
docker compose run --rm --no-deps web backup /data/books.zip
docker compose run --rm --no-deps -it web backup /data/books-with-secrets.zip --include-secrets
# The second command prompts for a passphrase on the terminal; never put it in argv.
docker compose cp web:/data/books.zip /safe/place/books.zip
```

The ZIP contains a manifest (format, schema version, UTC creation time, source
home and SHA-256 per member), an SQLite online snapshot, company documents,
e-Factura files, history, inbox, pending mail documents, payment files, and firm facts in the ledger.
Logs, scheduler state and the LIVE marker are excluded. Only the
configured company directories and product document roots are included; arbitrary
files placed elsewhere in FISCALM_HOME are not a document store. Symlinks and
special files refuse the backup. A changing document tree refuses publication;
retry after the import finishes. SQLite uses its online backup API in small page
steps; document copying and compression hold no database lock, so writers can
continue. Unfinished file-move journals refuse backup and restore; finish source
recovery first. The completed archive is private (0600) and never overwritten.
Keep the destination outside document roots. Allow free disk space for staging.

Secrets are excluded by default. `--include-secrets` puts FISCALM_SECRETS into an authenticated AES-256-GCM payload, with a random salt
and nonce and scrypt (N=32768, r=8, p=1) passphrase derivation. The secrets ZIP is
built in memory, never staged in clear on disk. Books and company configuration
remain in clear in the outer ZIP: store archives on a private encrypted disk.
Keep the passphrase separately; it cannot be recovered.

Stop the services for the **destination**, then restore into an empty directory: one that does not
exist yet, or an existing empty one such as a fresh Docker volume. For Docker, follow
[Restore into a new Docker installation](#restore-into-a-new-docker-installation).

```sh
fiscalm --home /new/finance restore /safe/place/books.zip
# For an archive containing secrets (prompts for the passphrase on the terminal):
fiscalm --home /new/finance restore /safe/place/books-with-secrets.zip --with-secrets
```

Restore validates every checksum, SQLite integrity and foreign keys, and refuses schemas newer than
this code. It refuses nonempty destinations, duplicate members, path escapes, symlinks, unexpected
runtime state and wrong/missing passphrases. Empty means no entries at all; only `lost+found` is
allowed, and only when the directory is a filesystem root. Validation happens in a private staging
directory, `.fiscalm-restore`, inside the destination; a failed validation removes it and leaves the
destination as it was. Secrets go to FISCALM_SECRETS: inside that staging directory when it is
`<home>/secrets` (the default with `--home`), otherwise in a `.fiscalm-restore` of its own inside
that directory, which must be empty or not exist. Each staging directory is created exclusively: one
that already exists is never adopted or removed. A failure before publishing removes the secrets
staging directory first and the home's last, and leaves both destinations as they were; if one cannot
be removed, the restore says which and the home keeps its staging directory. Publishing then follows a
fixed order: each staging directory, its destination and every directory the restore created on the
way are synced to disk before anything moves; entries are moved with a rename that never
replaces an existing file (Linux `renameat2`; another system or filesystem refuses the restore),
secrets first and the ledger's `state` directory last; everything is flushed to disk; only then are
the staging directories removed, the home's last. While a `.fiscalm-restore` exists in the home or in
the secrets directory, the web
service, `init`, `live`, every CLI command that opens the ledger, the scheduler's ticks and both of
its loops, every backup (manual, scheduled, pre-migration) and the backup download refuse, so a
half-restored home is never initialised, served or backed up, and no older backup is pruned. If a
restore is interrupted while publishing, running it again refuses too and never merges two archives:
stop the services, empty the home and the secrets directory, and restore again. Restored directories
are private (0700) and files 0600, whatever the umask.

Document references under the original home (including its symlink alias) are relocated to the
destination. Ledger file references outside that home or containing path traversal refuse restore;
documents must live in the product storage roots. It never runs migrations. An archive never
contains `state/LIVE` (an archive that does is refused), so **the copy always starts in Sandbox**:
nothing outward happens until an administrator has reviewed it and selected Live. Use a secrets
directory isolated from every running installation. Accounts and their sessions are in the ledger:
before starting the restored web service, run `fiscalm --home /new/finance auth forget-sessions` to
revoke restored cookies, tickets and setup links. Review integrations and paths before explicitly
selecting Live in Setări › Mod.
Without secrets, existing passkeys remain usable at the same DNS HTTPS origin.
To replace a missing TOTP seed, sign in with an owned passkey and choose
**Cont › Schimbă aplicația de autentificare**. If no working sign-in method is
available, run `fiscalm --home /new/finance user reset admin` and configure a new
password/TOTP pair or passkey through its setup link. Reconfigure integration
credentials separately.

### Restore into a new Docker installation

This recovers an installation that lost its volumes, and moves an installation from another machine
into Docker. Run every command in the installation's directory, with the same project name
(`COMPOSE_PROJECT_NAME`) as the volumes you restore into.

1. Stop the services: `docker compose down`.
2. Check that both volumes are empty (or new). This lists their entries and must print nothing
   under `/data:` and `/secrets:`:

   ```sh
   docker compose run --rm --no-deps --entrypoint ls web -A /data /secrets
   ```

   If anything is listed, for example because `docker compose up` already created a new installation,
   restore elsewhere or, only if nothing there must be kept, remove the volumes with
   `docker compose down -v`, which deletes the data, the secrets and the AI CLI logins of this project.
3. Make the archive readable to the container's user (uid 10001) without exposing it on the host: a
   private directory holds a readable copy, mounted read-only.

   ```sh
   install -d -m 700 ~/fiscalm-restore && install -d -m 755 ~/fiscalm-restore/archive
   install -m 644 /safe/place/books-with-secrets.zip ~/fiscalm-restore/archive/books.zip
   ```

4. Restore. For an archive with secrets, add `--with-secrets`: the passphrase prompt reads from your
   terminal, which `docker compose run` attaches by default, so it never appears on a command line or in
   shell history. Then revoke the restored sessions.

   ```sh
   docker compose build
   docker compose run --rm --no-deps -v ~/fiscalm-restore/archive:/restore:ro \
     web restore /restore/books.zip --with-secrets
   docker compose run --rm --no-deps web auth forget-sessions
   rm -r ~/fiscalm-restore
   ```

5. Start and check:

   ```sh
   docker compose up -d --wait
   docker compose exec web fiscalm live status    # prints shadow: Sandbox
   docker compose logs scheduler backup           # a tick ran; the backup service wrote an archive
   ```

   Sign in as before (without restored secrets, see the TOTP and passkey notes above), check the
   firms, documents and integrations, then select Live in Setări › Mod when the copy is right.

If the restore stops while publishing (the container is killed, the disk fills), the services refuse
to start and the restore refuses to run again. Remove the volumes with `docker compose down -v` and
start again from step 2.

Compose includes a separate backup service (`fiscalm scheduler --backup`): it runs
`fiscalm backup --scheduled`, keeping the newest `FISCALM_BACKUP_KEEP` archives only after a successful backup.
Backups exclude secrets so it needs no passphrase; see failures with `docker compose logs backup`. Its archives live in `/data/backups`
in the `data` named volume, initialized for the image's user. Losing that volume
also loses these local snapshots; keep an independent off-host copy. No host-directory
setup is needed. Copy archives independently of the installation: `docker compose
down -v` removes these local snapshots too. Set `FISCALM_BACKUP_INTERVAL` (seconds,
at least 60, default 86400) and `FISCALM_BACKUP_KEEP` (default 7).
The service backs up on startup and at the interval, logs failures, and retries at
the next interval. A manual scheduled snapshot uses the web service's CLI entrypoint:
`docker compose run --rm --no-deps web backup --scheduled`.
Administrators see the last completed local backup and can download it in Setări;
members cannot read metadata or download books. Retain an independent off-host copy; document uploads are not a backup.

### Container backup and restore test

```sh
# Paths here are inside the container. Keep web running for cp/exec below.
docker compose run --rm --no-deps web backup /data/manual-backup.zip
# Copy privately to the host before replacing volumes.
docker compose cp web:/data/manual-backup.zip "$HOME/fiscalm-manual-backup.zip"
# Restore into an empty directory, never over the running data.
docker compose run --rm --no-deps web --home /data/restore-check restore /data/manual-backup.zip
```

A restore refuses a non-empty destination, damaged archive or newer schema. It
rewrites stored document paths for the new root. To exercise the restored copy,
use both data and secrets overrides, because `--home` preserves environment variables that are already set:

```sh
docker compose exec -e FISCALM_HOME=/data/restore-check \
  -e FISCALM_SECRETS=/data/restore-check/secrets web fiscalm live status
# After exporting and testing, remove only these temporary copies.
docker compose exec web rm -rf /data/restore-check /data/manual-backup.zip
```

Use those overrides for every command against the restored copy. Accounts, password hashes
and recovery-code hashes are in the ledger; TOTP seeds are in FISCALM_SECRETS. If seeds
were excluded, use `fiscalm user reset <username>` to set up the affected accounts again.
Never enable LIVE in a restore test. [Full stopped-volume backups](#upgrade-and-uninstall) additionally preserve credentials
and integration configuration outside the snapshot. Protect both kinds as private
financial data; a snapshot without secrets still contains company documents.

## Release notices

On by default, each day's first scheduled tick (`fiscalm update-check`) makes an anonymous
GET to the fixed public endpoint `https://api.github.com/repos/ELASTO-TECH/fiscalm/releases/latest`.
This read also runs in Sandbox. No authentication, install data, company facts,
version, identifiers or telemetry are sent (GitHub sees the connecting IP).
The request has a four-second socket timeout (ten seconds for the tick command), a 64 KiB response cap and refuses redirects.
Only `tag_name`, `html_url` and `published_at` are retained; malformed responses
are ignored and retried the next day. Prereleases are ignored. A newer stable
SemVer appears in Setări, with a dot on its entry and a validated GitHub release
page link. A failed check never prevents accounting work.

Administrators can turn off **Verifică versiunile noi** in Setări; the preference
is stored in the ledger for the whole installation. Turning it off also hides any
cached notice. No update is downloaded or executed. The update notice displays
`git pull && docker compose up -d --build`; back up and follow the upgrade steps below.

### Anonymous usage statistics

On by default, the same scheduled command also POSTs to the fixed endpoint
`https://fiscalm.ro/api/v1/ping`, at most once a day, only in Live. The JSON contains
exactly `install` (a random UUIDv4 stored once in ledger meta), `version` (fiscalm version),
`firms` (active firm count) and `schema` (ledger schema version). No CUIs, names, IBANs,
amounts, hostnames or user counts are sent. The receiver sees the connecting IP.

Administrators can turn off **Statistici anonime** in Setări › Sistem. Setting
`FISCALM_TELEMETRY=0` overrides that preference, including in Docker Compose: put it
in the Compose `.env` and recreate the services. Sandbox never sends statistics.
Requests refuse proxies and redirects, use a four-second socket timeout and a 64 KiB
response limit. Failures, including non-2xx responses, skip the day without interrupting
the tick; logs contain only a fixed failure message.

## Upgrade and uninstall

Back up before upgrades. For a stopped-volume backup, stop all services, copy all
FISCALM_HOME plus external config/secrets and mail/storage tool configuration,
preserving private permissions and container uid 10001, then restart:
`docker compose stop` / `docker compose start`. Archive both `data` and `secrets`, not just the data volume. Encrypt full copies, restrict access and test
restores in an isolated Sandbox installation. Full copies include LIVE: remove its
marker before a restore test.

Read [CHANGELOG.md](../CHANGELOG.md). Stop the old installation's services,
then extract the reviewed archive into a **fresh** directory, never over old source
files. Compose derives its project name and volume names from the directory name
unless overridden. Copy the old `.env` privately into the new directory and set
`COMPOSE_PROJECT_NAME` there to the old project name (`fiscalm` for the clone above,
unless explicitly overridden), so the new directory reuses the existing volumes. Existing clones can
instead use `git pull --ff-only`. Run `docker compose build` in the updated source,
then apply migrations explicitly before restarting (all commands must use the
same project name):

```sh
docker compose stop
docker compose run --rm --no-deps web init
docker compose up -d --wait
```

`init` uses the same ordered migrations as web startup; repeated initialization
preserves existing firms, credentials and Live/Sandbox mode.
Restart web after rebuilding the static export: its inline-script CSP hashes are
cached at startup. Scheduler interval changes apply on scheduler restart; an old
due time beyond the new interval is recomputed.

Before upgrading an existing schema, startup creates a full document and ledger
archive with `fiscalm.backup` in `FISCALM_BACKUPS` (default `<FISCALM_HOME>/backups`).
Names include the UTC date and from/to schema versions; the newest five
`pre-migration-*.zip` archives are retained independently of scheduled backups.
Secrets are excluded, as for scheduled backups. No archive is needed for a fresh
or current schema. If backup fails, startup refuses to migrate: check free space,
permissions and other upgrading processes, then retry. Pending file-move journals
must be recovered with the previous release first. Schemas older than the company
registry (1–4) refuse automatic migration because a complete backup cannot be
guaranteed; keep the previous release and make and verify a full installation copy. A crash during migration leaves
the archive intact. To roll back, stop services, use the matching release and
`fiscalm --home /new/empty/finance restore /path/to/pre-migration-backup.zip`
as described under [Backup and restore](#backup-and-restore).

Web/tick startup applies ordered SQLite migrations under a write lock. Each
migration and recorded version commit together; crashes roll back unfinished work.
Fresh initialization and upgrades share a per-ledger startup lock. A concurrent starter waits,
then rechecks the schema before deciding whether a backup is needed. SQLite lock timeouts leave
the schema unchanged; stop old
services and restart the new release. A newer ledger is refused by older code;
downgrades are unsupported. Rollback may require restoring the matching backup
with its release. `fiscalm.migrations.SCHEMA_VERSION` is the current version;
the product ledger is not a conversion of the private MVP.
There is no stable release cadence or downgrade compatibility promise yet.

`docker compose down` preserves volumes.
`down -v` also deletes local snapshots in the data volume: use it only after
exporting and verifying an independent backup. Keep data/secrets until explicitly choosing to delete them.

### Shopify Payments payouts

`shopify-sync` also reads payouts and their balance transactions through the existing
Shopify channel, configured domain and custom-app token, pinned to GraphQL `2026-10`.
Grant `read_shopify_payments_accounts`, `read_shopify_payments_payouts` and the user's `view_payouts` permission in addition
to the order scopes. Only payouts issued on or after the channel connection date are imported.
Both connections paginate completely (at most 100 pages each);
a failed read retries on the next tick, without importing a partial report. Imported or withdrawn payouts are never read again;
a withdrawn report stays withdrawn. Older payouts can use manual report intake.

Only paid deposits made entirely of non-test charges with order references and exact,
same-currency gross/fees/net become company/channel-scoped settlement reports. Refunds,
adjustments, missing orders and inconsistent totals refuse the whole payout; the tick's
`payout_refused` count reports refusals without remote contents. These require manual
review. The payer identity is `Shopify Payments`; a different bank payer name refuses
matching and requires a manually described report with the actual payer identity.
Local import/reconciliation also works in Sandbox, with no new remote writes.
Collection uses the existing whole-report bank checks and first-three-confirmations
rule; Shopify's paid flag alone never collects an invoice. Order references resolve
only through that company's channel and issued invoices linked from sent SPV documents.


### Open banking: each install's own Enable Banking account

In Setări › Integrări › Enable Banking, the five-step guide prepares the public certificate on the server,
accepts the application ID and return addresses, and checks the provider's application. Opening the guide
makes no provider request; “Pregătește certificatul” generates the key once, and “Verifică configurarea”
explicitly checks the saved application. The private key never leaves `FISCALM_SECRETS`.

1. Create this install's own free Enable Banking account.
2. In API applications, add a Production application named fiscalm and import the public certificate
   prepared by fiscalm. Register every return address shown in the guide.
3. Save the application's ID and return addresses in fiscalm.
4. Authorize the company's own accounts in Enable Banking's restricted mode. For BT, choose BT Go
   and confirm in BT Go. Connecting from fiscalm later requires another bank confirmation.
5. Run the guide's checks. Each failed row explains what remains; only a complete successful check
   says the setup is ready. The same checks are available as `fiscalm bank check`.

Restricted mode is free for the owner's own accounts. A hosted fiscalm serving clients needs the commercial
tier. Open banking is read-only and works in Sandbox. These written steps are the guide's authority;
no third-party panel screenshots are distributed or claimed to be current.

The primary return address is the install's origin followed by `/bank/return`. Additional addresses can be
entered in “Alte adrese de întoarcere”, separated by whitespace, for example a domain and a Tailscale name.
All must also be registered in Enable Banking. `fiscalm bank check` checks all registrations. A connection
selects the configured address matching the browser's Origin; an unknown origin is refused before the
bank call. No action body can supply a return address. Use HTTPS, or HTTP only on localhost, 127.0.0.1 or ::1.
A flow started on a computer at localhost returns to that computer. Starting on a phone requires an HTTPS
address the phone can reach.

The bank callback only prepares an account choice, valid for 15 minutes. The administrator who started
the connection reopens fiscalm at the same firm and explicitly selects the firm's accounts before reading
starts. No account is selected automatically; new accounts use the same firm/partner and IBAN checks as
manual entry and do not become payment accounts. Only opaque choice IDs are accepted, never IBANs or
provider account IDs from the request. On phones, the bank may return to a different browser than the
installed web app: no callback cookie is required, and returning alone never activates access. Reconnecting
keeps the old connection until the selected accounts and new session commit together.

After adding the first firm, fiscalm opens Acasă; its Configurează card links to the firm's bank section.
Choose the catalogue bank before entering an IBAN, or continue without connecting. The same section, “Băncile
firmei”, opens with “Modifică” on the firm in Setări › Firme.
Before leaving for bank confirmation, fiscalm keeps this firm's bank-section link in the app address;
reopening the app restores account selection, including after a phone handoff to a different browser.

Only booked bank transactions can confirm payments. Pending lines, non-finite amounts and amounts that
would need rounding to cents are refused. Statement and direct-bank proofs with the same account-servicer
reference count once, including copies held while sources overlap across month boundaries. Conflicting
amount, currency or direction holds the evidence; rereading a stable key never replaces its original
monetary proof or settles another item. Historical statement uploads remain available as fallback.

An administrator can use “Oprește citirea directă” on the firm's bank card. This removes the local session
and cancels pending connections and account choices for that firm and bank. Imported history, firm accounts,
other banks, and mail/upload statement intake remain. A read already started can finish. This makes no provider
request and does not revoke bank-side consent; revoke that separately in the bank or Enable Banking if needed.
