// Regression tests (RED -> GREEN) for the 3 live telemetry & recall-guard seams:
//   Seam 1: Multi-host use-detector & transcript shape parity (gemini, codex, grok-build + no_transcript classification)
//   Seam 2: Gemini/Antigravity PreInvocation step-loop idempotency & consult disarm parity (call_mcp_tool + MCP server)
//   Seam 3: Session key & agent key canonicalization across snake_case / camelCase / host fields
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  claudeCodeWire,
  codexWire,
  geminiWire,
  grokBuildWire,
  wireCapabilities,
} from "../dist/hook-platform.js";
import { enrichAgyPromptPayload } from "../dist/gemini-adapter.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import { isConsultTool } from "../dist/recall-guard.js";
import {
  armingKeyFor,
  claimArming,
  readRecallState,
  writeRecallState,
} from "../dist/recall-state.js";
import * as recallStateMod from "../dist/recall-state.js";
import { runDetector } from "../dist/use-detector.js";
import {
  agentKeyFrom,
  appendLedgerRows,
  buildSurfacedRow,
  buildTurnRow,
  ledgerContextFrom,
  readSessionLedger,
  sessionKeyFrom,
} from "../dist/use-ledger.js";
import { buildUseReport } from "../dist/use-report.js";
import { writeDfSnapshot } from "./fixtures/use-detector-fixtures.mjs";
import { daemonResult, makeFixture, readLedger, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

delete process.env.MINNI_USE_LEDGER;

const execFileAsync = promisify(execFile);
const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const GEMINI_HOOK_JS = path.join(PLUGIN_ROOT, "dist", "gemini-hook.js");
const GROK_HOOK_JS = path.join(PLUGIN_ROOT, "dist", "grok-hook.js");
const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");

async function makeTempVault(t, prefix = "minni-live-seams-") {
  const root = await mkdtemp(path.join(tmpdir(), prefix));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, "wiki", "concepts"), { recursive: true });
  return { root, vault };
}

// ============================================================================
// SEAM 3: Session Key & Agent Key Canonicalization
// ============================================================================

test("Seam 3: sessionKeyFrom canonicalizes session_id, sessionId, conversationId and transcript path variants", async (t) => {
  const { root } = await makeTempVault(t, "seam3-session-key-");
  const expectedSessionKey = sha256("session-alpha-01").slice(0, 16);

  assert.equal(sessionKeyFrom({ session_id: "session-alpha-01" }), expectedSessionKey);
  assert.equal(sessionKeyFrom({ sessionId: "session-alpha-01" }), expectedSessionKey, "camelCase sessionId must resolve to same key");
  assert.equal(sessionKeyFrom({ conversationId: "session-alpha-01" }), expectedSessionKey, "Antigravity conversationId must resolve to same key");
  assert.equal(
    sessionKeyFrom({ session_id: "", sessionId: "session-alpha-01" }),
    expectedSessionKey,
    "empty session_id falls through to sessionId",
  );

  const txPath = path.join(root, "transcript_full.jsonl");
  await writeFile(txPath, "", "utf8");
  const viaSnake = sessionKeyFrom({ transcript_path: txPath });
  assert.match(viaSnake, /^tx-[0-9a-f]{16}$/);
  assert.equal(sessionKeyFrom({ transcriptPath: txPath }), viaSnake, "camelCase transcriptPath must resolve to same tx- key");
  assert.equal(sessionKeyFrom({ agent_transcript_path: txPath }), viaSnake, "agent_transcript_path fallback must resolve to same tx- key");
  assert.equal(sessionKeyFrom({ agentTranscriptPath: txPath }), viaSnake, "agentTranscriptPath fallback must resolve to same tx- key");
});

test("Seam 3: agentKeyFrom and ledgerContextFrom canonicalize camelCase subagent and prompt fields", async (t) => {
  const { vault } = await makeTempVault(t, "seam3-agent-key-");
  const expectedAgentKey = `a-${sha256("sub-42").slice(0, 12)}`;

  assert.equal(agentKeyFrom({ agent_id: "agent-sub-42" }), expectedAgentKey);
  assert.equal(agentKeyFrom({ agentId: "agent-sub-42" }), expectedAgentKey, "camelCase agentId must resolve to same a- key");
  assert.equal(
    agentKeyFrom({ transcriptPath: "/tmp/proj/subagents/agent-sub-42.jsonl" }),
    expectedAgentKey,
    "camelCase transcriptPath under subagents/ must resolve to same a- key",
  );
  assert.equal(
    agentKeyFrom({ agent_transcript_path: "/tmp/proj/agent-sub-42.jsonl" }),
    expectedAgentKey,
    "agent_transcript_path must resolve to same a- key",
  );
  assert.equal(
    agentKeyFrom({ agentTranscriptPath: "/tmp/proj/agent-sub-42.jsonl" }),
    expectedAgentKey,
    "camelCase agentTranscriptPath must resolve to same a- key",
  );

  const ctx = ledgerContextFrom(
    {
      conversationId: "conv-99",
      agentId: "agent-sub-42",
      agentType: "recon",
      promptId: "p-camel-1",
    },
    { wire: "gemini", principal: "gemini", vaultPath: vault },
  );
  assert.equal(ctx.session_key, sha256("conv-99").slice(0, 16));
  assert.equal(ctx.agent_key, expectedAgentKey);
  assert.equal(ctx.subagent_type, "recon");
  assert.equal(ctx.prompt_id, "p-camel-1");
});

// ============================================================================
// SEAM 1: Multi-Host Use-Detector & Transcript Shape Parity
// ============================================================================

test("Seam 1a: gemini, codex, and grok-build wires declare their transcriptShape capability", () => {
  assert.equal(wireCapabilities(claudeCodeWire).transcriptShape, "claude-code");
  assert.equal(wireCapabilities(geminiWire).transcriptShape, "gemini");
  assert.equal(wireCapabilities(codexWire).transcriptShape, "codex");
  assert.equal(wireCapabilities(grokBuildWire).transcriptShape, "grok-build");
});

test("Seam 1b: use-detector parses Antigravity/Gemini transcript_full.jsonl and judges open surfacings + explicit_recall", async (t) => {
  const { root, vault } = await makeTempVault(t, "seam1-gemini-detector-");
  await writeDfSnapshot(vault);

  const noteCiteRel = "wiki/concepts/cobalt-beacon-quorum.md";
  const noteOpenRel = "wiki/concepts/amber-valve-backpressure.md";
  const noteRecallRel = "wiki/concepts/zircon-shard-lease.md";
  await writeFile(path.join(vault, noteCiteRel), "# Cobalt beacon quorum\n\nQuorum requires three nodes.\n", "utf8");
  await writeFile(path.join(vault, noteOpenRel), "# Amber valve backpressure\n\nThrottle at eighty percent.\n", "utf8");
  await writeFile(path.join(vault, noteRecallRel), "# Zircon shard lease\n\nLeases expire after ten seconds.\n", "utf8");

  const sessionId = "agy-session-1001";
  const sessionKey = sessionKeyFrom({ conversationId: sessionId });
  const ctx = {
    session_key: sessionKey,
    agent_key: "main",
    wire: "gemini",
    principal: "gemini",
    features: ["MINNI_USE_LEDGER"],
    prompt_id: "agy-step-0",
  };

  const t0 = Date.parse("2026-10-27T12:00:00.000Z");
  const iso = (offsetMs) => new Date(t0 + offsetMs).toISOString();

  const turn = buildTurnRow(ctx, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "strong" }, iso(100));
  const surfacedCite = buildSurfacedRow(
    ctx,
    {
      channel: "prompt_pointer",
      mkey: `vault:${noteCiteRel}`,
      title: "Cobalt beacon quorum",
      rank: 0,
      visible: true,
      delivered: true,
      turn_ix: 1,
    },
    iso(110),
  );
  const surfacedOpen = buildSurfacedRow(
    ctx,
    {
      channel: "prompt_pointer",
      mkey: `vault:${noteOpenRel}`,
      title: "Amber valve backpressure",
      rank: 1,
      visible: true,
      delivered: true,
      turn_ix: 1,
    },
    iso(120),
  );
  appendLedgerRows(vault, [turn, surfacedCite, surfacedOpen]);

  const transcriptPath = path.join(root, "transcript_full.jsonl");
  const agyLines = [
    {
      step_index: 0,
      source: "USER_EXPLICIT",
      type: "USER_INPUT",
      status: "DONE",
      created_at: iso(0),
      content: "<USER_REQUEST>\nCheck the cluster and flow settings.\n</USER_REQUEST>",
    },
    {
      step_index: 1,
      source: "SYSTEM_SDK",
      type: "EPHEMERAL_MESSAGE",
      status: "DONE",
      created_at: iso(50),
      content: "<minni:context event=\"UserPromptSubmit\">pointer</minni:context>",
    },
    {
      step_index: 2,
      source: "MODEL",
      type: "PLANNER_RESPONSE",
      status: "DONE",
      created_at: iso(500),
      thinking: "Checking files and recalling shard lease.",
      tool_calls: [
        {
          name: "read_file",
          args: { Path: JSON.stringify(path.join(vault, noteOpenRel)) },
        },
        {
          name: "call_mcp_tool",
          args: {
            ServerName: JSON.stringify("minni"),
            ToolName: JSON.stringify("minni_recall"),
            Arguments: JSON.stringify({ query: "zircon shard lease" }),
          },
        },
      ],
    },
    {
      step_index: 3,
      source: "MODEL",
      type: "GENERIC",
      status: "DONE",
      created_at: iso(600),
      content: "Created At: 2026-10-27T12:00:00Z\n# Amber valve backpressure\n",
    },
    {
      step_index: 4,
      source: "MODEL",
      type: "GENERIC",
      status: "DONE",
      created_at: iso(700),
      content: [
        "Created At: 2026-10-27T12:00:00Z",
        "Completed At: 2026-10-27T12:00:00Z",
        "# Minni Recall",
        "",
        "Query: zircon shard lease [faiss-disk]",
        "",
        "## AI Context Pack",
        "",
        "1. [[wiki/concepts/zircon-shard-lease]] — Zircon shard lease (score=0.92)",
        `   <EVIDENCE source="${noteRecallRel}" agent="gemini" status="accepted" privacy="safe" score="0.920">lease snippet</EVIDENCE>`,
      ].join("\n"),
    },
    {
      step_index: 5,
      source: "MODEL",
      type: "PLANNER_RESPONSE",
      status: "DONE",
      created_at: iso(900),
      content: "The cobalt beacon quorum requires three nodes, and the zircon shard lease expires after ten seconds.",
    },
  ];
  await writeFile(transcriptPath, agyLines.map((l) => JSON.stringify(l)).join("\n") + "\n", "utf8");

  const res = runDetector({
    vaultPath: vault,
    wire: "gemini",
    principal: "gemini",
    session_key: sessionKey,
    agent_key: "main",
    transcriptPath,
    now: iso(2000),
  });
  assert.equal(res.status, "ran");
  assert.ok(res.judged_written >= 3, `expected >= 3 judged rows, got ${res.judged_written}`);
  assert.equal(res.surfaced_written, 1, "explicit_recall surfaced row rebuilt from call_mcp_tool(minni_recall)");

  const { rows } = readSessionLedger(vault, sessionKey);
  const judged = new Map(rows.filter((r) => r.kind === "judged" && r.final === true).map((r) => [r.sid, r]));
  assert.equal(judged.get(surfacedCite.sid)?.outcome, "cited");
  assert.equal(judged.get(surfacedOpen.sid)?.outcome, "opened");

  const explicitSurfaced = rows.find((r) => r.kind === "surfaced" && r.channel === "explicit_recall");
  assert.ok(explicitSurfaced, "explicit_recall surfaced row exists");
  assert.equal(judged.get(explicitSurfaced.sid)?.outcome, "cited");

  const report = buildUseReport(vault, { days: 7, now: iso(3000) });
  assert.equal(report.principal, "gemini");
  assert.equal(report.coverage.find((c) => c.wire === "gemini")?.status, "measured");
  assert.equal(report.surfacings.total, 3);
});

test("Seam 1c: use-detector parses Codex rollout JSONL, Grok updates.jsonl, and classifies missing transcripts as unknown(no_transcript)", async (t) => {
  // 1. Codex rollout JSONL
  {
    const { root, vault } = await makeTempVault(t, "seam1-codex-detector-");
    await writeDfSnapshot(vault);
    const noteRel = "wiki/concepts/ basalt-pipeline-fence.md".replace(" ", "");
    await writeFile(path.join(vault, noteRel), "# Basalt pipeline fence\n\nFence waits for drain.\n", "utf8");

    const sessionKey = sessionKeyFrom({ session_id: "codex-sess-1" });
    const t0 = Date.parse("2026-10-27T12:00:00.000Z");
    const iso = (ms) => new Date(t0 + ms).toISOString();
    const ctx = {
      session_key: sessionKey,
      agent_key: "main",
      wire: "codex",
      principal: "codex",
      features: ["MINNI_USE_LEDGER"],
      prompt_id: "turn-codex-1",
    };
    const turn = buildTurnRow(ctx, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "strong" }, iso(10));
    const surfaced = buildSurfacedRow(
      ctx,
      {
        channel: "prompt_pointer",
        mkey: `vault:${noteRel}`,
        title: "Basalt pipeline fence",
        rank: 0,
        visible: true,
        delivered: true,
        turn_ix: 1,
      },
      iso(20),
    );
    appendLedgerRows(vault, [turn, surfaced]);

    const txPath = path.join(root, "rollout-codex.jsonl");
    const codexLines = [
      {
        timestamp: iso(0),
        type: "response_item",
        payload: {
          type: "message",
          id: "msg_u1",
          role: "user",
          content: [{ type: "input_text", text: "Explain the stage ordering." }],
          internal_chat_message_metadata_passthrough: { turn_id: "turn-codex-1" },
        },
      },
      {
        timestamp: iso(500),
        type: "response_item",
        payload: {
          type: "message",
          id: "msg_a1",
          role: "assistant",
          content: [{ type: "output_text", text: "The basalt pipeline fence waits for the queue to drain before switching." }],
          internal_chat_message_metadata_passthrough: { turn_id: "turn-codex-1" },
        },
      },
    ];
    await writeFile(txPath, codexLines.map((l) => JSON.stringify(l)).join("\n") + "\n", "utf8");

    const res = runDetector({
      vaultPath: vault,
      wire: "codex",
      principal: "codex",
      session_key: sessionKey,
      agent_key: "main",
      transcriptPath: txPath,
      now: iso(1000),
    });
    assert.equal(res.status, "ran");
    const { rows } = readSessionLedger(vault, sessionKey);
    const judged = rows.find((r) => r.kind === "judged" && r.sid === surfaced.sid && r.final === true);
    assert.equal(judged?.outcome, "cited");
  }

  // 2. Grok updates.jsonl (streamed chunks + Unix-second timestamps)
  {
    const { root, vault } = await makeTempVault(t, "seam1-grok-detector-");
    await writeDfSnapshot(vault);
    const noteRel = "wiki/concepts/garnet-socket-backfill.md";
    await writeFile(path.join(vault, noteRel), "# Garnet socket backfill\n\nBackfill replays missed frames.\n", "utf8");

    const sessionKey = sessionKeyFrom({ sessionId: "grok-sess-1" });
    const baseSec = 1793100000;
    const iso = (secOffset) => new Date((baseSec + secOffset) * 1000).toISOString();
    const ctx = {
      session_key: sessionKey,
      agent_key: "main",
      wire: "grok-build",
      principal: "grok-build",
      features: ["MINNI_USE_LEDGER"],
      prompt_id: "grok-p1",
    };
    const turn = buildTurnRow(ctx, { turn_ix: 1, envelope_kind: "human", subkind: null, exit: "strong" }, iso(1));
    const surfacedVisible = buildSurfacedRow(
      ctx,
      {
        channel: "prompt_pointer",
        mkey: `vault:${noteRel}`,
        title: "Garnet socket backfill",
        rank: 0,
        visible: true,
        delivered: true,
        turn_ix: 1,
      },
      iso(2),
    );
    const surfacedDropped = buildSurfacedRow(
      ctx,
      {
        channel: "boot_recall",
        mkey: "vault:wiki/concepts/dropped-boot.md",
        title: "Dropped boot",
        rank: 0,
        visible: false,
        delivered: false,
      },
      iso(1),
    );
    appendLedgerRows(vault, [turn, surfacedVisible, surfacedDropped]);

    const txPath = path.join(root, "updates.jsonl");
    const grokLines = [
      {
        timestamp: baseSec,
        method: "session/update",
        params: {
          sessionId: "grok-sess-1",
          update: {
            sessionUpdate: "user_message_chunk",
            content: { type: "text", text: "How does frame recovery work?" },
          },
          _meta: { promptId: "grok-p1", agentTimestampMs: baseSec * 1000 },
        },
      },
      {
        timestamp: baseSec + 3,
        method: "session/update",
        params: {
          sessionId: "grok-sess-1",
          update: {
            sessionUpdate: "agent_message_chunk",
            content: { type: "text", text: "The garnet socket " },
          },
          _meta: { promptId: "grok-p1", agentTimestampMs: (baseSec + 3) * 1000 },
        },
      },
      {
        timestamp: baseSec + 4,
        method: "session/update",
        params: {
          sessionId: "grok-sess-1",
          update: {
            sessionUpdate: "agent_message_chunk",
            content: { type: "text", text: "backfill replays missed frames cleanly." },
          },
          _meta: { promptId: "grok-p1", agentTimestampMs: (baseSec + 4) * 1000 },
        },
      },
    ];
    await writeFile(txPath, grokLines.map((l) => JSON.stringify(l)).join("\n") + "\n", "utf8");

    const res = runDetector({
      vaultPath: vault,
      wire: "grok-build",
      principal: "grok-build",
      session_key: sessionKey,
      agent_key: "main",
      transcriptPath: txPath,
      now: iso(10),
    });
    assert.equal(res.status, "ran");
    const { rows } = readSessionLedger(vault, sessionKey);
    const judged = new Map(rows.filter((r) => r.kind === "judged" && r.final === true).map((r) => [r.sid, r]));
    assert.equal(judged.get(surfacedVisible.sid)?.outcome, "cited");
    assert.equal(judged.get(surfacedDropped.sid)?.outcome, "unknown(not_delivered)");
  }

  // 3. Missing / unreadable transcript on Stop classifies explicitly as unknown(no_transcript)
  {
    const { vault } = await makeTempVault(t, "seam1-no-tx-");
    const sessionKey = sessionKeyFrom({ session_id: "no-tx-sess-1" });
    const nowIso = "2026-10-27T12:05:00.000Z";
    const ctx = {
      session_key: sessionKey,
      agent_key: "main",
      wire: "codex",
      principal: "codex",
      features: ["MINNI_USE_LEDGER"],
    };
    const surfaced = buildSurfacedRow(
      ctx,
      {
        channel: "prompt_pointer",
        mkey: "vault:wiki/concepts/no-tx.md",
        title: "No transcript note",
        rank: 0,
        visible: true,
        delivered: true,
        turn_ix: 1,
      },
      "2026-10-27T12:04:00.000Z",
    );
    appendLedgerRows(vault, [surfaced]);

    const res = runDetector({
      vaultPath: vault,
      wire: "codex",
      principal: "codex",
      session_key: sessionKey,
      agent_key: "main",
      transcriptPath: null,
      now: nowIso,
    });
    assert.equal(res.status, "ran");
    assert.equal(res.judged_written, 1);
    const { rows } = readSessionLedger(vault, sessionKey);
    const judged = rows.find((r) => r.kind === "judged" && r.sid === surfaced.sid);
    assert.equal(judged?.outcome, "unknown(no_transcript)");
    assert.equal(judged?.via, "no_transcript");
    assert.equal(judged?.final, true);
  }
});

// ============================================================================
// SEAM 2: Gemini PreInvocation Step-Loop Idempotency & Consult Disarm Parity
// ============================================================================

test("Seam 2a: Gemini PreInvocation is idempotent across mid-turn tool steps and fires again on a new USER_EXPLICIT turn", async (t) => {
  const fixture = await makeFixture(t, "seam2-gemini-preinvocation");
  fixture.vault = path.join(fixture.root, "gemini-vault");
  await mkdir(fixture.vault, { recursive: true });
  await startFakeDaemon(fixture, {
    search: () => ({
      agent_id: "gemini",
      results: [daemonResult({ source: "/elsewhere/deploy.md", docId: 4, wikilink: "[[deploy]]", title: "Deploy" })],
    }),
  });

  const transcriptPath = path.join(fixture.root, "transcript_full.jsonl");
  const convId = "agy-conv-idempotent-1";
  const env = {
    ...process.env,
    HOME: fixture.root,
    MINNI_HOME: fixture.root,
    MINNI_GEMINI_VAULT_PATH: fixture.vault,
    MINNI_GEMINI_AGENT_ID: "gemini",
    MINNI_SOCKET_PATH: fixture.socket,
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    MINNI_UPS_RANK_V2: "off",
  };
  const callPreInvocation = async () => {
    const child = execFileAsync(process.execPath, [GEMINI_HOOK_JS, "PreInvocation"], { env, timeout: 30_000 });
    child.child.stdin.end(
      JSON.stringify({
        conversationId: convId,
        transcriptPath,
        workspacePaths: [fixture.root],
      }),
    );
    const { stdout } = await child;
    return JSON.parse(stdout.trim().split("\n").at(-1));
  };

  // Turn 1, Step 0: New USER_EXPLICIT prompt arrives.
  const turn1Lines = [
    JSON.stringify({
      step_index: 0,
      source: "USER_EXPLICIT",
      type: "USER_INPUT",
      created_at: "2026-10-27T12:00:00Z",
      content: "<USER_REQUEST>\nwhere is the deploy procedure\n</USER_REQUEST>",
    }),
  ];
  await writeFile(transcriptPath, turn1Lines.join("\n") + "\n", "utf8");

  const step1Out = await callPreInvocation();
  assert.ok(Array.isArray(step1Out.injectSteps) && step1Out.injectSteps.length > 0, "first PreInvocation of turn 1 injects context");
  assert.equal((await readLedger(fixture.vault)).filter((r) => r.kind === "turn").length, 1, "1 turn row after first PreInvocation");

  // Simulate mid-turn tool step: MODEL PLANNER_RESPONSE + GENERIC tool result appended,
  // AND another subagent session clobbers global .runtime/recall-state.json!
  await writeRecallState(
    fixture.vault,
    {
      task_signature: "sig-subagent-prompt",
      intent: "general",
      top_hits: [{ title: "Subagent", wikilink: "[[sub]]", score: 0.9 }],
      top_score: 0.9,
      consumed: false,
      ts: new Date().toISOString(),
    },
    { sessionId: "subagent-conv-other", guardV2: true },
  );
  turn1Lines.push(
    JSON.stringify({
      step_index: 1,
      source: "MODEL",
      type: "PLANNER_RESPONSE",
      created_at: "2026-10-27T12:00:02Z",
      tool_calls: [{ name: "read_file", args: { Path: "\"/tmp/x\"" } }],
    }),
    JSON.stringify({
      step_index: 2,
      source: "MODEL",
      type: "GENERIC",
      created_at: "2026-10-27T12:00:03Z",
      content: "file contents",
    }),
  );
  await writeFile(transcriptPath, turn1Lines.join("\n") + "\n", "utf8");

  const step2Out = await callPreInvocation();
  assert.deepEqual(step2Out, {}, "mid-turn PreInvocation must return geminiWire.noop() ({})");
  assert.equal((await readLedger(fixture.vault)).filter((r) => r.kind === "turn").length, 1, "still 1 turn row after mid-turn step");

  // Turn 2, Step 3: User submits a new USER_EXPLICIT prompt (even with the same text).
  turn1Lines.push(
    JSON.stringify({
      step_index: 3,
      source: "USER_EXPLICIT",
      type: "USER_INPUT",
      created_at: "2026-10-27T12:01:00Z",
      content: "<USER_REQUEST>\nwhere is the deploy procedure\n</USER_REQUEST>",
    }),
  );
  await writeFile(transcriptPath, turn1Lines.join("\n") + "\n", "utf8");

  const turn2Out = await callPreInvocation();
  assert.ok(Array.isArray(turn2Out.injectSteps) && turn2Out.injectSteps.length > 0, "new USER_EXPLICIT step_index fires UserPromptSubmit");
  assert.equal((await readLedger(fixture.vault)).filter((r) => r.kind === "turn").length, 2, "2 turn rows after second user turn");
});

test("Seam 2b: isConsultTool, PreToolUse(call_mcp_tool), and disarmRecallStateOnConsult disarm recall-state", async (t) => {
  // 1. isConsultTool recognizes call_mcp_tool wrapping minni_recall / minni_drill
  assert.equal(isConsultTool("call_mcp_tool", { ServerName: "minni", ToolName: "minni_recall" }), true);
  assert.equal(isConsultTool("call_mcp_tool", { ServerName: "minni", ToolName: "minni_drill" }), true);
  assert.equal(isConsultTool("mcp__host__call_mcp_tool", { tool_name: "minni_recall" }), true);
  assert.equal(isConsultTool("call_mcp_tool", { ServerName: "workbench", ToolName: "wb_sift" }), false);
  assert.equal(isConsultTool("call_mcp_tool"), false);

  // 2. handlePreToolUse with call_mcp_tool(minni_recall) disarms the active session recall-state
  const { vault } = await makeTempVault(t, "seam2b-consult-disarm-");
  const sessionId = "agy-session-disarm-1";
  await writeRecallState(
    vault,
    {
      task_signature: "sig-disarm-1",
      intent: "status",
      top_hits: [{ title: "Deploy guide", wikilink: "[[deploy-guide]]", score: 0.91 }],
      top_score: 0.91,
      consumed: false,
      ts: new Date().toISOString(),
    },
    { sessionId, guardV2: true },
  );

  const handlers = createHookHandlers({
    agentId: "gemini",
    vaultPath: vault,
    defaultWorkspaceId: "ws",
    contextWindow: 200_000,
    hooksEnabled: true,
    runtime: "gemini",
    hookScript: "gemini-hook.js",
    auditPrefix: "hook_gemini",
    recallGuardMode: "strict",
    wire: geminiWire,
  });

  const consultOut = await handlers.handlePreToolUse({
    session_id: sessionId,
    tool_name: "call_mcp_tool",
    tool_input: { ServerName: "minni", ToolName: "minni_recall", Arguments: { query: "deploy guide" } },
  });
  assert.equal(consultOut.hookSpecificOutput?.permissionDecision, undefined, "call_mcp_tool(minni_recall) is allowed");

  const afterPreTool = await readRecallState(vault, { sessionId, guardV2: true });
  assert.equal(afterPreTool?.consumed, true, "PreToolUse(call_mcp_tool -> minni_recall) marks recall-state consumed");
  assert.equal(await claimArming(vault, sessionId, armingKeyFor(afterPreTool)), false, "and claims arming id (already claimed)");

  // 3. Direct MCP server consult helper (disarmRecallStateOnConsult) disarms both mirror and per-session recall-state
  const session2 = "agy-session-disarm-2";
  await writeRecallState(
    vault,
    {
      task_signature: "sig-disarm-2",
      intent: "status",
      top_hits: [{ title: "Auth flow", wikilink: "[[auth-flow]]", score: 0.88 }],
      top_score: 0.88,
      consumed: false,
      ts: new Date().toISOString(),
    },
    { sessionId: session2, guardV2: true },
  );
  assert.equal(typeof recallStateMod.disarmRecallStateOnConsult, "function", "disarmRecallStateOnConsult must be exported");
  await recallStateMod.disarmRecallStateOnConsult(vault);

  const afterMcpConsult = await readRecallState(vault, { sessionId: session2, guardV2: true });
  assert.equal(afterMcpConsult?.consumed, true, "MCP consult disarms per-session recall-state");
  assert.equal(await claimArming(vault, session2, armingKeyFor(afterMcpConsult)), false, "MCP consult claims arming id (already claimed)");

  // Subsequent cold Bash tool call on that session is now allowed without deny!
  const bashOut = await handlers.handlePreToolUse({
    session_id: session2,
    tool_name: "Bash",
    tool_input: { command: "grep -rn auth src/" },
  });
  assert.equal(bashOut.hookSpecificOutput?.permissionDecision, undefined, "cold Bash is allowed after MCP consult disarm");
});
