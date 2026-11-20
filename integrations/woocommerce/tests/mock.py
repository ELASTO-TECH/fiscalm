"""Contract fixture, not an implementation of fiscalm's accounting rules."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import hmac
import hashlib
import time
import urllib.request

orders = {}
payloads = {}
calls = []
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/calls':
            return self.reply(200, calls)
        if self.headers.get('Authorization') != 'Bearer ' + os.environ['WC_TEST_TOKEN']:
            return self.reply(401, {})
        if '/invoicing/issued/' in self.path and not self.path.endswith('/pdf'):
            key = self.path.rsplit('/', 1)[-1].removeprefix('issued-')
            return self.reply(200, {'series': 'TEST', 'number': 1, 'version': orders.get(key, {}).get('invoice_version', 1)})
        if self.path.endswith('/pdf'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.end_headers()
            return self.wfile.write(b'%PDF-1.4\n%%EOF\n')
        key = self.path.rsplit('/', 1)[-1]
        self.reply(200, orders.get(key, {}))

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + os.environ['WC_TEST_TOKEN']:
            return self.reply(401, {})
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append({'path': self.path, 'body': body})
        if self.path == '/invoice-version':
            orders[body['external_id']]['invoice_version'] = body['version']
            return self.reply(200, {'ok': True})
        if self.path == '/emit':
            key = body['external_id']
            orders[key].update(invoice_id='issued-' + key, invoice_number='TEST-1', spv_status='accepted', status='issued')
            raw = json.dumps(body).encode()
            stamp = str(int(time.time()))
            signature = hmac.new(os.environ['WC_TEST_WEBHOOK_SECRET'].encode(), stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
            request = urllib.request.Request('http://shop/?rest_route=/fiscalm/v1/webhook', raw, headers={'Content-Type': 'application/json', 'X-Fiscalm-Timestamp': stamp, 'X-Fiscalm-Signature': 'sha256=' + signature})
            with urllib.request.urlopen(request, timeout=30) as response:
                return self.reply(response.status, {'ok': True})
        if self.path.endswith('/orders'):
            key = body['external_id']
            if key in payloads and payloads[key] != body and orders[key]['status'] != 'draft':
                return self.reply(409, {'error': {'code': 'order_changed'}})
            payloads[key] = body
            orders.setdefault(key, dict(body, id=key, draft_id='draft-' + key, invoice_id=None, status='draft', draft_version=1))
            return self.reply(200, dict(ok=True, message='Comanda a fost salvată.', **orders[key]))
        key = self.path.split('/')[-2]
        if key not in orders:
            return self.reply(404, {})
        if self.path.endswith('/refund') and (orders[key]['status'] != 'issued' or any(type(n) is not int for n in body.get('lines', []))):
            return self.reply(409, {'error': {'code': 'order_changed'}})
        if self.path.endswith('/awb'):
            orders[key]['awb'] = body
        if self.path.endswith('/cancel'):
            orders[key]['status'] = 'cancelled'
        self.reply(200, {'ok': True})

    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
