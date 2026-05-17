---
description: Show recent Sovereign Memory tool activity from the Claude Code vault audit log.
---

Call `sovereign_audit_tail` with `limit: 20` (or the number in `$ARGUMENTS` if numeric). Then call `sovereign_audit_report` for a tool-call histogram.

Present:
- The histogram (which tools fired, how often).
- The last 5–10 entry headers in chronological order, one line each.
- Any `hook_error` entries — these indicate the spine misfired and deserve attention.
