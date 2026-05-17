# Sovereign Memory Core Upgrades — Master Tracker

> **Source of truth:** [`SOVEREIGN-MEMORY-CORE-UPGRADES-SCALE-AGNOSTIC.md`](../SOVEREIGN-MEMORY-CORE-UPGRADES-SCALE-AGNOSTIC.md)
>
> This file is the execution dashboard. Each checkbox maps to one shippable, revertible PR.
> Phase docs contain the full technical spec for each PR.
>
> **Subagent-Driven Development / Two-Stage Review**
> - `[ ]` Pending
> - `[A]` In Progress (Subagent dispatched)
> - `[R]` Pending Review (Subagent staged changes, orchestrator must review and merge)
> - `[x]` Completed and Merged

---

## Hard Constraints (apply to every PR)

- **Zero regression.** Every existing JSON-RPC method, table, column, and config key keeps working.
- **Schema-additive only.** No `DROP`, no destructive `ALTER`. New columns are nullable.
- **Default behavior unchanged.** New features opt-in or auto-fallback. Pull-and-restart = identical behavior.
- **No new mandatory dependencies.** Optional deps are lazy-imported and feature-gated.
- **Graceful downgrade is a feature.** Every new feature defines its degraded mode. Recall always returns *something*. Failures degrade to a less-rich envelope, never to a stack trace.
- **Memory is evidence, not instruction.** Recalled content is citation, never command.
- **SQLite remains runtime truth.** Every other surface is derived or overlay.

---

## Rollout Order

| # | PR | Phase(s) | Scope | Depends On | Status |
|---|-----|----------|-------|------------|--------|
| 01 | PR-1 | 0.1, 0.2, 0.3 | Schema versioning, model singletons, tiktoken | — | `[x]` |
| 02 | PR-1b | 0.4, 0.5, 0.6 | Agent contract, vault contract, progressive disclosure | — | `[x]` |
| 03 | PR-2 | 1.1, 1.2, 1.3 | Persistent FAISS, result envelope, vault page schema | PR-1 | `[x]` |
| 04 | PR-3 | 2.1–2.5 | Storage abstraction (VectorBackend protocol) | PR-2 | `[x]` |
| 05 | PR-4 | 3.0 | Eval harness, policy docs, workflows | PR-2 | `[x]` |
| 06 | PR-5 | 3.1, 3.2 | Cross-encoder cache, layer-aware retrieval | PR-2 | `[x]` |
| 07 | PR-6 | 3.3 | Structured learnings + contradiction detection | PR-2 | `[x]` |
| 08 | PR-7 | 3.4 | Query expansion (rule-based + AFM opt-in) | PR-4 (eval gate) | `[x]` |
| 09 | PR-8 | 3.5 | HyDE for cold queries | PR-4 (eval gate) | `[x]` |
| 10 | PR-9 | 4.1, 4.2 | Negative feedback + per-query trace | PR-2 | `[x]` |
| 11 | PR-10 | 4.6 | Agent inbox/outbox + handoff spec | PR-1b, PR-2 | `[x]` |
| 12 | PR-11 | 4.5, 5.x | Provenance edges, health, stats, hygiene | PR-2, PR-6 | `[x]` |
| 13 | PR-12 | 6 PR-A | AFM scheduler + session distillation pass | PR-11 | `[x]` |
| 14 | PR-13 | 6 PR-B | Synthesis + procedure extraction passes | PR-12 | `[x]` |
| 15 | PR-14 | 6 PR-C | Reorganization + pruning passes | PR-13 | `[x]` |
| 16 | PR-15 | 7.1, 7.2 | Quantized embeddings, semantic chunking | PR-4 (eval gate) | `[x]` |

---

## Dependency Graph

```
PR-1 (Foundation) ──┬──► PR-2 (FAISS + Envelope + Vault Schema)
                     │       ├──► PR-3 (Storage Abstraction)
                     │       ├──► PR-5 (Rerank Cache + Layers)
                     │       ├──► PR-6 (Contradictions)
                     │       ├──► PR-9 (Feedback + Trace)
                     │       └──► PR-4 (Eval Harness + Policy Docs)
                     │               ├──► PR-7 (Query Expansion)
                     │               ├──► PR-8 (HyDE)
                     │               └──► PR-15 (Quantized + Semantic)
                     │
PR-1b (Contracts) ──►├──► PR-10 (Inbox/Outbox + Handoff)
                     │
                     └──► PR-11 (Provenance + Observability + Hygiene)
                              └──► PR-12 (AFM Loop: Session Distill)
                                      └──► PR-13 (AFM Loop: Synthesis + Procedures)
                                              └──► PR-14 (AFM Loop: Reorg + Pruning)
```

---

## Verification Checklist (run after every PR)

- [ ] `cd engine && pytest -q` — all tests green
- [ ] JSON-RPC contract test — every pre-existing method returns unchanged shape
- [ ] `cd plugins/sovereign-memory && npm test` — all 29 tests pass
- [ ] Claude Code hook smoke: `npm run smoke:hook` — valid envelopes
- [ ] Live recall sanity: known query returns same top result ±1 position
- [ ] Migration safety: apply to copy of live DB, no rows lost

---

## Phase Documents

| Doc | File |
|-----|------|
| PR-1 | [01_PR1_Phase0_Foundation.md](./01_PR1_Phase0_Foundation.md) |
| PR-1b | [02_PR1b_Phase0_Contracts.md](./02_PR1b_Phase0_Contracts.md) |
| PR-2 | [03_PR2_Phase1_FAISS_Envelope_Schema.md](./03_PR2_Phase1_FAISS_Envelope_Schema.md) |
| PR-3 | [04_PR3_Phase2_Storage_Abstraction.md](./04_PR3_Phase2_Storage_Abstraction.md) |
| PR-4 | [05_PR4_Phase3_0_Eval_Harness_Policy.md](./05_PR4_Phase3_0_Eval_Harness_Policy.md) |
| PR-5 | [06_PR5_Phase3_1_2_Cache_Layers.md](./06_PR5_Phase3_1_2_Cache_Layers.md) |
| PR-6 | [07_PR6_Phase3_3_Contradictions.md](./07_PR6_Phase3_3_Contradictions.md) |
| PR-7 | [08_PR7_Phase3_4_Query_Expansion.md](./08_PR7_Phase3_4_Query_Expansion.md) |
| PR-8 | [09_PR8_Phase3_5_HyDE.md](./09_PR8_Phase3_5_HyDE.md) |
| PR-9 | [10_PR9_Phase4_1_2_Feedback_Trace.md](./10_PR9_Phase4_1_2_Feedback_Trace.md) |
| PR-10 | [11_PR10_Phase4_6_Inbox_Handoff.md](./11_PR10_Phase4_6_Inbox_Handoff.md) |
| PR-11 | [12_PR11_Phase4_5_Phase5_Observability.md](./12_PR11_Phase4_5_Phase5_Observability.md) |
| PR-12 | [13_PR12_Phase6A_AFM_Session_Distill.md](./13_PR12_Phase6A_AFM_Session_Distill.md) |
| PR-13 | [14_PR13_Phase6B_Synthesis_Procedures.md](./14_PR13_Phase6B_Synthesis_Procedures.md) |
| PR-14 | [15_PR14_Phase6C_Reorg_Pruning.md](./15_PR14_Phase6C_Reorg_Pruning.md) |
| PR-15 | [16_PR15_Phase7_Quantized_Semantic.md](./16_PR15_Phase7_Quantized_Semantic.md) |
