# fiscalm API

A guide for integrators. [openapi.json](openapi.json) is the HTTP contract: routes, fields, request schemas and
refusal codes. A running installation serves the same schema without authentication at `GET /api/v1/openapi.json`.
The file is generated from `fiscalm.api.schema.openapi()`; contributors change descriptions there and regenerate it:

```sh
uv run python -c 'import json; from fiscalm.api.schema import openapi; print(json.dumps(openapi(), ensure_ascii=False, indent=2))' > docs/openapi.json
```

## Authentication

Create a token for an account, from the command line or in the app (Setări › Linkuri și chei API; before the first company, Setări › Cont):

```sh
fiscalm token create --user admin --name 'Reporting script' --scope read
fiscalm token list
fiscalm token revoke <id>
```

The secret starts with `fiscalm_` and is shown once; only its digest is stored. Send it as `Authorization: Bearer <token>` to `/api/v1/`, and
never put it in a URL, source control or logs. A token covers every company in the installation; `write` includes
`read`.

Every token belongs to an enabled account, and its scope is limited by that account's current role: a write token
becomes read-only as soon as its owner is made Doar citire (writes return 403 `insufficient_scope`). A disabled or
missing owner returns 401 `unauthorized`; enabling the owner restores access without a new token. Administrators
see every owner's tokens; other accounts see only their own, and Doar citire can create only read tokens.
Revocation and the owner's role are checked again under the write lock in each engine transaction, so a revoked
token cannot start a new write; a transaction already authorized under that lock may finish. App links and
cookies never authenticate the public API, and bearer tokens cannot manage tokens.

Receipt-upload links (Bondrop) are separate upload-only capabilities, not API credentials.

## Transport

Use HTTPS through the installation's reverse proxy. Every route except `/healthz` checks Host and port against
`FISCALM_PUBLIC_URL`, so preserve Host in the proxy. Forwarded headers are trusted only with
`FISCALM_TRUST_PROXY`; see [self-hosting](self-hosting.md).

POST requests need `application/json`, one Content-Length, no transfer encoding, and at most 4 KB (order intake
accepts 256 KB). If Origin is present it must match the server's origin. No CORS is enabled.

`GET /api/v1/meta` is public and reports the installed fiscalm version, the same as `fiscalm --version`.
Responses carry `Fiscalm-API-Version: 1`.

## Reading state

Start with `/api/v1/companies`, then a company's state or resource lists. Treat identifiers as opaque, including
payment ids. An entity's `links` hold related ids, not URLs; null or empty means none. Money is a two-decimal
string, dates are ISO `YYYY-MM-DD`.

Lists return `data` and `next_cursor`: pass the cursor back unchanged, URL-encoded, with the same filters. The
default limit is 50, the maximum 200. Pagination is not a snapshot. The invoicing workspace's embedded lists are not
paginated. Files are served through resource routes, never by storage path.

## Actions

POST `/api/v1/companies/{co}/actions/{action}` with the fields the schema declares for that action. Unknown keys are
refused; a `co` in the body must equal the one in the URL. Invoicing has its own `/invoicing/{operation}` routes and
request schemas. Refresh state after an action and use the returned capability flags; a flag is a preview, and the
engine checks every precondition again when it writes.

Errors are `{"error":{"code":"…","message":"…"}}`. Successful actions return a Romanian message. Match on codes and
schemas, never on messages.

Invoice issue and storno accept an `Idempotency-Key`, scoped to the token and company for 24 hours: retry an
uncertain result with the same key and body; a changed body conflicts. Other operations refuse the header. An
issued invoice does not prove submission to SPV; read its SPV state separately.

What fiscalm never does through the API either: it moves no money and sends no mail. A user marking an invoice paid
is not bank proof. Payment files and documents are downloads a person imports or sends. Administrator settings
(companies, integrations, automations) are app or CLI only, not bearer actions.

### Sandbox

Without `<FISCALM_HOME>/state/LIVE` every local action works with the same evidence and authorization guards.
SPV uploads and status checks go only to ANAF TEST (an `accepted` there has no fiscal value); production uploads, nudges, webhooks, marketplace writes and
payment-file generation and downloads are refused or skipped. The schema's `x-app-action-effects` and
`x-invoicing-action-effects` list each action's effect.

### Accountant's pack, documents and reminders

Prepare a month's pack with the action `pack-prepare` (`month`), read the report's `version`, download
`GET /api/v1/companies/{co}/reports/{month}/pack?version=SHA256`, and after a person has sent it confirm with
`pack-sent` (`month` and that exact `version`). The confirmation covers only that frozen set of documents, never
later arrivals. Issued invoices offer PDF and XML downloads. Reminder tasks carry a `subject` and `body` for a
person to copy. There is no mail-draft action and no outgoing mail route.

## MCP

MCP uses JSON-RPC 2.0 at `POST /api/v1/mcp`, or `fiscalm mcp` over stdio with the token in `FISCALM_MCP_TOKEN`.
It has the same bearer permissions and guards as REST. HTTP requests with an id need
`Accept: application/json, text/event-stream`. For protocol `2025-11-25`, initialize with `protocolVersion` and
`capabilities`, then send `MCP-Protocol-Version: 2025-11-25` on later requests; the newer protocol and its headers
are in the schema. Tool results are JSON text in content blocks, no resource capability is advertised, and
notifications return HTTP 202.

## Signed shop webhooks

Set `webhook_url` and `webhook_secret` together on a shop integration. Events are sent only in Live and delivered
at least once, so a receiver verifies the signature and freshness, then deduplicates by event id. The schema's
`x-shop-webhooks` section gives the header names, the signature calculation, payload fields, event types and
retries. An order can never choose a webhook destination.

## The fiscalm app

The web app uses `/web/api/…` with a `__Host-session` cookie, not bearer tokens. Its sign-in, passkey, recovery,
step-up, own-account and download routes are in the schema's `x-browser-auth`; token management, user
administration, app action fields and roles, and the AI CLI login are in `x-app-token-management`,
`x-app-user-administration`, `x-app-actions`, `x-app-action-roles` and `x-app-ai-cli`. Every browser POST needs a
matching Origin and, when sent, `Sec-Fetch-Site: same-origin`. A sensitive action returns
`401 {ok:false, reauth:true}` until the same account confirms its identity. A refused app action may return HTTP 200
with `ok:false`; authentication and transport refusals use HTTP status codes.

The browser reads `GET /web/api/bootstrap?co=KEY` on first load and firm switches.
It returns `identity` (the `/web/api/me` shape), `state` (the compact summary with Acasă first pages),
and `live`. All three belong to the request cookie; recovery-only sessions receive no state.
Missing or unknown firm keys select `ALL`. The app discards responses across session-change generations
and checks the user, role, authentication and current session digest before applying a firm switch.

Upload links can accept documents for every configured firm: pass `all_firms: true`
when creating a link, or use `fiscalm upload-link create --all-firms --label Phone`.
The upload page identifies this scope as « Toate firmele ». The next tick reads the
buyer using the same ownership evidence as mail intake. Unclear documents remain
in install-level staging, outside every firm's inbox, until a person answers
« Pentru ce firmă e documentul? » in Sarcini. Assignment then starts normal receipt processing.
