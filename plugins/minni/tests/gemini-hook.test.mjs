// #133: Gemini/Antigravity (agy CLI) hook — adapter units + behavioral spawns.
//
// The protocol facts these tests pin down were captured live against agy
// 1.0.15 (payloads in the #133 investigation): agy speaks Claude Code's
// hooks.json manifest format but NOT its payload/output protocol. The
// load-bearing invariant is that a PreToolUse invocation ALWAYS prints a
// non-empty decision — agy 1.0.15's permission manager errors on empty
// decision strings, and a wedged permission manager blocks the whole session.
//
// Isolation mirrors hook-behavior.test.mjs: every env knob points inside a
// tmp fixture; the live ~/.minni is never read or written.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  adaptAgyPayload,
  adaptPreToolUseOutput,
  agyApprove,
} from "../dist/gemini-adapter.js";

const execFileAsync = promisify(execFile);
const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const GEMINI_HOOK_JS = path.join(PLUGIN_ROOT, "dist", "gemini-hook.js");
const HOOKS_GEMINI_JSON = path.join(PLUGIN_ROOT, "hooks", "hooks-gemini.json");

// A live-captured agy 1.0.15 PreToolUse payload (paths shortened).
function agyPreToolUsePayload(overrides = {}) {
  return {
    artifactDirectoryPath: "/tmp/brain/cfcd2d03",
    conversationId: "cfcd2d03-9775-4ff2-8667-ba461998307f",
    modelName: "gemini-pro-agent",
    stepIdx: 4,
    toolCall: {
      args: { CommandLine: "echo hello", Cwd: "/tmp/scratch", WaitMsBeforeAsync: 500 },
      name: "run_command",
    },
    transcriptPath: "/tmp/brain/cfcd2d03/transcript_full.jsonl",
    workspacePaths: [],
    ...overrides,
  };
}

async function makeFixture() {
  const root = await mkdtemp(path.join(tmpdir(), "minni-gemini-hook-"));
  const vault = path.join(root, "gemini-vault");
  await mkdir(vault, { recursive: true });
  return { root, vault };
}

async function runGeminiHook(event, fixture, payload, extraEnv = {}) {
  const env = {
    ...process.env,
    MINNI_HOME: fixture.root,
    MINNI_GEMINI_VAULT_PATH: fixture.vault,
    MINNI_SOCKET_PATH: path.join(fixture.root, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    ...extraEnv,
  };
  const child = execFileAsync(process.execPath, [GEMINI_HOOK_JS, event], {
    env,
    timeout: 30_000,
  });
  child.child.stdin.end(payload === undefined ? "" : JSON.stringify(payload));
  const { stdout } = await child;
  return JSON.parse(stdout.trim().split("\n").at(-1));
}

test("adaptAgyPayload maps agy fields to the factory's canonical names", () => {
  const adapted = adaptAgyPayload(agyPreToolUsePayload({ workspacePaths: ["/w/repo"] }));
  assert.equal(adapted.session_id, "cfcd2d03-9775-4ff2-8667-ba461998307f");
  assert.equal(adapted.workspace_id, "/w/repo");
  assert.equal(adapted.tool_name, "Bash");
  assert.deepEqual(adapted.tool_input, {
    command: "echo hello",
    cwd: "/tmp/scratch",
    WaitMsBeforeAsync: 500,
  });
  // Original agy fields are preserved for forward-compat.
  assert.equal(adapted.conversationId, "cfcd2d03-9775-4ff2-8667-ba461998307f");
});

test("adaptAgyPayload never clobbers canonical fields and passes unknown tools through", () => {
  const native = adaptAgyPayload({
    session_id: "native-session",
    conversationId: "agy-conversation",
    toolCall: { name: "browser_navigate", args: { Url: "https://x" } },
    workspacePaths: ["", "/w/second"],
  });
  assert.equal(native.session_id, "native-session");
  assert.equal(native.tool_name, "browser_navigate");
  assert.deepEqual(native.tool_input, { Url: "https://x" });
  assert.equal(native.workspace_id, "/w/second");
});

test("adaptPreToolUseOutput: allow collapses to explicit approve, deny carries the reason", () => {
  assert.deepEqual(adaptPreToolUseOutput({ continue: true }), { decision: "approve" });
  assert.deepEqual(agyApprove(), { decision: "approve" });
  const deny = adaptPreToolUseOutput({
    continue: true,
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "consult recall first",
    },
  });
  assert.deepEqual(deny, { decision: "block", reason: "consult recall first" });
});

test("hooks-gemini.json template: matcher-free, token-stamped, no CLAUDE_PLUGIN_ROOT", async () => {
  const template = JSON.parse(await readFile(HOOKS_GEMINI_JSON, "utf8"));
  const events = Object.keys(template.hooks);
  assert.ok(events.includes("PreToolUse") && events.includes("Stop"));
  for (const [event, groups] of Object.entries(template.hooks)) {
    for (const group of groups) {
      // agy's loader drops matcher-bearing entries ("0 total handlers").
      assert.equal(group.matcher, undefined, `${event} entry must be matcher-free`);
      for (const hook of group.hooks) {
        assert.equal(hook.type, "command");
        assert.ok(
          hook.command.includes("__MINNI_GEMINI_DIST__/gemini-hook.js"),
          `${event} command must run gemini-hook.js via the dist token`,
        );
        assert.ok(
          !hook.command.includes("CLAUDE_PLUGIN_ROOT"),
          "agy does not expand ${CLAUDE_PLUGIN_ROOT}",
        );
        assert.ok(hook.command.endsWith(` ${event}`), "command must pass its event name");
      }
    }
  }
});

test("PreToolUse with no recall state prints exactly the explicit approve", async () => {
  const fixture = await makeFixture();
  try {
    const output = await runGeminiHook("PreToolUse", fixture, agyPreToolUsePayload());
    assert.deepEqual(output, { decision: "approve" });
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("PreToolUse never emits an empty decision, even when hooks are disabled or the event is unknown", async () => {
  const fixture = await makeFixture();
  try {
    const disabled = await runGeminiHook("PreToolUse", fixture, agyPreToolUsePayload(), {
      MINNI_GEMINI_HOOKS: "off",
    });
    assert.deepEqual(disabled, { decision: "approve" });
    const unknownEvent = await runGeminiHook("PostToolUse", fixture, agyPreToolUsePayload());
    // Non-PreToolUse unknown events keep the plain continue shape.
    assert.deepEqual(unknownEvent, { continue: true });
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("PreToolUse denies-to-surface through agy's decision vocabulary and flips consumed", async () => {
  const fixture = await makeFixture();
  try {
    const runtimeDir = path.join(fixture.vault, ".runtime");
    await mkdir(runtimeDir, { recursive: true });
    const statePath = path.join(runtimeDir, "recall-state.json");
    await writeFile(
      statePath,
      JSON.stringify({
        task_signature: "t-test",
        intent: "status",
        top_hits: [{ title: "Prior fix", wikilink: "[[prior-fix]]", score: 0.91 }],
        top_score: 0.91,
        consumed: false,
        ts: new Date().toISOString(),
      }),
    );
    // strict mode guards read/search Bash; the adapter maps run_command ->
    // Bash and CommandLine -> command, so this exercises the whole chain.
    const output = await runGeminiHook(
      "PreToolUse",
      fixture,
      agyPreToolUsePayload({
        toolCall: { name: "run_command", args: { CommandLine: "grep foo bar.txt" } },
      }),
      { MINNI_RECALL_GUARD_MODE: "strict" },
    );
    assert.equal(output.decision, "block");
    assert.match(output.reason, /recall guard/i);
    assert.match(output.reason, /prior-fix/);
    const state = JSON.parse(await readFile(statePath, "utf8"));
    assert.equal(state.consumed, true);

    // Idempotent re-issue: the same call now approves.
    const rerun = await runGeminiHook(
      "PreToolUse",
      fixture,
      agyPreToolUsePayload({
        toolCall: { name: "run_command", args: { CommandLine: "grep foo bar.txt" } },
      }),
      { MINNI_RECALL_GUARD_MODE: "strict" },
    );
    assert.deepEqual(rerun, { decision: "approve" });
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("Stop drafts candidates under gemini's own identity stamps", async () => {
  const fixture = await makeFixture();
  try {
    const output = await runGeminiHook("Stop", fixture, agyPreToolUsePayload({ toolCall: null }));
    assert.equal(output.continue, true);
    const inboxDir = path.join(fixture.vault, "inbox");
    const entries = await readdir(inboxDir).catch(() => []);
    // Candidate drafting is local (no daemon needed): if the compact outcome
    // produced candidates, the draft must carry gemini's canonical stamps —
    // never another agent's identity — and audit under the gemini prefix.
    for (const entry of entries) {
      const draft = JSON.parse(await readFile(path.join(inboxDir, entry), "utf8"));
      assert.equal(draft.kind, "stop_candidates");
      assert.equal(draft.agent_id, "gemini");
      // The agy conversationId must have become the session id in the filename.
      assert.match(entry, /cfcd2d03/);
    }
    if (entries.length > 0) {
      const log = await readFile(path.join(fixture.vault, "log.md"), "utf8");
      assert.ok(log.includes("hook_gemini_stop"), "Stop must audit under the hook_gemini prefix");
      assert.ok(!log.includes("hook_codex"), "gemini hook must not audit under another agent's prefix");
    }
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});
