---
description: Build a compact task packet — ranked vault sources, daemon recall, constraints, risks — before tackling complex work.
---

Call `minni_prepare_task` with:
- `task`: $ARGUMENTS
- `profile`: `standard` (use `compact` for quick tasks, `deep` for architecture work)
- `includeVault`: `true`

(Agent identity is stamped server-side as `DEFAULT_AGENT_ID`; the tool no longer accepts a client-supplied `agentId`, to prevent identity spoofing.)

Read the packet's `brief`, `constraints`, `relevantSources`, and `risks` before writing any code. The packet's `recommendedNextActions` is a starting point — adapt to the specifics.
