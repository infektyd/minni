# AFM consolidation loop — plan & build notes

Status: **built, NOT yet enabled.** Code is staged in source; the running daemon
(PID at write time: launchd `com.minni.minnid`) has NOT been restarted, so none
of this is live yet. Go-live is a deliberate operator step (see "Go-live").

Date: 2026-06-01. Author: claude-code (operator console).

## Problem (verified)

`learnings`/`documents` counts are frozen (1265 / 773; last real learning
2026-05-30) while agents write daily. Root cause, confirmed in code + DB:

1. `learn` is **proposal-first** (`minnid.py:1584`): every write lands in
   `candidate_packets` (status `proposed`), not in `learnings`. 265 proposed,
   2 accepted ever, 446 inbox JSON.
2. Promotion (`resolve_candidate accept`) is **operator-only and manually
   triggered** — and nothing triggers it. `consolidation_actions` = 0 rows.
3. There was **no AFM loop runner**: `main()` only spawned the socket/HTTP
   servers. The `afm_loop_schedule` config + `_afm_loop_enabled` flag + 5 passes
   + on-demand `compile_vault` RPC all existed, but nothing ticked the schedule.
   0 drafts ever written; no afm log activity. The "loop" was config describing
   an engine that didn't exist.

## What was built

### 1. The missing loop runner — `minnid.py`
`async def _afm_loop_runner()`: gated by `_afm_loop_enabled()`. Every
`idle_seconds`, checks each scheduled pass's `interval_seconds` vs last-run
(in-memory) and fires due passes via the existing `_handle_daemon_compile`
path with `dry_run=False`. Defensive: every pass run wrapped in try/except so a
bad pass logs and is skipped — it can NEVER crash the socket server task.
Spawned in `main()` only when enabled (so an accidental restart with the flag
still off loads it dormant).

### 2. Consolidation pass — `engine/afm_passes/consolidation.py`
New pass #6. Pure triage over `candidate_packets WHERE status='proposed'`
(capped per run, deferred count logged — no silent truncation). Conservative,
deterministic v1 (AFM scoring hook left for later, matching the other passes'
"deterministic-first" design). Per candidate:

- **duplicate** (normalized content already in `learnings` or seen this run)
  → `dedup_candidate_ids` (daemon marks `rejected`, reason logged).
- **instruction_like** → review draft (humans must approve instructions).
- **privacy_level not in (safe, '', null)** → review draft (sensitive).
- **too short / trivial** → review draft.
- else → `promote_candidate_ids` (safe, non-instruction, non-dup).

Returns `{promote_candidate_ids, dedup_candidate_ids, drafts(review), summary}`.
In `dry_run`, decides but writes nothing.

### 3. Durable promotion in the daemon — `minnid.py`
`_promote_candidate_durable(candidate_id, reason)`: mirrors the `force=true`
durable-learn path (embed via `wb.model` + INSERT `learnings` + derived-from
edges + write-to-disk), flips candidate to `accepted`, writes a
`consolidation_actions` audit row (`action_type='afm_promote'`,
`target_learning_id`). Promotion **also embeds**, so it fixes the frozen FAISS
staleness for promoted content. `_apply_consolidation_result()` applies
promotions + dedup rejections after the pass (skipped on dry_run). Privileged
durable write stays in the daemon, not scattered into passes.

### 4. Schedule registration — `config.py`
Adds `consolidation` to `afm_loop_schedule.passes` with a modest interval.

## Quality gate (added, option A — done 2026-06-01)

`_quality_blockers(content)` in the pass routes structural garbage to review
instead of promoting it: oversized blobs (>8 KB), low character diversity
(repeated-char filler like `yyyy…`), too few distinct words, low alphabetic
ratio (gibberish). Verified on a DB snapshot: caught the seeded filler/blobs
(5 → review), still promoted real-looking content. It deliberately does NOT
reject plausible short facts — those need de-seeding (option B purge), not a
quality gate.

## Scaling — dupes are expected; the system must absorb them (esp. swarms)

Confirmed behavior, by design: ~39/50 of the current backlog are EXACT
duplicates and are dropped, not promoted. A swarm will generate far more.
Two hardening items before this is swarm-grade (NOT yet built):

1. **Throughput:** current `max_per_run=50` @ 15 min ≈ 200/hr drain. A swarm can
   out-produce that. Fix: drain in a loop per tick until the proposed queue is
   empty OR a per-tick time/`max` budget is hit (so bursts clear, not trickle).
2. **Dedup cost at scale:** the pass rebuilds the normalized-content set from all
   `learnings` each run — O(N) per tick. At tens of thousands of learnings that's
   heavy. Fix: add a normalized-content hash column + index (or a UNIQUE index)
   so dedup is an O(1) indexed lookup, optionally enforced at insert. Then dup
   volume costs nothing regardless of swarm size.

## Scaling build — DONE + validated (2026-06-01)

Both hardening items built and validated on a DB snapshot (live daemon untouched):

1. **Drain loop** (`_afm_loop_runner`): consolidation runs batch-after-batch per
   tick until the proposed queue is empty or `max_batches_per_tick` (40 ×
   max_per_run 50 = 2000/tick) is hit. Every decision moves the candidate out of
   `proposed`, so it strictly progresses.
2. **Indexed dedup**: `learnings.content_hash` column + index, backfilled
   idempotently; dedup is an O(log N) indexed lookup, not an O(N) in-memory scan.
   Promote path writes the hash. Swarm dup-floods cost ~nothing.

**Bug found + fixed by the test:** `candidate_packets.status` has a CHECK
constraint (`proposed/accepted/rejected/redacted/expired/merged/superseded`). The
first design used a new `needs_review` status → would have crashed the daemon at
runtime. Fix: review items stay `proposed` but get an `afm_review` marker in
`consolidation_actions`; the pass excludes marked rows (drain loop still
progresses; nothing re-drafted) and they remain operator-resolvable.

**Validated drain of the real 265-candidate backlog (on snapshot):**
- 7 batches → queue drained.
- promoted **37** (≈14%, novel) → learnings 1265 → **1302**.
- deduped **219** (≈83%, exact duplicates) → rejected.
- review-flagged **9** (≈3%) → stay `proposed` for the operator.
- re-run examined = **0** (terminates, idempotent, no spin).

Confirms the swarm thesis: the system absorbs the duplicate flood (219 collapsed)
and grows only by the genuinely-new (37).

## Governance (unchanged intent)

The gate stays meaningful: only `privacy: safe`, non-`instruction_like`,
non-duplicate candidates auto-promote. Instruction-like / sensitive /
contradictory candidates are surfaced as review drafts and stay `proposed`
for human decision. Every action is audited in `consolidation_actions`.

## Go-live (operator step — do when Codex is at a stopping point)

The live daemon must NOT be restarted under an active Codex session.
When clear:

1. Add to `~/Library/LaunchAgents/com.minni.minnid.plist` →
   `EnvironmentVariables` dict: `<key>MINNI_AFM_LOOP</key><string>on</string>`.
2. `launchctl kickstart -k gui/$(id -u)/com.minni.minnid` (≈1–2s socket blip;
   Codex hooks/recall reconnect on next call).
3. Verify: `minni_status` → `afm_loop` not "disabled"; tail daemon log for
   `_afm_loop_runner` ticks; `consolidation_actions` row count > 0; `learnings`
   count climbs as the 265 backlog drains over successive ticks.

**First run should be dry-run** to preview promotions before any durable write
(call `compile_vault pass_name=consolidation dry_run=true`, inspect, then enable
the loop). See "Bug-test" — validated in isolation first.

## Bug-test (no live restart)

Validated against a COPY of the DB on a throwaway socket/home before go-live —
see `/tmp/afm-consolidation-test/`. The live daemon is untouched.
