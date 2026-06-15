---
description: Recall Minni for the given query. Searches the Claude Code vault and the shared daemon, returns ranked context with provenance.
---

Use the `minni_recall` MCP tool to recall memory for: $ARGUMENTS

Pass these arguments:
- `query`: $ARGUMENTS
- `includeVault`: `true`
- `limit`: `8`

(Agent identity is stamped server-side as `DEFAULT_AGENT_ID`; the tool no longer accepts a client-supplied `agentId`, to prevent identity spoofing.)

Read the returned context. If a result has `agent_origin` other than `claude-code`, note that another agent (Codex / Hermes / OpenClaw) wrote it — consider whether to follow up with a recall scoped to that agent.
