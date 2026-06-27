# Git hooks

Committed hooks that enforce the same gates locally that CI runs. They live in
this directory (`core.hooksPath`) rather than `.git/hooks/` so they are version
controlled and shared.

## Enable

Once per clone:

```bash
git config core.hooksPath .githooks
```

This is opt-in by design — it is not set automatically, and it does not touch
your global git config beyond this repository.

## What runs

| Hook | When | What it does |
|---|---|---|
| `pre-commit` | every commit | 1) public-boundary guard (blocks staged paths matching `.gitignore`/`.gitfilters`); 2) fast lint gate: `ruff` (only if `engine/*.py` staged) + `eslint` + `tsc --noEmit` (only if `plugins/minni/{src,tests}` staged). No full test run, so commits stay fast. |
| `pre-push` | every push | `make check` — ruff + plugin eslint/typecheck/build/test + scoped engine pytest. The full gate before code leaves your machine. |

## Escape hatch

Skip the hooks for a single git command (e.g. a WIP commit) without disabling
them:

```bash
MINNI_SKIP_HOOKS=1 git commit -m "wip"
MINNI_SKIP_HOOKS=1 git push
```

The hooks degrade gracefully: if `ruff` is not on `PATH`, or
`plugins/minni/node_modules` / `make` are missing, the affected step is skipped
with a warning rather than blocking the operation.
