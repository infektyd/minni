// Reality-bench seam 2: agy has no note channel, and the Stop receipt rides
// EVERY Stop as a note. A dropped NOTE used to mark the whole output dropped,
// so emitAndCommit discarded the measured Stop's post-delivery ledger work:
// gemini (a MEASURED wire) never ran its use detector or DF recount at all.
// A note carries no memory, so its drop is audited but the noop the host
// received is a real delivery and the record-only work runs.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { auditTail } from "../dist/vault.js";
import { DF_SNAPSHOT_RELPATH } from "../dist/use-detector.js";
import { DF_PARTIAL_RELPATH } from "../dist/use-df-recount.js";
import { exists, makeFixture, runHook, startFakeDaemon } from "./use-telemetry-hook-fixture.mjs";

test("gemini Stop: receipt note is dropped (audited) and the DF recount still runs", { timeout: 120_000 }, async (t) => {
  const fixture = await makeFixture(t, "gemini-stop-ledger");
  await startFakeDaemon(fixture);
  const wiki = path.join(fixture.vault, "wiki", "concepts");
  await mkdir(wiki, { recursive: true });
  await writeFile(path.join(wiki, "alpha.md"), "# Alpha retention rule\n\nThe alpha rule keeps pelican data.\n", "utf8");
  await writeFile(path.join(wiki, "beta.md"), "# Beta release notes\n\nThe beta train ships alpha fixes.\n", "utf8");

  const payload = {
    conversationId: "cfcd2d03-9775-4ff2-8667-ba461998307f",
    artifactDirectoryPath: path.join(fixture.root, "brain"),
    transcriptPath: path.join(fixture.root, "brain", "transcript_full.jsonl"),
    workspacePaths: [],
  };
  const { code } = await runHook(fixture, "Stop", payload, {
    bin: "gemini-hook.js",
    env: { MINNI_GEMINI_VAULT_PATH: fixture.vault },
  });
  assert.equal(code, 0);

  const audit = (await auditTail(fixture.vault, 50)).text;
  assert.match(audit, /intent_dropped/, "the undeliverable note is still recorded");
  const ran =
    (await exists(path.join(fixture.vault, DF_SNAPSHOT_RELPATH))) ||
    (await exists(path.join(fixture.vault, DF_PARTIAL_RELPATH)));
  assert.equal(ran, true, "gemini is a measured wire: its Stop ledger work must run");
});
