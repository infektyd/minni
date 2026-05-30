---
name: sovereign-memory
description: Use when the user asks Gemini to recall, learn, write, audit, or operate through Sovereign Memory, or when a task likely benefits from prior local memory. Default automatic behavior is recall-only; do not learn unless explicitly requested.
---

# Sovereign Memory for Gemini

Use this skill to operate Gemini's local Sovereign Memory bridge and Gemini-owned Obsidian vault. It connects to the shared Sovereign Memory daemon and allows for durable, transparent context.

## Default Behavior

- On tasks that likely benefit from prior local context, call `sovereign_status` first if available, then use `sovereign_recall` with a narrow query.
- Automatic behavior is recall-only.
- Do not call `sovereign_learn` unless the user explicitly asks to remember, learn, save to memory, or write a durable note.
- Do not run AFM extraction, session mining, adapter training, or staging review in v1.
- Keep private session content, adapter files, launchd plists, datasets, DB files, and raw vault contents out of public git.

## Manual Tools

- `sovereign_route`: Classify whether a task should recall, learn, write a note, show audit, check status, or do nothing.
- `sovereign_status`: Check daemon, AFM bridge, vault path, and recent audit state.
- `sovereign_prepare_task`: Build a compact Gemini task packet with ranked context, source reasons, privacy metadata, and optional AFM distillation.
- `sovereign_prepare_outcome`: Build a dry-run post-task outcome packet without writing durable memory.
- `sovereign_recall`: Search existing Sovereign Memory, prepend a Sovereign vault context pack, and log the lookup.
- `sovereign_learning_quality`: Review a potential memory before writing it.
- `sovereign_learn`: Write a Sovereign vault note first, quality-report it, then store the learning through Sovereign Memory.
- `sovereign_vault_write`: Write a structured Obsidian note without durable learning.
- `sovereign_audit_report`: Summarize recent memory tool activity.
- `sovereign_audit_tail`: Show recent memory audit entries.
- `sovereign_negotiate_handoff`: Build an agent-to-agent handoff envelope.

## Vault Rules

Gemini's vault defaults to `~/.gemini/sovereign-vault` (or similarly scoped).

- `raw/` is immutable raw sources.
- `wiki/` is Gemini-maintained synthesis.
- `schema/AGENTS.md` is the operating schema.
- `index.md` catalogs pages.
- `log.md` and `logs/YYYY-MM-DD.md` are append-only transparency logs.

For Karpathy-style LLM wiki behavior, prefer short sourced pages with Obsidian wikilinks over opaque hidden memory. The vault is the visible surface; SQLite/FTS/FAISS is the recall machinery.
