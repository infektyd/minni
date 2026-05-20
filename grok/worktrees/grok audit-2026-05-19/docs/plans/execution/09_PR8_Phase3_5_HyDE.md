# PR-8: Phase 3.5 — HyDE for Cold Queries

> **Scope:** Hypothetical Document Embedding when all top-K confidence scores are below threshold.
>
> **Depends on:** PR-4 (eval harness gates default-flip).
>
> **Behavior change:** When enabled and all results are low-confidence, generates a hypothetical answer via AFM, embeds it, searches again, merges via RRF. Default-on but gated by eval harness.

---

## 3.5 — HyDE

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/hyde.py` | HyDE logic: when all top-K confidence < `config.hyde_confidence_floor`, call AFM for 2-sentence hypothetical answer, embed it, search again, merge via RRF. |
| **MODIFY** | `engine/retrieval.py` | After initial search, check confidence floor. If HyDE triggered, run second pass and merge. Add `via_hyde: true` to provenance. |
| **MODIFY** | `engine/config.py` | Add `hyde_enabled: bool = True`, `hyde_confidence_floor: float = 0.4`. |

### Logic Flow

1. Run normal hybrid search.
2. Check: are all top-K results' `confidence` < `hyde_confidence_floor`?
3. If yes and `hyde_enabled`: call AFM bridge for 2-sentence hypothetical answer.
4. Embed the hypothetical answer.
5. Search again using the hypothetical embedding.
6. Merge original + HyDE results via RRF.
7. Add `via_hyde: true` to provenance of HyDE-contributed results.

### Constraints

- Max one HyDE pass per query (no recursion).
- If AFM bridge is down: gracefully skip — return original results as-is.
- `via_hyde: true` in provenance so agent knows.
- Default-on gate: eval harness must confirm net positive recall before this is truly default.

### Verification

```bash
# HyDE triggers on cold query (low confidence results)
python -c "
from engine.retrieval import hybrid_search
results = hybrid_search('extremely obscure topic with no direct matches')
hyde_results = [r for r in results if r.get('provenance', {}).get('via_hyde')]
print(f'HyDE contributed {len(hyde_results)} results')
"

# HyDE does NOT trigger when results are confident
python -c "
from engine.retrieval import hybrid_search
results = hybrid_search('well-known topic in the vault')
hyde_results = [r for r in results if r.get('provenance', {}).get('via_hyde')]
assert len(hyde_results) == 0, 'HyDE should not trigger on confident results'
print('PASS')
"

# Eval harness comparison
python -m engine.eval.harness run --config baseline,with-hyde
```

---

## PR-8 Completion Checklist

- [ ] `engine/hyde.py` implements HyDE logic
- [ ] Triggers only when all top-K confidence < threshold
- [ ] Max one pass per query
- [ ] `via_hyde: true` in provenance for HyDE-contributed results
- [ ] Gracefully skips when AFM bridge down
- [ ] `hyde_enabled` and `hyde_confidence_floor` in config
- [ ] Eval harness comparison run and documented
- [ ] All existing tests pass

---

## Next Steps

→ [10_PR9_Phase4_1_2_Feedback_Trace.md](./10_PR9_Phase4_1_2_Feedback_Trace.md)
