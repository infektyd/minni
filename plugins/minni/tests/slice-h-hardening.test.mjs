// Slice H security hardening regression tests (finding X4 — console auth).
// X1/X2/X3 (propagate.py) are covered by the Python harness in
// skills/minni-install/scripts/test_propagate_antigravity.py.
//
// X4: the sensitive console routes (/api/status, /api/prepare-*, /api/candidates)
// must require a bearer token UNCONDITIONALLY. Before the fix they were open
// whenever MINNI_CONSOLE_TOKEN was unset (the default). The auth decision is
// made at module import time (the token const is evaluated then), so we exercise
// it in a fresh subprocess with MINNI_CONSOLE_TOKEN removed — the module then
// auto-generates a token unknown to the caller, and every vault route must 403.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/**
 * Boot a UI server in a child process with a scrubbed environment, hit one
 * route with no Authorization header, and return the HTTP status. The child
 * prints a single JSON line `{"port":N}` once listening; we then fetch and
 * report the status back through its exit.
 */
function probeUnauthenticated(method, pathname, { withToken = false } = {}) {
  return new Promise((resolve, reject) => {
    const script = `
      import { createServer as createNetServer } from "node:net";
      import { createUiServer } from ${JSON.stringify(path.join(PLUGIN_ROOT, "dist", "ui-server.js"))};
      const freePort = await new Promise((resolve) => {
        const s = createNetServer();
        s.listen(0, "127.0.0.1", () => {
          const p = s.address().port;
          s.close(() => resolve(p));
        });
      });
      const app = createUiServer({
        host: "127.0.0.1",
        port: freePort,
        staticRoot: ${JSON.stringify(path.join(PLUGIN_ROOT, "frontend"))},
        vaultPath: "/tmp/slice-h-vault",
        status: async () => ({ vault: { path: "/tmp/slice-h-vault", exists: true }, socket: { ok: true }, afm: { ok: true }, audit: { entries: 0 } }),
        prepareTask: async () => ({ task: "x" }),
        prepareOutcome: async () => ({ ok: true }),
      });
      await app.start();
      const baseUrl = "http://127.0.0.1:" + freePort;
      const res = await fetch(baseUrl + ${JSON.stringify(pathname)}, {
        method: ${JSON.stringify(method)},
        headers: ${method === "POST" ? '{ "Content-Type": "application/json" }' : "{}"},
        ${method === "POST" ? "body: JSON.stringify({ task: 'x' })," : ""}
      });
      console.log(JSON.stringify({ status: res.status }));
      await app.close();
      process.exit(0);
    `;
    const env = { ...process.env };
    if (!withToken) delete env.MINNI_CONSOLE_TOKEN;
    const child = spawn(process.execPath, ["--input-type=module", "-e", script], {
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    let err = "";
    child.stdout.on("data", (c) => (out += c));
    child.stderr.on("data", (c) => (err += c));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`probe timed out; stderr=${err}`));
    }, 30_000);
    child.on("close", () => {
      clearTimeout(timer);
      const line = out.trim().split("\n").at(-1) ?? "";
      try {
        resolve(JSON.parse(line).status);
      } catch {
        reject(new Error(`unparseable probe output: ${out} / ${err}`));
      }
    });
  });
}

test("X4: /api/status requires auth even when MINNI_CONSOLE_TOKEN is unset", async () => {
  const status = await probeUnauthenticated("GET", "/api/status");
  assert.equal(status, 403, "tokenless default must NOT expose /api/status");
});

test("X4: /api/prepare-task requires auth even when MINNI_CONSOLE_TOKEN is unset", async () => {
  const status = await probeUnauthenticated("POST", "/api/prepare-task");
  assert.equal(status, 403, "tokenless default must NOT expose /api/prepare-task");
});

test("X4: /api/candidates requires auth even when MINNI_CONSOLE_TOKEN is unset", async () => {
  const status = await probeUnauthenticated("GET", "/api/candidates");
  assert.equal(status, 403, "tokenless default must NOT expose /api/candidates");
});

test("X4: /api/health stays open (liveness probe, no vault data)", async () => {
  const status = await probeUnauthenticated("GET", "/api/health");
  assert.equal(status, 200, "health must remain reachable without a token");
});
