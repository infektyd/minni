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
import { mkdir, mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { createHookHandlers } from "../dist/hook-handlers.js";

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
  },
];

for (const agent of STOP_AGENTS) {
  test(`Stop hook (${agent.name}): drafts ONE stop_candidates inbox file with identity stamps`, { timeout: 120_000 }, async () => {
    const root = await mkdtemp(path.join(tmpdir(), `sm-hook-stop-${agent.name}-`));
    const fixture = { vault: path.join(root, "vault"), home: path.join(root, "home") };
    try {
      await mkdir(fixture.home, { recursive: true });
      const output = await runHook(
        path.join(PLUGIN_ROOT, "dist", agent.hookJs),
        "Stop",
        fixture,
        agent.env(fixture.vault),
        {
          session_id: "stop-fixture",
          last_user_message: "fixture stop task",
          workspace_id: "fixture-workspace",
        },
      );
      assert.match(output.systemMessage ?? "", /drafted to inbox/);

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

// The deterministic outcome draft always yields one candidate, so the
// zero-candidate branch is driven through the factory's test seam: an
// injected prepareOutcome returning an empty draft.
function emptyOutcomeStub() {
  return async () => ({
    outcomeDraft: { learnCandidates: [], logOnly: [], expires: [], doNotStore: [] },
  });
}

function stopConfig(vaultPath, alwaysWriteStopInbox) {
  return {
    agentId: alwaysWriteStopInbox ? "codex" : "grok-build",
    vaultPath,
    defaultWorkspaceId: "fixture-workspace",
    contextWindow: 200_000,
    hooksEnabled: true,
    auditPrefix: "hook_test",
    alwaysWriteStopInbox,
  };
}

test("Stop divergence: empty candidates skip the inbox write unless alwaysWriteStopInbox", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-hook-stop-empty-"));
  const savedHome = process.env.MINNI_HOME;
  const savedBypass = process.env.MINNI_BYPASS_AUDIT_LIMIT;
  process.env.MINNI_HOME = path.join(root, "home");
  process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
  try {
    // grok/kilocode behavior (alwaysWriteStopInbox: false): an empty outcome
    // never litters the inbox with a zero-candidate file.
    const grokVault = path.join(root, "grok-vault");
    const grokHandlers = createHookHandlers(stopConfig(grokVault, false), {
      prepareOutcome: emptyOutcomeStub(),
    });
    const grokOut = await grokHandlers.handleStop({ session_id: "empty-stop" });
    assert.equal(grokOut.continue, true);
    assert.equal(grokOut.systemMessage, undefined);
    const grokNames = (await readdir(path.join(grokVault, "inbox"))).filter((n) =>
      n.endsWith(".json"),
    );
    assert.deepEqual(grokNames, [], "empty outcome must not write an inbox file");

    // codex historical behavior (alwaysWriteStopInbox: true): the file IS
    // written — and still carries the canonical kind + identity stamps.
    const codexVault = path.join(root, "codex-vault");
    const codexHandlers = createHookHandlers(stopConfig(codexVault, true), {
      prepareOutcome: emptyOutcomeStub(),
    });
    const codexOut = await codexHandlers.handleStop({ session_id: "empty-stop" });
    assert.equal(codexOut.continue, true);
    assert.equal(codexOut.systemMessage, undefined, "no candidates => no call-to-action");
    const codexNames = (await readdir(path.join(codexVault, "inbox"))).filter((n) =>
      n.endsWith(".json"),
    );
    assert.equal(codexNames.length, 1);
    const body = JSON.parse(
      await readFile(path.join(codexVault, "inbox", codexNames[0]), "utf8"),
    );
    assert.equal(body.kind, "stop_candidates");
    assert.equal(body.agent_id, "codex");
    assert.equal(body.workspace_id, "fixture-workspace");
    assert.deepEqual(body.candidates, []);
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
