// #295 (June audit N8): shelf drift detection used to be reachable only by
// explicitly calling minni_thread_status with live_shelf_content supplied by
// hand — drift was found only when someone thought to check. This pins the
// automatic check now wired into resolveActivePlanView, driven from
// SessionStart for BOTH claude-code's own hook.ts and the shared
// hook-handlers.ts factory every other platform runs — the two independent
// SessionStart call sites, unblocked by the #283 hook.ts migration.
//
// BEHAVIORAL, not source-text grep (standing scar per
// sessionstart-layer1-shelf.test.mjs): every assertion here drives the real
// built hook (`node dist/hook.js SessionStart` / `dist/codex-hook.js`)
// against a temp vault fixture with a real plan + shelf_ref persisted into
// it, and reads the actual envelope the hook emits. Live ~/.minni is never
// touched.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { createPlan } from "../dist/plan.js";
import { ensureVault } from "../dist/vault.js";

const execFileAsync = promisify(execFile);
const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK_JS = path.join(PLUGIN_ROOT, "dist", "hook.js");
const CODEX_HOOK_JS = path.join(PLUGIN_ROOT, "dist", "codex-hook.js");

async function seedPlanWithShelf(vaultPath, shelfContent) {
  await ensureVault(vaultPath);
  await createPlan(
    {
      goal: "#295 shelf drift auto-trigger fixture",
      vaultPath,
      shelf_ref: {
        agent: "fixture-agent",
        wikilink: "[[wiki/identity/fixture-agent]]",
        pull_hint: "pull before each session",
        shelf_content: shelfContent,
      },
    },
    { vaultPath },
  );
}

function envelopeBody(output) {
  const context = output.hookSpecificOutput?.additionalContext ?? "";
  const match = context.match(/<minni:context [^>]*>\n([\s\S]*?)\n<\/minni:context>/);
  return match ? JSON.parse(match[1]) : undefined;
}

async function runClaudeCodeSessionStart(fixture) {
  const env = {
    ...process.env,
    MINNI_HOME: fixture.home,
    MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    MINNI_CLAUDECODE_VAULT_PATH: fixture.vault,
    MINNI_CLAUDECODE_HOOKS: "on",
  };
  const child = execFileAsync(process.execPath, [HOOK_JS, "SessionStart"], { env, timeout: 30_000 });
  child.child.stdin.end(JSON.stringify({ session_id: "shelf-drift-fixture" }));
  const { stdout } = await child;
  const output = JSON.parse(stdout.trim().split("\n").pop());
  assert.equal(output.continue, true);
  return envelopeBody(output);
}

async function runCodexSessionStart(fixture) {
  const env = {
    ...process.env,
    MINNI_HOME: fixture.home,
    MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    MINNI_CODEX_AGENT_ID: "codex",
    MINNI_CODEX_VAULT_PATH: fixture.vault,
    MINNI_CODEX_HOOKS: "on",
  };
  const child = execFileAsync(process.execPath, [CODEX_HOOK_JS, "SessionStart"], { env, timeout: 30_000 });
  child.child.stdin.end(JSON.stringify({ session_id: "shelf-drift-fixture-codex" }));
  const { stdout } = await child;
  const output = JSON.parse(stdout.trim().split("\n").pop());
  assert.equal(output.continue, true);
  return envelopeBody(output);
}

async function makeFixture(prefix, vaultDirName) {
  const root = await mkdtemp(path.join(tmpdir(), prefix));
  const fixture = { root, vault: path.join(root, vaultDirName), home: path.join(root, "home") };
  await mkdir(fixture.vault, { recursive: true });
  await mkdir(fixture.home, { recursive: true });
  return fixture;
}

test(
  "claude-code SessionStart: matching shelf reports active_thread.shelf_drift.drifted === false, no manual check needed",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-shelf-drift-claude-match-", "claudecode-vault");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    const coreText = "# Identity\n\nModel ladder: sonnet -> opus on stall.\n";
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), coreText, "utf8");
    await seedPlanWithShelf(fixture.vault, coreText);

    const body = await runClaudeCodeSessionStart(fixture);
    assert.ok(body.active_thread, "active_thread must be present with an active plan");
    assert.ok(body.active_thread.shelf_drift, "shelf_drift must be auto-attached, no manual live_shelf_content call needed");
    assert.equal(body.active_thread.shelf_drift.configured, true);
    assert.equal(body.active_thread.shelf_drift.drifted, false);
  },
);

test(
  "claude-code SessionStart: a shelf that changed since plan creation is auto-flagged as drifted",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-shelf-drift-claude-drift-", "claudecode-vault");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    const originalCore = "# Identity\n\nModel ladder: sonnet -> opus on stall.\n";
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    // The plan is created against the ORIGINAL text...
    await seedPlanWithShelf(fixture.vault, originalCore);
    // ...but the live shelf on disk has since moved — the whole point of drift.
    const changedCore = `${originalCore}\nScar: new scar recorded after the plan's shelf_ref was set.\n`;
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), changedCore, "utf8");

    const body = await runClaudeCodeSessionStart(fixture);
    assert.ok(body.active_thread?.shelf_drift, "shelf_drift must be attached");
    assert.equal(body.active_thread.shelf_drift.configured, true);
    assert.equal(
      body.active_thread.shelf_drift.drifted,
      true,
      "a shelf that changed after the plan's shelf_ref was set must be auto-flagged, without anyone manually calling minni_thread_status with live_shelf_content",
    );
  },
);

test(
  "claude-code SessionStart: no active plan at all — no active_thread, so obviously no shelf_drift either",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-shelf-drift-claude-noplan-", "claudecode-vault");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), "# Identity\n", "utf8");

    const body = await runClaudeCodeSessionStart(fixture);
    assert.equal(body.active_thread, undefined, "no active plan was created in this fixture");
  },
);

// Review round: the test above is VACUOUS against the shelf_drift feature —
// deleting the whole feature still passes it, since it never creates a plan
// in the first place. This is the real omit-branch coverage: an active plan
// IS present, it just has no shelf_ref configured, so shelf_drift must be
// absent from an otherwise-populated active_thread, not present as any value.
test(
  "claude-code SessionStart: an active plan with NO shelf_ref omits shelf_drift, without deleting active_thread",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-shelf-drift-claude-noshelfref-", "claudecode-vault");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), "# Identity\n", "utf8");
    await ensureVault(fixture.vault);
    await createPlan(
      { goal: "plan with no shelf_ref at all", vaultPath: fixture.vault },
      { vaultPath: fixture.vault },
    );

    const body = await runClaudeCodeSessionStart(fixture);
    assert.ok(body.active_thread, "the plan must still be injected");
    assert.equal(
      Object.hasOwn(body.active_thread, "shelf_drift"),
      false,
      "shelf_drift must be absent, not present as any value, when the plan has no shelf_ref",
    );
  },
);

// Review round finding: readLayer1Shelf caps at LAYER1_SHELF_MAX_BYTES and
// still returns ok:true with the truncated PREFIX. Hashing that prefix as if
// it were the whole file makes shelfDrift compare against content the stored
// hash was never computed from — a PERMANENT false "drifted" the operator
// can never clear (the live hash can never again match the stored one, since
// the file will always be truncated the same way once it exceeds the cap).
// Reproduced live: the operator's own ~/.minni/claudecode-vault/layer1/core.md
// is already at 5743 of 8192 bytes and grows by appended scars — this is not
// a hypothetical edge case.
test(
  "claude-code SessionStart: a TRUNCATED shelf read omits shelf_drift instead of a permanent false 'drifted'",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-shelf-drift-claude-truncated-", "claudecode-vault");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });

    // Oversized core.md (LAYER1_SHELF_MAX_BYTES is 8192; comfortably over it).
    const oversizedCore = `# Identity\n\n${"x".repeat(9000)}\n`;
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), oversizedCore, "utf8");
    // The plan's shelf_ref was set against the FULL (oversized) content —
    // exactly what createPlan would receive from a caller that read it
    // directly, unlike the truncated read the SessionStart hook gets.
    await ensureVault(fixture.vault);
    await createPlan(
      {
        goal: "#295 review: truncated shelf must not falsely drift",
        vaultPath: fixture.vault,
        shelf_ref: {
          agent: "fixture-agent",
          wikilink: "[[wiki/identity/fixture-agent]]",
          pull_hint: "pull before each session",
          shelf_content: oversizedCore,
        },
      },
      { vaultPath: fixture.vault },
    );

    const body = await runClaudeCodeSessionStart(fixture);
    assert.equal(body.layer1_shelf.truncated, true, "test setup: core.md must actually be truncated by the reader");
    assert.ok(body.active_thread, "the plan must still be injected");
    assert.equal(
      Object.hasOwn(body.active_thread, "shelf_drift"),
      false,
      "a truncated shelf read must OMIT shelf_drift, not report a permanent false 'drifted' " +
        "against content the stored hash was never computed from",
    );
  },
);

test(
  "codex SessionStart (shared hook-handlers.ts factory): shelf drift auto-check works on the non-claude-code path too, not blocked by #283",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-shelf-drift-codex-", "codex-vault");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    const originalCore = "# Identity\n\nCodex-path fixture core.md.\n";
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    await seedPlanWithShelf(fixture.vault, originalCore);
    const changedCore = `${originalCore}\nDrifted for the codex-path assertion.\n`;
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), changedCore, "utf8");

    const body = await runCodexSessionStart(fixture);
    assert.ok(body.active_thread?.shelf_drift, "shelf_drift must be attached on the shared-factory path too");
    assert.equal(body.active_thread.shelf_drift.configured, true);
    assert.equal(body.active_thread.shelf_drift.drifted, true);
  },
);
