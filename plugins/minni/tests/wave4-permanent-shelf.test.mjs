import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { ENVELOPE_PRIORITY_ORDER, fitEnvelope } from "../dist/agent_envelope.js";
import { createHookHandlers } from "../dist/hook-handlers.js";
import {
  claudeCodeWire,
  codexWire,
  cursorWire,
  geminiWire,
  grokBuildWire,
  kilocodeWire,
  supportsSubagentStartContext,
  unprofiledWire,
} from "../dist/hook-platform.js";
import { computeRepoMapCardStalenessNote } from "../dist/project_shelf.js";
import { captureBootSurfacings } from "../dist/use-capture.js";
import { runDeliveryCommits } from "../dist/hook-delivery.js";
import { LAYER1_SHELF_MAX_BYTES } from "../dist/vault.js";
import { readLedger } from "./use-telemetry-hook-fixture.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(__dirname, "..");
const CLAUDE_HOOK = path.join(PLUGIN_ROOT, "dist", "hook.js");

function cleanupTempDir(dirPath) {
  try {
    fs.rmSync(dirPath, { recursive: true, force: true });
  } catch {
    // Best-effort temp fixture cleanup.
  }
}

function runGit(repoDir, args) {
  return execFileSync("git", ["-C", repoDir, ...args], {
    encoding: "utf8",
    env: {
      ...process.env,
      GIT_AUTHOR_NAME: "Wave4 Shelf Test",
      GIT_AUTHOR_EMAIL: "shelf@example.invalid",
      GIT_COMMITTER_NAME: "Wave4 Shelf Test",
      GIT_COMMITTER_EMAIL: "shelf@example.invalid",
    },
  }).trim();
}

function runHookEvent(event, payload, envOverrides = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [CLAUDE_HOOK, event], {
      cwd: PLUGIN_ROOT,
      env: {
        ...process.env,
        MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
        MINNI_BYPASS_AUDIT_LIMIT: "true",
        MINNI_CLAUDECODE_HOOKS: "on",
        ...envOverrides,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString("utf8")));
    child.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    child.on("error", reject);
    child.on("close", (code) => {
      assert.equal(code, 0, `${event} exited ${code}: ${stderr}`);
      const trimmed = stdout.trim();
      const rawOut = trimmed ? JSON.parse(trimmed.split("\n").pop()) : {};
      const ctx = rawOut?.hookSpecificOutput?.additionalContext ?? "";
      const m = ctx.match(/<minni:context[^>]*>\n([\s\S]*?)\n<\/minni:context>/);
      resolve({
        output: rawOut,
        additionalContext: ctx,
        body: m ? JSON.parse(m[1]) : undefined,
      });
    });
    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}

async function createMappedRepoFixture(prefix) {
  const root = await mkdtemp(path.join(os.tmpdir(), prefix));
  const home = path.join(root, "minni-home");
  const vault = path.join(home, "claudecode-vault");
  const repo = path.join(root, "alpha-repo");
  await mkdir(path.join(vault, "layer1"), { recursive: true });
  await mkdir(path.join(repo, "src", "app"), { recursive: true });

  await writeFile(path.join(repo, "AGENTS.md"), "# Alpha Repo\n\nStrict verification gates.\n", "utf8");
  await writeFile(path.join(repo, "src", "app", "auth.py"), "def verify_token(tok):\n    return True\n", "utf8");
  await writeFile(path.join(repo, "src", "app", "storage.py"), "def save_row(row):\n    return 1\n", "utf8");

  runGit(repo, ["init"]);
  runGit(repo, ["add", "AGENTS.md", "src/app/auth.py", "src/app/storage.py"]);
  runGit(repo, ["commit", "-m", "initial"]);
  const sha1 = runGit(repo, ["rev-parse", "HEAD"]);

  const mapsDir = path.join(home, "vault", "maps", "alpha-repo");
  await mkdir(mapsDir, { recursive: true });
  const indexPayload = {
    workspace_id: "workspace-alpha-repo",
    workspace_slug: "alpha-repo",
    repo_root: repo,
    git_sha: sha1,
    updated_at: Date.now() / 1000,
    gate_commands: ["make test", "ruff check"],
    doc_files: ["AGENTS.md"],
    subsystems: [
      {
        slug: "auth",
        title: "Auth Subsystem",
        summary_line: "Authentication and session token verification.",
        card_rel_path: "maps/alpha-repo/auth.md",
        file_count: 1,
        sample_files: ["src/app/auth.py"],
        owned_files: ["src/app/auth.py"],
        dir_prefixes: [],
      },
      {
        slug: "storage",
        title: "Storage Subsystem",
        summary_line: "Durable SQLite persistence layer.",
        card_rel_path: "maps/alpha-repo/storage.md",
        file_count: 1,
        sample_files: ["src/app/storage.py"],
        owned_files: ["src/app/storage.py"],
        dir_prefixes: [],
      },
    ],
  };
  await writeFile(path.join(mapsDir, "_index.json"), JSON.stringify(indexPayload, null, 2), "utf8");

  // Seed durable workspace-scoped scars in <home>/minni.db via node:sqlite
  const sqliteMod = await import("node:sqlite");
  if (sqliteMod.DatabaseSync) {
    const db = new sqliteMod.DatabaseSync(path.join(home, "minni.db"));
    try {
      db.exec(`
        CREATE TABLE learnings (
          learning_id INTEGER PRIMARY KEY AUTOINCREMENT,
          agent_id TEXT NOT NULL,
          category TEXT NOT NULL,
          content TEXT NOT NULL,
          confidence REAL DEFAULT 0.9,
          workspace_id TEXT NOT NULL DEFAULT '',
          superseded_by INTEGER,
          status TEXT DEFAULT 'accepted',
          privacy_level TEXT DEFAULT 'safe'
        );
      `);
      const ins = db.prepare(
        "INSERT INTO learnings (agent_id, category, content, confidence, workspace_id) VALUES (?, ?, ?, ?, ?)",
      );
      ins.run("claude-code", "scar", "Never name the map table document_metadata; legacy DBs already own it.", 0.98, "workspace-alpha-repo");
      ins.run("claude-code", "architecture", "Keep CLI top-level imports strictly Python stdlib.", 0.95, "workspace-alpha-repo");
      ins.run("claude-code", "scar", "Foreign workspace scar that must NEVER leak into alpha-repo.", 0.99, "workspace-other-repo");
    } finally {
      db.close();
    }
  }

  const baseEnv = {
    MINNI_HOME: home,
    MINNI_DB_PATH: path.join(home, "minni.db"),
    MINNI_CLAUDECODE_VAULT_PATH: vault,
    MINNI_SOCKET_PATH: path.join(home, "missing.sock"),
  };
  return { root, home, vault, repo, sha1, baseEnv };
}

test(
  "BUG-13: 12,000-byte layer1/core.md loads on SessionStart with truncated:false and survives fitEnvelope intact alongside project_shelf",
  { timeout: 120_000 },
  async (t) => {
    assert.equal(LAYER1_SHELF_MAX_BYTES, 24576);
    const shelfIdx = ENVELOPE_PRIORITY_ORDER.indexOf("layer1_shelf");
    const projIdx = ENVELOPE_PRIORITY_ORDER.indexOf("project_shelf");
    const recallIdx = ENVELOPE_PRIORITY_ORDER.indexOf("recall");
    const idBodyIdx = ENVELOPE_PRIORITY_ORDER.indexOf("identity_body");
    assert.ok(shelfIdx > 0 && shelfIdx < recallIdx && shelfIdx < idBodyIdx);
    assert.ok(projIdx > 0 && projIdx < recallIdx && projIdx < idBodyIdx);

    const fx = await createMappedRepoFixture("sm-w4-bug13-");
    t.after(() => cleanupTempDir(fx.root));

    const header = "# Operator Layer 1 Core\n\n";
    const tailSentinel = "\n\n## Final Invariant\nTAIL_SENTINEL_INTACT_12KB\n";
    const fillerLen = 12_000 - Buffer.byteLength(header, "utf8") - Buffer.byteLength(tailSentinel, "utf8");
    const core12k = header + "Rule line preserving permanent agent orientation.\n".repeat(Math.ceil(fillerLen / 50)).slice(0, fillerLen) + tailSentinel;
    assert.equal(Buffer.byteLength(core12k, "utf8"), 12_000);
    await writeFile(path.join(fx.vault, "layer1", "core.md"), core12k, "utf8");

    const { body } = await runHookEvent(
      "SessionStart",
      { session_id: "sess-w4-bug13", cwd: fx.repo },
      fx.baseEnv,
    );

    assert.ok(body, "SessionStart must emit <minni:context>");
    assert.equal(body.layer1_shelf.ok, true);
    assert.equal(body.layer1_shelf.truncated, false, "12 KB core.md must load with truncated: false");
    assert.equal(body.layer1_shelf.inline_trim, undefined, "12 KB core.md must NOT be crushed to boot form");
    assert.equal(body.layer1_shelf.content.length, 12_000);
    assert.ok(body.layer1_shelf.content.includes("TAIL_SENTINEL_INTACT_12KB"));
    assert.equal(typeof body.project_shelf, "string");
    assert.ok(body.project_shelf.includes("### Project Shelf (workspace-alpha-repo)"));

    // Direct Step 9 proportional scaling check: string layer1_shelf & project_shelf are exempt from 0.1x (400-char) crush
    const step9Fitted = fitEnvelope(
      {
        contract: "CONTRACT",
        layer1_shelf: { ok: true, content: core12k, truncated: false },
        project_shelf: body.project_shelf,
        identity_body: "I".repeat(5000),
        recall: {
          ok: true,
          count: 2,
          results: [
            { text: `<EVIDENCE>${"R".repeat(3000)}</EVIDENCE>` },
            { text: `<EVIDENCE>${"S".repeat(3000)}</EVIDENCE>` },
          ],
        },
      },
      { tokenLimit: 4500, charLimit: 18_000 },
    );
    assert.equal(step9Fitted.layer1_shelf.content.length, 12_000);
    assert.equal(step9Fitted.layer1_shelf.inline_trim, undefined);
    assert.equal(step9Fitted.project_shelf, body.project_shelf);
  },
);

test(
  "Wave 4 project_shelf: SessionStart injects Subsystem Maps + Repo Scars and tags git-stale subsystems after new commit",
  { timeout: 120_000 },
  async (t) => {
    const fx = await createMappedRepoFixture("sm-w4-projshelf-");
    t.after(() => cleanupTempDir(fx.root));

    await writeFile(path.join(fx.vault, "layer1", "core.md"), "# Short Core\n", "utf8");

    const cold = await runHookEvent(
      "SessionStart",
      { session_id: "sess-w4-cold", cwd: fx.repo },
      fx.baseEnv,
    );
    assert.equal(typeof cold.body.project_shelf, "string");
    assert.ok(cold.body.project_shelf.length <= 2500);
    assert.ok(cold.body.project_shelf.includes("maps/alpha-repo/auth.md"));
    assert.ok(cold.body.project_shelf.includes("maps/alpha-repo/storage.md"));
    assert.ok(cold.body.project_shelf.includes("Never name the map table document_metadata"));
    assert.ok(!cold.body.project_shelf.includes("Foreign workspace scar"));
    assert.ok(!cold.body.project_shelf.includes("changed since"), "fresh map must have no staleness tag");

    // Modify owned file src/app/auth.py and commit sha2
    await writeFile(
      path.join(fx.repo, "src", "app", "auth.py"),
      "def verify_token(tok):\n    return tok.startswith('v2_')\n",
      "utf8",
    );
    runGit(fx.repo, ["add", "src/app/auth.py"]);
    runGit(fx.repo, ["commit", "-m", "update auth token prefix"]);

    const afterCommit = await runHookEvent(
      "SessionStart",
      { session_id: "sess-w4-stale", cwd: fx.repo },
      fx.baseEnv,
    );
    const sha7 = fx.sha1.slice(0, 7);
    assert.ok(
      afterCommit.body.project_shelf.includes(
        `(changed since ${sha7}: src/app/auth.py)`,
      ),
      `Expected git staleness tag on auth subsystem, got:\n${afterCommit.body.project_shelf}`,
    );
    const storageLine = afterCommit.body.project_shelf
      .split("\n")
      .find((ln) => ln.includes("maps/alpha-repo/storage.md"));
    assert.ok(storageLine && !storageLine.includes("changed since"), "unchanged storage subsystem must stay clean");
  },
);

test(
  "BUG-14: PostCompact -> UserPromptSubmit re-injects Permanent Shelf once (skipping machine wakeups), and SubagentStart injects project_shelf without MINNI_SUBAGENT_BRIEF",
  { timeout: 120_000 },
  async (t) => {
    const fx = await createMappedRepoFixture("sm-w4-bug14-");
    t.after(() => cleanupTempDir(fx.root));

    const coreContent = "# Core Invariants\n\nAlways verify real process boundaries.\n";
    await writeFile(path.join(fx.vault, "layer1", "core.md"), coreContent, "utf8");

    // 1. Fire PostCompact on Claude Code (where PostCompact stdout cannot inject)
    const compactRes = await runHookEvent(
      "PostCompact",
      {
        session_id: "sess-w4-compact",
        cwd: fx.repo,
        compact_summary: "Compacted earlier exploration turns.",
      },
      fx.baseEnv,
    );
    assert.equal(compactRes.output.continue, true);

    // 2. Machine wakeup (<SYSTEM_MESSAGE>) must be skipped WITHOUT consuming the recovery marker!
    const wakeupRes = await runHookEvent(
      "UserPromptSubmit",
      {
        session_id: "sess-w4-compact",
        cwd: fx.repo,
        prompt: "<SYSTEM_MESSAGE>\nBackground task finished.\n</SYSTEM_MESSAGE>",
      },
      fx.baseEnv,
    );
    assert.equal(wakeupRes.body, undefined, "machine wakeup must not consume PostCompact recovery marker");

    // 3. Next real user prompt (even short/conversational "continue please") MUST re-inject layer1_shelf + project_shelf!
    const firstTurn = await runHookEvent(
      "UserPromptSubmit",
      {
        session_id: "sess-w4-compact",
        cwd: fx.repo,
        prompt: "ok continue",
      },
      {
        ...fx.baseEnv,
        MINNI_LIFECYCLE_NUDGE_MODE: "off",
      },
    );
    assert.ok(firstTurn.body, "first UserPromptSubmit after PostCompact must re-inject envelope");
    assert.equal(firstTurn.body.layer1_shelf?.ok, true);
    assert.equal(firstTurn.body.layer1_shelf?.content, coreContent);
    assert.ok(
      typeof firstTurn.body.project_shelf === "string" &&
        firstTurn.body.project_shelf.includes("maps/alpha-repo/auth.md"),
      "re-injected envelope must carry project_shelf",
    );

    // 4. Subsequent quiet turn must NOT re-inject shelves again (marker was atomically consumed)
    const secondTurn = await runHookEvent(
      "UserPromptSubmit",
      {
        session_id: "sess-w4-compact",
        cwd: fx.repo,
        prompt: "thanks",
      },
      {
        ...fx.baseEnv,
        MINNI_LIFECYCLE_NUDGE_MODE: "off",
      },
    );
    assert.equal(secondTurn.body, undefined, "subsequent non-substantive turn must stay quiet");

    // 5. Direct PostCompact wire injection check when a host wire supports PostCompact injection
    const prevHome = process.env.MINNI_HOME;
    const prevDb = process.env.MINNI_DB_PATH;
    process.env.MINNI_HOME = fx.home;
    process.env.MINNI_DB_PATH = path.join(fx.home, "minni.db");
    try {
      const customHandlers = createHookHandlers({
        agentId: "custom-host",
        vaultPath: fx.vault,
        auditPrefix: "hook_custom",
        hookScript: "hook.js",
        wire: {
          id: "custom-host",
          noop: () => ({ continue: true }),
          inject: (event, text) => ({
            continue: true,
            hookSpecificOutput: { hookEventName: event, additionalContext: text },
          }),
          note: () => null,
          lastTaskText: () => "",
          capabilities: { subagentStartContext: true },
        },
      });
      const directCompact = await customHandlers.handlePostCompact({
        session_id: "sess-direct-compact",
        cwd: fx.repo,
      });
      const directCtx = directCompact?.hookSpecificOutput?.additionalContext ?? "";
      assert.ok(directCtx.includes("Core Invariants"));
      assert.ok(directCtx.includes("maps/alpha-repo/auth.md"));
    } finally {
      if (prevHome === undefined) delete process.env.MINNI_HOME;
      else process.env.MINNI_HOME = prevHome;
      if (prevDb === undefined) delete process.env.MINNI_DB_PATH;
      else process.env.MINNI_DB_PATH = prevDb;
    }

    // 6. SubagentStart WITHOUT MINNI_SUBAGENT_BRIEF injects project_shelf in mapped workspace!
    const subRes = await runHookEvent(
      "SubagentStart",
      {
        session_id: "sess-w4-subagent",
        cwd: fx.repo,
        agent_type: "Explore",
      },
      {
        ...fx.baseEnv,
        MINNI_SUBAGENT_BRIEF: "off",
      },
    );
    assert.equal(subRes.output.continue, true);
    const subCtx = subRes.output?.hookSpecificOutput?.additionalContext ?? "";
    assert.ok(
      subCtx.includes("### Project Shelf (workspace-alpha-repo)") &&
        subCtx.includes("maps/alpha-repo/auth.md"),
      `Expected SubagentStart to inject project_shelf without MINNI_SUBAGENT_BRIEF, got: ${JSON.stringify(subRes.output)}`,
    );
  },
);

test(
  "BUG-39, BUG-41 & BUG-43: git_sha --output flag injection blocked, supportsSubagentStartContext fail-closed, and local-only scars excluded",
  async (t) => {
    // BUG-41: supportsSubagentStartContext is fail-closed unless explicitly true
    assert.equal(supportsSubagentStartContext(claudeCodeWire), true);
    for (const nonSubWire of [
      codexWire,
      grokBuildWire,
      geminiWire,
      kilocodeWire,
      cursorWire,
      unprofiledWire("unknown-host"),
    ]) {
      assert.equal(
        supportsSubagentStartContext(nonSubWire),
        false,
        `Expected ${nonSubWire.id} to fail closed on SubagentStart context`,
      );
    }

    const fx = await createMappedRepoFixture("minni-w4-sec-");
    t.after(() => cleanupTempDir(fx.root));

    // Advance git HEAD so HEAD != builtSha (would trigger git diff if git_sha were trusted)
    await writeFile(
      path.join(fx.repo, "src", "app", "auth.py"),
      "def verify_token_v3(): return True\n",
      "utf8",
    );
    runGit(fx.repo, ["add", "src/app/auth.py"]);
    runGit(fx.repo, ["commit", "-m", "advance head for bug39 check"]);

    // BUG-43: Insert a local-only scar and a private scar into learnings; neither may leak into Project Shelf
    const sqliteMod = await import("node:sqlite");
    if (sqliteMod.DatabaseSync) {
      const db = new sqliteMod.DatabaseSync(path.join(fx.home, "minni.db"));
      try {
        const insPriv = db.prepare(
          "INSERT INTO learnings (agent_id, category, content, confidence, workspace_id, privacy_level) VALUES (?, ?, ?, ?, ?, ?)",
        );
        insPriv.run(
          "claude-code",
          "scar",
          "LOCAL_ONLY_SCAR_CANARY_774 must not surface in shared project shelf.",
          0.999,
          "workspace-alpha-repo",
          "local-only",
        );
        insPriv.run(
          "claude-code",
          "scar",
          "PRIVATE_SCAR_CANARY_775 must not surface in shared project shelf.",
          0.999,
          "workspace-alpha-repo",
          "private",
        );
      } finally {
        db.close();
      }
    }

    // BUG-39: Poison _index.json git_sha with --output=<canaryFile>
    const canaryFile = path.join(fx.root, "git_diff_flag_injection_canary.txt");
    const indexPath = path.join(fx.home, "vault", "maps", "alpha-repo", "_index.json");
    const idxObj = JSON.parse(fs.readFileSync(indexPath, "utf8"));
    idxObj.git_sha = `--output=${canaryFile}`;
    await writeFile(indexPath, JSON.stringify(idxObj, null, 2), "utf8");

    const cardPath = path.join(fx.home, "vault", "maps", "alpha-repo", "auth.md");
    await mkdir(path.dirname(cardPath), { recursive: true });
    await writeFile(cardPath, "# Auth Card\n", "utf8");

    const prevHome = process.env.MINNI_HOME;
    process.env.MINNI_HOME = fx.home;
    try {
      const note = computeRepoMapCardStalenessNote(cardPath);
      assert.equal(note, null, "Non-hex git_sha must return null staleness note");
      assert.equal(
        fs.existsSync(canaryFile),
        false,
        "git diff --output flag injection created canary file via computeRepoMapCardStalenessNote!",
      );
    } finally {
      if (prevHome === undefined) delete process.env.MINNI_HOME;
      else process.env.MINNI_HOME = prevHome;
    }

    const boot = await runHookEvent(
      "SessionStart",
      { session_id: "sess-w4-bug39", cwd: fx.repo },
      fx.baseEnv,
    );
    assert.equal(
      fs.existsSync(canaryFile),
      false,
      "git diff --output flag injection created canary file via SessionStart project shelf!",
    );
    const shelfText = String(boot.body?.project_shelf ?? "");
    assert.ok(
      !shelfText.includes("LOCAL_ONLY_SCAR_CANARY_774"),
      "local-only learning leaked into project_shelf",
    );
    assert.ok(
      !shelfText.includes("PRIVATE_SCAR_CANARY_775"),
      "private learning leaked into project_shelf",
    );
    assert.ok(
      !shelfText.includes("--output="),
      "poisoned git_sha flag leaked into project_shelf banner",
    );

    // BUG-42: captureBootSurfacings uses effective raised capChars when Permanent Shelf raises SessionStart budget
    await mkdir(path.join(fx.vault, ".runtime"), { recursive: true });
    await writeFile(
      path.join(fx.vault, ".runtime", "flags.json"),
      JSON.stringify({ MINNI_USE_LEDGER: "on" }),
      "utf8",
    );
    const prevLedger = process.env.MINNI_USE_LEDGER;
    process.env.MINNI_USE_LEDGER = "on";
    try {
      const targetLine = "- [learning:42] Durable boot invariant beyond 10k wire inline cap";
      const largeBootEmitted = `${"P".repeat(12_500)}\n${targetLine}\n`;
      const baseCap = {
        vaultPath: fx.vault,
        context: {
          session_key: "sess-bug42",
          agent_key: "main",
          wire: "claude-code",
          principal: "claude-code",
          features: ["MINNI_USE_LEDGER"],
        },
        capChars: 10_000,
      };
      captureBootSurfacings(baseCap, {
        emitted: largeBootEmitted,
        recall: { ok: true, data: { results: [] } },
        identity: {
          ok: true,
          data: {
            context: `## Learnings\n${targetLine}`,
            recent_learnings: [
              { learning_id: 42, line: targetLine, content: "Durable boot invariant" },
            ],
          },
        },
        capChars: 16_000,
      });
      await runDeliveryCommits();
      const rows = await readLedger(fx.vault);
      const bootRow = rows.find((r) => r.channel === "boot_recent_learnings");
      assert.ok(bootRow, "Expected surfaced row for boot_recent_learnings");
      assert.equal(
        bootRow.visible,
        true,
        "Expected item at offset >12,500 to be marked visible:true under raised capChars=16,000",
      );
    } finally {
      if (prevLedger === undefined) delete process.env.MINNI_USE_LEDGER;
      else process.env.MINNI_USE_LEDGER = prevLedger;
    }
  },
);

