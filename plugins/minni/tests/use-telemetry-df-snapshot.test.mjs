// Recall-to-use S1b (§4.4 "Distinctive tokens"): the use detector judges
// titles against `.runtime/df-snapshot.json` and never scans the vault.
//
// UPS is read-only for the snapshot. A recount over the whole vault costs
// about 145 ms of tokenisation at the real vault size, which breaks the
// prompt budget, so UPS never counts, never writes the snapshot, and never
// loads the detector to do so, whatever the snapshot's age.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, stat, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { DF_SNAPSHOT_RELPATH } from "../dist/use-detector.js";
import { writeDfSnapshot as writeFixtureSnapshot } from "./fixtures/use-detector-fixtures.mjs";
import { exists, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

const HOUR = 60 * 60 * 1000;

async function writeNotes(vault) {
  const wiki = path.join(vault, "wiki", "concepts");
  await mkdir(wiki, { recursive: true });
  await writeFile(path.join(wiki, "alpha.md"), "# Alpha retention rule\n\nThe alpha rule keeps pelican data.\n", "utf8");
  await writeFile(path.join(wiki, "beta.md"), "# Beta release notes\n\nThe beta train ships alpha fixes.\n", "utf8");
}

function upsPayload(prompt) {
  return { session_id: "df-ups", prompt_id: "p1", prompt };
}

/** The module file names a spawned hook resolves. */
async function loads(t, fixture, payload) {
  const dir = await mkdtemp(path.join(tmpdir(), "minni-df-ups-loads-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const log = path.join(dir, "loads.log");
  const preload = path.join(dir, "log-resolve.mjs");
  await writeFile(
    preload,
    [
      'import { registerHooks } from "node:module";',
      'import { appendFileSync } from "node:fs";',
      "registerHooks({",
      "  resolve(specifier, context, nextResolve) {",
      "    const result = nextResolve(specifier, context);",
      "    appendFileSync(process.env.MINNI_TEST_LOAD_LOG, `${result.url}\\n`);",
      "    return result;",
      "  },",
      "});",
      "",
    ].join("\n"),
    "utf8",
  );
  const { code } = await runHook(fixture, "UserPromptSubmit", payload, {
    env: { MINNI_TEST_LOAD_LOG: log },
    nodeArgs: ["--import", pathToFileURL(preload).href],
  });
  assert.equal(code, 0);
  return (await readFile(log, "utf8")).split("\n").filter(Boolean).map((url) => url.split("/").pop());
}

test("UPS with the ledger on is read-only for the DF snapshot: absent stays absent, stale stays stale", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "df-ups-readonly");
  await startFakeDaemon(fixture);
  await writeNotes(fixture.vault);
  const file = path.join(fixture.vault, DF_SNAPSHOT_RELPATH);
  const partial = path.join(fixture.vault, ".runtime", "df-snapshot.partial.json");

  // No snapshot at all: a scanned prompt writes none, and loads no detector.
  const absent = await loads(t, fixture, upsPayload("where is the alpha retention rule"));
  assert.ok(absent.includes("use-capture.js"), "positive control: the ledger is on");
  assert.ok(!absent.includes("use-detector.js"), "UPS loads no detector to count");
  assert.ok(!(await exists(file)), "UPS wrote no snapshot");
  assert.ok(!(await exists(partial)), "UPS wrote no partial recount");

  // A stale snapshot (well past a day): UPS leaves it byte-identical, same mtime.
  await writeFixtureSnapshot(fixture.vault);
  const stale = new Date(Date.now() - 48 * HOUR);
  await utimes(file, stale, stale);
  const bytes = await readFile(file);
  const { mtimeMs } = await stat(file);
  const names = await loads(t, fixture, upsPayload("and the beta release notes"));
  assert.ok(!names.includes("use-detector.js"));
  assert.deepEqual(await readFile(file), bytes, "a stale snapshot is not rewritten by UPS");
  assert.equal((await stat(file)).mtimeMs, mtimeMs);
  assert.ok(!(await exists(partial)));
});
