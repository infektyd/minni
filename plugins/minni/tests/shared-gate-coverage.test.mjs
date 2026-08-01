import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

const sourcePath = path.join(process.cwd(), "src", "server.ts");

function toolBlock(source, toolName) {
  const start = source.indexOf(`server.registerTool(\n  "${toolName}"`);
  assert.notEqual(start, -1, `${toolName} registration not found`);
  const next = source.indexOf("server.registerTool(", start + 1);
  return source.slice(start, next === -1 ? undefined : next);
}

test("shared model-facing tools gate before local shared work", async () => {
  const source = await readFile(sourcePath, "utf8");
  const expected = new Map([
    ["minni_team_runtime", "team.runtime"],
    ["minni_team_evidence", "team.evidence"],
    ["minni_team_promotion", "team.promotion"],
    ["minni_status", "audit.status"],
    ["minni_route", "audit.route"],
    ["minni_resolve_candidate", "candidates.resolve"],
    ["minni_learning_quality", "audit.learning_quality"],
    ["minni_audit_report", "audit.report"],
    ["minni_audit_tail", "audit.tail"],
    ["minni_negotiate_handoff", "handoff.negotiate"],
    ["minni_ping_agent_request", "ping.request"],
    ["minni_ping_agent_inbox", "ping.inbox"],
    ["minni_ping_agent_decide", "ping.decide"],
    ["minni_ping_agent_status", "ping.status"],
    ["minni_ack_handoff", "handoff.ack"],
    ["minni_list_pending_handoffs", "handoff.pending"],
    ["minni_await_handoff", "handoff.await"],
    ["minni_subscribe_contradictions", "contradictions.subscribe"],
    ["minni_thread_create", "plan.create"],
    ["minni_thread_update", "plan.update"],
    ["minni_thread_scar", "plan.scar"],
    ["minni_thread_status", "plan.status"],
    ["minni_thread_replan", "plan.replan"],
    ["minni_thread_history", "plan.history"],
    ["minni_thread_revision", "plan.revision"],
    ["minni_thread_diff", "plan.diff"],
    ["minni_thread_restore", "plan.restore"],
    ["minni_thread_activate", "plan.activate"],
    ["minni_thread_deactivate", "plan.deactivate"],
  ]);

  for (const [toolName, operation] of expected) {
    const block = toolBlock(source, toolName);
    assert.match(block, new RegExp(`requireSharedGate\\("${operation.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`), toolName);
  }
});

test("personal-vault and prefilter tools stay plugin-local", async () => {
  const source = await readFile(sourcePath, "utf8");
  for (const toolName of [
    "minni_prepare_task",
    "minni_prepare_outcome",
    "minni_recall",
    "minni_learn",
    "minni_vault_write",
  ]) {
    assert.doesNotMatch(toolBlock(source, toolName), /requireSharedGate\(/, toolName);
  }
});
