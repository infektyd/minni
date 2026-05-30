// Pure-data contract tests for the TaskSource → EvidenceRow mapper that the
// frontend uses. Re-implements the rules from
// frontend-src/src/api.ts in plain JS so the test stays runnable with
// `node --test` (no TS toolchain). When you change the rules over there,
// update this file in lockstep — the assertions below are the invariants.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";

// -----------------------------------------------------------------------------
// Mirror of the mapping rules in frontend-src/src/api.ts
// -----------------------------------------------------------------------------

function classFromPath(p) {
  p = p || "";
  if (/^vault\/wiki\/|wiki\//.test(p)) return "wiki";
  if (/^vault\/raw\/|raw\//.test(p)) return "raw";
  if (/^vault\/logs?\/|logs?\//.test(p)) return "log";
  if (/^vault\/inbox\/|inbox\//.test(p)) return "inbox";
  if (/\.(ts|tsx|js|jsx|py|rs|go|md)$/i.test(p) && /(^|\/)(src|code|app)\//.test(p))
    return "code";
  return "other";
}

function privacyFromLevel(level) {
  switch (level) {
    case "private":
    case "blocked":
      return "private";
    case "local-only":
      return "team";
    case "safe":
    default:
      return "public";
  }
}

function authorityFromSource(value) {
  switch (value) {
    case "handoff":
    case "decision":
    case "schema":
      return "owner";
    case "session":
    case "concept":
      return "team";
    case "daemon":
    case "vault":
      return "system";
    default:
      return "public";
  }
}

function afmForSource(src, outcome) {
  if (src.privacyLevel === "blocked") return "dns";
  if (!outcome) return "safe";
  const haystacks = [
    { kind: "dns", list: outcome.doNotStore || [] },
    { kind: "log", list: [...(outcome.logOnly || []), ...(outcome.expires || [])] },
    { kind: "learn", list: outcome.learnCandidates || [] },
  ];
  for (const h of haystacks) {
    if (h.list.some((entry) => entry.includes(src.relativePath) || entry.includes(src.title))) {
      return h.kind;
    }
  }
  return "safe";
}

// -----------------------------------------------------------------------------
// Tests
// -----------------------------------------------------------------------------

test("classFromPath classifies vault sub-trees", () => {
  assert.equal(classFromPath("vault/wiki/handoffs/v4-envelope-schema.md"), "wiki");
  assert.equal(classFromPath("vault/raw/sessions/2026-04-21/compact.json"), "raw");
  assert.equal(classFromPath("vault/logs/hygiene/2026-04-23-prune.log"), "log");
  assert.equal(classFromPath("vault/inbox/2026-04-26T11-40_gemini.json"), "inbox");
  assert.equal(classFromPath(""), "other");
  assert.equal(classFromPath(undefined), "other");
});

test("privacyFromLevel maps daemon privacy onto the UI shape", () => {
  assert.equal(privacyFromLevel("private"), "private");
  assert.equal(privacyFromLevel("blocked"), "private");
  assert.equal(privacyFromLevel("local-only"), "team");
  assert.equal(privacyFromLevel("safe"), "public");
  assert.equal(privacyFromLevel(undefined), "public");
});

test("authorityFromSource collapses authority kinds onto the chip palette", () => {
  assert.equal(authorityFromSource("handoff"), "owner");
  assert.equal(authorityFromSource("decision"), "owner");
  assert.equal(authorityFromSource("schema"), "owner");
  assert.equal(authorityFromSource("session"), "team");
  assert.equal(authorityFromSource("concept"), "team");
  assert.equal(authorityFromSource("daemon"), "system");
  assert.equal(authorityFromSource("vault"), "system");
  assert.equal(authorityFromSource(undefined), "public");
});

test("afmForSource picks dns when source is blocked or do-not-store", () => {
  const blocked = { relativePath: "x", title: "x", privacyLevel: "blocked" };
  assert.equal(afmForSource(blocked, undefined), "dns");

  const src = { relativePath: "vault/raw/foo.md", title: "Foo" };
  const out = {
    learnCandidates: [],
    logOnly: [],
    expires: [],
    doNotStore: ["Found vault/raw/foo.md in pruning"],
  };
  assert.equal(afmForSource(src, out), "dns");
});

test("afmForSource prefers dns over log over learn", () => {
  const src = { relativePath: "vault/raw/foo.md", title: "Foo" };
  const all = {
    learnCandidates: ["Foo"],
    logOnly: ["vault/raw/foo.md"],
    expires: [],
    doNotStore: ["vault/raw/foo.md"],
  };
  assert.equal(afmForSource(src, all), "dns");
  const log = { ...all, doNotStore: [] };
  assert.equal(afmForSource(src, log), "log");
  const learn = { ...log, logOnly: [] };
  assert.equal(afmForSource(src, learn), "learn");
});

test("afmForSource defaults to safe when no outcome match", () => {
  const src = { relativePath: "vault/wiki/foo.md", title: "Foo" };
  assert.equal(afmForSource(src, undefined), "safe");
  assert.equal(
    afmForSource(src, {
      learnCandidates: ["bar"],
      logOnly: ["baz"],
      expires: [],
      doNotStore: [],
    }),
    "safe",
  );
});

// -----------------------------------------------------------------------------
// Drift guard: assert frontend-src/src/api.ts still contains the literals
// that the rules above mirror. If you change one, fail the other.
// -----------------------------------------------------------------------------

test("frontend mapper source still encodes the same rule branches", async () => {
  const apiTs = await readFile(
    path.join(process.cwd(), "frontend-src", "src", "api.ts"),
    "utf8",
  );
  // class branches
  assert.match(apiTs, /vault\\\/wiki\\\/\|wiki\\\//);
  assert.match(apiTs, /vault\\\/raw\\\/\|raw\\\//);
  assert.match(apiTs, /vault\\\/logs\?\\\/\|logs\?\\\//);
  assert.match(apiTs, /vault\\\/inbox\\\/\|inbox\\\//);
  // privacy branches
  assert.match(apiTs, /case "blocked":/);
  assert.match(apiTs, /case "local-only":/);
  // authority branches
  assert.match(apiTs, /case "handoff":/);
  assert.match(apiTs, /case "decision":/);
  assert.match(apiTs, /case "schema":/);
  // outcome partition order: dns first, then log, then learn
  assert.match(apiTs, /kind: "dns"/);
  assert.match(apiTs, /kind: "log"/);
  assert.match(apiTs, /kind: "learn"/);
});
