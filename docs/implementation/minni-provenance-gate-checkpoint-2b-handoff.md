# Minni Provenance Gate Checkpoint 2b Handoff

Date: 2026-06-15
Branch: `feat/minni-provenance-gate`
Agent: Codex

## Scope

Focused checkpoint-2 blocker fix only: shared plugin gates now distinguish gate/daemon unavailability from real identity/authz rejection.

## Files Touched

- `plugins/minni/src/sovereign.ts`
- `plugins/minni/src/server.ts`
- `plugins/minni/tests/shared-gate.test.mjs`

Existing uncommitted checkpoint work remains in the tree; no commit, push, merge, or main changes were made.

## Behavior

- Gate present and accepting: shared tools continue through `gate.shared`.
- Gate unavailable: `Method not found: gate.shared`, missing socket, refused/unreachable socket, timeout, or equivalent socket errors degrade to the old local plugin path.
- Identity/authz failures are unchanged: unresolved agent / real gate rejection still return `gate-rejected`.

The blocker path is covered by:

- `plugins/minni/tests/plan.test.mjs`: existing MCP e2e confirms id-less plan tools return the normal no-active-plan JSON through the MCP server when the gate is unavailable via missing socket.
- `plugins/minni/tests/shared-gate.test.mjs`: new classifier pin confirms `Method not found: gate.shared` and socket-down errors are degraded, while identity/authz-like errors are not.
- `plugins/minni/tests/shared-gate-coverage.test.mjs`: shared tool coverage pin remains green.

## Verification

✅ Targeted plugin gate/plan checks:

```text
PATH=/opt/homebrew/opt/node@20/bin:$PATH npm run build:server
PATH=/opt/homebrew/opt/node@20/bin:$PATH node --test --import ./tests/setup-env.mjs tests/shared-gate.test.mjs tests/shared-gate-coverage.test.mjs tests/plan.test.mjs

20 passed / 0 failed
```

✅ Engine suite:

```text
engine/.venv/bin/python -m pytest -q engine

613 passed / 5 skipped / 3 warnings
```

❌ Full plugin suite in this Codex sandbox:

```text
PATH=/opt/homebrew/opt/node@20/bin:$PATH npm --prefix plugins/minni run test

302 passed / 40 failed
```

Visible failures were environment bind failures, not gate assertions:

```text
listen EPERM: operation not permitted 127.0.0.1
listen EPERM: operation not permitted /private/tmp/.../minnid.sock
```

Affected failing groups included AFM HTTP fixture tests and UI server tests that bind local loopback. The same runner also blocked a temporary Unix-socket fake daemon fixture, so the method-not-found case is pinned at the classifier level instead.

## Minni MCP

Minni MCP tool call was blocked in this environment:

```text
user cancelled MCP tool call
```

Per stop condition, this file is the checkpoint-2b handoff for Claude Code / Opus verification.
