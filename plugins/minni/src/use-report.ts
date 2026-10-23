// Use reports (design rev5 §4.5, slice S1b).
//
// Reads the use ledger (and, for epochs still open, the detector's
// windows.json) and reports what was surfaced and what was used:
//   - counts by outcome;
//   - used (applied + cited + cited_handle, the headline) and used_ii
//     (applied + title/content cited, the comparison) per visible EXPOSURE,
//     the before/after unit, by channel of first surfacing and by agent (main,
//     and each subagent type); the same per SURFACING, as diagnostics. Handle
//     citations (wikilink, slug, m:/d: handle, learning #id) are their own
//     column, cited_handle, and never used_ii: only the after pointer prints
//     handles;
//   - used_ii credits per 100 human prompt turns (`turn` rows whose
//     envelope_kind is `human`), so a rate cannot rise just because less was
//     shown;
//   - top-10 surfaced-never-used and used memories;
//   - the ledger-off stretches inside the window (from session_start audit rows);
//   - the provisional, unknown and unnameable shares, the title_assessable
//     share (per channel and agent, so "not cited" can be told apart from
//     "not assessable"), the features in effect, the fixed line "lower bounds;
//     silent use is invisible", and the undercount caveat.
// Phase 1 measures one principal (claude-code); every other wire appears in
// the coverage table as `unmeasured` and is never mixed into a rate. The
// `unlabeled` session stays out of every rate.
//
// Strictly read-only. The CLI runs the detector's idle close before reporting
// (finalizeIdleSessions); this module never writes.
import fs from "node:fs";
import path from "node:path";

import { flagOn } from "./flags.js";
import { DETECTOR_RELDIR, readDfSnapshot, readRegularFileSync } from "./use-detector.js";
import {
  containedDir,
  LEDGER_RELDIR,
  readSessionLedger,
  sessionKeyFrom,
  type ExposureRow,
  type JudgedRow,
  type LedgerRow,
  type SurfacedRow,
  type TurnRow,
} from "./use-ledger.js";
import { assertWriteTargetUnder, type SessionUseCounts } from "./vault.js";

export const USE_REPORT_NOTE = "lower bounds; silent use is invisible";
/** The recital rule's known blind spot (rev5 §4.7), printed with every report. */
export const USE_REPORT_CAVEAT =
  "Known undercount: a short rule applied by quoting it word for word reads as recital and is not counted; this affects both windows equally, and the used rate is a floor.";
/** The wires whose rows feed the rates (Phase 1). */
export const MEASURED_WIRES: readonly string[] = ["claude-code"];
export const DEFAULT_USE_PRINCIPAL = "claude-code";
/** `minni_status`'s fixed window. */
export const STATUS_USE_DAYS = 7;
/**
 * Named before/after windows for `use-report --window`:
 * `{"before": {"since": "<ISO>", "until": "<ISO>"}, "after": {"since": "<ISO>"}}`.
 * A missing `until` means "now".
 */
export const USE_WINDOWS_RELPATH = path.join(".runtime", "use-windows.json");

const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_DAYS = 365;
const TOP_N = 10;
const MAX_WINDOWS_FILE_BYTES = 64 * 1024;
const MAX_STATE_BYTES = 8 * 1024 * 1024;
const DAY_FILE_RE = /^(\d{4}-\d{2}-\d{2})\.jsonl$/;
/** A daily audit log larger than this is not read. */
const MAX_AUDIT_LOG_BYTES = 16 * 1024 * 1024;
/** `## [<ISO>] <tool> | <summary>`, as recordAudit writes each entry header. Bounded fields. */
const AUDIT_HEADER_RE = /^## \[([^\]\n]{1,64})\] ([^|\n]{1,200}) \|/;
const SESSION_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

export interface UseReportOptions {
  /** The last N days up to `now` (default 7 when no other range is given). */
  days?: number;
  since?: number | string | Date;
  until?: number | string | Date;
  /** A named window from `.runtime/use-windows.json`. */
  window?: "before" | "after";
  now?: number | string | Date;
  /** Restrict to one memory key. */
  memory?: string;
  principal?: string;
  /** Leave out the top-10 lists (the `minni_status` block). */
  compact?: boolean;
}

export interface UseBreakdown {
  total: number;
  visible: number;
  /** Headline: applied + cited + cited_handle. */
  used: number;
  /** The comparison: applied + title/content cited (exposures: a credited turn). */
  used_ii: number;
  /** Handle-style citations: their own bucket, never used_ii. */
  cited_handle: number;
  /** Surfacings judged dismissed; exposures with a dismissed turn. */
  dismissed: number;
  /** Visible surfacings with a standing judgment (surfacings only; 0 for exposures). */
  title_judged: number;
  /** Of those, the ones the title rule could assess: "not cited" vs "not assessable". */
  title_assessable: number;
  title_assessable_share: number | null;
  used_per_visible: number | null;
  used_ii_per_visible: number | null;
}

export interface UseSection extends UseBreakdown {
  by_outcome: Record<string, number>;
  by_channel: Record<string, UseBreakdown>;
  by_agent: { main: UseBreakdown; subagents: Record<string, UseBreakdown> };
}

export interface UseReport {
  principal: string;
  note: string;
  caveat: string;
  since: string;
  until: string;
  days?: number;
  window?: "before" | "after";
  ledger: "on" | "off";
  title_rule: "on" | "off";
  turns: { total: number; human: number };
  surfacings: UseSection;
  exposures: UseSection & { final: number; provisional: number };
  used_ii_credits: number;
  used_ii_credits_per_100_human_turns: number | null;
  provisional_share: number | null;
  unknown_share: number | null;
  /** Explicit-recall cards: keyed (one surfaced row each) plus unnameable (never dropped). */
  explicit_recall: { cards: number; keyed: number; unnameable: number; unnameable_share: number | null };
  /**
   * Stretches with the ledger off (rev5 §8.2), from the `<prefix>_session_start`
   * audit rows in the window: `details.use_ledger` is "on" or "off". A run of
   * consecutive "off" session starts is one stretch; a row without the field
   * is counted `unknown` and neither starts nor ends one.
   */
  ledger_off: {
    session_starts: number;
    off: number;
    unknown: number;
    stretches: Array<{ count: number; first: string; last: string }>;
    /** Days in the window with no daily audit log (pruned after 30 days, or no activity). */
    days_without_log: number;
  };
  features: Array<{ features: string[]; rows: number }>;
  coverage: Array<{ wire: string; rows: number; sessions: number; status: "measured" | "unmeasured" }>;
  top_unused?: Array<{ mkey: string; title: string; surfaced: number }>;
  top_used?: Array<{ mkey: string; title: string; used: number }>;
  memory?: {
    mkey: string;
    surfacings: Array<{ sid: string; ts: string; channel: string; visible: boolean; outcome: string; final: boolean }>;
    exposures: Array<{ exposure_id: string; outcome: string; credits: number[]; final: boolean }>;
  };
}

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function msOf(value: unknown): number | null {
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value.getTime() : null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? ms : null;
  }
  return null;
}

function ratio(n: number, d: number): number | null {
  return d > 0 ? Math.round((n / d) * 10_000) / 10_000 : null;
}

function emptyBreakdown(): UseBreakdown {
  return {
    total: 0,
    visible: 0,
    used: 0,
    used_ii: 0,
    cited_handle: 0,
    dismissed: 0,
    title_judged: 0,
    title_assessable: 0,
    title_assessable_share: null,
    used_per_visible: null,
    used_ii_per_visible: null,
  };
}

function emptySection(): UseSection {
  return { ...emptyBreakdown(), by_outcome: {}, by_channel: {}, by_agent: { main: emptyBreakdown(), subagents: {} } };
}

interface Item {
  channel: string;
  agentKey: string;
  subagentType?: string;
  visible: boolean;
  used: boolean;
  usedII: boolean;
  citedHandle: boolean;
  dismissed: boolean;
  /** The standing judgment's title_assessable; null when there is none (or for exposures). */
  titleAssessable: boolean | null;
  outcome: string;
}

function tally(target: UseBreakdown, item: Item): void {
  target.total += 1;
  if (!item.visible) return;
  target.visible += 1;
  if (item.used) target.used += 1;
  if (item.usedII) target.used_ii += 1;
  if (item.citedHandle) target.cited_handle += 1;
  if (item.dismissed) target.dismissed += 1;
  if (item.titleAssessable !== null) {
    target.title_judged += 1;
    if (item.titleAssessable) target.title_assessable += 1;
  }
}

function finish(target: UseBreakdown): void {
  target.used_per_visible = ratio(target.used, target.visible);
  target.used_ii_per_visible = ratio(target.used_ii, target.visible);
  target.title_assessable_share = ratio(target.title_assessable, target.title_judged);
}

function finishSection(section: UseSection): void {
  finish(section);
  for (const breakdown of Object.values(section.by_channel)) finish(breakdown);
  finish(section.by_agent.main);
  for (const breakdown of Object.values(section.by_agent.subagents)) finish(breakdown);
}

function addTo(section: UseSection, item: Item): void {
  tally(section, item);
  section.by_outcome[item.outcome] = (section.by_outcome[item.outcome] ?? 0) + 1;
  tally((section.by_channel[item.channel] ??= emptyBreakdown()), item);
  if (item.agentKey === "main") {
    tally(section.by_agent.main, item);
  } else {
    tally((section.by_agent.subagents[item.subagentType ?? "unknown"] ??= emptyBreakdown()), item);
  }
}

const isUsed = (outcome: string): boolean => outcome === "cited" || outcome === "applied" || outcome === "cited_handle";

/** Read a named window from `.runtime/use-windows.json`. Throws when it is not there. */
export function resolveUseWindow(vaultPath: string, name: "before" | "after"): { since: number; until: number | null } {
  const file = path.join(vaultPath, USE_WINDOWS_RELPATH);
  let raw: unknown;
  try {
    assertWriteTargetUnder(file, vaultPath);
    const text = readRegularFileSync(file, MAX_WINDOWS_FILE_BYTES);
    if (text === null) throw new Error("not a windows file");
    raw = JSON.parse(text) as unknown;
  } catch {
    throw new Error(`use-report: no "${name}" window: ${USE_WINDOWS_RELPATH} is missing or unreadable`);
  }
  const entry = isRecord(raw) ? raw[name] : undefined;
  const since = isRecord(entry) ? msOf(entry.since) : null;
  if (since === null) throw new Error(`use-report: no "${name}" window in ${USE_WINDOWS_RELPATH}`);
  const until = isRecord(entry) && entry.until !== undefined && entry.until !== null ? msOf(entry.until) : null;
  if (isRecord(entry) && entry.until !== undefined && entry.until !== null && until === null) {
    throw new Error(`use-report: the "${name}" window's until is not a date`);
  }
  return { since, until };
}

function resolveRange(vaultPath: string, options: UseReportOptions): { since: number; until: number; days?: number; window?: "before" | "after" } {
  const now = msOf(options.now) ?? Date.now();
  if (options.window !== undefined) {
    if (options.window !== "before" && options.window !== "after") throw new Error("use-report: --window is before or after");
    const named = resolveUseWindow(vaultPath, options.window);
    return { since: named.since, until: named.until ?? now, window: options.window };
  }
  const since = options.since !== undefined ? msOf(options.since) : null;
  const until = options.until !== undefined ? msOf(options.until) : null;
  if (options.since !== undefined && since === null) throw new Error("use-report: --since is not a date");
  if (options.until !== undefined && until === null) throw new Error("use-report: --until is not a date");
  if (since !== null || until !== null) {
    const end = until ?? now;
    return { since: since ?? end - STATUS_USE_DAYS * DAY_MS, until: end };
  }
  const days =
    typeof options.days === "number" && Number.isSafeInteger(options.days) && options.days >= 1
      ? Math.min(options.days, MAX_DAYS)
      : STATUS_USE_DAYS;
  return { since: now - days * DAY_MS, until: now, days };
}

/** One session_start audit row: its time and whether the ledger was on. */
interface SessionStartRow {
  ts: number;
  iso: string;
  ledger: "on" | "off" | "unknown";
}

/** The session_start rows of one daily audit log; tolerant of torn or hand-edited entries. */
function sessionStartRows(text: string): SessionStartRow[] {
  const rows: SessionStartRow[] = [];
  const entries = text.split(/\n(?=## \[)/);
  for (const entry of entries) {
    const header = AUDIT_HEADER_RE.exec(entry);
    if (!header) continue;
    const tool = (header[2] as string).trim();
    if (!tool.endsWith("_session_start")) continue;
    const iso = (header[1] as string).trim();
    const ts = Date.parse(iso);
    if (!Number.isFinite(ts)) continue;
    let ledger: SessionStartRow["ledger"] = "unknown";
    const open = entry.indexOf("```json\n");
    const close = open >= 0 ? entry.indexOf("\n```", open + 8) : -1;
    if (open >= 0 && close > open) {
      try {
        const details = JSON.parse(entry.slice(open + 8, close)) as unknown;
        const value = isRecord(details) ? details.use_ledger : undefined;
        if (value === "on" || value === "off") ledger = value;
      } catch {
        // An unparseable details block says nothing about the ledger.
      }
    }
    rows.push({ ts, iso, ledger });
  }
  return rows;
}

function ledgerOffStretches(vaultPath: string, since: number, until: number): UseReport["ledger_off"] {
  const out: UseReport["ledger_off"] = { session_starts: 0, off: 0, unknown: 0, stretches: [], days_without_log: 0 };
  const rows: SessionStartRow[] = [];
  const lastDay = dayOf(until);
  let cursor = Date.parse(`${dayOf(since)}T00:00:00.000Z`);
  for (let guard = 0; guard <= MAX_DAYS + 1 && dayOf(cursor) <= lastDay; guard += 1, cursor += DAY_MS) {
    const file = path.join(vaultPath, "logs", `${dayOf(cursor)}.md`);
    try {
      assertWriteTargetUnder(file, vaultPath);
      // Non-blocking and judged on the opened descriptor: a FIFO is a missing day.
      const text = readRegularFileSync(file, MAX_AUDIT_LOG_BYTES);
      if (text === null) {
        out.days_without_log += 1;
        continue;
      }
      rows.push(...sessionStartRows(text));
    } catch {
      out.days_without_log += 1;
    }
  }
  rows.sort((a, b) => a.ts - b.ts);
  let current: { count: number; first: string; last: string } | null = null;
  for (const row of rows) {
    if (row.ts < since || row.ts >= until) continue;
    out.session_starts += 1;
    if (row.ledger === "unknown") {
      out.unknown += 1;
    } else if (row.ledger === "off") {
      out.off += 1;
      if (current) {
        current.count += 1;
        current.last = row.iso;
      } else {
        current = { count: 1, first: row.iso, last: row.iso };
        out.stretches.push(current);
      }
    } else {
      current = null;
    }
  }
  return out;
}

function dayOf(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** Session keys with a day file in [sinceDay, untilDay]. */
function sessionsInRange(vaultPath: string, since: number, until: number): string[] {
  let root: string | null;
  try {
    root = containedDir(vaultPath, LEDGER_RELDIR.split(path.sep), false);
  } catch {
    return [];
  }
  if (!root) return [];
  const first = dayOf(since);
  const last = dayOf(until);
  const keys: string[] = [];
  let names: string[];
  try {
    names = fs.readdirSync(root).sort();
  } catch {
    return [];
  }
  for (const name of names) {
    if (!SESSION_KEY_RE.test(name) || name.toLowerCase() === "unlabeled") continue;
    try {
      const days = fs
        .readdirSync(path.join(root, name))
        .map((file) => DAY_FILE_RE.exec(file)?.[1])
        .filter((day): day is string => typeof day === "string");
      if (days.some((day) => day >= first && day <= last)) keys.push(name);
    } catch {
      // An unreadable session contributes nothing.
    }
  }
  return keys;
}

interface OpenExposure {
  dismissed_turns: number;
  exposure_id: string;
  mkey: string;
  first_sid: string;
  first_ts: string;
  channel: string;
  visible: boolean;
  outcome: string;
  credits: number[];
  agent_key: string;
  subagent_type?: string;
  wire: string;
  principal: string;
}

/** Epochs still open in the detector's state: provisional exposures. */
function openExposures(vaultPath: string, sessionKey: string): OpenExposure[] {
  try {
    const dir = containedDir(vaultPath, [...DETECTOR_RELDIR.split(path.sep), "sessions", sessionKey], false);
    if (!dir) return [];
    const file = path.join(dir, "windows.json");
    assertWriteTargetUnder(file, vaultPath);
    const text = readRegularFileSync(file, MAX_STATE_BYTES);
    if (text === null) return [];
    const raw = JSON.parse(text) as unknown;
    if (!isRecord(raw) || !isRecord(raw.agents)) return [];
    const out: OpenExposure[] = [];
    for (const [agentKey, agent] of Object.entries(raw.agents)) {
      if (!isRecord(agent) || !Array.isArray(agent.exposures)) continue;
      for (const exposure of agent.exposures) {
        if (!isRecord(exposure) || typeof exposure.exposure_id !== "string" || typeof exposure.mkey !== "string") continue;
        const open: OpenExposure = {
          dismissed_turns: Number.isSafeInteger(exposure.dismissed_turns) ? (exposure.dismissed_turns as number) : 0,
          exposure_id: exposure.exposure_id,
          mkey: exposure.mkey,
          first_sid: typeof exposure.first_sid === "string" ? exposure.first_sid : "",
          first_ts: typeof exposure.first_ts === "string" ? exposure.first_ts : "",
          channel: typeof exposure.channel === "string" ? exposure.channel : "unknown",
          visible: exposure.visible === true,
          outcome:
            typeof exposure.outcome === "string" ? exposure.outcome : exposure.visible === true ? "ignored" : "unknown(not_visible)",
          credits: Array.isArray(exposure.credits) ? exposure.credits.filter((n): n is number => Number.isSafeInteger(n)) : [],
          agent_key: agentKey,
          wire: typeof agent.wire === "string" ? agent.wire : "unknown",
          principal: typeof agent.principal === "string" ? agent.principal : "unknown",
        };
        if (typeof agent.subagent_type === "string") open.subagent_type = agent.subagent_type;
        out.push(open);
      }
    }
    return out;
  } catch {
    return [];
  }
}

/** The judgment that stands for each sid: the final one, else the latest. */
function standingJudgments(rows: readonly LedgerRow[]): Map<string, JudgedRow> {
  const out = new Map<string, JudgedRow>();
  for (const row of rows) {
    if (row.kind !== "judged" || typeof row.sid !== "string") continue;
    const current = out.get(row.sid);
    if (!current || (row.final === true && current.final !== true) || (row.final === current.final && row.ts >= current.ts)) {
      out.set(row.sid, row);
    }
  }
  return out;
}

function standingExposures(rows: readonly LedgerRow[]): Map<string, ExposureRow> {
  const out = new Map<string, ExposureRow>();
  for (const row of rows) {
    if (row.kind !== "exposure" || typeof row.exposure_id !== "string") continue;
    const current = out.get(row.exposure_id);
    if (!current || (row.final === true && current.final !== true) || (row.final === current.final && row.ts >= current.ts)) {
      out.set(row.exposure_id, row);
    }
  }
  return out;
}

/**
 * The use report for a time range (§4.5). Throws only on an unusable range
 * (a bad date, a named window that is not defined); otherwise a missing or
 * empty ledger is a report of zeros.
 */
export function buildUseReport(vaultPath: string, options: UseReportOptions = {}): UseReport {
  const range = resolveRange(vaultPath, options);
  const principal = typeof options.principal === "string" && options.principal.length > 0 ? options.principal : DEFAULT_USE_PRINCIPAL;
  const memory = typeof options.memory === "string" && options.memory.length > 0 ? options.memory : undefined;
  const report: UseReport = {
    principal,
    note: USE_REPORT_NOTE,
    caveat: USE_REPORT_CAVEAT,
    since: new Date(range.since).toISOString(),
    until: new Date(range.until).toISOString(),
    ledger: flagOn(vaultPath, "MINNI_USE_LEDGER") ? "on" : "off",
    title_rule: readDfSnapshot(vaultPath) ? "on" : "off",
    turns: { total: 0, human: 0 },
    surfacings: emptySection(),
    exposures: { ...emptySection(), final: 0, provisional: 0 },
    used_ii_credits: 0,
    used_ii_credits_per_100_human_turns: null,
    provisional_share: null,
    unknown_share: null,
    explicit_recall: { cards: 0, keyed: 0, unnameable: 0, unnameable_share: null },
    ledger_off: ledgerOffStretches(vaultPath, range.since, range.until),
    features: [],
    coverage: [],
  };
  if (range.days !== undefined) report.days = range.days;
  if (range.window !== undefined) report.window = range.window;
  if (memory) report.memory = { mkey: memory, surfacings: [], exposures: [] };

  const inRange = (ts: unknown): boolean => {
    const ms = msOf(ts);
    return ms !== null && ms >= range.since && ms < range.until;
  };
  const measured = (row: { wire?: unknown; principal?: unknown }): boolean =>
    typeof row.wire === "string" && MEASURED_WIRES.includes(row.wire) && row.principal === principal;

  const coverage = new Map<string, { rows: number; sessions: Set<string> }>();
  const features = new Map<string, { features: string[]; rows: number }>();
  const perMemory = new Map<string, { title: string; surfaced: number; used: number; everUsed: boolean }>();
  const memoryEntry = (mkey: string, title: string) => {
    const entry = perMemory.get(mkey) ?? { title: "", surfaced: 0, used: 0, everUsed: false };
    if (title) entry.title = title;
    perMemory.set(mkey, entry);
    return entry;
  };
  let provisionalSurfacings = 0;
  let unknownSurfacings = 0;

  for (const sessionKey of sessionsInRange(vaultPath, range.since, range.until)) {
    const { rows, refused } = readSessionLedger(vaultPath, sessionKey);
    if (refused) continue;
    const judgments = standingJudgments(rows);
    const surfacedBySid = new Map<string, SurfacedRow>();
    for (const row of rows) if (row.kind === "surfaced") surfacedBySid.set(row.sid, row);

    for (const row of rows) {
      if (!inRange(row.ts)) continue;
      const wire = typeof row.wire === "string" ? row.wire : "unknown";
      const cov = coverage.get(wire) ?? { rows: 0, sessions: new Set<string>() };
      cov.rows += 1;
      cov.sessions.add(sessionKey);
      coverage.set(wire, cov);
      if (!measured(row)) continue;
      const featureList = Array.isArray(row.features) ? [...row.features].filter((f) => typeof f === "string").sort() : [];
      const featureKey = JSON.stringify(featureList);
      const group = features.get(featureKey) ?? { features: featureList, rows: 0 };
      group.rows += 1;
      features.set(featureKey, group);

      if (row.kind === "turn") {
        const turn = row as TurnRow;
        report.turns.total += 1;
        if (turn.envelope_kind === "human") report.turns.human += 1;
        continue;
      }
      if (row.kind === "judged" && row.outcome === "unknown(unnameable)") {
        if (!memory) report.explicit_recall.unnameable += 1;
        continue;
      }
      if (row.kind !== "surfaced") continue;
      const surfaced = row as SurfacedRow;
      if (memory && surfaced.mkey !== memory) continue;
      const judgment = judgments.get(surfaced.sid);
      const outcome = judgment?.outcome ?? "pending";
      const visible = surfaced.visible === true && surfaced.delivered === true;
      const used = isUsed(outcome);
      const titleEvidence = Array.isArray(judgment?.evidence) && judgment.evidence.includes("title");
      const usedII = outcome === "applied" || (outcome === "cited" && titleEvidence);
      addTo(report.surfacings, {
        channel: surfaced.channel,
        agentKey: surfaced.agent_key,
        subagentType: surfaced.subagent_type,
        visible,
        used,
        usedII,
        // A pre-s1b-2 `cited` row resting on handle evidence alone is a handle citation.
        citedHandle: outcome === "cited_handle" || (outcome === "cited" && !titleEvidence),
        dismissed: outcome === "dismissed",
        // An unknown(*) outcome (unreached, missed, unparsed) was never assessed:
        // it stays out of the share's denominator.
        titleAssessable: judgment && !outcome.startsWith("unknown") ? judgment.title_assessable === true : null,
        outcome,
      });
      if (!judgment || judgment.final !== true) provisionalSurfacings += 1;
      if (outcome.startsWith("unknown")) unknownSurfacings += 1;
      if (surfaced.channel === "explicit_recall") report.explicit_recall.keyed += 1;
      if (visible) {
        const entry = memoryEntry(surfaced.mkey, surfaced.title);
        entry.surfaced += 1;
        if (used) {
          entry.used += 1;
          entry.everUsed = true;
        }
      }
      report.memory?.surfacings.push({
        sid: surfaced.sid,
        ts: surfaced.ts,
        channel: surfaced.channel,
        visible,
        outcome,
        final: judgment?.final === true,
      });
    }

    // Exposures: closed epochs from the ledger, open ones (provisional) from the detector state.
    const exposures: OpenExposure[] = [];
    const closed = standingExposures(rows);
    for (const row of closed.values()) {
      exposures.push({
        dismissed_turns: Number.isSafeInteger(row.dismissed_turns) ? (row.dismissed_turns as number) : 0,
        exposure_id: row.exposure_id,
        mkey: row.mkey,
        first_sid: row.first_sid,
        first_ts: surfacedBySid.get(row.first_sid)?.ts ?? row.ts,
        channel: row.channel,
        visible: row.visible === true,
        outcome: row.outcome,
        credits: Array.isArray(row.credits) ? row.credits : [],
        agent_key: row.agent_key,
        ...(row.subagent_type ? { subagent_type: row.subagent_type } : {}),
        wire: row.wire,
        principal: row.principal,
      });
    }
    const closedIds = new Set(closed.keys());
    const open = openExposures(vaultPath, sessionKey).filter((exposure) => !closedIds.has(exposure.exposure_id));
    for (const exposure of [...exposures, ...open]) {
      if (!measured(exposure) || !inRange(exposure.first_ts)) continue;
      if (memory && exposure.mkey !== memory) continue;
      const provisional = open.includes(exposure);
      const used = isUsed(exposure.outcome);
      const usedII = exposure.credits.length > 0;
      addTo(report.exposures, {
        channel: exposure.channel,
        agentKey: exposure.agent_key,
        subagentType: exposure.subagent_type,
        visible: exposure.visible,
        used,
        usedII,
        citedHandle: exposure.outcome === "cited_handle",
        dismissed: exposure.dismissed_turns > 0,
        titleAssessable: null,
        outcome: exposure.outcome,
      });
      if (provisional) report.exposures.provisional += 1;
      else report.exposures.final += 1;
      if (exposure.visible) {
        report.used_ii_credits += exposure.credits.length;
        const entry = memoryEntry(exposure.mkey, "");
        entry.used += exposure.credits.length;
        if (used || usedII) entry.everUsed = true;
      }
      report.memory?.exposures.push({
        exposure_id: exposure.exposure_id,
        outcome: exposure.outcome,
        credits: [...exposure.credits],
        final: !provisional,
      });
    }
  }

  finishSection(report.surfacings);
  finishSection(report.exposures);
  report.used_ii_credits_per_100_human_turns =
    report.turns.human > 0 ? Math.round((report.used_ii_credits / report.turns.human) * 100 * 100) / 100 : null;
  report.provisional_share = ratio(provisionalSurfacings, report.surfacings.total);
  report.unknown_share = ratio(unknownSurfacings, report.surfacings.total);
  report.explicit_recall.cards = report.explicit_recall.keyed + report.explicit_recall.unnameable;
  report.explicit_recall.unnameable_share = ratio(report.explicit_recall.unnameable, report.explicit_recall.cards);
  report.features = [...features.values()].sort((a, b) => b.rows - a.rows);
  report.coverage = [...coverage.entries()]
    .map(([wire, value]) => ({
      wire,
      rows: value.rows,
      sessions: value.sessions.size,
      status: (MEASURED_WIRES.includes(wire) ? "measured" : "unmeasured") as "measured" | "unmeasured",
    }))
    .sort((a, b) => a.wire.localeCompare(b.wire));
  if (!options.compact) {
    const entries = [...perMemory.entries()];
    report.top_unused = entries
      .filter(([, entry]) => entry.surfaced > 0 && !entry.everUsed)
      .sort((a, b) => b[1].surfaced - a[1].surfaced || a[0].localeCompare(b[0]))
      .slice(0, TOP_N)
      .map(([mkey, entry]) => ({ mkey, title: entry.title, surfaced: entry.surfaced }));
    report.top_used = entries
      .filter(([, entry]) => entry.everUsed)
      .sort((a, b) => b[1].used - a[1].used || a[0].localeCompare(b[0]))
      .slice(0, TOP_N)
      .map(([mkey, entry]) => ({ mkey, title: entry.title, used: entry.used }));
  }
  return report;
}

/** `minni_status`'s fixed 7-day block: the report without the top lists. Never throws. */
export function useStatusBlock(vaultPath: string, now?: number | string | Date): UseReport | { error: string } {
  try {
    return buildUseReport(vaultPath, { days: STATUS_USE_DAYS, now, compact: true });
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * One session's use counts for the Stop receipt: delivered surfacings, and
 * each one's standing outcome (`pending` when not judged yet). Null when the
 * ledger is off, the session has no surfacing, or its ledger is refused.
 */
export function sessionUseCounts(vaultPath: string, sessionId: string): SessionUseCounts | null {
  try {
    if (typeof sessionId !== "string" || sessionId.length === 0) return null;
    if (!flagOn(vaultPath, "MINNI_USE_LEDGER")) return null;
    const sessionKey = sessionKeyFrom({ session_id: sessionId });
    const { rows, refused } = readSessionLedger(vaultPath, sessionKey);
    if (refused) return null;
    const judgments = standingJudgments(rows);
    const counts: SessionUseCounts = {
      surfaced: 0,
      cited: 0,
      cited_handle: 0,
      applied: 0,
      opened: 0,
      dismissed: 0,
      ignored: 0,
      unknown: 0,
      pending: 0,
    };
    const seen = new Set<string>();
    for (const row of rows) {
      if (row.kind !== "surfaced" || row.delivered !== true || seen.has(row.sid)) continue;
      seen.add(row.sid);
      counts.surfaced += 1;
      const outcome = judgments.get(row.sid)?.outcome;
      if (outcome === undefined) counts.pending += 1;
      else if (
        outcome === "cited" ||
        outcome === "cited_handle" ||
        outcome === "applied" ||
        outcome === "opened" ||
        outcome === "dismissed" ||
        outcome === "ignored"
      ) {
        counts[outcome] += 1;
      } else counts.unknown += 1;
    }
    return counts.surfaced > 0 ? counts : null;
  } catch {
    return null;
  }
}
