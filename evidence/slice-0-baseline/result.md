# slice-0-baseline

## Status

✅ Proven / tested

## Gate

Command run from `engine/`:

```bash
/Users/hansaxelsson/Projects/Minni/engine/.venv/bin/python -m pytest -q
```

Result:

```text
555 passed, 2 skipped, 3 warnings in 38.79s
```

This satisfies the slice gate: full engine pytest green in the worktree.

## Spec-vs-today deltas confirmed

✅ Proven / tested: codex is no longer rejected solely because it is in `platform_agent_ids`.

- `engine/principal.py:407-440` resolves a strict-mode `platform_agent_ids` entry to its own stamped `EffectivePrincipal`.
- `engine/test_principal_binding.py:127-151` covers `codex` resolving to `agent_id == "codex"` with scoped platform capabilities.

✅ Proven / tested: `principal.py` has the new helper APIs that later slices must preserve.

- `engine/principal.py:272-288` defines `validate_agent_id`.
- `engine/principal.py:349-361` defines `agent_scope_for` and exposes `agent_scope_for.cache_clear`.

✅ Proven / tested: learning recall is scoped by default and cross-agent recall is opt-in.

- `engine/test_scoped_recall.py:94-107` proves default recall returns only the caller principal's learning rows.
- `engine/test_scoped_recall.py:110-124` proves `cross_agent: True` returns matching rows across agents.
- `engine/retrieval.py:1459-1463` carries the `cross_agent` and stamped-principal inputs through the retrieval API.

## Notes

No source code changed in this slice. It establishes the branch baseline before the per-connection resolver work.
