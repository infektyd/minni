---
description: Recall Sovereign Memory for the given query. Searches the Claude Code vault and the shared daemon, returns ranked context with provenance.
---

Use the `sovereign_recall` MCP tool to recall memory for: $ARGUMENTS

Pass these arguments:
- `query`: $ARGUMENTS
- `agentId`: `claude-code`
- `includeVault`: `true`
- `limit`: `8`

Read the returned context. If a result has `agent_origin` other than `claude-code`, note that another agent (Codex / Hermes / OpenClaw) wrote it — consider whether to follow up with a recall scoped to that agent.
