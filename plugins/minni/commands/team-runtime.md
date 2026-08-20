---
description: Project one vault Thread as a Team packet (plan_id + readySlices).
---

Call `minni_team_runtime` with:
- `task`: $ARGUMENTS
- `plan_id`: the Thread to project, when you already have one
- `profile`: `standard`
- `includeVault`: `true`

`minni_team_runtime` projects one vault Thread. `plan_id` present reads that Thread and fails if it is missing. `plan_id` absent creates one Thread from the task. Ready is the expiry sweep plus `readySlices`. The leftover `taskLedger` name is a view of `ready`, not a second graph. Do not pass `coordinatorAgentId`. The server stamps it (G11).

`buildWorkerPacketAfterClaim` and `dispatchWorkerPacket` are library adapters, not MCP tools. Do not treat `hydrationPackets` as the worker contract.

This does not spawn agents, promote profiles, learn, or write vault notes. grok worker-start is missing. Default agy cannot run `minni_thread_worker_update`. Codex dispatch is UNPROVEN and `spawned` is false. G3 daemon relay, automatic spawning, and immediate wake are not implemented.
