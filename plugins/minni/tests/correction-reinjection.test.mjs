import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  BOOT_RECALL_LAYERS,
  extractLearningsSection,
  recallMemory,
} from "../dist/sovereign.js";
import {
  collectCorrectionsReassert,
  ensureVault,
  searchVaultNotes,
} from "../dist/vault.js";

// ---------------------------------------------------------------------------
// recall-F1 / recall-F6 — boot recall layer policy
// ---------------------------------------------------------------------------

test("BOOT_RECALL_LAYERS includes the correction-bearing layers, not identity only", () => {
  assert.ok(BOOT_RECALL_LAYERS.includes("identity"));
  assert.ok(
    BOOT_RECALL_LAYERS.includes("knowledge"),
    "knowledge-layer corrections were dropped by the old identity-only whitelist",
  );
  assert.ok(BOOT_RECALL_LAYERS.includes("episodic"));
});

test("recallMemory sends a layers array to the search RPC", async () => {
  const calls = [];
  const requester = async (_socketPath, method, params) => {
    calls.push({ method, params });
    return { ok: true, data: { results: [] } };
  };

  await recallMemory(
    {
      query: "boot identity for workspace-x",
      layers: BOOT_RECALL_LAYERS,
      limit: 8,
      agentId: "claude-code",
    },
    requester,
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "search");
  assert.deepEqual(calls[0].params.layers, ["identity", "knowledge", "episodic"]);
  assert.equal(calls[0].params.limit, 8);
});

test("recallMemory keeps single-layer back-compat", async () => {
  const calls = [];
  const requester = async (_socketPath, method, params) => {
    calls.push({ method, params });
    return { ok: true, data: { results: [] } };
  };

  await recallMemory({ query: "q", layer: "identity" }, requester);
  assert.deepEqual(calls[0].params.layers, ["identity"]);

  await recallMemory({ query: "q" }, requester);
  assert.equal(calls[1].params.layers, undefined);
});

// ---------------------------------------------------------------------------
// hooks-PL-2 — boot read context trimming (the read RPC records learning_reads;
// only its recency-ordered Learnings slice is re-injected)
// ---------------------------------------------------------------------------

test("extractLearningsSection returns only the learnings slice of read context", () => {
  const context = [
    "## Agent Identity: Codex",
    "Loaded whole (not chunked). This is Layer 1.",
    "",
    "## Prior Context (codex)",
    "  - **note.md** (T) [codex] accessed 3x, decay=1.00",
    "",
    "## Learnings (codex)",
    "  - [fix] Correction: service X moved to port 9090 (conf=1.0)",
    "  - [fact] vault lives at ~/.minni (conf=1.0)",
    "",
    "## Recent Activity (codex)",
    "  - [observation] deployed v2 (2026-06-09 12:00)",
  ].join("\n");

  const section = extractLearningsSection(context);
  assert.ok(section.startsWith("## Learnings (codex)"));
  assert.match(section, /port 9090/);
  assert.ok(!section.includes("Agent Identity"), "identity shelf must be trimmed");
  assert.ok(!section.includes("Recent Activity"), "section must stop at the next header");
});

test("extractLearningsSection handles missing input without throwing", () => {
  assert.equal(extractLearningsSection(undefined), undefined);
  assert.equal(extractLearningsSection(""), undefined);
  assert.equal(extractLearningsSection("## Prior Context\n- x"), undefined);
});

// ---------------------------------------------------------------------------
// hooks-PL-3 — PreCompact correction re-assert channel
// ---------------------------------------------------------------------------

test("collectCorrectionsReassert gathers stashed stale-belief events from inbox entries", () => {
  const events = collectCorrectionsReassert([
    {
      payload: {
        kind: "precompact_reassert",
        stale_belief_events: [
          { event_id: 1, superseded_learning_id: 7, new_learning_id: 9 },
        ],
      },
    },
    {
      // codex/grok precompact handoffs carry the same field
      payload: {
        kind: "codex_precompact_handoff",
        stale_belief_events: [
          { event_id: 2, superseded_learning_id: 11, new_learning_id: 12 },
        ],
      },
    },
    { payload: { kind: "stop_candidates", candidates: [] } },
    { payload: { kind: "precompact_reassert", stale_belief_events: "not-an-array" } },
  ]);

  assert.equal(events.length, 2);
  assert.deepEqual(
    events.map((e) => e.superseded_learning_id),
    [7, 11],
  );
});

// ---------------------------------------------------------------------------
// recall-F3 mirror — vault search salience + superseded exclusion
// Regression: a superseded belief with a stored correction MUST surface the
// correction (and not the superseded belief) on the vault search path.
// ---------------------------------------------------------------------------

async function makeCorrectionFixtureVault() {
  const root = await mkdtemp(path.join(tmpdir(), "sm-corrections-"));
  await ensureVault(root);

  const conceptsDir = path.join(root, "wiki", "concepts");
  const decisionsDir = path.join(root, "wiki", "decisions");
  await mkdir(conceptsDir, { recursive: true });
  await mkdir(decisionsDir, { recursive: true });

  // The stale belief — already corrected, marked superseded.
  await writeFile(
    path.join(conceptsDir, "service-x-port.md"),
    [
      "---",
      "title: Service X port",
      "type: concept",
      "status: superseded",
      "superseded_by: wiki/decisions/service-x-port-correction",
      "---",
      "",
      "# Service X port",
      "",
      "Service X listens on port 8080.",
    ].join("\n"),
    "utf8",
  );

  // The correction.
  await writeFile(
    path.join(decisionsDir, "service-x-port-correction.md"),
    [
      "---",
      "title: Service X port correction",
      "type: correction",
      "status: accepted",
      "---",
      "",
      "# Service X port correction",
      "",
      "Correction: service X moved to port 9090 on 2026-06-01.",
    ].join("\n"),
    "utf8",
  );

  // A habitual non-correction note with the same term profile; sorts BEFORE
  // the correction on the path tie-break, so only the salience boost can put
  // the correction first.
  await writeFile(
    path.join(conceptsDir, "service-x-port-runbook.md"),
    [
      "---",
      "title: Service X port runbook",
      "type: concept",
      "status: accepted",
      "---",
      "",
      "# Service X port runbook",
      "",
      "Runbook notes: service X port checks happen on 2026-06-01.",
    ].join("\n"),
    "utf8",
  );

  return root;
}

test("searchVaultNotes excludes superseded beliefs and surfaces the correction first", async () => {
  const root = await makeCorrectionFixtureVault();
  try {
    const results = await searchVaultNotes(root, "service X port", 5);
    const paths = results.map((r) => r.relativePath);

    assert.ok(
      !paths.includes(path.join("wiki", "concepts", "service-x-port.md")),
      "superseded belief must not re-surface (PR-2 status mirror)",
    );
    assert.ok(
      paths.includes(path.join("wiki", "decisions", "service-x-port-correction.md")),
      "the correction must surface",
    );
    assert.equal(
      results[0].relativePath,
      path.join("wiki", "decisions", "service-x-port-correction.md"),
      "correction-class salience boost must outrank the habitual hit",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("searchVaultNotes also drops rejected and expired notes", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-status-filter-"));
  try {
    await ensureVault(root);
    const conceptsDir = path.join(root, "wiki", "concepts");
    await mkdir(conceptsDir, { recursive: true });
    for (const status of ["rejected", "expired"]) {
      await writeFile(
        path.join(conceptsDir, `${status}-note.md`),
        `---\ntitle: ${status} zebra note\nstatus: ${status}\n---\n\nzebra migration facts.\n`,
        "utf8",
      );
    }
    await writeFile(
      path.join(conceptsDir, "live-note.md"),
      "---\ntitle: live zebra note\nstatus: accepted\n---\n\nzebra migration facts.\n",
      "utf8",
    );

    const results = await searchVaultNotes(root, "zebra migration", 5);
    assert.deepEqual(
      results.map((r) => r.relativePath),
      [path.join("wiki", "concepts", "live-note.md")],
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Behavioral boot regression (recall-F1 + hooks-PL-1/2/3): spawn the real
// SessionStart hook against a fake daemon socket. A superseded belief with a
// stored correction MUST surface the correction at boot and via stale_beliefs.
// ---------------------------------------------------------------------------

import { spawn } from "node:child_process";
import net from "node:net";
import { fileURLToPath } from "node:url";

const PLUGIN_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const STALE_BELIEF_EVENT = {
  event_id: 1,
  superseded_learning_id: 7,
  new_learning_id: 9,
  originating_agent: "operator",
  created_at: 1750000000,
};

function startFakeDaemon(socketPath, calls) {
  const server = net.createServer((socket) => {
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      if (!buffer.includes("\n")) return;
      const request = JSON.parse(buffer.split("\n")[0]);
      calls.push(request);
      const respond = (result) => {
        socket.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
      };
      switch (request.method) {
        case "status":
          respond({ status: "ok", agent: "fake-daemon" });
          break;
        case "search":
          respond({
            agent_id: request.params.agent_id,
            layer: "mixed",
            results: [
              {
                wikilink: "[[wiki/decisions/service-x-port-correction]]",
                layer: "knowledge",
                score: 1.42,
                text: "Correction: service X moved to port 9090 on 2026-06-01.",
                provenance: { salience_boost: 0.25 },
              },
            ],
          });
          break;
        case "read":
          respond({
            agent_id: request.params.agent_id,
            context: [
              "## Agent Identity: Claude-Code",
              "Loaded whole (not chunked). This is Layer 1.",
              "",
              "## Learnings (claude-code)",
              "  - [fix] Correction: service X moved to port 9090 (conf=1.0)",
              "",
              "## Recent Activity (claude-code)",
              "  - [observation] boot (2026-06-09 12:00)",
            ].join("\n"),
          });
          break;
        case "minni_list_pending_handoffs":
          respond({ handoffs: [] });
          break;
        case "minni_subscribe_contradictions":
          respond({
            agent_id: request.params.agent_id,
            events: [STALE_BELIEF_EVENT],
            status: "matched",
            checked: {
              contradiction_events_in_window: 1,
              learning_reads_for_agent: 3,
              event_window_days: 30,
              read_window_hours: null,
              since_ts: 0,
            },
          });
          break;
        default:
          respond({ ok: true });
      }
    });
  });
  return new Promise((resolve) => server.listen(socketPath, () => resolve(server)));
}

function envelopeJson(additionalContext) {
  const match = additionalContext.match(/<minni:context [^>]*>\n([\s\S]*)\n<\/minni:context>/);
  assert.ok(match, `expected a minni:context envelope, got: ${additionalContext.slice(0, 200)}`);
  return JSON.parse(match[1]);
}

function runHook(event, env, payload = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(PLUGIN_ROOT, "dist", "hook.js"), event], {
      env: { ...process.env, ...env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`hook ${event} timed out; stderr=${stderr}`));
    }, 30_000);
    child.on("close", () => {
      clearTimeout(timer);
      const line = stdout.trim().split("\n").at(-1) ?? "";
      try {
        resolve(JSON.parse(line));
      } catch (error) {
        reject(new Error(`unparseable hook output: ${stdout} / ${stderr}`));
      }
    });
    child.stdin.write(JSON.stringify({ session_id: "test-session", ...payload }));
    child.stdin.end();
  });
}

test("SessionStart boot re-injects corrections: widened layers, stale_beliefs, read learnings", async (t) => {
  const home = await mkdtemp(path.join(tmpdir(), "sm-home-"));
  const vault = await mkdtemp(path.join(tmpdir(), "sm-cc-vault-"));
  const socketPath = path.join(home, "minnid.sock");
  const calls = [];
  const server = await startFakeDaemon(socketPath, calls);
  t.after(async () => {
    server.close();
    await rm(home, { recursive: true, force: true });
    await rm(vault, { recursive: true, force: true });
  });

  const env = {
    MINNI_HOME: home,
    MINNI_SOCKET_PATH: socketPath,
    MINNI_CLAUDECODE_VAULT_PATH: vault,
    MINNI_CLAUDECODE_AGENT_ID: "claude-code",
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
  };

  const output = await runHook("SessionStart", env);
  assert.equal(output.continue, true);
  const body = envelopeJson(output.hookSpecificOutput.additionalContext);

  // recall-F1: the boot search must request the widened layer set.
  const searchCall = calls.find((c) => c.method === "search");
  assert.ok(searchCall, "boot must issue a search RPC");
  assert.deepEqual(searchCall.params.layers, ["identity", "knowledge", "episodic"]);

  // hooks-PL-2 leg (a): boot must issue a read RPC (the learning_reads writer).
  assert.ok(calls.some((c) => c.method === "read"), "boot must issue a read RPC");

  // The correction surfaces at boot in recall + recent learnings.
  assert.match(JSON.stringify(body.recall), /port 9090/);
  assert.match(body.recent_learnings.context, /port 9090/);
  assert.ok(
    !body.recent_learnings.context.includes("Agent Identity"),
    "recent_learnings must be the trimmed learnings slice",
  );

  // hooks-PL-1: discriminated stale_beliefs payload reaches the envelope.
  assert.equal(body.stale_beliefs.status, "matched");
  assert.equal(body.stale_beliefs.events[0].superseded_learning_id, 7);
});

test("PreCompact stashes stale-belief events; post-compaction boot re-asserts them", async (t) => {
  const home = await mkdtemp(path.join(tmpdir(), "sm-home-"));
  const vault = await mkdtemp(path.join(tmpdir(), "sm-cc-vault-"));
  const socketPath = path.join(home, "minnid.sock");
  const calls = [];
  const server = await startFakeDaemon(socketPath, calls);
  t.after(async () => {
    server.close();
    await rm(home, { recursive: true, force: true });
    await rm(vault, { recursive: true, force: true });
  });

  const env = {
    MINNI_HOME: home,
    MINNI_SOCKET_PATH: socketPath,
    MINNI_CLAUDECODE_VAULT_PATH: vault,
    MINNI_CLAUDECODE_AGENT_ID: "claude-code",
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
  };

  // 1. PreCompact: must write the re-assert inbox entry (hooks-PL-3).
  const preCompact = await runHook("PreCompact", env, { trigger: "auto" });
  assert.equal(preCompact.continue, true);
  const { readdir, readFile } = await import("node:fs/promises");
  const inboxFiles = (await readdir(path.join(vault, "inbox"))).filter((f) => f.endsWith(".json"));
  assert.equal(inboxFiles.length, 1, "PreCompact must stash a reassert inbox entry");
  const stash = JSON.parse(await readFile(path.join(vault, "inbox", inboxFiles[0]), "utf8"));
  assert.equal(stash.kind, "precompact_reassert");
  assert.equal(stash.stale_belief_events[0].superseded_learning_id, 7);

  // 2. Post-compaction SessionStart: the stashed events come back as
  //    corrections_reassert even before the daemon is consulted.
  const boot = await runHook("SessionStart", env);
  const body = envelopeJson(boot.hookSpecificOutput.additionalContext);
  assert.ok(Array.isArray(body.corrections_reassert), "boot must re-assert stashed corrections");
  assert.equal(body.corrections_reassert[0].superseded_learning_id, 7);
});
