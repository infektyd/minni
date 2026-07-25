import assert from "node:assert/strict";
import test from "node:test";
import { isAuditTelemetryLine } from "../dist/task.js";
import { workspaceFromPayload, findProjectRoot } from "../dist/hook-utils.js";
import { searchVaultNotes, ensureVault, vaultFirstLearn } from "../dist/vault.js";
import { mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
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

// Fixture-driven, NOT self-referential: the expected roots are directories this
// test creates, so a findProjectRoot that always returned "/" (or the input, or
// $HOME) fails here. Deriving the expectation from findProjectRoot itself would
// make the assertion vacuously true and couple it to the runner's cwd.
test("Issue #173: workspaceFromPayload resolves sub-directories to the fixture project root", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-projroot-"));
  try {
    // realpath: macOS tmpdir is a /var -> /private/var symlink, and
    // path.resolve does not collapse it — compare against the same form
    // findProjectRoot returns for a resolved input.
    const base = await realpath(root);
    const repoRoot = path.join(base, "repo");
    const nested = path.join(repoRoot, "plugins", "minni", "src");
    await mkdir(nested, { recursive: true });
    await mkdir(path.join(repoRoot, ".git"), { recursive: true });

    assert.equal(
      workspaceFromPayload({ cwd: nested }, "fallback"),
      repoRoot,
      "a nested sub-directory must resolve to the nearest .git ancestor",
    );
    assert.equal(
      workspaceFromPayload({ working_directory: repoRoot }, "fallback"),
      repoRoot,
      "the repo root resolves to itself",
    );

    // A worktree/submodule checkout carries `.git` as a FILE, not a directory —
    // it is still a project root.
    const worktree = path.join(base, "worktree");
    await mkdir(path.join(worktree, "src"), { recursive: true });
    await writeFile(path.join(worktree, ".git"), "gitdir: /elsewhere/.git/worktrees/wt\n", "utf8");
    assert.equal(
      workspaceFromPayload({ cwd: path.join(worktree, "src") }, "fallback"),
      worktree,
      "a .git FILE marks a project root just as a .git directory does",
    );

    // (a) No .git anywhere up the chain => the resolved input path, unchanged.
    const orphan = path.join(base, "orphan", "deep");
    await mkdir(orphan, { recursive: true });
    assert.equal(
      workspaceFromPayload({ cwd: orphan }, "fallback"),
      orphan,
      "no repo root found => the resolved input path",
    );

    // (b) An explicit workspace_id always wins over cwd derivation.
    assert.equal(
      workspaceFromPayload({ workspace_id: "explicit-ws", cwd: nested }, "fallback"),
      "explicit-ws",
      "explicit workspace_id must win over cwd",
    );
    assert.equal(workspaceFromPayload({}, "fallback"), "fallback", "no cwd => the fallback");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// (c) The $HOME stop. A dotfiles repo at $HOME is common; without the stop every
// non-repo directory under $HOME would report $HOME as its workspace, merging
// unrelated projects into one memory label.
test("Issue #173: findProjectRoot never walks past — or returns — $HOME", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-projroot-home-"));
  const savedHome = process.env.HOME;
  try {
    const base = await realpath(root);
    const home = path.join(base, "home", "user");
    const project = path.join(home, "code", "widget");
    await mkdir(project, { recursive: true });
    // The dotfiles repo that used to swallow every child directory.
    await mkdir(path.join(home, ".git"), { recursive: true });
    process.env.HOME = home;

    assert.equal(
      findProjectRoot(project),
      project,
      "a non-repo dir under a $HOME dotfiles repo keeps its own identity",
    );
    assert.equal(findProjectRoot(home), home, "$HOME itself falls back to the resolved input");

    // A real repo BELOW $HOME still resolves normally — the stop is a ceiling,
    // not a blanket opt-out.
    await mkdir(path.join(project, ".git"), { recursive: true });
    assert.equal(findProjectRoot(path.join(project, "src", "deep")), project);
  } finally {
    if (savedHome === undefined) delete process.env.HOME;
    else process.env.HOME = savedHome;
    await rm(root, { recursive: true, force: true });
  }
});
