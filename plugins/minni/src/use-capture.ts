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
// MINNI_USE_LEDGER off, `openDispatchCapture` returns null and no file of any
// kind is written.
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
//   prompt-templates.json                             the classifier's counts
//       (harness-envelope.ts; UPS is its only writer)
//
// Privacy: rows hold keys, hashes and sanitized titles only. The prompt text and
// its template hash never reach the ledger.
import { randomBytes, randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { stableStringify } from "./agent_envelope.js";
import { extractLearningsSection } from "./daemon_transport.js";
import type { JsonResult, ReadContextResponse, RecallResponse } from "./daemon_transport.js";
import { FLAG_NAMES } from "./flags.js";
import type { FlagSnapshot } from "./flags.js";
import { classifyPromptEnvelope, readPromptTemplates, recordOccurrence } from "./harness-envelope.js";
import type { PromptTemplateEntry, PromptTemplatesFile } from "./harness-envelope.js";
import { wireCapabilities } from "./hook-platform.js";
import type { PlatformWire } from "./hook-platform.js";
import { asString } from "./hook-utils.js";
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

/** Events that open a capture: the ones that surface, count a turn, or reset. */
const CAPTURE_EVENTS: ReadonlySet<string> = new Set([
  "SessionStart",
  "UserPromptSubmit",
  "PreToolUse",
  "PreCompact",
  "PostCompact",
]);

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

/**
 * The capture for one dispatched event, or null when the event captures
 * nothing or MINNI_USE_LEDGER is off. Reads nothing but the payload and the
 * already-loaded flags. Never throws.
 */
export function openDispatchCapture(args: {
  vaultPath: string;
  event: string;
  payload: Record<string, unknown>;
  wire: PlatformWire;
  principal: string;
  flags: FlagSnapshot;
}): UseCapture | null {
  try {
    if (!CAPTURE_EVENTS.has(args.event)) return null;
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
function readSideFile(file: string, vaultPath: string): unknown {
  assertWriteTargetUnder(file, vaultPath);
  let fd: number;
  try {
    fd = fs.openSync(file, READ_FLAGS);
  } catch (error) {
    if (errorCode(error) === "ENOENT") return undefined;
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

/** Increment and return this session's turn counter; 0 when it cannot be kept. */
function nextTurnIndex(capture: UseCapture): number {
  try {
    const dir = sessionDir(capture, true);
    if (dir === null) return 0;
    const file = path.join(dir, "counters.json");
    let current = 0;
    try {
      current = turnIndexIn(readSideFile(file, capture.vaultPath)) ?? 0;
    } catch {
      // Unreadable or corrupt: the write below decides whether we can go on.
    }
    const next = current + 1;
    writeSideFile(file, { v: 1, turn_ix: next }, capture.vaultPath);
    return next;
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

// Built from a string, as flags.ts does, to keep the literal out of no-control-regex.
const MKEY_RE = new RegExp(
  "^(?:vault:[^\\u0000-\\u001F\\u007F]{1,1024}|doc:[A-Za-z0-9_-]{1,32}:[A-Za-z0-9_.-]{1,128}|" +
    "learning:[A-Za-z0-9_.-]{1,128}|lhash:[0-9a-f]{12})$",
);

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
      const mkey = typeof value.mkey === "string" && MKEY_RE.test(value.mkey) ? value.mkey : null;
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
