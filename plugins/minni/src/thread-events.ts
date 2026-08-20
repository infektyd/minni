import { createHash } from "node:crypto";
import { open, readFile, stat } from "node:fs/promises";

import { stableStringify } from "./agent_envelope.js";
import { type AppendJournalDeps } from "./plan.js";
import { appendFileWithFsync, writeFileAtomic } from "./vault.js";
import {
  ingestJournalIntoVault,
  loadRelayStore,
  seedRelaySubscribers,
  vaultPathFromJournalPath,
  type JournalEvent,
} from "./thread-notification-relay.js";

export interface OrderedThreadEvent {
  seq: number;
  rev: number;
  event_id: string;
  idempotency_key: string;
  actor: string;
  kind: string;
  at: string;
  slice_id?: string;
  payload?: Record<string, unknown>;
}

export interface ReadySummaryPayload {
  slices: Array<{ id: string; title: string }>;
}

export interface OperationEventIdentity {
  idempotencyKey: string;
  kind: string;
  actor: string;
  sliceId?: string;
}

export interface AppendOrderedEventBatchInput {
  journalPath: string;
  planId: string;
  rev: number;
  actor: string;
  at?: string;
  orderedSnapshot?: OrderedThreadEvent[];
  events: Array<{
    idempotencyKey: string;
    kind: string;
    sliceId?: string;
    payload?: Record<string, unknown>;
  }>;
}

export interface EnsureOrderedBaselineInput {
  journalPath: string;
  planId: string;
  rev: number;
  actor: string;
  at?: string;
  readySummary: ReadySummaryPayload;
  orderedSnapshot?: OrderedThreadEvent[];
}

export interface ReconcileThreadJournalInput {
  journalPath: string;
  notePath: string;
  planId: string;
  rev: number;
  actor: string;
  at?: string;
  readySummary: ReadySummaryPayload;
  orderedSnapshot?: OrderedThreadEvent[];
}

export class ThreadInconsistentError extends Error {
  readonly code = "THREAD_INCONSISTENT" as const;

  constructor(noteRev: number, journalRev: number) {
    super(
      `thread_inconsistent: note rev ${noteRev} is behind journal rev ${journalRev}`,
    );
    this.name = "ThreadInconsistentError";
  }
}

export class ThreadEventIdempotencyConflictError extends Error {
  readonly code = "THREAD_EVENT_IDEMPOTENCY_CONFLICT" as const;

  constructor(idempotencyKey: string) {
    super(
      `thread_event_idempotency_conflict: idempotency key "${idempotencyKey}" is already bound to a different operation`,
    );
    this.name = "ThreadEventIdempotencyConflictError";
  }
}

const NODE_ERRNO_CODE = /^E[A-Z][A-Z0-9]{1,30}$/;

function nodeErrnoCode(error: unknown): string | undefined {
  if (typeof error === "object" && error !== null && "code" in error) {
    const code = (error as { code: unknown }).code;
    if (typeof code === "string" && NODE_ERRNO_CODE.test(code)) {
      return code;
    }
  }
  return undefined;
}

/**
 * Ordered-journal load failed for a reason other than a missing file.
 * `journalPath` stays a typed field — never interpolate it into `.message`.
 * ENOENT is not this error: a missing journal is an empty cursor.
 */
export class ThreadJournalReadError extends Error {
  readonly code = "THREAD_JOURNAL_UNREADABLE" as const;
  readonly journalPath: string;
  readonly causeCode?: string;

  constructor(journalPath: string, cause: unknown) {
    const causeCode = nodeErrnoCode(cause);
    super(
      causeCode
        ? `thread journal is unreadable: ${causeCode}`
        : "thread journal is unreadable",
      cause instanceof Error ? { cause } : undefined,
    );
    this.name = "ThreadJournalReadError";
    this.journalPath = journalPath;
    this.causeCode = causeCode;
  }
}

/**
 * Ordered journal append failed after the note mutation was already durable,
 * and the mutation's operation event is not on the cursor. Callers must not
 * report MCP OK for this — success and cursor-moved are the same moment, or
 * this typed error. Distinct from THREAD_JOURNAL_UNREADABLE (read path).
 */
export class ThreadJournalAppendError extends Error {
  readonly code = "THREAD_JOURNAL_APPEND_FAILED" as const;
  readonly operationKey: string;
  readonly kind: string;
  readonly causeCode?: string;

  constructor(operationKey: string, kind: string, cause?: unknown) {
    const causeCode = nodeErrnoCode(cause);
    super(
      causeCode
        ? `thread journal append failed for ${kind}: ${causeCode}`
        : `thread journal append failed for ${kind}: ordered append failed`,
      cause instanceof Error ? { cause } : undefined,
    );
    this.name = "ThreadJournalAppendError";
    this.operationKey = operationKey;
    this.kind = kind;
    this.causeCode = causeCode;
  }
}

/** Wire kinds for an honest ordered-journal hole. Same payload shape. */
export const JOURNAL_TRUNCATION_KIND = "journal_truncated";
export const CURSOR_GAP_KIND = "cursor_gap";

export interface JournalTruncationPayload {
  last_dropped_seq: number;
  first_kept_seq: number;
}

/**
 * since_seq poller would jump over missing seqs with no journal_truncated /
 * cursor_gap marker explaining last_dropped_seq + first_kept_seq. Fail closed:
 * a silent hole is worse than an unbounded parse.
 */
export class ThreadCursorGapError extends Error {
  readonly code = "THREAD_CURSOR_GAP" as const;
  readonly sinceSeq: number;
  readonly firstKeptSeq: number;

  constructor(sinceSeq: number, firstKeptSeq: number) {
    super(
      `unmarked cursor_gap: since_seq ${sinceSeq} jumps to first_kept_seq ${firstKeptSeq} without journal_truncated`,
    );
    this.name = "ThreadCursorGapError";
    this.sinceSeq = sinceSeq;
    this.firstKeptSeq = firstKeptSeq;
  }
}

/** Default max bytes the cursor reader will load from the end of a journal. */
export const ORDERED_JOURNAL_CURSOR_TAIL_BYTES = 1024 * 1024;

export interface ReadThreadEventsOptions {
  /** Override the cursor read/parse byte bound (tests + deliberate tuning). */
  maxReadBytes?: number;
}

export function isJournalGapKind(kind: string): boolean {
  return kind === JOURNAL_TRUNCATION_KIND || kind === CURSOR_GAP_KIND;
}

export function journalTruncationPayload(
  event: OrderedThreadEvent,
): JournalTruncationPayload | undefined {
  if (!isJournalGapKind(event.kind)) return undefined;
  const payload = event.payload;
  if (payload === undefined || typeof payload !== "object" || payload === null) {
    return undefined;
  }
  const lastDropped = (payload as Record<string, unknown>).last_dropped_seq;
  const firstKept = (payload as Record<string, unknown>).first_kept_seq;
  if (
    typeof lastDropped !== "number" ||
    !Number.isSafeInteger(lastDropped) ||
    lastDropped < 1 ||
    typeof firstKept !== "number" ||
    !Number.isSafeInteger(firstKept) ||
    firstKept <= lastDropped
  ) {
    return undefined;
  }
  return { last_dropped_seq: lastDropped, first_kept_seq: firstKept };
}

function isErrno(error: unknown, code: string): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    (error as { code: unknown }).code === code
  );
}

/** Missing journal → undefined. Any other read failure → ThreadJournalReadError. */
async function readOrderedJournalText(
  journalPath: string,
): Promise<string | undefined> {
  try {
    return await readFile(journalPath, "utf8");
  } catch (error) {
    if (isErrno(error, "ENOENT")) {
      return undefined;
    }
    throw new ThreadJournalReadError(journalPath, error);
  }
}

/**
 * Cursor-path read: load at most maxReadBytes from the file tail. When the
 * prefix is skipped, the caller must emit journal_truncated / cursor_gap or
 * fall back to a full parse — never pretend the stream is contiguous from seq 1.
 */
async function readOrderedJournalTextBounded(
  journalPath: string,
  maxReadBytes: number,
): Promise<{ text: string; prefixTruncated: boolean } | undefined> {
  let fileSize: number;
  try {
    fileSize = (await stat(journalPath)).size;
  } catch (error) {
    if (isErrno(error, "ENOENT")) {
      return undefined;
    }
    throw new ThreadJournalReadError(journalPath, error);
  }

  if (fileSize <= maxReadBytes) {
    const text = await readOrderedJournalText(journalPath);
    if (text === undefined) {
      return undefined;
    }
    return { text, prefixTruncated: false };
  }

  try {
    const handle = await open(journalPath, "r");
    try {
      const length = Math.min(maxReadBytes, fileSize);
      const start = fileSize - length;
      const buffer = Buffer.alloc(length);
      const { bytesRead } = await handle.read(buffer, 0, length, start);
      let text = buffer.toString("utf8", 0, bytesRead);
      const newline = text.indexOf("\n");
      if (newline === -1) {
        return { text: "", prefixTruncated: true };
      }
      // Discard the leading torn line so parse never invents a half-event.
      text = text.slice(newline + 1);
      return { text, prefixTruncated: true };
    } finally {
      await handle.close();
    }
  } catch (error) {
    if (isErrno(error, "ENOENT")) {
      return undefined;
    }
    throw new ThreadJournalReadError(journalPath, error);
  }
}

function synthesizeCursorGapForReadBound(
  firstKept: OrderedThreadEvent,
): OrderedThreadEvent {
  const lastDropped = firstKept.seq - 1;
  const idempotencyKey = deriveSystemEventKey(
    CURSOR_GAP_KIND,
    String(lastDropped),
    String(firstKept.seq),
  );
  return {
    seq: lastDropped,
    rev: firstKept.rev,
    event_id: createHash("sha256")
      .update(
        stableStringify({
          kind: CURSOR_GAP_KIND,
          last_dropped_seq: lastDropped,
          first_kept_seq: firstKept.seq,
        }),
      )
      .digest("hex")
      .slice(0, 32),
    idempotency_key: idempotencyKey,
    actor: "minni",
    kind: CURSOR_GAP_KIND,
    at: firstKept.at,
    payload: {
      last_dropped_seq: lastDropped,
      first_kept_seq: firstKept.seq,
    },
  };
}

/**
 * After a tail-only read, surface cursor_gap when event seqs were skipped.
 * Uses contiguous-seq honesty: last_dropped = first_kept - 1. Does not rewrite
 * the file. Returns undefined when the tail cannot name those bounds — caller
 * must leave the parse unbounded instead of inventing a second protocol.
 */
function applyCursorReadBound(
  events: OrderedThreadEvent[],
  prefixTruncated: boolean,
): OrderedThreadEvent[] | undefined {
  if (!prefixTruncated) {
    return events;
  }
  const firstKept = events.find((event) => !isJournalGapKind(event.kind));
  if (firstKept === undefined) {
    return undefined;
  }
  if (firstKept.seq <= 1) {
    return events;
  }
  const covering = events.find((event) => {
    const payload = journalTruncationPayload(event);
    return payload !== undefined && payload.first_kept_seq === firstKept.seq;
  });
  if (covering) {
    return events;
  }
  return [synthesizeCursorGapForReadBound(firstKept), ...events];
}

const ORDERED_JOURNAL_HEAD_PEEK_BYTES = 8 * 1024;

/**
 * Read the start of the journal just far enough to see whether the file
 * itself begins with an unmarked hole. Not a full parse.
 */
async function peekOrderedJournalHead(
  journalPath: string,
): Promise<OrderedThreadEvent[] | undefined> {
  let fileSize: number;
  try {
    fileSize = (await stat(journalPath)).size;
  } catch (error) {
    if (isErrno(error, "ENOENT")) {
      return undefined;
    }
    throw new ThreadJournalReadError(journalPath, error);
  }
  const length = Math.min(ORDERED_JOURNAL_HEAD_PEEK_BYTES, fileSize);
  if (length < 1) {
    return [];
  }
  try {
    const handle = await open(journalPath, "r");
    try {
      const buffer = Buffer.alloc(length);
      const { bytesRead } = await handle.read(buffer, 0, length, 0);
      let text = buffer.toString("utf8", 0, bytesRead);
      if (fileSize > length) {
        const lastNewline = text.lastIndexOf("\n");
        if (lastNewline === -1) {
          return [];
        }
        text = text.slice(0, lastNewline + 1);
      }
      return parseOrderedThreadEvents(text);
    } finally {
      await handle.close();
    }
  } catch (error) {
    if (isErrno(error, "ENOENT")) {
      return undefined;
    }
    throw new ThreadJournalReadError(journalPath, error);
  }
}

/**
 * A tail bound hides a hole when it would turn THREAD_CURSOR_GAP (or a
 * durable marker the tail cannot see) into a successful synthesized skip.
 * Unbounded parse > that lie.
 */
async function tailBoundWouldHideHole(
  journalPath: string,
  sinceSeq: number,
  tailEvents: OrderedThreadEvent[],
  tailFirstKept: OrderedThreadEvent,
): Promise<boolean> {
  if (
    findCoveringGapMarker(tailEvents, sinceSeq, tailFirstKept.seq) !== undefined
  ) {
    return false;
  }
  if (tailFirstKept.seq <= sinceSeq + 1) {
    return false;
  }
  // Unread seqs after a live cursor were not inspected. Synthesizing a
  // cover would hide an unmarked hole parked behind since_seq.
  if (sinceSeq > 0) {
    return true;
  }
  const head = await peekOrderedJournalHead(journalPath);
  if (head === undefined || head.length === 0) {
    return true;
  }
  const headFirstKept = head.find((event) => !isJournalGapKind(event.kind));
  if (headFirstKept === undefined) {
    return true;
  }
  if (headFirstKept.seq > 1) {
    return true;
  }
  return false;
}

/**
 * Cursor load. Bound the tail when that can emit an honest gap kind without
 * hiding a hole; otherwise full-parse. Mutation stays on readOrderedThreadEvents.
 */
async function loadOrderedEventsForCursor(
  journalPath: string,
  maxReadBytes: number,
  sinceSeq: number,
): Promise<OrderedThreadEvent[]> {
  if (maxReadBytes < 1) {
    return readOrderedThreadEvents(journalPath);
  }
  const loaded = await readOrderedJournalTextBounded(journalPath, maxReadBytes);
  if (loaded === undefined) {
    orderedJournalParseCount += 1;
    return [];
  }
  if (!loaded.prefixTruncated) {
    orderedJournalParseCount += 1;
    return parseOrderedThreadEvents(loaded.text);
  }
  const tailEvents = parseOrderedThreadEvents(loaded.text);
  const tailFirstKept = tailEvents.find((event) => !isJournalGapKind(event.kind));
  if (tailFirstKept === undefined) {
    return readOrderedThreadEvents(journalPath);
  }
  if (await tailBoundWouldHideHole(journalPath, sinceSeq, tailEvents, tailFirstKept)) {
    return readOrderedThreadEvents(journalPath);
  }
  const bounded = applyCursorReadBound(tailEvents, true);
  if (bounded === undefined) {
    return readOrderedThreadEvents(journalPath);
  }
  orderedJournalParseCount += 1;
  return bounded;
}

/** Namespaced journal key for client-supplied idempotency (claim/worker). */
export function deriveClientEventKey(
  scope: string,
  identity: Record<string, unknown>,
): string {
  const hash = createHash("sha256")
    .update(stableStringify(identity))
    .digest("hex")
    .slice(0, 32);
  return `client:${scope}:${hash}`;
}

/** Namespaced journal key for structural/system events (never raw client keys). */
export function deriveSystemEventKey(kind: string, ...parts: string[]): string {
  return `system:${kind}:${parts.join(":")}`;
}

export function deriveReadyChangedKey(operationKey: string): string {
  return `${operationKey}:ready`;
}

export function findRecoveryEvent(
  ordered: OrderedThreadEvent[],
  noteRev: number,
): OrderedThreadEvent | undefined {
  return ordered.find(
    (event) => event.kind === "state.recovered" && event.rev === noteRev,
  );
}

function recoveryKeyCollisionSuffix(
  primaryKey: string,
  conflicting: OrderedThreadEvent,
): string {
  return createHash("sha256")
    .update(
      stableStringify({
        key: primaryKey,
        event_id: conflicting.event_id,
        kind: conflicting.kind,
        rev: conflicting.rev,
      }),
    )
    .digest("hex")
    .slice(0, 16);
}

/** Pick a system recovery key, avoiding a historical client-key collision. */
export function deriveRecoveryEventKey(
  ordered: OrderedThreadEvent[],
  rev: number,
): string {
  const primary = deriveSystemEventKey("state.recovered", String(rev));
  const existing = findOrderedEventByIdempotencyKey(ordered, primary);
  if (!existing) return primary;
  if (existing.kind === "state.recovered" && existing.rev === rev) {
    return primary;
  }
  const suffix = recoveryKeyCollisionSuffix(primary, existing);
  const alternate = deriveSystemEventKey("state.recovered", String(rev), suffix);
  const alternateExisting = findOrderedEventByIdempotencyKey(ordered, alternate);
  if (
    !alternateExisting ||
    (alternateExisting.kind === "state.recovered" &&
      alternateExisting.rev === rev)
  ) {
    return alternate;
  }
  return deriveSystemEventKey(
    "state.recovered",
    String(rev),
    recoveryKeyCollisionSuffix(alternate, alternateExisting),
  );
}

interface ThreadEventBatchLine {
  thread_event_batch: OrderedThreadEvent[];
}

function deriveEventId(
  planId: string,
  seq: number,
  idempotencyKey: string,
): string {
  return createHash("sha256")
    .update(
      stableStringify({
        plan_id: planId,
        seq,
        idempotency_key: idempotencyKey,
      }),
    )
    .digest("hex")
    .slice(0, 32);
}

function isOrderedThreadEvent(value: unknown): value is OrderedThreadEvent {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const event = value as Record<string, unknown>;
  return (
    typeof event.seq === "number" &&
    Number.isSafeInteger(event.seq) &&
    event.seq > 0 &&
    typeof event.rev === "number" &&
    Number.isSafeInteger(event.rev) &&
    event.rev >= 0 &&
    typeof event.event_id === "string" &&
    typeof event.idempotency_key === "string" &&
    typeof event.actor === "string" &&
    typeof event.kind === "string" &&
    typeof event.at === "string"
  );
}

function isIncompleteJsonLine(
  trimmed: string,
  lineIndex: number,
  lineCount: number,
  journalText: string,
): boolean {
  if (!trimmed.startsWith("{")) return true;
  if (lineIndex === lineCount - 1 && !journalText.endsWith("\n")) {
    try {
      JSON.parse(trimmed);
      return false;
    } catch {
      return true;
    }
  }
  return false;
}

/**
 * Parse durable ordered events, including journal_truncated / cursor_gap wire
 * kinds. Does not invent a gap marker. Slice 1 only surfaces a durable kind.
 */
export function parseOrderedThreadEvents(
  journalText: string,
): OrderedThreadEvent[] {
  const lines = journalText.split(/\r?\n/);
  const events: OrderedThreadEvent[] = [];

  for (let index = 0; index < lines.length; index += 1) {
    const trimmed = lines[index].trim();
    if (!trimmed || !trimmed.startsWith("{")) continue;
    if (isIncompleteJsonLine(trimmed, index, lines.length, journalText)) {
      continue;
    }
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (
        typeof parsed === "object" &&
        parsed !== null &&
        Array.isArray((parsed as ThreadEventBatchLine).thread_event_batch)
      ) {
        for (const item of (parsed as ThreadEventBatchLine).thread_event_batch) {
          if (isOrderedThreadEvent(item)) {
            events.push(item);
          }
        }
        continue;
      }
      if (isOrderedThreadEvent(parsed)) {
        events.push(parsed);
      }
    } catch {
      // ignore malformed complete lines
    }
  }

  return events.sort((left, right) => left.seq - right.seq);
}

/** Incremented on each ordered-journal load in readOrderedThreadEvents (test contract). */
export let orderedJournalParseCount = 0;

export function resetOrderedJournalParseCountForTests(): void {
  orderedJournalParseCount = 0;
}

export async function readOrderedThreadEvents(
  journalPath: string,
): Promise<OrderedThreadEvent[]> {
  orderedJournalParseCount += 1;
  const journalText = await readOrderedJournalText(journalPath);
  if (journalText === undefined) {
    return [];
  }
  return parseOrderedThreadEvents(journalText);
}

function findCoveringGapMarker(
  ordered: OrderedThreadEvent[],
  sinceSeq: number,
  firstKeptSeq: number,
): OrderedThreadEvent | undefined {
  for (const event of ordered) {
    const payload = journalTruncationPayload(event);
    if (!payload) continue;
    if (payload.first_kept_seq !== firstKeptSeq) continue;
    // Poller still sits before the kept window — surface the marker even when
    // its own seq is <= since_seq (cursor parked inside the hole).
    if (sinceSeq < payload.first_kept_seq) {
      return event;
    }
  }
  return undefined;
}

/**
 * Cursor page after since_seq. Byte-bounded tail read by default. Surfaces
 * journal_truncated / cursor_gap when the poller would otherwise jump a
 * marked hole; throws THREAD_CURSOR_GAP on an unmarked jump. Never
 * renumbers seq. Does not delete or rotate the file. If the tail cannot
 * name last_dropped_seq + first_kept_seq, or if bounding would hide a
 * hole, falls back to unbounded parse.
 */
export async function readThreadEvents(
  journalPath: string,
  sinceSeq = 0,
  limit = 100,
  options: ReadThreadEventsOptions = {},
): Promise<{ events: OrderedThreadEvent[]; next_seq: number }> {
  const maxReadBytes =
    options.maxReadBytes ?? ORDERED_JOURNAL_CURSOR_TAIL_BYTES;
  const ordered = await loadOrderedEventsForCursor(
    journalPath,
    maxReadBytes,
    sinceSeq,
  );
  const firstKept = ordered.find(
    (event) => event.seq > sinceSeq && !isJournalGapKind(event.kind),
  );
  const expectedNext = sinceSeq + 1;
  let leadingGap: OrderedThreadEvent | undefined;

  if (firstKept !== undefined && firstKept.seq > expectedNext) {
    const marker = findCoveringGapMarker(ordered, sinceSeq, firstKept.seq);
    if (!marker || !journalTruncationPayload(marker)) {
      throw new ThreadCursorGapError(sinceSeq, firstKept.seq);
    }
    leadingGap = marker;
  }

  const filtered = ordered.filter((event) => event.seq > sinceSeq);
  const page: OrderedThreadEvent[] = [];
  if (
    leadingGap !== undefined &&
    !filtered.some((event) => event.event_id === leadingGap.event_id)
  ) {
    page.push(leadingGap);
  }
  for (const event of filtered) {
    if (page.length >= limit) break;
    page.push(event);
  }

  let nextSeq = page.length > 0 ? page[page.length - 1].seq : sinceSeq;
  const last = page.length > 0 ? page[page.length - 1] : undefined;
  const lastPayload = last ? journalTruncationPayload(last) : undefined;
  if (lastPayload) {
    // limit=1 may return only the marker; advance past the hole so the next
    // poll lands on first_kept_seq instead of re-entering a silent jump.
    nextSeq = Math.max(nextSeq, lastPayload.first_kept_seq - 1);
  }
  return {
    events: page,
    next_seq: nextSeq,
  };
}

export function findOrderedEventByIdempotencyKey(
  ordered: OrderedThreadEvent[],
  idempotencyKey: string,
): OrderedThreadEvent | undefined {
  return ordered.find((event) => event.idempotency_key === idempotencyKey);
}

export function operationIdentityMatches(
  event: OrderedThreadEvent,
  identity: OperationEventIdentity,
): boolean {
  return (
    event.idempotency_key === identity.idempotencyKey &&
    event.kind === identity.kind &&
    event.actor === identity.actor &&
    (identity.sliceId === undefined || event.slice_id === identity.sliceId)
  );
}

export function assertOperationIdentity(
  event: OrderedThreadEvent,
  identity: OperationEventIdentity,
): void {
  if (!operationIdentityMatches(event, identity)) {
    throw new ThreadEventIdempotencyConflictError(identity.idempotencyKey);
  }
}

function nextSequence(ordered: OrderedThreadEvent[]): number {
  return ordered.reduce((highest, event) => Math.max(highest, event.seq), 0) + 1;
}

/** Disk load for snapshot resync — does not increment orderedJournalParseCount. */
async function loadOrderedThreadEventsWithoutParseCount(
  journalPath: string,
): Promise<OrderedThreadEvent[]> {
  const journalText = await readOrderedJournalText(journalPath);
  if (journalText === undefined) {
    return [];
  }
  return parseOrderedThreadEvents(journalText);
}

function replaceOrderedSnapshotContents(
  snapshot: OrderedThreadEvent[],
  diskEvents: OrderedThreadEvent[],
): void {
  snapshot.length = 0;
  for (const event of diskEvents) {
    snapshot.push(event);
  }
}

function orderedSnapshotsEqual(
  left: OrderedThreadEvent[],
  right: OrderedThreadEvent[],
): boolean {
  if (left.length !== right.length) {
    return false;
  }
  for (let index = 0; index < left.length; index += 1) {
    const leftEvent = left[index];
    const rightEvent = right[index];
    if (
      leftEvent.seq !== rightEvent.seq ||
      leftEvent.event_id !== rightEvent.event_id
    ) {
      return false;
    }
  }
  return true;
}

/** Whether a shared snapshot matches the durable ordered journal on disk. */
export async function orderedSnapshotMatchesJournal(
  snapshot: OrderedThreadEvent[],
  journalPath: string,
): Promise<boolean> {
  const diskEvents = await loadOrderedThreadEventsWithoutParseCount(journalPath);
  return orderedSnapshotsEqual(snapshot, diskEvents);
}

async function appendJournalLine(
  journalPath: string,
  payload: unknown,
  deps: AppendJournalDeps = {},
): Promise<void> {
  const doAppendWithFsync = deps.appendFileWithFsync ?? appendFileWithFsync;
  const doWriteAtomic = deps.writeFileAtomic ?? writeFileAtomic;
  const line = `${JSON.stringify(payload)}\n`;

  let existing: string;
  try {
    existing = await readFile(journalPath, "utf8");
  } catch (error) {
    if (!isErrno(error, "ENOENT")) {
      throw error;
    }
    const header = `# Minni Plan Journal\n\n## events\n`;
    await doWriteAtomic(journalPath, header + line);
    return;
  }

  const prefix =
    existing.length > 0 && !existing.endsWith("\n") ? "\n" : "";
  await doAppendWithFsync(journalPath, prefix + line);
}

function materializeBatchEvents(
  ordered: OrderedThreadEvent[],
  input: AppendOrderedEventBatchInput,
): OrderedThreadEvent[] {
  const at = input.at ?? new Date().toISOString();
  let seqCursor = nextSequence(ordered);
  const materialized: OrderedThreadEvent[] = [];

  for (const spec of input.events) {
    const existing = findOrderedEventByIdempotencyKey(ordered, spec.idempotencyKey);
    if (existing) {
      assertOperationIdentity(existing, {
        idempotencyKey: spec.idempotencyKey,
        kind: spec.kind,
        actor: input.actor,
        sliceId: spec.sliceId,
      });
      materialized.push(existing);
      continue;
    }

    const event: OrderedThreadEvent = {
      seq: seqCursor,
      rev: input.rev,
      event_id: deriveEventId(input.planId, seqCursor, spec.idempotencyKey),
      idempotency_key: spec.idempotencyKey,
      actor: input.actor,
      kind: spec.kind,
      at,
      ...(spec.sliceId ? { slice_id: spec.sliceId } : {}),
      ...(spec.payload ? { payload: spec.payload } : {}),
    };
    materialized.push(event);
    ordered = [...ordered, event];
    seqCursor += 1;
  }

  return materialized;
}


async function ingestRelayAfterJournalAppend(input: {
  journalPath: string;
  planId: string;
  actor: string;
  events: OrderedThreadEvent[];
}): Promise<void> {
  const vaultPath = vaultPathFromJournalPath(input.journalPath);
  if (!vaultPath) return;
  try {
    const store = await loadRelayStore(vaultPath);
    const events: JournalEvent[] = input.events.map((event) => {
      const item: JournalEvent = {
        seq: event.seq,
        kind: event.kind,
        actor: event.actor,
        at: event.at,
      };
      if (event.slice_id) item.slice_id = event.slice_id;
      return item;
    });
    const subscriberIds = seedRelaySubscribers({
      actor: input.actor,
      planId: input.planId,
      cursors: store.cursors,
      events,
    });
    await ingestJournalIntoVault(vaultPath, input.planId, events, subscriberIds);
  } catch {
    // Relay is not SoT. The journal line already landed.
  }
}

export async function appendOrderedEventBatch(
  input: AppendOrderedEventBatchInput,
  deps: AppendJournalDeps = {},
): Promise<OrderedThreadEvent[]> {
  const ordered =
    input.orderedSnapshot ?? await readOrderedThreadEvents(input.journalPath);
  const allExisting = input.events.every((spec) =>
    findOrderedEventByIdempotencyKey(ordered, spec.idempotencyKey)
  );
  if (allExisting) {
    return input.events.map((spec) => {
      const existing = findOrderedEventByIdempotencyKey(
        ordered,
        spec.idempotencyKey,
      );
      if (!existing) {
        throw new Error("appendOrderedEventBatch: missing existing event");
      }
      assertOperationIdentity(existing, {
        idempotencyKey: spec.idempotencyKey,
        kind: spec.kind,
        actor: input.actor,
        sliceId: spec.sliceId,
      });
      return existing;
    });
  }

  const materialized = materializeBatchEvents(ordered, input);
  const fresh = materialized.filter(
    (event) =>
      !findOrderedEventByIdempotencyKey(ordered, event.idempotency_key),
  );
  if (fresh.length === 0) {
    return materialized;
  }

  const batchLine: ThreadEventBatchLine = {
    thread_event_batch: fresh,
  };
  try {
    await appendJournalLine(input.journalPath, batchLine, deps);
    if (input.orderedSnapshot) {
      for (const event of fresh) {
        input.orderedSnapshot.push(event);
      }
    }
    const landed = input.orderedSnapshot ?? [...ordered, ...fresh];
    await ingestRelayAfterJournalAppend({
      journalPath: input.journalPath,
      planId: input.planId,
      actor: input.actor,
      events: landed,
    });
    return materialized;
  } catch (error) {
    if (input.orderedSnapshot) {
      try {
        const diskEvents = await loadOrderedThreadEventsWithoutParseCount(
          input.journalPath,
        );
        replaceOrderedSnapshotContents(input.orderedSnapshot, diskEvents);
      } catch {
        // Unreadable journal is not empty. Leave the live snapshot alone
        // and rethrow the original append failure.
      }
    }
    throw error;
  }
}

/** @deprecated Prefer appendOrderedEventBatch; retained for low-level tests. */
export async function appendOrderedThreadEvent(
  input: {
    journalPath: string;
    planId: string;
    rev: number;
    idempotencyKey: string;
    actor: string;
    kind: string;
    at?: string;
    sliceId?: string;
    payload?: Record<string, unknown>;
  },
  deps: AppendJournalDeps = {},
): Promise<OrderedThreadEvent> {
  const [event] = await appendOrderedEventBatch(
    {
      journalPath: input.journalPath,
      planId: input.planId,
      rev: input.rev,
      actor: input.actor,
      at: input.at,
      events: [
        {
          idempotencyKey: input.idempotencyKey,
          kind: input.kind,
          sliceId: input.sliceId,
          payload: input.payload,
        },
      ],
    },
    deps,
  );
  return event;
}

export async function ensureOrderedBaseline(
  input: EnsureOrderedBaselineInput,
  deps: AppendJournalDeps = {},
): Promise<OrderedThreadEvent | undefined> {
  const ordered =
    input.orderedSnapshot ?? await readOrderedThreadEvents(input.journalPath);
  if (ordered.length > 0) {
    return undefined;
  }
  const [baseline] = await appendOrderedEventBatch(
    {
      journalPath: input.journalPath,
      planId: input.planId,
      rev: input.rev,
      actor: input.actor,
      at: input.at,
      orderedSnapshot: input.orderedSnapshot ?? ordered,
      events: [
        {
          idempotencyKey: deriveSystemEventKey("state.baseline", String(input.rev)),
          kind: "state.baseline",
          payload: { ready: input.readySummary },
        },
      ],
    },
    deps,
  );
  return baseline;
}

export async function reconcileThreadJournal(
  input: ReconcileThreadJournalInput,
  deps: AppendJournalDeps = {},
): Promise<"ok" | "recovered"> {
  const ordered =
    input.orderedSnapshot ?? await readOrderedThreadEvents(input.journalPath);
  if (ordered.length === 0) {
    return "ok";
  }

  const journalRev = ordered.reduce(
    (highest, event) => Math.max(highest, event.rev),
    0,
  );

  if (input.rev < journalRev) {
    throw new ThreadInconsistentError(input.rev, journalRev);
  }
  if (input.rev === journalRev) {
    return "ok";
  }

  if (findRecoveryEvent(ordered, input.rev)) {
    return "recovered";
  }

  const recoveryKey = deriveRecoveryEventKey(ordered, input.rev);
  await appendOrderedEventBatch(
    {
      journalPath: input.journalPath,
      planId: input.planId,
      rev: input.rev,
      actor: input.actor,
      at: input.at,
      orderedSnapshot: input.orderedSnapshot ?? ordered,
      events: [
        {
          idempotencyKey: recoveryKey,
          kind: "state.recovered",
          payload: { ready: input.readySummary },
        },
      ],
    },
    deps,
  );
  return "recovered";
}
