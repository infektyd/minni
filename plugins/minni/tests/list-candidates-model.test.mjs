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
  const schemaStart = block.indexOf("inputSchema:");
  const handlerStart = block.indexOf("async");
  const schema = block.slice(schemaStart, handlerStart);
  assert.match(schema, /status:\s*z\.enum\(\["proposed"\]\)\.optional\(\)/);
  assert.doesNotMatch(schema, /status:\s*z\.string\(\)/);
});

test("failed candidate list does not report an empty complete drain", () => {
  const payload = modelListCandidatesPayload(
    { ok: false, error: "socket refused /Users/example/minnid.sock" },
    "proposed",
  );
  assert.equal(payload.ok, false);
  assert.equal(typeof payload.error, "string");
  assert.equal(String(payload.error).includes("/Users/example"), false);
  assert.equal("candidates" in payload, false, JSON.stringify(payload));
  assert.equal("count" in payload, false, JSON.stringify(payload));
  assert.equal("total" in payload, false, JSON.stringify(payload));
  assert.equal("has_more" in payload, false, JSON.stringify(payload));
  assert.notEqual(payload.total, 0);
  assert.notEqual(payload.has_more, false);
  assert.notDeepEqual(payload.candidates, []);
});

test("model list never returns do_not_store/log_only/accepted/merged/superseded content", () => {
  const leakStatuses = ["do_not_store", "log_only", "accepted", "merged", "superseded", "expired"];
  for (const status of leakStatuses) {
    const marker = `LEAK_${status}_MARKER`;
    const payload = modelListCandidatesPayload(
      {
        ok: true,
        data: {
          status,
          candidates: [
            {
              candidate_id: 11,
              status,
              content: `${marker} remember this packet forever`,
            },
          ],
        },
      },
      status,
    );
    const blob = JSON.stringify(payload);
    assert.equal(blob.includes(marker), false, blob);
    assert.deepEqual(payload.candidates, []);
  }

  const mixed = modelListCandidatesPayload(
    {
      ok: true,
      data: {
        status: "proposed",
        candidates: [
          { candidate_id: 1, status: "proposed", content: "ok-to-see" },
          { candidate_id: 2, status: "do_not_store", content: "DNS_MARKER" },
          { candidate_id: 3, status: "log_only", content: "LOG_ONLY_MARKER" },
          { candidate_id: 4, status: "accepted", content: "ACCEPTED_MARKER" },
        ],
      },
    },
    "proposed",
  );
  const mixedBlob = JSON.stringify(mixed);
  assert.equal(mixedBlob.includes("DNS_MARKER"), false, mixedBlob);
  assert.equal(mixedBlob.includes("LOG_ONLY_MARKER"), false, mixedBlob);
  assert.equal(mixedBlob.includes("ACCEPTED_MARKER"), false, mixedBlob);
  assert.equal(mixed.candidates.length, 1);
  assert.equal(mixed.candidates[0].candidate_id, 1);
});
