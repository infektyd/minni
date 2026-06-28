// Slice s6: the PreToolUse recall guard (BACKSTOP). Proves EVERY path:
//   - deny-first-cold-search: strong+unconsumed state + Grep => deny, the reason
//     contains the hit titles, and consumed flips to true on disk;
//   - idempotent re-issue: after a deny, the SAME call again => ALLOW (no deny),
//     because consumed===true — the re-issued call ALWAYS passes (no block loop);
//   - allow-when-consumed / allow-when-weak / allow-when-no-state / allow-off;
//   - soft mode does NOT guard Bash; strict mode guards a read/search Bash
//     command (grep) but NOT an editing Bash command (npm/git/mv);
//   - never blocks a non-scope tool (Edit) or a minni_* / mcp__ tool.
//
// Isolation: the guard's only inputs are (1) the recall-state file under the
// vault and (2) the mode/threshold config. We write the state file directly to
// drive each scenario — no daemon, no UserPromptSubmit run needed.
import assert from "node:assert/strict";
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { createHookHandlers } from "../dist/hook-handlers.js";
import {
  LIFECYCLE_STATE_RELPATH,
  RECALL_STATE_RELPATH,
  markRecallConsumed,
  readLifecycleState,
  readRecallState,
} from "../dist/recall-state.js";
import {
  decideGuard,
  isReadSearchBashCommand,
  isToolInScope,
  recallGuardMode,
} from "../dist/recall-guard.js";

function turnConfig(vaultPath, overrides = {}) {
  return {
    agentId: "claude-code",
    vaultPath,
    defaultWorkspaceId: "workspace-fixture",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
    alwaysWriteStopInbox: false,
    ...overrides,
  };
}

const STRONG_STATE = {
  task_signature: "sig-abc",
  intent: "recall",
  top_hits: [
    { title: "cobalt-wal-decision", wikilink: "[[wiki/knowledge/cobalt-wal]]", score: 0.81 },
    { title: "falcon-retry-backoff", wikilink: "[[wiki/sessions/falcon-retry]]", score: 0.62 },
  ],
  top_score: 0.81,
  consumed: false,
  ts: "2026-06-17T00:00:00.000Z",
};

async function withFixture(run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-recall-guard-"));
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
    await run({ vault, home });
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

async function writeState(vault, state) {
  const p = path.join(vault, RECALL_STATE_RELPATH);
  await mkdir(path.dirname(p), { recursive: true });
  await writeFile(p, JSON.stringify(state, null, 2), "utf8");
  return p;
}

async function readState(vault) {
  return JSON.parse(await readFile(path.join(vault, RECALL_STATE_RELPATH), "utf8"));
}

function isDeny(output) {
  return output?.hookSpecificOutput?.permissionDecision === "deny";
}

// ── Behavioral: the full handler against the state file ─────────────────────

test("deny-first-cold-search: strong+unconsumed state + Grep => deny, reason lists hits, consumed flips true", async () => {
  await withFixture(async ({ vault }) => {
    const statePath = await writeState(vault, STRONG_STATE);
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "soft" }));

    const output = await handlers.handlePreToolUse({
      tool_name: "Grep",
      tool_input: { pattern: "cobalt" },
    });

    assert.ok(isDeny(output), "strong unconsumed Grep must be denied");
    assert.equal(output.hookSpecificOutput.hookEventName, "PreToolUse");
    const reason = output.hookSpecificOutput.permissionDecisionReason;
    assert.match(reason, /cobalt-wal-decision/, "reason must contain the top hit title");
    assert.match(reason, /\[\[wiki\/knowledge\/cobalt-wal\]\]/, "reason must contain the wikilink");
    assert.match(reason, /Re-issue this exact call to proceed/);

    // consumed flipped to true on disk (the idempotency write).
    const after = await readState(vault);
    assert.equal(after.consumed, true, "consumed must be true after the deny");
    assert.ok(statePath.endsWith(path.join(".runtime", "recall-state.json")));
  });
});

test("idempotent re-issue: the SAME call after a deny => ALLOW (consumed=true), no block loop", async () => {
  await withFixture(async ({ vault }) => {
    await writeState(vault, STRONG_STATE);
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "soft" }));

    const first = await handlers.handlePreToolUse({ tool_name: "Grep", tool_input: { pattern: "x" } });
    assert.ok(isDeny(first), "first call denies");

    // Re-issue the EXACT same call. Must now ALLOW (no permissionDecision).
    const second = await handlers.handlePreToolUse({ tool_name: "Grep", tool_input: { pattern: "x" } });
    assert.equal(isDeny(second), false, "re-issued call must ALLOW");
    assert.equal(second.hookSpecificOutput, undefined, "allow emits no permissionDecision");
    assert.equal(second.continue, true);

    // And a THIRD, different cold-search tool this turn also allows (fires at most once).
    const third = await handlers.handlePreToolUse({ tool_name: "Read", tool_input: { file_path: "/x" } });
    assert.equal(isDeny(third), false, "all subsequent calls this turn allow once consumed");
  });
});

test("allow-when-consumed: a pre-consumed state never denies", async () => {
  await withFixture(async ({ vault }) => {
    await writeState(vault, { ...STRONG_STATE, consumed: true });
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "soft" }));
    const out = await handlers.handlePreToolUse({ tool_name: "Glob", tool_input: { pattern: "**" } });
    assert.equal(isDeny(out), false);
  });
});

test("allow-when-weak: top_score below threshold => allow", async () => {
  await withFixture(async ({ vault }) => {
    await writeState(vault, { ...STRONG_STATE, top_score: 0.10, top_hits: [{ title: "t", wikilink: "[[t]]", score: 0.10 }] });
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "soft" }));
    const out = await handlers.handlePreToolUse({ tool_name: "Grep", tool_input: { pattern: "y" } });
    assert.equal(isDeny(out), false, "weak recall must not fire the guard");
  });
});

test("allow-when-no-state: no recall-state file => allow", async () => {
  await withFixture(async ({ vault }) => {
    await mkdir(vault, { recursive: true });
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "soft" }));
    const out = await handlers.handlePreToolUse({ tool_name: "Read", tool_input: { file_path: "/a" } });
    assert.equal(isDeny(out), false);
  });
});

test("state readers ignore absent or malformed files but surface filesystem errors", async () => {
  await withFixture(async ({ vault }) => {
    await mkdir(path.join(vault, ".runtime"), { recursive: true });
    assert.equal(await readRecallState(vault), null);
    assert.equal(await readLifecycleState(vault), null);

    await writeFile(path.join(vault, RECALL_STATE_RELPATH), "null", "utf8");
    await writeFile(path.join(vault, LIFECYCLE_STATE_RELPATH), "{", "utf8");
    assert.equal(await readRecallState(vault), null);
    assert.equal(await readLifecycleState(vault), null);

    await rm(path.join(vault, RECALL_STATE_RELPATH), { force: true });
    await mkdir(path.join(vault, RECALL_STATE_RELPATH), { recursive: true });
    await assert.rejects(() => readRecallState(vault));
  });
});

test("allow-in-off-mode: mode=off never denies even with strong unconsumed state", async () => {
  await withFixture(async ({ vault }) => {
    await writeState(vault, STRONG_STATE);
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "off" }));
    const out = await handlers.handlePreToolUse({ tool_name: "Grep", tool_input: { pattern: "z" } });
    assert.equal(isDeny(out), false, "off mode disables the guard");
    // And it must NOT have consumed the state (no side effects in off mode).
    const after = await readState(vault);
    assert.equal(after.consumed, false);
  });
});

test("soft mode does NOT guard Bash (even a read/search command)", async () => {
  await withFixture(async ({ vault }) => {
    await writeState(vault, STRONG_STATE);
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "soft" }));
    const out = await handlers.handlePreToolUse({
      tool_name: "Bash",
      tool_input: { command: "grep -r cobalt ." },
    });
    assert.equal(isDeny(out), false, "soft mode leaves Bash untouched");
  });
});

test("strict mode guards a read/search Bash command (grep) => deny", async () => {
  await withFixture(async ({ vault }) => {
    await writeState(vault, STRONG_STATE);
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "strict" }));
    const out = await handlers.handlePreToolUse({
      tool_name: "Bash",
      tool_input: { command: "grep -rn cobalt src/" },
    });
    assert.ok(isDeny(out), "strict mode must guard a pure read/search Bash command");
  });
});

test("strict mode does NOT guard an editing Bash command (npm/git/mv/redirect)", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "strict" }));
    for (const command of [
      "npm run build",
      "git commit -m wip",
      "mv a b",
      "rm -rf foo",
      "node script.js",
      "python run.py",
      "grep cobalt . > out.txt", // read verb but writes a file => allow
      "sed -i s/a/b/ f", // in-place edit => allow
    ]) {
      // Re-arm fresh unconsumed state for each command (a prior deny would consume it).
      await writeState(vault, STRONG_STATE);
      const out = await handlers.handlePreToolUse({ tool_name: "Bash", tool_input: { command } });
      assert.equal(isDeny(out), false, `editing Bash must NOT be guarded: ${command}`);
    }
  });
});

test("never blocks a non-scope tool (Edit) or a minni_* / mcp__ tool", async () => {
  await withFixture(async ({ vault }) => {
    const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "strict" }));
    for (const tool_name of ["Edit", "Write", "minni_recall", "mcp__minni__minni_recall"]) {
      await writeState(vault, STRONG_STATE);
      const out = await handlers.handlePreToolUse({ tool_name, tool_input: {} });
      assert.equal(isDeny(out), false, `must never guard ${tool_name}`);
    }
  });
});

// ── Pure unit tests: scope, Bash detection, decision, mode ──────────────────

test("isReadSearchBashCommand fires on pure read/search, allows mutations", () => {
  // fire
  for (const c of ["grep -r x .", "rg foo", "rg foo 2>&1", "cat a b", "find . -name x", "ls -la", "head -n 5 f", "tail f", "egrep x f", "/usr/bin/grep x f", "cat a | grep b", "cat missing 2>&1 | grep error"]) {
    assert.equal(isReadSearchBashCommand(c), true, `should fire: ${c}`);
  }
  // allow (do not fire)
  for (const c of ["npm run build", "git commit -m x", "mv a b", "rm f", "node x.js", "python y.py", "grep x > out", "grep x >> out", "grep x 2> err.log", "grep x &> out", "sed -i s/a/b/ f", "cat f | node parse.js", "FOO=1 grep x f", "echo hi", ""]) {
    assert.equal(isReadSearchBashCommand(c), false, `should allow: ${c}`);
  }
});

test("isToolInScope: soft scopes Grep/Read/Glob only; strict adds read Bash; off none; minni never", () => {
  assert.equal(isToolInScope("soft", "Grep", {}), true);
  assert.equal(isToolInScope("soft", "Read", {}), true);
  assert.equal(isToolInScope("soft", "Glob", {}), true);
  assert.equal(isToolInScope("soft", "Bash", { command: "grep x ." }), false);
  assert.equal(isToolInScope("strict", "Bash", { command: "grep x ." }), true);
  assert.equal(isToolInScope("strict", "Bash", { command: "npm run build" }), false);
  assert.equal(isToolInScope("off", "Grep", {}), false);
  assert.equal(isToolInScope("strict", "minni_recall", {}), false);
  assert.equal(isToolInScope("strict", "mcp__minni__minni_recall", {}), false);
  assert.equal(isToolInScope("soft", "Edit", {}), false);
});

test("decideGuard: deny only when state+unconsumed+strong+in-scope+non-off", () => {
  const base = { mode: "soft", threshold: 0.55, toolName: "Grep", toolInput: {} };
  assert.equal(decideGuard({ ...base, state: STRONG_STATE }), "deny");
  assert.equal(decideGuard({ ...base, state: { ...STRONG_STATE, consumed: true } }), "allow");
  assert.equal(decideGuard({ ...base, state: { ...STRONG_STATE, top_score: 0.1 } }), "allow");
  assert.equal(decideGuard({ ...base, state: null }), "allow");
  assert.equal(decideGuard({ ...base, mode: "off", state: STRONG_STATE }), "allow");
  assert.equal(decideGuard({ ...base, toolName: "Bash", toolInput: { command: "grep x ." }, state: STRONG_STATE }), "allow"); // soft
  assert.equal(decideGuard({ ...base, mode: "strict", toolName: "Bash", toolInput: { command: "grep x ." }, state: STRONG_STATE }), "deny");
});

test("recallGuardMode default is soft; honors override; unknown falls back to soft", () => {
  assert.equal(recallGuardMode({}), "soft");
  assert.equal(recallGuardMode({ MINNI_RECALL_GUARD_MODE: "off" }), "off");
  assert.equal(recallGuardMode({ MINNI_RECALL_GUARD_MODE: "STRICT" }), "strict");
  assert.equal(recallGuardMode({ MINNI_RECALL_GUARD_MODE: "nonsense" }), "soft");
});

// ── S4: NEW-03 / PR90-2 / PR90-3 — atomic consume + fail-open guard ──────────

test("PR90-3/NEW-03: concurrent markRecallConsumed converges to consumed=true, no corruption", async () => {
  await withFixture(async ({ vault }) => {
    await writeState(vault, STRONG_STATE);
    // Fire many concurrent consume writes (simulating a parallel tool batch of
    // separate hook processes). The atomic temp+rename must never leave the file
    // truncated; all racers write consumed=true so the result converges.
    const results = await Promise.all(
      Array.from({ length: 20 }, () => markRecallConsumed(vault)),
    );
    assert.ok(results.every((r) => r === true));
    const after = await readState(vault); // throws if JSON is corrupt/truncated
    assert.equal(after.consumed, true);
    assert.equal(after.top_score, STRONG_STATE.top_score); // payload intact
  });
});

test("PR90-2: consume-write failure => guard ALLOWS (no whole-turn block loop)", async () => {
  await withFixture(async ({ vault }) => {
    const statePath = await writeState(vault, STRONG_STATE);
    const stateDir = path.dirname(statePath);
    // Make the recall-state directory read-only so the atomic temp write fails;
    // the file itself stays readable (state still strong+unconsumed).
    await chmod(stateDir, 0o500);
    try {
      // Precondition: the consume write genuinely fails now.
      assert.equal(await markRecallConsumed(vault), false);

      const handlers = createHookHandlers(turnConfig(vault, { recallGuardMode: "soft" }));
      // First call would normally DENY (strong+unconsumed). With the consume
      // write failing, the guard must FAIL OPEN and allow — and stay allow on
      // re-issue (no block loop), because it can never persist consumed=true.
      const first = await handlers.handlePreToolUse({ tool_name: "Grep", tool_input: { pattern: "x" } });
      const second = await handlers.handlePreToolUse({ tool_name: "Grep", tool_input: { pattern: "x" } });
      assert.equal(isDeny(first), false);
      assert.equal(isDeny(second), false);
    } finally {
      await chmod(stateDir, 0o700);
    }
  });
});
