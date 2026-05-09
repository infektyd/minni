import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

export const DEFAULT_LOOKBACK_DAYS = 14;
export const DEFAULT_MIN_REPEATS = 3;
export const MAX_SUGGESTIONS = 20;
const MAX_EXAMPLES_PER_SIGNATURE = 3;

export interface RepeatedAgentSuggestion {
  signature: string;
  role: string;
  normalizedFocus: string;
  count: number;
  examples: Array<{
    runtimeId: string;
    timestamp: string;
    agentId: string;
    rawFocus: string;
  }>;
  suggestPromotion: boolean;
}

export interface FindRepeatedAgentsInput {
  vaultPath: string;
  lookbackDays?: number;
  minRepeats?: number;
  now?: Date;
}

export interface RepeatedAgentDeps {
  readAuditLogPaths?: (vaultPath: string) => Promise<string[]>;
  readAuditFile?: (filePath: string) => Promise<string>;
}

interface ParsedAuditEntry {
  timestamp: Date;
  tool: string;
  details: Record<string, unknown> | null;
}

interface AgentObservation {
  runtimeId: string;
  timestamp: Date;
  agentId: string;
  role: string;
  focus: string;
}

// We split on `^## [` rather than using a single multi-section regex so a malformed
// JSON block in one entry cannot poison the parse of subsequent entries.
const ENTRY_HEADER = /^\[([^\]]+)\]\s+([^|]+?)\s*\|\s*(.*)$/;

function normalizeFocus(focus: string): string {
  return focus
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\.$/, "");
}

function parseAuditFile(text: string): ParsedAuditEntry[] {
  const entries: ParsedAuditEntry[] = [];
  // First section before the initial `## [` is file preamble (e.g. the `# 2026-05-08 ...` heading).
  const sections = text.split(/^## (?=\[)/m).slice(1);
  for (const section of sections) {
    const newlineIndex = section.indexOf("\n");
    const headerLine = newlineIndex === -1 ? section : section.slice(0, newlineIndex);
    const rest = newlineIndex === -1 ? "" : section.slice(newlineIndex + 1);
    const headerMatch = ENTRY_HEADER.exec(headerLine.trim());
    if (!headerMatch) continue;
    const [, isoTimestamp, tool] = headerMatch;
    const timestamp = new Date(isoTimestamp);
    if (Number.isNaN(timestamp.getTime())) continue;

    let details: Record<string, unknown> | null = null;
    const jsonMatch = /```json\n([\s\S]*?)\n```/.exec(rest);
    if (jsonMatch) {
      try {
        const parsed = JSON.parse(jsonMatch[1]);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          details = parsed as Record<string, unknown>;
        }
      } catch {
        // Single malformed entry should not invalidate the rest of the file.
      }
    }
    entries.push({ timestamp, tool: tool.trim(), details });
  }
  return entries;
}

function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

async function defaultReadAuditLogPaths(vaultPath: string): Promise<string[]> {
  const paths: string[] = [];
  const logsDir = path.join(vaultPath, "logs");
  try {
    const entries = await readdir(logsDir);
    for (const name of entries.sort()) {
      if (/^\d{4}-\d{2}-\d{2}\.md$/.test(name)) {
        paths.push(path.join(logsDir, name));
      }
    }
  } catch {
    // logs/ may not exist yet on a fresh vault; the rolling log.md still gets scanned.
  }
  paths.push(path.join(vaultPath, "log.md"));
  return paths;
}

function dateFromLogFilename(filePath: string): Date | undefined {
  const base = path.basename(filePath, ".md");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(base)) return undefined;
  const date = new Date(`${base}T00:00:00.000Z`);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

function shouldScanLogFile(filePath: string, windowStart: Date): boolean {
  // Daily files older than the window can be skipped wholesale (cheap optimization).
  // The rolling log.md (no date in filename) is always scanned because it spans the full history.
  const fileDate = dateFromLogFilename(filePath);
  if (!fileDate) return true;
  const dayEnd = new Date(fileDate.getTime() + 86400 * 1000);
  return dayEnd >= windowStart;
}

function extractAgents(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is Record<string, unknown> =>
      item !== null && typeof item === "object" && !Array.isArray(item),
  );
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim().length > 0 ? value : undefined;
}

export async function findRepeatedAgents(
  input: FindRepeatedAgentsInput,
  deps: RepeatedAgentDeps = {},
): Promise<RepeatedAgentSuggestion[]> {
  const lookbackDays = input.lookbackDays ?? DEFAULT_LOOKBACK_DAYS;
  const minRepeats = input.minRepeats ?? DEFAULT_MIN_REPEATS;
  const now = input.now ?? new Date();
  const windowStart = new Date(now.getTime() - lookbackDays * 86400 * 1000);
  const readPaths = deps.readAuditLogPaths ?? defaultReadAuditLogPaths;
  const readFileFn = deps.readAuditFile ?? ((filePath: string) => readFile(filePath, "utf8"));

  let logPaths: string[];
  try {
    logPaths = await readPaths(input.vaultPath);
  } catch {
    return [];
  }

  const observations: AgentObservation[] = [];
  for (const filePath of logPaths) {
    if (!shouldScanLogFile(filePath, windowStart)) continue;
    let text: string;
    try {
      text = await readFileFn(filePath);
    } catch {
      continue;
    }
    const entries = parseAuditFile(text);
    for (const entry of entries) {
      if (entry.tool !== "sovereign_team_runtime") continue;
      if (!entry.details) continue;
      if (entry.timestamp < windowStart) continue;
      const runtimeId = asString(entry.details.runtimeId) ?? "unknown-runtime";
      const agents = extractAgents(entry.details.agents);
      for (const agent of agents) {
        const role = asString(agent.role);
        const focus = asString(agent.focus);
        const agentId = asString(agent.agentId) ?? "unknown-agent";
        if (!role || !focus) continue;
        observations.push({
          runtimeId,
          timestamp: entry.timestamp,
          agentId,
          role,
          focus,
        });
      }
    }
  }

  const groups = new Map<string, {
    role: string;
    normalizedFocus: string;
    observations: AgentObservation[];
  }>();
  for (const obs of observations) {
    const normalizedFocus = normalizeFocus(obs.focus);
    if (!normalizedFocus) continue;
    const signature = `${obs.role}::${normalizedFocus}`;
    let group = groups.get(signature);
    if (!group) {
      group = { role: obs.role, normalizedFocus, observations: [] };
      groups.set(signature, group);
    }
    group.observations.push(obs);
  }

  const suggestions: RepeatedAgentSuggestion[] = [];
  for (const [signature, group] of groups) {
    const sorted = [...group.observations].sort(
      (a, b) => a.timestamp.getTime() - b.timestamp.getTime(),
    );
    const examples = sorted.slice(0, MAX_EXAMPLES_PER_SIGNATURE).map((obs) => ({
      runtimeId: obs.runtimeId,
      timestamp: obs.timestamp.toISOString(),
      agentId: obs.agentId,
      rawFocus: obs.focus,
    }));
    suggestions.push({
      signature,
      role: group.role,
      normalizedFocus: group.normalizedFocus,
      count: sorted.length,
      examples,
      suggestPromotion: sorted.length >= minRepeats,
    });
  }

  suggestions.sort((a, b) => {
    if (b.count !== a.count) return b.count - a.count;
    return a.signature.localeCompare(b.signature);
  });

  return suggestions.slice(0, MAX_SUGGESTIONS);
}
