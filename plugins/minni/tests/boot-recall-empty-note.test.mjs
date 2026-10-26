import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { buildBootRecallSlice } from "../dist/daemon_transport.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(__dirname, "..");
const CLAUDE_HOOK = path.join(PLUGIN_ROOT, "dist", "hook.js");

test("buildBootRecallSlice sets recall.note when both results and episodic are empty", () => {
  const emptySlice = buildBootRecallSlice(
    { results: [], count: 0, episodic: [], episodic_count: 0 },
    "claudecode",
  );
  assert.equal(emptySlice.ok, true);
  assert.deepEqual(emptySlice.results, []);
  assert.deepEqual(emptySlice.episodic, []);
  assert.equal(emptySlice.note, "no boot recall results; call minni_recall");

  // Non-empty episodic means recall was NOT empty
  const episodicOnly = buildBootRecallSlice(
    { results: [], count: 0, episodic: [{ id: "e1", content: "event" }], episodic_count: 1 },
    "claudecode",
  );
  assert.equal(episodicOnly.note, undefined);

  // Non-empty results means recall was NOT empty
  const resultsOnly = buildBootRecallSlice(
    {
      results: [{ wikilink: "[[a]]", title: "A", text: "body", layer: "project", score: 1.0 }],
      count: 1,
      episodic: [],
      episodic_count: 0,
    },
    "claudecode",
  );
  assert.equal(resultsOnly.note, undefined);
});

test(
  "SessionStart emits recall.note when daemon returns zero results and zero episodic items",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(os.tmpdir(), "sm-s2a-empty-note-"));
    t.after(() => rm(root, { recursive: true, force: true }));

    const home = path.join(root, "home");
    const vault = path.join(root, "claudecode-vault");
    await mkdir(home, { recursive: true });
    await mkdir(path.join(vault, "layer1"), { recursive: true });
    await writeFile(path.join(vault, "layer1", "core.md"), "# Core\n", "utf8");

    const sockPath = path.join(root, "d.sock");
    const server = net.createServer((conn) => {
      let buf = "";
      conn.on("data", (chunk) => {
        buf += chunk.toString("utf8");
        let nl;
        while ((nl = buf.indexOf("\n")) !== -1) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          const req = JSON.parse(line);
          let result = {};
          if (req.method === "read") {
            result = { context: "# Identity\n", agent_id: "claudecode", backend: "sqlite" };
          } else if (req.method === "search" || req.method === "recall") {
            result = { results: [], count: 0, episodic: [], episodic_count: 0 };
          } else if (req.method === "stale_beliefs") {
            result = { status: "checked_no_match", events: [], count: 0 };
          } else if (req.method === "handoff_leases" || req.method === "handoff_list") {
            result = { leases: [], handoffs: [], count: 0 };
          }
          conn.write(JSON.stringify({ jsonrpc: "2.0", id: req.id, result }) + "\n");
        }
      });
    });
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(sockPath, resolve);
    });
    t.after(() => new Promise((resolve) => server.close(() => resolve())));

    const res = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [CLAUDE_HOOK, "SessionStart"], {
        cwd: PLUGIN_ROOT,
        env: {
          ...process.env,
          MINNI_HOME: home,
          MINNI_CLAUDECODE_VAULT_PATH: vault,
          MINNI_SOCKET_PATH: sockPath,
          MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
          MINNI_BYPASS_AUDIT_LIMIT: "true",
          MINNI_CLAUDECODE_HOOKS: "on",
        },
        stdio: ["pipe", "pipe", "pipe"],
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (d) => (stdout += d.toString("utf8")));
      child.stderr.on("data", (d) => (stderr += d.toString("utf8")));
      child.on("error", reject);
      child.on("close", (code) => resolve({ code, stdout, stderr }));
      child.stdin.write(JSON.stringify({ session_id: "sess-empty-recall", cwd: root }));
      child.stdin.end();
    });

    assert.equal(res.code, 0, res.stderr);
    const out = JSON.parse(res.stdout);
    const ctx = out?.hookSpecificOutput?.additionalContext ?? "";
    const m = ctx.match(/<minni:context[^>]*>\n([\s\S]*?)\n<\/minni:context>/);
    assert.ok(m);
    const body = JSON.parse(m[1]);
    assert.equal(body.recall.ok, true);
    assert.deepEqual(body.recall.results, []);
    assert.equal(body.recall.note, "no boot recall results; call minni_recall");
  },
);
