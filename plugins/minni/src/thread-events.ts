import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

import { stableStringify } from "./agent_envelope.js";
import {
  appendJournal,
  type AppendJournalDeps,
  type PlanEvent,
} from "./plan.js";

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

export interface AppendOrderedThreadEventInput {
  journalPath: string;
  planId: string;
  rev: number;
  idempotencyKey: string;
  actor: string;
  kind: string;
  at?: string;
  sliceId?: string;
  payload?: Record<string, unknown>;
}

export interface ReconcileThreadJournalInput {
  journalPath: string;
  notePath: string;
  planId: string;
  rev: number;
  actor: string;
  at?: string;
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

export function parseOrderedThreadEvents(
  journalText: string,
): OrderedThreadEvent[] {
  const events: OrderedThreadEvent[] = [];
  for (const line of journalText.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || !trimmed.startsWith("{") || !trimmed.endsWith("}")) {
      continue;
    }
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (isOrderedThreadEvent(parsed)) {
        events.push(parsed);
      }
    } catch {
      // ignore malformed lines
    }
  }
  return events.sort((left, right) => left.seq - right.seq);
}

async function readOrderedEvents(
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
  const ordered = await readOrderedEvents(journalPath);
  const filtered = ordered.filter((event) => event.seq > sinceSeq);
  const page = filtered.slice(0, limit);
  return {
    events: page,
    next_seq: page.length > 0 ? page[page.length - 1].seq : sinceSeq,
  };
}

export async function appendOrderedThreadEvent(
  input: AppendOrderedThreadEventInput,
  deps: AppendJournalDeps = {},
): Promise<OrderedThreadEvent> {
  const ordered = await readOrderedEvents(input.journalPath);
  const existing = ordered.find(
    (event) => event.idempotency_key === input.idempotencyKey,
  );
  if (existing) {
    return existing;
  }

  const seq =
    ordered.reduce((highest, event) => Math.max(highest, event.seq), 0) + 1;
  const at = input.at ?? new Date().toISOString();
  const event: OrderedThreadEvent = {
    seq,
    rev: input.rev,
    event_id: deriveEventId(input.planId, seq, input.idempotencyKey),
    idempotency_key: input.idempotencyKey,
    actor: input.actor,
    kind: input.kind,
    at,
    ...(input.sliceId ? { slice_id: input.sliceId } : {}),
    ...(input.payload ? { payload: input.payload } : {}),
  };
  await appendJournal(
    input.journalPath,
    event as unknown as PlanEvent,
    deps,
  );
  return event;
}

export async function reconcileThreadJournal(
  input: ReconcileThreadJournalInput,
  deps: AppendJournalDeps = {},
): Promise<"ok" | "recovered"> {
  const ordered = await readOrderedEvents(input.journalPath);
  const journalRev =
    ordered.length > 0
      ? ordered.reduce((highest, event) => Math.max(highest, event.rev), 0)
      : input.rev;

  if (input.rev < journalRev) {
    throw new ThreadInconsistentError(input.rev, journalRev);
  }
  if (input.rev === journalRev) {
    return "ok";
  }

  const recoveryKey = `state.recovered:${input.rev}`;
  const existingRecovery = ordered.find(
    (event) => event.idempotency_key === recoveryKey,
  );
  if (existingRecovery) {
    return "recovered";
  }

  await appendOrderedThreadEvent(
    {
      journalPath: input.journalPath,
      planId: input.planId,
      rev: input.rev,
      idempotencyKey: recoveryKey,
      actor: input.actor,
      kind: "state.recovered",
      at: input.at,
    },
    deps,
  );
  return "recovered";
}
