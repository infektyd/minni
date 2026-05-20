#!/usr/bin/env node
/*
 * Day 5 Sovereign Memory smoke test.
 * Verifies OpenClaw plugin bridge -> Unix socket -> sovrd -> Sovereign DB.
 */

const {
  health,
  identity,
  full,
  learn,
  recall,
  getMemorySearchManager,
} = require("../dist/index.js");

async function main() {
  const marker = `DAY5-SMOKE-${Date.now()}`;
  const checks = [];

  const h = await health();
  checks.push(["health", h.status === "ok", h]);

  const forgeIdentity = await identity("forge");
  checks.push(["identity endpoint", Object.prototype.hasOwnProperty.call(forgeIdentity, "identity"), forgeIdentity]);

  const forgeFull = await full("forge");
  checks.push(["full endpoint", typeof forgeFull.context === "string" && forgeFull.context.length > 0, { length: String(forgeFull.context || "").length }]);

  const firstLearn = await learn(marker, "fact", "forge", undefined, "workspace-forge");
  checks.push(["learn", firstLearn.status === "learned", firstLearn]);

  const duplicateLearn = await learn(marker, "fact", "forge", undefined, "workspace-forge");
  checks.push(["duplicate learn", duplicateLearn.status === "duplicate", duplicateLearn]);

  const forgeRecall = await recall(marker, "forge", undefined, "workspace-forge", 5);
  checks.push(["forge exact recall", JSON.stringify(forgeRecall).includes(marker), forgeRecall]);

  const syntraKnowledgeRecall = await recall(marker, "syntra", "knowledge", "workspace-syntra", 5);
  checks.push(["cross-agent knowledge recall", JSON.stringify(syntraKnowledgeRecall).includes(marker), syntraKnowledgeRecall]);

  const manager = getMemorySearchManager("forge", "workspace-forge");
  const vectorAvailable = await manager.probeVectorAvailability();
  checks.push(["manager vector probe", vectorAvailable === true, vectorAvailable]);

  const managerResults = await manager.search(marker, { maxResults: 5, minScore: 0 });
  checks.push(["manager search exact recall", JSON.stringify(managerResults).includes(marker), managerResults[0] || null]);

  const failed = checks.filter(([, ok]) => !ok);
  for (const [name, ok, detail] of checks) {
    console.log(`${ok ? "PASS" : "FAIL"} ${name}: ${JSON.stringify(detail).slice(0, 500)}`);
  }
  if (failed.length) {
    console.error(`Day 5 smoke failed: ${failed.map(([name]) => name).join(", ")}`);
    process.exit(1);
  }
  console.log(`DAY5_SMOKE_OK marker=${marker}`);
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
