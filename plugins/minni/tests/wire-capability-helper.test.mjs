// Recall-to-use S1a (§4.3, §6.2): wire capabilities.
//
// Visibility and envelope fitting need host facts that differ per platform: how
// many chars an injected string may carry inline, whether PreToolUse can carry
// context, and what the transcript looks like. They live on the wire, next to
// the rest of the platform contract, and `wireCapabilities` gives every wire
// (profiled or not) a complete answer. Only claude-code is profiled in S1a, and
// `preToolUseContext` stays false until S2b flips it.
import assert from "node:assert/strict";
import test from "node:test";

import {
  claudeCodeWire,
  codexWire,
  cursorWire,
  geminiWire,
  grokBuildWire,
  kilocodeWire,
  unprofiledWire,
  wireCapabilities,
  wireFor,
} from "../dist/hook-platform.js";

const DEFAULTS = { hostInlineCapChars: undefined, preToolUseContext: false, transcriptShape: "unknown" };

test("claude-code declares its inline cap, transcript shape, and PreToolUse context", () => {
  assert.deepEqual(wireCapabilities(claudeCodeWire), {
    hostInlineCapChars: 10_000,
    preToolUseContext: true,
    transcriptShape: "claude-code",
  });
  assert.deepEqual(wireCapabilities(wireFor("claude-code")), wireCapabilities(claudeCodeWire));
});

test("every other wire gets the defaults: no known cap, no PreToolUse context", () => {
  for (const wire of [codexWire, grokBuildWire, kilocodeWire, geminiWire, cursorWire, unprofiledWire("muse")]) {
    assert.equal(wire.capabilities, undefined, `${wire.id} is not profiled in S1a`);
    assert.deepEqual(wireCapabilities(wire), DEFAULTS, wire.id);
  }
});

test("partial or invalid capability values fall back field by field", () => {
  const base = unprofiledWire("fixture-host");
  const cases = [
    [{ hostInlineCapChars: 4_000 }, { ...DEFAULTS, hostInlineCapChars: 4_000 }],
    [{ hostInlineCapChars: 0 }, DEFAULTS],
    [{ hostInlineCapChars: -5 }, DEFAULTS],
    [{ hostInlineCapChars: 12.5 }, DEFAULTS],
    [{ hostInlineCapChars: Number.POSITIVE_INFINITY }, DEFAULTS],
    [{ hostInlineCapChars: "10000" }, DEFAULTS],
    [{ preToolUseContext: true }, { ...DEFAULTS, preToolUseContext: true }],
    [{ preToolUseContext: "yes" }, DEFAULTS],
    [{ transcriptShape: "" }, DEFAULTS],
    [{ transcriptShape: 3 }, DEFAULTS],
    [{ transcriptShape: "codex" }, { ...DEFAULTS, transcriptShape: "codex" }],
  ];
  for (const [capabilities, expected] of cases) {
    assert.deepEqual(wireCapabilities({ ...base, capabilities }), expected, JSON.stringify(capabilities));
  }
});

test("the helper returns a fresh object, so callers cannot mutate the wire", () => {
  const caps = wireCapabilities(claudeCodeWire);
  caps.hostInlineCapChars = 1;
  assert.equal(wireCapabilities(claudeCodeWire).hostInlineCapChars, 10_000);
});

test("the claude-code wire contract is otherwise unchanged", () => {
  assert.deepEqual(claudeCodeWire.noop(), { continue: true });
  assert.equal(claudeCodeWire.inject("PreToolUse", "x"), null, "PreToolUse context stays off in S1a");
  assert.deepEqual(claudeCodeWire.inject("SessionStart", "x"), {
    continue: true,
    hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: "x" },
  });
});
