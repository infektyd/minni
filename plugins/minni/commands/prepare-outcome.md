---
description: Dry-run an outcome packet — learn candidates, log-only items, expirations, do-not-store list — without writing memory.
---

Call `sovereign_prepare_outcome` with:
- `task`: a one-line description of what was attempted
- `summary`: what actually happened (success/failure, key changes, verification done)
- `changedFiles`: relevant file paths if known
- `verification`: what tests/checks were run
- `profile`: `compact`

This is dry-run only — nothing is written. Review the `outcomeDraft.learnCandidates`. If any are worth committing, follow up with `/sovereign-memory:learn`. The draft's `doNotStore` list is non-negotiable: never commit those even if asked.
