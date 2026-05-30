---
description: Summarize temporary team evidence reports and identify promotion candidates without writing memory.
---

Call `sovereign_team_evidence` after helper agents report back:
- `task`: the original team task
- `runtimeId`: the `runtimeId` from `sovereign_team_runtime` if available
- `results`: one entry per temporary agent with `agentId`, `status`, `summary`, and any `evidence`, `changedFiles`, `verification`, or `blockers`

Review `promotionCandidates` manually. Promotion is never automatic, and durable learnings still require `/sovereign-memory:learn`.
