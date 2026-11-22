// The Tether protocol in Node (~70 lines): shows that the protocol does not depend on Python and is a starting point for embedding in
// Electron/Node apps (persistent vm context, token, registry file).
// Usage: node example_node.js [app_name]
const net = require('net'), fs = require('fs'), os = require('os'), path = require('path'), vm = require('vm'), crypto = require('crypto');

const APP = process.argv[2] || 'example-node';
const DIR = path.join(process.env.TETHER_HOME || path.join(os.homedir(), '.tether'), 'apps');
const TOKEN = crypto.randomBytes(16).toString('hex');
const LIMIT = 20000;
let output = [];
const fresh = () => vm.createContext({ console: { log: (...a) => output.push(a.map(String).join(' ')) } });
let ctx = fresh();

function run(req) {
  output = [];
  if (typeof req.code !== 'string') return { ok: false, error: { type: 'request', message: 'code required' } };
  const t0 = Date.now();
  let result, error;
  try { result = vm.runInContext(req.code, ctx, { filename: '<tether>' }); }
  catch (e) { error = { type: e.name, message: String(e.message).slice(0, 2000), trace: String(e.stack).slice(-4000) }; }
  let text = output.join('\n') + (output.length ? '\n' : '');
  const truncated = text.length > LIMIT;
  if (truncated) text = text.slice(0, LIMIT * 0.6) + '\n[... omitted ...]\n' + text.slice(-LIMIT * 0.4);
  const r = { ok: !error, output: text, truncated, ms: Date.now() - t0 };
  if (error) r.error = error;
  else if (result !== undefined) { r.result = result; r.result_truncated = false; }
  return r;
}

function handle(req) {
  if (String(req.token) !== TOKEN) return { ok: false, error: { type: 'token', message: 'invalid token; re-read the registry file' } };
  switch (req.op) {
    case 'info':
      return { ok: true, tether: 1, app: APP, version: process.version, mode: 'background', lang: 'javascript', pid: process.pid,
        port: server.address().port, busy: false, queue: 0, queue_wait_s: 0, running: null,
        capabilities: ['run', 'reset', 'search', 'close'], hint: 'javascript (node vm); console.log gives the output; the last expression is the value' };
    case 'run': return run(req);
    case 'search': return { ok: true, text: 'example-node: no API; explore with code (Object.keys(globalThis))' };
    case 'reset': ctx = fresh(); return { ok: true };
    case 'close': return { ok: true, _close: true };
    default: return { ok: false, error: { type: 'unsupported', message: `${req.op} is not supported` } };
  }
}

const server = net.createServer((sock) => {
  let buffer = '';
  sock.setEncoding('utf8');
  sock.on('error', () => {});
  sock.on('data', (chunk) => {
    buffer += chunk;
    let i;
    while ((i = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, i); buffer = buffer.slice(i + 1);
      let r;
      let id = null;
      try { const req = JSON.parse(line); id = req.id; r = handle(req); }
      catch (e) { r = { ok: false, error: { type: 'protocol', message: 'could not parse JSON: ' + e.message } }; }
      const close = r._close; delete r._close;
      r.id = id;
      sock.write(JSON.stringify(r) + '\n', () => { if (close) process.exit(0); });
    }
  });
});

const FILE = path.join(DIR, `${APP}-${process.pid}.json`);
server.listen(0, '127.0.0.1', () => {
  fs.mkdirSync(DIR, { recursive: true });
  fs.writeFileSync(FILE + '.tmp', JSON.stringify({ tether: 1, app: APP, pid: process.pid, port: server.address().port, token: TOKEN,
    version: process.version, mode: 'background', lang: 'javascript', started: new Date().toISOString().slice(0, 19) }));
  fs.renameSync(FILE + '.tmp', FILE);
});
process.on('exit', () => { try { fs.unlinkSync(FILE); } catch (e) {} });
