import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { GROK_WORKER_START } from "./thread-host-dispatch.js";

/**
 * G3 notification relay — not a second graph authority.
 *
 * The daemon/runtime stores delivery cursors (and a rebuildable pending
 * queue), not Thread graph state. Journal seq rebuilds the queue. A failed
 * wake leaves the cursor behind the event. Cursor advances monotonically
 * on successful delivery only.
 *
 * Hooks are fallback readers of pending attention. They do not poll
 * `minni_thread_events`. Notifications are concise attributed state
 * deltas for an already-working orchestrator: not raw evidence, not
 * instructions, not a human inbox.
 *
 * Immediate wake is used only where wet-tested. G2 in-session
 * worker_update complete is not a spawn. `spawned` stays false.
 * `GROK_WORKER_START` stays null. Do not invent a grok start API.
 */

export { GROK_WORKER_START };

/** Hooks read this store. They do not call the journal tool. */
export const HOOKS_POLL_MINNI_THREAD_EVENTS = false as const;

/** This store is delivery state, never slices/deps/claims/evidence. */
export const RELAY_IS_GRAPH_STATE = false as const;

/** SQLite 020 thread_delivery_cursors is unused in production. Live store is cursors.json. */
export const SQLITE_020_LIVE = false as const;

/** G2 in-session complete is not a spawn. Landing G3 does not spawn. */
export const SPAWNED = false as const;

export const RELAY_STORE_VERSION = 1 as const;

export type RelayHost = "agy" | "grok" | "codex" | "cursor";
export type RelayHook = "sessionStart" | "promptSubmit" | "stop";
export type WakeMode = "immediate" | "deferred" | "unsupported";
export type InjectionCapability = "injects" | "ignored" | "rejects" | "cannot" | "out";

/** Wire / agent ids that map onto a G3 relay host. Unknown ids fail closed. */
const RELAY_HOST_ALIASES: Record<string, RelayHost> = {
  agy: "agy",
  gemini: "agy",
  antigravity: "agy",
  grok: "grok",
  "grok-build": "grok",
  codex: "codex",
  cursor: "cursor",
};

export function relayHostFor(id: string): RelayHost | undefined {
  const key = id.trim().toLowerCase();
  return RELAY_HOST_ALIASES[key];
}

/**
 * Confirm delivery=true ONLY when this host's wire for this hook actually
 * injects. ignored / rejects / cannot / out leave the cursor behind.
 * Unknown hosts fail closed (do not confirm true).
 */
export function hostHookInjects(host: RelayHost, hook: RelayHook): boolean {
  return HOST_INJECTION_TABLE[host][hook] === "injects";
}

export function deliverySucceededForHostHook(hostId: string, hook: RelayHook): boolean {
  const host = relayHostFor(hostId);
  if (host === undefined) return false;
  return hostHookInjects(host, hook);
}

export interface HostInjectionRow {
  host: RelayHost;
  sessionStart: InjectionCapability;
  promptSubmit: InjectionCapability;
  stop: InjectionCapability;
  wire: string;
  wake: WakeMode;
  wetImmediate: false;
  spawned: false;
  grokWorkerStart: null;
}

/**
 * Injection from the existing wire only. Immediate wake is unsupported
 * everywhere: no host has wet-tested it. agy/grok can defer through
 * hooks. Codex stays UNPROVEN (unsupported wake, still in the set).
 * Cursor is out.
 */
export const HOST_INJECTION_TABLE: Record<RelayHost, HostInjectionRow> = {
  agy: {
    host: "agy",
    sessionStart: "injects",
    promptSubmit: "injects",
    stop: "rejects",
    wire: "SessionStart + PreInvocation injectSteps",
    wake: "deferred",
    wetImmediate: false,
    spawned: false,
    grokWorkerStart: null,
  },
  grok: {
    host: "grok",
    sessionStart: "ignored",
    promptSubmit: "ignored",
    stop: "injects",
    wire: "Stop injects; SessionStart/UPS stdout ignored; boot ~/.grok/rules",
    wake: "deferred",
    wetImmediate: false,
    spawned: false,
    grokWorkerStart: null,
  },
  codex: {
    host: "codex",
    sessionStart: "injects",
    promptSubmit: "injects",
    stop: "cannot",
    wire: "SessionStart + UserPromptSubmit; Stop cannot; UNPROVEN",
    wake: "unsupported",
    wetImmediate: false,
    spawned: false,
    grokWorkerStart: null,
  },
  cursor: {
    host: "cursor",
    sessionStart: "out",
    promptSubmit: "out",
    stop: "out",
    wire: "out",
    wake: "unsupported",
    wetImmediate: false,
    spawned: false,
    grokWorkerStart: null,
  },
};

export interface JournalEvent {
  seq: number;
  kind: string;
  actor: string;
  at: string;
  slice_id?: string;
}

export interface DeliveryCursor {
  subscriber_id: string;
  plan_id: string;
  last_delivered_seq: number;
}

export interface RelayNotification {
  seq: number;
  plan_id: string;
  subscriber_id: string;
  actor: string;
  kind: string;
  slice_id?: string;
  delta: string;
  at: string;
}

export interface RelayStore {
  version: typeof RELAY_STORE_VERSION;
  graph: false;
  cursors: DeliveryCursor[];
  pending: RelayNotification[];
}

export function emptyRelayStore(): RelayStore {
  return {
    version: RELAY_STORE_VERSION,
    graph: false,
    cursors: [],
    pending: [],
  };
}

export function relayDir(vaultPath: string): string {
  return path.join(vaultPath, ".runtime", "thread-relay");
}

export function relayStorePath(vaultPath: string): string {
  return path.join(relayDir(vaultPath), "cursors.json");
}

const GRAPH_KEYS = [
  "slices",
  "dependencies",
  "depends_on",
  "claims",
  "claim_token",
  "evidence",
  "gate",
  "assigned_to",
  "generation",
] as const;

export function storeHoldsGraphState(store: RelayStore): boolean {
  if (store.graph !== false) return true;
  const raw = store as unknown as Record<string, unknown>;
  return GRAPH_KEYS.some((key) => key in raw);
}

export function formatStateDelta(event: JournalEvent, planId: string): string {
  const slice = event.slice_id ? ` slice=${event.slice_id}` : "";
  return `${event.actor} ${event.kind} plan=${planId}${slice} seq=${event.seq}`;
}

export function rebuildQueueFromJournal(
  planId: string,
  events: readonly JournalEvent[],
  cursor: DeliveryCursor,
): RelayNotification[] {
  return events
    .filter((event) => event.seq > cursor.last_delivered_seq)
    .slice()
    .sort((a, b) => a.seq - b.seq)
    .map((event) => {
      const notification: RelayNotification = {
        seq: event.seq,
        plan_id: planId,
        subscriber_id: cursor.subscriber_id,
        actor: event.actor,
        kind: event.kind,
        delta: formatStateDelta(event, planId),
        at: event.at,
      };
      if (event.slice_id) notification.slice_id = event.slice_id;
      return notification;
    });
}

/**
 * Fail-closed: a failed delivery leaves the cursor behind the event.
 * Success advances monotonically; a lower seq is ignored.
 */
export function advanceCursor(
  cursor: DeliveryCursor,
  deliveredThroughSeq: number,
  deliverySucceeded: boolean,
): DeliveryCursor {
  if (!deliverySucceeded) return { ...cursor };
  if (deliveredThroughSeq <= cursor.last_delivered_seq) return { ...cursor };
  return { ...cursor, last_delivered_seq: deliveredThroughSeq };
}

/**
 * Who gets pending after a journal append.
 *
 * Old seed was `{current append actor} ∪ existing cursors for that plan`.
 * That notifies the writer on a cursor-less store and leaves the
 * already-working orchestrator empty. Landed journal actors and any
 * plan orchestrator / coordinator identity must be seeded too.
 */
export function seedRelaySubscribers(input: {
  actor?: string;
  planId: string;
  cursors?: readonly DeliveryCursor[];
  events?: readonly { actor?: string }[];
  extraIds?: readonly string[];
}): string[] {
  const ids = new Set<string>();
  const add = (value: string | undefined) => {
    const id = typeof value === "string" ? value.trim() : "";
    if (id) ids.add(id);
  };
  add(input.actor);
  for (const cursor of input.cursors ?? []) {
    if (cursor.plan_id === input.planId) add(cursor.subscriber_id);
  }
  for (const event of input.events ?? []) {
    add(event.actor);
  }
  for (const extra of input.extraIds ?? []) {
    add(extra);
  }
  return [...ids];
}

export function cursorFor(
  store: RelayStore,
  subscriberId: string,
  planId: string,
): DeliveryCursor {
  return (
    store.cursors.find(
      (cursor) => cursor.subscriber_id === subscriberId && cursor.plan_id === planId,
    ) ?? { subscriber_id: subscriberId, plan_id: planId, last_delivered_seq: 0 }
  );
}

export function ingestJournalEvents(
  store: RelayStore,
  planId: string,
  events: readonly JournalEvent[],
  subscriberIds: readonly string[],
): RelayStore {
  const next = emptyRelayStore();
  next.cursors = store.cursors.map((cursor) => ({ ...cursor }));
  next.pending = store.pending.filter((item) => item.plan_id !== planId);
  const seen = new Set<string>();
  for (const subscriberId of subscriberIds) {
    seen.add(subscriberId);
    const cursor = cursorFor(next, subscriberId, planId);
    if (!next.cursors.some((row) => row.subscriber_id === subscriberId && row.plan_id === planId)) {
      next.cursors.push(cursor);
    }
    next.pending.push(...rebuildQueueFromJournal(planId, events, cursor));
  }
  for (const cursor of next.cursors) {
    if (cursor.plan_id === planId && !seen.has(cursor.subscriber_id)) {
      next.pending.push(...rebuildQueueFromJournal(planId, events, cursor));
    }
  }
  next.pending.sort(
    (a, b) =>
      a.seq - b.seq ||
      a.subscriber_id.localeCompare(b.subscriber_id) ||
      a.plan_id.localeCompare(b.plan_id),
  );
  return next;
}

export function confirmDelivery(
  store: RelayStore,
  subscriberId: string,
  planId: string,
  deliveredThroughSeq: number,
  deliverySucceeded: boolean,
): RelayStore {
  const current = cursorFor(store, subscriberId, planId);
  const advanced = advanceCursor(current, deliveredThroughSeq, deliverySucceeded);
  const next = emptyRelayStore();
  next.cursors = store.cursors.filter(
    (cursor) => !(cursor.subscriber_id === subscriberId && cursor.plan_id === planId),
  );
  next.cursors.push(advanced);
  // Prune ONLY this subscriber's queue. A shared plan+seq filter would drop
  // subscriber B's 2–5 when A confirms seq 5.
  next.pending = store.pending.filter((item) => {
    if (item.subscriber_id !== subscriberId) return true;
    if (item.plan_id !== planId) return true;
    return item.seq > advanced.last_delivered_seq;
  });
  return next;
}

export interface ImmediateWakeResult {
  host: RelayHost;
  mode: WakeMode;
  spawned: false;
  cursorAdvanced: false;
  grokWorkerStart: null;
  wetImmediate: false;
}

/** Immediate wake only where wet-tested. None are. Never advances the cursor. */
export function attemptImmediateWake(host: RelayHost): ImmediateWakeResult {
  const row = HOST_INJECTION_TABLE[host];
  return {
    host,
    mode: row.wake,
    spawned: false,
    cursorAdvanced: false,
    grokWorkerStart: GROK_WORKER_START,
    wetImmediate: false,
  };
}

export interface PendingAttention {
  notifications: RelayNotification[];
  text: string;
  hooksPollThreadEvents: false;
  spawned: false;
}

export function readPendingAttention(
  store: RelayStore,
  subscriberId: string,
  planId: string,
): PendingAttention {
  const cursor = cursorFor(store, subscriberId, planId);
  const notifications = store.pending.filter(
    (item) =>
      item.subscriber_id === subscriberId &&
      item.plan_id === planId &&
      item.seq > cursor.last_delivered_seq,
  );
  return {
    notifications,
    text: notifications.map((item) => item.delta).join("\n"),
    hooksPollThreadEvents: HOOKS_POLL_MINNI_THREAD_EVENTS,
    spawned: false,
  };
}

export async function loadRelayStore(vaultPath: string): Promise<RelayStore> {
  try {
    const raw = JSON.parse(await readFile(relayStorePath(vaultPath), "utf8")) as RelayStore;
    if (raw?.version !== RELAY_STORE_VERSION || raw.graph !== false) {
      return emptyRelayStore();
    }
    return {
      version: RELAY_STORE_VERSION,
      graph: false,
      cursors: Array.isArray(raw.cursors) ? raw.cursors : [],
      pending: Array.isArray(raw.pending) ? raw.pending : [],
    };
  } catch {
    return emptyRelayStore();
  }
}

export async function saveRelayStore(vaultPath: string, store: RelayStore): Promise<void> {
  if (storeHoldsGraphState(store)) {
    throw new Error("thread relay refuses graph state");
  }
  await mkdir(relayDir(vaultPath), { recursive: true, mode: 0o700 });
  const payload = `${JSON.stringify({ ...store, graph: false }, null, 2)}\n`;
  await writeFile(relayStorePath(vaultPath), payload, { encoding: "utf8", mode: 0o600 });
}

export async function readPendingAttentionFromVault(
  vaultPath: string,
  subscriberId: string,
  planId: string,
): Promise<PendingAttention> {
  const store = await loadRelayStore(vaultPath);
  return readPendingAttention(store, subscriberId, planId);
}

export async function confirmDeliveryInVault(
  vaultPath: string,
  subscriberId: string,
  planId: string,
  deliveredThroughSeq: number,
  deliverySucceeded: boolean,
): Promise<DeliveryCursor> {
  const store = await loadRelayStore(vaultPath);
  const next = confirmDelivery(store, subscriberId, planId, deliveredThroughSeq, deliverySucceeded);
  await saveRelayStore(vaultPath, next);
  return cursorFor(next, subscriberId, planId);
}

/**
 * Hook fallback reader. Attaches concise attributed deltas. Does not call
 * `minni_thread_events`. Caller must only confirm delivery after the host
 * accepted the envelope (see hook-delivery fail-closed).
 */
export async function pendingAttentionForHook(input: {
  vaultPath: string;
  subscriberId: string;
  planId?: string;
}): Promise<PendingAttention | null> {
  const planId = input.planId?.trim();
  if (!planId) return null;
  const pending = await readPendingAttentionFromVault(input.vaultPath, input.subscriberId, planId);
  if (pending.notifications.length === 0) return null;
  return pending;
}

export async function activePlanIdFromVault(vaultPath: string): Promise<string | undefined> {
  try {
    const raw = JSON.parse(
      await readFile(path.join(vaultPath, "wiki", "artifacts", "_active_plan.json"), "utf8"),
    ) as { plan_id?: unknown };
    const planId = typeof raw.plan_id === "string" ? raw.plan_id.trim() : "";
    return planId || undefined;
  } catch {
    return undefined;
  }
}

export function planJournalPath(vaultPath: string, planId: string): string {
  return path.join(vaultPath, "wiki", "artifacts", `${planId}.log.md`);
}

/** Journal lives at <vault>/wiki/artifacts/<planId>.log.md. */
export function vaultPathFromJournalPath(journalPath: string): string | undefined {
  const artifactsDir = path.dirname(journalPath);
  const wikiDir = path.dirname(artifactsDir);
  if (path.basename(artifactsDir) !== "artifacts" || path.basename(wikiDir) !== "wiki") {
    return undefined;
  }
  return path.dirname(wikiDir);
}

export function parseJournalEventsFromLog(raw: string): JournalEvent[] {
  const events: JournalEvent[] = [];
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("{")) continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (!parsed || typeof parsed !== "object") continue;
    const record = parsed as Record<string, unknown>;
    const batch = record.thread_event_batch;
    const items: unknown[] = Array.isArray(batch) ? [...batch] : [];
    if (typeof record.seq === "number" && typeof record.kind === "string") {
      items.push(record);
    }
    for (const item of items) {
      if (!item || typeof item !== "object") continue;
      const row = item as Record<string, unknown>;
      if (typeof row.seq !== "number" || typeof row.kind !== "string") continue;
      const event: JournalEvent = {
        seq: row.seq,
        kind: row.kind,
        actor: typeof row.actor === "string" ? row.actor : "",
        at: typeof row.at === "string" ? row.at : "",
      };
      if (typeof row.slice_id === "string" && row.slice_id) event.slice_id = row.slice_id;
      events.push(event);
    }
  }
  events.sort((a, b) => a.seq - b.seq);
  return events;
}

/**
 * Production ingest: persist rebuilt per-subscriber pending from journal seq
 * into cursors.json. Not a second graph. Not an MCP tool. Hooks remain
 * readers of that store and do not poll minni_thread_events.
 */
export async function ingestJournalIntoVault(
  vaultPath: string,
  planId: string,
  events: readonly JournalEvent[],
  subscriberIds: readonly string[],
): Promise<RelayStore> {
  const store = await loadRelayStore(vaultPath);
  const next = ingestJournalEvents(store, planId, events, subscriberIds);
  await saveRelayStore(vaultPath, next);
  return next;
}

export async function ingestPlanJournalFromDisk(
  vaultPath: string,
  planId: string,
  subscriberIds: readonly string[],
): Promise<RelayStore> {
  let events: JournalEvent[] = [];
  try {
    const raw = await readFile(planJournalPath(vaultPath, planId), "utf8");
    events = parseJournalEventsFromLog(raw);
  } catch {
    events = [];
  }
  return ingestJournalIntoVault(vaultPath, planId, events, subscriberIds);
}
