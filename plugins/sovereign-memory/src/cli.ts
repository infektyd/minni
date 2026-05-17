import { DEFAULT_VAULT_PATH } from "./config.js";
import { assessLearningQuality, routeMemoryIntent } from "./policy.js";
import { readAgentContext, statusAndAudit } from "./sovereign.js";
import { prepareOutcome, prepareTask } from "./task.js";
import { buildTeamEvidencePacket, buildTeamPromotionPacket, buildTeamRuntime } from "./team.js";
import { auditReport, auditTail, ensureVault, vaultFirstLearn, writeVaultPage } from "./vault.js";

async function main() {
  const [command, ...args] = process.argv.slice(2);

  if (!command || command === "help") {
    console.log(
      "Usage: node dist/cli.js <status|read|ensure-vault|audit-tail|audit-report|route|prepare|prepare-outcome|team-runtime|team-evidence|team-promotion|quality|learn|write> ...",
    );
    return;
  }

  if (command === "ensure-vault") {
    console.log(JSON.stringify(await ensureVault(DEFAULT_VAULT_PATH), null, 2));
    return;
  }

  if (command === "status") {
    console.log(JSON.stringify(await statusAndAudit(DEFAULT_VAULT_PATH), null, 2));
    return;
  }

  if (command === "read") {
    console.log(JSON.stringify(await readAgentContext({ agentId: args[0] }), null, 2));
    return;
  }

  if (command === "audit-tail") {
    const limit = Number(args[0] ?? "20");
    console.log((await auditTail(DEFAULT_VAULT_PATH, limit)).text);
    return;
  }

  if (command === "audit-report") {
    const limit = Number(args[0] ?? "100");
    console.log(JSON.stringify(await auditReport(DEFAULT_VAULT_PATH, limit), null, 2));
    return;
  }

  if (command === "route") {
    console.log(JSON.stringify(routeMemoryIntent(args.join(" ")), null, 2));
    return;
  }

  if (command === "prepare") {
    const useAfm = args.includes("--afm");
    const profileIndex = args.indexOf("--profile");
    const profile = profileIndex >= 0 ? args[profileIndex + 1] : undefined;
    const task = args
      .filter((arg, index) => arg !== "--afm" && arg !== "--profile" && (profileIndex < 0 || index !== profileIndex + 1))
      .join(" ");
    if (!task) throw new Error("prepare requires a task");
    console.log(JSON.stringify(await prepareTask({ task, vaultPath: DEFAULT_VAULT_PATH, useAfm, profile: profile as never }), null, 2));
    return;
  }

  if (command === "prepare-outcome") {
    const useAfm = args.includes("--afm");
    const profileIndex = args.indexOf("--profile");
    const profile = profileIndex >= 0 ? args[profileIndex + 1] : undefined;
    const summaryIndex = args.indexOf("--summary");
    if (summaryIndex < 0 || !args[summaryIndex + 1]) throw new Error("prepare-outcome requires --summary <summary>");
    const task = args
      .filter(
        (arg, index) =>
          arg !== "--afm" &&
          arg !== "--profile" &&
          arg !== "--summary" &&
          (profileIndex < 0 || index !== profileIndex + 1) &&
          (summaryIndex < 0 || index !== summaryIndex + 1),
      )
      .join(" ");
    if (!task) throw new Error("prepare-outcome requires a task");
    console.log(
      JSON.stringify(
        await prepareOutcome({
          task,
          summary: args[summaryIndex + 1],
          vaultPath: DEFAULT_VAULT_PATH,
          useAfm,
          profile: profile as never,
        }),
        null,
        2,
      ),
    );
    return;
  }

  if (command === "team-runtime") {
    const useAfm = args.includes("--afm");
    const profileIndex = args.indexOf("--profile");
    const agentsIndex = args.indexOf("--agents-json");
    const profile = profileIndex >= 0 ? args[profileIndex + 1] : undefined;
    const agents =
      agentsIndex >= 0 && args[agentsIndex + 1] ? JSON.parse(args[agentsIndex + 1]) as never : undefined;
    const task = args
      .filter(
        (arg, index) =>
          arg !== "--afm" &&
          arg !== "--profile" &&
          arg !== "--agents-json" &&
          (profileIndex < 0 || index !== profileIndex + 1) &&
          (agentsIndex < 0 || index !== agentsIndex + 1),
      )
      .join(" ");
    if (!task) throw new Error("team-runtime requires a task");
    console.log(
      JSON.stringify(
        await buildTeamRuntime({
          task,
          agents,
          vaultPath: DEFAULT_VAULT_PATH,
          useAfm,
          profile: profile as never,
        }),
        null,
        2,
      ),
    );
    return;
  }

  if (command === "team-evidence") {
    const resultsIndex = args.indexOf("--results-json");
    const runtimeIndex = args.indexOf("--runtime-id");
    if (resultsIndex < 0 || !args[resultsIndex + 1]) throw new Error("team-evidence requires --results-json <json>");
    const task = args
      .filter(
        (arg, index) =>
          arg !== "--results-json" &&
          arg !== "--runtime-id" &&
          (resultsIndex < 0 || index !== resultsIndex + 1) &&
          (runtimeIndex < 0 || index !== runtimeIndex + 1),
      )
      .join(" ");
    if (!task) throw new Error("team-evidence requires a task");
    console.log(
      JSON.stringify(
        buildTeamEvidencePacket({
          task,
          runtimeId: runtimeIndex >= 0 ? args[runtimeIndex + 1] : undefined,
          results: JSON.parse(args[resultsIndex + 1]),
        }),
        null,
        2,
      ),
    );
    return;
  }

  if (command === "team-promotion") {
    const agentIndex = args.indexOf("--agent-json");
    const evidenceIndex = args.indexOf("--evidence-json");
    const permissionsIndex = args.indexOf("--permissions-json");
    const idIndex = args.indexOf("--permanent-id");
    const approved = args.includes("--approved");
    if (agentIndex < 0 || !args[agentIndex + 1]) throw new Error("team-promotion requires --agent-json <json>");
    if (evidenceIndex < 0 || !args[evidenceIndex + 1]) throw new Error("team-promotion requires --evidence-json <json>");
    const promotionPacket = await buildTeamPromotionPacket({
      agent: JSON.parse(args[agentIndex + 1]),
      evidence: JSON.parse(args[evidenceIndex + 1]),
      requestedPermissions:
        permissionsIndex >= 0 && args[permissionsIndex + 1] ? JSON.parse(args[permissionsIndex + 1]) : undefined,
      approved,
      permanentAgentId: idIndex >= 0 ? args[idIndex + 1] : undefined,
    });
    console.log(JSON.stringify(promotionPacket, null, 2));
    return;
  }

  if (command === "quality") {
    const [title, ...contentParts] = args;
    if (!title || contentParts.length === 0) throw new Error("quality requires title and content");
    console.log(JSON.stringify(assessLearningQuality({ title, content: contentParts.join(" ") }), null, 2));
    return;
  }

  if (command === "learn") {
    const [title, ...contentParts] = args;
    if (!title || contentParts.length === 0) throw new Error("learn requires title and content");
    console.log(
      JSON.stringify(
        await vaultFirstLearn({
          vaultPath: DEFAULT_VAULT_PATH,
          title,
          content: contentParts.join(" "),
        }),
        null,
        2,
      ),
    );
    return;
  }

  if (command === "write") {
    const [section, title, ...contentParts] = args;
    if (!section || !title || contentParts.length === 0) throw new Error("write requires section, title, and content");
    console.log(
      JSON.stringify(
        await writeVaultPage({
          vaultPath: DEFAULT_VAULT_PATH,
          section: section as never,
          title,
          content: contentParts.join(" "),
        }),
        null,
        2,
      ),
    );
    return;
  }

  throw new Error(`Unknown command: ${command}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
