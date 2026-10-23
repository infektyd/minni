// Which vault-search candidate path each process type runs (see
// VaultSearchCandidates in src/vault.ts). The paths return identical results
// (vault-search-candidates-parity.test.mjs); what differs is cost, and that is
// decided per process type:
//   - a hook is a one-shot process: UserPromptSubmit searches once and exits,
//     so it runs "scan" and never builds the prefix index;
//   - the MCP server is long-lived and keeps its cache warm, so it runs
//     "index".
// Both are pinned on the real built entry points, spawned as processes, with
// a preload probe (vault-search-path-probe.mjs) that reports the process's
// search cache on its way out.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROBE = path.join(HERE, "vault-search-path-probe.mjs");
const NOTE_COUNT = 24;

async function seedNotes(vault) {
  const dir = path.join(vault, "wiki", "concepts");
  await mkdir(dir, { recursive: true });
  for (let i = 0; i < NOTE_COUNT; i++) {
    await writeFile(
      path.join(dir, `cache-note-${i}.md`),
      `---\ntitle: Cache note ${i}\nstatus: accepted\n---\n\n# Cache note ${i}\n\nThe cache policy for tests, variant ${i}.\n`,
      "utf8",
    );
  }
}

function probeState(stderr) {
  const line = stderr.split("\n").find((l) => l.startsWith("VAULT_SEARCH_PROBE "));
  assert.ok(line, `the probe reported (stderr: ${stderr.slice(-2000)})`);
  return JSON.parse(line.slice("VAULT_SEARCH_PROBE ".length));
}

test("hook process: UserPromptSubmit searches the vault on the scan path, with no prefix index", async (t) => {
  const fixture = await makeFixture(t, "search-path-hook");
  await seedNotes(fixture.vault);
  await startFakeDaemon(fixture);
  const { stderr, code } = await runHook(
    fixture,
    "UserPromptSubmit",
    { session_id: "search-path-hook", prompt_id: "p1", prompt: "how does the cache policy handle tests" },
    { nodeArgs: ["--import", PROBE], env: { MINNI_TEST_SEARCH_PROBE_VAULT: fixture.vault } },
  );
  assert.equal(code, 0);
  const state = probeState(stderr);
  assert.ok(state, "the hook searched the vault in this process");
  assert.ok(state.notes >= 20, `the cache holds the vault (${state.notes} notes), so the lookup gate was open`);
  assert.equal(state.prefixIndex, false, "a hook process must never build the prefix index");
});

test("MCP server process: minni_recall's vault pre-scan runs on the index path", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-search-path-server-"));
  const home = path.join(root, "home");
  await mkdir(home, { recursive: true });
  await seedNotes(root);
  const child = spawn(
    process.execPath,
    ["--import", PROBE, new URL("../dist/server.js", import.meta.url).pathname],
    {
      env: {
        ...process.env,
        MINNI_HOME: home,
        // No listener: recall fails fast, so the offline pre-scan runs.
        MINNI_SOCKET_PATH: path.join(home, "minnid-unreachable.sock"),
        MINNI_VAULT_PATH: root,
        MINNI_CLAUDECODE_VAULT_PATH: root,
        MINNI_TEST_SEARCH_PROBE_VAULT: root,
      },
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const closed = new Promise((resolve) => child.on("close", resolve));
  try {
    const waiters = new Map();
    let buffered = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffered += chunk;
      let nl;
      while ((nl = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, nl).trim();
        buffered = buffered.slice(nl + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined) waiters.get(msg.id)?.(msg);
        } catch {
          /* protocol noise surfaces via timeout */
        }
      }
    });
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    const call = (id, method, params, ms = 15_000) =>
      new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timeout ${method}; stderr=${stderr}`)), ms);
        waiters.set(id, (msg) => {
          clearTimeout(timer);
          resolve(msg);
        });
        send({ jsonrpc: "2.0", id, method, params });
      });

    await call(1, "initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "search-path-test", version: "0.0.0" },
    });
    send({ jsonrpc: "2.0", method: "notifications/initialized" });
    const reply = await call(2, "tools/call", {
      name: "minni_recall",
      arguments: { query: "cache policy tests", limit: 5, includeVault: true },
    });
    assert.match(reply.result.content[0].text, /cache-note-/, "the pre-scan found the seeded notes");

    child.kill("SIGTERM");
    await closed;
    const state = probeState(stderr);
    assert.ok(state, "the server searched the vault in this process");
    assert.ok(state.notes >= 20, `the cache holds the vault (${state.notes} notes)`);
    assert.equal(state.prefixIndex, true, "the long-lived server keeps the prefix index");
  } finally {
    child.kill("SIGKILL");
    await rm(root, { recursive: true, force: true });
  }
});

test("source: only the hook handler opts into the scan path; every other caller keeps the index default", async () => {
  const srcDir = new URL("../src/", import.meta.url);
  const optIns = [];
  for (const name of (await readdir(srcDir)).sort()) {
    if (!name.endsWith(".ts")) continue;
    const text = await readFile(new URL(name, srcDir), "utf8");
    if (/candidates:\s*"scan"/.test(text) && name !== "vault.ts") optIns.push(name);
  }
  assert.deepEqual(optIns, ["hook-handlers.ts"]);
});
