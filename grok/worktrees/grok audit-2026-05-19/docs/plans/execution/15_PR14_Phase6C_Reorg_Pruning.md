# PR-14: Phase 6 PR-C — Reorganization + Pruning Passes

> **Scope:** Vault reorganization pass (split overloaded entities, merge redundant concepts, rehome orphans) and pruning pass (expire/demote stale pages). **Most invasive — ships last with most caution.**
>
> **Depends on:** PR-13 (shared graph code, scheduler, lifecycle gating).
>
> **Behavior change:** Two new AFM passes proposing structural vault changes. All outputs are proposals — originals never modified directly by the loop.

---

## 6.1.d — Reorganization Pass

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/afm_passes/reorganization.py` | Reads vault as graph (`memory_links` + wikilinks). Operates incrementally by default. Detects structural issues and drafts proposals. |
| **CREATE** | `engine/afm_prompts/reorganization.md` | Frozen prompt template. |
| **MODIFY** | `engine/config.py` | Add `reorg_horizon_days: int = 30`. |

### Incremental Operation

Default `config.reorg_horizon_days=30`: only processes recently updated pages and their 1-2 hop neighbors. Avoids O(N) scaling on massive vaults.

### Detections

| Issue | Threshold | Proposal |
|-------|-----------|----------|
| **Overloaded entity** | One page accumulating ≥N distinct concepts | Split proposal: original marked `superseded_by` new peer set |
| **Redundant concepts** | Two pages with embedding cosine > 0.92 + overlapping wikilink sets | Merge proposal |
| **Orphan pages** | No wikilinks, no `index.md` entry | Rehome proposal: which pages should link to this, or archive |

### Constraints

- Outputs are **proposals** (diffs the agent/human can accept).
- The original page is **never modified** by the loop directly.
- All proposals are `status: draft` with `agent: afm-loop`.

---

## 6.1.e — Pruning Pass

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/afm_passes/pruning.py` | Reads `expires_at`, `decay_score`, `access_count`. Drafts status transitions and hygiene findings. |
| **CREATE** | `engine/afm_prompts/pruning.md` | Frozen prompt template. |

### Proposed Transitions

| Condition | Proposed Transition |
|-----------|-------------------|
| Page past `expires_at` | `accepted → expired` |
| Evidence superseded but page not updated | `accepted → candidate` (re-review) |
| Page fails hygiene validation but still `accepted` | Hygiene finding surfaced |

### Output

Proposals written to `inbox/afm-pruning-YYYY-MM-DD.json` (Phase 4.6 inbox contract). Next agent SessionStart surfaces it for review.

### Constraints

- The AFM loop **never deletes**. It supersedes (`superseded_by`) or expires (`status: expired`).
- Originals remain on disk under `_archive/` per existing convention.

---

## Verification

1. **Reorg dry-run:** `python -m engine.sovereign_memory compile --pass reorganization --dry-run` against a vault with known overloaded/redundant pages → returns split/merge proposals.
2. **Pruning dry-run:** `python -m engine.sovereign_memory compile --pass pruning --dry-run` against a vault with expired pages → returns transition proposals.
3. **No direct modification:** After wet-run, verify original pages unchanged. Only new `status: draft` proposal pages created.
4. **Pruning inbox:** `inbox/afm-pruning-YYYY-MM-DD.json` written with proposals.
5. **Incremental:** Reorg only processes pages updated within `reorg_horizon_days`.
6. **Lifecycle:** All proposals require endorsement to take effect.

---

## PR-14 Completion Checklist

- [ ] `engine/afm_passes/reorganization.py` detects overloaded, redundant, orphan pages
- [ ] `engine/afm_passes/pruning.py` proposes expire/demote transitions
- [ ] `engine/afm_prompts/reorganization.md` and `pruning.md` frozen templates
- [ ] `reorg_horizon_days` config for incremental operation
- [ ] Proposals are `status: draft`, never modify originals
- [ ] Pruning outputs to inbox JSON file
- [ ] AFM loop never deletes — only supersedes or expires
- [ ] Each pass independently disableable via `afm_loop_schedule`
- [ ] Observability: audit entries and traces
- [ ] All existing tests pass

---

## Next Steps

→ [16_PR15_Phase7_Quantized_Semantic.md](./16_PR15_Phase7_Quantized_Semantic.md)
