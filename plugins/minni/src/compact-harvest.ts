// Compaction-summary harvest (plan-3e5a410b9ab6f715).
//
// When a platform compacts its context it first DISTILLS the session into a
// structured summary — the highest-signal outcome document the session ever
// produces, and until now Minni dropped it on the floor: PreCompact fires
// before the summary exists, and the Stop auto-draft was retired 2026-07-24
// precisely because it distilled Minni's own telemetry instead of real
// material. The compaction summary IS the "genuine outcome material" that
// retirement note anticipated.
//
// The harvest is deliberately split across the boot/daemon boundary:
//   1. HOOK SIDE (this module, fast + fail-open): capture the RAW summary
//      verbatim into ONE inbox file (`kind: "compact_summary"`). No AFM call
//      and no drafting here — SessionStart blocks the session boot, and the
//      raw document is exactly what the reviewer should see. The only
//      transformation applied is stripping the continuation frame (transport
//      boilerplate, not session content) and a size cap.
//   2. DAEMON SIDE (afm_passes/compact_distillation.py): the same AFM-loop
//      timer that ingests stop-candidate learnings picks the file up,
//      distills it (native `session_distill` op when AFM is up, deterministic
//      section split when it is not) and proposes governance-gated
//      candidates. Harvest proposes; the operator resolves. Nothing here
//      auto-writes a learning.
//
// Dedup is keyed on a CONTENT HASH of the frame-stripped summary, persisted
// under `<vault>/.runtime/compact-harvest-state.json`. A content key (rather
// than the platform's summary id) is what lets two delivery paths coexist
// without double-harvesting: Claude Code's primary path is the PostCompact
// hook (docs 2026-07-29: PostCompact receives the summary directly as
// `compact_summary` — no transcript read, no flush race), and the SessionStart
// transcript tail-read remains as a BACKSTOP for summaries that PostCompact
// missed (hook not yet registered, older CLI, crash between compaction and the
// hook). PostCompact has no summary uuid, the transcript entry has no
// guarantee of being flushed (documented: transcript_path "may lag the
// in-memory conversation") — the hash is the one identity both paths share.
// Platform ids ride along as provenance only.
import { createHash } from "node:crypto";
import path from "node:path";
import { mkdir, open, readFile, rename, stat, writeFile } from "node:fs/promises";

import { asString, inboxPrincipalForVaultPath } from "./hook-utils.js";
import { recordAudit, writeInbox } from "./vault.js";

/** Relative location of the harvest dedup state under the vault. */
export const COMPACT_HARVEST_STATE_RELPATH = path.join(
  ".runtime",
  "compact-harvest-state.json",
);

/** Inbox file format tag — the compact_distillation pass's ingest contract. */
export const COMPACT_SUMMARY_KIND = "compact_summary";

/**
 * How much transcript tail to scan for the summary. Compaction summaries land
 * near the end of the file at compact/resume time; 4 MiB of tail covers even
 * pathological post-summary growth without ever pulling a whole multi-hundred-
 * megabyte transcript through the boot path.
 */
const DEFAULT_MAX_TAIL_BYTES = 4 * 1024 * 1024;

/** Cap on remembered summary ids — a session compacts a handful of times. */
const STATE_MAX_ENTRIES = 32;

/**
 * Cap on the stored raw summary. Real summaries run a few KB; the cap only
 * exists so a pathological transcript entry cannot balloon the inbox. Truncation
 * keeps the head — the structured sections front-load the durable content.
 */
const SUMMARY_TEXT_MAX_CHARS = 65536;

/**
 * Floor under which a "summary" is treated as empty (aborted or placeholder
 * compactions). Kept as a character floor on purpose: the substance/redaction
 * judgment belongs to the daemon-side distiller and governance review, not the
 * boot path.
 */
const SUMMARY_TEXT_MIN_CHARS = 40;

export interface CompactSummaryHit {
  /** Full summary text as the platform wrote it (frame included). */
  text: string;
  /** Transcript uuid of the summary message — the dedup key. */
  uuid: string;
  /** ISO timestamp of the summary entry, when present. */
  timestamp?: string;
}

export interface CompactHarvestConfig {
  vaultPath: string;
  workspaceId: string;
  /** Audit tool prefix, e.g. "hook" → hook_compact_harvest. */
  auditPrefix: string;
  /** Source platform recorded in the inbox provenance, e.g. "claude-code". */
  platform: string;
}

export interface CompactHarvestResult {
  harvested: boolean;
  /** Why nothing was written when harvested=false. */
  reason?:
    | "no_transcript_path"
    | "no_summary_found"
    | "already_harvested"
    | "empty_summary"
    | "harvest_error";
  inboxPath?: string;
  summaryId?: string;
  summaryChars?: number;
}

interface HarvestState {
  harvested: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Transcript extraction (Claude Code shape)
// ---------------------------------------------------------------------------

/** Message content in transcripts is a string or an array of typed blocks. */
function messageText(message: unknown): string {
  if (!message || typeof message !== "object") return "";
  const content = (message as { content?: unknown }).content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((block) =>
        block && typeof block === "object" && (block as { type?: unknown }).type === "text"
          ? asString((block as { text?: unknown }).text)
          : "",
      )
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

/**
 * Tail-read the transcript JSONL and return the NEWEST entry flagged
 * `isCompactSummary: true`. Reads at most `maxTailBytes` from the end of the
 * file; the first (possibly partial) line of a truncated read is discarded.
 * Returns undefined on any shape surprise — the harvest must never be the
 * thing that breaks a session boot.
 */
export async function extractLatestCompactSummary(
  transcriptPath: string,
  maxTailBytes = DEFAULT_MAX_TAIL_BYTES,
): Promise<CompactSummaryHit | undefined> {
  let raw: string;
  let truncated = false;
  try {
    const info = await stat(transcriptPath);
    if (!info.isFile() || info.size === 0) return undefined;
    if (info.size <= maxTailBytes) {
      raw = await readFile(transcriptPath, "utf8");
    } else {
      truncated = true;
      const handle = await open(transcriptPath, "r");
      try {
        const buffer = Buffer.alloc(maxTailBytes);
        await handle.read(buffer, 0, maxTailBytes, info.size - maxTailBytes);
        raw = buffer.toString("utf8");
      } finally {
        await handle.close();
      }
    }
  } catch {
    return undefined;
  }

  const lines = raw.split("\n");
  const start = truncated ? 1 : 0; // drop the partial first line of a tail read
  for (let i = lines.length - 1; i >= start; i -= 1) {
    const line = lines[i].trim();
    if (!line) continue;
    let entry: Record<string, unknown>;
    try {
      entry = JSON.parse(line) as Record<string, unknown>;
    } catch {
      continue;
    }
    if (entry.isCompactSummary !== true) continue;
    const text = messageText(entry.message);
    const uuid = asString(entry.uuid);
    if (!text || !uuid) continue;
    return {
      text,
      uuid,
      timestamp: asString(entry.timestamp) || undefined,
    };
  }
  return undefined;
}

/**
 * Strip the continuation boilerplate that frames the summary ("This session is
 * being continued …" / trailing "Please continue the conversation …" and the
 * transcript-pointer footer). Transport framing, not session content — the raw
 * SUMMARY is what gets stored. Falls back to the full text when the frame is
 * not recognized: an unfamiliar shape should degrade to over-inclusion, and
 * the daemon-side distiller + governance review still stand between it and
 * durable memory.
 */
export function stripContinuationFrame(text: string): string {
  let body = text;
  const summaryStart = body.match(/\bSummary:\s*\n/);
  if (summaryStart && summaryStart.index !== undefined) {
    body = body.slice(summaryStart.index + summaryStart[0].length);
  }
  for (const trailer of [
    /\nIf you need specific details from before compaction[\s\S]*$/,
    /\n(?:Please )?[Cc]ontinue the conversation from where (?:it|we) left (?:it )?off[\s\S]*$/,
  ]) {
    body = body.replace(trailer, "");
  }
  return body.trim();
}

// ---------------------------------------------------------------------------
// Dedup state
// ---------------------------------------------------------------------------

async function readHarvestState(vaultPath: string): Promise<HarvestState> {
  try {
    const raw = await readFile(path.join(vaultPath, COMPACT_HARVEST_STATE_RELPATH), "utf8");
    const parsed = JSON.parse(raw) as { harvested?: unknown };
    if (parsed && typeof parsed.harvested === "object" && parsed.harvested !== null) {
      const harvested: Record<string, string> = {};
      for (const [key, value] of Object.entries(parsed.harvested as Record<string, unknown>)) {
        if (typeof value === "string") harvested[key] = value;
      }
      return { harvested };
    }
  } catch {
    // Missing or corrupt state reads as empty — worst case is one duplicate
    // harvest, which the distiller's file-level idempotency absorbs.
  }
  return { harvested: {} };
}

async function writeHarvestState(vaultPath: string, state: HarvestState): Promise<void> {
  const entries = Object.entries(state.harvested)
    .sort((a, b) => (a[1] < b[1] ? -1 : 1))
    .slice(-STATE_MAX_ENTRIES);
  const target = path.join(vaultPath, COMPACT_HARVEST_STATE_RELPATH);
  const tmp = `${target}.tmp`;
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(tmp, JSON.stringify({ harvested: Object.fromEntries(entries) }, null, 2), "utf8");
  await rename(tmp, target);
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

export interface RawSummaryInput {
  /** The summary text, frame and all — this function strips/caps it. */
  summaryText: string;
  /**
   * Platform id for this summary (transcript uuid, Kilo message id) —
   * PROVENANCE only; dedup runs on the content hash. Optional because
   * PostCompact delivers the summary with no id at all.
   */
  summaryId?: string;
  sessionId: string;
  /** ISO timestamp of the summary, when the platform provides one. */
  timestamp?: string;
}

/**
 * Store one raw compaction summary as a `compact_summary` inbox file. The
 * shared trunk for every platform: Claude Code reaches it via transcript
 * extraction (harvestCompactSummary), Kilo via SDK read-back in the native
 * plugin. Fail-open: error paths return a result instead of throwing —
 * a broken harvest must never break a session boot.
 */
export async function harvestSummaryText(
  config: CompactHarvestConfig,
  input: RawSummaryInput,
): Promise<CompactHarvestResult> {
  try {
    const summaryId = asString(input.summaryId) || undefined;
    const text = stripContinuationFrame(asString(input.summaryText)).slice(
      0,
      SUMMARY_TEXT_MAX_CHARS,
    );
    if (!text) return { harvested: false, reason: "no_summary_found" };
    // The dedup identity both delivery paths share — see the module header.
    const contentKey = createHash("sha1").update(text).digest("hex");

    const state = await readHarvestState(config.vaultPath);
    if (state.harvested[contentKey]) {
      return { harvested: false, reason: "already_harvested", summaryId };
    }

    if (text.length < SUMMARY_TEXT_MIN_CHARS) {
      // Aborted/placeholder compactions stay empty forever — mark the key so
      // the text is not re-scanned every boot, but write no inbox file.
      state.harvested[contentKey] = new Date().toISOString();
      await writeHarvestState(config.vaultPath, state);
      await recordAudit(config.vaultPath, {
        tool: `${config.auditPrefix}_compact_harvest`,
        summary: `compact-harvest ${input.sessionId}`,
        details: { reason: "empty_summary", ...(summaryId ? { summary_id: summaryId } : {}) },
      });
      return { harvested: false, reason: "empty_summary", summaryId };
    }

    // `kind`/`agent_id`/`workspace_id` follow the stop-candidate ingest
    // contract conventions (agent_id is the vault-derived principal, never the
    // operator-settable config id — see handleStopCore). inbox_ingest counts
    // this kind under skipped_by_kind (observable), and the
    // compact_distillation pass is the consumer that claims it.
    const inbox = await writeInbox(config.vaultPath, input.sessionId, {
      kind: COMPACT_SUMMARY_KIND,
      agent_id: inboxPrincipalForVaultPath(config.vaultPath),
      workspace_id: config.workspaceId,
      summary_text: text,
      summary_sha1: contentKey,
      ...(summaryId ? { summary_id: summaryId } : {}),
      platform: config.platform,
      session_id: input.sessionId,
      ...(input.timestamp ? { summary_timestamp: input.timestamp } : {}),
    });

    state.harvested[contentKey] = new Date().toISOString();
    await writeHarvestState(config.vaultPath, state);

    await recordAudit(config.vaultPath, {
      tool: `${config.auditPrefix}_compact_harvest`,
      summary: `compact-harvest ${input.sessionId}`,
      details: {
        ...(summaryId ? { summary_id: summaryId } : {}),
        summary_sha1: contentKey,
        summary_chars: text.length,
        workspace: config.workspaceId,
        inbox_path: inbox.filePath,
      },
    });

    return {
      harvested: true,
      inboxPath: inbox.filePath,
      summaryId,
      summaryChars: text.length,
    };
  } catch {
    return { harvested: false, reason: "harvest_error" };
  }
}

/**
 * Claude Code entry: extract the newest un-harvested compaction summary from
 * `transcriptPath`, then hand it to the shared trunk above.
 */
export async function harvestCompactSummary(
  config: CompactHarvestConfig,
  input: { transcriptPath?: string; sessionId: string },
): Promise<CompactHarvestResult> {
  try {
    const transcriptPath = asString(input.transcriptPath);
    if (!transcriptPath) return { harvested: false, reason: "no_transcript_path" };

    const hit = await extractLatestCompactSummary(transcriptPath);
    if (!hit) return { harvested: false, reason: "no_summary_found" };

    return await harvestSummaryText(config, {
      summaryText: hit.text,
      summaryId: hit.uuid,
      sessionId: input.sessionId,
      timestamp: hit.timestamp,
    });
  } catch {
    return { harvested: false, reason: "harvest_error" };
  }
}
