# PR-13: Phase 6 PR-B — Synthesis + Procedure Extraction Passes

> **Scope:** Synthesis pass (bridges accepted pages sharing tags), procedure extraction pass (detects repeated patterns and codifies them). Ships together because they share wiki-graph reading code.
>
> **Depends on:** PR-12 (scheduler, writer, prompt contracts, lifecycle gating).
>
> **Behavior change:** Two new AFM compilation passes. Both produce `status: draft` pages requiring endorsement.

---

## 6.1.b — Synthesis Pass

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/afm_passes/synthesis.py` | Reads `accepted` pages within same `tags` cluster or wikilink neighborhood. Drafts `synthesis` pages bridging multiple sources. |
| **CREATE** | `engine/afm_prompts/synthesis.md` | Frozen prompt template for synthesis. |

### Trigger Conditions

- ≥3 `accepted` pages share a tag AND no `synthesis` page exists for that tag.
- OR: existing synthesis is older than its sources by configurable threshold (default 30 days).

### Output

`synthesis` pages with `status: draft`, `agent: afm-loop`, sources citing the constituent pages.

---

## 6.1.c — Procedure Extraction Pass

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/afm_passes/procedure_extraction.py` | Detects repeated patterns in episodic events and `session` pages: "agent did X, then Y, then Z" appearing 3+ times across sessions. Drafts `procedure` page codifying the steps. |
| **CREATE** | `engine/afm_prompts/procedure_extraction.md` | Frozen prompt template for procedure extraction. |

### Detection Logic

1. Scan recent `session` pages and episodic events.
2. Identify action sequences appearing 3+ times.
3. Draft a `procedure` page with steps and citations to originating sessions.
4. Agent can then recall the procedure instead of rediscovering the pattern.

### Output

`procedure` pages with `status: draft`, `agent: afm-loop`, sources citing the originating sessions.

---

## Shared Code

Both passes share wiki-graph reading code:
- Reading `memory_links` + wikilinks to understand page neighborhoods.
- Tag clustering logic.
- Common utilities for iterating accepted pages.

Factor shared logic into `engine/afm_passes/_graph_utils.py` or similar.

---

## Verification

1. **Synthesis dry-run:** `python -m engine.sovereign_memory compile --pass synthesis --dry-run` against a vault with ≥3 pages sharing a tag → returns synthesis draft proposals.
2. **Procedure dry-run:** `python -m engine.sovereign_memory compile --pass procedure_extraction --dry-run` against a vault with repeated session patterns → returns procedure draft proposals.
3. **Lifecycle:** Wet-run produces `status: draft` pages; no auto-accept.
4. **Prompt contracts:** Templates versioned, trace records prompt version.
5. **Quality gate:** Drafts go through `assessLearningQuality` + contradiction detection.

---

## PR-13 Completion Checklist

- [ ] `engine/afm_passes/synthesis.py` drafts synthesis pages from tag clusters
- [ ] `engine/afm_passes/procedure_extraction.py` detects repeated patterns, drafts procedures
- [ ] `engine/afm_prompts/synthesis.md` and `procedure_extraction.md` frozen templates
- [ ] Shared graph-reading code factored out
- [ ] Both passes produce `status: draft` with proper sourcing
- [ ] `daemon.compile(pass_name="synthesis")` and `procedure_extraction` work
- [ ] Lifecycle gating: endorsement required
- [ ] Observability: audit entries and traces for both passes
- [ ] All existing tests pass

---

## Next Steps

→ [15_PR14_Phase6C_Reorg_Pruning.md](./15_PR14_Phase6C_Reorg_Pruning.md)
