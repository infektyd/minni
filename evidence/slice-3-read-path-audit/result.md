# slice-3-read-path-audit

## Status

✅ Proven / tested

## Red Test

Command run from `engine/` after adding visibility/drill coverage and before implementation:

```bash
<repo>/engine/.venv/bin/python -m pytest -q test_retrieval_visibility.py
```

Expected RED result after test harness fixes:

```text
1 failed, 7 passed, 3 warnings in 4.72s
```

The remaining failure proved `_handle_sm_drill` called `expand_result` with `principal=None` and `workspace=None`.

## Green Tests

Focused command from `engine/`:

```bash
<repo>/engine/.venv/bin/python -m pytest -q test_retrieval_visibility.py test_handoff_wikilink_containment.py test_scoped_recall.py test_pr1b_contracts.py::TestDepthTiers::test_sm_drill_batches_result_ids
```

Result:

```text
16 passed, 3 warnings in 6.15s
```

Full gate command from `engine/`:

```bash
<repo>/engine/.venv/bin/python -m pytest -q
```

Result:

```text
565 passed, 2 skipped, 3 warnings in 33.80s
```

## Read-path audit

✅ Proven / tested: `RetrievalEngine.retrieve(... principal=...)` filters every merged result through `can_read_document`. New `test_retrieve_scoped_principal_sees_only_own_and_shared_docs` proves a scoped `codex` principal sees only `codex-vault` content plus safe shared wiki content, not foreign or private shared content.

✅ Proven / tested: `_handle_search` already resolves a stamped principal and passes it to `retrieve`; `test_scoped_recall.py` also stays green for scoped learning recall with `cross_agent` opt-in.

✅ Proven / tested: `_handle_expand` already passes stamped `principal` and `workspace` to `expand_result`; `test_expand_result_with_principal_denies_forbidden` keeps signature coverage.

✅ Proven / tested: `_handle_read` resolves a stamped principal and gates prior-context rows through `can_read_document`. Layer 1 identity rows remain constrained by stamped `identity:<agent_id>` selection.

✅ Proven / tested: wikilink expansion/containment remains covered by `test_handoff_wikilink_containment.py`.

✅ Proven / tested: `agent_api.recall` constructs a principal from the agent API instance and calls `retrieve(... principal=p, workspace="default")`.

✅ Fixed: `_handle_sm_drill` was an additional read surface not named in the slice title. It now resolves a stamped principal and passes `principal`/`workspace` to each `expand_result` call. Existing depth-tier tests were updated to assert that behavior.

## Diff

See `evidence/slice-3-read-path-audit/diff.patch`.
