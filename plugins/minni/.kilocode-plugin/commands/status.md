---
description: Report Minni health for KiloCode — daemon socket, AFM bridge, vault, audit tail.
---

Call the `minni_status` MCP tool. If the user supplied an alternate vault path in `$ARGUMENTS`, pass it as `vaultPath`; otherwise let it default to the KiloCode vault.

Summarize for the user in one paragraph:
- Daemon socket health (ok/error + reason).
- AFM bridge health (ok/error).
- Vault path and whether it exists.
- Latest audit entry, if any.

If the daemon socket is missing, suggest checking that the Minni daemon is running.
