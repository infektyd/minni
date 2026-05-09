import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { URL } from "node:url";
import { AFM_PREPARE_TASK_MODEL, AFM_PREPARE_TASK_URL } from "./config.js";
import type { TeamAgentResultInput } from "./team.js";
import { recordAudit, writeInbox } from "./vault.js";

export interface HarvestedLearning {
  agentId: string;
  inboxFilePath: string;
  slug: string;
  candidateText: string;
  source: "afm" | "skipped";
  reason?: string;
}

export interface HarvestEvidenceInput {
  task: string;
  vaultPath: string;
  reports: TeamAgentResultInput[];
  runtimeId?: string;
  afmUrl?: string;
  afmModel?: string;
  maxLearningsPerAgent?: number;
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

function parseAfmResponse(raw: string): { kind: "learning"; text: string } | { kind: "skip" } | { kind: "empty" } {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) return { kind: "empty" };
  if (/^skip\s*$/i.test(trimmed)) return { kind: "skip" };
  const learning = trimmed.match(/^learning\s*:\s*(.+)$/i);
  if (learning) {
    const text = learning[1].trim();
    return text ? { kind: "learning", text } : { kind: "empty" };
  }
  // Fall back to treating the whole response as the learning if it looks substantive.
  if (/^[a-z0-9]/i.test(trimmed) && trimmed.length <= 500) {
    return { kind: "learning", text: trimmed };
  }
  return { kind: "empty" };
}

async function defaultCallAfm(system: string, user: string, url: string, model: string): Promise<string> {
  const body = JSON.stringify({
    model,
    temperature: 0,
    max_tokens: 120,
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
  });
  return new Promise((resolve, reject) => {
    const parsedUrl = new URL(url);
    const client = parsedUrl.protocol === "https:" ? httpsRequest : httpRequest;
    const req = client(
      parsedUrl,
      {
        method: "POST",
        timeout: 30000,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body).toString(),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => {
          data += chunk;
        });
        res.on("end", () => {
          if (res.statusCode && res.statusCode >= 400) {
            reject(new Error(`AFM harvest HTTP ${res.statusCode}`));
            return;
          }
          try {
            const parsed = JSON.parse(data) as { choices?: Array<{ message?: { content?: string } }> };
            const content = parsed.choices?.[0]?.message?.content;
            resolve(typeof content === "string" ? content : "");
          } catch (error) {
            reject(error instanceof Error ? error : new Error(String(error)));
          }
        });
      },
    );
    req.on("timeout", () => {
      req.destroy(new Error("AFM harvest request timed out"));
    });
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

export async function harvestEvidence(
  input: HarvestEvidenceInput,
  deps: HarvestDeps = {},
): Promise<HarvestedLearning[]> {
  if (!input.task.trim()) throw new Error("harvest requires task.");
  if (!input.vaultPath.trim()) throw new Error("harvest requires vaultPath.");
  const afmUrl = input.afmUrl ?? AFM_PREPARE_TASK_URL;
  const afmModel = input.afmModel ?? AFM_PREPARE_TASK_MODEL;
  const maxPerAgent = Math.max(1, input.maxLearningsPerAgent ?? 3);
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
      learnings.push({
        agentId,
        inboxFilePath: "",
        slug: `harvest-${agentId}`,
        candidateText: "",
        source: "skipped",
        reason,
      });
      skipped += 1;
      continue;
    }

    const parsed = parseAfmResponse(raw);
    if (parsed.kind === "skip") {
      learnings.push({
        agentId,
        inboxFilePath: "",
        slug: `harvest-${agentId}`,
        candidateText: "",
        source: "skipped",
        reason: "AFM returned SKIP",
      });
      skipped += 1;
      continue;
    }
    if (parsed.kind === "empty") {
      learnings.push({
        agentId,
        inboxFilePath: "",
        slug: `harvest-${agentId}`,
        candidateText: "",
        source: "skipped",
        reason: "AFM returned empty response",
      });
      skipped += 1;
      continue;
    }

    const candidateText = redact(parsed.text).slice(0, 500);
    if (!candidateText) {
      learnings.push({
        agentId,
        inboxFilePath: "",
        slug: `harvest-${agentId}`,
        candidateText: "",
        source: "skipped",
        reason: "candidate empty after redaction",
      });
      skipped += 1;
      continue;
    }

    if (written >= input.reports.length * maxPerAgent) {
      learnings.push({
        agentId,
        inboxFilePath: "",
        slug: `harvest-${agentId}`,
        candidateText,
        source: "skipped",
        reason: "per-call cap reached",
      });
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
        inboxFilePath: entry.filePath,
        slug: entry.slug,
        candidateText,
        source: "afm",
      });
      written += 1;
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      learnings.push({
        agentId,
        inboxFilePath: "",
        slug: `harvest-${agentId}`,
        candidateText,
        source: "skipped",
        reason: `inbox write failed: ${reason}`,
      });
      skipped += 1;
    }
  }

  try {
    await audit(input.vaultPath, {
      tool: "sovereign_team_harvest",
      summary: input.task.slice(0, 120),
      details: {
        runtimeId: input.runtimeId,
        totalReports: input.reports.length,
        written,
        skipped,
      },
    });
  } catch {
    // best effort; harvest result still returned
  }

  return learnings;
}
