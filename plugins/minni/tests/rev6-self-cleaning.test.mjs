import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

// Hermetic isolation BEFORE importing dist modules so module-load paths never point at live ~/.minni
const HERMETIC_HOME = await mkdtemp(path.join(tmpdir(), "minni-rev6-home-"));
process.env.MINNI_HOME = HERMETIC_HOME;
process.env.MINNI_SOCKET_PATH = path.join(HERMETIC_HOME, "run", "nonexistent-minnid.sock");

const { learnMemory, recallMemory } = await import("../dist/daemon_transport.js");
const { createHookHandlers, isSubstantiveRecallPrompt } = await import("../dist/hook-handlers.js");
const { executeLearnPipeline, isSessionStatusChatter } = await import("../dist/jev-harvest.js");
const { ensureVault, writeVaultPage } = await import("../dist/vault.js");
const { readRecallState, writeRecallState } = await import("../dist/recall-state.js");

test.after(async () => {
  await rm(HERMETIC_HOME, { recursive: true, force: true }).catch(() => {});
});

async function withTempEnv(t, fn) {
  const root = await mkdtemp(path.join(tmpdir(), "minni-rev6-"));
  const vault = path.join(root, "codex-vault");
  const handoffsDir = path.join(root, "session_handoffs");
  const sockPath = path.join(root, "minnid.sock");
  const prevHome = process.env.MINNI_HOME;
  const prevSock = process.env.MINNI_SOCKET_PATH;
  const prevHandoffs = process.env.MINNI_SESSION_HANDOFFS_DIR;

  process.env.MINNI_HOME = root;
  process.env.MINNI_SOCKET_PATH = sockPath;
  process.env.MINNI_SESSION_HANDOFFS_DIR = handoffsDir;

  t.after(async () => {
    process.env.MINNI_HOME = prevHome;
    process.env.MINNI_SOCKET_PATH = prevSock;
    if (prevHandoffs === undefined) {
      delete process.env.MINNI_SESSION_HANDOFFS_DIR;
    } else {
      process.env.MINNI_SESSION_HANDOFFS_DIR = prevHandoffs;
    }
    await rm(root, { recursive: true, force: true }).catch(() => {});
  });

  await ensureVault(vault);
  return fn({ root, vault, handoffsDir, sockPath });
}

async function withMockJsonRpcServer(t, sockPath, handler) {
  const requests = [];
  const server = net.createServer((conn) => {
    let buf = "";
    conn.setEncoding("utf8");
    conn.on("data", async (chunk) => {
      buf += chunk;
      const nl = buf.indexOf("\n");
      if (nl === -1) return;
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) return;
      const req = JSON.parse(line);
      requests.push(req);
      const result = await handler(req);
      conn.end(`${JSON.stringify({ jsonrpc: "2.0", id: req.id ?? 1, result })}\n`);
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(sockPath, resolve);
  });

  t.after(async () => {
    await new Promise((resolve) => server.close(() => resolve()));
  });

  return { requests };
}

test("Pillar 4: isSubstantiveRecallPrompt filters conversational acknowledgements and preserves technical prompts", () => {
  // Required task assertions
  assert.equal(isSubstantiveRecallPrompt("Ok, what do you suggest?"), false);
  assert.equal(isSubstantiveRecallPrompt("Can you explain that in plain English?"), false);
  assert.equal(isSubstantiveRecallPrompt("Yes, go ahead and do that please"), false);
  assert.equal(isSubstantiveRecallPrompt("Fix recall_score_observations in recall.py"), true);

  // Additional conversational fillers / short replies / <3 substantive tokens / conversational slash-pairs
  for (const filler of [
    "",
    "   ",
    "ok",
    "okay",
    "yes",
    "sure",
    "thanks",
    "lgtm",
    "continue",
    "proceed",
    "do it",
    "ship it",
    "looks good",
    "sounds good",
    "go ahead",
    "next",
    "why?",
    "how?",
    "really?",
    "Thank you, sounds good!",
    "Confirming readiness",
    "Ok, sounds good! Confirming readiness",
    "Ok, yes/no?",
    "N/A, thanks!",
    "Is it on/off?",
    "Wait, and/or?",
  ]) {
    assert.equal(
      isSubstantiveRecallPrompt(filler),
      false,
      `Expected conversational filler ${JSON.stringify(filler)} to be non-substantive`,
    );
  }

  // Additional technical anchors and multi-word queries
  for (const substantive of [
    "check plugins/minni/src/hook-handlers.ts",
    "inspect `vault_index_doc` callers",
    "why did #313 fail on sqlite wal mode",
    "why did (#313) fail?",
    "revert commit 7f3a9b2",
    "run pytest with --maxfail=1",
    "update RecallMemoryInput in daemon_transport.ts",
    "where is the deploy procedure",
  ]) {
    assert.equal(
      isSubstantiveRecallPrompt(substantive),
      true,
      `Expected technical query ${JSON.stringify(substantive)} to be substantive`,
    );
  }
});

test("Pillar 4: handleUserPromptSubmit gates non-substantive prompts, returns 0 recall pointers, and clears stale recall state", async (t) => {
  await withTempEnv(t, async ({ vault, sockPath }) => {
    const { requests } = await withMockJsonRpcServer(t, sockPath, async (req) => {
      if (req.method === "search") {
        return {
          results: [
            {
              id: 99,
              content: "Suggestion engine architecture and plain English guidelines",
              category: "architecture",
              score: 0.99,
            },
          ],
        };
      }
      return { ok: true };
    });

    await writeVaultPage({
      vaultPath: vault,
      title: "Suggestion Architecture",
      section: "decisions",
      content: "What do you suggest for plain English architecture.",
      tags: ["architecture"],
    });

    // Seed stale strong recall state from a previous turn
    await writeRecallState(
      vault,
      {
        session_id: "sess-p4",
        task_signature: "sig-prev",
        intent: "architecture",
        top_hits: ["wiki/decisions/suggestion-architecture.md"],
        top_score: 0.99,
      },
      { sessionId: "sess-p4" },
    );
    assert.ok(await readRecallState(vault, { sessionId: "sess-p4" }));

    const handlers = createHookHandlers({
      agentId: "codex",
      vaultPath: vault,
      defaultWorkspaceId: "ws-rev6",
      hooksEnabled: true,
      runtime: "codex",
      auditPrefix: "hook_codex",
    });

    const out = await handlers.handleUserPromptSubmit({
      session_id: "sess-p4",
      prompt: "Ok, what do you suggest?",
    });

    const additionalContext =
      out?.hookSpecificOutput?.additionalContext ?? out?.additionalContext ?? "";
    assert.equal(additionalContext, "", "Non-substantive prompt must inject 0 recall pointers");
    assert.equal(
      requests.filter((r) => r.method === "search").length,
      0,
      "Non-substantive prompt must not query daemon search RPC",
    );
    assert.equal(
      await readRecallState(vault, { sessionId: "sess-p4" }),
      null,
      "Non-substantive prompt must clear stale recall-state",
    );
  });
});

test("Pillar 5: handleSessionStart and handleUserPromptSubmit pass passive: true and update_access: false to search RPC", async (t) => {
  await withTempEnv(t, async ({ vault, sockPath }) => {
    const { requests } = await withMockJsonRpcServer(t, sockPath, async (req) => {
      if (req.method === "search") {
        return { results: [] };
      }
      if (req.method === "agent_context") {
        return { context: "", recent_learnings: [] };
      }
      if (req.method === "subscribe_contradictions") {
        return { events: [] };
      }
      return { ok: true };
    });

    const handlers = createHookHandlers({
      agentId: "codex",
      vaultPath: vault,
      defaultWorkspaceId: "ws-rev6",
      hooksEnabled: true,
      runtime: "codex",
      auditPrefix: "hook_codex",
    });

    await handlers.handleSessionStart({
      session_id: "sess-p5",
    });

    const bootSearch = requests.find((r) => r.method === "search");
    assert.ok(bootSearch, "handleSessionStart should issue a search RPC");
    assert.equal(bootSearch.params.passive, true);
    assert.equal(bootSearch.params.update_access, false);

    requests.length = 0;

    await handlers.handleUserPromptSubmit({
      session_id: "sess-p5",
      prompt: "Fix recall_score_observations in recall.py",
    });

    const upsSearch = requests.find((r) => r.method === "search");
    assert.ok(upsSearch, "Substantive handleUserPromptSubmit should issue a search RPC");
    assert.equal(upsSearch.params.passive, true);
    assert.equal(upsSearch.params.update_access, false);

    requests.length = 0;

    // Also verify SubagentStart passive recall
    await handlers.handlePreToolUse({
      session_id: "sess-p5",
      tool_name: "Agent",
      tool_use_id: "toolu_rev6_sub",
      tool_input: {
        subagent_type: "Explore",
        prompt: "inspect recall_score_observations in recall.py",
      },
    });
    await handlers.handleSubagentStart({
      session_id: "sess-p5",
      agent_id: "subagent-rev6",
      agent_type: "Explore",
    });

    const subSearch = requests.find((r) => r.method === "search");
    assert.ok(subSearch, "handleSubagentStart should issue a search RPC");
    assert.equal(subSearch.params.passive, true);
    assert.equal(subSearch.params.update_access, false);
  });
});

test("Pillar 1 & 5: recallMemory and learnMemory forward source, origin, passive, and update_access over JSON-RPC", async (t) => {
  await withTempEnv(t, async ({ root, sockPath }) => {
    const { requests } = await withMockJsonRpcServer(t, sockPath, async () => ({
      status: "staged",
      candidate_id: 42,
      results: [],
    }));

    // Point process.env.MINNI_SOCKET_PATH to a nonexistent socket to prove explicit socketPath is honored
    process.env.MINNI_SOCKET_PATH = path.join(root, "unlinked-env.sock");

    const learnRes = await learnMemory({
      content: "Decision: Always isolate daemon sockets in tests.",
      category: "decision",
      agentId: "codex",
      workspaceId: "ws-rev6",
      source: "jev_harvest",
      origin: "agent",
      socketPath: sockPath,
    });
    assert.equal(learnRes.ok, true);
    const learnReq = requests.find((r) => r.method === "learn");
    assert.ok(learnReq);
    assert.equal(learnReq.params.source, "jev_harvest");
    assert.equal(learnReq.params.origin, "agent");

    const recallRes = await recallMemory({
      query: "daemon socket isolation",
      agentId: "codex",
      workspaceId: "ws-rev6",
      passive: true,
      updateAccess: false,
      socketPath: sockPath,
    });
    assert.equal(recallRes.ok, true);
    const searchReq = requests.find((r) => r.method === "search");
    assert.ok(searchReq);
    assert.equal(searchReq.params.passive, true);
    assert.equal(searchReq.params.update_access, false);
  });
});

test("Pillar 1 & 3: executeLearnPipeline gates out session status chatter and ordinary notes to handoffs JSONL, and forwards source/origin for durable rules without vault_index_doc", async (t) => {
  await withTempEnv(t, async ({ vault, handoffsDir, sockPath }) => {
    const { requests } = await withMockJsonRpcServer(t, sockPath, async (req) => {
      if (req.method === "learn") {
        return { status: "staged", candidate_id: 77 };
      }
      return { ok: true };
    });

    // 1. Sprint/session status chatter even with durable category ("architecture") is gated out
    assert.equal(isSessionStatusChatter("Phase 2 complete: 42/42 tests passing"), true);
    assert.equal(isSessionStatusChatter("42 passing (1.2s)"), true);
    assert.equal(isSessionStatusChatter("14 passed in 0.27s"), true);
    assert.equal(
      isSessionStatusChatter("Completed slice 2 of the migration and verified the local branch state."),
      true,
    );
    assert.equal(isSessionStatusChatter("Pushed commit 9f8e7d6 to main"), true);

    const chatterRes = await executeLearnPipeline({
      vaultPath: vault,
      workspaceId: "ws-rev6",
      agentId: "codex",
      sessionId: "sess-p1",
      socketPath: sockPath,
      payload: {
        title: "Phase 2 status",
        content: "Phase 2 complete: 42/42 tests passing and pushed commit 9f8e7d6.",
        category: "architecture",
        source: "jev_harvest",
        triggerKind: "test_insight",
      },
    });

    assert.equal(chatterRes.ok, true);
    assert.equal(chatterRes.status, "routed-to-session-handoff");
    assert.ok(chatterRes.handoffPath);
    assert.equal(
      requests.length,
      0,
      "Session status chatter must not call learnMemory or vault_index_doc",
    );

    const verbFirstChatterRes = await executeLearnPipeline({
      vaultPath: vault,
      workspaceId: "ws-rev6",
      agentId: "codex",
      sessionId: "sess-p1",
      socketPath: sockPath,
      payload: {
        title: "Migration slice status",
        content: "Completed slice 2 of the migration and verified the local branch state.",
        category: "procedure",
        source: "jev_harvest",
        triggerKind: "test_insight",
      },
    });
    assert.equal(verbFirstChatterRes.ok, true);
    assert.equal(verbFirstChatterRes.status, "routed-to-session-handoff");
    assert.equal(requests.length, 0, "Verb-first slice completion chatter must not call learnMemory");

    // 2. Ordinary non-durable note is gated out to handoffs JSONL
    const ordinaryRes = await executeLearnPipeline({
      vaultPath: vault,
      workspaceId: "ws-rev6",
      agentId: "codex",
      sessionId: "sess-p1",
      socketPath: sockPath,
      payload: {
        title: "Checked local logs",
        content: "Inspected the output logs during the afternoon debugging session.",
        category: "fact",
        source: "jev_harvest",
        triggerKind: "test_insight",
      },
    });

    assert.equal(ordinaryRes.ok, true);
    assert.equal(ordinaryRes.status, "routed-to-session-handoff");
    assert.equal(requests.length, 0, "Ordinary note must not call learnMemory");

    // Verify handoffs-YYYY-MM-DD.jsonl contents
    const handoffFiles = await readdir(handoffsDir);
    assert.equal(handoffFiles.length, 1);
    assert.match(handoffFiles[0], /^handoffs-\d{4}-\d{2}-\d{2}\.jsonl$/);
    const lines = (await readFile(path.join(handoffsDir, handoffFiles[0]), "utf8"))
      .trim()
      .split("\n")
      .map((l) => JSON.parse(l));
    assert.equal(lines.length, 3);
    assert.equal(lines[0].title, "Phase 2 status");
    assert.equal(lines[0].source, "jev_harvest");
    assert.equal(lines[1].title, "Migration slice status");
    assert.equal(lines[1].durability, "ordinary");
    assert.equal(lines[2].title, "Checked local logs");
    assert.equal(lines[2].durability, "ordinary");

    // Verify no wiki/sessions/*.md note was created for handoff items
    const sessionNotesBefore = await readdir(path.join(vault, "wiki", "sessions"));
    assert.equal(sessionNotesBefore.length, 0);

    // 3. Durable architectural rule passes gate, sends source: "jev_harvest" and origin: "agent" to learnMemory,
    // and does NOT call vaultFirstLearn or vault_index_doc when daemon store succeeds.
    const durableRes = await executeLearnPipeline({
      vaultPath: vault,
      workspaceId: "ws-rev6",
      agentId: "codex",
      sessionId: "sess-p1",
      socketPath: sockPath,
      payload: {
        title: "Never index session telemetry into FTS5",
        content: "Never call vault_index_doc on wiki/sessions/*.md telemetry notes.",
        category: "architecture",
        source: "jev_harvest",
        triggerKind: "operator_directive",
      },
    });

    assert.equal(durableRes.ok, true);
    assert.equal(durableRes.status, "learned");
    assert.equal(durableRes.note, undefined, "Should not write wiki/sessions/*.md when daemon store succeeds");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].method, "learn");
    assert.equal(requests[0].params.source, "jev_harvest");
    assert.equal(requests[0].params.origin, "agent");
    assert.equal(
      requests.some((r) => r.method === "vault_index_doc"),
      false,
      "executeLearnPipeline must never call vault_index_doc",
    );

    // 4. Stage-2 triage rejection routes otherwise-durable candidate to handoffs JSONL without calling learnMemory
    requests.length = 0;
    const triageRejectedRes = await executeLearnPipeline({
      vaultPath: vault,
      workspaceId: "ws-rev6",
      agentId: "codex",
      sessionId: "sess-p1",
      socketPath: sockPath,
      triage: async () => ({ durable: false, verdict: "reject" }),
      payload: {
        title: "Triage rejected candidate",
        content: "Always check whether Stage-2 triage rejects borderline items.",
        category: "architecture",
        source: "jev_harvest",
        triggerKind: "operator_directive",
      },
    });
    assert.equal(triageRejectedRes.ok, true);
    assert.equal(triageRejectedRes.status, "routed-to-session-handoff");
    assert.equal(requests.length, 0, "Triage-rejected candidate must not call learnMemory");

    // 5. Offline fallback: when daemon is unreachable (!store.ok), vaultFirstLearn writes note without vault_index_doc
    const offlineSock = path.join(handoffsDir, "offline.sock");
    const offlineRes = await executeLearnPipeline({
      vaultPath: vault,
      workspaceId: "ws-rev6",
      agentId: "codex",
      sessionId: "sess-p1",
      socketPath: offlineSock,
      payload: {
        title: "Offline fallback rule",
        content: "Always use isolated sockets when testing offline fallback.",
        category: "architecture",
        source: "jev_harvest",
        triggerKind: "operator_directive",
      },
    });
    assert.equal(offlineRes.ok, true);
    assert.equal(offlineRes.status, "learned");
    assert.ok(offlineRes.note, "Should fall back to vaultFirstLearn when daemon is unreachable");
  });
});

// --- Rev 6.1 3-Part Surgical Fix Tests ---
import os from "node:os";

const { fitEnvelope } = await import("../dist/agent_envelope.js");
const { vaultFirstLearn } = await import("../dist/vault.js");

test("Part 1: fitEnvelope unconditionally trims recent_learnings (10 -> 3 durable items) even when total tokens (~2,400) are below tokenLimit: 4000 and charLimit: 10000", () => {
  const tenBullets = [
    "## Learnings (claude-code)",
    "  - [candidate] Unverified candidate note 1 (conf=0.5)",
    "  - [general] Ordinary compaction item 1 (conf=1.0)",
    "  - [general] Ordinary compaction item 2 (conf=1.0)",
    "  - [general] Ordinary compaction item 3 (conf=1.0)",
    "  - [decision] Durable architectural decision A (conf=1.0)",
    "  - [general] Ordinary compaction item 4 (conf=1.0)",
    "  - [fix] Durable fix B for socket timeout (conf=1.0)",
    "  - [general] Ordinary compaction item 5 (conf=1.0)",
    "  - [invariant] Durable invariant C on fail-closed indexing (conf=1.0)",
    "  - [architecture] Fourth durable item D that should be sliced after top 3 (conf=1.0)",
  ].join("\n");

  const body = {
    contract: "MEMORY CONTRACT",
    recent_learnings: {
      ok: true,
      context: tenBullets,
    },
    audit_tail: [{ tool: "hook_session_start", summary: "boot ok" }],
  };

  const fitted = fitEnvelope(body, { tokenLimit: 4000, charLimit: 10_000 });
  const resultContext = fitted.recent_learnings.context;
  const resultBullets = resultContext
    .split("\n")
    .filter((line) => /^\s*-\s/.test(line));

  assert.equal(
    resultBullets.length,
    3,
    `expected recent_learnings to be trimmed to 3 bullets even under tokenLimit=4000, got ${resultBullets.length}:\n${resultContext}`,
  );
  assert.ok(resultBullets[0].includes("[decision] Durable architectural decision A"));
  assert.ok(resultBullets[1].includes("[fix] Durable fix B"));
  assert.ok(resultBullets[2].includes("[invariant] Durable invariant C"));
  assert.deepEqual(fitted.trimmed?.recent_learnings, { from: 10, to: 3 });
  assert.equal(
    fitted.audit_tail.length,
    1,
    "audit_tail must NOT be dropped when total tokens are well below tokenLimit",
  );
});

test("Part 1b: fitEnvelope drops [candidate] lines from recent_learnings even when total bullets <= 3 and under budget", () => {
  const body = {
    contract: "MEMORY CONTRACT",
    recent_learnings: {
      ok: true,
      context: [
        "## Learnings (claude-code)",
        "  - [candidate] Unverified candidate line",
        "  - [decision] Durable decision 1",
        "  - [fix] Durable fix 2",
      ].join("\n"),
    },
  };

  const fitted = fitEnvelope(body, { tokenLimit: 4000, charLimit: 10_000 });
  const resultContext = fitted.recent_learnings.context;
  assert.ok(
    !resultContext.includes("[candidate] Unverified candidate line"),
    "expected [candidate] line to be dropped unconditionally in pre-pass",
  );
  assert.ok(resultContext.includes("[decision] Durable decision 1"));
  assert.ok(resultContext.includes("[fix] Durable fix 2"));
  assert.deepEqual(fitted.trimmed?.recent_learnings, { from: 3, to: 2 });
});

test("Part 2: vaultFirstLearn stamps status: accepted and learning_id in frontmatter when storeResult.data.status === 'accepted'", async (t) => {
  const tmpRoot = await mkdtemp(path.join(os.tmpdir(), "rev6-vault-learn-"));
  t.after(() => rm(tmpRoot, { recursive: true, force: true }));

  const note = await vaultFirstLearn({
    vaultPath: tmpRoot,
    title: "Coordinator Synchronous Promotion",
    content: "Decision: promote coordinator durable learnings synchronously.",
    category: "decision",
    agentId: "claude-code",
    storeResult: {
      ok: true,
      data: {
        status: "accepted",
        candidate_id: 9200,
        learning_id: 7400,
        principal: "claude-code",
      },
    },
  });

  const diskText = await readFile(note.notePath, "utf8");
  assert.match(
    diskText,
    /^status:\s*accepted$/m,
    `expected frontmatter status: accepted when storeResult.data.status === 'accepted', got:\n${diskText}`,
  );
  assert.match(
    diskText,
    /^learning_id:\s*7400$/m,
    `expected frontmatter learning_id: 7400 when storeResult.data.learning_id === 7400, got:\n${diskText}`,
  );
});
