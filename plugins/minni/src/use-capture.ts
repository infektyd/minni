// Recall-to-use capture wiring (design rev5 §4.3, slice S1a-2).
//
// The hooks' half of the use ledger: how each prompt left UserPromptSubmit
// (`turn`), what each hook surfaced (`surfaced`), and when an agent's context
// was reset (`reset`). hook-handlers.ts calls in at a few points; the rest
// lives here so the handlers stay readable.
//
// The hard rule: capture only RECORDS. Nothing here changes a byte of hook
// output, and nothing here throws into a hook. Every write is best effort; a
// write that fails is not made (the ledger counts its own failures). With
// MINNI_USE_LEDGER off this module is not even loaded (ledger-gate.ts), and if
// flags.ts says off after all, `openLedgerSession` opens no capture: no file
// of any kind is written.
//
// Files, all under <vault>/.runtime, every directory walked with the ledger's
// containment check (a component that is a symlink out of the vault is
// refused) and every file written through a temp name and a rename:
//   recall-session.d/<session_key>/counters.json      {v, turn_ix}
//   recall-session.d/<session_key>/<agent_key>.reset  {reset_id, at, reason}
//   recall-session.d/<session_key>/hit-keys.json      mkeys of the hits the last
//       strong UPS armed the guard with, so a deny can key the hits it lists
//       (the recall-state file holds only titles and wikilinks, and a wikilink
//       does not name one memory: §4.2)
//   recall-session.d/.sweep.json                      the retention sweep's cursor
//   prompt-templates.json                             the classifier's counts
//       (harness-envelope.ts; UPS is its only writer)
// Session directories idle for 90 days are removed by a bounded sweep after a
// SessionStart is delivered (`scheduleSessionSweep`), and so are ledger session
// directories the use detector holds nothing open for (use-retention.ts).
//
// Privacy: rows hold keys, hashes and sanitized titles only. The prompt text and
// its template hash never reach the ledger.
import { randomBytes, randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { stableStringify } from "./agent_envelope.js";
import { extractLearningsSection } from "./daemon_transport.js";
import type { JsonResult, ReadContextResponse, RecallResponse } from "./daemon_transport.js";
import { FLAG_NAMES, loadFlags } from "./flags.js";
import type { FlagSnapshot } from "./flags.js";
import { classifyPromptEnvelope, readPromptTemplates, recordOccurrence } from "./harness-envelope.js";
import type { PromptTemplateEntry, PromptTemplatesFile } from "./harness-envelope.js";
import { wireCapabilities } from "./hook-platform.js";
import type { PlatformWire } from "./hook-platform.js";
import { deferUntilDelivered } from "./hook-delivery.js";
import { asString } from "./hook-utils.js";
import { LEDGER_EVENTS } from "./ledger-gate.js";
import { extractStrongRecall, sanitizeRecallField } from "./recall-state.js";
import type { RecallState, StrongRecall } from "./recall-state.js";
import {
  appendLedgerRows,
  buildResetRow,
  buildTurnRow,
  containedDir,
  echoHashes,
  handlesForKey,
  ledgerContextFrom,
  memoryKeyFor,
  recordSurfaced,
  shingleHashes,
} from "./use-ledger.js";
import type { LedgerContext, MemoryRef, SurfaceChannel, SurfacedItem } from "./use-ledger.js";
// Re-exported so hook-handlers.ts can read it without loading use-ledger.js itself.
export { ledgerFailureCount } from "./use-ledger.js";
import { sweepLedgerDirs } from "./use-retention.js";
import type { Finalizer, LedgerSweepResult } from "./use-retention.js";
import { assertWriteTargetUnder, recordAudit } from "./vault.js";
import type { VaultSearchResult } from "./vault.js";

/** Per-session capture state, under the vault. */
export const SESSION_RELDIR = path.join(".runtime", "recall-session.d");

const SESSION_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const AGENT_KEY_RE = /^(?:main|a-[0-9a-f]{12})$/;
/** A side file larger than this is not ours; it is ignored. */
const MAX_SIDE_FILE_BYTES = 64 * 1024;
/** Bound on the items one boot channel records (the daemon sends at most 8 results, 10 learnings). */
const MAX_BOOT_ITEMS = 32;
/** What buildGuardDenyReason lists (recall-guard.ts: `top_hits.slice(0, 5)`). */
const GUARD_LISTED_HITS = 5;
const MAX_PID_CHARS = 128;
const RECENT_LEARNING_CONTENT_MAX = 4000;

/** One hook event's capture: who is recording, and what the wire can show. */
export interface UseCapture {
  readonly vaultPath: string;
  readonly context: LedgerContext;
  /** The wire's inline cap; undefined when it declares none (visibility then cap_unknown). */
  readonly capChars: number | undefined;
}

/** How one UserPromptSubmit is leaving; every exit sets `exit`. */
export interface TurnRecord {
  /** The exit's reason. An exit that sets none is recorded as `error`. */
  exit?: string;
  /** This prompt's index in its session, from 1; 0 when the counter could not be kept. */
  readonly turn_ix: number;
}

/** What handleUserPromptSubmit receives from the dispatch arm when capture is on. */
export interface UpsCapture {
  capture: UseCapture;
  turn: TurnRecord;
}

function errorCode(error: unknown): unknown {
  return (error as { code?: unknown } | null)?.code;
}

// ---------------------------------------------------------------------------
// Opening a capture; the flags (§8.2)
// ---------------------------------------------------------------------------

/** A dispatched event's flags (read once) and its capture, when it has one. */
export interface LedgerSession {
  flags: FlagSnapshot;
  capture: UseCapture | null;
}

/**
 * The flags for this hook process (read once, through flags.ts) and the
 * capture for one dispatched event: null when the event captures nothing or
 * flags.ts says MINNI_USE_LEDGER is off. hook-handlers.ts only loads this
 * module once ledger-gate.ts has said the ledger may be on. Never throws.
 */
export function openLedgerSession(args: {
  vaultPath: string;
  event: string;
  payload: Record<string, unknown>;
  wire: PlatformWire;
  principal: string;
}): LedgerSession {
  const flags = loadFlags(args.vaultPath);
  return { flags, capture: openCapture({ ...args, flags }) };
}

function openCapture(args: {
  vaultPath: string;
  event: string;
  payload: Record<string, unknown>;
  wire: PlatformWire;
  principal: string;
  flags: FlagSnapshot;
}): UseCapture | null {
  try {
    if (!LEDGER_EVENTS.has(args.event)) return null;
    if (args.flags.values.MINNI_USE_LEDGER !== "on") return null;
    const features = FLAG_NAMES.filter((name) => args.flags.values[name] === "on").sort();
    const context = ledgerContextFrom(args.payload, {
      wire: args.wire.id,
      principal: args.principal,
      vaultPath: args.vaultPath,
      features,
    });
    // Both become path components below; refuse anything the key helpers
    // would never produce rather than trust it.
    if (!SESSION_KEY_RE.test(context.session_key) || !AGENT_KEY_RE.test(context.agent_key)) return null;
    return { vaultPath: args.vaultPath, context, capChars: wireCapabilities(args.wire).hostInlineCapChars };
  } catch {
    return null;
  }
}

// Each note flags.ts can produce, as a problem code. The note text itself is
// never audited: it quotes the offending value and the file's path.
const NOTE_PROBLEMS: ReadonlyArray<readonly [RegExp, string]> = [
  [/^flags: env (MINNI_[A-Z_]+)=/, "env_value_ignored"],
  [/^flags: (MINNI_[A-Z_]+)=/, "file_value_ignored"],
  [/unknown flag/, "file_unknown_flag"],
  [/refused \(containment\)/, "file_refused"],
  [/not valid JSON/, "file_invalid_json"],
  [/not a JSON object/, "file_not_object"],
  [/not a regular file/, "file_not_regular"],
  [/exceeds \d+ bytes/, "file_too_large"],
  [/unreadable|could not be read/, "file_unreadable"],
];
const KNOWN_FLAGS: ReadonlySet<string> = new Set(FLAG_NAMES);

/** A flag note as a problem code, naming the flag only when it is a known one. */
export function flagProblem(note: string): string {
  for (const [pattern, code] of NOTE_PROBLEMS) {
    const match = pattern.exec(note);
    if (!match) continue;
    const flag = match[1];
    return flag !== undefined && KNOWN_FLAGS.has(flag) ? `${code}:${flag}` : code;
  }
  return "other";
}

/**
 * One throttled audit line when the flags had problems, naming the problems
 * but no value and no path. Never throws.
 */
export async function auditFlagNotes(vaultPath: string, auditPrefix: string, flags: FlagSnapshot): Promise<void> {
  try {
    if (!Array.isArray(flags.notes) || flags.notes.length === 0) return;
    const problems = [...new Set(flags.notes.map(flagProblem))].sort();
    await recordAudit(vaultPath, {
      tool: `${auditPrefix}_flags_note`,
      summary: `flags: ${problems.join(", ")}; each ignored value falls back to the next source`,
      throttleKey: `${auditPrefix}_flags_note`,
    });
  } catch {
    // Losing the note must not also cost the event.
  }
}

// ---------------------------------------------------------------------------
// Side files
// ---------------------------------------------------------------------------

function sessionDir(capture: UseCapture, create: boolean): string | null {
  return containedDir(capture.vaultPath, [...SESSION_RELDIR.split(path.sep), capture.context.session_key], create);
}

const READ_FLAGS =
  fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0);

/**
 * A small JSON side file, or undefined when it is absent, not a regular file
 * or oversize. Opened without following a symlink and without blocking (a FIFO
 * planted in its place cannot hang the hook). Throws on refusal or bad JSON.
 */
function readSideFile(file: string, vaultPath: string, missing: unknown = undefined): unknown {
  assertWriteTargetUnder(file, vaultPath);
  let fd: number;
  try {
    fd = fs.openSync(file, READ_FLAGS);
  } catch (error) {
    if (errorCode(error) === "ENOENT") return missing;
    throw error;
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > MAX_SIDE_FILE_BYTES) return undefined;
    const buffer = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < buffer.length) {
      const n = fs.readSync(fd, buffer, offset, buffer.length - offset, offset);
      if (n <= 0) break;
      offset += n;
    }
    return JSON.parse(buffer.subarray(0, offset).toString("utf8"));
  } finally {
    fs.closeSync(fd);
  }
}

/** Replace a side file atomically: a fresh temp name (never followed), then a rename. Throws on failure. */
function writeSideFile(file: string, value: unknown, vaultPath: string): void {
  const tmp = `${file}.${process.pid}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(tmp, JSON.stringify(value), { flag: "wx", mode: 0o600 });
    assertWriteTargetUnder(file, vaultPath);
    fs.renameSync(tmp, file);
  } catch (error) {
    try {
      fs.rmSync(tmp, { force: true });
    } catch {
      // A stray temp file is harmless; the error below is what matters.
    }
    throw error;
  }
}

function turnIndexIn(raw: unknown): number | undefined {
  if (raw === null || typeof raw !== "object") return undefined;
  const value = raw as { v?: unknown; turn_ix?: unknown };
  return value.v === 1 && Number.isSafeInteger(value.turn_ix) && (value.turn_ix as number) >= 0
    ? (value.turn_ix as number)
    : undefined;
}

const MISSING = Symbol("missing");

/**
 * The counter's current value: 0 when the file is absent, null when it is
 * present but not a counter (corrupt, foreign, not a regular file, refused).
 * A present-but-invalid file is evidence of something; it is never overwritten
 * with a restart at 1, which would hand out indices the session already used.
 */
function counterIn(file: string, vaultPath: string): number | null {
  let raw: unknown;
  try {
    raw = readSideFile(file, vaultPath, MISSING);
  } catch {
    return null;
  }
  if (raw === MISSING) return 0;
  return turnIndexIn(raw) ?? null;
}

/** Wait this long, at most, for another UPS holding the counter lock. */
const COUNTER_LOCK_WAIT_MS = 200;
/** A lock this old belongs to a process that died holding it; it is broken. */
export const COUNTER_LOCK_STALE_MS = 2000;
const COUNTER_LOCK_POLL_MS = 2;
const LOCK_FLAGS =
  fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | (fs.constants.O_NOFOLLOW ?? 0);
const sleepCell = new Int32Array(new SharedArrayBuffer(4));

function sleepSync(ms: number): void {
  Atomics.wait(sleepCell, 0, 0, ms);
}

/** Test seam for acquireCounterLock; production callers pass nothing. */
export interface CounterLockHooks {
  /** Runs after a lock was judged stale and before it is broken. */
  beforeBreak?: () => void;
}

/**
 * Break a lock judged stale. It is renamed away first: of several breakers,
 * exactly one rename takes it, the others get ENOENT and retry the create.
 * What a breaker renamed may still not be the lock it judged: another breaker
 * can have broken that one and a fresh lock taken its place in between. So
 * only a renamed lock that is still stale is removed; a fresh one is linked
 * back (never over a lock that appeared meanwhile) and the breaker retries.
 */
function breakStaleLock(lock: string): "broken" | "fresh" | "gone" | "error" {
  const grave = `${lock}.stale-${process.pid}-${randomUUID()}`;
  try {
    fs.renameSync(lock, grave);
  } catch (error) {
    return errorCode(error) === "ENOENT" ? "gone" : "error";
  }
  let stale = false;
  try {
    const stat = fs.lstatSync(grave);
    stale = stat.isFile() && Date.now() - stat.mtimeMs > COUNTER_LOCK_STALE_MS;
  } catch {
    // Unjudgeable: treat it as live and put it back.
  }
  if (!stale) {
    try {
      fs.linkSync(grave, lock);
    } catch {
      // EEXIST: a newer lock holds the path; its holder is the one inside.
    }
  }
  try {
    fs.unlinkSync(grave);
  } catch {
    // Best effort: a leftover grave holds no lock.
  }
  return stale ? "broken" : "fresh";
}

/** Remove the lock only if it is still ours (its token), never another holder's. */
function releaseCounterLock(lock: string, token: string): void {
  let fd: number | undefined;
  try {
    fd = fs.openSync(lock, READ_FLAGS);
    const buffer = Buffer.alloc(token.length + 1);
    const n = fs.readSync(fd, buffer, 0, buffer.length, 0);
    if (buffer.subarray(0, n).toString("utf8") === token) fs.unlinkSync(lock);
  } catch {
    // Already gone, or not ours: nothing to release.
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        // Nothing was written.
      }
    }
  }
}

/**
 * Take `counters.json.lock` (O_EXCL) in a session directory. Returns its
 * release, or null when it could not be taken within the bounded wait, or
 * something that is not a lock file sits in its place. A lock older than
 * COUNTER_LOCK_STALE_MS is broken through breakStaleLock (the critical
 * section is a few small file operations, so a lock that old has no live
 * holder). Each lock carries a random token, so a release removes only its own.
 */
export function acquireCounterLock(
  dir: string,
  vaultPath: string,
  hooks: CounterLockHooks = {},
): (() => void) | null {
  const lock = path.join(dir, "counters.json.lock");
  const token = `${process.pid} ${randomUUID()}\n`;
  const deadline = Date.now() + COUNTER_LOCK_WAIT_MS;
  for (let attempt = 0; ; attempt += 1) {
    if (attempt > 0 && Date.now() >= deadline) return null;
    try {
      assertWriteTargetUnder(lock, vaultPath);
      const fd = fs.openSync(lock, LOCK_FLAGS, 0o600);
      try {
        fs.writeSync(fd, token);
      } finally {
        fs.closeSync(fd);
      }
      return () => releaseCounterLock(lock, token);
    } catch (error) {
      if (errorCode(error) !== "EEXIST") return null;
    }
    try {
      const stat = fs.lstatSync(lock);
      if (!stat.isFile()) return null;
      if (Date.now() - stat.mtimeMs > COUNTER_LOCK_STALE_MS) {
        hooks.beforeBreak?.();
        if (breakStaleLock(lock) === "error") sleepSync(COUNTER_LOCK_POLL_MS);
        continue;
      }
    } catch (error) {
      if (errorCode(error) === "ENOENT") continue;
      return null;
    }
    sleepSync(COUNTER_LOCK_POLL_MS);
  }
}

/**
 * Increment and return this session's turn counter, under its lock, so
 * concurrent prompts never share an index. 0 when it cannot be kept: the lock
 * was not taken in time, the file is present but invalid (left as it is), or
 * the new value could not be written.
 */
function nextTurnIndex(capture: UseCapture): number {
  try {
    const dir = sessionDir(capture, true);
    if (dir === null) return 0;
    const release = acquireCounterLock(dir, capture.vaultPath);
    if (release === null) return 0;
    try {
      const file = path.join(dir, "counters.json");
      const current = counterIn(file, capture.vaultPath);
      if (current === null) return 0;
      const next = current + 1;
      writeSideFile(file, { v: 1, turn_ix: next }, capture.vaultPath);
      return next;
    } finally {
      release();
    }
  } catch {
    // An index that could not be persisted would repeat next turn; say unknown.
    return 0;
  }
}

/** This session's current turn index without changing it, if known. */
function currentTurnIndex(capture: UseCapture): number | undefined {
  try {
    const dir = sessionDir(capture, false);
    if (dir === null) return undefined;
    const ix = turnIndexIn(readSideFile(path.join(dir, "counters.json"), capture.vaultPath));
    return ix !== undefined && ix > 0 ? ix : undefined;
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------------------
// The UPS turn record (§4.3 "Every UPS exit is counted")
// ---------------------------------------------------------------------------

interface TurnClass {
  envelope_kind: string | null;
  subkind: string | null;
  template_rule?: "on" | "off";
  rule_id?: string;
}

function promptIdOf(payload: Record<string, unknown>): string | undefined {
  const raw = payload.prompt_id;
  return typeof raw === "string" && raw.length > 0 && raw.length <= MAX_PID_CHARS ? raw : undefined;
}

function templateEntryIn(file: PromptTemplatesFile | null, hash: string): PromptTemplateEntry | undefined {
  return file !== null && Object.hasOwn(file.templates, hash) ? file.templates[hash] : undefined;
}

/**
 * Classify the prompt, record-only (§5.2.1), with its template context; count
 * an eligible prompt in the counts file. Changes no behaviour. Never rejects.
 * It runs while the handler does; the handler never touches the counts file,
 * so the overlap is safe, and UPS stays the file's only writer.
 */
async function classifyTurn(capture: UseCapture, payload: Record<string, unknown>, turnIx: number): Promise<TurnClass> {
  try {
    const prompt = asString(payload.prompt) || asString(payload.user_prompt);
    const at = Date.now();
    const pid =
      promptIdOf(payload) ??
      `turn:${capture.context.session_key}:${turnIx > 0 ? String(turnIx) : randomUUID()}`;
    const templates = await readPromptTemplates(capture.vaultPath).catch(() => null);
    const envelope = classifyPromptEnvelope(prompt, {
      at,
      pid,
      templateEntry: (hash) => templateEntryIn(templates, hash),
    });
    if (envelope.template !== undefined) {
      await recordOccurrence(capture.vaultPath, {
        hash: envelope.template.hash,
        ts: at,
        pid,
        sessionKeyPrefix: capture.context.session_key.slice(0, 8),
        now: at,
      }).catch(() => false);
    }
    return {
      envelope_kind: envelope.kind,
      subkind: envelope.subkind,
      template_rule: envelope.template_rule,
      ...(envelope.rule_id !== undefined ? { rule_id: envelope.rule_id } : {}),
    };
  } catch {
    return { envelope_kind: null, subkind: null };
  }
}

function appendTurn(capture: UseCapture, turnIx: number, klass: TurnClass, exit: string): void {
  try {
    appendLedgerRows(capture.vaultPath, [
      buildTurnRow(capture.context, {
        turn_ix: turnIx,
        envelope_kind: klass.envelope_kind,
        subkind: klass.subkind,
        exit,
        template_rule: klass.template_rule,
        rule_id: klass.rule_id,
      }),
    ]);
  } catch {
    // appendLedgerRows does not throw; this is only the last line of defence.
  }
}

/**
 * How long a finished handler waits for its prompt's classification. It runs
 * alongside the handler and is normally done long before; the bound exists so
 * a counts file that cannot be read (a FIFO in its place blocks a read) can
 * never hold the turn, and with it the hook's output.
 */
const CLASSIFY_WAIT_MS = 250;
const UNCLASSIFIED: TurnClass = { envelope_kind: null, subkind: null };

/** `work`, or `fallback` after `ms`. The timer is not unref'd, so the wait always ends. */
function settleWithin<T>(work: Promise<T>, ms: number, fallback: T): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const budget = new Promise<T>((resolve) => {
    timer = setTimeout(() => resolve(fallback), ms);
  });
  return Promise.race([work, budget]).finally(() => clearTimeout(timer));
}

/**
 * The UserPromptSubmit dispatch arm. Counts the turn at entry, classifies the
 * prompt while `inner` runs, and appends the `turn` row as soon as `inner`
 * returns (before emission: it records an input) or throws (then rethrows,
 * into today's failure handling). With capture off it is just `inner`.
 */
export async function withTurnRecord<T>(
  capture: UseCapture | null,
  payload: Record<string, unknown>,
  inner: (turn: TurnRecord) => Promise<T>,
): Promise<T> {
  if (capture === null) return inner({ turn_ix: 0 });
  const turnIx = nextTurnIndex(capture);
  // Not awaited here: its file I/O overlaps the handler's own. Never rejects.
  const classified = classifyTurn(capture, payload, turnIx);
  const turn: TurnRecord = { turn_ix: turnIx };
  let result: T;
  try {
    result = await inner(turn);
  } catch (error) {
    appendTurn(capture, turnIx, await settleWithin(classified, CLASSIFY_WAIT_MS, UNCLASSIFIED), "error");
    throw error;
  }
  appendTurn(capture, turnIx, await settleWithin(classified, CLASSIFY_WAIT_MS, UNCLASSIFIED), turn.exit ?? "error");
  return result;
}

// ---------------------------------------------------------------------------
// Resets (§4.3)
// ---------------------------------------------------------------------------

const BOOT_SOURCES: ReadonlySet<string> = new Set(["startup", "resume", "compact", "clear"]);

function resetReason(event: string, payload: Record<string, unknown>): string {
  if (event !== "SessionStart") return event;
  const source = asString(payload.source);
  return BOOT_SOURCES.has(source) ? `SessionStart:${source}` : "SessionStart";
}

/**
 * The payload's own agent lost its context (compaction, a boot): write its
 * `<agent_key>.reset` marker with a fresh `reset_id` and append the matching
 * `reset` row, immediately. Never throws.
 */
export function recordReset(capture: UseCapture | null, event: string, payload: Record<string, unknown>): void {
  if (capture === null) return;
  const resetId = randomBytes(8).toString("hex");
  const at = new Date().toISOString();
  try {
    const dir = sessionDir(capture, true);
    if (dir !== null) {
      writeSideFile(
        path.join(dir, `${capture.context.agent_key}.reset`),
        { reset_id: resetId, at, reason: resetReason(event, payload) },
        capture.vaultPath,
      );
    }
  } catch {
    // The row below still records the reset.
  }
  try {
    appendLedgerRows(capture.vaultPath, [buildResetRow(capture.context, { event, reset_id: resetId }, at)]);
  } catch {
    // appendLedgerRows does not throw; last line of defence.
  }
}

// ---------------------------------------------------------------------------
// Surfacings: deliver first, record second (§4.3)
// ---------------------------------------------------------------------------

function record(
  capture: UseCapture,
  channel: SurfaceChannel,
  items: readonly SurfacedItem[],
  emitted: string,
  turnIx?: number,
): void {
  if (items.length === 0) return;
  recordSurfaced({
    vaultPath: capture.vaultPath,
    context: capture.context,
    channel,
    items,
    emitted,
    capChars: capture.capChars,
    ...(turnIx !== undefined && turnIx > 0 ? { turn_ix: turnIx } : {}),
  });
}

/**
 * The text to look for: `text` as is when the emitted string carries it raw
 * (a native Layer 1 prefix), else its JSON-escaped form (inside the envelope,
 * whose body is JSON). JSON escaping is per character, so an escaped substring
 * is a substring of the escaped whole.
 */
function locateText(emitted: string, text: string): string {
  if (!text || emitted.includes(text)) return text;
  const escaped = JSON.stringify(text).slice(1, -1);
  return emitted.includes(escaped) ? escaped : text;
}

function finiteOrUndefined(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function resultTitle(result: Record<string, unknown>): string {
  if (typeof result.title === "string" && result.title.trim()) return result.title.trim();
  if (typeof result.wikilink === "string") {
    const inner = result.wikilink.replace(/^\[\[/, "").replace(/\]\]$/, "");
    const base = inner.split("/").pop();
    if (base) return base;
  }
  return typeof result.filename === "string" ? result.filename : "";
}

function daemonRef(result: Record<string, unknown>): MemoryRef {
  return { kind: "daemon", src: result.src, doc_id: result.doc_id, source: result.source };
}

/**
 * A boot learning as the daemon `read` sends it (S1a-D). Distinct from the
 * envelope's own `recent_learnings` field, which is rendered text: the daemon
 * response is read for these and never spread into the envelope.
 */
export interface DaemonRecentLearning {
  learning_id: unknown;
  /** The rendered line, exactly as it appears in the read context (may span physical lines). */
  line: string;
  /** Stored content, hashed into shingles here and never stored or injected. */
  content: string;
}

/** The structured boot learnings of a daemon `read`, or null when it sent none (version skew). */
export function daemonRecentLearnings(data: unknown): DaemonRecentLearning[] | null {
  if (data === null || typeof data !== "object") return null;
  const raw = (data as { recent_learnings?: unknown }).recent_learnings;
  if (!Array.isArray(raw)) return null;
  const out: DaemonRecentLearning[] = [];
  for (const entry of raw.slice(0, MAX_BOOT_ITEMS)) {
    if (entry === null || typeof entry !== "object") continue;
    const value = entry as Record<string, unknown>;
    out.push({
      learning_id: value.learning_id,
      line: typeof value.line === "string" ? value.line : "",
      content: typeof value.content === "string" ? value.content.slice(0, RECENT_LEARNING_CONTENT_MAX) : "",
    });
  }
  return out;
}

export interface BootCaptureInput {
  /** The exact string SessionStart emits. */
  emitted: string;
  recall: JsonResult<RecallResponse>;
  identity: JsonResult<ReadContextResponse>;
}

function bootRecallItems(capture: UseCapture, input: BootCaptureInput): SurfacedItem[] {
  const results = input.recall.ok ? input.recall.data?.results : undefined;
  if (!Array.isArray(results)) return [];
  const items: SurfacedItem[] = [];
  results.slice(0, MAX_BOOT_ITEMS).forEach((raw, index) => {
    if (raw === null || typeof raw !== "object") return;
    const result = raw as Record<string, unknown>;
    const mkey = memoryKeyFor(daemonRef(result), capture.vaultPath);
    if (!mkey) return;
    items.push({
      mkey,
      handles: handlesForKey(mkey),
      title: resultTitle(result),
      rank: index + 1,
      score: finiteOrUndefined(result.confidence) ?? finiteOrUndefined(result.score),
      // The envelope serializes each projected result with stableStringify,
      // so this is its exact span in the emitted string.
      locate: stableStringify(raw),
    });
  });
  return items;
}

function bootLearningItems(capture: UseCapture, input: BootCaptureInput): SurfacedItem[] {
  const data = input.identity.ok ? input.identity.data : undefined;
  if (!data) return [];
  const structured = daemonRecentLearnings(data);
  if (structured !== null) {
    const items: SurfacedItem[] = [];
    structured.forEach((entry, index) => {
      const mkey =
        memoryKeyFor({ kind: "learning", learning_id: entry.learning_id }, capture.vaultPath) ??
        memoryKeyFor({ kind: "boot_line", line: entry.line }, capture.vaultPath);
      if (!mkey) return;
      items.push({
        mkey,
        rank: index + 1,
        // Boot learnings have no title (§4.4); a line is located as a
        // substring, since one can span physical lines.
        title: "",
        locate: locateText(input.emitted, entry.line),
        ...(entry.content ? { shingles: shingleHashes(entry.content) } : {}),
        echo: echoHashes(entry.line),
      });
    });
    return items;
  }
  // No structured ids: key the extracted Learnings lines by hash.
  const section = extractLearningsSection(data.context);
  if (!section) return [];
  const lines = section
    .split("\n")
    .slice(1)
    .filter((line) => /^\s*-\s/.test(line))
    .slice(0, MAX_BOOT_ITEMS);
  const items: SurfacedItem[] = [];
  lines.forEach((line, index) => {
    const mkey = memoryKeyFor({ kind: "boot_line", line }, capture.vaultPath);
    if (!mkey) return;
    items.push({ mkey, rank: index + 1, title: "", locate: locateText(input.emitted, line), echo: echoHashes(line) });
  });
  return items;
}

/** SessionStart: one row per projected recall result and per boot learning. Never throws. */
export function captureBootSurfacings(capture: UseCapture | undefined, input: BootCaptureInput): void {
  if (capture === undefined) return;
  try {
    record(capture, "boot_recall", bootRecallItems(capture, input), input.emitted);
  } catch {
    // Best effort, per channel.
  }
  try {
    record(capture, "boot_recent_learnings", bootLearningItems(capture, input), input.emitted);
  } catch {
    // Best effort, per channel.
  }
}

interface HitKey {
  wikilink: string;
  score: number;
  mkey: string | null;
}

const NON_VAULT_MKEY_RE = /^(?:doc:[A-Za-z0-9_-]{1,32}:[A-Za-z0-9_.-]{1,128}|learning:[A-Za-z0-9_.-]{1,128}|lhash:[0-9a-f]{12})$/;
// Built from a string, as flags.ts does, to keep the literal out of no-control-regex.
const CONTROL_RE = new RegExp("[\\u0000-\\u001F\\u007F\\\\]");

/**
 * A key read back from `hit-keys.json` (a file any local process can write)
 * has the shape memoryKeyFor gives: a `vault:` key is a vault-relative POSIX
 * path, so an absolute path, a drive letter, a backslash, an empty, `.` or
 * `..` segment, or a control character is refused rather than recorded.
 */
function isValidMkey(value: string): boolean {
  if (!value.startsWith("vault:")) return NON_VAULT_MKEY_RE.test(value);
  const rel = value.slice("vault:".length);
  if (rel.length === 0 || rel.length > 1024 || CONTROL_RE.test(rel)) return false;
  if (rel.startsWith("/") || /^[A-Za-z]:/.test(rel)) return false;
  return rel.split("/").every((segment) => segment !== "" && segment !== "." && segment !== "..");
}

function sameHit(a: { title: unknown; wikilink: unknown; score: unknown }, b: typeof a): boolean {
  return a.title === b.title && a.wikilink === b.wikilink && a.score === b.score;
}

/**
 * The mkey of each top hit, by matching it back to the result it came from.
 * Each candidate is projected through extractStrongRecall itself, so the match
 * uses the very title/wikilink/score the top hit carries; candidates are
 * scanned in its insertion order (daemon, then vault) and used once, which is
 * how its stable sort broke any tie.
 */
function topHitKeys(
  vaultPath: string,
  strong: StrongRecall,
  response: RecallResponse | undefined,
  vaultResults: readonly VaultSearchResult[],
  threshold: number,
): Array<string | null> {
  const pool: Array<{ hit: StrongRecall["topHits"][number]; ref: MemoryRef }> = [];
  const daemonResults = response && Array.isArray(response.results) ? response.results : [];
  for (const raw of daemonResults) {
    const hit = extractStrongRecall({ results: [raw] }, [], threshold, 1)?.topHits[0];
    if (hit) pool.push({ hit, ref: daemonRef((raw ?? {}) as Record<string, unknown>) });
  }
  for (const result of vaultResults) {
    const hit = extractStrongRecall(undefined, [result], threshold, 1)?.topHits[0];
    if (hit) pool.push({ hit, ref: { kind: "vault", path: result.relativePath } });
  }
  const used = new Set<number>();
  return strong.topHits.map((hit) => {
    const index = pool.findIndex((candidate, i) => !used.has(i) && sameHit(candidate.hit, hit));
    if (index < 0) return null;
    used.add(index);
    return memoryKeyFor((pool[index] as { ref: MemoryRef }).ref, vaultPath);
  });
}

export interface PointerCaptureInput {
  /** The exact string UserPromptSubmit emits. */
  emitted: string;
  /** The pointer text inside it. */
  pointer: string;
  strong: StrongRecall;
  recall: RecallResponse | undefined;
  vaultResults: readonly VaultSearchResult[];
  threshold: number;
  taskSignature: string;
  /** The recall-state file was written: the guard is armed with these hits. */
  armed: boolean;
}

/**
 * A strong UPS turn: a `prompt_pointer` row for the hit the pointer names
 * (today only the top hit; the other top_hits live only in the recall-state
 * file, which the model never sees, so they get no row). When the guard was
 * armed, the hits' mkeys are kept for it. Never throws.
 */
export function capturePromptPointer(use: UpsCapture | undefined, input: PointerCaptureInput): void {
  if (use === undefined) return;
  const { capture } = use;
  try {
    const keys = topHitKeys(capture.vaultPath, input.strong, input.recall, input.vaultResults, input.threshold);
    if (input.armed) {
      try {
        const dir = sessionDir(capture, true);
        if (dir !== null) {
          const hits: HitKey[] = input.strong.topHits.map((hit, index) => ({
            wikilink: hit.wikilink,
            score: hit.score,
            mkey: keys[index] ?? null,
          }));
          writeSideFile(
            path.join(dir, "hit-keys.json"),
            { v: 1, task_signature: input.taskSignature, hits },
            capture.vaultPath,
          );
        }
      } catch {
        // The guard then records no keyed rows for this state; the pointer row stands.
      }
    }
    const top = input.strong.topHits[0];
    const mkey = keys[0];
    if (!top || !mkey) return;
    record(
      capture,
      "prompt_pointer",
      [
        {
          mkey,
          handles: handlesForKey(mkey),
          title: top.title,
          rank: 1,
          score: top.score,
          locate: locateText(input.emitted, input.pointer),
        },
      ],
      input.emitted,
      use.turn.turn_ix,
    );
  } catch {
    // Best effort.
  }
}

function readHitKeys(capture: UseCapture, taskSignature: string): HitKey[] {
  try {
    const dir = sessionDir(capture, false);
    if (dir === null) return [];
    const raw = readSideFile(path.join(dir, "hit-keys.json"), capture.vaultPath) as
      | { v?: unknown; task_signature?: unknown; hits?: unknown }
      | undefined;
    if (!raw || raw.v !== 1 || raw.task_signature !== taskSignature || !Array.isArray(raw.hits)) return [];
    const out: HitKey[] = [];
    for (const entry of raw.hits.slice(0, GUARD_LISTED_HITS * 2)) {
      if (entry === null || typeof entry !== "object") continue;
      const value = entry as Record<string, unknown>;
      if (typeof value.wikilink !== "string" || typeof value.score !== "number") continue;
      // The file lives in a vault any local process can write: only a
      // well-formed key may reach a row.
      const mkey = typeof value.mkey === "string" && isValidMkey(value.mkey) ? value.mkey : null;
      out.push({ wikilink: value.wikilink, score: value.score, mkey });
    }
    return out;
  } catch {
    return [];
  }
}

/**
 * A PreToolUse deny that is being returned: one `guard` row per hit its reason
 * lists, keyed from what the arming UPS recorded. A hit with no recorded key is
 * not guessed from its wikilink. Never throws.
 */
export function captureGuardDeny(capture: UseCapture | undefined, state: RecallState, reason: string): void {
  if (capture === undefined) return;
  try {
    const listed = Array.isArray(state.top_hits) ? state.top_hits.slice(0, GUARD_LISTED_HITS) : [];
    if (listed.length === 0) return;
    const keys = readHitKeys(capture, state.task_signature);
    const used = new Set<number>();
    const items: SurfacedItem[] = [];
    listed.forEach((hit, index) => {
      const at = keys.findIndex(
        (key, i) => !used.has(i) && key.wikilink === hit?.wikilink && key.score === hit?.score,
      );
      if (at < 0) return;
      used.add(at);
      const mkey = (keys[at] as HitKey).mkey;
      if (!mkey) return;
      items.push({
        mkey,
        handles: handlesForKey(mkey),
        title: typeof hit.title === "string" ? hit.title : "",
        rank: index + 1,
        score: finiteOrUndefined(hit.score),
        // The deny lists each hit by its sanitized wikilink.
        locate: sanitizeRecallField(hit.wikilink),
      });
    });
    record(capture, "guard", items, reason, currentTurnIndex(capture));
  } catch {
    // Best effort.
  }
}

export interface SubagentBriefCaptureInput {
  /** The exact brief string SubagentStart emits in additionalContext. */
  emitted: string;
  parentState?: RecallState | null;
  taskStrong?: StrongRecall | null;
  taskRecall?: RecallResponse | undefined;
  taskVaultResults?: readonly VaultSearchResult[];
  threshold: number;
}

function keyFromVaultWikilink(vaultPath: string, wikilink: string): string | null {
  const clean = wikilink.replace(/^\[\[/, "").replace(/\]\]$/, "");
  const inner = (clean.split("|")[0] ?? "").split("#")[0]?.trim() ?? "";
  if (!inner || inner.includes("\0") || inner.startsWith("/") || inner.split("/").includes("..")) return null;
  for (const candidate of [inner, `${inner}.md`, `wiki/${inner}`, `wiki/${inner}.md`]) {
    try {
      const full = path.join(vaultPath, candidate);
      if (fs.statSync(full).isFile()) {
        const key = memoryKeyFor({ kind: "vault", path: candidate }, vaultPath);
        if (key && isValidMkey(key)) return key;
      }
    } catch {
      // Try next candidate.
    }
  }
  return null;
}

/**
 * S2b (§6.4): SubagentStart brief capture. Records one `surfaced` row with
 * `channel: "subagent_brief"` per hit actually present in `emitted`, keyed from
 * the parent session's `hit-keys.json` (or vault wikilink fallback) for parent
 * hits and `topHitKeys` for task-specific recall hits. Never throws.
 */
export function captureSubagentBrief(capture: UseCapture | undefined, input: SubagentBriefCaptureInput): void {
  if (capture === undefined || !input.emitted) return;
  try {
    const items: SurfacedItem[] = [];
    const seenMkeys = new Set<string>();
    const parentHits = Array.isArray(input.parentState?.top_hits)
      ? input.parentState!.top_hits.slice(0, 3)
      : [];
    const parentKeys =
      input.parentState && typeof input.parentState.task_signature === "string"
        ? readHitKeys(capture, input.parentState.task_signature)
        : [];
    const usedParentKeyIx = new Set<number>();

    for (const hit of parentHits) {
      if (!hit || typeof hit.wikilink !== "string") continue;
      const locate = sanitizeRecallField(hit.wikilink);
      if (!locate || !input.emitted.includes(locate)) continue;
      let mkey: string | null = null;
      const at = parentKeys.findIndex(
        (k, i) => !usedParentKeyIx.has(i) && k.wikilink === hit.wikilink && k.score === hit.score,
      );
      if (at >= 0) {
        usedParentKeyIx.add(at);
        mkey = parentKeys[at]?.mkey ?? null;
      }
      if (!mkey && parentKeys.length === 0) {
        mkey = keyFromVaultWikilink(capture.vaultPath, hit.wikilink);
      }
      if (!mkey || seenMkeys.has(mkey)) continue;
      seenMkeys.add(mkey);
      items.push({
        mkey,
        handles: handlesForKey(mkey),
        title: typeof hit.title === "string" ? hit.title : "",
        rank: items.length + 1,
        score: finiteOrUndefined(hit.score),
        locate,
      });
    }

    if (input.taskStrong && Array.isArray(input.taskStrong.topHits)) {
      const taskKeys = topHitKeys(
        capture.vaultPath,
        input.taskStrong,
        input.taskRecall,
        input.taskVaultResults ?? [],
        input.threshold,
      );
      input.taskStrong.topHits.slice(0, 2).forEach((hit, index) => {
        if (!hit || typeof hit.wikilink !== "string") return;
        const locate = sanitizeRecallField(hit.wikilink);
        if (!locate || !input.emitted.includes(locate)) return;
        const mkey = taskKeys[index] ?? keyFromVaultWikilink(capture.vaultPath, hit.wikilink);
        if (!mkey || seenMkeys.has(mkey)) return;
        seenMkeys.add(mkey);
        items.push({
          mkey,
          handles: handlesForKey(mkey),
          title: typeof hit.title === "string" ? hit.title : "",
          rank: items.length + 1,
          score: finiteOrUndefined(hit.score),
          locate,
        });
      });
    }

    record(capture, "subagent_brief", items, input.emitted);
  } catch {
    // Best effort.
  }
}


// ---------------------------------------------------------------------------
// Retention of recall-session.d (§4.3 "Retention", §6.2.5)
// ---------------------------------------------------------------------------

/** A session directory idle this long is removed: the ledger's own retention horizon. */
export const SESSION_IDLE_MAX_MS = 90 * 24 * 60 * 60 * 1000;
/** After a complete pass, the next sweep waits this long. */
export const SWEEP_INTERVAL_MS = 24 * 60 * 60 * 1000;
/** Cooperative budget for one run (§6.2.5): checked at every item boundary. */
export const SWEEP_BUDGET_MS = 150;
/** At most this many directories are removed per run (§4.3). */
export const SWEEP_MAX_REMOVALS = 20;
/** A session directory holding more entries than this is never judged idle (bounds the lstat calls). */
const SWEEP_MAX_ENTRIES_PER_DIR = 256;
const SWEEP_STATE_FILE = ".sweep.json";
const TRASH_RE = /^\.trash-\d{1,16}-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

interface SweepState {
  v: 1;
  /** No new pass starts before this (epoch ms). */
  next_after: number;
  /** The last name a cut-off pass handled; the next run resumes after it. */
  cursor: string | null;
}

export interface SweepResult {
  removed: number;
  scanned: number;
  /** Orphaned counts-file temp files removed from `.runtime`. */
  temps_removed: number;
  /** The pass reached the end of the directory. */
  complete: boolean;
  /** Nothing to do: no directory yet, or the last complete pass is too recent. */
  skipped: boolean;
}

function sweepStateIn(raw: unknown): SweepState | undefined {
  if (raw === null || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  if (value.v !== 1 || typeof value.next_after !== "number" || !Number.isFinite(value.next_after)) return undefined;
  const cursor = value.cursor;
  if (cursor !== null && !(typeof cursor === "string" && (SESSION_KEY_RE.test(cursor) || TRASH_RE.test(cursor)))) {
    return undefined;
  }
  return { v: 1, next_after: value.next_after, cursor };
}

/** Whether every entry of a session directory (and the directory) is older than the horizon. */
function isIdleSessionDir(dir: string, dirStat: fs.Stats, now: number): boolean {
  let newest = dirStat.mtimeMs;
  const entries = fs.readdirSync(dir);
  if (entries.length > SWEEP_MAX_ENTRIES_PER_DIR) return false;
  for (const entry of entries) {
    newest = Math.max(newest, fs.lstatSync(path.join(dir, entry)).mtimeMs);
  }
  return now - newest > SESSION_IDLE_MAX_MS;
}

/** A counts-file temp file this old was left by a writer that died before its rename. */
export const TEMP_ORPHAN_AGE_MS = 60 * 60 * 1000;
const TEMPLATE_TMP_RE =
  /^prompt-templates\.json\.\d{1,10}\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.tmp$/;

/**
 * Remove `prompt-templates.json.<pid>.<uuid>.tmp` files older than an hour
 * from `.runtime` (recordOccurrence's temp names, orphaned by a writer that
 * died before its rename). Regular files only, reached through the
 * containment walk; at most SWEEP_MAX_REMOVALS, stopping when `spent()`.
 */
function sweepTemplateTemps(vaultPath: string, now: number, spent: () => boolean): number {
  const runtime = containedDir(vaultPath, [".runtime"], false);
  if (runtime === null) return 0;
  let removed = 0;
  for (const name of fs.readdirSync(runtime)) {
    if (!TEMPLATE_TMP_RE.test(name)) continue;
    if (spent() || removed >= SWEEP_MAX_REMOVALS) break;
    try {
      const target = path.join(runtime, name);
      const stat = fs.lstatSync(target);
      if (!stat.isFile() || now - stat.mtimeMs <= TEMP_ORPHAN_AGE_MS) continue;
      fs.unlinkSync(target);
      removed += 1;
    } catch {
      // Gone already, or not ours to remove.
    }
  }
  return removed;
}

/** Handle one name; true when a directory was removed. Never follows a symlink. */
function sweepOne(root: string, name: string, now: number): boolean {
  const target = path.join(root, name);
  const stat = fs.lstatSync(target);
  // A symlink or a plain file is never followed and never removed.
  if (!stat.isDirectory()) return false;
  if (TRASH_RE.test(name)) {
    // A run cut off between its rename and its removal.
    fs.rmSync(target, { recursive: true, force: true });
    return true;
  }
  if (!isIdleSessionDir(target, stat, now)) return false;
  // Renamed first, so a removal cut short leaves a .trash- name the next run
  // finishes, never a half-emptied session directory.
  const trash = path.join(root, `.trash-${Math.floor(now)}-${name}`);
  fs.renameSync(target, trash);
  fs.rmSync(trash, { recursive: true, force: true });
  return true;
}

/**
 * Remove `recall-session.d/<session_key>` directories idle for 90 days, and
 * finish any `.trash-` left by a cut-off run. Bounded: at most 20 removals and
 * about `budgetMs` per run, checked between items, with a cursor so the next
 * run resumes where this one stopped; after a complete pass the next waits a
 * day. The directory is reached through the containment walk, and nothing in
 * it that is a symlink is followed. Never throws.
 */
export function sweepSessionDirs(
  vaultPath: string,
  options: { now?: number; budgetMs?: number; clock?: () => number; deadline?: number } = {},
): SweepResult {
  const clock = options.clock ?? Date.now;
  const result: SweepResult = { removed: 0, scanned: 0, temps_removed: 0, complete: false, skipped: false };
  // With a shared `deadline` (runSessionMaintenance) a spent budget does
  // nothing at all; on its own, the budget runs from this call's start.
  let spent: () => boolean;
  let now: number;
  if (options.deadline !== undefined) {
    const deadline = options.deadline;
    if (clock() >= deadline) {
      result.skipped = true;
      return result;
    }
    spent = () => clock() >= deadline;
    now = options.now ?? Date.now();
  } else {
    const started = clock();
    const budgetMs = options.budgetMs ?? SWEEP_BUDGET_MS;
    spent = () => clock() - started >= budgetMs;
    now = options.now ?? started;
  }
  try {
    const root = containedDir(vaultPath, SESSION_RELDIR.split(path.sep), false);
    if (root === null) {
      result.skipped = true;
      return result;
    }
    const stateFile = path.join(root, SWEEP_STATE_FILE);
    let state: SweepState = { v: 1, next_after: 0, cursor: null };
    try {
      state = sweepStateIn(readSideFile(stateFile, vaultPath)) ?? state;
    } catch {
      // Unreadable state: start a fresh pass.
    }
    if (state.cursor === null && now < state.next_after) {
      result.skipped = true;
      return result;
    }
    try {
      result.temps_removed = sweepTemplateTemps(vaultPath, now, spent);
    } catch {
      // The session pass below does not depend on it.
    }
    const names = fs
      .readdirSync(root)
      .filter((name) => SESSION_KEY_RE.test(name) || TRASH_RE.test(name))
      .sort();
    const cursor = state.cursor;
    let index = cursor === null ? 0 : names.findIndex((name) => name > cursor);
    if (index < 0) index = names.length;
    let last = cursor;
    for (; index < names.length; index += 1) {
      if (spent() || result.removed >= SWEEP_MAX_REMOVALS) break;
      const name = names[index] as string;
      result.scanned += 1;
      try {
        if (sweepOne(root, name, now)) result.removed += 1;
      } catch {
        // One directory that cannot be judged or removed does not stop the pass.
      }
      last = name;
    }
    result.complete = index >= names.length;
    writeSideFile(
      stateFile,
      result.complete
        ? { v: 1, next_after: now + SWEEP_INTERVAL_MS, cursor: null }
        : { v: 1, next_after: state.next_after, cursor: last },
      vaultPath,
    );
  } catch {
    // Best effort: retention runs again next boot.
  }
  return result;
}

/** ONE budget for all of SessionStart's post-delivery maintenance (§4.3, §6.2.5). */
export const SESSION_MAINTENANCE_BUDGET_MS = 150;

export interface SessionMaintenanceResult {
  /** Null only when the maintenance itself failed before the sweep ran. */
  sessions: SweepResult | null;
  ledger: LedgerSweepResult | null;
}

/**
 * SessionStart's post-delivery maintenance, under ONE deadline: the
 * `recall-session.d` sweep, then the ledger's retention (its finalize step
 * included), in that fixed order, each handed what is left of the shared
 * budget (one absolute deadline). A sweep that finds it passed does nothing
 * (`skipped`) and resumes next time from its own cursor. The deadline is
 * checked before every item, so the total can overrun by one item's work: the
 * item in progress when it passes (one directory removal, or one session's
 * idle close, which is at least one session even on a tiny budget). Never
 * throws.
 */
export async function runSessionMaintenance(
  vaultPath: string,
  options: { budgetMs?: number; clock?: () => number; now?: number; finalizer?: Finalizer } = {},
): Promise<SessionMaintenanceResult> {
  const clock = options.clock ?? Date.now;
  const deadline = clock() + (options.budgetMs ?? SESSION_MAINTENANCE_BUDGET_MS);
  const out: SessionMaintenanceResult = { sessions: null, ledger: null };
  try {
    // Fixed order, one absolute deadline: each sweep checks it before every
    // item, and does nothing when it finds it passed.
    out.sessions = sweepSessionDirs(vaultPath, { deadline, clock, now: options.now });
    out.ledger = await sweepLedgerDirs(vaultPath, { deadline, clock, now: options.now, finalizer: options.finalizer });
  } catch {
    // Best effort: maintenance runs again next boot.
  }
  return out;
}

/**
 * SessionStart: run the maintenance (above) once the boot output is settled,
 * on every outcome (S1a-2 review F4): it consumes nothing, so delivery is
 * irrelevant to it. It runs as the commit when the output was delivered, and
 * as the abandon handler when it was not (a wire that drops SessionStart,
 * such as Grok, would otherwise never sweep, and both trees would grow without
 * bound). Record-only, so an abandoned run is not counted as a rollback. It
 * adds to the hook's wall time (P16), hence its single budget; after a
 * complete pass each sweep waits a day, so a typical boot pays two cursor
 * reads. On the throw path the abandon handler starts before the degraded
 * output is flushed and is awaited within hook-delivery's settle budget.
 */
export function scheduleSessionSweep(capture: UseCapture | null): void {
  if (capture === null) return;
  const sweep = async (): Promise<void> => {
    await runSessionMaintenance(capture.vaultPath);
  };
  deferUntilDelivered(sweep, { recordOnly: true, onAbandon: sweep });
}
