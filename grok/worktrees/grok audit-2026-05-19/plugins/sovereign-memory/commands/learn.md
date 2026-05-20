---
description: Commit a durable learning to the Claude Code vault and the Sovereign Memory daemon. Vault-first; quality-checked.
---

Commit a learning from this session: $ARGUMENTS

Steps:

1. If `$ARGUMENTS` is empty or just a hint, first call `sovereign_audit_tail` (limit 30) and review the pending inbox under `~/.sovereign-memory/claudecode-vault/inbox/` to surface candidate learnings. Pick the strongest one (durable, sourced, specific).
2. Draft a `title` (8+ chars, durable, searchable) and `content` (12+ words, complete fact/decision/procedure with source).
3. Call `sovereign_learning_quality` with `{title, content, category, source}` to pre-check. If the report's `ok` is false, refine the draft before writing.
4. Call `sovereign_learn` with `{title, content, category, source, agentId: "claude-code", requireQuality: true}`. Vault note is written first, then the daemon stores the learning.
5. If a matching inbox file fed this learning, mention its path so the user knows it can be archived next session.

Never commit secrets, tokens, raw logs, or local absolute paths.
