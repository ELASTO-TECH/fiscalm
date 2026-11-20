"""Synthetic commerce contract double; never contacts an external service."""
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

orders = {}
retry_attempts = 0

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if self.path == '/healthz':
            return self.reply(200, {'ok': True})
        if self.headers.get('Authorization') != 'Bearer ' + os.environ['E2E_TOKEN']:
            return self.reply(401, {})
        key = self.path.rsplit('/', 1)[-1]
        if key in orders:
            return self.reply(200, orders[key]['result'] | {'body': orders[key]['body']})
        self.reply(404, {})

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + os.environ['E2E_TOKEN']:
            return self.reply(401, {})
        global retry_attempts
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith(('/awb', '/cancel', '/refund')):
            key = self.path.split('/')[-2]
            if key not in orders:
                return self.reply(404, {})
            operation = self.path.split('/')[-1]
            orders[key]['result'][operation + '_calls'] = orders[key]['result'].get(operation + '_calls', 0) + 1
            if operation == 'cancel':
                return self.reply(503, {})
            return self.reply(200, {'ok': True, 'id': key})
        if self.path != '/api/v1/companies/NORD/orders':
            return self.reply(404, {})
        if body.get('external_id') == 'retry-once':
            retry_attempts += 1
            if retry_attempts == 1:
                return self.reply(503, {})
        required = {'channel', 'external_id', 'placed_at', 'currency', 'prices_include_vat', 'buyer', 'lines', 'payment'}
        if not required <= body.keys() or not body['lines'] or type(body.get('payment', {}).get('paid')) is not bool:
            return self.reply(400, {})
        key = hashlib.sha256((body['channel'] + ':' + body['external_id']).encode()).hexdigest()
        result = {'ok': True, 'message': 'Comanda a fost primită.', 'id': key, 'draft_id': key, 'status': 'draft'}
        orders[key] = {'body': body, 'result': result}
        self.reply(200, result)

HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
