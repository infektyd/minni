# DESIGN: Multi-Session Principal (`agent_id` + `session_id`)

- **Status:** Implemented (Slices S1–S4 + Amendments R1, R2)
- **Baseline:** `main` @ `e8b0ed8e`
- **Defects addressed:**
  - **H4 (One active plan per vault):** `minni_thread_create` unconditionally overwrites the vault's single active-plan pointer (`<vault>/wiki/artifacts/_active_plan.json`) and has no `activate` parameter. A second live session creating a plan silently displaces the first session's active plan; id-less `minni_thread_*` calls and hook injections in the first session then target the wrong plan.
  - **H6 (Multi-session principal):** Concurrent live sessions of the same agent principal (for example, a `claude-code` coordinator session and a `claude-code` worker session sharing one vault) collide on ambient runtime state:
    - **(a)** the vault's active plan (`H4`),
    - **(b)** thread notification relay delivery cursors keyed solely by `subscriber_id = <agent_id>` (`<vault>/.runtime/thread-relay/cursors.json`, plus dormant `src/minni/migrations/020_thread_delivery_cursors.sql`), where one session's hook or poll ack prunes notifications before the second session sees them,
    - **(c)** `<vault>/.runtime/recall-state.json` (the unkeyed legacy mirror) and cross-session `disarmRecallStateOnConsult` in the MCP server (`plugins/minni/src/server.ts:722,832`), which disarms **every** session's `.runtime/recall-state.d/<sessionKey>.json` when `minni_recall` or `minni_drill` is called without a `sessionId`,
    - **(d)** `<vault>/.runtime/active-session-<agent>-<hostPid>.json` workspace hints and `<vault>/.runtime/lifecycle-state.json`.
  - **H5 (Context only — closed):** A session whose `cwd` is outside a git repository receives no workspace hint by design and files learnings under `workspace_id = "workspace-unknown"`.

---

## 0. Executive Summary & Architectural Principle

Minni's governance model is anchored to the **Agent Principal** (`EffectivePrincipal.agent_id` in `src/minni/principal.py:37-49`, stamped in `<minni-home>/principals/<agent>.json`). Durable knowledge—accepted learnings, candidate packets, vault markdown notes, memory graph links, and Thread/Plan artifacts (`<vault>/wiki/artifacts/plan-<16hex>.md`, `*.log.md`, `*.history.jsonl`)—belongs to the principal (or principal + workspace) and **must remain shared** across all concurrent sessions of that principal.

However, **ambient session state**—which conversation turn is armed for recall, which thread/plan a particular terminal window is currently driving, which thread relay events have been shown to *this* context window, and which host process owns a workspace hint—belongs to a **live session** (`session_id`, hashed to a 16-hex `session_key` via `sessionStateKey(sessionId)` in `plugins/minni/src/recall-state.ts:535-545`).

Today, Minni is halfway through this split:
1. Hook handlers (`plugins/minni/src/hook-handlers.ts`) already extract `rawSessionId` from host hook payloads and key `.runtime/recall-state.d/<sessionKey>.*`, `.runtime/recall-session.d/<sessionKey>/`, `.runtime/use-ledger/<sessionKey>/`, and `.runtime/use-detector/sessions/<sessionKey>/` per session.
2. Workspace hints (`plugins/minni/src/config.ts:117-373`) are already keyed per host process (`.runtime/active-session-<agent>-<hostPid>.json`), matching `process.ppid` in the MCP server against the `host_pid` recorded by the hook.
3. **The missing bridge:** The MCP server (`plugins/minni/src/server.ts`) never learns the host's `session_id` (it coins an isolated `MCP_PROCESS_SESSION_ID = "mcp-<ts>-<rand>"` at `server.ts:356`), the active-session hint file does not store `session_id`, the active thread pointer (`<vault>/wiki/artifacts/_active_plan.json`) is a single vault-global file, and the thread notification relay (`<vault>/.runtime/thread-relay/cursors.json`) keys cursors by bare `agent_id`.

### Core Design Rule

$$\text{Identity} = \underbrace{\text{agent\_id}}_{\text{Principal (AuthZ, Durable Memory, Vault Ownership)}} + \underbrace{\text{session\_key}}_{\text{Ambient Runtime Scope (Active Thread, Relay Cursor, Recall Guard, Handles)}}$$

1. **`agent_id` remains the sole security and durability principal.** `session_id` never grants capabilities, never widens `allowed_vault_roots`, and never partitions durable learnings or vault notes.
2. **`session_key` (`sha256(session_id)[:16]`) scopes all ambient runtime state.** Every hook invocation and every MCP server tool call resolves an effective `(agent_id, session_id, session_key)` tuple—falling back fail-closed to `"default"` (today's exact single-session behavior) only when neither hook hint, environment variable, nor host PID is available.

---

## 1. Inventory of Per-Agent Ambient & Durable State

The table below inventories every piece of per-agent state across the vault filesystem (`<vault>/`), the vault runtime directory (`<vault>/.runtime/`), the SQLite databases (`<minni-home>/minni.db` and `<vault>/.index/vault.db`), and in-memory server/daemon state.

> ** separating What Ships Today from Proposed Design:** Columns **Where It Lives**, **Key Today**, **Writers / Readers Today**, and **Concurrent-Session Hazard Today** describe the deployed code at `e8b0ed8e`. Column **Verdict** states whether the state must remain shared (`SHARED-OK`) or become per-session (`PER-SESSION`).

### 1.1 Vault `.runtime/` & Artifact Pointers (Ambient Runtime State)

| # | State Surface | Where It Lives | Key Today | Writers Today (`file:line`) | Readers Today (`file:line`) | Concurrent-Session Hazard Today (2 Live Sessions of Same Agent) | Verdict |
|---|---|---|---|---|---|---|---|
| **R1** | **Active Plan Pointer (`H4`)** | `<vault>/wiki/artifacts/_active_plan.json` | Vault-global singleton (`plan_id`, `path`, `activated_at`) | `createPlan` (`plugins/minni/src/plan.ts:1044`), `setActivePlan` (`plan.ts:2472-2490`), `activatePlanChecked` (`plan.ts:2501-2521`), `clearActivePlan` (`plan.ts:2568-2577`), `minni_thread_update` auto-clear (`server.ts:1792`), `minni_thread_deactivate` (`server.ts:2408`), `resolveActivePlanView` auto-heal (`plan.ts:2684,2739`) | `getActivePlan` (`plan.ts:2523-2566`), `resolvePlanIdOrActive` (`plan.ts:2622-2636`), `resolvePlanTarget` (`server.ts:1590-1616`), `resolveActivePlanView` (`plan.ts:2651-2801`), `SessionStart` (`hook-handlers.ts:1981`), `UserPromptSubmit` (`hook-handlers.ts:2766`), `Stop` (`hook-handlers.ts:3362`) | **Misleads & corrupts workflow (`H4`):** Session B calling `minni_thread_create` overwrites `_active_plan.json` and displaces Session A's plan. Subsequent id-less `minni_thread_*` calls and `UserPromptSubmit` injections in Session A target Session B's plan. Session B calling `minni_thread_deactivate` clears Session A's pointer. | **PER-SESSION** (with vault fallback pointer for legacy/unkeyed callers) |
| **R2** | **Thread Relay Delivery Cursors (`H6b`)** | `<vault>/.runtime/thread-relay/cursors.json` | `(subscriber_id, plan_id)` where `subscriber_id = <agent_id>` | `ingestJournalIntoVault` (`plugins/minni/src/thread-notification-relay.ts:624-634`, `src/minni/thread_relay.py:456-475`), `confirmDeliveryInVault` (`thread-notification-relay.ts:464-475`), `acknowledgeRelayThroughSeq` (`thread-notification-relay.ts:481-498`, `server.ts:2766`), `acknowledgeSelfAuthoredPrefixInVault` (`thread-notification-relay.ts:506-538`, `server.ts:1781,1924`), `saveRelayStore` (`thread-notification-relay.ts:446-453`, non-atomic `writeFile`) | `pendingAttentionForHook` (`thread-notification-relay.ts:604-619`), `attachPendingAttention` (`hook-handlers.ts:771-805`), `handleStop` (`hook-handlers.ts:3363-3381`), `readPendingAttentionInVault` (`thread-notification-relay.ts:588-595`) | **Hides data & races (`H6b`):** Session A's hook delivery or `minni_thread_events` poll advances `last_delivered_seq` for `subscriber_id = <agent_id>` and filters delivered items out of `store.pending` (`thread-notification-relay.ts:372-376`). Session B never sees those events. Also, `saveRelayStore` does an unguarded `writeFile` without a lock or atomic rename. | **PER-SESSION** (`subscriber_id = "<agent_id>:<session_key>"`, plus atomic write) |
| **R3** | **Dormant SQLite Relay Cursor Table (`H6b`)** | `<minni-home>/minni.db` table `thread_delivery_cursors` | `PRIMARY KEY (subscriber_id, plan_id)` | None in production (`SQLITE_020_LIVE = False` at `src/minni/thread_relay.py:28-31`, `thread-notification-relay.ts:33-34`, `migrations/020_thread_delivery_cursors.sql:1-15`) | Tested only in unit tests against in-memory/temp SQLite | **Dormant schema:** Keyed by `(subscriber_id, plan_id)` text column; supports composite `"<agent_id>:<session_key>"` strings without DDL change, or table retirement. | **PER-SESSION** (via composite `subscriber_id` string) |
| **R4** | **Active Session Workspace Hint (`H6d`)** | `<vault>/.runtime/active-session-<agent>-<hostPid>.json` | `(agent_id, host_pid)` (`ACTIVE_SESSION_TTL_MS = 30m`, pruned at `24h`) | `recordActiveSessionWorkspace` (`plugins/minni/src/config.ts:117-204`), called from `workspaceFor()` on every hook event (`hook-handlers.ts:1585`) | `resolveSessionWorkspaceWithDiag` (`config.ts:210-373`), called by MCP server tool handlers (`server.ts:681,967`) | **Partly solved, two remaining gaps:** (1) Stores `{workspace_id, agent_id, host_pid, updated_at}` but **omits `session_id` / `session_key`**, so the MCP server (`server.ts`) cannot discover the hook's `session_id`. (2) When `process.ppid` in the MCP server does not match (e.g. wrapper process), `resolveSessionWorkspaceWithDiag` returns `ambiguous_multi_session` if 2 sessions run in different workspaces (`config.ts:346-367`). | **PER-SESSION** (enrich with `session_id`, `session_key`, `mcp_ppid` chain) |
| **R5** | **Per-Session Recall State (Guard V2)** | `<vault>/.runtime/recall-state.d/<sessionKey>.json` | `sessionKey = sha256(session_id)[:16]` (or `tx-<sha256(transcript)[:16]>`) | `writeRecallState` (`plugins/minni/src/recall-state.ts:741-799`), `clearRecallState` (`recall-state.ts:843-907`), `markRecallStateConsumed` (`recall-state.ts:914-961`), `disarmRecallStateOnConsult` (`recall-state.ts:974-1020`) | `readRecallState` (`recall-state.ts:806-836`), `evaluatePreToolUse` (`recall-guard.ts:299-329`), `handlePreToolUse` (`hook-handlers.ts:3000-3120`) | **Cross-session disarm bug (`H6c`):** File storage is per-session, **but** `server.ts:722` (`minni_recall`) and `server.ts:832` (`minni_drill`) call `disarmRecallStateOnConsult(DEFAULT_VAULT_PATH)` with **no `sessionId`**, which loops over **every** `<16hex>.json` file in `.runtime/recall-state.d/` (`recall-state.ts:999-1014`) and marks all concurrent sessions' recall guards `consumed = true`! | **PER-SESSION** (fix MCP server `sessionId` pass-through; stop mass-disarming other sessions) |
| **R6** | **Legacy Unkeyed Recall State Mirror (`H6c`)** | `<vault>/.runtime/recall-state.json` | Vault-global singleton | `writeRecallState` (`recall-state.ts:746,796` — written unconditionally on every session's write), `clearRecallState` (`recall-state.ts:868`), `markRecallStateConsumed` (`recall-state.ts:925`), `disarmRecallStateOnConsult` (`recall-state.ts:986`) | `readRecallState` (`recall-state.ts:817-826` — fallback when `sessionKey` is absent or `.runtime/recall-state.d/<sessionKey>.json` returns `ENOENT`) | **Misleads & corrupts (`H6c`):** Session B's `writeRecallState` overwrites `.runtime/recall-state.json`. If Session A has not yet written its own `.runtime/recall-state.d/<sessionKey>.json` (e.g. before its first strong prompt), `readRecallState` in Session A falls back to `.runtime/recall-state.json` and inherits Session B's armed recall guard (`task_signature`, `consult_required`)! | **PER-SESSION** (only write/read `.runtime/recall-state.json` when `sessionKey === null` or `guardV2 === false`) |
| **R7** | **Recall Guard Arming Claim Files** | `<vault>/.runtime/recall-state.d/<sessionKey>.<armingKey>.claim` | `(sessionKey, armingKey)` (`O_CREAT\|O_EXCL`, pruned at `24h`) | `claimArming` (`recall-state.ts:652-684`), `pruneOldClaimFiles` (`recall-state.ts:579-608`) | `claimArming` (`recall-state.ts:652-684`) | **None** when `sessionId` is present (falls back to `"default"` prefix only when `sessionId` is missing). | **PER-SESSION** (already keyed; preserve) |
| **R8** | **Per-Session Turn Counters & Surfaced Suppression** | `<vault>/.runtime/recall-state.d/<sessionKey>.counters.json` (and `.runtime/recall-session.d/<sessionKey>/counters.json`) | `sessionKey` (fallback `default.counters.json`) | `incrementTurn` (`recall-state.ts:1784-1820`), `recordSurfaced` (`recall-state.ts:1828-1848`), `resetTurnCounters` (`recall-state.ts:1854-1873`), `SessionStart` (`hook-handlers.ts:410-442`) | `readCounters` (`recall-state.ts:1770-1778`), `filterRecentlySurfaced` (`recall-state.ts:1880-1910`) | **None** when `sessionId` is present; collides on `default.counters.json` only when host provides no `session_id`. | **PER-SESSION** (already keyed; preserve) |
| **R9** | **Session Recall Handles (`m:<8hex>`, `d:<src>:<id>`)** | `<vault>/.runtime/recall-state.d/<sessionKey>.handles.json`, `.runtime/recall-session.d/<sessionKey>/handles.json`, and legacy `<vault>/.handles.json` | `sessionKey` (plus vault-global `.handles.json`) | `recordSessionHandles` (`recall-state.ts:1335-1391`), `writeLegacyHandles` (`recall-state.ts:1398-1430`) | `readSessionHandle` (`recall-state.ts:1439-1549`), called by `minni_drill` (`server.ts:831-843`) | **Cross-process mismatch:** Hook writes handles under the host's `sessionKey`, but `minni_drill` in `server.ts:841` passes `MCP_PROCESS_SESSION_ID` (`mcp-<ts>-<rand>`), which misses `candidateKeys` and forces `readSessionHandle` (`recall-state.ts:1519-1543`) to scan all `*.handles.json` newest-first and `<vault>/.handles.json`. | **PER-SESSION** (resolve real `session_id` in `server.ts`; deprecate `<vault>/.handles.json` cross-session overwrite) |
| **R10** | **Session Recall / Use-Capture Directory** | `<vault>/.runtime/recall-session.d/<sessionKey>/{counters.json,handles.json,hit-keys.json,main.reset,subagent-pending.json}` + `.sweep.json` | `sessionKey` (`sweepSessionDirs` TTL sweep) | `SessionStart` / `UserPromptSubmit` / `PostToolUse` / `SubagentStart` / `SubagentStop` (`hook-handlers.ts:410-545`, `use-capture.ts`) | `use-capture.ts`, `hook-handlers.ts` | **None** when `sessionId` is present. | **PER-SESSION** (already keyed; preserve) |
| **R11** | **Failure Lessons Fired State** | `<vault>/.runtime/recall-state.d/<sessionKey>.failure-lessons.json` | `sessionKey` (fallback `unlabeled.failure-lessons.json`) | `recordFiredFailureLessons` (`plugins/minni/src/failure-lessons.ts:392-424,583-593`) | `readFiredFailureLessons` (`failure-lessons.ts:392-424`) | **None** when `sessionId` is present. | **PER-SESSION** (already keyed; preserve) |
| **R12** | **Antigravity Turn Dedup State** | `<vault>/.runtime/recall-state.d/<sessionKey>.agy-turn.json` | `sessionKey` | `gemini-hook.ts` (`plugins/minni/src/gemini-hook.ts:140-170`) | `gemini-hook.ts:140-170` | **None** (`gemini-hook.ts` resolves `invocation_id` / `session_id` or `ppid-<ppid>`). | **PER-SESSION** (already keyed; preserve) |
| **R13** | **Lifecycle Emphasis State** | `<vault>/.runtime/lifecycle-state.json` | Vault-global singleton (`{session_id, emphasized: string[]}`) | `writeLifecycleState` (`recall-state.ts:1927-1942`), overwritten at `SessionStart` (`hook-handlers.ts:1725-1730`) and updated at `UserPromptSubmit` (`hook-handlers.ts:2360-2363`) | `readLifecycleState` (`recall-state.ts:1944-1957`), called at `UserPromptSubmit` (`hook-handlers.ts:2354`) | **Misleads (`PER-SESSION` collision):** Session B starting resets `lifecycle-state.json` to `emphasized: []`, erasing Session A's record of which lifecycle surfaces were already emphasized once-per-session; conversely Session B's emphasis suppresses first-time emphasis in Session A. | **PER-SESSION** (move to `.runtime/recall-state.d/<sessionKey>.lifecycle.json`) |
| **R14** | **Use-Ledger Daily JSONL** | `<vault>/.runtime/use-ledger/<sessionKey>/<yyyy-mm-dd>.jsonl` | `(sessionKey, date)` (pruned at `30d` by `sweepUseLedger`, `use-ledger.ts:740-810`) | `appendUseLedgerRecord` (`plugins/minni/src/use-ledger.ts:321-360,850`) | `use-df-recount.ts` (`plugins/minni/src/use-df-recount.ts:23-76`), `compact-harvest.ts` | **None** (already partitioned by `<sessionKey>/` directory). | **PER-SESSION** (already keyed; preserve) |
| **R15** | **Use-Detector Session State & Transcript Cursors** | `<vault>/.runtime/use-detector/sessions/<sessionKey>/` and `<vault>/.runtime/use-detector/cursors/<txHandleKey>.json` | `sessionKey` and `txHandleKey = sha256(transcriptPath)[:16]` | `use-detector.ts` (`plugins/minni/src/use-detector.ts:43,91,433-439,2956-2965`) | `use-detector.ts` | **None** (each session has its own `sessionKey` and its own host transcript file). | **PER-SESSION** (already keyed; preserve) |
| **R16** | **Use-DF Recount Watermark & Corpus DF** | `<vault>/.runtime/use-df-state.json` and `<vault>/.runtime/corpus-df.json` | Vault-global aggregate statistics | `use-df-recount.ts` (`plugins/minni/src/use-df-recount.ts:23-76,327-339`) | `recall-state.ts`, `use-detector.ts` | **None:** Aggregates document-frequency across all sessions of the vault. | **SHARED-OK** |
| **R17** | **Prompt Template Registry** | `<vault>/.runtime/prompt-templates.json` | `template.hash` (vault-global learned boilerplate templates, `sessionKeyPrefix` recorded per occurrence) | `recordOccurrence` (`plugins/minni/src/prompt-class.ts:485-510,582-596,731-741`, `hook-handlers.ts:2442`) | `readPromptTemplates` (`prompt-class.ts`, `hook-handlers.ts:2426`) | **None:** Cross-session template detection intentionally pools occurrences across sessions (`sessionKeyPrefix` distinguishes distinct sessions). | **SHARED-OK** |
| **R18** | **Compact Harvest Cursor & Jev Harvest Lock** | `<vault>/.runtime/compact-harvest-cursor.json`, `<vault>/.runtime/jev-harvest.lock` | Keyed by `session_id` map inside `cursors` (`compact-harvest.ts:74-120`), vault-level mutex lock (`jev-harvest.ts:25-36,966-978`) | `compact-harvest.ts`, `jev-harvest.ts` | `compact-harvest.ts`, `jev-harvest.ts` | **None:** `compact-harvest-cursor.json` already keys its internal map by `session_id` (`cursors[sessionId]`). | **SHARED-OK** (internal map already keyed by `session_id`) |
| **R19** | **Thread Exclusive Lock & Worker Write Queue** | `<vault>/.runtime/thread-locks/<lockKey>.lock/` and `<vault>/.runtime/thread-locks/<lockKey>.q/` | `lockKey = sha256(plan_id)[:32]` (`thread-lock.ts:66-68`, `thread-write-queue.ts:59-61`) | `withThreadLock` (`plugins/minni/src/thread-lock.ts:250-420`), `enqueueWorkerWrite` (`thread-write-queue.ts:197-310`), `worker_write_drain.py` (`src/minni/worker_write_drain.py:115-267`) | `withThreadLock`, `drainWorkerWriteQueue` | **None:** Synchronizes concurrent multi-process mutations to the *same* `plan_id`. Must stay keyed by `plan_id`. | **SHARED-OK** (per-`plan_id` concurrency control) |
| **R20** | **Thread Slice Claim Secrets & Receipts** | `<vault>/.runtime/thread-claims/<planKey>/<sliceKey>.json` and `receipts/` | `(plan_id, slice_id)` (`plugins/minni/src/thread-claims.ts:468-478,606-642`) | `claimSlice` (`thread-claims.ts`), `workerUpdateSlice` (`thread-worker.ts`) | `thread-claims.ts`, `thread-worker.ts` | **None:** Claim tokens are capability secrets bound to `(plan_id, slice_id, worker_agent_id)`. | **SHARED-OK** (per-plan/slice capability state) |

---

### 1.2 Vault Durable & Inbox Filesystem State

| # | State Surface | Where It Lives | Key Today | Writers Today (`file:line`) | Readers Today (`file:line`) | Concurrent-Session Hazard Today | Verdict |
|---|---|---|---|---|---|---|---|
| **V1** | **Thread / Plan Artifacts, History & Event Journals** | `<vault>/wiki/artifacts/plan-<16hex>.md`, `<plan_id>.history.jsonl`, `<plan_id>.log.md` | `plan_id` | `createPlan`, `persistPlan`, `appendHistoryEntry` (`plugins/minni/src/plan.ts:988-1075`), `appendOrderedEventBatch` (`thread-events.ts:942-980`) | `rehydratePlan`, `readOrderedEvents`, `minni_thread_*` tools (`server.ts:1536-2780`) | **None (when `plan_id` is explicit):** Protected by `withThreadLock(vaultPath, plan_id)`. Threads as data are shared across sessions so a coordinator and worker can collaborate on one `plan_id`. | **SHARED-OK** (threads as data stay shared) |
| **V2** | **Layer 1 Identity Shelf & Durable Wiki Notes** | `<vault>/layer1/core.md`, `<vault>/layer1/*.md`, `<vault>/wiki/**`, `<vault>/raw/**` | Relative vault path | `minni_vault_write` (`server.ts:890-950`), daemon consolidation / promotion (`src/minni/minnid_runtime/governance.py`) | `readLayer1Shelf` (`hook-handlers.ts:1881`), `minni_recall`, `minni_drill` | **None:** Durable principal memory. | **SHARED-OK** |
| **V3** | **PreCompact Correction Reassert Stash** | `<vault>/inbox/precompact-reassert-*.json` and `<vault>/inbox/.undeliverable/` | Timestamped inbox entry per compaction (`plugins/minni/src/hook-handlers.ts:1873-1965`) | `handlePreCompact` (`hook-handlers.ts:3140-3220`) | `handleSessionStart` (`hook-handlers.ts:1873-1965`), `handleStop` (`hook-handlers.ts:3310-3355`) | **Minor cross-session steal:** If Session A compacts (`PreCompact`) and Session B boots (`SessionStart`) milliseconds later before Session A re-boots, Session B can consume and settle Session A's `precompact-reassert-*.json`. Scoping the filename or metadata with optional `session_key` (preferring matching `session_key` first, falling back to unclaimed/stale entries >10m) prevents Session B from stealing Session A's post-compaction reassert. | **PER-SESSION** (session-preferring affinity with orphan fallback) |
| **V4** | **Cross-Agent Pings & Handoff Inbox** | `<vault>/inbox/agent-pings/<requestId>.json`, `<vault>/outbox/agent-pings/<requestId>.json`, `<vault>/inbox/*.md` | `requestId` / inbox entry filename (`plugins/minni/src/agent_ping.ts:152-163`) | `minni_ping_agent_request`, `minni_ping_agent_decide` (`agent_ping.ts:293-368,562-615`), `minni_negotiate_handoff` | `readInboxStatus` (`hook-handlers.ts:1791`), `minni_ping_agent_inbox` | **None:** Addressed to the agent principal (`toAgent`). | **SHARED-OK** |
| **V5** | **Vault Audit Log** | `<vault>/logs/audit.jsonl` (and `<vault>/.audit/audit.jsonl`) | Append-only JSONL (`session_id` recorded in entry details when present) | `recordAudit` (`plugins/minni/src/vault.ts`, `hook-handlers.ts`, `server.ts`) | `auditTail` (`hook-handlers.ts:1753`), `minni_audit_tail`, `minni_audit_report` | **None:** Append-only audit trail. Passing resolved `session_id` from `server.ts` improves correlation. | **SHARED-OK** |

---

### 1.3 Daemon SQLite Tables (`<minni-home>/minni.db` & `<vault>/.index/vault.db`)

| # | Table | Defined In (`file:line`) | Key Today | Writers / Readers | Concurrent-Session Hazard Today | Verdict |
|---|---|---|---|---|---|---|
| **D1** | `documents`, `vault_fts`, `chunk_embeddings`, `memory_links`, `learning_documents` | `src/minni/db.py:339-418`, `migrations/021_typed_memory_graph.sql:5-34` | `doc_id`, `path`, `agent` | Indexer (`src/minni/indexer.py`), recall engine (`src/minni/minnid_runtime/recall.py`) | **None:** Durable indexed vault documents. | **SHARED-OK** |
| **D2** | `learnings`, `learnings_fts`, `contradiction_log`, `consolidation_actions` | `src/minni/db.py:503-533`, `migrations/005`, `009`, `011`, `014` | `learning_id`, `agent_id` | Governance & consolidation (`src/minni/minnid_runtime/governance.py`) | **None:** Durable principal beliefs. | **SHARED-OK** |
| **D3** | `candidate_packets` | `src/minni/migrations/007_candidate_packets.sql:8-30`, `022`, `023` | `candidate_id`, scoped by `(principal, workspace_id)` | `learn` RPC (`governance.py`), `list_candidates`, `resolve_candidate` | **None:** Governance staging queue per principal + workspace. | **SHARED-OK** |
| **D4** | `learning_reads` & `contradiction_events` | `src/minni/migrations/008_handoff_contradiction_events.sql:18-37` | `(learning_id, agent_id, read_at)` | `recall.py:2790-2795`, `subscribe_contradictions` (`handoff.py:873-918`) | **None:** Tracks whether the principal has read a belief that was later contradicted. | **SHARED-OK** |
| **D5** | `handoff_leases` | `src/minni/migrations/008_handoff_contradiction_events.sql:3-16` | `lease_id`, routed by `to_agent` | `negotiate_handoff`, `list_pending_handoffs`, `ack_handoff` (`src/minni/minnid_runtime/handoff.py:539-620`), `SessionStart` (`hook-handlers.ts:1829-1861`) | **Known principal-level behavior:** When `ackPendingHandoffsAtBoot` is enabled (`hook.ts:69`), the first session to boot acks pending handoff leases addressed to `to_agent`. Handoffs are inter-agent (`from_agent -> to_agent`); intra-agent multi-session work uses Thread slices/attach (§3.5). | **SHARED-OK** |
| **D6** | `episodic_events`, `episodic_fts`, `task_logs`, `threads`, `thread_doc_links`, `agent_context`, `feedback`, `ax_snapshots` | `src/minni/db.py:422-548`, `migrations/006`, `010`, `024` | `event_id` / `task_id` / `thread_id` / `(agent_id, doc_id)` (`session_id` stored in `episodic_events.metadata` JSON) | Daemon RPC handlers (`src/minni/minnid.py`, `recall.py`) | **Audit gap only:** MCP server passes synthetic `MCP_PROCESS_SESSION_ID` (`server.ts:356,737`) instead of the host's real `session_id`, splitting recall trace correlation between hooks and MCP tools. | **SHARED-OK** (correlate via resolved `session_id` in metadata) |

---

### 1.4 In-Memory Process State (MCP Server, Hook Bridges, Daemon)

| # | Process & Surface | Where It Lives (`file:line`) | Key Today | Concurrent-Session Hazard Today | Verdict |
|---|---|---|---|---|---|
| **M1** | **MCP Server (`plugins/minni/src/server.ts`)** `MCP_PROCESS_SESSION_ID` | `plugins/minni/src/server.ts:356` (`const MCP_PROCESS_SESSION_ID = "mcp-" + Date.now() + "-" + ...`) | Process-static random string | **Misleads (`H6`):** Every MCP server stdio process is spawned 1:1 by a single host session (`process.ppid` = host PID), yet `server.ts` never resolves the host's `session_id` from `.runtime/active-session-<agent>-<hostPid>.json` or env. It passes `undefined` to `disarmRecallStateOnConsult` (`server.ts:722,832`) and `MCP_PROCESS_SESSION_ID` to `readSessionHandle` (`server.ts:841`). | **PER-SESSION** (dynamically resolve host `session_id` / `session_key` per tool call via `resolveMcpSessionIdentity()`) |
| **M2** | **KiloCode In-Process Plugin Bridge (`plugins/minni/kilo/minni-plugin.js`)** | `plugins/minni/kilo/minni-plugin.js:33-51` (`booted`, `pending`, `lastPrompt`) | Per-session maps keyed by `sessionID` (`BOOTED_MAX=256`, `PENDING_MAX=64`, `LAST_PROMPT_MAX=64`) | **None:** Already keyed by `sessionID` in memory (`minni-plugin.js:729-735`). | **PER-SESSION** (already keyed; preserve) |
| **M3** | **Daemon (`src/minni/minnid.py`)** In-Memory Caches & Worker Drain | `src/minni/minnid.py:2416-2420` (`worker_write_drain.py`) | Per-vault / per-`plan_id` | **None:** Daemon is stateless with respect to session turns; RPC parameters carry `agent_id`, `workspace_id`, and `session_id`. | **SHARED-OK** |

---

## 2. Session Identity (`session_id` & `session_key`)

### 2.1 What Ships Today: Host-by-Host Session Identity Survey

Every host platform invokes Minni across **two** runtime channels that run as separate OS processes:
1. **Hook channel:** Short-lived CLI processes (`hook.js`, `codex-hook.js`, `gemini-hook.js`, `grok-hook.js`, `cursor-hook.js`, `kilocode-hook.js`, `minni-hook.sh`) spawned on lifecycle events (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PreCompact`, `Stop`).
2. **MCP tool channel:** A long-lived stdio MCP server (`plugins/minni/dist/server.js`) spawned by the host for the duration of the session.

The table below documents what each supported host provides on both channels today (verified against `plugins/minni/src/*-hook.ts`, `*-adapter.ts`, and `docs/contracts/hook-platforms.md:327-348`):

| Host Platform | Principal (`agent_id`) | Hook Payload Session ID Field (`file:line`) | Environment Variables Available | Host PID (`process.ppid`) Relationship |
|---|---|---|---|---|
| **Claude Code** | `claude-code` | `payload.session_id` (UUID string, parsed by `rawSessionIdFromPayload` at `plugins/minni/src/hook-handlers.ts:392-400`) | `CLAUDE_CODE_SESSION_ID` (when exported in user/host env) | Hook `process.ppid` == Claude Code session process PID == MCP server `process.ppid` (`config.ts:132-135,233-239`) |
| **Codex CLI** | `codex` | `payload.session_id` (`plugins/minni/src/codex-hook.ts:1-41`, `hook-handlers.ts:392-400`) | `CODEX_SESSION_ID` / `MINNI_SESSION_ID` (optional) | Hook `process.ppid` == Codex session PID == MCP server `process.ppid` |
| **Gemini CLI / Antigravity (`agy`)** | `gemini` | `payload.session_id` (`GeminiCLI`) or `payload.invocation_id` (`Antigravity`), normalized by `normalizeGeminiHookInput` (`plugins/minni/src/gemini-adapter.ts:103-112`); falls back to `ppid-${process.ppid}` (`gemini-hook.ts:154-156`) | `GEMINI_SESSION_ID` / `ANTIGRAVITY_SESSION_ID` (optional) | Hook `process.ppid` == `agy` / Gemini process PID == MCP server `process.ppid` |
| **Grok Build (`grok`)** | `grok-build` | `payload.session_id` (`plugins/minni/src/grok-adapter.ts:35-52`, `grok-hook.ts:1-138`) | `GROK_SESSION_ID` (optional) | Hook `process.ppid` == Grok session PID == MCP server `process.ppid` |
| **Cursor** | `cursor` | `payload.conversation_id` or `payload.generation_id`, mapped to `session_id` by `normalizeCursorPayload` (`plugins/minni/src/cursor-adapter.ts:18-32`, `cursor-hook.ts:1-96`) | None standard | Hook and MCP server share Cursor workspace/extension-host parent tree |
| **KiloCode** | `kilocode` | `event.properties.sessionID` or `event.properties.info.id`, passed as `payload.session_id` by `plugins/minni/kilo/minni-plugin.js:729-735` to `kilocode-hook.ts` | `MINNI_KILOCODE_AGENT_ID` (`minni-plugin.js:14`) | `minni-plugin.js` runs inside Kilo's Bun host process; `kilocode-hook.js` is a child of that host process |
| **Devin CLI** | `devin` | Passed via `scripts/minni-hook.sh` (`session_id` in stdin JSON when configured; MCP-first otherwise) | `MINNI_SESSION_ID` / `DEVIN_SESSION_ID` (optional) | MCP server `process.ppid` == Devin CLI process PID |
| **Muse** | `muse` | No native hook wire; MCP-only (`server.ts`) | `MINNI_SESSION_ID` (optional) | MCP server `process.ppid` == Muse job runner process PID |

### 2.2 Proposed Design: Canonical Session Identity & Hook-to-MCP Bridge

#### 2.2.1 Canonical Identifiers (`session_id` and `session_key`)

1. **`session_id` (raw correlation string):**
   - Trimmed non-empty string up to 256 characters (control characters and path separators rejected).
   - Used in human-readable audit logs (`logs/audit.jsonl`), recall trace metadata (`episodic_events.metadata.session_id`), and diagnostic payloads.
2. **`session_key` (filesystem & cursor token):**
   - Computed by existing `sessionStateKey(sessionId)` (`plugins/minni/src/recall-state.ts:535-545`):
     ```ts
     export function sessionStateKey(sessionId: string | undefined): string | null {
       const trimmed = sessionId?.trim();
       if (!trimmed) return null;
       return createHash("sha256").update(trimmed).digest("hex").slice(0, 16);
     }
     ```
   - Always matching `/^[0-9a-f]{16}$/` (or `"default"` in the fail-closed fallback path).
   - Because `session_key` is a fixed 16-hex SHA-256 prefix, an arbitrary or untrusted `session_id` can **never** inject path separators (`/`), dots (`..`), or colons (`:`) into filenames or composite cursor keys.

#### 2.2.2 Enriching `.runtime/active-session-<agent>-<hostPid>.json`

In `plugins/minni/src/config.ts:117-204`, `recordActiveSessionWorkspace()` already runs on **every** hook invocation (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop` via `workspaceFor()` at `hook-handlers.ts:1585`) and writes `<vault>/.runtime/active-session-<agent>-<hostPid>.json` keyed by `hostPid = process.ppid`.

We extend `ActiveSessionRecord` (`config.ts:75-81`) to carry the session identity and process start marker:

```ts
export interface ActiveSessionRecord {
  workspace_id: string;
  agent_id: string;
  host_pid: number;
  /** OS process start marker for host_pid (guards against PID reuse). */
  process_start_marker?: string;
  /** Raw session_id from the hook payload (when provided by the host). */
  session_id?: string;
  /** 16-hex sha256(session_id) from sessionStateKey(session_id), or "pid-<16hex>" fallback. */
  session_key: string;
  updated_at: string;
}
```

And we also write a reverse index symlink or small pointer `<vault>/.runtime/active-session.d/<sessionKey>.json` containing the same `ActiveSessionRecord` so that:
- Given a `host_pid` (`process.ppid`), the MCP server immediately finds `(session_id, session_key, workspace_id)` in $O(1)$ via `active-session-<agent>-<hostPid>.json`.
- Given a `session_key` (for example, during GC or when checking if another session's active thread belongs to a still-live host process), Minni can check `host_pid` liveness in $O(1)$.

#### 2.2.3 How the MCP Server (`plugins/minni/src/server.ts`) Learns `session_id`

When an MCP tool handler runs in `plugins/minni/src/server.ts`, `resolveMcpSessionIdentity(vaultPath, agentId)` resolves `(sessionId, sessionKey, source)` in strict priority order:

1. **Priority 1 — Explicit environment variable (`MINNI_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` / `CODEX_SESSION_ID` / `GEMINI_SESSION_ID` / `ANTIGRAVITY_SESSION_ID` / `GROK_SESSION_ID`):**
   - If set and non-empty in the MCP server's environment, use `envSessionId` directly (`sessionKey = sessionStateKey(envSessionId)`, `source = "env"`).
2. **Priority 2 — Exact `host_pid` match in `<vault>/.runtime/active-session-<agent>-<hostPid>.json`:**
   - Read `active-session-${agentId}-${process.ppid}.json`.
   - If present, fresh within `ACTIVE_SESSION_TTL_MS` (30 minutes), and carries `session_id` / `session_key`, adopt that `session_id` and `session_key` (`source = "pid_exact"`).
   - *Why re-read (or cache with short 2s stat TTL) instead of reading only once at MCP server boot?* The host often spawns the MCP server in parallel with `SessionStart`, so the first hook write to `active-session-<agent>-<hostPid>.json` may land a few hundred milliseconds after `server.js` starts. Furthermore, if a host resumes or rotates `session_id` on `/clear` or `/compact` under the same `host_pid`, the hook updates `active-session-<agent>-<hostPid>.json` and the MCP server automatically picks up the new `session_id` on its next tool call.
3. **Priority 3 — Unambiguous single-live-session fallback (for hosts like Cursor where the MCP server's `process.ppid` is an extension-host helper rather than the hook's `process.ppid`):**
   - Scan fresh (`<= 30m`) `active-session-${agentId}-*.json` records whose `host_pid` is alive (`process.kill(pid, 0)`).
   - If all fresh live records agree on a single `session_key` (only one live session is running for this agent), adopt that `(session_id, session_key)` (`source = "single_live_session"`).
   - If multiple live sessions with different `session_key`s exist and `process.ppid` matched none of them, do **not** guess another session's `session_id`—fall through to Priority 4.
4. **Priority 4 — Host-PID-derived session identity (for MCP-only hosts without hooks, such as Muse or Devin without hooks, or before the first hook fires):**
   - If `process.ppid > 1`, derive deterministic `sessionId = "pid-${process.ppid}-${processStartMarker ?? MCP_PROCESS_BOOT_ID}"` and `sessionKey = sessionStateKey(sessionId)` (`source = "ppid_derived"`).
   - Because each concurrent MCP server instance is a child of its own host session process, two concurrent MCP-only sessions still receive **distinct** `session_key` values and never collide on active threads or relay cursors!
5. **Priority 5 — Fail-Closed Default (`"default"`):**
   - If no env var, no hook hint, and no usable `process.ppid` exists (`ppid <= 1` in a container init environment with no boot marker), return `{ sessionId: undefined, sessionKey: "default", source: "fallback_default" }`.
   - This matches today's single-session behavior without crashing or dropping tool calls.

#### 2.2.4 Fixing `disarmRecallStateOnConsult` and `readSessionHandle` in `server.ts`

With `resolveMcpSessionIdentity()` in place:
- **`minni_recall` (`server.ts:722`) and `minni_drill` (`server.ts:832`):** Pass `{ sessionId: mcpSession.sessionId }` to `disarmRecallStateOnConsult(DEFAULT_VAULT_PATH, { sessionId: mcpSession.sessionId })`.
- **Inside `disarmRecallStateOnConsult` (`plugins/minni/src/recall-state.ts:974-1020`):** Remove the loop (`lines 999-1014`) that iterates over all `*.json` files in `.runtime/recall-state.d/` when `effectiveSessionId` is undefined. When `sessionId` is provided, disarm **only** `.runtime/recall-state.d/<sessionKey>.json`. When `sessionId` is genuinely unknown (`source === "fallback_default"`), disarm only the legacy `.runtime/recall-state.json` (never another session's keyed `.runtime/recall-state.d/<otherKey>.json`).
- **Inside `writeRecallState` (`recall-state.ts:746,796`) and `readRecallState` (`recall-state.ts:817-826`):** When `guardV2 !== false` and `key !== null`, write and read **only** `.runtime/recall-state.d/<key>.json`. Do **not** mirror keyed session writes into `.runtime/recall-state.json`, and do **not** fall back from a missing `.runtime/recall-state.d/<key>.json` to `.runtime/recall-state.json` (which belongs only to unkeyed legacy callers).
- **Inside `minni_drill` (`server.ts:841`):** Pass `mcpSession.sessionId` to `readSessionHandle(DEFAULT_VAULT_PATH, args.handle, { sessionId: mcpSession.sessionId })` so `minni_drill` hits `.runtime/recall-state.d/<sessionKey>.handles.json` directly on the first lookup.

---

## 3. Active Thread Per Session (`H4`)

### 3.1 What Ships Today

1. **Single vault-wide pointer:** `<vault>/wiki/artifacts/_active_plan.json` (`plugins/minni/src/plan.ts:2468-2470`) stores one JSON object `{ plan_id, path, activated_at }`.
2. **Unconditional activation on create:** `minni_thread_create` (`server.ts:1536-1585`) has no `activate` input field. `createPlan` (`plan.ts:1023-1044`) unconditionally calls `setActivePlan(vaultPath, plan.plan_id, writeRes.notePath)`. If another non-terminal plan was active, it overwrites `_active_plan.json` and returns `displaced_active: incumbent.plan_id`.
3. **Global id-less resolution:** `resolvePlanIdOrActive` (`plan.ts:2622-2636`) and `resolvePlanTarget` (`server.ts:1590-1616`) read `getActivePlan(vaultPath)` whenever `plan_id` is omitted across 10 MCP tools (`minni_thread_update`, `minni_thread_scar`, `minni_thread_status`, `minni_thread_replan`, `minni_thread_history`, `minni_thread_ready`, `minni_thread_assign`, `minni_thread_claim`, `minni_thread_worker_update`, `minni_thread_events`).
4. **Global hook injection:** `SessionStart` (`hook-handlers.ts:1981-1990,2135-2137`) injects `active_thread` and `UserPromptSubmit` (`hook-handlers.ts:2766,2832-2834`) injects `active_thread_ref` by calling `resolveActivePlanView(config.vaultPath, ...)`, which reads `_active_plan.json`.

### 3.2 Proposed Design: Per-Session Active Thread Pointers

#### 3.2.1 Storage Layout

We introduce a per-session active-plan directory `<vault>/.runtime/active-thread.d/` alongside the existing `<vault>/wiki/artifacts/_active_plan.json`:

1. **Per-session pointer (primary):**
   - Path: `<vault>/.runtime/active-thread.d/<sessionKey>.json`
   - Schema:
     ```ts
     export interface SessionActivePlanPointer {
       version: 1;
       session_key: string;
       session_id?: string;
       agent_id: string;
        host_pid?: number;
       plan_id: string;
       path: string;          // "wiki/artifacts/plan-<16hex>.md"
       activated_at: string;  // ISO-8601 UTC
       updated_at: string;    // ISO-8601 UTC (refreshed on hook/tool access)
     }
     ```
   - Written atomically via `writeFileAtomicWithFsync` under `assertWriteTargetUnder(vaultPath, ...)`.
2. **Vault-level pointer (`<vault>/wiki/artifacts/_active_plan.json` — retained as last-activated fallback):**
   - Schema extended backward-compatibly with optional `owner_session_key?: string` and `owner_host_pid?: number`:
     ```ts
     export interface ActivePlanPointer {
       plan_id: string;
       path: string;
       activated_at: string;
       owner_session_key?: string;
       owner_host_pid?: number;
     }
     ```
   - Retained so single-session workflows, legacy tools, and fresh sessions that resume after a terminal restart continue to work seamlessly.

#### 3.2.2 `minni_thread_create` Semantics & the `activate` Parameter

We add an optional `activate?: boolean` parameter to `minni_thread_create` (`plugins/minni/src/server.ts:1542-1549` and `CreatePlanInput` in `plugins/minni/src/plan.ts:960-968`):

1. **`activate: true` (default when `activate` is omitted):**
   - Sets `<vault>/.runtime/active-thread.d/<callerSessionKey>.json` to the newly created `plan_id`.
   - If the **calling session** already had a non-terminal active plan `P_old` in `<callerSessionKey>.json`, returns `displaced_active: P_old` (scoped to the calling session!).
   - **Never overwrites another live session's `<otherSessionKey>.json`!** Session A's active plan in `.runtime/active-thread.d/<sessionKeyA>.json` is untouched.
   - Updates the vault-level fallback `<vault>/wiki/artifacts/_active_plan.json` **only if**:
     - `_active_plan.json` does not exist, OR
     - its current plan is terminal / missing, OR
     - its `owner_session_key` equals `callerSessionKey`, OR
     - the session recorded in `owner_session_key` / `owner_host_pid` is no longer live (dead `host_pid` or expired TTL > 30m).
   - If another live session `sessionKeyA` currently owns `_active_plan.json` with a non-terminal plan, `_active_plan.json` is left pointing to Session A's plan while Session B's `.runtime/active-thread.d/<sessionKeyB>.json` points to Session B's new plan. Response includes `session_active: true, vault_active_preserved_for_other_session: true` (and **no** false `displaced_active`).
2. **`activate: false`:**
   - Creates and persists the plan artifact `<vault>/wiki/artifacts/plan-<16hex>.md` and initial `.history.jsonl` / `.log.md` without modifying either `<callerSessionKey>.json` or `_active_plan.json`.
   - Useful when a coordinator pre-stages a backlog thread without switching its own active thread.

#### 3.2.3 Id-less `minni_thread_*` Tool Resolution (`resolvePlanIdOrActive`)

When any of the 10 optional-`plan_id` tools (`minni_thread_update`, `minni_thread_scar`, `minni_thread_status`, `minni_thread_replan`, `minni_thread_history`, `minni_thread_ready`, `minni_thread_assign`, `minni_thread_claim`, `minni_thread_worker_update`, `minni_thread_events`) is called without `plan_id`:

```
resolvePlanIdOrActive(vaultPath, planId, { sessionKey }):
  1. If explicit `planId` is non-empty -> return { plan_id: explicit }.
  2. If `sessionKey !== "default"`:
     Read `.runtime/active-thread.d/<sessionKey>.json`.
     If present and non-terminal -> return { plan_id: sessionPointer.plan_id }.
     If present and terminal -> auto-clear `<sessionKey>.json`.
  3. Inspect live `.runtime/active-thread.d/*.json` across all live sessions:
     - Case 3a (No other live session has an active plan):
       Read fallback `wiki/artifacts/_active_plan.json`.
       If present and non-terminal -> bind `<sessionKey>.json` to it and return { plan_id: vaultActive.plan_id }.
     - Case 3b (Exactly one live session has a non-terminal active plan AND `sessionKey === "default"`):
       Return that plan.
     - Case 3c (Caller session has NO active plan in `<sessionKey>.json`, while another live session `S_other` HAS an active plan `P_other`):
       Fail closed with a clear disambiguation error:
       "No active plan is bound to this session (another live session is working on <P_other>: '<goal>'). Pass plan_id explicitly, or call minni_thread_activate({ plan_id: '<P_other>' }) to attach this session to that plan."
```

Why Case 3c fails closed instead of silently falling back to `_active_plan.json`:
- Suppose Session A has active plan `P1`, and the user opens a brand-new Session B for an unrelated quick task. If Session B accidentally calls an id-less `minni_thread_update` or `minni_thread_deactivate`, silently falling back to Session A's `_active_plan.json` would mutate or deactivate Session A's in-flight plan!
- Requiring a new concurrent session to either pass `plan_id` or call `minni_thread_activate` (when another live session is already running a plan) prevents accidental cross-session mutation while keeping single-session resumption automatic (Case 3a: when you close your terminal and open a new one, the old session's `host_pid` is dead, so the new session automatically adopts `_active_plan.json`).

#### 3.2.4 `SessionStart`, `UserPromptSubmit`, and `Stop` Shelf / Active-Thread Injection

In `plugins/minni/src/hook-handlers.ts`:
1. **`SessionStart` (`hook-handlers.ts:1981-1990`):**
   - Calls `resolveActivePlanView(config.vaultPath, shelfContent, { sessionKey, sessionId: rawSessionId, hostPid: process.ppid })`.
   - Resolution rules at `SessionStart`:
     - If `.runtime/active-thread.d/<sessionKey>.json` exists (e.g., `/clear` or `/compact` re-boot of the same `session_id`), inject that session's active plan.
     - Else if no other **live** session (`kill(host_pid, 0)` alive and updated within 30m) currently owns `_active_plan.json`, adopt `_active_plan.json` into `.runtime/active-thread.d/<sessionKey>.json` and inject its full `active_thread` view (preserving seamless continuity when resuming work in a new terminal after closing the old one).
     - Else (another live session `S_other` is actively running plan `P_other`): do **not** bind `<sessionKey>.json` to `P_other` automatically. Instead, omit `active_thread` (so Session B's context budget isn't bloated by Session A's full plan markdown) and include a compact 1-line awareness field `concurrent_active_threads: [{ plan_id, goal, status, owner_session_key }]` in `SessionStart` so Session B knows `P_other` is running in another window and can attach to it via `minni_thread_activate` if it is a helper/worker session.
2. **`UserPromptSubmit` (`hook-handlers.ts:2766,2832-2834`):**
   - Calls `resolveActivePlanView(config.vaultPath, undefined, { sessionKey, strictSessionOnly: true })`.
   - Injects `active_thread_ref` **only** if `<sessionKey>.json` has an active plan (or if `sessionKey === "default"` and no other live session exists). A second session working on an unrelated task never gets `active_thread_ref` for Session A's plan injected on every prompt turn.
3. **`minni_thread_deactivate` (`server.ts:2407-2420`):**
   - Clears `.runtime/active-thread.d/<callerSessionKey>.json`.
   - Clears `wiki/artifacts/_active_plan.json` **only** if `_active_plan.json` points to the same `plan_id` and no other live session has that `plan_id` active in `.runtime/active-thread.d/`.

#### 3.2.5 Handoff & Multi-Session Collaboration on the Same Thread

How do two concurrent sessions collaborate on the **same** plan (e.g., Session A is the coordinator on `P1`, and Session B is a worker or reviewer on `P1`)?
1. **Explicit `plan_id` (Worker Slice Model — works today and remains primary):**
   - Session A calls `minni_thread_assign` or `minni_thread_claim` and hands `(plan_id, slice_id, claim_token)` to Session B in its prompt. Session B passes `plan_id` explicitly to `minni_thread_worker_update` / `minni_thread_status`.
2. **Session Attach (`minni_thread_activate({ plan_id })`):**
   - Multiple concurrent sessions **may** bind `.runtime/active-thread.d/<sessionKey>.json` to the **same** `plan_id` by calling `minni_thread_activate({ plan_id })`.
   - Both sessions then see `active_thread_ref` for that plan and both receive independent thread relay notifications (§4) via their own `(agent_id, session_key)` cursor!
3. **Session Handoff (Terminal Replacement):**
   - When Session A exits and Session B starts later, Session A's `host_pid` is no longer live. Session B's `SessionStart` sees `_active_plan.json` is unclaimed by any live process, adopts `P1` into `.runtime/active-thread.d/<sessionKeyB>.json`, and continues seamlessly.

---

## 4. Thread Notification Relay Cursors (`H6b`)

### 4.1 What Ships Today

In `plugins/minni/src/thread-notification-relay.ts` and `src/minni/thread_relay.py`:
- `SQLITE_020_LIVE = False` (`thread-notification-relay.ts:33-34`, `thread_relay.py:28-31`); all live cursor state lives in `<vault>/.runtime/thread-relay/cursors.json`.
- `RelayCursor` has shape `{ subscriber_id: string, plan_id: string, last_delivered_seq: number }`.
- `RelayNotification` in `store.pending` has shape `{ subscriber_id, plan_id, seq, event_id, kind, at, actor, slice_id?, summary, fingerprint }`.
- `seedRelaySubscribers()` (`thread-notification-relay.ts:287-310`, `thread_relay.py:332-361`) seeds bare agent IDs (`"claude-code"`, `"codex"`, etc.).
- When Session A confirms hook delivery (`confirmDeliveryInVault` at `hook-handlers.ts:760`), polls `minni_thread_events` (`acknowledgeRelayThroughSeq` at `server.ts:2766`), or mutates a plan (`acknowledgeSelfAuthoredPrefixInVault` at `server.ts:1781,1924`), it advances `last_delivered_seq` for `subscriber_id = "claude-code"` and deletes those entries from `store.pending`, hiding them from Session B.
- Furthermore, `saveRelayStore()` (`thread-notification-relay.ts:446-453`, `thread_relay.py:293-302`) writes `cursors.json` with a plain non-atomic `writeFile` / `write_text` and no lock, risking torn writes when a hook process and an MCP server process ack concurrently.

### 4.2 Proposed Design: Session-Scoped Relay Cursors

#### 4.2.1 Composite Subscriber ID Format

We keep the `(subscriber_id, plan_id)` primary key shape in `RelayCursor` (and `020_thread_delivery_cursors.sql`), and encode session scope directly in `subscriber_id`:

$$\text{subscriber\_id} = \begin{cases} \texttt{"<agent\_id>:<session\_key>"} & \text{when } \texttt{session\_key} \neq \texttt{"default"} \\ \texttt{"<agent\_id>"} & \text{when } \texttt{session\_key} = \texttt{"default"} \text{ (legacy/unkeyed fallback)} \end{cases}$$

Examples:
- Session A (`claude-code`, `session_key = "a1b2c3d4e5f60718"`): `subscriber_id = "claude-code:a1b2c3d4e5f60718"`
- Session B (`claude-code`, `session_key = "9988776655443322"`): `subscriber_id = "claude-code:9988776655443322"`
- Cross-agent worker (`codex`, `session_key = "1122334455667788"`): `subscriber_id = "codex:1122334455667788"`

Why `<agent_id>:<session_key>` instead of changing the JSON/SQLite primary key tuple?
1. `agent_id` conforms to `/^[a-z0-9][a-z0-9_-]{0,63}$/` (`src/minni/principal.py:67`) and **never** contains `:`.
2. `session_key` is `[0-9a-f]{16}` and **never** contains `:`.
3. Therefore `subscriber_id.split(":")` is an unambiguous, lossless bijection `(agent_id, session_key?) <-> subscriber_id` that requires **zero schema migration** for `RelayCursor` or `020_thread_delivery_cursors.sql`!

#### 4.2.2 Seeding & Fanout Rules (`seedRelaySubscribers`)

Which sessions should receive relay notifications for `planId` when `ingestJournalIntoVault` runs?
1. **Existing session cursors for `planId`:** Every `cursor.subscriber_id` already present in `store.cursors` for `planId`.
2. **Sessions bound to `planId` in `.runtime/active-thread.d/*.json`:** Every live session in `<vault>/.runtime/active-thread.d/<sessionKey>.json` whose `plan_id === planId` is seeded as `"<agent_id>:<sessionKey>"`.
3. **The mutating caller session (`input.actorSessionKey`):** If the caller is `actor = "claude-code"` with `sessionKey = "a1b2..."`, seed `"claude-code:a1b2..."`.
4. **Principal baseline cursor (`"<agent_id>"`):** Still maintained as the principal-wide high-water fallback for new sessions (see §4.2.3).

#### 4.2.3 Replay Semantics for a New Session (No Ancient History Flood, No Lost In-Flight Events)

When a new session `S_new` (`subscriber_id = "<agent_id>:<sessionKeyNew>"`) first attaches to or queries an existing `planId` that already has journal events `1..N`:

- **Problem if initialized at `last_delivered_seq = 0`:** If `planId` has 80 historical events from earlier sessions, `S_new` would be flooded with up to `MAX_HOOK_ATTENTION_ITEMS` (8) ancient events that were already handled hours ago.
- **Problem if initialized at `last_delivered_seq = N` (journal head):** If a worker completed a slice 2 seconds *before* `S_new` booted (and nobody in `agent_id` has seen that completion yet), initializing at `N` would drop that unacknowledged event.
- **Solution — Fork Cursor from Principal Unacked Floor (`forkSessionCursor`):**
  When `"<agent_id>:<sessionKeyNew>"` has no cursor for `planId` yet:
  1. Look up existing cursors for `planId` whose principal prefix is `<agent_id>` (either bare `"<agent_id>"` or live `"<agent_id>:<otherSessionKey>"`).
  2. Set `initial_seq`:
     - If `S_new` was just attached via `minni_thread_create`, `initial_seq = 0` (and self-authored `thread_created` is auto-acked for the creator session).
     - If `S_new` is a **second concurrent session** attaching via `minni_thread_activate({ plan_id })` while another live session `S_live` of `<agent_id>` is already tracking `planId`: initialize `last_delivered_seq = S_live.last_delivered_seq` (or if `minni_thread_events` was called with explicit `since_seq`, use `since_seq`). That way `S_new` starts caught up to the active coordinator point without replaying old history, and from that moment forward `S_live` and `S_new` advance their cursors **independently**.
     - If `S_new` is **resuming** `planId` when no other live session of `<agent_id>` exists: initialize `last_delivered_seq` from the bare `"<agent_id>"` cursor (or the most recently updated dead session cursor for `<agent_id>` on `planId`), so any worker events that landed while no session was open are still delivered to `S_new` at `SessionStart`!

#### 4.2.4 Self-Authored Event Auto-Ack (`acknowledgeSelfAuthoredPrefixInVault`)

In `plugins/minni/src/server.ts:1781,1924`:
- Today, when Session A calls `minni_thread_update` or `minni_thread_scar`, `acknowledgeSelfAuthoredPrefixInVault` auto-acks leading events where `item.actor === "claude-code"`.
- **With multi-session principal:** Session A's mutation is self-authored **only for Session A** (`"claude-code:<sessionKeyA>"`), **not** for Session B (`"claude-code:<sessionKeyB>"`)! If Session A (a sub-slice worker or peer session sharing the `claude-code` principal) updates a slice or records a scar, Session B (the coordinator session) **wants** to be notified of that update/scar.
- Therefore:
  1. Ordered thread events in `<plan_id>.log.md` and `RelayNotification` gain an optional `actor_session_key?: string` metadata field.
  2. `acknowledgeSelfAuthoredPrefixInVault` advances **only** the calling session's cursor (`"<agent_id>:<callerSessionKey>"`), never another session's cursor (`"<agent_id>:<otherSessionKey>"`).

#### 4.2.5 Atomic `cursors.json` Persistence & Lock Protection

To eliminate the race in `saveRelayStore()` (`thread-notification-relay.ts:446-453` and `src/minni/thread_relay.py:293-302`):
1. All read-modify-write operations on `<vault>/.runtime/thread-relay/cursors.json` acquire a short cross-process lock `<vault>/.runtime/thread-relay/cursors.lock` (reusing `thread-lock.ts`'s `mkdir` + `owner.json` + PID liveness pattern with `waitMs = 2_000`).
2. `saveRelayStore()` writes to a temporary file `<vault>/.runtime/thread-relay/cursors.json.<pid>.<uuid>.tmp`, `fsync`s, and atomically `rename`s over `cursors.json` (`writeFileAtomicWithFsync`).

#### 4.2.6 Backward Compatibility & Migration of Existing `cursors.json` Rows

1. Existing `<vault>/.runtime/thread-relay/cursors.json` files on disk have `version: 1` with bare `subscriber_id = "<agent_id>"` entries.
2. No destructive migration is required:
   - Bare `"<agent_id>"` rows remain valid in `cursors.json` and serve as the initial watermark when the first session `"<agent_id>:<sessionKey>"` forks its session cursor (§4.2.3).
   - Whenever any `"<agent_id>:<sessionKey>"` cursor advances to `seq`, the bare `"<agent_id>"` watermark cursor is updated to the **minimum** `last_delivered_seq` across all live sessions of `<agent_id>` on `plan_id` (or `seq` if only one live session is attached), so if all sessions close and a new session opens tomorrow, the bare `"<agent_id>"` cursor accurately reflects what has been delivered.
3. For `src/minni/migrations/020_thread_delivery_cursors.sql`: keep `SQLITE_020_LIVE = False` (or if ever enabled later, its `subscriber_id TEXT NOT NULL` column already accepts `"<agent_id>:<session_key>"` without schema changes).

---

## 5. Lifecycle & Garbage Collection

### 5.1 Liveness Detection & TTLs

Per-session files in `<vault>/.runtime/` must expire deterministically so crashed terminals do not leave zombie active plans or unbounded cursor arrays behind.

#### 5.1.1 Two-Factor Liveness Check (`host_pid` + TTL)

A session `S` recorded in `<vault>/.runtime/active-session-<agent>-<hostPid>.json` or `<vault>/.runtime/active-thread.d/<sessionKey>.json` is considered **LIVE** if and only if:
1. **Process liveness:** If `host_pid` is recorded (`host_pid > 1`), `process.kill(host_pid, 0)` succeeds (or fails with `EPERM`, not `ESRCH`), **and** if `process_start_marker` is present, it matches `readProcessStartMarker(host_pid)` (`plugins/minni/src/thread-lock.ts:84-99`).
   - *Immediate death detection:* As soon as a terminal window or agent process exits (`ESRCH`), its `host_pid` is immediately detected as dead (`0 ms` wait)—no waiting for a 30-minute timer to expire!
2. **Sliding inactivity TTL:** `now - Date.parse(updated_at) <= ACTIVE_SESSION_TTL_MS` (`30 minutes` for workspace/active-session liveness; `24 hours` for `.runtime/active-thread.d/<sessionKey>.json` when `host_pid` is still alive, e.g. a terminal left open overnight).

#### 5.1.2 Per-Surface Expiry & GC Schedule

| Runtime Surface | Live Window | Prune / GC Threshold | Who Sweeps It |
|---|---|---|---|
| `.runtime/active-session-<agent>-<hostPid>.json` & `.runtime/active-session.d/<sessionKey>.json` | `host_pid` alive AND `updated_at <= 30m` | Delete file when `host_pid` is dead (`ESRCH`) OR `updated_at > 24h` (`ACTIVE_SESSION_PRUNE_AGE_MS`) | `recordActiveSessionWorkspace` (`config.ts:152-200`) on hook writes |
| `.runtime/active-thread.d/<sessionKey>.json` | `host_pid` alive (if known) AND `updated_at <= 24h` | Delete pointer file when `plan` is terminal, OR (`host_pid` dead AND `updated_at > 24h`), OR `updated_at > 7d`. (Before deleting, if `wiki/artifacts/_active_plan.json` is empty, preserve the last non-terminal `plan_id` in `_active_plan.json`.) | `resolveActivePlanView`, `createPlan`, and `.sweep.json` gated sweeper (at most once per 10 minutes) |
| `.runtime/thread-relay/cursors.json` (`<agent>:<sessionKey>` cursors & `pending` items) | While `<sessionKey>` is live or `updated_at <= 24h` | Prune `"<agent>:<sessionKey>"` cursors and their `store.pending` entries when `<sessionKey>` has been dead/inactive `> 24h` OR when `plan_id` is in `TERMINAL_PLAN_STATUSES` (`accepted`, `complete`, `rejected`, `superseded`) for `> 24h`. Cap total cursors at `MAX_RELAY_CURSORS = 256` (LRU by `updated_at`). | `ingestJournalIntoVault` / `confirmDeliveryInVault` ( adds `updated_at: string` to `RelayCursor`) |
| `.runtime/recall-state.d/<sessionKey>.*` (`.json`, `.counters.json`, `.handles.json`, `.failure-lessons.json`, `.lifecycle.json`, `.agy-turn.json`, `.claim`) | `24h` | Delete files with `mtime > 24h` (`CLAIM_RETENTION_MS`) | `pruneOldClaimFiles` (`recall-state.ts:579-608`) / `sweepSessionDirs` (`.sweep.json` gate) |
| `.runtime/recall-session.d/<sessionKey>/` | `24h` | Delete session subdirectory with `mtime > 24h` | `sweepSessionDirs` (`recall-state.ts`) |
| `.runtime/use-ledger/<sessionKey>/<yyyy-mm-dd>.jsonl` | `30 days` | Delete daily JSONL files older than `30 days` (`USE_LEDGER_RETENTION_DAYS = 30`) | `sweepUseLedger` (`use-ledger.ts:740-810`) |

### 5.2 Strict `NEVER DELETE` Invariants

Session GC operates **exclusively** on ephemeral pointer/cursor/guard files under `<vault>/.runtime/`. It **MUST NEVER DELETE**:
1. **Plan & Thread artifacts:** `<vault>/wiki/artifacts/plan-<16hex>.md`, `<plan_id>.history.jsonl`, `<plan_id>.log.md`. Expiring `.runtime/active-thread.d/<sessionKey>.json` only unbinds the session pointer; the plan note, revision history, scars, and ordered event log remain intact on disk forever.
2. **Vault durable notes & shelves:** `<vault>/layer1/**`, `<vault>/wiki/**`, `<vault>/raw/**`, `<vault>/inbox/**` (except settled `precompact-reassert` entries after verified host delivery), `<vault>/outbox/**`, `<vault>/logs/**`.
3. **Use-ledger and detector evidence within the 30-day window:** `<vault>/.runtime/use-ledger/<sessionKey>/*.jsonl` (within 30d), `<vault>/.runtime/corpus-df.json`, `<vault>/.runtime/use-df-state.json`, `<vault>/.runtime/prompt-templates.json`.
4. **Live thread locks or active worker write queues:** `<vault>/.runtime/thread-locks/<lockKey>.lock` and `<lockKey>.q/` (governed solely by `thread-lock.ts` and `thread-write-queue.ts`).
5. **SQLite tables:** `learnings`, `candidate_packets`, `documents`, `memory_links`, `episodic_events`, `handoff_leases`, `contradiction_events`, `contradiction_log`.

---

## 6. Security, Trust & Containment

### 6.1 Non-Escalation Invariant: `session_id` Is Never a Principal

In `src/minni/principal.py`:
- Authentication and authorization are governed by `EffectivePrincipal` (`agent_id`, `role`, `allowed_vault_roots`, `auto_accept_own`, `allow_shared_Promotion`, `allow_public_export`, `default_Denied`).
- **Rule 1 (No capability grant):** `session_id` / `session_key` is an ambient runtime sub-handle **inside** `EffectivePrincipal.agent_id`. It is never looked up in `<minni-home>/principals/*.json`, never alters `default_denied`, and never grants write access to governed RPCs (`learn`, `resolve_candidate`, `promote`).
- **Rule 2 (Read policy stays per principal):** `can_read_document()` (`src/minni/principal.py:700-750`) evaluates `privacy_level` (`private`, `shared`, `public`) and `agent` ownership against `EffectivePrincipal.agent_id` and `allowed_vault_roots`. Two sessions of `claude-code` have identical read visibility over `claudecode-vault` and shared memories.
- **Rule 3 (Governance & `auto_accept_own` stay per principal):** `candidate_packets` remain keyed by `(principal, workspace_id)`. A candidate proposed in Session A is visible to `minni_list_candidates` in Session B under the same principal.

### 6.2 Threat Model & Spoofing / Containment Controls

| Risk / Attack Vector | Analysis | Concrete Control |
|---|---|---|
| **T1. Path Traversal / Arbitrary File Overwrite via Malicious `session_id`** (e.g. `session_id = "../../wiki/layer1/core"`) | If raw `session_id` were interpolated into `.runtime/` filenames, a crafted hook payload could write outside `.runtime/`. | **Never use raw `session_id` in filesystem paths.** All paths use `sessionKey = sessionStateKey(sessionId)` (`sha256(sessionId).slice(0, 16)`), strictly validated against `/^[0-9a-f]{16}$/` (plus `"default"`), combined with `assertWriteTargetUnder(vaultPath, targetPath)` and `O_NOFOLLOW` atomic writes (`plugins/minni/src/vault.ts`). |
| **T2. Composite Cursor Key Injection** (e.g. `session_id` containing `:` or JSON control chars) | Could corrupt `subscriber_id = "<agent_id>:<session_key>"` parsing in `cursors.json`. | `subscriber_id` always uses the 16-hex `session_key` (`[0-9a-f]{16}`), never the raw `session_id`. `agent_id` is validated against `AGENT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/` (`principal.py:67`). |
| **T3. Model-Supplied `session_id` Spoofing on MCP Tool Calls** | If MCP tool schemas (`minni_thread_update`, `minni_recall`, etc.) exposed a `session_id` argument, a confused or prompt-injected model could pass another session's ID and disarm its recall guard or hijack its active thread. | **Do NOT expose `session_id` on MCP tool input schemas.** The MCP server resolves `session_id` / `session_key` strictly from its own OS process environment and `process.ppid` -> `active-session-<agent>-<hostPid>.json` (§2.2.3). |
| **T4. Same-Principal Cross-Session Contention** | Two sessions of `claude-code` run under the same OS user and same `agent_id` against the same `<vault>`. | **Explicit trust boundary:** Same-principal sessions are **not** adversarial security principals to each other (they share filesystem permissions and vault access); the boundary between them is **concurrency isolation and ergonomics** (preventing accidental clobbering of active threads, relay cursors, and recall guards). Cross-principal isolation continues to be enforced by the daemon's `EffectivePrincipal` and `allowed_vault_roots`. |
| **T5. Symlink Plant in `<vault>/.runtime/active-thread.d/` or `thread-relay/`** | Local attacker or rogue script plants a symlink at `.runtime/active-thread.d/<sessionKey>.json`. | All `.runtime/` directory creations and file writes go through `assertWriteTargetUnder(vaultPath, ...)` (`plugins/minni/src/vault.ts`) which walks ancestor components with `lstat` and rejects any symlink component. |

---

## 7. Migration & Rollout Plan (Small Verifiable Slices)

Each slice below is self-contained, keeps `main` green, and ships with deterministic tests—including **real multi-process concurrency tests** that spawn **two live `dist/server.js` MCP server child processes** (with distinct parent PIDs / `MINNI_SESSION_ID`s) against a single temporary vault.

### Slice S1: Enrich Active-Session Hint & MCP Server Session Identity Resolution (`H6c`, `H6d`)
- **Files touched:**
  - `plugins/minni/src/config.ts` (`recordActiveSessionWorkspace`, `resolveSessionWorkspaceWithDiag`, new `resolveMcpSessionIdentity`)
  - `plugins/minni/src/hook-handlers.ts` (pass `rawSessionId` and `processStartMarker` into `recordActiveSessionWorkspace` inside `workspaceFor`)
  - `plugins/minni/src/recall-state.ts` (stop writing/reading `.runtime/recall-state.json` when `sessionKey !== null` and `guardV2 !== false`; stop `disarmRecallStateOnConsult` from looping over all `.runtime/recall-state.d/*.json` when `sessionId` is omitted; scope `lifecycle-state` to `.runtime/recall-state.d/<sessionKey>.lifecycle.json`)
  - `plugins/minni/src/server.ts` (replace static `MCP_PROCESS_SESSION_ID` usage in `minni_recall` and `minni_drill` with `resolveMcpSessionIdentity()`)
- **Real concurrency test (`plugins/minni/tests/multi-session-recall-guard.test.mjs`):**
  - Spawn two real MCP server processes (`serverA`, `serverB`) against one temp vault, paired with two hook sessions (`sessionA`, `sessionB`) that arm strong recall guards in `.runtime/recall-state.d/<keyA>.json` and `<keyB>.json`.
  - Call `minni_recall` on `serverB` only.
  - **Assert:** `sessionB`'s recall state is marked `consumed: true` and `PreToolUse` on `sessionB` allows `Read`/`Grep`; `sessionA`'s recall state in `<keyA>.json` remains `consumed: false` (armed), and `PreToolUse` on `sessionA` still blocks `Read`/`Grep` until `serverA` calls `minni_recall`!
  - **Assert (`minni_drill`):** Handles `m:<8hex>` recorded by `sessionA`'s hook are resolved directly by `serverA`'s `minni_drill` from `<keyA>.handles.json` even when `sessionB` has overwritten its own handles.

### Slice S2: Per-Session Active Thread Pointers & `minni_thread_create({ activate })` (`H4`)
- **Files touched:**
  - `plugins/minni/src/plan.ts` (`getSessionActivePlan`, `setSessionActivePlan`, `clearSessionActivePlan`, `resolvePlanIdOrActive`, `resolveActivePlanView`, `createPlan` `activate?: boolean`)
  - `plugins/minni/src/server.ts` (`minni_thread_create` schema + handler, `resolvePlanTarget`, `minni_thread_activate`, `minni_thread_deactivate`, `minni_thread_update` auto-clear)
  - `plugins/minni/src/hook-handlers.ts` (`handleSessionStart`, `handleUserPromptSubmit`, `handleStop` pass `sessionKey` / `hostPid` to `resolveActivePlanView`)
- **Real concurrency test (`plugins/minni/tests/multi-session-active-thread.test.mjs`):**
  - Spawn two real MCP server processes (`serverA`, `serverB`) against one temp vault.
  - `serverA` calls `minni_thread_create({ goal: "Coordinator Plan A", slices: [...] })` -> creates `planA`.
  - `serverB` calls `minni_thread_create({ goal: "Worker Plan B", slices: [...] })` (with no `activate` flag) -> creates `planB`.
  - **Assert:**
    1. `serverA` calling id-less `minni_thread_status({})` and `minni_thread_update({ status: "candidate" })` resolves to `planA` (NOT `planB`).
    2. `serverB` calling id-less `minni_thread_status({})` resolves to `planB`.
    3. `UserPromptSubmit` hook for `sessionA` injects `active_thread_ref` for `planA`; `UserPromptSubmit` hook for `sessionB` injects `active_thread_ref` for `planB`.
    4. `serverB` calling `minni_thread_deactivate({})` deactivates `planB` for `sessionB` while `planA` remains active for `sessionA`.
    5. A third fresh session `serverC` (with no active plan of its own while `serverA` is still live) calling id-less `minni_thread_update({})` fails closed with the disambiguation error rather than mutating `planA`.

### Slice S3: Session-Scoped Thread Notification Relay Cursors & Atomic Store (`H6b`)
- **Files touched:**
  - `plugins/minni/src/thread-notification-relay.ts` (`formatSessionSubscriberId`, `forkSessionCursor`, atomic lock + `writeFileAtomicWithFsync` in `saveRelayStore`, session-scoped `acknowledgeSelfAuthoredPrefixInVault`, cursor GC)
  - `src/minni/thread_relay.py` (exact Python parity for `format_session_subscriber_id`, `parse_session_subscriber_id`, cursor forking, fail-closed `cursors.lock`, Q2 sibling-session notifications, bare watermark propagation, per-session subscriber discovery, and dead-session cursor GC)
  - `plugins/minni/src/thread-events.ts` (`actor_session_key` on ordered events)
  - `plugins/minni/src/hook-handlers.ts` (`attachPendingAttention` and `handleStop` pass `subscriberId = formatSessionSubscriberId(config.agentId, sessionKey)`)
  - `plugins/minni/src/server.ts` (`minni_thread_events`, `minni_thread_update`, `minni_thread_scar` pass `mcpSession.sessionKey`)
- **Real concurrency tests (`plugins/minni/tests/multi-session-thread-relay.test.mjs` and `tests/test_thread_relay.py`):**
  - Spawn two real MCP server processes (`serverA`, `serverB`) sharing one vault and both attached to `planA` via `minni_thread_activate({ plan_id: planA })`.
  - A worker (`codex`) completes slice `S1` on `planA` (`minni_thread_worker_update`), generating a `slice_completed` relay event (`seq = 2`).
  - `sessionA` runs `UserPromptSubmit` (or `serverA` calls `minni_thread_events`), receiving and acknowledging `seq = 2`.
  - **Assert:** `sessionB` subsequently runs `UserPromptSubmit` (or `serverB` calls `minni_thread_events`) and **still receives `seq = 2` (`slice_completed`)**!
  - Next, `serverA` calls `minni_thread_scar` on `planA` (`seq = 3`).
  - **Assert:** `seq = 3` is auto-acked as self-authored for `sessionA`, but **delivered** as a pending notification to `sessionB`!

### Slice S4: PreCompact Reassert Session Affinity & Dead-Session GC Sweep
- **Files touched:**
  - `plugins/minni/src/hook-handlers.ts` (tag `precompact-reassert` files with `session_key`; prefer matching `session_key` at `SessionStart` before claiming orphaned `>10m` files)
  - `plugins/minni/src/plan.ts`, `plugins/minni/src/thread-notification-relay.ts`, & `src/minni/thread_relay.py` (dead-session GC sweep gated on `.sweep.json`)
- **Tests (`plugins/minni/tests/multi-session-gc-and-reassert.test.mjs` and `tests/test_thread_relay.py`):**
  - Verify `SessionA` `PreCompact` stash is not stolen when `SessionB` runs `SessionStart` 100ms later, and verify orphaned stashes from dead sessions (`>10m`) are still recovered on the next boot.
  - Verify GC removes expired `.runtime/active-thread.d/<deadKey>.json` and dead session cursors without ever touching `wiki/artifacts/plan-*.md`, `*.log.md`, or `*.history.jsonl`.

---

## 8. Explicit Non-Goals & Open Questions for Hans

### 8.1 Explicit Non-Goals

1. **No splitting of durable memory (`learnings`, `documents`, `wiki/`):** Two sessions of `claude-code` are still the same agent mind. What Session A learns or writes to the vault wiki is immediately searchable by Session B.
2. **No new security principal per session:** Sessions do not get their own `<minni-home>/principals/*.json` files, permissions, or private sub-vaults.
3. **No SQLite schema churn required:** Threads remain markdown + JSONL artifacts in `<vault>/wiki/artifacts/`, and relay cursors remain in `<vault>/.runtime/thread-relay/cursors.json` using composite `"<agent_id>:<session_key>"` strings.
4. **No cross-machine distributed locking:** Session liveness (`process.kill(host_pid, 0)`) assumes all sessions against a local vault run on the same host machine.

### 8.2 Open Questions for Hans (Plain Words)

Everything in slices S1–S4 has a clear technical default. There are **two behavior choices** where your preference decides how a second terminal window behaves:

#### Question 1: When you open a SECOND terminal window while the first window is actively working on a plan, what should the second window see at startup?

- **(Recommended) Option A — Clean slate with a 1-line heads-up:** The second window starts with **no** active plan attached to it (so it won't accidentally edit the first window's plan and won't waste context space repeating the full plan), and gets a short 1-line note saying *"Another live session is currently working on plan `<goal>` (`<plan_id>`) — call `minni_thread_activate` if you want this window to join it."* (If the first window was already closed, the new window automatically picks up the plan just like today.)
- **Option B — Auto-attach both windows to the same plan:** Every new window automatically attaches to the vault's active plan until it creates or activates a different plan.
- **Option C — Never auto-attach even after closing a window:** Every new terminal window starts with no active plan unless you explicitly tell it to activate one.

#### Question 2: When one window updates or adds a scar to a shared plan, should your OTHER open window get a notification about it?

- **(Recommended) Option A — Yes, notify the other open window:** If Window A and Window B are both attached to the same plan and Window A records a scar or updates a slice, Window B gets a notification on its next turn so both windows stay in sync (while Window A does not notify itself).
- **Option B — No, only notify on updates from a different agent (like Codex or Grok):** Treat all `claude-code` windows as "self" so one `claude-code` window never notifies another `claude-code` window.

---

## 9. Coordinator Review Amendments (binding on implementation)

Review of commit 3d321f28. Code claims spot-checked (`server.ts:356`, `plan.ts:2469`, `thread-notification-relay.ts:33,201`, `recall-state.ts:999-1014`): they hold. Two changes are binding:

### R1. Write the session record even when the workspace is unknown (blocks S1)

`recordActiveSessionWorkspace` (`config.ts:126-129`) returns early when the workspace normalizes to `workspace-unknown` or the path is a disallowed hint path (for example the home directory). Under §2.2.2 the session identity rides in that same file. So a session started outside a git repository never writes a session record. Then:
- Priority 2 (`pid_exact`) misses.
- Priority 3 (`single_live_session`) adopts **another** live session's `session_key` when exactly one other session is live. That is exactly the collision this design exists to prevent.

Required:
1. Split the write. The session record (`agent_id`, `host_pid`, `process_start_marker`, `session_id`, `session_key`, `updated_at`) is always written when the hook has a session id or a usable host pid. `workspace_id` becomes optional in that record and is omitted (never `workspace-unknown`) when unknown. Workspace resolution keeps its current fail-closed rule.
2. Priority 3 applies only to hosts known to run the MCP server under a different parent than the hooks (today: Cursor), behind an explicit per-host allowlist. For every other host, a miss falls through to Priority 4 (`ppid_derived`). Never borrow another session's key.
3. S1's concurrency test adds this case: session A is started in a repository, session B is started outside any repository, and B's MCP server must not resolve to A's `session_key`.

### R2. Host session environment variables are trusted only for the matching agent

Child processes inherit the environment. A Gemini or Codex CLI launched from a Claude Code shell inherits `CLAUDE_CODE_SESSION_ID`. Priority 1 therefore reads only the variable for the resolved agent (`CLAUDE_CODE_SESSION_ID` only for `claude-code`, `CODEX_SESSION_ID` only for `codex`, and so on) plus the generic `MINNI_SESSION_ID`. A foreign host's variable is ignored and logged once at debug level.

### Decisions (operator, 2026-11-03)
- §8.2 Question 1: Option A (clean slate with a one-line heads-up).
- §8.2 Question 2: Option A (notify the other open session of the same agent; never self-notify).
- Design approved. Implemented in S1–S4 with R1 and R2.
