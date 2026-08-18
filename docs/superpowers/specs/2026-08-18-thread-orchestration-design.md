# Agent-First Thread Orchestration

Status: superseded after G0 review
Date: 2026-08-18  
Scope: make Minni Threads the durable graph shared by orchestrators and temporary workers without turning Minni into a host-specific agent runner

> Superseded by
> `docs/superpowers/specs/2026-08-18-thread-orchestration-v2-design.md`.
> G0 found that moving canonical Thread state into the Python daemon would
> duplicate the TypeScript domain, create a file/SQLite split-brain with hooks,
> and still fail to distinguish same-platform subagents. The replacement keeps
> the existing vault Thread authoritative, fixes its real multi-writer gap
> first, and introduces daemon notification delivery only after ordered Thread
> events are proven.

## 1. Decision

Threads and Team Mode will share one canonical graph but remain separate
modules:

- **Threads own durable work state**: topology, slice state, assignments,
  claims, evidence, scars, revisions, and events.
- **Team / Research / Bulk adapters execute that graph**: they recommend
  workers, create bounded hydration packets, dispatch through the current host,
  and return reports.
- **The host owns model execution**: Minni does not pretend it can wake or spawn
  an agent where a host exposes no such capability.
- **The orchestrating agent owns graph structure**: workers may update their
  assigned slice and propose structural changes, but only the orchestrator may
  expand, contract, or replan the graph.

This is "one graph, multiple execution adapters," not two ledgers and not one
large module.

## 2. Goals

1. Let an orchestrator keep brainstorming or doing other work while workers
   execute independent Thread slices.
2. Let each worker claim and update only its assigned slice.
3. Recompute the ready set after every accepted mutation.
4. Notify the orchestrator when progress reaches an actionable point.
5. Preserve expansion, contraction, evidence, scars, and revision history.
6. Recommend models using explicit capabilities, availability, and prior
   evidence quality while leaving the final assignment to the orchestrator.
7. Keep the system host-neutral and honest about immediate-wake support.
8. Preserve current `minni_thread_*` callers and frozen on-disk `plan_*`
   naming.

## 3. Non-goals

- Minni will not become an LLM execution host.
- Workers will not freely rewrite the graph.
- `minni_ping_agent_*` will not be repurposed for scheduler notifications.
  Ping remains a bounded peer-consultation contract for seeking another
  model's perspective.
- The first release will not learn an opaque routing policy or automatically
  infer model "personality."
- Contraction will never delete historical slices or evidence.
- Immediate wake will not be claimed on a host until a real host-supported
  delivery path is wet-tested.

## 4. Current seam

Today two structures describe related work:

- `PlanArtifact.slices[].depends_on` in `plugins/minni/src/plan.ts`
- `TeamRuntimePacket.taskLedger[].dependencies` in
  `plugins/minni/src/team.ts`

Threads already provide durable slices, dependency enforcement, evidence gates,
replanning, scars, revisions, history, active-pointer injection, and
compaction survival. Team Mode independently creates temporary profiles,
another task ledger, hydration packets, and evidence reports.

The duplicate ledger is the architectural fault. Team Mode should project a
Thread's ready slices into host work; it should not create a second source of
truth.

Thread mutation is currently plugin-local, file-backed read-modify-write.
History rotation has a same-process lock, but the Thread artifact itself has no
cross-process lock or slice claim protocol. Direct multi-writer use would permit
lost updates. The daemon must therefore become the mutation authority before
workers can safely update slices.

## 5. Architecture

```text
User
  |
Orchestrating agent
  | creates/replans/assigns
  v
minni_thread_* MCP tools
  |
Thread daemon service
  |-- canonical graph + transactional mutation
  |-- ready-set computation
  |-- slice claims and lease expiry
  |-- append-only event sequence
  |-- orchestrator subscriptions and acknowledgements
  |-- human-readable vault projection
  |
  +--> Team adapter ----> host subagents
  +--> Research adapter -> research agents
  +--> Bulk adapter ----> bounded worker pool
  |
  +--> durable notification queue
         |
         +--> immediate host wake when verified
         +--> next-turn/session hook fallback
```

The daemon is the source of mutation ordering and event truth. The owner
vault's Markdown Thread remains the inspectable projection. A projection write
failure is visible and retryable; it does not roll back an already-committed
state transition or silently make the vault appear current.

## 6. Canonical state

Use two compact SQLite records rather than prematurely normalizing every slice:

### `thread_states`

- `plan_id` — frozen external identity
- `owner_agent_id`
- `workspace_id`
- `status`
- `rev`
- `graph_json`
- `projection_status`
- `created_at`
- `updated_at`

`graph_json` contains the existing `PlanArtifact` fields plus the additions
below. Threads are expected to remain small enough that loading one graph and
computing its ready set is cheaper and easier to reason about than a generalized
workflow schema.

### `thread_events`

- monotonic `seq`
- `plan_id`
- `event_type`
- `actor_agent_id`
- `slice_id` when applicable
- `idempotency_key`
- `payload_json`
- `created_at`

The unique `(plan_id, idempotency_key)` constraint makes retries safe.

### `thread_subscriptions`

- `plan_id`
- `subscriber_agent_id`
- `last_acked_seq`
- `delivery_preference`
- `updated_at`

This is sufficient for durable at-least-once notification without a separate
message broker.

## 7. Slice model

Preserve the existing fields:

- `id`
- `title`
- `status`
- `gate`
- `depends_on`
- `evidence`
- `superseded_by`

Add:

- `requirements?: string[]` — capability or perspective tags such as
  `research`, `adversarial-review`, `implementation`, or `synthesis`
- `assigned_to?: string` — chosen worker identity
- `assignment_profile?: string` — profile used for the recommendation
- `generation: number` — increments whenever assignment or slice meaning
  changes
- `claim?: { token_hash, worker_agent_id, claimed_at, expires_at }`
- `attempt: number`
- `proposals?: StructuralProposal[]`

The claim token is returned once to the worker; only its hash is stored. A
completion is accepted only when the token, worker identity, generation, and
non-terminal slice state match.

Global Thread revision remains useful for history and projection. Worker
updates do not require the caller to hold the latest global revision because
parallel workers would otherwise conflict merely because one finished first.
The claim generation provides the relevant stale-work guard.

## 8. Authority model

### Orchestrator may

- create and close a Thread
- add, split, supersede, or replace slices
- change dependencies and gates
- choose or override worker recommendations
- assign and revoke claims
- accept or reject worker structural proposals
- force dependency overrides with a recorded reason

### Worker may, for its claimed slice only

- acknowledge and start work
- append bounded progress evidence
- mark the slice blocked with a reason
- add scar tissue
- propose expansion or contraction
- complete the slice with substantive evidence

### Worker may not

- mutate another slice
- change dependencies, constraints, or gates
- assign another worker
- replan the Thread
- mark a replacement generation complete with an old claim
- promote recalled material into instruction or durable learning

## 9. Worker protocol

1. The daemon computes ready slices: non-terminal slices whose dependencies are
   all `done` or `superseded` and which have no live claim.
2. Minni returns ranked worker recommendations with reasons.
3. The orchestrator chooses a recommendation or supplies an override.
4. The adapter dispatches through the host and asks the daemon to claim the
   slice.
5. The worker receives a bounded projection:
   - Thread goal and hard constraints
   - assigned slice, gate, generation, and claim token
   - completed dependency evidence references
   - relevant Minni recall, fenced as evidence
   - permitted operations and reporting contract
6. Worker mutations are idempotent and bound to the claim.
7. Completion commits evidence, clears the claim, recomputes readiness, and
   emits events in the same transaction.

Expired claims return the slice to ready state and emit
`slice.lease_expired` plus `thread.attention_required`.

## 10. Expansion and contraction

Workers submit proposals; the orchestrator applies structure.

### Expansion

- add independent parallel slices
- split an unexpectedly broad slice
- add competing-hypothesis branches
- add verification or synthesis joins
- attach `spawned_from` in the structural event payload

### Contraction

- supersede redundant branches
- stop low-value research after sufficient evidence
- replace duplicate slices with one synthesis slice
- terminate a branch after a decisive result

Contraction never deletes. Claimed slices being replaced have their claims
revoked and generations incremented. Late results remain attributed event
evidence but cannot complete the replacement.

The daemon applies a structural mutation, recomputes the ready set, and emits
one coalesced `ready.changed` event in a single transaction.

## 11. Model recommendation

Model selection is advisory:

1. Slice requirements name capabilities or desired lenses, not brands.
2. Agent profiles provide explicit strengths, runtime, permissions, model tier,
   and availability.
3. Host adapters report rate-limit or unavailable state when the host exposes
   it.
4. Minni may use prior accepted evidence quality as a transparent tie-breaker.
5. The response contains ranked candidates and human-readable reasons.
6. The orchestrator makes the final choice.

The first implementation uses explicit profiles and current availability only.
Historical quality scoring is enabled only after enough real outcomes exist to
define and test a non-gameable metric.

`minni_ping_agent_*` remains available when the orchestrator wants a bounded
second opinion from a differently capable model without assigning it a Thread
slice.

## 12. Notification lifecycle

Scheduler notifications are not pings. They are durable Thread events.

### Attention-producing events

- `ready.changed`
- `slice.blocked`
- `slice.lease_expired`
- `thread.attention_required`
- `thread.completed`

Routine progress events remain queryable but do not wake the orchestrator.

### Delivery

1. The state transition and event commit atomically.
2. The daemon exposes pending events after `last_acked_seq`.
3. A host adapter with a verified wake/resume mechanism attempts immediate
   delivery.
4. Unsupported or failed immediate delivery remains pending.
5. SessionStart and UserPromptSubmit hooks inject a compact pending-event
   summary at the next safe model boundary.
6. The orchestrator acknowledges the highest consumed sequence.

Delivery is at least once. Events carry stable sequence and idempotency keys so
the orchestrator can deduplicate.

An event should be concise:

> Thread `plan-a1b2`: research slices `sources` and `counterevidence` completed;
> synthesis slice `verdict` is now ready.

The notification does not interrupt or override the user's current
conversation. Immediate adapters surface it according to host semantics;
fallback delivery waits for the next safe turn.

## 13. MCP and daemon surface

Existing `minni_thread_*` tools remain compatible and become daemon-backed.

Add narrowly scoped operations:

- `minni_thread_ready` — list ready slices and recommendations
- `minni_thread_assign` — orchestrator assigns a worker
- `minni_thread_claim` — worker receives a claim token
- `minni_thread_worker_update` — worker progress/block/complete/scar
- `minni_thread_propose_structure` — worker expansion/contraction proposal
- `minni_thread_events` — pending events after a sequence
- `minni_thread_ack_events` — acknowledge through a sequence

Do not create a generic family/action dispatcher. Existing Minni convention is
literal registered tool names with typed schemas.

Team Mode changes:

- accepts `plan_id`
- projects ready slices instead of creating a separate authoritative ledger
- returns host dispatch packets and recommendation reasons
- records `runtime_id` as execution metadata in Thread events
- submits evidence through worker-update operations

The compatibility path for callers without `plan_id` may create a Thread once
and return its `plan_id`; it must not maintain an independent ledger afterward.

## 14. Vault projection and migration

1. Existing `wiki/artifacts/plan-*.md` notes remain valid.
2. First daemon-backed access imports a valid current artifact and its history
   into canonical state.
3. Import is idempotent and records the original digest and revision.
4. Existing frozen names (`plan_id`, `plan_*`, `_active_plan.json`) remain.
5. Every committed mutation schedules an atomic Markdown projection.
6. Projection status is surfaced in Thread status and daemon health.
7. A stale or failed projection is never reported as current.
8. Direct file edits after import are treated as drift and require explicit
   reconcile/restore; they never silently overwrite canonical state.

## 15. Failure semantics

- Invalid or stale claim: reject with the current slice generation and status.
- Duplicate mutation: return the previously committed result.
- Worker crash: lease expiry returns the slice to ready.
- Daemon restart: state and unacknowledged events survive in SQLite.
- Projection failure: state remains committed; health and status report stale
  projection; retry is bounded and observable.
- Immediate wake failure: event remains pending for hook or polling delivery.
- Replan of claimed slice: revoke claim, increment generation, preserve late
  evidence as non-completing attributed output.
- Conflicting orchestrator structural edits: compare expected global revision
  and reject one; structural edits are rare and should not be silently merged.

## 16. Security and governance

- Daemon `EffectivePrincipal` remains the identity source.
- Worker assignment and claim are capability-gated.
- Claim tokens are unguessable, stored hashed, scoped to one slice, and
  time-bounded.
- Worker evidence remains attributed data, never instruction.
- Cross-vault writes remain prohibited.
- Host adapters enforce worker tool permissions; the daemon enforces Thread
  mutation scope.
- Model recommendation cannot grant capabilities.
- Structural override, claim revocation, and contraction are audited.

## 17. Implementation workflow with strict gates

Every phase is a separate logical commit. A gate failure is a **NO-GO**: stop,
fix the failed contract, rerun the gate, and do not start the next phase.

No child task uses `inherit`. At most three child agents run concurrently.

### Phase 0 — Contract and baseline

Assignments:

- **Gemini 3.7 Flash High** — quick/medium effort: inventory all Thread, Team,
  hook, daemon, migration, and test seams; return a path/contract matrix.
- **Claude Opus 5 Thinking High** — deep/high effort: challenge the canonical
  state, authority, migration, and event design.
- **Cursor Grok 4.6 High Fast** — adversarial/high effort: concurrency, stale
  claim, identity, and notification threat analysis.

Gate G0:

- Existing Python and plugin suites pass before behavior changes.
- Current Thread create/update/replan/history and Team tests are captured.
- Every host's documented injection/wake capability is classified as verified,
  deferred-only, or unsupported.
- No unresolved HIGH design finding.

NO-GO if the baseline is red, a current behavior lacks a compatibility test, or
an immediate-wake claim lacks wet evidence.

### Phase 1 — Daemon state and pure domain logic

Assignments:

- **GPT-5.6 Sol XHigh** — deep/xhigh effort: SQLite migration, Thread domain
  service, transactional events, claims, ready-set computation.
- **Claude Sonnet 5 Thinking High** — focused/high effort: independent domain
  tests for dependency, expansion/contraction, claim, and idempotency semantics.
- **Cursor Grok 4.6 High Fast** — adversarial/high effort: race and stale-worker
  test review.

Gate G1:

- Migration up and fresh-init paths pass.
- Ready-set computation is deterministic.
- Two parallel workers can complete independent slices without lost updates.
- A stale/revoked claim cannot complete.
- Structural edits use expected global revision.
- Event and state commit atomically.
- Full pre-existing engine suite remains green.

NO-GO on any lost update, duplicate event, identity bypass, migration drift, or
pre-existing regression.

### Phase 2 — Vault compatibility and MCP transition

Assignments:

- **Claude Sonnet 5 Thinking XHigh** — deep/xhigh effort: daemon-backed
  `minni_thread_*`, lazy import, projection, status honesty.
- **GPT-5.6 Sol High** — focused/high effort: compatibility tests and frozen
  naming audit.
- **Gemini 3.7 Flash High** — quick/medium effort: schema and docs/code drift
  scan.

Gate G2:

- Existing tool schemas remain compatible.
- Existing artifacts import byte-safely where no mutation occurs.
- Frozen `plan_*` names and active pointer continue to resolve.
- Projection failure is visible and recoverable.
- Direct-file drift cannot silently overwrite daemon truth.
- Full plugin and engine suites pass.

NO-GO on orphaned plans, silent projection staleness, schema breakage, or
history loss.

### Phase 3 — Worker protocol and Team projection

Assignments:

- **GPT-5.6 Sol XHigh** — deep/xhigh effort: assign/claim/update/proposal APIs.
- **Claude Sonnet 5 Thinking High** — focused/high effort: Team-to-Thread
  projection and bounded hydration.
- **Cursor Grok 4.6 High Fast** — adversarial/high effort: permission and
  cross-slice mutation attempts.

Gate G3:

- Team Mode with `plan_id` creates no second authoritative ledger.
- Worker can mutate only its claimed slice.
- Worker cannot replan or alter dependencies.
- Expansion/contraction proposals preserve attribution.
- Replan revokes affected claims and preserves late evidence without completion.
- Real host workers complete a fan-out/fan-in Thread in at least one available
  host.

NO-GO if validation uses only mocked workers when a real host is available, if
permissions rely only on prompt text, or if any worker can mutate another
slice.

### Phase 4 — Durable notifications and host delivery

Assignments:

- **Claude Opus 5 Thinking High** — deep/high effort: event subscription,
  acknowledgement, coalescing, and conversation-boundary semantics.
- **GPT-5.6 Sol High** — focused/high effort: hook fallback and status/health
  surfaces.
- **Cursor Grok 4.6 High Fast** — adversarial/high effort: duplicate, missed,
  reordered, and wake-failure scenarios.

Gate G4:

- Unacknowledged events survive daemon restart.
- Duplicate delivery is harmless.
- Failed immediate wake falls back to next-turn/session delivery.
- No event is reported delivered before the host accepts it.
- Notifications never overwrite or masquerade as user instructions.
- Each host is reported honestly as immediate, deferred, or unsupported.
- A real orchestrator can continue a conversation while workers run and then
  receive the actionable ready transition.

NO-GO on event loss, false delivery, notification injection with instruction
authority, or unsupported host claims.

### Phase 5 — End-to-end dogfood and release gate

Assignments:

- **Gemini 3.7 Flash High** — quick/medium effort: documentation and setup-path
  audit.
- **Claude Sonnet 5 Thinking XHigh** — deep/xhigh effort: real research workflow
  with expansion, contraction, join, and blocked branch.
- **GPT-5.6 Sol XHigh** — deep/xhigh effort: final integration verification and
  regression triage.

Gate G5:

- Real orchestrator plus at least two differently profiled real workers.
- One worker-proposed expansion accepted.
- One contraction or early-stop branch preserved as superseded history.
- One blocked or expired claim recovered.
- Join slice unlocks only after dependencies resolve.
- Orchestrator receives one coalesced actionable notification.
- Restart/compaction continuity is demonstrated.
- Full Python, plugin, wire/propagation, and relevant host smoke suites pass.
- Docs describe actual support, not intended support.

NO-GO if the demonstration is synthetic where real agents are available, if a
manual filesystem repair is required, or if any support claim exceeds observed
behavior.

## 18. Test matrix

### Pure/domain

- dependency ready-set permutations
- multi-dependency joins
- claim/revoke/expire/reassign
- idempotent retry
- independent parallel completions
- structural expected-revision conflict
- expansion and contraction
- late result after supersession
- event coalescing and acknowledgement

### Persistence

- fresh migration
- upgrade migration
- daemon restart
- artifact lazy import
- projection retry and drift
- bounded event/history retention without loss of unacknowledged events

### Security

- wrong principal
- wrong worker
- wrong/expired claim
- cross-slice mutation
- cross-vault assignment
- model recommendation attempting capability escalation
- evidence containing instruction-like content

### Integration

- current `minni_thread_*` compatibility
- Thread active-pointer injection
- Team projection from ready slices
- real worker fan-out/fan-in
- orchestrator notification during an unrelated conversation
- immediate-delivery failure to hook fallback
- host capability honesty

## 19. Success criteria

The feature is successful when an orchestrating agent can create or expand a
Thread, assign independent slices to differently capable models, continue
working or conversing, and later receive a durable actionable notification
that the next graph point is ready—without workers overwriting one another,
without a second Team ledger, and without Minni claiming host behavior it
cannot verify.
