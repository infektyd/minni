# Thread Orchestration Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing vault-backed Thread safe for slice-scoped parallel workers by adding cross-process mutation locking, retrievable claims, ordered events, and worker-only updates.

**Architecture:** The owner vault's `plan-*.md` remains canonical. Every mutation reloads and writes under one cross-process lock; private claim tokens live under `.runtime/`, while the existing append-only journal gains ordered scheduler events. Team adapters and daemon notification delivery remain later phases.

**Tech Stack:** TypeScript 6, Node.js 20+, MCP SDK, Zod 4, Node built-in test runner, filesystem-atomic rename/mkdir primitives.

## Global Constraints

- Preserve `minni_thread_*` names and frozen `plan_id`, `plan-*`, `plan_*`, and `_active_plan.json` artifacts.
- Do not add Python Thread domain code or SQLite Thread tables in Phase 1.
- Claim tokens must never enter Markdown, journals, audits, compact views, or vault search.
- Same-platform worker restrictions are enforced by claim scope plus host tool exposure; do not describe workers as distinct daemon principals.
- Structural changes remain orchestrator-only and use existing replan/supersession behavior.
- All mutation steps follow TDD and end in a dedicated commit.
- Every implementation child uses the exact non-`inherit` model and effort listed for its task.
- At most one implementation child runs at once because these tasks modify shared Thread files.
- G1 is NO-GO on any lost update, token leak, unsafe stale-lock theft, duplicate ordered event, or existing Thread regression.
- From Task 3 review onward, child work and review use only explicitly selected
  GPT-5.6 Sol or GPT-5.6 Luna variants. Each task receives an adversarial review;
  Critical or Important findings trigger a fix and re-review loop until clean.

---

## File Structure

### New focused modules

- `plugins/minni/src/thread-lock.ts` — cross-process lock acquisition, stale recovery, and ownership-safe release.
- `plugins/minni/src/thread-events.ts` — ordered scheduler-event allocation, cursor reads, and note/journal revision reconciliation.
- `plugins/minni/src/thread-claims.ts` — mode-0600 private claim envelopes and same-key response replay.
- `plugins/minni/src/thread-worker.ts` — ready-set, assignment, claim, and worker-update orchestration over existing `plan.ts`.
- `plugins/minni/tests/thread-lock.test.mjs` — real cross-process lock tests.
- `plugins/minni/tests/thread-worker.test.mjs` — claim, expiry, worker scope, race, and token-leak tests.
- `plugins/minni/tests/thread-events.test.mjs` — sequence, cursor, and crash-gap tests.
- `plugins/minni/tests/fixtures/thread-lock-worker.mjs` — child-process lock fixture.

### Existing modules changed

- `plugins/minni/src/plan.ts` — slice metadata, digest v3, backward-compatible reads, pure worker mutation helpers.
- `plugins/minni/src/server.ts` — typed MCP tools and shared-gate calls for ready/assign/claim/worker-update/events.
- `plugins/minni/tests/plan.test.mjs` — digest-v3 and compatibility characterization.
- `plugins/minni/tests/shared-gate-coverage.test.mjs` — new tool-to-gate mappings.
- `plugins/minni/skills/minni/SKILL.md` — agent-facing Phase-1 worker protocol and honest limits.
- `plugins/minni/commands/threads.md` — orchestrator/worker usage contract.

---

### Task 1: Cross-process Thread mutation lock

**Execution assignment:** GPT-5.6 Sol High, high effort.

**Files:**
- Create: `plugins/minni/src/thread-lock.ts`
- Create: `plugins/minni/tests/thread-lock.test.mjs`
- Create: `plugins/minni/tests/fixtures/thread-lock-worker.mjs`

**Interfaces:**
- Produces:
  ```ts
  export class ThreadBusyError extends Error {
    readonly code: "THREAD_BUSY";
    readonly owner?: ThreadLockOwner;
  }

  export interface ThreadLockOwner {
    pid: number;
    operationId: string;
    acquiredAt: string;
  }

  export interface ThreadLockOptions {
    waitMs?: number;
    staleMs?: number;
    pollMs?: number;
    now?: () => Date;
    isProcessAlive?: (pid: number) => boolean;
  }

  export function withThreadLock<T>(
    vaultPath: string,
    planId: string,
    operationId: string,
    fn: () => Promise<T>,
    options?: ThreadLockOptions,
  ): Promise<T>;
  ```
- Consumes: `writeFileAtomic` conventions from `plugins/minni/src/vault.ts`; no plan mutation yet.

- [ ] **Step 1: Write failing lock tests**

Add tests that use a real temporary vault and real child processes:

```js
test("withThreadLock serializes two OS processes for one plan", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "minni-thread-lock-"));
  const log = path.join(root, "critical.log");
  const worker = new URL("./fixtures/thread-lock-worker.mjs", import.meta.url);
  const args = [fileURLToPath(worker), root, "plan-shared", log];
  const [a, b] = await Promise.all([
    execFileAsync(process.execPath, args),
    execFileAsync(process.execPath, args),
  ]);
  assert.equal(a.stderr, "");
  assert.equal(b.stderr, "");
  const intervals = (await readFile(log, "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(intervals.length, 2);
  const [first, second] = intervals.sort((x, y) => x.entered - y.entered);
  assert.ok(first.left <= second.entered, JSON.stringify(intervals));
});

test("withThreadLock never steals a live owner's old lock", async () => {
  await seedOwner(root, "plan-live", {
    pid: process.pid,
    operationId: "live-op",
    acquiredAt: "2026-01-01T00:00:00.000Z",
  });
  await assert.rejects(
    withThreadLock(root, "plan-live", "contender", async () => undefined, {
      waitMs: 40,
      staleMs: 1,
      pollMs: 5,
      isProcessAlive: () => true,
    }),
    (error) => error?.code === "THREAD_BUSY",
  );
});

test("withThreadLock recovers only a stale dead-owner directory", async () => {
  await seedOwner(root, "plan-dead", {
    pid: 999999,
    operationId: "dead-op",
    acquiredAt: "2026-01-01T00:00:00.000Z",
  });
  let entered = false;
  await withThreadLock(root, "plan-dead", "recovery", async () => {
    entered = true;
  }, {
    staleMs: 1,
    isProcessAlive: () => false,
  });
  assert.equal(entered, true);
});
```

The fixture must append `{entered,left,pid}` only while inside
`withThreadLock`, sleep for 75ms, then exit.

- [ ] **Step 2: Run tests and verify the missing module failure**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs tests/thread-lock.test.mjs
```

Expected: FAIL because `dist/thread-lock.js` does not exist.

- [ ] **Step 3: Implement ownership-safe directory locking**

Implement:

```ts
import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

const DEFAULT_WAIT_MS = 5_000;
const DEFAULT_STALE_MS = 120_000;
const DEFAULT_POLL_MS = 25;

function lockKey(planId: string): string {
  return createHash("sha256").update(planId).digest("hex").slice(0, 32);
}

function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}
```

`withThreadLock` must:

1. `mkdir(lockDir)` atomically.
2. Write `owner.json` with mode `0o600`.
3. On `EEXIST`, read owner and directory age.
4. Recover only when age exceeds `staleMs` and the owner PID is not alive.
5. Recover by `rename(lockDir, uniqueQuarantineDir)`, never by deleting the
   contested path directly.
6. On release, reread `owner.json` and remove only when `operationId` still
   matches.
7. Throw `ThreadBusyError` at the bounded deadline.

- [ ] **Step 4: Run focused and existing Thread tests**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs \
  tests/thread-lock.test.mjs \
  tests/plan.test.mjs \
  tests/plan-integrity-122.test.mjs
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/minni/src/thread-lock.ts \
  plugins/minni/tests/thread-lock.test.mjs \
  plugins/minni/tests/fixtures/thread-lock-worker.mjs
git commit -m "feat(threads): serialize cross-process mutations"
```

**Gate T1:** GO only if two real OS processes cannot overlap, a live lock is
never stolen, and stale recovery uses atomic rename.

---

### Task 2: Slice metadata and digest-v3 compatibility

**Execution assignment:** completed before the current model restriction; do
not redispatch this task.

**Files:**
- Modify: `plugins/minni/src/plan.ts:15-218,1320-1434`
- Modify: `plugins/minni/tests/plan.test.mjs`

**Interfaces:**
- Produces:
  ```ts
  export interface ThreadClaimRef {
    claim_id: string;
    worker_agent_id: string;
    claimed_at: string;
    expires_at: string;
  }

  export type StructuralProposal =
    | { kind: "expand" | "split"; reason: string; slices: CreatePlanInput["slices"] }
    | { kind: "contract"; reason: string; slice_ids: string[] };
  ```
- `PlanSlice` gains `requirements`, `assigned_to`, `assignment_profile`,
  `generation`, `attempt`, `claim`, and `proposals`.
- `PLAN_DIGEST_VERSION` becomes `3`; v1 and v2 remain readable.

- [ ] **Step 1: Add failing digest and compatibility tests**

```js
test("digest v3 changes for assignment, generation, claim metadata, and proposals", () => {
  const base = makePlan();
  const variants = [
    { assigned_to: "worker-a" },
    { generation: 2 },
    { attempt: 1 },
    { claim: {
      claim_id: "claim-a",
      worker_agent_id: "worker-a",
      claimed_at: "2026-08-18T00:00:00.000Z",
      expires_at: "2026-08-18T00:10:00.000Z",
    }},
    { proposals: [{ kind: "contract", reason: "enough evidence", slice_ids: ["b"] }] },
  ];
  for (const extra of variants) {
    const changed = {
      ...base,
      slices: [{ ...base.slices[0], ...extra }],
    };
    assert.notEqual(computePlanDigest(base), computePlanDigest(changed));
  }
});

test("rehydratePlan reads declared v2 without write-on-read upgrade", async () => {
  const fixture = await writeDeclaredV2Plan();
  const before = await readFile(fixture.notePath, "utf8");
  const plan = await rehydratePlan(fixture.notePath);
  const after = await readFile(fixture.notePath, "utf8");
  assert.equal(plan.plan_id, fixture.plan.plan_id);
  assert.equal(after, before);
});
```

Also preserve the existing newer-version rejection test.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs tests/plan.test.mjs
```

Expected: FAIL because v2 ignores the new fields and read re-persistence
changes the file.

- [ ] **Step 3: Implement digest v3 and no-write backward reads**

Add `computePlanDigestHexV3` using `stableStringify` over every existing v2
field plus the new slice fields. Register:

```ts
export const PLAN_DIGEST_VERSION = 3;

const PLAN_DIGEST_ALGORITHMS: Record<number, (plan: PlanArtifact) => string> = {
  1: computePlanDigestV1,
  2: computePlanDigestHexV2,
  3: computePlanDigestHexV3,
};

export function computePlanDigest(plan: PlanArtifact): string {
  return computePlanDigestHexV3(plan);
}
```

When a declared older algorithm validates, return the plan without
`persistPlan` side effects. The next explicit mutation naturally persists v3.
Continue normalizing interim tagged digest strings only when the declared
algorithm is current.

Default missing `generation` and `attempt` to `0` in pure helpers without
rewriting old files on read.

- [ ] **Step 4: Run focused tests**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs \
  tests/plan.test.mjs \
  tests/plan-integrity-122.test.mjs \
  tests/sessionstart-shelf-drift.test.mjs
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/minni/src/plan.ts plugins/minni/tests/plan.test.mjs
git commit -m "feat(threads): version worker slice metadata"
```

**Gate T2:** GO only if every new durable field affects v3, declared v1/v2
notes remain readable without mutation, and newer notes still fail closed.

---

### Task 3: Private idempotent claim store

**Execution assignment:** GPT-5.6 Sol XHigh, xhigh effort.

**Files:**
- Create: `plugins/minni/src/thread-claims.ts`
- Create: `plugins/minni/tests/thread-worker.test.mjs`

**Interfaces:**
- Produces:
  ```ts
  export interface ClaimSecretEnvelope {
    schema: "minni.thread-claim.v1";
    plan_id: string;
    slice_id: string;
    claim_id: string;
    generation: number;
    worker_agent_id: string;
    idempotency_key: string;
    token: string;
    expires_at: string;
    response: ThreadClaimResponse;
  }

  export interface ThreadClaimResponse {
    plan_id: string;
    slice_id: string;
    claim_id: string;
    generation: number;
    worker_agent_id: string;
    token: string;
    expires_at: string;
    rev: number;
  }

  export interface StoredClaimSecret {
    envelope: ClaimSecretEnvelope;
    filePath: string;
  }

  export function readClaimByIdempotency(
    vaultPath: string,
    planId: string,
    sliceId: string,
    generation: number,
    idempotencyKey: string,
  ): Promise<ClaimSecretEnvelope | undefined>;

  export function createClaimSecret(...): Promise<StoredClaimSecret>;
  export function verifyClaimToken(...): Promise<StoredClaimSecret>;
  export function deleteClaimSecret(...): Promise<void>;
  ```
- Secret paths are SHA-256-derived segments under
  `<vault>/.runtime/thread-claims/`.

- [ ] **Step 1: Write failing private-store tests**

```js
test("same claim idempotency key returns the same token", async () => {
  const first = await createClaimSecret(input);
  const second = await createClaimSecret(input);
  assert.equal(second.envelope.token, first.envelope.token);
  assert.equal(second.envelope.claim_id, first.envelope.claim_id);
});

test("claim envelope is private and outside indexed markdown", async () => {
  const claim = await createClaimSecret(input);
  const mode = (await stat(claim.filePath)).mode & 0o777;
  assert.equal(mode, 0o600);
  assert.match(claim.filePath, /[\/\\]\.runtime[\/\\]thread-claims[\/\\]/);
  assert.equal(claim.filePath.endsWith(".md"), false);
});

test("verifyClaimToken rejects wrong and expired tokens", async () => {
  const claim = await createClaimSecret(input);
  await assert.rejects(
    verifyClaimToken({ ...input, token: "wrong", now: beforeExpiry }),
    /claim token mismatch/,
  );
  await assert.rejects(
    verifyClaimToken({ ...input, token: claim.envelope.token, now: afterExpiry }),
    /claim expired/,
  );
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs tests/thread-worker.test.mjs
```

Expected: FAIL because `dist/thread-claims.js` does not exist.

- [ ] **Step 3: Implement atomic private envelopes**

Use `randomBytes(32).toString("base64url")` for tokens and
`timingSafeEqual` on SHA-256 token digests. Derive `claim_id` from:

```ts
createHash("sha256")
  .update(stableStringify({
    plan_id,
    slice_id,
    generation,
    idempotency_key,
  }))
  .digest("hex")
  .slice(0, 32);
```

Write with mode `0o600` to a unique temporary file, then atomic rename.
If an envelope already exists, validate that every identity field matches and
return the stored response and token unchanged.

Reject empty idempotency keys and path/metadata mismatches. Never expose the
secret path in model-facing return values.

- [ ] **Step 4: Run focused tests**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs tests/thread-worker.test.mjs
```

Expected: all private-store tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/minni/src/thread-claims.ts \
  plugins/minni/tests/thread-worker.test.mjs
git commit -m "feat(threads): persist retryable private claims"
```

**Gate T3:** GO only if response-loss retry returns the identical token and no
secret reaches an indexed or group-readable surface.

---

### Task 4: Slice assignment, ready set, and claimed worker updates

**Execution assignment:** GPT-5.6 Sol XHigh, xhigh effort.

**Files:**
- Create: `plugins/minni/src/thread-worker.ts`
- Modify: `plugins/minni/src/plan.ts`
- Modify: `plugins/minni/tests/thread-worker.test.mjs`

**Interfaces:**
- Produces:
  ```ts
  export type WorkerUpdateAction =
    | { action: "start" }
    | { action: "progress"; evidence: string }
    | { action: "block"; evidence: string }
    | { action: "scar"; kind: ScarTissueEntry["kind"]; signal: string; resolution?: string }
    | { action: "propose_structure"; proposal: StructuralProposal }
    | { action: "complete"; evidence: string };

  export function readySlices(plan: PlanArtifact, now: Date): PlanSlice[];
  export interface ThreadMutationResult {
    plan: PlanArtifact;
    slice: PlanSlice;
    ready_before: string[];
    ready_after: string[];
  }
  export function assignSlice(...): Promise<ThreadMutationResult>;
  export function claimSlice(...): Promise<ThreadClaimResponse>;
  export function updateClaimedSlice(...): Promise<ThreadMutationResult>;
  ```
- Consumes `withThreadLock`, claim-store functions, `rehydratePlan`,
  `persistPlan`, existing evidence/dependency helpers.

- [ ] **Step 1: Add failing worker-domain and concurrency tests**

```js
test("two processes completing independent slices preserve both results", async () => {
  const fixture = await createTwoSliceThread();
  const [claimA, claimB] = await Promise.all([
    claimSlice({ ...fixture, sliceId: "a", workerAgentId: "worker-a", idempotencyKey: "claim-a" }),
    claimSlice({ ...fixture, sliceId: "b", workerAgentId: "worker-b", idempotencyKey: "claim-b" }),
  ]);
  await Promise.all([
    runWorkerProcess({ ...fixture, sliceId: "a", token: claimA.token, evidence: "A verified in test output" }),
    runWorkerProcess({ ...fixture, sliceId: "b", token: claimB.token, evidence: "B verified in test output" }),
  ]);
  const final = await rehydratePlan(fixture.notePath);
  assert.equal(final.slices.find((s) => s.id === "a").status, "done");
  assert.equal(final.slices.find((s) => s.id === "b").status, "done");
});

test("completion after expiry is rejected without a sweeper", async () => {
  const claim = await claimSlice({ ...input, now: at("12:00"), ttlSeconds: 60 });
  await assert.rejects(
    updateClaimedSlice({
      ...input,
      token: claim.token,
      action: { action: "complete", evidence: "verified by test output" },
      now: at("12:02"),
    }),
    /claim expired/,
  );
});

test("worker token cannot mutate sibling or topology", async () => {
  const claim = await claimSlice(input);
  await assert.rejects(
    updateClaimedSlice({ ...input, sliceId: "sibling", token: claim.token, action: { action: "start" } }),
    /claim scope mismatch/,
  );
});
```

Add a barrier-based expiry-versus-complete race and simultaneous double-claim
test. Exactly one outcome may commit.

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs tests/thread-worker.test.mjs
```

Expected: FAIL because `thread-worker` operations do not exist.

- [ ] **Step 3: Implement locked mutation flow**

Every operation must call `withThreadLock` before `rehydratePlan`.

`assignSlice`:

- requires a structurally ready or pending slice;
- revokes and deletes an existing claim;
- sets `assigned_to` and optional profile;
- increments generation on reassignment;
- persists under lock.

`claimSlice`:

- lazily expires old claim using injected `now`;
- returns the existing private response for the same idempotency key;
- verifies dependencies are resolved and `assigned_to` matches worker label;
- creates the private envelope before committing claim metadata;
- deletes the envelope if note persistence fails;
- increments `attempt` only for a new claim.

`updateClaimedSlice`:

- verifies private token, plan/slice/generation/worker scope, and expiry under
  the lock;
- applies only the discriminated action;
- reuses `updateSlice` for block/complete evidence semantics;
- deletes the private claim on completion;
- compares ready sets before/after.

Use a strict action switch with no object spreading from caller input.

- [ ] **Step 4: Run focused race and compatibility tests**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs \
  tests/thread-lock.test.mjs \
  tests/thread-worker.test.mjs \
  tests/plan.test.mjs \
  tests/plan-integrity-122.test.mjs
```

Expected: all pass in five consecutive runs:

```bash
for i in 1 2 3 4 5; do
  node --test --import ./tests/setup-env.mjs \
    tests/thread-lock.test.mjs tests/thread-worker.test.mjs || exit 1
done
```

- [ ] **Step 5: Commit**

```bash
git add plugins/minni/src/thread-worker.ts \
  plugins/minni/src/plan.ts \
  plugins/minni/tests/thread-worker.test.mjs
git commit -m "feat(threads): scope workers to claimed slices"
```

**Gate T4:** GO only if independent process updates both survive, claim expiry
has one winner, and no worker input can alter topology or a sibling slice.

---

### Task 5: Ordered scheduler events and cursor reads

**Execution assignment:** GPT-5.6 Sol High, high effort.

**Files:**
- Create: `plugins/minni/src/thread-events.ts`
- Create: `plugins/minni/tests/thread-events.test.mjs`
- Modify: `plugins/minni/src/thread-worker.ts`
- Modify: `plugins/minni/src/plan.ts`

**Interfaces:**
- Produces:
  ```ts
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

  export function appendOrderedThreadEvent(...): Promise<OrderedThreadEvent>;
  export function readThreadEvents(
    journalPath: string,
    sinceSeq?: number,
    limit?: number,
  ): Promise<{ events: OrderedThreadEvent[]; next_seq: number }>;
  export function reconcileThreadJournal(...): Promise<"ok" | "recovered">;
  ```

`appendOrderedThreadEvent` and `reconcileThreadJournal` require the caller to
already hold `withThreadLock`; they must not acquire it recursively.

- [ ] **Step 1: Write failing ordered-event tests**

```js
test("ordered events allocate unique monotonic seq under concurrent callers", async () => {
  const fixture = await createThread();
  await Promise.all([...Array(12)].map((_, index) =>
    appendEventInChildUnderThreadLock(fixture, `event-${index}`)
  ));
  const { events } = await readThreadEvents(fixture.journalPath, 0, 100);
  assert.deepEqual(events.map((event) => event.seq), [...Array(12)].map((_, i) => i + 1));
  assert.equal(new Set(events.map((event) => event.event_id)).size, 12);
});

test("cursor read excludes seq at or below since_seq", async () => {
  const result = await readThreadEvents(journalPath, 2, 20);
  assert.deepEqual(result.events.map((event) => event.seq), [3, 4]);
  assert.equal(result.next_seq, 4);
});

test("note ahead of journal appends state.recovered", async () => {
  await seedNoteAtRev(5);
  await seedNewestOrderedEventAtRev(4);
  assert.equal(await reconcileThreadJournal(input), "recovered");
  const result = await readThreadEvents(journalPath, 0, 100);
  assert.equal(result.events.at(-1).kind, "state.recovered");
  assert.equal(result.events.at(-1).rev, 5);
});

test("journal ahead of note blocks mutation", async () => {
  await seedNoteAtRev(4);
  await seedNewestOrderedEventAtRev(5);
  await assert.rejects(reconcileThreadJournal(input), /thread_inconsistent/);
});
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs tests/thread-events.test.mjs
```

Expected: FAIL because the event module does not exist.

- [ ] **Step 3: Implement ordered event functions**

Parse existing journal lines and ignore legacy events without numeric `seq`.
While the caller holds `withThreadLock`, allocate:

```ts
const seq = orderedEvents.reduce(
  (highest, event) => Math.max(highest, event.seq),
  0,
) + 1;
```

Derive `event_id` from `plan_id`, `seq`, and `idempotency_key`. Reject duplicate
idempotency keys by returning the existing event. Append with the existing
fsync helper.

Reconciliation compares the note's `rev` with the greatest ordered-event
`rev`. A journal with no ordered events begins at the current note revision and
does not reinterpret legacy history.

`ready.changed` payload contains only slice ids and titles.

- [ ] **Step 4: Wire worker operations to events**

Under the same Thread lock, append one operation event after persistence.
When the ready set changes, append one coalesced `ready.changed` event using a
derived child idempotency key `${operationKey}:ready`.

If event append fails after note persistence, the next locked operation must
run reconciliation before accepting a new mutation.

- [ ] **Step 5: Run event, worker, and plan tests**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs \
  tests/thread-events.test.mjs \
  tests/thread-worker.test.mjs \
  tests/thread-lock.test.mjs \
  tests/plan.test.mjs \
  tests/plan-integrity-122.test.mjs
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/minni/src/thread-events.ts \
  plugins/minni/src/thread-worker.ts \
  plugins/minni/src/plan.ts \
  plugins/minni/tests/thread-events.test.mjs \
  plugins/minni/tests/thread-worker.test.mjs
git commit -m "feat(threads): expose durable ordered events"
```

**Gate T5:** GO only if sequence is stable under real process concurrency,
cursor reads are exact, and note/journal crash gaps cannot remain silent.

---

### Task 6: Typed MCP worker surface

**Execution assignment:** GPT-5.6 Luna High, high effort.

**Files:**
- Modify: `plugins/minni/src/server.ts:1244-1762`
- Modify: `plugins/minni/tests/shared-gate-coverage.test.mjs`
- Create: `plugins/minni/tests/thread-server.test.mjs`

**Interfaces:**
- Produces literal MCP tools:
  - `minni_thread_ready`
  - `minni_thread_assign`
  - `minni_thread_claim`
  - `minni_thread_worker_update`
  - `minni_thread_events`

- [ ] **Step 1: Write failing schema and end-to-end tests**

Add exact shared-gate mappings:

```js
["minni_thread_ready", "plan.ready"],
["minni_thread_assign", "plan.assign"],
["minni_thread_claim", "plan.claim"],
["minni_thread_worker_update", "plan.worker_update"],
["minni_thread_events", "plan.events"],
```

Add MCP tests proving:

```js
const claim = await call("minni_thread_claim", {
  plan_id,
  slice_id: "research",
  worker_agent_id: "worker-a",
  idempotency_key: "claim-research-1",
});
const done = await call("minni_thread_worker_update", {
  plan_id,
  slice_id: "research",
  claim_token: claim.token,
  idempotency_key: "done-research-1",
  action: "complete",
  evidence: "Verified against docs/source-a.md and docs/source-b.md",
});
assert.equal(done.slice.status, "done");
```

Schema tests must prove worker-update exposes no dependency, gate, assignee,
constraint, sibling-slice, force, or replan field.

- [ ] **Step 2: Run and verify missing-tool failures**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs \
  tests/thread-server.test.mjs \
  tests/shared-gate-coverage.test.mjs
```

Expected: FAIL because tools are unregistered.

- [ ] **Step 3: Register strict Zod schemas and handlers**

Every handler:

1. calls `requireSharedGate` with the exact `plan.*` key;
2. pins `vaultPath` and actor to server-side defaults;
3. passes only schema-discriminated fields to `thread-worker`;
4. returns typed error codes without transport fallback;
5. never serializes claim secret paths.

`minni_thread_worker_update` uses a discriminated union by `action`. Do not use
`z.record` or pass the raw request into domain code.

- [ ] **Step 4: Run MCP and full focused suite**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs \
  tests/thread-server.test.mjs \
  tests/shared-gate-coverage.test.mjs \
  tests/thread-events.test.mjs \
  tests/thread-worker.test.mjs \
  tests/thread-lock.test.mjs \
  tests/plan.test.mjs \
  tests/plan-integrity-122.test.mjs
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/minni/src/server.ts \
  plugins/minni/tests/thread-server.test.mjs \
  plugins/minni/tests/shared-gate-coverage.test.mjs
git commit -m "feat(threads): add slice-scoped worker tools"
```

**Gate T6:** GO only if the model-facing schema cannot express a structural
mutation and every tool passes the existing daemon shared gate.

---

### Task 7: Agent-facing workflow documentation and G1 gate

**Execution assignment:** GPT-5.6 Luna High, medium effort for docs audit;
GPT-5.6 Sol XHigh Fast, xhigh effort for adversarial gate review. Run
sequentially.

**Files:**
- Modify: `plugins/minni/skills/minni/SKILL.md:200-258`
- Modify: `plugins/minni/commands/threads.md`
- Modify: `docs/architecture.md:105-120`
- Modify: `docs/concepts.md:165-170`

**Interfaces:**
- Documents the exact separation between orchestrator structural tools,
  worker claim tools, host-enforced tool scoping, and ordered event cursor
  reads.

- [ ] **Step 1: Update docs with implemented behavior only**

Document:

```text
Orchestrator:
  thread_create/replan/assign/ready/events

Worker packet:
  plan_id + slice_id + generation + claim_token
  thread_worker_update only

Honest limit:
  same-platform workers share EffectivePrincipal; structural-tool restriction
  depends on host tool exposure. Claim scope is enforced regardless.
```

Do not mention Team projection or immediate wake as implemented.

- [ ] **Step 2: Run documentation and schema drift checks**

Run:

```bash
cd plugins/minni
npm run build:server
node --test --import ./tests/setup-env.mjs \
  tests/tool-schema-boundary.test.mjs \
  tests/mcp-instructions.test.mjs \
  tests/shared-gate-coverage.test.mjs
cd ../..
git diff --check
```

Expected: all pass and no whitespace errors.

- [ ] **Step 3: Run the strict G1 gate**

Run:

```bash
make check
cd plugins/minni
for i in 1 2 3 4 5; do
  node --test --import ./tests/setup-env.mjs \
    tests/thread-lock.test.mjs \
    tests/thread-worker.test.mjs \
    tests/thread-events.test.mjs \
    tests/thread-server.test.mjs || exit 1
done
```

Expected:

- plugin suite passes;
- scoped Python suite passes unchanged;
- all five race repetitions pass;
- lint/typecheck/build are green.

- [ ] **Step 4: Run adversarial review**

Dispatch GPT-5.6 Sol XHigh Fast with the exact instruction:

```text
Review only Phase-1 Thread changes against
docs/superpowers/specs/2026-08-18-thread-orchestration-v2-design.md.
Attempt to falsify cross-process exclusion, claim replay, expiry single-winner,
worker slice scope, token non-disclosure, ordered sequence, and backward
compatibility. Return HIGH/MEDIUM/LOW findings with reproductions. Any
unresolved HIGH is NO-GO.
```

- [ ] **Step 5: Commit documentation after the gate**

```bash
git add plugins/minni/skills/minni/SKILL.md \
  plugins/minni/commands/threads.md \
  docs/architecture.md \
  docs/concepts.md
git commit -m "docs: wire slice workers into Minni Threads"
```

**Gate G1:** GO only after every Task gate is green, `make check` is green,
five race repetitions are green, token scans find no disclosure, and the
adversarial review has no unresolved HIGH.

---

## Final token-disclosure scan

Run after all implementation tasks:

```bash
rg -n "claim_token|token_hash|thread-claims" \
  plugins/minni/src \
  plugins/minni/skills \
  plugins/minni/commands \
  docs \
  --glob '!docs/superpowers/**'
```

Expected: only private-store implementation, typed input schema, and explicit
non-disclosure documentation references. No renderer, envelope, audit payload,
journal payload, or model-facing response contains a stored secret path or
token except the one-time `minni_thread_claim` response.

## Phase 1 completion

Phase 1 is complete only when the orchestrator can safely assign, claim, and
complete independent Thread slices through real MCP calls, concurrent
processes cannot lose updates, ordered events are cursor-readable, and all
legacy Thread behavior remains green. Team execution and daemon notification
delivery begin in separate plans after G1.
