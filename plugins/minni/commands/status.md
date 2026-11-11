---
description: Report Minni health for Claude Code — daemon socket, AFM bridge, vault, recall-to-use stats, and TODAY.md digest.
---

Call the `minni_status` MCP tool. It takes no arguments — the vault is fixed server-side to the operator's Claude Code vault (`vaultPath` was removed from the model-facing schema to prevent attacker redirection).

Summarize for the user in one concise paragraph:
- Daemon socket health (`ok`/error + reason).
- AFM / local judge health (`ok`/degraded).
- Vault path, audit summary, and 7-day recall-to-use stats.
- Remind the user they can also skim `~/.minni/TODAY.md` or run `minni status` / `minni activity` in the terminal for live plans, map freshness, and conflicts.

If the daemon socket is missing, suggest starting the daemon with `minni up`.
