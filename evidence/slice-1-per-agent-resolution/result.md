# slice-1-per-agent-resolution

## Status

✅ Proven / tested

## Red Test

Command run from `engine/` after adding tests and before implementation:

```bash
<repo>/engine/.venv/bin/python -m pytest -q test_principal_binding.py test_vault_root_binding.py
```

Expected RED result:

```text
7 failed, 15 passed in 0.09s
```

The failures covered:

- valid fileless agent claims still raising `IdentityMismatchError` instead of receiving a default-deny principal
- `principals/<agent_id>.json` not being loaded for the caller
- `main` claims receiving operator capabilities
- missing `operator_context` support
- empty capabilities + empty roots still allowing every vault path

## Green Tests

Focused command from `engine/`:

```bash
<repo>/engine/.venv/bin/python -m pytest -q test_principal_binding.py test_vault_root_binding.py test_approval_rpc.py test_scoped_recall.py
```

Result:

```text
28 passed in 0.24s
```

Full gate command from `engine/`:

```bash
<repo>/engine/.venv/bin/python -m pytest -q
```

Result:

```text
560 passed, 2 skipped, 3 warnings in 36.42s
```

## Implementation Evidence

✅ Proven / tested: `engine/principal.py` now resolves a supplied non-operator `agent_id` through `principals/<agent_id>.json` before falling back to legacy/platform compatibility paths.

✅ Proven / tested: a valid unknown/fileless agent claim returns `EffectivePrincipal(agent_id=<claim>, capabilities=[], allowed_vault_roots=[])`.

✅ Proven / tested: empty capabilities + empty roots is now a true deny shape for vault-root checks, while capable operator/default principals with empty roots preserve the historical wide-open behavior.

✅ Proven / tested: an agent claiming `main` receives the default-deny shape unless `operator_context=True` is passed.

✅ Proven / tested: per-agent file/name mismatches still raise `IdentityMismatchError`, and strict non-operator principal mismatches still return the existing `-32000` RPC mismatch shape.

## Operator-context choice

🟡 Likely / reasonable assumption: the simplest testable operator context is explicit resolver intent, implemented as `operator_context=True`. Existing local/operator no-agent-id calls still resolve through the canonical operator path for compatibility, while a model/agent-supplied `"main"` claim is default-denied.

➡️ What to review next: whether daemon operator-only handlers should eventually pass `operator_context=True` from a dedicated operator transport instead of relying on the no-agent-id local path.

## Diff

See `evidence/slice-1-per-agent-resolution/diff.patch`.
