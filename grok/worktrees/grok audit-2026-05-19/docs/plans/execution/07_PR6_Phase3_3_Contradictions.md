# PR-6: Phase 3.3 — Structured Learnings + Contradiction Detection

> **Scope:** Structured assertion fields on learnings, semantic contradiction detection, force/resolve workflow, native resolution tool.
>
> **Depends on:** PR-2 (envelope, migrations).
>
> **Behavior change:** `learn()` may now return `{status: "contradiction"}` instead of writing. Agents must handle this.

---

## 3.3 — Structured Learnings + Contradiction Detection

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/migrations/005_structured_learnings.sql` | `ALTER TABLE learnings ADD COLUMN assertion TEXT, applies_when TEXT, evidence_doc_ids TEXT, contradicts_id INTEGER REFERENCES learnings(learning_id)`. All nullable. |
| **MODIFY** | `engine/writeback.py` | Add `detect_contradictions(content_or_assertion, agent_id)`: semantic search against active learnings; returns hits with cosine > 0.85. |
| **MODIFY** | `engine/sovrd.py` | Modify `learn()` JSON-RPC: if contradictions found and `force=False` (default), return `{status: "contradiction", candidates: [...]}`. Agent must resubmit with `force=true` or supply `contradicts_id`. |
| **MODIFY** | `engine/sovrd.py` | Add `daemon.resolve_contradiction(new_content, supersede_ids=[...])`: writes new learning and atomically updates `superseded_by` on old conflicting pages. |

### Contradiction Detection Logic

1. Extract assertion from new learning content (or use explicit `assertion` field if provided).
2. Embed the assertion.
3. Search existing active learnings (not superseded/rejected/expired) with cosine similarity.
4. Hits with cosine > 0.85 are candidates.
5. Return candidates with their content, scores, and IDs.

### Resolution Tool

`daemon.resolve_contradiction(new_content, supersede_ids=[...])`:
- Writes the new learning.
- Atomically sets `superseded_by = new_learning_id` on each page in `supersede_ids`.
- Updates status lifecycle on superseded pages.
- Prevents agents from lazily spamming `force=true`.

### Constraints

- Old free-text `content` stays canonical. New fields populated only when caller provides them.
- The plugin's `assessLearningQuality` already does some client-side checking — the daemon now does it for **all** clients uniformly.
- Contradiction threshold (0.85) should be configurable in `config.py`.

### Verification

```bash
# Round-trip test
python -c "
from engine.sovrd_client import learn, resolve_contradiction

# Store a learning
learn('The auth system uses JWT tokens', agent_id='test')

# Store a contradicting one — should be blocked
result = learn('The auth system uses session cookies, not JWT', agent_id='test')
assert result['status'] == 'contradiction', f'Expected contradiction, got {result}'
print(f'Blocked with {len(result[\"candidates\"])} candidates')

# Resolve it properly
resolve_contradiction(
    'The auth system migrated from JWT to session cookies in April 2026',
    supersede_ids=[result['candidates'][0]['id']]
)
print('PASS')
"
```

---

## PR-6 Completion Checklist

- [ ] `005_structured_learnings.sql` adds nullable columns
- [ ] `detect_contradictions()` in writeback.py works with cosine > 0.85
- [ ] `learn()` returns `{status: "contradiction"}` when conflicts found and `force=False`
- [ ] `learn()` with `force=True` bypasses detection and writes
- [ ] `resolve_contradiction()` writes new + supersedes old atomically
- [ ] Contradiction threshold configurable in config.py
- [ ] Existing `learn()` calls without new fields still work (backward compatible)
- [ ] All existing tests pass — **careful testing required**

---

## Next Steps

→ [08_PR7_Phase3_4_Query_Expansion.md](./08_PR7_Phase3_4_Query_Expansion.md)
