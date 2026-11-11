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
    ["minni_status", "audit.status"],
    ["minni_list_candidates", "candidates.list"],
    ["minni_resolve_candidate", "candidates.resolve"],
    ["minni_handoff_send", "handoff.send"],
    ["minni_handoff_inbox", "handoff.inbox"],
    ["minni_thread_create", "plan.create"],
    ["minni_thread_status", "plan.status"],
    ["minni_thread_update", "plan.update"],
    ["minni_thread_scar", "plan.scar"],
  ]);

  for (const [toolName, operation] of expected) {
    const block = toolBlock(source, toolName);
    assert.match(block, new RegExp(`requireSharedGate\\("${operation.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`), toolName);
  }
});

test("personal-vault recall/drill/learn tools stay plugin-local", async () => {
  const source = await readFile(sourcePath, "utf8");
  for (const toolName of [
    "minni_recall",
    "minni_drill",
    "minni_learn",
  ]) {
    assert.doesNotMatch(toolBlock(source, toolName), /requireSharedGate\(/, toolName);
  }
});
