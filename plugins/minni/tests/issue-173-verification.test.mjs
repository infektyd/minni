import assert from "node:assert/strict";
import test from "node:test";
import { isAuditTelemetryLine } from "../dist/task.js";
import { workspaceFromPayload, findProjectRoot } from "../dist/hook-utils.js";
import { searchVaultNotes, ensureVault, vaultFirstLearn } from "../dist/vault.js";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

test("Issue #173: isAuditTelemetryLine detects audit log header signatures", () => {
  assert.equal(isAuditTelemetryLine("## [2026-07-25 12:00:00] hook_stop | stop session"), true);
  assert.equal(isAuditTelemetryLine("hook_session_start | session started"), true);
  assert.equal(isAuditTelemetryLine("hook_user_prompt_submit | submit"), true);
  assert.equal(isAuditTelemetryLine("Use WAL mode for SQLite performance"), false);
});

test("Issue #173: queryTerms preserves 2-letter technical terms like CI/CD, PR, UI, DB", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-vault-173-"));
  const vaultPath = path.join(root, "vault");
  try {
    await ensureVault(vaultPath);
    await vaultFirstLearn({
      vaultPath,
      title: "CI/CD Setup and PR Workflow",
      content: "Set up CI CD pipeline for PR validation and DB migrations.",
      category: "procedures",
      agentId: "codex",
    });

    const results = await searchVaultNotes(vaultPath, "CI CD pipeline PR DB", 5);
    assert.equal(results.length > 0, true, "searchVaultNotes should return hits for 2-letter term queries");
    assert.match(results[0].title, /CI\/CD Setup/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Issue #173: word boundary term matching prevents substring false positives", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-vault-173-wordbound-"));
  const vaultPath = path.join(root, "vault");
  try {
    await ensureVault(vaultPath);
    // Create a note containing "project", "approach", "reproduce" but NOT the standalone term "pro"
    await vaultFirstLearn({
      vaultPath,
      title: "Project Approach and Reproduce Steps",
      content: "This document describes the project approach to reproduce errors.",
      category: "concepts",
      agentId: "codex",
    });

    // Query for "pro" — should NOT match "project" / "approach" / "reproduce"
    const results = await searchVaultNotes(vaultPath, "pro", 5);
    assert.equal(results.length, 0, "standalone term 'pro' must not false-match inside 'project', 'approach', or 'reproduce'");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Issue #173: workspaceFromPayload resolves sub-directories to canonical project root", () => {
  const topRepoRoot = findProjectRoot(process.cwd());
  const subDir = path.join(topRepoRoot, "plugins", "minni", "src");
  const resolved = workspaceFromPayload({ cwd: subDir }, "fallback");
  assert.equal(resolved, topRepoRoot, "sub-directory path should resolve to canonical project root directory");
});
