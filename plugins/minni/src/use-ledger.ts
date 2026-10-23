// The recall-to-use ledger (design rev5 §4.2, §4.3).
//
// Problem this solves: the hooks audit what they FOUND (vault match paths, a
// top score, "hits: N"), never what the model was SHOWN or whether it used it.
// Without that, "did recall help?" has no denominator and no numerator. The
// ledger is a per-session, append-only record of turns, surfacings, judgments,
// resets and exposures, keyed so the same memory is recognisable across
// channels and sessions.
//
// Layout: <vault>/.runtime/use-ledger/<session_key>/<yyyy-mm-dd>.jsonl, one
// JSON row per line. The writing discipline is what makes it safe under the
// concurrency it lives with (a main thread and its subagents share a session):
//   - each row is ONE O_APPEND write() of at most 4 KB, so concurrent writers
//     interleave whole rows, never bytes within one;
//   - an over-size row is clipped (hash lists dropped) or rejected, never split;
//   - every directory component is checked before it is created or followed,
//     and every file through assertWriteTargetUnder, so a symlinked .runtime
//     cannot redirect a write out of the vault;
//   - rows never go through recordAudit, whose 5 s throttle would drop them;
//   - nothing here throws out of a hook: a refused or failed write is a count.
//
// Privacy: rows hold keys, hashes and sanitized titles. Session and agent ids
// are hashed before they reach a row or a path, and no memory text is stored.
//
// Writes are synchronous on purpose: they are a few small appends, and a sync
// write has finished before an exit path's process.exit can cut it off.
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { featuresInEffect, flagOn } from "./flags.js";
import { deferUntilDelivered } from "./hook-delivery.js";
import { sanitizeRecallField } from "./recall-state.js";
import { assertWriteTargetUnder } from "./vault.js";

export const LEDGER_VERSION = 5;
/** Ledger root under the vault. */
export const LEDGER_RELDIR = path.join(".runtime", "use-ledger");
/** Hard bound on one row, newline included. */
export const MAX_ROW_BYTES = 4096;
/** What a host keeps in context of an over-cap string (claude-code, probe P6). */
export const HOST_PREVIEW_CHARS = 2000;
export const SHINGLE_WORDS = 8;
export const MAX_SHINGLES = 64;
const TITLE_MAX = 120;
/** Text past this is not shingled: bounds the hashing a hook can be made to do. */
const MAX_SHINGLE_INPUT_CHARS = 200_000;
/** A day file larger than this is not read (rows are ≤ 4 KB; this is not a ledger). */
const MAX_LEDGER_FILE_BYTES = 32 * 1024 * 1024;

export type LedgerKind = "turn" | "surfaced" | "judged" | "reset" | "exposure";
const KINDS: ReadonlySet<string> = new Set<LedgerKind>(["turn", "surfaced", "judged", "reset", "exposure"]);

export type SurfaceChannel =
  | "boot_recall"
  | "boot_recent_learnings"
  | "prompt_pointer"
  | "guard"
  | "explicit_recall"
  | "subagent_brief";

// ---------------------------------------------------------------------------
// Hashing
// ---------------------------------------------------------------------------

function hexDigest(algorithm: "sha1" | "sha256", text: string): string {
  return createHash(algorithm).update(text, "utf8").digest("hex");
}

/**
 * Deterministic id over several parts: sha256 of their JSON array, truncated.
 * JSON (not a separator join) so no part can forge a boundary.
 */
export function idHash(parts: ReadonlyArray<string | number>, length = 16): string {
  return hexDigest("sha256", JSON.stringify(parts)).slice(0, length);
}

// ---------------------------------------------------------------------------
// Paths and containment
// ---------------------------------------------------------------------------

function errorCode(error: unknown): unknown {
  return (error as { code?: unknown } | null)?.code;
}

function isInside(realTarget: string, realRoot: string): boolean {
  const rel = path.relative(realRoot, realTarget);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

/**
 * Real path of `p`, even when it does not exist yet: the nearest existing
 * ancestor is resolved and the missing tail appended. So a file keyed before it
 * is created keys the same afterwards, and a symlinked ancestor is followed.
 */
function realish(p: string): string {
  const absolute = path.resolve(p);
  const tail: string[] = [];
  let head = absolute;
  for (;;) {
    try {
      return path.join(fs.realpathSync(head), ...tail);
    } catch {
      const parent = path.dirname(head);
      if (parent === head) return absolute;
      tail.unshift(path.basename(head));
      head = parent;
    }
  }
}

/**
 * Vault-relative POSIX path of an absolute `candidate` when it is a file inside
 * the vault, judged on real paths (a symlink in the vault that points outside
 * is outside). A path that exists but is not a regular file is not a memory.
 */
function fileInsideVault(candidate: string, vaultPath: string): string | null {
  if (!path.isAbsolute(candidate) || candidate.includes("\0")) return null;
  let root: string;
  try {
    root = fs.realpathSync(vaultPath);
  } catch {
    return null;
  }
  const real = realish(candidate);
  if (!isInside(real, root)) return null;
  const rel = path.relative(root, real);
  if (rel === "") return null;
  try {
    if (!fs.statSync(real).isFile()) return null;
  } catch (error) {
    // Missing (a stale index row) still names this vault's note; anything else
    // (EACCES, ELOOP) cannot be judged.
    if (errorCode(error) !== "ENOENT") return null;
  }
  return rel.split(path.sep).join("/");
}

const SESSION_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const DAY_FILE_RE = /^\d{4}-\d{2}-\d{2}\.jsonl$/;

function dayOf(ts: unknown): string | null {
  if (typeof ts !== "string") return null;
  const ms = Date.parse(ts);
  if (!Number.isFinite(ms)) return null;
  const day = new Date(ms).toISOString().slice(0, 10);
  return DAY_RE.test(day) ? day : null;
}

/**
 * Absolute path of the day file a row with this session key and timestamp goes
 * to (UTC day). Throws on a session key that is not a single safe path
 * component, or a timestamp that is not a date.
 */
export function ledgerFilePath(vaultPath: string, sessionKey: string, ts: string): string {
  if (typeof sessionKey !== "string" || !SESSION_KEY_RE.test(sessionKey)) {
    throw new Error("use-ledger: invalid session key");
  }
  const day = dayOf(ts);
  if (!day) throw new Error("use-ledger: invalid row timestamp");
  return path.join(vaultPath, LEDGER_RELDIR, sessionKey, `${day}.jsonl`);
}

/**
 * Walk `segments` below the vault, refusing any component that is a symlink
 * resolving outside it or is not a directory. With `create`, missing
 * components are made one at a time, so nothing is ever created beyond a
 * component that failed the check. Returns the directory path, or null when it
 * is missing and `create` is false. Throws on refusal.
 */
function containedDir(vaultPath: string, segments: readonly string[], create: boolean): string | null {
  const root = fs.realpathSync(vaultPath);
  let current = vaultPath;
  for (const segment of segments) {
    current = path.join(current, segment);
    let stat: fs.Stats | undefined;
    try {
      stat = fs.lstatSync(current);
    } catch (error) {
      if (errorCode(error) !== "ENOENT") throw error;
    }
    if (!stat) {
      if (!create) return null;
      try {
        fs.mkdirSync(current, { mode: 0o700 });
      } catch (error) {
        if (errorCode(error) !== "EEXIST") throw error;
      }
      stat = fs.lstatSync(current);
    }
    if (stat.isSymbolicLink()) {
      if (!isInside(fs.realpathSync(current), root)) {
        throw new Error("use-ledger: path escapes the vault");
      }
      stat = fs.statSync(current);
    }
    if (!stat.isDirectory()) throw new Error("use-ledger: path component is not a directory");
  }
  return current;
}

function ledgerSegments(sessionKey: string): string[] {
  return [...LEDGER_RELDIR.split(path.sep), sessionKey];
}

// ---------------------------------------------------------------------------
// Keys and handles (§4.2)
// ---------------------------------------------------------------------------

/** What a surfaced memory is, as the caller knows it. */
export type MemoryRef =
  /** A vault hit: its path, vault-relative or absolute. */
  | { kind: "vault"; path: string }
  /** A daemon result: its index tag, doc id, and `source` (wikilink is never used). */
  | { kind: "daemon"; src?: unknown; doc_id?: unknown; source?: unknown; wikilink?: unknown }
  /** A daemon learning. */
  | { kind: "learning"; learning_id: unknown }
  /** A boot learning line the daemon sent no id for (version skew). */
  | { kind: "boot_line"; line: unknown };

const INDEX_TAG_RE = /^[A-Za-z0-9_-]{1,32}$/;
const ID_RE = /^[A-Za-z0-9_.-]{1,128}$/;

function indexTag(value: unknown): string | null {
  return typeof value === "string" && INDEX_TAG_RE.test(value) ? value : null;
}

function idString(value: unknown): string | null {
  if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? String(value) : null;
  return typeof value === "string" && ID_RE.test(value) ? value : null;
}

/**
 * The one key for a memory, or null when it cannot be keyed (counted
 * `unnameable` downstream, never guessed):
 *   - `vault:<vault-relative path>` for vault hits, and for daemon results whose
 *     `source` is a file inside THIS vault. Matched on `source`, never on the
 *     wikilink text: `_path_to_wikilink` emits the same `[[wiki/…]]` for notes
 *     from different vaults.
 *   - `doc:<src>:<doc_id>` for other daemon documents. A doc id is unique only
 *     within one index, so the index tag is part of the key.
 *   - `learning:<learning_id>` for daemon learnings.
 *   - `lhash:<sha1(line)[:12]>` for a boot learning line (trimmed) with no id.
 */
export function memoryKeyFor(ref: MemoryRef, vaultPath: string): string | null {
  switch (ref?.kind) {
    case "vault": {
      if (typeof ref.path !== "string" || ref.path.length === 0) return null;
      const absolute = path.isAbsolute(ref.path) ? ref.path : path.join(vaultPath, ref.path);
      const rel = fileInsideVault(absolute, vaultPath);
      return rel ? `vault:${rel}` : null;
    }
    case "daemon": {
      if (typeof ref.source === "string" && path.isAbsolute(ref.source)) {
        const rel = fileInsideVault(ref.source, vaultPath);
        if (rel) return `vault:${rel}`;
      }
      const src = indexTag(ref.src);
      const id = idString(ref.doc_id);
      return src && id ? `doc:${src}:${id}` : null;
    }
    case "learning": {
      const id = idString(ref.learning_id);
      return id ? `learning:${id}` : null;
    }
    case "boot_line": {
      const line = typeof ref.line === "string" ? ref.line.trim() : "";
      return line ? `lhash:${hexDigest("sha1", line).slice(0, 12)}` : null;
    }
    default:
      return null;
  }
}

/** `m:<sha1(vault-relative path)[:8]>`, resolved by the plugin. */
export function vaultHandle(relPath: string): string {
  return `m:${hexDigest("sha1", relPath).slice(0, 8)}`;
}

/** `d:<src>:<doc_id>`; the daemon picks the index by `src`. Null if unkeyable. */
export function docHandle(src: unknown, docId: unknown): string | null {
  const tag = indexTag(src);
  const id = idString(docId);
  return tag && id ? `d:${tag}:${id}` : null;
}

/** The handles an agent can pass back to `minni_drill` for an mkey. */
export function handlesForKey(mkey: string): string[] {
  if (typeof mkey !== "string") return [];
  if (mkey.startsWith("vault:") && mkey.length > "vault:".length) return [vaultHandle(mkey.slice("vault:".length))];
  const doc = /^doc:([^:]+):(.+)$/.exec(mkey);
  if (doc) {
    const handle = docHandle(doc[1], doc[2]);
    return handle ? [handle] : [];
  }
  return [];
}

type Payload = Record<string, unknown> | null | undefined;

function field(payload: Payload, name: string): unknown {
  return payload && typeof payload === "object" ? (payload as Record<string, unknown>)[name] : undefined;
}

/**
 * `sha256(session_id)[:16]`; else `tx-` plus a hash of the transcript's real
 * path; else `unlabeled` (which stays out of every rate).
 */
export function sessionKeyFrom(payload: Payload): string {
  const sessionId = field(payload, "session_id");
  if (typeof sessionId === "string" && sessionId.length > 0) {
    return hexDigest("sha256", sessionId).slice(0, 16);
  }
  const transcript = field(payload, "transcript_path");
  if (typeof transcript === "string" && transcript.length > 0 && !transcript.includes("\0")) {
    return `tx-${hexDigest("sha256", realish(transcript)).slice(0, 16)}`;
  }
  return "unlabeled";
}

/**
 * One spelling of a subagent id from either source. The host's `agent_id`
 * reads `agent-<id>` and the subagent's transcript is `agent-<id>.jsonl`, so
 * both reduce to `<id>` and key the same agent identically.
 */
export function canonicalAgentId(value: string): string {
  return value.replace(/\.jsonl$/, "").replace(/^agent-/, "");
}

function subagentKey(raw: string): string {
  // A bare "agent-" or ".jsonl" canonicalizes to nothing; it still names a
  // subagent, so hash the raw spelling rather than fall back to `main`.
  const id = canonicalAgentId(raw) || raw;
  return `a-${hexDigest("sha256", id).slice(0, 12)}`;
}

/**
 * `main` on the main thread; `a-<sha256(canonicalAgentId(id))[:12]>` for a
 * subagent. A subagent is a payload with `agent_id` (probe P5), or one whose
 * `transcript_path` sits under a `subagents/` directory, in which case the id
 * is the file basename. Both give the same key for the same subagent.
 */
export function agentKeyFrom(payload: Payload): string {
  const agentId = field(payload, "agent_id");
  if (typeof agentId === "string" && agentId.length > 0) return subagentKey(agentId);
  const transcript = field(payload, "transcript_path");
  if (typeof transcript === "string") {
    const parts = transcript.split(/[\\/]+/);
    const base = parts.pop();
    if (base && parts.includes("subagents")) return subagentKey(base);
  }
  return "main";
}

// ---------------------------------------------------------------------------
// Rows (§4.3)
// ---------------------------------------------------------------------------

/** Which build wrote a row. `built_from` is left out: it is an absolute path. */
export interface BuildStamp {
  version: string;
  git_sha: string;
  git_dirty: boolean;
  built_at: string;
}

let buildCache: BuildStamp | null | undefined;

function boundedString(value: unknown, max: number): string {
  return typeof value === "string" ? value.slice(0, max) : "";
}

/** `dist/build-manifest.json`, read once per process; null when absent or malformed. */
export function buildStamp(): BuildStamp | null {
  if (buildCache !== undefined) return buildCache;
  buildCache = null;
  try {
    const file = fileURLToPath(new URL("./build-manifest.json", import.meta.url));
    const raw = JSON.parse(fs.readFileSync(file, "utf8")) as Record<string, unknown> | null;
    if (raw && typeof raw === "object" && typeof raw.version === "string" && typeof raw.git_sha === "string") {
      buildCache = {
        version: boundedString(raw.version, 64),
        git_sha: boundedString(raw.git_sha, 64),
        git_dirty: raw.git_dirty === true,
        built_at: boundedString(raw.built_at, 40),
      };
    }
  } catch {
    // No manifest (an unbuilt tree, a stripped install): rows say build: null.
  }
  return buildCache;
}

/** Who wrote a row, and under which switches. */
export interface LedgerContext {
  session_key: string;
  agent_key: string;
  wire: string;
  principal: string;
  prompt_id?: string;
  subagent_type?: string;
  /** Kill switches in effect (flags that are on). */
  features: readonly string[];
}

export interface LedgerCommon {
  v: typeof LEDGER_VERSION;
  kind: LedgerKind;
  ts: string;
  session_key: string;
  agent_key: string;
  wire: string;
  principal: string;
  prompt_id?: string;
  subagent_type?: string;
  features: string[];
  build: BuildStamp | null;
}

export interface TurnFields {
  turn_ix: number;
  envelope_kind: string | null;
  subkind: string | null;
  exit: string;
}

export interface SurfacedFields {
  channel: SurfaceChannel;
  mkey: string;
  handles?: readonly string[];
  title?: string;
  rank: number;
  score?: number;
  route?: string;
  turn_ix?: number;
  visible: boolean;
  delivered: boolean;
  cap_unknown?: boolean;
  tx_off?: number;
  shingles?: readonly string[];
  echo?: readonly string[];
  /** explicit_recall only: keys the sid so re-reading a transcript cannot duplicate it. */
  tool_use_id?: string;
}

export interface JudgedFields {
  sid: string;
  outcome: string;
  via: string;
  evidence: unknown;
  title_assessable: boolean;
  final: boolean;
  rule_version: string | number;
}

export interface ResetFields {
  /** The hook event that reset this agent's context (PreCompact, PostCompact, SessionStart). */
  event: string;
  reset_id?: string;
}

export interface ExposureFields {
  reset_id: string;
  mkey: string;
  first_sid: string;
  channel: SurfaceChannel;
  visible: boolean;
  outcome: string;
  credits: readonly number[];
  final: boolean;
}

export type TurnRow = LedgerCommon & { kind: "turn" } & TurnFields;
export type SurfacedRow = LedgerCommon & {
  kind: "surfaced";
  sid: string;
  channel: SurfaceChannel;
  mkey: string;
  handles: string[];
  title: string;
  rank: number;
  score?: number;
  route?: string;
  turn_ix?: number;
  visible: boolean;
  delivered: boolean;
  cap_unknown?: true;
  tx_off?: number;
  shingles?: string[];
  echo?: string[];
  clipped?: true;
};
export type JudgedRow = LedgerCommon & { kind: "judged" } & JudgedFields;
export type ResetRow = LedgerCommon & { kind: "reset"; event: string; reset_id: string };
export type ExposureRow = LedgerCommon & {
  kind: "exposure";
  exposure_id: string;
  reset_id: string;
  mkey: string;
  first_sid: string;
  channel: SurfaceChannel;
  visible: boolean;
  outcome: string;
  credits: number[];
  final: boolean;
};
export type LedgerRow = TurnRow | SurfacedRow | JudgedRow | ResetRow | ExposureRow;

const SHORT_FIELD_MAX = 128;

function shortField(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length === 0) return undefined;
  const clean = sanitizeRecallField(value, SHORT_FIELD_MAX);
  return clean.length > 0 ? clean : undefined;
}

/**
 * The row context for a hook payload. `features` defaults to the flags in
 * effect for `vaultPath` (empty when neither is given).
 */
export function ledgerContextFrom(
  payload: Payload,
  options: { wire: string; principal: string; vaultPath?: string; features?: readonly string[] },
): LedgerContext {
  const context: LedgerContext = {
    session_key: sessionKeyFrom(payload),
    agent_key: agentKeyFrom(payload),
    wire: options.wire,
    principal: options.principal,
    features: options.features ?? (options.vaultPath ? featuresInEffect(options.vaultPath) : []),
  };
  const promptId = shortField(field(payload, "prompt_id"));
  if (promptId) context.prompt_id = promptId;
  const subagentType = shortField(field(payload, "agent_type") ?? field(payload, "subagent_type"));
  if (subagentType) context.subagent_type = subagentType;
  return context;
}

function common<K extends LedgerKind>(context: LedgerContext, kind: K, ts: string): LedgerCommon & { kind: K } {
  // Built in §4.3's field order (JSON keeps insertion order), optional fields
  // in their place before features/build.
  const head = {
    v: LEDGER_VERSION,
    kind,
    ts,
    session_key: context.session_key,
    agent_key: context.agent_key,
    wire: context.wire,
    principal: context.principal,
  } as LedgerCommon & { kind: K };
  if (context.prompt_id) head.prompt_id = context.prompt_id;
  if (context.subagent_type) head.subagent_type = context.subagent_type;
  head.features = [...context.features];
  head.build = buildStamp();
  return head;
}

function nowIso(): string {
  return new Date().toISOString();
}

export function buildTurnRow(context: LedgerContext, fields: TurnFields, ts: string = nowIso()): TurnRow {
  return {
    ...common(context, "turn", ts),
    turn_ix: fields.turn_ix,
    envelope_kind: fields.envelope_kind,
    subkind: fields.subkind,
    exit: fields.exit,
  };
}

/**
 * `sid = sha256(session_key, ts, channel, mkey, rank)[:16]`; explicit_recall
 * rows use `sha256(tool_use_id, mkey)[:16]` so a transcript re-read is idempotent.
 */
export function surfacedSid(args: {
  session_key: string;
  ts: string;
  channel: SurfaceChannel;
  mkey: string;
  rank: number;
  tool_use_id?: string;
}): string {
  if (args.channel === "explicit_recall" && typeof args.tool_use_id === "string" && args.tool_use_id.length > 0) {
    return idHash([args.tool_use_id, args.mkey]);
  }
  return idHash([args.session_key, args.ts, args.channel, args.mkey, args.rank]);
}

function hashList(values: readonly string[] | undefined): string[] | undefined {
  if (!Array.isArray(values)) return undefined;
  return values.filter((value) => typeof value === "string");
}

export function buildSurfacedRow(
  context: LedgerContext,
  fields: SurfacedFields,
  ts: string = nowIso(),
): SurfacedRow {
  const row: SurfacedRow = {
    ...common(context, "surfaced", ts),
    sid: surfacedSid({
      session_key: context.session_key,
      ts,
      channel: fields.channel,
      mkey: fields.mkey,
      rank: fields.rank,
      tool_use_id: fields.tool_use_id,
    }),
    channel: fields.channel,
    mkey: fields.mkey,
    handles: (fields.handles ?? []).filter((handle) => typeof handle === "string").slice(0, 8),
    title: sanitizeRecallField(fields.title ?? "", TITLE_MAX),
    rank: fields.rank,
    visible: fields.visible,
    delivered: fields.delivered,
  };
  if (typeof fields.score === "number" && Number.isFinite(fields.score)) row.score = fields.score;
  const route = shortField(fields.route);
  if (route) row.route = route;
  if (typeof fields.turn_ix === "number") row.turn_ix = fields.turn_ix;
  if (fields.cap_unknown === true) row.cap_unknown = true;
  if (typeof fields.tx_off === "number" && Number.isFinite(fields.tx_off)) row.tx_off = fields.tx_off;
  const shingles = hashList(fields.shingles);
  if (shingles) row.shingles = shingles;
  const echo = hashList(fields.echo);
  if (echo) row.echo = echo;
  return row;
}

export function buildJudgedRow(context: LedgerContext, fields: JudgedFields, ts: string = nowIso()): JudgedRow {
  return {
    ...common(context, "judged", ts),
    sid: fields.sid,
    outcome: fields.outcome,
    via: fields.via,
    evidence: fields.evidence,
    title_assessable: fields.title_assessable,
    final: fields.final,
    rule_version: fields.rule_version,
  };
}

/** A reset of the payload's OWN agent key; `reset_id` defaults to a hash of who/when/why. */
export function buildResetRow(context: LedgerContext, fields: ResetFields, ts: string = nowIso()): ResetRow {
  return {
    ...common(context, "reset", ts),
    event: fields.event,
    reset_id: fields.reset_id ?? idHash([context.session_key, context.agent_key, ts, fields.event]),
  };
}

/** `exposure_id = sha256(session_key, agent_key, reset_id, mkey)[:16]`. */
export function exposureIdFor(sessionKey: string, agentKey: string, resetId: string, mkey: string): string {
  return idHash([sessionKey, agentKey, resetId, mkey]);
}

export function buildExposureRow(
  context: LedgerContext,
  fields: ExposureFields,
  ts: string = nowIso(),
): ExposureRow {
  return {
    ...common(context, "exposure", ts),
    exposure_id: exposureIdFor(context.session_key, context.agent_key, fields.reset_id, fields.mkey),
    reset_id: fields.reset_id,
    mkey: fields.mkey,
    first_sid: fields.first_sid,
    channel: fields.channel,
    visible: fields.visible,
    outcome: fields.outcome,
    credits: [...fields.credits],
    final: fields.final,
  };
}

// ---------------------------------------------------------------------------
// Append and read
// ---------------------------------------------------------------------------

export interface LedgerAppendResult {
  written: number;
  /** Rows not written: refused, rejected as over-size, or failed to write. */
  failed: number;
  /** Rows written with their hash lists dropped to fit MAX_ROW_BYTES. */
  clipped: number;
  /** MINNI_USE_LEDGER is off: nothing was attempted. */
  disabled: boolean;
}

let failuresThisProcess = 0;

/** Rows this process failed to write, for the hook to audit after delivery. */
export function ledgerFailureCount(): number {
  return failuresThisProcess;
}

interface EncodedRow {
  bytes: Buffer;
  clipped: boolean;
}

/** One row as one line within MAX_ROW_BYTES, clipping hash lists if needed; null if it cannot fit. */
function encodeRow(row: LedgerRow): EncodedRow | null {
  const full = Buffer.from(`${JSON.stringify(row)}\n`, "utf8");
  if (full.length <= MAX_ROW_BYTES) return { bytes: full, clipped: false };
  const slim: Record<string, unknown> = { ...row, clipped: true };
  delete slim.shingles;
  delete slim.echo;
  const clipped = Buffer.from(`${JSON.stringify(slim)}\n`, "utf8");
  return clipped.length <= MAX_ROW_BYTES ? { bytes: clipped, clipped: true } : null;
}

const OPEN_FLAGS =
  fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_CREAT | (fs.constants.O_NOFOLLOW ?? 0);

/**
 * Append rows to their session/day files: one write() per row. Never throws;
 * the result counts what was written and what was not.
 */
export function appendLedgerRows(vaultPath: string, rows: readonly LedgerRow[]): LedgerAppendResult {
  const result: LedgerAppendResult = { written: 0, failed: 0, clipped: 0, disabled: false };
  try {
    if (!Array.isArray(rows) || rows.length === 0) return result;
    if (!flagOn(vaultPath, "MINNI_USE_LEDGER")) {
      result.disabled = true;
      return result;
    }
    // Group by file, keeping row order within each.
    const groups = new Map<string, { sessionKey: string; lines: EncodedRow[] }>();
    for (const row of rows) {
      try {
        const file = ledgerFilePath(vaultPath, row.session_key, row.ts);
        const encoded = encodeRow(row);
        if (!encoded) {
          result.failed += 1;
          continue;
        }
        const group = groups.get(file) ?? { sessionKey: row.session_key, lines: [] as EncodedRow[] };
        group.lines.push(encoded);
        groups.set(file, group);
      } catch {
        result.failed += 1;
      }
    }
    for (const [file, group] of groups) {
      let fd: number | undefined;
      let attempted = 0;
      try {
        containedDir(vaultPath, ledgerSegments(group.sessionKey), true);
        assertWriteTargetUnder(file, vaultPath);
        fd = fs.openSync(file, OPEN_FLAGS, 0o600);
        for (const line of group.lines) {
          attempted += 1;
          try {
            const n = fs.writeSync(fd, line.bytes, 0, line.bytes.length);
            if (n === line.bytes.length) {
              result.written += 1;
              if (line.clipped) result.clipped += 1;
            } else {
              // A short write leaves a torn line, which readers skip.
              result.failed += 1;
            }
          } catch {
            result.failed += 1;
          }
        }
      } catch {
        result.failed += group.lines.length - attempted;
      } finally {
        if (fd !== undefined) {
          try {
            fs.closeSync(fd);
          } catch {
            // The rows already landed; a close failure loses nothing.
          }
        }
      }
    }
  } catch {
    // Last line of defence: count whatever is unaccounted for.
    const accounted = result.written + result.failed;
    if (Array.isArray(rows) && rows.length > accounted) result.failed += rows.length - accounted;
  }
  failuresThisProcess += result.failed;
  return result;
}

export interface LedgerFileRead {
  rows: LedgerRow[];
  /** Complete lines that were not rows, plus a torn (unterminated) tail. */
  skipped: number;
}

export interface LedgerReadResult extends LedgerFileRead {
  /** The ledger path failed containment; nothing was read. */
  refused: boolean;
}

/** Parse ledger text. A row counts only once its newline landed. */
export function parseLedgerText(text: string): LedgerFileRead {
  const rows: LedgerRow[] = [];
  let skipped = 0;
  const lines = text.split("\n");
  const tail = lines.pop();
  if (tail !== undefined && tail.length > 0) skipped += 1;
  for (const line of lines) {
    if (line.trim().length === 0) continue;
    try {
      const parsed = JSON.parse(line) as unknown;
      if (
        parsed !== null &&
        typeof parsed === "object" &&
        !Array.isArray(parsed) &&
        (parsed as { v?: unknown }).v === LEDGER_VERSION &&
        KINDS.has(String((parsed as { kind?: unknown }).kind))
      ) {
        rows.push(parsed as LedgerRow);
      } else {
        skipped += 1;
      }
    } catch {
      skipped += 1;
    }
  }
  return { rows, skipped };
}

/** Read one day file, skipping torn and malformed lines. Missing file: no rows. */
export function readLedgerFile(filePath: string): LedgerFileRead {
  try {
    const stat = fs.statSync(filePath);
    if (!stat.isFile() || stat.size > MAX_LEDGER_FILE_BYTES) return { rows: [], skipped: 0 };
    return parseLedgerText(fs.readFileSync(filePath, "utf8"));
  } catch {
    return { rows: [], skipped: 0 };
  }
}

/**
 * Every row of one session, day files in date order. Refuses (reads nothing)
 * when any ledger path component escapes the vault. Never throws.
 */
export function readSessionLedger(vaultPath: string, sessionKey: string): LedgerReadResult {
  const empty = (refused: boolean): LedgerReadResult => ({ rows: [], skipped: 0, refused });
  if (typeof sessionKey !== "string" || !SESSION_KEY_RE.test(sessionKey)) return empty(true);
  let dir: string | null;
  try {
    dir = containedDir(vaultPath, ledgerSegments(sessionKey), false);
  } catch {
    return empty(true);
  }
  if (!dir) return empty(false);
  const result = empty(false);
  let names: string[];
  try {
    names = fs.readdirSync(dir).filter((name) => DAY_FILE_RE.test(name)).sort();
  } catch {
    return result;
  }
  for (const name of names) {
    const file = path.join(dir, name);
    try {
      assertWriteTargetUnder(file, vaultPath);
    } catch {
      result.refused = true;
      continue;
    }
    const read = readLedgerFile(file);
    result.rows.push(...read.rows);
    result.skipped += read.skipped;
  }
  return result;
}

// ---------------------------------------------------------------------------
// Visibility (§4.3)
// ---------------------------------------------------------------------------

export interface ItemLocation {
  visible: boolean;
  /** The item text occurs wholly inside some emitted string. */
  found: boolean;
  /** The wire declares no inline cap, so visibility could not be judged. */
  cap_unknown?: true;
  emitted_index?: number;
  start?: number;
  end?: number;
}

/**
 * Where each item sits in the exact strings a hook emitted, and whether the
 * model could see it. An item is visible when it lies wholly inside one emitted
 * string of at most `capChars`; in an over-cap string (which the host spills to
 * a preview) only when it ends by char HOST_PREVIEW_CHARS. With no known cap,
 * every item is `visible: true, cap_unknown: true`.
 */
export function locateItems(
  emitted: string | readonly string[],
  items: ReadonlyArray<string | { text: string }>,
  capChars: number | null | undefined,
): ItemLocation[] {
  const strings = (typeof emitted === "string" ? [emitted] : Array.isArray(emitted) ? emitted : []).map(
    (value) => (typeof value === "string" ? value : ""),
  );
  const capKnown = typeof capChars === "number" && Number.isFinite(capChars) && capChars > 0;
  return items.map((item) => {
    const text = typeof item === "string" ? item : typeof item?.text === "string" ? item.text : "";
    let first: ItemLocation | undefined;
    if (text.length > 0) {
      for (let index = 0; index < strings.length; index += 1) {
        const value = strings[index] as string;
        const start = value.indexOf(text);
        if (start < 0) continue;
        const end = start + text.length;
        const visible = capKnown && (value.length <= (capChars as number) || end <= HOST_PREVIEW_CHARS);
        const location: ItemLocation = { visible, found: true, emitted_index: index, start, end };
        if (visible) {
          first = location;
          break;
        }
        first ??= location;
      }
    }
    const location = first ?? { visible: false, found: false };
    if (!capKnown) return { ...location, visible: true, cap_unknown: true };
    return location;
  });
}

// ---------------------------------------------------------------------------
// Shingles and echo (§4.3)
// ---------------------------------------------------------------------------

const WORD_RE = /[\p{L}\p{N}]+/gu;
const RESOLUTION_HEADING_RE = /^(?:resolution|fix|decision|root cause)\b/i;
const RESOLUTION_LEAD_RE = /^(?:[-*]\s+)?(?:resolution|fix|decision|root cause)\s*:/i;
const HEADING_RE = /^(#{1,6})\s+(.*)$/;

function wordsOf(text: string): string[] {
  return text.normalize("NFKC").toLowerCase().match(WORD_RE) ?? [];
}

function shingleHashOf(words: readonly string[]): string {
  // 64 bits as 16 hex chars: JSON has no exact 64-bit integers.
  return hexDigest("sha256", words.join(" ")).slice(0, 16);
}

/**
 * Every distinct 8-word shingle hash of `text`, in position order. Words are
 * NFKC-normalised, lower-cased runs of letters and digits, so punctuation and
 * markdown do not change a match. Text under 8 words has no shingles.
 */
export function allShingleHashes(text: string): string[] {
  if (typeof text !== "string") return [];
  const words = wordsOf(text.slice(0, MAX_SHINGLE_INPUT_CHARS));
  const seen = new Set<string>();
  const out: string[] = [];
  for (let i = 0; i + SHINGLE_WORDS <= words.length; i += 1) {
    const hash = shingleHashOf(words.slice(i, i + SHINGLE_WORDS));
    if (!seen.has(hash)) {
      seen.add(hash);
      out.push(hash);
    }
  }
  return out;
}

function clampMax(max: number | undefined): number {
  if (typeof max !== "number" || !Number.isFinite(max)) return MAX_SHINGLES;
  return Math.max(0, Math.min(MAX_SHINGLES, Math.floor(max)));
}

/**
 * At most 64 shingle hashes of a memory body, for the detector's `applied`
 * rule. Shingles starting in a resolution section (a `## Resolution`/`Fix`/
 * `Decision`/`Root cause` heading, or a line led by one of those words and a
 * colon) come first, then the rest. With a DF map (hash → document frequency),
 * each group is ordered rarer first (an unseen hash counts as 0); without one,
 * position order is kept.
 */
export function shingleHashes(
  body: string,
  options: { df?: ReadonlyMap<string, number>; max?: number } = {},
): string[] {
  const max = clampMax(options.max);
  if (typeof body !== "string" || max === 0) return [];
  const words: string[] = [];
  const inResolution: boolean[] = [];
  let section = false;
  let sectionLevel = 0;
  for (const rawLine of body.slice(0, MAX_SHINGLE_INPUT_CHARS).split(/\r?\n/)) {
    const line = rawLine.trim();
    const heading = HEADING_RE.exec(line);
    if (heading) {
      const level = (heading[1] as string).length;
      if (section && level <= sectionLevel) section = false;
      if (RESOLUTION_HEADING_RE.test((heading[2] as string).trim())) {
        section = true;
        sectionLevel = level;
      }
    }
    const marked = section || RESOLUTION_LEAD_RE.test(line);
    for (const word of wordsOf(line)) {
      words.push(word);
      inResolution.push(marked);
    }
  }
  const seen = new Set<string>();
  const resolution: Array<{ hash: string; pos: number }> = [];
  const rest: Array<{ hash: string; pos: number }> = [];
  for (let i = 0; i + SHINGLE_WORDS <= words.length; i += 1) {
    const hash = shingleHashOf(words.slice(i, i + SHINGLE_WORDS));
    if (seen.has(hash)) continue;
    seen.add(hash);
    (inResolution[i] ? resolution : rest).push({ hash, pos: i });
  }
  const df = options.df;
  if (df) {
    const rarity = (entry: { hash: string; pos: number }): number => {
      const count = df.get(entry.hash);
      return typeof count === "number" && Number.isFinite(count) ? count : 0;
    };
    const byRarity = (a: { hash: string; pos: number }, b: { hash: string; pos: number }): number =>
      rarity(a) - rarity(b) || a.pos - b.pos;
    resolution.sort(byRarity);
    rest.sort(byRarity);
  }
  return [...resolution, ...rest].slice(0, max).map((entry) => entry.hash);
}

/** At most 64 shingle hashes of the text injected for an item, in position order. */
export function echoHashes(injected: string, max = MAX_SHINGLES): string[] {
  return allShingleHashes(injected).slice(0, clampMax(max));
}

// ---------------------------------------------------------------------------
// Deliver first, record second (§4.3)
// ---------------------------------------------------------------------------

export interface SurfacedItem {
  mkey: string;
  handles?: readonly string[];
  title?: string;
  rank: number;
  score?: number;
  route?: string;
  /** The text to look for in the emitted strings; defaults to `title`. */
  locate?: string;
  shingles?: readonly string[];
  echo?: readonly string[];
  tool_use_id?: string;
  tx_off?: number;
}

export interface RecordSurfacedArgs {
  vaultPath: string;
  context: LedgerContext;
  channel: SurfaceChannel;
  items: readonly SurfacedItem[];
  /** The exact strings the hook emits. */
  emitted: string | readonly string[];
  /** The wire's inline cap (`wireCapabilities(wire).hostInlineCapChars`). */
  capChars: number | null | undefined;
  turn_ix?: number;
  ts?: string;
}

export interface RecordSurfacedResult {
  rows: SurfacedRow[];
  /** Rows were registered with deferUntilDelivered. */
  registered: boolean;
}

/**
 * Build `surfaced` rows in memory and register them to be written once the
 * output is delivered. If the output is abandoned instead, they are written as
 * `delivered: false, visible: false`. Never throws; with MINNI_USE_LEDGER off,
 * registers nothing.
 */
export function recordSurfaced(args: RecordSurfacedArgs): RecordSurfacedResult {
  try {
    if (!Array.isArray(args.items) || args.items.length === 0) return { rows: [], registered: false };
    if (!flagOn(args.vaultPath, "MINNI_USE_LEDGER")) return { rows: [], registered: false };
    const ts = args.ts ?? nowIso();
    const locations = locateItems(
      args.emitted,
      args.items.map((item) => ({ text: item.locate ?? item.title ?? "" })),
      args.capChars,
    );
    const rows = args.items.map((item, index) => {
      const location = locations[index] as ItemLocation;
      return buildSurfacedRow(
        args.context,
        {
          channel: args.channel,
          mkey: item.mkey,
          handles: item.handles,
          title: item.title,
          rank: item.rank,
          score: item.score,
          route: item.route,
          turn_ix: args.turn_ix,
          visible: location.visible,
          delivered: true,
          cap_unknown: location.cap_unknown === true,
          tx_off: item.tx_off,
          shingles: item.shingles,
          echo: item.echo,
          tool_use_id: item.tool_use_id,
        },
        ts,
      );
    });
    const vaultPath = args.vaultPath;
    deferUntilDelivered(
      async () => {
        appendLedgerRows(vaultPath, rows);
      },
      {
        onAbandon: () => {
          appendLedgerRows(
            vaultPath,
            rows.map((row) => {
              const abandoned: SurfacedRow = { ...row, delivered: false, visible: false };
              delete abandoned.cap_unknown;
              return abandoned;
            }),
          );
        },
      },
    );
    return { rows, registered: true };
  } catch {
    return { rows: [], registered: false };
  }
}
