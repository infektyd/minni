import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldPrescanVault } from "../dist/sovereign.js";

// X5: the local vault pre-scan (searchVaultNotes) is workspace-unscoped and
// bypasses the daemon read/privacy policy. It must only run as an OFFLINE
// FALLBACK — never injected alongside a successful (scoped) daemon recall.

test("X5: no local vault pre-scan when the daemon recall succeeded", () => {
  // daemon reachable + includeVault requested -> still no pre-scan (daemon is scoped source of truth)
  assert.equal(shouldPrescanVault(true, true), false);
  assert.equal(shouldPrescanVault(true, false), false);
});

test("X5: local vault pre-scan only as offline fallback when daemon is unavailable", () => {
  assert.equal(shouldPrescanVault(false, true), true);
});

test("X5: includeVault=false disables the pre-scan even when the daemon is down", () => {
  assert.equal(shouldPrescanVault(false, false), false);
});
