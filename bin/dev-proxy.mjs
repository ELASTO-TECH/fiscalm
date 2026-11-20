// Loopback development only: one HTTPS origin for Next.js and the real cookie-authenticated engine.
import { lstatSync, readFileSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import path from 'node:path';

const persistent = process.env.FISCALM_DEV_PERSISTENT === '1';
const root = process.env.FISCALM_DEV_ROOT;
// bin/dev writes this marker into the directory it created; refuse any other directory.
const owned = () => { try { return lstatSync(path.join(root, '.fiscalm-dev')).isFile(); } catch { return false; } };
if (!persistent && (!root || !owned())) {
  throw new Error('Start this helper through bin/dev engine.');
}
const ports = [process.env.FISCALM_DEV_PORT ?? '3000', process.env.FISCALM_DEV_APP_PORT ?? '3001', process.env.FISCALM_DEV_ENGINE_PORT ?? '8765'].map(Number);
if (ports.some(port => !Number.isInteger(port) || port < 1 || port > 65535) || new Set(ports).size !== 3) {
  throw new Error('Development ports must be distinct integers between 1 and 65535.');
}
const [publicPort, appPort, enginePort] = ports;
const origin = persistent ? process.env.FISCALM_PUBLIC_URL : `https://localhost:${publicPort}`;
const parsed = new URL(origin);
if (parsed.protocol !== 'https:' || parsed.origin !== origin) throw new Error('An exact HTTPS origin is required.');
const authority = parsed.host;
const server = persistent ? http.createServer() : https.createServer({
  key: readFileSync(path.join(root, 'key.pem')),
  cert: readFileSync(path.join(root, 'cert.pem')),
});

function admitted(req) {
  return req.headers.host === authority && req.rawHeaders.filter((_, i) => i % 2 === 0 && req.rawHeaders[i].toLowerCase() === 'host').length === 1
    && req.url.startsWith('/') && !req.url.startsWith('//');
}
function headers(req, engine) {
  const result = { ...req.headers };
  for (const key of Object.keys(result)) {
    if (key.startsWith('x-forwarded-') || key === 'forwarded') delete result[key];
  }
  result.host = authority;
  if (engine) {
    result['x-forwarded-host'] = authority;
    result['x-forwarded-proto'] = 'https';
    result['x-forwarded-for'] = req.socket.remoteAddress;
  } else {
    delete result.cookie;
  }
  return result;
}

server.on('request', (req, res) => {
  if (!admitted(req)) { res.writeHead(404); res.end(); return; }
  let url;
  try { url = new URL(req.url, origin); }
  catch { res.writeHead(404); res.end(); return; }
  const engine = /^\/(?:auth(?:\/|$)|web\/(?!$)|api\/v1(?:\/|$)|u(?:\/|$)|anaf(?:\/|$)|hooks\/shopify\/|bank\/return$|emag-pdf\/|healthz$|manifest\.webmanifest$)/.test(url.pathname);
  const target = !engine && ['/setup', '/web', '/web/'].includes(url.pathname) ? `/${url.search}` : req.url;
  const upstream = http.request({ hostname: '127.0.0.1', port: engine ? enginePort : appPort, path: target, method: req.method, headers: headers(req, engine) }, response => {
    res.writeHead(response.statusCode, response.headers);
    response.pipe(res);
  });
  upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
  req.on('aborted', () => upstream.destroy());
  req.pipe(upstream);
});

// Next.js HMR only; app/API websocket routes are not exposed by this proxy.
server.on('upgrade', (req, socket, head) => {
  if (!admitted(req) || !req.url.startsWith('/_next/')) { socket.destroy(); return; }
  const upstream = net.connect(appPort, '127.0.0.1', () => {
    const lines = Object.entries(headers(req, false)).flatMap(([key, value]) => Array.isArray(value) ? value.map(v => `${key}: ${v}`) : [`${key}: ${value}`]);
    upstream.write(`${req.method} ${req.url} HTTP/1.1\r\n${lines.join('\r\n')}\r\n\r\n`);
    if (head.length) upstream.write(head);
    socket.pipe(upstream).pipe(socket);
  });
  upstream.on('error', () => socket.destroy());
  socket.on('error', () => upstream.destroy());
  socket.on('close', () => upstream.destroy());
});
server.listen(publicPort, '127.0.0.1', () => console.log(persistent ? 'Development proxy ready.' : 'Development HTTPS ready.'));
