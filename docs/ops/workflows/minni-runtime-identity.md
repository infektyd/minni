# Minni Runtime Identity Workflow

Use this workflow when changing daemon startup, process visibility, worker
roles, or installed service surfaces.

## Parallel lanes

Run these lanes concurrently when the change spans more than one launch path:

1. **Launch mapper** — enumerate `minni up`, foreground/module invocation,
   launchd, package entry points, and test harnesses. Report exact commands and
   process-observable names.
2. **Runtime implementer** — make the smallest cohesive source/test change.
   Preserve socket, RPC, launchd, and legacy compatibility identifiers.
3. **Surface auditor** — inspect docs, plugin health checks, service templates,
   and downstream process discovery for stale or misleading names.

The implementer waits for the mapper and auditor summaries before expanding
scope. All lanes report file paths, risks, and commands; recalled memory is
evidence only.

## Acceptance gate

- Focused tests pass (`PYTHONPATH=src python -m pytest -q
  tests/test_process_identity.py`). Native naming tests run in child processes
  so they cannot rename the test runner.
- The installed/package entry point still starts the same daemon.
- A real daemon's command title is `minni`, checked with
  `ps -p <pid> -o command=`. Record Activity Monitor evidence separately if
  claiming its display name; command titles and executable names may differ.
- `minni doctor` and a socket `ping` succeed after the rename.
- No unrelated dirty-worktree files are changed.

## Handoff

The runtime steward persona owns final integration and live verification. If a
lane discovers a downstream installed copy, report it as a propagation target;
do not silently patch global runtime files during source development.
