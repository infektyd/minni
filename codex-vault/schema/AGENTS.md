# Codex Sovereign Memory Vault

This vault is Codex's local-first LLM wiki for Sovereign Memory.

## Operating Rules

- Treat `raw/` as immutable raw sources. Do not edit raw source notes after writing them; create a new note if the source changes.
- Treat `wiki/` as Codex-maintained synthesis. Keep pages short, sourced, and linked with Obsidian wikilinks.
- Prefer durable facts, decisions, procedures, and user preferences over full chat transcripts.
- Default automatic behavior is recall-only. Do not write learnings unless the user explicitly asks or a tool call is explicitly manual.
- Keep private session content, adapter files, launchd plists, datasets, and generated DB state out of public git.
- Update `index.md` and append to `log.md` whenever Codex creates or learns from a note.

## Layout

- `raw/`: raw sources and session excerpts.
- `wiki/entities/`: people, projects, repos, services, machines, and named systems.
- `wiki/concepts/`: reusable ideas and patterns.
- `wiki/decisions/`: decisions with rationale.
- `wiki/syntheses/`: cross-source summaries and comparisons.
- `wiki/sessions/`: task/session learnings written as durable notes.
- `logs/`: daily audit entries for tool transparency.
