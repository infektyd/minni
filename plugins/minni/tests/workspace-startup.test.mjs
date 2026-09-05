// Plugin context follows a fresh process's git cwd; recall remains governed
// by the daemon principal, without a caller-selected project filter.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const configUrl = new URL("../dist/config.js", import.meta.url).href;
const sovereignUrl = new URL("../dist/sovereign.js", import.meta.url).href;
const script = `
import { DEFAULT_WORKSPACE_ID, CODEX_WORKSPACE_ID } from ${JSON.stringify(configUrl)};
import { recallMemory } from ${JSON.stringify(sovereignUrl)};
const requests = [];
for (const workspaceId of [undefined, 'workspace-requested-elsewhere']) {
  await recallMemory({ query: 'cross-project context', agentId: 'codex', workspaceId },
    async (_socket, method, params) => {
      requests.push(JSON.parse(JSON.stringify({ method, params })));
      return { ok: true, data: { results: [] } };
    });
}
console.log(JSON.stringify({ DEFAULT_WORKSPACE_ID, CODEX_WORKSPACE_ID, requests }));
`;

function isolatedGitEnv(source) {
  // A linked-worktree pre-push exports GIT_DIR. Passing it to git init changes
  // the parent repository instead of creating the requested disposable repo.
  return Object.fromEntries(Object.entries(source).filter(([key]) => !key.startsWith("GIT_")));
}

for (const hookEnvironment of [false, true]) {
  test(`fresh processes derive distinct git context without narrowing daemon recall (hook=${hookEnvironment})`, async () => {
    const root = await mkdtemp(path.join(tmpdir(), "minni-workspace-startup-"));
    let env = isolatedGitEnv(process.env);
    delete env.MINNI_WORKSPACE_ID;
    delete env.MINNI_CODEX_WORKSPACE_ID;
    try {
      let parentConfig;
      let parentConfigBefore;
      if (hookEnvironment) {
        const parent = path.join(root, "parent");
        const linked = path.join(root, "linked");
        execFileSync("git", ["init", "-q", parent], { env });
        execFileSync("git", ["-C", parent, "-c", "user.name=fixture",
          "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
          "commit", "--allow-empty", "-qm", "fixture"], { env });
        execFileSync("git", ["-C", parent, "worktree", "add", "-q", "--detach", linked], { env });
        const gitDir = execFileSync("git", ["-C", linked, "rev-parse", "--absolute-git-dir"],
          { env, encoding: "utf8" }).trim();
        parentConfig = path.join(parent, ".git", "config");
        parentConfigBefore = await readFile(parentConfig, "utf8");
        assert.match(parentConfigBefore, /bare = false/);
        env = isolatedGitEnv({ ...env, GIT_DIR: gitDir, GIT_PREFIX: "" });
      }
      const observed = [];
      for (const name of ["project-a", "project-b", "plugin-cache"]) {
        const cwd = path.join(root, name);
        await mkdir(cwd);
        if (name !== "plugin-cache") execFileSync("git", ["init", "-q", cwd], { env });
        observed.push(JSON.parse(execFileSync(process.execPath,
          ["--input-type=module", "-e", script], { cwd, env, encoding: "utf8", timeout: 10_000 })));
      }
      assert.deepEqual(observed.map(row => row.DEFAULT_WORKSPACE_ID), [
        "workspace-project-a", "workspace-project-b", "workspace-unknown",
      ]);
      // Hook constants intentionally never infer from their process cwd: hooks
      // use the event payload's project path, otherwise remain explicitly unknown.
      assert.deepEqual(observed.map(row => row.CODEX_WORKSPACE_ID), [
        "workspace-unknown", "workspace-unknown", "workspace-unknown",
      ]);
      for (const row of observed) {
        assert.equal(row.requests.length, 2);
        for (const request of row.requests) {
          assert.equal(request.method, "search");
          assert.equal(request.params.agent_id, "codex");
          assert.equal(request.params.query, "cross-project context");
          assert.equal(Object.hasOwn(request.params, "workspace_id"), false);
          assert.equal(Object.hasOwn(request.params, "scope"), false);
        }
        assert.deepEqual(row.requests[0], row.requests[1]);
      }
      assert.deepEqual(observed[0].requests, observed[1].requests);
      if (parentConfig) {
        assert.equal(await readFile(parentConfig, "utf8"), parentConfigBefore,
          "disposable git initialization must not modify the hook's parent repository");
      }
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });

}
