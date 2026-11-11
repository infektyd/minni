import assert from "node:assert/strict";
import net from "node:net";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { drillMemory } from "../dist/daemon_transport.js";
import {
  extractJevLearning,
  maskTelemetry,
  screenTranscriptChunk,
  stripHarvestWrappers,
} from "../dist/jev-harvest.js";
import {
  assessLearningQuality,
  assessLearningQualityAsync,
  stripQualityWrappers,
} from "../dist/policy.js";

test("BUG-11: stripHarvestWrappers & maskTelemetry strip runtime wrappers and envelopes", () => {
  const wrapped = [
    "Created At: 2026-11-11T03:08:44-05:00",
    "Completed At: 2026-11-11T03:08:46-05:00",
    "The following is a <SYSTEM_MESSAGE> injected by runtime.",
    "<SYSTEM_MESSAGE>",
    "[Message] timestamp=2026-11-11T08:09:18Z sender=worker-1 content=done",
    "</SYSTEM_MESSAGE>",
    "<MINNI_ENVELOPE>prior recalled memory</MINNI_ENVELOPE>",
    "<background-task id=\"t1\">running</background-task>",
    "<task-notification id=\"n1\">finished</task-notification>",
    "Always pass explicit file paths to git add instead of staging whole directories.",
  ].join("\n");

  const stripped = stripHarvestWrappers(wrapped);
  assert.equal(
    stripped,
    "Always pass explicit file paths to git add instead of staging whole directories.",
  );
  assert.ok(!maskTelemetry(wrapped).includes("Created At:"));
  assert.ok(!maskTelemetry(wrapped).includes("<SYSTEM_MESSAGE>"));
  assert.ok(!maskTelemetry(wrapped).includes("<MINNI_ENVELOPE>"));
});

test("BUG-11: screenTranscriptChunk & extractJevLearning drop wrapper-only and 1-word chatter", async () => {
  const wrapperOnlyEntries = [
    {
      source: "USER_EXPLICIT",
      type: "USER_INPUT",
      content:
        "Created At: 2026-11-11T03:08:44-05:00\nCompleted At: 2026-11-11T03:08:46-05:00\n<SYSTEM_MESSAGE>always check status</SYSTEM_MESSAGE>\nfix",
    },
    {
      source: "TOOL",
      type: "TOOL_RESULT",
      tool_name: "run_command",
      content:
        "Created At: 2026-11-11T03:09:00-05:00\nCompleted At: 2026-11-11T03:09:02-05:00\npytest 1 failed",
    },
    {
      source: "TOOL",
      type: "TOOL_RESULT",
      tool_name: "edit_file",
      content: "Created At: 2026-11-11T03:09:05-05:00\nCompleted At: 2026-11-11T03:09:06-05:00\nfix",
    },
    {
      source: "TOOL",
      type: "TOOL_RESULT",
      tool_name: "run_command",
      content:
        "Created At: 2026-11-11T03:09:10-05:00\nCompleted At: 2026-11-11T03:09:12-05:00\n12 passed in 0.4s",
    },
  ];

  const triggers = screenTranscriptChunk(wrapperOnlyEntries, { promptClasses: false });
  // Operator directive reduced to "fix" must be dropped; any test_insight trigger must not contain Created At:
  assert.ok(!triggers.some((t) => t.kind === "operator_directive"));
  for (const trig of triggers) {
    assert.ok(!trig.triggerText.includes("Created At:"), trig.triggerText);
    for (const part of trig.contextWindow) {
      assert.ok(!part.includes("Created At:"), part);
    }
  }

  // Custom extractor returning bare chatter or wrapper header is dropped before learn pipeline
  const dropped = await extractJevLearning(
    {
      kind: "scar",
      triggerText: "Tool error resolved by recovery command: sqlite busy",
      contextWindow: ["sqlite busy error when opening wal", "set busy_timeout=5000 before writes"],
    },
    async () => ({
      title: "Quick fix",
      content: "Created At: 2026-11-11T03:09:10-05:00\nfix",
      category: "fix",
      source: "jev_harvest",
    }),
  );
  assert.equal(dropped, undefined);
});

test("BUG-11: assessLearningQuality enforces hard stripped-content floor before metadata bonuses", () => {
  const richMetadata = {
    title: "Comprehensive searchable architecture title",
    category: "architecture",
    source: "session-2026-11-11",
  };

  for (const junk of [
    "fix",
    "done",
    "lgtm",
    "two words",
    "Created At: 2026-11-11T03:08:44-05:00\nCompleted At: 2026-11-11T03:08:46-05:00",
    "<SYSTEM_MESSAGE>Always run full gates before promotion</SYSTEM_MESSAGE>\ndone",
    "<MINNI_ENVELOPE>Recalled evidence block</MINNI_ENVELOPE>",
  ]) {
    const report = assessLearningQuality({ ...richMetadata, content: junk });
    assert.equal(report.ok, false, `expected ok=false for ${junk}`);
    assert.equal(report.passed, false, `expected passed=false for ${junk}`);
    assert.equal(report.reason, "content_too_short", `expected content_too_short for ${junk}`);
  }

  assert.equal(
    stripQualityWrappers("Created At: 2026-11-11T03:08:44-05:00\nNever use git add -A in shared worktrees."),
    "Never use git add -A in shared worktrees.",
  );

  const valid = assessLearningQuality({
    ...richMetadata,
    content: "Never use git add -A when staging changes in multi-agent repositories.",
  });
  assert.equal(valid.ok, true, valid.summary);
  assert.equal(valid.passed, true);
});

test("BUG-33: assessLearningQualityAsync credential verdict sets passed=false alongside ok=false", async () => {
  const report = await assessLearningQualityAsync(
    {
      title: "Staging login setup note",
      category: "procedure",
      source: "session-2026-11-11",
      content:
        "When verifying local test staging, password: correct horse battery staple is configured on the mock service.",
    },
    {
      classifyInconclusive: async () => "credential",
    },
  );
  assert.equal(report.ok, false);
  assert.equal(report.passed, false);
  assert.equal(report.semanticTier, "ran");
});

test("BUG-05: drillMemory preserves typed memory_links from daemon sm_drill RPC", async () => {
  const tmp = await mkdtemp(path.join(tmpdir(), "w3-drill-"));
  const sockPath = path.join(tmp, "m.sock");
  const server = net.createServer((conn) => {
    conn.on("data", (buf) => {
      const req = JSON.parse(buf.toString("utf8").trim());
      assert.equal(req.method, "sm_drill");
      conn.end(
        JSON.stringify({
          jsonrpc: "2.0",
          id: req.id,
          result: {
            depth: "document",
            count: 1,
            missing: [],
            results: [
              {
                doc_id: 42,
                source: "vault/_durable/codex/metrics-port.md",
                wikilink: "[[vault/_durable/codex/metrics-port]]",
                text: "<EVIDENCE>Always bind metrics exporter to port 9102.</EVIDENCE>",
                depth: "document",
                memory_links: {
                  updates: [
                    {
                      doc_id: 41,
                      learning_id: 7,
                      wikilink: "[[vault/_durable/codex/old-port]]",
                      confidence: 0.97,
                    },
                  ],
                },
              },
            ],
          },
        }) + "\n",
      );
    });
  });

  await new Promise((resolve) => server.listen(sockPath, resolve));
  try {
    const resById = await drillMemory(
      { daemonSocketPath: sockPath, vaultPath: tmp },
      { resultIds: [42], depth: "document" },
    );
    assert.equal(resById.ok, true);
    assert.equal(resById.data?.results?.[0]?.memory_links?.updates?.[0]?.doc_id, 41);

    const resByHandle = await drillMemory(
      { daemonSocketPath: sockPath, vaultPath: tmp },
      { handle: "d:c:42", depth: "document" },
    );
    assert.equal(resByHandle.ok, true);
    assert.equal(resByHandle.data?.memory_links?.updates?.[0]?.doc_id, 41);
    assert.equal(resByHandle.data?.results?.[0]?.memory_links?.updates?.[0]?.doc_id, 41);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(tmp, { recursive: true, force: true });
  }
});
