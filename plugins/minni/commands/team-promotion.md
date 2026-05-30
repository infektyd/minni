---
description: Review a temporary team agent for permanent-profile promotion without writing durable memory.
---

Call `minni_team_promotion` with:
- `agent`: the temporary profile from `minni_team_runtime`
- `evidence`: the matching candidate from `minni_team_evidence`
- `approved`: `false` unless the user explicitly approved promotion
- `requestedPermissions`: only the permissions the permanent profile should hold

The tool returns a promotion review packet. It never writes a vault note, stores a durable profile, or increases permissions by itself.
