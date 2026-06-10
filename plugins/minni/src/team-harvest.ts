import { AFM_PREPARE_TASK_MODEL, AFM_PREPARE_TASK_URL } from "./config.js";
import { defaultProviderChain } from "./providers.js";
import type { TeamAgentResultInput } from "./team.js";
import { recordAudit, writeInbox } from "./vault.js";

const MAX_CANDIDATE_LENGTH = 500;
const DEFAULT_MAX_LEARNINGS_PER_CALL = 3;

export interface HarvestedLearning {
  agentId: string;
  candidateText: string;
  source: "afm" | "skipped";
  inboxFilePath?: string;
  slug?: string;
  reason?: string;
}

export interface HarvestEvidenceInput {
  task: string;
  vaultPath: string;
  reports: TeamAgentResultInput[];
  runtimeId?: string;
  afmUrl?: string;
  afmModel?: string;
  maxLearningsPerCall?: number;
}

export interface HarvestDeps {
  callAfm?: (system: string, user: string) => Promise<string>;
  writeInbox?: typeof writeInbox;
  audit?: typeof recordAudit;
}

const HARVEST_SYSTEM_PROMPT = [
  "You distill one durable learning from a team agent's evidence report.",
  "Output exactly one line in one of these two forms, nothing else:",
  '  LEARNING: <single sentence the operator could re-use next session>',
  "  SKIP",
  "Use SKIP when the report holds nothing reusable beyond this task.",
  "No preamble, no chain-of-thought, no markdown, no quotes, no JSON.",
].join("\n");

// Redacts machine-local paths only; AFM is trusted not to emit secrets — extend if that assumption changes.
function redact(value: string): string {
  return value
    .replace(/\/Users\/[^\s"',)]+/g, "[local-path]")
    .replace(/\/Volumes\/[^\s"',)]+/g, "[local-path]")
    .replace(/[\w._-]+\.fmadapter/g, "[adapter-file]");
}

function buildUserPrompt(input: HarvestEvidenceInput, report: TeamAgentResultInput): string {
  const safe = (value: string | undefined, max: number): string =>
    redact(String(value ?? "")).slice(0, max);
  const list = (items: string[] | undefined, max: number): string =>
    Array.isArray(items) ? items.map((item) => safe(item, max)).filter(Boolean).join(" | ") : "";
  return [
    `Task: ${safe(input.task, 400)}`,
    `Agent: ${safe(report.agentId, 80)}`,
    `Status: ${safe(report.status, 40)}`,
    `Summary: ${safe(report.summary, 600)}`,
    `Evidence: ${list(report.evidence, 200)}`,
    `Changed files: ${list(report.changedFiles, 160)}`,
    `Verification: ${list(report.verification, 200)}`,
    `Blockers: ${list(report.blockers, 200)}`,
  ].join("\n");
}

type ParsedAfm =
  | { kind: "learning"; text: string }
  | { kind: "skip" }
  | { kind: "empty" }
  | { kind: "off-contract" };

function parseAfmResponse(raw: string): ParsedAfm {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) return { kind: "empty" };
  if (/^skip\s*$/i.test(trimmed)) return { kind: "skip" };
  const learning = trimmed.match(/^learning\s*:\s*(.+)$/i);
  if (learning) {
    const text = learning[1].trim();
    return text ? { kind: "learning", text } : { kind: "empty" };
  }
  return { kind: "off-contract" };
}

async function defaultCallAfm(system: string, user: string, url: string, model: string): Promise<string> {
  const body = {
    model,
    temperature: 0,
    max_tokens: 120,
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
  };
  // P2: routed through the provider chain (AFM-only chain stays byte-identical
  // to the old direct postJson path — enforced by the P0 golden contracts).
  const result = await defaultProviderChain().chat({
    payload: body,
    operation: "extraction",
    url,
    timeoutMs: 30000,
    mode: "bridge",
  });
  if (!result.ok) {
    const message = result.error ?? "AFM harvest failed";
    if (message.startsWith("HTTP ")) {
      throw new Error(`AFM harvest ${message}`);
    }
    if (message.endsWith("timed out")) {
      throw new Error("AFM harvest request timed out");
    }
    throw new Error(message);
  }
  const parsed = result.data as { choices?: Array<{ message?: { content?: string } }> } | undefined;
  const content = parsed?.choices?.[0]?.message?.content;
  return typeof content === "string" ? content : "";
}

function skippedEntry(agentId: string, reason: string, candidateText = ""): HarvestedLearning {
  return { agentId, candidateText, source: "skipped", reason };
}

export async function harvestEvidence(
  input: HarvestEvidenceInput,
  deps: HarvestDeps = {},
): Promise<HarvestedLearning[]> {
  if (!input.task.trim()) throw new Error("harvest requires task.");
  if (!input.vaultPath.trim()) throw new Error("harvest requires vaultPath.");
  const afmUrl = input.afmUrl ?? AFM_PREPARE_TASK_URL;
  const afmModel = input.afmModel ?? AFM_PREPARE_TASK_MODEL;
  const maxPerCall = Math.max(1, input.maxLearningsPerCall ?? DEFAULT_MAX_LEARNINGS_PER_CALL);
  const callAfm = deps.callAfm ?? ((system, user) => defaultCallAfm(system, user, afmUrl, afmModel));
  const writer = deps.writeInbox ?? writeInbox;
  const audit = deps.audit ?? recordAudit;

  const learnings: HarvestedLearning[] = [];
  let written = 0;
  let skipped = 0;

  for (const report of input.reports) {
    const agentId = report.agentId || "unknown-agent";
    let raw = "";
    try {
      raw = await callAfm(HARVEST_SYSTEM_PROMPT, buildUserPrompt(input, report));
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      learnings.push(skippedEntry(agentId, reason));
      skipped += 1;
      continue;
    }

    const parsed = parseAfmResponse(raw);
    if (parsed.kind === "skip") {
      learnings.push(skippedEntry(agentId, "AFM returned SKIP"));
      skipped += 1;
      continue;
    }
    if (parsed.kind === "empty") {
      learnings.push(skippedEntry(agentId, "AFM returned empty response"));
      skipped += 1;
      continue;
    }
    if (parsed.kind === "off-contract") {
      learnings.push(skippedEntry(agentId, "no LEARNING: prefix"));
      skipped += 1;
      continue;
    }

    const candidateText = redact(parsed.text).slice(0, MAX_CANDIDATE_LENGTH);
    if (!candidateText) {
      learnings.push(skippedEntry(agentId, "candidate empty after redaction"));
      skipped += 1;
      continue;
    }

    if (written >= maxPerCall) {
      learnings.push(skippedEntry(agentId, "per-call cap reached", candidateText));
      skipped += 1;
      continue;
    }

    try {
      const entry = await writer(input.vaultPath, `harvest-${agentId}`, {
        kind: "team-harvest",
        task: input.task,
        runtimeId: input.runtimeId,
        agentId,
        candidateText,
        source: "afm",
      });
      learnings.push({
        agentId,
        candidateText,
        source: "afm",
        inboxFilePath: entry.filePath,
        slug: entry.slug,
      });
      written += 1;
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      learnings.push(skippedEntry(agentId, `inbox write failed: ${reason}`, candidateText));
      skipped += 1;
    }
  }

  try {
    await audit(input.vaultPath, {
      tool: "minni_team_harvest",
      summary: input.task.slice(0, 120),
      details: {
        runtimeId: input.runtimeId,
        totalReports: input.reports.length,
        written,
        skipped,
      },
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    process.stderr.write(`minni_team_harvest: audit append failed: ${reason}\n`);
  }

  return learnings;
}
