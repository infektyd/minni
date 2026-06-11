# slice-2-principal-templates

## Status

✅ Proven / tested

## Red Test

Command run from `engine/` before adding templates/script:

```bash
/Users/hansaxelsson/Projects/Minni/engine/.venv/bin/python -m pytest -q test_principal_templates.py
```

Expected RED result:

```text
3 failed in 0.03s
```

All failures were `ModuleNotFoundError: No module named 'tools.author_principals'`, proving the test was exercising missing template/script behavior.

## Green Tests

Focused command from `engine/`:

```bash
/Users/hansaxelsson/Projects/Minni/engine/.venv/bin/python -m pytest -q test_principal_templates.py test_principal_binding.py test_vault_root_binding.py
```

Result:

```text
25 passed in 0.05s
```

Full gate command from `engine/`:

```bash
/Users/hansaxelsson/Projects/Minni/engine/.venv/bin/python -m pytest -q
```

Result:

```text
563 passed, 2 skipped, 3 warnings in 34.32s
```

## Dry-run Smoke

Command run from the worktree root, without `--apply`:

```bash
engine/tools/author_principals.py --minni-home /tmp/minni-authoring-dryrun
```

Result:

```text
"dry_run": true
```

The smoke listed five would-write paths under `/tmp/minni-authoring-dryrun/principals/` and did not create live `~/.minni/principals` files.

## Implementation Evidence

✅ Proven / tested: templates exist for `claude-code`, `codex`, `gemini`, `grok-build`, and `kilocode` under `engine/principal_templates/`.

✅ Proven / tested: each rendered template contains the scoped per-agent vault root plus shared scope:

- `claude-code` -> `<MINNI_HOME>/claudecode-vault` and `<MINNI_HOME>/shared`
- `codex` -> `<MINNI_HOME>/codex-vault` and `<MINNI_HOME>/shared`
- `gemini` -> `<MINNI_HOME>/gemini-vault` and `<MINNI_HOME>/shared`
- `grok-build` -> `<MINNI_HOME>/grok-build-vault` and `<MINNI_HOME>/shared`
- `kilocode` -> `<MINNI_HOME>/kilocode-vault` and `<MINNI_HOME>/shared`

✅ Proven / tested: `engine/tools/author_principals.py` is dry-run by default and writes 0600 files only when `apply=True` / `--apply` is used. Unit tests apply only to a tmp_path principals dir, not the live system.

## Diff

See `evidence/slice-2-principal-templates/diff.patch`.
