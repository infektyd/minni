import { randomUUID } from "node:crypto";
import { mkdir, open, readFile, readdir, rename, rm, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

import { isLiveMatchingProcess } from "./config.js";
import { inboxPrincipalForVaultPath } from "./hook-utils.js";
import { GROK_WORKER_START } from "./thread-host-dispatch.js";
import { assertWriteTargetUnder } from "./vault.js";

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

export type RelayHost = "agy" | "grok" | "codex" | "cursor" | "claude-code";
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
  claude: "claude-code",
  "claude-code": "claude-code",
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
  "claude-code": {
    host: "claude-code",
    sessionStart: "injects",
    promptSubmit: "injects",
    stop: "out",
    wire: "SessionStart + UserPromptSubmit; Stop additionalContext wakes stopped turn so relay is out",
    wake: "deferred",
    wetImmediate: false,
    spawned: false,
    grokWorkerStart: null,
  },
};

export interface JournalEvent {
  seq: number;
  kind: string;
  actor: string;
  actor_session_key?: string;
  at: string;
  slice_id?: string;
}

export interface DeliveryCursor {
  subscriber_id: string;
  plan_id: string;
  last_delivered_seq: number;
  updated_at?: string;
}

export interface RelayNotification {
  seq: number;
  plan_id: string;
  subscriber_id: string;
  actor: string;
  actor_session_key?: string;
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

const SESSION_KEY_HEX_RE = /^[0-9a-f]{16}$/;
const SAFE_AGENT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/i;
export const MAX_RELAY_CURSORS = 256;
export const DEAD_CURSOR_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const RELAY_CURSOR_PRUNE_AGE_MS = DEAD_CURSOR_MAX_AGE_MS;
export const RELAY_LOCK_TIMEOUT_MS = 5_000;
export const RELAY_LOCK_STALE_MS = 10_000;

export function formatSessionSubscriberId(
  agentId: string,
  sessionKey?: string | null,
): string {
  const cleanAgent = typeof agentId === "string" ? agentId.trim() : "";
  if (!cleanAgent) return "";
  const cleanKey = typeof sessionKey === "string" ? sessionKey.trim() : "";
  if (
    cleanKey &&
    cleanKey !== "default" &&
    SESSION_KEY_HEX_RE.test(cleanKey) &&
    SAFE_AGENT_ID_RE.test(cleanAgent)
  ) {
    return `${cleanAgent}:${cleanKey}`;
  }
  return cleanAgent;
}

export function parseSubscriberPrincipal(subscriberId: string): {
  agentId: string;
  sessionKey?: string;
} {
  const trimmed = typeof subscriberId === "string" ? subscriberId.trim() : "";
  const colonIdx = trimmed.indexOf(":");
  if (colonIdx > 0 && colonIdx === trimmed.lastIndexOf(":")) {
    const agentId = trimmed.slice(0, colonIdx);
    const sessionKey = trimmed.slice(colonIdx + 1);
    if (SAFE_AGENT_ID_RE.test(agentId) && SESSION_KEY_HEX_RE.test(sessionKey)) {
      return { agentId, sessionKey };
    }
  }
  return { agentId: trimmed };
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
  const parsedSub = parseSubscriberPrincipal(cursor.subscriber_id);
  return events
    .filter((event) => {
      if (event.seq <= cursor.last_delivered_seq) return false;
      if (
        parsedSub.sessionKey &&
        (event.actor === cursor.subscriber_id || event.actor === parsedSub.agentId) &&
        event.actor_session_key === parsedSub.sessionKey
      ) {
        return false;
      }
      return true;
    })
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
      if (event.actor_session_key && SESSION_KEY_HEX_RE.test(event.actor_session_key)) {
        notification.actor_session_key = event.actor_session_key;
      }
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
  const fallbackUpdatedAt = cursor.updated_at ?? new Date().toISOString();
  if (!deliverySucceeded) return { ...cursor, updated_at: fallbackUpdatedAt };
  if (deliveredThroughSeq <= cursor.last_delivered_seq) {
    return { ...cursor, updated_at: fallbackUpdatedAt };
  }
  return {
    ...cursor,
    last_delivered_seq: deliveredThroughSeq,
    updated_at: new Date().toISOString(),
  };
}

/**
 * Honest orchestrator subscriber for this vault, if one exists.
 *
 * PlanArtifact has no owner. DEFAULT_AGENT_ID on a worker append is the
 * writer. Hook config.agentId is not on the journal path. The product
 * already derives a fail-closed vault principal from the `<slug>-vault`
 * directory name (`inboxPrincipalForVaultPath`). No `-vault` suffix →
 * undefined. Do not invent a subscriber.
 */
export function vaultPrincipalSubscriberId(vaultPath: string): string | undefined {
  return inboxPrincipalForVaultPath(vaultPath);
}

/**
 * Who gets pending after a journal append.
 *
 * Old seed was `{current append actor} ∪ existing cursors for that plan`.
 * That notifies the writer on a cursor-less store and leaves the
 * already-working orchestrator empty. Landed journal actors and the
 * fail-closed vault principal (when the vault dir names one) must be
 * seeded too. extraIds is how production passes that principal.
 */
export function seedRelaySubscribers(input: {
  actor?: string;
  actorSessionKey?: string;
  planId: string;
  cursors?: readonly DeliveryCursor[];
  events?: readonly { actor?: string; actor_session_key?: string }[];
  extraIds?: readonly string[];
}): string[] {
  const ids = new Set<string>();
  const add = (value: string | undefined) => {
    const id = typeof value === "string" ? value.trim() : "";
    if (id) ids.add(id);
  };
  add(input.actor);
  if (input.actor && input.actorSessionKey) {
    add(formatSessionSubscriberId(input.actor, input.actorSessionKey));
  }
  for (const cursor of input.cursors ?? []) {
    if (cursor.plan_id === input.planId) add(cursor.subscriber_id);
  }
  for (const event of input.events ?? []) {
    add(event.actor);
    if (event.actor && event.actor_session_key) {
      add(formatSessionSubscriberId(event.actor, event.actor_session_key));
    }
  }
  for (const extra of input.extraIds ?? []) {
    add(extra);
  }
  return [...ids];
}

export function forkSessionCursor(
  store: RelayStore,
  subscriberId: string,
  planId: string,
): DeliveryCursor {
  const nowIso = new Date().toISOString();
  const existing = store.cursors.find(
    (cursor) => cursor.subscriber_id === subscriberId && cursor.plan_id === planId,
  );
  if (existing) {
    return { ...existing, updated_at: existing.updated_at ?? nowIso };
  }
  const parsed = parseSubscriberPrincipal(subscriberId);
  if (parsed.sessionKey) {
    const bare = store.cursors.find(
      (cursor) => cursor.subscriber_id === parsed.agentId && cursor.plan_id === planId,
    );
    if (bare) {
      return {
        subscriber_id: subscriberId,
        plan_id: planId,
        last_delivered_seq: bare.last_delivered_seq,
        updated_at: nowIso,
      };
    }
  }
  return {
    subscriber_id: subscriberId,
    plan_id: planId,
    last_delivered_seq: 0,
    updated_at: nowIso,
  };
}

export function cursorFor(
  store: RelayStore,
  subscriberId: string,
  planId: string,
): DeliveryCursor {
  const existing = store.cursors.find(
    (cursor) => cursor.subscriber_id === subscriberId && cursor.plan_id === planId,
  );
  if (existing) {
    return { ...existing, updated_at: existing.updated_at ?? new Date().toISOString() };
  }
  return forkSessionCursor(store, subscriberId, planId);
}

export function ingestJournalEvents(
  store: RelayStore,
  planId: string,
  events: readonly JournalEvent[],
  subscriberIds: readonly string[],
): RelayStore {
  const nowIso = new Date().toISOString();
  const priorSeqsBySub = new Map<string, string>();
  for (const item of store.pending) {
    if (item.plan_id !== planId) continue;
    const prev = priorSeqsBySub.get(item.subscriber_id);
    priorSeqsBySub.set(item.subscriber_id, prev ? `${prev},${item.seq}` : String(item.seq));
  }
  const next = emptyRelayStore();
  next.cursors = store.cursors.map((cursor) => ({ ...cursor }));
  next.pending = store.pending.filter((item) => item.plan_id !== planId);
  const seen = new Set<string>();
  for (const subscriberId of subscriberIds) {
    seen.add(subscriberId);
    let cursor = next.cursors.find(
      (row) => row.subscriber_id === subscriberId && row.plan_id === planId,
    );
    const rebuilt = rebuildQueueFromJournal(
      planId,
      events,
      cursor ?? forkSessionCursor(next, subscriberId, planId),
    );
    const nextSeqSig = rebuilt.map((item) => item.seq).join(",");
    const queueMutated = (priorSeqsBySub.get(subscriberId) ?? "") !== nextSeqSig;
    if (!cursor) {
      cursor = {
        ...forkSessionCursor(next, subscriberId, planId),
        updated_at: nowIso,
      };
      next.cursors.push(cursor);
    } else if (queueMutated || !cursor.updated_at) {
      cursor.updated_at = nowIso;
    }
    next.pending.push(...rebuilt);
  }
  for (const cursor of next.cursors) {
    if (cursor.plan_id === planId && !seen.has(cursor.subscriber_id)) {
      const rebuilt = rebuildQueueFromJournal(planId, events, cursor);
      const nextSeqSig = rebuilt.map((item) => item.seq).join(",");
      if ((priorSeqsBySub.get(cursor.subscriber_id) ?? "") !== nextSeqSig || !cursor.updated_at) {
        cursor.updated_at = nowIso;
      }
      next.pending.push(...rebuilt);
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
  const nowIso = new Date().toISOString();
  const current = cursorFor(store, subscriberId, planId);
  const advancedBase = advanceCursor(current, deliveredThroughSeq, deliverySucceeded);
  const advanced: DeliveryCursor = {
    ...advancedBase,
    updated_at: deliverySucceeded ? nowIso : (advancedBase.updated_at ?? nowIso),
  };
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

  // §4.2.6 item 2: When a session-scoped cursor ("<agent>:<sessionKey>") advances,
  // update the bare "<agent>" watermark cursor to the minimum last_delivered_seq
  // across all session cursors of <agent> on planId.
  const parsed = parseSubscriberPrincipal(subscriberId);
  if (deliverySucceeded && parsed.sessionKey) {
    const sessionCursors = next.cursors.filter((c) => {
      if (c.plan_id !== planId) return false;
      const p = parseSubscriberPrincipal(c.subscriber_id);
      return p.agentId === parsed.agentId && Boolean(p.sessionKey);
    });
    if (sessionCursors.length > 0) {
      const minSeq = Math.min(...sessionCursors.map((c) => c.last_delivered_seq));
      const bareCurrent = next.cursors.find(
        (c) => c.subscriber_id === parsed.agentId && c.plan_id === planId,
      ) ?? {
        subscriber_id: parsed.agentId,
        plan_id: planId,
        last_delivered_seq: 0,
        updated_at: nowIso,
      };
      const bareAdvanced: DeliveryCursor = {
        ...advanceCursor(bareCurrent, minSeq, true),
        updated_at: nowIso,
      };
      next.cursors = next.cursors.filter(
        (c) => !(c.subscriber_id === parsed.agentId && c.plan_id === planId),
      );
      next.cursors.push(bareAdvanced);
      next.pending = next.pending.filter((item) => {
        if (item.subscriber_id !== parsed.agentId) return true;
        if (item.plan_id !== planId) return true;
        return item.seq > bareAdvanced.last_delivered_seq;
      });
    }
  }
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
  const parsed = parseSubscriberPrincipal(subscriberId);
  const isSelfSessionAuthored = (item: RelayNotification): boolean =>
    Boolean(
      parsed.sessionKey &&
        (item.actor === subscriberId || item.actor === parsed.agentId) &&
        item.actor_session_key === parsed.sessionKey,
    );
  let notifications = store.pending.filter(
    (item) =>
      item.subscriber_id === subscriberId &&
      item.plan_id === planId &&
      item.seq > cursor.last_delivered_seq &&
      !isSelfSessionAuthored(item),
  );
  if (notifications.length === 0) {
    const hasOwnCursor = store.cursors.some(
      (c) => c.subscriber_id === subscriberId && c.plan_id === planId,
    );
    if (parsed.sessionKey && !hasOwnCursor) {
      notifications = store.pending
        .filter(
          (item) =>
            item.subscriber_id === parsed.agentId &&
            item.plan_id === planId &&
            item.seq > cursor.last_delivered_seq &&
            !isSelfSessionAuthored(item),
        )
        .map((item) => ({ ...item, subscriber_id: subscriberId }));
    }
  }
  return {
    notifications,
    text: notifications.map((item) => item.delta).join("\n"),
    hooksPollThreadEvents: HOOKS_POLL_MINNI_THREAD_EVENTS,
    spawned: false,
  };
}

const inProcessRelayLocks = new Map<string, Promise<void>>();

function isProcessLiveForRelayLock(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 1) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException)?.code === "EPERM";
  }
}

export async function withRelayStoreLock<T>(
  vaultPath: string,
  fn: () => Promise<T>,
  options?: { timeoutMs?: number },
): Promise<T> {
  const resolvedVault = path.resolve(vaultPath);
  const prior = inProcessRelayLocks.get(resolvedVault) ?? Promise.resolve();
  let releaseInProcess!: () => void;
  const currentGate = new Promise<void>((resolve) => {
    releaseInProcess = resolve;
  });
  inProcessRelayLocks.set(
    resolvedVault,
    prior.then(() => currentGate, () => currentGate),
  );
  await prior.catch(() => {});

  const dir = relayDir(vaultPath);
  const lockDir = path.join(dir, "cursors.lock");
  const ownerPath = path.join(lockDir, "owner.json");
  const timeoutMs =
    typeof options?.timeoutMs === "number" &&
    Number.isFinite(options.timeoutMs) &&
    options.timeoutMs > 0
      ? options.timeoutMs
      : RELAY_LOCK_TIMEOUT_MS;
  let acquired = false;
  try {
    await mkdir(dir, { recursive: true, mode: 0o700 });
    assertWriteTargetUnder(lockDir, vaultPath);
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        await mkdir(lockDir, { mode: 0o700 });
        acquired = true;
        await writeFile(
          ownerPath,
          JSON.stringify({ pid: process.pid, acquiredAt: new Date().toISOString() }),
          { encoding: "utf8", mode: 0o600 },
        ).catch(() => {});
        break;
      } catch (err) {
        if ((err as NodeJS.ErrnoException)?.code !== "EEXIST") {
          throw err;
        }
        try {
          const rawOwner = JSON.parse(await readFile(ownerPath, "utf8")) as {
            pid?: number;
            acquiredAt?: string;
          };
          const ownerDead =
            typeof rawOwner.pid === "number" && !isProcessLiveForRelayLock(rawOwner.pid);
          const ageMs = rawOwner.acquiredAt
            ? Date.now() - Date.parse(rawOwner.acquiredAt)
            : 0;
          if (ownerDead || (Number.isFinite(ageMs) && ageMs > RELAY_LOCK_STALE_MS)) {
            await rm(lockDir, { recursive: true, force: true }).catch(() => {});
            continue;
          }
        } catch {
          // Owner file still being written or unreadable; wait briefly.
        }
        const jitterMs = 10 + Math.floor(Math.random() * 15);
        await new Promise((r) => setTimeout(r, jitterMs));
      }
    }
    if (!acquired) {
      throw new Error(`Timed out acquiring thread relay store lock: ${lockDir}`);
    }
    return await fn();
  } finally {
    if (acquired) {
      await rm(lockDir, { recursive: true, force: true }).catch(() => {});
    }
    releaseInProcess();
  }
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
  const dir = relayDir(vaultPath);
  await mkdir(dir, { recursive: true, mode: 0o700 });
  const targetPath = relayStorePath(vaultPath);
  assertWriteTargetUnder(targetPath, vaultPath);
  const tmpPath = path.join(dir, `cursors.json.${process.pid}.${randomUUID().slice(0, 8)}.tmp`);
  assertWriteTargetUnder(tmpPath, vaultPath);
  const payload = `${JSON.stringify({ ...store, graph: false }, null, 2)}\n`;
  let handle: Awaited<ReturnType<typeof open>> | undefined;
  try {
    handle = await open(tmpPath, "w", 0o600);
    await handle.writeFile(payload, "utf8");
    await handle.sync();
    await handle.close();
    handle = undefined;
    await rename(tmpPath, targetPath);
  } catch (err) {
    if (handle) {
      await handle.close().catch(() => {});
    }
    await unlink(tmpPath).catch(() => {});
    throw err;
  }
}

export async function liveSessionSubscribersForPlan(
  vaultPath: string,
  planId: string,
  fallbackAgentId?: string,
): Promise<string[]> {
  const dir = path.join(vaultPath, ".runtime", "active-thread.d");
  let entries: string[];
  try {
    entries = await readdir(dir);
  } catch {
    return [];
  }
  const nowMs = Date.now();
  const defaultPrincipal = vaultPrincipalSubscriberId(vaultPath) ?? fallbackAgentId ?? "";
  const out = new Set<string>();
  for (const name of entries) {
    if (!name.endsWith(".json")) continue;
    const stem = name.slice(0, -".json".length);
    if (!SESSION_KEY_HEX_RE.test(stem)) continue;
    try {
      const raw = JSON.parse(await readFile(path.join(dir, name), "utf8")) as {
        plan_id?: string;
        agent_id?: string;
        host_pid?: number;
        updated_at?: string;
        set_at?: string;
      };
      if (raw?.plan_id !== planId) continue;
      if (typeof raw.host_pid === "number" && !isProcessLiveForRelayLock(raw.host_pid)) {
        continue;
      }
      const ts = Date.parse(raw.updated_at ?? raw.set_at ?? "");
      if (Number.isFinite(ts) && nowMs - ts > RELAY_CURSOR_PRUNE_AGE_MS) {
        continue;
      }
      const agentId =
        typeof raw.agent_id === "string" && raw.agent_id && raw.agent_id !== "unknown"
          ? raw.agent_id
          : defaultPrincipal;
      const subId = formatSessionSubscriberId(agentId, stem);
      if (subId) out.add(subId);
    } catch {
      // Ignore unreadable session pointer.
    }
  }
  return [...out];
}

async function materializeLiveSessionSubscribersInStore(
  vaultPath: string,
  store: RelayStore,
  planId: string,
  extraSubscriberIds: readonly string[] = [],
): Promise<RelayStore> {
  const parsedExtras = extraSubscriberIds
    .map((id) => parseSubscriberPrincipal(id))
    .filter((p) => Boolean(p.sessionKey));
  const fallbackAgent = parsedExtras[0]?.agentId;
  const liveOnPlan = await liveSessionSubscribersForPlan(vaultPath, planId, fallbackAgent);
  const allSessionSubs = new Set<string>([
    ...liveOnPlan,
    ...extraSubscriberIds.filter((id) => Boolean(parseSubscriberPrincipal(id).sessionKey)),
  ]);
  if (allSessionSubs.size === 0) return store;

  let mutated = false;
  const nextCursors = store.cursors.map((c) => ({ ...c }));
  const nextPending = store.pending.map((p) => ({ ...p }));
  for (const subId of allSessionSubs) {
    if (nextCursors.some((c) => c.subscriber_id === subId && c.plan_id === planId)) {
      continue;
    }
    const forked = forkSessionCursor(store, subId, planId);
    nextCursors.push(forked);
    const { agentId } = parseSubscriberPrincipal(subId);
    const barePending = store.pending.filter(
      (item) =>
        item.subscriber_id === agentId &&
        item.plan_id === planId &&
        item.seq > forked.last_delivered_seq,
    );
    for (const item of barePending) {
      if (
        !nextPending.some(
          (p) => p.subscriber_id === subId && p.plan_id === planId && p.seq === item.seq,
        )
      ) {
        nextPending.push({ ...item, subscriber_id: subId });
      }
    }
    mutated = true;
  }
  if (!mutated) return store;
  nextPending.sort(
    (a, b) =>
      a.seq - b.seq ||
      a.subscriber_id.localeCompare(b.subscriber_id) ||
      a.plan_id.localeCompare(b.plan_id),
  );
  return {
    version: RELAY_STORE_VERSION,
    graph: false,
    cursors: nextCursors,
    pending: nextPending,
  };
}

export async function ensureSessionRelayCursor(
  vaultPath: string,
  planId: string,
  subscriberId: string,
): Promise<void> {
  const parsed = parseSubscriberPrincipal(subscriberId);
  if (!parsed.sessionKey || !planId) return;
  await withRelayStoreLock(vaultPath, async () => {
    const store = await loadRelayStore(vaultPath);
    if (store.cursors.some((c) => c.subscriber_id === subscriberId && c.plan_id === planId)) {
      return;
    }
    // §4.2.3: If a sibling live session of the same agent is already tracking planId,
    // initialize from the sibling's last_delivered_seq; otherwise fork from bare agentId.
    const liveSubs = await liveSessionSubscribersForPlan(vaultPath, planId, parsed.agentId);
    const siblingCursors = store.cursors.filter(
      (c) =>
        c.plan_id === planId &&
        c.subscriber_id !== subscriberId &&
        liveSubs.includes(c.subscriber_id),
    );
    const initialSeq =
      siblingCursors.length > 0
        ? Math.max(...siblingCursors.map((c) => c.last_delivered_seq))
        : forkSessionCursor(store, subscriberId, planId).last_delivered_seq;
    store.cursors.push({
      subscriber_id: subscriberId,
      plan_id: planId,
      last_delivered_seq: initialSeq,
      updated_at: new Date().toISOString(),
    });
    await saveRelayStore(vaultPath, store);
  });
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
  return withRelayStoreLock(vaultPath, async () => {
    let store = await loadRelayStore(vaultPath);
    store = await materializeLiveSessionSubscribersInStore(vaultPath, store, planId, [
      subscriberId,
    ]);
    const next = confirmDelivery(
      store,
      subscriberId,
      planId,
      deliveredThroughSeq,
      deliverySucceeded,
    );
    await saveRelayStore(vaultPath, next);
    return cursorFor(next, subscriberId, planId);
  });
}

/**
 * Acknowledge relay notifications through `throughSeq` for the given subscribers
 * (e.g. when a reader consumes events via `minni_thread_events`).
 */
export async function acknowledgeRelayThroughSeq(
  vaultPath: string,
  planId: string,
  throughSeq: number,
  subscriberIds: readonly string[],
): Promise<RelayStore> {
  return withRelayStoreLock(vaultPath, async () => {
    let store = await loadRelayStore(vaultPath);
    if (throughSeq <= 0) return store;
    store = await materializeLiveSessionSubscribersInStore(
      vaultPath,
      store,
      planId,
      subscriberIds,
    );
    const seen = new Set<string>();
    const hasSessionSub = subscriberIds.some((id) =>
      Boolean(parseSubscriberPrincipal(id).sessionKey),
    );
    for (const rawId of subscriberIds) {
      const id = rawId.trim();
      if (!id || id === "unknown-agent" || seen.has(id)) continue;
      seen.add(id);
      const parsed = parseSubscriberPrincipal(id);
      if (!parsed.sessionKey && hasSessionSub) {
        const otherSessionCursors = store.cursors.filter((c) => {
          if (c.plan_id !== planId) return false;
          const p = parseSubscriberPrincipal(c.subscriber_id);
          return p.agentId === parsed.agentId && Boolean(p.sessionKey) && !subscriberIds.includes(c.subscriber_id);
        });
        if (otherSessionCursors.length > 0) {
          continue;
        }
      }
      store = confirmDelivery(store, id, planId, throughSeq, true);
    }
    await saveRelayStore(vaultPath, store);
    return store;
  });
}

/**
 * Advance a subscriber's cursor past any contiguous leading self-authored
 * events (`item.actor === subscriberId`) so an orchestrator's own mutations
 * do not queue self-notifications while still preserving events authored by
 * any other worker/subscriber (and, per Q2 = Option A, preserving events
 * authored by Session A so Session B of the same agent is notified).
 */
export async function acknowledgeSelfAuthoredPrefixInVault(
  vaultPath: string,
  planId: string,
  subscriberIds: readonly string[],
  options?: { actorSessionKey?: string },
): Promise<RelayStore> {
  return withRelayStoreLock(vaultPath, async () => {
    let store = await loadRelayStore(vaultPath);
    const actorSessionKey =
      options?.actorSessionKey && SESSION_KEY_HEX_RE.test(options.actorSessionKey)
        ? options.actorSessionKey
        : undefined;
    store = await materializeLiveSessionSubscribersInStore(
      vaultPath,
      store,
      planId,
      subscriberIds,
    );
    let changed = false;
    const seen = new Set<string>();
    for (const rawId of subscriberIds) {
      const subscriberId = rawId.trim();
      if (!subscriberId || subscriberId === "unknown-agent" || seen.has(subscriberId)) continue;
      seen.add(subscriberId);
      const parsed = parseSubscriberPrincipal(subscriberId);
      if (actorSessionKey) {
        if (parsed.sessionKey && parsed.sessionKey !== actorSessionKey) {
          continue;
        }
        if (!parsed.sessionKey) {
          const otherSessionCursors = store.cursors.filter((c) => {
            if (c.plan_id !== planId) return false;
            const p = parseSubscriberPrincipal(c.subscriber_id);
            return (
              p.agentId === parsed.agentId &&
              Boolean(p.sessionKey) &&
              p.sessionKey !== actorSessionKey
            );
          });
          if (otherSessionCursors.length > 0) {
            continue;
          }
        }
      }
      const items = store.pending
        .filter((item) => item.subscriber_id === subscriberId && item.plan_id === planId)
        .sort((a, b) => a.seq - b.seq);
      let ackSeq = 0;
      for (const item of items) {
        const actorMatches = item.actor === subscriberId || item.actor === parsed.agentId;
        const effectiveSessionKey = actorSessionKey ?? parsed.sessionKey;
        const sessionMatches = effectiveSessionKey
          ? item.actor_session_key === effectiveSessionKey
          : !item.actor_session_key;
        if (actorMatches && sessionMatches) {
          ackSeq = item.seq;
        } else {
          break;
        }
      }
      if (ackSeq > 0) {
        store = confirmDelivery(store, subscriberId, planId, ackSeq, true);
        changed = true;
      }
    }
    if (changed) {
      await saveRelayStore(vaultPath, store);
    }
    return store;
  });
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
      if (
        typeof row.actor_session_key === "string" &&
        SESSION_KEY_HEX_RE.test(row.actor_session_key)
      ) {
        event.actor_session_key = row.actor_session_key;
      }
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
  return withRelayStoreLock(vaultPath, async () => {
    const store = await loadRelayStore(vaultPath);
    const firstBareAgent = subscriberIds
      .map((id) => parseSubscriberPrincipal(id).agentId)
      .find(Boolean);
    const liveSessionSubs = await liveSessionSubscribersForPlan(
      vaultPath,
      planId,
      firstBareAgent,
    );
    const mergedSubscribers = [...new Set([...subscriberIds, ...liveSessionSubs])];
    const next = ingestJournalEvents(store, planId, events, mergedSubscribers);
    await saveRelayStore(vaultPath, next);
    return next;
  });
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

const RELAY_TERMINAL_PLAN_STATUSES = new Set([
  "accepted",
  "complete",
  "rejected",
  "superseded",
  "abandoned",
]);

async function isRelayPlanMissingOrTerminal(
  vaultPath: string,
  planId: string,
  cache: Map<string, boolean>,
): Promise<boolean> {
  const cached = cache.get(planId);
  if (cached !== undefined) return cached;
  if (!planId || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(planId)) {
    cache.set(planId, true);
    return true;
  }
  const notePath = path.join(vaultPath, "wiki", "artifacts", `${planId}.md`);
  try {
    const raw = await readFile(notePath, "utf8");
    const statusMatch = /^status:\s*['"]?([a-z0-9_-]+)['"]?\s*$/im.exec(raw);
    const status = statusMatch?.[1]?.toLowerCase() ?? "";
    if (RELAY_TERMINAL_PLAN_STATUSES.has(status)) {
      cache.set(planId, true);
      return true;
    }
    cache.set(planId, false);
    return false;
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") {
      cache.set(planId, true);
      return true;
    }
    cache.set(planId, false);
    return false;
  }
}

/**
 * §5.1.2 (Slice S4): Prune expired session-scoped relay cursors ("<agent>:<sessionKey>")
 * whose session has been dead/inactive > 24h or whose plan is missing/terminal with
 * empty pending delivery, and cap inactive composite cursors at MAX_RELAY_CURSORS
 * without ever evicting bare <agent> cursors, liveSessionKeys, or pending queues.
 * Never deletes plan notes, journals, or histories.
 */
export async function sweepDeadRelayCursors(
  vaultPath: string,
  nowMs = Date.now(),
  prunedSessionKeys?: ReadonlySet<string>,
): Promise<{ prunedCursors: number }> {
  return withRelayStoreLock(vaultPath, async () => {
    const store = await loadRelayStore(vaultPath);
    if (store.cursors.length === 0) return { prunedCursors: 0 };
    const activeThreadDir = path.join(vaultPath, ".runtime", "active-thread.d");
    const liveSessionKeys = new Set<string>();
    try {
      const files = await readdir(activeThreadDir);
      for (const f of files) {
        if (!f.endsWith(".json")) continue;
        const stem = f.slice(0, -".json".length);
        if (!SESSION_KEY_HEX_RE.test(stem)) continue;
        try {
          const raw = JSON.parse(
            await readFile(path.join(activeThreadDir, f), "utf8"),
          ) as { host_pid?: number; updated_at?: string; set_at?: string };
          const pidLive =
            typeof raw.host_pid !== "number" || isProcessLiveForRelayLock(raw.host_pid);
          const ts = Date.parse(raw.updated_at ?? raw.set_at ?? "");
          const recent = !Number.isFinite(ts) || nowMs - ts <= RELAY_CURSOR_PRUNE_AGE_MS;
          if (pidLive && recent) {
            liveSessionKeys.add(stem);
          }
        } catch {
          // Ignore unreadable session file.
        }
      }
    } catch {
      // No active-thread.d dir.
    }

    try {
      const runtimeDir = path.join(vaultPath, ".runtime");
      const runtimeFiles = await readdir(runtimeDir);
      for (const f of runtimeFiles) {
        if (!f.startsWith("active-session-") || !f.endsWith(".json")) continue;
        try {
          const raw = JSON.parse(
            await readFile(path.join(runtimeDir, f), "utf8"),
          ) as {
            host_pid?: number;
            process_start_marker?: string;
            session_key?: string;
            updated_at?: string;
          };
          if (
            typeof raw.session_key === "string" &&
            SESSION_KEY_HEX_RE.test(raw.session_key) &&
            typeof raw.host_pid === "number" &&
            isLiveMatchingProcess(raw.host_pid, raw.process_start_marker)
          ) {
            const ts = Date.parse(raw.updated_at ?? "");
            if (!Number.isFinite(ts) || nowMs - ts <= RELAY_CURSOR_PRUNE_AGE_MS) {
              liveSessionKeys.add(raw.session_key);
            }
          }
        } catch {
          // Ignore unreadable active-session hint.
        }
      }
    } catch {
      // No .runtime dir.
    }

    const pendingCounts = new Map<string, number>();
    for (const item of store.pending) {
      const key = `${item.subscriber_id}\0${item.plan_id}`;
      pendingCounts.set(key, (pendingCounts.get(key) ?? 0) + 1);
    }

    const planTerminalCache = new Map<string, boolean>();
    const prunedSubPlans = new Set<string>();
    const keptCursors: DeliveryCursor[] = [];
    for (const cursor of store.cursors) {
      const parsed = parseSubscriberPrincipal(cursor.subscriber_id);
      const subPlanKey = `${cursor.subscriber_id}\0${cursor.plan_id}`;
      const hasPending = (pendingCounts.get(subPlanKey) ?? 0) > 0;
      if (parsed.sessionKey && !liveSessionKeys.has(parsed.sessionKey)) {
        const updatedTs = cursor.updated_at ? Date.parse(cursor.updated_at) : Number.NaN;
        const expiredByAge =
          Number.isFinite(updatedTs) && nowMs - updatedTs > DEAD_CURSOR_MAX_AGE_MS;
        const expiredBySessionSweep = prunedSessionKeys?.has(parsed.sessionKey) ?? false;
        let expiredWhenQueueEmpty = false;
        if (!hasPending && !expiredByAge && !expiredBySessionSweep) {
          if (!Number.isFinite(updatedTs)) {
            expiredWhenQueueEmpty = true;
          } else {
            expiredWhenQueueEmpty = await isRelayPlanMissingOrTerminal(
              vaultPath,
              cursor.plan_id,
              planTerminalCache,
            );
          }
        }
        if (expiredByAge || expiredBySessionSweep || expiredWhenQueueEmpty) {
          prunedSubPlans.add(subPlanKey);
          continue;
        }
      }
      keptCursors.push(cursor);
    }

    if (keptCursors.length > MAX_RELAY_CURSORS) {
      const protectedCursors: DeliveryCursor[] = [];
      const evictableCursors: DeliveryCursor[] = [];
      for (const cursor of keptCursors) {
        const parsed = parseSubscriberPrincipal(cursor.subscriber_id);
        const subPlanKey = `${cursor.subscriber_id}\0${cursor.plan_id}`;
        const hasPending = (pendingCounts.get(subPlanKey) ?? 0) > 0;
        if (!parsed.sessionKey || liveSessionKeys.has(parsed.sessionKey) || hasPending) {
          protectedCursors.push(cursor);
        } else {
          evictableCursors.push(cursor);
        }
      }
      if (evictableCursors.length > 0) {
        evictableCursors.sort((a, b) => {
          const ta = a.updated_at ? Date.parse(a.updated_at) : 0;
          const tb = b.updated_at ? Date.parse(b.updated_at) : 0;
          return (Number.isFinite(tb) ? tb : 0) - (Number.isFinite(ta) ? ta : 0);
        });
        const maxEvictableKeep = Math.max(0, MAX_RELAY_CURSORS - protectedCursors.length);
        const dropped = evictableCursors.splice(maxEvictableKeep);
        for (const d of dropped) {
          prunedSubPlans.add(`${d.subscriber_id}\0${d.plan_id}`);
        }
        keptCursors.length = 0;
        keptCursors.push(...protectedCursors, ...evictableCursors);
      }
    }

    if (prunedSubPlans.size === 0) return { prunedCursors: 0 };
    const next: RelayStore = {
      version: RELAY_STORE_VERSION,
      graph: false,
      cursors: keptCursors,
      pending: store.pending.filter(
        (item) => !prunedSubPlans.has(`${item.subscriber_id}\0${item.plan_id}`),
      ),
    };
    await saveRelayStore(vaultPath, next);
    return { prunedCursors: prunedSubPlans.size };
  });
}
