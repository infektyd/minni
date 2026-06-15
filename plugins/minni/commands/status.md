---
description: Report Minni health for Claude Code — daemon socket, AFM bridge, vault, audit tail.
---

Call the `minni_status` MCP tool. It takes no arguments — the vault is fixed server-side to the operator's Claude Code vault (`vaultPath` was removed from the model-facing schema to prevent attacker redirection).

Summarize for the user in one paragraph:
- Daemon socket health (ok/error + reason).
- AFM bridge health (ok/error).
- Vault path and whether it exists.
- Latest audit entry, if any.

If the daemon socket is missing, suggest checking that `engine/minnid.py` is running.
