import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { estimateTokens, fitEnvelope } from "../dist/agent_envelope.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(__dirname, "..");
const CLAUDE_HOOK = path.join(PLUGIN_ROOT, "dist", "hook.js");

function runHook(scriptPath, event, payload, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [scriptPath, event], {
      cwd: PLUGIN_ROOT,
      env: { ...process.env, ...env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString("utf8")));
    child.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, stdout, stderr }));
    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}

function parseEnvelopeFromContext(text) {
  const m = text.match(/<minni:context([^>]*)>\n([\s\S]*?)\n<\/minni:context>/);
  assert.ok(m, `expected <minni:context> block in:\n${text}`);
  const attrs = {};
  for (const attr of m[1].trim().split(/\s+/)) {
    const eq = attr.indexOf("=");
    if (eq > 0) {
      attrs[attr.slice(0, eq)] = attr.slice(eq + 1).replace(/^"|"$/g, "");
    }
  }
  return { attrs, body: JSON.parse(m[2]), rawJson: m[2] };
}

async function startLargeFakeDaemon(socketPath) {
  const identityMarkdown = [
    "# claudecode",
    "",
    "Role: Lead orchestrator for Minni.",
    "Guardrails: verify gates before reporting.",
    "",
    "## Learnings",
    "- [2026-10-20] [architecture] Keep SessionStart under the 10,000-char Claude Code inline cap.",
    "- [2026-10-19] [debugging] Check daemon socket paths under short tmpdir.",
    "- [2026-10-18] [testing] Prove RED before GREEN on every new test.",
    "- [2026-10-17] [security] Refuse symlink and FIFO paths with O_NOFOLLOW | O_NONBLOCK.",
  ].join("\n");

  const largeResults = Array.from({ length: 4 }, (_, i) => ({
    wikilink: `[[notes/large-note-${i + 1}]]`,
    title: `Large Architectural Note ${i + 1}`,
    text: `<EVIDENCE src="shared" id="${100 + i}">Detailed architectural context ${i + 1}: ${"alpha beta gamma delta ".repeat(45)}</EVIDENCE>`,
    snippet: `Detailed architectural context ${i + 1}: ${"alpha beta gamma delta ".repeat(35)}`,
    score: 2.5 - i * 0.2,
    confidence: 0.85 - i * 0.05,
    layer: "project",
    review_state: "approved",
    age_days: i + 1,
    doc_id: 100 + i,
    chunk_id: i,
    source: `/vault/notes/large-note-${i + 1}.md`,
    src: "shared",
    filename: `large-note-${i + 1}.md`,
    agent_origin: "claudecode",
    visibility: "global",
    rationale: "matched query terms and high recency weight ".repeat(6),
    recommended_action: "cite_if_relevant",
    recommended_scope: "workspace",
    attribution: ["claudecode", "codex"],
    attribution_score: 0.92,
    provenance: {
      agent_id: "claudecode",
      session_id: `sess-${i}`,
      created_at: "2026-10-20T00:00:00Z",
    },
    evidence_envelope: {
      raw: `<EVIDENCE src="shared" id="${100 + i}">${"duplicate envelope payload ".repeat(30)}</EVIDENCE>`,
      sha256: "a".repeat(64),
    },
  }));

  const episodic = Array.from({ length: 4 }, (_, i) => ({
    id: `ep-${i + 1}`,
    content: `Episodic event ${i + 1}: ${"worked on recall-to-use slice ".repeat(8)}`,
    timestamp: "2026-10-23T12:00:00Z",
  }));

  const server = net.createServer((conn) => {
    let buf = "";
    conn.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let nl;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        const req = JSON.parse(line);
        let result = {};
        if (req.method === "read") {
          result = {
            context: identityMarkdown,
            agent_id: "claudecode",
            backend: "sqlite",
          };
        } else if (req.method === "search" || req.method === "recall") {
          result = {
            results: largeResults,
            count: largeResults.length,
            episodic,
            episodic_count: episodic.length,
          };
        } else if (req.method === "stale_beliefs") {
          result = { status: "checked_no_match", events: [], count: 0 };
        } else if (req.method === "handoff_leases" || req.method === "handoff_list") {
          result = { leases: [], handoffs: [], count: 0 };
        }
        conn.write(JSON.stringify({ jsonrpc: "2.0", id: req.id, result }) + "\n");
      }
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  return {
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}

test(
  "SessionStart enforces token budget (<= 4000) and 10,000-char inline cap with 4 large results + 8 KB shelf",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(os.tmpdir(), "sm-s2a-budget-"));
    t.after(() => rm(root, { recursive: true, force: true }));

    const home = path.join(root, "home");
    const vault = path.join(root, "claudecode-vault");
    await mkdir(home, { recursive: true });
    await mkdir(path.join(vault, "layer1"), { recursive: true });
    await mkdir(path.join(vault, "90_audit"), { recursive: true });

    const paragraphs = Array.from(
      { length: 32 },
      (_, i) => `## Section ${i + 1}\n${"Durable rule and operational invariant text. ".repeat(5)}`,
    ).join("\n\n");
    await writeFile(path.join(vault, "layer1", "core.md"), paragraphs.slice(0, 8000), "utf8");

    const auditLines = Array.from({ length: 20 }, (_, i) =>
      JSON.stringify({
        ts: `2026-10-23T10:${String(i).padStart(2, "0")}:00Z`,
        tool: "hook_session_start",
        summary: `session start audit line ${i + 1}`,
      }),
    ).join("\n") + "\n";
    await writeFile(path.join(vault, "90_audit", "protocol.jsonl"), auditLines, "utf8");

    const sockPath = path.join(root, "d.sock");
    const daemon = await startLargeFakeDaemon(sockPath);
    t.after(() => daemon.close());

    const res = await runHook(
      CLAUDE_HOOK,
      "SessionStart",
      { session_id: "sess-budget-enforced", cwd: root },
      {
        MINNI_HOME: home,
        MINNI_CLAUDECODE_VAULT_PATH: vault,
        MINNI_SOCKET_PATH: sockPath,
        MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
        MINNI_BYPASS_AUDIT_LIMIT: "true",
        MINNI_CLAUDECODE_HOOKS: "on",
      },
    );

    assert.equal(res.code, 0, `hook exited non-zero: ${res.stderr}`);
    const output = JSON.parse(res.stdout);
    const additionalContext = output?.hookSpecificOutput?.additionalContext ?? "";
    assert.ok(
      additionalContext.length <= 10_000,
      `additionalContext must fit inside the 10,000-char Claude Code inline cap (got ${additionalContext.length})`,
    );

    const { attrs, body, rawJson } = parseEnvelopeFromContext(additionalContext);
    const tokens = Number(attrs.tokens);
    const budget = Number(attrs.budget);
    assert.equal(budget, 4000);
    assert.ok(
      tokens <= budget,
      `emitted envelope tokens (${tokens}) must be <= budget (${budget})`,
    );
    assert.equal(tokens, estimateTokens(rawJson));
    assert.ok(
      body.trimmed && typeof body.trimmed === "object",
      "body.trimmed must record trimmed sections when fitEnvelope trims",
    );
    assert.ok(
      Object.keys(body.trimmed).length > 0,
      `expected at least one trimmed section in ${JSON.stringify(body.trimmed)}`,
    );
    for (const [section, entry] of Object.entries(body.trimmed)) {
      assert.equal(typeof entry.from, "number", `${section}.from must be a number`);
      assert.equal(typeof entry.to, "number", `${section}.to must be a number`);
    }
  },
);

test(
  "MINNI_BOOT_FIT=off kill switch restores legacy unfitted SessionStart envelope",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(os.tmpdir(), "sm-s2a-fit-off-"));
    t.after(() => rm(root, { recursive: true, force: true }));

    const home = path.join(root, "home");
    const vault = path.join(root, "claudecode-vault");
    await mkdir(home, { recursive: true });
    await mkdir(path.join(vault, "layer1"), { recursive: true });
    await writeFile(path.join(vault, "layer1", "core.md"), "x".repeat(8000), "utf8");

    const sockPath = path.join(root, "d.sock");
    const daemon = await startLargeFakeDaemon(sockPath);
    t.after(() => daemon.close());

    const res = await runHook(
      CLAUDE_HOOK,
      "SessionStart",
      { session_id: "sess-budget-off", cwd: root },
      {
        MINNI_HOME: home,
        MINNI_CLAUDECODE_VAULT_PATH: vault,
        MINNI_SOCKET_PATH: sockPath,
        MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
        MINNI_BYPASS_AUDIT_LIMIT: "true",
        MINNI_CLAUDECODE_HOOKS: "on",
        MINNI_BOOT_FIT: "off",
      },
    );

    assert.equal(res.code, 0, `hook exited non-zero: ${res.stderr}`);
    const output = JSON.parse(res.stdout);
    const additionalContext = output?.hookSpecificOutput?.additionalContext ?? "";
    assert.ok(
      additionalContext.length > 10_000,
      `with MINNI_BOOT_FIT=off, unfitted envelope should exceed 10,000 chars (got ${additionalContext.length})`,
    );
    const { body, rawJson } = parseEnvelopeFromContext(additionalContext);
    assert.equal(body.trimmed, undefined, "MINNI_BOOT_FIT=off must not attach body.trimmed");
    assert.equal(body.recall.results.length, 4, "MINNI_BOOT_FIT=off must not cap recall results to 3");
    assert.ok(
      body.recall.results[0].evidence_envelope !== undefined,
      "MINNI_BOOT_FIT=off must preserve raw evidence_envelope",
    );
    // Legacy key sort: keys are strictly alphabetical (audit_tail comes before contract)
    const keys = Object.keys(JSON.parse(rawJson));
    assert.deepEqual(
      keys,
      [...keys].sort((a, b) => a.localeCompare(b)),
      "MINNI_BOOT_FIT=off must restore legacy alphabetical key sorting",
    );
  },
);

test(
  "fitEnvelope attaches over_budget ({ tokens, chars, limit, reason }) when non-trimmable floor sections exceed limits",
  () => {
    const body = {
      contract: "MEMORY CONTRACT ".repeat(20),
      corrections_reassert: [{ note: "untrimmable correction ".repeat(15) }],
      audit_tail: [{ tool: "a" }, { tool: "b" }],
    };
    const fitted = fitEnvelope(body, { tokenLimit: 20, charLimit: 80 });
    assert.deepEqual(fitted.audit_tail, []);
    assert.ok(
      fitted.over_budget && typeof fitted.over_budget === "object",
      "fitEnvelope must set over_budget when floor still exceeds limits",
    );
    assert.equal(fitted.over_budget.limit, 80);
    assert.equal(fitted.over_budget.reason, "floors_exceed_limit");
    assert.ok(fitted.over_budget.tokens > 20);
    assert.ok(fitted.over_budget.chars > 80);
  },
);

test(
  "fitEnvelope Step 7 trims string identity_body (> 2,000 chars) to 2,000-char floor and Step 6 does not inflate a short layer1_shelf",
  () => {
    const body = {
      contract: "MEMORY CONTRACT",
      identity_body: "## Agent Identity: claudecode\n" + "Identity rule line. ".repeat(250),
      layer1_shelf: {
        ok: true,
        content: "# Short Core\n concise shelf.",
        truncated: false,
      },
      audit_tail: [],
    };
    const fitted = fitEnvelope(body, { tokenLimit: 800, charLimit: 3200 });
    assert.equal(typeof fitted.identity_body, "string");
    assert.equal(fitted.identity_body.length, 2000);
    assert.match(fitted.identity_body, /\[identity_body trimmed to 2000 chars\]$/);
    assert.ok(fitted.trimmed?.identity_body, "trimmed.identity_body must be recorded");
    assert.equal(
      fitted.trimmed?.layer1_shelf,
      undefined,
      "a short non-truncated layer1_shelf must not be inflated by Step 6",
    );
    assert.equal(fitted.layer1_shelf.content, "# Short Core\n concise shelf.");
  },
);

test(
  "fitEnvelope converts reader-truncated layer1_shelf before Steps 1-5 and preserves durable learnings mentioning 'candidate' in prose",
  () => {
    // F1: truncated: true shelf (8192 chars) + audit_tail + recent_learnings -> converting shelf first brings under 6000 chars without dropping audit_tail
    const bodyWithTruncatedShelf = {
      contract: "MEMORY CONTRACT",
      layer1_shelf: {
        ok: true,
        path: "layer1/core.md",
        content: ("### Section\n" + "Shelf rule text. ".repeat(10) + "\n\n").repeat(45).slice(0, 8192),
        truncated: true,
        omitted_bytes: 500,
      },
      audit_tail: [{ tool: "hook_session_start", summary: "keep me" }],
    };
    const fittedShelfFirst = fitEnvelope(bodyWithTruncatedShelf, { tokenLimit: 2500, charLimit: 6000 });
    assert.equal(fittedShelfFirst.layer1_shelf.inline_trim, true);
    assert.equal(
      fittedShelfFirst.audit_tail.length,
      1,
      "audit_tail must not be wiped when reader-truncated shelf boot form already satisfies limits",
    );

    // F2: durable learning bullet mentioning the English word "candidate" in prose must stay durable, not be dropped as a candidate
    const learningsBody = {
      contract: "MEMORY CONTRACT",
      recent_learnings: {
        ok: true,
        context: [
          "## Learnings (claude-code)",
          "  - [candidate] Unverified note",
          "  - [note] Ordinary observation 1",
          "  - [note] Ordinary observation 2",
          "  - [note] Ordinary observation 3",
          "  - [correction] resolve_candidate requires a stamped principal",
        ].join("\n"),
      },
      audit_tail: [],
    };
    const fittedLearnings = fitEnvelope(learningsBody, { tokenLimit: 10, charLimit: 50 });
    assert.ok(
      fittedLearnings.recent_learnings.context.includes("resolve_candidate requires a stamped principal"),
      "durable [correction] mentioning 'candidate' in prose must be retained ahead of ordinary/candidate lines",
    );
    assert.ok(
      !fittedLearnings.recent_learnings.context.includes("[candidate] Unverified note"),
      "[candidate] tagged line must be dropped first",
    );
  },
);
