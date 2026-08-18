import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

import { stableStringify } from "./agent_envelope.js";
import { type AppendJournalDeps } from "./plan.js";
import { appendFileWithFsync, writeFileAtomic } from "./vault.js";

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
}

export interface ReconcileThreadJournalInput {
  journalPath: string;
  notePath: string;
  planId: string;
  rev: number;
  actor: string;
  at?: string;
  readySummary: ReadySummaryPayload;
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

export async function readOrderedThreadEvents(
  journalPath: string,
): Promise<OrderedThreadEvent[]> {
  try {
    const journalText = await readFile(journalPath, "utf8");
    return parseOrderedThreadEvents(journalText);
  } catch {
    return [];
  }
}

export async function readThreadEvents(
  journalPath: string,
  sinceSeq = 0,
  limit = 100,
): Promise<{ events: OrderedThreadEvent[]; next_seq: number }> {
  const ordered = await readOrderedThreadEvents(journalPath);
  const filtered = ordered.filter((event) => event.seq > sinceSeq);
  const page = filtered.slice(0, limit);
  return {
    events: page,
    next_seq: page.length > 0 ? page[page.length - 1].seq : sinceSeq,
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

async function appendJournalLine(
  journalPath: string,
  payload: unknown,
  deps: AppendJournalDeps = {},
): Promise<void> {
  const doAppendWithFsync = deps.appendFileWithFsync ?? appendFileWithFsync;
  const doWriteAtomic = deps.writeFileAtomic ?? writeFileAtomic;
  const line = `${JSON.stringify(payload)}\n`;

  try {
    const existing = await readFile(journalPath, "utf8");
    const prefix =
      existing.length > 0 && !existing.endsWith("\n") ? "\n" : "";
    await doAppendWithFsync(journalPath, prefix + line);
  } catch {
    const header = `# Minni Plan Journal\n\n## events\n`;
    await doWriteAtomic(journalPath, header + line);
  }
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

export async function appendOrderedEventBatch(
  input: AppendOrderedEventBatchInput,
  deps: AppendJournalDeps = {},
): Promise<OrderedThreadEvent[]> {
  const ordered = await readOrderedThreadEvents(input.journalPath);
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
  await appendJournalLine(input.journalPath, batchLine, deps);
  return materialized;
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
  const ordered = await readOrderedThreadEvents(input.journalPath);
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
  const ordered = await readOrderedThreadEvents(input.journalPath);
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
