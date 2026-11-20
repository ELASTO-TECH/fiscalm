CREATE TABLE account_month_source (  -- which source settles an account's month: the first to bring it
  company TEXT NOT NULL, iban TEXT NOT NULL, month TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY (company, iban, month)
);

CREATE TABLE ambiguous_tx (  -- statement debits that fit several candidates, so settled none
  tx_key TEXT PRIMARY KEY, company TEXT NOT NULL,
  tx TEXT NOT NULL,                    -- the debit: statements.Tx.to_dict() as JSON
  candidates TEXT NOT NULL,            -- JSON [["efactura", message_id] | ["payment", item_id], ...]
  seen_at TEXT NOT NULL
);

CREATE TABLE anaf_auth_states (  -- "Conectează ANAF" in progress (anaf.start); hashes only, single use
  link_hash TEXT PRIMARY KEY, browser_hash TEXT UNIQUE, integration_id TEXT NOT NULL, client_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL,
  stage TEXT NOT NULL DEFAULT 'issued'  -- issued -> opened (the browser has its cookie) -> used
);

CREATE TABLE api_invoice_requests (
  token_id TEXT NOT NULL, company TEXT NOT NULL, key_hash TEXT NOT NULL,
  fingerprint TEXT NOT NULL, created_at INTEGER NOT NULL, response TEXT NOT NULL,
  PRIMARY KEY(token_id, company, key_hash)
);

CREATE TABLE api_tokens (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, scope TEXT NOT NULL CHECK(scope IN ('read','write')),
 secret_hash TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT, created_by TEXT
);

CREATE TABLE bank_auth_states (  -- a connection started in Fiscalm, waiting for the bank to send back
  state TEXT PRIMARY KEY, company TEXT NOT NULL, bank TEXT NOT NULL, created_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE bank_connections (  -- open banking: one per company and bank; the session is a secret file
  id TEXT PRIMARY KEY, company TEXT NOT NULL, bank TEXT NOT NULL, provider TEXT NOT NULL,
  accounts TEXT NOT NULL,        -- JSON [{"iban":..,"uid":..}]: the company's accounts the bank shared
  valid_until TEXT NOT NULL, created_at TEXT NOT NULL, last_read TEXT, last_error TEXT,
  session_version TEXT NOT NULL DEFAULT '', attempted_on TEXT,  -- which session secret file is the current one
  UNIQUE (company, bank)
);

CREATE TABLE client_reminders (
  invoice_id TEXT NOT NULL, sequence INTEGER NOT NULL, company TEXT NOT NULL,
  prepared_on TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','draft')),
  message_id TEXT, PRIMARY KEY(invoice_id,sequence)
);

CREATE TABLE commerce_delivery (
  order_id TEXT PRIMARY KEY, picked_up_at TEXT, returned_at TEXT
);

CREATE TABLE commerce_orders (
  id TEXT PRIMARY KEY, company TEXT NOT NULL, channel TEXT NOT NULL, external_id TEXT NOT NULL,
  placed_at TEXT NOT NULL, body TEXT NOT NULL, draft_id TEXT NOT NULL, client_id TEXT NOT NULL,
  draft_version INTEGER NOT NULL, status TEXT NOT NULL, awb TEXT, reason TEXT,
  UNIQUE(company,channel,external_id)
);

CREATE TABLE commerce_outbox (
  id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, channel TEXT NOT NULL, company TEXT NOT NULL,
  body TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
  lease TEXT, sent_at INTEGER
);

CREATE TABLE csv_formats (  -- which CSV column is what, picked once per account (finrec.csvstatement)
  company TEXT NOT NULL, iban TEXT NOT NULL, mapping TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (company, iban)
);

CREATE TABLE documents (
  sha TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  month TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('statement','receipt')),
  path TEXT NOT NULL,
  vendor TEXT, doc_date TEXT, amount TEXT, currency TEXT, card_last4 TEXT,
  issues TEXT,           -- JSON list of strings
  source TEXT,           -- e.g. mail id or 'gmail:Finance/NORD'
  sent_at TEXT, sent_message_date TEXT,
  created_at TEXT NOT NULL
, note TEXT, efactura_id TEXT, accounts TEXT);

CREATE TABLE efactura (  -- supplier invoices received through ANAF e-Factura
  message_id TEXT PRIMARY KEY,         -- ANAF download id: one row per message, never fetched twice
  company TEXT NOT NULL,
  upload_id TEXT, listed_at TEXT,
  doc_type TEXT, number TEXT, issue_date TEXT, due_date TEXT, currency TEXT, total TEXT,
  supplier_name TEXT, supplier_cui TEXT, supplier_iban TEXT, bic TEXT,
  ibans TEXT,                          -- JSON list of valid IBANs from PaymentMeans
  issues TEXT,                         -- JSON list of strings
  raw_path TEXT NOT NULL, xml_sha TEXT, parse_error TEXT,
  status TEXT NOT NULL CHECK (status IN ('open','paid','skipped','linked','credit','duplicate')),
  item_id TEXT,                        -- payments row when the user added it to the payment file ('linked')
  paid_at TEXT, paid_note TEXT, paid_tx TEXT,
  created_at TEXT NOT NULL
, party_address TEXT, supplier_tax_id TEXT);

CREATE TABLE emag_attachments (
  invoice_id TEXT PRIMARY KEY, channel TEXT NOT NULL, company TEXT NOT NULL, external_id TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
  sent_at INTEGER
);

CREATE TABLE emag_context (id TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE emag_decisions (id TEXT PRIMARY KEY, reason TEXT NOT NULL);

CREATE TABLE emag_orders (
  channel TEXT NOT NULL, company TEXT NOT NULL, external_id TEXT NOT NULL, id TEXT NOT NULL UNIQUE,
  reason TEXT, PRIMARY KEY(channel,company,external_id)
);

CREATE TABLE history (  -- invoices dated before the firm's start in Fiscalm, imported (finrec.history):
  company TEXT NOT NULL, xml_sha TEXT NOT NULL,  -- kept for reports and the series, never matched, paid or reminded
  direction TEXT NOT NULL CHECK (direction IN ('issued','received')),
  doc_type TEXT NOT NULL, number TEXT NOT NULL, issue_date TEXT NOT NULL, due_date TEXT, currency TEXT NOT NULL,
  total TEXT NOT NULL,                 -- what the XML says is payable; no paid state
  party_name TEXT, party_cui TEXT, party_tax_id TEXT, billing_ref TEXT,
  ibans TEXT,                          -- received: the supplier's IBANs on the XML, JSON (matched as rivals only)
  raw_path TEXT NOT NULL, imported_at TEXT NOT NULL,
  PRIMARY KEY (company, xml_sha)
);

CREATE TABLE integrations (  -- Integrări: plain fields only; secrets live in files (finrec.integrations)
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, config TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE invoice_clients (
  id TEXT PRIMARY KEY, company TEXT NOT NULL, identity TEXT, data TEXT NOT NULL, UNIQUE(company, identity)
);

CREATE TABLE invoice_lines (
  invoice_id TEXT NOT NULL, position INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(invoice_id,position)
);

CREATE TABLE invoice_rates (
  currency TEXT NOT NULL, invoice_date TEXT NOT NULL, rate TEXT NOT NULL, published TEXT NOT NULL,
  PRIMARY KEY(currency,invoice_date)
);

CREATE TABLE invoice_recurrence_periods (
  recurrence TEXT NOT NULL, period TEXT NOT NULL, draft_id TEXT NOT NULL,
  PRIMARY KEY(recurrence,period)
);

CREATE TABLE invoice_recurrences (
  id TEXT PRIMARY KEY, company TEXT NOT NULL, client_id TEXT NOT NULL, series TEXT NOT NULL,
  template TEXT NOT NULL, day INTEGER NOT NULL CHECK(day BETWEEN 1 AND 31),
  due_days INTEGER NOT NULL CHECK(due_days BETWEEN 0 AND 365), start_period TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)), reason TEXT,
  last_period TEXT, skipped_periods INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE invoice_refund_drafts (
  draft_id TEXT PRIMARY KEY, company TEXT NOT NULL, original_id TEXT NOT NULL, positions TEXT NOT NULL
);

CREATE TABLE invoice_reversals (
  original_id TEXT NOT NULL, position INTEGER NOT NULL, storno_id TEXT NOT NULL,
  PRIMARY KEY(original_id,position)
);

CREATE TABLE invoice_sellers (company TEXT PRIMARY KEY, data TEXT NOT NULL);

CREATE TABLE invoice_sent_links (message_id TEXT NOT NULL, invoice_id TEXT NOT NULL, environment TEXT NOT NULL, PRIMARY KEY(message_id,environment));

CREATE TABLE invoice_series (
  company TEXT NOT NULL, code TEXT NOT NULL, start_number INTEGER NOT NULL CHECK(start_number>0),
  next_number INTEGER NOT NULL CHECK(next_number>=start_number), PRIMARY KEY(company,code)
);

CREATE TABLE invoice_settings (company TEXT PRIMARY KEY, auto_upload INTEGER NOT NULL CHECK(auto_upload IN (0,1)));

CREATE TABLE invoice_submissions (
  invoice_id TEXT NOT NULL, environment TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('uploading','unknown','uploaded','accepted','rejected')),
  upload_id TEXT, download_id TEXT, errors TEXT NOT NULL DEFAULT '[]', raw_path TEXT, xml_sha TEXT NOT NULL, claimed_at REAL, PRIMARY KEY(invoice_id,environment)
);

CREATE TABLE invoices (
  id TEXT PRIMARY KEY, company TEXT NOT NULL, series TEXT NOT NULL, number INTEGER,
  status TEXT NOT NULL CHECK(status IN ('draft','issued','storno')), data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(company,series,number),
  CHECK((status='draft' AND number IS NULL) OR (status!='draft' AND number IS NOT NULL))
);

CREATE TABLE issued (    -- invoices the companies ISSUED through ANAF e-Factura (sent messages)
  message_id TEXT PRIMARY KEY,         -- ANAF download id of the sent message: downloaded once
  company TEXT NOT NULL,
  upload_id TEXT, listed_at TEXT,
  doc_type TEXT, number TEXT, issue_date TEXT, due_date TEXT, currency TEXT, total TEXT,
  client_name TEXT, client_cui TEXT,
  billing_ref TEXT,                    -- credit notes: number of the invoice they correct
  issues TEXT,                         -- JSON list of strings
  raw_path TEXT NOT NULL, xml_sha TEXT, parse_error TEXT,
  status TEXT NOT NULL CHECK (status IN ('open','collected','marked','credit','duplicate')),
  collected_at TEXT, collected_note TEXT,
  collected_tx TEXT,                   -- the statement credit that collected it (also in settled_tx)
  collected_from_iban TEXT,            -- the client's IBAN on that credit: identifies its next payments
  created_at TEXT NOT NULL
, collected_into_iban TEXT, party_address TEXT, client_tax_id TEXT);

CREATE TABLE mail_seen (  -- messages a mail account's reading folder showed (finrec.imapread): read once
  account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
  message_id TEXT NOT NULL,            -- the Message-ID, or sha256:<digest> of the message without one
  sender TEXT, subject TEXT, received TEXT, result TEXT NOT NULL, at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,  -- failed attempts while result='error' (retried up to imapread.ATTEMPTS)
  PRIMARY KEY (account, folder, uidvalidity, uid)
);

CREATE TABLE mails (
  mail_key TEXT PRIMARY KEY,   -- account|mailbox|message_id
  kind TEXT, subject TEXT, date_received TEXT, handled_at TEXT, result TEXT
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE packs (
  company TEXT NOT NULL,
  month TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('built','composed','sent')),
  zip_path TEXT, updated_at TEXT, note TEXT,
  PRIMARY KEY (company, month)
);

CREATE TABLE partner_anaf (
    cui TEXT PRIMARY KEY, attempted_at TEXT NOT NULL, status TEXT, error TEXT
);

CREATE TABLE payments (
  item_id TEXT PRIMARY KEY,
  company TEXT NOT NULL,
  kind TEXT NOT NULL,
  month TEXT NOT NULL,
  payee_name TEXT, payee_iban TEXT, payee_bic TEXT, fiscal_code TEXT,
  amount TEXT NOT NULL,
  details TEXT, issue_date TEXT, doc_ref TEXT,
  source_path TEXT, source_sha TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','exported','paid','skipped')),
  csv_path TEXT, exported_at TEXT,
  paid_at TEXT, paid_note TEXT,
  created_at TEXT NOT NULL
, source_iban TEXT);

CREATE TABLE receipt_uploads (
 sha TEXT PRIMARY KEY REFERENCES documents(sha), link_id TEXT NOT NULL REFERENCES upload_links(id),
 uploaded_at TEXT NOT NULL, file_type TEXT NOT NULL
);

CREATE TABLE settled_tx (  -- statement debits that already paid something: one debit, one invoice
  tx_key TEXT PRIMARY KEY, what TEXT NOT NULL, at TEXT NOT NULL
);

CREATE TABLE settlement_confirmations (
  report_id TEXT PRIMARY KEY REFERENCES settlement_reports(id), company TEXT NOT NULL,
  payer_id TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL
);

CREATE TABLE settlement_credits (
  report_id TEXT PRIMARY KEY REFERENCES settlement_reports(id), credit TEXT NOT NULL,
  invoice_count INTEGER NOT NULL
);

CREATE TABLE settlement_fees (
  report_id TEXT PRIMARY KEY REFERENCES settlement_reports(id), company TEXT NOT NULL,
  amount TEXT NOT NULL, currency TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'settlement_fee'
);

CREATE TABLE settlement_lines (
  report_id TEXT NOT NULL REFERENCES settlement_reports(id), position INTEGER NOT NULL,
  invoice_id TEXT NOT NULL REFERENCES issued(message_id), PRIMARY KEY(report_id,position)
);

CREATE TABLE settlement_reports (
  id TEXT PRIMARY KEY, company TEXT NOT NULL, source TEXT NOT NULL, report_key TEXT NOT NULL,
  digest TEXT NOT NULL, raw BLOB NOT NULL, data TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', error TEXT, tx_key TEXT UNIQUE
);

CREATE TABLE statement_txs (  -- every transaction of a company's stored statements: what a statement key in
  tx_key TEXT PRIMARY KEY,                  -- settled_tx or efactura.paid_tx is checked against (Ledger.paid_proofs)
  company TEXT NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('debit','credit')),
  tx TEXT                                   -- the line as Tranzacții shows it: statements.Tx.to_dict() as JSON
, day TEXT, amount TEXT, source TEXT, iban TEXT);

CREATE TABLE supplier_iban_confirmations (
  company TEXT NOT NULL, supplier_cui TEXT NOT NULL, iban TEXT NOT NULL,
  at TEXT NOT NULL, message_id TEXT NOT NULL, created_by TEXT, revoked_at TEXT, revoked_by TEXT,
  PRIMARY KEY(company,supplier_cui,iban,at)
);

CREATE TABLE supplier_ibans (
  company TEXT NOT NULL, supplier_cui TEXT NOT NULL, iban TEXT NOT NULL, proof TEXT NOT NULL,
  PRIMARY KEY(company, supplier_cui, iban)
);

CREATE TABLE upload_attempts (
 id INTEGER PRIMARY KEY, link_id TEXT NOT NULL, company TEXT NOT NULL, at TEXT NOT NULL,
 files INTEGER NOT NULL, bytes INTEGER NOT NULL
);

CREATE TABLE upload_links (
 id TEXT PRIMARY KEY, company TEXT NOT NULL, label TEXT NOT NULL, secret_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT, max_files INTEGER,
 uploads INTEGER NOT NULL DEFAULT 0, last_used_at TEXT
);

CREATE TABLE upload_results (
 id INTEGER PRIMARY KEY, sha TEXT NOT NULL, link_id TEXT NOT NULL, at TEXT NOT NULL, duplicate INTEGER NOT NULL
);

CREATE TABLE users (
  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','member','readonly')),
  all_firms INTEGER NOT NULL DEFAULT 1, password_hash TEXT, totp_version TEXT, totp_step INTEGER,
  webauthn_handle BLOB NOT NULL UNIQUE, legacy_name TEXT UNIQUE,
  created_at TEXT NOT NULL, created_by TEXT, disabled_at TEXT, disabled_by TEXT,
  locked_until TEXT, failures INTEGER NOT NULL DEFAULT 0, failures_since TEXT);

CREATE INDEX efactura_paid_tx ON efactura(paid_tx) WHERE paid_tx IS NOT NULL;

CREATE UNIQUE INDEX integrations_one_anaf ON integrations(kind) WHERE kind='anaf';

CREATE UNIQUE INDEX integrations_one_default ON integrations(kind) WHERE is_default=1;

CREATE INDEX mail_seen_message ON mail_seen(account, message_id);

CREATE INDEX receipt_upload_rate ON receipt_uploads(link_id, uploaded_at);

CREATE INDEX settled_tx_what ON settled_tx(what);

CREATE INDEX settlement_confirmed_payer ON settlement_confirmations(company,payer_id);

CREATE UNIQUE INDEX settlement_digest ON settlement_reports(company,digest) WHERE status!='withdrawn';

CREATE UNIQUE INDEX settlement_identity ON settlement_reports(company,json_extract(data,'$.payer_id'),report_key) WHERE status!='withdrawn';

CREATE UNIQUE INDEX settlement_invoice_proof ON settlement_lines(invoice_id);

CREATE INDEX upload_attempt_time ON upload_attempts(at);
