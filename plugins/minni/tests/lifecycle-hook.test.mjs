// Slices c3/c4/c5: the passive-hook lifecycle representation (claude-code only),
// proven via real `node dist/hook.js UserPromptSubmit` subprocess invocations
// (the same integration style as hook-behavior.test.mjs), not unit calls.
//
//   c3 — the PERSISTENT 4-surface line survives BOTH early-returns (write-intent
//        gate and nothing-salient gate) so it shows every turn;
//   c4 — a situational `lifecycle_focus` is added on an ambition intent, at most
//        once per surface per session;
//   c5 — MINNI_LIFECYCLE_NUDGE_MODE=off makes the whole feature silent.
//
// Isolation: daemon socket points at a missing path (weak recall), the fixture
// vault is empty (no active plan, no notes), AFM health URL is a closed port.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { MINNI_LIFECYCLE_LINE, buildLifecycleEmphasis } from "../dist/agent_envelope.js";

const execFileAsync = promisify(execFile);
const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK_JS = path.join(PLUGIN_ROOT, "dist", "hook.js");

async function makeFixture() {
  const root = await mkdtemp(path.join(tmpdir(), "sm-lifecycle-hook-"));
  const vault = path.join(root, "vault");
  const home = path.join(root, "home");
  await mkdir(vault, { recursive: true });
  await mkdir(home, { recursive: true });
  return { vault, home };
}

async function runUserPromptIn(fixture, prompt, extraEnv = {}) {
  const env = {
    ...process.env,
    MINNI_HOME: fixture.home,
    MINNI_CLAUDECODE_VAULT_PATH: fixture.vault,
    MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    ...extraEnv,
  };
  const child = execFileAsync(process.execPath, [HOOK_JS, "UserPromptSubmit"], {
    env,
    timeout: 30_000,
  });
  child.child.stdin.end(JSON.stringify({ prompt }));
  const { stdout } = await child;
  return JSON.parse(stdout.trim().split("\n").pop());
}

async function runUserPrompt(prompt, extraEnv = {}) {
  return runUserPromptIn(await makeFixture(), prompt, extraEnv);
}

function additionalContext(output) {
  return output.hookSpecificOutput?.additionalContext ?? "";
}

test("c3: nothing-salient turn STILL emits the lifecycle line (~297 early-return)", async () => {
  const out = await runUserPrompt("hello there, just chatting about the weather today");
  assert.equal(out.continue, true);
  const ctx = additionalContext(out);
  assert.ok(
    ctx.includes(MINNI_LIFECYCLE_LINE),
    "the 4-surface line must ride a weak-recall / no-active-plan turn",
  );
  for (const surface of ["prepare_task", "prepare_outcome", "plan", "learn"]) {
    assert.ok(ctx.includes(surface), `names ${surface}`);
  }
});

test("c3: write-intent turn STILL emits the lifecycle line (~243 early-return)", async () => {
  const out = await runUserPrompt("remember this important detail for later");
  assert.equal(out.continue, true);
  assert.ok(
    additionalContext(out).includes(MINNI_LIFECYCLE_LINE),
    "the 4-surface line must ride a learn/vault_write turn too",
  );
});

test("c4: planning intent adds the Plan focus, at most once per session", async () => {
  const fx = await makeFixture();
  const out1 = await runUserPromptIn(fx, "plan the architecture for the new module");
  const ctx1 = additionalContext(out1);
  assert.ok(ctx1.includes(MINNI_LIFECYCLE_LINE), "persistent line present");
  assert.ok(ctx1.includes('"lifecycle_focus"'), "focus field present on a planning turn");
  assert.ok(ctx1.includes(buildLifecycleEmphasis("plan")), "Plan focus names its options");

  // second planning turn in the SAME session: persistent line stays, focus is gone
  const out2 = await runUserPromptIn(fx, "plan the migration approach next");
  const ctx2 = additionalContext(out2);
  assert.ok(ctx2.includes(MINNI_LIFECYCLE_LINE), "persistent line still present");
  assert.ok(!ctx2.includes('"lifecycle_focus"'), "focus suppressed the 2nd time (once/session)");
});

test("c4: ambitious task intent emphasizes prepare_task", async () => {
  const ctx = additionalContext(await runUserPrompt("implement the new feature end to end"));
  assert.ok(ctx.includes(buildLifecycleEmphasis("prepare_task")), "prepare_task focus present");
});

test("c5: MINNI_LIFECYCLE_NUDGE_MODE=off makes the feature fully silent", async () => {
  const ctx = additionalContext(
    await runUserPrompt("plan the architecture for the new module", {
      MINNI_LIFECYCLE_NUDGE_MODE: "off",
    }),
  );
  assert.ok(!ctx.includes(MINNI_LIFECYCLE_LINE), "no persistent line when off");
  assert.ok(!ctx.includes("Minni lifecycle"), "no lifecycle content at all when off");
});
