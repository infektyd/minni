import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  COMPACT_HARVEST_STATE_RELPATH,
  COMPACT_SUMMARY_KIND,
  extractLatestCompactSummary,
  harvestCompactSummary,
  harvestSummaryText,
  stripContinuationFrame,
} from "../dist/compact-harvest.js";
import { ensureVault } from "../dist/vault.js";

// Hermetic guard: recordAudit writes per-agent rate-limit state under
// MINNI_HOME (falling back to ~/.minni) — point it at a temp dir so the
// suite never touches the real home.
process.env.MINNI_HOME = await mkdtemp(path.join(tmpdir(), "sm-test-home-"));

const SUMMARY_TEXT = [
  "This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.",
  "",
  "Summary:",
  "1. Primary Request and Intent:",
  "   User asked to fix the daemon fd leak and deploy the fix to all platforms.",
  "2. Key Technical Concepts:",
  "   SovereignDB caches one sqlite connection per thread; the RPC pool multiplies open fds past the launchd soft limit.",
  "3. Errors and fixes:",
  "   launchctl bootstrap error 5 right after bootout is a teardown race; sleeping two seconds and retrying succeeds.",
  "",
  "If you need specific details from before compaction, read the transcript at /tmp/whatever.jsonl",
].join("\n");

function transcriptLine(entry) {
  return `${JSON.stringify(entry)}\n`;
}

function summaryEntry(uuid, text, extra = {}) {
  return {
    type: "user",
    isCompactSummary: true,
    uuid,
    timestamp: "2026-07-30T12:00:00.000Z",
    message: { role: "user", content: text },
    ...extra,
  };
}

async function tmpTranscript(lines) {
  const dir = await mkdtemp(path.join(tmpdir(), "sm-compact-"));
  const file = path.join(dir, "session.jsonl");
  await writeFile(file, lines.join(""), "utf8");
  return file;
}

async function tmpVault() {
  const dir = await mkdtemp(path.join(tmpdir(), "sm-compact-vault-"));
  const vault = path.join(dir, "claudecode-vault");
  await ensureVault(vault);
  return vault;
}

const HARVEST_CONFIG = (vaultPath) => ({
  vaultPath,
  workspaceId: "workspace-test",
  auditPrefix: "hook",
  platform: "claude-code",
});

test("extractLatestCompactSummary finds the newest summary entry", async () => {
  const file = await tmpTranscript([
    transcriptLine({ type: "user", message: { role: "user", content: "hello" } }),
    transcriptLine(summaryEntry("uuid-old", "old summary text here")),
    transcriptLine({ type: "assistant", message: { role: "assistant", content: "ok" } }),
    transcriptLine(summaryEntry("uuid-new", SUMMARY_TEXT)),
    transcriptLine({ type: "user", message: { role: "user", content: "continue" } }),
  ]);
  const hit = await extractLatestCompactSummary(file);
  assert.ok(hit);
  assert.equal(hit.uuid, "uuid-new");
  assert.ok(hit.text.includes("Primary Request and Intent"));
  assert.equal(hit.timestamp, "2026-07-30T12:00:00.000Z");
});

test("extractLatestCompactSummary handles content-block arrays", async () => {
  const file = await tmpTranscript([
    transcriptLine(
      summaryEntry("uuid-blocks", undefined, {
        message: {
          role: "user",
          content: [
            { type: "text", text: "Summary part one." },
            { type: "tool_result", text: "ignored" },
            { type: "text", text: "Summary part two." },
          ],
        },
      }),
    ),
  ]);
  const hit = await extractLatestCompactSummary(file);
  assert.ok(hit);
  assert.equal(hit.text, "Summary part one.\nSummary part two.");
});

test("extractLatestCompactSummary tolerates malformed lines and missing files", async () => {
  const file = await tmpTranscript([
    "{not json at all\n",
    transcriptLine(summaryEntry("uuid-x", "valid summary body")),
    "another broken line\n",
  ]);
  const hit = await extractLatestCompactSummary(file);
  assert.equal(hit?.uuid, "uuid-x");
  assert.equal(await extractLatestCompactSummary("/nonexistent/nope.jsonl"), undefined);
});

test("extractLatestCompactSummary only scans the bounded tail", async () => {
  const filler = transcriptLine({ type: "assistant", message: { role: "assistant", content: "x".repeat(512) } });
  const file = await tmpTranscript([
    transcriptLine(summaryEntry("uuid-buried", "buried far before the tail window")),
    ...Array.from({ length: 50 }, () => filler),
  ]);
  // A tail window smaller than the filler must not find the buried summary…
  assert.equal(await extractLatestCompactSummary(file, 4096), undefined);
  // …while the default window does.
  const hit = await extractLatestCompactSummary(file);
  assert.equal(hit?.uuid, "uuid-buried");
});

test("stripContinuationFrame removes boilerplate and trailers", () => {
  const body = stripContinuationFrame(SUMMARY_TEXT);
  assert.ok(body.startsWith("1. Primary Request and Intent:"));
  assert.ok(!body.includes("being continued from a previous conversation"));
  assert.ok(!body.includes("If you need specific details"));
  // Unrecognized frames pass through untouched.
  assert.equal(stripContinuationFrame("plain text"), "plain text");
});

test("harvestCompactSummary writes one raw compact_summary inbox file with stamps", async () => {
  const vault = await tmpVault();
  const file = await tmpTranscript([transcriptLine(summaryEntry("uuid-h1", SUMMARY_TEXT))]);
  const result = await harvestCompactSummary(HARVEST_CONFIG(vault), {
    transcriptPath: file,
    sessionId: "sess-1",
  });
  assert.equal(result.harvested, true);
  assert.equal(result.summaryId, "uuid-h1");
  const doc = JSON.parse(await readFile(result.inboxPath, "utf8"));
  assert.equal(doc.kind, COMPACT_SUMMARY_KIND);
  assert.equal(doc.agent_id, "claude-code");
  assert.equal(doc.workspace_id, "workspace-test");
  assert.equal(doc.platform, "claude-code");
  assert.equal(doc.summary_id, "uuid-h1");
  assert.equal(doc.summary_timestamp, "2026-07-30T12:00:00.000Z");
  // The RAW summary is stored, frame-stripped — no drafting on the hook side.
  assert.ok(doc.summary_text.startsWith("1. Primary Request and Intent:"));
  assert.ok(doc.summary_text.includes("teardown race"));
  assert.ok(!doc.summary_text.includes("being continued from a previous conversation"));
  assert.equal(doc.candidates, undefined);
});

test("harvestSummaryText is the shared trunk (Kilo path) and caps oversized text", async () => {
  const vault = await tmpVault();
  const big = `Summary:\n${"All the findings in this section matter a lot. ".repeat(3000)}`;
  const result = await harvestSummaryText(
    { ...HARVEST_CONFIG(vault), platform: "kilocode" },
    { summaryText: big, summaryId: "msg_abc123", sessionId: "ses_1" },
  );
  assert.equal(result.harvested, true);
  assert.ok(result.summaryChars <= 65536);
  const doc = JSON.parse(await readFile(result.inboxPath, "utf8"));
  assert.equal(doc.platform, "kilocode");
  assert.equal(doc.summary_id, "msg_abc123");
  assert.ok(doc.summary_text.length <= 65536);
});

test("harvest dedups by summary id across boots", async () => {
  const vault = await tmpVault();
  const file = await tmpTranscript([transcriptLine(summaryEntry("uuid-d1", SUMMARY_TEXT))]);
  const first = await harvestCompactSummary(HARVEST_CONFIG(vault), {
    transcriptPath: file,
    sessionId: "sess-1",
  });
  assert.equal(first.harvested, true);
  const second = await harvestCompactSummary(HARVEST_CONFIG(vault), {
    transcriptPath: file,
    sessionId: "sess-1",
  });
  assert.equal(second.harvested, false);
  assert.equal(second.reason, "already_harvested");
  const inbox = (await readdir(path.join(vault, "inbox"))).filter((name) => name.endsWith(".json"));
  assert.equal(inbox.length, 1);
  const state = JSON.parse(await readFile(path.join(vault, COMPACT_HARVEST_STATE_RELPATH), "utf8"));
  assert.ok(state.harvested["uuid-d1"]);
});

test("empty/aborted summaries write no inbox file but mark state", async () => {
  const vault = await tmpVault();
  const result = await harvestSummaryText(HARVEST_CONFIG(vault), {
    summaryText: "tiny",
    summaryId: "uuid-z1",
    sessionId: "sess-z",
  });
  assert.equal(result.harvested, false);
  assert.equal(result.reason, "empty_summary");
  const inbox = (await readdir(path.join(vault, "inbox"))).filter((name) => name.endsWith(".json"));
  assert.equal(inbox.length, 0);
  const state = JSON.parse(await readFile(path.join(vault, COMPACT_HARVEST_STATE_RELPATH), "utf8"));
  assert.ok(state.harvested["uuid-z1"], "empty summary id must still be marked to stop rescans");
});

test("harvest is fail-open on missing inputs", async () => {
  const vault = await tmpVault();
  assert.deepEqual(await harvestCompactSummary(HARVEST_CONFIG(vault), { sessionId: "s" }), {
    harvested: false,
    reason: "no_transcript_path",
  });
  const empty = await tmpTranscript([transcriptLine({ type: "user", message: { role: "user", content: "hi" } })]);
  assert.equal(
    (await harvestCompactSummary(HARVEST_CONFIG(vault), { transcriptPath: empty, sessionId: "s" })).reason,
    "no_summary_found",
  );
  assert.equal(
    (await harvestSummaryText(HARVEST_CONFIG(vault), { summaryText: "x", summaryId: "", sessionId: "s" })).reason,
    "no_summary_found",
  );
});
