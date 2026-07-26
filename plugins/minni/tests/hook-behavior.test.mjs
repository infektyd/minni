// C6 (inbox-lifecycle follow-up): BEHAVIORAL SessionStart proof against a
// FIXTURE vault — a real `node dist/hook.js SessionStart` invocation, not a
// unit call — that resolved/archived inbox candidates no longer re-surface in
// pending_learnings, and that the TTL reaper drains an aged handoff exactly
// once across consecutive sessions.
//
// Isolation: every env knob the hook consumes points inside the tmp fixture —
// vault, MINNI_HOME (rate-limit stamps), daemon socket (missing => fast
// structured failure) and AFM health URL (closed loopback port => instant
// refusal). The live ~/.minni is never read or written.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, readdir, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { auditTail } from "../dist/vault.js";

const execFileAsync = promisify(execFile);
const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK_JS = path.join(PLUGIN_ROOT, "dist", "hook.js");

const DAY = 86_400_000;

function compactName(epochMs, slug) {
  const stamp = new Date(epochMs).toISOString().slice(0, 19).replace(/[-:]/g, "") + "Z";
  return `${stamp}-${slug}.json`;
}

function dashedName(epochMs, slug) {
  const day = new Date(epochMs).toISOString().slice(0, 10);
  return `${day}-${epochMs.toString(36)}-${slug}.json`;
}

async function runHook(hookJs, event, fixture, extraEnv, payload) {
  const env = {
    ...process.env,
    MINNI_HOME: fixture.home,
    MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    ...extraEnv,
  };
  const child = execFileAsync(process.execPath, [hookJs, event], {
    env,
    timeout: 30_000,
  });
  child.child.stdin.end(JSON.stringify(payload));
  const { stdout } = await child;
  const output = JSON.parse(stdout.trim().split("\n").pop());
  assert.equal(output.continue, true);
  return output;
}

async function runSessionStart(fixture) {
  const output = await runHook(
    HOOK_JS,
    "SessionStart",
    fixture,
    { MINNI_CLAUDECODE_VAULT_PATH: fixture.vault, MINNI_CLAUDECODE_HOOKS: "on" },
    { session_id: "fixture-session" },
  );
  const context = output.hookSpecificOutput?.additionalContext ?? "";
  const body = context.match(/<minni:context [^>]*>\n([\s\S]*?)\n<\/minni:context>/)?.[1];
  assert.ok(body, "SessionStart must emit a minni:context envelope");
  return JSON.parse(body);
}

test("SessionStart hook: resolved/archived candidates stay out of pending_learnings; TTL drains once", { timeout: 120_000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-behavior-"));
  const fixture = { vault: path.join(root, "claudecode-vault"), home: path.join(root, "home") };
  try {
    const now = Date.now();
    const inbox = path.join(fixture.vault, "inbox");
    const archive = path.join(inbox, ".archive");
    await mkdir(archive, { recursive: true });
    await mkdir(fixture.home, { recursive: true });

    const stop = (slug, candidates) => ({
      slug,
      createdAt: new Date(now - DAY).toISOString(),
      kind: "stop_candidates",
      candidates,
      log_only: [],
      do_not_store: [],
      last_task: "fixture task",
    });

    // (a) LIVE pending candidate file — must surface.
    await writeFile(
      path.join(inbox, dashedName(now - DAY, "live-session")),
      JSON.stringify(stop("live-session", ["a live pending learning"])),
      "utf8",
    );
    // (b) RESOLVED candidate file, already archived by drain-on-resolution —
    // must NOT surface and must NOT count toward totals.
    await writeFile(
      path.join(archive, dashedName(now - 10 * DAY, "resolved-session")),
      JSON.stringify(stop("resolved-session", ["an already resolved learning"])),
      "utf8",
    );
    // (c) Aged orphan file handoff (45d > 7d TTL) — reaped on FIRST session,
    // surfaced once as expired, gone from the second session.
    await writeFile(
      path.join(inbox, compactName(now - 45 * DAY, "aged-handoff")),
      JSON.stringify({ kind: "handoff", slug: "aged-handoff", task: "stale handoff" }),
      "utf8",
    );

    // ── First session ──
    const first = await runSessionStart(fixture);
    const pending1 = first.pending_learnings;
    assert.ok(pending1, "envelope must carry pending_learnings");
    assert.equal(pending1.total_pending, 1, "archived/reaped files must not inflate totals");
    assert.deepEqual(
      pending1.entries.map((e) => e.slug),
      ["live-session"],
      "only the live candidate surfaces",
    );
    const dump1 = JSON.stringify(first);
    assert.ok(!dump1.includes("resolved-session"), "archived candidate must not re-surface");
    assert.ok(!dump1.includes("already resolved learning"), "archived content must not re-surface");
    assert.equal(pending1.expired_handoffs.length, 1, "aged handoff surfaces exactly once");
    assert.equal(pending1.expired_handoffs[0].slug, "aged-handoff");
    assert.equal(pending1.expired_handoffs[0].status, "expired");

    // The reap archived (renamed), never deleted.
    const archived = await readdir(archive);
    assert.ok(
      archived.some((name) => name.includes("aged-handoff")),
      "reaped handoff must land in .archive",
    );

    // ── Second session: nothing resolved/reaped re-surfaces ──
    const second = await runSessionStart(fixture);
    const pending2 = second.pending_learnings;
    assert.equal(pending2.total_pending, 1);
    assert.deepEqual(pending2.entries.map((e) => e.slug), ["live-session"]);
    assert.deepEqual(pending2.expired_handoffs, [], "expired handoff reported once, never again");
    const dump2 = JSON.stringify(second);
    assert.ok(!dump2.includes("resolved-session"));
    assert.ok(!dump2.includes("aged-handoff"), "reaped handoff must not re-surface");

    // Conservation: the fixture inbox lost nothing — files only moved to .archive.
    const liveNames = (await readdir(inbox)).filter((n) => n.endsWith(".json"));
    const archiveNames = await readdir(archive);
    assert.equal(liveNames.length, 1);
    assert.equal(archiveNames.length, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ── Stop-path parity (review panel): grok/codex/kilocode through the shared ──
// factory. Subprocess proof per agent that a real `node dist/<agent>-hook.js
// Stop` writes ONE inbox file carrying the canonical stop_candidates kind and
// the agent_id/workspace_id stamps the cleanup/ingest tooling keys on.

const STOP_AGENTS = [
  {
    name: "grok",
    hookJs: "grok-hook.js",
    agentId: "grok-build",
    env: (vault) => ({ MINNI_GROK_VAULT_PATH: vault, MINNI_GROK_HOOKS: "on" }),
  },
  {
    name: "codex",
    hookJs: "codex-hook.js",
    agentId: "codex",
    env: (vault) => ({ MINNI_CODEX_AGENT_ID: "codex", MINNI_CODEX_VAULT_PATH: vault, MINNI_CODEX_HOOKS: "on" }),
  },
  {
    name: "kilocode",
    hookJs: "kilocode-hook.js",
    agentId: "kilocode",
    env: (vault) => ({ MINNI_KILOCODE_VAULT_PATH: vault, MINNI_KILOCODE_HOOKS: "on" }),
    // Kilo alone has NO note channel at Stop. opencode's `session.idle` is a
    // bare `event` handler with no output argument, so the bridge awaits the
    // hook and discards whatever it returns. Emitting a systemMessage there
    // was a false success -- it announced delivery to a reader that does not
    // exist. The inbox write below is the real Stop contract on every agent;
    // the note is only ever the human-facing extra.
    notesAtStop: false,
  },
];

for (const agent of STOP_AGENTS) {
  test(`Stop hook (${agent.name}): drafts ONE stop_candidates inbox file with identity stamps`, { timeout: 120_000 }, async () => {
    const root = await mkdtemp(path.join(tmpdir(), `sm-hook-stop-${agent.name}-`));
    // `<slug>-vault`, like a real install: the agent_id stamp is derived from
    // the vault DIR NAME (inboxPrincipalForVaultPath), because that is what
    // inbox_ingest.py's `_principal_for_inbox` cross-check compares against.
    const fixture = { vault: path.join(root, `${agent.name}-vault`), home: path.join(root, "home") };
    try {
      await mkdir(fixture.home, { recursive: true });
      // Stop auto-draft is RETIRED; the only way Stop writes a file now is the
      // forward-compat hook — a payload carrying genuine outcome material
      // (changed files / explicit summary), NOT audit-tail mining. Supply real
      // material so this identity-stamp proof draws a written file.
      const output = await runHook(
        path.join(PLUGIN_ROOT, "dist", agent.hookJs),
        "Stop",
        fixture,
        agent.env(fixture.vault),
        {
          session_id: "stop-fixture",
          last_user_message: "fixture stop task",
          workspace_id: "fixture-workspace",
          summary: "shipped the retry fix and verified the suite passes",
          changedFiles: ["src/retry.ts"],
        },
      );
      if (agent.notesAtStop === false) {
        // The note must be ABSENT, not merely unasserted: emitting one here
        // would be the platform-shaped false success this branch exists to end.
        assert.equal(output.systemMessage, undefined, `${agent.name} has no Stop note channel`);
      } else {
        assert.match(output.systemMessage ?? "", /drafted to inbox/);
      }

      const names = (await readdir(path.join(fixture.vault, "inbox"))).filter((n) =>
        n.endsWith(".json"),
      );
      assert.equal(names.length, 1, `${agent.name} Stop must write exactly one inbox file`);
      const body = JSON.parse(
        await readFile(path.join(fixture.vault, "inbox", names[0]), "utf8"),
      );
      assert.equal(body.kind, "stop_candidates", "canonical kind the ingest/cleanup tooling keys on");
      assert.equal(body.agent_id, agent.agentId);
      assert.equal(body.workspace_id, "fixture-workspace");
      assert.ok(Array.isArray(body.candidates) && body.candidates.length > 0);
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
}

// The Claude Code Stop path is a SEPARATE copy of handleStop in hook.ts
// (dist/hook.js), not the shared factory — so it gets its own subprocess proof
// of the noise filter: a pure-telemetry tail writes NO inbox file, a tail with
// genuine work still drafts exactly one.
test("Stop hook (claude-code): no material writes no file; genuine material still drafts", { timeout: 120_000 }, async () => {
  // The Claude Code Stop path is a SEPARATE copy of handleStop in hook.ts
  // (dist/hook.js), so it gets its own forwarder proof: a Stop carrying no
  // outcome material (Claude Code's native Stop payload) writes NO inbox file,
  // while one carrying a real summary/changed files drafts exactly one.
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stop-cc-"));
  const noiseVault = path.join(root, "noise-vault");
  const realVault = path.join(root, "real-vault");
  try {
    await mkdir(noiseVault, { recursive: true });
    const noiseOut = await runHook(
      HOOK_JS, "Stop",
      { vault: noiseVault, home: path.join(root, "home") },
      { MINNI_CLAUDECODE_VAULT_PATH: noiseVault },
      { session_id: "cc-noise", last_user_message: "what's next" },
    );
    // Session receipts: even a no-material Stop surfaces the proof-of-use line.
    assert.match(noiseOut.systemMessage ?? "", /^Minni session receipt: /);
    assert.doesNotMatch(noiseOut.systemMessage ?? "", /drafted to inbox/);
    const noiseNames = (await readdir(path.join(noiseVault, "inbox")).catch(() => []))
      .filter((n) => n.endsWith(".json"));
    assert.deepEqual(noiseNames, [], "no-material Claude Code Stop writes no inbox file");

    await mkdir(realVault, { recursive: true });
    const realOut = await runHook(
      HOOK_JS, "Stop",
      { vault: realVault, home: path.join(root, "home") },
      { MINNI_CLAUDECODE_VAULT_PATH: realVault },
      {
        session_id: "cc-real",
        last_user_message: "wrap up",
        summary: "shipped the retry fix and verified the suite passes",
        changedFiles: ["src/retry.ts"],
      },
    );
    assert.match(realOut.systemMessage ?? "", /drafted to inbox/);
    assert.match(realOut.systemMessage ?? "", /Minni session receipt:/);
    const realNames = (await readdir(path.join(realVault, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.equal(realNames.length, 1, "a Claude Code session with real work drafts one file");
    const body = JSON.parse(await readFile(path.join(realVault, "inbox", realNames[0]), "utf8"));
    assert.equal(body.candidates.length, 1);
    assert.match(body.candidates.join("\n"), /retry fix/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// REGRESSION (red-team, defect 1): the claude-code Stop wrote the inbox file
// WITHOUT kind/agent_id/workspace_id and never called workspaceFromPayload, so
// `ws = doc.get("workspace_id") or "default"` in
// src/minni/afm_passes/inbox_ingest.py filed EVERY Claude Code candidate under
// workspace "default" no matter which repo the session ran in — the whole
// findProjectRoot effort was inert on the flagship path. Stop now routes
// through the shared handleStopCore, so the stamps and the repo-root-resolved
// workspace are identical to every other platform's.
test("Stop hook (claude-code): the inbox artifact carries kind/agent_id and the repo-resolved workspace", { timeout: 120_000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stop-cc-ws-"));
  try {
    const base = await realpath(root);
    const vault = path.join(base, "claudecode-vault");
    // A real fixture repo: the session's cwd is a SUB-directory of it, so a
    // correct workspace label requires the .git walk — not the raw cwd.
    const repoRoot = path.join(base, "fixture-repo");
    const sessionCwd = path.join(repoRoot, "plugins", "minni", "src");
    await mkdir(sessionCwd, { recursive: true });
    await mkdir(path.join(repoRoot, ".git"), { recursive: true });
    await mkdir(vault, { recursive: true });

    const out = await runHook(
      HOOK_JS, "Stop",
      { vault, home: path.join(base, "home") },
      {
        MINNI_CLAUDECODE_VAULT_PATH: vault,
        // A sentinel default: if the hook fell back to the configured workspace
        // instead of deriving one from cwd, the assertion below names it.
        MINNI_CLAUDECODE_WORKSPACE_ID: "workspace-must-not-be-used",
      },
      {
        session_id: "cc-workspace",
        cwd: sessionCwd,
        last_user_message: "wrap up",
        summary: "shipped the retry fix and verified the suite passes",
        changedFiles: ["src/retry.ts"],
      },
    );
    assert.match(out.systemMessage ?? "", /drafted to inbox/);
    assert.match(out.systemMessage ?? "", /\/minni:learn/, "claude-code keeps its own commit hint");

    const names = (await readdir(path.join(vault, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.equal(names.length, 1);
    const body = JSON.parse(await readFile(path.join(vault, "inbox", names[0]), "utf8"));

    assert.equal(body.kind, "stop_candidates", "the canonical ingest format tag must be stamped");
    assert.equal(body.agent_id, "claude-code", "agent_id must match the claudecode-vault principal");
    assert.notEqual(body.workspace_id, undefined, "workspace_id must be stamped, not left to ingest");
    assert.notEqual(body.workspace_id, "default", "an unstamped workspace_id ingests as 'default'");
    assert.notEqual(
      body.workspace_id,
      "workspace-must-not-be-used",
      "the workspace must come from cwd, not from the configured default",
    );
    assert.equal(
      body.workspace_id,
      repoRoot,
      "the workspace is the .git repo ROOT of the session cwd, not the cwd itself",
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// REGRESSION (red-team, defect 4): readStdin JSON.parse'd the literal `null`
// SUCCESSFULLY and returned it, and both entrypoints then dereference the
// result as Record<string, unknown> OUTSIDE their try blocks — an uncaught
// TypeError, a crashed hook, and no `continue` for the harness. Any non-object
// parse result now degrades to {} like unparseable input does.
test("Hook stdin: a non-object JSON payload degrades to {} instead of crashing the hook", { timeout: 120_000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stdin-"));
  try {
    const vault = path.join(root, "claudecode-vault");
    await mkdir(vault, { recursive: true });
    // `null` is the reported crash; arrays and bare scalars are the same
    // wrong-shape class and must degrade identically.
    for (const raw of ["null", "[]", '"a string"', "42", "true"]) {
      const child = execFileAsync(process.execPath, [HOOK_JS, "Stop"], {
        env: {
          ...process.env,
          MINNI_HOME: path.join(root, "home"),
          MINNI_SOCKET_PATH: path.join(root, "home", "missing.sock"),
          MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
          MINNI_BYPASS_AUDIT_LIMIT: "true",
          MINNI_CLAUDECODE_VAULT_PATH: vault,
        },
        timeout: 30_000,
      });
      child.child.stdin.end(raw);
      const { stdout } = await child;
      const output = JSON.parse(stdout.trim().split("\n").pop());
      assert.equal(output.continue, true, `stdin ${raw} must still emit continue:true`);
      // Empty/shapeless payload = no draftable signal. Receipt may still ride
      // the note channel; never a candidate CTA (that would mean draft ran).
      assert.doesNotMatch(
        output.systemMessage ?? "",
        /drafted to inbox|candidate learning/,
        `stdin ${raw} must be treated as an empty payload, not a draft path`,
      );
      if (output.systemMessage !== undefined) {
        assert.match(output.systemMessage, /^Minni session receipt: /);
      }
    }
    const names = (await readdir(path.join(vault, "inbox")).catch(() => []))
      .filter((n) => n.endsWith(".json"));
    assert.deepEqual(names, [], "a shapeless payload has no outcome material to draft");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// The deterministic outcome draft yields one candidate for genuine material, so
// the post-scrub zero-candidate branch is driven through the factory's test
// seam: an injected prepareOutcome returning an empty draft.
function emptyOutcomeStub() {
  return async () => ({
    outcomeDraft: { learnCandidates: [], logOnly: [], expires: [], doNotStore: [] },
  });
}

function stopConfig(vaultPath, agentId = "grok-build") {
  return {
    agentId,
    vaultPath,
    defaultWorkspaceId: "fixture-workspace",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
  };
}

// REGRESSION (issue #173 review): the no-draftable-signal early return is NOT
// the only way to reach zero candidates. A payload can clear the signal gate
// and still scrub to nothing — that path used to fall through to writeInbox and
// litter the inbox with a `candidates: []` file, defeating the scrub at exactly
// the layer that matters. The guard is unconditional now (no per-agent knob),
// so both an arbitrary agentId and both drive-modes must skip the write.
test("Stop scrub guard: a draftable signal scrubbing to zero candidates writes no inbox file", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stop-empty-"));
  const savedHome = process.env.MINNI_HOME;
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  try {
    // Seam-driven: the payload HAS a summary (hasDraftableSignal === true), so
    // the no-signal early return is bypassed and the scrub guard is the only
    // thing standing between this call and an empty inbox file.
    const seamVault = path.join(root, "seam-vault");
    const seamHandlers = createHookHandlers(stopConfig(seamVault), {
      prepareOutcome: emptyOutcomeStub(),
    });
    const seamOut = await seamHandlers.handleStop({
      session_id: "scrubbed-stop",
      summary: "a summary the drafter rejects wholesale",
    });
    assert.equal(seamOut.continue, true);
    assert.match(seamOut.systemMessage ?? "", /^Minni session receipt: /);
    assert.doesNotMatch(seamOut.systemMessage ?? "", /candidate learning/);
    const seamNames = (await readdir(path.join(seamVault, "inbox"))).filter((n) =>
      n.endsWith(".json"),
    );
    assert.deepEqual(seamNames, [], "zero-candidate outcome must not write an inbox file");
    const seamTail = await auditTail(seamVault, 10);
    assert.ok(
      seamTail.entries.some((entry) => entry.includes("| stop scrubbed-stop")),
      "scrubbed Stop must record the stop audit marker",
    );
    assert.match(seamTail.text, /no_candidates_after_scrub/, "breadcrumb reason must be recorded");

    // End-to-end against the REAL drafter: a telemetry-shaped summary (the
    // shape issue #173 severed — Minni's own audit log fed back in) passes the
    // signal gate, is scrubbed to [] by isAuditTelemetryLine, and still must
    // produce zero files. This is the case the review reproduced as 1 file.
    const realVault = path.join(root, "real-vault");
    const realHandlers = createHookHandlers(stopConfig(realVault, "codex"));
    const realOut = await realHandlers.handleStop({
      session_id: "telemetry-stop",
      summary: "## [2026-07-25 12:00:00] hook_stop | stop session-abc",
    });
    assert.equal(realOut.continue, true);
    assert.match(realOut.systemMessage ?? "", /^Minni session receipt: /);
    assert.doesNotMatch(realOut.systemMessage ?? "", /candidate learning/);
    const realNames = (await readdir(path.join(realVault, "inbox"))).filter((n) =>
      n.endsWith(".json"),
    );
    assert.deepEqual(realNames, [], "scrubbed telemetry must not write an inbox file");
  } finally {
    if (savedHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = savedHome;
    if (savedBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = savedBypass;
    await rm(root, { recursive: true, force: true });
  }
});

// ── Stop auto-draft RETIRED 2026-07-24 (investigation: 0 real learnings in 40 ──
// drafts; the audit tail is Minni's own telemetry log). Capture is now
// EXCLUSIVELY the explicit minni_prepare_outcome / minni_learn path. Stop no
// longer self-drafts: with no outcome material it records one log-only
// breadcrumb and writes nothing. The payload branch is a documented
// forward-compat hook (real harnesses supply no material today).

test("Stop auto-draft retired: no outcome material writes no inbox file, only a log breadcrumb", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stop-nosignal-"));
  const savedHome = process.env.MINNI_HOME;
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  try {
    const vault = path.join(root, "grok-vault");
    await mkdir(vault, { recursive: true });
    const handlers = createHookHandlers(stopConfig(vault));
    // A bare last_user_message (the prompt) is NOT draftable outcome material.
    const out = await handlers.handleStop({
      session_id: "nosignal-stop",
      last_user_message: "what's next",
    });
    assert.equal(out.continue, true);
    // Receipt rides the note channel; no candidate CTA.
    assert.match(out.systemMessage ?? "", /^Minni session receipt: /);
    assert.doesNotMatch(out.systemMessage ?? "", /candidate learning/);
    const names = (await readdir(path.join(vault, "inbox")).catch(() => []))
      .filter((n) => n.endsWith(".json"));
    assert.deepEqual(names, [], "no-signal Stop must not write an inbox file");
    // Stop marker closes the sessionReceipt window; reason lives in details.
    const tail = await auditTail(vault, 10);
    assert.ok(
      tail.entries.some((entry) => entry.includes("| stop nosignal-stop")),
      "no-signal Stop must record the stop audit marker",
    );
    assert.match(tail.text, /no_draftable_signal/, "breadcrumb reason must be recorded");
  } finally {
    if (savedHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = savedHome;
    if (savedBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = savedBypass;
    await rm(root, { recursive: true, force: true });
  }
});

test("Stop forward-compat hook: outcome material in the payload still drafts exactly one file", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stop-material-"));
  const savedHome = process.env.MINNI_HOME;
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  try {
    const vault = path.join(root, "grok-vault");
    await mkdir(vault, { recursive: true });
    const handlers = createHookHandlers(stopConfig(vault));
    const out = await handlers.handleStop({
      session_id: "material-stop",
      last_user_message: "wrap up",
      summary: "shipped the retry fix and verified the suite passes",
      changedFiles: ["src/retry.ts"],
    });
    assert.match(out.systemMessage ?? "", /candidate learning/);
    assert.match(out.systemMessage ?? "", /Minni session receipt:/);
    const names = (await readdir(path.join(vault, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.equal(names.length, 1, "genuine outcome material drafts one file");
    const body = JSON.parse(await readFile(path.join(vault, "inbox", names[0]), "utf8"));
    assert.equal(body.candidates.length, 1);
    assert.match(body.candidates.join("\n"), /retry fix/);
  } finally {
    if (savedHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = savedHome;
    if (savedBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = savedBypass;
    await rm(root, { recursive: true, force: true });
  }
});

// ── kilocode migration proof: factory SessionStart in identity-recall mode ───

test("SessionStart hook (kilocode): identity-recall envelope keeps recall + pending_learnings, no layer1 fallbacks", { timeout: 120_000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-kilo-boot-"));
  const fixture = { vault: path.join(root, "kilocode-vault"), home: path.join(root, "home") };
  try {
    await mkdir(fixture.home, { recursive: true });
    const output = await runHook(
      path.join(PLUGIN_ROOT, "dist", "kilocode-hook.js"),
      "SessionStart",
      fixture,
      { MINNI_KILOCODE_VAULT_PATH: fixture.vault, MINNI_KILOCODE_HOOKS: "on" },
      { session_id: "kilo-boot" },
    );
    const context = output.hookSpecificOutput?.additionalContext ?? "";
    const raw = context.match(/<minni:context [^>]*>\n([\s\S]*?)\n<\/minni:context>/)?.[1];
    assert.ok(raw, "kilocode SessionStart must emit a minni:context envelope");
    const body = JSON.parse(raw);
    assert.equal(body.identity.agent, "kilocode");
    assert.ok(body.pending_learnings, "shared pending_learnings builder still runs");
    // identity-recall boot: recall is present (structured failure with no
    // daemon socket), while the agent-context-only fields stay absent.
    assert.ok(body.recall, "identity-recall boot surfaces a recall body");
    assert.equal(body.layer1_source, undefined);
    assert.equal(body.fallback_commands, undefined);
    assert.equal(body.identity.runtime, undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// ── Red-team pass 3: the governance breadcrumb must actually land ────────────

test("Stop breadcrumb survives a same-second UserPromptSubmit (audit throttle is per-tool)", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-throttle-"));
  const savedHome = process.env.MINNI_HOME;
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  // NO bypass on purpose: this is the shipped rate-limiter path. With the
  // limiter keyed per AGENT, the UserPromptSubmit stamp swallowed the Stop
  // breadcrumb that is now the ONLY record of a zero-candidate turn.
  delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
  try {
    const vault = path.join(root, "grok-vault");
    await mkdir(vault, { recursive: true });
    const handlers = createHookHandlers(stopConfig(vault));
    await handlers.handleUserPromptSubmit({ session_id: "z1", prompt: "how do I fix the sqlite lock" });
    await handlers.handleStop({ session_id: "z1", last_user_message: "how do I fix the sqlite lock" });

    const tail = await auditTail(vault, 20);
    assert.match(tail.text, /hook_test_user_prompt_submit/, "prompt breadcrumb recorded");
    assert.match(tail.text, /hook_test_stop/, "Stop breadcrumb must not be throttled away");

    // Flood protection is NOT reopened: a second same-window UserPromptSubmit
    // is still collapsed by its own per-tool stamp.
    await handlers.handleUserPromptSubmit({ session_id: "z1", prompt: "and the wal mode question" });
    const after = await auditTail(vault, 20);
    assert.equal(
      (after.text.match(/hook_test_user_prompt_submit/g) ?? []).length,
      1,
      "repeat prompts inside the 5s window stay collapsed",
    );
  } finally {
    if (savedHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = savedHome;
    if (savedBypass !== undefined) process.env.MINNI_BYPASS_AUDIT_LIMIT = savedBypass;
    await rm(root, { recursive: true, force: true });
  }
});

test("Two consecutive no-signal Stops both leave a breadcrumb", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stop-twice-"));
  const savedHome = process.env.MINNI_HOME;
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
  try {
    const vault = path.join(root, "grok-vault");
    await mkdir(vault, { recursive: true });
    const handlers = createHookHandlers(stopConfig(vault));
    await handlers.handleStop({ session_id: "s1", last_user_message: "first" });
    await handlers.handleStop({ session_id: "s2", last_user_message: "second" });
    const tail = await auditTail(vault, 20);
    assert.equal(
      (tail.text.match(/hook_test_stop/g) ?? []).length,
      2,
      "the Stop breadcrumb is exempt from the throttle — it is the only record",
    );
  } finally {
    if (savedHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = savedHome;
    if (savedBypass !== undefined) process.env.MINNI_BYPASS_AUDIT_LIMIT = savedBypass;
    await rm(root, { recursive: true, force: true });
  }
});

test("Stop summary-only payload does not duplicate summary as task", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-sum-"));
  const savedHome = process.env.MINNI_HOME;
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  try {
    const vault = path.join(root, "codex-vault");
    await mkdir(vault, { recursive: true });
    const handlers = createHookHandlers(stopConfig(vault, "codex"));
    const summary = "shipped the retry fix and verified the suite passes";
    await handlers.handleStop({
      session_id: "sum-only-1",
      summary,
    });
    const names = (await readdir(path.join(vault, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.equal(names.length, 1);
    const body = JSON.parse(await readFile(path.join(vault, "inbox", names[0]), "utf8"));
    assert.equal(body.candidates.length, 1);
    assert.equal(body.candidates[0], `sum-only-1: ${summary}`);
    assert.notEqual(body.candidates[0], `${summary}: ${summary}`);
  } finally {
    if (savedHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = savedHome;
    if (savedBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = savedBypass;
    await rm(root, { recursive: true, force: true });
  }
});

test("Stop stamps the vault-derived principal, not the configured agent id", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stamp-"));
  const savedHome = process.env.MINNI_HOME;
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  try {
    // MINNI_CLAUDECODE_AGENT_ID=claudecode is a legal operator setting; the
    // ingest compares the stamp against the vault-dir principal (claude-code),
    // so stamping the raw config id dropped every row as _agent_mismatch.
    const vault = path.join(root, "claudecode-vault");
    await mkdir(vault, { recursive: true });
    const handlers = createHookHandlers(stopConfig(vault, "claudecode"));
    await handlers.handleStop({
      session_id: "stamp-1",
      summary: "shipped the retry fix and verified the suite passes",
      changedFiles: ["src/retry.ts"],
    });
    const names = (await readdir(path.join(vault, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.equal(names.length, 1);
    const body = JSON.parse(await readFile(path.join(vault, "inbox", names[0]), "utf8"));
    assert.equal(body.agent_id, "claude-code", "stamp must match _principal_for_inbox");

    // A vault dir with no `-vault` suffix has no derivable principal on this
    // side (python falls back to a value we cannot know), so no stamp at all —
    // an absent agent_id skips the cross-check instead of failing it.
    const bare = path.join(root, "vault");
    await mkdir(bare, { recursive: true });
    const bareHandlers = createHookHandlers(stopConfig(bare, "claudecode"));
    await bareHandlers.handleStop({
      session_id: "stamp-2",
      summary: "shipped the retry fix and verified the suite passes",
      changedFiles: ["src/retry.ts"],
    });
    const bareNames = (await readdir(path.join(bare, "inbox"))).filter((n) => n.endsWith(".json"));
    assert.equal(bareNames.length, 1);
    const bareBody = JSON.parse(await readFile(path.join(bare, "inbox", bareNames[0]), "utf8"));
    assert.equal(bareBody.agent_id, undefined, "no derivable principal => no stamp");
  } finally {
    if (savedHome === undefined) delete process.env.MINNI_HOME;
    else process.env.MINNI_HOME = savedHome;
    if (savedBypass === undefined) delete process.env.MINNI_BYPASS_AUDIT_LIMIT;
    else process.env.MINNI_BYPASS_AUDIT_LIMIT = savedBypass;
    await rm(root, { recursive: true, force: true });
  }
});

// ── Review finding (medium): synthetic "session" id must never be stamped ───
// into audit details.session_id or threaded into the daemon recall-trace.
// UserPromptSubmit's weak (nothing-salient) and strong (recall_pointer) paths
// both stamp details.session_id conditionally on the RAW payload id; the
// "session" fallback stays reserved for envelope identity / inbox filenames.

function upsConfig(vaultPath) {
  return {
    agentId: "claude-code",
    vaultPath,
    defaultWorkspaceId: "workspace-fixture",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
    alwaysWriteStopInbox: false,
  };
}

async function withRawSessionFixture(run) {
  const root = await mkdtemp(path.join(tmpdir(), "sm-raw-session-"));
  const vault = path.join(root, "vault");
  const home = path.join(root, "home");
  const saved = {
    home: process.env.MINNI_HOME,
    socket: process.env.MINNI_SOCKET_PATH,
    afm: process.env.MINNI_AFM_HEALTH_URL,
    bypass: process.env.MINNI_BYPASS_AUDIT_LIMIT,
  };
  process.env.MINNI_HOME = home;
  process.env.MINNI_SOCKET_PATH = path.join(home, "missing.sock");
  process.env.MINNI_AFM_HEALTH_URL = "http://127.0.0.1:1/health";
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  await mkdir(home, { recursive: true });
  try {
    await run({ vault, home });
  } finally {
    for (const [key, value] of [
      ["MINNI_HOME", saved.home],
      ["MINNI_SOCKET_PATH", saved.socket],
      ["MINNI_AFM_HEALTH_URL", saved.afm],
      ["MINNI_BYPASS_AUDIT_LIMIT", saved.bypass],
    ]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await rm(root, { recursive: true, force: true });
  }
}

/** Finds the last audit entry for `tool` and returns its parsed details JSON. */
async function lastAuditDetails(vaultPath, tool) {
  const tail = await auditTail(vaultPath, 20);
  for (let i = tail.entries.length - 1; i >= 0; i -= 1) {
    const entry = tail.entries[i];
    if (!entry.includes(`] ${tool} |`)) continue;
    const match = entry.match(/```json\n([\s\S]*?)\n```/);
    return match ? JSON.parse(match[1]) : undefined;
  }
  return undefined;
}

test("UserPromptSubmit audit (weak/nothing-salient path): no session_id in payload omits details.session_id", async () => {
  await withRawSessionFixture(async ({ vault }) => {
    const handlers = createHookHandlers(upsConfig(vault));
    // No vault notes + missing daemon socket => weak recall; no active plan
    // => the nothing-salient early return, which is the audit call under test.
    const output = await handlers.handleUserPromptSubmit({
      prompt: "an utterly novel question with zero prior memory zzzqqx",
      workspace_id: "workspace-fixture",
      // deliberately NO session_id / sessionId field
    });
    assert.equal(output.continue, true);

    const details = await lastAuditDetails(vault, "hook_test_user_prompt_submit");
    assert.ok(details, "UserPromptSubmit must record an audit entry");
    assert.equal(
      Object.prototype.hasOwnProperty.call(details, "session_id"),
      false,
      "unlabeled payload must not stamp a synthetic session_id into audit details",
    );
  });
});

test("UserPromptSubmit audit (weak/nothing-salient path): a real session_id is stamped as before", async () => {
  await withRawSessionFixture(async ({ vault }) => {
    const handlers = createHookHandlers(upsConfig(vault));
    const output = await handlers.handleUserPromptSubmit({
      prompt: "another utterly novel question with zero prior memory wobblequux",
      workspace_id: "workspace-fixture",
      session_id: "real-session-42",
    });
    assert.equal(output.continue, true);

    const details = await lastAuditDetails(vault, "hook_test_user_prompt_submit");
    assert.ok(details);
    assert.equal(details.session_id, "real-session-42");
  });
});

test("UserPromptSubmit audit (strong/recall_pointer path): no session_id in payload omits details.session_id", async () => {
  await withRawSessionFixture(async ({ vault }) => {
    const prompt = "resume the raw-session-id review-finding investigation from prior context";
    const wiki = path.join(vault, "wiki", "sessions");
    await mkdir(wiki, { recursive: true });
    await writeFile(
      path.join(wiki, "20260617-raw-session-strong-hit.md"),
      `# Strong hit note\n\nThe exact phrase: ${prompt}\nDocumented so the agent need not re-derive it.\n`,
      "utf8",
    );

    const handlers = createHookHandlers(upsConfig(vault));
    const output = await handlers.handleUserPromptSubmit({
      prompt,
      workspace_id: "workspace-fixture",
      // deliberately NO session_id / sessionId field
    });
    assert.equal(output.continue, true);
    assert.ok(output.hookSpecificOutput, "strong turn must inject an envelope");

    const details = await lastAuditDetails(vault, "hook_test_user_prompt_submit");
    assert.ok(details);
    assert.equal(details.recall_strong, true, "precondition: this must be the strong-recall path");
    assert.equal(
      Object.prototype.hasOwnProperty.call(details, "session_id"),
      false,
      "unlabeled payload must not stamp a synthetic session_id into audit details",
    );
  });
});

test("UserPromptSubmit audit (strong/recall_pointer path): a real session_id is stamped as before", async () => {
  await withRawSessionFixture(async ({ vault }) => {
    const prompt = "resume the raw-session-id stamped review-finding investigation from prior context";
    const wiki = path.join(vault, "wiki", "sessions");
    await mkdir(wiki, { recursive: true });
    await writeFile(
      path.join(wiki, "20260617-raw-session-strong-hit-2.md"),
      `# Strong hit note\n\nThe exact phrase: ${prompt}\nDocumented so the agent need not re-derive it.\n`,
      "utf8",
    );

    const handlers = createHookHandlers(upsConfig(vault));
    const output = await handlers.handleUserPromptSubmit({
      prompt,
      workspace_id: "workspace-fixture",
      session_id: "real-session-strong-7",
    });
    assert.equal(output.continue, true);

    const details = await lastAuditDetails(vault, "hook_test_user_prompt_submit");
    assert.ok(details);
    assert.equal(details.recall_strong, true, "precondition: this must be the strong-recall path");
    assert.equal(details.session_id, "real-session-strong-7");
  });
});
