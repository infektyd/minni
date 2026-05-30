---
name: minni
description: Use when the user asks the agent to recall, learn, write, audit, or operate through Minni, or when a task likely benefits from prior local memory. Default automatic behavior is recall-only; do not learn unless explicitly requested.
version: 0.1.0
author: Infektyd
tags: [minni, obsidian, llm-wiki, local-first, mcp]
compatibility: Requires minni MCP server running (node dist/server.js) and the Minni daemon at the canonical per-user socket.
---

# Minni for KiloCode

Use this skill to operate KiloCode's local Minni bridge and the KiloCode-owned Obsidian vault. Minni is shared across multiple agents (KiloCode, Claude Code, Codex, Hermes, OpenClaw) — each has its own vault, but they all talk to the same daemon and can recall each other's notes, tagged with `agent_origin`.

## KiloCode Spine Integration

When loaded as a KiloCode plugin, Minni wires four hooks into the session:

- **SessionStart** — boots identity context, recent audit, and any pending learnings from the inbox.
- **UserPromptSubmit** — auto-recalls before each turn and injects ranked vault + daemon results.
- **PreCompact** — captures scar tissue (failed paths, dead ends) so post-compaction doesn't re-walk them.
- **Stop** — drafts candidate learnings into the vault inbox (never auto-writes); next session reviews them.

All hook output is wrapped in `<sovereign:context version="1" event="..." agent="kilocode" tokens="...">` envelopes containing JSON. Parse the JSON; don't reformat the envelope. Disable with `MINNI_KILOCODE_HOOKS=off`.

The KiloCode vault lives at `~/.minni/kilocode-vault` (override: `MINNI_KILOCODE_VAULT_PATH`). The Claude Code vault at `~/.minni/claudecode-vault` is a peer, not a parent — they share a daemon, not a directory.

## Default Behavior

- On tasks that likely benefit from prior local context, call `minni_status` first if available, then use `minni_recall` with a narrow query.
- Automatic behavior is recall-only.
- Do not call `minni_learn` unless the user explicitly asks to remember, learn, save to memory, or write a durable note.
- Do not run AFM extraction, session mining, adapter training, or staging review in v1.
- Keep private session content, adapter files, launchd plists, datasets, DB files, and raw vault contents out of public git.

## When to Use

- User asks to recall or search prior memory/context
- User wants to learn, remember, or save something to durable memory
- User asks for audit logs or status of the memory system
- Starting a complex task that benefits from prior context (recall first)
- Finishing a task and wanting to capture learnings
- Delegating to a subagent that needs context (use handoff)
- User asks about Minni or the vault

## When NOT to Use

- Pure code-generation tasks with no memory component
- Tasks where the user has explicitly disabled memory
- When the minni MCP server is unavailable

## Quick Reference

| Action | Tool | Key Args |
|--------|------|----------|
| Recall context | `minni_recall` | `query`, `agentId: "kilocode"`, `limit: 8` |
| Check health | `minni_status` | `vaultPath` (optional) |
| Learn something | `minni_learn` | `title`, `content`, `category`, `source`, `agentId: "kilocode"` |
| Quality check | `minni_learning_quality` | `title`, `content`, `category`, `source` |
| Write vault note | `minni_vault_write` | `title`, `content`, `section`, `source` |
| Prepare for task | `minni_prepare_task` | `task`, `agentId: "kilocode"`, `profile` |
| Dry-run outcome | `minni_prepare_outcome` | `task`, `summary`, `changedFiles`, `verification` |
| Route intent | `minni_route` | `task` |
| Audit tail | `minni_audit_tail` | `limit: 20` |
| Audit report | `minni_audit_report` | `limit: 20` |
| Handoff | `minni_negotiate_handoff` | `task`, `agentId: "kilocode"`, `toAgent` |
| Compile vault | `minni_compile_vault` | `passName`, `dryRun: true` |

## Tool Details

- `minni_route`: Classify whether a task should recall, learn, write a note, show audit, check status, or do nothing.
- `minni_status`: Check daemon, AFM bridge, vault path, and recent audit state.
- `minni_prepare_task`: Build a compact task packet with ranked context, source reasons, privacy metadata, and optional AFM distillation.
- `minni_prepare_outcome`: Build a dry-run post-task outcome packet without writing durable memory.
- `minni_recall`: Search existing Minni, prepend a vault context pack, and log the lookup.
- `minni_learning_quality`: Review a potential memory before writing it.
- `minni_learn`: Write a vault note first, quality-report it, then store the learning through Minni.
- `minni_vault_write`: Write a structured Obsidian note without durable learning.
- `minni_audit_report`: Summarize recent memory tool activity.
- `minni_audit_tail`: Show recent memory audit entries.
- `minni_negotiate_handoff`: Build an agent-to-agent handoff envelope (identity, top recalls with provenance, scar tissue, open questions, inbox pointer) optimized for another LLM to consume — use before delegating to a subagent or another session.
- `minni_compile_vault`: Dry-run AFM compile passes: session_distillation, synthesis, procedure_extraction, reorganization, pruning.

## Slash Commands

- `/minni:recall <query>` — quick recall against the KiloCode vault + daemon.
- `/minni:learn` — commit a durable learning (vault-first, quality-checked).
- `/minni:status` — daemon + AFM + vault health.
- `/minni:audit` — recent tool activity from the audit log.
- `/minni:prepare-task <task>` — ranked task packet before complex work.
- `/minni:prepare-outcome` — dry-run outcome packet, no writes.

## Vault Rules

The KiloCode vault defaults to `~/.minni/kilocode-vault` (override: `MINNI_KILOCODE_VAULT_PATH`).

- `raw/` is immutable raw sources.
- `wiki/` is agent-maintained synthesis.
- `schema/AGENTS.md` is the operating schema.
- `index.md` catalogs pages.
- `log.md` and `logs/YYYY-MM-DD.md` are append-only transparency logs.
- `inbox/` holds candidate learnings drafted by the Stop hook awaiting next-session review.

For Karpathy-style LLM wiki behavior, prefer short sourced pages with Obsidian wikilinks over opaque hidden memory. The vault is the visible surface; SQLite/FTS/FAISS is the recall machinery.

## Cross-Agent Awareness

When a recalled snippet has `agent_origin` other than `kilocode` (e.g., Claude Code wrote it), treat it as authoritative for what *that agent* concluded — but verify before acting on it in your own context. If it's load-bearing, recall scoped to that agent or read the source note directly.

## Workflow Patterns

### Starting a complex task

1. Call `minni_status` to check system health
2. Call `minni_prepare_task` with the task description and `agentId: "kilocode"`
3. Review the returned `brief`, `constraints`, `relevantSources`, and `risks`
4. Proceed with implementation using the context

### Finishing a task

1. Call `minni_prepare_outcome` with task summary, changed files, and verification results
2. Review `outcomeDraft.learnCandidates` — if any are worth committing, call `minni_learn`
3. The `doNotStore` list is non-negotiable: never commit those even if asked

### Delegating to another agent

1. Call `minni_negotiate_handoff` with the task, your agent ID (`kilocode`), and the target agent
2. Pass the returned envelope to the subagent as context
3. The envelope includes top recalls, scar tissue, and open questions

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MINNI_KILOCODE_VAULT_PATH` | `~/.minni/kilocode-vault` | KiloCode vault location |
| `MINNI_KILOCODE_AGENT_ID` | `kilocode` | Agent identity string |
| `MINNI_KILOCODE_WORKSPACE_ID` | `workspace-<dir>` | Workspace identifier |
| `MINNI_KILOCODE_HOOKS` | `on` | Set to `off` to disable hooks |
| `MINNI_SOCKET_PATH` | `~/.minni/run/sovrd.sock` | Minni daemon socket |
| `MINNI_AFM_HEALTH_URL` | `http://127.0.0.1:11437/health` | AFM bridge health URL |

## Red Flags

- **STOP** if `minni_learn` is called without explicit user request. Automatic behavior is recall-only.
- **STOP** if you're about to write secrets, tokens, raw logs, or local absolute paths to the vault.
- **STOP** if `doNotStore` items from `prepare_outcome` are being committed — honor them always.
- **STOP** if hook output envelope is being reformatted — parse the JSON inside, don't touch the wrapper.
