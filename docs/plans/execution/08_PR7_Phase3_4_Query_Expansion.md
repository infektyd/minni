# PR-7: Phase 3.4 — Query Expansion

> **Scope:** Rule-based query expansion (default-on), AFM-assisted expansion (opt-in), graph neighborhood summarization.
>
> **Depends on:** PR-4 (eval harness must exist to gate AFM default-flip).
>
> **Behavior change:** Rule-based expansion is default-on (~5ms overhead). AFM expansion opt-in only.

---

## 3.4 — Query Expansion

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/query_expand.py` | `expand(query, mode="rule") -> list[str]`. Two strategies behind one function. |
| **CREATE** | `engine/data/synonyms.yml` | Small synonym table: common abbreviations, acronyms, casing variants relevant to the codebase. |
| **MODIFY** | `engine/retrieval.py` | `search()` accepts `expand: bool | "rule" | "afm"` (default `True` = rule-based). Each variant runs full hybrid retrieval; results merged via RRF. Response includes `query_variants: list[str]`. |
| **MODIFY** | `engine/sovrd.py` | Expose `expand` kwarg. Add `summarize_neighborhood` kwarg. |
| **MODIFY** | `engine/config.py` | Add `query_expand_default: str = "rule"`. |

### Expansion Strategies

| Strategy | When | Latency | Details |
|----------|------|---------|---------|
| **Rule-based** | Default (`expand=True` or `expand="rule"`) | ~5ms | Synonym table, acronym expansion, casing variants. |
| **AFM-assisted** | Opt-in (`expand="afm"`) | ~200ms | Calls AFM bridge at `127.0.0.1:11437/v1/chat/completions` with 2-shot prompt returning 2-3 reformulations. |

### Graph Neighborhood Summarization

When `summarize_neighborhood=True`: after recalling a specific entity/concept page, use AFM bridge to summarize its 1-hop wiki links. Saves agent from manual graph traversal.

### Default-Flip Gate

AFM mode only becomes default after Phase 3.0 eval harness shows ≥+5% recall@5 with no regression.

### Constraints

- Rule-based is instant and always available.
- AFM-assisted gracefully degrades if AFM bridge is down (skip, return rule-based only).
- `synonyms.yml` starts small; agents and users can expand it.

### Verification

```bash
# Rule-based expansion works
python -c "
from engine.query_expand import expand
variants = expand('auth JWT migration')
print(f'Variants: {variants}')
assert len(variants) >= 2
"

# Eval harness comparison
python -m engine.eval.harness run --config baseline,with-expand
# Inspect report for recall@5 delta
```

---

## PR-7 Completion Checklist

- [x] `engine/query_expand.py` with rule-based + AFM strategies
- [x] `engine/data/synonyms.yml` with initial synonym/acronym set
- [x] `search()` accepts `expand` parameter (default `True` = rule-based)
- [x] Query variants included in response
- [x] `summarize_neighborhood` works when AFM available
- [x] AFM mode gracefully degrades when bridge down
- [x] Eval harness comparison run and documented (`python3 -m engine.eval.harness run --config baseline,with-expand --mock`; baseline R@5=0.9608, with-expand R@5=0.9608)
- [x] All existing engine tests pass (`pytest -q engine`: 180 passed, 3 skipped; plugin `npm test` not run because this worktree has no `node_modules` and `tsc` is unavailable)

---

## Next Steps

→ [09_PR8_Phase3_5_HyDE.md](./09_PR8_Phase3_5_HyDE.md)
