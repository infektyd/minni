// Recall-to-use S1a-2 (§8.2): the flags are loaded once per hook process, and
// when they have problems (a value that is not "on"/"off", an unknown flag, a
// file that cannot be used) one throttled audit line names the problems, never
// the offending values and never a path.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { auditTail } from "../dist/vault.js";
import { makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

test("flag problems are audited once per hook, by name, with no values or paths", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "flag-notes");
  await startFakeDaemon(fixture);
  await mkdir(path.join(fixture.vault, ".runtime"), { recursive: true });
  await writeFile(
    path.join(fixture.vault, ".runtime", "flags.json"),
    JSON.stringify({ MINNI_USE_LEDGER: "maybe-zq7", MINNI_SECRETISH_FLAG_ZQ8: "on" }),
    "utf8",
  );
  const { code } = await runHook(
    fixture,
    "UserPromptSubmit",
    { session_id: "flag-notes", prompt: "a question about flags" },
    { env: { MINNI_BOOT_FIT: "sideways-zq9" } },
  );
  assert.equal(code, 0);

  const { entries } = await auditTail(fixture.vault, 50);
  const notes = entries.filter((entry) => entry.includes("hook_flags_note"));
  assert.equal(notes.length, 1, `one note line per hook: ${JSON.stringify(entries)}`);
  const [note] = notes;
  assert.match(note, /file_value_ignored:MINNI_USE_LEDGER/);
  assert.match(note, /env_value_ignored:MINNI_BOOT_FIT/);
  assert.match(note, /file_unknown_flag/);
  for (const secret of ["maybe-zq7", "SECRETISH", "zq8", "sideways-zq9", "flags.json", fixture.vault, fixture.root]) {
    assert.ok(!note.includes(secret), `the note leaked ${secret}: ${note}`);
  }
});

test("clean flags write no note", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "flag-notes-clean");
  await startFakeDaemon(fixture);
  await runHook(fixture, "UserPromptSubmit", { session_id: "flag-notes-clean", prompt: "a question" });
  const { entries } = await auditTail(fixture.vault, 50);
  assert.equal(entries.filter((entry) => entry.includes("hook_flags_note")).length, 0);
});
