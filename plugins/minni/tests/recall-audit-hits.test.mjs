// Learn→recall lineage: the minni_recall audit recorded only local vaultMatches
// paths, so whether a shared-store learning was ever recalled could only be
// answered from raw session transcripts. recallAuditHits pulls the daemon's own
// ids out of the response so the audit log can answer it directly.
import assert from "node:assert/strict";
import test from "node:test";

import { recallAuditHits } from "../dist/daemon_transport.js";

/** The shape recall.py's response_payload puts on the wire. */
const response = {
  count: 3,
  results: [
    { doc_id: 11, chunk_id: 101, source: "wiki/a.md", score: 1.2 },
    { doc_id: 11, chunk_id: 102, source: "wiki/a.md", score: 1.1 },
    { doc_id: 42, chunk_id: 420, source: "wiki/b.md", score: 0.9 },
  ],
  learnings: [
    { learning_id: 7, agent_id: "claudecode", content: "never waitpid after a bounded wait" },
    { learning_id: 9, agent_id: "claudecode", content: "start fresh, no vault import" },
  ],
};

test("recallAuditHits records deduplicated doc, chunk and learning ids", () => {
  assert.deepEqual(recallAuditHits(response), {
    doc_ids: [11, 42],
    chunk_ids: [101, 102, 420],
    learning_ids: [7, 9],
  });
});

test("recallAuditHits records ids only, never content or paths", () => {
  const serialized = JSON.stringify(recallAuditHits(response));
  assert.doesNotMatch(serialized, /waitpid|wiki\/a\.md|claudecode/);
});

test("recallAuditHits tolerates string results, missing arrays and malformed ids", () => {
  const empty = { doc_ids: [], chunk_ids: [], learning_ids: [] };
  assert.deepEqual(recallAuditHits(undefined), empty);
  assert.deepEqual(recallAuditHits({ results: "No results." }), empty);
  assert.deepEqual(
    recallAuditHits({
      results: [null, "x", { doc_id: "11" }, { doc_id: 1.5 }, { doc_id: 3 }],
      learnings: [{ learning_id: null }, { learning_id: 4 }],
    }),
    { doc_ids: [3], chunk_ids: [], learning_ids: [4] },
  );
});
