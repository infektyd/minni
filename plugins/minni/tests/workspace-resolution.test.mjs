import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  deriveWorkspaceIdFromCwd,
  recordActiveSessionWorkspace,
  resolveSessionWorkspaceId,
  resolveSessionWorkspaceWithDiag,
  writeActiveSessionHint,
} from "../dist/config.js";
import { findProjectRoot } from "../dist/hook-utils.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = path.resolve(__dirname, "..");

test("test_workspace_resolution_order_and_h5_diagnostics", async () => {
  // 1. Verify no roots/list or .listRoots( in plugins/minni/src/
  const srcDir = path.join(PLUGIN_ROOT, "src");
  for (const entry of fs.readdirSync(srcDir)) {
    if (!entry.endsWith(".ts")) continue;
    const text = fs.readFileSync(path.join(srcDir, entry), "utf8");
    assert.ok(!text.includes("roots/list"), `forbidden roots/list in ${entry}`);
    assert.ok(!text.includes(".listRoots("), `forbidden .listRoots( in ${entry}`);
  }

  // 2. Verify plugins/minni/hooks/hooks.json uses `exec node ...` on every command (P2-4)
  const hooksJson = JSON.parse(
    fs.readFileSync(path.join(PLUGIN_ROOT, "hooks", "hooks.json"), "utf8"),
  );
  for (const [event, groups] of Object.entries(hooksJson.hooks)) {
    for (const group of groups) {
      for (const hook of group.hooks) {
        assert.ok(
          hook.command.startsWith("exec node "),
          `hooks.json ${event} command must start with 'exec node ': got ${hook.command}`,
        );
      }
    }
  }

  // 3. Verify deriveWorkspaceIdFromCwd & findProjectRoot unchanged (N9)
  assert.equal(typeof deriveWorkspaceIdFromCwd, "function");
  assert.equal(typeof findProjectRoot, "function");
  assert.equal(typeof writeActiveSessionHint, "function");

  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "minni-ws-res-"));
  try {
    const vaultDir = path.join(tmpRoot, "vault");
    fs.mkdirSync(vaultDir, { recursive: true });
    const runtimeDir = path.join(vaultDir, ".runtime");
    fs.mkdirSync(runtimeDir, { recursive: true });

    // 4. writeActiveSessionHint skips ~/.minni/plugin paths and non-git $HOME
    const fakePluginPath = path.join(os.homedir(), ".minni", "plugin", "0.6.0+git.abc12345");
    writeActiveSessionHint(vaultDir, fakePluginPath, { agentId: "claude-code", hostPid: process.pid });
    assert.ok(
      !fs.existsSync(path.join(runtimeDir, `active-session-claude-code-${process.pid}.json`)),
      "must not write active-session hint for ~/.minni/plugin path",
    );

    if (!fs.existsSync(path.join(os.homedir(), ".git"))) {
      writeActiveSessionHint(vaultDir, os.homedir(), { agentId: "claude-code", hostPid: process.pid });
      assert.ok(
        !fs.existsSync(path.join(runtimeDir, `active-session-claude-code-${process.pid}.json`)),
        "must not write active-session hint for non-git $HOME",
      );
    }

    // 5. Valid active-session hint resolves in Step 2, and MINNI_PINNED_WORKSPACE_ID wins in Step 1
    const nowMs = Date.now();
    recordActiveSessionWorkspace(vaultDir, "workspace-hint-project", {
      agentId: "claude-code",
      hostPid: process.pid,
      nowMs,
    });
    assert.equal(
      resolveSessionWorkspaceId(vaultDir, undefined, {
        agentId: "claude-code",
        hostPid: process.pid,
        nowMs,
      }),
      "workspace-hint-project",
    );

    const prevPinned = process.env.MINNI_PINNED_WORKSPACE_ID;
    process.env.MINNI_PINNED_WORKSPACE_ID = "workspace-operator-pin";
    try {
      assert.equal(
        resolveSessionWorkspaceId(vaultDir, undefined, {
          agentId: "claude-code",
          hostPid: process.pid,
          nowMs,
        }),
        "workspace-operator-pin",
      );
    } finally {
      if (prevPinned === undefined) delete process.env.MINNI_PINNED_WORKSPACE_ID;
      else process.env.MINNI_PINNED_WORKSPACE_ID = prevPinned;
    }

    // 6. H5 Diagnostics: verify all 7 HintMissReason enum classifications
    const hintFile = path.join(runtimeDir, `active-session-claude-code-${process.pid}.json`);

    // (a) hint_file_missing
    fs.unlinkSync(hintFile);
    const missMissing = resolveSessionWorkspaceWithDiag(vaultDir, undefined, {
      agentId: "claude-code",
      hostPid: process.pid,
      nowMs,
    });
    assert.equal(missMissing.hint_miss_reason, "hint_file_missing");
    assert.equal(missMissing.diag?.event, "workspace_resolution_diag");

    // (b) hint_parse_error
    fs.writeFileSync(hintFile, "{corrupt-json", "utf8");
    const missParse = resolveSessionWorkspaceWithDiag(vaultDir, undefined, {
      agentId: "claude-code",
      hostPid: process.pid,
      nowMs,
    });
    assert.equal(missParse.hint_miss_reason, "hint_parse_error");

    // (c) hint_agent_mismatch
    fs.writeFileSync(
      hintFile,
      JSON.stringify({
        workspace_id: "workspace-x",
        agent_id: "other-agent",
        host_pid: process.pid,
        updated_at: new Date(nowMs).toISOString(),
      }),
      "utf8",
    );
    const missAgent = resolveSessionWorkspaceWithDiag(vaultDir, undefined, {
      agentId: "claude-code",
      hostPid: process.pid,
      nowMs,
    });
    assert.equal(missAgent.hint_miss_reason, "hint_agent_mismatch");

    // (d) hint_pid_not_alive
    const deadPid = 999999;
    const deadHintFile = path.join(runtimeDir, `active-session-claude-code-${deadPid}.json`);
    fs.writeFileSync(
      deadHintFile,
      JSON.stringify({
        workspace_id: "workspace-x",
        agent_id: "claude-code",
        host_pid: deadPid,
        updated_at: new Date(nowMs).toISOString(),
      }),
      "utf8",
    );
    const missDeadPid = resolveSessionWorkspaceWithDiag(vaultDir, undefined, {
      agentId: "claude-code",
      hostPid: deadPid,
      nowMs,
      checkHostPidAlive: true,
    });
    assert.equal(missDeadPid.hint_miss_reason, "hint_pid_not_alive");

    // (e) hint_expired_ttl
    fs.writeFileSync(
      hintFile,
      JSON.stringify({
        workspace_id: "workspace-x",
        agent_id: "claude-code",
        host_pid: process.pid,
        updated_at: new Date(nowMs - 31 * 60 * 1000).toISOString(),
      }),
      "utf8",
    );
    const missTtl = resolveSessionWorkspaceWithDiag(vaultDir, undefined, {
      agentId: "claude-code",
      hostPid: process.pid,
      nowMs,
    });
    assert.equal(missTtl.hint_miss_reason, "hint_expired_ttl");

    // (f) hint_clock_skew
    fs.writeFileSync(
      hintFile,
      JSON.stringify({
        workspace_id: "workspace-x",
        agent_id: "claude-code",
        host_pid: process.pid,
        updated_at: new Date(nowMs + 120_000).toISOString(),
      }),
      "utf8",
    );
    const missSkew = resolveSessionWorkspaceWithDiag(vaultDir, undefined, {
      agentId: "claude-code",
      hostPid: process.pid,
      nowMs,
    });
    assert.equal(missSkew.hint_miss_reason, "hint_clock_skew");

    // (g) hint_workspace_invalid
    fs.writeFileSync(
      hintFile,
      JSON.stringify({
        workspace_id: "workspace-unknown",
        agent_id: "claude-code",
        host_pid: process.pid,
        updated_at: new Date(nowMs).toISOString(),
      }),
      "utf8",
    );
    const missInvalidWs = resolveSessionWorkspaceWithDiag(vaultDir, undefined, {
      agentId: "claude-code",
      hostPid: process.pid,
      nowMs,
    });
    assert.equal(missInvalidWs.hint_miss_reason, "hint_workspace_invalid");
  } finally {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  }
});
