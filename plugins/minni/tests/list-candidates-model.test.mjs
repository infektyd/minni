import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  drainStatusForModel,
  modelListCandidatesPayload,
  MODEL_HIDDEN_CANDIDATE_STATUSES,
} from "../dist/list-candidates-model.js";

const SECRET = "hunter2-not-a-path";
const LOCAL_PATH = "/Users/example/Projects/secret-notes.md";

test("drainStatusForModel defaults omitted status to proposed", () => {
  assert.equal(drainStatusForModel(undefined), "proposed");
  assert.equal(drainStatusForModel(""), "proposed");
  assert.equal(drainStatusForModel("  "), "proposed");
  assert.equal(drainStatusForModel("log_only"), "log_only");
});

test("model list redacts secrets/paths and drops full packet fields", () => {
  const payload = modelListCandidatesPayload(
    {
      ok: true,
      data: {
        principal: "cursor",
        status: "proposed",
        count: 1,
        total: 1,
        has_more: false,
        limit: 100,
        candidates: [
          {
            candidate_id: 7,
            status: "proposed",
            content: `Remember api_key=${SECRET} lives at ${LOCAL_PATH}`,
            evidence_refs: [{ path: LOCAL_PATH }],
            derived_from: { inbox_file: LOCAL_PATH },
            instruction_like: 0,
            proposed_at: 1,
            layer: "knowledge",
            privacy_level: "safe",
            workspace_id: "default",
            resolved_by: "nobody",
          },
        ],
      },
    },
    "proposed",
  );
  const blob = JSON.stringify(payload);
  assert.equal(blob.includes(SECRET), false, blob);
  assert.equal(blob.includes("/Users/example"), false, blob);
  assert.equal(Array.isArray(payload.candidates), true);
  assert.equal(payload.candidates.length, 1);
  const row = payload.candidates[0];
  assert.equal("evidence_refs" in row, false);
  assert.equal("derived_from" in row, false);
  assert.equal("resolved_by" in row, false);
  assert.match(String(row.content), /\[REDACTED\]|\[local-path\]/);
});

test("model list refuses redacted/rejected content even when the daemon returns it", () => {
  assert.equal(MODEL_HIDDEN_CANDIDATE_STATUSES.has("redacted"), true);
  assert.equal(MODEL_HIDDEN_CANDIDATE_STATUSES.has("rejected"), true);
  for (const status of ["redacted", "rejected"]) {
    const payload = modelListCandidatesPayload(
      {
        ok: true,
        data: {
          candidates: [
            {
              candidate_id: 9,
              status,
              content: "HIDDEN_REDACT_MARKER_xyz api_key=hunter2-not-a-path",
              derived_from: { inbox_file: "/Users/example/hidden.md" },
            },
          ],
        },
      },
      status,
    );
    const blob = JSON.stringify(payload);
    assert.equal(payload.hidden, true, status);
    assert.deepEqual(payload.candidates, []);
    assert.equal(blob.includes("HIDDEN_REDACT_MARKER_xyz"), false, blob);
    assert.equal(blob.includes("hunter2-not-a-path"), false, blob);
  }
});

test("minni_list_candidates handler projects through modelListCandidatesPayload", async () => {
  const source = await readFile(new URL("../src/server.ts", import.meta.url), "utf8");
  const start = source.indexOf('"minni_list_candidates"');
  assert.notEqual(start, -1);
  const nextTool = source.indexOf("server.registerTool(", start + 1);
  const block = source.slice(start, nextTool === -1 ? undefined : nextTool);
  assert.match(block, /modelListCandidatesPayload/);
  assert.match(block, /drainStatusForModel/);
  assert.doesNotMatch(block, /JSON\.stringify\(rpc,/);
});
