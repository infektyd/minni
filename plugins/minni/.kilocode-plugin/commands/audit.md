---
description: Show recent Minni tool activity from the KiloCode vault audit log.
---

Call `minni_audit_tail` with `limit: 20` (or the number in `$ARGUMENTS` if numeric). Then call `minni_audit_report` for a tool-call histogram.

Present:
- The histogram (which tools fired, how often).
- The last 5–10 entry headers in chronological order, one line each.
- Any `hook_error` entries — these indicate the spine misfired and deserve attention.
