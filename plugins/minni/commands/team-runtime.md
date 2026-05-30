---
description: Build a temporary Sovereign Team Runtime packet with profiles, task ledger, and hydration packets.
---

Call `minni_team_runtime` with:
- `task`: $ARGUMENTS
- `coordinatorAgentId`: `claude-code`
- `profile`: `standard`
- `includeVault`: `true`

Use the returned `temporaryProfiles`, `taskLedger`, and `hydrationPackets` to coordinate helper agents. This does not spawn agents, promote profiles, learn, or write vault notes.
