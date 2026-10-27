// Recall-to-use S1a-2 (§4.3): SessionStart records what it surfaced, deliver
// first. One `boot_recall` row per projected recall result and one
// `boot_recent_learnings` row per daemon learning, each with its visibility
// judged on the EXACT string the hook emitted: Claude Code keeps 10,000 chars
// inline and only a 2,000-char preview of anything longer, so in an over-cap
// boot an item is visible only if it ends by char 2,000.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

import { stableStringify } from "../dist/agent_envelope.js";
import {
  additionalContext,
  daemonResult,
  makeFixture,
  readLedger,
  runHook,
  sha1Prefix,
  startFakeDaemon,
} from "./use-telemetry-hook-fixture.mjs";

const LINES = [
  "  - [fix] Correction: service X moved to port 9090 (conf=1.0)",
  "  - [decision] Builds go through make, never a bare tsc (conf=0.9)",
];
const READ_CONTEXT = ["## Learnings (claude-code)", ...LINES, "", "## Recent Activity (claude-code)", "  - [note] x"].join(
  "\n",
);
const STRUCTURED = [
  { learning_id: 501, category: "fix", line: LINES[0], content: "Correction: service X moved to port 9090" },
  { learning_id: 502, category: "decision", line: LINES[1], content: "Builds go through make, never a bare tsc" },
];

/**
 * Six results: a tiny first one (early in the envelope), one whose source is a
 * note in this vault, then four more; with `long`, their text pushes the boot
 * past 10k.
 */
async function bootResults(vault, { long = true } = {}) {
  const inVault = path.join(vault, "wiki", "decisions", "port-move.md");
  await mkdir(path.dirname(inVault), { recursive: true });
  await writeFile(inVault, "# Port move\n\nService X moved.\n", "utf8");
  const results = [
    daemonResult({ source: "/e/a.md", docId: 7, wikilink: "[[a]]", text: "", title: "A" }),
    daemonResult({
      source: inVault,
      docId: 8,
      wikilink: "[[wiki/decisions/port-move]]",
      text: long ? "moved ".repeat(250) : "moved",
      title: "Port move",
    }),
  ];
  for (let k = 2; k < 6; k += 1) {
    results.push(
      daemonResult({
        source: `/elsewhere/corpus/note-${k}.md`,
        docId: 100 + k,
        src: "shared",
        wikilink: `[[wiki/note-${k}]]`,
        text: long ? `filler ${k} `.repeat(250) : `brief ${k}`,
      }),
    );
  }
  return results;
}

function rowsOf(rows, channel) {
  return rows.filter((row) => row.kind === "surfaced" && row.channel === channel);
}

test("an over-cap boot: items ending past char 2,000 are invisible, earlier ones visible", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "boot-visibility");
  const results = await bootResults(fixture.vault);
  await startFakeDaemon(fixture, {
    search: () => ({ agent_id: "claude-code", results }),
    read: () => ({ agent_id: "claude-code", context: READ_CONTEXT, recent_learnings: STRUCTURED, daemon_version: "test" }),
  });
  const { output, code } = await runHook(
    fixture,
    "SessionStart",
    { session_id: "boot-over-cap", source: "startup" },
    { env: { MINNI_BOOT_FIT: "off" } },
  );
  assert.equal(code, 0);
  const emitted = additionalContext(output);
  assert.ok(emitted.length > 10_000, `the fixture must overflow the cap (got ${emitted.length})`);

  const rows = await readLedger(fixture.vault);
  const recall = rowsOf(rows, "boot_recall");
  assert.equal(recall.length, results.length, "one row per projected recall result");
  let seenVisible = 0;
  let seenHidden = 0;
  for (const [index, result] of results.entries()) {
    const row = recall[index];
    const start = emitted.indexOf(stableStringify(result));
    assert.ok(start >= 0, "every projected result is in the emitted string");
    const end = start + stableStringify(result).length;
    assert.equal(row.visible, end <= 2000, `result ${index} ends at ${end}`);
    assert.equal(row.delivered, true);
    assert.equal(row.rank, index + 1);
    if (row.visible) seenVisible += 1;
    else seenHidden += 1;
  }
  assert.ok(seenVisible >= 1 && seenHidden >= 1, `the fixture straddles char 2,000 (${seenVisible}/${seenHidden})`);
  assert.equal(recall[0].mkey, "doc:personal:7", "a result from elsewhere keys as doc:<src>:<doc_id>");
  assert.deepEqual(recall[0].handles, ["d:personal:7"]);
  assert.equal(recall[1].mkey, "vault:wiki/decisions/port-move.md", "a source inside this vault keys as vault:");
  assert.equal(recall[2].mkey, "doc:shared:102");

  const learnings = rowsOf(rows, "boot_recent_learnings");
  assert.deepEqual(
    learnings.map((row) => row.mkey),
    ["learning:501", "learning:502"],
    "structured ids key as learning:<id>",
  );
  for (const row of learnings) {
    assert.equal(row.visible, false, "the learnings sit past char 2,000 of an over-cap boot");
    assert.equal(row.title, "", "boot learnings have no title");
    assert.ok(Array.isArray(row.shingles) && row.shingles.every((h) => /^[0-9a-f]{16}$/.test(h)));
  }
  const text = JSON.stringify(rows);
  assert.ok(!text.includes("port 9090") && !text.includes("bare tsc"), "no learning text in the ledger");
});

test("an under-cap boot: every item is visible", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "boot-under-cap");
  const results = await bootResults(fixture.vault, { long: false });
  await startFakeDaemon(fixture, {
    search: () => ({ agent_id: "claude-code", results }),
    read: () => ({ agent_id: "claude-code", context: READ_CONTEXT, recent_learnings: STRUCTURED }),
  });
  const { output } = await runHook(
    fixture,
    "SessionStart",
    { session_id: "boot-under-cap", source: "startup" },
    { env: { MINNI_BOOT_FIT: "off" } },
  );
  assert.ok(additionalContext(output).length <= 10_000);
  const rows = await readLedger(fixture.vault);
  const surfaced = rows.filter((row) => row.kind === "surfaced");
  assert.equal(surfaced.length, results.length + STRUCTURED.length);
  for (const row of surfaced) assert.equal(row.visible, true, `${row.channel} ${row.mkey}`);
});

test("a fitted boot (MINNI_BOOT_FIT=on default): projected boot_recall rows keep mkeys and are visible", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "boot-fitted");
  const results = await bootResults(fixture.vault, { long: true });
  await startFakeDaemon(fixture, {
    search: () => ({ agent_id: "claude-code", results }),
    read: () => ({ agent_id: "claude-code", context: READ_CONTEXT, recent_learnings: STRUCTURED }),
  });
  const { output, code } = await runHook(fixture, "SessionStart", { session_id: "boot-fitted", source: "startup" });
  assert.equal(code, 0);
  assert.ok(additionalContext(output).length <= 10_000);
  const rows = await readLedger(fixture.vault);
  const recall = rowsOf(rows, "boot_recall");
  assert.equal(recall.length, 3, "S2a caps non-identity boot recall at 3 projected results");
  assert.equal(recall[0].mkey, "doc:personal:7");
  assert.deepEqual(recall[0].handles, ["d:personal:7"]);
  assert.equal(recall[1].mkey, "vault:wiki/decisions/port-move.md");
  assert.equal(recall[2].mkey, "doc:shared:102");
  for (const row of recall) assert.equal(row.visible, true, `${row.channel} ${row.mkey}`);
});

test("a daemon without structured ids: boot learnings key as lhash: from the Learnings lines", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "boot-lhash");
  await startFakeDaemon(fixture, {
    read: () => ({ agent_id: "claude-code", context: READ_CONTEXT }),
  });
  await runHook(fixture, "SessionStart", { session_id: "boot-lhash", source: "startup" });
  const learnings = rowsOf(await readLedger(fixture.vault), "boot_recent_learnings");
  assert.deepEqual(
    learnings.map((row) => row.mkey),
    LINES.map((line) => `lhash:${sha1Prefix(line.trim())}`),
  );
  for (const row of learnings) assert.equal(row.visible, true);
});

test("B5: boot_recall surfaced rows carry non-empty shingles and echo hashes from vault note or daemon result text", { timeout: 60_000 }, async (t) => {
  const fixture = await makeFixture(t, "boot-b5-shingles-echo");
  const inVault = path.join(fixture.vault, "wiki", "decisions", "boot-shingle-note.md");
  await mkdir(path.dirname(inVault), { recursive: true });
  await writeFile(
    inVault,
    "# Boot Shingle Note\n\nSessionStart boot recall rows must carry shingleHashes and echoHashes so R2 and R5 attribution can match.\n",
    "utf8",
  );
  const results = [
    daemonResult({
      source: inVault,
      docId: 901,
      wikilink: "[[wiki/decisions/boot-shingle-note]]",
      text: "SessionStart boot recall rows must carry shingleHashes and echoHashes.",
      title: "Boot Shingle Note",
    }),
    daemonResult({
      source: "/elsewhere/corpus/daemon-only.md",
      docId: 902,
      src: "shared",
      wikilink: "[[wiki/daemon-only]]",
      text: "Daemon-backed boot recall item with multi-word prose for shingle and echo extraction in SessionStart.",
      title: "Daemon Only Boot Note",
    }),
  ];
  await startFakeDaemon(fixture, {
    search: () => ({ agent_id: "claude-code", results }),
    read: () => ({ agent_id: "claude-code", context: READ_CONTEXT, recent_learnings: STRUCTURED }),
  });
  await runHook(fixture, "SessionStart", { session_id: "boot-b5", source: "startup" });
  const rows = await readLedger(fixture.vault);
  const recall = rowsOf(rows, "boot_recall");
  assert.equal(recall.length, 2);
  for (const row of recall) {
    assert.ok(
      Array.isArray(row.shingles) && row.shingles.length > 0 && row.shingles.every((h) => /^[0-9a-f]{16}$/.test(h)),
      `expected non-empty 16-hex shingles on boot_recall row ${row.mkey}, got ${JSON.stringify(row.shingles)}`,
    );
    assert.ok(
      Array.isArray(row.echo) && row.echo.length > 0 && row.echo.every((h) => /^[0-9a-f]{16}$/.test(h)),
      `expected non-empty 16-hex echo hashes on boot_recall row ${row.mkey}, got ${JSON.stringify(row.echo)}`,
    );
  }
});

