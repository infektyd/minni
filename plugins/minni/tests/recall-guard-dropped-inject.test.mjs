// L-nexus-2 / L-plumb-7: dropped inject must not arm the s6 UNCONSULTED guard.
//
// Grok ignores passive-event stdout (GROK_INJECTABLE={"Stop"} is STRUCTURAL —
// do not add SessionStart/UPS inject). Cursor injects at SessionStart only.
// Unprofiled wires inject nowhere. UPS used to writeRecallState(consumed=false)
// BEFORE renderIntent dropped, so PreToolUse denied against an envelope the
// model never saw. Consume/deny applies only when the envelope reached the model.
//
// Isolation: missing daemon socket + a vault note whose body is the prompt
// (direct substring match is strong). No live minni.db. No 30s harness raise.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createHookHandlers } from "../dist/hook-handlers.js";
import {
  claudeCodeWire,
  cursorWire,
  grokBuildWire,
} from "../dist/hook-platform.js";
import { RECALL_STATE_RELPATH, readRecallState } from "../dist/recall-state.js";

const PLUGIN_SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src");

function isDeny(output) {
  return output?.hookSpecificOutput?.permissionDecision === "deny";
}

async function fileExists(p) {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

function turnConfig(vaultPath, overrides = {}) {
  return {
    agentId: "claude-code",
    vaultPath,
    defaultWorkspaceId: "workspace-fixture",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
    recallGuardMode: "soft",
    ...overrides,
  };
}

async function withFixture(run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-recall-dropped-inject-"));
  const vault = path.join(root, "vault");
  const home = path.join(root, "home");
  const saved = {
    home: process.env.MINNI_HOME,
    socket: process.env.MINNI_SOCKET_PATH,
    afm: process.env.MINNI_AFM_HEALTH_URL,
    bypass: process.env.MINNI_BYPASS_AUDIT_LIMIT,
    threshold: process.env.MINNI_RECALL_POINTER_THRESHOLD,
    mode: process.env.MINNI_RECALL_GUARD_MODE,
  };
  process.env.MINNI_HOME = home;
  process.env.MINNI_SOCKET_PATH = path.join(home, "missing.sock");
  process.env.MINNI_AFM_HEALTH_URL = "http://127.0.0.1:1/health";
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  delete process.env.MINNI_RECALL_POINTER_THRESHOLD;
  delete process.env.MINNI_RECALL_GUARD_MODE;
  await mkdir(home, { recursive: true });
  try {
    await run({ vault, home, root });
  } finally {
    for (const [key, value] of [
      ["MINNI_HOME", saved.home],
      ["MINNI_SOCKET_PATH", saved.socket],
      ["MINNI_AFM_HEALTH_URL", saved.afm],
      ["MINNI_BYPASS_AUDIT_LIMIT", saved.bypass],
      ["MINNI_RECALL_POINTER_THRESHOLD", saved.threshold],
      ["MINNI_RECALL_GUARD_MODE", saved.mode],
    ]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await rm(root, { recursive: true, force: true });
  }
}

async function writeStrongVaultNote(vault, prompt) {
  const wiki = path.join(vault, "wiki", "sessions");
  await mkdir(wiki, { recursive: true });
  await writeFile(
    path.join(wiki, "20260901-dropped-inject-strong.md"),
    `# Strong hit note\n\nThe exact phrase: ${prompt}\nThis note documents the decision so the agent need not derive it from scratch.\n`,
    "utf8",
  );
}

function unprofiledWire() {
  return {
    id: "unprofiled",
    noop: () => ({ continue: true }),
    inject: () => null,
    note: () => null,
    lastTaskText: () => "",
  };
}

async function strongUpsThenRead(handlers, vault, prompt) {
  await writeStrongVaultNote(vault, prompt);
  const ups = await handlers.handleUserPromptSubmit({
    prompt,
    workspace_id: "workspace-fixture",
  });
  const state = await readRecallState(vault);
  const pre = await handlers.handlePreToolUse({
    tool_name: "Read",
    tool_input: { file_path: "/tmp/work/README.md" },
  });
  return { ups, state, pre, statePath: path.join(vault, RECALL_STATE_RELPATH) };
}

test("GROK_INJECTABLE stays Stop-only — do not 'fix' dropped UPS by expanding inject", async () => {
  const src = await readFile(path.join(PLUGIN_SRC, "hook-platform.ts"), "utf8");
  const match = src.match(
    /const GROK_INJECTABLE: ReadonlySet<EnvelopeEvent> = new Set\(\[([^\]]*)\]\)/,
  );
  assert.ok(match, "GROK_INJECTABLE declaration must remain in hook-platform.ts");
  const members = [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(members, ["Stop"], "GROK_INJECTABLE must stay {Stop}; SessionStart/UPS stay uninjectable");
  assert.equal(grokBuildWire.inject("SessionStart", "memory"), null);
  assert.equal(grokBuildWire.inject("UserPromptSubmit", "memory"), null);
  assert.notEqual(grokBuildWire.inject("Stop", "memory"), null);
});

test("Grok UPS strong recall does not plant consumed=false; PreToolUse allows", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(
      turnConfig(vault, { agentId: "grok-build", auditPrefix: "hook_grok", wire: grokBuildWire }),
    );
    const prompt = "resume the dropped-inject grok cobalt wal decision from prior context";
    const { state, pre, statePath } = await strongUpsThenRead(handlers, vault, prompt);

    assert.equal(await fileExists(statePath), false, "dropped Grok UPS must not write recall-state.json");
    assert.equal(state, null);
    assert.equal(isDeny(pre), false, "UNCONSULTED must not fire — the model never saw the envelope");
    assert.equal(pre.continue, true);
    assert.equal(pre.hookSpecificOutput, undefined);
  });
});

test("Cursor UPS (non-sessionStart) strong recall does not plant consumed=false; PreToolUse allows", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(
      turnConfig(vault, { agentId: "cursor", auditPrefix: "hook_cursor", wire: cursorWire }),
    );
    const prompt = "resume the dropped-inject cursor cobalt wal decision from prior context";
    const { state, pre, statePath } = await strongUpsThenRead(handlers, vault, prompt);

    assert.equal(cursorWire.inject("UserPromptSubmit", "memory"), null);
    assert.notEqual(cursorWire.inject("SessionStart", "memory"), null);
    assert.equal(await fileExists(statePath), false, "dropped Cursor UPS must not write recall-state.json");
    assert.equal(state, null);
    assert.equal(isDeny(pre), false, "UNCONSULTED must not fire — Cursor cannot inject at beforeSubmitPrompt");
  });
});

test("unprofiled wire strong UPS does not plant consumed=false; PreToolUse allows", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(
      turnConfig(vault, { agentId: "unprofiled", auditPrefix: "hook_unprofiled", wire: unprofiledWire() }),
    );
    const prompt = "resume the dropped-inject unprofiled cobalt wal decision from prior context";
    const { state, pre, statePath } = await strongUpsThenRead(handlers, vault, prompt);

    assert.equal(await fileExists(statePath), false, "unprofiled drop must not write recall-state.json");
    assert.equal(state, null);
    assert.equal(isDeny(pre), false, "UNCONSULTED must not fire when no envelope can reach the model");
  });
});

test("Grok UPS clears a leftover consumed=false plant so UNCONSULTED cannot fire", async () => {
  await withFixture(async ({ vault }) => {
    const statePath = path.join(vault, RECALL_STATE_RELPATH);
    await mkdir(path.dirname(statePath), { recursive: true });
    await writeFile(
      statePath,
      JSON.stringify({
        task_signature: "leftover-from-dropped-ups",
        intent: "recall",
        top_hits: [{ title: "Prior fix", wikilink: "[[prior-fix]]", score: 0.91 }],
        top_score: 0.91,
        consumed: false,
        ts: "2026-08-30T00:00:00.000Z",
      }),
      "utf8",
    );
    const handlers = createHookHandlers(
      turnConfig(vault, { agentId: "grok-build", auditPrefix: "hook_grok", wire: grokBuildWire }),
    );
    const prompt = "resume the leftover dropped-inject grok cobalt wal decision from prior context";
    const { state, pre } = await strongUpsThenRead(handlers, vault, prompt);
    assert.equal(await fileExists(statePath), false, "leftover consumed=false must not survive a dropped UPS");
    assert.equal(state, null);
    assert.equal(isDeny(pre), false, "leftover plant must not deny after the envelope was dropped");
  });
});

test("Claude UPS still plants consumed=false and PreToolUse denies UNCONSULTED", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(turnConfig(vault, { wire: claudeCodeWire }));
    const prompt = "resume the delivered-inject claude cobalt wal decision from prior context";
    const { state, pre, statePath } = await strongUpsThenRead(handlers, vault, prompt);

    assert.ok(await fileExists(statePath), "injectable UPS must still write recall-state.json");
    assert.equal(state.consumed, false, "delivered envelope arms the s6 guard");
    assert.ok(isDeny(pre), "Claude PreToolUse must still deny UNCONSULTED after a delivered pointer");
    assert.match(pre.hookSpecificOutput.permissionDecisionReason, /UNCONSULTED/);
  });
});
