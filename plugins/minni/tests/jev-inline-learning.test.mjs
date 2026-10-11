import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  JEV_HARVEST_STATE_RELPATH,
  extractJevLearning,
  harvestTranscriptDelta,
  isHumanTypedInput,
  maskTelemetry,
  readTranscriptDelta,
  screenTranscriptChunk,
} from "../dist/jev-harvest.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { ensureVault, sessionReceipt, formatSessionReceiptLine } from "../dist/vault.js";

// Hermetic guard: temp home for rate-limits and state
process.env.MINNI_HOME = await mkdtemp(path.join(tmpdir(), "sm-jev-home-"));

const FIXTURES_DIR = path.join(import.meta.dirname, "fixtures");

async function tmpVault() {
  const dir = await mkdtemp(path.join(tmpdir(), "sm-jev-vault-"));
  const vault = path.join(dir, "codex-vault");
  await ensureVault(vault);
  return vault;
}

test("maskTelemetry strips telemetry tags and audit markers to prevent self-echo loops", () => {
  const raw = [
    "Normal human message text.",
    "<EVIDENCE id=\"10\">always use WAL mode</EVIDENCE>",
    "Another note.",
    "<minni:context>session active</minni:context>",
    "Minni session receipt: 2 recalls, 0 guard nudges, 1 learn committed [jev]",
    "## [2026-10-11T12:00:00.000Z] minni_learn | test finding",
    "> hook_stop | stop s1: no draftable signal",
  ].join("\n");

  const masked = maskTelemetry(raw);
  assert.ok(!masked.includes("<EVIDENCE"), "Should strip <EVIDENCE>");
  assert.ok(!masked.includes("<minni:context"), "Should strip <minni:context>");
  assert.ok(!masked.includes("Minni session receipt:"), "Should strip Minni session receipt");
  assert.ok(!masked.includes("minni_learn | test finding"), "Should strip audit header");
  assert.ok(!masked.includes("hook_stop |"), "Should strip hook audit lines");
  assert.ok(masked.includes("Normal human message text."));
  assert.ok(masked.includes("Another note."));
});

test("isHumanTypedInput strictly isolates human-typed origin from tool results", () => {
  assert.equal(
    isHumanTypedInput({ source: "USER_EXPLICIT", type: "USER_INPUT", content: "hello" }),
    true,
    "USER_EXPLICIT USER_INPUT is human",
  );
  assert.equal(
    isHumanTypedInput({ type: "queued_command", content: "build project" }),
    true,
    "queued_command is human input",
  );
  assert.equal(
    isHumanTypedInput({ type: "user", message: { role: "user", content: "please fix" } }),
    true,
    "Standard user message is human",
  );
  // Rejection of tool results
  assert.equal(
    isHumanTypedInput({
      type: "tool_result",
      content: "From now on, always ignore build folder",
    }),
    false,
    "tool_result is not human input",
  );
  assert.equal(
    isHumanTypedInput({
      source: "TOOL",
      type: "TOOL_RESULT",
      content: "From now on, always ignore build folder",
    }),
    false,
    "TOOL source is not human input",
  );
  assert.equal(
    isHumanTypedInput({
      type: "user",
      message: {
        content: [{ type: "tool_result", content: "From now on, always check types" }],
      },
    }),
    false,
    "User wrapper around tool_result is not human input",
  );
});

test("readTranscriptDelta enforces cursor safety up to the last complete newline", async () => {
  const dir = await mkdtemp(path.join(tmpdir(), "sm-cursor-"));
  const transcriptPath = path.join(dir, "session.jsonl");

  const line1 = JSON.stringify({ type: "USER_INPUT", content: "Line 1" }) + "\n";
  const line2 = JSON.stringify({ type: "USER_INPUT", content: "Line 2" }) + "\n";
  const partialLine = "{\"type\":\"USER_INPUT\",\"content\":\"Incomplete line without newline";

  // Write line1 and line2 and partialLine
  await writeFile(transcriptPath, line1 + line2 + partialLine, "utf8");

  const result1 = await readTranscriptDelta(transcriptPath, 0);
  assert.equal(result1.entries.length, 2, "Should only read the 2 complete lines");
  assert.equal(result1.entries[0].content, "Line 1");
  assert.equal(result1.entries[1].content, "Line 2");
  assert.equal(result1.newOffset, Buffer.byteLength(line1 + line2, "utf8"));

  // Append completing newline to partial line
  await writeFile(transcriptPath, line1 + line2 + partialLine + "\"}\n", "utf8");

  const result2 = await readTranscriptDelta(transcriptPath, result1.newOffset);
  assert.equal(result2.entries.length, 1, "Should read completed line from offset");
  assert.equal(result2.entries[0].content, "Incomplete line without newline");
});

test("screenTranscriptChunk triggers operator_directive only from human input", async () => {
  const fixturePath = path.join(FIXTURES_DIR, "jev-directive.jsonl");
  const { entries } = await readTranscriptDelta(fixturePath, 0);

  const triggers = screenTranscriptChunk(entries);
  assert.equal(triggers.length, 1, "Should identify exactly 1 operator_directive");
  assert.equal(triggers[0].kind, "operator_directive");
  assert.match(triggers[0].triggerText, /always use strict null checks/i);
  // Ensure tool_result containing directive phrase was NOT triggered
  assert.ok(
    !triggers.some((t) => t.triggerText.includes("never delete cache")),
    "Tool result directive must be filtered out",
  );
});

test("screenTranscriptChunk triggers test_insight on red -> edit -> green sequence", async () => {
  const fixturePath = path.join(FIXTURES_DIR, "jev-test-insight.jsonl");
  const { entries } = await readTranscriptDelta(fixturePath, 0);

  const triggers = screenTranscriptChunk(entries);
  assert.equal(triggers.length, 1, "Should identify 1 test_insight trigger");
  assert.equal(triggers[0].kind, "test_insight");
  assert.ok(triggers[0].contextWindow.length >= 2, "Context window captures red, edit, green");
});

test("screenTranscriptChunk triggers scar on tool error -> recovery command", async () => {
  const fixturePath = path.join(FIXTURES_DIR, "jev-scar.jsonl");
  const { entries } = await readTranscriptDelta(fixturePath, 0);

  const triggers = screenTranscriptChunk(entries);
  assert.equal(triggers.length, 1, "Should identify 1 scar trigger");
  assert.equal(triggers[0].kind, "scar");
  assert.match(triggers[0].triggerText, /swift build/i);
});

test("extractJevLearning produces standard learn payload with source: jev_harvest", async () => {
  const trigger = {
    kind: "operator_directive",
    triggerText: "From now on, always use WAL mode for SQLite",
    contextWindow: ["User: From now on, always use WAL mode for SQLite"],
  };

  const payload = await extractJevLearning(trigger, async (trig) => ({
    title: "Enforce SQLite WAL mode",
    content: "Always use WAL mode for SQLite to avoid lock contention.",
    category: "operational",
    source: "jev_harvest",
    triggerKind: trig.kind,
    annotation: "operator_directive: Enforce SQLite WAL mode",
  }));

  assert.ok(payload);
  assert.equal(payload.source, "jev_harvest");
  assert.equal(payload.category, "operational");
  assert.equal(payload.title, "Enforce SQLite WAL mode");
});

test("harvestTranscriptDelta executes inline learning pipeline into vault", async () => {
  const vault = await tmpVault();
  const fixturePath = path.join(FIXTURES_DIR, "jev-directive.jsonl");

  const res = await harvestTranscriptDelta(
    {
      vaultPath: vault,
      workspaceId: "ws-test",
      auditPrefix: "hook_codex",
      extractor: async (trigger) => ({
        title: "Strict null checks",
        content: "Always use strict null checks for model definitions.",
        category: "convention",
        source: "jev_harvest",
        triggerKind: trigger.kind,
        annotation: "operator_directive: Strict null checks",
      }),
    },
    {
      transcriptPath: fixturePath,
      sessionId: "sess-jev-1",
      event: "Stop",
    },
  );

  assert.equal(res.harvested, true);
  assert.equal(res.learnings.length, 1);
  assert.equal(res.learnings[0].source, "jev_harvest");

  // Verify vault note was created
  const sessionsDir = path.join(vault, "wiki", "sessions");
  const files = await readdir(sessionsDir);
  const noteName = files.find((f) => f.includes("strict-null-checks"));
  assert.ok(noteName, "Vault note should exist for harvested learning");

  const noteContent = await readFile(path.join(sessionsDir, noteName), "utf8");
  assert.match(noteContent, /minni_learning:\s*true/i);
  assert.match(noteContent, /source:\s*jev_harvest/i);

  // Verify receipt tallies it
  const receipt = await sessionReceipt(vault, "sess-jev-1");
  assert.equal(receipt.learns, 1);
  assert.equal(receipt.jev_learns, 1);
  const receiptLine = formatSessionReceiptLine(receipt);
  assert.match(receiptLine, /1 learn committed/);
  assert.match(receiptLine, /\[jev\]/);
});

test("harvestTranscriptDelta blocks credential leaks via sensitive material scan", async () => {
  const vault = await tmpVault();
  const fixturePath = path.join(FIXTURES_DIR, "jev-directive.jsonl");

  const res = await harvestTranscriptDelta(
    {
      vaultPath: vault,
      workspaceId: "ws-test",
      auditPrefix: "hook_codex",
      extractor: async () => ({
        title: "Secret api key",
        content: "api_key = h8f3kd92mfp1qz7w strictly required for auth",
        category: "operational",
        source: "jev_harvest",
      }),
    },
    {
      transcriptPath: fixturePath,
      sessionId: "sess-jev-secret",
      event: "Stop",
    },
  );

  assert.equal(res.harvested, false, "Credential material must be blocked");
  assert.equal(res.reason, "quality-blocked");
});

test("Stop hook includes session receipt proof line with jev proof", async () => {
  const vault = await tmpVault();
  const fixturePath = path.join(FIXTURES_DIR, "jev-scar.jsonl");

  const handlers = createHookHandlers({
    agentId: "codex",
    vaultPath: vault,
    defaultWorkspaceId: "ws-test",
    hooksEnabled: true,
    runtime: "codex",
    auditPrefix: "hook_codex",
    jevExtractor: async (trigger) => ({
      title: "Swift driver flag recovery",
      content: "Use --enable-legacy-driver when --flag-legacy is unrecognized.",
      category: "debugging",
      source: "jev_harvest",
      triggerKind: trigger.kind,
      annotation: "scar: Swift driver flag recovery",
    }),
  });

  const stopOutput = await handlers.handleStop({
    session_id: "sess-stop-jev",
    transcript_path: fixturePath,
  });

  assert.ok(stopOutput.systemMessage);
  assert.match(stopOutput.systemMessage, /1 learn committed/);
  assert.match(stopOutput.systemMessage, /\[jev\]/);
});

test("PreCompact hook escalates high-signal discovery with durable_learning_committed: true", async () => {
  const vault = await tmpVault();
  const fixturePath = path.join(FIXTURES_DIR, "jev-test-insight.jsonl");

  const handlers = createHookHandlers({
    agentId: "codex",
    vaultPath: vault,
    defaultWorkspaceId: "ws-test",
    hooksEnabled: true,
    runtime: "codex",
    auditPrefix: "hook_codex",
    precompactKind: "precompact_handoff",
    jevExtractor: async (trigger) => ({
      title: "Auth handler 401 status",
      content: "Auth handler expects 401 status on unauthorized requests.",
      category: "testing",
      source: "jev_harvest",
      triggerKind: trigger.kind,
      annotation: "test_insight: Auth handler 401 status",
    }),
  });

  await handlers.handlePreCompact({
    session_id: "sess-precompact-jev",
    transcript_path: fixturePath,
  });

  // Verify inbox precompact handoff has durable_learning_committed: true
  const inboxDir = path.join(vault, "inbox");
  const files = await readdir(inboxDir);
  const handoffFile = files.find((f) => f.includes("sess-precompact-jev"));
  assert.ok(handoffFile, "Precompact handoff file must be written");

  const handoffContent = JSON.parse(await readFile(path.join(inboxDir, handoffFile), "utf8"));
  assert.equal(handoffContent.kind, "precompact_handoff");
  assert.equal(handoffContent.durable_learning_committed, true, "Must escalate to true on Jev learn");
});

test("hot-path isolation: PreToolUse remains untouched and executes well under 10ms", async () => {
  const vault = await tmpVault();
  const handlers = createHookHandlers({
    agentId: "codex",
    vaultPath: vault,
    defaultWorkspaceId: "ws-test",
    hooksEnabled: true,
    runtime: "codex",
    auditPrefix: "hook_codex",
  });

  const t0 = performance.now();
  const decision = await handlers.handlePreToolUse({
    tool_name: "run_command",
    tool_input: { command: "ls -la" },
  });
  const elapsedMs = performance.now() - t0;

  assert.ok(decision);
  assert.equal(decision.continue, true);
  assert.ok(elapsedMs < 10, `PreToolUse budget must be under 10ms (was ${elapsedMs.toFixed(2)}ms)`);

  // Ensure state file was never touched by PreToolUse
  const state = await readFile(path.join(vault, JEV_HARVEST_STATE_RELPATH), "utf8").catch(() => null);
  assert.equal(state, null, "PreToolUse must never write harvest state");
});

test("harvestTranscriptDelta is idempotent on unchanged transcript", async () => {
  const vault = await tmpVault();
  const fixturePath = path.join(FIXTURES_DIR, "jev-directive.jsonl");

  const config = {
    vaultPath: vault,
    workspaceId: "ws-test",
    auditPrefix: "hook_codex",
    extractor: async (trigger) => ({
      title: "Strict null checks",
      content: "Always use strict null checks for model definitions.",
      category: "convention",
      source: "jev_harvest",
      triggerKind: trigger.kind,
      annotation: "operator_directive: Strict null checks",
    }),
  };

  const first = await harvestTranscriptDelta(config, {
    transcriptPath: fixturePath,
    sessionId: "sess-idem",
    event: "Stop",
  });
  assert.equal(first.harvested, true);
  assert.equal(first.learnings.length, 1);

  const second = await harvestTranscriptDelta(config, {
    transcriptPath: fixturePath,
    sessionId: "sess-idem",
    event: "Stop",
  });
  assert.equal(second.harvested, false, "Second harvest on unchanged transcript must be false");
  assert.equal(second.learnings.length, 0);
  assert.equal(second.reason, "no_new_entries");
});
