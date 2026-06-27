// Per-turn recall POINTER + portable recall-state file (slice s5).
//
// Problem this solves: the UserPromptSubmit hook used to inject the FULL recall
// pack (~1500 tokens) into EVERY turn. The agent habituates and tunes it out,
// and the content lived only in the (compactable, ignorable) prompt. s5 splits
// that into two:
//   1. a LIGHT POINTER (≤ ~120 tokens) injected into the prompt — only when the
//      recall is actually STRONG, so it stays salient instead of becoming noise;
//   2. the FULL top hits written OUT of the prompt into a portable state file
//      `<vault>/.runtime/recall-state.json`, which the s6 PreToolUse guard reads.
//
// The state file lives under the VAULT (not ${CLAUDE_PLUGIN_DATA}) on purpose:
// it must be portable across codex/grok/claude, all of which share the vault
// convention but not the plugin-data dir.
//
// Strength gate (operator preference): we do NOT trigger on routeMemoryIntent
// keyword classification — the operator explicitly rejected "monitoring prompts
// for trigger words". Recall runs as before; the pointer + state are emitted
// ONLY when the recall itself is strong (top strength ≥ a configurable
// threshold). Weak/absent hits inject nothing and write no state.
import path from "node:path";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";

import type { RecallResponse } from "./sovereign.js";
import type { VaultSearchResult } from "./vault.js";

/** Relative location of the recall-state file under the vault. Portable across agents. */
export const RECALL_STATE_RELPATH = path.join(".runtime", "recall-state.json");

/**
 * Default strength threshold for emitting the per-turn pointer + state file.
 *
 * The gate runs against the daemon's per-result `confidence` — a calibrated
 * value in [0, 1] (engine/scoring.py compute_confidence: sigmoid-blended
 * cross-encoder + RRF, percentile-calibrated against a rolling window). The raw
 * `score` field is deliberately NOT used as the gate: it is un-normalized
 * (cross-encoder logits / rerank scores observed anywhere from negative to ~100
 * across layers), so no fixed cutoff is meaningful on it.
 *
 * 0.55 == "above the calibrated median, with cross-encoder agreement". With a
 * calibrated distribution this is roughly the 55th percentile, so the pointer
 * fires only on the genuinely-relevant minority of turns rather than every turn.
 * Conservative on purpose: a missed pointer just means the agent recalls on
 * demand (cheap); a false-strong pointer is exactly the habituation we are
 * removing. Override with MINNI_RECALL_POINTER_THRESHOLD.
 */
export const DEFAULT_RECALL_POINTER_THRESHOLD = 0.55;

/**
 * A direct vault substring match scores +50 in scoreVaultNote (full-query
 * substring hit). That is an independent, genuinely-strong lexical signal on a
 * separate scale from the daemon confidence, so a vault hit at/above this raw
 * score also opens the gate even when the daemon is unreachable. This is a
 * documented vault-scale constant, not a confidence value.
 */
export const VAULT_DIRECT_MATCH_SCORE = 50;

export function recallPointerThreshold(
  env: NodeJS.ProcessEnv = process.env,
): number {
  const raw = Number(env.MINNI_RECALL_POINTER_THRESHOLD);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_RECALL_POINTER_THRESHOLD;
}

export interface RecallStateHit {
  title: string;
  wikilink: string;
  /** The per-hit strength used (daemon confidence in [0,1], or normalized vault signal). */
  score: number;
}

export interface RecallState {
  task_signature: string;
  intent: string;
  top_hits: RecallStateHit[];
  top_score: number;
  /** s6 guard sets this true once it has surfaced the pointer; reset false each new turn. */
  consumed: boolean;
  ts: string;
}

export interface StrongRecall {
  topScore: number;
  topHits: RecallStateHit[];
}

function titleFromWikilink(wikilink: string): string {
  // "[[wiki/sessions/20260608-aetherkernel-v63]]" -> "20260608-aetherkernel-v63"
  const inner = wikilink.replace(/^\[\[/, "").replace(/\]\]$/, "");
  const base = inner.split("/").pop() ?? inner;
  return base || inner || "(untitled)";
}

function strengthOf(result: Record<string, unknown>): number | undefined {
  // Prefer the calibrated [0,1] confidence; the raw `score` is un-normalized and
  // is NOT a valid gate signal unless it already happens to be in [0,1].
  const confidence = result.confidence;
  if (typeof confidence === "number" && Number.isFinite(confidence)) {
    return Math.max(0, Math.min(1, confidence));
  }
  const score = result.score;
  if (typeof score === "number" && Number.isFinite(score) && score >= 0 && score <= 1) {
    return score;
  }
  return undefined;
}

function hitTitleWikilink(result: Record<string, unknown>): { title: string; wikilink: string } {
  const wikilink =
    typeof result.wikilink === "string"
      ? result.wikilink
      : typeof result.filename === "string"
        ? `[[${result.filename}]]`
        : "[[?]]";
  const title =
    typeof result.title === "string" && result.title.trim()
      ? result.title.trim()
      : titleFromWikilink(wikilink);
  return { title, wikilink };
}

/**
 * Decide whether this turn's recall is STRONG enough to surface a pointer, and
 * collect the top hits for the state file. Pure: no I/O. Returns null when the
 * recall is weak/absent (caller injects nothing and writes no state).
 *
 * Strength sources (gate opens if EITHER clears its bar):
 *   - daemon recall: per-result calibrated confidence ≥ `threshold`
 *     (identity-shelf hits excluded — they are boot context, not turn-relevant);
 *   - vault search: a direct substring match (raw score ≥ VAULT_DIRECT_MATCH_SCORE),
 *     normalized into [0,1] for the unified top_score scale.
 */
export function extractStrongRecall(
  response: RecallResponse | undefined,
  vaultResults: VaultSearchResult[],
  threshold: number,
  limit = 5,
): StrongRecall | null {
  const hits: RecallStateHit[] = [];

  const daemonResults = response && Array.isArray(response.results) ? response.results : [];
  for (const raw of daemonResults) {
    const result = (raw ?? {}) as Record<string, unknown>;
    if (String(result.layer ?? "") === "identity") continue; // boot shelf, not turn-relevant
    const strength = strengthOf(result);
    if (strength === undefined || strength < threshold) continue;
    const { title, wikilink } = hitTitleWikilink(result);
    hits.push({ title, wikilink, score: Number(strength.toFixed(4)) });
  }

  for (const vaultHit of vaultResults) {
    if (vaultHit.score < VAULT_DIRECT_MATCH_SCORE) continue;
    // Normalize the vault raw score into [0,1] relative to the direct-match
    // floor so it shares the top_score scale (1.0 == a clean full-query hit).
    const normalized = Math.min(1, vaultHit.score / VAULT_DIRECT_MATCH_SCORE);
    hits.push({
      title: vaultHit.title || titleFromWikilink(vaultHit.wikilink),
      wikilink: vaultHit.wikilink,
      score: Number(normalized.toFixed(4)),
    });
  }

  if (hits.length === 0) return null;

  hits.sort((a, b) => b.score - a.score);
  const topHits = hits.slice(0, limit);
  return { topScore: topHits[0].score, topHits };
}

/** Absolute path to the recall-state file for a vault. */
export function recallStatePath(vaultPath: string): string {
  return path.join(vaultPath, RECALL_STATE_RELPATH);
}

/**
 * Persist the strong recall for this turn. `consumed` is always written false:
 * a new task_signature is a new turn, and the s6 guard flips it to true once it
 * has surfaced the pointer. Best-effort: a write failure must never break the
 * hook, so the caller treats a thrown error as "no state written".
 */
export async function writeRecallState(
  vaultPath: string,
  state: Omit<RecallState, "consumed" | "ts"> & { ts?: string },
): Promise<string> {
  const filePath = recallStatePath(vaultPath);
  await mkdir(path.dirname(filePath), { recursive: true });
  const payload: RecallState = {
    task_signature: state.task_signature,
    intent: state.intent,
    top_hits: state.top_hits,
    top_score: state.top_score,
    consumed: false,
    ts: state.ts ?? new Date().toISOString(),
  };
  await writeFile(filePath, JSON.stringify(payload, null, 2), "utf8");
  return filePath;
}

/** Read the recall-state file, or null when absent/malformed. */
export async function readRecallState(vaultPath: string): Promise<RecallState | null> {
  try {
    const raw = await readFile(recallStatePath(vaultPath), "utf8");
    const parsed = JSON.parse(raw) as RecallState;
    if (parsed && typeof parsed === "object" && typeof parsed.task_signature === "string") {
      return parsed;
    }
  } catch {
    // absent or malformed
  }
  return null;
}

/** Remove the recall-state file (used on weak turns to clear a stale strong pointer). */
export async function clearRecallState(vaultPath: string): Promise<void> {
  await rm(recallStatePath(vaultPath), { force: true });
}

/**
 * s6 guard: flip `consumed` to true in place after the guard has surfaced the
 * recall (denied once). This is the load-bearing idempotency write — once set,
 * the s6 guard ALLOWS every subsequent tool call this turn, so the re-issued
 * call always passes. Best-effort: returns false on any read/write failure so a
 * persistence hiccup degrades to "allow next time" rather than a block loop.
 */
export async function markRecallConsumed(vaultPath: string): Promise<boolean> {
  const filePath = recallStatePath(vaultPath);
  try {
    const raw = await readFile(filePath, "utf8");
    const parsed = JSON.parse(raw) as RecallState;
    if (!parsed || typeof parsed !== "object") return false;
    parsed.consumed = true;
    await writeFile(filePath, JSON.stringify(parsed, null, 2), "utf8");
    return true;
  } catch {
    return false;
  }
}

/**
 * The ≤ ~120-token light pointer injected into the prompt on a strong turn.
 * Names the count + top hit + score and tells the agent to consult minni_recall
 * before deriving from scratch — a SIGNPOST, not the recall content itself.
 */
export function buildRecallPointer(state: StrongRecall): string {
  const top = state.topHits[0];
  const n = state.topHits.length;
  return (
    `📓 ${n} relevant ${n === 1 ? "memory" : "memories"} ` +
    `(top: ${top.title}, score ${top.score.toFixed(2)}). ` +
    `Consult minni_recall (or the recall-state file) before deriving from scratch.`
  );
}

// ---- Lifecycle nudge state (slice c4/c5) --------------------------------------
//
// Kept SEPARATE from the recall-state file (which is rewritten on strong turns
// and cleared on weak ones): this records which lifecycle SURFACES have already
// been emphasized this session, so the situational `lifecycle_focus` fires at
// most once per surface per session. SessionStart resets it (fresh session ->
// re-emphasize). Portable under the vault, like recall-state.

/** Relative location of the lifecycle-state file under the vault. */
export const LIFECYCLE_STATE_RELPATH = path.join(".runtime", "lifecycle-state.json");

export interface LifecycleState {
  /** session id this emphasis set belongs to (advisory; SessionStart resets). */
  session_id: string;
  /** lifecycle surfaces already emphasized this session. */
  emphasized: string[];
}

/** Absolute path to the lifecycle-state file for a vault. */
export function lifecycleStatePath(vaultPath: string): string {
  return path.join(vaultPath, LIFECYCLE_STATE_RELPATH);
}

/** Read the lifecycle-state file, or null when absent/malformed. */
export async function readLifecycleState(vaultPath: string): Promise<LifecycleState | null> {
  try {
    const raw = await readFile(lifecycleStatePath(vaultPath), "utf8");
    const parsed = JSON.parse(raw) as LifecycleState;
    if (parsed && typeof parsed === "object" && Array.isArray(parsed.emphasized)) {
      return parsed;
    }
  } catch {
    // absent or malformed
  }
  return null;
}

/** Persist the lifecycle-state file. Best-effort; callers ignore failures. */
export async function writeLifecycleState(
  vaultPath: string,
  state: LifecycleState,
): Promise<void> {
  const filePath = lifecycleStatePath(vaultPath);
  await mkdir(path.dirname(filePath), { recursive: true });
  await writeFile(filePath, JSON.stringify(state, null, 2), "utf8");
}
