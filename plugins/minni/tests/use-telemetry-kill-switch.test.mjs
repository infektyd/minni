// Recall-to-use S1a-2 (§8.2): with MINNI_USE_LEDGER off, capture writes
// nothing at all: no ledger, no per-session counters or reset files, no counts
// file. Off is honoured from env and from `<vault>/.runtime/flags.json`. The
// same run with it on is the positive control: it must create all of them.
import assert from "node:assert/strict";
import { mkdir, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { daemonResult, exists, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

const LONG_PROMPT =
  "Recurring check: review the open pull requests, summarise the CI failures, and list anything blocked on review today.";

async function driveEveryCapture(fixture, env) {
  const session = { session_id: "kill-switch-session" };
  await runHook(fixture, "SessionStart", { ...session, source: "startup" }, { env });
  await runHook(fixture, "UserPromptSubmit", { ...session, prompt_id: "k1", prompt: LONG_PROMPT }, { env });
  await runHook(fixture, "UserPromptSubmit", { ...session, prompt_id: "k2", prompt: "STRONGQ deploy notes" }, { env });
  await runHook(
    fixture,
    "PreToolUse",
    { ...session, tool_name: "Read", tool_input: { file_path: "/tmp/x.txt" } },
    { env },
  );
  await runHook(fixture, "PreCompact", { ...session, trigger: "auto" }, { env });
  await runHook(fixture, "PostCompact", { ...session, trigger: "auto", compact_summary: "" }, { env });
}

async function capturePaths(vault) {
  const runtime = path.join(vault, ".runtime");
  return {
    ledger: await exists(path.join(runtime, "use-ledger")),
    sessions: await exists(path.join(runtime, "recall-session.d")),
    counts: await exists(path.join(runtime, "prompt-templates.json")),
  };
}

async function daemonFor(fixture) {
  await startFakeDaemon(fixture, {
    search: (params) => ({
      agent_id: "claude-code",
      results: String(params.query ?? "").includes("STRONGQ")
        ? [daemonResult({ source: "/elsewhere/deploy.md", docId: 4, wikilink: "[[deploy]]", title: "Deploy" })]
        : [],
    }),
    read: () => ({
      agent_id: "claude-code",
      context: "## Learnings (claude-code)\n  - [fix] a (conf=1.0)",
      recent_learnings: [{ learning_id: 1, category: "fix", line: "  - [fix] a (conf=1.0)", content: "a" }],
    }),
  });
}

test("ledger on (control): every capture file is created", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "kill-switch-on");
  await daemonFor(fixture);
  await driveEveryCapture(fixture, { MINNI_USE_LEDGER: "on" });
  assert.deepEqual(await capturePaths(fixture.vault), { ledger: true, sessions: true, counts: true });
});

test("MINNI_USE_LEDGER=off in env: no ledger, counters, reset or counts files", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "kill-switch-env");
  await daemonFor(fixture);
  await driveEveryCapture(fixture, { MINNI_USE_LEDGER: "off" });
  assert.deepEqual(await capturePaths(fixture.vault), { ledger: false, sessions: false, counts: false });
});

test("MINNI_USE_LEDGER off in flags.json: no ledger, counters, reset or counts files", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "kill-switch-file");
  await daemonFor(fixture);
  await mkdir(path.join(fixture.vault, ".runtime"), { recursive: true });
  await writeFile(path.join(fixture.vault, ".runtime", "flags.json"), '{"MINNI_USE_LEDGER": "off"}\n', "utf8");
  await driveEveryCapture(fixture, {});
  assert.deepEqual(await capturePaths(fixture.vault), { ledger: false, sessions: false, counts: false });
  const runtime = (await readdir(path.join(fixture.vault, ".runtime"))).sort();
  for (const name of runtime) {
    assert.ok(!/use-ledger|recall-session|prompt-templates/.test(name), `unexpected capture file ${name}`);
  }
});
