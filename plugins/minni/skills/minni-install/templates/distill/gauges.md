---
type: minni-distill-gauges
agent: {{agent}}
last_updated: {{timestamp}}
version: 1
mode: explicit
---

# Minni Distill Gauges

Live context meter for the Minni Distill Ritual V1. Read this file FIRST at any
wind-down signal — do not reason about your own token usage. Maintained by the
agent during each distill; seeded once by `minni-install` and not wholesale-
overwritten on re-seed (issue #254: a frozen `identity_present: "not seeded"`
line is surgically healed when `layer1/core.md` is on disk).

## Pressure Signals
- recent_turns: 0 (freshly seeded, no burst recorded yet)
- recent_tool_activity: none
- time_since_last_high_signal_minni_write: unknown (no distill has run in this vault)
- pending_inbox_count: unknown (check `inbox/`)

## Layer 1 Reference
- identity_present: {{layer1_identity_present}}
- last_layer1_context_summary: {{layer1_summary}}

## Recent Burst
- burst_description: (none yet — first distill will fill this in)
- significant_artifacts: []

## Decision Aids
- pressure_level: low
- recommended: "no action"
- future_route_signals: []

## Last Distill Outcome
- last_distill_timestamp: (never)
- summary: No distill has run against this vault yet. Gauges seeded at install
  time with honest empty values so the first wind-down signal reads real state
  instead of running blind.
