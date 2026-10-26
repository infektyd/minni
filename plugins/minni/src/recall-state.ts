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
import { durabilityOf } from "./durability.js";
import type { VaultSearchResult } from "./vault.js";
import { assertWriteTargetUnder } from "./vault.js";

/** Relative location of the recall-state file under the vault. Portable across agents. */
export const RECALL_STATE_RELPATH = path.join(".runtime", "recall-state.json");

/** S2b (§6.3.1): session-scoped recall-state directory under the vault. */
export const RECALL_STATE_DIR_RELPATH = path.join(".runtime", "recall-state.d");

/** S2b (§6.3.2): retention horizon for .claim files under recall-state.d (24h). */
export const CLAIM_RETENTION_MS = 24 * 60 * 60 * 1000;

/** S3c (§5.2 item 6): max characters for a sanitized recall field (title/wikilink). */
export const RECALL_FIELD_MAX_CHARS = 100;

/** S3c (§5.2 item 6): max characters for the single-line recall pointer. */
export const RECALL_POINTER_MAX_CHARS = 300;

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
  /** The per-hit strength used (daemon confidence in [0,1], or calibrated vault signal). */
  score: number;
  handle?: string;
  mkey?: string;
  relativePath?: string;
  scope?: string;
  docId?: number | string;
  gateRoute?: "a" | "b" | "c" | null;
}

/**
 * Normalize a vault path or wikilink to a canonical vault-relative path
 * (`wiki/.../foo.md`).
 */
export function normalizeVaultRelPath(rawPathOrWikilink: string): string {
  let cleaned = typeof rawPathOrWikilink === "string" ? rawPathOrWikilink.trim() : "";
  if (cleaned.startsWith("[[") && cleaned.endsWith("]]")) {
    cleaned = cleaned.slice(2, -2).trim();
  }
  cleaned = cleaned.replaceAll("\\", "/").replace(/^\.\/+/, "").replace(/^\/+/, "");
  if ((cleaned.startsWith("wiki/") || cleaned.startsWith("raw/")) && !cleaned.endsWith(".md")) {
    cleaned = `${cleaned}.md`;
  }
  return cleaned;
}

/** Deterministic short handle `m:<sha1(relPath)[:8]>` for a vault note. */
export function vaultHandleForPath(relPath: string): string {
  const norm = normalizeVaultRelPath(relPath);
  return `m:${createHash("sha1").update(norm, "utf8").digest("hex").slice(0, 8)}`;
}

/** Deterministic daemon document handle `d:<src>:<id>`. */
export function docHandleFor(src: string, docId: number | string): string {
  const safeSrc =
    String(src || "personal")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]/g, "") || "personal";
  return `d:${safeSrc}:${String(docId).trim()}`;
}

/** Resolve or compute the deterministic handle (`m:<8hex>` or `d:<src>:<id>`) for a hit. */
export function handleForHit(
  hit: Partial<RecallStateHit> & {
    relativePath?: string;
    wikilink?: string;
    scope?: string;
    docId?: number | string;
    handle?: string;
  },
): string {
  if (typeof hit.handle === "string" && hit.handle.trim()) {
    return hit.handle.trim();
  }
  if (hit.docId !== undefined && hit.docId !== null && String(hit.docId).trim() !== "") {
    return docHandleFor(hit.scope ?? "personal", hit.docId);
  }
  if (typeof hit.relativePath === "string" && hit.relativePath.trim()) {
    return vaultHandleForPath(hit.relativePath);
  }
  if (typeof hit.wikilink === "string" && hit.wikilink.trim()) {
    return vaultHandleForPath(hit.wikilink);
  }
  return vaultHandleForPath(String(hit.title ?? "untitled"));
}

/** Resolve or compute the canonical memory key (`vault:<relPath>` or `doc:<src>:<id>`) for a hit. */
export function mkeyForHit(
  hit: Partial<RecallStateHit> & {
    relativePath?: string;
    wikilink?: string;
    scope?: string;
    docId?: number | string;
    mkey?: string;
  },
): string {
  if (typeof hit.mkey === "string" && hit.mkey.trim()) {
    return hit.mkey.trim();
  }
  if (typeof hit.relativePath === "string" && hit.relativePath.trim()) {
    return `vault:${normalizeVaultRelPath(hit.relativePath)}`;
  }
  if (hit.docId !== undefined && hit.docId !== null && String(hit.docId).trim() !== "") {
    const safeSrc =
      String(hit.scope || "personal")
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9_-]/g, "") || "personal";
    return `doc:${safeSrc}:${String(hit.docId).trim()}`;
  }
  if (typeof hit.wikilink === "string" && hit.wikilink.trim()) {
    return `vault:${normalizeVaultRelPath(hit.wikilink)}`;
  }
  return `vault:${normalizeVaultRelPath(String(hit.title ?? "untitled"))}`;
}

/**
 * H3 + S3c (§5.2 item 6): hit titles/wikilinks originate from vault note content,
 * which is untrusted. Neutralize structural markers and invisible/bidi overrides:
 *   - strip Unicode zero-width and bidi control chars (\u200B-\u200F, \u202A-\u202E,
 *     \u2060-\u2069, \uFEFF);
 *   - collapse every C0/C1 control char (\u0000-\u001F, \u007F-\u009F) to a space;
 *   - strip markdown heading markers (#) and backticks;
 *   - collapse whitespace runs and clamp to `maxLen` (default RECALL_FIELD_MAX_CHARS = 100).
 */
export function sanitizeRecallField(value: unknown, maxLen = RECALL_FIELD_MAX_CHARS): string {
  const raw = typeof value === "string" ? value : String(value ?? "");
  const collapsed = raw
    .replace(/[\u200B-\u200F\u202A-\u202E\u2060-\u2069\uFEFF]+/g, "")
    .replace(/[\u0000-\u001F\u007F-\u009F]+/g, " ")
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

export interface ExtractStrongRecallOptions {
  rankV2?: boolean;
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

function isDaemonResultNoise(result: Record<string, unknown>): boolean {
  if (result.durability === "noise") return true;
  const title = typeof result.title === "string" ? result.title : "";
  if (/^\s*\[Subagent Context\]/i.test(title)) return true;
  const cls = durabilityOf({
    path: result.rel_path ?? result.path ?? result.filename ?? result.wikilink,
    title,
    content: result.content ?? result.snippet ?? result.body,
    source: result.source,
    status: result.status,
    type: result.type,
    category: result.category,
  });
  return cls === "noise";
}

/**
 * Decide whether this turn's recall is STRONG enough to surface a pointer, and
 * collect the top hits for the state file. Pure: no I/O. Returns null when the
 * recall is weak/absent (caller injects nothing and writes no state).
 *
 * Strength sources (gate opens if EITHER clears its bar):
 *   - daemon recall: per-result calibrated confidence ≥ `threshold`
 *     (identity-shelf hits and, when rankV2 is active, noise hits excluded);
 *   - vault search: calibrated `ups_gate` score (`clearsGate && weightedScore >= threshold`),
 *     falling back to legacy `score >= VAULT_DIRECT_MATCH_SCORE` only when `rankV2 === false`
 *     or when a caller passes un-calibrated mock results without `weightedScore`.
 */
export function extractStrongRecall(
  response: RecallResponse | undefined,
  vaultResults: VaultSearchResult[],
  threshold: number = DEFAULT_RECALL_POINTER_THRESHOLD,
  limit = 5,
  options?: ExtractStrongRecallOptions,
): StrongRecall | null {
  const rankV2 = options?.rankV2 !== false;
  const effThreshold =
    typeof threshold === "number" && Number.isFinite(threshold) && threshold > 0
      ? threshold
      : DEFAULT_RECALL_POINTER_THRESHOLD;
  const hits: RecallStateHit[] = [];

  const daemonResults = response && Array.isArray(response.results) ? response.results : [];
  for (const raw of daemonResults) {
    const result = (raw ?? {}) as Record<string, unknown>;
    if (String(result.layer ?? "") === "identity") continue; // boot shelf, not turn-relevant
    if (rankV2 && isDaemonResultNoise(result)) continue;
    const strength = strengthOf(result);
    if (strength === undefined || strength < effThreshold) continue;
    const { title, wikilink } = hitTitleWikilink(result);
    const rawDocId = result.id ?? result.doc_id ?? result.learning_id;
    const docId =
      typeof rawDocId === "number" || (typeof rawDocId === "string" && rawDocId.trim() !== "")
        ? rawDocId
        : undefined;
    const scope =
      typeof result.scope === "string" && result.scope.trim()
        ? result.scope.trim()
        : typeof result.source_scope === "string" && result.source_scope.trim()
          ? result.source_scope.trim()
          : typeof result.src === "string" && result.src.trim()
            ? result.src.trim()
            : "personal";
    const rawRelPath =
      typeof result.rel_path === "string" && result.rel_path.trim()
        ? result.rel_path.trim()
        : typeof result.path === "string" &&
            !path.isAbsolute(result.path) &&
            result.path.trim()
          ? result.path.trim()
          : undefined;
    const relativePath = rawRelPath ? normalizeVaultRelPath(rawRelPath) : undefined;
    const handle =
      docId !== undefined
        ? docHandleFor(scope, docId)
        : relativePath
          ? vaultHandleForPath(relativePath)
          : vaultHandleForPath(wikilink);
    const mkey =
      relativePath && (relativePath.startsWith("wiki/") || relativePath.startsWith("raw/"))
        ? `vault:${relativePath}`
        : docId !== undefined
          ? `doc:${scope.toLowerCase()}:${String(docId).trim()}`
          : `vault:${normalizeVaultRelPath(wikilink)}`;
    hits.push({
      title,
      wikilink,
      score: Number(strength.toFixed(4)),
      handle,
      mkey,
      ...(relativePath ? { relativePath } : {}),
      ...(docId !== undefined ? { scope, docId } : {}),
    });
  }

  for (const vaultHit of vaultResults) {
    const hasCalibrated =
      typeof vaultHit.weightedScore === "number" || typeof vaultHit.clearsGate === "boolean";
    let hitScore: number;
    if (rankV2 && hasCalibrated) {
      if (vaultHit.clearsGate !== true) continue;
      const weighted = vaultHit.weightedScore ?? vaultHit.gateScore ?? 0;
      if (weighted < effThreshold) continue;
      hitScore = Number(Math.max(0, Math.min(1, weighted)).toFixed(4));
    } else {
      if (vaultHit.score < VAULT_DIRECT_MATCH_SCORE) continue;
      const normalized = Math.min(1, vaultHit.score / VAULT_DIRECT_MATCH_SCORE);
      hitScore = Number(normalized.toFixed(4));
    }
    const relativePath = vaultHit.relativePath
      ? normalizeVaultRelPath(vaultHit.relativePath)
      : normalizeVaultRelPath(vaultHit.wikilink);
    const handle = vaultHandleForPath(relativePath);
    const mkey = `vault:${relativePath}`;
    hits.push({
      title: vaultHit.title || titleFromWikilink(vaultHit.wikilink),
      wikilink: vaultHit.wikilink,
      score: hitScore,
      handle,
      mkey,
      relativePath,
      ...(vaultHit.gateRoute !== undefined ? { gateRoute: vaultHit.gateRoute } : {}),
    });
  }

  if (hits.length === 0) return null;

  hits.sort((a, b) => b.score - a.score);
  const deduped: RecallStateHit[] = [];
  const seenKeys = new Set<string>();
  for (const hit of hits) {
    const key = hit.mkey ?? hit.wikilink;
    if (seenKeys.has(key)) continue;
    seenKeys.add(key);
    deduped.push(hit);
    if (deduped.length >= limit) break;
  }
  return { topScore: deduped[0].score, topHits: deduped };
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

export interface BuildRecallPointerOptions {
  /** Maximum number of hits to name in the pointer (default 3). */
  maxHits?: number;
  /** Score band below topScore within which additional hits are included (default 0.15). */
  scoreBand?: number;
}

/**
 * S3c (§5.2.6): select up to `maxHits` (default 3) top hits whose score is
 * strictly within `scoreBand` (default 0.15) of `hits[0].score`.
 */
export function selectPointerHits(
  hits: readonly RecallStateHit[],
  options?: BuildRecallPointerOptions,
): RecallStateHit[] {
  const maxHits = Math.max(1, options?.maxHits ?? 3);
  const scoreBand = options?.scoreBand ?? 0.15;
  if (!Array.isArray(hits) || hits.length === 0) return [];
  const topScore = hits[0].score;
  const selected: RecallStateHit[] = [hits[0]];
  for (let i = 1; i < hits.length && selected.length < maxHits; i += 1) {
    const hit = hits[i];
    const diff = Math.round((topScore - hit.score) * 1e6) / 1e6;
    if (diff < scoreBand) {
      selected.push(hit);
    }
  }
  return selected;
}

/**
 * The ≤ ~120-token (≤ 300 char, single-line) light pointer injected into the
 * prompt on a strong turn. Names up to 3 top hits within 0.15 of the top score
 * with their short handles (`m:<8hex>` or `d:<src>:<id>`) and scores, and tells
 * the agent to consult `minni_recall` or `minni_drill(<handle>)` before deriving
 * from scratch — a SIGNPOST, not the recall content itself.
 */
export function buildRecallPointer(
  state: StrongRecall,
  options?: BuildRecallPointerOptions,
): string {
  const selected = selectPointerHits(state.topHits, options);
  if (selected.length === 0) return "";
  const totalHits = state.topHits.length;
  const handles = selected.map((hit) => handleForHit(hit));
  const scores = selected.map((hit) => hit.score.toFixed(2));
  const h0 = handles[0];

  const renderWithTitles = (titles: readonly string[]): string => {
    if (selected.length === 1 && totalHits > 1) {
      return (
        `📓 ${totalHits} relevant memories ` +
        `(top: "${titles[0]}" (${h0}) ${scores[0]}). ` +
        `Consult minni_recall or minni_drill(${h0}) before deriving from scratch.`
      );
    }
    if (selected.length === 1) {
      return (
        `📓 1 relevant memory: "${titles[0]}" (${h0}) ${scores[0]}. ` +
        `Consult minni_recall or minni_drill(${h0}) before deriving from scratch.`
      );
    }
    const items = selected.map((_, idx) => `"${titles[idx]}" (${handles[idx]}) ${scores[idx]}`);
    return (
      `📓 ${selected.length} relevant memories: ${items.join(" · ")}. ` +
      `Consult minni_recall or minni_drill(${h0}) before deriving from scratch.`
    );
  };

  const emptyOverhead = renderWithTitles(selected.map(() => "")).length;
  const availableForTitles = Math.max(
    selected.length * 8,
    RECALL_POINTER_MAX_CHARS - emptyOverhead,
  );
  const perTitleMax = Math.max(
    8,
    Math.min(RECALL_FIELD_MAX_CHARS, Math.floor(availableForTitles / selected.length)),
  );
  const sanitizedTitles = selected.map((hit) =>
    sanitizeRecallField(hit.title, perTitleMax).replaceAll('"', "'"),
  );
  return renderWithTitles(sanitizedTitles);
}

// ---- S3c (§5.2.6, §5.2.8): Session handle registry & cross-turn suppression --

export interface SessionHandleEntry {
  handle: string;
  mkey: string;
  wikilink: string;
  title: string;
  relativePath?: string;
  scope?: string;
  docId?: number;
}

interface SessionHandlesFile {
  v: 1;
  updated_at: string;
  handles: Record<string, SessionHandleEntry>;
}

export interface SurfacedEntry {
  turn_ix: number;
  score: number;
  reset_id: string;
}

export interface SessionSurfacedState {
  sessionKey: string;
  thisTurn: number;
  resetId: string;
  surfaced: Record<string, SurfacedEntry>;
  hasExistingSurfaced: boolean;
}

interface SessionCountersFile {
  v: 1;
  turn_ix: number;
  reset_id?: string;
  surfaced?: Record<string, SurfacedEntry>;
}

function existingSessionLedgerDir(vaultPath: string, sessionKey: string): string | null {
  try {
    const dirPath = path.join(vaultPath, ".runtime", "recall-session.d", sessionKey);
    assertWriteTargetUnder(dirPath, vaultPath);
    const st = fs.lstatSync(dirPath);
    if (st.isDirectory() && !st.isSymbolicLink()) return dirPath;
  } catch {
    // Directory does not exist or failed containment.
  }
  return null;
}

function writeJsonAtomicSync(filePath: string, vaultPath: string, data: unknown): void {
  assertWriteTargetUnder(filePath, vaultPath);
  const tmpPath = `${filePath}.tmp-${process.pid}-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2)}`;
  try {
    fs.writeFileSync(tmpPath, `${JSON.stringify(data)}\n`, { encoding: "utf8", mode: 0o600 });
    fs.renameSync(tmpPath, filePath);
  } catch (err) {
    try {
      fs.rmSync(tmpPath, { force: true });
    } catch {
      // Ignore cleanup error.
    }
    throw err;
  }
}

/**
 * S3c (§5.2.6): record handles surfaced in this session so `minni_drill` can
 * resolve `m:<8hex>` and verify `d:<src>:<id>` handles against the active session.
 */
export async function recordSessionHandles(
  vaultPath: string,
  sessionId: string | undefined,
  hits: readonly RecallStateHit[],
): Promise<void> {
  if (!Array.isArray(hits) || hits.length === 0) return;
  const sKey = sessionStateKey(sessionId) ?? "default";
  try {
    const stateDir = ensureContainedDir(vaultPath, [".runtime", "recall-state.d"], true);
    if (!stateDir) return;
    const handlesPath = path.join(stateDir, `${sKey}.handles.json`);
    let existing: Record<string, SessionHandleEntry> = {};
    try {
      assertWriteTargetUnder(handlesPath, vaultPath);
      const raw = fs.readFileSync(handlesPath, "utf8");
      const parsed = JSON.parse(raw) as SessionHandlesFile;
      if (parsed && parsed.v === 1 && parsed.handles && typeof parsed.handles === "object") {
        existing = { ...parsed.handles };
      }
    } catch {
      // Start fresh.
    }
    for (const hit of hits) {
      const handle = handleForHit(hit);
      const mkey = mkeyForHit(hit);
      existing[handle] = {
        handle,
        mkey,
        wikilink: hit.wikilink,
        title: sanitizeRecallField(hit.title),
        ...(hit.relativePath ? { relativePath: normalizeVaultRelPath(hit.relativePath) } : {}),
        ...(hit.scope ? { scope: hit.scope } : {}),
        ...(typeof hit.docId === "number" ? { docId: hit.docId } : {}),
      };
    }
    const payload: SessionHandlesFile = {
      v: 1,
      updated_at: new Date().toISOString(),
      handles: existing,
    };
    writeJsonAtomicSync(handlesPath, vaultPath, payload);

    const ledgerDir = existingSessionLedgerDir(vaultPath, sKey);
    if (ledgerDir) {
      const mirrorPath = path.join(ledgerDir, "handles.json");
      writeJsonAtomicSync(mirrorPath, vaultPath, payload);
    }
  } catch {
    // Best-effort.
  }
}

/**
 * S3c (§5.2.6): look up a handle in the session handle registry.
 */
export async function readSessionHandle(
  vaultPath: string,
  sessionId: string | undefined,
  handle: string,
): Promise<SessionHandleEntry | null> {
  const normalizedHandle = handle.trim();
  if (!normalizedHandle) return null;
  const candidateKeys: string[] = [];
  const sKey = sessionStateKey(sessionId);
  if (sKey) candidateKeys.push(sKey);
  candidateKeys.push("default");

  const tryReadHandlesFile = (filePath: string): SessionHandleEntry | null => {
    try {
      assertWriteTargetUnder(filePath, vaultPath);
      const raw = fs.readFileSync(filePath, "utf8");
      const parsed = JSON.parse(raw) as SessionHandlesFile;
      if (parsed && parsed.v === 1 && parsed.handles && typeof parsed.handles === "object") {
        const match = parsed.handles[normalizedHandle] ?? parsed.handles[normalizedHandle.toLowerCase()];
        if (match && typeof match === "object") return match;
      }
    } catch {
      // Ignore missing or unreadable file.
    }
    return null;
  };

  for (const key of candidateKeys) {
    const ledgerDir = existingSessionLedgerDir(vaultPath, key);
    if (ledgerDir) {
      const fromLedger = tryReadHandlesFile(path.join(ledgerDir, "handles.json"));
      if (fromLedger) return fromLedger;
    }
    const stateFile = path.join(vaultPath, RECALL_STATE_DIR_RELPATH, `${key}.handles.json`);
    const fromState = tryReadHandlesFile(stateFile);
    if (fromState) return fromState;
  }

  // Fallback scan when sessionId was not provided by the caller: check any
  // session handle file in .runtime/recall-state.d.
  if (!sKey) {
    try {
      const dirPath = path.join(vaultPath, RECALL_STATE_DIR_RELPATH);
      assertWriteTargetUnder(dirPath, vaultPath);
      for (const name of fs.readdirSync(dirPath)) {
        if (!name.endsWith(".handles.json")) continue;
        const found = tryReadHandlesFile(path.join(dirPath, name));
        if (found) return found;
      }
    } catch {
      // Ignore scan errors.
    }
  }
  return null;
}

const SUPPRESSION_TURN_WINDOW = 5;
const SUPPRESSION_SCORE_JUMP = 0.1;

function readResetMarkerId(vaultPath: string, sessionKey: string): string | null {
  const ledgerDir = existingSessionLedgerDir(vaultPath, sessionKey);
  if (!ledgerDir) return null;
  try {
    const resetPath = path.join(ledgerDir, "main.reset");
    assertWriteTargetUnder(resetPath, vaultPath);
    const raw = fs.readFileSync(resetPath, "utf8");
    const parsed = JSON.parse(raw) as { reset_id?: unknown };
    if (parsed && typeof parsed.reset_id === "string" && parsed.reset_id.trim()) {
      return parsed.reset_id.trim();
    }
  } catch {
    // Absent or unreadable.
  }
  return null;
}

/**
 * S3c (§5.2.8): read the session's current turn counter, reset epoch, and
 * recently surfaced memory map.
 */
export async function readSessionSurfacedState(
  vaultPath: string,
  sessionId: string | undefined,
  knownTurnIx?: number,
): Promise<SessionSurfacedState> {
  const sessionKey = sessionStateKey(sessionId) ?? "default";
  let prevTurnIx = 0;
  let stateResetId = "0";
  let surfaced: Record<string, SurfacedEntry> = {};
  let hasExistingSurfaced = false;

  const stateCountersPath = path.join(vaultPath, RECALL_STATE_DIR_RELPATH, `${sessionKey}.counters.json`);
  try {
    assertWriteTargetUnder(stateCountersPath, vaultPath);
    const raw = fs.readFileSync(stateCountersPath, "utf8");
    const parsed = JSON.parse(raw) as SessionCountersFile;
    if (parsed && parsed.v === 1 && Number.isSafeInteger(parsed.turn_ix) && parsed.turn_ix >= 0) {
      prevTurnIx = parsed.turn_ix;
      if (typeof parsed.reset_id === "string" && parsed.reset_id.trim()) {
        stateResetId = parsed.reset_id.trim();
      }
      if (parsed.surfaced && typeof parsed.surfaced === "object") {
        surfaced = { ...parsed.surfaced };
        hasExistingSurfaced = Object.keys(surfaced).length > 0;
      }
    }
  } catch {
    // Absent or unreadable.
  }

  const ledgerDir = existingSessionLedgerDir(vaultPath, sessionKey);
  if (ledgerDir) {
    try {
      const ledgerCountersPath = path.join(ledgerDir, "counters.json");
      assertWriteTargetUnder(ledgerCountersPath, vaultPath);
      const raw = fs.readFileSync(ledgerCountersPath, "utf8");
      const parsed = JSON.parse(raw) as SessionCountersFile;
      if (parsed && parsed.v === 1 && Number.isSafeInteger(parsed.turn_ix) && parsed.turn_ix >= 0) {
        if (parsed.surfaced && typeof parsed.surfaced === "object") {
          surfaced = { ...parsed.surfaced, ...surfaced };
          hasExistingSurfaced = Object.keys(surfaced).length > 0;
        }
      }
    } catch {
      // Ignore.
    }
  }

  const markerResetId = readResetMarkerId(vaultPath, sessionKey);
  const resetId = markerResetId ?? stateResetId;
  const thisTurn =
    typeof knownTurnIx === "number" && Number.isSafeInteger(knownTurnIx) && knownTurnIx >= 1
      ? knownTurnIx
      : prevTurnIx + 1;

  return {
    sessionKey,
    thisTurn,
    resetId,
    surfaced,
    hasExistingSurfaced,
  };
}

/**
 * S3c (§5.2.8): return true when `hit` was already delivered in a pointer within
 * the last 5 turns of the same reset epoch and its score has not jumped by >= 0.10.
 */
export function shouldSuppressHit(hit: RecallStateHit, state: SessionSurfacedState): boolean {
  const mkey = mkeyForHit(hit);
  const entry = state.surfaced[mkey];
  if (!entry || typeof entry !== "object") return false;
  if (entry.reset_id !== state.resetId) return false;
  const turnDiff = state.thisTurn - entry.turn_ix;
  if (turnDiff < 1 || turnDiff > SUPPRESSION_TURN_WINDOW) return false;
  const scoreGain = Math.round((hit.score - entry.score) * 1e6) / 1e6;
  return scoreGain < SUPPRESSION_SCORE_JUMP;
}

function mirrorSurfacedToLedgerCounters(
  vaultPath: string,
  sessionKey: string,
  thisTurn: number,
  surfaced: Record<string, SurfacedEntry>,
): void {
  const ledgerDir = existingSessionLedgerDir(vaultPath, sessionKey);
  if (!ledgerDir) return;
  const countersPath = path.join(ledgerDir, "counters.json");
  try {
    assertWriteTargetUnder(countersPath, vaultPath);
    if (!fs.existsSync(countersPath)) return;
    const raw = fs.readFileSync(countersPath, "utf8");
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    // Never overwrite a corrupt counters.json (preserves F8 review invariant).
    if (
      !parsed ||
      parsed.v !== 1 ||
      typeof parsed.turn_ix !== "number" ||
      !Number.isSafeInteger(parsed.turn_ix) ||
      parsed.turn_ix < 0
    ) {
      return;
    }
    writeJsonAtomicSync(countersPath, vaultPath, {
      ...parsed,
      turn_ix: Math.max(parsed.turn_ix, thisTurn),
      surfaced,
    });
  } catch {
    // Best-effort.
  }
}

/**
 * S3c (§5.2.8): commit the hits named by a DELIVERED pointer into the session's
 * surfaced map and advance the session turn counter. Registered via
 * `deferUntilDelivered` so an undelivered/dropped pointer never suppresses a
 * subsequent turn.
 */
export async function commitSurfacedPointer(
  vaultPath: string,
  sessionId: string | undefined,
  state: SessionSurfacedState,
  surfacedHits: readonly RecallStateHit[],
): Promise<void> {
  const sKey = sessionStateKey(sessionId) ?? state.sessionKey;
  try {
    const stateDir = ensureContainedDir(vaultPath, [".runtime", "recall-state.d"], true);
    if (!stateDir) return;
    const nextSurfaced: Record<string, SurfacedEntry> = {};
    for (const [key, entry] of Object.entries(state.surfaced)) {
      if (
        entry &&
        entry.reset_id === state.resetId &&
        state.thisTurn - entry.turn_ix <= SUPPRESSION_TURN_WINDOW
      ) {
        nextSurfaced[key] = entry;
      }
    }
    for (const hit of surfacedHits) {
      const mkey = mkeyForHit(hit);
      nextSurfaced[mkey] = {
        turn_ix: state.thisTurn,
        score: hit.score,
        reset_id: state.resetId,
      };
    }
    const payload: SessionCountersFile = {
      v: 1,
      turn_ix: state.thisTurn,
      reset_id: state.resetId,
      surfaced: nextSurfaced,
    };
    const stateCountersPath = path.join(stateDir, `${sKey}.counters.json`);
    writeJsonAtomicSync(stateCountersPath, vaultPath, payload);
    mirrorSurfacedToLedgerCounters(vaultPath, sKey, state.thisTurn, nextSurfaced);
  } catch {
    // Best-effort.
  }
}

/**
 * S3c (§5.2.8): advance the session turn counter on a delivered turn where all
 * hits were suppressed (or weak), so the 5-turn suppression window progresses.
 * Only writes when a counters file already exists for the session.
 */
export async function advanceSessionTurnIfPresent(
  vaultPath: string,
  sessionId: string | undefined,
  state: SessionSurfacedState,
): Promise<void> {
  const sKey = sessionStateKey(sessionId) ?? state.sessionKey;
  try {
    const stateCountersPath = path.join(vaultPath, RECALL_STATE_DIR_RELPATH, `${sKey}.counters.json`);
    assertWriteTargetUnder(stateCountersPath, vaultPath);
    if (!fs.existsSync(stateCountersPath)) return;
    const nextSurfaced: Record<string, SurfacedEntry> = {};
    for (const [key, entry] of Object.entries(state.surfaced)) {
      if (
        entry &&
        entry.reset_id === state.resetId &&
        state.thisTurn - entry.turn_ix <= SUPPRESSION_TURN_WINDOW
      ) {
        nextSurfaced[key] = entry;
      }
    }
    const payload: SessionCountersFile = {
      v: 1,
      turn_ix: state.thisTurn,
      reset_id: state.resetId,
      surfaced: nextSurfaced,
    };
    writeJsonAtomicSync(stateCountersPath, vaultPath, payload);
    mirrorSurfacedToLedgerCounters(vaultPath, sKey, state.thisTurn, nextSurfaced);
  } catch {
    // Best-effort.
  }
}

/**
 * S3c (§5.2.8): clear cross-turn suppression state on SessionStart / PreCompact /
 * PostCompact. Only updates existing counter files so a reset with the ledger
 * off creates no new directories or files.
 */
export async function clearSessionSurfacedOnReset(
  vaultPath: string,
  sessionId: string | undefined,
): Promise<void> {
  const sKey = sessionStateKey(sessionId) ?? "default";
  const nextResetId = randomBytes(8).toString("hex");
  try {
    const stateCountersPath = path.join(vaultPath, RECALL_STATE_DIR_RELPATH, `${sKey}.counters.json`);
    assertWriteTargetUnder(stateCountersPath, vaultPath);
    if (fs.existsSync(stateCountersPath)) {
      const raw = fs.readFileSync(stateCountersPath, "utf8");
      const parsed = JSON.parse(raw) as SessionCountersFile;
      const turnIx =
        parsed && parsed.v === 1 && Number.isSafeInteger(parsed.turn_ix) && parsed.turn_ix >= 0
          ? parsed.turn_ix
          : 0;
      writeJsonAtomicSync(stateCountersPath, vaultPath, {
        v: 1,
        turn_ix: turnIx,
        reset_id: nextResetId,
        surfaced: {},
      });
    }
  } catch {
    // Ignore.
  }
  mirrorSurfacedToLedgerCounters(vaultPath, sKey, 0, {});
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
