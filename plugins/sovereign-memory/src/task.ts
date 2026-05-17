import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { URL } from "node:url";
import {
  AFM_PREPARE_TASK_MODEL,
  AFM_PREPARE_TASK_URL,
  DEFAULT_AGENT_ID,
  DEFAULT_VAULT_PATH,
  DEFAULT_WORKSPACE_ID,
} from "./config.js";
import { recallMemory } from "./sovereign.js";
import type { JsonResult, RecallResponse } from "./sovereign.js";
import { recordAudit, searchVaultNotes } from "./vault.js";
import type { VaultSearchResult } from "./vault.js";

export type TaskProfile = "compact" | "standard" | "deep";
export type SourceAuthority = "schema" | "handoff" | "decision" | "session" | "concept" | "daemon" | "vault";
export type SourceFreshness = "fresh" | "recent" | "old" | "unknown";
export type PrivacyLevel = "safe" | "local-only" | "private" | "blocked";

export interface BudgetPolicy {
  profile: TaskProfile;
  tokens: number;
  sourceLimit: number;
  snippetLength: number;
  afmSourceLimit: number;
  afmSnippetLength: number;
  afmMaxTokens: number;
}

export interface SourceScoreBreakdown {
  lexical: number;
  authority: number;
  freshness: number;
  privacy: number;
  total: number;
}

export interface TaskSource {
  title: string;
  wikilink: string;
  relativePath: string;
  snippet: string;
  score: number;
  authority?: SourceAuthority;
  freshness?: SourceFreshness;
  privacyLevel?: PrivacyLevel;
  reasons?: string[];
  scoreBreakdown?: SourceScoreBreakdown;
}

export interface OutcomeDraft {
  learnCandidates: string[];
  logOnly: string[];
  expires: string[];
  doNotStore: string[];
}

export interface PreparedTaskPacket {
  task: string;
  budgetTokens: number;
  profile: TaskProfile;
  budget: BudgetPolicy;
  mode: "deterministic" | "afm";
  intent: string;
  brief: string;
  constraints: string[];
  currentState: string[];
  relevantSources: TaskSource[];
  recommendedNextActions: string[];
  risks: string[];
  recall: {
    daemonOk: boolean;
    daemonLead?: string;
    error?: string;
  };
  afm: {
    requested: boolean;
    used: boolean;
    url?: string;
    error?: string;
  };
  outcomeDraft?: OutcomeDraft;
  contextMarkdown: string;
}

export interface PrepareTaskInput {
  task: string;
  profile?: TaskProfile;
  budgetTokens?: number;
  useAfm?: boolean;
  layer?: "identity" | "episodic" | "knowledge" | "artifact";
  limit?: number;
  workspaceId?: string;
  agentId?: string;
  vaultPath?: string;
  includeVault?: boolean;
  afmPrepareUrl?: string;
  afmModel?: string;
}

export interface PrepareOutcomeInput {
  task: string;
  summary: string;
  changedFiles?: string[];
  verification?: string[];
  profile?: TaskProfile;
  useAfm?: boolean;
  vaultPath?: string;
  afmPrepareUrl?: string;
  afmModel?: string;
}

export interface PreparedOutcomePacket {
  task: string;
  summary: string;
  profile: TaskProfile;
  budget: BudgetPolicy;
  mode: "deterministic" | "afm";
  changedFiles: string[];
  verification: string[];
  outcomeDraft: OutcomeDraft;
  afm: {
    requested: boolean;
    used: boolean;
    url?: string;
    error?: string;
  };
  contextMarkdown: string;
}

export interface PrepareTaskDeps {
  searchVault?: typeof searchVaultNotes;
  recall?: typeof recallMemory;
  afmPrepare?: (url: string, payload: Record<string, unknown>) => Promise<JsonResult<Partial<PreparedTaskPacket>>>;
  audit?: typeof recordAudit;
}

export interface PrepareOutcomeDeps {
  afmPrepare?: (url: string, payload: Record<string, unknown>) => Promise<JsonResult<Partial<PreparedOutcomePacket>>>;
}

const PROFILE_POLICIES: Record<TaskProfile, BudgetPolicy> = {
  compact: {
    profile: "compact",
    tokens: 1500,
    sourceLimit: 3,
    snippetLength: 160,
    afmSourceLimit: 2,
    afmSnippetLength: 120,
    afmMaxTokens: 140,
  },
  standard: {
    profile: "standard",
    tokens: 4000,
    sourceLimit: 6,
    snippetLength: 280,
    afmSourceLimit: 4,
    afmSnippetLength: 220,
    afmMaxTokens: 220,
  },
  deep: {
    profile: "deep",
    tokens: 12000,
    sourceLimit: 10,
    snippetLength: 520,
    afmSourceLimit: 6,
    afmSnippetLength: 320,
    afmMaxTokens: 420,
  },
};

function resolveProfile(profile: TaskProfile | undefined): TaskProfile {
  return profile && profile in PROFILE_POLICIES ? profile : "standard";
}

function clampBudgetTokens(value: number | undefined, profile: TaskProfile): number {
  if (!Number.isFinite(value ?? NaN)) return PROFILE_POLICIES[profile].tokens;
  return Math.max(1000, Math.min(32000, Math.trunc(value ?? 4000)));
}

function resolveBudget(profileInput: TaskProfile | undefined, budgetTokens: number | undefined): BudgetPolicy {
  const profile = resolveProfile(profileInput);
  return {
    ...PROFILE_POLICIES[profile],
    tokens: clampBudgetTokens(budgetTokens, profile),
  };
}

function classifyIntent(task: string): string {
  const text = task.toLowerCase();
  if (/review|audit|risk/.test(text)) return "review";
  if (/debug|fix|broken|failing|error/.test(text)) return "debug";
  if (/test|verify|smoke/.test(text)) return "verify";
  if (/plan|design|think|upgrade|architecture/.test(text)) return "plan";
  if (/build|implement|add|create/.test(text)) return "implement";
  return "work";
}

function firstLine(text: string | unknown[] | undefined): string | undefined {
  const raw = Array.isArray(text) ? JSON.stringify(text) : text;
  return raw?.split("\n").find((line) => line.trim().length > 0)?.trim();
}

function constraintsForTask(task: string): string[] {
  const constraints = [
    "Default automatic behavior is recall-only; durable learning and vault writes must stay explicit.",
    "Keep adapter files, launchd plists, datasets, DB files, raw sessions, vault raw/log material, and local runtime files out of public git unless sanitized.",
  ];
  if (/afm|foundation|adapter|extract|training|session mining/i.test(task)) {
    constraints.push("Do not run AFM extraction, adapter training, session mining, staging review, or production extraction unless explicitly requested.");
  }
  if (/frontend|dashboard|ui/i.test(task)) {
    constraints.push("Frontend/dashboard work should wait until the plugin backend behavior is stable and verified.");
  }
  return constraints;
}

function authorityForSource(result: VaultSearchResult): SourceAuthority {
  const path = result.relativePath.toLowerCase();
  const title = result.title.toLowerCase();
  if (path.includes("schema/agents") || title.includes("agents.md") || title.includes("operating rules")) return "schema";
  if (path.includes("handoff") || title.includes("handoff")) return "handoff";
  if (path.startsWith("wiki/decisions/") || title.includes("decision")) return "decision";
  if (path.startsWith("wiki/sessions/")) return "session";
  if (path.startsWith("wiki/concepts/")) return "concept";
  return "vault";
}

function dateFromSource(result: VaultSearchResult): Date | undefined {
  const raw = `${result.relativePath} ${result.title}`.match(/\b(20\d{2})[-/]?([01]\d)[-/]?([0-3]\d)\b/);
  if (!raw) return undefined;
  const date = new Date(`${raw[1]}-${raw[2]}-${raw[3]}T00:00:00.000Z`);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

function freshnessForSource(result: VaultSearchResult): { freshness: SourceFreshness; points: number; reason?: string } {
  const date = dateFromSource(result);
  if (!date) return { freshness: "unknown", points: 0 };
  const ageDays = Math.max(0, Math.floor((Date.now() - date.getTime()) / 86_400_000));
  if (ageDays <= 30) return { freshness: "fresh", points: 24, reason: "fresh note" };
  if (ageDays <= 180) return { freshness: "recent", points: 10, reason: "recent note" };
  return { freshness: "old", points: -8, reason: "older note" };
}

function privacyForSource(result: VaultSearchResult): { privacyLevel: PrivacyLevel; points: number; reason?: string } {
  const text = `${result.relativePath}\n${result.title}\n${result.snippet}`.toLowerCase();
  if (/\b(api[_ -]?key|private key|password|secret|token)\b/.test(text)) {
    return { privacyLevel: "blocked", points: -1000, reason: "blocked sensitive content" };
  }
  if (/raw\/|\/logs?\/|\.db\b|sqlite|\.fmadapter|launchd|plist/.test(text)) {
    return { privacyLevel: "blocked", points: -1000, reason: "blocked local artifact" };
  }
  if (/private|raw session|session content/.test(text)) {
    return { privacyLevel: "private", points: -20, reason: "private source" };
  }
  if (/local runtime|local-only|\/users\/|\/volumes\/|adapter|afm bridge/.test(text)) {
    return { privacyLevel: "local-only", points: -4, reason: "local-only source" };
  }
  return { privacyLevel: "safe", points: 0, reason: "safe source" };
}

function authorityPoints(authority: SourceAuthority): number {
  if (authority === "schema") return 60;
  if (authority === "handoff") return 42;
  if (authority === "decision") return 30;
  if (authority === "session") return 10;
  if (authority === "concept") return 6;
  return 0;
}

function enhancedSourceFromVault(result: VaultSearchResult, budget: BudgetPolicy): TaskSource | undefined {
  const authority = authorityForSource(result);
  const freshness = freshnessForSource(result);
  const privacy = privacyForSource(result);
  if (privacy.privacyLevel === "blocked") return undefined;
  const authorityScore = authorityPoints(authority);
  const reasons = ["lexical match"];
  if (authority === "schema") reasons.push("hard constraint");
  if (authority === "handoff") reasons.push(freshness.freshness === "fresh" ? "fresh handoff" : "handoff");
  if (authority === "decision") reasons.push("prior decision");
  if (freshness.reason && freshness.freshness !== "old") reasons.push(freshness.reason);
  if (privacy.reason && privacy.privacyLevel !== "safe") reasons.push(privacy.reason);
  const total = result.score + authorityScore + freshness.points + privacy.points;
  return {
    title: result.title,
    wikilink: result.wikilink,
    relativePath: result.relativePath,
    snippet: result.snippet.replace(/\s+/g, " ").slice(0, budget.snippetLength),
    score: total,
    authority,
    freshness: freshness.freshness,
    privacyLevel: privacy.privacyLevel,
    reasons,
    scoreBreakdown: {
      lexical: result.score,
      authority: authorityScore,
      freshness: freshness.points,
      privacy: privacy.points,
      total,
    },
  };
}

function taskSourcesFromVault(results: VaultSearchResult[], budget: BudgetPolicy): TaskSource[] {
  return results
    .map((result) => enhancedSourceFromVault(result, budget))
    .filter((source): source is TaskSource => Boolean(source))
    .sort((a, b) => b.score - a.score || a.relativePath.localeCompare(b.relativePath))
    .slice(0, budget.sourceLimit);
}

function sourceFromVault(result: VaultSearchResult): TaskSource {
  return {
    title: result.title,
    wikilink: result.wikilink,
    relativePath: result.relativePath,
    snippet: result.snippet.replace(/\s+/g, " "),
    score: result.score,
  };
}

function deterministicPacket(input: {
  task: string;
  budget: BudgetPolicy;
  budgetTokens: number;
  vaultResults: VaultSearchResult[];
  recallResult: JsonResult<RecallResponse>;
  afmRequested: boolean;
  afmUrl: string;
  afmError?: string;
}): PreparedTaskPacket {
  const intent = classifyIntent(input.task);
  const relevantSources = taskSourcesFromVault(input.vaultResults, input.budget);
  const daemonLead = input.recallResult.ok ? firstLine(input.recallResult.data?.results) : undefined;
  const constraints = constraintsForTask(input.task);
  const currentState = [
    relevantSources.length > 0
      ? `Vault context available from ${relevantSources.length} ranked note${relevantSources.length === 1 ? "" : "s"}.`
      : "No matching Codex vault notes were found for this task.",
    input.recallResult.ok ? "Daemon recall responded." : `Daemon recall unavailable: ${input.recallResult.error ?? "unknown error"}.`,
  ];
  if (daemonLead) currentState.push(`Daemon lead: ${daemonLead}`);
  const recommendedNextActions = [
    "Read the highest-ranked source notes before editing.",
    "Make the narrowest code change that satisfies the task packet.",
    "Run focused tests first, then the plugin build/test suite before handoff.",
  ];
  const risks = [
    "Older broad semantic recall can outrank fresher Codex vault notes unless source ranking is explicit.",
    "Private local memory material can accidentally leak if public-safety scans are skipped.",
  ];
  const brief = [
    `Intent: ${intent}.`,
    relevantSources[0] ? `Top source: ${relevantSources[0].wikilink} - ${relevantSources[0].snippet}` : "No top vault source.",
    constraints[0],
  ].join(" ");
  const contextMarkdown = [
    "# Sovereign Task Packet",
    `Task: ${input.task}`,
    `Intent: ${intent}`,
    `Budget: ${input.budgetTokens} tokens`,
    "## Brief",
    brief,
    "## Constraints",
    constraints.map((item) => `- ${item}`).join("\n"),
    "## Current State",
    currentState.map((item) => `- ${item}`).join("\n"),
    "## Relevant Sources",
    relevantSources.length === 0
      ? "- None"
      : relevantSources
          .map(
            (source) =>
              `- ${source.wikilink} (score=${source.score}; ${source.reasons?.join(", ") ?? "included"}) ${source.snippet}`,
          )
          .join("\n"),
    "## Recommended Next Actions",
    recommendedNextActions.map((item) => `- ${item}`).join("\n"),
    "## Risks",
    risks.map((item) => `- ${item}`).join("\n"),
  ].join("\n\n");

  return {
    task: input.task,
    budgetTokens: input.budgetTokens,
    profile: input.budget.profile,
    budget: input.budget,
    mode: "deterministic",
    intent,
    brief,
    constraints,
    currentState,
    relevantSources,
    recommendedNextActions,
    risks,
    recall: {
      daemonOk: input.recallResult.ok,
      daemonLead,
      error: input.recallResult.error,
    },
    afm: {
      requested: input.afmRequested,
      used: false,
      url: input.afmUrl,
      error: input.afmError,
    },
    contextMarkdown,
  };
}

function mergeAfmPacket(base: PreparedTaskPacket, afmData: Partial<PreparedTaskPacket>): PreparedTaskPacket {
  const merged = {
    ...base,
    ...afmData,
    task: base.task,
    budgetTokens: base.budgetTokens,
    profile: base.profile,
    budget: base.budget,
    mode: "afm" as const,
    intent: afmData.intent ?? base.intent,
    constraints: afmData.constraints ?? base.constraints,
    currentState: afmData.currentState ?? base.currentState,
    relevantSources: base.relevantSources,
    recommendedNextActions: afmData.recommendedNextActions ?? base.recommendedNextActions,
    risks: afmData.risks ?? base.risks,
    recall: base.recall,
    afm: {
      ...base.afm,
      requested: true,
      used: true,
      error: undefined,
    },
  };
  return {
    ...merged,
    contextMarkdown: afmData.contextMarkdown ?? base.contextMarkdown,
    brief: afmData.brief ?? base.brief,
  };
}

function extractJsonObject(raw: string): unknown | undefined {
  const trimmed = raw.trim();
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1]?.trim();
  const candidates = [trimmed, fenced].filter((candidate): candidate is string => Boolean(candidate));
  const objectMatch = trimmed.match(/\{[\s\S]*\}/)?.[0];
  if (objectMatch) candidates.push(objectMatch);
  for (const candidate of candidates) {
    try {
      return JSON.parse(candidate);
    } catch {
      // Keep trying more permissive candidates.
    }
  }
  return undefined;
}

function contentFromChatCompletion(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const choices = (data as { choices?: unknown }).choices;
  if (!Array.isArray(choices)) return undefined;
  const first = choices[0];
  if (!first || typeof first !== "object") return undefined;
  const message = (first as { message?: unknown }).message;
  if (!message || typeof message !== "object") return undefined;
  const content = (message as { content?: unknown }).content;
  return typeof content === "string" ? content : undefined;
}

function normalizeAfmResponse(data: unknown): Partial<PreparedTaskPacket> {
  const chatContent = contentFromChatCompletion(data);
  const parsed = chatContent ? extractJsonObject(chatContent) : data;
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const partial = parsed as Partial<PreparedTaskPacket> & { ok?: unknown };
    const normalized: Partial<PreparedTaskPacket> = {};
    if (typeof partial.brief === "string") normalized.brief = partial.brief;
    if (typeof partial.intent === "string") normalized.intent = partial.intent;
    if (typeof partial.contextMarkdown === "string") normalized.contextMarkdown = partial.contextMarkdown;
    if (Array.isArray(partial.constraints)) normalized.constraints = partial.constraints.filter((item) => typeof item === "string");
    if (Array.isArray(partial.currentState)) normalized.currentState = partial.currentState.filter((item) => typeof item === "string");
    if (Array.isArray(partial.recommendedNextActions)) {
      normalized.recommendedNextActions = partial.recommendedNextActions.filter((item) => typeof item === "string");
    }
    if (Array.isArray(partial.risks)) normalized.risks = partial.risks.filter((item) => typeof item === "string");
    if (partial.outcomeDraft && typeof partial.outcomeDraft === "object") {
      const draft = partial.outcomeDraft as Partial<OutcomeDraft>;
      normalized.outcomeDraft = {
        learnCandidates: Array.isArray(draft.learnCandidates) ? draft.learnCandidates.filter((item) => typeof item === "string") : [],
        logOnly: Array.isArray(draft.logOnly) ? draft.logOnly.filter((item) => typeof item === "string") : [],
        expires: Array.isArray(draft.expires) ? draft.expires.filter((item) => typeof item === "string") : [],
        doNotStore: Array.isArray(draft.doNotStore) ? draft.doNotStore.filter((item) => typeof item === "string") : [],
      };
    }
    if (Object.keys(normalized).length > 0) return normalized;
  }
  if (chatContent?.trim()) return { brief: chatContent.trim() };
  return {};
}

function sourceAllowedForAfm(source: unknown): source is TaskSource {
  if (!source || typeof source !== "object") return false;
  const privacyLevel = (source as { privacyLevel?: unknown }).privacyLevel;
  return privacyLevel === undefined || privacyLevel === "safe";
}

export function buildAfmChatPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const profile = resolveProfile(payload.profile as TaskProfile | undefined);
  const budget = resolveBudget(profile, typeof payload.budgetTokens === "number" ? payload.budgetTokens : undefined);
  const purpose = payload.purpose === "outcome" ? "outcome" : "task";
  const redactLocal = (value: unknown, maxLength: number): string =>
    String(value ?? "")
      .replace(/\/Users\/[^\s"',)]+/g, "[local-path]")
      .replace(/\/Volumes\/[^\s"',)]+/g, "[local-path]")
      .slice(0, maxLength);
  const sourceLines = Array.isArray(payload.relevantSources)
    ? payload.relevantSources
        .filter(sourceAllowedForAfm)
        .slice(0, budget.afmSourceLimit)
        .map((source) => {
          const item = source as { wikilink?: unknown; snippet?: unknown; score?: unknown; reasons?: unknown };
          const reasons = Array.isArray(item.reasons) ? item.reasons.filter((reason) => typeof reason === "string").join(", ") : "";
          return `${String(item.wikilink ?? "source")} score=${String(item.score ?? "?")} reasons=${reasons}: ${String(
            item.snippet ?? "",
          ).slice(0, budget.afmSnippetLength)}`;
        })
        .filter(Boolean)
    : [];
  const content =
    purpose === "outcome"
      ? [
          "Return compact JSON only for Codex outcome prep.",
          "Keys: outcomeDraft with learnCandidates, logOnly, expires, doNotStore.",
          "No secrets, no raw private logs, no local absolute paths.",
          `Task: ${redactLocal(payload.task, 500)}`,
          `Summary: ${redactLocal(payload.summary, 700)}`,
          `Profile: ${profile}`,
          `Changed files: ${
            Array.isArray(payload.changedFiles)
              ? payload.changedFiles.map((item) => redactLocal(item, 160)).slice(0, budget.afmSourceLimit).join(" | ")
              : ""
          }`,
          `Verification: ${
            Array.isArray(payload.verification)
              ? payload.verification.map((item) => redactLocal(item, 180)).slice(0, budget.afmSourceLimit).join(" | ")
              : ""
          }`,
          `Existing draft: ${redactLocal(JSON.stringify(payload.outcomeDraft ?? {}), 1200)}`,
        ].join("\n")
      : [
          "Return compact JSON only for Codex task prep.",
          "Keys: brief, recommendedNextActions, risks.",
          "No secrets, no raw private logs.",
          `Task: ${String(payload.task ?? "").slice(0, 500)}`,
          `Intent: ${String(payload.intent ?? "work")}`,
          `Profile: ${profile}`,
          `Budget: ${String(payload.budgetTokens ?? 4000)} tokens`,
          `Constraints: ${Array.isArray(payload.constraints) ? payload.constraints.join(" | ").slice(0, 700) : ""}`,
          `State: ${Array.isArray(payload.currentState) ? payload.currentState.join(" | ").slice(0, 700) : ""}`,
          `Sources: ${sourceLines.join(" || ").slice(0, 1200)}`,
          `Daemon: ${String(payload.daemonLead ?? "").slice(0, 400)}`,
        ].join("\n");
  return {
    model: typeof payload.model === "string" ? payload.model : AFM_PREPARE_TASK_MODEL,
    temperature: 0,
    max_tokens: budget.afmMaxTokens,
    messages: [
      {
        role: "user",
        content,
      },
    ],
  };
}

export async function callAfmPrepareTask(
  url: string,
  payload: Record<string, unknown>,
): Promise<JsonResult<Partial<PreparedTaskPacket>>> {
  return new Promise((resolve) => {
    const parsedUrl = new URL(url);
    const client = parsedUrl.protocol === "https:" ? httpsRequest : httpRequest;
    const isChatCompletions = parsedUrl.pathname.endsWith("/chat/completions");
    const body = JSON.stringify(isChatCompletions ? buildAfmChatPayload(payload) : payload);
    const req = client(
      parsedUrl,
      {
        method: "POST",
        timeout: 45000,
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
          try {
            const parsed = JSON.parse(data) as unknown;
            if (res.statusCode && res.statusCode >= 400) {
              resolve({ ok: false, data: normalizeAfmResponse(parsed), error: `HTTP ${res.statusCode}` });
              return;
            }
            resolve({ ok: true, data: normalizeAfmResponse(parsed) });
          } catch (error) {
            resolve({ ok: false, error: error instanceof Error ? error.message : String(error) });
          }
        });
      },
    );
    req.on("timeout", () => {
      req.destroy(new Error("AFM prepare_task request timed out"));
    });
    req.on("error", (error) => resolve({ ok: false, error: error.message }));
    req.write(body);
    req.end();
  });
}

export async function prepareTask(input: PrepareTaskInput, deps: PrepareTaskDeps = {}): Promise<PreparedTaskPacket> {
  const vaultPath = input.vaultPath ?? DEFAULT_VAULT_PATH;
  const agentId = input.agentId ?? DEFAULT_AGENT_ID;
  const workspaceId = input.workspaceId ?? DEFAULT_WORKSPACE_ID;
  const budget = resolveBudget(input.profile, input.budgetTokens);
  const limit = Math.max(1, Math.min(input.limit ?? budget.sourceLimit * 2, 12));
  const budgetTokens = budget.tokens;
  const afmUrl = input.afmPrepareUrl ?? AFM_PREPARE_TASK_URL;
  const search = deps.searchVault ?? searchVaultNotes;
  const recall = deps.recall ?? recallMemory;
  const afmPrepare = deps.afmPrepare ?? callAfmPrepareTask;
  const audit = deps.audit ?? recordAudit;

  const [vaultResults, recallResult] = await Promise.all([
    input.includeVault === false ? Promise.resolve([]) : search(vaultPath, input.task, limit),
    recall({
      query: input.task,
      layer: input.layer,
      limit,
      workspaceId,
      agentId,
    }),
  ]);

  let packet = deterministicPacket({
    task: input.task,
    budget,
    budgetTokens,
    vaultResults,
    recallResult,
    afmRequested: input.useAfm === true,
    afmUrl,
  });

  if (input.useAfm === true) {
    const afmResult = await afmPrepare(afmUrl, {
      task: input.task,
      budgetTokens,
      profile: budget.profile,
      intent: packet.intent,
      constraints: packet.constraints,
      currentState: packet.currentState,
      relevantSources: packet.relevantSources,
      daemonLead: packet.recall.daemonLead,
      model: input.afmModel ?? AFM_PREPARE_TASK_MODEL,
    });
    if (afmResult.ok && afmResult.data) {
      packet = mergeAfmPacket(packet, afmResult.data);
    } else {
      packet.afm.error = afmResult.error ?? "AFM prepare_task returned no data.";
    }
  }

  await audit(vaultPath, {
    tool: "sovereign_prepare_task",
    summary: input.task.slice(0, 120),
    details: {
      mode: packet.mode,
      intent: packet.intent,
      profile: packet.profile,
      budgetTokens,
      vaultMatches: packet.relevantSources.map((source) => source.relativePath),
      daemonOk: packet.recall.daemonOk,
      afm: packet.afm,
    },
  });

  return packet;
}

function outcomeDraft(input: PrepareOutcomeInput): OutcomeDraft {
  const verification = input.verification ?? [];
  const changedFiles = input.changedFiles ?? [];
  const learnCandidates = [
    `${input.task}: ${input.summary}`.replace(/\s+/g, " ").slice(0, 500),
  ];
  const logOnly = [
    ...verification.map((item) => `Verification: ${item}`),
    changedFiles.length > 0 ? `Changed files: ${changedFiles.join(", ")}` : "",
  ].filter(Boolean);
  return {
    learnCandidates,
    logOnly,
    expires: ["Implementation-specific status should be refreshed after the next backend pass."],
    doNotStore: [
      "Do not store raw logs, raw sessions, local DB contents, adapter files, launchd plists, secrets, or machine-local paths.",
    ],
  };
}

function outcomeContextMarkdown(packet: PreparedOutcomePacket): string {
  return [
    "# Sovereign Outcome Packet",
    `Task: ${packet.task}`,
    `Profile: ${packet.profile}`,
    "## Summary",
    packet.summary,
    "## Learn Candidates",
    packet.outcomeDraft.learnCandidates.map((item) => `- ${item}`).join("\n"),
    "## Log Only",
    packet.outcomeDraft.logOnly.length === 0 ? "- None" : packet.outcomeDraft.logOnly.map((item) => `- ${item}`).join("\n"),
    "## Expires",
    packet.outcomeDraft.expires.map((item) => `- ${item}`).join("\n"),
    "## Do Not Store",
    packet.outcomeDraft.doNotStore.map((item) => `- ${item}`).join("\n"),
  ].join("\n\n");
}

export interface ScarTissueEntry {
  kind: "failed_command" | "dead_end" | "rejected_hypothesis";
  signal: string;
  resolution?: string;
}

export function extractScarTissue(auditEntries: string[]): ScarTissueEntry[] {
  const scars: ScarTissueEntry[] = [];
  for (const entry of auditEntries) {
    const header = entry.match(/^## \[[^\]]+\]\s+([^|]+)\|\s+(.+)$/m);
    if (!header) continue;
    const tool = header[1].trim();
    const summary = header[2].trim();
    const lower = summary.toLowerCase();
    if (lower.includes("error") || lower.includes("failed") || lower.includes("quality-blocked")) {
      scars.push({
        kind: lower.includes("quality-blocked") ? "rejected_hypothesis" : "failed_command",
        signal: `${tool}: ${summary}`.slice(0, 220),
      });
    } else if (tool === "sovereign_recall" && /no recall results|recall failed/i.test(entry)) {
      scars.push({
        kind: "dead_end",
        signal: `recall miss: ${summary}`.slice(0, 220),
      });
    }
  }
  return scars.slice(-12);
}

export interface HandoffPacketInput {
  task: string;
  agentId?: string;
  workspaceId?: string;
  vaultPath?: string;
  openQuestions?: string[];
  inboxPointer?: string;
  scarTissue?: ScarTissueEntry[];
  limit?: number;
}

export interface HandoffPacket {
  task: string;
  agentOrigin: string;
  workspace: string;
  identity: string;
  topRecalls: TaskSource[];
  daemonOk: boolean;
  daemonLead?: string;
  scarTissue: ScarTissueEntry[];
  openQuestions: string[];
  inboxPointer?: string;
}

export async function buildHandoffPacket(
  input: HandoffPacketInput,
  deps: PrepareTaskDeps = {},
): Promise<HandoffPacket> {
  const vaultPath = input.vaultPath ?? DEFAULT_VAULT_PATH;
  const agentId = input.agentId ?? DEFAULT_AGENT_ID;
  const workspaceId = input.workspaceId ?? DEFAULT_WORKSPACE_ID;
  const limit = Math.max(1, Math.min(input.limit ?? 5, 12));
  const search = deps.searchVault ?? searchVaultNotes;
  const recall = deps.recall ?? recallMemory;
  const budget = resolveBudget("compact", undefined);

  const [vaultResults, recallResult] = await Promise.all([
    search(vaultPath, input.task, limit),
    recall({ query: input.task, limit, agentId, workspaceId }),
  ]);

  const topRecalls = taskSourcesFromVault(vaultResults, budget);
  const daemonLead = recallResult.ok ? firstLine(recallResult.data?.results) : undefined;

  return {
    task: input.task,
    agentOrigin: agentId,
    workspace: workspaceId,
    identity: `agent=${agentId} workspace=${workspaceId}`,
    topRecalls,
    daemonOk: recallResult.ok,
    daemonLead,
    scarTissue: input.scarTissue ?? [],
    openQuestions: input.openQuestions ?? [],
    inboxPointer: input.inboxPointer,
  };
}

export async function prepareOutcome(
  input: PrepareOutcomeInput,
  deps: PrepareOutcomeDeps = {},
): Promise<PreparedOutcomePacket> {
  const budget = resolveBudget(input.profile, undefined);
  const afmUrl = input.afmPrepareUrl ?? AFM_PREPARE_TASK_URL;
  const afmPrepare = deps.afmPrepare ?? callAfmPrepareTask;
  let packet: PreparedOutcomePacket = {
    task: input.task,
    summary: input.summary,
    profile: budget.profile,
    budget,
    mode: "deterministic",
    changedFiles: input.changedFiles ?? [],
    verification: input.verification ?? [],
    outcomeDraft: outcomeDraft(input),
    afm: {
      requested: input.useAfm === true,
      used: false,
      url: afmUrl,
    },
    contextMarkdown: "",
  };
  packet.contextMarkdown = outcomeContextMarkdown(packet);

  if (input.useAfm === true) {
    const afmResult = await afmPrepare(afmUrl, {
      task: input.task,
      summary: input.summary,
      purpose: "outcome",
      changedFiles: packet.changedFiles.slice(0, budget.afmSourceLimit),
      verification: packet.verification.slice(0, budget.afmSourceLimit),
      outcomeDraft: packet.outcomeDraft,
      profile: budget.profile,
      budgetTokens: budget.tokens,
      model: input.afmModel ?? AFM_PREPARE_TASK_MODEL,
    });
    if (afmResult.ok && afmResult.data) {
      const data = afmResult.data as Partial<PreparedOutcomePacket>;
      packet = {
        ...packet,
        mode: "afm",
        outcomeDraft: data.outcomeDraft ?? packet.outcomeDraft,
        afm: {
          ...packet.afm,
          used: true,
          error: undefined,
        },
      };
      packet.contextMarkdown = outcomeContextMarkdown(packet);
    } else {
      packet.afm.error = afmResult.error ?? "AFM prepare_outcome returned no data.";
    }
  }

  return packet;
}
