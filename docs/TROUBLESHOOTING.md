# Troubleshooting Minni

This guide records product-level failure modes that can happen when the daemon,
plugins, and agent runtime are updated at different speeds.

## Codex Plugin Reports `Parse Error: Expected HTTP/, RTSP/ or ICE/`

### Symptom

`minni_status`, `minni_recall`, or `minni_learn` can show a socket
failure like:

```text
Parse Error: Expected HTTP/, RTSP/ or ICE/
```

The vault may still write notes, while daemon-backed recall or learn storage
fails.

### Root Cause

This means a client tried to speak HTTP over the Minni daemon Unix socket,
but the live daemon is speaking line-delimited JSON-RPC.

This commonly happens after updating Minni source code while Codex is
still running an older installed plugin cache. The v4 daemon socket protocol is:

```text
JSON.stringify({"jsonrpc":"2.0","id":1,"method":"status","params":{}}) + "\n"
```

It is not:

```text
GET /health HTTP/1.1
```

### Diagnosis

Check the live socket:

```bash
ls -l ~/.minni/run/minnid.sock 2>&1 || true
lsof -U | rg 'minni/run/minnid\.sock|minnid\.sock'
```

Probe JSON-RPC directly:

```bash
node - <<'NODE'
const net = require('node:net');
const socketPath = `${process.env.HOME}/.minni/run/minnid.sock`;
const client = net.createConnection(socketPath);
client.on('connect', () => {
  client.write(JSON.stringify({jsonrpc: '2.0', id: 1, method: 'status', params: {}}) + '\n');
});
client.on('data', (chunk) => {
  console.log(chunk.toString('utf8').split('\n')[0]);
  client.destroy();
});
client.on('error', (error) => {
  console.error(error.message);
  process.exitCode = 1;
});
NODE
```

If that succeeds but the plugin still reports the HTTP parse error, compare the
repo plugin and the installed Codex plugin cache:

```bash
rg -n 'socketRequest|jsonRpcSocketRequest|/health|/learn|/recall' \
  plugins/minni/src \
  ~/.codex/plugins/cache/minni -g '!node_modules'
```

The stale cache usually still contains HTTP fallback calls such as
`socketRequest("GET", "/health")` without JSON-RPC-first helpers.

### Fix

Build and test the repo plugin first:

```bash
cd ~/Projects/Minni/plugins/minni
npm run build
npm test
node dist/cli.js status
```

If the repo build succeeds but Codex still fails, reinstall or resync the Codex
plugin cache so the running MCP server uses the current `dist/` output. Then
restart stale plugin server processes or restart Codex.

On this machine, stale plugin servers can be spotted with:

```bash
ps aux | rg 'minni|dist/server|minnid' | rg -v rg
for pid in $(pgrep -f 'minni.*/dist/server.js'); do
  lsof -p "$pid" -a -d cwd
done
```

Do not restart the daemon as the first fix unless direct JSON-RPC probing fails.
If direct JSON-RPC works, the daemon is healthy and the problem is the client
cache or plugin protocol layer.

### Regression Guard

The plugin test suite should include a Unix-socket JSON-RPC test that proves the
client writes a newline-delimited JSON-RPC request and parses the daemon result.
This prevents a future HTTP-over-socket fallback from becoming the primary path
again.
