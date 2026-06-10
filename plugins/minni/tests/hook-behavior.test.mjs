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
import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

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

async function runSessionStart(fixture) {
  const env = {
    ...process.env,
    MINNI_CLAUDECODE_VAULT_PATH: fixture.vault,
    MINNI_CLAUDECODE_HOOKS: "on",
    MINNI_HOME: fixture.home,
    MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
  };
  const child = execFileAsync(process.execPath, [HOOK_JS, "SessionStart"], {
    env,
    timeout: 30_000,
  });
  child.child.stdin.end(JSON.stringify({ session_id: "fixture-session" }));
  const { stdout } = await child;
  const output = JSON.parse(stdout.trim().split("\n").pop());
  assert.equal(output.continue, true);
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
