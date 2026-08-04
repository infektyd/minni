// Layer 1 shelf inline (operator scar 2026-08-04): the SessionStart
// <minni:context> envelope carried only identity keys + a pointer, so an
// agent's model ladder and orchestration scars — layer1/core.md — never rode
// the boot envelope itself. Three recorded scars got re-hit the same night
// because of it. This inlines core.md into `layer1_shelf` on SessionStart
// ONLY (UserPromptSubmit's envelope stays lean by design).
//
// BEHAVIORAL, not source-text grep (standing scar): every assertion here
// drives the real built hook (`node dist/hook.js SessionStart`) against a
// temp vault fixture and reads the actual envelope it emits. Isolation
// follows tests/hook-behavior.test.mjs: every env knob points inside the tmp
// fixture, the daemon socket is absent, the AFM health URL is a closed
// loopback port. Live ~/.minni is never touched.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { createHookHandlers } from "../dist/hook-handlers.js";
import { LAYER1_SHELF_MAX_BYTES, readFullOrEof, readLayer1Shelf } from "../dist/vault.js";

const execFileAsync = promisify(execFile);
const PLUGIN_ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK_JS = path.join(PLUGIN_ROOT, "dist", "hook.js");

async function makeFixture(prefix) {
  const root = await mkdtemp(path.join(tmpdir(), prefix));
  const fixture = {
    root,
    vault: path.join(root, "claudecode-vault"),
    home: path.join(root, "home"),
  };
  await mkdir(fixture.vault, { recursive: true });
  await mkdir(fixture.home, { recursive: true });
  return fixture;
}

async function runHook(event, fixture, payload) {
  const env = {
    ...process.env,
    MINNI_HOME: fixture.home,
    MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
    MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
    MINNI_BYPASS_AUDIT_LIMIT: "true",
    MINNI_CLAUDECODE_VAULT_PATH: fixture.vault,
    MINNI_CLAUDECODE_HOOKS: "on",
  };
  const child = execFileAsync(process.execPath, [HOOK_JS, event], { env, timeout: 30_000 });
  child.child.stdin.end(JSON.stringify(payload));
  const { stdout } = await child;
  const output = JSON.parse(stdout.trim().split("\n").pop());
  assert.equal(output.continue, true);
  return output;
}

/** Extracts and parses the <minni:context> envelope body, or undefined if the
 * hook emitted no injection (e.g. a wire that cannot inject at this event). */
function envelopeBody(output) {
  const context = output.hookSpecificOutput?.additionalContext ?? "";
  const match = context.match(/<minni:context [^>]*>\n([\s\S]*?)\n<\/minni:context>/);
  return match ? JSON.parse(match[1]) : undefined;
}

async function runSessionStart(fixture) {
  const output = await runHook("SessionStart", fixture, { session_id: "layer1-shelf-fixture" });
  const body = envelopeBody(output);
  assert.ok(body, "SessionStart must emit a minni:context envelope");
  return body;
}

test(
  "layer1_shelf: a present core.md is inlined verbatim, not truncated",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-layer1-shelf-present-");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    const coreText = "# Identity\n\nModel ladder: sonnet -> opus on stall.\n\nScar: never trust a green CI without the daemon check.\n";
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), coreText, "utf8");

    const body = await runSessionStart(fixture);

    assert.ok(body.layer1_shelf, "layer1_shelf must be present as a top-level envelope key");
    assert.equal(body.layer1_shelf.ok, true);
    assert.equal(body.layer1_shelf.content, coreText, "content must be inlined verbatim");
    assert.equal(body.layer1_shelf.truncated, false);
    assert.equal(body.layer1_shelf.omitted_bytes, undefined);
  },
);

test(
  "layer1_shelf: an oversized core.md is truncated with an explicit, sized marker (no silent truncation)",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-layer1-shelf-oversized-");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));

    const overBy = 500;
    const bigCore = "x".repeat(LAYER1_SHELF_MAX_BYTES + overBy);
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), bigCore, "utf8");

    const body = await runSessionStart(fixture);

    assert.equal(body.layer1_shelf.ok, true);
    assert.equal(body.layer1_shelf.truncated, true, "an oversized shelf must be flagged truncated");
    assert.equal(
      body.layer1_shelf.content.length,
      LAYER1_SHELF_MAX_BYTES,
      "the inlined content must stop at the byte cap",
    );
    assert.equal(
      body.layer1_shelf.omitted_bytes,
      overBy,
      "the omitted byte count must be exact, not a rounded or vague estimate",
    );
    assert.ok(
      typeof body.layer1_shelf.note === "string" && /omitted/i.test(body.layer1_shelf.note),
      `truncation must carry an explicit note describing what was omitted (got ${JSON.stringify(body.layer1_shelf.note)})`,
    );
    assert.match(
      body.layer1_shelf.note,
      new RegExp(String(overBy)),
      "the note must state the actual omitted byte count, not just that truncation happened",
    );
  },
);

test(
  "layer1_shelf: a missing core.md is VISIBLY absent, never a silently missing key",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-layer1-shelf-absent-");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));
    // No layer1/ directory at all — the common case for a freshly-bootstrapped
    // or not-yet-seeded vault.

    const body = await runSessionStart(fixture);

    assert.ok(
      Object.prototype.hasOwnProperty.call(body, "layer1_shelf"),
      "layer1_shelf must be present as a key even when core.md does not exist",
    );
    assert.equal(body.layer1_shelf.ok, false);
    assert.equal(typeof body.layer1_shelf.reason, "string");
    assert.match(
      body.layer1_shelf.reason,
      /^absent:/,
      `the absent reason must be visibly labeled "absent: ..." (got ${JSON.stringify(body.layer1_shelf.reason)})`,
    );
  },
);

test(
  "layer1_shelf: an unreadable core.md (not a regular file) is also VISIBLY absent",
  { timeout: 120_000 },
  async (t) => {
    const fixture = await makeFixture("sm-layer1-shelf-notfile-");
    t.after(() => rm(fixture.root, { recursive: true, force: true }));
    // core.md as a directory: same "unreadable" family as a permissions error,
    // exercised without relying on platform-specific chmod semantics.
    await mkdir(path.join(fixture.vault, "layer1", "core.md"), { recursive: true });

    const body = await runSessionStart(fixture);

    assert.equal(body.layer1_shelf.ok, false);
    assert.match(body.layer1_shelf.reason, /^absent:/);
  },
);

test(
  "layer1_shelf: UserPromptSubmit's envelope is unchanged — no layer1_shelf key, even with core.md present",
  { timeout: 120_000 },
  async () => {
    const fixture = await makeFixture("sm-layer1-shelf-ups-");
    try {
      await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
      await writeFile(
        path.join(fixture.vault, "layer1", "core.md"),
        "# Identity\n\nModel ladder here.\n",
        "utf8",
      );

      const saved = {
        home: process.env.MINNI_HOME,
        socket: process.env.MINNI_SOCKET_PATH,
        afm: process.env.MINNI_AFM_HEALTH_URL,
        bypass: process.env.MINNI_BYPASS_AUDIT_LIMIT,
      };
      process.env.MINNI_HOME = fixture.home;
      process.env.MINNI_SOCKET_PATH = path.join(fixture.home, "missing.sock");
      process.env.MINNI_AFM_HEALTH_URL = "http://127.0.0.1:1/health";
      process.env.MINNI_BYPASS_AUDIT_LIMIT = "true";
      try {
        const handlers = createHookHandlers({
          agentId: "claude-code",
          vaultPath: fixture.vault,
          defaultWorkspaceId: "workspace-fixture",
          contextWindow: 200_000,
          hooksEnabled: true,
          auditPrefix: "hook_test",
          alwaysWriteStopInbox: false,
        });
        const output = await handlers.handleUserPromptSubmit({
          prompt: "an utterly novel question with zero prior memory zzzqqx",
        });
        const context = output.hookSpecificOutput?.additionalContext ?? output.systemMessage ?? "";
        assert.doesNotMatch(
          context,
          /layer1_shelf/,
          "UserPromptSubmit must stay lean — layer1_shelf is a SessionStart-only section",
        );
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
      }
    } finally {
      await rm(fixture.root, { recursive: true, force: true });
    }
  },
);

// ---------------------------------------------------------------------------
// Bugbot round 2 on PR #282: a single `handle.read` call is permitted by
// POSIX to return FEWER bytes than requested even on an ordinary regular
// file (a short read is not EOF). readLayer1Shelf's probe read used to be
// one call; a short read <= cap on a file actually larger than the cap
// would have reported truncated:false over incomplete content — H5 one
// layer deeper than the fstat race fixed in round 1. The fix loops the read
// (readFullOrEof) until the buffer fills or a read genuinely returns 0.
// These pin the loop directly against a fake short-read producer, since
// real filesystem reads rarely short-read in a test environment and would
// not actually exercise the accumulation path.
// ---------------------------------------------------------------------------

test(
  "readFullOrEof: accumulates across multiple short reads until the buffer is full",
  async () => {
    const length = 10;
    const buffer = Buffer.alloc(length);
    // Every call hands back at most 3 bytes, regardless of how much room is
    // left in the buffer — the shape of a real POSIX short read.
    const chunks = [3, 3, 3, 1]; // sums to 10
    let call = 0;
    const fakeRead = async (buf, offset, len, pos) => {
      const bytesRead = Math.min(chunks[call], len);
      call += 1;
      buf.fill(0x41 + offset, offset, offset + bytesRead); // distinct byte per call, for eyeballing
      return { bytesRead };
    };

    const totalRead = await readFullOrEof(fakeRead, buffer, length);

    assert.equal(totalRead, length, "short reads must accumulate to the full requested length");
    assert.equal(call, chunks.length, "the loop must keep calling read until the buffer is full");
  },
);

test(
  "readFullOrEof: stops at a genuine EOF (read returns 0) short of the target length",
  async () => {
    const length = 10;
    const buffer = Buffer.alloc(length);
    // The file only has 5 bytes: two short reads of varying size, then EOF.
    const chunks = [2, 3, 0];
    let call = 0;
    const fakeRead = async (buf, offset, len) => {
      const bytesRead = Math.min(chunks[call] ?? 0, len);
      call += 1;
      return { bytesRead };
    };

    const totalRead = await readFullOrEof(fakeRead, buffer, length);

    assert.equal(totalRead, 5, "the loop must stop at EOF, not pad past what the file actually had");
    assert.equal(call, 3, "the loop must stop calling read once it sees bytesRead === 0");
  },
);

test(
  "readFullOrEof: a read that fills the buffer in one call still terminates (no extra call past length)",
  async () => {
    const length = 4;
    const buffer = Buffer.alloc(length);
    let call = 0;
    const fakeRead = async (buf, offset, len) => {
      call += 1;
      return { bytesRead: len };
    };

    const totalRead = await readFullOrEof(fakeRead, buffer, length);

    assert.equal(totalRead, length);
    assert.equal(call, 1, "a full single read must not trigger a spurious extra call");
  },
);

// ---------------------------------------------------------------------------
// Bugbot finding on PR #282: truncated/omitted_bytes must derive from the
// READ itself, never a pre-read fstat — a stale size (file grown or shrunk
// between stat and read) previously produced either silently-incomplete
// content with truncated:false (the exact H5 failure this field exists to
// prevent) or a false truncated:true with a wrong omitted_bytes. The fixed
// readLayer1Shelf probes LAYER1_SHELF_MAX_BYTES + 1 bytes and derives
// `truncated` from bytesRead alone. These pin the boundary exactly: a file
// sized precisely AT the cap must read whole, and one byte past it must
// truncate — the two cases a pre-read-stat implementation could get right
// by accident but a bytesRead-off-by-one would get wrong.
// ---------------------------------------------------------------------------

test(
  "layer1_shelf boundary: a core.md sized EXACTLY at the cap is not truncated",
  { timeout: 30_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-layer1-shelf-boundary-exact-"));
    const vault = path.join(root, "vault");
    await mkdir(path.join(vault, "layer1"), { recursive: true });
    t.after(() => rm(root, { recursive: true, force: true }));

    const exact = "y".repeat(LAYER1_SHELF_MAX_BYTES);
    await writeFile(path.join(vault, "layer1", "core.md"), exact, "utf8");

    const result = await readLayer1Shelf(vault);

    assert.equal(result.ok, true);
    assert.equal(result.truncated, false, "a file of exactly the cap size must read whole");
    assert.equal(result.content.length, LAYER1_SHELF_MAX_BYTES);
    assert.equal(result.omittedBytes, undefined);
  },
);

test(
  "layer1_shelf boundary: one byte past the cap is truncated (derived from the read, not a stale stat)",
  { timeout: 30_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-layer1-shelf-boundary-over-"));
    const vault = path.join(root, "vault");
    await mkdir(path.join(vault, "layer1"), { recursive: true });
    t.after(() => rm(root, { recursive: true, force: true }));

    const overByOne = "z".repeat(LAYER1_SHELF_MAX_BYTES + 1);
    await writeFile(path.join(vault, "layer1", "core.md"), overByOne, "utf8");

    const result = await readLayer1Shelf(vault);

    assert.equal(result.ok, true);
    assert.equal(result.truncated, true, "one byte past the cap must trip truncation");
    assert.equal(result.content.length, LAYER1_SHELF_MAX_BYTES);
    assert.equal(
      result.omittedBytes,
      1,
      "the omitted count for a static one-byte overflow must be exactly 1, not off by the probe byte",
    );
  },
);

// ---------------------------------------------------------------------------
// Drift guard: the shared factory path (hook-handlers.ts, used by codex,
// grok, gemini, cursor) is a SEPARATE call site from claude-code's own
// hook.ts. Pin at least one factory entrypoint too, so a future envelope
// edit on either side cannot drop layer1_shelf without a test noticing.
// ---------------------------------------------------------------------------

const CODEX_HOOK_JS = path.join(PLUGIN_ROOT, "dist", "codex-hook.js");

test(
  "layer1_shelf: present on the shared factory path too (codex SessionStart)",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-layer1-shelf-codex-"));
    const fixture = { root, vault: path.join(root, "codex-vault"), home: path.join(root, "home") };
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    await mkdir(fixture.home, { recursive: true });
    const coreText = "# Identity\n\nCodex-path fixture core.md.\n";
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), coreText, "utf8");
    t.after(() => rm(root, { recursive: true, force: true }));

    const env = {
      ...process.env,
      MINNI_HOME: fixture.home,
      MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
      MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
      MINNI_BYPASS_AUDIT_LIMIT: "true",
      MINNI_CODEX_AGENT_ID: "codex",
      MINNI_CODEX_VAULT_PATH: fixture.vault,
      MINNI_CODEX_HOOKS: "on",
    };
    const child = execFileAsync(process.execPath, [CODEX_HOOK_JS, "SessionStart"], {
      env,
      timeout: 30_000,
    });
    child.child.stdin.end(JSON.stringify({ session_id: "codex-layer1-shelf" }));
    const { stdout } = await child;
    const output = JSON.parse(stdout.trim().split("\n").pop());
    const body = envelopeBody(output);
    assert.ok(body, "codex SessionStart must emit a minni:context envelope");

    assert.equal(body.layer1_shelf.ok, true);
    assert.equal(body.layer1_shelf.content, coreText);
  },
);

// ---------------------------------------------------------------------------
// grok-build: SessionStart is structurally un-injectable on this wire
// (hook-platform.ts GROK_INJECTABLE = {"Stop"}). It must NOT get a special
// case here — it simply never receives layer1_shelf at boot, same as every
// other SessionStart section. This pins that as intentional, not a bug.
// ---------------------------------------------------------------------------

const GROK_HOOK_JS = path.join(PLUGIN_ROOT, "dist", "grok-hook.js");

test(
  "layer1_shelf: grok-build's un-injectable SessionStart drops the whole envelope, layer1_shelf included — no special-casing",
  { timeout: 120_000 },
  async (t) => {
    const root = await mkdtemp(path.join(tmpdir(), "sm-layer1-shelf-grok-"));
    const fixture = { root, vault: path.join(root, "grok-build-vault"), home: path.join(root, "home") };
    await mkdir(path.join(fixture.vault, "layer1"), { recursive: true });
    await mkdir(fixture.home, { recursive: true });
    await writeFile(path.join(fixture.vault, "layer1", "core.md"), "# Identity\n", "utf8");
    t.after(() => rm(root, { recursive: true, force: true }));

    const env = {
      ...process.env,
      MINNI_HOME: fixture.home,
      MINNI_SOCKET_PATH: path.join(fixture.home, "missing.sock"),
      MINNI_AFM_HEALTH_URL: "http://127.0.0.1:1/health",
      MINNI_BYPASS_AUDIT_LIMIT: "true",
      MINNI_GROK_VAULT_PATH: fixture.vault,
      MINNI_GROK_HOOKS: "on",
    };
    const child = execFileAsync(process.execPath, [GROK_HOOK_JS, "SessionStart"], {
      env,
      timeout: 30_000,
    });
    child.child.stdin.end(JSON.stringify({ session_id: "grok-layer1-shelf" }));
    const { stdout } = await child;
    const output = JSON.parse(stdout.trim().split("\n").pop());

    assert.equal(
      output.hookSpecificOutput,
      undefined,
      "grok-build's SessionStart wire is structurally un-injectable — the whole envelope, including layer1_shelf, must be dropped, not carried some other way",
    );
  },
);
