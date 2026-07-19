import { test } from "node:test";
import assert from "node:assert/strict";
import { isDaemonResultEmpty, shouldPrescanVault } from "../dist/sovereign.js";

// X5: the local vault pre-scan (searchVaultNotes) is workspace-unscoped and
// bypasses the daemon read/privacy policy. It must only run as an OFFLINE
// FALLBACK — never injected alongside a successful (scoped) daemon recall.
//
// W5 (punch-list #1, recall vs prepare_task asymmetry): a daemon that ANSWERS
// with zero results is JSON-RPC success (daemonOk=true), so the old 2-arg
// !daemonOk gate suppressed the pre-scan on legitimate zero-hit daemon
// answers too — leaving minni_recall blinder than prepare_task's markdown/AFM
// path, which has no such gate. The 3rd arg (daemonEmpty) widens the trigger
// to also cover "daemon answered but empty", without touching the
// !identityDenied guard (a denial is a refusal, not an empty — see
// recovery-denial.test.mjs's reserved_agent_id case).

test("X5: no local vault pre-scan when the daemon recall succeeded with hits", () => {
  // daemon reachable + nonempty + includeVault requested -> still no pre-scan
  // (daemon is the scoped source of truth when it actually has results)
  assert.equal(shouldPrescanVault(true, true, false), false);
  assert.equal(shouldPrescanVault(true, false, false), false);
});

test("X5: local vault pre-scan only as offline fallback when daemon is unavailable", () => {
  assert.equal(shouldPrescanVault(false, true, false), true);
});

test("X5: includeVault=false disables the pre-scan even when the daemon is down", () => {
  assert.equal(shouldPrescanVault(false, false, false), false);
});

test("W5: local vault pre-scan also fires when the daemon answered with zero results", () => {
  assert.equal(shouldPrescanVault(true, true, true), true);
});

test("W5: daemon-empty does not override includeVault=false", () => {
  assert.equal(shouldPrescanVault(true, false, true), false);
});

test("W5: daemon-empty is irrelevant when the daemon already has hits (documentation case)", () => {
  assert.equal(shouldPrescanVault(true, true, false), false);
});

// ── isDaemonResultEmpty: the wire-shape detector behind daemonEmpty ────────
// Prefers the daemon's own `count` field (recall.py:307, always present on a
// successful search response) over inspecting `results` shape, and never
// regex-matches free-form English prose.

test("isDaemonResultEmpty: count=0 with an empty results array is empty", () => {
  assert.equal(isDaemonResultEmpty({ results: [], count: 0 }), true);
});

test("isDaemonResultEmpty: count=1 with a populated results array is not empty", () => {
  assert.equal(
    isDaemonResultEmpty({ results: [{ wikilink: "[[wiki/x]]", score: 1 }], count: 1 }),
    false,
  );
});

test("isDaemonResultEmpty: falls back to array-length when count is absent", () => {
  assert.equal(isDaemonResultEmpty({ results: [] }), true);
  assert.equal(isDaemonResultEmpty({ results: [{ wikilink: "[[wiki/x]]" }] }), false);
});

test("isDaemonResultEmpty: a pre-formatted empty-results sentinel string is empty", () => {
  assert.equal(isDaemonResultEmpty({ results: "No recall results." }), true);
});

test("isDaemonResultEmpty: an undefined response is treated as empty", () => {
  assert.equal(isDaemonResultEmpty(undefined), true);
});
