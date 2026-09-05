# Run a Thread through MCP

A Thread records a plan, dependencies, worker leases, evidence, and mutations in
the agent vault. The host still starts and supervises workers. Creating a plan
or preparing context does not dispatch an agent or execute the plan.

These are **MCP tool names and argument objects**, not shell commands or daemon
JSON-RPC methods. Use a wired Minni MCP server with the required capabilities.
The standalone CLI does not provide a matching Thread claim/update command set.
Calls below write plan state; use a disposable vault when rehearsing them.

## Prepare and create

Call `minni_prepare_task` to assemble evidence for the coordinator:

```json
{"task":"Verify the parser handles an empty file","profile":"standard","useAfm":false,"limit":4}
```

Review the evidence and scope before calling `minni_thread_create`:

```json
{"goal":"Verify empty-file parser behavior","constraints":["Preserve existing valid-file behavior"],"slices":[{"id":"check-empty","title":"Exercise an empty file","gate":"Record the command and observed result"}]}
```

Save the returned `plan_id`. Creation can displace an existing active plan; read
the response warning. Use explicit IDs below so another active-plan change does
not redirect a later call. Replace `<plan_id>` with that returned value and
`<worker_agent_id>` with the intended worker identity in your authorized setup.

## Find, assign, and claim a slice

Call `minni_thread_ready`:

```json
{"plan_id":"<plan_id>"}
```

Check that `check-empty` is ready, then call `minni_thread_assign`:

```json
{"plan_id":"<plan_id>","slice_id":"check-empty","worker_agent_id":"<worker_agent_id>"}
```

Assignment changes plan state and clears any existing claim on that slice. It
does not register a principal, grant capabilities, or start a host worker.
Call `minni_thread_claim` after assignment:

```json
{"plan_id":"<plan_id>","slice_id":"check-empty","worker_agent_id":"<worker_agent_id>","idempotency_key":"empty-file-claim-1","ttl_seconds":600}
```

Keep the returned `token`, `generation`, and `expires_at`. Pass `token` as
`claim_token` to worker updates. It is a secret lease credential reused for
multiple actions during that claim. Do not place it in shared evidence, logs,
or a public handoff.

The default lease is ten minutes; the maximum requested TTL is seven days.
Expiry, reassignment, and generation changes can invalidate authority. Retrying
the same claim request uses its idempotency key; a key is not a lease renewal.

## Start, report, and complete

Send the actual worker the task, constraints, evidence, and claim credential via
the host's authorized collaboration mechanism. The source helper
`buildWorkerPacketAfterClaim` can assemble a worker packet after assign → claim;
it is a library helper, not an MCP tool or an automatic `minni_team_runtime`
dispatch step.

Call `minni_thread_worker_update` to start:

```json
{"plan_id":"<plan_id>","slice_id":"check-empty","worker_agent_id":"<worker_agent_id>","claim_token":"<token>","idempotency_key":"empty-file-start-1","action":"start"}
```

Wait for start to be applied before reporting completion. Run the work in the
host, then report actual evidence through the same tool:

```json
{"plan_id":"<plan_id>","slice_id":"check-empty","worker_agent_id":"<worker_agent_id>","claim_token":"<token>","idempotency_key":"empty-file-progress-1","action":"progress","evidence":"<actual command, result, and relevant artifact path>"}
```

After reviewing that evidence against the slice's gate, complete it:

```json
{"plan_id":"<plan_id>","slice_id":"check-empty","worker_agent_id":"<worker_agent_id>","claim_token":"<token>","idempotency_key":"empty-file-complete-1","action":"complete","evidence":"<verified outcome and remaining limitations>"}
```

Use a new idempotency key for each distinct action. Retry an uncertain action
with the same key, token, and unchanged payload; do not make a new key merely
because a response was lost. Worker actions also include `block`, `scar`, and
`propose_structure`. A structural proposal records a request for coordinator
review; it does not itself change the plan topology.

## Confirm applied state, not just acceptance

A contended write can return `status: "accepted"`, `applied: false`, and
`queued: true`. This means the queue accepted the request, **not** that the slice
changed or a completion gate passed. An applied response contains `slice`,
`ready_before`, `ready_after`, and `rev`.

Call `minni_thread_events` to follow applied journal events:

```json
{"plan_id":"<plan_id>","since_seq":0,"limit":100}
```

Advance the cursor with the returned `next_seq` and require a matching
`slice.completed` event for the intended plan and `slice_id` before reporting
that queued completion as applied. Check the corresponding operation and
revision; another slice's completion is not confirmation of this one.
`minni_thread_status` is a compact progress view: it omits finished slices and
its resolved count includes superseded slices, so that count alone cannot prove
a specific slice completed. Completion clears the live claim; an exact retry
can still replay its receipt. `minni_prepare_outcome` drafts an outcome
packet; it does not complete the Thread or approve shared memory.

## Recovery and current limits

- On a busy response or uncertain transport result, retry the same operation
  with bounded backoff and the same idempotency key. Do not infer success from
  the absence of an error in a different tool.
- If a lease expires or assignment changes, re-read status and coordinate a
  fresh valid claim. Replaying an old token does not regain authority.
- Queued requests need a drain process. MCP startup and in-process drain kicks
  help; the daemon's standing worker-write drain normally polls every five
  seconds and can be disabled with `MINNI_WORKER_WRITE_DRAIN=off`. Missing drain
  support, process outages, or apply failures can leave work pending. There is
  no guaranteed completion latency. Inspect status/events and runtime errors;
  preserve failure tickets instead of deleting them to make the queue look clear.
- If an event cursor is rejected or has a gap, re-read authoritative Thread
  status before continuing. A journal is evidence of applied transitions, not
  proof that an external command or test actually succeeded.

The implementation boundary is in
[`server.ts`](../plugins/minni/src/server.ts) (MCP schemas),
[`thread-worker.ts`](../plugins/minni/src/thread-worker.ts) (lease and mutation
rules), and [`worker_write_drain.py`](../src/minni/worker_write_drain.py)
(standing drain). Regression coverage includes
[`thread-worker.test.mjs`](../plugins/minni/tests/thread-worker.test.mjs) and
[`thread-write-queue.test.mjs`](../plugins/minni/tests/thread-write-queue.test.mjs).
