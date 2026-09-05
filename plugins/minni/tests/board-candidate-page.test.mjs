import assert from "node:assert/strict";
import test from "node:test";
import { candidatePageInfo, stagedCountLabel, unwrapCandidatesResponse } from "./.compiled/board-test.mjs";

test("candidate page keeps owner and daemon truncation instead of presenting a fleet total", () => {
  const response = unwrapCandidatesResponse({ ok: true, data: {
    candidates: Array.from({ length: 200 }, () => ({})), principal: "codex",
    count: 200, total: 201, has_more: true, limit: 200,
  } });
  assert.deepEqual(candidatePageInfo(response, 200), { principal: "codex", hasMore: true });
  assert.equal(stagedCountLabel(response.candidates.length, true), "200+");
});

test("an explicit complete full page is exact; a legacy full page is only a lower bound", () => {
  const candidates = Array.from({ length: 200 }, () => ({}));
  assert.equal(candidatePageInfo({ candidates, has_more: false }, 200).hasMore, false);
  assert.equal(candidatePageInfo({ candidates }, 200).hasMore, true);
  assert.equal(stagedCountLabel(200, false), "200");
});

test("empty and unknown scopes stay distinct from a known empty owner page", () => {
  assert.deepEqual(candidatePageInfo({ candidates: [], principal: "  " }, 200), { principal: null, hasMore: false });
  assert.deepEqual(candidatePageInfo({ candidates: [], principal: "codex", has_more: false }, 200), { principal: "codex", hasMore: false });
  assert.equal(stagedCountLabel(0), "0");
});
