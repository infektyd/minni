import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

import { callAfmPrepareTask, prepareOutcome, prepareTask } from "../dist/task.js";

const AFM_URL = process.env.MINNI_AFM_PREPARE_TASK_URL ?? "http://127.0.0.1:11437/v1/chat/completions";
const AFM_HEALTH_URL = process.env.MINNI_AFM_HEALTH_URL ?? "http://127.0.0.1:11437/health";

async function assertAfmHealth() {
  const response = await fetch(AFM_HEALTH_URL);
  assert.equal(response.ok, true, `AFM health failed with HTTP ${response.status}`);
  const health = await response.json();
  assert.equal(health.status, "ok");
  assert.equal(health.backend, "apple-foundation-models");
  assert.equal(health.availability, "available");
  return health;
}

function summarizePacket(packet) {
  return {
    mode: packet.mode,
    afm: packet.afm,
    daemonOk: packet.recall.daemonOk,
    intent: packet.intent,
    briefLength: packet.brief.length,
    sourceCount: packet.relevantSources.length,
    actionCount: packet.recommendedNextActions.length,
  };
}

async function main() {
  const root = await mkdtemp(path.join(tmpdir(), "sm-live-prepare-"));
  const evidence = {};
  try {
    evidence.health = await assertAfmHealth();

    const direct = await callAfmPrepareTask(AFM_URL, {
      task: "Live low-memory v0 adapter check for Codex task prep.",
      intent: "verify",
      budgetTokens: 1000,
      constraints: ["No secrets.", "No adapter writes.", "No extraction."],
      currentState: ["The AFM bridge health endpoint is available."],
      relevantSources: [],
      daemonLead: "daemon reachable",
      model: "apple-foundation-models",
    });
    assert.equal(direct.ok, true, direct.error);
    assert.equal(typeof direct.data.brief, "string");
    assert.ok(direct.data.brief.length > 0);
    evidence.directAfm = {
      ok: direct.ok,
      briefLength: direct.data.brief.length,
      actionCount: direct.data.recommendedNextActions?.length ?? 0,
      riskCount: direct.data.risks?.length ?? 0,
    };

    const packet = await prepareTask({
      task: "live low-memory battery for sovereign_prepare_task using the v0 AFM adapter",
      useAfm: true,
      budgetTokens: 2000,
      vaultPath: root,
      limit: 2,
    });
    assert.equal(packet.afm.requested, true);
    assert.equal(packet.afm.used, true, packet.afm.error);
    assert.equal(packet.recall.daemonOk, true, packet.recall.error);
    assert.ok(packet.brief.length > 0);
    evidence.prepareTask = summarizePacket(packet);

    const cli = spawnSync(
      process.execPath,
      ["dist/cli.js", "prepare", "--afm", "live CLI prepare task using v0 adapter"],
      {
        cwd: process.cwd(),
        encoding: "utf8",
        env: {
          ...process.env,
          MINNI_CODEX_VAULT_PATH: root,
        },
        timeout: 60000,
      },
    );
    assert.equal(cli.status, 0, cli.stderr || cli.stdout);
    const cliPacket = JSON.parse(cli.stdout);
    assert.equal(cliPacket.afm.used, true, cliPacket.afm.error);
    evidence.cli = summarizePacket(cliPacket);

    const outcome = await prepareOutcome({
      task: "live low-memory outcome battery for v0 adapter",
      summary: "Verified the prepare outcome dry-run path with tiny prompts.",
      changedFiles: ["plugins/minni/src/task.ts"],
      verification: ["live AFM bridge responded"],
      profile: "compact",
      useAfm: true,
      vaultPath: root,
    });
    assert.equal(outcome.afm.requested, true);
    assert.equal(outcome.afm.used, true, outcome.afm.error);
    assert.ok(outcome.outcomeDraft.learnCandidates.length > 0);
    evidence.prepareOutcome = {
      mode: outcome.mode,
      afm: outcome.afm,
      profile: outcome.profile,
      learnCandidates: outcome.outcomeDraft.learnCandidates.length,
      doNotStore: outcome.outcomeDraft.doNotStore.length,
    };

    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ["dist/server.js"],
      cwd: process.cwd(),
      stderr: "pipe",
      env: {
        ...process.env,
        MINNI_CODEX_VAULT_PATH: root,
      },
    });
    const client = new Client({ name: "sovereign-memory-live-prepare-test", version: "0.1.0" });
    try {
      await client.connect(transport);
      const tools = await client.listTools();
      assert.equal(tools.tools.length, 12, `expected 12 v1 MCP tools, got ${tools.tools.length}`);
      assert.ok(tools.tools.some((tool) => tool.name === "minni_recall"));
      assert.ok(tools.tools.some((tool) => tool.name === "minni_status"));
      assert.ok(!tools.tools.some((tool) => tool.name === "minni_prepare_task"));
      assert.ok(!tools.tools.some((tool) => tool.name === "minni_prepare_outcome"));
      const recallResult = await client.callTool({
        name: "minni_recall",
        arguments: {
          query: "live MCP recall check on 12-tool v1 server",
          limit: 2,
        },
      });
      const recallText = recallResult.content?.[0]?.text;
      assert.equal(typeof recallText, "string");
      assert.ok(recallText.length > 0);
      const statusResult = await client.callTool({
        name: "minni_status",
        arguments: {},
      });
      const statusText = statusResult.content?.[0]?.text;
      assert.equal(typeof statusText, "string");
      const statusJson = JSON.parse(statusText);
      assert.equal(typeof statusJson.agentId, "string");
      evidence.mcp = {
        toolCount: tools.tools.length,
        hasRecall: true,
        hasStatus: true,
        recallLength: recallText.length,
        agentId: statusJson.agentId,
      };
    } finally {
      await client.close();
    }

    console.log(JSON.stringify(evidence, null, 2));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack : error);
  process.exit(1);
});
