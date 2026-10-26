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
import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";

import type { RecallResponse } from "./daemon_transport.js";
import type { VaultSearchResult } from "./vault.js";
import { assertWriteTargetUnder } from "./vault.js";

/** Relative location of the recall-state file under the vault. Portable across agents. */
export const RECALL_STATE_RELPATH = path.join(".runtime", "recall-state.json");

/** S2b (§6.3.1): session-scoped recall-state directory under the vault. */
export const RECALL_STATE_DIR_RELPATH = path.join(".runtime", "recall-state.d");

/** S2b (§6.3.2): retention horizon for .claim files under recall-state.d (24h). */
export const CLAIM_RETENTION_MS = 24 * 60 * 60 * 1000;

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

/**
 * H3: hit titles/wikilinks originate from vault note content, which is untrusted
 * (any local process can write a note). These strings are interpolated into the
 * PreToolUse deny reason and the per-turn pointer — text the model reads as
 * guidance. A title carrying newlines/control chars could smuggle its own lines
 * (e.g. "\n## SYSTEM: ignore prior instructions") that read as fresh imperative
 * instructions rather than inert data. Neutralize the structural markers an
 * injection relies on:
 *   - collapse every ASCII control char (CR/LF/tab included) to a space;
 *   - strip markdown heading markers (#) and backticks so a title cannot render
 *     as its own heading or code fence;
 *   - collapse whitespace runs and clamp the length.
 * The result is a single, bounded, inert token that stays inside the fixed
 * template rather than reading as structure.
 */
export function sanitizeRecallField(value: unknown, maxLen = 120): string {
  const raw = typeof value === "string" ? value : String(value ?? "");
  const collapsed = raw
    .replace(new RegExp("[\\u0000-\\u001F\\u007F]+", "g"), " ")
    .replace(/[#`]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (collapsed.length <= maxLen) return collapsed;
  return `${collapsed.slice(0, maxLen - 1).trimEnd()}…`;
}

export interface RecallState {
  task_signature: string;
  intent: string;
  top_hits: RecallStateHit[];
  top_score: number;
  /** s6 guard sets this true once it has surfaced the pointer; reset false each new turn. */
  consumed: boolean;
  ts: string;
  /** S2b (§6.3.1): raw session_id that armed this state (omitted when unlabeled). */
  session_id?: string;
  /** S2b (§6.3.1): agent_id from the arming payload, when present. */
  agent_id?: string;
  /** S2b (§6.3.1): prompt_id from the arming payload, when present. */
  prompt_id?: string;
  /** S2b (§6.3.4): wikilinks/titles/handles already surfaced in the UPS pointer. */
  pointer_named?: string[];
  /** S2b (§6.3.2): fresh random nonce per arming so re-planting is a new arming. */
  arming_id?: string;
}

export interface RecallStateOptions {
  sessionId?: string;
  guardV2?: boolean;
}

export interface StrongRecall {
  topScore: number;
  topHits: RecallStateHit[];
}

function shouldIgnoreStateReadError(error: unknown): boolean {
  if (error instanceof SyntaxError) return true;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    (error as { code?: unknown }).code === "ENOENT"
  );
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

const HEX16_RE = /^[0-9a-f]{16}$/;
const SAFE_TOKEN_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

/**
 * S2b (§6.3.1): derive the 16-hex session key for a raw session_id (matching
 * use-ledger.ts sessionKeyFrom). Returns null when unlabeled or synthetic "session".
 */
export function sessionStateKey(sessionId?: string): string | null {
  const trimmed = typeof sessionId === "string" ? sessionId.trim() : "";
  if (!trimmed || trimmed === "session") return null;
  if (HEX16_RE.test(trimmed)) return trimmed;
  return createHash("sha256").update(trimmed, "utf8").digest("hex").slice(0, 16);
}

/** Absolute path to the session-scoped recall-state file, or null when unlabeled. */
export function recallStateSessionPath(vaultPath: string, sessionId?: string): string | null {
  const key = sessionStateKey(sessionId);
  if (!key) return null;
  return path.join(vaultPath, RECALL_STATE_DIR_RELPATH, `${key}.json`);
}

/**
 * S2b (§6.3.2, §6.3.3): derive the arming key for claimArming.
 * Uses `state.arming_id` when present, else `sha1(task_signature + ts)[:16]`.
 */
export function armingKeyFor(
  state: Pick<RecallState, "task_signature"> & { ts?: string; arming_id?: string },
): string {
  const armingId = typeof state.arming_id === "string" ? state.arming_id.trim() : "";
  if (armingId && SAFE_TOKEN_RE.test(armingId)) return armingId;
  return createHash("sha1")
    .update(`${state.task_signature}${state.ts ?? ""}`, "utf8")
    .digest("hex")
    .slice(0, 16);
}

function safeClaimSegment(raw: string | undefined, fallback: string): string {
  const trimmed = typeof raw === "string" ? raw.trim() : "";
  if (!trimmed) return fallback;
  if (SAFE_TOKEN_RE.test(trimmed)) return trimmed;
  return createHash("sha256").update(trimmed, "utf8").digest("hex").slice(0, 16);
}

/**
 * S2b (§6.3.2): best-effort prune of `.claim` files in `<vault>/.runtime/recall-state.d`
 * older than `maxAgeMs` (default 24h). Never follows symlinks and never throws.
 */
export function pruneOldClaimFiles(
  vaultPath: string,
  nowMs: number = Date.now(),
  maxAgeMs: number = CLAIM_RETENTION_MS,
): number {
  let removed = 0;
  try {
    const dirPath = path.join(vaultPath, RECALL_STATE_DIR_RELPATH);
    assertWriteTargetUnder(dirPath, vaultPath);
    const dirStat = fs.lstatSync(dirPath);
    if (!dirStat.isDirectory() || dirStat.isSymbolicLink()) return 0;
    for (const name of fs.readdirSync(dirPath)) {
      if (!name.endsWith(".claim")) continue;
      try {
        const target = path.join(dirPath, name);
        const st = fs.lstatSync(target);
        if (!st.isFile() || st.isSymbolicLink()) continue;
        if (nowMs - st.mtimeMs > maxAgeMs) {
          fs.unlinkSync(target);
          removed += 1;
        }
      } catch {
        // Ignore per-entry errors.
      }
    }
  } catch {
    // Best-effort cleanup.
  }
  return removed;
}

function ensureContainedDir(
  vaultPath: string,
  segments: readonly string[],
  create: boolean,
): string | null {
  let current = vaultPath;
  for (const segment of segments) {
    const next = path.join(current, segment);
    assertWriteTargetUnder(next, vaultPath);
    let st: fs.Stats | undefined;
    try {
      st = fs.lstatSync(next);
    } catch (error) {
      if ((error as { code?: unknown } | null)?.code !== "ENOENT") throw error;
    }
    if (!st) {
      if (!create) return null;
      try {
        fs.mkdirSync(next, { mode: 0o700 });
      } catch (error) {
        if ((error as { code?: unknown } | null)?.code !== "EEXIST") throw error;
      }
      st = fs.lstatSync(next);
    }
    if (st.isSymbolicLink()) {
      assertWriteTargetUnder(fs.realpathSync(next), vaultPath);
      st = fs.statSync(next);
    }
    if (!st.isDirectory()) {
      throw new Error(`write path component is not a directory: ${next}`);
    }
    current = next;
  }
  return current;
}

/**
 * S2b (§6.3.2): atomically claim an arming once across concurrent PreToolUse
 * processes using `O_CREAT | O_EXCL` (`wx`, mode `0o600`) on
 * `<vault>/.runtime/recall-state.d/<safeSessionKey>.<safeArmingKey>.claim`.
 * Returns true for the single winner, false on EEXIST or any error.
 */
export async function claimArming(
  vaultPath: string,
  sessionKey: string | undefined,
  armingKey: string,
): Promise<boolean> {
  let fd: number | undefined;
  try {
    const sKey = sessionStateKey(sessionKey) ?? safeClaimSegment(sessionKey, "default");
    const aKey = safeClaimSegment(armingKey, "arming");
    const dirPath = ensureContainedDir(vaultPath, [".runtime", "recall-state.d"], true);
    if (!dirPath) return false;
    const claimPath = path.join(dirPath, `${sKey}.${aKey}.claim`);
    assertWriteTargetUnder(claimPath, vaultPath);
    const flags =
      fs.constants.O_CREAT |
      fs.constants.O_EXCL |
      fs.constants.O_WRONLY |
      (fs.constants.O_NOFOLLOW ?? 0);
    fd = fs.openSync(claimPath, flags, 0o600);
    fs.writeSync(fd, `${process.pid} ${Date.now()}\n`);
    return true;
  } catch {
    return false;
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // Ignore close error.
      }
    }
  }
}

async function readStateFileAt(filePath: string, vaultPath: string): Promise<RecallState | null> {
  try {
    assertWriteTargetUnder(filePath, vaultPath);
    const raw = await readFile(filePath, "utf8");
    const parsed = JSON.parse(raw) as RecallState;
    if (
      parsed &&
      typeof parsed === "object" &&
      typeof parsed.task_signature === "string" &&
      Array.isArray(parsed.top_hits)
    ) {
      return parsed;
    }
  } catch (error) {
    // Containment failures (symlink escape) → treat as absent, not disclosure.
    if (
      error instanceof Error &&
      /symlink|escapes root|containment/i.test(error.message)
    ) {
      return null;
    }
    if (!shouldIgnoreStateReadError(error)) {
      throw error;
    }
  }
  return null;
}

async function writeStateFileAtomic(
  filePath: string,
  vaultPath: string,
  payload: RecallState,
): Promise<void> {
  const relDir = path.relative(vaultPath, path.dirname(filePath));
  const segments = relDir.split(path.sep).filter(Boolean);
  ensureContainedDir(vaultPath, segments, true);
  assertWriteTargetUnder(filePath, vaultPath);
  const tmpPath = `${filePath}.tmp-${process.pid}-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2)}`;
  try {
    await writeFile(tmpPath, JSON.stringify(payload, null, 2), { encoding: "utf8", mode: 0o600 });
    await rename(tmpPath, filePath);
  } catch (writeErr) {
    await rm(tmpPath, { force: true }).catch(() => {});
    throw writeErr;
  }
}

/**
 * Persist the strong recall for this turn. `consumed` is always written false:
 * a new task_signature is a new turn, and the s6 guard flips it to true once it
 * has surfaced the pointer. Best-effort: a write failure must never break the
 * hook, so the caller treats a thrown error as "no state written".
 */
export async function writeRecallState(
  vaultPath: string,
  state: Omit<RecallState, "consumed" | "ts"> & { ts?: string; consumed?: boolean },
  options?: RecallStateOptions,
): Promise<string> {
  const mirrorPath = recallStatePath(vaultPath);
  const rawSession =
    (typeof state.session_id === "string" && state.session_id.trim()) ||
    (typeof options?.sessionId === "string" && options.sessionId.trim()) ||
    undefined;
  const effectiveSessionId =
    rawSession && rawSession !== "session" ? rawSession : undefined;
  await mkdir(path.dirname(mirrorPath), { recursive: true });
  // H2: the state file lives under the vault, which is writable by any local
  // process. A bare writeFile follows symlinks — an attacker who points
  // <vault>/.runtime (or the target file itself) outside the vault could
  // redirect this write. Reject any symlink escape before writing.
  assertWriteTargetUnder(mirrorPath, vaultPath);
  const existing = await readRecallState(vaultPath, {
    sessionId: effectiveSessionId,
    guardV2: options?.guardV2,
  }).catch(() => null);
  const preserveConsumed =
    state.consumed === true ||
    (existing !== null &&
      existing.task_signature === state.task_signature &&
      existing.consumed === true);
  const payload: RecallState = {
    task_signature: state.task_signature,
    intent: state.intent,
    top_hits: state.top_hits,
    top_score: state.top_score,
    consumed: preserveConsumed,
    ts: state.ts ?? new Date().toISOString(),
    ...(effectiveSessionId ? { session_id: effectiveSessionId } : {}),
    ...(typeof state.agent_id === "string" && state.agent_id.trim()
      ? { agent_id: state.agent_id.trim() }
      : {}),
    ...(typeof state.prompt_id === "string" && state.prompt_id.trim()
      ? { prompt_id: state.prompt_id.trim() }
      : {}),
    ...(Array.isArray(state.pointer_named)
      ? { pointer_named: [...state.pointer_named] }
      : {}),
    arming_id:
      preserveConsumed && existing?.arming_id
        ? existing.arming_id
        : state.arming_id ?? randomBytes(8).toString("hex"),
  };
  if (effectiveSessionId && options?.guardV2 !== false) {
    const sessionFile = recallStateSessionPath(vaultPath, effectiveSessionId);
    if (sessionFile) {
      await writeStateFileAtomic(sessionFile, vaultPath, payload);
    }
  }
  await writeFile(mirrorPath, JSON.stringify(payload, null, 2), "utf8");
  return mirrorPath;
}

/** Read the recall-state file, or null when absent/malformed. */
export async function readRecallState(
  vaultPath: string,
  options?: RecallStateOptions,
): Promise<RecallState | null> {
  const sessionId =
    typeof options?.sessionId === "string" &&
    options.sessionId.trim() &&
    options.sessionId.trim() !== "session"
      ? options.sessionId.trim()
      : undefined;
  if (sessionId && options?.guardV2 !== false) {
    const sessionFile = recallStateSessionPath(vaultPath, sessionId);
    if (sessionFile) {
      const scoped = await readStateFileAt(sessionFile, vaultPath);
      if (scoped !== null) return scoped;
    }
    const mirror = await readStateFileAt(recallStatePath(vaultPath), vaultPath);
    if (
      mirror &&
      (!mirror.session_id ||
        mirror.session_id === sessionId ||
        sessionStateKey(mirror.session_id) === sessionStateKey(sessionId))
    ) {
      return mirror;
    }
    return null;
  }
  return readStateFileAt(recallStatePath(vaultPath), vaultPath);
}

/** Remove the recall-state file (used on weak turns to clear a stale strong pointer). */
export async function clearRecallState(
  vaultPath: string,
  options?: RecallStateOptions,
): Promise<void> {
  const mirrorPath = recallStatePath(vaultPath);
  // H2: a bare rm follows a parent dir symlink — <vault>/.runtime → outside
  // would unlink the real file in the escape target. Write/markConsumed already
  // refuse that; clear must too. Callers treat a throw as best-effort no-op.
  assertWriteTargetUnder(mirrorPath, vaultPath);
  const sessionId =
    typeof options?.sessionId === "string" &&
    options.sessionId.trim() &&
    options.sessionId.trim() !== "session"
      ? options.sessionId.trim()
      : undefined;
  if (sessionId && options?.guardV2 !== false) {
    const sessionFile = recallStateSessionPath(vaultPath, sessionId);
    if (sessionFile) {
      const dir = ensureContainedDir(vaultPath, [".runtime", "recall-state.d"], false);
      if (dir) {
        assertWriteTargetUnder(sessionFile, vaultPath);
        await rm(sessionFile, { force: true });
      }
    }
    const mirror = await readStateFileAt(mirrorPath, vaultPath).catch(() => null);
    if (
      !mirror ||
      !mirror.session_id ||
      mirror.session_id === sessionId ||
      sessionStateKey(mirror.session_id) === sessionStateKey(sessionId)
    ) {
      await rm(mirrorPath, { force: true });
    }
    return;
  }
  await rm(mirrorPath, { force: true });
}

function fileOrLinkExists(filePath: string): boolean {
  try {
    fs.lstatSync(filePath);
    return true;
  } catch {
    return false;
  }
}

async function markFileConsumed(filePath: string, vaultPath: string): Promise<boolean> {
  try {
    assertWriteTargetUnder(filePath, vaultPath);
    const raw = await readFile(filePath, "utf8");
    const parsed = JSON.parse(raw) as RecallState;
    if (!parsed || typeof parsed !== "object") return false;
    parsed.consumed = true;
    await writeStateFileAtomic(filePath, vaultPath, parsed);
    return true;
  } catch {
    return false;
  }
}

/**
 * s6 guard: flip `consumed` to true in place after the guard has surfaced the
 * recall (once). This is the load-bearing idempotency write — once set, the
 * guard ALLOWS every subsequent tool call this turn, so the re-issued call
 * always passes. Best-effort: returns false on any read/write failure so a
 * persistence hiccup degrades to "allow next time" rather than a block loop.
 */
export async function markRecallConsumed(
  vaultPath: string,
  options?: RecallStateOptions,
): Promise<boolean> {
  const mirrorPath = recallStatePath(vaultPath);
  const sessionId =
    typeof options?.sessionId === "string" &&
    options.sessionId.trim() &&
    options.sessionId.trim() !== "session"
      ? options.sessionId.trim()
      : undefined;
  if (sessionId && options?.guardV2 !== false) {
    const sessionFile = recallStateSessionPath(vaultPath, sessionId);
    const hasScoped = sessionFile !== null && fileOrLinkExists(sessionFile);
    const scopedOk = hasScoped && sessionFile ? await markFileConsumed(sessionFile, vaultPath) : false;
    const mirror = await readStateFileAt(mirrorPath, vaultPath).catch(() => null);
    let mirrorOk = false;
    if (
      mirror &&
      (!mirror.session_id ||
        mirror.session_id === sessionId ||
        sessionStateKey(mirror.session_id) === sessionStateKey(sessionId))
    ) {
      mirrorOk = await markFileConsumed(mirrorPath, vaultPath);
    }
    return hasScoped ? scopedOk : mirrorOk;
  }
  const mirror = await readStateFileAt(mirrorPath, vaultPath).catch(() => null);
  const mirrorOk = await markFileConsumed(mirrorPath, vaultPath);
  if (mirror?.session_id && options?.guardV2 !== false) {
    const sessionFile = recallStateSessionPath(vaultPath, mirror.session_id);
    if (sessionFile && fileOrLinkExists(sessionFile)) {
      await markFileConsumed(sessionFile, vaultPath);
    }
  }
  return mirrorOk;
}

/**
 * The ≤ ~120-token light pointer injected into the prompt on a strong turn.
 * Names the count + top hit + score and tells the agent to consult minni_recall
 * before deriving from scratch — a SIGNPOST, not the recall content itself.
 */
export function buildRecallPointer(state: StrongRecall): string {
  const top = state.topHits[0];
  const n = state.topHits.length;
  // H3: `top.title` is untrusted note content — sanitize (strip newlines/control
  // chars, clamp) and quote it as inert data so a crafted title cannot inject
  // its own line/instruction into the prompt pointer.
  const title = sanitizeRecallField(top.title);
  return (
    `📓 ${n} relevant ${n === 1 ? "memory" : "memories"} ` +
    `(top: "${title}", score ${top.score.toFixed(2)}). ` +
    `Consult minni_recall (or the recall-state file) before deriving from scratch.`
  );
}

/**
 * The degraded-turn pointer: the hook's budget expired before the daemon
 * answered, so we surface the PREVIOUS turn's recall-state instead of injecting
 * nothing. Explicitly marked stale, with its age, because a pointer the agent
 * cannot tell is stale is worse than none — it would read last turn's hits as
 * evidence about this turn's question. The agent is told to re-run recall.
 *
 * Fails closed to undefined on an empty/missing state: there is nothing honest
 * to point at, and an empty "stale" banner is pure noise.
 */
export function buildStaleRecallPointer(
  state: RecallState | null,
  now: Date = new Date(),
): string | undefined {
  const top = state?.top_hits?.[0];
  if (!state || !top) return undefined;
  const title = sanitizeRecallField(top.title);
  const ageMs = now.getTime() - Date.parse(state.ts);
  const age = Number.isFinite(ageMs) && ageMs >= 0
    ? `${Math.round(ageMs / 1000)}s old`
    : "age unknown";
  const n = state.top_hits.length;
  return (
    `⚠️ STALE recall pointer (${age}) — the memory daemon did not answer within ` +
    `this turn's budget, so these are the PREVIOUS turn's ${n} ` +
    `${n === 1 ? "hit" : "hits"} (top: "${title}"), NOT a match for the current ` +
    `prompt. Re-run minni_recall before relying on them.`
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
  } catch (error) {
    if (!shouldIgnoreStateReadError(error)) {
      throw error;
    }
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
