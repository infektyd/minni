// API types mirror plugins/sovereign-memory/src/{task.ts,sovereign.ts,vault.ts}.
// Kept here as a structural copy so the bundle is self-contained.

export type TaskProfile = "compact" | "standard" | "deep";
export type SourceAuthority =
  | "schema"
  | "handoff"
  | "decision"
  | "session"
  | "concept"
  | "daemon"
  | "vault";
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
  recall: { daemonOk: boolean; daemonLead?: string; error?: string };
  afm: { requested: boolean; used: boolean; url?: string; error?: string };
  outcomeDraft?: OutcomeDraft;
  contextMarkdown: string;
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
  afm: { requested: boolean; used: boolean; url?: string; error?: string };
  contextMarkdown: string;
}

export interface JsonResult<T = unknown> {
  ok: boolean;
  data?: T;
  error?: string;
}

export interface StatusReport {
  vault: { path: string; exists: boolean };
  socket: JsonResult;
  afm: JsonResult;
  audit: { entries: number; latest?: string };
}

export interface AuditTailResult {
  entries: string[];
  text: string;
}

export interface HealthReport {
  ok: boolean;
  host?: string;
  port?: number;
  tools?: string[];
  automaticLearning?: boolean;
}

export type ResearchMode = "web" | "local-docs" | "hybrid";
export type ResearchTool = "google_search" | "url_context" | "code_execution";

export interface DeepResearchPaths {
  root: string;
  cli: string;
  local_docs: string;
  runs: string;
}

export interface DeepResearchRun {
  run_id: string;
  created_at?: string;
  updated_at?: string;
  prompt?: string;
  mode?: ResearchMode | string;
  interaction_id?: string | null;
  status?: string;
  has_result?: boolean;
  has_report?: boolean;
  has_events?: boolean;
}

export interface DeepResearchRunDetail {
  metadata: DeepResearchRun;
  result: Record<string, unknown> | null;
  report: string;
  events: string[];
}

export interface DeepResearchRequest {
  prompt: string;
  mode?: ResearchMode;
  fileSearchStores?: string[];
  enabledTools?: ResearchTool[];
  documentUris?: string[];
  imageUris?: string[];
  mcpServers?: string[];
  maxMode?: boolean;
  visualization?: boolean;
}

export interface DeepResearchPlanFollowupRequest {
  previousInteractionId: string;
  prompt: string;
}

export interface DeepResearchStatusRequest {
  interactionId: string;
  runId?: string;
}

// ---- UI-side row shape derived from TaskSource ----

export type EvidenceClass = "wiki" | "raw" | "log" | "inbox" | "code" | "other";
export type EvidencePrivacy = "private" | "team" | "public";
export type EvidenceAuthority = "owner" | "team" | "system" | "public";
export type EvidenceAfm = "safe" | "learn" | "log" | "dns";

export interface EvidenceRow {
  id: string;
  score: number;
  title: string;
  path: string;
  cls: EvidenceClass;
  privacy: EvidencePrivacy;
  authority: EvidenceAuthority;
  afm: EvidenceAfm;
  reason: string;
  selected: boolean;
  private: boolean;
  ingested?: string;
  modified?: string;
  size?: string;
  locality: "local";
  hash?: string;
  collection: string;
  tags: string[];
  excerpt: string;
}

// ---- Mappers ----

export function classFromPath(relativePath: string): EvidenceClass {
  const p = relativePath || "";
  if (/^vault\/wiki\/|wiki\//.test(p)) return "wiki";
  if (/^vault\/raw\/|raw\//.test(p)) return "raw";
  if (/^vault\/logs?\/|logs?\//.test(p)) return "log";
  if (/^vault\/inbox\/|inbox\//.test(p)) return "inbox";
  if (/\.(ts|tsx|js|jsx|py|rs|go|md)$/i.test(p) && /(^|\/)(src|code|app)\//.test(p))
    return "code";
  return "other";
}

export function privacyFromLevel(level: PrivacyLevel | undefined): EvidencePrivacy {
  switch (level) {
    case "private":
    case "blocked":
      return "private";
    case "local-only":
      return "team";
    case "safe":
    default:
      return "public";
  }
}

export function authorityFromSource(value: SourceAuthority | undefined): EvidenceAuthority {
  switch (value) {
    case "handoff":
    case "decision":
    case "schema":
      return "owner";
    case "session":
    case "concept":
      return "team";
    case "daemon":
    case "vault":
      return "system";
    default:
      return "public";
  }
}

function shortIdFromPath(p: string): string {
  let h = 0;
  for (let i = 0; i < p.length; i++) h = (h * 31 + p.charCodeAt(i)) >>> 0;
  return "src_" + h.toString(16).padStart(8, "0").slice(0, 6);
}

// AFM classification per source: a source is "dns" if its relativePath
// matches an entry in outcomeDraft.doNotStore; "log" if in logOnly or
// expires; "learn" if in learnCandidates; otherwise "safe".
export function afmForSource(
  src: TaskSource,
  outcome: OutcomeDraft | undefined,
): EvidenceAfm {
  if (src.privacyLevel === "blocked") return "dns";
  if (!outcome) return "safe";
  const haystacks: { kind: EvidenceAfm; list: string[] }[] = [
    { kind: "dns", list: outcome.doNotStore || [] },
    { kind: "log", list: [...(outcome.logOnly || []), ...(outcome.expires || [])] },
    { kind: "learn", list: outcome.learnCandidates || [] },
  ];
  for (const h of haystacks) {
    if (h.list.some((entry) => entry.includes(src.relativePath) || entry.includes(src.title))) {
      return h.kind;
    }
  }
  return "safe";
}

export function evidenceFromSource(
  src: TaskSource,
  outcome: OutcomeDraft | undefined,
): EvidenceRow {
  const cls = classFromPath(src.relativePath);
  const privacy = privacyFromLevel(src.privacyLevel);
  const authority = authorityFromSource(src.authority);
  const afm = afmForSource(src, outcome);
  return {
    id: shortIdFromPath(src.relativePath || src.title),
    score: typeof src.score === "number" ? src.score : 0,
    title: src.title,
    path: src.relativePath,
    cls,
    privacy,
    authority,
    afm,
    reason: (src.reasons || []).join(" · ") || src.snippet?.slice(0, 80) || "",
    selected: afm !== "dns",
    private: privacy === "private",
    locality: "local",
    collection: cls,
    tags: [],
    excerpt: src.snippet || "",
  };
}

// ---- Fetch wrappers (same-origin; ui-server enforces auth) ----

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return (await res.json()) as T;
}

export function getHealth(): Promise<HealthReport> {
  return jsonFetch<HealthReport>("/api/health");
}

export function getStatus(): Promise<StatusReport> {
  return jsonFetch<StatusReport>("/api/status");
}

export function getAuditTail(limit = 20): Promise<AuditTailResult> {
  const safeLimit = Math.max(1, Math.min(100, Math.floor(limit) || 20));
  return jsonFetch<AuditTailResult>(`/api/audit-tail?limit=${safeLimit}`);
}

export interface PrepareTaskRequest {
  task: string;
  profile?: TaskProfile;
  budgetTokens?: number;
  useAfm?: boolean;
  layer?: "identity" | "episodic" | "knowledge" | "artifact";
  limit?: number;
  workspaceId?: string;
  agentId?: string;
  includeVault?: boolean;
}

export function prepareTask(req: PrepareTaskRequest): Promise<PreparedTaskPacket> {
  return jsonFetch<PreparedTaskPacket>("/api/prepare-task", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export interface PrepareOutcomeRequest {
  task: string;
  summary: string;
  changedFiles?: string[];
  verification?: string[];
  profile?: TaskProfile;
  useAfm?: boolean;
}

export function prepareOutcome(
  req: PrepareOutcomeRequest,
): Promise<PreparedOutcomePacket> {
  return jsonFetch<PreparedOutcomePacket>("/api/prepare-outcome", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function getDeepResearchPaths(): Promise<DeepResearchPaths> {
  return jsonFetch<DeepResearchPaths>("/api/deep-research/paths");
}

export function listDeepResearchRuns(): Promise<DeepResearchRun[]> {
  return jsonFetch<DeepResearchRun[]>("/api/deep-research/runs");
}

export function getDeepResearchRun(runId: string): Promise<DeepResearchRunDetail> {
  return jsonFetch<DeepResearchRunDetail>(`/api/deep-research/runs/${encodeURIComponent(runId)}`);
}

export function getLocalDocsManifest(): Promise<unknown> {
  return jsonFetch<unknown>("/api/deep-research/local-docs-manifest");
}

export function listFileStores(): Promise<unknown> {
  return jsonFetch<unknown>("/api/deep-research/file-stores");
}

export function createFileStore(displayName: string): Promise<unknown> {
  return jsonFetch<unknown>("/api/deep-research/create-file-store", {
    method: "POST",
    body: JSON.stringify({ displayName }),
  });
}

export function deleteFileStore(name: string): Promise<unknown> {
  return jsonFetch<unknown>("/api/deep-research/delete-file-store", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function createResearchPlan(req: DeepResearchRequest): Promise<Record<string, unknown>> {
  return jsonFetch<Record<string, unknown>>("/api/deep-research/plan", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function refineResearchPlan(req: DeepResearchPlanFollowupRequest): Promise<Record<string, unknown>> {
  return jsonFetch<Record<string, unknown>>("/api/deep-research/refine-plan", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function approveResearchPlan(req: DeepResearchPlanFollowupRequest): Promise<Record<string, unknown>> {
  return jsonFetch<Record<string, unknown>>("/api/deep-research/approve-plan", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function startResearchRun(req: DeepResearchRequest): Promise<Record<string, unknown>> {
  return jsonFetch<Record<string, unknown>>("/api/deep-research/run", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function refreshResearchStatus(req: DeepResearchStatusRequest): Promise<Record<string, unknown>> {
  return jsonFetch<Record<string, unknown>>("/api/deep-research/status", {
    method: "POST",
    body: JSON.stringify(req),
  });
}
