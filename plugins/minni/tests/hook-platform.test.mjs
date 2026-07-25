// Per-platform wire contracts.
//
// These encode docs/contracts/hook-platforms.md as executable assertions. They
// exist because the failure they guard against is INVISIBLE at runtime: every
// platform here silently discards output it does not understand, so a
// regression looks exactly like a healthy run. Only a test can tell them apart.

import test from "node:test";
import assert from "node:assert/strict";

import {
  claudeCodeWire,
  codexWire,
  grokBuildWire,
  kilocodeWire,
  geminiWire,
  cursorWire,
  renderIntent,
  wireFor,
} from "../dist/hook-platform.js";
import { injectIntent, noteIntent, noIntent } from "../dist/hook-intent.js";

const ctx = (out) => out?.hookSpecificOutput?.additionalContext;

test("wireFor resolves each platform by agent id", () => {
  assert.equal(wireFor("claude-code").id, "claude-code");
  assert.equal(wireFor("codex").id, "codex");
  assert.equal(wireFor("grok-build").id, "grok-build");
  assert.equal(wireFor("kilocode").id, "kilocode");
});

test("wireFor falls back to the Claude shape for an unknown platform", () => {
  // Narrow but deliberate: Codex and Grok Build are both Claude-contract
  // clones, so it is the least-bad guess. Add a profile rather than lean on it.
  assert.equal(wireFor("some-future-agent").id, "claude-code");
});

test("Claude Code injects at SessionStart/UserPromptSubmit/Stop but NOT PreCompact", () => {
  for (const event of ["SessionStart", "UserPromptSubmit", "Stop"]) {
    assert.equal(ctx(claudeCodeWire.inject(event, "memory")), "memory", event);
  }
  // PreCompact is absent from the documented hookSpecificOutput union.
  assert.equal(claudeCodeWire.inject("PreCompact", "memory"), null);
});

test("Codex cannot inject at Stop or PreCompact", () => {
  assert.equal(ctx(codexWire.inject("SessionStart", "memory")), "memory");
  assert.equal(ctx(codexWire.inject("UserPromptSubmit", "memory")), "memory");
  // stop.command.output has no hookSpecificOutput key and the schema is
  // additionalProperties:false -- emitting one voids the ENTIRE output.
  assert.equal(codexWire.inject("Stop", "memory"), null);
  assert.equal(codexWire.inject("PreCompact", "memory"), null);
});

test("Grok Build injects ONLY at Stop; passive-event stdout is ignored", () => {
  // xAI: "For passive events, stdout is ignored; exit 0 on success."
  assert.equal(grokBuildWire.inject("SessionStart", "memory"), null);
  assert.equal(grokBuildWire.inject("UserPromptSubmit", "memory"), null);
  assert.equal(grokBuildWire.inject("PreCompact", "memory"), null);
  assert.equal(ctx(grokBuildWire.inject("Stop", "memory")), "memory");
});

test("Kilocode injects via the bridge, including at PreCompact", () => {
  // The consumer is Minni's own kilo/minni-plugin.js, which reads
  // hookSpecificOutput.additionalContext and pushes it into opencode's
  // output.system / output.context.
  assert.equal(ctx(kilocodeWire.inject("SessionStart", "memory")), "memory");
  assert.equal(ctx(kilocodeWire.inject("UserPromptSubmit", "memory")), "memory");
  // The one platform that CAN inject at PreCompact
  // (experimental.session.compacting accepts replacement context).
  assert.equal(ctx(kilocodeWire.inject("PreCompact", "memory")), "memory");
});

test("every wire reads the ASSISTANT message, under its own spelling", () => {
  // `last_user_message` exists on no platform; reading it always yielded "".
  assert.equal(
    claudeCodeWire.lastTaskText({ last_assistant_message: "done" }),
    "done",
  );
  assert.equal(codexWire.lastTaskText({ last_assistant_message: "done" }), "done");
  // Grok's envelope is camelCase throughout.
  assert.equal(grokBuildWire.lastTaskText({ lastAssistantMessage: "done" }), "done");

  for (const wire of [claudeCodeWire, codexWire, grokBuildWire, kilocodeWire]) {
    assert.equal(
      wire.lastTaskText({ last_user_message: "nope" }),
      "",
      `${wire.id} must not resurrect the field that exists nowhere`,
    );
  }
});

test("renderIntent reports a drop instead of silently swallowing it", () => {
  const { output, dropped } = renderIntent(grokBuildWire, injectIntent("SessionStart", "m"));

  assert.deepEqual(output, { continue: true }, "must still emit a valid no-op");
  assert.ok(dropped, "an undeliverable injection MUST be reported");
  assert.equal(dropped.event, "SessionStart");
  assert.match(dropped.reason, /grok-build/);
});

test("renderIntent reports no drop when the platform can carry the intent", () => {
  const { output, dropped } = renderIntent(claudeCodeWire, injectIntent("SessionStart", "m"));

  assert.equal(ctx(output), "m");
  assert.equal(dropped, undefined);
});

test("renderIntent passes notes and no-ops through", () => {
  const note = renderIntent(claudeCodeWire, noteIntent("2 candidates drafted"));
  assert.equal(note.output.systemMessage, "2 candidates drafted");
  assert.equal(note.dropped, undefined);

  const none = renderIntent(claudeCodeWire, noIntent);
  assert.deepEqual(none.output, { continue: true });
  assert.equal(none.dropped, undefined);
});

test("a note on Grok Build is dropped, not silently discarded", () => {
  // Grok parses stdout only on the Stop gate; a note has no other channel.
  const { dropped } = renderIntent(grokBuildWire, noteIntent("hello"));
  assert.equal(dropped, undefined, "notes probe the Stop channel, which Grok parses");
});

test("agy: injectSteps is valid on SessionStart but NOT on Stop", () => {
  // Verified live against agy 1.1.7: SessionStart accepted the injectSteps
  // payload, while Stop rejected it -- its output is a different proto.
  //     failed to unmarshal result from hook jsonhook__minni_Stop_0_0 via
  //     protojson: ... unknown field "injectSteps"
  const steps = (out) => out?.injectSteps?.[0]?.ephemeralMessage;

  assert.equal(steps(geminiWire.inject("SessionStart", "memory")), "memory");
  assert.equal(steps(geminiWire.inject("UserPromptSubmit", "memory")), "memory");
  assert.equal(geminiWire.inject("Stop", "memory"), null);
  assert.equal(geminiWire.note("Stop", "2 candidates"), null);

  // agy has no `continue` field at all; its no-op is a bare {}.
  assert.deepEqual(geminiWire.noop(), {});
});

test("agy: a Stop note is dropped and recorded, never emitted as a parse error", () => {
  const { output, dropped } = renderIntent(geminiWire, noteIntent("2 candidates"));

  assert.deepEqual(output, {}, "must emit agy's bare no-op, not injectSteps");
  assert.ok(dropped, "the undeliverable note must be recorded");
});

test("Cursor injects at sessionStart only, and notes ride followup_message", () => {
  // Verified against cursor.com/docs: sessionStart takes additional_context;
  // beforeSubmitPrompt takes continue + user_message ONLY; preCompact takes
  // user_message; stop takes followup_message.
  assert.deepEqual(cursorWire.inject("SessionStart", "memory"), { additional_context: "memory" });
  assert.equal(cursorWire.inject("UserPromptSubmit", "memory"), null);
  assert.equal(cursorWire.inject("PreCompact", "memory"), null);
  assert.equal(cursorWire.inject("Stop", "memory"), null);

  // Cursor has no safe note channel: `followup_message` is auto-follow-up, not
  // a message. Returning it from Stop drove a self-feeding loop -- 6 Stop hooks
  // in 90s from one prompt, each drafting a candidate from the audit trail the
  // last one wrote. Never reintroduce this.
  assert.equal(cursorWire.note("Stop", "2 candidates"), null);
  assert.equal(cursorWire.note("SessionStart", "2 candidates"), null);
});

test("Cursor: a Stop note is dropped, never sent as followup_message", () => {
  const { output, dropped } = renderIntent(cursorWire, noteIntent("1 candidate drafted"));

  assert.equal(
    output.followup_message,
    undefined,
    "followup_message continues the agent -- announcing a candidate must never cost a turn",
  );
  assert.ok(dropped, "the undeliverable note must still be recorded");
});

test("Cursor resolves to its OWN wire, not the Claude fallback", () => {
  // Regression: cursor previously fell through wireFor() to claudeCodeWire, so
  // handlers emitted Claude envelopes that the adapter then discarded with no
  // record. The fallback must stay a safety net, never load-bearing.
  assert.equal(wireFor("cursor").id, "cursor");
});

test("Cursor: an undeliverable prompt-submit injection is RECORDED", () => {
  const { output, dropped } = renderIntent(cursorWire, injectIntent("UserPromptSubmit", "memory"));

  assert.deepEqual(output, { continue: true });
  assert.ok(dropped, "Cursor cannot inject at beforeSubmitPrompt -- that must be visible");
  assert.equal(dropped.event, "UserPromptSubmit");
});

test("every platform's audit prefix is distinct and namespaced", async () => {
  // kilocode shipped `auditPrefix: "hook"`, so its entries were shaped exactly
  // like claude-code's. Separate vaults hid the collision, but it defeats any
  // cross-vault query and it cost a real misdiagnosis: a grep for
  // hook_kilocode_* found nothing and the integration looked dead when it was
  // in fact working.
  const { readFile } = await import("node:fs/promises");
  const path = await import("node:path");
  const { fileURLToPath } = await import("node:url");
  const SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src");

  const entries = {
    "codex-hook.ts": "hook_codex",
    "grok-hook.ts": "hook_grok",
    "cursor-hook.ts": "hook_cursor",
    "gemini-hook.ts": "hook_gemini",
    "kilocode-hook.ts": "hook_kilocode",
  };

  const seen = new Set();
  for (const [file, expected] of Object.entries(entries)) {
    const src = await readFile(path.join(SRC, file), "utf8");
    const found = /auditPrefix:\s*"([^"]+)"/.exec(src)?.[1];
    assert.equal(found, expected, `${file} audit prefix`);
    assert.ok(found.startsWith("hook_"), `${file}: throttle keys off the hook_ prefix`);
    assert.ok(!seen.has(found), `${file}: duplicate audit prefix ${found}`);
    seen.add(found);
  }
});
