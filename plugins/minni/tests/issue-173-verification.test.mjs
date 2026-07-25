import assert from "node:assert/strict";
import test from "node:test";
import { isAuditTelemetryLine } from "../dist/task.js";
import { workspaceFromPayload, findProjectRoot } from "../dist/hook-utils.js";
import { searchVaultNotes, ensureVault, vaultFirstLearn } from "../dist/vault.js";
import { mkdir, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
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

// REGRESSION (red-team, defect 3 "symlinks"): path.resolve does NOT collapse
// symlinks, so the same directory reached through a link and through its real
// path produced two different workspace labels — one project's memory split
// across two partitions. findProjectRoot realpaths before the walk.
test("Issue #173: findProjectRoot collapses symlinks to one canonical workspace label", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-projroot-link-"));
  try {
    const base = await realpath(root);
    const repoRoot = path.join(base, "repo");
    const nested = path.join(repoRoot, "src");
    await mkdir(nested, { recursive: true });
    await mkdir(path.join(repoRoot, ".git"), { recursive: true });

    // `link` -> `repo/src`: the SAME directory, reached a second way.
    const link = path.join(base, "link");
    await symlink(nested, link, "dir");

    assert.equal(
      findProjectRoot(link),
      repoRoot,
      "a symlink into a repo must resolve to the repo root, not to the link path",
    );
    assert.equal(
      findProjectRoot(link),
      findProjectRoot(nested),
      "the link and its target must yield ONE workspace label, not two",
    );

    // A symlinked path that is NOT inside any repo still canonicalizes, so the
    // fallback label is stable across both spellings too.
    const orphanReal = path.join(base, "orphan");
    await mkdir(orphanReal, { recursive: true });
    const orphanLink = path.join(base, "orphan-link");
    await symlink(orphanReal, orphanLink, "dir");
    assert.equal(findProjectRoot(orphanLink), orphanReal, "no-repo fallback canonicalizes too");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

// REGRESSION (red-team, defect 3 "relative / ~ input"): a relative cwd used to
// be resolved against the HOOK PROCESS's cwd — a value the harness controls, so
// the same session could label itself differently on two runs. The contract is
// now: expand `~`, require absolute, otherwise report "no honest root" so the
// caller falls back to its configured default.
test("Issue #173: findProjectRoot rejects non-absolute input and expands ~", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-projroot-rel-"));
  const savedHome = process.env.HOME;
  try {
    const base = await realpath(root);

    assert.equal(findProjectRoot("src"), undefined, "a bare relative path has no deterministic root");
    assert.equal(findProjectRoot("./src"), undefined, "an explicitly relative path likewise");
    assert.equal(findProjectRoot("../up"), undefined, "a parent-relative path likewise");
    assert.equal(findProjectRoot(""), undefined, "an empty cwd is not a workspace");

    // The caller must fall back to its stable configured label rather than mint
    // a cwd-dependent one.
    assert.equal(
      workspaceFromPayload({ cwd: "src" }, "fallback"),
      "fallback",
      "a non-anchorable cwd falls back instead of guessing from process.cwd()",
    );

    // `~` IS anchorable — expand it and walk normally.
    const home = path.join(base, "home", "user");
    const project = path.join(home, "code", "widget");
    await mkdir(project, { recursive: true });
    await mkdir(path.join(project, ".git"), { recursive: true });
    process.env.HOME = home;
    assert.equal(findProjectRoot("~/code/widget/src"), project, "~ expands to $HOME then walks");
    assert.equal(findProjectRoot("~"), home, "a bare ~ resolves to $HOME itself");
  } finally {
    if (savedHome === undefined) delete process.env.HOME;
    else process.env.HOME = savedHome;
    await rm(root, { recursive: true, force: true });
  }
});

// (c) The $HOME stop. A dotfiles repo at $HOME is common; without the stop every
// non-repo directory under $HOME would report $HOME as its workspace, merging
// unrelated projects into one memory label.
//
// DECIDED (red-team, defect 3 "repo at $HOME"): the ceiling stops DESCENDANTS
// from being attributed to $HOME; $HOME itself still maps to itself. That is
// the identity result, not a collapse — no other input can produce it — and for
// a session actually running in a home-directory repo it is the only correct
// label. The code was right; the doc comment claiming $HOME is never returned
// was wrong and has been corrected to match.
test("Issue #173: findProjectRoot never attributes a DESCENDANT to $HOME; $HOME maps to itself", async () => {
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
    assert.equal(
      findProjectRoot(home),
      home,
      "$HOME itself maps to itself — the identity result, not a collapse",
    );

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
