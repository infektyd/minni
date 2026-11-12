import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  MINNI_LIFECYCLE_LINE,
  buildLifecycleEmphasis,
  lifecycleSurfaceForIntent,
} from "../dist/agent_envelope.js";
import { drillMemory, recallCrossAgentDegrade } from "../dist/daemon_transport.js";
import { planHandoffDelivery } from "../dist/handoff_guard.js";
import { activatePlanChecked, createPlan, findPlanNote, persistPlan, resolvePlanIdOrActive } from "../dist/plan.js";
import { routeMemoryIntent } from "../dist/policy.js";
import { buildProjectPermanentShelf } from "../dist/project_shelf.js";

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(__dirname, "..");
const CLAUDE_HOOK = path.join(PLUGIN_ROOT, "dist", "hook.js");
const CODEX_HOOK = path.join(PLUGIN_ROOT, "dist", "codex-hook.js");
const KILOCODE_HOOK = path.join(PLUGIN_ROOT, "dist", "kilocode-hook.js");
const SERVER_PATH = path.join(PLUGIN_ROOT, "dist", "server.js");

async function runHookScript(hookPath, eventName, payload, extraEnv = {}) {
  const childPromise = execFileAsync(process.execPath, [hookPath, eventName], {
    env: {
      ...process.env,
      MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
      MINNI_BYPASS_AUDIT_LIMIT: "true",
      ...extraEnv,
    },
    timeout: 30_000,
    maxBuffer: 8 * 1024 * 1024,
  });
  childPromise.child.stdin.end(JSON.stringify(payload));
  const { stdout } = await childPromise;
  const lastLine = stdout.trim().split("\n").pop() ?? "{}";
  return JSON.parse(lastLine);
}

function parseEnvelopeContext(additionalContext) {
  const openIdx = additionalContext.indexOf("<minni:context ");
  assert.ok(openIdx >= 0, `expected <minni:context in: ${additionalContext.slice(0, 200)}`);
  const firstNl = additionalContext.indexOf("\n", openIdx);
  const closeIdx = additionalContext.lastIndexOf("\n</minni:context>");
  assert.ok(firstNl > openIdx && closeIdx > firstNl, "malformed <minni:context> wrapper");
  const openTag = additionalContext.slice(openIdx, firstNl);
  const attrs = Object.fromEntries(
    [...openTag.matchAll(/([a-z_]+)="([^"]*)"/gi)].map(([, k, v]) => [k, v]),
  );
  const rawJson = additionalContext.slice(firstNl + 1, closeIdx);
  return { attrs, body: JSON.parse(rawJson), rawJson };
}

async function seedRealMinniDb(dbPath, rows) {
  const { DatabaseSync } = await import("node:sqlite");
  const db = new DatabaseSync(dbPath);
  try {
    db.exec(`
      CREATE TABLE learnings (
        learning_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        category TEXT NOT NULL,
        content TEXT NOT NULL,
        source_doc_ids TEXT DEFAULT '[]',
        source_query TEXT DEFAULT '',
        confidence REAL DEFAULT 0.9,
        created_at REAL NOT NULL DEFAULT 0,
        access_count INTEGER DEFAULT 0,
        last_accessed REAL,
        superseded_by INTEGER,
        workspace_id TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE documents (
        doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE NOT NULL,
        agent TEXT NOT NULL DEFAULT 'claude-code',
        page_status TEXT DEFAULT 'accepted',
        privacy_level TEXT DEFAULT 'safe',
        page_type TEXT DEFAULT 'wiki',
        superseded_by INTEGER,
        expires_at REAL,
        workspace_id TEXT NOT NULL DEFAULT '',
        memory_kind TEXT DEFAULT 'learning',
        memory_uri TEXT
      );
      CREATE TABLE learning_documents (
        learning_id INTEGER NOT NULL,
        doc_id INTEGER NOT NULL,
        created_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (learning_id, doc_id)
      );
    `);
    const insLearn = db.prepare(
      "INSERT INTO learnings (agent_id, category, content, confidence, superseded_by, workspace_id) VALUES (?, ?, ?, ?, ?, ?)",
    );
    const insDoc = db.prepare(
      "INSERT INTO documents (path, agent, page_status, privacy_level, superseded_by, expires_at, workspace_id, memory_uri) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    );
    const insLink = db.prepare(
      "INSERT INTO learning_documents (learning_id, doc_id) VALUES (?, ?)",
    );
    const ids = [];
    for (const r of rows) {
      const lr = insLearn.run(
        r.agent ?? "claude-code",
        r.category ?? "scar",
        r.content,
        r.confidence ?? 0.95,
        r.learnSupersededBy ?? null,
        r.workspaceId ?? "workspace-audit-repo",
      );
      const lid = Number(lr.lastInsertRowid);
      if (r.skipDoc !== true) {
        const useJoin = r.linkMode !== "memory_uri_only";
        const useUri = r.linkMode !== "join_only";
        const dr = insDoc.run(
          r.path ?? `/vault/learnings/item-${lid}.md`,
          r.agent ?? "claude-code",
          r.pageStatus ?? "accepted",
          r.privacyLevel ?? "safe",
          r.docSupersededBy ?? null,
          r.expiresAt ?? null,
          r.workspaceId ?? "workspace-audit-repo",
          useUri ? `learning://${lid}` : null,
        );
        if (useJoin) {
          insLink.run(lid, Number(dr.lastInsertRowid));
        }
      }
      ids.push(lid);
    }
    return ids;
  } finally {
    db.close();
  }
}

test("BUG-65: buildProjectPermanentShelf + SessionStart/SubagentStart/PostCompact exclude blocked, private, local-only, secret, rejected, retired, superseded, and expired learnings via canonical documents join", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-bug65-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const home = path.join(root, "home");
  const vault = path.join(root, "claudecode-vault");
  const cwd = path.join(root, "audit-repo");
  await mkdir(home, { recursive: true });
  await mkdir(path.join(vault, "layer1"), { recursive: true });
  await mkdir(cwd, { recursive: true });
  await writeFile(path.join(vault, "layer1", "core.md"), "# Core\n", "utf8");

  const nowSec = Math.floor(Date.now() / 1000);
  await seedRealMinniDb(path.join(home, "minni.db"), [
    { content: "SAFE_ACCEPTED_SCAR_001 always run npm audit", privacyLevel: "safe", pageStatus: "accepted", linkMode: "join_only" },
    { content: "BLOCKED_CANARY_65_A must never appear", privacyLevel: "blocked", pageStatus: "accepted", linkMode: "join_only" },
    { content: "PRIVATE_CANARY_65_B must never appear", privacyLevel: "private", pageStatus: "accepted", linkMode: "memory_uri_only" },
    { content: "LOCAL_ONLY_CANARY_65_C must never appear", privacyLevel: "local-only", pageStatus: "accepted", linkMode: "join_only" },
    { content: "SECRET_CANARY_65_D must never appear", privacyLevel: "secret", pageStatus: "accepted", linkMode: "memory_uri_only" },
    { content: "REJECTED_CANARY_65_E must never appear", privacyLevel: "safe", pageStatus: "rejected", linkMode: "join_only" },
    { content: "RETIRED_CANARY_65_F must never appear", privacyLevel: "safe", pageStatus: "retired", linkMode: "memory_uri_only" },
    { content: "DOC_SUPERSEDED_CANARY_65_G must never appear", privacyLevel: "safe", pageStatus: "accepted", docSupersededBy: 999 },
    { content: "DOC_EXPIRED_CANARY_65_H must never appear", privacyLevel: "safe", pageStatus: "accepted", expiresAt: nowSec - 3600 },
  ]);

  const prevHome = process.env.MINNI_HOME;
  const prevDb = process.env.MINNI_DB_PATH;
  process.env.MINNI_HOME = home;
  process.env.MINNI_DB_PATH = path.join(home, "minni.db");
  let directShelf;
  try {
    directShelf = await buildProjectPermanentShelf({
      vaultPath: vault,
      workspaceId: "workspace-audit-repo",
      cwd,
    });
  } finally {
    if (prevHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = prevHome;
    if (prevDb === undefined) delete process.env.MINNI_DB_PATH;
    else process.env.MINNI_DB_PATH = prevDb;
  }

  assert.ok(directShelf && directShelf.includes("SAFE_ACCEPTED_SCAR_001"));
  for (const canary of [
    "BLOCKED_CANARY_65_A",
    "PRIVATE_CANARY_65_B",
    "LOCAL_ONLY_CANARY_65_C",
    "SECRET_CANARY_65_D",
    "REJECTED_CANARY_65_E",
    "RETIRED_CANARY_65_F",
    "DOC_SUPERSEDED_CANARY_65_G",
    "DOC_EXPIRED_CANARY_65_H",
  ]) {
    assert.ok(!directShelf.includes(canary), `direct project_shelf leaked ${canary}`);
  }

  const hookEnv = {
    MINNI_HOME: home,
    MINNI_DB_PATH: path.join(home, "minni.db"),
    MINNI_CLAUDECODE_VAULT_PATH: vault,
    MINNI_SOCKET_PATH: path.join(home, "missing.sock"),
    MINNI_CLAUDECODE_HOOKS: "on",
  };

  // SessionStart
  const bootOut = await runHookScript(CLAUDE_HOOK, "SessionStart", { session_id: "s-bug65", cwd }, hookEnv);
  const bootShelf = String(parseEnvelopeContext(bootOut.hookSpecificOutput.additionalContext).body.project_shelf ?? "");
  assert.ok(bootShelf.includes("SAFE_ACCEPTED_SCAR_001"));
  assert.ok(!bootShelf.includes("BLOCKED_CANARY_65_A"));
  assert.ok(!bootShelf.includes("PRIVATE_CANARY_65_B"));

  // SubagentStart
  const subOut = await runHookScript(CLAUDE_HOOK, "SubagentStart", { session_id: "s-bug65", cwd, agent_type: "worker" }, hookEnv);
  const subCtx = String(subOut.hookSpecificOutput?.additionalContext ?? "");
  assert.ok(subCtx.includes("SAFE_ACCEPTED_SCAR_001"));
  assert.ok(!subCtx.includes("BLOCKED_CANARY_65_A"));
  assert.ok(!subCtx.includes("RETIRED_CANARY_65_F"));

  // PostCompact -> UserPromptSubmit
  await runHookScript(CLAUDE_HOOK, "PostCompact", { session_id: "s-bug65", cwd, compact_summary: "compacted" }, hookEnv);
  const upsOut = await runHookScript(CLAUDE_HOOK, "UserPromptSubmit", { session_id: "s-bug65", cwd, prompt: "continue" }, hookEnv);
  const upsShelf = String(parseEnvelopeContext(upsOut.hookSpecificOutput.additionalContext).body.project_shelf ?? "");
  assert.ok(upsShelf.includes("SAFE_ACCEPTED_SCAR_001"));
  assert.ok(!upsShelf.includes("LOCAL_ONLY_CANARY_65_C"));
  assert.ok(!upsShelf.includes("REJECTED_CANARY_65_E"));
});

test("BUG-66: drillMemory stamps agent_id on both sm_drill branches, refuses SQLite/vault fallback on -32004/-32003 authz denials, enforces documents privacy/owner in offline SQLite fallback, and preserves workspaceId on recallCrossAgentDegrade", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-bug66-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const vault = path.join(root, "claudecode-vault");
  await mkdir(path.join(vault, "wiki", "decisions"), { recursive: true });
  await writeFile(
    path.join(vault, "wiki", "decisions", "secret-target.md"),
    "---\ntitle: Secret Target\nprivacy: safe\n---\n# Secret Target\n\nShould NOT leak on daemon authz denial.\n",
    "utf8",
  );

  const ids = await seedRealMinniDb(path.join(root, "minni.db"), [
    { agent: "claude-code", content: "OWNER_PRIVATE_LEARNING_66_OK", privacyLevel: "private", pageStatus: "accepted" },
    { agent: "codex", content: "FOREIGN_PRIVATE_LEARNING_66_DENY", privacyLevel: "private", pageStatus: "accepted" },
    { agent: "claude-code", content: "BLOCKED_LEARNING_66_DENY", privacyLevel: "blocked", pageStatus: "accepted" },
    { agent: "claude-code", content: "LOCAL_ONLY_LEARNING_66_DENY", privacyLevel: "local-only", pageStatus: "accepted" },
    { agent: "claude-code", content: "RETIRED_LEARNING_66_DENY", privacyLevel: "safe", pageStatus: "retired" },
  ]);

  const handleForLearning = (id) =>
    `m:${createHash("sha256").update(`learning:${id}`, "utf8").digest("hex").slice(0, 8)}`;

  // 1. Both sm_drill branches receive agent_id
  const observedRpc = [];
  await drillMemory(
    {
      references: ["d:personal:101"],
      vaultPath: vault,
      sessionId: "sess-66",
      agentId: "claude-code",
    },
    async (_sock, method, params) => {
      observedRpc.push({ branch: "handle", method, params });
      return { ok: true, data: { results: [{ doc_id: 101, src: "personal", text: "ok" }] } };
    },
  );
  await drillMemory(
    {
      wikilink: "wiki/decisions/secret-target",
      vaultPath: vault,
      sessionId: "sess-66",
      agentId: "claude-code",
    },
    async (_sock, method, params) => {
      observedRpc.push({ branch: "standard", method, params });
      return { ok: true, data: { results: [{ text: "ok" }] } };
    },
  );
  assert.equal(observedRpc.length, 2);
  assert.equal(observedRpc[0].params.agent_id, "claude-code");
  assert.equal(observedRpc[1].params.agent_id, "claude-code");

  // 2. Explicit -32004 / -32003 / unknown_identity / access_denied / vault_root_denied NEVER falls back to vault or SQLite
  for (const denialPayload of [
    { code: -32004, message: "unknown_identity: unregistered caller" },
    { code: -32003, message: "access_denied: cross-vault read forbidden" },
    { code: -32004, message: "vault_root_denied: outside principal root" },
  ]) {
    const deniedStandard = await drillMemory(
      {
        references: ["wiki/decisions/secret-target.md"],
        vaultPath: vault,
        sessionId: "sess-66",
        agentId: "claude-code",
      },
      async () => ({
        ok: false,
        error: denialPayload.message,
        data: { jsonrpc: "2.0", id: 1, error: denialPayload },
      }),
    );
    assert.equal(deniedStandard.ok, false, `must not fallback to local vault on ${denialPayload.message}`);
    assert.notEqual(deniedStandard.source, "vault-fallback");

    // Seed a session handle mapping m:<8hex> -> doc_id so hasHandleRef hits sm_drill first, then gets -32004
    const runtimeHandlesDir = path.join(vault, ".runtime", "session-handles");
    await mkdir(runtimeHandlesDir, { recursive: true });
    const sessionKey = createHash("sha256").update("sess-66-deny").digest("hex").slice(0, 16);
    const ownerHandle = handleForLearning(ids[0]);
    await writeFile(
      path.join(runtimeHandlesDir, `${sessionKey}.json`),
      JSON.stringify({
        handles: {
          [ownerHandle]: { docId: 1, scope: "personal", mkey: `learning:${ids[0]}` },
        },
      }),
      "utf8",
    );
    const deniedSqlite = await drillMemory(
      {
        references: [ownerHandle],
        vaultPath: vault,
        sessionId: "sess-66-deny",
        agentId: "claude-code",
      },
      async () => ({
        ok: false,
        error: denialPayload.message,
        data: { jsonrpc: "2.0", id: 2, error: denialPayload },
      }),
    );
    assert.equal(deniedSqlite.ok, false, `must not fallback to SQLite on ${denialPayload.message}`);
  }

  // 3. Genuine offline daemon: resolveSqliteHandleNote allows owner's private row, blocks foreign private + blocked + local-only + retired
  const deadDaemon = async () => ({ ok: false, error: "Socket not found: /tmp/missing.sock" });
  const ownerOk = await drillMemory(
    {
      references: [handleForLearning(ids[0])],
      vaultPath: vault,
      sessionId: "sess-66-offline",
      agentId: "claude-code",
    },
    deadDaemon,
  );
  assert.equal(ownerOk.ok, true, "owner agent may resolve its own private learning offline");
  assert.match(ownerOk.data.content, /OWNER_PRIVATE_LEARNING_66_OK/);

  for (const [idx, label] of [
    [1, "foreign private"],
    [2, "blocked"],
    [3, "local-only"],
    [4, "retired"],
  ]) {
    const blockedRes = await drillMemory(
      {
        references: [handleForLearning(ids[idx])],
        vaultPath: vault,
        sessionId: "sess-66-offline",
        agentId: "claude-code",
      },
      deadDaemon,
    );
    assert.equal(blockedRes.ok, false, `offline SQLite fallback must reject ${label} row`);
  }

  // 4. recallCrossAgentDegrade preserves non-default workspaceId
  const retryCalls = [];
  const degradedText = await recallCrossAgentDegrade(
    { query: "architecture map", workspaceId: "workspace-custom-proj", agentId: "gemini" },
    true,
    "capability_denied: 'cross_agent' is disabled for principal 'gemini'",
    async (_sock, method, params) => {
      retryCalls.push({ method, params });
      return {
        ok: true,
        data: {
          results: [{ title: "Hit", text: "<EVIDENCE source=\"a.md\">Hit body</EVIDENCE>", score: 0.9 }],
        },
      };
    },
  );
  assert.equal(retryCalls.length, 1);
  assert.equal(retryCalls[0].params.workspace_id, "workspace-custom-proj");
  assert.equal(retryCalls[0].params.scope, "personal");
  assert.ok(degradedText && degradedText.includes("Hit body"));
});

test("BUG-67: 5.7 KB Claude core.md and 10.8 KB Codex core.md survive SessionStart uncrushed; PostCompact UserPromptSubmit runs fitEnvelope; KiloCode CompactSummary recovers shelf", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-bug67-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const home = path.join(root, "home");
  const claudeVault = path.join(root, "claudecode-vault");
  const codexVault = path.join(root, "codex-vault");
  const kiloVault = path.join(root, "kilocode-vault");
  const cwd = path.join(root, "proj-67");
  await mkdir(home, { recursive: true });
  await mkdir(path.join(claudeVault, "layer1"), { recursive: true });
  await mkdir(path.join(codexVault, "layer1"), { recursive: true });
  await mkdir(path.join(kiloVault, "layer1"), { recursive: true });
  await mkdir(cwd, { recursive: true });

  const makeCore = (targetBytes, tag) => {
    const block = `## ${tag} Invariant\n- Keep Permanent Shelf intact across SessionStart and compaction boundaries.\n\n`;
    return block.repeat(Math.ceil(targetBytes / block.length)).slice(0, targetBytes);
  };
  const claudeCore57k = makeCore(5_840, "Claude57K");
  const codexCore108k = makeCore(11_060, "Codex108K");
  const kiloCore6k = makeCore(6_200, "Kilo6K");
  await writeFile(path.join(claudeVault, "layer1", "core.md"), claudeCore57k, "utf8");
  await writeFile(path.join(codexVault, "layer1", "core.md"), codexCore108k, "utf8");
  await writeFile(path.join(kiloVault, "layer1", "core.md"), kiloCore6k, "utf8");

  // Fake daemon returning large identity context + recall results so fitEnvelope is exercised
  const sockPath = path.join(home, "d67.sock");
  const server = net.createServer((socket) => {
    let buf = "";
    socket.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      while (buf.includes("\n")) {
        const idx = buf.indexOf("\n");
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        const req = JSON.parse(line);
        if (req.method === "read") {
          socket.write(
            JSON.stringify({
              jsonrpc: "2.0",
              id: req.id,
              result: {
                agent_id: req.params?.agent_id ?? "codex",
                backend: "sqlite",
                context: `## Agent Identity\n${"Host context line. ".repeat(220)}\n\n## Learnings\n  - [decision] Keep shelves whole.\n`,
              },
            }) + "\n",
          );
        } else if (req.method === "search") {
          socket.write(
            JSON.stringify({
              jsonrpc: "2.0",
              id: req.id,
              result: {
                results: [
                  {
                    wikilink: "[[wiki/decisions/d1]]",
                    title: "Decision 1",
                    text: `<EVIDENCE source="wiki/decisions/d1.md">${"Recall body ".repeat(140)}</EVIDENCE>`,
                    score: 0.92,
                    layer: "knowledge",
                    review_state: "accepted",
                  },
                ],
                episodic: [],
              },
            }) + "\n",
          );
        } else {
          socket.write(JSON.stringify({ jsonrpc: "2.0", id: req.id, result: { ok: true, handoffs: [] } }) + "\n");
        }
      }
    });
  });
  await new Promise((resolve) => server.listen(sockPath, resolve));
  t.after(() => server.close());

  // 1. Claude Code 5.7 KB core.md survives SessionStart without inline_trim
  const claudeBoot = await runHookScript(
    CLAUDE_HOOK,
    "SessionStart",
    { session_id: "s-claude-67", cwd },
    {
      MINNI_HOME: home,
      MINNI_CLAUDECODE_VAULT_PATH: claudeVault,
      MINNI_SOCKET_PATH: sockPath,
      MINNI_CLAUDECODE_HOOKS: "on",
    },
  );
  const claudeParsed = parseEnvelopeContext(claudeBoot.hookSpecificOutput.additionalContext);
  assert.equal(claudeParsed.body.layer1_shelf.inline_trim, undefined, "5.7 KB Claude core.md must not be crushed");
  assert.equal(claudeParsed.body.layer1_shelf.content.length, claudeCore57k.length);

  // 2. Codex 10.8 KB core.md survives SessionStart without inline_trim
  const codexBoot = await runHookScript(
    CODEX_HOOK,
    "SessionStart",
    { session_id: "s-codex-67", cwd },
    {
      MINNI_HOME: home,
      MINNI_CODEX_VAULT_PATH: codexVault,
      MINNI_SOCKET_PATH: sockPath,
      MINNI_CODEX_HOOKS: "on",
    },
  );
  const codexParsed = parseEnvelopeContext(codexBoot.hookSpecificOutput.additionalContext);
  assert.equal(codexParsed.body.layer1_shelf.inline_trim, undefined, "10.8 KB Codex core.md must not be crushed");
  assert.equal(codexParsed.body.layer1_shelf.content.length, codexCore108k.length);

  // 3. PostCompact -> UserPromptSubmit runs fitEnvelope: >24 KB truncated core.md converts to boot form instead of emitting raw unfitted 24 KB
  const oversizedCore30k = makeCore(30_000, "Oversized30K");
  await writeFile(path.join(claudeVault, "layer1", "core.md"), oversizedCore30k, "utf8");
  await runHookScript(
    CLAUDE_HOOK,
    "PostCompact",
    { session_id: "s-claude-over24k", cwd, compact_summary: "compacted" },
    {
      MINNI_HOME: home,
      MINNI_CLAUDECODE_VAULT_PATH: claudeVault,
      MINNI_SOCKET_PATH: path.join(home, "missing.sock"),
      MINNI_CLAUDECODE_HOOKS: "on",
    },
  );
  const upsFitted = await runHookScript(
    CLAUDE_HOOK,
    "UserPromptSubmit",
    { session_id: "s-claude-over24k", cwd, prompt: "hello after compact" },
    {
      MINNI_HOME: home,
      MINNI_CLAUDECODE_VAULT_PATH: claudeVault,
      MINNI_SOCKET_PATH: path.join(home, "missing.sock"),
      MINNI_CLAUDECODE_HOOKS: "on",
    },
  );
  const upsParsed = parseEnvelopeContext(upsFitted.hookSpecificOutput.additionalContext);
  assert.ok(upsParsed.attrs.budget, "PostCompact UserPromptSubmit recovery must carry fitted budget");
  assert.equal(upsParsed.body.layer1_shelf.inline_trim, true, "Oversized >24 KB shelf must be fitted on PostCompact recovery");

  // 4. KiloCode CompactSummary writes recovery marker and next UserPromptSubmit recovers layer1_shelf + project_shelf
  await seedRealMinniDb(path.join(home, "minni.db"), [
    { content: "KILO_COMPACT_SCAR_67 recovered after session.compacted", workspaceId: "workspace-proj-67" },
  ]);
  const kiloEnv = {
    MINNI_HOME: home,
    MINNI_DB_PATH: path.join(home, "minni.db"),
    MINNI_KILOCODE_VAULT_PATH: kiloVault,
    MINNI_SOCKET_PATH: path.join(home, "missing.sock"),
    MINNI_KILOCODE_HOOKS: "on",
  };
  await runHookScript(
    KILOCODE_HOOK,
    "CompactSummary",
    {
      session_id: "s-kilo-67",
      cwd,
      summary_text: "KiloCode compacted session summary",
      summary_id: "sum-67",
    },
    kiloEnv,
  );
  const kiloUps = await runHookScript(
    KILOCODE_HOOK,
    "UserPromptSubmit",
    { session_id: "s-kilo-67", cwd, prompt: "resume work after kilo compaction" },
    kiloEnv,
  );
  const kiloParsed = parseEnvelopeContext(kiloUps.hookSpecificOutput.additionalContext);
  assert.equal(kiloParsed.body.layer1_shelf.ok, true);
  assert.equal(kiloParsed.body.layer1_shelf.content, kiloCore6k);
  assert.match(String(kiloParsed.body.project_shelf ?? ""), /KILO_COMPACT_SCAR_67/);
});

test("BUG-56: 4-surface v1 lifecycle spine and runtime modules contain zero retired ghost-tool directives", async (t) => {
  assert.equal(
    MINNI_LIFECYCLE_LINE,
    "🧭 Minni lifecycle (reach for these): recall = orient before ambitious work · drill = expand recalled cards/scars · thread = track & coordinate (minni_thread_*, handoff) · learn = commit a durable finding.",
  );
  assert.equal(lifecycleSurfaceForIntent("plan"), "thread");
  assert.equal(lifecycleSurfaceForIntent("implement"), "recall");
  assert.equal(lifecycleSurfaceForIntent("debug"), "recall");
  assert.equal(buildLifecycleEmphasis("recall").includes("minni_recall"), true);
  assert.equal(buildLifecycleEmphasis("drill").includes("minni_drill"), true);

  assert.equal(routeMemoryIntent("write a wiki note in obsidian").suggestedTool, "minni_learn");
  assert.equal(routeMemoryIntent("show audit log history").suggestedTool, "minni_status");

  assert.throws(
    () =>
      planHandoffDelivery({
        runtimeAgent: "codex",
        fromAgent: "claude-code",
        toAgent: "codex",
        task: "handoff work",
      }),
    /minni_handoff_send/,
  );
  const pingPlan = planHandoffDelivery({
    runtimeAgent: "codex",
    fromAgent: "codex",
    toAgent: "claude-code",
    task: "Ask Claude what its memory says about migrations",
  });
  assert.equal(pingPlan.kind, "ping_required");
  assert.match(pingPlan.purpose, /minni_handoff_send/);
  assert.doesNotMatch(pingPlan.purpose, /minni_negotiate_handoff/);

  const tmpVault = await mkdtemp(path.join(tmpdir(), "minni-bug56-"));
  t.after(() => rm(tmpVault, { recursive: true, force: true }));
  const noActive = await resolvePlanIdOrActive(tmpVault, undefined);
  assert.ok("error" in noActive);
  assert.match(noActive.error, /minni_thread_update \(\{ plan_id, activate: true \}\)/);
  assert.doesNotMatch(noActive.error, /minni_thread_activate/);

  const created = await createPlan(
    { goal: "Terminal plan check", slices: [{ id: "s1", title: "Done slice" }], vaultPath: tmpVault },
    { vaultPath: tmpVault },
  );
  const createdNotePath = await findPlanNote(tmpVault, created.plan.plan_id);
  assert.ok(createdNotePath);
  const completedPlan = {
    ...created.plan,
    status: "complete",
    slices: created.plan.slices.map((s) => ({ ...s, status: "done" })),
  };
  await persistPlan(completedPlan, { vaultPath: tmpVault, notePath: createdNotePath });
  const termErr = await activatePlanChecked(tmpVault, created.plan.plan_id, createdNotePath);
  assert.equal(termErr.ok, false);
  assert.match(termErr.error, /minni_thread_create/);
  assert.doesNotMatch(termErr.error, /minni_thread_restore/);
});

test("BUG-68: minni_thread_scar promotes resolved scars (blocking secrets), minni_handoff_inbox filters by workspace_id and previews before single-lease ack, and all minni_thread_* responses stay strictly <= 2000 UTF-8 bytes", async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-bug68-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const home = path.join(root, "home");
  const vault = path.join(root, "claudecode-vault");
  const inboxDir = path.join(home, "inbox");
  const ackDir = path.join(home, "inbox", ".acknowledged");
  await mkdir(inboxDir, { recursive: true });
  await mkdir(ackDir, { recursive: true });
  await mkdir(vault, { recursive: true });

  const learnedCalls = [];
  const handoffStore = new Map();
  const sockPath = path.join(home, "d68.sock");
  const daemon = net.createServer((socket) => {
    let buf = "";
    socket.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      while (buf.includes("\n")) {
        const idx = buf.indexOf("\n");
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        const req = JSON.parse(line);
        const { id, method, params } = req;
        if (method === "gate.shared") {
          socket.write(JSON.stringify({ jsonrpc: "2.0", id, result: { status: "ok", principal: "claude-code" } }) + "\n");
        } else if (method === "learn") {
          learnedCalls.push(params);
          socket.write(
            JSON.stringify({
              jsonrpc: "2.0",
              id,
              result: { ok: true, status: "proposed", candidate_id: learnedCalls.length },
            }) + "\n",
          );
        } else if (method === "daemon.handoff") {
          const pkt = params?.packet ?? params ?? {};
          const leaseId = pkt.lease_id ?? `lease-${handoffStore.size + 1}`;
          const pktPath = path.join(inboxDir, `${leaseId}.json`);
          writeFile(pktPath, JSON.stringify(pkt, null, 2), "utf8").then(() => {
            handoffStore.set(leaseId, {
              lease_id: leaseId,
              from_agent: params?.from_agent ?? pkt.from_agent ?? null,
              to_agent: params?.to_agent ?? pkt.to_agent ?? null,
              task: pkt.task ?? "",
              expires_at: pkt.expires_at ?? null,
              workspace_id: pkt.workspace_id,
              path: pktPath,
            });
            socket.write(
              JSON.stringify({
                jsonrpc: "2.0",
                id,
                result: { ok: true, delivered: true, status: "ok", lease_id: leaseId, expires_at: pkt.expires_at },
              }) + "\n",
            );
          });
        } else if (method === "minni_list_pending_handoffs") {
          socket.write(
            JSON.stringify({
              jsonrpc: "2.0",
              id,
              result: { ok: true, handoffs: [...handoffStore.values()] },
            }) + "\n",
          );
        } else if (method === "minni_ack_handoff") {
          const lid = params?.lease_id;
          const entry = handoffStore.get(lid);
          if (!entry) {
            socket.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code: -32000, message: "not found" } }) + "\n");
          } else {
            // Simulate daemon moving packet out of inbox on ack!
            const movedPath = path.join(ackDir, `${lid}.json`);
            rm(entry.path, { force: true })
              .then(() => writeFile(movedPath, JSON.stringify(entry), "utf8"))
              .then(() => {
                handoffStore.delete(lid);
                socket.write(JSON.stringify({ jsonrpc: "2.0", id, result: { ok: true, acked: true } }) + "\n");
              });
          }
        } else {
          socket.write(JSON.stringify({ jsonrpc: "2.0", id, result: { ok: true, results: [] } }) + "\n");
        }
      }
    });
  });
  await new Promise((resolve) => daemon.listen(sockPath, resolve));
  t.after(() => daemon.close());

  const child = spawn(process.execPath, [SERVER_PATH], {
    env: {
      ...process.env,
      MINNI_HOME: home,
      MINNI_SOCKET_PATH: sockPath,
      MINNI_VAULT_PATH: vault,
      MINNI_CLAUDECODE_VAULT_PATH: vault,
      MINNI_AGENT_ID: "claude-code",
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  t.after(() => child.kill("SIGKILL"));

  let nextId = 1;
  const waiters = new Map();
  let stdoutBuf = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdoutBuf += chunk;
    while (stdoutBuf.includes("\n")) {
      const nl = stdoutBuf.indexOf("\n");
      const line = stdoutBuf.slice(0, nl).trim();
      stdoutBuf = stdoutBuf.slice(nl + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      const w = waiters.get(msg.id);
      if (w) {
        waiters.delete(msg.id);
        w(msg);
      }
    }
  });
  const rpc = (method, params) => {
    const id = nextId++;
    const p = new Promise((resolve) => waiters.set(id, resolve));
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    return p;
  };
  const callToolRaw = async (name, args) => {
    const reply = await rpc("tools/call", { name, arguments: args });
    assert.ok(reply.result, `${name} failed: ${JSON.stringify(reply)}`);
    const text = reply.result.content[0].text;
    return { text, bytes: Buffer.byteLength(text, "utf8"), data: JSON.parse(text) };
  };

  await rpc("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "bug68-test", version: "1.0.0" },
  });
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");

  // 1. Thread create + standalone activate + huge scars/questions stay strictly <= 2000 UTF-8 bytes
  const bigCreate = await callToolRaw("minni_thread_create", {
    goal: `Goal ${"G".repeat(600)}`,
    open_questions: Array.from({ length: 12 }, (_, i) => `Open question ${i + 1}: ${"Q".repeat(260)}`),
    slices: Array.from({ length: 18 }, (_, i) => ({
      id: `s${i + 1}`,
      title: `Slice ${i + 1} ${"T".repeat(180)}`,
      gate: `timeout 60s npm test -- ${"X".repeat(180)}`,
    })),
    activate: true,
  });
  assert.ok(bigCreate.bytes <= 2000, `minni_thread_create exceeded 2000 bytes: ${bigCreate.bytes}`);
  const planId = bigCreate.data.plan_id;

  // 2. minni_thread_scar promotes durable resolved scars, blocks secret scars, and stays < 1024 bytes
  const promotedScar = await callToolRaw("minni_thread_scar", {
    plan_id: planId,
    slice_id: "s1",
    kind: "dead_end",
    signal: "learnings table has no privacy_level column in canonical MinniDB schema",
    resolution: "Join canonical documents via learning_documents and fallback memory_uri before rendering shelf scars.",
    workspace_id: "workspace-minni",
  });
  assert.ok(promotedScar.bytes < 1024, `minni_thread_scar exceeded 1024 bytes: ${promotedScar.bytes}`);
  assert.equal(promotedScar.data.promoted, true);
  assert.equal(learnedCalls.length, 1);
  assert.equal(learnedCalls[0].category, "scar");
  assert.equal(learnedCalls[0].workspace_id, "workspace-minni");

  const sessionNotes = await readdir(path.join(vault, "wiki", "sessions"));
  assert.ok(sessionNotes.some((f) => f.endsWith(".md")), "vaultFirstLearn must write scar session note");

  const syntheticPat = ["ghp", "1234567890abcdefghijklmnopqrstuvwxyzABCD"].join("_");
  const secretScar = await callToolRaw("minni_thread_scar", {
    plan_id: planId,
    slice_id: "s1",
    kind: "failed_command",
    signal: `Auth failed with token ${syntheticPat}`,
    resolution: "Rotate leaked GitHub personal access token immediately and use env var.",
    promote: true,
  });
  assert.equal(secretScar.data.promoted, false, "scar containing secret material must NOT be promoted");
  assert.equal(learnedCalls.length, 1, "secret scar must never call daemon learn");

  // Add several long scars and verify standalone activate + status + update stay <= 2000 bytes
  for (let i = 0; i < 6; i++) {
    await callToolRaw("minni_thread_scar", {
      plan_id: planId,
      kind: "rejected_hypothesis",
      signal: `Long scar ${i}: ${"Z".repeat(400)}`,
      promote: false,
    });
  }
  const statusCheck = await callToolRaw("minni_thread_status", { plan_id: planId });
  assert.ok(statusCheck.bytes <= 2000, `minni_thread_status exceeded 2000 bytes: ${statusCheck.bytes}`);

  const deactivateCheck = await callToolRaw("minni_thread_update", { plan_id: planId, activate: false });
  assert.ok(deactivateCheck.bytes <= 2000, `standalone deactivate exceeded 2000 bytes: ${deactivateCheck.bytes}`);

  const reactivateCheck = await callToolRaw("minni_thread_update", { plan_id: planId, activate: true });
  assert.ok(reactivateCheck.bytes <= 2000, `standalone activate exceeded 2000 bytes: ${reactivateCheck.bytes}`);

  const sliceUpdateCheck = await callToolRaw("minni_thread_update", {
    plan_id: planId,
    slice_id: "s1",
    status: "in_progress",
    next_action: `Next action ${"N".repeat(900)}`,
  });
  assert.ok(sliceUpdateCheck.bytes <= 2000, `slice update exceeded 2000 bytes: ${sliceUpdateCheck.bytes}`);

  // 3. minni_handoff_send stamps workspace_id; minni_handoff_inbox filters by workspace_id and returns handoff preview BEFORE single-lease ack
  const longSummary = `Detailed handoff summary: ${"H".repeat(1100)}`;
  const sendAlpha = await callToolRaw("minni_handoff_send", {
    task: "Finish alpha workspace slice",
    summary: longSummary,
    files: ["plugins/minni/src/server.ts"],
    workspace_id: "workspace-alpha",
  });
  const sendBeta = await callToolRaw("minni_handoff_send", {
    task: "Finish beta workspace slice",
    summary: "Beta summary",
    workspace_id: "workspace-beta",
  });
  assert.equal(sendAlpha.data.status, "ok");
  assert.equal(sendBeta.data.status, "ok");

  // Filter inbox to workspace-alpha only
  const alphaOnly = await callToolRaw("minni_handoff_inbox", {
    workspace_id: "/Users/example/Projects/alpha",
  });
  assert.equal(alphaOnly.data.count, 1);
  assert.equal(alphaOnly.data.handoffs[0].lease_id, sendAlpha.data.lease_id);
  assert.equal(alphaOnly.data.handoffs[0].workspace_id, "workspace-alpha");
  assert.equal(alphaOnly.data.handoffs[0].summary.length, longSummary.length, "summary up to 1500 chars preserved");
  assert.ok(Array.isArray(alphaOnly.data.handoffs[0].wikilink_refs));

  // Single-lease ack reads preview BEFORE ackHandoff deletes/moves the inbox file!
  const singleAck = await callToolRaw("minni_handoff_inbox", {
    lease_id: sendAlpha.data.lease_id,
  });
  assert.equal(singleAck.data.acked, true);
  assert.ok(singleAck.data.handoff, "single-lease ack must return hydrated handoff preview");
  assert.equal(singleAck.data.handoff.task, "Finish alpha workspace slice");
  assert.equal(singleAck.data.handoff.workspace_id, "workspace-alpha");
  assert.equal(singleAck.data.handoff.summary, longSummary);

  // Batch ack with workspace_id only clears matching workspace-beta
  const betaBatchAck = await callToolRaw("minni_handoff_inbox", {
    ack: true,
    workspace_id: "workspace-beta",
  });
  assert.equal(betaBatchAck.data.count, 1);
  assert.equal(betaBatchAck.data.acked_count, 1);
  assert.equal(betaBatchAck.data.handoffs[0].lease_id, sendBeta.data.lease_id);
});

test("BUG-54: plugins/minni/package.json pins patched SDK/linter versions and zero-vuln overrides", async () => {
  const pkg = JSON.parse(await readFile(path.join(PLUGIN_ROOT, "package.json"), "utf8"));
  assert.equal(pkg.dependencies["@modelcontextprotocol/sdk"], "^1.32.2");
  assert.equal(pkg.devDependencies.eslint, "^10.14.0");
  assert.equal(pkg.devDependencies["typescript-eslint"], "^8.72.2");
  assert.equal(pkg.overrides?.["@google/design.md"]?.zod, "^4.6.7");
  assert.equal(pkg.overrides?.minimatch, "^11.0.8");
  assert.equal(pkg.overrides?.["@hono/node-server"], "^2.1.7");
  assert.equal(pkg.overrides?.["body-parser"], "^2.4.0");
  assert.equal(pkg.overrides?.["fast-uri"], "^3.1.9");
  assert.equal(pkg.overrides?.qs, "^6.17.1");
});

test("BUG-73: jev-harvest ignores read_file outputs and strips truncation headers instead of drafting viewer dumps", async () => {
  const { screenTranscriptChunk, extractJevLearning, stripHarvestWrappers } = await import(
    "../dist/jev-harvest.js"
  );

  // 1. Truncation headers + middle markers are stripped cleanly
  const truncatedRaw = [
    "Created At: 2026-11-11T22:00:00-05:00",
    "Completed At: 2026-11-11T22:00:02-05:00",
    "The tool's output was truncated due to being too long. You can find the full output in a file at /tmp/out.txt.",
    "Here is a preview with the middle of the output truncated:",
    "pytest failed on missing workspace_id",
    "<truncated 1200 of 4000 bytes from the middle of the output>",
    "1 failed in 0.4s",
  ].join("\n");
  const stripped = stripHarvestWrappers(truncatedRaw);
  assert.ok(!stripped.includes("The tool's output was truncated"));
  assert.ok(!stripped.includes("<truncated 1200"));
  assert.ok(stripped.includes("pytest failed on missing workspace_id"));

  // 2. read_file output ("Showing lines 1-157 of 157 ...") mentioning "passed" after an edit must NOT trigger test_insight
  const viewerSequence = [
    {
      source: "TOOL",
      type: "TOOL_RESULT",
      tool_name: "run_command",
      content: "pytest tests/test_auth.py\n1 failed in 0.4s",
    },
    {
      source: "TOOL",
      type: "TOOL_CALL",
      tool_name: "edit_file",
      content: "fix auth workspace guard",
    },
    {
      source: "TOOL",
      type: "TOOL_RESULT",
      tool_name: "read_file",
      content: "Showing lines 1-157 of 157\n1: # Architecture\n2: All gates passed.",
    },
  ];
  assert.equal(screenTranscriptChunk(viewerSequence, { promptClasses: false }).length, 0);

  // 3. Synthetic "Test failure recovered after edit: Showing lines ..." payload is rejected by extractJevLearning
  const badPayload = await extractJevLearning(
    {
      kind: "test_insight",
      triggerText: "Test failure recovered after edit: Showing lines 1-250 of 428\n1: #!/usr/bin/env bash",
      contextWindow: ["pytest failed", "fix script", "Showing lines 1-250 of 428\n1: #!/usr/bin/env bash"],
    },
    async () => ({
      title: "Test fix insight",
      content: "Test failure recovered after edit: Showing lines 1-250 of 428\n1: #!/usr/bin/env bash",
      category: "pattern",
      source: "jev_harvest",
    }),
  );
  assert.equal(badPayload, undefined);
});

