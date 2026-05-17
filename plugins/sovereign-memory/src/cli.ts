import { DEFAULT_VAULT_PATH } from "./config.js";
import { assessLearningQuality, routeMemoryIntent } from "./policy.js";
import { statusAndAudit } from "./sovereign.js";
import { prepareOutcome, prepareTask } from "./task.js";
import { auditReport, auditTail, ensureVault, vaultFirstLearn, writeVaultPage } from "./vault.js";

function parsePrepareArgs(args: string[]) {
  const remaining: string[] = [];
  let useAfm = false;
  let profile: string | undefined;
  let afmProviderMode: "auto" | "bridge" | "native" | "off" | undefined;

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--afm") {
      useAfm = true;
    } else if (arg === "--profile") {
      profile = args[index + 1];
      index += 1;
    } else if (arg === "--afm-provider") {
      const value = args[index + 1];
      afmProviderMode = value === "auto" || value === "bridge" || value === "native" || value === "off" ? value : undefined;
      index += 1;
    } else {
      remaining.push(arg);
    }
  }

  return {
    useAfm,
    profile,
    afmProviderMode,
    task: remaining.join(" "),
  };
}

function parsePrepareOutcomeArgs(args: string[]) {
  const remaining: string[] = [];
  let useAfm = false;
  let profile: string | undefined;
  let afmProviderMode: "auto" | "bridge" | "native" | "off" | undefined;
  let summary: string | undefined;

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--afm") {
      useAfm = true;
    } else if (arg === "--profile") {
      profile = args[index + 1];
      index += 1;
    } else if (arg === "--afm-provider") {
      const value = args[index + 1];
      afmProviderMode = value === "auto" || value === "bridge" || value === "native" || value === "off" ? value : undefined;
      index += 1;
    } else if (arg === "--summary") {
      summary = args[index + 1];
      index += 1;
    } else {
      remaining.push(arg);
    }
  }

  return {
    useAfm,
    profile,
    afmProviderMode,
    summary,
    task: remaining.join(" "),
  };
}

async function main() {
  const [command, ...args] = process.argv.slice(2);

  if (!command || command === "help") {
    console.log(
      "Usage: node dist/cli.js <status|ensure-vault|audit-tail|audit-report|route|prepare|prepare-outcome|quality|learn|write> ...",
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
    const { task, useAfm, profile, afmProviderMode } = parsePrepareArgs(args);
    if (!task) throw new Error("prepare requires a task");
    console.log(JSON.stringify(await prepareTask({ task, vaultPath: DEFAULT_VAULT_PATH, useAfm, profile: profile as never, afmProviderMode }), null, 2));
    return;
  }

  if (command === "prepare-outcome") {
    const { task, summary, useAfm, profile, afmProviderMode } = parsePrepareOutcomeArgs(args);
    if (!summary) throw new Error("prepare-outcome requires --summary <summary>");
    if (!task) throw new Error("prepare-outcome requires a task");
    console.log(
      JSON.stringify(
        await prepareOutcome({
          task,
          summary,
          vaultPath: DEFAULT_VAULT_PATH,
          useAfm,
          profile: profile as never,
          afmProviderMode,
        }),
        null,
        2,
      ),
    );
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
