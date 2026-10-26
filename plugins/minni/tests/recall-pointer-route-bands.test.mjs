import assert from "node:assert/strict";
import test from "node:test";

import { buildRecallPointer, selectPointerHits } from "../dist/recall-state.js";

test("selectPointerHits never compares scores across routes: daemon 0.90 + vault 0.70 and vault 1.00 + daemon 0.65 both name both route leaders", () => {
  // Case 1: daemon at 0.90, vault at 0.70 (0.20 apart across routes, but each is its route's leader)
  const pair1 = selectPointerHits([
    {
      title: "Daemon Leader",
      wikilink: "[[daemon/leader]]",
      scope: "personal",
      docId: 10,
      route: "daemon",
      score: 0.90,
    },
    {
      title: "Vault Leader",
      wikilink: "[[wiki/decisions/vault-leader]]",
      relativePath: "wiki/decisions/vault-leader.md",
      route: "vault",
      score: 0.70,
    },
  ]);
  assert.equal(pair1.length, 2, "both route leaders must be selected despite 0.20 cross-route gap");
  assert.equal(pair1[0].title, "Daemon Leader", "daemon leader goes first");
  assert.equal(pair1[1].title, "Vault Leader", "vault leader goes second");

  // Case 2: vault at 1.00, daemon at 0.65 (0.35 apart across routes)
  const pair2 = selectPointerHits([
    {
      title: "Vault Phrase Match",
      wikilink: "[[wiki/decisions/phrase]]",
      relativePath: "wiki/decisions/phrase.md",
      route: "vault",
      score: 1.0,
    },
    {
      title: "Daemon Hit",
      wikilink: "[[daemon/hit]]",
      scope: "episodic",
      docId: 99,
      route: "daemon",
      score: 0.65,
    },
  ]);
  assert.equal(pair2.length, 2, "both route leaders must be selected despite 0.35 cross-route gap");
  assert.equal(pair2[0].title, "Daemon Hit", "daemon route leader is ordered before vault route leader");
  assert.equal(pair2[1].title, "Vault Phrase Match");
});

test("selectPointerHits enforces the 0.15 score band within each route independently", () => {
  const hits = [
    {
      title: "Daemon Top",
      wikilink: "[[d1]]",
      scope: "personal",
      docId: 1,
      route: "daemon",
      score: 0.90,
    },
    {
      title: "Daemon In-Band",
      wikilink: "[[d2]]",
      scope: "personal",
      docId: 2,
      route: "daemon",
      score: 0.78, // diff 0.12 < 0.15 -> kept
    },
    {
      title: "Daemon Out-of-Band",
      wikilink: "[[d3]]",
      scope: "personal",
      docId: 3,
      route: "daemon",
      score: 0.70, // diff 0.20 >= 0.15 -> dropped
    },
  ];

  const selected = selectPointerHits(hits);
  assert.deepEqual(
    selected.map((h) => h.title),
    ["Daemon Top", "Daemon In-Band"],
  );
});

test("selectPointerHits drops candidates lacking a resolvable m: or d: handle", () => {
  const hits = [
    {
      title: "Unresolvable Daemon Hit (no docId)",
      wikilink: "[[?]]",
      route: "daemon",
      score: 0.95,
    },
    {
      title: "Explicit Empty Handle",
      wikilink: "[[wiki/decisions/a]]",
      handle: "",
      score: 0.92,
    },
    {
      title: "Valid Vault Hit",
      wikilink: "[[wiki/decisions/valid]]",
      relativePath: "wiki/decisions/valid.md",
      route: "vault",
      score: 0.80,
    },
  ];

  const selected = selectPointerHits(hits);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].title, "Valid Vault Hit");
  assert.match(selected[0].handle, /^m:[0-9a-f]{8}$/);
});

test("selectPointerHits enforces the noise rule: noise never takes slot 1, and only takes slot 2/3 when slot 1 is non-noise", () => {
  // All-noise pool -> returns []
  const onlyNoise = selectPointerHits([
    {
      title: "Plan Journal Noise",
      wikilink: "[[wiki/artifacts/plan-1.log]]",
      relativePath: "wiki/artifacts/plan-1.log.md",
      durability: "noise",
      route: "vault",
      score: 0.85,
    },
  ]);
  assert.equal(onlyNoise.length, 0, "noise-only pool must not emit a pointer");

  // Noise has higher score than durable hit -> durable takes slot 1, noise takes slot 2
  const mixed = selectPointerHits([
    {
      title: "Noise Hit",
      wikilink: "[[d-noise]]",
      scope: "personal",
      docId: 50,
      durability: "noise",
      route: "daemon",
      score: 0.91,
    },
    {
      title: "Durable Hit",
      wikilink: "[[wiki/decisions/d1]]",
      relativePath: "wiki/decisions/d1.md",
      durability: "durable",
      route: "vault",
      score: 0.82,
    },
  ]);
  assert.equal(mixed.length, 2);
  assert.equal(mixed[0].title, "Durable Hit", "non-noise hit must hold slot 1");
  assert.equal(mixed[1].title, "Noise Hit", "noise hit may occupy slot 2 when slot 1 is non-noise");

  const pointer = buildRecallPointer({ topScore: 0.91, topHits: mixed });
  assert.match(pointer, /^📓 2 relevant memories: "Durable Hit"/);
});
