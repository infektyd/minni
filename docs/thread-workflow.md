# Run a Thread through MCP

A Thread records a shared multi-slice plan, slice dependencies, verification gates, evidence, and failure scars as plain Markdown in the agent vault (`plan-*.md` + `plan-*.log.md`). The host runtime still runs commands and supervises subagents: creating or updating a Thread tracks shared state across turns, compaction, and sibling sessions without spawning processes.

These are **MCP tool names and JSON argument objects** exposed by the 12-tool Minni MCP server (`plugins/minni/src/server.ts`), not shell commands or raw daemon JSON-RPC methods. All 4 Thread tools (`minni_thread_create`, `minni_thread_status`, `minni_thread_update`, `minni_thread_scar`) enforce compact `< 2 KB` responses (`< 1 KB` for scars) and serialize mutations safely under an on-disk plan lock — there are no worker lease tokens, `claim` steps, or `idempotency_key` fields to manage. Calls below write plan state; use a disposable vault when rehearsing them.

## 1. Orient and create (`minni_recall` → `minni_thread_create`)

Before opening a multi-slice plan, call `minni_recall` to pull Living Repo Map subsystem cards, prior decisions, and repository scars for the active workspace:

```json
{"query": "empty-file parser behavior"}
```

Create the Thread with `minni_thread_create`:

```json
{
  "goal": "Verify empty-file parser behavior",
  "constraints": ["Preserve existing valid-file behavior"],
  "slices": [
    {
      "id": "repro-empty",
      "title": "Add empty-file parser regression test",
      "gate": "pytest tests/test_parser.py -k empty"
    },
    {
      "id": "verify-suite",
      "title": "Run full parser suite",
      "depends_on": ["repro-empty"],
      "gate": "pytest tests/test_parser.py"
    }
  ],
  "seed_scar_from_audit": true,
  "activate": true
}
```

Save the returned `plan_id`. By default (`activate: true`), `minni_thread_create` binds the new plan to the current session's isolated pointer (`<vault>/.runtime/active-thread.d/<sessionKey>.json`) so concurrent terminal or IDE windows can run independent plans without clobbering one another. If this session already had an in-flight active plan, the `< 2 KB` receipt includes `displaced_active` and a warning showing how to re-activate it. Passing explicit `plan_id` values in subsequent calls prevents ambiguity if active-plan state changes later.

## 2. Inspect compact progress (`minni_thread_status`)

Call `minni_thread_status` (`plan_id` is optional and defaults to the current session's active plan):

```json
{"plan_id": "<plan_id>"}
```

The `< 2 KB` response returns `plan_id`, `status`, `rev`, `active`, and `view` (`headline`, `progress` counts, non-terminal slices, `next_action`, and capped `scars` previews). If the plan recorded a `shelf_ref`, passing optional `live_shelf_content` reports whether the shelf hash drifted (`drift`) without auto-pulling content into context.

## 3. Start, gate, and complete slices (`minni_thread_update`)

Mark a slice `in_progress` when starting work:

```json
{
  "plan_id": "<plan_id>",
  "slice_id": "repro-empty",
  "status": "in_progress"
}
```

Run the slice's verification gate in the host, then mark the slice `done` with concrete `evidence`:

```json
{
  "plan_id": "<plan_id>",
  "slice_id": "repro-empty",
  "status": "done",
  "evidence": "pytest tests/test_parser.py -k empty -> 1 passed in 0.12s"
}
```

Rules enforced by `minni_thread_update`:

- **Allowed `status` values:** `"pending"`, `"in_progress"`, `"done"`, `"blocked"`, `"superseded"`.
- **Evidence gate:** Transitioning a slice to `"done"` requires a non-empty `evidence` string recording what was verified.
- **Dependency gate (`depends_on`):** A slice cannot transition to `"done"` while any slice in its `depends_on` list is neither `"done"` nor `"superseded"`. Overriding an unmet dependency requires both `"force": true` and a non-empty `"force_reason"`, which appends an audited `depends_on_override` entry to `plan-*.log.md`.
- **Compact `< 2 KB` response by default:** Responses omit full slice arrays and unbounded `scar_tissue` so routine status updates never bloat the context window (`compact` defaults to `true`; pass `"include_full_plan": true` or `"compact": false` only when inspecting the full plan body).
- **Per-session activation control:** Toggle or clear this session's active-plan pointer without mutating a slice by calling `{"plan_id": "<plan_id>", "activate": true}` or `{"plan_id": "<plan_id>", "activate": false}`. When all slices reach terminal state, the session's active pointer clears automatically.

## 4. Record dead ends and failed commands (`minni_thread_scar`)

When a command fails, an approach hits a dead end, or a hypothesis is refuted, record it immediately with `minni_thread_scar` (`< 1 KB` receipt):

```json
{
  "plan_id": "<plan_id>",
  "slice_id": "repro-empty",
  "kind": "failed_command",
  "signal": "parser.parse('') raised EOFError before empty-input guard",
  "resolution": "Return empty AST node before tokenizer read"
}
```

`kind` must be `"failed_command"`, `"dead_end"`, or `"rejected_hypothesis"`. Scars are appended to the Thread artifact, surfaced in compact status/boot previews so retries are avoided after compaction, and promoted into workspace-scoped scar memory so future sessions on the same repository see them via `minni_recall` and `project_shelf`.

## 5. Hand off across sessions or agents (`minni_handoff_send` & `minni_handoff_inbox`)

To hand the remaining slices to your next session (default when `to_agent` is omitted) or to another wired principal (`"codex"`, `"claude-code"`, `"gemini"`, `"grok-build"`, `"kilocode"`, `"cursor"`), call `minni_handoff_send`:

```json
{
  "to_agent": "codex",
  "task": "Run verify-suite slice on <plan_id>",
  "summary": "repro-empty slice is done and green",
  "files": ["src/parser.py", "tests/test_parser.py"],
  "next_steps": ["Run pytest tests/test_parser.py and mark verify-suite done"]
}
```

`minni_handoff_send` packages top recalls and recent scars into an `<EVIDENCE>`-framed handoff envelope and returns a `< 1 KB` lease receipt (`status`, `lease_id`, `from_agent`, `to_agent`, `expires_at`). Cross-agent handoff requires the `handoff` capability on the sending principal.

On the receiving session or agent, preview pending handoffs first with `minni_handoff_inbox`:

```json
{}
```

The inbox response previews `task`, `summary`, `files`, `next_steps`, and `open_questions` for waiting handoffs in the active workspace before acknowledgement. Acknowledge one lease (`{"lease_id": "<lease_id>", "status": "accepted"}`) or read-and-clear all waiting handoffs in one step:

```json
{
  "ack": true,
  "status": "accepted",
  "note": "Picked up <plan_id> verify-suite"
}
```

## Recovery and implementation boundaries

- **Lock contention:** Concurrent `minni_thread_update` or `minni_thread_scar` calls against the same `plan_id` serialize under `withThreadPlanLock`. If a call returns `THREAD_BUSY`, retry after a brief pause and re-check `minni_thread_status` (`rev` increments monotonically on every applied mutation).
- **Operator visibility:** Operators can inspect active Threads, pending handoffs, and recent scars from the terminal at any time via `minni activity` and `~/.minni/TODAY.md` without opening an agent session.
- **Source & tests:** MCP schemas and compact budget serialization live in [`server.ts`](../plugins/minni/src/server.ts), plan state and active pointers in [`plan.ts`](../plugins/minni/src/plan.ts), lock/dependency/journal helpers in [`thread-worker.ts`](../plugins/minni/src/thread-worker.ts), and per-session relay cursors in [`thread-notification-relay.ts`](../plugins/minni/src/thread-notification-relay.ts) and [`thread_relay.py`](../src/minni/thread_relay.py), covered by [`plan.test.mjs`](../plugins/minni/tests/plan.test.mjs) and [`thread-worker.test.mjs`](../plugins/minni/tests/thread-worker.test.mjs).
