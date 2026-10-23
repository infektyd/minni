// Recall-to-use S1a-2: a ledger that cannot be written never fails a hook.
// With `.runtime/use-ledger` a plain file, a symlink that escapes the vault,
// the per-session counters file unwritable, the session directory read-only,
// or a FIFO where the counts file or a ledger day file should be, every hook
// exits 0, says nothing on stderr, and returns byte-for-byte what it returns
// with a working ledger. The escape target stays empty.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmod, mkdir, mkdtemp, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  daemonResult,
  makeFixture,
  closeFixture,
  readLedger,
  runHook,
  sessionKeyOf,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

const SESSION = "unwritable-session";

const daemon = {
  search: (params) => ({
    agent_id: "claude-code",
    results: String(params.query ?? "").includes("STRONGQ") || String(params.query ?? "").startsWith("boot identity")
      ? [daemonResult({ source: "/elsewhere/deploy.md", docId: 4, wikilink: "[[deploy]]", title: "Deploy" })]
      : [],
  }),
  read: () => ({
    agent_id: "claude-code",
    context: "## Learnings (claude-code)\n  - [fix] a (conf=1.0)",
    recent_learnings: [{ learning_id: 1, category: "fix", line: "  - [fix] a (conf=1.0)", content: "a" }],
  }),
};

/** Every capturing hook, in order; returns each one's exact output line. */
async function driveHooks(fixture) {
  const steps = [
    ["SessionStart", { session_id: SESSION, source: "startup" }],
    [
      "UserPromptSubmit",
      {
        session_id: SESSION,
        prompt_id: "u1",
        prompt: "Recurring check: review the open pull requests, summarise the CI failures, and list blockers.",
      },
    ],
    ["UserPromptSubmit", { session_id: SESSION, prompt_id: "u2", prompt: "STRONGQ deploy notes" }],
    ["PreToolUse", { session_id: SESSION, tool_name: "Read", tool_input: { file_path: "/tmp/x.txt" } }],
    ["PreCompact", { session_id: SESSION, trigger: "auto" }],
    ["PostCompact", { session_id: SESSION, trigger: "auto", compact_summary: "" }],
  ];
  const outputs = [];
  for (const [event, payload] of steps) {
    const { raw, code, stderr } = await runHook(fixture, event, payload);
    assert.equal(code, 0, `${event} exit code`);
    assert.equal(stderr.trim(), "", `${event} wrote to stderr: ${stderr}`);
    outputs.push([event, raw]);
  }
  return outputs;
}

async function withRoot(t, label) {
  const root = path.join(tmpdir(), `minni-unwritable-${label}-${process.pid}`);
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

async function baseline(root) {
  const fixture = await makeFixture(null, "unwritable", { at: root });
  await startFakeDaemon(fixture, daemon);
  try {
    const outputs = await driveHooks(fixture);
    const rows = await readLedger(fixture.vault);
    assert.ok(rows.length > 0, "control: a working ledger records rows");
    return outputs;
  } finally {
    await closeFixture(fixture);
  }
}

async function broken(root, breakIt) {
  const fixture = await makeFixture(null, "unwritable", { at: root });
  await startFakeDaemon(fixture, daemon);
  try {
    await breakIt(fixture);
    return await driveHooks(fixture);
  } finally {
    if (fixture.restore) await fixture.restore();
    await closeFixture(fixture);
  }
}

test("use-ledger is a plain file: every hook still returns its normal output", { timeout: 240_000 }, async (t) => {
  const root = await withRoot(t, "file");
  const want = await baseline(root);
  const got = await broken(root, async (fixture) => {
    await mkdir(path.join(fixture.vault, ".runtime"), { recursive: true });
    await writeFile(path.join(fixture.vault, ".runtime", "use-ledger"), "not a directory\n", "utf8");
  });
  assert.deepEqual(got, want);
});

test("use-ledger is a symlink out of the vault: refused, outputs unchanged, target untouched", { timeout: 240_000 }, async (t) => {
  const root = await withRoot(t, "symlink");
  const outside = await mkdtemp(path.join(tmpdir(), "minni-unwritable-outside-"));
  t.after(() => rm(outside, { recursive: true, force: true }));
  const want = await baseline(root);
  const got = await broken(root, async (fixture) => {
    await mkdir(path.join(fixture.vault, ".runtime"), { recursive: true });
    await symlink(outside, path.join(fixture.vault, ".runtime", "use-ledger"));
  });
  assert.deepEqual(got, want);
  assert.deepEqual(await readdir(outside), [], "nothing may be written through the escape");
});

test("the counters file is unwritable: outputs unchanged", { timeout: 240_000 }, async (t) => {
  const root = await withRoot(t, "counters");
  const want = await baseline(root);
  const got = await broken(root, async (fixture) => {
    const dir = path.join(fixture.vault, ".runtime", "recall-session.d", sessionKeyOf(SESSION));
    // A directory where counters.json should be: it can be neither read nor replaced.
    await mkdir(path.join(dir, "counters.json", "occupied"), { recursive: true });
  });
  assert.deepEqual(got, want);
});

function mkfifo(target) {
  execFileSync("mkfifo", [target]);
}

test("a FIFO in place of the counts file or of today's ledger file cannot hang a hook", { timeout: 240_000 }, async (t) => {
  if (process.platform === "win32") {
    t.skip("no FIFOs");
    return;
  }
  const root = await withRoot(t, "fifo");
  const want = await baseline(root);
  const got = await broken(root, async (fixture) => {
    const runtime = path.join(fixture.vault, ".runtime");
    await mkdir(runtime, { recursive: true });
    // Reading a FIFO with no writer blocks; so does opening one for write with no reader.
    mkfifo(path.join(runtime, "prompt-templates.json"));
    const day = new Date().toISOString().slice(0, 10);
    const sessionDir = path.join(runtime, "use-ledger", sessionKeyOf(SESSION));
    await mkdir(sessionDir, { recursive: true });
    mkfifo(path.join(sessionDir, `${day}.jsonl`));
  });
  assert.deepEqual(got, want);
});

test("the session directory is read-only: outputs unchanged", { timeout: 240_000 }, async (t) => {
  if (process.getuid?.() === 0) {
    t.skip("root ignores directory permissions");
    return;
  }
  const root = await withRoot(t, "readonly");
  const want = await baseline(root);
  const got = await broken(root, async (fixture) => {
    const dir = path.join(fixture.vault, ".runtime", "recall-session.d", sessionKeyOf(SESSION));
    await mkdir(dir, { recursive: true });
    await chmod(dir, 0o500);
    fixture.restore = () => chmod(dir, 0o700).catch(() => {});
  });
  assert.deepEqual(got, want);
});
