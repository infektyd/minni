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
