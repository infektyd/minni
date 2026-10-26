import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  ENVELOPE_PRIORITY_ORDER,
  serializeEnvelope,
  stableStringify,
  wrapEnvelope,
} from "../dist/agent_envelope.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(__dirname, "..");
const CLAUDE_HOOK = path.join(PLUGIN_ROOT, "dist", "hook.js");

test("serializeEnvelope orders top-level keys by ENVELOPE_PRIORITY_ORDER, places audit_tail last, and keeps nested keys alphabetical", () => {
  assert.ok(Array.isArray(ENVELOPE_PRIORITY_ORDER), "ENVELOPE_PRIORITY_ORDER must be exported");
  assert.equal(ENVELOPE_PRIORITY_ORDER[0], "contract");
  assert.equal(
    ENVELOPE_PRIORITY_ORDER[ENVELOPE_PRIORITY_ORDER.length - 1],
    "audit_tail",
  );

  const body = {
    audit_tail: [{ z_field: 1, a_field: 2 }],
    zzz_custom: true,
    aaa_custom: false,
    recall: { ok: true, results: [] },
    active_thread: { plan_id: "p1", rev: 1 },
    layer1_shelf: { ok: true, content: "shelf" },
    identity_body: { ok: true, content: "id" },
    contract: "contract-text",
  };

  const json = serializeEnvelope(body);
  assert.ok(
    json.startsWith('{"contract":"contract-text"'),
    `expected serialized envelope to start with {"contract":..., got: ${json.slice(0, 60)}`,
  );

  const parsedKeys = Object.keys(JSON.parse(json));
  assert.deepEqual(parsedKeys, [
    "contract",
    "identity_body",
    "layer1_shelf",
    "active_thread",
    "recall",
    "aaa_custom",
    "zzz_custom",
    "audit_tail",
  ]);

  // Nested objects inside audit_tail must still be sorted alphabetically via stableStringify
  assert.ok(
    json.includes('{"a_field":2,"z_field":1}'),
    `expected nested object keys to remain alphabetically sorted: ${json}`,
  );

  // stableStringify itself must remain strictly alphabetical for plan digest stability
  assert.equal(
    stableStringify({ contract: "c", audit_tail: [] }),
    '{"audit_tail":[],"contract":"c"}',
  );
});

test("wrapEnvelope uses ENVELOPE_PRIORITY_ORDER by default and supports legacyKeySort", () => {
  const body = {
    audit_tail: [],
    contract: "c",
    identity_body: { ok: true },
  };
  const wrapped = wrapEnvelope({
    event: "SessionStart",
    agent: "claudecode",
    body,
    budget: 4000,
  });
  const m = wrapped.match(/<minni:context[^>]*>\n([\s\S]*?)\n<\/minni:context>/);
  assert.ok(m);
  assert.ok(m[1].startsWith('{"contract":"c","identity_body":{"ok":true},"audit_tail":[]}'));

  const legacy = wrapEnvelope({
    event: "SessionStart",
    agent: "claudecode",
    body,
    budget: 4000,
    legacyKeySort: true,
  });
  const mLegacy = legacy.match(/<minni:context[^>]*>\n([\s\S]*?)\n<\/minni:context>/);
  assert.ok(mLegacy);
  assert.ok(mLegacy[1].startsWith('{"audit_tail":[],"contract":"c"'));
});

test(
  "SessionStart hook emits <minni:context> starting with {\"contract\": and ending with \"audit_tail\":",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(os.tmpdir(), "sm-s2a-prio-"));
    t.after(() => rm(root, { recursive: true, force: true }));

    const home = path.join(root, "home");
    const vault = path.join(root, "claudecode-vault");
    await mkdir(home, { recursive: true });
    await mkdir(path.join(vault, "layer1"), { recursive: true });
    await writeFile(path.join(vault, "layer1", "core.md"), "# Core\n", "utf8");

    const res = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [CLAUDE_HOOK, "SessionStart"], {
        cwd: PLUGIN_ROOT,
        env: {
          ...process.env,
          MINNI_HOME: home,
          MINNI_CLAUDECODE_VAULT_PATH: vault,
          MINNI_SOCKET_PATH: path.join(root, "missing.sock"),
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
      child.stdin.write(JSON.stringify({ session_id: "sess-prio", cwd: root }));
      child.stdin.end();
    });

    assert.equal(res.code, 0, res.stderr);
    const out = JSON.parse(res.stdout);
    const ctx = out?.hookSpecificOutput?.additionalContext ?? "";
    const m = ctx.match(/<minni:context[^>]*>\n([\s\S]*?)\n<\/minni:context>/);
    assert.ok(m, "expected <minni:context> block");
    const rawJson = m[1];
    assert.ok(
      rawJson.startsWith('{"contract":'),
      `SessionStart JSON must begin with {"contract":, got: ${rawJson.slice(0, 60)}`,
    );
    const keys = Object.keys(JSON.parse(rawJson));
    assert.equal(keys[0], "contract");
    assert.equal(keys[keys.length - 1], "audit_tail");
  },
);
