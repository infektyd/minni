---
description: Commit a durable learning to the Claude Code vault and the Minni daemon. Vault-first; quality-checked.
---

Commit a learning from this session: $ARGUMENTS

Steps:

1. If `$ARGUMENTS` is empty or just a hint, first call `minni_list_candidates` and review the pending inbox under `~/.minni/claudecode-vault/inbox/` to surface candidate learnings. Pick the strongest one (durable, sourced, specific).
2. Draft a `title` (8+ chars, durable, searchable) and `content` (12+ words, complete fact/decision/procedure with source), plus `category` (`architecture`, `codebase`, `decision`, `fix`, `pattern`, `preference`, `procedure`, `fact`, or `general`).
3. Call `minni_learn` with `{title, content, category, source, requireQuality: true}` (principal identity is stamped server-side; never pass client `agentId`). If the built-in quality check rejects a draft, refine and retry.
4. If the user explicitly asked to approve a staged candidate row immediately and your principal holds operator governance, call `minni_resolve_candidate` with `{candidate_id, decision: "accept"}`.
5. If a matching inbox file fed this learning, mention its path so the user knows it can be archived next session.

Never commit secrets, tokens, raw logs, or local absolute paths.
