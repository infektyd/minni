# Agent-First Thread Orchestration V2

Status: approved direction, corrected by G0 review  
Date: 2026-08-18

## Decision

Keep the existing vault Thread as the single durable graph and evolve it in
place before adding delivery infrastructure.

- **Threads own graph state**: slices, dependencies, evidence, claims, scars,
  revisions, and ordered events.
- **Team / Research / Bulk are execution adapters**: they project ready Thread
  slices into host-specific worker packets and do not create another
  authoritative ledger.
- **The orchestrator owns topology**: workers may mutate only a claimed slice
  and may propose expansion or contraction.
- **The host executes models**: Minni does not claim spawn or immediate-wake
  behavior that the host cannot verify.
- **The daemon becomes a notification relay later**: the Thread journal is the
  durable event source. A failed relay publish is recovered by reading journal
  events after a cursor; it never becomes a second Thread authority.

This preserves one implementation language and one source of truth while
making the graph safe for multiple workers.

## Why V1 stopped at G0

G0 found seven blocking problems in the original daemon-canonical design:

1. One `graph_json` row still loses independent updates unless every mutation
   reloads under `BEGIN IMMEDIATE`.
2. Hash-only claim tokens cannot be returned after a lost claim response.
3. Current Team workers share their platform principal with the orchestrator,
   so the daemon cannot infer orchestrator-versus-worker authority.
4. Claim expiry, completion, and reassignment had no single-winner contract.
5. Event uniqueness did not define idempotent response replay.
6. G1 would leave file-backed MCP tools and hooks mutating a projection while
   SQLite claimed canonical authority.
7. Porting the TypeScript Thread domain and digest algorithms to Python would
   duplicate roughly 1,900 lines of mature semantics and risk rejecting the
   daemon's own Markdown as tampered.

The smaller design makes those failure classes unreachable instead of adding
machinery to manage them.

## Existing contracts preserved

- Tool names remain `minni_thread_*`.
- Frozen artifact names remain `plan_id`, `plan-*`, `plan_*` frontmatter, and
  `_active_plan.json`.
- Existing digest, evidence, dependency, replan, history, restore, activation,
  and compact-injection behavior remains.
- `minni_ping_agent_*` remains a bounded peer-consultation contract, not a
  scheduler event channel.
- Recalled content and worker evidence remain attributed data, never
  instruction.

## Architecture

```text
Orchestrating agent
  | create / assign / replan / contract / expand
  v
Thread MCP tools
  |
Cross-process Thread lock
  |
  +-- canonical plan-*.md state
  +-- ordered plan-*.log.md events
  +-- private .runtime/thread-claims/ secrets
  |
  +--> Team adapter ----> host workers
  +--> Research adapter -> research agents
  +--> Bulk adapter ----> bounded workers
  |
  +--> minni_thread_events(since_seq)
           |
           +--> orchestrator polling / hook fallback
           +--> later daemon relay and verified host wake
```

## Phase boundaries

### Phase 1 — Safe shared Thread core

Implement only:

- cross-process mutation lock
- slice assignment and claim metadata
- private retrievable claim secrets
- slice-scoped worker updates
- ordered journal events with cursor reads
- expansion/contraction proposals
- concurrency and compatibility tests

Do not add SQLite Thread tables, Python Thread domain code, Team integration,
or host wake delivery.

### Phase 2 — Execution adapters

- Team Mode accepts a `plan_id`.
- Ready slices replace Team's synthetic star ledger.
- Worker hydration carries one slice, dependency evidence, claim token, and
  reporting contract.
- Real host workers exercise fan-out/fan-in.

### Phase 3 — Durable notification relay

- The daemon stores delivery cursors or notifications, not graph state.
- Journal sequence remains the recoverable source.
- Host adapters attempt immediate wake only where wet-tested.
- SessionStart/UserPromptSubmit provide the fallback.

Cross-vault shared Thread authority is a separate future design. It is not
smuggled into this implementation.

## Cross-process mutation lock

All operations that can mutate a Thread acquire one lock keyed by canonical
vault path and `plan_id` before reading the note:

```text
acquire lock
  rehydrate latest note
  validate operation against latest state
  apply one mutation
  persist note
  append event carrying resulting rev
release lock
```

Use an atomic lock directory under
`<vault>/.runtime/thread-locks/<plan_id>.lock/`, not an in-memory mutex.
Directory creation is atomic across processes on supported local filesystems.

The lock record contains owner PID, process-start marker, operation id, and
acquired timestamp. Acquisition has:

- bounded wait
- typed `thread_busy` failure after timeout
- stale-lock recovery only when the recorded local PID is no longer alive and
  the lock exceeds the stale threshold
- audit/event record when recovery occurs

No network filesystem support is claimed.

The note and journal are separate files, so a process can still crash between
them. Every event carries the resulting note revision. On the next locked
operation, reconciliation compares note revision with the newest journal
revision:

- note ahead: append `state.recovered` for the already-durable state
- journal ahead: reject mutation as `thread_inconsistent`; require restore or
  explicit repair

This makes a crash visible and recoverable without introducing another source
of truth.

## Slice additions

Preserve existing fields and add:

```ts
requirements?: string[];
assigned_to?: string;
assignment_profile?: string;
generation: number;
attempt: number;
claim?: {
  claim_id: string;
  worker_agent_id: string;
  claimed_at: string;
  expires_at: string;
};
proposals?: StructuralProposal[];
```

Claim tokens are never written to plan frontmatter, journal payloads, compact
views, audit logs, or indexed Markdown.

## Claim secrets and retry

An orchestrator assigns a slice, then the worker claims it.

The claim operation requires a non-empty idempotency key. Under the Thread
lock it:

1. reloads the current Thread;
2. lazily expires an old claim when `expires_at <= now`;
3. verifies the slice is ready, assigned to the requested worker label, and
   unclaimed;
4. creates a random claim token;
5. stores the token in a mode-0600 private JSON envelope under
   `<vault>/.runtime/thread-claims/<plan_id>/<claim_id>.json`;
6. writes claim metadata (without token) into the slice;
7. records the idempotency key and response in the private envelope;
8. persists the Thread and event before returning the token.

A retry with the same idempotency key returns the same token and response.
`ON CONFLICT DO UPDATE` behavior is forbidden.

If the private envelope cannot be persisted, the claim does not commit.
If the note commits but the response is lost, the envelope makes the same
response retrievable.

Expiry and reassignment delete the private secret, clear the claim, increment
`generation`, and increment `attempt`. A completion after expiry is rejected
even when no sweeper has run; expiry is checked on the acceptance path using
one injected `now`.

## Authority model

### Enforced by Thread code

- A worker update requires the current claim token.
- The token resolves to one `plan_id`, `slice_id`, `generation`, and worker
  label.
- The worker update can change only that slice's progress, evidence, block
  reason, scar, or structural proposal.
- Stale, expired, revoked, wrong-slice, or wrong-generation tokens fail.
- Structural mutation tools never accept a worker claim token as authority.

### Enforced by host adapter

Current same-platform subagents inherit the same `EffectivePrincipal` as their
orchestrator. Minni cannot honestly distinguish them at daemon provenance
level. Therefore adapters must expose only worker-update/reporting tools to
workers and retain create/replan/assign/restore tools for the coordinator.

This is a host tool-scope boundary, not a daemon identity claim. If a host
cannot scope tools, the restriction is advisory on that host and must be
reported as such.

Per-session delegated principals may be designed later; they are not required
to make slice-token updates safe.

## Worker update protocol

The worker receives:

- Thread goal and hard constraints
- its slice id, generation, gate, and claim token
- completed dependency evidence references
- bounded relevant recall
- allowed mutation schema

Allowed actions:

- `start`
- `progress`
- `block`
- `scar`
- `propose_structure`
- `complete`

`complete` requires substantive evidence and all existing evidence-gate
semantics. It clears and deletes the claim secret, increments the Thread
revision through normal persistence, and may unlock dependent slices.

The worker cannot mutate dependencies, gates, assignments, constraints,
another slice, or the Thread status directly.

## Expansion and contraction

Workers return attributed proposals:

```ts
type StructuralProposal =
  | { kind: "expand"; reason: string; slices: ProposedSlice[] }
  | { kind: "split"; reason: string; slices: ProposedSlice[] }
  | { kind: "contract"; reason: string; slice_ids: string[] };
```

Only the orchestrator applies a proposal through existing replan/delta
semantics.

- Expansion adds branches or joins.
- Contraction supersedes; it never deletes.
- Replanning a claimed slice revokes its claim secret and increments
  generation.
- A late result is retained as attributed non-completing evidence.

## Ordered events

Reuse `<plan_id>.log.md` as the durable event source.

Each JSON event gains:

```ts
seq: number;
rev: number;
event_id: string;
idempotency_key: string;
actor: string;
```

The Thread lock serializes sequence allocation and event append. Sequence is
monotonic per Thread.

Required event kinds:

- `slice.assigned`
- `slice.claimed`
- `slice.started`
- `slice.progressed`
- `slice.completed`
- `slice.blocked`
- `slice.lease_expired`
- `slice.claim_revoked`
- `structure.proposed`
- existing `replan`, `status_changed`, `scar_added`, and `restored`
- `ready.changed`
- `thread.attention_required`
- `thread.completed`
- `state.recovered`
- `journal_truncated` / `cursor_gap` (same payload: `last_dropped_seq` + `first_kept_seq`; unmarked holes fail closed)

Routine events are queryable. Attention events are the later notification
input.

`minni_thread_events(plan_id?, since_seq?, limit?)` returns ordered events and
`next_seq`. The client owns its cursor in Phase 1. No subscription or ack table
is introduced.

## Ready-set semantics

A slice is ready when:

- it is `pending` or `blocked` as explicitly reopened;
- every `depends_on` slice is `done` or `superseded`;
- it has no non-expired claim;
- it is not itself superseded.

Ready-set computation accepts an injected `now`. Claim expiry is applied under
the Thread lock before returning ready slices.

`ready.changed` is emitted only when the set changes. Its summary contains
slice ids and titles, never raw worker evidence.

Model recommendation is a separate adapter operation. It must not make
ready-set computation availability-dependent.

## Model recommendation

Slices name capability or perspective requirements, not brands.
Agent profiles provide explicit strengths, runtime, permissions, model tier,
and current availability. Minni returns ranked candidates with reasons; the
orchestrator decides.

Historical quality is excluded until real outcomes support a defined,
non-gameable metric.

Implementation-run model assignments are recorded in the implementation plan,
not this durable architecture spec.

## Team integration

Phase 2 removes the synthetic Team dependency ledger as a decision input.
`minni_team_runtime(plan_id)`:

1. reads Thread ready slices;
2. obtains model recommendations separately;
3. asks the orchestrator to choose assignments;
4. creates bounded worker hydration packets;
5. dispatches through the host;
6. routes worker reports through slice-token updates.

Compatibility callers without `plan_id` may create one Thread and return its
id. They do not maintain an independent durable ledger.

## Notification delivery

Phase 3 follows durable-source plus delivery-cache semantics:

1. `ready.changed` or another attention event lands in the journal.
2. The adapter or daemon relay reads after its stored cursor.
3. A verified host wake/resume path attempts immediate delivery.
4. Failure leaves the cursor behind the event.
5. SessionStart/UserPromptSubmit reads pending attention events as fallback.
6. Delivery advances the relay cursor monotonically.

The daemon does not store or mutate Thread graph state. Rebuilding the delivery
queue from journal sequence is always possible.

Notifications carry concise attributed state deltas, not raw evidence and not
instructions.

## Security and privacy

- Thread access remains bounded by the owner vault.
- Worker labels are audit metadata, not daemon principals.
- Claim tokens are random, private, time-bounded, and excluded from indexed
  surfaces.
- Structural tools require the normal orchestrator tool exposure and existing
  shared gate; they never accept worker tokens.
- Worker evidence is fenced before model-facing notification or hydration.
- Cross-vault Thread reads and writes are out of scope.
- No token, hash, private envelope path, or raw evidence appears in events.

## Strict implementation gates

No child implementation task uses `inherit`; exact assignments and effort
levels live in the implementation plan. At most three children run
concurrently.

### G0 — Baseline and corrected contract

GO only when:

- `make check` passes;
- current Thread compatibility behavior is characterized;
- this V2 spec has no unresolved HIGH review finding.

### G1 — Shared Thread core

GO only when:

- concurrent independent updates preserve both results;
- lock timeout and stale-lock recovery are deterministic and tested;
- same-key claim retry returns the same token;
- expired/revoked claims cannot complete;
- expiry versus completion has one winner;
- worker update cannot change another slice or graph topology;
- note/journal crash gaps are detected;
- event sequence is monotonic under concurrent callers;
- current Thread suites and full plugin suite pass.

NO-GO on lost update, leaked claim token, silent stale-lock theft, duplicate
event, compatibility regression, or an unenforced claim in documentation.

### G2 — Team projection

GO only when:

- Team consumes ready Thread slices;
- no synthetic ledger drives scheduling;
- host tool scoping is verified or honestly marked advisory;
- real differently profiled workers complete a fan-out/fan-in Thread;
- expansion and contraction proposals preserve attribution and history.

### G3 — Notification relay

GO only when:

- journal events rebuild relay state;
- failed wake falls back without event loss;
- cursors advance monotonically;
- a real orchestrator receives an actionable event while continuing unrelated
  conversation;
- each host is reported as immediate, deferred, or unsupported from wet
  evidence.

## Required G1 tests

1. Two OS processes update independent slices from the same starting revision;
   both persist.
2. Lock timeout returns `thread_busy` without mutation.
3. Dead local PID plus stale threshold permits one audited recovery.
4. Live PID lock is never stolen because of age alone.
5. Claim response is discarded; same idempotency retry returns the same token.
6. Two simultaneous claims produce one winner.
7. Completion after expiry is rejected without a sweeper.
8. Expiry and completion race yields exactly one durable outcome.
9. Reassign/replan invalidates the old token by generation.
10. Worker update with a valid token cannot alter a sibling slice,
    dependencies, gate, assignment, constraints, or status.
11. Event sequence remains unique and ordered under concurrent callers.
12. Note revision ahead of journal produces `state.recovered`.
13. Journal revision ahead of note blocks mutation as inconsistent.
14. Claim token and secret path never appear in note, journal, compact view,
    audit, or vault search results.
15. Existing create/update/replan/history/restore/active-pointer tests remain
    green.

## Success criteria

An orchestrator can maintain one durable Thread, assign independent slices to
differently capable workers, continue other work, accept worker-proposed graph
expansion or contraction, and consume ordered actionable events without lost
updates or a second source of truth.
