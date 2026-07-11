// ============================================================================
// Minni Memory Board — data seam
//
// This module is the SINGLE seam between the board UI and its data. Board types
// and pure mappers live here; hooks fetch live daemon/vault data and map into
// these shapes. Fail-loud: never invent sample rows — empty live data is valid
// and distinct from error (hooks surface error → OFFLINE zone UI).
// ============================================================================

import type { AgentApiRow, HandoffRow, PolicyReport, RecallStateResponse } from "../api";

export type ZoneId = "agents" | "hub" | "staged" | "logs" | "quarantine" | "recall";
export type ZoneStatus = "online" | "pending" | "private" | "danger";

/** One live-traffic ticker event, emitted by the flow pulse layer. */
export interface FlowEvent {
  n: number;
  label: string;
  color: string;
}

/**
 * Daemon summary that feeds the hub zone + chrome chips. BoardScreen derives
 * this entirely from real `/api/status` + `/api/health`; absent API fields must
 * be rendered as "—", never borrowed from sample data.
 */
export interface DaemonInfo {
  online: boolean;
  version: string;
  uptime: string;
  storeLine: string;
  doctorLine: string;
  socket: string;
  mode: string;
  vaultPath: string;
  vaultExists: string;
  auditEntries: string;
  afmHealth: string;
  bridge: string;
  tools: number;
  automaticLearning: boolean;
}

export interface ZoneDef {
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
  title: string;
  status: ZoneStatus;
}

export interface AgentCaps {
  R: 0 | 1;
  L: 0 | 1;
  H: 0 | 1;
}

export interface BoardAgent {
  id: string;
  on: boolean;
  vault: string;
  seen: string;
  caps: AgentCaps;
  /** null when staged count is unknown (daemon RPC failed) — UI shows "—". */
  staged: number | null;
  stagedUnknown?: boolean;
  /** true when staged is a floor (server query hit its limit). */
  stagedAtLimit?: boolean;
  note?: string;
}

export interface BoardLearning {
  id: string;
  agent: string;
  score?: number | string;
  title: string;
  src: string;
  age: string;
  /** Chronological rank for the "recency" sort — lower = newer. */
  order: number;
  tag?: string;
}

export interface BoardLog {
  id: string;
  agent: string;
  score: number;
  title: string;
  age: string;
}

export interface BoardDeny {
  id: string;
  agent: string;
  score: number;
  title: string;
  body: string;
  src: string;
  ingested: string;
  hash: string;
  risk: string;
}

export interface BoardRecallResult {
  score: number;
  path: string;
  sub: string;
  cls: string;
  priv: string;
  auth: string;
  age: string;
  /** AFM verdict when known; "—" when the payload carries no AFM field (never invent SAFE). */
  afm: "SAFE" | "DEFUSED" | "—";
  body: string;
}

// ── Candidate / API row shapes ──────────────────────────────────────────────

export interface CandidateRow {
  candidate_id: string | number;
  principal: string;
  content: string;
  proposed_at: number | string;
  evidence_refs?: string[] | string;
  derived_from?: Record<string, unknown> | string;
  status?: string;
  resolution_reason?: string;
  reason?: string;
}

export type AgentRow = AgentApiRow;

export interface RecallStateHit {
  title?: string;
  wikilink?: string;
  score?: number;
}

export type RecallStatePayload = RecallStateResponse;

/**
 * Humanizes age from proposed_at timestamp (e.g. "4h", "2d")
 */
export function humanizeAge(proposedAt: number | string): string {
  try {
    // Handle unix SECONDS (daemon) vs ISO string
    // DEFECT 2: proposed_at is unix seconds, needs ×1000 for Date ms
    let ms: number;
    if (typeof proposedAt === "number") {
      // Assume seconds if small enough; ms if already ms-scale
      ms = proposedAt < 1e12 ? proposedAt * 1000 : proposedAt;
    } else if (typeof proposedAt === "string") {
      // Try ISO string first, then seconds
      const date = new Date(proposedAt);
      if (!isNaN(date.getTime())) {
        ms = date.getTime();
      } else {
        // Try parsing as seconds
        const secs = parseFloat(proposedAt);
        if (!isNaN(secs) && secs > 0) {
          ms = secs < 1e12 ? secs * 1000 : secs;
        } else {
          return "—";
        }
      }
    } else {
      return "—";
    }

    const now = Date.now();
    const diffMs = now - ms;

    if (isNaN(diffMs) || diffMs < 0) {
      return "—";
    }

    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 60) {
      return `${Math.max(1, diffMins)}m`;
    }

    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) {
      return `${diffHours}h`;
    }

    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d`;
  } catch {
    return "—";
  }
}

function firstEvidenceSrc(row: CandidateRow): string {
  if (row.evidence_refs) {
    if (Array.isArray(row.evidence_refs)) {
      return row.evidence_refs[0] || "—";
    }
    if (typeof row.evidence_refs === "string") {
      return row.evidence_refs;
    }
  }
  if (row.derived_from) {
    if (typeof row.derived_from === "string") {
      return row.derived_from;
    }
    if (typeof row.derived_from === "object" && row.derived_from !== null) {
      const obj = row.derived_from as Record<string, unknown>;
      const inbox = obj.inbox_file;
      const source = obj.source;
      if (typeof inbox === "string") return inbox;
      if (typeof source === "string") return source;
      const first = Object.values(obj).find((v) => typeof v === "string");
      if (typeof first === "string") return first;
    }
  }
  return "—";
}

/**
 * Maps a single daemon row to the BoardLearning representation
 */
export function mapCandidateToBoardLearning(
  row: CandidateRow,
  index: number,
): BoardLearning {
  const content = row.content || "";
  const firstLine = content.split(/\r?\n/)[0] || "";
  const title = firstLine.trim();

  return {
    id: "C-" + row.candidate_id,
    agent: row.principal || "—",
    score: "—",
    title: title || "—",
    src: firstEvidenceSrc(row),
    age: humanizeAge(row.proposed_at),
    order: index,
  };
}

/**
 * Sorts candidates DESC by proposed_at and maps them
 */
export function mapCandidates(rows: CandidateRow[]): BoardLearning[] {
  const sorted = [...rows].sort((a, b) => {
    const timeA =
      typeof a.proposed_at === "number"
        ? a.proposed_at < 1e12
          ? a.proposed_at * 1000
          : a.proposed_at
        : new Date(a.proposed_at).getTime();
    const timeB =
      typeof b.proposed_at === "number"
        ? b.proposed_at < 1e12
          ? b.proposed_at * 1000
          : b.proposed_at
        : new Date(b.proposed_at).getTime();
    return timeB - timeA;
  });

  return sorted.map((row, index) => mapCandidateToBoardLearning(row, index));
}

/** Map log_only candidate rows → BoardLog. */
export function mapCandidateToBoardLog(row: CandidateRow, _index: number): BoardLog {
  const content = row.content || "";
  const title = (content.split(/\r?\n/)[0] || "").trim() || "—";
  return {
    id: "C-" + row.candidate_id,
    agent: row.principal || "—",
    score: 0,
    title,
    age: humanizeAge(row.proposed_at),
  };
}

export function mapLogOnlyCandidates(rows: CandidateRow[]): BoardLog[] {
  // Sort via mapCandidates order, then re-map through mapCandidateToBoardLog.
  const byId = new Map(rows.map((r) => [String(r.candidate_id), r] as const));
  return mapCandidates(rows).map((l) => {
    const cid = l.id.startsWith("C-") ? l.id.slice(2) : l.id;
    const row = byId.get(cid);
    return row
      ? mapCandidateToBoardLog(row, l.order)
      : { id: l.id, agent: l.agent, score: 0, title: l.title, age: l.age };
  });
}

/** Map do_not_store candidate rows → BoardDeny. */
export function mapCandidateToBoardDeny(row: CandidateRow): BoardDeny {
  const content = row.content || "";
  const title = (content.split(/\r?\n/)[0] || "").trim() || "—";
  const body = content.trim() || title;
  const reason = row.resolution_reason || row.reason || "";
  return {
    id: "C-" + row.candidate_id,
    agent: row.principal || "—",
    score: 0,
    title: title.length > 80 ? title.slice(0, 77) + "…" : title,
    body,
    src: firstEvidenceSrc(row),
    ingested: humanizeAge(row.proposed_at),
    hash: "—",
    risk: reason || "Quarantined (do_not_store). Citable as evidence; never framed as instruction.",
  };
}

export function mapQuarantineCandidates(rows: CandidateRow[]): BoardDeny[] {
  const sorted = mapCandidates(rows); // reuse sort by proposed_at via candidate map
  // mapCandidates loses body; re-map from original rows in same order
  const byId = new Map(rows.map((r) => ["C-" + r.candidate_id, r] as const));
  return sorted.map((l) => {
    const row = byId.get(l.id);
    return row
      ? mapCandidateToBoardDeny(row)
      : {
          id: l.id,
          agent: l.agent,
          score: 0,
          title: l.title,
          body: l.title,
          src: l.src,
          ingested: l.age,
          hash: "—",
          risk: "Quarantined (do_not_store).",
        };
  });
}

/** Map /api/agents row → BoardAgent. */
export function mapAgentRow(row: AgentRow): BoardAgent {
  const capsIn = row.caps || { R: 0, L: 0, H: 0 };
  const stagedUnknown =
    row.stagedUnknown === true || row.staged === null || row.staged === undefined;
  return {
    id: row.id || "—",
    on: Boolean(row.on),
    vault: row.vault || row.vaultPath || "—",
    seen: row.seen || "—",
    caps: {
      R: capsIn.R ? 1 : 0,
      L: capsIn.L ? 1 : 0,
      H: capsIn.H ? 1 : 0,
    },
    staged: stagedUnknown ? null : (row.staged as number),
    stagedUnknown,
    stagedAtLimit: row.stagedAtLimit === true,
    note: stagedUnknown
      ? row.note
        ? `${row.note}; staged unknown`
        : "staged count unavailable"
      : row.note,
  };
}

export function mapAgents(rows: AgentRow[]): BoardAgent[] {
  return rows.map(mapAgentRow);
}

/** Map recall-state.json hits → BoardRecallResult list. */
export function mapRecallState(payload: RecallStatePayload | null | undefined): {
  results: BoardRecallResult[];
  query: string;
  present: boolean;
  message: string;
} {
  if (!payload || !payload.present || !payload.state) {
    return {
      results: [],
      query: "",
      present: false,
      message: payload?.message || "no recent recall",
    };
  }
  const hits = Array.isArray(payload.state.top_hits) ? payload.state.top_hits : [];
  const results: BoardRecallResult[] = hits.map((h) => {
    const wikilink = h.wikilink || "";
    const path = wikilink.replace(/^\[\[/, "").replace(/\]\]$/, "") || "—";
    const score = typeof h.score === "number" ? h.score : 0;
    // recall-state.json has no AFM field — never invent SAFE.
    return {
      score,
      path,
      sub: h.title || path.split("/").pop() || "—",
      cls: path.includes("wiki") ? "WIKI" : path.includes("log") ? "LOG" : "MEMORY",
      priv: "—",
      auth: "—",
      age: payload.state?.ts ? humanizeAge(payload.state.ts) : "—",
      afm: "—",
      body: h.title || path,
    };
  });
  return {
    results,
    query: payload.state.intent || payload.state.task_signature || "",
    present: true,
    message: results.length === 0 ? "no recent recall hits" : "",
  };
}

// ── world geometry — 1880×1092 board space ──────────────────────────────────
export const WORLD = { w: 1880, h: 1092 } as const;

/** Static geometry only. Labels/status are overridden live by BoardScreen. */
export const BOARD_ZONES: Record<ZoneId, ZoneDef> = {
  agents: {
    x: 40,
    y: 240,
    w: 280,
    h: 660,
    label: "RUNTIMES",
    title: "Runtimes",
    status: "online",
  },
  hub: {
    x: 380,
    y: 330,
    w: 260,
    h: 220,
    label: "DAEMON",
    title: "Daemon · minnid",
    status: "online",
  },
  staged: {
    x: 700,
    y: 210,
    w: 620,
    h: 470,
    label: "STAGED · LEARN CANDIDATES",
    title: "Staged learnings",
    status: "pending",
  },
  logs: {
    x: 700,
    y: 716,
    w: 620,
    h: 150,
    label: "LOG-ONLY · PERSONAL",
    title: "Log-only",
    status: "private",
  },
  quarantine: {
    x: 986,
    y: 900,
    w: 334,
    h: 150,
    label: "QUARANTINE · DO-NOT-STORE",
    title: "Quarantine",
    status: "danger",
  },
  recall: {
    x: 1390,
    y: 80,
    w: 470,
    h: 420,
    label: "RECALL · LAST QUERY",
    title: "Recall",
    status: "private",
  },
};

export const BOARD_ORDER: ZoneId[] = ["agents", "hub", "staged", "logs", "quarantine", "recall"];

// Agent accent colors resolve to console theme tokens so they flip with the
// active console theme (paper/phosphor) rather than carrying their own palette.
export const AGENT_COLORS: Record<string, string> = {
  "claude-code": "var(--bd-ac-claude)",
  codex: "var(--bd-ac-codex)",
  gemini: "var(--bd-ac-gemini)",
  antigravity: "var(--bd-ac-anti)",
  grok: "var(--bd-ac-grok)",
};

export function agentColor(id: string): string {
  return AGENT_COLORS[id] || "var(--border-strong)";
}

// ── live traffic flows (derived from audit-tail in BoardScreen) ─────────────
export interface FlowStep {
  l: string;
  rev?: boolean;
}
export interface BoardFlow {
  steps: FlowStep[];
  color: string;
  label: string;
}

/**
 * Align detail/overview: offline/loading when !isLive; empty only when live.
 * Pure (no React) so node:test can pin the gate without a component mount.
 */
export function zoneGate(
  state: { isLive: boolean; loading: boolean; error: string | null } | undefined,
  _label?: string,
): "loading" | "offline" | "ready" {
  if (!state) return "ready";
  if (state.isLive) return "ready";
  if (state.loading && !state.error) return "loading";
  return "offline";
}

/**
 * Zone label helper (tri-state). Empty live is still live.
 * - live → `BASE · count` (or base alone)
 * - loading && !error → `BASE · …` (not OFFLINE flash)
 * - offline / error → `BASE · OFFLINE`
 */
export function zoneLabel(
  base: string,
  opts: {
    isLive: boolean;
    count?: number;
    offline?: boolean;
    loading?: boolean;
    error?: string | null;
  },
): string {
  if (opts.isLive) {
    if (typeof opts.count === "number") return `${base} · ${opts.count}`;
    return base;
  }
  // Initial fetch or in-flight: never flash OFFLINE while still loading.
  if (opts.loading && !opts.error) return `${base} · …`;
  if (opts.offline || opts.error || !opts.isLive) return `${base} · OFFLINE`;
  return base;
}

// ── Zone-fetch result transitions (pure; unit-tested without React) ─────────

export type ZoneFetchResult<T> =
  | { kind: "live"; data: T; error: null }
  | { kind: "error"; data: T; error: string; authRequired: boolean };

export function zoneFetchSuccess<T>(data: T): ZoneFetchResult<T> {
  return { kind: "live", data, error: null };
}

/**
 * Fail-loud transition after a rejected fetch. `authRequired` is true when the
 * error name/message indicates AuthRequiredError (avoids importing the class
 * into pure board data).
 */
export function zoneFetchFailure<T>(
  empty: T,
  err: unknown,
  isAuthRequired?: (e: unknown) => boolean,
): ZoneFetchResult<T> {
  const auth =
    typeof isAuthRequired === "function"
      ? isAuthRequired(err)
      : err instanceof Error && err.name === "AuthRequiredError";
  let message = "unknown error";
  if (err instanceof Error) message = err.message || String(err);
  else message = String(err);
  if (auth) message = "Enter console token";
  return {
    kind: "error",
    data: empty,
    error: message,
    authRequired: auth,
  };
}
