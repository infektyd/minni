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
  agyAllow,
  enrichAgyPromptPayload,
  enrichAgyStopPayload,
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

test("adaptPreToolUseOutput: allow collapses to explicit allow, deny carries the reason", () => {
  // agy's enum is allow|deny|ask|force_ask. Claude's "approve"/"block" fail the
  // step outright: unknown pre-tool hook decision "approve".
  assert.deepEqual(adaptPreToolUseOutput({ continue: true }), { decision: "allow" });
  assert.deepEqual(agyAllow(), { decision: "allow" });
  const deny = adaptPreToolUseOutput({
    continue: true,
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "consult recall first",
    },
  });
  assert.deepEqual(deny, { decision: "deny", reason: "consult recall first" });
});

test("hooks-gemini.json template: agy's native shape, token-stamped", async () => {
  const template = JSON.parse(await readFile(HOOKS_GEMINI_JSON, "utf8"));

  // agy reads every TOP-LEVEL key as a hook NAME. A literal "hooks" wrapper
  // declares a hook named "hooks" and agy rejects the ENTIRE file -- which is
  // exactly what it was doing ("invalid hook \"hooks\": command hook must
  // specify 'command'"), leaving Minni with zero hooks on every Antigravity
  // surface. Any non-hook key here is the same hazard, so there must be
  // exactly one and it must be the hook name.
  assert.deepEqual(Object.keys(template), ["minni"]);

  const events = template.minni;
  // agy has no UserPromptSubmit and no PreCompact; PreInvocation is the
  // prompt-submit analogue and the documented injection point.
  assert.ok("PreInvocation" in events, "PreInvocation is agy's prompt-submit event");
  assert.ok(!("UserPromptSubmit" in events), "UserPromptSubmit does not exist on agy");
  assert.ok(!("PreCompact" in events), "PreCompact does not exist on agy");

  for (const [event, entries] of Object.entries(events)) {
    // Grouped vs flat is PER EVENT: only PreToolUse/PostToolUse take
    // {matcher, hooks:[]}. Using the grouped form elsewhere is what produced
    // the parse failure above.
    const handlers =
      event === "PreToolUse" || event === "PostToolUse"
        ? entries.flatMap((group) => group.hooks)
        : entries;

    if (event === "PreToolUse") {
      // Matchers ARE honored -- the old "agy drops matcher-bearing entries"
      // note was the whole-file rejection, misdiagnosed.
      assert.ok(entries[0].matcher, "PreToolUse must scope with a matcher");
    }

    for (const hook of handlers) {
      assert.equal(hook.type, "command");
      assert.ok(
        hook.command.includes("__MINNI_GEMINI_DIST__/gemini-hook.js"),
        `${event} command must run gemini-hook.js via the dist token`,
      );
      assert.ok(
        !hook.command.includes("CLAUDE_PLUGIN_ROOT"),
        "agy never DEFINES CLAUDE_PLUGIN_ROOT; it expands to empty under sh -c",
      );
      assert.ok(hook.command.endsWith(` ${event}`), "command must pass its event name");
      assert.ok(hook.timeout > 0, `${event} must bound a hook that blocks agy's loop`);
    }
  }
});

test("PreToolUse with no recall state prints exactly the explicit approve", async () => {
  const fixture = await makeFixture();
  try {
    const output = await runGeminiHook("PreToolUse", fixture, agyPreToolUsePayload());
    assert.deepEqual(output, { decision: "allow" });
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
    assert.deepEqual(disabled, { decision: "allow" });
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
    assert.equal(output.decision, "deny");
    assert.match(output.reason, /recall guard/i);
    assert.match(output.reason, /prior-fix/);
    const state = JSON.parse(await readFile(statePath, "utf8"));
    assert.equal(state.consumed, true);

    // Idempotent re-issue: the same call now allows.
    const rerun = await runGeminiHook(
      "PreToolUse",
      fixture,
      agyPreToolUsePayload({
        toolCall: { name: "run_command", args: { CommandLine: "grep foo bar.txt" } },
      }),
      { MINNI_RECALL_GUARD_MODE: "strict" },
    );
    assert.deepEqual(rerun, { decision: "allow" });
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("enrichAgyStopPayload pulls the LAST explicit user message from the transcript", async () => {
  const fixture = await makeFixture();
  try {
    const transcript = path.join(fixture.root, "transcript_full.jsonl");
    await writeFile(
      transcript,
      [
        JSON.stringify({
          source: "USER_EXPLICIT",
          type: "USER_INPUT",
          content: "<USER_REQUEST>\nfirst prompt\n</USER_REQUEST>\n<ADDITIONAL_METADATA>time</ADDITIONAL_METADATA>",
        }),
        JSON.stringify({ source: "SYSTEM", type: "EPHEMERAL_MESSAGE", content: "reminder noise" }),
        "{ not valid json",
        JSON.stringify({
          source: "USER_EXPLICIT",
          type: "USER_INPUT",
          content: "<USER_REQUEST>\nfix the gemini hooks per issue 133\n</USER_REQUEST>",
        }),
        JSON.stringify({ source: "MODEL", type: "PLANNER_RESPONSE", content: "ok" }),
      ].join("\n"),
    );
    const enriched = await enrichAgyStopPayload({ transcriptPath: transcript });
    assert.equal(enriched.last_user_message, "fix the gemini hooks per issue 133");

    // Missing file and pre-existing task text both leave the payload untouched.
    const missing = await enrichAgyStopPayload({ transcriptPath: path.join(fixture.root, "nope.jsonl") });
    assert.equal(missing.last_user_message, undefined);
    const preset = await enrichAgyStopPayload({ transcriptPath: transcript, summary: "already set" });
    assert.equal(preset.last_user_message, undefined);
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("enrichAgyPromptPayload surfaces transcript text as prompt for PreInvocation", async () => {
  // PreInvocation has no prompt field; without this, handleUserPromptSubmit
  // returns noIntent on every real agy turn.
  const fixture = await makeFixture();
  try {
    const transcript = path.join(fixture.root, "transcript_full.jsonl");
    await writeFile(
      transcript,
      JSON.stringify({
        source: "USER_EXPLICIT",
        type: "USER_INPUT",
        content: "<USER_REQUEST>\nwhat is my active plan\n</USER_REQUEST>",
      }) + "\n",
    );
    const enriched = await enrichAgyPromptPayload({ transcriptPath: transcript });
    assert.equal(enriched.prompt, "what is my active plan");

    const preset = await enrichAgyPromptPayload({
      transcriptPath: transcript,
      prompt: "already set",
    });
    assert.equal(preset.prompt, "already set");
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("guard defaults to strict on agy: read/search run_command is denied without an env override", async () => {
  const fixture = await makeFixture();
  try {
    const runtimeDir = path.join(fixture.vault, ".runtime");
    await mkdir(runtimeDir, { recursive: true });
    await writeFile(
      path.join(runtimeDir, "recall-state.json"),
      JSON.stringify({
        task_signature: "t-default-mode",
        intent: "status",
        top_hits: [{ title: "Prior fix", wikilink: "[[prior-fix]]", score: 0.91 }],
        top_score: 0.91,
        consumed: false,
        ts: new Date().toISOString(),
      }),
    );
    // Empty string behaves as unset for the gemini default (and shields the
    // test from any MINNI_RECALL_GUARD_MODE in the runner's environment).
    const output = await runGeminiHook(
      "PreToolUse",
      fixture,
      agyPreToolUsePayload({
        toolCall: { name: "run_command", args: { CommandLine: "grep foo bar.txt" } },
      }),
      { MINNI_RECALL_GUARD_MODE: "" },
    );
    assert.equal(output.decision, "deny");

    // An explicit soft override is honored: Bash stays unguarded there.
    await writeFile(
      path.join(runtimeDir, "recall-state.json"),
      JSON.stringify({
        task_signature: "t-soft-mode",
        intent: "status",
        top_hits: [{ title: "Prior fix", wikilink: "[[prior-fix]]", score: 0.91 }],
        top_score: 0.91,
        consumed: false,
        ts: new Date().toISOString(),
      }),
    );
    const soft = await runGeminiHook(
      "PreToolUse",
      fixture,
      agyPreToolUsePayload({
        toolCall: { name: "run_command", args: { CommandLine: "grep foo bar.txt" } },
      }),
      { MINNI_RECALL_GUARD_MODE: "soft" },
    );
    assert.deepEqual(soft, { decision: "allow" });
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("vault default mirrors propagate's legacy fallback when only ~/.gemini/minni-vault has data", async () => {
  const fixture = await makeFixture();
  try {
    const legacyVault = path.join(fixture.root, ".gemini", "minni-vault");
    await mkdir(legacyVault, { recursive: true });
    await writeFile(path.join(legacyVault, "log.md"), "# legacy memory\n");
    // HOME points into the fixture and MINNI_GEMINI_VAULT_PATH is cleared, so
    // the hook resolves its default: canonical (~/.minni/gemini-vault) is
    // missing, legacy exists with content -> the hook must use the legacy
    // vault, exactly like propagate.vault_for("gemini").
    const env = {
      ...process.env,
      HOME: fixture.root,
      MINNI_HOME: fixture.root,
      MINNI_GEMINI_VAULT_PATH: "",
      MINNI_SOCKET_PATH: path.join(fixture.root, "missing.sock"),
      MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
      MINNI_BYPASS_AUDIT_LIMIT: "true",
    };
    delete env.MINNI_GEMINI_VAULT_PATH;
    const child = execFileAsync(process.execPath, [GEMINI_HOOK_JS, "Stop"], { env, timeout: 30_000 });
    child.child.stdin.end(JSON.stringify(agyPreToolUsePayload({ toolCall: null })));
    const { stdout } = await child;
    const output = JSON.parse(stdout.trim().split("\n").at(-1));
    // agy's no-op is a bare {} -- it has no `continue` field. This test is
    // about vault resolution; the assertion just pins that the hook ran.
    assert.equal(output.continue, undefined);
    const legacyLog = await readFile(path.join(legacyVault, "log.md"), "utf8");
    assert.ok(legacyLog.includes("hook_gemini"), "hook must write to the legacy vault, not a fresh canonical one");
    const canonical = path.join(fixture.root, ".minni", "gemini-vault");
    const canonicalEntries = await readdir(canonical).catch(() => null);
    assert.equal(canonicalEntries, null, "no fresh canonical vault may be created on a legacy install");
  } finally {
    await rm(fixture.root, { recursive: true, force: true });
  }
});

test("Stop drafts candidates under gemini's own identity stamps", async () => {
  const fixture = await makeFixture();
  try {
    const output = await runGeminiHook("Stop", fixture, agyPreToolUsePayload({ toolCall: null }));
    // agy has no `continue` field -- its passive-event no-op is a bare {}, and
    // anything it does surface travels as injectSteps. Asserting Claude's
    // `continue: true` here is what the shared layer used to emit blindly.
    assert.ok(
      output.continue === undefined,
      "agy output must not carry Claude Code's `continue` field",
    );
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
