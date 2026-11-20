"""An isolated real engine: no live marker, network integrations or real credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import hmac
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

root = Path(tempfile.mkdtemp(prefix='fiscalm-wc-', dir='/scratch'))
for key, suffix in {
    'FISCALM_HOME': 'data',
    'FISCALM_SECRETS': 'secrets',
    'FISCALM_GMAIL_SECRET': 'gmail',
}.items():
    os.environ[key] = str(root / suffix)
os.environ['FISCALM_PUBLIC_URL'] = 'https://fiscalm:8790'
from fiscalm import companies, integrations
from fiscalm.invoicing import engine as invoicing
from fiscalm.api import tokens, users
from fiscalm.api import web
from fiscalm.ledger import Ledger, is_live

ledger = Ledger()
ledger.save_company(companies.company_from(dict(key='NORD', legal_name='Atelier Nord SRL', cui='99999984',
    accounts=[dict(iban='RO00FAKE0000000000000000', currency='RON', pays=True)]), 0))
try:
    invoicing.save_seller(ledger, 'NORD', dict(name='Atelier Nord SRL', cui='99999984',
        country='RO', street='Strada Exemplului 1', city='Cluj-Napoca', county='RO-CJ',
        vat_payer=True, vat_id='RO99999984', iban='RO00FAKE0000000000000000',
        bank='Banca Fictivă', reg_com='J00/0000/2026'))
    invoicing.create_series(ledger, 'NORD', 'TEST', 1)
    channel = integrations.add(ledger, 'shop', 'Synthetic WooCommerce', dict(
        platform='woocommerce', company='NORD', series='TEST', auto_issue='false'))
    owner = users.create(ledger, 'shop-api', 'Shop API', 'member', datetime.now(UTC), actor_id=None)
    token = tokens.create(ledger, 'Synthetic WooCommerce', 'write', user_id=owner)['token']
    assert not is_live()
    connection = Path('/exchange/connection.json')
    connection.write_text(json.dumps(dict(url='http://fiscalm:8790', token=token,
        company='NORD', channel=channel['id'], send_status='processing')))
    connection.chmod(0o600)
finally:
    ledger.close()

class FixtureHandler(BaseHTTPRequestHandler):
    """Private test-only fixture seeding; never part of the production image/server."""
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != '/seed-issued' or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
            self.send_error(403)
            return
        body = json.loads(self.rfile.read(min(4096, int(self.headers.get('Content-Length', 0)))))
        from fiscalm.commerce import orders as commerce
        ledger = Ledger()
        try:
            assert not is_live()
            original = commerce.get(ledger, 'NORD', body['id'])
            invoice = invoicing.invoice(ledger, original['draft_id'], company='NORD')
            if invoice['status'] == 'draft':
                # Seed a synthetic issued fixture without enabling LIVE or the public issue action.
                invoicing.issue(ledger, invoice['id'], date.today().isoformat(), company='NORD', version=invoice['version'])
            assert not is_live()
            data = json.dumps({'ok': True}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(data)
        finally:
            ledger.close()

fixture_server = ThreadingHTTPServer(('0.0.0.0', 8792), FixtureHandler)
Thread(target=fixture_server.serve_forever, daemon=True).start()
web.serve('0.0.0.0', 8790)
