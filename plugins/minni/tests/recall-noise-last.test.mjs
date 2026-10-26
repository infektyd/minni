import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  partitionRecallResultsNoiseLast,
  recallMemory,
} from "../dist/daemon_transport.js";

test("partitionRecallResultsNoiseLast places durable and ordinary items before noise items while preserving relative order", () => {
  const input = [
    {
      id: 1,
      title: "Plan Journal 01",
      rel_path: "wiki/artifacts/plan-20261025.log.md",
      score: 95,
    },
    {
      id: 2,
      title: "Durable Architecture Decision",
      category: "decision",
      content: "Decision: always validate socket paths before connecting.",
      score: 80,
    },
    {
      id: 3,
      title: "Compaction Summary",
      source: "compact_distillation:session-1",
      content: "Compacted session notes.",
      score: 78,
    },
    {
      id: 4,
      title: "Ordinary Concept Note",
      category: "concept",
      content: "General overview of transport layer.",
      score: 70,
    },
    {
      id: 5,
      title: "Wrapped Relay Echo",
      content:
        'Another Claude session sent a message:\n<cross-session-message from="lead">\nHello\n</cross-session-message>',
      score: 65,
    },
  ];

  const partitioned = partitionRecallResultsNoiseLast(input);
  assert.deepEqual(
    partitioned.map((item) => item.id),
    [2, 4, 1, 3, 5],
    "non-noise items [2, 4] must precede noise items [1, 3, 5] with relative order preserved",
  );
});

test("recallMemory applies MINNI_RECALL_NOISE_LAST=on by default and restores raw order when MINNI_RECALL_NOISE_LAST=off", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "minni-s3c-noise-last-"));
  const vault = path.join(root, "vault");
  await mkdir(path.join(vault, "wiki", "artifacts"), { recursive: true });
  await mkdir(path.join(vault, "wiki", "decisions"), { recursive: true });

  const savedEnv = process.env.MINNI_RECALL_NOISE_LAST;
  try {
    const fakeDaemon = async () => ({
      ok: true,
      data: {
        results: [
          {
            id: 10,
            title: "[Subagent Context] Worker slice",
            content: "Subagent context echo",
            score: 0.95,
          },
          {
            id: 20,
            title: "Socket Backoff Decision",
            category: "decision",
            content: "Decision: use bounded retry.",
            score: 0.82,
          },
        ],
      },
    });

    delete process.env.MINNI_RECALL_NOISE_LAST;
    const onRes = await recallMemory(
      { query: "socket backoff", vaultPath: vault },
      fakeDaemon,
    );
    assert.equal(onRes.ok, true);
    assert.deepEqual(
      onRes.data.results.map((r) => r.id),
      [20, 10],
      "default MINNI_RECALL_NOISE_LAST=on places durable hit 20 before noise hit 10",
    );

    process.env.MINNI_RECALL_NOISE_LAST = "off";
    const offRes = await recallMemory(
      { query: "socket backoff", vaultPath: vault },
      fakeDaemon,
    );
    assert.equal(offRes.ok, true);
    assert.deepEqual(
      offRes.data.results.map((r) => r.id),
      [10, 20],
      "MINNI_RECALL_NOISE_LAST=off preserves original daemon order [10, 20]",
    );
  } finally {
    if (savedEnv === undefined) delete process.env.MINNI_RECALL_NOISE_LAST;
    else process.env.MINNI_RECALL_NOISE_LAST = savedEnv;
    await rm(root, { recursive: true, force: true });
  }
});
